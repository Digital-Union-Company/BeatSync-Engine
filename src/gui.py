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
    # [FORK] Digital-Union (AI Director V1): the one general root-path constant. The Director's
    # llama.cpp binary and GGUF live under it, resolved HERE in the GUI — the pure Director module
    # owns no filesystem path, and the Stage-5 worker is deliberately not imported for its paths.
    ROOT_DIR,
    USING_PORTABLE_PYTHON, USING_PORTABLE_CUDA, USING_CUPY_CTK, FFMPEG_FOUND
)

# Initialize environment
setup_environment()
# NOW import other modules (after CUDA environment is set)
import gradio as gr
import tempfile
import shutil
import datetime
# [FORK] Digital-Union (H1): `errno.EXDEV` tells a cross-volume promotion apart from a collision.
import errno
import multiprocessing
import queue
import re
import subprocess
import threading
import time
import socket
import uuid
from typing import Callable, Iterator, TypeAlias, Tuple, Dict, List

# Import FFmpeg processing module
from ffmpeg_processing import get_video_fps, FFMPEG_PATH, run_cancellable_media_command

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
# into the existing Variation Seed and six sliders — and, since E2 V1, a sibling audio recipe into
# the three existing audio level widgets — and nothing downstream of them learns that Variant Lab
# exists. `creative_recipe` is deliberately NOT imported here — the GUI only ever
# handles the resolution object, so it has no reason to name the recipe type.
from beatsync_fork import variant_lab as fork_lab
# [FORK] Digital-Union (Variant Lab C3 V1): multi-variant generation and comparison. A sibling of
# variant_lab that orchestrates its frozen resolvers — it derives one candidate master per index
# under the `batch` RNG domain and calls `resolve`/`resolve_audio` unchanged, so no C2 or E2 golden
# vector can move. Stdlib-only and Gradio-free like the rest of the package; this module only wires
# it to widgets. It renders nothing and knows nothing about rendering.
from beatsync_fork import variant_batch as fork_batch
# [FORK] Digital-Union (C3-R0 -> C3-R1B-b): rendering 2-4 compared candidates. The selection
# contract (MIN 2 / MAX 4, canonical ascending index),
# the candidate output identity and the batch summary all live in
# src/beatsync_fork/render_batch.py (stdlib-only, Gradio-free, renders nothing); this module
# supplies the request tag and performs every side effect. `variant_batch` stays generation and
# comparison state and knows nothing about rendering — its own guard enforces that.
from beatsync_fork import render_batch as fork_render_batch
# [FORK] Digital-Union (C3-R1A): the pure render-lifecycle/cancellation contract. Stdlib-only,
# Gradio-free, owns no Gradio object, Popen, filesystem path or media/cache identity -- see
# src/beatsync_fork/render_worker.py's own docstring.
from beatsync_fork.render_worker import (
    RenderCancelled,
    RenderLifecycle,
    RenderLifecycleState,
    RenderOutcomeKind,
)
# [FORK] Digital-Union (AI Director V1): a SECOND producer of the existing visual `CreativeRecipe`,
# not a new render pipeline. The schema, both prompts, the strict machine-response parser, the
# explanation policy, the `DirectorProposal` record and its read-out all live in
# src/beatsync_fork/director.py (stdlib-only, Gradio-free, no subprocess, no model path, no clock,
# no randomness). This module performs the one bounded text-only llama.cpp invocation, mints the
# Variation Seed through the existing `variation.random_seed()`, and writes the proposal into the
# visible execution controls only when the user presses Apply. It renders nothing.
from beatsync_fork import director as fork_director
# [FORK] Digital-Union (Freestyle V1): section-scoped creative modulation. The field registry, the
# section-type vocabulary, the sparse `SectionOverride` / `FreestyleDeclaration` records, the
# preset projection and the truthful summary all live in src/beatsync_fork/freestyle.py
# (stdlib-only, Gradio-free, media-free). This module only builds a declaration from the LIVE
# widgets at render-submission time and threads it down the existing render chain. It writes no
# global creative widget, and nothing downstream of Stage 6 has heard of it.
from beatsync_fork import freestyle as fork_freestyle
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
#: [FORK] Digital-Union (C3-R0): where the finished render records its **durable** output file.
#: Same lifecycle as the two report keys below — cleared at the start of every attempt, written
#: only after the move into `output/` succeeds.
#:
#: It exists because the value the handler *returns* is not the durable artifact in every mode: a
#: ProRes render moves the real `.mov` into `output/` and then hands back a session-temp
#: `_preview.mp4` for display. A batch that treated the returned path as its result would record a
#: temporary file as candidate output. Nothing parses the status prose and nothing reconstructs the
#: timestamp; the producer states the path it actually wrote.
LAST_OUTPUT_PATH_KEY = 'last_output_path'

#: [FORK] Digital-Union (C3-R1A): the typed terminal cause of one render attempt, a
#: ``beatsync_fork.render_worker.RenderOutcomeKind`` member -- never inferred from ``status_msg``.
#: Cleared to ``None`` at the start of every attempt (same lifecycle as ``LAST_OUTPUT_PATH_KEY``,
#: including before the gate, so a gate refusal never carries a stale prior outcome) and written only
#: by a boundary that actually PROVED the cause:
#:
#: [FORK] Digital-Union (C3-R1B-a): every reachable producer on the render path now names its class,
#: and ``SHARED_FATAL`` has real producers for the first time. The full matrix, and the one question
#: each row answers -- *can a candidate recipe change this outcome?*:
#:
#: * ``SUCCESS``         -- exactly once, immediately after the durable promotion succeeded.
#: * ``CANCELLED``       -- only from a caught ``RenderCancelled``.
#: * ``SHARED_FATAL``    -- proven identical for every remaining candidate, because nothing the
#:                          candidate resolves is an input to the outcome:
#:                            - the live source-gate refusal (gate inputs are all batch-frozen);
#:                            - the six primary audio/video selection failures;
#:                            - the voice preflight (``voice_files`` is frozen; no ``AudioRecipe``
#:                              field changes whether it runs or what it validates);
#:                            - ``AudioMixPlanError`` (voice placement reads only frozen config, and
#:                              is unreachable with an empty voice selection);
#:                            - an ``errno.EXDEV`` durable promotion (``session_dir`` and
#:                              ``output/`` are process-global, so the volume pair cannot change).
#: * ``CANDIDATE_LOCAL`` -- proven NOT to force every remaining candidate to fail:
#:                            - the early output-path collision and a ``FileExistsError`` promotion
#:                              (the destination name carries the candidate index and master, and
#:                              `RenderBatchRequest.__post_init__` asserts stems are distinct);
#:                            - the SFX preflight and ``SmartMixStructureError``, which run only
#:                              inside the ``smart_mix_active`` branch -- gated on
#:                              ``sfx_amount > 0``, a per-candidate ``AudioRecipe`` value, so a
#:                              candidate resolving 0 never performs the failing operation.
#: * ``UNKNOWN_FATAL``   -- fail closed, wherever the cause is not proven: ``AudioMixExecutionError``
#:                          (FFmpeg/disk/driver are indistinguishable here), the defensive
#:                          music-duration fallback probe, any other promotion ``OSError``, a
#:                          Stage 1-3/4 failure, extraction and encode failures, ``MemoryError`` and
#:                          the generic ``except Exception``.
#:
#: **Classification is by exception TYPE and by batch-frozen values, never by reading a message.**
#: A plain early return may still leave it ``None``, and every consumer reads that conservatively --
#: ``RenderCandidateOutcome.__post_init__`` derives ``UNKNOWN_FATAL`` and both mutex-owning wrappers
#: derive ``FAILED``.
#:
#: **R1B-a records; it does not act.** The batch still stops after *every* non-success candidate,
#: exactly as before. Continue-after-``CANDIDATE_LOCAL`` and rendering more than two candidates are
#: C3-R1B-b and are deliberately not implemented here. Pure diagnostics for the batch/UI layer --
#: nothing inside the pipeline reads it back.
RENDER_OUTCOME_KEY = 'render_outcome_kind'

AUDIO_LAYERS_REPORT_KEY = 'audio_layers_report'

#: [FORK] Digital-Union (Smart Mix V1 / E): the same bookkeeping seam as the Audio Layers report —
#: the worker thread must never touch a Gradio component, so the text rides on `session_state` and
#: the generator projects it onto the widget. Pure diagnostics: nothing downstream reads it.
SMART_MIX_REPORT_KEY = 'smart_mix_report'

# [FORK] Digital-Union (AI Director V1): the Director's runtime assets.
#
# The SAME installed GGUF Stage 5 uses, invoked **text-only** — no `mmproj` is loaded and no image
# argument is passed, because the Director V1 is media-blind by design. Stage 5's worker is
# deliberately NOT reused: it exists to batch frames through a persistent `llama-server` and to
# write a semantic response file, which is a different contract from one bounded 320-token JSON
# answer. Reusing it would have meant teaching a media-semantics worker about creative intent —
# exactly the leak `.claude/rules/stage5-worker.md` forbids.
#
# Resolved from the one general `ROOT_DIR` constant rather than from `video_analysis` or the
# worker's own module constants, so obtaining a path costs no import of either: the Director has no
# business reaching into Stage 5, in any direction. No environment override, for the reason the
# Stage-5 recovery constants are hard-coded too — a result-affecting knob belongs under contract.
DIRECTOR_LLAMA_DIR = os.path.join(ROOT_DIR, 'bin', 'llama-bin-win-vulkan-x64')
#: **`llama-completion.exe`, not `llama-cli.exe`, and that is measured rather than preferred.** On
#: the installed llama.cpp build (`b9842-6f4f53f2b`) `llama-cli` is the interactive chat front end:
#: it *rejects* `-no-cnv` outright ("--no-conversation is not supported by llama-cli / please use
#: llama-completion instead"), ignores `--no-display-prompt`, and prints its banner, its command
#: list, the echoed prompt and a timings line **into stdout** alongside the answer. Parsing that
#: would mean fishing a `{...}` out of tool prose with a regex, which is exactly what the Director's
#: strict parser exists to refuse. `llama-completion.exe` ships in the same `bin` layout, is the
#: binary llama-cli itself names, takes every argument below, and emits the JSON object alone on
#: stdout with the banner, the logs and the timings on stderr. Same build, same model asset, same
#: one-shot process — only a correctly-chosen entry point.
DIRECTOR_LLAMA_EXE = os.path.join(DIRECTOR_LLAMA_DIR, 'llama-completion.exe')
DIRECTOR_MODEL = os.path.join(ROOT_DIR, 'bin', 'models', 'Qwen3VL-2B-Instruct-Q8_0.gguf')

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


def _promote_output_no_replace(temp_output: str, output_path: str):
    """Promote a finished render into `output/`, and **never** replace what is already there.

    [FORK] Digital-Union (H1): this one OS call is what makes a render durable, and it is the whole
    no-overwrite guarantee. It never raises, and on no path does it remove anything.

    [FORK] Digital-Union (C3-R1B-a): returns ``(message, outcome_kind)`` instead of a bare string --
    ``('', None)`` on success, otherwise the **unchanged** user-facing text plus the typed cause. The
    three failure branches were already structurally separate here, so this adds no logic and no new
    branch; it only stops the caller having to recover the cause by reading the prose:

        destination occupied (FileExistsError)  ->  CANDIDATE_LOCAL
        cross-volume        (errno.EXDEV)       ->  SHARED_FATAL
        anything else       (OSError)           ->  UNKNOWN_FATAL

    ``CANDIDATE_LOCAL`` for the collision is sound because the destination name carries the
    candidate index and master, and no other selected candidate can compute it. ``SHARED_FATAL`` for
    EXDEV is sound because `session_dir` and `get_output_dir()` are process-global, so every
    remaining candidate promotes between the identical pair of volumes.

    **Why not `shutil.move` + `os.path.exists`.** The previous promotion was `shutil.move`, which
    silently replaced an existing destination — measured on the supported Windows environment: the
    user's old bytes were simply gone. C3-R0 guarded it with an `os.path.exists` check immediately
    beforehand, which helps but is a TOCTOU pair: another process can create the path in the window
    between the check and the move, and the move then destroys it anyway.

    **Why `os.rename`.** The check *is* the operation, so there is no window. Measured on Windows:
    renaming onto an existing name raises `FileExistsError` (`winerror 183`) with the destination
    bytes unchanged and the source still on disk; renaming onto a free name promotes cleanly. This
    is deliberately Windows-specific behaviour — POSIX `rename(2)` replaces silently — and it is
    only sound as a guarantee because this app is Windows-only by construction
    (`.claude/rules/platform-and-packaging.md`). The portable test suite therefore stubs
    `os.rename` rather than asserting the real platform's semantics.

    **Every failure is fail-closed.** The destination is never deleted to make room and the new
    render is never discarded, so a collision leaves the user holding *both* files. There is no
    copy fallback for a cross-volume destination on purpose: a copy is not an atomic promotion, and
    an interrupted one would leave a partial video sitting at the final path — which is worse than
    refusing, because it looks like a finished render.
    """
    try:
        os.rename(temp_output, output_path)
    except FileExistsError:
        return (f"❌ Output already exists and was preserved: {output_path}\n"
                f"Nothing was overwritten. The new render is kept at: {temp_output}",
                RenderOutcomeKind.CANDIDATE_LOCAL)
    except OSError as exc:
        if getattr(exc, 'errno', None) == errno.EXDEV:
            return (f"❌ Durable promotion failed: the render folder and the output folder are on "
                    f"different volumes, so the move cannot be atomic and was not attempted by "
                    f"copying. The destination was not replaced: {output_path}\n"
                    f"The new render is kept at: {temp_output}",
                    RenderOutcomeKind.SHARED_FATAL)
        return (f"❌ Durable promotion failed ({exc}). The destination was not replaced: "
                f"{output_path}\nThe new render is kept at: {temp_output}",
                RenderOutcomeKind.UNKNOWN_FATAL)
    return '', None


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
                       smart_mix: fork_smart_mix.SmartMixConfig | None = None,
                       # [FORK] Digital-Union (Freestyle V1): appended LAST with a default, so every
                       # existing caller — production and test — stays valid and no positional
                       # argument moved. `None` means no section rules, i.e. today's render.
                       freestyle: object | None = None,
                       # [FORK] Digital-Union (C3-R1A): appended LAST with a default, same reasoning.
                       # `None` preserves today's exact behaviour -- no boundary check anywhere
                       # below ever fires, and RENDER_OUTCOME_KEY is still written truthfully.
                       lifecycle: "RenderLifecycle | None" = None
                       ) -> StatusResult:
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
    # [FORK] Digital-Union (H1): the durable-output key joins them, with the same lifecycle. The
    # gate core already clears it before the gate — that clear must stay, because a gate refusal
    # never reaches this function — but "empty unless a promotion succeeded" is a property of
    # *this* function and is now guaranteed here rather than inherited from a caller. Every
    # collision, promotion failure and early return below therefore leaves it empty by
    # construction instead of by remembering to.
    session_state[LAST_OUTPUT_PATH_KEY] = ''
    # [FORK] Digital-Union (C3-R1A): the typed terminal cause, same clear-every-attempt lifecycle
    # as the key above. Written below only where the cause is proven -- SUCCESS after promotion,
    # CANCELLED from a caught RenderCancelled, and -- since C3-R1B-a -- an explicitly named class at
    # every other reachable producer below, including the six shared primary-input returns, both
    # preflights, the output collision and the three promotion branches. A producer that still
    # proves nothing leaves it None on purpose; see RENDER_OUTCOME_KEY's own comment for the full
    # matrix and for why None stays the conservative answer.
    session_state[RENDER_OUTCOME_KEY] = None
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

        # [FORK] Digital-Union (C3-R1B-a): the six primary-input failures below are all
        # SHARED_FATAL, and the proof is the same for each of them: `audio_file` and `video_files`
        # are frozen for the whole C3 batch (the batch handler passes its own submitted arguments,
        # and the video paths come from the one shared source gate), while a candidate contributes
        # only its output stem, seven creative values and three audio levels. None of those is an
        # input to "can this file be reached", so every remaining candidate fails identically.
        # The messages are unchanged.

        # Handle audio by referencing the selected file path directly.
        if audio_file:
            if audio_file != session_state.get('original_audio_path'):
                local_audio_path = _as_existing_source_path(audio_file)
                if local_audio_path:
                    session_state['local_audio_path'] = local_audio_path
                    session_state['original_audio_path'] = audio_file
                else:
                    session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
                    return None, '❌ Error: Could not access audio file', session_state
            else:
                local_audio_path = session_state.get('local_audio_path')
        else:
            session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
            return None, '❌ Error: No audio file selected', session_state

        # Handle videos by referencing selected file paths directly.
        if video_files:
            if video_files != session_state.get('original_video_paths'):
                local_video_paths = _as_existing_source_paths(video_files)
                if local_video_paths:
                    session_state['local_video_paths'] = local_video_paths
                    session_state['original_video_paths'] = video_files
                else:
                    session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
                    return None, '❌ Error: Could not access video files', session_state
            else:
                local_video_paths = session_state.get('local_video_paths')
        else:
            session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
            return None, '❌ Error: No video files selected', session_state

        # Verify files exist
        if not local_audio_path or not os.path.exists(local_audio_path):
             session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
             return None, f"❌ Error: Audio file is missing or inaccessible.", session_state
        if not local_video_paths or not all(p and os.path.exists(p) for p in local_video_paths):
             session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
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
                # [FORK] Digital-Union (C3-R1B-a): SHARED_FATAL, proven. The gate is a bare
                # `if voice_files:` with no candidate condition, `voice_files` is frozen for the
                # whole batch, and the call takes that selection and nothing else -- so every
                # candidate validates the identical file list and fails identically. (The
                # `music_under_voice_percent` a candidate does vary never reaches this call.)
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
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
                # [FORK] Digital-Union (C3-R1B-a): CANDIDATE_LOCAL, and this is the row that forced
                # the local-cause / batch-outcome split. The SFX root and the enabled roles are
                # batch-frozen, so it is tempting to call this shared -- but REACHABILITY is not.
                # `smart_mix_active` above requires `smart_mix.plans_anything`, i.e.
                # `sfx_amount > 0`, and `sfx_amount` is per-candidate `AudioRecipe` state. A
                # candidate resolving 0 never calls `prepare_sfx_inputs` at all, so a broken library
                # does NOT prove every remaining candidate must fail. Measured on the real
                # resolvers: root master 92, spread 100, base 50, full range, 4 candidates ->
                # amounts 87 / 87 / 0 / 79, so candidate 3 renders fine with the library broken.
                #
                # R1B-a nevertheless STILL STOPS here. This value is foundation for R1B-b only.
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.CANDIDATE_LOCAL
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

        # [FORK] Digital-Union (H1): the universal early collision check, BEFORE any analysis.
        #
        # Unconditional, and that is the point: C3-R0 made this batch-only behind a flag, which
        # left ordinary Create Music Video able to destroy an existing output. There is no GUI
        # caller that wants destructive replacement, so there is no flag — see
        # `_promote_output_no_replace`, which is the authority this only anticipates.
        #
        # The name above is distinct only per second per Variation Seed, so a collision is real:
        # C3 guarantees candidate *master* uniqueness but says nothing about `CreativeRecipe.seed`,
        # two candidates of one batch really can compute the same `_seedNNN`, and an earlier app
        # run or a manual copy can occupy the path for a single render. Refusing here costs
        # nothing; refusing after Stage 5 would waste the whole run.
        if os.path.exists(output_path):
            # [FORK] Digital-Union (C3-R1B-a): CANDIDATE_LOCAL. `output_path` is built from the
            # candidate's own stem -- request tag + `_cNN_m<master>` -- and
            # `RenderBatchRequest.__post_init__` asserts the stems in one batch are distinct, so no
            # other selected candidate can compute this name. The collision is a fact about one
            # candidate's destination, not about the batch. Still stops, as every R1B-a class does.
            session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.CANDIDATE_LOCAL
            return None, (f"❌ Output already exists and was preserved: {output_path}\n"
                          f"Nothing was rendered. Rename the output, or move the existing file, "
                          f"and run again."), session_state

        selected_beats, beat_info = analyze_beats_auto(
            local_audio_path,
            use_gpu=use_gpu,
            video_files=local_video_paths,
            progress_callback=progress_callback,
            console_callback=lambda stage, message: console_logger.stage_line(stage, message) if console_logger else None,
            event_callback=event_callback,
            creative=creative.as_dict(),
            # [FORK] Digital-Union (Freestyle V1): the frozen section-rule declaration for this
            # render. Stages 1-3 ignore it; Stage 4 resolves per-section Cut Density from it after
            # Stage 3 exists, and it rides `beat_info` to Stage 6. It reaches no cache input.
            freestyle=freestyle,
            lifecycle=lifecycle,
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
                # [FORK] Digital-Union (C3-R1B-a): CANDIDATE_LOCAL, for the same reachability reason
                # as the SFX preflight -- this sits inside the identical `smart_mix_active` branch.
                # The `beat_info` structure it reads IS shared, and that is deliberately not enough:
                # shared *data* does not make a shared *failure* when a candidate resolving
                # `sfx_amount == 0` never performs the failing operation. `smart_mix.py` is
                # untouched; only this mapping is new. Still stops.
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.CANDIDATE_LOCAL
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
            # [FORK] Digital-Union (C3-R1B-a): the defensive music-duration fallback, hoisted out of
            # the `build_mixed_master(...)` argument list for exactly one reason -- an exception
            # raised inside an argument expression cannot be caught separately from the call it
            # feeds, so the probe's cause was previously indistinguishable from a mixdown failure.
            #
            # Semantically identical to the `float(...) or probe_duration(...)` it replaces:
            # `if not music_duration` reproduces the `or` short-circuit exactly, and the call takes
            # no `lifecycle` here, just as it never did.
            #
            # The fallback is **unreachable in current production**: `analyze_beats_auto` guards
            # `y.size == 0` and then sets `audio_duration = len(y) / sr`, which is strictly
            # positive, and the L2 cache can only store a value from such a run. It is classified
            # UNKNOWN_FATAL as a frozen product decision -- conservative and forward-safe, so a
            # future change that made this reachable gets a fresh review rather than inheriting a
            # stale conditional.
            music_duration = float(beat_info.get('audio_duration') or 0.0)
            if not music_duration:
                try:
                    music_duration = audio_mixdown.probe_duration(local_audio_path)
                except audio_mixdown.AudioProbeError as exc:
                    session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.UNKNOWN_FATAL
                    return None, f'❌ Audio Layers: {exc}', session_state
            try:
                mixed_master_path, audio_plan = audio_mixdown.build_mixed_master(
                    music_path=local_audio_path,
                    music_duration=music_duration,
                    beat_times=beat_times,
                    sections=fork_audio_mix.project_sections(beat_info.get('sections')),
                    voices=prepared_voices,
                    config=audio_mix or fork_audio_mix.AudioMixConfig(),
                    session_dir=session_dir,
                    sfx_placements=sfx_placements,
                    sfx_level_percent=smart_mix.sfx_level_percent,
                    lifecycle=lifecycle,
                )
            # Deliberately before any clip extraction: the user asked for voice and/or SFX, so a
            # silent fallback to the original music would render a plausible but wrong video.
            #
            # [FORK] Digital-Union (C3-R1B-a): two clauses, ordered, split by TYPE and never by
            # message. R1A mapped every `AudioMixError` here to CANDIDATE_LOCAL on the grounds that
            # "a different candidate's music-under-voice / SFX amount / level could succeed on these
            # same files". That was wrong for both causes that actually reach this point:
            #
            #   AudioMixPlanError       voice placement reads only avoid_drops / start_delay_seconds
            #                           / min_gap_seconds -- all batch-frozen -- and is unreachable
            #                           with an empty voice selection. `music_under_voice_percent`,
            #                           the one value a candidate varies, touches only the duck
            #                           floor, AFTER placement has already succeeded. So no
            #                           candidate recipe can rescue it: SHARED_FATAL.
            #
            #   anything else           an FFmpeg timeout, a non-zero return, a missing or empty
            #                           master, a failed verification probe or a duration drift are
            #                           indistinguishable here from a bad binary, a full disk or a
            #                           driver fault, and this repository has no structured FFmpeg
            #                           diagnostic classification to tell them apart. Fail closed:
            #                           UNKNOWN_FATAL. This clause also catches a bare
            #                           AudioProbeError or AudioMixInputError, which cannot reach
            #                           here today, for the same fail-closed reason.
            except audio_mixdown.AudioMixPlanError as exc:
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
                return None, f'❌ Audio Layers: {exc}', session_state
            except audio_mixdown.AudioMixError as exc:
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.UNKNOWN_FATAL
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

        # [FORK] Digital-Union (C3-R1A): the safe boundary before Stage 6 extraction/rendering
        # begins.
        if lifecycle is not None:
            lifecycle.raise_if_cancelled()

        # Create video
        result_path = create_music_video(
            render_audio_path, local_video_paths, selected_beats,
            output_file=temp_output, max_workers=parallel_workers,
            beat_info=beat_info, lossless_mode=is_prores,
            use_gpu=use_gpu, gpu_encoder=gpu_encoder, fps=output_fps,
            event_callback=event_callback, lifecycle=lifecycle
        )

        # [FORK] Digital-Union (H1): the ONE durable promotion, and it is a single no-replace OS
        # operation rather than a check followed by a move. The early check above ran before Stage
        # 1 and a render takes minutes, so the path can be occupied by now — by another process, by
        # an earlier candidate of this same batch, or by the user. `os.rename` decides and acts
        # atomically, so no window remains for anything to appear in; it is deliberately NOT
        # preceded by another `os.path.exists`, which would only re-open the race it closes.
        promotion_error, promotion_kind = _promote_output_no_replace(result_path, output_path)
        if promotion_error:
            # Fail closed. Both files survive, the durable-output key stays empty (cleared at the
            # top of this function, and again by the gate core before the gate), and the message
            # names both paths — a promotion failure must never read as a finished render.
            # [FORK] Digital-Union (C3-R1B-a): the helper's three already-separate branches now
            # carry their own cause, so this no longer collapses all of them into UNKNOWN_FATAL --
            # CANDIDATE_LOCAL for an occupied candidate-unique destination, SHARED_FATAL for a
            # cross-volume pair that cannot differ between candidates, UNKNOWN_FATAL otherwise. The
            # `or UNKNOWN_FATAL` keeps this fail-closed if a future branch forgets to name one.
            session_state[RENDER_OUTCOME_KEY] = (
                promotion_kind or RenderOutcomeKind.UNKNOWN_FATAL)
            return None, promotion_error, session_state
        # [FORK] Digital-Union (C3-R0): the durable artifact is now on disk, so record it. Set
        # only here — after the promotion succeeded — and never from the returned display path,
        # which for ProRes is a session-temp preview rather than the real `.mov`.
        session_state[LAST_OUTPUT_PATH_KEY] = output_path
        # [FORK] Digital-Union (C3-R1A): durable promotion is the one and only SUCCESS commit
        # point -- nothing after this line (preview generation included) may ever downgrade it.
        session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS

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
            # [FORK] Digital-Union (C3-R1A / R2): the durable .mov above is already SUCCESS and
            # STAYS so regardless of anything below. Cancellation stops this post-commit
            # convenience work; it never changes the render's terminal result, and nothing here
            # touches RENDER_OUTCOME_KEY. Three explicit branches, because they are three genuinely
            # different situations:
            #
            #   lifecycle is None          today's exact blocking call, byte-for-byte, including
            #                              its existing TimeoutExpired behaviour. Every pre-R1A
            #                              caller and the headless path take this.
            #   already cancelled          never START a child at all. Cheaper than starting one
            #                              and terminating it on the first poll, and it is the
            #                              common case when Cancel landed during the render.
            #   cancelled WHILE running    R2 fixed this: R1A only handled the branch above, so an
            #                              already-running preview kept FFmpeg alive for up to 180s
            #                              while the UI claimed cancellation. The cancellable runner
            #                              terminates, graces, kills if needed and REAPS the child,
            #                              then raises RenderCancelled -- which is caught HERE and
            #                              goes no further, because a cancelled preview is not a
            #                              cancelled render.
            #
            # A partial preview is simply never selected: `preview_path` falls back to the durable
            # `output_path`. It is deliberately not deleted -- `session_dir` already legitimately
            # retains non-promoted artifacts (a cross-volume promotion failure keeps the whole
            # render there and names it in the message), and adding a destructive operation to this
            # function to tidy session scratch is not worth the surface.
            #
            # A genuine `TimeoutExpired` is deliberately NOT caught on either path: a stuck encode
            # is a different fact from a user pressing Stop, and its existing behaviour is unchanged.
            if lifecycle is None:
                subprocess.run(preview_cmd, capture_output=True, text=True, timeout=180)
            elif lifecycle.cancel_requested():
                preview_path = output_path
            else:
                try:
                    run_cancellable_media_command(preview_cmd, 180, lifecycle=lifecycle)
                except RenderCancelled:
                    preview_path = output_path
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

        # [FORK] Digital-Union (Freestyle V1): the section rules this render actually used, read
        # back off the bus the pipeline just returned rather than re-derived from the request.
        # Duck-typed, and deliberately so: this function is AST-extracted and executed by a frozen
        # seam suite against a synthesised namespace, so naming a fork module or calling a
        # module-level helper here would break a test this feature must preserve unchanged.
        # `None` when Freestyle had no effect, so a today-style render's panel is byte-identical.
        resolved_freestyle = (beat_info or {}).get('freestyle')
        freestyle_report = (
            resolved_freestyle.describe()
            if getattr(resolved_freestyle, 'is_active', None) and resolved_freestyle.is_active()
            else None)

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
            # [FORK] Digital-Union (Freestyle V1): numeric execution truth, not the preset labels
            # the user picked — those were UI input and are not stored. No new stage, no ETA.
            freestyle_text=freestyle_report,
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

    except RenderCancelled:
        # [FORK] Digital-Union (C3-R1A): never collapse into "❌ Error: ...". The typed cause rides
        # on RENDER_OUTCOME_KEY so process_video's worker can publish CANCELLED without parsing
        # this string; the string itself stays truthful on its own.
        session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.CANCELLED
        return None, STATUS_RENDER_CANCELLED, session_state
    except Exception as e:
        # [FORK] Digital-Union (C3-R1A): conservative by construction -- this generic handler
        # cannot prove whether the cause is candidate-local or shared, so it fails closed rather
        # than guessing. Only a path above that already proved a more specific cause may write a
        # different value.
        if session_state.get(RENDER_OUTCOME_KEY) is None:
            session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.UNKNOWN_FATAL
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
                 smart_mix: fork_smart_mix.SmartMixConfig | None = None,
                 # [FORK] Digital-Union (Freestyle V1): appended last with a default. Freestyle adds
                 # render intent only — this function's worker thread, its synchronous join on
                 # generator close, the render mutex and the concurrency group are all untouched.
                 freestyle: object | None = None,
                 # [FORK] Digital-Union (C3-R1A): appended last with a default, same reasoning.
                 # `None` preserves today's exact behaviour.
                 lifecycle: "RenderLifecycle | None" = None
                 ) -> Iterator[StatusResult]:
    """Run the pipeline in a worker thread, streaming structured progress to the UI.

    [FORK] Digital-Union: the queue now carries :class:`ProgressEvent` objects instead of status
    sentences, and stage identity comes from ``event.stage`` rather than a regex over prose. The
    architecture is otherwise the one that was already here — worker thread + ``queue.Queue`` +
    generator — because Gradio components must only be touched from the generator, never from the
    worker thread.

    Legacy string statuses are still accepted on the same queue as a compatibility fallback, so a
    caller that only supplies ``progress_callback`` keeps working.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` is constructed and installed into the active-render
    slot by the caller (`process_video_guarded` / `render_selected_variants_guarded`) -- this
    function only threads it down to `_process_video_impl` and reads it for nothing else. This
    generator's own worker-thread-join lifetime and abandonment handling are completely unchanged;
    cancellation changes *how fast* the worker reaches a terminal state, never *whether* the
    finalizer below waits for it.
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
                    freestyle=freestyle,
                    lifecycle=lifecycle,
                )
        except RenderCancelled:
            # [FORK] Digital-Union (C3-R1A): defensive only -- _process_video_impl already catches
            # RenderCancelled internally and returns a normal tuple with RENDER_OUTCOME_KEY set, so
            # this clause should never actually fire in practice. It exists so that if some future
            # call path ever raised past that boundary, it still could not be stringified into
            # "\u274c Error: ...".
            if lifecycle is not None and session_state.get(RENDER_OUTCOME_KEY) is None:
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.CANCELLED
            console_logger.line("Cancelled")
            result = None, STATUS_RENDER_CANCELLED, session_state
        except Exception as e:
            console_logger.line(f"Error: {e}")
            if session_state.get(RENDER_OUTCOME_KEY) is None:
                session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.UNKNOWN_FATAL
            result = None, f"\u274c Error: {e}", session_state
        finally:
            console_logger.finish()
        # [FORK] Digital-Union (C3-R1A): deliberately NO lifecycle.mark_terminal(...) here. This
        # worker is reused once PER CANDIDATE inside the C3-R0 batch, which threads one SHARED
        # lifecycle through both calls -- terminal-marking it from here would make candidate 1's
        # outcome freeze the batch's shared lifecycle before candidate 2 ever runs. Deriving and
        # marking the authoritative terminal state is the mutex-owning wrapper's job
        # (`process_video_guarded` / `render_selected_variants_guarded`), done exactly once, after
        # it has read the outcome of everything it ran.
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

    # [FORK] Digital-Union (C3-R0 R1): this generator OWNS the render worker, so its lifetime is
    # the worker's lifetime \u2014 not merely the frame's.
    #
    # Everything after `thread.start()` is inside `try/finally` because a consumer may abandon the
    # generator: `gen.close()`, a dropped Gradio event, or an exception while draining all unwind
    # this frame without the `while` loop ever finishing. Before R1 that returned immediately
    # while the daemon worker kept running, so the caller's `finally: _RENDER_LOCK.release()` ran
    # with a render still in flight \u2014 and `create_music_video` clears one *process-global*
    # processing dir at the start of every render, so the next one would delete the live
    # one's clips. The mutex was protecting the frame, not the render.
    #
    # The finalizer therefore **joins**. It does not terminate the worker, there is no timeout and
    # no cancel flag: C3-R0 ships no cancellation, so abandoning the stream means waiting for the
    # render in progress, which is exactly what the mutex contract already promises.
    try:
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
                # Legacy string status: shown only when no structured event has arrived yet, so
                # the old "Stage N is processing" sentences cannot overwrite richer structured
                # output.
                if view.active_stage() is not None:
                    continue
                rendered = str(item)
            if rendered != last_status:
                last_status = rendered
                yield None, rendered, session_state

        thread.join()
        yield result_queue.get()
    finally:
        # Normal completion already joined above; this is the abandonment path. `is_alive()`
        # keeps the ordinary case free and makes the intent explicit: never return while the
        # render worker we started is still running.
        if thread.is_alive():
            thread.join()


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


# [FORK] Digital-Union (Freestyle V1): the Freestyle seam, and the whole of it.
#
# One handler, and it writes one read-only textbox. It is deliberately thin — every decision (the
# five-field registry, the section vocabulary, the preset projection, `Base` semantics, the sparse
# composition, the summary layout) lives in `beatsync_fork/freestyle.py`, so the GUI contributes no
# Freestyle policy of its own. The render path does not come through here at all: both render
# wrappers take the live widget values as their own arguments and `auto_mode._resolve_freestyle`
# builds the one declaration, so a summary refresh can never be on a render's critical path.
#
# What Freestyle deliberately does NOT do: write the Variation Seed, write any of the six global
# creative sliders, write `creative_preset`, touch Director or Variant Lab state, reach a source or
# preparation widget, or render. It is a separate layer, not another Apply operation.

#: The ten section-type dropdown values in :data:`fork_freestyle.SECTION_TYPES` order. One
#: definition is the contract between the two render registrations, the two render signatures, the
#: summary handler and `auto_mode`'s reader — which is what stops the order drifting across five
#: places.
_FREESTYLE_WIDGET_ORDER = fork_freestyle.SECTION_TYPES


def _on_freestyle_change(freestyle_enabled, *styles) -> str:
    """Re-render the Freestyle summary. Writes the summary textbox and nothing else.

    Deliberately does **not** show inherited numeric values: the global controls have five
    legitimate writers (the preset selector, Variant Lab Generate / New / Apply, and Director
    Apply), so a number here would go stale the moment any of them fired. `Base` means "inherit the
    live global value at render time" and stays true whatever the global sliders later become.

    It takes no global slider as an input, which is what makes that guarantee structural rather
    than a promise: there is no global value in scope to print.
    """
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(
        freestyle_enabled, dict(zip(_FREESTYLE_WIDGET_ORDER, styles)))
    return fork_freestyle.summary_text(declaration)


# [FORK] Digital-Union (AI Director V1): two thin handlers, and the asymmetry between them IS the
# product.
#
#   Generate Proposal  runs one bounded text-only model invocation and writes **no execution
#                      widget at all** — only the proposal state, the read-out and the status. So
#                      generating cannot change the render, and a user who dislikes a proposal
#                      simply never applies it. `GENERATING_IS_NOT_APPLYING` is structural here,
#                      exactly as it is for `generate_variants_btn`.
#
#   Apply Proposal     writes the existing Variation Seed, the six creative sliders and the preset
#                      label — and nothing else. No audio widget, no source widget, no Variant Lab
#                      master seed, no batch state, no render.
#
# Neither renders, neither reads the current sliders, and neither touches a source, preparation or
# render widget. `director_proposal_state` has exactly one reader: `apply_director_btn.click`.

#: How many execution widgets Apply writes: the Variation Seed, the six sliders, the preset label.
#: Deliberately a derived count rather than a literal 8, so adding a creative control moves it.
_DIRECTOR_APPLY_OUTPUT_COUNT = 2 + len(fork_presets.CREATIVE_CONTROL_FIELDS)


def _director_apply_skips() -> Tuple:
    """`gr.skip()` for every execution widget — a refusal changes none of them."""
    return tuple(gr.skip() for _ in range(_DIRECTOR_APPLY_OUTPUT_COUNT))


def _director_apply_outputs(proposal) -> Tuple:
    """Project one proposal onto the widgets the Director is allowed to write.

    Ordered to match the `outputs` list: the Variation Seed, the six sliders in
    `CREATIVE_CONTROL_FIELDS` order, then the preset label.

    `_variant_apply_outputs` is deliberately **not** reused. It is the right projection for Variant
    Lab and the wrong one here: it also writes the Variant Lab Master Seed (generator provenance
    the Director never had), the three `AudioRecipe` levels (which Director V1 does not generate)
    and the lab's own report. Reusing it would have made the Director claim audio values it never
    produced. What *is* shared is the semantic helper that matters —
    `fork_presets.matching_preset` — so there is still exactly one preset-label path.

    The label is computed explicitly because programmatic slider writes do not fire the sliders'
    `.input()` handlers; without it the selector would keep claiming whatever preset the user was
    on before. The Director never emits a preset name of its own: a label is a read-out of six
    numbers, so it is derived from them here rather than guessed by a model.
    """
    recipe = proposal.recipe
    values = tuple(getattr(recipe, field)
                   for field in fork_presets.CREATIVE_CONTROL_FIELDS)
    return (recipe.seed,) + values + (fork_presets.matching_preset(values),)


def _run_director_model(instruction: str) -> Tuple[str, str]:
    """One bounded, one-shot, text-only `llama-completion` invocation. Returns `(stdout, failure)`.

    Exactly one of the two is non-empty. Every failure path produces a Director status message and
    leaves the caller with nothing to apply.

    **One process per proposal, and no server lifecycle.** No `llama-server`, no port, no readiness
    polling, no persistent model process and no session — a proposal is a single `subprocess.run`
    that exits, which is dramatically less new runtime architecture than managing a server for an
    interactive one-shot answer. There is no chat history either, and that is structural: the
    process dies after one turn, so there is nothing to carry.

    **`-cnv -st` rather than `-no-cnv`, and this is measured.** `-cnv` is what applies the model's
    own chat template; `-st` runs exactly one turn and exits (non-interactively, because the turn
    is predefined by `-p`). Raw completion mode skips the template, and on an *Instruct* model that
    is not a small difference: measured over five intents, `-no-cnv` collapsed every control to 0
    or 1 and rambled past the token budget, while `-cnv -st` produced coherent, well-separated
    recipes — e.g. `20/10/70/60/30/50` for a cinematic intention against `100/100/50/100/50/50` for
    an aggressive one. The retained P0 probes could not distinguish the two: the server probe went
    through `/v1/chat/completions` (template applied) and the `llama-cli` probe's `-no-cnv` was
    silently rejected by the binary, so both measured template-applied output while one of them
    *looked* like a raw-completion result. Do not "simplify" this back to `-no-cnv`.

    `--no-display-prompt` keeps the echoed prompt out of stdout, `--no-perf` and `-co off` keep
    timings and ANSI colour out of it, and `--json-schema` is defence in depth behind
    `director.parse_model_payload`, which remains the authority. No image argument and no `mmproj`:
    the Director is media-blind.
    """
    env = dict(os.environ)
    env['PATH'] = DIRECTOR_LLAMA_DIR + os.pathsep + env.get('PATH', '')
    command = [
        DIRECTOR_LLAMA_EXE,
        '-m', DIRECTOR_MODEL,
        '-ngl', '99',
        '-c', str(fork_director.CONTEXT_TOKENS),
        '-n', str(fork_director.MAX_NEW_TOKENS),
        '-cnv',
        '-st',
        '--no-display-prompt',
        '--no-perf',
        '-co', 'off',
        '--temp', str(fork_director.TEMPERATURE),
        '--top-k', str(fork_director.TOP_K),
        '-sys', fork_director.system_prompt(),
        '-p', fork_director.user_prompt(instruction),
        '--json-schema', fork_director.model_schema_json(),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=DIRECTOR_LLAMA_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            # Bounded, always. `subprocess.run` kills the child and reaps it before raising, so a
            # timeout leaves no model process behind — which is why this is `run` with a timeout
            # rather than a `Popen` the handler would have to police itself.
            timeout=fork_director.TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except subprocess.TimeoutExpired:
        return '', fork_director.STATUS_TIMEOUT
    except OSError as exc:
        return '', fork_director.launch_failure_status(exc)
    if completed.returncode != 0:
        return '', fork_director.exit_failure_status(completed.returncode, completed.stderr)
    return completed.stdout or '', ''


def _on_generate_director_proposal(director_instruction) -> Tuple:
    """Turn one instruction into a reviewable proposal. Writes no execution widget.

    Its only input is the instruction: the Director deliberately does not read the Variation Seed,
    the six sliders, the preset, the Variant Lab state or anything else on screen, so the sentence
    is interpreted as an absolute editing intention rather than as a transformation of the current
    settings. There is no cache and no history either — every press is independent.

    **The seed is minted last, and only for a valid payload (the ordering is the contract).**
    Strict six-control validation happens first, and the mint is the existing
    `fork_variation.random_seed()` — the one implementation every seed in this application comes
    from, called here in the GUI because the pure Director owns no randomness. Minting before
    validation would have turned a malformed response into a plausible-looking half proposal,
    which is the exact outcome `CreativeRecipe`'s all-or-nothing boundary exists to prevent.

    Outputs: the proposal state, the read-out, the status. A failure clears the state rather than
    leaving the previous proposal behind a status line that contradicts it.
    """
    instruction = fork_director.normalize_instruction(director_instruction)
    if not instruction:
        return None, '', fork_director.STATUS_NO_INSTRUCTION

    for required in (DIRECTOR_LLAMA_EXE, DIRECTOR_MODEL):
        if not os.path.isfile(required):
            return None, '', fork_director.missing_runtime_status(required)

    stdout, failure = _run_director_model(instruction)
    if failure:
        return None, '', failure

    payload = fork_director.parse_model_payload(stdout)
    if payload is None:
        return None, '', fork_director.STATUS_INVALID_PAYLOAD

    proposal = fork_director.build_proposal(
        instruction, payload, fork_variation.random_seed())
    if proposal is None:
        return None, '', fork_director.STATUS_INVALID_PAYLOAD

    return proposal, proposal.display_text(), fork_director.ready_status(proposal)


def _on_apply_director_proposal(director_proposal_state) -> Tuple:
    """Write the reviewed proposal into the existing execution controls — or change nothing.

    Reads **only** the proposal state. There is deliberately no stale-declaration gate here, and
    that is a difference from Variant Lab's Apply rather than an omission: that gate exists because
    a candidate describes a *base* the screen may have moved away from, whereas a Director proposal
    is an absolute set of seven values that is as valid now as when it was generated. So the state
    survives an apply and the same explicit proposal may be re-applied after manual experiments.
    Do not add Apply-staleness semantics here, and do not weaken Variant Lab's.

    Nothing is rendered, and no audio widget, source widget, preparation widget, Variant Lab master
    seed, batch state or report is written.
    """
    proposal = director_proposal_state
    if not isinstance(proposal, fork_director.DirectorProposal):
        return _director_apply_skips() + (fork_director.STATUS_NOTHING_TO_APPLY,)
    return _director_apply_outputs(proposal) + (fork_director.applied_status(proposal),)


# [FORK] Digital-Union (Variant Lab V1 / C2; audio half added by Variant Lab Audio / E2 V1): the
# two Variant Lab handlers.
#
# Both read the LIVE widget values as inputs — the six creative sliders, and since E2 V1 the three
# audio levels as well — so there is no cached base profile anywhere and a preset change or a
# manual edit is picked up by the next Generate automatically. Neither handler registers anything
# on a slider: the existing preset `.input()` graph is untouched, and these run only on their own
# button clicks.
#
# What they write is deliberately the whole story: the master seed (so a freshly minted one is
# always visible), the existing Variation Seed, the six sliders, the preset label, the three audio
# levels (`music_under_voice`, `sfx_amount`, `sfx_level` — E2 V1) and the report. No source widget,
# no preparation widget, no `process_btn` — generating a variant cannot clear a confirmation or
# start a render.


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


def _build_variant_resolution_context(
        variant_master_seed, variation_spread, variant_randomize,
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
        music_under_voice, sfx_amount, sfx_level,
        *, mint_unset_master: bool = True) -> Tuple:
    """Normalise the whole Variant Lab screen into `(master_seed, config, base, audio_config,
    audio_base)` — the **one** place that construction happens.

    [FORK] Digital-Union (Variant Lab C3 V1): extracted verbatim from `_on_generate_variant`, which
    was the only caller until C3 added two more. Three handlers now resolve from the same screen —
    Generate, Generate Variants and Apply Selected — and the live declaration Apply compares
    against is only trustworthy if it is built by the *same* code that built the batch. A second
    copy of this would be a second opinion about what the screen says.

    **`mint_unset_master` is the one place the three callers legitimately differ, and getting it
    wrong is a correctness bug rather than a nuisance (R1).** Generating is an *action*: an unusable
    master seed (0, empty, or anything `normalize_seed` refuses) is replaced by a fresh positive
    one, and both Generate handlers return it to `variant_master_seed`, so it is on screen before it
    is used — `fork_variation.random_seed()` is the only non-deterministic call in the lab, it
    happens here in the GUI rather than in the pure resolvers, and its result is always surfaced.

    Apply is **not** an action until its gate passes; it is rebuilding the live declaration in order
    to *compare* it. Minting there would make a correctness decision depend on a draw: with the
    Master Seed box blanked, a one-in-a-million `random_seed()` landing on the batch's original root
    would make the reconstructed declaration compare **equal**, and Apply would write a candidate
    for a screen that no longer declares that root — unsurfaced randomness deciding a gate. So Apply
    passes `mint_unset_master=False`: an unusable live master normalises to `0`, stays unusable, and
    the declaration is therefore *deterministically* stale. Exactly the reasoning that made
    `_fresh_variant_master_seed` a guarantee rather than a probability.

        Generate Variant / Generate Variants  ->  may mint, and surfaces what it minted
        Apply Selected (validation)           ->  NEVER mints; unset master => stale, always

    Parameter names and order mirror the `inputs` list, because Gradio passes them positionally.
    `mint_unset_master` is keyword-only so it can never be supplied by that positional list.
    """
    master_seed = fork_lab.normalize_master_seed(variant_master_seed)
    if master_seed <= 0 and mint_unset_master:
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
    return master_seed, config, base, audio_config, audio_base


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

    Unchanged by C3 in every observable way: the same inputs in the same order, the same two
    resolver calls, the same output tuple. Only the normalisation was lifted into
    `_build_variant_resolution_context`, so Generate Variants and Apply Selected cannot build a
    *differently* normalised view of the same screen. The explicit signature is kept rather than
    collapsed into `*args` because it is half of the positional contract a seam test pins against
    `variant_lab_inputs`.

    `config.spread` rather than the raw widget value reaches `resolve_audio`, which normalises its
    `spread` argument itself — `normalize_spread` is idempotent, so the resolved value is identical
    either way, and passing the normalised one is what lets a C3 batch declaration record exactly
    the number that was used.
    """
    master_seed, config, base, audio_config, audio_base = _build_variant_resolution_context(
        variant_master_seed, variation_spread, variant_randomize,
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
        music_under_voice, sfx_amount, sfx_level)
    return _variant_apply_outputs(
        master_seed,
        fork_lab.resolve(config, base),
        fork_lab.resolve_audio(master_seed, audio_config, config.spread, audio_base),
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


# [FORK] Digital-Union (Variant Lab C3 V1): multi-variant generation, comparison and Apply.
#
# Two handlers, and the asymmetry between them is the whole design:
#
#   Generate Variants  writes NO execution widget. It produces a candidate list and nothing else,
#                      so there is structurally no path by which candidate 2 could resolve from
#                      candidate 1 — the chaining bug that makes looping `_on_generate_variant`
#                      wrong exists only because *that* handler writes its result back.
#
#   Apply Selected     writes exactly one candidate through the SAME `_variant_apply_outputs`
#                      projection an ordinary Generate uses, after re-deriving the live declaration
#                      and requiring it to equal the one the batch was built from.
#
# Neither renders. Neither touches a source, preparation or render widget. `variant_batch_state` is
# comparison/selection state: only Apply ever reads it, and nothing downstream of the Variant Lab
# has heard of it.

#: How many blank-but-present outputs Apply must produce when it refuses. Exactly the length of
#: `variant_lab_outputs`, because a refusal returns `gr.skip()` for every execution widget.
_VARIANT_APPLY_OUTPUT_COUNT = 13


def _variant_batch_skips() -> Tuple:
    """`gr.skip()` for every execution widget — a refusal changes none of them."""
    return tuple(gr.skip() for _ in range(_VARIANT_APPLY_OUTPUT_COUNT))


#: [FORK] Digital-Union (C3-R0): clearing BOTH selectors, used wherever Apply consumes the batch.
#: The render choices describe candidates of a specific batch, so leaving them on screen after the
#: batch is gone would offer the user a render of something that no longer exists.
def _cleared_candidate_selectors() -> Tuple:
    return gr.update(choices=[], value=None), gr.update(choices=[], value=[])


def _on_generate_variants(variant_candidate_count,
                          variant_master_seed, variation_spread, variant_randomize,
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
    """Resolve N candidates from one frozen reading of the screen. Writes no execution widget.

    The count leads the parameter list so everything after it is `variant_lab_inputs` element for
    element — one list concatenation at the registration, and one alignment for a seam test to pin.

    The Master Seed box becomes the **batch root**: an unusable value is replaced by a fresh
    positive one and returned, exactly as ordinary Generate does, so the root is visible before it
    is used. Every candidate's own master is derived from it under the `batch` domain and shown in
    the comparison table.

    Outputs, in order: the root master seed, the batch state, the comparison table, the candidate
    selector and the status line. Deliberately **not** the Variation Seed, the six sliders, the
    preset label, the three audio levels or the Variant Lab report — generating candidates is not
    applying one, and it renders nothing.
    """
    master_seed, config, base, audio_config, audio_base = _build_variant_resolution_context(
        variant_master_seed, variation_spread, variant_randomize,
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
        music_under_voice, sfx_amount, sfx_level)

    declaration = fork_batch.declaration_from(
        config, base, audio_config, audio_base, variant_candidate_count)
    batch = fork_batch.resolve_batch(declaration)
    status = (f"{len(batch.candidates)} candidates generated from master "
              f"{declaration.root_master_seed}. Pick one and press "
              f"{LABEL_APPLY_VARIANT}. Nothing has been rendered or changed yet.")
    # [FORK] Digital-Union (C3-R0): both selectors are refreshed from the same candidate list —
    # the Apply Radio and the Render CheckboxGroup. Still no execution widget is written.
    return (
        master_seed,
        batch,
        batch.table_text(),
        gr.update(choices=batch.choices(), value=None),
        gr.update(choices=batch.choices(), value=[]),
        status,
    )


def _on_apply_selected_variant(variant_batch_state, variant_candidate_selector,
                               variant_candidate_count,
                               variant_master_seed, variation_spread, variant_randomize,
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
    """Write exactly one candidate into the execution widgets — or refuse and change nothing.

    **The live declaration is the authority, not the stored one.** The whole screen is re-read and
    re-normalised through the shared helper at click time and must equal the declaration the batch
    was generated from. That is the same reason `process_btn` validates the live source controls
    and `prep_analyze_btn` takes the live preparation controls: Gradio delivers widget changes as
    separate queued events, so at click time the state can lag behind the widgets, and applying a
    candidate that describes a base the user has since moved away from must be impossible rather
    than merely unlikely.

    **The rebuild never mints a master seed (R1).** It passes `mint_unset_master=False`, so a
    blanked or malformed Master Seed box normalises to `0`, stays unusable, and makes the live
    declaration *deterministically* unequal to any real batch's. Minting here would have put a
    `random_seed()` draw inside a correctness decision — and on the draw that happened to equal the
    batch's own root, Apply would have written a candidate for a screen no longer declaring it.

    There is deliberately **no** `.change()` invalidation on the declaration widgets. Adding one to
    every slider would put a second handler on widgets whose single `.input()` binding is itself a
    load-bearing contract (see `.claude/rules/creative-presets.md`). The table is labelled *Last
    generated batch*, so its continued presence claims history, not currency — and this gate, not
    the table, decides what may be applied.

    A refusal returns `gr.skip()` for every execution widget, so nothing moves. A **stale** refusal
    also consumes the batch and clears the selector, so pressing Apply again cannot keep retrying a
    list that can never become valid again.

    On success the candidate's own master seed goes into the Master Seed box, because that field
    means *provenance for the recipe now on the sliders* — leaving the batch root there would make
    the visible seed disagree with `VariantLabResolution.describe()` in the report beside it. The
    batch root stays visible in the Last generated batch text. Neither seed alone reproduces a
    candidate: the same base, ranges, randomize selections and Spread are required too.

    Apply is terminal for one batch: a successful apply moves the live base, so every remaining
    candidate now describes a starting point that no longer exists. Generate Variants again.
    """
    batch = variant_batch_state
    if not isinstance(batch, fork_batch.VariantBatch) or not batch.candidates:
        return _variant_batch_skips() + (
            gr.skip(), gr.skip(), gr.skip(),
            "No candidate list. Press Generate Variants first.")

    candidate = batch.candidate(variant_candidate_selector)
    if candidate is None:
        # Not stale — the list is still perfectly valid, the user simply has not chosen. Keeping
        # the batch here is the difference between a prompt and a punishment.
        return _variant_batch_skips() + (
            gr.skip(), gr.skip(), gr.skip(),
            "Select a candidate above, then press Apply Selected Variant.")

    _master_seed, config, base, audio_config, audio_base = _build_variant_resolution_context(
        variant_master_seed, variation_spread, variant_randomize,
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
        music_under_voice, sfx_amount, sfx_level,
        # R1: validation must be deterministic. Minting here would let a blanked Master Seed box
        # pass the gate whenever the hidden draw happened to land on the batch's own root.
        mint_unset_master=False)
    live_declaration = fork_batch.declaration_from(
        config, base, audio_config, audio_base, variant_candidate_count)

    if not live_declaration.matches(batch.declaration):
        return _variant_batch_skips() + (
            None,
        ) + _cleared_candidate_selectors() + (
            "These candidates were generated from different settings and no longer describe this "
            "screen, so nothing was applied. Press Generate Variants to make a new list.",)

    resolution, audio_resolution = fork_batch.rehydrate(batch.declaration, candidate)
    applied = _variant_apply_outputs(candidate.master_seed, resolution, audio_resolution)
    return applied + (
        None,
    ) + _cleared_candidate_selectors() + (
        f"Applied candidate {candidate.index + 1} (master {candidate.master_seed}) from batch "
        f"root {batch.declaration.root_master_seed}. Press Create Music Video when ready.",)


def _process_video_guarded_unlocked(audio_file: str,
                                    voice_files: VideoFilesInput, voice_start_delay: float,
                                    voice_min_gap: float, voice_avoid_drops: bool,
                                    music_under_voice: int,
                                    sfx_folder: str, sfx_roles, sfx_amount: int, sfx_level: int,
                                    source_mode: str, source_folder: str,
                                    source_recursive: bool, video_input: VideoFilesInput,
                                    output_filename: str, processing_mode: str,
                                    custom_fps: float, variation_seed: int,
                                    cut_density: int, energy_response: int, motion_bias: int,
                                    source_diversity: int, micro_cuts: int,
                                    semantic_emphasis: int,
                                    session_state: dict,
                                    source_state,
                                    # [FORK] Digital-Union (Freestyle V1): appended LAST with a
                                    # default, so no existing positional argument moved and every
                                    # existing caller stays valid.
                                    freestyle: object | None = None,
                                    # [FORK] Digital-Union (C3-R1A): appended LAST with a default,
                                    # same reasoning. Constructed and installed by the caller
                                    # (`process_video_guarded` / `render_selected_variants_guarded`)
                                    # -- this core only threads it down and reads its invocation_id
                                    # to publish on every yield.
                                    lifecycle: "RenderLifecycle | None" = None
                                    ) -> Iterator[GuardedResult]:
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

    [FORK] Digital-Union (C3-R0): this is the **unlocked core** — the one authoritative live
    source-gate-plus-render body — and it is **not** a public render entry point. Exactly two
    mutex-owning wrappers may call it in production: `process_video_guarded` (single render) and
    `render_selected_variants_guarded` (the 2-4-candidate batch). The batch must reach *this*
    rather than the single-render wrapper, because it already holds the render mutex for the whole
    batch and calling a wrapper that acquires the same non-reentrant lock would make the batch
    refuse itself on its own first candidate.

    [FORK] Digital-Union (H1): there is **no** overwrite-policy parameter here, and that absence is
    the contract. Both wrappers get one universal durable-output policy — atomic no-replace
    promotion, see `_promote_output_no_replace` — because no GUI caller ever wants to destroy an
    existing output. C3-R0's keyword-only `refuse_existing_output` made safety opt-in and left the
    ordinary single render destructive; do not reintroduce it or any other boolean in its place.
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
    # [FORK] Digital-Union (C3-R0): the durable-output bookkeeping key follows the same lifecycle
    # as the two read-outs above — cleared before the gate, so a refused render cannot leave the
    # previous attempt's output path behind for a batch to read as this candidate's result.
    session_state[LAST_OUTPUT_PATH_KEY] = ''
    # [FORK] Digital-Union (C3-R1A): same lifecycle again -- cleared before the gate too.
    session_state[RENDER_OUTCOME_KEY] = None

    verification_started = time.perf_counter()
    decision = resolve_for_render(
        source_state,
        live_declaration(source_mode, source_folder, source_recursive, video_input),
    )
    verification_seconds = time.perf_counter() - verification_started
    invocation_id = lifecycle.invocation_id if lifecycle is not None else ''
    if not decision.allowed:
        # [FORK] Digital-Union (C3-R1B-a): SHARED_FATAL. The gate's only inputs are `source_state`
        # and `live_declaration(source_mode, source_folder, source_recursive, video_input)` -- every
        # one of them an argument the C3 batch handler froze before its candidate loop, identical
        # for every candidate. A candidate contributes its output stem, seven creative values and
        # three audio levels, none of which the gate reads, so a refusal cannot be rescued by a
        # different recipe. This is the first producer `SHARED_FATAL` ever had.
        session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SHARED_FATAL
        yield None, f"❌ {decision.message}", session_state, '', '', invocation_id
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
    # [FORK] Digital-Union (C3-R0 R1): the stream is OWNED, not anonymous.
    #
    # `yield from` or an anonymous `for` would propagate a close eventually, but *when*
    # depends on when the generator object is collected — and here the close chain is a
    # safety contract, not a convenience: it has to run before the caller's
    # `finally: _RENDER_LOCK.release()`. An owned stream closed in a finalizer makes that
    # ordering explicit, synchronous and visible to a structural test. `process_video`'s own
    # finalizer then joins the worker, so by the time this `finally` returns the render has
    # actually stopped rather than merely been let go of.
    render_stream = process_video(
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
        freestyle=freestyle,
        lifecycle=lifecycle,
    )
    try:
        for video, status, state in render_stream:
            yield (video, status, state,
                   (state or {}).get(AUDIO_LAYERS_REPORT_KEY, ''),
                   (state or {}).get(SMART_MIX_REPORT_KEY, ''),
                   invocation_id)
    finally:
        render_stream.close()


# [FORK] Digital-Union (C3-R0): one process-global render mutex, and it is the authority.
#
# `create_music_video` clears `get_processing_dir()` — a single module-level path, process-global
# and NOT per session — at the start of every render. Two overlapping renders would therefore
# delete each other's in-flight clips. Until C3-R0 there was exactly one render event, so the
# hazard was latent; adding a second one creates it.
#
# Gradio's own `concurrency_id` is wired below as well, but it is cooperative: separate listeners
# get separate queues unless grouped, and this repository cannot verify the installed library's
# behaviour (the portable runtime is not present on every machine). A plain non-reentrant
# `threading.Lock`, acquired non-blockingly, is provable by a unit test with no Gradio at all — so
# that is what guarantees the invariant, and the concurrency group is the courtesy on top.
#
# Deliberately a `Lock`, never an `RLock`: re-entrancy is exactly the bug (a batch calling the
# single-render wrapper) that the unlocked core exists to prevent, and a reentrant lock would hide
# it instead of refusing.
_RENDER_LOCK = threading.Lock()

#: Both render events join one Gradio concurrency group. The string itself carries no meaning; one
#: shared value is the whole contract.
RENDER_CONCURRENCY_ID = 'beatsync-render'

#: [FORK] Digital-Union (C3-R1A): the Cancel button's OWN concurrency lane -- deliberately never
#: `RENDER_CONCURRENCY_ID`. Gradio's concurrency groups are cooperative queuing, not an execution
#: guarantee, but queuing Cancel behind the very render it needs to signal would make it useless:
#: it would only run after the render it was meant to interrupt had already finished.
CANCEL_CONCURRENCY_ID = 'beatsync-cancel'

RENDER_BUSY_MESSAGE = (
    "⏳ A render is already running. Wait for it to finish before starting another — "
    "BeatSync renders one video at a time."
)

# ---------------------------------------------------------------------------
# [FORK] Digital-Union (C3-R1A): the capacity-one active-render slot.
# ---------------------------------------------------------------------------
#
# Runtime coordination state only, never persisted and never a second render authority:
# `_RENDER_LOCK` above remains the one thing that decides whether a render may start. This slot
# exists solely so a SEPARATE Gradio event (Cancel) can find the correct live `RenderLifecycle` to
# signal, without ever storing a `threading.Event`, a `RenderLifecycle` or any process/thread
# handle in `gr.State` — only the plain string invocation id crosses into Gradio state.
#
# Capacity exactly one, protected by its own small lock (deliberately NOT `_RENDER_LOCK`: that lock
# is held for the whole render and installing/reading this slot must never risk contending with or
# substituting for it). Holds only `(invocation_id, lifecycle)` or `None` — never a Popen, a
# worker thread, or a list of past invocations.
_ACTIVE_RENDER_SLOT_LOCK = threading.Lock()
_active_render_slot: "tuple[str, RenderLifecycle] | None" = None


def _install_active_render(lifecycle: "RenderLifecycle") -> None:
    """Install the one lifecycle a Cancel click may reach. Overwrites any stale prior entry.

    Only ever called by a mutex-owning wrapper that already holds `_RENDER_LOCK`, so "install"
    never races another "install" -- but the slot's own lock is still what a concurrent Cancel
    read/clear synchronizes against.
    """
    global _active_render_slot
    with _ACTIVE_RENDER_SLOT_LOCK:
        _active_render_slot = (lifecycle.invocation_id, lifecycle)


def _clear_active_render(invocation_id: str) -> None:
    """Unregister the slot, but ONLY if it still names this exact invocation.

    This is what stops a stale finalizer (an abandoned render's delayed cleanup) from ever
    clearing a NEWER render's slot -- the check and the clear are one atomic step under the lock.
    """
    global _active_render_slot
    with _ACTIVE_RENDER_SLOT_LOCK:
        if _active_render_slot is not None and _active_render_slot[0] == invocation_id:
            _active_render_slot = None


def _request_cancel_if_matching(invocation_id: str) -> bool:
    """Signal the active lifecycle IFF the submitted id matches it. Returns whether it matched.

    This is the ENTIRE body of work the Cancel handler performs on the slot: no lock beyond the
    slot's own, no Popen, no thread, no Gradio component. A stale or empty id, or an id that no
    longer matches because a newer render has since started, is a safe, silent no-op -- never an
    error and never a signal to the wrong invocation.
    """
    if not invocation_id:
        return False
    with _ACTIVE_RENDER_SLOT_LOCK:
        slot = _active_render_slot
    if slot is None or slot[0] != invocation_id:
        return False
    slot[1].request_cancel()
    return True


def _on_cancel_render_click(invocation_id: str) -> str:
    """The Cancel button's entire handler. Touches only the active-render slot, never a widget.

    [FORK] Digital-Union (C3-R1A): deliberately returns a plain status string rather than
    `gr.skip()` for a non-matching id -- a stale id (an abandoned browser tab, or a batch that has
    since moved to its next candidate under a NEW invocation id) is reported truthfully as
    "nothing to cancel" rather than silently doing nothing with no feedback. This never raises and
    never touches `_RENDER_LOCK`, `session_state` or any report panel.
    """
    if _request_cancel_if_matching(invocation_id):
        return STATUS_CANCEL_REQUESTED
    return STATUS_CANCEL_NOTHING_ACTIVE


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
                          source_state,
                          # [FORK] Digital-Union (Freestyle V1): the LIVE Freestyle widgets, read at
                          # click time and appended at the END so every existing positional index
                          # is unchanged. They are the render's execution authority for section
                          # rules — see the docstring below for why `gr.State` is not.
                          freestyle_enabled: bool = False,
                          freestyle_intro: str | None = None,
                          freestyle_hook: str | None = None,
                          freestyle_outro: str | None = None,
                          freestyle_finale: str | None = None,
                          freestyle_drop: str | None = None,
                          freestyle_chorus: str | None = None,
                          freestyle_bridge: str | None = None,
                          freestyle_breakdown: str | None = None,
                          freestyle_verse: str | None = None,
                          freestyle_body: str | None = None,
                          ) -> Iterator[GuardedResult]:
    """The single-render entry point: take the render mutex, then run the shared gated core.

    [FORK] Digital-Union (C3-R0): the gate logic itself did not move or change — it is
    `_process_video_guarded_unlocked`, and this wrapper adds only mutual exclusion. The parameter
    list is byte-identical to before, because it is half of the positional contract with
    `process_btn.click(inputs=...)` that a seam test pins name-for-name.

    If another render holds the lock this refuses immediately and **touches nothing**: no source
    state, no report widgets, and `gr.skip()` for the video so the previous preview survives. A
    user who clicks twice loses nothing.

    [FORK] Digital-Union (Freestyle V1): the section-rule declaration is built **here, from the
    live widget values submitted with this click** — not from a `gr.State`. That is the same
    reasoning the live source gate rests on: Gradio delivers widget changes as separate queued
    events, so a `gr.State` can lag behind the widgets at click time, and a render must use the
    rules the user is currently declaring. The readout state exists for the summary panel only and
    is never execution authority.
    """
    if not _RENDER_LOCK.acquire(blocking=False):
        yield gr.skip(), RENDER_BUSY_MESSAGE, session_state, gr.skip(), gr.skip(), ''
        return
    # [FORK] Digital-Union (C3-R1A): ONE lifecycle for this whole render event, constructed and
    # installed here, before any expensive work -- the mutex-owning wrapper owns
    # installation/uninstallation, exactly as the frozen authorization requires.
    lifecycle = RenderLifecycle(invocation_id=str(uuid.uuid4()))
    _install_active_render(lifecycle)
    try:
        lifecycle.mark_running()
        # The user cannot meaningfully cancel a render before the browser/session has been told
        # which invocation is active, so this id-publishing yield happens BEFORE the gate or any
        # pipeline work -- it writes no execution widget and no report, only the control-plane id.
        yield gr.skip(), 'Starting…', session_state, gr.skip(), gr.skip(), lifecycle.invocation_id
        yield from _process_video_guarded_unlocked(
            audio_file, voice_files, voice_start_delay, voice_min_gap, voice_avoid_drops,
            music_under_voice, sfx_folder, sfx_roles, sfx_amount, sfx_level,
            source_mode, source_folder, source_recursive, video_input,
            output_filename, processing_mode, custom_fps, variation_seed,
            cut_density, energy_response, motion_bias, source_diversity,
            micro_cuts, semantic_emphasis, session_state, source_state,
            # One frozen tuple of exactly what the user submitted, in
            # `freestyle.SECTION_TYPES` order. A plain tuple rather than a built
            # `FreestyleDeclaration` on purpose: this body is AST-extracted and executed by the
            # frozen progress/guard seam suites against a synthesised namespace, so naming a fork
            # module here would break tests this feature is required to preserve unchanged. The
            # tuple is immutable, so it is every bit as frozen; `analyze_beats_auto` turns it into
            # the one declaration.
            (freestyle_enabled, freestyle_intro, freestyle_hook, freestyle_outro,
             freestyle_finale, freestyle_drop, freestyle_chorus, freestyle_bridge,
             freestyle_breakdown, freestyle_verse, freestyle_body),
            lifecycle=lifecycle,
        )
        # [FORK] Digital-Union (C3-R1A): the ONE place this single render's lifecycle reaches a
        # terminal state -- derived from the typed outcome `_process_video_impl` recorded, not from
        # `process_video`'s reused-per-candidate worker (see its own comment for why that would be
        # wrong for the C3-R0 batch, which shares one lifecycle across two worker calls).
        outcome_kind = session_state.get(RENDER_OUTCOME_KEY)
        if outcome_kind is RenderOutcomeKind.CANCELLED:
            lifecycle.mark_terminal(RenderLifecycleState.CANCELLED)
        elif outcome_kind is RenderOutcomeKind.SUCCESS:
            lifecycle.mark_terminal(RenderLifecycleState.FINISHED)
        else:
            lifecycle.mark_terminal(RenderLifecycleState.FAILED)
        # The terminal yield clears the invocation id from the session -- a render that finished
        # normally has nothing left for a Cancel click to signal.
        yield gr.skip(), gr.skip(), session_state, gr.skip(), gr.skip(), ''
    finally:
        # [FORK] Digital-Union (C3-R1A): defensive terminal mark -- the normal path above already
        # reaches a terminal lifecycle state; this only catches an abandoned-generator or truly
        # unexpected-exception path that did not. Unregistering is conditional on THIS exact
        # invocation id, so a slower-finishing abandoned render can never clear a newer one's slot.
        if not lifecycle.is_terminal():
            lifecycle.mark_terminal(RenderLifecycleState.FAILED)
        _clear_active_render(lifecycle.invocation_id)
        _RENDER_LOCK.release()


def _render_batch_request_tag() -> str:
    """One visible, sortable tag per Render Selected click. Microseconds, no randomness."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def render_selected_variants_guarded(
        variant_batch_state, variant_render_selection,
        audio_file: str,
        voice_files: VideoFilesInput, voice_start_delay: float,
        voice_min_gap: float, voice_avoid_drops: bool,
        sfx_folder: str, sfx_roles,
        source_mode: str, source_folder: str,
        source_recursive: bool, video_input: VideoFilesInput,
        output_filename: str, processing_mode: str, custom_fps: float,
        session_state: dict, source_state,
        # [FORK] Digital-Union (Freestyle V1): the LIVE Freestyle widgets, appended LAST with
        # defaults so no existing positional argument moved. Frozen once below, before the
        # candidate loop.
        freestyle_enabled: bool = False,
        freestyle_intro: str | None = None,
        freestyle_hook: str | None = None,
        freestyle_outro: str | None = None,
        freestyle_finale: str | None = None,
        freestyle_drop: str | None = None,
        freestyle_chorus: str | None = None,
        freestyle_bridge: str | None = None,
        freestyle_breakdown: str | None = None,
        freestyle_verse: str | None = None,
        freestyle_body: str | None = None,
        ) -> Iterator[Tuple]:
    """Render the 2-4 selected candidates, one after the other. C3-R0 -> C3-R1B-b.

    [FORK] Digital-Union (C3-R0). Three things make this safe, and all three are deliberate:

    **It takes the mutex once, for the whole batch**, and reaches
    `_process_video_guarded_unlocked` directly rather than `process_video_guarded`. Calling the
    single-render wrapper would try to re-acquire a non-reentrant lock this function already holds
    and the batch would refuse itself on candidate 1.

    **The candidate-specific values come from the stored recipes, never from the screen.** Two
    candidates are never simultaneously visible, so the live widgets cannot be execution authority
    for a batch. `variation_seed`, the six creative controls and the three audio levels are read
    off `creative_recipe` / `audio_recipe`; nothing is re-resolved. Everything else — audio, voice,
    SFX, source, output, encoder, FPS — is frozen from this handler's own submitted arguments, so
    edits made while the batch runs cannot reach it.

    **It does not require Apply's stale-declaration gate.** That gate exists because Apply writes a
    historical candidate into the *current* screen. Rendering reads already-resolved artifacts and
    writes no widget, so a user who nudged a slider after generating may still render the pair.

    Fail-fast: a failed candidate stops the batch and the earlier candidate's file is kept. The
    `VariantBatch` is **not** consumed — the comparison survives, so the pair can be rendered again
    or one of them applied.
    """
    batch = variant_batch_state
    summary_only = (gr.skip(), gr.skip(), gr.skip())

    request, refusal = fork_render_batch.build_request(
        batch, variant_render_selection,
        user_base=output_filename or 'music_video',
        request_tag=_render_batch_request_tag(),
    )
    if request is None:
        yield summary_only[0], f"❌ {refusal}", session_state, refusal, ''
        return

    if not _RENDER_LOCK.acquire(blocking=False):
        yield gr.skip(), RENDER_BUSY_MESSAGE, session_state, RENDER_BUSY_MESSAGE, ''
        return

    # [FORK] Digital-Union (C3-R1A): ONE lifecycle for the WHOLE batch -- every selected candidate
    # shares it,
    # exactly as the frozen authorization requires ("no moment between candidates where the batch
    # has no cancellable top-level lifecycle"). Constructed and installed here, by the mutex-owning
    # wrapper, mirroring `process_video_guarded` exactly.
    lifecycle = RenderLifecycle(invocation_id=str(uuid.uuid4()))
    _install_active_render(lifecycle)

    outcomes: list = []
    stopped = False
    try:
        lifecycle.mark_running()
        # Publish the invocation id BEFORE any candidate work, same reasoning as the single-render
        # wrapper: a Cancel click needs the id on screen before there is anything to cancel.
        yield gr.skip(), 'Starting…', session_state, gr.skip(), lifecycle.invocation_id

        # [FORK] Digital-Union (Freestyle V1): ONE declaration for the whole batch, frozen HERE —
        # outside the candidate loop, from this handler's own submitted arguments. Both candidates
        # therefore render under the identical section rules, and a user editing a Freestyle
        # dropdown while the batch runs cannot reach candidate 2. Exactly the treatment audio,
        # voice, SFX, source, output, encoder and FPS already get: Freestyle is shared render
        # intent, not a candidate value, so C3 needed no new frozen field and `render_batch.py` is
        # untouched. Each candidate still composes its OWN global recipe with these shared rules.
        batch_freestyle = (
            freestyle_enabled, freestyle_intro, freestyle_hook, freestyle_outro,
            freestyle_finale, freestyle_drop, freestyle_chorus, freestyle_bridge,
            freestyle_breakdown, freestyle_verse, freestyle_body)

        for position, candidate in enumerate(request.candidates, start=1):
            # [FORK] Digital-Union (C3-R1A): checked BEFORE starting each candidate, closing the
            # candidate-boundary race the frozen authorization calls out explicitly -- a Cancel
            # click that lands in the gap between two candidates must stop the batch here rather
            # than silently being honoured only on the NEXT internal safe-boundary check deep
            # inside the candidate that is about to start.
            if lifecycle.cancel_requested():
                stopped = True
                break

            prefix = f"Rendering candidate {position} / {request.count}"
            recipe = candidate.creative_recipe
            audio = candidate.audio_recipe
            last_status, last_video = '', None

            # The whole live source gate runs again for this candidate, through the same core the
            # single render uses — so filesystem identity is freshly verified before each one and
            # there is no second gate implementation anywhere.
            # Owned for the same reason, one level up: if the batch generator is abandoned
            # mid-candidate this finalizer closes the candidate's core stream, which closes
            # `process_video`, which joins the worker — all before the batch's own
            # `finally: _RENDER_LOCK.release()` below. The lock cannot reach a second render
            # while this candidate is still rendering.
            candidate_stream = _process_video_guarded_unlocked(
                        audio_file,
                        voice_files, voice_start_delay, voice_min_gap, voice_avoid_drops,
                        audio.music_under_voice_percent,
                        sfx_folder, sfx_roles, audio.sfx_amount, audio.sfx_level_percent,
                        source_mode, source_folder, source_recursive, video_input,
                        candidate.output_stem, processing_mode, custom_fps,
                        recipe.seed,
                        recipe.cut_density, recipe.energy_response, recipe.motion_bias,
                        recipe.source_diversity, recipe.micro_cuts, recipe.semantic_emphasis,
                        session_state, source_state,
                        batch_freestyle,
                        lifecycle=lifecycle)
            try:
                for video, status, state, _a_report, _s_report, _invocation_id in candidate_stream:
                    last_status = status or ''
                    if video is not None:
                        last_video = video
                    # Prefix only. The inner ProgressView text is passed through untouched — no new
                    # stage, no new phase, no second progress protocol, and nothing parsed out of it.
                    yield (gr.skip() if video is None else video,
                           f"{prefix}\n\n{last_status}",
                           state,
                           gr.skip(),
                           lifecycle.invocation_id)
            finally:
                candidate_stream.close()

            # Durable output is the success authority, not the preview and not the prose. A ProRes
            # render that finished and then hit trouble generating its display preview has still
            # produced a `.mov` the user owns.
            durable = (session_state or {}).get(LAST_OUTPUT_PATH_KEY, '') or ''
            # [FORK] Digital-Union (C3-R1A): a candidate cancelled mid-render is a typed CANCELLED
            # outcome, never an ordinary "not durable" failure -- `session_state[RENDER_OUTCOME_KEY]`
            # is the one authority for that, never inferred from `durable` or `last_status`.
            #
            # [FORK] Digital-Union (C3-R1B-a): read the FULL typed class, not just CANCELLED. R1A
            # recorded `CANCELLED if candidate_cancelled else None`, which threw away every other
            # class the producers had proven -- `CANDIDATE_LOCAL` was written by
            # `_process_video_impl` and read by nobody, and `None` then derived to `UNKNOWN_FATAL`
            # in `RenderCandidateOutcome.__post_init__`. The class is still taken ONLY from
            # `RENDER_OUTCOME_KEY`; nothing is inferred from the status text, the durable path, a
            # message prefix or an emoji. `durable` remains the success authority.
            candidate_kind = (session_state or {}).get(RENDER_OUTCOME_KEY)
            # [FORK] Digital-Union (C3-R1B-b): R1A's `candidate_cancelled` local is gone rather than
            # left unused. Cancellation is still **typed, never inferred** -- it rides
            # `RENDER_OUTCOME_KEY` into `outcome_kind` below, and the model exposes it as
            # `RenderCandidateOutcome.cancelled`. A cancelled candidate has `success is False`, so
            # the stop branch below covers it; what it is NOT is `CANDIDATE_LOCAL`, which is the only
            # class that continues.
            # [FORK] Digital-Union (C3-R1B-a / R2): the explicit class is passed through
            # **unchanged**. R1 wrapped it in a `success`/`kind` agreement filter and substituted
            # `None` on disagreement, which was wrong twice over: it defeated the invariant
            # `RenderCandidateOutcome.__post_init__` exists to enforce, and it then let the
            # conservative derivation publish a DIFFERENT class than the producer named -- laundering
            # a broken producer contract into a plausible-looking outcome.
            #
            # A disagreement here is not a runtime situation to absorb. Either shape --
            # SUCCESS with no durable path, or a failure class WITH one -- means a producer violated
            # its contract, and the durable promotion remains the sole success authority. So it must
            # be LOUD: the model raises `ValueError`, and that is correct. Hiding it would leave
            # C3-R1B-b making a continuation decision on a class nobody verified.
            #
            # Conservative derivation is reserved for the one case that genuinely proves nothing:
            # `candidate_kind is None`, i.e. a producer that never classified itself. The model then
            # derives SUCCESS from a durable path and UNKNOWN_FATAL otherwise.
            # [FORK] Digital-Union (C3-R1B-b): construct the outcome FIRST, append that exact
            # object, and then read the continuation policy off `candidate_outcome.outcome_kind` --
            # never off the raw `candidate_kind` local. The difference is load-bearing: a producer
            # that classified nothing leaves `candidate_kind` as `None`, and only the MODEL decides
            # what `None` means (SUCCESS with a durable path, UNKNOWN_FATAL without). Branching on
            # the raw value would let an unclassified failure slip past as "not CANDIDATE_LOCAL,
            # therefore keep going" -- or worse, be mistaken for a local one.
            candidate_outcome = fork_render_batch.RenderCandidateOutcome(
                candidate_index=candidate.candidate_index,
                candidate_master_seed=candidate.candidate_master_seed,
                variation_seed=candidate.variation_seed(),
                success=bool(durable),
                durable_output_path=durable,
                preview_path=last_video or '',
                status_text=last_status,
                audio_layers_report=(session_state or {}).get(AUDIO_LAYERS_REPORT_KEY, '') or '',
                smart_mix_report=(session_state or {}).get(SMART_MIX_REPORT_KEY, '') or '',
                outcome_kind=candidate_kind,
            )
            outcomes.append(candidate_outcome)

            # [FORK] Digital-Union (C3-R1B-b): the continuation matrix, and the whole of it.
            #
            #   SUCCESS           -> fall through, render the next selected candidate
            #   CANDIDATE_LOCAL   -> recorded above, then CONTINUE -- the failure is proven not to
            #                        condemn the rest (a different candidate's resolved SFX Amount
            #                        can skip the failing operation entirely, and an output
            #                        collision is on a candidate-unique name)
            #   SHARED_FATAL      -> STOP; every remaining candidate would fail identically
            #   UNKNOWN_FATAL     -> STOP; fail closed, the cause is not proven
            #   CANCELLED         -> STOP; a Stop is never continued past
            #
            # `continue` is NOT a free pass over a Cancel: the loop head re-checks
            # `lifecycle.cancel_requested()` before the next candidate starts, so a Cancel arriving
            # after a local failure still leaves the remaining candidates NOT ATTEMPTED. One
            # lifecycle spans all of them, so there is no gap to slip through.
            if candidate_outcome.outcome_kind is RenderOutcomeKind.CANDIDATE_LOCAL:
                continue
            if not candidate_outcome.success:
                # Every other non-success class -- SHARED_FATAL, UNKNOWN_FATAL, CANCELLED, and the
                # model's conservative UNKNOWN_FATAL derivation -- ends the batch. Earlier durable
                # outputs are kept; nothing is deleted and no candidate is retried.
                stopped = True
                break
    finally:
        # [FORK] Digital-Union (C3-R1A): the ONE place the batch's shared lifecycle reaches a
        # terminal state -- after everything it ran, mirroring `process_video_guarded`. A Cancel
        # click that landed anywhere in the batch (mid-candidate or at a candidate boundary) is
        # authoritative over everything else: the top-level EVENT was cancelled.
        #
        # [FORK] Digital-Union (C3-R1B-b / R2): the completion test is **selection exhaustion**, not
        # the last candidate's outcome. R1A read `session_state[RENDER_OUTCOME_KEY]`, which holds
        # whatever the final candidate happened to write -- sufficient while every candidate failure
        # stopped the batch, and order-dependent nonsense once CANDIDATE_LOCAL continues:
        #
        #     CANDIDATE_LOCAL, SUCCESS  -> FINISHED
        #     SUCCESS, CANDIDATE_LOCAL  -> FAILED     <- same batch result, different state
        #
        # Both batches attempted their whole selection, both report `1 / 2 succeeded; 1 failed`, and
        # both have batch `outcome_kind is None`. Only the order differed.
        #
        # `RenderLifecycle` belongs to the TOP-LEVEL render event, so its terminal state answers
        # "what happened to the event", not "did every candidate succeed":
        #
        #     CANCELLED  an explicit cancellation request won
        #     FINISHED   the batch exhausted its full selected list under the authorized policy,
        #                however many attempted candidates failed locally along the way
        #     FAILED     the event ended BEFORE exhausting its selection -- an early fatal stop, an
        #                unexpected exception, or an invariant ValueError
        #
        # Candidate failures stay represented where they belong: on their own
        # `RenderCandidateOutcome` and in the summary. Nothing is erased or reclassified -- a
        # candidate that carried SHARED_FATAL still carries it even when it was the final one and
        # the lifecycle therefore reads FINISHED.
        if lifecycle.cancel_requested():
            lifecycle.mark_terminal(RenderLifecycleState.CANCELLED)
        elif len(outcomes) == request.count:
            lifecycle.mark_terminal(RenderLifecycleState.FINISHED)
        else:
            lifecycle.mark_terminal(RenderLifecycleState.FAILED)
        # Unchanged defensive backstop for a path that never reached the derivation above at all
        # (an abandoned generator, an exception before the loop). Abandonment is still NOT an
        # explicit Cancel -- nothing here calls `request_cancel()`.
        if not lifecycle.is_terminal():
            lifecycle.mark_terminal(RenderLifecycleState.FAILED)
        _clear_active_render(lifecycle.invocation_id)
        _RENDER_LOCK.release()

    # [FORK] Digital-Union (C3-R1A / R2): the batch's OWN terminal cause, stated explicitly.
    # `lifecycle` is still in scope and its cancellation Event survives the terminal transition
    # above by design, so this reads the same authority the `finally` just used. It has to be stated
    # rather than derived, because the batch-boundary case leaves no cancelled CANDIDATE to derive
    # from: candidate 1 genuinely succeeded and candidate 2 was never attempted, so the fact that
    # the top-level EVENT was cancelled is expressible nowhere else. Without this, the summary
    # reported "stopped on candidate 1" for a candidate that had just succeeded.
    #
    # [FORK] Digital-Union (C3-R1B-b): the batch cause is now a three-way decision, because
    # "a candidate failed" no longer implies "the batch stopped".
    #
    #   cancellation requested anywhere   -> CANCELLED, authoritative over everything else
    #   the batch STOPPED on a candidate  -> that candidate's exact fatal class
    #   the whole selection was attempted -> None, even with CANDIDATE_LOCAL failures in it
    #
    # The last line is the one worth being explicit about: a completed batch carrying one or two
    # local failures is **not** batch-`CANDIDATE_LOCAL`. That cause belongs to the candidate that
    # suffered it; the batch executed its policy to the end and has no terminal cause of its own.
    # Only the candidate that actually ended the run contributes one, which is why this reads the
    # LAST appended outcome rather than searching for the first failure.
    # `stopped and len(outcomes) < request.count` is the precise test for "a candidate ended the
    # run with work still outstanding". A fatal on the FINAL selected candidate left nothing
    # unattempted, so the batch completed its selection and has no terminal cause -- the candidate
    # owns its failure, exactly as a CANDIDATE_LOCAL one does.
    if lifecycle.cancel_requested():
        batch_outcome_kind = RenderOutcomeKind.CANCELLED
    elif stopped and outcomes and len(outcomes) < request.count:
        terminal = outcomes[-1]
        batch_outcome_kind = (
            terminal.outcome_kind
            if terminal.outcome_kind in (RenderOutcomeKind.SHARED_FATAL,
                                         RenderOutcomeKind.UNKNOWN_FATAL)
            else None)
    else:
        batch_outcome_kind = None
    outcome = fork_render_batch.RenderBatchOutcome(
        requested_count=request.count,
        outcomes=tuple(outcomes),
        outcome_kind=batch_outcome_kind,
    )
    # The newest successful preview wins, and a later failure never blanks an earlier success.
    preview = outcome.latest_successful_preview()
    # The batch is fully done -- nothing left for a Cancel click to signal.
    yield (preview if preview else gr.skip(),
           outcome.headline(),
           session_state,
           outcome.summary_text(),
           '')


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
        # [FORK] Digital-Union (C3-R1A): the plain-string bridge from a render's `RenderLifecycle`
        # into Gradio. Deliberately a bare string, never the lifecycle object itself (nor an Event,
        # nor a lock) -- `gr.State` deep-copies and may serialize its value, and a live
        # synchronization primitive must never cross that boundary. Written only by the two render
        # wrappers (publish on start, clear to '' on terminal completion) and read only by the
        # Cancel handler, which compares it against the server-side active-render slot.
        render_invocation_state = gr.State('')
        # [FORK] Digital-Union (Variant Lab C3 V1): the candidate list awaiting selection, and
        # nothing else. Comparison/selection state: only `apply_variant_btn` ever reads it, it is
        # absent from `process_btn.click`, `source_outputs`, `prep_outputs` and `live_declaration`,
        # and no planner, renderer, profile or cache has heard of it. Its value is a frozen
        # `VariantBatch` of plain ints, strings and tuples — Gradio deep-copies state, so a config
        # or resolution object (which carry `MappingProxyType`) could not live here.
        variant_batch_state = gr.State(None)
        # [FORK] Digital-Union (AI Director V1): the last generated proposal awaiting an explicit
        # Apply, and nothing else. Exactly ONE reader — `apply_director_btn.click` — and it is
        # absent from `process_btn.click`, `source_outputs`, `prep_outputs`, `live_declaration`,
        # `CreativeProfile`, `beat_info["creative"]` and both mix configs. Its value is a frozen
        # `DirectorProposal` of a seven-integer `CreativeRecipe` plus two strings: Gradio
        # deep-copies state, so a model object, a process handle or anything carrying
        # `MappingProxyType` could not live here.
        director_proposal_state = gr.State(None)

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
                    # [FORK] Digital-Union (AI Director V1): a compact group at the TOP of this
                    # block, because the order is the explanation — describe the edit, review a
                    # proposal, then the existing Creative Controls below are what actually gets
                    # rendered:
                    #
                    #     AI Director  ->  Creative Controls  ->  Variant Lab  ->  Render
                    #
                    # Five components and one hidden state object. No gallery, no chat UI, no
                    # conversation history, and deliberately not a new top-level workflow: the
                    # Director is a second way to fill in controls this app already had.
                    #
                    # The instruction box registers NOTHING — it is read at click time, exactly
                    # like every Variant Lab configuration widget — so typing an instruction
                    # cannot move a slider, clear a confirmation or start anything.
                    with gr.Accordion(label=LABEL_AI_DIRECTOR, open=False):
                        gr.Markdown(INFO_AI_DIRECTOR)
                        director_instruction = gr.Textbox(
                            label=LABEL_DIRECTOR_INSTRUCTION,
                            value='',
                            placeholder=PLACEHOLDER_DIRECTOR_INSTRUCTION,
                            info=INFO_DIRECTOR_INSTRUCTION,
                            lines=3,
                            max_lines=6,
                            elem_id='director-instruction-input',
                        )
                        with gr.Row():
                            generate_director_btn = gr.Button(
                                LABEL_GENERATE_PROPOSAL, variant='secondary',
                                elem_id='generate-director-proposal-button')
                            apply_director_btn = gr.Button(
                                LABEL_APPLY_PROPOSAL, variant='secondary',
                                elem_id='apply-director-proposal-button')
                        # Read-only, and formatted entirely by `DirectorProposal.display_text()` —
                        # one formatter per read-out, exactly as the Variant Lab and mix reports
                        # work. Four lines of content plus room for a bounded explanation.
                        director_proposal = gr.Textbox(
                            label=LABEL_DIRECTOR_PROPOSAL,
                            value='',
                            placeholder=PLACEHOLDER_DIRECTOR_PROPOSAL,
                            lines=6,
                            max_lines=10,
                            interactive=False,
                            elem_id='director-proposal-box',
                        )
                        # Its own panel. The Director never borrows `variant_report`,
                        # `variant_batch_status`, `audio_layers_report` or `smart_mix_report`:
                        # a panel that described two different things would leave the user unable
                        # to tell which statement was about which.
                        director_status = gr.Textbox(
                            label=LABEL_DIRECTOR_STATUS,
                            value='',
                            placeholder=PLACEHOLDER_DIRECTOR_STATUS,
                            lines=3,
                            max_lines=5,
                            interactive=False,
                            elem_id='director-status-box',
                        )
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

                    # [FORK] Digital-Union (Freestyle V1): section-type rules, collapsed, placed
                    # AFTER the global controls it layers over and BEFORE Variant Lab — the layout
                    # is the explanation: the sliders above are the base, these rules modulate it
                    # per section, and the lab below varies the base.
                    #
                    # Twelve components: an enable checkbox, ten section-type dropdowns and one
                    # read-only summary. No numeric per-section sliders, no Analyze Music button, no
                    # section-instance timeline, and deliberately no new top-level workflow.
                    #
                    # These widgets write NO global creative widget. They are also live inputs to
                    # both render events, which is what makes them execution authority rather than
                    # the summary state.
                    with gr.Accordion(label=LABEL_FREESTYLE, open=False):
                        gr.Markdown(INFO_FREESTYLE)
                        freestyle_enabled = gr.Checkbox(
                            value=False,
                            label=LABEL_FREESTYLE_ENABLED,
                            info=INFO_FREESTYLE_ENABLED,
                            elem_id='freestyle-enabled-checkbox',
                        )
                        # One dropdown per Stage-3 section type, declared explicitly rather than in
                        # a loop so each widget is visible to the per-widget seam assertions — the
                        # same reason the six slider bindings are written out one at a time.
                        # `Base` is the default and means "inherit the live global value".
                        freestyle_intro = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} intro',
                            elem_id='freestyle-intro')
                        freestyle_hook = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} hook',
                            elem_id='freestyle-hook')
                        freestyle_outro = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} outro',
                            elem_id='freestyle-outro')
                        freestyle_finale = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} finale',
                            elem_id='freestyle-finale')
                        freestyle_drop = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} drop',
                            elem_id='freestyle-drop')
                        freestyle_chorus = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} chorus',
                            elem_id='freestyle-chorus')
                        freestyle_bridge = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} bridge',
                            elem_id='freestyle-bridge')
                        freestyle_breakdown = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} breakdown',
                            elem_id='freestyle-breakdown')
                        freestyle_verse = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} verse',
                            elem_id='freestyle-verse')
                        freestyle_body = gr.Dropdown(
                            choices=list(fork_freestyle.SECTION_STYLE_CHOICES),
                            value=fork_freestyle.BASE_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} body',
                            elem_id='freestyle-body')
                        # Read-only, and formatted entirely by `freestyle.summary_text()` — one
                        # formatter per read-out, exactly as the Variant Lab, mix and Director
                        # reports work. It never prints an inherited number; see the handler.
                        freestyle_summary = gr.Textbox(
                            label=LABEL_FREESTYLE_SUMMARY,
                            value=fork_freestyle.summary_text(
                                fork_freestyle.FreestyleDeclaration()),
                            lines=6,
                            max_lines=16,
                            interactive=False,
                            elem_id='freestyle-summary',
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
                        # [FORK] Digital-Union (Variant Lab C3 V1): compare several candidates and
                        # apply one. Inside the *existing* accordion, below the single-variant
                        # flow, because it is the same workflow at a different width — not a
                        # second Variant Lab. Like every other lab widget these are read at click
                        # time and register nothing; the candidate selector deliberately has no
                        # `.change()` handler, since Apply reads its current value itself.
                        gr.Markdown(INFO_VARIANT_COMPARE)
                        variant_candidate_count = gr.Number(
                            label=LABEL_CANDIDATE_COUNT,
                            value=fork_batch.CANDIDATE_COUNT_DEFAULT,
                            precision=0,
                            minimum=fork_batch.CANDIDATE_COUNT_MIN,
                            maximum=fork_batch.CANDIDATE_COUNT_MAX,
                            info=INFO_CANDIDATE_COUNT,
                            elem_id='variant-candidate-count',
                        )
                        generate_variants_btn = gr.Button(
                            LABEL_GENERATE_VARIANTS, variant='secondary',
                            elem_id='generate-variants-button')
                        # Monospace-ish fixed-width rows produced entirely by
                        # `VariantBatch.table_text()` — one formatter per read-out, exactly as the
                        # two mix reports work. Thirteen lines holds the header, the column rule
                        # and the maximum twelve candidates without clipping a row.
                        variant_batch_table = gr.Textbox(
                            label=LABEL_VARIANT_BATCH_TABLE,
                            value='',
                            placeholder=PLACEHOLDER_VARIANT_BATCH_TABLE,
                            lines=13,
                            max_lines=16,
                            interactive=False,
                            elem_id='variant-batch-table',
                        )
                        # `(label, value)` choices again, and the value IS the candidate index —
                        # never the display string, which a reworded label could silently re-map
                        # onto a different candidate.
                        variant_candidate_selector = gr.Radio(
                            choices=[],
                            value=None,
                            label=LABEL_VARIANT_CANDIDATE,
                            info=INFO_VARIANT_CANDIDATE,
                            elem_id='variant-candidate-selector',
                        )
                        apply_variant_btn = gr.Button(
                            LABEL_APPLY_VARIANT, variant='secondary',
                            elem_id='apply-variant-button')
                        variant_batch_status = gr.Textbox(
                            label=LABEL_VARIANT_BATCH_STATUS,
                            value='',
                            placeholder=PLACEHOLDER_VARIANT_BATCH_STATUS,
                            lines=2,
                            max_lines=3,
                            interactive=False,
                            elem_id='variant-batch-status',
                        )
                        gr.Markdown(INFO_VARIANT_APPLY)
                        # [FORK] Digital-Union (C3-R0 -> C3-R1B-b): render 2 to 4 of the compared
                        # candidates. A SEPARATE selector from the Apply Radio above — one control
                        # cannot honestly mean both "apply this one" and "render these", and
                        # overloading it is how a user ends up rendering what they meant to apply.
                        # Like every other lab widget it registers nothing; the selection is read
                        # and validated at click time.
                        gr.Markdown(INFO_RENDER_SELECTED)
                        variant_render_selector = gr.CheckboxGroup(
                            choices=[],
                            # Empty by default and never pre-filled: committing two uninterruptible
                            # renders has to be something the user actively chose.
                            value=[],
                            label=LABEL_RENDER_CANDIDATES,
                            info=INFO_RENDER_CANDIDATES,
                            elem_id='variant-render-selector',
                        )
                        render_selected_variants_btn = gr.Button(
                            LABEL_RENDER_SELECTED, variant='secondary',
                            elem_id='render-selected-variants-button')
                        # Its own panel. `variant_batch_table` and `variant_batch_status` describe
                        # generation and comparison; making them carry render results too would
                        # leave the user unable to tell which statement was about which thing.
                        render_batch_summary = gr.Textbox(
                            label=LABEL_RENDER_BATCH_SUMMARY,
                            value='',
                            placeholder=PLACEHOLDER_RENDER_BATCH_SUMMARY,
                            # [FORK] Digital-Union (C3-R1B-b): widened from 10/20 because four
                            # candidates overflow it. `report_lines()` yields up to 4 lines per
                            # ATTEMPTED candidate, plus a 2-line header and a trailer of up to 3 --
                            # so 4 candidates reach ~21 lines and 20 would scroll a complete batch
                            # summary. A one-number widening, deliberately not a layout redesign:
                            # no gallery, no new component, one block per candidate as before.
                            lines=12,
                            max_lines=28,
                            interactive=False,
                            elem_id='render-batch-summary',
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

                # [FORK] Digital-Union (C3-R1A): deliberately ALWAYS interactive -- unlike
                # `process_btn`, this is never gated by the source-confirmation state, because a
                # render already in flight has nothing to do with whether a NEW one could start.
                # It is also never disabled while a render runs: that disabling is what would make
                # it useless, since it is the only way to reach a render already in progress.
                cancel_render_btn = gr.Button(LABEL_CANCEL_RENDER, variant='stop', size='sm')
                gr.Markdown(INFO_CANCEL_RENDER)

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

        # [FORK] Digital-Union (Freestyle V1): the Freestyle wiring — eleven summary refreshes and
        # nothing else.
        #
        # `freestyle_live_inputs` is the ONE definition of the live-widget order for the eleven
        # summary handlers. The two render registrations deliberately do NOT reuse it — they write
        # the same widgets out explicitly, because the positional seam tests read those `inputs`
        # lists' own `.elts` and a concatenation would hide the widgets from the very assertion
        # that keeps each list aligned with its handler signature. What keeps all four places in
        # step is `fork_freestyle.SECTION_TYPES`: `_FREESTYLE_WIDGET_ORDER` is it, the two render
        # signatures are pinned against it by test, and `auto_mode._resolve_freestyle` zips the
        # submitted tuple against it.
        freestyle_section_dropdowns = [
            freestyle_intro, freestyle_hook, freestyle_outro, freestyle_finale,
            freestyle_drop, freestyle_chorus, freestyle_bridge, freestyle_breakdown,
            freestyle_verse, freestyle_body,
        ]
        freestyle_live_inputs = [freestyle_enabled] + freestyle_section_dropdowns

        # Each widget refreshes the summary and writes NOTHING else. `.change()` is correct here
        # (unlike the preset/slider graph, which needs `.input()` to stay acyclic): nothing ever
        # writes a Freestyle widget programmatically, so there is no cycle to create — and a test
        # pins that absence. Registered explicitly per widget, not in a loop, so each binding is
        # visible to the per-widget seam assertions.
        freestyle_enabled.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_intro.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_hook.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_outro.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_finale.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_drop.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_chorus.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_bridge.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_breakdown.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_verse.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])
        freestyle_body.change(
            fn=_on_freestyle_change, inputs=freestyle_live_inputs, outputs=[freestyle_summary])

        # [FORK] Digital-Union (AI Director V1): the Director's entire wiring — two button clicks.
        #
        # Note what Generate's `outputs` does NOT contain: `variation_seed`, the six sliders,
        # `creative_preset`, any audio widget, `variant_master_seed`, `variant_batch_state`, any
        # Variant Lab or C3 widget, `source_outputs`, `prep_outputs` and `process_btn`. Generating
        # a proposal writes the proposal and the two read-outs, and that absence is what makes
        # "generating is not applying" structural rather than careful.
        #
        # Note also what Generate's `inputs` does not contain: everything except the instruction.
        # There is no hidden creative base, so the Director cannot drift from its own last answer
        # the way a transform-the-current-settings mode would.
        generate_director_btn.click(
            fn=_on_generate_director_proposal,
            inputs=[director_instruction],
            outputs=[director_proposal_state, director_proposal, director_status],
        )

        # Apply is the ONE Director writer of execution widgets, and the exact set is the
        # Variation Seed, the six sliders and the preset label — the same three things the preset
        # selector and Variant Lab already write between them, and nothing new. It reuses
        # `creative_control_sliders` rather than restating the six, so the positional contract with
        # `CREATIVE_CONTROL_FIELDS` has one definition. `_director_apply_outputs` is a small
        # Director-specific projection: `_variant_apply_outputs` also writes the lab's master seed,
        # the three audio levels and the lab report, none of which the Director generates.
        director_apply_outputs = [variation_seed] + creative_control_sliders + [
            creative_preset, director_status,
        ]

        apply_director_btn.click(
            fn=_on_apply_director_proposal,
            inputs=[director_proposal_state],
            outputs=director_apply_outputs,
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

        # [FORK] Digital-Union (Variant Lab C3 V1): generate / compare / apply one.
        #
        # Both C3 events reuse `variant_lab_inputs` verbatim rather than restating it, so the
        # declaration Apply re-derives is read from exactly the widgets Generate Variants read.
        # The candidate count leads each list, matching both handlers' parameter order, and Apply
        # additionally leads with the state and the selector it consumes.
        variant_batch_inputs = [variant_candidate_count] + variant_lab_inputs

        # Note what is absent from these outputs: every execution widget. Generating candidates
        # writes the root master seed back (so a freshly minted one is visible), the batch state,
        # the table, the selector and the status — and NOT the Variation Seed, the six sliders,
        # the preset label, the three audio levels or the Variant Lab report. That absence is what
        # makes candidate chaining structurally impossible, not merely avoided.
        variant_batch_outputs = [
            variant_master_seed, variant_batch_state, variant_batch_table,
            variant_candidate_selector, variant_render_selector, variant_batch_status,
        ]

        generate_variants_btn.click(
            fn=_on_generate_variants,
            inputs=variant_batch_inputs,
            outputs=variant_batch_outputs,
        )

        # Apply writes the ordinary Variant Lab output set — the same thirteen widgets a single
        # Generate writes, through the same projection helper — and then consumes the batch it
        # applied from. It is the only reader of `variant_batch_state`.
        apply_variant_btn.click(
            fn=_on_apply_selected_variant,
            inputs=[variant_batch_state, variant_candidate_selector] + variant_batch_inputs,
            outputs=variant_lab_outputs + [
                variant_batch_state, variant_candidate_selector, variant_render_selector,
                variant_batch_status],
        )

        # [FORK] Digital-Union (C3-R0 -> C3-R1B-b): render 2-4 compared candidates, sequentially.
        #
        # Its `inputs` are deliberately the batch state, the render selection and the
        # NON-candidate render intent only. The six creative sliders and the three audio levels
        # are absent on purpose: a batch's candidate values come from the stored recipes, and
        # reading them from the screen would mean rendering whichever candidate happened to be
        # applied rather than the two that were ticked.
        #
        # It shares `process_btn.click`'s concurrency group, and both are additionally serialized
        # by the process-global `_RENDER_LOCK`, which is the authority — `create_music_video`
        # clears one process-global processing dir per render, so two renders may never
        # overlap.
        render_selected_variants_btn.click(
            fn=render_selected_variants_guarded,
            inputs=[
                variant_batch_state, variant_render_selector,
                audio_input,
                voice_files, voice_start_delay, voice_min_gap, voice_avoid_drops,
                sfx_folder, sfx_roles,
                source_mode, source_folder, source_recursive, video_input,
                output_filename, processing_mode, custom_fps,
                session_state, source_state,
                # [FORK] Digital-Union (Freestyle V1): the same live widgets, appended at the end
                # and written out explicitly for the same inspectability reason. The batch freezes
                # ONE declaration from them before its candidate loop, so every selected candidate
                # renders under identical section rules and a mid-batch dropdown edit cannot reach
                # a later candidate — exactly how audio, source, output, encoder and FPS behave.
                freestyle_enabled,
                freestyle_intro, freestyle_hook, freestyle_outro, freestyle_finale,
                freestyle_drop, freestyle_chorus, freestyle_bridge, freestyle_breakdown,
                freestyle_verse, freestyle_body,
            ],
            outputs=[video_output, status_output, session_state, render_batch_summary,
                     render_invocation_state],
            show_progress='hidden',
            concurrency_id=RENDER_CONCURRENCY_ID,
            concurrency_limit=1,
        )

        # [FORK] Digital-Union (C3-R1A): the Cancel button. Its ENTIRE input is the plain string
        # invocation id, never `session_state` or any report widget, and its entire output is the
        # status line -- it writes no execution widget, no report panel and no state object. A
        # dedicated concurrency lane (never `RENDER_CONCURRENCY_ID`) is what lets it actually run
        # while a render is in progress instead of queuing uselessly behind it.
        cancel_render_btn.click(
            fn=_on_cancel_render_click,
            inputs=[render_invocation_state],
            outputs=[status_output],
            show_progress='hidden',
            concurrency_id=CANCEL_CONCURRENCY_ID,
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
                session_state, source_state,
                # [FORK] Digital-Union (Freestyle V1): the LIVE Freestyle widgets, appended at the
                # END so every pre-existing positional index is unchanged. Written out explicitly
                # rather than as `+ freestyle_live_inputs`: the positional seam test reads this
                # list's own `.elts`, so a concatenation would hide the widgets from the very
                # assertion that keeps `inputs` and the handler signature aligned.
                #
                # These are the render's execution authority for section rules. The summary state
                # is a read-out and is deliberately absent here.
                freestyle_enabled,
                freestyle_intro, freestyle_hook, freestyle_outro, freestyle_finale,
                freestyle_drop, freestyle_chorus, freestyle_bridge, freestyle_breakdown,
                freestyle_verse, freestyle_body,
            ],
            # [FORK] Digital-Union (Audio Layers V1 / D, R1-B; Smart Mix V1 / E): both read-outs are
            # written here and nowhere else. They are outputs only — never inputs, never source or
            # preparation state, and never consulted by the pipeline.
            outputs=[video_output, status_output, session_state,
                     audio_layers_report, smart_mix_report, render_invocation_state],
            show_progress='hidden',
            # [FORK] Digital-Union (C3-R0): the same concurrency group as the batch render event,
            # so Gradio queues them together instead of giving each listener its own lane. The
            # process-global `_RENDER_LOCK` remains the authority; this is cooperative UX.
            concurrency_id=RENDER_CONCURRENCY_ID,
            concurrency_limit=1,
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
