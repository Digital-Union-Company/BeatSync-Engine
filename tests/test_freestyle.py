"""Freestyle V1 — the fork record's own contract.

The feature's *pipeline* behaviour is pinned where it happens: per-section Cut Density and the two
Stage-4 compositions in `test_cut_density.py`, the global accent layer on a heterogeneous timeline in
`test_micro_cuts.py`, the `(ScoringControls, target)` table in `test_stage6_score_precompute.py`, the
GUI seam in `test_creative_controls_seam.py` / `test_creative_presets.py`, and the boundary against
the Variant Lab and the Director in `test_variant_lab.py`.

What is left — and what this suite owns — is `beatsync_fork/freestyle.py` itself: which controls a
rule may reach, what a section type is, how a preset projects into a rule, what inherits, and the two
read-outs. It is a stdlib-only fork module, so it is imported and tested as ordinary code.

Three properties here are **structural rather than careful**, and they are the ones worth protecting
first: Micro Cuts and the Variation Seed are unreachable because `SectionOverride` has no field for
them; the five fields are *derived* from the preset registry rather than restated; and
`SECTION_TYPES` is pinned against the real `stage3_sections.classify_section` so the one upstream
vocabulary this package copies cannot drift.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import os

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import freestyle as fork_freestyle
from beatsync_fork import presets as fork_presets

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FREESTYLE_PATH = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "freestyle.py")
_STAGE3_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage3_sections.py")


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _strip_docstrings(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _executable_source(path):
    """The module without its prose, so a docstring *stating* a boundary never reads as crossing it."""
    return ast.unparse(_strip_docstrings(_tree(path)))


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only(path):
    """Executable source with every string literal blanked: identifiers and calls, no copy.

    The word bans below are about *machinery*, and this module's user-facing read-out legitimately
    contains words like "render" and "global" in ordinary English. Banning them across the string
    literals too would force the copy to be written around a test, which is the wrong way round.
    """
    return ast.unparse(_BlankStrings().visit(_strip_docstrings(_tree(path))))


# ===========================================================================
# 1. THE FIELD REGISTRY — five, derived, and two unreachable
# ===========================================================================


def test_the_five_fields_are_derived_from_the_preset_registry():
    """Derived, not restated: a seventh global control cannot appear in one place and not the other.

    Filtered by *name* rather than by index, so reordering the global tuple cannot silently remove a
    different control — which is why this asserts the derivation expression itself rather than a
    hand-written list of five strings.
    """
    assert fork_freestyle.FREESTYLE_CONTROL_FIELDS == tuple(
        name for name in fork_presets.CREATIVE_CONTROL_FIELDS if name != "micro_cuts")
    assert len(fork_freestyle.FREESTYLE_CONTROL_FIELDS) == 5
    assert set(fork_freestyle.FREESTYLE_CONTROL_FIELDS) < set(
        fork_presets.CREATIVE_CONTROL_FIELDS)


def test_the_derivation_is_read_off_the_preset_module_in_the_source():
    """The equality above would also pass against a literal tuple that happens to agree today."""
    source = _executable_source(_FREESTYLE_PATH)
    assert "fork_presets.CREATIVE_CONTROL_FIELDS" in source
    assert "FREESTYLE_CONTROL_FIELDS = tuple(" in source


def test_micro_cuts_and_the_seed_have_no_field_on_a_section_override():
    """**The whole enforcement of "Micro Cuts and the Variation Seed stay global".**

    Not a validation rule that could be relaxed, and not a filter that could be bypassed: there is
    nowhere to put either value. A rule cannot touch them because the record has no field.
    """
    names = {field.name for field in dataclasses.fields(fork_freestyle.SectionOverride)}
    assert names == set(fork_freestyle.FREESTYLE_CONTROL_FIELDS)
    for forbidden in fork_freestyle.GLOBAL_ONLY_CONTROL_FIELDS:
        assert forbidden not in names
        assert not hasattr(fork_freestyle.SectionOverride(), forbidden)
    with pytest.raises(TypeError):
        fork_freestyle.SectionOverride(micro_cuts=10)
    with pytest.raises(TypeError):
        fork_freestyle.SectionOverride(seed=7)


def test_a_declaration_carries_no_seed_or_micro_cuts_either():
    names = {field.name for field in dataclasses.fields(fork_freestyle.FreestyleDeclaration)}
    assert names == {"enabled", "overrides"}


def test_every_field_has_a_label_and_the_labels_match_the_global_panel():
    """One control must never be called two things in two panels."""
    assert set(fork_freestyle.CONTROL_LABELS) == set(fork_freestyle.FREESTYLE_CONTROL_FIELDS)
    global_line = fork_creative.CreativeProfile(seed=1, cut_density=10).describe()
    for label in fork_freestyle.CONTROL_LABELS.values():
        assert label in global_line, label


# ===========================================================================
# 2. THE SECTION VOCABULARY — pinned against the real classifier
# ===========================================================================


def _stage3_vocabulary():
    """Every literal `classify_section` can return, read out of the real upstream source.

    `stage3_sections.py` needs numpy, so it is inspected rather than imported — the standard
    technique for upstream modules in this suite.
    """
    fn = next(node for node in ast.walk(_tree(_STAGE3_PATH))
              if isinstance(node, ast.FunctionDef) and node.name == "classify_section")
    returned = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            returned.add(node.value.value)
        elif isinstance(node, ast.Return) and isinstance(node.value, ast.IfExp):
            for branch in (node.value.body, node.value.orelse):
                if isinstance(branch, ast.Constant) and isinstance(branch.value, str):
                    returned.add(branch.value)
    return returned


def test_the_section_vocabulary_is_pinned_against_the_real_classifier():
    """This tuple is the one upstream vocabulary the fork package copies, so it gets a pin.

    It is copied rather than imported because importing `stage3_sections` would drag numpy and the
    whole upstream runtime into a stdlib-only module — the hard rule. The copy is therefore only as
    trustworthy as this assertion.
    """
    classifier = _stage3_vocabulary()
    assert classifier, "classify_section returned no literals; this test is measuring nothing"
    assert classifier <= set(fork_freestyle.SECTION_TYPES), (
        f"Stage 3 can return types Freestyle has no rule slot for: "
        f"{sorted(classifier - set(fork_freestyle.SECTION_TYPES))}")


def test_the_vocabulary_adds_exactly_the_undividable_track_fallback():
    """`body` is not a `classify_section` outcome — it is what Stage 3 labels a track it could not
    divide at all. It belongs in the list (such a track is one section and a user may rule it) and
    it is the *only* addition, so the tuple cannot quietly acquire an invented type."""
    extra = set(fork_freestyle.SECTION_TYPES) - _stage3_vocabulary()
    assert extra == {"body"}
    with open(_STAGE3_PATH, "r", encoding="utf-8") as handle:
        assert '"type": "body"' in handle.read()


def test_the_section_types_are_ten_unique_lowercase_names():
    assert len(fork_freestyle.SECTION_TYPES) == 10
    assert len(set(fork_freestyle.SECTION_TYPES)) == 10
    for name in fork_freestyle.SECTION_TYPES:
        assert isinstance(name, str) and name == name.lower() and name.isalpha()


def test_the_repeated_section_policy_is_recorded():
    """Keyed by type, so every instance of a repeated type shares one rule."""
    assert fork_freestyle.REPEATED_SECTION_POLICY == "ALL_INSTANCES_SHARE_RULE"


# ===========================================================================
# 3. THE STYLE CHOICES — Base in, Custom out
# ===========================================================================


def test_base_is_first_and_the_four_recipes_follow():
    assert fork_freestyle.SECTION_STYLE_CHOICES[0] == fork_freestyle.BASE_STYLE
    assert fork_freestyle.SECTION_STYLE_CHOICES[1:] == tuple(fork_presets.PRESETS)
    assert len(fork_freestyle.SECTION_STYLE_CHOICES) == 5


def test_custom_is_excluded_because_it_has_no_values_to_project():
    """`Custom` is a *state* meaning "the live values match no recipe". `Base` takes its place and
    says something `Custom` cannot: inherit, whatever the sliders later become."""
    assert fork_presets.CUSTOM_PRESET not in fork_freestyle.SECTION_STYLE_CHOICES
    assert fork_freestyle.override_from_style(fork_presets.CUSTOM_PRESET) is None
    assert fork_freestyle.BASE_STYLE not in fork_presets.PRESET_NAMES


# ===========================================================================
# 4. SECTION OVERRIDE
# ===========================================================================


def test_an_override_is_frozen_hashable_and_deepcopy_safe():
    override = fork_freestyle.SectionOverride(cut_density=100, motion_bias=20)
    with pytest.raises(dataclasses.FrozenInstanceError):
        override.cut_density = 10
    assert hash(override) == hash(fork_freestyle.SectionOverride(cut_density=100, motion_bias=20))
    assert copy.deepcopy(override) == override


def test_an_unset_field_stays_unset_rather_than_becoming_neutral():
    """The one place this layer must **not** reuse `normalize_control`'s notion of neutral.

    That function answers 50 for a missing value, and 50 is a perfectly ordinary density a user may
    have deliberately ruled. Here absence means "do not touch this control", so it must survive as
    absence — otherwise every rule would silently pin all five controls.
    """
    override = fork_freestyle.SectionOverride(cut_density=70)
    assert override.cut_density == 70
    assert override.semantic_emphasis is None
    assert override.motion_bias is None
    assert override.set_values() == (("cut_density", 70),)


def test_a_set_field_goes_through_the_one_control_normalisation():
    """What a control value *is* stays defined in exactly one place."""
    assert fork_freestyle.SectionOverride(cut_density=140).cut_density == 100
    assert fork_freestyle.SectionOverride(cut_density=-10).cut_density == 0
    assert fork_freestyle.SectionOverride(motion_bias=70.0).motion_bias == 70
    assert fork_freestyle.SectionOverride(motion_bias="70").motion_bias == 70
    # `bool` is rejected first because it subclasses `int`, exactly as `normalize_control` does.
    assert fork_freestyle.SectionOverride(motion_bias=True).motion_bias == (
        fork_creative.DEFAULT_CONTROL)


def test_an_empty_override_is_empty_and_a_set_one_is_not():
    assert fork_freestyle.SectionOverride().is_empty()
    assert not fork_freestyle.SectionOverride(source_diversity=0).is_empty()
    # 0 is a real value, not an absent one.
    assert fork_freestyle.SectionOverride(source_diversity=0).set_values() == (
        ("source_diversity", 0),)


def test_set_values_keeps_the_registry_order():
    override = fork_freestyle.SectionOverride(source_diversity=10, cut_density=20, motion_bias=30)
    assert [name for name, _value in override.set_values()] == [
        "cut_density", "motion_bias", "source_diversity"]


def test_describe_prints_only_what_the_rule_decided():
    override = fork_freestyle.SectionOverride(cut_density=100, motion_bias=80)
    assert override.describe() == "Cut Density 100 · Motion Bias 80"
    assert fork_freestyle.SectionOverride().describe() == ""


def test_summarize_marks_every_inherited_field_base():
    """The read-out form: all five fields, and never an inherited number."""
    summary = fork_freestyle.SectionOverride(cut_density=100).summarize()
    assert summary.startswith("Cut Density 100")
    assert summary.count(fork_freestyle.BASE_STYLE) == 4
    for label in fork_freestyle.CONTROL_LABELS.values():
        assert label in summary


# ===========================================================================
# 5. THE PRESET PROJECTION
# ===========================================================================


@pytest.mark.parametrize("style", list(fork_presets.PRESETS))
def test_a_preset_projects_its_five_values_and_drops_micro_cuts(style):
    """What makes "a style named Cinematic keeps your Micro Cuts exactly as you set it" true rather
    than merely intended."""
    recipe = fork_presets.PRESETS[style]
    override = fork_freestyle.override_from_style(style)
    assert override is not None
    for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS:
        assert getattr(override, name) == recipe[name], name
    assert not hasattr(override, "micro_cuts")
    # ...and the recipe really did carry a micro value that was dropped, so this is not vacuous.
    assert "micro_cuts" in recipe


def test_a_rule_stores_the_numbers_never_the_name():
    """A later retune of the preset table must not silently change what a saved rule meant — the
    same reproducibility argument that keeps the preset name out of `CreativeProfile`."""
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(True, {"drop": "Cinematic"})
    rendered = repr(declaration)
    for name in fork_presets.PRESET_NAMES:
        assert name not in rendered, name
    assert "Cut Density 30" in declaration.describe()


@pytest.mark.parametrize("style", [
    None, "", "Base", "Custom", "cinematic", "HIGH ENERGY", 42, 0, True, [], {}, object()])
def test_everything_that_is_not_a_recipe_projects_to_no_rule(style):
    """One answer for "nothing to declare", so a caller needs one branch. Matching is exact and
    case-sensitive, delegated to `presets.resolve_preset`."""
    assert fork_freestyle.override_from_style(style) is None


# ===========================================================================
# 6. THE DECLARATION
# ===========================================================================


def _rule(**values):
    return fork_freestyle.SectionOverride(**values)


def test_the_default_declaration_is_off_with_nothing_declared():
    declaration = fork_freestyle.FreestyleDeclaration()
    assert declaration.enabled is False
    assert declaration.overrides == ()
    assert not declaration.is_active()
    assert declaration.ruled_types() == ()
    assert declaration.describe() == "off"


def test_rules_are_retained_while_the_switch_is_off():
    """A user who unticks the checkbox to compare two renders must not lose what they declared. So
    "has rules" is deliberately not permission to use them."""
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(
        False, {"drop": "High Energy", "intro": "Cinematic"})
    assert declaration.overrides, "the rules were discarded"
    assert declaration.ruled_types() == ("intro", "drop")
    assert not declaration.is_active()
    assert declaration.rule_for("drop") is None, "an inactive declaration must rule nothing"
    assert declaration.effective_cut_densities(50) == {}
    assert declaration.describe() == "off"


def test_is_active_is_the_one_gate_both_stages_share():
    """Enabled **and** carrying at least one rule. A real failure mode this closes: an `isinstance`
    check in one stage let Freestyle-off change the cut timeline while leaving scoring global, so
    the two stages disagreed about whether the feature was on at all."""
    assert not fork_freestyle.FreestyleDeclaration(enabled=True).is_active()
    assert not fork_freestyle.FreestyleDeclaration(
        enabled=False, overrides=(("drop", _rule(cut_density=90)),)).is_active()
    assert fork_freestyle.FreestyleDeclaration(
        enabled=True, overrides=(("drop", _rule(cut_density=90)),)).is_active()


def test_an_all_base_screen_declares_nothing_at_all():
    """Not "a declaration whose rules happen to be neutral" — no rules, so `is_active()` is False
    and both stages take their legacy paths by the same decision."""
    styles = {name: fork_freestyle.BASE_STYLE for name in fork_freestyle.SECTION_TYPES}
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(True, styles)
    assert declaration.overrides == ()
    assert not declaration.is_active()


@pytest.mark.parametrize("styles", [None, "drop", 42, [], object(), {"nonsense": "Cinematic"}])
def test_from_styles_is_total(styles):
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(True, styles)
    assert isinstance(declaration, fork_freestyle.FreestyleDeclaration)
    assert declaration.overrides == ()


@pytest.mark.parametrize("enabled,expected", [
    (True, True), (1, True), ("yes", True), (False, False), (0, False), (None, False), ("", False)])
def test_enabled_is_coerced_to_a_real_bool(enabled, expected):
    """It rides on the shared bus and lands in a frozen record, so it must not carry a Gradio value
    whose truthiness a later reader has to re-decide."""
    declaration = fork_freestyle.FreestyleDeclaration(enabled=enabled)
    assert declaration.enabled is expected


def test_unknown_and_empty_rules_are_dropped_rather_than_stored():
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("drop", _rule(cut_density=90)),
                   ("nonsense", _rule(cut_density=10)),
                   ("verse", _rule()),
                   ("chorus", "High Energy"),
                   ("bridge",),
                   "junk"))
    assert declaration.overrides == (("drop", _rule(cut_density=90)),)


def test_rules_are_canonically_ordered_so_equal_screens_compare_equal():
    """Ordered by `SECTION_TYPES`, not by insertion, so two declarations built by different routes
    — a GUI tuple, a preset projection, a direct caller — compare equal when they say the same
    thing."""
    first = fork_freestyle.FreestyleDeclaration.from_styles(
        True, {"drop": "Cinematic", "intro": "Dynamic"})
    second = fork_freestyle.FreestyleDeclaration.from_styles(
        True, {"intro": "Dynamic", "drop": "Cinematic"})
    assert first.overrides == second.overrides
    assert first == second
    assert first.ruled_types() == ("intro", "drop")


def test_a_declaration_is_frozen_hashable_and_deepcopy_safe():
    """It rides on `beat_info`, which is deepcopied and mutated downstream, so it must carry no
    mapping proxy, no path, no media and no cache handle."""
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(True, {"drop": "Dynamic"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        declaration.enabled = False
    assert hash(declaration) == hash(
        fork_freestyle.FreestyleDeclaration.from_styles(True, {"drop": "Dynamic"}))
    clone = copy.deepcopy(declaration)
    assert clone == declaration and clone.overrides == declaration.overrides


def test_a_later_duplicate_rule_for_one_type_wins_once():
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("drop", _rule(cut_density=10)), ("drop", _rule(cut_density=90))))
    assert declaration.overrides == (("drop", _rule(cut_density=90)),)


def test_rule_for_answers_only_the_declared_type():
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True, overrides=(("drop", _rule(motion_bias=90)),))
    assert declaration.rule_for("drop") == _rule(motion_bias=90)
    assert declaration.rule_for("verse") is None
    assert declaration.rule_for("nonsense") is None
    assert declaration.rule_for(None) is None


# --- effective_cut_densities: Stage 4's one query ---------------------------------------


def test_effective_cut_densities_reports_the_ruled_types_only():
    """Unruled types are deliberately absent rather than mapped to the base: Stage 4 fills them in
    from its own base config, and listing them here would invent an authority this record lacks."""
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("intro", _rule(cut_density=10)), ("drop", _rule(cut_density=100))))
    assert declaration.effective_cut_densities(50) == {"intro": 10, "drop": 100}


def test_a_rule_setting_no_density_resolves_to_the_base_density():
    """What makes Stage 4's short-circuit a decision about resolved *values*: a screen that varies
    only the four Stage-6 controls resolves every section to one density, so the cut timeline must
    be byte-identical."""
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True, overrides=(("drop", _rule(motion_bias=90)),))
    assert declaration.effective_cut_densities(50) == {"drop": 50}
    assert declaration.effective_cut_densities(80) == {"drop": 80}


def test_effective_cut_densities_normalises_its_base():
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True, overrides=(("drop", _rule(motion_bias=90)),))
    assert declaration.effective_cut_densities(None) == {"drop": fork_creative.DEFAULT_CONTROL}
    assert declaration.effective_cut_densities(999) == {"drop": 100}


def test_effective_cut_densities_is_empty_when_inactive():
    assert fork_freestyle.FreestyleDeclaration().effective_cut_densities(50) == {}


def test_describe_is_unlabelled_execution_truth():
    """Unlabelled on purpose: its two callers supply their own framing, and a label baked in here
    read as `Freestyle: ... (Freestyle: ...)` in one of them."""
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("intro", _rule(cut_density=10)), ("drop", _rule(cut_density=100,
                                                                    motion_bias=80))))
    assert declaration.describe() == (
        "intro → Cut Density 10; drop → Cut Density 100 · Motion Bias 80")
    assert not declaration.describe().startswith("Freestyle")


# ===========================================================================
# 7. COMPOSITION — effective_profile
# ===========================================================================


_BASE = fork_creative.CreativeProfile(seed=4242, cut_density=40, micro_cuts=85,
                                      semantic_emphasis=45, energy_response=55,
                                      motion_bias=35, source_diversity=60)


def _declared(**by_type):
    ordered = tuple(
        (section_type, override) for section_type, override in sorted(
            by_type.items(), key=lambda kv: fork_freestyle.SECTION_TYPES.index(kv[0])))
    return fork_freestyle.FreestyleDeclaration(enabled=True, overrides=ordered)


def test_a_rule_replaces_only_the_fields_it_sets():
    effective = fork_freestyle.effective_profile(
        _BASE, "drop", _declared(drop=_rule(motion_bias=100, cut_density=90)))
    assert effective.motion_bias == 100
    assert effective.cut_density == 90
    # everything unset inherits the live global value
    assert effective.semantic_emphasis == _BASE.semantic_emphasis
    assert effective.energy_response == _BASE.energy_response
    assert effective.source_diversity == _BASE.source_diversity


def test_the_seed_and_micro_cuts_are_carried_over_untouched():
    """They have no field on a rule, so this is a consequence rather than a check — asserted anyway
    because it is the user-visible promise."""
    effective = fork_freestyle.effective_profile(
        _BASE, "drop", _declared(drop=_rule(cut_density=100, motion_bias=100)))
    assert effective.seed == _BASE.seed
    assert effective.micro_cuts == _BASE.micro_cuts


@pytest.mark.parametrize("section_type,declaration", [
    ("drop", fork_freestyle.FreestyleDeclaration()),
    ("drop", fork_freestyle.FreestyleDeclaration(enabled=True)),
    ("verse", None),
    ("verse", "junk"),
    ("verse", 42),
])
def test_nothing_to_apply_returns_the_base_object_itself(section_type, declaration):
    """**Identity, not equality.** Stage 6's caller tests `effective is profile_settings` to reuse
    the global scoring column, so an equal-but-separate copy would quietly add a table column per
    section and undo L1A's whole point."""
    assert fork_freestyle.effective_profile(_BASE, section_type, declaration) is _BASE


def test_an_unruled_section_gets_the_base_object_by_identity():
    declaration = _declared(drop=_rule(motion_bias=100))
    assert fork_freestyle.effective_profile(_BASE, "verse", declaration) is _BASE
    assert fork_freestyle.effective_profile(_BASE, "nonsense", declaration) is _BASE
    assert fork_freestyle.effective_profile(_BASE, None, declaration) is _BASE


def test_a_rule_that_restates_the_base_values_returns_the_base_object():
    """A user who rules a section at the value the sliders already hold has asked for nothing, and
    must not pay a table column for it."""
    declaration = _declared(drop=_rule(motion_bias=_BASE.motion_bias,
                                       cut_density=_BASE.cut_density))
    assert fork_freestyle.effective_profile(_BASE, "drop", declaration) is _BASE


def test_a_partially_redundant_rule_still_applies_its_real_change():
    declaration = _declared(drop=_rule(motion_bias=_BASE.motion_bias, cut_density=99))
    effective = fork_freestyle.effective_profile(_BASE, "drop", declaration)
    assert effective is not _BASE
    assert effective.cut_density == 99
    assert effective.motion_bias == _BASE.motion_bias


def test_a_disabled_declaration_composes_nothing():
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=False, overrides=(("drop", _rule(motion_bias=100)),))
    assert fork_freestyle.effective_profile(_BASE, "drop", declaration) is _BASE


def test_the_base_profile_is_never_mutated():
    before = _BASE.as_dict()
    fork_freestyle.effective_profile(_BASE, "drop", _declared(drop=_rule(motion_bias=100)))
    assert _BASE.as_dict() == before


def test_two_sections_resolving_to_the_same_values_collapse_to_one_scoring_key():
    """The dedup mechanism Stage 6's lazy table relies on: `ScoringControls` is a frozen dataclass
    of three `float | None`, so two independently resolved profiles that land on the same three
    numbers produce the *same* dict key — no new identity type was invented for it."""
    rule = _rule(motion_bias=100)
    first = fork_freestyle.effective_profile(_BASE, "drop", _declared(drop=rule))
    second = fork_freestyle.effective_profile(_BASE, "intro", _declared(intro=rule))
    assert first.scoring_controls() == second.scoring_controls()
    assert hash(first.scoring_controls()) == hash(second.scoring_controls())
    other = fork_freestyle.effective_profile(_BASE, "drop", _declared(drop=_rule(motion_bias=20)))
    assert other.scoring_controls() != first.scoring_controls()


def test_a_section_rule_can_reach_every_one_of_the_five_controls():
    """Non-vacuity for the whole registry: each field really is wired through composition."""
    for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS:
        value = 100 if getattr(_BASE, name) != 100 else 0
        effective = fork_freestyle.effective_profile(
            _BASE, "drop", _declared(drop=_rule(**{name: value})))
        assert getattr(effective, name) == value, name
        assert effective is not _BASE


# ===========================================================================
# 8. THE READ-OUT
# ===========================================================================


def test_the_summary_never_prints_an_inherited_number():
    """The global controls have five legitimate writers (the preset selector, Variant Lab Generate /
    New / Apply, and Director Apply), so a number copied into this read-out could not be kept
    honest. `Base` stays true whatever the sliders later become."""
    declaration = _declared(drop=_rule(cut_density=100))
    text = fork_freestyle.summary_text(declaration)
    assert "100" in text
    assert text.count(fork_freestyle.BASE_STYLE) >= 4
    for value in (_BASE.semantic_emphasis, _BASE.energy_response, _BASE.source_diversity):
        assert str(value) not in text, value


def test_the_summary_formatter_takes_no_global_control_at_all():
    """Which makes the guarantee above structural rather than a promise: there is no global value in
    scope to print."""
    import inspect
    parameters = list(inspect.signature(fork_freestyle.summary_text).parameters)
    assert parameters == ["declaration"]


def test_the_summary_distinguishes_off_from_on_with_nothing_ruled():
    off = fork_freestyle.summary_text(fork_freestyle.FreestyleDeclaration())
    on_empty = fork_freestyle.summary_text(fork_freestyle.FreestyleDeclaration(enabled=True))
    assert off == fork_freestyle.SUMMARY_DISABLED
    assert on_empty == fork_freestyle.SUMMARY_NO_RULES
    assert off != on_empty
    # both have to say the render is unchanged, because it is
    assert "off" in off.lower()
    assert "Base" in on_empty


def test_the_summary_says_micro_cuts_and_the_seed_stay_global():
    """Two things a user could reasonably assume otherwise, so the read-out states both."""
    text = fork_freestyle.summary_text(_declared(drop=_rule(cut_density=100)))
    assert "Micro Cuts" in text
    assert "Variation Seed" in text


def test_the_summary_lists_the_unruled_types_as_base():
    text = fork_freestyle.summary_text(_declared(drop=_rule(cut_density=100)))
    for section_type in fork_freestyle.SECTION_TYPES:
        assert section_type in text, section_type


def test_the_summary_is_one_line_per_ruled_section_and_fits_the_textbox():
    """The GUI textbox is 6 lines with `max_lines=16`, so ten ruled sections must not overflow it
    into a scrolling wall."""
    declaration = _declared(**{name: _rule(cut_density=70)
                               for name in fork_freestyle.SECTION_TYPES})
    text = fork_freestyle.summary_text(declaration)
    assert len(text.splitlines()) <= 16
    assert "Base for every control" not in text, "nothing is unruled here"


@pytest.mark.parametrize("value", [None, 42, "drop", {}, [], object()])
def test_the_summary_is_total(value):
    """It is a widget default and a `.change()` return value; nothing here may raise mid-interaction."""
    assert fork_freestyle.summary_text(value) == fork_freestyle.SUMMARY_DISABLED


def test_the_summary_is_deterministic():
    declaration = _declared(drop=_rule(cut_density=100), intro=_rule(motion_bias=10))
    assert fork_freestyle.summary_text(declaration) == fork_freestyle.summary_text(declaration)


# ===========================================================================
# 9. ISOLATION — what this module is not allowed to know
# ===========================================================================


def test_the_module_imports_only_creative_and_presets():
    """`fork-package.md`'s row for this module. The dependency runs one way only, so `presets.py`
    never learns that a section mechanism exists."""
    imported = set()
    for node in ast.walk(_tree(_FREESTYLE_PATH)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert imported == {
        "__future__.annotations",
        "dataclasses.dataclass", "dataclasses.fields", "dataclasses.replace",
        "typing.Any", "typing.Mapping",
        "beatsync_fork.creative", "beatsync_fork.presets",
    }, sorted(imported)


def test_the_module_renders_nothing_analyses_nothing_and_caches_nothing():
    """A rule is interpretation. Stage 5 owns intrinsic media truth, and no value here may reach a
    Qwen request, the prompt, a persisted record or any cache signature."""
    source = _code_only(_FREESTYLE_PATH).lower()
    for forbidden in ("gradio", "gr.", "numpy", "ffmpeg", "subprocess", "open(", "os.", "json",
                      "qwen", "cache", "analysis_version", "_video_signature", "_cache_path",
                      "video_analysis", "analyze_beats_auto", "create_music_video",
                      "process_video", "render", "audio_mix", "smart_mix", "beat_info"):
        assert forbidden not in source, f"freestyle.py references {forbidden!r}"


def test_the_module_knows_no_stage_and_no_section_boundary():
    """It declares rules by section *type*. Beat times, section boundaries, durations and segment
    plans belong to the stages that own them."""
    source = _code_only(_FREESTYLE_PATH).lower()
    for forbidden in ("beat_times", "section_settings", "segment", "stage4", "stage6",
                      "audiowaveconfig", "autowaveconfig", "density_factor", "candidate"):
        assert forbidden not in source, f"freestyle.py references {forbidden!r}"


def test_the_module_holds_no_mutable_state():
    """Every module-level binding is an immutable constant: a rule is per-render intent, so nothing
    here may remember the last render."""
    for node in _tree(_FREESTYLE_PATH).body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            # Every fork module writes `__all__` as a list; it is export metadata, not state.
            continue
        # Asserted on the AST rather than on the unparsed text: the read-out copy contains the
        # English word "global", and a text match cannot tell that from a `global` statement.
        assert not isinstance(node.value, (ast.List, ast.Set)), ast.unparse(node)
        assert not (isinstance(node.value, ast.Dict) and not node.value.keys), ast.unparse(node)
    assert "global " not in _code_only(_FREESTYLE_PATH)
    for name in dir(fork_freestyle):
        if name.startswith("_"):
            continue
        value = getattr(fork_freestyle, name)
        assert not isinstance(value, (list, set, bytearray)), name
        assert not isinstance(value, dict) or name == "CONTROL_LABELS", name
