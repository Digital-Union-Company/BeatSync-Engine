"""Audio Layers V1: the pure placement planner and duck model.

Everything here runs on a bare interpreter — no FFmpeg, no audio files, no runtime. That is the
point of splitting the feature: every *decision* (where speech lands, how deep the music ducks, what
counts as legal) is arithmetic over small numbers and can be pinned exactly.

Two rules get disproportionate attention because they are the ones that would silently degrade:

* **avoid-drops is whole-interval.** A start-instant test would let a clip begin 0.4 s before a drop
  and talk straight through it; on real material that put 96% of a clip's speech inside the drop.
* **preference is bounded.** Unbounded "snap to the nicest section start" was measured to throw a
  clip 83 s forward and strand the rest of the track, so the lookahead window is asserted, not
  assumed.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from beatsync_fork import audio_mix as am
from beatsync_fork import input_manager as fork_input_manager

FLOOR = 0.35


def executable_source(module) -> str:
    """A module's source with every docstring removed, lowercased.

    The prose in `audio_mix` legitimately names `CreativeRecipe` and `Variant Lab` in order to say
    that neither may reach it, so the guards below must look at code rather than at comments.
    """
    import ast
    import copy
    import inspect

    tree = copy.deepcopy(ast.parse(inspect.getsource(module)))
    for inner in ast.walk(tree):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree)).lower()


def beats(step: float = 0.5, count: int = 2400) -> tuple:
    """A dense regular grid, so placement is governed by the rules rather than by grid gaps."""
    return tuple(round(i * step, 6) for i in range(count))


def sections(*spec) -> tuple:
    return tuple(am.MusicSection(s, e, t) for s, e, t in spec)


def voices(*durations) -> tuple:
    return tuple(am.VoiceInput(i, f"voices/{i:02d}_clip.wav", d)
                 for i, d in enumerate(durations))


SIMPLE = sections((0.0, 40.0, "intro"), (40.0, 60.0, "drop"), (60.0, 200.0, "verse"))


# ===========================================================================
# 1. CONFIG
# ===========================================================================


def test_shipped_defaults():
    config = am.AudioMixConfig()
    assert config.start_delay_seconds == 2.0
    assert config.min_gap_seconds == 1.0
    assert config.avoid_drops is True
    assert config.music_under_voice_percent == 35
    assert config.music_floor == 0.35


def test_fixed_v1_constants():
    assert am.DUCK_ATTACK_SECONDS == 0.250
    assert am.DUCK_RELEASE_SECONDS == 0.400
    assert am.PLACEMENT_LOOKAHEAD_SECONDS == 4.0


def test_the_section_vocabulary_is_stage_threes_own():
    """Read literally from `stage3_sections.classify_section`; no second classifier is invented."""
    assert am.PREFERRED_SECTION_TYPES == frozenset(
        {"intro", "verse", "breakdown", "bridge", "outro"})
    assert am.AVOIDED_SECTION_TYPES == frozenset({"drop", "finale"})
    # energetic but common types are deliberately NOT avoided
    for common in ("hook", "chorus", "body"):
        assert common not in am.AVOIDED_SECTION_TYPES


def test_supported_voice_extensions_match_the_music_input():
    assert am.SUPPORTED_VOICE_EXTENSIONS == (".mp3", ".wav", ".flac")
    assert ".m4a" not in am.SUPPORTED_VOICE_EXTENSIONS


@pytest.mark.parametrize("value,expected", [
    (2.0, 2.0), (0.0, 0.0), (0.5, 0.5), (2.5, 2.5), (7, 7.0), (60.0, 60.0),
    (-1.0, 0.0), (999.0, 60.0),                       # clamped, not refused
    (None, 2.0), (True, 2.0), (False, 2.0), ("3", 2.0), ("", 2.0), (object(), 2.0),
    (float("nan"), 2.0), (float("inf"), 2.0), (float("-inf"), 2.0),
])
def test_start_delay_normalisation_is_total(value: Any, expected: float):
    """Seconds are genuinely fractional, so `2.5` is honoured here where the integer creative
    controls would refuse it. `bool` is still rejected first, and NaN/inf become the default rather
    than clamping to a bound the user never typed — an infinite delay would otherwise become an
    enormous FFmpeg `adelay`."""
    assert am.normalize_start_delay(value) == expected


@pytest.mark.parametrize("value,expected", [
    (1.0, 1.0), (0.0, 0.0), (0.25, 0.25), (30.0, 30.0), (-5, 0.0), (99.0, 30.0),
    (None, 1.0), (True, 1.0), ("2", 1.0), (float("nan"), 1.0), (float("inf"), 1.0),
])
def test_min_gap_normalisation_is_total(value: Any, expected: float):
    assert am.normalize_min_gap(value) == expected


@pytest.mark.parametrize("value,expected", [
    (35, 35), (0, 0), (100, 100), (-10, 0), (140, 100), (35.0, 35), (34.6, 35),
    (None, 35), (True, 35), (False, 35), ("50", 35), (float("nan"), 35), (object(), 35),
])
def test_music_under_voice_normalisation_is_total(value: Any, expected: int):
    assert am.normalize_music_under_voice(value) == expected


@pytest.mark.parametrize("value,expected", [
    (True, True), (False, False), (None, True), (1, True), ("no", True), (object(), True),
])
def test_avoid_drops_normalisation_defaults_protective(value: Any, expected: bool):
    assert am.normalize_avoid_drops(value) == expected


def test_the_config_normalises_on_construction():
    config = am.AudioMixConfig(start_delay_seconds="x", min_gap_seconds=float("inf"),
                               avoid_drops=None, music_under_voice_percent=500)
    assert (config.start_delay_seconds, config.min_gap_seconds) == (2.0, 1.0)
    assert config.avoid_drops is True
    assert config.music_under_voice_percent == 100


def test_min_gap_is_not_secretly_forced_above_attack_plus_release():
    """Overlap is handled by the duck model's minimum-gain rule, not by a hidden constraint the
    user never asked for."""
    assert am.AudioMixConfig(min_gap_seconds=0.1).min_gap_seconds == 0.1
    assert 0.1 < am.DUCK_ATTACK_SECONDS + am.DUCK_RELEASE_SECONDS


@pytest.mark.parametrize("percent,floor", [(0, 0.0), (35, 0.35), (100, 1.0), (50, 0.5)])
def test_music_floor_is_linear_gain_not_decibels(percent: int, floor: float):
    assert am.AudioMixConfig(music_under_voice_percent=percent).music_floor == pytest.approx(floor)


# ===========================================================================
# 2. DETERMINISTIC VOICE ORDER
# ===========================================================================


def test_voice_order_delegates_to_the_projects_existing_total_order():
    """One path-sorting rule in the fork, not two subtly different ones."""
    paths = ["b/02.wav", "b/10.wav", "b/01.wav", "B/03.WAV"]
    assert am.order_voice_paths(paths) == tuple(sorted(paths, key=fork_input_manager.order_key))


def test_voice_order_ignores_the_given_sequence_order():
    forward = am.order_voice_paths(["a/01.wav", "a/02.wav", "a/03.wav"])
    backward = am.order_voice_paths(["a/03.wav", "a/01.wav", "a/02.wav"])
    assert forward == backward
    assert [p.rsplit("/", 1)[-1] for p in forward] == ["01.wav", "02.wav", "03.wav"]


def test_voice_order_normalises_separators():
    assert am.order_voice_paths([r"x\02.wav", "x/01.wav"]) == (
        tuple(sorted([r"x\02.wav", "x/01.wav"], key=fork_input_manager.order_key)))


@pytest.mark.parametrize("value", [None, "", "a/01.wav", 7, object(), b"x"])
def test_voice_order_is_total(value: Any):
    assert am.order_voice_paths(value) == ()


def test_voice_order_drops_non_string_entries_without_raising():
    assert am.order_voice_paths(["a/01.wav", None, 5, "", "a/02.wav"]) == ("a/01.wav", "a/02.wav")


@pytest.mark.parametrize("path,ok", [
    ("a/x.wav", True), ("a/x.MP3", True), ("a/x.flac", True), ("a/x.FLAC", True),
    ("a/x.m4a", False), ("a/x.ogg", False), ("a/x", False), (None, False), (7, False),
])
def test_supported_extension_check(path: Any, ok: bool):
    assert am.has_supported_voice_extension(path) is ok


# ===========================================================================
# 3. PROJECTION
# ===========================================================================


def test_sections_project_to_the_three_fields_placement_needs():
    projected = am.project_sections([
        {"index": 0, "start": 0.0, "end": 10.0, "duration": 10.0, "type": "intro",
         "energy": 0.16, "impact": 0.2, "brightness": 0.3, "dominant_pattern": "mixed"},
    ])
    assert projected == (am.MusicSection(0.0, 10.0, "intro"),)
    assert not hasattr(projected[0], "energy")


def test_section_projection_is_total():
    assert am.project_sections(None) == ()
    assert am.project_sections("sections") == ()
    assert am.project_sections([None, 7, {}, {"start": 0.0}]) == ()
    assert am.project_sections([{"start": 5.0, "end": 1.0, "type": "intro"}]) == ()
    assert am.project_sections([{"start": 0.0, "end": 1.0}]) == (am.MusicSection(0.0, 1.0, "body"),)


def test_beat_time_projection_accepts_a_numpy_like_sequence_and_is_total():
    class FakeArray:
        def __init__(self, values):
            self._v = values

        def __iter__(self):
            return iter(self._v)

    assert am.project_beat_times(FakeArray([2.0, 1.0, 3.0])) == (1.0, 2.0, 3.0)
    assert am.project_beat_times(None) == ()
    assert am.project_beat_times([1.0, float("nan"), float("inf"), -2.0, "x"]) == (1.0,)


# ===========================================================================
# 4. PLACEMENT
# ===========================================================================


def test_voice_order_is_preserved_and_never_rearranged():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 5.0, 2.0),
                                    am.AudioMixConfig())
    assert [p.index for p in plan.placements] == [0, 1, 2]
    assert [p.duration for p in plan.placements] == [3.0, 5.0, 2.0]
    assert [p.start for p in plan.placements] == sorted(p.start for p in plan.placements)


def test_start_delay_is_honoured():
    for delay in (0.0, 2.0, 5.0, 12.5):
        plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0),
                                        am.AudioMixConfig(start_delay_seconds=delay))
        assert plan.placements[0].start >= delay - 1e-9


def test_minimum_gap_is_enforced():
    for gap in (0.0, 0.5, 1.0, 3.0):
        plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 2.0, 4.0),
                                        am.AudioMixConfig(min_gap_seconds=gap))
        placed = plan.placements
        for a, b in zip(placed, placed[1:]):
            assert b.start - a.end >= gap - 1e-9


def test_clips_never_overlap_each_other():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 5.0, 2.0, 4.0),
                                    am.AudioMixConfig(min_gap_seconds=0.0))
    for a, b in zip(plan.placements, plan.placements[1:]):
        assert b.start >= a.end


def test_a_clip_never_extends_past_the_music():
    plan = am.plan_voice_placements(30.0, beats(), sections((0.0, 30.0, "intro")),
                                    voices(5.0, 5.0, 5.0), am.AudioMixConfig())
    for placement in plan.placements:
        assert placement.end <= 30.0 + 1e-9


def test_preferred_section_start_wins_inside_the_lookahead_window():
    """The verse start at 60.0 is 1.0 s past the earliest legal beat, so it should win."""
    structure = sections((0.0, 59.0, "drop"), (59.0, 60.0, "chorus"), (60.0, 200.0, "verse"))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(2.0),
                                    am.AudioMixConfig(start_delay_seconds=0.0))
    placement = plan.placements[0]
    assert placement.start == 60.0
    assert placement.anchor == am.ANCHOR_PREFERRED_SECTION
    assert placement.section_type == "verse"


def test_preference_is_bounded_and_never_leaps():
    """A preferred start far beyond the window must NOT be chosen: measured on real material,
    unbounded preference threw clips 27 s and 83 s forward and stranded the track."""
    far = 100.0
    structure = sections((0.0, far, "chorus"), (far, 300.0, "verse"))
    plan = am.plan_voice_placements(300.0, beats(), structure, voices(2.0),
                                    am.AudioMixConfig(start_delay_seconds=2.0))
    placement = plan.placements[0]
    assert placement.anchor == am.ANCHOR_BEAT
    assert placement.start < 2.0 + am.PLACEMENT_LOOKAHEAD_SECONDS + 1e-9
    assert placement.start != far


def test_a_non_preferred_section_start_beats_a_plain_beat():
    structure = sections((0.0, 10.0, "verse"), (10.0, 200.0, "chorus"))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(2.0),
                                    am.AudioMixConfig(start_delay_seconds=8.0))
    placement = plan.placements[0]
    assert placement.start == 10.0
    assert placement.anchor == am.ANCHOR_SECTION


def test_shift_is_reported():
    structure = sections((0.0, 59.0, "drop"), (59.0, 200.0, "verse"))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(2.0),
                                    am.AudioMixConfig(start_delay_seconds=0.0))
    assert plan.placements[0].shift_seconds == pytest.approx(59.0)


# ===========================================================================
# 5. AVOID DROPS — WHOLE INTERVAL
# ===========================================================================


def test_the_whole_spoken_interval_must_avoid_a_drop():
    """The rule that matters: starting just before a drop and talking through it is NOT avoiding
    it. On real material a start-only rule put 11.56 s of a 12 s clip inside the drop."""
    structure = sections((0.0, 50.0, "intro"), (50.0, 80.0, "drop"), (80.0, 200.0, "verse"))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(10.0),
                                    am.AudioMixConfig(start_delay_seconds=45.0))
    placement = plan.placements[0]
    assert not (placement.start < 80.0 and placement.end > 50.0)
    assert placement.start >= 80.0


@pytest.mark.parametrize("section_type", ["drop", "finale"])
def test_both_avoided_types_are_avoided(section_type: str):
    structure = sections((0.0, 10.0, "intro"), (10.0, 40.0, section_type),
                         (40.0, 200.0, "verse"))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(5.0),
                                    am.AudioMixConfig(start_delay_seconds=8.0))
    assert plan.placements[0].start >= 40.0


@pytest.mark.parametrize("section_type", ["hook", "chorus", "bridge", "outro", "body"])
def test_other_energetic_types_are_not_avoided(section_type: str):
    structure = sections((0.0, 10.0, "intro"), (10.0, 200.0, section_type))
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(5.0),
                                    am.AudioMixConfig(start_delay_seconds=12.0))
    assert plan.placements[0].start < 40.0


def test_avoid_drops_off_allows_speech_over_a_drop():
    structure = sections((0.0, 200.0, "drop"),)
    assert isinstance(am.plan_voice_placements(200.0, beats(), structure, voices(5.0),
                                               am.AudioMixConfig(avoid_drops=True)),
                      am.PlacementFailure)
    plan = am.plan_voice_placements(200.0, beats(), structure, voices(5.0),
                                    am.AudioMixConfig(avoid_drops=False))
    assert isinstance(plan, am.AudioMixPlan)
    assert plan.placements[0].start == pytest.approx(2.0)


def test_boundary_touching_is_legal_half_open_intervals():
    """`[start, end)` for both: a clip ending exactly where a drop begins is legal, and so is one
    starting exactly where a drop ends."""
    section = am.MusicSection(10.0, 20.0, "drop")
    assert not section.overlaps(5.0, 10.0)      # ends exactly at the drop's start
    assert not section.overlaps(20.0, 25.0)     # starts exactly at the drop's end
    assert section.overlaps(9.0, 11.0)
    assert section.overlaps(19.0, 21.0)
    assert section.overlaps(12.0, 15.0)


def test_a_clip_may_end_exactly_where_a_drop_begins():
    structure = sections((0.0, 10.0, "intro"), (10.0, 60.0, "drop"), (60.0, 200.0, "verse"))
    plan = am.plan_voice_placements(200.0, beats(0.5), structure, voices(8.0),
                                    am.AudioMixConfig(start_delay_seconds=2.0))
    placement = plan.placements[0]
    assert placement.end <= 10.0 or placement.start >= 60.0
    if placement.end <= 10.0:
        assert placement.end == pytest.approx(10.0)


# ===========================================================================
# 6. FAILURE — NEVER DROP, TRUNCATE OR OVERLAP
# ===========================================================================


def test_no_legal_placement_is_an_explicit_failure():
    structure = sections((0.0, 200.0, "drop"),)
    failure = am.plan_voice_placements(200.0, beats(), structure, voices(5.0),
                                       am.AudioMixConfig())
    assert isinstance(failure, am.PlacementFailure)
    assert failure.index == 0
    assert failure.duration == 5.0
    assert failure.after_seconds == pytest.approx(2.0)
    assert "no legal placement" in failure.reason
    assert "5.0s" in failure.reason


def test_a_clip_longer_than_the_music_fails_rather_than_being_truncated():
    failure = am.plan_voice_placements(10.0, beats(), sections((0.0, 10.0, "intro")),
                                       voices(30.0), am.AudioMixConfig())
    assert isinstance(failure, am.PlacementFailure)
    assert failure.duration == 30.0


def test_failure_reports_the_offending_clip_not_the_first_one():
    structure = sections((0.0, 30.0, "intro"), (30.0, 200.0, "drop"))
    failure = am.plan_voice_placements(200.0, beats(), structure, voices(3.0, 3.0, 50.0),
                                       am.AudioMixConfig())
    assert isinstance(failure, am.PlacementFailure)
    assert failure.index == 2
    assert failure.duration == 50.0
    assert "Voice 3" in failure.reason


def test_a_failure_places_nothing_at_all():
    structure = sections((0.0, 200.0, "drop"),)
    result = am.plan_voice_placements(200.0, beats(), structure, voices(1.0, 1.0),
                                      am.AudioMixConfig())
    assert isinstance(result, am.PlacementFailure)
    assert not hasattr(result, "placements")


# ===========================================================================
# 7. DETERMINISM
# ===========================================================================


def test_placement_is_deterministic_and_seedless():
    args = (200.0, beats(), SIMPLE, voices(3.0, 5.0, 2.0), am.AudioMixConfig())
    first = am.plan_voice_placements(*args)
    for _ in range(5):
        again = am.plan_voice_placements(*args)
        assert [p.start for p in again.placements] == [p.start for p in first.placements]
        assert [p.anchor for p in again.placements] == [p.anchor for p in first.placements]


def test_the_module_imports_nothing_that_could_make_it_nondeterministic():
    """Placement must be a pure function of its inputs: no RNG, no clock, no uuid, no process."""
    import ast
    import copy
    import inspect

    tree = ast.parse(inspect.getsource(am))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "dataclasses", "typing", "beatsync_fork"}

    # executable source only, so the prose above may legitimately say "seedless"
    executable = executable_source(am)
    for forbidden in ("random", "uuid", "time.", "datetime", "subprocess", "ffmpeg",
                      "numpy", "open("):
        assert forbidden not in executable, f"audio_mix references {forbidden!r}"


# ===========================================================================
# 8. DUCK MODEL
# ===========================================================================


def test_one_duck_event_per_placement():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 2.0),
                                    am.AudioMixConfig())
    assert len(plan.duck_events) == len(plan.placements) == 2
    for event, placement in zip(plan.duck_events, plan.placements):
        assert event.voice_start == placement.start
        assert event.voice_end == placement.end
        assert event.attack == am.DUCK_ATTACK_SECONDS
        assert event.release == am.DUCK_RELEASE_SECONDS
        assert event.floor == 0.35


def test_duck_window_includes_the_ramps_and_is_clamped_to_the_music():
    config = am.AudioMixConfig()
    events = am.build_duck_events(
        (am.VoicePlacement(0, "v.wav", 2.0, 5.0, 7.0, "intro", am.ANCHOR_BEAT, 0.0),), 100.0,
        config)
    assert events[0].start == pytest.approx(5.0 - 0.250)
    assert events[0].end == pytest.approx(7.0 + 0.400)

    # a clip at the very start/end has its window clamped, but the ramps are not rescaled
    edge = am.build_duck_events(
        (am.VoicePlacement(0, "v.wav", 2.0, 0.1, 9.9, "intro", am.ANCHOR_BEAT, 0.0),), 10.0,
        config)
    assert edge[0].start == 0.0
    assert edge[0].end == 10.0
    assert edge[0].attack == 0.250 and edge[0].release == 0.400


def _one_event(start: float, end: float, floor: float = FLOOR) -> tuple:
    return (am.DuckEvent(start, end, max(0.0, start - 0.250), end + 0.400,
                         floor, 0.250, 0.400),)


@pytest.mark.parametrize("instant,expected", [
    (0.0, 1.0),
    (4.74, 1.0),            # just before the attack begins
    (4.75, 1.0),            # attack start
    (4.875, 0.675),         # half way down
    (5.0, 0.35),            # voice starts, already at the floor
    (6.0, 0.35),            # body
    (7.0, 0.35),            # voice ends
    (7.2, 0.675),           # half way back
    (7.4, 1.0),             # release complete
    (9.0, 1.0),
])
def test_duck_envelope_boundary_values(instant: float, expected: float):
    assert am.duck_gain_at(_one_event(5.0, 7.0), instant) == pytest.approx(expected)


def test_the_music_is_already_at_the_floor_when_the_first_syllable_lands():
    """The point of ducking *before* the voice start rather than at it."""
    assert am.duck_gain_at(_one_event(5.0, 7.0), 5.0) == pytest.approx(FLOOR)
    assert am.duck_gain_at(_one_event(5.0, 7.0), 5.0 - 1e-6) == pytest.approx(FLOOR, abs=1e-5)


@pytest.mark.parametrize("floor", [0.0, 0.35, 0.5, 1.0])
def test_floor_is_respected_exactly(floor: float):
    events = _one_event(5.0, 7.0, floor)
    assert am.duck_gain_at(events, 6.0) == pytest.approx(floor)
    assert am.duck_gain_at(events, 0.0) == pytest.approx(1.0)


def test_no_events_means_no_ducking():
    assert am.duck_gain_at((), 5.0) == 1.0


def test_overlapping_events_use_the_minimum_gain():
    """A gap below attack+release must leave the music continuously ducked rather than letting it
    bounce back up between clips."""
    gap = 0.2
    assert gap < am.DUCK_ATTACK_SECONDS + am.DUCK_RELEASE_SECONDS
    events = _one_event(5.0, 6.0) + _one_event(6.0 + gap, 7.2)
    between = [6.0 + 0.01 * k for k in range(int(gap * 100) + 1)]
    gains = [am.duck_gain_at(events, t) for t in between]
    assert max(gains) < 1.0
    assert all(g <= 1.0 - 1e-9 for g in gains)
    # and both bodies are still exactly at the floor
    assert am.duck_gain_at(events, 5.5) == pytest.approx(FLOOR)
    assert am.duck_gain_at(events, 6.8) == pytest.approx(FLOOR)


def test_overlap_result_is_independent_of_event_order():
    a = _one_event(5.0, 6.0)[0]
    b = _one_event(6.1, 7.0)[0]
    for instant in [5.0 + 0.05 * k for k in range(60)]:
        assert am.duck_gain_at((a, b), instant) == pytest.approx(am.duck_gain_at((b, a), instant))


def test_deeply_nested_overlap_takes_the_deepest_duck():
    events = _one_event(5.0, 10.0) + _one_event(6.0, 7.0) + _one_event(8.0, 9.0)
    for instant in (5.5, 6.5, 7.5, 8.5, 9.5):
        assert am.duck_gain_at(events, instant) == pytest.approx(FLOOR)


# ===========================================================================
# 9. REPORTING
# ===========================================================================


def test_summary_line_only_describes_what_happened():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 2.0, 4.0, 1.0),
                                    am.AudioMixConfig())
    assert plan.summary_line() == "Audio Layers: 4 voice clips · music 35% · 4 duck events"
    single = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0), am.AudioMixConfig())
    assert single.summary_line() == "Audio Layers: 1 voice clip · music 35% · 1 duck event"


def test_report_lines_show_resolved_order_and_anchors():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0, 2.0),
                                    am.AudioMixConfig())
    text = "\n".join(plan.report_lines())
    assert "Music + 2 voice clips" in text
    assert "Music under voice: 35%" in text
    assert "Attack / release: 250 / 400 ms" in text
    assert "1 · 00_clip.wav" in text
    assert "2 · 01_clip.wav" in text


def test_report_never_contains_an_ffmpeg_filtergraph():
    plan = am.plan_voice_placements(200.0, beats(), SIMPLE, voices(3.0), am.AudioMixConfig())
    text = "\n".join(plan.report_lines()).lower()
    for token in ("amix", "adelay", "aresample", "alimiter", "filter_complex", "volume="):
        assert token not in text


# ===========================================================================
# 10. ISOLATION
# ===========================================================================


def test_no_creative_or_variant_lab_concept_leaks_in():
    """Audio Layers is a separate audio concern; E2 is where Variant Lab may later reach it.

    Executable source only — the module docstring says "AudioMixConfig, not CreativeRecipe" on
    purpose, and prose stating a boundary must not be read as crossing it.
    """
    source = executable_source(am)
    for word in ("creativeprofile", "creativerecipe", "variantlab", "rng_for", "master_seed",
                 "variation_seed", "matching_preset"):
        assert word not in source, f"audio_mix references {word!r}"


def test_audio_config_is_not_a_creative_profile_field():
    from beatsync_fork import creative as fork_creative
    profile = fork_creative.CreativeProfile()
    for field in ("voice_files", "audio_mix", "music_under_voice", "start_delay_seconds"):
        assert not hasattr(profile, field)
        assert field not in profile.as_dict()
