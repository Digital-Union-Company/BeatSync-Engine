#!/usr/bin/env python3
"""[FORK] Digital-Union: Freestyle V1 — per-**section** creative rules.

Every creative control this app has ever had is global for the whole track: one Cut Density, one
Motion Bias, one of everything, first beat to last. Freestyle is the first feature that lets a
``drop`` be edited differently from the ``verse`` before it::

    THE SIX SLIDERS   = the global base                      (unchanged execution truth)
    A FREESTYLE RULE  = a per-SECTION delta on five of them   (a render request, never identity)
    STILL EXACTLY ONE = the Variation Seed, and Micro Cuts

===============================================================================
Five fields, and the two that are absent by construction
===============================================================================

:data:`FREESTYLE_CONTROL_FIELDS` is **derived** from ``presets.CREATIVE_CONTROL_FIELDS`` minus
``micro_cuts`` rather than restated, so a seventh global control cannot appear in one place and not
the other. What is left is Cut Density, Semantic Emphasis, Energy Response, Motion Bias and Source
Diversity.

**Micro Cuts stays global** because it governs a rare half-beat accent layer whose whole contract is
that it cannot become flicker; a per-section ratio multiplied by a per-section density is exactly
how that contract would be lost by arithmetic nobody reviewed. **The Variation Seed stays global**
so the one number a user writes down keeps describing the whole render.

Neither has a field on :class:`SectionOverride`. That is the enforcement: a rule cannot touch them
because there is nowhere to put the value — structural, not careful.

===============================================================================
Sparse, and keyed by section TYPE
===============================================================================

A rule sets *some* of the five fields. An unset field inherits the live global value at render
time, which is why the summary prints ``Base`` instead of a number: the global controls have five
legitimate writers (the preset selector, Variant Lab Generate / New / Apply, and Director Apply), so
a number copied into this read-out would go stale the moment any of them fired.

Rules are keyed by Stage-3 section **type**, so every instance of a repeated type shares one rule —
:data:`REPEATED_SECTION_POLICY`. There is deliberately no per-instance editor and no timeline: a
track's section boundaries are detected from the music *during* the render, so a position-keyed rule
would describe a layout the user has never seen.

:data:`SECTION_TYPES` is the one place in this package that restates an upstream vocabulary
(``auto_mode/stage3_sections.py``'s ``classify_section`` plus its degenerate ``body`` fallback). It
is restated rather than imported because importing it would pull numpy and the whole upstream
runtime into a stdlib-only module, and a test pins the tuple against the real classifier so the copy
cannot drift.

===============================================================================
What this module does not do
===============================================================================

It renders nothing, analyses nothing and caches nothing. No value here reaches a Qwen request, the
Stage-5 prompt, a persisted record or any cache signature: a rule is *interpretation*, and Stage 5
owns intrinsic media truth. Changing a rule re-plans and never re-analyses.

It also knows nothing about the Variant Lab or the AI Director, and they know nothing about it. Both
of those produce one global seven-value recipe; Freestyle declares a delta on top of whatever the
sliders currently hold. A rule is not a recipe, a recipe is not a rule, and neither is an input to
the other.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): this is a small immutable record
plus arithmetic over integers, so it is testable on a bare interpreter with no numpy, no Gradio, no
CUDA and no FFmpeg. It depends on :mod:`beatsync_fork.creative` and :mod:`beatsync_fork.presets`
and nothing else — and the dependency runs one way only, so ``presets.py`` never learns that a
section mechanism exists.

Full contract: ``.claude/rules/freestyle.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

from beatsync_fork import creative as fork_creative
from beatsync_fork import presets as fork_presets

#: The five controls a section rule may set, **derived** from the six global ones by removing
#: ``micro_cuts``. Derived rather than restated so the two can never disagree, and filtered by name
#: rather than by index so a reordering of the global tuple cannot silently drop a different
#: control.
FREESTYLE_CONTROL_FIELDS = tuple(
    name for name in fork_presets.CREATIVE_CONTROL_FIELDS if name != "micro_cuts"
)

#: The two global controls a rule may deliberately never reach. Listed for the tests and the
#: read-out; the real enforcement is that :class:`SectionOverride` has no field for either.
GLOBAL_ONLY_CONTROL_FIELDS = ("micro_cuts", "seed")

#: Human labels, matching ``CreativeProfile.describe()`` word for word so one control is never
#: called two things in two panels.
CONTROL_LABELS: Mapping[str, str] = {
    "cut_density": "Cut Density",
    "semantic_emphasis": "Semantic Emphasis",
    "energy_response": "Energy Response",
    "motion_bias": "Motion Bias",
    "source_diversity": "Source Diversity",
}

#: Every section type ``stage3_sections.classify_section`` can return, plus ``body`` — the label
#: Stage 3 uses for a track it could not divide at all. Ordered as the classifier decides them
#: (position first, then energy), which is also the order the GUI lays the dropdowns out and the
#: order a render request threads them in. One definition; a test pins it against the real
#: classifier.
SECTION_TYPES = (
    "intro",
    "hook",
    "outro",
    "finale",
    "drop",
    "chorus",
    "bridge",
    "breakdown",
    "verse",
    "body",
)

#: Every instance of a repeated section type shares one rule. A statement about the *rule*, not
#: about the outcome: two drops get the identical settings and then each selects cuts from its own
#: musical content, which is the whole point of a beat-anchored selector.
REPEATED_SECTION_POLICY = "ALL_INSTANCES_SHARE_RULE"

#: The default section style, and the only one that is not a preset name. It means "inherit the live
#: global value at render time" — not "50", and not a snapshot of the sliders.
BASE_STYLE = "Base"

#: The section-style choices: :data:`BASE_STYLE` then the four named recipes.
#:
#: ``presets.CUSTOM_PRESET`` is **excluded**, and that is not tidiness. ``Custom`` is a *state*
#: meaning "the six live values match no named recipe", so it has no values to project into a rule.
#: ``Base`` is what takes its place, and it says something ``Custom`` cannot: inherit, whatever the
#: sliders later become.
SECTION_STYLE_CHOICES = (BASE_STYLE,) + tuple(fork_presets.PRESETS)


def _override_value(value: Any) -> int | None:
    """One override field: ``None`` for "inherit", otherwise a real 0..100 control.

    ``None`` is preserved rather than normalised, which is the one place this layer must *not*
    reuse ``normalize_control``'s notion of neutral: that function answers 50 for a missing value,
    and 50 is a perfectly ordinary density a user may have deliberately ruled. Here the absence of a
    value means "do not touch this control", so it has to survive as absence.

    Everything else goes through ``creative.normalize_control``, so what a control value *is* stays
    defined in exactly one place.
    """
    if value is None:
        return None
    return fork_creative.normalize_control(value)


@dataclass(frozen=True)
class SectionOverride:
    """A sparse delta on five of the six global controls, for one section type.

    Frozen, and built from plain ``int``/``None`` only, so a declaration stays deepcopy-safe and
    carries no mapping proxy, no path, no media and no cache handle onto the shared bus.

    **There is no ``micro_cuts`` field and no ``seed`` field.** That absence is the enforcement of
    "Micro Cuts and the Variation Seed stay global", and it is the reason this record is written out
    field by field rather than generated from :data:`FREESTYLE_CONTROL_FIELDS`: a generated record
    would make "which controls can a rule reach" a runtime question instead of something a reviewer
    can read.
    """

    cut_density: int | None = None
    semantic_emphasis: int | None = None
    energy_response: int | None = None
    motion_bias: int | None = None
    source_diversity: int | None = None

    def __post_init__(self) -> None:
        for field in fields(self):
            object.__setattr__(self, field.name, _override_value(getattr(self, field.name)))

    def is_empty(self) -> bool:
        """True when this rule sets nothing, i.e. it is indistinguishable from having no rule.

        An empty rule is dropped by :class:`FreestyleDeclaration` rather than stored, so "has
        rules" and "would change something" are the same question.
        """
        return all(getattr(self, name) is None for name in FREESTYLE_CONTROL_FIELDS)

    def set_values(self) -> tuple[tuple[str, int], ...]:
        """The fields this rule actually sets, in :data:`FREESTYLE_CONTROL_FIELDS` order."""
        return tuple(
            (name, getattr(self, name))
            for name in FREESTYLE_CONTROL_FIELDS
            if getattr(self, name) is not None
        )

    def describe(self) -> str:
        """The numbers this rule sets, and only those. Empty string for an empty rule."""
        return " · ".join(
            f"{CONTROL_LABELS[name]} {value}" for name, value in self.set_values()
        )

    def summarize(self) -> str:
        """All five fields, with every inherited one shown as ``Base`` rather than as a number.

        The read-out form. :meth:`describe` is the execution-truth form — it prints only what the
        rule decided, because that is what a user needs in order to reproduce a render.
        """
        return " · ".join(
            f"{CONTROL_LABELS[name]} "
            f"{BASE_STYLE if getattr(self, name) is None else getattr(self, name)}"
            for name in FREESTYLE_CONTROL_FIELDS
        )


def override_from_style(style: Any) -> SectionOverride | None:
    """Project one named preset recipe into a :class:`SectionOverride`, or ``None``.

    ``None`` — meaning "no rule for this section" — is the answer for :data:`BASE_STYLE`, for
    ``Custom``, for an unknown name and for anything that is not a string at all, so a caller needs
    one branch. Delegated to ``presets.resolve_preset``, which already answers ``None`` for all four
    of those cases.

    **A rule stores the numbers, never the name.** The label does not survive into the declaration,
    so a later retune of the preset table cannot silently change what a saved rule meant — the same
    reproducibility argument that keeps the preset name out of ``CreativeProfile``.

    The projection takes **five** of the recipe's six values: ``micro_cuts`` is dropped on the floor
    here, which is what makes "a style named Cinematic keeps your Micro Cuts exactly as you set it"
    true rather than merely intended.
    """
    recipe = fork_presets.resolve_preset(style)
    if recipe is None:
        return None
    return SectionOverride(
        **{name: recipe[name] for name in FREESTYLE_CONTROL_FIELDS if name in recipe}
    )


def _normalize_overrides(value: Any) -> tuple[tuple[str, SectionOverride], ...]:
    """``((section_type, SectionOverride), ...)`` in :data:`SECTION_TYPES` order.

    Total, and canonicalising: an unknown section type, an empty rule, a malformed pair and a
    non-sequence are all simply dropped. Ordering by :data:`SECTION_TYPES` rather than by insertion
    is what makes two declarations built by different routes — the GUI tuple, a preset projection, a
    direct caller — compare equal when they say the same thing.
    """
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, (tuple, list)):
        items = list(value)
    else:
        return ()

    collected: dict[str, SectionOverride] = {}
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        section_type, override = item
        if section_type not in SECTION_TYPES:
            continue
        if not isinstance(override, SectionOverride) or override.is_empty():
            continue
        collected[section_type] = override

    return tuple(
        (section_type, collected[section_type])
        for section_type in SECTION_TYPES
        if section_type in collected
    )


@dataclass(frozen=True)
class FreestyleDeclaration:
    """The section rules for one render: a switch, plus a sparse set of per-type rules.

    Frozen and built from plain bools, strings, ints and tuples, so it is deepcopy-safe, hashable,
    and safe to put on ``beat_info`` as ephemeral run-scoped intent.

    **The rules are retained while the switch is off.** A user who unticks the checkbox to compare
    one render against another must not lose what they declared, so "has rules" is deliberately not
    permission to use them — :meth:`is_active` is, and both Stage 4 and Stage 6 gate on that one
    method rather than each deciding for itself whether the feature is on.
    """

    enabled: bool = False
    overrides: tuple[tuple[str, SectionOverride], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", bool(self.enabled))
        object.__setattr__(self, "overrides", _normalize_overrides(self.overrides))

    # -- construction -------------------------------------------------------

    @classmethod
    def from_styles(cls, enabled: Any, styles: Any) -> "FreestyleDeclaration":
        """One declaration from ``{section_type: style_label}``. Total: never raises.

        This is the GUI's entry point, and it is where the preset labels stop existing. Anything
        that is not a mapping is no rules at all, and within the mapping an unknown section type, an
        unknown style, :data:`BASE_STYLE` and ``Custom`` all contribute nothing.
        """
        if not isinstance(styles, Mapping):
            return cls(enabled=enabled)
        collected = []
        for section_type, style in styles.items():
            override = override_from_style(style)
            if override is not None:
                collected.append((section_type, override))
        return cls(enabled=enabled, overrides=tuple(collected))

    # -- queries ------------------------------------------------------------

    def is_active(self) -> bool:
        """True when this render must take a per-section path at all.

        The single gate. Stage 4 and Stage 6 both ask this and nothing else, which is what stops the
        two stages disagreeing about whether the feature is on — a real failure mode: an
        ``isinstance`` check in one of them let Freestyle-off change the cut timeline while leaving
        scoring global.
        """
        return bool(self.enabled) and bool(self.overrides)

    def rule_for(self, section_type: Any) -> SectionOverride | None:
        """This section type's rule, or ``None``. ``None`` also for an inactive declaration."""
        if not self.is_active():
            return None
        for declared_type, override in self.overrides:
            if declared_type == section_type:
                return override
        return None

    def ruled_types(self) -> tuple[str, ...]:
        """The section types carrying a rule, in :data:`SECTION_TYPES` order."""
        return tuple(section_type for section_type, _override in self.overrides)

    def effective_cut_densities(self, base_density: Any) -> dict[str, int]:
        """``section_type -> effective Cut Density``, for the ruled types only.

        Stage 4's entry point, and the only Freestyle query that touches the cut timeline. A ruled
        type whose rule sets no density resolves to ``base_density``, so Stage 4's "is every section
        the same density?" short-circuit is decided on resolved **values** rather than on whether
        the checkbox is ticked — which is what makes "on with every rule landing on the base" as
        byte-identical as "off".

        Empty for an inactive declaration. Unruled types are deliberately absent rather than mapped
        to the base: Stage 4 fills them in from its own base, and listing them here would invent an
        authority this record does not have.
        """
        if not self.is_active():
            return {}
        base = fork_creative.normalize_control(base_density)
        return {
            section_type: (base if override.cut_density is None else override.cut_density)
            for section_type, override in self.overrides
        }

    # -- reporting ----------------------------------------------------------

    def describe(self) -> str:
        """One compact line of execution truth: the numbers, never the preset labels.

        Deliberately **unlabelled** — no leading ``Freestyle:``. Its two callers already supply
        their own framing (``ui_content``'s success line and Stage 4's console line, which prints
        it in parentheses after its own counts), and a label baked in here read as
        ``Freestyle: ... (Freestyle: ...)`` in one of them. Same division of labour as
        ``CreativeProfile.describe()``, whose ``Creative variation:`` label also lives in
        ``ui_content``.

        ``off`` for an inactive declaration, so a caller that prints this unconditionally still
        reads honestly. The labels the user picked were UI input and are not stored — see
        :func:`override_from_style`.
        """
        if not self.is_active():
            return "off"
        return "; ".join(
            f"{section_type} → {override.describe()}"
            for section_type, override in self.overrides
        )


#: Shown while the checkbox is off. States the consequence rather than the state, because "off" is
#: the thing a user can already see.
SUMMARY_DISABLED = (
    "Freestyle is off — every section uses your global Creative Controls above.\n"
    "Section rules below are remembered, so switching Freestyle on restores them."
)

#: Shown while the checkbox is on but nothing is ruled. Says the render is unchanged, because it is:
#: Stage 4's short-circuit and Stage 6's gate both resolve this to the global path.
SUMMARY_NO_RULES = (
    "Freestyle is on, but every section is set to Base — this render is exactly what it\n"
    "would be with Freestyle off. Pick a style for a section type to change that."
)


def summary_text(declaration: Any) -> str:
    """The read-only Freestyle summary. One formatter, so the read-out cannot fork.

    Deliberately shows ``Base`` for every inherited field and **never prints an inherited number**.
    The global controls have five legitimate writers, so a number copied in here could not be kept
    honest; ``Base`` stays true whatever the sliders later become. This function takes no global
    control as an argument, which makes that guarantee structural rather than a promise — there is
    no global value in scope to print.

    Total: anything that is not a :class:`FreestyleDeclaration` reads as the default one.
    """
    if not isinstance(declaration, FreestyleDeclaration):
        declaration = FreestyleDeclaration()
    if not declaration.enabled:
        return SUMMARY_DISABLED
    if not declaration.overrides:
        return SUMMARY_NO_RULES

    ruled = declaration.ruled_types()
    width = max(len(section_type) for section_type in ruled)
    lines = [
        "Freestyle is on. Micro Cuts and the Variation Seed stay global for the whole render.",
        "Base means the section inherits your global Creative Control at render time.",
        "",
    ]
    lines.extend(
        f"{section_type:<{width}}  {override.summarize()}"
        for section_type, override in declaration.overrides
    )
    inherited = [section_type for section_type in SECTION_TYPES if section_type not in ruled]
    if inherited:
        lines.append("")
        lines.append("Base for every control: " + ", ".join(inherited))
    return "\n".join(lines)


def effective_profile(base: fork_creative.CreativeProfile, section_type: Any,
                      declaration: Any) -> fork_creative.CreativeProfile:
    """The profile one section actually plans under: ``base``, with its rule composed on top.

    Returns ``base`` **itself** — the same object, not an equal copy — whenever nothing changes: an
    inactive declaration, a section with no rule, an unknown section type, and a rule whose every
    value already equals the base's. Identity matters rather than merely being tidy: Stage 6's
    caller tests ``effective is profile_settings`` to reuse the global scoring column, so an
    equal-but-separate copy would quietly add a table column per section and undo L1A's whole point.

    The Variation Seed and Micro Cuts are carried over untouched, because :class:`SectionOverride`
    has no field for either.

    Total: a foreign or absent declaration yields ``base``, so a render can never fail here.
    """
    if not isinstance(declaration, FreestyleDeclaration):
        return base
    override = declaration.rule_for(section_type)
    if override is None:
        return base
    changes = {
        name: value
        for name, value in override.set_values()
        if getattr(base, name) != value
    }
    if not changes:
        return base
    return replace(base, **changes)


__all__ = [
    "BASE_STYLE",
    "CONTROL_LABELS",
    "FREESTYLE_CONTROL_FIELDS",
    "FreestyleDeclaration",
    "GLOBAL_ONLY_CONTROL_FIELDS",
    "REPEATED_SECTION_POLICY",
    "SECTION_STYLE_CHOICES",
    "SECTION_TYPES",
    "SUMMARY_DISABLED",
    "SUMMARY_NO_RULES",
    "SectionOverride",
    "effective_profile",
    "override_from_style",
    "summary_text",
]
