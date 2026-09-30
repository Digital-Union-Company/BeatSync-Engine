"""Source Diversity — the Stage 6 **dynamic** creative control (Creative Controls Extra).

Everything the planner does after the static score is history-dependent: it reads the running
``usage`` counter and the ``recent_ids`` / ``recent_videos`` windows. Source Diversity lives there
and only there, which produces two obligations this suite exists to pin:

* it scales the two **source-video-level** reuse penalties and *nothing else* — the two
  candidate-level protections are byte-identical at every setting, because diversity decides how
  willing the edit is to return to the same *source*, never whether it may repeat a *moment*;
* it must not touch the L1A static table. Changing it alone may not cause one extra
  ``_static_base_score`` evaluation, because those are keyed on (candidate, target) and diversity is
  neither.

The planner needs numpy but has no relative imports, so it is loaded by path — the technique the
Core suites already use, which keeps ``auto_mode/__init__`` (librosa, cupy, logger) out.
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
    spec = importlib.util.spec_from_file_location("stage6_source_diversity", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A pool with more sources than a plan can comfortably use, and a quality gradient
# ---------------------------------------------------------------------------
#
# 24 sources x 4 moments over 40 segments. The gradient is gentle but real, so neutral naturally
# concentrates on the stronger sources and Source Diversity has to *pay* something to spread out —
# which is exactly the trade the control exists to expose.

_SOURCES = 24
_PER_SOURCE = 4


def _pool() -> list[dict]:
    pool = []
    for s in range(_SOURCES):
        for k in range(_PER_SOURCE):
            i = s * _PER_SOURCE + k
            pool.append({
                "id": f"c{s:02d}_{k}", "video_file": f"C:/lib/src_{s:02d}.mp4",
                "source_name": f"src_{s:02d}", "duration": 4.0, "video_duration": 60.0,
                "center": 10.0 + i, "peak_time": 10.2 + i, "start": 8.0 + i,
                "quality_score": 0.80 - s * 0.006 + k * 0.001,
                "action_score": 0.60 - s * 0.005 + k * 0.001,
                "beauty_score": 0.60 - s * 0.005,
                "tension_score": 0.45, "soft_score": 0.40,
                "motion": 0.50, "brightness": 0.60, "tags": ["action"],
                "semantic": {"character_focus": 0.4, "combat": 0.2, "chase": 0.2,
                             "explosion": 0.1},
                "ai_analyzed": True,
            })
    return pool


_CUTS = [float(t) for t in range(0, 41)]
_DURATIONS = [1.0] * (len(_CUTS) - 1)


def _beat_info(candidates, **creative) -> dict:
    n = len(_CUTS)
    info = {
        "times": [float(t) for t in range(n)],
        "sections": [{"type": "verse", "start": 0.0, "end": 20.0, "energy": 0.5},
                     {"type": "drop", "start": 20.0, "end": 40.0, "energy": 0.9}],
        "energy_profile": {"wave": [0.3 + 0.015 * i for i in range(n)],
                           "arc": [0.2 + 0.015 * i for i in range(n)]},
        "rhythm_data": {"impact_strength": [0.3 + 0.015 * i for i in range(n)],
                        "combined_strength": [0.3 + 0.014 * i for i in range(n)],
                        "novelty_strength": [0.2 + 0.010 * i for i in range(n)]},
        "video_analysis": {"candidates": candidates},
    }
    if creative:
        info["creative"] = dict(creative)
    return info


def _plan(planner, **creative):
    return planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(_pool(), **creative), video_files=[])


def _ids(plan):
    return [item["candidate_id"] for item in plan]


def _videos(plan):
    return [item["video_file"] for item in plan]


_SWEEP = (0, 25, 50, 75, 100)


# ===========================================================================
# 1. NEUTRAL IS CURRENT MAIN
# ===========================================================================


def test_neutral_diversity_plans_exactly_what_no_creative_state_plans(planner):
    baseline = _ids(_plan(planner))

    assert _ids(_plan(planner, source_diversity=50)) == baseline
    assert _ids(_plan(planner, seed=0, cut_density=50, energy_response=50,
                      motion_bias=50, source_diversity=50, micro_cuts=50)) == baseline


def test_a_bus_without_the_new_fields_still_plans_the_legacy_way(planner):
    """A Creative Controls Core bus carries no `source_diversity`; it must read back neutral."""
    baseline = _ids(_plan(planner))

    assert _ids(_plan(planner, seed=0, cut_density=50, energy_response=50,
                      motion_bias=50)) == baseline
    assert _ids(_plan(planner, seed=0)) == baseline


def test_the_neutral_branch_uses_the_untouched_penalty_expressions():
    """Pinned at the source: the neutral branch must not multiply anything by a factor.

    Equality of results is necessary but not sufficient — the contract is that a default render runs
    the *existing arithmetic*, not a neutral-valued version of the new arithmetic.
    """
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_adjusted_score")
    branch = next(n for n in ast.walk(fn)
                  if isinstance(n, ast.If) and "source_diversity_factor is None" in ast.unparse(n.test))
    neutral = "\n".join(ast.unparse(node) for node in branch.body)

    assert "source_diversity_factor" not in neutral, neutral
    for token in ("score -= 0.28", "score -= 0.1", "min(0.28, usage[cid] * 0.1)",
                  "min(0.18, usage[video_file] * 0.012)"):
        assert token in neutral, token


# ===========================================================================
# 2. THE CONTROL WORKS, MONOTONICALLY
# ===========================================================================


@pytest.fixture(scope="module")
def sweep(planner):
    return {d: _plan(planner, source_diversity=d) for d in _SWEEP}


def test_unique_source_count_rises_monotonically_with_diversity(sweep):
    unique = [len(set(_videos(sweep[d]))) for d in _SWEEP]

    assert all(a <= b for a, b in zip(unique, unique[1:])), unique
    assert unique[0] < unique[2] < unique[-1], unique


def test_the_extremes_move_source_usage_substantially(sweep):
    low, neutral, high = (len(set(_videos(sweep[d]))) for d in (0, 50, 100))

    assert low <= neutral * 0.85, f"diversity 0 barely concentrated anything: {low} vs {neutral}"
    assert high >= neutral * 1.3, f"diversity 100 barely spread anything: {high} vs {neutral}"
    assert high <= _SOURCES, "cannot use more sources than exist"


def test_the_busiest_source_is_used_less_as_diversity_rises(sweep):
    busiest = [max(Counter(_videos(sweep[d])).values()) for d in _SWEEP]

    assert all(a >= b for a, b in zip(busiest, busiest[1:])), busiest
    assert busiest[0] > busiest[-1]


def test_low_diversity_really_does_permit_more_source_reuse(sweep):
    """The left-hand label, measured: fewer distinct sources carrying the same 40 segments."""
    counts_low = Counter(_videos(sweep[0]))
    counts_high = Counter(_videos(sweep[100]))

    assert len(counts_low) < len(counts_high)
    # concentration, not just breadth
    hhi = lambda c: sum((n / sum(c.values())) ** 2 for n in c.values())  # noqa: E731
    assert hhi(counts_low) > hhi(counts_high)


# ===========================================================================
# 3. CANDIDATE-LEVEL PROTECTION IS NOT SCALED — THE LOAD-BEARING PART
# ===========================================================================


def _profile(duration: float = 1.0, target: str = "flow") -> dict:
    return {"target": target, "duration": duration, "start": 0.0, "end": duration}


@pytest.mark.parametrize("diversity", [0, 25, 50, 75, 100])
def test_the_recent_candidate_penalty_is_exactly_0_28_at_every_setting(planner, diversity):
    candidate = {"id": "c1", "video_file": "v1.mp4", "duration": 4.0}
    factor = (None if diversity == 50
              else fork_creative.CreativeProfile(source_diversity=diversity).source_diversity_factor())

    clean = planner._adjusted_score(
        candidate, _profile(), deque(maxlen=10), deque(maxlen=5), Counter(),
        base_score=1.0, source_diversity_factor=factor)
    repeated = planner._adjusted_score(
        candidate, _profile(), deque(["c1"], maxlen=10), deque(maxlen=5), Counter(),
        base_score=1.0, source_diversity_factor=factor)

    assert clean - repeated == pytest.approx(0.28), diversity


@pytest.mark.parametrize("diversity", [0, 25, 50, 75, 100])
def test_the_candidate_usage_penalty_is_unscaled_at_every_setting(planner, diversity):
    candidate = {"id": "c1", "video_file": "v1.mp4", "duration": 4.0}
    factor = (None if diversity == 50
              else fork_creative.CreativeProfile(source_diversity=diversity).source_diversity_factor())

    def adjusted(usage):
        return planner._adjusted_score(
            candidate, _profile(), deque(maxlen=10), deque(maxlen=5), Counter(usage),
            base_score=1.0, source_diversity_factor=factor)

    assert adjusted({}) - adjusted({"c1": 2}) == pytest.approx(0.20)   # 2 * 0.10
    assert adjusted({}) - adjusted({"c1": 9}) == pytest.approx(0.28)   # capped


@pytest.mark.parametrize("diversity", [0, 25, 75, 100])
def test_the_source_penalties_are_the_only_thing_that_moves(planner, diversity):
    """Scaled exactly as documented: `factor * legacy`, on both source terms."""
    candidate = {"id": "c1", "video_file": "v1.mp4", "duration": 4.0}
    factor = fork_creative.CreativeProfile(source_diversity=diversity).source_diversity_factor()

    def adjusted(recent_videos=(), usage=None, f=None):
        return planner._adjusted_score(
            candidate, _profile(), deque(maxlen=10), deque(recent_videos, maxlen=5),
            Counter(usage or {}), base_score=1.0, source_diversity_factor=f)

    # recent-source term
    legacy_recent = adjusted() - adjusted(recent_videos=["v1.mp4"])
    scaled_recent = adjusted(f=factor) - adjusted(recent_videos=["v1.mp4"], f=factor)
    assert legacy_recent == pytest.approx(0.10)
    assert scaled_recent == pytest.approx(0.10 * factor)

    # source-usage term, below and at its cap
    legacy_usage = adjusted() - adjusted(usage={"v1.mp4": 5})
    scaled_usage = adjusted(f=factor) - adjusted(usage={"v1.mp4": 5}, f=factor)
    assert legacy_usage == pytest.approx(0.06)
    assert scaled_usage == pytest.approx(0.06 * factor)

    legacy_cap = adjusted() - adjusted(usage={"v1.mp4": 99})
    scaled_cap = adjusted(f=factor) - adjusted(usage={"v1.mp4": 99}, f=factor)
    assert legacy_cap == pytest.approx(0.18)
    assert scaled_cap == pytest.approx(0.18 * factor), "the cap is scaled, not re-capped"


def test_no_setting_lets_the_same_moment_repeat_back_to_back(sweep):
    for diversity, plan in sweep.items():
        ids = _ids(plan)
        assert all(a != b for a, b in zip(ids, ids[1:])), f"diversity {diversity}: {ids}"


def test_diversity_does_not_reduce_candidate_variety(sweep):
    """Spreading across sources must not come at the cost of reusing moments more."""
    for diversity, plan in sweep.items():
        assert len(set(_ids(plan))) == len(plan), diversity


def test_a_catastrophically_worse_candidate_never_wins_on_novelty_alone(planner):
    """The control buys breadth, not nonsense. A candidate from an unused source that is far below
    everything else must still lose at maximum diversity."""
    pool = _pool()
    junk = {
        "id": "junk_00", "video_file": "C:/lib/junk.mp4", "source_name": "junk",
        "duration": 4.0, "video_duration": 60.0, "center": 5.0, "peak_time": 5.0, "start": 4.0,
        "quality_score": 0.02, "action_score": 0.0, "beauty_score": 0.0,
        "tension_score": 0.0, "soft_score": 0.0, "motion": 0.0, "brightness": 0.02,
        "tags": [], "semantic": {"character_focus": 0.0, "combat": 0.0, "chase": 0.0,
                                 "explosion": 0.0},
        "ai_analyzed": False,
    }
    plan = planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(pool + [junk], source_diversity=100), video_files=[])

    assert len(plan) == len(_DURATIONS)
    assert "junk_00" not in _ids(plan), "maximum diversity promoted a visibly broken candidate"


# ===========================================================================
# 4. PLAN HEALTH, DETERMINISM AND THE SEED
# ===========================================================================


@pytest.mark.parametrize("diversity", _SWEEP)
def test_every_setting_produces_a_complete_well_formed_plan(planner, diversity):
    plan = _plan(planner, source_diversity=diversity)

    assert len(plan) == len(_DURATIONS), "a short plan means the planner fell back"
    for item in plan:
        assert item["video_file"]
        assert item["final_duration"] > 0
        assert item["start_time"] >= 0.0
        assert -1.0 <= item["score"] <= 2.0


def test_diversity_changes_nothing_about_the_music(planner):
    baseline = _plan(planner)
    targets = [i["target"] for i in baseline]

    for diversity in _SWEEP:
        plan = _plan(planner, source_diversity=diversity)
        assert [i["target"] for i in plan] == targets, diversity
        assert [i["audio_start"] for i in plan] == [i["audio_start"] for i in baseline]
        assert [i["final_duration"] for i in plan] == [i["final_duration"] for i in baseline]


@pytest.mark.parametrize("diversity", _SWEEP)
def test_every_setting_is_deterministic(planner, diversity):
    assert _ids(_plan(planner, source_diversity=diversity)) == _ids(
        _plan(planner, source_diversity=diversity))


def test_a_positive_seed_still_varies_and_reproduces_under_diversity(planner):
    legacy = _ids(_plan(planner, source_diversity=80))
    a = _ids(_plan(planner, seed=101, source_diversity=80))
    b = _ids(_plan(planner, seed=202, source_diversity=80))

    assert a == _ids(_plan(planner, seed=101, source_diversity=80)), "seed stopped reproducing"
    assert a != legacy and b != legacy
    assert a != b


def test_one_render_s_diversity_cannot_leak_into_the_next(planner):
    low = _ids(_plan(planner, source_diversity=0))
    _plan(planner, source_diversity=100)
    neutral = _ids(_plan(planner))
    _plan(planner, source_diversity=25)
    low_again = _ids(_plan(planner, source_diversity=0))
    neutral_again = _ids(_plan(planner))

    assert low == low_again
    assert neutral == neutral_again
    assert neutral != low


# ===========================================================================
# 5. THE L1A BOUNDARY — DIVERSITY IS DYNAMIC AND MUST STAY OUT OF THE TABLE
# ===========================================================================


def _static_evaluations(planner, monkeypatch, **creative) -> int:
    calls: list[str] = []
    real = planner._static_base_score
    monkeypatch.setattr(planner, "_static_base_score",
                        lambda c, t: (calls.append(t), real(c, t))[1])
    try:
        _plan(planner, **creative)
    finally:
        monkeypatch.undo()
    return len(calls)


def test_diversity_does_not_change_the_static_evaluation_count(planner, monkeypatch):
    """The critical dynamic/static boundary, asserted as a count rather than by inspection."""
    counts = {d: _static_evaluations(planner, monkeypatch, source_diversity=d)
              for d in (0, 50, 100)}

    assert len(set(counts.values())) == 1, counts
    assert counts[50] == _static_evaluations(planner, monkeypatch)


def test_diversity_does_not_trigger_energy_response_flow_work(planner, monkeypatch):
    """Independent neutrality: moving one control must not activate another's optional work."""
    neutral = _static_evaluations(planner, monkeypatch)
    diversity_only = _static_evaluations(planner, monkeypatch, source_diversity=100)
    energy_too = _static_evaluations(planner, monkeypatch, source_diversity=100,
                                     energy_response=100)

    assert diversity_only == neutral
    assert energy_too > neutral, "Energy Response should still add its flow column"


def test_the_precomputed_path_matches_the_unprecomputed_path(planner):
    """`base_scores=None` is the fallback shape; it must resolve to the same plan under diversity."""
    import numpy as np

    for diversity in (0, 50, 100):
        beat_info = _beat_info(_pool(), source_diversity=diversity)
        candidates = [c for c in beat_info["video_analysis"]["candidates"] if c.get("video_file")]
        profiles = planner._build_segment_profiles(
            np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), beat_info)
        settings = planner.creative_profile(beat_info)
        factor = (None if settings.is_neutral_source_diversity()
                  else settings.source_diversity_factor())
        recent_ids, recent_videos, usage = deque(maxlen=10), deque(maxlen=5), Counter()
        chosen = []
        for i, profile in enumerate(profiles):
            candidate = planner._choose_candidate(
                candidates=candidates, profile=profile, recent_ids=recent_ids,
                recent_videos=recent_videos, usage=usage, index=i, seed=settings.seed,
                base_scores=None, controls=settings.scoring_controls(),
                source_diversity_factor=factor)
            chosen.append(candidate["id"])
            recent_ids.append(candidate.get("id"))
            recent_videos.append(candidate.get("video_file"))
            usage[candidate.get("id")] += 1
            usage[candidate.get("video_file")] += 1

        assert chosen == _ids(_plan(planner, source_diversity=diversity)), diversity


def test_diversity_is_not_part_of_the_static_scoring_controls():
    """`ScoringControls` feeds the precompute table. A dynamic control inside it would be one
    refactor away from silently becoming a table key."""
    controls = fork_creative.CreativeProfile(source_diversity=0).scoring_controls()

    assert controls is fork_creative.NEUTRAL_SCORING
    assert not hasattr(controls, "source_diversity_factor")
    assert (fork_creative.CreativeProfile(source_diversity=0).scoring_controls()
            == fork_creative.CreativeProfile(source_diversity=100).scoring_controls())


def test_the_planner_holds_no_module_level_diversity_state():
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    assignments = {t.id for node in tree.body if isinstance(node, ast.Assign)
                   for t in node.targets if isinstance(t, ast.Name)}

    assert not assignments, f"the planner gained module-level state: {assignments}"
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))]


# ===========================================================================
# 6. DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_source_diversity_sweep(planner, sweep):
    """Readable evidence. To read it::

        python -m pytest tests/test_source_diversity.py -s -k practical_diagnostic
    """
    lines = []
    for diversity in _SWEEP:
        plan = sweep[diversity]
        videos = _videos(plan)
        counts = Counter(videos)
        factor = fork_creative.CreativeProfile(source_diversity=diversity).source_diversity_factor()
        hhi = sum((n / len(videos)) ** 2 for n in counts.values())
        lines.append(
            f"  diversity {diversity:3d}  factor {factor:5.3f}  unique sources {len(counts):3d}/"
            f"{_SOURCES}  busiest {max(counts.values()):2d}  HHI {hhi:.4f}  "
            f"unique moments {len(set(_ids(plan))):3d}/{len(plan)}")
    print(f"\nSource Diversity over {len(_DURATIONS)} segments, {_SOURCES} sources "
          f"x {_PER_SOURCE} moments:\n" + "\n".join(lines))

    assert len(lines) == len(_SWEEP)
