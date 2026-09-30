#!/usr/bin/env python3
"""[FORK] Digital-Union: the deterministic (pre-Qwen) view of a persisted Stage-5 candidate.

Semantic Emphasis needs to ask "what would Stage 6 have thought of this moment on deterministic
visual evidence alone?". That question is harder than it looks, because Stage 5 does **not** persist
the answer.

``video_analysis._merge_semantic`` fuses Qwen's reading into the candidate **in place** and discards
the pre-fusion values::

    quality_score  = 0.70 * deterministic + 0.30 * semantic.visual_quality
    action_score   = 0.72 * deterministic + 0.28 * semantic.action_intensity * motion_gate
    beauty_score   = 0.65 * deterministic + 0.35 * semantic.beauty_score
    tension_score  = re-derived from the *fused* action/camera-motion plus character_focus
    soft_score     = re-derived entirely from the *fused* beauty/action/quality
    tags           = re-derived, plus recommended_use / emotion / combat / chase / explosion

So ``candidate["quality_score"]`` is emphatically **not** deterministic truth, and there is no
"original" hiding anywhere in the record.

What *does* survive is the raw computer-vision layer underneath. ``_merge_semantic`` never writes
``motion``, ``brightness``, ``contrast``, ``saturation``, ``sharpness`` or ``colorfulness`` — and
Stage 5's deterministic scores are a pure function of exactly those six numbers. This module
therefore **recomputes the pre-Qwen candidate forward** from the surviving primitives. It is not an
inversion of the fusion: nothing is divided back out, no clamp is undone, and no value is inferred
from the semantic side.

Stdlib-only (CLAUDE.md's hard rule) and side-effect free: no numpy, no OpenCV, no filesystem, and the
supplied candidate is never mutated.

--------------------------------------------------------------------------------------------------
The duplication is deliberate, and it is this module's one real hazard
--------------------------------------------------------------------------------------------------

These formulas are a second copy of ``video_analysis._build_candidate`` and of the quality
expression in ``_measure_windows``. Sharing them would mean editing production Stage-5 code to suit a
Stage-6 creative feature — risking a change to persisted floating-point values and dragging the cache
contract into a PR that has no business touching it. The copy is the lesser risk, **provided drift is
made loud**, which is what ``tests/test_deterministic_view.py`` exists for:

* a *fixed-point* test runs the real ``_build_candidate`` and requires this module to return its
  output unchanged — so the pin depends on the actual Stage-5 implementation, not on a repeated
  literal;
* a *quality-formula* test extracts the arithmetic out of ``_measure_windows`` and evaluates it.

If Stage 5's deterministic formulas ever change, those tests fail and the reconciliation is a
conscious decision rather than a silent divergence.

--------------------------------------------------------------------------------------------------
One honest boundary
--------------------------------------------------------------------------------------------------

Stage 5 computes quality from the *raw* measurements and stores ``_clamp``-ed primitives, so this
reconstruction reproduces the original exactly whenever those raw metrics were already inside
``[0, 1]``. On the real 509-candidate library that held for every candidate (measured: the documented
fusion applied to the reconstruction reproduces the persisted ``quality_score`` to 0.0). A primitive
that had been clipped at storage would make the reconstruction approximate rather than exact — it
would still be a sane deterministic view, just not a bit-perfect one.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping

#: The six persisted raw CV primitives `_merge_semantic` never overwrites. Everything below is a
#: pure function of these.
PRIMITIVE_FIELDS = ("motion", "brightness", "contrast", "saturation", "sharpness", "colorfulness")

#: Fields this view replaces with their reconstructed pre-Qwen values.
RECONSTRUCTED_FIELDS = ("quality_score", "action_score", "beauty_score", "tension_score",
                        "soft_score", "editorial_score", "tags", "semantic", "ai_analyzed")

#: `_build_candidate`'s fallback-tag thresholds. Deliberately transcribed rather than guessed —
#: they are *not* all the same number, and the test suite pins them against the real function.
_TAG_ACTION = 0.68
_TAG_BEAUTY = 0.62
_TAG_TENSION = 0.62
_TAG_SOFT = 0.64
_TAG_CLEAN = 0.68

#: `_build_candidate`'s emotion / recommended_use thresholds. Note these are strict `>` where the
#: tag thresholds above are `>=`; that asymmetry is Stage 5's and is preserved on purpose.
_USE_ACTION = 0.68
_USE_SOFT = 0.64
_USE_TENSION = 0.62


def _clamp(value: Any, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    """``video_analysis._clamp``, transcribed. Non-numeric and non-finite fall back to ``default``."""
    try:
        v = float(value)
    except Exception:
        v = default
    if not math.isfinite(v):
        v = default
    return max(lo, min(hi, v))


def deterministic_quality(brightness: float, contrast: float, saturation: float,
                          sharpness: float) -> float:
    """Stage 5's deterministic ``quality_score``, recomputed from the persisted primitives.

    This one is separate because ``_build_candidate`` does **not** compute it — it arrives already
    made in ``metrics["quality_score"]`` from ``_measure_windows``. It therefore needs its own drift
    pin, and having it as a named function is what makes that test possible.

    Arithmetic order is preserved exactly as written in Stage 5.
    """
    darkness_penalty = _clamp((0.25 - brightness) / 0.25)
    blown_penalty = _clamp((brightness - 0.86) / 0.14)
    return _clamp(
        0.36 * sharpness
        + 0.22 * _clamp(contrast)
        + 0.18 * _clamp(saturation)
        + 0.14 * (1.0 - darkness_penalty)
        + 0.10 * (1.0 - blown_penalty)
    )


def deterministic_tags(action: float, beauty: float, tension: float, soft: float,
                       quality: float) -> List[str]:
    """``video_analysis._fallback_tags``, transcribed. Order matters: Stage 6 reads it as a set, but
    the plan records the list, so the view must produce the same sequence."""
    tags: List[str] = []
    if action >= _TAG_ACTION:
        tags.append("action")
    if beauty >= _TAG_BEAUTY:
        tags.append("beauty")
    if tension >= _TAG_TENSION:
        tags.append("tension")
    if soft >= _TAG_SOFT:
        tags.append("soft")
    if quality >= _TAG_CLEAN:
        tags.append("clean")
    if not tags:
        tags.append("flow")
    return tags


def deterministic_scores(candidate: Mapping) -> Dict[str, Any]:
    """Every pre-Qwen scoring value, from the six surviving primitives.

    Returned as a plain dict so the parity tests can compare field by field without having to build
    a whole candidate. ``deterministic_candidate_view`` is the normal entry point.
    """
    brightness = _clamp(candidate.get("brightness", 0.0))
    contrast = _clamp(candidate.get("contrast", 0.0))
    saturation = _clamp(candidate.get("saturation", 0.0))
    sharpness = _clamp(candidate.get("sharpness", 0.0))
    colorfulness = _clamp(candidate.get("colorfulness", 0.0))
    motion = _clamp(candidate.get("motion", 0.0))

    quality = deterministic_quality(brightness, contrast, saturation, sharpness)
    balanced_light = 1.0 - min(abs(brightness - 0.52) / 0.52, 1.0)

    action = _clamp(0.58 * motion + 0.17 * contrast + 0.12 * saturation + 0.13 * quality)
    beauty = _clamp(0.34 * quality + 0.21 * colorfulness + 0.18 * saturation
                    + 0.17 * balanced_light + 0.10 * sharpness)
    tension = _clamp(0.42 * motion + 0.22 * contrast + 0.18 * (1.0 - balanced_light)
                     + 0.18 * saturation)
    soft = _clamp(0.55 * beauty + 0.25 * balanced_light + 0.20 * (1.0 - motion))
    editorial = _clamp(0.35 * max(action, beauty, tension) + 0.35 * quality
                       + 0.16 * saturation + 0.14 * contrast)

    semantic = {
        "action_intensity": action,
        "beauty_score": beauty,
        "emotion": ("hype" if action > _USE_ACTION
                    else "soft" if soft > _USE_SOFT
                    else "tension" if tension > _USE_TENSION
                    else "neutral"),
        "combat": 0.0,
        "chase": _clamp(motion * 0.6),
        "explosion": 0.0,
        "character_focus": _clamp(0.35 * quality + 0.20 * sharpness + 0.10 * balanced_light),
        "camera_motion": motion,
        "visual_quality": quality,
        "recommended_use": ("drop" if action > _USE_ACTION
                            else "soft" if soft > _USE_SOFT
                            else "build" if tension > _USE_TENSION
                            else "flow"),
        "description": "",
    }

    return {
        "quality_score": quality,
        "action_score": action,
        "beauty_score": beauty,
        "tension_score": tension,
        "soft_score": soft,
        "editorial_score": editorial,
        "tags": deterministic_tags(action, beauty, tension, soft, quality),
        "semantic": semantic,
        "ai_analyzed": False,
    }


def deterministic_candidate_view(candidate: Mapping) -> Dict[str, Any]:
    """A candidate-shaped view with the fused fields replaced by their pre-Qwen values.

    Identity and timing (``id``, ``video_file``, ``start``, ``center``, ``peak_time``, ``duration``
    …) are carried over untouched — Semantic Emphasis reinterprets *how good* a moment looks, never
    *which* moment it is or *when* it sits.

    The supplied mapping is never mutated: this builds a new dict, and the nested ``semantic`` value
    is a freshly constructed dict rather than a reference into the original.
    """
    view: Dict[str, Any] = dict(candidate)
    view.update(deterministic_scores(candidate))
    return view


__all__ = [
    "PRIMITIVE_FIELDS",
    "RECONSTRUCTED_FIELDS",
    "deterministic_candidate_view",
    "deterministic_quality",
    "deterministic_scores",
    "deterministic_tags",
]
