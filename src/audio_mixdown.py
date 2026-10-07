#!/usr/bin/env python3
"""[FORK] Digital-Union: Audio Layers V1 — the runtime mixdown executor (D).

The runtime half of Audio Layers. :mod:`beatsync_fork.audio_mix` decides *where* speech lands and
*how deep* the music ducks; this module probes durations, turns a plan into one FFmpeg command, runs
the project's portable binary and verifies what came out. The dependency is one-way: this module
imports the pure one, never the reverse.

===============================================================================
The graph, and why each piece is there
===============================================================================

::

    aevalsrc='<envelope>':s=48000:c=stereo:d=<D>, aformat=fltp/stereo               -> [env]
    [0:a] aresample=48000, aformat=fltp/stereo                                     -> [mus]
    [mus][env] amultiply                                                           -> [music]
    [k:a] aresample=48000, aformat=fltp/stereo, adelay=<start_ms>:all=1             -> [vk]
    [music][v1]..[vN] amix=inputs=N+1:duration=longest:dropout_transition=0:normalize=0 -> [mixed]
    [mixed] alimiter=limit=0.97:attack=1:release=50:level=0:latency=1, apad,
            atrim=end=<D>, asetpts=N/SR/TB                                             -> [outa]
    -map [outa] -c:a pcm_s24le -ar 48000 -ac 2

* **``normalize=0``** — measured: ``normalize=1`` moved the same mix from -21.28 to -27.09 dBFS,
  because it divides by the input count. The music level would then quietly change every time a
  voice clip was added or removed.
* **The envelope is a generated audio stream multiplied into the music, not ``volume``.** The
  obvious ``volume=eval=frame:volume='<expr>'`` evaluates once per audio *frame*, which measured as
  a visible staircase: about five steps across the 250 ms attack, the largest a 0.22 linear jump
  (~2.2 dB) — audible zipper on sustained music. ``volume`` has no per-sample mode (``eval``
  accepts only ``once`` and ``frame``), so the envelope is instead synthesised by ``aevalsrc``,
  which *does* evaluate per sample, and applied with ``amultiply``. Measured maximum deviation from
  the pure reference fell from **0.2245 to 0.0101**, and a realistic 280 s track still mixes in
  2.3 s.
* **The expression is built from ``max()`` of per-event duck amounts**, so overlapping ramps
  resolve to the deepest duck. A chain of ``if()`` would instead let whichever event matched first
  win, which is order-dependent and wrong.
* **``alimiter`` is a safety ceiling, not loudness normalisation.** Measured on this portable
  build: full-scale music plus a full-scale voice clips **7.9% of samples** without it. With it,
  zero. ``latency=1`` is load-bearing — without it the limiter delays the whole master by 47 samples
  (0.979 ms); with it, impulses land on exactly the planned sample, and a non-clipping fixture comes
  out **byte-identical** to the unlimited mix.
* **``apad`` then ``atrim``** guarantees the master is exactly the music's duration: pad if the mix
  somehow ran short, cut if the limiter's release or a voice tail ran long. Measured delta 0.0000 ms
  across one voice, three voices and a voice ending at the music's end.

``aresample`` on every input is what lets voice clips be any sample rate; ``adelay`` is
sample-exact (measured shift 0).

===============================================================================
Failure is loud, never silent
===============================================================================

If the user supplied voice files, every failure path returns an :class:`AudioMixError` and the
render stops **before** any video clip is extracted. Falling back to the original music would
produce a plausible-looking video that silently lacks the voice the user asked for. The only path
that legitimately uses the original music untouched is "no voice files at all", and that branch
never reaches this module.

===============================================================================
Local cause, not batch classification (C3-R1B-a)
===============================================================================

:class:`AudioMixError` remains the base of everything this module raises, and four subclasses now
name *what* failed:

    AudioProbeError          media/duration probing failed
    AudioMixInputError       a user-supplied voice/SFX input is unusable
    AudioMixPlanError        voice placement could not produce a legal plan
    AudioMixExecutionError   producing or verifying the mixed master failed

**None of them implies a ``RenderOutcomeKind``**, and that separation is deliberate rather than
fastidious. This module does not know whether it is executing a single Create Music Video click,
candidate 1 of a C3 batch or candidate 4, and it must never learn -- so it cannot answer "would
every remaining candidate fail the same way?". `gui.py` owns that mapping, per call site, from
values it has frozen for the whole batch. Nothing here imports `RenderOutcomeKind`, and no caller
may recover a cause by reading an exception's message.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import time
import uuid

from ffmpeg_processing import FFMPEG_PATH, FFPROBE_PATH

from beatsync_fork import audio_mix as fork_audio_mix
from beatsync_fork import smart_mix as fork_smart_mix
# [FORK] Digital-Union (C3-R1A): the pure cancellation contract (stdlib-only fork module). This
# module keeps its own small cancellable runner below rather than importing
# ffmpeg_processing._run_media_command -- the process handle always belongs to the call frame that
# creates it, and the two runtime modules stay independent of each other for this.
from beatsync_fork.render_worker import RenderCancelled

#: The mixed master's format — identical to what final assembly already encodes, so the mux has no
#: new work to do and there is no lossy intermediate.
MASTER_SAMPLE_RATE = 48000
MASTER_CHANNELS = 2
MASTER_CODEC = "pcm_s24le"

#: Safety ceiling only. `level=0` disables alimiter's auto output levelling (that would be
#: normalisation); `latency=1` compensates its lookahead so nothing shifts in time.
LIMITER_LIMIT = 0.97
LIMITER_ATTACK_MS = 1
LIMITER_RELEASE_MS = 50

#: How far the produced master may differ from the music. One PCM sample at 48 kHz is 0.021 ms, so
#: 1 ms is generous against ffprobe's reporting precision while still far below one video frame —
#: and the master's duration becomes authoritative for the frame timeline in `create_music_video`.
DURATION_TOLERANCE_SECONDS = 0.001

_PROBE_TIMEOUT_SECONDS = 30
_MIX_TIMEOUT_SECONDS = 600
#: Bounded so a loudly-failing FFmpeg cannot paste megabytes into a status panel.
_STDERR_TAIL = 1200


class AudioMixError(Exception):
    """Any Audio Layers failure. Carries a short, already-bounded human-readable reason.

    [FORK] Digital-Union (C3-R1B-a): **retained as the base**, so every pre-existing
    ``except audio_mixdown.AudioMixError`` keeps catching exactly what it caught before. The four
    subclasses below name *what failed locally*; they deliberately do **not** imply a
    ``RenderOutcomeKind``. See the module docstring section below for why that separation is
    load-bearing.
    """


# ---------------------------------------------------------------------------
# [FORK] Digital-Union (C3-R1B-a): local cause types. NOT batch classifications.
# ---------------------------------------------------------------------------
#
# A subclass here says what broke *in this module*. Whether that break is shared across a C3 batch,
# specific to one candidate, or simply unproven is a question about the *batch*, and this module has
# no way to answer it -- it does not know whether it is running a single render, candidate 1 or
# candidate 4, and it must not learn. So `gui.py` owns the mapping and nothing here imports
# `RenderOutcomeKind`.
#
# The measured reason this is not over-engineering: `probe_duration` has four production call sites,
# and the *same* four internal failures resolve to three *different* `RenderOutcomeKind`s depending
# on which caller invoked it --
#
#     prepare_voice_inputs   probing a selected voice clip     -> SHARED_FATAL
#     prepare_sfx_inputs     probing a selected SFX asset      -> CANDIDATE_LOCAL
#     render_mixed_master    probing the GENERATED master      -> UNKNOWN_FATAL
#     the gui.py defensive music fallback                      -> UNKNOWN_FATAL
#
# No exception type raised by `probe_duration` itself could carry that, which is exactly why
# `AudioProbeError` carries none and the wrapping callers re-type it instead.


class AudioProbeError(AudioMixError):
    """Media/duration probing failed. Context-neutral: carries no batch classification.

    Raised by :func:`probe_duration` for its own four failures (timeout, non-zero ffprobe,
    unparseable duration, unusable duration) and **nowhere else**. Callers that know what they were
    probing narrow it into one of the types below; the one caller that deliberately does not is
    `gui.py`'s defensive music-duration fallback, which maps it conservatively.
    """


class AudioMixInputError(AudioMixError):
    """A user-supplied voice or SFX input is unusable. Raised by the two preflights."""


class AudioMixPlanError(AudioMixError):
    """Voice placement could not produce a legal plan.

    Reachable only when voice clips exist, and placement feasibility reads only batch-frozen
    configuration -- see :func:`build_mixed_master`.
    """


class AudioMixExecutionError(AudioMixError):
    """Producing or verifying the mixed master failed.

    The artifact in question is this render's **own output**, never a user input -- which is why a
    failure probing the generated master is this type and explicitly not
    :class:`AudioMixInputError`.
    """


#: Same shape as ffmpeg_processing's poll/grace constants, deliberately a separate pair -- each
#: module owns its own small cancellable runner rather than sharing one.
_CANCEL_POLL_SECONDS = 0.15
_TERMINATE_GRACE_SECONDS = 5.0


def _run(command, timeout, lifecycle=None):
    """Run one FFmpeg/FFprobe command. ``lifecycle`` defaults to ``None`` (unchanged behaviour).

    [FORK] Digital-Union (C3-R1A): mirrors ``ffmpeg_processing._run_media_command``'s cancellable
    shape exactly, as its own independent implementation -- this module never imports that one, so
    the process handle it creates stays owned entirely within this call frame. A real timeout still
    raises ``subprocess.TimeoutExpired``; only a matching cancellation raises ``RenderCancelled``,
    and only after the child is provably reaped.
    """
    if lifecycle is None:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)

    started = time.perf_counter()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_SECONDS)
                return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if lifecycle.cancel_requested():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    raise RenderCancelled("audio mixdown command cancelled")
                if time.perf_counter() - started > timeout:
                    proc.kill()
                    proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    raise subprocess.TimeoutExpired(command, timeout)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except Exception:
                pass


def _tail(text) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed[-_STDERR_TAIL:]


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


def probe_duration(path: str, lifecycle=None) -> float:
    """Media duration in seconds, via the project's portable ffprobe.

    Deliberately raises rather than returning a fallback: ``ffmpeg_processing.get_video_duration``
    answers ``10.0`` when probing fails, which is right for its caller and catastrophic here — a
    guessed voice length would place speech at the wrong time and silently mis-duck the music.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` defaults to ``None`` (unchanged behaviour). Probes
    are short, so this is a boundary check (raise_if_cancelled before starting) plus pass-through,
    not fine-grained polling -- a cancelled render must not begin a new preflight unit.

    [FORK] Digital-Union (C3-R1B-a): all four failures raise :class:`AudioProbeError` -- a *narrower*
    type than before, so every pre-existing ``except AudioMixError`` still catches them and the
    messages are unchanged character for character. ``RenderCancelled`` from the boundary check or
    from ``_run`` still escapes untouched: the only ``except`` here remains the narrow
    ``subprocess.TimeoutExpired``, which a cancellation is not.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    command = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = _run(command, _PROBE_TIMEOUT_SECONDS, lifecycle=lifecycle)
    except subprocess.TimeoutExpired:
        raise AudioProbeError(f"Timed out reading the duration of {os.path.basename(path)}")
    if result.returncode != 0:
        raise AudioProbeError(
            f"Could not read {os.path.basename(path)}: {_tail(result.stderr) or 'ffprobe failed'}")
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise AudioProbeError(f"Could not read a duration from {os.path.basename(path)}")
    if duration != duration or duration in (float("inf"), float("-inf")) or duration <= 0.0:
        raise AudioProbeError(
            f"{os.path.basename(path)} reports an unusable duration ({result.stdout.strip()})")
    return duration


def _selected_voice_paths(voice_paths) -> list:
    """The user's selection as a list, **without dropping anything**.

    A bare string is one selection, not an iterable of characters. Nothing else is filtered here:
    deciding whether an entry is usable is validation's job, and silently shrinking the collection
    first is exactly the defect this function exists to prevent.
    """
    if voice_paths is None:
        return []
    if isinstance(voice_paths, (str, bytes, os.PathLike)):
        return [voice_paths]
    try:
        return list(voice_paths)
    except TypeError:
        return [voice_paths]


def prepare_voice_inputs(voice_paths) -> tuple:
    """Resolve, order and probe the voice clips — the cheap preflight, run *before* Stage 1.

    **Every selected entry must survive into validation.** A non-empty selection may never come
    back as a smaller valid subset: if the user picked three clips and the middle one has since
    been moved, the render must fail saying so, not quietly produce a two-voice video that looks
    deliberate. So the collection is taken whole (see :func:`_selected_voice_paths`), each entry is
    checked, and the *first* invalid one raises — the caller never receives a partial result.

    Ordering is applied only once the selection has been validated as a selection, and it is the
    pure module's deterministic path order rather than the browser's. Catching all of this before
    Stage 1 means a fixable input problem costs no analysis.

    [FORK] Digital-Union (C3-R1B-a): every failure is an :class:`AudioMixInputError` -- a statement
    about the user's selected files, which is what lets `gui.py` map this preflight to
    ``SHARED_FATAL`` for a C3 batch (the voice selection is frozen for the whole batch and no
    ``AudioRecipe`` field changes whether this runs or what it validates). A probe failure is
    wrapped **narrowly** so a ``RenderCancelled`` from inside ``probe_duration`` cannot be re-typed
    as the user's file being broken.
    """
    selected = _selected_voice_paths(voice_paths)
    if not selected:
        return ()

    for position, raw in enumerate(selected, start=1):
        if isinstance(raw, os.PathLike):
            raw = os.fspath(raw)
        if not isinstance(raw, str) or not raw.strip():
            raise AudioMixInputError(
                f"Voice clip {position} of {len(selected)} is not a usable file path")

    usable = [os.fspath(p) if isinstance(p, os.PathLike) else p for p in selected]
    ordered = fork_audio_mix.order_voice_paths(usable)
    if len(ordered) != len(usable):
        # Defensive: the loop above already guarantees every entry is a non-empty string, so this
        # can only fire if the ordering contract changes underneath us. Failing loudly is still
        # better than returning fewer clips than the user chose.
        raise AudioMixInputError(
            f"Could not order all {len(usable)} selected voice clips")

    prepared = []
    for index, path in enumerate(ordered):
        if not fork_audio_mix.has_supported_voice_extension(path):
            raise AudioMixInputError(
                f"Voice clip {os.path.basename(path)} is not a supported type "
                f"({', '.join(fork_audio_mix.SUPPORTED_VOICE_EXTENSIONS)})")
        if not os.path.isfile(path):
            raise AudioMixInputError(f"Voice clip is missing or unreadable: {path}")
        # [FORK] Digital-Union (C3-R1B-a): `except AudioProbeError`, never `except AudioMixError`
        # and never `except Exception`. A wide catch here would swallow a cancellation and report
        # the user's Stop as an unusable voice clip. The message is preserved verbatim and the
        # original cause is chained.
        try:
            duration = probe_duration(path)
        except AudioProbeError as exc:
            raise AudioMixInputError(str(exc)) from exc
        prepared.append(fork_audio_mix.VoiceInput(
            index=index, path=path, duration=duration))
    return tuple(prepared)


# ---------------------------------------------------------------------------
# filtergraph
# ---------------------------------------------------------------------------


def build_duck_expression(duck_events, floor: float) -> str:
    """The music gain as one FFmpeg expression: ``1 - (1-floor) * max(duck_1, ..., duck_n)``.

    Each event's duck amount is ``min(rising_ramp, falling_ramp)`` with both ramps clipped to
    ``[0, 1]``, so the body of a clip sits at 1 and everything outside the window at 0. Combining
    with ``max`` means overlapping events take the deepest duck, independently of their order —
    matching :func:`audio_mix.duck_gain_at` exactly, which is what the tests compare against.
    """
    if not duck_events:
        return "1"
    terms = []
    for event in duck_events:
        rise_from = event.voice_start - event.attack
        terms.append(
            f"min(clip((t-{rise_from:.6f})/{event.attack:.6f},0,1),"
            f"clip(({event.voice_end:.6f}+{event.release:.6f}-t)/{event.release:.6f},0,1))")
    deepest = terms[0]
    for term in terms[1:]:
        deepest = f"max({deepest},{term})"
    return f"1-{1.0 - floor:.6f}*({deepest})"


def escape_filter_expression(expression: str) -> str:
    """Escape an expression for use as a filtergraph option value.

    The graph parser splits option values on commas, and every ``min``/``max``/``clip`` call in the
    envelope contains them, so each one is escaped. Quoting alone is not relied on — the escaped
    form is what the real-FFmpeg integration test exercises.
    """
    return expression.replace(",", r"\,")


def build_mix_command(music_path: str, plan, output_path: str,
                      sfx_level_percent: int = fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT) -> list:
    """The complete argv for one mixdown. One list, no shell — the filtergraph never meets a shell.

    Input order is fixed and positional, never derived from a dict: **0 is always the music**, voice
    inputs follow in plan order, then Smart Mix SFX in resolved placement order. So ``adelay`` on
    stream ``k`` always belongs to the ``k``-th entry of ``placements + sfx_placements``.

    [FORK] Digital-Union (Smart Mix V1 / E): ``sfx_level_percent`` is an **execution argument**, not
    plan data. :class:`SfxPlacement` is frozen and deliberately carries no gain, and ``AudioMixPlan``
    gained only ``sfx_placements`` — so the one global linear SFX gain is threaded here instead. The
    default exists for direct/test callers; the production path always passes the resolved value.
    """
    command = [FFMPEG_PATH, "-y", "-i", music_path]
    for placement in plan.placements:
        command += ["-i", placement.path]
    sfx_placements = tuple(getattr(plan, "sfx_placements", ()) or ())
    for placement in sfx_placements:
        command += ["-i", placement.path]

    chain = []
    if plan.duck_events:
        envelope = escape_filter_expression(
            build_duck_expression(plan.duck_events, plan.config.music_floor))
        chain += [
            # Per-sample envelope as its own stream, multiplied into the music. See the module
            # docstring for why this is not `volume=eval=frame`.
            f"aevalsrc='{envelope}':s={MASTER_SAMPLE_RATE}:c=stereo:"
            f"d={plan.music_duration:.6f},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[env]",
            f"[0:a]aresample={MASTER_SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[mus]",
            "[mus][env]amultiply[music]",
        ]
    else:
        # [FORK] Digital-Union (Smart Mix V1 / E): with no voice there is nothing to duck, so no
        # unity envelope is synthesised. `build_duck_expression(())` would return the constant "1"
        # and `amultiply` by it is an exact no-op — generating a full-length 48 kHz stereo stream to
        # multiply by one is pure waste on an SFX-only or music-only mix.
        chain.append(f"[0:a]aresample={MASTER_SAMPLE_RATE},"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo[music]")

    labels = ["[music]"]
    stream = 0
    for stream, placement in enumerate(plan.placements, start=1):
        chain.append(
            f"[{stream}:a]aresample={MASTER_SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={int(round(placement.start * 1000))}:all=1[v{stream}]")
        labels.append(f"[v{stream}]")

    gain = fork_smart_mix.normalize_control(
        sfx_level_percent, fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT) / 100.0
    for offset, placement in enumerate(sfx_placements, start=1):
        index = stream + offset
        # `atrim` ONLY for a trimmed atmosphere bed — the one role whose semantics allow it. A
        # non-atmosphere SFX is never shortened to make it fit; it is skipped at planning time.
        trim = (f"atrim=end={placement.play_duration:.6f},"
                if getattr(placement, "trimmed", False) else "")
        chain.append(
            f"[{index}:a]aresample={MASTER_SAMPLE_RATE},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"{trim}volume={gain:.6f},"
            f"adelay={int(round(placement.start * 1000))}:all=1[s{index}]")
        labels.append(f"[s{index}]")

    chain.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
                 f"dropout_transition=0:normalize=0[mixed]")
    chain.append(
        f"[mixed]alimiter=limit={LIMITER_LIMIT}:attack={LIMITER_ATTACK_MS}:"
        f"release={LIMITER_RELEASE_MS}:level=0:latency=1,"
        f"apad,atrim=end={plan.music_duration:.6f},asetpts=N/SR/TB[outa]")

    command += [
        "-filter_complex", ";".join(chain),
        "-map", "[outa]",
        "-c:a", MASTER_CODEC,
        "-ar", str(MASTER_SAMPLE_RATE),
        "-ac", str(MASTER_CHANNELS),
        output_path,
    ]
    return command


def master_path_for(session_dir: str) -> str:
    """A fresh master path per render, so a stale mix can never be picked up by a later one."""
    return os.path.join(session_dir, f"beatsync_mix_{uuid.uuid4().hex}.wav")


# ---------------------------------------------------------------------------
# execution
# ---------------------------------------------------------------------------


def render_mixed_master(music_path: str, plan, output_path: str,
                        sfx_level_percent: int = fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT,
                        lifecycle=None) -> str:
    """Produce the mixed master and verify it. Raises :class:`AudioMixError` on any failure.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` defaults to ``None`` (unchanged behaviour). A
    matching cancellation raises ``RenderCancelled`` from ``_run`` below; this function's own
    ``except subprocess.TimeoutExpired`` is deliberately narrow and was never broad enough to catch
    it, so cancellation already propagates through this function unchanged.

    [FORK] Digital-Union (C3-R1B-a): every failure is an :class:`AudioMixExecutionError`, including
    the duration probe of the **generated** master -- that artifact is this render's own output, so
    a failure reading it is never a statement about a user input. `gui.py` maps this type to
    ``UNKNOWN_FATAL``: a timeout, a non-zero FFmpeg, a missing/empty file or a drift could all be
    systemic (binary, disk, driver), and this repository has no structured FFmpeg diagnostic
    classification to prove otherwise. Nothing is inferred from stderr prose.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    command = build_mix_command(music_path, plan, output_path,
                                sfx_level_percent=sfx_level_percent)
    try:
        result = _run(command, _MIX_TIMEOUT_SECONDS, lifecycle=lifecycle)
    except subprocess.TimeoutExpired:
        raise AudioMixExecutionError("Audio mixdown timed out")
    if result.returncode != 0:
        raise AudioMixExecutionError(
            f"Audio mixdown failed: {_tail(result.stderr) or 'FFmpeg failed'}")

    if not os.path.isfile(output_path):
        raise AudioMixExecutionError("Audio mixdown produced no output file")
    if os.path.getsize(output_path) <= 0:
        raise AudioMixExecutionError("Audio mixdown produced an empty output file")

    # [FORK] Digital-Union (C3-R1B-a): `except AudioProbeError` only. `probe_duration` is handed the
    # lifecycle here (unchanged from R1A), so a wide catch would convert a user's Stop into an
    # execution failure -- and in a C3 batch would report a cancelled candidate as a broken mix.
    #
    # [FORK] Digital-Union (C3-R1B-a / R2): `str(exc)`, NOT a new prefixed sentence. R1B-a is
    # classification-only, so the *type* changes and the displayed reason must not: the reason the
    # user saw before was the probe's own message, reached through the pre-existing
    # `except AudioMixError` at the GUI boundary. R1 added "Could not verify the mixed master: "
    # in front of it, which was a real user-facing change in a milestone that claimed none. The
    # cause is still chained, so nothing is lost for diagnostics.
    try:
        produced = probe_duration(output_path, lifecycle=lifecycle)
    except AudioProbeError as exc:
        raise AudioMixExecutionError(str(exc)) from exc
    drift = abs(produced - plan.music_duration)
    if drift > DURATION_TOLERANCE_SECONDS:
        # Load-bearing: `create_music_video` derives the frame-locked timeline from this file's
        # duration, so a master that is not the music's length would shift every cut.
        raise AudioMixExecutionError(
            f"Mixed audio is {produced:.3f}s but the music is {plan.music_duration:.3f}s "
            f"({drift * 1000:.1f} ms drift)")
    return output_path


def discard_master(path) -> None:
    """Best-effort removal of a temporary master. Never raises: cleanup must not mask a result."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def build_mixed_master(music_path: str, music_duration: float, beat_times, sections,
                       voices, config, session_dir: str,
                       sfx_placements=(),
                       sfx_level_percent: int = fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT,
                       lifecycle=None):
    """Plan voice, attach any Smart Mix SFX, render one master. Returns ``(master_path, plan)``.

    The master is written into ``session_dir`` — **never** ``get_processing_dir()``, which
    ``create_music_video`` clears at startup and would therefore delete this file moments after it
    was produced.

    [FORK] Digital-Union (Smart Mix V1 / E): ``sfx_placements`` are already resolved by the pure
    planner — this function never plans SFX, it only attaches them to the voice plan so one executor
    produces one master. ``sfx_level_percent`` rides alongside as execution state.

    [FORK] Digital-Union (C3-R1A): ``lifecycle`` defaults to ``None`` (unchanged behaviour).

    [FORK] Digital-Union (C3-R1B-a): a :class:`~beatsync_fork.audio_mix.PlacementFailure` becomes an
    :class:`AudioMixPlanError`, which `gui.py` maps to ``SHARED_FATAL``. That is proven rather than
    assumed, in two parts. Placement feasibility reads only ``avoid_drops``,
    ``start_delay_seconds`` and ``min_gap_seconds`` -- all frozen for a whole C3 batch -- while the
    one candidate-varied ``AudioMixConfig`` field, ``music_under_voice_percent``, reaches only
    ``config.music_floor`` in the duck model, *after* placement has already succeeded. And
    ``PlacementFailure`` is returned only from inside ``plan_voice_placements``'s
    ``for voice in voices:`` loop, so it is unreachable with an empty selection -- meaning it can
    only fire when the batch-frozen voice selection is non-empty. No candidate recipe can change
    the outcome.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    plan = fork_audio_mix.plan_voice_placements(
        music_duration=music_duration,
        beat_times=fork_audio_mix.project_beat_times(beat_times),
        sections=sections,
        voices=voices,
        config=config,
    )
    if isinstance(plan, fork_audio_mix.PlacementFailure):
        raise AudioMixPlanError(plan.reason)

    sfx_placements = tuple(sfx_placements or ())
    if sfx_placements:
        plan = dataclasses.replace(plan, sfx_placements=sfx_placements)

    output_path = master_path_for(session_dir)
    try:
        render_mixed_master(music_path, plan, output_path,
                            sfx_level_percent=sfx_level_percent, lifecycle=lifecycle)
    except AudioMixError:
        discard_master(output_path)
        raise
    except RenderCancelled:
        # [FORK] Digital-Union (C3-R1A): same partial-output cleanup as the AudioMixError path --
        # a cancelled mixdown must not leave a stray WAV in session_dir -- but RenderCancelled must
        # still escape unchanged so the caller sees a typed cancellation, not an AudioMixError.
        discard_master(output_path)
        raise
    return output_path, plan


# ---------------------------------------------------------------------------
# Smart Mix library preflight (E)
# ---------------------------------------------------------------------------


def _role_relative_component(root: str, path: str):
    """The FIRST path component under the root, or ``None`` for a file sitting in the root."""
    relative = os.path.relpath(path, root)
    parts = relative.replace("\\", "/").split("/")
    if len(parts) < 2:
        return None
    return parts[0]


def prepare_sfx_inputs(root, enabled_roles):
    """Enumerate, classify, order and probe the Smart Mix library.

    Returns ``(assets, diagnostics)`` where ``diagnostics`` is the pure
    :class:`smart_mix.SfxLibraryDiagnostics`. [FORK] Digital-Union (Smart Mix V1 / E, R1): it used
    to be a loose dict that only ``library_root`` was ever read from, so everything the scan
    *ignored* was collected and then silently dropped. Handing back the immutable value instead
    means the planner can carry it and the report can render it, with no second formatter.

    Runs **before Stage 1** whenever Smart Mix is active, for the same reason D's voice preflight
    does: resolving paths and probing durations is cheap, a full Stage 1-5 analysis is not, and a
    broken library is a user-fixable input problem.

    **Every enabled, recognised, supported asset is probed** — not merely the ones a later plan
    happens to select. A corrupt file inside an enabled role is therefore fatal before any analysis,
    which matches D's "the user's explicit inputs are honoured or the render fails" philosophy; a
    musical rule finding no anchor is a different thing entirely and stays non-fatal.

    Classification is the pure module's exact alias table on the **first** component under the root.
    Unknown folders, root-level files, disabled roles and unsupported extensions are counted for the
    report and **never probed**.

    [FORK] Digital-Union (C3-R1B-a): every failure is an :class:`AudioMixInputError` -- the same
    local cause type the voice preflight uses, because both are statements about user-supplied
    files. The *batch* classification is nevertheless different: `gui.py` maps this preflight to
    ``CANDIDATE_LOCAL``, because it runs only when ``smart_mix_active``, which requires
    ``SmartMixConfig.plans_anything``, which requires ``sfx_amount > 0`` -- and ``sfx_amount`` is
    candidate-specific ``AudioRecipe`` state. A candidate resolving 0 never calls this function at
    all, so a broken library does not prove every remaining candidate must fail. That is precisely
    the asymmetry that stops the local cause type from carrying the classification itself.
    """
    if not isinstance(root, str) or not root.strip():
        raise AudioMixInputError("Smart Mix: no SFX library folder was given")
    root = os.path.abspath(root)
    if not os.path.exists(root):
        raise AudioMixInputError(f"Smart Mix: SFX library folder does not exist: {root}")
    if not os.path.isdir(root):
        raise AudioMixInputError(f"Smart Mix: SFX library path is not a folder: {root}")

    wanted = fork_smart_mix.normalize_roles(enabled_roles)
    unknown_folders = []
    root_level_files = 0
    unsupported_files = 0
    skipped_disabled_files = 0

    by_role = {}
    try:
        walker = os.walk(root, onerror=_walk_error)
        for directory, _subdirs, filenames in walker:
            for name in sorted(filenames):
                full = os.path.join(directory, name)
                component = _role_relative_component(root, full)
                if component is None:
                    root_level_files += 1
                    continue
                role = fork_smart_mix.role_for_folder(component)
                if role is None:
                    if component not in unknown_folders:
                        unknown_folders.append(component)
                    continue
                if role not in wanted:
                    skipped_disabled_files += 1
                    continue
                if not fork_smart_mix.has_supported_sfx_extension(full):
                    unsupported_files += 1
                    continue
                by_role.setdefault(role, []).append(full)
    except OSError as exc:
        raise AudioMixInputError(f"Smart Mix: could not read the SFX library: {exc}")

    assets = []
    per_role = []
    for role in fork_smart_mix.ROLE_ORDER:
        ordered = fork_smart_mix.order_sfx_paths(by_role.get(role, ()))
        per_role.append((role, len(ordered)))
        for path in ordered:
            if not os.path.isfile(path):
                raise AudioMixInputError(f"Smart Mix: SFX asset is missing or unreadable: {path}")
            # [FORK] Digital-Union (C3-R1B-a): narrow, same reasoning as the voice preflight's wrap.
            try:
                duration = probe_duration(path)
            except AudioProbeError as exc:
                raise AudioMixInputError(str(exc)) from exc
            assets.append(fork_smart_mix.SfxAsset(
                role=role, path=path, duration=duration))

    diagnostics = fork_smart_mix.SfxLibraryDiagnostics(
        root=root,
        # Ordered here once, deterministically, so the report never depends on `os.walk`.
        unknown_folders=fork_smart_mix.sort_unknown_folders(unknown_folders),
        root_level_files=root_level_files,
        unsupported_files=unsupported_files,
        skipped_disabled_files=skipped_disabled_files,
        per_role=tuple(per_role),
    )

    if not assets:
        raise AudioMixInputError(
            "Smart Mix: the SFX library has no usable audio in any enabled role "
            f"({', '.join(sorted(wanted)) or 'none enabled'})")
    return tuple(assets), diagnostics


def _walk_error(error):
    """Never let `os.walk` silently skip an unreadable directory — a quietly smaller library is
    exactly the failure the fork's input manager exists to prevent."""
    raise error


__all__ = [
    "DURATION_TOLERANCE_SECONDS",
    "LIMITER_ATTACK_MS",
    "LIMITER_LIMIT",
    "LIMITER_RELEASE_MS",
    "MASTER_CHANNELS",
    "MASTER_CODEC",
    "MASTER_SAMPLE_RATE",
    "AudioMixError",
    # [FORK] Digital-Union (C3-R1B-a): the four local cause types, under the retained base above.
    "AudioMixExecutionError",
    "AudioMixInputError",
    "AudioMixPlanError",
    "AudioProbeError",
    "build_duck_expression",
    "build_mix_command",
    "build_mixed_master",
    "discard_master",
    "master_path_for",
    "prepare_sfx_inputs",
    "prepare_voice_inputs",
    "probe_duration",
    "render_mixed_master",
]
