"""AI Director V2: the pure semantic-intent boundary.

The architectural claim V2 rests on is that **the model never speaks BeatSync's control vocabulary**.
That is asserted here statically against the real model-facing surface, not assumed — it is the one
change that made the frozen measurement set pass after three earlier output contracts failed the
same sentence.

Two trust contracts are deliberately opposite and are tested as such: the resolved execution
controls are all-or-nothing, while ``explanation`` is tolerant display text. Conflating them is the
mistake worth naming.

The GUI runtime seam for the Director lives in ``tests/test_gui_guard_seam.py``, which is where the
other ``gui.py`` AST-extraction suites already are; this file stays importable-pure.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import json
import os
import re
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import director as fork_director
from beatsync_fork import library_prep as fork_prep
from beatsync_fork import presets as fork_presets
from beatsync_fork.creative_recipe import CreativeRecipe

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIRECTOR = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "director.py")

#: The frozen P2-R3 evidence hashes. The selected model's whole measurement record — 6/6 energy
#: cluster, 14/14 concepts, 10/10 media-control concepts, 0 wrong-direction, 12/12 unseen holdout —
#: was produced against *these exact bytes*, so production drifting off them silently would
#: invalidate the evidence this feature was authorized on.
FROZEN_PROMPT_SHA256 = "2ef076e1693f08e0ac7a9d4f055fad88cf0ed3f5913b107882c52fd821e58ddd"
FROZEN_SCHEMA_SHA256 = "411615315546af8707127d506375033c48369188b2b985b8df0fbab13889df35"

AXES = ("cut_pacing", "impact_accents", "scene_reading", "section_reactivity",
        "motion_preference", "source_variety")

EXPECTED_DIRECTIONS = {
    "cut_pacing": ("sparser", "denser"),
    "impact_accents": ("fewer", "more"),
    "scene_reading": ("visual", "semantic"),
    "section_reactivity": ("steadier", "responsive"),
    "motion_preference": ("calmer", "dynamic"),
    "source_variety": ("reuse", "diverse"),
}

EXPECTED_CONTROLS = {
    "cut_pacing": "cut_density",
    "impact_accents": "micro_cuts",
    "scene_reading": "semantic_emphasis",
    "section_reactivity": "energy_response",
    "motion_preference": "motion_bias",
    "source_variety": "source_diversity",
}


def _stdout(intent: dict, explanation: str | None = None, marker: bool = True) -> str:
    payload: dict[str, Any] = {"intent": intent}
    if explanation is not None:
        payload["explanation"] = explanation
    text = json.dumps(payload)
    return f"{text} [end of text]" if marker else text


def _axis(axis: str, direction: str, strength: int) -> dict:
    return {axis: {"direction": direction, "strength": strength}}


# ===========================================================================
# 1. THE MODEL-FACING SURFACE CARRIES NO EXECUTION VOCABULARY
# ===========================================================================

def test_the_system_prompt_matches_the_frozen_evidence_bytes():
    actual = hashlib.sha256(fork_director.system_prompt().encode("utf-8")).hexdigest()
    assert actual == FROZEN_PROMPT_SHA256, (
        "the system prompt drifted off the bytes P2-R3 measured; re-run the evidence or revert")


def test_the_model_schema_matches_the_frozen_evidence_bytes():
    actual = hashlib.sha256(fork_director.model_schema_json().encode("utf-8")).hexdigest()
    assert actual == FROZEN_SCHEMA_SHA256, (
        "the model schema drifted off the bytes P2-R3 measured; re-run the evidence or revert")


@pytest.mark.parametrize("control", fork_presets.CREATIVE_CONTROL_FIELDS)
def test_no_internal_control_name_appears_in_the_system_prompt(control: str):
    """The whole architectural distinction of V2. A rename cannot evade this."""
    assert control not in fork_director.system_prompt().lower()


@pytest.mark.parametrize("control", fork_presets.CREATIVE_CONTROL_FIELDS)
def test_no_internal_control_name_appears_in_the_model_schema(control: str):
    assert control not in fork_director.model_schema_json().lower()


def test_the_model_facing_surface_mentions_no_execution_control_in_any_spelling():
    surface = (fork_director.system_prompt() + fork_director.model_schema_json()).lower()
    for control in fork_presets.CREATIVE_CONTROL_FIELDS:
        for spelling in (control, control.replace("_", " "), control.replace("_", "-")):
            assert spelling not in surface, spelling


def test_the_user_prompt_is_the_instruction_and_nothing_else():
    assert fork_director.user_prompt("make it calm") == "Editing intention: make it calm"
    for control in fork_presets.CREATIVE_CONTROL_FIELDS:
        assert control not in fork_director.user_prompt("x").lower()


def test_the_system_prompt_is_pure_ascii():
    """It is a process argument to a native binary, so a decorative dash is a mojibake risk."""
    assert fork_director.system_prompt().isascii()


# ===========================================================================
# 2. THE SIX SEMANTIC AXES
# ===========================================================================

def test_there_are_exactly_six_axes_in_the_documented_order():
    assert tuple(fork_director.SEMANTIC_AXES) == AXES


def test_every_axis_carries_its_own_meaningful_direction_pair():
    for axis, expected in EXPECTED_DIRECTIONS.items():
        assert fork_director.directions(axis) == expected


def test_the_twelve_direction_words_are_all_distinct():
    words = [d for axis in AXES for d in fork_director.directions(axis)]
    assert len(words) == 12 and len(set(words)) == 12


def test_generic_up_and_down_are_not_the_model_facing_vocabulary():
    """The point of axis-specific enums: the model classifies meaning, not slider direction."""
    schema = json.loads(fork_director.model_schema_json())
    for axis in AXES:
        enum = schema["properties"]["intent"]["properties"][axis]["properties"]["direction"]["enum"]
        assert "up" not in enum and "down" not in enum
        assert "neutral" not in enum


def test_each_axis_maps_to_exactly_one_execution_control():
    assert fork_director.AXIS_TO_CONTROL == EXPECTED_CONTROLS
    assert sorted(fork_director.AXIS_TO_CONTROL.values()) == \
        sorted(fork_presets.CREATIVE_CONTROL_FIELDS)
    for control, axis in fork_director.CONTROL_TO_AXIS.items():
        assert fork_director.AXIS_TO_CONTROL[axis] == control


def test_the_control_fields_are_derived_from_the_one_registry():
    assert fork_director.DIRECTOR_CONTROL_FIELDS is fork_presets.CREATIVE_CONTROL_FIELDS


# ===========================================================================
# 3. THE SCHEMA SHAPE
# ===========================================================================

def test_no_axis_is_required_so_an_empty_intent_is_legal():
    schema = fork_director.model_schema()
    assert schema["required"] == ["intent"]
    assert "required" not in schema["properties"]["intent"]


def test_additional_properties_are_forbidden_at_both_levels():
    schema = fork_director.model_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["intent"]["additionalProperties"] is False
    for axis in AXES:
        assert schema["properties"]["intent"]["properties"][axis]["additionalProperties"] is False


def test_each_present_axis_requires_both_direction_and_strength():
    schema = fork_director.model_schema()
    for axis in AXES:
        node = schema["properties"]["intent"]["properties"][axis]
        assert sorted(node["required"]) == ["direction", "strength"]
        strength = node["properties"]["strength"]
        assert strength == {"type": "integer", "minimum": 1, "maximum": 100}


def test_strength_excludes_zero_because_zero_is_indistinguishable_from_omission():
    assert fork_director.STRENGTH_MIN == 1
    assert fork_director.STRENGTH_MAX == 100
    schema = fork_director.model_schema()
    for axis in AXES:
        assert schema["properties"]["intent"]["properties"][axis][
            "properties"]["strength"]["minimum"] == 1


def test_the_schema_has_no_seed_property_and_never_asks_for_one():
    text = fork_director.model_schema_json().lower()
    assert "seed" not in text
    schema = fork_director.model_schema()
    assert "seed" not in schema["properties"]
    assert "seed" not in schema["properties"]["intent"]["properties"]


def test_the_explanation_is_optional_bounded_and_a_string():
    schema = fork_director.model_schema()
    assert schema["properties"]["explanation"] == {
        "type": "string", "maxLength": fork_director.EXPLANATION_MAX_CHARS}
    assert "explanation" not in schema["required"]


def test_the_schema_carries_no_version_machinery():
    text = fork_director.model_schema_json().lower()
    for speculative in ("version", "schema_version", "$schema", "contract"):
        assert speculative not in text


def test_the_schema_is_fresh_each_call_so_a_caller_cannot_poison_it():
    first = fork_director.model_schema()
    first["properties"].pop("intent")
    assert "intent" in fork_director.model_schema()["properties"]


def test_the_wire_form_round_trips_to_the_same_schema():
    assert json.loads(fork_director.model_schema_json()) == fork_director.model_schema()


# ===========================================================================
# 4. THE STRICT PARSER
# ===========================================================================

def test_a_valid_single_axis_answer_parses():
    parsed = fork_director.parse_semantic_intent(
        _stdout(_axis("section_reactivity", "steadier", 80)))
    assert parsed is not None
    intent, explanation = parsed
    assert explanation == ""
    assert [(r.axis, r.direction, r.strength) for r in intent.requests] == \
        [("section_reactivity", "steadier", 80)]


def test_the_real_measured_model_output_parses():
    """Verbatim stdout from the selected 4B model on the A1 sentence P2-R3 measured."""
    raw = ('{"intent": {"section_reactivity": {"direction": "steadier", "strength": 80}}, '
           '"explanation": "The intention to keep scene choice relatively even across sections '
           'implies a consistent scene selection regardless of musical intensity."} [end of text]')
    parsed = fork_director.parse_semantic_intent(raw)
    assert parsed is not None
    intent, explanation = parsed
    assert intent.by_axis()["section_reactivity"].direction == "steadier"
    assert explanation.startswith("The intention to keep scene choice")


def test_an_empty_intent_is_valid_and_means_no_expressed_preference():
    parsed = fork_director.parse_semantic_intent(_stdout({}))
    assert parsed is not None
    intent, _ = parsed
    assert intent.requests == ()
    assert fork_director.resolve_semantic_intent(intent) == \
        {name: 50 for name in fork_presets.CREATIVE_CONTROL_FIELDS}


def test_all_six_axes_may_be_reported_at_once():
    intent_payload: dict[str, Any] = {}
    for axis in AXES:
        intent_payload.update(_axis(axis, EXPECTED_DIRECTIONS[axis][1], 100))
    parsed = fork_director.parse_semantic_intent(_stdout(intent_payload))
    assert parsed is not None
    assert len(parsed[0].requests) == 6


def test_the_end_of_generation_marker_is_stripped_but_nothing_else_is():
    body = _stdout(_axis("cut_pacing", "denser", 50), marker=False)
    assert fork_director.parse_semantic_intent(body) is not None
    assert fork_director.parse_semantic_intent(body + " [end of text]") is not None
    assert fork_director.parse_semantic_intent(body + "\n [end of text] \n") is not None
    # a marker anywhere but the very end is still prose, and still fails
    assert fork_director.parse_semantic_intent("[end of text] " + body) is None
    assert fork_director.parse_semantic_intent(body + " [end of text] trailing") is None


def test_a_missing_intent_key_rejects_the_whole_payload():
    assert fork_director.parse_semantic_intent('{"explanation": "hi"}') is None


@pytest.mark.parametrize("extra", ["seed", "preset", "controls", "cut_density", "audio", "unknown"])
def test_an_extra_top_level_property_rejects_the_whole_payload(extra: str):
    raw = json.dumps({"intent": {}, extra: 1})
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("axis", ["cut_density", "energy_response", "tempo", "", "CUT_PACING"])
def test_an_unknown_axis_rejects_the_whole_payload(axis: str):
    raw = json.dumps({"intent": {axis: {"direction": "denser", "strength": 50}}})
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("axis,bad", [
    ("cut_pacing", "fewer"),            # another axis's word
    ("cut_pacing", "up"),
    ("cut_pacing", "neutral"),
    ("section_reactivity", "calmer"),
    ("motion_preference", "dynamic "),  # whitespace is not the enum value
    ("source_variety", "DIVERSE"),
])
def test_a_direction_from_the_wrong_axis_or_the_wrong_case_rejects_everything(axis, bad):
    raw = json.dumps({"intent": {axis: {"direction": bad, "strength": 50}}})
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("strength", [0, -1, 101, 1000, True, False, 50.0, 50.5, "50", None, [50]])
def test_a_malformed_strength_rejects_the_whole_payload(strength: Any):
    raw = json.dumps({"intent": {"cut_pacing": {"direction": "denser", "strength": strength}}})
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("strength", [1, 2, 49, 50, 51, 99, 100])
def test_every_in_range_strength_endpoint_is_accepted(strength: int):
    raw = json.dumps({"intent": {"cut_pacing": {"direction": "denser", "strength": strength}}})
    parsed = fork_director.parse_semantic_intent(raw)
    assert parsed is not None
    assert parsed[0].requests[0].strength == strength


@pytest.mark.parametrize("spec", [
    {"direction": "denser"},                              # missing strength
    {"strength": 50},                                     # missing direction
    {},                                                   # both missing
    {"direction": "denser", "strength": 50, "extra": 1},  # extra key inside the axis
    "denser",                                             # not an object
    50,
    None,
    ["denser", 50],
])
def test_a_malformed_present_axis_rejects_the_whole_intent_rather_than_being_dropped(spec: Any):
    """A producer that disagrees about what an axis is must fail, not be salvaged."""
    raw = json.dumps({"intent": {"cut_pacing": spec}})
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("raw", [
    "", "   ", "not json", "{", "[]", '["intent"]', "null", "true", "42",
    '{"intent": []}',
    '{"intent": "none"}',
    '```json\n{"intent": {}}\n```',
    'Here is the intent: {"intent": {}}',
    '{"intent": {}} Hope that helps!',
    '{"intent": {"cut_pacing": {"direction": "denser", "strength": 50',
])
def test_malformed_prose_fenced_and_truncated_output_all_fail(raw: str):
    assert fork_director.parse_semantic_intent(raw) is None


@pytest.mark.parametrize("value", [None, 42, b"{}", {"intent": {}}, ["x"]])
def test_a_non_string_stdout_is_rejected_rather_than_raising(value: Any):
    assert fork_director.parse_semantic_intent(value) is None


def test_surrounding_whitespace_alone_is_tolerated():
    body = _stdout({}, marker=False)
    assert fork_director.parse_semantic_intent(f"\n\n  {body}  \n") is not None


def test_one_axis_cannot_be_reported_twice():
    """JSON collapses duplicate keys, so the guard lives on the record itself."""
    with pytest.raises(ValueError):
        fork_director.SemanticIntent(requests=(
            fork_director.SemanticAxisRequest("cut_pacing", "denser", 50),
            fork_director.SemanticAxisRequest("cut_pacing", "sparser", 50),
        ))


# ===========================================================================
# 5. THE EXPLANATION IS TOLERANT AND NON-LOAD-BEARING
# ===========================================================================

def test_a_missing_explanation_is_tolerated():
    parsed = fork_director.parse_semantic_intent(_stdout({}))
    assert parsed is not None and parsed[1] == ""


@pytest.mark.parametrize("value", [None, 42, [], {}, True])
def test_a_non_string_explanation_is_tolerated_as_blank(value: Any):
    raw = json.dumps({"intent": {}, "explanation": value})
    parsed = fork_director.parse_semantic_intent(raw)
    assert parsed is not None and parsed[1] == ""


def test_an_overlong_explanation_is_bounded_rather_than_rejected():
    parsed = fork_director.parse_semantic_intent(_stdout({}, "x" * 5000))
    assert parsed is not None
    assert len(parsed[1]) == fork_director.EXPLANATION_MAX_CHARS


def test_the_explanation_is_whitespace_collapsed_for_display():
    parsed = fork_director.parse_semantic_intent(_stdout({}, "a\n\n b\t\tc  "))
    assert parsed is not None and parsed[1] == "a b c"


# ===========================================================================
# 6. THE DETERMINISTIC SEMANTIC -> CONTROL MAPPING
# ===========================================================================

@pytest.mark.parametrize("strength,expected", [
    (1, 1), (2, 1), (49, 25), (50, 25), (51, 26), (99, 50), (100, 50)])
def test_the_magnitude_is_integer_half_up(strength: int, expected: int):
    assert fork_director.magnitude_for_strength(strength) == expected


def test_the_magnitude_is_not_bankers_rounding():
    """``round(1/2)`` is 0, which would make the smallest possible request a silent no-op."""
    assert round(1 / 2) == 0
    assert fork_director.magnitude_for_strength(1) == 1


def test_the_magnitude_is_monotonic_in_strength():
    previous = 0
    for strength in range(1, 101):
        magnitude = fork_director.magnitude_for_strength(strength)
        assert magnitude >= previous
        previous = magnitude


@pytest.mark.parametrize("bad", [0, -1, 101, True, 50.0, "50", None])
def test_the_magnitude_refuses_an_invalid_strength(bad: Any):
    with pytest.raises(ValueError):
        fork_director.magnitude_for_strength(bad)


def test_an_omitted_axis_leaves_its_control_at_exactly_fifty():
    intent = fork_director.SemanticIntent(
        requests=(fork_director.SemanticAxisRequest("cut_pacing", "denser", 100),))
    resolved = fork_director.resolve_semantic_intent(intent)
    assert resolved["cut_density"] == 100
    for control in fork_presets.CREATIVE_CONTROL_FIELDS:
        if control != "cut_density":
            assert resolved[control] == 50, control


@pytest.mark.parametrize("axis", AXES)
@pytest.mark.parametrize("strength", [1, 2, 49, 50, 51, 99, 100])
def test_both_directions_of_every_axis_land_on_the_right_side_of_neutral(axis: str, strength: int):
    negative, positive = fork_director.directions(axis)
    control = fork_director.AXIS_TO_CONTROL[axis]
    magnitude = fork_director.magnitude_for_strength(strength)

    low = fork_director.resolve_semantic_intent(fork_director.SemanticIntent(
        requests=(fork_director.SemanticAxisRequest(axis, negative, strength),)))
    high = fork_director.resolve_semantic_intent(fork_director.SemanticIntent(
        requests=(fork_director.SemanticAxisRequest(axis, positive, strength),)))

    assert low[control] == 50 - magnitude
    assert high[control] == 50 + magnitude
    assert low[control] < 50 < high[control]


def test_every_resolved_control_is_a_plain_int_in_range():
    for axis in AXES:
        for direction in fork_director.directions(axis):
            for strength in range(1, 101):
                resolved = fork_director.resolve_semantic_intent(fork_director.SemanticIntent(
                    requests=(fork_director.SemanticAxisRequest(axis, direction, strength),)))
                for value in resolved.values():
                    assert isinstance(value, int) and not isinstance(value, bool)
                    assert fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX


def test_the_mapping_is_deterministic():
    intent = fork_director.SemanticIntent(requests=(
        fork_director.SemanticAxisRequest("motion_preference", "dynamic", 100),
        fork_director.SemanticAxisRequest("source_variety", "diverse", 75),
    ))
    first = fork_director.resolve_semantic_intent(intent)
    for _ in range(50):
        assert fork_director.resolve_semantic_intent(intent) == first


def test_the_mapping_returns_a_fresh_dict():
    intent = fork_director.SemanticIntent()
    first = fork_director.resolve_semantic_intent(intent)
    first["cut_density"] = 999
    assert fork_director.resolve_semantic_intent(intent)["cut_density"] == 50


@pytest.mark.parametrize("bad", [None, {}, "steadier", 42, [], fork_director.SemanticAxisRequest])
def test_a_non_intent_resolves_to_nothing(bad: Any):
    assert fork_director.resolve_semantic_intent(bad) is None


@pytest.mark.parametrize("axis,direction,strength", [
    ("nonsense", "denser", 50),
    ("cut_pacing", "sideways", 50),
    ("cut_pacing", "denser", 0),
    ("cut_pacing", "denser", 101),
    ("cut_pacing", "denser", True),
])
def test_the_axis_request_record_refuses_an_invalid_combination(axis, direction, strength):
    with pytest.raises(ValueError):
        fork_director.SemanticAxisRequest(axis, direction, strength)


# ===========================================================================
# 7. THE PROPOSAL: BASE, FINAL AND PROVENANCE
# ===========================================================================

CONCENTRATED = fork_prep.PreparedMediaSummary(
    candidate_moments=309, effective_sources=4.0,
    top_source_share=0.4126, median_moments_per_source=77.0)

DIVERSE_LIBRARY = fork_prep.PreparedMediaSummary(
    candidate_moments=12392, effective_sources=681.3,
    top_source_share=0.01, median_moments_per_source=9.0)


def _diverse_intent(strength: int = 100) -> fork_director.SemanticIntent:
    return fork_director.SemanticIntent(
        requests=(fork_director.SemanticAxisRequest("source_variety", "diverse", strength),))


def test_a_proposal_without_media_has_identical_base_and_final():
    proposal = fork_director.build_proposal("spread it out", _diverse_intent(), "", 4242)
    assert proposal is not None
    assert proposal.base_recipe == proposal.final_recipe
    assert proposal.adjustment is None
    assert proposal.media_adjusted is False


def test_a_proposal_on_a_concentrated_library_keeps_base_and_attenuates_final():
    proposal = fork_director.build_proposal(
        "spread it out", _diverse_intent(), "", 4242, summary=CONCENTRATED)
    assert proposal is not None
    assert proposal.base_recipe.source_diversity == 100
    assert proposal.final_recipe.source_diversity == 67
    assert proposal.media_adjusted is True
    assert proposal.adjustment.field == "source_diversity"


def test_apply_uses_final_not_base():
    proposal = fork_director.build_proposal(
        "spread it out", _diverse_intent(), "", 4242, summary=CONCENTRATED)
    assert proposal.recipe is proposal.final_recipe
    assert proposal.recipe.source_diversity == 67


def test_both_recipes_share_the_one_minted_seed():
    """The media step must not look like it re-rolled the clip selection."""
    proposal = fork_director.build_proposal(
        "spread it out", _diverse_intent(), "", 987654, summary=CONCENTRATED)
    assert proposal.base_recipe.seed == proposal.final_recipe.seed == 987654


def test_a_fully_supported_library_leaves_the_recipe_alone():
    proposal = fork_director.build_proposal(
        "spread it out", _diverse_intent(), "", 4242, summary=DIVERSE_LIBRARY)
    assert proposal.media_adjusted is False
    assert proposal.final_recipe.source_diversity == 100


def test_a_reuse_request_is_never_media_adjusted():
    intent = fork_director.SemanticIntent(
        requests=(fork_director.SemanticAxisRequest("source_variety", "reuse", 100),))
    proposal = fork_director.build_proposal("a few heroes", intent, "", 4242,
                                            summary=CONCENTRATED)
    assert proposal.final_recipe.source_diversity == 0
    assert proposal.media_adjusted is False


def test_no_other_control_is_ever_media_adjusted():
    intent = fork_director.SemanticIntent(requests=tuple(
        fork_director.SemanticAxisRequest(axis, EXPECTED_DIRECTIONS[axis][1], 100)
        for axis in AXES))
    proposal = fork_director.build_proposal("everything up", intent, "", 4242,
                                            summary=CONCENTRATED)
    for control in fork_presets.CREATIVE_CONTROL_FIELDS:
        if control == "source_diversity":
            continue
        assert getattr(proposal.final_recipe, control) == \
            getattr(proposal.base_recipe, control), control


def test_the_seed_is_whatever_the_caller_minted_and_the_module_mints_nothing():
    source = _executable_source(_DIRECTOR)
    assert "random" not in source
    for seed in (1, 42, 999999):
        proposal = fork_director.build_proposal("x", _diverse_intent(), "", seed)
        assert proposal.recipe.seed == seed


@pytest.mark.parametrize("bad_seed", [0, -1, None, "7", 1.5, True])
def test_an_invalid_seed_rejects_the_whole_proposal(bad_seed: Any):
    """The existing strict recipe boundary owns this, and is reused unweakened."""
    assert fork_director.build_proposal("x", _diverse_intent(), "", bad_seed) is None


@pytest.mark.parametrize("bad_intent", [None, {}, "steadier", 42])
def test_an_invalid_intent_produces_no_proposal(bad_intent: Any):
    assert fork_director.build_proposal("x", bad_intent, "", 4242) is None


def test_build_proposal_goes_through_the_existing_recipe_boundary(monkeypatch):
    calls = []
    original = CreativeRecipe.from_mapping

    def spy(mapping):
        calls.append(dict(mapping))
        return original(mapping)

    monkeypatch.setattr(CreativeRecipe, "from_mapping", staticmethod(spy))
    fork_director.build_proposal("x", _diverse_intent(), "", 4242, summary=CONCENTRATED)
    assert len(calls) == 2, "both BASE and FINAL must pass the one trust boundary"
    for mapping in calls:
        assert set(mapping) == {"seed", *fork_presets.CREATIVE_CONTROL_FIELDS}


def test_the_existing_strict_recipe_contract_is_unweakened():
    assert CreativeRecipe.from_mapping({"seed": 1}) is None
    assert CreativeRecipe.from_mapping({"seed": 0, **{n: 50 for n in
                                                      fork_presets.CREATIVE_CONTROL_FIELDS}}) \
        is None


def test_the_proposal_carries_exactly_seven_fields():
    assert tuple(f.name for f in dataclasses.fields(fork_director.DirectorProposal)) == (
        "final_recipe", "base_recipe", "intent", "explanation", "instruction", "adjustment",
        "media_note")


def test_the_proposal_is_frozen():
    proposal = fork_director.build_proposal("x", _diverse_intent(), "", 4242)
    with pytest.raises(dataclasses.FrozenInstanceError):
        proposal.instruction = "other"


def test_the_proposal_is_deepcopy_safe():
    """``gr.State`` deep-copies its value, so this is a contract rather than a nicety."""
    proposal = fork_director.build_proposal(
        "x", _diverse_intent(), "why", 4242, summary=CONCENTRATED,
        media_note=fork_director.MEDIA_NOTE_CHECKED_NO_CHANGE)
    assert copy.deepcopy(proposal) == proposal


def test_every_value_reachable_from_a_proposal_is_a_plain_type():
    proposal = fork_director.build_proposal(
        "x", _diverse_intent(), "why", 4242, summary=CONCENTRATED)
    plain = (int, float, str, bool, type(None))

    def walk(value, path="proposal"):
        if isinstance(value, plain):
            return
        if dataclasses.is_dataclass(value):
            for field in dataclasses.fields(value):
                walk(getattr(value, field.name), f"{path}.{field.name}")
            return
        if isinstance(value, tuple):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
            return
        raise AssertionError(f"{path} is a {type(value).__name__}, not a plain deepcopy-safe value")

    walk(proposal)


# ===========================================================================
# 8. THE READ-OUT
# ===========================================================================

def test_the_read_out_separates_the_director_from_the_media_adjustment():
    proposal = fork_director.build_proposal(
        "Showcase everything I have", _diverse_intent(), "Spreading across many sources.",
        4242, summary=CONCENTRATED)
    text = proposal.display_text()
    assert "Base:" in text
    assert "Director:" in text
    assert "Media adjustment:" in text
    assert "Final:" in text
    # the model is never credited with the deterministic change
    director_part = text.split("Media adjustment:")[0]
    assert "effective source" not in director_part


def test_the_read_out_states_the_tradeoff_whenever_it_reports_an_adjustment():
    proposal = fork_director.build_proposal(
        "Showcase everything", _diverse_intent(), "", 4242, summary=CONCENTRATED)
    text = proposal.display_text().lower()
    assert "adjacent source reuse" in text
    for overclaim in ("better", "optimal", "pareto", "perfect", "best edit"):
        assert overclaim not in text, overclaim


def test_an_unadjusted_proposal_reads_as_an_ordinary_recipe():
    proposal = fork_director.build_proposal("x", _diverse_intent(), "", 4242)
    text = proposal.display_text()
    assert "Media adjustment:" not in text
    assert "Base:" not in text


def test_the_media_note_is_shown_when_no_adjustment_happened():
    proposal = fork_director.build_proposal(
        "x", _diverse_intent(), "", 4242, media_note=fork_director.MEDIA_NOTE_NO_SCAN)
    assert fork_director.MEDIA_NOTE_NO_SCAN in proposal.display_text()


def test_the_read_out_omits_an_absent_explanation_rather_than_printing_a_blank_line():
    text = fork_director.build_proposal("x", _diverse_intent(), "", 4242).display_text()
    assert "Director:" not in text
    assert "\n\n" not in text


def test_the_read_out_never_claims_the_instruction_reproduces_a_result_or_that_media_was_watched():
    proposal = fork_director.build_proposal(
        "cinematic", _diverse_intent(), "because", 4242, summary=CONCENTRATED)
    text = proposal.display_text().lower()
    for overclaim in ("reproduce", "reproducible", "watched your footage", "watched the footage",
                      "understands your footage", "fully automatic", "analysed your video"):
        assert overclaim not in text, overclaim
    assert "proposal only" in text
    assert "nothing has been rendered" in text


def test_the_proposal_read_out_is_formatted_by_the_proposal_itself():
    """`gui.py` must own no second formatter."""
    proposal = fork_director.build_proposal("x", _diverse_intent(), "", 4242)
    assert proposal.display_text().startswith("Instruction: x")


# ===========================================================================
# 9. STATUS TEXT
# ===========================================================================

@pytest.mark.parametrize("status", [
    fork_director.STATUS_NO_INSTRUCTION,
    fork_director.STATUS_INVALID_PAYLOAD,
    fork_director.STATUS_TIMEOUT,
    fork_director.STATUS_NOTHING_TO_APPLY,
])
def test_every_failure_status_says_nothing_changed(status: str):
    assert "no control changed" in status.lower() or "nothing changed" in status.lower()


def test_the_timeout_status_names_the_bound():
    assert str(fork_director.TIMEOUT_SECONDS) in fork_director.STATUS_TIMEOUT


def test_the_missing_model_status_names_the_asset_and_offers_the_installer():
    text = fork_director.missing_runtime_status(r"bin\models\qwen3-4b.gguf")
    assert "qwen3-4b.gguf" in text
    assert "install" in text.lower()
    assert "no control changed" in text.lower()


def test_the_missing_model_status_never_offers_the_stage_5_model_as_a_fallback():
    """The 2B model was measured against this contract and failed it."""
    text = fork_director.missing_runtime_status("x").lower()
    for forbidden in ("qwen3vl", "2b", "fallback", "instead"):
        assert forbidden not in text, forbidden


def test_a_bounded_stderr_tail_rides_along_without_the_argv():
    text = fork_director.exit_failure_status(9, "boom " * 200)
    assert "9" in text and len(text) < 500
    assert "--json-schema" not in text


@pytest.mark.parametrize("detail", [None, 42, [], {}])
def test_a_non_string_stderr_tail_is_simply_omitted(detail: Any):
    assert "None" not in fork_director.exit_failure_status(1, detail)


def test_the_ready_and_applied_statuses_report_the_seed_and_promise_no_render():
    proposal = fork_director.build_proposal("x", _diverse_intent(), "", 4242)
    for status in (fork_director.ready_status(proposal),
                   fork_director.applied_status(proposal)):
        assert "4242" in status
        assert "rendered" in status.lower()


def test_the_ready_status_mentions_the_media_relaxation_only_when_it_happened():
    adjusted = fork_director.build_proposal(
        "x", _diverse_intent(), "", 4242, summary=CONCENTRATED)
    plain = fork_director.build_proposal("x", _diverse_intent(), "", 4242)
    assert "relaxed" in fork_director.ready_status(adjusted).lower()
    assert "relaxed" not in fork_director.ready_status(plain).lower()


@pytest.mark.parametrize("note", [
    fork_director.MEDIA_NOTE_NO_SCAN, fork_director.MEDIA_NOTE_STALE_SCAN,
    fork_director.MEDIA_NOTE_NOT_PREPARED, fork_director.MEDIA_NOTE_NO_SUMMARY,
])
def test_every_media_unavailable_note_is_truthful_and_not_called_a_fallback(note: str):
    lowered = note.lower()
    assert "media adjustment not used" in lowered
    for forbidden in ("fallback", "v1", "error", "failed", "degraded"):
        assert forbidden not in lowered, forbidden


# ===========================================================================
# 10. PURITY AND BOUNDARIES
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
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "json", "collections", "dataclasses", "typing",
                        "beatsync_fork"}, imported


def test_the_director_module_owns_no_subprocess_path_clock_or_randomness():
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("subprocess", "popen", "os.path", "open(", "time.", "random",
                      "datetime", "llama", ".gguf", "gradio"):
        assert forbidden not in source, f"director.py uses {forbidden!r}"


def test_the_director_module_names_no_pipeline_render_or_cache_machinery():
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("beat_info", "render_info", "stage5", "stage_6", "cache_contract",
                      "analysis_version", "_cache_path", "_load_cache", "ffmpeg", "nvenc",
                      "build_planned_clip_sequence"):
        assert forbidden not in source, f"director.py mentions {forbidden!r}"


def test_the_director_module_declares_no_resource_identity():
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("music_under_voice", "sfx_amount", "sfx_level", "avoid_drops",
                      "source_folder", "output_filename", "encoder", "fps"):
        assert forbidden not in source, f"director.py mentions {forbidden!r}"


def test_the_director_has_no_cache_history_or_conversation_state():
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("_cache", "history", "conversation", "session", "memory", "previous"):
        assert forbidden not in source, f"director.py mentions {forbidden!r}"


def test_the_director_module_has_no_module_level_mutable_state():
    for node in _tree(_DIRECTOR).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if targets in ({"__all__"}, {"SEMANTIC_AXES"}):
            continue  # a declaration and a frozen-by-convention registry, both never mutated
        rendered = ast.unparse(node)
        assert not rendered.endswith("= []"), rendered
        assert not rendered.endswith("= {}"), rendered
        assert "set()" not in rendered, rendered


def test_the_semantic_axis_registry_is_never_mutated():
    """Reads like ``SEMANTIC_AXES[axis]`` are fine; a write or a mutating method is not.

    Checked structurally rather than by substring, because ``SEMANTIC_AXES[`` appears in every
    legitimate lookup — a text match would either forbid reading the registry at all or, if
    loosened, miss ``.update()``.
    """
    tree = _tree(_DIRECTOR)
    for node in ast.walk(tree):
        # SEMANTIC_AXES[...] = ... / del SEMANTIC_AXES[...]
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) \
                else [node.target]
            for target in targets:
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id == "SEMANTIC_AXES"):
                    raise AssertionError(f"registry written: {ast.unparse(node)}")
        # SEMANTIC_AXES.update(...) and friends
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "SEMANTIC_AXES"):
            assert node.func.attr not in {"update", "pop", "popitem", "clear", "setdefault"}, \
                ast.unparse(node)


def test_the_director_does_not_read_the_current_sliders_or_any_screen_state():
    """The instruction is an absolute intention, not a transform of what is on screen."""
    source = _executable_source(_DIRECTOR).lower()
    for forbidden in ("current_", "live_", "on_screen", "existing_recipe", "transform"):
        assert forbidden not in source, f"director.py mentions {forbidden!r}"
