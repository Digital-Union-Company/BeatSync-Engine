#!/usr/bin/env python3
"""[FORK] Digital-Union: Director V2's one narrow media-aware adaptation.

**Exactly one control, in exactly one direction, from exactly one media fact.** That narrowness is
the whole design, and it is a measured result rather than a starting preference: the P1 prototype
proposed four media-aware adapters (Semantic Emphasis, Energy Response, Motion Bias, Source
Diversity), all four were pure, bounded, deterministic and cheap, and the P3 value study put every
one of them through the real Stage-6 planner on real candidate pools across eight fixed Variation
Seeds. Three of the four earned **DROP** — safe, but not worth the product complexity:

* *Semantic Emphasis* is **execution-inert** where it would matter. The control blends
  ``det + factor * (full - det)``, and ``full - det`` is non-zero only where Stage 5 actually fused
  a Qwen reading, so at 0 % semantic coverage attenuation changes no render outcome at all
  (measured: character effect exactly 0.00000). At 50 % coverage neither direction cleared the bar.
* *Energy Response* changed the **sign** of its benefit with the direction of the request --
  "responsive" lost legacy score on all three target mixes while "steadier" gained -- which is not
  a formula worth shipping.
* *Motion Bias* discarded 71-78 % of the requested character for a +0.04-0.13 % score change whose
  sign was inconsistent across seeds, i.e. noise.

Only Source Diversity survived, and only upward. On a prepared library with ~4 effective sources a
strong diversity request cannot buy a single extra source -- the plan already uses all four at
neutral -- so the extra reuse pressure is pure score cost; relaxing it recovered **+1.2353 %** mean
legacy score across 8/8 seeds with the unique-source count identical every time.

**That is a trade-off, not a free win, and the UI must say so.** ``adjacent_source_repeats`` rose
(19.6 -> 28.8 mean), so this is a P3 "meaningful trade-off", explicitly **not** strict dominance.
Relaxing diversity pressure may allow more adjacent source reuse.

**Downward requests are never attenuated**, and that is also measured: on the same concentrated
library, attenuating reuse-direction requests was consistently *harmful* (4/4 cases negative, mean
-0.4352 %), while diverse-direction requests were consistently positive (7/7, mean +0.8653 %). The
support function measures *leverage* and is direction-agnostic; the **value** is directional, so the
direction gate lives here.

Pure, and deliberately so (CLAUDE.md's hard rule for ``beatsync_fork``): stdlib only, no filesystem,
no cache, no model runtime, no clock, no randomness, no Gradio. This module decides; ``gui.py``
performs every side effect, and ``video_analysis.py`` supplies the counts while it already holds
each reusable record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from beatsync_fork.library_prep import PreparedMediaSummary

# The media FACTS record lives in ``library_prep`` and the media POLICY lives here, deliberately
# split: a prepared-library concentration fact is generic preparation truth that one day other
# features may read, while "how much of a creative request does that fact support?" is Director
# policy. The Stage-5 / preparation side therefore knows about library concentration and knows
# nothing about a Director, which is the architectural property
# `tests/test_media_neutral_semantics.py` has guarded since P2.

#: The one execution control a media adjustment may ever touch. Named once so a second authorized
#: field cannot appear by accident, and asserted against ``presets.CREATIVE_CONTROL_FIELDS`` below.
ADJUSTABLE_FIELD = "source_diversity"

#: Support never drops below this. The derivation is an ASYMMETRY rather than a measurement:
#: attenuating a control that *would* have had leverage destroys explicit user intent, while failing
#: to attenuate an inert control costs nothing (an inert control does not change the render, by
#: definition). So every support estimate errs toward 1, and no media estimate may erase more than
#: 80 % of an explicit request.
SUPPORT_FLOOR = 0.20

#: Effective source count at which Source Diversity reaches full strength and must not be
#: attenuated at all. Measured (P1 sweep A, unique-source gain at ~600 candidates held constant):
#: 3..20 sources gained EXACTLY +0 unique sources, 32 gained +2, 60 gained +8. 24 is the smallest
#: effective-source count at which any gain was measured (eff 24.7), and it is chosen
#: conservatively because the real upper edge of the zero zone is set by the SEGMENT count, which
#: the Director does not know.
EFFECTIVE_SOURCES_FULL = 24.0

#: The neutral control value. Imported rather than restated would be circular here (``creative``
#: imports nothing from this module, but this module is also loaded by the pure test tier), so it is
#: asserted equal to ``creative.DEFAULT_CONTROL`` at the bottom of the file instead.
NEUTRAL = 50

CONTROL_MIN = 0
CONTROL_MAX = 100

#: Reasons a proposal may carry. ``attenuated`` is the only one that produces a numeric change.
REASON_ATTENUATED = "attenuated"
REASON_FULL_SUPPORT = "full_support_no_change"
REASON_NOT_DIVERSE_DIRECTION = "not_diverse_direction"
REASON_SUMMARY_UNAVAILABLE = "media_summary_unavailable"


def half_up(value: float) -> int:
    """Explicit half-up rounding to an ``int``. **Never** ``round()``.

    ``round()`` is banker's rounding: ``round(0.5) == 0`` and ``round(24.5) == 24``. Used for a
    creative control that would make the smallest request a silent no-op and make the mapping
    non-monotonic at every ``.5`` point. The same reason ``smart_mix`` rounds explicitly.

    Symmetric about zero, so a future negative-delta caller cannot accidentally get floor-toward-
    minus-infinity behaviour; the only current caller passes a positive product.
    """
    if not _finite_number(value):
        raise ValueError(f"half_up expects a finite number, got {value!r}")
    x = float(value)
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _positive(value: Any) -> bool:
    return _finite_number(value) and float(value) > 0.0


def _non_negative(value: Any) -> bool:
    return _finite_number(value) and float(value) >= 0.0


# ---------------------------------------------------------------------------
# The prepared-library media summary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The one support function
# ---------------------------------------------------------------------------


def source_diversity_support(summary: Any) -> float | None:
    """Source Diversity leverage, as ``0.20..1.0``, or ``None`` when it cannot be computed.

    ``support = SUPPORT_FLOOR + (1 - SUPPORT_FLOOR) * clamp01(effective_sources / 24)``.

    Monotonic in ``effective_sources``, exactly ``1.0`` at or above 24 effective sources, and never
    below the floor. ``None`` means UNAVAILABLE and must never be replaced by 0, by the floor or by
    1 -- a fabricated support value is how an unprovable media fact would silently become a real
    creative change.
    """
    if not isinstance(summary, PreparedMediaSummary) or not summary.is_usable():
        return None
    raw = float(summary.effective_sources) / EFFECTIVE_SOURCES_FULL
    raw = max(0.0, min(1.0, raw))
    return SUPPORT_FLOOR + (1.0 - SUPPORT_FLOOR) * raw


# ---------------------------------------------------------------------------
# The adjustment record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MediaAdjustment:
    """One reviewable deterministic change, and why it happened.

    Frozen and plain: four numbers, two strings. It rides on ``DirectorProposal`` into ``gr.State``,
    which deep-copies, so no candidate list, path, cache object, summary object or runtime identity
    may live here.

    ``support`` and ``effective_sources`` are carried because the *reason* is the media leverage,
    not generic prose: a user reading "100 -> 67" is owed the fact that produced it.
    """

    field: str
    base: int
    final: int
    support: float
    effective_sources: float
    reason: str = REASON_ATTENUATED

    def __post_init__(self) -> None:
        if self.field != ADJUSTABLE_FIELD:
            raise ValueError(f"only {ADJUSTABLE_FIELD} may be media-adjusted, got {self.field!r}")

    def describe(self) -> str:
        """The one provenance line, stating BOTH the benefit and the trade-off.

        P3 disproved "free improvement": the score recovery is real and so is the cost, so the copy
        names the measured cost in the same breath. Never "better", "optimal" or "Pareto".
        """
        return (
            f"Source Diversity {self.base} -> {self.final} - this prepared library has only "
            f"{self.effective_sources:.1f} effective source(s), so stronger diversity pressure "
            f"cannot spread the edit across any more of them. Relaxing it may allow more adjacent "
            f"source reuse."
        )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


def adapt_source_diversity(base: Any, summary: Any) -> tuple[int, MediaAdjustment | None]:
    """``(final_value, adjustment_or_None)``. Total: never raises, never fabricates support.

    The whole policy, in order:

    1. a non-integer / out-of-range BASE is returned untouched (the strict recipe boundary owns it);
    2. ``BASE <= 50`` is returned untouched -- **reuse-direction requests are never attenuated**,
       because P3 measured that as consistently harmful (4/4 cases, mean -0.4352 %);
    3. an unusable summary is returned untouched, with no adjustment record;
    4. ``support == 1.0`` returns BASE untouched -- real leverage is never weakened;
    5. otherwise ``FINAL = 50 + half_up((BASE - 50) * support)``.

    The invariant ``50 <= FINAL <= BASE`` holds for every reachable input and is asserted, not
    hoped for: media may attenuate an unsupported request toward neutral, but it may never reverse
    intent, amplify it, or invent a direction from a neutral BASE.
    """
    if isinstance(base, bool) or not isinstance(base, int):
        return base, None
    if not CONTROL_MIN <= base <= CONTROL_MAX:
        return base, None
    if base <= NEUTRAL:
        return base, None

    support = source_diversity_support(summary)
    if support is None:
        return base, None
    if support >= 1.0:
        return base, None

    # One multiplication, then exactly ONE half-up rounding of the product -- not of either factor,
    # and not twice. BASE 100 with 4.0 effective sources gives support 1/3, delta 50,
    # 50 * 1/3 = 16.667 -> 17 -> FINAL 67, which is the value P3 measured on the real planner.
    delta = base - NEUTRAL
    final = NEUTRAL + half_up(delta * support)
    final = max(CONTROL_MIN, min(CONTROL_MAX, final))
    # Defensive, not ordinary behaviour: the arithmetic above cannot leave this range.
    assert NEUTRAL <= final <= base, (base, support, final)
    if final == base:
        return base, None
    return final, MediaAdjustment(
        field=ADJUSTABLE_FIELD,
        base=base,
        final=final,
        support=float(support),
        effective_sources=float(summary.effective_sources),
        reason=REASON_ATTENUATED,
    )


def adaptation_unavailable_reason(base: Any, summary: Any) -> str:
    """Why no numeric change happened, for the read-out. Reporting only, never a decision."""
    if not isinstance(summary, PreparedMediaSummary) or not summary.is_usable():
        return REASON_SUMMARY_UNAVAILABLE
    if isinstance(base, bool) or not isinstance(base, int) or base <= NEUTRAL:
        return REASON_NOT_DIVERSE_DIRECTION
    support = source_diversity_support(summary)
    if support is not None and support >= 1.0:
        return REASON_FULL_SUPPORT
    return REASON_FULL_SUPPORT


def preserves_intent(base: int, final: int) -> bool:
    """The checkable invariant: attenuate toward neutral, never past it, never away from it."""
    if base == NEUTRAL:
        return final == NEUTRAL
    if base > NEUTRAL:
        return NEUTRAL <= final <= base
    return base <= final <= NEUTRAL


# The one adjustable field must be a real creative control, and the neutral value must be the one
# the rest of the application uses. Both fail at import time rather than drifting silently.
from beatsync_fork import creative as _fork_creative  # noqa: E402
from beatsync_fork import presets as _fork_presets  # noqa: E402

assert ADJUSTABLE_FIELD in _fork_presets.CREATIVE_CONTROL_FIELDS, ADJUSTABLE_FIELD
assert NEUTRAL == _fork_creative.DEFAULT_CONTROL
assert (CONTROL_MIN, CONTROL_MAX) == (_fork_creative.CONTROL_MIN, _fork_creative.CONTROL_MAX)


__all__ = [
    "ADJUSTABLE_FIELD",
    "CONTROL_MAX",
    "CONTROL_MIN",
    "EFFECTIVE_SOURCES_FULL",
    "MediaAdjustment",
    "NEUTRAL",
    "REASON_ATTENUATED",
    "REASON_FULL_SUPPORT",
    "REASON_NOT_DIVERSE_DIRECTION",
    "REASON_SUMMARY_UNAVAILABLE",
    "SUPPORT_FLOOR",
    "adapt_source_diversity",
    "adaptation_unavailable_reason",
    "half_up",
    "preserves_intent",
    "source_diversity_support",
]
