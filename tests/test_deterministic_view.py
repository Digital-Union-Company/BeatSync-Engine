"""Stage-5 parity for the reconstructed deterministic candidate view.

``beatsync_fork.deterministic_view`` is a **second copy** of Stage 5's deterministic candidate
arithmetic. That duplication is deliberate — PR2 must not edit production Stage-5 code to suit a
Stage-6 creative feature — but it is also the single biggest risk in this change: if Stage 5's
formulas ever move and this module does not, Semantic Emphasis silently starts blending against a
view that no longer corresponds to anything real, and nothing else in the suite would notice.

So this file's job is to make drift **loud**, using two independent defences that both depend on the
*actual* Stage-5 source rather than on literals copied twice:

1. **Fixed-point parity.** Run the real ``video_analysis._build_candidate`` over a matrix of
   synthetic primitives and require ``deterministic_candidate_view`` to return its output unchanged.
   A pre-Qwen candidate is by definition already deterministic, so the view must be the identity on
   it. This pins every scoring field, both tag sets and every Stage-6-relevant semantic stand-in at
   once, against code that is executed rather than transcribed.
2. **Quality-formula pin.** ``_build_candidate`` never computes quality — it arrives ready-made in
   ``metrics["quality_score"]`` from ``_measure_windows``, so defence 1 cannot see that formula at
   all. The quality expression is therefore extracted from ``_measure_windows``'s source and
   evaluated directly against :func:`deterministic_quality`.

``video_analysis`` cannot be imported here (it needs the whole runtime: cv2, numpy, logger, cupy), so
``_build_candidate`` and ``_fallback_tags`` are AST-extracted and executed with a tiny namespace —
the technique the Stage-5 suites already use. That keeps the test on a bare interpreter while still
exercising the production bodies.
"""

from __future__ import annotations

import ast
import itertools
import math
import os

import pytest

from beatsync_fork import deterministic_view as dv

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")

#: Everything `_build_candidate` needs, plus the tag helper it calls.
_EXTRACTED = ("_clamp", "_fallback_tags", "_build_candidate")


@pytest.fixture(scope="module")
def stage5():
    """The real Stage-5 candidate builder, executed without importing the runtime."""
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=_VA)

    namespace = {"os": os, "math": math, "Any": object, "Dict": dict, "List": list,
                 "Sequence": list, "__builtins__": __builtins__}
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _EXTRACTED:
            found[node.name] = ast.get_source_segment(source, node)
    missing = [name for name in _EXTRACTED if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"

    # `_build_candidate` calls `_hash_text` for the id; a stub keeps this dependency-light and the
    # id is carried through untouched by the view, so it plays no part in the parity claim.
    namespace["_hash_text"] = lambda text, length=8: "0" * length
    for name in _EXTRACTED:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    return namespace


# ---------------------------------------------------------------------------
# A matrix of primitives that reaches every branch
# ---------------------------------------------------------------------------
#
# Chosen to straddle every threshold in the deterministic path: the darkness knee (0.25), the blown
# knee (0.86), the balanced-light centre (0.52), and all five fallback-tag thresholds.

_LEVELS = (0.0, 0.12, 0.25, 0.40, 0.52, 0.66, 0.80, 0.90, 1.0)


def _metrics(brightness, contrast, saturation, sharpness, motion, colorfulness):
    """Exactly what `_measure_windows` hands `_build_candidate`, quality included."""
    return {
        "duration": 2.0,
        "brightness": brightness, "contrast": contrast, "saturation": saturation,
        "sharpness": sharpness, "motion": motion, "colorfulness": colorfulness,
        "quality_score": dv.deterministic_quality(brightness, contrast, saturation, sharpness),
        "peak_offset": 1.0,
    }


def _window():
    return {"start": 4.0, "end": 6.0, "scene_index": 3, "kind": "scene"}


def _matrix():
    """A deterministic sweep. Full product would be 9**6; this walks each axis against two
    contrasting backdrops, which still crosses every threshold in both directions."""
    backdrops = (
        dict(brightness=0.52, contrast=0.50, saturation=0.50, sharpness=0.50,
             motion=0.50, colorfulness=0.50),
        dict(brightness=0.20, contrast=0.85, saturation=0.15, sharpness=0.90,
             motion=0.95, colorfulness=0.10),
    )
    seen = set()
    for backdrop in backdrops:
        for axis in ("brightness", "contrast", "saturation", "sharpness", "motion", "colorfulness"):
            for level in _LEVELS:
                values = dict(backdrop)
                values[axis] = level
                key = tuple(sorted(values.items()))
                if key not in seen:
                    seen.add(key)
                    yield values
    # plus every corner of the extremes, so saturating clamps are covered too
    for corner in itertools.product((0.0, 1.0), repeat=6):
        yield dict(zip(("brightness", "contrast", "saturation", "sharpness",
                        "motion", "colorfulness"), corner))
    # and hand-picked cases for branches the sweep does not otherwise reach. `build` in particular
    # needs high tension with only moderate action, which no axis-sweep backdrop produces.
    yield dict(brightness=0.0, contrast=1.0, saturation=1.0, sharpness=0.0,
               motion=0.50, colorfulness=0.0)   # -> recommended_use "build", emotion "tension"
    yield dict(brightness=0.05, contrast=0.95, saturation=0.90, sharpness=0.10,
               motion=0.45, colorfulness=0.05)  # -> the same branch, just off the exact corner


# ===========================================================================
# 1. FIXED-POINT PARITY AGAINST THE REAL `_build_candidate`
# ===========================================================================


_SCORING_FIELDS = ("quality_score", "action_score", "beauty_score", "tension_score",
                   "soft_score", "editorial_score")
_SEMANTIC_FIELDS = ("action_intensity", "beauty_score", "combat", "chase", "explosion",
                    "character_focus", "camera_motion", "visual_quality", "emotion",
                    "recommended_use", "description")


def test_the_view_is_the_identity_on_a_real_pre_qwen_candidate(stage5):
    """The central parity statement, and the one that will catch almost any drift.

    A candidate straight out of Stage 5 *is* deterministic — Qwen has not touched it — so the
    reconstruction must return it unchanged, field for field. Because the left-hand side is produced
    by the real `_build_candidate`, this cannot be satisfied by copying a formula into the test.
    """
    checked = 0
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 60.0, 7, _window(), _metrics(**values))
        view = dv.deterministic_candidate_view(built)

        assert view == built, f"deterministic view diverged for {values}"
        checked += 1
    assert checked > 100, f"the matrix collapsed to {checked} cases"


@pytest.mark.parametrize("field", _SCORING_FIELDS)
def test_each_scoring_field_matches_stage_5_exactly(stage5, field):
    """Field-by-field, so a failure names the formula that moved rather than just 'the dict'."""
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(**values))
        view = dv.deterministic_candidate_view(built)
        assert view[field] == built[field], (field, values)


def test_tags_match_stage_5_exactly_including_order(stage5):
    """Stage 6 reads tags as a set, but the plan records the list, so order is part of the view."""
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(**values))
        assert dv.deterministic_candidate_view(built)["tags"] == built["tags"], values


@pytest.mark.parametrize("field", _SEMANTIC_FIELDS)
def test_each_semantic_stand_in_matches_stage_5_exactly(stage5, field):
    """The `semantic` block of a pre-Qwen candidate is a *deterministic stand-in*, not Qwen output.
    Stage 6 reads `character_focus`, `combat`, `chase`, `explosion` and `camera_motion` from it, so
    the view has to reproduce it rather than leave the fused one in place."""
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(**values))
        view = dv.deterministic_candidate_view(built)
        assert view["semantic"][field] == built["semantic"][field], (field, values)


def test_the_tag_thresholds_really_are_exercised(stage5):
    """Guards against a vacuous matrix: if no case ever produced a given tag, the tag comparison
    above would pass without testing anything about that threshold."""
    produced = set()
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(**values))
        produced.update(built["tags"])
    assert {"action", "beauty", "tension", "soft", "clean", "flow"} <= produced, produced


def test_the_recommended_use_and_emotion_branches_are_exercised(stage5):
    uses, emotions = set(), set()
    for values in _matrix():
        built = stage5["_build_candidate"](
            "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(**values))
        uses.add(built["semantic"]["recommended_use"])
        emotions.add(built["semantic"]["emotion"])
    assert {"drop", "soft", "build", "flow"} <= uses, uses
    assert {"hype", "soft", "tension", "neutral"} <= emotions, emotions


def test_ai_analyzed_is_false_on_the_view(stage5):
    """Even when the source candidate was Qwen-tagged: the view is by construction pre-Qwen."""
    built = stage5["_build_candidate"](
        "C:/lib/clip.mp4", "clip", 1.0, 0, _window(),
        _metrics(0.5, 0.5, 0.5, 0.5, 0.5, 0.5))
    fused = dict(built, ai_analyzed=True, quality_score=0.99, action_score=0.99,
                 tags=["hype"], semantic=dict(built["semantic"], combat=0.9))

    assert dv.deterministic_candidate_view(fused)["ai_analyzed"] is False


# ===========================================================================
# 2. THE QUALITY FORMULA — the one `_build_candidate` cannot pin
# ===========================================================================


def _quality_expression_source() -> str:
    """The deterministic `quality = _clamp(...)` arithmetic, as source text.

    The window-measuring functions themselves need cv2/numpy, so the *expression* is lifted rather
    than the function. It is located by the two penalty terms that appear in no other formula.

    Stage 5 carries **two** copies — the CPU metric path and the CuPy one — and they must agree, or
    a render's deterministic quality would depend on which backend measured it. That equality is
    asserted here rather than assumed, so a change to only one of them fails too.
    """
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=_VA)
    matches = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "quality"):
            rendered = ast.unparse(node.value)
            if "darkness_penalty" in rendered and "blown_penalty" in rendered:
                matches.append(rendered)
    assert matches, "no deterministic quality formula found in video_analysis.py"
    assert len(set(matches)) == 1, (
        f"the CPU and GPU metric paths disagree about deterministic quality: {set(matches)}")
    return matches[0]


def _penalty_expressions() -> dict:
    with open(_VA, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_VA)
    out: dict[str, set] = {"darkness_penalty": set(), "blown_penalty": set()}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) in out):
            out[node.targets[0].id].add(ast.unparse(node.value))
    # same story as the quality formula: both metric paths define these, and they must agree
    for name, rendered in out.items():
        assert len(rendered) == 1, f"the metric paths disagree about {name}: {rendered}"
    return {name: rendered.pop() for name, rendered in out.items()}


def test_deterministic_quality_matches_the_extracted_stage_5_formula():
    """Evaluates Stage 5's own quality arithmetic and compares it to the reconstruction.

    If the weights, the knees or the clamping in `_measure_windows` ever change, this fails and the
    reconciliation of `deterministic_view` becomes a conscious decision.
    """
    quality_src = _quality_expression_source()
    penalties = _penalty_expressions()

    for values in _matrix():
        env = {"_clamp": dv._clamp, "brightness": values["brightness"],
               "contrast": values["contrast"], "saturation": values["saturation"],
               "sharpness": values["sharpness"]}
        env["darkness_penalty"] = eval(penalties["darkness_penalty"], {}, env)  # noqa: S307
        env["blown_penalty"] = eval(penalties["blown_penalty"], {}, env)        # noqa: S307
        expected = eval(quality_src, {}, env)                                    # noqa: S307

        assert dv.deterministic_quality(
            values["brightness"], values["contrast"],
            values["saturation"], values["sharpness"]) == expected, values


def test_the_quality_formula_still_reads_the_primitives_the_view_supplies():
    """A new input to Stage 5's quality (say, colourfulness) would not fail the evaluation above —
    it would simply raise NameError there — so the variable set is pinned explicitly."""
    names = {node.id for node in ast.walk(ast.parse(_quality_expression_source()))
             if isinstance(node, ast.Name)}

    assert names == {"_clamp", "sharpness", "contrast", "saturation",
                     "darkness_penalty", "blown_penalty"}, names


def test_the_reconstruction_only_reads_primitives_stage_5_never_fuses():
    """The whole approach rests on these six fields surviving `_merge_semantic`. If a future Stage-5
    change started writing one of them, the reconstruction would silently become circular."""
    with open(_VA, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_VA)
    merge = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_merge_semantic")

    written = set()
    for node in ast.walk(merge):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Subscript)
                        and getattr(target.value, "id", None) == "candidate"
                        and isinstance(target.slice, ast.Constant)):
                    written.add(target.slice.value)

    for primitive in dv.PRIMITIVE_FIELDS:
        assert primitive not in written, (
            f"_merge_semantic now overwrites {primitive!r}; the deterministic reconstruction can no "
            f"longer recover it and deterministic_view must be revisited")
    # and it really does overwrite the fields the view replaces, or there would be nothing to undo
    assert {"quality_score", "action_score", "beauty_score", "tension_score",
            "soft_score", "tags"} <= written, written


# ===========================================================================
# 3. PURITY
# ===========================================================================


def test_the_supplied_candidate_is_never_mutated(stage5):
    """Stage 6 keeps scoring the original candidate alongside the view, and the plan records the
    original's fields; a mutation here would corrupt the render rather than just the view."""
    import copy

    built = stage5["_build_candidate"](
        "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(0.4, 0.6, 0.3, 0.7, 0.8, 0.2))
    fused = dict(built, ai_analyzed=True, quality_score=0.91, action_score=0.88,
                 beauty_score=0.77, tension_score=0.66, soft_score=0.55,
                 tags=["hype", "combat"],
                 semantic=dict(built["semantic"], combat=0.8, visual_quality=0.95))
    before = copy.deepcopy(fused)

    view = dv.deterministic_candidate_view(fused)

    assert fused == before, "the input candidate was mutated"
    assert view is not fused
    assert view["semantic"] is not fused["semantic"], "the nested semantic dict is shared"
    view["semantic"]["combat"] = 0.123
    assert fused["semantic"]["combat"] == 0.8


def test_identity_and_timing_fields_are_carried_through_untouched(stage5):
    """Semantic Emphasis reinterprets how good a moment looks, never which moment it is."""
    built = stage5["_build_candidate"](
        "C:/lib/clip.mp4", "clip", 60.0, 7, _window(), _metrics(0.5, 0.5, 0.5, 0.5, 0.5, 0.5))
    fused = dict(built, ai_analyzed=True, quality_score=0.9)
    view = dv.deterministic_candidate_view(fused)

    for field in ("id", "video_file", "source_name", "video_duration", "start", "end",
                  "duration", "center", "peak_time", "scene_index", "kind"):
        assert view[field] == fused[field], field
    for field in dv.PRIMITIVE_FIELDS:
        assert view[field] == fused[field], field


def test_the_view_is_stable_and_side_effect_free(stage5):
    built = stage5["_build_candidate"](
        "C:/lib/clip.mp4", "clip", 1.0, 0, _window(), _metrics(0.3, 0.7, 0.4, 0.6, 0.2, 0.9))

    assert dv.deterministic_candidate_view(built) == dv.deterministic_candidate_view(built)


def test_a_candidate_missing_primitives_degrades_instead_of_raising():
    """The planner must never die on a malformed or truncated record."""
    for candidate in ({}, {"id": "x"}, {"brightness": None, "motion": "nope"},
                      {"contrast": float("nan"), "sharpness": float("inf")}):
        view = dv.deterministic_candidate_view(candidate)
        for field in ("quality_score", "action_score", "beauty_score", "tension_score",
                      "soft_score"):
            assert 0.0 <= view[field] <= 1.0, (candidate, field)
        assert view["tags"], candidate
        assert view["ai_analyzed"] is False
