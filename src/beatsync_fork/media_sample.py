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
:data:`SAMPLE_ONLY_TEXT` is the sentence every report carries. A successful sample is
**one window of one stream** -- it is not proof of whole-file integrity, not proof the production
NVENC extraction would succeed, and not proof of renderability. The single positive status is named
:attr:`SampleStatus.OUTPUT_FRAMES_OK` rather than anything resembling a health claim, and the word
"output" in it is load-bearing: FFmpeg's ``frame=`` counter counts frames **written to the output
pipeline**, so with ``-ss`` before ``-i`` the frames decoded during accurate-seek pre-roll are
exercised but never counted. The counter therefore understates decoder work and must never be
described as "frames decoded" or "frames verified".

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
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Tuple

from beatsync_fork.media_verify import (
    CHANGE_DETECTION_LIMIT_TEXT,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
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
    "SAMPLE_ONLY_TEXT",
    "SOURCE_COUNT_CAP",
    "WORKER_CAP",
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
#: verbatim in every report.
SAMPLE_ONLY_TEXT = (
    "One sampled window of one stream was decoded to a discarded output -- this is not proof of "
    "whole-file integrity, not proof the render's NVENC extraction would succeed, and not proof of "
    "full renderability."
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
    """What FFmpeg's ``-progress`` stream actually said. ``None`` always means *unavailable*."""

    saw_end: bool = False
    """Did the final ``progress`` key carry the documented terminal value ``end``?"""
    frames: int | None = None
    """Output frames reported, or ``None`` when the key was missing or malformed."""
    dup_frames: int | None = None
    """``None`` is UNKNOWN -- never silently converted to zero, and never read as proof of zero."""
    drop_frames: int | None = None
    malformed_frame: bool = False
    """A ``frame`` key existed but did not parse as a non-negative integer."""
    blocks: int = 0
    """How many progress blocks were seen (a terminal block counts)."""


def parse_progress_output(raw: Any) -> FrameEvidence:
    """Parse ``-progress`` key/value output. Pure and total -- never raises, never fabricates.

    The documented wire format is ``key=value`` lines where *"the last key of a sequence of progress
    information is always 'progress' with the value 'continue' or 'end'"*. So blocks repeat, and the
    **last** value seen for each key is the operative one.

    Deliberately tolerant of anything that is not a ``key=value`` line: a banner, a muxer byte, a
    partial trailing write. That tolerance is also what makes the choice of null output target
    harmless -- stray bytes on the progress channel cannot corrupt the frame evidence, because they
    are not ``key=value`` and are skipped.

    Accepts ``str``, ``bytes``, ``None`` and invalid UTF-8 (see :func:`coerce_stream_text`), and
    handles CRLF and LF alike via :meth:`str.splitlines`.
    """
    text = coerce_stream_text(raw)
    last: Dict[str, str] = {}
    blocks = 0
    saw_frame_key = False
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        last[key] = value
        if key == "frame":
            saw_frame_key = True
        elif key == "progress":
            blocks += 1

    frames = _non_negative_int(last.get("frame")) if saw_frame_key else None
    return FrameEvidence(
        saw_end=last.get("progress") == "end",
        frames=frames,
        dup_frames=_non_negative_int(last.get("dup_frames")),
        drop_frames=_non_negative_int(last.get("drop_frames")),
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
    stream_index: int | None = None
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    duration_source: DurationSource = DurationSource.NONE
    equivalence: StreamEquivalence = StreamEquivalence.UNKNOWN
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
        """Did a decode child actually run? ``returncode``/``timed_out`` are only set when it did."""
        return self.returncode is not None or self.status in (
            SampleStatus.TIMEOUT, SampleStatus.NOT_RUN)


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
        f"{requested_frames} output frames were produced at the requested position. "
        f"{SAMPLE_ONLY_TEXT}")


# ---------------------------------------------------------------------------
# the bounded check
# ---------------------------------------------------------------------------

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
    """
    clock = clock or time.perf_counter
    stat_kwargs = {} if stat is None else {"stat": stat}
    started = clock()
    budget = WorkBudget(budget_seconds, started=started)

    def verdict(status: SampleStatus, detail: str, **extra: Any) -> SampleVerdict:
        extra.setdefault("stability", Stability.UNVERIFIED)
        return SampleVerdict(
            path=path, status=status, detail=detail,
            requested_seek=_finite(seek_seconds),
            requested_frames=(_non_negative_int(requested_frames) or 0),
            elapsed_seconds=float(clock() - started), **extra)

    frames = _non_negative_int(requested_frames)
    if frames is None or frames < 1:
        return verdict(SampleStatus.INVALID_REQUEST,
                       f"A sample needs at least one requested frame, not {requested_frames!r}.")
    if not _is_finite(seek_seconds) or _finite(seek_seconds) < 0:
        return verdict(SampleStatus.INVALID_REQUEST,
                       f"The seek position must be a finite, non-negative number of seconds, not "
                       f"{seek_seconds!r}.")

    baseline = fingerprint_source(path, **stat_kwargs)
    if baseline is None:
        return verdict(SampleStatus.SOURCE_MISSING,
                       "The source could not be observed at all (missing, locked or denied).")

    outcome = probe(path)
    if getattr(outcome, "error", ""):
        return verdict(SampleStatus.NOT_RUN,
                       f"The metadata probe could not be run: "
                       f"{_bounded(getattr(outcome, 'error', ''))}")
    if getattr(outcome, "timed_out", False):
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       "FFprobe exceeded its timeout, so the metadata needed to choose a stream is "
                       "UNAVAILABLE. The file is unverified, not corrupt.")
    if int(getattr(outcome, "returncode", 0) or 0) != 0:
        return verdict(SampleStatus.METADATA_UNUSABLE,
                       f"FFprobe exit {outcome.returncode}: "
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
        "equivalence": chosen.equivalence,
    }

    seek = _finite(seek_seconds)
    if seek >= chosen.duration:
        return verdict(SampleStatus.SEEK_OUT_OF_RANGE,
                       f"The requested seek {seek:.3f}s is at or past the selected stream's "
                       f"reported duration {chosen.duration:.3f}s "
                       f"({chosen.duration_source.value}).", **stream_fields)

    pre = fingerprint_source(path, **stat_kwargs)
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
        result = decode(path, seek_seconds=seek, stream_index=chosen.index,
                        requested_frames=frames, timeout=decode_timeout)
    except Exception as exc:  # noqa: BLE001 - a seam crash is a reported outcome, never a raise
        result = SampleDecodeOutcome(error=f"{type(exc).__name__}: {exc}")
    decode_seconds = float(clock() - decode_started)

    # Observation three, on EVERY terminal path above -- success, non-zero exit, timeout, spawn
    # error and an unexpected seam exception alike.
    post = fingerprint_source(path, **stat_kwargs)
    during_decode = post is not None and post != pre
    post_stat_ok = post is not None

    evidence = parse_progress_output(getattr(result, "stdout", ""))
    window = _window(False, during_decode)
    status, detail = classify_decode(result, evidence, frames,
                                     change_window=window, post_stat_ok=post_stat_ok)
    if not post_stat_ok:
        stability = Stability.UNVERIFIED
    elif during_decode:
        stability = Stability.CHANGED
    else:
        stability = Stability.STABLE

    return SampleVerdict(
        path=path, status=status, detail=detail,
        requested_seek=seek, requested_frames=frames,
        observed_frames=evidence.frames,
        dup_frames=evidence.dup_frames, drop_frames=evidence.drop_frames,
        returncode=(None if (result.timed_out or result.error) else int(result.returncode)),
        elapsed_seconds=decode_seconds,
        change_window=window, stability=stability,
        diagnostic=_bounded(getattr(result, "stderr", "")),
        **stream_fields)


# ---------------------------------------------------------------------------
# the operator report
# ---------------------------------------------------------------------------

def format_sample_report(verdict: SampleVerdict) -> str:
    """One operator-facing report for one sample check.

    The full source path is never shortened -- this runs locally for the one person who owns the
    files, and a truncated path is useless for finding the file again. Every report carries
    :data:`SAMPLE_ONLY_TEXT` and :data:`CHANGE_DETECTION_LIMIT_TEXT`, and no line ever upgrades a
    sampled window into a statement about the whole file.
    """
    frames = "not established" if verdict.observed_frames is None else str(verdict.observed_frames)
    dup = "unknown" if verdict.dup_frames is None else str(verdict.dup_frames)
    drop = "unknown" if verdict.drop_frames is None else str(verdict.drop_frames)
    stream = "not selected" if verdict.stream_index is None else str(verdict.stream_index)
    equivalence = verdict.equivalence.value or "unknown"
    duration_source = verdict.duration_source.value or "unavailable"

    lines = [
        "Sample decode check",
        f"  Source:                      {verdict.path}",
        f"  Result:                      {verdict.status.value}",
        f"  Detail:                      {verdict.detail}",
        f"  Tested stream index:         {stream}",
        f"  Renderer equivalence:        {equivalence}",
        f"  Stream codec / size / fps:   {verdict.codec or 'unknown'} / "
        f"{verdict.width}x{verdict.height} / {verdict.fps:.3f}",
        f"  Reported duration:           {verdict.duration:.3f}s (from {duration_source})",
        f"  Requested seek:              {verdict.requested_seek:.3f}s",
        f"  Requested output frames:     {verdict.requested_frames}",
        f"  Observed output frames:      {frames}",
        f"  Duplicated / dropped:        {dup} / {drop}",
        f"  FFmpeg exit code:            "
        f"{'not reached' if verdict.returncode is None else verdict.returncode}",
        f"  Elapsed:                     {verdict.elapsed_seconds:.2f}s",
        f"  Source stability:            {verdict.stability.value}"
        + (f" ({verdict.change_window.value})" if verdict.changed else ""),
    ]
    if verdict.diagnostic:
        lines.append(f"  FFmpeg output:               {verdict.diagnostic}")
    lines.append("")
    lines.append(f"  {SAMPLE_ONLY_TEXT}")
    lines.append(f"  {CHANGE_DETECTION_LIMIT_TEXT}")
    if verdict.observed_frames is not None and verdict.dup_frames is None:
        lines.append("  Duplication/drop counters were not reported; UNKNOWN is not a measurement "
                     "of zero.")
    return "\n".join(lines)
