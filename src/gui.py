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
# [FORK] Digital-Union: the resolved Creative Profile (Creative Controls Core). Normalisation, the
# control mappings and every neutrality decision live in src/beatsync_fork/creative.py; this module
# only collapses the four widgets into one profile at the render boundary.
from beatsync_fork import creative as fork_creative
# [FORK] Digital-Union (Creative Controls Extra PR3): the named preset recipes. The table and the
# three total helpers live in src/beatsync_fork/presets.py (stdlib-only, Gradio-free); this module
# only wires them to the selector. Nothing below the widget layer ever learns a preset name.
from beatsync_fork import presets as fork_presets
# [FORK] Digital-Union (Variant Lab V1 / C2): the reproducible recipe generator. The named RNG
# sub-streams, the spread formula and every normalisation live in src/beatsync_fork/variant_lab.py,
# and the resolved seven-integer recipe it returns is src/beatsync_fork/creative_recipe.py (both
# stdlib-only, Gradio-free). This module only wires them to widgets: a resolved recipe is written
# into the existing Variation Seed and six sliders, and nothing downstream of them learns that
# Variant Lab exists. `creative_recipe` is deliberately NOT imported here — the GUI only ever
# handles the resolution object, so it has no reason to name the recipe type.
from beatsync_fork import variant_lab as fork_lab
# [FORK] Digital-Union (Audio Layers V1 / D): voice over music. The placement rules live in
# src/beatsync_fork/audio_mix.py (stdlib-only, Gradio-free) and the FFmpeg mixdown in
# src/audio_mixdown.py. Both run AFTER the music analysis and feed only the final render audio —
# the original music stays the one thing Stages 1-5 ever see.
from beatsync_fork import audio_mix as fork_audio_mix
# [FORK] Digital-Union (Smart Mix V1 / E): deterministic SFX accents. The role vocabulary and all
# five placement rules live in src/beatsync_fork/smart_mix.py (stdlib-only, Gradio-free); the
# library scan, probing and the FFmpeg streams reuse src/audio_mixdown.py — there is no second
# mix engine, and SFX reach only the final render audio.
from beatsync_fork import smart_mix as fork_smart_mix
import audio_mixdown

#: [FORK] Digital-Union (Audio Layers V1 / D): where the placement read-out rides between the
#: worker and the generator. It lives in `session_state` as ordinary render bookkeeping — the
#: worker thread must never touch a Gradio component, so the value is carried on the dict the
#: generator already yields and projected onto the widget there.
AUDIO_LAYERS_REPORT_KEY = 'audio_layers_report'

#: [FORK] Digital-Union (Smart Mix V1 / E): the same bookkeeping seam as the Audio Layers report —
#: the worker thread must never touch a Gradio component, so the text rides on `session_state` and
#: the generator projects it onto the widget. Pure diagnostics: nothing downstream reads it.
SMART_MIX_REPORT_KEY = 'smart_mix_report'

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
# [FORK] Digital-Union (Audio Layers V1 / D, R1-B; extended by Smart Mix V1 / E): what
# `process_video_guarded` yields to Gradio — the three values `process_video` streams, plus the
# Audio Layers placement read-out and the Smart Mix read-out. The inner
# 3-value contract is deliberately unchanged; only the outermost handler projects onto the widgets.
GuardedResult : TypeAlias = Tuple[str, str, Dict, str, str]

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
        # [FORK] Digital-Union (Creative Controls Core): the resolved profile replaces the seed-only
        # text on the line that already exists, rather than spending another of the console's five
        # slots. A neutral render still prints exactly "legacy", as it does today; the fallback
        # keeps a pre-Core plan summary (seed only) readable.
        planner_bits.append(
            plan_summary.get("creative_text") or fork_variation.describe(plan_summary.get("seed"))
        )
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
                       custom_fps: float, creative: fork_creative.CreativeProfile | None,
                       session_state: dict,
                       progress_callback: Callable[[str], None] | None = None,
                       console_logger: StageConsoleLogger | None = None,
                       event_callback: Callable[[ProgressEvent], None] | None = None,
                       verification_seconds: float | None = None,
                       voice_files: VideoFilesInput = None,
                       audio_mix: fork_audio_mix.AudioMixConfig | None = None,
                       sfx_root: str | None = None,
                       smart_mix: fork_smart_mix.SmartMixConfig | None = None) -> StatusResult:
    # [FORK] Digital-Union (Creative Controls Core): one already-normalised `CreativeProfile`
    # replaces the Phase A raw `variation_seed`, so the four controls are not threaded through every
    # inner function as loose scalars. `None` means an all-neutral render, which is what a caller
    # supplying nothing has always got.
    creative = creative if creative is not None else fork_creative.NEUTRAL_PROFILE
    # [FORK] Digital-Union (Audio Layers V1 / D): declared before the `try` so the `finally` below
    # can always run and the success panel can always ask whether voice was used, including on the
    # early-return paths.
    mixed_master_path = None
    audio_plan = None
    # [FORK] Digital-Union (Smart Mix V1 / E): same lifetime, same reason — the success panel asks
    # afterwards whether any SFX was actually used.
    smart_mix_plan = None
    # [FORK] Digital-Union (Audio Layers V1 / D, R1-B; Smart Mix V1 / E): the read-outs are render
    # bookkeeping, cleared at the start of every attempt so a previous render's placements can
    # never be mistaken for this one's. They are pure diagnostics: nothing downstream reads them,
    # and they are not source or preparation state.
    session_state[AUDIO_LAYERS_REPORT_KEY] = ''
    session_state[SMART_MIX_REPORT_KEY] = ''
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
        
        # [FORK] Digital-Union (Audio Layers V1 / D): voice preflight, deliberately BEFORE Stage 1.
        #
        # Resolving paths and probing durations is cheap; a full Stage 1-5 analysis is not. A
        # missing or unreadable voice clip is a user-fixable input problem, so it is caught here
        # rather than after minutes of audio and video analysis. Placement needs `beat_info` and
        # therefore cannot happen yet — only the file facts are established now.
        #
        # Ordering is the pure module's deterministic path order, never the browser's multi-select
        # order. `prepare_voice_inputs` returns () for an empty selection, which is what keeps the
        # no-voice path below structurally the original one.
        #
        # The selection is handed over WHOLE. `_as_existing_source_paths` is deliberately not used
        # here: it filters out paths that no longer exist, which is right for video sources (the
        # confirmation gate has already vouched for them) and wrong for voice — it would turn a
        # three-clip selection with a missing middle file into a silent two-clip render. Validating
        # the complete selection is `prepare_voice_inputs`'s job.
        prepared_voices = ()
        if voice_files:
            try:
                prepared_voices = audio_mixdown.prepare_voice_inputs(voice_files)
            except audio_mixdown.AudioMixError as exc:
                return None, f'❌ Audio Layers: {exc}', session_state

        # [FORK] Digital-Union (Smart Mix V1 / E): the SFX library preflight, also BEFORE Stage 1
        # and for the same reason. Smart Mix is ACTIVE only when a root is given, Amount > 0 and at
        # least one role is enabled; otherwise nothing here runs at all — no scan, no ffprobe, no
        # planner, no FFmpeg input — which is what keeps the no-SFX path structurally identical to
        # D's (voice present) or to the original legacy path (no voice).
        #
        # SFX Level deliberately does NOT gate this: level 0 is a valid mute/debug value, so the
        # plan and the report still resolve and the streams are simply added at gain 0.0.
        smart_mix = smart_mix if smart_mix is not None else fork_smart_mix.SmartMixConfig()
        smart_mix_active = bool(sfx_root and str(sfx_root).strip()) and smart_mix.plans_anything
        sfx_assets = ()
        sfx_diagnostics = None
        if smart_mix_active:
            try:
                sfx_assets, sfx_diagnostics = audio_mixdown.prepare_sfx_inputs(
                    sfx_root, smart_mix.enabled_roles)
            except audio_mixdown.AudioMixError as exc:
                return None, f'❌ Smart Mix: {exc}', session_state

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
        # good result can be found again. Seed 0 keeps today's name exactly, and the three newer
        # creative controls deliberately add nothing to the name.
        filename = f"{name}_{timestamp}{creative.filename_suffix()}{ext}"
        output_path = os.path.join(output_folder, filename)
        temp_output = os.path.join(session_dir, filename)

        selected_beats, beat_info = analyze_beats_auto(
            local_audio_path,
            use_gpu=use_gpu,
            video_files=local_video_paths,
            progress_callback=progress_callback,
            console_callback=lambda stage, message: console_logger.stage_line(stage, message) if console_logger else None,
            event_callback=event_callback,
            creative=creative.as_dict(),
        )
        beat_times = beat_info.get('times', selected_beats)
        _stage5_summary(console_logger, beat_info.get("video_analysis"))

        # [FORK] Digital-Union (Audio Layers V1 / D): the ONE audio substitution, and the only
        # place voice touches the render.
        #
        # Note what has already happened above: `analyze_beats_auto` was handed
        # `local_audio_path` — the ORIGINAL music — so tempo, the beat grid, sections, energy, cut
        # selection, Qwen and the visual targets were all decided before any voice existed. The
        # mixed master is produced *from* that finished analysis and is never fed back into it.
        #
        # With no voice clips and no SFX this branch does nothing at all: no planner, no FFmpeg, no
        # temporary file, and `render_audio_path` is still the exact original path. That is a
        # structural branch, not a mixed-but-equivalent WAV.
        render_audio_path = local_audio_path

        # [FORK] Digital-Union (Smart Mix V1 / E): SFX are planned HERE, from the finished
        # `beat_info` — after the analysis, never before it and never fed back into it. The pure
        # planner receives a small immutable projection rather than the mutable bus.
        sfx_placements = ()
        if smart_mix_active:
            try:
                structure = fork_smart_mix.project_structure(beat_info)
            except fork_smart_mix.SmartMixStructureError as exc:
                return None, f'❌ Smart Mix: {exc}', session_state
            # The scan's diagnostics ride along untouched: the GUI never formats a warning of its
            # own, so `SmartMixPlan.report_lines()` stays the single source of the report.
            smart_mix_plan = fork_smart_mix.plan_sfx(
                structure, sfx_assets, smart_mix,
                library_root=str(sfx_root),
                library_diagnostics=sfx_diagnostics)
            sfx_placements = smart_mix_plan.placements
            # The pure planner already produced these lines; the GUI never recomputes placement.
            session_state[SMART_MIX_REPORT_KEY] = '\n'.join(smart_mix_plan.report_lines())

        # Smart Mix that resolved ZERO placements must not manufacture a mixdown: with no voice
        # either, the original music is still exactly the right render audio, and producing an
        # identical-but-re-encoded WAV would be pure cost. The zero-placement report survives to
        # explain why.
        if prepared_voices or sfx_placements:
            try:
                mixed_master_path, audio_plan = audio_mixdown.build_mixed_master(
                    music_path=local_audio_path,
                    music_duration=float(beat_info.get('audio_duration') or 0.0)
                    or audio_mixdown.probe_duration(local_audio_path),
                    beat_times=beat_times,
                    sections=fork_audio_mix.project_sections(beat_info.get('sections')),
                    voices=prepared_voices,
                    config=audio_mix or fork_audio_mix.AudioMixConfig(),
                    session_dir=session_dir,
                    sfx_placements=sfx_placements,
                    sfx_level_percent=smart_mix.sfx_level_percent,
                )
            except audio_mixdown.AudioMixError as exc:
                # Deliberately before any clip extraction: the user asked for voice and/or SFX, so a
                # silent fallback to the original music would render a plausible but wrong video.
                return None, f'❌ Audio Layers: {exc}', session_state
            render_audio_path = mixed_master_path
            # The pure planner already produced these lines; the GUI never recomputes placement.
            # Guarded on `prepared_voices`, not on the mixdown having happened: an SFX-only render
            # also builds a master, and a voice report reading "Music + 0 voice clips" would be a
            # read-out of something the user never asked for.
            if prepared_voices:
                session_state[AUDIO_LAYERS_REPORT_KEY] = '\n'.join(audio_plan.report_lines())
                if console_logger:
                    for line in audio_plan.report_lines():
                        console_logger.line(line)

        if progress_callback:
            progress_callback(_stage_status(6))

        # Create video
        result_path = create_music_video(
            render_audio_path, local_video_paths, selected_beats,
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
            # [FORK] Digital-Union (Creative Controls Core): the whole resolved profile, so a render
            # worth keeping can be reproduced from the panel. Omitted entirely on a neutral render,
            # exactly as the seed-only line was.
            variation_text=None if creative.is_neutral() else creative.describe(),
        )
        # [FORK] Digital-Union (L0.1): append the L0 baseline. The success statistics above are
        # untouched; this only adds a block the user can copy after the run, from values already
        # measured this run.
        diagnostics = _scale_diagnostics_block(verification_seconds, beat_info)
        if diagnostics:
            status_msg = f"{status_msg}\n\n{diagnostics}"
        # [FORK] Digital-Union (Audio Layers V1 / D): one concise line, and ONLY when voice was
        # actually used — a render without voice keeps the existing panel character for character.
        if audio_plan is not None and prepared_voices:
            status_msg = f"{status_msg}\n\n{audio_plan.summary_line()}"
        # [FORK] Digital-Union (Smart Mix V1 / E): a separate concise line, and ONLY when at least
        # one SFX was actually placed. An active Smart Mix that resolved zero placements keeps its
        # explanatory report but adds no summary, because no SFX was used.
        if smart_mix_plan is not None and smart_mix_plan.total:
            status_msg = f"{status_msg}\n\n{smart_mix_plan.summary_line()}"
        # Return preview path for display, keep session_state intact
        return preview_path, status_msg, session_state

    except Exception as e:
        error_msg = f"❌ Error: {str(e)}"
        import traceback
        traceback.print_exc()
        return None, error_msg, session_state

    finally:
        # [FORK] Digital-Union (Audio Layers V1 / D): the mixed master is temporary, so it goes on
        # every path — success, render failure, and the early returns above. There is no persistent
        # audio cache in V1 and a fresh uuid path per render, so nothing stale can be picked up
        # later. The original music and the user's voice files are never touched.
        audio_mixdown.discard_master(mixed_master_path)


def process_video(audio_file: str, video_files: VideoFilesInput,
                 output_filename: str, processing_mode: str,
                 custom_fps: float, session_state: dict,
                 creative: fork_creative.CreativeProfile | None = None,
                 verification_seconds: float | None = None,
                 voice_files: VideoFilesInput = None,
                 audio_mix: fork_audio_mix.AudioMixConfig | None = None,
                 sfx_root: str | None = None,
                 smart_mix: fork_smart_mix.SmartMixConfig | None = None) -> Iterator[StatusResult]:
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
                    creative=creative,
                    session_state=session_state,
                    progress_callback=progress_callback,
                    console_logger=console_logger,
                    event_callback=event_callback,
                    verification_seconds=verification_seconds,
                    voice_files=voice_files,
                    audio_mix=audio_mix,
                    sfx_root=sfx_root,
                    smart_mix=smart_mix,
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


# [FORK] Digital-Union (Creative Controls Extra PR3): the two preset handlers.
#
# They are the entire feature. Both are pure, both touch only Creative Direction widgets, and
# neither knows that a render, a source set or a pipeline exists — a preset is a name for six
# numbers, and these two functions are the only place that name is ever resolved.
#
# The pair forms an acyclic event graph because both are registered on `.input()`, which Gradio
# fires only for a *user* change (`.change()` fires for programmatic updates too). So writing the
# sliders from a preset cannot re-trigger the slider handler, and writing the selector from a
# slider cannot re-trigger the preset handler. Nothing here needs a re-entrancy guard, and a
# `.change()` registration on any of these seven widgets would reintroduce the cycle.


def _on_preset_input(preset_name: str) -> Tuple:
    """Write one named recipe into the six creative sliders, in `CREATIVE_CONTROL_FIELDS` order.

    `Custom` is not a recipe — it is the selector's way of reporting that the live values match no
    named one — so selecting it resolves to `None` and returns `gr.skip()` for every slider rather
    than writing anything. The same branch covers an unknown or malformed selector value, so this
    cannot raise mid-interaction.

    The Variation Seed is deliberately not an output: it is independent creative state with its own
    randomiser, and no preset may move it.
    """
    values = fork_presets.preset_values(preset_name)
    if values is None:
        return tuple(gr.skip() for _ in fork_presets.CREATIVE_CONTROL_FIELDS)
    return values


def _on_creative_control_input(cut_density, micro_cuts, semantic_emphasis,
                               energy_response, motion_bias, source_diversity) -> str:
    """Recompute the selector label from the six live slider values.

    Registered from every one of the six sliders, and it reads *all* of them rather than being told
    which one moved: the label is a statement about the whole six-value tuple, so it is derived from
    the whole tuple. That is what lets a manual edit back onto a recipe correctly read that recipe's
    name again instead of being stuck on `Custom`, and it is why there is no remembered
    "last selected preset" anywhere in this module.

    Parameter order mirrors `fork_presets.CREATIVE_CONTROL_FIELDS`, because Gradio passes `inputs`
    positionally.
    """
    return fork_presets.matching_preset((cut_density, micro_cuts, semantic_emphasis,
                                         energy_response, motion_bias, source_diversity))


# [FORK] Digital-Union (Variant Lab V1 / C2): the two Variant Lab handlers.
#
# Both read the six LIVE slider values as inputs — there is no cached base profile anywhere, so a
# preset change or a manual edit is picked up by the next Generate automatically. Neither handler
# registers anything on a slider: the existing preset `.input()` graph is untouched, and these run
# only on their own button clicks.
#
# What they write is deliberately the whole story: the master seed (so a freshly minted one is
# always visible), the existing Variation Seed, the six sliders, the preset label and the report.
# No source widget, no preparation widget, no `process_btn` — generating a variant cannot clear a
# confirmation or start a render.


#: Display label for each creative control inside the Variant Lab checkbox group. An explicit
#: mapping, never derived from the field name by lowercasing or replacing underscores: the resolver
#: keys on exact field names, so a heuristic here would be a silent correctness hazard. The choice
#: list is ordered by `CREATIVE_CONTROL_FIELDS`, so it cannot drift from the resolver's order.
_VARIANT_CONTROL_LABELS = {
    'cut_density': LABEL_CUT_DENSITY,
    'micro_cuts': LABEL_MICRO_CUTS,
    'semantic_emphasis': LABEL_SEMANTIC_EMPHASIS,
    'energy_response': LABEL_ENERGY_RESPONSE,
    'motion_bias': LABEL_MOTION_BIAS,
    'source_diversity': LABEL_SOURCE_DIVERSITY,
}
_VARIANT_RANDOMIZE_CHOICES = [
    (_VARIANT_CONTROL_LABELS[field], field)
    for field in fork_presets.CREATIVE_CONTROL_FIELDS
]


def _variant_apply_outputs(master_seed: int, resolution, audio_resolution) -> Tuple:
    """Project one visual + audio resolution onto the widgets Variant Lab is allowed to write.

    Ordered to match the `outputs` list: master seed, Variation Seed, the six sliders in
    `CREATIVE_CONTROL_FIELDS` order, the preset label, the three audio levels in
    `AUDIO_CONTROL_FIELDS` order, the report. The preset label is computed explicitly because
    programmatic slider writes do not fire the sliders' `.input()` handlers — without it the label
    would keep claiming whatever preset the base came from.

    [FORK] Digital-Union (Variant Lab Audio / E2 V1): there is **one** projection helper, so the
    widget output tuple has a single definition and the visual and audio halves cannot drift out of
    alignment with the `outputs` list. The report is the two resolutions' own `describe()` output
    joined — this function formats no text of its own, so each half keeps exactly one formatter.
    """
    recipe = resolution.recipe
    values = tuple(getattr(recipe, field)
                   for field in fork_presets.CREATIVE_CONTROL_FIELDS)
    audio_values = tuple(getattr(audio_resolution.recipe, field)
                         for field in fork_lab.AUDIO_CONTROL_FIELDS)
    return (master_seed, recipe.seed) + values + (
        fork_presets.matching_preset(values),
    ) + audio_values + (
        '\n'.join([resolution.describe(), audio_resolution.describe()]),
    )


def _on_generate_variant(variant_master_seed, variation_spread, variant_randomize,
                         range_cut_density_min, range_cut_density_max,
                         range_micro_cuts_min, range_micro_cuts_max,
                         range_semantic_emphasis_min, range_semantic_emphasis_max,
                         range_energy_response_min, range_energy_response_max,
                         range_motion_bias_min, range_motion_bias_max,
                         range_source_diversity_min, range_source_diversity_max,
                         variant_audio_randomize,
                         range_music_under_voice_min, range_music_under_voice_max,
                         range_sfx_amount_min, range_sfx_amount_max,
                         range_sfx_level_min, range_sfx_level_max,
                         cut_density, micro_cuts, semantic_emphasis,
                         energy_response, motion_bias, source_diversity,
                         music_under_voice, sfx_amount, sfx_level) -> Tuple:
    """Resolve and apply exactly one recipe from the live base and the lab configuration.

    An unusable master seed (0, empty, or anything `normalize_seed` refuses) is replaced by a fresh
    positive one **which is returned as the first output**, so it is on screen before it is used.
    That is the whole rule about hidden randomness: `fork_variation.random_seed()` is the only
    non-deterministic call here, it happens in the GUI rather than in the pure resolver, and its
    result is always surfaced. Everything after it is a pure function of visible values.

    Parameter names and order mirror the `inputs` list, because Gradio passes them positionally.
    """
    master_seed = fork_lab.normalize_master_seed(variant_master_seed)
    if master_seed <= 0:
        master_seed = fork_variation.random_seed()

    config = fork_lab.VariantLabConfig(
        master_seed=master_seed,
        spread=variation_spread,
        randomized=variant_randomize,
        ranges={
            'cut_density': (range_cut_density_min, range_cut_density_max),
            'micro_cuts': (range_micro_cuts_min, range_micro_cuts_max),
            'semantic_emphasis': (range_semantic_emphasis_min, range_semantic_emphasis_max),
            'energy_response': (range_energy_response_min, range_energy_response_max),
            'motion_bias': (range_motion_bias_min, range_motion_bias_max),
            'source_diversity': (range_source_diversity_min, range_source_diversity_max),
        },
    )
    base = {
        'cut_density': cut_density,
        'micro_cuts': micro_cuts,
        'semantic_emphasis': semantic_emphasis,
        'energy_response': energy_response,
        'motion_bias': motion_bias,
        'source_diversity': source_diversity,
    }

    # [FORK] Digital-Union (Variant Lab Audio / E2 V1): the audio half, resolved from the SAME
    # visible master seed and the SAME visible Spread — there is deliberately no second audio seed
    # and no second audio Spread widget. The default ticked selection is empty, so an existing C2
    # user who presses Generate gets their audio levels back unchanged.
    audio_config = fork_lab.AudioVariantConfig(
        randomized=variant_audio_randomize,
        ranges={
            'music_under_voice_percent': (range_music_under_voice_min,
                                          range_music_under_voice_max),
            'sfx_amount': (range_sfx_amount_min, range_sfx_amount_max),
            'sfx_level_percent': (range_sfx_level_min, range_sfx_level_max),
        },
    )
    # Each audio base goes through the normaliser that OWNS that control, never a shared 0..100 one:
    # `music_under_voice` falls back to 35 while both Smart Mix controls fall back to 50, so routing
    # all three through `creative.normalize_control` (fallback 50) would silently change the music
    # floor a malformed widget value resolves to. The resolver delegates to these same functions, so
    # the two cannot disagree.
    audio_base = {
        'music_under_voice_percent':
            fork_audio_mix.normalize_music_under_voice(music_under_voice),
        'sfx_amount':
            fork_smart_mix.normalize_control(sfx_amount, fork_smart_mix.DEFAULT_AMOUNT),
        'sfx_level_percent':
            fork_smart_mix.normalize_control(sfx_level,
                                             fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT),
    }
    return _variant_apply_outputs(
        master_seed,
        fork_lab.resolve(config, base),
        fork_lab.resolve_audio(master_seed, audio_config, variation_spread, audio_base),
    )


def _fresh_variant_master_seed(previous) -> int:
    """A positive master seed that is **guaranteed** to differ from the usable previous one.

    `random_seed()` draws from 1..999999, so it can legitimately return the value already in the
    box. "New Variant always mints a new master" is a product contract, and leaving it to a
    one-in-a-million draw makes it a probability rather than a guarantee — including in tests, which
    would then carry a rare random failure.

    So: draw once, and on a collision step deterministically to an adjacent seed. One draw, no
    retry loop — waiting on `SystemRandom` to disagree is exactly the unbounded behaviour this
    avoids. The step is expressed in terms of the drawn candidate rather than a repeated range
    literal, so it stays inside `random_seed()`'s own 1..999999 range by construction whatever that
    range later becomes. Randomness stays here in the GUI; the pure resolver never draws.
    """
    previous_master = fork_lab.normalize_master_seed(previous)
    candidate = fork_variation.random_seed()
    if previous_master > 0 and candidate == previous_master:
        # Both branches stay in range: the draw is >= 1, so stepping down is safe unless it is
        # exactly the low bound, in which case stepping up is.
        return candidate - 1 if candidate > 1 else candidate + 1
    return candidate


def _on_new_variant(variant_master_seed, *lab_and_base) -> Tuple:
    """Always mint a fresh visible master seed, then take the ordinary Generate path.

    The incoming master seed is deliberately discarded — that is what "new" means — but it is still
    read, because "new" also has to mean *different*. Everything after that is delegated, so there
    is exactly one resolver call site and no second implementation that could drift from it.
    """
    return _on_generate_variant(_fresh_variant_master_seed(variant_master_seed), *lab_and_base)


def process_video_guarded(audio_file: str,
                          voice_files: VideoFilesInput, voice_start_delay: float,
                          voice_min_gap: float, voice_avoid_drops: bool,
                          music_under_voice: int,
                          sfx_folder: str, sfx_roles, sfx_amount: int, sfx_level: int,
                          source_mode: str, source_folder: str,
                          source_recursive: bool, video_input: VideoFilesInput,
                          output_filename: str, processing_mode: str,
                          custom_fps: float, variation_seed: int,
                          cut_density: int, energy_response: int, motion_bias: int,
                          source_diversity: int, micro_cuts: int, semantic_emphasis: int,
                          session_state: dict,
                          source_state) -> Iterator[GuardedResult]:
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
    # `variation_seed`, `cut_density`, `energy_response`, `motion_bias`, `source_diversity`,
    # `micro_cuts` and `semantic_emphasis` are render-request inputs
    # like FPS or the encoder, NOT source identity: none of them is wired into the
    # source-confirmation handlers, so changing any of them cannot clear a confirmation, trigger a
    # scan or touch Media Library Preparation. They are still *live* inputs here for the same reason
    # the source controls are — the click must act on what is on screen.
    #
    # [FORK] Digital-Union (L0): the verification is timed, not changed. In local-folder mode it is
    # an authoritative filesystem re-scan of every confirmed source, so it is one of the costs that
    # grows with the library. Measuring wraps the existing call: the gate, the snapshot identity and
    # the allow/deny outcome are all untouched, and nothing is reused between renders.
    # [FORK] Digital-Union (Audio Layers V1 / D, R1-B; Smart Mix V1 / E): clear both read-outs
    # before the gate, so a refused render shows no placements either — and so no path through this
    # handler can leave the previous attempt's reports on screen.
    session_state[AUDIO_LAYERS_REPORT_KEY] = ''
    session_state[SMART_MIX_REPORT_KEY] = ''

    verification_started = time.perf_counter()
    decision = resolve_for_render(
        source_state,
        live_declaration(source_mode, source_folder, source_recursive, video_input),
    )
    verification_seconds = time.perf_counter() - verification_started
    if not decision.allowed:
        yield None, f"❌ {decision.message}", session_state, '', ''
        return

    # [FORK] Digital-Union (Creative Controls Core): the four raw widget values are collapsed into
    # one normalised profile here, at the render boundary, rather than being threaded onward as
    # loose scalars. Nothing below this line ever sees an untrusted widget value, and nothing here
    # can raise: `CreativeProfile` normalises every field on construction.
    creative = fork_creative.CreativeProfile.from_widgets(
        seed=variation_seed,
        cut_density=cut_density,
        energy_response=energy_response,
        motion_bias=motion_bias,
        source_diversity=source_diversity,
        micro_cuts=micro_cuts,
        semantic_emphasis=semantic_emphasis,
    )

    # [FORK] Digital-Union (Audio Layers V1 / D): the four raw audio-layer widget values are
    # collapsed into one normalised `AudioMixConfig` here, at the same render boundary the creative
    # profile uses, so nothing downstream ever sees an untrusted widget value. Nothing here can
    # raise: every field is normalised on construction, and seconds are clamped into a sane range
    # so an absurd typed value cannot become an enormous FFmpeg delay.
    #
    # Like the creative controls, these are render-request inputs and NOT video-source identity:
    # none of them is wired into a source handler, so changing any of them cannot clear a
    # confirmation, start a scan or touch Media Library Preparation.
    audio_mix = fork_audio_mix.AudioMixConfig(
        start_delay_seconds=voice_start_delay,
        min_gap_seconds=voice_min_gap,
        avoid_drops=voice_avoid_drops,
        music_under_voice_percent=music_under_voice,
    )

    # [FORK] Digital-Union (Smart Mix V1 / E): the three raw Smart Mix widget values become one
    # normalised `SmartMixConfig` at the same render boundary, for the same reason. The SFX library
    # ROOT stays a separate runtime path argument: it is a filesystem location the executor needs,
    # not creative state, and it deliberately belongs to neither `CreativeProfile` nor
    # `CreativeRecipe`. Like every other audio control these are render-request inputs and not video
    # source identity, so changing any of them cannot clear a confirmation or start a scan.
    smart_mix = fork_smart_mix.SmartMixConfig(
        enabled_roles=sfx_roles,
        amount=sfx_amount,
        sfx_level_percent=sfx_level,
    )

    # [FORK] Digital-Union (Audio Layers V1 / D, R1-B; Smart Mix V1 / E): `process_video` keeps its
    # existing 3-value streaming contract — video, status, session_state — and this handler projects
    # each yield onto the five Gradio outputs by appending the two read-outs that
    # `_process_video_impl` has recorded on `session_state`. Two extra outputs, no second placement
    # computation, no new state object, and still nothing but `queue.put` on the worker thread.
    for video, status, state in process_video(
        audio_file=audio_file,
        video_files=list(decision.paths),
        output_filename=output_filename,
        processing_mode=processing_mode,
        custom_fps=custom_fps,
        session_state=session_state,
        creative=creative,
        verification_seconds=verification_seconds,
        voice_files=voice_files,
        audio_mix=audio_mix,
        sfx_root=sfx_folder,
        smart_mix=smart_mix,
    ):
        yield (video, status, state,
               (state or {}).get(AUDIO_LAYERS_REPORT_KEY, ''),
               (state or {}).get(SMART_MIX_REPORT_KEY, ''))


# [FORK] Digital-Union (P V1 / P2): media library preparation.
#
# A deliberately separate workflow. It has its own `gr.State`, its own handlers and its own buttons,
# and it touches NONE of the Create Video machinery above: not `source_state`, not `source_outputs`,
# not `confirm_action`, not `process_btn`. Preparing a library can therefore never clear a render
# confirmation, and confirming sources can never invalidate a preparation scan.
#
# The division of labour mirrors the rest of the fork: every decision (what invalidates a scan, what
# may be analysed, what the report says) lives in `beatsync_fork.library_prep`, which is stdlib-only
# and tested without Gradio. This module supplies only the two runtime calls that module may not make
# itself - the folder scan and the Stage 5 classifier/analyzer.
#
# P2: preparation is media-neutral and therefore trackless. There is no audio input, no Stage 1-4
# pass and no edit style anywhere in here, because none of that reaches Stage-5 cache identity.


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


def _on_prep_batch_size_change(batch_size, state) -> Tuple:
    """Relabel the Analyze button and re-render the report. Deliberately NOT an invalidation.

    Batch size is execution policy, not classification identity: it decides how many of the already
    classified outstanding sources one click submits, and changes nothing about how any of them were
    classified. `fork_prep.set_batch_size` therefore keeps the recorded scan, unlike every handler
    above it, so a user may retune this between Scan and Analyze without paying for a re-scan.

    The report is re-rendered from that same scan because it quotes the batch size; leaving it alone
    let the screen say "Analyze next 50" above a report still claiming 100. That is presentation
    only - no folder scan, no classification, no identity probe.
    """
    return _prep_ui_updates(fork_prep.set_batch_size(state, batch_size))


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


def _prep_scan_impl(folder_path: str, recursive: bool, state,
                    event_callback=None, console_logger: StageConsoleLogger | None = None):
    """Classify the whole library once, media-neutrally.

    This is the expensive half of the P.1 rule: the Scan click pays for the full-library
    classification so the Analyze click can pass only the subset it identified.

    [FORK] Digital-Union (P2): trackless. No audio file, no `analyze_beats_auto`, no Stages 1-4 and
    no edit style - persisted Stage-5 semantics describe the media itself, so there is nothing about
    a song for a classification to depend on. The remaining cost is folder enumeration, source
    identity and the cache-record lookups, which is what the measured ~15-20 s track-profile
    component used to sit on top of.
    """
    from video_analysis import classify_library_sources

    # Re-apply the LIVE widget values first. A Textbox `change` event may not have fired if the user
    # typed a path and clicked Scan immediately - the same reason `_on_scan_click` does this.
    state = fork_prep.set_recursive(fork_prep.set_folder(state, folder_path), recursive)

    if not state.folder.strip():
        return fork_prep.record_failure(
            state, "No library folder selected. Enter a folder path and press Scan Library.")

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

    # 2. Resolve the runtime exactly as Stage 5 expects, then classify read-only.
    qwen_enabled, model_path = _resolve_prep_qwen_runtime()
    classification = classify_library_sources(
        ready_paths,
        enable_ai=qwen_enabled,
        qwen_model_path=model_path,
        event_callback=event_callback,
    )

    runtime = fork_prep.runtime_identity_from_classification(
        classification, qwen_enabled=qwen_enabled)

    scan = fork_prep.build_scan_result(
        folder=state.folder,
        recursive=state.recursive,
        runtime=runtime,
        classification_items=classification.get("classifications") or (),
        supported_count=len(ready_paths),
        folder_scan_seconds=folder_scan_seconds,
        classify_seconds=classification.get("classify_seconds") or 0.0,
        cache_identity_seconds=classification.get("cache_identity_seconds") or 0.0,
        cache_lookup_seconds=classification.get("cache_lookup_seconds") or 0.0,
    )
    return fork_prep.record_scan(state, scan)


def _prep_analyze_impl(state, live, batch_size=None, event_callback=None,
                       console_logger: StageConsoleLogger | None = None):
    """Analyze one bounded batch of the subset the recorded scan classified as needing work.

    `live` is the preparation controls as the widgets declare them at click time, and checking it
    comes **first** - before the runtime identity is recomputed and long before anything is
    analysed. Gradio delivers widget changes as separate queued events, so a user can retype the
    folder or toggle subfolders and click Analyze before the `change` handler has updated the state;
    without this guard the previous library's classification would be analysed while the screen
    declared something else. It is the same reason `process_video_guarded` takes the live source
    controls rather than trusting `gr.State` alone.

    `batch_size` is the live batch widget, and it is deliberately NOT part of `live`: it is not a
    classification input, so a value that disagrees with the recorded state is not a stale scan and
    must not be refused. It is read here rather than from the state for the same queued-event reason
    the folder is - the user may retune it and click immediately.

    [FORK] Digital-Union (P2): Stage 5 is called media-neutrally - no audio profile is forwarded,
    because none of it reaches the Qwen prompt, the Qwen request or the cache key any more.

    Persistence is entirely the existing analyzer's: this never calls `_analyze_single_video`,
    `_checkpoint_cache` or `_save_cache`, and the returned candidate library is discarded apart from
    the current-run statistics used for the summary.
    """
    from video_analysis import analyze_video_sources, classify_library_sources

    scan = state.scan
    refusal = fork_prep.declaration_refusal(state, live)
    if refusal is None:
        refusal = fork_prep.analyze_refusal(state, None)
    if refusal is not None:
        return fork_prep.record_failure(state, refusal, notice="Preparation run refused.")

    # Recompute the invocation-scoped identity once and compare it with the scan's. Passing an empty
    # source list reuses the one classifier seam to do exactly that work - backend availability,
    # backend token, config token - and classifies nothing, so no alternate identity code exists
    # here. No progress callback: this probe is not a phase the user needs to watch.
    qwen_enabled, model_path = _resolve_prep_qwen_runtime()
    probe = classify_library_sources(
        [], enable_ai=qwen_enabled, qwen_model_path=model_path, event_callback=None)
    runtime = fork_prep.runtime_identity_from_classification(probe, qwen_enabled=qwen_enabled)
    refusal = fork_prep.analyze_refusal(state, runtime)
    if refusal is not None:
        return fork_prep.record_failure(state, refusal, notice="Preparation run refused.")

    # The P.1 rule: only the classified subset, and only one bounded batch of it. For the measured
    # 902-source library the subset is 4 paths, not 902; for a cold 1107-source library it is 1107,
    # and the batch bound is what stops all of it reaching one shared Qwen worker whose response -
    # and therefore every checkpoint in it - arrives only after its whole job loop finishes.
    # The analyzer re-derives each selected source's own identity, which is correct.
    subset = list(scan.subset_for_analysis_batch(batch_size))
    try:
        result = analyze_video_sources(
            video_files=subset,
            use_gpu=GPU_AVAILABLE,
            enable_ai=qwen_enabled,
            qwen_model_path=model_path,
            event_callback=event_callback,
        )
    except Exception as exc:
        return fork_prep.record_failure(
            state, f"PREPARATION ANALYSIS FAILED\n{exc}", notice="Preparation run failed.")

    _stage5_summary(console_logger, result if isinstance(result, dict) else None)
    return fork_prep.record_analysis_complete(
        state, fork_prep.summarize_analysis_run(result, submitted=len(subset)))


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


def _on_prep_scan_click(folder_path: str, recursive: bool, state) -> Iterator[Tuple]:
    yield from _run_prep_in_worker(
        lambda event_callback, console_logger: _prep_scan_impl(
            folder_path, recursive, state,
            event_callback=event_callback, console_logger=console_logger),
        state,
    )


def _on_prep_analyze_click(folder_path: str, recursive: bool, batch_size,
                           state) -> Iterator[Tuple]:
    # The live preparation controls are inputs to the Analyze request, not just `gr.State` - a
    # queued `change` event must not be able to let a stale scan be analysed. Parameter names
    # mirror the widget names in `prep_analyze_btn.click(inputs=...)`, which Gradio supplies
    # positionally; a test asserts the two lists line up.
    #
    # `batch_size` is passed separately and is absent from `LivePrepDeclaration` on purpose: the
    # declaration describes the *classification* (folder, recursive), and a batch-size change must
    # never be read as "the scan no longer describes what the user is declaring".
    live = fork_prep.LivePrepDeclaration.from_widgets(folder_path, recursive)
    yield from _run_prep_in_worker(
        lambda event_callback, console_logger: _prep_analyze_impl(
            state, live, batch_size,
            event_callback=event_callback, console_logger=console_logger),
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

                # [FORK] Digital-Union (Audio Layers V1 / D): voice over music. Collapsed, and
                # directly under the main audio input because that is what it layers onto.
                #
                # Every widget here is a render-request input read at click time: none registers a
                # handler, none appears in `source_outputs` or `prep_outputs`, and none is part of
                # video-source identity — so changing any of them cannot clear a confirmation.
                with gr.Accordion(label=LABEL_AUDIO_LAYERS, open=False):
                    gr.Markdown(INFO_AUDIO_LAYERS)
                    # Order comes from `audio_mix.order_voice_paths` (the project's existing
                    # `input_manager.order_key`), NOT from this picker: the browser's multi-select
                    # order is whatever the OS dialog supplies and is not the user's click order.
                    voice_files = gr.File(
                        label=LABEL_VOICE_FILES,
                        file_count='multiple',
                        file_types=['.mp3', '.wav', '.flac'],
                        type='filepath',
                        elem_id='voice-files-input',
                    )
                    voice_start_delay = gr.Number(
                        label=LABEL_VOICE_START_DELAY,
                        value=fork_audio_mix.DEFAULT_START_DELAY_SECONDS,
                        minimum=fork_audio_mix.START_DELAY_MIN_SECONDS,
                        maximum=fork_audio_mix.START_DELAY_MAX_SECONDS,
                        info=INFO_VOICE_START_DELAY,
                        elem_id='voice-start-delay-input',
                    )
                    voice_min_gap = gr.Number(
                        label=LABEL_VOICE_MIN_GAP,
                        value=fork_audio_mix.DEFAULT_MIN_GAP_SECONDS,
                        minimum=fork_audio_mix.MIN_GAP_MIN_SECONDS,
                        maximum=fork_audio_mix.MIN_GAP_MAX_SECONDS,
                        info=INFO_VOICE_MIN_GAP,
                        elem_id='voice-min-gap-input',
                    )
                    voice_avoid_drops = gr.Checkbox(
                        label=LABEL_VOICE_AVOID_DROPS,
                        value=True,
                        info=INFO_VOICE_AVOID_DROPS,
                        elem_id='voice-avoid-drops-checkbox',
                    )
                    music_under_voice = gr.Slider(
                        minimum=fork_audio_mix.MUSIC_UNDER_VOICE_MIN,
                        maximum=fork_audio_mix.MUSIC_UNDER_VOICE_MAX,
                        step=1,
                        value=fork_audio_mix.DEFAULT_MUSIC_UNDER_VOICE_PERCENT,
                        label=LABEL_MUSIC_UNDER_VOICE,
                        info=INFO_MUSIC_UNDER_VOICE,
                        elem_id='music-under-voice-slider',
                    )
                    audio_layers_report = gr.Textbox(
                        label=LABEL_AUDIO_LAYERS_REPORT,
                        value='',
                        placeholder=PLACEHOLDER_AUDIO_LAYERS_REPORT,
                        lines=6,
                        max_lines=12,
                        interactive=False,
                        elem_id='audio-layers-report-box',
                    )

                # [FORK] Digital-Union (Smart Mix V1 / E): deterministic SFX accents, a collapsed
                # sibling of Audio Layers because it layers onto the same final master.
                #
                # Five actual components. Exactly like the Audio Layers block, every one of them is
                # a render-request input read at click time: none registers a handler, none appears
                # in `source_outputs` or `prep_outputs`, and none is part of video-source identity,
                # so changing the folder, the roles, Amount or Level cannot clear a confirmation,
                # start a scan or touch Media Library Preparation. There is deliberately no Scan
                # button — the library is validated by the Create Music Video preflight.
                with gr.Accordion(label=LABEL_SMART_MIX, open=False):
                    gr.Markdown(INFO_SMART_MIX)
                    sfx_folder = gr.Textbox(
                        label=LABEL_SFX_FOLDER,
                        placeholder=PLACEHOLDER_SFX_FOLDER,
                        info=INFO_SFX_FOLDER,
                        elem_id='sfx-folder-input',
                    )
                    # `(label, value)` choices, so the value Gradio returns IS the exact internal
                    # role name. Never a lowercase/replace heuristic over the display label: the
                    # planner keys on these strings, so a derived name would be a silent
                    # correctness hazard the moment a label is reworded.
                    sfx_roles = gr.CheckboxGroup(
                        choices=list(fork_smart_mix.ROLE_CHOICES),
                        value=[role for _label, role in fork_smart_mix.ROLE_CHOICES],
                        label=LABEL_SFX_ROLES,
                        info=INFO_SFX_ROLES,
                        elem_id='sfx-roles-group',
                    )
                    sfx_amount = gr.Slider(
                        minimum=fork_smart_mix.CONTROL_MIN,
                        maximum=fork_smart_mix.CONTROL_MAX,
                        step=1,
                        value=fork_smart_mix.DEFAULT_AMOUNT,
                        label=LABEL_SFX_AMOUNT,
                        info=INFO_SFX_AMOUNT,
                        elem_id='sfx-amount-slider',
                    )
                    sfx_level = gr.Slider(
                        minimum=fork_smart_mix.CONTROL_MIN,
                        maximum=fork_smart_mix.CONTROL_MAX,
                        step=1,
                        value=fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT,
                        label=LABEL_SFX_LEVEL,
                        info=INFO_SFX_LEVEL,
                        elem_id='sfx-level-slider',
                    )
                    smart_mix_report = gr.Textbox(
                        label=LABEL_SMART_MIX_REPORT,
                        value='',
                        placeholder=PLACEHOLDER_SMART_MIX_REPORT,
                        lines=6,
                        max_lines=14,
                        interactive=False,
                        elem_id='smart-mix-report-box',
                    )

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

                # [FORK] Digital-Union (Phase A + Creative Controls Core): creative direction.
                # Deliberately outside the Video Source group and never wired into `source_outputs`,
                # so changing any of these cannot invalidate a confirmed source set. All four are
                # render-request creative state: they re-plan, they never re-analyse.
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
                    # [FORK] Digital-Union (Creative Controls Extra PR3): the preset selector sits
                    # directly above the six sliders it writes, and below the seed — the layout is
                    # the first statement that a preset moves those six and nothing else. It is NOT
                    # a `process_btn` input: the sliders already are, and they stay the only thing
                    # the render request carries. `Custom` is the last choice because it is a state
                    # the selector reports, not a recipe a user picks.
                    creative_preset = gr.Radio(
                        choices=list(fork_presets.PRESET_NAMES),
                        value=fork_presets.BALANCED_PRESET,
                        label=LABEL_CREATIVE_PRESET,
                        info=INFO_CREATIVE_PRESET,
                        elem_id='creative-preset-radio',
                    )
                    # Randomize stays seed-only on purpose; these three have no randomizer and no
                    # reset. 50 is current BeatSync behaviour in every one of them, and the default
                    # is the fork module's constant rather than a literal repeated here.
                    cut_density = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_CUT_DENSITY,
                        info=INFO_CUT_DENSITY,
                        elem_id='cut-density-slider',
                    )
                    energy_response = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_ENERGY_RESPONSE,
                        info=INFO_ENERGY_RESPONSE,
                        elem_id='energy-response-slider',
                    )
                    motion_bias = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_MOTION_BIAS,
                        info=INFO_MOTION_BIAS,
                        elem_id='motion-bias-slider',
                    )
                    # [FORK] Digital-Union (Creative Controls Extra): two more render-request
                    # creative controls, wired exactly like the ones above — no handler, no reset,
                    # no randomizer, and absent from every source and preparation output list.
                    source_diversity = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_SOURCE_DIVERSITY,
                        info=INFO_SOURCE_DIVERSITY,
                        elem_id='source-diversity-slider',
                    )
                    micro_cuts = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_MICRO_CUTS,
                        info=INFO_MICRO_CUTS,
                        elem_id='micro-cuts-slider',
                    )
                    # [FORK] Digital-Union (Creative Controls Extra PR2): Stage-6 interpretation
                    # only. Wired exactly like the controls above - no handler, no reset, no
                    # randomizer, absent from every source and preparation output list.
                    semantic_emphasis = gr.Slider(
                        minimum=fork_creative.CONTROL_MIN,
                        maximum=fork_creative.CONTROL_MAX,
                        step=1,
                        value=fork_creative.DEFAULT_CONTROL,
                        label=LABEL_SEMANTIC_EMPHASIS,
                        info=INFO_SEMANTIC_EMPHASIS,
                        elem_id='semantic-emphasis-slider',
                    )

                    # [FORK] Digital-Union (Variant Lab V1 / C2): collapsed by default and placed
                    # below the six sliders it generates values for — the layout says what the lab
                    # does. Every widget in here is lab configuration read at click time only: none
                    # registers a handler, none is a `process_btn` input, and none appears in
                    # `source_outputs` or `prep_outputs`.
                    with gr.Accordion(label=LABEL_VARIANT_LAB, open=False):
                        gr.Markdown(INFO_VARIANT_LAB)
                        variant_master_seed = gr.Number(
                            label=LABEL_MASTER_SEED,
                            value=0,
                            precision=0,
                            minimum=0,
                            info=INFO_MASTER_SEED,
                            elem_id='variant-master-seed-input',
                        )
                        variation_spread = gr.Slider(
                            minimum=fork_lab.SPREAD_MIN,
                            maximum=fork_lab.SPREAD_MAX,
                            step=1,
                            value=fork_lab.DEFAULT_VARIATION_SPREAD,
                            label=LABEL_VARIATION_SPREAD,
                            info=INFO_VARIATION_SPREAD,
                            elem_id='variation-spread-slider',
                        )
                        # One CheckboxGroup rather than six Checkboxes: it is the question the
                        # feature asks, and Gradio 6.19 supports `(label, value)` choices, so the
                        # displayed text stays human while the returned value is the exact field
                        # name the resolver keys on. Default: all six.
                        variant_randomize = gr.CheckboxGroup(
                            choices=_VARIANT_RANDOMIZE_CHOICES,
                            value=list(fork_presets.CREATIVE_CONTROL_FIELDS),
                            label=LABEL_VARIANT_RANDOMIZE,
                            info=INFO_VARIANT_RANDOMIZE,
                            elem_id='variant-randomize-group',
                        )
                        gr.Markdown(INFO_VARIANT_RANGES)
                        # `gr.RangeSlider` does not exist in Gradio 6.19.0 (verified against the
                        # installed package), and a custom JS control is out of scope, so each
                        # control gets an explicit min/max pair. Declared one widget at a time, like
                        # every other control in this file, so the seam tests can read each one.
                        with gr.Row():
                            range_cut_density_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_CUT_DENSITY} min', elem_id='variant-range-cut-density-min')
                            range_cut_density_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_CUT_DENSITY} max', elem_id='variant-range-cut-density-max')
                        with gr.Row():
                            range_micro_cuts_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MICRO_CUTS} min', elem_id='variant-range-micro-cuts-min')
                            range_micro_cuts_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MICRO_CUTS} max', elem_id='variant-range-micro-cuts-max')
                        with gr.Row():
                            range_semantic_emphasis_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SEMANTIC_EMPHASIS} min', elem_id='variant-range-semantic-emphasis-min')
                            range_semantic_emphasis_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SEMANTIC_EMPHASIS} max', elem_id='variant-range-semantic-emphasis-max')
                        with gr.Row():
                            range_energy_response_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_ENERGY_RESPONSE} min', elem_id='variant-range-energy-response-min')
                            range_energy_response_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_ENERGY_RESPONSE} max', elem_id='variant-range-energy-response-max')
                        with gr.Row():
                            range_motion_bias_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MOTION_BIAS} min', elem_id='variant-range-motion-bias-min')
                            range_motion_bias_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MOTION_BIAS} max', elem_id='variant-range-motion-bias-max')
                        with gr.Row():
                            range_source_diversity_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SOURCE_DIVERSITY} min', elem_id='variant-range-source-diversity-min')
                            range_source_diversity_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SOURCE_DIVERSITY} max', elem_id='variant-range-source-diversity-max')
                        # [FORK] Digital-Union (Variant Lab Audio / E2 V1): the audio subsection of
                        # this same accordion — not a second Variant Lab and not a second workflow.
                        # Seven components: one CheckboxGroup and three min/max pairs. There is
                        # deliberately no audio master seed and no audio Spread; the two above drive
                        # both halves. Like every other lab widget these register NOTHING and are
                        # read at click time only.
                        gr.Markdown(INFO_VARIANT_AUDIO)
                        # `(label, value)` choices, so the returned value IS the exact field name
                        # the resolver keys on and the RNG stream is named by. Never derived from
                        # the display label by lowercasing or rewriting: these three strings are
                        # frozen stream names, so a reworded label must not be able to re-key them.
                        variant_audio_randomize = gr.CheckboxGroup(
                            choices=[
                                (LABEL_MUSIC_UNDER_VOICE, 'music_under_voice_percent'),
                                (LABEL_SFX_AMOUNT, 'sfx_amount'),
                                (LABEL_SFX_LEVEL, 'sfx_level_percent'),
                            ],
                            # Empty by default, unlike the visual side's all-six: opening an
                            # existing Variant Lab and pressing Generate must not move a mix level.
                            value=sorted(fork_lab.default_audio_randomized()),
                            label=LABEL_VARIANT_AUDIO_RANDOMIZE,
                            info=INFO_VARIANT_AUDIO_RANDOMIZE,
                            elem_id='variant-audio-randomize-group',
                        )
                        gr.Markdown(INFO_VARIANT_AUDIO_RANGES)
                        with gr.Row():
                            range_music_under_voice_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MUSIC_UNDER_VOICE} min', elem_id='variant-range-music-under-voice-min')
                            range_music_under_voice_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_MUSIC_UNDER_VOICE} max', elem_id='variant-range-music-under-voice-max')
                        with gr.Row():
                            range_sfx_amount_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SFX_AMOUNT} min', elem_id='variant-range-sfx-amount-min')
                            range_sfx_amount_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SFX_AMOUNT} max', elem_id='variant-range-sfx-amount-max')
                        with gr.Row():
                            range_sfx_level_min = gr.Number(value=fork_lab.DEFAULT_RANGE_LO, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SFX_LEVEL} min', elem_id='variant-range-sfx-level-min')
                            range_sfx_level_max = gr.Number(value=fork_lab.DEFAULT_RANGE_HI, precision=0, minimum=fork_creative.CONTROL_MIN, maximum=fork_creative.CONTROL_MAX, label=f'{LABEL_SFX_LEVEL} max', elem_id='variant-range-sfx-level-max')
                        with gr.Row():
                            generate_variant_btn = gr.Button(LABEL_GENERATE_VARIANT, variant='secondary', elem_id='generate-variant-button')
                            new_variant_btn = gr.Button(LABEL_NEW_VARIANT, elem_id='new-variant-button')
                        # "Last generated", never "Current": the user may edit the seed or any
                        # slider afterwards, and a read-out claiming to describe the render would
                        # then be lying. The seed and sliders above stay the execution truth.
                        # Seven lines: the visual resolution's five plus the audio resolution's two.
                        # Raised for truthful display only — if the box were left at five the audio
                        # half would be silently clipped out of a read-out that claims to describe
                        # the whole generated variant.
                        variant_report = gr.Textbox(
                            label=LABEL_VARIANT_REPORT,
                            value='',
                            placeholder=PLACEHOLDER_VARIANT_REPORT,
                            lines=7,
                            max_lines=7,
                            interactive=False,
                            elem_id='variant-report-box',
                        )

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
                    # No `maximum`: this bounds one analysis run, never the supported library size.
                    # The default is the preparation module's constant, not a literal repeated here.
                    prep_batch_size = gr.Number(
                        value=fork_prep.DEFAULT_ANALYZE_BATCH_SIZE,
                        precision=0,
                        minimum=1,
                        label=LABEL_PREP_BATCH_SIZE,
                        info=INFO_PREP_BATCH_SIZE,
                        elem_id='prep-batch-size',
                    )
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

        # [FORK] Digital-Union (Creative Controls Extra PR3): preset wiring, and the whole of it.
        #
        # The list is in `fork_presets.CREATIVE_CONTROL_FIELDS` order, not in widget-declaration
        # order, because Gradio matches `inputs`/`outputs` positionally against the handlers — so
        # this ordering and the field tuple are one contract, asserted by the seam tests.
        #
        # Note what is absent from both directions: `source_outputs`, `prep_outputs`,
        # `variation_seed` and `process_btn`. A preset moves these six sliders and the label above
        # them; it cannot clear a confirmation, disable Create Music Video, start a scan or touch
        # the seed.
        creative_control_sliders = [
            cut_density, micro_cuts, semantic_emphasis,
            energy_response, motion_bias, source_diversity,
        ]

        # Preset -> sliders. `.input()` rather than `.change()`: the slider handlers below write
        # this selector programmatically, and only `.change()` would fire for that, which is what
        # would turn these two registrations into an event loop.
        creative_preset.input(
            fn=_on_preset_input,
            inputs=[creative_preset],
            outputs=creative_control_sliders,
        )

        # Sliders -> preset. Registered explicitly per widget rather than in a loop or through
        # `gr.on`, so each binding is visible where the widget is: the seam tests assert, per
        # slider, that its one and only handler writes nothing but `creative_preset`. Every one
        # reads all six values, because the label describes the whole tuple.
        cut_density.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )
        micro_cuts.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )
        semantic_emphasis.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )
        energy_response.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )
        motion_bias.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )
        source_diversity.input(
            fn=_on_creative_control_input,
            inputs=creative_control_sliders,
            outputs=[creative_preset],
        )

        # [FORK] Digital-Union (Variant Lab V1 / C2): the lab's entire wiring — two button clicks.
        #
        # The lab's own config widgets register NOTHING: a master seed, a spread, a checkbox group
        # and twelve range boxes are read at click time, so the preset event graph above stays
        # exactly as PR3 left it and no creative slider gains a second handler.
        #
        # `inputs` is config first, then the six LIVE slider values, matching both handlers'
        # parameter order (Gradio passes positionally). The base is read here and nowhere else,
        # which is what makes "the current sliders are the base" true rather than aspirational.
        # [FORK] Digital-Union (Variant Lab Audio / E2 V1): the audio config widgets extend the
        # same list, and the three live audio widgets extend the base block — config first, then
        # every live base, so the documented "config then bases" shape still describes the whole
        # list. `_on_generate_variant`'s parameter order mirrors this exactly and a seam test pins
        # the alignment, because Gradio passes these positionally.
        variant_lab_audio_config = [
            variant_audio_randomize,
            range_music_under_voice_min, range_music_under_voice_max,
            range_sfx_amount_min, range_sfx_amount_max,
            range_sfx_level_min, range_sfx_level_max,
        ]
        variant_lab_audio_bases = [music_under_voice, sfx_amount, sfx_level]

        variant_lab_inputs = [
            variant_master_seed, variation_spread, variant_randomize,
            range_cut_density_min, range_cut_density_max,
            range_micro_cuts_min, range_micro_cuts_max,
            range_semantic_emphasis_min, range_semantic_emphasis_max,
            range_energy_response_min, range_energy_response_max,
            range_motion_bias_min, range_motion_bias_max,
            range_source_diversity_min, range_source_diversity_max,
        ] + variant_lab_audio_config + creative_control_sliders + variant_lab_audio_bases

        # Note what is absent from `outputs`: `source_outputs`, `prep_outputs`, `process_btn` and
        # every render setting. A variant moves the master seed, the Variation Seed, the six
        # sliders, the preset label, the three audio levels and the report — and starts nothing.
        #
        # The three audio levels are the ONLY audio widgets Variant Lab may write. `voice_files`,
        # `voice_start_delay`, `voice_min_gap`, `voice_avoid_drops`, `sfx_folder` and `sfx_roles`
        # stay zero-writer configuration, and the two report panels keep `process_btn.click` as
        # their single writer. Split seam tests pin that whole matrix rather than trusting this note.
        variant_lab_outputs = [
            variant_master_seed, variation_seed,
        ] + creative_control_sliders + [
            creative_preset,
        ] + variant_lab_audio_bases + [variant_report]

        generate_variant_btn.click(
            fn=_on_generate_variant,
            inputs=variant_lab_inputs,
            outputs=variant_lab_outputs,
        )
        new_variant_btn.click(
            fn=_on_new_variant,
            inputs=variant_lab_inputs,
            outputs=variant_lab_outputs,
        )

        # [FORK] Digital-Union (P V1): media library preparation wiring.
        #
        # `prep_outputs` is a wholly separate list from `source_outputs`: it contains no source
        # widget, no confirmation widget and not `process_btn`, so preparation can never enable,
        # disable or invalidate the Create Video gate. Conversely no source handler writes here.
        #
        # Every classification input - folder and recursive, which after P2 is all of them - clears
        # the recorded scan, which is what stops Analyze from acting on a classification that no
        # longer describes reality. Batch size is wired here too but is explicitly NOT one of them:
        # it changes how much of a valid classification one click consumes, not what it says.
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
        # Batch size is the one preparation control that does NOT clear the scan: it is execution
        # policy, not a classification input, so the recorded classification stays exactly as valid.
        # This handler only relabels the Analyze button.
        prep_batch_size.change(
            fn=_on_prep_batch_size_change,
            inputs=[prep_batch_size, prep_state],
            outputs=prep_outputs,
        )
        # Scan classifies the whole library and takes no batch size: how much of the result one
        # click later submits has no bearing on how any source is classified.
        prep_scan_btn.click(
            fn=_on_prep_scan_click,
            inputs=[prep_folder, prep_recursive, prep_state],
            outputs=prep_outputs,
            show_progress='hidden',
        )
        # Analyze takes the LIVE controls as well as the state, for the same reason
        # `process_btn` does: at click time the state can lag behind the widgets, and analysing a
        # scan the user is no longer declaring must be impossible, not merely unlikely. The live
        # batch size rides along so the bound applied is the one currently on screen.
        prep_analyze_btn.click(
            fn=_on_prep_analyze_click,
            inputs=[prep_folder, prep_recursive, prep_batch_size, prep_state],
            outputs=prep_outputs,
            show_progress='hidden',
        )

        # [FORK] Digital-Union: the live source controls are inputs to the render request, so the
        # gate validates what the widgets currently declare rather than possibly-stale gr.State.
        process_btn.click(
            fn=process_video_guarded,
            inputs=[
                audio_input,
                # [FORK] Digital-Union (Audio Layers V1 / D): live render-request inputs, grouped
                # with the audio they layer onto. Positionally aligned with
                # `process_video_guarded`'s parameters, which a seam test pins name-for-name.
                voice_files, voice_start_delay, voice_min_gap, voice_avoid_drops,
                music_under_voice,
                # [FORK] Digital-Union (Smart Mix V1 / E): the four Smart Mix *config* widgets,
                # same contract. The report is an output only and is deliberately absent here.
                sfx_folder, sfx_roles, sfx_amount, sfx_level,
                source_mode, source_folder, source_recursive, video_input,
                output_filename, processing_mode, custom_fps, variation_seed,
                cut_density, energy_response, motion_bias,
                source_diversity, micro_cuts, semantic_emphasis,
                session_state, source_state
            ],
            # [FORK] Digital-Union (Audio Layers V1 / D, R1-B; Smart Mix V1 / E): both read-outs are
            # written here and nowhere else. They are outputs only — never inputs, never source or
            # preparation state, and never consulted by the pipeline.
            outputs=[video_output, status_output, session_state,
                     audio_layers_report, smart_mix_report],
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
