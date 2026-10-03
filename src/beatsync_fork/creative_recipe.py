#!/usr/bin/env python3
"""[FORK] Digital-Union: the resolved Creative Recipe (Variant Lab C2 V1).

A **recipe is the exact resolved VISUAL creative configuration for one render** — seven integers
and nothing else::

    seed                     1..999999, POSITIVE
    cut_density              0..100
    micro_cuts               0..100
    semantic_emphasis        0..100
    energy_response          0..100
    motion_bias              0..100
    source_diversity         0..100

Those seven are complete and durable *for the creative interpretation they stand for*, which is the
point: a recipe is a *name for how the edit will be made*, not a new interpretation of it.
:meth:`CreativeRecipe.to_profile` hands it straight to the existing
:class:`~beatsync_fork.creative.CreativeProfile`, so no stage learns that recipes exist and there is
no second planner path. That central architecture is unchanged and is the whole design::

    CreativeRecipe  ->  CreativeProfile  ->  the existing planner

They are **not**, however, a complete description of the physical render, and since E2 V1 that
distinction is load-bearing rather than pedantic. Variant Lab's audio half resolves a **sibling**
artifact — ``AudioRecipe``, three generated 0..100 levels — which is deliberately *not* a field of
this class, is deliberately *not* imported here, and reaches a render through its own existing
widgets rather than through ``CreativeProfile``. This module must keep depending on nothing but
:mod:`beatsync_fork.creative`; the sibling contract lives next to the generator that produces it.

Plenty of render intent sits outside **both** recipes and is never generated at all: the source
media, the voice clips, their timing and ``avoid_drops``, the SFX folder and the enabled roles, and
the output / encoder settings. So replaying a recipe reproduces the edit's creative configuration —
not, on its own, a byte-identical render.

===============================================================================
Why this is a separate object from the generator that produced it
===============================================================================

Variant Lab produces a recipe from a master seed, ranges and a spread. A future AI Director would
produce one from something else entirely, and must not have to *pretend* it had a master seed or
random ranges in order to express its answer. So generator provenance lives on
``variant_lab.VariantLabResolution`` and never here: this module deliberately carries no
``master_seed``, no ``spread``, no ``ranges``, no ``randomized_fields`` and no
``algorithm_version``. It carries no ``preset_label`` either — which named preset a tuple of six
values happens to match is a GUI read-out, not a property of the render.

===============================================================================
Two trust contracts, deliberately opposite
===============================================================================

:meth:`CreativeRecipe.from_mapping` is **all-or-nothing**: one bad field rejects the whole recipe
and returns ``None``. ``CreativeProfile.from_mapping`` is the opposite — lenient, per-field, falling
back to neutral — and both are right for their jobs:

* ``CreativeProfile`` reads a possibly-stale *internal bus* and must survive anything it finds
  there without raising mid-render, so a malformed field becomes 50 and the render continues.
* ``CreativeRecipe`` is a *contract boundary* for a future untrusted generator. Half-applying a
  model's output — three fields from the model and three silent 50s — is the worst available
  outcome, because the result looks deliberate and is not. Failing the recipe as one unit is what
  makes a bad generator visible.

Constructing an invalid recipe **raises**, so there is no such thing as a half-trusted instance;
only the untrusted-mapping entry point degrades softly to ``None``.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): seven integers and their
validation, testable on a bare interpreter.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from beatsync_fork import creative as fork_creative
from beatsync_fork import presets as fork_presets

#: The clip Variation Seed a recipe may carry. **Never 0** — seed 0 is the planner's legacy branch,
#: and a generated variant must always be an actual variation. The upper bound matches the existing
#: ``variation.random_seed()`` idiom (six digits, short enough to read aloud).
RECIPE_SEED_MIN = 1
RECIPE_SEED_MAX = 999_999

#: The exact key set of a recipe mapping: the clip seed first, then the six creative controls in
#: ``presets.CREATIVE_CONTROL_FIELDS`` order. Derived rather than restated so the recipe and the
#: preset/slider ordering can never drift apart.
VARIANT_RECIPE_FIELDS = ("seed",) + fork_presets.CREATIVE_CONTROL_FIELDS


def _is_plain_int(value: Any) -> bool:
    """A real ``int``, never a ``bool``.

    ``bool`` subclasses ``int``, so ``True`` would otherwise be accepted as the control value 1 and
    ``seed=True`` as seed 1. A recipe is a precise statement; a checkbox-shaped value is not one.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_valid_seed(value: Any) -> bool:
    return _is_plain_int(value) and RECIPE_SEED_MIN <= value <= RECIPE_SEED_MAX


def _is_valid_control(value: Any) -> bool:
    return _is_plain_int(value) and fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX


@dataclass(frozen=True)
class CreativeRecipe:
    """One render's exact resolved creative configuration. Immutable and always valid.

    Validation happens in ``__post_init__`` and **raises** ``ValueError``: an instance that exists
    is an instance that is valid. Nothing is clamped, nothing falls back and nothing is coerced —
    those are normalisation behaviours, and normalising here would quietly turn a generator's
    mistake into a plausible-looking render.
    """

    seed: int
    cut_density: int
    micro_cuts: int
    semantic_emphasis: int
    energy_response: int
    motion_bias: int
    source_diversity: int

    def __post_init__(self) -> None:
        if not _is_valid_seed(self.seed):
            raise ValueError(
                f"recipe seed must be a plain int in "
                f"{RECIPE_SEED_MIN}..{RECIPE_SEED_MAX}, got {self.seed!r}")
        for name in fork_presets.CREATIVE_CONTROL_FIELDS:
            value = getattr(self, name)
            if not _is_valid_control(value):
                raise ValueError(
                    f"recipe {name} must be a plain int in "
                    f"{fork_creative.CONTROL_MIN}..{fork_creative.CONTROL_MAX}, got {value!r}")

    # -- construction -------------------------------------------------------

    @classmethod
    def from_mapping(cls, mapping: Any) -> "CreativeRecipe | None":
        """Parse an **untrusted** recipe mapping, or ``None`` if it is not exactly one.

        All-or-nothing, and the key set must match :data:`VARIANT_RECIPE_FIELDS` *exactly* — a
        missing field is not a recipe, and an unexpected one means the producer and this contract
        disagree about what a recipe is, which is precisely the disagreement worth failing on.

        Never raises: the whole point of this entry point is that an untrusted producer cannot take
        the UI down with it.
        """
        if not isinstance(mapping, Mapping):
            return None
        if set(mapping) != set(VARIANT_RECIPE_FIELDS):
            return None
        if not _is_valid_seed(mapping["seed"]):
            return None
        for name in fork_presets.CREATIVE_CONTROL_FIELDS:
            if not _is_valid_control(mapping[name]):
                return None
        return cls(**{name: mapping[name] for name in VARIANT_RECIPE_FIELDS})

    # -- transport ----------------------------------------------------------

    def as_mapping(self) -> dict:
        """The authoritative serialisation: a **fresh** plain ``dict`` of plain ``int``s.

        Fresh each call so a caller may mutate the result without reaching the recipe, which is
        frozen and must stay the single source of truth for the render it describes.
        """
        return {name: getattr(self, name) for name in VARIANT_RECIPE_FIELDS}

    # -- the bridge to execution --------------------------------------------

    def to_profile(self) -> fork_creative.CreativeProfile:
        """The existing execution object, with no semantic difference.

        Every value is already valid, so ``CreativeProfile``'s own normalisation is a no-op here and
        ``to_profile().as_dict() == as_mapping()`` exactly — asserted by test. This is a *bridge*,
        not a second interpretation: the planner keeps receiving the profile it has always received
        and never learns how the numbers were chosen.
        """
        return fork_creative.CreativeProfile(**self.as_mapping())

    # -- reporting ----------------------------------------------------------

    def describe(self) -> str:
        """One compact line for the Variant Lab read-out. Diagnostic, never execution-authoritative."""
        return " · ".join([
            f"Clip seed {self.seed}",
            f"Cut {self.cut_density}",
            f"Micro {self.micro_cuts}",
            f"Semantic {self.semantic_emphasis}",
            f"Energy {self.energy_response}",
            f"Motion {self.motion_bias}",
            f"Diversity {self.source_diversity}",
        ])


# A structural guard rather than a comment: the dataclass fields and the declared key set are the
# same thing, so adding a field without extending `VARIANT_RECIPE_FIELDS` (or vice versa) fails at
# import time instead of silently producing recipes that do not round trip.
assert tuple(f.name for f in fields(CreativeRecipe)) == VARIANT_RECIPE_FIELDS


__all__ = [
    "RECIPE_SEED_MAX",
    "RECIPE_SEED_MIN",
    "VARIANT_RECIPE_FIELDS",
    "CreativeRecipe",
]
