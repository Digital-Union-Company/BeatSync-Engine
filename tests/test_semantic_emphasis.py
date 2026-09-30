"""Semantic Emphasis — the Stage 6 interpretation control (Creative Controls Extra PR2).

The control weights *how much of the persisted semantic reading Stage 6 should believe*, against the
deterministic visual evidence reconstructed by ``beatsync_fork.deterministic_view``. It is
interpretation, not analysis: Stage 5 is untouched, and 0 does **not** disable Qwen — on a cold cache
Qwen still runs and still writes the same records.

Three things this suite has to establish, in order of importance:

1. **50 costs nothing.** Not merely "the factor is 1.0" — the neutral branch must construct no
   deterministic view and perform no extra score evaluation, because the reconstruction is the
   expensive and drift-prone half of the feature.
2. **It actually changes choices**, in the direction the slider claims, on fixtures where
   deterministic and semantic evidence genuinely disagree.
3. **It invents nothing.** On a candidate Qwen never touched, the deterministic view *is* the
   candidate, so the control must be exactly inert rather than manufacturing an "AI effect".

Fixtures are built by replicating ``_merge_semantic``'s fusion, so the candidates have the shape a
real fused record has: a deterministic layer underneath and a semantic layer blended over it. That
also means the deterministic view can recover the layer underneath, which is the whole premise.
"""

from __future__ import annotations

import ast
import importlib.util
import os
from collections import Counter, deque

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import deterministic_view as dv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_semantic_emphasis", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fused candidates, built the way Stage 5 builds them
# ---------------------------------------------------------------------------


def _fused(index, prefix, *, brightness, contrast, saturation, sharpness, motion, colorfulness,
           sem_quality, sem_action, sem_beauty, character, combat=0.0, chase=0.0, explosion=0.0,
           use="flow", emotion="neutral"):
    """A candidate as it exists *after* ``_merge_semantic``.

    The fusion arithmetic is replicated here rather than imported, because the point of the fixture
    is to produce a record with a recoverable deterministic layer underneath — exactly what a real
    persisted candidate has. ``tests/test_deterministic_view.py`` is where the reconstruction is
    pinned against the real Stage-5 code; this file only needs realistic material.
    """
    def clamp(v):
        return max(0.0, min(1.0, v))

    primitives = {"brightness": brightness, "contrast": contrast, "saturation": saturation,
                  "sharpness": sharpness, "motion": motion, "colorfulness": colorfulness}
    det = dv.deterministic_scores(primitives)

    gate = clamp(0.35 + 0.65 * motion)
    action = clamp(0.72 * det["action_score"] + 0.28 * sem_action * gate)
    beauty = clamp(0.65 * det["beauty_score"] + 0.35 * sem_beauty)
    quality = clamp(0.70 * det["quality_score"] + 0.30 * sem_quality)
    camera = clamp(0.75 * motion + 0.25 * motion)
    tension = clamp(0.48 * det["tension_score"] + 0.22 * camera + 0.18 * action
                    + 0.12 * character)
    soft = clamp(0.48 * beauty + 0.24 * character + 0.18 * (1.0 - action) + 0.10 * quality)

    tags = set(dv.deterministic_tags(action, beauty, tension, soft, quality))
    tags.update({use, emotion} - {""})

    candidate = dict(primitives)
    candidate.update({
        "id": f"{prefix}_{index:02d}", "video_file": f"C:/lib/{prefix}_{index:02d}.mp4",
        "source_name": f"{prefix}_{index:02d}", "duration": 4.0, "video_duration": 60.0,
        "center": 10.0 + index, "peak_time": 10.2 + index, "start": 8.0 + index,
        "quality_score": quality, "action_score": action, "beauty_score": beauty,
        "tension_score": tension, "soft_score": soft, "tags": sorted(tags),
        "semantic": {
            "action_intensity": sem_action, "beauty_score": sem_beauty, "emotion": emotion,
            "combat": combat, "chase": chase, "explosion": explosion,
            "character_focus": character, "camera_motion": motion,
            "visual_quality": sem_quality, "recommended_use": use, "description": "",
        },
        "ai_analyzed": True,
    })
    return candidate


def _rich(count=8):
    """Semantically rich, visually plain: a calm, softly-lit character moment.

    Low contrast/colour/sharpness, so deterministic metrics rate it poorly; high semantic quality,
    beauty and character focus, so the persisted reading rates it highly.
    """
    return [_fused(i, "rich", brightness=0.50, contrast=0.22, saturation=0.20, sharpness=0.25,
                   motion=0.10, colorfulness=0.15, sem_quality=0.92, sem_action=0.10,
                   sem_beauty=0.95, character=0.92, use="soft", emotion="soft")
            for i in range(count)]


def _plain(count=8):
    """Visually strong, semantically empty: a crisp, colourful but meaningless frame."""
    return [_fused(i, "plain", brightness=0.52, contrast=0.80, saturation=0.75, sharpness=0.85,
                   motion=0.12, colorfulness=0.80, sem_quality=0.20, sem_action=0.05,
                   sem_beauty=0.10, character=0.05, use="flow", emotion="neutral")
            for i in range(count)]


def _action(count=8):
    """High-motion action material, where Stage 5's motion gate leaves little for semantics to add."""
    return [_fused(i, "act", brightness=0.52, contrast=0.70, saturation=0.65, sharpness=0.70,
                   motion=0.90, colorfulness=0.60, sem_quality=0.90, sem_action=0.95,
                   sem_beauty=0.60, character=0.50, combat=0.8, chase=0.7, explosion=0.6,
                   use="drop", emotion="hype")
            for i in range(count)]


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


def _ids(plan):
    return [item["candidate_id"] for item in plan]


def _targets(plan):
    return [item["target"] for item in plan]


_SWEEP = (0, 25, 50, 75, 100)


# ===========================================================================
# 1. NEUTRAL COSTS NOTHING — not just "the factor is 1"
# ===========================================================================


def test_neutral_builds_no_deterministic_view(planner, monkeypatch):
    """The load-bearing neutral claim. A default render must not pay for the reconstruction at all."""
    built: list = []
    real = planner.fork_deterministic.deterministic_candidate_view
    monkeypatch.setattr(planner.fork_deterministic, "deterministic_candidate_view",
                        lambda c: (built.append(c.get("id")), real(c))[1])

    _plan(planner, _rich() + _plain())
    assert built == [], "a neutral render reconstructed deterministic views"

    _plan(planner, _rich() + _plain(), semantic_emphasis=50)
    assert built == [], "semantic_emphasis=50 reconstructed deterministic views"

    # ...and a non-neutral one does, or the assertion above would be vacuous
    _plan(planner, _rich() + _plain(), semantic_emphasis=100)
    assert built, "semantic_emphasis=100 built no deterministic view"


def test_neutral_leaves_the_static_evaluation_count_unchanged(planner, monkeypatch):
    def static_calls(**creative):
        calls: list = []
        real = planner._static_base_score
        monkeypatch.setattr(planner, "_static_base_score",
                            lambda c, t: (calls.append(t), real(c, t))[1])
        try:
            _plan(planner, _rich() + _plain(), **creative)
        finally:
            monkeypatch.undo()
        return len(calls)

    assert static_calls(semantic_emphasis=50) == static_calls()


def test_neutral_plans_exactly_what_no_creative_state_plans(planner):
    pool = _rich() + _plain()
    baseline = _ids(_plan(planner, pool))

    assert _ids(_plan(planner, pool, semantic_emphasis=50)) == baseline
    assert _ids(_plan(planner, pool, seed=0, cut_density=50, energy_response=50, motion_bias=50,
                      source_diversity=50, micro_cuts=50, semantic_emphasis=50)) == baseline


def test_a_pre_pr2_bus_reads_back_neutral(planner):
    """A Creative Controls Extra PR1 bus carries no `semantic_emphasis`."""
    pool = _rich() + _plain()
    baseline = _ids(_plan(planner, pool))

    assert _ids(_plan(planner, pool, seed=0, cut_density=50, energy_response=50,
                      motion_bias=50, source_diversity=50, micro_cuts=50)) == baseline
    assert _ids(_plan(planner, pool, seed=0)) == baseline


@pytest.mark.parametrize("target", ["drop", "soft", "build", "rhythm", "flow"])
def test_neutral_returns_the_legacy_static_score_identically(planner, target):
    neutral = fork_creative.CreativeProfile().scoring_controls()
    for candidate in _rich(3) + _plain(3) + _action(3):
        legacy = planner._static_base_score(candidate, target)
        assert planner._effective_base_score(candidate, target, neutral) == legacy
        assert planner._effective_base_score(candidate, target, None) == legacy


def test_positive_seed_reproducibility_is_unchanged(planner):
    pool = _rich() + _plain()
    for seed in (101, 202):
        assert _ids(_plan(planner, pool, seed=seed)) == _ids(_plan(planner, pool, seed=seed))
    assert _ids(_plan(planner, pool, seed=101)) != _ids(_plan(planner, pool, seed=0))


# ===========================================================================
# 2. BEHAVIOUR — soft/build, where the evidence genuinely disagrees
# ===========================================================================


def test_the_soft_fixture_really_does_disagree(planner):
    """The premise of the behavioural tests below. If the two classes agreed, a later flip could be
    an artefact of the pool rather than of the control."""
    rich, plain = _rich(1)[0], _plain(1)[0]
    rich_full = planner._static_base_score(rich, "soft")
    rich_det = planner._static_base_score(dv.deterministic_candidate_view(rich), "soft")
    plain_full = planner._static_base_score(plain, "soft")
    plain_det = planner._static_base_score(dv.deterministic_candidate_view(plain), "soft")

    # semantics lift the rich one and are absent from the plain one
    assert rich_full - rich_det > 0.3
    assert plain_full - plain_det < -0.3
    # at neutral the rich one wins; on deterministic evidence alone the plain one does
    assert rich_full > plain_full
    assert plain_det > rich_det


def _rich_share(planner, emphasis, target=None) -> tuple:
    """How many segments the semantically-rich class wins.

    ``target`` restricts the count to one segment type. That matters: the fixture's divergence is
    engineered on ``soft`` (and to a lesser degree ``build``), so the *whole-plan* total is a diluted
    measure — it also counts ``drop`` and ``flow`` segments where the two classes score similarly and
    the anti-repeat penalties, not the control, decide. Both views are asserted below.
    """
    plan = _plan(planner, _rich() + _plain(), semantic_emphasis=emphasis)
    segments = [i for i in plan if target is None or i["target"] == target]
    rich = sum(1 for i in segments if i["candidate_id"].startswith("rich"))
    return rich, len(segments)


def test_low_emphasis_prefers_deterministic_evidence(planner):
    """The slider's left-hand label, measured on the segments the fixture was built to separate.

    On ``soft`` targets the flip is total: with the semantic contribution removed, the
    visually-strong but semantically-empty class takes every segment.
    """
    low, total = _rich_share(planner, 0, "soft")
    neutral, _ = _rich_share(planner, 50, "soft")

    assert total >= 4, f"only {total} soft segments to measure on"
    assert low == 0, f"emphasis 0 still chose {low}/{total} semantically-rich clips on soft"
    assert neutral == total, f"neutral chose {neutral}/{total} — the fixture premise moved"


def test_high_emphasis_prefers_semantically_stronger_candidates(planner):
    neutral_soft, soft_total = _rich_share(planner, 50, "soft")
    high_soft, _ = _rich_share(planner, 100, "soft")
    neutral_all, _ = _rich_share(planner, 50)
    high_all, _ = _rich_share(planner, 100)

    assert high_soft == soft_total, f"emphasis 100 chose {high_soft}/{soft_total} on soft"
    assert high_all > neutral_all, (high_all, neutral_all)


def test_the_preference_moves_monotonically_across_the_sweep(planner):
    """Across the whole plan, so this is the diluted measure — and it still moves in one direction.

    Individual non-soft targets are *not* required to be monotone: on ``build`` the divergence is
    smaller and the per-segment repeat penalties reorder choices between neighbouring settings.
    Demanding monotonicity per target would be over-fitting the fixture.
    """
    shares = [_rich_share(planner, e)[0] for e in _SWEEP]

    assert all(a <= b for a, b in zip(shares, shares[1:])), shares
    assert shares[0] < shares[2] < shares[-1], shares


def test_the_effect_is_weaker_on_motion_obvious_action_material(planner):
    """An honest limitation, pinned rather than hidden.

    Stage 5 deliberately motion-gates semantic action (``0.28 * semantic * motion_gate``), so on
    high-motion material the persisted reading is already close to the deterministic one and this
    control has little left to move. That is correct behaviour, not a defect, and the factor must
    not be retuned to manufacture drama here.
    """
    act = _action(1)[0]
    rhythm_delta = abs(planner._static_base_score(act, "rhythm")
                       - planner._static_base_score(dv.deterministic_candidate_view(act), "rhythm"))
    soft_delta = abs(planner._static_base_score(_rich(1)[0], "soft")
                     - planner._static_base_score(
                         dv.deterministic_candidate_view(_rich(1)[0]), "soft"))

    assert rhythm_delta < 0.10, rhythm_delta
    assert soft_delta > 0.30, soft_delta
    assert soft_delta > rhythm_delta * 4, (soft_delta, rhythm_delta)


def test_an_action_only_pool_still_plans_completely_at_every_setting(planner):
    """Weak effect must not mean broken output."""
    for emphasis in _SWEEP:
        plan = _plan(planner, _action(16), semantic_emphasis=emphasis)
        assert len(plan) == len(_DURATIONS), emphasis
        assert all(-1.0 <= i["score"] <= 2.0 for i in plan)


# ===========================================================================
# 3. IT INVENTS NOTHING — candidates Qwen never touched
# ===========================================================================


def _never_analysed(count=12):
    """Candidates whose `semantic` block is the deterministic stand-in, i.e. `ai_analyzed=False`.

    Built by running the deterministic reconstruction over raw primitives, which is exactly the
    shape `_build_candidate` produces before `_merge_semantic` ever runs.
    """
    pool = []
    for i in range(count):
        primitives = {"brightness": 0.30 + (i % 5) * 0.10, "contrast": 0.20 + (i % 4) * 0.15,
                      "saturation": 0.25 + (i % 3) * 0.20, "sharpness": 0.30 + (i % 6) * 0.10,
                      "motion": 0.10 + (i % 7) * 0.12, "colorfulness": 0.20 + (i % 4) * 0.18}
        candidate = dict(primitives)
        candidate.update({
            "id": f"det_{i:02d}", "video_file": f"C:/lib/det_{i % 5:02d}.mp4",
            "source_name": f"det_{i % 5:02d}", "duration": 4.0, "video_duration": 60.0,
            "center": 10.0 + i, "peak_time": 10.2 + i, "start": 8.0 + i,
        })
        candidate.update(dv.deterministic_scores(primitives))
        pool.append(candidate)
    return pool


def test_the_view_of_a_never_analysed_candidate_is_the_candidate(planner):
    for candidate in _never_analysed():
        assert dv.deterministic_candidate_view(candidate) == candidate


@pytest.mark.parametrize("emphasis", _SWEEP)
@pytest.mark.parametrize("target", ["drop", "soft", "build", "rhythm", "flow"])
def test_the_control_is_exactly_inert_on_never_analysed_candidates(planner, emphasis, target):
    """No manufactured "AI effect" where no AI reading exists.

    Exactly inert, not approximately: the deterministic view equals the candidate, so
    ``det + factor * (full - det)`` is ``det`` for every factor.
    """
    controls = fork_creative.CreativeProfile(semantic_emphasis=emphasis).scoring_controls()
    for candidate in _never_analysed(6):
        assert planner._effective_base_score(candidate, target, controls) == \
            planner._static_base_score(candidate, target)


def test_a_never_analysed_pool_plans_identically_at_every_emphasis(planner):
    pool = _never_analysed(16)
    baseline = _ids(_plan(planner, pool))
    for emphasis in _SWEEP:
        assert _ids(_plan(planner, pool, semantic_emphasis=emphasis)) == baseline, emphasis


# ===========================================================================
# 4. THE FORMULA AND ITS ORDERING
# ===========================================================================


@pytest.mark.parametrize("emphasis, factor", [(0, 0.0), (25, 0.5), (50, 1.0), (75, 1.5), (100, 2.0)])
def test_the_blend_is_the_accepted_formula(planner, emphasis, factor):
    controls = fork_creative.CreativeProfile(semantic_emphasis=emphasis).scoring_controls()
    for candidate in _rich(4) + _plain(4) + _action(4):
        for target in ("drop", "soft", "build", "rhythm", "flow"):
            full = planner._static_base_score(candidate, target)
            det = planner._static_base_score(dv.deterministic_candidate_view(candidate), target)
            expected = planner._clamp(det + factor * (full - det), lo=-1.0, hi=2.0)
            assert planner._effective_base_score(candidate, target, controls) == pytest.approx(
                expected), (emphasis, target, candidate["id"])


def test_emphasis_zero_is_the_deterministic_score(planner):
    controls = fork_creative.CreativeProfile(semantic_emphasis=0).scoring_controls()
    for candidate in _rich(4) + _plain(4):
        for target in ("soft", "flow"):
            assert planner._effective_base_score(candidate, target, controls) == pytest.approx(
                planner._clamp(
                    planner._static_base_score(dv.deterministic_candidate_view(candidate), target),
                    lo=-1.0, hi=2.0))


def test_energy_response_blends_against_the_semantic_adjusted_flow(planner):
    """The documented interaction, and the one easiest to get wrong.

    Blending a semantic-adjusted target against a *legacy* flow score would mix two different
    interpretations of the same candidate. Recomputed here in the documented order.
    """
    profile = fork_creative.CreativeProfile(semantic_emphasis=80, energy_response=80)
    controls = profile.scoring_controls()

    for candidate in _rich(3) + _plain(3) + _action(3):
        for target in ("drop", "soft", "build"):
            def semantic(t):
                full = planner._static_base_score(candidate, t)
                det = planner._static_base_score(dv.deterministic_candidate_view(candidate), t)
                return det + controls.semantic_factor * (full - det)

            expected = semantic("flow") + controls.energy_factor * (
                semantic(target) - semantic("flow"))
            expected = planner._clamp(expected, lo=-1.0, hi=2.0)

            assert planner._effective_base_score(candidate, target, controls) == pytest.approx(
                expected), (target, candidate["id"])


def test_motion_bias_is_applied_after_the_semantic_blend(planner):
    profile = fork_creative.CreativeProfile(semantic_emphasis=0, motion_bias=100)
    controls = profile.scoring_controls()

    for candidate in _rich(3) + _action(3):
        for target in ("soft", "drop"):
            det = planner._static_base_score(dv.deterministic_candidate_view(candidate), target)
            shift = (controls.motion_centered * fork_creative.MOTION_BIAS_COEFFICIENT
                     * (2.0 * planner._candidate_motion(candidate) - 1.0))
            assert planner._effective_base_score(candidate, target, controls) == pytest.approx(
                planner._clamp(det + shift, lo=-1.0, hi=2.0)), (target, candidate["id"])


def test_motion_bias_reads_a_primitive_semantic_emphasis_cannot_move(planner):
    """`motion` is a raw CV primitive Stage 5 never fuses, so the deterministic view carries the
    same value and Semantic Emphasis cannot change what Motion Bias sees."""
    for candidate in _rich(4) + _plain(4) + _action(4):
        assert planner._candidate_motion(dv.deterministic_candidate_view(candidate)) == \
            planner._candidate_motion(candidate)


def test_the_control_does_not_depend_on_the_seed(planner):
    pool = _rich() + _plain()
    for seed in (0, 101, 381944):
        plan = _plan(planner, pool, seed=seed, semantic_emphasis=80)
        controls = fork_creative.CreativeProfile(semantic_emphasis=80).scoring_controls()
        for item in plan:
            candidate = next(c for c in pool if c["id"] == item["candidate_id"])
            assert item["score"] == planner._effective_base_score(
                candidate, item["target"], controls)


def test_the_materialized_score_reports_the_effective_score(planner):
    """`plan["score"]` is diagnostic, but reporting the legacy score for a render that chose on an
    adjusted one would be a quietly misleading record."""
    pool = _rich() + _plain()
    for creative in ({"semantic_emphasis": 0}, {"semantic_emphasis": 100},
                     {"semantic_emphasis": 80, "energy_response": 20, "motion_bias": 70}):
        controls = fork_creative.CreativeProfile.from_mapping(creative).scoring_controls()
        plan = _plan(planner, pool, **creative)
        for item in plan:
            candidate = next(c for c in pool if c["id"] == item["candidate_id"])
            assert item["score"] == planner._effective_base_score(
                candidate, item["target"], controls), creative
        assert any(
            item["score"] != planner._static_base_score(
                next(c for c in pool if c["id"] == item["candidate_id"]), item["target"])
            for item in plan), creative


# ===========================================================================
# 5. THE MUSIC IS UNTOUCHED
# ===========================================================================


def test_semantic_emphasis_changes_nothing_about_the_music(planner):
    pool = _rich() + _plain()
    baseline = _plan(planner, pool)
    targets = _targets(baseline)

    for emphasis in _SWEEP:
        plan = _plan(planner, pool, semantic_emphasis=emphasis)
        assert _targets(plan) == targets, emphasis
        assert len(plan) == len(_DURATIONS), f"emphasis {emphasis} fell back"
        assert [i["audio_start"] for i in plan] == [i["audio_start"] for i in baseline]
        assert [i["final_duration"] for i in plan] == [i["final_duration"] for i in baseline]
        assert [i["start_time"] for i in plan] == [
            planner._materialize_clip(
                next(c for c in pool if c["id"] == i["candidate_id"]),
                {"index": i["index"], "target": i["target"], "duration": i["final_duration"],
                 "start": i["audio_start"], "end": i["audio_end"]},
                i["index"])["start_time"]
            for i in plan], "clip anchoring moved"


# ===========================================================================
# 6. L1A
# ===========================================================================


def _counts(planner, monkeypatch, pool, **creative):
    static: list = []
    views: list = []
    real_static = planner._static_base_score
    real_view = planner.fork_deterministic.deterministic_candidate_view
    monkeypatch.setattr(planner, "_static_base_score",
                        lambda c, t: (static.append(t), real_static(c, t))[1])
    monkeypatch.setattr(planner.fork_deterministic, "deterministic_candidate_view",
                        lambda c: (views.append(c.get("id")), real_view(c))[1])
    try:
        plan = _plan(planner, pool, **creative)
    finally:
        monkeypatch.undo()
    return len(static), len(views), plan


def test_deterministic_views_are_built_once_per_candidate(planner, monkeypatch):
    """Not once per (candidate, target), and not again for each materialised clip."""
    pool = _rich() + _plain()
    _, views, plan = _counts(planner, monkeypatch, pool, semantic_emphasis=100)

    assert views == len(pool), f"{views} views for {len(pool)} candidates"
    assert len(plan) == len(_DURATIONS)


def test_static_work_scales_with_targets_not_segments(planner, monkeypatch):
    """The L1A property, asserted as a count rather than a clock.

    With Semantic Emphasis on, each cell costs two evaluations — the persisted candidate and its
    deterministic view — but the table is still ``candidates × distinct targets``.
    """
    pool = _rich() + _plain()
    for creative, per_cell, flow_cols, per_clip in (
        ({"semantic_emphasis": 100}, 2, 0, 2),
        ({"semantic_emphasis": 100, "energy_response": 100}, 2, 1, 4),
        ({"motion_bias": 100}, 1, 0, 1),
    ):
        static, _, plan = _counts(planner, monkeypatch, pool, **creative)
        distinct = len(set(_targets(plan)))
        expected = len(pool) * (distinct * per_cell + flow_cols * per_cell) + len(plan) * per_clip

        assert static == expected, (creative, static, expected)
        assert static < len(pool) * len(_DURATIONS) * per_cell, "regressed towards segments x candidates"


def test_source_diversity_does_not_change_the_static_count(planner, monkeypatch):
    """PR1's dynamic/static boundary must survive PR2."""
    pool = _rich() + _plain()
    counts = {d: _counts(planner, monkeypatch, pool, semantic_emphasis=80, source_diversity=d)[0]
              for d in (0, 50, 100)}

    assert len(set(counts.values())) == 1, counts


def test_semantic_emphasis_alone_builds_no_flow_column(planner, monkeypatch):
    """Independent neutrality: moving one control must not activate another's optional work."""
    pool = _rich() + _plain()
    neutral_static, _, plan = _counts(planner, monkeypatch, pool)
    distinct = len(set(_targets(plan)))

    semantic_only, _, _ = _counts(planner, monkeypatch, pool, semantic_emphasis=100)
    assert semantic_only == len(pool) * distinct * 2 + len(plan) * 2, semantic_only

    with_energy, _, _ = _counts(planner, monkeypatch, pool,
                                semantic_emphasis=100, energy_response=100)
    assert with_energy > semantic_only, "Energy Response should still add its flow column"


def test_the_precomputed_path_matches_the_unprecomputed_path(planner):
    """`base_scores=None` is the defensive shape; under Semantic Emphasis it must resolve to the
    same effective score rather than silently dropping back to legacy scoring."""
    import numpy as np

    pool = _rich() + _plain()
    for creative in ({"semantic_emphasis": 0}, {"semantic_emphasis": 100},
                     {"semantic_emphasis": 75, "energy_response": 80, "motion_bias": 25},
                     {"seed": 101, "semantic_emphasis": 75, "source_diversity": 80}):
        beat_info = _beat_info(pool, **creative)
        candidates = [c for c in pool if c.get("video_file")]
        profiles = planner._build_segment_profiles(
            np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), beat_info)
        settings = planner.creative_profile(beat_info)
        controls = settings.scoring_controls()
        diversity = (None if settings.is_neutral_source_diversity()
                     else settings.source_diversity_factor())
        recent_ids, recent_videos, usage = deque(maxlen=10), deque(maxlen=5), Counter()
        chosen = []
        for i, profile in enumerate(profiles):
            candidate = planner._choose_candidate(
                candidates=candidates, profile=profile, recent_ids=recent_ids,
                recent_videos=recent_videos, usage=usage, index=i, seed=settings.seed,
                base_scores=None, controls=controls, source_diversity_factor=diversity)
            chosen.append(candidate["id"])
            recent_ids.append(candidate.get("id"))
            recent_videos.append(candidate.get("video_file"))
            usage[candidate.get("id")] += 1
            usage[candidate.get("video_file")] += 1

        assert chosen == _ids(_plan(planner, pool, **creative)), creative


def test_the_planner_holds_no_module_level_state():
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    assignments = {t.id for node in tree.body if isinstance(node, ast.Assign)
                   for t in node.targets if isinstance(t, ast.Name)}

    assert not assignments, f"the planner gained module-level state: {assignments}"
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))]
    body = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                            and n.name == "build_planned_clip_sequence"))
    for forbidden in ("lru_cache", "functools.cache", "global "):
        assert forbidden not in body, forbidden


# ===========================================================================
# 7. INTERACTION WITH CORE / PR1
# ===========================================================================


_GRID = [
    {"semantic_emphasis": 0},
    {"semantic_emphasis": 100},
    {"semantic_emphasis": 80, "energy_response": 20},
    {"semantic_emphasis": 80, "motion_bias": 90},
    {"semantic_emphasis": 80, "source_diversity": 100},
    {"semantic_emphasis": 75, "seed": 101},
    {"seed": 101, "cut_density": 75, "micro_cuts": 70, "energy_response": 80,
     "motion_bias": 25, "source_diversity": 80, "semantic_emphasis": 75},
]


@pytest.mark.parametrize("creative", _GRID)
def test_every_combination_is_deterministic_and_complete(planner, creative):
    pool = _rich() + _plain() + _action(4)
    first = _plan(planner, pool, **creative)
    second = _plan(planner, pool, **creative)

    assert _ids(first) == _ids(second), creative
    assert len(first) == len(_DURATIONS), f"{creative} fell back"
    assert _targets(first) == _targets(_plan(planner, pool))
    for item in first:
        assert item["video_file"]
        assert item["final_duration"] > 0
        assert -1.0 <= item["score"] <= 2.0


def test_the_full_combined_profile_still_varies_by_seed(planner):
    pool = _rich() + _plain() + _action(4)
    combined = {"cut_density": 75, "micro_cuts": 70, "energy_response": 80,
                "motion_bias": 25, "source_diversity": 80, "semantic_emphasis": 75}
    legacy = _ids(_plan(planner, pool, **combined))
    seeded = _ids(_plan(planner, pool, seed=101, **combined))

    assert seeded == _ids(_plan(planner, pool, seed=101, **combined))
    assert seeded != legacy


def test_one_render_s_emphasis_cannot_leak_into_the_next(planner):
    pool = _rich() + _plain()
    low = _ids(_plan(planner, pool, semantic_emphasis=0))
    _plan(planner, pool, semantic_emphasis=100)
    neutral = _ids(_plan(planner, pool))
    _plan(planner, pool, semantic_emphasis=25)
    low_again = _ids(_plan(planner, pool, semantic_emphasis=0))

    assert low == low_again
    assert neutral == _ids(_plan(planner, pool))
    assert neutral != low


# ===========================================================================
# 8. DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_semantic_emphasis_sweep(planner):
    """Readable evidence. To read it::

        python -m pytest tests/test_semantic_emphasis.py -s -k practical_diagnostic
    """
    lines = []
    for emphasis in _SWEEP:
        soft, soft_total = _rich_share(planner, emphasis, "soft")
        total, plan_len = _rich_share(planner, emphasis)
        factor = fork_creative.CreativeProfile(semantic_emphasis=emphasis).semantic_emphasis_factor()
        lines.append(f"  emphasis {emphasis:3d}  factor {factor:4.2f}  "
                     f"semantically-rich chosen: {soft}/{soft_total} on soft, "
                     f"{total}/{plan_len} overall")
    act = _action(1)[0]
    rhythm = abs(planner._static_base_score(act, "rhythm")
                 - planner._static_base_score(dv.deterministic_candidate_view(act), "rhythm"))
    soft = abs(planner._static_base_score(_rich(1)[0], "soft")
               - planner._static_base_score(dv.deterministic_candidate_view(_rich(1)[0]), "soft"))
    print("\nSemantic Emphasis on a rich-vs-plain pool:\n" + "\n".join(lines))
    print(f"  target-dependence: |semantic - deterministic| = {soft:.4f} on soft, "
          f"{rhythm:.4f} on rhythm (motion-gated)")

    assert len(lines) == len(_SWEEP)
