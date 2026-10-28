"""The truthful effective Creative Profile (R8 UX Refresh, Phase 5).

The defect this module exists for: the Creative Preset selector could read `Balanced` while
Freestyle rules, a non-zero Variation Seed, voice clips or SFX were all active. Every individual
statement was true and the combination was misleading.

So the suite is organised around the two ways a read-out like this goes wrong, and it asserts both
directions rather than only the obvious one:

* **Under-reporting** — claiming a plain named-preset render when something else is armed. That is
  the reported defect.
* **Over-reporting** — claiming a render is modified when it is not. Freestyle ON with all ten
  sections on `Base` is the live example: `Base` means "inherit the live global value", so the
  renderer takes the ordinary path and announcing an edit would be a new lie in the other
  direction. An earlier read-out in this repository made exactly that mistake twice, which is why
  every verdict here is delegated to the subsystem that owns it.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from beatsync_fork import effective_profile as effective
from beatsync_fork import freestyle as fork_freestyle
from beatsync_fork import freestyle_random as fork_random
from beatsync_fork import presets
from beatsync_fork import smart_mix as fork_smart_mix

BALANCED = presets.preset_values(presets.BALANCED_PRESET)
CINEMATIC = presets.preset_values("Cinematic")
UNMATCHED = tuple(7 for _ in BALANCED)


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only(module) -> str:
    """Executable source with every string literal blanked, docstrings included.

    The same helper, and the same reasoning, that `test_gui_tabs.py`, `test_cache_admin.py` and
    the Freestyle suites carry: the bans below are about *machinery*, and this module's docstring
    legitimately contains the words "remembered base" in the paragraph explaining why it has none.

    A ban that the explanation of the ban cannot survive forces documentation to be written around
    a test. This file's first draft made exactly that mistake and failed on its own prose -- which
    is now the third time in this one change, after `gui.py`'s Video-Review comment and
    `test_cache_admin.py`'s category builder. Blank the strings; never scan raw source.
    """
    import copy

    return ast.unparse(_BlankStrings().visit(copy.deepcopy(module)))


def _declaration(enabled=True, styles=None):
    resolved = styles or {section: fork_freestyle.BASE_STYLE
                          for section in fork_freestyle.SECTION_TYPES}
    return fork_random.declaration_from_styles(enabled, resolved, 0)


def _one_real_rule():
    styles = {section: fork_freestyle.BASE_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    styles[fork_freestyle.SECTION_TYPES[0]] = "Cinematic"
    return _declaration(True, styles)


# ---------------------------------------------------------------------------------------------
# The base is named only when it is verifiable
# ---------------------------------------------------------------------------------------------

def test_a_recipe_match_is_named():
    assert effective.base_label(BALANCED) == presets.BALANCED_PRESET
    assert effective.base_label(CINEMATIC) == "Cinematic"


def test_values_matching_no_recipe_are_custom():
    assert effective.base_label(UNMATCHED) == effective.CUSTOM_LABEL


def test_the_base_label_comes_from_the_one_existing_preset_path():
    """A second preset-label implementation could disagree with the selector on the Creative
    Settings tab, which is the exact class of defect this module exists to remove. Asserted
    structurally: the module delegates to `presets.matching_preset` and derives no label itself.
    """
    tree = ast.parse(inspect.getsource(effective))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "presets.matching_preset" in calls
    # and it does not restate any recipe or threshold of its own
    rendered = _code_only(tree)
    for forbidden in ("PRESETS", "preset_values", "DEFAULT_CONTROL", "50"):
        assert forbidden not in rendered, f"{forbidden} is restated here"


def test_no_remembered_base_is_introduced():
    """The R8 task asked for `Custom — based on Balanced`. That needs a "last selected preset",
    which `.claude/rules/creative-presets.md` forbids and `test_creative_presets.py` pins -- six
    integers describe a render completely, so a label is recomputed and then stops existing.

    Reintroducing one would create a second source of truth that could *disagree* with the
    sliders, so a base is named only when the live values prove it. This asserts the module
    acquired no such state.
    """
    rendered = _code_only(ast.parse(inspect.getsource(effective)))
    for forbidden in ("last_preset", "last_selected", "previous_preset", "remembered",
                      "_BASE_STATE", "global "):
        assert forbidden not in rendered, f"{forbidden} smells like remembered base state"


# ---------------------------------------------------------------------------------------------
# Under-reporting: the reported defect
# ---------------------------------------------------------------------------------------------

def test_a_named_preset_with_an_active_override_is_not_presented_as_unmodified():
    """The owner's exact complaint, as a test."""
    answer = effective.summary(BALANCED, freestyle_declaration=_one_real_rule())
    assert answer != presets.BALANCED_PRESET
    assert presets.BALANCED_PRESET in answer, answer
    assert "Freestyle" in answer, answer


@pytest.mark.parametrize("kwargs,expected", [
    ({"variation_seed": 7}, "Variation Seed 7"),
    ({"voice_clips": ["a.wav", "b.wav"]}, "Audio Layers (2 voice clips)"),
    ({"smart_mix_active": True}, "Smart Mix"),
])
def test_each_override_is_named(kwargs, expected):
    answer = effective.summary(BALANCED, **kwargs)
    assert expected in answer, answer
    assert answer.startswith(presets.BALANCED_PRESET), answer


def test_the_base_is_stated_even_when_it_is_custom():
    """"Freestyle" alone does not tell an operator what the six controls are doing underneath."""
    answer = effective.summary(UNMATCHED, freestyle_declaration=_one_real_rule())
    assert answer.startswith(effective.CUSTOM_LABEL), answer


def test_every_override_appears_in_a_fixed_order():
    """The same state must always render the same string, or an operator comparing two screens
    sees a difference that is not there."""
    answer = effective.summary(UNMATCHED, freestyle_declaration=_one_real_rule(),
                               variation_seed=99, voice_clips=["v.wav"], smart_mix_active=True)
    positions = [answer.index(fragment) for fragment in
                 (effective.CUSTOM_LABEL, "Freestyle", "Variation Seed", "Audio Layers",
                  "Smart Mix")]
    assert positions == sorted(positions), answer


# ---------------------------------------------------------------------------------------------
# Over-reporting: the mistake an earlier read-out made twice
# ---------------------------------------------------------------------------------------------

def test_a_plain_preset_render_reads_as_just_that_preset():
    assert effective.summary(BALANCED) == presets.BALANCED_PRESET
    assert effective.summary(CINEMATIC) == "Cinematic"


def test_freestyle_on_with_every_section_on_base_is_not_an_override():
    """`Base` means "inherit the live global value", so the declaration has no overrides,
    `is_active()` is False, and the renderer takes the ordinary global path."""
    assert effective.summary(BALANCED, freestyle_declaration=_declaration(True)) \
        == presets.BALANCED_PRESET


def test_freestyle_off_is_never_reported_whatever_the_sections_say():
    styles = {section: fork_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    assert effective.summary(BALANCED, freestyle_declaration=_declaration(False, styles)) \
        == presets.BALANCED_PRESET


def test_random_sections_are_reported_because_they_do_yield_rules():
    """The positive control for the two tests above: the correction must not simply silence
    Freestyle. `Random` is not a preset name, so V1's resolver answers `None` for it -- reading ten
    Random sections as ten Base sections is a defect this repository has already paid for once.
    """
    styles = {section: fork_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    answer = effective.summary(BALANCED, freestyle_declaration=_declaration(True, styles))
    assert "Freestyle" in answer, answer


def test_seed_zero_is_not_an_override():
    """0 is the documented precision baseline, not a variation."""
    assert effective.summary(BALANCED, variation_seed=0) == presets.BALANCED_PRESET


def test_the_smart_mix_verdict_follows_the_fork_record():
    """`Amount 0` is a hard off-branch however many roles are ticked. The verdict is the caller's
    to obtain from `SmartMixConfig`, and this asserts the read-out agrees with it for every amount
    rather than applying a local `> 0` that happened to match today.
    """
    for amount in (0, 1, 25, 50, 99, 100):
        expected = fork_smart_mix.SmartMixConfig(
            enabled_roles=("impact",), amount=amount).plans_anything
        answer = effective.summary(BALANCED, smart_mix_active=expected)
        assert ("Smart Mix" in answer) is bool(expected), f"amount={amount}: {answer}"


def test_the_module_builds_no_declaration_or_config_of_its_own():
    """Both verdicts are delegated. Building either here is how a read-out starts disagreeing with
    the renderer, and it is the specific failure mode this module replaced."""
    tree = ast.parse(inspect.getsource(effective))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("SmartMixConfig", "declaration_from_styles",
                      "FreestyleDeclaration", "fork_freestyle.FreestyleDeclaration"):
        assert forbidden not in calls, f"{forbidden} is constructed by the read-out"


# ---------------------------------------------------------------------------------------------
# is_modified
# ---------------------------------------------------------------------------------------------

def test_is_modified_is_false_only_for_a_plain_named_preset():
    assert effective.is_modified(BALANCED) is False
    assert effective.is_modified(CINEMATIC) is False
    assert effective.is_modified(UNMATCHED) is True
    assert effective.is_modified(BALANCED, variation_seed=1) is True
    assert effective.is_modified(BALANCED, freestyle_declaration=_one_real_rule()) is True


def test_is_modified_does_not_parse_the_summary_string():
    """A string a human reads and a boolean a control depends on must not share a parser."""
    source = inspect.getsource(effective.is_modified)
    assert "summary(" not in source, "is_modified derives its answer by parsing prose"


# ---------------------------------------------------------------------------------------------
# Totality: a read-out that can crash a tab selection is worse than one that says little
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, "", "abc", [], {}, object(), 3.5, True, [1, 2],
                                  (1, 2, 3), float("nan")])
def test_junk_values_always_produce_a_sentence(junk):
    answer = effective.summary(junk, freestyle_declaration=junk, variation_seed=junk,
                               voice_clips=junk, smart_mix_active=junk)
    assert isinstance(answer, str) and answer
    assert isinstance(effective.is_modified(junk, freestyle_declaration=junk,
                                            variation_seed=junk), bool)


def test_a_declaration_whose_is_active_raises_cannot_escape():
    class Exploding:
        def is_active(self):
            raise RuntimeError("boom")

    assert isinstance(effective.summary(BALANCED, freestyle_declaration=Exploding()), str)


def test_the_module_renders_resolves_and_caches_nothing():
    """It is a view. The six sliders and the declarations remain the only execution truth."""
    tree = ast.parse(inspect.getsource(effective))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("open", "os.path.exists", "subprocess.run", "time.time",
                      "random.random", "os.makedirs"):
        assert forbidden not in calls, f"{forbidden} is reachable from a read-out"
