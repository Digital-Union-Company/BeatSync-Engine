"""AI Director V1: the pure proposal boundary, and the GUI runtime seam around it.

Two halves, two techniques, and the split mirrors the feature's own design:

* **`beatsync_fork/director.py` is imported normally** and tested as ordinary code — the schema,
  both prompts, the strict parser, the explanation policy, the frozen `DirectorProposal` and its
  read-out. That is what the stdlib-only hard rule buys, and it is where every *decision* lives.
* **`gui.py` cannot be imported here** (its prologue runs `logger.setup_environment()` and then
  imports gradio/cupy/cv2), so the runtime half is reached the way the rest of this suite reaches
  it: the real handler bodies are AST-extracted from `src/gui.py` and executed against a
  synthesised namespace. The only substitutions are the three runtime-path constants, a `gr.skip()`
  sentinel, and a `subprocess` shim that produces each failure outcome on demand. Everything about
  *policy* — what is validated, when the seed is minted, what Apply writes — is the production code.

No model is needed, no GPU, no FFmpeg and no portable runtime. The real-model acceptance is a
separate, out-of-repository exercise (see `.claude/rules/director.md`).
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import json
import os
import subprocess
from types import MappingProxyType
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import director as fork_director
from beatsync_fork import presets as fork_presets
from beatsync_fork import variation as fork_variation
from beatsync_fork.creative_recipe import CreativeRecipe

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_DIRECTOR = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "director.py")

FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS

#: One valid model answer, reused everywhere. Deliberately not six identical values: a payload of
#: 50s would pass several of these assertions for the wrong reason.
VALID = {
    "cut_density": 30,
    "micro_cuts": 10,
    "semantic_emphasis": 70,
    "energy_response": 60,
    "motion_bias": 25,
    "source_diversity": 55,
}

#: A real measured answer from the installed Qwen3-VL GGUF, byte for byte, including the
#: `[end of text]` terminator llama.cpp appends. Pinned as a fixture so the strict parser is proved
#: against the actual wire shape rather than against a hand-written ideal of it.
REAL_STDOUT = (
    '{\n  "cut_density": 20,\n  "micro_cuts": 10,\n  "semantic_emphasis": 70,\n'
    '  "energy_response": 60,\n  "motion_bias": 30,\n  "source_diversity": 50,\n'
    '  "explanation": "Fewer cuts enhance cinematic flow, aligning with emotional depth. '
    'Stronger energy response matches musical dynamics, while visual emphasis on meaningful '
    'moments ensures narrative impact."\n} [end of text]\n\n\n'
)


def payload(**overrides) -> str:
    body = dict(VALID)
    body.update(overrides)
    return json.dumps(body)


# ===========================================================================
# 1. THE MODEL SCHEMA
# ===========================================================================


def test_the_schema_properties_are_derived_from_the_one_control_registry():
    """`presets.CREATIVE_CONTROL_FIELDS` is the single six-field registry.

    A second hard-coded tuple inside the Director would be how its schema and the sliders
    eventually disagree about what "the six controls" are, so the equality is asserted rather than
    assumed — and the module carries the same assertion at import time.
    """
    assert fork_director.DIRECTOR_CONTROL_FIELDS == FIELDS
    assert fork_director.DIRECTOR_CONTROL_FIELDS is FIELDS

    schema = fork_director.model_schema()
    assert list(schema["required"]) == list(FIELDS)
    assert [k for k in schema["properties"] if k != "explanation"] == list(FIELDS)


def test_the_six_controls_are_required_integers_in_range():
    schema = fork_director.model_schema()
    for name in FIELDS:
        prop = schema["properties"][name]
        assert prop == {
            "type": "integer",
            "minimum": fork_creative.CONTROL_MIN,
            "maximum": fork_creative.CONTROL_MAX,
        }, name
        assert name in schema["required"], name


def test_additional_properties_are_forbidden():
    assert fork_director.model_schema()["additionalProperties"] is False
    assert fork_director.model_schema()["type"] == "object"


def test_the_schema_has_no_seed_property_and_never_asks_for_one():
    """`DIRECTOR_VARIATION_SEED_POLICY = GUI_MINTS`, expressed in the grammar.

    With `additionalProperties` false there is nowhere for a seed to go, so the GUI stays the only
    place a Variation Seed is minted and there is no second seed implementation.
    """
    schema = fork_director.model_schema()
    assert "seed" not in schema["properties"]
    assert "seed" not in schema["required"]
    assert "seed" not in json.dumps(schema)


def test_the_explanation_is_optional_bounded_and_a_string():
    schema = fork_director.model_schema()
    explanation = schema["properties"][fork_director.EXPLANATION_KEY]

    assert explanation["type"] == "string"
    assert explanation["maxLength"] == fork_director.EXPLANATION_MAX_CHARS
    assert 200 <= fork_director.EXPLANATION_MAX_CHARS <= 400, "approximately 280 characters"
    assert fork_director.EXPLANATION_KEY not in schema["required"]


def test_the_schema_carries_no_version_machinery():
    """No schema version in V1: a version field is migration machinery for a migration that has
    not happened, and the strict parser is what makes a mismatched producer visible."""
    rendered = json.dumps(fork_director.model_schema()).lower()
    for speculative in ("version", "$schema", "$id", "schema_version"):
        assert speculative not in rendered, speculative


def test_the_schema_is_fresh_each_call_so_a_caller_cannot_poison_it():
    first = fork_director.model_schema()
    first["properties"]["cut_density"]["maximum"] = 7
    first["additionalProperties"] = True

    assert fork_director.model_schema()["additionalProperties"] is False
    assert fork_director.model_schema()["properties"]["cut_density"]["maximum"] == 100


def test_the_wire_form_round_trips_to_the_same_schema():
    """`gui.py` serialises nothing of its own, so the string form has one owner too."""
    assert json.loads(fork_director.model_schema_json()) == fork_director.model_schema()


# ===========================================================================
# 2. PROMPTS
# ===========================================================================


def test_the_system_prompt_defines_all_six_controls():
    prompt = fork_director.system_prompt()
    for name in FIELDS:
        assert f"{name}:" in prompt, f"{name} has no semantic definition in the prompt"


def test_the_system_prompt_states_the_range_and_the_neutral_value():
    prompt = fork_director.system_prompt()
    assert "integers from 0 to 100" in prompt
    assert "50 is neutral" in prompt
    assert str(fork_creative.DEFAULT_CONTROL) in prompt


@pytest.mark.parametrize("name, required", [
    ("cut_density", ("fewer", "denser")),
    ("micro_cuts", ("half-beat", "no micro-cut accent layer")),
    ("semantic_emphasis", ("already-analysed", "never changes how the videos were analysed")),
    ("energy_response", ("generic visual flow", "segment")),
    ("motion_bias", ("steadier", "dynamic")),
    ("source_diversity", ("reuses", "spreads")),
])
def test_each_control_description_states_this_repositorys_actual_semantics(name, required):
    """Describing a control with behaviour the implementation does not have is worse than
    describing it vaguely: the model would confidently answer a question BeatSync is not asking.

    In particular Semantic Emphasis must read as a *Stage-6 re-weighting of an already-analysed
    library* — not as a change to Stage-5 tagging and not as "AI off".
    """
    prompt = fork_director.system_prompt()
    line = next(item for item in prompt.splitlines() if item.startswith(f"{name}:"))
    for fragment in required:
        assert fragment in line, f"{name} description is missing {fragment!r}"


def test_the_prompt_never_implies_stage_5_reanalysis_or_media_inspection():
    """`DIRECTOR_INSPECTS_MEDIA = NO`, including in what the model is told it can see."""
    prompt = fork_director.system_prompt().lower()
    for forbidden in ("frame", "mmproj", "thumbnail", "cache", "stage 5", "stage5",
                      "re-analyse", "reanalyse", "re-analyze", "tagging", "qwen",
                      "source file", "filename", "tempo", "beat grid detection"):
        assert forbidden not in prompt, f"the prompt mentions {forbidden!r}"


def test_the_prompt_tells_the_model_it_does_not_choose_the_seed_or_any_resource():
    prompt = fork_director.system_prompt()
    assert "You do not choose the clip variation seed" in prompt
    for resource in ("audio levels", "source videos", "output setting"):
        assert resource in prompt, resource


def test_the_prompt_demands_json_only():
    prompt = fork_director.system_prompt()
    assert "one JSON object only" in prompt
    assert "no code" in prompt


def test_the_system_prompt_is_pure_ascii():
    """It is passed as a process argument to a native binary, so a decorative em dash is a
    mojibake risk for no gain. The UI copy is free to use them; the prompt is not."""
    prompt = fork_director.system_prompt()
    offenders = sorted({c for c in prompt if ord(c) > 127})
    assert offenders == [], f"non-ASCII in the system prompt: {offenders}"


def test_the_user_prompt_is_the_instruction_and_nothing_else():
    """`DIRECTOR_READS_CURRENT_SLIDERS = NO`, and the Director is media-blind: there is nowhere in
    the user prompt for a slider value, a filename or a music feature to appear."""
    rendered = fork_director.user_prompt("calm and slow")
    assert rendered == "Editing intention: calm and slow"


def test_the_user_prompt_builder_takes_only_the_instruction():
    signature = fork_director.user_prompt.__code__
    assert signature.co_varnames[:signature.co_argcount] == ("instruction",)


# ===========================================================================
# 3. STRICT PARSING OF THE MODEL'S STDOUT
# ===========================================================================


def test_a_valid_payload_is_accepted_with_a_blank_explanation():
    parsed = fork_director.parse_model_payload(payload())

    assert parsed is not None
    assert {name: parsed[name] for name in FIELDS} == VALID
    assert parsed[fork_director.EXPLANATION_KEY] == ""


def test_the_real_measured_model_output_parses():
    """The actual wire shape, including llama.cpp's `[end of text]` terminator."""
    parsed = fork_director.parse_model_payload(REAL_STDOUT)

    assert parsed is not None
    assert [parsed[name] for name in FIELDS] == [20, 10, 70, 60, 30, 50]
    assert parsed["explanation"].startswith("Fewer cuts enhance cinematic flow")


def test_the_end_of_generation_marker_is_stripped_but_nothing_else_is():
    """One fixed tool-emitted terminator, removed exactly once from exactly the end.

    It is the *tool's* framing of its own output, not model prose. Deliberately not generalised:
    a marker in the middle, a different suffix or real trailing commentary must still fail, which
    is what keeps this from becoming the regex the strict parse exists to refuse.
    """
    assert fork_director.parse_model_payload(payload() + " [end of text]") is not None
    assert fork_director.parse_model_payload(payload() + "\n[end of text]\n\n") is not None

    assert fork_director.parse_model_payload(payload() + " [end of text] and more") is None
    assert fork_director.parse_model_payload("[end of text] " + payload()) is None
    assert fork_director.parse_model_payload(payload() + " [done]") is None
    assert fork_director.parse_model_payload("[end of text]") is None


@pytest.mark.parametrize("name", FIELDS)
def test_a_missing_control_rejects_the_whole_payload(name):
    body = dict(VALID)
    del body[name]

    assert fork_director.parse_model_payload(json.dumps(body)) is None


def test_an_extra_property_rejects_the_whole_payload():
    """A producer emitting a field this contract has never heard of disagrees with it about what a
    proposal is, which is precisely the disagreement worth failing on."""
    assert fork_director.parse_model_payload(payload(mood="sad")) is None
    assert fork_director.parse_model_payload(payload(sfx_amount=70)) is None
    assert fork_director.parse_model_payload(payload(preset="Cinematic")) is None


def test_a_seed_property_rejects_the_whole_payload():
    """The model must never choose the Variation Seed, so one arriving is a hard failure rather
    than a value to ignore — ignoring it would hide a producer that thinks it owns the seed."""
    assert fork_director.parse_model_payload(payload(seed=424242)) is None
    assert fork_director.parse_model_payload(payload(seed=0)) is None


@pytest.mark.parametrize("value", ["30", "", None, 30.0, 30.5, True, False, [30], {"v": 30},
                                   float("nan"), float("inf")])
def test_a_wrongly_typed_control_rejects_the_whole_payload(value: Any):
    """No coercion, no clamping, no silent default. `"30"` is not 30, `30.0` is not an integer
    answer, and `True` must be rejected first because `bool` subclasses `int`."""
    assert fork_director.parse_model_payload(payload(cut_density=value)) is None


@pytest.mark.parametrize("value", [-1, 101, 1000, -100])
def test_an_out_of_range_control_rejects_the_whole_payload(value: int):
    assert fork_director.parse_model_payload(payload(motion_bias=value)) is None


@pytest.mark.parametrize("value", [0, 1, 50, 99, 100])
def test_every_in_range_endpoint_is_accepted(value: int):
    parsed = fork_director.parse_model_payload(payload(cut_density=value))

    assert parsed is not None and parsed["cut_density"] == value


@pytest.mark.parametrize("raw", [
    "",
    "   \n\t ",
    "not json at all",
    "{",
    '{"cut_density": 30,',
    "```json\n" + json.dumps(VALID) + "\n```",
    "Sure! Here is your recipe:\n" + json.dumps(VALID),
    json.dumps(VALID) + "\nHope that helps!",
    json.dumps([VALID]),
    json.dumps("a string"),
    json.dumps(42),
    "null",
    "true",
])
def test_malformed_prose_fenced_and_truncated_output_all_fail(raw: str):
    """No regex fishes a `{...}` out of surrounding text. A broad `\\{.*\\}` search is how a
    truncated object or a chatty preamble gets half-accepted, and Stage 5's own documented
    truncation defect lived exactly there."""
    assert fork_director.parse_model_payload(raw) is None


@pytest.mark.parametrize("value", [None, 42, b"{}", ["{}"], {"a": 1}])
def test_a_non_string_stdout_is_rejected_rather_than_raising(value: Any):
    assert fork_director.parse_model_payload(value) is None


def test_surrounding_whitespace_alone_is_tolerated():
    assert fork_director.parse_model_payload("\n\n  " + payload() + "  \n\n") is not None


def test_the_parser_returns_a_fresh_dict():
    first = fork_director.parse_model_payload(payload())
    first["cut_density"] = 999
    second = fork_director.parse_model_payload(payload())

    assert second["cut_density"] == VALID["cut_density"]


# ===========================================================================
# 4. THE EXPLANATION IS TOLERANT, NON-LOAD-BEARING AND BOUNDED
# ===========================================================================


def test_a_missing_explanation_is_tolerated():
    parsed = fork_director.parse_model_payload(payload())

    assert parsed is not None
    assert parsed[fork_director.EXPLANATION_KEY] == ""


@pytest.mark.parametrize("value", [None, 42, 1.5, True, [], {}, ["text"]])
def test_a_non_string_explanation_is_tolerated_as_blank(value: Any):
    """Strictness pointed at the one field where it buys nothing would fail a perfectly good
    recipe because the prose was the wrong type."""
    parsed = fork_director.parse_model_payload(payload(explanation=value))

    assert parsed is not None
    assert {name: parsed[name] for name in FIELDS} == VALID
    assert parsed[fork_director.EXPLANATION_KEY] == ""


def test_an_overlong_explanation_is_bounded_rather_than_rejected():
    parsed = fork_director.parse_model_payload(payload(explanation="x" * 5000))

    assert parsed is not None
    assert len(parsed[fork_director.EXPLANATION_KEY]) == fork_director.EXPLANATION_MAX_CHARS


def test_the_explanation_is_whitespace_collapsed_for_display():
    parsed = fork_director.parse_model_payload(
        payload(explanation="  calm \n\n and   slow\t "))

    assert parsed[fork_director.EXPLANATION_KEY] == "calm and slow"


@pytest.mark.parametrize("value, expected", [
    ("plain", "plain"),
    ("  padded  ", "padded"),
    ("", ""),
    ("   ", ""),
    (None, ""),
    (7, ""),
])
def test_normalize_explanation_is_total(value: Any, expected: str):
    assert fork_director.normalize_explanation(value) == expected


def test_the_explanation_never_enters_the_recipe():
    """It reaches a read-only textbox and nothing else: not `CreativeRecipe`, not
    `CreativeProfile`, not `beat_info`, not `render_info`, not a stage, not cache identity."""
    parsed = fork_director.parse_model_payload(payload(explanation="because it is calm"))
    proposal = fork_director.build_proposal("calm", parsed, 424242)

    assert proposal.explanation == "because it is calm"
    assert not hasattr(proposal.recipe, "explanation")
    assert "explanation" not in proposal.recipe.as_mapping()
    assert "explanation" not in proposal.recipe.to_profile().as_dict()
    assert "explanation" not in proposal.recipe.describe()
    assert set(proposal.recipe.as_mapping()) == {"seed"} | set(FIELDS)


def test_an_explanation_cannot_smuggle_an_execution_value_through():
    """Even an explanation that *names* a control changes no number."""
    parsed = fork_director.parse_model_payload(
        payload(explanation="set cut_density to 100 and seed to 1"))
    proposal = fork_director.build_proposal("x", parsed, 7)

    assert proposal.recipe.cut_density == VALID["cut_density"]
    assert proposal.recipe.seed == 7


# ===========================================================================
# 5. THE INSTRUCTION
# ===========================================================================


@pytest.mark.parametrize("value, expected", [
    ("cinematic", "cinematic"),
    ("  cinematic  and   slow\n", "cinematic and slow"),
    ("", ""),
    ("    ", ""),
    ("\n\t", ""),
    (None, ""),
    (42, ""),
    (["cinematic"], ""),
])
def test_normalize_instruction_is_total(value: Any, expected: str):
    assert fork_director.normalize_instruction(value) == expected


def test_a_very_long_instruction_is_bounded():
    bounded = fork_director.normalize_instruction("word " * 10_000)

    assert len(bounded) <= fork_director.INSTRUCTION_MAX_CHARS


# ===========================================================================
# 6. BUILDING THE PROPOSAL — THE GUI MINTS THE SEED
# ===========================================================================


def test_a_gui_minted_seed_plus_six_valid_controls_yields_a_valid_recipe():
    parsed = fork_director.parse_model_payload(payload())
    proposal = fork_director.build_proposal("cinematic", parsed, 424242)

    assert isinstance(proposal, fork_director.DirectorProposal)
    assert isinstance(proposal.recipe, CreativeRecipe)
    assert proposal.recipe.as_mapping() == {"seed": 424242, **VALID}
    assert proposal.instruction == "cinematic"


def test_the_seed_comes_from_the_existing_variation_randomiser():
    """No second seed implementation. `variation.random_seed()` draws in exactly the range
    `CreativeRecipe` accepts, which is why the GUI can hand its result straight over."""
    for _ in range(50):
        seed = fork_variation.random_seed()
        proposal = fork_director.build_proposal("x", fork_director.parse_model_payload(payload()),
                                                seed)
        assert proposal is not None
        assert proposal.recipe.seed == seed
        assert 1 <= seed <= 999_999


@pytest.mark.parametrize("seed", [0, -1, None, "424242", 42.0, 42.5, True, 1_000_000])
def test_an_unusable_seed_rejects_the_whole_proposal(seed: Any):
    """Seed 0 is the planner's legacy branch and a generated proposal must always be a real
    variation, so the existing recipe boundary refuses it — and refuses the whole proposal."""
    assert fork_director.build_proposal("x", fork_director.parse_model_payload(payload()),
                                        seed) is None


@pytest.mark.parametrize("bad", [None, {}, "payload", 42, {"cut_density": 30},
                                 dict(VALID, cut_density="30"),
                                 dict(VALID, cut_density=101)])
def test_an_invalid_execution_mapping_produces_no_proposal(bad: Any):
    """No partial application: three model values and three silent 50s would look deliberate."""
    assert fork_director.build_proposal("x", bad, 424242) is None


def test_build_proposal_goes_through_the_existing_recipe_boundary(monkeypatch):
    """`CreativeRecipe.from_mapping` is the authority, reused unmodified — not re-implemented and
    not weakened. Asserted by observing that the Director's answer follows it."""
    seen: list = []
    original = CreativeRecipe.from_mapping

    def spy(mapping):
        seen.append(dict(mapping))
        return original(mapping)

    monkeypatch.setattr(fork_director.CreativeRecipe, "from_mapping", staticmethod(spy))
    fork_director.build_proposal("x", fork_director.parse_model_payload(payload()), 424242)

    assert len(seen) == 1
    assert seen[0] == {"seed": 424242, **VALID}
    assert set(seen[0]) == {"seed"} | set(FIELDS), "exactly the seven recipe keys, nothing else"


def test_the_existing_strict_recipe_contract_is_unweakened():
    """Restated here because the Director is the untrusted producer it was written for."""
    good = {"seed": 5, **VALID}
    assert CreativeRecipe.from_mapping(good) is not None

    for mutation in (
        {k: v for k, v in good.items() if k != "cut_density"},   # missing execution field
        dict(good, extra=1),                                     # extra execution field
        dict(good, cut_density="30"),                            # wrong type
        dict(good, cut_density=True),                            # bool
        dict(good, cut_density=30.0),                            # float
        dict(good, cut_density=101),                             # out of range
        dict(good, seed=0),                                      # seed 0
    ):
        assert CreativeRecipe.from_mapping(mutation) is None, mutation


# ===========================================================================
# 7. DirectorProposal IS A FROZEN, DEEPCOPY-SAFE RECORD
# ===========================================================================


def proposal(**overrides) -> fork_director.DirectorProposal:
    parsed = fork_director.parse_model_payload(payload(**overrides))
    built = fork_director.build_proposal("cinematic and emotional", parsed, 424242)
    assert built is not None
    return built


def test_the_proposal_carries_exactly_three_fields():
    assert [f.name for f in dataclasses.fields(fork_director.DirectorProposal)] == [
        "recipe", "explanation", "instruction"]


def test_the_proposal_is_frozen():
    item = proposal()
    for name, value in (("recipe", None), ("explanation", "x"), ("instruction", "y")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(item, name, value)


def test_the_proposal_is_deepcopy_safe():
    """`gr.State` deep-copies its value, so this is a real constraint rather than tidiness."""
    item = proposal(explanation="calm")

    assert copy.deepcopy(item) == item
    assert copy.deepcopy(item) is not item
    assert copy.deepcopy(item).recipe == item.recipe


def test_every_value_reachable_from_a_proposal_is_a_plain_type():
    """No `MappingProxyType`, no model object, no process handle, no filesystem object, no source
    state, no `VariantLabConfig`/`AudioVariantConfig`, no cache object — the reachable graph is
    walked rather than the top level inspected."""
    allowed = (int, str, bool, float, type(None), tuple, list, dict,
               fork_director.DirectorProposal, CreativeRecipe)
    seen: list = []
    pending = [proposal(explanation="calm")]
    while pending:
        value = pending.pop()
        assert isinstance(value, allowed), f"forbidden {type(value)!r} reachable from a proposal"
        assert not isinstance(value, MappingProxyType)
        seen.append(value)
        if dataclasses.is_dataclass(value):
            pending.extend(getattr(value, f.name) for f in dataclasses.fields(value))
        elif isinstance(value, (tuple, list)):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
    assert len(seen) > 10, "the walk did not reach the recipe's integers"


def test_two_proposals_from_the_same_answer_are_equal():
    assert proposal() == proposal()
    assert proposal(cut_density=31) != proposal()


def test_the_proposal_read_out_is_formatted_by_the_proposal_itself():
    """One formatter per read-out, exactly as the Variant Lab and mix reports work; `gui.py`
    formats none of it. The numbers come from the recipe's own `describe()`, so the Director does
    not restate them in a second format that could drift."""
    item = proposal(explanation="calm and slow")
    text = item.display_text()

    assert item.recipe.describe() in text
    assert "cinematic and emotional" in text
    assert "calm and slow" in text
    assert "Apply Proposal" in text
    assert "Nothing has been rendered" in text


def test_the_read_out_omits_an_absent_explanation_rather_than_printing_a_blank_line():
    text = proposal().display_text()

    assert "Director:" not in text
    assert "" not in text.splitlines()


def test_the_read_out_never_claims_the_instruction_reproduces_a_result():
    """A prompt is not a recipe identifier: the Variation Seed is minted fresh every time, and the
    read-out must not imply otherwise."""
    text = proposal(explanation="calm").display_text().lower()
    for overclaim in ("reproduc", "deterministic", "guarantee", "identical", "same edit",
                      "best", "perfect", "fully automatic", "understands your footage"):
        assert overclaim not in text, f"the read-out claims {overclaim!r}"


# ===========================================================================
# 8. STATUS TEXT
# ===========================================================================


@pytest.mark.parametrize("status", [
    fork_director.STATUS_NO_INSTRUCTION,
    fork_director.STATUS_INVALID_PAYLOAD,
    fork_director.STATUS_TIMEOUT,
    fork_director.STATUS_NOTHING_TO_APPLY,
])
def test_every_failure_status_says_nothing_changed(status: str):
    """A Director error must never read as a quiet partial success."""
    assert "chang" in status.lower()
    assert status.strip() == status and status


def test_the_timeout_status_names_the_bound():
    assert str(fork_director.TIMEOUT_SECONDS) in fork_director.STATUS_TIMEOUT
    assert fork_director.TIMEOUT_SECONDS == 60


def test_the_runtime_failure_statuses_name_what_failed():
    assert "C:\\nope\\llama.exe" in fork_director.missing_runtime_status("C:\\nope\\llama.exe")
    assert "boom" in fork_director.launch_failure_status("boom")
    assert "3221225477" in fork_director.exit_failure_status(3221225477)
    for status in (fork_director.missing_runtime_status("x"),
                   fork_director.launch_failure_status("x"),
                   fork_director.exit_failure_status(1)):
        assert "no control changed" in status


def test_a_bounded_stderr_tail_rides_along_without_the_argv():
    status = fork_director.exit_failure_status(1, "ggml error\n" * 400)

    assert len(status) < 600, "a megabyte of stderr must not reach the status box"
    assert "ggml error" in status


@pytest.mark.parametrize("detail", [None, 42, [], {"a": 1}])
def test_a_non_string_stderr_tail_is_simply_omitted(detail: Any):
    status = fork_director.exit_failure_status(7, detail)

    assert "7" in status
    assert "None" not in status


def test_the_ready_and_applied_statuses_report_the_seed_and_promise_no_render():
    item = proposal()
    for status in (fork_director.ready_status(item), fork_director.applied_status(item)):
        assert str(item.recipe.seed) in status
        assert "render" in status.lower()
    assert "Apply Proposal" in fork_director.ready_status(item)
    assert "nothing has been rendered" in fork_director.applied_status(item).lower()


# ===========================================================================
# 9. MODULE PURITY
# ===========================================================================


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _strip_docstrings(node):
    node = copy.deepcopy(node)
    for inner in ast.walk(node):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return ast.fix_missing_locations(node)


def _executable_source(path: str) -> str:
    return ast.unparse(_strip_docstrings(_tree(path)))


def test_the_director_module_imports_only_stdlib_and_pure_fork_modules():
    imported: set[str] = set()
    for node in ast.walk(_tree(_DIRECTOR)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported <= {"__future__", "collections", "dataclasses", "json", "typing",
                        "beatsync_fork"}, sorted(imported)


def test_the_director_module_owns_no_subprocess_path_clock_or_randomness():
    """It decides; `gui.py` performs every side effect. Executable source only — the docstring
    legitimately names `llama-cli`, `llama-server` and the GUI in order to say what lives where."""
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("subprocess", "popen", "os.path", "open(", "gradio", "gr.",
                      "random", "time.", "datetime", "llama", "mmproj", "cupy", "cv2",
                      "numpy", "librosa", "logger", "paths", "video_analysis", "auto_mode",
                      "stage5", "qwen_progress", "threading", "hashlib", "environ"):
        assert forbidden not in source, f"director.py references {forbidden!r}"


def test_the_director_module_names_no_pipeline_render_or_cache_machinery():
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("create_music_video", "process_video", "analyze_beats_auto",
                      "beat_info", "render_info", "cache", "stage4_select", "stage6_av_planner",
                      "variant_lab", "variant_batch", "render_batch", "audio_mix", "smart_mix",
                      "audiorecipe", "music_under_voice", "sfx_amount", "sfx_level"):
        assert forbidden not in source, f"director.py references {forbidden!r}"


def test_the_director_module_declares_no_resource_identity():
    """`DIRECTOR_WRITES_RESOURCE_IDENTITY = NO`, asserted at the vocabulary level too."""
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("voice_files", "avoid_drops", "sfx_folder", "sfx_roles", "source_folder",
                      "source_state", "prep_state", "output_filename", "custom_fps",
                      "processing_mode", "encoder", "fps"):
        assert forbidden not in source, f"director.py references {forbidden!r}"


def test_the_director_has_no_cache_history_or_conversation_state():
    """`DIRECTOR_CACHE = NONE`. Every Generate Proposal is independent."""
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("history", "conversation", "session", "_last_", "memo", "lru_cache",
                      "cache_contract_version", "analysis_version"):
        assert forbidden not in source, f"director.py references {forbidden!r}"


def test_the_director_module_has_no_module_level_mutable_state():
    """A proposal must not be able to leak into the next one through a module global."""
    for node in _tree(_DIRECTOR).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            rendered = ast.unparse(node)
            assert not rendered.endswith("= []"), rendered
            assert not rendered.endswith("= {}"), rendered
            assert "set()" not in rendered, rendered


# ===========================================================================
# 10. THE GUI RUNTIME SEAM
#
# `gui.py` cannot be imported on a bare interpreter, so the REAL handler bodies are extracted and
# executed against a synthesised namespace. Only the runtime edge is substituted: the three path
# constants, a `gr.skip()` sentinel and a `subprocess` shim. Every policy decision below is
# production code.
# ===========================================================================


_GUI_FUNCTIONS = ("_director_apply_skips", "_director_apply_outputs", "_run_director_model",
                  "_on_generate_director_proposal", "_on_apply_director_proposal")


class Skip:
    """Stands in for `gr.skip()`; identity is what the assertions check."""


class FakeGr:
    @staticmethod
    def skip():
        return Skip()


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class SubprocessShim:
    """The real `subprocess` module's constants, with a scripted `run`.

    A controlled seam rather than a real child process, and deliberately so: the outcomes that
    matter here are a timeout, a non-zero exit, an `OSError` at launch and a malformed payload, and
    scripting them is both portable (the suite must run on any CPython) and exact. The *argv* this
    produces is asserted against the measured-working shape, and the real binary is exercised by
    the out-of-repository acceptance run.
    """

    TimeoutExpired = subprocess.TimeoutExpired
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list = []

    def run(self, command, **kwargs):
        self.calls.append({"command": list(command), **kwargs})
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def load_gui(tmp_path, outcome, *, exe_exists=True, model_exists=True):
    """The real Director handlers from `src/gui.py`, over a controlled runtime edge."""
    llama_dir = tmp_path / "bin" / "llama-bin-win-vulkan-x64"
    models = tmp_path / "bin" / "models"
    llama_dir.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    exe = llama_dir / "llama-completion.exe"
    model = models / "Qwen3VL-2B-Instruct-Q8_0.gguf"
    if exe_exists:
        exe.write_bytes(b"MZ")
    if model_exists:
        model.write_bytes(b"GGUF")

    shim = SubprocessShim(outcome)
    nodes = [n for n in _tree(_GUI).body
             if isinstance(n, ast.FunctionDef) and n.name in _GUI_FUNCTIONS]
    assert len(nodes) == len(_GUI_FUNCTIONS), [n.name for n in nodes]

    namespace = {
        "os": os, "subprocess": shim, "gr": FakeGr, "Tuple": tuple,
        "fork_director": fork_director, "fork_presets": fork_presets,
        "fork_variation": fork_variation,
        "DIRECTOR_LLAMA_DIR": str(llama_dir),
        "DIRECTOR_LLAMA_EXE": str(exe),
        "DIRECTOR_MODEL": str(model),
        "_DIRECTOR_APPLY_OUTPUT_COUNT": 2 + len(FIELDS),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)
    namespace["_shim"] = shim
    return namespace


# --- Generate Proposal -----------------------------------------------------


def test_generate_turns_a_valid_answer_into_a_proposal(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))

    state, read_out, status = gui["_on_generate_director_proposal"]("cinematic and emotional")

    assert isinstance(state, fork_director.DirectorProposal)
    assert [getattr(state.recipe, f) for f in FIELDS] == [20, 10, 70, 60, 30, 50]
    assert 1 <= state.recipe.seed <= 999_999
    assert read_out == state.display_text()
    assert status == fork_director.ready_status(state)


def test_generate_passes_the_measured_argument_shape(tmp_path):
    """`MODEL_ASSETS_REUSED = YES`, `STAGE5_WORKER_REUSED = NO`, text-only and one-shot.

    The flags are measured rather than remembered: `-no-cnv` skips the chat template and collapses
    the answer to zeros on this Instruct model, while `llama-cli` rejects that flag and prints its
    banner into stdout. Hence `llama-completion.exe` with `-cnv -st`.
    """
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    gui["_on_generate_director_proposal"]("cinematic")

    call = gui["_shim"].calls[0]
    command = call["command"]

    assert command[0].endswith("llama-completion.exe")
    assert "llama-cli.exe" not in command[0]
    assert command[1] == "-m" and command[2].endswith("Qwen3VL-2B-Instruct-Q8_0.gguf")
    for flag in ("-cnv", "-st", "--no-display-prompt", "--no-perf"):
        assert flag in command, flag
    assert "-no-cnv" not in command, "raw completion mode skips the chat template"
    assert command[command.index("-n") + 1] == str(fork_director.MAX_NEW_TOKENS)
    assert command[command.index("-c") + 1] == str(fork_director.CONTEXT_TOKENS)
    assert command[command.index("-sys") + 1] == fork_director.system_prompt()
    assert command[command.index("-p") + 1] == fork_director.user_prompt("cinematic")
    assert command[command.index("--json-schema") + 1] == fork_director.model_schema_json()

    # media-blind, and no Stage-5 asset
    for forbidden in ("--mmproj", "--image", "--audio", "-mmproj", "--port", "--host"):
        assert forbidden not in command, forbidden
    assert not any("mmproj" in str(part) for part in command)

    # bounded, windowless, captured separately
    assert call["timeout"] == fork_director.TIMEOUT_SECONDS == 60
    assert call["capture_output"] is True
    assert call["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_generate_never_mentions_the_stage_5_worker_or_a_server(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    gui["_on_generate_director_proposal"]("cinematic")

    rendered = " ".join(str(p) for p in gui["_shim"].calls[0]["command"]).lower()
    for forbidden in ("stage5_qwen_scene_worker", "llama-server", "llama-mtmd-cli",
                      "qwen_progress", "video_analysis_cache"):
        assert forbidden not in rendered, forbidden


def test_generate_is_a_single_bounded_invocation(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    gui["_on_generate_director_proposal"]("cinematic")
    gui["_on_generate_director_proposal"]("cinematic")

    assert len(gui["_shim"].calls) == 2, "each press is independent; nothing is cached or reused"
    assert all(call["timeout"] == 60 for call in gui["_shim"].calls)


def test_generate_mints_the_seed_only_after_the_payload_validates(tmp_path, monkeypatch):
    """**The ordering is the contract.** An invalid response must not consume a draw and must not
    produce a plausible-looking half proposal."""
    draws: list = []

    def counted():
        draws.append(1)
        return 424242

    monkeypatch.setattr(fork_variation, "random_seed", counted)

    gui = load_gui(tmp_path, FakeCompleted(stdout='{"cut_density": 30}'))
    state, _read_out, status = gui["_on_generate_director_proposal"]("cinematic")
    assert state is None
    assert status == fork_director.STATUS_INVALID_PAYLOAD
    assert draws == [], "a seed was minted for an answer that never validated"

    gui = load_gui(tmp_path, FakeCompleted(stdout=payload()))
    state, _read_out, _status = gui["_on_generate_director_proposal"]("cinematic")
    assert state.recipe.seed == 424242
    assert len(draws) == 1, "exactly one draw per valid proposal"


@pytest.mark.parametrize("instruction", ["", "   ", None, 42])
def test_generate_refuses_an_empty_instruction_without_launching_anything(tmp_path, instruction):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))

    state, read_out, status = gui["_on_generate_director_proposal"](instruction)

    assert state is None and read_out == ""
    assert status == fork_director.STATUS_NO_INSTRUCTION
    assert gui["_shim"].calls == [], "no process may be started for an empty instruction"


@pytest.mark.parametrize("outcome, expected", [
    (FakeCompleted(stdout="not json"), fork_director.STATUS_INVALID_PAYLOAD),
    (FakeCompleted(stdout=""), fork_director.STATUS_INVALID_PAYLOAD),
    (FakeCompleted(stdout="```json\n{}\n```"), fork_director.STATUS_INVALID_PAYLOAD),
    (FakeCompleted(stdout=json.dumps(dict(VALID, seed=7))),
     fork_director.STATUS_INVALID_PAYLOAD),
    (FakeCompleted(stdout=json.dumps(dict(VALID, cut_density=101))),
     fork_director.STATUS_INVALID_PAYLOAD),
    (subprocess.TimeoutExpired(cmd="llama", timeout=60), fork_director.STATUS_TIMEOUT),
])
def test_every_generate_failure_clears_the_state_and_reports_it(tmp_path, outcome, expected):
    """**§40.** The state must never be replaced with a fake valid object, and a failed Generate
    must not leave the previous proposal behind a status line that contradicts it."""
    gui = load_gui(tmp_path, outcome)

    state, read_out, status = gui["_on_generate_director_proposal"]("cinematic")

    assert state is None, "a failure left something applyable in the proposal state"
    assert not isinstance(state, fork_director.DirectorProposal)
    assert read_out == ""
    assert status == expected
    assert "chang" in status.lower()


def test_a_non_zero_exit_reports_the_code_and_a_bounded_stderr_tail(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(returncode=3, stdout=payload(),
                                           stderr="vulkan: device lost\n" * 500))

    state, read_out, status = gui["_on_generate_director_proposal"]("cinematic")

    assert state is None and read_out == ""
    assert "3" in status and "device lost" in status
    assert len(status) < 600


def test_a_launch_failure_is_reported_rather_than_raised(tmp_path):
    gui = load_gui(tmp_path, OSError(8, "Exec format error"))

    state, _read_out, status = gui["_on_generate_director_proposal"]("cinematic")

    assert state is None
    assert "Exec format error" in status
    assert "no control changed" in status


@pytest.mark.parametrize("exe_exists, model_exists, missing", [
    (False, True, "llama-completion.exe"),
    (True, False, "Qwen3VL-2B-Instruct-Q8_0.gguf"),
])
def test_a_missing_executable_or_model_is_caught_before_any_launch(
        tmp_path, exe_exists, model_exists, missing):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT),
                   exe_exists=exe_exists, model_exists=model_exists)

    state, read_out, status = gui["_on_generate_director_proposal"]("cinematic")

    assert state is None and read_out == ""
    assert missing in status
    assert gui["_shim"].calls == [], "a launch was attempted with a missing asset"


def test_the_runner_leaves_no_child_process_behind(tmp_path):
    """`subprocess.run` with a `timeout` kills and reaps the child before raising, which is why
    the Director uses `run` rather than a `Popen` the handler would have to police itself. Pinned
    structurally: there is no `Popen` anywhere in the Director's call graph."""
    gui = load_gui(tmp_path, subprocess.TimeoutExpired(cmd="llama", timeout=60))
    state, _read_out, status = gui["_on_generate_director_proposal"]("cinematic")

    assert state is None and status == fork_director.STATUS_TIMEOUT

    body = ast.unparse(_strip_docstrings(_gui_func("_run_director_model")))
    assert "subprocess.run(" in body
    for forbidden in ("Popen", "terminate(", "kill(", "wait(", "communicate("):
        assert forbidden not in body, f"the Director runner uses {forbidden}"
    assert "timeout=fork_director.TIMEOUT_SECONDS" in body


def test_generate_writes_no_execution_value_at_all(tmp_path):
    """`GENERATING_IS_NOT_APPLYING = YES`, at the handler level: it returns exactly three values,
    and none of them is a widget value."""
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    result = gui["_on_generate_director_proposal"]("cinematic")

    assert len(result) == 3
    state, read_out, status = result
    assert isinstance(state, fork_director.DirectorProposal)
    assert isinstance(read_out, str) and isinstance(status, str)
    assert not any(isinstance(value, int) for value in result)


# --- Apply Proposal --------------------------------------------------------


def test_apply_writes_the_seed_the_six_sliders_and_the_preset(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    state, _read_out, _status = gui["_on_generate_director_proposal"]("cinematic")

    applied = gui["_on_apply_director_proposal"](state)

    assert len(applied) == 2 + len(FIELDS) + 1
    assert applied[0] == state.recipe.seed
    assert tuple(applied[1:7]) == tuple(getattr(state.recipe, f) for f in FIELDS)
    assert applied[7] == fork_presets.matching_preset(applied[1:7])
    assert applied[8] == fork_director.applied_status(state)


def test_apply_recomputes_the_preset_label_from_the_numbers(tmp_path):
    """`DIRECTOR_PRESET_MATCHING_REUSED = YES`. The model never emits a preset name, and
    programmatic slider writes do not fire `.input()`, so Apply must return the label itself."""
    gui = load_gui(tmp_path, FakeCompleted(stdout=json.dumps(dict.fromkeys(FIELDS, 50))))
    state, _r, _s = gui["_on_generate_director_proposal"]("neutral please")

    assert gui["_on_apply_director_proposal"](state)[7] == fork_presets.BALANCED_PRESET

    gui = load_gui(tmp_path, FakeCompleted(stdout=payload()))
    state, _r, _s = gui["_on_generate_director_proposal"]("something else")
    assert gui["_on_apply_director_proposal"](state)[7] == fork_presets.CUSTOM_PRESET


@pytest.mark.parametrize("name", list(fork_presets.PRESETS))
def test_a_proposal_that_lands_on_a_named_recipe_reads_that_name(tmp_path, name):
    recipe = fork_presets.resolve_preset(name)
    gui = load_gui(tmp_path, FakeCompleted(stdout=json.dumps(recipe)))
    state, _r, _s = gui["_on_generate_director_proposal"]("whatever")

    assert gui["_on_apply_director_proposal"](state)[7] == name


@pytest.mark.parametrize("state", [None, "a proposal", 42, {}, [],
                                   {"seed": 5, "cut_density": 50}])
def test_apply_without_a_real_proposal_changes_nothing(tmp_path, state):
    """A refusal returns `gr.skip()` for every execution widget, so nothing moves."""
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))

    applied = gui["_on_apply_director_proposal"](state)

    assert len(applied) == 2 + len(FIELDS) + 1
    assert all(isinstance(value, Skip) for value in applied[:-1])
    assert applied[-1] == fork_director.STATUS_NOTHING_TO_APPLY


def test_apply_reads_only_the_proposal_state(tmp_path):
    """No live creative base, no stale-declaration gate, no source or audio value."""
    handler = _gui_func("_on_apply_director_proposal")
    assert [a.arg for a in handler.args.args] == ["director_proposal_state"]
    assert handler.args.vararg is None and handler.args.kwarg is None
    assert not handler.args.kwonlyargs


def test_apply_is_repeatable_and_does_not_consume_the_proposal(tmp_path):
    """Director Apply is deliberately not stale-gated: a proposal is an absolute set of seven
    values, as valid now as when it was generated, so the user may re-apply it after manual
    experiments. Variant Lab's Apply gate is a different contract and must not be weakened."""
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    state, _r, _s = gui["_on_generate_director_proposal"]("cinematic")

    first = gui["_on_apply_director_proposal"](state)
    second = gui["_on_apply_director_proposal"](state)

    assert first == second


def test_apply_does_not_reuse_the_variant_lab_projection(tmp_path):
    """**§28.** `_variant_apply_outputs` also carries Variant Lab provenance, the three audio
    levels and the lab report — none of which the Director generates. Only the shared *semantic*
    helper is reused."""
    body = ast.unparse(_strip_docstrings(_gui_func("_director_apply_outputs")))

    assert "fork_presets.matching_preset(values)" in body
    assert "_variant_apply_outputs" not in body
    for forbidden in ("master_seed", "audio_resolution", "AUDIO_CONTROL_FIELDS",
                      "music_under_voice", "sfx_amount", "sfx_level", "describe()",
                      "variant_report", "fork_lab", "fork_batch"):
        assert forbidden not in body, f"the Director projection references {forbidden}"


def test_the_director_handlers_write_no_audio_source_or_batch_value(tmp_path):
    gui = load_gui(tmp_path, FakeCompleted(stdout=REAL_STDOUT))
    state, read_out, status = gui["_on_generate_director_proposal"]("cinematic")
    applied = gui["_on_apply_director_proposal"](state)

    for value in tuple(applied) + (read_out, status):
        assert not isinstance(value, (dict, list, set)), value
    # the whole Apply tuple is the seed, six ints, a preset label and a status string
    assert all(isinstance(v, int) for v in applied[:7])
    assert isinstance(applied[7], str) and isinstance(applied[8], str)


# --- structural: the handlers render nothing --------------------------------


def _gui_func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(_GUI)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in gui.py")


#: Everything the Director's event graph must not be able to reach: the renderer, the pipeline, the
#: shared gate core and the durable promotion. Wider than "the renderer" on purpose — a Director
#: that could reach the *gate* would be able to invalidate or approve a source set.
_RENDER_AND_GATE = frozenset({
    "process_video_guarded", "_process_video_guarded_unlocked", "process_video",
    "_process_video_impl", "analyze_beats_auto", "create_music_video",
    "render_selected_variants_guarded", "resolve_for_render", "live_declaration",
    "confirm_action", "scan_folder_action", "build_mixed_master", "prepare_voice_inputs",
    "prepare_sfx_inputs", "_promote_output_no_replace",
})


def _reachable_names(tree: ast.Module, entry: str) -> tuple[set, set]:
    """Every name referenced from `entry`, transitively through the functions it calls.

    Returns `(referenced, visited)`. **References, not just calls**, and that distinction is
    load-bearing rather than thorough: a mutation proof showed that `_ = create_music_video`
    reaches the renderer perfectly well while appearing in no `ast.Call` node, so a call-only
    walker reported a clean boundary. Attribute leaves are included too, so `mod.create_music_video`
    cannot hide behind a module alias.
    """
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert entry in defined, f"{entry} is not defined at module level in gui.py"

    referenced: set = set()
    visited: set = set()
    pending = [entry]
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        node = defined.get(name)
        if node is None:
            continue
        for inner in ast.walk(node):
            leaf = None
            if isinstance(inner, ast.Name):
                leaf = inner.id
            elif isinstance(inner, ast.Attribute):
                leaf = inner.attr
            elif isinstance(inner, ast.Call):
                leaf = ast.unparse(inner.func).rsplit(".", 1)[-1]
            if leaf is None:
                continue
            referenced.add(leaf)
            if leaf in defined:
                pending.append(leaf)
    return referenced, visited


def test_no_director_handler_can_reach_a_render_entry_point():
    """`DIRECTOR_AUTO_RENDER = NO`, asserted structurally rather than by banned token: the graph is
    walked from both buttons' handlers, so renaming one cannot slip past it."""
    tree = _tree(_GUI)

    for entry in ("_on_generate_director_proposal", "_on_apply_director_proposal"):
        referenced, visited = _reachable_names(tree, entry)
        leaked = sorted(referenced & _RENDER_AND_GATE)
        assert not leaked, f"{entry} reaches {leaked}"
        assert len(visited) > 1, f"{entry} resolved no call graph at all"


def test_the_reachability_walker_would_actually_notice_a_leak():
    """The guard's own calibration: a walker that never fires proves nothing.

    Both shapes are checked, because the call-only version of this walker genuinely missed the
    second one: a plain reference reaches the renderer just as effectively as a call.
    """
    for leak in ("    create_music_video(1)", "    _ = create_music_video"):
        module = ast.parse("def _entry():\n    _helper()\n\n\ndef _helper():\n" + leak + "\n")
        referenced, visited = _reachable_names(module, "_entry")
        assert "create_music_video" in referenced, leak
        assert visited == {"_entry", "_helper"}, "the walker must follow calls transitively"


def test_no_director_handler_touches_source_preparation_or_session_state():
    for name in _GUI_FUNCTIONS:
        body = ast.unparse(_strip_docstrings(_gui_func(name)))
        for forbidden in ("source_state", "prep_state", "session_state", "variant_batch_state",
                          "LAST_OUTPUT_PATH_KEY", "AUDIO_LAYERS_REPORT_KEY",
                          "SMART_MIX_REPORT_KEY", "_RENDER_LOCK", "scan_folder",
                          "CreativeProfile", "beat_info"):
            assert forbidden not in body, f"{name} references {forbidden}"


def test_no_director_handler_touches_a_gradio_component_or_a_global():
    for name in _GUI_FUNCTIONS:
        body = ast.unparse(_strip_docstrings(_gui_func(name)))
        for forbidden in ("gr.Textbox", "gr.Button", "gr.State", "gr.Number", "gr.Slider",
                          "gr.Radio", "gr.update(", "global "):
            assert forbidden not in body, f"{name} references {forbidden}"


def test_the_director_introduced_no_hidden_proposal_state():
    source = _executable_source(_GUI)
    for forbidden in ("_DIRECTOR_CACHE", "_LAST_PROPOSAL", "_director_history",
                      "DIRECTOR_PROPOSAL_KEY", "_proposal_snapshot", "_director_session"):
        assert forbidden not in source, forbidden


def test_the_director_runtime_paths_live_in_the_gui_and_use_the_general_root():
    """`.claude/rules/fork-package.md`'s hard rule from the other side: the pure module owns no
    path, and the GUI derives both from the one general `ROOT_DIR` constant rather than importing
    `video_analysis` or the Stage-5 worker for them."""
    source = _executable_source(_GUI)
    assert ("DIRECTOR_LLAMA_DIR = os.path.join(ROOT_DIR, 'bin', 'llama-bin-win-vulkan-x64')"
            in source)
    assert "DIRECTOR_LLAMA_EXE = os.path.join(DIRECTOR_LLAMA_DIR, 'llama-completion.exe')" in source
    assert ("DIRECTOR_MODEL = os.path.join(ROOT_DIR, 'bin', 'models', "
            "'Qwen3VL-2B-Instruct-Q8_0.gguf')" in source)

    for node in ast.walk(_tree(_GUI)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node)
            assert "stage5_qwen_scene_worker" not in rendered, rendered
            assert "qwen_progress" not in rendered, rendered
