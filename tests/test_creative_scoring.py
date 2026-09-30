"""Energy Response and Motion Bias — the two Stage 6 creative controls.

Both change **only** how a candidate is scored. Neither may touch the cut timeline, the audio
features, the sections, the per-segment target distribution or anything Stage 5 persisted. And at 50
each must reproduce current main's arithmetic exactly, not approximately: the neutral branch returns
``_static_base_score`` untouched rather than running the new expression with a neutral coefficient.

The measurements here are **behavioural**. Asserting the algebra would only restate the
implementation; what matters is whether the control changes which candidates a real plan picks, in
the direction the label on the slider claims, monotonically, without breaking the plan.

The planner needs numpy but has no relative imports, so it is loaded by path — the technique
``test_creative_seed`` and ``test_stage6_score_precompute`` already use, which keeps
``auto_mode/__init__`` (librosa, cupy, logger) out of the suite.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from collections import Counter, deque

import pytest

from beatsync_fork import creative as fork_creative

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_creative_scoring", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A music timeline with every target represented
# ---------------------------------------------------------------------------

_CUTS = [float(t) for t in range(0, 25)]
_DURATIONS = [1.0] * (len(_CUTS) - 1)

_WAVE = [0.20, 0.22, 0.26, 0.30, 0.34, 0.40, 0.46, 0.50, 0.54, 0.56, 0.58, 0.60,
         0.62, 0.64, 0.66, 0.68, 0.70, 0.72, 0.86, 0.90, 0.92, 0.90, 0.86, 0.80, 0.70]
_ARC = [0.10, 0.14, 0.18, 0.22, 0.28, 0.34, 0.40, 0.46, 0.52, 0.56, 0.60, 0.64,
        0.68, 0.70, 0.72, 0.74, 0.76, 0.78, 0.82, 0.86, 0.88, 0.86, 0.82, 0.76, 0.68]
_IMPACT = [0.20, 0.22, 0.26, 0.30, 0.34, 0.40, 0.46, 0.50, 0.54, 0.58, 0.60, 0.62,
           0.64, 0.66, 0.68, 0.70, 0.72, 0.74, 0.86, 0.92, 0.94, 0.90, 0.86, 0.78, 0.70]
_RHYTHM = [0.20, 0.24, 0.28, 0.32, 0.36, 0.42, 0.48, 0.52, 0.56, 0.60, 0.62, 0.64,
           0.66, 0.68, 0.70, 0.72, 0.74, 0.76, 0.84, 0.90, 0.92, 0.88, 0.84, 0.76, 0.68]
_NOVELTY = [0.15, 0.18, 0.22, 0.26, 0.30, 0.34, 0.38, 0.42, 0.46, 0.50, 0.54, 0.58,
            0.70, 0.72, 0.74, 0.72, 0.68, 0.64, 0.58, 0.54, 0.50, 0.46, 0.42, 0.38, 0.34]

_NO_SEMANTIC_ACTION = {"character_focus": 0.3, "combat": 0.0, "chase": 0.0, "explosion": 0.0}


def _beat_info(candidates, **creative) -> dict:
    info = {
        "times": [float(t) for t in range(0, 25)],
        "sections": [{"type": "intro", "start": 0.0, "end": 6.0, "energy": 0.2},
                     {"type": "verse", "start": 6.0, "end": 12.0, "energy": 0.5},
                     {"type": "bridge", "start": 12.0, "end": 18.0, "energy": 0.7},
                     {"type": "drop", "start": 18.0, "end": 24.0, "energy": 0.95}],
        "energy_profile": {"wave": _WAVE, "arc": _ARC},
        "rhythm_data": {"impact_strength": _IMPACT, "combined_strength": _RHYTHM,
                        "novelty_strength": _NOVELTY},
        "video_analysis": {"candidates": candidates},
    }
    if creative:
        info["creative"] = dict(creative)
    return info


def _plan(planner, candidates, **creative):
    return planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(candidates, **creative), video_files=[])


def _base(index: str, **over) -> dict:
    candidate = {
        "id": index, "video_file": f"C:/lib/{index}.mp4", "source_name": index,
        "duration": 4.0, "video_duration": 60.0,
        "center": 10.0, "peak_time": 10.5, "start": 8.0,
        "quality_score": 0.60, "action_score": 0.50, "beauty_score": 0.55,
        "tension_score": 0.40, "soft_score": 0.35, "motion": 0.50, "brightness": 0.60,
        "tags": ["action"], "semantic": dict(_NO_SEMANTIC_ACTION), "ai_analyzed": True,
    }
    candidate.update(over)
    return candidate


# ---------------------------------------------------------------------------
# The Energy Response pool: target-matched vs generically-good, tied at neutral
# ---------------------------------------------------------------------------
#
# The two classes are tuned so their "drop" scores are *equal* while their "flow" scores differ by
# ~0.42. That tie is what makes the control's direction unambiguous: at neutral the blend does
# nothing, above neutral the target-matched class pulls ahead, below neutral the generically-good one
# does. A tie is asserted rather than assumed — see the first test in that section.


def _energy_pool(count: int = 14) -> list[dict]:
    pool = []
    for i in range(count):
        drift = (i % 5) * 0.004
        pool.append(_base(
            f"matched_{i:02d}",
            video_file=f"C:/lib/matched_{i:02d}.mp4", source_name=f"matched_{i:02d}",
            center=10.0 + i, peak_time=10.5 + i, start=8.0 + i,
            quality_score=0.30 + drift, action_score=0.55, beauty_score=0.15,
            tension_score=0.30, soft_score=0.10, motion=0.50, tags=["action"],
        ))
    for i in range(count):
        drift = (i % 5) * 0.004
        pool.append(_base(
            f"generic_{i:02d}",
            video_file=f"C:/lib/generic_{i:02d}.mp4", source_name=f"generic_{i:02d}",
            center=10.0 + i, peak_time=10.5 + i, start=8.0 + i,
            quality_score=0.90 - drift, action_score=0.55, beauty_score=0.60,
            tension_score=0.40, soft_score=0.55, motion=0.50, tags=["soft", "beauty"],
        ))
    return pool


def _motion_pool(count: int = 14) -> list[dict]:
    """Calm and dynamic candidates that are otherwise identical.

    ``motion`` feeds several of the target formulas, so an "otherwise identical" pair is not
    score-identical for every target — which is exactly why the assertions below are about the
    *direction* of a monotone sweep rather than about a hand-computed winner.
    """
    pool = []
    for i in range(count):
        drift = (i % 5) * 0.004
        pool.append(_base(
            f"calm_{i:02d}",
            video_file=f"C:/lib/calm_{i:02d}.mp4", source_name=f"calm_{i:02d}",
            center=10.0 + i, peak_time=10.5 + i, start=8.0 + i,
            quality_score=0.70 + drift, motion=0.05,
        ))
    for i in range(count):
        drift = (i % 5) * 0.004
        pool.append(_base(
            f"dynamic_{i:02d}",
            video_file=f"C:/lib/dynamic_{i:02d}.mp4", source_name=f"dynamic_{i:02d}",
            center=10.0 + i, peak_time=10.5 + i, start=8.0 + i,
            quality_score=0.70 + drift, motion=0.95,
        ))
    return pool


_SWEEP = (0, 25, 50, 75, 100)


def _targets(plan):
    return [item["target"] for item in plan]


def _ids(plan):
    return [item["candidate_id"] for item in plan]


# ===========================================================================
# 1. EXACT LEGACY STAGE 6 AT 50 / 50
# ===========================================================================


@pytest.mark.parametrize("target", ["drop", "soft", "build", "rhythm", "flow"])
def test_neutral_controls_return_the_legacy_static_score_identically(planner, target):
    """Bit-identical, and by the same route: the neutral branch returns `_static_base_score` itself.

    `is` cannot be used on floats, so this asserts exact equality across a pool wide enough that a
    stray blend or shift anywhere in the range would show up somewhere.
    """
    neutral = fork_creative.CreativeProfile().scoring_controls()
    for candidate in _energy_pool() + _motion_pool():
        legacy = planner._static_base_score(candidate, target)
        assert planner._effective_base_score(candidate, target, neutral) == legacy
        assert planner._effective_base_score(candidate, target, None) == legacy
        assert planner._effective_base_score(candidate, target) == legacy
        assert planner._score_candidate(candidate, {"target": target}, neutral) == legacy
        assert planner._score_candidate(candidate, {"target": target}) == legacy


def test_a_neutral_profile_plans_exactly_what_no_creative_state_plans(planner):
    """Upgrading and leaving the sliders alone must not change anyone's edit."""
    for pool in (_energy_pool(), _motion_pool()):
        baseline = _ids(_plan(planner, pool))
        neutral = _ids(_plan(planner, pool, seed=0, cut_density=50,
                             energy_response=50, motion_bias=50))
        phase_a = _ids(_plan(planner, pool, seed=0))

        assert neutral == baseline
        assert phase_a == baseline


def test_the_plan_score_field_is_the_legacy_score_on_a_neutral_render(planner):
    pool = _energy_pool()
    for item in _plan(planner, pool, energy_response=50, motion_bias=50):
        candidate = next(c for c in pool if c["id"] == item["candidate_id"])
        assert item["score"] == planner._static_base_score(candidate, item["target"])


def test_cut_density_does_not_reach_stage_6_scoring(planner):
    """Stage 4 owns Cut Density. A density on the bus must leave Stage 6 completely unmoved — the
    planner is handed a *different cut timeline* on a real render, but the timeline is Stage 4's
    output, not something Stage 6 re-derives from the control."""
    pool = _energy_pool()
    baseline = _plan(planner, pool)

    for density in (0, 25, 75, 100):
        plan = _plan(planner, pool, cut_density=density)
        assert _ids(plan) == _ids(baseline), density
        assert [i["score"] for i in plan] == [i["score"] for i in baseline], density


def test_the_seed_picks_a_winner_without_changing_what_good_means(planner):
    """A seeded render chooses different candidates; each one's score is still the legacy score."""
    pool = _energy_pool()
    for item in _plan(planner, pool, seed=381944):
        candidate = next(c for c in pool if c["id"] == item["candidate_id"])
        assert item["score"] == planner._static_base_score(candidate, item["target"])


# ===========================================================================
# 2. ENERGY RESPONSE
# ===========================================================================


def test_the_energy_fixture_really_is_tied_at_neutral(planner):
    """The premise of every assertion below. If the two classes were not tied on "drop" at neutral,
    a later flip could be an artefact of the pool rather than of the control."""
    matched = _energy_pool()[0]
    generic = _energy_pool()[14]

    assert planner._static_base_score(matched, "drop") == pytest.approx(
        planner._static_base_score(generic, "drop"), abs=1e-9)
    # ...and they disagree sharply about generic suitability, in the expected directions
    assert planner._static_base_score(matched, "drop") > planner._static_base_score(matched, "flow")
    assert planner._static_base_score(generic, "flow") > planner._static_base_score(generic, "drop")


def _target_lift(planner, plan, pool) -> float:
    """Mean ``score(chosen, its target) − score(chosen, "flow")``.

    A direct measure of "did the planner pick target-matched material?", computed from the **legacy**
    scores of whatever got chosen — so it describes the outcome rather than restating the blend.
    """
    by_id = {c["id"]: c for c in pool}
    lifts = [planner._static_base_score(by_id[i["candidate_id"]], i["target"])
             - planner._static_base_score(by_id[i["candidate_id"]], "flow") for i in plan]
    return sum(lifts) / len(lifts)


def test_higher_energy_response_picks_more_target_matched_material(planner):
    pool = _energy_pool()
    lifts = [_target_lift(planner, _plan(planner, pool, energy_response=r), pool) for r in _SWEEP]

    assert all(a <= b + 1e-12 for a, b in zip(lifts, lifts[1:])), lifts
    assert lifts[0] < lifts[2] < lifts[-1], lifts


def test_low_energy_response_moves_towards_generic_suitability(planner):
    """The slider's left-hand label, measured: on drop segments the target-matched class loses its
    place to the generically-good one."""
    pool = _energy_pool()

    def matched_on_drop(response):
        plan = _plan(planner, pool, energy_response=response)
        drops = [i for i in plan if i["target"] == "drop"]
        assert drops, "the fixture must contain drop segments"
        return sum(1 for i in drops if i["candidate_id"].startswith("matched")), len(drops)

    weak, total = matched_on_drop(0)
    neutral, _ = matched_on_drop(50)
    strong, _ = matched_on_drop(100)

    assert weak == 0, f"weak matching still chose {weak}/{total} target-matched candidates"
    assert strong == total, f"strong matching chose only {strong}/{total}"
    assert weak < neutral <= strong


def test_energy_response_changes_nothing_about_the_music(planner):
    """Targets, cut timeline and plan length are Stage 2-4 facts. A scoring control may not move
    them, and the target distribution is the one most easily broken by accident."""
    pool = _energy_pool()
    baseline = _plan(planner, pool)
    baseline_targets = _targets(baseline)

    for response in _SWEEP:
        plan = _plan(planner, pool, energy_response=response)
        assert _targets(plan) == baseline_targets, response
        assert Counter(_targets(plan)) == Counter(baseline_targets)
        assert len(plan) == len(_DURATIONS), f"energy {response} dropped segments"
        assert [i["audio_start"] for i in plan] == [i["audio_start"] for i in baseline]
        assert [i["audio_end"] for i in plan] == [i["audio_end"] for i in baseline]
        assert [i["final_duration"] for i in plan] == [i["final_duration"] for i in baseline]


@pytest.mark.parametrize("response", _SWEEP)
def test_every_energy_response_produces_a_complete_well_formed_plan(planner, response):
    plan = _plan(planner, _energy_pool(), energy_response=response)

    assert len(plan) == len(_DURATIONS), "a fallback plan means the planner gave up"
    for item in plan:
        assert item["video_file"]
        assert item["final_duration"] > 0
        assert item["start_time"] >= 0.0
        assert item["target"] in {"soft", "flow", "build", "rhythm", "drop"}
        assert -1.0 <= item["score"] <= 2.0


def test_energy_response_keeps_the_score_inside_the_planner_s_range(planner):
    """The blend can overshoot; the existing clamp is what catches it."""
    extreme = _base("x", quality_score=1.0, action_score=1.0, beauty_score=1.0,
                    tension_score=1.0, soft_score=1.0, motion=1.0,
                    tags=["drop", "action", "combat", "chase", "explosion", "hype"],
                    semantic={"character_focus": 1.0, "combat": 1.0, "chase": 1.0,
                              "explosion": 1.0})
    empty = _base("y", quality_score=0.0, action_score=0.0, beauty_score=0.0,
                  tension_score=0.0, soft_score=0.0, motion=0.0, brightness=0.0, tags=[])

    for response in range(0, 101, 5):
        controls = fork_creative.CreativeProfile(energy_response=response).scoring_controls()
        for candidate in (extreme, empty):
            for target in ("drop", "soft", "build", "rhythm", "flow"):
                score = planner._effective_base_score(candidate, target, controls)
                assert -1.0 <= score <= 2.0, (response, target, score)


def test_the_flow_target_is_its_own_blend_fixed_point(planner):
    """``flow + f * (flow - flow)`` is ``flow`` for every ``f``, so a flow segment's score cannot
    move with Energy Response. Useful as a sanity check on the blend's reference point."""
    for response in _SWEEP:
        controls = fork_creative.CreativeProfile(energy_response=response).scoring_controls()
        for candidate in _energy_pool(4):
            assert planner._effective_base_score(candidate, "flow", controls) == pytest.approx(
                planner._static_base_score(candidate, "flow"))


def test_a_supplied_flow_score_matches_computing_it(planner):
    """The precompute optimisation must be invisible: passing the flow column or letting the
    function fetch it must give the same number."""
    controls = fork_creative.CreativeProfile(energy_response=90).scoring_controls()
    for candidate in _energy_pool(6) + _motion_pool(6):
        for target in ("drop", "soft", "build", "rhythm", "flow"):
            computed = planner._effective_base_score(candidate, target, controls)
            supplied = planner._effective_base_score(
                candidate, target, controls,
                flow_score=planner._static_base_score(candidate, "flow"))
            assert supplied == computed, (candidate["id"], target)


# ===========================================================================
# 3. MOTION BIAS
# ===========================================================================


def _dynamic_share(planner, pool, bias) -> int:
    return sum(1 for i in _plan(planner, pool, motion_bias=bias)
               if i["candidate_id"].startswith("dynamic"))


def test_motion_bias_moves_monotonically_from_calm_to_dynamic(planner):
    pool = _motion_pool()
    shares = [_dynamic_share(planner, pool, bias) for bias in _SWEEP]

    assert all(a <= b for a, b in zip(shares, shares[1:])), shares
    assert shares[0] < shares[2] < shares[-1], shares


@pytest.mark.parametrize("bias, preferred", [(0, "calm"), (100, "dynamic")])
def test_each_extreme_overwhelmingly_prefers_its_own_material(planner, bias, preferred):
    """"Overwhelmingly", not "exclusively", and the difference is the point.

    The pool holds 14 of each class for 24 segments, so once the preferred class has been used up the
    repeat and usage penalties legitimately push the planner across into the other one — a bias that
    could suppress that would be a bias strong enough to override the anti-repeat rules, which is
    exactly what the 0.15 coefficient is sized not to do.
    """
    plan = _plan(planner, _motion_pool(), motion_bias=bias)
    chosen = sum(1 for i in plan if i["candidate_id"].startswith(preferred))

    assert chosen >= len(plan) * 0.8, f"{chosen}/{len(plan)} {preferred}: {_ids(plan)}"


def test_motion_bias_changes_nothing_about_the_music(planner):
    pool = _motion_pool()
    baseline = _plan(planner, pool)
    baseline_targets = _targets(baseline)

    for bias in _SWEEP:
        plan = _plan(planner, pool, motion_bias=bias)
        assert _targets(plan) == baseline_targets, bias
        assert len(plan) == len(_DURATIONS), f"motion {bias} dropped segments"
        assert [i["audio_start"] for i in plan] == [i["audio_start"] for i in baseline]
        assert [i["final_duration"] for i in plan] == [i["final_duration"] for i in baseline]


def test_motion_bias_uses_the_semantic_camera_motion_fallback(planner):
    """The same fallback the base score already uses: the deterministic metric when present, Qwen's
    ``camera_motion`` otherwise. A second definition of "dynamic" would silently diverge from the
    planner's own."""
    deterministic = _base("d", motion=0.9)
    semantic_only = _base("s", semantic={"character_focus": 0.3, "combat": 0.0, "chase": 0.0,
                                         "explosion": 0.0, "camera_motion": 0.9})
    semantic_only.pop("motion")
    neither = _base("n", semantic=dict(_NO_SEMANTIC_ACTION))
    neither.pop("motion")

    assert planner._candidate_motion(deterministic) == pytest.approx(0.9)
    assert planner._candidate_motion(semantic_only) == pytest.approx(0.9)
    assert planner._candidate_motion(neither) == 0.0
    # and the deterministic value wins when both are present, as the base score already decides
    both = _base("b", motion=0.2, semantic={"camera_motion": 0.95})
    assert planner._candidate_motion(both) == pytest.approx(0.2)


def test_candidate_motion_reads_exactly_what_the_legacy_score_reads():
    """The one drift risk in this change, pinned at the source.

    `_static_base_score` was deliberately left byte-identical, so it still resolves motion inline
    rather than calling `_candidate_motion`. The two expressions must therefore stay the same
    expression, or "Motion Bias weights the value the planner already believes" would quietly stop
    being true. Compared as unparsed ASTs, so formatting cannot mask a real difference.
    """
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    helper = next(ast.unparse(n.value) for n in funcs["_candidate_motion"].body
                  if isinstance(n, ast.Return))
    legacy = next(ast.unparse(n.value) for n in ast.walk(funcs["_static_base_score"])
                  if isinstance(n, ast.Assign)
                  and any(getattr(t, "id", None) == "motion" for t in n.targets))

    assert helper == legacy, f"{helper!r} vs {legacy!r}"


def test_motion_bias_is_symmetric_about_the_midpoint_of_motion(planner):
    """`2 * motion - 1` means motion 0.5 is the pivot: a mid-motion candidate is neither rewarded
    nor punished at any bias setting."""
    middling = _base("m", motion=0.5)

    for bias in range(0, 101, 10):
        controls = fork_creative.CreativeProfile(motion_bias=bias).scoring_controls()
        assert planner._effective_base_score(middling, "flow", controls) == pytest.approx(
            planner._static_base_score(middling, "flow"))


def test_the_anti_repeat_penalty_outweighs_any_candidate_s_own_motion_advantage(planner):
    """The reason 0.15 was chosen. A recently-used candidate loses 0.28 and can gain at most 0.15,
    so Motion Bias can never overturn an anti-repeat decision the planner made on purpose.

    Target "flow" deliberately: motion is absent from the flow formula, so the two candidates have
    identical base scores and the only difference in play is the bias shift itself.
    """
    dynamic = _base("dynamic", motion=1.0)
    middling = _base("middling", motion=0.5)
    profile = {"target": "flow", "duration": 1.0, "start": 0.0, "end": 1.0}
    controls = fork_creative.CreativeProfile(motion_bias=100).scoring_controls()

    assert planner._static_base_score(dynamic, "flow") == planner._static_base_score(middling, "flow")

    def adjusted(candidate, recent):
        return planner._adjusted_score(
            candidate, profile, deque(recent, maxlen=10), deque(maxlen=5), Counter(),
            base_score=planner._effective_base_score(candidate, "flow", controls),
            controls=controls)

    # with nothing used recently, the bias does its job
    assert adjusted(dynamic, []) > adjusted(middling, [])
    # once the dynamic candidate is in the recent window, the penalty wins
    assert adjusted(dynamic, ["dynamic"]) < adjusted(middling, [])
    assert fork_creative.MOTION_BIAS_COEFFICIENT < 0.28


def test_quality_and_visibility_penalties_survive_a_maximal_motion_bias(planner):
    """A dark, low-quality but highly dynamic shot must not be promoted past a good one."""
    good = _base("good", quality_score=0.80, brightness=0.60, motion=0.40)
    dark = _base("dark", quality_score=0.10, brightness=0.05, motion=1.00)
    controls = fork_creative.CreativeProfile(motion_bias=100).scoring_controls()

    assert (planner._effective_base_score(good, "rhythm", controls)
            > planner._effective_base_score(dark, "rhythm", controls))


def test_repeat_penalties_still_spread_a_biased_plan_across_sources(planner):
    for bias in (0, 100):
        plan = _plan(planner, _motion_pool(), motion_bias=bias)
        ids = _ids(plan)
        assert len(set(ids)) > 1, f"bias {bias} collapsed onto one candidate"
        assert all(a != b for a, b in zip(ids, ids[1:])), "same candidate back to back"
        assert len({i["video_file"] for i in plan}) > 1


# ===========================================================================
# 4. COMPOSITION AND ORDERING
# ===========================================================================


def test_the_documented_ordering_is_what_the_code_does(planner):
    """Energy blend first, then the motion shift, then one clamp. Recomputed here in that order from
    the legacy primitives, so a reordering — which would change results — fails."""
    controls = fork_creative.CreativeProfile(energy_response=80, motion_bias=20).scoring_controls()

    for candidate in _energy_pool(6) + _motion_pool(6):
        for target in ("drop", "soft", "build", "rhythm", "flow"):
            target_score = planner._static_base_score(candidate, target)
            flow_score = planner._static_base_score(candidate, "flow")
            expected = flow_score + controls.energy_factor * (target_score - flow_score)
            expected += (controls.motion_centered * fork_creative.MOTION_BIAS_COEFFICIENT
                         * (2.0 * planner._candidate_motion(candidate) - 1.0))
            expected = planner._clamp(expected, lo=-1.0, hi=2.0)

            assert planner._effective_base_score(candidate, target, controls) == expected


def test_one_control_at_a_time_costs_nothing_for_the_other(planner):
    """A render that only moves Motion Bias must score identically to one where Energy Response is
    neutral — i.e. the energy blend must genuinely not run."""
    motion_only = fork_creative.CreativeProfile(motion_bias=20).scoring_controls()
    for candidate in _energy_pool(6):
        for target in ("drop", "soft"):
            legacy = planner._static_base_score(candidate, target)
            shift = (motion_only.motion_centered * fork_creative.MOTION_BIAS_COEFFICIENT
                     * (2.0 * planner._candidate_motion(candidate) - 1.0))
            assert planner._effective_base_score(candidate, target, motion_only) == pytest.approx(
                planner._clamp(legacy + shift, lo=-1.0, hi=2.0))


def test_the_controls_do_not_depend_on_the_seed(planner):
    """Scoring is seed-free; the seed changes only which of the scored candidates wins."""
    pool = _energy_pool()
    for seed in (0, 101, 381944):
        plan = _plan(planner, pool, seed=seed, energy_response=90, motion_bias=20)
        for item in plan:
            candidate = next(c for c in pool if c["id"] == item["candidate_id"])
            controls = fork_creative.CreativeProfile(
                energy_response=90, motion_bias=20).scoring_controls()
            assert item["score"] == planner._effective_base_score(
                candidate, item["target"], controls)


def test_the_materialized_score_agrees_with_the_effective_scoring(planner):
    """`plan["score"]` is diagnostic, but a plan reporting the legacy score for a render that chose
    on a modified one would be a quietly misleading record."""
    pool = _motion_pool()
    for creative in ({"energy_response": 0}, {"motion_bias": 100},
                     {"energy_response": 100, "motion_bias": 0}):
        controls = fork_creative.CreativeProfile.from_mapping(creative).scoring_controls()
        plan = _plan(planner, pool, **creative)
        for item in plan:
            candidate = next(c for c in pool if c["id"] == item["candidate_id"])
            assert item["score"] == planner._effective_base_score(
                candidate, item["target"], controls), creative
        # and it is genuinely not the legacy number somewhere in the plan, or this would be vacuous
        assert any(
            item["score"] != planner._static_base_score(
                next(c for c in pool if c["id"] == item["candidate_id"]), item["target"])
            for item in plan), creative


def test_clip_timing_and_anchoring_are_untouched_by_either_control(planner):
    """Anchor choice and alignment are functions of the candidate and the target, both unchanged."""
    pool = _motion_pool()
    baseline = {i["index"]: (i["target"], i["start_time"], i["source_duration"])
                for i in _plan(planner, pool)}

    for creative in ({"energy_response": 0}, {"motion_bias": 100}):
        for item in _plan(planner, pool, **creative):
            target, _, duration = baseline[item["index"]]
            assert item["target"] == target
            assert item["source_duration"] == duration
            # the chosen candidate may differ, but its anchor must follow the same rule
            candidate = next(c for c in pool if c["id"] == item["candidate_id"])
            expected = planner._materialize_clip(
                candidate, {"index": item["index"], "target": target,
                            "duration": item["final_duration"],
                            "start": item["audio_start"], "end": item["audio_end"],
                            "wave": item["wave"], "impact": item["impact"]},
                item["index"])
            assert item["start_time"] == expected["start_time"]


# ===========================================================================
# 5. L1A PRECOMPUTE MUST SURVIVE
# ===========================================================================


def _reference_plan(planner, pool, **creative):
    """The same planner, with the static-score table switched off.

    ``base_scores=None`` makes ``_adjusted_score`` compute each static score on the spot — the
    pre-L1A shape — while every other piece is the production code. ``controls`` rides along, because
    the fallback must resolve to the same number the table would have held; if it did not, this
    reference would silently be a *legacy* plan and the comparison would be meaningless.
    """
    import numpy as np

    beat_info = _beat_info(pool, **creative)
    candidates = [c for c in pool if c.get("video_file")]
    profiles = planner._build_segment_profiles(
        np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), beat_info)
    controls = planner.creative_profile(beat_info).scoring_controls()
    seed = planner.creative_seed(beat_info)
    recent_ids, recent_videos, usage = deque(maxlen=10), deque(maxlen=5), Counter()
    chosen = []
    for i, profile in enumerate(profiles):
        candidate = planner._choose_candidate(
            candidates=candidates, profile=profile, recent_ids=recent_ids,
            recent_videos=recent_videos, usage=usage, index=i, seed=seed,
            base_scores=None, controls=controls)
        if not candidate:
            continue
        chosen.append(candidate["id"])
        recent_ids.append(candidate.get("id"))
        recent_videos.append(candidate.get("video_file"))
        usage[candidate.get("id")] += 1
        usage[candidate.get("video_file")] += 1
    return chosen


_GRID = [
    {},
    {"energy_response": 0}, {"energy_response": 100},
    {"motion_bias": 0}, {"motion_bias": 100},
    {"energy_response": 80, "motion_bias": 30},
    {"seed": 101, "energy_response": 80, "motion_bias": 30},
    {"seed": 381944, "energy_response": 0, "motion_bias": 100},
]


@pytest.mark.parametrize("creative", _GRID)
def test_the_precomputed_table_plans_what_the_unprecomputed_path_plans(planner, creative):
    """The L1A equivalence proof, extended to every creative profile."""
    for pool in (_energy_pool(), _motion_pool()):
        assert _ids(_plan(planner, pool, **creative)) == _reference_plan(
            planner, pool, **creative), creative


@pytest.mark.parametrize("creative", _GRID)
def test_static_scoring_never_scales_with_segment_count(planner, monkeypatch, creative):
    """The critical L1A property, asserted as an algorithmic count rather than a clock.

    Expected shape: ``candidates × distinct segment targets``, plus one ``candidates`` column for
    "flow" when Energy Response needs it, plus the per-clip materialise call that L1A already left
    alone (two of those when Energy Response is on, because the diagnostic score also needs a flow
    reference). Never ``candidates × segments``.
    """
    pool = _energy_pool()
    calls: list[str] = []
    real = planner._static_base_score

    def spy(candidate, target):
        calls.append(target)
        return real(candidate, target)

    monkeypatch.setattr(planner, "_static_base_score", spy)
    plan = _plan(planner, pool, **creative)
    monkeypatch.undo()

    assert len(plan) == len(_DURATIONS)
    controls = fork_creative.CreativeProfile.from_mapping(creative).scoring_controls()
    segment_targets = _targets(plan)
    distinct = len(set(segment_targets))
    flow_column = 1 if controls.needs_flow_column else 0
    per_clip = 2 if controls.energy_factor is not None else 1
    expected = len(pool) * (distinct + flow_column) + len(plan) * per_clip

    assert len(calls) == expected, (
        f"{len(calls)} static evaluations; expected {len(pool)} candidates × "
        f"({distinct} targets + {flow_column} flow column) + {len(plan)} × {per_clip}")
    assert len(calls) < len(pool) * len(_DURATIONS), "regressed to candidates × segments"


def test_the_flow_column_is_built_only_when_energy_response_needs_it(planner, monkeypatch):
    """Motion Bias must not pay for work it does not use, and Energy Response must pay for exactly
    one extra candidates-wide column rather than one extra evaluation per (candidate, target)."""
    pool = _energy_pool()

    def flow_evaluations(**creative):
        calls: list[str] = []
        real = planner._static_base_score
        monkeypatch.setattr(planner, "_static_base_score",
                            lambda c, t: (calls.append(t), real(c, t))[1])
        try:
            _plan(planner, pool, **creative)
        finally:
            monkeypatch.undo()
        return Counter(calls)["flow"]

    neutral = flow_evaluations()
    motion_only = flow_evaluations(motion_bias=100)
    energy = flow_evaluations(energy_response=100)

    assert motion_only == neutral, "Motion Bias built a flow column it does not need"
    # one whole column, plus the per-clip diagnostic score's own flow reference
    assert energy - neutral == len(pool) + len(_DURATIONS)
    # decisively cheaper than one flow evaluation per (candidate, target)
    assert energy - neutral < len(pool) * len(set(_targets(_plan(planner, pool))))


def test_the_precomputation_stays_inside_the_measured_planner_call():
    """L0's `planner_seconds` must keep describing the whole call, blend included."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_planned_clip_sequence")
    body = ast.unparse(fn)

    assert "base_scores_by_target" in body
    assert "flow_scores" in body, "the flow column must be built inside the planner call"
    for forbidden in ("lru_cache", "functools.cache", "global "):
        assert forbidden not in body, forbidden


def test_the_planner_holds_no_module_level_creative_state():
    """One render's controls must not be observable by the next."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    assignments = {t.id for node in tree.body if isinstance(node, ast.Assign)
                   for t in node.targets if isinstance(t, ast.Name)}

    assert not assignments, f"the planner gained module-level state: {assignments}"
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))]


def test_controls_are_bound_explicitly_rather_than_read_from_a_global():
    """`_static_base_score` still takes only (candidate, target), so a scoring term that depends on
    anything else cannot be added there by accident; the controls arrive as parameters."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    assert [a.arg for a in funcs["_static_base_score"].args.args] == ["candidate", "target"]
    for name in ("_effective_base_score", "_score_candidate", "_materialize_clip",
                 "_adjusted_score", "_choose_candidate"):
        parameters = [a.arg for a in funcs[name].args.args + funcs[name].args.kwonlyargs]
        assert "controls" in parameters, name


# ===========================================================================
# 6. INTERACTIONS AND DETERMINISM
# ===========================================================================


@pytest.mark.parametrize("creative", _GRID)
def test_every_profile_in_the_grid_is_deterministic_and_complete(planner, creative):
    for pool in (_energy_pool(), _motion_pool()):
        first = _plan(planner, pool, **creative)
        second = _plan(planner, pool, **creative)

        assert _ids(first) == _ids(second), creative
        assert len(first) == len(_DURATIONS), f"{creative} fell back"
        assert _targets(first) == _targets(_plan(planner, pool))


def test_a_positive_seed_still_varies_and_still_reproduces_under_the_new_controls(planner):
    pool = _energy_pool()
    combined = {"energy_response": 80, "motion_bias": 30}

    legacy = _ids(_plan(planner, pool, seed=0, **combined))
    a = _ids(_plan(planner, pool, seed=101, **combined))
    b = _ids(_plan(planner, pool, seed=202, **combined))

    assert a == _ids(_plan(planner, pool, seed=101, **combined)), "seed stopped reproducing"
    assert a != legacy and b != legacy, "the seed stopped varying once controls were on"
    assert a != b


def test_one_render_s_controls_cannot_leak_into_the_next(planner):
    """Interleaved plans in one process: the controls live on the call stack only."""
    pool = _motion_pool()
    calm = _ids(_plan(planner, pool, motion_bias=0))
    _plan(planner, pool, motion_bias=100, energy_response=0)
    neutral = _ids(_plan(planner, pool))
    _plan(planner, pool, energy_response=100)
    calm_again = _ids(_plan(planner, pool, motion_bias=0))
    neutral_again = _ids(_plan(planner, pool))

    assert calm == calm_again
    assert neutral == neutral_again
    assert neutral != calm


# ===========================================================================
# 7. REPORTING
# ===========================================================================


def test_the_plan_summary_carries_the_resolved_profile(planner):
    plan = _plan(planner, _energy_pool(), seed=101, cut_density=65,
                 energy_response=80, motion_bias=40)
    profile = fork_creative.CreativeProfile(seed=101, cut_density=65,
                                            energy_response=80, motion_bias=40)
    summary = planner.summarize_clip_plan(plan, seed=101, creative=profile)

    assert summary["creative"] == profile.as_dict()
    assert summary["creative_text"] == profile.describe()
    assert summary["seed"] == 101
    assert summary["clip_count"] == len(plan)


def test_the_plan_summary_still_works_for_pre_core_callers(planner):
    """`summarize_clip_plan(plan)` and `(plan, seed=n)` are existing call shapes."""
    plan = _plan(planner, _energy_pool())

    bare = planner.summarize_clip_plan(plan)
    assert bare["variation"] == "legacy"
    assert bare["creative"] == fork_creative.NEUTRAL_PROFILE.as_dict()
    assert bare["creative_text"] == "legacy"

    seeded = planner.summarize_clip_plan(plan, seed=381944)
    assert seeded["seed"] == 381944
    assert seeded["creative"]["seed"] == 381944
    assert seeded["creative"]["cut_density"] == fork_creative.DEFAULT_CONTROL


def test_an_empty_plan_summary_still_reports_the_profile(planner):
    profile = fork_creative.CreativeProfile(cut_density=10)
    summary = planner.summarize_clip_plan([], creative=profile)

    assert summary["clip_count"] == 0
    assert summary["creative"] == profile.as_dict()
    assert summary["creative_text"] == profile.describe()


def test_creative_profile_reads_the_bus_defensively(planner):
    assert planner.creative_profile(None) == fork_creative.NEUTRAL_PROFILE
    assert planner.creative_profile({}) == fork_creative.NEUTRAL_PROFILE
    assert planner.creative_profile({"creative": "not a dict"}) == fork_creative.NEUTRAL_PROFILE
    assert planner.creative_profile({"creative": {"seed": "12"}}).seed == 12
    assert planner.creative_profile(
        {"creative": {"cut_density": 999}}).cut_density == fork_creative.CONTROL_MAX


# ===========================================================================
# 8. DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_the_controls_change_the_plan(planner):
    """Readable evidence on synthetic fixtures. To read it::

        python -m pytest tests/test_creative_scoring.py -s -k practical_diagnostic
    """
    energy, motion = _energy_pool(), _motion_pool()
    lines = []
    for label, pool, creative in [
        ("Legacy      50/50/50", energy, {}),
        ("Weak energy    E=0", energy, {"energy_response": 0}),
        ("Strong energy  E=100", energy, {"energy_response": 100}),
        ("Calm           M=0", motion, {"motion_bias": 0}),
        ("Dynamic        M=100", motion, {"motion_bias": 100}),
    ]:
        plan = _plan(planner, pool, **creative)
        profile = fork_creative.CreativeProfile.from_mapping(creative)
        matched = sum(1 for i in plan if i["candidate_id"].startswith(("matched", "dynamic")))
        lines.append(
            f"  {label:22s} {profile.describe():<62s} clips {len(plan):3d}  "
            f"target-lift {_target_lift(planner, plan, pool):+.4f}  "
            f"matched/dynamic {matched:2d}/{len(plan)}")
    print("\nStage 6 creative controls — same music, same library:\n" + "\n".join(lines))

    assert len(lines) == 5
