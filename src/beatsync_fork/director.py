#!/usr/bin/env python3
"""[FORK] Digital-Union: AI Director V2 — the pure semantic-intent boundary.

The Director turns one sentence of editing intent into **one visual**
:class:`~beatsync_fork.creative_recipe.CreativeRecipe`, which the user then reviews and explicitly
applies::

    natural-language intent
        -> local text-only Qwen3-4B generation      (gui.py performs the subprocess)
        -> strictly validated SEMANTIC INTENT       (this module)
        -> deterministic semantic -> six-control BASE mapping   (this module)
        -> optional narrow prepared-media Source Diversity attenuation  (director_media.py)
        -> FINAL six controls
        -> GUI mints the Variation Seed             (variation.random_seed, in gui.py)
        -> CreativeRecipe.from_mapping              (the existing, unweakened trust boundary)
        -> DirectorProposal                         (this module)
        -> the user presses Apply Proposal
        -> the existing Variation Seed + six Creative Controls

**The Director proposes; it never renders, and it is not a second execution path.** It is a *second
producer* of the artifact Variant Lab already produces.

===============================================================================
V2: the model speaks USER language, not BeatSync's control names
===============================================================================

V1 asked the model for the six internal controls directly — dense in V1, and later measured in
sparse and direction+strength variants. All three made the language-understanding task include
"which way does the ``energy_response`` slider move?", and all three failed the same way: the
natural sentence *"Keep scene choice relatively even across sections."* was read as neutral by the
2B model, by a 4B model and by an 8B model, across three different output contracts — six
measurements, one answer.

V2 removes the implementation vocabulary from the model's job entirely. The model classifies the
user's meaning on six ordinary **editing dimensions**, each with its own meaningful direction pair,
and deterministic code alone knows that ``section_reactivity: steadier`` means
``energy_response < 50``. With that one change the same 4B model read that sentence correctly, and
the frozen measurement set came out at 6/6 on the energy cluster, 14/14 concepts, 10/10
media-control concepts, 0 wrong-direction answers, 12/12 on a held-out matrix it had never seen,
2.50 % non-target emission and byte-identical repeats.

So the separation is load-bearing, not cosmetic:

* :data:`SEMANTIC_AXES` is the **model-facing** vocabulary. None of the six execution control names
  may appear in :func:`system_prompt` or :func:`model_schema_json`, and an import-time assertion
  plus a permanent test both enforce that.
* :func:`resolve_semantic_intent` is the **only** place the two vocabularies meet.

===============================================================================
Strength, and why there is no neutral direction
===============================================================================

Each reported dimension carries a ``direction`` and a ``strength`` of ``1..100``. There is
deliberately **no** ``0`` and **no** ``neutral`` direction: both would be indistinguishable from
simply omitting the dimension, and the sparse experiment measured exactly that failure — a model
given a neutral option emitted no-op entries (``{"source_diversity": 50}``) instead of omitting,
and a model asked for absolute values collapsed every downward request onto 50. Omission is the
only way to say "leave this alone", and it means *no expressed preference*.

===============================================================================
Evidence basis: did the user ask, or does it merely suit the style?
===============================================================================

Each reported dimension also carries a ``basis`` of ``requested`` or ``associated``. It answers a
different question from ``strength``:

* ``strength`` — *how much* does the user want this direction?
* ``basis`` — did the user **express** this preference at all?

``basis`` is therefore **not** a confidence number, and it is **not** a seventh dimension: it is
metadata about one existing request. All four combinations are legal and meaningful, which is the
point — a faintly-put real request is ``requested`` with a small strength, and a dimension the model
is sure would suit the described style is ``associated`` however strong that feeling is.

The field exists because five successive prompt-only attempts could not stop the model inferring
``source_variety`` from mood alone. Mood does not establish a preference about source breadth: a
calm edit may use every source and a frantic one may reuse a few. Asking the model to suppress the
thought failed; asking it the separate question *"was this requested?"* did not.

So the product policy is per axis, and it lives in :func:`resolve_semantic_intent` alone:
:data:`STYLE_INFERABLE_ASSOCIATED_AXES` may be moved by an ``associated`` reading because ordinary
filmmaking language is *intended* to imply them, while on :data:`REQUESTED_ONLY_AXES` an
``associated`` reading resolves exactly as an omitted axis — the control stays at 50, at any
strength. The raw intent is never filtered in place: it is provenance, and
:meth:`SemanticIntent.actionable_requests` is how a caller asks what survived.

**Known limitation, measured and accepted for v0.1.0.** RC0-P3 measured two of twenty-four very
faintly phrased explicit source-breadth requests as ``associated`` — direction correct, basis
conservative — so they leave Source Diversity neutral instead of nudging it. That is a false
negative, i.e. a no-op, and it was preferred to the alternative measured in RC0-P4, where a wording
that fixed those two also made style-only text read as a request and changed execution without one.
See ``.claude/rules/director.md``; do not claim 100 % natural-language breadth recall.

===============================================================================
What V2 still deliberately does not do
===============================================================================

V2 is **visual only**: the six creative controls in ``presets.CREATIVE_CONTROL_FIELDS`` order and
nothing else. It does not generate or modify the three audio levels, voice clips, voice timing,
``avoid_drops``, the SFX folder or roles, the source folder or files, FPS, the encoder, the output
filename, the source confirmation or the Media Library Preparation state.

**The model never chooses the Variation Seed.** The schema has no ``seed`` property and — because
``additionalProperties`` is ``false`` — an emitted one is a grammar violation; a ``seed`` key that
somehow arrives anyway is rejected by :func:`parse_semantic_intent`. The seed is minted in the GUI
by the existing ``variation.random_seed()`` *after* a valid intent has resolved to valid controls.

**The model remains media-blind.** It receives the instruction, the system prompt and the schema —
no frames, no filenames, no Stage-5 records, no ``beat_info``, no sections, no tempo, no music
features, no current source state and **no media summary**. V2's content awareness is entirely a
local deterministic step that happens *after* the model has answered; see ``director_media.py``.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): a schema, two prompts, a parser,
a deterministic mapping, two frozen records and the text they format. No subprocess, no model path,
no filesystem, no clock and no randomness live here — this module decides, and ``gui.py`` performs
every side effect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from beatsync_fork import creative as fork_creative
from beatsync_fork import director_media as fork_media
from beatsync_fork import presets as fork_presets
from beatsync_fork.creative_recipe import CreativeRecipe

#: The exact execution controls a resolved intent produces, **derived** from the one creative-control
#: registry rather than restated. A second hard-coded six-field tuple is how the Director and the
#: sliders would eventually disagree about what "the six controls" are.
DIRECTOR_CONTROL_FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS

#: The optional, non-execution field. Named once so the parser, the schema and the display text
#: cannot drift onto two spellings.
EXPLANATION_KEY = "explanation"

#: The model-facing container for the reported dimensions.
INTENT_KEY = "intent"

#: Bound on the explanation, in characters. Declared in the model schema *and* — load-bearingly —
#: enforced again by :func:`normalize_explanation` on the way to the screen.
#:
#: **The schema half is advisory on the installed build, measured rather than assumed.**
#: `maxLength` is not compiled into llama.cpp's JSON-schema grammar there: generations at declared
#: limits of 60, 160 and 280 produced identical ~460-character strings. So this constant is a
#: request in the schema and a guarantee only in the normaliser.
EXPLANATION_MAX_CHARS = 280

#: Bound on the instruction accepted from the textbox. Not a safety claim — a prompt long enough to
#: exhaust the context window produces a failure that looks like a model fault.
INSTRUCTION_MAX_CHARS = 2000

#: The inclusive strength range. ``0`` is excluded on purpose: see the module docstring.
STRENGTH_MIN = 1
STRENGTH_MAX = 100

#: The per-dimension evidence field. ``basis`` is **metadata about one reported dimension**, never a
#: seventh dimension and never a confidence number — see the "Evidence basis" section of the module
#: docstring for why those two readings are both wrong.
BASIS_KEY = "basis"

#: The user's own instruction expressed this editing preference, literally or by clear implication.
BASIS_REQUESTED = "requested"

#: The model supplied the dimension because it seems stylistically compatible with what the user
#: asked for — the user did not actually request it.
BASIS_ASSOCIATED = "associated"

#: The only two legal values, in schema order. Immutable: a third value would be a contract change.
BASIS_VALUES: tuple[str, str] = (BASIS_REQUESTED, BASIS_ASSOCIATED)

# ---------------------------------------------------------------------------
# Generation settings
# ---------------------------------------------------------------------------
#
# Hard-coded rather than environment variables, for the reason
# `.claude/rules/stage5-worker.md` records for the recovery constants: a knob that changes a
# result belongs under contract, not in an env var nobody audits. These are *not* Stage-5
# settings, they reach no cache key, and they re-key nothing — the Director has no cache at all.

#: Context window for the one-shot invocation.
CONTEXT_TOKENS = 2048

#: Generation budget. Deliberate headroom, because exhausting the budget mid-string is exactly how
#: the JSON fails to close.
MAX_NEW_TOKENS = 320

#: Greedy decoding, matching the repository's existing Qwen convention. The same instruction
#: therefore proposes the same intent — which is the honest product, since the Director reads
#: *words*. The Variation Seed is freshly minted every press regardless.
TEMPERATURE = 0.0
TOP_K = 1

#: The hard wall on one Director generation. Measured median end-to-end cost on the selected 4B
#: model is ~3.5 s, so this is deliberately generous (cold model load every time, because the
#: process exits).
TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# The six model-facing semantic axes
# ---------------------------------------------------------------------------
#
# Each entry is (negative_direction, positive_direction, execution_control, meaning). "negative"
# means the resolved control lands BELOW 50, "positive" above. The meanings are written in ordinary
# editing language on purpose: reusing the V1 control descriptions is impossible here, because those
# strings literally name `cut_density` / `energy_response` / etc., and leaking the implementation
# vocabulary is the one thing V2 exists to prevent.

SEMANTIC_AXES: dict[str, tuple[str, str, str, str]] = {
    "cut_pacing": (
        "sparser", "denser", "cut_density",
        "how often the edit changes shot. \"sparser\" means fewer changes, holding each shot "
        "longer; \"denser\" means changing shot more often."),
    "impact_accents": (
        "fewer", "more", "micro_cuts",
        "the brief extra accent cuts placed on the biggest musical impacts. \"fewer\" means less "
        "of that accenting; \"more\" means stronger accenting on the big hits."),
    "scene_reading": (
        "visual", "semantic", "semantic_emphasis",
        "what makes a moment worth using. \"visual\" means judging moments mainly by measurable "
        "picture qualities such as movement, sharpness, colour and exposure; \"semantic\" means "
        "judging them mainly by what the scene is understood to contain."),
    "section_reactivity": (
        "steadier", "responsive", "energy_response",
        "how much the choice of imagery follows the song's structure. \"steadier\" means keeping "
        "the character of the scene selection fairly consistent as the music moves between drops, "
        "builds, quiet passages and other section types; \"responsive\" means changing the scene "
        "preference strongly with each section's intensity."),
    "motion_preference": (
        "calmer", "dynamic", "motion_bias",
        "how much movement the chosen moments should have. \"calmer\" prefers steadier, "
        "less-moving moments; \"dynamic\" prefers stronger movement."),
    "source_variety": (
        "reuse", "diverse", "source_diversity",
        "how widely the edit draws on the available source videos. \"reuse\" concentrates on a "
        "smaller recurring set; \"diverse\" spreads usage across more of them."),
}

#: Axis -> execution control, and the inverse. The only bridge between the two vocabularies.
AXIS_TO_CONTROL = {axis: spec[2] for axis, spec in SEMANTIC_AXES.items()}
CONTROL_TO_AXIS = {control: axis for axis, control in AXIS_TO_CONTROL.items()}

#: The six internal names that must never reach the model.
_INTERNAL_CONTROL_NAMES = tuple(DIRECTOR_CONTROL_FIELDS)

# ---------------------------------------------------------------------------
# The actionability policy: which `basis` values may move an execution control
# ---------------------------------------------------------------------------
#
# This policy lives HERE, in the pure boundary, and nowhere else. `gui.py`, the invocation code, the
# media adapter and the Apply handler must all receive the same product semantics, which is only
# true if `resolve_semantic_intent` owns the decision — see RC0-P3/P4 and `.claude/rules/director.md`.

#: Dimensions ordinary filmmaking language is *intentionally allowed* to imply. A style description
#: may move these, so both bases are actionable. This is the behaviour the short-style measurement
#: set depends on: "Use a cinematic, patient, emotional style." resolves to a real three-axis
#: proposal precisely because these three accept `associated`.
STYLE_INFERABLE_ASSOCIATED_AXES = frozenset({
    "cut_pacing", "scene_reading", "motion_preference"})

#: Dimensions that require the user to have actually asked. An `associated` value on one of these
#: resolves **exactly as an omitted axis** — the control stays at 50 — regardless of strength.
#:
#: `source_variety` is here because mood does not establish a preference about source breadth: a
#: calm edit may use every source and a frantic one may reuse a few, so inferring it from style
#: changes execution without a request. `impact_accents` and `section_reactivity` are here to
#: preserve a measured property rather than to add a new restriction: RC0-P1 and RC0-P3 both
#: recorded zero unsupported associated emissions for them, and generic style does not by itself
#: ask for extra impact accenting or stronger section-following.
REQUESTED_ONLY_AXES = frozenset({
    "impact_accents", "section_reactivity", "source_variety"})

# The policy must partition the six axes exactly: an axis added without a decision would otherwise
# default to whichever branch the resolver happened to be written with.
assert STYLE_INFERABLE_ASSOCIATED_AXES.isdisjoint(REQUESTED_ONLY_AXES)
assert STYLE_INFERABLE_ASSOCIATED_AXES | REQUESTED_ONLY_AXES == set(SEMANTIC_AXES)


def actionable_bases(axis: str) -> frozenset[str]:
    """The ``basis`` values that may move ``axis``'s execution control. Total over the six axes."""
    if axis not in SEMANTIC_AXES:
        raise KeyError(axis)
    return frozenset(BASIS_VALUES) if axis in STYLE_INFERABLE_ASSOCIATED_AXES \
        else frozenset({BASIS_REQUESTED})


#: User-facing label per semantic axis, for the read-out. Written from the **model-facing** axis
#: vocabulary and never derived from ``AXIS_TO_CONTROL`` — a label built from an execution control
#: name is how ``source_diversity`` would eventually reach the screen.
AXIS_DISPLAY_LABELS: dict[str, str] = {
    "cut_pacing": "Cut pacing",
    "impact_accents": "Impact accents",
    "scene_reading": "Scene reading",
    "section_reactivity": "Section reactivity",
    "motion_preference": "Motion preference",
    "source_variety": "Source variety",
}

# Total over the six axes, and free of execution vocabulary — both checked rather than trusted.
assert set(AXIS_DISPLAY_LABELS) == set(SEMANTIC_AXES)
for _label in AXIS_DISPLAY_LABELS.values():
    assert not any(c in _label.lower() for c in _INTERNAL_CONTROL_NAMES), _label

# Structural guards rather than comments: the axes and the control registry are the same six, and
# every axis has a distinct direction pair. A control added to `presets.CREATIVE_CONTROL_FIELDS`
# without a semantic axis fails at import time instead of being silently left for the model to guess.
assert len(SEMANTIC_AXES) == len(DIRECTOR_CONTROL_FIELDS) == 6
assert sorted(AXIS_TO_CONTROL.values()) == sorted(DIRECTOR_CONTROL_FIELDS)
assert len({d for spec in SEMANTIC_AXES.values() for d in spec[:2]}) == 12


def axis_for_control(control: str) -> str:
    return CONTROL_TO_AXIS[control]


def directions(axis: str) -> tuple[str, str]:
    """``(negative, positive)`` for one axis — the only legal values of its ``direction``."""
    spec = SEMANTIC_AXES[axis]
    return spec[0], spec[1]


# ---------------------------------------------------------------------------
# The model-facing schema
# ---------------------------------------------------------------------------


def model_schema() -> dict:
    """The JSON schema the model is constrained to, as a **fresh** plain ``dict``.

    Six **optional** axes inside a closed ``intent`` object; each present axis requires both
    ``direction`` (an axis-specific two-value enum) and ``strength`` (integer ``1..100``). Fresh
    each call so a caller may serialise or mutate the result without reaching a shared object.

    ``{"intent": {}}`` is valid and means "the user expressed no actionable editing preference" —
    the Director must not invent one. Verified reachable on the installed llama.cpp build.

    ``seed`` is **absent**, and that absence is the contract: with ``additionalProperties`` false
    the grammar cannot emit one, so the GUI remains the only place a Variation Seed is minted. No
    execution control name appears anywhere in this document.
    """
    axis_properties: dict[str, dict] = {}
    for axis, spec in SEMANTIC_AXES.items():
        negative, positive = spec[0], spec[1]
        axis_properties[axis] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["direction", "strength", BASIS_KEY],
            "properties": {
                "direction": {"type": "string", "enum": [negative, positive]},
                "strength": {"type": "integer", "minimum": STRENGTH_MIN, "maximum": STRENGTH_MAX},
                BASIS_KEY: {"type": "string", "enum": list(BASIS_VALUES)},
            },
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [INTENT_KEY],
        "properties": {
            INTENT_KEY: {
                "type": "object",
                "additionalProperties": False,
                "properties": axis_properties,
            },
            EXPLANATION_KEY: {"type": "string", "maxLength": EXPLANATION_MAX_CHARS},
        },
    }


def model_schema_json() -> str:
    """The schema as the compact string ``llama-completion --json-schema`` takes.

    Serialised here rather than in ``gui.py`` so the schema has exactly one owner — including its
    wire form — and the GUI needs no JSON dependency of its own.
    """
    return json.dumps(model_schema())


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def system_prompt() -> str:
    """The Director's system prompt: the six editing dimensions, in the user's language.

    Built from :data:`SEMANTIC_AXES`, so the prompt and the schema enumerate one list. Asserted to
    be pure ASCII — it is a process argument to a native binary, so a decorative em dash is a
    mojibake risk for no gain — and asserted to contain none of the six internal control names.
    """
    lines = [
        "You are BeatSync's AI Director. You read one editing intention and describe what the "
        "user is asking for, using a small fixed set of editing dimensions, for a "
        "beat-synchronised music video.",
        "",
        "There are six dimensions. For each one you report, give a direction, a strength and a "
        "basis:",
        f"  strength is a whole number from {STRENGTH_MIN} to {STRENGTH_MAX}, where "
        f"{STRENGTH_MIN} is a very slight preference and {STRENGTH_MAX} is as strong as possible.",
        f"  basis is either \"{BASIS_REQUESTED}\" or \"{BASIS_ASSOCIATED}\".",
        "",
    ]
    for axis, spec in SEMANTIC_AXES.items():
        negative, positive, _control, meaning = spec
        lines.append(f"{axis} ({negative} / {positive}): {meaning}")
    lines.extend([
        "",
        "Report a dimension when the user's words directly express it, or when ordinary editing "
        "and style language clearly and conventionally implies it. The user does not have to name "
        "editing mechanics literally.",
        "",
        "cut_pacing, scene_reading and motion_preference answer to everyday descriptions of "
        "style. Where a described style carries a clear and conventional implication for one of "
        "those three, report it even though the user never named it.",
        "",
        "impact_accents, section_reactivity and source_variety are narrower, and need words aimed "
        "at what that dimension is itself about: brief accenting on the biggest musical impacts; "
        "how much the choice of imagery follows the song's structure and each section's intensity; "
        "how widely the edit draws on the available source videos. Apply a stricter standard to "
        "these three. A described style that only conveys how intense or how pleasing the result "
        "should be does not reach that standard, however strongly it is put.",
        "",
        "For a short, broad description of style, report the smallest defensible set of "
        "dimensions. Do not report several dimensions merely because together they would amount "
        "to a plausible house style. If one dimension captures what was asked, one is enough; "
        "report more than one only where separate elements of the instruction independently "
        "support each of them.",
        "",
        "Omit every dimension the instruction does not speak to, and omit any dimension you are "
        "unsure about. There is no neutral option and no way to say \"leave this alone\" other "
        "than leaving it out: omitting a dimension simply means the user expressed no preference "
        "about it.",
        "",
        "Do not turn a specific request into a generally more intense or more extreme edit. Do "
        "not add a second dimension to reinforce, balance or compensate for the one actually "
        "asked about. If the instruction speaks to one dimension, report only that one.",
        "",
        "A broad instruction about overall style may genuinely speak to several dimensions at "
        "once. Report each dimension you can justify from the user's own words, and no others.",
        "",
        "If the instruction expresses no actionable editing preference at all, report an empty "
        "set of dimensions.",
        "",
        "For every dimension you report, say which of the two it is. Report it as "
        f"\"{BASIS_REQUESTED}\" when the user's own instruction expresses that editing preference, "
        "either plainly or by a clear implication of what they said. Report it as "
        f"\"{BASIS_ASSOCIATED}\" when the dimension merely seems to fit the style they described, "
        "but they did not ask for it.",
        "",
        "This is not a measure of how certain you are, and it is not strength under another name. "
        f"A slight preference the user genuinely expressed is still \"{BASIS_REQUESTED}\", however "
        "gently they put it. A dimension you are confident would complement their style is still "
        f"\"{BASIS_ASSOCIATED}\" if they never asked for it.",
        "",
        "Label the basis honestly. Do not omit a dimension you would have to call "
        f"\"{BASIS_ASSOCIATED}\" in order to present a stricter answer, and do not report a "
        "dimension you would not report in any case just so you can label it "
        f"\"{BASIS_ASSOCIATED}\". Label each dimension you do report truthfully.",
        "",
        f"You may add one short sentence of reasoning in an optional \"{EXPLANATION_KEY}\" field, "
        f"at most {EXPLANATION_MAX_CHARS} characters. It is shown to the user for review and "
        "changes nothing. Describe only the dimensions you actually reported.",
        "You do not choose the clip variation seed, the audio levels, the source videos or any "
        "output setting. Answer with one JSON object only, with the dimensions you report inside "
        f"an \"{INTENT_KEY}\" object: no prose outside it, and no code fences.",
    ])
    text = "\n".join(lines)
    assert text.isascii(), "the system prompt is a native process argument; keep it ASCII"
    lowered = text.lower()
    for name in _INTERNAL_CONTROL_NAMES:
        assert name not in lowered, f"internal control name {name!r} leaked into the system prompt"
    return text


def user_prompt(instruction: str) -> str:
    """The user half: the editing intention, labelled and nothing else.

    No creative base, no media description, no music features — see the module docstring. The
    instruction is presented as an absolute intention rather than a transformation of the current
    settings, because the Director deliberately does not read them.
    """
    return f"Editing intention: {instruction}"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _collapse(text: str, limit: int) -> str:
    """Whitespace-collapsed and length-bounded. Total, and never raises."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip()


def normalize_instruction(value: Any) -> str:
    """The instruction as a single bounded line, or ``""`` when there is no instruction at all."""
    if not isinstance(value, str):
        return ""
    return _collapse(value, INSTRUCTION_MAX_CHARS)


def normalize_explanation(value: Any) -> str:
    """The explanation as bounded display text, or ``""``.

    Deliberately tolerant, and deliberately **display only**: a missing field, a non-string field
    and an overlong field are all survivable, because this value reaches a read-only textbox and
    nothing else. The execution half is the opposite.
    """
    if not isinstance(value, str):
        return ""
    return _collapse(value, EXPLANATION_MAX_CHARS)


# ---------------------------------------------------------------------------
# The validated semantic intent
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SemanticAxisRequest:
    """One reported dimension: a direction from that axis's own pair, a strength and a basis.

    ``basis`` records *whether the user asked*; ``strength`` records *how much they want it*. The
    two are independent, and conflating them is the mistake worth naming: a faintly-put request is
    ``requested`` with a small strength, while a confident stylistic hunch is ``associated`` no
    matter how strong it feels. :attr:`is_actionable` answers only the first question.
    """

    axis: str
    direction: str
    strength: int
    basis: str

    def __post_init__(self) -> None:
        if self.axis not in SEMANTIC_AXES:
            raise ValueError(f"unknown semantic axis {self.axis!r}")
        if self.direction not in directions(self.axis):
            raise ValueError(f"{self.axis!r} cannot be {self.direction!r}")
        if isinstance(self.strength, bool) or not isinstance(self.strength, int):
            raise ValueError(f"strength must be a plain int, got {self.strength!r}")
        if not STRENGTH_MIN <= self.strength <= STRENGTH_MAX:
            raise ValueError(f"strength {self.strength} outside {STRENGTH_MIN}..{STRENGTH_MAX}")
        if not isinstance(self.basis, str) or self.basis not in BASIS_VALUES:
            raise ValueError(f"basis must be one of {BASIS_VALUES}, got {self.basis!r}")

    @property
    def is_positive(self) -> bool:
        return self.direction == directions(self.axis)[1]

    @property
    def is_actionable(self) -> bool:
        """May this request move its execution control?

        Deliberately independent of :attr:`strength`: strength scales a preference that is already
        actionable, it never decides actionability. ``source_variety`` at strength 100 on an
        ``associated`` basis is exactly as non-actionable as the same axis at strength 1.
        """
        return self.basis in actionable_bases(self.axis)

    def describe(self) -> str:
        return f"{self.axis} {self.direction} ({self.strength}, {self.basis})"


@dataclass(frozen=True, slots=True)
class SemanticIntent:
    """Zero to six validated axis requests. Deepcopy-safe: a tuple of frozen scalars-only records.

    An **empty** intent is valid and resolves to the all-neutral recipe. That is the honest answer
    to an instruction expressing no actionable editing preference, and inventing a direction for it
    would be the Director guessing.
    """

    requests: tuple[SemanticAxisRequest, ...] = ()

    def __post_init__(self) -> None:
        seen = [r.axis for r in self.requests]
        if len(seen) != len(set(seen)):
            raise ValueError("one axis may be reported at most once")

    def by_axis(self) -> dict[str, SemanticAxisRequest]:
        return {r.axis: r for r in self.requests}

    def actionable_requests(self) -> tuple[SemanticAxisRequest, ...]:
        """Only the requests that may move an execution control.

        The raw :attr:`requests` tuple is **never** filtered in place — it is the provenance of what
        the model actually said, and the read-out and tests both depend on it surviving. The
        invariant this method exists to express is not "associated entries disappear" but
        "associated entries on requested-only axes cannot become execution controls".
        """
        return tuple(r for r in self.requests if r.is_actionable)

    def non_actionable_associations(self) -> tuple[SemanticAxisRequest, ...]:
        """Reported ``associated`` dimensions that product policy leaves at neutral.

        The input to the read-out's disclosure line. Note what decides membership: the request's
        own :attr:`~SemanticAxisRequest.is_actionable`, which reads the one frozen policy the
        resolver reads. It is deliberately **not** decided by noticing that some control came out
        at 50 — a genuine request can resolve near neutral, and an actionable axis is not silently
        reclassified because its arithmetic happened to land there.
        """
        return tuple(r for r in self.requests
                     if r.basis == BASIS_ASSOCIATED and not r.is_actionable)

    def describe(self) -> str:
        if not self.requests:
            return "no specific editing preference expressed"
        return ", ".join(r.describe() for r in self.requests)


# ---------------------------------------------------------------------------
# Strict parsing of the model's stdout
# ---------------------------------------------------------------------------


#: llama.cpp appends this exact literal to stdout when generation stops on end-of-sequence rather
#: than on the token budget. It is the **tool's** framing of its own output, not model prose, and it
#: is a fixed string rather than a pattern — so stripping exactly it, exactly once, from exactly the
#: end leaves every failure mode the strict parse exists to catch fully intact: a truncated object,
#: a code fence, a chatty preamble and real trailing commentary all still fail.
#:
#: Deliberately **not** generalised into a regex or a list of tolerated suffixes — that is the slope
#: this constant exists at the bottom of.
_END_OF_GENERATION_MARKER = "[end of text]"


def _is_plain_int(value: Any) -> bool:
    """A real ``int``, never a ``bool``.

    The same explicit boundary ``creative_recipe`` draws, for the same reason: ``bool`` subclasses
    ``int``, so ``strength: true`` would otherwise be accepted as the strength 1.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def parse_semantic_intent(stdout: Any) -> tuple[SemanticIntent, str] | None:
    """The **whole** of the model's stdout, parsed strictly, or ``None``.

    Returns ``(intent, explanation)``. Never raises.

    **The entire output is the machine payload.** There is deliberately no regex fishing an
    ``{...}`` out of surrounding prose: a broad ``\\{.*\\}`` search is how a truncated object, a
    code fence or a chatty preamble gets silently half-accepted, and Stage 5's own documented
    truncation defect lived exactly there. So: strip, remove the one fixed
    :data:`_END_OF_GENERATION_MARKER` llama.cpp appends to its own output, ``json.loads`` the lot,
    require a mapping. Prose, a fence, trailing commentary, an empty output and a JSON array all
    fail. The ``--json-schema`` grammar is defence in depth; this parser is the authority.

    A malformed **present** axis rejects the whole intent rather than being dropped — a producer
    that emits ``{"motion_preference": {"direction": "sideways"}}`` disagrees with this contract
    about what an intent is, and that disagreement is precisely what is worth failing on. Missing
    axes are not malformed: absence is the contract's way of saying "no preference".
    """
    if not isinstance(stdout, str):
        return None
    text = stdout.strip()
    if text.endswith(_END_OF_GENERATION_MARKER):
        text = text[:-len(_END_OF_GENERATION_MARKER)].rstrip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(payload, Mapping):
        return None

    keys = set(payload)
    if INTENT_KEY not in keys:
        return None
    if not keys <= {INTENT_KEY, EXPLANATION_KEY}:
        return None

    raw_intent = payload[INTENT_KEY]
    if not isinstance(raw_intent, Mapping):
        return None
    if not set(raw_intent) <= set(SEMANTIC_AXES):
        return None

    requests: list[SemanticAxisRequest] = []
    for axis in SEMANTIC_AXES:
        if axis not in raw_intent:
            continue
        spec = raw_intent[axis]
        if not isinstance(spec, Mapping):
            return None
        if set(spec) != {"direction", "strength", BASIS_KEY}:
            return None
        direction = spec["direction"]
        strength = spec["strength"]
        basis = spec[BASIS_KEY]
        if not isinstance(direction, str) or direction not in directions(axis):
            return None
        if not _is_plain_int(strength) or not STRENGTH_MIN <= strength <= STRENGTH_MAX:
            return None
        # No default and no salvage: a present axis without a legal basis is a producer disagreeing
        # with this contract about what a reported dimension is. In particular an absent `basis` is
        # NOT read as "requested" — that would silently promote unclassified output to execution.
        if not isinstance(basis, str) or basis not in BASIS_VALUES:
            return None
        requests.append(SemanticAxisRequest(axis=axis, direction=direction, strength=strength,
                                            basis=basis))

    try:
        intent = SemanticIntent(requests=tuple(requests))
    except ValueError:
        return None
    return intent, normalize_explanation(payload.get(EXPLANATION_KEY))


# ---------------------------------------------------------------------------
# The deterministic semantic -> control mapping
# ---------------------------------------------------------------------------


def magnitude_for_strength(strength: int) -> int:
    """``half_up(strength / 2)`` as exact integer arithmetic: ``(strength + 1) // 2``.

    Explicitly **not** ``round()``: banker's rounding sends ``0.5`` to ``0``, which would make a
    strength of 1 a silent no-op, and sends ``24.5`` to ``24``, which would break monotonicity at
    every half point. For a positive integer numerator over 2 the shift identity is exact.
    """
    if isinstance(strength, bool) or not isinstance(strength, int):
        raise ValueError(f"strength must be a plain int, got {strength!r}")
    if not STRENGTH_MIN <= strength <= STRENGTH_MAX:
        raise ValueError(f"strength {strength} outside {STRENGTH_MIN}..{STRENGTH_MAX}")
    return (strength + 1) // 2


def resolve_semantic_intent(intent: Any) -> dict[str, int] | None:
    """One validated :class:`SemanticIntent` -> the complete six-control BASE mapping, or ``None``.

    The **only** place the model-facing vocabulary and the execution vocabulary meet.

    * an **omitted** axis leaves its control at exactly ``50`` — never a previous slider value,
      never a preset value, never a model default, never a guess;
    * a reported **actionable** axis lands at ``50 ± magnitude_for_strength(strength)``, below
      neutral for the axis's negative direction and above it for the positive one;
    * a reported axis whose ``basis`` is not actionable for it resolves **exactly as an omitted
      axis** — ``50``, at any strength.

    That third rule is the whole product meaning of ``basis``, and it lives here rather than in
    ``gui.py`` so that every caller gets it: the GUI, the media adapter, Apply and the tests all
    read the same resolved truth. The raw intent is left untouched — it is provenance, and
    :meth:`SemanticIntent.actionable_requests` is how a caller asks what survived.

    Every result is six plain ``int``s in ``0..100``, which the final assertion checks rather than
    assumes.
    """
    if not isinstance(intent, SemanticIntent):
        return None
    resolved = {control: fork_creative.DEFAULT_CONTROL for control in DIRECTOR_CONTROL_FIELDS}
    for request in intent.actionable_requests():
        magnitude = magnitude_for_strength(request.strength)
        control = AXIS_TO_CONTROL[request.axis]
        value = (fork_creative.DEFAULT_CONTROL + magnitude if request.is_positive
                 else fork_creative.DEFAULT_CONTROL - magnitude)
        if not fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX:
            return None
        resolved[control] = value
    assert all(_is_plain_int(v) and fork_creative.CONTROL_MIN <= v <= fork_creative.CONTROL_MAX
               for v in resolved.values()), resolved
    return resolved


def apply_media_adaptation(
    base: Any, summary: Any,
) -> tuple[dict[str, int], fork_media.MediaAdjustment | None]:
    """``(final_controls, adjustment_or_None)`` — the one narrow media-aware step.

    Only ``source_diversity`` may change, only when the BASE asks for *more* diversity than
    neutral, and only from a prepared library whose effective-source count cannot support it. Every
    other control is copied through untouched: ``cut_density`` and ``micro_cuts`` are Stage-4
    decisions that media interpretation never sees, and the other three media-adjustable candidates
    were measured and dropped (see ``director_media``).

    BASE is **not** mutated — the caller keeps it for provenance.
    """
    if not isinstance(base, Mapping):
        return {}, None
    final = dict(base)
    value, adjustment = fork_media.adapt_source_diversity(
        base.get(fork_media.ADJUSTABLE_FIELD), summary)
    if adjustment is not None:
        final[fork_media.ADJUSTABLE_FIELD] = value
    return final, adjustment


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectorProposal:
    """One reviewed-but-not-applied answer: BASE, FINAL, the prose beside them, and their cause.

    Frozen, and every reachable value is a plain deepcopy-safe one — the two recipes are seven
    ``int``s each, ``intent`` is a tuple of frozen scalar records, ``adjustment`` is six scalars,
    and the other two fields are ``str``. That is a real constraint rather than a style note:
    ``gr.State`` deep-copies its value, so a ``MappingProxyType``, a model object, a process
    handle, source state, a ``VariantLabConfig``, a ``PreparedMediaSummary`` holding candidate data
    or a cache object could not live here. ``copy.deepcopy(proposal) == proposal`` is asserted by
    test.

    ``base_recipe`` is carried for **provenance only**. :meth:`recipe` — what Apply writes — is
    always FINAL. Both share the one minted Variation Seed, so the read-out cannot imply that the
    media step re-rolled the clip selection.
    """

    final_recipe: CreativeRecipe
    base_recipe: CreativeRecipe
    intent: SemanticIntent
    explanation: str
    instruction: str
    adjustment: fork_media.MediaAdjustment | None = None
    media_note: str = ""
    """One truthful line about why media adaptation did or did not apply. Never an authority."""

    @property
    def recipe(self) -> CreativeRecipe:
        """What Apply writes: the FINAL recipe, always."""
        return self.final_recipe

    @property
    def media_adjusted(self) -> bool:
        return self.adjustment is not None

    # -- reporting ----------------------------------------------------------

    def not_applied_line(self) -> str:
        """One truthful line naming the reported associations that were left neutral, or ``""``.

        This exists because ``basis`` created a distinction the read-out previously could not have:
        the model's ``explanation`` describes its **raw** reading, while the recipe shows what was
        **applied**, and on a requested-only axis those two can now legitimately disagree. The
        answer is to state the difference, not to edit the prose — the explanation is model
        provenance and is never parsed, rewritten, truncated or withheld here.

        Wording is attributed on purpose. It says the Director *marked* these as associated rather
        than requested; it does not assert that the user's instruction objectively contained no
        such request, because that is a claim about the world and all we have is a classification.
        """
        suppressed = self.intent.non_actionable_associations() \
            if isinstance(self.intent, SemanticIntent) else ()
        if not suppressed:
            return ""
        # Registry order, so two proposals with the same suppressed set read identically.
        names = [AXIS_DISPLAY_LABELS[axis] for axis in SEMANTIC_AXES
                 if any(r.axis == axis for r in suppressed)]
        return (f"Not applied (associated, not requested): {', '.join(names)} - left neutral.")

    def display_text(self) -> str:
        """The whole Director proposal read-out. ``gui.py`` formats none of it.

        When a media adjustment happened the read-out **visibly separates** the Director's
        interpretation from the deterministic media change, because they have different authors and
        the user is entitled to know which is which. The model is never credited with the media
        step, and the media step is never described as an improvement — P3 measured it as a
        trade-off, and :meth:`MediaAdjustment.describe` states both halves.
        """
        not_applied = self.not_applied_line()
        lines = [f"Instruction: {self.instruction}"]
        if self.media_adjusted:
            lines.append("")
            lines.append(f"Base:  {self.base_recipe.describe()}")
            if self.explanation:
                lines.append(f"Director: {self.explanation}")
            if not_applied:
                lines.append(not_applied)
            lines.append("")
            lines.append(f"Media adjustment: {self.adjustment.describe()}")
            lines.append("")
            lines.append(f"Final: {self.final_recipe.describe()}")
        else:
            lines.append(self.final_recipe.describe())
            if self.explanation:
                lines.append(f"Director: {self.explanation}")
            if not_applied:
                lines.append(not_applied)
            if self.media_note:
                lines.append(self.media_note)
        lines.append("Proposal only - press Apply Proposal to move the controls. "
                     "Nothing has been rendered.")
        return "\n".join(lines)


def build_proposal(
    instruction: Any,
    intent: Any,
    explanation: Any,
    seed: Any,
    summary: Any = None,
    media_note: str = "",
) -> DirectorProposal | None:
    """Assemble a proposal from a validated intent and a **GUI-minted** seed, or ``None``.

    The seed arrives as an argument because minting it is the one non-deterministic step and it
    lives in the GUI with every other draw in this application — ``variation.random_seed()`` is the
    only implementation, and this module owns no randomness.

    Order is the contract: resolve BASE, adapt to FINAL, then build **both** recipes through
    :meth:`CreativeRecipe.from_mapping`, which is reused **unmodified** and remains the authority.
    A bad seed, a bad control, a wrong key set or seed 0 rejects the whole proposal and returns
    ``None``. Nothing is coerced and nothing is half-applied, so an invalid model response cannot
    produce a plausible partial proposal.
    """
    base_controls = resolve_semantic_intent(intent)
    if base_controls is None:
        return None
    final_controls, adjustment = apply_media_adaptation(base_controls, summary)

    base_recipe = CreativeRecipe.from_mapping({"seed": seed, **base_controls})
    final_recipe = CreativeRecipe.from_mapping({"seed": seed, **final_controls})
    if base_recipe is None or final_recipe is None:
        return None

    return DirectorProposal(
        final_recipe=final_recipe,
        base_recipe=base_recipe,
        intent=intent,
        explanation=normalize_explanation(explanation),
        instruction=normalize_instruction(instruction),
        adjustment=adjustment,
        media_note=str(media_note or ""),
    )


# ---------------------------------------------------------------------------
# Status text
# ---------------------------------------------------------------------------
#
# The Director owns its own read-outs: no pipeline or render handler writes them, and no existing
# report panel is reused. Every message below is one line of plain text, and every failure says
# plainly that nothing changed — a Director error must never read as a quiet partial success.

STATUS_NO_INSTRUCTION = (
    "Describe the editing style you want, then press Generate Proposal. "
    "Nothing was generated and no control changed."
)

STATUS_INVALID_PAYLOAD = (
    "The model did not return a usable editing intent, so no proposal was created "
    "and no control changed. Try rephrasing the instruction."
)

STATUS_TIMEOUT = (
    f"The Director timed out after {TIMEOUT_SECONDS} seconds and was stopped. No proposal was "
    "created and no control changed."
)

STATUS_NOTHING_TO_APPLY = (
    "There is no proposal to apply. Press Generate Proposal first. Nothing changed."
)

# -- the media-eligibility notes (s19): truthful, and never called a fallback ----------------
MEDIA_NOTE_NO_SCAN = (
    "Media adjustment not used: there is no current Media Library scan."
)
MEDIA_NOTE_STALE_SCAN = (
    "Media adjustment not used: the Media Library controls changed since the scan."
)
MEDIA_NOTE_NOT_PREPARED = (
    "Media adjustment not used: the library is not fully prepared yet."
)
MEDIA_NOTE_NO_SUMMARY = (
    # Deliberately does not say "candidate": `tests/test_media_neutral_semantics.py` bans that word
    # in this module because a Stage-5 candidate must have nowhere here to enter, and a status
    # string is not worth weakening that guard for.
    "Media adjustment not used: the prepared library has no usable moments to measure."
)
MEDIA_NOTE_CHECKED_NO_CHANGE = (
    "Prepared media checked - no Source Diversity adjustment was needed."
)


def missing_runtime_status(path: Any) -> str:
    """A required local asset is absent, named so the user can see which one.

    There is deliberately **no** fallback to the Stage-5 2B model: it was measured against this
    intent contract and failed it, so silently substituting it would produce confident wrong
    recipes instead of an honest error.
    """
    return (f"The Director needs a local model that is not installed: {path}. "
            "Run install.bat to download it. No proposal was created and no control changed.")


def launch_failure_status(detail: Any) -> str:
    """The process could not be started at all."""
    return (f"The Director could not start the local model: {detail}. "
            "No proposal was created and no control changed.")


def exit_failure_status(returncode: Any, detail: Any = "") -> str:
    """The process ran and failed. ``detail`` is a bounded tail of its stderr, never the argv."""
    tail = _collapse(detail, 240) if isinstance(detail, str) else ""
    suffix = f" {tail}" if tail else ""
    return (f"The local model exited with code {returncode}, so no proposal was created and no "
            f"control changed.{suffix}")


def ready_status(proposal: DirectorProposal) -> str:
    """A proposal exists and is waiting for an explicit Apply."""
    media = " Source Diversity was relaxed for this prepared library." if proposal.media_adjusted \
        else ""
    return (f"Proposal ready (Variation Seed {proposal.recipe.seed}).{media} Review it above, then "
            "press Apply Proposal to move the Creative Controls. Nothing has been applied or "
            "rendered yet.")


def applied_status(proposal: DirectorProposal) -> str:
    """The proposal was written into the visible execution controls — and only those."""
    return (f"Applied the proposal: Variation Seed {proposal.recipe.seed} and the six Creative "
            "Controls. Edit them, use Variant Lab, or press Create Music Video when ready - "
            "nothing has been rendered.")


# The dataclass fields and the record this module documents are the same seven, so adding a field
# without revisiting the deepcopy-safety contract fails at import time.
assert tuple(f.name for f in fields(DirectorProposal)) == (
    "final_recipe", "base_recipe", "intent", "explanation", "instruction", "adjustment",
    "media_note")

# The model-facing surface must never mention an execution control. Checked at import, and again by
# a permanent test, because this is the whole architectural distinction V2 rests on.
_SCHEMA_TEXT = model_schema_json().lower()
for _name in _INTERNAL_CONTROL_NAMES:
    assert _name not in _SCHEMA_TEXT, f"internal control name {_name!r} leaked into the schema"


__all__ = [
    "AXIS_DISPLAY_LABELS",
    "AXIS_TO_CONTROL",
    "BASIS_ASSOCIATED",
    "BASIS_KEY",
    "BASIS_REQUESTED",
    "BASIS_VALUES",
    "CONTEXT_TOKENS",
    "CONTROL_TO_AXIS",
    "DIRECTOR_CONTROL_FIELDS",
    "DirectorProposal",
    "EXPLANATION_KEY",
    "EXPLANATION_MAX_CHARS",
    "INSTRUCTION_MAX_CHARS",
    "INTENT_KEY",
    "MAX_NEW_TOKENS",
    "MEDIA_NOTE_CHECKED_NO_CHANGE",
    "MEDIA_NOTE_NOT_PREPARED",
    "MEDIA_NOTE_NO_SCAN",
    "MEDIA_NOTE_NO_SUMMARY",
    "MEDIA_NOTE_STALE_SCAN",
    "REQUESTED_ONLY_AXES",
    "SEMANTIC_AXES",
    "STATUS_INVALID_PAYLOAD",
    "STATUS_NOTHING_TO_APPLY",
    "STATUS_NO_INSTRUCTION",
    "STATUS_TIMEOUT",
    "STRENGTH_MAX",
    "STRENGTH_MIN",
    "STYLE_INFERABLE_ASSOCIATED_AXES",
    "SemanticAxisRequest",
    "SemanticIntent",
    "TEMPERATURE",
    "TIMEOUT_SECONDS",
    "TOP_K",
    "actionable_bases",
    "applied_status",
    "apply_media_adaptation",
    "axis_for_control",
    "build_proposal",
    "directions",
    "exit_failure_status",
    "launch_failure_status",
    "magnitude_for_strength",
    "missing_runtime_status",
    "model_schema",
    "model_schema_json",
    "normalize_explanation",
    "normalize_instruction",
    "parse_semantic_intent",
    "ready_status",
    "resolve_semantic_intent",
    "system_prompt",
    "user_prompt",
]
