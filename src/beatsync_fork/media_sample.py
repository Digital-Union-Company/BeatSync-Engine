"""[FORK] Digital-Union (MEDIA-VERIFY-S2-B P1): bounded sample-decode diagnostic core.

Stdlib only, per CLAUDE.md's hard rule for ``src/beatsync_fork/`` -- no ``logger``, ``paths``,
``gradio``, ``cupy``, ``cv2``, ``librosa`` or ``numpy``. The two child processes live in
``src/ffmpeg_processing.py``; everything here is the pure part: statuses, stream selection, the
progress parser, the classifier and the operator report. Both subprocesses arrive as **injected
callables**, exactly as :func:`beatsync_fork.media_verify.verify_media_health` takes its ``probe``
-- which is what lets the whole of P1 be tested without FFmpeg existing.

What this module is for
-----------------------
S2-A answers "did the container header parse?". It never decodes a frame, so a file whose header is
intact and whose middle is shredded passes it. S2-B is the next bounded question: *take one source,
one seek position, and ask the decoder to actually produce frames there*.

The honesty boundary that matters most
--------------------------------------
:data:`SAMPLE_ONLY_TEXT` is the sentence every report carries, and it is phrased **hypothetically**
("even a successful sample...") for one reason: it is unconditional, so it must be true of a report
whose decode never started. A past-tense "one window was decoded" would be a false statement on
:attr:`SampleStatus.SOURCE_MISSING`, :attr:`SampleStatus.INVALID_REQUEST`,
:attr:`SampleStatus.NOT_RUN` and every other non-executed outcome. What actually happened is carried
separately and factually by :class:`DecodeExecution`, which distinguishes *not requested*, *start
not confirmed*, *ran without a clean result* and *requested frames produced* -- and which is also
the only basis for :attr:`SampleVerdict.decoder_was_launched`, so that property can never claim a
child started when the seam refused to spawn one. It describes **process execution only**: how many
frames a child produced is separate evidence, and no execution label may assert underproduction
that the reported frame count contradicts.

Even where a sample does succeed it is **one window of one stream** -- not proof of whole-file
integrity, not proof the production NVENC extraction would succeed, not proof of renderability. The
single positive status is named :attr:`SampleStatus.OUTPUT_FRAMES_OK` rather than anything
resembling a health claim, and the word "output" in it is load-bearing: FFmpeg's ``frame=`` counter
counts frames **written to the output pipeline**, so with ``-ss`` before ``-i`` the frames decoded
during accurate-seek pre-roll are exercised but never counted. The counter therefore understates
decoder work and must never be described as "frames decoded" or "frames verified".

Progress evidence is read from one completed block, never assembled across blocks
---------------------------------------------------------------------------------
:func:`parse_progress_output` accumulates keys into the **current** block and only completes that
block when its ``progress`` marker arrives. Evidence comes from the last completed block alone. An
earlier design kept one last-seen-value dictionary across the whole stream, which let
``frame=30 / progress=continue / progress=end`` report ``frames=30`` for a terminal block that
carried no frame count at all -- and that is the one combination that must stay
:attr:`SampleStatus.EVIDENCE_UNTRUSTWORTHY`, because the sole positive status may never rest on a
number no terminal block ever emitted. Counters inherit nothing either: a ``dup_frames=0`` from an
earlier block is not a measurement of the final one.

Two things this module deliberately does NOT do
-----------------------------------------------
**It never divides by a frame rate.** An earlier design classified a short sample as end-of-file
when ``seek + frames/fps`` exceeded the reported duration. That predicate is unsound three times
over: :func:`~beatsync_fork.media_verify.classify_probe` returns ``METADATA_OK`` without ever
validating ``fps``, so the divisor can legitimately be ``0``; ``r_frame_rate`` is a base/maximum
rate that for VFR media can sit far above the true average, which biases the comparison toward
*claiming damage* on healthy files; and for variable frame rate the quantity ``frames/fps`` is not
the time span of those frames at all. So underproduction is reported as
:attr:`SampleStatus.UNDERFILLED_UNVERIFIED` -- inconclusive, never "damaged", never "confirmed
EOF" -- and ``fps`` is carried as report context only.

**It never treats S2-A's verdict as authority over the stream it decodes.**
:func:`~beatsync_fork.media_verify.classify_probe` picks the *first* ``codec_type == "video"``
stream and validates that one's geometry and duration. FFmpeg's documented automatic stream
selection picks the **highest-resolution** video stream (ties broken by lowest index), which is
what the renderer gets, since
``ffmpeg_processing.extract_clip_segment_ffmpeg_detailed`` passes no ``-map``. Those are different
streams on a multi-video container, so treating ``classify_probe`` as a prerequisite would both
refuse files whose unselected first stream has a bad duration and bless a selected stream nobody
validated. :func:`select_renderer_equivalent_stream` therefore re-reads the raw ``-show_streams``
payload, identifies the renderer-equivalent stream, and validates **that** stream's own metadata.
``probe_media_metadata`` is reused for the FFprobe execution, its timeout and its launch-error
semantics; its classifier is not.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from stat import S_ISREG
from typing import Any, Callable, Dict, List, Tuple

from beatsync_fork.input_manager import SUPPORTED_VIDEO_EXTENSIONS
from beatsync_fork.media_verify import (
    CHANGE_DETECTION_LIMIT_TEXT,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    SourceFingerprint,
    WorkBudget,
    fingerprint_source,
)

__all__ = [
    "CHANGE_DETECTION_LIMIT_TEXT",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_SAMPLE_BUDGET_SECONDS",
    "DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS",
    "DEFAULT_SAMPLE_FRAMES",
    "MAX_DIAGNOSTIC_CHARS",
    "MAX_SAMPLE_FRAMES",
    "SAMPLE_ONLY_TEXT",
    "SOURCE_COUNT_CAP",
    "SUPPORTED_VIDEO_EXTENSIONS",
    "WORKER_CAP",
    "DecodeExecution",
    "DurationSource",
    "FrameEvidence",
    "SampleChangeWindow",
    "SampleDecodeOutcome",
    "SampleStatus",
    "SampleVerdict",
    "SelectedStream",
    "Stability",
    "StreamEquivalence",
    "StreamSelection",
    "classify_decode",
    "coerce_stream_text",
    "format_sample_report",
    "parse_progress_output",
    "run_sample_check",
    "select_renderer_equivalent_stream",
]


# ---------------------------------------------------------------------------
# bounds -- every one a conservative policy choice, NOT a measured optimum
# ---------------------------------------------------------------------------

#: Output frames requested from the one sampled window. ~1 s at 30 fps: enough to prove the decoder
#: produces a run of frames at the seek point, small enough that a stalled read is bounded. It is
#: NOT coverage -- on a long-GOP source 30 frames may not even cross a GOP boundary.
DEFAULT_SAMPLE_FRAMES = 30

#: The HARD ceiling, not merely the default. :func:`run_sample_check` refuses a larger request with
#: :attr:`SampleStatus.INVALID_REQUEST` and starts no child, because the whole bounded-risk argument
#: for P1 is a property of this module rather than a promise about its callers: a mistaken or
#: hostile ``requested_frames`` must not be able to widen the work a diagnostic performs. The pure
#: orchestrator is the single owner of this bound -- ``build_sample_decode_command`` stays a
#: serializer and deliberately does not re-decide policy, and no future GUI validator is relied on.
MAX_SAMPLE_FRAMES = DEFAULT_SAMPLE_FRAMES

#: Wall clock for the one decode child. Sized to stop a *stall* (a dying drive, a disconnected
#: share), not to race a healthy decode. Unmeasured: no real decode has been timed under this
#: design, so a file that exceeds it is reported :attr:`SampleStatus.TIMEOUT`, never corrupt.
DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS = 30

#: Scheduling budget for one operator-initiated check, started when the operation begins. It gates
#: whether the decode child is *scheduled*; it is **not** a guaranteed end-to-end wall clock.
#: ``os.stat``, Windows process creation, process termination and post-timeout pipe draining all sit
#: outside it, so no hard total bound is claimed anywhere in this module.
DEFAULT_SAMPLE_BUDGET_SECONDS = 60

#: One source, one child, per check. The whole bounded-risk argument for P1 rests on this: there is
#: no pool, no queue and no concurrency to reason about.
SOURCE_COUNT_CAP = 1
WORKER_CAP = 1

#: How much FFmpeg diagnostic text may travel into a report. The full stderr stays in the outcome
#: record; a status surface is not a log sink.
MAX_DIAGNOSTIC_CHARS = 400

#: The sentence that keeps one sampled window from being read as a verdict on the file. Carried
#: verbatim in exactly one place in every report -- and phrased **hypothetically**, because it is
#: unconditional: it has to be true of a report whose decode never started. It therefore states what
#: a successful sample *would* bound, and never that a decode occurred. What did occur is
#: :class:`DecodeExecution`, reported on its own line.
SAMPLE_ONLY_TEXT = (
    "Even a successful sample checks only one short window of one stream, decoded to a discarded "
    "output: it is not proof of whole-file integrity, not proof the render's NVENC extraction "
    "would succeed, and not proof of full renderability."
)


class SampleStatus(str, Enum):
    """Read-only sample-decode reporting labels.

    These are **report** labels. None is a cache-generation token, none reaches a cache key, a
    Stage-5 payload, the completion rule or any persisted record.

    :attr:`OUTPUT_FRAMES_OK` is the only positive outcome. Every other member needs operator
    attention or explicitly holds no opinion, and none of them asserts corruption: a timeout, a
    missing frame counter and an unusable duration are all *unverified*, not *damaged*.
    """

    INVALID_REQUEST = "SAMPLE_INVALID_REQUEST"
    SOURCE_MISSING = "SAMPLE_SOURCE_MISSING"
    METADATA_UNUSABLE = "SAMPLE_METADATA_UNUSABLE"
    STREAM_AMBIGUOUS = "SAMPLE_STREAM_AMBIGUOUS"
    SEEK_OUT_OF_RANGE = "SAMPLE_SEEK_OUT_OF_RANGE"
    BUDGET_EXHAUSTED = "SAMPLE_BUDGET_EXHAUSTED"
    NOT_RUN = "SAMPLE_NOT_RUN"
    TIMEOUT = "SAMPLE_TIMEOUT"
    FFMPEG_FAILED = "SAMPLE_FFMPEG_FAILED"
    OUTPUT_FRAMES_OK = "SAMPLE_OUTPUT_FRAMES_OK"
    ZERO_FRAMES = "SAMPLE_ZERO_FRAMES"
    UNDERFILLED_UNVERIFIED = "SAMPLE_UNDERFILLED_UNVERIFIED"
    EVIDENCE_UNTRUSTWORTHY = "SAMPLE_EVIDENCE_UNTRUSTWORTHY"
    OUTPUT_WITH_DECODER_ERRORS = "SAMPLE_OUTPUT_WITH_DECODER_ERRORS"
    SOURCE_CHANGED = "SAMPLE_SOURCE_CHANGED"


class DecodeExecution(str, Enum):
    """How far the decode actually got. The factual counterpart to the hypothetical coverage text.

    This exists because a report must be able to say *what happened* without a status having to
    carry that load, and because :attr:`SampleVerdict.decoder_was_launched` must never be inferred
    from a status label. The distinction that matters most is the middle one: the seam reporting
    ``error`` (executable missing, OS refusal) or raising means **no FFmpeg child is known to have
    started**, which is a different fact from one that started and failed. Where child-start
    evidence cannot be established it is not invented.

    This enum describes **process execution only**. How much output a child produced is separate
    evidence, carried by :attr:`SampleVerdict.observed_frames`, and the two are never conflated:
    :attr:`RAN_WITHOUT_CLEAN_RESULT` means "ran, and did not earn the sole positive status", which
    is emphatically *not* "ran, and produced fewer frames than requested". An earlier phrasing
    asserted the latter, and was therefore false on five reachable paths where the terminal block
    reported the full requested count but an earlier precedence tier -- decoder errors, a source
    change, an unobservable post-decode source, a timeout, a non-zero exit -- withheld success.
    """

    #: No decode was requested: the check refused or failed before scheduling a child. Covers an
    #: invalid request, a missing source, unusable metadata, an ambiguous stream, a seek out of
    #: range, a cross-phase source change and an exhausted scheduling budget.
    NOT_REQUESTED = "not_requested"
    #: The decode callable was invoked, but no FFmpeg child is positively known to have started --
    #: a refused spawn, an unexpected exception from the seam itself, or a returned record whose
    #: exit code could not be read. Because this member denies confirmed child-start evidence, a
    #: report carrying it must not also print a frame count: :attr:`SampleVerdict.observed_frames`
    #: is always ``None`` here, and ``run_sample_check`` guarantees that by discarding the untrusted
    #: ``stdout`` of a record it could not read an exit code from.
    START_NOT_CONFIRMED = "start_not_confirmed"
    #: A child demonstrably ran (it returned an exit code, or it was killed by its own timeout) and
    #: the sample did not reach :attr:`SampleStatus.OUTPUT_FRAMES_OK`. It says **nothing about how
    #: many frames were produced**: ``classify_decode``'s early tiers -- a timeout, a non-zero exit,
    #: an unobservable post-decode source, a source change -- all outrank the frame tiers, so a run
    #: that reported the full requested count lands here too. Asserting underproduction from this
    #: member would contradict the ``observed_frames`` evidence printed beside it.
    RAN_WITHOUT_CLEAN_RESULT = "ran_without_clean_result"
    #: A child ran and the requested output frames were produced.
    REQUESTED_FRAMES_PRODUCED = "requested_frames_produced"


#: Prose for each execution stage. Factual, past tense only where a child provably ran.
_EXECUTION_TEXT = {
    DecodeExecution.NOT_REQUESTED:
        "no decode was requested (the check stopped before any child was scheduled)",
    DecodeExecution.START_NOT_CONFIRMED:
        "a decode was requested, but no FFmpeg child was confirmed to have started",
    DecodeExecution.RAN_WITHOUT_CLEAN_RESULT:
        "an FFmpeg child ran, but no clean sample verdict was established; see the result and "
        "observed output frames",
    DecodeExecution.REQUESTED_FRAMES_PRODUCED:
        "an FFmpeg child ran and produced the requested output frames",
}


class SampleChangeWindow(str, Enum):
    """*Which* window a detected source change was observed in.

    Deliberately a separate enum from :class:`~beatsync_fork.media_verify.ChangeWindow` rather than
    a reuse of it: S2-A brackets its FFprobe call, so its member is named ``DURING_PROBE``, and
    reusing that name to mean "during the decode" would make the field lie about which child was
    running. The two windows S2-B brackets are the cross-phase gap and the decode itself.
    """

    NONE = ""
    #: Changed between the first observation and the one taken immediately before decode. The
    #: FFprobe metadata -- which supplied the duration AND the ``-map`` target -- describes bytes
    #: that are already gone, so the decoder is never launched.
    CROSS_PHASE = "cross_phase"
    #: Changed while FFmpeg was reading it.
    DURING_DECODE = "during_decode"
    #: Both windows. Reported as both, never collapsed to the narrower one.
    BOTH = "both"


_CHANGED_WINDOWS = (
    SampleChangeWindow.CROSS_PHASE,
    SampleChangeWindow.DURING_DECODE,
    SampleChangeWindow.BOTH,
)


class Stability(str, Enum):
    """Whether the source-stability brackets could be completed at all."""

    #: Every required observation succeeded and agreed.
    STABLE = "stable"
    #: An observation succeeded and disagreed with an earlier one.
    CHANGED = "changed"
    #: An observation could not be taken (the file became unstattable). Claims NEITHER stability
    #: nor change -- and never permits a success verdict.
    UNVERIFIED = "unverified"


class StreamEquivalence(str, Enum):
    """How confidently the tested stream matches the one the renderer would select."""

    #: Not determined (no selection was reached).
    UNKNOWN = ""
    #: Exactly one video stream existed, so the renderer has no other choice.
    SINGLE_VIDEO_STREAM = "equivalent_single_video_stream"
    #: Several candidates, all rankable: FFmpeg's documented highest-resolution rule (ties by
    #: lowest index) determines the renderer's pick, and we pinned the same one.
    BY_HIGHEST_RESOLUTION = "equivalent_by_highest_resolution"
    #: Equivalence could not be established. Never paired with a tested stream.
    NOT_ESTABLISHED = "not_established"


class DurationSource(str, Enum):
    """Where the selected stream's duration came from. Recorded because it is not interchangeable.

    A stream duration describes the stream we decode. A container duration may describe a longer
    audio tail, or be an estimate, and on a multi-video container it does not belong to the selected
    stream at all. S2-A folds the fallback in silently; here the provenance travels with the number.
    """

    NONE = ""
    STREAM = "stream"
    CONTAINER = "container"


def coerce_stream_text(raw: Any) -> str:
    """Total coercion of one captured pipe into text.

    ``subprocess.TimeoutExpired.stdout``/``.stderr`` are documented as **always bytes when any
    output was captured, regardless of the ``text=True`` setting**, and may be ``None`` instead of
    ``b""`` when nothing was observed. A decode timeout is exactly the case whose partial progress
    we most want to read, so every parser entry point goes through here: ``str``, ``bytes``,
    ``None``, empty and invalid/partial UTF-8 all become a string, and nothing raises.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        return raw
    return str(raw)


def _bounded(text: Any, limit: int = MAX_DIAGNOSTIC_CHARS) -> str:
    """One whitespace-collapsed line, bounded, with truncation marked."""
    flat = " ".join(coerce_stream_text(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _finite(value: Any) -> float:
    """A finite float, or ``0.0``. Never raises, never returns NaN or an infinity."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):
        return 0.0
    return out


def _is_finite(value: Any) -> bool:
    """Is this a real, finite number? Distinguishes "0.0" from "unparseable"."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return False
    return out == out and out not in (float("inf"), float("-inf"))


def _frame_rate(raw: Any) -> float:
    """``r_frame_rate`` as a float, or ``0.0``. Report context only -- never a divisor.

    Intentionally a local implementation rather than an import of ``media_verify``'s private
    equivalent: this one is applied to the *selected* stream, and a cross-module private import
    would couple two contracts that are deliberately independent.
    """
    text = coerce_stream_text(raw).strip()
    if not text:
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        denominator = _finite(den)
        if denominator == 0.0:
            return 0.0
        return _finite(num) / denominator
    return _finite(text)


def _non_negative_int(raw: Any) -> int | None:
    """A non-negative ``int``, or ``None`` for missing/malformed. Never fabricates a value.

    Rejects floats and bools on purpose: ``frame=29.5`` and ``frame=True`` are malformed progress
    output, and silently flooring or truthing them would invent evidence.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    text = coerce_stream_text(raw).strip()
    if not text or not text.isdigit():
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


# ---------------------------------------------------------------------------
# stream selection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectedStream:
    """The one video stream S2-B will pin with ``-map`` and report on."""

    index: int
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    duration_source: DurationSource = DurationSource.NONE
    equivalence: StreamEquivalence = StreamEquivalence.UNKNOWN
    attached_pic: bool = False
    """True when the selected stream is itself flagged as cover art / a thumbnail. Only reachable
    when it is the *only* video stream -- disclosed because a one-frame cover art will legitimately
    underfill a 30-frame request."""

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class StreamSelection:
    """Outcome of renderer-equivalent selection: a stream, an ambiguity, or bad metadata."""

    stream: SelectedStream | None = None
    ambiguous_reason: str = ""
    invalid_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.stream is not None


def _video_streams(payload: Any) -> List[Dict[str, Any]]:
    streams = payload.get("streams") if isinstance(payload, dict) else None
    streams = streams if isinstance(streams, list) else []
    return [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]


def _attached_pic(stream: Dict[str, Any]) -> bool:
    disposition = stream.get("disposition")
    if not isinstance(disposition, dict):
        return False
    return _non_negative_int(disposition.get("attached_pic")) not in (None, 0)


def select_renderer_equivalent_stream(payload: Any) -> StreamSelection:
    """Pick the video stream the renderer would decode, or fail closed. Pure: no I/O, no clock.

    The renderer passes no ``-map``, so FFmpeg's documented automatic stream selection applies:
    *"for video, it is the stream with the highest resolution"*, and on a tie *"the stream with the
    lowest index is chosen"*. A tie is therefore **resolvable, not ambiguous** -- the ordering rule
    is part of the documented contract.

    Fails closed (``ambiguous_reason``) only where the renderer's pick genuinely cannot be
    determined from metadata:

    * any video stream lacks a usable absolute index -- it can neither be ranked nor ``-map``ped;
    * two video streams declare the **same** absolute index -- ``-map 0:<index>`` then names two
      streams, so whichever we ranked, the pinned target is not the one we described. A resolution
      tie between streams with *distinct* indices is unaffected: that is resolvable by the
      documented lowest-index rule, and stays resolvable;
    * two or more video streams and any is flagged ``attached_pic`` -- the documentation states the
      resolution rule and the ``V`` specifier's exclusion of cover art, but does not state how
      automatic selection ranks an attached picture, so equivalence cannot be *established*;
    * two or more video streams and any lacks usable geometry -- an unrankable candidate might
      outrank the one we would otherwise choose.

    Reports bad metadata (``invalid_reason``) where the pick *is* determined but the chosen stream
    cannot be described: no video stream at all, unusable geometry on a lone stream, or no usable
    duration. That distinction matters -- "I cannot tell which stream" and "the stream I must test
    is undescribable" are different operator problems.
    """
    video = _video_streams(payload)
    if not video:
        total = len(payload.get("streams", [])) if isinstance(payload, dict) else 0
        return StreamSelection(
            invalid_reason=f"Container parsed, but it declares no video stream "
                           f"({total} stream(s) total).")

    indexed: List[Tuple[int, Dict[str, Any]]] = []
    for stream in video:
        index = _non_negative_int(stream.get("index"))
        if index is None:
            return StreamSelection(
                ambiguous_reason="A video stream has no usable absolute index, so neither the "
                                 "renderer's choice nor an explicit -map target can be determined.")
        indexed.append((index, stream))

    if len({index for index, _ in indexed}) != len(indexed):
        return StreamSelection(
            ambiguous_reason=f"{len(indexed)} video streams declare only "
                             f"{len({i for i, _ in indexed})} distinct absolute index(es), so a "
                             f"-map target cannot name exactly one of them and renderer "
                             f"equivalence cannot be established.")

    if len(indexed) > 1 and any(_attached_pic(s) for _, s in indexed):
        return StreamSelection(
            ambiguous_reason=f"{len(indexed)} video streams and at least one is an attached "
                             f"picture; FFmpeg's documented selection rule does not state how an "
                             f"attached picture is ranked, so renderer equivalence cannot be "
                             f"established.")

    def rankable(stream: Dict[str, Any]) -> bool:
        return (_non_negative_int(stream.get("width")) or 0) > 0 and \
               (_non_negative_int(stream.get("height")) or 0) > 0

    if len(indexed) > 1 and not all(rankable(s) for _, s in indexed):
        return StreamSelection(
            ambiguous_reason=f"{len(indexed)} video streams and at least one has no usable frame "
                             f"size; an unrankable candidate could outrank the chosen stream, so "
                             f"renderer equivalence cannot be established.")

    if len(indexed) == 1 and not rankable(indexed[0][1]):
        return StreamSelection(
            invalid_reason="The only video stream has a missing or non-positive frame size.")

    # Highest resolution wins; the lowest index breaks a tie, exactly as documented.
    index, chosen = min(
        indexed,
        key=lambda pair: (-((_non_negative_int(pair[1].get("width")) or 0)
                            * (_non_negative_int(pair[1].get("height")) or 0)), pair[0]),
    )

    container = payload.get("format") if isinstance(payload, dict) else None
    container = container if isinstance(container, dict) else {}
    duration = _finite(chosen.get("duration"))
    duration_source = DurationSource.STREAM if duration > 0 else DurationSource.NONE
    if duration <= 0:
        duration = _finite(container.get("duration"))
        duration_source = DurationSource.CONTAINER if duration > 0 else DurationSource.NONE
    if duration <= 0:
        return StreamSelection(
            invalid_reason=f"Video stream {index} has no usable duration on the stream or the "
                           f"container.")

    equivalence = (StreamEquivalence.SINGLE_VIDEO_STREAM if len(indexed) == 1
                   else StreamEquivalence.BY_HIGHEST_RESOLUTION)
    return StreamSelection(stream=SelectedStream(
        index=index,
        codec=str(chosen.get("codec_name") or ""),
        width=_non_negative_int(chosen.get("width")) or 0,
        height=_non_negative_int(chosen.get("height")) or 0,
        fps=_frame_rate(chosen.get("r_frame_rate")),
        duration=duration,
        duration_source=duration_source,
        equivalence=equivalence,
        attached_pic=_attached_pic(chosen),
    ))


# ---------------------------------------------------------------------------
# progress evidence
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameEvidence:
    """What FFmpeg's ``-progress`` stream actually said. ``None`` always means *unavailable*.

    Every field describes the **last completed block only**. Nothing is carried across a block
    boundary, so ``None`` really does mean "the terminal block did not report this".
    """

    saw_end: bool = False
    """Did the last completed block's ``progress`` key carry the terminal value ``end``?"""
    frames: int | None = None
    """Output frames reported **by that block**, or ``None`` when it carried no usable ``frame``."""
    dup_frames: int | None = None
    """``None`` is UNKNOWN -- never silently converted to zero, and never read as proof of zero."""
    drop_frames: int | None = None
    malformed_frame: bool = False
    """That block carried a ``frame`` key that did not parse as a non-negative integer."""
    blocks: int = 0
    """How many blocks were COMPLETED by a ``progress`` marker. A trailing partial write is not
    one, so it can neither become the evidence nor inflate this count."""


def parse_progress_output(raw: Any) -> FrameEvidence:
    """Parse ``-progress`` key/value output. Pure and total -- never raises, never fabricates.

    The documented wire format is ``key=value`` lines where *"the last key of a sequence of progress
    information is always 'progress' with the value 'continue' or 'end'"*. That marker is therefore
    a **block terminator**, and this parser treats it as one: keys accumulate into the current
    block, the ``progress`` key completes it, and all evidence is then read from the last
    **completed** block alone.

    Scoping blocks independently is the whole point, not an implementation detail. A single
    last-seen-value dictionary spanning the stream reports ``frames=30`` for
    ``frame=30 / progress=continue / progress=end``, whose terminal block carried no frame count --
    and that would let the sole positive status rest on a number no terminal block emitted. Nothing
    is inherited across a boundary: a missing terminal ``frame`` stays ``None``, a malformed
    terminal ``frame`` is never replaced by an earlier valid one, and a ``dup_frames=0`` from an
    earlier block is not a measurement of the final one. An earlier ``progress=end`` cannot
    authorize success either -- a later completed block supersedes it.

    A trailing partial write completes no block, so it contributes nothing; the last completed
    block remains the evidence. Deliberately tolerant of anything that is not a ``key=value`` line:
    a banner, a muxer byte, a half-written line. That tolerance is also what makes the null output
    target harmless -- stray bytes cannot corrupt the frame evidence, because they are not
    ``key=value`` and are skipped.

    Accepts ``str``, ``bytes``, ``None`` and invalid UTF-8 (see :func:`coerce_stream_text`), and
    handles CRLF and LF alike via :meth:`str.splitlines`.
    """
    text = coerce_stream_text(raw)
    block: Dict[str, str] = {}
    final: Dict[str, str] = {}
    blocks = 0
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        block[key] = value.strip()
        if key == "progress":
            blocks += 1
            final = block
            block = {}

    saw_frame_key = "frame" in final
    frames = _non_negative_int(final.get("frame")) if saw_frame_key else None
    return FrameEvidence(
        saw_end=final.get("progress") == "end",
        frames=frames,
        dup_frames=_non_negative_int(final.get("dup_frames")),
        drop_frames=_non_negative_int(final.get("drop_frames")),
        malformed_frame=saw_frame_key and frames is None,
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# execution records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SampleDecodeOutcome:
    """What the execution seam in ``ffmpeg_processing`` hands back for one decode.

    Shaped after :class:`~beatsync_fork.media_verify.ProbeOutcome` on purpose, including keeping
    ``timed_out`` a distinct field rather than a sentinel return code -- a timeout is a different
    fact from a non-zero exit, and collapsing them is how a slow drive becomes a damage report.
    Unlike ``ProbeOutcome``, ``stdout``/``stderr`` are typed ``Any``: on the timeout path they
    arrive as ``bytes`` (or ``None``) however the child was launched.
    """

    returncode: int = 0
    stdout: Any = ""
    stderr: Any = ""
    timed_out: bool = False
    error: str = ""
    """Set when the seam itself failed (executable missing, OS refusal) -- not a verdict on the
    file."""


@dataclass(frozen=True)
class SampleVerdict:
    """One source's sample-decode result. ``status`` is the only authority; the rest is detail.

    ``change_window`` and ``stability`` are **separate fields rather than statuses** so that a
    timeout or a launch failure can never disappear behind a generic "changed" label: a decode that
    both timed out and saw the file move reports :attr:`SampleStatus.TIMEOUT` *and*
    :attr:`SampleChangeWindow.DURING_DECODE`, keeping both facts.
    """

    path: str
    status: SampleStatus
    detail: str = ""
    requested_seek: float = 0.0
    requested_frames: int = 0
    observed_frames: int | None = None
    dup_frames: int | None = None
    drop_frames: int | None = None
    returncode: int | None = None
    elapsed_seconds: float = 0.0
    """The WHOLE check, on every path: validation, FFprobe, the stat observations and any decode.
    Set identically whether the check returned early or ran a child, so the number means one thing.
    """
    decode_seconds: float | None = None
    """Just the decode call, or ``None`` when no decode was requested. Kept separate from
    :attr:`elapsed_seconds` because one field that silently switches between "the whole check" and
    "the decode only" cannot be read correctly by anyone."""
    execution: DecodeExecution = DecodeExecution.NOT_REQUESTED
    """What actually happened to the child. The only basis for :attr:`decoder_was_launched`."""
    stream_index: int | None = None
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    duration_source: DurationSource = DurationSource.NONE
    equivalence: StreamEquivalence = StreamEquivalence.UNKNOWN
    attached_pic: bool = False
    """The selected stream is flagged as cover art / a thumbnail. Reachable only when it is the one
    video stream in the container (two or more with an attached picture fails closed as ambiguous),
    and surfaced in the report because a one-frame cover art will legitimately underfill the
    request -- an underfill that says nothing about damage."""
    change_window: SampleChangeWindow = SampleChangeWindow.NONE
    stability: Stability = Stability.UNVERIFIED
    diagnostic: str = ""
    """Bounded FFmpeg text. The full capture stays in the outcome record, never in a report."""

    @property
    def is_problem(self) -> bool:
        return self.status is not SampleStatus.OUTPUT_FRAMES_OK

    @property
    def changed(self) -> bool:
        return self.change_window in _CHANGED_WINDOWS

    @property
    def decoder_was_launched(self) -> bool:
        """Is an FFmpeg child **positively known** to have started?

        Read from :attr:`execution`, never inferred from a status. The two false readings this
        replaces both came from treating :attr:`SampleStatus.NOT_RUN` as a launch: that status means
        the seam reported ``error`` -- a missing executable or an OS refusal -- so no child existed,
        and on the pre-decode path it means *FFprobe* could not start, before a decode was even
        requested. A refused spawn is :attr:`DecodeExecution.START_NOT_CONFIRMED`, which is not a
        launch; a timeout is, because the child had to start in order to be killed by one.
        """
        return self.execution in (DecodeExecution.RAN_WITHOUT_CLEAN_RESULT,
                                  DecodeExecution.REQUESTED_FRAMES_PRODUCED)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def classify_decode(outcome: SampleDecodeOutcome, evidence: FrameEvidence, requested_frames: int,
                    *, change_window: SampleChangeWindow = SampleChangeWindow.NONE,
                    post_stat_ok: bool = True) -> Tuple[SampleStatus, str]:
    """Turn one decode outcome into a status. Pure: no I/O, no clock, no subprocess.

    Precedence is deliberate, and the first three tiers exist so that apparent progress can never
    outrank what the process itself did:

    1. seam ``error`` -> ``NOT_RUN``. Our tooling failed; we hold no opinion on the file.
    2. ``timed_out`` -> ``TIMEOUT``. **Checked before any frame count**, so partial progress that
       happens to reach the requested total can never become a success.
    3. non-zero exit -> ``FFMPEG_FAILED``, whatever was produced first.
    4. a failed post-decode observation -> ``EVIDENCE_UNTRUSTWORTHY``. Fails closed: it claims no
       change (we did not observe one) and permits no success (we cannot rule one out).
    5. a change observed during the decode -> ``SOURCE_CHANGED``, outranking any success below.
    6. missing terminal block, missing/malformed frame count, a count above the request, or an
       explicitly positive dup/drop -> ``EVIDENCE_UNTRUSTWORTHY``. A count *above* the request
       contradicts ``-frames:v``, so the counter itself is what stops being trustworthy.
    7. ``frame=0`` -> ``ZERO_FRAMES``, ahead of the stderr tier: "nothing was produced" is the
       stronger fact, and it is reported even when the decoder also complained.
    8. ``0 < frame < requested`` -> ``UNDERFILLED_UNVERIFIED``. Inconclusive. Never EOF, never
       damage -- see this module's docstring for why that attribution is unobtainable here.
    9. ``frame == requested`` with stderr output -> ``OUTPUT_WITH_DECODER_ERRORS``. At ``-v error``
       FFmpeg prints nothing on a clean run, so any stderr IS the error signal; no fragile pattern
       matching, and the bias is toward withholding success rather than granting it.
    10. ``frame == requested``, clean -> ``OUTPUT_FRAMES_OK``.

    Unknown dup/drop counters (``None``) do **not** block tier 10: FFmpeg need not emit those keys,
    and requiring them would make success unreachable rather than rigorous. They are reported as
    UNKNOWN and never read as proof of zero. An explicitly positive one does block it.
    """
    if outcome.error:
        return SampleStatus.NOT_RUN, f"The decode could not be run: {_bounded(outcome.error)}"

    if outcome.timed_out:
        seen = "no frame count" if evidence.frames is None else f"{evidence.frames} frame(s)"
        return SampleStatus.TIMEOUT, (
            f"FFmpeg exceeded its decode timeout after reporting {seen}; the sample is UNVERIFIED, "
            f"not corrupt, and partial progress is not a successful decode.")

    if outcome.returncode != 0:
        return SampleStatus.FFMPEG_FAILED, (
            f"FFmpeg exit {outcome.returncode}: {_bounded(outcome.stderr) or 'no error output'}")

    if not post_stat_ok:
        return SampleStatus.EVIDENCE_UNTRUSTWORTHY, (
            "The decode finished, but the post-decode source observation could not be taken, so "
            "stability over the decode window is unverified and no success can be claimed.")

    if change_window in (SampleChangeWindow.DURING_DECODE, SampleChangeWindow.BOTH):
        return SampleStatus.SOURCE_CHANGED, (
            "The source changed while FFmpeg was reading it, so this sample describes bytes that "
            "are already gone.")

    if not evidence.saw_end:
        return SampleStatus.EVIDENCE_UNTRUSTWORTHY, (
            "FFmpeg exited 0 but produced no terminal 'progress=end' block, so the frame evidence "
            "is incomplete.")

    if evidence.frames is None:
        reason = ("reported a malformed frame count" if evidence.malformed_frame
                  else "reported no frame count")
        return SampleStatus.EVIDENCE_UNTRUSTWORTHY, (
            f"FFmpeg exited 0 and {reason}, so no output-frame evidence exists.")

    if evidence.frames > requested_frames:
        return SampleStatus.EVIDENCE_UNTRUSTWORTHY, (
            f"FFmpeg reported {evidence.frames} output frames for a {requested_frames}-frame "
            f"request, which contradicts -frames:v, so the counter is not trustworthy.")

    if (evidence.dup_frames or 0) > 0 or (evidence.drop_frames or 0) > 0:
        return SampleStatus.EVIDENCE_UNTRUSTWORTHY, (
            f"FFmpeg reported dup={evidence.dup_frames} drop={evidence.drop_frames} under "
            f"-fps_mode passthrough, where neither is expected, so the frame count does not mean "
            f"what this check assumes.")

    if evidence.frames == 0:
        return SampleStatus.ZERO_FRAMES, (
            "FFmpeg exited 0 but produced no output frames at the requested position.")

    if evidence.frames < requested_frames:
        return SampleStatus.UNDERFILLED_UNVERIFIED, (
            f"FFmpeg produced {evidence.frames} of {requested_frames} requested output frames. "
            f"INCONCLUSIVE: this is not attributed to end-of-file and not attributed to damage.")

    if _bounded(outcome.stderr):
        return SampleStatus.OUTPUT_WITH_DECODER_ERRORS, (
            f"All {requested_frames} output frames were produced, but FFmpeg reported errors at "
            f"-v error loglevel: {_bounded(outcome.stderr)}")

    return SampleStatus.OUTPUT_FRAMES_OK, (
        f"{requested_frames} output frames were produced at the requested position.")


# ---------------------------------------------------------------------------
# the bounded check
# ---------------------------------------------------------------------------

#: Exception classes that mean "the environment refused", as opposed to a defect in our own code.
#: Both are contained -- the difference is only what the operator is told, because "the probe could
#: not start" and "the probe hit an internal error" lead to different next actions.
_ENVIRONMENTAL = (OSError, ValueError)


def _seam_detail(what: str, exc: BaseException) -> str:
    """One bounded sentence describing a contained seam failure, honest about which kind it was."""
    kind = ("could not be run" if isinstance(exc, _ENVIRONMENTAL)
            else "hit an unexpected internal error")
    return f"The {what} {kind}: {_bounded(f'{type(exc).__name__}: {exc}')}"


def _exit_code(raw: Any) -> int | None:
    """An exit code as an ``int``, or ``None`` for missing/malformed. **Never fabricates a zero.**

    ``int(x) or 0`` was the original coercion and it had two failure modes that both end in a false
    clean result: it raises on an unparseable value, and it turns ``None`` into ``0`` -- a success
    code invented out of an absent one.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(coerce_stream_text(raw).strip())
    except (TypeError, ValueError):
        return None


def _as_outcome(result: Any) -> SampleDecodeOutcome:
    """Normalize whatever the decode seam returned into a real record. Total, never raises.

    The seam always returns a :class:`SampleDecodeOutcome`; an injected or future one might not, and
    a duck-typed object missing ``.error`` or ``.timed_out`` would otherwise raise ``AttributeError``
    out of a function that promises a verdict. A malformed return is a *tooling* failure, so it
    becomes a seam error -- never a returncode of zero.
    """
    if isinstance(result, SampleDecodeOutcome):
        return result
    missing = [name for name in ("returncode", "stdout", "stderr", "timed_out", "error")
               if not hasattr(result, name)]
    if missing:
        return SampleDecodeOutcome(
            error=f"the decode seam returned {type(result).__name__}, which is missing "
                  f"{', '.join(missing)}")
    try:
        return SampleDecodeOutcome(
            returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
            timed_out=bool(result.timed_out), error=coerce_stream_text(result.error))
    except Exception as exc:  # noqa: BLE001 - a malformed record is reported, never raised
        return SampleDecodeOutcome(
            error=f"the decode seam returned an unreadable record: {type(exc).__name__}: {exc}")


def _observe(path: Any, stat) -> SourceFingerprint | None:
    """One stability observation, made total. ``None`` means "could not be taken" -- fails closed.

    :func:`~beatsync_fork.media_verify.fingerprint_source` is used unchanged; this only widens the
    containment around it, because an *injected* ``stat`` may raise anything at all and S2-A's
    narrower ``(OSError, ValueError)`` contract is not ours to alter.
    """
    try:
        return fingerprint_source(path, **({} if stat is None else {"stat": stat}))
    except Exception:  # noqa: BLE001 - an unobservable source is a verdict, never a raise
        return None


def _observe_with_mode(path: Any, stat) -> Tuple[SourceFingerprint | None, int | None]:
    """The baseline observation: ONE stat call yielding both the fingerprint and the file mode.

    One call rather than ``fingerprint_source`` plus a separate mode read, because the stability
    bracket is exactly three observations and a fourth ``stat`` would widen it. The two-line
    fingerprint construction mirrors :func:`~beatsync_fork.media_verify.fingerprint_source` instead
    of changing it -- S2-A's contract is untouched, and observations two and three still go through
    it. ``os.stat`` is the only ``os`` use in this module: the mode is not reachable otherwise.
    """
    try:
        info = (stat or os.stat)(path)
        return (SourceFingerprint(size=int(info.st_size), mtime_ns=int(info.st_mtime_ns)),
                _non_negative_int(getattr(info, "st_mode", None)))
    except Exception:  # noqa: BLE001 - same reason as _observe
        return None, None


def _bad_request(path: Any, requested_frames: Any) -> str:
    """Pure request validation, before any ``stat``, probe or child. ``""`` means acceptable.

    The extension list is :data:`~beatsync_fork.input_manager.SUPPORTED_VIDEO_EXTENSIONS`, imported
    rather than restated so a third copy of ".mp4/.mkv" cannot drift. Comparison is
    case-insensitive, so ``.MP4`` and ``.MKV`` are accepted.
    """
    frames = _non_negative_int(requested_frames)
    if frames is None or frames < 1:
        return f"A sample needs at least one requested frame, not {requested_frames!r}."
    if frames > MAX_SAMPLE_FRAMES:
        return (f"A sample is capped at {MAX_SAMPLE_FRAMES} output frames and {frames} were "
                f"requested. The cap is the bound, not a default to be raised by a caller.")
    if not isinstance(path, str):
        return f"The source path must be a string, not {type(path).__name__}."
    if not path.strip():
        return "The source path is empty."
    name = path.replace("\\", "/").rpartition("/")[2]
    base, dot, ext = name.rpartition(".")
    suffix = f".{ext}".lower() if (dot and base) else ""
    if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
        return (f"{name!r} is not a supported video source "
                f"({', '.join(sorted(SUPPORTED_VIDEO_EXTENSIONS))} only), so there is no video "
                f"stream to sample.")
    return ""


def _window(cross_phase: bool, during_decode: bool) -> SampleChangeWindow:
    if cross_phase and during_decode:
        return SampleChangeWindow.BOTH
    if cross_phase:
        return SampleChangeWindow.CROSS_PHASE
    if during_decode:
        return SampleChangeWindow.DURING_DECODE
    return SampleChangeWindow.NONE


def run_sample_check(path: str, seek_seconds: Any, *,
                     probe: Callable[[str], Any],
                     decode: Callable[..., SampleDecodeOutcome],
                     requested_frames: int = DEFAULT_SAMPLE_FRAMES,
                     decode_timeout: float = DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS,
                     budget_seconds: float = DEFAULT_SAMPLE_BUDGET_SECONDS,
                     stat=None, clock: Callable[[], float] = None) -> SampleVerdict:
    """One bounded sample-decode check. Total by construction: every failure becomes a verdict.

    ``probe`` and ``decode`` are injected, so this orchestrator -- and therefore every status --
    is exercisable with no FFmpeg, no FFprobe and no real media. ``probe`` is expected to be
    ``ffmpeg_processing.probe_media_metadata``: reused for its execution, timeout and launch-error
    semantics only. Its *classifier* is deliberately not consulted; see the module docstring.

    **Three** ``(size, mtime_ns)`` observations are compared, not two: before FFprobe, immediately
    before the decode, and after the decode terminates by any path. The middle one is a fail-closed
    gate -- if the file moved in that window, the metadata that chose the ``-map`` target and the
    duration describes bytes that are gone, so the decoder is never launched. Matching size and
    mtime_ns is not byte-integrity proof; :data:`CHANGE_DETECTION_LIMIT_TEXT` states that bound.

    What "total" means here, precisely
    ----------------------------------
    Every *ordinary* failure becomes a verdict: an injected ``probe`` or ``stat`` that raises, a
    probe whose exit code is malformed or absent, a ``decode`` that raises, and a ``decode`` that
    returns a record this module cannot read. Each is contained with ``except Exception`` --
    never ``BaseException``, so ``KeyboardInterrupt`` and ``SystemExit`` still propagate and a
    cancelled operation is not silently reported as a file problem. Containment is not silence: the
    detail says whether the environment refused (``OSError``/``ValueError``) or the seam hit an
    unexpected internal error, because those lead the operator somewhere different.

    The request itself is bounded before anything is touched. ``requested_frames`` must land in
    ``1..``:data:`MAX_SAMPLE_FRAMES`; the path must be a non-empty ``str`` with a
    :data:`~beatsync_fork.input_manager.SUPPORTED_VIDEO_EXTENSIONS` suffix (case-insensitively) and
    must resolve to a **regular file**. Each failure is :attr:`SampleStatus.INVALID_REQUEST` with no
    child started -- a diagnostic that would sample a directory, a device or a ``.txt`` is not
    bounded by its caller's good manners.
    """
    clock = clock or time.perf_counter
    started = clock()
    budget = WorkBudget(budget_seconds, started=started)

    def verdict(status: SampleStatus, detail: str, **extra: Any) -> SampleVerdict:
        extra.setdefault("stability", Stability.UNVERIFIED)
        return SampleVerdict(
            path=path if isinstance(path, str) else repr(path),
            status=status, detail=detail,
            requested_seek=_finite(seek_seconds),
            requested_frames=(_non_negative_int(requested_frames) or 0),
            elapsed_seconds=float(clock() - started), **extra)

    bad = _bad_request(path, requested_frames)
    if bad:
        return verdict(SampleStatus.INVALID_REQUEST, bad)
    frames = _non_negative_int(requested_frames)
    if not _is_finite(seek_seconds) or _finite(seek_seconds) < 0:
        return verdict(SampleStatus.INVALID_REQUEST,
                       f"The seek position must be a finite, non-negative number of seconds, not "
                       f"{seek_seconds!r}.")

    baseline, mode = _observe_with_mode(path, stat)
    if baseline is None:
        return verdict(SampleStatus.SOURCE_MISSING,
                       "The source could not be observed at all (missing, locked or denied).")
    if mode is None or not S_ISREG(mode):
        return verdict(SampleStatus.INVALID_REQUEST,
                       "The source path exists but is not a regular file (a directory, a device or "
                       "another special object), so there is nothing to sample.")

    try:
        outcome = probe(path)
    except Exception as exc:  # noqa: BLE001 - BaseException deliberately NOT caught; see below
        return verdict(SampleStatus.NOT_RUN, _seam_detail("metadata probe", exc))
    if getattr(outcome, "error", ""):
        return verdict(SampleStatus.NOT_RUN,
                       f"The metadata probe could not be run: "
                       f"{_bounded(getattr(outcome, 'error', ''))}")
    if getattr(outcome, "timed_out", False):
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       "FFprobe exceeded its timeout, so the metadata needed to choose a stream is "
                       "UNAVAILABLE. The file is unverified, not corrupt.")
    probe_exit = _exit_code(getattr(outcome, "returncode", 0))
    if probe_exit != 0:
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       f"FFprobe exit "
                       f"{probe_exit if probe_exit is not None else '(malformed/absent)'}: "
                       f"{_bounded(getattr(outcome, 'stderr', '')) or 'no error output'}")

    try:
        payload = json.loads(coerce_stream_text(getattr(outcome, "stdout", "")) or "")
    except (ValueError, TypeError):
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       "FFprobe exited 0 but its metadata was not parseable JSON.")
    if not isinstance(payload, dict):
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       "FFprobe metadata was not a JSON object.")

    selection = select_renderer_equivalent_stream(payload)
    if selection.ambiguous_reason:
        return verdict(SampleStatus.STREAM_AMBIGUOUS, selection.ambiguous_reason,
                       equivalence=StreamEquivalence.NOT_ESTABLISHED)
    if not selection.ok:
        return verdict(SampleStatus.METADATA_UNUSABLE, selection.invalid_reason)

    chosen = selection.stream
    stream_fields: Dict[str, Any] = {
        "stream_index": chosen.index, "codec": chosen.codec,
        "width": chosen.width, "height": chosen.height, "fps": chosen.fps,
        "duration": chosen.duration, "duration_source": chosen.duration_source,
        "equivalence": chosen.equivalence, "attached_pic": chosen.attached_pic,
    }

    seek = _finite(seek_seconds)
    if seek >= chosen.duration:
        return verdict(SampleStatus.SEEK_OUT_OF_RANGE,
                       f"The requested seek {seek:.3f}s is at or past the selected stream's "
                       f"reported duration {chosen.duration:.3f}s "
                       f"({chosen.duration_source.value}).", **stream_fields)

    pre = _observe(path, stat)
    if pre is None:
        return verdict(SampleStatus.SOURCE_MISSING,
                       "The source stopped being observable before the decode could start.",
                       **stream_fields)
    if pre != baseline:
        return verdict(SampleStatus.SOURCE_CHANGED,
                       "The source changed between the metadata read and the decode, so the "
                       "metadata that chose the stream and the duration describes bytes that are "
                       "already gone. The decoder was NOT launched.",
                       change_window=SampleChangeWindow.CROSS_PHASE,
                       stability=Stability.CHANGED, **stream_fields)

    # The budget is re-measured here, after the last potentially blocking pre-decode stat, so no
    # child is started that the budget could not let finish. A child killed by the budget rather
    # than by its own timeout would be an ambiguous artifact.
    if budget.remaining(clock()) < float(decode_timeout):
        return verdict(SampleStatus.BUDGET_EXHAUSTED,
                       f"Less than the {float(decode_timeout):.0f}s decode timeout remained in the "
                       f"{float(budget_seconds):.0f}s scheduling budget, so no decode was started.",
                       **stream_fields)

    decode_started = clock()
    try:
        raw_result = decode(path, seek_seconds=seek, stream_index=chosen.index,
                            requested_frames=frames, timeout=decode_timeout)
    except Exception as exc:  # noqa: BLE001 - a seam crash is a reported outcome, never a raise
        raw_result = SampleDecodeOutcome(error=_seam_detail("decode", exc))
    decode_seconds = float(clock() - decode_started)

    result = _as_outcome(raw_result)
    exit_code = _exit_code(result.returncode)
    if not result.timed_out and not result.error and exit_code is None:
        # A malformed exit code is a tooling failure, not a file verdict -- and specifically not a
        # zero. Converting it to a seam error keeps it out of every success tier.
        #
        # Its ``stdout`` is deliberately DROPPED, which is the one thing this branch must do beyond
        # renaming the failure. A record whose exit code cannot be read is not a completed decode
        # record, so its progress stream is not trustworthy evidence either -- and carrying it
        # forward put a frame count in a report that simultaneously said no child was confirmed to
        # have started. That is the same defect class as deriving an execution label from a status:
        # two lines of one report sourced from facts of different strength. ``observed_frames``
        # therefore stays ``None`` here and the report reads "not established", exactly as it does
        # for a refused spawn. ``stderr`` is kept -- it is bounded diagnostic context about the
        # malformed outcome, not a measurement -- and the malformed value itself is quoted in the
        # detail. Note the guards: a genuine ``timed_out`` never enters this branch, so a timeout's
        # partial progress is still preserved, and a legitimate non-zero exit has a readable code,
        # so its frame evidence is untouched.
        result = SampleDecodeOutcome(
            stderr=result.stderr,
            error=f"the decode seam reported a malformed exit code {result.returncode!r}")

    # Observation three, on EVERY terminal path above -- success, non-zero exit, timeout, spawn
    # error and an unexpected seam exception alike.
    post = _observe(path, stat)
    during_decode = post is not None and post != pre
    post_stat_ok = post is not None

    evidence = parse_progress_output(result.stdout)
    window = _window(False, during_decode)
    status, detail = classify_decode(result, evidence, frames,
                                     change_window=window, post_stat_ok=post_stat_ok)
    if not post_stat_ok:
        stability = Stability.UNVERIFIED
    elif during_decode:
        stability = Stability.CHANGED
    else:
        stability = Stability.STABLE

    # A refused spawn and an unexpected seam exception both leave child-start UNCONFIRMED; a
    # timeout confirms one started, because it had to in order to be killed by its own clock.
    #
    # The remaining split is "did the sample earn the sole positive status?", NOT "how many frames
    # came out". Those are different questions and the status cannot answer the second one: tiers
    # 1-6 of classify_decode outrank every frame tier, so OUTPUT_WITH_DECODER_ERRORS, SOURCE_CHANGED,
    # EVIDENCE_UNTRUSTWORTHY, TIMEOUT and FFMPEG_FAILED are all reachable with a terminal block
    # reporting the full requested count. RAN_WITHOUT_CLEAN_RESULT is therefore worded to assert
    # only what this branch establishes; the frame count travels separately as observed_frames.
    if result.error:
        execution = DecodeExecution.START_NOT_CONFIRMED
    elif status is SampleStatus.OUTPUT_FRAMES_OK:
        execution = DecodeExecution.REQUESTED_FRAMES_PRODUCED
    else:
        execution = DecodeExecution.RAN_WITHOUT_CLEAN_RESULT

    return SampleVerdict(
        path=path, status=status, detail=detail,
        requested_seek=seek, requested_frames=frames,
        observed_frames=evidence.frames,
        dup_frames=evidence.dup_frames, drop_frames=evidence.drop_frames,
        returncode=(None if (result.timed_out or result.error) else _exit_code(result.returncode)),
        elapsed_seconds=float(clock() - started), decode_seconds=decode_seconds,
        execution=execution,
        change_window=window, stability=stability,
        diagnostic=_bounded(result.stderr),
        **stream_fields)


# ---------------------------------------------------------------------------
# the operator report
# ---------------------------------------------------------------------------

def format_sample_report(verdict: SampleVerdict) -> str:
    """One operator-facing report for one sample check.

    The full source path is never shortened -- this runs locally for the one person who owns the
    files, and a truncated path is useless for finding the file again.

    Three honesty rules govern the wording. First, **what happened is stated, not implied**: the
    ``Decode:`` line carries :class:`DecodeExecution` verbatim, so a report whose child never
    started says so in its own words rather than leaving the reader to infer it from a status code.
    Second, **the unconditional coverage sentence is hypothetical**. It appears exactly once, it is
    phrased "even a successful sample...", and it is therefore true on a report where nothing was
    decoded -- which the previous past-tense phrasing was not. No line upgrades a sampled window
    into a statement about the whole file, and no line claims a measurement that was not taken:
    geometry, frame rate and duration all read "unknown"/"not established" when no stream was ever
    selected, rather than ``0x0`` and ``0.000``.

    Third, **no two lines of one report may contradict each other.** The ``Decode:`` line describes
    process execution; ``Observed output frames`` reports the frame evidence; ``Result`` and
    ``Detail`` carry the verdict. Each states only what its own source establishes. An earlier
    ``Decode:`` wording claimed underproduction for every non-clean outcome, which on
    ``SAMPLE_OUTPUT_WITH_DECODER_ERRORS`` sat in the same report as a ``Detail`` confirming all
    requested frames were produced and an ``Observed output frames`` equal to the request. A report
    that argues with itself tells the operator nothing.

    That rule has a second edge, closed separately: a ``Decode:`` line **denying** confirmed
    child-start evidence may not sit beside a frame count either. Both halves are now structural
    rather than editorial -- ``run_sample_check`` discards the untrusted ``stdout`` of a record
    whose exit code it could not read, so ``START_NOT_CONFIRMED`` always renders "not established".

    ``Observed output frames`` renders :attr:`SampleVerdict.observed_frames` **verbatim**, and that
    is load-bearing in both directions: it is never clamped to the request, never defaulted to zero
    and never substituted from ``requested_frames``. A count *above* the request is real evidence --
    it is exactly what makes ``SAMPLE_EVIDENCE_UNTRUSTWORTHY`` the right verdict, because it
    contradicts ``-frames:v`` -- so hiding it behind a plausible-looking 30 would conceal the very
    anomaly the status is reporting.
    """
    frames = "not established" if verdict.observed_frames is None else str(verdict.observed_frames)
    dup = "unknown" if verdict.dup_frames is None else str(verdict.dup_frames)
    drop = "unknown" if verdict.drop_frames is None else str(verdict.drop_frames)
    selected = verdict.stream_index is not None
    stream = str(verdict.stream_index) if selected else "not selected"
    equivalence = verdict.equivalence.value or "unknown"
    geometry = (f"{verdict.codec or 'unknown'} / {verdict.width}x{verdict.height} / "
                f"{verdict.fps:.3f}" if selected else "unknown / unknown / unknown")
    duration = (f"{verdict.duration:.3f}s (from {verdict.duration_source.value or 'unavailable'})"
                if selected and verdict.duration > 0 else "not established")
    decode_time = ("not reached" if verdict.decode_seconds is None
                   else f"{verdict.decode_seconds:.2f}s")

    lines = [
        "Sample decode check",
        f"  Source:                      {verdict.path}",
        f"  Result:                      {verdict.status.value}",
        f"  Detail:                      {verdict.detail}",
        f"  Decode:                      {_EXECUTION_TEXT[verdict.execution]}",
        f"  Tested stream index:         {stream}",
        f"  Renderer equivalence:        {equivalence}",
        f"  Stream codec / size / fps:   {geometry}",
        f"  Reported duration:           {duration}",
        f"  Requested seek:              {verdict.requested_seek:.3f}s",
        f"  Requested output frames:     {verdict.requested_frames}",
        f"  Observed output frames:      {frames}",
        f"  Duplicated / dropped:        {dup} / {drop}",
        f"  FFmpeg exit code:            "
        f"{'not reached' if verdict.returncode is None else verdict.returncode}",
        f"  Elapsed (whole check):       {verdict.elapsed_seconds:.2f}s",
        f"  Decode call:                 {decode_time}",
        f"  Source stability:            {verdict.stability.value}"
        + (f" ({verdict.change_window.value})" if verdict.changed else ""),
    ]
    if verdict.diagnostic:
        lines.append(f"  FFmpeg output:               {verdict.diagnostic}")
    lines.append("")
    if verdict.attached_pic:
        lines.append("  The selected video stream is flagged as an attached picture (cover art or "
                     "a thumbnail), not an ordinary moving-picture stream. A one-frame attached "
                     "picture will legitimately underfill the request, so a short result here is "
                     "expected and says nothing about damage.")
    lines.append(f"  {SAMPLE_ONLY_TEXT}")
    lines.append(f"  {CHANGE_DETECTION_LIMIT_TEXT}")
    if verdict.observed_frames is not None and verdict.dup_frames is None:
        lines.append("  Duplication/drop counters were not reported; UNKNOWN is not a measurement "
                     "of zero.")
    return "\n".join(lines)
