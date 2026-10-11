"""P1 — shot-level diversity diagnostics, and the selection they are forbidden to touch.

A *shot* is ``(video_file, scene_index)``: Stage 5's own within-file scene boundary, which
``_build_candidate`` already persists on every candidate. It exists because a measured real
library's candidates are 98 % ``scene_chunk`` slices of shots longer than the 5.2 s window cap, so
many distinct candidate ids describe one continuous shot — and ``source_count`` cannot see that.

Two directions are pinned here, and the second is the load-bearing one:

* the three metrics are arithmetically correct, order-independent and total against malformed input;
* **adding them changed no selection, no score, no RNG draw and no clip timing.** That is checked
  against the *committed baseline planner source*, loaded out of git, rather than against a
  test-side reimplementation of the scorer — a duplicated scorer would only prove the duplicate
  agrees with itself.

The planner needs numpy but has no relative imports, so it is loaded by path — the technique
``test_creative_seed``, ``test_stage6_score_precompute`` and ``test_creative_scoring`` already use,
which keeps ``auto_mode/__init__`` (librosa, cupy, logger) out of the suite.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")

#: The merged main this slice branched from. The baseline comparison below reads the planner at this
#: exact commit, so "unchanged selection" means unchanged against published history rather than
#: against whatever happens to be checked out.
_BASELINE_SHA = "72255d86d3245a2d55b5e80d65907d7ba7173844"


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_shot_diag", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def baseline_planner(tmp_path_factory):
    """The planner exactly as committed at ``_BASELINE_SHA``, loaded as a second module.

    Skips rather than fails when git or the object is unavailable: the portable suite must run on a
    bare CPython checkout, and this one test is the only thing in the repository that needs history.
    Every other gate in this module is always-on.
    """
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    try:
        blob = subprocess.run(
            ["git", "show", f"{_BASELINE_SHA}:src/auto_mode/stage6_av_planner.py"],
            cwd=_REPO_ROOT, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment
        pytest.skip(f"git unavailable, cannot load the committed baseline: {exc}")
    if blob.returncode != 0:  # pragma: no cover - environment
        pytest.skip(f"baseline {_BASELINE_SHA[:8]} not in this checkout's history")

    path = tmp_path_factory.mktemp("baseline") / "stage6_av_planner_baseline.py"
    path.write_bytes(blob.stdout)
    spec = importlib.util.spec_from_file_location("stage6_baseline", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A pool shaped like real material: chunked shots, several sources
# ---------------------------------------------------------------------------


def _candidate(index: int, source: int, scene: int, **over) -> dict:
    """One candidate. Scores vary enough that penalties really move the winner around."""
    item = {
        "id": f"cand_{index:03d}",
        "video_file": f"C:/library/source_{source:02d}.mp4",
        "source_name": f"source_{source:02d}",
        "scene_index": scene,
        "kind": "scene_chunk",
        "duration": 5.2,
        "video_duration": 120.0,
        "start": 4.0 + index * 1.7,
        "center": 6.6 + index * 1.7,
        "peak_time": 6.0 + index * 1.7,
        "quality_score": 0.52 + (index % 9) * 0.03,
        "action_score": 0.30 + (index % 7) * 0.05,
        "beauty_score": 0.40 + (index % 5) * 0.06,
        "tension_score": 0.25 + (index % 11) * 0.04,
        "soft_score": 0.35 + (index % 6) * 0.05,
        "motion": 0.20 + (index % 8) * 0.07,
        "brightness": 0.30 + (index % 4) * 0.12,
        "contrast": 0.40 + (index % 3) * 0.10,
        "saturation": 0.35 + (index % 5) * 0.08,
        "sharpness": 0.30 + (index % 7) * 0.06,
        "colorfulness": 0.25 + (index % 6) * 0.09,
        "editorial_score": 0.45 + (index % 9) * 0.02,
        "tags": ["action"] if index % 3 == 0 else ["beauty", "clean"],
        "semantic": {
            "action_intensity": 0.40 + (index % 5) * 0.07,
            "beauty_score": 0.45 + (index % 4) * 0.08,
            "visual_quality": 0.55 + (index % 6) * 0.05,
            "camera_motion": 0.25 + (index % 7) * 0.06,
            "character_focus": 0.50 + (index % 3) * 0.10,
            "combat": 0.0,
            "chase": 0.0,
            "explosion": 0.0,
            "emotion": "neutral",
            "recommended_use": "filler",
            "description": "a moment",
        },
        "ai_analyzed": True,
    }
    item.update(over)
    return item


def _pool() -> list[dict]:
    """9 sources x 2 shots each, 3 chunks per shot = 54 candidates over 18 distinct shots."""
    pool = []
    index = 0
    for source in range(9):
        for scene in range(2):
            for _chunk in range(3):
                pool.append(_candidate(index, source, scene))
                index += 1
    return pool


_CUTS = [float(t) * 1.9 for t in range(0, 25)]
_DURATIONS = [1.9] * 24


def _beat_info(creative=None, candidates=None):
    n = 61
    end = _CUTS[-1]
    times = [end * i / (n - 1) for i in range(n)]
    wave = [0.25 + 0.65 * (i % 9) / 8.0 for i in range(n)]
    return {
        "times": times,
        "sections": [
            {"index": 0, "start": 0.0, "end": end * 0.3, "duration": end * 0.3, "type": "intro"},
            {"index": 1, "start": end * 0.3, "end": end * 0.6, "duration": end * 0.3,
             "type": "chorus"},
            {"index": 2, "start": end * 0.6, "end": end, "duration": end * 0.4, "type": "drop"},
        ],
        "energy_profile": {"wave": wave, "arc": [i / (n - 1) for i in range(n)]},
        "rhythm_data": {
            "impact_strength": [0.30 + 0.60 * (i % 7) / 6.0 for i in range(n)],
            "combined_strength": [0.35 + 0.55 * (i % 5) / 4.0 for i in range(n)],
            "novelty_strength": [0.70 if i % 16 == 0 else 0.25 for i in range(n)],
        },
        "video_analysis": {"candidates": list(_pool() if candidates is None else candidates)},
        "creative": creative,
    }


def _plan(module, creative=None, candidates=None):
    return module.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS,
        beat_info=_beat_info(creative, candidates), video_files=[])


#: Neutral plus one representative non-neutral setting for each Stage-6 control, at every required
#: seed. `source_diversity` is the dynamic control, the other three are the static/L1A ones.
_SEEDS = (0, 101, 4242, 381944)
_CONFIGS = (
    ("neutral", {}),
    ("diversity_0", {"source_diversity": 0}),
    ("diversity_100", {"source_diversity": 100}),
    ("semantic_0", {"semantic_emphasis": 0}),
    ("semantic_100", {"semantic_emphasis": 100}),
    ("energy_15", {"energy_response": 15}),
    ("energy_90", {"energy_response": 90}),
    ("motion_10", {"motion_bias": 10}),
    ("motion_95", {"motion_bias": 95}),
    ("combined", {"source_diversity": 80, "semantic_emphasis": 70,
                  "energy_response": 30, "motion_bias": 85}),
)


# ===========================================================================
# 1. SHOT IDENTITY
# ===========================================================================


def test_shot_key_is_the_source_and_the_scene_index(planner):
    assert planner._shot_key({"video_file": "a.mp4", "scene_index": 3}) == ("a.mp4", 3)
    assert planner._shot_key({"video_file": "a.mp4", "scene_index": 0}) == ("a.mp4", 0)


def test_identical_scene_indices_in_different_videos_are_different_shots(planner):
    """The whole media-local guarantee: the path is half the key, so files never merge.

    This is what keeps the diagnostic from becoming provenance inference. Two candidates with the
    same `scene_index` from different sources are unrelated, and no filename, prefix, numeric
    ordering or parent folder is consulted to decide it.
    """
    a = planner._shot_key({"video_file": "C:/x/0_1.mp4", "scene_index": 0})
    b = planner._shot_key({"video_file": "C:/x/0_2.mp4", "scene_index": 0})
    assert a != b

    plan = [{"video_file": f"C:/x/{name}", "scene_index": 0}
            for name in ("0_1.mp4", "0_2.mp4", "1.mp4", "136.mp4")]
    assert planner._shot_metrics(plan)["shot_count"] == 4


def test_multiple_scene_indices_within_one_video_are_different_shots(planner):
    plan = [{"video_file": "C:/x/a.mp4", "scene_index": s} for s in (0, 1, 2, 3)]
    metrics = planner._shot_metrics(plan)

    assert metrics["shot_count"] == 4
    assert metrics["top_shot_share"] == 0.25
    # four equal shares of 1/4 -> 4 * 0.0625
    assert metrics["shot_hhi"] == pytest.approx(0.25)


@pytest.mark.parametrize("scene", [None, True, False, 2.0, 2.5, "2", "", [], {}, (), object()])
def test_an_unusable_scene_index_falls_back_to_source_level(planner, scene):
    """`bool` first, because it subclasses `int`: `True` must not read as scene 1.

    Same explicit type boundary as `variation.normalize_seed` and `creative.normalize_control`. A
    float is refused rather than truncated for the same reason `50.5` is not a request for 50 — and
    here there is the stronger argument that a scene boundary must never be *invented*.
    """
    assert planner._shot_key({"video_file": "a.mp4", "scene_index": scene}) == ("a.mp4", None)


def test_a_missing_scene_index_falls_back_to_source_level(planner):
    assert planner._shot_key({"video_file": "a.mp4"}) == ("a.mp4", None)


def test_the_fallback_cannot_flatter_the_edit(planner):
    """The conservative direction, asserted rather than asserted-in-a-docstring.

    Three unknown-shot cuts of one source collapse to ONE shot, so `shot_count` is under-reported
    and concentration over-reported. The alternative — treating each unknown as its own shot —
    would report perfect diversity for a record carrying no scene information at all.
    """
    unknown = [{"video_file": "a.mp4", "scene_index": None} for _ in range(3)]
    known = [{"video_file": "a.mp4", "scene_index": s} for s in (0, 1, 2)]

    assert planner._shot_metrics(unknown)["shot_count"] == 1
    assert planner._shot_metrics(unknown)["top_shot_share"] == 1.0
    assert planner._shot_metrics(known)["shot_count"] == 3
    assert planner._shot_metrics(unknown)["shot_count"] <= planner._shot_metrics(known)["shot_count"]
    assert planner._shot_metrics(unknown)["shot_hhi"] >= planner._shot_metrics(known)["shot_hhi"]


def test_shot_key_never_mutates_the_item(planner):
    item = {"video_file": "a.mp4", "scene_index": "bad"}
    before = dict(item)
    planner._shot_key(item)
    assert item == before


# ===========================================================================
# 2. THE ARITHMETIC
# ===========================================================================


def test_one_shot_used_once(planner):
    metrics = planner._shot_metrics([{"video_file": "a.mp4", "scene_index": 0}])

    assert metrics == {"shot_count": 1, "top_shot_share": 1.0, "shot_hhi": 1.0}


def test_one_shot_used_repeatedly(planner):
    plan = [{"video_file": "a.mp4", "scene_index": 0} for _ in range(13)]
    metrics = planner._shot_metrics(plan)

    assert metrics["shot_count"] == 1
    assert metrics["top_shot_share"] == 1.0
    assert metrics["shot_hhi"] == 1.0


def test_exact_hhi_and_top_share_arithmetic(planner):
    """Hand-computed, against the documented convention `sum((cuts / total) ** 2)`.

    4 shots over 10 cuts: 5 + 3 + 1 + 1.
      top share = 5/10                                          = 0.5
      hhi       = 0.5**2 + 0.3**2 + 0.1**2 + 0.1**2 = 0.25 + 0.09 + 0.01 + 0.01 = 0.36
    """
    plan = (
        [{"video_file": "a.mp4", "scene_index": 0}] * 5
        + [{"video_file": "a.mp4", "scene_index": 1}] * 3
        + [{"video_file": "b.mp4", "scene_index": 0}] * 1
        + [{"video_file": "c.mp4", "scene_index": 7}] * 1
    )
    metrics = planner._shot_metrics(plan)

    assert len(plan) == 10
    assert metrics["shot_count"] == 4
    assert metrics["top_shot_share"] == pytest.approx(0.5)
    assert metrics["shot_hhi"] == pytest.approx(0.36)


def test_the_share_is_always_a_fraction_of_selected_cuts(planner):
    """Labelled unambiguously as a share, so it must stay inside [0, 1] for every shape."""
    for plan in ([],
                 [{"video_file": "a.mp4", "scene_index": 0}],
                 [{"video_file": "a.mp4", "scene_index": 0}] * 40,
                 [{"video_file": f"s{i}.mp4", "scene_index": i} for i in range(40)],
                 [{} for _ in range(5)]):
        metrics = planner._shot_metrics(plan)
        assert 0.0 <= metrics["top_shot_share"] <= 1.0, plan[:1]
        assert 0.0 <= metrics["shot_hhi"] <= 1.0, plan[:1]
        assert metrics["shot_count"] <= max(1, len(plan))


def test_hhi_bounds_match_the_count(planner):
    """`hhi` is `1/n` when `n` equal shots share the plan, and `1.0` when one shot owns it."""
    for n in (1, 2, 4, 8):
        plan = [{"video_file": f"s{i % n}.mp4", "scene_index": i % n} for i in range(8 * n)]
        metrics = planner._shot_metrics(plan)
        assert metrics["shot_count"] == n
        assert metrics["shot_hhi"] == pytest.approx(1.0 / n)
        assert metrics["top_shot_share"] == pytest.approx(1.0 / n)


def test_an_empty_plan_is_finite_zeros_and_never_divides(planner):
    metrics = planner._shot_metrics([])

    assert metrics == {"shot_count": 0, "top_shot_share": 0.0, "shot_hhi": 0.0}
    assert isinstance(metrics["top_shot_share"], float)
    assert isinstance(metrics["shot_hhi"], float)


def test_malformed_planned_clip_metadata_is_total(planner):
    """Nothing here may raise: a summary is rendered inside a UI callback."""
    for plan in ([{}],
                 [{"video_file": None, "scene_index": None}],
                 [{"scene_index": 3}],
                 [{"video_file": "a.mp4"}, {}, {"scene_index": "x"}],
                 [{"video_file": object(), "scene_index": 1}]):
        metrics = planner._shot_metrics(plan)
        assert metrics["shot_count"] >= 1
        assert 0.0 <= metrics["top_shot_share"] <= 1.0


def test_metrics_are_order_independent(planner):
    """A set size, a maximum and a sum of squares — invariant by construction, not by sorting."""
    plan = (
        [{"video_file": "a.mp4", "scene_index": 0}] * 4
        + [{"video_file": "a.mp4", "scene_index": 1}] * 2
        + [{"video_file": "b.mp4", "scene_index": 0}] * 3
        + [{"video_file": "c.mp4", "scene_index": 2}] * 1
    )
    forward = planner._shot_metrics(plan)

    assert planner._shot_metrics(list(reversed(plan))) == forward
    # a deterministic rotation and a deterministic interleave, no RNG in a test
    assert planner._shot_metrics(plan[5:] + plan[:5]) == forward
    assert planner._shot_metrics(plan[::2] + plan[1::2]) == forward


# ===========================================================================
# 3. THE SUMMARY SURFACE
# ===========================================================================


def test_the_summary_carries_all_three_metrics(planner):
    plan = _plan(planner)
    summary = planner.summarize_clip_plan(plan)

    assert summary["shot_count"] == planner._shot_metrics(plan)["shot_count"]
    assert summary["top_shot_share"] == planner._shot_metrics(plan)["top_shot_share"]
    assert summary["shot_hhi"] == planner._shot_metrics(plan)["shot_hhi"]


def test_an_empty_plan_summary_still_carries_the_three_keys(planner):
    """The reason they are seeded before the early return rather than added in the `update`."""
    summary = planner.summarize_clip_plan([])

    assert summary["clip_count"] == 0
    assert summary["shot_count"] == 0
    assert summary["top_shot_share"] == 0.0
    assert summary["shot_hhi"] == 0.0


def test_every_pre_existing_summary_key_is_untouched(planner):
    """Additive, asserted as a superset rather than by freezing the key set.

    No existing test pins `summarize_clip_plan`'s exact key set, and this one deliberately does not
    start: the property that matters is that nothing was removed or renamed.
    """
    plan = _plan(planner, {"seed": 101})
    summary = planner.summarize_clip_plan(plan, seed=101)

    for key in ("clip_count", "targets", "ai_tagged", "seed", "variation",
                "creative", "creative_text", "source_count"):
        assert key in summary, key
    assert summary["clip_count"] == len(plan)
    assert summary["source_count"] == len({item["video_file"] for item in plan})


def test_shot_count_is_never_below_the_source_count(planner):
    """A sanity relation between the new number and the one it sits beside.

    Every shot belongs to exactly one source, so distinct shots >= distinct sources — unless an
    unusable `scene_index` collapses a source's cuts, which lands on equality, never below.
    """
    for seed in _SEEDS:
        for _name, config in _CONFIGS:
            plan = _plan(planner, {"seed": seed, **config})
            summary = planner.summarize_clip_plan(plan, seed=seed)
            assert summary["shot_count"] >= summary["source_count"]


def test_the_planned_clip_carries_the_scene_index(planner):
    plan = _plan(planner)
    pool = {c["id"]: c for c in _pool()}

    assert plan
    for item in plan:
        assert "scene_index" in item
        assert item["scene_index"] == pool[item["candidate_id"]]["scene_index"]


def test_a_candidate_without_a_scene_index_still_plans(planner):
    """Stage 5 always writes one, but a legacy or hand-edited record must not break a render."""
    pool = [dict(c) for c in _pool()]
    for item in pool:
        item.pop("scene_index")

    plan = _plan(planner, candidates=pool)
    assert len(plan) == len(_DURATIONS)
    assert all(item["scene_index"] is None for item in plan)

    summary = planner.summarize_clip_plan(plan)
    # degrades exactly to the source-level view, which is the documented limitation
    assert summary["shot_count"] == summary["source_count"]


def test_the_planner_never_mutates_a_candidate(planner):
    """`scene_index` is read off the winner; the pool must come back untouched."""
    pool = _pool()
    before = [dict(c) for c in pool]
    _plan(planner, candidates=pool)

    assert pool == before


# ===========================================================================
# 4. SELECTION EQUIVALENCE AGAINST THE COMMITTED BASELINE
# ===========================================================================

#: Everything a render actually consumes from a planned clip. `scene_index` is excluded by design:
#: it is the new metadata key, and its presence must not read as a selection regression.
_DECIDED_FIELDS = ("index", "video_file", "source_name", "start_time", "source_duration",
                   "final_duration", "target", "score", "candidate_id", "tags", "ai_analyzed",
                   "audio_start", "audio_end", "wave", "impact")


def _projection(plan):
    """The canonical comparison view: every pre-existing key, in order, `scene_index` dropped."""
    return [tuple(item[field] for field in _DECIDED_FIELDS) for item in plan]


def test_the_projection_covers_every_pre_existing_key(planner, baseline_planner):
    """Guards the guard: if the baseline grew a key the projection ignores, this test fails.

    Without it, `_DECIDED_FIELDS` could silently drift into a subset and the equivalence tests below
    would compare less and less while still passing.
    """
    baseline = _plan(baseline_planner)
    assert baseline
    assert set(_DECIDED_FIELDS) == set(baseline[0].keys())

    current = _plan(planner)
    assert set(current[0].keys()) == set(_DECIDED_FIELDS) | {"scene_index"}


@pytest.mark.parametrize("seed", _SEEDS)
@pytest.mark.parametrize("name,config", _CONFIGS, ids=[c[0] for c in _CONFIGS])
def test_selection_is_identical_to_the_committed_baseline(planner, baseline_planner,
                                                          seed, name, config):
    """G1. Exact equality on every deterministic field — no tolerance.

    Compared against the planner as committed at `_BASELINE_SHA`, not against a reimplementation.
    Covers the seeded and the legacy argmax branch (seed 0), the dynamic control (Source Diversity)
    and all three static/L1A controls.
    """
    creative = {"seed": seed, **config}
    mine = _plan(planner, creative)
    theirs = _plan(baseline_planner, creative)

    assert len(mine) == len(theirs) == len(_DURATIONS)
    assert _projection(mine) == _projection(theirs)
    # stated separately so a failure names the thing that broke
    assert [i["candidate_id"] for i in mine] == [i["candidate_id"] for i in theirs]
    assert [i["video_file"] for i in mine] == [i["video_file"] for i in theirs]
    assert [i["score"] for i in mine] == [i["score"] for i in theirs]
    assert [i["start_time"] for i in mine] == [i["start_time"] for i in theirs]
    assert [i["target"] for i in mine] == [i["target"] for i in theirs]


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_scorers_are_identical_to_the_committed_baseline(planner, baseline_planner, seed):
    """G2 (scoring). Every scorer, over every candidate and every target, bit-for-bit."""
    controls_cases = [None] + [
        __import__("beatsync_fork.creative", fromlist=["creative"]).CreativeProfile.from_widgets(
            seed=seed, **config).scoring_controls()
        for _name, config in _CONFIGS
    ]
    for candidate in _pool():
        for target in ("drop", "soft", "build", "rhythm", "flow"):
            assert (planner._static_base_score(candidate, target)
                    == baseline_planner._static_base_score(candidate, target))
            for controls in controls_cases:
                assert (planner._effective_base_score(candidate, target, controls)
                        == baseline_planner._effective_base_score(candidate, target, controls))


def test_the_rng_stream_is_identical_to_the_committed_baseline(planner, baseline_planner):
    """G2 (RNG). Same seeding inputs produce the same draws, so no stream was perturbed."""
    for parts in ((0, "drop", 1.9), (101, 3, "flow", 7.6), (4242, 11, "soft", 20.9),
                  (381944, 23, "build", 43.7)):
        mine = planner._stable_rng(*parts)
        theirs = baseline_planner._stable_rng(*parts)
        assert [mine.random() for _ in range(16)] == [theirs.random() for _ in range(16)]


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_static_evaluation_count_is_unchanged(planner, baseline_planner, seed, monkeypatch):
    """G2 (L1A). The precompute shape is `candidates x distinct targets` (+ one per materialise).

    Counted on both modules, so the assertion is a comparison rather than a restatement of the
    formula — if L1A's shape were disturbed, the two counts would diverge.
    """
    def counted(module):
        calls = []
        real = module._static_base_score

        def spy(candidate, target):
            calls.append(target)
            return real(candidate, target)

        monkeypatch.setattr(module, "_static_base_score", spy)
        plan = _plan(module, {"seed": seed})
        monkeypatch.setattr(module, "_static_base_score", real)
        return calls, plan

    mine, my_plan = counted(planner)
    theirs, their_plan = counted(baseline_planner)

    assert len(mine) == len(theirs)
    assert sorted(mine) == sorted(theirs)
    assert len(mine) == len(_pool()) * len(set(mine)) + len(my_plan)
    assert len(my_plan) == len(their_plan)
    # and still decisively fewer than the pre-L1A candidate x segment product
    assert len(mine) < len(_pool()) * len(_DURATIONS)


def test_clip_timing_is_unchanged_for_every_target(planner, baseline_planner):
    """G (timing). `_materialize_clip`'s anchor/align arithmetic, candidate by candidate."""
    for candidate in _pool():
        for index, target in enumerate(("drop", "soft", "build", "rhythm", "flow")):
            profile = {"index": index, "start": 3.8, "end": 5.7, "duration": 1.9,
                       "target": target, "wave": 0.6, "impact": 0.5}
            mine = planner._materialize_clip(candidate, profile, index)
            theirs = baseline_planner._materialize_clip(candidate, profile, index)
            assert mine["start_time"] == theirs["start_time"]
            assert mine["source_duration"] == theirs["source_duration"]
            assert mine["final_duration"] == theirs["final_duration"]
            assert mine["score"] == theirs["score"]


def test_the_pre_existing_summary_values_are_unchanged(planner, baseline_planner):
    """The reporting half of equivalence: only new keys appeared, no old value moved."""
    for seed in _SEEDS:
        plan_mine = _plan(planner, {"seed": seed})
        plan_theirs = _plan(baseline_planner, {"seed": seed})
        mine = planner.summarize_clip_plan(plan_mine, seed=seed)
        theirs = baseline_planner.summarize_clip_plan(plan_theirs, seed=seed)

        assert set(mine) - set(theirs) == {"shot_count", "top_shot_share", "shot_hhi"}
        for key in theirs:
            assert mine[key] == theirs[key], key


# ===========================================================================
# 5. ISOLATION — structural, so a rename cannot evade it
# ===========================================================================


def _tree():
    with open(_PLANNER_PATH, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_PLANNER_PATH)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _strip_docstrings(node):
    """Return `node` with every docstring removed, so prose cannot satisfy or trip a check."""
    import copy

    clone = copy.deepcopy(node)
    for item in ast.walk(clone):
        body = getattr(item, "body", None)
        if isinstance(item, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and isinstance(body, list) and body \
                and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            item.body = body[1:] or [ast.Pass()]
    return clone


def _referenced(node) -> set:
    """Identifiers **and** string literals the code uses, docstrings excluded.

    A dict key or a `.get("scene_index")` is a string Constant rather than a Name, so banning the
    *use* of a field needs both halves — and excluding docstrings is what keeps the ban from
    matching the comment that explains it.
    """
    clean = _strip_docstrings(node)
    names = _identifiers(clean)
    names.update(item.value for item in ast.walk(clean)
                 if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return names


def test_no_selection_function_mentions_the_diagnostic(planner):
    """G1/G8 structurally: the metrics are unreachable from any scoring or choosing path.

    Behavioural equivalence above is the primary evidence; this is what keeps it true after a future
    edit, and it is checked against the real source rather than the imported module.
    """
    tree = _tree()
    for name in ("_static_base_score", "_effective_base_score", "_semantic_adjusted_score",
                 "_adjusted_score", "_choose_candidate", "_score_candidate", "_candidate_motion",
                 "_build_segment_profiles", "_target_for_segment"):
        used = _referenced(_func(tree, name))
        for token in ("_shot_key", "_shot_metrics", "shot_count", "top_shot_share", "shot_hhi",
                      "scene_index"):
            assert token not in used, f"{token} must not be reachable from {name}"


def test_the_diagnostic_has_exactly_one_consumer(planner):
    """`_shot_metrics` is called once, from `summarize_clip_plan`, and `_shot_key` only from it."""
    tree = _tree()

    def calls(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and ((isinstance(n.func, ast.Name) and n.func.id == name)
                     or (isinstance(n.func, ast.Attribute) and n.func.attr == name))]

    assert len(calls(tree, "_shot_metrics")) == 1
    assert len(calls(_func(tree, "summarize_clip_plan"), "_shot_metrics")) == 1
    assert len(calls(tree, "_shot_key")) == 1
    assert len(calls(_func(tree, "_shot_metrics"), "_shot_key")) == 1


def test_the_planner_loop_only_carries_the_key_it_never_reads_it(planner):
    """`build_planned_clip_sequence` must not branch on a shot: no penalty was added.

    The explicit guard against this slice quietly becoming the shot-reuse penalty it is scoped out
    of — including the deque a penalty would need.
    """
    fn = _func(_tree(), "build_planned_clip_sequence")
    used = _referenced(fn)

    for token in ("_shot_key", "_shot_metrics", "scene_index", "recent_shots", "shot_usage"):
        assert token not in used, token
    # the two pre-existing reuse deques are untouched, and no third appeared
    deques = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name) and n.func.id == "deque"]
    assert len(deques) == 2
    assert {"recent_ids", "recent_videos", "usage"} <= used


def test_no_new_penalty_or_threshold_entered_the_dynamic_scorer(planner):
    """The four documented reuse penalties, byte-for-byte, and nothing beside them."""
    source = ast.unparse(_strip_docstrings(_func(_tree(), "_adjusted_score")))

    assert "score -= 0.28" in source
    assert "score -= 0.1" in source
    assert "min(0.28, usage[cid] * 0.1)" in source
    assert "min(0.18, usage[video_file] * 0.012)" in source
    assert "score -= 0.18" in source          # the duration-suitability penalty
    assert source.count("score -=") == 9      # 4 neutral + 4 scaled + 1 shared duration


def _identifiers(node) -> set:
    """Every identifier the code actually *uses* — never a word in a docstring or a comment.

    Deliberately structural. A substring scan over `ast.unparse` output reads the prose too, so a
    docstring explaining why a name is banned would trip its own ban; that has happened three times
    in this repository's history and once while writing this module. Collected from `Name`,
    `Attribute`, `Import`/`ImportFrom` and keyword nodes only.
    """
    names = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)
        elif isinstance(item, ast.keyword) and item.arg:
            names.add(item.arg)
        elif isinstance(item, ast.Import):
            for alias in item.names:
                names.add(alias.name.split(".")[0])
                if alias.asname:
                    names.add(alias.asname)
        elif isinstance(item, ast.ImportFrom):
            if item.module:
                names.add(item.module.split(".")[0])
            for alias in item.names:
                names.add(alias.asname or alias.name)
    return names


def test_the_planner_module_imports_exactly_what_it_did_before(planner):
    """G5/G8. No import was added, so no new dependency surface exists at all."""
    tree = _tree()
    imports = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            imports.update(a.name for a in item.names)
        elif isinstance(item, ast.ImportFrom):
            imports.update(f"{item.module}.{a.name}" for a in item.names)

    assert imports == {
        "hashlib", "random", "numpy",
        "__future__.annotations",
        "collections.Counter", "collections.deque",
        "typing.Dict", "typing.List", "typing.Sequence",
        "beatsync_fork.variation", "beatsync_fork.creative",
        "beatsync_fork.deterministic_view", "beatsync_fork.freestyle",
    }, sorted(imports)


def test_the_diagnostic_touches_no_cache_or_stage5_surface(planner):
    """G5. A plan summary may not read a record, a key, a path or a contract constant."""
    used = _identifiers(_tree())

    for token in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "PARTIAL_CONTRACT_VERSION",
                  "_cache_path", "_video_signature", "_qwen_config_token", "_load_cache",
                  "_save_cache", "_checkpoint_cache", "_cache_entry_is_complete",
                  "cache_contract", "json", "glob", "listdir", "makedirs", "environ"):
        assert token not in used, f"the planner must not use {token}"


def test_the_diagnostic_is_pure(planner):
    """No clock, no randomness, no filesystem — so the same plan always summarises the same way."""
    tree = _tree()
    for name in ("_shot_key", "_shot_metrics"):
        used = _identifiers(_func(tree, name))
        for token in ("random", "Random", "time", "perf_counter", "datetime", "open",
                      "os", "environ", "_stable_rng"):
            assert token not in used, f"{token} used in {name}"

    plan = _plan(planner, {"seed": 4242})
    first = planner._shot_metrics(plan)
    for _ in range(3):
        assert planner._shot_metrics(plan) == first


def test_repeated_summaries_of_one_plan_agree(planner):
    """No unexpected cache dependency: the summary is a function of its argument."""
    plan = _plan(planner, {"seed": 101, "source_diversity": 80})
    first = planner.summarize_clip_plan(plan, seed=101)

    for _ in range(3):
        assert planner.summarize_clip_plan(plan, seed=101) == first
    # and planning twice from the same inputs reproduces the same metrics
    again = _plan(planner, {"seed": 101, "source_diversity": 80})
    assert planner.summarize_clip_plan(again, seed=101) == first


# ===========================================================================
# 6. PRACTICAL DIAGNOSTIC — readable evidence that the metric says something
# ===========================================================================


def test_source_diversity_does_not_monotonically_improve_shot_diversity(planner):
    """**The finding that justifies the metric existing**, pinned so it cannot be forgotten.

    Source Diversity scales the two *source-level* reuse penalties only, and nothing in the planner
    reads a shot — so spreading an edit across more **files** does not imply spreading it across
    more **shots**, and on this pool it measurably does not. Measured here: 15 shots at diversity 0
    and 13 at diversity 100, with `shot_hhi` moving the *wrong* way (0.0764 -> 0.0868) while
    `source_count` is 9 at both.

    That is the honest reading and it cuts both ways. P0 measured the opposite sign on the real
    41-source TEST1 pool (49 -> 55 shots, top shot 13 -> 9 cuts), so the sign is **pool-dependent**
    and neither direction may be stated as a general property. What *is* established is the
    structural fact: a source-level control is not a shot-level control, which is exactly why the
    source-level numbers could not answer the question this diagnostic was added to answer.

    This deliberately asserts no direction. It pins that the two settings can differ, and that the
    invariants hold at both.
    """
    results = {}
    lines = []
    for diversity in (0, 50, 100):
        plan = _plan(planner, {"seed": 0, "source_diversity": diversity})
        summary = planner.summarize_clip_plan(plan)
        results[diversity] = summary
        lines.append(
            f"  diversity {diversity:3d}: {summary['clip_count']} cuts, "
            f"{summary['source_count']} sources, {summary['shot_count']} shots, "
            f"top shot {summary['top_shot_share'] * 100:.1f}%, hhi {summary['shot_hhi']:.4f}")
    print("\nshot concentration vs Source Diversity (synthetic pool):\n" + "\n".join(lines))

    # the metric is not a restatement of `source_count`: it moves while that stands still
    assert results[0]["source_count"] == results[100]["source_count"]
    assert results[0]["shot_count"] != results[100]["shot_count"]
    for summary in results.values():
        assert summary["shot_count"] >= summary["source_count"]
        assert 0.0 <= summary["top_shot_share"] <= 1.0
        assert 0.0 <= summary["shot_hhi"] <= 1.0
