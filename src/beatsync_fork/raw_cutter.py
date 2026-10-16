"""Raw Cutter V1: the pure segmentation planner.

[FORK] Digital-Union. Split ONE long source video into smaller physical files at its existing
edits, so footage can be curated before it enters the media library.

**This module decides; it never acts.** No FFmpeg, no subprocess, no filesystem, no clock, no
randomness — per CLAUDE.md's hard rule it imports nothing from the upstream runtime, which is what
makes the whole segmentation policy testable on a bare interpreter. The execution adapter is
``src/raw_cutter_export.py`` and the wiring is ``gui.py``.

Why a separate planner rather than reusing Stage 5's windowing
--------------------------------------------------------------
``video_analysis._make_candidate_windows`` builds ~5.2-second *candidate windows* for scoring —
an internal sampling grid that never becomes a file. The Raw Cutter's output is physical video, so
its policy is the opposite: cover the source **exactly once**, honour human-scale minimum and
maximum lengths, and prefer the editor's own cuts. Sharing that code would have forced one of the
two policies to bend. The *detector* is shared (the FFmpeg scene score); only the segmentation is
new.

The four invariants, enforced in :func:`plan_segments` and asserted by its own tests:

1. **Complete coverage** — the segments tile ``[0, duration]`` with no gap and no overlap.
2. **No zero-length segment** ever reaches the plan.
3. **Bounds are honoured whenever arithmetic allows**, and when they cannot be, the plan *says so*
   rather than discarding footage. A 3-second source with a 5-second minimum yields one 3-second
   segment flagged ``below_minimum`` — never an empty plan.
4. **Deterministic** — identical inputs and settings give an identical plan, every time.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------------------------
# Sensitivity -> FFmpeg scene-score threshold
# ---------------------------------------------------------------------------------------------

SENSITIVITY_MIN = 0
SENSITIVITY_MAX = 100
SENSITIVITY_DEFAULT = 50

#: The anchor points of the mapping. FFmpeg's `scene` score is a 0..1 *difference* measure, so a
#: LOWER threshold detects MORE cuts -- the mapping is deliberately decreasing, because the slider
#: is phrased as sensitivity and a control that reads backwards is a usability bug.
#:
#: The midpoint is pinned to the detector's own default (`detect_video_scene_changes` threshold
#: 0.28, Stage 5 calls it at ~0.27), so sensitivity 50 reproduces the behaviour the rest of the
#: application already relies on instead of inventing a third number.
THRESHOLD_AT_MIN_SENSITIVITY = 0.60
THRESHOLD_AT_DEFAULT_SENSITIVITY = 0.27
THRESHOLD_AT_MAX_SENSITIVITY = 0.05

#: Below this the scene filter reports essentially every frame, which is not a creative cut.
THRESHOLD_FLOOR = 0.01

MIN_CLIP_SECONDS_DEFAULT = 5.0
MAX_CLIP_SECONDS_DEFAULT = 300.0

#: Segment arithmetic is rounded to whole milliseconds before the invariants are checked. Without
#: it, binary float accumulation makes "no gaps" a statement about 1e-16 rather than about video.
TIME_QUANTUM = 0.001


def _finite(value: Any) -> float | None:
    """A real finite float, or ``None``. ``bool`` is rejected: it is not a user-entered number."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _quantize(value: float) -> float:
    return round(round(value / TIME_QUANTUM) * TIME_QUANTUM, 3)


def normalize_sensitivity(value: Any) -> int:
    """Any widget value to ``0..100``. Total — junk reads as the default, never as an exception."""
    number = _finite(value)
    if number is None:
        return SENSITIVITY_DEFAULT
    return max(SENSITIVITY_MIN, min(SENSITIVITY_MAX, int(round(number))))


def sensitivity_to_threshold(value: Any) -> float:
    """Map the 0..100 slider onto the FFmpeg scene-score threshold.

    Piecewise-linear through the three anchors above, so the default position lands exactly on the
    detector's established threshold rather than near it. Strictly decreasing, so "more
    sensitivity" always means "more candidate cuts" -- a property its test checks across the whole
    range rather than at the anchors only.
    """
    sensitivity = normalize_sensitivity(value)
    if sensitivity <= SENSITIVITY_DEFAULT:
        span = SENSITIVITY_DEFAULT - SENSITIVITY_MIN
        ratio = (sensitivity - SENSITIVITY_MIN) / span if span else 0.0
        threshold = (THRESHOLD_AT_MIN_SENSITIVITY
                     + ratio * (THRESHOLD_AT_DEFAULT_SENSITIVITY - THRESHOLD_AT_MIN_SENSITIVITY))
    else:
        span = SENSITIVITY_MAX - SENSITIVITY_DEFAULT
        ratio = (sensitivity - SENSITIVITY_DEFAULT) / span if span else 0.0
        threshold = (THRESHOLD_AT_DEFAULT_SENSITIVITY
                     + ratio * (THRESHOLD_AT_MAX_SENSITIVITY - THRESHOLD_AT_DEFAULT_SENSITIVITY))
    return round(max(THRESHOLD_FLOOR, threshold), 4)


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------

class RawCutterError(ValueError):
    """A request the planner refuses. Carries text meant for the operator."""


def normalize_durations(minimum: Any, maximum: Any) -> tuple[float, float]:
    """Validate the two length bounds together, because their relationship is the contract.

    Raises :class:`RawCutterError` rather than clamping: silently repairing ``max < min`` would
    produce a plan the operator did not ask for and could not predict.
    """
    low = _finite(minimum)
    high = _finite(maximum)
    if low is None or low <= 0:
        raise RawCutterError("Minimum clip duration must be a positive number of seconds.")
    if high is None or high <= 0:
        raise RawCutterError("Maximum clip duration must be a positive number of seconds.")
    if high < low:
        raise RawCutterError(
            f"Maximum clip duration ({high:g}s) must be greater than or equal to the "
            f"minimum ({low:g}s).")
    return _quantize(low), _quantize(high)


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

SCENE_CUT = "scene"
TIME_CUT = "time"
SOURCE_END = "source-end"


@dataclass(frozen=True)
class SourceInfo:
    """What the probe learned about the input. Pure data — the adapter fills it in."""

    path: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size_bytes: int = 0
    mtime_ns: int = 0

    @property
    def basename(self) -> str:
        return os.path.basename(self.path) if self.path else ""

    @property
    def stem(self) -> str:
        return os.path.splitext(self.basename)[0]

    def describe(self) -> str:
        if not self.path:
            return "No source probed yet."
        geometry = f"{self.width}x{self.height}" if self.width and self.height else "unknown size"
        rate = f"{self.fps:.3f} fps".rstrip("0").rstrip(".") if self.fps else "unknown frame rate"
        audio = "has audio" if self.has_audio else "no audio stream"
        return (f"{self.basename}  •  {format_timecode(self.duration)}  •  {geometry}  •  "
                f"{rate}  •  {audio}")


@dataclass(frozen=True)
class Segment:
    """One physical output clip. ``end`` is exclusive, as every interval in this project is."""

    index: int
    start: float
    end: float
    boundary_kind: str = SCENE_CUT
    below_minimum: bool = False

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class SegmentPlan:
    """The complete, validated plan. Produced only by :func:`plan_segments`."""

    segments: tuple[Segment, ...] = ()
    source_duration: float = 0.0
    sensitivity: int = SENSITIVITY_DEFAULT
    threshold: float = THRESHOLD_AT_DEFAULT_SENSITIVITY
    min_seconds: float = MIN_CLIP_SECONDS_DEFAULT
    max_seconds: float = MAX_CLIP_SECONDS_DEFAULT
    detected_scene_count: int = 0
    notes: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.segments)

    @property
    def scene_cuts(self) -> int:
        return sum(1 for s in self.segments if s.boundary_kind == SCENE_CUT)

    @property
    def time_cuts(self) -> int:
        return sum(1 for s in self.segments if s.boundary_kind == TIME_CUT)

    @property
    def short_segments(self) -> tuple[Segment, ...]:
        return tuple(s for s in self.segments if s.below_minimum)

    @property
    def covered_duration(self) -> float:
        return round(sum(s.duration for s in self.segments), 3)


# ---------------------------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------------------------

def _usable_scene_times(scene_times: Iterable[Any], duration: float) -> list[float]:
    """Sorted, de-duplicated interior cut points. Anything outside ``(0, duration)`` is dropped."""
    collected: set[float] = set()
    for value in scene_times or ():
        number = _finite(value)
        if number is None:
            continue
        moment = _quantize(number)
        if 0.0 < moment < duration:
            collected.add(moment)
    return sorted(collected)


def _merge_short(boundaries: Sequence[float], duration: float,
                 min_seconds: float) -> list[tuple[float, float, str]]:
    """Walk the cut points, opening a segment and closing it once it is long enough.

    This is the "combine very short adjacent scenes" rule, and it is a forward accumulation rather
    than a filter: a rapid-fire sequence of half-second cuts becomes ONE segment spanning all of
    them, so nothing is discarded for being short.
    """
    spans: list[tuple[float, float, str]] = []
    start = 0.0
    for moment in boundaries:
        if moment - start >= min_seconds:
            spans.append((start, moment, SCENE_CUT))
            start = moment
    if duration - start > 0:
        spans.append((start, duration, SOURCE_END))
    return spans


def _choose_split(start: float, scene_times: Sequence[float], target: float,
                  earliest: float, latest: float) -> tuple[float, str]:
    """Pick one cut inside ``[earliest, latest]``, preferring a real scene change.

    The window is what keeps both bounds intact: every candidate inside it produces a piece that is
    at least the minimum and at most the maximum. Among those, the one nearest the even-split
    target wins, so pieces stay as regular as the footage allows. Ties break toward the EARLIER
    boundary, which is what makes the result deterministic.
    """
    best: float | None = None
    best_distance = 0.0
    for moment in scene_times:
        if moment < earliest or moment > latest:
            continue
        distance = abs(moment - target)
        if best is None or distance < best_distance - 1e-9:
            best, best_distance = moment, distance
    if best is not None:
        return best, SCENE_CUT
    return _quantize(min(latest, max(earliest, target))), TIME_CUT


def _split_long(span: tuple[float, float, str], scene_times: Sequence[float],
                min_seconds: float, max_seconds: float) -> list[tuple[float, float, str]]:
    """Break one over-long span into pieces of at most ``max_seconds``."""
    start, end, kind = span
    length = end - start
    if length <= max_seconds + 1e-9:
        return [span]

    pieces: list[tuple[float, float, str]] = []
    cursor = start
    while end - cursor > max_seconds + 1e-9:
        remaining = end - cursor
        # How many pieces are still needed, and therefore where an even cut would fall.
        needed = max(2, int(math.ceil(remaining / max_seconds)))
        target = _quantize(cursor + remaining / needed)
        # Leave at least `min_seconds` behind for the tail when that is arithmetically possible.
        latest = min(cursor + max_seconds, end - min_seconds)
        if latest < cursor + min_seconds:
            latest = cursor + max_seconds
        earliest = cursor + min_seconds
        if earliest > latest:
            earliest = latest
        cut, cut_kind = _choose_split(cursor, scene_times, target, _quantize(earliest),
                                      _quantize(latest))
        if cut <= cursor or cut >= end:
            break
        pieces.append((cursor, cut, cut_kind))
        cursor = cut
    if end - cursor > 0:
        pieces.append((cursor, end, kind))
    return pieces


def plan_segments(duration: Any, scene_times: Iterable[Any] = (),
                  min_seconds: Any = MIN_CLIP_SECONDS_DEFAULT,
                  max_seconds: Any = MAX_CLIP_SECONDS_DEFAULT,
                  sensitivity: Any = SENSITIVITY_DEFAULT) -> SegmentPlan:
    """Turn a duration plus detected scene changes into a complete, validated segment plan.

    Three passes, in this order and for this reason:

    1. **Merge** short adjacent scenes forward until each span reaches the minimum.
    2. **Split** any span longer than the maximum, preferring an interior scene change.
    3. **Settle the tail** — a final span below the minimum is folded back into its predecessor
       when that does not break the maximum, and otherwise kept and flagged.

    Raises :class:`RawCutterError` for an unusable duration or contradictory bounds. Everything
    else is absorbed: unusable scene times are ignored rather than fatal, because the detector is
    an estimate and a malformed timestamp must not cost the operator their plan.
    """
    total = _finite(duration)
    if total is None or total <= 0:
        raise RawCutterError("The source duration could not be determined, so it cannot be cut.")
    total = _quantize(total)
    low, high = normalize_durations(min_seconds, max_seconds)
    level = normalize_sensitivity(sensitivity)

    usable = _usable_scene_times(scene_times, total)
    notes: list[str] = []

    spans = _merge_short(usable, total, low)
    if not spans:
        spans = [(0.0, total, SOURCE_END)]

    expanded: list[tuple[float, float, str]] = []
    for span in spans:
        expanded.extend(_split_long(span, usable, low, high))

    # -- settle the tail ------------------------------------------------------------------------
    if len(expanded) >= 2:
        last_start, last_end, _ = expanded[-1]
        if last_end - last_start < low - 1e-9:
            prev_start, _prev_end, prev_kind = expanded[-2]
            if last_end - prev_start <= high + 1e-9:
                expanded[-2:] = [(prev_start, last_end, prev_kind)]

    segments: list[Segment] = []
    for index, (start, end, kind) in enumerate(expanded, start=1):
        start_q, end_q = _quantize(start), _quantize(end)
        if end_q - start_q <= 0:
            continue
        segments.append(Segment(
            index=len(segments) + 1,
            start=start_q,
            end=end_q,
            boundary_kind=kind,
            below_minimum=(end_q - start_q) < low - 1e-9,
        ))

    if not segments:
        raise RawCutterError("No usable segment could be planned for this source.")

    short = [s for s in segments if s.below_minimum]
    if short:
        notes.append(
            f"{len(short)} segment(s) are shorter than the {low:g}s minimum because the footage "
            f"left no longer alternative. They are kept, not discarded.")
    over = [s for s in segments if s.duration > high + 1e-9]
    if over:
        notes.append(f"{len(over)} segment(s) exceed the {high:g}s maximum.")
    if not usable:
        notes.append("No scene changes were used, so the source was divided by duration alone.")

    _assert_invariants(segments, total)

    return SegmentPlan(
        segments=tuple(segments),
        source_duration=total,
        sensitivity=level,
        threshold=sensitivity_to_threshold(level),
        min_seconds=low,
        max_seconds=high,
        detected_scene_count=len(usable),
        notes=tuple(notes),
    )


def _assert_invariants(segments: Sequence[Segment], duration: float) -> None:
    """The four guarantees, checked on every plan rather than trusted.

    This is cheap (one pass over at most a few thousand segments) and it runs in production, not
    only under test: a planner bug that silently dropped footage would otherwise be discovered as a
    missing clip after an hour of encoding.
    """
    if not segments:
        raise RawCutterError("The plan is empty.")
    if segments[0].start != 0.0:
        raise RawCutterError(f"The plan does not start at 0 (starts at {segments[0].start}).")
    if abs(segments[-1].end - duration) > TIME_QUANTUM:
        raise RawCutterError(
            f"The plan ends at {segments[-1].end}, not at the source duration {duration}.")
    for previous, following in zip(segments, segments[1:]):
        if abs(following.start - previous.end) > TIME_QUANTUM:
            raise RawCutterError(
                f"Segment {following.index} starts at {following.start} but the previous one ends "
                f"at {previous.end} — the plan has a gap or an overlap.")
    for segment in segments:
        if segment.duration <= 0:
            raise RawCutterError(f"Segment {segment.index} has no duration.")


# ---------------------------------------------------------------------------------------------
# Naming, staleness and reporting
# ---------------------------------------------------------------------------------------------

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(name: Any, fallback: str = "source") -> str:
    """A filesystem-safe stem. Never empty, never a reserved-looking dot-run, bounded in length."""
    text = str(name or "").strip()
    text = os.path.splitext(os.path.basename(text))[0]
    text = _UNSAFE.sub("_", text).strip("._")
    return (text or fallback)[:60]


def run_directory_name(source_path: Any, timestamp: Any) -> str:
    """``<stem>_raw_parts_<timestamp>`` — the fresh per-run folder.

    The timestamp is an ARGUMENT, not read from a clock here: this module stays pure, and the
    caller already knows when the run started. Uniqueness is the caller's to enforce by refusing
    an occupied directory; this only proposes a name.
    """
    return f"{safe_stem(source_path)}_raw_parts_{str(timestamp).strip() or 'run'}"


def segment_filename(source_path: Any, index: Any, total: Any, extension: str = ".mp4") -> str:
    """``<stem>_part_001.mp4``, zero-padded to the real total so files sort correctly."""
    try:
        count = max(1, int(total))
    except (TypeError, ValueError):
        count = 1
    try:
        position = max(1, int(index))
    except (TypeError, ValueError):
        position = 1
    width = max(3, len(str(count)))
    return f"{safe_stem(source_path)}_part_{position:0{width}d}{extension}"


def plan_signature(source: SourceInfo, sensitivity: Any, min_seconds: Any,
                   max_seconds: Any) -> str:
    """Identity of the inputs a plan depends on, for refusing a stale one.

    Deliberately covers the source's path, size and mtime plus the three PLANNING controls — and
    deliberately **not** Remove Audio or the encoder, which change how a segment is exported but
    not where it begins or ends. Including them would refuse a plan that is still perfectly valid
    and force a needless re-analysis of a long video.
    """
    try:
        low, high = normalize_durations(min_seconds, max_seconds)
    except RawCutterError:
        low, high = -1.0, -1.0
    payload = "|".join((
        "raw_cutter_plan_v1",
        os.path.normcase(os.path.abspath(source.path)) if source.path else "",
        str(int(source.size_bytes or 0)),
        str(int(source.mtime_ns or 0)),
        f"{float(source.duration or 0.0):.3f}",
        str(normalize_sensitivity(sensitivity)),
        f"{low:.3f}",
        f"{high:.3f}",
    ))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def format_timecode(seconds: Any) -> str:
    """``H:MM:SS.mmm`` — readable in a table, and unambiguous about sub-second boundaries."""
    value = _finite(seconds)
    if value is None or value < 0:
        return "--:--:--"
    whole = int(value)
    millis = int(round((value - whole) * 1000))
    if millis == 1000:
        whole, millis = whole + 1, 0
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_size(total_bytes: Any) -> str:
    """Human-readable bytes, picking a unit that keeps the number meaningful.

    A fixed `MB` unit printed "Total written: 0.0 MB" after a run that really did write two
    files -- which reads as *nothing happened* and is the opposite of what the report is for.
    """
    value = _finite(total_bytes)
    if value is None or value < 0:
        return "unknown size"
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if value >= step:
            return f"{value / step:,.1f} {unit}"
    return f"{int(value)} bytes"


def plan_table_rows(plan: SegmentPlan) -> list[list[str]]:
    """Rows for the preview table: one per segment, in order."""
    rows: list[list[str]] = []
    for segment in plan.segments:
        kind = {SCENE_CUT: "scene", TIME_CUT: "time", SOURCE_END: "source end"}.get(
            segment.boundary_kind, segment.boundary_kind)
        rows.append([
            str(segment.index),
            format_timecode(segment.start),
            format_timecode(segment.end),
            f"{segment.duration:.3f}",
            kind + (" • short" if segment.below_minimum else ""),
        ])
    return rows


PLAN_TABLE_HEADERS = ("#", "Start", "End", "Seconds", "Cut")


def plan_summary_text(plan: SegmentPlan, source: SourceInfo) -> str:
    """The operator-facing summary. States what WILL happen, and never more than is known."""
    lines = [
        f"{plan.count} segment(s) planned from {format_timecode(plan.source_duration)} of video.",
        f"Source: {source.describe()}" if source.path else "",
        f"Scene changes detected: {plan.detected_scene_count} "
        f"(sensitivity {plan.sensitivity}, scene threshold {plan.threshold:g})",
        f"Cuts: {plan.scene_cuts} at a detected scene change, {plan.time_cuts} by duration.",
        f"Lengths: {plan.min_seconds:g}s minimum, {plan.max_seconds:g}s maximum.",
        f"Coverage: {format_timecode(plan.covered_duration)} of "
        f"{format_timecode(plan.source_duration)} — the whole source, with no gaps or overlaps.",
    ]
    lines.extend(f"Note: {note}" for note in plan.notes)
    lines.append("")
    lines.append("Nothing has been written yet. Press Cut Video to export these segments.")
    return "\n".join(line for line in lines if line != "")


__all__ = [
    "MAX_CLIP_SECONDS_DEFAULT",
    "MIN_CLIP_SECONDS_DEFAULT",
    "PLAN_TABLE_HEADERS",
    "RawCutterError",
    "SCENE_CUT",
    "SENSITIVITY_DEFAULT",
    "SENSITIVITY_MAX",
    "SENSITIVITY_MIN",
    "SOURCE_END",
    "Segment",
    "SegmentPlan",
    "SourceInfo",
    "TIME_CUT",
    "format_size",
    "format_timecode",
    "normalize_durations",
    "normalize_sensitivity",
    "plan_segments",
    "plan_signature",
    "plan_summary_text",
    "plan_table_rows",
    "run_directory_name",
    "safe_stem",
    "segment_filename",
    "sensitivity_to_threshold",
]
