"""The resolved Creative Profile (B0 + Creative Controls Core), on a bare interpreter.

``beatsync_fork.creative`` is stdlib-only by rule, so everything here runs with nothing but pytest:
no numpy, no Gradio, no CUDA, no FFmpeg. That is deliberate — the profile is the object every other
layer trusts, and its normalisation must be provable without any of the machinery it feeds.

The load-bearing contract is the same one Phase A established for the seed and now extends to three
controls: **50 is today**. ``seed=0, cut_density=50, energy_response=50, motion_bias=50`` is current
main, and every neutrality helper here exists so the stages downstream can take an explicit legacy
branch rather than running new arithmetic with neutral coefficients.
"""

from __future__ import annotations

import math

import pytest

from beatsync_fork import creative, variation


# ---------------------------------------------------------------------------
# normalize_control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        # accepted whole values, in range
        (0, 0), (1, 1), (50, 50), (99, 99), (100, 100),
        # accepted whole floats
        (0.0, 0), (50.0, 50), (100.0, 100), (-0.0, 0),
        # accepted plain decimal strings, including a negative one (which then clamps)
        ("0", 0), ("50", 50), ("100", 100), ("  65  ", 65), ("-10", 0), ("250", 100),
        # clamped rather than rejected: a range has meaningful ends
        (120, 100), (10_000, 100), (-10, 0), (-9999, 0), (120.0, 100), (-10.0, 0),
        # fractional values are NOT floored: 50.5 is not a request for 50
        (50.5, 50), (0.5, 50), (99.9, 50), (-0.5, 50), (100.5, 50),
        # bool subclasses int, so True would otherwise read as "1" — nearly maximally sparse
        (True, 50), (False, 50),
        # malformed / absent
        (None, 50), ("", 50), (" ", 50), ("abc", 50), ("50.0", 50), ("5e1", 50),
        ([], 50), ({}, 50), (object(), 50),
        (float("nan"), 50), (float("inf"), 50), (float("-inf"), 50),
    ],
)
def test_normalize_control_clamps_whole_values_and_refuses_to_guess(value, expected):
    assert creative.normalize_control(value) == expected


def test_normalize_control_never_raises_on_anything_a_widget_can_produce():
    """A render must not die because a number box was in a strange state."""
    for value in (None, "", "?", -1, 1e308, -1e308, float("nan"), True, [], {}, (), 3.5, "０"):
        assert creative.CONTROL_MIN <= creative.normalize_control(value) <= creative.CONTROL_MAX


def test_the_neutral_value_is_the_midpoint_of_the_declared_range():
    assert creative.CONTROL_MIN == 0
    assert creative.CONTROL_MAX == 100
    assert creative.DEFAULT_CONTROL == 50


# ---------------------------------------------------------------------------
# Construction and defaults
# ---------------------------------------------------------------------------


def test_the_default_profile_is_exactly_neutral():
    profile = creative.CreativeProfile()

    assert profile.seed == 0
    assert profile.cut_density == 50
    assert profile.energy_response == 50
    assert profile.motion_bias == 50
    assert profile.is_neutral()
    assert profile.is_neutral_cuts()
    assert profile.is_neutral_scoring()
    assert creative.NEUTRAL_PROFILE == profile


def test_every_construction_route_normalises():
    """There is no such thing as a half-trusted profile: raw values may enter by any door."""
    direct = creative.CreativeProfile(seed=7.9, cut_density=120, energy_response=True,
                                      motion_bias="-5")
    widgets = creative.CreativeProfile.from_widgets(seed=7.9, cut_density=120,
                                                    energy_response=True, motion_bias="-5")
    mapping = creative.CreativeProfile.from_mapping(
        {"seed": 7.9, "cut_density": 120, "energy_response": True, "motion_bias": "-5"})

    for profile in (direct, widgets, mapping):
        assert profile.seed == 0          # 7.9 is not a request for seed 7
        assert profile.cut_density == 100  # clamped
        assert profile.energy_response == 50  # bool refused
        assert profile.motion_bias == 0    # clamped
    assert direct == widgets == mapping


def test_profiles_are_immutable():
    profile = creative.CreativeProfile(seed=101)
    with pytest.raises(Exception):
        profile.cut_density = 90  # type: ignore[misc]


def test_from_widgets_treats_none_as_neutral_everywhere():
    assert creative.CreativeProfile.from_widgets() == creative.NEUTRAL_PROFILE
    assert creative.CreativeProfile.from_widgets(
        seed=None, cut_density=None, energy_response=None, motion_bias=None
    ) == creative.NEUTRAL_PROFILE


@pytest.mark.parametrize("value", [None, "not a dict", 7, [], (), object()])
def test_from_mapping_survives_a_bus_it_did_not_write(value):
    assert creative.CreativeProfile.from_mapping(value) == creative.NEUTRAL_PROFILE


def test_a_phase_a_seed_only_dict_still_resolves():
    """The bus used to carry ``{"seed": n}``. Reading one back must keep the seed and stay neutral
    everywhere else, so nothing in flight or recorded from an older build changes meaning."""
    profile = creative.CreativeProfile.from_mapping({"seed": 381944})

    assert profile.seed == 381944
    assert profile.is_neutral_cuts() and profile.is_neutral_scoring()
    assert not profile.is_neutral()


def test_unknown_keys_on_the_bus_are_ignored():
    profile = creative.CreativeProfile.from_mapping(
        {"seed": 5, "cut_density": 70, "micro_cuts": 90, "director": "freestyle"})

    assert profile == creative.CreativeProfile(seed=5, cut_density=70)


# ---------------------------------------------------------------------------
# Seed authority stays in variation.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [0, -1, None, "", "abc", 7, 7.0, 7.9, "7", " 7 ", True, False, 381944,
     float("nan"), float("inf"), -7.0, 0.5],
)
def test_seed_handling_is_delegated_not_reimplemented(value):
    """The one seed authority is `beatsync_fork.variation`; this profile must agree with it for
    every input, or seed 0's frozen legacy meaning would have two definitions."""
    assert creative.CreativeProfile(seed=value).seed == variation.normalize_seed(value)


@pytest.mark.parametrize("seed, expected", [(0, ""), (-4, ""), (None, ""), (381944, "_seed381944")])
def test_filename_behaviour_is_the_seed_rule_and_only_the_seed_rule(seed, expected):
    """The three new controls contribute nothing to a filename — today's names must not change."""
    for density in (0, 50, 100):
        for energy in (0, 50, 100):
            profile = creative.CreativeProfile(
                seed=seed, cut_density=density, energy_response=energy, motion_bias=100 - density)
            assert profile.filename_suffix() == expected
            assert profile.filename_suffix() == variation.filename_suffix(seed)


def test_describe_seed_is_the_variation_wording():
    assert creative.CreativeProfile().describe_seed() == "legacy"
    assert creative.CreativeProfile(seed=381944).describe_seed() == "seed 381944"


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_as_dict_round_trips_exactly():
    profile = creative.CreativeProfile(seed=101, cut_density=70, energy_response=80, motion_bias=30)
    payload = profile.as_dict()

    assert payload == {"seed": 101, "cut_density": 70, "energy_response": 80, "motion_bias": 30}
    assert all(isinstance(value, int) for value in payload.values())
    assert creative.CreativeProfile.from_mapping(payload) == profile


def test_as_dict_hands_out_a_fresh_container_each_time():
    """It rides on the shared `beat_info` bus and lands in `render_info`; a shared dict would let a
    downstream reader mutate what another reader sees."""
    profile = creative.CreativeProfile(seed=5)
    first, second = profile.as_dict(), profile.as_dict()

    assert first == second and first is not second
    first["seed"] = 999
    assert profile.as_dict()["seed"] == 5


# ---------------------------------------------------------------------------
# Neutrality helpers — the thing every legacy branch keys off
# ---------------------------------------------------------------------------


def test_is_neutral_cuts_tracks_only_cut_density():
    assert creative.CreativeProfile(seed=101, energy_response=0, motion_bias=100).is_neutral_cuts()
    assert not creative.CreativeProfile(cut_density=49).is_neutral_cuts()
    assert not creative.CreativeProfile(cut_density=51).is_neutral_cuts()


def test_is_neutral_scoring_tracks_only_the_two_stage_6_controls():
    assert creative.CreativeProfile(seed=101, cut_density=0).is_neutral_scoring()
    assert not creative.CreativeProfile(energy_response=49).is_neutral_scoring()
    assert not creative.CreativeProfile(motion_bias=51).is_neutral_scoring()


def test_is_neutral_requires_the_seed_too():
    assert not creative.CreativeProfile(seed=1).is_neutral()
    assert creative.CreativeProfile(seed=0).is_neutral()


# ---------------------------------------------------------------------------
# The measured mappings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("density, expected", [(0, 0.5), (25, 2 ** -0.5), (50, 1.0),
                                               (75, 2 ** 0.5), (100, 2.0)])
def test_cut_density_factor_is_the_accepted_exponential_mapping(density, expected):
    assert creative.CreativeProfile(cut_density=density).cut_density_factor() == pytest.approx(expected)


def test_cut_density_factor_is_exactly_one_at_neutral():
    """Exactly, not approximately — the neutral branch is chosen on `is_neutral_cuts()`, but a
    factor that were not exactly 1.0 would mean the mapping itself had drifted off centre."""
    assert creative.CreativeProfile(cut_density=50).cut_density_factor() == 1.0


def test_cut_density_factor_is_strictly_monotonic():
    factors = [creative.CreativeProfile(cut_density=d).cut_density_factor() for d in range(0, 101)]
    assert all(a < b for a, b in zip(factors, factors[1:]))
    assert all(math.isfinite(f) and f > 0 for f in factors)


@pytest.mark.parametrize("response, expected", [(0, 0.40), (25, 0.70), (50, 1.00),
                                                (75, 1.30), (100, 1.60)])
def test_energy_factor_is_the_accepted_linear_mapping(response, expected):
    assert creative.CreativeProfile(energy_response=response).energy_factor() == pytest.approx(expected)


def test_energy_factor_is_exactly_one_at_neutral():
    assert creative.CreativeProfile(energy_response=50).energy_factor() == 1.0


@pytest.mark.parametrize("bias, expected", [(0, -1.0), (25, -0.5), (50, 0.0), (75, 0.5), (100, 1.0)])
def test_motion_centered_is_symmetric_about_neutral(bias, expected):
    assert creative.CreativeProfile(motion_bias=bias).motion_centered() == pytest.approx(expected)


def test_motion_centered_is_exactly_zero_at_neutral():
    assert creative.CreativeProfile(motion_bias=50).motion_centered() == 0.0


def test_the_motion_coefficient_sits_between_the_two_planner_constants_it_must_respect():
    """0.15 was chosen to be visible against the seeded selector's window and powerless against the
    planner's anti-repeat penalty. Both comparisons are read from the real constants, so retuning
    either of those without revisiting this one fails here."""
    assert creative.MOTION_BIAS_COEFFICIENT > variation.SCORE_WINDOW / 2
    assert creative.MOTION_BIAS_COEFFICIENT < 0.28  # the planner's "seen recently" penalty


# ---------------------------------------------------------------------------
# ScoringControls
# ---------------------------------------------------------------------------


def test_neutral_scoring_controls_are_all_none():
    controls = creative.CreativeProfile().scoring_controls()

    assert controls is creative.NEUTRAL_SCORING
    assert controls.energy_factor is None
    assert controls.motion_centered is None
    assert controls.is_neutral
    assert not controls.needs_flow_column


def test_each_scoring_control_is_decided_independently():
    """Setting Energy Response must not make Motion Bias cost anything, and vice versa."""
    energy_only = creative.CreativeProfile(energy_response=100).scoring_controls()
    assert energy_only.energy_factor == pytest.approx(1.6)
    assert energy_only.motion_centered is None
    assert energy_only.needs_flow_column

    motion_only = creative.CreativeProfile(motion_bias=0).scoring_controls()
    assert motion_only.energy_factor is None
    assert motion_only.motion_centered == pytest.approx(-1.0)
    assert not motion_only.needs_flow_column, "Motion Bias needs no flow column"

    both = creative.CreativeProfile(energy_response=0, motion_bias=100).scoring_controls()
    assert both.energy_factor == pytest.approx(0.4)
    assert both.motion_centered == pytest.approx(1.0)
    assert not both.is_neutral


def test_the_seed_never_reaches_the_scoring_controls():
    """Controls modulate scoring; the seed decides the winner afterwards. Mixing them would make a
    seed change what "good" means, which is exactly the separation Phase A established."""
    assert (creative.CreativeProfile(seed=381944).scoring_controls()
            == creative.CreativeProfile(seed=0).scoring_controls())


def test_cut_density_never_reaches_the_scoring_controls():
    """Cut Density is a Stage 4 control. If it leaked into Stage 6 scoring it would silently make
    the two stages' ownership untrue."""
    assert (creative.CreativeProfile(cut_density=0).scoring_controls()
            == creative.CreativeProfile(cut_density=100).scoring_controls()
            == creative.NEUTRAL_SCORING)


# ---------------------------------------------------------------------------
# Stage-4 quantisation helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step, factor, expected", [
    # neutral factor leaves every step alone
    (1, 1.0, 1), (2, 1.0, 2), (3, 1.0, 3), (4, 1.0, 4), (8, 1.0, 8),
    # denser: steps shrink, floored at 1
    (4, 2.0, 2), (3, 2.0, 2), (2, 2.0, 1), (1, 2.0, 1),
    # sparser: steps grow, capped at 8
    (4, 0.5, 8), (3, 0.5, 6), (2, 0.5, 4), (1, 0.5, 2), (8, 0.5, 8),
])
def test_scale_beat_step_stays_on_the_grid_and_inside_the_selector_s_range(step, factor, expected):
    assert creative.scale_beat_step(step, factor) == expected


def test_scale_beat_step_rounds_half_up_not_to_even():
    """`round()` is banker's rounding: it would send both 1.5 and 2.5 to 2, flattening two different
    musical situations onto one spacing. The mapping is explicitly half-up instead."""
    assert creative.scale_beat_step(3, 2.0) == 2      # 1.5 -> 2, not 2
    assert creative.scale_beat_step(5, 2.0) == 3      # 2.5 -> 3, where round() gives 2
    assert round(2.5) == 2, "sanity: Python really does round half to even"


def test_scale_beat_step_result_is_always_a_usable_step():
    for step in range(1, 9):
        for density in range(0, 101):
            factor = creative.CreativeProfile(cut_density=density).cut_density_factor()
            scaled = creative.scale_beat_step(step, factor)
            assert isinstance(scaled, int)
            assert creative.BEAT_STEP_MIN <= scaled <= creative.BEAT_STEP_MAX


def test_the_weak_score_threshold_scales_inversely_with_density():
    assert creative.scale_weak_score_threshold(1.0) == creative.WEAK_SCORE_THRESHOLD
    assert creative.scale_weak_score_threshold(2.0) == pytest.approx(0.21)
    assert creative.scale_weak_score_threshold(0.5) == pytest.approx(0.84)
    assert creative.WEAK_SCORE_THRESHOLD == 0.42, "Stage 4's existing constant, unchanged"


# ---------------------------------------------------------------------------
# describe()
# ---------------------------------------------------------------------------


def test_describe_reports_legacy_for_a_neutral_render():
    """A default render's console output must read exactly as it does today."""
    assert creative.CreativeProfile().describe() == "legacy"


def test_describe_lists_every_control_once_anything_is_set():
    text = creative.CreativeProfile(seed=101, cut_density=65, energy_response=80,
                                    motion_bias=40).describe()

    assert text == "Seed 101 · Cut Density 65 · Energy Response 80 · Motion Bias 40"


def test_describe_still_names_a_default_seed_when_only_a_control_moved():
    text = creative.CreativeProfile(cut_density=65).describe()

    assert text == "Seed default · Cut Density 65 · Energy Response 50 · Motion Bias 50"


def test_describe_is_enough_to_reproduce_the_render():
    """The whole point of reporting it: every number the profile holds must be readable back."""
    profile = creative.CreativeProfile(seed=7, cut_density=0, energy_response=100, motion_bias=0)
    text = profile.describe()

    for value in profile.as_dict().values():
        assert str(value) in text
