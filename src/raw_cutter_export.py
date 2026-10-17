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
import uuid
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
                    on_progress: Callable[[int, int, str], None] | None = None,
                    on_complete: Callable[["SegmentResult"], None] | None = None
                    ) -> list[SegmentResult]:
    """Export every planned segment into ``run_directory``.

    **Staged write, then an atomic no-replace publication (R1).** FFmpeg writes to a task-owned
    staging file; only a validated staging file is renamed into place. The earlier version let
    FFmpeg write the final name directly and deleted *that path* on failure, which meant a failed
    export could remove a file some other process had created in the window after the existence
    check -- a file this run never owned. Now the only thing ever deleted is our own staging
    artifact, and **the destination is never removed on any path**.

    The publication is a single same-directory ``os.rename``. On Windows that raises
    ``FileExistsError`` when the destination exists, so the check and the move are one indivisible
    step: there is no window in which a collision can be missed, and a second run can never destroy
    the first one's output. Same volume by construction (staging lives inside the run directory),
    so ``EXDEV`` is structurally impossible rather than merely unlikely.

    Staging names carry a per-attempt token rather than a predictable ``.part`` suffix: a fixed
    name is a latent collision in which a retry truncates the first attempt's in-flight write.

    Other properties, each deliberate:

    * The source is opened read-only by FFmpeg and is never a destination argument.
    * Destinations resolve INSIDE ``run_directory``, measured with ``commonpath`` rather than
      assumed from a string prefix.
    * **`on_complete` fires as each segment finishes**, so a caller that is interrupted by
      ``CutterCancelled`` still knows exactly what was written. Returning the list is not enough:
      the exception discards it.
    * **Nothing fallible runs between a successful publication and `on_complete` (R4).** The
      clip's size is measured on the staging file *before* the rename, so a published clip can
      never be lost from the caller's accounting because a later metadata read failed. See the
      comment at the measurement itself for the reproduction this came from.
    * Completed segments are never touched by a later failure or by cancellation.
    """
    results: list[SegmentResult] = []
    total = plan.count
    root = os.path.abspath(run_directory)

    def record(result: SegmentResult) -> None:
        results.append(result)
        if on_complete is not None:
            on_complete(result)

    for segment in plan.segments:
        if job is not None and job.cancelled:
            raise CutterCancelled()
        name = fork_cutter.segment_filename(source.path, segment.index, total)
        destination = os.path.abspath(os.path.join(root, name))
        if os.path.commonpath([root, destination]) != root:
            record(SegmentResult(index=segment.index, error="Refused an unsafe path."))
            continue
        if os.path.exists(destination):
            # A courtesy check so a doomed segment costs no encoding. The rename below is the
            # authority -- this is deliberately not treated as proof the name is free.
            record(SegmentResult(index=segment.index, path=destination,
                                 error="A file with this name already exists; refused."))
            continue

        token = uuid.uuid4().hex[:12]
        staging = os.path.join(root, fork_cutter.staging_filename(
            source.path, segment.index, total, token))

        if on_progress is not None:
            on_progress(segment.index, total, name)

        command = build_segment_command(source.path, staging, segment.start, segment.duration,
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
            valid, message = _validate_output(staging)
            if not valid:
                failure = message

        # [FORK] R4 (R3-F6): the size is measured on the STAGING file, BEFORE publication.
        #
        # It used to be read off the destination immediately after the rename, which put one
        # fallible filesystem call between a successful publication and the `record()` that
        # fires `on_complete`. An independent review reproduced the consequence: with a one-shot
        # `OSError` injected there, six clips were encoded, validated and published -- the sixth
        # independently FFprobed as a valid 4 s h264 file -- yet only five callbacks fired, so the
        # batch reported "5 of 6" and classified a source that had published EVERY planned clip as
        # a partial export. `on_complete` is the only evidence the batch layer has, so a clip that
        # never reaches it is a clip the manifest denies producing.
        #
        # Measuring first removes the window instead of trying to recover from it: once the rename
        # succeeds there is nothing left that can fail before the callback. The value is identical
        # either way -- a same-directory rename relinks a directory entry and does not touch the
        # bytes -- so this is strictly a reordering, not a different measurement.
        #
        # A failure HERE is an ordinary segment failure: the rename has not run, so no destination
        # exists, and the only thing discarded below is this attempt's own staging file. Validation
        # passing is deliberately not treated as licence to report success without a size.
        published_size = 0
        if not failure:
            try:
                published_size = os.path.getsize(staging)
            except OSError as exc:
                failure = f"Could not measure the finished clip: {exc}"

        if not failure:
            try:
                os.rename(staging, destination)
            except FileExistsError:
                failure = ("Another file appeared with this name while the clip was encoding; "
                           "refused rather than replacing it.")
            except OSError as exc:
                failure = f"Could not publish the finished clip: {exc}"

        if failure:
            # ONLY our own staging artifact. The destination is never removed -- if it exists it
            # belongs to somebody else, and this run has no claim on it.
            _discard_staging(staging)
            record(SegmentResult(index=segment.index, path=destination, error=failure))
        else:
            # No filesystem call between the rename above and this record(). That is the R3-F6
            # invariant; do not reintroduce one.
            record(SegmentResult(index=segment.index, path=destination, ok=True,
                                 size_bytes=published_size))

        if job is not None and job.cancelled:
            raise CutterCancelled()

    return results


def _discard_staging(path: str) -> None:
    """Remove only the task-owned staging artifact this attempt created. Never anything else.

    Deliberately narrow: no directory sweep, no glob, no pattern match. Another invocation's
    staging file may be in flight in the same folder, and an interrupted process may legitimately
    leave one behind -- that is acceptable and must not block a later run.
    """
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# Batch V1: discovery and the batch output layout
#
# [FORK] Digital-Union (Raw Cutter Batch V1). Everything below is the filesystem half of batch
# mode; the decisions are `beatsync_fork.raw_cutter_batch`. Nothing here re-implements V1 --
# `probe_source`, `detect_scenes` and `export_segments` above are called unchanged, once per
# source, by the loop in `gui.py`.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class DiscoveryResult:
    """The frozen input list for one batch. Order is deterministic and fixed before any work."""

    root: str = ""
    recursive: bool = True
    paths: tuple[str, ...] = ()
    rejected_count: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def discover_batch_sources(folder: str, recursive: bool) -> DiscoveryResult:
    """Enumerate the videos a batch will process, in a deterministic frozen order.

    **Reuses `input_manager.scan_folder` rather than walking the tree again.** That function already
    owns the two properties this needs and would be wrong to re-derive: traversal is
    all-or-nothing, so a directory that cannot be listed raises instead of quietly yielding a
    smaller library, and the result is ordered by the project's existing case-insensitive
    `order_key` with a total-order tie-break, so two scans of the same tree agree exactly. A frozen
    deterministic list is what makes a batch's manifest reproducible.

    `detect_duplicates=False` is deliberate and is the one deviation from the render path's call:
    duplicate grouping fingerprints files (size plus SHA-256 of the first and last 1 MiB), and a
    folder of multi-gigabyte footage would pay that cost before a single clip was cut. Batch mode
    cuts whatever it is pointed at; it does not de-duplicate, so the read would buy nothing.

    The extension set is Raw Cutter's own `SUPPORTED_SOURCE_SUFFIXES`, not the render pipeline's
    narrower `.mp4`/`.mkv` default -- the cutter accepts MOV, M4V, AVI and WebM too, and a batch
    must accept exactly what Single Video mode accepts.
    """
    from beatsync_fork import input_manager

    if not isinstance(folder, str) or not folder.strip():
        return DiscoveryResult(error="Enter the folder that holds your source videos.")
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
    if not os.path.exists(root):
        return DiscoveryResult(root=root, error=f"Folder not found:\n{root}")
    if not os.path.isdir(root):
        return DiscoveryResult(root=root, error=f"Not a folder:\n{root}")

    try:
        found = input_manager.scan_folder(
            root, recursive=bool(recursive),
            extensions=SUPPORTED_SOURCE_SUFFIXES,
            detect_duplicates=False)
    except input_manager.InputScanError as exc:
        return DiscoveryResult(root=root, recursive=bool(recursive), error=str(exc))
    except OSError as exc:
        return DiscoveryResult(root=root, recursive=bool(recursive),
                               error=f"Could not read the folder: {exc}")

    return DiscoveryResult(
        root=found.root,
        recursive=bool(recursive),
        paths=tuple(item.path for item in found.files),
        rejected_count=len(found.rejected),
    )


def resolve_batch_roots(input_folder: str, output_folder: str) -> tuple[str, str, str]:
    """Fully resolve both roots and refuse a nesting loop. Returns ``(input, output, error)``.

    `realpath` runs on BOTH before the comparison, which is the whole point: the containment
    decision has to see through a junction or a symlink, because an output path that merely *looks*
    separate may resolve inside the footage tree, and then the next scan re-ingests this run's own
    clips. The same resolution pattern the delivery share file uses, and for the same reason --
    a reparse point is resolved and then judged on where it actually lands, never refused on sight.

    The output folder does not have to exist yet; its nearest existing ancestor is resolved instead,
    so a brand-new destination is judged on where it will really be.
    """
    if not isinstance(output_folder, str) or not output_folder.strip():
        return "", "", "Choose an output folder for the finished clips."

    source = os.path.abspath(os.path.expandvars(os.path.expanduser(str(input_folder).strip()
                                                                   .strip('"'))))
    target = os.path.abspath(os.path.expandvars(os.path.expanduser(output_folder.strip()
                                                                   .strip('"'))))
    if os.path.exists(target) and not os.path.isdir(target):
        return "", "", f"The output path is not a folder:\n{target}"

    try:
        resolved_source = os.path.realpath(source)
    except OSError:
        resolved_source = source

    # Resolve the deepest ancestor that exists, then re-attach the rest: `realpath` on a path whose
    # parents do not exist yet returns it unchanged on Windows, which would skip the junction.
    probe, tail = target, []
    while probe and not os.path.exists(probe):
        probe, piece = os.path.split(probe)
        if not piece:
            break
        tail.append(piece)
    try:
        resolved_target = os.path.join(os.path.realpath(probe), *reversed(tail)) if probe else target
    except OSError:
        resolved_target = target

    from beatsync_fork import raw_cutter_batch as fork_batch
    conflict = fork_batch.output_root_conflict(resolved_source, resolved_target)
    if conflict:
        return "", "", conflict
    return resolved_source, resolved_target, ""


def prepare_batch_directory(output_root: str, timestamp: str) -> tuple[str, str]:
    """Create the one new ``RawCutter_Batch_<timestamp>`` folder. Returns ``(path, error)``.

    `exist_ok=False` on the batch folder itself, exactly as `prepare_run_directory` does for a
    single run: the creation IS the test, so a batch can never adopt a directory that already holds
    somebody else's clips. Intermediate parents are created normally -- an output root the operator
    typed but has not made yet is a convenience, not a collision risk.
    """
    from beatsync_fork import raw_cutter_batch as fork_batch

    if not isinstance(output_root, str) or not output_root.strip():
        return "", "Choose an output folder."
    root = os.path.abspath(output_root.strip())
    destination = os.path.join(root, fork_batch.batch_directory_name(timestamp))
    if os.path.exists(destination):
        return "", f"This batch folder already exists; refusing to reuse it:\n{destination}"
    try:
        os.makedirs(root, exist_ok=True)
        os.makedirs(destination, exist_ok=False)
    except OSError as exc:
        return "", f"Could not create the batch folder:\n{destination}\n{exc}"
    return destination, ""


def prepare_item_directory(batch_directory: str,
                           relative_parts: Sequence[str]) -> tuple[str, str]:
    """Create one source's own folder inside the batch folder. Returns ``(path, error)``.

    The leaf is created with `exist_ok=False` so two sources can never share a folder even if the
    uniqueness planning upstream were ever wrong; the intermediate directories are shared by
    design, because preserving the input structure means several sources legitimately sit under the
    same parent.

    Containment is **measured** with `commonpath`, not assumed from the string the caller passed:
    a prefix test would accept ``output-evil`` for ``output``, and these components are derived from
    operator-supplied folder names.
    """
    root = os.path.abspath(batch_directory)
    if not relative_parts:
        return "", "No output location was planned for this source."
    destination = os.path.abspath(os.path.join(root, *relative_parts))
    try:
        if os.path.commonpath([root, destination]) != root:
            return "", "Refused an unsafe output path for this source."
    except ValueError:
        return "", "Refused an unsafe output path for this source."
    parent = os.path.dirname(destination)
    try:
        os.makedirs(parent, exist_ok=True)
        os.makedirs(destination, exist_ok=False)
    except OSError as exc:
        return "", f"Could not create the clip folder:\n{destination}\n{exc}"
    return destination, ""


def write_batch_manifest(batch_directory: str, payload: dict) -> str:
    """Write the per-source record into the batch folder. Returns ``''`` or an error string.

    Written **once**, from the caller's `finally`, so it exists after a failure or a cancellation
    too -- a manifest that only appeared on the happy path would be worthless exactly when it is
    needed. Unique temp then a no-replace `os.rename`, same-directory, so a partial file never
    appears under the real name and an existing manifest is never destroyed.

    It never raises: a batch that wrote clips successfully must not be reported as failed because
    its bookkeeping file could not be saved.
    """
    import json

    from beatsync_fork import raw_cutter_batch as fork_batch

    root = os.path.abspath(batch_directory)
    destination = os.path.join(root, fork_batch.MANIFEST_FILENAME)
    staging = os.path.join(root, f".{fork_batch.MANIFEST_FILENAME}.{uuid.uuid4().hex[:12]}.part")
    try:
        with open(staging, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(staging, destination)
        return ""
    except (OSError, TypeError, ValueError) as exc:
        _discard_staging(staging)
        return f"Could not write {fork_batch.MANIFEST_FILENAME}: {exc}"


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
