import os
import sys
import contextlib
import asyncio

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)


def _install_windows_asyncio_connection_reset_filter() -> None:
    """Hide benign Windows asyncio pipe resets after browser/subprocess shutdown.

    On Windows, asyncio's Proactor transport can log a scary traceback when a
    local socket or subprocess pipe is closed by the other side after the real
    work is already complete. The app result is not affected, so suppress only
    that exact WinError 10054 callback and let all other async errors through.
    """
    if os.name != "nt" or getattr(asyncio, "_beatsync_win10054_filter", False):
        return
    asyncio._beatsync_win10054_filter = True

    def is_benign_reset(exc: BaseException | None) -> bool:
        if not isinstance(exc, ConnectionResetError):
            return False
        winerror = getattr(exc, "winerror", None)
        errno_value = getattr(exc, "errno", None)
        return winerror == 10054 or errno_value == 10054 or "WinError 10054" in str(exc)

    # Directly patch the noisy Proactor pipe cleanup callback when available.
    try:
        from asyncio import proactor_events

        transport_cls = getattr(proactor_events, "_ProactorBasePipeTransport", None)
        original_call_lost = getattr(transport_cls, "_call_connection_lost", None)
        if transport_cls is not None and original_call_lost is not None:
            def quiet_call_connection_lost(self, exc):  # type: ignore[no-untyped-def]
                try:
                    return original_call_lost(self, exc)
                except ConnectionResetError as reset_exc:
                    if is_benign_reset(reset_exc):
                        return None
                    raise

            transport_cls._call_connection_lost = quiet_call_connection_lost
    except Exception:
        pass

    # Fallback for the same exception if it still reaches the loop logger.
    original_exception_handler = asyncio.BaseEventLoop.call_exception_handler

    def quiet_exception_handler(self, context):  # type: ignore[no-untyped-def]
        exc = context.get("exception") if isinstance(context, dict) else None
        handle = str(context.get("handle", "")) if isinstance(context, dict) else ""
        if is_benign_reset(exc) and "_ProactorBasePipeTransport._call_connection_lost" in handle:
            return None
        return original_exception_handler(self, context)

    asyncio.BaseEventLoop.call_exception_handler = quiet_exception_handler


_install_windows_asyncio_connection_reset_filter()

from logger import (
    setup_environment,
    USING_PORTABLE_PYTHON, USING_PORTABLE_CUDA, USING_CUPY_CTK, FFMPEG_FOUND
)

# Initialize environment
setup_environment()
# NOW import other modules (after CUDA environment is set)
import gradio as gr
import tempfile
import shutil
import datetime
import multiprocessing
import queue
import re
import subprocess
import threading
import time
import socket
from typing import Callable, Iterator, TypeAlias, Tuple, Dict, List

# Import FFmpeg processing module
from ffmpeg_processing import get_video_fps, FFMPEG_PATH

# Shared runtime settings
from gpu_cpu_utils import (
    CPU_COUNT,
    MAX_THREADS,
    PARALLEL_WORKERS,
    GPU_INFO,
    GPU_AVAILABLE,
    NVENC_AVAILABLE,
    set_gpu_mode,
)
from paths import (
    GRADIO_TEMP_DIR,
    get_input_dir,
    get_audio_input_dir,
    get_video_input_dir,
    get_output_dir,
)

gpu_data = GPU_INFO
gpu_info = f"{gpu_data['name']} ({gpu_data['cuda_version']})" if gpu_data['available'] else "CPU Mode"

from video_processor import create_music_video

from auto_mode import CONFIG as AUTO_MODE_CONFIG, analyze_beats_auto

# [FORK] Digital-Union: structured pipeline progress. Event model and rendering live in
# src/beatsync_fork/ (stdlib-only, Gradio-free); this module only moves events to widgets.
from beatsync_fork.progress import EventKind, ProgressEvent
from beatsync_fork.progress_view import ProgressView

# [FORK] Digital-Union: creative variation seed (Phase A). Selection rule and seed normalisation
# live in src/beatsync_fork/ (stdlib-only, Gradio-free); this module only wires them to widgets.
from beatsync_fork import variation as fork_variation

# [FORK] Digital-Union (P V1): media library preparation. All state, classification vocabulary and
# report rendering live in src/beatsync_fork/library_prep.py (stdlib-only, Gradio-free); this module
# only wires it to widgets and supplies the runtime calls it must not make itself.
from beatsync_fork import library_prep as fork_prep
from beatsync_fork.library_prep import initial_state as initial_prep_state
from beatsync_fork.input_manager import InputScanError, scan_folder as scan_library_folder

# [FORK] Digital-Union: source-input confirmation gate. All decision logic lives in
# src/beatsync_fork/ (stdlib-only, Gradio-free); this module only wires it to widgets.
from beatsync_fork.input_confirmation import SourceMode
from beatsync_fork.input_session import (
    confirm_action,
    initial_state as initial_source_state,
    live_declaration,
    resolve_for_render,
    scan_folder_action,
    set_browser_files,
    set_folder_path,
    set_mode,
    set_recursive,
)

# Import UI content
from ui_content import *

# Set environment variable for Gradio
os.environ['GRADIO_TEMP_DIR'] = GRADIO_TEMP_DIR

VideoFilesInput : TypeAlias = List[str]
StatusResult : TypeAlias = Tuple[str, str, Dict]

STATUS_BOX_CSS = """
#status-output-box {
    min-height: 238px !important;
}

#status-output-box textarea {
    height: 186px !important;
    min-height: 186px !important;
    max-height: 186px !important;
    overflow-y: auto !important;
    resize: none !important;
}
"""


def _stage_status(stage_number: int) -> str:
    return f"Stage {stage_number} is processing. Please wait."


class QuietConsole:
    """Discard legacy verbose prints while the Gradio worker runs."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


class StageConsoleLogger:
    """Small CMD logger: stage start, up to 5 useful lines, stage end."""

    def __init__(self, stream, max_lines_per_stage: int = 5):
        self.stream = stream
        self.max_lines_per_stage = max(1, int(max_lines_per_stage))
        self.stage_number: int | None = None
        self.stage_started = 0.0
        self.stage_line_count = 0
        self.total_started = time.perf_counter()

    def start_stage(self, stage_number: int) -> None:
        if self.stage_number == stage_number:
            return
        self.end_stage()
        self.stage_number = stage_number
        self.stage_started = time.perf_counter()
        self.stage_line_count = 0
        self._write(f"Stage {stage_number} processing started:\n")

    def stage_line(self, stage_number: int, message: str) -> None:
        if self.stage_number != stage_number:
            self.start_stage(stage_number)
        self.line(message)

    def line(self, message: str) -> None:
        if self.stage_number is None:
            return
        if self.stage_line_count >= self.max_lines_per_stage:
            return
        message = self._clean(message)
        if message:
            self._write(f"  {message}\n")
            self.stage_line_count += 1

    def end_stage(self, elapsed_seconds: float | None = None) -> None:
        # [FORK] Digital-Union (L0): `elapsed_seconds` is for a stage this logger never saw start -
        # Stage 0 is reported by a single END event carrying its own measurement, so the logger's
        # own clock would print "0 seconds" for work that already happened. Normal stages pass
        # nothing and keep measuring themselves exactly as before.
        if self.stage_number is None:
            return
        if elapsed_seconds is None:
            elapsed = int(round(time.perf_counter() - self.stage_started))
        else:
            elapsed = int(round(max(0.0, float(elapsed_seconds))))
        self._write(f"Stage {self.stage_number} ended in {elapsed} seconds.\n\n")
        self.stage_number = None
        self.stage_started = 0.0
        self.stage_line_count = 0

    def finish(self) -> None:
        self.end_stage()
        elapsed = int(round(time.perf_counter() - self.total_started))
        self._write(f"Total time processing: {elapsed} seconds\n")

    def _write(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def _clean(self, text: str) -> str:
        text = str(text).encode("ascii", "ignore").decode("ascii")
        return re.sub(r"\s+", " ", text).strip()

    # [FORK] Digital-Union: drive the CMD log from structured events too, so the console and the
    # GUI agree and neither depends on parsing prose.
    def apply_event(self, event: ProgressEvent) -> None:
        if event.kind is EventKind.START:
            self.start_stage(event.stage)
            if event.message:
                self.line(event.message)
        elif event.kind is EventKind.END:
            # A stage whose END is the first event seen for it was never timed by this logger (the
            # L0 Stage-0 verification report is the only such case today), so its own measurement is
            # used instead of a stage that existed for microseconds.
            unseen = self.stage_number != event.stage
            if event.message:
                self.stage_line(event.stage, event.message)
            if self.stage_number == event.stage:
                self.end_stage(event.elapsed_seconds if unseen else None)
        elif event.kind in (EventKind.METRIC, EventKind.STATE):
            if event.message:
                self.stage_line(event.stage, event.message)
        elif event.kind in (EventKind.WARNING, EventKind.ERROR):
            self.stage_line(event.stage, f"! {event.message}")


def _fmt_stage_seconds(seconds: float | int | None) -> str:
    try:
        return f"{float(seconds):.1f}s"
    except Exception:
        return "0.0s"


def _short_model_name(model_id: str | None) -> str:
    if not model_id:
        return ""
    return os.path.basename(str(model_id).rstrip("/\\")) or str(model_id)


def _stage5_summary(console_logger: StageConsoleLogger | None, video_analysis: Dict | None) -> None:
    if console_logger is None or not isinstance(video_analysis, dict):
        return

    # [FORK] Digital-Union (R1): Stage 5 returns two different kinds of number and this summary used
    # to blend them. The `qwen_*` fields without a `_this_run` suffix aggregate the whole returned
    # library, cache hits included, so a fully warm run rendered the cached library's historical
    # 8704/8704 tags and 3031.9s of inference as if this invocation had produced them - while the
    # five-line budget pushed out the one line that was actually true of the run. Current-run truth
    # is now sourced exclusively from the explicit `*_this_run` fields, and the lines are ordered so
    # the authoritative `Analysis time … cache H/N` can never be the one that gets dropped.
    source_count = int(video_analysis.get("source_count") or len(video_analysis.get("videos") or []))
    cache_hits = int(video_analysis.get("cache_hits") or 0)
    analyzed = int(video_analysis.get("sources_analyzed_this_run") or 0)
    ai_enabled = bool(video_analysis.get("ai_enabled"))

    qwen_jobs = int(video_analysis.get("qwen_jobs_this_run") or 0)
    qwen_tags_run = int(video_analysis.get("qwen_tag_count_this_run") or 0)
    # [FORK] Digital-Union (R2): the denominator is what was SUBMITTED, not what the worker managed
    # to decode. Using the decoded frame count would render a job that requested 10 candidates and
    # decoded only 8 as a flawless "8/8", hiding the two that never arrived - and on a worker-level
    # failure there is no decoded count at all. Decoded frames stay available to structured
    # consumers through `qwen_frame_count_this_run`.
    qwen_requested_run = int(video_analysis.get("qwen_requested_count_this_run") or 0)
    qwen_incomplete = int(video_analysis.get("qwen_incomplete_jobs_this_run") or 0)
    qwen_seconds_run = video_analysis.get("qwen_seconds_this_run")

    # 1. sources / cache / analysed-this-run
    console_logger.line(
        f"Sources: {source_count}, cache {cache_hits}/{source_count}, analyzed this run {analyzed}"
    )

    # 2. current-run Qwen status - never the library aggregate
    if not ai_enabled:
        console_logger.line("Qwen: disabled")
    elif qwen_jobs == 0:
        console_logger.line("Qwen: enabled, no inference this run")
    else:
        bits = [f"Qwen this run: {qwen_jobs} job(s)", f"{qwen_tags_run}/{qwen_requested_run} tags"]
        # A failure path may not be able to prove elapsed time; omit it rather than understate it.
        if qwen_seconds_run:
            bits.append(f"in {_fmt_stage_seconds(qwen_seconds_run)}")
        if qwen_incomplete:
            bits.append(f"{qwen_incomplete} incomplete, not cached")
        console_logger.line(", ".join(bits))

    # 3. [FORK] Digital-Union (L0): what the cache scan itself cost, split into strong identity
    # (the D2 content fingerprint) and record load/validation. Omitted entirely when the producer
    # did not report it, so an older/other caller renders exactly as before.
    identity_seconds = video_analysis.get("cache_identity_seconds")
    lookup_seconds = video_analysis.get("cache_lookup_seconds")
    if identity_seconds is not None or lookup_seconds is not None:
        console_logger.line(
            f"Cache check: identity {_fmt_stage_seconds(identity_seconds)}, "
            f"records {_fmt_stage_seconds(lookup_seconds)}"
        )

    # 4. the authoritative measurement - must survive the five-line budget
    console_logger.line(
        f"Analysis time: {_fmt_stage_seconds(video_analysis.get('analysis_seconds'))}, cache {cache_hits}/{source_count}"
    )

    # 5. library summary
    summary = video_analysis.get("summary")
    if summary:
        console_logger.line(f"Visual library: {summary}")

    # 6. optional historical metadata, explicitly labelled as cached and only if space remains.
    library_tags = int(video_analysis.get("qwen_tag_count") or 0)
    if ai_enabled and library_tags and qwen_jobs == 0:
        console_logger.line(f"Cached library: {library_tags} previously tagged candidates")


def _scale_diagnostics_block(verification_seconds: float | None,
                             beat_info: Dict | None) -> str:
    """[FORK] Digital-Union (L0.1): the L0 measurements, repeated in the final success panel.

    Every number here was already measured by L0 and is only being re-read: nothing is recomputed,
    no folder is rescanned and no cache is consulted. The reason this exists is presentation, not
    instrumentation - `_stage5_summary`/`_stage6_summary` write to `StageConsoleLogger` from the
    worker thread while the generator thread drives the same logger from the event queue, so whether
    those console lines survive is timing-dependent. A real 41-source run reproduced exactly that:
    the candidate count and the planner figures never reached the console. The final success message
    is deterministic and stays on screen, so the baseline can be copied from it reliably.

    Presence is tested with ``is not None``, never truthiness: a measured ``0.0s`` (the real run's
    source verification) is a result and must stay visible, while an absent value is omitted rather
    than rendered as a fabricated zero. A caller that supplies nothing gets an empty string and the
    success message is unchanged.
    """
    info = beat_info if isinstance(beat_info, dict) else {}
    analysis = info.get("video_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    render_info = info.get("render_info")
    render_info = render_info if isinstance(render_info, dict) else {}

    lines: list[str] = []
    if verification_seconds is not None:
        lines.append(f"Source verification: {_fmt_stage_seconds(verification_seconds)}")

    stage5: list[str] = []
    if analysis.get("cache_identity_seconds") is not None:
        stage5.append(f"identity {_fmt_stage_seconds(analysis['cache_identity_seconds'])}")
    if analysis.get("cache_lookup_seconds") is not None:
        stage5.append(f"records {_fmt_stage_seconds(analysis['cache_lookup_seconds'])}")
    if analysis.get("cache_hits") is not None and analysis.get("source_count") is not None:
        stage5.append(f"cache {int(analysis['cache_hits'])}/{int(analysis['source_count'])}")
    if analysis.get("candidate_count") is not None:
        stage5.append(f"{int(analysis['candidate_count'])} candidates")
    if stage5:
        lines.append("Stage 5: " + " · ".join(stage5))

    planner: list[str] = []
    segments = render_info.get("planner_segment_count")
    candidates = render_info.get("planner_candidate_count")
    if segments is not None and candidates is not None:
        planner.append(f"{int(segments)} segments × {int(candidates)} candidates")
    elif segments is not None:
        planner.append(f"{int(segments)} segments")
    elif candidates is not None:
        planner.append(f"{int(candidates)} candidates")
    if render_info.get("planner_seconds") is not None:
        planner.append(_fmt_stage_seconds(render_info["planner_seconds"]))
    if planner:
        lines.append("Planner: " + " · ".join(planner))

    return "Scale diagnostics:\n" + "\n".join(lines) if lines else ""


def _stage6_summary(console_logger: StageConsoleLogger | None, beat_info: Dict | None) -> None:
    if console_logger is None or not isinstance(beat_info, dict):
        return

    render_info = beat_info.get("render_info") or {}
    if not render_info:
        return

    cuts = int(render_info.get("render_cuts") or 0)
    frames = int(render_info.get("timeline_frames") or 0)
    fps = render_info.get("output_fps")
    if cuts or frames:
        fps_text = f" @ {float(fps):.1f} FPS" if fps is not None else ""
        console_logger.line(f"Render timeline: {cuts} cuts, {frames} frames{fps_text}")

    clip_workers = render_info.get("clip_workers")
    requested_workers = render_info.get("requested_workers")
    worker_text = ""
    if clip_workers:
        worker_text = f", workers {clip_workers}"
        if requested_workers and requested_workers != clip_workers:
            worker_text += f"/{requested_workers}"
    encoder = render_info.get("encoder")
    if encoder or worker_text:
        console_logger.line(f"Encoder: {encoder or 'unknown'}{worker_text}")

    if render_info.get("audio_duration") is not None:
        console_logger.line(f"Audio duration: {float(render_info['audio_duration']):.2f} seconds")

    plan_summary = render_info.get("plan_summary") or {}
    if plan_summary:
        # [FORK] Digital-Union (Phase A): the seed rides on the existing planner line rather than
        # spending one of the five console slots on its own.
        # [FORK] Digital-Union (L0): the planner's scale facts ride on the line that already exists
        # rather than spending another of the five console slots. Candidates and elapsed time are
        # appended only when Stage 6 reported them.
        planner_bits = [
            f"{int(plan_summary.get('clip_count') or 0)} clips",
            f"{int(plan_summary.get('source_count') or 0)} sources",
            f"AI moments {int(plan_summary.get('ai_tagged') or 0)}",
        ]
        if render_info.get("planner_candidate_count") is not None:
            planner_bits.append(f"{int(render_info['planner_candidate_count'])} candidates")
        if render_info.get("planner_seconds") is not None:
            planner_bits.append(_fmt_stage_seconds(render_info["planner_seconds"]))
        planner_bits.append(fork_variation.describe(plan_summary.get("seed")))
        console_logger.line("Planner: " + ", ".join(planner_bits))
    elif render_info.get("planner_seconds") is not None:
        # The planner ran and produced no usable plan (renderer falls back to random sampling).
        # It still cost real time at library scale, so report it instead of losing the measurement.
        console_logger.line(
            f"Planner: fallback, {int(render_info.get('planner_segment_count') or 0)} segments over "
            f"{int(render_info.get('planner_candidate_count') or 0)} candidates, "
            f"{_fmt_stage_seconds(render_info['planner_seconds'])}"
        )

    final_bits = []
    if render_info.get("target_resolution"):
        final_bits.append(f"resolution {render_info['target_resolution']}")
    if render_info.get("final_assembly_seconds") is not None:
        final_bits.append(f"assembly {_fmt_stage_seconds(render_info['final_assembly_seconds'])}")
    if final_bits:
        console_logger.line("Final: " + ", ".join(final_bits))

def find_launch_port(default_port: int = 7860, search_limit: int = 20) -> int:
    """Prefer the default Gradio port, then step forward if it is busy."""
    env_port = os.environ.get("GRADIO_SERVER_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            print(f"⚠️ Invalid GRADIO_SERVER_PORT={env_port!r}; using auto port search.")

    for port in range(default_port, default_port + search_limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                if port != default_port:
                    print(f"⚠️ Port {default_port} is busy. Starting BeatSync on port {port}.")
                return port

    raise OSError(f"Cannot find empty port in range: {default_port}-{default_port + search_limit - 1}")


def _as_existing_source_path(file_path: str | None) -> str | None:
    """Use the selected source file directly instead of copying it locally."""
    if not file_path:
        return None
    try:
        path = os.path.abspath(os.fspath(file_path))
    except TypeError:
        return None
    return path if os.path.isfile(path) else None


def _as_existing_source_paths(file_paths: VideoFilesInput) -> list[str]:
    if not file_paths:
        return []
    return [path for path in (_as_existing_source_path(p) for p in file_paths) if path]


def _process_video_impl(audio_file: str, video_files: VideoFilesInput,
                       output_filename: str, processing_mode: str,
                       custom_fps: float, variation_seed: int, session_state: dict,
                       progress_callback: Callable[[str], None] | None = None,
                       console_logger: StageConsoleLogger | None = None,
                       event_callback: Callable[[ProgressEvent], None] | None = None,
                       verification_seconds: float | None = None) -> StatusResult:
    total_started = time.perf_counter()
    try:
        parallel_workers = PARALLEL_WORKERS

        # Initialize session state if needed
        if 'original_audio_path' not in session_state:
            session_state['original_audio_path'] = None
            session_state['original_video_paths'] = []
        if 'session_dir' not in session_state or not os.path.isdir(session_state['session_dir']):
            session_state['session_dir'] = tempfile.mkdtemp(prefix='beatsync_', dir=GRADIO_TEMP_DIR)
        session_dir = session_state['session_dir']

        # Handle audio by referencing the selected file path directly.
        if audio_file:
            if audio_file != session_state.get('original_audio_path'):
                local_audio_path = _as_existing_source_path(audio_file)
                if local_audio_path:
                    session_state['local_audio_path'] = local_audio_path
                    session_state['original_audio_path'] = audio_file
                else:
                    return None, '❌ Error: Could not access audio file', session_state
            else:
                local_audio_path = session_state.get('local_audio_path')
        else:
            return None, '❌ Error: No audio file selected', session_state

        # Handle videos by referencing selected file paths directly.
        if video_files:
            if video_files != session_state.get('original_video_paths'):
                local_video_paths = _as_existing_source_paths(video_files)
                if local_video_paths:
                    session_state['local_video_paths'] = local_video_paths
                    session_state['original_video_paths'] = video_files
                else:
                    return None, '❌ Error: Could not access video files', session_state
            else:
                local_video_paths = session_state.get('local_video_paths')
        else:
            return None, '❌ Error: No video files selected', session_state

        # Verify files exist
        if not local_audio_path or not os.path.exists(local_audio_path):
             return None, f"❌ Error: Audio file is missing or inaccessible.", session_state
        if not local_video_paths or not all(p and os.path.exists(p) for p in local_video_paths):
             return None, f"❌ Error: Video files are missing or inaccessible.", session_state
        
        # Set GPU mode
        use_gpu = GPU_AVAILABLE
        set_gpu_mode(use_gpu)
        
        # Determine processing mode
        is_prores = processing_mode == 'prores_proxy'
        use_nvenc = (processing_mode in ['h264_nvenc', 'hevc_nvenc']) and NVENC_AVAILABLE
        gpu_encoder = processing_mode if use_nvenc else 'none'
        
        python_str = "Portable" if USING_PORTABLE_PYTHON else "System"
        cuda_str = "CuPy CTK" if USING_CUPY_CTK else ("Portable" if USING_PORTABLE_CUDA else "System/None")

        # Determine FPS
        if custom_fps is not None and custom_fps > 0:
            output_fps = custom_fps
        else:
            output_fps = get_video_fps(local_video_paths[0])
            
        # Prepare output paths
        output_folder = get_output_dir()
        os.makedirs(output_folder, exist_ok=True)
        name, _ = os.path.splitext(output_filename)
        ext = '.mov' if is_prores else '.mp4'
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        # [FORK] Digital-Union (Phase A): a variation render carries its seed in the filename so a
        # good result can be found again. Seed 0 keeps today's name exactly.
        seed = fork_variation.normalize_seed(variation_seed)
        filename = f"{name}_{timestamp}{fork_variation.filename_suffix(seed)}{ext}"
        output_path = os.path.join(output_folder, filename)
        temp_output = os.path.join(session_dir, filename)

        selected_beats, beat_info = analyze_beats_auto(
            local_audio_path,
            use_gpu=use_gpu,
            video_files=local_video_paths,
            progress_callback=progress_callback,
            console_callback=lambda stage, message: console_logger.stage_line(stage, message) if console_logger else None,
            event_callback=event_callback,
            creative={"seed": seed},
        )
        beat_times = beat_info.get('times', selected_beats)
        _stage5_summary(console_logger, beat_info.get("video_analysis"))

        if progress_callback:
            progress_callback(_stage_status(6))

        # Create video
        result_path = create_music_video(
            local_audio_path, local_video_paths, selected_beats,
            output_file=temp_output, max_workers=parallel_workers,
            beat_info=beat_info, lossless_mode=is_prores,
            use_gpu=use_gpu, gpu_encoder=gpu_encoder, fps=output_fps,
            event_callback=event_callback
        )

        # Move to output folder
        shutil.move(result_path, output_path)

        # Create preview for ProRes if needed
        preview_path = output_path
        if is_prores:
            preview_filename = f"{name}_{timestamp}_preview.mp4"
            preview_path = os.path.join(session_dir, preview_filename)
            preview_cmd = [FFMPEG_PATH]
            if NVENC_AVAILABLE:
                preview_cmd.extend(['-hwaccel', 'cuda', '-c:v', 'h264_nvenc', '-preset', 'p5', '-cq', '23'])
            else:
                preview_cmd.extend(['-hwaccel', 'auto', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23'])
            preview_cmd.extend(['-i', output_path, '-pix_fmt', 'yuv420p', '-y', preview_path])
            subprocess.run(preview_cmd, capture_output=True, text=True, timeout=180)
        _stage6_summary(console_logger, beat_info)

        # Generate status message based on mode
        gpu_info = f"⚡ GPU: {GPU_INFO}" if use_gpu else "💻 CPU"
        fps_info = f"{output_fps:.2f} FPS (custom)" if custom_fps else f"{output_fps:.2f} FPS (auto-detected)"
        audio_info = "PCM 24-bit (48kHz)"
        
        if is_prores:
            codec_info = "ProRes 422 Proxy (.mov) - Lossless"
            encoder_info = "🎯 Lossless Concatenation"
        elif use_nvenc:
            codec_info = f"{gpu_encoder.upper()} (.mp4)"
            encoder_info = f"⚡ {gpu_encoder.upper()}"
        else:
            codec_info = "H.264 (.mp4)"
            encoder_info = "💻 libx264"

        total_cuts = len(selected_beats) - 1
        sections_info = beat_info.get('selection_info', [])
        total_processing_seconds = time.perf_counter() - total_started
        processing_label = gpu_encoder.upper() if use_nvenc else ("PRORES_PROXY" if is_prores else "H264_CPU")
        
        status_msg = get_success_message_auto(
            total_cuts, len(beat_times),
            beat_info.get('tempo', 120), sections_info,
            python_str, cuda_str, MAX_THREADS, CPU_COUNT,
            parallel_workers, gpu_info, encoder_info,
            codec_info, fps_info, filename, audio_info,
            audio_duration=beat_info.get('audio_duration'),
            output_fps=output_fps,
            total_processing_seconds=total_processing_seconds,
            processing_label=processing_label,
            variation_text=fork_variation.describe(seed) if seed else None,
        )
        # [FORK] Digital-Union (L0.1): append the L0 baseline. The success statistics above are
        # untouched; this only adds a block the user can copy after the run, from values already
        # measured this run.
        diagnostics = _scale_diagnostics_block(verification_seconds, beat_info)
        if diagnostics:
            status_msg = f"{status_msg}\n\n{diagnostics}"
        # Return preview path for display, keep session_state intact
        return preview_path, status_msg, session_state

    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        import traceback
        traceback.print_exc()
        return None, error_msg, session_state


def process_video(audio_file: str, video_files: VideoFilesInput,
                 output_filename: str, processing_mode: str,
                 custom_fps: float, session_state: dict,
                 variation_seed: int = 0,
                 verification_seconds: float | None = None) -> Iterator[StatusResult]:
    """Run the pipeline in a worker thread, streaming structured progress to the UI.

    [FORK] Digital-Union: the queue now carries :class:`ProgressEvent` objects instead of status
    sentences, and stage identity comes from ``event.stage`` rather than a regex over prose. The
    architecture is otherwise the one that was already here — worker thread + ``queue.Queue`` +
    generator — because Gradio components must only be touched from the generator, never from the
    worker thread.

    Legacy string statuses are still accepted on the same queue as a compatibility fallback, so a
    caller that only supplies ``progress_callback`` keeps working.
    """
    status_queue: queue.Queue[object | None] = queue.Queue()
    result_queue: queue.Queue[StatusResult] = queue.Queue(maxsize=1)
    console_logger = StageConsoleLogger(sys.__stdout__)
    quiet_console = QuietConsole()
    view = ProgressView()

    def progress_callback(message: str) -> None:
        # Compatibility path only: structured events are the primary source of stage identity.
        status_queue.put(str(message))

    def event_callback(event: ProgressEvent) -> None:
        # Called from the worker thread and from Stage 5/6 worker threads. Putting on a Queue is the
        # only cross-thread action taken here; no Gradio component is touched.
        status_queue.put(event)

    def worker() -> None:
        try:
            with contextlib.redirect_stdout(quiet_console), contextlib.redirect_stderr(quiet_console):
                result = _process_video_impl(
                    audio_file=audio_file,
                    video_files=video_files,
                    output_filename=output_filename,
                    processing_mode=processing_mode,
                    custom_fps=custom_fps,
                    variation_seed=variation_seed,
                    session_state=session_state,
                    progress_callback=progress_callback,
                    console_logger=console_logger,
                    event_callback=event_callback,
                    verification_seconds=verification_seconds,
                )
        except Exception as e:
            console_logger.line(f"Error: {e}")
            result = None, f"\u274c Error: {e}", session_state
        finally:
            console_logger.finish()
        result_queue.put(result)
        status_queue.put(None)

    # [FORK] Digital-Union (L0): the source verification already happened in the caller (it decides
    # whether this generator runs at all), so it is reported as a completed Stage 0 - `Stage.INPUT`,
    # which the progress model already defines for pre-Stage-1 source work. One event, on the queue
    # the loop below already drains, so both the status panel and the CMD log get it from the same
    # channel. No new logging system, and nothing here can affect the gate.
    if verification_seconds is not None:
        status_queue.put(ProgressEvent(
            stage=0,
            kind=EventKind.END,
            message=(f"Source verification: {_fmt_stage_seconds(verification_seconds)} "
                     f"for {len(video_files or [])} files"),
            elapsed_seconds=float(verification_seconds),
            data={"verified_source_count": len(video_files or [])},
        ))

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    last_status = "Starting\u2026"
    yield None, last_status, session_state

    while True:
        item = status_queue.get()
        if item is None:
            break
        if isinstance(item, ProgressEvent):
            view.apply(item)
            console_logger.apply_event(item)
            rendered = view.render()
        else:
            # Legacy string status: shown only when no structured event has arrived yet, so the old
            # "Stage N is processing" sentences cannot overwrite richer structured output.
            if view.active_stage() is not None:
                continue
            rendered = str(item)
        if rendered != last_status:
            last_status = rendered
            yield None, rendered, session_state

    thread.join()
    yield result_queue.get()


# [FORK] Digital-Union: source-confirmation UI glue.
#
# These handlers are deliberately thin. Every decision — what invalidates a confirmation, what may be
# confirmed, whether a render may start — is made by beatsync_fork.input_session, which has no Gradio
# dependency and is covered by tests/test_input_gate.py. Nothing here is authoritative: the Gradio
# State object holds the backend truth, and process_video_guarded() re-checks it against the
# filesystem before any Stage 1 work begins.

SOURCE_MODE_VALUES = {
    SourceMode.LOCAL_FOLDER.value: SourceMode.LOCAL_FOLDER,
    SourceMode.BROWSER_FILES.value: SourceMode.BROWSER_FILES,
}


def _source_ui_updates(state) -> Tuple:
    """Project the backend source state onto the four source widgets plus the Create button."""
    return (
        state.report_text,
        gr.update(value=state.confirm_button_label(), interactive=state.can_confirm()),
        state.confirmation_status_text(),
        gr.update(interactive=state.is_confirmed()),
        state,
    )


def _on_source_mode_change(mode_value: str, state) -> Tuple:
    mode = SOURCE_MODE_VALUES.get(str(mode_value), SourceMode.LOCAL_FOLDER)
    new_state = set_mode(state, mode)
    is_folder = mode is SourceMode.LOCAL_FOLDER
    return (gr.update(visible=is_folder), gr.update(visible=not is_folder)) + _source_ui_updates(
        new_state
    )


def _on_folder_path_change(folder_path: str, state) -> Tuple:
    return _source_ui_updates(set_folder_path(state, folder_path))


def _on_recursive_change(recursive: bool, state) -> Tuple:
    return _source_ui_updates(set_recursive(state, recursive))


def _on_scan_click(folder_path: str, recursive: bool, state) -> Tuple:
    # Re-apply the live widget values first: a Textbox `change` event may not have fired yet if the
    # user typed a path and clicked Scan immediately, and the scan must use what is on screen.
    state = set_recursive(set_folder_path(state, folder_path), recursive)
    return _source_ui_updates(scan_folder_action(state))


def _on_browser_files_change(file_paths, state) -> Tuple:
    return _source_ui_updates(set_browser_files(state, file_paths))


def _on_confirm_click(state) -> Tuple:
    return _source_ui_updates(confirm_action(state))


def process_video_guarded(audio_file: str, source_mode: str, source_folder: str,
                          source_recursive: bool, video_input: VideoFilesInput,
                          output_filename: str, processing_mode: str,
                          custom_fps: float, variation_seed: int, session_state: dict,
                          source_state) -> Iterator[StatusResult]:
    """Re-verify the confirmed source set against the LIVE controls, then delegate to the pipeline.

    This is the gate that matters. UI disablement is a courtesy; a stale browser tab, a queued event
    or a direct API call can all reach this handler.

    Crucially it takes the **live** source-control values submitted with this request, not just the
    stored session state. Gradio delivers widget changes as separate queued events, so at click time
    the state can lag behind the widgets — a file can finish uploading, or the folder textbox can
    change, before its `change` handler has run. Trusting the state alone would approve a render for a
    source set the user is no longer declaring. The event handlers above remain for immediate UX
    feedback; this is the authority.

    On success the freshly verified paths are handed to the existing `process_video` generator as the
    same `List[str]` it already consumed, so Auto Mode and the renderer are entirely unaware of input
    modes.
    """
    # Parameter names deliberately mirror the widget names in process_btn.click(inputs=...):
    # Gradio supplies them positionally, so a silent reordering would be invisible. A test asserts
    # the two lists line up name-for-name.
    #
    # `variation_seed` is a render-request input like FPS or the encoder, NOT source identity: it is
    # not wired into the source-confirmation handlers, so changing it cannot clear a confirmation.
    #
    # [FORK] Digital-Union (L0): the verification is timed, not changed. In local-folder mode it is
    # an authoritative filesystem re-scan of every confirmed source, so it is one of the costs that
    # grows with the library. Measuring wraps the existing call: the gate, the snapshot identity and
    # the allow/deny outcome are all untouched, and nothing is reused between renders.
    verification_started = time.perf_counter()
    decision = resolve_for_render(
        source_state,
        live_declaration(source_mode, source_folder, source_recursive, video_input),
    )
    verification_seconds = time.perf_counter() - verification_started
    if not decision.allowed:
        yield None, f"❌ {decision.message}", session_state
        return

    yield from process_video(
        audio_file=audio_file,
        video_files=list(decision.paths),
        output_filename=output_filename,
        processing_mode=processing_mode,
        custom_fps=custom_fps,
        session_state=session_state,
        variation_seed=variation_seed,
        verification_seconds=verification_seconds,
    )


# [FORK] Digital-Union (P V1): media library preparation.
#
# A deliberately separate workflow. It has its own `gr.State`, its own handlers and its own buttons,
# and it touches NONE of the Create Video machinery above: not `source_state`, not `source_outputs`,
# not `confirm_action`, not `process_btn`. Preparing a library can therefore never clear a render
# confirmation, and confirming sources can never invalidate a preparation scan.
#
# The division of labour mirrors the rest of the fork: every decision (what invalidates a scan, what
# may be analysed, what the report says) lives in `beatsync_fork.library_prep`, which is stdlib-only
# and tested without Gradio. This module supplies only the three runtime calls that module may not
# make itself - the folder scan, the Stage 1-4 track profile, and the Stage 5 classifier/analyzer.


def _prep_ui_updates(state) -> Tuple:
    """Project the preparation state onto its three widgets plus the state object."""
    return (
        state.report_text,
        state.notice,
        gr.update(value=state.analyze_button_label(), interactive=state.can_analyze()),
        state,
    )


def _prep_busy_updates(state, rendered: str, notice: str) -> Tuple:
    """An in-flight frame: live progress in the report box, Analyze held disabled."""
    return (rendered, notice, gr.update(interactive=False), state)


def _on_prep_folder_change(folder_path: str, state) -> Tuple:
    return _prep_ui_updates(fork_prep.set_folder(state, folder_path))


def _on_prep_recursive_change(recursive: bool, state) -> Tuple:
    return _prep_ui_updates(fork_prep.set_recursive(state, recursive))


def _on_prep_track_change(track_path: str, state) -> Tuple:
    return _prep_ui_updates(fork_prep.set_track(state, track_path))


def _resolve_prep_qwen_runtime() -> Tuple[bool, str]:
    """Resolve Qwen enablement and model path exactly as the normal Auto Mode render path does.

    Mirrors `auto_mode.analyze_beats_auto`: ``cfg.enable_qwen_semantics`` and the caller's enable
    flag (which the GUI never overrides, so it is ``True``), then ``BEATSYNC_DISABLE_QWEN=1`` forces
    it off; the model path falls back through ``cfg.qwen_model_path`` to ``DEFAULT_QWEN_MODEL_DIR``.

    Whether AI is actually *available* is not decided here - that stays with Stage 5's existing
    `_qwen_backend_available` rule, reached through `classify_library_sources`. No new analysis
    configuration model is introduced.
    """
    from video_analysis import DEFAULT_QWEN_MODEL_DIR

    qwen_enabled = bool(AUTO_MODE_CONFIG.enable_qwen_semantics)
    if os.environ.get("BEATSYNC_DISABLE_QWEN", "0") == "1":
        qwen_enabled = False
    model_path = AUTO_MODE_CONFIG.qwen_model_path or DEFAULT_QWEN_MODEL_DIR
    return qwen_enabled, model_path


def _prep_scan_impl(folder_path: str, recursive: bool, track_path: str, state,
                    event_callback=None, console_logger: StageConsoleLogger | None = None):
    """Classify the whole library once, against the edit style the chosen track resolves to.

    This is the expensive half of the P.1 rule: the Scan click pays for the full-library
    classification so the Analyze click can pass only the subset it identified.
    """
    from video_analysis import classify_library_sources

    # Re-apply the LIVE widget values first. A Textbox `change` event may not have fired if the user
    # typed a path and clicked Scan immediately - the same reason `_on_scan_click` does this.
    state = fork_prep.set_track(
        fork_prep.set_recursive(fork_prep.set_folder(state, folder_path), recursive),
        track_path,
    )

    if not state.folder.strip():
        return fork_prep.record_failure(
            state, "No library folder selected. Enter a folder path and press Scan Library.")
    audio_path = _as_existing_source_path(state.track_path)
    if not audio_path:
        return fork_prep.record_failure(
            state, "No track selected. Choose the audio track you will render with.")

    # 1. Enumerate the folder with the existing scanner. `detect_duplicates=False`: duplicate
    #    grouping is a reporting feature of the source screen and would be pure extra reads here.
    scan_started = time.perf_counter()
    try:
        input_set = scan_library_folder(state.folder, recursive=state.recursive,
                                        detect_duplicates=False)
    except (InputScanError, OSError) as exc:
        return fork_prep.record_failure(state, f"LIBRARY SCAN FAILED\n{exc}")
    folder_scan_seconds = time.perf_counter() - scan_started
    ready_paths = [media.path for media in input_set.files]

    # 2. Derive the profile through the existing seam. `video_files=None` makes
    #    `analyze_beats_auto` run audio Stages 1-4 and skip Stage 5 entirely, so this is the same
    #    `_build_audio_visual_profile` output a render would forward - not an approximation of it.
    profile_started = time.perf_counter()
    try:
        _selected, beat_info = analyze_beats_auto(
            audio_path,
            use_gpu=GPU_AVAILABLE,
            video_files=None,
            console_callback=(lambda stage, message:
                              console_logger.stage_line(stage, message) if console_logger else None),
            event_callback=event_callback,
        )
    except Exception as exc:
        return fork_prep.record_failure(state, f"TRACK ANALYSIS FAILED\n{exc}")
    profile = beat_info.get("audio_visual_profile") or {}
    profile_seconds = time.perf_counter() - profile_started

    # 3/4. Resolve the runtime exactly as Stage 5 expects, then classify read-only.
    qwen_enabled, model_path = _resolve_prep_qwen_runtime()
    classification = classify_library_sources(
        ready_paths,
        audio_profile=profile,
        enable_ai=qwen_enabled,
        qwen_model_path=model_path,
        event_callback=event_callback,
    )

    runtime = fork_prep.runtime_identity_from_classification(
        classification, qwen_enabled=qwen_enabled)
    preset = str(profile.get("smart_preset") or classification.get("smart_preset") or "")
    track = fork_prep.TrackIdentity.from_file(audio_path, preset)
    if track is None:
        return fork_prep.record_failure(
            state, "The track became unreadable during the scan. Choose it again.")

    scan = fork_prep.build_scan_result(
        folder=state.folder,
        recursive=state.recursive,
        track=track,
        audio_profile=profile,
        runtime=runtime,
        classification_items=classification.get("classifications") or (),
        supported_count=len(ready_paths),
        folder_scan_seconds=folder_scan_seconds,
        profile_seconds=profile_seconds,
        classify_seconds=classification.get("classify_seconds") or 0.0,
        cache_identity_seconds=classification.get("cache_identity_seconds") or 0.0,
        cache_lookup_seconds=classification.get("cache_lookup_seconds") or 0.0,
    )
    return fork_prep.record_scan(state, scan)


def _prep_analyze_impl(state, event_callback=None,
                       console_logger: StageConsoleLogger | None = None):
    """Analyze only the subset the recorded scan classified as needing work.

    Persistence is entirely the existing analyzer's: this never calls `_analyze_single_video`,
    `_checkpoint_cache` or `_save_cache`, and the returned candidate library is discarded apart from
    the current-run statistics used for the summary.
    """
    from video_analysis import analyze_video_sources, classify_library_sources

    scan = state.scan
    refusal = fork_prep.analyze_refusal(state, None)
    if refusal is not None:
        return fork_prep.record_failure(state, refusal, notice="Preparation run refused.")

    # Recompute the invocation-scoped identity once and compare it with the scan's. Passing an empty
    # source list reuses the one classifier seam to do exactly that work - backend availability,
    # backend token, config token - and classifies nothing, so no alternate identity code exists
    # here. No progress callback: this probe is not a phase the user needs to watch.
    qwen_enabled, model_path = _resolve_prep_qwen_runtime()
    probe = classify_library_sources(
        [], audio_profile=scan.audio_profile, enable_ai=qwen_enabled,
        qwen_model_path=model_path, event_callback=None)
    runtime = fork_prep.runtime_identity_from_classification(probe, qwen_enabled=qwen_enabled)
    refusal = fork_prep.analyze_refusal(state, runtime)
    if refusal is not None:
        return fork_prep.record_failure(state, refusal, notice="Preparation run refused.")

    # The P.1 rule: only the classified subset. For the measured 902-source library that is 4 paths,
    # not 902. The analyzer re-derives each selected source's own identity, which is correct.
    subset = list(scan.subset_for_analysis())
    try:
        result = analyze_video_sources(
            video_files=subset,
            audio_profile=dict(scan.audio_profile),
            use_gpu=GPU_AVAILABLE,
            enable_ai=qwen_enabled,
            qwen_model_path=model_path,
            event_callback=event_callback,
        )
    except Exception as exc:
        return fork_prep.record_failure(
            state, f"PREPARATION ANALYSIS FAILED\n{exc}", notice="Preparation run failed.")

    _stage5_summary(console_logger, result if isinstance(result, dict) else None)
    return fork_prep.record_analysis_complete(state, fork_prep.summarize_analysis_run(result))


def _run_prep_in_worker(work, state) -> Iterator[Tuple]:
    """Run one preparation step on a worker thread, streaming structured progress to the UI.

    Same shape as `process_video`, and for the same reasons: the pipeline prints to a stdout the GUI
    discards, Stage 1-4 and Stage 5 emit `ProgressEvent`s from worker threads, and Gradio components
    may only be touched from the generator. `work` takes ``(event_callback, console_logger)`` and
    returns the new preparation state.
    """
    status_queue: queue.Queue[object | None] = queue.Queue()
    result_queue: queue.Queue[object] = queue.Queue(maxsize=1)
    console_logger = StageConsoleLogger(sys.__stdout__)
    quiet_console = QuietConsole()
    view = ProgressView()

    def event_callback(event: ProgressEvent) -> None:
        # Worker thread: putting on a Queue is the only cross-thread action, exactly as in
        # `process_video`. No Gradio component is touched here.
        status_queue.put(event)

    def worker() -> None:
        try:
            with contextlib.redirect_stdout(quiet_console), contextlib.redirect_stderr(quiet_console):
                new_state = work(event_callback, console_logger)
        except Exception as exc:
            console_logger.line(f"Error: {exc}")
            new_state = fork_prep.record_failure(state, f"UNEXPECTED ERROR\n{exc}")
        finally:
            console_logger.finish()
        result_queue.put(new_state)
        status_queue.put(None)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    last_rendered = "Starting…"
    yield _prep_busy_updates(state, last_rendered, "Working…")

    while True:
        item = status_queue.get()
        if item is None:
            break
        if not isinstance(item, ProgressEvent):
            continue
        view.apply(item)
        console_logger.apply_event(item)
        rendered = view.render()
        if rendered != last_rendered:
            last_rendered = rendered
            yield _prep_busy_updates(state, rendered, "Working…")

    thread.join()
    yield _prep_ui_updates(result_queue.get())


def _on_prep_scan_click(folder_path: str, recursive: bool, track_path: str, state) -> Iterator[Tuple]:
    yield from _run_prep_in_worker(
        lambda event_callback, console_logger: _prep_scan_impl(
            folder_path, recursive, track_path, state,
            event_callback=event_callback, console_logger=console_logger),
        state,
    )


def _on_prep_analyze_click(state) -> Iterator[Tuple]:
    yield from _run_prep_in_worker(
        lambda event_callback, console_logger: _prep_analyze_impl(
            state, event_callback=event_callback, console_logger=console_logger),
        state,
    )


def cleanup_on_startup():
    """
    Clean temporary runtime files on script start while preserving user inputs
    and the persistent video analysis cache.
    """
    input_base = get_input_dir()
    protected_dirs = {'audio', 'video', 'video_analysis_cache'}

    try:
        os.makedirs(get_audio_input_dir(), exist_ok=True)
        os.makedirs(get_video_input_dir(), exist_ok=True)
        os.makedirs(os.path.join(input_base, 'gradio_uploads'), exist_ok=True)

        if os.path.exists(input_base):
            for item in os.listdir(input_base):
                item_path = os.path.join(input_base, item)

                # Keep the latest user input files across restarts.
                if item in protected_dirs:
                    continue

                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path, ignore_errors=True)
                    elif os.path.isfile(item_path):
                        os.remove(item_path)
                except Exception as e:
                    print(f"   ⚠️  Could not clean {item}: {e}")

        # Recreate runtime temp upload folder after cleanup.
        os.makedirs(os.path.join(input_base, 'gradio_uploads'), exist_ok=True)

    except Exception as e:
        print(f"   ⚠️  Warning during startup cleanup: {e}")


def create_ui() -> gr.Blocks:
    # These definitions are needed within the function's scope
    python_status = "✅ Portable (bin/python-3.13.14-embed-amd64/)" if USING_PORTABLE_PYTHON else "⚠️  System Python"
    if USING_CUPY_CTK:
        cuda_status = "✅ CuPy CTK (Python wheel libraries)"
    elif USING_PORTABLE_CUDA:
        cuda_status = "✅ Portable (bin/CUDA/v13.3)"
    else:
        cuda_status = "⚠️  System CUDA (or not available)"
    ffmpeg_status = "✅ Portable (bin/ffmpeg/)" if FFMPEG_FOUND else "⚠️  System FFmpeg"
    
    app = gr.Blocks(title='BeatSync Engine', theme='ocean', css=STATUS_BOX_CSS)
    with app:
        session_state = gr.State({})
        # [FORK] Digital-Union: source-input confirmation state, kept separate from the processing
        # session state so source identity is never entangled with render bookkeeping.
        source_state = gr.State(initial_source_state())
        # [FORK] Digital-Union (P V1): preparation has its OWN state object. It is never read or
        # written by the source-confirmation handlers, and it never reaches `process_video_guarded`.
        prep_state = gr.State(initial_prep_state())

        gr.Markdown(f"# {UI_TITLE}")
        gr.Markdown(UI_MAIN_DESCRIPTION)

        with gr.Row():
            with gr.Column(scale=1):
                gr.Markdown('### 📁 Input Files')
                audio_input = gr.File(label=LABEL_AUDIO_FILE, file_types=['.mp3', '.wav', '.flac'], type='filepath', elem_id='audio-file-input')

                # [FORK] Digital-Union: local-folder mode + authoritative confirmation gate.
                with gr.Group():
                    gr.Markdown('### 🎬 Video Source')
                    source_mode = gr.Radio(
                        choices=[
                            (CHOICE_SOURCE_LOCAL_FOLDER, SourceMode.LOCAL_FOLDER.value),
                            (CHOICE_SOURCE_BROWSER_FILES, SourceMode.BROWSER_FILES.value),
                        ],
                        value=SourceMode.LOCAL_FOLDER.value,
                        label=LABEL_SOURCE_MODE,
                        info=INFO_SOURCE_MODE,
                        elem_id='source-mode-radio',
                    )

                    with gr.Group(visible=True) as folder_group:
                        source_folder = gr.Textbox(
                            label=LABEL_SOURCE_FOLDER,
                            placeholder=PLACEHOLDER_SOURCE_FOLDER,
                            info=INFO_SOURCE_FOLDER,
                            elem_id='source-folder-input',
                        )
                        source_recursive = gr.Checkbox(
                            value=True, label=LABEL_SOURCE_RECURSIVE, elem_id='source-recursive'
                        )
                        scan_btn = gr.Button(LABEL_SCAN_FOLDER, elem_id='scan-folder-button')

                    with gr.Group(visible=False) as browser_group:
                        video_input = gr.File(label=LABEL_VIDEO_FILES, file_count='multiple', file_types=['.mp4', '.mkv'], type='filepath', elem_id='video-files-input')

                    source_report = gr.Textbox(
                        label=LABEL_SOURCE_REPORT,
                        value=initial_source_state().report_text,
                        interactive=False,
                        lines=9,
                        max_lines=14,
                        elem_id='source-report-box',
                    )
                    confirm_btn = gr.Button(
                        LABEL_CONFIRM_SOURCES, interactive=False, elem_id='confirm-sources-button'
                    )
                    confirm_status = gr.Markdown(
                        initial_source_state().confirmation_status_text(),
                        elem_id='confirm-status',
                    )

                with gr.Group():
                    gr.Markdown('### ⚙️ Video Settings')
                    custom_fps = gr.Number(label=LABEL_CUSTOM_FPS, value=None, precision=2, info=INFO_CUSTOM_FPS)

                # [FORK] Digital-Union (Phase A): creative variation seed. Deliberately outside the
                # Video Source group and never wired into `source_outputs`, so changing it cannot
                # invalidate a confirmed source set.
                with gr.Group():
                    gr.Markdown('### 🎨 Creative Direction')
                    variation_seed = gr.Number(
                        label=LABEL_VARIATION_SEED,
                        value=0,
                        precision=0,
                        minimum=0,
                        info=INFO_VARIATION_SEED,
                        elem_id='variation-seed-input',
                    )
                    randomize_btn = gr.Button(LABEL_RANDOMIZE_SEED, elem_id='randomize-seed-button')

                with gr.Group():
                    gr.Markdown(f'### 🎬 Processing Mode')
                    if NVENC_AVAILABLE:
                        processing_mode = gr.Radio(choices=[('NVIDIA NVENC H.264', 'h264_nvenc'), ('NVIDIA NVENC HEVC (H.265)', 'hevc_nvenc'), ('CPU (H.264)', 'cpu'), ('ProRes 422 Proxy (Precise Mode)', 'prores_proxy')], value='h264_nvenc', label=LABEL_PROCESSING_MODE, info=get_processing_mode_info_nvenc())
                    else:
                        processing_mode = gr.Radio(choices=[('CPU (H.264)', 'cpu'), ('ProRes 422 Proxy (Precise Mode)', 'prores_proxy')], value='cpu', label=LABEL_PROCESSING_MODE, info=get_processing_mode_info_cpu())
                
                with gr.Group():
                    gr.Markdown('### 📁 Output Settings')
                    output_filename = gr.Textbox(value='music_video.mp4', label=LABEL_OUTPUT_FILENAME, info=INFO_OUTPUT_FILENAME)

                # [FORK] Digital-Union: starts disabled; enabled only by an explicit confirmation.
                process_btn = gr.Button(
                    '🎬 Create Music Video', variant='primary', size='lg', interactive=False
                )
                gr.Markdown(INFO_CONFIRMATION_GATE)

                # [FORK] Digital-Union (P V1): media library preparation. A separate, collapsed
                # section rather than a restructuring of the app into tabs - it is an occasional
                # maintenance workflow, not part of the render flow. Nothing in here is wired to
                # `source_outputs`, so it cannot touch the Create Music Video gate.
                with gr.Accordion(label=LABEL_PREP_SECTION, open=False):
                    gr.Markdown(INFO_PREP_SECTION)
                    prep_folder = gr.Textbox(
                        label=LABEL_PREP_FOLDER,
                        placeholder=PLACEHOLDER_SOURCE_FOLDER,
                        info=INFO_PREP_FOLDER,
                        elem_id='prep-folder-input',
                    )
                    prep_recursive = gr.Checkbox(
                        value=True, label=LABEL_PREP_RECURSIVE, elem_id='prep-recursive'
                    )
                    prep_track = gr.File(
                        label=LABEL_PREP_TRACK,
                        file_types=['.mp3', '.wav', '.flac'],
                        type='filepath',
                        elem_id='prep-track-input',
                    )
                    gr.Markdown(INFO_PREP_TRACK)
                    prep_scan_btn = gr.Button(LABEL_PREP_SCAN, elem_id='prep-scan-button')
                    prep_report = gr.Textbox(
                        label=LABEL_PREP_REPORT,
                        value=initial_prep_state().report_text,
                        interactive=False,
                        lines=10,
                        max_lines=16,
                        elem_id='prep-report-box',
                    )
                    prep_analyze_btn = gr.Button(
                        LABEL_PREP_ANALYZE, interactive=False, elem_id='prep-analyze-button'
                    )
                    prep_status = gr.Markdown(
                        initial_prep_state().notice, elem_id='prep-status'
                    )

            with gr.Column(scale=1):
                gr.Markdown('### 📺 Output')
                status_output = gr.Textbox(label='Status', interactive=False, value=get_ready_status(python_status, cuda_status, MAX_THREADS, CPU_COUNT, ffmpeg_status, GPU_AVAILABLE, gpu_info, NVENC_AVAILABLE), lines=4, max_lines=4, elem_id='status-output-box')
                video_output = gr.Video(label='Generated Music Video', interactive=False, elem_id='generated-video-output')
                
        # [FORK] Digital-Union: source-mode / scan / confirm wiring.
        #
        # Every one of these events routes through beatsync_fork.input_session, so any source change
        # clears the confirmation and disables Create Music Video. Note which inputs are absent:
        # FPS, encoder, output filename and audio are never wired here, so changing them cannot
        # invalidate a source confirmation — they are not source-video identity.
        source_outputs = [source_report, confirm_btn, confirm_status, process_btn, source_state]

        source_mode.change(
            fn=_on_source_mode_change,
            inputs=[source_mode, source_state],
            outputs=[folder_group, browser_group] + source_outputs,
        )
        source_folder.change(
            fn=_on_folder_path_change,
            inputs=[source_folder, source_state],
            outputs=source_outputs,
        )
        source_recursive.change(
            fn=_on_recursive_change,
            inputs=[source_recursive, source_state],
            outputs=source_outputs,
        )
        scan_btn.click(
            fn=_on_scan_click,
            inputs=[source_folder, source_recursive, source_state],
            outputs=source_outputs,
        )
        # `change` covers files arriving, being added to, and being cleared from the browser input.
        video_input.change(
            fn=_on_browser_files_change,
            inputs=[video_input, source_state],
            outputs=source_outputs,
        )
        confirm_btn.click(
            fn=_on_confirm_click,
            inputs=[source_state],
            outputs=source_outputs,
        )

        # [FORK] Digital-Union (Phase A): writes a fresh positive seed into the box and nothing else.
        # Note what is absent: `source_outputs`. The seed is creative state, not source identity.
        randomize_btn.click(
            fn=fork_variation.random_seed,
            inputs=[],
            outputs=[variation_seed],
        )

        # [FORK] Digital-Union (P V1): media library preparation wiring.
        #
        # `prep_outputs` is a wholly separate list from `source_outputs`: it contains no source
        # widget, no confirmation widget and not `process_btn`, so preparation can never enable,
        # disable or invalidate the Create Video gate. Conversely no source handler writes here.
        #
        # Every classification input - folder, recursive, track - clears the recorded scan, which
        # is what stops Analyze from acting on a classification that no longer describes reality.
        prep_outputs = [prep_report, prep_status, prep_analyze_btn, prep_state]

        prep_folder.change(
            fn=_on_prep_folder_change,
            inputs=[prep_folder, prep_state],
            outputs=prep_outputs,
        )
        prep_recursive.change(
            fn=_on_prep_recursive_change,
            inputs=[prep_recursive, prep_state],
            outputs=prep_outputs,
        )
        prep_track.change(
            fn=_on_prep_track_change,
            inputs=[prep_track, prep_state],
            outputs=prep_outputs,
        )
        prep_scan_btn.click(
            fn=_on_prep_scan_click,
            inputs=[prep_folder, prep_recursive, prep_track, prep_state],
            outputs=prep_outputs,
            show_progress='hidden',
        )
        prep_analyze_btn.click(
            fn=_on_prep_analyze_click,
            inputs=[prep_state],
            outputs=prep_outputs,
            show_progress='hidden',
        )

        # [FORK] Digital-Union: the live source controls are inputs to the render request, so the
        # gate validates what the widgets currently declare rather than possibly-stale gr.State.
        process_btn.click(
            fn=process_video_guarded,
            inputs=[
                audio_input,
                source_mode, source_folder, source_recursive, video_input,
                output_filename, processing_mode, custom_fps, variation_seed,
                session_state, source_state
            ],
            outputs=[video_output, status_output, session_state],
            show_progress='hidden'
        )

    return app

if __name__ == '__main__':
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass
    
    # Clean up old files only on startup
    cleanup_on_startup()
    
    app = create_ui()
    launch_port = find_launch_port()
    app.launch(
        server_name="127.0.0.1",
        server_port=launch_port,
        share=False,
        inbrowser=True,
        show_error=True
    )
