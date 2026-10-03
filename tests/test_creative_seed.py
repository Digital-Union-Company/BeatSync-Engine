"""Phase A: the user-controlled creative variation seed.

Three layers, because the seed crosses three trust boundaries:

* the **selection rule** (`beatsync_fork.variation`) is pure stdlib arithmetic and is tested directly;
* the **planner wiring** (`auto_mode/stage6_av_planner.py`) needs numpy, so the end-to-end plan tests
  load that one module by path and skip cleanly where numpy is absent — the module has no relative
  imports, so it loads without dragging in `auto_mode/__init__` (librosa, cupy, logger);
* the **cache boundary** is asserted with `ast` over `video_analysis.py` and `gui.py`, which cannot be
  imported here at all. That is also the stronger check: what matters is that the seed is *absent*
  from specific functions, not what a mock returned.

The load-bearing product contract is that **seed 0 is legacy**: upgrading BeatSync and leaving the
box alone must not change anyone's edit merely because this feature now exists.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import random

import pytest

from beatsync_fork import variation

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")
_ANALYSIS_PATH = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_AUTO_MODE_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_GUI_PATH = os.path.join(_REPO_ROOT, "src", "gui.py")


# ---------------------------------------------------------------------------
# The selection rule (stdlib only)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        # rejected: not a positive whole number
        (0, 0), (-1, 0), (-999, 0), (None, 0), ("", 0), ("abc", 0), ([], 0), ({}, 0),
        (float("nan"), 0), (float("inf"), 0), (float("-inf"), 0),
        (7.9, 0), (0.5, 0), (-7.0, 0), (0.0, 0), ("7.0", 0), ("-7", 0), (" ", 0),
        # rejected: bool subclasses int, so `True` would otherwise slip through as seed 1
        (True, 0), (False, 0),
        # accepted
        (7, 7), (7.0, 7), ("7", 7), ("  7  ", 7), (381944, 381944), (1, 1),
    ],
)
def test_normalize_seed_accepts_only_a_positive_whole_number(value, expected):
    """A Gradio number box yields floats, an emptied one yields None, and a user can type anything.

    None of that may raise mid-render, and none of it may be *guessed at*: truncating ``7.9`` to 7
    would render a seed the user never chose, and would make two different inputs reproduce as the
    same "reproducible" variation. Only an exact positive whole number is a variation request.
    """
    assert variation.normalize_seed(value) == expected


def test_only_a_positive_seed_is_a_variation():
    assert not variation.is_variation(0)
    assert not variation.is_variation(None)
    assert not variation.is_variation(-5)
    assert variation.is_variation(1)
    assert variation.is_variation(381944)


def test_select_index_stays_inside_the_high_scoring_eligible_set():
    """Requirement D. Variation is *among good candidates*, never arbitrary.

    The pool has 4 strong candidates inside the window and 6 clearly worse ones; no draw may ever
    return one of the worse ones, whatever the seed.
    """
    scores = [0.90, 0.86, 0.82, 0.80, 0.50, 0.40, 0.30, 0.20, 0.10, 0.00]
    eligible = {0, 1, 2, 3}

    chosen = {variation.select_index(scores, random.Random(s)) for s in range(500)}

    assert chosen <= eligible, f"picked outside the window: {sorted(chosen - eligible)}"
    assert chosen == eligible, "every eligible candidate should be reachable"


def test_select_index_never_exceeds_top_k_even_when_everything_ties():
    scores = [0.5] * 40

    chosen = {variation.select_index(scores, random.Random(s)) for s in range(500)}

    assert chosen == set(range(variation.TOP_K))


def test_select_index_is_a_pure_function_of_the_rng():
    scores = [0.9, 0.88, 0.85, 0.84, 0.83, 0.82, 0.10]
    for seed in (1, 2, 3, 101, 202):
        first = variation.select_index(scores, random.Random(seed))
        second = variation.select_index(scores, random.Random(seed))
        assert first == second


def test_a_lone_leader_is_always_chosen():
    """Nothing else is within the window, so variation cannot degrade quality here."""
    scores = [0.95, 0.40, 0.39, 0.38]
    assert {variation.select_index(scores, random.Random(s)) for s in range(200)} == {0}


def test_the_best_candidate_is_favoured_over_the_boundary_one():
    """Weighted, not uniform: variation should still lean towards the planner's own preference."""
    scores = [0.90, 0.90 - variation.SCORE_WINDOW]
    picks = [variation.select_index(scores, random.Random(s)) for s in range(1000)]
    assert picks.count(0) > picks.count(1) * 3


def test_describe_and_filename_suffix_leave_legacy_renders_untouched():
    assert variation.describe(0) == "legacy"
    assert variation.describe(None) == "legacy"
    assert variation.describe(381944) == "seed 381944"
    assert variation.filename_suffix(0) == ""
    assert variation.filename_suffix(-4) == ""
    assert variation.filename_suffix(381944) == "_seed381944"


def test_random_seed_is_always_a_usable_variation_seed():
    for _ in range(50):
        seed = variation.random_seed()
        assert variation.is_variation(seed)
        assert variation.normalize_seed(seed) == seed


# ---------------------------------------------------------------------------
# The planner, end to end (needs numpy)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def planner():
    """Load `stage6_av_planner` alone, bypassing `auto_mode/__init__` (librosa/cupy/logger)."""
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_av_planner_under_test", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_pool(count: int = 60) -> list[dict]:
    """A deterministic pool holding many similarly-good choices.

    Scores are spread narrowly on purpose: this is the shape a real library has once the planner's
    penalties have been applied, and it is the shape a seed must be able to move through.
    """
    pool = []
    for i in range(count):
        pool.append({
            "id": f"cand_{i:03d}",
            "video_file": f"C:/library/source_{i % 12:02d}.mp4",
            "source_name": f"source_{i % 12:02d}",
            "duration": 4.0,
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
            "tags": ["action"] if i % 3 == 0 else ["soft"],
            "ai_analyzed": i % 2 == 0,
        })
    return pool


def _beat_info(seed=None) -> dict:
    info = {
        "times": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
        "sections": [{"type": "verse", "start": 0.0, "end": 6.0, "energy": 0.5},
                     {"type": "drop", "start": 6.0, "end": 12.0, "energy": 0.85}],
        "energy_profile": {"wave": [0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.8, 0.85, 0.9, 0.85, 0.8, 0.7, 0.6],
                           "arc": [0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.7, 0.75, 0.8, 0.75, 0.7, 0.6, 0.5]},
        "rhythm_data": {
            "impact_strength": [0.3, 0.4, 0.35, 0.5, 0.45, 0.6, 0.8, 0.85, 0.9, 0.8, 0.7, 0.6, 0.5],
            "combined_strength": [0.3, 0.35, 0.4, 0.5, 0.5, 0.6, 0.75, 0.8, 0.85, 0.8, 0.7, 0.6, 0.5],
            "novelty_strength": [0.2, 0.3, 0.3, 0.4, 0.4, 0.5, 0.6, 0.7, 0.7, 0.6, 0.5, 0.4, 0.3],
        },
        "video_analysis": {"candidates": _candidate_pool()},
    }
    if seed is not None:
        info["creative"] = {"seed": seed}
    return info


_CUTS = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
_DURATIONS = [1.0] * (len(_CUTS) - 1)


def _plan(planner, seed=None):
    return planner.build_planned_clip_sequence(
        cut_times=_CUTS,
        segment_durations=_DURATIONS,
        beat_info=_beat_info(seed),
        video_files=[],
    )


def _digest(plan):
    return [item["candidate_id"] for item in plan]


def test_absent_creative_dict_behaves_exactly_like_seed_zero(planner):
    """Requirement E. Every existing caller — the CLI, any headless script — supplies nothing."""
    assert _digest(_plan(planner, seed=None)) == _digest(_plan(planner, seed=0))


@pytest.mark.parametrize("legacy", [0, -1, None, "", "nonsense", {"nope": 1}])
def test_every_non_variation_value_lands_on_the_legacy_plan(planner, legacy):
    baseline = _digest(_plan(planner, seed=0))
    info = _beat_info()
    info["creative"] = legacy if isinstance(legacy, dict) else {"seed": legacy}
    plan = planner.build_planned_clip_sequence(
        cut_times=_CUTS, segment_durations=_DURATIONS, beat_info=info, video_files=[])
    assert _digest(plan) == baseline


def test_legacy_seed_zero_picks_the_pre_seed_argmax_winner(planner):
    """Requirement A. Reproduces current main's selection independently of the new code path.

    The expected plan is recomputed here with the *old* algorithm — argmax over the scored
    candidates plus the tiny `_stable_rng(index, target, start)` jitter — rather than being a
    recorded constant, so this fails if the legacy arithmetic or its RNG stream drifts at all.
    """
    from collections import Counter, deque

    import numpy as np

    profiles = planner._build_segment_profiles(
        np.asarray(_CUTS, dtype=float), np.asarray(_DURATIONS, dtype=float), _beat_info())
    candidates = _candidate_pool()
    recent_ids, recent_videos, usage = deque(maxlen=10), deque(maxlen=5), Counter()
    expected = []
    for i, profile in enumerate(profiles):
        rng = planner._stable_rng(i, profile.get("target"), profile.get("start"))
        best, best_score = None, -999.0
        for candidate in candidates:
            score = planner._score_candidate(candidate, profile)
            cid, video_file = candidate.get("id"), candidate.get("video_file")
            if cid in recent_ids:
                score -= 0.28
            if video_file in recent_videos:
                score -= 0.10
            score -= min(0.28, usage[cid] * 0.10)
            score -= min(0.18, usage[video_file] * 0.012)
            required = max(0.05, profile["duration"])
            candidate_duration = max(0.05, float(candidate.get("duration", required)))
            if candidate_duration < required * 0.55:
                score -= 0.18
            score += rng.random() * 0.015
            if score > best_score:
                best_score, best = score, candidate
        expected.append(best["id"])
        recent_ids.append(best["id"])
        recent_videos.append(best["video_file"])
        usage[best["id"]] += 1
        usage[best["video_file"]] += 1

    assert _digest(_plan(planner, seed=0)) == expected


def test_the_same_positive_seed_reproduces_the_same_plan(planner):
    """Requirement B."""
    for seed in (101, 202, 381944):
        assert _digest(_plan(planner, seed=seed)) == _digest(_plan(planner, seed=seed))


def test_different_positive_seeds_produce_visibly_different_plans(planner):
    """Requirement C. The defect this phase exists to fix: a seed that changed nothing."""
    legacy = _digest(_plan(planner, seed=0))
    first = _digest(_plan(planner, seed=101))
    second = _digest(_plan(planner, seed=202))

    assert len(first) == len(second) == len(legacy)
    changed = sum(1 for a, b in zip(first, second) if a != b)
    assert changed >= len(first) * 0.3, f"only {changed}/{len(first)} segments differ"
    assert first != legacy and second != legacy


def test_variation_plans_stay_complete_and_well_formed(planner):
    """A different plan is not licence to be a worse one: same length, real sources, real clips."""
    for seed in (0, 101, 202, 999983):
        plan = _plan(planner, seed=seed)
        assert len(plan) == len(_DURATIONS), f"seed {seed} dropped segments"
        for item in plan:
            assert item["video_file"]
            assert item["final_duration"] > 0
            assert item["start_time"] >= 0.0
            assert item["target"] in {"soft", "flow", "build", "rhythm", "drop"}


def test_summarize_clip_plan_reports_the_seed_and_defaults_to_legacy(planner):
    plan = _plan(planner, seed=381944)

    assert planner.summarize_clip_plan(plan)["variation"] == "legacy"
    summary = planner.summarize_clip_plan(plan, seed=381944)
    assert summary["seed"] == 381944
    assert summary["variation"] == "seed 381944"
    assert summary["clip_count"] == len(plan)


def test_creative_seed_reads_the_bus_defensively(planner):
    assert planner.creative_seed(None) == 0
    assert planner.creative_seed({}) == 0
    assert planner.creative_seed({"creative": "not a dict"}) == 0
    assert planner.creative_seed({"creative": {}}) == 0
    assert planner.creative_seed({"creative": {"seed": "12"}}) == 12


def test_practical_diagnostic_two_seeds_change_the_plan(planner):
    """Requirement 12: readable evidence that the feature does something.

    Captured by default so the ordinary suite stays quiet. To read it::

        python -m pytest tests/test_creative_seed.py -s -k practical_diagnostic
    """
    lines = []
    for seed in (0, 101, 202):
        plan = _plan(planner, seed=seed)
        summary = planner.summarize_clip_plan(plan, seed=seed)
        sources = sorted({os.path.basename(item["video_file"]) for item in plan})
        lines.append(
            f"  {variation.describe(seed):>12}: {summary['clip_count']} clips, "
            f"{summary['source_count']} sources, targets {summary['targets']}\n"
            f"                {' '.join(item['candidate_id'].removeprefix('cand_') for item in plan)}\n"
            f"                sources {', '.join(sources)}"
        )
    print("\nCreative variation seed - same library, same music:\n" + "\n".join(lines))

    assert len(lines) == 3


# ---------------------------------------------------------------------------
# The seed must not reach Stage 5 cache identity
# ---------------------------------------------------------------------------


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


_SEED_WORDS = ("seed", "creative", "variation")


@pytest.mark.parametrize("name", [
    "_video_signature",
    "_cache_path",
    "_qwen_config_token",
    "_qwen_backend_signature_token",
    "_path_signature_token",
])
def test_no_cache_identity_function_mentions_the_seed(name):
    """Requirement F. A creative knob that re-keys 845 Qwen records is a multi-hour cold rebuild.

    Executable statements only: since P2 these docstrings legitimately name the variation seed and
    creative state in order to state that neither may enter identity, so prose must not be able to
    fail — or satisfy — this assertion.
    """
    fn = _func(_tree(_ANALYSIS_PATH), name)
    source = "\n".join(
        ast.unparse(node) for node in fn.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ).lower()
    for word in _SEED_WORDS:
        assert word not in source, f"{name} mentions {word!r}"


def test_the_cache_contract_and_analysis_versions_are_as_p2_left_them():
    """The seed still changes neither. `stage5_cache_v3` is P2's one deliberate bump — the Qwen
    prompt became media-neutral — and the creative seed had nothing to do with it."""
    tree = _tree(_ANALYSIS_PATH)
    values = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION"
                }:
                    values[target.id] = node.value.value

    assert values["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"
    assert values["ANALYSIS_VERSION"] == "auto_av_analysis_v8_llama_vulkan_batched"


def test_the_seed_never_enters_the_audio_visual_profile():
    """The profile is the bus Stage 6 reads for music-aware planning; creative state belongs on
    `beat_info["creative"]` instead, so it stays separable from anything downstream may forward.

    Under P2 no `audio_profile` field reaches Stage-5 identity at all (`smart_preset` was the last
    one, retired with the media-neutral prompt), so this is no longer the one-step-from-a-rebuild
    hazard it was. Keeping the separation is still right: the profile describes the *track*.
    """
    source = ast.unparse(_func(_tree(_AUTO_MODE_PATH), "_build_audio_visual_profile")).lower()
    for word in _SEED_WORDS:
        assert word not in source, f"_build_audio_visual_profile mentions {word!r}"


def test_the_seed_reaches_stage_6_through_beat_info_only():
    """`analyze_beats_auto` stores it on the bus and passes it to nothing else.

    Creative Controls Core generalised the bus value from Phase A's ``{"seed": n}`` to the whole
    resolved profile, so what is pinned here is that the bus entry comes from the one authority
    (``CreativeProfile.as_dict()``) rather than being assembled key by key at the call site.
    """
    fn = _func(_tree(_AUTO_MODE_PATH), "analyze_beats_auto")

    assert "creative" in [arg.arg for arg in fn.args.kwonlyargs + fn.args.args]
    source = ast.unparse(fn)
    assert "'creative': profile.as_dict()" in source
    assert "profile = fork_creative.CreativeProfile.from_mapping(creative)" in source
    # analyze_video_sources is the Stage 5 entry point; it must not learn about the seed.
    stage5 = [n for n in ast.walk(fn)
              if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "analyze_video_sources"]
    assert len(stage5) == 1
    assert "seed" not in ast.unparse(stage5[0]).lower()
    assert "creative" not in ast.unparse(stage5[0]).lower()


# ---------------------------------------------------------------------------
# The seed must not disturb the source-confirmation gate
# ---------------------------------------------------------------------------


def _gui_tree():
    return _tree(_GUI_PATH)


def _click_inputs(tree, button: str):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change"}
                and isinstance(node.func.value, ast.Name) and node.func.value.id == button):
            keywords = {kw.arg: kw.value for kw in node.keywords}
            return (
                [n.id for n in ast.walk(keywords["inputs"]) if isinstance(n, ast.Name)],
                [n.id for n in ast.walk(keywords["outputs"]) if isinstance(n, ast.Name)],
            )
    raise AssertionError(f"no click/change registration found for {button}")


def test_changing_the_seed_cannot_clear_a_source_confirmation():
    """Requirement G. Every source handler writes `source_outputs`, which disables the render button.

    The seed widget must have no handler of its own, and must appear in no source handler's inputs.
    """
    tree = _gui_tree()

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"change", "input", "submit"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "variation_seed"):
            raise AssertionError("the seed widget must not register a change handler")

    for button in ("source_mode", "source_folder", "source_recursive", "scan_btn",
                   "video_input", "confirm_btn"):
        inputs, outputs = _click_inputs(tree, button)
        assert "variation_seed" not in inputs, f"{button} reads the seed"
        assert "source_outputs" in outputs or "source_report" in outputs


def test_randomize_only_writes_the_seed_box():
    inputs, outputs = _click_inputs(_gui_tree(), "randomize_btn")

    assert inputs == []
    assert outputs == ["variation_seed"], outputs
    assert "source_outputs" not in outputs


def test_the_seed_is_a_render_request_input():
    """It must reach the handler, in a position matching the widget list (Gradio passes positionally).

    `tests/test_gui_guard_seam.py` pins the full name-for-name alignment; this pins that the seed is
    in both lists at all, which is the part this phase adds.
    """
    tree = _gui_tree()
    click = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "click" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "process_btn"
    )
    inputs = [n.id for n in next(kw.value for kw in click.keywords if kw.arg == "inputs").elts
              if isinstance(n, ast.Name)]
    parameters = [a.arg for a in _func(tree, "process_video_guarded").args.args]

    assert "variation_seed" in inputs
    assert inputs.index("variation_seed") == parameters.index("variation_seed")


def test_the_gui_normalises_the_seed_before_it_reaches_the_pipeline():
    """A raw widget value must never be trusted into `analyze_beats_auto`.

    Creative Controls Core moved the normalisation one seam earlier: the guarded handler collapses
    the four raw widget values into one already-normalised ``CreativeProfile``, so what reaches the
    pipeline is a profile rather than a scalar the implementation has to re-clean.
    **Re-pointed by C3-R0.** The normalisation moved one function inward, into the shared
    live-source-gate + render core that both the single-render wrapper and the two-candidate batch
    wrapper call. The seam is unchanged; it is now the seam for *both* render paths.
    """
    tree = _gui_tree()
    guarded = ast.unparse(_func(tree, "_process_video_guarded_unlocked"))
    assert "fork_creative.CreativeProfile.from_widgets(" in guarded
    assert "seed=variation_seed" in guarded

    # the public wrapper still owns the Gradio contract and delegates rather than re-normalising
    wrapper = ast.unparse(_func(tree, "process_video_guarded"))
    assert "_process_video_guarded_unlocked(" in wrapper
    assert "fork_creative.CreativeProfile.from_widgets(" not in wrapper

    source = ast.unparse(_func(tree, "_process_video_impl"))
    assert "creative=creative.as_dict()" in source
    assert "creative.filename_suffix()" in source


# ---------------------------------------------------------------------------
# The legacy branch must stay recognisably itself
# ---------------------------------------------------------------------------


def test_the_legacy_rng_stream_has_no_seed_component():
    """Seed 0 must hash exactly `(index, target, start)`, as current main does.

    Prepending a `0` would change the hash input, change the jitter, and silently change every
    default render — the precise regression this contract exists to prevent.
    """
    fn = _func(_tree(_PLANNER_PATH), "_choose_candidate")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_stable_rng"]
    rendered = {ast.unparse(call) for call in calls}

    assert "_stable_rng(index, profile.get('target'), profile.get('start'))" in rendered
    assert "_stable_rng(seed, index, profile.get('target'), profile.get('start'))" in rendered
    assert len(rendered) == 2


def test_the_legacy_branch_still_keeps_its_tiny_jitter_and_argmax():
    source = ast.unparse(_func(_tree(_PLANNER_PATH), "_choose_candidate"))

    assert "rng.random() * 0.015" in source
    assert "best_score" in source and "best_candidate" in source
