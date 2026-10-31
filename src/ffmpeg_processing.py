#!/usr/bin/env python3
"""
FFmpeg Processing Module - Frame-Accurate Video Operations
- Frame-perfect segment extraction
- Zero timing drift
"""

import os
import sys

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

import subprocess
import time
import json
import random
import shutil
import uuid
import re
from typing import Tuple, List


from logger import (
    setup_environment,
    FFMPEG_EXE as FFMPEG_PATH,
    check_nvenc as check_nvenc_support,
)
from gpu_cpu_utils import MAX_THREADS

# [FORK] Digital-Union (Phase 3B): bounded FFmpeg failure summaries (stdlib-only fork module).
from beatsync_fork import ffmpeg_diagnostics as fork_diagnostics
# [FORK] Digital-Union (C3-R1A): the pure cancellation contract (stdlib-only fork module).
from beatsync_fork.render_worker import RenderCancelled
# [FORK] Digital-Union (FUN R10 Final Frame Fix): the frame-exact segment contract (stdlib-only
# fork module). Same one-way direction as every fork import here. It owns the pure decisions --
# what the progress stream said, where to re-seek, how a shortfall is worded -- while this module
# keeps the one thing it must not have: a child process.
from beatsync_fork import frame_lock as fork_frame_lock
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): the read-only verification contract (stdlib-only fork
# module). Imported for the probe's result record and its bound only; the hard rule is one-way --
# a runtime module may import the fork package, never the reverse.
from beatsync_fork.media_verify import (
    DEFAULT_PROBE_TIMEOUT_SECONDS as _MEDIA_VERIFY_PROBE_TIMEOUT,
    ProbeOutcome,
)
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P1): the sample-decode contract (stdlib-only fork
# module). Same one-way direction as the import above -- the runtime may import the fork package,
# never the reverse, which is what keeps `media_sample` testable on a bare interpreter.
from beatsync_fork.media_sample import (
    DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS as _MEDIA_SAMPLE_DECODE_TIMEOUT,
    DEFAULT_SAMPLE_FRAMES as _MEDIA_SAMPLE_FRAMES,
    SampleDecodeOutcome,
)

# Initialize environment
setup_environment()

# Set up FFPROBE_PATH based on FFMPEG_PATH
FFPROBE_PATH = FFMPEG_PATH.replace('ffmpeg.exe', 'ffprobe.exe')


NVENC_QUALITY_CQ = '1'
NVENC_LOOKAHEAD = '32'
NVENC_AQ_STRENGTH = '12'

#: Poll interval while a cancellable command runs, and the bounded grace given to a terminated
#: child before escalating to a kill. Short enough that Cancel-to-quiescent stays in the
#: measured ~tens-of-milliseconds class for a single ffmpeg.exe child; long enough that it does
#: not become a busy loop.
_CANCEL_POLL_SECONDS = 0.15
_TERMINATE_GRACE_SECONDS = 5.0


def _run_media_command(cmd: List[str], timeout: int,
                       lifecycle=None) -> subprocess.CompletedProcess[str]:
    """Run an FFmpeg/FFprobe command with consistent capture settings.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` is optional and defaults to ``None``, which is
    every existing caller today -- that path is byte-for-byte the original blocking
    ``subprocess.run(..., timeout=timeout)`` call, unchanged.

    When a :class:`~beatsync_fork.render_worker.RenderLifecycle` is supplied, the child is polled
    cooperatively: ``Popen.communicate(timeout=...)`` is the stdlib's own documented safe-to-retry
    pattern for exactly this (it accumulates output across repeated calls rather than losing any).
    On a matching cancellation request the child is terminated, given a bounded grace, escalated to
    a kill only if still alive, and reaped -- ``RenderCancelled`` is raised only once that reap has
    actually completed, never while the child may still be alive. A real command timeout (the
    ``timeout`` argument, measured independently of the poll interval) still raises
    ``subprocess.TimeoutExpired`` exactly as before; a timeout is never reported as cancellation.
    """
    if lifecycle is None:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_SECONDS)
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if lifecycle.cancel_requested():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    # The process is now provably reaped (communicate() returned), so quiescence
                    # is proven before this raises -- never while the child may still be alive.
                    raise RenderCancelled(
                        f"ffmpeg command cancelled: {os.path.basename(cmd[0]) if cmd else ''}")
                if time.perf_counter() - started > timeout:
                    proc.kill()
                    proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    raise subprocess.TimeoutExpired(cmd, timeout)
    finally:
        # Defensive only: every return/raise above already reaps the child first. This exists so
        # an unforeseen exception inside the loop can never leave a live orphan behind it.
        if proc.poll() is None:
            try:
                proc.kill()
                proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except Exception:
                pass


def run_cancellable_media_command(cmd: List[str], timeout: int,
                                  lifecycle=None) -> subprocess.CompletedProcess[str]:
    """[FORK] Digital-Union (C3-R1A / R2): the one public entry point to the runner above.

    A deliberately thin, reviewed alias rather than a second implementation. `gui.py`'s ProRes
    preview is the only caller outside this module, and it needs exactly the behaviour
    `_run_media_command` already has: ``lifecycle=None`` is the original blocking
    ``subprocess.run(..., timeout=timeout)``, and a supplied lifecycle polls cancellation,
    terminates, graces, kills if necessary and **reaps** the child before ``RenderCancelled``
    escapes. A genuine command timeout still raises ``subprocess.TimeoutExpired``.

    It exists so that `gui.py` does not reach across a module boundary for a private name, and so
    that this module stays the single owner of media-subprocess execution: duplicating the poll /
    terminate / grace / kill / reap logic in the GUI is exactly the kind of second process-handling
    path that eventually diverges. The child handle still belongs entirely to the call frame
    `_run_media_command` creates it in — nothing is stored at module scope here or anywhere else.
    """
    return _run_media_command(cmd, timeout, lifecycle=lifecycle)


def _safe_remove_file(path: str | None) -> None:
    """Best-effort removal for temporary media files."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f"   ⚠️  Could not remove temporary file {os.path.basename(path)}: {e}")


def _short_ffmpeg_error(stderr: str, max_chars: int = 2200) -> str:
    """Return the useful tail of FFmpeg stderr without flooding the UI/log."""
    if not stderr:
        return ""
    text = stderr.strip()
    if len(text) <= max_chars:
        return text
    return "..." + text[-max_chars:]


def _fmt_seconds(seconds: float) -> str:
    try:
        value = float(seconds)
    except Exception:
        value = 0.0
    if value < 1.0:
        return f"{value * 1000:.0f}ms"
    return f"{value:.1f}s"


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _extract_scene_times(stderr: str) -> List[float]:
    """Parse showinfo pts_time values from FFmpeg stderr."""
    scene_changes: List[float] = []
    for line in stderr.split('\n'):
        if 'pts_time:' in line:
            match = re.search(r'pts_time:([\d.]+)', line)
            if match:
                try:
                    scene_changes.append(float(match.group(1)))
                except ValueError:
                    pass
    scene_changes = sorted(set(round(t, 3) for t in scene_changes if t >= 0.0))
    cleaned: List[float] = []
    for t in scene_changes:
        if not cleaned or t - cleaned[-1] >= 0.08:
            cleaned.append(t)
    return cleaned


def get_nvenc_quality_args(gpu_encoder: str, include_pix_fmt: bool = True) -> List[str]:
    """Return high-quality NVENC settings for H.264/HEVC exports."""
    args = [
        '-c:v', gpu_encoder,
        '-preset', 'p7',
        '-tune', 'uhq' if gpu_encoder == 'hevc_nvenc' else 'hq',
        '-rc', 'vbr',
        '-b:v', '0',
        '-cq', NVENC_QUALITY_CQ,
        '-multipass', 'fullres',
        '-rc-lookahead', NVENC_LOOKAHEAD,
        '-spatial_aq', '1',
        '-temporal_aq', '1',
        '-aq-strength', NVENC_AQ_STRENGTH,
        '-b_ref_mode', 'middle',
    ]

    if gpu_encoder == 'h264_nvenc':
        args.extend(['-profile:v', 'high'])
    elif gpu_encoder == 'hevc_nvenc':
        args.extend(['-profile:v', 'main'])

    if include_pix_fmt:
        args.extend(['-pix_fmt', 'yuv420p'])

    return args


def get_cpu_h264_quality_args(include_pix_fmt: bool = True) -> List[str]:
    """Return lossless CPU H.264 settings."""
    args = [
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-crf', '0',
    ]
    if include_pix_fmt:
        args.extend(['-pix_fmt', 'yuv420p'])
    args.extend(['-threads', str(MAX_THREADS)])
    return args


def get_video_duration(video_file: str) -> float:
    """Get the duration of a video file using ffprobe."""
    try:
        probe_cmd = [
            FFPROBE_PATH,
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_file
        ]
        
        result = _run_media_command(probe_cmd, timeout=10)
        duration = float(result.stdout.strip())
        return duration
    except Exception as e:
        print(f"   ⚠️  Could not get video duration with ffprobe: {e}")
        return 10.0  # Default fallback


def _probe_output_frame_count(output_file: str):
    """[FORK] Digital-Union (FUN R10 Final Frame Fix): frames a segment we just wrote declares.

    **Metadata only -- this never decodes a picture.** ``-show_entries stream=nb_frames`` reads the
    container header, so it costs one short child process rather than a full decode of the clip,
    which is why it is allowed to sit on the render path at all.

    Measured on the bundled FFmpeg build, over the whole reported-defect matrix: for the CFR
    MP4/MOV segments :func:`extract_clip_segment_ffmpeg_detailed` and
    :func:`extract_prores_segment_random` write, ``nb_frames`` agreed **exactly** with both
    FFmpeg's own ``-progress`` count and a full ``-count_frames`` decode (28/28, 34/34, 29/29,
    35/35). So the cheap read is the reliable one here; it is not a weaker approximation of the
    expensive one.

    Returns ``None`` when the value cannot be read, which the caller reports as *unmeasured* and
    never as zero. The handler is deliberately a **narrow** tuple rather than ``except
    Exception``: a broad one here would be a fourth generic handler in a module whose count is
    pinned precisely so a new one cannot arrive unnoticed, and these are the only failures a
    header read actually has -- a missing binary, a timed-out child, an unparseable value.
    """
    try:
        probe_cmd = [
            FFPROBE_PATH,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=nb_frames',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            output_file
        ]
        result = _run_media_command(probe_cmd, timeout=20)
        if result.returncode != 0:
            return None
        counted = int(result.stdout.strip())
        return counted if counted >= 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def get_video_fps(video_file: str) -> float:
    """Get the FPS of a video file using ffprobe."""
    try:
        probe_cmd = [
            FFPROBE_PATH,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=r_frame_rate',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_file
        ]
        
        result = _run_media_command(probe_cmd, timeout=10)
        fps_str = result.stdout.strip()
        
        # Parse fraction (e.g., "30000/1001" or "30/1")
        if '/' in fps_str:
            num, den = fps_str.split('/')
            fps = float(num) / float(den)
        else:
            fps = float(fps_str)
        
        return fps
    except Exception as e:
        print(f"   ⚠️  Could not get video FPS with ffprobe: {e}")
        return 30.0  # Default fallback


def get_video_resolution(video_file: str) -> Tuple[int, int]:
    """Get the resolution (width, height) of a video file using ffprobe."""
    try:
        probe_cmd = [
            FFPROBE_PATH,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height',
            '-of', 'json',
            video_file
        ]
        
        result = _run_media_command(probe_cmd, timeout=10)
        data = json.loads(result.stdout)
        
        width = data['streams'][0]['width']
        height = data['streams'][0]['height']
        
        return (width, height)
    except Exception as e:
        print(f"   ⚠️  Could not get video resolution with ffprobe: {e}")
        return (1920, 1080)  # Default fallback


# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): the read-only metadata probe.
#
# A pure argv builder plus the smallest bounded execution seam, placed beside the existing FFprobe
# helpers above and sharing their `FFPROBE_PATH` and `_run_media_command` conventions. It exists
# here rather than in `gui.py` so that no new `subprocess.run`/`Popen` site is added to the UI
# layer, and so the command itself can be asserted without running anything.
#
# Unlike `get_video_duration`/`get_video_fps`/`get_video_resolution` above, this returns the raw
# outcome instead of a fallback value. Those three are render-path helpers whose callers need *a*
# number to proceed; this is a diagnostic whose entire purpose is to report failure accurately, and
# a silent `10.0` default would be the exact dishonesty the milestone exists to remove.
#
# Nothing here is a cache input. It reads no cache record, writes nothing, launches no Qwen worker
# and touches no media: `-show_format -show_streams` reads headers only and never decodes a frame.
def build_ffprobe_metadata_command(video_file: str) -> List[str]:
    """The argv for one read-only metadata probe. An ARRAY, never a shell string.

    `-show_format` and `-show_streams` together give container duration plus per-stream codec,
    geometry and frame rate in one child process — one spin-up per source rather than the three the
    existing single-value helpers would cost.
    """
    return [
        FFPROBE_PATH,
        '-v', 'error',
        '-print_format', 'json',
        '-show_format',
        '-show_streams',
        video_file,
    ]


def probe_media_metadata(video_file: str,
                         timeout: float = _MEDIA_VERIFY_PROBE_TIMEOUT) -> ProbeOutcome:
    """Run one bounded metadata probe and report the outcome verbatim.

    Total by construction, because this runs inside a verification worker pool: every failure mode
    becomes a reported outcome rather than an exception that would abort a 2,000-source run.

    The three outcomes are kept DISTINCT rather than collapsed into one error string, because the
    verdict they imply differs:

    * `timed_out=True` — the probe exceeded its bound. Says nothing about the file's bytes; a
      mechanical drive under concurrent load is the likely cause, so the classifier reports
      UNVERIFIED rather than corrupt.
    * `error` — the seam itself failed (FFprobe missing, OS refused the spawn). We hold no opinion
      on the file at all.
    * a real `returncode`/`stdout`/`stderr` — FFprobe ran and judged the file.

    No `lifecycle` is passed to `_run_media_command`: that parameter is the render cancellation
    path, and verification is not a render. The plain `timeout` branch it takes is the original
    unchanged `subprocess.run(..., timeout=timeout)`.
    """
    cmd = build_ffprobe_metadata_command(video_file)
    try:
        result = _run_media_command(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return ProbeOutcome(timed_out=True)
    except (OSError, ValueError) as exc:
        return ProbeOutcome(error=f"{type(exc).__name__}: {exc}")
    return ProbeOutcome(
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


# ---------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P1): the one bounded sample-decode child.
#
# A pure argv builder plus the smallest bounded execution seam, beside the FFprobe pair above and
# sharing their `FFMPEG_PATH` / `_run_media_command` conventions. Nothing here classifies: the
# outcome travels verbatim into `beatsync_fork.media_sample`, whose decision table is pure and
# therefore assertable without a child process.
#
# This is a DIAGNOSTIC, not a render. It writes no media, reads no cache record, launches no Qwen
# worker and touches no source bytes. Unlike `extract_clip_segment_ffmpeg_detailed` it deliberately
# does NOT reproduce the render pipeline -- no filter chain, no NVENC, no output file -- so a clean
# result says the decoder produced frames, never that the production extraction would succeed.
def build_sample_decode_command(video_file: str, seek_seconds: float, stream_index: int,
                                requested_frames: int = _MEDIA_SAMPLE_FRAMES) -> List[str]:
    """The argv for one bounded sample decode. An ARRAY, never a shell string.

    Each option earns its place, and several are load-bearing rather than cosmetic:

    * ``-v error`` -- at this loglevel a clean run prints nothing, so a non-empty stderr IS the
      decoder-error signal and `media_sample` needs no fragile pattern matching. It also keeps the
      capture small (see `_run_media_command`'s unbounded-capture caveat below).
    * ``-nostdin`` -- a UI-launched child must never consume the parent's stdin.
    * exactly one ``-ss``, BEFORE ``-i`` -- input seeking, matching what the renderer actually does.
      There is no second output-side seek.
    * ``-map 0:<absolute index>`` -- the renderer passes no ``-map`` and so gets FFmpeg's automatic
      selection (highest resolution, ties by lowest index). `media_sample` reproduces that rule from
      metadata and pins the result by ABSOLUTE index, which is unambiguous in a way ``0:v:N`` is
      not.
    * ``-frames:v`` -- documented as "stop writing to the stream after framecount frames", i.e. a
      bound on OUTPUT frames. Frames decoded during accurate-seek pre-roll are exercised but never
      counted, which is why the status is named OUTPUT_FRAMES_OK.
    * ``-fps_mode passthrough`` -- the one deliberate divergence from the renderer's ``cfr``. Under
      CFR, FFmpeg can reach the requested count by DUPLICATING a single good frame, which would let
      a damaged source pass; passthrough removes that false-pass path entirely.
    * ``-progress pipe:1`` -- machine-readable ``key=value`` evidence on stdout, which
      `_run_media_command` already captures.
    * ``-f null -`` -- the null muxer, which generates no output file. No ``-y`` is needed because
      nothing is written.

    Deliberately absent: ``-xerror`` (we want FFmpeg to finish and report, not abort on the first
    complaint), ``-noaccurate_seek`` (we want the frames actually at the requested position), any
    retry, and any ``-vf``/encoder argument.
    """
    return [
        FFMPEG_PATH,
        '-hide_banner',
        '-v', 'error',
        '-nostdin',
        '-ss', str(seek_seconds),
        '-i', video_file,
        '-map', f'0:{int(stream_index)}',
        '-an', '-sn', '-dn',
        '-frames:v', str(int(requested_frames)),
        '-fps_mode', 'passthrough',
        '-progress', 'pipe:1',
        '-f', 'null', '-',
    ]


def sample_decode_video(video_file: str, *, seek_seconds: float, stream_index: int,
                        requested_frames: int = _MEDIA_SAMPLE_FRAMES,
                        timeout: float = _MEDIA_SAMPLE_DECODE_TIMEOUT) -> SampleDecodeOutcome:
    """Run one bounded sample decode and report the outcome verbatim.

    Total by construction: every failure mode becomes a reported outcome, because the caller is a
    read-only diagnostic whose entire purpose is to describe failure accurately.

    No ``lifecycle`` is passed to `_run_media_command`, matching `probe_media_metadata`: that
    parameter is the render cancellation path, and a diagnostic is not a render. The plain
    ``timeout`` branch it takes is the original unchanged ``subprocess.run(..., timeout=timeout)``.
    That choice also PRESERVES EVIDENCE -- `subprocess.run` attaches the partial capture to
    `TimeoutExpired`, whereas the lifecycle branch raises a bare `TimeoutExpired(cmd, timeout)` with
    no output at all. On timeout we therefore still learn how far the decode got.

    Those partial attributes are documented as **always bytes when any output was captured,
    regardless of the ``text=True`` setting**, and may be ``None``. They are handed on untouched;
    `media_sample.coerce_stream_text` owns the decoding, so this seam never has to guess an
    encoding. A timeout remains a timeout: partial progress reaching the requested count is never a
    successful decode.

    Caveat carried deliberately rather than papered over: `_run_media_command` captures stderr
    without a size limit. ``-v error`` makes large output unlikely, and the frame/timeout bounds cap
    how long a pathological file can keep writing, but neither establishes a hard memory bound.
    """
    cmd = build_sample_decode_command(video_file, seek_seconds, stream_index, requested_frames)
    try:
        result = _run_media_command(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return SampleDecodeOutcome(timed_out=True, stdout=exc.stdout, stderr=exc.stderr)
    except (OSError, ValueError) as exc:
        return SampleDecodeOutcome(error=f"{type(exc).__name__}: {exc}")
    return SampleDecodeOutcome(
        returncode=int(result.returncode),
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


def seconds_to_frame_count(seconds: float, fps: float) -> int:
    """
    Convert seconds to exact frame count.
    This ensures frame-accurate timing with no drift.
    """
    return int(round(seconds * fps))


def frame_count_to_seconds(frames: int, fps: float) -> float:
    """
    Convert frame count back to exact seconds.
    This is the EXACT duration for the frame count.
    """
    return frames / fps


#: [FORK] Digital-Union (Deliverable Export V1-A): the one delivery-preview profile. A single CPU
#: H.264 profile serves all four master modes, so the preview never depends on NVENC being present
#: and both arms of a comparison are encoded identically. A master-side NVENC preview is a separate,
#: later optimisation and deliberately out of scope here.
DELIVERY_PREVIEW_CRF = '21'
DELIVERY_PREVIEW_PRESET = 'veryfast'
DELIVERY_PREVIEW_AUDIO_BITRATE = '192k'
DELIVERY_PREVIEW_TIMEOUT_SECONDS = 1800


def build_delivery_preview_command(source_file: str, output_file: str) -> List[str]:
    """[FORK] Digital-Union (Deliverable Export V1-A): argv for one browser-playable preview.

    The master this reads is excellent and unshareable: every mode muxes ``pcm_s24le``, which lands
    in MP4 as ``ipcm`` (ISO/IEC 23003-5) and is not decodable by ordinary browsers, so a successful
    render currently hands the UI a file whose audio nothing in the page can play. ProRes and HEVC
    masters are additionally not AVC at all. One H.264/AAC MP4 fixes every mode at once.

    **Every encoder option is emitted AFTER ``-i``, and that ordering is the whole point.** The
    block this replaces put ``-c:v`` *before* the input, so FFmpeg read the encoder as an input
    **decoder** and both branches died on ``Unknown decoder 'h264_nvenc'`` / ``Unknown decoder
    'libx264'`` — measured against the pinned FFmpeg 8.1.2, non-zero exit, no file produced. A test
    pins the ordering positionally so the mistake cannot return.

    **Streams are mapped explicitly** (``-map 0:v:0 -map 0:a:0``) rather than left to FFmpeg's
    default selection. If the master somehow lacks either stream the encode must fail loudly: a
    silent video-only "preview" would be reported as a playable deliverable while being exactly the
    thing the user complained about.

    **No scaling, no rate option, no level pin.** Resolution and frame rate are preserved by
    omitting ``-vf``/``-s``/``-r`` entirely — the master is already CFR at the render's own fps, so
    re-stating it could only introduce a re-timing risk. ``-profile:v high`` is specified because it
    is universally supported; the **level is deliberately left to FFmpeg**, since a fixed ``4.1``
    would be wrong for a 4K or high-fps master.

    The video is re-encoded rather than stream-copied, and that is measured rather than assumed: on
    18 real renders from this pipeline, copying the ``-crf 0`` master and swapping only PCM->AAC
    moved 47.1 Mbps to 44.9 Mbps — a 4.7 % reduction. Copying fixes playability and not size.

    Raises ``ValueError`` when the destination is the source, because a preview that overwrote the
    master would destroy the one durable artifact this feature exists to protect.
    """
    if not source_file or not output_file:
        raise ValueError("delivery preview needs both a source and a destination")
    if os.path.normcase(os.path.abspath(source_file)) == \
            os.path.normcase(os.path.abspath(output_file)):
        raise ValueError(
            f"delivery preview destination must differ from the master: {output_file}")
    return [
        FFMPEG_PATH,
        '-nostdin',
        '-hide_banner',
        '-i', source_file,
        '-map', '0:v:0',
        '-map', '0:a:0',
        '-c:v', 'libx264',
        '-preset', DELIVERY_PREVIEW_PRESET,
        '-crf', DELIVERY_PREVIEW_CRF,
        '-pix_fmt', 'yuv420p',
        '-profile:v', 'high',
        '-c:a', 'aac',
        '-b:a', DELIVERY_PREVIEW_AUDIO_BITRATE,
        '-ar', '48000',
        '-ac', '2',
        '-movflags', '+faststart',
        '-y',
        output_file,
    ]


def convert_to_prores_proxy(video_file: str, output_dir: str, fps: float = None,
                            lifecycle=None) -> str:
    """
    Convert video to ProRes 422 Proxy for lossless editing.
    All frames are I-frames (keyframes) for frame-accurate cutting.
    STRIPS AUDIO - we'll add the music track at the end.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` defaults to ``None`` (unchanged behaviour); see
    ``_run_media_command``.
    """
    filename = os.path.basename(video_file)
    name, _ = os.path.splitext(filename)
    output_file = os.path.join(output_dir, f"{name}_prores.mov")
    
    print(f"   📹 Converting to ProRes 422 Proxy: {filename}")
    
    # Detect FPS if not provided
    if fps is None:
        fps = get_video_fps(video_file)
    
    # Build FFmpeg command for ProRes 422 Proxy (NO AUDIO).
    # ProRes encode/decode is CPU-native in FFmpeg; forcing hwaccel auto can make
    # FFmpeg pick Vulkan/D3D paths that are slower or unstable for ProRes.
    cmd = [
        FFMPEG_PATH,
        '-nostdin',
        '-hide_banner',
        '-i', video_file,
        '-map', '0:v:0',
        '-c:v', 'prores',  # ProRes encoder
        '-profile:v', '0',  # Proxy quality (0=Proxy, 1=LT, 2=Standard, 3=HQ)
        '-vendor', 'apl0',
        '-pix_fmt', 'yuv422p10le',
        '-an',  # ✅ STRIP AUDIO - we'll add music at the end
        '-sn',
        '-dn',
        '-r', str(fps),  # Set frame rate
        '-threads', str(MAX_THREADS),
        '-y',
        output_file
    ]
    
    try:
        result = _run_media_command(cmd, timeout=600, lifecycle=lifecycle)  # 10 minute timeout

        if result.returncode != 0:
            print(f"   ⚠️  FFmpeg error: {result.stderr}")
            raise Exception(f"ProRes conversion failed for {filename}")

        print(f"   ✓ ProRes conversion complete: {name}_prores.mov (video only, no audio)")
        return output_file

    except RenderCancelled:
        # [FORK] Digital-Union (C3-R1A): must escape unchanged -- the two except clauses below
        # would otherwise re-wrap it as a plain Exception and destroy its type.
        raise
    except subprocess.TimeoutExpired:
        raise Exception(f"ProRes conversion timeout for {filename}")
    except Exception as e:
        raise Exception(f"ProRes conversion error: {str(e)}")


def extract_clip_segment_ffmpeg(video_file: str, start_time: float, duration: float,
                                output_file: str, fps: float, target_size: Tuple[int, int],
                                use_nvenc: bool,
                                gpu_encoder: str = 'h264_nvenc') -> bool:
    """
    Extract a video segment using FFmpeg with FRAME-ACCURATE timing.

    ✅ FRAME-ACCURATE: Uses exact frame counts instead of floating-point seconds
    ✅ ZERO DRIFT: No cumulative timing errors

    Returns a plain bool, unchanged. [FORK] Digital-Union: callers that also want to know *why* a clip
    failed use :func:`extract_clip_segment_ffmpeg_detailed`; this signature stays boolean so existing
    and upstream callers are unaffected.
    """
    success, _reason = extract_clip_segment_ffmpeg_detailed(
        video_file=video_file, start_time=start_time, duration=duration,
        output_file=output_file, fps=fps, target_size=target_size,
        use_nvenc=use_nvenc, gpu_encoder=gpu_encoder,
    )
    return success


def extract_clip_segment_ffmpeg_detailed(video_file: str, start_time: float, duration: float,
                                         output_file: str, fps: float,
                                         target_size: Tuple[int, int], use_nvenc: bool,
                                         gpu_encoder: str = 'h264_nvenc',
                                         # [FORK] R8 Phase 6: BEFORE `lifecycle`. A seam test pins
                                         # `lifecycle` as the last parameter of every cancellable
                                         # entry point, so a new one goes in front of it. Every
                                         # caller passes both by keyword.
                                         geometry: str = '',
                                         lifecycle=None) -> Tuple[bool, str]:
    """[FORK] Digital-Union (Phase 3B): the same extraction, plus a bounded failure reason.

    Returns ``(success, reason)`` where ``reason`` is ``""`` on success. The FFmpeg command, the
    frame-accurate arithmetic, the encoder arguments and the success/failure conditions are all
    identical to the boolean version — the only addition is that a failure now carries the short
    explanation FFmpeg already printed, so Stage 6 can show it instead of a generic string.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` is optional and defaults to ``None`` -- absent, the
    FFmpeg call below is the original blocking call, byte-identical to today. A matching
    cancellation raises :class:`~beatsync_fork.render_worker.RenderCancelled`, which this function's
    own ``except Exception`` must never collapse into an ordinary ``(False, reason)`` failure tuple.

    [FORK] Digital-Union (R8 UX Refresh, Phase 6): ``geometry`` is the scale-and-pad filter chain
    for a fixed Output Video Profile, and it defaults to ``''`` meaning **Auto** -- the historical
    bare ``scale=width:height``, unchanged. The default is what makes every existing caller and
    every existing test still describe the same command. See the comment at the filter assembly
    for why a profile *replaces* the bare scale rather than being appended to it.

    [FORK] Digital-Union (FUN R10 Final Frame Fix): **a segment requesting N output frames now
    produces N, or fails.** Three things changed, and no parameter did:

    * ``-progress pipe:1`` is appended, so FFmpeg's own count of frames written to the output is
      captured for free alongside the stderr that was already being captured;
    * a measured shortfall buys **one** corrected re-seek (`frame_lock.recovery_seek`), anchored
      on the end of the source with one source frame of lead-in for accurate ``-ss`` to round
      forward into -- bounded by ``frame_lock.MAX_EXTRACTION_ATTEMPTS``, never a loop;
    * a shortfall that survives the re-seek returns ``(False, reason)``, so it becomes an ordinary
      clip failure carrying the full MEDIA-VERIFY-S1 context and ``create_music_video`` refuses the
      render rather than concatenating a short timeline.

    **The first attempt's argv is unchanged apart from ``-progress``**, because ``seek_time``
    starts at ``start_time`` -- so every clip that was already correct is extracted by the same
    command from the same position and is bit-identical. Only a clip that was measurably short
    takes the second attempt, and the only clips that were short are the ones flush against the
    end of their source. An **unmeasured** count is not a failure: if neither the progress stream
    nor the container header yields a number, the segment keeps the historical behaviour rather
    than being refused on the strength of a missing measurement.
    """
    try:
        # ✅ FRAME-ACCURATE: Calculate exact source and output frame counts.
        source_frame_count = max(1, seconds_to_frame_count(duration, fps))
        exact_source_duration = frame_count_to_seconds(source_frame_count, fps)
        output_frame_count = source_frame_count

        # Build filter complex
        filters = []

        # Trim first so each extracted segment has exact timing.
        filters.extend([f"trim=duration={exact_source_duration}", "setpts=PTS-STARTPTS"])

        # Scale to target size
        #
        # [FORK] Digital-Union (R8 UX Refresh, Phase 6): `geometry` is the scale-and-pad chain for
        # a fixed Output Video Profile, and `""` is Auto.
        #
        # The bare `scale={width}:{height}` below is the historical behaviour and is kept EXACTLY
        # for Auto -- including its defect, deliberately. It stretches any source whose aspect
        # ratio differs from the target, because the target is simply the first source's
        # resolution. Fixing that unconditionally would silently change the geometry of every
        # render an operator has already tuned and compared, so Auto is left untouched and the
        # corrected geometry is what choosing a profile buys.
        #
        # When `geometry` is supplied it REPLACES the bare scale rather than being appended to it:
        # the chain already contains its own `scale`, and running both would fit the picture twice.
        if geometry:
            filters.append(geometry)
        elif target_size:
            width, height = target_size
            filters.append(f"scale={width}:{height}")

        # FPS filter
        filters.append(f"fps={fps}")
        
        filter_complex = ",".join(filters)

        # [FORK] Digital-Union (FUN R10 Final Frame Fix): the seek is a RESOLVED value now, not the
        # argument itself. A window flush against the end of a source needs an earlier one -- see
        # `beatsync_fork/frame_lock.py` for the measurement. `seek_time` starts as `start_time`, so
        # the first attempt's argv is byte-identical to the historical one for every clip.
        seek_time = float(start_time)
        attempts = 0
        verdict = None

        while True:
            attempts += 1

            # Build FFmpeg command
            cmd = [FFMPEG_PATH]

            # Hardware acceleration
            # [FORK] Digital-Union (Phase 3C): the NVENC branch asks for no input hwaccel at all.
            # Measured on RTX 3080 / driver 617.14: '-hwaccel cuda' failed to initialise on most
            # sampled sources (33 > 32 nvdec decode surfaces) and FFmpeg fell back to software
            # decode anyway; where real NVDEC did engage it was ~20% slower, because this CPU
            # filter chain (trim/setpts/scale/fps) has to pull the frames back to system memory.
            # A/B outputs were byte-identical on 20/20 clips. Encoding is still h264_nvenc — only
            # the decode request is gone. The CPU-encode branch was not part of that measurement
            # and is unchanged.
            if not use_nvenc:
                cmd.extend(['-hwaccel', 'auto'])

            # ✅ FRAME-ACCURATE INPUT SEEKING
            # Use -ss BEFORE -i for faster seeking (keyframe-based)
            # Then use -ss AFTER -i for frame-accurate positioning
            cmd.extend([
                '-ss', str(seek_time),
                '-t', str(exact_source_duration),
                '-i', video_file
            ])

            # Video filters
            cmd.extend(['-vf', filter_complex])

            # ✅ FRAME-ACCURATE DURATION: Use -vframes instead of -t
            #
            # [FORK] FUN R10: this is also what makes the re-seek below safe in the one direction
            # that matters. A corrected window deliberately carries MORE content than the segment
            # needs, and `-vframes` bounds the output at exactly the requested count, so a
            # recovery can never produce a long segment.
            cmd.extend(['-vframes', str(output_frame_count)])

            # Video encoding
            if use_nvenc:
                cmd.extend(get_nvenc_quality_args(gpu_encoder, include_pix_fmt=True))
            else:
                cmd.extend(get_cpu_h264_quality_args(include_pix_fmt=True))

            # No audio, frame-accurate settings
            #
            # [FORK] FUN R10: `-progress pipe:1` is the frame-count evidence, and it is free --
            # both `_run_media_command` paths already capture stdout, the output goes to a file so
            # stdout is otherwise unused, and stderr (which the failure summariser reads) is
            # untouched. FFmpeg's `frame=` counts frames WRITTEN to the output, which is exactly
            # the quantity `-vframes` bounds, so no separate probe is needed on the happy path.
            cmd.extend([
                '-an',
                '-fps_mode', 'cfr',  # Constant frame rate
                '-r', str(fps),   # Exact output FPS
                '-fflags', '+genpts',
                '-movflags', '+faststart',
                '-y',
                '-progress', 'pipe:1',
                output_file
            ])

            result = _run_media_command(cmd, timeout=120, lifecycle=lifecycle)

            if result.returncode != 0:
                print(f"   ⚠️  FFmpeg error: {result.stderr}")
                # [FORK] The full text still goes to the console exactly as before; only a bounded
                # summary travels onward, because ProgressView is a status line, not a log sink.
                reason = fork_diagnostics.summarize_ffmpeg_failure(result.stderr)
                return False, reason or f"FFmpeg exited with code {result.returncode}"

            # Verify output exists and has content
            if not os.path.exists(output_file) or os.path.getsize(output_file) == 0:
                exists = os.path.exists(output_file)
                size = os.path.getsize(output_file) if exists else 0
                return False, fork_diagnostics.describe_output_problem(exists, size)

            # [FORK] Digital-Union (FUN R10 Final Frame Fix): exit code 0 plus a non-empty file is
            # NOT proof of a correct segment, and that is precisely how a one-frame-short clip
            # reached final assembly for as long as it did. Count what was actually written.
            verdict = fork_frame_lock.frames_from_progress(output_frame_count, result.stdout)
            if not verdict.exact:
                # Never act on a single reading. The container header is an independent
                # measurement of the same file, so a progress stream this build happened not to
                # emit -- or emitted oddly -- cannot on its own fail a good clip or trigger a
                # pointless re-extraction.
                confirmation = fork_frame_lock.from_container_count(
                    output_frame_count, _probe_output_frame_count(output_file))
                if confirmation.measured:
                    verdict = confirmation

            if verdict.exact or not verdict.measured:
                # Exact, or no evidence either way. An unmeasured segment keeps the historical
                # behaviour rather than being refused on the strength of a missing number.
                break

            # A re-seek is the remedy for a SHORTFALL and nothing else. `-vframes` makes a long
            # segment impossible, so one is evidence the output is not the segment that was asked
            # for -- re-seeking would not address that, and would spend an extraction pretending
            # it might. Report it instead.
            if attempts >= fork_frame_lock.MAX_EXTRACTION_ATTEMPTS or not verdict.short_by:
                break

            # The window came back short. Ask for a corrected one -- anchored on the end of the
            # source, one source frame of lead-in for accurate `-ss` to round forward into. `None`
            # means there is no honest earlier position, so the shortfall is reported instead.
            recovered = fork_frame_lock.recovery_seek(
                seek_time, exact_source_duration,
                get_video_duration(video_file), get_video_fps(video_file))
            if recovered is None:
                break
            print(f"   🔁 Segment short at {seek_time:.6f}s "
                  f"({verdict.produced}/{output_frame_count} frames); "
                  f"re-seeking to {recovered:.6f}s for frame completion")
            seek_time = recovered

        if verdict is not None and verdict.measured and not verdict.exact:
            return False, fork_frame_lock.describe_shortfall(verdict, attempts)

        return True, ""

    except RenderCancelled:
        # [FORK] Digital-Union (C3-R1A): never collapse a typed cancellation into an ordinary
        # (False, reason) clip-failure tuple -- the caller's except-order depends on this escaping
        # unchanged, before the generic handler below ever sees it.
        raise
    except Exception as e:
        print(f"   ⚠️  Error extracting clip: {e}")
        return False, fork_diagnostics.describe_exception(e)


def extract_prores_segment_random(video_file: str, duration: float, fps: float,
                                  temp_dir: str, segment_index: int,
                                  start_time: float = None, lifecycle=None) -> str:
    """
    Extract a segment from a ProRes proxy with frame-perfect precision.

    Important stability fix:
    - Do NOT use '-hwaccel auto' here. FFmpeg can select Vulkan/D3D hwaccel for
      ProRes, which is unnecessary for ProRes and can fail on some Windows GPU
      driver combinations.
    - Use a CPU-native ProRes path, exact frame count, and a safe retry command.

    [FORK] Digital-Union (FUN R10 Final Frame Fix): this path carried the **same** end-of-source
    frame loss as :func:`extract_clip_segment_ffmpeg_detailed` -- identical structure (``-ss`` at a
    start clamped with the *unquantised* duration, ``trim=duration=exact_duration``,
    ``-vframes frame_count``) and an acceptance test of exactly what the milestone forbids:
    ``returncode == 0`` plus a non-empty file. Each attempt's frame count is now verified, and a
    measured shortfall buys one corrected re-seek through the same
    :func:`beatsync_fork.frame_lock.recovery_seek`. The two pre-existing attempts (fast intra-frame
    seek, then the safe accurate-seek fallback) are **unchanged** in order, argv and timeout; the
    re-seek is appended as a third, and only a short segment ever reaches it. A shortfall that
    survives raises, exactly as every other failure on this path already does.
    """
    output_file = os.path.join(temp_dir, f"segment_{segment_index:05d}.mov")

    video_duration = get_video_duration(video_file)
    if video_duration <= 0:
        raise Exception(f"Invalid ProRes source duration: {video_file}")

    if video_duration >= duration:
        max_start = max(0.0, video_duration - duration)
        if start_time is None:
            start_time = random.uniform(0.0, max_start)
        else:
            start_time = max(0.0, min(float(start_time), max_start))
    else:
        # [FORK] Digital-Union (FUN R10 Final Frame Fix): this branch shortened `duration` to the
        # source's real length, which also shortened the SEGMENT -- `frame_count` below is derived
        # from it, so a source smaller than its slot produced a frame-exact clip for the window it
        # was given and a short one for the timeline it was planned into. The NVENC/CPU path refuses
        # the same case in `create_clip_parallel`; this is its ProRes counterpart, and the same
        # pure sentence describes it.
        planned_frames = max(1, seconds_to_frame_count(duration, fps))
        available_frames = max(1, seconds_to_frame_count(video_duration, fps))
        if available_frames < planned_frames:
            raise Exception(
                f"ProRes segment extraction error: "
                f"{fork_frame_lock.describe_truncated_plan(planned_frames, available_frames, video_duration)} "
                f"Source: {video_file}")
        start_time = 0.0
        duration = video_duration

    frame_count = max(1, seconds_to_frame_count(duration, fps))
    exact_duration = frame_count_to_seconds(frame_count, fps)

    filters = [f"trim=duration={exact_duration}", "setpts=PTS-STARTPTS"]
    filters.append(f"fps={fps}")
    filter_complex = ",".join(filters)

    def build_cmd(fast_seek: bool, seek: float) -> List[str]:
        cmd = [FFMPEG_PATH, '-nostdin', '-hide_banner']
        if fast_seek:
            # ProRes proxy is intra-frame, so input-side seeking remains accurate
            # while being much faster for long sources.
            cmd.extend(['-ss', f'{seek:.6f}', '-i', video_file])
        else:
            # Ultra-safe fallback. Slower on long sources, but avoids muxer/seek
            # edge cases if a specific FFmpeg build rejects the fast path.
            cmd.extend(['-i', video_file, '-ss', f'{seek:.6f}'])

        cmd.extend([
            '-map', '0:v:0',
            '-vf', filter_complex,
            '-vframes', str(frame_count),
            '-c:v', 'prores',
            '-profile:v', '0',
            '-vendor', 'apl0',
            '-pix_fmt', 'yuv422p10le',
            '-r', str(fps),
            '-fps_mode', 'cfr',
            '-fflags', '+genpts',
            '-an',
            '-sn',
            '-dn',
            '-threads', str(MAX_THREADS),
            '-y',
            output_file,
        ])
        return cmd

    last_error = ""
    seek_time = float(start_time)
    # [FORK] FUN R10: the two pre-existing attempts, plus at most one corrected re-seek appended
    # below. A list rather than a literal `for` target only so that third entry can exist; the
    # first two are byte-identical to the historical pair and are always tried first.
    plan = [
        ('fast intra-frame seek', True, 180),
        ('safe accurate seek retry', False, 360),
    ]
    reseek_spent = False
    index = 0
    while index < len(plan):
        attempt_name, fast_seek, timeout = plan[index]
        index += 1
        _safe_remove_file(output_file)
        result = _run_media_command(build_cmd(fast_seek, seek_time),
                                    timeout=timeout, lifecycle=lifecycle)
        if result.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0:
            # [FORK] FUN R10: exit code 0 and a non-empty file are not proof of a correct segment.
            verdict = fork_frame_lock.from_container_count(
                frame_count, _probe_output_frame_count(output_file))
            if verdict.exact or not verdict.measured:
                return output_file
            last_error = fork_frame_lock.describe_shortfall(verdict, index)
            print(f"   ⚠️  ProRes {attempt_name} produced "
                  f"{verdict.produced}/{frame_count} frames")
            if not reseek_spent:
                recovered = fork_frame_lock.recovery_seek(
                    seek_time, exact_duration, video_duration, get_video_fps(video_file))
                if recovered is not None:
                    reseek_spent = True
                    print(f"   🔁 Re-seeking {seek_time:.6f}s -> {recovered:.6f}s "
                          f"for frame completion")
                    seek_time = recovered
                    plan.append(('frame-completion re-seek', True, 180))
            continue
        last_error = _short_ffmpeg_error(result.stderr)
        print(f"   ⚠️  ProRes extraction failed on {attempt_name}: {last_error}")

    raise Exception(f"ProRes segment extraction error: {last_error}")

def concatenate_videos_ffmpeg(video_files: List[str], output_file: str,
                              audio_file: str = None, start_time: float = 0.0,
                              end_time: float = None, use_nvenc: bool = False,
                              gpu_encoder: str = 'h264_nvenc', fps: float = 30.0,
                              temp_dir: str = None, timeline_seconds: float = None,
                              lifecycle=None) -> str:
    """
    Concatenate video files using FFmpeg concat demuxer.

    ✅ FRAME-ACCURATE: Maintains precise timing through concatenation

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` defaults to ``None`` (unchanged behaviour) and is
    threaded to every ``_run_media_command`` call below. This function's own ``finally`` only
    cleans up temp files -- it has no ``except Exception``, so a ``RenderCancelled`` raised from any
    branch already propagates unchanged; nothing here may add a catch that would swallow it.

    [FORK] Digital-Union (FUN R10 A/V End Alignment): ``timeline_seconds`` is the frame-locked
    plan's own length, ``sum(segment_frames) / fps``, and it becomes an output-side ``-t`` on each
    of the three audio-muxing branches. ``None`` is the previous behaviour exactly, so every other
    caller is unchanged; it is placed before ``lifecycle`` because the C3-R1A seam contract requires
    ``lifecycle`` to stay the LAST parameter.

    **Why ``-shortest`` alone is not enough, measured rather than assumed.** ``-shortest`` ends the
    output when the first stream ends, but with stream copy it is imprecise by a small and
    inconsistent amount -- it cut 0, 1 and 2 PCM packets off the audio of three otherwise identical
    renders, and 77.3 ms off a 14-minute one. So the published video and its music did not end
    together even once the timeline stopped planning past the audio. An explicit ``-t`` at the plan
    cuts both streams at exactly the planned position: measured 20851 frames / 834.040000 s video
    against 834.040000 s audio, a 0.0 ms difference, where ``-shortest`` alone gave 833.962667 s.

    ``-shortest`` is **kept** rather than replaced. It still protects the case ``-t`` cannot: an
    audio file shorter than the plan. The two are belt and braces, and whichever is shorter wins.
    """
    if temp_dir is None:
        temp_dir = os.path.dirname(output_file)
    
    # Create concat file
    concat_file = os.path.join(temp_dir, f'concat_list_{uuid.uuid4().hex}.txt')
    with open(concat_file, 'w', encoding='utf-8') as f:
        for video_file in video_files:
            escaped_path = video_file.replace('\\', '/')
            f.write(f"file '{escaped_path}'\n")
    
    is_prores = output_file.lower().endswith('.mov')
    temp_video = None
    temp_audio = None
    
    try:
        if is_prores:
            # ProRes: concat with stream copy (lossless)
            print(f"   🔗 Concatenating {len(video_files)} segments (lossless stream copy)...")
            
            temp_video = os.path.join(temp_dir, f'video_only_{uuid.uuid4().hex}.mov')
            
            cmd = [
                FFMPEG_PATH,
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c', 'copy',
                '-y',
                temp_video
            ]
            
            result = _run_media_command(cmd, timeout=300, lifecycle=lifecycle)
            
            if result.returncode != 0:
                raise Exception(f"Concatenation failed: {result.stderr}")
            
            # Add audio if provided
            if audio_file:
                print(f"   🎵 Adding music track...")
                
                temp_audio = os.path.join(temp_dir, f'music_{uuid.uuid4().hex}.wav')
                
                audio_cmd = [FFMPEG_PATH, '-i', audio_file]
                
                if end_time and end_time > start_time:
                    audio_cmd.extend(['-ss', str(start_time), '-t', str(end_time - start_time)])
                elif start_time > 0:
                    audio_cmd.extend(['-ss', str(start_time)])
                
                audio_cmd.extend([
                    '-acodec', 'pcm_s24le',
                    '-ar', '48000',
                    '-ac', '2',
                    '-y',
                    temp_audio
                ])
                
                result = _run_media_command(audio_cmd, timeout=120, lifecycle=lifecycle)
                
                if result.returncode != 0:
                    raise Exception(f"Audio extraction failed: {result.stderr}")
                
                # Combine video + audio with AUDIO as master timeline
                cmd = [
                    FFMPEG_PATH,
                    '-i', temp_video,
                    '-i', temp_audio,
                    '-map', '0:v',
                    '-map', '1:a',
                    '-c:v', 'copy',
                    '-c:a', 'pcm_s24le',
                    '-ar', '48000',
                    '-shortest',  # Use shortest stream (audio)
                ]
                # [FORK] Digital-Union (FUN R10 A/V End Alignment): cut both streams at the plan.
                if timeline_seconds:
                    cmd.extend(['-t', str(timeline_seconds)])
                cmd.extend([
                    '-y',
                    output_file
                ])
                
                result = _run_media_command(cmd, timeout=300, lifecycle=lifecycle)
                
                if result.returncode != 0:
                    raise Exception(f"Audio merging failed: {result.stderr}")
                
                _safe_remove_file(temp_video)
                _safe_remove_file(temp_audio)
            else:
                shutil.move(temp_video, output_file)
        
        else:
            # H.264/H.265 standard path. Temp clips were already encoded with
            # matching FPS/resolution/codec settings, so the fastest safe path is
            # stream-copy concatenation plus audio mux. If a specific codec/container
            # combination rejects stream copy, fall back to the old re-encode path.
            fast_concat_enabled = _env_flag('BEATSYNC_FAST_CONCAT_COPY', True)

            def add_audio_input(cmd: List[str]) -> None:
                if not audio_file:
                    return
                if end_time and end_time > start_time:
                    cmd.extend(['-ss', str(start_time), '-t', str(end_time - start_time), '-i', audio_file])
                elif start_time > 0:
                    cmd.extend(['-ss', str(start_time), '-i', audio_file])
                else:
                    cmd.extend(['-i', audio_file])

            if fast_concat_enabled:
                print(f"   🔗 Fast final assembly: concat stream-copy video + mux audio...")
                copy_started = time.perf_counter()
                cmd = [
                    FFMPEG_PATH,
                    '-nostdin',
                    '-hide_banner',
                    '-f', 'concat',
                    '-safe', '0',
                    '-i', concat_file,
                ]
                add_audio_input(cmd)
                if audio_file:
                    cmd.extend(['-map', '0:v:0', '-map', '1:a:0'])
                else:
                    cmd.extend(['-map', '0:v:0'])
                cmd.extend(['-c:v', 'copy'])
                if audio_file:
                    cmd.extend(['-c:a', 'pcm_s24le', '-ar', '48000', '-shortest'])
                    # [FORK] Digital-Union (FUN R10 A/V End Alignment): cut both streams at the plan.
                    if timeline_seconds:
                        cmd.extend(['-t', str(timeline_seconds)])
                cmd.extend(['-fflags', '+genpts', '-movflags', '+faststart', '-y', output_file])

                result = _run_media_command(cmd, timeout=300, lifecycle=lifecycle)
                if result.returncode == 0 and os.path.exists(output_file) and os.path.getsize(output_file) > 0:
                    print(f"   ✓ Fast concat-copy complete in {_fmt_seconds(time.perf_counter() - copy_started)}")
                    return output_file
                print(
                    f"   ⚠️  Fast concat-copy failed in {_fmt_seconds(time.perf_counter() - copy_started)}; "
                    f"falling back to re-encode. {_short_ffmpeg_error(result.stderr, 900)}"
                )

            # Fallback/original behavior: H.264/H.265 full re-encode.
            print(f"   🔗 Concatenating and encoding {len(video_files)} segments...")
            encode_started = time.perf_counter()
            cmd = [FFMPEG_PATH]
            
            if use_nvenc:
                cmd.extend(['-hwaccel', 'cuda'])
            else:
                cmd.extend(['-hwaccel', 'auto'])
            
            cmd.extend([
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file
            ])
            
            add_audio_input(cmd)
            if audio_file:
                cmd.extend(['-map', '0:v', '-map', '1:a'])
            
            if use_nvenc:
                cmd.extend(get_nvenc_quality_args(gpu_encoder, include_pix_fmt=True))
            else:
                cmd.extend(get_cpu_h264_quality_args(include_pix_fmt=True))
            
            if audio_file:
                cmd.extend([
                    '-c:a', 'pcm_s24le',
                    '-ar', '48000',
                    '-shortest',
                ])
                # [FORK] Digital-Union (FUN R10 A/V End Alignment): cut both streams at the plan.
                if timeline_seconds:
                    cmd.extend(['-t', str(timeline_seconds)])

            cmd.extend([
                '-fps_mode', 'cfr',
                '-r', str(fps),
                '-y',
                output_file
            ])
            
            result = _run_media_command(cmd, timeout=600, lifecycle=lifecycle)
            
            if result.returncode != 0:
                raise Exception(f"Encoding failed: {result.stderr}")
            print(f"   ✓ Full final re-encode complete in {_fmt_seconds(time.perf_counter() - encode_started)}")
        
        return output_file
    finally:
        _safe_remove_file(concat_file)
        _safe_remove_file(temp_audio)
        _safe_remove_file(temp_video)


def detect_video_scene_changes(video_path: str, threshold: float = 0.28,
                               use_gpu: bool = False,
                               analysis_fps: float = 8.0,
                               analysis_width: int = 384,
                               lifecycle=None) -> List[float]:
    """
    Detect likely montage/scene changes using FFmpeg's scene score.

    [FORK] Digital-Union (FUN Stage-5 Cancel): ``lifecycle`` is appended last with a default of
    ``None``, which is byte-for-byte the pre-existing behaviour — `_run_media_command` falls
    straight through to its original blocking `subprocess.run`. Supplied, it makes the one
    genuinely long FFmpeg pass in Stage 5 cancellable: this filter graph decodes the **whole**
    source video, so on a large library it is where most of an uncancellable hour was actually
    spent. The `except RenderCancelled: raise` below sits ahead of both broad handlers on purpose —
    this function's contract is to swallow failures and return `[]`, and a cancellation collapsing
    into "no scene changes found" would let the analysis carry on building windows for a render
    nobody asked for.

    GPU note:
    FFmpeg's `scene` comparison itself is a CPU video filter, but when GPU mode
    is enabled this uses CUDA decode + CUDA resize, then downloads a small
    analysis frame for the CPU scene score. That makes the expensive decode/scale
    stage GPU-assisted while preserving the same semantic scene-cut signal.
    If CUDA decode is unsupported for a source codec, it automatically falls
    back to the CPU analysis path.
    """
    try:
        print(f"   🎬 Analyzing scene changes: {os.path.basename(video_path)}")
        print(f"   Threshold: {threshold}")
        print(f"   Analysis: {analysis_fps:g} fps @ {analysis_width}px wide")

        filter_cpu = (
            f"fps={analysis_fps},"
            f"scale={analysis_width}:-2:flags=fast_bilinear,"
            f"select='gt(scene,{threshold})',showinfo"
        )
        filter_gpu = (
            f"scale_cuda={analysis_width}:-2,"
            f"hwdownload,format=nv12,"
            f"fps={analysis_fps},"
            f"select='gt(scene,{threshold})',showinfo"
        )

        commands = []
        if use_gpu:
            commands.append((
                'GPU-assisted CUDA decode/scale',
                [
                    FFMPEG_PATH,
                    '-nostdin',
                    '-hide_banner',
                    '-hwaccel', 'cuda',
                    '-hwaccel_output_format', 'cuda',
                    '-i', video_path,
                    '-vf', filter_gpu,
                    '-an', '-sn', '-dn',
                    '-f', 'null',
                    '-'
                ],
            ))

        commands.append((
            'CPU fast scene score',
            [
                FFMPEG_PATH,
                '-nostdin',
                '-hide_banner',
                '-threads', str(MAX_THREADS),
                '-i', video_path,
                '-vf', filter_cpu,
                '-an', '-sn', '-dn',
                '-f', 'null',
                '-'
            ],
        ))

        last_error = ''
        for label, cmd in commands:
            if label.startswith('GPU'):
                print("   ⚡ GPU scene analysis: CUDA decode/scale + CPU scene score")
            else:
                if use_gpu:
                    print("   ↪ GPU scene analysis unavailable/failed, using CPU fallback")
                else:
                    print("   💻 CPU scene analysis")

            command_started = time.perf_counter()
            result = _run_media_command(cmd, timeout=300, lifecycle=lifecycle)
            command_elapsed = time.perf_counter() - command_started
            if result.returncode == 0:
                cleaned = _extract_scene_times(result.stderr)
                print(
                    f"   ✓ Found {len(cleaned)} visual scene changes ({label}) "
                    f"in {command_elapsed:.1f}s"
                )
                return cleaned

            print(f"   ⚠️  Scene command failed after {command_elapsed:.1f}s ({label})")
            last_error = _short_ffmpeg_error(result.stderr, max_chars=1200)
            if label.startswith('GPU'):
                print(f"   ⚠️  GPU scene analysis failed, fallback enabled: {last_error}")

        print(f"   ⚠️  Warning: Scene detection failed: {last_error}")
        return []

    # [FORK] Digital-Union (FUN Stage-5 Cancel): FIRST, ahead of both broad handlers below.
    # `RenderCancelled` is an ordinary `Exception`, so the generic handler would otherwise catch it
    # and return `[]` -- reporting the operator's Stop as "could not analyze scene changes" and
    # letting Stage 5 continue into window building for the next source.
    except RenderCancelled:
        raise
    except subprocess.TimeoutExpired:
        print(f"   ⚠️  Warning: Scene detection timeout")
        return []
    except Exception as e:
        print(f"   ⚠️  Warning: Could not analyze scene changes: {e}")
        return []

def detect_video_keyframes(video_path: str, min_interval: float = 0.20) -> List[float]:
    """
    Detect codec keyframes/I-frames with ffprobe.

    Keyframes are not always creative scene cuts, but they are useful as a
    fallback signal for footage that was encoded with keyframes at montage cuts.
    """
    try:
        print(f"   🔑 Reading codec keyframes: {os.path.basename(video_path)}")
        cmd = [
            FFPROBE_PATH,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-skip_frame', 'nokey',
            '-show_entries', 'frame=best_effort_timestamp_time,pkt_pts_time,pts_time',
            '-of', 'json',
            video_path,
        ]
        result = _run_media_command(cmd, timeout=120)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        data = json.loads(result.stdout)
        keyframes = []
        for frame in data.get('frames', []):
            ts = frame.get('best_effort_timestamp_time') or frame.get('pkt_pts_time') or frame.get('pts_time')
            if ts is None:
                continue
            try:
                t = float(ts)
            except (TypeError, ValueError):
                continue
            if t >= 0.0:
                keyframes.append(round(t, 3))

        keyframes = sorted(set(keyframes))
        cleaned = []
        for t in keyframes:
            if not cleaned or t - cleaned[-1] >= min_interval:
                cleaned.append(t)

        print(f"   ✓ Found {len(cleaned)} codec keyframes")
        return cleaned
    except subprocess.TimeoutExpired:
        print("   ⚠️  Warning: Keyframe detection timeout")
        return []
    except Exception as e:
        print(f"   ⚠️  Warning: Could not read keyframes: {e}")
        return []
