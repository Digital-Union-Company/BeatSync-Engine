"""Creative Presets (Creative Controls Extra PR3): the recipe data, the three total helpers, and
the GUI seam that keeps a preset name a UI-only thing.

A preset is a *name for six numbers*. That makes the whole feature testable on a bare interpreter,
and it makes the interesting assertions boundary assertions rather than behavioural ones:

* **The data** is checked as data — every value a real `int` in range that
  `creative.normalize_control` returns *unchanged*. The recipes are deliberately not normalised at
  definition time, so this suite is what proves they did not need to be. A typo is a test failure,
  not a silently clamped slider.
* **The helpers** are checked for *totality*, because two of the three run on live widget values
  during a UI interaction and neither may ever raise.
* **The seam** is checked with `ast` over `gui.py`, which cannot be imported here. The two things
  worth pinning are the event model (`.input()` in both directions, which is what makes the graph
  acyclic) and the absence of `creative_preset` from the render request, the source gate and
  preparation.

The six sliders remain the sole execution truth; `tests/test_creative_controls_seam.py` and
`tests/test_creative_profile.py` own the proof that nothing downstream of them changed.
"""

from __future__ import annotations

import ast
import os
import re
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import presets as fork_presets

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_PRESETS = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "presets.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")

#: The accepted recipe table, restated here independently of the module under test. Measured on real
#: material during the design probe and frozen: a change to any number must be a deliberate edit in
#: two places, not a drive-by retune in one.
_ACCEPTED = {
    "Balanced":    {"cut_density": 50,  "micro_cuts": 50, "semantic_emphasis": 50,
                    "energy_response": 50, "motion_bias": 50, "source_diversity": 50},
    "Cinematic":   {"cut_density": 30,  "micro_cuts": 25, "semantic_emphasis": 65,
                    "energy_response": 40, "motion_bias": 30, "source_diversity": 50},
    "Dynamic":     {"cut_density": 65,  "micro_cuts": 60, "semantic_emphasis": 50,
                    "energy_response": 70, "motion_bias": 70, "source_diversity": 75},
    "High Energy": {"cut_density": 100, "micro_cuts": 85, "semantic_emphasis": 50,
                    "energy_response": 85, "motion_bias": 80, "source_diversity": 80},
}

_FIELDS = ("cut_density", "micro_cuts", "semantic_emphasis",
           "energy_response", "motion_bias", "source_diversity")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _strip_docstrings(node):
    """A deep copy with every docstring removed, so prose can neither satisfy nor fail a check.

    `presets.py`'s module docstring legitimately names the Variation Seed, precisely in order to say
    that no preset may move it.
    """
    import copy

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


def _gui_tree() -> ast.Module:
    return _tree(_GUI)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _registration(tree, widget: str, attrs=("click", "change", "input", "submit", "release")):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in attrs
            and isinstance(node.func.value, ast.Name) and node.func.value.id == widget]


def _kwargs(call):
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node):
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _widget_call(tree, name: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no widget assignment for {name}")


# ===========================================================================
# 1. THE RECIPE DATA
# ===========================================================================


def test_there_are_exactly_four_named_recipes():
    assert sorted(fork_presets.PRESETS) == ["Balanced", "Cinematic", "Dynamic", "High Energy"]
    assert len(fork_presets.PRESETS) == 4


def test_custom_is_not_a_recipe():
    """`Custom` is the selector reporting "these values match no recipe", so it must have no entry —
    otherwise selecting it would write something."""
    assert fork_presets.CUSTOM_PRESET == "Custom"
    assert fork_presets.CUSTOM_PRESET not in fork_presets.PRESETS
    assert fork_presets.resolve_preset(fork_presets.CUSTOM_PRESET) is None
    assert fork_presets.preset_values(fork_presets.CUSTOM_PRESET) is None


def test_preset_names_are_the_four_recipes_then_custom():
    """Selector order. Custom is last because it is a state the selector reports, not a choice."""
    assert fork_presets.PRESET_NAMES == ("Balanced", "Cinematic", "Dynamic", "High Energy",
                                         "Custom")
    assert fork_presets.BALANCED_PRESET == "Balanced"
    assert fork_presets.PRESET_NAMES[0] == fork_presets.BALANCED_PRESET
    assert fork_presets.PRESET_NAMES[-1] == fork_presets.CUSTOM_PRESET


def test_the_control_field_tuple_is_exactly_the_six_in_order():
    assert fork_presets.CREATIVE_CONTROL_FIELDS == _FIELDS


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_every_recipe_contains_exactly_the_six_control_fields(name):
    """No missing field silently inheriting a default, and no extra field smuggling in state."""
    assert set(fork_presets.PRESETS[name]) == set(_FIELDS)
    assert len(fork_presets.PRESETS[name]) == len(_FIELDS)


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_every_recipe_matches_the_accepted_table_exactly(name):
    assert dict(fork_presets.PRESETS[name]) == _ACCEPTED[name]


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_every_value_is_a_real_int_in_range(name):
    """`bool` is rejected explicitly because it subclasses `int`: `True` in a recipe would read as
    1 — "almost maximally sparse" — from a table that never meant to say that."""
    for field, value in fork_presets.PRESETS[name].items():
        assert isinstance(value, int), f"{name}.{field} is {type(value).__name__}"
        assert not isinstance(value, bool), f"{name}.{field} is a bool"
        assert fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX, f"{name}.{field}"


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_normalize_control_returns_every_recipe_value_unchanged(name):
    """The reason the recipes are plain literals rather than normalised at definition time: this
    suite proves they did not need coercing, so a typo surfaces here instead of being clamped into
    a slider position the author never chose."""
    for field, value in fork_presets.PRESETS[name].items():
        assert fork_creative.normalize_control(value) == value, f"{name}.{field}"


def test_balanced_is_exactly_six_fifties():
    """It is the reset-to-current-behaviour preset, so it must be the neutral value of every control
    read from the fork constant rather than from a literal here."""
    assert dict(fork_presets.PRESETS["Balanced"]) == {
        field: fork_creative.DEFAULT_CONTROL for field in _FIELDS}
    assert fork_presets.preset_values("Balanced") == (50,) * 6


def test_balanced_resolves_through_the_profile_to_legacy():
    """The product contract behind "Balanced is current BeatSync": the profile built from this
    recipe at the default seed must be the *neutral* one, by its own definition of neutral."""
    profile = fork_creative.CreativeProfile(**dict(fork_presets.PRESETS["Balanced"]))

    assert profile.is_neutral()
    assert profile.describe() == "legacy"
    assert profile.filename_suffix() == ""
    assert profile.scoring_controls() is fork_creative.NEUTRAL_SCORING
    assert profile.is_neutral_cuts() and profile.is_neutral_micro_cuts()
    assert profile.is_neutral_scoring() and profile.is_neutral_source_diversity()


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_no_recipe_is_neutral_except_balanced(name):
    """A named preset that resolved to legacy would be a second Balanced under another label."""
    profile = fork_creative.CreativeProfile(**dict(fork_presets.PRESETS[name]))
    assert profile.is_neutral() == (name == "Balanced")


def test_the_intentional_non_diagonals_are_pinned():
    """The recipes are coherent recipes, not a diagonal through every slider. Each of these three
    values is a deliberate design decision, so each gets its own assertion.

    * Cinematic is the only preset that moves Semantic Emphasis — the only name that genuinely
      implies contextual reading.
    * Cinematic leaves Source Diversity neutral: ~20% fewer cuts already lowers source-reuse
      pressure, so the control is not spent where it was not needed.
    * Dynamic and High Energy leave Semantic Emphasis neutral: Stage 5 motion-gates semantic
      action, so on the action material a dense edit selects, moving it would be near-inert.
    """
    assert fork_presets.PRESETS["Cinematic"]["semantic_emphasis"] == 65
    assert fork_presets.PRESETS["Cinematic"]["source_diversity"] == fork_creative.DEFAULT_CONTROL
    assert fork_presets.PRESETS["Dynamic"]["semantic_emphasis"] == fork_creative.DEFAULT_CONTROL
    assert fork_presets.PRESETS["High Energy"]["semantic_emphasis"] == fork_creative.DEFAULT_CONTROL

    moved = [name for name, recipe in fork_presets.PRESETS.items()
             if recipe["semantic_emphasis"] != fork_creative.DEFAULT_CONTROL]
    assert moved == ["Cinematic"]


def test_high_energy_is_not_every_slider_at_maximum():
    """The owner decision put Cut Density at 100 so the strongest named pacing recipe is actually
    distinct from Dynamic. That is one control at its limit, not a preset meaning "everything up"."""
    recipe = fork_presets.PRESETS["High Energy"]
    assert recipe["cut_density"] == fork_creative.CONTROL_MAX
    at_max = [f for f, v in recipe.items() if v == fork_creative.CONTROL_MAX]
    assert at_max == ["cut_density"]


def test_the_pacing_ladder_is_ordered():
    """Cut Density is what the four names promise first, so the recipes must at least be ordered in
    it. (How that maps to cut *counts* is Stage 4's business and was measured in design; this is the
    cheap invariant that survives in a stdlib-only test.)"""
    densities = [fork_presets.PRESETS[n]["cut_density"]
                 for n in ("Cinematic", "Balanced", "Dynamic", "High Energy")]
    assert densities == sorted(densities)
    assert len(set(densities)) == 4


# ===========================================================================
# 2. SEED INDEPENDENCE
# ===========================================================================


def test_no_recipe_carries_a_seed():
    assert "seed" not in _FIELDS
    for name, recipe in fork_presets.PRESETS.items():
        assert "seed" not in recipe, name
        assert "variation_seed" not in recipe, name
        assert fork_presets.resolve_preset(name).keys() == set(_FIELDS)


def test_the_preset_module_has_no_seed_concept_at_all():
    """Docstrings excluded, because the module docstring says the seed is independent precisely in
    order to document that it is."""
    source = _executable_source(_PRESETS).lower()
    for word in ("seed", "variation", "random", "randomize", "randomise"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"presets.py mentions {word!r}"


def test_applying_any_preset_leaves_the_profile_seed_alone():
    """Executable version of the same thing: a recipe splatted onto a seeded profile must not be
    able to disturb the seed, because it carries no key that could."""
    for name in fork_presets.PRESETS:
        profile = fork_creative.CreativeProfile(seed=381944,
                                                **dict(fork_presets.PRESETS[name]))
        assert profile.seed == 381944, name
        assert profile.filename_suffix() == "_seed381944", name


# ===========================================================================
# 3. IMMUTABILITY
# ===========================================================================


def test_the_recipe_table_cannot_be_mutated_by_a_caller():
    with pytest.raises(TypeError):
        fork_presets.PRESETS["Nope"] = {}
    with pytest.raises(TypeError):
        del fork_presets.PRESETS["Balanced"]
    with pytest.raises(TypeError):
        fork_presets.PRESETS["Balanced"]["cut_density"] = 1

    assert fork_presets.PRESETS["Balanced"]["cut_density"] == 50
    assert sorted(fork_presets.PRESETS) == ["Balanced", "Cinematic", "Dynamic", "High Energy"]


def test_resolve_preset_hands_back_a_fresh_dict_each_time():
    """So a caller may freely mutate the result without reaching the table it came from."""
    first = fork_presets.resolve_preset("Cinematic")
    second = fork_presets.resolve_preset("Cinematic")

    assert first == second
    assert first is not second

    first["cut_density"] = 999
    assert fork_presets.PRESETS["Cinematic"]["cut_density"] == 30
    assert fork_presets.resolve_preset("Cinematic")["cut_density"] == 30


# ===========================================================================
# 4. resolve_preset / preset_values — TOTAL
# ===========================================================================


def test_resolve_preset_returns_the_six_values_for_a_named_recipe():
    assert fork_presets.resolve_preset("Balanced") == _ACCEPTED["Balanced"]
    assert fork_presets.resolve_preset("High Energy") == _ACCEPTED["High Energy"]


def test_preset_names_are_matched_exactly_and_never_case_normalised():
    """Accepting a different spelling would make the displayed value and the applied recipe two
    separately-decided things; this layer exists because they are one."""
    for spelling in ("cinematic", "CINEMATIC", "Cinematic ", " Cinematic", "high energy",
                     "HighEnergy", "High  Energy", "balanced"):
        assert fork_presets.resolve_preset(spelling) is None, spelling
        assert fork_presets.preset_values(spelling) is None, spelling


@pytest.mark.parametrize("value", [
    None, "", "Custom", "nope", 0, 1, 7, 50.0, True, False, [], {}, (), object(),
    ["Balanced"], {"Balanced": 1}, b"Balanced", bytearray(b"Balanced"),
])
def test_resolve_preset_is_total(value: Any):
    """It runs on whatever the selector happens to hold, so it answers rather than raising."""
    assert fork_presets.resolve_preset(value) is None
    assert fork_presets.preset_values(value) is None


def test_preset_values_is_ordered_by_the_control_field_tuple():
    """Gradio writes `outputs` positionally, so this ordering is the contract with the slider list
    in gui.py — restated from the one field tuple rather than hard-coded twice."""
    for name, recipe in _ACCEPTED.items():
        assert fork_presets.preset_values(name) == tuple(recipe[f] for f in _FIELDS)

    # and the ordering is load-bearing: Cinematic's values distinguish all six positions
    assert fork_presets.preset_values("Cinematic") == (30, 25, 65, 40, 30, 50)


# ===========================================================================
# 5. matching_preset — TOTAL, AND AN HONEST READ-OUT
# ===========================================================================


@pytest.mark.parametrize("name", sorted(_ACCEPTED))
def test_matching_preset_round_trips_every_recipe(name):
    assert fork_presets.matching_preset(fork_presets.preset_values(name)) == name


def test_matching_preset_accepts_the_float_values_a_slider_reports():
    """A Gradio slider hands back `50.0`, not `50`. If that read as Custom, every preset would flip
    its own label to Custom the instant the user touched any slider."""
    for name, recipe in _ACCEPTED.items():
        floats = tuple(float(recipe[f]) for f in _FIELDS)
        assert fork_presets.matching_preset(floats) == name


def test_matching_preset_accepts_any_ordinary_sequence():
    assert fork_presets.matching_preset([50] * 6) == "Balanced"
    assert fork_presets.matching_preset(iter([30, 25, 65, 40, 30, 50])) == "Cinematic"


def test_one_control_off_by_one_is_custom():
    """The whole point of recomputing the label: the moment the six values stop being a recipe, the
    selector must say so, and it must say so for *any* of the six."""
    for position, field in enumerate(_FIELDS):
        for delta in (-1, +1):
            values = list(fork_presets.preset_values("Cinematic"))
            values[position] += delta
            if not (fork_creative.CONTROL_MIN <= values[position] <= fork_creative.CONTROL_MAX):
                continue
            assert fork_presets.matching_preset(tuple(values)) == "Custom", (field, delta)


def test_returning_exactly_to_a_recipe_reports_that_recipe_again():
    """There is no remembered "last selected preset": the label is derived from the live values, so
    a manual edit and a manual edit back are symmetric."""
    edited = list(fork_presets.preset_values("Dynamic"))
    edited[4] = 55
    assert fork_presets.matching_preset(tuple(edited)) == "Custom"

    edited[4] = _ACCEPTED["Dynamic"]["motion_bias"]
    assert fork_presets.matching_preset(tuple(edited)) == "Dynamic"


def test_a_bool_anywhere_is_custom():
    """`bool` subclasses `int`, so `True == 1`. No recipe holds a 0 or 1 today, but relying on that
    would make a future recipe value of 1 silently matchable by a checkbox-shaped value."""
    for position in range(6):
        values = list(fork_presets.preset_values("Balanced"))
        values[position] = True
        assert fork_presets.matching_preset(tuple(values)) == "Custom", position
        values[position] = False
        assert fork_presets.matching_preset(tuple(values)) == "Custom", position


class _Exploding:
    """An object whose every interrogation raises — the worst case a widget value could be."""

    def __len__(self):
        raise RuntimeError("len")

    def __iter__(self):
        raise RuntimeError("iter")

    def __eq__(self, other):
        raise RuntimeError("eq")

    def __hash__(self):
        raise RuntimeError("hash")


@pytest.mark.parametrize("value", [
    None, 0, 50, 50.0, True, False, "", "Balanced", "505050", b"505050", bytearray(b"505050"),
    (), (50,), (50,) * 5, (50,) * 7, [50] * 7, object(), _Exploding(), [_Exploding()] * 6,
    {"cut_density": 50}, {50: 1, 25: 1, 65: 1, 40: 1, 30: 1},
    (50, 50, 50, 50, 50, "50"), (50, 50, 50, 50, 50, None), (50.5,) * 6,
    (float("nan"),) * 6, (float("inf"),) * 6, (-1,) * 6, (101,) * 6,
])
def test_matching_preset_is_total(value: Any):
    """It runs on live widget values during a UI interaction, so it must answer, never raise."""
    assert fork_presets.matching_preset(value) == "Custom"


def test_a_six_character_string_is_not_read_as_six_controls():
    """`"505050"` is a sequence of length six; iterating it would compare characters."""
    assert fork_presets.matching_preset("505050") == "Custom"


def test_a_mapping_is_never_read_as_a_control_tuple():
    """Iterating a dict yields its keys, so a dict with six numeric keys could otherwise match."""
    assert fork_presets.matching_preset(dict.fromkeys([50, 50, 50, 50, 50, 50])) == "Custom"
    assert fork_presets.matching_preset(_ACCEPTED["Balanced"]) == "Custom"


def test_matching_preset_always_returns_a_selector_choice():
    """Whatever it answers has to be a value the Radio can actually hold."""
    for value in (None, (50,) * 6, (30, 25, 65, 40, 30, 50), object(), [1, 2, 3]):
        assert fork_presets.matching_preset(value) in fork_presets.PRESET_NAMES


# ===========================================================================
# 6. MODULE PURITY
# ===========================================================================


def test_the_preset_module_imports_only_a_few_stdlib_names():
    """Belt to `test_no_runtime_dependency.py`'s braces, and more specific: a preset table has no
    business reaching the planner, the profile, Gradio, numpy or the filesystem.

    `creative.py` is excluded deliberately — the recipes are inert data that the existing
    `normalize_control` boundary happens to accept unchanged, and this suite proves that rather than
    the module enforcing it by coercion.
    """
    tree = _tree(_PRESETS)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    assert imported <= {"__future__", "collections.abc", "types", "typing"}, sorted(imported)


def test_the_preset_module_names_no_pipeline_or_ui_machinery():
    source = _executable_source(_PRESETS).lower()
    for word in ("gradio", "numpy", "auto_mode", "stage4_select", "stage6_av_planner",
                 "video_analysis", "video_processor", "creativeprofile", "normalize_control",
                 "beat_info", "render_info", "os", "open", "cache"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"presets.py mentions {word!r}"


#: Still speculative everywhere, including the GUI: Freestyle, an AI Director, L2 stage caching,
#: source groups, per-section profiles, and any remembered preset state.
_STILL_SPECULATIVE = ("freestyle", "director", "shortlist", "stage_cache", "source_group",
                      "per_section_profile", "preset_history", "last_preset", "preset_state")

#: Variant Lab's own vocabulary. `presets.py` must still know none of it — a preset stays a named
#: set of slider values with no generator concept — but `gui.py` legitimately wires the lab up.
_VARIANT_LAB_VOCABULARY = ("variant_lab", "creative_recipe", "master_seed", "variation_spread")


def test_presets_module_knows_nothing_about_generators_or_future_modes():
    """**Amended by Variant Lab V1 (C2).** This forbade Variant Lab vocabulary in `presets.py` *and*
    `gui.py`. C2 legitimately adds a Variant Lab to the GUI, so the assertion split rather than
    weakened: `presets.py` keeps the full prohibition — presets remain recipes-only and must never
    grow a generator — while the GUI keeps every prohibition that is still speculative.

    Deliberately not evaded by renaming: the lab is called Variant Lab in the GUI, and this test now
    says so out loud.
    """
    source = _executable_source(_PRESETS).lower()
    for word in _STILL_SPECULATIVE + _VARIANT_LAB_VOCABULARY:
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"presets.py mentions {word!r}"


#: Multi-variant vocabulary that C3 V1 **did not** implement and that must not drift in beside the
#: part it did. `num_variants` and `variant_count` are banned as spellings, not as ideas: C3's own
#: control is the explicit `variant_candidate_count`, and keeping the shorter names out stops a
#: second, differently-bounded count appearing next to it. `compare_variants` and `variant_gallery`
#: are the *rendered* comparison this milestone deliberately refused — a gallery implies rendered
#: thumbnails, which is batch rendering wearing a different hat.
_C3_STILL_SPECULATIVE = ("num_variants", "variant_count", "compare_variants", "variant_gallery",
                         "variant_preview", "thumbnail")

#: **Split again by C3-R0.** `render_batch` and `batch_render` left the ban list because C3-R0
#: implements exactly one of them — rendering two explicitly selected candidates, sequentially,
#: through the existing pipeline. What stayed banned is what C3-R0 deliberately did *not* build: a
#: rendered gallery, thumbnails, a second count, and automatic rendering of a whole comparison.
#: The token list is the weak half of this guard either way; the structural assertion below is the
#: one a rename cannot evade.
_C3_R0_ACCEPTED_IN_GUI = ("render_batch", "render_selected_variants")

#: What C3 V1 legitimately added, by name. Listed explicitly rather than simply removed from the
#: ban, so the accepted surface is a reviewed allow-list and a *fourth* multi-variant concept
#: cannot arrive unnoticed.
_C3_ACCEPTED_IN_GUI = ("generate_variants", "variant_batch")


def test_the_gui_added_no_speculative_mode_machinery():
    """Freestyle, an AI Director, source groups, preset history and the rest stay out of the GUI.

    **Split by Variant Lab C3 V1, not deleted** — the same reconciliation C2 applied to this
    file's `presets.py`/`gui.py` pair, and the same one C3 applied to
    `test_variant_lab.py::test_no_multi_variant_or_c3_machinery_was_added`.

    This test used to forbid *all* multi-variant vocabulary in `gui.py`, including
    `generate_variants` and `variant_batch`. C3 V1 shipped exactly those two concepts — N
    deterministic candidates, compared and applied one at a time, rendering nothing — so the
    blanket form became false. It was split rather than dropped, and rather than evaded: renaming
    C3's handler and state purely to slip past a boundary guard would be the dishonest fix, and
    the names are kept.

    `_STILL_SPECULATIVE` is untouched and still applies at full strength. What C3 added is an
    allow-list, and everything multi-variant that C3 deliberately did *not* build — a rendered
    gallery, a second count, batch rendering — is now banned by name here rather than incidentally.
    """
    source = _executable_source(_GUI).lower()
    for word in _STILL_SPECULATIVE:
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"gui.py mentions {word!r}"
    for word in _C3_STILL_SPECULATIVE:
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"gui.py mentions {word!r}"

    # the allow-list is a statement about what C3 *is*, so it has to actually be there
    for word in _C3_ACCEPTED_IN_GUI:
        assert word in source, f"gui.py lost C3's {word!r}"
    for word in _C3_R0_ACCEPTED_IN_GUI:
        assert word in source, f"gui.py lost C3-R0's {word!r}"


def test_the_accepted_c3_machinery_is_not_rendering_machinery():
    """The structural half, because a shrinking token ban is the weak half of any boundary guard.

    `test_variant_lab.py` owns C3's full structural contract; this is the narrow property *this*
    file is responsible for, since it is where the "no speculative mode machinery" prohibition
    lives: the two concepts C3 was allowed to add must not have arrived as a way to render.
    """
    tree = _gui_tree()
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    render_entry_points = {"process_video_guarded", "process_video", "_process_video_impl",
                           "analyze_beats_auto", "create_music_video"}

    for entry in ("_on_generate_variants", "_on_apply_selected_variant"):
        # C3-R0's render wrapper is deliberately absent from this list: rendering is exactly what
        # it is for. Its boundary — one mutex, one shared gate core, no cancellation — is pinned
        # in `tests/test_gui_guard_seam.py`.
        assert entry in defined, f"{entry} is missing"
        seen, pending = set(), [entry]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            node = defined.get(name)
            if node is None:
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    leaf = ast.unparse(call.func).rsplit(".", 1)[-1]
                    assert leaf not in render_entry_points, f"{entry} reaches {leaf}"
                    if leaf in defined:
                        pending.append(leaf)


# ===========================================================================
# 7. THE GUI SELECTOR
# ===========================================================================


def test_the_selector_is_a_radio_with_exactly_the_five_choices_defaulting_to_balanced():
    kwargs = _kwargs(_widget_call(_gui_tree(), "creative_preset"))
    call = _widget_call(_gui_tree(), "creative_preset")

    assert ast.unparse(call.func) == "gr.Radio"
    assert ast.unparse(kwargs["choices"]) == "list(fork_presets.PRESET_NAMES)"
    assert ast.unparse(kwargs["value"]) == "fork_presets.BALANCED_PRESET"
    # the label and help text come from ui_content, like every other control
    assert ast.unparse(kwargs["label"]) == "LABEL_CREATIVE_PRESET"
    assert ast.unparse(kwargs["info"]) == "INFO_CREATIVE_PRESET"


def test_the_choices_really_are_the_four_recipes_plus_custom():
    """The widget reads them from the fork module, so this pins what that module exposes rather
    than re-reading the literal out of gui.py."""
    assert list(fork_presets.PRESET_NAMES) == ["Balanced", "Cinematic", "Dynamic", "High Energy",
                                               "Custom"]
    assert fork_presets.BALANCED_PRESET in fork_presets.PRESET_NAMES


def test_its_help_text_describes_a_preset_honestly():
    """The label and help text live in `ui_content`, like every other control. The text is checked
    because it is the only place a user learns what a preset is, and over-promising there (that it
    is remembered, or that it re-analyses) would misdescribe the whole design."""
    source = open(_UI_CONTENT, encoding="utf-8").read()
    assert "LABEL_CREATIVE_PRESET" in source

    start = source.index("INFO_CREATIVE_PRESET")
    info = source[start:source.index("\n)", start)].lower()

    assert "custom" in info, "the help text must say when the selector reads Custom"
    assert "seed" in info, "the help text must say a preset never changes the seed"
    assert "balanced" in info, "the help text must name the way back to current behaviour"
    for overclaim in ("saved", "remembered", "stored", "mode"):
        assert overclaim not in info, f"INFO_CREATIVE_PRESET claims {overclaim!r}"


def test_the_selector_lives_in_the_creative_direction_group():
    """Grouped with the controls it writes, not inside Video Source: placement is the first
    statement that this is render-request creative state."""
    source = open(_GUI, encoding="utf-8").read()
    heading = source.index("Creative Direction")
    following = source.index("Processing Mode", heading)
    position = source.index("creative_preset = gr.", heading)

    assert heading < position < following
    # and directly above the six sliders it writes
    assert position < source.index("cut_density = gr.", heading)


# ===========================================================================
# 8. THE EVENT MODEL
# ===========================================================================


def test_the_preset_writes_exactly_the_six_sliders_on_input():
    calls = _registration(_gui_tree(), "creative_preset")
    assert len(calls) == 1, f"creative_preset registers {len(calls)} handlers"

    call = calls[0]
    assert call.func.attr == "input", f"the selector uses .{call.func.attr}()"

    kwargs = _kwargs(call)
    assert ast.unparse(kwargs["fn"]) == "_on_preset_input"
    assert _names(kwargs["inputs"]) == ["creative_preset"]
    assert _names(kwargs["outputs"]) == ["creative_control_sliders"]


@pytest.mark.parametrize("widget", _FIELDS)
def test_each_slider_syncs_the_label_on_input_and_writes_nothing_else(widget):
    calls = _registration(_gui_tree(), widget)
    assert len(calls) == 1, f"{widget} registers {len(calls)} handlers"

    call = calls[0]
    assert call.func.attr == "input", f"{widget} uses .{call.func.attr}()"

    kwargs = _kwargs(call)
    assert ast.unparse(kwargs["fn"]) == "_on_creative_control_input"
    assert _names(kwargs["inputs"]) == ["creative_control_sliders"]
    assert _names(kwargs["outputs"]) == ["creative_preset"]


def test_no_preset_sync_is_registered_on_change_anywhere():
    """`.change()` fires for programmatic updates as well as user ones, so a `.change()` binding on
    any of these seven widgets is exactly how these two handlers would become an event loop. The
    acyclicity of this graph is a property of the event *kind*, so it is pinned as one."""
    tree = _gui_tree()
    for widget in ("creative_preset",) + _FIELDS:
        for call in _registration(tree, widget, attrs=("change", "release", "submit")):
            raise AssertionError(f"{widget}.{call.func.attr}() would reintroduce the cycle")


def test_neither_handler_is_routed_through_gr_on():
    """`gr.on(triggers=[slider.input, ...])` would register the same behaviour without the binding
    being visible on the widget, which would quietly satisfy the per-widget seam assertions above
    without them checking anything. Explicit per-widget registration is the contract."""
    source = _executable_source(_GUI)
    assert "gr.on(" not in source


def test_the_slider_list_is_the_six_controls_in_field_order():
    """It is the preset handler's `outputs` and every slider handler's `inputs`, and Gradio matches
    those positionally — so its order *is* the contract with `CREATIVE_CONTROL_FIELDS`."""
    assignment = next(n for n in ast.walk(_gui_tree()) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", None) == "creative_control_sliders")
    assert _names(assignment.value) == list(fork_presets.CREATIVE_CONTROL_FIELDS)


# ===========================================================================
# 9. THE TWO HANDLERS
# ===========================================================================


def test_the_preset_handler_takes_the_name_and_returns_six_values():
    tree = _gui_tree()
    fn = _func(tree, "_on_preset_input")
    assert [a.arg for a in fn.args.args] == ["preset_name"]

    body = ast.unparse(_strip_docstrings(fn))
    assert "fork_presets.preset_values(preset_name)" in body
    # Custom resolves to None, and None must write nothing at all
    assert "gr.skip()" in body
    assert "variation_seed" not in body


def test_the_slider_handler_reads_all_six_and_returns_only_the_label():
    tree = _gui_tree()
    fn = _func(tree, "_on_creative_control_input")
    assert [a.arg for a in fn.args.args] == list(_FIELDS), (
        "parameter order must mirror CREATIVE_CONTROL_FIELDS; Gradio passes inputs positionally")

    body = ast.unparse(_strip_docstrings(fn))
    assert "fork_presets.matching_preset(" in body
    for field in _FIELDS:
        assert field in body, f"{field} is accepted but never used"


def test_selecting_custom_changes_no_slider():
    """Executable version of the handler's `None` branch: `Custom` has no recipe, so there is
    nothing to write, and the handler must produce one skip per slider rather than six values."""
    assert fork_presets.preset_values("Custom") is None
    assert fork_presets.preset_values(None) is None
    # the handler turns exactly that into "write nothing", one entry per control
    fn = _func(_gui_tree(), "_on_preset_input")
    body = ast.unparse(_strip_docstrings(fn))
    assert "for _ in fork_presets.CREATIVE_CONTROL_FIELDS" in body


def test_neither_handler_touches_state_or_the_pipeline():
    tree = _gui_tree()
    for name in ("_on_preset_input", "_on_creative_control_input"):
        body = ast.unparse(_strip_docstrings(_func(tree, name)))
        for forbidden in ("source_state", "prep_state", "session_state", "process_video",
                          "resolve_for_render", "live_declaration", "analyze_beats_auto",
                          "CreativeProfile", "create_music_video", "scan_folder"):
            assert forbidden not in body, f"{name} references {forbidden}"


# ===========================================================================
# 10. ISOLATION: THE RENDER REQUEST, THE SOURCE GATE AND PREPARATION
# ===========================================================================


def test_the_selector_is_not_a_render_request_input():
    """The six sliders already are, and they stay the only creative values the render carries. A
    preset name reaching the handler would be a second source of truth for the same six numbers."""
    tree = _gui_tree()
    calls = _registration(tree, "process_btn", attrs=("click",))
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])

    inputs = [n.id for n in kwargs["inputs"].elts if isinstance(n, ast.Name)]
    assert "creative_preset" not in inputs
    for field in _FIELDS:
        assert field in inputs, f"{field} stopped being a render-request input"


def test_the_guard_and_the_pipeline_never_see_a_preset_name():
    tree = _gui_tree()
    for name in ("process_video_guarded", "process_video", "_process_video_impl"):
        fn = _func(tree, name)
        assert "creative_preset" not in [a.arg for a in fn.args.args], name
        body = ast.unparse(_strip_docstrings(fn))
        for word in ("creative_preset", "fork_presets", "preset_values", "matching_preset"):
            assert word not in body, f"{name} references {word}"


def test_no_preset_name_reaches_the_profile_or_the_creative_bus():
    """Executable: the profile has no field a name could land in, and a bus carrying one is
    ignored rather than half-honoured."""
    profile = fork_creative.CreativeProfile()
    assert not hasattr(profile, "preset")
    assert "preset" not in profile.as_dict()

    seeded = fork_creative.CreativeProfile.from_mapping(
        {"seed": 5, "preset": "Cinematic", "creative_preset": "Dynamic"})
    assert seeded == fork_creative.CreativeProfile(seed=5)
    assert "preset" not in seeded.as_dict()


def test_the_selector_is_absent_from_the_source_and_preparation_wiring():
    tree = _gui_tree()
    for list_name in ("source_outputs", "prep_outputs"):
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == list_name)
        assert "creative_preset" not in _names(assignment.value), list_name

    for widget in ("source_mode", "source_folder", "source_recursive", "scan_btn", "video_input",
                   "confirm_btn", "prep_folder", "prep_recursive", "prep_batch_size",
                   "prep_scan_btn", "prep_analyze_btn"):
        for call in _registration(tree, widget, attrs=("click", "change")):
            kwargs = _kwargs(call)
            for key in ("inputs", "outputs"):
                if key in kwargs:
                    assert "creative_preset" not in _names(kwargs[key]), f"{widget}.{key}"


def test_no_preset_handler_writes_a_gate_or_preparation_widget():
    """The one assertion that makes "preset selection cannot invalidate a confirmation" structural
    rather than a claim: neither registration's outputs can reach the gate at all."""
    tree = _gui_tree()
    gate = ("source_outputs", "source_report", "confirm_btn", "confirm_status", "process_btn",
            "source_state", "prep_outputs", "prep_report", "prep_status", "prep_analyze_btn",
            "prep_state", "session_state", "video_output", "status_output")

    registrations = _registration(tree, "creative_preset")
    for field in _FIELDS:
        registrations.extend(_registration(tree, field))
    assert len(registrations) == 7, "expected one selector handler plus one per slider"

    for call in registrations:
        outputs = _names(_kwargs(call)["outputs"])
        for forbidden in gate:
            assert forbidden not in outputs, f"{ast.unparse(call.func)} writes {forbidden}"


def test_randomize_is_still_seed_only_and_does_not_move_the_selector():
    calls = _registration(_gui_tree(), "randomize_btn", attrs=("click",))
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])

    assert ast.unparse(kwargs["fn"]) == "fork_variation.random_seed"
    assert _names(kwargs["inputs"]) == []
    assert _names(kwargs["outputs"]) == ["variation_seed"]


def test_the_seed_box_registers_no_preset_handler():
    """Seed independence, from the other direction: moving the seed must not relabel the preset,
    because the seed is not part of any recipe."""
    assert _registration(_gui_tree(), "variation_seed") == []


# ===========================================================================
# 11. NO PLANNER, PIPELINE OR CLI CHANGE
# ===========================================================================


_UNTOUCHED = (
    os.path.join("src", "video_analysis.py"),
    os.path.join("src", "video_processor.py"),
    os.path.join("src", "auto_mode", "__init__.py"),
    os.path.join("src", "auto_mode", "stage4_select.py"),
    os.path.join("src", "auto_mode", "stage6_av_planner.py"),
    os.path.join("src", "auto_mode", "stage5_qwen_scene_worker.py"),
    os.path.join("src", "beatsync_fork", "creative.py"),
    os.path.join("src", "beatsync_fork", "variation.py"),
    os.path.join("src", "beatsync_fork", "deterministic_view.py"),
    os.path.join("src", "beatsync_fork", "library_prep.py"),
)


#: The identifiers this feature travels under. Deliberately NOT the bare word "preset": Stage 4 has
#: carried an unrelated `smart_preset` on the audio profile since long before this PR (and prints
#: "Smart preset:"), so forbidding that word would assert something false. These tokens cannot
#: collide with it — `\bpresets\b` does not match `smart_preset`.
_PRESET_TOKENS = ("presets", "fork_presets", "creative_preset", "matching_preset",
                  "resolve_preset", "preset_values", "preset_names", "balanced_preset",
                  "custom_preset", "creative_control_fields")


@pytest.mark.parametrize("relative", _UNTOUCHED)
def test_no_planner_or_pipeline_file_has_heard_of_presets(relative):
    """A preset is a UI value setter. If any of these files learned the feature, it would have
    stopped being one and become a mode."""
    source = _executable_source(os.path.join(_REPO_ROOT, relative)).lower()
    for word in _PRESET_TOKENS:
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"{relative} mentions {word!r}"


@pytest.mark.parametrize("relative", _UNTOUCHED)
def test_no_planner_or_pipeline_file_imports_the_preset_module(relative):
    """Not even for a type hint: an import is the first step towards a use."""
    tree = _tree(os.path.join(_REPO_ROOT, relative))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "presets" not in ast.unparse(node), f"{relative}: {ast.unparse(node)}"


def test_no_cli_preset_flag_was_added():
    """The CLI already exposes all six controls, and six explicit numbers are the reproducible
    contract. A `--preset` alias would add precedence ambiguity (`--preset X --motion-bias 70`) and
    recipe-name versioning ambiguity for no gain."""
    source = open(os.path.join(_REPO_ROOT, "src", "video_processor.py"), encoding="utf-8").read()
    assert "--preset" not in source


def test_the_six_cli_flags_still_exist():
    """Stated positively, since they are the reason no preset flag is needed."""
    source = open(os.path.join(_REPO_ROOT, "src", "video_processor.py"), encoding="utf-8").read()
    for flag in ("--cut-density", "--micro-cuts", "--semantic-emphasis", "--energy-response",
                 "--motion-bias", "--source-diversity"):
        assert flag in source, flag
