"""L1A — Stage 6 precomputes the static half of the candidate score.

A measured real run planned 148 segments over 9241 candidates: 1,367,668 evaluations of arithmetic
that only ever depended on the candidate and the segment's *target*, of which there are at most five
distinct values in a plan. L1A computes that half once per (candidate, target) and keeps every
dynamic penalty exactly where it was.

This suite is about **semantic equivalence**, not speed. There is no wall-clock assertion anywhere;
the one performance-shaped test counts static evaluations, which is a property of the algorithm
rather than of the machine.

The planner needs numpy but has no relative imports, so it is loaded by path — the same technique
`test_creative_seed.py` uses — which keeps `auto_mode/__init__` (librosa, cupy, logger) out of it.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import random
from collections import Counter, deque

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_planner_l1a", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A pool wide enough that penalties really move the winner around
# ---------------------------------------------------------------------------


def _pool(count: int = 48) -> list[dict]:
    pool = []
    for i in range(count):
        pool.append({
            "id": f"cand_{i:03d}",
            "video_file": f"C:/library/source_{i % 9:02d}.mp4",
            "source_name": f"source_{i % 9:02d}",
            "duration": 4.0 if i % 11 else 0.4,      # a few short ones, for the duration penalty
            "video_duration": 60.0,
            "center": 10.0 + i,
            "peak_time": 10.5 + i,
            "start": 8.0 + i,
            "quality_score": 0.60 + (i % 7) * 0.01,
            "action_score": 0.50 + (i % 5) * 0.02,
            "beauty_score": 0.55 + (i % 4) * 0.02,
            "tension_score": 0.40 + (i % 6) * 0.01,
            "soft_score": 0.30 + (i % 3) * 0.02,
            "motion": 0.45 + (i % 8) * 0.01,
            "brightness": 0.55,
            "tags": ["action"] if i % 3 == 0 else (["soft"] if i % 3 == 1 else ["tension"]),
            "semantic": {"character_focus": 0.4, "combat": 0.3, "chase": 0.2, "explosion": 0.1},
            "ai_analyzed": i % 2 == 0,
        })
    return pool


_CUTS = [float(t) for t in range(0, 17)]
_DURATIONS = [1.0] * (len(_CUTS) - 1)


def _beat_info(seed=None, candidates=None) -> dict:
    info = {
        "times": [float(t) for t in range(0, 17)],
        "sections": [{"type": "intro", "start": 0.0, "end": 4.0, "energy": 0.2},
                     {"type": "verse", "start": 4.0, "end": 8.0, "energy": 0.5},
                     {"type": "bridge", "start": 8.0, "end": 12.0, "energy": 0.7},
                     {"type": "drop", "start": 12.0, "end": 16.0, "energy": 0.9}],
        "energy_profile": {
            "wave": [0.20, 0.25, 0.30, 0.35, 0.45, 0.50, 0.55, 0.60,
                     0.62, 0.66, 0.70, 0.74, 0.86, 0.90, 0.88, 0.80, 0.70],
            "arc": [0.15, 0.20, 0.25, 0.30, 0.40, 0.45, 0.50, 0.58,
                    0.62, 0.66, 0.70, 0.72, 0.80, 0.84, 0.82, 0.75, 0.65],
        },
        "rhythm_data": {
            "impact_strength": [0.20, 0.25, 0.30, 0.35, 0.45, 0.50, 0.55, 0.60,
                                0.62, 0.68, 0.72, 0.75, 0.86, 0.90, 0.88, 0.78, 0.68],
            "combined_strength": [0.20, 0.28, 0.32, 0.38, 0.46, 0.52, 0.58, 0.62,
                                  0.64, 0.68, 0.72, 0.76, 0.84, 0.88, 0.86, 0.76, 0.66],
            "novelty_strength": [0.15, 0.20, 0.25, 0.30, 0.38, 0.44, 0.50, 0.58,
                                 0.70, 0.72, 0.74, 0.70, 0.60, 0.55, 0.50, 0.45, 0.40],
        },
        "video_analysis": {"candidates": _pool() if candidates is None else candidates},
    }
    if seed is not None:
        info["creative"] = {"seed": seed}
    return info


def _plan(planner, seed=None, **over):
    return planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(seed, **over), video_files=[])


def _ids(plan):
    return [item["candidate_id"] for item in plan]


def _reference_plan(planner, seed=0):
    """The full-score path: `_choose_candidate` with **no** precomputed table, i.e. today's code.

    `base_scores=None` makes `_adjusted_score` call `_score_candidate` per candidate per segment,
    which is exactly what main did before L1A. Everything else — profiles, penalties, RNG, seeded
    selection — is the same production code, so any divergence is the precomputation's fault.
    """
    import numpy as np

    beat_info = _beat_info(seed)
    candidates = [c for c in beat_info["video_analysis"]["candidates"] if c.get("video_file")]
    profiles = planner._build_segment_profiles(
        np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), beat_info)
    recent_ids, recent_videos, usage = deque(maxlen=10), deque(maxlen=5), Counter()
    chosen = []
    for i, profile in enumerate(profiles):
        candidate = planner._choose_candidate(
            candidates=candidates, profile=profile, recent_ids=recent_ids,
            recent_videos=recent_videos, usage=usage, index=i,
            seed=planner.creative_seed(beat_info), base_scores=None)
        if not candidate:
            continue
        chosen.append(candidate["id"])
        recent_ids.append(candidate.get("id"))
        recent_videos.append(candidate.get("video_file"))
        usage[candidate.get("id")] += 1
        usage[candidate.get("video_file")] += 1
    return chosen


# ======================================================================================
# 1-2. equivalence: the precomputed path chooses exactly what the full-score path chooses
# ======================================================================================


def test_seed_zero_plan_is_identical_to_the_full_score_path(planner):
    plan = _plan(planner, seed=0)

    assert len(plan) == len(_DURATIONS), "the plan must still be complete"
    assert _ids(plan) == _reference_plan(planner, seed=0)


def test_absent_seed_matches_seed_zero_and_the_reference(planner):
    assert _ids(_plan(planner, seed=None)) == _ids(_plan(planner, seed=0))
    assert _ids(_plan(planner, seed=None)) == _reference_plan(planner, seed=0)


@pytest.mark.parametrize("seed", [1, 101, 202, 381944])
def test_positive_seed_plans_are_identical_to_the_full_score_path(planner, seed):
    plan = _plan(planner, seed=seed)

    assert len(plan) == len(_DURATIONS)
    assert _ids(plan) == _reference_plan(planner, seed=seed)


def test_positive_seeds_still_differ_from_legacy_and_from_each_other(planner):
    """Equivalence must not have been achieved by flattening variation into the legacy plan."""
    legacy = _ids(_plan(planner, seed=0))
    a = _ids(_plan(planner, seed=101))
    b = _ids(_plan(planner, seed=202))

    assert a != legacy and b != legacy
    assert a != b


def test_a_plan_is_reproducible_across_repeated_calls(planner):
    for seed in (0, 101):
        assert _ids(_plan(planner, seed=seed)) == _ids(_plan(planner, seed=seed))


# ======================================================================================
# 3-4. the candidate pool itself is untouched
# ======================================================================================


def test_candidate_order_and_count_reaching_the_chooser_are_unchanged(planner, monkeypatch):
    """Every segment must see the same list object content, in the same order, at full length."""
    pool = _pool()
    expected_ids = [c["id"] for c in pool]
    seen: list[list[str]] = []
    real = planner._choose_candidate

    def spy(**kwargs):
        seen.append([c["id"] for c in kwargs["candidates"]])
        return real(**kwargs)

    monkeypatch.setattr(planner, "_choose_candidate", spy)
    plan = planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(0, candidates=pool), video_files=[])

    assert len(plan) == len(_DURATIONS)
    assert len(seen) == len(_DURATIONS), "one chooser call per segment, as before"
    for order in seen:
        assert order == expected_ids, "candidates were reordered, filtered or deduplicated"
        assert len(order) == len(pool)
    # and the caller's own list was not mutated
    assert [c["id"] for c in pool] == expected_ids


def test_the_base_score_table_is_aligned_with_the_candidate_order(planner, monkeypatch):
    """Position `i` in the table must be candidate `i`'s score — the alignment L1A relies on."""
    pool = _pool()
    captured: list[tuple] = []
    real = planner._choose_candidate

    def spy(**kwargs):
        captured.append((list(kwargs["candidates"]), kwargs["base_scores"], kwargs["profile"]))
        return real(**kwargs)

    monkeypatch.setattr(planner, "_choose_candidate", spy)
    planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(0, candidates=pool), video_files=[])

    for candidates, base_scores, profile in captured:
        assert base_scores is not None and len(base_scores) == len(candidates)
        for position, candidate in enumerate(candidates):
            assert base_scores[position] == planner._score_candidate(candidate, profile)


def test_a_misaligned_table_is_ignored_rather_than_trusted(planner):
    """Defensive: a future caller must not be able to score candidates against the wrong table."""
    import numpy as np

    beat_info = _beat_info(0)
    candidates = list(beat_info["video_analysis"]["candidates"])
    profiles = planner._build_segment_profiles(
        np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), beat_info)

    honest = planner._choose_candidate(
        candidates=candidates, profile=profiles[0], recent_ids=deque(maxlen=10),
        recent_videos=deque(maxlen=5), usage=Counter(), index=0, seed=0, base_scores=None)
    wrong_length = planner._choose_candidate(
        candidates=candidates, profile=profiles[0], recent_ids=deque(maxlen=10),
        recent_videos=deque(maxlen=5), usage=Counter(), index=0, seed=0,
        base_scores=(99.0, 99.0))

    assert wrong_length is honest


# ======================================================================================
# 5. every dynamic penalty still applies, per segment
# ======================================================================================


def _profile(duration: float = 1.0, target: str = "flow") -> dict:
    return {"target": target, "duration": duration, "start": 0.0, "end": duration}


def test_dynamic_penalties_apply_to_a_supplied_base_score(planner):
    """The penalties are unchanged and must bite on a precomputed base score exactly as before."""
    candidate = {"id": "c1", "video_file": "v1.mp4", "duration": 4.0}
    profile = _profile()
    base = 1.0

    def adjusted(recent_ids=(), recent_videos=(), usage=None, cand=candidate, prof=profile):
        return planner._adjusted_score(
            cand, prof, deque(recent_ids, maxlen=10), deque(recent_videos, maxlen=5),
            Counter(usage or {}), base_score=base)

    assert adjusted() == pytest.approx(1.0)
    assert adjusted(recent_ids=["c1"]) == pytest.approx(1.0 - 0.28)          # recent candidate
    assert adjusted(recent_videos=["v1.mp4"]) == pytest.approx(1.0 - 0.10)   # recent video
    assert adjusted(usage={"c1": 2}) == pytest.approx(1.0 - 0.20)            # candidate usage
    assert adjusted(usage={"c1": 9}) == pytest.approx(1.0 - 0.28)            # usage cap
    assert adjusted(usage={"v1.mp4": 5}) == pytest.approx(1.0 - 0.06)        # source usage
    assert adjusted(usage={"v1.mp4": 99}) == pytest.approx(1.0 - 0.18)       # source usage cap

    # duration suitability: a candidate far shorter than the segment is penalised
    short = {"id": "c1", "video_file": "v1.mp4", "duration": 0.4}
    assert adjusted(cand=short, prof=_profile(duration=1.0)) == pytest.approx(1.0 - 0.18)
    assert adjusted(cand=short, prof=_profile(duration=0.5)) == pytest.approx(1.0)


def test_supplying_the_base_score_is_equivalent_to_computing_it(planner):
    candidate = _pool()[3]
    profile = _profile(duration=1.0, target="drop")
    recent_ids, recent_videos = deque(["cand_003"], maxlen=10), deque(["C:/library/source_03.mp4"], maxlen=5)
    usage = Counter({"cand_003": 2, "C:/library/source_03.mp4": 4})

    computed = planner._adjusted_score(candidate, profile, recent_ids, recent_videos, usage)
    supplied = planner._adjusted_score(
        candidate, profile, recent_ids, recent_videos, usage,
        base_score=planner._static_base_score(candidate, "drop"))

    assert supplied == computed


def test_penalties_still_change_the_chosen_candidate_in_a_real_plan(planner):
    """End-to-end: consecutive segments must not all collapse onto one candidate."""
    ids = _ids(_plan(planner, seed=0))

    assert len(set(ids)) > 1, "the repeat/usage penalties stopped working"
    assert all(a != b for a, b in zip(ids, ids[1:])), "the same candidate was used back-to-back"


# ======================================================================================
# 6. the legacy RNG stream is untouched
# ======================================================================================


class _CountingRandom(random.Random):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.draws = 0

    def random(self):  # noqa: D102 - counting wrapper
        self.draws += 1
        return super().random()


def test_legacy_path_draws_exactly_one_random_per_candidate_per_segment(planner, monkeypatch):
    pool = _pool()
    made: list[tuple] = []
    real = planner._stable_rng

    def spy(*parts):
        rng = _CountingRandom(real(*parts).randrange(2 ** 32))
        made.append((parts, rng))
        return rng

    # seed the counting RNG deterministically from the real one so behaviour stays reproducible
    monkeypatch.setattr(planner, "_stable_rng", spy)
    planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(0, candidates=pool), video_files=[])

    assert len(made) == len(_DURATIONS), "one RNG per segment, as before"
    for parts, rng in made:
        assert len(parts) == 3, "the legacy stream must stay `(index, target, start)` — no seed"
        assert rng.draws == len(pool), "exactly one draw per candidate, in candidate order"


def test_positive_seed_path_keeps_its_own_stream_shape(planner, monkeypatch):
    parts_seen: list[tuple] = []
    real = planner._stable_rng

    def spy(*parts):
        parts_seen.append(parts)
        return real(*parts)

    monkeypatch.setattr(planner, "_stable_rng", spy)
    planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(202), video_files=[])

    assert parts_seen, "the seeded path must still build an RNG"
    for parts in parts_seen:
        assert len(parts) == 4 and parts[0] == 202, "`(seed, index, target, start)` unchanged"


def test_seeded_selection_still_goes_through_the_fork_rule(planner, monkeypatch):
    calls = []
    real = planner.fork_variation.select_index

    def spy(scores, rng):
        calls.append(list(scores))
        return real(scores, rng)

    monkeypatch.setattr(planner.fork_variation, "select_index", spy)
    planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(101), video_files=[])

    assert len(calls) == len(_DURATIONS)
    assert all(len(scores) == len(_pool()) for scores in calls), "the full score vector is passed"


# ======================================================================================
# 7. base-score parity across every target
# ======================================================================================


_TARGETS = ("drop", "soft", "build", "rhythm", "flow")


@pytest.mark.parametrize("target", _TARGETS)
def test_static_base_score_equals_todays_score_candidate(planner, target):
    for candidate in _pool(12):
        assert planner._static_base_score(candidate, target) == planner._score_candidate(
            candidate, {"target": target, "duration": 1.0, "start": 0.0})


@pytest.mark.parametrize("target", _TARGETS)
def test_the_base_score_ignores_everything_else_in_the_profile(planner, target):
    """The contract that makes precomputation valid: only the target matters."""
    candidate = _pool(5)[2]
    lean = {"target": target}
    rich = {"target": target, "duration": 9.0, "start": 123.4, "end": 130.0,
            "wave": 0.9, "impact": 0.8, "section": {"type": "drop"}, "novelty": 0.7}

    assert planner._score_candidate(candidate, lean) == planner._score_candidate(candidate, rich)
    assert planner._score_candidate(candidate, rich) == planner._static_base_score(candidate, target)


def test_an_unknown_or_missing_target_still_resolves_to_flow(planner):
    candidate = _pool(3)[1]
    assert planner._score_candidate(candidate, {}) == planner._static_base_score(candidate, "flow")
    assert planner._static_base_score(candidate, "not_a_target") == planner._static_base_score(
        candidate, "flow"), "the else-branch is the flow formula, as before"


def test_distinct_targets_really_do_score_differently(planner):
    """Otherwise the parity tests above would be vacuous."""
    candidate = _pool(4)[0]
    scores = {t: planner._static_base_score(candidate, t) for t in _TARGETS}
    assert len(set(scores.values())) > 1, scores


# ======================================================================================
# 8. fallback behaviour
# ======================================================================================


@pytest.mark.parametrize("beat_info", [
    None, {}, {"video_analysis": {}}, {"video_analysis": {"candidates": []}},
    {"video_analysis": {"candidates": [{"id": "no_source"}]}},   # filtered: no video_file
])
def test_no_usable_candidates_still_returns_the_empty_fallback(planner, beat_info):
    assert planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=beat_info, video_files=[]) == []


def test_degenerate_timelines_still_refuse_to_plan(planner):
    assert planner.build_planned_clip_sequence(
        cut_times=[0.0], segment_durations=[1.0], beat_info=_beat_info(0), video_files=[]) == []
    assert planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=[], beat_info=_beat_info(0), video_files=[]) == []


# ======================================================================================
# 9. the shape of the work — counts, not clocks
# ======================================================================================


def test_static_scoring_runs_per_candidate_per_target_not_per_segment(planner, monkeypatch):
    """The whole point of L1A, asserted as an algorithmic property rather than a duration."""
    pool = _pool()
    calls: list[str] = []
    real = planner._static_base_score

    def spy(candidate, target):
        calls.append(target)
        return real(candidate, target)

    monkeypatch.setattr(planner, "_static_base_score", spy)
    plan = planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(0, candidates=pool), video_files=[])

    distinct_targets = len(set(calls))
    # `_materialize_clip` still scores the one winning candidate per segment through
    # `_score_candidate`; that is ~148 calls in the real run and is deliberately left alone.
    expected = len(pool) * distinct_targets + len(plan)
    assert len(calls) == expected, (
        f"{len(calls)} static evaluations for {len(pool)} candidates × {distinct_targets} "
        f"targets (+{len(plan)} materialise)")
    # and decisively fewer than the pre-L1A candidate × segment product
    assert len(calls) < len(pool) * len(_DURATIONS)


def test_the_precomputation_happens_inside_the_measured_planner_call():
    """L0's `planner_seconds` must keep describing the whole call, precomputation included."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "build_planned_clip_sequence")
    body = ast.unparse(fn)

    assert "base_scores_by_target" in body, "the table must be built inside the planner call"
    assert "_static_base_score" in body
    # nothing at module level may precompute or memoise across calls
    module_assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
                      for t in n.targets if isinstance(t, ast.Name)}
    assert not any("base_score" in name or "cache" in name for name in module_assigns), (
        "L1A is invocation-local: no module-level score cache")
    for forbidden in ("lru_cache", "functools.cache", "global "):
        assert forbidden not in body, forbidden


def test_stage6_is_the_only_thing_touched():
    """No cache identity, no Stage 5, no variation-rule changes ride along with this."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        planner_src = handle.read()
    assert "CACHE_CONTRACT_VERSION" not in planner_src
    assert "ANALYSIS_VERSION" not in planner_src

    variation_src = open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variation.py"),
                         encoding="utf-8").read()
    assert "TOP_K = 6" in variation_src
    assert "SCORE_WINDOW = 0.12" in variation_src
    assert "_WEIGHT_FLOOR = 0.25" in variation_src
    assert "base_score" not in variation_src, "the selection rule must be untouched by L1A"

    va = open(os.path.join(_REPO_ROOT, "src", "video_analysis.py"), encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in va
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in va


def test_the_penalty_constants_are_unchanged(planner):
    """Read straight out of the dynamic half, so a silent re-weighting fails here."""
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_PLANNER_PATH)
    adjusted = ast.unparse(next(n for n in ast.walk(tree)
                                if isinstance(n, ast.FunctionDef) and n.name == "_adjusted_score"))

    # ast.unparse normalises numeric literals, so `0.10` reads back as `0.1`
    for token in ("score -= 0.28", "score -= 0.1", "min(0.28, usage[cid] * 0.1)",
                  "min(0.18, usage[video_file] * 0.012)", "required_source * 0.55",
                  "score -= 0.18"):
        assert token in adjusted, token
