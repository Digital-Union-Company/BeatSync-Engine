#!/usr/bin/env python3
"""[FORK] Digital-Union: Audio Layers V1 — the pure voice-placement planner (D).

Audio Layers lets a render carry spoken voice over the music **without touching the edit the music
produced**::

    ORIGINAL MUSIC  ->  Stages 1-5  ->  selected_beats + beat_info     (the video edit)
    ORIGINAL MUSIC + voice + beat_info projection  ->  this planner    (where speech lands)
    plan  ->  audio_mixdown  ->  one mixed master WAV                  (final render audio ONLY)

That ordering is the load-bearing part. The mixed master is never analysed: tempo, the beat grid,
sections, energy, cut selection, Qwen and the visual targets all continue to see the original music,
so adding a voice clip can never change which shots were chosen or where the cuts fall.

This module owns the *decisions* and nothing else — no FFmpeg, no subprocess, no probing, no
filesystem access beyond pure path-string ordering. It is stdlib-only (CLAUDE.md's hard rule), so
every placement rule is testable on a bare interpreter. The runtime half lives in
``src/audio_mixdown.py`` and may import this module; this module must never import it.

===============================================================================
What V1 decides
===============================================================================

Voice clips play in **deterministic filename order**, never in browser multi-select order — the
HTML File API hands back whatever the OS dialog supplies, which is not the order the user clicked.
Ordering therefore reuses :func:`input_manager.order_key`, the project's existing documented total
order, so there is one path-sorting rule in the fork rather than two subtly different ones.

Placement is deterministic and seedless. There is no RNG, no Variation Seed and no Master Creative
Seed: the same music structure, voice durations and :class:`AudioMixConfig` always produce the same
plan.

**Variant Lab reaches audio since E2 V1 — and this planner still knows nothing about it.** That is
not a contradiction, because what E2 added happens entirely *upstream* of here. Variant Lab resolves
one Audio Layers value, ``music_under_voice_percent``, and writes the existing *Music under voice*
widget with it; the GUI then builds an :class:`AudioMixConfig` from that visible widget at
render-click time, exactly as it did before E2 existed. Nothing hands this module a generated
object: no master seed, no ``"audio"`` RNG stream, no ``AudioRecipe``, no ``AudioVariantConfig`` and
no Variant Lab range ever arrives here. The planner receives one already-normalised config, and a
config is a config however its number was chosen — so every placement rule below, and the duck
model, remain exactly as deterministic and as seedless as they were in V1.

:class:`AudioMixConfig` is therefore where a varied value *lands*, not a destination still waiting
on future work, and it is still emphatically not a field of ``CreativeRecipe``. E2's other two
levels are Smart Mix controls and never enter this planner either.

===============================================================================
Two rules worth stating in full
===============================================================================

**Avoid drops means the WHOLE spoken interval.** A clip that merely *starts* before a drop has not
avoided it: measured on real material, a 12 s clip starting 0.44 s before a drop puts 11.56 s —
96% of its speech — inside that drop. So the rule is interval disjointness against every ``drop``
and ``finale`` span, not a test on the start instant.

**Preference is bounded, never a leap.** Snapping to "the nicest section start anywhere ahead" was
measured on the real track to throw the first clip 27 s forward and the second 83 s forward, leaving
84 s holes and wasting the song. The planner therefore finds the earliest legal beat first and only
looks :data:`PLACEMENT_LOOKAHEAD_SECONDS` beyond it for a better musical anchor.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beatsync_fork import input_manager as fork_input_manager

# ---------------------------------------------------------------------------
# Fixed V1 constants — deliberately NOT user controls
# ---------------------------------------------------------------------------

#: How long the music takes to drop to the floor *before* the first syllable, and to come back
#: *after* the last one. Fixed so V1 ships four controls rather than six; the envelope completing
#: before speech starts is what stops the first word being buried.
DUCK_ATTACK_SECONDS = 0.250
DUCK_RELEASE_SECONDS = 0.400

#: How far past the earliest legal beat the planner may look for a nicer musical anchor. Bounded on
#: measured evidence: unbounded preference leaps tens of seconds and strands the rest of the track.
PLACEMENT_LOOKAHEAD_SECONDS = 4.0

#: Section types the planner prefers to start a clip on, read literally from Stage 3's vocabulary
#: (``stage3_sections.classify_section``). No second classifier is invented here.
PREFERRED_SECTION_TYPES = frozenset({"intro", "verse", "breakdown", "bridge", "outro"})

#: Section types speech may not overlap when ``avoid_drops`` is on. Deliberately only these two:
#: ``hook`` and ``chorus`` are energetic but common, and excluding them would make placement fail on
#: chorus-heavy tracks for no real gain.
AVOIDED_SECTION_TYPES = frozenset({"drop", "finale"})

#: Accepted voice extensions — exactly the music input's set (``gui.py``'s audio picker and the CLI
#: both say MP3/WAV/FLAC). ``.m4a`` is excluded on purpose: advertising a format the main audio
#: input refuses would make the UI and the executor disagree about the contract.
SUPPORTED_VOICE_EXTENSIONS = (".mp3", ".wav", ".flac")

# ---------------------------------------------------------------------------
# Control ranges. Seconds are genuinely fractional, so these deliberately do NOT reuse
# `creative.normalize_control`, which exists for 0..100 integer creative controls.
# ---------------------------------------------------------------------------

DEFAULT_START_DELAY_SECONDS = 2.0
START_DELAY_MIN_SECONDS = 0.0
START_DELAY_MAX_SECONDS = 60.0

DEFAULT_MIN_GAP_SECONDS = 1.0
MIN_GAP_MIN_SECONDS = 0.0
MIN_GAP_MAX_SECONDS = 30.0

DEFAULT_MUSIC_UNDER_VOICE_PERCENT = 35
MUSIC_UNDER_VOICE_MIN = 0
MUSIC_UNDER_VOICE_MAX = 100

#: Anchor labels, reported so a shifted clip can explain itself.
ANCHOR_PREFERRED_SECTION = "preferred section start"
ANCHOR_SECTION = "section start"
ANCHOR_BEAT = "next legal beat"


# ---------------------------------------------------------------------------
# normalisation — total, never raising
# ---------------------------------------------------------------------------


def normalize_seconds(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Coerce a user-entered duration in seconds into ``[minimum, maximum]``.

    A seconds control is not a creative 0..100 integer: ``2.5`` is a perfectly good start delay, so
    fractional values are *accepted* here where ``creative.normalize_control`` rejects them. What is
    shared is the explicit type boundary — ``bool`` is refused first because it subclasses ``int``,
    and a malformed value becomes the default rather than acquiring a guessed meaning. ``NaN`` and
    both infinities are refused because clamping them would silently invent a bound, and an infinite
    delay would otherwise become an enormous FFmpeg ``adelay``.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN fails both comparisons; infinities clamp to a real bound, which is not a reading of
        # what the user typed, so both are refused outright.
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return max(minimum, min(maximum, number))
    return default


def normalize_start_delay(value: Any) -> float:
    return normalize_seconds(value, DEFAULT_START_DELAY_SECONDS,
                             START_DELAY_MIN_SECONDS, START_DELAY_MAX_SECONDS)


def normalize_min_gap(value: Any) -> float:
    return normalize_seconds(value, DEFAULT_MIN_GAP_SECONDS,
                             MIN_GAP_MIN_SECONDS, MIN_GAP_MAX_SECONDS)


def normalize_music_under_voice(value: Any) -> int:
    """Percent of the normal music level during speech. A whole 0..100 integer, clamped."""
    if isinstance(value, bool):
        return DEFAULT_MUSIC_UNDER_VOICE_PERCENT
    if isinstance(value, int):
        return max(MUSIC_UNDER_VOICE_MIN, min(MUSIC_UNDER_VOICE_MAX, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return DEFAULT_MUSIC_UNDER_VOICE_PERCENT
        return max(MUSIC_UNDER_VOICE_MIN, min(MUSIC_UNDER_VOICE_MAX, int(round(value))))
    return DEFAULT_MUSIC_UNDER_VOICE_PERCENT


def normalize_avoid_drops(value: Any) -> bool:
    """Only a real ``bool`` is honoured; anything else is the protective default (``True``)."""
    return value if isinstance(value, bool) else True


def order_voice_paths(paths: Any) -> tuple:
    """Voice clips in deterministic path order — the V1 answer to "what order do these play in?".

    Browser multi-select order is not trustworthy, so it is not used. This delegates to
    :func:`input_manager.order_key`, already documented there as a *total* order (separator
    normalised, case-folded, with the normalised path as tie-break so two paths differing only in
    case still order deterministically). Reusing it keeps one path-sorting rule in the fork.

    Pure string work: no ``stat``, no filesystem access. Non-string entries are dropped rather than
    raising, because this runs on a widget value.
    """
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Iterable):
        return ()
    usable = [p for p in paths if isinstance(p, str) and p]
    return tuple(sorted(usable, key=fork_input_manager.order_key))


def has_supported_voice_extension(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    lowered = path.casefold()
    return any(lowered.endswith(ext) for ext in SUPPORTED_VOICE_EXTENSIONS)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioMixConfig:
    """The four Audio Layers controls, normalised on construction."""

    start_delay_seconds: float = DEFAULT_START_DELAY_SECONDS
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS
    avoid_drops: bool = True
    music_under_voice_percent: int = DEFAULT_MUSIC_UNDER_VOICE_PERCENT

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_delay_seconds",
                           normalize_start_delay(self.start_delay_seconds))
        object.__setattr__(self, "min_gap_seconds", normalize_min_gap(self.min_gap_seconds))
        object.__setattr__(self, "avoid_drops", normalize_avoid_drops(self.avoid_drops))
        object.__setattr__(self, "music_under_voice_percent",
                           normalize_music_under_voice(self.music_under_voice_percent))

    @property
    def music_floor(self) -> float:
        """Linear music gain under speech — ``35`` percent means gain ``0.35``, **not** -35 dB."""
        return self.music_under_voice_percent / 100.0


@dataclass(frozen=True)
class VoiceInput:
    """One voice clip, already ordered and already probed by the runtime half."""

    index: int
    path: str
    duration: float


@dataclass(frozen=True)
class MusicSection:
    """The only three section fields placement needs. Stage 3's ``type`` is used literally."""

    start: float
    end: float
    section_type: str

    def overlaps(self, start: float, end: float) -> bool:
        """Half-open ``[start, end)`` overlap. Boundary-touching is **not** an overlap."""
        return start < self.end and end > self.start


@dataclass(frozen=True)
class VoicePlacement:
    index: int
    path: str
    duration: float
    start: float
    end: float
    section_type: str
    anchor: str
    shift_seconds: float


@dataclass(frozen=True)
class DuckEvent:
    """One music-ducking envelope. ``start``/``end`` include the attack and release ramps."""

    voice_start: float
    voice_end: float
    start: float
    end: float
    floor: float
    attack: float
    release: float


@dataclass(frozen=True)
class PlacementFailure:
    """No legal placement exists. V1 never drops, truncates or overlaps a clip to avoid this."""

    index: int
    path: str
    duration: float
    after_seconds: float
    reason: str


@dataclass(frozen=True)
class AudioMixPlan:
    """The final audio plan: voice placements, their duck envelopes, and any Smart Mix SFX.

    [FORK] Digital-Union (Smart Mix V1 / E): ``sfx_placements`` is the **one** field E added, and it
    is last and defaulted so every pre-existing positional construction
    ``AudioMixPlan(duration, placements, duck_events, config)`` stays valid. It deliberately carries
    only *resolved placements*: Smart Mix's own config, amount, level and library root stay on
    :class:`smart_mix.SmartMixPlan`, because the executor needs the events, not the generator that
    produced them. The SFX gain reaches FFmpeg as a separate execution argument for the same reason.
    There is no parallel final-audio plan type.
    """

    music_duration: float
    placements: tuple
    duck_events: tuple
    config: AudioMixConfig
    sfx_placements: tuple = ()

    @property
    def voice_count(self) -> int:
        return len(self.placements)

    @property
    def sfx_count(self) -> int:
        return len(self.sfx_placements)

    def summary_line(self) -> str:
        """One line for the success panel. Only ever shown when voice was actually used."""
        return (f"Audio Layers: {self.voice_count} voice clip"
                f"{'' if self.voice_count == 1 else 's'} · "
                f"music {self.config.music_under_voice_percent}% · "
                f"{len(self.duck_events)} duck event"
                f"{'' if len(self.duck_events) == 1 else 's'}")

    def report_lines(self) -> tuple:
        """The read-only placement report. Deliberately no FFmpeg filtergraph in normal UI."""
        header = [
            "Audio Layers",
            f"Music + {self.voice_count} voice clip{'' if self.voice_count == 1 else 's'}",
            f"Music under voice: {self.config.music_under_voice_percent}%",
            f"Duck events: {len(self.duck_events)}",
            f"Attack / release: {int(DUCK_ATTACK_SECONDS * 1000)} / "
            f"{int(DUCK_RELEASE_SECONDS * 1000)} ms",
            "",
        ]
        rows = []
        for placement in self.placements:
            name = placement.path.replace("\\", "/").rsplit("/", 1)[-1]
            row = (f"{placement.index + 1} · {name} · {placement.duration:.1f}s · "
                   f"{placement.start:.1f} → {placement.end:.1f} · "
                   f"{placement.section_type} · {placement.anchor}")
            if placement.shift_seconds >= 0.05:
                row += f" (+{placement.shift_seconds:.1f}s)"
            rows.append(row)
        return tuple(header + rows)


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def project_sections(sections: Any) -> tuple:
    """The small immutable section view the planner needs, from Stage 3's section mappings.

    Takes the ``beat_info["sections"]`` *list*, never the whole mutable ``beat_info`` dict — the
    planner has no business reading energy, impact, brightness or the dominant pattern, so it is
    not handed them. Malformed entries are skipped rather than raising.
    """
    if not isinstance(sections, Iterable) or isinstance(sections, (str, bytes)):
        return ()
    out = []
    for item in sections:
        if not isinstance(item, Mapping):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start == start and end == end) or end <= start:
            continue
        section_type = item.get("type")
        out.append(MusicSection(start, end,
                                section_type if isinstance(section_type, str) else "body"))
    return tuple(out)


def project_beat_times(beat_times: Any) -> tuple:
    """Finite, non-negative, ascending beat times. Accepts a numpy array without importing numpy."""
    if not isinstance(beat_times, Iterable) or isinstance(beat_times, (str, bytes)):
        return ()
    out = []
    for value in beat_times:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
            continue
        out.append(number)
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


def _avoided_spans(sections: Sequence, avoid_drops: bool) -> tuple:
    if not avoid_drops:
        return ()
    return tuple(s for s in sections if s.section_type in AVOIDED_SECTION_TYPES)


def _is_legal(start: float, duration: float, music_duration: float, avoided: Sequence) -> bool:
    """Legal when the whole clip fits inside the music and touches no avoided section.

    Half-open intervals throughout: a clip ending exactly where a drop begins is legal, and so is
    one starting exactly where a drop ends.
    """
    end = start + duration
    if start < 0.0 or end > music_duration:
        return False
    return not any(section.overlaps(start, end) for section in avoided)


def _section_type_at(sections: Sequence, instant: float) -> str:
    for section in sections:
        if section.start <= instant < section.end:
            return section.section_type
    return "body"


def plan_voice_placements(music_duration: float, beat_times: Sequence,
                          sections: Sequence, voices: Sequence,
                          config: AudioMixConfig):
    """Place every voice clip, in the order given. Returns an :class:`AudioMixPlan` or a
    :class:`PlacementFailure`.

    Deterministic and seedless. The clip order is the caller's and is never rearranged for musical
    convenience — the planner decides only *where* each ordered clip lands.
    """
    avoided = _avoided_spans(sections, config.avoid_drops)
    beats = tuple(beat_times)
    section_starts = tuple((s.start, s.section_type) for s in sections
                           if s.section_type not in AVOIDED_SECTION_TYPES or not config.avoid_drops)

    placements = []
    cursor = config.start_delay_seconds
    for voice in voices:
        earliest = None
        for beat in beats:
            if beat + 1e-9 < cursor:
                continue
            if _is_legal(beat, voice.duration, music_duration, avoided):
                earliest = beat
                break
        if earliest is None:
            return PlacementFailure(
                index=voice.index, path=voice.path, duration=voice.duration,
                after_seconds=cursor,
                reason=(f"Voice {voice.index + 1} ({voice.duration:.1f}s) has no legal placement "
                        f"after {cursor:.1f}s"),
            )

        chosen, anchor = earliest, ANCHOR_BEAT
        # Bounded preference: look only a little way past the earliest legal beat, so a nicer
        # anchor can win but a distant one can never strand the rest of the track.
        limit = earliest + PLACEMENT_LOOKAHEAD_SECONDS
        for label, wanted in ((ANCHOR_PREFERRED_SECTION, PREFERRED_SECTION_TYPES),
                              (ANCHOR_SECTION, None)):
            candidates = [
                start for start, section_type in section_starts
                if start + 1e-9 >= cursor and start <= limit + 1e-9
                and (wanted is None or section_type in wanted)
                and _is_legal(start, voice.duration, music_duration, avoided)
            ]
            if candidates:
                chosen, anchor = min(candidates), label
                break

        placements.append(VoicePlacement(
            index=voice.index, path=voice.path, duration=voice.duration,
            start=chosen, end=chosen + voice.duration,
            section_type=_section_type_at(sections, chosen),
            anchor=anchor, shift_seconds=chosen - cursor,
        ))
        cursor = chosen + voice.duration + config.min_gap_seconds

    return AudioMixPlan(
        music_duration=music_duration,
        placements=tuple(placements),
        duck_events=build_duck_events(tuple(placements), music_duration, config),
        config=config,
    )


# ---------------------------------------------------------------------------
# ducking
# ---------------------------------------------------------------------------


def build_duck_events(placements: Sequence, music_duration: float,
                      config: AudioMixConfig) -> tuple:
    """One envelope per placement. Windows are clamped to the music, ramps are not rescaled."""
    floor = config.music_floor
    return tuple(
        DuckEvent(
            voice_start=p.start,
            voice_end=p.end,
            start=max(0.0, p.start - DUCK_ATTACK_SECONDS),
            end=min(music_duration, p.end + DUCK_RELEASE_SECONDS),
            floor=floor,
            attack=DUCK_ATTACK_SECONDS,
            release=DUCK_RELEASE_SECONDS,
        )
        for p in placements
    )


def _duck_amount_at(event: DuckEvent, instant: float) -> float:
    """How fully this event is ducking at ``instant``: 0 outside, 1 during the spoken body."""
    rising = (instant - (event.voice_start - event.attack)) / event.attack
    falling = ((event.voice_end + event.release) - instant) / event.release
    return max(0.0, min(1.0, min(max(0.0, min(1.0, rising)), max(0.0, min(1.0, falling)))))


def duck_gain_at(events: Sequence, instant: float, floor: float | None = None) -> float:
    """The music gain at ``instant`` — the **minimum** gain any active event asks for.

    Equivalently, the deepest duck wins. That is what makes overlap well defined without a hidden
    "min gap must exceed attack + release" constraint: if one event is still releasing while the
    next is already attacking, the music simply stays down rather than bouncing back up. It is also
    why the FFmpeg expression is built from ``max()`` of duck amounts and never from a chain of
    ``if()`` where whichever event matched first would win.
    """
    if not events:
        return 1.0
    resolved_floor = events[0].floor if floor is None else floor
    deepest = max(_duck_amount_at(event, instant) for event in events)
    return 1.0 - (1.0 - resolved_floor) * deepest


__all__ = [
    "ANCHOR_BEAT",
    "ANCHOR_PREFERRED_SECTION",
    "ANCHOR_SECTION",
    "AVOIDED_SECTION_TYPES",
    "DEFAULT_MIN_GAP_SECONDS",
    "DEFAULT_MUSIC_UNDER_VOICE_PERCENT",
    "DEFAULT_START_DELAY_SECONDS",
    "DUCK_ATTACK_SECONDS",
    "DUCK_RELEASE_SECONDS",
    "MIN_GAP_MAX_SECONDS",
    "MIN_GAP_MIN_SECONDS",
    "MUSIC_UNDER_VOICE_MAX",
    "MUSIC_UNDER_VOICE_MIN",
    "PLACEMENT_LOOKAHEAD_SECONDS",
    "PREFERRED_SECTION_TYPES",
    "START_DELAY_MAX_SECONDS",
    "START_DELAY_MIN_SECONDS",
    "SUPPORTED_VOICE_EXTENSIONS",
    "AudioMixConfig",
    "AudioMixPlan",
    "DuckEvent",
    "MusicSection",
    "PlacementFailure",
    "VoiceInput",
    "VoicePlacement",
    "build_duck_events",
    "duck_gain_at",
    "has_supported_voice_extension",
    "normalize_avoid_drops",
    "normalize_min_gap",
    "normalize_music_under_voice",
    "normalize_seconds",
    "normalize_start_delay",
    "order_voice_paths",
    "plan_voice_placements",
    "project_beat_times",
    "project_sections",
]
