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
# [FORK] Digital-Union (Deliverable Export V1-B-S1): the subordinate share publication verifies its
# staged copy by re-reading it, so it needs a digest. Stdlib only, like everything else here.
import hashlib
import html as html_lib
# [FORK] Digital-Union (R5-P3): used to emit operator text into the gallery script as a properly
# escaped JS string literal. Interpolating it bare is how an apostrophe in the wording broke the
# whole script's compilation.
import json
import multiprocessing
import queue
import re
import secrets
import subprocess
import threading
import time
from collections import OrderedDict
import socket
import uuid
from typing import Callable, Iterator, TypeAlias, Tuple, Dict, List

# Import FFmpeg processing module
from ffmpeg_processing import (
    get_video_fps, run_cancellable_media_command,
    # [FORK] Digital-Union (Deliverable Export V1-A): the delivery-preview argv lives in
    # `ffmpeg_processing` with every other FFmpeg command, so it is unit-testable without Gradio
    # and the GUI gains no second command-construction site. `FFMPEG_PATH` is no longer imported
    # here: the preview was the last consumer, and the builder resolves it from its own module.
    build_delivery_preview_command, DELIVERY_PREVIEW_TIMEOUT_SECONDS,
)

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
    get_processing_dir,
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
# [FORK] Digital-Union (Random Freestyle V2): the seeded per-section generator. A sibling module so
# freestyle.py keeps its pinned import set; it adds one dropdown choice and generates the five
# numbers for every section left on it. A seeded generator, never a model.
from beatsync_fork import freestyle_random as fork_freestyle_random
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
# [FORK] Digital-Union (Raw Cutter V1): the pure segmentation planner. The FFmpeg adapter
# (src/raw_cutter_export.py) is imported INSIDE the two handlers rather than here, deliberately:
# it is only reached when the operator presses a Raw Cutter button, and a module-level import
# would add an FFmpeg-touching dependency to every GUI start for a tool most sessions never open.
from beatsync_fork import raw_cutter as fork_cutter
# [FORK] Digital-Union (R5 Clips Preview Selector): the stdlib-only secure core --
# opaque ids, the authorization gates, descriptor-identity revalidation, Range
# parsing, path-relationship refusal, the decision journal and the gallery markup.
# [FORK] Digital-Union (R6): Raw Cutter output video profiles.
from beatsync_fork import raw_cutter_profiles as fork_profiles
from beatsync_fork import clips_review as fork_clips
# [FORK] Digital-Union (UX V4): the native folder dialog and the remembered folder choices. Both
# are stdlib-only fork modules; the dialog's subprocess lives there rather than here because two
# suites pin this module's `subprocess.run` site count at exactly two.
from beatsync_fork import desktop_folders as fork_folders
from beatsync_fork import user_prefs as fork_prefs
# [FORK] Digital-Union (R8 UX Refresh): what to do about a taken destination filename, and the
# cache inspector. Both are stdlib-only and pure enough to be unit-tested without a disk: the
# collision helper takes the caller's rename as an argument, and the cache inspector is handed its
# roots and never discovers one. Neither launches a process, so this module's `subprocess.run`
# site count stays at exactly the two that two suites pin.
from beatsync_fork import collision as fork_collision
from beatsync_fork import cache_admin as fork_cache
# [FORK] Digital-Union (R8 UX Refresh, Commit C): the one composition rule for "what is this
# render actually going to do creatively". It is a pure view over values this module already has,
# and it delegates every verdict -- the preset name to `presets.matching_preset`, Freestyle
# activity to the declaration, the SFX verdict to the caller's `plans_anything_from` result -- so
# the render tab's summary and the selector on Creative Settings are the same computation.
from beatsync_fork import effective_profile as fork_effective
# [FORK] Digital-Union (R8 UX Refresh, Phase 6): the Output Video Profile table and its pure
# geometry. `resolve` answers None for Auto and every helper returns its fallback unchanged there,
# so the historical render path is structurally unreachable from a profile rather than merely
# preserved alongside one.
from beatsync_fork import render_profiles as fork_render_profiles
# [FORK] Digital-Union (R7 Raw Audio Cutter V1): the silence planner, the FFmpeg argv builders and
# the filing rules. Stdlib-only fork modules, so they import at module scope; both process launches
# live inside `audio_cutter` rather than here, because two suites pin this module's
# `subprocess.run` site count at exactly two.
from beatsync_fork import audio_cutter as fork_audio
from beatsync_fork import audio_library as fork_audio_library
# [FORK] Digital-Union (R7.2): the ONE audio review core — enumeration, paging, the tile markup,
# the decision vocabulary, the sort plan and the destination rules — shared by the Raw Audio
# Cutter's review half and the standalone Audio Preview tab. Stdlib-only like its siblings.
from beatsync_fork import audio_review as fork_audio_review
# [FORK] Digital-Union (R5): the ASGI media endpoint and Starlette's middleware wrapper. The
# wrapper is a Gradio dependency already, so this adds nothing to requirements.
from clips_media_server import ClipMediaMiddleware
# [FORK] Digital-Union (R7.2): the audio sibling of that endpoint. One route, GET/HEAD, range
# capable, reusing R5's registry and its six authorization gates unmodified.
from audio_media_server import AudioMediaMiddleware
from starlette.middleware import Middleware
# [FORK] Digital-Union (Raw Cutter Batch V1): the pure batch bookkeeping — the outcome vocabulary,
# the collision-safe output layout, the totals, the manifest payload and every line of report text.
# Stdlib-only like its sibling, so it imports here at module scope; only the FFmpeg adapter is
# deferred into the handlers.
from beatsync_fork import raw_cutter_batch as fork_cutter_batch
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

# [FORK] Digital-Union (AI Director V2): the Director's runtime assets.
#
# A SEPARATE text-only GGUF from Stage 5's — see `DIRECTOR_MODEL` below for why the shared 2B model
# could not stay. No `mmproj` is loaded and no image argument is passed: **the model** is blind to
# the footage, and V2's one media fact never travels through it. Stage 5's worker is deliberately
# NOT reused either: it exists to batch frames through a persistent `llama-server` and to write a
# semantic response file, which is a different contract from one bounded 320-token JSON answer.
# Reusing it would have meant teaching a media-semantics worker about creative intent — exactly the
# leak `.claude/rules/stage5-worker.md` forbids.
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
#: **Director V2's own model, and deliberately not Stage 5's.** V1 reused the installed
#: `Qwen3VL-2B-Instruct-Q8_0.gguf` text-only, and that model was then measured against the V2 intent
#: contract and **failed** it: on the frozen 14-intention matrix it reached 8/14 with 0/4 of the
#: downward requests understood, and it could not read an ordinary sentence like "Keep scene choice
#: relatively even across sections" as a request at all. `Qwen3-4B-Instruct-2507` reached 14/14
#: concepts, 6/6 on that cluster and 12/12 on an unseen holdout, at a ~3.5 s median one-shot cost.
#:
#: So there are now two Qwen assets with two different jobs, and they must not be conflated:
#: Stage 5 keeps the **vision** model plus its `mmproj` for media semantics, and the Director gets
#: this **text-only** 4B. There is no `mmproj` here and no image argument: the Director is blind to
#: the footage, and the one media fact V2 uses never travels through the model at all.
#:
#: There is deliberately **no fallback to the 2B model** when this file is missing — see
#: `director.missing_runtime_status`. Silently substituting a model that failed the contract would
#: produce confident wrong recipes instead of an honest "run the installer".
DIRECTOR_MODEL = os.path.join(ROOT_DIR, 'bin', 'models', 'qwen3-4b-instruct-2507-q8_0.gguf')

# [FORK] Digital-Union (P V1): media library preparation. All state, classification vocabulary and
# report rendering live in src/beatsync_fork/library_prep.py (stdlib-only, Gradio-free); this module
# only wires it to widgets and supplies the runtime calls it must not make itself.
from beatsync_fork import library_prep as fork_prep
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source verification (stdlib-only fork module).
from beatsync_fork import media_verify as fork_media_verify
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the accepted sample-decode core. Stdlib-only and
# therefore import-safe: it spawns nothing, reads no cache and adds no import-time subprocess
# activity. The EXECUTION seam (`probe_media_metadata` / `sample_decode_video`) is imported
# locally in the impl, mirroring `_verify_sources_impl`.
from beatsync_fork import media_sample as fork_media_sample
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

# [FORK] Digital-Union (P19-R4): make the two Raw Cutter Cancel controls actually RED.
#
# `variant='stop'` is NOT a guarantee of red. Measured in the pinned Gradio 6.19.0 + Ocean theme:
# the stop variant paints `background-image: linear-gradient(120deg, #52525b ...)` over a
# `background-color` of `rgba(0,0,0,0)` — a neutral grey. An emergency control that looks like
# every other button is one an operator does not reach for, which is why this is written against
# the measured DOM rather than against the variant's name.
#
# Three measured facts this depends on, each of which would have been wrong if guessed:
#   * `elem_id` lands on the `<button>` ITSELF here, not on a wrapper — so `button#id`, never
#     `#id button`.
#   * the theme paints with `background-image`, so overriding `background-color` alone would be
#     invisible underneath the gradient.
#   * Quick Render's "Cancel Active Render" carries the SAME `stop` class and has no `elem_id`.
#     Scoping by id is therefore what keeps this off every other button in the app; a `.stop`
#     selector would have restyled a render control this task has no business touching.
#
# Colour is never the only state indicator: the label text and Gradio's own `disabled` attribute
# both remain, and the disabled rule below keeps the subdued treatment the theme already applies.
CUT_CANCEL_CSS = """
button#cut-cancel-button,
button#cut-batch-cancel-button {
    background-image: linear-gradient(120deg, #a32015 0%, #c5362c 60%, #a32015 100%) !important;
    background-color: #c5362c !important;
    border-color: #ff8a80 !important;
    color: #ffffff !important;
    font-weight: 600 !important;
}

button#cut-cancel-button:not(:disabled):hover,
button#cut-batch-cancel-button:not(:disabled):hover {
    background-image: linear-gradient(120deg, #b8291d 0%, #d4423a 60%, #b8291d 100%) !important;
    border-color: #ffb3ab !important;
}

button#cut-cancel-button:focus-visible,
button#cut-batch-cancel-button:focus-visible {
    outline: 3px solid #ffd5d5 !important;
    outline-offset: 2px !important;
}

button#cut-cancel-button:disabled,
button#cut-batch-cancel-button:disabled {
    background-image: none !important;
    background-color: rgba(163, 32, 21, 0.20) !important;
    border-color: rgba(163, 32, 21, 0.45) !important;
    color: rgba(255, 255, 255, 0.55) !important;
    cursor: not-allowed !important;
}
"""

# [FORK] Digital-Union (R5 Clips Preview Selector, P1): the gallery grid.
#
# `minmax(300px, 1fr)` is chosen for the specification's 3-4 columns per desktop row, not the
# five the R5-P0 prototype produced at 1448 px with a 250 px minimum. An explicit cap of four
# columns keeps a very wide monitor from drifting to five, and the floor keeps a narrow window
# from collapsing to one.
#
# Colour is never the only status signal: every tile also carries a badge glyph, its `aria-label`
# names the decision in words, and focus is a visible ring rather than a colour change.
CLIPS_GALLERY_CSS = """
#clips-gallery .r5-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 14px;
}
@media (min-width: 1500px) {
    #clips-gallery .r5-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
#clips-gallery .r5-tile {
    position: relative;
    border: 3px solid #8a8a8a;
    border-radius: 10px;
    overflow: hidden;
    background: #16181c;
    cursor: pointer;
    outline: none;
}
#clips-gallery .r5-tile:focus-visible {
    outline: 3px solid #4da3ff;
    outline-offset: 2px;
}
#clips-gallery .r5-media {
    position: relative;
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #000;
}
#clips-gallery .r5-poster,
#clips-gallery .r5-slot video {
    width: 100%; height: 100%; object-fit: cover; display: block;
}
#clips-gallery .r5-slot { position: absolute; inset: 0; }
#clips-gallery .r5-nopreview {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    height: 100%; text-align: center; gap: 4px;
}
#clips-gallery .r5-nopreview-main { color: #ffb4b4; font-size: 12px; }
#clips-gallery .r5-nopreview-sub { color: #8a8a8a; font-size: 10px; }
#clips-gallery .r5-bar {
    display: flex; align-items: center; gap: 8px; padding: 5px 8px; font-size: 12px;
    color: #dcdcdc;
}
#clips-gallery .r5-label { flex: 1; font-family: monospace; }
#clips-gallery .r5-badge { font-weight: 700; font-size: 15px; }
#clips-gallery .r5-reset {
    background: #2c2f36; color: #cfcfcf; border: 1px solid #555; border-radius: 5px;
    cursor: pointer; font-size: 11px; line-height: 1; padding: 3px 6px;
}
#clips-gallery .r5-reset:focus-visible { outline: 2px solid #4da3ff; outline-offset: 1px; }
#clips-gallery .r5-pending { border-color: #d8b11a; }
#clips-gallery .r5-pending .r5-badge { color: #d8b11a; }
#clips-gallery .r5-selected { border-color: #2ea043; background: #10240f; }
#clips-gallery .r5-selected .r5-badge { color: #46d16a; }
#clips-gallery .r5-rejected { border-color: #d02828; background: #26100f; }
#clips-gallery .r5-rejected .r5-badge { color: #ff6b6b; }
#clips-gallery .r5-empty { padding: 24px; color: #8a8a8a; font-style: italic; }
"""

# [FORK] Digital-Union (R5 Clips Preview Selector, P1): gallery interaction.
#
# Decisions POST to `/r5decide`, the server's own authorized endpoint, and NOT through a Gradio
# event. That is a measured constraint, not a preference: on the pinned gradio 6.19.0, setting a
# hidden Textbox's value and dispatching `input` does not make Gradio fire its `.input()` event
# -- no request is issued at all -- so the technique that worked in Gradio 4 is simply gone. The
# endpoint is also the better shape here: a click does not queue a Gradio event, and the decision
# travels through the same authorization the media bytes already pass.
#
# A tile is repainted ONLY from a server response. The DOM is a rendering of the decision record,
# never the record itself.
# [FORK] Digital-Union (R7.1, rebuilt in R7.2): the audio galleries' interaction layer.
#
# Deliberately independent of CLIPS_GALLERY_JS rather than a generalisation of it. That one talks
# to a durable decision journal and posts every classification to the server; these galleries keep
# their decisions in the page until Confirm. They now share an authorised media endpoint, but that
# is one URL in a dataset attribute, not shared behaviour -- merging the two clients would make
# both harder to reason about for no gain.
#
# The hidden field named by the grid's `data-store` is the whole server contract: every decision and
# every edited name lives in it as JSON, and Confirm reads nothing else from the browser.
#
# R7.2 changed three things, all of them consequences of the audio no longer being in the page:
#
# 1. **ONE <audio> element exists for the whole document**, created on first hover and re-pointed
#    at whichever tile the pointer is over. R7.1 had one per tile, pre-loaded with base64 bytes; a
#    page of 60 clips therefore carried 60 decoded buffers and was capped by a byte budget that made
#    valid clips unplayable. Now the page holds markup only and the bytes arrive on demand.
# 2. **Tiles are addressed per grid.** Two galleries exist in R7.2 and both render a `.r7-grid`, so
#    every lookup starts from the grid the tile belongs to and the hidden field and counts box are
#    read off that grid's `data-store` / `data-counts`. A document-wide `#audio-decisions` lookup
#    would make the Audio Preview tab write the Audio Cutter's decisions.
# 3. **A failure to play is reported as what it is.** There is no loading limit any more, so the
#    only remaining failures are real ones -- the fetch was refused, or the browser cannot decode
#    the bytes -- and `MediaError` distinguishes them. A clip that will not play stays classifiable.
AUDIO_GALLERY_JS = r"""
() => {
  if (window.__r7Installed) { return; }
  window.__r7Installed = true;

  const PENDING = 'PENDING', SELECTED = 'SELECTED', REJECTED = 'REJECTED';
  const CLS = {PENDING: 'r7-pending', SELECTED: 'r7-selected', REJECTED: 'r7-rejected'};
  const BADGE = {PENDING: '?', SELECTED: '\u2713', REJECTED: '\u2717'};

  let armed = true;          // false once the browser has refused an audible play()
  let player = null;         // the ONE <audio> in the document, created on first hover
  let holder = null;         // the tile `player` is currently pointed at, if any

  // Seconds per wheel notch, scaled to the clip -- the same convention the Clips Preview uses, so
  // the two galleries do not behave differently under the same gesture. 1s under 20s, 2s up to and
  // including 90s, 5s beyond.
  function seekStep(duration) {
    if (!isFinite(duration) || duration <= 0) { return 2; }
    if (duration < 20) { return 1; }
    if (duration <= 90) { return 2; }
    return 5;
  }

  function gridOf(tile) { return tile && tile.closest ? tile.closest('.r7-grid') : null; }
  function tilesIn(grid) {
    return grid ? Array.from(grid.querySelectorAll('.r7-tile')) : [];
  }
  function grids() { return Array.from(document.querySelectorAll('.r7-grid')); }
  function stateOf(tile) { return tile ? tile.querySelector('.r7-state') : null; }

  function fieldIn(elemId) {
    if (!elemId) { return null; }
    const host = document.getElementById(elemId);
    return host ? (host.querySelector('textarea') || host.querySelector('input')) : null;
  }

  // Gradio tracks a textbox's value from the native setter plus an `input` event. Setting
  // `.value` directly does NOT update its internal store, which is why the prototype descriptor
  // is used -- measured in R7.1 and unchanged.
  function writeField(box, text) {
    if (!box || box.value === text) { return; }
    const proto = box.tagName === 'TEXTAREA'
      ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(box, text);
    box.dispatchEvent(new Event('input', {bubbles: true}));
  }

  // ---- the hidden field is the only thing the server reads -----------------------------------
  function publish(grid) {
    if (!grid) { return; }
    const field = fieldIn(grid.dataset.store);
    if (!field) { return; }
    const decisions = {}, names = {};
    tilesIn(grid).forEach(tile => {
      const key = tile.dataset.idx;
      decisions[key] = tile.dataset.decision || PENDING;
      const box = tile.querySelector('input.r7-name');
      if (box) { names[key] = box.value; }
    });
    writeField(field, JSON.stringify({decisions: decisions, names: names}));
    paintCounts(grid);
  }

  function paintCounts(grid) {
    if (!grid) { return; }
    const box = fieldIn(grid.dataset.counts);
    if (!box) { return; }
    let keep = 0, drop = 0, pend = 0;
    const all = tilesIn(grid);
    all.forEach(tile => {
      const d = tile.dataset.decision || PENDING;
      if (d === SELECTED) { keep++; } else if (d === REJECTED) { drop++; } else { pend++; }
    });
    let text = 'Keep ' + keep + '   \u00b7   Discard ' + drop
             + '   \u00b7   Undecided ' + pend + '   \u00b7   Total ' + all.length;
    // [R7.2] Confirm needs one kept clip, not a fully decided page. Saying so here is display
    // text only: it enables nothing, and the server re-derives the whole plan from its own
    // record before it moves anything.
    if (keep === 0) { text += '   \u2014   mark at least one clip to Keep'; }
    else if (pend > 0) {
      text += '   \u2014   ' + pend + ' unmarked will be Discarded unless you mark them';
    }
    writeField(box, text);
  }

  function classify(tile, decision) {
    if (!tile) { return; }
    tile.dataset.decision = decision;
    Object.keys(CLS).forEach(key => tile.classList.remove(CLS[key]));
    tile.classList.add(CLS[decision]);
    const badge = tile.querySelector('.r7-badge');
    if (badge) { badge.textContent = BADGE[decision]; }
    publish(gridOf(tile));
  }

  // ---- playback: ONE element, created on demand, pointed at one tile at a time ---------------
  function ensurePlayer() {
    if (player) { return player; }
    player = document.createElement('audio');
    player.className = 'r7-audio';
    // `metadata` is enough to answer `duration`, which Shift+wheel needs; the endpoint serves
    // byte ranges, so seeking fetches only the part it needs rather than the whole clip.
    player.preload = 'metadata';
    player.loop = true;
    player.style.display = 'none';
    // A failure now means something real: the page no longer declines to load anything.
    player.addEventListener('error', () => {
      const tile = holder;
      if (!tile) { return; }
      tile.classList.remove('r7-playing');
      tile.classList.add('r7-failed');
      const label = stateOf(tile);
      const code = player.error ? player.error.code : 0;
      let why = 'cannot be played';
      if (code === 2) { why = 'the server refused it \u2014 rescan'; }
      else if (code === 3) { why = 'the audio could not be decoded'; }
      else if (code === 4) { why = 'this format is not playable here'; }
      if (label) { label.textContent = why; }
    }, true);
    document.body.appendChild(player);
    return player;
  }

  function stop() {
    if (!player) { holder = null; return; }
    try {
      player.pause();
      player.currentTime = 0;
    } catch (e) {}
    // Releasing the source matters twice over: it ends the HTTP fetch, and on Windows it releases
    // the handle the server opened -- `os.rename` will not move a file another handle still has
    // open, so the clip the operator just listened to is exactly the one that would refuse to
    // sort. `removeAttribute` then `load()` is what actually drops it; setting src='' re-requests
    // the page URL in Chrome.
    try {
      player.removeAttribute('src');
      player.load();
    } catch (e) {}
    if (holder) {
      holder.classList.remove('r7-playing');
      const label = stateOf(holder);
      if (label && !holder.classList.contains('r7-failed')) {
        label.textContent = armed ? 'hover to play' : 'click Enable hover audio';
      }
    }
    holder = null;
  }

  function play(tile) {
    const url = tile ? tile.dataset.audio : '';
    if (!url) { return; }
    stop();                                   // never two at once
    const audio = ensurePlayer();
    holder = tile;
    tile.classList.remove('r7-failed');
    tile.classList.add('r7-playing');
    audio.muted = false;                      // the operator needs to HEAR it; never a silent fake
    audio.volume = 1.0;
    audio.src = url;
    const label = stateOf(tile);
    if (label) { label.textContent = 'loading\u2026'; }
    const started = audio.play();
    if (started && typeof started.catch === 'function') {
      started.catch(error => {
        if (holder !== tile) { return; }                 // the pointer already moved on
        if (error && error.name === 'AbortError') { return; }
        if (error && error.name === 'NotAllowedError') {
          // Chrome refuses audible autoplay before a user gesture. Say so and offer the one click
          // that fixes it -- deliberately NOT falling back to muted playback, which would look
          // like it worked while the operator heard nothing.
          armed = false;
          tile.classList.remove('r7-playing');
          if (label) { label.textContent = 'click Enable hover audio'; }
          document.querySelectorAll('.r7-enable-hover').forEach(b => {
            b.classList.add('r7-needed');
          });
          return;
        }
        // Anything else is a genuine load or decode failure; the `error` listener names it.
        tile.classList.remove('r7-playing');
        tile.classList.add('r7-failed');
        if (label && label.textContent === 'loading\u2026') {
          label.textContent = 'cannot be played';
        }
      });
    }
    if (label) {
      audio.addEventListener('playing', () => {
        if (holder === tile && label) { label.textContent = 'playing\u2026'; }
      }, {once: true});
    }
  }

  // ---- events ---------------------------------------------------------------------------------
  document.addEventListener('mouseover', event => {
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile || !gridOf(tile)) { return; }
    if (event.target.classList && event.target.classList.contains('r7-name')) { return; }
    if (holder === tile) { return; }
    play(tile);
  }, true);

  document.addEventListener('mouseout', event => {
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile) { return; }
    const to = event.relatedTarget;
    if (to && to.closest && to.closest('.r7-tile') === tile) { return; }   // still inside
    if (holder === tile) { stop(); }
  }, true);

  document.addEventListener('click', event => {
    const banner = event.target.closest
      ? event.target.closest('.r7-enable-hover') : null;
    if (banner) {
      // This click IS the gesture Chrome wanted.
      armed = true;
      document.querySelectorAll('.r7-enable-hover').forEach(b => {
        b.classList.remove('r7-needed');
      });
      const first = tilesIn(grids()[0])[0];
      if (first) { play(first); setTimeout(stop, 400); }
      return;
    }
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile) { return; }
    if (event.target.classList && event.target.classList.contains('r7-name')) { return; }
    classify(tile, SELECTED);
  }, true);

  document.addEventListener('contextmenu', event => {
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile) { return; }
    if (event.target.classList && event.target.classList.contains('r7-name')) { return; }
    event.preventDefault();
    classify(tile, REJECTED);
  }, true);

  document.addEventListener('auxclick', event => {
    if (event.button !== 1) { return; }
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile) { return; }
    event.preventDefault();
    classify(tile, PENDING);
  }, true);

  document.addEventListener('keydown', event => {
    const tile = document.activeElement && document.activeElement.closest
      ? document.activeElement.closest('.r7-tile') : null;
    if (!tile) { return; }
    if (document.activeElement.classList
        && document.activeElement.classList.contains('r7-name')) { return; }
    const key = event.key.toLowerCase();
    if (key === 's') { event.preventDefault(); classify(tile, SELECTED); }
    else if (key === 'r') { event.preventDefault(); classify(tile, REJECTED); }
    else if (key === 'p') { event.preventDefault(); classify(tile, PENDING); }
    else if (key === 'arrowright' || key === 'arrowleft'
             || key === 'arrowdown' || key === 'arrowup') {
      const all = tilesIn(gridOf(tile));
      const at = all.indexOf(tile);
      if (at < 0) { return; }
      event.preventDefault();
      const step = (key === 'arrowright' || key === 'arrowdown') ? 1 : -1;
      const next = all[Math.min(all.length - 1, Math.max(0, at + step))];
      if (next) { next.focus(); play(next); }
    }
  }, true);

  // Shift+wheel seeks the tile under the pointer, which is by construction the one holding the
  // player. `{passive:false}` is needed or preventDefault is ignored; Chrome re-routes a shifted
  // wheel to deltaX on some devices, so whichever axis moved is read.
  document.addEventListener('wheel', event => {
    if (!event.shiftKey) { return; }
    const tile = event.target.closest ? event.target.closest('.r7-tile') : null;
    if (!tile || holder !== tile || !player) { return; }
    const moved = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
    if (!moved) { return; }
    event.preventDefault();
    // `duration` is NaN until metadata lands; fall back to the length the SERVER measured, which
    // the tile carries, so a seek works on the first notch instead of being silently dropped.
    let duration = player.duration;
    if (!isFinite(duration) || duration <= 0) {
      duration = parseFloat(tile.dataset.dur || '0');
    }
    if (!isFinite(duration) || duration <= 0) { return; }
    const step = seekStep(duration) * (moved > 0 ? 1 : -1);
    try {
      player.currentTime = Math.min(duration, Math.max(0, player.currentTime + step));
    } catch (e) {}
  }, {passive: false, capture: true});

  document.addEventListener('input', event => {
    if (event.target.classList && event.target.classList.contains('r7-name')) {
      publish(gridOf(event.target));
    }
  }, true);

  // A repaint replaces a grid, so its hidden field is re-published from whatever the server
  // rendered. Keyed on the NONCE rather than a seen-flag: a repaint that happens to produce the
  // same tiles (a Cancel, or a partial sort) still carries a fresh nonce and must still
  // re-publish, because the server's idea of the decisions may have moved.
  const observer = new MutationObserver(() => {
    const live = grids();
    if (!live.length) { stop(); return; }
    live.forEach(grid => {
      if (grid.dataset.r7Nonce === grid.dataset.nonce) { return; }
      grid.dataset.r7Nonce = grid.dataset.nonce;
      if (holder && !document.body.contains(holder)) { stop(); }
      tilesIn(grid).forEach(tile => {
        if (!tile.dataset.decision) { tile.dataset.decision = PENDING; }
      });
      publish(grid);
    });
  });
  observer.observe(document.body, {childList: true, subtree: true});

  // Releasing the player before a move is the difference between a clip filing and a winerror 32.
  // Capture phase, so it runs before Gradio sends the click. Matched by CLASS, so every button
  // that can cause a move -- both tabs' Confirm, and Proceed -- is covered without this list
  // having to be kept in step with the markup.
  document.addEventListener('pointerdown', event => {
    const button = event.target.closest ? event.target.closest('.r7-release-audio') : null;
    if (button) { stop(); }
  }, true);
}
"""

CLIPS_GALLERY_JS = r"""
() => {
  if (window.__r5Installed) { return; }
  window.__r5Installed = true;
  const CLS = {PENDING: 'r5-pending', SELECTED: 'r5-selected', REJECTED: 'r5-rejected'};
  const BADGE = {PENDING: '?', SELECTED: '✓', REJECTED: '✗'};
  let active = null;          // the single mounted <video>, if any
  let lastNonce = null;
  let readyState = null;      // last observed "every clip decided" answer, or null if unknown
  let decideSeq = 0;          // monotonic stamp so a slow reply cannot repaint newer counts
  let syncTimer = null;

  // [UX] Seconds per wheel notch, scaled to the clip. A fixed 2s crawls through a two-minute
  // take and overshoots a ten-second one, so the step follows the length: 1s under 20s, 2s up to
  // and including 90s, 5s beyond. Both boundaries are deliberately 2s.
  function seekStep(duration) {
    if (!Number.isFinite(duration) || duration <= 0) { return null; }
    if (duration < 20) { return 1; }
    if (duration <= 90) { return 2; }
    return 5;
  }

  const grid = () => document.querySelector('#clips-gallery .r5-grid');
  const tileOf = (el) => (el && el.closest) ? el.closest('#clips-gallery .r5-tile') : null;

  function paint(clip, decision, rev) {
    const tile = document.querySelector('#clips-gallery .r5-tile[data-clip="' + clip + '"]');
    if (!tile) { return; }
    tile.classList.remove('r5-pending', 'r5-selected', 'r5-rejected');
    tile.classList.add(CLS[decision] || 'r5-pending');
    if (rev !== undefined && rev !== null) { tile.dataset.rev = String(rev); }
    const badge = tile.querySelector('.r5-badge');
    if (badge) { badge.textContent = BADGE[decision] || '?'; }
    const label = tile.querySelector('.r5-label');
    if (label) {
      tile.setAttribute('aria-label', label.textContent + ', ' + String(decision).toLowerCase());
    }
  }

  function countsBox() {
    return document.querySelector('#clips-counts-box textarea, #clips-counts-box input');
  }

  function paintCounts(counts) {
    const box = countsBox();
    if (!box || !counts) { return; }
    let text = 'Kept ' + counts.SELECTED + '   ·   Discarded ' + counts.REJECTED +
               '   ·   Undecided ' + counts.PENDING + '   ·   Total ' + counts.TOTAL;
    // [R5-P3 / F8] Decisions do not travel through a Gradio event, so this response cannot
    // change the Confirm button's enabled state -- only Refresh can, and it derives that state
    // from the durable record. Rather than leave the operator looking at "Undecided 0" beside a
    // greyed-out Confirm with no explanation, say what is needed. This is display text only: it
    // never enables anything, and the authority stays on the server.
    // [FORK] R8 Final Polish: readiness is "at least one clip KEPT", matching the server's
    // `_clips_render_current`. The two must agree or the button lights at the wrong moment --
    // and the server is the authority either way, so a disagreement here only ever costs an
    // extra read or a late enable, never a wrong enable.
    if (counts.TOTAL > 0 && counts.SELECTED > 0) { text += '\n' + __REFRESH_HINT__; }
    box.value = text;
    syncConfirm(counts.TOTAL > 0 && counts.SELECTED > 0);
  }

  // [UX] Ask the server to re-derive the Confirm state, but only when the all-decided answer
  // actually flips. The client still decides NOTHING here: it presses the same Refresh control
  // the operator used to press by hand, and that handler reads `store.counts()`. So the worst a
  // wrong guess on this side can do is cause one extra server read -- it can never turn Confirm
  // on, because nothing in this file is permitted to touch the button at all.
  //
  // Only transitions trigger it, so deciding the 2nd of 40 clips sends nothing; and a 120ms
  // coalescing window keeps a burst of fast clicks to a single read. The stamp comparison in
  // `send` means an out-of-order reply cannot drive this with stale counts.
  function syncConfirm(ready) {
    if (ready === readyState) { return; }
    readyState = ready;
    if (syncTimer) { clearTimeout(syncTimer); }
    syncTimer = setTimeout(() => {
      syncTimer = null;
      const button = document.querySelector('#clips-refresh-button');
      if (button) { button.click(); }
    }, 120);
  }

  function sayProblem(message) {
    const box = countsBox();
    if (box) { box.value = message; }
  }

  // [R5-P3 / F2] The WHOLE attribute must be a non-negative integer. `parseInt` is not usable
  // here: it accepts a numeric prefix ("3abc" -> 3) and returns NaN for anything else, and
  // `JSON.stringify` turns NaN into `null`, which the server used to read as "no concurrency
  // check requested". So a tampered `data-rev` disabled the very guarantee it was meant to feed.
  function validRevision(text) {
    if (typeof text !== 'string' || !/^(0|[1-9][0-9]*)$/.test(text)) { return null; }
    const value = Number(text);
    return Number.isSafeInteger(value) && value >= 0 ? value : null;
  }

  async function send(clip, to, rev) {
    const g = grid();
    if (!g) { return; }
    // [UX] Two decisions in flight can come back in either order, and the loser used to repaint
    // the counter with its own older totals. The tile paint below is still applied either way --
    // it is about one clip and the server answered for that clip -- but the page-wide counts, and
    // the Confirm sync derived from them, follow the newest reply only.
    const stamp = ++decideSeq;
    let res, body;
    try {
      res = await fetch('/r5decide/' + g.dataset.session + '/' + g.dataset.page + '/' + clip, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({to: to, rev: rev}),
      });
      body = await res.json();
    } catch (err) {
      window.__r5Last = {ok: false, code: 'network_error'};
      return;
    }
    window.__r5Last = Object.assign({status: res.status}, body);
    if (body && body.counts && stamp === decideSeq) { paintCounts(body.counts); }
    // Repaint on refusal too, so a stale tab is corrected rather than left lying.
    if (body && body.decision) { paint(clip, body.decision, body.rev); }
  }

  async function decide(tile, to) {
    if (!tile) { return; }
    const rev = validRevision(tile.dataset.rev);
    if (rev === null) {
      // The DOM revision is not trustworthy. Send NOTHING, re-read the server's record, and
      // repaint from it. If even that fails, say so rather than leaving a silent dead click.
      window.__r5Last = {ok: false, code: 'local_revision_invalid'};
      const body = await window.__r5Resync();
      if (!body || !body.ok) {
        sayProblem('Could not read the decision record from the server. ' +
                   'Press "Re-read the server\'s record" before deciding again.');
      }
      return;
    }
    send(tile.dataset.clip, to, rev);
  }

  // Lazy media: no <video> exists until hover, and exactly one exists at a time.
  function mount(tile) {
    if (!tile) { return; }
    if (active && active.tile === tile) { active.el.play().catch(() => {}); return; }
    unmount();
    const slot = tile.querySelector('.r5-slot');
    if (!slot) { return; }
    const video = document.createElement('video');
    video.muted = true;
    video.loop = true;
    video.preload = 'metadata';
    video.playsInline = true;
    video.setAttribute('aria-hidden', 'true');
    video.src = tile.dataset.video;
    slot.appendChild(video);
    active = {tile: tile, el: video};
    video.play().catch(() => {});
  }

  function unmount() {
    if (!active) { return; }
    try {
      active.el.pause();
      active.el.removeAttribute('src');
      active.el.load();
    } catch (err) { /* tearing down is best-effort */ }
    if (active.el.parentNode) { active.el.parentNode.removeChild(active.el); }
    active = null;
  }

  window.__r5Mounted = () => document.querySelectorAll('#clips-gallery .r5-tile video').length;

  // [UX V3] Let go of the clip before Confirm sorts it. A hovered tile holds an open <video> on
  // that file, and Windows refuses to rename a file another handle still has open -- measured in
  // the browser as winerror 32 on exactly the last tile the operator touched, every time. This
  // runs on pointerdown in the CAPTURE phase, so the handle is released before the click reaches
  // Gradio and the request goes out. Harmless when nothing is mounted.
  document.addEventListener('pointerdown', (e) => {
    const button = e.target.closest ? e.target.closest('#clips-confirm-button') : null;
    if (button) { unmount(); }
  }, true);

  document.addEventListener('mouseover', (e) => { const t = tileOf(e.target); if (t) { mount(t); } });
  document.addEventListener('mouseout', (e) => {
    const t = tileOf(e.target);
    if (t && active && active.tile === t && !t.contains(e.relatedTarget)) { unmount(); }
  });

  // [UX] Shift + wheel scrubs the clip under the cursor: up seeks forward, down seeks back,
  // SEEK_STEP seconds per notch. One notch per event rather than a multiple of the delta, so a
  // trackpad's large deltas do not jump halfway through the clip.
  //
  // Three details that are easy to get wrong here:
  //   * Chrome re-routes a shifted wheel to `deltaX` on some devices, so take whichever axis
  //     actually moved. Reading `deltaY` alone silently does nothing on those machines.
  //   * `duration` is NaN until metadata arrives. `preload='metadata'` makes that brief, not
  //     impossible, so an early notch must bail rather than assign NaN to `currentTime` -- which
  //     throws and would leave the player wedged.
  //   * The listener must be non-passive or `preventDefault` is ignored and the page scrolls
  //     underneath the seek.
  function showSeek(tile, seconds) {
    if (!tile) { return; }
    let tag = tile.querySelector('.r5-seek');
    if (!tag) {
      tag = document.createElement('div');
      tag.className = 'r5-seek';
      tag.setAttribute('aria-hidden', 'true');
      tag.style.cssText = 'position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);' +
        'padding:4px 10px;border-radius:999px;background:rgba(0,0,0,.72);color:#fff;' +
        'font:600 13px/1.2 system-ui,sans-serif;pointer-events:none;z-index:5;';
      tile.appendChild(tag);
    }
    tag.textContent = (seconds > 0 ? '+' : '') + seconds + 's';
    tag.style.opacity = '1';
    if (tag.__hide) { clearTimeout(tag.__hide); }
    tag.__hide = setTimeout(() => { tag.style.opacity = '0'; }, 420);
  }

  document.addEventListener('wheel', (e) => {
    if (!e.shiftKey) { return; }                      // plain wheel keeps scrolling the page
    const tile = tileOf(e.target);
    if (!tile || !active || active.tile !== tile) { return; }   // elsewhere: normal browser behaviour
    e.preventDefault();                               // we own this gesture over a playing tile
    const raw = e.deltaY !== 0 ? e.deltaY : e.deltaX;
    if (!raw) { return; }
    const video = active.el;
    if (!video) { return; }
    // The element's own duration, never the rounded text in the tile label: the label truncates
    // to whole seconds, so a 20.4s clip reads "00:20" and a threshold taken from that would pick
    // the wrong step for the clip actually loaded.
    const span = video.duration;
    const unit = seekStep(span);
    if (unit === null) { return; }                              // metadata not in yet: do nothing
    const step = raw < 0 ? unit : -unit;                        // wheel up = forward
    const at = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    const target = Math.min(Math.max(at + step, 0), span);
    try { video.currentTime = target; } catch (err) { return; }
    showSeek(tile, step);
  }, {passive: false});
  document.addEventListener('click', (e) => {
    const reset = e.target.closest ? e.target.closest('#clips-gallery .r5-reset') : null;
    if (reset) {
      e.preventDefault(); e.stopPropagation();
      decide(tileOf(reset), 'PENDING');
      return;
    }
    const tile = tileOf(e.target);
    if (tile) { tile.focus(); decide(tile, 'SELECTED'); }
  });
  document.addEventListener('contextmenu', (e) => {
    const tile = tileOf(e.target);
    if (tile) { e.preventDefault(); tile.focus(); decide(tile, 'REJECTED'); }
  });
  document.addEventListener('auxclick', (e) => {
    if (e.button !== 1) { return; }
    const tile = tileOf(e.target);
    if (tile) { e.preventDefault(); decide(tile, 'PENDING'); }
  });
  document.addEventListener('keydown', (e) => {
    const tile = tileOf(document.activeElement);
    if (!tile) { return; }
    const tiles = Array.from(document.querySelectorAll('#clips-gallery .r5-tile'));
    const index = tiles.indexOf(tile);
    const columns = Math.max(1, Math.round(tile.parentElement.clientWidth / tile.offsetWidth));
    let next = -1;
    switch (e.key) {
      case 'ArrowRight': next = index + 1; break;
      case 'ArrowLeft': next = index - 1; break;
      case 'ArrowDown': next = index + columns; break;
      case 'ArrowUp': next = index - columns; break;
      case 's': case 'S': case 'Enter':
        e.preventDefault(); decide(tile, 'SELECTED'); return;
      case 'r': case 'R': case 'Delete':
        e.preventDefault(); decide(tile, 'REJECTED'); return;
      case 'p': case 'P':
        e.preventDefault(); decide(tile, 'PENDING'); return;
      case ' ':
        e.preventDefault();
        if (active && active.tile === tile) {
          if (active.el.paused) { active.el.play().catch(() => {}); } else { active.el.pause(); }
        } else { mount(tile); }
        return;
      default: return;
    }
    if (next >= 0 && next < tiles.length) {
      e.preventDefault();
      tiles[next].focus();
      mount(tiles[next]);
    }
  });

  // Re-read the server's record for every tile on the page.
  window.__r5Resync = async () => {
    const g = grid();
    if (!g) { return {ok: false, code: 'no_grid'}; }
    // [R5-P3 / F2] Never throws: `decide` depends on this answering so that an untrustworthy DOM
    // revision results in a resync or a visible failure, not an unhandled rejection.
    let body;
    try {
      const res = await fetch('/r5state/' + g.dataset.session + '/' + g.dataset.page);
      body = await res.json();
    } catch (err) {
      return {ok: false, code: 'network_error'};
    }
    if (!body || !body.ok) { return body || {ok: false, code: 'no_body'}; }
    Object.keys(body.clips).forEach((clip) => {
      paint(clip, body.clips[clip].decision, body.clips[clip].rev);
    });
    paintCounts(body.counts);
    return body;
  };

  // A freshly delivered grid seeds its counters from the server instead of sitting at zero.
  const host = document.querySelector('#clips-gallery');
  if (host) {
    new MutationObserver(() => {
      const g = grid();
      if (g && g.dataset.nonce !== lastNonce) {
        lastNonce = g.dataset.nonce;
        unmount();
        window.__r5Resync();
      }
    }).observe(host, {childList: true, subtree: true});
  }
}
""".replace('__REFRESH_HINT__', json.dumps(CLIPS_REFRESH_TO_ENABLE_HINT))
# [R5-P3 / F8] Operator-facing wording stays in `ui_content.py` like every other string in this
# app; the gallery layer is a raw JS literal, so it is substituted in rather than interpolated.
#
# The substitution emits a COMPLETE JS string literal via `json.dumps`, and the placeholder above
# is deliberately NOT wrapped in quotes in the JS. Dropping raw text inside `'...'` was wrong and
# the real browser caught it: this hint contains an apostrophe ("the server's record"), which
# closed the literal and made the whole gallery script fail to compile --
# `SyntaxError: Unexpected identifier 's'` out of Gradio's `new AsyncFunction`, which silently
# costs every listener on the page. JSON encoding is the only safe way to move operator text into
# a script; never interpolate it bare, and never assume a constant has no quote in it.


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


def _finite_metric(value: object) -> float | None:
    """[FORK] Digital-Union (P3): one usable finite number, or ``None`` for "not a measurement".

    Same totality contract as `_fmt_stage_seconds` above — a diagnostic must never raise inside a
    render — but it returns ``None`` instead of a zero, because the caller's job is different. A
    shot metric that is absent, ``None``, ``NaN``, infinite or unparseable is **not** a measurement
    of zero concentration, and printing ``0.0%`` for it would invent one. The caller omits the
    segment instead, exactly as `video_processor._planner_sources_line` does for the same three
    keys on the console side.

    Deliberately a local twin of that helper rather than an import of it: `_scale_diagnostics_block`
    is AST-extracted and executed against a synthesised namespace by
    `tests/test_scale_diagnostics.py`, so every name its body uses must be extractable from
    `gui.py` itself. `video_processor` cannot be imported on a bare interpreter at all. This
    module already carries `_fmt_stage_seconds` beside `beatsync_fork.input_report.format_seconds`
    for the same reason.

    `value != value` is the NaN test and the infinity comparison is literal, so this needs no
    ``math`` import.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


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

    # [FORK] Digital-Union (P3): the shot diagnostics P1 merged into the plan summary, re-read for
    # the panel that survives. Reporting only — every value comes straight off `plan_summary`,
    # which `summarize_clip_plan` already computed, so this can never become a second authority on
    # what a plan contains. Same three keys, same clamps and same omission rule as
    # `video_processor._planner_sources_line`, which prints them on the console line the GUI
    # discards.
    #
    # These describe how the selected cuts were SPREAD across the available shots. They are not a
    # quality score: a low shot count can be the right answer for a short track or a small library,
    # and a high one does not make an edit good.
    plan_summary = render_info.get("plan_summary")
    plan_summary = plan_summary if isinstance(plan_summary, dict) else {}

    variety: list[str] = []
    shot_count = _finite_metric(plan_summary.get("shot_count"))
    if shot_count is not None:
        variety.append(f"{max(0, int(shot_count))} distinct shots")
    top_shot_share = _finite_metric(plan_summary.get("top_shot_share"))
    if top_shot_share is not None:
        variety.append(f"Top shot {min(1.0, max(0.0, top_shot_share)) * 100:.1f}%")
    shot_hhi = _finite_metric(plan_summary.get("shot_hhi"))
    if shot_hhi is not None:
        variety.append(f"HHI {min(1.0, max(0.0, shot_hhi)):.4f}")
    if variety:
        lines.append("Shot variety: " + " · ".join(variety))

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


#: [FORK] Digital-Union (Deliverable Export V1-B-S1 / H1-b): the subordinate publication's staging
#: directory name and copy chunk.
#:
#: Staging lives **inside** `output/` on purpose. The final step is therefore always a
#: same-directory `os.rename`, which makes `errno.EXDEV` structurally impossible rather than merely
#: unlikely -- the master promotion cannot have that property, because it must cross from
#: `session_dir`, which is exactly why H1 needs an EXDEV branch and H1-b does not. The leading dot
#: keeps it out of the way in a folder the user browses.
SHARE_STAGING_DIR_NAME = '.beatsync-staging'
SHARE_COPY_CHUNK_BYTES = 1 << 20


def _publish_share_no_replace(preview_source: str, share_path: str, staging_dir: str,
                              lifecycle=None):
    """[FORK] Digital-Union (V1-B-S1 / H1-b): publish the SUBORDINATE durable shareable MP4.

    Returns ``(failure_reason, published_bytes)`` -- ``('', n)`` on success. **It never raises**,
    and this wrapper is what makes that true rather than merely intended.

    The attempt below handles every failure it can *name*. This outer guard exists because
    "never raises" cannot be delivered by naming causes one at a time: a first draft put
    ``hashlib.sha256()`` one line above its ``try`` and a test that broke the digest backend
    escaped the helper entirely. Since every caller sits after the master SUCCESS commit point, an
    escape would report a finished, promoted render as ``❌ Error`` -- precisely the downgrade H1-b
    forbids. So the contract is enforced structurally, and a staging file the attempt had already
    created is still discarded on the way out.
    """
    created: List[str] = []
    try:
        return _publish_share_attempt(
            preview_source, share_path, staging_dir, lifecycle, created)
    except Exception as exc:
        for path in created:
            try:
                os.remove(path)
            except OSError:
                pass
        return f'the shareable MP4 could not be published ({exc})', 0


def _publish_share_attempt(preview_source: str, share_path: str, staging_dir: str,
                           lifecycle, created: list):
    """The publication itself. May raise; `_publish_share_no_replace` is the only caller and
    contains everything. ``created`` collects the staging path this attempt exclusively made, so
    the wrapper can clean it up if an unnamed failure unwinds past here.

    This is deliberately **not** `_promote_output_no_replace` and must never become it. That helper
    carries *authoritative* semantics -- its `CANDIDATE_LOCAL` / `SHARED_FATAL` / `UNKNOWN_FATAL`
    classes feed `RenderCandidateOutcome` and batch continuation. A share failure must produce none
    of those, so it returns a plain reason string and no outcome class at all. Routing both through
    one helper with a policy flag is exactly the per-caller boolean that `refuse_existing_output`
    was deleted for.

    **No second transcode.** The bytes already exist: this copies the validated V1-A preview. Over
    18 real renders a re-encode would buy nothing, and the preview is 5.5-9.2 % of the master
    (measured), so the extra durable artifact is cheap.

    **The staging file is attempt-unique and exclusively created** (`O_CREAT | O_EXCL`), not a
    predictable `<name>.part`. A predictable name is a latent collision between two attempts at the
    same share name -- the second would silently truncate the first's in-flight copy and could
    publish interleaved bytes that still passed a digest check computed from its own writes. With
    `O_EXCL` the loser of that race cannot even open the file. Nothing here ever uses `os.replace`,
    truncates an existing file, removes another invocation's staging artifact, or deletes an
    existing final share file.

    **Verification re-reads the staged bytes and compares them to the bytes read during the copy.**
    That is the exact scope of the claim, and stating it loosely as "matches the preview" was
    imprecise. Re-reading is what detects a short or partial write -- trusting the digest
    accumulated while writing would confirm only "what I hashed is what I hashed". What it does
    **not** prove is equality with the source as it stands at publication time: a same-size
    in-place mutation of the preview mid-copy would stage a torn read that still matches, and was
    measured doing exactly that. A size-changing mutation IS caught, because the size check
    compares against `source_bytes` sampled before the copy, and size is checked first because it
    is the cheap, decisive case.

    Source stability is supplied by the runtime rather than by this helper, which is why that
    narrower claim is sufficient: the render lock serialises renders, `session_dir` is a
    per-session `mkdtemp`, and the preview's FFmpeg child has exited and been reaped before
    publication starts, so nothing in the app writes that file while it is being copied.

    **Atomicity is final-path visibility, not power-loss durability.** `os.rename` makes the
    complete file appear under its final name in one step, and on Windows refuses when the
    destination exists (`FileExistsError`, WinError 183) with both files intact -- that refusal *is*
    the no-replace guarantee, and an early `exists()` check would only be an optimisation, never the
    authority. `os.fsync` is called on the staged copy before the rename, but **that is not a claim
    of crash consistency**: proving the published bytes survive power loss would need
    filesystem-level durability testing that has not been done. What is guaranteed is weaker and
    sufficient: a partial file never appears under the final name.

    **Containment is measured, not assumed.** The staging directory must be a real child of the
    final file's own directory by `realpath` + `commonpath` -- a string prefix test would accept
    `output-evil/` for `output/`. Same volume is checked explicitly, and `errno.EXDEV` is still
    handled fail-closed even though the layout should make it unreachable.

    **Reparse points are resolved, and containment is then measured on the resolved location** --
    they are NOT refused on sight, and the earlier wording that said so was wrong. `realpath` runs
    first, so a junction or symlink is followed and `_is_reparse` below sees an already-resolved
    path, which by construction is no longer a reparse point; it therefore fires only in the
    residual case where `realpath` could not resolve and returned its input unchanged. Measured
    against real Windows junctions: one escaping `output/` is refused by `commonpath`, one pointing
    to another volume by the `splitdrive` check, and one resolving back inside `output/` is allowed
    -- so containment holds in every case, which is the stronger property and the one that matters.
    **Do not "harden" this by inspecting the unresolved paths instead.** An `output/` folder that is
    itself a junction to a larger drive is a legitimate, measured-working configuration, and
    refusing reparse points on sight would break it.
    """
    def _cancelled():
        return lifecycle is not None and lifecycle.cancel_requested()

    def _is_reparse(path):
        try:
            attrs = getattr(os.lstat(path), 'st_file_attributes', 0)
        except OSError:
            return False
        # 0x400 == FILE_ATTRIBUTE_REPARSE_POINT. `st_file_attributes` is Windows-only, and the
        # portable test suite runs on bare CPython elsewhere, so this degrades to the islink check.
        return bool(attrs & 0x400) or os.path.islink(path)

    def _discard_own_staging(path):
        # Only ever the file THIS invocation exclusively created. Never another attempt's.
        try:
            os.remove(path)
        except OSError:
            pass

    if _cancelled():
        return 'cancelled before publication started', 0

    # H1-b: only ever derived from a validated preview. No preview, no share file.
    try:
        if not preview_source or not os.path.isfile(preview_source):
            return 'the playable preview is missing', 0
        source_bytes = os.path.getsize(preview_source)
        if source_bytes == 0:
            return 'the playable preview is empty', 0
    except OSError as exc:
        return f'the playable preview could not be read ({exc})', 0

    final_dir = os.path.dirname(os.path.abspath(share_path))
    try:
        os.makedirs(staging_dir, exist_ok=True)
        real_staging = os.path.realpath(staging_dir)
        real_final = os.path.realpath(final_dir)
    except OSError as exc:
        return f'the staging directory is unavailable ({exc})', 0

    if os.path.splitdrive(real_staging)[0].lower() != os.path.splitdrive(real_final)[0].lower():
        return 'refused: staging and the output folder are on different volumes', 0
    try:
        if os.path.commonpath([real_staging, real_final]) != real_final:
            return 'refused: the staging folder is not inside the output folder', 0
    except ValueError:
        return 'refused: the staging folder is not inside the output folder', 0
    if _is_reparse(real_staging) or _is_reparse(real_final):
        return 'refused: a junction or symlink is in the publication path', 0

    staging_path = os.path.join(
        staging_dir,
        f'{os.path.basename(share_path)}.{os.getpid()}.{uuid.uuid4().hex[:12]}.part')
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
        handle = os.open(staging_path, flags, 0o600)
    except FileExistsError:
        return 'refused: the staging filename was already taken', 0
    except OSError as exc:
        if getattr(exc, 'errno', None) == errno.ENOSPC:
            return 'not enough free disk space for the shareable MP4', 0
        return f'the staging file could not be created ({exc})', 0
    # From here on a staging file exists and is owned by THIS attempt, so the wrapper must be
    # able to discard it if anything unnamed unwinds past the handlers below.
    created.append(staging_path)

    failure = ''
    try:
        written = hashlib.sha256()
        with open(handle, 'wb', closefd=True) as staged:
            with open(preview_source, 'rb') as src:
                while True:
                    # Bounded chunks so Stop is observable during the copy. This cannot interrupt a
                    # read/write already blocked inside the OS -- it bounds the work between checks.
                    if _cancelled():
                        failure = 'cancelled during publication'
                        break
                    chunk = src.read(SHARE_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    staged.write(chunk)
                    written.update(chunk)
            if not failure:
                staged.flush()
                os.fsync(staged.fileno())
    except OSError as exc:
        if getattr(exc, 'errno', None) == errno.ENOSPC:
            failure = 'not enough free disk space for the shareable MP4'
        else:
            failure = f'the shareable MP4 could not be written ({exc})'
    except Exception as exc:
        failure = f'the shareable MP4 could not be written ({exc})'

    if not failure:
        try:
            if os.path.getsize(staging_path) != source_bytes:
                failure = 'verification failed: the staged copy is the wrong size'
            else:
                staged_digest = hashlib.sha256()
                with open(staging_path, 'rb') as fh:
                    while True:
                        block = fh.read(SHARE_COPY_CHUNK_BYTES)
                        if not block:
                            break
                        staged_digest.update(block)
                if staged_digest.hexdigest() != written.hexdigest():
                    # Names what was actually compared. The re-read detects a corrupt or short
                    # write; it does not speak to a source mutated during the copy.
                    failure = 'verification failed: the staged copy does not match the bytes copied'
        except OSError as exc:
            failure = f'verification failed ({exc})'
        except Exception as exc:
            failure = f'verification failed ({exc})'

    # Last cancellation check, immediately before the one irreversible step.
    if not failure and _cancelled():
        failure = 'cancelled before publication'

    if failure:
        _discard_own_staging(staging_path)
        return failure, 0

    try:
        os.rename(staging_path, share_path)
    except FileExistsError:
        _discard_own_staging(staging_path)
        return (f'a file already exists at {share_path} and was preserved'), 0
    except OSError as exc:
        _discard_own_staging(staging_path)
        if getattr(exc, 'errno', None) == errno.EXDEV:
            return 'refused: staging and the output folder are on different volumes', 0
        if getattr(exc, 'errno', None) == errno.ENOSPC:
            return 'not enough free disk space for the shareable MP4', 0
        return f'the shareable MP4 could not be published ({exc})', 0
    except Exception as exc:
        _discard_own_staging(staging_path)
        return f'the shareable MP4 could not be published ({exc})', 0

    try:
        return '', os.path.getsize(share_path)
    except OSError:
        return '', source_bytes


def _process_video_impl(audio_file: str, video_files: VideoFilesInput,
                       output_filename: str, processing_mode: str,
                       output_profile: str, creative: fork_creative.CreativeProfile | None,
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
        #
        # [FORK] R8 UX Refresh (Phase 6): a fixed Output Video Profile states the frame rate.
        # Under Auto, `target_fps` returns its fallback unchanged -- here `None` -- so the
        # auto-detect branch below is reached exactly as it always was.
        profile_fps = fork_render_profiles.target_fps(output_profile, None)
        if profile_fps:
            output_fps = profile_fps
        else:
            output_fps = get_video_fps(local_video_paths[0])
            
        # Prepare output paths
        # [FORK] R8: the configured Render output folder, falling back to the installation's own
        # `output/`. Only the PUBLISHED video moves -- `session_dir` below is unchanged, so no
        # temporary processing directory or cache follows this setting.
        output_folder = get_render_output_dir()
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
            event_callback=event_callback, lifecycle=lifecycle,
            # [FORK] R8 Phase 6: the Output Video Profile. `None`/`auto` leaves the renderer on
            # its historical path -- first-source resolution and a bare scale -- untouched.
            output_profile=output_profile,
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

        # [FORK] Digital-Union (Deliverable Export V1-A): ONE browser-playable preview, for EVERY
        # processing mode rather than only ProRes.
        #
        # Why every mode: the master always muxes `pcm_s24le`, which lands in MP4 as `ipcm`
        # (ISO/IEC 23003-5). An ordinary browser cannot decode that, so handing the durable master
        # straight to `gr.Video` — which is what the non-ProRes path did — produced a preview whose
        # audio nothing in the page can play. ProRes and HEVC masters are not AVC at all.
        #
        # Two defects in the block this replaces are fixed here, and both were measured:
        #   * the argv placed `-c:v` BEFORE `-i`, so FFmpeg read the encoder as an input decoder
        #     and died on `Unknown decoder 'h264_nvenc'` / `Unknown decoder 'libx264'`. The argv
        #     now comes from `build_delivery_preview_command`, which emits every encoder option
        #     after the input and is pinned positionally by test.
        #   * `preview_path` was assigned BEFORE the subprocess and there was no returncode or
        #     existence check, so a failed preview (i.e. always) returned a path to a file that was
        #     never created. The validation below is the fix: the preview is selected only after
        #     FFmpeg succeeded AND the file exists AND it is non-empty.
        #
        # Everything here runs AFTER the SUCCESS commit point, so it is post-commit convenience
        # work and is contained accordingly: no failure, timeout or cancellation may change the
        # render's terminal result, downgrade SUCCESS, touch `LAST_OUTPUT_PATH_KEY`, or reach the
        # generic render-failure handler below. `except RenderCancelled` therefore comes FIRST --
        # it is an ordinary `Exception`, so the generic clause would otherwise swallow it.
        #
        # One deliberate behaviour change from the pre-V1-A block: a `TimeoutExpired` IS caught
        # now. The old comment reserved it as "a stuck encode is a different fact from a user
        # pressing Stop", but after the commit point a stuck *preview* must not be able to turn a
        # finished render into a traceback.
        preview_path = output_path
        preview_bytes = 0
        preview_failure = None
        preview_target = os.path.join(
            session_dir, f"{name}_{timestamp}{creative.filename_suffix()}_preview.mp4")
        if os.path.exists(preview_target):
            # Never overwrite an existing artifact, and never silently reuse one: the master stem
            # already carries a timestamp and the seed suffix, and `session_dir` is a per-session
            # mkdtemp, so this is close to impossible -- and refusing costs only the preview.
            preview_failure = 'a file already occupies the preview path'
        elif lifecycle is not None and lifecycle.cancel_requested():
            # Never START a child when cancellation is already pending. Cheaper than starting one
            # and terminating it on the first poll, and it is the common case when Stop landed
            # during the render.
            preview_failure = 'cancelled before the preview started'
        else:
            try:
                preview_cmd = build_delivery_preview_command(output_path, preview_target)
                if lifecycle is None:
                    completed = subprocess.run(
                        preview_cmd, capture_output=True, text=True,
                        timeout=DELIVERY_PREVIEW_TIMEOUT_SECONDS)
                else:
                    completed = run_cancellable_media_command(
                        preview_cmd, DELIVERY_PREVIEW_TIMEOUT_SECONDS, lifecycle=lifecycle)
                if getattr(completed, 'returncode', 1) != 0:
                    preview_failure = 'FFmpeg exited non-zero'
                elif not os.path.exists(preview_target):
                    preview_failure = 'FFmpeg produced no output file'
                elif os.path.getsize(preview_target) == 0:
                    preview_failure = 'FFmpeg produced an empty output file'
                else:
                    preview_bytes = os.path.getsize(preview_target)
                    preview_path = preview_target
            except RenderCancelled:
                preview_failure = 'cancelled during preview encoding'
            except subprocess.TimeoutExpired:
                preview_failure = (
                    f'preview encoding exceeded {DELIVERY_PREVIEW_TIMEOUT_SECONDS}s')
            except Exception as preview_error:
                preview_failure = f'preview encoding failed: {preview_error}'
        # The cancellable runner (C3-R1A / R2) terminates, graces, kills and REAPS its child before
        # raising, so a Stop during preview encoding leaves no FFmpeg behind. A failed or abandoned
        # preview is simply never selected and is deliberately NOT deleted: `session_dir` already
        # legitimately retains non-promoted artifacts, and adding a destructive operation here to
        # tidy session scratch is not worth the surface.
        #
        # [FORK] Digital-Union (Deliverable Export V1-B-S1 / H1-b): the SUBORDINATE durable
        # publication -- one permanent, browser-playable `<stem>_share.mp4` beside the master.
        #
        # H1 is unchanged and still owns the one AUTHORITATIVE promotion above. This is subordinate
        # by construction, and every clause of that is load-bearing:
        #   * it runs strictly after the SUCCESS commit point, and only when a VALIDATED preview
        #     exists -- no preview, no share file, because the share file is a copy of the preview;
        #   * it writes neither `RENDER_OUTCOME_KEY` nor `LAST_OUTPUT_PATH_KEY`, so the master stays
        #     the sole success authority and the C3 batch's continuation policy cannot see this at
        #     all -- a share failure on candidate 2 produces no outcome class and cannot stop
        #     candidates 3-4;
        #   * `preview_path` is NOT reassigned: `gr.Video` keeps the session preview, which is the
        #     only location Gradio will serve. Measured against the live app: a path under
        #     `GRADIO_TEMP_DIR` answers 206 with `video/mp4` and range support, while a raw
        #     `output/` path answers 403. So "move the preview into output/ and show that" is not a
        #     simpler alternative, it is broken -- and widening `allowed_paths` to fix it would
        #     expose the whole folder to every user of the app.
        # The helper never raises, so this needs no try/except of its own. Be precise about what
        # the generic handler below would and would not do if that guarantee were ever breached,
        # because the two are not the same thing: `RENDER_OUTCOME_KEY` is already SUCCESS and its
        # `is None` guard leaves it standing, so the committed outcome and the batch's
        # continuation decision are safe -- but the function would still return
        # `(None, '❌ Error: ...')`, clearing `gr.Video` and showing an error panel for a finished,
        # promoted render. So the generic handler protects the OUTCOME, not the user-visible
        # result. What makes the breach unreachable is the helper's own structural wrapper, and
        # that is where the guarantee lives.
        share_path = ''
        share_bytes = 0
        share_failure = None
        if preview_failure is None:
            share_target = os.path.join(
                output_folder, f"{name}_{timestamp}{creative.filename_suffix()}_share.mp4")
            share_failure, share_bytes = _publish_share_no_replace(
                preview_path, share_target,
                os.path.join(output_folder, SHARE_STAGING_DIR_NAME),
                lifecycle=lifecycle)
            if not share_failure:
                share_path = share_target
            else:
                share_bytes = 0
        _stage6_summary(console_logger, beat_info)

        # Generate status message based on mode
        gpu_info = f"⚡ GPU: {GPU_INFO}" if use_gpu else "💻 CPU"
        fps_info = (f"{output_fps:.2f} FPS (profile)" if fork_render_profiles.resolve(output_profile)
                    else f"{output_fps:.2f} FPS (auto-detected)")
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
        # [FORK] Digital-Union (Deliverable Export V1-A): name the two artifacts explicitly,
        # because they are genuinely different things and conflating them is what made the old
        # panel misleading. The master is durable; the preview is session-scoped and must never be
        # described as a permanent download. On failure this says so plainly and does NOT claim the
        # master is browser-playable -- it usually is not, which is the whole reason for the
        # preview. Sizes come from the files just written, so nothing is estimated.
        # No second `os.path.exists(output_path)`: the promotion above already proved the master is
        # there, and a second check would be exactly the TOCTOU pair the no-replace rename exists
        # to avoid (a test pins that exactly one such check survives, the early one). A stat that
        # somehow fails must not turn a finished render into an error message either, so it
        # degrades to 0 rather than propagating.
        try:
            master_bytes = os.path.getsize(output_path)
        except OSError:
            master_bytes = 0
        delivery_lines = [
            "HIGH-QUALITY MASTER:",
            f"   {output_path}",
            f"   {codec_info}, {audio_info} - {master_bytes / 1048576:.1f} MB",
        ]
        # [FORK] Digital-Union (Deliverable Export V1-B-S1): three artifacts now, and exactly two of
        # them are permanent. Naming them separately is the whole point -- the one thing the panel
        # must never do is let the session-scoped preview read as a permanent download, or let a
        # failed share publication read as a delivered one.
        # Three states, deliberately distinguished rather than collapsed:
        #   share_failure is None  -> never attempted, because the preview failed. The preview
        #                             branch below already reports that; adding a second line
        #                             would say the same thing twice.
        #   share_failure == ''    -> published.
        #   share_failure is text  -> attempted and refused/failed, with the reason.
        if share_failure == '':
            delivery_lines.extend([
                "SHAREABLE MP4 (saved permanently):",
                f"   {share_path}",
                f"   H.264/AAC MP4 - {share_bytes / 1048576:.1f} MB",
            ])
        elif share_failure:
            delivery_lines.extend([
                f"⚠️  No permanent shareable MP4 was saved ({share_failure}).",
                "   The high-quality master above is complete and unchanged, and the playable",
                "   preview below still works for this session.",
            ])
        if preview_failure is None:
            delivery_lines.extend([
                "PLAYABLE PREVIEW (this session only - not a permanent download):",
                f"   {preview_path}",
                f"   H.264/AAC MP4 - {preview_bytes / 1048576:.1f} MB",
            ])
        else:
            delivery_lines.extend([
                f"⚠️  Browser-compatible preview was not created ({preview_failure}).",
                "   The master above is unchanged and complete, but it may not play in this",
                "   browser; no shareable MP4 was produced.",
            ])
        status_msg = f"{status_msg}\n\n" + "\n".join(delivery_lines)
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
                 output_profile: str, session_state: dict,
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
                    output_profile=output_profile,
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

#: [FORK] R8 UX Refresh (Commit C): the radio's third choice, and the reason it is NOT a third
#: `SourceMode`.
#:
#: "Use Media Library" is not a new kind of source. It is a local folder whose path the operator
#: configured once in Settings instead of typing every time -- so it maps onto the EXISTING
#: `LOCAL_FOLDER` mode and reuses the whole engine behind it: the same scan, the same
#: `SourceSnapshot` identity, the same confirmation gate, the same invalidation rules, the same
#: media analysis. Nothing about the gate needed changing, which is the point: the task asked for
#: a convenience, and a third enum member would have been a second source concept for the
#: snapshot, the session state and every test that enumerates modes to carry forever.
#:
#: The consequence an operator can observe is exactly the one required: because selecting it
#: WRITES `source_folder`, that textbox's own `.change` fires and invalidates any previous
#: confirmation through the established path -- identical to editing the folder by hand, because
#: it *is* editing the folder.
SOURCE_CHOICE_MEDIA_LIBRARY = "media_library"

SOURCE_MODE_VALUES = {
    SourceMode.LOCAL_FOLDER.value: SourceMode.LOCAL_FOLDER,
    SourceMode.BROWSER_FILES.value: SourceMode.BROWSER_FILES,
    SOURCE_CHOICE_MEDIA_LIBRARY: SourceMode.LOCAL_FOLDER,
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
    """Switch source mode, and for "Use Media Library" also fill in the configured folder.

    [FORK] R8: the folder update LEADS the returned tuple and is `gr.update()` -- change nothing --
    for both pre-existing choices, so their behaviour is untouched.

    When the library is not configured the folder is deliberately **left alone** rather than
    cleared or invented, and the operator is told where to set it. Clearing it would silently
    destroy a path they had already typed; inventing one would point the render at a directory
    nobody chose.
    """
    mode = SOURCE_MODE_VALUES.get(str(mode_value), SourceMode.LOCAL_FOLDER)
    new_state = set_mode(state, mode)
    is_folder = mode is SourceMode.LOCAL_FOLDER

    folder_update = gr.update()
    if str(mode_value) == SOURCE_CHOICE_MEDIA_LIBRARY:
        configured = _pref(fork_prefs.CLIPS_LIBRARY)
        if not configured:
            gr.Warning(STATUS_LIBRARY_SOURCE_UNSET)
        elif not os.path.isdir(configured):
            # Reported, never recreated -- the same rule the rest of the preference store follows.
            gr.Warning(STATUS_LIBRARY_SOURCE_MISSING.format(path=configured))
            folder_update = gr.update(value=configured)
        else:
            folder_update = gr.update(value=configured)

    return (folder_update, gr.update(visible=is_folder),
            gr.update(visible=not is_folder)) + _source_ui_updates(new_state)


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
    # [FORK] Digital-Union (Random Freestyle V2 / R1): the V2 formatter, because the V1 one takes a
    # *declaration* and a generated rule is indistinguishable from a hand-picked one once resolved.
    # Routing a V2 screen through V1's parser reported the exact opposite of the truth: it resolves
    # styles with `override_from_style`, which answers None for "Random" (not a preset name), so ten
    # Random sections read as ten Base sections and the panel claimed the render was unmodified
    # while the renderer was generating ten active rules. It was the DEFAULT screen, so every user
    # would have seen it.
    return fork_freestyle_random.summary_text(
        freestyle_enabled, dict(zip(_FREESTYLE_WIDGET_ORDER, styles)))


def _on_clear_creative_variations(*styles):
    """Clear the Variation Seed and turn Freestyle off. Touches nothing else.

    [FORK] R8 Final Polish. Returns `(seed, freestyle_enabled, freestyle_summary)` -- three
    values, and the third only because the summary is a read-out of the first two and would
    otherwise sit there describing a Freestyle that is no longer on.

    **What it deliberately does NOT return is the point of it.** No slider, no preset, no Audio
    Mix widget, no source or preparation widget. Selecting the `Balanced` preset resets the six
    sliders and nothing else; this resets the seed and Freestyle and nothing else. The two are
    disjoint, so neither is "reset everything" and neither can be described as such.

    The section dropdowns are read, not written: turning Freestyle off must leave the rules the
    operator built intact, so ticking the box again restores exactly what was there. Clearing them
    would make this a destructive action wearing a tidy-up's name.
    """
    return (0, False,
            fork_freestyle_random.summary_text(False,
                                               dict(zip(_FREESTYLE_WIDGET_ORDER, styles))))


def _on_surprise_me(freestyle_enabled, *styles):
    """[FORK] Digital-Union (Random Freestyle V2): roll a new Variation Seed and preview the result.

    Returns ``(new_seed, preview_text)`` — it writes the Variation Seed and the Freestyle summary,
    and nothing else. No slider, no preset, no section dropdown.

    **It deliberately does not rewrite the dropdowns.** The dropdown is the user's *intent*:
    ``Random`` means "this section may be regenerated", and a named style means "I fixed this".
    Replacing a ``Random`` dropdown with the concrete style it happened to produce would destroy
    that distinction and make the next press a no-op for that section. So the dropdowns are an
    **input** here and never an output, which is also what keeps Freestyle's "nothing ever writes a
    Freestyle widget programmatically" invariant intact.

    The new seed is minted by the existing `fork_variation.random_seed`, the same producer the
    Randomize button uses — Freestyle gains no seed of its own and no second source of randomness.

    Unlike `_on_freestyle_change`, this one *may* print numbers: it is a point-in-time preview of
    what the seed it just minted generates, not a standing claim about inherited globals. The
    inherited-value staleness argument does not apply, because a generated value is not inherited.
    """
    seed = fork_variation.random_seed()
    preview = fork_freestyle_random.describe_resolved(
        dict(zip(_FREESTYLE_WIDGET_ORDER, styles)), seed)
    if not freestyle_enabled:
        # R1: the earlier wording ("so this render is unchanged") was false in the half that
        # matters. The Variation Seed is GLOBAL and this button really did move it, so the next
        # ordinary render changes too — clip choice is seeded from it. Only the *section rules*
        # are inactive while the switch is off.
        preview = (f"Variation Seed is now {seed}. Freestyle is switched OFF, so the section "
                   f"rules below are not applied — but the seed is global, so your next render "
                   f"will still vary.\nTick Enable Freestyle to use the sections below.\n\n"
                   ) + preview
    return seed, preview


def _on_render_tab_select(cut_density_value, micro_cuts_value, semantic_emphasis_value,
                          energy_response_value, motion_bias_value, source_diversity_value,
                          freestyle_enabled, variation_seed_value, voice_files_value,
                          sfx_folder_value, sfx_roles_value, sfx_amount_value,
                          *styles) -> str:
    """[FORK] Digital-Union (GUI Refresh A): what will actually affect the next render.

    Several tabs mean a user can arm something elsewhere and then look at a render screen that
    shows no sign of it. This says so in one read-only box.

    **It is a read-out, not an authority.** It writes nothing, no render path reads it, and it
    stores no state: it is recomputed from the LIVE widget values every time the Render tab is
    selected, so it cannot drift from the controls it describes.

    **R8 — it now reports the CREATIVE PROFILE, and the six sliders came back into its inputs.**
    That is a deliberate reversal of R1's removal, and the reason R1 gave is exactly the reason it
    is now safe to undo:

    R1 dropped the creative line because the Creative Preset radio sat on *this* tab and could
    change all six sliders while this box -- refreshed only on tab selection -- went on showing the
    old answer. That was the one staleness hole the read-out had. R8 Commit C moved the preset to
    Creative Settings, so **every** control this box describes now lives on another tab, and the
    `.select()` that fires on the way back here is sufficient for all of them. The hole is closed
    by the move rather than by the omission.

    What replaced the omission is a stronger statement than either version made. The preset
    selector and the old extras box were individually true and *jointly misleading*: the selector
    could read `Balanced` while Freestyle rules, a Variation Seed and SFX were armed, and nothing
    said the render would not be a plain Balanced render. One composed line fixes that, and the
    composition rule lives in `beatsync_fork.effective_profile` rather than here, so this label and
    the selector on the other tab are provably the same computation.

    **It still constructs nothing and judges nothing.** Freestyle activity comes from the
    declaration's own `is_active()`, the Smart Mix verdict from `plans_anything_from`, and the
    preset name from `presets.matching_preset` -- each asked of the subsystem that owns it. The
    two local judgements an earlier version made were both wrong in the same direction, claiming a
    render would be changed when it would not.

    Bound to the tab's own `.select()` rather than to `.change()` on a dozen existing widgets: the
    creative sliders carry exactly one `.input()` registration each by contract, and the Freestyle
    widgets write only their own summary. Adding handlers to them to feed a cosmetic read-out would
    put a second binding on widgets whose single binding is itself a pinned contract.

    **The two local judgements, kept delegated.** Both are recorded because each was a real defect
    once, and both are still answered by the owning subsystem rather than here:

    * *Freestyle ON with every section on `Base`* has no overrides, so `is_active()` is False and
      the renderer takes the ordinary global path. The checkbox alone was never the gate; the gate
      is `enabled AND overrides`, which is the single question Stage 4 and Stage 6 both ask.
    * *Amount 0* is a hard off-branch for Smart Mix however many roles are ticked — exactly what
      `SmartMixConfig.plans_anything` says. The first version ignored Amount entirely.

    **What it still refuses to do**, and the reason is unchanged from R1: it adds no second
    registration to any creative widget. An earlier draft rebuilt this string from a
    `creative_preset` event, which needed `sfx_folder`, `sfx_roles` and `sfx_amount` wired into
    that event — forbidden outright by
    `test_smart_mix_widgets_are_absent_from_source_and_preparation_wiring` — and pushed the
    selector to two registrations against a pin that exists to keep it at one. A fix that has to
    relax two guards is the wrong fix. The tab's own `.select()` remains the only trigger.

    Total: never raises, so selecting a tab can never fail. Every value here arrives straight from
    a widget, and `effective_profile` is total by contract for exactly that reason.
    """
    # The six live control values, in `CREATIVE_CONTROL_FIELDS` order -- the same order
    # `creative_control_sliders` uses, because Gradio matches `inputs` positionally and the
    # preset label is computed from the tuple as a whole.
    controls = (cut_density_value, micro_cuts_value, semantic_emphasis_value,
                energy_response_value, motion_bias_value, source_diversity_value)

    # Freestyle: the declaration decides, not the checkbox. The live seed is passed because it is
    # already in scope, but activity cannot depend on it -- a `Random` section yields a rule for
    # every seed, so the question asked is only whether any rule exists at all.
    declaration = fork_freestyle_random.declaration_from_styles(
        freestyle_enabled, dict(zip(_FREESTYLE_WIDGET_ORDER, styles)), variation_seed_value)

    # Smart Mix: `plans_anything_from` is the authority on Amount 0 and on an empty role set.
    #
    # The question is asked of the fork module rather than answered here, and this handler builds
    # NO `SmartMixConfig`: `test_the_public_wrapper_owns_the_mutex_and_duplicates_no_gate_logic`
    # pins exactly one construction from widget values in this file, because two places collapsing
    # widgets into a config is two chances to disagree about what the widgets mean. A read-out has
    # no business being the second one.
    #
    # An empty role selection is passed through as an explicit empty set rather than as the raw
    # falsey value: `normalize_roles` treats a non-iterable as *malformed* and falls back to all
    # five roles, which would read an untouched control as "everything on".
    #
    # The folder is checked separately because the config does not carry one -- without a library
    # there is nothing to place, whatever the three controls say.
    roles = fork_smart_mix.normalize_roles(sfx_roles_value) if sfx_roles_value else frozenset()
    amount = fork_smart_mix.normalize_control(sfx_amount_value)
    has_folder = isinstance(sfx_folder_value, str) and bool(sfx_folder_value.strip())
    smart_mix_on = has_folder and fork_smart_mix.plans_anything_from(roles, amount)

    # [FORK] R8: the one resolved line. The composition rule lives in the fork module so that the
    # preset label here and the selector on Creative Settings are provably the same computation --
    # both end at `presets.matching_preset` over the same six live values.
    line = fork_effective.summary(
        controls,
        freestyle_declaration=declaration,
        variation_seed=variation_seed_value,
        voice_clips=voice_files_value,
        smart_mix_active=smart_mix_on)
    if fork_effective.is_modified(
            controls,
            freestyle_declaration=declaration,
            variation_seed=variation_seed_value,
            voice_clips=voice_files_value,
            smart_mix_active=smart_mix_on):
        return f"{line}\n{CREATIVE_PROFILE_MODIFIED_HINT}"
    return f"{line}\n{CREATIVE_PROFILE_PLAIN_HINT}"


# [FORK] Digital-Union (AI Director — V1 introduced the split; V2 added the media step): two thin
# handlers, and the asymmetry between them IS the product.
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
# The read/write asymmetry is worth stating exactly, because V2 made the read side wider:
#
#   * neither handler renders;
#   * neither reads any current CREATIVE state — no slider, no Variation Seed, no preset, no
#     Variant Lab widget — so an instruction is an absolute intention, never a transform of what is
#     on screen;
#   * Generate READS the preparation declaration and state (`prep_folder`, `prep_recursive`,
#     `prep_state`), read-only, solely so `_eligible_media_summary` can tell a current fully
#     prepared scan from a stale or partial one;
#   * neither handler WRITES any source or preparation widget or state, so generating or applying
#     intent cannot invalidate a scan or a confirmed source set;
#   * Apply reads only `director_proposal_state`, which has exactly one reader:
#     `apply_director_btn.click`.

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
    the Director never had), the three `AudioRecipe` levels (which the Director does not generate —
    V2 is visual-only, exactly as V1 was) and the lab's own report. Reusing it would have made the
    Director claim audio values it never produced. What *is* shared is the semantic helper that matters —
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
    `director.parse_semantic_intent`, which remains the authority.

    **No image argument and no `mmproj`: this invocation is media-blind, and that remains exactly
    true under V2.** The Director as a whole is now narrowly content-aware, but the one media fact
    reaches deterministic local code *after* this call returns — nothing about the prepared library
    is interpolated into the argv, so there is no media prompt-injection surface here at all.
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


def _eligible_media_summary(prep_folder, prep_recursive, prep_state) -> Tuple:
    """`(summary_or_None, truthful_note)` — may this proposal use the prepared media facts?

    Returns the summary **only** from a current, live-declared, *fully prepared* scan. Every other
    outcome returns `None` plus a note naming the actual reason, because the alternative is silently
    claiming content awareness the user has not earned by preparing their library.

    Four gates, in cheapening order:

    1. **a scan exists at all** — a `gr.State` with no recorded scan has no facts;
    2. **the live declaration still describes it** — Gradio delivers widget changes as separate
       queued events, so the user can retype the folder or toggle recursive and press Generate
       before the `change` handler has run. The same `LivePrepDeclaration.describes` check the
       Analyze button uses answers that **without** stat-ing the folder, rescanning or probing
       runtime identity: it is practical equality on the normalised folder and the exact flag;
    3. **the library is fully prepared** — P3's value evidence was produced on complete candidate
       pools, so a partial scan's aggregate describes a subset and extrapolating from it would be
       reading a statistic the user never finished producing;
    4. **the summary is provable** — an empty or unusable one is UNAVAILABLE, never fabricated.

    This is deliberately **not** a generation failure. The semantic-IR proposal is produced either
    way; only the deterministic media step is skipped, and the read-out says so.
    """
    state = prep_state
    scan = getattr(state, 'scan', None)
    if scan is None:
        return None, fork_director.MEDIA_NOTE_NO_SCAN

    live = fork_prep.LivePrepDeclaration.from_widgets(prep_folder, prep_recursive)
    if not live.describes(scan):
        return None, fork_director.MEDIA_NOTE_STALE_SCAN

    if not scan.is_fully_prepared():
        return None, fork_director.MEDIA_NOTE_NOT_PREPARED

    summary = scan.usable_media_summary()
    if summary is None:
        return None, fork_director.MEDIA_NOTE_NO_SUMMARY

    return summary, fork_director.MEDIA_NOTE_CHECKED_NO_CHANGE


def _on_generate_director_proposal(director_instruction, prep_folder, prep_recursive,
                                   prep_state) -> Tuple:
    """Turn one instruction into a reviewable proposal. Writes no execution widget.

    **What the model sees is still only the instruction.** The three preparation inputs are read by
    `_eligible_media_summary` and reach deterministic local code alone — no media summary, filename,
    frame, record or count is ever interpolated into the prompt, so there is no media
    prompt-injection surface and `DIRECTOR_MODEL_SEES_MEDIA` stays `NO`. The Director still does not
    read the Variation Seed, the six sliders, the preset or any Variant Lab state, so the sentence
    is an absolute editing intention rather than a transformation of what is on screen. There is no
    cache and no history: every press is independent.

    **The ordering is the contract, and the seed is minted last.** Instruction, runtime, model,
    strict intent parse, BASE resolution and the media step all complete before
    `fork_variation.random_seed()` is called — the one implementation every seed in this application
    comes from, called here because the pure Director owns no randomness. Minting earlier would turn
    a malformed response into a plausible-looking half proposal, which is exactly what
    `CreativeRecipe`'s all-or-nothing boundary exists to prevent. BASE and FINAL then share that one
    seed, so the media step cannot look like it re-rolled the clip selection.

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

    parsed = fork_director.parse_semantic_intent(stdout)
    if parsed is None:
        return None, '', fork_director.STATUS_INVALID_PAYLOAD
    intent, explanation = parsed

    summary, media_note = _eligible_media_summary(prep_folder, prep_recursive, prep_state)

    proposal = fork_director.build_proposal(
        instruction, intent, explanation, fork_variation.random_seed(),
        summary=summary, media_note=media_note)
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
                                    output_profile: str, variation_seed: int,
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
                                    # [FORK] R7.1: appended with legacy defaults -- no existing
                                    # caller or positional index moves, and omitting them
                                    # reproduces the previous behaviour exactly. Placed BEFORE
                                    # `lifecycle` because the C3-R1A cancellation seam requires
                                    # `lifecycle` to remain the LAST parameter of every seam it
                                    # threads through; both of these are keyword-passed, so the
                                    # ordering costs nothing.
                                    voice_auto_balance: bool = False,
                                    voice_music_return: float = 0.4,
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
        # [FORK] R7.1: both normalised on construction like every other field. With Auto Balance
        # off, `normalize_music_return` leaves the value alone, so the legacy 0.4s release — and
        # therefore the legacy filtergraph — is what reaches the executor.
        auto_balance=voice_auto_balance,
        music_return_seconds=voice_music_return,
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
        output_profile=output_profile,
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

#: [FORK] Digital-Union (Raw Cutter V1): the cutter Cancel button's own lane, deliberately neither
#: `RENDER_CONCURRENCY_ID` nor `CANCEL_CONCURRENCY_ID`. Same reasoning as the render Cancel -- a
#: stop queued behind the job it is meant to stop would only run once that job had finished -- and
#: its own id so a cutter stop and a render stop can never queue behind one another.
CUTTER_CANCEL_CONCURRENCY_ID = 'beatsync-cutter-cancel'

RENDER_BUSY_MESSAGE = (
    "⏳ A render is already running. Wait for it to finish before starting another — "
    "BeatSync renders one video at a time."
)

# ---------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): render <-> verification exclusion.
# ---------------------------------------------------------------------------
#
# Source verification issues up to four concurrent FFprobe reads against the same mechanical drive
# a render extracts clips from. D0 established that exactly this kind of contention is what made
# clip extraction exceed its 120 s bound (four NVENC workers against a 5400-RPM source disk), so
# the two must never run together.
#
# **The mutex is reused, not duplicated.** `_RENDER_LOCK` is already a plain non-reentrant
# `threading.Lock`, acquired non-blockingly at exactly two sites and released in `finally`. Taking
# that same lock for verification yields bidirectional exclusion from one atomic test-and-set in
# each direction, with no second lock to order against it and therefore no deadlock to reason
# about:
#
#   * Verify while a render runs  -> `acquire(blocking=False)` fails -> verification refuses.
#   * Render while Verify runs    -> the EXISTING `acquire(blocking=False)` fails -> render refuses.
#
# The second direction needed no change to the render path at all; it already refuses when the lock
# is held, and it does not care who holds it. That is the whole reason this is a reused mutex rather
# than a new flag: `if not lock.locked(): start_work()` is a check-then-act race, and a second lock
# would have to be ordered against the first. One `acquire(blocking=False)` is the test and the
# acquisition in a single indivisible step.
#
# **R3: the LOCK is only half the story, and the Gradio queue was quietly the other half.** S2-A
# put the Verify event in `RENDER_CONCURRENCY_ID` with `concurrency_limit=1`, so a Verify clicked
# during a render never reached the `acquire` above at all -- Gradio's `Queue.get_events()` skips a
# concurrency group that is at its limit and leaves the event sitting in the queue. Measured on the
# pinned gradio==6.19.0: the handler was entered only after the render finished, then ran
# automatically against the folder value captured at CLICK time and overwrote the newer
# "folder changed" invalidation. Both refusal messages were therefore unreachable, and the rule
# text promising an immediate refusal described something the operator could never see. The Verify
# event now has NO shared `concurrency_id` and `concurrency_limit=None`, so its handler always
# executes and the mutex really is the gate -- re-measured on the same version.
#
# Nothing is stored in `gr.State`: no lock, no thread, no process handle, no cancellation object.
# The cross-request state is a boolean (so a refusal can name the real holder) and a bounded
# session->revision map (so a report cannot be published for a folder the operator has since
# changed). Both are plain immutable values behind one small lock.
#
# Deliberately NOT extended to the preparation Scan/Analyze buttons. Those are the operator's own
# long-running maintenance workflow in the same accordion; serializing them against verification
# would be new behaviour for existing controls, and any new serialization must stay narrow.
# Verification is bounded and read-only, so overlapping it with a Scan costs disk
# throughput and nothing else -- unlike a render, which has a hard per-clip timeout that contention
# can actually break.
_VERIFY_FLAG_LOCK = threading.Lock()

#: [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): WHO holds `_RENDER_LOCK` right now, or `''`.
#
# This replaces the `_verification_running` boolean, and the reason is a correctness one rather
# than tidiness. With two holders a boolean was sufficient only because the render path never
# marked itself: `_busy_message` read `False` and concluded "a render" BY ELIMINATION. A third
# holder makes that inference false in both directions -- a render refused while a sample decode
# ran would have claimed another render was running, and S2-A would have called a sample a render.
#
# So the holder is now stated, never inferred. Three facts make this safe to add:
#
#   * It is NOT an admission authority. `_RENDER_LOCK.acquire(blocking=False)` remains the one
#     indivisible test-and-set that decides whether any operation may start. This token is read
#     only AFTER an acquire has already failed, and only to word the refusal.
#   * A missed, delayed or stale token can therefore never permit a second worker. The worst it can
#     do is word one refusal badly, which is why the unknown case has its own honest message
#     instead of a guess (see `MEDIA_BUSY_UNKNOWN_TEXT`).
#   * Every holder marks immediately AFTER a successful acquire and clears immediately BEFORE the
#     release, inside the same `finally` that releases. An exception, a `BaseException` or an
#     abandoned generator cannot leave the token set, because the clear sits on the same path as
#     the release it precedes -- if the mutex is free, the token is empty.
_LOCK_HOLDER_RENDER = 'render'
_LOCK_HOLDER_VERIFY = 'verify'
_LOCK_HOLDER_SAMPLE = 'sample'
#: [FORK] Digital-Union (Raw Cutter V1): the FOURTH holder of the one `_RENDER_LOCK`.
#
# Cutting a long source re-encodes it segment by segment against the same drive a render extracts
# clips from, which is heavier disk work than either diagnostic already sharing this mutex. D0
# traced clip-extraction timeouts to exactly that contention, so the two must be exclusive.
#
# Reusing the existing mutex is what buys BOTH directions for one acquire and no new primitive: a
# cut refuses while a render runs, and the render path already refuses when the lock is held
# without caring who holds it, so no render code changed. The alternative -- a private cutter lock
# plus `if not _RENDER_LOCK.locked()` -- is the check-then-act race this file bans outright.
_LOCK_HOLDER_CUTTER = 'cutter'
_lock_holder: str = ''

#: [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): per-session verification revisions.
#
# One small integer per Gradio session, bumped when that session's folder or recursive setting
# changes, so a verification that finishes AFTER an invalidation can tell that its answer is no
# longer about the selected library.
#
# **Session-scoped, deliberately not process-global.** A single global counter would let a folder
# edit in one browser session invalidate a legitimate, still-running verification in another. The
# key is `gr.Request.session_hash`, which was measured on gradio==6.19.0 to be injected into both
# the `.click` and the `inputs=None` `.change` handler, stable within a session and distinct
# between two independent clients.
#
# **Bounded, and the bound IS the cleanup.** `demo.unload` was measured NOT to fire for a closed
# API client on this version, so no end-of-session hook is relied on. Instead the map holds at most
# `_VERIFY_REVISION_CAP` entries with least-recently-touched eviction, and an entry is only ever
# created by an actual invalidation -- a session that never edits the folder never appears here.
#
# **R5: eviction used to be able to publish a stale report, and now cannot.** The R3 text claimed
# eviction "can only ever make a comparison fail". That was wrong, and an independent review
# reproduced it against these functions. `_verify_revision` returns 0 for an ABSENT key, so absence
# and "revision zero" were indistinguishable:
#
#   1. Session A starts a verification with no entry yet -- it captures revision 0.
#   2. A's folder changes; `_bump_verify_revision` stores A = 1.
#   3. `_VERIFY_REVISION_CAP` OTHER sessions invalidate, and A -- whose verification is still
#      running -- is evicted as least-recently-touched.
#   4. The terminal check reads A again, finds it absent, and gets 0 back. `0 == 0`, so the report
#      for the folder the operator has already left is published as current.
#
# Measured exactly: 511 other sessions were safe, 512 published the stale report. The same hole had
# an ABA form -- an entry evicted at revision N and recreated at N by a later invalidation.
#
# The fix uses the invariant that already exists. `_RENDER_LOCK` permits at most ONE verification at
# a time, so at most one session's revision is load-bearing at any instant. That session is pinned
# in `_verify_active_session` for exactly the span of its own verification, and the eviction loop
# skips it. Everything else is unchanged: other sessions evict normally, the cap still bounds the
# map INCLUDING the pinned entry (there are always >= cap evictable keys when the cap is exceeded,
# because only one key can be pinned), reads still never insert, and an unedited session still
# never appears. Once the verification ends the pin is dropped and that entry is ordinary again.
#
# The pin is a plain string and nothing else -- no lock, thread, event, handle or lifecycle object
# enters `_verify_revisions` or `gr.State`, and nothing is persisted.
_VERIFY_REVISION_CAP = 512
_verify_revisions: Dict[str, int] = {}

#: The session whose verification is running right now, or `''`. Capacity exactly one, because
#: `_RENDER_LOCK` already guarantees exactly one verification -- this is not a second authority on
#: whether a verification may start, only a note of whose revision must survive eviction while it
#: does. Set by `_begin_verify_session` after the mutex is held and cleared by
#: `_end_verify_session` in a `finally`, so a refused click never pins anything and no terminal
#: path can leave it set.
_verify_active_session: str = ''


def _mark_lock_holder(holder: str) -> None:
    """Publish WHO holds the shared mutex, or `''` for nobody.

    Called immediately after acquiring `_RENDER_LOCK` and immediately before releasing it, so a
    reader that has just FAILED to acquire sees the correct holder. The one-statement window
    between the acquire and this call can only mislabel a refusal message, never admit a second
    worker: the mutex, not this token, is the exclusion. That window is why `''` has its own honest
    message rather than being folded into any particular holder.
    """
    global _lock_holder
    with _VERIFY_FLAG_LOCK:
        _lock_holder = str(holder or '')


def _lock_holder_now() -> str:
    """WHO holds `_RENDER_LOCK` right now: `'render'`, `'verify'`, `'sample'` or `''` for unknown.

    Two sources, both POSITIVE, neither an elimination:

      * the diagnostic token, set explicitly by the verification and sample paths;
      * the **existing** active-render slot, which is positive evidence of a render.

    The render case deliberately needs no new marking in the render wrappers. `_install_active_render`
    is called by a mutex-owning wrapper immediately after its acquire, and `_clear_active_render`
    immediately before its release, so an installed slot means a render owns the mutex -- that is
    the invariant `_install_active_render`'s own docstring already states, and the Cancel path only
    ever reads or clears the slot, never installs it. Reusing it keeps `process_video_guarded` and
    `render_selected_variants_guarded` byte-identical to the pre-S2-B implementation, which is what
    keeps every render lifecycle, cancellation and generator-shape regression valid unchanged.

    **Absence of evidence is reported as unknown, never as a render.** The old boolean read `False`
    as "a render" because the render path never marked itself; that inference is what a third holder
    makes false. Here, `''` plus no installed slot is the genuine one-statement window between an
    acquire and the holder becoming observable, and it gets its own honest message.

    **The two locks are taken SEQUENTIALLY and never nested**, so there is no acquisition order to
    get wrong and no inversion is even expressible: the token is copied under `_VERIFY_FLAG_LOCK`
    and that lock released, then the slot's presence is copied under `_ACTIVE_RENDER_SLOT_LOCK` and
    that lock released, and only then is the decision made. Mirrors the existing read in
    `_signal_active_render`, which copies the slot under its lock and acts outside it.

    This is diagnostic only. `_RENDER_LOCK.acquire(blocking=False)` remains the sole authority on
    whether any operation may start, so a stale, missing or delayed answer here can never admit a
    second worker -- it can only word one refusal.
    """
    with _VERIFY_FLAG_LOCK:
        token = _lock_holder
    if token:
        return token
    with _ACTIVE_RENDER_SLOT_LOCK:
        render_active = _active_render_slot is not None
    return _LOCK_HOLDER_RENDER if render_active else ''


def _mark_verification(active: bool) -> None:
    """[FORK] Digital-Union (MEDIA-VERIFY-S2-A): retained verification-side spelling.

    Kept as the verification path's own call so that path reads exactly as it did, while the shared
    state underneath is now the three-way token. It is a thin alias, never a second flag.
    """
    _mark_lock_holder(_LOCK_HOLDER_VERIFY if active else '')


def _busy_message() -> str:
    """The accurate refusal for a RENDER caller that just failed to take `_RENDER_LOCK`.

    Three named holders and one honest unknown. Nothing here is decided by elimination: before
    S2-B, `False` was read as "a render", which was only ever true because the render path did not
    mark itself. It now does, so an empty token means the holder has not published yet -- a real
    window of one statement -- and that is reported as an unattributed media operation rather than
    guessed at.
    """
    holder = _lock_holder_now()
    if holder == _LOCK_HOLDER_VERIFY:
        return VERIFY_BUSY_MESSAGE
    if holder == _LOCK_HOLDER_SAMPLE:
        return SAMPLE_BUSY_MESSAGE
    if holder == _LOCK_HOLDER_CUTTER:
        return CUTTER_BUSY_MESSAGE
    if holder == _LOCK_HOLDER_RENDER:
        return RENDER_BUSY_MESSAGE
    return MEDIA_BUSY_UNKNOWN_TEXT


def _verify_busy_message() -> str:
    """The accurate refusal for a VERIFY caller that just failed to take `_RENDER_LOCK`.

    The mirror of `_busy_message`, and it exists for the same reason: telling the operator "a
    render is running" when another verification actually owns the mutex is a wrong answer, and the
    cases need different advice (wait for the render, versus this one is already underway, versus a
    sample decode is using the drive).
    """
    holder = _lock_holder_now()
    if holder == _LOCK_HOLDER_VERIFY:
        return PREP_VERIFY_ALREADY_RUNNING_TEXT
    if holder == _LOCK_HOLDER_SAMPLE:
        return PREP_VERIFY_SAMPLE_BUSY_TEXT
    if holder == _LOCK_HOLDER_CUTTER:
        return CUTTER_BUSY_MESSAGE
    if holder == _LOCK_HOLDER_RENDER:
        return PREP_VERIFY_RENDER_BUSY_TEXT
    return MEDIA_BUSY_UNKNOWN_TEXT


def _sample_busy_message() -> str:
    """The accurate refusal for a SAMPLE DECODE caller that just failed to take `_RENDER_LOCK`.

    The third face of the same table. Added rather than reusing `_verify_busy_message` because the
    operator is told which control to press again, and "press Verify Sources again" is the wrong
    instruction for someone who pressed Sample Decode.
    """
    holder = _lock_holder_now()
    if holder == _LOCK_HOLDER_SAMPLE:
        return PREP_SAMPLE_ALREADY_RUNNING_TEXT
    if holder == _LOCK_HOLDER_VERIFY:
        return PREP_SAMPLE_VERIFY_BUSY_TEXT
    if holder == _LOCK_HOLDER_CUTTER:
        return CUTTER_BUSY_MESSAGE
    if holder == _LOCK_HOLDER_RENDER:
        return PREP_SAMPLE_RENDER_BUSY_TEXT
    return MEDIA_BUSY_UNKNOWN_TEXT


def _verify_session_key(request) -> str:
    """The session this verification request belongs to, or a shared fallback bucket.

    A request without a usable `session_hash` (a scripted API call, a transport that did not carry
    one) falls into one named bucket rather than into the per-session map: the fallback must not be
    a *unique* key, or an invalidation could never match the verification it belongs to.
    """
    session = getattr(request, "session_hash", None) if request is not None else None
    return str(session) if session else "__no_session__"


#: [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the two revision SCOPES sharing one bounded map.
#
# S2-A invalidates on folder/recursive; S2-B invalidates on file/seek. They are independent
# diagnostics, so one must not invalidate the other's pending answer -- a seek edit clearing a
# running source verification would be a wrong answer, not merely a conservative one.
#
# Scoping the KEY rather than adding a second map is the whole point. Every function below
# (`_touch_verify_revision_locked`, `_verify_revision`, `_bump_verify_revision`,
# `_begin_verify_session`, `_end_verify_session`) is key-agnostic and is reused UNCHANGED, so the
# bound, the LRU order, the active-session pin and the R5 eviction exemption are not duplicated,
# not re-derived and not re-proved -- they are the same code operating on a longer string.
#
# The R5 correctness argument survives intact because it rests on `_RENDER_LOCK`, not on the key
# shape: at most ONE of {render, verification, sample} runs at a time, so at most one revision is
# load-bearing at any instant, so pinning one key and skipping it during eviction is still
# sufficient. The pinned value is simply a scoped key now.
_SCOPE_VERIFY = 'verify'
_SCOPE_SAMPLE = 'sample'


def _scoped_key(session: str, scope: str) -> str:
    """One revision key for `(session, scope)`.

    `\\x00` is the separator because a Gradio `session_hash` is a hex string and the fallback
    bucket is `__no_session__`, so neither can contain a NUL -- two scopes of one session can
    therefore never collide, and no scope can forge another's key by naming.
    """
    return f"{session}\x00{scope}"


def _touch_verify_revision_locked(session: str) -> int:
    """This session's revision, moved to the most-recently-touched end. Caller holds the lock.

    A read, never an insert: an absent session reports 0 and STAYS absent, so a session that never
    edits its folder never occupies a slot. Absence reporting 0 is exactly what made eviction
    dangerous before R5, which is why the active session is pinned rather than inserted here --
    inserting on read would let any idle session hold a slot, and the R3 contract rules that out.
    """
    value = _verify_revisions.get(session)
    if value is None:
        return 0
    del _verify_revisions[session]
    _verify_revisions[session] = value
    return value


def _verify_revision(session: str) -> int:
    """This session's current revision. A read, never an insert -- an unedited session stays absent.

    The standalone accessor. The verification click does NOT use it: capturing a revision and
    pinning the session have to happen in one locked step (see `_begin_verify_session`), because a
    read-then-pin sequence would let an invalidation land in between and be captured as the
    baseline instead of being detected.
    """
    with _VERIFY_FLAG_LOCK:
        return _touch_verify_revision_locked(session)


def _bump_verify_revision(session: str) -> int:
    """Invalidate this session's pending verification result. Bounded, with LRU eviction.

    Eviction skips `_verify_active_session`. That one exemption is the R5 correction: the running
    verification's revision is the only one whose disappearance could be misread as "revision 0,
    nothing has changed", and publishing an answer about a folder the operator has already left is
    the exact failure this map exists to prevent.

    The cap still bounds the map including the pinned entry. When the cap is exceeded there are at
    least `_VERIFY_REVISION_CAP` keys available to drop, since at most one key is ever pinned, so
    the loop always makes progress; the `break` is a belt-and-braces guard, not a reachable path.
    """
    with _VERIFY_FLAG_LOCK:
        value = _verify_revisions.pop(session, 0) + 1
        _verify_revisions[session] = value
        while len(_verify_revisions) > _VERIFY_REVISION_CAP:
            # Insertion order is touch order, so the first key is the least recently touched --
            # except the active verification's, which must outlive every other session's churn.
            victim = next((key for key in _verify_revisions
                           if key != _verify_active_session), None)
            if victim is None:
                break
            del _verify_revisions[victim]
        return value


def _begin_verify_session(session: str) -> int:
    """Pin `session` as the active verification AND capture its revision, in one locked step.

    Called only after `_RENDER_LOCK` has been acquired and before any verification work, so a
    refused click never pins and never leaves active state behind.

    Atomic on purpose. Pinning and reading as two separate locked operations would reopen the race
    in a new place: an invalidation arriving between them would already be folded into the captured
    baseline, and the terminal comparison would then see no change and publish the stale report.
    """
    global _verify_active_session
    with _VERIFY_FLAG_LOCK:
        _verify_active_session = session
        return _touch_verify_revision_locked(session)


def _end_verify_session(session: str, revision: int) -> bool:
    """Decide staleness and release the pin, in one locked step. True means withhold the answer.

    The order inside the lock is load-bearing and is the whole point of this function existing:
    the comparison happens while the entry is still protected, and only then is the pin dropped.
    Unpinning first and comparing afterwards would restore the original defect, because the entry
    could be evicted in that gap and read back as 0.

    Safe to call on every terminal path, including one where `_begin_verify_session` never ran:
    clearing the pin is conditional on this session actually holding it.
    """
    global _verify_active_session
    with _VERIFY_FLAG_LOCK:
        current = _touch_verify_revision_locked(session)
        if _verify_active_session == session:
            _verify_active_session = ''
        return current != revision

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


def _on_open_output_folder_click() -> None:
    """[FORK] Digital-Union (Deliverable Export V1-B-S2): the Open Output Folder button's ENTIRE
    handler. Takes no input, writes no component, and returns nothing.

    The product owner rejected a `gr.DownloadButton` in favour of this, and the rejection is
    load-bearing rather than a style preference. A download control would have had to solve a
    problem V1-B-S1 already measured: a raw `output/` path is served **403** by
    `route_utils.file_fetch`, so making one work means either widening the served-path allowlist --
    which exposes the whole folder to every user of the app -- or copying each video into
    `GRADIO_TEMP_DIR`, i.e. a third copy of an artifact that is already durable on this very disk.
    This button needs neither: the files are already where the user wants them.

    **Zero inputs and zero outputs, both deliberate.** Nothing about which folder to open comes
    from the screen, so there is nothing to pass in: the path is the one process-global directory
    this installation renders into, resolved by the same helper the render path uses. It is
    explicitly NOT derived from the durable-output key, the outcome key, the status textbox or the
    video widget -- so the button works before the first render of a session, and a render in
    flight cannot change where it points. No user-controlled text reaches the OS call, which is why
    no quoting, escaping or shell involvement exists to get wrong.

    Feedback is a Gradio toast (`gr.Info` / `gr.Warning`) precisely so the handler writes no
    component. **The invariant is that this button adds NO new writer to `status_output`** -- not
    that the panel has only one. It already has three, and naming the status textbox in `outputs`
    here would make a fourth: `process_btn.click`, `render_selected_variants_btn.click` and
    `cancel_render_btn.click` all write it, and `tests/test_open_output_folder.py` asserts exactly
    that set. (An earlier version of this docstring claimed a single existing writer; that was
    wrong, and the distinction matters because the real requirement is additive.) What the toast
    buys is that opening a folder can never overwrite a render's result panel.

    **`os.startfile`, not a child process.** It is the stdlib Windows shell verb, so this adds no
    `run`/`Popen` site to a module whose count of two is pinned by test, and there is no
    `explorer.exe` command line to assemble. `getattr` rather than a bare attribute access because
    `os.startfile` exists only on Windows and the test suite runs on bare CPython anywhere.

    **It never claims Explorer appeared.** `startfile` hands the path to the shell and returns as
    soon as the request is accepted; a window could still fail to surface. The success toast
    therefore says the request was made. Visual confirmation is a human step, recorded as such.
    """
    # [FORK] R8: the SAME resolver the render publishes through, so this button always opens the
    # folder the last render actually landed in rather than a folder it might not have used.
    output_dir = get_render_output_dir()
    # Windows-only by construction, same as the rest of the app -- but a *missing* attribute is a
    # fact to report, not an exception to raise, because this runs inside a live Gradio event.
    open_in_shell = getattr(os, 'startfile', None)
    if open_in_shell is None:
        gr.Warning(STATUS_OPEN_FOLDER_UNSUPPORTED.format(path=output_dir))
        return
    # Checked, never created. The directory is made by `paths.ensure_project_dirs()` at import and
    # by the render itself; creating it here would make a read-only user action write to disk.
    if not os.path.isdir(output_dir):
        gr.Warning(STATUS_OPEN_FOLDER_MISSING.format(path=output_dir))
        return
    try:
        open_in_shell(output_dir)
    except OSError as exc:
        gr.Warning(STATUS_OPEN_FOLDER_FAILED.format(path=output_dir, reason=exc))
        return
    except Exception as exc:
        # Broad on purpose: an unhandled exception inside a Gradio event surfaces as a raw server
        # error, and no shell-association failure justifies that for a button that changes nothing.
        gr.Warning(STATUS_OPEN_FOLDER_FAILED.format(path=output_dir, reason=exc))
        return
    gr.Info(STATUS_OPEN_FOLDER_REQUESTED.format(path=output_dir))


def process_video_guarded(audio_file: str,
                          voice_files: VideoFilesInput, voice_start_delay: float,
                          voice_min_gap: float, voice_avoid_drops: bool,
                          music_under_voice: int,
                          sfx_folder: str, sfx_roles, sfx_amount: int, sfx_level: int,
                          source_mode: str, source_folder: str,
                          source_recursive: bool, video_input: VideoFilesInput,
                          output_filename: str, processing_mode: str,
                          output_profile: str, variation_seed: int,
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
                          # [FORK] R7.1 (Comfort Mix): appended at the END with legacy defaults,
                          # exactly as Freestyle was, so every existing positional index in the
                          # `process_btn.click(inputs=...)` contract is unchanged.
                          voice_auto_balance: bool = False,
                          voice_music_return: float = fork_audio_mix.DEFAULT_MUSIC_RETURN_SECONDS,
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
        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A): the refusal is unchanged -- only its wording is
        # now accurate about WHO holds the mutex, since read-only source verification can also hold
        # it. The gate itself is the same single non-blocking acquire.
        yield gr.skip(), _busy_message(), session_state, gr.skip(), gr.skip(), ''
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
            output_filename, processing_mode, output_profile, variation_seed,
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
            voice_auto_balance=voice_auto_balance,
            voice_music_return=voice_music_return,
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
        output_filename: str, processing_mode: str, output_profile: str,
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
        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A): same accurate wording as the single-render
        # wrapper; the gate is the unchanged single non-blocking acquire.
        _busy = _busy_message()
        yield gr.skip(), _busy, session_state, _busy, ''
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
                        candidate.output_stem, processing_mode, output_profile,
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
        # [FORK] Digital-Union: the small per-source moment counts the classifier accumulated while
        # it already held each reusable record. Converted to the frozen summary inside
        # `build_scan_result`; the GUI never re-reads a cache record to build it.
        source_candidate_counts=classification.get("source_candidate_counts"),
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


# ---------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source verification.
# ---------------------------------------------------------------------------

def _verify_sources_impl(folder_path: str, recursive: bool) -> str:
    """Answer the two verification questions for the folder the widgets declare RIGHT NOW.

    Returns the report text and nothing else. It takes no `gr.State` and returns no state, by
    design: verification is a pure read of the disk, it owns no session state, and giving it one
    would be a second persistence layer for media-health results that nothing needs.

    Why the LIVE widget values rather than `prep_state`
    ---------------------------------------------------
    Gradio delivers widget changes as separate queued events, so a user can retype the folder and
    click Verify before the `change` handler has run. Reusing the recorded scan would then verify
    the *previous* library while the screen declared a different one -- the same hazard the render
    gate and `_prep_analyze_impl` already take live widget values to avoid. This enumerates the
    folder itself, with the existing scanner and the live recursive flag, rather than trusting any
    prior scan.

    What it deliberately does NOT do
    --------------------------------
    No Qwen worker is launched (the classifier answers the cache question from records alone), no
    analysis runs, no cache record is written, checkpointed or deleted, no media is modified, and
    nothing is rendered. It writes no file at all -- the report lives in one textbox, because a
    persistent report artifact is a later, separately reviewed milestone.
    """
    from ffmpeg_processing import probe_media_metadata
    from video_analysis import classify_library_sources

    folder = (folder_path or "").strip()
    recursive = bool(recursive)
    verified_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not folder:
        return PREP_VERIFY_NO_FOLDER_TEXT

    # 1. Enumerate the live folder with the existing scanner. `detect_duplicates=False` matches
    #    `_prep_scan_impl`: duplicate grouping is a source-screen reporting feature and would be
    #    pure extra reads here.
    scan_started = time.perf_counter()
    try:
        input_set = scan_library_folder(folder, recursive=recursive, detect_duplicates=False)
    except (InputScanError, OSError) as exc:
        return (f"SOURCE VERIFICATION FAILED\n  Folder: {folder}\n"
                f"  Include subfolders: {'yes' if recursive else 'no'}\n"
                f"  Verified at: {verified_at}\n\n{exc}")
    folder_scan_seconds = time.perf_counter() - scan_started
    sources = [media.path for media in input_set.files]

    # 2. [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): one cheap `(size, mtime_ns)` observation per
    #    source, taken HERE -- before the cache phase looks at anything. This is the whole of the
    #    cross-phase fix: without it, a file overwritten between being classified and being probed
    #    produced PREPARED (old bytes) beside METADATA_OK (new bytes) under one COMPLETE banner.
    #    One `os.stat` per source, aligned BY POSITION so a duplicate path keeps its own
    #    observation. No hash, no second identity formula, nothing persisted.
    baselines = fork_media_verify.fingerprint_sources(sources)

    # 3. The cache question, through the EXISTING Stage-5 classifier. No new key formula, no new
    #    completion rule, and the same Qwen enablement/model resolution the preparation workflow
    #    uses -- `_resolve_prep_qwen_runtime` is shared, not reimplemented.
    cache_started = time.perf_counter()
    qwen_enabled, model_path = _resolve_prep_qwen_runtime()
    classification = classify_library_sources(
        sources, enable_ai=qwen_enabled, qwen_model_path=model_path)
    cache_seconds = time.perf_counter() - cache_started

    # An unprovable backend identity means no per-source cache verdict can EXIST. It is reported as
    # UNVERIFIABLE, never as evidence that the cache is damaged -- the classifier returns empty
    # counts in that state, and presenting them as "0 reusable" would be a fabricated verdict.
    unverifiable = ""
    if classification.get("ai_cache_disabled"):
        unverifiable = ("The Qwen backend identity could not be verified, so Stage-5 cache records "
                        "cannot be looked up for this library.")

    # 4. The media question. Independent of the cache answer, and bounded: at most four probe
    #    workers, a per-file timeout, and an overall budget after which unreached sources are
    #    reported as not checked rather than quietly omitted. The baselines from step 2 ride along,
    #    so each verdict can see whether its own file moved since its own classification.
    run = fork_media_verify.verify_media_health(
        sources, probe_media_metadata, baselines=baselines)

    return fork_media_verify.format_verification_report(
        folder=folder, recursive=recursive, verified_at=verified_at,
        source_count=len(sources),
        cache=classification,
        cache_classifications=classification.get("classifications") or (),
        run=run,
        folder_scan_seconds=folder_scan_seconds,
        cache_seconds=cache_seconds,
        cache_unverifiable_reason=unverifiable,
    )


def _on_verify_sources_click(folder_path: str, recursive: bool,
                             request: gr.Request = None) -> str:
    """The Verify Sources click: take the shared render mutex, or refuse immediately.

    One non-blocking `acquire` is both the test and the acquisition, so there is no window in which
    a render could start alongside an accepted verification -- and none in which this could start
    alongside a render. A render that arrives while this holds the lock hits the EXISTING
    non-blocking acquire in `process_video_guarded` / the batch wrapper and refuses; nothing in the
    render path needed a new check.

    This is only an *immediate* refusal because the event is no longer queued behind the render
    lane -- see the exclusion block above. Measured on gradio==6.19.0, not inferred.

    The release is in `finally`, so an exception, an early return or an abandoned request cannot
    leave the application permanently refusing renders. A running render is never signalled,
    throttled or terminated to make room for this.

    `request` is Gradio's injected `gr.Request`, not a widget: it carries the session identity used
    to decide, at the very end, whether this answer is still about the library the operator has
    selected. It is read for `session_hash` and nothing else, defaults to `None` so the function
    stays callable without Gradio, and never reaches `gr.State`.

    Four states, and the revision bookkeeping exists to keep them distinguishable
    ---------------------------------------------------------------------------
    *Starting* (mutex taken, pin and baseline established together), *running* (an invalidation may
    arrive at any moment and must survive other sessions' churn), *finished but not published* (the
    comparison is made while the entry is still pinned), *published or withheld*. R5's correction is
    entirely about the third state: before it, the entry could be evicted between the second and
    third, and its absence read back as "revision 0, nothing changed".
    """
    # [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): scoped to 'verify' so a sample-decode input edit
    # cannot invalidate this verification, and this folder edit cannot invalidate a sample.
    session = _scoped_key(_verify_session_key(request), _SCOPE_VERIFY)

    if not _RENDER_LOCK.acquire(blocking=False):
        # Names the real holder: a render, or another verification already underway. Nothing is
        # pinned and no revision is captured on this path -- a refused click leaves no state at all.
        return _verify_busy_message()
    _mark_verification(True)
    # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R5): pinned and captured together, AFTER the mutex is
    # held and BEFORE any work, so this session's revision cannot be evicted by other sessions'
    # churn while the verification it belongs to is still running. `stale` is decided inside the
    # same `finally` that drops the pin, so there is no window between the two.
    #
    # `revision` is pre-set because the `finally` below reads it: if `_begin_verify_session` itself
    # ever failed, the cleanup still has to run with a defined baseline. `stale` needs no such
    # default -- the `finally` always assigns it, and the only way past it without one is an
    # exception propagating, which never reaches the comparison.
    revision = 0
    try:
        revision = _begin_verify_session(session)
        try:
            report = _verify_sources_impl(folder_path, recursive)
        except Exception as exc:
            # Total: a verification that raised must report, not vanish behind Gradio's generic
            # error and not leave the operator unable to tell whether anything ran.
            import traceback
            traceback.print_exc()
            report = (f"SOURCE VERIFICATION FAILED\n  {type(exc).__name__}: {exc}\n\n"
                      f"No analysis, cache change, media change or render was performed.")
    finally:
        # The staleness decision happens HERE, while the entry is still pinned, and the pin is
        # released by the same locked call. Reached on every terminal path including a
        # `BaseException` propagating out, so the pin can never outlive its verification.
        stale = _end_verify_session(session, revision)
        # Cleared BEFORE the release, so a caller that fails the acquire a moment later attributes
        # the mutex to whoever actually holds it next.
        _mark_verification(False)
        _RENDER_LOCK.release()

    # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): the folder-change listener runs in its own
    # Gradio lane, so it can complete WHILE this is working. Publishing now would overwrite the
    # newer "the folder changed" notice with an older answer about a folder that is no longer
    # selected. The failure text is withheld for the same reason: it names a folder too.
    if stale:
        return PREP_VERIFY_STALE_TEXT
    return report


def _on_verify_invalidate(request: gr.Request = None) -> str:
    """Clear the verification report when the folder or recursive setting changes.

    A separate listener rather than an addition to `prep_outputs`: the preparation state machine is
    untouched by verification and must stay that way, so this writes ONLY the verification textbox
    and shares no outputs with the preparation events.

    It also bumps this SESSION's verification revision, which is what stops a verification already
    in flight from publishing its older answer over this notice. Session-scoped on purpose: a
    folder edit here must not invalidate a legitimate verification running in a different browser
    session.
    """
    # [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the 'verify' scope only -- a folder edit must not
    # invalidate a sample decode that is running for a different question.
    _bump_verify_revision(_scoped_key(_verify_session_key(request), _SCOPE_VERIFY))
    return PREP_VERIFY_STALE_TEXT


# ---------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the one-video sample-decode diagnostic.
# ---------------------------------------------------------------------------
#
# S2-A asks whether a library's container metadata can be read; this asks whether FFmpeg can
# actually produce output frames at one position in one video. Everything that decides anything is
# the ACCEPTED P1 core: `run_sample_check` orchestrates, `classify_decode` decides the status,
# `parse_progress_output` reads the evidence and `format_sample_report` writes the report. This
# layer contributes no classification, no stream selection, no progress parsing and no subprocess
# handling -- and it deliberately passes NO resource arguments, so there is no parameter for a
# widget value to widen (see `_sample_decode_impl`).
def _sample_decode_impl(video_path: str, seek_seconds) -> str:
    """Run one bounded sample decode and return the operator report. Nothing else.

    Takes no `gr.State` and returns none, exactly like `_verify_sources_impl`: a sample is a pure
    read of the disk and owns no session state.

    The resource policy is enforced BY OMISSION, which is stronger than validation: this calls
    `run_sample_check` with `probe=` and `decode=` only, so `requested_frames`,
    `decode_timeout` and `budget_seconds` all come from the module's own constants
    (30 frames / 20 s probe / 30 s decode / 60 s budget, one source, one worker). There is no
    argument here for a GUI value to reach, and no widget exists that could supply one.

    Validation is also inherited rather than rewritten. `run_sample_check` already rejects a
    non-string, empty or wrong-suffix path, a path that is not a regular file, and a seek that is
    not finite and non-negative -- each as `SAMPLE_INVALID_REQUEST` with no child started. A seek
    at or past the selected stream's reported duration is `SAMPLE_SEEK_OUT_OF_RANGE`. Nothing is
    clamped, so the report's requested position is always the one the operator asked for.
    """
    from ffmpeg_processing import probe_media_metadata, sample_decode_video

    path = (video_path or "").strip() if isinstance(video_path, str) else video_path
    if isinstance(path, str) and not path:
        return PREP_SAMPLE_NO_PATH_TEXT

    verdict = fork_media_sample.run_sample_check(
        path, seek_seconds,
        probe=probe_media_metadata,
        decode=sample_decode_video,
    )
    return fork_media_sample.format_sample_report(verdict)


def _on_sample_decode_click(video_path: str, seek_seconds,
                            request: gr.Request = None) -> str:
    """The Sample Decode click: take the shared render mutex, or refuse immediately.

    Structurally identical to `_on_verify_sources_click`, and that is the point -- it is the third
    holder of ONE mutex, not a new exclusion mechanism. A single non-blocking `acquire` is both the
    test and the acquisition, so there is no window in which a render, a verification or a second
    sample could run alongside this one. A render arriving while this holds the lock hits the
    EXISTING non-blocking acquire in `process_video_guarded` / the batch wrapper and refuses;
    nothing in the render path needed a new check.

    The refusal is *immediate* only because this event has no shared `concurrency_id` and
    `concurrency_limit=None` -- the same wiring S2-A's R3 correction established after measuring
    that a shared lane leaves the event QUEUED and then runs it automatically against click-time
    values. A refusal here is terminal: it performs no work and schedules nothing.

    The release is in `finally`, so an exception, an early return or an abandoned request cannot
    leave the application permanently refusing renders, and the holder token is cleared on the same
    path immediately before that release.

    `request` is Gradio's injected `gr.Request`, not a widget: it is read for `session_hash` only,
    defaults to `None` so the function stays callable without Gradio, and never reaches `gr.State`.
    """
    # Scoped to 'sample': this session's own file/seek edits invalidate it, a folder edit does not,
    # and another session's edits never do.
    session = _scoped_key(_verify_session_key(request), _SCOPE_SAMPLE)

    if not _RENDER_LOCK.acquire(blocking=False):
        # Names the real holder -- render, verification, another sample, or honestly unknown for
        # the one-statement window before a holder publishes itself. Nothing is pinned and no
        # revision is captured on this path: a refused click leaves no state at all.
        return _sample_busy_message()
    _mark_lock_holder(_LOCK_HOLDER_SAMPLE)
    # Pinned and captured together, AFTER the mutex is held and BEFORE any work, so this session's
    # revision cannot be evicted by other sessions' churn while the sample it belongs to is still
    # running. `stale` is decided inside the same `finally` that drops the pin.
    revision = 0
    try:
        revision = _begin_verify_session(session)
        try:
            report = _sample_decode_impl(video_path, seek_seconds)
        except Exception as exc:
            # Total: a sample that raised must report, not vanish behind Gradio's generic error.
            import traceback
            traceback.print_exc()
            report = (f"SAMPLE DECODE FAILED\n  {type(exc).__name__}: {exc}\n\n"
                      f"No analysis, cache change, media change or render was performed.")
    finally:
        # The staleness decision happens HERE, while the entry is still pinned, and the pin is
        # released by the same locked call. Reached on every terminal path including a
        # `BaseException` propagating out, so neither the pin, the holder token nor the mutex can
        # outlive this sample.
        stale = _end_verify_session(session, revision)
        _mark_lock_holder('')
        _RENDER_LOCK.release()

    # The file/seek change listeners run in their own Gradio lane, so one can complete WHILE this
    # is working. Publishing now would overwrite the newer "selection changed" notice with an older
    # answer about a file or position that is no longer selected. The failure text is withheld for
    # the same reason: it names a path too.
    if stale:
        return PREP_SAMPLE_STALE_TEXT
    return report


# ============================================================================================
# [FORK] Digital-Union (Raw Cutter V1): split ONE long source video at its existing edits.
#
# A standalone footage-prep tool. It reaches NO render, no source-confirmation gate, no Stage-5
# cache, no Qwen worker, no Variant Lab / Director / Freestyle state, no output promotion and no
# `RenderLifecycle`. Cutting raw footage is not a render and must not acquire a render's
# machinery -- a structural test walks both handlers and enforces that.
#
# The division of labour is deliberate and is what keeps this out of `gui.py`'s weight class:
#
#   beatsync_fork/raw_cutter.py   PURE: threshold mapping, the segmentation planner, naming,
#                                 staleness identity, report text. Stdlib only, so the whole
#                                 policy is testable on a bare interpreter.
#   raw_cutter_export.py          the FFmpeg adapter: probe, scene detection, segment export.
#   here                          widgets, the mutex, cancellation and progress.
# ============================================================================================

#: Guards the Cancel button's view of the running job. Small and separate from `_RENDER_LOCK`:
#: this protects a one-slot reference, not admission. Admission is `_RENDER_LOCK` and nothing else.
_CUTTER_JOB_LOCK = threading.Lock()
_cutter_job = None


def _install_cutter_job(job):
    global _cutter_job
    with _CUTTER_JOB_LOCK:
        _cutter_job = job


def _clear_cutter_job() -> None:
    global _cutter_job
    with _CUTTER_JOB_LOCK:
        _cutter_job = None


def _cut_busy_message() -> str:
    """The accurate refusal for a CUT caller that just failed to take `_RENDER_LOCK`.

    The fourth face of the same table. Like its three siblings it names the real holder rather
    than deducing one, and it gives advice a Raw Cutter user can act on.
    """
    holder = _lock_holder_now()
    if holder == _LOCK_HOLDER_CUTTER:
        return CUT_BUSY_TEXT
    if holder == _LOCK_HOLDER_RENDER:
        return CUT_RENDER_BUSY_TEXT
    if holder in (_LOCK_HOLDER_VERIFY, _LOCK_HOLDER_SAMPLE):
        return CUT_MEDIA_BUSY_TEXT
    return MEDIA_BUSY_UNKNOWN_TEXT


# ============================================================================================
# [FORK] Digital-Union (Raw Cutter progress P1): a visible, TRUTHFUL progress surface.
#
# The problem this solves: `Analyze` and `Cut Video` both do long FFmpeg work, and before this
# the screen said nothing while they did it. An operator could not tell a slow cut from a hung
# one, and nothing stopped a second click landing on top of the first.
#
# The rule the whole surface obeys: **a number is only shown when it was measured.** The cutter
# measures exactly one thing it can turn into a percentage -- clips actually published over clips
# planned -- so that is the only determinate bar. Scene detection is one opaque FFmpeg pass, so it
# gets a named phase and an indeterminate bar. There is no ETA anywhere, and none is derivable:
# segment lengths vary, the encoder is not linear in wall-clock, and a guessed time would be a
# lie told with a straight face.
# ============================================================================================

#: Tones carry meaning, not decoration: an operator reads colour before text.
_CUT_TONES = {
    'active': ('#2f81f7', '#1f6feb'),     # work is genuinely running
    'done': ('#2ea043', '#238636'),       # a terminal success
    'stopping': ('#d29922', '#bb8009'),   # cancellation requested, not yet complete
    'failed': ('#f85149', '#da3633'),     # a terminal failure or refusal
}


def _cut_progress_html(title: str, detail: str = '', *, done=None, total=None,
                       note: str = '', tone: str = 'active') -> str:
    """One progress panel for the Raw Cutter. Returns HTML; never invents a number.

    Passing `done` and `total` produces a DETERMINATE bar whose percentage is exactly
    ``done / total``. They are only ever given counts the cutter has already recorded -- a clip
    is counted when `on_complete` fired for it, a source when its outcome was classified -- so
    the bar can stand still for a long time and still be honest.

    Omitting them produces an INDETERMINATE bar: a phase that is really running but whose
    internal progress FFmpeg does not report. That is shown as motion without a number, which is
    the truthful rendering of "working, amount unknown".

    `data-*` attributes are deliberate: they are the stable contract the browser tests read, so a
    test never has to parse presentation.
    """
    bar, fill = _CUT_TONES.get(tone, _CUT_TONES['active'])
    determinate = isinstance(done, int) and isinstance(total, int) and total > 0
    if determinate:
        done = max(0, min(int(done), int(total)))
        pct = int(round((done * 100.0) / total))
        width = f'{pct}%'
        counter = f'{done} / {total}'
        anim = ''
    else:
        pct = -1                      # -1 == "not measurable", never 0, which would read as stalled
        width = '100%'
        counter = ''
        # A slow sweep, so an indeterminate phase still looks alive rather than frozen.
        anim = ('background-image:linear-gradient(90deg,rgba(255,255,255,0) 0%,'
                'rgba(255,255,255,.45) 50%,rgba(255,255,255,0) 100%);'
                'background-size:40% 100%;background-repeat:no-repeat;'
                'animation:bsweep 1.4s linear infinite;')
    rows = [
        '<style>@keyframes bsweep{0%{background-position:-40% 0}100%{background-position:140% 0}}</style>',
        f'<div id="cut-progress-panel" data-state="{tone}" data-pct="{pct}" '
        f'data-done="{done if determinate else ""}" data-total="{total if determinate else ""}" '
        f'data-determinate="{"1" if determinate else "0"}" '
        'style="padding:10px 12px;border-radius:8px;border:1px solid rgba(255,255,255,.12);">',
        '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;">',
        f'<strong id="cut-progress-title" style="color:{bar};">{html_lib.escape(title)}</strong>',
        f'<span id="cut-progress-counter" style="opacity:.85;font-variant-numeric:tabular-nums;">'
        f'{html_lib.escape(counter)}</span>',
        '</div>',
        f'<div style="margin:8px 0 6px;height:10px;border-radius:999px;'
        f'background:rgba(255,255,255,.10);overflow:hidden;">'
        f'<div id="cut-progress-bar" style="height:100%;width:{width};border-radius:999px;'
        f'background:{fill};{anim}"></div></div>',
    ]
    if detail:
        rows.append(f'<div id="cut-progress-detail" style="opacity:.9;">'
                    f'{html_lib.escape(detail)}</div>')
    if note:
        rows.append(f'<div id="cut-progress-note" style="opacity:.65;font-size:.85em;'
                    f'margin-top:4px;">{html_lib.escape(note)}</div>')
    rows.append('</div>')
    return ''.join(rows)


def _cut_progress_clear() -> str:
    """The idle panel: empty, so nothing claims work is happening when none is."""
    return ''


def _cut_buttons(*, analyze: bool, run: bool, cancel: bool):
    """The three single-video controls, as one honest set.

    Returned together on every path on purpose. Setting them one at a time is how a surface ends
    up with Cancel still glowing after a job finished, or Cut Video enabled against a plan that no
    longer exists. These are a CONVENIENCE for the operator, never a safety gate -- admission is
    `_RENDER_LOCK` and plan validity is `plan_signature`, both unchanged and both still enforced
    server-side for a caller that never saw this UI.
    """
    return (gr.update(interactive=analyze),
            gr.update(interactive=run),
            gr.update(interactive=cancel, variant='stop'))


def _cut_batch_buttons(*, count: bool, start: bool, cancel: bool):
    """The batch controls. Same reasoning as `_cut_buttons`."""
    return (gr.update(interactive=count),
            gr.update(interactive=start),
            gr.update(interactive=cancel, variant='stop'))


def _on_cut_planning_input_change():
    """A planning setting moved, so the plan on screen no longer describes this source.

    Disables Cut Video and says why. This is presentation only: `_on_cut_run` still recomputes
    `plan_signature` against the LIVE widgets and the LIVE file and still refuses a stale plan on
    its own, which is what protects an API caller, a second browser tab and anyone who bypasses
    this handler entirely. Nothing is queued and no state is cleared -- the plan itself is left
    alone so a user who reverts the setting can still cut it.
    """
    return (gr.update(interactive=False),
            _cut_progress_html(CUT_PLAN_STALE_TITLE, CUT_SETTINGS_CHANGED_TEXT, tone='stopping'))


def _on_cut_analyze(source_path, sensitivity, min_seconds, max_seconds):
    """Analyze / Preview. Probes, detects scene changes, plans — and writes NOTHING.

    Returns `(summary_text, table_rows, plan_state, source_details)`. The plan travels in `gr.State` as a plain
    dict of primitives, never a live object: nothing with a thread, handle or lock may enter Gradio
    state, and a dict is also what makes the staleness signature comparable later.

    Detection failure is reported as failure. `ffmpeg_processing.detect_video_scene_changes`
    answers `[]` for both "no cuts" and "the detector did not run", which is fine for Stage 5 but
    would be a lie here: it would present one giant time-split clip as the footage's own structure.
    `raw_cutter_export.detect_scenes` keeps the two apart and this refuses on the failure.

    [FORK] Digital-Union (Raw Cutter progress P1): now a GENERATOR, so the named phase on screen
    is the phase actually executing. The three phases are the three real steps -- probe, detect,
    plan -- and each is announced *before* its blocking call, which is what makes the label true
    while the call runs rather than after it.

    The bar is indeterminate throughout. Detection is one FFmpeg pass that reports nothing
    usable, so any percentage here would be fabricated.

    Analyze has **no cancellable job**, by design and unchanged: it writes nothing, holds no
    mutex and installs no `CutterJob`. Cancel cutting is therefore left DISABLED for the whole of
    analysis -- presenting it as able to stop this would be a lie, and pressing it would
    truthfully answer "no cut job is running".
    """
    import raw_cutter_export as cutter_export

    busy = _cut_buttons(analyze=False, run=False, cancel=False)
    idle_no_plan = _cut_buttons(analyze=True, run=False, cancel=False)

    def fail(summary, details_text):
        return (summary, [], None, details_text,
                _cut_progress_html(CUT_ANALYZE_FAILED_TITLE, summary.lstrip('❌ ').split('\n')[0],
                                   tone='failed')) + idle_no_plan

    yield ('', [], gr.update(), gr.update(),
           _cut_progress_html(CUT_PHASE_ANALYZING_TITLE, CUT_PHASE_PROBE_TEXT,
                              note=CUT_PROGRESS_INDETERMINATE_NOTE)) + busy

    probe = cutter_export.probe_source(source_path)
    if not probe.ok:
        yield fail(f"❌ {probe.error}", "")
        return
    source = probe.source
    details = source.describe()

    try:
        low, high = fork_cutter.normalize_durations(min_seconds, max_seconds)
    except fork_cutter.RawCutterError as exc:
        yield fail(f"❌ {exc}", details)
        return

    level = fork_cutter.normalize_sensitivity(sensitivity)
    threshold = fork_cutter.sensitivity_to_threshold(level)

    yield ('', [], gr.update(), details,
           _cut_progress_html(CUT_PHASE_ANALYZING_TITLE, CUT_PHASE_DETECT_TEXT,
                              note=CUT_PROGRESS_INDETERMINATE_NOTE)) + busy

    scenes = cutter_export.detect_scenes(source.path, threshold)
    if not scenes.succeeded:
        yield fail((f"❌ Scene detection failed, so no plan was made.\n{scenes.error}\n\n"
                    f"Nothing was written and your source file was not touched."), details)
        return

    yield ('', [], gr.update(), details,
           _cut_progress_html(CUT_PHASE_ANALYZING_TITLE, CUT_PHASE_PLAN_TEXT,
                              note=CUT_PROGRESS_INDETERMINATE_NOTE)) + busy

    try:
        plan = fork_cutter.plan_segments(source.duration, scenes.times, low, high, level)
    except fork_cutter.RawCutterError as exc:
        yield fail(f"❌ {exc}", details)
        return

    summary = fork_cutter.plan_summary_text(plan, source)
    summary = (f"{summary}\n\nScene detection took {scenes.elapsed_seconds:g}s.")
    state = {
        'signature': fork_cutter.plan_signature(source, level, low, high),
        'source': {
            'path': source.path, 'duration': source.duration, 'width': source.width,
            'height': source.height, 'fps': source.fps, 'has_audio': source.has_audio,
            'size_bytes': source.size_bytes, 'mtime_ns': source.mtime_ns,
        },
        'segments': [[s.index, s.start, s.end, s.shot_index, s.boundary_kind]
                     for s in plan.segments],
        'shots': [[s.index, s.start, s.end, s.exported, s.skip_reason, s.piece_count]
                  for s in plan.shots],
        'sensitivity': level, 'min_seconds': low, 'max_seconds': high,
        'threshold': plan.threshold, 'detected': plan.detected_scene_count,
    }
    # [FORK] P1: Cut Video becomes available ONLY for a plan that actually exports something.
    # `plan.has_work` is the same predicate `_on_cut_run` refuses on, so the button and the
    # server-side guard cannot disagree -- the button is just the earlier, friendlier half.
    if plan.has_work:
        panel = _cut_progress_html(
            CUT_PHASE_ANALYZING_TITLE + ' — done',
            f'{plan.count} clip(s) planned. Press Cut Video to export them.',
            done=plan.count, total=plan.count, tone='done')
        buttons = _cut_buttons(analyze=True, run=True, cancel=False)
    else:
        panel = _cut_progress_html(CUT_PLAN_STALE_TITLE, CUT_ANALYZE_DONE_NO_WORK_TEXT,
                                   tone='stopping')
        buttons = _cut_buttons(analyze=True, run=False, cancel=False)
    yield (summary, fork_cutter.plan_table_rows(plan), state, details, panel) + buttons


def _restore_plan(state):
    """Rebuild the typed records from `gr.State`. Returns `(source, plan)` or `(None, None)`."""
    if not isinstance(state, dict) or not state.get('segments'):
        return None, None
    try:
        raw = state['source']
        source = fork_cutter.SourceInfo(
            path=raw['path'], duration=float(raw['duration']), width=int(raw['width']),
            height=int(raw['height']), fps=float(raw['fps']), has_audio=bool(raw['has_audio']),
            size_bytes=int(raw['size_bytes']), mtime_ns=int(raw['mtime_ns']))
        segments = tuple(
            fork_cutter.Segment(index=int(i), start=float(a), end=float(b),
                                shot_index=int(shot), boundary_kind=str(kind))
            for i, a, b, shot, kind in state['segments'])
        shots = tuple(
            fork_cutter.Shot(index=int(i), start=float(a), end=float(b), exported=bool(kept),
                             skip_reason=str(reason), piece_count=int(pieces))
            for i, a, b, kept, reason, pieces in state.get('shots', ()))
        plan = fork_cutter.SegmentPlan(
            segments=segments, shots=shots, source_duration=source.duration,
            sensitivity=int(state['sensitivity']), threshold=float(state['threshold']),
            min_seconds=float(state['min_seconds']), max_seconds=float(state['max_seconds']),
            detected_scene_count=int(state['detected']))
        return source, plan
    except (KeyError, TypeError, ValueError):
        return None, None


def _on_cut_run(plan_state, source_path, output_folder, sensitivity, min_seconds, max_seconds,
                remove_audio, rename_output, output_profile):
    """Cut Video. A generator, so clip X / total is visible while FFmpeg works.

    The staleness gate is the first real step and it is deliberately strict: the plan is refused
    unless the LIVE widgets and the LIVE file still produce the identical planning signature. A
    plan describes where this specific file's cuts fall, so a re-saved source or a changed
    sensitivity makes it fiction. `Remove Audio` is intentionally absent from the signature -- it
    changes how a segment is exported, never where it begins or ends, so it would refuse a plan
    that is still perfectly valid.

    [FORK] Digital-Union (Raw Cutter R4-C): `rename_output` joins it on exactly the same footing,
    and for the same reason -- it decides what a clip is CALLED. It is resolved into one frozen
    `OutputNaming` here, at submission, and that record is what the directory and every filename
    are built from. Toggling the checkbox in another browser session therefore cannot re-name the
    clips of a job already running: this frame read the value once and holds its own copy.
    """
    import raw_cutter_export as cutter_export

    busy = _cut_buttons(analyze=False, run=False, cancel=True)
    idle_plan = _cut_buttons(analyze=True, run=True, cancel=False)
    idle_no_plan = _cut_buttons(analyze=True, run=False, cancel=False)

    def refuse(text, *, keep_plan=True):
        """A refusal performs no work, so the controls go straight back to idle."""
        return (text, _cut_progress_html(CUT_CUT_REFUSED_TITLE, text.split('\n')[0],
                                         tone='failed')) + (idle_plan if keep_plan else idle_no_plan)

    source, plan = _restore_plan(plan_state)
    if source is None:
        yield refuse(CUT_NO_PLAN_TEXT, keep_plan=False)
        return
    if not plan.has_work:
        # [FORK] Digital-Union (Raw Cutter R1): a zero-clip plan is a legitimate ANALYSIS result --
        # the source genuinely has nothing long enough to export -- but it is not something to
        # execute. Refused clearly here rather than creating an empty run folder.
        yield refuse(CUT_NOTHING_TO_EXPORT_TEXT, keep_plan=False)
        return

    probe = cutter_export.probe_source(source_path)
    if not probe.ok:
        yield refuse(f"❌ {probe.error}")
        return
    try:
        low, high = fork_cutter.normalize_durations(min_seconds, max_seconds)
    except fork_cutter.RawCutterError as exc:
        yield refuse(f"❌ {exc}")
        return
    live_signature = fork_cutter.plan_signature(probe.source, sensitivity, low, high)
    if live_signature != plan_state.get('signature'):
        # The backend staleness gate, unchanged and still the ONLY authority. A disabled button
        # is a courtesy; this is the guarantee, and it refuses an API caller just the same.
        yield refuse(CUT_STALE_PLAN_TEXT, keep_plan=False)
        return

    if not _RENDER_LOCK.acquire(blocking=False):
        # Names the real holder, exactly as the three sibling guards do. A refused click performs
        # no work, creates no directory and leaves no state.
        yield refuse(_cut_busy_message())
        return
    _mark_lock_holder(_LOCK_HOLDER_CUTTER)
    job = cutter_export.CutterJob()
    _install_cutter_job(job)

    # [FORK] R4-C: ONE clock reading for the whole run, and both naming fields derive from it.
    # The run timestamp and the job date must describe the same instant, and nothing further down
    # may read the time again -- that is what keeps a job that crosses midnight consistent.
    started_at = datetime.datetime.now()
    timestamp = started_at.strftime('%Y%m%d_%H%M%S')
    naming = fork_cutter.OutputNaming(
        synthetic=bool(rename_output),
        # Single Video is always source 001: there is one source in this job's frozen list, which
        # is also why the job-wide source width is the three-digit minimum here. Stated rather
        # than defaulted, so both handlers visibly freeze the same two facts.
        source_index=1,
        job_date=started_at.strftime('%d%m%Y'),
        source_total=1)
    results = []
    # [FORK] Digital-Union (Raw Cutter R1): the completed list is accumulated through a callback,
    # NOT read from the return value. `export_segments` raises `CutterCancelled` on a stop, and an
    # exception discards its return -- so the earlier version reported "0 clip(s) were completed"
    # after a cancellation that had already written several files to disk. Declared out here so
    # the handler below can read it on every terminal path.
    completed: List[Any] = []
    run_directory = ''
    worker = None
    try:
        run_directory, error = cutter_export.prepare_run_directory(
            output_folder, source.path, timestamp, naming=naming)
        if error:
            yield refuse(f"❌ {error}")
            return

        encoder = 'h264_nvenc' if NVENC_AVAILABLE else 'libx264'
        header = (f"✂️ Cutting {plan.count} clip(s) into:\n{run_directory}\n\n"
                  f"Encoder: {encoder}. Audio: "
                  f"{'removed' if remove_audio else 'kept'}.")
        yield (header, _cut_progress_html(
            CUT_PHASE_CUTTING_TITLE, 'Starting…', done=0, total=plan.count,
            note=CUT_PROGRESS_ENCODING_NOTE)) + busy

        # [FORK] P1: `export_segments` is a BLOCKING call, so running it in this frame is exactly
        # why the screen used to freeze from the first clip to the last. It now runs on a worker
        # and reports through a queue, which is what lets this generator keep yielding WHILE
        # FFmpeg works instead of only after it returns.
        #
        # The two callbacks are the existing, documented ones -- `raw_cutter_export` is not
        # modified. `on_progress` announces the clip that is STARTING (so the detail line is true
        # during the encode); `on_complete` is what moves the bar, because it is the only signal
        # that a clip was really published. Both meanings are preserved exactly.
        events: "queue.Queue" = queue.Queue()
        outcome: Dict[str, Any] = {}

        def started(index, total, name):
            events.put(('start', index, total, name))

        def finished(result):
            completed.append(result)       # the R4 contract: the only reliable evidence on a stop
            events.put(('done', result))

        def run_export():
            try:
                outcome['results'] = cutter_export.export_segments(
                    source=source, plan=plan, run_directory=run_directory,
                    remove_audio=bool(remove_audio), encoder=encoder, job=job,
                    on_progress=started, on_complete=finished, naming=naming,
                    output_profile=output_profile)
            except BaseException as exc:   # noqa: BLE001 - re-raised on the generator's thread
                outcome['error'] = exc
            finally:
                events.put(('eof', None))

        worker = threading.Thread(target=run_export, name='raw-cutter-export', daemon=True)
        worker.start()

        published, current = 0, ''
        while True:
            try:
                event = events.get(timeout=0.4)
            except queue.Empty:
                # A heartbeat. The percentage deliberately does NOT move -- nothing finished --
                # but the panel keeps rendering, so a two-minute clip looks busy rather than hung.
                yield (header, _cut_progress_html(
                    CUT_PHASE_CUTTING_TITLE, current or 'Encoding…',
                    done=published, total=plan.count,
                    note=CUT_PROGRESS_ENCODING_NOTE)) + busy
                continue
            kind = event[0]
            if kind == 'eof':
                break
            if kind == 'start':
                current = f'Encoding clip {event[1]} of {event[2]}: {event[3]}'
            else:
                published = sum(1 for r in completed if r.ok)
                current = f'Published {os.path.basename(event[1].path)}' if event[1].ok \
                          else f'Clip failed: {event[1].error}'
            yield (header, _cut_progress_html(
                CUT_PHASE_CUTTING_TITLE, current, done=published, total=plan.count,
                note=CUT_PROGRESS_ENCODING_NOTE)) + busy

        worker.join()
        if 'error' in outcome:
            # Re-raised here so the existing, reviewed handlers below keep owning every terminal
            # path. Nothing about cancellation or failure classification changes.
            raise outcome['error']
        results = outcome.get('results', [])

        done = [r for r in results if r.ok]
        failed = [r for r in results if not r.ok]
        lines = [
            f"{'✅' if not failed else '⚠️'} Finished: {len(done)} of {plan.count} clip(s) written.",
            f"Output folder:\n{run_directory}",
            "",
            f"Encoder: {encoder}   Audio: {'removed (-an)' if remove_audio else 'kept (AAC)'}",
            f"Output profile: {fork_profiles.label_for(output_profile)}",
        ]
        if done:
            total_bytes = sum(r.size_bytes for r in done)
            lines.append(f"Total written: {fork_cutter.format_size(total_bytes)}")
        if failed:
            lines.append("")
            lines.append(f"{len(failed)} clip(s) failed:")
            lines.extend(f"  #{r.index}: {r.error}" for r in failed[:20])
            if len(failed) > 20:
                lines.append(f"  ... and {len(failed) - 20} more")
        lines.append("")
        lines.append("Your source file was not modified.")
        yield ("\n".join(lines), _cut_progress_html(
            CUT_CUT_DONE_TITLE,
            f'{len(done)} of {plan.count} clip(s) written'
            + (f'; {len(failed)} failed' if failed else '.'),
            done=len(done), total=plan.count,
            tone='failed' if failed else 'done')) + idle_plan

    except cutter_export.CutterCancelled:
        # `completed` is the truth here, never `results`: the exception propagated out of
        # `export_segments` before it could return, so its list never reached this frame.
        written = [r for r in completed if r.ok]
        failed_before_stop = [r for r in completed if not r.ok]
        lines = [f"⏹️ {CUT_CANCELLED_TEXT}", ""]
        lines.append(f"{len(written)} of {plan.count} clip(s) were completed and kept"
                     + (f" in:\n{run_directory}" if run_directory else "."))
        if written:
            lines.append("Total written: "
                         f"{fork_cutter.format_size(sum(r.size_bytes for r in written))}")
            lines.extend(f"  kept: {os.path.basename(r.path)}" for r in written[:20])
            if len(written) > 20:
                lines.append(f"  ... and {len(written) - 20} more")
        if failed_before_stop:
            lines.append(f"{len(failed_before_stop)} clip(s) had already failed before the stop.")
        lines.append("")
        lines.append("Your source file was not modified.")
        yield ("\n".join(lines), _cut_progress_html(
            CUT_CANCELLED_TEXT.split('.')[0], f'{len(written)} of {plan.count} clip(s) kept.',
            done=len(written), total=plan.count, tone='stopping')) + idle_plan
    except Exception as exc:
        # Total: a cut that raised must report rather than vanish behind Gradio's generic error.
        import traceback
        traceback.print_exc()
        text = (f"❌ Cutting failed.\n  {type(exc).__name__}: {exc}\n\n"
                f"Your source file was not modified.")
        yield (text, _cut_progress_html(CUT_CUT_FAILED_TITLE,
                                        f'{type(exc).__name__}: {exc}', tone='failed')) + idle_plan
    finally:
        # [FORK] P1: the export now lives on a worker, so admission must not be handed back while
        # that worker is still writing. If this frame unwinds early -- an abandoned Gradio stream
        # closing the generator under `GeneratorExit` -- the job is cancelled COOPERATIVELY and
        # reaped before the mutex is released. Releasing first would let a second cut start while
        # FFmpeg from the first was still running, which is precisely what `_RENDER_LOCK` exists
        # to prevent. On every ordinary path the worker has already been joined above and this
        # costs nothing.
        if worker is not None and worker.is_alive():
            job.cancel()
            worker.join()
        _clear_cutter_job()
        _mark_lock_holder('')
        _RENDER_LOCK.release()


def _on_cut_cancel() -> Tuple[str, str]:
    """Signal the running cut job. Writes the cutter's report line and the progress panel.

    Cooperative, exactly like the render Cancel: the flag is observed between segments, so the
    clip currently encoding finishes or is discarded cleanly rather than being torn up mid-write.
    Completed clips are always kept.

    [FORK] Digital-Union (Raw Cutter progress P1): also writes the progress panel, and is careful
    about the word it uses. Pressing Cancel reports **requested**, never "cancelled" -- the job
    stops at the next cooperative checkpoint, and the running generator is what announces the real
    terminal state once the work has actually stopped. Claiming completion here would be the exact
    lie this panel exists to avoid.
    """
    with _CUTTER_JOB_LOCK:
        job = _cutter_job
    if job is None:
        return CUT_NOTHING_RUNNING_TEXT, _cut_progress_clear()
    job.cancel()
    return CUT_CANCEL_REQUESTED_TEXT, _cut_progress_html(
        CUT_CANCEL_REQUESTED_TITLE, CUT_CANCEL_REQUESTED_TEXT, tone='stopping')


# ============================================================================================
# [FORK] Digital-Union (Raw Cutter Batch V1): the whole folder, one press.
#
# The product decision this implements: process MANY sources automatically with minimal operator
# interaction. "Detect what we can, cut what we can, skip short unusable shots, and keep going."
# There is deliberately no per-file preview, no confirmation step and no transition detection --
# a missed cross-dissolve is an accepted V1 limitation, recorded in ROADMAP.md R2 and in the R2-A
# research, not something this loop tries to repair.
#
# It adds no second planner and no second detector. `probe_source`, `detect_scenes`,
# `plan_segments` and `export_segments` are the V1 functions, called unchanged once per source.
# What is new is bookkeeping: the frozen input list, the collision-safe output layout, the
# per-source outcome classification and the manifest -- all of it in
# `beatsync_fork.raw_cutter_batch`, which is pure.
#
# ONE mutex acquisition for the WHOLE batch, and one `CutterJob` for the whole batch. Taking
# `_RENDER_LOCK` per source would deadlock on the second file: it is a plain non-reentrant `Lock`
# on purpose, and the batch still holds it.
# ============================================================================================

def _on_cut_mode_change(mode):
    """Show the controls for the chosen mode. Writes nothing but four visibilities.

    No execution setting moves and no state is cleared, so switching modes cannot change what
    either workflow would do. The sensitivity, minimum, maximum and Remove Audio controls are
    SHARED between the modes and are deliberately outside both groups -- one value per execution
    setting on the screen, which is also why this handler cannot touch them.
    """
    batch = (mode == CUT_MODE_BATCH)
    return (
        gr.update(visible=not batch),   # cut_single_inputs
        gr.update(visible=batch),       # cut_batch_inputs
        gr.update(visible=not batch),   # cut_single_actions
        gr.update(visible=batch),       # cut_batch_actions
        gr.update(visible=not batch),   # cut_single_readouts
        gr.update(visible=batch),       # cut_batch_readouts
    )


def _on_cut_batch_count(input_folder, recursive) -> str:
    """The optional read-only file count. Reads the file list and nothing else.

    No mutex: this is a metadata-only directory walk with `detect_duplicates=False`, so it opens no
    media, starts no FFmpeg child and competes with nothing for the drive. Cheaper than Verify
    Sources, which does probe, and that is why it does not need the admission control Verify does.
    """
    import raw_cutter_export as cutter_export

    found = cutter_export.discover_batch_sources(input_folder, recursive)
    if not found.ok:
        return f"❌ {found.error}"
    return fork_cutter_batch.discovery_text(len(found.paths), found.root, bool(recursive),
                                     found.rejected_count)


def _on_cut_batch_run(input_folder, recursive, output_folder, sensitivity, min_seconds,
                      max_seconds, remove_audio, rename_output, output_profile,
                      sink=None):
    """Start Batch. A generator, so the current file and phase stay visible for a long job.

    Yields `(live_status, final_summary)`. The summary stays empty until the end, so the operator
    is never shown a half-built report as if it were the result.

    Validation order is deliberate and all of it happens BEFORE the mutex is taken, so a refused
    batch creates no directory and holds nothing: the length bounds once for the whole run, then
    the two roots resolved and checked for nesting, then discovery. Only then is admission
    attempted.

    [FORK] Digital-Union (Raw Cutter progress P1): `sink` is an OPTIONAL output-only dict for the
    presentation wrapper. It defaults to a throwaway, so every existing caller -- including the
    test harness that exec's this body against a synthesised parent package -- keeps working with
    the original seven arguments and no new module-level name to resolve. Nothing in the batch's
    behaviour reads it; it is written, never consulted.

    [FORK] Digital-Union (Raw Cutter R4-C): `rename_output` is the shared checkbox, read once and
    resolved into one frozen `OutputNaming` for the WHOLE batch -- including a single `job_date`
    taken from the same `started_at` the output folder is named after. A batch that runs past
    midnight therefore keeps the date it started with, and each source's naming is derived from
    that one record with `for_source(position)` rather than from a counter this loop mutates. The
    frozen input list is what a source index means, so a source that is skipped, unreadable or
    fails never renumbers the ones after it.
    """
    import raw_cutter_export as cutter_export

    sink = {} if sink is None else sink

    try:
        low, high = fork_cutter.normalize_durations(min_seconds, max_seconds)
    except fork_cutter.RawCutterError as exc:
        yield f"❌ {exc}", ''
        return
    level = fork_cutter.normalize_sensitivity(sensitivity)
    threshold = fork_cutter.sensitivity_to_threshold(level)

    source_root, output_root, error = cutter_export.resolve_batch_roots(
        input_folder, output_folder)
    if error:
        yield f"❌ {error}", ''
        return

    found = cutter_export.discover_batch_sources(source_root, recursive)
    if not found.ok:
        yield f"❌ {found.error}", ''
        return
    if not found.paths:
        yield fork_cutter_batch.discovery_text(0, found.root, bool(recursive)), ''
        return

    # The input list is frozen here, before anything is written: the manifest, the output layout
    # and the progress counter all refer to this exact ordered tuple.
    sources = found.paths
    total = len(sources)

    if not _RENDER_LOCK.acquire(blocking=False):
        yield _cut_busy_message(), ''
        return
    _mark_lock_holder(_LOCK_HOLDER_CUTTER)
    job = cutter_export.CutterJob()
    _install_cutter_job(job)

    clock_started = time.perf_counter()
    started_at = datetime.datetime.now()
    # [FORK] Digital-Union (Raw Cutter R4-C): ONE naming record for the whole batch, resolved
    # here and never recomputed. `job_date` comes from the SAME `started_at` the batch folder is
    # named after, so the two cannot describe different days, and no clip export reads a clock of
    # its own -- which is what keeps a run that crosses midnight on one scheme.
    #
    # `source_total` is the frozen count of the list above, and it is the only place the source
    # field's width is decided: the layout below and every filename the exporter builds both read
    # it back out of this record, so a 1200-source batch is `File0001`..`File1200` throughout
    # instead of three digits up to 999 and four after it. It is taken from `total` rather than
    # recounted, so it cannot describe a different list than the manifest and the progress counter.
    naming = fork_cutter.OutputNaming(synthetic=bool(rename_output),
                                      job_date=started_at.strftime('%d%m%Y'),
                                      source_total=total)
    # The output layout is decided from the frozen list and the naming mode together, still
    # before anything is written: mirrored input structure by default, one flat `File<SSS>`
    # folder per source when renaming is on.
    layout = fork_cutter_batch.plan_item_directories(sources, found.root, naming=naming)
    encoder = 'h264_nvenc' if NVENC_AVAILABLE else 'libx264'
    settings = fork_cutter_batch.BatchSettings(
        sensitivity=level, threshold=threshold, min_seconds=low, max_seconds=high,
        remove_audio=bool(remove_audio), encoder=encoder, recursive=bool(recursive),
        rename_output_clips=bool(rename_output),
        # [FORK] R6: FROZEN here, once, at submission. Every source and every segment in this
        # batch uses this value; changing the widget mid-run cannot reach the running job.
        output_video_profile=fork_profiles.manifest_value(output_profile))

    items: List[Any] = []
    batch_directory = ''
    cancelled = False
    finalised = False

    def publish(**extra):
        """[FORK] Digital-Union (P19-R4, finding F-1): write the sink from RECORDED outcomes.

        The count is `summarise(items, total).attempted` -- the same fold the manifest's own
        totals use. Two consequences, both deliberate: the bar and the record cannot disagree
        about how much of the batch is done, and the `not_attempted` backfill is excluded by
        construction rather than by a second rule that could drift away from the first.

        Called wherever `items` changes AND at finalisation. The previous version wrote the count
        only from `progress(phase)`, which runs BEFORE each source's `record(...)`, so the final
        source was missing from every terminal panel and a fully successful batch reported
        `n - 1` of `n` -- measured at 50 %, 67 % and 80 % for two, three and five sources.
        """
        totals = fork_cutter_batch.summarise(items, total)
        sink.update(finished=totals.attempted, completed=totals.completed, **extra)

    def finalise(is_cancelled, fatal=''):
        """[FORK] R2: the ONE terminal finalisation. Backfill, then write the manifest.

        Every terminal path calls exactly this -- normal end, cancellation, and the unexpected
        error -- which is what fixes R1 finding F-1. Before R2 the `not_attempted` backfill sat
        at the end of the `try`, so an exception jumped straight over it and the manifest was
        written short (or, when nothing had been recorded yet, not written at all because of an
        `and items` guard). Sources then vanished from a record whose own contract is one entry
        per DISCOVERED source.

        Idempotent by construction: the backfill range is empty once every source has a record,
        so a second call adds nothing and can never duplicate one. Returns the manifest error
        string ('' means saved) so the caller can REPORT the write instead of predicting it.

        It never raises. A failure here must not be able to skip the caller's `finally` -- which
        is where the cutter job is cleared and `_RENDER_LOCK` is released -- because that would
        strand the one process-global mutex and lock the operator out of every render, batch and
        verification until the app was restarted.
        """
        nonlocal finalised
        finalised = True
        # [FORK] P1: record the terminal CLASS structurally, at the one place that already knows
        # it. The presentation wrapper reads this instead of inspecting the status text -- the
        # project forbids deriving an outcome class from a status or error string, and rightly:
        # prose is not a contract, and a reworded message would silently change the UI's verdict.
        #
        # [FORK] Digital-Union (P19-R4, finding F-2): the class is now the PURE classifier the
        # manifest itself uses, so the panel and the record can never disagree, and
        # `completed_with_failures` -- a class the classifier has always produced -- stops being
        # flattened into the same green success a clean run gets.
        #
        # A run that never obtained an output directory never started, and is published with NO
        # terminal class at all. That is the structural way to say "there was no terminal state",
        # and it lands on the same honest "Batch did not run" panel the pre-admission refusal
        # already uses -- one presentation for both refusal shapes, and no third vocabulary to
        # keep in step. The old expression could only answer done/cancelled/failed, so the one
        # early `return` inside the `try` below -- a refused `prepare_batch_directory` -- reached
        # this net with neither a cancellation nor a fatal error and was published as `done`: a
        # green "Batch finished" over a run that created nothing.
        #
        # Published BEFORE the backfill, and provably equivalent to publishing after it:
        # `not_attempted` is in neither `_FAILED` nor `attempted`, so appending those rows moves
        # neither `terminal_status` nor the count. Doing it first means no fault inside the `try`
        # can leave the operator looking at a stale panel.
        started = bool(batch_directory)
        if started:
            sink['terminal'] = fork_cutter_batch.terminal_status(
                items, total, is_cancelled, fatal_error=fatal)
            publish(total=total)
        else:
            sink.pop('terminal', None)
            publish()
        try:
            # Only sources never opened at all become `not_attempted`; anything the loop touched
            # has already recorded itself, including on the fatal path.
            for index in range(len(items) + 1, total + 1):
                items.append(fork_cutter_batch.BatchItem(
                    index=index, source_path=sources[index - 1],
                    relative_parts=layout[index - 1],
                    outcome=fork_cutter_batch.OUTCOME_NOT_ATTEMPTED))
            if not batch_directory:
                return ''
            # [FORK] P19-R4: the write's RESULT is recorded in the sink as well as returned, so
            # the panel can say that the authoritative record did not reach disk. The summary has
            # always disclosed it; the panel used to show a run whose manifest was lost with the
            # same green headline as one whose manifest was saved.
            sink['manifest_error'] = cutter_export.write_batch_manifest(
                batch_directory,
                fork_cutter_batch.manifest_payload(
                    items, total, settings, batch_directory, found.root,
                    started_at.isoformat(timespec='seconds'),
                    datetime.datetime.now().isoformat(timespec='seconds'),
                    time.perf_counter() - clock_started, is_cancelled, fatal_error=fatal))
            return sink['manifest_error']
        except Exception as exc:                      # noqa: BLE001 - see the docstring
            import traceback
            traceback.print_exc()
            sink['manifest_error'] = (
                f"the batch record could not be built: {type(exc).__name__}: {exc}")
            return sink['manifest_error']

    try:
        batch_directory, error = cutter_export.prepare_batch_directory(
            output_root, started_at.strftime('%Y%m%d_%H%M%S'))
        if error:
            yield f"❌ {error}", ''
            return

        yield (f"🚀 Batch starting: {total} video(s) from\n{found.root}\n\n"
               f"Output: {batch_directory}\n"
               f"Settings: {settings.describe()}"), ''

        for position, source_path in enumerate(sources, start=1):
            if job.cancelled:
                cancelled = True
                break
            parts = layout[position - 1]
            basename = os.path.basename(source_path)
            file_clock = time.perf_counter()
            recorded = False

            def progress(phase):
                # [FORK] Digital-Union (Raw Cutter progress P1): the text is unchanged; the sink
                # is a side-channel of the SAME facts this line already had in scope, so the
                # visual bar and the status text can never disagree about where the batch is.
                #
                # [FORK] P19-R4 (F-1): this line owns the position, the name and the phase, and
                # carries only those. The COUNT comes from `publish`, which folds the recorded
                # outcomes -- deriving it here was the off-by-one, because this callback runs
                # before the current source has been recorded.
                publish(position=position, total=total, name=basename, phase=phase)
                return fork_cutter_batch.progress_text(position, total, basename, phase, items,
                                                batch_directory)

            def record(outcome, **extra):
                nonlocal recorded
                items.append(fork_cutter_batch.BatchItem(
                    index=position, source_path=source_path, relative_parts=parts,
                    outcome=outcome, seconds=round(time.perf_counter() - file_clock, 3), **extra))
                recorded = True
                # [FORK] P19-R4 (F-1): the count moves when an outcome is RECORDED, because that
                # is the moment a source is genuinely finished -- including the last one, which
                # has no further `progress(...)` call to carry it.
                publish()

            # [FORK] R2 (F-3): a source the loop has OPENED always leaves a record, even when the
            # fault is one nothing here anticipated. Without this the record was simply never
            # appended, the backfill then labelled the file `not_attempted`, and clips already on
            # disk were reported as never produced. `CutterCancelled` is deliberately NOT caught
            # here -- it is an ordinary `Exception`, it is already handled at the two safe
            # boundaries below, and swallowing it would turn a Stop into a crash.
            try:
                yield progress(fork_cutter_batch.PHASE_PROBE), ''
                probe = cutter_export.probe_source(source_path)
                if not probe.ok:
                    record(fork_cutter_batch.OUTCOME_UNREADABLE, error=probe.error)
                    continue
                source = probe.source

                yield progress(fork_cutter_batch.PHASE_DETECT), ''
                try:
                    scenes = cutter_export.detect_scenes(source.path, threshold, job=job)
                except cutter_export.CutterCancelled:
                    cancelled = True
                    record(fork_cutter_batch.OUTCOME_CANCELLED)
                    break
                if not scenes.succeeded:
                    # A failed detector is NEVER recorded as a successful zero-cut analysis. That
                    # distinction is the reason `SceneResult` exists, and in a batch it matters
                    # more: one giant time-split clip per unreadable file would look like a
                    # working run.
                    record(fork_cutter_batch.OUTCOME_DETECTION_FAILED, error=scenes.error)
                    continue

                yield progress(fork_cutter_batch.PHASE_PLAN), ''
                try:
                    plan = fork_cutter.plan_segments(source.duration, scenes.times, low, high,
                                                     level)
                except fork_cutter.RawCutterError as exc:
                    record(fork_cutter_batch.OUTCOME_PLAN_FAILED, error=str(exc))
                    continue
                if not plan.has_work:
                    # A truthful answer about this footage, not a failure: nothing in it is long
                    # enough at these settings. No folder is created for it.
                    record(fork_cutter_batch.OUTCOME_NO_CLIPS)
                    continue

                item_directory, error = cutter_export.prepare_item_directory(batch_directory,
                                                                             parts)
                if error:
                    record(fork_cutter_batch.OUTCOME_EXPORT_FAILED, clips_planned=plan.count,
                           error=error)
                    continue

                yield progress(fork_cutter_batch.PHASE_EXPORT), ''
                lengths = {segment.index: segment.duration for segment in plan.segments}
                written: List[Any] = []
                stopped = False
                fatal_export = None
                try:
                    cutter_export.export_segments(
                        source=source, plan=plan, run_directory=item_directory,
                        remove_audio=bool(remove_audio), encoder=encoder, job=job,
                        on_complete=written.append,
                        # [FORK] R6: read from the FROZEN settings, never from the widget.
                        output_profile=settings.output_video_profile,
                        # [FORK] R4-C: the job's naming aimed at THIS source's frozen position.
                        # `for_source` returns a new frozen record, so the batch-level one is
                        # never mutated and the index cannot drift between sources.
                        naming=naming.for_source(position))
                except cutter_export.CutterCancelled:
                    # `written` is the truth on this path: the exception discards
                    # `export_segments`' return value, so the callback's list is the only record
                    # of what reached disk.
                    stopped = True
                    cancelled = True
                except Exception as exc:                  # noqa: BLE001 - re-raised below
                    # [FORK] R2 (F-3): `written` is the truth here for exactly the same reason,
                    # and an unexpected fault is precisely when it matters most. The clips it
                    # names are already validated and already on disk; losing them from the
                    # accounting was the worst of the R1 findings. Classified from that evidence
                    # below, recorded, and only THEN re-raised, so the batch still stops and the
                    # error still reaches the operator.
                    fatal_export = exc

                good = [result for result in written if result.ok]
                if stopped:
                    outcome = fork_cutter_batch.OUTCOME_CANCELLED
                elif len(good) == plan.count:
                    # Every planned clip fired `on_complete` validated, so the source really did
                    # finish -- even if the adapter then raised on its way out.
                    outcome = fork_cutter_batch.OUTCOME_COMPLETED
                elif good:
                    outcome = fork_cutter_batch.OUTCOME_PARTIAL
                elif fatal_export is not None:
                    outcome = fork_cutter_batch.OUTCOME_UNEXPECTED_ERROR
                else:
                    outcome = fork_cutter_batch.OUTCOME_EXPORT_FAILED
                failures = [result for result in written if not result.ok]
                detail = ("; ".join(f"#{r.index}: {r.error}" for r in failures[:5])
                          if failures else '')
                if fatal_export is not None:
                    detail = (f"{type(fatal_export).__name__}: {fatal_export}"
                              + (f"; {detail}" if detail else ''))
                record(outcome,
                       clips_planned=plan.count,
                       clips_written=len(good),
                       exported_seconds=round(sum(lengths.get(r.index, 0.0) for r in good), 3),
                       bytes_written=sum(result.size_bytes for result in good),
                       output_directory=item_directory,
                       error=detail)
                if fatal_export is not None:
                    raise fatal_export
                if stopped:
                    break
            except cutter_export.CutterCancelled:
                raise
            except Exception as exc:                      # noqa: BLE001 - re-raised immediately
                # A fault anywhere else in this source's processing (probe, detection, planning,
                # directory preparation). No clip evidence exists, so the honest record is that
                # the tool broke on a file it HAD opened -- never `not_attempted`.
                if not recorded:
                    record(fork_cutter_batch.OUTCOME_UNEXPECTED_ERROR,
                           error=f"{type(exc).__name__}: {exc}")
                raise

        # [FORK] R2 (F-1/F-4): finalise BEFORE the terminal yield, so the report states what the
        # manifest write actually did instead of predicting it, and so the operator never sees a
        # result that is still waiting on persistence.
        manifest_error = finalise(cancelled)
        elapsed = time.perf_counter() - clock_started
        summary = fork_cutter_batch.summary_text(
            items, total, settings, batch_directory, elapsed, cancelled, input_root=found.root,
            manifest_error=manifest_error)
        yield (CUT_BATCH_CANCELLED_STATUS if cancelled else CUT_BATCH_DONE_STATUS), summary

    except cutter_export.CutterCancelled:
        # Named BEFORE the generic handler, for the same reason `_on_cut_run` does it:
        # `CutterCancelled` is an ordinary `Exception`, so a generic clause listed first would
        # report the operator's own Stop as a crash. Cancellation is normally handled at the two
        # safe boundaries inside the loop; this clause covers a stop observed anywhere else and
        # still produces the truthful report rather than an error.
        cancelled = True
        manifest_error = finalise(True)
        elapsed = time.perf_counter() - clock_started
        yield CUT_BATCH_CANCELLED_STATUS, fork_cutter_batch.summary_text(
            items, total, settings, batch_directory, elapsed, True, input_root=found.root,
            manifest_error=manifest_error)
    except Exception as exc:
        # Total: a batch that raised must report rather than vanish behind Gradio's generic error,
        # and it must still say what it had already written.
        #
        # [FORK] R2 (F-2): `cancelled` is passed THROUGH, never forced. This clause used to hand
        # `summary_text` a literal `True`, so every crash was headlined "Batch cancelled" -- an
        # operator was told they had stopped a run they had not touched, while the manifest
        # beside it recorded `cancelled: false`. The failure now travels as `fatal_error`, which
        # outranks the cancellation flag in both the report and the record, so the two agree.
        import traceback
        traceback.print_exc()
        fatal = f"{type(exc).__name__}: {exc}"
        manifest_error = finalise(cancelled, fatal=fatal)
        elapsed = time.perf_counter() - clock_started
        partial = fork_cutter_batch.summary_text(
            items, total, settings, batch_directory, elapsed, cancelled, input_root=found.root,
            fatal_error=fatal, manifest_error=manifest_error)
        yield (f"❌ The batch stopped on an unexpected error.\n  {fatal}"), partial
    finally:
        # [FORK] R2: the terminal paths above already finalised, each one before its own yield.
        # This is the net for the path that has no terminal yield at all -- an abandoned Gradio
        # stream, where the frame unwinds under `GeneratorExit` and nothing may be yielded. The
        # manifest is still worth writing there; the report simply has nobody to go to.
        #
        # Nothing in this block may raise. `finalise` is total by construction, and the three
        # cleanup calls below must run even if it somehow were not: stranding `_RENDER_LOCK`
        # would lock the operator out of every render, batch and verification in the process
        # until the app was restarted.
        # The net is wrapped, and the three cleanup calls deliberately stay TOP-LEVEL statements
        # of this `finally` -- the shape every other mutex-owning handler in this file uses, and
        # the one `test_the_batch_clears_its_holder_token_and_job_before_releasing` and
        # `test_only_the_diagnostics_mark_the_holder_and_each_clears_before_release` both read.
        # Nesting them inside a second `try` protects nothing extra here (`finalise` is already
        # total) and breaks that documented ordering contract in two suites at once.
        try:
            if not finalised:
                leftover = finalise(cancelled)
                if leftover:
                    print(f"[raw-cutter-batch] {leftover}")
        except Exception:                             # noqa: BLE001 - cleanup must still run
            pass
        _clear_cutter_job()
        _mark_lock_holder('')
        _RENDER_LOCK.release()


def _on_cut_batch_run_ui(input_folder, recursive, output_folder, sensitivity, min_seconds,
                         max_seconds, remove_audio, rename_output, output_profile):
    """[FORK] Digital-Union (Raw Cutter progress P1): the batch, with a visible bar and controls.

    A thin presentation wrapper, on purpose. `_on_cut_batch_run` above keeps every reviewed
    terminal path -- the R2 single finalisation, the manifest, the outcome classification, the
    cancellation and abandoned-stream handling -- with its original name, signature and body, so
    the assertions that guard those invariants keep guarding exactly the same code. This function
    adds nothing but the progress panel and the three button states.

    The bar counts **sources whose outcome has been recorded** (`items`), never sources started,
    so it cannot run ahead of what the manifest will say.
    """
    sink: Dict[str, Any] = {}
    busy = _cut_batch_buttons(count=False, start=False, cancel=True)
    idle = _cut_batch_buttons(count=True, start=True, cancel=False)

    status, summary = '', ''
    for status, summary in _on_cut_batch_run(
            input_folder, recursive, output_folder, sensitivity, min_seconds,
            max_seconds, remove_audio, rename_output, output_profile, sink=sink):
        total = sink.get('total')
        detail = (f"Source {sink.get('position')} of {total}: "
                  f"{sink.get('name', '')} — {sink.get('phase', '')}") if total else ''
        yield (status, summary,
               _cut_progress_html(CUT_BATCH_RUNNING_TITLE, detail,
                                  done=sink.get('finished'), total=total,
                                  note=CUT_PROGRESS_ENCODING_NOTE)) + busy

    # One final frame: the controls come back and the panel states the terminal outcome.
    #
    # The class comes from `sink['terminal']`, which the batch's single finalisation helper set
    # structurally. It is NOT inferred from the status text: this project forbids deriving an
    # outcome class from a status or error string.
    #
    # [FORK] Digital-Union (P19-R4): the four values are now the batch module's own
    # `TERMINAL_*` vocabulary, so this table is a rendering of the pure classifier rather than a
    # parallel one that can drift from it. `completed_with_failures` gets its own qualified
    # headline and a non-green tone -- the single-video cut already uses the `failed` tone when
    # some clips failed, and a batch with a failed source is the same statement about the run.
    #
    # The neutral default covers BOTH refusal shapes: a batch refused before admission never
    # reaches finalisation at all, and one refused before it got an output directory is
    # deliberately published with no terminal class. Neither ran, so neither gets a verdict.
    tone, title = {
        fork_cutter_batch.TERMINAL_FAILED: ('failed', CUT_BATCH_FAILED_TITLE),
        fork_cutter_batch.TERMINAL_CANCELLED: ('stopping', CUT_BATCH_CANCELLED_TITLE),
        fork_cutter_batch.TERMINAL_COMPLETED_WITH_FAILURES: ('failed', CUT_BATCH_PARTIAL_TITLE),
        fork_cutter_batch.TERMINAL_COMPLETED: ('done', CUT_BATCH_DONE_TITLE),
    }.get(sink.get('terminal'), ('stopping', CUT_BATCH_NOT_RUN_TITLE))
    # A run whose every source succeeded but whose authoritative record never reached disk is not
    # a clean success. The summary has always said so; the panel now agrees with it.
    if sink.get('manifest_error') and tone == 'done':
        tone, title = 'failed', CUT_BATCH_RECORD_LOST_TITLE
    yield (status, summary,
           _cut_progress_html(title, '', done=sink.get('finished'),
                              total=sink.get('total'), tone=tone)) + idle


def _on_cut_batch_cancel() -> Tuple[str, str]:
    """Signal the running batch. Writes only the batch status line.

    The same cooperative flag and the same one-slot job reference the single-video Cancel uses --
    only one cutter job can exist at a time, because admission is the one shared mutex. Observed at
    two safe boundaries: between source videos, and between clips inside the current source. No
    child is killed, nothing already written is removed, and no output folder is deleted.

    [FORK] Digital-Union (Raw Cutter progress P1): also writes the progress panel, and like the
    single-video Cancel it reports **requested**, never "cancelled". The running batch generator
    announces the real terminal state once the work has actually stopped.
    """
    with _CUTTER_JOB_LOCK:
        job = _cutter_job
    if job is None:
        return CUT_BATCH_NOTHING_RUNNING_TEXT, _cut_progress_clear()
    job.cancel()
    return CUT_BATCH_CANCEL_REQUESTED_TEXT, _cut_progress_html(
        CUT_CANCEL_REQUESTED_TITLE, CUT_BATCH_CANCEL_REQUESTED_TEXT, tone='stopping')


def _on_sample_invalidate(request: gr.Request = None) -> str:
    """Clear the sample report when the video file or the seek position changes.

    A separate listener rather than an entry in `prep_outputs` or the S2-A outputs: this writes
    ONLY the sample textbox, so neither the preparation state machine nor the verification report
    can be moved by it.

    It bumps this SESSION's SAMPLE revision, which is what stops a sample already in flight from
    publishing its older answer over this notice. Scoped on purpose, twice over: session-scoped so
    an edit here cannot invalidate a sample running in a different browser session, and
    sample-scoped so it cannot invalidate a source verification, which answers a different
    question about a different selection.

    It performs NO FFprobe, no decode, no stat and no scan -- a change listener must never start
    disk work, which is why metadata is only ever read inside the click handler.
    """
    _bump_verify_revision(_scoped_key(_verify_session_key(request), _SCOPE_SAMPLE))
    return PREP_SAMPLE_STALE_TEXT


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


#: [FORK] R8: where a previous session's browser uploads are kept instead of being deleted.
#: Protected from the startup sweep by name, and clearable only through Settings -> Cache
#: Management, where the operator can see what is in it first.
RETIRED_UPLOADS_DIR_NAME = 'retired_uploads'
RETIRED_UPLOADS_DIR = os.path.join(ROOT_DIR, 'input', RETIRED_UPLOADS_DIR_NAME)


def _retire_previous_uploads(input_base: str) -> None:
    """Move the last session's `gradio_uploads` aside instead of deleting it.

    [FORK] R8 UX Refresh. This used to be a `shutil.rmtree`, and it ran before the operator had
    done anything -- so a file dragged into the browser in a previous session was gone at the next
    start, with no record that it had existed. A browser upload is a COPY the operator made into
    the app, and for some of them it is the only copy they still have a path to.

    The replacement is deliberately the simplest thing that is reversible: one `os.rename` into a
    timestamped folder under `input/retired_uploads/`. No index, no manifest, no restore command,
    no second copy -- the files are simply still there, under a name that says what they are. That
    is "reversible" in the sense that matters (the bytes survive and the operator can go and get
    them) without becoming the backup system this must not grow into.

    Nothing here deletes. If the rename fails for any reason the old folder is left exactly where
    it is, which is the safe direction: a session that reuses it writes alongside, and the next
    start tries again.

    The folder is bounded only by the operator's own clearing, which is a real trade: it grows one
    subdirectory per start that had uploads. That is why it is a first-class Cache Management
    category with its size on screen, rather than something that silently accumulates.
    """
    uploads = os.path.join(input_base, 'gradio_uploads')
    try:
        if not os.path.isdir(uploads) or not os.listdir(uploads):
            return                                   # nothing uploaded last time: nothing to keep
    except OSError:
        return
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    destination = os.path.join(RETIRED_UPLOADS_DIR, stamp)
    try:
        os.makedirs(RETIRED_UPLOADS_DIR, exist_ok=True)
        # No-replace: a second start inside the same second must not merge into, or overwrite,
        # the first one's retirement.
        suffix = 0
        candidate = destination
        while os.path.exists(candidate):
            suffix += 1
            candidate = f"{destination}_{suffix:02d}"
        os.rename(uploads, candidate)
        print(f"   📦 Kept last session's uploads: {candidate}")
    except OSError as exc:
        print(f"   ⚠️  Could not set aside the previous uploads, so they were left alone: {exc}")


def cleanup_on_startup():
    """
    Clean temporary runtime files on script start while preserving user inputs
    and the persistent video analysis cache.

    [FORK] R8 UX Refresh: `gradio_uploads` is now RETIRED rather than deleted (see
    :func:`_retire_previous_uploads`), and `retired_uploads` joins the protected set so the sweep
    does not simply delete it on the following start -- which would have reproduced the original
    behaviour one run later. Everything else about this function is unchanged: `processing/` is
    genuinely disposable scratch from a finished render and is still removed, and `audio`,
    `video` and the Stage-5 analysis cache are still protected.
    """
    input_base = get_input_dir()
    protected_dirs = {'audio', 'video', 'video_analysis_cache', RETIRED_UPLOADS_DIR_NAME}

    try:
        os.makedirs(get_audio_input_dir(), exist_ok=True)
        os.makedirs(get_video_input_dir(), exist_ok=True)

        # Before the sweep, because the sweep is what used to destroy them.
        _retire_previous_uploads(input_base)

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


# =============================================================================================
# [FORK] Digital-Union (R5 Clips Preview Selector, P1): the fifth tab's server side.
#
# READ-ONLY BY CONSTRUCTION. Nothing below moves, renames, deletes or re-encodes a reviewed
# clip; the only write anywhere in this block is the decision journal and the poster cache, both
# under `state/clips-review/`. Transfer is a separate, separately authorized phase, and it is
# deliberately absent rather than present-and-disabled -- there is no code path here that could
# be switched on by a flag.
#
# Everything with a rule in it lives in `beatsync_fork.clips_review`: opaque ids, the six
# authorization gates, descriptor-identity revalidation, Range parsing, path-relationship
# refusal, the journal, and the gallery markup. This layer is Gradio wiring, so the security
# model stays testable on a bare interpreter.
# =============================================================================================

CLIPS_STATE_DIR = os.path.join(ROOT_DIR, 'state', 'clips-review')
CLIPS_POSTER_DIR = os.path.join(CLIPS_STATE_DIR, 'posters')

#: One registry and one store per process. The registry is scoped per owner internally, so two
#: browser sessions cannot see or revoke each other's pages.
_CLIPS_REGISTRY = fork_clips.ClipRegistry()
_CLIPS_STORE_LOCK = threading.Lock()
_CLIPS_STORE = None

# [FORK] Digital-Union (R7.2): the two audio review registries.
#
# **Separate instances, and separate from `_CLIPS_REGISTRY`, for one measured structural reason:**
# `ClipRegistry.new_session` revokes every previous session that owner holds. That is correct
# within one review surface -- an operator rescanning has finished with the old page -- and wrong
# across two, because scanning a folder in Audio Preview would then revoke the Raw Audio Cutter's
# just-cut gallery and leave every tile in it answering 403 with nothing on screen to explain it.
#
# Each registry is paired with its own URL prefix at middleware construction time, so a token
# minted for one surface is simply not in the other's map. They hold no decisions and no operator
# state: a registry maps opaque ids to file identities and nothing else, and every decision lives
# in the owning tab's own `gr.State`.
_AUDIO_CUT_REGISTRY = fork_clips.ClipRegistry()
_AUDIO_PREVIEW_REGISTRY = fork_clips.ClipRegistry()

#: [R5-P3 / F1] Per-owner scan generations AND the authoritative per-owner view, in one object so
#: that "is my scan still the newest?" and "change the authoritative state" cannot drift apart.
#: Replaces the bare `_CLIPS_PAGES` dict, whose unconditional writes let a slow older scan
#: republish its own revoked session after a newer scan had already superseded it.
CLIPS_MAX_TRACKED_SESSIONS = 32
_CLIPS_SCANS = fork_clips.ScanCoordinator(max_owners=CLIPS_MAX_TRACKED_SESSIONS)


def _clips_store():
    """The decision store, created on first use so importing `gui` writes nothing.

    [R5-P11 / N7] `ReviewStore` now raises `PersistenceError` rather than returning a store that
    recovered only part of the durable record, and **that failure must not be cached**. The global
    is therefore bound from a local that only exists once the constructor has returned: if the
    snapshot or journal was transiently unreadable -- a backup or indexer holding an exclusive
    share is enough -- `_CLIPS_STORE` stays `None` and the next call retries the recovery from
    scratch. Caching a refusal, or a half-loaded store, would turn a passing lock into a
    process-lifetime outage; caching a half-loaded one would also re-arm the loss path, because
    every later caller would be writing to a record the disk does not hold.
    """
    global _CLIPS_STORE
    with _CLIPS_STORE_LOCK:
        if _CLIPS_STORE is None:
            recovered = fork_clips.ReviewStore(CLIPS_STATE_DIR)
            _CLIPS_STORE = recovered
        return _CLIPS_STORE


def _clips_state_unavailable_text(failure) -> str:
    """[R5-P11 / N7] The operator-facing wording for a durable record that could not be read.

    Three things it must do, because the pre-fix behaviour did none of them. Say plainly that the
    saved decisions could **not** be read, so a page of `PENDING` tiles is never mistaken for a
    folder nobody has reviewed. Say that nothing was changed or lost, which is now true by
    construction -- a refused load writes nothing. And name the retry, because the condition this
    reports is usually transient.

    It carries no filesystem path: `_clips_failure_text` reduces a bare `OSError` to numbers, and
    `PersistenceError` is already path-free by the R5-P7 contract.
    """
    return ("⚠️  Your saved decisions could NOT be read, so nothing has been loaded.\n"
            "Nothing was changed, overwritten or lost — the record on disk is untouched.\n"
            "This is usually temporary (a backup or indexer holding the file). "
            "Wait a moment and try again.\n"
            f"({_clips_failure_text(failure)})")


def _clips_unavailable_outputs(failure):
    """[R5-P11 / N7] The four scan outputs for a durable record that could not be read.

    Shaped so that every surface is honest at once, because the surfaces do not all get a status
    box. Refresh's outputs are `(gallery, counts, Confirm)` -- no report -- so the **gallery**
    carries the message rather than the "Nothing scanned yet" placeholder, which would read as a
    folder nobody has reviewed. The counters are replaced rather than recomputed: "Undecided 0"
    derived from a record that could not be read is precisely the misreading this exists to
    prevent, and so is a grid of `PENDING` tiles. Confirm is disabled, by the F8 rule -- its state
    comes only from the durable record, and there isn't one to read.
    """
    import html as _html

    message = _clips_state_unavailable_text(failure)
    grid = ('<div class="r5-empty">'
            + '<br>'.join(_html.escape(line) for line in message.splitlines())
            + '</div>')
    return (message, grid, 'Saved decisions unavailable — nothing was loaded.',
            gr.update(interactive=False))


def _clips_owner(request) -> str:
    """The owner key for a review session is the Gradio browser session hash.

    [R5-P3 / F3] Stated accurately, because the earlier wording here was wrong. `gr.Request`
    *injects* this value into the handler -- it is a real parameter, never listed in a click's
    `inputs`, so it is not a widget the browser can set directly. But the **value itself
    originates in the Gradio client**: `gradio/route_utils.py` reads it from the request body, and
    the browser mints it with `Math.random().toString(36).substring(2)`, which is not a
    cryptographically secure generator. So this is **not** an authenticated operator identity, and
    a non-conforming local client can choose any owner key it likes.

    What that does and does not cost. It scopes one browser session's pages and rescans against
    another's, which is its whole job. It is **not** what protects the media: the HTTP layer
    authorizes on server-minted `secrets.token_urlsafe` session/page/clip ids and asks the
    registry who owns them, so no owner identity is ever read from a request. The app stays
    localhost-only and claims no multi-user authentication.

    Absent or unusable identity **fails closed**: there is deliberately no shared anonymous
    registry to fall back into, because that is exactly how one browser's rescan would be able to
    revoke another's authorization.
    """
    session_hash = getattr(request, 'session_hash', None) if request is not None else None
    if not session_hash:
        raise fork_clips.Refusal(403, 'no_session_identity')
    return str(session_hash)


def _clips_remember(owner: str, generation, view: dict) -> bool:
    """Publish `view` as the owner's current page, but only if `generation` is still the newest.

    [R5-P3 / F1] The unconditional last-write-wins version of this function is what let an older
    scan reinstate its own revoked session. Publication is now conditional and the decision is
    made inside the coordinator's lock.
    """
    return _CLIPS_SCANS.publish(owner, generation, view)


def _clips_view(owner: str) -> dict:
    return _CLIPS_SCANS.view(owner)


def _clips_failure_text(failure) -> str:
    """Operator-visible text for a failed clips operation, carrying no filesystem path.

    [R5-P9] `PersistenceError` and `Refusal` already carry a deliberately path-free message --
    that is the R5-P7 contract the HTTP 503 body depends on, and it says more than a number does,
    so it is used as-is. A bare `OSError` is the opposite: it stringifies with `filename` (and
    `filename2`) attached whenever the failing call was made by pathname, so interpolating one put
    the journal's or the snapshot's absolute path into the status box the operator is looking at.
    Those are reported as numbers, by the same formatter the 503 body uses.
    """
    if isinstance(failure, OSError):
        return fork_clips.io_detail(failure)
    return str(failure)


def _clips_poster_exists(entry) -> bool:
    try:
        return os.path.getsize(fork_clips.poster_path_for(CLIPS_POSTER_DIR, entry)) > 0
    except OSError:
        return False


def _clips_poster_resolver(entry):
    return fork_clips.poster_path_for(CLIPS_POSTER_DIR, entry)


def _clips_identities(view: dict) -> list:
    return [view['identity'][entry.clip_id] for entry in view.get('entries', ())]


#: Remembered folder choices, under the same gitignored `state/` tree as the decision journal.
PREFS_DIR = os.path.join(ROOT_DIR, 'state')
PREFS_PATH = fork_prefs.preferences_path(PREFS_DIR)
PREFS_LABELS = {
    fork_prefs.CLIPS_LIBRARY: LABEL_CLIPS_LIBRARY,
    fork_prefs.CLIPS_REJECTED: LABEL_CLIPS_REJECTED,
    fork_prefs.RAW_CUTTER_OUTPUT: LABEL_PREF_RAW_OUTPUT,
    # [FORK] R8: the rest of the destinations, so "that folder has gone away" names the field the
    # operator recognises instead of falling back to the raw preference key.
    fork_prefs.AUDIO_LIBRARY: LABEL_PREF_AUDIO_LIBRARY,
    fork_prefs.AUDIO_DISCARDED: LABEL_PREF_AUDIO_DISCARDED,
    fork_prefs.AUDIO_CUTTER_OUTPUT: LABEL_PREF_AUDIO_OUTPUT,
    fork_prefs.AUDIO_PREVIEW_LIBRARY: LABEL_PREF_AUDIO_PREVIEW_LIBRARY,
    fork_prefs.AUDIO_PREVIEW_DISCARDED: LABEL_PREF_AUDIO_PREVIEW_DISCARDED,
    fork_prefs.RENDER_OUTPUT: LABEL_PREF_RENDER_OUTPUT,
}


def _browse_folder(current):
    """Open the native Windows folder dialog and answer with a path for the textbox.

    Returns the operator's existing value on cancel, on timeout and on any failure to show the
    dialog, so pressing Browse can only ever *add* a choice -- it can never clear a path the
    operator already typed. The dialog starts at the current value when that is a real folder.

    Choosing a folder does nothing else: no scan, no validation, no move. The existing Scan and
    Confirm checks still run on whatever ends up in the box, whether it was picked or pasted.
    """
    existing = current if isinstance(current, str) else ''
    try:
        status, chosen = fork_folders.pick_folder(existing or None)
    except Exception:                                  # noqa: BLE001 - browsing is never fatal
        return gr.update()
    if status == fork_folders.PICKED and chosen:
        return chosen
    return gr.update()                                  # cancel/timeout/unavailable: unchanged


def _load_folder_prefs():
    """Read saved defaults once at import. Creates nothing and validates nothing."""
    preferences, notice = fork_prefs.load_preferences(PREFS_PATH)
    missing = fork_prefs.unavailable_notice(preferences, PREFS_LABELS)
    return preferences, "\n".join(part for part in (notice, missing) if part)


_FOLDER_PREFS, _FOLDER_PREFS_NOTICE = _load_folder_prefs()

#: [FORK] R8: the non-folder options, from the same file and read at the same moment. Kept in a
#: separate dict rather than merged into `_FOLDER_PREFS`, because `_pref` feeds folder textboxes
#: and a policy string appearing in one would be a path.
_FOLDER_OPTIONS = fork_prefs.load_options(PREFS_PATH)


def _pref(key: str) -> str:
    return _FOLDER_PREFS.get(key, '')


def _option(key: str) -> str:
    return _FOLDER_OPTIONS.get(key, fork_prefs.OPTION_DEFAULTS.get(key, ''))


def _collision_policy() -> str:
    """The operator's saved filename-collision policy, resolved safely.

    One reader for both sorters, so the video and audio review workflows can never disagree about
    what the setting means. `resolve_policy` is total, so a hand-edited or corrupt value in
    `preferences.json` yields the safe default rather than raising inside a live Gradio event.
    """
    return fork_collision.resolve_policy(_option(fork_prefs.COLLISION_POLICY))


def _on_collision_policy_change(policy):
    """Apply and persist the collision policy as soon as it is chosen.

    It persists on change rather than waiting for Save defaults, because the two sorters read the
    *stored* value -- a radio that applied only after a separate Save would mean the screen and
    the behaviour disagreed until the operator pressed a second button.

    Feedback is a toast and `outputs` is exactly `[]`, which is the pattern
    `_on_open_output_folder_click` established and for the same reason: writing `pref_status` here
    would add a third writer to that panel and let changing a radio overwrite the result of a Save
    the operator is still reading.
    """
    resolved = fork_collision.resolve_policy(policy)
    _FOLDER_OPTIONS[fork_prefs.COLLISION_POLICY] = resolved
    failure = fork_prefs.save_preferences(
        PREFS_PATH, _FOLDER_PREFS, {fork_prefs.COLLISION_POLICY: resolved})
    if failure:
        # The in-memory value still applies to this session; only persistence failed, and saying
        # so is better than implying the choice did not take.
        gr.Warning(f"{failure} The choice applies to this session only.")
        return
    gr.Info(STATUS_COLLISION_SAVED_SUFFIX if resolved == fork_collision.SUFFIX
            else STATUS_COLLISION_SAVED_SKIP)


def get_render_output_dir() -> str:
    """Where a finished render is published. The configured folder, or the installation's own.

    [FORK] R8 UX Refresh. Before this, `output/` inside the installation was the only possible
    destination, which is wrong for an app whose other five destinations are all configurable.

    Three properties, each deliberate:

    * **It resolves at call time, not at import.** Saving a new folder in Settings takes effect on
      the next render rather than on the next restart.
    * **A configured folder is used only if it ALREADY EXISTS.** This function never creates one,
      which is the same rule the rest of the preference store follows -- a remembered path is a
      suggestion, not an action, and a folder that has gone away is *reported* (Settings shows it
      through `unavailable_notice`, since `RENDER_OUTPUT` is a destination key) rather than
      silently recreated somewhere a typo pointed. Falling back is therefore never silent data
      loss: the render still lands somewhere real, and Settings says why it was not the configured
      folder.
    * **It governs the PUBLISHED render and nothing else.** Temporary processing directories, the
      Gradio upload folder, `session_dir` and every cache keep resolving through their own
      constants. Moving finished videos must not relocate the scratch the renderer is mid-way
      through writing, and the share-staging folder stays a child of whatever this returns, so the
      final step is still a same-directory rename and `EXDEV` stays structurally impossible.
    """
    configured = _pref(fork_prefs.RENDER_OUTPUT)
    if configured and os.path.isdir(configured):
        return configured
    return get_output_dir()


def _on_save_folder_prefs(library, rejected, raw_output, clips_input_folder, raw_input_folder,
                          render_source,
                          audio_library_folder, audio_discarded_folder, audio_output_folder,
                          audio_source_file,
                          audio_preview_input_folder, audio_preview_library_folder,
                          audio_preview_discarded_folder,
                          render_output_folder=None, collision_policy=None):
    """Persist the folder defaults. Writes a file; creates no media directory.

    [FORK] R7 appended its four fields at the END of the signature, and R7.2 appended its three
    after those, for the reason the fork appends everywhere else: Gradio passes handler arguments
    POSITIONALLY, so every pre-existing index stays exactly where the registration already put it.

    [FORK] R8 appends two more, and gives both a DEFAULT. The default is not decoration: several
    suites call this handler with the pre-R8 argument list, and a required parameter would turn
    every one of them into a `TypeError` that says nothing about the feature being tested. `None`
    means "not supplied", which is distinct from "supplied empty" and is handled below.
    """
    values = {
        fork_prefs.CLIPS_LIBRARY: library,
        fork_prefs.CLIPS_REJECTED: rejected,
        fork_prefs.RAW_CUTTER_OUTPUT: raw_output,
        fork_prefs.CLIPS_INPUT: clips_input_folder,
        fork_prefs.RAW_CUTTER_INPUT: raw_input_folder,
        fork_prefs.QUICK_RENDER_SOURCE: render_source,
        fork_prefs.AUDIO_LIBRARY: audio_library_folder,
        fork_prefs.AUDIO_DISCARDED: audio_discarded_folder,
        fork_prefs.AUDIO_CUTTER_OUTPUT: audio_output_folder,
        fork_prefs.AUDIO_CUTTER_INPUT: audio_source_file,
        fork_prefs.AUDIO_PREVIEW_INPUT: audio_preview_input_folder,
        fork_prefs.AUDIO_PREVIEW_LIBRARY: audio_preview_library_folder,
        fork_prefs.AUDIO_PREVIEW_DISCARDED: audio_preview_discarded_folder,
        # [FORK] R8: where finished renders are published.
        fork_prefs.RENDER_OUTPUT: (render_output_folder
                                   if render_output_folder is not None
                                   else _pref(fork_prefs.RENDER_OUTPUT)),
    }
    # A pre-R8 caller supplies no policy, and must not reset the operator's choice to the default
    # as a side effect of saving folders. `None` therefore means "keep what is stored".
    options = (None if collision_policy is None
               else {fork_prefs.COLLISION_POLICY:
                     fork_collision.resolve_policy(collision_policy)})
    failure = fork_prefs.save_preferences(PREFS_PATH, values, options)
    if failure:
        return f"❌ {failure}"
    _FOLDER_PREFS.update({key: (values.get(key) or '').strip() for key in fork_prefs.KEYS})
    if options is not None:
        _FOLDER_OPTIONS.update(options)
    missing = fork_prefs.unavailable_notice(_FOLDER_PREFS, PREFS_LABELS)
    saved = f"✅ Saved. These settings will be filled in next time you start BeatSync."
    return f"{saved}\n{missing}" if missing else saved


# =============================================================================================
# [FORK] Digital-Union (R8 UX Refresh): Settings -> Cache Management.
#
# The whole safety argument is CONTAINMENT, and it lives in two halves. This half decides which
# four directories may ever be named; `beatsync_fork.cache_admin` proves that nothing outside the
# directory it is handed is touched. Neither half discovers a path, so the list of things that
# must never be cleared -- input media, finished exports, Media Libraries, Discarded folders,
# models, the portable runtime, saved preferences, the review decision journals -- is enforced by
# never appearing here rather than by a blocklist that a future edit could get wrong.
#
# Note what is deliberately absent: `output/` (finished renders), `input/audio`, `input/video`
# (the operator's own media), and `state/clips-review/*.json` (the decision journals, which sit
# BESIDE the poster cache and are why the poster subdirectory is named rather than its parent).
# =============================================================================================

def _cache_categories() -> list:
    """The four clearable caches, built fresh so a changed Render output folder is reflected.

    `CLIPS_POSTER_DIR` rather than `CLIPS_STATE_DIR`: the journals live in the parent, and a
    category pointed at the parent would delete the operator's review decisions.
    """
    return [
        fork_cache.Category(
            fork_cache.POSTERS, LABEL_CACHE_POSTERS, INFO_CACHE_POSTERS, CLIPS_POSTER_DIR),
        fork_cache.Category(
            fork_cache.PROCESSING, LABEL_CACHE_PROCESSING, INFO_CACHE_PROCESSING,
            get_processing_dir()),
        fork_cache.Category(
            fork_cache.RETIRED_UPLOADS, LABEL_CACHE_UPLOADS, INFO_CACHE_UPLOADS,
            RETIRED_UPLOADS_DIR),
        fork_cache.Category(
            fork_cache.ANALYSIS, LABEL_CACHE_ANALYSIS, INFO_CACHE_ANALYSIS,
            os.path.join(get_input_dir(), 'video_analysis_cache'), expensive=True),
    ]


def _on_inspect_cache():
    """Measure every category. Reads only -- creates nothing, deletes nothing, moves nothing."""
    measurements = [fork_cache.measure(category) for category in _cache_categories()]
    return "\n".join(fork_cache.report_lines(measurements))


def _on_clear_cache(selected, confirmed):
    """Delete the selected categories, and only after an explicit tick.

    The confirmation is a separate checkbox rather than a second click, because a second click is
    indistinguishable from an impatient first one. Nothing is deleted until both a selection and
    the tick are present, and the refusal says which of the two is missing.

    Selection arrives as the checkbox group's values, which are category KEYS. They are matched
    against the built category list rather than used to construct anything, so a value the browser
    invented names no directory and is simply not found.
    """
    chosen = [str(value) for value in (selected or [])]
    if not chosen:
        return "Nothing was selected, so nothing was cleared."
    if not confirmed:
        return ("Tick the confirmation box as well — nothing has been deleted.\n"
                "Clearing a cache cannot be undone.")
    wanted = [category for category in _cache_categories() if category.key in chosen]
    if not wanted:
        return "Those categories are not recognised, so nothing was cleared."
    outcomes = [fork_cache.clear(category) for category in wanted]
    return "\n".join(fork_cache.cleared_lines(outcomes))


def _on_reset_folder_prefs():
    """Clear the saved defaults. Removes no folder and no media -- only the remembered choices.

    [FORK] R8 appends the Render output box to the cleared set and returns the collision policy
    to its default. The tuple grew by two and its ORDER matches the registration's `outputs`
    positionally, as every return on this handler must.
    """
    failure = fork_prefs.save_preferences(PREFS_PATH, fork_prefs.empty_preferences(),
                                          fork_prefs.empty_options())
    if failure:
        return ('', '', '', '', '', '', '', '', '',
                fork_prefs.OPTION_DEFAULTS[fork_prefs.COLLISION_POLICY], f"❌ {failure}")
    _FOLDER_PREFS.update(fork_prefs.empty_preferences())
    _FOLDER_OPTIONS.update(fork_prefs.empty_options())
    return ('', '', '', '', '', '', '', '', '',
            fork_prefs.OPTION_DEFAULTS[fork_prefs.COLLISION_POLICY],
            "✅ Defaults cleared. No folder or file was deleted.")


# =============================================================================================
# [FORK] Digital-Union (R7): Raw Audio Cutter V1.
#
# The whole feature is "FFmpeg's silencedetect says where the quiet is, and the clips are what is
# left over". Every decision -- the argv, the log parsing, the segment arithmetic, the filing rules
# -- lives in `beatsync_fork.audio_cutter` and `beatsync_fork.audio_library`, which are stdlib-only
# and directly testable. These handlers are a view: they read widgets, call the fork core, and turn
# its answers into text.
#
# BOTH process launches are in the fork core, not here. Two suites pin this module's
# `subprocess.run` site count at exactly two, and R7 adds none.
#
# The review list rides on a `gr.State`, so it is per-browser-session and this module keeps no
# mutable record of one operator's clips that another could reach.
# =============================================================================================

def _audio_tools() -> tuple:
    """`(ffmpeg, ffprobe)` from the runtime module, imported locally so no module-level import is
    added to a file whose import order is load-bearing."""
    from ffmpeg_processing import FFMPEG_PATH, FFPROBE_PATH
    return FFMPEG_PATH, FFPROBE_PATH


def _audio_settings(threshold, min_silence, pad, min_clip, fmt, fade_edges=False) -> dict:
    """Widget values to the resolved settings, total over anything Gradio can hand over.

    [FORK] R8 Phase 9: `fade_edges` is APPENDED with a default, so the pre-R8 five-argument
    call every other caller makes still resolves to fades OFF -- which is the documented
    default and keeps already-accepted exports bit-identical.

    A `gr.Number` legitimately arrives as `None` or as `''` when the operator clears it, so every
    read falls back to the documented default rather than raising inside an event handler.
    """
    def number(value, default):
        try:
            resolved = float(value)
        except (TypeError, ValueError):
            return float(default)
        if resolved != resolved:                      # NaN
            return float(default)
        return resolved

    return {
        'threshold_db': number(threshold, fork_audio.DEFAULT_THRESHOLD_DB),
        'min_silence': max(0.0, number(min_silence, fork_audio.DEFAULT_MIN_SILENCE)),
        'pad': max(0.0, number(pad, fork_audio.DEFAULT_PAD)),
        'min_clip': max(0.0, number(min_clip, fork_audio.DEFAULT_MIN_CLIP)),
        # [R7.2] One total resolver in the fork core rather than a chain of comparisons here. The
        # old `FLAC if fmt == FLAC else WAV` form silently mapped MP3 onto WAV, so adding a third
        # format to the widget alone would have written `.wav` files from an MP3 selection.
        'format': fork_audio.normalize_format(fmt),
        # [FORK] R8 Phase 9: a checkbox, so `bool()` is the whole resolution. Anything Gradio
        # could hand over that is not truthy means off, which is the safe direction.
        'fade_edges': bool(fade_edges),
    }


def _audio_probe(source: str):
    """`(SourceInfo, refusal_text)`. A refusal is prose for the operator, never an exception."""
    text = (source or '').strip().strip('"')
    if not text:
        return None, "❌ Choose a recording first — press Browse file… or paste a full path."
    if not os.path.isfile(text):
        return None, f"❌ That file does not exist:\n{text}"
    try:
        ffmpeg, ffprobe = _audio_tools()
    except Exception as failure:                      # noqa: BLE001 - reported, never raised
        return None, f"❌ FFmpeg is not available: {failure}"
    probe = fork_audio.run_probe_command(fork_audio.build_probe_command(ffprobe, text))
    info = fork_audio.parse_probe(probe, path=text)
    if not info.has_audio:
        return None, fork_audio.plan_summary(info, (), (), {})
    return info, ""


def _audio_plan(info, settings) -> tuple:
    """Run the detector and plan the segments. `(silences, segments, failure_text)`."""
    ffmpeg, _ = _audio_tools()
    code, log = fork_audio.run_media_command(
        fork_audio.build_silence_command(ffmpeg, info.path,
                                         settings['threshold_db'], settings['min_silence']))
    # A non-zero code with no parsable silence is a real failure; a non-zero code that still
    # produced a usable log is not worth refusing over, so the log is read either way.
    silences = fork_audio.parse_silence_log(log, info.duration)
    segments = fork_audio.plan_segments(info.duration, silences,
                                        settings['pad'], settings['min_clip'])
    if code != 0 and not silences and not segments:
        detail = (log or '').strip().splitlines()
        return (), (), ("❌ FFmpeg could not read that file.\n"
                        + (detail[-1] if detail else 'No further detail.'))
    return silences, segments, ""


def _on_audio_analyze(source, threshold, min_silence, pad, min_clip, fmt):
    """Analyze: detect silence and show the plan. **Writes nothing at all.**"""
    settings = _audio_settings(threshold, min_silence, pad, min_clip, fmt)
    info, refusal = _audio_probe(source)
    if refusal:
        return refusal, []
    try:
        silences, segments, failure = _audio_plan(info, settings)
    except Exception as failure:                      # noqa: BLE001 - analysis is never fatal
        return f"❌ Analysis failed: {failure}", []
    if failure:
        return failure, []
    stem = fork_audio.safe_stem(info.path)
    return (fork_audio.plan_summary(info, silences, segments, settings),
            fork_audio.segment_rows(segments, stem, settings['format']))


def _audio_mp3_problem(fmt) -> str:
    """`""` unless MP3 was asked for and the installed FFmpeg cannot encode it.

    [R7.2] Asked once per cut rather than discovered as every clip failing with an FFmpeg error
    the operator has no way to read. The portable build this app installs does carry
    `libmp3lame`, but the installer can be pointed at another FFmpeg, so the check is real rather
    than ceremonial. Any failure to *run* the probe is treated as "cannot confirm", which refuses:
    writing nothing is better than writing a folder of unplayable files.
    """
    if fork_audio.normalize_format(fmt) != fork_audio.FORMAT_MP3:
        return ''
    try:
        ffmpeg, _ = _audio_tools()
    except Exception as failure:                      # noqa: BLE001 - reported, never raised
        return f"FFmpeg is not available: {failure}"
    listing = fork_audio.run_probe_command(fork_audio.build_encoders_command(ffmpeg))
    if fork_audio.encoder_available(listing, fork_audio.MP3_ENCODER):
        return ''
    return (f"this FFmpeg build has no {fork_audio.MP3_ENCODER} encoder, so it cannot write MP3. "
            f"Choose WAV or FLAC, or reinstall the portable FFmpeg.")


def _audio_format_line(settings: dict) -> str:
    """One honest sentence about what was written, including the MP3 caveat the owner asked for."""
    fmt = fork_audio.normalize_format(settings.get('format'))
    if fmt == fork_audio.FORMAT_FLAC:
        return f"Format: FLAC - lossless - {fork_audio.OUTPUT_SAMPLE_RATE} Hz"
    if fmt == fork_audio.FORMAT_MP3:
        return (f"Format: MP3 - {fork_audio.DEFAULT_MP3_BITRATE_KBPS} kbps CBR "
                f"({fork_audio.MP3_ENCODER}) - {fork_audio.OUTPUT_SAMPLE_RATE} Hz\n"
                f"         MP3 is a LOSSY re-encode, including from an MP3 source: this tool "
                f"decodes the audio to find the silences, so there is no stream copy.\n"
                f"         What you gain is much smaller files, not better audio.")
    return f"Format: WAV - 16-bit PCM - {fork_audio.OUTPUT_SAMPLE_RATE} Hz"


def _audio_fade_line(settings: dict, segments) -> str:
    """What the fades ACTUALLY did, or `''` when they are off.

    [FORK] R8 Phase 9. It reports the **measured range across the clips that were planned**, not
    the setting, because the two differ whenever a clip received less padding than was requested --
    which is the normal case in a crowded passage and is exactly what the operator needs to see.
    A clip with no padding gets no fade and is counted as such.
    """
    if not settings.get('fade_edges'):
        return ""
    lengths = []
    for segment in segments or ():
        fade_in, fade_out = fork_audio.fade_lengths(segment, True)
        lengths.extend([fade_in, fade_out])
    if not lengths:
        return "Soft edges: on, but nothing was planned to fade."
    shaped = [value for value in lengths if value > 0.0]
    if not shaped:
        return ("Soft edges: on, but no clip had any padding to fade into, so none was applied. "
                "Raise Padding to give the fades room.")
    low, high = min(shaped), max(shaped)
    span = (f"{low * 1000:.0f} ms" if abs(high - low) < 0.0005
            else f"{low * 1000:.0f}-{high * 1000:.0f} ms")
    silent = len(lengths) - len(shaped)
    note = f" ({silent} edge(s) had no padding and were left sharp)" if silent else ""
    return f"Soft edges: on - fades of {span}, bounded by each clip's own padding{note}"


def _on_audio_cut(source, output_folder, threshold, min_silence, pad, min_clip, fmt,
                  fade_edges=False, request: gr.Request = None):
    """Cut: re-analyze, then write each planned clip into a fresh timestamped run folder.

    Re-analyzing rather than trusting the on-screen plan is deliberate and matches the Raw Cutter:
    the file or a setting may have changed since Analyze, and writing a stale plan would export
    clips that do not correspond to anything the operator was shown.

    [R7.2] The clips it wrote are then published into the audio registry, so the gallery can fetch
    them on hover instead of carrying their bytes. **Publication cannot fail the cut**: the files
    are already on disk and the report already says so, so a registry refusal costs the preview and
    nothing else.
    """
    settings = _audio_settings(threshold, min_silence, pad, min_clip, fmt, fade_edges)
    blank = (AUDIO_GALLERY_EMPTY_HTML, AUDIO_COUNTS_INITIAL_TEXT, '', {}, '')

    mp3_problem = _audio_mp3_problem(settings['format'])
    if mp3_problem:
        return (f"❌ Nothing was written — {mp3_problem}", [], *blank)

    info, refusal = _audio_probe(source)
    if refusal:
        return (refusal, [], *blank)

    folder = (output_folder or '').strip().strip('"')
    if not folder:
        return ("❌ Choose an output folder first.", [], *blank)
    try:
        silences, segments, failure = _audio_plan(info, settings)
    except Exception as failure:                      # noqa: BLE001
        return (f"❌ Analysis failed: {failure}", [], *blank)
    if failure:
        return (failure, [], *blank)

    stem = fork_audio.safe_stem(info.path)
    rows = fork_audio.segment_rows(segments, stem, settings['format'])
    if not segments:
        return (fork_audio.plan_summary(info, silences, segments, settings), rows, *blank)

    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    run_directory = os.path.join(folder, fork_audio.run_directory_name(stem, stamp))
    try:
        os.makedirs(run_directory, exist_ok=False)
    except OSError as failure:
        return (f"❌ Could not create the output folder:\n{run_directory}\n{failure}",
                rows, *blank)

    ffmpeg, _ = _audio_tools()
    written, failures, paths, spans = {}, [], [], {}
    for segment in segments:
        name = fork_audio.clip_filename(stem, segment.index, settings['format'])
        destination = os.path.join(run_directory, name)
        code, log = fork_audio.run_media_command(
            fork_audio.build_extract_command(ffmpeg, info.path, segment, destination,
                                             settings['format'],
                                             fade_edges=settings['fade_edges']),
            timeout=fork_audio.EXTRACT_TIMEOUT_SECONDS)
        if code == 0 and os.path.isfile(destination):
            written[segment.index] = name
            paths.append(destination)
            # Keyed by the decision identity so the span survives paging exactly as a decision
            # does. It is the one thing the Audio Preview tab cannot know and this one can: where
            # in the original recording the clip came from.
            spans[fork_clips.decision_identity(destination, os.path.getsize(destination),
                                               os.stat(destination).st_mtime_ns)] = (
                f"{fork_audio.format_timecode(segment.start)}"
                f"→{fork_audio.format_timecode(segment.end)}")
        else:
            detail = (log or '').strip().splitlines()
            failures.append(f"clip {segment.index}: {detail[-1] if detail else 'failed'}")

    encoding = fork_audio.encoding_record(settings['format'])
    manifest = fork_audio.manifest_payload(info, segments, settings, written, encoding)
    manifest_note = ''
    try:
        with open(os.path.join(run_directory, fork_audio.MANIFEST_NAME), 'w',
                  encoding='utf-8') as handle:
            json.dump(manifest, handle, indent=2)
    except OSError as failure:
        manifest_note = f"\n⚠️  The manifest could not be written: {failure}"

    lines = [f"✅ Wrote {len(written)} of {len(segments)} clip(s).",
             f"Output folder:\n{run_directory}", '',
             _audio_format_line(settings)]
    # [FORK] R8 Phase 9: the EFFECTIVE fade length, which the task asked to be shown and which
    # differs from the setting whenever a clip had less padding than was requested.
    fade_line = _audio_fade_line(settings, segments)
    if fade_line:
        lines.append(fade_line)
    lines += [f"A machine-readable record is in {fork_audio.MANIFEST_NAME}.",
              "Your source file was not modified."]
    if failures:
        lines += ['', "Not written:"] + [f"  • {line}" for line in failures]
    report = "\n".join(lines) + manifest_note

    # [R7.2] The review surface is the gallery, now backed by the registry. Decisions start empty
    # -- every clip is undecided until the operator says otherwise -- and Confirm needs at least
    # one KEEP before it will move anything.
    view, publish_note = _audio_load_view(
        _AUDIO_SURFACE_CUT, request, paths, run_directory, recursive=False,
        page_index=1, page_size=fork_audio_review.DEFAULT_PAGE_SIZE, spans=spans)
    gallery, counts = _audio_render(_AUDIO_SURFACE_CUT, view)
    return (report + publish_note, rows, gallery, counts, '', view, run_directory)


# =============================================================================================
# [FORK] Digital-Union (R7.2): ONE audio review core, two surfaces.
#
# The Raw Audio Cutter's review half and the standalone Audio Preview tab do the same job on
# different inputs: publish an ordered list of audio files into an authorized registry, draw the
# hover gallery, collect decisions in the page, then sort on Confirm. Everything here is written
# once and parameterised by an `_AudioSurface`, so a behaviour that is wrong is wrong in one place.
#
# Gradio needs distinct callables with distinct signatures, so each surface has thin handlers that
# do nothing but name their surface and their widgets.
#
# **No module-level mutable record of an operator's clips exists.** Every view rides on that tab's
# own `gr.State`, so it is per-browser-session; the registries hold opaque-id-to-file-identity maps
# and nothing else.
# =============================================================================================

class _AudioSurface:
    """Which registry, which URL prefix, which grid, and what to call an empty one."""

    def __init__(self, name, registry, media_prefix, grid_id, store_id, counts_id,
                 empty_message):
        self.name = name
        self.registry = registry
        self.media_prefix = media_prefix
        self.grid_id = grid_id
        self.store_id = store_id
        self.counts_id = counts_id
        self.empty_message = empty_message


_AUDIO_SURFACE_CUT = _AudioSurface(
    'cutter', _AUDIO_CUT_REGISTRY, fork_audio_review.MEDIA_PREFIX_CUTTER,
    fork_audio_review.GRID_CUTTER, 'audio-decisions', 'audio-counts',
    'No clips to review. Cut a recording first.')

_AUDIO_SURFACE_PREVIEW = _AudioSurface(
    'preview', _AUDIO_PREVIEW_REGISTRY, fork_audio_review.MEDIA_PREFIX_PREVIEW,
    fork_audio_review.GRID_PREVIEW, 'audio-preview-decisions', 'audio-preview-counts',
    'No audio clips loaded. Choose a folder and press Scan Folder.')


def _audio_owner(request):
    """The per-browser scoping key, injected by Gradio and never listed in a click's `inputs`.

    Same provenance caveat as the Clips Preview, restated because it is easy to over-read: the
    value originates in the Gradio **client** (`Math.random().toString(36)`), so it scopes one
    browser's review against another's and is **not** an authenticated identity. Absent identity
    fails closed -- there is deliberately no shared anonymous registry to fall into.
    """
    return fork_clips._require_owner(
        getattr(request, 'session_hash', None) if request is not None else None)


def _audio_publish(surface, owner, paths, page_index, page_size):
    """Mint a fresh session and one page of opaque clip ids for `paths`.

    Returns `(session_id, page_token, entries, start)`. This revokes the owner's previous session
    **on this surface only**, which is correct -- they have finished with the page it served -- and
    is why the two surfaces hold separate registries.
    """
    total = len(paths)
    start, count = fork_audio_review.page_slice(page_index, total, page_size)
    frozen = fork_audio_review.freeze_paths(paths, start, count)
    session_id = surface.registry.new_session(owner)
    page_token, entries = surface.registry.publish_page(
        owner, session_id, int(fork_audio_review.resolve_page(page_index, total, page_size)),
        frozen, servable=fork_audio_review.SERVABLE_SUFFIXES)
    return session_id, page_token, entries, start


def _audio_identities(entries) -> dict:
    """`clip_id -> stable decision identity`.

    The identity is `sha256(realpath | size | mtime_ns)`, and it is what makes a decision survive
    **paging** and stop applying when the bytes change. The browser never sees it: it holds the
    per-page `clip_id`, and this map is how the server translates. Re-recording a clip therefore
    does not inherit the old one's decision, structurally rather than by a check somebody has to
    remember to run.
    """
    return {entry.clip_id: fork_clips.decision_identity(entry.path, entry.size, entry.mtime_ns)
            for entry in entries}


def _audio_durations(entries) -> dict:
    """Real clip lengths for the tile labels, from the portable FFprobe.

    Cached inside `ensure_durations` on the clip's identity, so a scan pays for a page once. The
    command and the process launch both live in the fork core, exactly like the poster pass,
    because two suites pin this module's `subprocess.run` site count at two. A failure is never
    fatal: a tile shows an honest dash rather than a guessed length.
    """
    try:
        from ffmpeg_processing import FFPROBE_PATH    # local: no module-level import is added
        return fork_clips.ensure_durations(entries, FFPROBE_PATH,
                                           fork_clips.run_duration_command)
    except Exception:                                 # noqa: BLE001 - labels are never fatal
        return {}


def _audio_tiles(view) -> list:
    """The page's tiles, built from the server's own entry list and decision state."""
    durations = view.get('durations') or {}
    names = view.get('names') or {}
    spans = view.get('spans') or {}
    identity = view.get('identity') or {}
    tiles = []
    for offset, entry in enumerate(view.get('entries') or (), start=1):
        key = identity.get(entry.clip_id, entry.clip_id)
        tiles.append(fork_audio_review.AudioTile(
            clip_id=entry.clip_id,
            number=int(view.get('start', 0)) + offset,
            name=names.get(key) or os.path.basename(entry.path),
            duration=durations.get(entry.clip_id),
            span=spans.get(key, '')))
    return tiles


def _audio_page_decisions(view) -> dict:
    """`clip_id -> decision` for this page, projected from the identity-keyed record."""
    stored = view.get('decisions') or {}
    identity = view.get('identity') or {}
    return {clip_id: stored.get(key, fork_audio_review.PENDING)
            for clip_id, key in identity.items()}


def _audio_render(surface, view) -> tuple:
    """`(gallery_html, counts_line)` for a view. The one place either is produced."""
    tiles = _audio_tiles(view)
    decisions = _audio_page_decisions(view)
    grid = fork_audio_review.gallery_html(
        tiles, decisions, view.get('session', ''), view.get('page', ''),
        grid_id=surface.grid_id, store_id=surface.store_id, counts_id=surface.counts_id,
        media_prefix=surface.media_prefix, empty_message=surface.empty_message)
    return grid, fork_audio_review.counts_line(
        fork_audio_review.decision_counts(tiles, decisions))


def _audio_load_view(surface, request, paths, root, recursive, page_index, page_size,
                     spans=None, decisions=None, names=None):
    """Publish one page of `paths` and build the view. Returns `(view, note)`.

    `note` is prose for the caller's report when publication failed -- never an exception, because
    every caller is a Gradio event handler and a failed preview must not discard a cut that
    actually wrote files.

    `decisions` and `names` are carried in **by identity**, which is the whole of Phase 2's "keep
    review decisions stable while changing pages": the record is keyed by the file, so turning to
    page 3 and back preserves page 1 exactly, with no per-page bookkeeping at all.
    """
    resolved_size = fork_audio_review.resolve_page_size(page_size)
    empty = {'entries': (), 'identity': {}, 'decisions': dict(decisions or {}),
             'names': dict(names or {}), 'root': root, 'recursive': bool(recursive),
             'total': len(paths), 'paths': list(paths), 'page': '', 'session': '',
             'page_index': 1, 'page_size': resolved_size, 'start': 0,
             'spans': dict(spans or {}), 'durations': {}, 'armed': ''}
    if not paths:
        return empty, ''
    try:
        owner = _audio_owner(request)
    except fork_clips.Refusal as refusal:
        return empty, (f"\n\u26a0\ufe0f  The preview gallery needs a browser session identity "
                       f"({refusal.reason}). Reload the page.")
    try:
        session_id, page_token, entries, start = _audio_publish(
            surface, owner, paths, page_index, resolved_size)
    except fork_clips.Refusal as refusal:
        return empty, (f"\n\u26a0\ufe0f  The preview gallery could not be opened ({refusal.reason}). "
                       f"The files are on disk either way.")

    view = dict(empty)
    view.update({
        'session': session_id, 'page': page_token, 'entries': entries,
        'identity': _audio_identities(entries), 'start': start,
        'page_index': int(fork_audio_review.resolve_page(page_index, len(paths), resolved_size)),
    })
    view['durations'] = _audio_durations(entries)
    return view, ''


def _audio_merge_payload(view, payload) -> dict:
    """Fold the page's hidden field into the view's identity-keyed decision and name records.

    The browser is not trusted to be well-formed, only to be the operator's own page. An
    unreadable payload merges **nothing**, which leaves the previous server state standing rather
    than clearing it -- the opposite choice would let one malformed update silently discard a page
    of real decisions.

    **A clip id that is not on this page is ignored**, so a payload cannot reach a clip the
    operator is not looking at, and nothing is ever keyed by a path the page sent.
    """
    identity = view.get('identity') or {}
    decisions = dict(view.get('decisions') or {})
    names = dict(view.get('names') or {})
    try:
        parsed = json.loads(payload or '{}')
    except (ValueError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    sent_decisions = parsed.get('decisions')
    if isinstance(sent_decisions, dict):
        for clip_id, decision in sent_decisions.items():
            key = identity.get(str(clip_id))
            if key is None:
                continue
            text = str(decision)
            decisions[key] = (text if text in fork_audio_review.DECISIONS
                              else fork_audio_review.PENDING)

    sent_names = parsed.get('names')
    if isinstance(sent_names, dict):
        for clip_id, name in sent_names.items():
            key = identity.get(str(clip_id))
            if key is None:
                continue
            names[key] = str(name)

    updated = dict(view)
    updated['decisions'] = decisions
    updated['names'] = names
    return updated


def _audio_resolved_destinations(library, discarded, plan):
    """The real paths a sort will write, resolved the way validation resolved them.

    Resolving rather than reusing the typed strings matters for the report: the operator may have
    typed a junction, a relative path or a different letter case, and the report must name the
    folder the files actually went to. A destination this plan will not write resolves to `''`.
    """
    def real(role, typed):
        identified, verdict = fork_clips.identify_root(role, typed)
        return identified.real if (verdict.ok and identified) else str(typed or '').strip()

    return (real(fork_clips.ROLE_LIBRARY, library) if plan.needs_library else '',
            real(fork_clips.ROLE_REJECTED, discarded) if plan.needs_discarded else '')


def _audio_sort(surface, view, library, discarded, sweep_unmarked):
    """Perform the sort and return `(message, refreshed_view)`.

    Nothing about where a file goes comes from the browser: the entries are the registry's frozen
    page, each decision is read from the view's identity-keyed record, and the destinations are the
    already-validated real paths. `sweep_unmarked` is an explicit parameter so an unmarked clip can
    only be discarded by a caller that obtained the operator's one-time confirmation.
    """
    identity = view.get('identity') or {}
    decisions = view.get('decisions') or {}
    names = view.get('names') or {}

    def decision_of(entry):
        return decisions.get(identity.get(entry.clip_id, ''), fork_audio_review.PENDING)

    def name_of(entry):
        return names.get(identity.get(entry.clip_id, ''), '')

    outcomes = fork_audio_review.sort_clips(
        view.get('entries') or (), decision_of, name_of,
        {fork_audio_review.SELECTED: library, fork_audio_review.REJECTED: discarded},
        unmarked_as=(fork_audio_review.REJECTED if sweep_unmarked
                     else fork_audio_review.PENDING),
        # [FORK] R8: the same saved policy the video sorter reads, for the same reason.
        policy=_collision_policy())

    message = "\n".join(fork_audio_review.sorted_report_lines(outcomes, library, discarded))
    moved = {o.clip_id for o in outcomes if o.ok}

    # Repaint from what is genuinely still on disk. Showing the old page would leave already-moved
    # clips on screen, and pressing Confirm again would then report them as missing.
    remaining = [entry for entry in (view.get('entries') or ()) if entry.clip_id not in moved]
    survivor_ids = {entry.clip_id for entry in remaining}
    refreshed = dict(view)
    refreshed['entries'] = remaining
    refreshed['identity'] = {clip: key for clip, key in identity.items()
                             if clip in survivor_ids}
    # A clip that moved keeps no decision. Its identity is gone from this page, and a later scan of
    # the DESTINATION folder must not inherit a decision made about it here.
    survivors = set(refreshed['identity'].values())
    refreshed['decisions'] = {k: v for k, v in decisions.items() if k in survivors}
    refreshed['names'] = {k: v for k, v in names.items() if k in survivors}
    refreshed['armed'] = ''
    return message, refreshed


def _audio_confirm_core(surface, view, payload, library, discarded, armed_signature,
                        app_root=''):
    """The whole of Confirm, for either surface.

    Returns `(message, view, gallery, counts, arm_signature, show_gate, sorted_any)`. `show_gate`
    asks the caller to reveal Proceed/Cancel; `arm_signature` is what a later Proceed must still
    match; `sorted_any` says whether the sort was actually *attempted*.

    **`sorted_any` is returned rather than inferred, and that matters.** The Audio Preview tab
    re-enumerates its folder after a sort, and every other path here -- nothing loaded, no clip
    kept, a refused destination pair, the sweep gate -- moves nothing at all. Deriving "did
    anything happen?" from an empty signature would have conflated the refusals with the success,
    making a refused Confirm trigger a pointless folder walk and replace the refusal's own view.

    The order of the gates is the contract:

    1. **nothing loaded** -- nothing to do, and nothing is said about folders;
    2. **the page's own decisions are folded in first**, so Confirm acts on what is on screen;
    3. **at least one clip must be kept.** This is R7.2's admission rule, replacing R7.1's
       "nothing may be undecided": the operator marks the good clips and presses Confirm;
    4. **the destinations are re-validated here**, not trusted from the change handler. The folder
       that was checked and the folder that is written must be the same one, and a folder can be
       renamed between the two events;
    5. **unmarked clips need an explicit, batch-specific confirmation.** Without it nothing moves
       and the counts are *shown*; with a signature that still matches, the sweep proceeds.
    """
    view = dict(view or {})
    if not view.get('entries'):
        gallery, counts = _audio_render(surface, view)
        return ("Nothing has been loaded to review yet.", view, gallery, counts, '', False, False)

    view = _audio_merge_payload(view, payload)
    plan = fork_audio_review.plan_sort(_audio_tiles(view), _audio_page_decisions(view),
                                       library, discarded)
    gallery, counts = _audio_render(surface, view)

    if not plan.can_sort:
        return (f"\u274c {AUDIO_CONFIRM_DISABLED_TEXT}", view, gallery, counts, '', False, False)

    verdict = fork_audio_review.validate_destinations(
        view.get('root', ''), library, discarded, app_root=app_root,
        need_library=plan.needs_library, need_discarded=plan.needs_discarded)
    if not verdict.ok:
        detail = f"\n{verdict.detail}" if verdict.detail else ''
        return (f"\u274c Nothing was moved \u2014 {fork_audio_review.refusal_text(verdict)}{detail}",
                view, gallery, counts, '', False, False)

    resolved_library, resolved_discarded = _audio_resolved_destinations(library, discarded, plan)

    if plan.has_unmarked and str(armed_signature or '') != plan.signature:
        # Show the consequence and STOP. Nothing has moved, and the operator is told exactly what
        # pressing Proceed would do, which is the whole point of the gate.
        preview = fork_audio_review.plan_preview_lines(plan, resolved_library, resolved_discarded)
        message = "\n".join([AUDIO_SWEEP_GATE_HEADING, ''] + preview
                            + ['', AUDIO_SWEEP_GATE_PROMPT])
        return (message, view, gallery, counts, plan.signature, True, False)

    message, refreshed = _audio_sort(surface, view, resolved_library, resolved_discarded,
                                     sweep_unmarked=plan.has_unmarked)
    gallery, counts = _audio_render(surface, refreshed)
    return (message, refreshed, gallery, counts, '', False, True)


# ---------------------------------------------------------------------------------------------
# Surface 1: the Raw Audio Cutter's review half
# ---------------------------------------------------------------------------------------------

def _on_audio_confirm(payload, library_folder, discarded_folder, view, armed):
    """Sort the cutter's page: KEEP into the voice library, everything else into quarantine.

    The gallery releases the player on `pointerdown` of this button, before Gradio sends the click.
    That is not decoration: Windows will not rename a file a player still holds open, and the clip
    the operator just listened to is exactly the one that would refuse to move.
    """
    message, view, gallery, counts, signature, gate, _ = _audio_confirm_core(
        _AUDIO_SURFACE_CUT, view, payload, library_folder, discarded_folder, armed,
        app_root=ROOT_DIR)
    return (message, gallery, counts, view, signature,
            gr.update(visible=gate), gr.update(visible=gate))


def _on_audio_proceed(payload, library_folder, discarded_folder, view, armed):
    """Proceed: the operator agreed to sweep the unmarked clips in THIS batch.

    It re-runs the whole of Confirm rather than acting on anything the gate computed. The signature
    is a change detector, not a credential: the plan is re-derived, the destinations are
    re-validated, and if any of it moved since the gate was shown the sweep is refused and the new
    counts are presented instead.
    """
    return _on_audio_confirm(payload, library_folder, discarded_folder, view, armed)


def _on_audio_cancel(view):
    """Cancel the sweep. Nothing has moved, nothing was decided on the operator's behalf, and the
    batch stays exactly as it was -- which is what makes agreeing to a sweep safe to try.

    It writes the **same seven outputs as Confirm**, including a repainted gallery. That is not
    symmetry for its own sake: the grid nonce is what makes a `gr.HTML` update deliverable, so a
    Cancel that changed no markup would not reach the browser and the hidden field would never be
    re-published from the server's state.
    """
    updated = dict(view or {})
    updated['armed'] = ''
    gallery, counts = _audio_render(_AUDIO_SURFACE_CUT, updated)
    return (AUDIO_SWEEP_CANCELLED_TEXT, gallery, counts, updated, '',
            gr.update(visible=False), gr.update(visible=False))


def _on_audio_destinations_change(library_folder, discarded_folder, view):
    """[R7.2 Phase 4] The live read-out beside the two destination boxes.

    Runs on every edit of either field, so the operator's reported problem -- both boxes pointing
    at the same folder -- is visible while they are still typing rather than only when Confirm
    refuses. **It validates and reports; it never sorts, never writes a folder and never touches
    the gallery**, so a half-typed configuration leaves the page fully reviewable.
    """
    _, message = fork_audio_review.destination_feedback(
        (view or {}).get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)
    return message


# ---------------------------------------------------------------------------------------------
# Surface 2: the standalone Audio Preview tab  [R7.2 Phase 2]
#
# The owner's reason for it, in their words: they often cut recordings but want to classify the
# results later. R7.1 could only review the clips the current browser session had just produced, so
# "later" meant "never" -- closing the tab lost the review surface even though every clip was still
# on disk. This tab reads a folder, so it works after a restart and on clips this app did not make.
# ---------------------------------------------------------------------------------------------

def _audio_preview_scan_report(found, view, page_size) -> str:
    """What the scan found, including what it deliberately did not enumerate."""
    total = int(view.get('total', 0))
    pages = fork_audio_review.page_count(total, page_size)
    lines = [
        f"Found {total} audio clip(s) under:\n{found.root}",
        f"Subfolders: {'included' if view.get('recursive') else 'excluded'}",
        f"Not reviewable (unsupported, empty or unreadable): {len(found.rejected)}",
        f"Showing page {view.get('page_index', 1)} of {pages} "
        f"({len(view.get('entries') or ())} on this page).",
        '',
        f"Playable formats: {', '.join(fork_audio_review.SERVABLE_SUFFIXES)}",
        "Read-only: lengths and names only. No file was moved, renamed or deleted.",
    ]
    return "\n".join(lines)


def _audio_preview_page_label(view) -> str:
    """`Page 2 of 7   ·   412 clips`, or an honest empty statement."""
    total = int(view.get('total', 0))
    if not total:
        return "No clips loaded."
    pages = fork_audio_review.page_count(total, view.get('page_size'))
    return (f"Page {view.get('page_index', 1)} of {pages}   ·   {total} clip(s)   ·   "
            f"{len(view.get('entries') or ())} on this page")


#: Every Audio Preview handler writes exactly these nine, in this order. Declared once so the
#: positional contract between the handlers and the wiring lives in one place.
_AUDIO_PREVIEW_OUTPUT_ORDER = (
    'report', 'gallery', 'counts', 'view', 'page_label', 'armed', 'proceed', 'cancel',
    'destinations')


def _audio_preview_outputs(report, view, dest_message, gate=False, signature=''):
    """The nine outputs every Audio Preview handler writes, assembled in one place.

    Assembling them here rather than at each `return` keeps the positional contract in one
    function: a handler that grew a tenth output would have to come through here to get it, which
    is the only way a nine-output `outputs=` list and six handlers stay in step.
    """
    gallery, counts = _audio_render(_AUDIO_SURFACE_PREVIEW, view)
    return (report, gallery, counts, view, _audio_preview_page_label(view), signature,
            gr.update(visible=gate), gr.update(visible=gate), dest_message)


def _on_audio_preview_scan(folder, recursive, page_size, library_folder, discarded_folder,
                           view, request: gr.Request = None):
    """Enumerate a folder of audio clips and publish its first page.

    **This is the only thing that walks the folder**, and it runs only when the operator presses
    Scan. Turning a page re-uses the list this captured, which is what keeps page numbering stable
    while a review is in progress; a sort re-enumerates, because files have genuinely moved.

    Decisions already made are carried forward **by file identity**, so re-scanning the same folder
    after sorting part of it does not forget what the operator decided about the rest.
    """
    previous = dict(view or {})
    found = fork_audio_review.discover_audio(folder, recursive)
    _, dest_message = fork_audio_review.destination_feedback(
        found.root or folder, library_folder, discarded_folder, app_root=ROOT_DIR)

    if found.error:
        empty = {'entries': (), 'identity': {}, 'decisions': {}, 'names': {},
                 'root': found.root, 'recursive': bool(recursive), 'total': 0, 'paths': [],
                 'page': '', 'session': '', 'page_index': 1,
                 'page_size': fork_audio_review.resolve_page_size(page_size), 'start': 0,
                 'spans': {}, 'durations': {}, 'armed': ''}
        return _audio_preview_outputs(f"❌ {found.error}", empty, dest_message)

    paths = [media.path for media in found.files]
    new_view, note = _audio_load_view(
        _AUDIO_SURFACE_PREVIEW, request, paths, found.root, recursive=bool(recursive),
        page_index=1, page_size=page_size,
        decisions=previous.get('decisions'), names=previous.get('names'))
    if not paths:
        return _audio_preview_outputs(
            "No audio clips found in that folder.\n"
            f"Playable formats: {', '.join(fork_audio_review.SERVABLE_SUFFIXES)}",
            new_view, dest_message)
    report = _audio_preview_scan_report(found, new_view, page_size) + note
    return _audio_preview_outputs(report, new_view, dest_message)


def _audio_preview_turn(payload, step, page_size, view, request):
    """Move `step` pages through the list this view already captured. `(report, view)`.

    The page's decisions are folded in **before** the page changes, which is the whole of "keep
    review decisions stable while changing pages": the record is keyed by file identity, so what
    the operator marked on page 1 is still marked when they come back to it.

    No folder is walked here. Re-enumerating on every page turn would renumber the pages under the
    operator whenever a file appeared or vanished, and it would charge a disk walk for a keystroke.
    """
    current = _audio_merge_payload(dict(view or {}), payload)
    paths = list(current.get('paths') or ())
    if not paths:
        return ("Nothing is loaded. Press Scan Folder first.", current)

    resolved_size = fork_audio_review.resolve_page_size(page_size)
    pages = fork_audio_review.page_count(len(paths), resolved_size)
    target = fork_audio_review.resolve_page(
        int(current.get('page_index', 1)) + int(step), len(paths), resolved_size)
    if target == int(current.get('page_index', 1)) and resolved_size == current.get('page_size'):
        edge = "first" if step < 0 else "last"
        return (f"Already on the {edge} page ({target} of {pages}).", current)

    turned, note = _audio_load_view(
        _AUDIO_SURFACE_PREVIEW, request, paths, current.get('root', ''),
        recursive=current.get('recursive', True), page_index=target, page_size=resolved_size,
        spans=current.get('spans'), decisions=current.get('decisions'),
        names=current.get('names'))
    return (f"Page {turned.get('page_index', 1)} of "
            f"{fork_audio_review.page_count(len(paths), resolved_size)}." + note, turned)


def _on_audio_preview_prev(payload, page_size, library_folder, discarded_folder, view,
                           request: gr.Request = None):
    report, turned = _audio_preview_turn(payload, -1, page_size, view, request)
    _, dest_message = fork_audio_review.destination_feedback(
        turned.get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)
    return _audio_preview_outputs(report, turned, dest_message)


def _on_audio_preview_next(payload, page_size, library_folder, discarded_folder, view,
                           request: gr.Request = None):
    report, turned = _audio_preview_turn(payload, 1, page_size, view, request)
    _, dest_message = fork_audio_review.destination_feedback(
        turned.get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)
    return _audio_preview_outputs(report, turned, dest_message)


def _on_audio_preview_confirm(payload, library_folder, discarded_folder, page_size, view, armed,
                              request: gr.Request = None):
    """Sort this page, then **re-scan** so the operator sees what is genuinely left.

    Re-scanning after a successful sort is Phase 2's explicit requirement and is also the only
    honest page to show: the clips that moved are no longer in the folder, and leaving them on
    screen would let a second Confirm report them as missing.
    """
    message, sorted_view, gallery, counts, signature, gate, did_sort = _audio_confirm_core(
        _AUDIO_SURFACE_PREVIEW, view, payload, library_folder, discarded_folder, armed,
        app_root=ROOT_DIR)
    _, dest_message = fork_audio_review.destination_feedback(
        sorted_view.get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)

    if not did_sort or not sorted_view.get('root'):
        # The gate is showing, or Confirm refused. Nothing moved, so nothing is re-enumerated and
        # the refusal keeps its own view and its own message.
        return _audio_preview_outputs(message, sorted_view, dest_message, gate=gate,
                                      signature=signature)

    # Re-enumerate. A sort genuinely changed the folder, so the cached path list is stale and the
    # page count may have shrunk.
    found = fork_audio_review.discover_audio(sorted_view.get('root'),
                                             sorted_view.get('recursive', True))
    if found.error:
        return _audio_preview_outputs(
            message + f"\n\n⚠️  The folder could not be re-read: {found.error}",
            sorted_view, dest_message)
    paths = [media.path for media in found.files]
    rescanned, note = _audio_load_view(
        _AUDIO_SURFACE_PREVIEW, request, paths, found.root,
        recursive=sorted_view.get('recursive', True),
        page_index=sorted_view.get('page_index', 1), page_size=page_size,
        decisions=sorted_view.get('decisions'), names=sorted_view.get('names'))
    remaining = (f"\n\n{len(paths)} clip(s) still in the review folder."
                 if paths else "\n\nThe review folder is now empty.")
    return _audio_preview_outputs(message + remaining + note, rescanned, dest_message)


def _on_audio_preview_proceed(payload, library_folder, discarded_folder, page_size, view, armed,
                              request: gr.Request = None):
    """Proceed with the sweep this batch was armed for. Re-derives everything; see
    `_on_audio_proceed` for why the signature is a change detector rather than a credential."""
    return _on_audio_preview_confirm(payload, library_folder, discarded_folder, page_size,
                                     view, armed, request)


def _on_audio_preview_cancel(library_folder, discarded_folder, view):
    """Cancel the sweep. Nothing moved, and every clip is exactly as the operator left it.

    It repaints the gallery rather than leaving it alone, because the grid nonce is what makes a
    `gr.HTML` update deliverable: a Cancel that produced byte-identical markup would not reach the
    browser at all, so the hidden field would never be re-published from the server's state.
    """
    updated = dict(view or {})
    updated['armed'] = ''
    _, dest_message = fork_audio_review.destination_feedback(
        updated.get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)
    return _audio_preview_outputs(AUDIO_SWEEP_CANCELLED_TEXT, updated, dest_message)


def _on_audio_preview_destinations_change(library_folder, discarded_folder, view):
    """The same live validation the cutter's review half gets, for the same reason."""
    _, message = fork_audio_review.destination_feedback(
        (view or {}).get('root', ''), library_folder, discarded_folder, app_root=ROOT_DIR)
    return message


def _on_audio_open_in_library(run_directory):
    """"Open results in Media Library": hand this cut's output folder to Audio Review.

    It only fills the folder box and selects the section. **It deliberately does not scan**,
    because Phase 2 requires that nothing scans a folder as a side effect -- the operator presses
    Scan when they are ready, and until then no disk is walked and no session is minted.

    [FORK] R8 UX Refresh (Commit A): Audio Review is now a SECTION of Media Library rather than a
    top-level tab, so reaching it takes **two** selections, not one -- the outer tab and the inner
    section. Returning only the inner one lands the operator on whichever top-level tab they were
    already on, with an invisible selection applied underneath; returning only the outer one shows
    Media Library on whatever section was last open. Both halves are therefore load-bearing, and a
    failure path must still return an update for each of the four outputs or Gradio mis-aligns them.
    """
    folder = str(run_directory or '').strip()
    if not folder:
        return (gr.update(), gr.update(), gr.update(),
                "Cut some clips first — there is no output folder to open yet.")
    return (gr.update(value=folder),
            gr.Tabs(selected='tab-media-library'),
            gr.Tabs(selected='tab-audio-review'),
            f"Loaded the folder into Media Library → Audio Review:\n{folder}\n"
            f"Press Scan Folder there when you are ready to review it.")


def _on_cut_open_in_library(output_folder):
    """"Open results in Media Library": hand the Raw Cutter's output folder to Video Review.

    The symmetric partner of :func:`_on_audio_open_in_library`, and it shares that function's two
    properties: it selects both the outer tab and the inner section, and **it scans nothing**. The
    operator presses Scan when they are ready; until then no disk is walked.

    What it hands over is the *parent* output folder with subfolders included, because every run
    writes its own fresh timestamped subfolder and the Raw Cutter keeps no run-directory state. So
    the message says exactly that rather than implying this is the one run's directory -- an
    operator who has cut three times will see all three, which is honest and usually what they
    want.

    Total by construction: a missing or blank folder is reported and changes nothing, and every
    return path yields one update per output so Gradio's positional alignment cannot drift.
    """
    folder = str(output_folder or '').strip()
    if not folder:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                "Set an output folder first — there is nothing to open yet.")
    return (gr.update(value=folder),
            gr.update(value=True),
            gr.Tabs(selected='tab-media-library'),
            gr.Tabs(selected='tab-video-review'),
            f"Loaded the Raw Cutter output folder into Media Library → Video Review:\n{folder}\n"
            f"Subfolders are included, because each cut writes its own timestamped subfolder.\n"
            f"Press Scan Folder there when you are ready to review.")


def _browse_audio_source(current):
    """The native file dialog for the one source recording.

    Identical contract to `_browse_folder`: cancel, timeout and any failure to show the dialog all
    leave the operator's existing value untouched, so Browse can only ever add a choice.
    """
    existing = current if isinstance(current, str) else ''
    try:
        status, chosen = fork_folders.pick_file(
            existing or _pref(fork_prefs.AUDIO_CUTTER_INPUT) or None,
            filter_text=fork_folders.file_filter('Audio and video',
                                                 fork_audio.SOURCE_SUFFIXES))
    except Exception:                                  # noqa: BLE001 - browsing is never fatal
        return gr.update()
    if status == fork_folders.PICKED and chosen:
        return chosen
    return gr.update()


def _clips_durations(entries) -> dict:
    """Real clip lengths for the tile labels, from the portable FFprobe.

    Cached inside `ensure_durations` on the clip's identity, so a Scan pays for the page once and
    every later Refresh is answered from memory. The command and the process launch both live in
    the fork core, exactly like the poster pass, because two suites pin this module's
    `subprocess.run` site count at two.

    A probe that cannot answer yields `None`, which renders `(–:–)`. Deliberately **not**
    `ffmpeg_processing.get_video_duration`, whose 10.0-second fallback would print a confident
    `(00:10)` over a clip nobody measured.
    """
    try:
        from ffmpeg_processing import FFPROBE_PATH    # local: no module-level import is added
        return fork_clips.ensure_durations(entries, FFPROBE_PATH,
                                           fork_clips.run_duration_command)
    except Exception:                                 # noqa: BLE001 - labels are never fatal
        return {}


def _on_clips_check(input_folder, library_folder, rejected_folder):
    """Read-only validation of the three roots. Creates no directory and reads no media."""
    verdict, roots = fork_clips.validate_roots(input_folder, library_folder, rejected_folder,
                                               app_root=ROOT_DIR)
    if not verdict.ok:
        return f"❌ REFUSED: {verdict.code}\n{verdict.detail}"
    lines = ["✅ Folders accepted. Nothing was created, read or moved.", ""]
    for role in (fork_clips.ROLE_INPUT, fork_clips.ROLE_LIBRARY, fork_clips.ROLE_REJECTED):
        root = roots[role]
        suffix = '   [link resolved]' if root.is_reparse else ''
        missing = '' if root.exists else '   [does not exist yet — not created]'
        lines.append(f"  {role:<9} -> {root.real}{suffix}{missing}")
    return "\n".join(lines)


def _clips_render_current(owner: str, report: str):
    """Render whatever the server currently holds for `owner`, with `report` as the message.

    [R5-P3 / F1] Used by Refresh *and* by a superseded scan. A superseded scan must not hand the
    browser the grid it built -- that page's session was revoked the moment the newer scan opened
    its own, so every tile in it would answer 403. Repainting the authoritative page instead means
    that whichever response the browser receives last, what it displays is current.
    """
    view = _clips_view(owner)
    if not view.get('entries'):
        return (report, CLIPS_GALLERY_INITIAL_HTML, CLIPS_COUNTS_INITIAL_TEXT,
                gr.update(interactive=False))
    # [R5-P11 / N7] Refresh is the operator's recovery route, so it must survive the condition it
    # is recovering from. A store that cannot completely recover now refuses rather than reporting
    # an empty record, and repainting this page from a record that could not be read would show
    # every decided clip as undecided -- the exact misreading the refusal exists to replace.
    try:
        store = _clips_store()
    except (fork_clips.PersistenceError, OSError) as failure:
        return _clips_unavailable_outputs(failure)
    identities = _clips_identities(view)
    counts = store.counts(identities)
    grid = fork_clips.gallery_html(view['entries'], store.snapshot_of(identities),
                                   view['identity'], view['session'], view['page'],
                                   _clips_poster_exists, _clips_durations(view['entries']))
    # [FORK] R8 Final Polish: Confirm is admitted by AT LEAST ONE KEPT CLIP, not by a page with
    # nothing undecided. That is the rule Audio Review has always used; the asymmetry was an
    # unfinished requirement rather than a deliberate difference -- an operator picking six good
    # shots out of two hundred had to click the other 194 before the button would light.
    #
    # Nothing about the authority changed: still derived from `store.counts()`, the durable
    # record, and this is still the only place that moves the button.
    return (report, grid, fork_clips.counts_text(counts),
            gr.update(interactive=counts[fork_clips.SELECTED] > 0))


def _on_clips_scan(input_folder, recursive, library_folder, rejected_folder, page_size,
                   request: gr.Request = None):
    """Validate, enumerate, freeze one page, mint opaque ids and build the grid.

    Order matters twice over.

    The roots are refused **before** anything is read, so a destination that overlaps the input
    never reaches the filesystem.

    And [R5-P3 / F1] the scan now runs under an explicit per-owner generation:

    1. refusals that change nothing happen first, so a scan rejected for a bad folder or an empty
       enumeration never takes a generation and therefore cannot supersede a running scan;
    2. `begin()` takes the generation -- from here on this scan is visible as "the newest", even
       though it has not finished;
    3. the authority-changing calls (`begin_scan`, `new_session`, `publish_page`) run inside
       `authority(...)`, which re-checks the generation **while holding the owner's lock**;
    4. the slow poster pass runs **outside** that lock, and takes no render lock;
    5. `publish()` installs the view only if this generation is still the newest;
    6. [R5-P9 / N1] the response itself is then rendered from the **authoritative view**, not from
       this scan's locals, so a scan superseded after it published repaints what is current
       instead of returning markup whose session has already been revoked;
    7. [R5-P9 / N5] the optional journal compaction cannot destroy a scan that has succeeded.

    A superseded scan therefore changes nothing and repaints the authoritative page; a newest scan
    that *fails* discards the view rather than leaving an older revoked page looking current.
    """
    empty = (CLIPS_GALLERY_INITIAL_HTML, CLIPS_COUNTS_INITIAL_TEXT)
    try:
        owner = _clips_owner(request)
    except fork_clips.Refusal as refusal:
        return (f"❌ No browser session identity ({refusal.reason}). Reload the page.",
                empty[0], empty[1], gr.update(interactive=False))

    verdict, roots = fork_clips.validate_roots(input_folder, library_folder, rejected_folder,
                                               app_root=ROOT_DIR)
    if not verdict.ok:
        return (f"❌ REFUSED before reading anything: {verdict.code}\n{verdict.detail}",
                empty[0], empty[1], gr.update(interactive=False))

    found = fork_clips.discover_clips(input_folder, recursive)
    if found.error:
        return (f"❌ {found.error}", empty[0], empty[1], gr.update(interactive=False))
    if not found.files:
        return ("No reviewable clips found (0 enumerated).\n"
                "Supported: " + ", ".join(fork_clips.SERVABLE_SUFFIXES),
                empty[0], empty[1], gr.update(interactive=False))

    try:
        size = max(1, min(int(page_size), fork_clips.MAX_CLIPS_PER_PAGE))
    except (TypeError, ValueError):
        size = 100

    # [R5-P11 / N7] The durable record is obtained BEFORE a generation is taken, and that ordering
    # is the whole containment. A refused recovery therefore changes nothing at all: no generation
    # is minted, so no running scan is superseded and no current page is blanked; `begin_scan` and
    # `new_session` are never reached, so no review session is opened against a record the server
    # could not read; and the poster pass never starts. The operator is told what happened instead
    # of being handed a folder that looks entirely undecided.
    try:
        store = _clips_store()
    except (fork_clips.PersistenceError, OSError) as failure:
        return _clips_unavailable_outputs(failure)
    scan_id = secrets.token_urlsafe(12)
    generation = _CLIPS_SCANS.begin(owner)
    try:
        # Outside the lock: one stat per file, and it touches no authoritative state.
        frozen = fork_clips.freeze_page(found.files, 0, size)
        with _CLIPS_SCANS.authority(owner, generation):
            store.begin_scan(owner, scan_id)
            session_id = _CLIPS_REGISTRY.new_session(owner, scan_id)
            page_token, entries = _CLIPS_REGISTRY.publish_page(owner, session_id, 1, frozen)

        from ffmpeg_processing import FFMPEG_PATH      # local: no module-level import is added
        # The runner lives in the fork core, not here: two suites pin this module's
        # `subprocess.run` site count at exactly two (the delivery preview and the Director), and
        # that invariant keeps process launching out of the view layer.
        posters = fork_clips.ensure_posters(entries, CLIPS_POSTER_DIR, FFMPEG_PATH,
                                            fork_clips.run_poster_command)

        identity = {entry.clip_id: fork_clips.decision_identity(entry.path, entry.size,
                                                                entry.mtime_ns)
                    for entry in entries}
        view = {'session': session_id, 'page': page_token, 'scan_id': scan_id,
                'entries': entries, 'identity': identity, 'total': len(found.files),
                'root': found.root, 'page_size': size,
                # [UX V3] Confirm sorts into these, and re-validates them before it moves
                # anything -- it never trusts a folder the browser re-sends. `recursive` is kept
                # so a completed batch can enumerate the next one exactly as this scan did.
                'recursive': bool(recursive),
                'library': roots[fork_clips.ROLE_LIBRARY].real,
                'rejected': roots[fork_clips.ROLE_REJECTED].real}
        if not _clips_remember(owner, generation, view):
            raise fork_clips.SupersededScan(generation, _CLIPS_SCANS.newest(owner))
    except fork_clips.SupersededScan:
        return _clips_render_current(owner, CLIPS_SCAN_SUPERSEDED_TEXT)
    except (fork_clips.Refusal, fork_clips.PersistenceError, OSError) as failure:
        # A failed NEWEST scan must leave an honest empty state. Resurrecting the previous page
        # would present a revoked session as current.
        _CLIPS_SCANS.discard(owner, generation)
        return (f"❌ The scan failed and nothing is loaded: {_clips_failure_text(failure)}",
                empty[0], empty[1], gr.update(interactive=False))

    # [R5-P9 / N5] Compaction is an optimisation over the journal, never the record itself, and
    # it runs *after* this scan has already published a valid page and minted live credentials.
    # Letting its failure escape therefore destroyed a scan that had entirely succeeded: the
    # operator lost the gallery they were already entitled to, for a failure that costs nothing.
    # The journal stays authoritative and every committed decision remains recoverable, so the
    # page is served and the failure is reported honestly instead. Only the two I/O classes are
    # contained -- a `TypeError` from a programming mistake in `compact()` must still surface.
    compaction_notice = ''
    try:
        store.maybe_compact()
    except (fork_clips.PersistenceError, OSError) as failure:
        # [R5-P11] The reassurance here used to say every decision already made "is still in the
        # journal". That overstates what the storage contract guarantees, and an independent review
        # found it inaccurate: after a *successful* compaction the snapshot is the authoritative
        # copy and the journal is empty, so the journal alone is not the record. What is true, and
        # is what matters to the operator, is that compaction only ever runs against a completely
        # recovered record and that a failed one destroys neither copy -- either the snapshot has
        # not been replaced yet, or it has landed and the journal still replays onto it
        # idempotently. Promise the recoverability, not the location.
        compaction_notice = (
            "\n⚠️  Journal housekeeping (compaction) failed and was skipped "
            f"({_clips_failure_text(failure)}). Your decisions are safe: every decision already "
            "made stays recoverable from the snapshot plus the journal, and a failed compaction "
            "discards neither.")

    # [R5-P9 / N1] Render from the authoritative view, never from this scan's own locals.
    # Publication succeeded above, but the poster pass and the compaction below both take real
    # time, and a second scan can supersede this one in between -- after which `entries`,
    # `identity`, `session_id` and `page_token` name a session `new_session` has already revoked.
    # Handing that markup to the browser is the F1 symptom returning by a later route: every tile
    # answers 403. Deriving the response from `_clips_view` means a superseded scan repaints
    # whatever is current instead, so whichever response the browser receives last is live.
    current = _clips_view(owner)
    if current.get('generation') != generation:
        return _clips_render_current(owner, CLIPS_SCAN_SUPERSEDED_TEXT)

    identities = _clips_identities(current)
    counts = store.counts(identities)
    grid = fork_clips.gallery_html(current['entries'], store.snapshot_of(identities),
                                   current['identity'], current['session'], current['page'],
                                   _clips_poster_exists, _clips_durations(current['entries']))
    report = (
        f"Enumerated {len(found.files)} clip(s) under:\n{found.root}\n"
        f"Subfolders: {'included' if recursive else 'excluded'}\n"
        f"Not reviewable (unsupported, empty or unreadable): {len(found.rejected)}\n"
        f"This page shows {len(entries)} of {len(found.files)}.\n"
        f"scan {found.scan_seconds:.3f}s · posters {posters.seconds:.3f}s "
        f"(cached {posters.hits}, made {posters.generated}, "
        f"no frame {posters.failures}, {posters.workers} worker(s))\n"
        "Read-only: metadata and one poster frame per clip. No file was moved, renamed or deleted."
    ) + compaction_notice
    # [FORK] R8 Final Polish: Confirm is admitted by AT LEAST ONE KEPT CLIP, not by a page with
    # nothing undecided. That is the rule Audio Review has always used; the asymmetry was an
    # unfinished requirement rather than a deliberate difference -- an operator picking six good
    # shots out of two hundred had to click the other 194 before the button would light.
    #
    # Nothing about the authority changed: still derived from `store.counts()`, the durable
    # record, and this is still the only place that moves the button.
    return (report, grid, fork_clips.counts_text(counts),
            gr.update(interactive=counts[fork_clips.SELECTED] > 0))


def _clips_decide(owner, session_id, page_token, entry, payload):
    """The ONE decision entry point, called by the media server for POST /r5decide.

    Everything is revalidated here. The browser supplied an opaque id and a desired state; it did
    not name a file, and any `path` it put in the body is ignored.
    """
    view = _clips_view(owner)
    if view.get('session') != session_id or view.get('page') != page_token:
        return {'ok': False, 'code': 'not_the_active_page'}
    target = payload.get('to')
    if not isinstance(target, str) or target not in fork_clips.DECISIONS:
        return {'ok': False, 'code': 'illegal_transition'}
    identity = view['identity'].get(entry.clip_id)
    if identity is None:
        return {'ok': False, 'code': 'clip_not_on_page'}

    # [R5-P11 / N7] Deliberately NOT contained here. `_clips_store` raises `PersistenceError` when
    # the durable record could not be completely recovered, and the middleware already answers that
    # class with 503 `not_durably_recorded` and a path-free body -- which is the correct answer for
    # a classification that cannot be read or written. Catching it to return a JSON `ok: False`
    # would make an unreadable record indistinguishable from an ordinary refusal, and catching it
    # to return `ok: True` would be the false success the whole durability contract forbids.
    store = _clips_store()
    identities = _clips_identities(view)

    def refuse(code):
        """A refusal that discloses the server's current record to the already-authorized page.

        Nothing is appended and no revision moves: this returns before `store.decide` is reached.
        """
        current, revision = store.get(identity)
        return {'ok': False, 'code': code, 'clip': entry.clip_id, 'decision': current,
                'rev': revision, 'counts': store.counts(identities)}

    # [R5-P3 / F2] The expected revision is MANDATORY and strictly typed. The previous code did
    # `int(payload.get('rev'))` inside a try/except that assigned `None`, so a missing, null,
    # string, bool, float, list or dict revision all became "no concurrency check requested" --
    # a fail-open, and one reachable from the shipped client, because `parseInt` of a tampered
    # non-numeric `data-rev` is `NaN` and `JSON.stringify(NaN)` is `null`. Validation now happens
    # before anything is written, and a bad revision is refused with its own stable code.
    try:
        expected = fork_clips.parse_expected_revision(payload)
    except fork_clips.RevisionError as bad:
        return refuse(bad.code)

    # Identity is revalidated on the OPENED DESCRIPTOR, so a path repointed between enumeration
    # and now is caught rather than inherited.
    if not _CLIPS_REGISTRY.live_identity_ok(entry):
        return refuse('identity_changed')
    try:
        decision, revision = store.decide(owner, identity, target, view['scan_id'], expected)
    except fork_clips.StaleWrite as stale:
        return {'ok': False, 'code': stale.code, 'clip': entry.clip_id,
                'decision': stale.current, 'rev': stale.revision,
                'counts': store.counts(identities)}
    return {'ok': True, 'clip': entry.clip_id, 'decision': decision, 'rev': revision,
            'counts': store.counts(identities)}


def _clips_state(owner, session_id, page_token):
    """The authoritative record for one page, for GET /r5state.

    [R5-P11 / N7] A `PersistenceError` from `_clips_store` is left to propagate for the same
    reason as in `_clips_decide`: this route's entire purpose is to be believed, so an unreadable
    record must answer 503 rather than report every clip as `PENDING` with `ok: True`.
    """
    view = _clips_view(owner)
    if view.get('session') != session_id or view.get('page') != page_token:
        return {'ok': False, 'code': 'not_the_active_page'}
    store = _clips_store()
    identities = _clips_identities(view)
    snapshot = store.snapshot_of(identities)
    return {
        'ok': True,
        'counts': store.counts(identities),
        'clips': {entry.clip_id: {
            'decision': snapshot[view['identity'][entry.clip_id]][0],
            'rev': snapshot[view['identity'][entry.clip_id]][1],
        } for entry in view['entries']},
    }


def _on_clips_refresh(request: gr.Request = None):
    """Re-render the grid from the server's record. This is the proof that the DOM is not the
    decision: whatever the page currently shows, this repaints it to what the server holds.

    [R5-P3 / F8] This is also the only thing that moves the Confirm button, and it derives that
    state solely from `store.counts()` -- the durable record. So a clip put back to PENDING
    disables Confirm again on the next Refresh, and no amount of DOM editing can enable it.
    """
    try:
        owner = _clips_owner(request)
    except fork_clips.Refusal:
        return (CLIPS_GALLERY_INITIAL_HTML, CLIPS_COUNTS_INITIAL_TEXT,
                gr.update(interactive=False))
    return _clips_render_current(owner, '')[1:]


def _clips_confirm_only(message: str, gate=False, signature=''):
    """A Confirm answer that changes no page state: repaint nothing, say this.

    [FORK] R8 Final Polish: two trailing outputs. `gate` reveals Proceed/Cancel and `signature`
    is the batch those buttons are attached to -- both default to the pre-R8 values, so every
    existing refusal through this helper still hides the gate and clears the arming, which is the
    behaviour a refusal must have.
    """
    return (gr.update(), gr.update(), gr.update(), gr.update(), message,
            gr.update(visible=gate), gr.update(visible=gate), signature)


def _on_clips_confirm(armed='', request: gr.Request = None):
    """Sort this page: SELECTED to the Media Library, REJECTED to the Discarded folder.

    [UX V3] Confirm now does the thing its label promises. What it moves comes entirely from the
    server: the entries are the registry's frozen page and each decision is read from the durable
    store, so a repainted tile, an edited class or anything else the browser could send has no
    say in where a file goes.

    The destinations are the ones the operator typed at Scan time, carried on the view and
    **re-validated here** rather than re-read from the widgets -- otherwise the folder that was
    checked and the folder that is written could differ.

    [FORK] R8 Final Polish -- KEEP-ONLY CLASSIFICATION, matching Audio Review.

    `armed` is the batch signature the operator last agreed to, and it is the ONLY widget value
    this handler takes. That is deliberate and preserves the property the no-inputs version had:
    it is a **change detector, not a credential**. Presenting it authorises nothing, because the
    plan is re-derived from the server's snapshot and the destinations are re-validated on every
    press -- a stale or forged signature simply fails to match a freshly computed one, and the
    gate is shown again.

    The admission rule is now "at least one clip is KEPT" rather than "nothing is undecided", and
    unmarked clips are swept to Discarded **only** after an explicit Proceed on that exact batch.
    The durable record is never written by the sweep: see `decision_of` below.

    Refusals, each of which moves nothing: no identity, nothing scanned, an unreadable record, a
    page with no clip kept, or destinations that no longer validate.

    Every move is a single same-volume ``os.rename`` (see `sort_decided_clips`): no overwrite, no
    collision renaming, no deletion, and a cross-volume request is refused in words rather than
    turned into a copy. One clip failing leaves the rest moved and is reported as a partial
    result. The page only advances to the next batch when every clip on this one actually moved.
    """
    try:
        owner = _clips_owner(request)
    except fork_clips.Refusal:
        return _clips_confirm_only("Reload the page: this browser session has no identity.")
    view = _clips_view(owner)
    if not view.get('entries'):
        return _clips_confirm_only("Nothing has been scanned on this page.")
    # [R5-P11 / N7] A readiness report derived from a record that could not be read would be a
    # false negative with the authority of a positive statement -- "0 kept, 0 discarded" reads as
    # a reviewed page rather than an unreadable one. Refuse to report instead.
    try:
        store = _clips_store()
    except (fork_clips.PersistenceError, OSError) as failure:
        return _clips_confirm_only(_clips_state_unavailable_text(failure))
    identities = _clips_identities(view)
    snapshot = store.snapshot_of(identities)

    # [FORK] R8 Final Polish: keep-only classification, the same two-step gate Audio Review uses.
    #
    # The plan comes from `fork_clips.plan_sort` -- the ONE shared model, over the server's own
    # snapshot, never the browser's. `can_sort` is "at least one clip is kept"; that replaces the
    # old "nothing may be undecided" admission rule.
    def _decision(entry):
        return snapshot.get(view['identity'][entry.clip_id], (fork_clips.PENDING, 0))[0]

    plan = fork_clips.plan_sort(
        view['entries'], {entry.clip_id: _decision(entry) for entry in view['entries']},
        view.get('library', ''), view.get('rejected', ''))

    if not plan.can_sort:
        return _clips_confirm_only(
            f"❌ {CLIPS_CONFIRM_DISABLED_TEXT}\n\n"
            f"{len(plan.unmarked)} of {plan.total} clip(s) are still undecided.")

    verdict, roots = fork_clips.validate_roots(
        view['root'], view.get('library', ''), view.get('rejected', ''), app_root=ROOT_DIR)
    if not verdict.ok:
        return _clips_confirm_only(
            f"❌ Nothing was moved: the destination folders no longer validate.\n"
            f"{verdict.code}\n{verdict.detail}")

    # Unmarked clips need an explicit, batch-specific confirmation. Without a signature that
    # still matches, NOTHING MOVES: the counts and the consequence are shown and Proceed/Cancel
    # are revealed. The signature covers every clip id, its decision and both destinations, so
    # changing any of them voids the arming and the gate is shown again.
    if plan.has_unmarked and str(armed or '') != plan.signature:
        preview = fork_clips.plan_preview_lines(
            plan, roots[fork_clips.ROLE_LIBRARY].real, roots[fork_clips.ROLE_REJECTED].real)
        message = "\n".join([CLIPS_SWEEP_GATE_HEADING, ''] + preview
                            + ['', CLIPS_SWEEP_GATE_PROMPT])
        return _clips_confirm_only(message, gate=True, signature=plan.signature)

    def decision_of(entry):
        """What this clip is filed as **for this batch only**.

        An unmarked clip is treated as REJECTED when, and only when, the operator has just armed
        this exact batch through the gate above. **The durable record is not written**: the clip
        stays PENDING in the store, so the sweep is a property of one confirmed batch rather than
        a decision retroactively attributed to the operator. Re-scan the folder and it is undecided
        again.
        """
        decision = _decision(entry)
        if decision == fork_clips.PENDING and plan.has_unmarked:
            return fork_clips.REJECTED
        return decision

    outcomes = fork_clips.sort_decided_clips(
        view['entries'], decision_of,
        {fork_clips.SELECTED: roots[fork_clips.ROLE_LIBRARY].real,
         fork_clips.REJECTED: roots[fork_clips.ROLE_REJECTED].real},
        # [FORK] R8: the operator's saved collision policy, read from the preference store rather
        # than threaded in as a widget. Deliberate: everything that decides WHERE a file goes
        # comes from the server, and the one widget value this handler does take (`armed`) is a
        # change detector that cannot name a folder.
        _collision_policy())
    message = _clips_sorted_text(outcomes, roots)
    if plan.has_unmarked:
        message = (f"{message}\n\n{len(plan.unmarked)} unmarked clip(s) were moved to the "
                   f"Discarded folder as agreed. Nothing was deleted.")

    # Re-enumerate whichever way it went. On a clean batch that publishes the next one; on a
    # partial batch it republishes what is genuinely still on disk, which is the only honest
    # page to show -- repainting the old one would leave already-moved clips on screen, and
    # pressing Confirm again would then report them as missing. Clips that stayed keep their
    # decisions, because a decision is keyed on the file's identity and that has not changed.
    report, grid, counts_text, confirm = _on_clips_scan(
        view['root'], view.get('recursive', True), view.get('library', ''),
        view.get('rejected', ''), view.get('page_size', 100), request)
    # The gate closes and the arming is cleared: this batch is spent, and a signature that
    # outlived its batch is exactly what the signature exists to prevent.
    return (report, grid, counts_text, confirm, message,
            gr.update(visible=False), gr.update(visible=False), '')


def _on_clips_sweep_cancel(request: gr.Request = None):
    """Back out of the sweep gate. Changes nothing at all.

    [FORK] R8 Final Polish. It does not repaint the grid, does not touch a decision and does not
    move a file -- it closes the gate and clears the arming, so the next Confirm starts the
    two-step agreement again from the beginning. "Cancel changes nothing" is the whole contract,
    and it is kept by this function doing almost nothing.
    """
    return _clips_confirm_only(CLIPS_SWEEP_CANCELLED_TEXT)


def _clips_sorted_text(outcomes, roots) -> str:
    """What actually happened, per clip, in the operator's words rather than status codes."""
    moved = [o for o in outcomes if o.ok]
    kept = [o for o in moved if o.decision == fork_clips.SELECTED]
    binned = [o for o in moved if o.decision == fork_clips.REJECTED]
    stayed = [o for o in outcomes if not o.ok]

    lines = []
    if moved and not stayed:
        lines.append(f"✅ Sorted all {len(moved)} clip(s).")
    elif moved:
        lines.append(f"⚠️ Sorted {len(moved)} of {len(outcomes)} clip(s). "
                     f"{len(stayed)} stayed where they were.")
    else:
        lines.append("❌ Nothing was moved.")
    lines.append("")
    lines.append(f"Media Library ({roots[fork_clips.ROLE_LIBRARY].real}): {len(kept)} kept")
    lines.append(f"Discarded ({roots[fork_clips.ROLE_REJECTED].real}): {len(binned)} discarded")
    if stayed:
        lines.append("")
        lines.append("Left in place:")
        for outcome in stayed:
            lines.append(f"  {outcome.label} — {CLIPS_MOVE_REASONS.get(outcome.status, outcome.status)}"
                         f"{(': ' + outcome.detail) if outcome.detail else ''}")
        lines.append("")
        lines.append("Nothing was overwritten, renamed or deleted. Fix the listed clips and "
                     "press Confirm again.")
    return "\n".join(lines)


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
    
    app = gr.Blocks(title='BeatSync Engine', theme='ocean',
                    css=STATUS_BOX_CSS + CUT_CANCEL_CSS + CLIPS_GALLERY_CSS)
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
        # [FORK] Digital-Union (AI Director): the last generated proposal awaiting an explicit
        # Apply, and nothing else. Exactly ONE reader — `apply_director_btn.click` — and it is
        # absent from `process_btn.click`, `source_outputs`, `prep_outputs`, `live_declaration`,
        # `CreativeProfile`, `beat_info["creative"]` and both mix configs.
        #
        # Its value is a frozen `DirectorProposal`, and V2 widened it: a BASE `CreativeRecipe` and
        # a FINAL `CreativeRecipe` (seven ints each, sharing the one minted seed), the validated
        # `SemanticIntent` as a tuple of frozen axis records, the bounded explanation and
        # instruction strings, an optional plain `MediaAdjustment` (four numbers and two strings)
        # and a short media note. Every reachable value is an int, float, str or a frozen record of
        # those, which is a contract rather than a nicety: Gradio deep-copies state, so a cache
        # record, a `PreparedMediaSummary` holding candidate data, a model object, a process handle
        # or anything carrying `MappingProxyType` could not live here. A test walks the whole object
        # graph and asserts `copy.deepcopy(proposal) == proposal`.
        director_proposal_state = gr.State(None)

        gr.Markdown(f"# {UI_TITLE}")
        gr.Markdown(UI_MAIN_DESCRIPTION)

        # [FORK] Digital-Union (GUI Refresh A): three tabs instead of one long column.
        #
        # Pure re-parenting. Every component keeps its identity, its `elem_id` and its
        # handlers; only the enclosing containers changed. A widget's tab has no bearing on
        # which handler reads it, so the live values still reach the same render events and
        # no execution setting is duplicated -- there is still exactly ONE of each control.
        #
        # Why it was needed: the render workflow shared one scrolling column with six
        # collapsed accordions, so Create Music Video sat far below the fold and the two
        # newest features (Random Freestyle and Shot Variety) were the easiest to miss.
        # [FORK] R7.2: named so "Open results in Audio Preview" can select another tab. Naming the
        # container is the whole of that capability -- `gr.Tabs(selected=...)` as a return value
        # switches tabs, and nothing else about the tab set changes.
        with gr.Tabs() as main_tabs:
            with gr.Tab(LABEL_TAB_RENDER, id='tab-render') as tab_render:
                with gr.Row():
                    # [FORK] Digital-Union (GUI Refresh A): 3:2 at the top level -- the controls
                    # need the width, the preview needs enough to be watchable.
                    with gr.Column(scale=3):
                        # Two sub-columns rather than one tall one, and the reason is measured:
                        # stacked, this content ran to 1980 px and put Create Music Video at
                        # y=1714, still below the fold on a 1000 px viewport. The audio picker
                        # and the Video Source group alone are 877 px of that, so tightening the
                        # settings could never have recovered it -- declaring the inputs and
                        # choosing the settings had to sit side by side. Side by side, the whole
                        # render workflow including the button fits without scrolling.
                        with gr.Row():
                            # Wider: it holds a drop zone and a file path.
                            with gr.Column(scale=3):
                                gr.Markdown('### 📁 Input Files')
                                # [FORK] R8 Final Polish: the music track, in a third of the
                                # height. `height=` bounds the drop zone instead of letting it
                                # claim the default ~190 px for a single-file control -- measured
                                # as the largest block above Create Music Video that was not a
                                # setting. Drag-and-drop is unchanged, and so are the component,
                                # its `elem_id`, its `type` and every handler that names it.
                                #
                                # A native Browse button was ALSO built here and reverted: a
                                # `gr.File` refuses to display a path outside Gradio's allowed
                                # roots, and the three ways around that each cost more than the
                                # button is worth. The measurement, the exact exception and the
                                # rejected options are in `tests/test_render_music_input.py`.
                                audio_input = gr.File(label=LABEL_AUDIO_FILE, file_types=['.mp3', '.wav', '.flac'], type='filepath', height=110, elem_id='audio-file-input')
                                # [FORK] Digital-Union: local-folder mode + authoritative confirmation gate.
                                with gr.Group():
                                    # [FORK] R8 Phase 10: the group heading names the control,
                                    # so the radio below carries `show_label=False` rather than
                                    # repeating the same words on a second line under it. The
                                    # label CONSTANT is kept and still passed, so assistive
                                    # technology and every test that reads it are unaffected.
                                    gr.Markdown('### 🎬 Video Source')
                                    source_mode = gr.Radio(
                                        choices=[
                                            (CHOICE_SOURCE_LOCAL_FOLDER, SourceMode.LOCAL_FOLDER.value),
                                            # [FORK] R8: the configured Media Library as a local
                                            # folder. Not a third SourceMode -- see
                                            # `SOURCE_CHOICE_MEDIA_LIBRARY`.
                                            (CHOICE_SOURCE_MEDIA_LIBRARY, SOURCE_CHOICE_MEDIA_LIBRARY),
                                            (CHOICE_SOURCE_BROWSER_FILES, SourceMode.BROWSER_FILES.value),
                                        ],
                                        value=SourceMode.LOCAL_FOLDER.value,
                                        label=LABEL_SOURCE_MODE,
                                        show_label=False,
                                        info=INFO_SOURCE_MODE,
                                        elem_id='source-mode-radio',
                                    )

                                    with gr.Group(visible=True) as folder_group:
                                        source_folder = gr.Textbox(
                                            label=LABEL_SOURCE_FOLDER,
                                            placeholder=PLACEHOLDER_SOURCE_FOLDER,
                                            info=INFO_SOURCE_FOLDER,
                                            value=_pref(fork_prefs.QUICK_RENDER_SOURCE),
                                            elem_id='source-folder-input',
                                        )
                                        source_folder_browse = gr.Button(
                                            LABEL_BROWSE_FOLDER, variant='secondary', size='sm',
                                            elem_id='source-folder-browse')
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
                            with gr.Column(scale=2):
                                with gr.Group():
                                    # [FORK] R8 Final Polish: the "### Video Settings" heading
                                    # that used to sit here is gone. The Group holds exactly one
                                    # control, and that control's own label already reads
                                    # "Output Video Profile" -- so the heading was a second line
                                    # of chrome naming the same thing, directly above the fold
                                    # that Create Music Video has to stay on. Same for the
                                    # "Output Settings" heading below.
                                    # [FORK] R8 UX Refresh (Phase 6): the Output Video Profile
                                    # REPLACES the free-text "Custom FPS" box, in the same
                                    # position and at the same index in every handler signature
                                    # it feeds -- so the positional alignments several suites pin
                                    # are preserved by arity rather than by renumbering.
                                    #
                                    # It answers the question the FPS box could not: the render
                                    # took its RESOLUTION from the first source video and fitted
                                    # every other clip to it with a bare `scale=w:h`, so a
                                    # portrait, 4:3 or anamorphic source was stretched. Auto is
                                    # first and therefore default, and Auto is the untouched
                                    # historical path including that behaviour -- see
                                    # `beatsync_fork/render_profiles.py` for why it is not
                                    # "fixed" unconditionally.
                                    output_profile = gr.Radio(
                                        choices=fork_render_profiles.profile_choices(),
                                        value=fork_render_profiles.AUTO,
                                        label=LABEL_OUTPUT_PROFILE,
                                        info=INFO_OUTPUT_PROFILE,
                                        elem_id='output-profile-radio',
                                    )

                                with gr.Group():
                                    gr.Markdown(f'### 🎬 Processing Mode')
                                    if NVENC_AVAILABLE:
                                        processing_mode = gr.Radio(choices=[('NVIDIA NVENC H.264', 'h264_nvenc'), ('NVIDIA NVENC HEVC (H.265)', 'hevc_nvenc'), ('CPU (H.264)', 'cpu'), ('ProRes 422 Proxy (Precise Mode)', 'prores_proxy')], value='h264_nvenc', label=LABEL_PROCESSING_MODE, show_label=False, info=get_processing_mode_info_nvenc())
                                    else:
                                        processing_mode = gr.Radio(choices=[('CPU (H.264)', 'cpu'), ('ProRes 422 Proxy (Precise Mode)', 'prores_proxy')], value='cpu', label=LABEL_PROCESSING_MODE, show_label=False, info=get_processing_mode_info_cpu())
                                with gr.Group():
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

                                # [FORK] Digital-Union (GUI Refresh A): a read-only note saying which
                                # optional settings from the other tabs will affect the next render.
                                # Derived from the live widgets each time this tab is selected, so it
                                # cannot go stale and is not a second settings authority -- it writes
                                # nothing and no render path reads it.
                                #
                                # It trails the buttons rather than heading this column, and that was
                                # measured rather than assumed: heading the column pushed Create Music
                                # Video from y=1032 down to y=1177, because this box is 145 px tall and
                                # the button sits below it either way.
                                #
                                # [FORK] R8 UX Refresh (Commit C): this replaces `active_settings_summary`
                                # and answers a different question.
                                #
                                # The old box listed optional extras but said nothing about the six
                                # creative controls, because the Creative Preset radio sat on this very
                                # tab and stated that part itself. The preset has moved to Creative
                                # Settings, and more importantly the pair was *misleading together*: the
                                # selector could read `Balanced` while Freestyle rules, a Variation Seed
                                # and SFX were all armed, and nothing on screen said the render would not
                                # be a plain Balanced render.
                                #
                                # So this box now carries ONE resolved line -- the base recipe plus every
                                # active override -- built by `beatsync_fork.effective_profile`. It is
                                # still a read-out: it writes nothing, no render path reads it, and it is
                                # recomputed from the live widgets on every tab selection, which is
                                # sufficient *because* every control it describes now lives on another
                                # tab. That was not true before the preset moved, and is exactly why the
                                # six sliders could not be reported from here until now.
                                creative_profile_summary = gr.Textbox(
                                    label=LABEL_CREATIVE_PROFILE,
                                    value=CREATIVE_PROFILE_PLACEHOLDER,
                                    lines=2,
                                    max_lines=6,
                                    interactive=False,
                                    elem_id='creative-profile-summary',
                                )
                                # A one-click way to go and change it, which the old layout lacked:
                                # the summary named settings on other tabs without offering a route
                                # to them. Selecting a tab is not a render and writes no setting.
                                open_creative_settings_btn = gr.Button(
                                    LABEL_OPEN_CREATIVE_SETTINGS, variant='secondary', size='sm',
                                    elem_id='open-creative-settings-button')
                    with gr.Column(scale=2):
                        gr.Markdown('### 📺 Output')
                        status_output = gr.Textbox(label='Status', interactive=False, value=get_ready_status(python_status, cuda_status, MAX_THREADS, CPU_COUNT, ffmpeg_status, GPU_AVAILABLE, gpu_info, NVENC_AVAILABLE), lines=4, max_lines=4, elem_id='status-output-box')
                        video_output = gr.Video(label='Generated Music Video', interactive=False, elem_id='generated-video-output')
                        # [FORK] Digital-Union (Deliverable Export V1-B-S2): directly below the preview,
                        # because the two answer the same question in sequence -- "did it work" then
                        # "where is it". `secondary`, because it is not an action on the render.
                        #
                        # Like `cancel_render_btn` and unlike `process_btn`, this is deliberately ALWAYS
                        # interactive: it is never gated by the source confirmation and never disabled
                        # while a render runs. The folder is a fixed property of the installation, so
                        # there is no state in which pointing at it would be wrong, and it is absent from
                        # `source_outputs` and `prep_outputs` so no source or preparation event can
                        # disable it.
                        open_output_folder_btn = gr.Button(
                            LABEL_OPEN_OUTPUT_FOLDER, variant='secondary', size='sm',
                            elem_id='open-output-folder-button',
                        )
                        gr.Markdown(INFO_OPEN_OUTPUT_FOLDER)

            with gr.Tab(LABEL_TAB_CREATIVE_SETTINGS, id='tab-creative-settings'):
                # [FORK] Digital-Union (Phase A + Creative Controls Core): creative direction.
                # Deliberately outside the Video Source group and never wired into `source_outputs`,
                # so changing any of these cannot invalidate a confirmed source set. All four are
                # render-request creative state: they re-plan, they never re-analyse.
                #
                # GUI Refresh A promoted that group to a tab of its own. The contract above is
                # unchanged and is exactly why the promotion was safe: nothing in here was ever
                # part of source identity, so no amount of moving it can reach the render gate.
                gr.Markdown(INFO_TAB_CREATIVE_SETTINGS)
                with gr.Group():
                    # [FORK] Digital-Union (Creative Controls Extra PR3): the preset selector sits
                    # directly above the six sliders it writes, and below the seed — the layout is
                    # the first statement that a preset moves those six and nothing else. It is NOT
                    # a `process_btn` input: the sliders already are, and they stay the only thing
                    # the render request carries. `Custom` is the last choice because it is a state
                    # the selector reports, not a recipe a user picks.
                    #
                    # [FORK] R8 UX Refresh (Commit C): MOVED here from the render tab.
                    #
                    # GUI Refresh A put it on the render tab on the argument that it is the one
                    # creative choice a user who never opens the studio still wants. That argument
                    # did not survive contact: the selector is a read-out of six values that live
                    # on *another* tab, so a user who changed a slider saw a preset name change on
                    # a screen they were not looking at, and a user on the render tab saw a name
                    # with no way to see or adjust what it stood for. The `.input()` graph between
                    # the seven widgets is completely unchanged -- it always crossed tabs and still
                    # does -- so this is a pure re-parenting, and the render tab now carries a
                    # truthful resolved *summary* instead of one of the controls.
                    creative_preset = gr.Radio(
                        choices=list(fork_presets.PRESET_NAMES),
                        value=fork_presets.BALANCED_PRESET,
                        label=LABEL_CREATIVE_PRESET,
                        info=INFO_CREATIVE_PRESET,
                        elem_id='creative-preset-radio',
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

                    # [FORK] R8 Final Polish: Clear Creative Variations.
                    #
                    # It exists because the obvious alternative is a FALSE statement. Selecting
                    # the `Balanced` preset resets the six base sliders and NOTHING ELSE -- it
                    # leaves the Variation Seed, Freestyle and every Audio Mix setting exactly as
                    # they were -- so describing it as "start from scratch" would be wrong, and
                    # an earlier draft of this refresh did describe it that way.
                    #
                    # So the two actions are deliberately disjoint and neither is a superset:
                    #
                    #     Balanced preset          ->  the six sliders, and only those
                    #     Clear Creative Variations ->  the seed and Freestyle, and only those
                    #
                    # It writes no slider, so it cannot become a second preset authority, and it
                    # touches nothing in Audio Mix, which is a different tab and a different
                    # decision. Those two absences are what keep it from being a second source of
                    # truth; both are pinned by test.
                    clear_variations_btn = gr.Button(
                        LABEL_CLEAR_VARIATIONS, variant='secondary', size='sm',
                        elem_id='clear-variations-button')
                    gr.Markdown(INFO_CLEAR_VARIATIONS)

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
                with gr.Group():
                    # Twelve components: an enable checkbox, ten section-type dropdowns and one
                    # read-only summary. No numeric per-section sliders, no Analyze Music button, no
                    # section-instance timeline, and deliberately no new top-level workflow.
                    #
                    # These widgets write NO global creative widget. They are also live inputs to
                    # both render events, which is what makes them execution authority rather than
                    # the summary state.
                    # [FORK] R8 Final Polish: the Director MOVED below the six controls, and all
                    # three big blocks are COLLAPSED.
                    #
                    # GUI Refresh A opened the Director and Freestyle on the argument that they
                    # were easy to overlook. Measured afterwards, this tab was 3,516 px tall and
                    # the controls an operator actually reaches for every render -- the preset,
                    # the seed, the six sliders -- were the part you had to scroll past three
                    # open panels to reach. Discoverability is a tab away; scrolling is on every
                    # visit. The order is now frequency of use, and the explanation the old
                    # ordering carried ("describe the edit, then the controls") survives as the
                    # order of the collapsed panels themselves.
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
                        # [FORK] Digital-Union (Random Freestyle V2): `Random` is now the default,
                        # so enabling Freestyle is experimental without ten manual picks. `Base`
                        # remains available and still means "inherit the live global value"; the
                        # other four named styles are unchanged. Freestyle OFF is unaffected —
                        # the switch is still the only gate.
                        freestyle_intro = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} intro',
                            elem_id='freestyle-intro')
                        freestyle_hook = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} hook',
                            elem_id='freestyle-hook')
                        freestyle_outro = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} outro',
                            elem_id='freestyle-outro')
                        freestyle_finale = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} finale',
                            elem_id='freestyle-finale')
                        freestyle_drop = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} drop',
                            elem_id='freestyle-drop')
                        freestyle_chorus = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} chorus',
                            elem_id='freestyle-chorus')
                        freestyle_bridge = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} bridge',
                            elem_id='freestyle-bridge')
                        freestyle_breakdown = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} breakdown',
                            elem_id='freestyle-breakdown')
                        freestyle_verse = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} verse',
                            elem_id='freestyle-verse')
                        freestyle_body = gr.Dropdown(
                            choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES),
                            value=fork_freestyle_random.RANDOM_STYLE,
                            label=f'{LABEL_FREESTYLE_SECTION_PREFIX} body',
                            elem_id='freestyle-body')
                        # Read-only, and formatted entirely by `freestyle.summary_text()` — one
                        # formatter per read-out, exactly as the Variant Lab, mix and Director
                        # reports work. It never prints an inherited number; see the handler.
                        # [FORK] Digital-Union (Random Freestyle V2): rolls a new Variation Seed
                        # and previews what the Random sections become. Placed directly above the
                        # summary it writes. It reads the dropdowns and never writes one.
                        surprise_btn = gr.Button(
                            LABEL_SURPRISE_ME, variant='secondary',
                            elem_id='freestyle-surprise-button')
                        gr.Markdown(INFO_SURPRISE_ME)
                        freestyle_summary = gr.Textbox(
                            label=LABEL_FREESTYLE_SUMMARY,
                            value=fork_freestyle.summary_text(
                                fork_freestyle.FreestyleDeclaration()),
                            lines=6,
                            max_lines=16,
                            interactive=False,
                            elem_id='freestyle-summary',
                        )

                with gr.Group():
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

            # [FORK] Digital-Union (Raw Cutter V1): a top-level tab, not an accordion inside
            # Advanced Tools. It is a tool the operator comes to the app specifically to use --
            # curating footage BEFORE any of the render workflow applies -- so burying it would
            # repeat the discoverability problem the three-tab refresh existed to fix.
            #
            # Nothing in here is wired to `source_outputs`, `prep_outputs`, the render request or
            # any creative state, so using the cutter cannot clear a source confirmation, disturb
            # a prepared library or change what Create Music Video would produce.
            with gr.Tab(LABEL_TAB_RAW_CUTTER, id='tab-raw-cutter'):
                gr.Markdown(INFO_TAB_RAW_CUTTER)
                # [FORK] Digital-Union (Raw Cutter Batch V1): one tab, two modes. NOT a fifth
                # top-level tab -- batch mode is the same tool pointed at a folder instead of a
                # file, and the execution settings below are SHARED between the two modes rather
                # than duplicated, so there is exactly one sensitivity, minimum, maximum and
                # Remove Audio value on the screen.
                cut_mode = gr.Radio(
                    choices=[CUT_MODE_SINGLE, CUT_MODE_BATCH],
                    value=CUT_MODE_SINGLE,
                    label=LABEL_CUT_MODE,
                    info=INFO_CUT_MODE,
                    elem_id='cut-mode-radio',
                )
                with gr.Row():
                    with gr.Column(scale=2):
                        with gr.Group(visible=True) as cut_single_inputs:
                            cut_source = gr.Textbox(
                                label=LABEL_CUT_SOURCE,
                                placeholder=PLACEHOLDER_CUT_SOURCE,
                                info=INFO_CUT_SOURCE,
                                elem_id='cut-source-input',
                            )
                            cut_source_info = gr.Textbox(
                                label=LABEL_CUT_SOURCE_INFO,
                                value='',
                                placeholder=PLACEHOLDER_CUT_SOURCE_INFO,
                                interactive=False,
                                lines=2,
                                max_lines=3,
                                elem_id='cut-source-info-box',
                            )
                        with gr.Group(visible=False) as cut_batch_inputs:
                            cut_batch_folder = gr.Textbox(
                                label=LABEL_CUT_BATCH_FOLDER,
                                placeholder=PLACEHOLDER_CUT_BATCH_FOLDER,
                                info=INFO_CUT_BATCH_FOLDER,
                                value=_pref(fork_prefs.RAW_CUTTER_INPUT),
                                elem_id='cut-batch-folder-input',
                            )
                            cut_batch_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', size='sm',
                                elem_id='cut-batch-browse')
                            cut_batch_recursive = gr.Checkbox(
                                value=True,
                                label=LABEL_CUT_BATCH_RECURSIVE,
                                info=INFO_CUT_BATCH_RECURSIVE,
                                elem_id='cut-batch-recursive',
                            )
                            cut_batch_count_btn = gr.Button(
                                LABEL_CUT_BATCH_COUNT, variant='secondary', size='sm',
                                elem_id='cut-batch-count-button')
                            cut_batch_discovery = gr.Textbox(
                                label=LABEL_CUT_BATCH_DISCOVERY,
                                value='',
                                placeholder=PLACEHOLDER_CUT_BATCH_DISCOVERY,
                                interactive=False,
                                lines=3,
                                max_lines=6,
                                elem_id='cut-batch-discovery-box',
                            )
                        # Shared by both modes: one output root, one set of cutting settings.
                        with gr.Group():
                            cut_output = gr.Textbox(
                                label=LABEL_CUT_OUTPUT,
                                placeholder=PLACEHOLDER_CUT_OUTPUT,
                                info=INFO_CUT_OUTPUT,
                                value=_pref(fork_prefs.RAW_CUTTER_OUTPUT),
                                elem_id='cut-output-input',
                            )
                            cut_output_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', size='sm',
                                elem_id='cut-output-browse')
                        with gr.Group():
                            cut_sensitivity = gr.Slider(
                                minimum=fork_cutter.SENSITIVITY_MIN,
                                maximum=fork_cutter.SENSITIVITY_MAX,
                                step=1,
                                value=fork_cutter.SENSITIVITY_DEFAULT,
                                label=LABEL_CUT_SENSITIVITY,
                                info=INFO_CUT_SENSITIVITY,
                                elem_id='cut-sensitivity-slider',
                            )
                            with gr.Row():
                                cut_min_seconds = gr.Number(
                                    value=fork_cutter.MIN_CLIP_SECONDS_DEFAULT,
                                    minimum=0,
                                    label=LABEL_CUT_MIN_SECONDS,
                                    info=INFO_CUT_MIN_SECONDS,
                                    elem_id='cut-min-seconds',
                                )
                                cut_max_seconds = gr.Number(
                                    value=fork_cutter.MAX_CLIP_SECONDS_DEFAULT,
                                    minimum=0,
                                    label=LABEL_CUT_MAX_SECONDS,
                                    info=INFO_CUT_MAX_SECONDS,
                                    elem_id='cut-max-seconds',
                                )
                            cut_remove_audio = gr.Checkbox(
                                value=True,
                                label=LABEL_CUT_REMOVE_AUDIO,
                                info=INFO_CUT_REMOVE_AUDIO,
                                elem_id='cut-remove-audio',
                            )
                            # [FORK] Digital-Union (Raw Cutter R4-C): ONE shared checkbox, in the
                            # same shared settings group as sensitivity, the two lengths and
                            # Remove Audio -- so there is exactly one value per execution setting
                            # on the screen and both modes read the same one.
                            #
                            # Default OFF, and that is a product decision rather than a
                            # convenience: with it off every generated name is byte-for-byte what
                            # it has always been, so an existing workflow cannot be changed by
                            # this feature shipping.
                            #
                            # Deliberately NOT a planning input: it changes what a clip is CALLED,
                            # never where it begins or ends, so it is absent from
                            # `plan_signature` and from the staleness listeners below for exactly
                            # the reason Remove Audio is. Refusing a valid plan over a filename
                            # would force a needless re-analysis of a long video.
                            cut_rename_output = gr.Checkbox(
                                value=False,
                                label=LABEL_CUT_RENAME_OUTPUT,
                                info=INFO_CUT_RENAME_OUTPUT,
                                elem_id='cut-rename-output',
                            )
                            # [FORK] R6: ONE selector, shared by Single Video and Folder Batch,
                            # in the same shared settings area as Remove Audio and Rename.
                            #
                            # Deliberately NOT a planning input, for exactly the reason the two
                            # above are not: it changes how an exported clip is ENCODED, never
                            # where a cut is placed. So it is absent from `plan_signature` and
                            # from the staleness listeners, and switching it cannot invalidate
                            # an analysis of a long source.
                            cut_output_profile = gr.Radio(
                                choices=fork_profiles.profile_choices(),
                                value=fork_profiles.SAME_AS_SOURCE,
                                label=LABEL_CUT_OUTPUT_PROFILE,
                                info=INFO_CUT_OUTPUT_PROFILE,
                                elem_id='cut-output-profile',
                            )
                        with gr.Column(visible=True) as cut_single_actions:
                            with gr.Row():
                                cut_analyze_btn = gr.Button(
                                    LABEL_CUT_ANALYZE, variant='secondary',
                                    elem_id='cut-analyze-button')
                                # [FORK] P1: starts UNAVAILABLE. There is no plan at load, so an
                                # enabled Cut Video would only ever earn the "press Analyze first"
                                # refusal. The server-side plan check is unchanged and still the
                                # authority -- this just stops the pointless click.
                                cut_run_btn = gr.Button(
                                    LABEL_CUT_RUN, variant='primary', interactive=False,
                                    elem_id='cut-run-button')
                            # [FORK] P1: starts INACTIVE. A permanently red emergency control that
                            # does nothing teaches an operator to distrust it; it lights up only
                            # while a job it can really stop is running.
                            cut_cancel_btn = gr.Button(
                                LABEL_CUT_CANCEL, variant='stop', size='sm', interactive=False,
                                elem_id='cut-cancel-button')
                            gr.Markdown(INFO_CUT_ACTIONS)
                        with gr.Column(visible=False) as cut_batch_actions:
                            cut_batch_start_btn = gr.Button(
                                LABEL_CUT_BATCH_START, variant='primary',
                                elem_id='cut-batch-start-button')
                            cut_batch_cancel_btn = gr.Button(
                                LABEL_CUT_BATCH_CANCEL, variant='stop', size='sm',
                                interactive=False,
                                elem_id='cut-batch-cancel-button')
                            gr.Markdown(INFO_CUT_BATCH_ACTIONS)
                    with gr.Column(scale=3):
                        with gr.Column(visible=True) as cut_single_readouts:
                            # [FORK] Digital-Union (Raw Cutter progress P1): FIRST in the column,
                            # because it is the thing an operator looks at while waiting. Empty at
                            # idle so it never claims work that is not happening.
                            cut_progress = gr.HTML(
                                value='', elem_id='cut-progress', label=LABEL_CUT_PROGRESS)
                            cut_summary = gr.Textbox(
                                label=LABEL_CUT_PLAN_SUMMARY,
                                value='',
                                placeholder=PLACEHOLDER_CUT_PLAN_SUMMARY,
                                interactive=False,
                                lines=10,
                                max_lines=18,
                                elem_id='cut-plan-summary',
                            )
                            cut_table = gr.Dataframe(
                                headers=list(fork_cutter.PLAN_TABLE_HEADERS),
                                value=[],
                                interactive=False,
                                wrap=True,
                                elem_id='cut-plan-table',
                            )
                            cut_report = gr.Textbox(
                                label=LABEL_CUT_RUN_REPORT,
                                value='',
                                placeholder=PLACEHOLDER_CUT_RUN_REPORT,
                                interactive=False,
                                lines=8,
                                max_lines=20,
                                elem_id='cut-run-report',
                            )
                        with gr.Column(visible=False) as cut_batch_readouts:
                            cut_batch_progress = gr.HTML(
                                value='', elem_id='cut-batch-progress', label=LABEL_CUT_PROGRESS)
                            cut_batch_status = gr.Textbox(
                                label=LABEL_CUT_BATCH_STATUS,
                                value='',
                                placeholder=PLACEHOLDER_CUT_BATCH_STATUS,
                                interactive=False,
                                lines=10,
                                max_lines=16,
                                elem_id='cut-batch-status',
                            )
                            cut_batch_summary = gr.Textbox(
                                label=LABEL_CUT_BATCH_SUMMARY,
                                value='',
                                placeholder=PLACEHOLDER_CUT_BATCH_SUMMARY,
                                interactive=False,
                                lines=14,
                                max_lines=34,
                                elem_id='cut-batch-summary',
                            )
                # [FORK] R8 UX Refresh (Commit A): the Raw Cutter's half of "Open results in
                # Media Library", the symmetric partner of the Audio Cutter's button.
                #
                # It sits OUTSIDE both `cut_single_readouts` and `cut_batch_readouts` on purpose,
                # so it is available in Single and Folder Batch alike -- those two columns are
                # mutually exclusive and a button inside either would vanish in the other mode.
                #
                # It hands over `cut_output` (the parent output folder) with subfolders INCLUDED,
                # rather than the exact run directory. That is a deliberate accuracy trade and not
                # laziness: every run writes its own fresh timestamped subfolder, and the Raw
                # Cutter keeps no run-directory state the way the Audio Cutter's
                # `audio_run_dir_state` does. Adding one would mean changing the Cut handler's
                # return signature, which several suites pin positionally -- so the button reads a
                # widget that already exists and tells the operator exactly what it did. Reviewing
                # still never requires having run a cutter: Video Review's folder box is editable
                # and this only ever fills it in.
                cut_open_library_btn = gr.Button(
                    LABEL_CUT_OPEN_LIBRARY, variant='secondary', size='sm',
                    elem_id='cut-open-library-button')
                gr.Markdown(INFO_CUT_OPEN_LIBRARY)

                # The plan rides as a plain dict of primitives. Nothing live -- no thread, lock,
                # handle or lifecycle -- may enter `gr.State`, and a dict is also what lets the
                # Cut handler recompute the staleness signature and compare it.
                cut_plan_state = gr.State(None)

            # [FORK] Digital-Union (R7 Raw Audio Cutter V1): the SIXTH top-level tab, placed
            # between Advanced Tools and Clips Preview.
            #
            # That position is chosen, not incidental. Placing it here keeps Advanced Tools at
            # index 3, so every containment assertion in `test_gui_tabs.py` that counts from the
            # front is unaffected; it keeps Clips Preview LAST, which its own suite requires; and
            # it keeps this markup out of the Raw-Cutter-to-Advanced span that
            # `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans.
            #
            # Every numeric control here is a Number widget, and that is the same non-cosmetic
            # constraint the Clips Preview tab records: a tab in this region falls inside the
            # source span that
            # `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui`
            # guards, which enforces the approved sample resource policy by OMISSION. The guard is
            # a substring check, so the forbidden widget type must not appear in this span even
            # inside a comment explaining its absence -- which is why this one does not name it.
            with gr.Tab(LABEL_TAB_AUDIO_CUTTER, id='tab-audio-cutter'):
                gr.Markdown(INFO_TAB_AUDIO_CUTTER)
                with gr.Row():
                    with gr.Column(scale=3):
                        with gr.Group():
                            audio_source = gr.Textbox(
                                label=LABEL_AUDIO_SOURCE,
                                info=INFO_AUDIO_SOURCE,
                                value=_pref(fork_prefs.AUDIO_CUTTER_INPUT),
                                placeholder=r"J:\Recordings\interview_take2.wav",
                                elem_id='audio-source')
                            audio_source_browse = gr.Button(
                                LABEL_BROWSE_FILE, variant='secondary', scale=0,
                                elem_id='audio-source-browse')
                        with gr.Group():
                            audio_output = gr.Textbox(
                                label=LABEL_AUDIO_OUTPUT,
                                info=INFO_AUDIO_OUTPUT,
                                value=_pref(fork_prefs.AUDIO_CUTTER_OUTPUT),
                                placeholder=r"J:\Voice Parts",
                                elem_id='audio-output')
                            audio_output_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                elem_id='audio-output-browse')
                        with gr.Group():
                            with gr.Row():
                                audio_threshold = gr.Number(
                                    label=LABEL_AUDIO_THRESHOLD,
                                    info=INFO_AUDIO_THRESHOLD,
                                    value=fork_audio.DEFAULT_THRESHOLD_DB,
                                    elem_id='audio-threshold')
                                audio_min_silence = gr.Number(
                                    label=LABEL_AUDIO_MIN_SILENCE,
                                    info=INFO_AUDIO_MIN_SILENCE,
                                    value=fork_audio.DEFAULT_MIN_SILENCE, minimum=0,
                                    elem_id='audio-min-silence')
                            with gr.Row():
                                audio_pad = gr.Number(
                                    label=LABEL_AUDIO_PAD,
                                    info=INFO_AUDIO_PAD,
                                    value=fork_audio.DEFAULT_PAD, minimum=0,
                                    elem_id='audio-pad')
                                audio_min_clip = gr.Number(
                                    label=LABEL_AUDIO_MIN_CLIP,
                                    info=INFO_AUDIO_MIN_CLIP,
                                    value=fork_audio.DEFAULT_MIN_CLIP, minimum=0,
                                    elem_id='audio-min-clip')
                            # [R7.2] One choice per entry in `fork_audio.FORMATS`, APPENDED so
                            # WAV stays the default and the two existing values keep their
                            # positions. A test pins this list against `FORMATS` by value,
                            # because the first browser smoke test of Phase 5 found the core, the
                            # handler, the manifest and the argv all supporting MP3 while the
                            # selector still offered only two options -- the one place an operator
                            # could actually choose it.
                            audio_format = gr.Radio(
                                label=LABEL_AUDIO_FORMAT,
                                info=INFO_AUDIO_FORMAT,
                                choices=[("WAV · 16-bit PCM · 48 kHz", fork_audio.FORMAT_WAV),
                                         ("FLAC · lossless · 48 kHz", fork_audio.FORMAT_FLAC),
                                         (f"MP3 · {fork_audio.DEFAULT_MP3_BITRATE_KBPS} kbps · "
                                          f"smaller files, lossy", fork_audio.FORMAT_MP3)],
                                value=fork_audio.FORMAT_WAV,
                                elem_id='audio-format')
                            # [FORK] R8 UX Refresh (Phase 9): optional soft edges. OFF by
                            # default, so every export already accepted stays bit-identical
                            # unless the operator asks for this.
                            audio_fade_edges = gr.Checkbox(
                                value=False,
                                label=LABEL_AUDIO_FADE_EDGES,
                                info=INFO_AUDIO_FADE_EDGES,
                                elem_id='audio-fade-edges')
                        with gr.Row():
                            audio_analyze_btn = gr.Button(
                                LABEL_AUDIO_ANALYZE, variant='secondary',
                                elem_id='audio-analyze-button')
                            audio_cut_btn = gr.Button(
                                LABEL_AUDIO_CUT, variant='primary',
                                elem_id='audio-cut-button')
                        gr.Markdown(INFO_AUDIO_BUTTONS)
                    with gr.Column(scale=2):
                        audio_plan = gr.Textbox(
                            label=LABEL_AUDIO_PLAN,
                            value=AUDIO_PLAN_INITIAL_TEXT,
                            interactive=False, lines=12, max_lines=20,
                            elem_id='audio-plan-box')
                        audio_table = gr.Dataframe(
                            headers=["Clip", "Start", "End", "Length", "Output name"],
                            datatype=["number", "str", "str", "str", "str"],
                            value=[], interactive=False, wrap=True,
                            label=LABEL_AUDIO_TABLE,
                            elem_id='audio-table')
                        audio_report = gr.Textbox(
                            label=LABEL_AUDIO_REPORT,
                            value=AUDIO_REPORT_INITIAL_TEXT,
                            interactive=False, lines=8, max_lines=16,
                            elem_id='audio-report-box')

                # The review half: listen to what was actually written, then file it.
                with gr.Group():
                    gr.Markdown(f"### {LABEL_AUDIO_REVIEW}\n\n{INFO_AUDIO_REVIEW}")
                    with gr.Row():
                        audio_library_folder = gr.Textbox(
                            label=LABEL_AUDIO_LIBRARY,
                            info=INFO_AUDIO_LIBRARY,
                            value=_pref(fork_prefs.AUDIO_LIBRARY),
                            placeholder=r"J:\Voice Library",
                            elem_id='audio-library-folder')
                        audio_discarded_folder = gr.Textbox(
                            label=LABEL_AUDIO_DISCARDED,
                            info=INFO_AUDIO_DISCARDED,
                            value=_pref(fork_prefs.AUDIO_DISCARDED),
                            placeholder=r"J:\Voice Discarded",
                            elem_id='audio-discarded-folder')
                    with gr.Row():
                        audio_library_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='audio-library-browse')
                        audio_discarded_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='audio-discarded-browse')
                    # [R7.2 Phase 4] The live destination read-out. Written by the two folder
                    # `.change()` handlers and by Confirm, so "these two folders are the same" is
                    # visible while the operator is still typing rather than only when a sort is
                    # refused -- which is the defect reported from real use.
                    audio_destination_status = gr.Textbox(
                        label=LABEL_AUDIO_DESTINATION_STATUS,
                        value=AUDIO_DESTINATION_STATUS_INITIAL,
                        interactive=False, lines=2, max_lines=5,
                        elem_id='audio-destination-status')
                    gr.Markdown(INFO_AUDIO_GALLERY)
                    gr.HTML(AUDIO_GALLERY_CSS)
                    with gr.Row():
                        audio_enable_hover = gr.Button(
                            LABEL_AUDIO_ENABLE_HOVER, variant='secondary', scale=0,
                            elem_id='audio-enable-hover',
                            elem_classes=['r7-enable-hover'])
                        audio_counts = gr.Textbox(
                            label=LABEL_AUDIO_COUNTS, value=AUDIO_COUNTS_INITIAL_TEXT,
                            interactive=False, elem_id='audio-counts')
                    audio_gallery = gr.HTML(value=AUDIO_GALLERY_EMPTY_HTML,
                                            elem_id='audio-gallery')
                    # The ONE server contract with the page: every decision and every edited name
                    # as JSON. Hidden because it is machinery, not a control -- but a real widget,
                    # so Gradio carries it with the click like any other input.
                    # NOT `visible=False`: Gradio drops an invisible component from the DOM
                    # entirely, and the page has to be able to write into this one. It is hidden
                    # with CSS instead -- present, carried with the click, and off screen.
                    audio_decisions = gr.Textbox(
                        value='', label=' ', elem_id='audio-decisions')
                    audio_confirm_btn = gr.Button(
                        LABEL_AUDIO_CONFIRM, variant='primary',
                        elem_id='audio-confirm-button',
                        # The client releases the player on `pointerdown` of anything carrying
                        # this class. Windows will not rename a file a player still holds open.
                        elem_classes=['r7-release-audio'])
                    # [R7.2 Phase 3] The sweep gate. Hidden until Confirm finds unmarked clips, and
                    # hidden again the moment the batch is resolved either way, so the screen never
                    # offers a Proceed that is not attached to a specific set of counts.
                    with gr.Row():
                        audio_proceed_btn = gr.Button(
                            LABEL_AUDIO_PROCEED, variant='stop', visible=False,
                            elem_id='audio-proceed-button',
                            elem_classes=['r7-release-audio'])
                        audio_sweep_cancel_btn = gr.Button(
                            LABEL_AUDIO_SWEEP_CANCEL, variant='secondary', visible=False,
                            elem_id='audio-sweep-cancel-button')
                    audio_open_preview_btn = gr.Button(
                        LABEL_AUDIO_OPEN_IN_PREVIEW, variant='secondary',
                        elem_id='audio-open-preview-button')
                    gr.Markdown(INFO_AUDIO_OPEN_IN_PREVIEW)
                    with gr.Row():
                        with gr.Column(scale=2):
                            audio_clip_name = gr.Textbox(
                                label=LABEL_AUDIO_CLIP_NAME,
                                info=INFO_AUDIO_CLIP_NAME,
                                value='', visible=False, elem_id='audio-clip-name')
                        with gr.Column(scale=3):
                            # [R7.1] Kept as a fallback player for a single clip, hidden by
                            # default: the gallery's hover playback is the review surface now.
                            audio_player = gr.HTML(
                                value='', visible=False, elem_id='audio-player')
                    audio_file_report = gr.Textbox(
                        label=LABEL_AUDIO_FILE_REPORT,
                        value=AUDIO_FILE_REPORT_INITIAL_TEXT,
                        interactive=False, lines=4, max_lines=10,
                        elem_id='audio-file-report')
                    # Per-browser-session, so one operator's review list is not reachable from
                    # another tab. Nothing about the clips lives in a module-level record.
                    #
                    # [R7.2] Holds the whole view: the registry session and page tokens, the frozen
                    # entries, and the decision/name records keyed by FILE IDENTITY so they survive
                    # paging. `audio_armed_state` holds the sweep signature of the batch the
                    # operator confirmed, and only that batch -- a plain string, never a live
                    # object.
                    audio_review_state = gr.State({})
                    audio_armed_state = gr.State('')
                    audio_run_dir_state = gr.State('')

                gr.Markdown(INFO_AUDIO_LAYERS_TIP)

            with gr.Tab(LABEL_TAB_AUDIO_MIX, id='tab-audio-mix'):
                gr.Markdown(INFO_TAB_AUDIO_MIX)
                # [FORK] Digital-Union (Audio Layers V1 / D): voice over music. Collapsed.
                # GUI Refresh A moved it out from under the main audio input and into this tab: it
                # is an occasional extra, and keeping it beside the audio picker cost the render
                # flow vertical space on every single render. Staying collapsed is pinned by test.
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
                    # [FORK] R7.1 (Comfort Mix): two optional controls. Both default to the legacy
                    # behaviour, so an operator who never touches them gets the identical mix.
                    voice_auto_balance = gr.Checkbox(
                        label=LABEL_VOICE_AUTO_BALANCE,
                        value=False,
                        info=INFO_VOICE_AUTO_BALANCE,
                        elem_id='voice-auto-balance-checkbox',
                    )
                    voice_music_return = gr.Number(
                        label=LABEL_MUSIC_RETURN,
                        value=fork_audio_mix.DEFAULT_MUSIC_RETURN_SECONDS,
                        minimum=0.1,
                        maximum=fork_audio_mix.MUSIC_RETURN_MAX_SECONDS,
                        info=INFO_MUSIC_RETURN,
                        elem_id='music-return-input',
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

            # [FORK] Digital-Union (R8 UX Refresh, Commit A): the Media Library tab.
            #
            # Three review and maintenance workflows that all answer "what is in my library?"
            # used to be two separate top-level tabs plus an accordion buried at the bottom of
            # Advanced Tools. They are the same job at three stages, so they are now three
            # sections of one tab -- and nothing about their engines changed: every widget,
            # `elem_id` and event registration below is the pre-existing one, re-parented.
            #
            # Prepare & Verify is deliberately LAST. The span from `INFO_PREP_SAMPLE` to
            # `sample_btn.click(` is what
            # `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui`
            # slices to prove the approved sample resource policy is not widenable from the GUI,
            # and it enforces that by OMISSION of one widget type over the whole remaining
            # source. Putting this section last keeps the Audio Mix sliders *before* it, and
            # leaves only the Settings tab after it -- which is why every numeric control in
            # Settings is a Number. The guard is a substring check, so this comment does not
            # name the banned type either.
            with gr.Tab(LABEL_TAB_MEDIA_LIBRARY, id='tab-media-library'):
                gr.Markdown(INFO_TAB_MEDIA_LIBRARY)
                with gr.Tabs() as library_tabs:
                    # [FORK] Digital-Union (R5 Clips Preview Selector, P1): the FIFTH top-level tab.
                    #
                    # Placed LAST, after Advanced Tools, and that position is load-bearing twice over.
                    # It keeps Advanced Tools at tab index 3, so every existing containment assertion in
                    # `test_gui_tabs.py` needs no index change; and it keeps this tab's markup out of the
                    # Raw-Cutter-to-Advanced source span that
                    # `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans for `gr.Accordion`.
                    #
                    # Page size is a Number widget, and that is also not cosmetic. A tab placed last
                    # falls inside the source span that
                    # `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui`
                    # guards -- measured in R5-P0 as ~39 k characters, running past `source_outputs = [`
                    # into the event wiring. That test enforces the approved sample resource policy by
                    # OMISSION, so the forbidden widget type must not appear anywhere in this span, not
                    # even in a comment explaining why: the guard is a substring check, and prose naming
                    # the banned token trips it exactly as real code would. (It has now done so five
                    # times in this repository.) The test is right; the widget is a Number and this
                    # comment does not name the alternative.
                    with gr.Tab(LABEL_SECTION_VIDEO_REVIEW, id='tab-video-review'):
                        gr.Markdown(INFO_TAB_CLIPS_PREVIEW)
                        with gr.Row():
                            clips_input = gr.Textbox(
                                label=LABEL_CLIPS_INPUT,
                                info=INFO_CLIPS_INPUT,
                                value=_pref(fork_prefs.CLIPS_INPUT),
                                placeholder=r"J:\Raw Parts\RawCutter_Batch_20261018_153000",
                                elem_id='clips-input-folder',
                            )
                            clips_recursive = gr.Checkbox(
                                value=True,
                                label=LABEL_CLIPS_RECURSIVE,
                                info=INFO_CLIPS_RECURSIVE,
                                elem_id='clips-recursive',
                            )
                        with gr.Row():
                            clips_input_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                elem_id='clips-input-browse')
                        with gr.Row():
                            clips_library = gr.Textbox(
                                label=LABEL_CLIPS_LIBRARY,
                                info=INFO_CLIPS_LIBRARY,
                                value=_pref(fork_prefs.CLIPS_LIBRARY),
                                placeholder=r"J:\Media Library",
                                elem_id='clips-library-folder',
                            )
                            clips_rejected = gr.Textbox(
                                label=LABEL_CLIPS_REJECTED,
                                info=INFO_CLIPS_REJECTED,
                                value=_pref(fork_prefs.CLIPS_REJECTED),
                                placeholder=r"J:\Discarded",
                                elem_id='clips-rejected-folder',
                            )
                        with gr.Row():
                            clips_library_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                elem_id='clips-library-browse')
                            clips_rejected_browse = gr.Button(
                                LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                elem_id='clips-rejected-browse')
                        with gr.Row():
                            clips_page_size = gr.Number(
                                value=100,
                                precision=0,
                                minimum=1,
                                maximum=fork_clips.MAX_CLIPS_PER_PAGE,
                                label=LABEL_CLIPS_PAGE_SIZE,
                                info=INFO_CLIPS_PAGE_SIZE,
                                elem_id='clips-page-size',
                            )
                            clips_check_btn = gr.Button(
                                LABEL_CLIPS_CHECK, variant='secondary', elem_id='clips-check-button')
                            clips_scan_btn = gr.Button(
                                LABEL_CLIPS_SCAN, variant='primary', elem_id='clips-scan-button')
                            clips_refresh_btn = gr.Button(
                                LABEL_CLIPS_REFRESH, variant='secondary',
                                elem_id='clips-refresh-button')
                        clips_report = gr.Textbox(
                            label=LABEL_CLIPS_REPORT,
                            value=CLIPS_REPORT_INITIAL_TEXT,
                            interactive=False,
                            lines=7,
                            max_lines=14,
                            elem_id='clips-report-box',
                        )
                        clips_counts = gr.Textbox(
                            label=LABEL_CLIPS_COUNTS,
                            value=CLIPS_COUNTS_INITIAL_TEXT,
                            interactive=False,
                            elem_id='clips-counts-box',
                        )
                        # The grid is server-rendered markup. The browser never receives a path, only
                        # opaque ids, and the authority for what each tile says is the decision journal.
                        clips_gallery = gr.HTML(
                            CLIPS_GALLERY_INITIAL_HTML, elem_id='clips-gallery')
                        clips_confirm_btn = gr.Button(
                            LABEL_CLIPS_CONFIRM, variant='secondary', interactive=False,
                            elem_id='clips-confirm-button')
                        # [FORK] R8 Final Polish: the sweep gate, mirroring Audio Review exactly.
                        # Hidden until a Confirm finds unmarked clips, and the ONE thing that can
                        # authorise moving them. `clips_armed_state` holds the batch signature
                        # those two buttons are attached to -- a plain string, never anything
                        # live, and void the moment a decision or a destination changes.
                        with gr.Row():
                            clips_proceed_btn = gr.Button(
                                LABEL_CLIPS_PROCEED, variant='stop', visible=False,
                                elem_id='clips-proceed-button')
                            clips_sweep_cancel_btn = gr.Button(
                                LABEL_CLIPS_SWEEP_CANCEL, variant='secondary', visible=False,
                                elem_id='clips-sweep-cancel-button')
                        clips_armed_state = gr.State('')
                        clips_confirm_out = gr.Textbox(
                            label=LABEL_CLIPS_CONFIRM_OUT,
                            value=CLIPS_CONFIRM_DISABLED_TEXT,
                            interactive=False,
                            lines=5,
                            max_lines=10,
                            elem_id='clips-confirm-box',
                        )

                    # [FORK] Digital-Union (R7.2 Phase 2): the SEVENTH top-level tab, 🎧 Audio Preview.
                    #
                    # Placed between Audio Cutter and Clips Preview, and that position is chosen rather
                    # than incidental. It keeps Advanced Tools at `_TABS` index 3, so every containment
                    # assertion in `test_gui_tabs.py` that counts from the front is unaffected; it keeps
                    # Clips Preview LAST, which its own suite requires; and it keeps this markup out of the
                    # Raw-Cutter-to-Advanced span that
                    # `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans.
                    #
                    # Every numeric control here is a Number widget, which is the same non-cosmetic
                    # constraint the two tabs either side of it record: a tab in this region falls inside
                    # the source span that
                    # `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui`
                    # guards, which enforces the approved sample resource policy by OMISSION. The guard is
                    # a substring check over this whole region, so the forbidden widget type must not
                    # appear in this span even inside a comment explaining its absence -- which is why
                    # this one does not name it.
                    with gr.Tab(LABEL_SECTION_AUDIO_REVIEW, id='tab-audio-review'):
                        gr.Markdown(INFO_TAB_AUDIO_PREVIEW)
                        gr.HTML(AUDIO_GALLERY_CSS)
                        with gr.Row():
                            with gr.Column(scale=3):
                                with gr.Group():
                                    audio_preview_folder = gr.Textbox(
                                        label=LABEL_AUDIO_PREVIEW_FOLDER,
                                        info=INFO_AUDIO_PREVIEW_FOLDER,
                                        value=_pref(fork_prefs.AUDIO_PREVIEW_INPUT),
                                        placeholder=r"J:\New folder\CutsAudio\VOICE OVER",
                                        elem_id='audio-preview-folder')
                                    with gr.Row():
                                        audio_preview_browse = gr.Button(
                                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                            elem_id='audio-preview-browse')
                                        audio_preview_recursive = gr.Checkbox(
                                            label=LABEL_AUDIO_PREVIEW_RECURSIVE,
                                            info=INFO_AUDIO_PREVIEW_RECURSIVE,
                                            value=False, elem_id='audio-preview-recursive')
                                    with gr.Row():
                                        audio_preview_page_size = gr.Number(
                                            label=LABEL_AUDIO_PREVIEW_PAGE_SIZE,
                                            info=INFO_AUDIO_PREVIEW_PAGE_SIZE,
                                            value=fork_audio_review.DEFAULT_PAGE_SIZE,
                                            minimum=1, maximum=fork_audio_review.MAX_PAGE_SIZE,
                                            step=1, elem_id='audio-preview-page-size')
                                        audio_preview_scan_btn = gr.Button(
                                            LABEL_AUDIO_PREVIEW_SCAN, variant='primary',
                                            elem_id='audio-preview-scan-button')
                                with gr.Group():
                                    audio_preview_library = gr.Textbox(
                                        label=LABEL_AUDIO_PREVIEW_LIBRARY,
                                        info=INFO_AUDIO_LIBRARY,
                                        value=_pref(fork_prefs.AUDIO_PREVIEW_LIBRARY),
                                        placeholder=r"J:\Voice Library",
                                        elem_id='audio-preview-library')
                                    audio_preview_library_browse = gr.Button(
                                        LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                        elem_id='audio-preview-library-browse')
                                    audio_preview_discarded = gr.Textbox(
                                        label=LABEL_AUDIO_PREVIEW_DISCARDED,
                                        info=INFO_AUDIO_DISCARDED,
                                        value=_pref(fork_prefs.AUDIO_PREVIEW_DISCARDED),
                                        placeholder=r"J:\Voice Discarded",
                                        elem_id='audio-preview-discarded')
                                    audio_preview_discarded_browse = gr.Button(
                                        LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                                        elem_id='audio-preview-discarded-browse')
                                    audio_preview_destination_status = gr.Textbox(
                                        label=LABEL_AUDIO_DESTINATION_STATUS,
                                        value=AUDIO_DESTINATION_STATUS_INITIAL,
                                        interactive=False, lines=2, max_lines=5,
                                        elem_id='audio-preview-destination-status')
                            with gr.Column(scale=2):
                                audio_preview_report = gr.Textbox(
                                    label=LABEL_AUDIO_PREVIEW_REPORT,
                                    value=AUDIO_PREVIEW_REPORT_INITIAL,
                                    interactive=False, lines=10, max_lines=18,
                                    elem_id='audio-preview-report')

                        gr.Markdown(INFO_AUDIO_GALLERY)
                        with gr.Row():
                            audio_preview_enable_hover = gr.Button(
                                LABEL_AUDIO_ENABLE_HOVER, variant='secondary', scale=0,
                                elem_id='audio-preview-enable-hover',
                                elem_classes=['r7-enable-hover'])
                            audio_preview_counts = gr.Textbox(
                                label=LABEL_AUDIO_COUNTS, value=AUDIO_COUNTS_INITIAL_TEXT,
                                interactive=False, elem_id='audio-preview-counts')
                        with gr.Row():
                            audio_preview_prev_btn = gr.Button(
                                LABEL_AUDIO_PREVIEW_PREV, variant='secondary', scale=0,
                                elem_id='audio-preview-prev-button')
                            audio_preview_page_label = gr.Textbox(
                                label=LABEL_AUDIO_PREVIEW_PAGE,
                                value=AUDIO_PREVIEW_PAGE_INITIAL,
                                interactive=False, elem_id='audio-preview-page-label')
                            audio_preview_next_btn = gr.Button(
                                LABEL_AUDIO_PREVIEW_NEXT, variant='secondary', scale=0,
                                elem_id='audio-preview-next-button')
                        audio_preview_gallery = gr.HTML(value=AUDIO_GALLERY_EMPTY_HTML,
                                                        elem_id='audio-preview-gallery')
                        # This tab's own hidden decision channel. The grid names it in `data-store`, so the
                        # client writes HERE and never into the Audio Cutter's field -- which is the whole
                        # reason the grid carries that attribute.
                        audio_preview_decisions = gr.Textbox(
                            value='', label=' ', elem_id='audio-preview-decisions')
                        audio_preview_confirm_btn = gr.Button(
                            LABEL_AUDIO_CONFIRM, variant='primary',
                            elem_id='audio-preview-confirm-button',
                            elem_classes=['r7-release-audio'])
                        with gr.Row():
                            audio_preview_proceed_btn = gr.Button(
                                LABEL_AUDIO_PROCEED, variant='stop', visible=False,
                                elem_id='audio-preview-proceed-button',
                                elem_classes=['r7-release-audio'])
                            audio_preview_sweep_cancel_btn = gr.Button(
                                LABEL_AUDIO_SWEEP_CANCEL, variant='secondary', visible=False,
                                elem_id='audio-preview-sweep-cancel-button')
                        # Per-browser-session, exactly like the cutter's. The decision and name records are
                        # keyed by file identity, which is what makes paging lossless.
                        audio_preview_state = gr.State({})
                        audio_preview_armed_state = gr.State('')

                    with gr.Tab(LABEL_SECTION_PREPARE_VERIFY, id='tab-prepare-verify'):
                        # [FORK] Digital-Union (P V1): media library preparation. An occasional maintenance
                        # workflow, not part of the render flow. Nothing in here is wired to
                        # `source_outputs`, so it cannot touch the Create Music Video gate.
                        #
                        # It stays collapsed (pinned by test) even though it now has its own tab, and it is
                        # deliberately the LAST block in that tab: nothing containing a `gr.Slider` may sit
                        # between the sample UI and the event wiring, which is the span
                        # `test_no_frame_timeout_or_budget_control_exists_in_the_ui` slices to prove the
                        # approved sample resource policy is not widenable from the GUI.
                        #
                        # (GUI Refresh A note: this comment used to describe the section as "rather than a
                        # restructuring of the app into tabs". That restructuring has since happened, and
                        # this block was one of the things it moved.)
                        # [FORK] R8 UX Refresh (Commit A): OPEN, where it used to be collapsed.
                        #
                        # Collapsing it was right while it shared the Advanced Tools tab with
                        # Audio Layers and Smart Mix -- three long blocks in one column, so each
                        # one being shut is what made the tab readable. It is now the whole of its
                        # own Media Library section, so collapsing it means an operator who
                        # deliberately navigated to Prepare & Verify is shown a single closed
                        # drawer and nothing else. That is precisely the discoverability problem
                        # this refresh exists to fix, measured in the running app.
                        with gr.Accordion(label=LABEL_PREP_SECTION, open=True):
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

                            # [FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source verification.
                            # Inside this accordion because it asks about the same library folder, and
                            # below Analyze because it is the "did my files change / are they still OK"
                            # question you ask around a preparation run rather than part of one.
                            #
                            # Like the rest of this section it is absent from `source_outputs` and from
                            # `prep_outputs`: it cannot touch the Create Music Video gate, and it cannot
                            # move the preparation state machine. It is also ALWAYS interactive -- the
                            # render mutex, not a disabled button, is what prevents it running during a
                            # render, because a button's interactivity is per-session UX while the mutex is
                            # process-global truth.
                            gr.Markdown(INFO_PREP_VERIFY)
                            prep_verify_btn = gr.Button(
                                LABEL_PREP_VERIFY, variant='secondary',
                                elem_id='prep-verify-button',
                            )
                            prep_verify_report = gr.Textbox(
                                label=LABEL_PREP_VERIFY_REPORT,
                                value=PREP_VERIFY_INITIAL_TEXT,
                                interactive=False,
                                lines=12,
                                max_lines=24,
                                elem_id='prep-verify-report-box',
                            )

                            # [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the one-video sample decode.
                            # Immediately after Verify Sources because it is the natural follow-up
                            # question -- "the metadata reads fine, but can FFmpeg actually produce frames
                            # here?" -- and it is deliberately a DIFFERENT question, on one file rather
                            # than the whole library.
                            #
                            # Like the rest of this accordion it is absent from `source_outputs` and
                            # `prep_outputs`: it cannot touch the Create Music Video gate and cannot move
                            # the preparation state machine. Also ALWAYS interactive, for the same reason
                            # Verify Sources is -- the render mutex, not a disabled button, is what
                            # prevents it running during a render, because interactivity is per-session UX
                            # while the mutex is process-global truth.
                            #
                            # One file, one position, one explicit button. No dropdown of the library (a
                            # ~2,000-source folder on a mechanical drive would have to be enumerated to
                            # build it), no browser upload (it would copy large media into a temp folder),
                            # no frame-count control and no timeout control.
                            gr.Markdown(INFO_PREP_SAMPLE)
                            sample_path = gr.Textbox(
                                label=LABEL_PREP_SAMPLE_PATH,
                                placeholder=PLACEHOLDER_SAMPLE_VIDEO,
                                elem_id='prep-sample-path',
                            )
                            # Seconds, never a frame number: the diagnostic's own contract forbids
                            # frames/fps arithmetic, and the selected stream's reported duration is what
                            # decides whether a position is out of range.
                            #
                            # [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2, runtime R1 MINOR-1): deliberately
                            # NO `minimum`. It carried `minimum=0` on the theory that a widget bound is a
                            # client-side courtesy while `run_sample_check` owns the real rejection. GUI
                            # runtime acceptance measured that to be false: on the pinned gradio==6.19.0,
                            # `Number.preprocess` calls `raise_if_out_of_bounds` SERVER-SIDE, so `-5` raised
                            # `gradio.exceptions.Error: 'Value -5 is less than minimum value 0.'` before
                            # `_on_sample_decode_click` was ever entered. The operator saw a framework error
                            # toast instead of the diagnostic's own report, and the core's
                            # finite-and-non-negative branch was unreachable from this widget.
                            #
                            # Removing the bound hands the decision back to the accepted core, which refuses
                            # a negative or non-finite seek as `SAMPLE_INVALID_REQUEST` **before any stat,
                            # FFprobe or FFmpeg child** -- so this widens no resource policy and starts no
                            # work. No GUI-side validation, clamping or normalisation is added here: a
                            # second opinion about what a valid seek is would be exactly the duplication
                            # this integration exists to avoid.
                            sample_seek = gr.Number(
                                value=1.0,
                                step=0.5,
                                label=LABEL_PREP_SAMPLE_SEEK,
                                elem_id='prep-sample-seek',
                            )
                            sample_btn = gr.Button(
                                LABEL_PREP_SAMPLE, variant='secondary',
                                elem_id='prep-sample-button',
                            )
                            sample_report = gr.Textbox(
                                label=LABEL_PREP_SAMPLE_REPORT,
                                value=PREP_SAMPLE_INITIAL_TEXT,
                                interactive=False,
                                lines=14,
                                max_lines=28,
                                elem_id='prep-sample-report-box',
                            )

            # [FORK] Digital-Union (R8 UX Refresh, Commit A): the Settings tab.
            #
            # The remembered-folder defaults used to sit at the bottom of the Clips Preview tab,
            # which is where they were asked for but not where anyone looks for settings. The
            # panel below is the pre-existing one, re-parented verbatim: the same widgets, the
            # same `elem_id`s, the same two handlers, and the same contract -- saving writes
            # `state/preferences.json` and nothing else, no directory is created, nothing is
            # scanned and nothing is moved.
            with gr.Tab(LABEL_TAB_SETTINGS, id='tab-settings'):
                gr.Markdown(INFO_TAB_SETTINGS)
                # [UX V4] Remembered folders. Deliberately placed here rather than in a settings
                # tab of its own: these are the folders this tab asks for, so the operator sets
                # them where they already are. Saving writes `state/preferences.json` and nothing
                # else -- no directory is created, nothing is scanned and nothing is moved.
                # Deliberately a plain Group, not a collapsible panel -- an operator should be
                # able to see what their destinations are set to without opening anything.
                #
                # [FORK] R8 Final Polish: the layout, and ONLY the layout, changed here. Every
                # field keeps its widget name, its `elem_id`, its preference key and its saved
                # value; `_on_save_folder_prefs` and `_on_reset_folder_prefs` are untouched, so a
                # `state/preferences.json` written by any earlier build still loads unchanged.
                #
                # Two things were wrong. Browse buttons were collected into their own Row BELOW a
                # Row of two text fields, so a button sat under the gap between the two boxes and
                # read as belonging to neither. And nine paths arrived as one undifferentiated
                # list. So: one field per Row with its OWN Browse beside it -- the pattern the
                # render and raw-cutter fields already used -- and three purpose groups.
                with gr.Group():
                    gr.Markdown(f"### {LABEL_PREF_SECTION}\n\n{INFO_PREF_SECTION}")

                    gr.Markdown(f"**{LABEL_PREF_GROUP_VIDEO}**")
                    # [FORK] R8: where finished renders go. The one destination that used to be a
                    # fixed property of the installation; see `get_render_output_dir`.
                    with gr.Row():
                        pref_render_output = gr.Textbox(
                            label=LABEL_PREF_RENDER_OUTPUT,
                            value=_pref(fork_prefs.RENDER_OUTPUT),
                            info=INFO_PREF_RENDER_OUTPUT,
                            placeholder=r"J:\Finished Videos",
                            elem_id='pref-render-output')
                        pref_render_output_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-render-output-browse')
                    with gr.Row():
                        pref_raw_output = gr.Textbox(
                            label=LABEL_PREF_RAW_OUTPUT,
                            value=_pref(fork_prefs.RAW_CUTTER_OUTPUT),
                            placeholder=r"J:\Raw Parts",
                            elem_id='pref-raw-output')
                        pref_raw_output_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-raw-output-browse')

                    gr.Markdown(f"**{LABEL_PREF_GROUP_AUDIO}**")
                    # [FORK] R7: the Audio Cutter's output, remembered the same way.
                    with gr.Row():
                        pref_audio_output = gr.Textbox(
                            label=LABEL_PREF_AUDIO_OUTPUT,
                            value=_pref(fork_prefs.AUDIO_CUTTER_OUTPUT),
                            placeholder=r"J:\Voice Parts",
                            elem_id='pref-audio-output')
                        pref_audio_output_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-audio-output-browse')

                    gr.Markdown(
                        f"**{LABEL_PREF_GROUP_REVIEW}**\n\n{INFO_PREF_GROUP_REVIEW}")
                    with gr.Row():
                        pref_library = gr.Textbox(
                            label=LABEL_CLIPS_LIBRARY,
                            value=_pref(fork_prefs.CLIPS_LIBRARY),
                            placeholder=r"J:\Media Library",
                            elem_id='pref-library')
                        pref_library_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-library-browse')
                    with gr.Row():
                        pref_rejected = gr.Textbox(
                            label=LABEL_CLIPS_REJECTED,
                            value=_pref(fork_prefs.CLIPS_REJECTED),
                            placeholder=r"J:\Discarded",
                            elem_id='pref-rejected')
                        pref_rejected_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-rejected-browse')
                    with gr.Row():
                        pref_audio_library = gr.Textbox(
                            label=LABEL_PREF_AUDIO_LIBRARY,
                            value=_pref(fork_prefs.AUDIO_LIBRARY),
                            placeholder=r"J:\Voice Library",
                            elem_id='pref-audio-library')
                        pref_audio_library_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-audio-library-browse')
                    with gr.Row():
                        pref_audio_discarded = gr.Textbox(
                            label=LABEL_PREF_AUDIO_DISCARDED,
                            value=_pref(fork_prefs.AUDIO_DISCARDED),
                            placeholder=r"J:\Voice Discarded",
                            elem_id='pref-audio-discarded')
                        pref_audio_discarded_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-audio-discarded-browse')
                    # [FORK] R7.2: the Audio Preview tab's two destinations. Separate keys from the
                    # cutter's, because the folder a later review sorts into is a different
                    # decision from the folder a fresh cut files into.
                    with gr.Row():
                        pref_audio_preview_library = gr.Textbox(
                            label=LABEL_PREF_AUDIO_PREVIEW_LIBRARY,
                            value=_pref(fork_prefs.AUDIO_PREVIEW_LIBRARY),
                            placeholder=r"J:\Voice Library",
                            elem_id='pref-audio-preview-library')
                        pref_audio_preview_library_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-audio-preview-library-browse')
                    with gr.Row():
                        pref_audio_preview_discarded = gr.Textbox(
                            label=LABEL_PREF_AUDIO_PREVIEW_DISCARDED,
                            value=_pref(fork_prefs.AUDIO_PREVIEW_DISCARDED),
                            placeholder=r"J:\Voice Discarded",
                            elem_id='pref-audio-preview-discarded')
                        pref_audio_preview_discarded_browse = gr.Button(
                            LABEL_BROWSE_FOLDER, variant='secondary', scale=0,
                            elem_id='pref-audio-preview-discarded-browse')

                    with gr.Row():
                        pref_save_btn = gr.Button(
                            LABEL_PREF_SAVE, variant='primary', elem_id='pref-save-button')
                        pref_reset_btn = gr.Button(
                            LABEL_PREF_RESET, variant='secondary', elem_id='pref-reset-button')
                    pref_status = gr.Textbox(
                        label=LABEL_PREF_STATUS,
                        value=_FOLDER_PREFS_NOTICE or PREF_STATUS_INITIAL,
                        interactive=False, lines=3, max_lines=6,
                        elem_id='pref-status-box')

                # [FORK] R8 UX Refresh: the filename-collision policy. Saved with the folder
                # defaults because it belongs to the same Save button and the same file -- see
                # `user_prefs.OPTION_KEYS` for why it is a separate section of that file.
                with gr.Group():
                    gr.Markdown(f"### {LABEL_COLLISION_SECTION}\n\n{INFO_COLLISION_POLICY}")
                    pref_collision = gr.Radio(
                        choices=[(CHOICE_COLLISION_SKIP, fork_collision.SKIP),
                                 (CHOICE_COLLISION_SUFFIX, fork_collision.SUFFIX)],
                        value=fork_collision.resolve_policy(
                            _option(fork_prefs.COLLISION_POLICY)),
                        label=LABEL_COLLISION_SECTION,
                        show_label=False,
                        elem_id='pref-collision-policy')

                # [FORK] R8 UX Refresh: Cache Management.
                #
                # Every numeric read-out here is text, and the one interactive numeric control
                # this section might have wanted does not exist, which is not an aesthetic
                # choice: the whole remainder of the source after `INFO_PREP_SAMPLE` is the span
                # `test_media_sample.py` scans to prove the approved sample resource policy is
                # not widenable from the GUI, and it enforces that by OMISSION of one widget
                # type. Settings is the last tab, so everything here is inside that span. The
                # guard is a substring check, so this comment does not name the type either.
                #
                # [FORK] R8 Final Polish: collapsed. This is the one block on the tab an operator
                # does NOT set up once and leave alone -- it is occasional maintenance with a
                # destructive button in it, so it opens on request rather than greeting everyone
                # who came to change a folder. The deletion logic, the selection choices, the
                # confirmation checkbox and the report are all unchanged; only the container is.
                with gr.Accordion(label=LABEL_CACHE_SECTION, open=False):
                    gr.Markdown(INFO_CACHE_SECTION)
                    with gr.Row():
                        cache_inspect_btn = gr.Button(
                            LABEL_CACHE_INSPECT, variant='secondary',
                            elem_id='cache-inspect-button')
                    cache_select = gr.CheckboxGroup(
                        choices=[(LABEL_CACHE_POSTERS, fork_cache.POSTERS),
                                 (LABEL_CACHE_PROCESSING, fork_cache.PROCESSING),
                                 (LABEL_CACHE_UPLOADS, fork_cache.RETIRED_UPLOADS),
                                 (LABEL_CACHE_ANALYSIS, fork_cache.ANALYSIS)],
                        value=[],
                        label=LABEL_CACHE_SELECT,
                        elem_id='cache-select')
                    gr.Markdown(
                        f"- **{LABEL_CACHE_POSTERS}** — {INFO_CACHE_POSTERS}\n"
                        f"- **{LABEL_CACHE_PROCESSING}** — {INFO_CACHE_PROCESSING}\n"
                        f"- **{LABEL_CACHE_UPLOADS}** — {INFO_CACHE_UPLOADS}\n"
                        f"- **{LABEL_CACHE_ANALYSIS}** — {INFO_CACHE_ANALYSIS}")
                    cache_confirm = gr.Checkbox(
                        value=False, label=LABEL_CACHE_CONFIRM, info=INFO_CACHE_CONFIRM,
                        elem_id='cache-confirm')
                    cache_clear_btn = gr.Button(
                        LABEL_CACHE_CLEAR, variant='stop', elem_id='cache-clear-button')
                    cache_report = gr.Textbox(
                        label=LABEL_CACHE_REPORT,
                        value='',
                        placeholder=PLACEHOLDER_CACHE_REPORT,
                        interactive=False, lines=10, max_lines=24,
                        elem_id='cache-report-box')

        # [UX V4] One helper, six folder fields plus the three saved defaults. Each button sends
        # its own textbox in and writes the same textbox back, so Browse is self-contained: it
        # starts where the field already points and cannot touch anything else. Cancel returns
        # `gr.update()`, which leaves the value exactly as it was.
        for _browse_button, _target in (
                (clips_input_browse, clips_input),
                (clips_library_browse, clips_library),
                (clips_rejected_browse, clips_rejected),
                (cut_batch_browse, cut_batch_folder),
                (cut_output_browse, cut_output),
                (source_folder_browse, source_folder),
                (pref_library_browse, pref_library),
                (pref_rejected_browse, pref_rejected),
                (pref_raw_output_browse, pref_raw_output),
                # [FORK] R7: the Audio Cutter's folder fields use the same self-contained helper.
                (audio_output_browse, audio_output),
                (audio_library_browse, audio_library_folder),
                (audio_discarded_browse, audio_discarded_folder),
                (pref_audio_library_browse, pref_audio_library),
                (pref_audio_discarded_browse, pref_audio_discarded),
                (pref_audio_output_browse, pref_audio_output),
                # [FORK] R7.2: the Audio Preview tab's folder fields, including its native
                # Browse Folder button, reach the same self-contained helper. Browsing still
                # scans nothing -- it only fills a box.
                (audio_preview_browse, audio_preview_folder),
                (audio_preview_library_browse, audio_preview_library),
                (audio_preview_discarded_browse, audio_preview_discarded),
                (pref_audio_preview_library_browse, pref_audio_preview_library),
                (pref_audio_preview_discarded_browse, pref_audio_preview_discarded),
                # [FORK] R8: the Render output destination uses the identical helper, so Browse
                # behaves the same here as on every other folder field in the app.
                (pref_render_output_browse, pref_render_output)):
            _browse_button.click(fn=_browse_folder, inputs=[_target], outputs=[_target],
                                 concurrency_limit=None)

        # [FORK] R7: the one FILE field, which needs the file dialog rather than the folder one.
        audio_source_browse.click(fn=_browse_audio_source, inputs=[audio_source],
                                  outputs=[audio_source], concurrency_limit=None)


        pref_save_btn.click(
            fn=_on_save_folder_prefs,
            inputs=[pref_library, pref_rejected, pref_raw_output,
                    clips_input, cut_batch_folder, source_folder,
                    # [FORK] R7: appended at the END so no existing positional index moves.
                    pref_audio_library, pref_audio_discarded, pref_audio_output, audio_source,
                    # [FORK] R7.2: appended after those, for the same reason.
                    audio_preview_folder, pref_audio_preview_library,
                    pref_audio_preview_discarded,
                    # [FORK] R8: appended after those, for the same reason again.
                    pref_render_output, pref_collision],
            outputs=[pref_status],
        )
        pref_reset_btn.click(
            fn=_on_reset_folder_prefs,
            inputs=None,
            outputs=[pref_library, pref_rejected, pref_raw_output,
                     pref_audio_library, pref_audio_discarded, pref_audio_output,
                     pref_audio_preview_library, pref_audio_preview_discarded,
                     # [FORK] R8: the render destination clears with the rest, and the policy
                     # returns to its default. Both are APPENDED before the status box, and the
                     # handler's return tuple matches this order positionally.
                     pref_render_output, pref_collision,
                     pref_status],
        )

        # [FORK] R8 UX Refresh: the collision policy applies and persists the moment it is picked.
        # `outputs=[]` is exact and load-bearing -- feedback is a toast so this cannot become a
        # third writer of `pref_status`. `.change()` rather than `.input()` because the Reset
        # button writes this radio programmatically and the stored value must follow it.
        pref_collision.change(
            fn=_on_collision_policy_change,
            inputs=[pref_collision],
            outputs=[],
            concurrency_limit=None,
        )

        # [FORK] R8 UX Refresh: Cache Management. Two read-mostly events, neither of which is a
        # render: no `_RENDER_LOCK`, no render lane, and `concurrency_limit=None` for the same
        # measured R3 reason every other non-render registration here carries -- on the pinned
        # gradio 6.19.0 a full concurrency group leaves an event QUEUED rather than refusing it.
        #
        # Both write only `cache_report`. Neither touches a source, preparation, creative or
        # render widget, so inspecting or clearing a cache cannot invalidate a confirmed source
        # set or change what the next render would do.
        cache_inspect_btn.click(
            fn=_on_inspect_cache,
            inputs=[],
            outputs=[cache_report],
            concurrency_limit=None,
        )
        cache_clear_btn.click(
            fn=_on_clear_cache,
            inputs=[cache_select, cache_confirm],
            outputs=[cache_report],
            concurrency_limit=None,
        )

        # [FORK] Digital-Union (R7 Raw Audio Cutter V1): five registrations, all isolated.
        #
        # None of them writes `status_output`, `video_output`, `process_btn`, `source_state`, any
        # element of `source_outputs` or `prep_outputs`, any creative widget, any Raw Cutter widget
        # or any render state -- so cutting audio cannot clear a confirmed source set, change what
        # a render would do, or disturb a cutter job.
        #
        # `concurrency_limit=None` on every one, for the same measured S2-A R3 reason the Clips
        # Preview registrations carry: on the pinned gradio 6.19.0 a full concurrency group leaves
        # the event QUEUED rather than delivering it. None of these takes `_RENDER_LOCK` either --
        # cutting a voice memo is not a render.
        audio_analyze_btn.click(
            fn=_on_audio_analyze,
            inputs=[audio_source, audio_threshold, audio_min_silence, audio_pad,
                    audio_min_clip, audio_format],
            outputs=[audio_plan, audio_table],
            concurrency_limit=None,
        )
        audio_cut_btn.click(
            fn=_on_audio_cut,
            # [FORK] R8 Phase 9: `audio_fade_edges` is APPENDED last, before the injected
            # `gr.Request`, so every pre-existing positional index is unchanged. Analyze is
            # deliberately NOT given it: fades change what is written, never where a clip begins
            # or ends, so the plan on screen is identical either way and adding it there would
            # imply the planner had changed.
            inputs=[audio_source, audio_output, audio_threshold, audio_min_silence,
                    audio_pad, audio_min_clip, audio_format, audio_fade_edges],
            outputs=[audio_report, audio_table, audio_gallery, audio_counts,
                     audio_decisions, audio_review_state, audio_run_dir_state],
            concurrency_limit=None,
        )
        # [R7.1] ONE filing action, as the Clips Preview has. The browser contributes a decision
        # and a name per clip id through `audio_decisions`; every path comes from the server's own
        # frozen page, so the page cannot name a file to move.
        #
        # [R7.2] Confirm, Proceed and Cancel all write the SAME seven outputs through the same
        # core, which is what keeps the sweep gate honest: there is no path on which Proceed can
        # move a file that Confirm would not have moved, because Proceed *is* Confirm with the
        # batch's signature supplied.
        _audio_confirm_inputs = [audio_decisions, audio_library_folder, audio_discarded_folder,
                                 audio_review_state, audio_armed_state]
        _audio_confirm_outputs = [audio_file_report, audio_gallery, audio_counts,
                                  audio_review_state, audio_armed_state,
                                  audio_proceed_btn, audio_sweep_cancel_btn]
        audio_confirm_btn.click(
            fn=_on_audio_confirm,
            inputs=_audio_confirm_inputs,
            outputs=_audio_confirm_outputs,
            concurrency_limit=None,
        )
        audio_proceed_btn.click(
            fn=_on_audio_proceed,
            inputs=_audio_confirm_inputs,
            outputs=_audio_confirm_outputs,
            concurrency_limit=None,
        )
        audio_sweep_cancel_btn.click(
            fn=_on_audio_cancel,
            inputs=[audio_review_state],
            outputs=_audio_confirm_outputs,
            concurrency_limit=None,
        )
        # [R7.2 Phase 4] Immediate destination validation. Both fields reach the same handler and
        # it writes ONE read-only textbox -- never the gallery, never a decision, never a folder.
        #
        # Registered explicitly per widget rather than in a loop, which is the convention the
        # Freestyle dropdowns already follow and for the same reason: a loop body produces
        # `_field.change(...)` in the syntax tree, so every per-widget seam assertion in the suite
        # becomes unable to find the binding and the guard silently stops guarding.
        audio_library_folder.change(
            fn=_on_audio_destinations_change,
            inputs=[audio_library_folder, audio_discarded_folder, audio_review_state],
            outputs=[audio_destination_status],
            concurrency_limit=None,
        )
        audio_discarded_folder.change(
            fn=_on_audio_destinations_change,
            inputs=[audio_library_folder, audio_discarded_folder, audio_review_state],
            outputs=[audio_destination_status],
            concurrency_limit=None,
        )
        # [R7.2 Phase 2] "Open results in Media Library" fills Audio Review's folder box and
        # selects it. It scans nothing, which is the requirement: no folder is read as a side
        # effect of anything but a deliberate Scan.
        # [R8 Commit A] Two tab containers in `outputs`, because the destination is now a section
        # inside Media Library -- see the handler's docstring for why one is not enough.
        audio_open_preview_btn.click(
            fn=_on_audio_open_in_library,
            inputs=[audio_run_dir_state],
            outputs=[audio_preview_folder, main_tabs, library_tabs, audio_file_report],
            concurrency_limit=None,
        )

        # [FORK] Digital-Union (R7.2 Phase 2): the Audio Preview tab's six registrations.
        #
        # Same isolation as the Audio Cutter's: none of them writes `status_output`,
        # `video_output`, `process_btn`, `source_state`, any element of `source_outputs` or
        # `prep_outputs`, any creative widget, any Raw Cutter widget or any render state. None
        # takes `_RENDER_LOCK` and none joins a concurrency group, for the measured S2-A R3 reason
        # the Clips Preview registrations carry.
        #
        # All six write the same nine outputs, assembled by `_audio_preview_outputs`, so the
        # positional contract between six handlers and this list lives in one function.
        _audio_preview_outputs_list = [
            audio_preview_report, audio_preview_gallery, audio_preview_counts,
            audio_preview_state, audio_preview_page_label, audio_preview_armed_state,
            audio_preview_proceed_btn, audio_preview_sweep_cancel_btn,
            audio_preview_destination_status,
        ]
        audio_preview_scan_btn.click(
            fn=_on_audio_preview_scan,
            inputs=[audio_preview_folder, audio_preview_recursive, audio_preview_page_size,
                    audio_preview_library, audio_preview_discarded, audio_preview_state],
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        audio_preview_prev_btn.click(
            fn=_on_audio_preview_prev,
            inputs=[audio_preview_decisions, audio_preview_page_size, audio_preview_library,
                    audio_preview_discarded, audio_preview_state],
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        audio_preview_next_btn.click(
            fn=_on_audio_preview_next,
            inputs=[audio_preview_decisions, audio_preview_page_size, audio_preview_library,
                    audio_preview_discarded, audio_preview_state],
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        _audio_preview_confirm_inputs = [
            audio_preview_decisions, audio_preview_library, audio_preview_discarded,
            audio_preview_page_size, audio_preview_state, audio_preview_armed_state]
        audio_preview_confirm_btn.click(
            fn=_on_audio_preview_confirm,
            inputs=_audio_preview_confirm_inputs,
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        audio_preview_proceed_btn.click(
            fn=_on_audio_preview_proceed,
            inputs=_audio_preview_confirm_inputs,
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        audio_preview_sweep_cancel_btn.click(
            fn=_on_audio_preview_cancel,
            inputs=[audio_preview_library, audio_preview_discarded, audio_preview_state],
            outputs=_audio_preview_outputs_list,
            concurrency_limit=None,
        )
        # Explicitly per widget, for the seam-visibility reason recorded above the cutter's pair.
        audio_preview_library.change(
            fn=_on_audio_preview_destinations_change,
            inputs=[audio_preview_library, audio_preview_discarded, audio_preview_state],
            outputs=[audio_preview_destination_status],
            concurrency_limit=None,
        )
        audio_preview_discarded.change(
            fn=_on_audio_preview_destinations_change,
            inputs=[audio_preview_library, audio_preview_discarded, audio_preview_state],
            outputs=[audio_preview_destination_status],
            concurrency_limit=None,
        )

        # [FORK] Digital-Union: source-mode / scan / confirm wiring.
        #
        # Every one of these events routes through beatsync_fork.input_session, so any source change
        # clears the confirmation and disables Create Music Video. Note which inputs are absent:
        # FPS, encoder, output filename and audio are never wired here, so changing them cannot
        # invalidate a source confirmation — they are not source-video identity.
        # [FORK] Digital-Union (Raw Cutter V1): three registrations, and what they write is the
        # whole isolation story.
        #
        # Analyze writes the summary, the table and the plan state. Cut writes the report. Cancel
        # writes the report. NONE of them writes `status_output`, `video_output`, `process_btn`,
        # `source_state`, any element of `source_outputs` or `prep_outputs`, any creative widget,
        # or any render state -- so no Raw Cutter action can clear a source confirmation, enable
        # or disable Create Music Video, move the preparation state machine or alter a render.
        #
        # Neither Analyze nor Cut joins `RENDER_CONCURRENCY_ID`, and both keep
        # `concurrency_limit=None`. That is the measured S2-A R3 rule rather than a preference: on
        # the pinned gradio 6.19.0 a full concurrency group leaves the event QUEUED instead of
        # delivering it, so the handler is never entered and the mutex that was supposed to refuse
        # it is never reached. A lane of 1 recreates the same defect one step in.
        #
        # Cancel gets its own lane for the same reason the render Cancel does: queuing a stop
        # behind the job it is meant to stop makes it useless.
        #
        # [FORK] Digital-Union (Raw Cutter progress P1): each of the three now also writes the
        # progress panel and the three button states, so there is exactly ONE writer of the
        # cutter's control surface per event and the states cannot be left inconsistent by a
        # handler that forgot one of them.
        cut_analyze_btn.click(
            fn=_on_cut_analyze,
            inputs=[cut_source, cut_sensitivity, cut_min_seconds, cut_max_seconds],
            outputs=[cut_summary, cut_table, cut_plan_state, cut_source_info, cut_progress,
                     cut_analyze_btn, cut_run_btn, cut_cancel_btn],
            concurrency_limit=None,
        )
        cut_run_btn.click(
            fn=_on_cut_run,
            inputs=[cut_plan_state, cut_source, cut_output, cut_sensitivity,
                    cut_min_seconds, cut_max_seconds, cut_remove_audio, cut_rename_output,
                    cut_output_profile],
            outputs=[cut_report, cut_progress,
                     cut_analyze_btn, cut_run_btn, cut_cancel_btn],
            concurrency_limit=None,
        )
        cut_cancel_btn.click(
            fn=_on_cut_cancel,
            inputs=[],
            outputs=[cut_report, cut_progress],
            concurrency_id=CUTTER_CANCEL_CONCURRENCY_ID,
            concurrency_limit=None,
        )
        # [R8 Commit A] "Open results in Media Library". Registered HERE, among the Raw Cutter's
        # own events, rather than beside the Video Review registrations -- deliberately, because
        # `test_gui_tabs.py::test_the_clips_tab_writes_no_render_or_source_widget` slices the span
        # bounded by the first Video Review registration and the first app-load hook, and pins the
        # registration count inside it. This is a Raw Cutter action that happens to fill two Video
        # Review widgets, so it belongs to the cutter's event block and must stay out of that span.
        #
        # Note the wording above avoids spelling either boundary token literally: that guard finds
        # its span with `str.index`, so a comment naming the anchor becomes the match and silently
        # relocates the span. This repository has now been bitten by prose tripping a substring
        # check six times; see `prose-mentioning-a-ban-trips-substring-bans`.
        #
        # It is not a render and not a cut: no `_RENDER_LOCK`, no cutter lane, and its own
        # unlimited concurrency lane for the measured R3 reason -- a lane of 1 would queue a
        # second click instead of letting it run.
        cut_open_library_btn.click(
            fn=_on_cut_open_in_library,
            inputs=[cut_output],
            outputs=[clips_input, clips_recursive, main_tabs, library_tabs, clips_report],
            concurrency_limit=None,
        )

        # [FORK] P1: a planning setting moved, so the plan on screen is no longer about this
        # source. Cut Video goes unavailable and says why. Presentation only -- `_on_cut_run`
        # still recomputes `plan_signature` and still refuses a stale plan by itself, which is
        # what protects an API caller and a second browser tab that never fired these events.
        # Nothing is queued here and the plan state is deliberately NOT cleared, so reverting the
        # setting and pressing Analyze again is cheap.
        for _planning_input in (cut_source, cut_sensitivity, cut_min_seconds, cut_max_seconds):
            _planning_input.change(
                fn=_on_cut_planning_input_change,
                inputs=[],
                outputs=[cut_run_btn, cut_progress],
                concurrency_limit=None,
            )

        # [FORK] Digital-Union (Raw Cutter Batch V1): four more registrations, and the same
        # isolation story. The mode switch writes four visibilities. Count files writes one
        # read-only box. Start Batch writes the live status and the final summary. Cancel Batch
        # writes the status. NONE of them writes `status_output`, `video_output`, `process_btn`,
        # `source_state`, any element of `source_outputs` or `prep_outputs`, any creative widget or
        # any render state -- and none writes a shared EXECUTION setting, so switching mode or
        # counting files cannot change what either workflow would do.
        #
        # Start Batch and Count files keep `concurrency_limit=None` and join no shared lane, for
        # the measured S2-A R3 reason: on the pinned gradio 6.19.0 a full concurrency group leaves
        # the event QUEUED rather than delivering it, so the handler is never entered and the
        # mutex that was meant to refuse it is never reached.
        #
        # Cancel Batch shares `CUTTER_CANCEL_CONCURRENCY_ID` with the single-video Cancel, which is
        # correct rather than convenient: both target the SAME one-slot `_cutter_job`, because
        # admission is the one shared mutex and only one cutter job can exist at a time.
        cut_mode.change(
            fn=_on_cut_mode_change,
            inputs=[cut_mode],
            outputs=[cut_single_inputs, cut_batch_inputs,
                     cut_single_actions, cut_batch_actions,
                     cut_single_readouts, cut_batch_readouts],
            concurrency_limit=None,
        )
        cut_batch_count_btn.click(
            fn=_on_cut_batch_count,
            inputs=[cut_batch_folder, cut_batch_recursive],
            outputs=[cut_batch_discovery],
            concurrency_limit=None,
        )
        cut_batch_start_btn.click(
            fn=_on_cut_batch_run_ui,
            inputs=[cut_batch_folder, cut_batch_recursive, cut_output, cut_sensitivity,
                    cut_min_seconds, cut_max_seconds, cut_remove_audio, cut_rename_output,
                    cut_output_profile],
            outputs=[cut_batch_status, cut_batch_summary, cut_batch_progress,
                     cut_batch_count_btn, cut_batch_start_btn, cut_batch_cancel_btn],
            concurrency_limit=None,
        )
        cut_batch_cancel_btn.click(
            fn=_on_cut_batch_cancel,
            inputs=[],
            outputs=[cut_batch_status, cut_batch_progress],
            concurrency_id=CUTTER_CANCEL_CONCURRENCY_ID,
            concurrency_limit=None,
        )

        source_outputs = [source_report, confirm_btn, confirm_status, process_btn, source_state]

        # [FORK] R8 Commit C: `source_folder` is PREPENDED to the outputs so "Use Media Library"
        # can fill it in. Prepended rather than appended purely so the two group-visibility
        # updates stay adjacent to each other; the handler's return tuple matches this order
        # positionally, as every handler on this gate must.
        source_mode.change(
            fn=_on_source_mode_change,
            inputs=[source_mode, source_state],
            outputs=[source_folder, folder_group, browser_group] + source_outputs,
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

        # [FORK] Digital-Union (GUI Refresh A): one registration on the TAB, not on the widgets.
        #
        # Reads eleven live values and writes exactly one read-only textbox. Binding it to the
        # tab's own `.select()` is the whole point: the six creative sliders carry exactly one
        # `.input()` registration each by pinned contract, and the Freestyle widgets write only
        # `freestyle_summary`. Hanging a `.change()` on each of them to feed a cosmetic read-out
        # would put a second binding on widgets whose single binding IS the contract, for no gain —
        # the summary is only ever visible while this tab is open, so recomputing it on selection
        # is both sufficient and incapable of going stale.
        #
        # Reads nothing from `gr.State`, writes no execution widget, and is absent from
        # `source_outputs` and `prep_outputs`: looking at a tab cannot invalidate a confirmed
        # source set or a prepared library.
        # [FORK] Digital-Union (GUI Refresh A / R1): the ten section dropdowns ride LAST so the
        # handler can take them as `*styles`, and in `_FREESTYLE_WIDGET_ORDER` — the same explicit
        # ordering the render wrappers use, because Gradio passes `inputs` positionally and a
        # reordering here would silently mislabel which section carries which rule.
        #
        # Note what is NOT in this list any more: the six creative sliders. R1 dropped the
        # "away from neutral" line they fed, because the Creative Preset selector on this very tab
        # already states that fact and cannot go stale (Balanced IS the all-neutral recipe, and the
        # selector is recomputed from the live sliders). Every remaining input lives in another
        # tab, so the `.select()` that fires on the way back here is sufficient to keep all of them
        # current — no creative control needs a second binding, and no event here can be stale.
        # [FORK] R8 Commit C: the six sliders LEAD the input list, in `creative_control_sliders`
        # order so the handler's six named parameters line up positionally with the one tuple the
        # preset label is computed from. The ten Freestyle dropdowns still ride LAST and are still
        # collected by `*styles`, so every other parameter keeps a fixed index.
        tab_render.select(
            fn=_on_render_tab_select,
            inputs=creative_control_sliders
            + [freestyle_enabled, variation_seed, voice_files, sfx_folder, sfx_roles,
               sfx_amount] + freestyle_section_dropdowns,
            outputs=[creative_profile_summary])

        # [FORK] R8 Commit C: a one-click route to the tab the summary talks about. Selecting a
        # tab is not a render: no `_RENDER_LOCK`, no lane, and it writes no setting -- `outputs`
        # is exactly the tab container, which is what `gr.Tabs(selected=...)` needs.
        open_creative_settings_btn.click(
            fn=lambda: gr.Tabs(selected='tab-creative-settings'),
            inputs=[],
            outputs=[main_tabs],
            concurrency_limit=None,
        )

        # Each widget refreshes the summary and writes NOTHING else. `.change()` is correct here
        # (unlike the preset/slider graph, which needs `.input()` to stay acyclic): nothing ever
        # writes a Freestyle widget programmatically, so there is no cycle to create — and a test
        # pins that absence. Registered explicitly per widget, not in a loop, so each binding is
        # visible to the per-widget seam assertions.
        # [FORK] Digital-Union (Random Freestyle V2): the one Freestyle registration that is not a
        # `.change()` and the one that writes the Variation Seed. It READS the eleven live widgets
        # and writes exactly two things — the seed and the summary — so no section dropdown is ever
        # written programmatically and the `.change()` graph above stays acyclic.
        surprise_btn.click(
            fn=_on_surprise_me, inputs=freestyle_live_inputs,
            outputs=[variation_seed, freestyle_summary])

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
        # Generate's `inputs` are exactly four, in this order:
        #
        #     director_instruction, prep_folder, prep_recursive, prep_state
        #
        # and Gradio passes them positionally, so the list and the handler signature are pinned
        # against each other by test. The preparation trio exists for ONE purpose — letting
        # `_eligible_media_summary` decide whether a current, fully prepared scan is available for
        # the deterministic Source Diversity step — and **none of it reaches the model**: no
        # summary, count, filename or record is interpolated into the argv, which a seam test
        # asserts against the real command line.
        #
        # They are LIVE widget values rather than `prep_state` alone, for the same reason
        # `prep_analyze_btn` takes them: a queued `change` event may not have run yet, so the stored
        # scan can lag the screen, and comparing the live declaration is what stops a proposal
        # adapting to the *previous* library's concentration while the screen declares another
        # folder.
        #
        # The invariant is therefore NOT "the instruction and nothing else" — it is that Generate
        # reads **no current creative state**. There is no hidden creative base, so the Director
        # cannot drift from its own last answer the way a transform-the-current-settings mode would.
        generate_director_btn.click(
            fn=_on_generate_director_proposal,
            inputs=[director_instruction, prep_folder, prep_recursive, prep_state],
            outputs=[director_proposal_state, director_proposal, director_status],
        )

        # [FORK] R8 Final Polish: Clear Creative Variations -- the SEVENTH writer of
        # `variation_seed`, and the matrix is extended by exactly one with its reason stated,
        # never relaxed to a containment check.
        #
        # It is legitimate for the same reason Randomize is: "no variation" is a seed value, and
        # 0 is the documented precision baseline rather than a magic number. It writes no slider
        # and no preset, so it adds nothing to either of those matrices -- a test asserts that
        # absence, because a reset that quietly moved the sliders would be a second preset.
        clear_variations_btn.click(
            fn=_on_clear_creative_variations,
            inputs=freestyle_section_dropdowns,
            outputs=[variation_seed, freestyle_enabled, freestyle_summary],
            concurrency_limit=None,
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
                output_filename, processing_mode, output_profile,
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

        # [FORK] Digital-Union (Deliverable Export V1-B-S2): the Open Output Folder button.
        #
        # `inputs=[]` and `outputs=[]` are both EXACT and both the contract. Nothing from the
        # screen decides which folder to open, and nothing on the screen is written: feedback is a
        # toast raised inside the handler. `status_output` already has three writers -- the two
        # render clicks and Cancel -- so naming it here would add a FOURTH, and the rule for this
        # button is that it adds NONE. Opening a folder must not reach a render's result panel.
        #
        # No `concurrency_id`: this is not a render and must never share the render lane, or it
        # would queue uselessly behind the render it is most useful during. No `cancels=` either --
        # a module-wide test forbids it.
        open_output_folder_btn.click(
            fn=_on_open_output_folder_click,
            inputs=[],
            outputs=[],
            show_progress='hidden',
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

        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source verification.
        #
        # Note what is absent from `inputs`: `prep_state`. Verification owns no session state and
        # reads no recorded scan -- its inputs are exactly the two LIVE widgets that define "which
        # library did you mean", so a queued `change` event can never make it verify the previous
        # folder.
        #
        # Note what is absent from `outputs`: everything except its own report box. Not
        # `status_output`, `video_output`, `process_btn`, `source_state`,
        # `render_invocation_state`, `director_proposal_state`, `variant_batch_state`, the render
        # outcome state, `prep_outputs` or any element of `source_outputs`. A verification click
        # therefore cannot enable a render, disable one, move the preparation state machine or
        # overwrite a render's status line.
        #
        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): NO `concurrency_id`, and
        # `concurrency_limit=None`.
        #
        # S2-A put this event in `RENDER_CONCURRENCY_ID` with `concurrency_limit=1` as "cooperative
        # UX". Measured on the pinned gradio==6.19.0, that is what broke the contract: a Verify
        # clicked during a render was QUEUED rather than refused -- `Queue.get_events()` skips a
        # group at its limit and leaves the event queued -- then ran automatically once the render
        # finished, against the folder captured at click time, and overwrote the newer
        # "folder changed" notice. Both busy messages were unreachable.
        #
        # Its own lane makes the handler always execute, so `_RENDER_LOCK` is reached and the
        # refusal is real and immediate. `concurrency_limit=None` (unlimited) rather than the
        # default of 1, because a lane of 1 would re-create the same defect one step in: a second
        # Verify click would queue behind the first instead of being refused by the mutex. A
        # refusal is microseconds of work, so unlimited here costs nothing -- the mutex, not the
        # lane, is what bounds the actual disk reads to one at a time.
        #
        # Deliberately NOT `CANCEL_CONCURRENCY_ID`: that lane belongs to Cancel and is untouched.
        prep_verify_btn.click(
            fn=_on_verify_sources_click,
            inputs=[prep_folder, prep_recursive],
            outputs=[prep_verify_report],
            show_progress='hidden',
            concurrency_limit=None,
        )
        # Separate `change` listeners rather than entries in `prep_outputs`: a changed folder makes
        # the old verification describe a different library snapshot, so it is visibly cleared --
        # but the preparation state machine's own folder/recursive handling is left exactly as it
        # was, sharing no outputs with these. They also bump this session's verification revision,
        # which is what keeps an in-flight verification from publishing over this notice.
        prep_folder.change(
            fn=_on_verify_invalidate,
            inputs=None,
            outputs=[prep_verify_report],
        )
        prep_recursive.change(
            fn=_on_verify_invalidate,
            inputs=None,
            outputs=[prep_verify_report],
        )

        # [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the sample-decode event, wired exactly like
        # the Verify event above and for the same MEASURED reason. No `concurrency_id`, because a
        # shared lane is what made S2-A's handler never reach its mutex -- Gradio's
        # `Queue.get_events()` skips a group at its limit and leaves the event queued, then runs it
        # automatically once the render finishes, against the values captured at click time.
        # `concurrency_limit=None` rather than the default of 1, because a lane of 1 would re-create
        # that defect one step in: a SECOND Sample Decode click would queue behind the first instead
        # of being refused by the mutex. A refusal is microseconds, so unlimited costs nothing --
        # the mutex, not the lane, is what bounds the disk reads to one at a time.
        #
        # Deliberately NOT `CANCEL_CONCURRENCY_ID`: that lane belongs to Cancel and is untouched.
        #
        # `request: gr.Request` is injected by annotation and is deliberately absent from `inputs`.
        sample_btn.click(
            fn=_on_sample_decode_click,
            inputs=[sample_path, sample_seek],
            outputs=[sample_report],
            show_progress='hidden',
            concurrency_limit=None,
        )
        # Separate `change` listeners, writing ONLY the sample textbox: a changed file or position
        # makes the old sample describe a different selection, so it is visibly cleared. They also
        # bump this session's SAMPLE revision, which keeps an in-flight sample from publishing over
        # this notice. Neither listener does any disk work.
        sample_path.change(
            fn=_on_sample_invalidate,
            inputs=None,
            outputs=[sample_report],
        )
        sample_seek.change(
            fn=_on_sample_invalidate,
            inputs=None,
            outputs=[sample_report],
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
                output_filename, processing_mode, output_profile, variation_seed,
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
                # [FORK] R7.1 (Comfort Mix): appended at the END of the list, matching the two
                # parameters appended at the end of `process_video_guarded`. Gradio passes these
                # positionally, so list order and signature order are one contract -- a seam test
                # pins them name-for-name.
                voice_auto_balance, voice_music_return,
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

        # [FORK] Digital-Union (R5 Clips Preview Selector, P1): four registrations, all isolated.
        #
        # None of them writes `status_output`, `video_output`, `process_btn`, `source_state`, any
        # element of `source_outputs` or `prep_outputs`, any creative or audio widget, any Raw
        # Cutter widget or any render state -- so reviewing clips cannot clear a confirmed source
        # set, change what a render would do, or disturb a cutter job.
        #
        # No `concurrency_id` and `concurrency_limit=None` on every one, for the measured S2-A R3
        # reason: on the pinned gradio 6.19.0 a full concurrency group leaves the event QUEUED
        # rather than delivering it. None of these takes `_RENDER_LOCK` either -- a read-only
        # gallery is not a render, and its poster pass is one short bounded FFmpeg read per clip
        # rather than the four concurrent reads D0 traced extraction timeouts to.
        #
        # `gr.Request` is injected and is NOT listed in `inputs`: the owner of a review session is
        # the server-side session hash, never a value the browser could supply.
        clips_check_btn.click(
            fn=_on_clips_check,
            inputs=[clips_input, clips_library, clips_rejected],
            outputs=[clips_report],
            concurrency_limit=None,
        )
        clips_scan_btn.click(
            fn=_on_clips_scan,
            inputs=[clips_input, clips_recursive, clips_library, clips_rejected,
                    clips_page_size],
            outputs=[clips_report, clips_gallery, clips_counts, clips_confirm_btn],
            concurrency_limit=None,
        )
        clips_refresh_btn.click(
            fn=_on_clips_refresh,
            inputs=[],
            outputs=[clips_gallery, clips_counts, clips_confirm_btn],
            concurrency_limit=None,
        )
        # [UX V3] Confirm now sorts the page, so it repaints what it changed: the report, the
        # grid (the next batch, or this one again if anything stayed), the counts and the button.
        # The sources are the registry page and the durable record -- the only widget value this
        # click sends is the batch signature, which cannot name a folder or a decision, so the
        # browser is still out of the decision about where a file goes.
        #
        # [FORK] R8 Final Polish: Confirm, Proceed and Cancel all write the SAME eight outputs
        # through the same helper, which is what keeps the sweep gate honest -- there is no path
        # on which Proceed can repaint something Confirm cannot, and no path on which a refusal
        # leaves the gate open. The same one-assembler discipline Audio Review uses.
        _clips_confirm_outputs = [clips_report, clips_gallery, clips_counts, clips_confirm_btn,
                                  clips_confirm_out, clips_proceed_btn, clips_sweep_cancel_btn,
                                  clips_armed_state]
        clips_confirm_btn.click(
            fn=_on_clips_confirm,
            inputs=[clips_armed_state],
            outputs=_clips_confirm_outputs,
            concurrency_limit=None,
        )
        # Proceed is the SAME handler. It is not a second sorting path: it re-derives the plan,
        # re-validates the destinations and compares the signature exactly as Confirm does -- the
        # only difference is that by now `clips_armed_state` holds a signature that matches, so
        # the gate falls through instead of being shown. A second entry point that skipped any of
        # those checks would be a way around them.
        clips_proceed_btn.click(
            fn=_on_clips_confirm,
            inputs=[clips_armed_state],
            outputs=_clips_confirm_outputs,
            concurrency_limit=None,
        )
        clips_sweep_cancel_btn.click(
            fn=_on_clips_sweep_cancel,
            inputs=[],
            outputs=_clips_confirm_outputs,
            concurrency_limit=None,
        )

        # The gallery's interaction layer. `fn=None` means this registers no server call: it only
        # installs the listeners once per page load.
        app.load(fn=None, inputs=None, outputs=None, js=CLIPS_GALLERY_JS)
        # [R7.1] The Audio Cutter gallery's own listeners. A separate registration rather than a
        # merged script: the two galleries share no behaviour beyond being grids of tiles, and one
        # combined blob would make either harder to change safely.
        app.load(fn=None, inputs=None, outputs=None, js=AUDIO_GALLERY_JS)

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
    # [FORK] Digital-Union (R5 Clips Preview Selector, P1): the clips media endpoint is attached
    # here, through the PUBLIC `app_kwargs` parameter, which `Blocks.launch` forwards to the
    # FastAPI constructor. That is the whole integration: no second Uvicorn, no helper that mounts
    # this app inside another one, no launcher replacement, no new dependency (Starlette already
    # ships with Gradio), and `allowed_paths` / `set_static_paths` / `blocked_paths` are untouched.
    # (The rejected alternatives are named in `.claude/rules/clips-preview.md` rather than here:
    # a sibling suite scans this file's raw text for them, so prose naming one would trip it.)
    #
    # It has to be a middleware rather than Gradio's own file route because Gradio serves only its
    # `created_paths` (`[upload_dir, cache_folder]`): an arbitrary operator media path is served
    # 403. Widening `allowed_paths` to a media root would expose that whole tree to every request
    # the browser can make, and copying a page of clips into Gradio's cache would duplicate
    # gigabytes to draw thumbnails. So R5 serves its own bytes under its own authorization.
    #
    # Localhost binding is unchanged.
    app.launch(
        server_name="127.0.0.1",
        server_port=launch_port,
        share=False,
        inbrowser=True,
        show_error=True,
        app_kwargs={
            'middleware': [
                Middleware(
                    ClipMediaMiddleware,
                    registry=_CLIPS_REGISTRY,
                    poster_resolver=_clips_poster_resolver,
                    decide_handler=_clips_decide,
                    state_handler=_clips_state,
                ),
                # [FORK] R7.2: the two audio review surfaces, each bound to its own registry at
                # construction time. The prefix-to-registry pairing is therefore a structural fact
                # rather than a lookup, so a token minted for one surface is simply not in the
                # other's map. Each claims one prefix and passes everything else through, so the
                # R5 middleware above and Gradio's own routes are untouched.
                Middleware(
                    AudioMediaMiddleware,
                    registry=_AUDIO_CUT_REGISTRY,
                    prefix=fork_audio_review.MEDIA_PREFIX_CUTTER,
                ),
                Middleware(
                    AudioMediaMiddleware,
                    registry=_AUDIO_PREVIEW_REGISTRY,
                    prefix=fork_audio_review.MEDIA_PREFIX_PREVIEW,
                ),
            ],
        },
    )
