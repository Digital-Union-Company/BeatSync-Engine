"""Raw Cutter V1: the FFmpeg adapter.

[FORK] Digital-Union. Everything in Raw Cutter that touches a process or the filesystem lives
here; :mod:`beatsync_fork.raw_cutter` decides and this performs. The split is the reason the whole
segmentation policy is testable on a bare interpreter.

Scope boundary: this module probes one file, detects its scene changes and exports segments. It
reaches no render, no Stage-5 cache, no Qwen worker, no output promotion and no `RenderLifecycle`.
Cutting raw footage is not a render and must not acquire a render's machinery.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Sequence

from beatsync_fork import raw_cutter as fork_cutter
from ffmpeg_processing import (
    FFMPEG_PATH,
    FFPROBE_PATH,
    _extract_scene_times,
    _run_media_command,
    _short_ffmpeg_error,
)
from gpu_cpu_utils import MAX_THREADS

#: Scene detection reads the whole file once at a small analysis size. Generous, because a long
#: source is the entire point of this tool, but still bounded.
SCENE_TIMEOUT_SECONDS = 1800

#: Probing is metadata only.
PROBE_TIMEOUT_SECONDS = 60

#: Per-segment export bound. A 5-minute maximum segment re-encoded on CPU is the worst realistic
#: case; this leaves room for it without letting a wedged child hang the job forever.
EXPORT_TIMEOUT_SECONDS = 1800

#: Matches `detect_video_scene_changes`'s own analysis geometry, so the Raw Cutter and Stage 5 see
#: the same cuts for the same threshold rather than two slightly different estimates.
ANALYSIS_FPS = 8.0
ANALYSIS_WIDTH = 384

SUPPORTED_SOURCE_SUFFIXES = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")


class CutterCancelled(Exception):
    """Raised inside the worker when the operator pressed Cancel.

    An ordinary ``Exception``, deliberately -- the same choice `RenderCancelled` makes -- so a
    broad ``except Exception`` on this path must name it first if it wants to distinguish a stop
    from a failure.
    """


class CutterJob:
    """The cancellation token for one cut run. One flag, one lock, nothing else.

    Deliberately NOT a `RenderLifecycle`: that type carries render outcome classes, an active-render
    slot and promotion semantics this feature has no business touching.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise CutterCancelled()


# ---------------------------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeResult:
    source: fork_cutter.SourceInfo | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.source is not None and not self.error


def _parse_rate(text: object) -> float:
    """FFprobe reports a rational like ``30000/1001``. A zero denominator is 'unknown', not 0."""
    try:
        value = str(text or "").strip()
        if "/" in value:
            num, den = value.split("/", 1)
            denominator = float(den)
            return round(float(num) / denominator, 6) if denominator else 0.0
        return round(float(value), 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_source(path: str) -> ProbeResult:
    """One bounded metadata probe of a single local video file.

    Total: every failure becomes a reported `error` string. Validation happens here rather than in
    the GUI so there is one answer to "is this file usable?".
    """
    if not isinstance(path, str) or not path.strip():
        return ProbeResult(error="Enter the full path to a video file.")
    candidate = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if not os.path.exists(candidate):
        return ProbeResult(error=f"File not found:\n{candidate}")
    if not os.path.isfile(candidate):
        return ProbeResult(error=f"Not a file:\n{candidate}")
    if os.path.splitext(candidate)[1].lower() not in SUPPORTED_SOURCE_SUFFIXES:
        return ProbeResult(error=(
            f"Unsupported file type '{os.path.splitext(candidate)[1]}'. Supported: "
            + ", ".join(SUPPORTED_SOURCE_SUFFIXES)))

    command = [
        FFPROBE_PATH, '-v', 'error',
        '-show_entries', 'format=duration',
        '-show_entries', 'stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration',
        '-of', 'json', candidate,
    ]
    try:
        result = _run_media_command(command, timeout=PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return ProbeResult(error="FFprobe timed out reading this file.")
    except OSError as exc:
        return ProbeResult(error=f"Could not run FFprobe: {exc}")
    if result.returncode != 0:
        return ProbeResult(error=f"FFprobe could not read this file:\n"
                                 f"{_short_ffmpeg_error(result.stderr, max_chars=600)}")

    import json
    try:
        payload = json.loads(result.stdout or "{}")
    except (ValueError, TypeError):
        return ProbeResult(error="FFprobe returned output this tool could not parse.")

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return ProbeResult(error="This file has no video stream.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    duration = 0.0
    for source_value in (payload.get("format", {}).get("duration"), video.get("duration")):
        try:
            candidate_duration = float(source_value)
        except (TypeError, ValueError):
            continue
        if candidate_duration > 0:
            duration = candidate_duration
            break
    if duration <= 0:
        return ProbeResult(error="This file does not report a usable duration.")

    try:
        stat = os.stat(candidate)
    except OSError as exc:
        return ProbeResult(error=f"Could not read file metadata: {exc}")

    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return ProbeResult(source=fork_cutter.SourceInfo(
        path=candidate,
        duration=round(duration, 3),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        has_audio=has_audio,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    ))


# ---------------------------------------------------------------------------------------------
# Scene detection
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SceneResult:
    """Scene detection that distinguishes 'no cuts' from 'detection did not work'.

    `ffmpeg_processing.detect_video_scene_changes` returns ``[]`` for BOTH, which is correct for
    Stage 5 (an unsegmented source is still analysable) and wrong here: reporting a failed detector
    as a successful zero-cut analysis would hand the operator one giant time-split clip and call it
    the footage's own structure. So this is a separate, smaller wrapper around the same FFmpeg
    invocation rather than a call into that function.
    """

    times: tuple[float, ...] = ()
    succeeded: bool = False
    error: str = ""
    elapsed_seconds: float = 0.0


def detect_scenes(path: str, threshold: float, job: CutterJob | None = None) -> SceneResult:
    """Run one CPU scene-score pass and report whether it actually ran."""
    command = [
        FFMPEG_PATH, '-nostdin', '-hide_banner',
        '-threads', str(MAX_THREADS),
        '-i', path,
        '-vf', (f"fps={ANALYSIS_FPS},"
                f"scale={ANALYSIS_WIDTH}:-2:flags=fast_bilinear,"
                f"select='gt(scene,{threshold})',showinfo"),
        '-an', '-sn', '-dn',
        '-f', 'null', '-',
    ]
    started = time.perf_counter()
    try:
        if job is not None:
            job.raise_if_cancelled()
        result = _run_media_command(command, timeout=SCENE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return SceneResult(succeeded=False, error="Scene detection timed out.",
                           elapsed_seconds=round(time.perf_counter() - started, 2))
    except CutterCancelled:
        raise
    except OSError as exc:
        return SceneResult(succeeded=False, error=f"Could not run FFmpeg: {exc}",
                           elapsed_seconds=round(time.perf_counter() - started, 2))
    elapsed = round(time.perf_counter() - started, 2)
    if result.returncode != 0:
        return SceneResult(succeeded=False, elapsed_seconds=elapsed,
                           error=_short_ffmpeg_error(result.stderr, max_chars=800)
                                 or "Scene detection failed.")
    return SceneResult(times=tuple(_extract_scene_times(result.stderr)), succeeded=True,
                       elapsed_seconds=elapsed)


# ---------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SegmentResult:
    index: int
    path: str = ""
    ok: bool = False
    error: str = ""
    size_bytes: int = 0


def build_segment_command(source_path: str, destination: str, start: float, duration: float,
                          remove_audio: bool, encoder: str, has_audio: bool) -> list[str]:
    """The argv for one segment. A LIST, never a shell string, and never with ``-y``.

    `-ss` before `-i` is the fast accurate seek: FFmpeg seeks to the preceding keyframe and decodes
    forward, so the output starts on the requested frame while skipping the decode of everything
    before it. `-t` bounds the piece by duration rather than by an absolute end, which keeps each
    export independent of the others.

    No `-vf`, no `-s`, no `-r`: the source's own resolution, aspect ratio and frame rate are
    preserved by saying nothing about them. `-map 0:v:0` pins the video stream explicitly so a
    multi-stream container cannot silently export a different one than was probed.
    """
    command = [
        FFMPEG_PATH, '-nostdin', '-hide_banner', '-v', 'error',
        '-ss', f"{start:.3f}",
        '-i', source_path,
        '-t', f"{duration:.3f}",
        '-map', '0:v:0',
    ]
    if remove_audio or not has_audio:
        # `-an` drops the stream itself, not merely its level: the output container ends up with
        # no audio stream at all, which is what "Remove Audio" has to mean to be worth having.
        command.append('-an')
    else:
        command.extend(['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '192k'])

    if encoder == 'h264_nvenc':
        command.extend(['-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', '21', '-pix_fmt', 'yuv420p'])
    else:
        command.extend(['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                        '-pix_fmt', 'yuv420p'])
    command.extend(['-movflags', '+faststart', destination])
    return command


def _validate_output(path: str) -> tuple[bool, str]:
    """A completed file is one that exists, is non-empty and probes as real video."""
    if not os.path.exists(path):
        return False, "FFmpeg reported success but produced no file."
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"Could not stat the exported file: {exc}"
    if size <= 0:
        return False, "FFmpeg produced an empty file."
    probe = [FFPROBE_PATH, '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type', '-of', 'csv=p=0', path]
    try:
        result = _run_media_command(probe, timeout=PROBE_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Could not verify the exported file: {exc}"
    if result.returncode != 0 or 'video' not in (result.stdout or ''):
        return False, "The exported file does not contain a readable video stream."
    return True, ""


def export_segments(source: fork_cutter.SourceInfo, plan: fork_cutter.SegmentPlan,
                    run_directory: str, remove_audio: bool, encoder: str,
                    job: CutterJob | None = None,
                    on_progress: Callable[[int, int, str], None] | None = None
                    ) -> list[SegmentResult]:
    """Export every planned segment into ``run_directory``.

    Safety properties, each deliberate:

    * The source is opened read-only by FFmpeg and is never a destination argument.
    * Destinations are built from the planner's own sanitised names and always resolve INSIDE
      ``run_directory``, which is checked per file rather than assumed.
    * No `-y`. A collision is a refusal, not an overwrite, so a second run can never destroy the
      first one's output.
    * A failed or cancelled segment's partial file is removed -- but only that file, which this
      call exclusively created moments earlier.
    * Completed segments are never touched by a later failure or by cancellation.
    """
    results: list[SegmentResult] = []
    total = plan.count
    root = os.path.abspath(run_directory)

    for segment in plan.segments:
        if job is not None and job.cancelled:
            raise CutterCancelled()
        name = fork_cutter.segment_filename(source.path, segment.index, total)
        destination = os.path.abspath(os.path.join(root, name))
        if os.path.commonpath([root, destination]) != root:
            results.append(SegmentResult(index=segment.index, error="Refused an unsafe path."))
            continue
        if os.path.exists(destination):
            results.append(SegmentResult(index=segment.index, path=destination,
                                         error="A file with this name already exists; refused."))
            continue

        if on_progress is not None:
            on_progress(segment.index, total, name)

        command = build_segment_command(source.path, destination, segment.start, segment.duration,
                                        remove_audio, encoder, source.has_audio)
        failure = ""
        try:
            result = _run_media_command(command, timeout=EXPORT_TIMEOUT_SECONDS)
            if result.returncode != 0:
                failure = _short_ffmpeg_error(result.stderr, max_chars=400) or "FFmpeg failed."
        except subprocess.TimeoutExpired:
            failure = "FFmpeg timed out on this segment."
        except OSError as exc:
            failure = f"Could not run FFmpeg: {exc}"

        if not failure:
            valid, message = _validate_output(destination)
            if not valid:
                failure = message

        if failure:
            _discard_partial(destination)
            results.append(SegmentResult(index=segment.index, path=destination, error=failure))
        else:
            results.append(SegmentResult(index=segment.index, path=destination, ok=True,
                                         size_bytes=os.path.getsize(destination)))

        if job is not None and job.cancelled:
            raise CutterCancelled()

    return results


def _discard_partial(path: str) -> None:
    """Remove only the incomplete file this operation just created. Never anything else."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def prepare_run_directory(output_root: str, source_path: str, timestamp: str) -> tuple[str, str]:
    """Create the fresh per-run subfolder. Returns ``(path, error)``.

    `os.makedirs(..., exist_ok=False)` is the test and the creation in one step, so a run can never
    adopt a directory that already held someone else's clips.
    """
    if not isinstance(output_root, str) or not output_root.strip():
        return "", "Choose an output folder."
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(output_root.strip().strip('"'))))
    if os.path.exists(root) and not os.path.isdir(root):
        return "", f"The output path is not a folder:\n{root}"
    name = fork_cutter.run_directory_name(source_path, timestamp)
    destination = os.path.join(root, name)
    if os.path.exists(destination):
        return "", f"This run folder already exists; refusing to reuse it:\n{destination}"
    try:
        os.makedirs(destination, exist_ok=False)
    except OSError as exc:
        return "", f"Could not create the output folder:\n{destination}\n{exc}"
    return destination, ""
