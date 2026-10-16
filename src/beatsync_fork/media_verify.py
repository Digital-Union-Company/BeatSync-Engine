"""[FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source health verification.

Stdlib only, per CLAUDE.md's hard rule for ``src/beatsync_fork/`` — no ``logger``, ``paths``,
``gradio``, ``cupy``, ``cv2``, ``librosa`` or ``numpy``. The probe *subprocess* lives in
``src/ffmpeg_processing.py``; everything here is the pure part: statuses, result records,
the bounded work policy, aggregation and the operator report.

What this module is for
-----------------------
The owner sometimes overwrites previously analysed video files and needs to know, quickly, whether
the library on disk still corresponds to reusable Stage-5 cache records, and whether those files
have basic technical problems. Rebuilding Qwen cache to find out costs GPU hours; this answers the
question with one bounded FFprobe metadata read per source.

**Two independent questions, two independent verdicts.** Cache reuse and media health are not the
same property and are never merged into one score. A source can be perfectly readable and have no
reusable cache (it was overwritten, so it re-keyed), or be unreadable while a stale record still
sits under its old identity. The report keeps them in separate sections for that reason.

The honesty boundary that matters most
--------------------------------------
``MEDIA_METADATA_OK`` means **the container header parsed and a video stream was described**. It is
not proof that every frame decodes. FFprobe reads metadata; it does not decode the picture. The
D0-class failure this milestone was motivated by — a truncated MP4 with no ``moov`` atom — *is*
caught by a metadata read, but a file whose header is intact and whose middle is shredded is not.
:data:`METADATA_NOT_DECODE_TEXT` is the one sentence every report carries so that distinction
cannot be lost between here and the operator, and no code path in this module ever upgrades a
metadata read into a decode claim.

A probe timeout is likewise **not** a corruption verdict. A 5400-RPM drive under a concurrent read
load times out for reasons that have nothing to do with the file's bytes, so a timeout is reported
as a timeout and the source is left unverified.

One click, one snapshot — and where it is not
---------------------------------------------
A verification click answers the cache question and the media question in sequence, so the two
answers are taken at different moments. R3 made that gap observable instead of assumed: one cheap
``(size, mtime_ns)`` baseline is taken per source *before* the cache phase, and
:func:`_verify_one` compares it against the two observations it already took around FFprobe. A
source that moved in **either** window is :attr:`MediaStatus.CHANGED_DURING_CHECK` with the window
recorded in :class:`ChangeWindow`, and the report says plainly that the cache verdict recorded for
it is inconclusive too. Before R3 such a source reported ``PREPARED`` beside ``METADATA_OK`` —
two confident verdicts about two different versions of one file, under a single ``COMPLETE``
banner. :data:`CHANGE_DETECTION_LIMIT_TEXT` states the bound of the detector in every report: it
catches an accidental overwrite, which is what the owner actually does, and it is not a byte-level
integrity proof.

The classification vocabulary is owned elsewhere (R2)
-----------------------------------------------------
The cache section does not classify anything. It *renders* what
``video_analysis.classify_library_sources()`` produced, and that producer emits the **values** of
:class:`beatsync_fork.library_prep.PrepStatus` and :class:`~beatsync_fork.library_prep.NeedReason`
— ``prepared``, ``needs_analysis``, ``source_identity_unavailable``, ``new_or_changed``,
``incomplete_or_invalid``. Those enums are therefore imported and compared against directly.

This is not a style preference. R2 corrected five comparisons here that had been written against
the enum *member names* (``"NEW_OR_CHANGED"``) instead of the values the producer actually emits.
Uppercase never matched, so all five filters were unreachable for genuine data: the two reason
counts always rendered ``0`` and the three per-source detail blocks were always empty, which meant
the report named **no** affected path while asserting that every source had a reusable record. The
fix is to share the vocabulary rather than restate it — a second spelling of a status is a second
source of truth, and this module has no authority to mint one. Do not reintroduce a literal, and
do not add a normalisation layer that would accept both spellings: accepting a spelling the
producer cannot emit only hides the next drift.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Sequence, Tuple

from beatsync_fork.library_prep import NeedReason, PrepStatus

__all__ = [
    "CHANGE_DETECTION_LIMIT_TEXT",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_PROBE_WORKERS",
    "MAX_PROBE_WORKERS",
    "MAX_REPORT_DETAIL_LINES",
    "METADATA_NOT_DECODE_TEXT",
    "NO_BASELINE",
    "ChangeWindow",
    "MediaStatus",
    "MediaVerdict",
    "ProbeOutcome",
    "SourceFingerprint",
    "VerificationRun",
    "WorkBudget",
    "aggregate_media",
    "classify_probe",
    "fingerprint_source",
    "fingerprint_sources",
    "format_verification_report",
    "resolve_worker_count",
    "verify_media_health",
]


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------

#: Hard ceiling on concurrent FFprobe children. D0 measured a 41-file sample on the owner's
#: mechanical source drive: 1.50 s sequential, 0.59 s at four workers. Beyond four, a 5400-RPM
#: spindle is seeking rather than reading, and verification would start competing with itself.
#: This is a CAP, not a target — :func:`resolve_worker_count` clamps into it.
MAX_PROBE_WORKERS = 4

#: The measured default. Deliberately equal to the cap rather than lower: four is the configuration
#: that was actually measured, and a slower unmeasured default would be a guess dressed as caution.
DEFAULT_PROBE_WORKERS = 4

#: Per-file wall clock for one FFprobe child. Generous against the ~37 ms/file D0 measured, because
#: the bound exists to stop a *stalled* probe (a dying drive, a disconnected network share), not to
#: race a healthy one. A file that exceeds it is reported ``MEDIA_PROBE_TIMEOUT``, never corrupt.
DEFAULT_PROBE_TIMEOUT_SECONDS = 20

#: Overall work budget for one verification click. Once exceeded, no NEW probe is scheduled and
#: every unreached source is reported ``MEDIA_NOT_CHECKED`` — the run becomes explicitly PARTIAL
#: rather than silently shorter. D0's ~30 s extrapolation for ~2,000 sources was never measured on
#: the whole library, so this is sized well above it and is NOT a performance promise.
DEFAULT_BUDGET_SECONDS = 600

#: How many per-source problem lines each report section prints before summarising the remainder.
#: Bounded because 2,000 failures must not become a 2,000-line textbox; a displayed failure's own
#: path is never shortened, and the number omitted is always stated.
MAX_REPORT_DETAIL_LINES = 25

#: The sentence that keeps a metadata read from being read as a decode guarantee. Carried verbatim
#: in every report.
METADATA_NOT_DECODE_TEXT = "Metadata readable — full video decoding not verified."

#: The second honesty sentence, added in R3 alongside cross-phase change detection. Every window
#: this module brackets is bracketed with ``(size, mtime_ns)``, so the detector catches the
#: accidental overwrite the milestone exists to find and makes no stronger claim than that.
CHANGE_DETECTION_LIMIT_TEXT = (
    "Change detection compares size and modification time only: it catches an accidental "
    "overwrite, and it is not whole-file byte-integrity proof."
)


class MediaStatus(str, Enum):
    """Read-only media-health reporting labels.

    These are **report** labels. None of them is a cache-generation token, none reaches a cache key,
    a Stage-5 payload, the completion rule or any persisted record, and nothing in this module
    writes them anywhere but the operator's report.
    """

    METADATA_OK = "MEDIA_METADATA_OK"
    UNREADABLE = "MEDIA_UNREADABLE"
    NO_VIDEO_STREAM = "MEDIA_NO_VIDEO_STREAM"
    INVALID_METADATA = "MEDIA_INVALID_METADATA"
    PROBE_TIMEOUT = "MEDIA_PROBE_TIMEOUT"
    CHANGED_DURING_CHECK = "MEDIA_CHANGED_DURING_CHECK"
    NOT_CHECKED = "MEDIA_NOT_CHECKED"


#: Every status except ``METADATA_OK`` needs operator attention. Derived, never hand-maintained, so
#: a status added later cannot be silently omitted from the problem list.
_PROBLEM_STATUSES = tuple(s for s in MediaStatus if s is not MediaStatus.METADATA_OK)


class ChangeWindow(str, Enum):
    """*Which* window a detected source change was observed in.

    R3 added the first of these. Before it, stability was only bracketed around the FFprobe call,
    so a file overwritten after its Stage-5 cache classification but before its probe produced
    ``PREPARED`` (describing the old bytes) beside ``METADATA_OK`` (describing the new ones), under
    one "Verified at" header and a ``COMPLETE`` banner — two verdicts about different files,
    presented as one snapshot.

    The window is carried as a field rather than folded into the detail text so the report, and a
    test, can distinguish the two cases structurally instead of by reading prose.
    """

    NONE = ""
    #: Changed after the cache phase observed it and before its probe began. The cache verdict
    #: recorded for this source is as inconclusive as the media verdict.
    CROSS_PHASE = "cross_phase"
    #: Changed while FFprobe was reading it — the original S2-A bracket.
    DURING_PROBE = "during_probe"
    #: Both windows. Reported as both, never collapsed to the narrower one.
    BOTH = "both"


_CHANGED_WINDOWS = (ChangeWindow.CROSS_PHASE, ChangeWindow.DURING_PROBE, ChangeWindow.BOTH)


def _bounded(text: Any, limit: int = 200) -> str:
    """One whitespace-collapsed line, bounded, with truncation marked."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# source stability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFingerprint:
    """``(size, mtime_ns)`` for one source, or :data:`None` where it could not be stat-ed.

    Deliberately the same cheap pair the source-confirmation snapshot uses, and deliberately **not**
    presented as proof of byte identity. It detects the overwrite this milestone exists to find — a
    file replaced by different content — and it does not detect an in-place edit that preserves both
    size and mtime. Stage-5's bounded fingerprint identity has exactly the same character; neither
    is a whole-file hash and this module never claims otherwise.
    """

    size: int
    mtime_ns: int


def fingerprint_source(path: str, *, stat=os.stat) -> SourceFingerprint | None:
    """Fingerprint one source, or ``None`` if it cannot be stat-ed (missing, locked, denied)."""
    try:
        info = stat(path)
    except (OSError, ValueError):
        return None
    return SourceFingerprint(size=int(info.st_size), mtime_ns=int(info.st_mtime_ns))


class _Unobserved:
    """The "no baseline was taken for this source" sentinel type.

    A distinct object rather than ``None``, because ``None`` already means something different and
    load-bearing: *a baseline WAS taken and the file could not be stat-ed*. Collapsing the two
    would make "we never looked" indistinguishable from "it was missing when we looked", and the
    second of those is a cross-phase change the moment the file reappears.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return "NO_BASELINE"


#: Default baseline for a source nobody observed before the cache phase. A run without baselines
#: behaves exactly as S2-A did: the probe bracket only.
NO_BASELINE = _Unobserved()


def fingerprint_sources(paths: Sequence[str], *,
                        stat=os.stat) -> List[SourceFingerprint | None]:
    """One baseline observation per source, **by position**.

    Positional, never a path-keyed mapping: the Stage-5 classifier treats position as the
    authority, so a library containing the same path twice is two independent entries with two
    independent verdicts. A dict would silently merge them and then hand the merged observation to
    both. One ``os.stat`` per source and nothing else — no open, no read, no hash.
    """
    return [fingerprint_source(str(path), stat=stat) for path in paths]


# ---------------------------------------------------------------------------
# work budget
# ---------------------------------------------------------------------------

class WorkBudget:
    """A monotonic deadline for one verification click.

    Takes its clock as an argument rather than reading one, so the partial-run behaviour is testable
    without sleeping. It only ever *stops scheduling*: work already handed to a worker is always
    awaited and reaped, because abandoning a live FFprobe child would leak the process this whole
    module exists to keep bounded.
    """

    def __init__(self, seconds: float, *, started: float = 0.0) -> None:
        self.seconds = float(seconds)
        self.started = float(started)

    def exhausted(self, now: float) -> bool:
        return (float(now) - self.started) >= self.seconds

    def remaining(self, now: float) -> float:
        return max(0.0, self.seconds - (float(now) - self.started))


def resolve_worker_count(requested: Any = None) -> int:
    """Clamp a requested worker count into ``1..MAX_PROBE_WORKERS``.

    Total: a junk value falls back to the measured default rather than raising, because a bad tuning
    value must not be able to fail a read-only verification click.
    """
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = DEFAULT_PROBE_WORKERS
    if value < 1:
        value = 1
    return min(value, MAX_PROBE_WORKERS)


# ---------------------------------------------------------------------------
# probe results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeOutcome:
    """What the execution seam in ``ffmpeg_processing`` hands back for one source.

    ``timed_out`` is a distinct field rather than a sentinel return code: a timeout is a different
    fact from a non-zero exit, and collapsing them is how a slow drive becomes a corruption report.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""
    """Set when the seam itself failed (executable missing, OS refusal) — not a verdict on the file."""


@dataclass(frozen=True)
class MediaVerdict:
    """One source's media-health result. ``status`` is the only authority; the rest is detail."""

    path: str
    status: MediaStatus
    detail: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    probe_seconds: float = 0.0
    change_window: ChangeWindow = ChangeWindow.NONE
    """Which stability window a change was observed in, if any. Appended last so every existing
    keyword construction is unaffected, and defaulted to ``NONE`` so a verdict that never observed
    a change cannot accidentally claim one."""

    @property
    def is_problem(self) -> bool:
        return self.status is not MediaStatus.METADATA_OK

    @property
    def changed(self) -> bool:
        """Was this source observed to change in ANY bracketed window?"""
        return self.change_window in _CHANGED_WINDOWS

    @property
    def changed_before_probe(self) -> bool:
        """Did it change between the cache phase observing it and the probe starting?

        This is the one that makes the *cache* verdict inconclusive: the classification was taken
        against bytes that were already gone by the time the probe ran.
        """
        return self.change_window in (ChangeWindow.CROSS_PHASE, ChangeWindow.BOTH)


def _finite(value: Any) -> float:
    """A finite float, or ``0.0``. Never raises, never returns NaN or an infinity."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):
        return 0.0
    return out


def _parse_frame_rate(raw: Any) -> float:
    """FFprobe's ``r_frame_rate`` is a rational string like ``30000/1001``."""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        denominator = _finite(den)
        if denominator == 0.0:
            return 0.0
        return _finite(num) / denominator
    return _finite(text)


def classify_probe(path: str, outcome: ProbeOutcome, *,
                   probe_seconds: float = 0.0) -> MediaVerdict:
    """Turn one raw FFprobe outcome into a verdict. Pure: no I/O, no clock, no subprocess.

    Order is deliberate — the cheapest and most certain disqualifications come first, and a
    structural failure is never allowed to fall through into a field-level complaint:

    1. ``timed_out`` -> ``PROBE_TIMEOUT``. Checked first and never reinterpreted.
    2. seam ``error`` -> ``NOT_CHECKED``. Our tooling failed, so we hold no opinion on the file.
    3. non-zero exit -> ``UNREADABLE``. FFprobe could not open or parse the container.
    4. unparseable stdout -> ``INVALID_METADATA``.
    5. no video stream -> ``NO_VIDEO_STREAM`` (an audio-only or data-only file).
    6. unusable geometry/duration -> ``INVALID_METADATA``.
    7. otherwise ``METADATA_OK`` — metadata only, decoding NOT verified.
    """
    if outcome.timed_out:
        return MediaVerdict(
            path=path, status=MediaStatus.PROBE_TIMEOUT, probe_seconds=probe_seconds,
            detail="FFprobe exceeded its per-file timeout; the file is UNVERIFIED, not corrupt.")

    if outcome.error:
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED, probe_seconds=probe_seconds,
            detail=f"Probe could not be run: {_bounded(outcome.error)}")

    if outcome.returncode != 0:
        return MediaVerdict(
            path=path, status=MediaStatus.UNREADABLE, probe_seconds=probe_seconds,
            detail=f"FFprobe exit {outcome.returncode}: "
                   f"{_bounded(outcome.stderr) or 'no error output'}")

    try:
        payload = json.loads(outcome.stdout or "")
    except (ValueError, TypeError):
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            detail="FFprobe exited 0 but its metadata was not parseable JSON.")
    if not isinstance(payload, dict):
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            detail="FFprobe metadata was not a JSON object.")

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    video = next((s for s in streams
                  if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        return MediaVerdict(
            path=path, status=MediaStatus.NO_VIDEO_STREAM, probe_seconds=probe_seconds,
            detail=f"Container parsed, but it declares no video stream "
                   f"({len(streams)} stream(s) total).")

    container = payload.get("format")
    container = container if isinstance(container, dict) else {}
    codec = str(video.get("codec_name") or "")
    width = int(_finite(video.get("width")))
    height = int(_finite(video.get("height")))
    fps = _parse_frame_rate(video.get("r_frame_rate"))
    # The stream's own duration first, the container's as the fallback: a remuxed file often carries
    # one and not the other, and neither being present is itself the finding.
    duration = _finite(video.get("duration")) or _finite(container.get("duration"))

    missing = []
    if width <= 0 or height <= 0:
        missing.append("frame size")
    if duration <= 0:
        missing.append("duration")
    if missing:
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            codec=codec, width=width, height=height, fps=fps, duration=duration,
            detail=f"Video stream present but its {' and '.join(missing)} is missing or "
                   f"non-positive.")

    return MediaVerdict(
        path=path, status=MediaStatus.METADATA_OK, probe_seconds=probe_seconds,
        codec=codec, width=width, height=height, fps=fps, duration=duration,
        detail=METADATA_NOT_DECODE_TEXT)


# ---------------------------------------------------------------------------
# the bounded run
# ---------------------------------------------------------------------------

@dataclass
class VerificationRun:
    """The media-health half of one verification click."""

    verdicts: List[MediaVerdict] = field(default_factory=list)
    scheduled: int = 0
    budget_exhausted: bool = False
    elapsed_seconds: float = 0.0
    workers: int = DEFAULT_PROBE_WORKERS

    @property
    def complete(self) -> bool:
        """True only when every source got a real verdict. Drives the PARTIAL banner.

        Deliberately unchanged by R3: ``complete`` is about probe *coverage*, and a source that
        changed mid-click was still probed. Its inconclusiveness is reported by its own status,
        its ``change_window`` and the dedicated report lines — folding it in here would make one
        flag mean two different things.
        """
        return not self.budget_exhausted and not any(
            v.status is MediaStatus.NOT_CHECKED for v in self.verdicts)

    @property
    def changed_before_probe(self) -> List[MediaVerdict]:
        """Sources that changed between the cache phase and their probe. Order preserved."""
        return [v for v in self.verdicts if v.changed_before_probe]

    @property
    def changed_during_probe(self) -> List[MediaVerdict]:
        """Sources that changed while FFprobe was reading them. Order preserved."""
        return [v for v in self.verdicts
                if v.change_window in (ChangeWindow.DURING_PROBE, ChangeWindow.BOTH)]


#: The three inconclusive-change details, keyed by window. Written out rather than assembled from
#: fragments so each sentence can be read as a whole and none of them can accidentally become a
#: clean verdict.
_CHANGE_DETAIL = {
    ChangeWindow.CROSS_PHASE: (
        "The file changed on disk AFTER its Stage-5 cache classification and BEFORE this probe "
        "started. Both verdicts are inconclusive: the cache record was matched against bytes that "
        "are no longer there. Nothing was reclassified, re-analysed or written."),
    ChangeWindow.DURING_PROBE: (
        "The file changed on disk while it was being verified; this result is inconclusive and "
        "its cache classification may describe different bytes."),
    ChangeWindow.BOTH: (
        "The file changed on disk after its Stage-5 cache classification AND again while it was "
        "being probed. Neither the cache verdict nor this probe describes the bytes now on disk. "
        "Nothing was reclassified, re-analysed or written."),
}


def _change_window(cross_phase: bool, during_probe: bool) -> ChangeWindow:
    """Which window(s) a change was seen in. ``BOTH`` is never collapsed to the narrower one."""
    if cross_phase and during_probe:
        return ChangeWindow.BOTH
    if cross_phase:
        return ChangeWindow.CROSS_PHASE
    if during_probe:
        return ChangeWindow.DURING_PROBE
    return ChangeWindow.NONE


def _verify_one(path: str, probe, per_file_timeout: float, clock,
                baseline: Any = NO_BASELINE) -> MediaVerdict:
    """Observe, probe, observe again — and compare against the pre-classification observation.

    Total by construction. **Three** ``(size, mtime_ns)`` observations are compared, not two:

    1. ``baseline`` — taken by the caller once per source immediately *before* the Stage-5 cache
       phase, so this function can see a file that was overwritten between being classified and
       being probed. That window is the R3 correction; it was previously invisible, and a source
       inside it reported ``PREPARED`` + ``METADATA_OK`` for two different versions of the file.
    2. ``before`` — immediately before FFprobe.
    3. ``after`` — immediately after FFprobe.

    Any disagreement yields ``CHANGED_DURING_CHECK`` **whatever the probe said**, including a
    perfectly clean read: the bytes FFprobe described are not the bytes the classification saw, or
    not the bytes on disk now, and a definite verdict about either would be invented.
    ``change_window`` records *which* window, because the cache half of the report needs to know.

    ``NO_BASELINE`` (the default, and what every caller that passes no baselines gets) disables
    comparison 1 and leaves S2-A's probe bracket exactly as it was.
    """
    started = clock()
    before = fingerprint_source(path)
    observed = baseline is not NO_BASELINE
    if before is None:
        # Disappeared, locked or denied. UNREADABLE is the accurate media verdict and is already a
        # problem status, so it cannot be mistaken for a clean read -- but if a baseline existed
        # and was readable, this is also a cross-phase change, and the cache verdict recorded for
        # it is no more conclusive than this one.
        vanished = observed and baseline is not None
        return MediaVerdict(
            path=path, status=MediaStatus.UNREADABLE,
            probe_seconds=float(clock() - started),
            change_window=ChangeWindow.CROSS_PHASE if vanished else ChangeWindow.NONE,
            detail=("Source could not be opened for inspection (missing, locked or denied); it "
                    "WAS readable when the cache phase classified it, so that verdict is "
                    "inconclusive too."
                    if vanished else
                    "Source could not be opened for inspection (missing, locked or denied)."))

    cross_phase = observed and baseline != before

    try:
        outcome = probe(path, timeout=per_file_timeout)
    except BaseException as exc:  # noqa: BLE001 — a worker that raised must not kill the run
        # Our own tooling failed, so we have NO verdict on this file. Reported as not-checked with
        # the cause stated, never as a clean read and never as corruption. The window is still
        # carried: a cross-phase change is a fact about the file, not about the probe.
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED,
            probe_seconds=float(clock() - started),
            change_window=_change_window(cross_phase, False),
            detail=f"Verification worker raised {type(exc).__name__}: {_bounded(exc, 120)}")

    probe_seconds = float(clock() - started)
    if not isinstance(outcome, ProbeOutcome):
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED, probe_seconds=probe_seconds,
            change_window=_change_window(cross_phase, False),
            detail=f"Probe seam returned {type(outcome).__name__}, not a ProbeOutcome.")

    after = fingerprint_source(path)
    window = _change_window(cross_phase, after != before)
    if window is not ChangeWindow.NONE:
        return MediaVerdict(
            path=path, status=MediaStatus.CHANGED_DURING_CHECK, probe_seconds=probe_seconds,
            change_window=window, detail=_CHANGE_DETAIL[window])

    return classify_probe(path, outcome, probe_seconds=probe_seconds)


def verify_media_health(sources: Sequence[str], probe: Callable[..., ProbeOutcome], *,
                        workers: Any = None,
                        per_file_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
                        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                        baselines: Sequence[Any] | None = None,
                        clock: Callable[[], float] = None) -> VerificationRun:
    """Probe every source under a bounded worker pool and a bounded overall budget.

    ``probe`` is injected — the real one is ``ffmpeg_processing.probe_media_metadata``. That is what
    lets the whole policy (ordering, budget, partial reporting, worker-exception containment) be
    tested on a bare interpreter with no FFmpeg present.

    ``baselines`` is the R3 addition: one pre-cache-phase observation per source, **aligned by
    position** with ``sources`` (see :func:`fingerprint_sources`). Supplying it lets each verdict
    see the window between its own cache classification and its own probe. Omitting it is
    S2-A behaviour exactly — the probe bracket only. A sequence of the wrong length degrades the
    unmatched positions to :data:`NO_BASELINE` rather than raising, because a bad caller must not
    be able to fail a read-only verification click.

    Three properties the implementation is shaped around:

    * **Deterministic order.** Results are written into a pre-sized list at each source's own
      index, so the report order is the scan order no matter which probe finishes first. A duplicate
      path therefore stays two independent positions with two independent verdicts, exactly as the
      Stage-5 classifier treats position as the authority.
    * **Bounded in-flight work.** Sources are submitted through a sliding window rather than all at
      once, so a 2,000-source library never has 2,000 queued work items and never more than
      ``workers`` live FFprobe children.
    * **Everything started is reaped.** The budget stops *scheduling*; it never abandons a running
      child. ``ThreadPoolExecutor``'s context manager exit waits, and the loop drains ``pending``
      before returning, so no verdict is reported while a probe it describes is still alive.
    """
    clock = clock or time.perf_counter
    paths = [str(p) for p in sources]
    worker_count = resolve_worker_count(workers)
    run = VerificationRun(workers=worker_count)
    started = clock()
    budget = WorkBudget(budget_seconds, started=started)

    if not paths:
        run.elapsed_seconds = float(clock() - started)
        return run

    verdicts: List[MediaVerdict | None] = [None] * len(paths)
    # Positional, never path-keyed: a duplicate path is two independent entries and must keep two
    # independent baselines. A short or long sequence pads/truncates to NO_BASELINE instead of
    # raising or silently reusing a neighbour's observation.
    observed = list(baselines) if baselines is not None else []
    per_source_baseline = [observed[i] if i < len(observed) else NO_BASELINE
                           for i in range(len(paths))]
    # Two per worker: enough that a worker never idles waiting for the scheduler, small enough that
    # the budget check below is reached promptly instead of after thousands of submissions.
    window = max(1, worker_count * 2)
    remaining = iter(list(enumerate(paths)))
    pending: Dict[Any, int] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        drained = False
        while True:
            while not drained and not run.budget_exhausted and len(pending) < window:
                if budget.exhausted(clock()):
                    run.budget_exhausted = True
                    break
                try:
                    index, path = next(remaining)
                except StopIteration:
                    drained = True
                    break
                pending[executor.submit(
                    _verify_one, path, probe, per_file_timeout, clock,
                    per_source_baseline[index])] = index
                run.scheduled += 1

            if not pending:
                break
            done, _still_running = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    verdicts[index] = future.result()
                except BaseException as exc:  # noqa: BLE001 — _verify_one is total, but never trust that
                    verdicts[index] = MediaVerdict(
                        path=paths[index], status=MediaStatus.NOT_CHECKED,
                        detail=f"Verification worker raised {type(exc).__name__}: "
                               f"{_bounded(exc, 120)}")

    # Anything the budget stopped us reaching is explicitly not-checked. A skipped probe never
    # becomes a successful one, and the run is reported PARTIAL because of these entries.
    run.verdicts = [
        v if v is not None else MediaVerdict(
            path=paths[i], status=MediaStatus.NOT_CHECKED,
            detail="Not checked: the verification work budget was exhausted before this source.")
        for i, v in enumerate(verdicts)
    ]
    run.elapsed_seconds = float(clock() - started)
    return run


# ---------------------------------------------------------------------------
# aggregation and the operator report
# ---------------------------------------------------------------------------

def aggregate_media(verdicts: Sequence[MediaVerdict]) -> Dict[str, int]:
    """Count verdicts by status. Keys are the :class:`MediaStatus` values."""
    counts = {status.value: 0 for status in MediaStatus}
    for verdict in verdicts:
        counts[verdict.status.value] = counts.get(verdict.status.value, 0) + 1
    return counts


def _detail_block(title: str, rows: Sequence[Tuple[str, str]],
                  limit: int = MAX_REPORT_DETAIL_LINES) -> List[str]:
    """Up to ``limit`` ``path -- reason`` lines, then an explicit count of what was omitted.

    A displayed row's path is never shortened: the path IS the identification the operator needs in
    order to act, and S1's MAJOR-1 was precisely a diagnostic that truncated away its own subject.
    The bound is on the NUMBER of rows, and the remainder is stated rather than dropped.
    """
    if not rows:
        return []
    lines = [f"  {title} ({len(rows)}):"]
    for path, reason in rows[:limit]:
        lines.append(f"    - {path}")
        if reason:
            lines.append(f"        {reason}")
    omitted = len(rows) - limit
    if omitted > 0:
        lines.append(f"    ... and {omitted} more not shown "
                     f"(showing the first {limit} of {len(rows)}).")
    return lines


def _all_sources_prepared(cache: Dict[str, Any],
                          classifications: Sequence[Dict[str, str]],
                          source_count: int) -> bool:
    """[FORK] Digital-Union (MEDIA-VERIFY-S2-A R2): may the report claim the whole library is warm?

    Affirmative evidence only, and every clause is load-bearing:

    * ``source_count > 0`` — an empty library has no sources to be prepared, so the claim is
      vacuous at best. Before R2 a valid folder containing no supported video printed the positive
      sentence, because three empty detail lists look exactly like three satisfied ones.
    * the three counts must **agree** — the folder's own count, the classifier's ``source_count``
      and the number of per-item classifications. A partial or misaligned list describes a subset,
      and a library-wide claim read off a subset is a statistic nobody computed. This mirrors
      ``library_prep.PrepScanResult.is_fully_prepared``, which refuses the same extrapolation.
    * every item's status must *be* ``PREPARED`` — the per-item check the aggregate cannot replace.
    * ``prepared_count`` must equal the item count, and ``needs_analysis_count`` and
      ``unavailable_count`` must both be zero — so self-contradictory totals (``prepared_count``
      and ``needs_analysis_count`` both nonzero over the same sources) can never be read as
      agreement. MINOR-3 is deliberately deferred: those totals are still printed **verbatim**,
      exactly as the classifier produced them, because silently reconciling them would publish a
      classification nobody ran. What R2 fixes is only that they can no longer *earn a positive
      claim*.

    Total by construction: unknown or malformed input makes this ``False``, never an exception and
    never a claim. It reads the classifier's output and recomputes no verdict of its own.
    """
    items = [item or {} for item in classifications]
    if not items or source_count <= 0:
        return False
    try:
        classified = int(cache.get("source_count") or 0)
        prepared = int(cache.get("prepared_count") or 0)
        needs = int(cache.get("needs_analysis_count") or 0)
        unavailable = int(cache.get("unavailable_count") or 0)
    except (TypeError, ValueError):
        return False
    if not (len(items) == source_count == classified):
        return False
    if prepared != len(items) or needs or unavailable:
        return False
    return all(str(item.get("status") or "") == PrepStatus.PREPARED.value for item in items)


def _readable_status(value: str) -> str:
    """[FORK] Digital-Union (MEDIA-VERIFY-S2-A R2, MINOR-2): display form of a classifier status.

    A recognised :class:`~beatsync_fork.library_prep.PrepStatus` value renders as its member name
    (``prepared`` → ``PREPARED``), which is the spelling the operator reads everywhere else in this
    section. An **unrecognised** value is returned unchanged and therefore stays visibly unknown:
    it is never mapped onto a recognised cache state, because inventing a verdict for a string
    nobody recognises is the fail-open direction. Purely presentational — it touches no
    classification value and no persisted record.
    """
    try:
        return PrepStatus(value).name
    except ValueError:
        return value


def _same_path(a: str, b: str) -> bool:
    """Case-insensitive, absolute path equality. Never raises."""
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except (OSError, ValueError, TypeError):
        return False


def _cross_phase_cache_rows(run: "VerificationRun | None",
                            classifications: Sequence[Dict[str, str]]) -> List[Tuple[str, str]]:
    """``(path, reason)`` for each source whose CACHE verdict the probe phase outdated.

    The classifier's own list and the media verdicts are both built from one ``sources`` list, so
    they align **by position** — and position, not path, is the authority, because a library may
    legitimately contain the same path twice. The recorded path is still compared as a guard: if
    the two lists ever stop aligning, this says so instead of attributing one source's
    classification to another.
    """
    if run is None:
        return []
    items = list(classifications or ())
    rows: List[Tuple[str, str]] = []
    for index, verdict in enumerate(run.verdicts):
        if not verdict.changed_before_probe:
            continue
        item = items[index] if index < len(items) else {}
        recorded_path = str((item or {}).get("path") or "")
        recorded_status = str((item or {}).get("status") or "")
        if not recorded_status or (recorded_path and not _same_path(recorded_path, verdict.path)):
            rows.append((verdict.path,
                         "The file changed on disk before it was probed; its cache classification "
                         "could not be matched by position and is treated as inconclusive."))
            continue
        rows.append((verdict.path,
                     f"Classified as {_readable_status(recorded_status)}, then the file changed "
                     f"on disk before it was probed — that verdict may describe different bytes."))
    return rows


def format_verification_report(*, folder: str, recursive: bool, verified_at: str,
                               source_count: int,
                               cache: Dict[str, Any] | None,
                               cache_classifications: Sequence[Dict[str, str]] = (),
                               run: VerificationRun | None = None,
                               folder_scan_seconds: float = 0.0,
                               cache_seconds: float = 0.0,
                               cache_unverifiable_reason: str = "") -> str:
    """The operator's read-only report: two independent sections, one snapshot header.

    The header names the folder, the recursive setting and the verification time so a report can
    never be mistaken for a different library snapshot — the owner has more than one source folder
    and a stale textbox that looks current is a wrong answer, not a cosmetic issue.
    """
    lines: List[str] = []
    lines.append("SOURCE VERIFICATION (read-only)")
    lines.append(f"  Folder: {folder or '(none)'}")
    lines.append(f"  Include subfolders: {'yes' if recursive else 'no'}")
    lines.append(f"  Verified at: {verified_at}")
    lines.append(f"  Supported source files found: {source_count}"
                 f"  [folder scan {folder_scan_seconds:.2f}s]")
    lines.append("")

    # ---- cache ------------------------------------------------------------
    lines.append("CACHE VERIFICATION")
    if cache_unverifiable_reason:
        # An unprovable Qwen backend identity is NOT evidence of a damaged cache. No per-source
        # verdict can exist in this state, so none is invented.
        lines.append("  UNVERIFIABLE — the Stage-5 cache question cannot be answered right now.")
        lines.append(f"    {cache_unverifiable_reason}")
        lines.append("    This says nothing about whether the existing cache is good; it means the "
                     "identity needed to look it up could not be established.")
    elif cache is None:
        lines.append("  UNVERIFIABLE — no classification was produced.")
    else:
        prepared = int(cache.get("prepared_count") or 0)
        needs = int(cache.get("needs_analysis_count") or 0)
        unavailable = int(cache.get("unavailable_count") or 0)
        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R2): compared against the producer's enum VALUES.
        # See the module docstring -- these were member names until R2 and matched nothing.
        new_or_changed = sum(1 for item in cache_classifications
                             if (item or {}).get("reason") == NeedReason.NEW_OR_CHANGED.value)
        incomplete = sum(1 for item in cache_classifications
                         if (item or {}).get("reason")
                         == NeedReason.INCOMPLETE_OR_INVALID.value)
        lines.append(f"  Sources classified:            {int(cache.get('source_count') or 0)}")
        lines.append(f"  Reusable cache (PREPARED):     {prepared}")
        lines.append(f"  New or changed:                {new_or_changed}")
        lines.append(f"  Incomplete / invalid record:   {incomplete}")
        lines.append(f"  Source identity unavailable:   {unavailable}")
        if needs != new_or_changed + incomplete:
            lines.append(f"  (needs analysis total: {needs})")
        lines.append(f"  Cache verification time:       {cache_seconds:.2f}s")

        changed_rows = [(str(i.get("path") or ""),
                         "No reusable record under the current identity — new, or overwritten "
                         "since it was analysed.")
                        for i in cache_classifications
                        if (i or {}).get("reason") == NeedReason.NEW_OR_CHANGED.value]
        incomplete_rows = [(str(i.get("path") or ""),
                            "A record exists but is deferred, wrong-contract or AI-inconsistent.")
                           for i in cache_classifications
                           if (i or {}).get("reason")
                           == NeedReason.INCOMPLETE_OR_INVALID.value]
        unavailable_rows = [(str(i.get("path") or ""),
                             "Source identity could not be computed (file missing or unreadable).")
                            for i in cache_classifications
                            if (i or {}).get("status")
                            == PrepStatus.SOURCE_IDENTITY_UNAVAILABLE.value]
        lines.extend(_detail_block("Needs analysis - new or changed", changed_rows))
        lines.extend(_detail_block("Needs analysis - incomplete record", incomplete_rows))
        lines.extend(_detail_block("Identity unavailable", unavailable_rows))

        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): sources the probe phase found had changed
        # since classification. The classifier's counts above are its REAL output and are never
        # silently adjusted -- doing that would publish a new classification nobody computed. What
        # is added is the boundary between "what was classified" and "what is on disk now".
        cross_rows = _cross_phase_cache_rows(run, cache_classifications)
        lines.extend(_detail_block(
            "Changed after classification - cache verdict INCONCLUSIVE", cross_rows))
        if cross_rows:
            lines.append(f"  The counts above are the classifier's own output, describing each "
                         f"source as it was WHEN CLASSIFIED. For the {len(cross_rows)} source(s) "
                         f"listed immediately above, the file changed afterwards, so whether "
                         f"those records still apply is INCONCLUSIVE. Nothing was reclassified, "
                         f"re-analysed, repaired or written.")

        # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R2): the positive claim requires AFFIRMATIVE
        # evidence that every classified source was PREPARED. Until R2 it rested on the three
        # detail lists being empty, which is not the same question: an empty list is also what an
        # unrecognised vocabulary, a missing key and a zero-source library produce, and none of
        # those is evidence of reuse. `_all_sources_prepared` is a total predicate over the
        # classifier's own output -- it recomputes no verdict and adjusts no count.
        if _all_sources_prepared(cache, cache_classifications, source_count):
            if cross_rows:
                lines.append("  Every source had a reusable Stage-5 cache record when it was "
                             "classified, but the inconclusive source(s) above changed since.")
            else:
                lines.append("  Every source has a reusable Stage-5 cache record.")
    lines.append("")

    # ---- media health -----------------------------------------------------
    lines.append("MEDIA HEALTH")
    lines.append(f"  {METADATA_NOT_DECODE_TEXT}")
    if run is None:
        lines.append("  Not run.")
        return "\n".join(lines)
    lines.append(f"  {CHANGE_DETECTION_LIMIT_TEXT}")

    counts = aggregate_media(run.verdicts)
    lines.append(f"  Metadata verified:             {counts[MediaStatus.METADATA_OK.value]}")
    lines.append(f"  Unreadable:                    {counts[MediaStatus.UNREADABLE.value]}")
    lines.append(f"  No video stream:               {counts[MediaStatus.NO_VIDEO_STREAM.value]}")
    lines.append(f"  Invalid metadata:              {counts[MediaStatus.INVALID_METADATA.value]}")
    lines.append(f"  Probe timeout (unverified):    {counts[MediaStatus.PROBE_TIMEOUT.value]}")
    lines.append(f"  Changed during verification:   "
                 f"{counts[MediaStatus.CHANGED_DURING_CHECK.value]}")
    lines.append(f"  Not checked:                   {counts[MediaStatus.NOT_CHECKED.value]}")
    lines.append(f"  Media verification time:       {run.elapsed_seconds:.2f}s "
                 f"({run.workers} probe worker(s))")

    if not run.complete:
        lines.append("  STATUS: PARTIAL / INCOMPLETE — not every source was verified. "
                     "Unchecked sources have NO health verdict.")
    else:
        lines.append("  STATUS: COMPLETE — every source was probed (metadata only).")

    # [FORK] Digital-Union (MEDIA-VERIFY-S2-A R3): a COMPLETE probe sweep is not the same claim as
    # "every source is fine as it stands now". A source that moved under us has no current verdict
    # on either side, and that has to sit next to the banner, not three blocks below it.
    cross_phase_count = len(run.changed_before_probe)
    during_probe_count = len(run.changed_during_probe)
    if cross_phase_count or during_probe_count:
        lines.append(f"  INCONCLUSIVE: {cross_phase_count} source(s) changed between the cache "
                     f"phase and their probe; {during_probe_count} changed while being probed. "
                     f"Those sources have NO current media verdict, and their cache verdict is "
                     f"inconclusive for the same reason.")

    for status in _PROBLEM_STATUSES:
        rows = [(v.path, v.detail) for v in run.verdicts if v.status is status]
        lines.extend(_detail_block(status.value, rows))
    if not any(v.is_problem for v in run.verdicts):
        lines.append("  No metadata problems found in any source.")

    return "\n".join(lines)
