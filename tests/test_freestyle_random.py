"""Random Freestyle V2 — the seeded per-section generator.

`freestyle.py` is deliberately untouched by this milestone, so its own suite still proves the
record, the five fields and the unreachable globals. What is tested here is only what V2 adds: the
generator, the one resolution point, and the GUI seam that exposes it.

Stdlib-only targets, so the generator is imported and executed directly. The GUI and `auto_mode`
seams are asserted structurally by `ast`, exactly as the other seam suites in this repo do — those
modules cannot be imported on a bare interpreter.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import freestyle as fork_freestyle
from beatsync_fork import freestyle_random as fork_random

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_AUTO = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_FREESTYLE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "freestyle.py")
_RANDOM = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "freestyle_random.py")

_SEED = 270187
_OTHER_SEED = 4242


def _tree(path: str) -> ast.Module:
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


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only(path: str) -> str:
    """Executable source with docstrings removed and string literals blanked.

    Same helper, and the same reasoning, as `test_freestyle.py`'s: the word bans here are about
    *machinery*. This module's prose legitimately contains "inference", "model" and "director" —
    in sentences explaining that it runs none of them — and banning a word across the explanation
    of why it is absent would force the documentation to be written around a test.
    """
    return ast.unparse(_BlankStrings().visit(_strip_docstrings(_tree(path))))


def _all_random() -> dict[str, str]:
    return {t: fork_random.RANDOM_STYLE for t in fork_freestyle.SECTION_TYPES}


# ======================================================================================
# 1. Freestyle OFF is unchanged — the whole backward-compatibility contract
# ======================================================================================


def test_freestyle_off_stays_inactive_even_with_every_section_random():
    """The switch is still the only gate. V2 may not make an off render do anything."""
    declaration = fork_random.declaration_from_styles(False, _all_random(), _SEED)
    assert declaration.is_active() is False
    assert declaration.rule_for("drop") is None


def test_a_screen_with_no_random_entry_equals_the_v1_declaration_exactly():
    """Every non-Random value still goes through V1's `override_from_style`, untouched."""
    styles = {t: fork_freestyle.BASE_STYLE for t in fork_freestyle.SECTION_TYPES}
    styles["drop"] = "High Energy"
    styles["intro"] = "Cinematic"

    v1 = fork_freestyle.FreestyleDeclaration.from_styles(True, styles)
    v2 = fork_random.declaration_from_styles(True, styles, _SEED)
    assert v2 == v1
    # and the seed cannot perturb a screen with nothing to generate
    assert fork_random.declaration_from_styles(True, styles, _OTHER_SEED) == v1


def test_a_non_mapping_is_no_rules_at_all():
    for bad in (None, "styles", 42, [], object()):
        declaration = fork_random.declaration_from_styles(True, bad, _SEED)
        assert declaration.is_active() is False


# ======================================================================================
# 2. Random produces real, active, per-section rules
# ======================================================================================


def test_every_section_type_resolves_to_an_active_numeric_rule():
    declaration = fork_random.declaration_from_styles(True, _all_random(), _SEED)
    assert declaration.is_active() is True
    assert len(declaration.overrides) == len(fork_freestyle.SECTION_TYPES)

    for section_type in fork_freestyle.SECTION_TYPES:
        rule = declaration.rule_for(section_type)
        assert rule is not None, section_type
        for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS:
            value = getattr(rule, name)
            assert isinstance(value, int), (section_type, name, value)
            assert 0 <= value <= 100, (section_type, name, value)


def test_a_generated_rule_sets_all_five_permitted_controls_and_no_other():
    rule = fork_random.generate_override("drop", _SEED)
    assert ([name for name, _ in rule.set_values()]
            == list(fork_freestyle.FREESTYLE_CONTROL_FIELDS))


def test_the_unreachable_globals_stay_unreachable():
    """Micro Cuts and the Variation Seed have no field, so the generator cannot set them."""
    rule = fork_random.generate_override("drop", _SEED)
    for name in fork_freestyle.GLOBAL_ONLY_CONTROL_FIELDS:
        assert not hasattr(rule, name), name


def test_every_section_type_has_a_character_band():
    """A band per type, so no section silently falls through to the neutral default."""
    for section_type in fork_freestyle.SECTION_TYPES:
        assert section_type in fork_random._SECTION_BANDS, section_type
        band = fork_random._SECTION_BANDS[section_type]
        for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS:
            low, high = band[name]
            assert 0 <= low <= high <= 100, (section_type, name)


def test_the_bands_are_musically_ordered_not_uniform_noise():
    """A generated intro must stay calmer than a generated drop, at any seed."""
    for seed in (0, 1, _SEED, _OTHER_SEED, 999999):
        intro = fork_random.generate_override("intro", seed)
        drop = fork_random.generate_override("drop", seed)
        assert intro.cut_density < drop.cut_density, seed
        assert intro.motion_bias < drop.motion_bias, seed


# ======================================================================================
# 3. Reproducibility — the whole point of a seeded generator
# ======================================================================================


def test_the_same_seed_and_selections_yield_identical_profiles():
    styles = _all_random()
    first = fork_random.declaration_from_styles(True, styles, _SEED)
    second = fork_random.declaration_from_styles(True, styles, _SEED)
    assert first == second
    assert first.overrides == second.overrides


def test_a_new_seed_can_produce_different_profiles():
    styles = _all_random()
    first = fork_random.declaration_from_styles(True, styles, _SEED)
    second = fork_random.declaration_from_styles(True, styles, _OTHER_SEED)
    assert first != second


def test_the_stream_does_not_depend_on_randomized_string_hashing():
    """Seeded with a *string*, which CPython derives through SHA-512 — never through `hash()`.

    A tuple seed would have been exactly that trap: `hash(str)` is randomised per process by
    PYTHONHASHSEED, so the same seed would generate different sections in a different run.
    """
    # Code only: the docstring legitimately *explains* the `hash()` trap it avoids.
    assert "hash(" not in _code_only(_RANDOM)
    # the stable form, read off the real source: an f-string seed over domain, seed and type
    source = open(_RANDOM, encoding="utf-8").read()
    assert 'random.Random(f"{_STREAM_DOMAIN}|{_seed_token(seed)}|{section_type}")' in source


def test_each_section_type_draws_from_its_own_domain_separated_stream():
    """Adding or reordering a section type cannot shift the numbers another type produced."""
    a = fork_random.stream_for(_SEED, "drop").random()
    b = fork_random.stream_for(_SEED, "intro").random()
    assert a != b
    # and the stream is a pure function of (seed, type)
    assert fork_random.stream_for(_SEED, "drop").random() == a


def test_an_unusable_seed_degrades_to_the_legacy_seed_rather_than_raising():
    for bad in (None, "", "abc", object(), float("nan")):
        rule = fork_random.generate_override("drop", bad)
        assert rule == fork_random.generate_override("drop", 0)


def test_generated_values_go_through_the_one_control_normaliser():
    """A generated control is the same kind of number a slider produces — not a second notion."""
    rule = fork_random.generate_override("chorus", _SEED)
    for name in fork_freestyle.FREESTYLE_CONTROL_FIELDS:
        value = getattr(rule, name)
        assert value == fork_creative.normalize_control(value)


# ======================================================================================
# 4. Manual selections always win, and survive regeneration
# ======================================================================================


def test_manual_selections_are_untouched_by_a_new_seed():
    styles = _all_random()
    styles["intro"] = "Cinematic"
    styles["outro"] = fork_freestyle.BASE_STYLE

    first = fork_random.declaration_from_styles(True, styles, _SEED)
    second = fork_random.declaration_from_styles(True, styles, _OTHER_SEED)

    # the fixed section is byte-identical across seeds...
    assert first.rule_for("intro") == second.rule_for("intro")
    assert first.rule_for("intro") == fork_freestyle.override_from_style("Cinematic")
    # ...Base still means inherit, so it contributes no rule at all...
    assert first.rule_for("outro") is None and second.rule_for("outro") is None
    # ...and a Random section really did move.
    assert first.rule_for("drop") != second.rule_for("drop")


def test_the_preview_lists_only_the_generated_sections():
    styles = _all_random()
    styles["intro"] = "Cinematic"
    text = fork_random.describe_resolved(styles, _SEED)
    assert "intro:" not in text
    assert "drop:" in text
    assert str(_SEED) in text
    for name in fork_freestyle.CONTROL_LABELS.values():
        assert name in text


def test_the_preview_says_so_when_nothing_is_generated():
    styles = {t: fork_freestyle.BASE_STYLE for t in fork_freestyle.SECTION_TYPES}
    assert fork_random.describe_resolved(styles, _SEED) == "No sections are set to Random."


# ======================================================================================
# 5. Repeated section types still share one rule
# ======================================================================================


def test_repeated_section_types_share_one_generated_rule():
    """`REPEATED_SECTION_POLICY` is unchanged: the rule is keyed by TYPE, so two drops get one rule.

    V2 generates per *type*, never per occurrence — there is one draw for `drop`, and every drop in
    the track uses it.
    """
    assert fork_freestyle.REPEATED_SECTION_POLICY == "ALL_INSTANCES_SHARE_RULE"
    declaration = fork_random.declaration_from_styles(True, _all_random(), _SEED)
    assert declaration.rule_for("drop") == declaration.rule_for("drop")
    assert fork_random.generate_override("drop", _SEED) == declaration.rule_for("drop")


def test_no_per_occurrence_concept_was_introduced():
    source = _code_only(_RANDOM).lower()
    for forbidden in ("occurrence", "instance_index", "section_index", "timeline", "boundary"):
        assert forbidden not in source, forbidden


# ======================================================================================
# 6. One resolution, shared by Stage 4 and Stage 6
# ======================================================================================


def test_the_generator_is_reached_from_exactly_one_place_in_auto_mode():
    """Resolved once per render, at the existing conversion, so the two stages cannot disagree."""
    source = open(_AUTO, encoding="utf-8").read()
    assert source.count("fork_freestyle_random.declaration_from_styles") == 1

    tree = _tree(_AUTO)
    resolver = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_resolve_freestyle")
    assert "fork_freestyle_random.declaration_from_styles" in ast.unparse(resolver)
    # the seed arrives as a parameter with a legacy-compatible default
    assert [a.arg for a in resolver.args.args] == ["freestyle", "seed"]
    assert ast.unparse(resolver.args.defaults[-1]) == "0"


def test_the_render_hands_the_global_variation_seed_to_the_resolver():
    source = open(_AUTO, encoding="utf-8").read()
    assert "_resolve_freestyle(freestyle, profile.seed)" in source


def test_stage4_and_stage6_still_read_one_declaration_and_gate_on_is_active():
    """Neither stage learns that a generator exists: they receive an already-resolved declaration."""
    for path in (_AUTO, os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")):
        source = open(path, encoding="utf-8").read()
        assert "freestyle_random" not in source or path == _AUTO
    planner = open(os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py"),
                   encoding="utf-8").read()
    assert "freestyle_random" not in planner
    assert "is_active()" in planner


# ======================================================================================
# 7. Stage-5 identity and the cache are untouched
# ======================================================================================


def test_no_generated_value_can_reach_stage5_identity_or_the_cache():
    source = _code_only(_RANDOM).lower()
    for forbidden in ("cache", "qwen", "_video_signature", "_cache_path", "analysis_version",
                      "video_analysis", "beat_info", "subprocess", "ffmpeg", "open("):
        assert forbidden not in source, forbidden


def test_the_cache_contract_constants_are_unchanged():
    source = open(os.path.join(_REPO_ROOT, "src", "video_analysis.py"), encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_freestyle_py_itself_was_not_modified_by_this_milestone():
    """V2 is a sibling module, so every V1 guarantee keeps holding without being re-argued."""
    source = open(_FREESTYLE, encoding="utf-8").read()
    assert "freestyle_random" not in source
    assert "random" not in source.lower().split("def ")[0].replace("randomised", "")
    assert fork_freestyle.SECTION_STYLE_CHOICES == ("Base", "Balanced", "Cinematic",
                                                    "Dynamic", "High Energy")


def test_the_module_stays_stdlib_only_and_depends_one_way():
    imported = set()
    for node in ast.walk(_tree(_RANDOM)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(f"{node.module}.{a.name}" for a in node.names)
    assert imported == {
        "__future__.annotations", "random",
        "typing.Any", "typing.Mapping",
        "beatsync_fork.creative", "beatsync_fork.freestyle",
    }, sorted(imported)


# ======================================================================================
# 8. The GUI seam — Random is visible, and the dropdowns are never rewritten
# ======================================================================================


def _registration(tree: ast.Module, obj: str, attr: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr
                and isinstance(node.func.value, ast.Name) and node.func.value.id == obj):
            found.append(node)
    return found


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node: ast.AST) -> list[str]:
    return [e.id for e in node.elts if isinstance(e, ast.Name)]


def test_every_section_dropdown_offers_random_and_defaults_to_it():
    source = open(_GUI, encoding="utf-8").read()
    n = len(fork_freestyle.SECTION_TYPES)
    assert source.count("choices=list(fork_freestyle_random.SECTION_STYLE_CHOICES)") == n
    assert source.count("value=fork_freestyle_random.RANDOM_STYLE") == n
    # the V1 default is gone from the dropdowns, so enabling Freestyle is experimental by default
    assert "value=fork_freestyle.BASE_STYLE" not in source


def test_surprise_me_writes_the_seed_and_the_summary_and_no_dropdown():
    """The dropdown is the user's INTENT. Replacing a `Random` entry with the style it produced
    would destroy the distinction between "regenerate me" and "I fixed this"."""
    calls = _registration(_tree(_GUI), "surprise_btn", "click")
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])

    assert ast.unparse(kwargs["fn"]) == "_on_surprise_me"
    assert _names(kwargs["outputs"]) == ["variation_seed", "freestyle_summary"]
    # it READS the eleven live widgets
    assert ast.unparse(kwargs["inputs"]) == "freestyle_live_inputs"
    # and writes no section dropdown: the only Freestyle widget it writes is the read-only summary
    for written in _names(kwargs["outputs"]):
        assert written not in [f"freestyle_{t}" for t in fork_freestyle.SECTION_TYPES], written
    assert "freestyle_enabled" not in _names(kwargs["outputs"])


def test_the_change_graph_still_writes_only_the_summary():
    """V2 adds a button; it must not turn any `.change()` into a widget writer."""
    tree = _tree(_GUI)
    for widget in ["freestyle_enabled"] + [f"freestyle_{t}" for t in fork_freestyle.SECTION_TYPES]:
        for call in _registration(tree, widget, "change"):
            assert _names(_kwargs(call)["outputs"]) == ["freestyle_summary"], widget


def test_the_gui_names_no_banned_speculative_freestyle_concept():
    source = open(_GUI, encoding="utf-8").read().lower()
    for word in ("freestyle_micro", "freestyle_seed", "freestyle_slider", "freestyle_numeric",
                 "freestyle_instance", "freestyle_timeline", "analyze_music",
                 "freestyle_director", "freestyle_variant"):
        assert word not in source, f"gui.py contains {word!r}"


def test_surprise_me_is_honestly_labelled_as_seeded_generation_not_ai():
    """`Random` is arithmetic. Calling it AI would be a false claim about what runs."""
    content = open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8").read()
    start = content.index("INFO_SURPRISE_ME")
    info = content[start:start + 1400].lower()
    assert "not an ai model" in info or "seeded generation, not an ai" in info
    # and it warns that the shared seed moves the whole edit
    assert "variation seed" in info

    # Code only: the module's docstring legitimately explains that it runs none of these.
    generator = _code_only(_RANDOM).lower()
    for forbidden in ("inference", "llama", "model", "prompt", "director", "subprocess"):
        assert forbidden not in generator, forbidden


@pytest.mark.parametrize("style", ["Base", "Balanced", "Cinematic", "Dynamic", "High Energy"])
def test_the_five_v1_choices_keep_their_meaning_and_position(style):
    assert fork_random.SECTION_STYLE_CHOICES.index(style) == \
        fork_freestyle.SECTION_STYLE_CHOICES.index(style)
    assert fork_random.is_random_style(style) is False
