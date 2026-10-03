#!/usr/bin/env python3
"""[FORK] Digital-Union: AI Director V1 — the pure proposal boundary.

The Director turns one sentence of editing intent into **one visual**
:class:`~beatsync_fork.creative_recipe.CreativeRecipe`, which the user then reviews and explicitly
applies::

    natural-language intent
        -> local text-only Qwen generation          (gui.py performs the subprocess)
        -> strictly validated six-control payload   (this module)
        -> GUI mints the Variation Seed             (variation.random_seed, in gui.py)
        -> CreativeRecipe.from_mapping              (the existing, unweakened trust boundary)
        -> DirectorProposal                         (this module)
        -> the user presses Apply Proposal
        -> the existing Variation Seed + six Creative Controls

**The Director proposes; it never renders, and it is not a second execution path.** It is a *second
producer* of the artifact Variant Lab already produces — which is exactly what
``creative_recipe.py``'s docstring anticipated when it refused to carry a master seed, a spread or
any other generator provenance. Nothing here is a new interpretation of a recipe: the visible
Variation Seed and the six sliders remain the sole execution truth, and every stage below them is
unchanged and has never heard of a Director.

===============================================================================
What V1 produces, and what it deliberately does not
===============================================================================

V1 is **visual only**. The model emits exactly the six 0..100 creative controls in
``presets.CREATIVE_CONTROL_FIELDS`` order and nothing else. It does **not** generate or modify the
three audio levels, voice clips, voice timing, ``avoid_drops``, the SFX folder or roles, the source
folder or files, FPS, the encoder, the output filename, the source confirmation or the Media Library
Preparation state. Those are resource identity and physical render intent; a text prompt is not an
authority on them.

**The model never chooses the Variation Seed.** The schema has no ``seed`` property and — because
``additionalProperties`` is ``false`` — an emitted one is a grammar violation; a ``seed`` key that
somehow arrives anyway is rejected by :func:`parse_model_payload`. The seed is minted in the GUI by
the existing ``variation.random_seed()`` *after* a valid six-control payload exists, so there is no
second seed implementation and an invalid model response cannot produce a plausible partial
proposal.

===============================================================================
Two trust contracts, deliberately opposite — again
===============================================================================

The execution half is strict and the explanation is not, and conflating them is the mistake worth
naming:

* **The six controls are a contract boundary.** One malformed field rejects the *whole* proposal.
  Nothing is coerced, clamped, defaulted or partially applied, and there is no fallback to Balanced
  — a mixture of three model values and three silent 50s looks deliberate and is not.
  :meth:`CreativeRecipe.from_mapping` is the authority and is reused **unmodified**.
* **``explanation`` is non-load-bearing UI text.** A missing one, a non-string one and an overlong
  one all still yield a valid proposal; the explanation is simply blank or bounded. It never enters
  the recipe, ``CreativeProfile``, ``beat_info``, ``render_info``, any stage, cache identity, a
  Stage-5 Qwen request or the planner. Failing a whole proposal because the prose was ugly would be
  strictness pointed at the one field where it buys nothing.

===============================================================================
Media-blind, cacheless, and out of the Stage-5 world entirely
===============================================================================

The Director invocation receives the instruction, the system prompt and the schema. It receives no
video frames, no source filenames, no Stage-5 semantic records, no ``beat_info``, no sections, no
tempo, no music features and no current source state — so it cannot perturb Stage-5 cache identity
or persisted media semantics, and ``CACHE_CONTRACT_VERSION`` / ``ANALYSIS_VERSION`` are untouched. A
content-aware Director is a later milestone, not a dormant abstraction here.

There is **no cache of any kind**: no Stage-5 cache use, no proposal cache, no prompt history and no
conversation state. Every Generate Proposal is independent, and the Director deliberately does not
read the current sliders either — the instruction is an *absolute* editing intention, so a
transform-what-I-have mode is out of scope rather than half-built.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): a schema, two prompts, a parser,
one frozen record and the text it formats. No subprocess, no model path, no filesystem, no clock and
no randomness live here — this module decides, and ``gui.py`` performs every side effect.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from beatsync_fork import creative as fork_creative
from beatsync_fork import presets as fork_presets
from beatsync_fork.creative_recipe import CreativeRecipe

#: The exact properties the model emits, **derived** from the one creative-control registry rather
#: than restated. A second hard-coded six-field tuple is how the Director's schema and the sliders
#: would eventually disagree about what "the six controls" are; the equality below is load-bearing
#: rather than decorative.
DIRECTOR_CONTROL_FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS

#: The optional, non-execution field. Named once so the parser, the schema and the display text
#: cannot drift onto two spellings.
EXPLANATION_KEY = "explanation"

#: Bound on the explanation, in characters. Declared in the model schema *and* — load-bearingly —
#: enforced again by :func:`normalize_explanation` on the way to the screen.
#:
#: **The schema half is advisory on the installed build, measured rather than assumed.**
#: `maxLength` is not compiled into llama.cpp's JSON-schema grammar there: generations at declared
#: limits of 60, 160 and 280 produced identical ~460-character strings. So this constant is a
#: request in the schema and a guarantee only in the normaliser, which is why the normaliser exists
#: rather than trusting the grammar. The reason a bound is wanted at all is Stage 5's documented
#: truncation defect — an **unbounded** string ate the token budget so the JSON never closed
#: (`.claude/rules/stage5-worker.md`) — and the mitigation here is the same shape: ask for one short
#: sentence in the prompt, budget :data:`MAX_NEW_TOKENS` at roughly three times the measured need,
#: and bound what reaches the screen.
EXPLANATION_MAX_CHARS = 280

#: Bound on the instruction accepted from the textbox. Not a safety claim — it is the same
#: reasoning as the explanation bound, applied to the input: a prompt long enough to exhaust the
#: context window produces a failure that looks like a model fault.
INSTRUCTION_MAX_CHARS = 2000

# ---------------------------------------------------------------------------
# Generation settings
# ---------------------------------------------------------------------------
#
# Hard-coded rather than environment variables, for the reason
# `.claude/rules/stage5-worker.md` records for the recovery constants: a knob that changes a
# result belongs under contract, not in an env var nobody audits. These are *not* Stage-5
# settings, they reach no cache key, and they re-key nothing — the Director has no cache at all.

#: Context window for the one-shot invocation. The whole conversation is one system prompt plus one
#: short instruction, so this is generous rather than tuned.
CONTEXT_TOKENS = 2048

#: Generation budget. Measured: the real six-field answer plus a one-sentence explanation is about
#: 115 tokens, so this is roughly three times the need — deliberate headroom, because exhausting
#: the budget mid-string is exactly how the JSON fails to close.
MAX_NEW_TOKENS = 320

#: Greedy decoding, matching the repository's existing Qwen convention
#: (`stage5_qwen_scene_worker` uses temperature 0 / top-k 1). So the same instruction proposes the
#: same six controls — which is the honest product: the Director reads *words*, and a reworded
#: instruction is the way to get a different reading. The Variation Seed is freshly minted every
#: time regardless, so two proposals from one instruction are still two different edits.
TEMPERATURE = 0.0
TOP_K = 1

#: The hard wall on one Director generation. A proposal is an interactive action, so an unbounded
#: wait is not an option, and the bound is deliberately generous against the measured one-shot cost
#: (cold model load every time, because the process exits).
TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# The model-facing schema
# ---------------------------------------------------------------------------


def model_schema() -> dict:
    """The JSON schema the model is constrained to, as a **fresh** plain ``dict``.

    Exactly the six execution controls — required, integer, 0..100 — plus an optional bounded
    ``explanation``, with ``additionalProperties`` false. Fresh each call so a caller may serialise
    or mutate the result without reaching a shared object.

    ``seed`` is **absent**, and that absence is the contract: with ``additionalProperties`` false
    the grammar cannot emit one, so the GUI remains the only place a Variation Seed is minted.
    There is deliberately no schema-version machinery in V1 — a version field is migration
    machinery for a migration that has not happened.
    """
    properties: dict[str, dict] = {
        name: {
            "type": "integer",
            "minimum": fork_creative.CONTROL_MIN,
            "maximum": fork_creative.CONTROL_MAX,
        }
        for name in DIRECTOR_CONTROL_FIELDS
    }
    properties[EXPLANATION_KEY] = {"type": "string", "maxLength": EXPLANATION_MAX_CHARS}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(DIRECTOR_CONTROL_FIELDS),
        "properties": properties,
    }


def model_schema_json() -> str:
    """The schema as the compact string ``llama-cli --json-schema`` takes.

    Serialised here rather than in ``gui.py`` so the schema has exactly one owner — including its
    wire form — and the GUI needs no JSON dependency of its own.
    """
    return json.dumps(model_schema())


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
#
# The control descriptions are the current repository semantics and nothing more. Describing a
# control with behaviour the implementation does not have is worse than describing it vaguely: the
# model would confidently answer a question BeatSync is not asking. In particular Semantic Emphasis
# re-weights STAGE 6's reading of an already-analysed library — it does not change Stage-5 tagging,
# and it does not switch analysis off.

_CONTROL_SEMANTICS = (
    ("cut_density",
     "Sparse <-> Dense. How many of the detected beats become cuts. "
     "0 = fewer, longer cuts that hold each shot; 50 = current neutral behaviour; "
     "100 = denser, more frequent cuts. Cuts always land on the detected beat grid."),
    ("micro_cuts",
     "Fewer <-> More accents. Only the rare extra half-beat cut on the biggest impacts. "
     "0 = no micro-cut accent layer at all; 50 = current neutral baseline accents; "
     "100 = stronger accents, still bounded so the edit cannot become flickery."),
    ("semantic_emphasis",
     "Visual metrics <-> Semantic context. How the planner weights an already-analysed library. "
     "Lower leans on measured visual metrics (motion, sharpness, colour, exposure); "
     "higher gives more weight to what the scene was understood to contain. "
     "50 = current neutral behaviour. This never changes how the videos were analysed."),
    ("energy_response",
     "Weak <-> Strong target matching. Lower scores moments on generic visual flow; "
     "higher follows the musical segment's own target (drop, build, soft) harder. "
     "50 = current neutral behaviour."),
    ("motion_bias",
     "Calm <-> Dynamic. Lower prefers calmer, steadier source moments; "
     "higher prefers more dynamic, moving ones. 50 = current neutral behaviour."),
    ("source_diversity",
     "Reuse <-> Diverse. Lower concentrates the edit on fewer source videos and reuses them more; "
     "higher spreads it across more of them. 50 = current neutral behaviour."),
)

# A structural guard rather than a comment: the described controls and the registry are the same
# six, in the same order, so a control added to `presets.CREATIVE_CONTROL_FIELDS` without a
# semantic description fails at import time instead of being silently left for the model to guess.
assert tuple(name for name, _text in _CONTROL_SEMANTICS) == DIRECTOR_CONTROL_FIELDS


def system_prompt() -> str:
    """The Director's system prompt: what the six controls actually mean in this application.

    Built from :data:`_CONTROL_SEMANTICS`, so the prompt and the schema enumerate one list.
    """
    lines = [
        "You are BeatSync's AI Director. You translate one editing intention into exactly six "
        "integer creative controls for a beat-synchronised music video.",
        "",
        "All six values are integers from "
        f"{fork_creative.CONTROL_MIN} to {fork_creative.CONTROL_MAX}. "
        f"{fork_creative.DEFAULT_CONTROL} is neutral: it means exactly what BeatSync does by "
        "default, so only move a control as far as the intention actually asks for.",
        "",
    ]
    lines.extend(f"{name}: {text}" for name, text in _CONTROL_SEMANTICS)
    lines.extend([
        "",
        f"You may add one short sentence of reasoning in an optional \"{EXPLANATION_KEY}\" field, "
        f"at most {EXPLANATION_MAX_CHARS} characters. It is shown to the user for review and "
        "changes nothing.",
        "You do not choose the clip variation seed, the audio levels, the source videos or any "
        "output setting. Answer with one JSON object only: no prose outside it, and no code "
        "fences.",
    ])
    return "\n".join(lines)


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
    """The instruction as a single bounded line, or ``""`` when there is no instruction at all.

    ``""`` is the one answer for "nothing was typed", so the caller needs a single branch: ``None``,
    a non-string, whitespace only and an empty box all produce it. Total — this runs on a live
    widget value and may not raise.
    """
    if not isinstance(value, str):
        return ""
    return _collapse(value, INSTRUCTION_MAX_CHARS)


def normalize_explanation(value: Any) -> str:
    """The explanation as bounded display text, or ``""``.

    Deliberately tolerant, and deliberately **display only**: a missing field, a non-string field
    and an overlong field are all survivable, because this value reaches a read-only textbox and
    nothing else. See the module docstring for why the execution half is the opposite.
    """
    if not isinstance(value, str):
        return ""
    return _collapse(value, EXPLANATION_MAX_CHARS)


# ---------------------------------------------------------------------------
# Strict parsing of the model's stdout
# ---------------------------------------------------------------------------


#: llama.cpp appends this exact literal to stdout when generation stops on end-of-sequence rather
#: than on the token budget. It is the **tool's** framing of its own output, not model prose, and it
#: is a fixed string rather than a pattern — so stripping exactly it, exactly once, from exactly the
#: end leaves every failure mode the strict parse exists to catch fully intact: a truncated object,
#: a code fence, a chatty preamble and real trailing commentary all still fail, because anything
#: other than this one constant remains in the text handed to ``json.loads``.
#:
#: Measured on the installed build (``b9842-6f4f53f2b``, text-only, no mmproj): with
#: ``--no-display-prompt`` and ``--no-perf``, stdout is the JSON object followed by
#: ``" [end of text]"`` and trailing newlines; the banner, the timings and every log line go to
#: stderr. Deliberately **not** generalised into a regex or a list of tolerated suffixes — that is
#: the slope this constant exists at the bottom of.
_END_OF_GENERATION_MARKER = "[end of text]"


def _is_plain_int(value: Any) -> bool:
    """A real ``int``, never a ``bool``.

    The same explicit boundary ``creative_recipe`` draws, for the same reason: ``bool`` subclasses
    ``int``, so ``cut_density: true`` would otherwise be accepted as the control value 1.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_valid_control(value: Any) -> bool:
    return (_is_plain_int(value)
            and fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX)


def parse_model_payload(stdout: Any) -> dict | None:
    """The **whole** of the model's stdout, parsed strictly, or ``None``.

    Returns a fresh ``dict`` of the six validated controls plus a normalised ``explanation`` string
    — i.e. the payload the caller may mint a seed for. Never raises.

    **The entire output is the machine payload.** There is deliberately no regex fishing a
    ``{...}`` out of surrounding prose: a broad `\\{.*\\}` search is how a truncated object, a code
    fence or a chatty preamble gets silently half-accepted, and Stage 5's own documented truncation
    defect lived exactly there. So: strip, remove the one fixed
    :data:`_END_OF_GENERATION_MARKER` llama.cpp appends to its own output, ``json.loads`` the lot,
    require a mapping. Prose, a fence, trailing commentary, an empty output and a JSON array all
    fail the proposal. The ``--json-schema`` grammar is defence in depth; this parser is the
    authority.

    The key set must be the six controls, optionally plus ``explanation``, and nothing else. An
    unexpected property — including ``seed``, which the Director must never receive from a model —
    means the producer and this contract disagree about what a proposal is, which is precisely the
    disagreement worth failing on.
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
    required = set(DIRECTOR_CONTROL_FIELDS)
    if not required <= keys:
        return None
    if not keys <= required | {EXPLANATION_KEY}:
        return None
    for name in DIRECTOR_CONTROL_FIELDS:
        if not _is_valid_control(payload[name]):
            return None

    controls = {name: payload[name] for name in DIRECTOR_CONTROL_FIELDS}
    controls[EXPLANATION_KEY] = normalize_explanation(payload.get(EXPLANATION_KEY))
    return controls


# ---------------------------------------------------------------------------
# The proposal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectorProposal:
    """One reviewed-but-not-applied answer: a valid recipe, the prose beside it, and its cause.

    Frozen, and every reachable value is a plain deepcopy-safe one — ``CreativeRecipe`` is seven
    ``int``s and the other two fields are ``str``. That is a real constraint rather than a style
    note: ``gr.State`` deep-copies its value, so a ``MappingProxyType``, a model object, a process
    handle, a filesystem object, source state, a ``VariantLabConfig``, an ``AudioVariantConfig`` or
    a cache object could not live here. ``copy.deepcopy(proposal) == proposal`` is asserted by test.

    ``instruction`` is carried for the read-out only, so the user can see *which* sentence produced
    the numbers on screen. Like ``explanation``, it reaches no stage, no profile and no cache.
    """

    recipe: CreativeRecipe
    explanation: str
    instruction: str

    # -- reporting ----------------------------------------------------------

    def display_text(self) -> str:
        """The whole Director proposal read-out. ``gui.py`` formats none of it.

        One formatter per read-out, exactly as the Variant Lab and mix reports work. The recipe's
        own ``describe()`` supplies the numbers, so the Director does not restate them in a second
        format that could drift.
        """
        lines = [
            f"Instruction: {self.instruction}",
            self.recipe.describe(),
        ]
        if self.explanation:
            lines.append(f"Director: {self.explanation}")
        lines.append("Proposal only — press Apply Proposal to move the controls. "
                     "Nothing has been rendered.")
        return "\n".join(lines)


def build_proposal(instruction: Any, payload: Any, seed: Any) -> DirectorProposal | None:
    """Assemble a proposal from a parsed payload and a **GUI-minted** seed, or ``None``.

    The seed arrives as an argument because minting it is the one non-deterministic step and it
    lives in the GUI with every other draw in this application — ``variation.random_seed()`` is the
    only implementation, and this module owns no randomness.

    The recipe is built by handing the exact seven-key mapping to
    :meth:`CreativeRecipe.from_mapping`, which is reused **unmodified** and remains the authority:
    a bad seed, a bad control, a wrong key set or seed 0 rejects the whole proposal and returns
    ``None``. Nothing is coerced and nothing is half-applied, so an invalid model response cannot
    produce a plausible partial proposal.
    """
    if not isinstance(payload, Mapping):
        return None
    if not set(DIRECTOR_CONTROL_FIELDS) <= set(payload):
        return None

    mapping: dict[str, Any] = {"seed": seed}
    mapping.update({name: payload[name] for name in DIRECTOR_CONTROL_FIELDS})
    recipe = CreativeRecipe.from_mapping(mapping)
    if recipe is None:
        return None

    return DirectorProposal(
        recipe=recipe,
        explanation=normalize_explanation(payload.get(EXPLANATION_KEY)),
        instruction=normalize_instruction(instruction),
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
    "The model did not return a usable set of six creative controls, so no proposal was created "
    "and no control changed. Try rephrasing the instruction."
)

STATUS_TIMEOUT = (
    f"The Director timed out after {TIMEOUT_SECONDS} seconds and was stopped. No proposal was "
    "created and no control changed."
)

STATUS_NOTHING_TO_APPLY = (
    "There is no proposal to apply. Press Generate Proposal first. Nothing changed."
)


def missing_runtime_status(path: Any) -> str:
    """A required local asset is absent, named so the user can see which one."""
    return (f"The Director needs a local model runtime that is not installed: {path}. "
            "No proposal was created and no control changed.")


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
    return (f"Proposal ready (Variation Seed {proposal.recipe.seed}). Review it above, then press "
            "Apply Proposal to move the Creative Controls. Nothing has been applied or rendered "
            "yet.")


def applied_status(proposal: DirectorProposal) -> str:
    """The proposal was written into the visible execution controls — and only those."""
    return (f"Applied the proposal: Variation Seed {proposal.recipe.seed} and the six Creative "
            "Controls. Edit them, use Variant Lab, or press Create Music Video when ready — "
            "nothing has been rendered.")


# The dataclass fields and the record this module documents are the same three, so adding a field
# without revisiting the deepcopy-safety contract fails at import time.
assert tuple(f.name for f in fields(DirectorProposal)) == ("recipe", "explanation", "instruction")


__all__ = [
    "CONTEXT_TOKENS",
    "DIRECTOR_CONTROL_FIELDS",
    "DirectorProposal",
    "EXPLANATION_KEY",
    "EXPLANATION_MAX_CHARS",
    "INSTRUCTION_MAX_CHARS",
    "MAX_NEW_TOKENS",
    "STATUS_INVALID_PAYLOAD",
    "STATUS_NO_INSTRUCTION",
    "STATUS_NOTHING_TO_APPLY",
    "STATUS_TIMEOUT",
    "TEMPERATURE",
    "TIMEOUT_SECONDS",
    "TOP_K",
    "applied_status",
    "build_proposal",
    "exit_failure_status",
    "launch_failure_status",
    "missing_runtime_status",
    "model_schema",
    "model_schema_json",
    "normalize_explanation",
    "normalize_instruction",
    "parse_model_payload",
    "ready_status",
    "system_prompt",
    "user_prompt",
]
