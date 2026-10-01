#!/usr/bin/env python3
"""[FORK] Digital-Union: Smart Mix / SFX Pool V1 — the pure SFX-placement planner (E).

Smart Mix adds deterministic sound-design accents — impacts, risers, atmospheres, transitions and
vocal shots — to the **same** final audio master Audio Layers V1 already produces::

    ORIGINAL MUSIC  ->  Stages 1-5  ->  selected_beats + beat_info      (the video edit)
    ORIGINAL MUSIC + voice + SFX + beat_info projection  ->  planners   (where sound lands)
    one plan  ->  audio_mixdown  ->  ONE mixed master WAV               (final render audio ONLY)

The ordering is load-bearing exactly as it is in D: the master is never analysed, so tempo, the beat
grid, sections, energy, Stage 4 cuts, Stage 5, Qwen and the visual planner all continue to see the
original music. Adding an SFX cannot move a single cut.

This module owns the *decisions* and nothing else — stdlib only, per CLAUDE.md's hard rule. No
FFmpeg, no ffprobe, no subprocess, no numpy, no Gradio, no filesystem walking, no Stage-5 import.
Enumeration and probing live in ``src/audio_mixdown.py``, which may import this module; this module
must never import it.

===============================================================================
Why there is no second mix engine
===============================================================================

E is a *second producer* into D's existing single graph, not a second pipeline. ``AudioMixPlan``
gained exactly one trailing defaulted field (``sfx_placements``) and the executor gained one stream
per placement. There is still one ``amix``, one ``alimiter`` and one exact-duration master.

===============================================================================
Three rules worth stating in full
===============================================================================

**Impact percentile is taken over the WHOLE aligned beat array, not over the bar anchors.** The
threshold is computed from every finite ``impact_strength`` value and the bar-anchor mask is applied
*afterwards*. That is not a taste call: it is what the accepted calibration measured, and
restricting the population first would silently shift every threshold.

**Cross-role collisions are resolved by priority, never by moving an anchor.** Roles plan in the
order riser -> impact -> transition -> vocal_shot -> atmosphere. Every accepted *non-atmosphere*
interval must be pairwise disjoint under half-open ``[start, end)`` semantics, so a riser ending
exactly where a transition begins is legal. A colliding candidate is skipped with a reason and the
planner moves on; nothing is nudged and nothing but an atmosphere is ever trimmed.

**The asset cursor advances on every candidate ATTEMPT, not on every success.** Candidate ``k`` of a
role always takes ``pool[k % len(pool)]`` whether or not it is placed. Without that, one unusually
long asset that never fits would be retried at every later anchor and permanently block the rest of
its pool.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beatsync_fork import input_manager as fork_input_manager

# ---------------------------------------------------------------------------
# Frozen role vocabulary
# ---------------------------------------------------------------------------

ROLE_IMPACT = "impact"
ROLE_RISER = "riser"
ROLE_ATMOSPHERE = "atmosphere"
ROLE_TRANSITION = "transition"
ROLE_VOCAL_SHOT = "vocal_shot"

#: The five roles, in the order they are *planned*. See the module docstring: risers are structural
#: lead-ins to genuine drops and therefore claim their span first; atmospheres underlay everything
#: and are resolved last because they never compete for space.
ROLE_PRIORITY = (ROLE_RISER, ROLE_IMPACT, ROLE_TRANSITION, ROLE_VOCAL_SHOT, ROLE_ATMOSPHERE)

#: The same five roles in a stable display order for reports and the UI.
ROLE_ORDER = (ROLE_IMPACT, ROLE_RISER, ROLE_ATMOSPHERE, ROLE_TRANSITION, ROLE_VOCAL_SHOT)

ALL_ROLES = frozenset(ROLE_ORDER)

#: Exact, case-folded, WHOLE first-level folder component -> role. Deliberately a literal table:
#: no ``contains``, no ``startswith``, no punctuation rewriting and no classifier. A folder either
#: names a role exactly or it is reported and ignored, so a typo is visible instead of guessed.
ROLE_ALIASES = {
    "impact": ROLE_IMPACT,
    "impacts": ROLE_IMPACT,
    "riser": ROLE_RISER,
    "risers": ROLE_RISER,
    "atmosphere": ROLE_ATMOSPHERE,
    "atmospheres": ROLE_ATMOSPHERE,
    "ambience": ROLE_ATMOSPHERE,
    "transition": ROLE_TRANSITION,
    "transitions": ROLE_TRANSITION,
    "vocalshot": ROLE_VOCAL_SHOT,
    "vocalshots": ROLE_VOCAL_SHOT,
    "vocal_shot": ROLE_VOCAL_SHOT,
    "vocal_shots": ROLE_VOCAL_SHOT,
    "vocal shot": ROLE_VOCAL_SHOT,
    "vocal shots": ROLE_VOCAL_SHOT,
}

#: Human labels for the GUI CheckboxGroup. The *value* is the exact internal role name, so the role
#: can never be re-derived from a display string by a lowercase/replace heuristic.
ROLE_CHOICES = (
    ("Impacts", ROLE_IMPACT),
    ("Risers", ROLE_RISER),
    ("Atmospheres", ROLE_ATMOSPHERE),
    ("Transitions", ROLE_TRANSITION),
    ("Vocal shots", ROLE_VOCAL_SHOT),
)

#: Exactly D's set. ``.m4a`` stays out for D's stated reason: advertising a format the main audio
#: picker refuses would make the UI and the executor disagree about the contract.
SUPPORTED_SFX_EXTENSIONS = (".mp3", ".wav", ".flac")

# ---------------------------------------------------------------------------
# Musical vocabulary, read literally from Stage 3 (`stage3_sections.classify_section`)
# ---------------------------------------------------------------------------

#: A riser targets a drop only when the music actually *enters* one. A drop preceded by another drop
#: or by a finale is a continuation, not an entry.
RISER_BLOCKING_PREDECESSORS = frozenset({"drop", "finale"})

#: Where an atmosphere bed may live, and how long a section must be to deserve one.
ATMOSPHERE_SECTION_TYPES = frozenset({"intro", "breakdown"})
ATMOSPHERE_MIN_SECTION_SECONDS = 12.0
ATMOSPHERE_CAP = 2

#: Vocal shots are short SFX, NOT Audio Layers narration. They fill the body of the track, which is
#: also why they carry no impact threshold: on real material "high impact AND outside a drop" is
#: very nearly empty, because on this kind of music the high-impact beats *are* the drops.
VOCAL_SHOT_SECTION_TYPES = frozenset({"chorus", "verse", "breakdown"})

#: At most two impacts may succeed inside any one musical section, keyed on the section's identity
#: (its index), never on its type — a track with four separate drops gets four separate budgets.
IMPACT_PER_SECTION_CAP = 2

#: One impact per this many seconds of music, as a global ceiling.
IMPACT_SECONDS_PER_GLOBAL_SLOT = 20.0

#: Anchor labels, reported so every row can explain itself.
ANCHOR_BAR = "bar anchor"
ANCHOR_DROP_ENTRY = "drop entry"
ANCHOR_SECTION_CHANGE = "section change"
ANCHOR_PHRASE = "phrase anchor"
ANCHOR_SECTION_BED = "section bed"

#: Comparison slack. Placement arithmetic is float seconds, and a riser whose end lands on a section
#: start must read as touching rather than overlapping.
EPSILON = 1e-9

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

DEFAULT_AMOUNT = 50
DEFAULT_SFX_LEVEL_PERCENT = 50
CONTROL_MIN = 0
CONTROL_MAX = 100

#: Amount knots measured on real material. Two families, and the split is deliberate:
#:
#: * continuous quantities have **no** value at amount 0 (the role is off there, so a threshold or a
#:   spacing would be meaningless). Below the lowest knot they **clamp** — extrapolating past the
#:   measured range would invent calibration nobody took.
#: * integer caps **do** have a real value at 0, so they interpolate all the way down and a very low
#:   amount legitimately resolves to "none of this role".
_AMOUNT_CONTINUOUS_KNOTS = {
    "impact_percentile": {25: 96.0, 50: 94.0, 75: 90.0, 100: 88.0},
    "impact_min_gap_seconds": {25: 6.0, 50: 4.0, 75: 3.0, 100: 3.0},
    "transition_min_gap_seconds": {25: 30.0, 50: 20.0, 75: 12.0, 100: 12.0},
}
_AMOUNT_CAP_KNOTS = {
    "transition_cap": {0: 0, 25: 4, 50: 6, 75: 8, 100: 8},
    "vocal_shot_cap": {0: 0, 25: 2, 50: 3, 75: 4, 100: 4},
}


# ---------------------------------------------------------------------------
# normalisation — total, never raising
# ---------------------------------------------------------------------------


def normalize_control(value: Any, default: int = DEFAULT_AMOUNT) -> int:
    """A whole 0..100 integer, clamped. Mirrors ``creative.normalize_control``'s type boundary.

    ``bool`` is rejected first because it subclasses ``int``; a *fractional* value falls back to the
    default rather than being floored, because ``50.5`` is not a request for 50 and rendering a
    setting the user never chose is worse than ignoring a malformed one. Nothing here may raise
    mid-render.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(CONTROL_MIN, min(CONTROL_MAX, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return default
        if value != int(value):
            return default
        return max(CONTROL_MIN, min(CONTROL_MAX, int(value)))
    return default


def normalize_roles(value: Any) -> frozenset:
    """Exact internal role names only; anything unrecognised is dropped.

    An explicit empty selection means *no roles*, which is a legitimate way to turn Smart Mix off.
    A value that is not iterable at all is malformed rather than empty, so it falls back to the
    all-five default instead of silently disabling the feature.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return frozenset(ALL_ROLES)
    try:
        items = list(value)
    except Exception:
        return frozenset(ALL_ROLES)
    return frozenset(item for item in items if isinstance(item, str) and item in ALL_ROLES)


def has_supported_sfx_extension(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    lowered = path.casefold()
    return any(lowered.endswith(ext) for ext in SUPPORTED_SFX_EXTENSIONS)


def role_for_folder(component: Any):
    """Exact case-folded whole-component lookup. Returns ``None`` for anything unrecognised."""
    if not isinstance(component, str):
        return None
    return ROLE_ALIASES.get(component.strip().casefold())


def order_sfx_paths(paths: Any) -> tuple:
    """Deterministic pool order via the fork's existing total order. Pure string work."""
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Iterable):
        return ()
    usable = [p for p in paths if isinstance(p, str) and p]
    return tuple(sorted(usable, key=fork_input_manager.order_key))


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------


def half_up(value: float) -> int:
    """Explicit half-up quantisation.

    Never ``round()``: banker's rounding sends both 2.5 and 3.5 to 2 and 4, so two different control
    positions would collapse onto one cap. On today's frozen knots no integer amount actually lands
    on a ``.5`` boundary, which is precisely why the rule is written down — a future knot edit must
    not be able to change behaviour silently.
    """
    return int(math.floor(float(value) + 0.5))


def percentile(values: Iterable, p: float) -> float:
    """NumPy's default ``linear`` percentile, stdlib only.

    Verified against ``numpy.percentile`` on the real 566-beat impact array at p50..p100 with a
    maximum absolute difference of **0.0**, so the pure planner reproduces the accepted calibration
    exactly while keeping ``beatsync_fork`` importable on a bare interpreter.

    Non-numbers, ``bool``, ``NaN`` and both infinities are excluded from the population rather than
    poisoning it; an empty population answers ``0.0`` so a degenerate track cannot raise mid-render.
    """
    data = sorted(
        float(v) for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool)
        and v == v and v not in (float("inf"), float("-inf"))
    )
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * (float(p) / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return data[int(position)]
    return data[lower] + (position - lower) * (data[upper] - data[lower])


def _interpolate(knots: Mapping, amount: int) -> float:
    positions = sorted(knots)
    if amount <= positions[0]:
        return float(knots[positions[0]])
    if amount >= positions[-1]:
        return float(knots[positions[-1]])
    for low, high in zip(positions, positions[1:]):
        if low <= amount <= high:
            ratio = (amount - low) / (high - low)
            return float(knots[low]) + ratio * (float(knots[high]) - float(knots[low]))
    return float(knots[positions[-1]])


@dataclass(frozen=True)
class AmountParams:
    """Everything the Amount control resolves to. Amount 0 never reaches here."""

    amount: int
    impact_percentile: float
    impact_min_gap_seconds: float
    transition_min_gap_seconds: float
    transition_cap: int
    vocal_shot_cap: int


def amount_params(amount: Any) -> AmountParams:
    """Resolve any 0..100 Amount. Callers must branch on ``amount == 0`` *before* calling this."""
    value = normalize_control(amount)
    return AmountParams(
        amount=value,
        impact_percentile=_interpolate(_AMOUNT_CONTINUOUS_KNOTS["impact_percentile"], value),
        impact_min_gap_seconds=_interpolate(
            _AMOUNT_CONTINUOUS_KNOTS["impact_min_gap_seconds"], value),
        transition_min_gap_seconds=_interpolate(
            _AMOUNT_CONTINUOUS_KNOTS["transition_min_gap_seconds"], value),
        transition_cap=half_up(_interpolate(_AMOUNT_CAP_KNOTS["transition_cap"], value)),
        vocal_shot_cap=half_up(_interpolate(_AMOUNT_CAP_KNOTS["vocal_shot_cap"], value)),
    )


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


class SmartMixStructureError(ValueError):
    """The musical structure handed to the planner is unusable.

    Raised rather than silently zipping mismatched beat-synchronous arrays: a shorter
    ``impact_strength`` than ``times`` would quietly re-map every threshold to the wrong beat.
    """


@dataclass(frozen=True)
class SmartMixConfig:
    """The three Smart Mix controls, normalised on construction."""

    enabled_roles: frozenset = frozenset(ALL_ROLES)
    amount: int = DEFAULT_AMOUNT
    sfx_level_percent: int = DEFAULT_SFX_LEVEL_PERCENT

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled_roles", normalize_roles(self.enabled_roles))
        object.__setattr__(self, "amount", normalize_control(self.amount, DEFAULT_AMOUNT))
        object.__setattr__(self, "sfx_level_percent",
                           normalize_control(self.sfx_level_percent, DEFAULT_SFX_LEVEL_PERCENT))

    @property
    def sfx_gain(self) -> float:
        """Linear gain — ``50`` percent means ``0.50``, **not** -50 dB, exactly as D's music floor."""
        return self.sfx_level_percent / 100.0

    @property
    def plans_anything(self) -> bool:
        """Amount 0 is a hard off-branch; an empty role selection is the other way to mean off."""
        return self.amount > 0 and bool(self.enabled_roles)


@dataclass(frozen=True)
class SfxAsset:
    """One prepared SFX file: already role-assigned, already ordered, already probed."""

    role: str
    path: str
    duration: float


@dataclass(frozen=True)
class SfxPlacement:
    """One resolved SFX event. ``end == start + play_duration`` always holds."""

    role: str
    path: str
    source_duration: float
    play_duration: float
    start: float
    end: float
    anchor: str
    reason: str
    trimmed: bool


@dataclass(frozen=True)
class SfxSkip:
    """A candidate anchor that was considered and rejected, with the reason it was rejected."""

    role: str
    at: float
    reason: str


@dataclass(frozen=True)
class MusicStructure:
    """The small immutable projection the planner needs — never the mutable ``beat_info`` itself."""

    music_duration: float
    beat_times: tuple
    is_bar_anchor: tuple
    is_phrase_anchor: tuple
    impact_strength: tuple
    sections: tuple          # tuple of (start, end, section_type)

    def section_index_at(self, instant: float) -> int:
        for index, (start, end, _type) in enumerate(self.sections):
            if start <= instant < end:
                return index
        return -1


@dataclass(frozen=True)
class SmartMixPlan:
    """What Smart Mix resolved for one render: the placements, the config and why things were not
    placed. Immutable, and carries no FFmpeg detail — the executor adds the gain."""

    placements: tuple
    config: SmartMixConfig
    library_asset_count: int
    library_role_count: int
    library_root: str
    skips: tuple
    empty_roles: tuple

    @property
    def total(self) -> int:
        return len(self.placements)

    def count_for(self, role: str) -> int:
        return sum(1 for placement in self.placements if placement.role == role)

    @property
    def counts(self) -> dict:
        return {role: self.count_for(role) for role in ROLE_ORDER}

    def summary_line(self) -> str:
        """One line for the success panel. Only shown when at least one SFX was actually used."""
        counts = self.counts
        parts = [f"{label.lower()} {counts[role]}"
                 for label, role in ROLE_CHOICES if counts[role]]
        return (f"Smart Mix: {self.total} SFX"
                + (" · " + " · ".join(parts) if parts else ""))

    def report_lines(self) -> tuple:
        """The read-only placement report. Deliberately no FFmpeg filtergraph in normal UI."""
        counts = self.counts
        header = [
            "Smart Mix",
            f"Library: {self.library_root}",
            f"Assets: {self.library_asset_count} in {self.library_role_count} role"
            f"{'' if self.library_role_count == 1 else 's'}",
            f"Amount: {self.config.amount}   SFX level: {self.config.sfx_level_percent}%",
            "Impacts {impact} · Risers {riser} · Atmospheres {atmosphere} · "
            "Transitions {transition} · Vocal shots {vocal_shot}".format(**counts),
            f"Total SFX: {self.total}",
            "",
        ]
        rows = []
        for placement in self.placements:
            name = placement.path.replace("\\", "/").rsplit("/", 1)[-1]
            row = (f"{placement.role} · {name} · "
                   f"{placement.start:.1f} → {placement.end:.1f} · {placement.anchor}")
            if placement.reason:
                row += f" · {placement.reason}"
            if placement.trimmed:
                row += (f" · trimmed {placement.source_duration:.1f}s → "
                        f"{placement.play_duration:.1f}s")
            rows.append(row)
        if not rows:
            rows.append("No SFX placed — see the reasons below.")

        tail = []
        for role in self.empty_roles:
            tail.append(f"{role}: enabled but the library has no assets for it")
        if self.skips:
            # Summarised, never one line per rejected candidate: a dense track can reject hundreds.
            by_reason = {}
            for skip in self.skips:
                by_reason.setdefault((skip.role, skip.reason), 0)
                by_reason[(skip.role, skip.reason)] += 1
            for (role, reason), count in sorted(by_reason.items()):
                tail.append(f"{role}: {count} candidate{'' if count == 1 else 's'} skipped — {reason}")
        if tail:
            tail.insert(0, "")
        return tuple(header + rows + tail)


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def _finite_floats(values: Any) -> tuple:
    out = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ()
        if number != number or number in (float("inf"), float("-inf")):
            return ()
        out.append(number)
    return tuple(out)


def project_structure(beat_info: Mapping) -> MusicStructure:
    """Build the planner's immutable view from the finished ``beat_info``.

    Only already-computed musical information is read — no Stage-5 data and no audio. The three
    beat-synchronous arrays must align with ``times``; a mismatch raises instead of zipping to the
    shorter sequence, because a silently re-indexed impact array changes every threshold's meaning
    while still producing a plausible-looking plan.
    """
    if not isinstance(beat_info, Mapping):
        raise SmartMixStructureError("beat_info is not a mapping")

    times = _finite_floats(beat_info.get("times") or ())
    if not times:
        raise SmartMixStructureError("no usable beat times")

    rhythm = beat_info.get("rhythm_data")
    if not isinstance(rhythm, Mapping):
        raise SmartMixStructureError("beat_info has no rhythm_data mapping")

    impact = _finite_floats(rhythm.get("impact_strength") or ())
    bar_raw = rhythm.get("is_bar_anchor") or ()
    phrase_raw = rhythm.get("is_phrase_anchor") or ()
    try:
        bar = tuple(bool(v) for v in bar_raw)
        phrase = tuple(bool(v) for v in phrase_raw)
    except TypeError:
        raise SmartMixStructureError("rhythm anchors are not iterable")

    if not (len(impact) == len(bar) == len(phrase) == len(times)):
        raise SmartMixStructureError(
            f"rhythm arrays are misaligned with times "
            f"(times={len(times)}, impact={len(impact)}, bar={len(bar)}, phrase={len(phrase)})")

    sections = []
    raw_sections = beat_info.get("sections")
    if isinstance(raw_sections, Iterable) and not isinstance(raw_sections, (str, bytes)):
        for item in raw_sections:
            if not isinstance(item, Mapping):
                continue
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if start != start or end != end or end <= start:
                continue
            section_type = item.get("type")
            sections.append((start, end,
                             section_type if isinstance(section_type, str) else "body"))
    sections.sort(key=lambda row: row[0])

    try:
        duration = float(beat_info.get("audio_duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration != duration or duration in (float("inf"), float("-inf")) or duration <= 0.0:
        duration = max(times) if times else 0.0
    if duration <= 0.0:
        raise SmartMixStructureError("music duration is not positive")

    return MusicStructure(
        music_duration=duration,
        beat_times=times,
        is_bar_anchor=bar,
        is_phrase_anchor=phrase,
        impact_strength=impact,
        sections=tuple(sections),
    )


# ---------------------------------------------------------------------------
# occupancy
# ---------------------------------------------------------------------------


class _Occupancy:
    """Accepted NON-atmosphere intervals, half-open ``[start, end)``.

    Atmospheres deliberately never enter this set: a bed is meant to sit under everything else.
    """

    __slots__ = ("spans",)

    def __init__(self) -> None:
        self.spans = []

    def conflicts(self, start: float, end: float) -> bool:
        return any(start < other_end - EPSILON and other_start < end - EPSILON
                   for other_start, other_end in self.spans)

    def add(self, start: float, end: float) -> None:
        self.spans.append((start, end))


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


def _pool_for(assets: Sequence, role: str) -> tuple:
    return tuple(asset for asset in assets if asset.role == role)


def _fits_music(start: float, end: float, music_duration: float) -> bool:
    return start >= -EPSILON and end <= music_duration + EPSILON


def _place_risers(structure: MusicStructure, pool: Sequence,
                  occupancy: _Occupancy, skips: list) -> list:
    """A riser's END aligns to a genuine drop entry. Never truncated, never shifted."""
    targets = []
    for index, (start, _end, section_type) in enumerate(structure.sections):
        if section_type != "drop":
            continue
        if index > 0 and structure.sections[index - 1][2] in RISER_BLOCKING_PREDECESSORS:
            continue
        targets.append(start)

    placed = []
    for ordinal, target in enumerate(targets):
        asset = pool[ordinal % len(pool)]            # cursor advances per ATTEMPT
        start = target - asset.duration
        end = target
        if start < -EPSILON:
            skips.append(SfxSkip(ROLE_RISER, target,
                                 "riser does not fit before its drop"))
            continue
        if not _fits_music(start, end, structure.music_duration):
            skips.append(SfxSkip(ROLE_RISER, target, "would overrun the music"))
            continue
        if occupancy.conflicts(start, end):
            skips.append(SfxSkip(ROLE_RISER, target, "collides with higher-priority SFX"))
            continue
        occupancy.add(start, end)
        placed.append(SfxPlacement(
            role=ROLE_RISER, path=asset.path, source_duration=asset.duration,
            play_duration=asset.duration, start=start, end=end,
            anchor=ANCHOR_DROP_ENTRY, reason=f"into drop at {target:.1f}s", trimmed=False))
    return placed


def _place_impacts(structure: MusicStructure, pool: Sequence, params: AmountParams,
                   occupancy: _Occupancy, skips: list) -> list:
    threshold = percentile(structure.impact_strength, params.impact_percentile)
    global_cap = max(1, math.ceil(structure.music_duration / IMPACT_SECONDS_PER_GLOBAL_SLOT))

    candidates = [structure.beat_times[i] for i in range(len(structure.beat_times))
                  if structure.is_bar_anchor[i] and structure.impact_strength[i] >= threshold]

    placed = []
    per_section = {}
    last_start = None
    for ordinal, instant in enumerate(candidates):
        if len(placed) >= global_cap:
            break
        asset = pool[ordinal % len(pool)]
        start, end = instant, instant + asset.duration
        if last_start is not None and start - last_start < params.impact_min_gap_seconds - EPSILON:
            continue
        section = structure.section_index_at(instant)
        if per_section.get(section, 0) >= IMPACT_PER_SECTION_CAP:
            continue
        if not _fits_music(start, end, structure.music_duration):
            skips.append(SfxSkip(ROLE_IMPACT, instant, "asset would overrun the music"))
            continue
        if occupancy.conflicts(start, end):
            skips.append(SfxSkip(ROLE_IMPACT, instant, "collides with higher-priority SFX"))
            continue
        occupancy.add(start, end)
        per_section[section] = per_section.get(section, 0) + 1
        last_start = start
        placed.append(SfxPlacement(
            role=ROLE_IMPACT, path=asset.path, source_duration=asset.duration,
            play_duration=asset.duration, start=start, end=end,
            anchor=ANCHOR_BAR, reason=f"impact >= p{params.impact_percentile:.0f}", trimmed=False))
    return placed


def _place_transitions(structure: MusicStructure, pool: Sequence, params: AmountParams,
                       occupancy: _Occupancy, skips: list) -> list:
    candidates = [structure.sections[i][0] for i in range(1, len(structure.sections))
                  if structure.sections[i][2] != structure.sections[i - 1][2]]

    placed = []
    last_start = None
    for ordinal, instant in enumerate(candidates):
        if len(placed) >= params.transition_cap:
            break
        asset = pool[ordinal % len(pool)]
        start, end = instant, instant + asset.duration
        if (last_start is not None
                and start - last_start < params.transition_min_gap_seconds - EPSILON):
            continue
        if not _fits_music(start, end, structure.music_duration):
            skips.append(SfxSkip(ROLE_TRANSITION, instant, "asset would overrun the music"))
            continue
        if occupancy.conflicts(start, end):
            skips.append(SfxSkip(ROLE_TRANSITION, instant, "collides with higher-priority SFX"))
            continue
        occupancy.add(start, end)
        last_start = start
        placed.append(SfxPlacement(
            role=ROLE_TRANSITION, path=asset.path, source_duration=asset.duration,
            play_duration=asset.duration, start=start, end=end,
            anchor=ANCHOR_SECTION_CHANGE, reason="section type change", trimmed=False))
    return placed


def _place_vocal_shots(structure: MusicStructure, pool: Sequence, params: AmountParams,
                       occupancy: _Occupancy, skips: list) -> list:
    candidates = []
    for index, instant in enumerate(structure.beat_times):
        if not structure.is_phrase_anchor[index]:
            continue
        section = structure.section_index_at(instant)
        if section < 0:
            continue
        if structure.sections[section][2] in VOCAL_SHOT_SECTION_TYPES:
            candidates.append((instant, section))

    placed = []
    used_sections = set()
    for ordinal, (instant, section) in enumerate(candidates):
        if len(placed) >= params.vocal_shot_cap:
            break
        asset = pool[ordinal % len(pool)]
        start, end = instant, instant + asset.duration
        if section in used_sections:
            continue
        if end > structure.sections[section][1] + EPSILON:
            skips.append(SfxSkip(ROLE_VOCAL_SHOT, instant,
                                 "does not fit inside its own section"))
            continue
        if not _fits_music(start, end, structure.music_duration):
            skips.append(SfxSkip(ROLE_VOCAL_SHOT, instant, "asset would overrun the music"))
            continue
        if occupancy.conflicts(start, end):
            skips.append(SfxSkip(ROLE_VOCAL_SHOT, instant, "collides with higher-priority SFX"))
            continue
        occupancy.add(start, end)
        used_sections.add(section)
        placed.append(SfxPlacement(
            role=ROLE_VOCAL_SHOT, path=asset.path, source_duration=asset.duration,
            play_duration=asset.duration, start=start, end=end,
            anchor=ANCHOR_PHRASE,
            reason=f"in {structure.sections[section][2]}", trimmed=False))
    return placed


def _place_atmospheres(structure: MusicStructure, pool: Sequence, skips: list) -> list:
    """The only role that may be trimmed: a bed is defined by the span it fills."""
    eligible = [(start, end, section_type)
                for (start, end, section_type) in structure.sections
                if section_type in ATMOSPHERE_SECTION_TYPES
                and (end - start) >= ATMOSPHERE_MIN_SECTION_SECONDS]
    eligible.sort(key=lambda row: (-(row[1] - row[0]), row[0]))

    placed = []
    for ordinal, (start, end, section_type) in enumerate(eligible[:ATMOSPHERE_CAP]):
        asset = pool[ordinal % len(pool)]
        span = end - start
        play = min(asset.duration, span)
        if play <= 0.0:
            skips.append(SfxSkip(ROLE_ATMOSPHERE, start, "section has no usable span"))
            continue
        placed.append(SfxPlacement(
            role=ROLE_ATMOSPHERE, path=asset.path, source_duration=asset.duration,
            play_duration=play, start=start, end=start + play,
            anchor=ANCHOR_SECTION_BED, reason=f"{section_type} section",
            trimmed=play < asset.duration - EPSILON))
    placed.sort(key=lambda placement: placement.start)
    return placed


def plan_sfx(structure: MusicStructure, assets: Sequence, config: SmartMixConfig,
             library_root: str = "") -> SmartMixPlan:
    """Resolve every enabled role against the finished musical structure.

    Deterministic and seedless: the same structure, the same ordered pools and the same config
    always produce the same plan. Roles are planned in :data:`ROLE_PRIORITY` order and share one
    non-atmosphere occupancy set, so the result depends on that order and on nothing else.
    """
    skips: list = []
    empty_roles: list = []
    placed: list = []

    if not config.plans_anything:
        return SmartMixPlan(
            placements=(), config=config, library_asset_count=len(assets),
            library_role_count=len({asset.role for asset in assets}),
            library_root=library_root, skips=(), empty_roles=())

    params = amount_params(config.amount)
    occupancy = _Occupancy()

    for role in ROLE_PRIORITY:
        if role not in config.enabled_roles:
            continue
        pool = _pool_for(assets, role)
        if not pool:
            empty_roles.append(role)
            continue
        if role == ROLE_RISER:
            placed.extend(_place_risers(structure, pool, occupancy, skips))
        elif role == ROLE_IMPACT:
            placed.extend(_place_impacts(structure, pool, params, occupancy, skips))
        elif role == ROLE_TRANSITION:
            placed.extend(_place_transitions(structure, pool, params, occupancy, skips))
        elif role == ROLE_VOCAL_SHOT:
            placed.extend(_place_vocal_shots(structure, pool, params, occupancy, skips))
        elif role == ROLE_ATMOSPHERE:
            placed.extend(_place_atmospheres(structure, pool, skips))

    placed.sort(key=lambda placement: (placement.start, ROLE_ORDER.index(placement.role)))
    return SmartMixPlan(
        placements=tuple(placed), config=config, library_asset_count=len(assets),
        library_role_count=len({asset.role for asset in assets}),
        library_root=library_root, skips=tuple(skips), empty_roles=tuple(empty_roles))


__all__ = [
    "ALL_ROLES",
    "ANCHOR_BAR",
    "ANCHOR_DROP_ENTRY",
    "ANCHOR_PHRASE",
    "ANCHOR_SECTION_BED",
    "ANCHOR_SECTION_CHANGE",
    "ATMOSPHERE_CAP",
    "ATMOSPHERE_MIN_SECTION_SECONDS",
    "ATMOSPHERE_SECTION_TYPES",
    "AmountParams",
    "DEFAULT_AMOUNT",
    "DEFAULT_SFX_LEVEL_PERCENT",
    "EPSILON",
    "IMPACT_PER_SECTION_CAP",
    "IMPACT_SECONDS_PER_GLOBAL_SLOT",
    "MusicStructure",
    "ROLE_ALIASES",
    "ROLE_ATMOSPHERE",
    "ROLE_CHOICES",
    "ROLE_IMPACT",
    "ROLE_ORDER",
    "ROLE_PRIORITY",
    "ROLE_RISER",
    "ROLE_TRANSITION",
    "ROLE_VOCAL_SHOT",
    "RISER_BLOCKING_PREDECESSORS",
    "SUPPORTED_SFX_EXTENSIONS",
    "SfxAsset",
    "SfxPlacement",
    "SfxSkip",
    "SmartMixConfig",
    "SmartMixPlan",
    "SmartMixStructureError",
    "VOCAL_SHOT_SECTION_TYPES",
    "amount_params",
    "half_up",
    "has_supported_sfx_extension",
    "normalize_control",
    "normalize_roles",
    "order_sfx_paths",
    "percentile",
    "plan_sfx",
    "project_structure",
    "role_for_folder",
]
