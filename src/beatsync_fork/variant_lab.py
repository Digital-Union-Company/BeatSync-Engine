#!/usr/bin/env python3
"""[FORK] Digital-Union: Variant Lab V1 — the reproducible recipe generator (C2).

The seed stopped being the only exploration axis. Variant Lab lets the user declare *which* of the
six creative controls may vary, *within what bounds*, and *how far from the base* — then resolves
exactly **one** reproducible :class:`~beatsync_fork.creative_recipe.CreativeRecipe`::

    VARIANT LAB        = what MAY vary          (configuration, ephemeral)
    CREATIVE RECIPE    = what WILL be rendered  (seven integers, the execution artifact)
    THE SIX SLIDERS    = where the recipe lands (unchanged execution truth)

V1 generates one recipe at a time and renders nothing.

**Multi-variant generation and comparison shipped as C3 V1**, in the sibling module
:mod:`beatsync_fork.variant_batch`, which derives one candidate master per index under
:data:`DOMAIN_BATCH` and then calls :func:`resolve` / :func:`resolve_audio` here **unchanged** —
this module gained one domain constant and nothing else. **Batch rendering remains deliberately
absent**: C3 generates, compares and applies exactly one candidate, and Create Music Video is still
the only thing that renders.

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
    rng_for(master, "audio", "sfx_amount")          -> one audio control, forever (E2)

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

===============================================================================
Audio variation (E2 V1) is a parallel path, never a wider recipe
===============================================================================

E2 varies exactly three audio values — :data:`AUDIO_CONTROL_FIELDS` — through
:func:`resolve_audio`, which is a **sibling** of :func:`resolve` rather than an extension of it.
:func:`_resolve_v1` is deliberately untouched, so every C2 golden vector is preserved structurally
and not merely by test.

The three were chosen because they are the audio settings that are already plain ``0..100``
integers, which lets them reuse :class:`ControlRange`, :func:`_resolve_control` and :func:`_half_up`
verbatim — no second range implementation, no fractional-seconds model, no boolean randomization.
Everything else in Audio Layers / Smart Mix is deliberately excluded and stays a user decision:
voice clips and the SFX folder are **resource identity**; ``avoid_drops`` is a *protective* rule
with measured evidence behind it (a clip starting 0.44 s before a drop puts 96% of its speech
inside that drop); enabled SFX roles are **structural intent** whose toggling would perturb the
frozen cross-role occupancy; and the two fractional-seconds controls are deferred because they are
placement controls whose draws can legitimately make a render *refuse*.

Two invariants worth stating plainly:

* **The master seed and Spread are shared with the visual side.** There is no second audio seed and
  no second audio Spread — one master, one wildness dial.
* **The default audio randomize selection is EMPTY**, deliberately unlike the visual side's
  all-six. Opening an existing C2 Variant Lab and pressing Generate must not silently move a mix
  level, so audio variation is opt-in.

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

from beatsync_fork import audio_mix as fork_audio_mix
from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets
from beatsync_fork import smart_mix as fork_smart_mix
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
#: creative control; `audio` yields one stream per E2 audio control (`resolve_audio`); `batch`
#: yields one candidate master seed per index for C3 multi-variant generation
#: (`variant_batch.candidate_master_seed`). They are distinct key components, so a draw in one can
#: never shift a draw in another — which is what let E2 and then C3 land without re-keying a single
#: master seed a user has written down.
#:
#: The registry lives here, in one place, even though C3's *orchestration* lives in
#: `variant_batch.py`: a domain list split across two modules is how two domains eventually collide.
#: The dependency stays one-way — `variant_batch` imports this module, never the reverse.
DOMAIN_CLIPS = "clips"
DOMAIN_CONTROLS = "controls"
DOMAIN_AUDIO = "audio"
DOMAIN_BATCH = "batch"

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
                     control_range: ControlRange, spread01: float,
                     domain: str = DOMAIN_CONTROLS) -> int:
    """One randomized control under the frozen V1 formula.

    ``u`` is drawn from this control's **own** named stream, so the value depends on nothing but
    ``(master seed, this domain, this control's name, this control's range, the base, the spread)``.

    ``domain`` defaults to :data:`DOMAIN_CONTROLS`, so the C2 call site in :func:`_resolve_v1` is
    unchanged and every visual golden vector keys exactly as before. E2 passes
    :data:`DOMAIN_AUDIO` instead, which is why the audio and visual streams for a shared name can
    never collide — and why this parameter exists rather than a second copy of the formula.
    """
    anchor = control_range.anchor_for(base)
    u = rng_for(master_seed, domain, name).uniform(-1.0, 1.0)
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


# ---------------------------------------------------------------------------
# E2: audio variation. A parallel path — `resolve` and `_resolve_v1` are deliberately untouched,
# so every C2 golden vector is preserved structurally rather than merely by assertion.
# ---------------------------------------------------------------------------

#: The three E2 V1 audio controls, in report and widget-output order. Each string is **also** its
#: RNG stream name under :data:`DOMAIN_AUDIO`, so these spellings are frozen forever: renaming one
#: would silently re-key that control for every master seed a user has written down. They are the
#: `AudioMixConfig` / `SmartMixConfig` field names rather than the GUI's widget variable names, so
#: they cannot drift from the thing they actually control.
AUDIO_CONTROL_FIELDS = (
    "music_under_voice_percent",
    "sfx_amount",
    "sfx_level_percent",
)


def _is_plain_int(value: Any) -> bool:
    """A real ``int``, never a ``bool``.

    Same rule and same reason as ``creative_recipe``'s own predicate: ``bool`` subclasses ``int``,
    so ``True`` would otherwise be accepted as the control value 1. Restated here rather than
    imported because that one is private to its module — a three-line predicate is a smaller cost
    than reaching across a module boundary for a name that is deliberately not exported.
    """
    return isinstance(value, int) and not isinstance(value, bool)


#: Each audio control's authoritative normaliser, owned by the module that owns the control.
#: Delegated, never reimplemented — and the delegation is load-bearing rather than tidy:
#: ``music_under_voice_percent``'s malformed fallback is **35** (Audio Layers) while both Smart Mix
#: controls fall back to **50**, so routing all three through ``creative.normalize_control`` (whose
#: fallback is 50) would quietly change the music floor the user gets from a malformed widget value.
_AUDIO_BASE_NORMALISERS = {
    "music_under_voice_percent": fork_audio_mix.normalize_music_under_voice,
    "sfx_amount": lambda value: fork_smart_mix.normalize_control(
        value, fork_smart_mix.DEFAULT_AMOUNT),
    "sfx_level_percent": lambda value: fork_smart_mix.normalize_control(
        value, fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT),
}


def normalize_audio_base_value(name: str, value: Any) -> int:
    """One audio base value, through the normaliser that **owns** that control.

    Delegated for exactly the reason :func:`normalize_master_seed` delegates to
    ``variation.normalize_seed``: the fork already has one answer per control, and here the three
    answers genuinely differ. The GUI normalises with these same functions before calling, so the
    two cannot disagree; doing it here as well means a future call site that forgets still gets the
    control's real default instead of a crash or an invented number.

    An unknown name is not an audio control and has no owning default, so it resolves to the
    neutral 50 rather than raising inside a UI callback.
    """
    normaliser = _AUDIO_BASE_NORMALISERS.get(name)
    if normaliser is None:
        return fork_creative.DEFAULT_CONTROL
    return normaliser(value)


def normalize_audio_randomized(fields: Any) -> frozenset:
    """The set of audio control names that may vary, filtered to the three that exist.

    Same total, never-raising contract as :func:`normalize_randomized`, and separate from it
    deliberately: the two read different name vocabularies, and one shared helper taking a
    vocabulary argument would make passing the wrong vocabulary possible at a call site. An unknown
    name is dropped — there is no stream for it and no widget to write it to.
    """
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Iterable):
        return frozenset()
    return frozenset(name for name in fields if name in AUDIO_CONTROL_FIELDS)


def default_audio_ranges() -> dict:
    """First-open audio ranges: the whole 0..100 control for each of the three."""
    return {name: FULL_RANGE for name in AUDIO_CONTROL_FIELDS}


def default_audio_randomized() -> frozenset:
    """First-open audio randomize selection: **none**.

    Deliberately not the visual side's all-six default, and this is the load-bearing half of E2's
    backward compatibility: a user who opens an existing C2 Variant Lab and presses Generate must
    get their mix levels back untouched. Audio variation is opt-in. A test pins it.
    """
    return frozenset()


@dataclass(frozen=True)
class AudioVariantConfig:
    """What audio *may* vary, and within what bounds. Normalised on construction.

    Deliberately absent: the master seed, the Spread and the base values. The first two are the
    existing **shared** Variant Lab controls — there is no second audio seed and no second audio
    Spread — and the third is the live widgets, so holding any of them here would create exactly the
    stale snapshot C2 refuses to keep.
    """

    randomized: frozenset = frozenset()
    ranges: Mapping[str, ControlRange] = MappingProxyType({})

    def __post_init__(self) -> None:
        object.__setattr__(self, "randomized", normalize_audio_randomized(self.randomized))
        ranges = {}
        supplied = self.ranges if isinstance(self.ranges, Mapping) else {}
        for name in AUDIO_CONTROL_FIELDS:
            value = supplied.get(name)
            if isinstance(value, ControlRange):
                ranges[name] = value
            elif isinstance(value, (tuple, list)) and len(value) == 2:
                ranges[name] = ControlRange(value[0], value[1])
            else:
                ranges[name] = FULL_RANGE
        object.__setattr__(self, "ranges", MappingProxyType(ranges))

    @property
    def varies_anything(self) -> bool:
        """False for the default empty selection, i.e. audio variation is switched off."""
        return bool(self.randomized)

    def range_for(self, name: str) -> ControlRange:
        return self.ranges.get(name, FULL_RANGE)

    def is_randomized(self, name: str) -> bool:
        return name in self.randomized


@dataclass(frozen=True)
class AudioRecipe:
    """One render's exact resolved audio values. Immutable and always valid.

    Mirrors :class:`~beatsync_fork.creative_recipe.CreativeRecipe`'s trust contract: validation
    **raises**, nothing is clamped and nothing falls back, because an instance that exists must be
    an instance that is valid. Clamping here would quietly turn a generator's mistake into a
    plausible-looking render — and normalisation has already happened upstream, in the authoritative
    Audio Layers / Smart Mix normalisers.

    It is **not** a field of ``CreativeRecipe`` and must never become one. It carries no path, no
    SFX role, no voice clip, no master seed, no Spread and no range: those are resource identity,
    structural intent or generator provenance, and none of them is a statement about the audio a
    render will produce.
    """

    music_under_voice_percent: int
    sfx_amount: int
    sfx_level_percent: int

    def __post_init__(self) -> None:
        for name in AUDIO_CONTROL_FIELDS:
            value = getattr(self, name)
            if not _is_plain_int(value) or not (
                    fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX):
                raise ValueError(
                    f"audio recipe {name} must be a plain int in "
                    f"{fork_creative.CONTROL_MIN}..{fork_creative.CONTROL_MAX}, got {value!r}")

    def as_mapping(self) -> dict:
        """A **fresh** plain dict of plain ints, so a caller may mutate it without reaching here."""
        return {name: getattr(self, name) for name in AUDIO_CONTROL_FIELDS}

    def describe(self) -> str:
        """One compact line. Diagnostic, never execution-authoritative."""
        return " · ".join([
            f"Music under voice {self.music_under_voice_percent}",
            f"SFX Amount {self.sfx_amount}",
            f"SFX Level {self.sfx_level_percent}",
        ])


@dataclass(frozen=True)
class AudioVariantResolution:
    """One audio resolution: the provenance that produced it, and the values it produced.

    Provenance lives here and never on :class:`AudioRecipe`, exactly as
    :class:`VariantLabResolution` carries it for the visual side. The three visible audio widgets
    remain the render's execution truth; this object is a read-out.
    """

    master_seed: int
    algorithm_version: int
    spread: int
    config: AudioVariantConfig
    recipe: AudioRecipe

    @property
    def fixed_fields(self) -> frozenset:
        return frozenset(AUDIO_CONTROL_FIELDS) - self.config.randomized

    def describe(self) -> str:
        """The audio half of the Variant Lab read-out, and the **one** formatter for this text.

        ``gui.py`` joins this with the visual ``describe()`` and formats nothing itself, so each
        half of the report has exactly one writer. The qualifier lines matter: with nothing selected
        or at spread 0 the audio values are simply the base, and a read-out that did not say so
        would read as though a variation had been applied.
        """
        randomized = len(self.config.randomized)
        total = len(AUDIO_CONTROL_FIELDS)
        headline = f"Audio · {randomized} randomized / {total - randomized} fixed"
        if not randomized:
            headline += " (audio variation off)"
        elif self.spread == 0:
            headline += " (spread 0 — held at base)"
        return "\n".join([headline, self.recipe.describe()])


def resolve_audio(master_seed: int, config: AudioVariantConfig, spread: Any,
                  base: Mapping[str, Any]) -> AudioVariantResolution:
    """Resolve exactly one audio recipe from the **shared** master seed and Spread plus the live
    audio widget values.

    Pure: no ``SystemRandom``, no clock, no filesystem, no GUI, no Stage 5, no FFmpeg, no hidden
    state. The same ``(master, config, spread, base)`` reproduces exactly.

    Each randomized field draws ``u`` from ``rng_for(master, DOMAIN_AUDIO, field_name)`` and goes
    through the *same* 0..100 range/spread formula as a visual control, so there is one spread rule
    in this module rather than two. **Spread 0 leaves all three at their base values** — there is no
    audio analogue of C2's clip Variation Seed, so unlike the visual side nothing else varies.

    Randomize OFF means *leave this control alone*: the exact normalised live value, with its
    configured range ignored entirely rather than used to clamp — clamping an excluded control would
    make the checkbox mean something weaker than off.
    """
    master = normalize_master_seed(master_seed)
    if master <= 0:
        raise ValueError("Variant Lab requires a positive master seed; got "
                         f"{master_seed!r}")
    resolved_spread = normalize_spread(spread)
    spread01 = resolved_spread / 100.0
    supplied = base if isinstance(base, Mapping) else {}
    values = {}
    for name in AUDIO_CONTROL_FIELDS:
        live = normalize_audio_base_value(name, supplied.get(name))
        if not config.is_randomized(name):
            values[name] = live
            continue
        values[name] = _resolve_control(
            master, name, live, config.range_for(name), spread01, DOMAIN_AUDIO)
    return AudioVariantResolution(
        master_seed=master,
        algorithm_version=VARIANT_LAB_ALGORITHM_VERSION,
        spread=resolved_spread,
        config=config,
        recipe=AudioRecipe(**values),
    )


__all__ = [
    "AUDIO_CONTROL_FIELDS",
    "DEFAULT_RANGE_HI",
    "DEFAULT_RANGE_LO",
    "DEFAULT_VARIATION_SPREAD",
    "DOMAIN_AUDIO",
    "DOMAIN_BATCH",
    "DOMAIN_CLIPS",
    "DOMAIN_CONTROLS",
    "FULL_RANGE",
    "NAMESPACE",
    "SPREAD_MAX",
    "SPREAD_MIN",
    "VARIANT_LAB_ALGORITHM_VERSION",
    "AudioRecipe",
    "AudioVariantConfig",
    "AudioVariantResolution",
    "ControlRange",
    "VariantLabConfig",
    "VariantLabResolution",
    "default_audio_randomized",
    "default_audio_ranges",
    "default_randomized",
    "default_ranges",
    "normalize_audio_base_value",
    "normalize_audio_randomized",
    "normalize_master_seed",
    "normalize_randomized",
    "normalize_spread",
    "resolve",
    "resolve_audio",
    "resolve_clip_seed",
    "rng_for",
]
