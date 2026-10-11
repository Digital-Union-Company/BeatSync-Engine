"""P2 — the planner's ``Sources used:`` console line carries P1's three shot diagnostics.

``video_processor`` needs the whole portable runtime (logger, cupy, cv2, gradio-adjacent imports), so
it cannot be imported on a bare interpreter. Two techniques reach the real code anyway, and both are
already established here:

* **Execute the real reporting functions.** ``_finite_diagnostic`` and ``_planner_sources_line`` are
  deliberately dependency-free — builtins and f-strings only, no ``math``, no ``numpy``, no module
  global — so they are AST-extracted from the real source and exec'd verbatim. Every formatting
  assertion below therefore runs **production code**, not a test-side restatement of it. That is the
  whole reason the helper exists as a separate function instead of being inlined into the ``print``.
* **Inspect the call site with ``ast``.** The print count, the line order and the other three planner
  lines are structure, not output, so they are asserted against the real tree — which also catches a
  fifth ``print`` arriving later.

The end-to-end direction is covered too: the real ``stage6_av_planner`` (loadable by path, numpy
only) plans a real summary, and that summary is fed to the real line builder. So the keys this reads
are pinned to the keys the planner actually writes, rather than to a fixture's idea of them.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROCESSOR = os.path.join(_REPO_ROOT, "src", "video_processor.py")
_PLANNER = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")

#: The merged main this slice branched from. The planner must stay byte-identical to it: P2 is a
#: reporting change, and the thing it reports was already merged and independently reviewed.
_PLANNER_BASELINE_SHA = "079e9b31afac5257aa5cef79a80a5aaad15b6aa5"

#: Every ``print`` in the planner-summary block, in order, as it stood before P2. The second entry
#: is the one P2 replaces; the rest must be untouched.
_PLANNER_PRINT_COUNT = 4


def _tree(path=_PROCESSOR):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


@pytest.fixture(scope="module")
def reporting():
    """The REAL ``_finite_diagnostic`` / ``_planner_sources_line``, exec'd from the real source.

    Executed into an empty namespace on purpose: if either function ever acquires a module-level
    dependency (``math``, ``np``, a constant), this fixture fails loudly rather than silently
    testing something simpler than production. That failure would be the signal to decide whether
    the dependency is wanted — not to paper over it by injecting the name here.
    """
    tree = _tree()
    namespace: dict = {}
    source = "\n\n".join(
        ast.unparse(_func(tree, name)) for name in ("_finite_diagnostic", "_planner_sources_line"))
    exec(compile(source, "<video_processor-extract>", "exec"), namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_console_p2", _PLANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(reporting, summary):
    return reporting["_planner_sources_line"](summary)


# ===========================================================================
# 1. G1 — THE VALUES AND THEIR UNITS
# ===========================================================================


def test_the_normal_line_reports_all_three_metrics(reporting):
    """The documented shape, on the real TEST1 figures P1 measured and review reproduced."""
    line = _line(reporting, {"source_count": 33, "shot_count": 49,
                             "top_shot_share": 0.09154929577464789, "shot_hhi": 0.0327319968260266})

    assert line == "   Sources used: 33 | Shots: 49 | Top shot: 9.2% | Shot HHI: 0.0327"


def test_the_share_is_rendered_as_a_percentage_not_a_fraction(reporting):
    """A bare ``0.092`` beside a count reads as nine hundredths of a shot. Units are not optional."""
    line = _line(reporting, {"source_count": 2, "shot_count": 4,
                             "top_shot_share": 0.5, "shot_hhi": 0.25})

    assert "Top shot: 50.0%" in line
    assert "Top shot: 0.5" not in line
    for share, expected in ((0.0, "0.0%"), (0.063, "6.3%"), (0.141, "14.1%"), (1.0, "100.0%")):
        assert f"Top shot: {expected}" in _line(
            reporting, {"source_count": 1, "top_shot_share": share})


def test_the_hhi_keeps_four_decimals(reporting):
    """0.0327 and 0.0252 are the real neutral/diversity-100 figures; two decimals collapses both."""
    for hhi, expected in ((0.0327319968260266, "0.0327"), (0.025198373338623, "0.0252"),
                          (0.0, "0.0000"), (1.0, "1.0000"), (1 / 3, "0.3333")):
        assert f"Shot HHI: {expected}" in _line(reporting, {"source_count": 1, "shot_hhi": hhi})


def test_the_shot_count_is_rendered_as_an_integer(reporting):
    for value, expected in ((49, "49"), (0, "0"), (1, "1"), (82.0, "82")):
        assert f"Shots: {expected}" in _line(reporting, {"source_count": 1, "shot_count": value})


def test_a_single_shot_plan(reporting):
    """One shot owning every cut: the maximum-concentration reading must be legible, not clipped."""
    line = _line(reporting, {"source_count": 1, "shot_count": 1,
                             "top_shot_share": 1.0, "shot_hhi": 1.0})

    assert line == "   Sources used: 1 | Shots: 1 | Top shot: 100.0% | Shot HHI: 1.0000"


def test_an_empty_plan_summary_renders_finite_zeros(reporting, planner):
    """The planner's empty-plan summary seeds the three keys and omits ``source_count``.

    So this is the one production shape where the pre-existing ``.get(..., 0)`` fallback actually
    fires — asserted against the real summary rather than a guess at its shape.
    """
    summary = planner.summarize_clip_plan([])
    assert summary["shot_count"] == 0 and "source_count" not in summary

    assert _line(reporting, summary) == (
        "   Sources used: 0 | Shots: 0 | Top shot: 0.0% | Shot HHI: 0.0000")


def test_the_output_is_deterministic(reporting):
    summary = {"source_count": 33, "shot_count": 49, "top_shot_share": 0.0915, "shot_hhi": 0.0327}
    first = _line(reporting, summary)

    assert all(_line(reporting, dict(summary)) == first for _ in range(5))


def test_no_file_path_or_scene_identity_is_ever_printed(reporting):
    """The line reports aggregates. A shot key is `(video_file, scene_index)` and must not leak."""
    line = _line(reporting, {"source_count": 1, "shot_count": 2, "top_shot_share": 0.5,
                             "shot_hhi": 0.5, "video_file": "C:/library/secret_source_07.mp4",
                             "scene_index": 11, "shots": [("C:/library/x.mp4", 3)]})

    for leaked in ("secret_source_07", ".mp4", "C:/", "scene", "[", "("):
        assert leaked not in line, leaked


# ===========================================================================
# 2. G3 — ABSENT AND MALFORMED DIAGNOSTICS
# ===========================================================================


def test_a_summary_with_no_shot_keys_renders_the_original_line(reporting):
    """A pre-P1 or hand-built summary degrades to exactly the pre-P2 output, byte-for-byte."""
    assert _line(reporting, {"source_count": 33}) == "   Sources used: 33"
    assert _line(reporting, {}) == "   Sources used: 0"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), float("-inf"), "", "abc",
                                 "9.2%", [], {}, (), object(), 3 + 4j])
def test_a_malformed_value_is_dropped_rather_than_fabricated(reporting, bad):
    """Omission, not ``0.0``: a `NaN` share is an absent measurement, never zero concentration.

    Printing ``Top shot: 0.0%`` for an unparseable value would state the most flattering possible
    reading of a plan nobody measured, which is the one direction a concentration diagnostic may
    never fail in.
    """
    line = _line(reporting, {"source_count": 7, "shot_count": bad,
                             "top_shot_share": bad, "shot_hhi": bad})

    assert line == "   Sources used: 7"
    assert "Shots:" not in line and "Top shot:" not in line and "Shot HHI:" not in line


@pytest.mark.parametrize("bad", [None, float("nan"), "x", [], {}])
def test_one_malformed_metric_never_hides_the_others(reporting, bad):
    line = _line(reporting, {"source_count": 7, "shot_count": 49,
                             "top_shot_share": bad, "shot_hhi": 0.0327})

    assert line == "   Sources used: 7 | Shots: 49 | Shot HHI: 0.0327"


def test_the_source_count_survives_every_malformed_diagnostic(reporting):
    """G3's explicit requirement: the original field outlives the newer ones."""
    for bad in (None, float("nan"), float("inf"), "x", [], {}, object()):
        assert _line(reporting, {"source_count": 41, "shot_count": bad,
                                 "top_shot_share": bad, "shot_hhi": bad}).startswith(
            "   Sources used: 41")


@pytest.mark.parametrize("summary", [None, [], "nope", 0, object(), ("source_count", 3)])
def test_a_non_mapping_summary_cannot_crash_a_render(reporting, summary):
    """`create_music_video` only reaches this print inside `if planned_clip_sequence:`, so a
    non-mapping cannot occur in production — but a reporting helper that can raise would turn a
    finished render into a traceback, so it is total regardless."""
    assert _line(reporting, summary) == "   Sources used: 0"


def test_out_of_range_shares_are_clamped_to_their_documented_units(reporting):
    """Bounded by construction in `_shot_metrics`, so this only fires on foreign input."""
    high = _line(reporting, {"source_count": 1, "top_shot_share": 4.2, "shot_hhi": 9.9})
    low = _line(reporting, {"source_count": 1, "shot_count": -5,
                            "top_shot_share": -0.4, "shot_hhi": -2.0})

    assert "Top shot: 100.0%" in high and "Shot HHI: 1.0000" in high
    assert "Top shot: 0.0%" in low and "Shot HHI: 0.0000" in low
    assert "Shots: 0" in low


def test_the_helpers_never_raise_on_any_shape(reporting):
    """A render is already finished by the time this prints; it must never be the thing that fails."""
    shapes = [None, {}, [], "x", 0, object(), {"source_count": object()},
              {"shot_count": float("nan"), "top_shot_share": [1], "shot_hhi": {"a": 1}},
              {"source_count": None, "shot_count": None,
               "top_shot_share": None, "shot_hhi": None}]
    for shape in shapes:
        assert isinstance(_line(reporting, shape), str)
    for value in (None, "x", [], {}, object(), float("nan"), float("inf"), 3, 3.5, True, b"3"):
        result = reporting["_finite_diagnostic"](value)
        assert result is None or isinstance(result, float)


def test_finite_diagnostic_accepts_exactly_what_float_can_parse(reporting):
    """The boundary is `float()`'s own, which is the convention `format_seconds` and `_fmt_seconds`
    already use in this repository — deliberately not a narrower `isinstance` gate.

    It differs from `_shot_key`'s boundary on purpose, and the difference is the point.
    `_shot_key` must refuse a float and a `bool` because it is deciding *shot identity*, where a
    coerced `2.0` or a `True` reading as scene 1 would invent a scene boundary. Nothing here decides
    anything: it formats a number that was already computed, so accepting any parseable spelling of
    that number is harmless and refusing one would only drop a diagnostic the summary did carry.
    Note `float()` accepts `bytes`, so `b"3"` parses — a quirk worth pinning rather than discovering.
    """
    finite = reporting["_finite_diagnostic"]

    assert finite(3) == 3.0
    assert finite(3.5) == 3.5
    assert finite("3.5") == 3.5          # a numeric string IS parseable, so it is a measurement
    assert finite(True) == 1.0           # bool is an int here; no scene boundary is being decided
    assert finite(b"3") == 3.0           # float() accepts bytes; pinned, not relied upon
    assert finite(" 0.0327 ") == 0.0327  # float() tolerates surrounding whitespace
    for bad in (None, float("nan"), float("inf"), float("-inf"), "", "abc", "9.2%",
                [], {}, (), object(), 3 + 4j):
        assert finite(bad) is None, bad


# ===========================================================================
# 3. G1 END-TO-END — THE REAL PLANNER'S REAL SUMMARY
# ===========================================================================


def _candidate(index, source, scene):
    return {
        "id": f"c{index:03d}", "video_file": f"C:/library/source_{source:02d}.mp4",
        "source_name": f"source_{source:02d}", "scene_index": scene, "kind": "scene_chunk",
        "duration": 5.2, "video_duration": 120.0, "start": 4.0 + index * 1.7,
        "center": 6.6 + index * 1.7, "peak_time": 6.0 + index * 1.7,
        "quality_score": 0.52 + (index % 9) * 0.03, "action_score": 0.30 + (index % 7) * 0.05,
        "beauty_score": 0.40 + (index % 5) * 0.06, "tension_score": 0.25 + (index % 11) * 0.04,
        "soft_score": 0.35 + (index % 6) * 0.05, "motion": 0.20 + (index % 8) * 0.07,
        "brightness": 0.30 + (index % 4) * 0.12, "contrast": 0.40 + (index % 3) * 0.10,
        "saturation": 0.35 + (index % 5) * 0.08, "sharpness": 0.30 + (index % 7) * 0.06,
        "colorfulness": 0.25 + (index % 6) * 0.09, "editorial_score": 0.45 + (index % 9) * 0.02,
        "tags": ["action"] if index % 3 == 0 else ["beauty", "clean"],
        "semantic": {"action_intensity": 0.40 + (index % 5) * 0.07,
                     "beauty_score": 0.45 + (index % 4) * 0.08,
                     "visual_quality": 0.55 + (index % 6) * 0.05,
                     "camera_motion": 0.25 + (index % 7) * 0.06,
                     "character_focus": 0.50 + (index % 3) * 0.10,
                     "combat": 0.0, "chase": 0.0, "explosion": 0.0, "emotion": "neutral",
                     "recommended_use": "filler", "description": "a moment"},
        "ai_analyzed": True,
    }


def _real_summary(planner, creative=None):
    pool, index = [], 0
    for source in range(9):
        for scene in range(2):
            for _chunk in range(3):
                pool.append(_candidate(index, source, scene))
                index += 1
    cuts = [float(t) * 1.9 for t in range(0, 25)]
    n = 61
    end = cuts[-1]
    beat_info = {
        "times": [end * i / (n - 1) for i in range(n)],
        "sections": [
            {"index": 0, "start": 0.0, "end": end * 0.3, "duration": end * 0.3, "type": "intro"},
            {"index": 1, "start": end * 0.3, "end": end * 0.6, "duration": end * 0.3,
             "type": "chorus"},
            {"index": 2, "start": end * 0.6, "end": end, "duration": end * 0.4, "type": "drop"},
        ],
        "energy_profile": {"wave": [0.25 + 0.65 * (i % 9) / 8.0 for i in range(n)],
                           "arc": [i / (n - 1) for i in range(n)]},
        "rhythm_data": {"impact_strength": [0.30 + 0.60 * (i % 7) / 6.0 for i in range(n)],
                        "combined_strength": [0.35 + 0.55 * (i % 5) / 4.0 for i in range(n)],
                        "novelty_strength": [0.70 if i % 16 == 0 else 0.25 for i in range(n)]},
        "video_analysis": {"candidates": pool},
        "creative": creative,
    }
    plan = planner.build_planned_clip_sequence(
        cut_times=cuts, segment_durations=[1.9] * 24, beat_info=beat_info, video_files=[])
    return plan, planner.summarize_clip_plan(plan)


def test_the_line_reports_what_the_real_planner_actually_computed(reporting, planner):
    """The keys read here are the keys the merged planner writes — pinned to it, not to a fixture."""
    plan, summary = _real_summary(planner)
    assert plan
    line = _line(reporting, summary)

    assert line.startswith(f"   Sources used: {summary['source_count']}")
    assert f"Shots: {summary['shot_count']}" in line
    assert f"Top shot: {summary['top_shot_share'] * 100:.1f}%" in line
    assert f"Shot HHI: {summary['shot_hhi']:.4f}" in line
    # the four segments, exactly, in the documented order
    assert line.count(" | ") == 3
    assert [segment.split(":")[0] for segment in line.strip().split(" | ")] == [
        "Sources used", "Shots", "Top shot", "Shot HHI"]


@pytest.mark.parametrize("creative", [None, {"seed": 101}, {"seed": 0, "source_diversity": 0},
                                      {"seed": 0, "source_diversity": 100},
                                      {"seed": 4242, "semantic_emphasis": 100}])
def test_every_real_summary_renders_exactly_four_segments(reporting, planner, creative):
    plan, summary = _real_summary(planner, creative)
    line = _line(reporting, summary)

    assert plan and line.count(" | ") == 3
    assert "\n" not in line, "one line, never two"
    assert line.startswith("   Sources used: ")


def test_the_merged_plan_summary_already_produces_the_three_keys(planner):
    """P2 must not recompute anything: the keys are P1's, verified present before they are read."""
    _plan, summary = _real_summary(planner)

    for key in ("shot_count", "top_shot_share", "shot_hhi", "source_count"):
        assert key in summary, key
    assert isinstance(summary["shot_count"], int)
    assert 0.0 <= summary["top_shot_share"] <= 1.0
    assert 0.0 <= summary["shot_hhi"] <= 1.0


# ===========================================================================
# 4. G2 / G4 — THE LINE BUDGET AND EVERY OTHER REPORTING LINE
# ===========================================================================


def _planner_block_prints():
    """Every ``print`` whose argument mentions ``plan_summary``, in source order."""
    fn = _func(_tree(), "create_music_video")
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            rendered = ast.unparse(node)
            if "plan_summary" in rendered:
                found.append((node.lineno, rendered))
    return [rendered for _lineno, rendered in sorted(found)]


def test_the_planner_summary_block_still_has_exactly_four_prints():
    """G2. No line was added; the metrics ride on the line that already existed."""
    prints = _planner_block_prints()

    assert len(prints) == _PLANNER_PRINT_COUNT, prints


def test_the_other_three_planner_lines_are_untouched():
    """G4. Only the ``Sources used:`` print changed; the neighbours keep their exact text."""
    prints = _planner_block_prints()

    assert prints[0] == (
        'print(f"🧠 Auto visual planner: {plan_summary[\'clip_count\']} planned clips")')
    assert prints[2] == 'print(f"   Targets: {plan_summary.get(\'targets\', {})}")'
    assert prints[3] == (
        'print(f"   AI-tagged source moments used: {plan_summary.get(\'ai_tagged\', 0)}")')


def test_the_sources_line_is_produced_by_the_helper_and_keeps_its_prefix():
    """The second print delegates, so there is exactly one formatter and it is the tested one."""
    prints = _planner_block_prints()

    assert prints[1] == "print(_planner_sources_line(plan_summary))"
    assert "Sources used" not in prints[1], "the text belongs to the helper, not the call site"
    # ...and the prefix really is preserved, in the helper
    assert "Sources used: " in ast.unparse(_func(_tree(), "_planner_sources_line"))


def test_the_fallback_for_the_pre_existing_source_count_is_unchanged():
    """``.get('source_count', 0)`` is the original expression and must not have been rewritten."""
    source = ast.unparse(_func(_tree(), "_planner_sources_line"))

    assert "summary.get('source_count', 0)" in source


def test_no_other_print_in_the_renderer_mentions_a_shot_metric():
    """The diagnostics appear on exactly one line, nowhere else in the console."""
    tree = _tree()
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "print":
            rendered = ast.unparse(node)
            if any(token in rendered for token in ("shot_count", "top_shot_share", "shot_hhi",
                                                   "Shot HHI", "Top shot", "Shots:")):
                offenders.append(rendered)

    assert offenders == [], offenders


# ===========================================================================
# 5. G5 / G6 — WHAT MUST NOT HAVE MOVED
# ===========================================================================


def test_the_reporting_helpers_are_pure(reporting):
    """No clock, no RNG, no filesystem, no environment — and no numpy, or the exec above would fail."""
    tree = _tree()
    for name in ("_finite_diagnostic", "_planner_sources_line"):
        used = set()
        for item in ast.walk(_func(tree, name)):
            if isinstance(item, ast.Name):
                used.add(item.id)
            elif isinstance(item, ast.Attribute):
                used.add(item.attr)
        for token in ("random", "time", "perf_counter", "datetime", "open", "os", "environ",
                      "np", "math", "subprocess", "uuid", "shutil"):
            assert token not in used, f"{token} used in {name}"


def test_the_helpers_never_touch_the_plan_or_the_candidates(reporting, planner):
    """Reporting only: the summary and the plan come back unmodified."""
    plan, summary = _real_summary(planner)
    plan_before = [dict(item) for item in plan]
    summary_before = dict(summary)

    _line(reporting, summary)

    assert dict(summary) == summary_before
    assert [dict(item) for item in plan] == plan_before


def test_render_info_and_the_summary_gained_no_field():
    """G5/G6. P2 publishes nothing: no `render_info` key, no summary key, no new state."""
    fn = _func(_tree(), "create_music_video")
    source = ast.unparse(fn)

    for token in ("shot_count", "top_shot_share", "shot_hhi", "scene_index"):
        assert token not in source, f"{token} must not appear in create_music_video"
    # the plan summary is still published exactly as before, and not rebuilt
    assert "render_info['plan_summary'] = plan_summary" in source
    assert "beat_info['clip_plan_summary'] = plan_summary" in source
    assert len(_calls(fn, "summarize_clip_plan")) == 1


def _calls(node, name):
    return [item for item in ast.walk(node)
            if isinstance(item, ast.Call)
            and ((isinstance(item.func, ast.Name) and item.func.id == name)
                 or (isinstance(item.func, ast.Attribute) and item.func.attr == name))]


def test_the_renderer_never_recomputes_a_shot_metric():
    """The console must not become a second authority: no shot arithmetic exists in this module."""
    tree = _tree()
    names = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)

    for token in ("_shot_key", "_shot_metrics", "Counter", "scene_index"):
        assert token not in names, f"{token} must not be reachable from video_processor"


def test_the_planner_import_set_is_unchanged():
    """G5. No new planner export was pulled in — least of all the private diagnostic helpers."""
    tree = _tree()
    imported = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.ImportFrom) and (item.module or "").endswith("stage6_av_planner"):
            imported.update(alias.asname or alias.name for alias in item.names)

    assert imported == {"build_planned_clip_sequence", "creative_profile", "summarize_clip_plan"}


def test_the_frame_lock_and_timeline_arithmetic_are_untouched():
    """G5. The one invariant a reporting change has no business near."""
    source = ast.unparse(_func(_tree(), "build_frame_aligned_cut_timeline"))

    assert "np.rint" in source
    assert "np.diff" in source or "diff" in source


def test_selection_scoring_and_rng_are_not_reachable_from_this_change():
    """G5. P2 adds two pure string helpers; nothing else in the module may have gained a branch."""
    tree = _tree()
    fn = _func(tree, "create_music_video")
    source = ast.unparse(fn)

    # the planner is still called exactly once, with the same keyword bundle
    planner_calls = _calls(fn, "build_planned_clip_sequence")
    assert len(planner_calls) == 1
    assert [kw.arg for kw in planner_calls[0].keywords] == [
        "cut_times", "segment_durations", "beat_info", "video_files"]
    # no seed, variation or scoring decision was introduced here
    for token in ("_stable_rng", "_score_candidate", "_adjusted_score", "normalize_seed"):
        assert token not in source, token


def test_no_environment_variable_was_added():
    """G6-adjacent: the reporting change is unconditional, not a new knob."""
    tree = _tree()
    envs = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Constant) and isinstance(item.value, str) \
                and item.value.startswith("BEATSYNC_"):
            envs.add(item.value)

    assert envs == {"BEATSYNC_NVENC_CLIP_WORKERS"}, sorted(envs)


def test_the_stage6_planner_is_unchanged_from_merged_main():
    """G6. P2 is a reporting slice: the thing it reports was merged and reviewed already.

    Asked via ``git diff``, **not** by comparing ``git show`` bytes to the file on disk. This
    repository is checked out with ``core.autocrlf`` active, so the working tree holds 845 CRLF
    line endings where the stored blob holds LF — a raw byte comparison fails on every platform
    where it matters and reports "the planner changed" when nothing did. ``git diff`` applies the
    same normalisation it used on the way in, so it answers the question actually being asked. The
    same trap has already cost this repository a false result once, during D0.

    Covers the working tree as well as the commit, because ``git diff <sha> -- <path>`` compares the
    baseline against what is on disk right now.

    Skips rather than fails when git or the object is unavailable, matching
    ``test_stage6_shot_diagnostics``: the suite must still run on a bare CPython checkout.
    """
    try:
        probe = subprocess.run(["git", "cat-file", "-e", f"{_PLANNER_BASELINE_SHA}^{{commit}}"],
                               cwd=_REPO_ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - environment
        pytest.skip(f"git unavailable, cannot reach the merged planner: {exc}")
    if probe.returncode != 0:  # pragma: no cover - environment
        pytest.skip(f"merged main {_PLANNER_BASELINE_SHA[:8]} not in this checkout's history")

    diff = subprocess.run(
        ["git", "diff", "--stat", _PLANNER_BASELINE_SHA, "--",
         "src/auto_mode/stage6_av_planner.py"],
        cwd=_REPO_ROOT, capture_output=True, timeout=60, text=True)

    assert diff.returncode == 0, diff.stderr
    assert diff.stdout.strip() == "", (
        f"the Stage-6 planner must not change in a reporting slice:\n{diff.stdout}")


def test_stage5_and_cache_identity_are_not_touched_by_this_module():
    """G6. A console line may not read a cache record, a contract constant or a signature."""
    tree = _tree()
    names = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)

    for token in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "PARTIAL_CONTRACT_VERSION",
                  "_CACHE_IDENTITY_WORKER_CAP", "DEFAULT_ANALYZE_BATCH_SIZE",
                  "_cache_path", "_video_signature", "_qwen_config_token", "_load_cache",
                  "_save_cache", "_cache_entry_is_complete"):
        assert token not in names, f"the renderer must not use {token}"
