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

Conservative trimming at scene changes (P6)
-------------------------------------------
The same product judgement, pushed one step further by the owner: **prefer simple, predictable,
conservative trimming over expensive per-cut boundary refinement.** A detected scene change is an
*estimate*, and the only way to be sure a clip holds no frame of its neighbour without knowing
exactly where the cut fell is to stand back from it. So every detected scene change gets a
:data:`SCENE_GUARD_SECONDS` margin on **both** sides, and the footage in between is deliberately
discarded.

The margin is applied at detected scene changes and nowhere else — not at the start of the source,
not at its end, and not at a time split inside one continuous shot, because none of those is an
estimate. A 30-second uninterrupted shot cut into three 10-second clips loses nothing at 10 s and
20 s; a shot change detected at 10 s costs 0.4 s around it.

The invariants that replace "complete coverage":

1. **No exported segment comes within the guard of a detected scene change.** Structural: segments
   are built inside one guarded shot and never across two, and :func:`_assert_invariants` measures
   it on every plan. This strictly subsumes the older "never spans a cut" guarantee.
2. **Every exported segment satisfies both bounds** — at least the minimum, at most the maximum.
   There is no "flagged short" escape hatch any more; material that cannot satisfy them is skipped.
3. **Segments stay inside the source, are ordered, and never overlap.** Gaps between them are
   allowed and expected.
4. **Deterministic** — identical inputs and settings give an identical plan, every time.

What this module does **not** claim: that every source frame reaches an output clip. It never did
(a short shot is skipped whole), and under the guard policy it is further from true by design. The
report says how much went where instead of implying nothing was lost.
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

#: [FORK] Digital-Union (frame-boundary P6): the CONSERVATIVE GUARD around a detected scene change,
#: each side, in seconds. Footage inside the guard is intentionally discarded.
#:
#: **Why a margin at all.** The coarse detector resamples to 8 fps before scoring, so the time it
#: reports is a LABEL on the 1/8 s grid rather than the position of the frames that were compared.
#: P3 measured where the real cut can actually sit relative to that label -- by decoding the `fps=8`
#: output and reading each output frame's own source-frame index back out at 23.976/25/29.97/30/60
#: fps -- and found the resampler carries a frame LATER than its own grid time by a quarter to a
#: half of an analysis period. That places the real cut inside
#:
#:     [reported - 0.100 s, reported + 0.063 s]
#:
#: i.e. at worst one analysis period before the label and half a period after it.
#:
#: **Why 0.20.** It clears that measured worst case by 100 ms on the early side and 137 ms on the
#: late side. The margin is the entire mechanism: the guard has to absorb the detector's error
#: *without being told what it was*, so the surplus over the measured bound is what the policy
#: buys, not waste to be optimised away.
#:
#: **Why this succeeds where per-cut refinement could not.** Refinement had to IDENTIFY the cut
#: frame, so a cut whose own inter-frame difference never clears the operator's sensitivity is
#: invisible to it however the candidates are ranked. P5-S1 measured exactly that: a pan->solid cut
#: at 5.066667 reported at 5.125, where the only super-threshold frame in the bracket was
#: pan-internal, so every selection rule returned 5.000 and the exported clip opened with two
#: frames of the outgoing shot. The guard needs only the BOUND, which still held -- 5.066667 lies
#: inside [5.025, 5.188] -- so it puts the clip edges 141 ms and 258 ms clear of a cut it never
#: located. Not knowing where the cut is, is the design.
#:
#: **Why not smaller.** 0.15 s is arithmetically defensible (50 ms / 87 ms of margin) and 0.125 s
#: (one analysis period) leaves 25 ms on the early side -- under one frame interval at 30 fps, and
#: below the measured bound's own uncertainty. Neither is recommended, and the guard must not be
#: reduced to improve frame retention: retention is explicitly not what this policy optimises.
#:
#: Applied at detected scene changes ONLY -- never at the start of the source, never at its end,
#: and never at a time split inside one continuous shot. See :func:`plan_segments`.
SCENE_GUARD_SECONDS = 0.20

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


def _quantize_floor(value: float) -> float:
    """[FORK] Digital-Union (frame-boundary P1): quantize a SCENE CUT **downwards**, never up.

    A scene cut is the presentation time of the first frame of the NEXT shot, and it is used as
    the exclusive end of the previous clip. :func:`_quantize` rounds to the nearest millisecond,
    so a cut at 3.066667 s became 3.067 s -- one millisecond *past* the frame it is supposed to
    stop before. Export bounds the clip with ``-t``, which keeps every frame whose timestamp is
    below the end, so that one millisecond is enough to decode the first frame of the next scene
    into the end of the clip. On a 30 fps source the operator sees a single-frame flash of the
    following shot; at 60 fps, two.

    Flooring is both safe and lossless here, and the margin is not a guess:

    * the last frame that genuinely belongs to this shot sits a whole frame earlier, at
      ``cut - 1/fps``. Flooring moves the boundary by less than one millisecond, so that frame is
      still comfortably inside for any source below 1000 fps -- no footage is discarded;
    * the next shot still starts on its true first frame, because export seeks with ``-ss`` and
      keeps frames at or after the boundary: a boundary up to a millisecond early still lands on
      the same frame.

    So the floor removes the overshoot without moving either clip's real content.

    [FORK] P3 (FIX D): the division needs a representation tolerance. ``value / TIME_QUANTUM`` is
    binary floating point, and for an input that is already an exact number of milliseconds the
    quotient can land just *below* the integer it should be -- ``3.002 / 0.001`` evaluates to
    ``3001.9999999999995`` -- so a plain floor drops a whole millisecond that was never overshoot.
    Measured, that hit 13.34 % of exact-millisecond values between 0 and 600 s.

    On its own that error is harmless: it moves the boundary EARLIER, which is the safe direction,
    and a millisecond is nothing against frame intervals of 8 to 42 ms -- zero leaks and zero lost
    frames across 18,000 real frame timestamps at six frame rates. What it did cost was honesty
    about duration: a shot whose true length is exactly the operator's minimum measured one
    millisecond short and was silently skipped instead of exported.

    ``1e-9`` of a millisecond -- a picosecond of video -- is far below any real timestamp's
    resolution, so it cannot pull a boundary past a frame that genuinely sits after it, but it is
    many orders of magnitude above the ~1e-13 relative error of the division itself. Note the
    tolerance belongs on the QUOTIENT, not the value: adding it to ``value`` would scale with the
    magnitude of the timestamp, while the representation error being corrected does not.

    Deliberately not `Decimal`: ``Decimal(3.002)`` is the float's exact binary value,
    ``3.00199999999999999733...``, which floors to 3.001 just the same -- measured *worse* than the
    plain float route (29,757 of 60,001 exact values wrong against 7,932). Decimal would only help
    applied to the timestamp's decimal *string* before it ever became a float.
    """
    return round(math.floor(value / TIME_QUANTUM + 1e-9) * TIME_QUANTUM, 3)


def _quantize_ceil(value: float) -> float:
    """[FORK] Digital-Union (frame-boundary P6): quantize a GUARDED START **upwards**, never down.

    The mirror of :func:`_quantize_floor`, for the mirrored reason. A guarded start is
    ``cut + SCENE_GUARD_SECONDS`` and means *no earlier than this*; rounding it to the nearest
    millisecond could move it half a millisecond back TOWARDS the cut, which is the one direction
    the guard exists to forbid. Rounding up keeps ``start - cut >= guard`` true as arithmetic
    rather than as a tolerance, which is what lets :func:`_assert_invariants` state the guard as a
    hard property.

    It carries the same representation tolerance :func:`_quantize_floor` does, applied in the
    opposite direction. ``value / TIME_QUANTUM`` is binary floating point, so an input that is
    already an exact number of milliseconds can land a hair *above* the integer it should be, and
    a plain ceiling would then add a whole millisecond that was never needed. Nudging the quotient
    down by ``1e-9`` -- a picosecond of video, far below any real timestamp's resolution and many
    orders of magnitude above the division's own ~1e-13 relative error -- removes that without
    being able to pull the boundary back onto an earlier frame.
    """
    return round(math.ceil(value / TIME_QUANTUM - 1e-9) * TIME_QUANTUM, 3)


def _resolve_guard(value: Any) -> float:
    """The conservative scene guard to apply, in seconds. ``None`` means the shipped policy value.

    **Not an operator control**, and deliberately absent from :func:`plan_signature`: it is a fixed
    product decision, and the parameter exists so a test can measure the policy at other values --
    not so the GUI can offer one. Junk reads as the policy value rather than raising, like every
    other absorbed input here.

    A negative reads as zero. A guard that EXTENDED a clip towards a cut would invert the whole
    point of the policy, so it is refused by clamping rather than honoured.
    """
    if value is None:
        return SCENE_GUARD_SECONDS
    number = _finite(value)
    if number is None:
        return SCENE_GUARD_SECONDS
    return max(0.0, _quantize(number))


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
    """One continuous interval between two confirmed scene changes, **after the guard**.

    The unit of the whole policy. A shot is either exported (possibly as several pieces, all from
    inside it) or skipped whole — it is never partially merged into a neighbour.

    [FORK] P6: ``start``/``end`` are the **usable** interval, i.e. the detected shot with
    :data:`SCENE_GUARD_SECONDS` removed at each end that is a detected cut. That is deliberate and
    it is what makes the record self-consistent: ``duration`` is the length actually judged against
    the operator's minimum, so a row saying *"3.9s — skip, shorter than the minimum"* is a true
    statement about the number beside it. The **detected** shot is still recoverable exactly, as
    ``(start - guard_before, end + guard_after)``.
    """

    index: int
    start: float
    end: float
    exported: bool = False
    skip_reason: str = ""
    piece_count: int = 0
    #: [FORK] P6: seconds the guard removed from this shot's own start and end. Zero at a boundary
    #: that is the start or the end of the SOURCE, because neither is an estimate. When the two
    #: guards meet (a detected shot shorter than the bands around it) the shot collapses to a
    #: point and these two carry its whole detected length between them — the honest reading, since
    #: the guards really did consume all of it.
    #:
    #: New fields are APPENDED: `gui.py` serialises a `Shot` as a fixed six-element list and
    #: rebuilds it by keyword, so the first six must keep their order.
    guard_before: float = 0.0
    guard_after: float = 0.0

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    @property
    def detected_duration(self) -> float:
        """The length of the shot as DETECTED, before the guard took anything off it."""
        return round(self.guard_before + self.duration + self.guard_after, 3)


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
    #: [FORK] P6: the guard actually applied (each side of every detected cut), and the total
    #: footage it intentionally removed. `guard_discarded` is the measure of the discarded regions
    #: and cannot double-count: each shot contributes only the two slivers inside its own detected
    #: interval, and a shot the guards consumed entirely contributes exactly its own length.
    guard_seconds: float = 0.0
    guard_discarded: float = 0.0

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

    @property
    def skipped_shot_duration(self) -> float:
        """Usable footage inside shots that were skipped whole — the QUALITY-FIRST loss.

        [FORK] P6. Guard bands are **excluded**: this counts only what survived the guard and was
        then still too short to export. Reporting the two together would let a guard-band number
        hide inside a skip number, which is exactly the conflation the owner asked to avoid.
        """
        return round(sum(s.duration for s in self.shots if not s.exported), 3)

    @property
    def dropped_tail_duration(self) -> float:
        """What is left over once the guard and the skipped shots are accounted for.

        Derived by subtraction from :attr:`skipped_duration` rather than accumulated separately,
        so the three categories are guaranteed to sum to the total unexported footage instead of
        being three numbers that merely ought to agree.
        """
        return round(max(0.0, self.skipped_duration - self.guard_discarded
                         - self.skipped_shot_duration), 3)


# ---------------------------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------------------------

def _usable_scene_times(scene_times: Iterable[Any], duration: float) -> list[float]:
    """Sorted, de-duplicated interior cut points. Anything outside ``(0, duration)`` is dropped.

    [FORK] Digital-Union (frame-boundary P1): cut points are quantized **downwards**. A cut is
    the first frame of the next shot and doubles as the exclusive end of this one, so rounding it
    up by half a millisecond is what let that frame be decoded into the end of the previous clip.
    See :func:`_quantize_floor`.
    """
    collected: set[float] = set()
    for value in scene_times or ():
        number = _finite(value)
        if number is None:
            continue
        moment = _quantize_floor(number)
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
                  sensitivity: Any = SENSITIVITY_DEFAULT,
                  guard_seconds: Any = None) -> SegmentPlan:
    """Select the usable shots from a duration plus its detected scene changes.

    Shot by shot, with no cross-boundary arithmetic anywhere: each interval between confirmed
    scene changes is judged on its own and is either divided into exportable pieces or skipped
    whole. That is what makes "no exported clip spans a scene change" structural rather than
    checked afterwards.

    **[FORK] Digital-Union (frame-boundary P6): the conservative guard, and exactly where it is
    not applied.** Each detected scene change is treated as an estimate with a known error bound
    (see :data:`SCENE_GUARD_SECONDS`), so the shot before it ends ``guard`` seconds early and the
    shot after it starts ``guard`` seconds late. The footage between those two is discarded on
    purpose and nothing is ever merged across the cut to recover it.

    Three boundaries deliberately get **no** guard, because none of them is an estimate:

    * the **start of the source** — position 0 opens at 0.0 exactly;
    * the **end of the source** — the final shot closes at ``total`` exactly;
    * a **time split inside one shot** — :func:`_divide_shot` divides the already-guarded interval,
      and its pieces tile it with no gap. A 30 s uninterrupted shot cut into three 10 s clips
      loses no frame at 10 s or 20 s, because there is no scene change there to be wrong about.

    The quantizers carry the direction: a guarded start rounds **up** and a guarded end rounds
    **down**, so millisecond rounding can only ever move a boundary further from the cut.

    When two detected cuts are closer together than twice the guard their bands meet, and the shot
    between them has nothing left. That is handled by collapsing it to a point and recording it as
    skipped — never as a negative duration, an overlap, or an invented frame — and never by
    merging its neighbours across either cut.

    ``guard_seconds`` exists for measurement, not for the operator; see :func:`_resolve_guard`.

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
    guard = _resolve_guard(guard_seconds)

    usable = _usable_scene_times(scene_times, total)
    boundaries = [0.0] + usable + [total]
    final = len(boundaries) - 2

    shots: list[Shot] = []
    segments: list[Segment] = []
    dropped_tails = 0.0
    guard_discarded = 0.0

    for position in range(len(boundaries) - 1):
        detected_start, detected_end = boundaries[position], boundaries[position + 1]
        if detected_end - detected_start <= 0:
            continue

        # The guard applies at DETECTED CUTS and nowhere else. Position 0 opens at the start of the
        # source and the last shot closes at its end; neither is a detection, so neither pays.
        start = (_quantize_ceil(detected_start + guard) if position > 0 else detected_start)
        end = (_quantize_floor(detected_end - guard) if position < final else detected_end)

        if end <= start:
            # The bands around the two cuts meet or overlap: this detected shot is shorter than the
            # guards that bracket it, so no part of it can be exported. Collapse it to the midpoint
            # of the DETECTED interval -- deterministic, inside the source, never negative -- and
            # still record it, so the report names a skipped shot rather than quietly losing one.
            middle = min(max(_quantize((detected_start + detected_end) / 2.0), detected_start),
                         detected_end)
            start = end = middle

        before = round(start - detected_start, 3)
        after = round(detected_end - end, 3)
        guard_discarded += before + after

        shot_index = len(shots) + 1
        # A collapsed shot has zero length, so this returns no pieces: the minimum is positive by
        # construction (`normalize_durations`), which is why the degenerate case needs no branch.
        pieces, dropped = _divide_shot(start, end, low, high)

        if not pieces:
            # A shot skipped whole is NOT a dropped tail. `_divide_shot` reports the entire length
            # as unused in that case, and adding it here made the report describe a skipped shot
            # twice -- once truthfully as a skipped shot, and once as "leftover tail dropped from
            # the end of long shots", which it never was.
            shots.append(Shot(index=shot_index, start=start, end=end, exported=False,
                              skip_reason=SKIP_TOO_SHORT,
                              guard_before=before, guard_after=after))
            continue

        dropped_tails += dropped

        shots.append(Shot(index=shot_index, start=start, end=end, exported=True,
                          piece_count=len(pieces),
                          skip_reason=SKIP_REMAINDER if dropped > 0 else "",
                          guard_before=before, guard_after=after))
        for piece_start, piece_end in pieces:
            at_shot_end = abs(piece_end - end) <= TIME_QUANTUM
            kind = SOURCE_END if (at_shot_end and abs(end - total) <= TIME_QUANTUM) else (
                SCENE_CUT if at_shot_end else TIME_CUT)
            segments.append(Segment(index=len(segments) + 1, start=piece_start, end=piece_end,
                                    shot_index=shot_index, boundary_kind=kind))

    guard_discarded = round(guard_discarded, 3)
    notes = _build_notes(shots, segments, low, high, usable, dropped_tails, guard,
                         guard_discarded)
    _assert_invariants(segments, shots, total, low, high, usable, guard)

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
        guard_seconds=guard,
        guard_discarded=guard_discarded,
    )


def _build_notes(shots: Sequence[Shot], segments: Sequence[Segment], low: float, high: float,
                 usable: Sequence[float], dropped_tails: float, guard: float,
                 guard_discarded: float) -> list[str]:
    notes: list[str] = []
    if not usable:
        notes.append("No scene changes were detected, so the whole source was treated as one "
                     "continuous shot.")
    elif guard > 0:
        # [FORK] P6: stated as a deliberate cost with its reason, not buried in the skip total.
        notes.append(f"{format_timecode(guard_discarded)} was trimmed away in {guard:g}s safety "
                     f"margins either side of {len(usable)} detected scene change(s). The "
                     f"detected time is an estimate, so standing back from it is what keeps "
                     f"frames of the neighbouring shot out of a clip. Splits inside a continuous "
                     f"shot, and the start and end of the source, lose nothing.")
    collapsed = [s for s in shots if not s.exported and s.duration <= 0]
    if collapsed:
        notes.append(f"{len(collapsed)} shot(s) were shorter than the safety margins around the "
                     f"cuts that define them, so nothing of them remained to export.")
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
                       low: float, high: float, scene_times: Sequence[float],
                       guard: float = 0.0) -> None:
    """The four guarantees, checked on every plan rather than trusted.

    Cheap, and it runs in production rather than only under test: a selection bug that exported a
    clip across a scene change would otherwise be discovered as a ruined source clip an hour of
    encoding later. Note what is NOT asserted any more -- complete coverage. Gaps are the point.

    [FORK] P6: the scene-change check is now a GUARD BAND check. A segment may not intersect
    ``(cut - guard, cut + guard)`` at all, which is strictly stronger than the old "must not
    contain the cut" and reduces to exactly that old test when ``guard`` is zero.
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
            if (segment.start < cut + guard - TIME_QUANTUM
                    and segment.end > cut - guard + TIME_QUANTUM):
                raise RawCutterError(
                    f"Segment {segment.index} ({segment.start}..{segment.end}) reaches inside the "
                    f"{guard:g}s safety margin around the scene change at {cut}, so it could "
                    f"carry a frame of the neighbouring shot.")
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
        # [FORK] P6: the unexported total, broken into its three causes. One number for "not
        # exported" lets a safety margin and a short shot hide in each other; the owner asked for
        # them to be distinguishable, and a plan that cannot say which is which cannot be audited.
        f"  of which {format_timecode(plan.guard_discarded)} is safety margin around "
        f"{plan.detected_scene_count} detected cut(s) at {plan.guard_seconds:g}s each side",
        f"  of which {format_timecode(plan.skipped_shot_duration)} is shots too short to keep",
        f"  of which {format_timecode(plan.dropped_tail_duration)} is leftover tail",
        "",
        f"Detection: sensitivity {plan.sensitivity}, scene threshold {plan.threshold:g}, "
        f"{plan.detected_scene_count} scene change(s) found.",
        f"Lengths: {plan.min_seconds:g}s minimum, {plan.max_seconds:g}s maximum.",
    ]
    lines.append("")
    lines.append("Skipped footage is intentional — the Raw Cutter keeps clean continuous shots "
                 "and leaves the rest. It is not trying to preserve every frame, and around a "
                 "detected cut it deliberately trims back rather than risk a frame of the "
                 "neighbouring shot.")
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
    "SCENE_GUARD_SECONDS",
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
