"""Variant Lab V1: the named RNG sub-streams, the frozen spread formula, and the GUI seam.

The interesting failures here are all *silent* ones, so the suite is built around them:

* **Golden vectors.** The RNG key format is a promise to every user who has written a master seed
  down. A change to the namespace, the separator, the digest, the hex slice or where the algorithm
  version sits would keep producing perfectly good recipes — just different ones. So the derived
  integers are pinned as literals and recomputed independently; no test here merely calls the same
  helper twice.
* **Independence.** With named sub-streams, a control's value depends on its own name only. The
  five ways that could regress — enabling another control, re-ranging another control, reordering
  declarations, appending a future control, adding a future *domain* — each get a test, because a
  sequential RNG would pass every other test in this file and fail only these.
* **Direction.** "Crazy" must mean *far from the base*, not *high*. Measured over a master-seed
  corpus rather than asserted.

The GUI seam is checked with `ast` over `gui.py`, which cannot be imported here.
"""

from __future__ import annotations

import ast
import os
import random
import re
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets
from beatsync_fork import variant_batch as fork_batch
from beatsync_fork import variant_lab as fork_lab

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_LAB = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_lab.py")
_RECIPE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "creative_recipe.py")

FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS
BALANCED = dict(zip(FIELDS, (50,) * 6))
CINEMATIC = dict(zip(FIELDS, (30, 25, 65, 40, 30, 50)))
HIGH_ENERGY = dict(zip(FIELDS, (100, 85, 50, 85, 80, 80)))
MASTERS = [100_000 + 7919 * i for i in range(120)]


def _config(master=582913, spread=50, randomized=None, ranges=None):
    return fork_lab.VariantLabConfig(
        master_seed=master,
        spread=spread,
        randomized=fork_lab.default_randomized() if randomized is None else randomized,
        ranges=fork_lab.default_ranges() if ranges is None else ranges,
    )


def _resolve(master=582913, base=None, spread=50, randomized=None, ranges=None):
    return fork_lab.resolve(_config(master, spread, randomized, ranges),
                            BALANCED if base is None else base)


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _strip_docstrings(node):
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


def _executable_source(path):
    return ast.unparse(_strip_docstrings(_tree(path)))


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _registration(tree, widget, attrs=("click", "change", "input", "submit", "release")):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in attrs
            and isinstance(n.func.value, ast.Name) and n.func.value.id == widget]


def _kwargs(call):
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node):
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _widget_call(tree, name):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no widget assignment for {name}")


def _ordered_names(node):
    """Names in **source order**, descending through list concatenation.

    `ast.walk` is breadth-first, so for `[a, b] + c` it yields `c` before `a` — useless for lists
    whose order is a positional contract with a handler signature.
    """
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [name for element in node.elts for name in _ordered_names(element)]
    if isinstance(node, ast.BinOp):
        return _ordered_names(node.left) + _ordered_names(node.right)
    return []


def _assigned_list(tree, name):
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == name)
    return _ordered_names(node.value)


def _list_valued_assign(tree, name):
    """The assigned value for `name`, but only when it is a list or a concatenation of lists.

    A widget assignment (`variant_master_seed = gr.Number(...)`) is an `Assign` too, so without
    this guard an expander would happily walk into a `gr.Number` call and return the names of its
    keyword arguments.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return node.value if isinstance(node.value, (ast.List, ast.BinOp)) else None
    return None


def _expanded_list(tree, name, _depth=0):
    """`_assigned_list`, with locally-assigned *list* variables resolved into their own elements.

    `variant_lab_inputs` and `variant_lab_outputs` are built by concatenating named sub-lists
    (`creative_control_sliders`, `variant_lab_audio_config`, `variant_lab_audio_bases`), so the
    opaque form cannot see the individual widgets. E2's writer matrix has to name exact widgets,
    so this flattens the indirection until only real widget names remain.
    """
    assert _depth < 6, "list indirection is deeper than expected"
    out = []
    for item in _assigned_list(tree, name):
        if _list_valued_assign(tree, item) is not None:
            out.extend(_expanded_list(tree, item, _depth + 1))
        else:
            out.append(item)
    return out


# ===========================================================================
# 1. GOLDEN RNG VECTORS — the reproducibility promise
# ===========================================================================

#: `int(sha1("variant_lab|1|<master>|<domain>|<name>").hexdigest()[:12], 16)`, computed
#: independently. If any of namespace / separator / version position / digest / slice width
#: changes, these stop matching — which is the entire point of pinning them.
_GOLDEN_DERIVED_SEEDS = {
    ("variant_lab|1|1|clips|"): 180_275_523_648_989,
    ("variant_lab|1|1|controls|cut_density"): 56_217_070_967_945,
    ("variant_lab|1|582913|clips|"): 33_745_846_327_435,
    ("variant_lab|1|582913|controls|cut_density"): 22_747_130_717_878,
    ("variant_lab|1|999999|clips|"): 190_237_024_579_974,
    ("variant_lab|1|999999|controls|cut_density"): 233_979_050_165_340,
}

_GOLDEN_CLIP_SEEDS = {1: 919_454, 582913: 822_019, 999999: 92_023}

_GOLDEN_U_582913 = {
    "cut_density": 0.7203952440172314,
    "micro_cuts": -0.7860631421233377,
    "semantic_emphasis": -0.15703010973726328,
    "energy_response": 0.7759707482158551,
    "motion_bias": -0.5369181398275573,
    "source_diversity": 0.46403877093590595,
}

_GOLDEN_RECIPES = {
    (1, "Balanced", 0): {"seed": 919454, "cut_density": 50, "micro_cuts": 50,
                         "semantic_emphasis": 50, "energy_response": 50, "motion_bias": 50,
                         "source_diversity": 50},
    (1, "Balanced", 50): {"seed": 919454, "cut_density": 71, "micro_cuts": 27,
                          "semantic_emphasis": 53, "energy_response": 75, "motion_bias": 28,
                          "source_diversity": 55},
    (1, "Balanced", 100): {"seed": 919454, "cut_density": 92, "micro_cuts": 4,
                           "semantic_emphasis": 57, "energy_response": 99, "motion_bias": 7,
                           "source_diversity": 61},
    (582913, "Balanced", 50): {"seed": 822019, "cut_density": 68, "micro_cuts": 30,
                               "semantic_emphasis": 46, "energy_response": 69, "motion_bias": 37,
                               "source_diversity": 62},
    (582913, "Cinematic", 0): {"seed": 822019, "cut_density": 30, "micro_cuts": 25,
                               "semantic_emphasis": 65, "energy_response": 40, "motion_bias": 30,
                               "source_diversity": 50},
    (582913, "Cinematic", 50): {"seed": 822019, "cut_density": 55, "micro_cuts": 15,
                                "semantic_emphasis": 60, "energy_response": 63, "motion_bias": 22,
                                "source_diversity": 62},
    (582913, "Cinematic", 100): {"seed": 822019, "cut_density": 80, "micro_cuts": 5,
                                 "semantic_emphasis": 55, "energy_response": 87, "motion_bias": 14,
                                 "source_diversity": 73},
    (999999, "Balanced", 50): {"seed": 92023, "cut_density": 49, "micro_cuts": 54,
                               "semantic_emphasis": 75, "energy_response": 63, "motion_bias": 41,
                               "source_diversity": 40},
    (999999, "Balanced", 100): {"seed": 92023, "cut_density": 48, "micro_cuts": 57,
                                "semantic_emphasis": 100, "energy_response": 77, "motion_bias": 32,
                                "source_diversity": 29},
}

_BASES_BY_NAME = {"Balanced": BALANCED, "Cinematic": CINEMATIC, "High Energy": HIGH_ENERGY}


def test_the_namespace_and_algorithm_version_are_pinned():
    assert fork_lab.NAMESPACE == "variant_lab"
    assert fork_lab.VARIANT_LAB_ALGORITHM_VERSION == 1
    assert (fork_lab.DOMAIN_CLIPS, fork_lab.DOMAIN_CONTROLS, fork_lab.DOMAIN_AUDIO) == \
        ("clips", "controls", "audio")


@pytest.mark.parametrize("key,derived", sorted(_GOLDEN_DERIVED_SEEDS.items()))
def test_golden_rng_key_derivation(key: str, derived: int):
    """Independent reconstruction: seed a stdlib `Random` with the pinned integer and require the
    module's stream to agree. This cannot be satisfied by calling the helper twice."""
    namespace, version, master, domain, name = key.split("|")
    assert namespace == fork_lab.NAMESPACE
    assert int(version) == fork_lab.VARIANT_LAB_ALGORITHM_VERSION

    expected = random.Random(derived)
    actual = fork_lab.rng_for(int(master), domain, name)
    assert [actual.random() for _ in range(5)] == [expected.random() for _ in range(5)]


@pytest.mark.parametrize("master,clip", sorted(_GOLDEN_CLIP_SEEDS.items()))
def test_golden_clip_seeds(master: int, clip: int):
    assert fork_lab.resolve_clip_seed(master) == clip


@pytest.mark.parametrize("field,u", sorted(_GOLDEN_U_582913.items()))
def test_golden_control_stream_draws(field: str, u: float):
    assert fork_lab.rng_for(582913, "controls", field).uniform(-1.0, 1.0) == u


@pytest.mark.parametrize("key,expected", sorted(_GOLDEN_RECIPES.items()))
def test_golden_resolved_recipes(key: tuple, expected: dict):
    master, base_name, spread = key
    recipe = _resolve(master=master, base=_BASES_BY_NAME[base_name], spread=spread).recipe
    assert recipe.as_mapping() == expected


def test_the_module_uses_hashlib_and_never_the_builtin_hash():
    """`hash()` is randomised per process, so a master seed would not survive a restart."""
    source = _executable_source(_LAB)
    assert "hashlib.sha1" in source
    assert not re.search(r"(?<![\w.])hash\(", source)
    for forbidden in ("uuid", "time.time", "datetime", "SystemRandom", "os.urandom", "getrandbits"):
        assert forbidden not in source, f"variant_lab uses {forbidden}"


# ===========================================================================
# 2. INDEPENDENCE — the principal C2 regression contract
# ===========================================================================


REF_MASTER, REF_SPREAD = 582913, 65


def _ref_recipe(**kwargs):
    return _resolve(master=REF_MASTER, base=CINEMATIC, spread=REF_SPREAD, **kwargs).recipe


@pytest.mark.parametrize("disabled", FIELDS)
def test_enabling_or_disabling_another_control_changes_nothing_else(disabled: str):
    reference = _ref_recipe()
    partial = _ref_recipe(randomized=frozenset(FIELDS) - {disabled})
    for field in FIELDS:
        if field == disabled:
            continue
        assert getattr(partial, field) == getattr(reference, field), field


@pytest.mark.parametrize("retuned", FIELDS)
def test_changing_another_controls_range_changes_nothing_else(retuned: str):
    reference = _ref_recipe()
    ranges = dict(fork_lab.default_ranges())
    ranges[retuned] = fork_lab.ControlRange(20, 60)
    narrowed = _ref_recipe(ranges=ranges)
    for field in FIELDS:
        if field == retuned:
            continue
        assert getattr(narrowed, field) == getattr(reference, field), field


def test_control_declaration_order_does_not_matter():
    """`randomized` is a set and `ranges` a mapping, so resolution cannot depend on insertion
    order — asserted rather than assumed, because a future refactor to a list would silently
    reintroduce order sensitivity."""
    reference = _ref_recipe()
    reversed_ranges = {f: fork_lab.FULL_RANGE for f in reversed(FIELDS)}
    shuffled = _ref_recipe(randomized=frozenset(reversed(FIELDS)), ranges=reversed_ranges)
    assert shuffled.as_mapping() == reference.as_mapping()


def test_a_future_control_cannot_shift_todays_values():
    """The decisive sequential-RNG regression: an unknown name is dropped, and even if it were
    honoured it would draw from its own stream, so every existing control is untouched."""
    reference = _ref_recipe()
    future_ranges = dict(fork_lab.default_ranges())
    future_ranges["future_bloom"] = fork_lab.ControlRange(0, 100)
    extended = _ref_recipe(randomized=frozenset(FIELDS) | {"future_bloom"},
                           ranges=future_ranges)
    assert extended.as_mapping() == reference.as_mapping()


def test_a_future_audio_domain_cannot_shift_todays_values():
    """E2 will register audio controls under `rng_for(master, "audio", ...)`. Drawing from that
    domain must leave the visual controls and the clip seed exactly where they are."""
    reference = _ref_recipe()
    for name in ("tempo_push", "stutter", "filter_sweep"):
        fork_lab.rng_for(REF_MASTER, fork_lab.DOMAIN_AUDIO, name).uniform(-1.0, 1.0)
    assert _ref_recipe().as_mapping() == reference.as_mapping()


def test_the_audio_domain_is_distinct_from_the_controls_domain():
    control = fork_lab.rng_for(REF_MASTER, "controls", "cut_density").random()
    audio = fork_lab.rng_for(REF_MASTER, "audio", "cut_density").random()
    assert control != audio


def test_resolution_is_deterministic():
    for master in MASTERS[:40]:
        first = _resolve(master=master, base=CINEMATIC).recipe
        second = _resolve(master=master, base=CINEMATIC).recipe
        assert first == second


def test_different_masters_usually_differ():
    recipes = {_resolve(master=m).recipe.as_mapping()["seed"] for m in MASTERS}
    assert len(recipes) >= int(0.95 * len(MASTERS))


# ===========================================================================
# 3. THE CLIP SEED
# ===========================================================================


def test_the_clip_seed_is_always_a_positive_usable_variation_seed():
    from beatsync_fork import variation as fork_variation
    for master in MASTERS:
        seed = _resolve(master=master).recipe.seed
        assert fork_recipe.RECIPE_SEED_MIN <= seed <= fork_recipe.RECIPE_SEED_MAX
        assert seed != 0
        assert fork_variation.normalize_seed(seed) == seed
        assert fork_variation.is_variation(seed)


def test_the_clip_seed_depends_on_the_master_seed_alone():
    """Spread, ranges, which controls vary, the base, and a future control all leave it alone."""
    master = 582913
    reference = _resolve(master=master).recipe.seed
    variants = [
        _resolve(master=master, spread=0).recipe.seed,
        _resolve(master=master, spread=100).recipe.seed,
        _resolve(master=master, randomized=frozenset()).recipe.seed,
        _resolve(master=master, randomized=frozenset({"motion_bias"})).recipe.seed,
        _resolve(master=master, base=HIGH_ENERGY).recipe.seed,
        _resolve(master=master, ranges={f: fork_lab.ControlRange(10, 20) for f in FIELDS}).recipe.seed,
        _resolve(master=master, randomized=frozenset(FIELDS) | {"future_bloom"}).recipe.seed,
    ]
    assert set(variants) == {reference}


def test_spread_zero_still_produces_a_positive_clip_seed():
    """The §35 nuance, pinned: Spread 0 freezes the six controls but is **not** a legacy render —
    the clip selection still changes with the master seed."""
    resolution = _resolve(master=4242, base=BALANCED, spread=0)
    assert resolution.recipe.as_mapping() == {"seed": resolution.recipe.seed, **BALANCED}
    assert resolution.recipe.seed > 0
    assert not resolution.recipe.to_profile().is_neutral()


# ===========================================================================
# 4. THE SPREAD FORMULA
# ===========================================================================


def test_spread_zero_resolves_every_randomized_control_to_its_anchor():
    for master in MASTERS[:40]:
        recipe = _resolve(master=master, base=CINEMATIC, spread=0).recipe
        for field in FIELDS:
            assert getattr(recipe, field) == CINEMATIC[field], field


def test_spread_zero_anchors_a_base_that_sits_outside_its_range():
    ranges = dict(fork_lab.default_ranges())
    ranges["cut_density"] = fork_lab.ControlRange(10, 40)
    high = _resolve(base=dict(BALANCED, cut_density=90), spread=0, ranges=ranges).recipe
    low = _resolve(base=dict(BALANCED, cut_density=5), spread=0, ranges=ranges).recipe
    assert high.cut_density == 40
    assert low.cut_density == 10


@pytest.mark.parametrize("spread", [0, 1, 25, 50, 75, 99, 100])
def test_a_resolved_value_is_always_inside_its_declared_range(spread: int):
    ranges = dict(fork_lab.default_ranges())
    ranges["cut_density"] = fork_lab.ControlRange(10, 40)
    for master in MASTERS[:60]:
        value = _resolve(master=master, base=dict(BALANCED, cut_density=90),
                         spread=spread, ranges=ranges).recipe.cut_density
        assert 10 <= value <= 40


def test_full_spread_reaches_both_ends_of_the_range():
    values = [_resolve(master=m, spread=100).recipe.cut_density for m in MASTERS]
    assert min(values) <= 5
    assert max(values) >= 95


def test_a_base_on_an_endpoint_can_only_move_inward():
    """Documented and correct: with no headroom above, roughly half of all masters leave it put."""
    values = [_resolve(master=m, base=HIGH_ENERGY, spread=100).recipe.cut_density
              for m in MASTERS]
    assert max(values) == 100
    assert min(values) < 100
    assert all(v <= 100 for v in values)
    assert values.count(100) > 0


def test_direction_is_approximately_a_fair_coin():
    """The honest bias contract. The anchor is the directional *median* — NOT "mean displacement is
    zero", which is false whenever the anchor sits off-centre in its range."""
    for field in FIELDS:
        deltas = [getattr(_resolve(master=m, spread=100).recipe, field) - 50 for m in MASTERS]
        up = sum(1 for d in deltas if d > 0)
        down = sum(1 for d in deltas if d < 0)
        assert 0.35 <= up / len(deltas) <= 0.65, (field, up)
        assert 0.35 <= down / len(deltas) <= 0.65, (field, down)


def test_crazy_is_mixed_direction_not_all_high():
    """At full spread most recipes must move controls *both* ways. An algorithm that pushed
    everything up would pass every other test in this file."""
    mixed = all_up = all_down = 0
    for master in MASTERS:
        recipe = _resolve(master=master, spread=100).recipe
        deltas = [getattr(recipe, f) - 50 for f in FIELDS]
        up = sum(1 for d in deltas if d > 0)
        down = sum(1 for d in deltas if d < 0)
        mixed += bool(up and down)
        all_up += up == len(FIELDS)
        all_down += down == len(FIELDS)

    assert mixed >= int(0.90 * len(MASTERS)), mixed
    assert all_up <= int(0.05 * len(MASTERS)), all_up
    assert all_down <= int(0.05 * len(MASTERS)), all_down


def test_an_off_centre_anchor_legitimately_drifts_toward_its_headroom():
    """The documented consequence of the range, asserted so nobody "fixes" it: base 30 in 0..100 has
    more room above than below, so it drifts up on average while still being a fair coin."""
    deltas = [_resolve(master=m, base=CINEMATIC, spread=100).recipe.cut_density - 30
              for m in MASTERS]
    assert sum(deltas) / len(deltas) > 0
    assert any(d < 0 for d in deltas)


def test_quantisation_is_explicit_half_up():
    """Never `round()`: banker's rounding would collapse 2.5 and 3.5 onto even values. A degenerate
    low/high pair makes the arithmetic exact enough to pin without reaching into privates."""
    # anchor 0, hi 5, u = +1 at spread 100 -> exactly 5; u = +0.5 -> 2.5 -> half-up 3
    assert fork_lab._half_up(2.5) == 3
    assert fork_lab._half_up(3.5) == 4
    assert fork_lab._half_up(2.4999) == 2
    assert fork_lab._half_up(0.5) == 1
    assert fork_lab._half_up(0.0) == 0
    assert round(2.5) == 2 and fork_lab._half_up(2.5) != round(2.5)


# ===========================================================================
# 5. RANDOMIZE OFF, AND RANGE NORMALISATION
# ===========================================================================


@pytest.mark.parametrize("kept", FIELDS)
def test_a_fixed_control_is_the_exact_live_value_and_ignores_its_range(kept: str):
    """Randomize OFF means "leave this alone" — including no clamping into a range the user may
    have typed and then excluded."""
    ranges = {f: fork_lab.ControlRange(0, 10) for f in FIELDS}
    base = dict(BALANCED, **{kept: 97})
    recipe = _resolve(base=base, spread=100, randomized=frozenset(), ranges=ranges).recipe
    assert getattr(recipe, kept) == 97
    for field in FIELDS:
        assert getattr(recipe, field) == base[field], field


def test_fixed_and_randomized_controls_coexist():
    recipe = _resolve(base=CINEMATIC, spread=100,
                      randomized=frozenset({"motion_bias"})).recipe
    for field in FIELDS:
        if field == "motion_bias":
            continue
        assert getattr(recipe, field) == CINEMATIC[field], field


@pytest.mark.parametrize("lo,hi,expected", [
    (0, 100, (0, 100)), (10, 40, (10, 40)), (40, 40, (40, 40)),
    (-10, 140, (0, 100)), (10.0, 40.0, (10, 40)),
    (80, 20, (80, 80)),                       # inverted -> degenerate, never swapped
    (None, None, (0, 100)), (True, False, (0, 100)),
    (37.5, 40.5, (0, 100)), ("10", "40", (0, 100)),
    (float("nan"), float("inf"), (0, 100)), (object(), object(), (0, 100)),
])
def test_range_normalisation_is_total(lo: Any, hi: Any, expected: tuple):
    assert fork_lab.ControlRange(lo, hi).as_tuple() == expected


def test_an_inverted_range_is_never_silently_swapped():
    """`gr.Number` guarantees no ordering, so "min 80, max 20" is a typo. The floor is its only
    unambiguous half; swapping would resolve a range the user never asked for."""
    collapsed = fork_lab.ControlRange(80, 20)
    assert collapsed.as_tuple() == (80, 80)
    assert collapsed.is_degenerate
    for master in MASTERS[:30]:
        ranges = dict(fork_lab.default_ranges())
        ranges["motion_bias"] = collapsed
        assert _resolve(master=master, spread=100, ranges=ranges).recipe.motion_bias == 80


def test_a_degenerate_range_pins_the_control():
    ranges = dict(fork_lab.default_ranges())
    ranges["energy_response"] = fork_lab.ControlRange(70, 70)
    for master in MASTERS[:30]:
        assert _resolve(master=master, spread=100, ranges=ranges).recipe.energy_response == 70


# ===========================================================================
# 6. CONFIG NORMALISATION
# ===========================================================================


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (999999, 999999), (1000000, 1000000), (-5, 0), (None, 0), (True, 0),
    (7.9, 0), ("7.0", 0), ("382", 382), (382.0, 382), ("", 0), (object(), 0),
])
def test_master_seed_normalisation_follows_the_existing_seed_contract(value: Any, expected: int):
    assert fork_lab.normalize_master_seed(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0, 0), (50, 50), (100, 100), (-10, 0), (140, 100), (50.0, 50),
    (None, 50), (True, 50), (50.5, 50), ("65", 65), (object(), 50),
])
def test_spread_normalisation_is_total(value: Any, expected: int):
    assert fork_lab.normalize_spread(value) == expected


@pytest.mark.parametrize("value,expected", [
    (["cut_density", "motion_bias"], {"cut_density", "motion_bias"}),
    (["cut_density", "nope"], {"cut_density"}),
    ([], set()), (None, set()), ("cut_density", set()), (object(), set()),
    (list(FIELDS), set(FIELDS)),
])
def test_unknown_randomized_names_are_dropped_deterministically(value: Any, expected: set):
    """Dropped rather than raising: this runs on a widget value mid-interaction, and an unknown name
    has no stream and no slider, so it cannot influence resolution either way."""
    assert fork_lab.normalize_randomized(value) == frozenset(expected)


def test_the_config_normalises_everything_on_construction():
    config = fork_lab.VariantLabConfig(master_seed="582913", spread="65",
                                       randomized=["cut_density", "bogus"],
                                       ranges={"cut_density": (80, 20), "bogus": (1, 2)})
    assert config.master_seed == 582913
    assert config.spread == 65
    assert config.randomized == frozenset({"cut_density"})
    assert config.range_for("cut_density").as_tuple() == (80, 80)
    assert set(config.ranges) == set(FIELDS)
    assert config.range_for("motion_bias").as_tuple() == (0, 100)


def test_a_missing_master_seed_refuses_to_resolve():
    """The GUI mints and *displays* one first; the pure resolver never invents randomness."""
    for bad in (0, None, -1, 7.9, True):
        with pytest.raises(ValueError):
            fork_lab.resolve(fork_lab.VariantLabConfig(master_seed=bad), BALANCED)


def test_the_config_holds_no_base_profile():
    """The base is the live sliders, passed at resolve time — a cached snapshot is the defect this
    prevents, because the sliders move and the snapshot would not."""
    config = _config()
    for field in ("base", "base_profile", "snapshot", "preset", "cut_density"):
        assert not hasattr(config, field), field


def test_a_resolution_records_its_provenance():
    resolution = _resolve(master=582913, spread=65,
                          randomized=frozenset({"cut_density", "motion_bias"}))
    assert resolution.algorithm_version == fork_lab.VARIANT_LAB_ALGORITHM_VERSION == 1
    assert resolution.config.master_seed == 582913
    assert resolution.config.spread == 65
    assert resolution.fixed_fields == frozenset(FIELDS) - {"cut_density", "motion_bias"}
    text = resolution.describe()
    assert "algorithm v1" in text and "582913" in text and "2 randomized / 4 fixed" in text


def test_the_resolver_has_no_side_effects_on_its_inputs():
    base = dict(CINEMATIC)
    ranges = fork_lab.default_ranges()
    before = dict(base)
    _resolve(base=base, spread=100, ranges=ranges)
    assert base == before


# ===========================================================================
# 7. DEFAULTS
# ===========================================================================


def test_the_shipped_defaults_are_the_owner_decisions():
    assert fork_lab.DEFAULT_VARIATION_SPREAD == 50
    assert (fork_lab.SPREAD_MIN, fork_lab.SPREAD_MAX) == (0, 100)
    assert fork_lab.default_randomized() == frozenset(FIELDS)
    assert len(fork_lab.default_randomized()) == 6
    assert (fork_lab.DEFAULT_RANGE_LO, fork_lab.DEFAULT_RANGE_HI) == (0, 100)
    for field in FIELDS:
        assert fork_lab.default_ranges()[field].as_tuple() == (0, 100), field


def test_source_diversity_is_not_quietly_narrowed():
    """Explicitly pinned: a default that suppressed a shipped control would be an opinion in
    disguise. The user may constrain it; the product does not do it for them."""
    assert fork_lab.default_ranges()["source_diversity"].as_tuple() == (0, 100)
    assert "source_diversity" in fork_lab.default_randomized()


# ===========================================================================
# 8. MODULE PURITY AND ISOLATION
# ===========================================================================


@pytest.mark.parametrize("path", [_LAB, _RECIPE])
def test_the_core_modules_import_only_stdlib_and_fork_modules(path: str):
    imported = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "dataclasses", "hashlib", "random",
                        "types", "typing", "beatsync_fork"}, sorted(imported)


@pytest.mark.parametrize("path", [_LAB, _RECIPE])
def test_the_core_modules_name_no_runtime_or_pipeline_machinery(path: str):
    source = _executable_source(path).lower()
    for word in ("gradio", "numpy", "auto_mode", "stage4_select", "stage6_av_planner",
                 "video_analysis", "video_processor", "beat_info", "render_info", "open",
                 "cache", "qwen", "os"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"{path} mentions {word!r}"


def test_the_frozen_resolvers_know_nothing_about_multi_variant_generation():
    """**Split by C3 V1, not deleted.** This was one blanket ban on every C3 term anywhere.

    C3 shipped generation and comparison, so the ban had to change — and the half that was always
    load-bearing is kept at *full* strength here: `variant_lab.py` and `creative_recipe.py` still
    contain no batch concept at all. C3 lives in a sibling module that imports them; they do not
    import it, and they did not learn a thing. `DOMAIN_BATCH` is the one deliberate exception —
    the domain registry stays in one place — and it is a bare string constant, not machinery.
    """
    for path in (_LAB, _RECIPE):
        source = _executable_source(path).lower()
        for word in ("generate_variants", "variant_batch", "variant_count", "num_variants",
                     "compare_variants", "variant_gallery", "resolve_batch", "candidate",
                     "freestyle", "director", "stage_cache", "shortlist"):
            assert not re.search(rf"\b{re.escape(word)}\b", source), f"{path} mentions {word!r}"


def test_the_gui_carries_only_the_accepted_c3_machinery():
    """The positive half: C3's names are legitimate in `gui.py` now, and only these.

    A term ban is the weak half of this guard — a rename defeats it — so the structural assertion
    below is the one that matters. This exists to stop the *other* deferred ideas drifting in
    alongside the accepted ones.
    """
    source = _executable_source(_GUI).lower()
    # **Split again by C3-R0**, which implements render_batch deliberately. What stays banned is
    # what C3-R0 refused to build; the structural render-boundary proofs live in
    # `tests/test_gui_guard_seam.py`, because a token list never survives a rename.
    for word in ("variant_gallery", "stage_cache", "shortlist",
                 "freestyle", "director", "thumbnail", "variant_preview"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"gui.py mentions {word!r}"


def test_no_c3_handler_can_reach_a_render_entry_point():
    """The load-bearing C3 boundary, asserted structurally rather than by banned token.

    C3 V1 generates, compares and applies settings. Neither of its buttons may reach the render
    path — not directly, and not through anything either handler calls. The call graph is walked
    from each entry point, so renaming a handler cannot slip past this the way a word list could.
    """
    tree = _tree(_GUI)
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}

    forbidden = {"process_video_guarded", "process_video", "_process_video_impl",
                 "analyze_beats_auto", "create_music_video", "build_mixed_master",
                 "prepare_voice_inputs", "prepare_sfx_inputs", "resolve_for_render"}

    for entry in ("_on_generate_variants", "_on_apply_selected_variant"):
        assert entry in defined, entry
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
                if not isinstance(call, ast.Call):
                    continue
                rendered = ast.unparse(call.func)
                leaf = rendered.rsplit(".", 1)[-1]
                assert leaf not in forbidden, f"{entry} reaches {rendered} (via {name})"
                if leaf in defined:
                    pending.append(leaf)
        assert len(seen) > 1, f"{entry} resolved no call graph at all"


def test_c3_generation_and_apply_write_no_render_or_gate_widget():
    tree = _tree(_GUI)
    for list_name in ("variant_batch_inputs", "variant_batch_outputs"):
        names = _expanded_list(tree, list_name)
        for forbidden in ("video_output", "status_output", "session_state", "process_btn",
                          "source_outputs", "source_state", "source_report", "confirm_btn",
                          "prep_outputs", "prep_state", "prep_report", "prep_analyze_btn",
                          "audio_input", "output_filename", "processing_mode", "custom_fps",
                          "audio_layers_report", "smart_mix_report"):
            assert forbidden not in names, f"{forbidden} in {list_name}"


def test_stage_five_and_the_planner_know_nothing_about_variant_lab():
    for relative in ("src/video_analysis.py", "src/video_processor.py",
                     "src/auto_mode/__init__.py", "src/auto_mode/stage4_select.py",
                     "src/auto_mode/stage6_av_planner.py",
                     "src/auto_mode/stage5_qwen_scene_worker.py",
                     "src/beatsync_fork/creative.py", "src/beatsync_fork/presets.py",
                     "src/beatsync_fork/variation.py", "src/beatsync_fork/library_prep.py"):
        source = _executable_source(os.path.join(_REPO_ROOT, *relative.split("/"))).lower()
        for word in ("variant_lab", "creative_recipe", "creativerecipe", "master_seed",
                     "variantlabconfig", "variation_spread", "rng_for"):
            assert not re.search(rf"\b{re.escape(word)}\b", source), \
                f"{relative} mentions {word!r}"


def test_stage_five_constants_are_untouched():
    source = open(os.path.join(_REPO_ROOT, "src", "video_analysis.py"), encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_no_cli_variant_lab_flag_was_added():
    source = open(os.path.join(_REPO_ROOT, "src", "video_processor.py"), encoding="utf-8").read()
    for flag in ("--master-seed", "--spread", "--variant-range", "--randomize-controls",
                 "--variant-lab"):
        assert flag not in source, flag


# ===========================================================================
# 9. THE GUI SEAM
# ===========================================================================


def test_the_variant_lab_accordion_exists_and_is_collapsed():
    source = open(_GUI, encoding="utf-8").read()
    assert "gr.Accordion(label=LABEL_VARIANT_LAB, open=False)" in source
    # inside Creative Direction, below the six sliders it writes
    heading = source.index("Creative Direction")
    following = source.index("Processing Mode", heading)
    position = source.index("LABEL_VARIANT_LAB", heading)
    assert heading < position < following
    assert source.index("semantic_emphasis = gr.", heading) < position


def test_the_master_seed_box_is_visible_and_defaults_to_unset():
    kwargs = _kwargs(_widget_call(_tree(_GUI), "variant_master_seed"))
    assert ast.unparse(_widget_call(_tree(_GUI), "variant_master_seed").func) == "gr.Number"
    assert ast.literal_eval(kwargs["value"]) == 0
    assert ast.literal_eval(kwargs["precision"]) == 0
    assert ast.unparse(kwargs["label"]) == "LABEL_MASTER_SEED"


def test_the_spread_slider_is_0_to_100_defaulting_to_50():
    call = _widget_call(_tree(_GUI), "variation_spread")
    kwargs = _kwargs(call)
    assert ast.unparse(call.func) == "gr.Slider"
    assert ast.unparse(kwargs["minimum"]) == "fork_lab.SPREAD_MIN"
    assert ast.unparse(kwargs["maximum"]) == "fork_lab.SPREAD_MAX"
    assert ast.unparse(kwargs["value"]) == "fork_lab.DEFAULT_VARIATION_SPREAD"
    assert ast.literal_eval(kwargs["step"]) == 1


def test_the_randomize_group_offers_all_six_with_exact_field_values():
    call = _widget_call(_tree(_GUI), "variant_randomize")
    kwargs = _kwargs(call)
    assert ast.unparse(call.func) == "gr.CheckboxGroup"
    assert ast.unparse(kwargs["choices"]) == "_VARIANT_RANDOMIZE_CHOICES"
    assert ast.unparse(kwargs["value"]) == "list(fork_presets.CREATIVE_CONTROL_FIELDS)"


def test_the_choice_labels_map_exactly_the_six_fields_with_no_heuristic():
    """Explicit mapping, never derived from the field name: the resolver keys on exact names, so a
    lowercase/replace heuristic would be a silent correctness hazard."""
    tree = _tree(_GUI)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "_VARIANT_CONTROL_LABELS")
    keys = [ast.literal_eval(k) for k in node.value.keys]
    assert tuple(keys) == FIELDS
    source = _executable_source(_GUI)
    assert ".replace('_', ' ')" not in source
    assert ".title()" not in source


@pytest.mark.parametrize("field", FIELDS)
def test_each_control_has_a_min_and_max_box_defaulting_to_the_full_range(field: str):
    tree = _tree(_GUI)
    for end, default in (("min", "fork_lab.DEFAULT_RANGE_LO"),
                         ("max", "fork_lab.DEFAULT_RANGE_HI")):
        call = _widget_call(tree, f"range_{field}_{end}")
        kwargs = _kwargs(call)
        assert ast.unparse(call.func) == "gr.Number"
        assert ast.unparse(kwargs["value"]) == default, (field, end)
        assert ast.literal_eval(kwargs["precision"]) == 0
        assert ast.unparse(kwargs["minimum"]) == "fork_creative.CONTROL_MIN"
        assert ast.unparse(kwargs["maximum"]) == "fork_creative.CONTROL_MAX"


def test_the_report_is_labelled_last_generated_and_is_read_only():
    call = _widget_call(_tree(_GUI), "variant_report")
    kwargs = _kwargs(call)
    assert ast.unparse(call.func) == "gr.Textbox"
    assert ast.unparse(kwargs["label"]) == "LABEL_VARIANT_REPORT"
    assert ast.literal_eval(kwargs["interactive"]) is False


def test_the_report_label_says_last_generated_not_current():
    source = open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8").read()
    start = source.index("LABEL_VARIANT_REPORT")
    line = source[start:source.index("\n", start)]
    assert "Last generated" in line
    assert "Current" not in line


# ===========================================================================
# 10. THE GUI EVENT MODEL
# ===========================================================================


def test_exactly_two_variant_lab_buttons_are_registered():
    tree = _tree(_GUI)
    for button, handler in (("generate_variant_btn", "_on_generate_variant"),
                            ("new_variant_btn", "_on_new_variant")):
        calls = _registration(tree, button)
        assert len(calls) == 1, button
        assert calls[0].func.attr == "click", button
        kwargs = _kwargs(calls[0])
        assert ast.unparse(kwargs["fn"]) == handler
        assert _names(kwargs["inputs"]) == ["variant_lab_inputs"]
        assert _names(kwargs["outputs"]) == ["variant_lab_outputs"]


@pytest.mark.parametrize("widget", [
    "variant_master_seed", "variation_spread", "variant_randomize", "variant_report",
] + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")])
def test_no_variant_lab_config_widget_registers_a_handler(widget: str):
    """They are read at click time only. A handler here would be new live event traffic for no
    benefit, and the brief's rule is to keep the preset event graph simple."""
    assert _registration(_tree(_GUI), widget) == [], widget


@pytest.mark.parametrize("widget", FIELDS)
def test_no_creative_slider_gained_a_second_handler(widget: str):
    """The PR3 preset graph must be exactly as it was: one `.input()` per slider, nothing else."""
    calls = _registration(_tree(_GUI), widget)
    assert len(calls) == 1, f"{widget} registers {len(calls)} handlers"
    assert calls[0].func.attr == "input"
    assert ast.unparse(_kwargs(calls[0])["fn"]) == "_on_creative_control_input"


#: The widget names behind the audio halves of the lab's input list. E2 (§10/§12): one
#: CheckboxGroup plus three min/max pairs of configuration, and the three live levels as bases.
AUDIO_CONFIG_WIDGETS = (["variant_audio_randomize"]
                        + [f"range_{w}_{e}" for w in ("music_under_voice", "sfx_amount",
                                                      "sfx_level")
                           for e in ("min", "max")])
AUDIO_BASE_WIDGETS = ["music_under_voice", "sfx_amount", "sfx_level"]


def test_the_input_list_is_config_then_every_live_base():
    """Config first (visual then audio), then every live base (visual then audio).

    E2 extended this list rather than creating a second one, so the documented "config then bases"
    shape still describes the whole thing.
    """
    names = _assigned_list(_tree(_GUI), "variant_lab_inputs")
    expected = (["variant_master_seed", "variation_spread", "variant_randomize"]
                + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")]
                + ["variant_lab_audio_config", "creative_control_sliders",
                   "variant_lab_audio_bases"])
    assert names == expected


def test_the_input_list_expands_to_exactly_the_expected_widgets():
    """The same contract with the sub-list indirection resolved, so the exact widgets are pinned."""
    tree = _tree(_GUI)
    expected = (["variant_master_seed", "variation_spread", "variant_randomize"]
                + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")]
                + AUDIO_CONFIG_WIDGETS + list(FIELDS) + AUDIO_BASE_WIDGETS)
    assert _expanded_list(tree, "variant_lab_inputs") == expected


def test_the_input_list_matches_both_handlers_parameter_order():
    """Gradio passes `inputs` positionally, so the list and the signatures are one contract.

    This is the test that would catch an E2 parameter inserted in the wrong place — the failure
    mode would otherwise be a silently mis-assigned audio range rather than an exception.
    """
    tree = _tree(_GUI)
    parameters = [a.arg for a in _func(tree, "_on_generate_variant").args.args]
    expected = (["variant_master_seed", "variation_spread", "variant_randomize"]
                + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")]
                + ["variant_audio_randomize"]
                + [f"range_{w}_{e}" for w in ("music_under_voice", "sfx_amount", "sfx_level")
                   for e in ("min", "max")]
                + list(FIELDS) + AUDIO_BASE_WIDGETS)
    assert parameters == expected

    # and the signature order is the *expanded* input order, element for element
    assert parameters == _expanded_list(tree, "variant_lab_inputs")

    new_variant = _func(tree, "_on_new_variant")
    assert [a.arg for a in new_variant.args.args] == ["variant_master_seed"]
    assert new_variant.args.vararg is not None


def test_the_output_list_is_exactly_what_variant_lab_may_write():
    names = _assigned_list(_tree(_GUI), "variant_lab_outputs")
    assert names == ["variant_master_seed", "variation_seed", "creative_control_sliders",
                     "creative_preset", "variant_lab_audio_bases", "variant_report"]


def test_the_output_list_expands_to_the_exact_writable_widgets():
    """E2 adds exactly three writable widgets — the three audio levels — and nothing else.

    Everything else in Audio Layers / Smart Mix stays unwritable, which is the property the split
    seam guards in `test_audio_layers_seam.py` assert from the other direction.
    """
    expanded = _expanded_list(_tree(_GUI), "variant_lab_outputs")
    assert expanded == (["variant_master_seed", "variation_seed"] + list(FIELDS)
                        + ["creative_preset"] + AUDIO_BASE_WIDGETS + ["variant_report"])
    for forbidden in ("voice_files", "voice_start_delay", "voice_min_gap", "voice_avoid_drops",
                      "sfx_folder", "sfx_roles", "audio_layers_report", "smart_mix_report"):
        assert forbidden not in expanded, forbidden


def test_variant_lab_writes_no_gate_or_preparation_widget():
    """Structural: generating a recipe cannot clear a confirmation or disable Create Music Video."""
    names = _assigned_list(_tree(_GUI), "variant_lab_outputs") + \
        _assigned_list(_tree(_GUI), "variant_lab_inputs")
    for forbidden in ("source_outputs", "source_report", "confirm_btn", "confirm_status",
                      "process_btn", "source_state", "prep_outputs", "prep_report", "prep_status",
                      "prep_analyze_btn", "prep_state", "session_state", "video_output",
                      "status_output", "audio_input", "custom_fps", "processing_mode",
                      "output_filename"):
        assert forbidden not in names, forbidden


def test_new_variant_delegates_to_the_one_generate_path():
    """One resolver call site: a second implementation is how the two buttons would drift apart.

    **R1-B.** Minting moved out of this handler into `_fresh_variant_master_seed`, which is what
    makes "a new master" a guarantee rather than a probability; the delegation property asserted
    here is unchanged, and the minting itself is owned by the tests above.
    """
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_on_new_variant")))
    assert "_on_generate_variant(" in body
    assert "_fresh_variant_master_seed(" in body
    assert "fork_lab.resolve" not in body
    assert "fork_lab.VariantLabConfig" not in body


def test_the_generate_handler_mints_a_visible_master_seed_when_unset():
    """**Structurally updated by C3**: the minting moved into the shared normalisation helper.

    The property is unchanged and is now asserted where it actually lives — and it is *stronger*
    for having moved, because all three handlers that read the Variant Lab screen now mint through
    the same code instead of two of them growing their own copy.
    """
    tree = _tree(_GUI)
    context = ast.unparse(_strip_docstrings(
        _func(tree, "_build_variant_resolution_context")))
    assert "fork_lab.normalize_master_seed(variant_master_seed)" in context
    assert "fork_variation.random_seed()" in context

    body = ast.unparse(_strip_docstrings(_func(tree, "_on_generate_variant")))
    assert "_build_variant_resolution_context(" in body
    assert "fork_lab.resolve(config, base)" in body
    # the minted seed is returned as an output rather than used invisibly
    outputs = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_variant_apply_outputs")))
    assert "master_seed" in outputs


def test_the_apply_outputs_compute_the_preset_label_explicitly():
    """Programmatic slider writes do not fire `.input()`, so without this the label would keep
    claiming whatever preset the base came from."""
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_variant_apply_outputs")))
    assert "fork_presets.matching_preset(values)" in body
    assert "resolution.describe()" in body


def test_no_variant_lab_widget_reaches_the_render_request_or_the_gate():
    tree = _tree(_GUI)
    lab_widgets = (["variant_master_seed", "variation_spread", "variant_randomize",
                    "variant_report", "variant_lab_inputs", "variant_lab_outputs"]
                   + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")])

    process = _registration(tree, "process_btn", attrs=("click",))[0]
    inputs = [n.id for n in _kwargs(process)["inputs"].elts if isinstance(n, ast.Name)]
    for widget in lab_widgets:
        assert widget not in inputs, widget

    for name in ("process_video_guarded", "process_video", "_process_video_impl"):
        fn = _func(tree, name)
        assert not set(a.arg for a in fn.args.args) & set(lab_widgets), name
        body = ast.unparse(_strip_docstrings(fn))
        for word in ("fork_lab", "fork_recipe", "variant_lab", "CreativeRecipe", "master_seed"):
            assert word not in body, f"{name} references {word}"


def test_variant_lab_is_absent_from_source_and_preparation_wiring():
    tree = _tree(_GUI)
    lab_widgets = set(["variant_master_seed", "variation_spread", "variant_randomize",
                       "variant_report"]
                      + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")])

    for list_name in ("source_outputs", "prep_outputs"):
        assert not lab_widgets & set(_assigned_list(tree, list_name)), list_name

    for widget in ("source_mode", "source_folder", "source_recursive", "scan_btn", "video_input",
                   "confirm_btn", "prep_folder", "prep_recursive", "prep_batch_size",
                   "prep_scan_btn", "prep_analyze_btn"):
        for call in _registration(tree, widget, attrs=("click", "change")):
            kwargs = _kwargs(call)
            for key in ("inputs", "outputs"):
                if key in kwargs:
                    assert not lab_widgets & set(_names(kwargs[key])), f"{widget}.{key}"


def test_the_existing_randomize_button_is_untouched():
    calls = _registration(_tree(_GUI), "randomize_btn", attrs=("click",))
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])
    assert ast.unparse(kwargs["fn"]) == "fork_variation.random_seed"
    assert _names(kwargs["inputs"]) == []
    assert _names(kwargs["outputs"]) == ["variation_seed"]


def test_generating_a_variant_starts_no_render():
    tree = _tree(_GUI)
    for name in ("_on_generate_variant", "_on_new_variant", "_variant_apply_outputs"):
        body = ast.unparse(_strip_docstrings(_func(tree, name)))
        for forbidden in ("create_music_video", "process_video", "analyze_beats_auto",
                          "resolve_for_render", "live_declaration", "scan_folder"):
            assert forbidden not in body, f"{name} calls {forbidden}"


# ===========================================================================
# 11. END-TO-END THROUGH THE REAL HANDLER LOGIC (pure half)
# ===========================================================================


def test_a_full_resolution_maps_onto_the_six_slider_values_in_order():
    """What the GUI writes: the recipe's controls, positionally aligned with the slider list."""
    resolution = _resolve(master=582913, base=CINEMATIC, spread=50)
    values = tuple(getattr(resolution.recipe, f) for f in FIELDS)
    assert values == (55, 15, 60, 63, 22, 62)
    assert fork_presets.matching_preset(values) == "Custom"


def test_spread_zero_from_a_named_preset_keeps_that_preset_label():
    """A pleasant consequence of the label being a read-out: freeze the controls and the selector
    still says what they are."""
    for name in fork_presets.PRESETS:
        base = dict(zip(FIELDS, fork_presets.preset_values(name)))
        recipe = _resolve(master=4242, base=base, spread=0).recipe
        values = tuple(getattr(recipe, f) for f in FIELDS)
        assert fork_presets.matching_preset(values) == name


def test_a_generated_variant_almost_always_reads_custom():
    labels = [fork_presets.matching_preset(
        tuple(getattr(_resolve(master=m, spread=50).recipe, f) for f in FIELDS))
        for m in MASTERS]
    assert labels.count("Custom") >= int(0.95 * len(labels))
    assert all(label in fork_presets.PRESET_NAMES for label in labels)


# ---------------------------------------------------------------------------
# The REAL gui.py handlers, AST-extracted and executed.
#
# `gui.py` cannot be imported here (it pulls in logger/librosa/cupy/gradio), but its Variant Lab
# handlers are pure: they touch only the three fork modules. Extracting and running the actual
# bodies is what turns "the structure looks right" into "the handler produces these exact values",
# which is the §47 contract. The extraction list is part of this contract: a handler that grows a
# dependency outside it fails here with NameError rather than silently going untested.
# ---------------------------------------------------------------------------

#: `_build_variant_resolution_context` joined the set with C3: the normalisation `_on_generate_variant`
#: used to inline was lifted into it so Generate Variants and Apply Selected read the screen through
#: exactly the same code. Extracting the handler without it would fail with `NameError`, which is
#: itself the proof that the helper is on the real call path rather than a parallel copy.
_HANDLER_NAMES = ("_variant_apply_outputs", "_build_variant_resolution_context",
                  "_on_generate_variant", "_fresh_variant_master_seed", "_on_new_variant")


def _gui_handlers():
    from beatsync_fork import audio_mix as fork_audio_mix
    from beatsync_fork import smart_mix as fork_smart_mix
    from beatsync_fork import variation as fork_variation
    from typing import Tuple

    tree = _tree(_GUI)
    wanted = {name: None for name in _HANDLER_NAMES}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            wanted[node.name] = node
    missing = [name for name, node in wanted.items() if node is None]
    assert not missing, f"handlers not found at module level: {missing}"

    # `fork_audio_mix` / `fork_smart_mix` joined the extraction namespace with E2: the handler
    # derives each audio base through the normaliser that OWNS that control, so those two modules
    # are now part of the handler's real dependency set.
    namespace = {"fork_presets": fork_presets, "fork_lab": fork_lab,
                 "fork_variation": fork_variation, "fork_audio_mix": fork_audio_mix,
                 "fork_smart_mix": fork_smart_mix, "Tuple": Tuple}
    exec(compile(ast.Module(body=list(wanted.values()), type_ignores=[]),
                 filename=_GUI, mode="exec"), namespace)
    return namespace


#: E2's three audio controls and their *owning* defaults — 35 for Audio Layers' music floor, 50 for
#: both Smart Mix controls. Written out rather than imported from the fork so a change to either
#: default is visible here as a deliberate test edit.
AUDIO_FIELDS = fork_lab.AUDIO_CONTROL_FIELDS
AUDIO_BASE_DEFAULTS = {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50}


def _generate(master, base, spread=50, randomized=None, ranges=None,
              audio_randomized=None, audio_ranges=None, audio_base=None):
    """Invoke the real handler. The audio defaults mirror a **first-open** lab — nothing ticked,
    full ranges, each control at its own default — so every pre-E2 call site in this file still
    describes exactly the scenario it always did, which is itself part of the C2 regression proof.
    """
    handlers = _gui_handlers()
    randomized = list(FIELDS) if randomized is None else randomized
    ranges = {f: (0, 100) for f in FIELDS} if ranges is None else ranges
    flat_ranges = [value for f in FIELDS for value in ranges[f]]
    audio_randomized = [] if audio_randomized is None else audio_randomized
    audio_ranges = ({f: (0, 100) for f in AUDIO_FIELDS}
                    if audio_ranges is None else audio_ranges)
    flat_audio_ranges = [value for f in AUDIO_FIELDS for value in audio_ranges[f]]
    audio_base = dict(AUDIO_BASE_DEFAULTS) if audio_base is None else audio_base
    return handlers["_on_generate_variant"](
        master, spread, randomized, *flat_ranges,
        audio_randomized, *flat_audio_ranges,
        *[base[f] for f in FIELDS], *[audio_base[f] for f in AUDIO_FIELDS])


def test_the_real_handler_produces_the_exact_expected_outputs():
    """§47, pinned end to end: master + config + base -> the exact widget values written."""
    outputs = _generate(582913, CINEMATIC, spread=50)

    assert outputs[0] == 582913                       # master seed, echoed back visibly
    assert outputs[1] == 822019                       # resolved clip Variation Seed
    assert outputs[2:8] == (55, 15, 60, 63, 22, 62)   # the six sliders, in field order
    assert outputs[8] == "Custom"                     # preset label, computed explicitly
    # E2: the three audio levels. Nothing is ticked by default, so these are the untouched bases —
    # the exact backward-compatibility property, asserted through the real handler.
    assert outputs[9:12] == (35, 50, 50)
    assert "algorithm v1" in outputs[12]
    assert "Master seed 582913" in outputs[12]
    assert "6 randomized / 0 fixed" in outputs[12]
    assert "Audio · 0 randomized / 3 fixed (audio variation off)" in outputs[12]
    assert len(outputs) == 13


def test_the_real_handler_is_reproducible():
    assert _generate(582913, CINEMATIC) == _generate(582913, CINEMATIC)
    assert _generate(1, BALANCED, spread=100)[1:9] == (919454, 92, 4, 57, 99, 7, 61, "Custom")


@pytest.mark.parametrize("bad_master", [0, None, "", -5, 7.9, True])
def test_an_unusable_master_seed_is_replaced_by_a_fresh_visible_one(bad_master: Any):
    """No hidden randomness: the minted seed comes back as the first output, so it is on screen,
    and re-entering it reproduces the same recipe exactly."""
    outputs = _generate(bad_master, BALANCED)
    minted = outputs[0]

    assert isinstance(minted, int) and 1 <= minted <= 999_999
    assert _generate(minted, BALANCED) == outputs


def _new_variant(handlers, previous, base, spread=50, audio_randomized=None, audio_base=None):
    flat_ranges = [value for _ in FIELDS for value in (0, 100)]
    flat_audio_ranges = [value for _ in AUDIO_FIELDS for value in (0, 100)]
    audio_randomized = [] if audio_randomized is None else audio_randomized
    audio_base = dict(AUDIO_BASE_DEFAULTS) if audio_base is None else audio_base
    return handlers["_on_new_variant"](
        previous, spread, list(FIELDS), *flat_ranges,
        audio_randomized, *flat_audio_ranges,
        *[base[f] for f in FIELDS], *[audio_base[f] for f in AUDIO_FIELDS])


def test_new_variant_mints_a_fresh_visible_master_on_the_normal_path():
    handlers = _gui_handlers()
    seen = set()
    for _ in range(12):
        outputs = _new_variant(handlers, 582913, BALANCED)
        assert outputs[0] != 582913, "New Variant must discard the incoming master seed"
        assert 1 <= outputs[0] <= 999_999
        # and the minted master reproduces its own recipe from the same base
        assert _generate(outputs[0], BALANCED) == outputs
        seen.add(outputs[0])
    assert len(seen) > 1, "New Variant produced the same master every time"


@pytest.mark.parametrize("collision", [1, 2, 582913, 999_999])
def test_new_variant_is_guaranteed_not_probabilistic(monkeypatch, collision: int):
    """**R1-B.** `random_seed()` draws from 1..999999, so it can legitimately return the value
    already in the box. "New Variant always mints a new master" is a product contract, not a
    probability — so the collision is *forced* here rather than hoped against.

    The old version of this test relied on `SystemRandom` simply not returning 582913, which gave
    it a one-in-a-million failure mode. No test in this suite may carry one.
    """
    from beatsync_fork import variation as fork_variation

    monkeypatch.setattr(fork_variation, "random_seed", lambda: collision)
    handlers = _gui_handlers()

    # the helper, directly
    fresh = handlers["_fresh_variant_master_seed"](collision)
    assert fresh != collision
    assert 1 <= fresh <= 999_999

    # and through the button, which must still produce a usable reproducible recipe
    outputs = _new_variant(handlers, collision, CINEMATIC)
    assert outputs[0] != collision
    assert 1 <= outputs[0] <= 999_999
    assert outputs[1] > 0
    assert _generate(outputs[0], CINEMATIC) == outputs


@pytest.mark.parametrize("previous", [0, None, "", -5, 7.9, True, "nonsense"])
def test_a_forced_collision_against_an_unusable_previous_master_is_not_a_collision(
        monkeypatch, previous: Any):
    """An unset or malformed box has no master to differ *from*, so the drawn seed is used as-is."""
    from beatsync_fork import variation as fork_variation

    monkeypatch.setattr(fork_variation, "random_seed", lambda: 4242)
    handlers = _gui_handlers()
    assert handlers["_fresh_variant_master_seed"](previous) == 4242


def test_the_collision_step_stays_in_range_at_both_ends(monkeypatch):
    from beatsync_fork import variation as fork_variation
    handlers = _gui_handlers()

    monkeypatch.setattr(fork_variation, "random_seed", lambda: 1)
    assert handlers["_fresh_variant_master_seed"](1) == 2

    monkeypatch.setattr(fork_variation, "random_seed", lambda: 999_999)
    assert handlers["_fresh_variant_master_seed"](999_999) == 999_998


def test_new_variant_reads_the_previous_master_rather_than_ignoring_it():
    """"New" has to mean *different*, which is only checkable if the previous value is read."""
    tree = _tree(_GUI)
    body = ast.unparse(_strip_docstrings(_func(tree, "_on_new_variant")))
    assert "_fresh_variant_master_seed(variant_master_seed)" in body
    assert "_on_generate_variant(" in body
    assert "fork_lab.resolve" not in body

    helper = ast.unparse(_strip_docstrings(_func(tree, "_fresh_variant_master_seed")))
    assert "fork_variation.random_seed()" in helper
    assert "fork_lab.normalize_master_seed(previous)" in helper
    # one draw, never a retry loop waiting on SystemRandom to disagree
    assert "while" not in helper
    assert "for " not in helper


# ---------------------------------------------------------------------------
# R1-A: the REAL live-base replay semantics.
# ---------------------------------------------------------------------------


def test_generating_twice_with_the_same_master_intentionally_differs():
    """**R1-A.** The base is the live sliders, and Generate writes the recipe back into them — so
    an immediate second Generate resolves from the *first recipe*, not from the original preset.

    This is correct under the accepted live-base architecture, and it is pinned here precisely so
    the documentation cannot drift back to "a Master Seed alone reproduces a recipe". The clip seed
    is unaffected, because it depends on the master seed alone.
    """
    first = _generate(582913, CINEMATIC, spread=50)
    assert first[1:9] == (822019, 55, 15, 60, 63, 22, 62, "Custom")

    # the sliders now hold the first recipe; press Generate again with the same master
    second_base = dict(zip(FIELDS, first[2:8]))
    second = _generate(582913, second_base, spread=50)

    assert second[2:8] == (71, 9, 55, 77, 16, 71), "live-base semantics changed"
    assert second[2:8] != first[2:8]
    assert second[1] == first[1] == 822019, "the clip seed depends on the master seed alone"


def test_restoring_the_base_replays_the_original_recipe_exactly():
    """The actual reproducibility contract: same master **and** same base/config."""
    first = _generate(582913, CINEMATIC, spread=50)
    drifted = _generate(582913, dict(zip(FIELDS, first[2:8])), spread=50)
    assert drifted[2:8] != first[2:8]

    restored = _generate(582913, CINEMATIC, spread=50)
    assert restored[1:9] == (822019, 55, 15, 60, 63, 22, 62, "Custom")
    assert restored == first


@pytest.mark.parametrize("changed", ["spread", "randomized", "ranges"])
def test_replay_also_requires_the_same_lab_configuration(changed: str):
    """Not just the base: the master seed is one input among several, and the others matter too."""
    reference = _generate(582913, CINEMATIC, spread=50)
    if changed == "spread":
        other = _generate(582913, CINEMATIC, spread=51)
    elif changed == "randomized":
        other = _generate(582913, CINEMATIC, spread=50, randomized=["cut_density"])
    else:
        ranges = {f: (0, 100) for f in FIELDS}
        ranges["cut_density"] = (0, 40)
        other = _generate(582913, CINEMATIC, spread=50, ranges=ranges)

    assert other[2:8] != reference[2:8], changed
    assert other[1] == reference[1], "but the clip seed still depends on the master alone"


def test_the_master_seed_help_text_does_not_claim_master_only_reproducibility():
    """**R1-A structural guard, extended for E2 V1.** Two failures are pinned here, not one.

    *R1-A's.* The copy said "Type a master seed you used before and Generate to get that exact
    recipe back", which is false on its own: Generate overwrites the sliders it generated from.
    That half is unchanged — Master Seed alone is **not** a recipe identifier.

    *E2's.* The sentence that replaced it — "the exact render settings are always the Variation
    Seed plus the six sliders" — was true of C2's visual-only generator and became **incomplete**
    the moment Variant Lab could also write `music_under_voice`, `sfx_amount` and `sfx_level`. The
    guard below required only "variation seed" and "six sliders", so it went on passing. It now
    also requires the audio levels to be named.

    And it must not over-correct in the other direction. Those ten numbers are what the *lab*
    generates, not a complete description of the physical render: voice clips, voice timing, Avoid
    drops, the SFX folder, the enabled roles and the source media are render intent the lab never
    touches, and a replay needs them back too. The copy has to say so. Semantic facts are pinned
    here; the prose is free to change.
    """
    source = open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8").read()
    start = source.index("INFO_MASTER_SEED")
    info = source[start:source.index("\n)", start)]
    lowered = info.lower()

    # it must name the conditions
    assert "starting slider" in lowered or "starting preset" in lowered or "starting value" in lowered
    assert "spread" in lowered
    assert "overwrites the sliders" in lowered or "generate overwrites" in lowered

    # it must name every value a draw actually writes — the visual two since C2...
    assert "variation seed" in lowered and "six sliders" in lowered
    # ...and the three audio levels since E2 V1, including the opt-in that produces them
    assert "audio level" in lowered, "E2 V1 also generates the three audio levels"
    assert "audio variation" in lowered, "and the copy must name the opt-in they come from"

    # it must keep saying the seed is provenance and the widgets are what is read
    assert "never the master seed" in lowered or "not the master seed" in lowered

    # it must not claim the generated values are the whole render: at least one piece of
    # pre-existing render/resource intent has to be named as staying the user's
    assert any(token in lowered for token in ("voice clip", "sfx folder", "source video")), \
        "INFO_MASTER_SEED must not imply the generated values describe the entire render"

    # and it must not make the old unconditional promise, nor the C2-era exhaustiveness claim
    for overclaim in ("that exact recipe back", "get the same recipe back",
                      "reproduces the recipe", "always reproduces",
                      "exact render settings are always"):
        assert overclaim not in lowered, f"INFO_MASTER_SEED claims {overclaim!r}"


def test_the_variant_lab_help_text_explains_the_write_back():
    source = open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8").read()
    start = source.index("INFO_VARIANT_LAB")
    info = source[start:source.index("\n)", start)].lower()
    assert "starting point" in info
    assert "writes the result back" in info or "writes it back" in info


def test_the_real_handler_honours_fixed_controls_and_spread_zero():
    frozen = _generate(4242, CINEMATIC, spread=0)
    assert frozen[2:8] == tuple(CINEMATIC[f] for f in FIELDS)
    assert frozen[8] == "Cinematic", "spread 0 keeps the base preset's label"
    assert frozen[1] > 0, "but the clip seed is still a real variation"

    partial = _generate(4242, CINEMATIC, spread=100, randomized=["motion_bias"])
    assert partial[2] == CINEMATIC["cut_density"]
    assert partial[8] == "Custom" or partial[8] in fork_presets.PRESET_NAMES


def test_the_real_handler_survives_malformed_lab_configuration():
    """Every lab widget value is user-typeable, so none of it may raise mid-interaction."""
    ranges = {f: (None, "nonsense") for f in FIELDS}
    ranges["motion_bias"] = (80, 20)          # inverted -> degenerate
    outputs = _generate(582913, BALANCED, spread="not a number",
                        randomized=["cut_density", "unknown_control"], ranges=ranges)
    assert outputs[0] == 582913
    assert outputs[1] > 0
    assert outputs[2 + FIELDS.index("motion_bias")] == BALANCED["motion_bias"]
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in outputs[2:8])


def test_every_resolved_recipe_is_a_valid_recipe_and_profile():
    for master in MASTERS:
        for spread in (0, 50, 100):
            for base in (BALANCED, CINEMATIC, HIGH_ENERGY):
                recipe = _resolve(master=master, base=base, spread=spread).recipe
                assert fork_recipe.CreativeRecipe.from_mapping(recipe.as_mapping()) == recipe
                assert recipe.to_profile().as_dict() == recipe.as_mapping()
                for field in FIELDS:
                    value = getattr(recipe, field)
                    assert fork_creative.normalize_control(value) == value


# ===========================================================================
# 11. AUDIO VARIATION (E2 V1)
#
# The feature's load-bearing claim is that it landed *without* re-keying anything: the audio domain
# is a sibling of `controls`/`clips`, `_resolve_v1` was not touched, and the default is off. Every
# property below exists to make one of those claims falsifiable.
# ===========================================================================

AUDIO_BASE = {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50}

#: `int(sha1("variant_lab|1|<master>|audio|<name>").hexdigest()[:12], 16)`, computed independently
#: of this module (a standalone script importing nothing from the repo) and pasted in as literals.
#: Calling `rng_for` to produce the expectation would assert only that the function equals itself.
_GOLDEN_AUDIO_SEEDS = {
    "variant_lab|1|1|audio|music_under_voice_percent": 113_440_183_495_007,
    "variant_lab|1|1|audio|sfx_amount": 96_089_840_406_388,
    "variant_lab|1|1|audio|sfx_level_percent": 113_071_866_695_483,
    "variant_lab|1|582913|audio|music_under_voice_percent": 151_937_303_078_522,
    "variant_lab|1|582913|audio|sfx_amount": 217_584_134_425_872,
    "variant_lab|1|582913|audio|sfx_level_percent": 48_345_587_537_798,
    "variant_lab|1|999999|audio|music_under_voice_percent": 140_493_823_870_728,
    "variant_lab|1|999999|audio|sfx_amount": 9_444_017_410_338,
    "variant_lab|1|999999|audio|sfx_level_percent": 203_896_594_817_344,
}

#: Resolved audio recipes for the full 0..100 range with all three ticked, from the same
#: independent reimplementation of the spread formula. These pin the *whole* path — key format,
#: draw, anchor, directional headroom, half-up quantisation and clamp — not merely the hash.
_GOLDEN_AUDIO_RECIPES = {
    (1, 25): {"music_under_voice_percent": 45, "sfx_amount": 47, "sfx_level_percent": 49},
    (1, 50): {"music_under_voice_percent": 55, "sfx_amount": 44, "sfx_level_percent": 49},
    (1, 65): {"music_under_voice_percent": 61, "sfx_amount": 42, "sfx_level_percent": 48},
    (1, 100): {"music_under_voice_percent": 74, "sfx_amount": 37, "sfx_level_percent": 47},
    (582913, 25): {"music_under_voice_percent": 32, "sfx_amount": 46, "sfx_level_percent": 54},
    (582913, 50): {"music_under_voice_percent": 29, "sfx_amount": 42, "sfx_level_percent": 58},
    (582913, 65): {"music_under_voice_percent": 27, "sfx_amount": 40, "sfx_level_percent": 60},
    (582913, 100): {"music_under_voice_percent": 23, "sfx_amount": 34, "sfx_level_percent": 65},
    (999999, 25): {"music_under_voice_percent": 34, "sfx_amount": 45, "sfx_level_percent": 57},
    (999999, 50): {"music_under_voice_percent": 33, "sfx_amount": 40, "sfx_level_percent": 63},
    (999999, 65): {"music_under_voice_percent": 33, "sfx_amount": 37, "sfx_level_percent": 68},
    (999999, 100): {"music_under_voice_percent": 32, "sfx_amount": 30, "sfx_level_percent": 77},
}


def _audio(master=582913, spread=50, randomized=None, ranges=None, base=None):
    randomized = AUDIO_FIELDS if randomized is None else randomized
    config = fork_lab.AudioVariantConfig(randomized=randomized, ranges=ranges or {})
    return fork_lab.resolve_audio(master, config, spread,
                                  AUDIO_BASE if base is None else base)


# -- the stream names and the golden vectors --------------------------------


def test_the_three_audio_field_names_are_frozen():
    """These strings are RNG stream names, so renaming one silently re-keys that control."""
    assert fork_lab.AUDIO_CONTROL_FIELDS == (
        "music_under_voice_percent", "sfx_amount", "sfx_level_percent")


@pytest.mark.parametrize("key,expected", sorted(_GOLDEN_AUDIO_SEEDS.items()))
def test_golden_audio_derived_seeds(key: str, expected: int):
    """The stream `rng_for` builds must be the stream the literal seed builds, draw for draw.

    Comparing several draws rather than one makes the assertion about the generator's whole state
    instead of a single float that two different seeds could coincidentally share.
    """
    parts = key.split("|")
    stream = fork_lab.rng_for(int(parts[2]), fork_lab.DOMAIN_AUDIO, parts[4])
    reference = random.Random(expected)
    assert [stream.random() for _ in range(5)] == [reference.random() for _ in range(5)]


@pytest.mark.parametrize("key,expected", sorted(_GOLDEN_AUDIO_RECIPES.items()))
def test_golden_audio_recipes(key: tuple, expected: dict):
    master, spread = key
    assert _audio(master=master, spread=spread).recipe.as_mapping() == expected


def test_audio_resolution_is_deterministic():
    for master in MASTERS[:40]:
        assert _audio(master=master).recipe == _audio(master=master).recipe


def test_the_resolver_actually_consumes_the_audio_domain():
    """Not a tautology: if the resolver drew from `controls` instead, these would be equal."""
    name = "sfx_amount"
    audio_u = fork_lab.rng_for(582913, fork_lab.DOMAIN_AUDIO, name).uniform(-1.0, 1.0)
    control_u = fork_lab.rng_for(582913, fork_lab.DOMAIN_CONTROLS, name).uniform(-1.0, 1.0)
    assert audio_u != control_u

    anchor = AUDIO_BASE[name]
    expected = anchor + 0.5 * audio_u * ((anchor - 0) if audio_u < 0 else (100 - anchor))
    assert _audio(spread=50).recipe.sfx_amount == max(0, min(100, int(expected + 0.5)))


# -- independence: the principal E2 regression contract ---------------------


def test_audio_draws_do_not_move_the_clip_seed():
    reference = fork_lab.resolve_clip_seed(REF_MASTER)
    for name in AUDIO_FIELDS:
        fork_lab.rng_for(REF_MASTER, fork_lab.DOMAIN_AUDIO, name).uniform(-1.0, 1.0)
    _audio(master=REF_MASTER)
    assert fork_lab.resolve_clip_seed(REF_MASTER) == reference


def test_audio_resolution_does_not_move_any_visual_control():
    reference = _ref_recipe().as_mapping()
    for spread in (0, 25, 50, 100):
        _audio(master=REF_MASTER, spread=spread)
    assert _ref_recipe().as_mapping() == reference


def test_each_audio_field_has_its_own_stream():
    draws = {name: fork_lab.rng_for(582913, fork_lab.DOMAIN_AUDIO, name).random()
             for name in AUDIO_FIELDS}
    assert len(set(draws.values())) == len(AUDIO_FIELDS)


@pytest.mark.parametrize("target", AUDIO_FIELDS)
def test_enabling_or_disabling_one_audio_field_leaves_the_others_alone(target: str):
    others = [n for n in AUDIO_FIELDS if n != target]
    with_target = _audio(randomized=AUDIO_FIELDS).recipe.as_mapping()
    without_target = _audio(randomized=others).recipe.as_mapping()
    for name in others:
        assert with_target[name] == without_target[name], name


@pytest.mark.parametrize("target", AUDIO_FIELDS)
def test_re_ranging_one_audio_field_leaves_the_others_alone(target: str):
    reference = _audio().recipe.as_mapping()
    narrowed = _audio(ranges={target: (10, 20)}).recipe.as_mapping()
    for name in AUDIO_FIELDS:
        if name != target:
            assert narrowed[name] == reference[name], name
    assert 10 <= narrowed[target] <= 20


def test_reordering_the_audio_declarations_changes_nothing():
    assert _audio(randomized=list(AUDIO_FIELDS)).recipe == \
        _audio(randomized=list(reversed(AUDIO_FIELDS))).recipe


def test_a_future_fourth_audio_field_cannot_shift_todays_three():
    """The decisive sequential-RNG regression for E2, in both available directions."""
    reference = _audio().recipe.as_mapping()

    extended_ranges = dict(fork_lab.default_audio_ranges())
    extended_ranges["sfx_stutter"] = fork_lab.ControlRange(0, 100)
    extended = _audio(randomized=list(AUDIO_FIELDS) + ["sfx_stutter"],
                      ranges=extended_ranges).recipe.as_mapping()
    assert extended == reference

    # and even if such a field existed and were drawn, its stream is its own
    for future in ("sfx_stutter", "voice_pitch", "filter_sweep"):
        fork_lab.rng_for(582913, fork_lab.DOMAIN_AUDIO, future).uniform(-1.0, 1.0)
    assert _audio().recipe.as_mapping() == reference


def test_an_unknown_audio_field_is_dropped_not_honoured():
    config = fork_lab.AudioVariantConfig(randomized=["sfx_amount", "not_a_control"])
    assert config.randomized == frozenset({"sfx_amount"})


# -- the off switches -------------------------------------------------------


def test_the_default_audio_randomize_selection_is_empty():
    """Load-bearing backward compatibility: audio variation is opt-in, not opt-out."""
    assert fork_lab.default_audio_randomized() == frozenset()
    assert fork_lab.AudioVariantConfig().randomized == frozenset()
    assert fork_lab.AudioVariantConfig().varies_anything is False


def test_an_empty_selection_leaves_every_audio_value_at_its_base():
    for master in MASTERS[:20]:
        for spread in (0, 50, 100):
            resolution = _audio(master=master, spread=spread, randomized=[])
            assert resolution.recipe.as_mapping() == AUDIO_BASE
            assert resolution.fixed_fields == frozenset(AUDIO_FIELDS)


def test_spread_zero_leaves_every_audio_value_at_its_base():
    """Unlike C2, spread 0 genuinely varies **nothing** here — there is no audio clip seed."""
    for master in MASTERS[:20]:
        assert _audio(master=master, spread=0).recipe.as_mapping() == AUDIO_BASE


def test_a_field_with_randomize_off_ignores_its_configured_range():
    """Randomize OFF means *leave it alone*, not *clamp it into the range*."""
    resolution = _audio(randomized=["sfx_amount"],
                        ranges={"music_under_voice_percent": (90, 100),
                                "sfx_level_percent": (0, 5)})
    assert resolution.recipe.music_under_voice_percent == AUDIO_BASE["music_under_voice_percent"]
    assert resolution.recipe.sfx_level_percent == AUDIO_BASE["sfx_level_percent"]


# -- the trust boundary -----------------------------------------------------


@pytest.mark.parametrize("bad", [101, -1, 50.5, True, False, None, "50", float("nan"),
                                 float("inf")])
def test_audio_recipe_rejects_malformed_values_rather_than_clamping(bad: Any):
    with pytest.raises(ValueError):
        fork_lab.AudioRecipe(music_under_voice_percent=bad, sfx_amount=50, sfx_level_percent=50)


def test_audio_recipe_is_frozen_and_round_trips():
    recipe = fork_lab.AudioRecipe(music_under_voice_percent=35, sfx_amount=50,
                                  sfx_level_percent=50)
    with pytest.raises(Exception):
        recipe.sfx_amount = 10
    assert recipe.as_mapping() == AUDIO_BASE
    assert recipe.as_mapping() is not recipe.as_mapping()


def test_the_audio_recipe_carries_no_provenance_resource_or_structure():
    recipe = _audio().recipe
    for forbidden in ("master_seed", "spread", "ranges", "randomized", "algorithm_version",
                      "voice_files", "sfx_folder", "sfx_roles", "enabled_roles",
                      "avoid_drops", "start_delay_seconds", "min_gap_seconds"):
        assert not hasattr(recipe, forbidden), forbidden
        assert forbidden not in recipe.as_mapping(), forbidden
    assert set(recipe.as_mapping()) == set(AUDIO_FIELDS)


def test_provenance_lives_on_the_resolution_not_the_recipe():
    resolution = _audio(master=582913, spread=65)
    assert resolution.master_seed == 582913
    assert resolution.spread == 65
    assert resolution.algorithm_version == fork_lab.VARIANT_LAB_ALGORITHM_VERSION
    assert isinstance(resolution.config, fork_lab.AudioVariantConfig)


def test_no_audio_field_was_added_to_the_visual_recipe_or_config():
    """E2's data model is a sibling, never a widening of the visual execution artifact."""
    recipe = _ref_recipe()
    config = fork_lab.VariantLabConfig(master_seed=1)
    for name in AUDIO_FIELDS:
        assert not hasattr(recipe, name), name
        assert name not in recipe.as_mapping(), name
        assert not hasattr(config, name), name
    assert set(recipe.as_mapping()) == set(fork_recipe.VARIANT_RECIPE_FIELDS)


def test_resolve_audio_requires_a_positive_master_seed():
    for bad in (0, None, "", -5, 7.9, True):
        with pytest.raises(ValueError):
            fork_lab.resolve_audio(bad, fork_lab.AudioVariantConfig(), 50, AUDIO_BASE)


# -- totality ---------------------------------------------------------------


def test_malformed_audio_input_is_total_and_never_raises():
    resolution = fork_lab.resolve_audio(
        582913,
        fork_lab.AudioVariantConfig(randomized="not iterable",
                                    ranges={"sfx_amount": (80, 20), "sfx_level_percent": None}),
        "not a number",
        "not a mapping")
    assert resolution.recipe.as_mapping() == AUDIO_BASE


def test_each_audio_base_uses_its_own_owning_normaliser():
    """The music floor falls back to **35** and the two Smart Mix levels to **50**.

    A single shared 0..100 normaliser would make all three fall back to 50, silently raising the
    music-under-voice floor whenever a widget value was malformed. This is the test that fails if
    someone "simplifies" the three delegations into `creative.normalize_control`.
    """
    assert fork_lab.normalize_audio_base_value("music_under_voice_percent", "junk") == 35
    assert fork_lab.normalize_audio_base_value("sfx_amount", "junk") == 50
    assert fork_lab.normalize_audio_base_value("sfx_level_percent", "junk") == 50

    resolved = fork_lab.resolve_audio(
        1, fork_lab.AudioVariantConfig(), 50,
        {"music_under_voice_percent": None, "sfx_amount": None, "sfx_level_percent": None})
    assert resolved.recipe.as_mapping() == AUDIO_BASE


def test_every_resolved_audio_recipe_is_in_range():
    for master in MASTERS:
        for spread in (0, 50, 100):
            mapping = _audio(master=master, spread=spread).recipe.as_mapping()
            assert all(isinstance(v, int) and 0 <= v <= 100 for v in mapping.values())


# -- the read-out -----------------------------------------------------------


def test_the_audio_describe_tells_the_truth_about_being_off():
    assert "audio variation off" in _audio(randomized=[]).describe()
    assert "held at base" in _audio(spread=0).describe()
    on = _audio(spread=50).describe()
    assert "3 randomized / 0 fixed" in on
    assert "audio variation off" not in on and "held at base" not in on


def test_the_audio_describe_reports_the_resolved_values():
    text = _audio(master=582913, spread=65).describe()
    assert "Music under voice 27" in text
    assert "SFX Amount 40" in text
    assert "SFX Level 60" in text


# ===========================================================================
# 12. THE E2 GUI EXTENSION
# ===========================================================================

#: The seven E2 configuration components, by `elem_id`. One CheckboxGroup and three min/max pairs.
E2_CONFIG_ELEM_IDS = (
    "variant-audio-randomize-group",
    "variant-range-music-under-voice-min", "variant-range-music-under-voice-max",
    "variant-range-sfx-amount-min", "variant-range-sfx-amount-max",
    "variant-range-sfx-level-min", "variant-range-sfx-level-max",
)


def _widget_calls(tree):
    """Every `gr.X(...)` call in the module, keyed by its `elem_id` where it has one."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            elem = next((kw.value for kw in node.keywords if kw.arg == "elem_id"), None)
            if isinstance(elem, ast.Constant) and isinstance(elem.value, str):
                found[elem.value] = node
    return found


def test_e2_adds_exactly_seven_configuration_widgets():
    """One CheckboxGroup + three min/max Number pairs, and no eighth component.

    A second master seed or a second Spread would show up here as an extra widget, which is the
    failure this counts rather than describes.
    """
    widgets = _widget_calls(_tree(_GUI))
    present = [e for e in E2_CONFIG_ELEM_IDS if e in widgets]
    assert present == list(E2_CONFIG_ELEM_IDS), \
        f"missing {set(E2_CONFIG_ELEM_IDS) - set(present)}"

    assert ast.unparse(widgets[E2_CONFIG_ELEM_IDS[0]].func).endswith("CheckboxGroup")
    for elem in E2_CONFIG_ELEM_IDS[1:]:
        assert ast.unparse(widgets[elem].func).endswith("Number")

    # no second seed / spread smuggled into the audio subsection
    for forbidden in ("variant-audio-master-seed", "variant-audio-spread",
                      "audio-variation-spread-slider"):
        assert forbidden not in widgets, forbidden


def test_the_audio_randomize_group_defaults_to_nothing_ticked():
    group = _widget_calls(_tree(_GUI))["variant-audio-randomize-group"]
    value = next(kw.value for kw in group.keywords if kw.arg == "value")
    rendered = ast.unparse(value)
    # either a literal empty list, or the resolver's own documented empty default
    assert rendered in ("[]", "sorted(fork_lab.default_audio_randomized())",
                        "list(fork_lab.default_audio_randomized())"), rendered
    assert fork_lab.default_audio_randomized() == frozenset()


def test_the_audio_randomize_choices_are_explicit_label_value_pairs():
    """The returned value must BE the field name, never derived from the display label.

    These three strings are RNG stream names: a lowercase/replace heuristic over a reworded label
    would silently re-key a control, which is a correctness hazard rather than a cosmetic one.
    """
    group = _widget_calls(_tree(_GUI))["variant-audio-randomize-group"]
    choices = next(kw.value for kw in group.keywords if kw.arg == "choices")
    assert isinstance(choices, ast.List)
    assert len(choices.elts) == len(AUDIO_FIELDS)

    values = []
    for element in choices.elts:
        assert isinstance(element, ast.Tuple) and len(element.elts) == 2, ast.unparse(element)
        label, value = element.elts
        assert isinstance(label, ast.Name), "label should be a LABEL_* constant"
        assert isinstance(value, ast.Constant) and isinstance(value.value, str)
        values.append(value.value)
    assert values == list(AUDIO_FIELDS)

    rendered = ast.unparse(choices)
    for heuristic in (".lower()", ".replace(", ".strip()", ".casefold()"):
        assert heuristic not in rendered, heuristic


def test_the_handler_resolves_audio_from_the_same_master_seed_and_spread():
    """One visible seed, one visible Spread, both halves. No hidden second source of randomness."""
    tree = _tree(_GUI)
    body = ast.unparse(_strip_docstrings(_func(tree, "_on_generate_variant")))

    # `config.spread` since C3 rather than the raw widget: `resolve_audio` normalises its own
    # `spread` argument and `normalize_spread` is idempotent, so the value is identical — and the
    # normalised one is what a batch declaration records.
    assert "fork_lab.resolve_audio(master_seed, audio_config, config.spread, audio_base)" in body
    assert "fork_lab.resolve(config, base)" in body
    # the only non-deterministic call stays the visible master-seed mint, now in the shared helper
    context = ast.unparse(_strip_docstrings(
        _func(tree, "_build_variant_resolution_context")))
    assert context.count("fork_variation.random_seed()") == 1
    assert "fork_variation.random_seed()" not in body
    for forbidden in ("rng_for", "DOMAIN_AUDIO", "DOMAIN_BATCH", "SystemRandom", "audio_spread",
                      "audio_master_seed"):
        assert forbidden not in body, forbidden
        assert forbidden not in context, forbidden


def test_the_live_audio_widgets_are_the_audio_bases():
    """The base is the live widget value, read at click time — no cached audio snapshot.

    **Structurally updated by C3**: this construction moved into the shared normalisation helper,
    so all three Variant Lab handlers derive each audio base through the normaliser that *owns*
    that control. One place, three callers — the 35/50/50 split cannot drift between them.
    """
    body = ast.unparse(_strip_docstrings(
        _func(_tree(_GUI), "_build_variant_resolution_context")))
    for expected in (
            "'music_under_voice_percent': fork_audio_mix.normalize_music_under_voice("
            "music_under_voice)",
            "'sfx_amount': fork_smart_mix.normalize_control(sfx_amount, "
            "fork_smart_mix.DEFAULT_AMOUNT)",
            "'sfx_level_percent': fork_smart_mix.normalize_control(sfx_level, "
            "fork_smart_mix.DEFAULT_SFX_LEVEL_PERCENT)"):
        assert expected in body, expected
    # and NOT through the visual normaliser, whose fallback is 50 rather than 35
    assert "normalize_control(music_under_voice" not in body
    assert "fork_creative" not in body


def test_there_is_no_hidden_audio_state():
    """No `gr.State`, no module global, no cached snapshot — the widgets are the whole truth."""
    tree = _tree(_GUI)
    widgets = _widget_calls(tree)
    for elem in widgets:
        assert "audio-variant-state" not in elem and "variant-audio-state" not in elem

    source = _executable_source(_GUI)
    for forbidden in ("_AUDIO_VARIANT_CACHE", "_LAST_AUDIO_RECIPE", "_audio_base_snapshot",
                      "AUDIO_VARIANT_STATE_KEY"):
        assert forbidden not in source, forbidden

    # the audio resolution is consumed by the projection helper and never stored
    body = ast.unparse(_strip_docstrings(_func(tree, "_on_generate_variant")))
    assert "global " not in body
    assert "session_state" not in body


def test_generating_audio_still_renders_nothing():
    """E2 added outputs, not a render path."""
    tree = _tree(_GUI)
    for button in ("generate_variant_btn", "new_variant_btn"):
        for call in _registration(tree, button, attrs=("click",)):
            outputs = _expanded_list(tree, "variant_lab_outputs")
            assert "video_output" not in outputs
            assert "status_output" not in outputs
            assert _kwargs(call).get("fn") is not None
    for handler in ("_on_generate_variant", "_on_new_variant", "_variant_apply_outputs"):
        body = ast.unparse(_strip_docstrings(_func(tree, handler)))
        for forbidden in ("create_music_video", "analyze_beats_auto", "process_video",
                          "build_mixed_master", "AudioMixConfig", "SmartMixConfig",
                          "prepare_voice_inputs", "prepare_sfx_inputs"):
            assert forbidden not in body, f"{handler} references {forbidden}"


def test_the_projection_helper_formats_no_text_of_its_own():
    """Both describe() formatters stay single-sourced; the GUI only joins them."""
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_variant_apply_outputs")))
    assert "resolution.describe()" in body
    assert "audio_resolution.describe()" in body
    assert "Audio ·" not in body and "Music under voice" not in body
    assert "SFX Amount" not in body and "SFX Level" not in body


# ===========================================================================
# 14. VARIANT LAB C3 V1 — generate N, compare N, apply exactly one
# ===========================================================================

#: The C3 handlers, extracted and executed the same way the C2/E2 ones are. `gr` is stubbed because
#: the real handlers legitimately return `gr.update(...)` / `gr.skip()` sentinels, and `fork_batch`
#: is the pure module they orchestrate — both belong to the real dependency set, so stubbing
#: anything *else* would be mocking the code under test rather than its environment. The
#: declaration gate, the staleness refusal and the Apply projection are all genuinely executed here.
_C3_HANDLER_NAMES = ("_variant_apply_outputs", "_build_variant_resolution_context",
                     "_variant_batch_skips", "_cleared_candidate_selectors",
                     "_on_generate_variants", "_on_apply_selected_variant")


class _FakeUpdate:
    """Stands in for `gr.update(...)`, recording what the handler asked the widget to become."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __eq__(self, other):
        return isinstance(other, _FakeUpdate) and self.kwargs == other.kwargs

    def __repr__(self):
        return f"update({self.kwargs})"


class _FakeSkip:
    """Stands in for `gr.skip()` — "leave this widget exactly as it is"."""

    def __eq__(self, other):
        return isinstance(other, _FakeSkip)

    def __repr__(self):
        return "skip"


class _FakeGradio:
    update = staticmethod(lambda **kwargs: _FakeUpdate(**kwargs))
    skip = staticmethod(_FakeSkip)


def _c3_handlers():
    from beatsync_fork import audio_mix as fork_audio_mix
    from beatsync_fork import smart_mix as fork_smart_mix
    from beatsync_fork import variation as fork_variation
    from typing import Tuple

    tree = _tree(_GUI)
    wanted = {name: None for name in _C3_HANDLER_NAMES}
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            wanted[node.name] = node
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", "").startswith("_VARIANT_APPLY")):
            constants[node.targets[0].id] = ast.literal_eval(node.value)
    missing = [name for name, node in wanted.items() if node is None]
    assert not missing, f"C3 handlers not found at module level: {missing}"

    namespace = {"fork_presets": fork_presets, "fork_lab": fork_lab, "fork_batch": fork_batch,
                 "fork_variation": fork_variation, "fork_audio_mix": fork_audio_mix,
                 "fork_smart_mix": fork_smart_mix, "Tuple": Tuple, "gr": _FakeGradio,
                 "LABEL_APPLY_VARIANT": "Apply Selected Variant", **constants}
    exec(compile(ast.Module(body=list(wanted.values()), type_ignores=[]),
                 filename=_GUI, mode="exec"), namespace)
    return namespace


def _lab_args(master=582913, base=None, spread=50, randomized=None, ranges=None,
              audio_randomized=None, audio_ranges=None, audio_base=None):
    """The thirty positional Variant Lab arguments, in `variant_lab_inputs` order."""
    base = CINEMATIC if base is None else base
    randomized = list(FIELDS) if randomized is None else randomized
    ranges = {f: (0, 100) for f in FIELDS} if ranges is None else ranges
    audio_randomized = list(AUDIO_FIELDS) if audio_randomized is None else audio_randomized
    audio_ranges = ({f: (0, 100) for f in AUDIO_FIELDS}
                    if audio_ranges is None else audio_ranges)
    audio_base = dict(AUDIO_BASE_DEFAULTS) if audio_base is None else audio_base
    return ([master, spread, randomized]
            + [v for f in FIELDS for v in ranges[f]]
            + [audio_randomized]
            + [v for f in AUDIO_FIELDS for v in audio_ranges[f]]
            + [base[f] for f in FIELDS]
            + [audio_base[f] for f in AUDIO_FIELDS])


def _generate_batch(count=5, handlers=None, **kwargs):
    handlers = _c3_handlers() if handlers is None else handlers
    return handlers["_on_generate_variants"](count, *_lab_args(**kwargs))


def _apply(batch, selection, count=5, handlers=None, **kwargs):
    handlers = _c3_handlers() if handlers is None else handlers
    return handlers["_on_apply_selected_variant"](
        batch, selection, count, *_lab_args(**kwargs))


# --- the widgets -----------------------------------------------------------

C3_ELEM_IDS = ("variant-candidate-count", "generate-variants-button", "variant-batch-table",
               "variant-candidate-selector", "apply-variant-button", "variant-batch-status")


def test_the_c3_components_exist_exactly_once_each():
    source = _executable_source(_GUI)
    widgets = _widget_calls(_tree(_GUI))
    for elem in C3_ELEM_IDS:
        assert elem in widgets, elem
        assert source.count(f"'{elem}'") == 1, f"{elem} is declared more than once"


def test_the_candidate_count_widget_carries_the_shipped_bounds():
    kwargs = {kw.arg: ast.unparse(kw.value)
              for kw in _widget_calls(_tree(_GUI))["variant-candidate-count"].keywords}
    assert kwargs["value"].endswith("CANDIDATE_COUNT_DEFAULT")
    assert kwargs["minimum"].endswith("CANDIDATE_COUNT_MIN")
    assert kwargs["maximum"].endswith("CANDIDATE_COUNT_MAX")
    assert kwargs["precision"] == "0"
    assert (fork_batch.CANDIDATE_COUNT_MIN, fork_batch.CANDIDATE_COUNT_DEFAULT,
            fork_batch.CANDIDATE_COUNT_MAX) == (2, 5, 12)


def test_the_comparison_table_and_status_are_read_only():
    widgets = _widget_calls(_tree(_GUI))
    for elem in ("variant-batch-table", "variant-batch-status"):
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in widgets[elem].keywords}
        assert ast.unparse(widgets[elem].func).endswith("Textbox")
        assert kwargs["interactive"] == "False"
        assert kwargs["value"] == "''"


def test_the_candidate_selector_is_a_radio_that_registers_no_handler():
    """Selection is read by Apply at click time; a `.change()` writer would be a second path."""
    tree = _tree(_GUI)
    assert ast.unparse(_widget_calls(tree)["variant-candidate-selector"].func).endswith("Radio")
    for widget in ("variant_candidate_selector", "variant_candidate_count",
                   "variant_batch_table", "variant_batch_status"):
        assert _registration(tree, widget) == [], f"{widget} registers a handler of its own"


def test_the_c3_state_starts_empty():
    assert "variant_batch_state = gr.State(None)" in _executable_source(_GUI)


def test_c3_added_no_handler_to_any_declaration_widget():
    """**Deliberately no eager invalidation.** The creative sliders keep exactly one `.input()`
    binding each, and the audio controls keep none — the live Apply gate is the authority, so a
    second handler per widget would be machinery bought for nothing and a cycle risk besides."""
    tree = _tree(_GUI)
    for widget in list(FIELDS) + AUDIO_BASE_WIDGETS + list(AUDIO_CONFIG_WIDGETS) + [
            "variant_master_seed", "variation_spread", "variant_randomize"]:
        registrations = _registration(tree, widget)
        if widget in FIELDS:
            assert len(registrations) == 1, f"{widget} must keep exactly one handler"
            assert registrations[0].func.attr == "input"
            assert _names(_kwargs(registrations[0])["outputs"]) == ["creative_preset"]
        else:
            assert registrations == [], f"{widget} gained a handler"


# --- Generate Variants -----------------------------------------------------


def test_generate_variants_reads_every_declaration_value_in_order():
    """Its inputs are the count plus `variant_lab_inputs` verbatim — one list, one alignment."""
    tree = _tree(_GUI)
    assert len(_registration(tree, "generate_variants_btn", attrs=("click",))) == 1
    inputs = _expanded_list(tree, "variant_batch_inputs")
    assert inputs == ["variant_candidate_count"] + _expanded_list(tree, "variant_lab_inputs")

    parameters = [a.arg for a in _func(tree, "_on_generate_variants").args.args]
    assert parameters == inputs, "Gradio passes positionally; the two are one contract"


def test_generate_variants_writes_no_execution_widget():
    """The structural reason candidate chaining is impossible rather than merely avoided.

    Read off the **registration**, not off the named list: `outputs=variant_batch_outputs + [x]`
    would satisfy a list-only check while writing `x` on every press.
    """
    tree = _tree(_GUI)
    call = _registration(tree, "generate_variants_btn", attrs=("click",))[0]
    declared = _ordered_names(_kwargs(call)["outputs"])
    assert declared == ["variant_batch_outputs"], "no widget may be appended at the call site"

    outputs = _expanded_list(tree, "variant_batch_outputs")
    assert outputs == ["variant_master_seed", "variant_batch_state", "variant_batch_table",
                       "variant_candidate_selector", "variant_render_selector",
                       "variant_batch_status"]
    for forbidden in (["variation_seed", "creative_preset", "variant_report"] + list(FIELDS)
                      + list(AUDIO_BASE_WIDGETS)):
        assert forbidden not in outputs, f"generating candidates writes {forbidden}"


def test_generating_candidates_renders_nothing_and_returns_a_real_batch():
    handlers = _c3_handlers()
    master, batch, table, selector, render_selector, status = _generate_batch(handlers=handlers)
    assert master == 582913
    assert isinstance(batch, fork_batch.VariantBatch)
    assert len(batch.candidates) == 5
    assert "root master 582913" in table
    assert isinstance(selector, _FakeUpdate)
    assert selector.kwargs["value"] is None
    assert [v for _l, v in selector.kwargs["choices"]] == [0, 1, 2, 3, 4]
    assert "generated" in status.lower()


@pytest.mark.parametrize("unusable", [0, None, "", -5, 7.9, True])
def test_an_unusable_root_master_is_minted_and_surfaced(unusable):
    master, batch, _table, _selector, _render, _status = _generate_batch(master=unusable)
    assert isinstance(master, int) and master > 0
    assert batch.declaration.root_master_seed == master, \
        "the batch must be built from the seed the user can actually see"


def test_the_generated_batch_is_exactly_what_the_pure_resolver_produces():
    """The handler normalises and delegates; it must not become a second resolution path."""
    _master, batch, table, _selector, _render_selector, _status = _generate_batch(count=7, spread=70)
    config = fork_lab.VariantLabConfig(
        master_seed=582913, spread=70, randomized=frozenset(FIELDS),
        ranges={f: (0, 100) for f in FIELDS})
    audio_config = fork_lab.AudioVariantConfig(
        randomized=frozenset(AUDIO_FIELDS), ranges={f: (0, 100) for f in AUDIO_FIELDS})
    expected = fork_batch.resolve_batch(fork_batch.declaration_from(
        config, CINEMATIC, audio_config, AUDIO_BASE_DEFAULTS, 7))
    assert batch == expected
    assert table == expected.table_text()


def test_the_batch_handed_to_state_survives_a_deep_copy():
    """Gradio deep-copies `State`; a config or resolution in here would fail at runtime."""
    import copy
    _master, batch, _table, _selector, _render_selector, _status = _generate_batch(count=12)
    assert copy.deepcopy(batch) == batch


def test_every_candidate_in_the_handler_output_shares_one_frozen_base():
    _master, batch, _t, _s, _rs, _st = _generate_batch(count=6, base=CINEMATIC, spread=100)
    assert batch.declaration.visual_base == tuple(CINEMATIC[f] for f in FIELDS)
    assert batch.declaration.audio_base == (35, 50, 50)


# --- Apply Selected --------------------------------------------------------


def test_apply_reads_the_live_declaration_inputs():
    tree = _tree(_GUI)
    calls = _registration(tree, "apply_variant_btn", attrs=("click",))
    assert len(calls) == 1
    # source order, not `ast.walk` order: this list is a positional contract with the signature
    assert _ordered_names(_kwargs(calls[0])["inputs"]) == [
        "variant_batch_state", "variant_candidate_selector", "variant_batch_inputs"]

    expected = (["variant_batch_state", "variant_candidate_selector"]
                + _expanded_list(tree, "variant_batch_inputs"))
    parameters = [a.arg for a in _func(tree, "_on_apply_selected_variant").args.args]
    assert parameters == expected, "Gradio passes positionally; the two are one contract"


def test_apply_writes_the_ordinary_variant_lab_output_set_plus_its_own_three():
    calls = _registration(_tree(_GUI), "apply_variant_btn", attrs=("click",))
    assert _ordered_names(_kwargs(calls[0])["outputs"]) == [
        "variant_lab_outputs", "variant_batch_state", "variant_candidate_selector",
        "variant_render_selector", "variant_batch_status"]


def test_applying_a_candidate_writes_exactly_that_candidate():
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(count=5, handlers=handlers)
    for index, candidate in enumerate(batch.candidates):
        applied = _apply(batch, index, handlers=handlers)
        assert applied[0] == candidate.master_seed, "the Master Seed box shows the CANDIDATE"
        assert applied[1] == candidate.creative_recipe.seed
        assert tuple(applied[2:8]) == candidate.visual_values()
        assert applied[8] == candidate.preset_label
        assert tuple(applied[9:12]) == candidate.audio_values()
        assert str(candidate.master_seed) in applied[12], "the report quotes the same provenance"


def test_apply_recomputes_the_preset_label_through_the_existing_projection():
    """Programmatic writes do not fire `.input()`, so the label must be computed explicitly."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(
        count=3, spread=0, base=CINEMATIC, handlers=handlers)
    applied = _apply(batch, 0, count=3, spread=0, base=CINEMATIC, handlers=handlers)
    assert tuple(applied[2:8]) == tuple(CINEMATIC[f] for f in FIELDS)
    assert applied[8] == "Cinematic", "spread 0 holds the base, so the base preset still matches"


def test_a_successful_apply_consumes_the_batch_and_clears_the_selector():
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    applied = _apply(batch, 2, handlers=handlers)
    assert applied[13] is None, "the batch is consumed"
    assert applied[14] == _FakeUpdate(choices=[], value=None)
    assert "applied candidate 3" in applied[16].lower()


def test_an_unchanged_screen_applies_successfully():
    """The gate must be a gate, not a wall: the untouched case has to work."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    applied = _apply(batch, 1, handlers=handlers)
    assert not any(isinstance(v, _FakeSkip) for v in applied[:13])


_STALE_EDITS = {
    "visual base moved": dict(base=dict(CINEMATIC, motion_bias=31)),
    "spread moved": dict(spread=51),
    "root master retyped": dict(master=582914),
    "randomize changed": dict(randomized=["cut_density"]),
    "range narrowed": dict(ranges={**{f: (0, 100) for f in FIELDS},
                                   "cut_density": (10, 40)}),
    "audio randomize changed": dict(audio_randomized=[]),
    "audio range narrowed": dict(audio_ranges={**{f: (0, 100) for f in AUDIO_FIELDS},
                                               "sfx_amount": (20, 30)}),
    "audio base moved": dict(audio_base={"music_under_voice_percent": 40,
                                         "sfx_amount": 50, "sfx_level_percent": 50}),
}


@pytest.mark.parametrize("label", sorted(_STALE_EDITS))
def test_a_stale_apply_changes_no_execution_widget_and_consumes_the_batch(label):
    """The live declaration is the authority — the whole reason the gate exists."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    applied = _apply(batch, 0, handlers=handlers, **_STALE_EDITS[label])

    for value in applied[:13]:
        assert isinstance(value, _FakeSkip), f"{label}: an execution widget was written"
    assert applied[13] is None, f"{label}: the stale batch must be consumed"
    assert applied[14] == _FakeUpdate(choices=[], value=None)
    assert "no longer describe" in applied[16].lower()


def test_a_changed_candidate_count_is_also_stale():
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(count=5, handlers=handlers)
    applied = _apply(batch, 0, count=8, handlers=handlers)
    for value in applied[:13]:
        assert isinstance(value, _FakeSkip)
    assert applied[13] is None


def test_a_stale_apply_cannot_be_retried():
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    first = _apply(batch, 0, handlers=handlers, spread=51)
    assert first[13] is None
    # the GUI now holds None, so a second press refuses earlier and still writes nothing
    second = _apply(first[13], 0, handlers=handlers, spread=51)
    for value in second[:13]:
        assert isinstance(value, _FakeSkip)
    assert "generate variants" in second[16].lower()


@pytest.mark.parametrize("selection", [None, -1, 99, "0", 1.5, True])
def test_apply_without_a_usable_selection_keeps_the_batch_and_writes_nothing(selection):
    """Not stale — the list is still valid, the user simply has not chosen. Prompt, not punish."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    applied = _apply(batch, selection, handlers=handlers)
    for value in applied[:13]:
        assert isinstance(value, _FakeSkip)
    assert isinstance(applied[13], _FakeSkip), "a valid list must survive a missed selection"
    assert isinstance(applied[14], _FakeSkip)
    assert "select a candidate" in applied[16].lower()


@pytest.mark.parametrize("empty", [None, "", 0, [], {}])
def test_apply_with_no_batch_at_all_refuses_and_writes_nothing(empty):
    handlers = _c3_handlers()
    applied = _apply(empty, 0, handlers=handlers)
    for value in applied[:13]:
        assert isinstance(value, _FakeSkip)
    assert "generate variants" in applied[16].lower()


def test_apply_produces_exactly_what_a_single_generate_from_that_master_would():
    """Apply must land on the ordinary Generate output, not on something adjacent to it."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(handlers=handlers)
    candidate = batch.candidates[3]
    applied = _apply(batch, 3, handlers=handlers)
    direct = _generate(candidate.master_seed, CINEMATIC,
                       audio_randomized=list(AUDIO_FIELDS))
    assert applied[:13] == direct


# --- isolation -------------------------------------------------------------


def test_the_batch_state_never_reaches_the_render_or_gate_paths():
    tree = _tree(_GUI)
    process = _registration(tree, "process_btn", attrs=("click",))[0]
    assert "variant_batch_state" not in _names(_kwargs(process)["inputs"])
    assert "variant_batch_state" not in _names(_kwargs(process)["outputs"])

    for list_name in ("source_outputs", "prep_outputs", "variant_lab_inputs",
                      "variant_lab_outputs"):
        assert "variant_batch_state" not in _expanded_list(tree, list_name), list_name

    for name in ("process_video_guarded", "process_video", "_process_video_impl"):
        body = ast.unparse(_strip_docstrings(_func(tree, name)))
        for word in ("variant_batch", "fork_batch", "VariantBatch", "candidate"):
            assert word not in body, f"{name} references {word}"


def test_only_apply_reads_the_batch_state():
    tree = _tree(_GUI)
    readers = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release"}):
            inputs = next((kw.value for kw in node.keywords if kw.arg == "inputs"), None)
            if inputs is None:
                continue
            reachable = set(_names(inputs))
            for alias in list(reachable):
                if _list_valued_assign(tree, alias) is not None:
                    reachable.update(_expanded_list(tree, alias))
            if "variant_batch_state" in reachable:
                readers.append(ast.unparse(node.func))
    # **C3-R0 makes this exactly TWO readers.** Rendering legitimately needs the stored
    # candidates; nothing else may. Still an exact list, never a containment check.
    assert readers == ["apply_variant_btn.click",
                       "render_selected_variants_btn.click"], readers


def test_the_c3_handlers_touch_no_gradio_component_directly():
    """They return update/skip sentinels; they never reach into a component or a global."""
    tree = _tree(_GUI)
    for name in ("_on_generate_variants", "_on_apply_selected_variant"):
        body = ast.unparse(_strip_docstrings(_func(tree, name)))
        for forbidden in ("gr.Textbox", "gr.Radio", "gr.Number", "gr.Button", "gr.State",
                          "session_state", "global "):
            assert forbidden not in body, f"{name} references {forbidden}"


def test_c3_introduced_no_hidden_execution_state():
    source = _executable_source(_GUI)
    for forbidden in ("_VARIANT_BATCH_CACHE", "_LAST_BATCH", "_candidate_snapshot",
                      "VARIANT_BATCH_STATE_KEY", "_batch_base_snapshot"):
        assert forbidden not in source, forbidden


def test_new_variant_still_delegates_to_the_one_single_variant_path():
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_on_new_variant")))
    assert "_on_generate_variant(" in body
    assert "_on_generate_variants(" not in body, "New Variant stays single-variant"
    assert "fork_batch" not in body


# --- R1: Apply validation is deterministic and never mints -----------------


class _CountingRandomSeed:
    """A `random_seed` stand-in that both counts calls and returns the worst possible value."""

    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


#: Every spelling of "the Master Seed box no longer holds a usable value". `normalize_seed`
#: refuses each of them, which is exactly why R0's mint branch fired on all of them. Note what is
#: deliberately absent: a numeric *string*. `variation.normalize_seed` accepts `"582913"` as 582913,
#: so that box is still declaring the same root and Apply must succeed — a test below pins it, and
#: listing it here would have asserted the opposite of the truth.
_UNUSABLE_MASTERS = [0, None, "", -5, 7.9, True, float("nan"), float("inf")]


@pytest.mark.parametrize("unusable", _UNUSABLE_MASTERS)
def test_apply_never_mints_a_master_while_validating(unusable, monkeypatch):
    """**R1 — the load-bearing regression.** Apply's gate must not depend on a draw.

    R0 reused the Generate normalisation wholesale, so rebuilding the live declaration minted a
    master whenever the box was unusable. With the box blanked, a `random_seed()` that happened to
    return the batch's own root made the reconstructed declaration compare **equal**, and Apply
    wrote a candidate for a screen that no longer declared that root — a correctness decision
    resolved by a one-in-a-million draw, and an unsurfaced one at that.

    This forces the unlucky draw rather than waiting for it: `random_seed` is patched to return
    exactly the batch root. Apply must still refuse, and must not have called it at all.
    """
    handlers = _c3_handlers()
    root = 582913
    _master, batch, _t, _s, _rs, _st = _generate_batch(master=root, handlers=handlers)
    assert batch.declaration.root_master_seed == root

    spy = _CountingRandomSeed(root)
    monkeypatch.setattr(handlers["fork_variation"], "random_seed", spy)

    applied = _apply(batch, 0, handlers=handlers, master=unusable)

    assert spy.calls == 0, "Apply must not draw while validating a candidate list"
    for value in applied[:13]:
        assert isinstance(value, _FakeSkip), "a refusal must change no execution widget"
    assert applied[13] is None, "the stale batch must be consumed"
    assert applied[14] == _FakeUpdate(choices=[], value=None)
    assert "no longer describe" in applied[16].lower()


def test_an_unusable_live_master_is_stale_deterministically_every_time(monkeypatch):
    """Not "usually stale": the same refusal a hundred times, with the draw rigged against us."""
    handlers = _c3_handlers()
    root = 582913
    _master, batch, _t, _s, _rs, _st = _generate_batch(master=root, handlers=handlers)
    spy = _CountingRandomSeed(root)
    monkeypatch.setattr(handlers["fork_variation"], "random_seed", spy)

    for _ in range(100):
        applied = _apply(batch, 0, handlers=handlers, master=0)
        assert all(isinstance(v, _FakeSkip) for v in applied[:13])
    assert spy.calls == 0


def test_the_two_generate_handlers_still_mint_and_surface(monkeypatch):
    """The other half of the split: generating is an action, so it may mint — and must show it."""
    handlers = _c3_handlers()
    minted = 424242

    spy = _CountingRandomSeed(minted)
    monkeypatch.setattr(handlers["fork_variation"], "random_seed", spy)
    master, batch, _t, _s, _rs, _st = _generate_batch(master=0, handlers=handlers)
    assert spy.calls == 1, "Generate Variants mints exactly once"
    assert master == minted, "and returns it to variant_master_seed"
    assert batch.declaration.root_master_seed == minted

    spy = _CountingRandomSeed(minted)
    monkeypatch.setattr(handlers["fork_variation"], "random_seed", spy)
    single = _gui_handlers()["_on_generate_variant"](*_lab_args(master=0))
    assert spy.calls == 1
    assert single[0] == minted, "single Generate also surfaces what it minted"


def test_a_numeric_string_master_is_the_same_declaration_not_a_stale_one():
    """`variation.normalize_seed` accepts `"582913"` as 582913, so the box still declares the same
    root and Apply must succeed. The gate refuses values that are *unusable*, not values that are
    merely typed differently — a stale check that fired on re-typing would be a bug of its own."""
    handlers = _c3_handlers()
    _master, batch, _t, _s, _rs, _st = _generate_batch(master=582913, handlers=handlers)
    applied = _apply(batch, 0, handlers=handlers, master="582913")
    assert not any(isinstance(v, _FakeSkip) for v in applied[:13])
    assert applied[0] == batch.candidates[0].master_seed


def test_the_mint_switch_is_keyword_only_and_apply_passes_false():
    """Structural: the positional `inputs` list must never be able to supply this flag."""
    tree = _tree(_GUI)
    helper = _func(tree, "_build_variant_resolution_context")
    assert [a.arg for a in helper.args.kwonlyargs] == ["mint_unset_master"]
    assert helper.args.args[-1].arg == "sfx_level", "the 30 positional widget args are unchanged"
    default = helper.args.kw_defaults[0]
    assert isinstance(default, ast.Constant) and default.value is True

    body = ast.unparse(_strip_docstrings(helper))
    assert "if master_seed <= 0 and mint_unset_master:" in body
    assert body.count("fork_variation.random_seed()") == 1

    apply_body = ast.unparse(_strip_docstrings(_func(tree, "_on_apply_selected_variant")))
    assert "mint_unset_master=False" in apply_body
    assert "fork_variation" not in apply_body, "Apply owns no randomness of its own"
    assert "random_seed" not in apply_body

    for generator in ("_on_generate_variant", "_on_generate_variants"):
        generated = ast.unparse(_strip_docstrings(_func(tree, generator)))
        assert "mint_unset_master" not in generated, f"{generator} keeps the minting default"


def test_only_the_shared_helper_owns_the_draw():
    """One `random_seed()` call site for the whole lab, plus Randomize and New Variant's own."""
    tree = _tree(_GUI)
    owners = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and "fork_variation.random_seed()" in ast.unparse(_strip_docstrings(node))):
            owners.append(node.name)
    assert sorted(owners) == ["_build_variant_resolution_context",
                              "_fresh_variant_master_seed"], owners
