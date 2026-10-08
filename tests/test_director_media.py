"""Director V2's one narrow media-aware adaptation: the support function and the adapter.

Every constant and every expected shape here is pinned against a **measured** P3 result rather than
against the formula's own arithmetic, because the point of the study was that a safe formula is not
automatically a valuable one. The three adapters P3 rejected are asserted *absent*, which is the
only way "we deliberately did not ship those" stays true.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import math
import os
import re
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import director as fork_director
from beatsync_fork import director_media as fork_media
from beatsync_fork import library_prep as fork_prep
from beatsync_fork import presets as fork_presets

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MEDIA = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "director_media.py")

#: The real concentrated library P3 measured: 309 candidate moments over 4 effective sources.
CONCENTRATED = fork_prep.PreparedMediaSummary(
    candidate_moments=309, effective_sources=4.0,
    top_source_share=0.4126, median_moments_per_source=77.0)

#: A library at the full-support edge.
AT_FULL = fork_prep.PreparedMediaSummary(
    candidate_moments=1000, effective_sources=24.0,
    top_source_share=0.05, median_moments_per_source=40.0)

#: The real full library P3 measured: 681 effective sources, far past the full-support edge.
DIVERSE = fork_prep.PreparedMediaSummary(
    candidate_moments=12392, effective_sources=681.3,
    top_source_share=0.01, median_moments_per_source=9.0)


# ---------------------------------------------------------------------------
# The calibration constants
# ---------------------------------------------------------------------------

def test_the_two_calibration_constants_are_the_measured_ones():
    assert fork_media.SUPPORT_FLOOR == 0.20
    assert fork_media.EFFECTIVE_SOURCES_FULL == 24.0


def test_the_neutral_value_and_range_come_from_the_one_creative_registry():
    assert fork_media.NEUTRAL == fork_creative.DEFAULT_CONTROL == 50
    assert fork_media.CONTROL_MIN == fork_creative.CONTROL_MIN
    assert fork_media.CONTROL_MAX == fork_creative.CONTROL_MAX


def test_only_source_diversity_may_ever_be_adjusted():
    assert fork_media.ADJUSTABLE_FIELD == "source_diversity"
    assert fork_media.ADJUSTABLE_FIELD in fork_presets.CREATIVE_CONTROL_FIELDS


# ---------------------------------------------------------------------------
# half_up
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0.0, 0), (0.5, 1), (1.4, 1), (1.5, 2), (2.5, 3), (16.6667, 17), (24.5, 25), (49.5, 50),
])
def test_half_up_rounds_half_away_from_zero(value: float, expected: int):
    assert fork_media.half_up(value) == expected


def test_half_up_is_not_bankers_rounding():
    """The whole reason the helper exists: ``round`` would send both 0.5 and 2.5 the wrong way."""
    assert round(0.5) == 0 and fork_media.half_up(0.5) == 1
    assert round(2.5) == 2 and fork_media.half_up(2.5) == 3


def test_half_up_is_symmetric_about_zero():
    assert fork_media.half_up(-0.5) == -1
    assert fork_media.half_up(-2.5) == -3


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None, "3", True])
def test_half_up_refuses_a_non_finite_or_non_numeric_value(bad: Any):
    with pytest.raises(ValueError):
        fork_media.half_up(bad)


# ---------------------------------------------------------------------------
# The support function
# ---------------------------------------------------------------------------

def test_support_is_inside_the_floor_and_one_for_every_effective_source_count():
    for tenths in range(1, 1200):
        summary = fork_prep.PreparedMediaSummary(
            candidate_moments=100, effective_sources=tenths / 10.0)
        support = fork_media.source_diversity_support(summary)
        assert support is not None
        assert fork_media.SUPPORT_FLOOR <= support <= 1.0, (tenths, support)


def test_support_is_monotonic_in_effective_sources():
    previous = -1.0
    for tenths in range(1, 400):
        summary = fork_prep.PreparedMediaSummary(
            candidate_moments=100, effective_sources=tenths / 10.0)
        support = fork_media.source_diversity_support(summary)
        assert support >= previous, tenths
        previous = support


def test_support_is_exactly_one_at_and_above_the_full_effective_source_count():
    assert fork_media.source_diversity_support(AT_FULL) == 1.0
    assert fork_media.source_diversity_support(DIVERSE) == 1.0
    for effective in (24.0, 24.1, 30.0, 100.0, 681.3, 10_000.0):
        summary = fork_prep.PreparedMediaSummary(
            candidate_moments=100, effective_sources=effective)
        assert fork_media.source_diversity_support(summary) == 1.0, effective


def test_support_on_the_measured_concentrated_library_is_one_third():
    assert fork_media.source_diversity_support(CONCENTRATED) == pytest.approx(1 / 3, abs=1e-12)


def test_support_is_deterministic():
    first = fork_media.source_diversity_support(CONCENTRATED)
    for _ in range(50):
        assert fork_media.source_diversity_support(CONCENTRATED) == first


@pytest.mark.parametrize("summary", [
    None, object(), {}, "concentrated", 4.0,
    fork_prep.PreparedMediaSummary(),
    fork_prep.PreparedMediaSummary(candidate_moments=0, effective_sources=4.0),
    fork_prep.PreparedMediaSummary(candidate_moments=-5, effective_sources=4.0),
    fork_prep.PreparedMediaSummary(candidate_moments=100, effective_sources=0.0),
    fork_prep.PreparedMediaSummary(candidate_moments=100, effective_sources=-4.0),
    fork_prep.PreparedMediaSummary(candidate_moments=100, effective_sources=float("nan")),
    fork_prep.PreparedMediaSummary(candidate_moments=100, effective_sources=float("inf")),
    fork_prep.PreparedMediaSummary(candidate_moments=True, effective_sources=4.0),
])
def test_an_unprovable_summary_yields_unavailable_rather_than_a_fabricated_support(summary: Any):
    """``None`` is the one honest answer, and it must never be 0, the floor or 1."""
    assert fork_media.source_diversity_support(summary) is None


# ---------------------------------------------------------------------------
# The adapter: the direction gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", [0, 1, 15, 30, 49, 50])
def test_a_reuse_direction_request_is_never_weakened(base: int):
    """P3 measured attenuating a reuse request as consistently harmful (4/4 cases, -0.4352 % mean).

    So the gate is a product decision backed by measurement, not an arithmetic convenience: the
    support function is direction-agnostic, and the asymmetry lives here.
    """
    final, adjustment = fork_media.adapt_source_diversity(base, CONCENTRATED)
    assert final == base
    assert adjustment is None


@pytest.mark.parametrize("base,expected", [
    (100, 67), (95, 65), (90, 63), (85, 62), (80, 60),
])
def test_the_measured_p3_attenuation_shapes_are_reproduced(base: int, expected: int):
    """These exact pairs were produced by the real Stage-6 planner runs in P3."""
    final, adjustment = fork_media.adapt_source_diversity(base, CONCENTRATED)
    assert final == expected
    assert adjustment is not None
    assert adjustment.base == base and adjustment.final == expected


def test_full_support_never_changes_any_base():
    for summary in (AT_FULL, DIVERSE):
        for base in range(0, 101):
            final, adjustment = fork_media.adapt_source_diversity(base, summary)
            assert final == base, (summary.effective_sources, base)
            assert adjustment is None


def test_an_unavailable_summary_leaves_every_base_untouched():
    for summary in (None, fork_prep.PreparedMediaSummary(), "x", 4):
        for base in (51, 75, 100):
            final, adjustment = fork_media.adapt_source_diversity(base, summary)
            assert final == base
            assert adjustment is None


@pytest.mark.parametrize("bad", [True, False, 50.5, "75", None, -1, 101])
def test_a_malformed_base_is_returned_untouched_rather_than_coerced(bad: Any):
    final, adjustment = fork_media.adapt_source_diversity(bad, CONCENTRATED)
    assert final is bad or final == bad
    assert adjustment is None


# ---------------------------------------------------------------------------
# The adapter: the invariants, exhaustively
# ---------------------------------------------------------------------------

def _summaries():
    for tenths in range(1, 300, 7):
        yield fork_prep.PreparedMediaSummary(
            candidate_moments=500, effective_sources=tenths / 10.0)


def test_the_intent_preservation_invariant_holds_for_every_base_and_summary():
    """Media may attenuate toward neutral. It may never reverse, amplify or invent intent."""
    checked = 0
    for summary in _summaries():
        for base in range(0, 101):
            final, _adjustment = fork_media.adapt_source_diversity(base, summary)
            checked += 1
            assert fork_media.preserves_intent(base, final), (base, summary.effective_sources,
                                                              final)
            assert fork_media.CONTROL_MIN <= final <= fork_media.CONTROL_MAX
            if base > fork_media.NEUTRAL:
                assert fork_media.NEUTRAL <= final <= base
            else:
                assert final == base
    assert checked > 4000, checked


def test_a_neutral_base_is_never_moved():
    for summary in _summaries():
        final, adjustment = fork_media.adapt_source_diversity(50, summary)
        assert final == 50 and adjustment is None


def test_attenuation_is_monotonic_in_support():
    """More effective sources must never attenuate a given request harder."""
    previous = -1
    for tenths in range(1, 300):
        summary = fork_prep.PreparedMediaSummary(
            candidate_moments=500, effective_sources=tenths / 10.0)
        final, _ = fork_media.adapt_source_diversity(100, summary)
        assert final >= previous, tenths
        previous = final


def test_the_adapter_is_deterministic():
    first = [fork_media.adapt_source_diversity(b, CONCENTRATED)[0] for b in range(101)]
    for _ in range(20):
        assert [fork_media.adapt_source_diversity(b, CONCENTRATED)[0]
                for b in range(101)] == first


# ---------------------------------------------------------------------------
# The adjustment record
# ---------------------------------------------------------------------------

def test_the_adjustment_record_is_frozen_plain_and_deepcopy_safe():
    _final, adjustment = fork_media.adapt_source_diversity(100, CONCENTRATED)
    assert dataclasses.is_dataclass(adjustment)
    with pytest.raises(dataclasses.FrozenInstanceError):
        adjustment.final = 99
    assert copy.deepcopy(adjustment) == adjustment
    for field in dataclasses.fields(adjustment):
        value = getattr(adjustment, field.name)
        assert isinstance(value, (int, float, str)), (field.name, type(value))


def test_the_adjustment_record_carries_the_media_fact_that_caused_it():
    _final, adjustment = fork_media.adapt_source_diversity(100, CONCENTRATED)
    assert adjustment.support == pytest.approx(1 / 3)
    assert adjustment.effective_sources == 4.0
    assert adjustment.reason == fork_media.REASON_ATTENUATED


def test_no_other_field_can_be_named_as_adjusted():
    for forbidden in ("motion_bias", "energy_response", "semantic_emphasis", "cut_density",
                      "micro_cuts", "seed", ""):
        with pytest.raises(ValueError):
            fork_media.MediaAdjustment(field=forbidden, base=100, final=67, support=0.33,
                                       effective_sources=4.0)


def test_the_provenance_line_states_both_the_benefit_and_the_tradeoff():
    """P3 disproved "free improvement", so the copy may not imply one."""
    _final, adjustment = fork_media.adapt_source_diversity(100, CONCENTRATED)
    text = adjustment.describe()
    assert "100" in text and "67" in text
    assert "4.0 effective source" in text
    # the benefit: more pressure cannot buy more variety
    assert "cannot spread" in text
    # the cost, named in the same breath
    assert "adjacent source reuse" in text
    for overclaim in ("better", "optimal", "pareto", "free", "improve", "best"):
        assert overclaim not in text.lower(), overclaim


# ---------------------------------------------------------------------------
# The three rejected adapters must be absent
# ---------------------------------------------------------------------------

def _executable_source(path: str) -> str:
    """Source with docstrings stripped, so prose about a rejected adapter is not a false positive."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.mark.parametrize("dropped", [
    "motion_spread", "action_spread", "soft_spread", "tension_spread",
    "qwen_semantic_coverage", "qwen_semantic_moments", "semantic_moments",
    "motion_p10", "motion_p50", "motion_p90",
    "support_motion_bias", "support_energy_response", "support_semantic_emphasis",
    "reusable_record_coverage",
])
def test_no_field_or_function_of_a_rejected_adapter_survives(dropped: str):
    """P3 dropped three of P1's four adapters. Their support inputs must not ship as dead weight."""
    source = _executable_source(_MEDIA).lower()
    assert not re.search(rf"\b{re.escape(dropped)}\b", source), f"director_media mentions {dropped}"
    prep_source = _executable_source(
        os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_prep.py")).lower()
    assert not re.search(rf"\b{re.escape(dropped)}\b", prep_source), (
        f"library_prep mentions {dropped}")


def test_the_summary_carries_only_the_four_fields_the_retained_feature_needs():
    assert fork_prep.MEDIA_SUMMARY_FIELDS == (
        "candidate_moments", "effective_sources", "top_source_share",
        "median_moments_per_source")
    assert tuple(f.name for f in dataclasses.fields(fork_prep.PreparedMediaSummary)) == \
        fork_prep.MEDIA_SUMMARY_FIELDS


def test_only_one_support_function_exists():
    source = _executable_source(_MEDIA)
    supports = [node.name for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and "support" in node.name]
    assert supports == ["source_diversity_support"], supports


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------

def test_the_media_module_is_pure():
    """No filesystem, no cache, no model runtime, no clock, no randomness, no Gradio."""
    source = _executable_source(_MEDIA).lower()
    for forbidden in ("open(", "os.path", "subprocess", "time.", "random", "gradio", "numpy",
                      "_cache_path", "_load_cache", "json.load", "datetime"):
        assert forbidden not in source, f"director_media uses {forbidden!r}"


def test_the_media_module_imports_only_stdlib_and_pure_fork_modules():
    with open(_MEDIA, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MEDIA)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "math", "dataclasses", "typing", "beatsync_fork"}, imported


def test_the_media_module_has_no_module_level_mutable_state():
    with open(_MEDIA, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_MEDIA)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if targets == {"__all__"}:
            continue  # the export list is a declaration, not state
        assert not isinstance(node.value, (ast.List, ast.Dict, ast.Set)), ast.unparse(node)


# ---------------------------------------------------------------------------
# The Director's own view of the adapter
# ---------------------------------------------------------------------------

def test_the_director_adapts_only_source_diversity_and_copies_everything_else():
    base = {"cut_density": 90, "micro_cuts": 80, "semantic_emphasis": 20,
            "energy_response": 10, "motion_bias": 95, "source_diversity": 100}
    final, adjustment = fork_director.apply_media_adaptation(base, CONCENTRATED)
    assert adjustment is not None
    assert final["source_diversity"] == 67
    for untouched in ("cut_density", "micro_cuts", "semantic_emphasis", "energy_response",
                      "motion_bias"):
        assert final[untouched] == base[untouched], untouched


def test_the_director_never_mutates_the_base_mapping():
    base = {"cut_density": 50, "micro_cuts": 50, "semantic_emphasis": 50,
            "energy_response": 50, "motion_bias": 50, "source_diversity": 100}
    snapshot = dict(base)
    fork_director.apply_media_adaptation(base, CONCENTRATED)
    assert base == snapshot


@pytest.mark.parametrize("summary", [None, fork_prep.PreparedMediaSummary(), AT_FULL, DIVERSE])
def test_no_adjustment_record_is_fabricated_when_nothing_changed(summary: Any):
    base = {name: 50 for name in fork_presets.CREATIVE_CONTROL_FIELDS}
    base["source_diversity"] = 100
    final, adjustment = fork_director.apply_media_adaptation(base, summary)
    assert adjustment is None
    assert final == base
