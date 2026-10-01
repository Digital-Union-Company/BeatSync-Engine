"""Smart Mix / SFX Pool V1 (E): the pure planner.

Everything here runs on a bare interpreter — `beatsync_fork.smart_mix` is stdlib-only by rule, so
the whole role vocabulary, the Amount mapping, the percentile, all five placement rules and the
cross-role occupancy policy are testable without FFmpeg, numpy, Gradio or a GPU.

Three properties are worth more than the rest, and each has a test that would be hard to satisfy
accidentally:

1. **The impact percentile population is the WHOLE aligned beat array**, with the bar-anchor mask
   applied afterwards. Restricting the population first silently shifts every threshold.
2. **Non-atmosphere SFX never overlap**, resolved by priority rather than by nudging an anchor.
3. **The asset cursor advances on every candidate ATTEMPT**, so one unusable asset cannot
   permanently block the rest of its pool.
"""

from __future__ import annotations

import math

import pytest

from beatsync_fork import smart_mix as sm


# ===========================================================================
# helpers — a synthetic track shaped like the accepted calibration material
# ===========================================================================


def structure(sections, beats=None, bar=None, phrase=None, impact=None, duration=None):
    """Build a `MusicStructure` directly, bypassing `project_structure`'s mapping shape."""
    beats = tuple(beats if beats is not None else [i * 0.5 for i in range(40)])
    n = len(beats)
    return sm.MusicStructure(
        music_duration=duration if duration is not None else (max(beats) + 1.0),
        beat_times=beats,
        is_bar_anchor=tuple(bar if bar is not None else [True] * n),
        is_phrase_anchor=tuple(phrase if phrase is not None else [False] * n),
        impact_strength=tuple(impact if impact is not None else [1.0] * n),
        sections=tuple(sections),
    )


def asset(role, duration, name=None):
    return sm.SfxAsset(role=role, path=f"C:/sfx/{role}/{name or duration}.wav", duration=duration)


NERO_SECTIONS = (
    (0.0, 29.002, "intro"), (29.002, 53.267, "intro"), (53.267, 64.435, "drop"),
    (64.435, 74.606, "drop"), (74.606, 88.143, "chorus"), (88.143, 103.631, "drop"),
    (103.631, 115.217, "drop"), (115.217, 150.094, "breakdown"), (150.094, 171.340, "verse"),
    (171.340, 204.266, "breakdown"), (204.266, 239.119, "drop"), (239.119, 254.607, "finale"),
    (254.607, 278.021, "finale"),
)


# ===========================================================================
# 1. ROLE VOCABULARY AND THE ALIAS TABLE
# ===========================================================================


def test_the_role_vocabulary_is_exactly_five():
    assert sm.ALL_ROLES == frozenset(
        {"impact", "riser", "atmosphere", "transition", "vocal_shot"})
    assert set(sm.ROLE_ORDER) == sm.ALL_ROLES
    assert set(sm.ROLE_PRIORITY) == sm.ALL_ROLES
    assert len(sm.ROLE_ORDER) == len(sm.ROLE_PRIORITY) == 5


def test_the_planning_priority_is_frozen():
    assert sm.ROLE_PRIORITY == ("riser", "impact", "transition", "vocal_shot", "atmosphere")


@pytest.mark.parametrize("folder,role", [
    ("impact", "impact"), ("impacts", "impact"), ("Impacts", "impact"), ("IMPACTS", "impact"),
    ("riser", "riser"), ("risers", "riser"), ("Risers", "riser"),
    ("atmosphere", "atmosphere"), ("atmospheres", "atmosphere"), ("ambience", "atmosphere"),
    ("Ambience", "atmosphere"),
    ("transition", "transition"), ("transitions", "transition"),
    ("vocalshot", "vocal_shot"), ("vocalshots", "vocal_shot"), ("vocal_shot", "vocal_shot"),
    ("vocal_shots", "vocal_shot"), ("vocal shot", "vocal_shot"), ("vocal shots", "vocal_shot"),
    ("VocalShots", "vocal_shot"), ("Vocal Shots", "vocal_shot"),
])
def test_every_frozen_alias_resolves(folder, role):
    assert sm.role_for_folder(folder) == role


@pytest.mark.parametrize("folder", [
    "impactful", "impact_hits", "my impacts", "riser2", "atmos", "ambiences", "transitioning",
    "vocal", "vocals", "shots", "vocal-shot", "vocal.shots", "", "   ", None, 7, b"impacts",
])
def test_no_fuzzy_match_is_accepted(folder):
    """Exact whole-component matches only — no contains, startswith or punctuation rewriting."""
    assert sm.role_for_folder(folder) is None


def test_the_alias_table_has_no_surprises():
    assert set(sm.ROLE_ALIASES.values()) == sm.ALL_ROLES
    assert all(key == key.casefold() for key in sm.ROLE_ALIASES)


def test_role_choices_carry_the_exact_internal_value():
    """The GUI must never re-derive a role from its display label."""
    assert [role for _label, role in sm.ROLE_CHOICES] == list(sm.ROLE_ORDER)
    for label, role in sm.ROLE_CHOICES:
        assert isinstance(label, str) and label
        assert role in sm.ALL_ROLES
        # a lowercase/replace heuristic over the label would NOT produce the value
    assert dict(sm.ROLE_CHOICES)["Vocal shots"] == "vocal_shot"


def test_supported_extensions_match_audio_layers():
    assert sm.SUPPORTED_SFX_EXTENSIONS == (".mp3", ".wav", ".flac")
    assert sm.has_supported_sfx_extension("a/b/c.WAV")
    assert sm.has_supported_sfx_extension("x.flac")
    assert not sm.has_supported_sfx_extension("x.m4a")
    assert not sm.has_supported_sfx_extension("x.ogg")
    assert not sm.has_supported_sfx_extension(None)


def test_pool_order_is_the_forks_total_order_not_the_filesystems():
    given = ["C:/s/10_b.wav", "C:/s/2_a.wav", "C:/s/1_c.wav"]
    once = sm.order_sfx_paths(given)
    twice = sm.order_sfx_paths(list(reversed(given)))
    assert once == twice, "ordering must not depend on the order it was handed"
    assert sm.order_sfx_paths("not-a-list") == ()
    assert sm.order_sfx_paths(None) == ()
    assert sm.order_sfx_paths(["ok.wav", None, 7, ""]) == ("ok.wav",)


# ===========================================================================
# 2. CONFIG NORMALISATION
# ===========================================================================


def test_config_defaults():
    config = sm.SmartMixConfig()
    assert config.enabled_roles == sm.ALL_ROLES
    assert config.amount == 50
    assert config.sfx_level_percent == 50
    assert config.sfx_gain == 0.5
    assert config.plans_anything


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (50, 50), (100, 100),
    (-10, 0), (120, 100),          # clamped
    (50.0, 50), (100.0, 100),      # whole floats accepted
    (50.5, 50), (0.5, 50),         # fractional -> default, never floored
    (True, 50), (False, 50),       # bool rejected first (it subclasses int)
    (float("nan"), 50), (float("inf"), 50), (float("-inf"), 50),
    ("50", 50), (None, 50), ([], 50), (object(), 50),
])
def test_control_normalisation_is_total(value, expected):
    assert sm.normalize_control(value) == expected
    assert sm.SmartMixConfig(amount=value).amount == expected
    assert sm.SmartMixConfig(sfx_level_percent=value).sfx_level_percent == expected


def test_sfx_level_is_linear_gain_not_decibels():
    assert sm.SmartMixConfig(sfx_level_percent=50).sfx_gain == 0.50
    assert sm.SmartMixConfig(sfx_level_percent=0).sfx_gain == 0.0
    assert sm.SmartMixConfig(sfx_level_percent=100).sfx_gain == 1.0


def test_role_normalisation():
    assert sm.normalize_roles(["impact", "riser"]) == frozenset({"impact", "riser"})
    assert sm.normalize_roles(["impact", "bogus", 7, None]) == frozenset({"impact"})
    assert sm.normalize_roles([]) == frozenset()                 # explicit "none"
    assert sm.normalize_roles(sm.ALL_ROLES) == sm.ALL_ROLES
    # malformed / non-iterable is NOT the same as empty
    assert sm.normalize_roles(None) == sm.ALL_ROLES
    assert sm.normalize_roles(7) == sm.ALL_ROLES
    assert sm.normalize_roles("impact") == sm.ALL_ROLES          # a bare string is not a selection


def test_amount_zero_and_empty_roles_are_both_off():
    assert not sm.SmartMixConfig(amount=0).plans_anything
    assert not sm.SmartMixConfig(enabled_roles=[]).plans_anything
    assert sm.SmartMixConfig(amount=1).plans_anything


def test_amount_zero_resolves_zero_placements_whatever_the_library():
    plan = sm.plan_sfx(
        structure(NERO_SECTIONS, duration=278.021),
        [asset("impact", 0.3), asset("riser", 3.0), asset("atmosphere", 40.0)],
        sm.SmartMixConfig(amount=0))
    assert plan.total == 0
    assert plan.placements == ()
    assert plan.skips == ()


# ===========================================================================
# 3. THE AMOUNT MAPPING
# ===========================================================================


#: The frozen R0 rows, written as literals so a knot edit fails a test rather than quietly
#: producing different-but-plausible placements.
AMOUNT_GOLDEN = {
    1:   (96.00, 6.00, 30.00, 0, 0),
    25:  (96.00, 6.00, 30.00, 4, 2),
    37:  (95.04, 5.04, 25.20, 5, 2),
    50:  (94.00, 4.00, 20.00, 6, 3),
    62:  (92.08, 3.52, 16.16, 7, 3),
    75:  (90.00, 3.00, 12.00, 8, 4),
    91:  (88.72, 3.00, 12.00, 8, 4),
    100: (88.00, 3.00, 12.00, 8, 4),
}


@pytest.mark.parametrize("amount,expected", sorted(AMOUNT_GOLDEN.items()))
def test_amount_golden_rows(amount, expected):
    params = sm.amount_params(amount)
    assert round(params.impact_percentile, 2) == expected[0]
    assert round(params.impact_min_gap_seconds, 2) == expected[1]
    assert round(params.transition_min_gap_seconds, 2) == expected[2]
    assert params.transition_cap == expected[3]
    assert params.vocal_shot_cap == expected[4]


def test_the_mapping_is_total_over_every_integer():
    for amount in range(0, 101):
        params = sm.amount_params(amount)
        assert isinstance(params.transition_cap, int)
        assert isinstance(params.vocal_shot_cap, int)
        assert 88.0 <= params.impact_percentile <= 96.0
        assert 3.0 <= params.impact_min_gap_seconds <= 6.0
        assert 12.0 <= params.transition_min_gap_seconds <= 30.0


def test_the_mapping_is_monotone():
    previous = None
    for amount in range(1, 101):
        params = sm.amount_params(amount)
        current = (-params.impact_percentile, -params.impact_min_gap_seconds,
                   -params.transition_min_gap_seconds, params.transition_cap,
                   params.vocal_shot_cap)
        if previous is not None:
            assert all(c >= p - 1e-12 for c, p in zip(current, previous)), amount
        previous = current


def test_continuous_quantities_clamp_below_the_lowest_measured_knot():
    """Amount 1..25 share the gentlest *measured* setting; the planner never extrapolates."""
    for amount in range(1, 26):
        params = sm.amount_params(amount)
        assert params.impact_percentile == 96.0
        assert params.impact_min_gap_seconds == 6.0
        assert params.transition_min_gap_seconds == 30.0


def test_caps_interpolate_all_the_way_down_to_zero():
    assert sm.amount_params(1).transition_cap == 0
    assert sm.amount_params(1).vocal_shot_cap == 0
    assert sm.amount_params(13).transition_cap == 2


def test_half_up_is_used_and_is_not_bankers_rounding():
    assert sm.half_up(0.5) == 1
    assert sm.half_up(1.5) == 2
    assert sm.half_up(2.5) == 3            # round() would give 2
    assert sm.half_up(3.5) == 4            # round() would give 4 — the other half of the pair
    assert sm.half_up(2.49) == 2
    assert sm.half_up(0.0) == 0


def test_no_integer_amount_lands_on_a_half_boundary_today():
    """Half-up is specified for safety; on the frozen knots it is never actually exercised, which
    is exactly why it must stay written down — a future knot edit must not silently flip a cap."""
    for amount in range(0, 101):
        for knots in ({0: 0, 25: 4, 50: 6, 75: 8, 100: 8}, {0: 0, 25: 2, 50: 3, 75: 4, 100: 4}):
            value = sm._interpolate(knots, amount)
            assert abs(value - math.floor(value) - 0.5) > 1e-9, (amount, value)


# ===========================================================================
# 4. THE PERCENTILE
# ===========================================================================


def test_percentile_golden_vectors():
    assert sm.percentile([], 50) == 0.0
    assert sm.percentile([7.0], 0) == 7.0
    assert sm.percentile([7.0], 99) == 7.0
    assert sm.percentile([0, 1], 0) == 0.0
    assert sm.percentile([0, 1], 50) == 0.5
    assert sm.percentile([0, 1], 100) == 1.0
    assert sm.percentile([1, 2, 3, 4], 25) == 1.75
    assert sm.percentile([1, 2, 3, 4], 0) == 1.0
    assert sm.percentile([1, 2, 3, 4], 100) == 4.0


def test_percentile_hits_exact_indices():
    data = [0.0, 10.0, 20.0, 30.0, 40.0]
    for p, expected in ((0, 0.0), (25, 10.0), (50, 20.0), (75, 30.0), (100, 40.0)):
        assert sm.percentile(data, p) == expected


def test_percentile_sorts_its_input():
    assert sm.percentile([4, 1, 3, 2], 25) == 1.75


def test_percentile_excludes_nan_inf_and_bool():
    assert sm.percentile([1.0, float("nan"), 2.0, float("inf"), 3.0, 4.0], 25) == 1.75
    # bool is excluded from the population rather than counted as 0/1
    assert sm.percentile([True, False], 50) == 0.0
    assert sm.percentile(["x", None, 1.0], 50) == 1.0


def test_the_impact_population_is_the_whole_array_not_only_bar_anchors():
    """The threshold is computed over EVERY beat; the bar mask is applied afterwards.

    Here only one beat is a bar anchor and the rest are low. If the percentile were taken over the
    bar anchors alone, p90 of a single value would be that value and the beat would qualify. Over
    the whole array, p90 sits far above it and nothing is placed.
    """
    beats = tuple(i * 1.0 for i in range(20))
    impact = tuple([0.2] + [0.9] * 19)
    bar = tuple([True] + [False] * 19)
    st = structure([(0.0, 25.0, "verse")], beats=beats, bar=bar,
                   impact=impact, duration=25.0)
    plan = sm.plan_sfx(st, [asset("impact", 0.3)],
                       sm.SmartMixConfig(enabled_roles=["impact"], amount=75))
    assert plan.count_for("impact") == 0, "a bar-anchor-only population would have placed this"
    assert sm.percentile(impact, 90) > 0.2


# ===========================================================================
# 5. ROLE RULES
# ===========================================================================


def test_riser_targets_only_genuine_drop_entries():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    ends = [round(p.end, 3) for p in plan.placements]
    # 53.267 / 88.143 / 204.266 are entries; 64.435 and 103.631 are drop->drop continuations
    assert ends == [53.267, 88.143, 204.266]
    for placement in plan.placements:
        assert placement.play_duration == placement.source_duration
        assert placement.trimmed is False
        assert round(placement.end - placement.start, 6) == 3.0


def test_a_first_section_drop_is_a_valid_target_when_it_fits():
    st = structure([(0.0, 10.0, "intro"), (10.0, 40.0, "drop")], duration=40.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert [round(p.start, 3) for p in plan.placements] == [7.0]


def test_a_riser_that_cannot_fit_is_skipped_never_truncated():
    st = structure([(0.0, 2.0, "intro"), (2.0, 40.0, "drop"),
                    (40.0, 60.0, "breakdown"), (60.0, 100.0, "drop")], duration=100.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert [round(p.end, 3) for p in plan.placements] == [60.0]
    reasons = [s.reason for s in plan.skips if s.role == "riser"]
    assert any("does not fit" in r for r in reasons)
    assert all(p.trimmed is False for p in plan.placements)


def test_no_drops_means_zero_risers_and_no_failure():
    st = structure([(0.0, 40.0, "intro"), (40.0, 90.0, "verse"), (90.0, 150.0, "chorus")],
                   duration=150.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert plan.count_for("riser") == 0
    assert plan.total == 0


def test_transitions_fire_only_on_a_type_change():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("transition", 1.0)],
                       sm.SmartMixConfig(enabled_roles=["transition"], amount=75))
    starts = [round(p.start, 3) for p in plan.placements]
    # 29.002 (intro->intro), 64.435 and 103.631 (drop->drop), 254.607 (finale->finale) excluded
    assert 29.002 not in starts and 64.435 not in starts
    assert 103.631 not in starts and 254.607 not in starts
    assert starts[0] == 53.267
    for placement in plan.placements:
        assert placement.anchor == sm.ANCHOR_SECTION_CHANGE
        assert placement.trimmed is False


def test_a_dense_boundary_track_cannot_explode():
    sections = []
    for i in range(24):
        sections.append((i * 5.0, (i + 1) * 5.0, "verse" if i % 2 else "chorus"))
    st = structure(sections, duration=120.0)
    plan = sm.plan_sfx(st, [asset("transition", 0.5)],
                       sm.SmartMixConfig(enabled_roles=["transition"], amount=100))
    assert plan.count_for("transition") <= sm.amount_params(100).transition_cap
    starts = [p.start for p in plan.placements]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= sm.amount_params(100).transition_min_gap_seconds - 1e-9 for g in gaps)


def test_vocal_shots_need_no_impact_threshold_and_stay_in_their_section():
    beats = (5.0, 6.0, 30.0, 60.0)
    st = structure([(0.0, 10.0, "chorus"), (10.0, 50.0, "verse"), (50.0, 80.0, "breakdown")],
                   beats=beats, phrase=(True, True, True, True),
                   impact=(0.0, 0.0, 0.0, 0.0), duration=80.0)
    plan = sm.plan_sfx(st, [asset("vocal_shot", 0.8)],
                       sm.SmartMixConfig(enabled_roles=["vocal_shot"], amount=100))
    starts = [round(p.start, 3) for p in plan.placements]
    assert starts == [5.0, 30.0, 60.0], "one per section, impact 0.0 is no obstacle"


def test_a_vocal_shot_that_overruns_its_section_is_skipped():
    st = structure([(0.0, 10.0, "chorus")], beats=(9.8,), phrase=(True,),
                   impact=(1.0,), duration=40.0)
    plan = sm.plan_sfx(st, [asset("vocal_shot", 1.0)],
                       sm.SmartMixConfig(enabled_roles=["vocal_shot"], amount=100))
    assert plan.count_for("vocal_shot") == 0
    assert any("inside its own section" in s.reason for s in plan.skips)


def test_vocal_shots_ignore_sections_outside_their_vocabulary():
    st = structure([(0.0, 40.0, "intro"), (40.0, 80.0, "drop")],
                   beats=(5.0, 50.0), phrase=(True, True), impact=(1.0, 1.0), duration=80.0)
    plan = sm.plan_sfx(st, [asset("vocal_shot", 0.5)],
                       sm.SmartMixConfig(enabled_roles=["vocal_shot"], amount=100))
    assert plan.count_for("vocal_shot") == 0


def test_atmosphere_is_the_only_role_that_may_be_trimmed():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("atmosphere", 40.0)],
                       sm.SmartMixConfig(enabled_roles=["atmosphere"]))
    assert plan.count_for("atmosphere") == 2
    for placement in plan.placements:
        assert placement.trimmed is True
        assert placement.play_duration < placement.source_duration
        assert round(placement.end - placement.start, 6) == round(placement.play_duration, 6)
    # longest first: both Nero breakdowns (34.877 s and 32.926 s) beat both intros
    assert sorted(round(p.play_duration, 3) for p in plan.placements) == [32.926, 34.877]


def test_a_short_atmosphere_asset_is_not_looped():
    st = structure([(0.0, 40.0, "breakdown")], duration=60.0)
    plan = sm.plan_sfx(st, [asset("atmosphere", 5.0)],
                       sm.SmartMixConfig(enabled_roles=["atmosphere"]))
    placement = plan.placements[0]
    assert placement.play_duration == 5.0
    assert placement.trimmed is False
    assert placement.end == 5.0


def test_short_sections_get_no_atmosphere():
    st = structure([(0.0, 11.0, "intro"), (11.0, 20.0, "breakdown")], duration=20.0)
    plan = sm.plan_sfx(st, [asset("atmosphere", 5.0)],
                       sm.SmartMixConfig(enabled_roles=["atmosphere"]))
    assert plan.count_for("atmosphere") == 0


def test_impact_caps_are_respected():
    beats = tuple(i * 0.5 for i in range(400))
    st = structure([(0.0, 200.0, "drop")], beats=beats, duration=200.0)
    plan = sm.plan_sfx(st, [asset("impact", 0.2)],
                       sm.SmartMixConfig(enabled_roles=["impact"], amount=100))
    # one section -> per-section cap binds first
    assert plan.count_for("impact") == sm.IMPACT_PER_SECTION_CAP


def test_impact_global_cap_is_one_per_twenty_seconds():
    sections = tuple((i * 20.0, (i + 1) * 20.0, f"verse{i}") for i in range(20))
    beats = tuple(i * 0.5 for i in range(800))
    st = structure(sections, beats=beats, duration=400.0)
    plan = sm.plan_sfx(st, [asset("impact", 0.2)],
                       sm.SmartMixConfig(enabled_roles=["impact"], amount=100))
    assert plan.count_for("impact") <= max(1, math.ceil(400.0 / 20.0))


def test_impact_per_section_cap_keys_on_identity_not_type():
    """Two separate sections of the same type each get their own budget."""
    sections = ((0.0, 50.0, "drop"), (50.0, 100.0, "chorus"), (100.0, 150.0, "drop"))
    beats = tuple(i * 1.0 for i in range(150))
    st = structure(sections, beats=beats, duration=150.0)
    plan = sm.plan_sfx(st, [asset("impact", 0.2)],
                       sm.SmartMixConfig(enabled_roles=["impact"], amount=100))
    per_section = {}
    for placement in plan.placements:
        per_section.setdefault(st.section_index_at(placement.start), 0)
        per_section[st.section_index_at(placement.start)] += 1
    assert all(count <= sm.IMPACT_PER_SECTION_CAP for count in per_section.values())
    assert len(per_section) >= 2, "both drops must get their own budget"


def test_impact_minimum_spacing_is_between_successful_placements():
    beats = tuple(i * 0.5 for i in range(200))
    sections = tuple((i * 10.0, (i + 1) * 10.0, f"s{i}") for i in range(10))
    st = structure(sections, beats=beats, duration=100.0)
    for amount in (25, 50, 75, 100):
        plan = sm.plan_sfx(st, [asset("impact", 0.2)],
                           sm.SmartMixConfig(enabled_roles=["impact"], amount=amount))
        starts = [p.start for p in plan.placements]
        gap = sm.amount_params(amount).impact_min_gap_seconds
        assert all(b - a >= gap - 1e-9 for a, b in zip(starts, starts[1:])), amount


# ===========================================================================
# 6. CROSS-ROLE OCCUPANCY
# ===========================================================================


def nero_pools():
    return [
        asset("impact", 0.35, "i1"), asset("impact", 0.40, "i2"), asset("impact", 0.30, "i3"),
        asset("riser", 3.0, "r1"), asset("riser", 2.5, "r2"),
        asset("atmosphere", 40.0, "a1"), asset("atmosphere", 25.0, "a2"),
        asset("transition", 1.2, "t1"), asset("transition", 1.0, "t2"),
        asset("vocal_shot", 0.8, "v1"), asset("vocal_shot", 0.6, "v2"),
    ]


def non_atmosphere(plan):
    return [p for p in plan.placements if p.role != "atmosphere"]


@pytest.mark.parametrize("amount", [1, 25, 37, 50, 62, 75, 91, 100])
def test_non_atmosphere_placements_never_overlap(amount):
    st = structure(NERO_SECTIONS, beats=tuple(i * 0.4878 for i in range(566)),
                   phrase=tuple(i % 8 == 0 for i in range(566)),
                   bar=tuple(i % 4 == 0 for i in range(566)),
                   impact=tuple((i % 13) / 13.0 for i in range(566)), duration=278.021)
    plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=amount))
    spans = sorted((p.start, p.end) for p in non_atmosphere(plan))
    for (_a1, b1), (a2, _b2) in zip(spans, spans[1:]):
        assert a2 >= b1 - 1e-9, f"overlap at amount {amount}"


def test_boundary_touching_is_legal():
    """A riser ending exactly where a transition begins is not an overlap — half-open intervals."""
    st = structure([(0.0, 10.0, "intro"), (10.0, 40.0, "drop")], duration=40.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0), asset("transition", 1.0)],
                       sm.SmartMixConfig(enabled_roles=["riser", "transition"], amount=50))
    by_role = {p.role: p for p in plan.placements}
    assert round(by_role["riser"].end, 6) == 10.0
    assert round(by_role["transition"].start, 6) == 10.0


def test_atmosphere_may_overlap_everything():
    st = structure(NERO_SECTIONS, beats=tuple(i * 0.4878 for i in range(566)),
                   bar=tuple(i % 4 == 0 for i in range(566)),
                   phrase=tuple(i % 8 == 0 for i in range(566)),
                   impact=tuple((i % 13) / 13.0 for i in range(566)), duration=278.021)
    plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=50))
    beds = [p for p in plan.placements if p.role == "atmosphere"]
    others = non_atmosphere(plan)
    assert beds, "the fixture must actually place beds"
    assert any(o.start < bed.end and bed.start < o.end for bed in beds for o in others), (
        "an atmosphere bed is expected to underlay other SFX")


def test_a_colliding_candidate_is_skipped_and_the_planner_continues():
    """A riser claims its span first; a later impact inside it is skipped, not nudged."""
    beats = (5.0, 8.5, 12.0)
    st = structure([(0.0, 10.0, "intro"), (10.0, 40.0, "drop")],
                   beats=beats, bar=(True, True, True), impact=(1.0, 1.0, 1.0), duration=40.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0), asset("impact", 0.5)],
                       sm.SmartMixConfig(enabled_roles=["riser", "impact"], amount=100))
    impacts = [round(p.start, 3) for p in plan.placements if p.role == "impact"]
    assert 8.5 not in impacts, "8.5 sits inside the riser 7.0 -> 10.0"
    assert 5.0 in impacts and 12.0 in impacts, "the planner must keep going after a collision"
    assert any(s.role == "impact" and "collides" in s.reason for s in plan.skips)


def test_an_anchor_is_never_moved_to_avoid_a_collision():
    beats = (8.5,)
    st = structure([(0.0, 10.0, "intro"), (10.0, 40.0, "drop")],
                   beats=beats, bar=(True,), impact=(1.0,), duration=40.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0), asset("impact", 0.5)],
                       sm.SmartMixConfig(enabled_roles=["riser", "impact"], amount=100))
    assert plan.count_for("impact") == 0, "skipped, not relocated to a free instant"


# ===========================================================================
# 7. THE ASSET CURSOR
# ===========================================================================


def test_the_cursor_advances_on_a_failed_attempt():
    """A first asset too long for its target must not block the shorter second one forever."""
    sections = [(0.0, 2.0, "intro"), (2.0, 20.0, "drop"),
                (20.0, 30.0, "breakdown"), (30.0, 60.0, "drop")]
    st = structure(sections, duration=60.0)
    # candidate 0 (target 2.0) takes the 5 s riser and cannot fit; candidate 1 (target 30.0)
    # must then take the 1 s riser — NOT retry the 5 s one.
    plan = sm.plan_sfx(st, [asset("riser", 5.0, "long"), asset("riser", 1.0, "short")],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert len(plan.placements) == 1
    placement = plan.placements[0]
    assert placement.path.endswith("short.wav")
    assert round(placement.start, 3) == 29.0
    assert any("does not fit" in s.reason for s in plan.skips)


def test_the_cursor_is_candidate_ordinal_not_success_ordinal():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("riser", 3.0, "r1"), asset("riser", 2.5, "r2")],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    # three genuine entries, pool of two -> r1, r2, r1
    assert [p.path.rsplit("/", 1)[-1] for p in plan.placements] == [
        "r1.wav", "r2.wav", "r1.wav"]


def test_a_single_asset_pool_repeats_rather_than_running_out():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("riser", 3.0, "only")],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert len({p.path for p in plan.placements}) == 1
    assert plan.count_for("riser") == 3


# ===========================================================================
# 8. DEGENERATE STRUCTURES AND DETERMINISM
# ===========================================================================


@pytest.mark.parametrize("name,sections,duration", [
    ("no drops", [(0.0, 40.0, "intro"), (40.0, 90.0, "verse"), (90.0, 150.0, "chorus")], 150.0),
    ("all drop", [(0.0, 30.0, "drop"), (30.0, 60.0, "drop"), (60.0, 90.0, "drop")], 90.0),
    ("single section", [(0.0, 60.0, "verse")], 60.0),
    ("very short", [(0.0, 4.0, "intro"), (4.0, 8.0, "drop")], 8.0),
    ("no sections at all", [], 60.0),
])
def test_degenerate_structures_degrade_to_zero_not_to_an_exception(name, sections, duration):
    beats = tuple(i * 0.5 for i in range(int(duration * 2)))
    st = structure(sections, beats=beats, duration=duration)
    plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=50))
    assert isinstance(plan.total, int)
    spans = sorted((p.start, p.end) for p in non_atmosphere(plan))
    for (_a1, b1), (a2, _b2) in zip(spans, spans[1:]):
        assert a2 >= b1 - 1e-9, name
    for placement in plan.placements:
        assert placement.start >= -1e-9
        assert placement.end <= duration + 1e-9


def test_an_enabled_role_with_no_assets_is_reported_not_fatal():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)], sm.SmartMixConfig())
    assert plan.count_for("riser") == 3
    assert set(plan.empty_roles) == sm.ALL_ROLES - {"riser"}
    assert "enabled but the library has no assets" in "\n".join(plan.report_lines())


def test_a_disabled_role_is_not_planned_even_with_assets():
    st = structure(NERO_SECTIONS, duration=278.021)
    plan = sm.plan_sfx(st, nero_pools(),
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert plan.counts["riser"] == 3
    assert all(plan.counts[role] == 0 for role in sm.ALL_ROLES - {"riser"})


def test_planning_is_deterministic():
    st = structure(NERO_SECTIONS, beats=tuple(i * 0.4878 for i in range(566)),
                   bar=tuple(i % 4 == 0 for i in range(566)),
                   phrase=tuple(i % 8 == 0 for i in range(566)),
                   impact=tuple((i % 13) / 13.0 for i in range(566)), duration=278.021)
    first = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=50))
    second = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=50))
    assert first.placements == second.placements


def test_every_placement_satisfies_the_frozen_invariant():
    st = structure(NERO_SECTIONS, beats=tuple(i * 0.4878 for i in range(566)),
                   bar=tuple(i % 4 == 0 for i in range(566)),
                   phrase=tuple(i % 8 == 0 for i in range(566)),
                   impact=tuple((i % 13) / 13.0 for i in range(566)), duration=278.021)
    plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=75))
    for placement in plan.placements:
        assert round(placement.end, 9) == round(placement.start + placement.play_duration, 9)
        if placement.role == "atmosphere":
            assert placement.trimmed == (placement.play_duration
                                         < placement.source_duration - sm.EPSILON)
        else:
            assert placement.play_duration == placement.source_duration
            assert placement.trimmed is False


# ===========================================================================
# 9. STRUCTURE PROJECTION
# ===========================================================================


def beat_info(times, impact=None, bar=None, phrase=None, sections=None, duration=30.0):
    n = len(times)
    return {
        "times": list(times),
        "rhythm_data": {
            "impact_strength": list(impact if impact is not None else [0.5] * n),
            "is_bar_anchor": list(bar if bar is not None else [True] * n),
            "is_phrase_anchor": list(phrase if phrase is not None else [False] * n),
        },
        "sections": sections if sections is not None else [
            {"start": 0.0, "end": duration, "type": "verse"}],
        "audio_duration": duration,
    }


def test_projection_reads_only_already_computed_music_information():
    st = sm.project_structure(beat_info([0.0, 1.0, 2.0]))
    assert st.beat_times == (0.0, 1.0, 2.0)
    assert st.music_duration == 30.0
    assert st.sections == ((0.0, 30.0, "verse"),)


def test_misaligned_rhythm_arrays_raise_rather_than_zip_short():
    info = beat_info([0.0, 1.0, 2.0])
    info["rhythm_data"]["impact_strength"] = [0.5, 0.5]        # one short
    with pytest.raises(sm.SmartMixStructureError, match="misaligned"):
        sm.project_structure(info)


@pytest.mark.parametrize("mutate,match", [
    (lambda i: i.pop("rhythm_data"), "rhythm_data"),
    (lambda i: i.update(times=[]), "beat times"),
    (lambda i: i.update(rhythm_data={}), "misaligned"),
])
def test_malformed_structure_fails_clearly(mutate, match):
    info = beat_info([0.0, 1.0])
    mutate(info)
    with pytest.raises(sm.SmartMixStructureError, match=match):
        sm.project_structure(info)


def test_projection_skips_malformed_sections_without_raising():
    info = beat_info([0.0, 1.0], sections=[
        {"start": 0.0, "end": 10.0, "type": "intro"},
        {"start": "x", "end": 20.0, "type": "drop"},
        {"start": 20.0, "end": 10.0, "type": "drop"},          # end <= start
        {"no": "shape"},
        {"start": 20.0, "end": 30.0},                          # missing type -> "body"
    ])
    st = sm.project_structure(info)
    assert st.sections == ((0.0, 10.0, "intro"), (20.0, 30.0, "body"))


def test_projection_falls_back_to_the_last_beat_when_duration_is_unusable():
    info = beat_info([0.0, 5.0, 11.5])
    info["audio_duration"] = 0.0
    assert sm.project_structure(info).music_duration == 11.5


def test_section_index_at():
    st = structure([(0.0, 10.0, "intro"), (10.0, 20.0, "drop")], duration=20.0)
    assert st.section_index_at(0.0) == 0
    assert st.section_index_at(9.999) == 0
    assert st.section_index_at(10.0) == 1
    assert st.section_index_at(99.0) == -1


# ===========================================================================
# 10. REPORTING
# ===========================================================================


def test_report_and_summary_shapes():
    st = structure(NERO_SECTIONS, beats=tuple(i * 0.4878 for i in range(566)),
                   bar=tuple(i % 4 == 0 for i in range(566)),
                   phrase=tuple(i % 8 == 0 for i in range(566)),
                   impact=tuple((i % 13) / 13.0 for i in range(566)), duration=278.021)
    plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=50),
                       library_root=r"D:\Audio\SFX")
    text = "\n".join(plan.report_lines())
    assert text.startswith("Smart Mix")
    assert r"D:\Audio\SFX" in text
    assert "Amount: 50" in text and "SFX level: 50%" in text
    assert "Total SFX:" in text
    assert "alimiter" not in text and "amix" not in text and "aevalsrc" not in text
    summary = plan.summary_line()
    assert summary.startswith(f"Smart Mix: {plan.total} SFX")


def test_a_zero_placement_plan_still_explains_itself():
    st = structure([(0.0, 60.0, "verse")], duration=60.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0)],
                       sm.SmartMixConfig(enabled_roles=["riser"]))
    assert plan.total == 0
    assert "No SFX placed" in "\n".join(plan.report_lines())


def test_skip_reasons_are_summarised_not_dumped_one_line_each():
    beats = tuple(i * 0.25 for i in range(400))
    st = structure([(0.0, 10.0, "intro"), (10.0, 100.0, "drop")],
                   beats=beats, duration=100.0)
    plan = sm.plan_sfx(st, [asset("riser", 3.0), asset("impact", 0.2)],
                       sm.SmartMixConfig(enabled_roles=["riser", "impact"], amount=100))
    lines = plan.report_lines()
    assert len(lines) < 60, "the report must summarise skips, never list every candidate"


def test_the_pure_module_imports_no_runtime():
    """Belt-and-braces next to tests/test_no_runtime_dependency.py's repo-wide check."""
    import ast as _ast
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "src", "beatsync_fork", "smart_mix.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = _ast.parse(handle.read())
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("numpy", "gradio", "cv2", "librosa", "cupy", "logger", "paths",
                   "subprocess", "audio_mixdown", "video_analysis", "ffmpeg_processing"):
        assert banned not in imported, banned


# ===========================================================================
# 11. THE ACCEPTED NERO CALIBRATION, REPRODUCED THROUGH THE REAL PLANNER
# ===========================================================================
#
# `tests/fixtures/nero_structure.json` holds the musical structure of the accepted calibration
# track, derived once by a read-only Stages 1-4 analysis. It contains **no audio** — only beat
# times, the two anchor masks, the impact curve and the section table — so the suite stays
# bare-interpreter portable and never depends on the production media file.


def _nero_structure():
    import json
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "fixtures", "nero_structure.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    count = data["beats"]

    def unbits(text):
        value = int(text, 16)
        return tuple(bool(value >> i & 1) for i in range(count))

    return sm.MusicStructure(
        music_duration=data["audio_duration"],
        beat_times=tuple(data["times"]),
        is_bar_anchor=unbits(data["is_bar_anchor_bits"]),
        is_phrase_anchor=unbits(data["is_phrase_anchor_bits"]),
        impact_strength=tuple(data["impact_strength"]),
        sections=tuple((s["start"], s["end"], s["type"]) for s in data["sections"]),
    )


def test_the_fixture_is_the_accepted_calibration_material():
    st = _nero_structure()
    assert len(st.beat_times) == 566
    assert len(st.sections) == 13
    assert round(st.music_duration, 3) == 278.021
    assert [s[2] for s in st.sections] == [
        "intro", "intro", "drop", "drop", "chorus", "drop", "drop",
        "breakdown", "verse", "breakdown", "drop", "finale", "finale"]


#: The finalized R0 ladder, under the frozen cross-role occupancy policy, with the R0 pools.
NERO_LADDER = {
    25:  {"riser": 3, "impact": 4, "transition": 4, "vocal_shot": 2, "atmosphere": 2, "_total": 15},
    50:  {"riser": 3, "impact": 5, "transition": 6, "vocal_shot": 3, "atmosphere": 2, "_total": 19},
    75:  {"riser": 3, "impact": 8, "transition": 7, "vocal_shot": 4, "atmosphere": 2, "_total": 24},
    100: {"riser": 3, "impact": 9, "transition": 7, "vocal_shot": 4, "atmosphere": 2, "_total": 25},
}


@pytest.mark.parametrize("amount,expected", sorted(NERO_LADDER.items()))
def test_the_accepted_nero_ladder_is_reproduced(amount, expected):
    plan = sm.plan_sfx(_nero_structure(), nero_pools(), sm.SmartMixConfig(amount=amount))
    counts = plan.counts
    for role in sm.ROLE_ORDER:
        assert counts[role] == expected[role], f"{role} at amount {amount}"
    assert plan.total == expected["_total"]


@pytest.mark.parametrize("amount", sorted(NERO_LADDER))
def test_the_nero_ladder_has_no_non_atmosphere_overlaps(amount):
    plan = sm.plan_sfx(_nero_structure(), nero_pools(), sm.SmartMixConfig(amount=amount))
    spans = sorted((p.start, p.end) for p in non_atmosphere(plan))
    for (_a1, b1), (a2, _b2) in zip(spans, spans[1:]):
        assert a2 >= b1 - 1e-9, f"overlap at amount {amount}"


def test_the_default_impact_count_is_five_because_a_riser_won_the_span():
    """The accepted consequence of riser priority, pinned so it cannot regress back to 6 silently.

    The bar anchor at ~87.655 s carries high impact, but the riser into the drop at 88.143 s
    already occupies 85.643 -> 88.143. Priority resolves it: the riser keeps its span, the impact
    is skipped with a reason, and nothing is nudged.
    """
    plan = sm.plan_sfx(_nero_structure(), nero_pools(), sm.SmartMixConfig(amount=50))
    assert plan.count_for("impact") == 5
    collisions = [s for s in plan.skips if s.role == "impact" and "collides" in s.reason]
    assert len(collisions) == 1
    assert 87.0 < collisions[0].at < 88.143
    riser_ends = [round(p.end, 3) for p in plan.placements if p.role == "riser"]
    assert 88.143 in riser_ends


def test_risers_end_exactly_on_the_genuine_drop_entries():
    plan = sm.plan_sfx(_nero_structure(), nero_pools(), sm.SmartMixConfig(amount=50))
    ends = sorted(round(p.end, 3) for p in plan.placements if p.role == "riser")
    assert ends == [53.267, 88.143, 204.266]


def test_nothing_runs_past_the_end_of_the_music():
    st = _nero_structure()
    for amount in NERO_LADDER:
        plan = sm.plan_sfx(st, nero_pools(), sm.SmartMixConfig(amount=amount))
        for placement in plan.placements:
            assert placement.end <= st.music_duration + 1e-9, amount


# ===========================================================================
# 12. LIBRARY DIAGNOSTICS (R1)
# ===========================================================================


def test_diagnostics_default_to_empty_and_silent():
    diagnostics = sm.SfxLibraryDiagnostics()
    assert diagnostics.unknown_folders == ()
    assert diagnostics.root_level_files == 0
    assert diagnostics.unsupported_files == 0
    assert diagnostics.skipped_disabled_files == 0
    assert diagnostics.has_ignores is False
    assert diagnostics.report_lines() == ()


def test_only_non_empty_counts_produce_a_line():
    assert sm.SfxLibraryDiagnostics(root_level_files=2).report_lines() == (
        "Ignored root-level files: 2",)
    assert sm.SfxLibraryDiagnostics(unsupported_files=3).report_lines() == (
        "Ignored unsupported files: 3",)
    assert sm.SfxLibraryDiagnostics(skipped_disabled_files=4).report_lines() == (
        "Files in disabled roles skipped: 4",)
    assert sm.SfxLibraryDiagnostics(unknown_folders=("Impats",)).report_lines() == (
        "Ignored unknown role folders: Impats",)


def test_all_four_lines_render_in_a_stable_order():
    diagnostics = sm.SfxLibraryDiagnostics(
        unknown_folders=("Impats", "Misc"), root_level_files=2,
        unsupported_files=3, skipped_disabled_files=4)
    assert diagnostics.report_lines() == (
        "Ignored unknown role folders: Impats, Misc",
        "Ignored root-level files: 2",
        "Ignored unsupported files: 3",
        "Files in disabled roles skipped: 4",
    )
    assert diagnostics.has_ignores is True


@pytest.mark.parametrize("given,expected", [
    (("zeta", "Alpha", "misc"), ("Alpha", "misc", "zeta")),
    (("b", "A", "a", "B"), ("A", "a", "B", "b")),          # case-folded, original as tie-break
    (("Impats", "impats"), ("Impats", "impats")),
    ((), ()),
    (None, ()),
    (("ok", None, 7, ""), ("ok",)),
])
def test_unknown_folder_ordering_is_deterministic(given, expected):
    assert sm.sort_unknown_folders(given) == expected


def test_ordering_does_not_depend_on_the_order_it_was_handed():
    names = ("zeta", "Alpha", "misc", "Bravo")
    once = sm.sort_unknown_folders(names)
    twice = sm.sort_unknown_folders(tuple(reversed(names)))
    assert once == twice


def test_the_report_renders_diagnostics_it_was_given():
    plan = sm.plan_sfx(
        structure(NERO_SECTIONS, duration=278.021), [asset("riser", 3.0)],
        sm.SmartMixConfig(enabled_roles=["riser"]),
        library_root="D:/sfx",
        library_diagnostics=sm.SfxLibraryDiagnostics(
            root="D:/sfx", unknown_folders=("Impats",), root_level_files=1))
    text = "\n".join(plan.report_lines())
    assert "Ignored unknown role folders: Impats" in text
    assert "Ignored root-level files: 1" in text


def test_diagnostics_never_influence_a_placement():
    st = structure(NERO_SECTIONS, duration=278.021)
    config = sm.SmartMixConfig(enabled_roles=["riser"])
    plain = sm.plan_sfx(st, [asset("riser", 3.0)], config)
    noisy = sm.plan_sfx(st, [asset("riser", 3.0)], config,
                        library_diagnostics=sm.SfxLibraryDiagnostics(
                            unknown_folders=("a", "b"), root_level_files=9,
                            unsupported_files=9, skipped_disabled_files=9))
    assert plain.placements == noisy.placements


def test_diagnostics_are_optional_and_default_silently():
    plan = sm.plan_sfx(structure(NERO_SECTIONS, duration=278.021),
                       [asset("riser", 3.0)], sm.SmartMixConfig(enabled_roles=["riser"]))
    assert plan.library_diagnostics == sm.SfxLibraryDiagnostics()
    assert "Ignored" not in "\n".join(plan.report_lines())


def test_an_amount_zero_plan_still_carries_its_diagnostics():
    """Amount 0 is a hard off-branch, but a typo'd folder is still worth telling the user about."""
    diagnostics = sm.SfxLibraryDiagnostics(unknown_folders=("Impats",))
    plan = sm.plan_sfx(structure(NERO_SECTIONS, duration=278.021),
                       [asset("riser", 3.0)], sm.SmartMixConfig(amount=0),
                       library_diagnostics=diagnostics)
    assert plan.total == 0
    assert plan.library_diagnostics is diagnostics
    assert "Ignored unknown role folders: Impats" in "\n".join(plan.report_lines())


def test_the_diagnostics_value_is_immutable_and_hashable():
    diagnostics = sm.SfxLibraryDiagnostics(
        unknown_folders=("a",), per_role=(("impact", 2),))
    with pytest.raises(Exception):
        diagnostics.root_level_files = 1
    assert isinstance(hash(diagnostics), int)


def test_the_library_root_falls_back_to_the_diagnostics_root():
    plan = sm.plan_sfx(structure(NERO_SECTIONS, duration=278.021),
                       [asset("riser", 3.0)], sm.SmartMixConfig(enabled_roles=["riser"]),
                       library_diagnostics=sm.SfxLibraryDiagnostics(root="D:/sfx"))
    assert plan.library_root == "D:/sfx"


def test_there_is_exactly_one_report_formatter():
    """The diagnostics render through `SmartMixPlan.report_lines()` and nowhere else."""
    import ast as _ast
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for relative in ("src/gui.py", "src/audio_mixdown.py"):
        with open(_os.path.join(repo, relative), "r", encoding="utf-8") as handle:
            tree = _ast.parse(handle.read())
        # strip docstrings: the prose legitimately explains what the report says
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef,
                                 _ast.ClassDef)):
                if (node.body and isinstance(node.body[0], _ast.Expr)
                        and isinstance(node.body[0].value, _ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:] or [_ast.Pass()]
        code = _ast.unparse(_ast.fix_missing_locations(tree))
        for phrase in ("Ignored unknown role folders", "Ignored root-level files",
                       "Ignored unsupported files", "Files in disabled roles skipped"):
            assert phrase not in code, f"{relative} formats the report itself"


def test_the_nero_ladder_is_untouched_by_r1():
    """R1 is reporting only — the accepted calibration must be value-identical."""
    for amount, expected in NERO_LADDER.items():
        plan = sm.plan_sfx(_nero_structure(), nero_pools(), sm.SmartMixConfig(amount=amount),
                           library_diagnostics=sm.SfxLibraryDiagnostics(
                               unknown_folders=("Impats",), root_level_files=3))
        assert plan.counts == {role: expected[role] for role in sm.ROLE_ORDER}
        assert plan.total == expected["_total"]


# ===========================================================================
# 13. ARRAY-LIKE STRUCTURE INPUTS (runtime R1)
# ===========================================================================
#
# The runtime acceptance render died after Stage 5 with
#
#     ValueError: The truth value of an array with more than one element is ambiguous.
#
# because `project_structure` defaulted four `beat_info` fields with `value or ()`. At real
# runtime all four are numpy `ndarray` of length 566, and `bool(ndarray)` raises for any length
# above one. Every fixture in this suite fed lists and tuples — whose truth value is perfectly
# well defined — so the whole suite passed while the feature could not run at all.
#
# These tests reproduce that hazard WITHOUT importing numpy: `beatsync_fork` is stdlib-only by
# rule, and importing numpy to test it would break the very property that makes the planner
# testable on a bare interpreter.


class AmbiguousArray:
    """A sequence that iterates normally but refuses to be truth-tested, exactly like ndarray.

    Any boolean coercion — `bool(x)`, `if x`, `x or y`, `not x` — raises, so a single accidental
    truthiness check anywhere in the projection path fails the test loudly instead of silently
    working on the list fixtures.
    """

    def __init__(self, values):
        self._values = list(values)
        self.bool_calls = 0

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __getitem__(self, index):
        return self._values[index]

    def __bool__(self):
        self.bool_calls += 1
        raise ValueError(
            "The truth value of an array with more than one element is ambiguous. "
            "Use a.any() or a.all()")


def test_the_fixture_itself_refuses_boolean_coercion():
    """If this ever stopped raising, every test below would pass vacuously."""
    array = AmbiguousArray([1.0, 2.0, 3.0])
    assert list(array) == [1.0, 2.0, 3.0]
    assert len(array) == 3
    with pytest.raises(ValueError, match="ambiguous"):
        bool(array)
    with pytest.raises(ValueError, match="ambiguous"):
        array or ()
    with pytest.raises(ValueError, match="ambiguous"):
        if array:
            pass


def _array_beat_info(times, impact, bar, phrase, sections=None, duration=30.0,
                     wrap=("times", "impact_strength", "is_bar_anchor", "is_phrase_anchor")):
    """`beat_info` with the chosen fields wrapped in `AmbiguousArray`."""
    def maybe(name, values):
        return AmbiguousArray(values) if name in wrap else list(values)

    return {
        "times": maybe("times", times),
        "rhythm_data": {
            "impact_strength": maybe("impact_strength", impact),
            "is_bar_anchor": maybe("is_bar_anchor", bar),
            "is_phrase_anchor": maybe("is_phrase_anchor", phrase),
        },
        "sections": sections if sections is not None else [
            {"start": 0.0, "end": duration, "type": "verse"}],
        "audio_duration": duration,
    }


def _simple_arrays(count=8):
    times = [i * 1.0 for i in range(count)]
    impact = [(i % 5) / 5.0 for i in range(count)]
    bar = [i % 2 == 0 for i in range(count)]
    phrase = [i % 4 == 0 for i in range(count)]
    return times, impact, bar, phrase


@pytest.mark.parametrize("field", [
    "times", "impact_strength", "is_bar_anchor", "is_phrase_anchor",
])
def test_each_structure_field_survives_an_ambiguous_array_independently(field):
    """`times` failed first at runtime, which would hide the other three. Each is pinned alone."""
    times, impact, bar, phrase = _simple_arrays()
    info = _array_beat_info(times, impact, bar, phrase, wrap=(field,))
    structure = sm.project_structure(info)
    assert structure.beat_times == tuple(times)
    assert structure.impact_strength == tuple(impact)
    assert structure.is_bar_anchor == tuple(bar)
    assert structure.is_phrase_anchor == tuple(phrase)


def test_all_four_fields_ambiguous_at_once():
    times, impact, bar, phrase = _simple_arrays()
    info = _array_beat_info(times, impact, bar, phrase)
    structure = sm.project_structure(info)
    assert len(structure.beat_times) == 8
    assert structure.music_duration == 30.0
    # and nothing tried to truth-test any of them
    for value in (info["times"], info["rhythm_data"]["impact_strength"],
                  info["rhythm_data"]["is_bar_anchor"],
                  info["rhythm_data"]["is_phrase_anchor"]):
        assert value.bool_calls == 0


def test_the_whole_nero_structure_projects_from_ambiguous_arrays():
    """End to end on the accepted calibration data, which is the shape production really has."""
    import json
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         "fixtures", "nero_structure.json")
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    count = data["beats"]

    def unbits(text):
        value = int(text, 16)
        return [bool(value >> i & 1) for i in range(count)]

    info = _array_beat_info(
        data["times"], data["impact_strength"],
        unbits(data["is_bar_anchor_bits"]), unbits(data["is_phrase_anchor_bits"]),
        sections=data["sections"], duration=data["audio_duration"])
    structure = sm.project_structure(info)
    assert len(structure.beat_times) == 566
    assert len(structure.sections) == 13

    # and the frozen ladder still resolves from it, unchanged
    for amount, expected in NERO_LADDER.items():
        plan = sm.plan_sfx(structure, nero_pools(), sm.SmartMixConfig(amount=amount))
        assert plan.total == expected["_total"], amount
        assert plan.counts == {role: expected[role] for role in sm.ROLE_ORDER}, amount


def test_missing_fields_still_behave_as_empty():
    """`None` means empty — the semantics the old `or ()` was reaching for, without the coercion."""
    assert sm._missing_as_empty(None) == ()
    with pytest.raises(sm.SmartMixStructureError, match="beat times"):
        sm.project_structure({"times": None, "rhythm_data": {}, "audio_duration": 10.0})


def test_a_non_none_value_passes_through_untouched():
    """Critically: the helper must not inspect or coerce anything that is not `None`."""
    array = AmbiguousArray([1.0])
    assert sm._missing_as_empty(array) is array
    assert array.bool_calls == 0
    for value in ([], (), [0.0], "", 0):
        assert sm._missing_as_empty(value) is value


def test_an_empty_ambiguous_array_still_fails_clearly():
    """A zero-length array-like is 'empty', not a crash, and reaches the existing error."""
    info = _array_beat_info([], [], [], [])
    with pytest.raises(sm.SmartMixStructureError, match="no usable beat times"):
        sm.project_structure(info)


def test_misaligned_ambiguous_arrays_still_raise_the_alignment_error():
    times, impact, bar, phrase = _simple_arrays()
    info = _array_beat_info(times, impact[:-1], bar, phrase)
    with pytest.raises(sm.SmartMixStructureError, match="misaligned"):
        sm.project_structure(info)


# ---------------------------------------------------------------------------
# structural guard: the four fields must never be truth-tested again
# ---------------------------------------------------------------------------


def _project_structure_ast():
    import ast as _ast
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "src", "beatsync_fork", "smart_mix.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = _ast.parse(handle.read())
    return _ast, next(n for n in _ast.walk(tree)
                      if isinstance(n, _ast.FunctionDef) and n.name == "project_structure")


ARRAY_FIELDS = ("times", "impact_strength", "is_bar_anchor", "is_phrase_anchor")


def test_no_array_field_is_defaulted_through_boolean_or():
    """Narrow guard: no `.get(<array field>) or ...` may return to `project_structure`.

    Deliberately not a module-wide ban on `or` — only these four musical-structure values are
    array-like, and only they are forbidden from appearing in boolean context.
    """
    ast_mod, func = _project_structure_ast()
    for node in ast_mod.walk(func):
        if not isinstance(node, ast_mod.BoolOp):
            continue
        rendered = ast_mod.unparse(node)
        for field in ARRAY_FIELDS:
            assert f"'{field}'" not in rendered and f'"{field}"' not in rendered, (
                f"{field} is truth-tested in: {rendered}")


def test_no_array_field_is_passed_to_bool_or_not():
    ast_mod, func = _project_structure_ast()
    for node in ast_mod.walk(func):
        rendered = None
        if isinstance(node, ast_mod.Call) and getattr(node.func, "id", None) == "bool":
            rendered = ast_mod.unparse(node)
        elif isinstance(node, ast_mod.UnaryOp) and isinstance(node.op, ast_mod.Not):
            rendered = ast_mod.unparse(node)
        if rendered is None:
            continue
        for field in ARRAY_FIELDS:
            assert f"'{field}'" not in rendered and f'"{field}"' not in rendered, (
                f"{field} is coerced in: {rendered}")


def test_the_four_fields_are_read_through_the_missing_helper():
    ast_mod, func = _project_structure_ast()
    body = ast_mod.unparse(func)
    for field in ARRAY_FIELDS:
        assert f"_missing_as_empty(" in body
        assert f"get('{field}')" in body, field


def test_the_pure_module_still_imports_no_numpy():
    """The fix must not be 'import numpy and call .size'."""
    import ast as _ast
    import os as _os
    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "src", "beatsync_fork", "smart_mix.py")
    with open(path, "r", encoding="utf-8") as handle:
        tree = _ast.parse(handle.read())
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "numpy" not in imported
    assert imported <= {"__future__", "math", "collections", "dataclasses", "typing",
                        "beatsync_fork"}, imported
