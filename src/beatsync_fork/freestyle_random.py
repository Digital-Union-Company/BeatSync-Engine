#!/usr/bin/env python3
"""[FORK] Digital-Union: Random Freestyle V2 — seeded per-section style generation.

Freestyle V1 made a ``drop`` editable differently from the ``verse`` before it, but it asked the
user to assign one of four named presets to each of ten section types by hand. This module adds a
sixth choice::

    Base                 inherit the live global control            (V1, unchanged)
    Balanced / Cinematic / Dynamic / High Energy
                         the four named recipes                     (V1, unchanged)
    Random               GENERATE this section's five numbers        <- V2

``Random`` is a **seeded generator, not a model**. It runs no inference, loads no weights and asks
nothing of the local text model; it is deterministic arithmetic over a stable pseudorandom stream.
It is deliberately never described as AI anywhere in this package or the UI. A future AI-assisted
section director is a separate feature with a separate contract.

===============================================================================
Why this is a sibling module rather than more of ``freestyle.py``
===============================================================================

``freestyle.py`` is pinned hard: ``test_freestyle.py`` asserts its **exact** import set (``__future__``,
three ``dataclasses`` names, two ``typing`` names, ``creative`` and ``presets`` — nothing else) and
scans its source for a long list of forbidden substrings. A generator needs a pseudorandom stream,
so it needs an import that pin forbids.

Putting the generator here leaves ``freestyle.py`` byte-for-byte unchanged, so every guarantee its
suite already proves — the five-field record, the absent Micro Cuts and Variation Seed fields, the
canonical ordering, the one-way dependency on ``presets`` — keeps holding without being re-argued.
This module depends on ``freestyle`` and ``creative``; the dependency runs one way, and
``freestyle.py`` never learns that a generator exists.

===============================================================================
What a generated section is, and what it still may not touch
===============================================================================

A generated rule is an ordinary :class:`~beatsync_fork.freestyle.SectionOverride` carrying real
numbers for the **five** permitted controls. It is not a sixth preset and not a stored per-section
record: once resolved it is indistinguishable from a rule the user typed, which is exactly why
Stage 4 and Stage 6 need no new branch.

Micro Cuts and the Variation Seed remain global and remain unreachable — ``SectionOverride`` has no
field for either, so this module could not set them if it tried.

===============================================================================
Reproducibility
===============================================================================

Generation is a pure function of ``(seed, section_type)``. Two properties matter:

* **Stable across processes.** The stream is seeded with a *string*, and CPython's
  ``random.Random(str)`` derives its state through SHA-512 — not through ``hash()``, which is
  randomised per process by PYTHONHASHSEED. A tuple seed would have been exactly that trap.
* **Domain-separated.** Every section type draws from its own stream under a fixed prefix, so the
  planner's own ``_stable_rng`` sequence is untouched and adding a section type cannot shift the
  numbers another type already produced.

The same seed therefore yields the same five numbers for the same section type, in this run and in
any later one, and the Variation Seed printed in the success panel is all that is needed to
reproduce the edit.

Kept stdlib-only (CLAUDE.md's hard rule for this package): integers, strings and one
:class:`random.Random` instance.
"""

from __future__ import annotations

import random
from typing import Any, Mapping

from beatsync_fork import creative as fork_creative
from beatsync_fork import freestyle as fork_freestyle

#: The generated style. The one choice that is neither "inherit" nor a named recipe.
RANDOM_STYLE = "Random"

#: The V2 choice list: everything V1 offered, plus :data:`RANDOM_STYLE` last so the existing five
#: keep their positions and a saved screen cannot change meaning.
SECTION_STYLE_CHOICES = fork_freestyle.SECTION_STYLE_CHOICES + (RANDOM_STYLE,)

#: Fixed prefix for the pseudorandom stream. Domain separation, so this generator can never draw
#: from the same sequence as the planner's seeded selection.
_STREAM_DOMAIN = "beatsync-freestyle-random-v2"

#: Per-section inclusive ranges for the five permitted controls, as
#: ``section_type -> {control: (low, high)}``.
#:
#: These are **character bands, not noise**. A generated ``intro`` should still feel like an intro:
#: calmer cutting, less motion emphasis. A ``drop`` should still feel like a drop. Bands overlap
#: deliberately so two renders of one track differ audibly without either becoming wrong, and no
#: band spans the full 0-100 — an unconstrained draw is what makes "random" feel broken rather than
#: experimental.
#:
#: ``body`` is Stage 3's label for a track it could not divide at all, so it stays near neutral:
#: a whole-track "section" is the one case where a strong character would be applied to everything
#: at once.
_SECTION_BANDS: Mapping[str, Mapping[str, tuple[int, int]]] = {
    "intro":     {"cut_density": (20, 45), "energy_response": (25, 50), "motion_bias": (15, 40),
                  "semantic_emphasis": (40, 70), "source_diversity": (45, 75)},
    "hook":      {"cut_density": (55, 80), "energy_response": (55, 85), "motion_bias": (50, 80),
                  "semantic_emphasis": (40, 70), "source_diversity": (40, 70)},
    "outro":     {"cut_density": (15, 40), "energy_response": (20, 45), "motion_bias": (15, 40),
                  "semantic_emphasis": (40, 70), "source_diversity": (45, 75)},
    "finale":    {"cut_density": (60, 90), "energy_response": (60, 90), "motion_bias": (55, 85),
                  "semantic_emphasis": (35, 65), "source_diversity": (40, 70)},
    "drop":      {"cut_density": (65, 95), "energy_response": (65, 95), "motion_bias": (60, 90),
                  "semantic_emphasis": (30, 60), "source_diversity": (35, 65)},
    "chorus":    {"cut_density": (55, 85), "energy_response": (55, 85), "motion_bias": (50, 80),
                  "semantic_emphasis": (35, 65), "source_diversity": (40, 70)},
    "bridge":    {"cut_density": (35, 65), "energy_response": (35, 65), "motion_bias": (30, 60),
                  "semantic_emphasis": (45, 75), "source_diversity": (50, 80)},
    "breakdown": {"cut_density": (20, 50), "energy_response": (25, 55), "motion_bias": (20, 50),
                  "semantic_emphasis": (45, 75), "source_diversity": (50, 80)},
    "verse":     {"cut_density": (35, 60), "energy_response": (35, 60), "motion_bias": (30, 55),
                  "semantic_emphasis": (40, 70), "source_diversity": (45, 75)},
    "body":      {"cut_density": (40, 60), "energy_response": (40, 60), "motion_bias": (40, 60),
                  "semantic_emphasis": (40, 60), "source_diversity": (40, 60)},
}

#: Used when a section type has no band of its own. Never reached for a
#: :data:`~beatsync_fork.freestyle.SECTION_TYPES` entry — a test pins that every one has a band —
#: and present so a future Stage-3 label degrades to "moderate" instead of raising.
_DEFAULT_BAND: Mapping[str, tuple[int, int]] = _SECTION_BANDS["body"]


def is_random_style(style: Any) -> bool:
    """Is this the generated style? Total: any non-matching value, including ``None``, is ``False``."""
    return isinstance(style, str) and style == RANDOM_STYLE


def stream_for(seed: Any, section_type: str) -> random.Random:
    """The stable, domain-separated stream for one section type.

    Seeded with a **string** on purpose: ``random.Random`` hashes ``str`` through SHA-512, so the
    result is identical in every process, whereas a tuple or a bare ``str`` passed to ``hash()``
    would vary with PYTHONHASHSEED.
    """
    return random.Random(f"{_STREAM_DOMAIN}|{_seed_token(seed)}|{section_type}")


def _seed_token(seed: Any) -> str:
    """The seed as a stable string. Anything unusable reads as the legacy seed ``0``."""
    try:
        return str(int(seed))
    except (TypeError, ValueError):
        return "0"


def generate_override(section_type: str, seed: Any) -> "fork_freestyle.SectionOverride":
    """One generated rule for one section type: five real numbers, deterministic in ``seed``.

    Every value goes through ``creative.normalize_control``, so a generated control is exactly the
    same kind of number a slider produces — there is no second notion of a valid control value.
    """
    band = _SECTION_BANDS.get(section_type, _DEFAULT_BAND)
    rng = stream_for(seed, section_type)
    drawn = {
        name: fork_creative.normalize_control(rng.randint(*band.get(name, (40, 60))))
        for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS
    }
    return fork_freestyle.SectionOverride(**drawn)


def declaration_from_styles(enabled: Any, styles: Any,
                            seed: Any) -> "fork_freestyle.FreestyleDeclaration":
    """One declaration from ``{section_type: style}``, generating every :data:`RANDOM_STYLE` entry.

    The V2 counterpart of ``FreestyleDeclaration.from_styles``, and it exists because that method
    resolves each value through ``override_from_style``, which answers ``None`` for anything that is
    not a *preset name* — so a generated record handed to it would be silently dropped rather than
    applied. This builds the override list itself and lets the frozen record's own ``__post_init__``
    do the canonical ordering, so two declarations that say the same thing still compare equal.

    **Manual selections always win**, and that is one branch rather than a convention: a value that
    is not :data:`RANDOM_STYLE` goes through V1's ``override_from_style`` untouched, so ``Base`` and
    the four named recipes keep their V1 meaning exactly. Regenerating with a new seed cannot
    disturb a section the user fixed, because a fixed section never reaches the generator.

    Total: never raises. A non-mapping ``styles`` is no rules at all, exactly as in V1.
    """
    if not isinstance(styles, Mapping):
        return fork_freestyle.FreestyleDeclaration(enabled=enabled)
    collected: list[tuple[str, Any]] = []
    for section_type, style in styles.items():
        if is_random_style(style):
            collected.append((section_type, generate_override(section_type, seed)))
            continue
        override = fork_freestyle.override_from_style(style)
        if override is not None:
            collected.append((section_type, override))
    return fork_freestyle.FreestyleDeclaration(enabled=enabled, overrides=tuple(collected))


def describe_resolved(styles: Mapping[str, Any], seed: Any) -> str:
    """A readable preview of what the generated sections currently resolve to.

    Shown before rendering so "Random" is never a black box. Only generated sections are listed:
    a ``Base`` or named-preset section is the user's own choice and is already visible in its
    dropdown, and printing an inherited number here would be the exact staleness
    ``freestyle.summary_text`` refuses to introduce.
    """
    lines: list[str] = []
    for section_type in fork_freestyle.SECTION_TYPES:
        if not is_random_style(dict(styles).get(section_type)):
            continue
        override = generate_override(section_type, seed)
        values = ", ".join(
            f"{fork_freestyle.CONTROL_LABELS[name]} {getattr(override, name)}"
            for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS
        )
        lines.append(f"  {section_type}: {values}")
    if not lines:
        return "No sections are set to Random."
    return (f"Generated from Variation Seed {_seed_token(seed)} "
            f"({len(lines)} section type(s)):\n" + "\n".join(lines))


__all__ = [
    "RANDOM_STYLE",
    "SECTION_STYLE_CHOICES",
    "is_random_style",
    "stream_for",
    "generate_override",
    "declaration_from_styles",
    "describe_resolved",
]
