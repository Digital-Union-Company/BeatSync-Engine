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


def test_no_multi_variant_or_c3_machinery_was_added():
    """C2 is one recipe at a time. Batch generation, comparison and reuse are C3."""
    for path in (_LAB, _RECIPE, _GUI):
        source = _executable_source(path).lower()
        for word in ("generate_variants", "variant_batch", "variant_count", "num_variants",
                     "compare_variants", "variant_gallery", "freestyle", "director",
                     "stage_cache", "shortlist"):
            assert not re.search(rf"\b{re.escape(word)}\b", source), f"{path} mentions {word!r}"


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


def test_the_input_list_is_config_then_the_six_live_sliders():
    names = _assigned_list(_tree(_GUI), "variant_lab_inputs")
    expected = (["variant_master_seed", "variation_spread", "variant_randomize"]
                + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")]
                + ["creative_control_sliders"])
    assert names == expected


def test_the_input_list_matches_both_handlers_parameter_order():
    """Gradio passes `inputs` positionally, so the list and the signatures are one contract."""
    tree = _tree(_GUI)
    parameters = [a.arg for a in _func(tree, "_on_generate_variant").args.args]
    expected = (["variant_master_seed", "variation_spread", "variant_randomize"]
                + [f"range_{f}_{e}" for f in FIELDS for e in ("min", "max")]
                + list(FIELDS))
    assert parameters == expected

    new_variant = _func(tree, "_on_new_variant")
    assert [a.arg for a in new_variant.args.args] == ["variant_master_seed"]
    assert new_variant.args.vararg is not None


def test_the_output_list_is_exactly_what_variant_lab_may_write():
    names = _assigned_list(_tree(_GUI), "variant_lab_outputs")
    assert names == ["variant_master_seed", "variation_seed", "creative_control_sliders",
                     "creative_preset", "variant_report"]


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
    """One resolver call site: a second implementation is how the two buttons would drift apart."""
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_on_new_variant")))
    assert "_on_generate_variant(" in body
    assert "fork_variation.random_seed()" in body
    assert "fork_lab.resolve" not in body


def test_the_generate_handler_mints_a_visible_master_seed_when_unset():
    body = ast.unparse(_strip_docstrings(_func(_tree(_GUI), "_on_generate_variant")))
    assert "fork_lab.normalize_master_seed(variant_master_seed)" in body
    assert "fork_variation.random_seed()" in body
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

_HANDLER_NAMES = ("_variant_apply_outputs", "_on_generate_variant", "_on_new_variant")


def _gui_handlers():
    from beatsync_fork import variation as fork_variation
    from typing import Tuple

    tree = _tree(_GUI)
    wanted = {name: None for name in _HANDLER_NAMES}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            wanted[node.name] = node
    missing = [name for name, node in wanted.items() if node is None]
    assert not missing, f"handlers not found at module level: {missing}"

    namespace = {"fork_presets": fork_presets, "fork_lab": fork_lab,
                 "fork_variation": fork_variation, "Tuple": Tuple}
    exec(compile(ast.Module(body=list(wanted.values()), type_ignores=[]),
                 filename=_GUI, mode="exec"), namespace)
    return namespace


def _generate(master, base, spread=50, randomized=None, ranges=None):
    handlers = _gui_handlers()
    randomized = list(FIELDS) if randomized is None else randomized
    ranges = {f: (0, 100) for f in FIELDS} if ranges is None else ranges
    flat_ranges = [value for f in FIELDS for value in ranges[f]]
    return handlers["_on_generate_variant"](
        master, spread, randomized, *flat_ranges, *[base[f] for f in FIELDS])


def test_the_real_handler_produces_the_exact_expected_outputs():
    """§47, pinned end to end: master + config + base -> the exact widget values written."""
    outputs = _generate(582913, CINEMATIC, spread=50)

    assert outputs[0] == 582913                       # master seed, echoed back visibly
    assert outputs[1] == 822019                       # resolved clip Variation Seed
    assert outputs[2:8] == (55, 15, 60, 63, 22, 62)   # the six sliders, in field order
    assert outputs[8] == "Custom"                     # preset label, computed explicitly
    assert "algorithm v1" in outputs[9]
    assert "Master seed 582913" in outputs[9]
    assert "6 randomized / 0 fixed" in outputs[9]
    assert len(outputs) == 10


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


def test_new_variant_always_mints_a_fresh_visible_master():
    handlers = _gui_handlers()
    flat_ranges = [value for _ in FIELDS for value in (0, 100)]
    seen = set()
    for _ in range(12):
        outputs = handlers["_on_new_variant"](
            582913, 50, list(FIELDS), *flat_ranges, *[BALANCED[f] for f in FIELDS])
        assert outputs[0] != 582913, "New Variant must discard the incoming master seed"
        assert 1 <= outputs[0] <= 999_999
        # and the minted master reproduces its own recipe
        assert _generate(outputs[0], BALANCED) == outputs
        seen.add(outputs[0])
    assert len(seen) > 1, "New Variant produced the same master every time"


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
