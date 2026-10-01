#!/usr/bin/env python3
"""[FORK] Digital-Union: Variant Lab V1 — the reproducible recipe generator (C2).

The seed stopped being the only exploration axis. Variant Lab lets the user declare *which* of the
six creative controls may vary, *within what bounds*, and *how far from the base* — then resolves
exactly **one** reproducible :class:`~beatsync_fork.creative_recipe.CreativeRecipe`::

    VARIANT LAB        = what MAY vary          (configuration, ephemeral)
    CREATIVE RECIPE    = what WILL be rendered  (seven integers, the execution artifact)
    THE SIX SLIDERS    = where the recipe lands (unchanged execution truth)

V1 generates one recipe at a time and renders nothing. Multi-variant generation, batch rendering
and variant comparison are C3 and are deliberately absent.

===============================================================================
The base is always the live sliders
===============================================================================

There is no base-profile control in here and no cached base snapshot: :func:`resolve` is handed the
six *current* values at click time. Pick Balanced and you explore around Balanced; pick Cinematic
and you explore around Cinematic; hand-tune and you explore around that. The preset *name* is never
read — only the six numbers — so Variant Lab and Presets compose without either knowing about the
other. Ranges are **explicit constraints** and deliberately do **not** follow the base around: when
the base moves outside a range, :func:`resolve` clamps the anchor into it rather than inventing an
error state.

===============================================================================
Named RNG sub-streams — the load-bearing part
===============================================================================

Every randomized control draws from its **own** named stream, derived from the master seed by hash
rather than by position in a sequence::

    rng_for(master, "clips")                        -> the clip Variation Seed
    rng_for(master, "controls", "cut_density")      -> one control, forever
    rng_for(master, "audio", <name>)                -> reserved for E2, unused today

A single ``random.Random(master)`` consumed in declaration order would be far simpler and is exactly
what this must not be: adding one control in a future release would shift every later draw, so every
previously recorded master seed would silently resolve to a different recipe. With named streams a
control's value depends on **its own name only**, so enabling another control, re-ranging another
control, reordering the declarations, appending a future control and adding a whole future *domain*
all leave it untouched. Those five properties are the principal C2 regression contract and each has
its own test.

``hashlib`` and not ``hash()``: the builtin is randomised per process, so it would not even survive
a restart.

===============================================================================
Spread, precisely
===============================================================================

Spread is distance from the base, not height: *"Conservative ↔ Crazy"*, never *"low ↔ high"*. Each
control's direction is drawn independently, so a wild recipe legitimately reads "dense cuts, calm
footage, low energy response, high source diversity".

The contract on bias is **directional**, and the wording matters because the obvious phrasing is
false: the anchor is the directional *median* and up/down is approximately a fair coin, but mean
displacement is **not** zero whenever the anchor sits off-centre in its range. Base 30 in 0..100 has
70 points of headroom above and 30 below, so it drifts upward on average — that is the range
speaking, and it is intended. A base already sitting on a range endpoint can only move inward, and
will therefore sit still for roughly half of all master seeds. None of that is a defect to be
"fixed"; a formula without it could not reach both endpoints at full spread.

Measured on real material (Nero track, 41-source TEST1 library, 240 resolved recipes through the
real Stage 4 and Stage 6): every recipe produced a complete plan, zero fallbacks, zero adjacent
candidate repeats, and an unchanged minimum cut gap. At spread 100 over 200 master seeds, 99% of
recipes moved controls in *mixed* directions and only 2 in 200 moved all six the same way — "crazy"
is not "everything at maximum".

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): arithmetic over small integers
plus a hash, testable on a bare interpreter with no Gradio, numpy, CUDA or FFmpeg.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets
from beatsync_fork import variation as fork_variation

#: Namespace of every Variant Lab RNG derivation. Distinct from the planner's own `_stable_rng`
#: keys, so the two can never collide however either is extended.
NAMESPACE = "variant_lab"

#: The resolver generation. It participates in **every** RNG derivation, so a future algorithm
#: change bumps this and visibly produces different recipes rather than silently doing so.
#:
#: There is deliberately no version-dispatch table today: `resolve` delegates straight to
#: `_resolve_v1`. A dispatch dict with one entry is machinery for a migration that has not happened.
#: If a V2 is ever genuinely implemented, `_resolve_v1` is retained and dispatch is added *then* —
#: and even that is a convenience, because the durable execution artifact is the resolved
#: seven-integer recipe, not the master seed that generated it.
VARIANT_LAB_ALGORITHM_VERSION = 1

#: RNG domains. `clips` yields the recipe's clip Variation Seed; `controls` yields one stream per
#: creative control. `audio` is reserved for E2's future audio controls and is named here only so
#: the namespace design is testable today — nothing uses it.
DOMAIN_CLIPS = "clips"
DOMAIN_CONTROLS = "controls"
DOMAIN_AUDIO = "audio"

#: Variation Spread: 0..100, neutral-by-default at 50 like every other creative control. Measured:
#: at 50 a variant moves five of six controls by >= 5 points and shifts the real cut count by a
#: median 8%, with no endpoint collapse and every plan complete.
SPREAD_MIN = 0
SPREAD_MAX = 100
DEFAULT_VARIATION_SPREAD = 50

#: First-open defaults: every control may vary, across its whole range. Range-narrowing and spread
#: are measurably redundant knobs (`base +/- 25 at spread 50` is numerically identical to
#: `full range at spread 25`), so Spread is the single wildness dial and ranges exist to express a
#: genuine constraint — "never let Cut Density below 40". Source Diversity is **not** quietly
#: narrowed: a default that suppresses a shipped control would be an opinion in disguise.
DEFAULT_RANGE_LO = fork_creative.CONTROL_MIN
DEFAULT_RANGE_HI = fork_creative.CONTROL_MAX


def _stable_rng(*parts: Any) -> random.Random:
    """A ``random.Random`` derived from the parts by hash, identically on every machine and run.

    Same idiom as the planner's own ``_stable_rng`` — joined with ``|``, SHA-1, first 12 hex digits —
    so the fork has one way of doing this rather than two. ``errors="ignore"`` matches it as well.
    The exact key format is pinned by golden-vector tests: a change to the separator, the digest,
    the slice width or the component order would otherwise silently invalidate every master seed a
    user has ever written down.
    """
    raw = "|".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return random.Random(int(digest[:12], 16))


def rng_for(master_seed: int, domain: str, name: str = "") -> random.Random:
    """The named sub-stream for one ``(master seed, domain, name)``.

    The namespace and the algorithm version lead every key, so a Variant Lab stream can never
    collide with another ``_stable_rng`` user and a version bump re-keys everything at once.
    """
    return _stable_rng(NAMESPACE, VARIANT_LAB_ALGORITHM_VERSION, master_seed, domain, name)


# ---------------------------------------------------------------------------
# normalisation — total, never raising
# ---------------------------------------------------------------------------


def normalize_master_seed(value: Any) -> int:
    """Coerce a master seed to a positive whole integer, or ``0`` meaning *unset*.

    Delegated to :func:`variation.normalize_seed` rather than reimplemented: the fork already has
    exactly one answer to "what is a positive whole seed", including why ``7.9``, ``"7.0"`` and
    ``True`` are all refused instead of guessed at. ``0`` is not a usable master seed — the GUI
    replaces it with a fresh visible one before resolving.
    """
    return fork_variation.normalize_seed(value)


def normalize_spread(value: Any) -> int:
    """Coerce Variation Spread to 0..100. Malformed input is the neutral default, not a crash.

    Delegated to ``creative.normalize_control``: Spread is an ordinary 0..100 control whose
    malformed-value fallback is 50, which is also its default — so there is no second normalisation
    rule to keep in step with the six it sits beside.
    """
    return fork_creative.normalize_control(value)


def _normalize_endpoint(value: Any, default: int) -> int:
    """One range endpoint: a plain int or a whole float, clamped. Anything else is the default.

    Strings are deliberately **not** parsed: a range box is a number box, and accepting ``"40"``
    here while :func:`normalize_master_seed` refuses ``"7.0"`` would make the module inconsistent
    about what counts as a number. Fractional values, ``NaN`` and both infinities fall back for the
    same reason ``creative.normalize_control`` refuses them — ``40.5`` is not a request for 40.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(fork_creative.CONTROL_MIN, min(fork_creative.CONTROL_MAX, value))
    if isinstance(value, float):
        # `is_integer()` is False for NaN and both infinities, so they need no separate guard.
        if value.is_integer():
            return max(fork_creative.CONTROL_MIN,
                       min(fork_creative.CONTROL_MAX, int(value)))
    return default


@dataclass(frozen=True)
class ControlRange:
    """The absolute bounds one randomized control may resolve inside. Normalised on construction."""

    lo: int = DEFAULT_RANGE_LO
    hi: int = DEFAULT_RANGE_HI

    def __post_init__(self) -> None:
        lo = _normalize_endpoint(self.lo, DEFAULT_RANGE_LO)
        hi = _normalize_endpoint(self.hi, DEFAULT_RANGE_HI)
        if lo > hi:
            # Deliberately NOT a silent swap. `gr.Number` guarantees no ordering, so "min 80, max
            # 20" is a typo whose only unambiguous half is the floor the user typed; swapping would
            # resolve a range they never asked for. Collapsing to a point is visible in the report.
            hi = lo
        object.__setattr__(self, "lo", lo)
        object.__setattr__(self, "hi", hi)

    @property
    def is_degenerate(self) -> bool:
        return self.lo == self.hi

    def anchor_for(self, base: int) -> int:
        """Where this control's variation is centred: the base, pulled into the allowed range.

        The range is authoritative. A base outside it is not an error — it is simply a base the user
        has since moved (ranges do not follow the sliders around), so it clamps.
        """
        return max(self.lo, min(self.hi, base))

    def as_tuple(self) -> tuple:
        return (self.lo, self.hi)


#: The neutral range: the whole control.
FULL_RANGE = ControlRange(DEFAULT_RANGE_LO, DEFAULT_RANGE_HI)


def normalize_randomized(fields: Any) -> frozenset:
    """The set of control names that may vary, filtered to the six that exist.

    Unknown names are **dropped deterministically** rather than raising: this runs on a widget value
    during a UI interaction, and an unknown name cannot influence resolution anyway — there is no
    stream for it and no slider to write it to. Dropping is observable in the report's
    "N randomized" count; raising would take the UI down for a value that means nothing.
    """
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Iterable):
        return frozenset()
    known = set(fork_presets.CREATIVE_CONTROL_FIELDS)
    return frozenset(name for name in fields if name in known)


def default_ranges() -> dict:
    """First-open ranges: the full control for every one of the six."""
    return {name: FULL_RANGE for name in fork_presets.CREATIVE_CONTROL_FIELDS}


def default_randomized() -> frozenset:
    """First-open randomize selection: all six."""
    return frozenset(fork_presets.CREATIVE_CONTROL_FIELDS)


# ---------------------------------------------------------------------------
# configuration and resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantLabConfig:
    """What *may* vary, and how far. Normalised on construction, so there is no half-trusted config.

    Deliberately absent: the base profile. The base is the live sliders and is passed to
    :func:`resolve` at click time, so this object can never hold a stale snapshot of it.
    """

    master_seed: int = 0
    spread: int = DEFAULT_VARIATION_SPREAD
    randomized: frozenset = frozenset()
    ranges: Mapping[str, ControlRange] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "master_seed", normalize_master_seed(self.master_seed))
        object.__setattr__(self, "spread", normalize_spread(self.spread))
        object.__setattr__(self, "randomized", normalize_randomized(self.randomized))
        ranges = {}
        supplied = self.ranges if isinstance(self.ranges, Mapping) else {}
        for name in fork_presets.CREATIVE_CONTROL_FIELDS:
            value = supplied.get(name)
            if isinstance(value, ControlRange):
                ranges[name] = value
            elif isinstance(value, (tuple, list)) and len(value) == 2:
                ranges[name] = ControlRange(value[0], value[1])
            else:
                ranges[name] = FULL_RANGE
        object.__setattr__(self, "ranges", MappingProxyType(ranges))

    @property
    def has_usable_master_seed(self) -> bool:
        """False when the master seed is unset; the GUI mints a visible one before resolving."""
        return self.master_seed > 0

    def range_for(self, name: str) -> ControlRange:
        return self.ranges.get(name, FULL_RANGE)

    def is_randomized(self, name: str) -> bool:
        return name in self.randomized


@dataclass(frozen=True)
class VariantLabResolution:
    """One resolution: the configuration that produced it, and the recipe it produced.

    This is where generator provenance lives — and the only place. ``CreativeRecipe`` stays a
    statement about the render, so a future non-random generator can emit one without inventing a
    master seed it never had.
    """

    config: VariantLabConfig
    algorithm_version: int
    recipe: fork_recipe.CreativeRecipe

    @property
    def fixed_fields(self) -> frozenset:
        return frozenset(fork_presets.CREATIVE_CONTROL_FIELDS) - self.config.randomized

    def describe(self) -> str:
        """The compact Variant Lab read-out. Diagnostic only — never execution-authoritative."""
        randomized = len(self.config.randomized)
        total = len(fork_presets.CREATIVE_CONTROL_FIELDS)
        return "\n".join([
            f"Variant Lab · algorithm v{self.algorithm_version}",
            f"Master seed {self.config.master_seed}",
            f"Spread {self.config.spread}",
            f"{randomized} randomized / {total - randomized} fixed",
            self.recipe.describe(),
        ])


def _half_up(value: float) -> int:
    """Explicit half-up rounding.

    Never :func:`round`: banker's rounding sends 2.5 and 3.5 both to even, quietly collapsing two
    different draws onto one value. Resolved values are always ``>= 0`` here (every bound is within
    0..100), so ``int(value + 0.5)`` is exactly half-up. Same idiom as ``creative.scale_beat_step``.
    """
    return int(value + 0.5)


def _resolve_control(master_seed: int, name: str, base: int,
                     control_range: ControlRange, spread01: float) -> int:
    """One randomized control under the frozen V1 formula.

    ``u`` is drawn from this control's **own** named stream, so the value depends on nothing but
    ``(master seed, this control's name, this control's range, the base, the spread)``.
    """
    anchor = control_range.anchor_for(base)
    u = rng_for(master_seed, DOMAIN_CONTROLS, name).uniform(-1.0, 1.0)
    if u < 0:
        raw = anchor + spread01 * u * (anchor - control_range.lo)
    else:
        raw = anchor + spread01 * u * (control_range.hi - anchor)
    return max(control_range.lo, min(control_range.hi, _half_up(raw)))


def resolve_clip_seed(master_seed: int) -> int:
    """The recipe's clip Variation Seed: positive, and derived **only** from the master seed.

    Its own named stream, so spread, ranges, which controls vary and any future control or domain
    leave it untouched for a given master seed — asserted by test. It is never 0: seed 0 is the
    planner's legacy branch, and a generated variant is by definition not that.
    """
    return rng_for(master_seed, DOMAIN_CLIPS).randint(
        fork_recipe.RECIPE_SEED_MIN, fork_recipe.RECIPE_SEED_MAX)


def _resolve_v1(config: VariantLabConfig, base: Mapping[str, int]) -> VariantLabResolution:
    """The frozen V1 resolver. Pure: no SystemRandom, no clock, no filesystem, no side effects."""
    spread01 = config.spread / 100.0
    values = {"seed": resolve_clip_seed(config.master_seed)}
    for name in fork_presets.CREATIVE_CONTROL_FIELDS:
        live = fork_creative.normalize_control(base.get(name))
        if not config.is_randomized(name):
            # Randomize OFF means "leave this control alone": the exact live value, and its
            # configured range is ignored entirely rather than being used to clamp it. Clamping a
            # control the user excluded would make the checkbox mean something weaker than off.
            values[name] = live
            continue
        values[name] = _resolve_control(
            config.master_seed, name, live, config.range_for(name), spread01)
    return VariantLabResolution(
        config=config,
        algorithm_version=VARIANT_LAB_ALGORITHM_VERSION,
        recipe=fork_recipe.CreativeRecipe(**values),
    )


def resolve(config: VariantLabConfig, base: Mapping[str, int]) -> VariantLabResolution:
    """Resolve exactly one recipe from a configuration and the six live base values.

    ``base`` is the live sliders, never a preset name and never a stored snapshot. Requires a usable
    master seed — the GUI mints and *displays* one first, so no hidden random value can ever reach a
    recipe.
    """
    if not config.has_usable_master_seed:
        raise ValueError("Variant Lab requires a positive master seed; got "
                         f"{config.master_seed!r}")
    return _resolve_v1(config, base)


__all__ = [
    "DEFAULT_RANGE_HI",
    "DEFAULT_RANGE_LO",
    "DEFAULT_VARIATION_SPREAD",
    "DOMAIN_AUDIO",
    "DOMAIN_CLIPS",
    "DOMAIN_CONTROLS",
    "FULL_RANGE",
    "NAMESPACE",
    "SPREAD_MAX",
    "SPREAD_MIN",
    "VARIANT_LAB_ALGORITHM_VERSION",
    "ControlRange",
    "VariantLabConfig",
    "VariantLabResolution",
    "default_randomized",
    "default_ranges",
    "normalize_master_seed",
    "normalize_randomized",
    "normalize_spread",
    "resolve",
    "resolve_clip_seed",
    "rng_for",
]
