#!/usr/bin/env python3
"""[FORK] Digital-Union: the creative variation seed (Phase A).

The Stage 6 planner already had ``_stable_rng``, but its only consumer was
``score += rng.random() * 0.015`` inside an argmax loop. Candidate scores span roughly ``-1.0 … 2.0``
and the planner's own repeat penalties are ``0.28 / 0.18 / 0.10``, so a 0.015 jitter changes the
winner only among near-exact ties. Handing a user a seed wired to *that* would have shipped a control
that visibly does nothing. This module is the seeded selection rule that makes the seed real.

Two behaviours, chosen by the seed itself:

* ``seed == 0`` — **legacy**. The planner keeps its existing argmax and its existing tiny jitter,
  byte-for-byte. Upgrading BeatSync and leaving the box alone must not change anyone's edit merely
  because this feature now exists, so seed 0 never reaches :func:`select_index`.
* ``seed > 0`` — **variation**. Scores and penalties are unchanged; only the *winner rule* changes,
  from "highest score" to "seeded weighted pick among the best few".

Kept here rather than in ``auto_mode`` because the interesting part is pure arithmetic over a list of
floats: this way it is testable on a bare interpreter with no numpy, no CUDA and no FFmpeg, which is
the fork package's whole purpose (CLAUDE.md's hard rule).
"""

from __future__ import annotations

import random
from typing import Sequence

#: The seed value that means "behave exactly like current main".
LEGACY_SEED = 0

#: At most this many of the best-scoring candidates are ever considered.
TOP_K = 6

#: A candidate more than this far below the best score is excluded, however few candidates remain.
#: This is what keeps variation *among good choices* rather than arbitrary. It is deliberately
#: smaller than the planner's own "seen recently" penalty (0.28), so variation can never undo a
#: repeat penalty the planner applied on purpose.
SCORE_WINDOW = 0.12

#: Weight given to a candidate sitting exactly on the eligibility boundary, as a fraction of
#: ``SCORE_WINDOW``. The best candidate therefore outweighs the worst eligible one 5:1 — near-best
#: choices dominate, but the tail is still reachable, which is what produces visible variation.
_WEIGHT_FLOOR = 0.25


def normalize_seed(value) -> int:
    """Coerce any UI/CLI value to a usable seed. Anything not a positive integer becomes legacy.

    Gradio's number box yields floats, an emptied box yields ``None``, and a hand-typed value can be
    anything at all. None of that should raise in the middle of a render, and none of it should be
    guessed at — a value that is not a positive whole number is simply not a variation request.
    """
    try:
        seed = int(value)
    except (TypeError, ValueError, OverflowError):
        return LEGACY_SEED
    return seed if seed > 0 else LEGACY_SEED


def is_variation(seed) -> bool:
    """True when this seed should use seeded selection rather than the legacy argmax."""
    return normalize_seed(seed) != LEGACY_SEED


def select_index(scores: Sequence[float], rng: random.Random) -> int:
    """Pick one index reproducibly from the best-scoring candidates.

    ``scores`` are the planner's final per-candidate scores, penalties already applied. The rule:
    take the best :data:`TOP_K`, drop anything more than :data:`SCORE_WINDOW` below the best, then
    make a weighted draw favouring the candidates nearest the best.

    Ties break on the lower index, so the eligible set is a pure function of the scores and the
    result depends only on ``rng`` — which the caller derives from the user's seed.
    """
    if not scores:
        raise ValueError("select_index requires at least one score")

    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    top = order[:TOP_K]
    floor = scores[top[0]] - SCORE_WINDOW
    eligible = [i for i in top if scores[i] >= floor]
    if len(eligible) == 1:
        return eligible[0]

    weights = [(scores[i] - floor) + _WEIGHT_FLOOR * SCORE_WINDOW for i in eligible]
    return rng.choices(eligible, weights=weights, k=1)[0]


def random_seed() -> int:
    """A fresh positive seed for the 🎲 Randomize button. Six digits: short enough to read aloud."""
    return random.SystemRandom().randrange(1, 1_000_000)


def describe(seed) -> str:
    """Short human-readable mode label: ``legacy`` or ``seed 381944``."""
    normalized = normalize_seed(seed)
    return "legacy" if normalized == LEGACY_SEED else f"seed {normalized}"


def filename_suffix(seed) -> str:
    """``_seed381944`` for a variation render, empty for legacy — today's names must not change."""
    normalized = normalize_seed(seed)
    return "" if normalized == LEGACY_SEED else f"_seed{normalized}"
