#!/usr/bin/env python3
"""[FORK] Digital-Union: Variant Lab multi-variant generation and comparison (C3 V1).

C2 resolves **one** recipe per click and writes it straight back into the sliders. That is a good
way to wander and a poor way to *choose*: to compare two directions you have to generate one, read
it, generate another, and have already lost the first. C3 resolves **N candidates at once from one
frozen starting point**, shows them side by side, and applies exactly one::

    GENERATE N  ->  COMPARE N  ->  APPLY ONE  ->  (the user presses Create Music Video)

**C3 V1 renders nothing.** No batch rendering, no preview rendering, no stage cache, no shortlist.
Generating and applying candidates touch the same visible execution widgets C2 and E2 already own,
and `Create Music Video` remains the single explicit render action. Batch rendering is a separate,
deliberately deferred milestone: a render is ~150 FFmpeg clip extractions through one hardware
encoder that a *single* render already saturates, while a candidate here costs tens of
microseconds. Those two things do not belong in one feature.

===============================================================================
This module orchestrates the frozen resolvers — it never re-implements them
===============================================================================

Every candidate is produced by calling today's :func:`variant_lab.resolve` and
:func:`variant_lab.resolve_audio` **unchanged**, with a derived candidate master seed. There is no
second spread formula, no second range model, no second normaliser, and no edit to a C2 or E2
resolver — which is why every existing golden vector is preserved *structurally* rather than by
assertion.

The dependency direction is one-way and must stay that way::

    variant_batch  ->  variant_lab        (allowed)
    variant_lab    ->  variant_batch      (NEVER)

Stdlib-only (CLAUDE.md's hard rule): no Gradio, no upstream runtime, no filesystem, no Stage 5, no
FFmpeg, no renderer. Arithmetic, a hash it borrows from `variant_lab`, and immutable records.

===============================================================================
Candidate masters: one named stream per index
===============================================================================

A candidate's master seed comes from its **own** named sub-stream under the C3 domain::

    "variant_lab|1|<root master>|batch|<zero-based index>"  ->  SHA-1  ->  1..999999

Index-keyed rather than drawn sequentially from one stream, and that is the load-bearing choice:
asking for 8 candidates instead of 5 must leave the first 5 **identical**, which a sequential
`Random(root)` could not promise. The domain component keeps C3 draws out of ``clips``, ``controls``
and ``audio`` entirely, so adding this feature cannot shift a single value any existing master seed
already resolves to.

**Candidate masters are unique within a batch, as a contract rather than a probability.** Twelve
draws from a six-digit range collide with probability ~7e-5 — small, but two identical rows read as
a bug, and the same argument already settled `_fresh_variant_master_seed` in C2 R1-B: *"a product
contract, and leaving it to a one-in-a-million draw makes it a probability rather than a
guarantee."* So a collision steps deterministically forward (wrapping at the top of the range)
against the masters already fixed at **lower** indices only. Prefix stability therefore survives:
candidate *i* depends on its own draw plus indices 0..i-1, which are the same whether the batch is
N or N+1. Bounded by construction — at most :data:`CANDIDATE_COUNT_MAX` - 1 steps.

===============================================================================
A candidate master is provenance, not a recipe identifier
===============================================================================

C2's R1-A rule applies here unchanged and is the reason this module stores a declaration at all::

    candidate master
      + the SAME original base, ranges, randomize selections, Spread and algorithm version
      -> the same candidate

    candidate master alone
      -> NOT enough

Nothing in this module, the GUI or the help text may claim master-only replay.

===============================================================================
The batch is deepcopy-safe state, and that is a runtime requirement
===============================================================================

:class:`VariantBatch` is carried in a Gradio ``State``, which deep-copies its value. The existing
configuration objects cannot go in one: :class:`variant_lab.VariantLabConfig` and
:class:`variant_lab.AudioVariantConfig` hold ``MappingProxyType``, and the resolution objects hold
those configs. So this module stores **only plain immutable data** — ints, strings and tuples of
them, plus the two frozen recipe dataclasses that are themselves nothing but ints.

:func:`rehydrate` rebuilds *temporary* resolution objects when the GUI needs them for its existing
report/projection path. Those are built, used and dropped inside one handler; they are never
returned into state. A test asserts ``copy.deepcopy(batch) == batch`` and that no config or
resolution type appears anywhere inside a batch, because this is a real runtime failure mode rather
than a style preference.

===============================================================================
Staleness is answered by equality, not by a digest
===============================================================================

:class:`VariantBatchDeclaration` is a canonical, fully normalised, totally ordered record of
everything a batch was generated from. Deciding whether a candidate list still describes the screen
is therefore plain structural equality between two small frozen records — no hash, no canonical
serialisation contract, and nothing that could disagree with the values it claims to summarise. A
short digest is offered for *display* only (:meth:`VariantBatchDeclaration.short_digest`) and is
never the correctness authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets
from beatsync_fork import variant_lab as fork_lab

# ---------------------------------------------------------------------------
# Candidate count — a comparison-UX bound, not a resource bound
# ---------------------------------------------------------------------------

#: Two, because one candidate is exactly today's single Generate: the feature is the comparison.
CANDIDATE_COUNT_MIN = 2

#: Twelve. The binding constraint is **human comparison and table legibility**, not compute — a
#: candidate costs tens of microseconds, so twelve is roughly a millisecond. The cap exists so a
#: mistyped ``1000`` cannot produce a thousand-row read-out, and nothing more.
#:
#: **Do not reuse this number as a future automatic-render batch limit.** Generating a candidate and
#: rendering one differ by about six orders of magnitude; a shared limit would be unargued.
CANDIDATE_COUNT_MAX = 12

#: Five: enough rows to show real spread without becoming a wall of numbers.
CANDIDATE_COUNT_DEFAULT = 5

#: Candidate masters live in the same six-digit range as every other seed this project shows a
#: user, so a candidate master is a value that can be read off the table and typed into the Master
#: Seed box. (``variation.random_seed()`` draws the root from 1..999999 and these two bound the clip
#: seed identically — the range is borrowed rather than restated.)
CANDIDATE_MASTER_MIN = fork_recipe.RECIPE_SEED_MIN
CANDIDATE_MASTER_MAX = fork_recipe.RECIPE_SEED_MAX


def normalize_candidate_count(value: Any) -> int:
    """Coerce a candidate count to :data:`CANDIDATE_COUNT_MIN`..:data:`CANDIDATE_COUNT_MAX`.

    Total and never raising, with the same explicit type boundary as
    ``variant_lab._normalize_endpoint`` rather than a third rule: ``bool`` is rejected first
    (``True`` would otherwise read as a count of one), a whole float is accepted because a number
    box reports ``5.0``, and a fractional value, ``NaN``, ``inf``, a string or ``None`` falls back
    to the default instead of being floored into a count nobody asked for. Out-of-range values
    **clamp**, because the count has meaningful ends.
    """
    if isinstance(value, bool):
        return CANDIDATE_COUNT_DEFAULT
    if isinstance(value, int):
        return max(CANDIDATE_COUNT_MIN, min(CANDIDATE_COUNT_MAX, value))
    if isinstance(value, float):
        # `is_integer()` is False for NaN and both infinities, so they need no separate guard.
        if value.is_integer():
            return max(CANDIDATE_COUNT_MIN, min(CANDIDATE_COUNT_MAX, int(value)))
    return CANDIDATE_COUNT_DEFAULT


# ---------------------------------------------------------------------------
# Candidate master derivation
# ---------------------------------------------------------------------------


def candidate_master_seed(root_master_seed: int, index: int) -> int:
    """The raw draw for one candidate index, before batch-level uniqueness is applied.

    Keyed ``variant_lab|1|<root>|batch|<index>`` through the shared :func:`variant_lab.rng_for`, so
    there is one key format in the fork rather than two, and so a C3 draw can never collide with a
    ``clips``, ``controls`` or ``audio`` stream.
    """
    return fork_lab.rng_for(root_master_seed, fork_lab.DOMAIN_BATCH, str(index)).randint(
        CANDIDATE_MASTER_MIN, CANDIDATE_MASTER_MAX)


def candidate_master_seeds(root_master_seed: int, count: int) -> tuple:
    """The batch's candidate masters, in index order, **guaranteed distinct**.

    The collision scan looks only at lower indices, so the first N entries are identical whether
    this is called with N or with N+1 — the property that makes "show me three more" safe.
    """
    used = set()
    resolved = []
    for index in range(count):
        master = candidate_master_seed(root_master_seed, index)
        while master in used:
            # Deterministic forward step with a wrap, never a redraw: a retry loop would make the
            # result depend on how many times the RNG was asked rather than on the inputs.
            master = master + 1 if master < CANDIDATE_MASTER_MAX else CANDIDATE_MASTER_MIN
        used.add(master)
        resolved.append(master)
    return tuple(resolved)


# ---------------------------------------------------------------------------
# The declaration — everything a batch was generated from, canonically
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantBatchDeclaration:
    """One batch's complete, normalised, deepcopy-safe input record.

    Field order is explicit and stable, every member is a plain int, string or tuple of them, and
    both ordered collections follow their owning module's frozen field order
    (``presets.CREATIVE_CONTROL_FIELDS`` and ``variant_lab.AUDIO_CONTROL_FIELDS``) rather than
    widget-declaration order — so two declarations built from the same screen are equal by
    construction and never merely "probably" equal.

    This is a *declaration record*, in the same family as ``SourceSnapshot`` and ``PrepScanResult``:
    it records what was declared so a later action can check it still holds. It is emphatically
    **not** the persistent cached base snapshot C2 rejected — it is never read as a substitute for
    the live widgets at resolve time, only compared against them.
    """

    root_master_seed: int
    spread: int
    visual_randomized: tuple
    visual_ranges: tuple
    visual_base: tuple
    audio_randomized: tuple
    audio_ranges: tuple
    audio_base: tuple
    count: int

    def matches(self, other: Any) -> bool:
        """Structural equality, named so the call site reads as the gate it is."""
        return isinstance(other, VariantBatchDeclaration) and self == other

    def short_digest(self) -> str:
        """A compact display tag. **Diagnostics only** — never the staleness authority.

        Equality above is the authority. This exists so a status line can say *which* batch is on
        screen without printing nine fields, and it deliberately reuses the fork's one hashing
        idiom instead of introducing a serialisation contract of its own.
        """
        parts = (self.root_master_seed, self.spread, self.visual_randomized, self.visual_ranges,
                 self.visual_base, self.audio_randomized, self.audio_ranges, self.audio_base,
                 self.count)
        return f"{fork_lab.rng_for(self.root_master_seed, 'display', str(parts)).randrange(1 << 24):06x}"

    def visual_base_mapping(self) -> dict:
        return dict(zip(fork_presets.CREATIVE_CONTROL_FIELDS, self.visual_base))

    def audio_base_mapping(self) -> dict:
        return dict(zip(fork_lab.AUDIO_CONTROL_FIELDS, self.audio_base))


def _canonical_ranges(fields: Sequence, config: Any) -> tuple:
    """``((field, lo, hi), …)`` in the owning module's field order, from an already-normalised config."""
    rows = []
    for name in fields:
        control_range = config.range_for(name)
        rows.append((name, control_range.lo, control_range.hi))
    return tuple(rows)


def declaration_from(config: Any, base: Mapping, audio_config: Any,
                     audio_base: Mapping, count: Any) -> VariantBatchDeclaration:
    """Flatten the normalised C2/E2 config objects plus the live bases into one canonical record.

    The configs arrive already normalised (both normalise in ``__post_init__``), and the bases are
    put through the normaliser that **owns** each control — ``creative.normalize_control`` for the
    six visual ones, ``variant_lab.normalize_audio_base_value`` for the three audio ones, whose
    35/50/50 fallbacks are not interchangeable. That is what makes ``50`` from a slider and ``50.0``
    from the same slider one declaration rather than two.
    """
    visual_base = tuple(fork_creative.normalize_control(base.get(name))
                        for name in fork_presets.CREATIVE_CONTROL_FIELDS)
    audio_values = tuple(fork_lab.normalize_audio_base_value(name, audio_base.get(name))
                         for name in fork_lab.AUDIO_CONTROL_FIELDS)
    return VariantBatchDeclaration(
        root_master_seed=config.master_seed,
        spread=config.spread,
        visual_randomized=tuple(sorted(config.randomized)),
        visual_ranges=_canonical_ranges(fork_presets.CREATIVE_CONTROL_FIELDS, config),
        visual_base=visual_base,
        audio_randomized=tuple(sorted(audio_config.randomized)),
        audio_ranges=_canonical_ranges(fork_lab.AUDIO_CONTROL_FIELDS, audio_config),
        audio_base=audio_values,
        count=normalize_candidate_count(count),
    )


# ---------------------------------------------------------------------------
# Candidates and the batch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VariantCandidate:
    """One resolved candidate. Durable, immutable and deepcopy-safe by construction.

    Carries the two **recipes** and never the resolutions that produced them: a resolution holds a
    config, a config holds a ``MappingProxyType``, and a ``MappingProxyType`` cannot survive the
    deep copy a Gradio ``State`` performs. :func:`rehydrate` rebuilds a temporary resolution when
    one is needed.

    ``preset_label`` is a display read-out computed once through the existing
    ``presets.matching_preset``, so the table cannot develop a second opinion about which preset a
    tuple of six values is.
    """

    index: int
    master_seed: int
    creative_recipe: fork_recipe.CreativeRecipe
    audio_recipe: fork_lab.AudioRecipe
    preset_label: str

    def visual_values(self) -> tuple:
        return tuple(getattr(self.creative_recipe, name)
                     for name in fork_presets.CREATIVE_CONTROL_FIELDS)

    def audio_values(self) -> tuple:
        return tuple(getattr(self.audio_recipe, name)
                     for name in fork_lab.AUDIO_CONTROL_FIELDS)

    def label(self) -> str:
        """The selector label. Identity is :attr:`index`; this is only what the user reads."""
        return f"Candidate {self.index + 1} · master {self.master_seed}"


#: Column headers for :meth:`VariantBatch.table_text`, paired with their field widths. Ten
#: execution values plus the two provenance columns and the preset read-out — and deliberately
#: nothing else: no algorithm version, no spread per row, no cache or Stage metadata. The
#: comparison is selection UI, never render authority.
_COLUMNS = (
    ("  #", 3), ("  master", 8), ("    seed", 8),
    ("  dens", 6), (" micro", 6), ("   sem", 6), ("  enrg", 6), ("  motn", 6), ("  divr", 6),
    (" music", 6), ("  sfxA", 6), ("  sfxL", 6),
)


@dataclass(frozen=True)
class VariantBatch:
    """A declaration and the ordered candidates it produced. The whole of C3's session state."""

    declaration: VariantBatchDeclaration
    candidates: tuple

    def candidate(self, index: Any) -> Any:
        """The candidate at ``index``, or ``None``. Total: a selector may hand over anything."""
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 0 <= index < len(self.candidates):
            return self.candidates[index]
        return None

    def choices(self) -> list:
        """``(label, value)`` pairs where the value **is** the candidate index.

        The same idiom the E2 checkbox group uses: the returned value is the exact identity the
        handler keys on, never a display string a reworded label could silently re-map.
        """
        return [(candidate.label(), candidate.index) for candidate in self.candidates]

    def table_text(self) -> str:
        """The whole comparison read-out, and the **one** formatter for it.

        ``gui.py`` formats none of this, exactly as it formats none of ``AudioMixPlan`` /
        ``SmartMixPlan`` / ``VariantLabResolution.describe()``. One formatter per read-out.
        """
        declaration = self.declaration
        header = (f"Last generated batch · root master {declaration.root_master_seed} · "
                  f"Spread {declaration.spread} · {len(self.candidates)} candidates")
        lines = [header, "", "".join(name.rjust(width) for name, width in _COLUMNS) + "  preset"]
        for candidate in self.candidates:
            cells = ((candidate.index + 1, candidate.master_seed, candidate.creative_recipe.seed)
                     + candidate.visual_values() + candidate.audio_values())
            row = "".join(str(cell).rjust(width) for cell, (_, width) in zip(cells, _COLUMNS))
            lines.append(f"{row}  {candidate.preset_label}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _visual_config(declaration: VariantBatchDeclaration, master_seed: int):
    """An ephemeral :class:`variant_lab.VariantLabConfig` for one candidate. Never stored."""
    return fork_lab.VariantLabConfig(
        master_seed=master_seed,
        spread=declaration.spread,
        randomized=frozenset(declaration.visual_randomized),
        ranges={name: (lo, hi) for name, lo, hi in declaration.visual_ranges},
    )


def _audio_config(declaration: VariantBatchDeclaration):
    """An ephemeral :class:`variant_lab.AudioVariantConfig`. Never stored — it holds a proxy map."""
    return fork_lab.AudioVariantConfig(
        randomized=frozenset(declaration.audio_randomized),
        ranges={name: (lo, hi) for name, lo, hi in declaration.audio_ranges},
    )


def resolve_batch(declaration: VariantBatchDeclaration) -> VariantBatch:
    """Resolve every candidate from the **one** frozen declaration.

    Each candidate reads the *same* original base. Nothing is chained: candidate 2 is resolved from
    the declaration, never from candidate 1's recipe — which is exactly what looping today's
    Generate would do, because that handler writes its result back into the sliders it read.

    Pure: no ``SystemRandom``, no clock, no filesystem, no side effect. The same declaration
    reproduces the same ordered list exactly.
    """
    visual_base = declaration.visual_base_mapping()
    audio_base = declaration.audio_base_mapping()
    audio_config = _audio_config(declaration)
    candidates = []
    for index, master_seed in enumerate(
            candidate_master_seeds(declaration.root_master_seed, declaration.count)):
        resolution = fork_lab.resolve(_visual_config(declaration, master_seed), visual_base)
        audio_resolution = fork_lab.resolve_audio(
            master_seed, audio_config, declaration.spread, audio_base)
        values = tuple(getattr(resolution.recipe, name)
                       for name in fork_presets.CREATIVE_CONTROL_FIELDS)
        candidates.append(VariantCandidate(
            index=index,
            master_seed=master_seed,
            creative_recipe=resolution.recipe,
            audio_recipe=audio_resolution.recipe,
            preset_label=fork_presets.matching_preset(values),
        ))
    return VariantBatch(declaration=declaration, candidates=tuple(candidates))


def rehydrate(declaration: VariantBatchDeclaration, candidate: VariantCandidate) -> tuple:
    """Temporary ``(VariantLabResolution, AudioVariantResolution)`` for one stored candidate.

    Built so Apply can reuse the GUI's existing single projection helper verbatim — the same output
    tuple, the same explicit preset recomputation and the same two ``describe()`` formatters an
    ordinary Generate produces. Without this the GUI would need a second projection path, which is
    how two slightly different reports eventually ship.

    **These objects must never be returned into a Gradio ``State``**: they carry the configs, and
    the configs carry ``MappingProxyType``, which a deep copy cannot reproduce.
    """
    return (
        fork_lab.VariantLabResolution(
            config=_visual_config(declaration, candidate.master_seed),
            algorithm_version=fork_lab.VARIANT_LAB_ALGORITHM_VERSION,
            recipe=candidate.creative_recipe,
        ),
        fork_lab.AudioVariantResolution(
            master_seed=candidate.master_seed,
            algorithm_version=fork_lab.VARIANT_LAB_ALGORITHM_VERSION,
            spread=declaration.spread,
            config=_audio_config(declaration),
            recipe=candidate.audio_recipe,
        ),
    )


__all__ = [
    "CANDIDATE_COUNT_DEFAULT",
    "CANDIDATE_COUNT_MAX",
    "CANDIDATE_COUNT_MIN",
    "CANDIDATE_MASTER_MAX",
    "CANDIDATE_MASTER_MIN",
    "VariantBatch",
    "VariantBatchDeclaration",
    "VariantCandidate",
    "candidate_master_seed",
    "candidate_master_seeds",
    "declaration_from",
    "normalize_candidate_count",
    "rehydrate",
    "resolve_batch",
]
