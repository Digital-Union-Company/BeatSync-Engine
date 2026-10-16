"""Raw Cutter V1: the pure shot-selection planner.

[FORK] Digital-Union. Split ONE long source video into smaller physical files at its existing
edits, so footage can be curated before it enters the media library.

**This module decides; it never acts.** No FFmpeg, no child process, no filesystem, no clock, no
randomness — per CLAUDE.md's hard rule it imports nothing from the upstream runtime, which is what
makes the whole selection policy testable on a bare interpreter. The execution adapter is
``src/raw_cutter_export.py`` and the wiring is ``gui.py``.

Quality first, not coverage first (R1)
--------------------------------------
The first version tiled the whole source: every frame ended up in some clip, and short scenes were
merged forward until they reached the minimum length. **That was the wrong product.** The Raw
Cutter curates footage for later editing; it is not an archive tool. Merging across a real scene
change to reach five seconds produces a clip that cuts in the middle — exactly the thing nobody
wants in a music-video source library — and it does so in the name of saving material that was not
worth keeping.

So the policy is inverted:

* A **shot** is a continuous interval between two confirmed scene changes. Shots are the unit.
* A shot shorter than the minimum is **skipped entirely**. Nothing is merged across a boundary to
  rescue it.
* A shot longer than the maximum is divided **inside itself**. Every piece still comes from one
  continuous take, so the division costs nothing in continuity.
* Gaps between exported clips are **intended output**, not a defect, and the report states how much
  was skipped and why.
* A plan with **zero** usable clips is a legitimate analysis result. It is not an error, and the
  Cut button refuses it rather than writing something unusable.

The invariants that replace "complete coverage":

1. **No exported segment ever spans a confirmed scene change.** Structural: segments are built
   inside one shot and never across two.
2. **Every exported segment satisfies both bounds** — at least the minimum, at most the maximum.
   There is no "flagged short" escape hatch any more; material that cannot satisfy them is skipped.
3. **Segments stay inside the source, are ordered, and never overlap.** Gaps between them are
   allowed and expected.
4. **Deterministic** — identical inputs and settings give an identical plan, every time.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
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
#: it, binary float accumulation makes "no overlap" a statement about 1e-16 rather than about video.
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

    **R1 — a positive bound that quantizes to zero is refused, not accepted.** Times are rounded to
    whole milliseconds, so ``0.0004`` is positive on the way in and ``0.0`` on the way out. The
    original check ran before quantization and let that through, which would have planned
    zero-length clips from a value the operator typed in good faith. Both bounds are therefore
    re-checked *after* rounding, and the message says what happened rather than reporting a plain
    "must be positive" about a number that visibly was.
    """
    low = _finite(minimum)
    high = _finite(maximum)
    if low is None or low <= 0:
        raise RawCutterError("Minimum clip duration must be a positive number of seconds.")
    if high is None or high <= 0:
        raise RawCutterError("Maximum clip duration must be a positive number of seconds.")

    low_q, high_q = _quantize(low), _quantize(high)
    if low_q <= 0:
        raise RawCutterError(
            f"Minimum clip duration ({low:g}s) is smaller than one millisecond, so it rounds to "
            f"zero. Use at least 0.001s.")
    if high_q <= 0:
        raise RawCutterError(
            f"Maximum clip duration ({high:g}s) is smaller than one millisecond, so it rounds to "
            f"zero. Use at least 0.001s.")
    if high_q < low_q:
        raise RawCutterError(
            f"Maximum clip duration ({high:g}s) must be greater than or equal to the "
            f"minimum ({low:g}s).")
    return low_q, high_q


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

#: Why a shot produced no exported clip. Rendered verbatim in the report.
SKIP_TOO_SHORT = "shorter than the minimum"
SKIP_REMAINDER = "leftover tail below the minimum"

#: How a segment's own end was decided.
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
class Shot:
    """One continuous interval between two confirmed scene changes.

    The unit of the whole policy. A shot is either exported (possibly as several pieces, all from
    inside it) or skipped whole — it is never partially merged into a neighbour.
    """

    index: int
    start: float
    end: float
    exported: bool = False
    skip_reason: str = ""
    piece_count: int = 0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class Segment:
    """One physical output clip. ``end`` is exclusive, as every interval in this project is."""

    index: int
    start: float
    end: float
    shot_index: int = 0
    boundary_kind: str = SCENE_CUT

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)


@dataclass(frozen=True)
class SegmentPlan:
    """The complete, validated plan. Produced only by :func:`plan_segments`."""

    segments: tuple[Segment, ...] = ()
    shots: tuple[Shot, ...] = ()
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
    def has_work(self) -> bool:
        """Is there anything to export? A plan of zero clips is valid analysis, not an error."""
        return bool(self.segments)

    @property
    def exported_shots(self) -> tuple[Shot, ...]:
        return tuple(s for s in self.shots if s.exported)

    @property
    def skipped_shots(self) -> tuple[Shot, ...]:
        return tuple(s for s in self.shots if not s.exported)

    @property
    def exported_duration(self) -> float:
        return round(sum(s.duration for s in self.segments), 3)

    @property
    def skipped_duration(self) -> float:
        """What the source contains that no clip will carry.

        Computed as source minus exported rather than by summing skipped shots, so dropped
        remainders are counted too -- the two must agree, and this is the one that cannot drift
        from what is actually written.
        """
        return round(max(0.0, self.source_duration - self.exported_duration), 3)


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


def _divide_shot(start: float, end: float, min_seconds: float,
                 max_seconds: float) -> tuple[list[tuple[float, float]], float]:
    """Split ONE continuous shot into exportable pieces. Returns ``(pieces, dropped_seconds)``.

    There is no scene change inside a shot — that is what makes it a shot — so the division is
    purely arithmetic and costs nothing in continuity.

    An **even** split is preferred when it keeps every piece above the minimum: three 8-second
    clips read better than two 10s and a 4s tail. When an even split would push pieces under the
    minimum, it falls back to filling maximum-length pieces and reports whatever tail is left over
    as dropped, because a sub-minimum tail is exactly the material this tool is supposed to leave
    behind.
    """
    length = _quantize(end - start)
    if length < min_seconds - 1e-9:
        return [], length
    if length <= max_seconds + 1e-9:
        return [(start, end)], 0.0

    pieces = max(2, int(math.ceil(length / max_seconds)))
    if length / pieces >= min_seconds - 1e-9:
        cuts = [_quantize(start + (length * index) / pieces) for index in range(pieces + 1)]
        cuts[0], cuts[-1] = start, end
        return [(cuts[i], cuts[i + 1]) for i in range(pieces)], 0.0

    # An even split would go under the minimum, so fill whole maximum-length pieces instead.
    whole = int(length // max_seconds)
    built = [(_quantize(start + index * max_seconds), _quantize(start + (index + 1) * max_seconds))
             for index in range(whole)]
    tail_start = built[-1][1] if built else start
    tail = _quantize(end - tail_start)
    if tail >= min_seconds - 1e-9:
        built.append((tail_start, end))
        return built, 0.0
    return built, tail


def plan_segments(duration: Any, scene_times: Iterable[Any] = (),
                  min_seconds: Any = MIN_CLIP_SECONDS_DEFAULT,
                  max_seconds: Any = MAX_CLIP_SECONDS_DEFAULT,
                  sensitivity: Any = SENSITIVITY_DEFAULT) -> SegmentPlan:
    """Select the usable shots from a duration plus its detected scene changes.

    Shot by shot, with no cross-boundary arithmetic anywhere: each interval between confirmed
    scene changes is judged on its own and is either divided into exportable pieces or skipped
    whole. That is what makes "no exported clip spans a scene change" structural rather than
    checked afterwards.

    Raises :class:`RawCutterError` for an unusable duration or contradictory bounds. Everything
    else is absorbed: unusable scene times are ignored rather than fatal, because the detector is
    an estimate and a malformed timestamp must not cost the operator their plan.

    A plan with **no** segments is returned normally. It is a truthful answer about footage that
    has nothing worth exporting, and the caller refuses to cut it.
    """
    total = _finite(duration)
    if total is None or total <= 0:
        raise RawCutterError("The source duration could not be determined, so it cannot be cut.")
    total = _quantize(total)
    low, high = normalize_durations(min_seconds, max_seconds)
    level = normalize_sensitivity(sensitivity)

    usable = _usable_scene_times(scene_times, total)
    boundaries = [0.0] + usable + [total]

    shots: list[Shot] = []
    segments: list[Segment] = []
    dropped_tails = 0.0

    for position in range(len(boundaries) - 1):
        start, end = boundaries[position], boundaries[position + 1]
        if end - start <= 0:
            continue
        shot_index = len(shots) + 1
        pieces, dropped = _divide_shot(start, end, low, high)

        if not pieces:
            # A shot skipped whole is NOT a dropped tail. `_divide_shot` reports the entire length
            # as unused in that case, and adding it here made the report describe a skipped shot
            # twice -- once truthfully as a skipped shot, and once as "leftover tail dropped from
            # the end of long shots", which it never was.
            shots.append(Shot(index=shot_index, start=start, end=end, exported=False,
                              skip_reason=SKIP_TOO_SHORT))
            continue

        dropped_tails += dropped

        shots.append(Shot(index=shot_index, start=start, end=end, exported=True,
                          piece_count=len(pieces),
                          skip_reason=SKIP_REMAINDER if dropped > 0 else ""))
        for piece_start, piece_end in pieces:
            at_shot_end = abs(piece_end - end) <= TIME_QUANTUM
            kind = SOURCE_END if (at_shot_end and abs(end - total) <= TIME_QUANTUM) else (
                SCENE_CUT if at_shot_end else TIME_CUT)
            segments.append(Segment(index=len(segments) + 1, start=piece_start, end=piece_end,
                                    shot_index=shot_index, boundary_kind=kind))

    notes = _build_notes(shots, segments, low, high, usable, dropped_tails)
    _assert_invariants(segments, shots, total, low, high, usable)

    return SegmentPlan(
        segments=tuple(segments),
        shots=tuple(shots),
        source_duration=total,
        sensitivity=level,
        threshold=sensitivity_to_threshold(level),
        min_seconds=low,
        max_seconds=high,
        detected_scene_count=len(usable),
        notes=tuple(notes),
    )


def _build_notes(shots: Sequence[Shot], segments: Sequence[Segment], low: float, high: float,
                 usable: Sequence[float], dropped_tails: float) -> list[str]:
    notes: list[str] = []
    if not usable:
        notes.append("No scene changes were detected, so the whole source was treated as one "
                     "continuous shot.")
    skipped = [s for s in shots if not s.exported]
    if skipped:
        notes.append(f"{len(skipped)} shot(s) were skipped for being shorter than the {low:g}s "
                     f"minimum. That is deliberate: a clip is not stretched across a scene change "
                     f"to reach the minimum.")
    if dropped_tails > 0:
        notes.append(f"{format_timecode(dropped_tails)} of leftover tail was dropped from the end "
                     f"of long shots, because what remained was under the {low:g}s minimum.")
    if not segments:
        notes.append("Nothing in this source is long enough to export at these settings. Lower "
                     "the minimum clip length, or lower the sensitivity so fewer scene changes "
                     "are detected.")
    return notes


def _assert_invariants(segments: Sequence[Segment], shots: Sequence[Shot], duration: float,
                       low: float, high: float, scene_times: Sequence[float]) -> None:
    """The four guarantees, checked on every plan rather than trusted.

    Cheap, and it runs in production rather than only under test: a selection bug that exported a
    clip across a scene change would otherwise be discovered as a ruined source clip an hour of
    encoding later. Note what is NOT asserted any more -- complete coverage. Gaps are the point.
    """
    cuts = set(scene_times)
    for previous, following in zip(segments, segments[1:]):
        if following.start < previous.end - TIME_QUANTUM:
            raise RawCutterError(
                f"Segment {following.index} starts at {following.start}, before segment "
                f"{previous.index} ends at {previous.end} — the plan overlaps.")
    for segment in segments:
        if segment.duration <= 0:
            raise RawCutterError(f"Segment {segment.index} has no duration.")
        if segment.start < -TIME_QUANTUM or segment.end > duration + TIME_QUANTUM:
            raise RawCutterError(
                f"Segment {segment.index} ({segment.start}..{segment.end}) leaves the source.")
        if segment.duration < low - 1e-9:
            raise RawCutterError(
                f"Segment {segment.index} is {segment.duration}s, under the {low}s minimum — "
                f"material that cannot satisfy the bounds must be skipped, not exported.")
        if segment.duration > high + 1e-9:
            raise RawCutterError(
                f"Segment {segment.index} is {segment.duration}s, over the {high}s maximum.")
        for cut in cuts:
            if segment.start + TIME_QUANTUM < cut < segment.end - TIME_QUANTUM:
                raise RawCutterError(
                    f"Segment {segment.index} ({segment.start}..{segment.end}) spans the scene "
                    f"change at {cut}.")
    for shot in shots:
        if shot.exported and shot.piece_count <= 0:
            raise RawCutterError(f"Shot {shot.index} is marked exported but produced no clip.")


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


def staging_filename(source_path: Any, index: Any, total: Any, token: Any,
                     extension: str = ".mp4") -> str:
    """The task-owned staging name a clip is written to before it is published.

    Carries a per-attempt token so two runs -- or a retry of the same run -- can never collide on
    it. A predictable ``<name>.part`` is a latent collision in which the second attempt truncates
    the first's in-flight write, which is the same trap the delivery share file had to be fixed
    for.

    **It keeps the real extension, and that is functional rather than cosmetic.** FFmpeg infers
    its muxer from the output extension when no ``-f`` is given, so a name ending in ``.part``
    makes it fail with "Unable to find a suitable output format" before a single frame is written.
    The marker therefore sits *before* the extension: ``.clip_part_001.a1b2c3.part.mp4``. The
    leading dot keeps it out of the way in a file listing.
    """
    safe_token = _UNSAFE.sub("", str(token or ""))[:24] or "run"
    base = segment_filename(source_path, index, total, extension="")
    return f".{base}.{safe_token}.part{extension}"


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


PLAN_TABLE_HEADERS = ("Shot", "Start", "End", "Seconds", "Status")


def plan_table_rows(plan: SegmentPlan) -> list[list[str]]:
    """One row per SHOT, in source order, saying plainly whether it will be exported.

    Shots rather than segments, because the skipped ones are the whole point of the new policy and
    a segment table could not show them -- a skipped shot produces no segment. An exported shot
    that was divided says so on its row.
    """
    rows: list[list[str]] = []
    pieces_by_shot: dict[int, list[Segment]] = {}
    for segment in plan.segments:
        pieces_by_shot.setdefault(segment.shot_index, []).append(segment)

    for shot in plan.shots:
        if shot.exported:
            pieces = pieces_by_shot.get(shot.index, ())
            numbers = ", ".join(f"#{p.index}" for p in pieces)
            status = f"export {numbers}" if len(pieces) == 1 else \
                f"export {len(pieces)} parts ({numbers})"
            if shot.skip_reason == SKIP_REMAINDER:
                status += " • tail dropped"
        else:
            status = f"skip — {shot.skip_reason}"
        rows.append([
            str(shot.index),
            format_timecode(shot.start),
            format_timecode(shot.end),
            f"{shot.duration:.3f}",
            status,
        ])
    return rows


def plan_summary_text(plan: SegmentPlan, source: SourceInfo) -> str:
    """The operator-facing summary.

    States what will be exported AND what will not, without presenting the gaps as a failure:
    skipping unusable material is the feature. It never claims the source was fully preserved.
    """
    exported = plan.exported_shots
    skipped = plan.skipped_shots
    lines = [
        f"Detected {len(plan.shots)} shot(s) from {len(plan.shots) - 1} scene change(s) "
        f"in {format_timecode(plan.source_duration)} of video."
        if plan.shots else "No shots detected.",
        f"Source: {source.describe()}" if source.path else "",
        "",
        f"EXPORT   {len(exported)} shot(s) → {plan.count} clip(s), "
        f"{format_timecode(plan.exported_duration)} of footage",
        f"SKIP     {len(skipped)} shot(s), "
        f"{format_timecode(plan.skipped_duration)} of footage not exported",
        "",
        f"Detection: sensitivity {plan.sensitivity}, scene threshold {plan.threshold:g}, "
        f"{plan.detected_scene_count} scene change(s) found.",
        f"Lengths: {plan.min_seconds:g}s minimum, {plan.max_seconds:g}s maximum.",
    ]
    lines.append("")
    lines.append("Skipped footage is intentional — the Raw Cutter keeps clean continuous shots "
                 "and leaves the rest. It is not trying to preserve every frame.")
    lines.extend(f"Note: {note}" for note in plan.notes)
    lines.append("")
    if plan.has_work:
        lines.append("Nothing has been written yet. Press Cut Video to export these clips.")
    else:
        lines.append("There is nothing to export, so Cut Video will decline.")
    return "\n".join(line for line in lines if line != "" or True).strip("\n")


__all__ = [
    "MAX_CLIP_SECONDS_DEFAULT",
    "MIN_CLIP_SECONDS_DEFAULT",
    "PLAN_TABLE_HEADERS",
    "RawCutterError",
    "SCENE_CUT",
    "SENSITIVITY_DEFAULT",
    "SENSITIVITY_MAX",
    "SENSITIVITY_MIN",
    "SKIP_REMAINDER",
    "SKIP_TOO_SHORT",
    "SOURCE_END",
    "Segment",
    "SegmentPlan",
    "Shot",
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
    "staging_filename",
]
