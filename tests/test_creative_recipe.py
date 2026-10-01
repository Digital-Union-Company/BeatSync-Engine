"""The resolved Creative Recipe: its exact shape, its strict validation, and its one bridge.

A recipe is seven integers. That makes nearly every interesting assertion a *boundary* assertion:

* **The shape** is checked against the preset/slider field order it derives from, so the recipe and
  the sliders cannot drift apart.
* **The validation** is checked for being *all-or-nothing*. This is the contract boundary a future
  untrusted generator (an AI Director) will cross, and the failure mode worth preventing is not a
  crash — it is a half-applied recipe where three fields came from the model and three are silent
  50s, which looks deliberate and is not.
* **The bridge** is checked for being exactly that: `to_profile()` must introduce no semantic
  difference, because the moment it does there are two interpretations of the same seven numbers.

The deliberate contrast with `CreativeProfile.from_mapping` — lenient, per-field, neutral-fallback —
is itself asserted here, because the two having *different* trust contracts is the design.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets

GOOD = {"seed": 381944, "cut_density": 72, "micro_cuts": 41, "semantic_emphasis": 66,
        "energy_response": 78, "motion_bias": 37, "source_diversity": 83}


def _recipe(**overrides) -> dict:
    return {**GOOD, **overrides}


# ===========================================================================
# 1. SHAPE
# ===========================================================================


def test_the_field_tuple_is_the_seed_then_the_six_controls_in_slider_order():
    """Derived from `presets.CREATIVE_CONTROL_FIELDS`, not restated — so a future reordering of the
    sliders cannot leave the recipe describing them in a different order."""
    assert fork_recipe.VARIANT_RECIPE_FIELDS == ("seed",) + fork_presets.CREATIVE_CONTROL_FIELDS
    assert len(fork_recipe.VARIANT_RECIPE_FIELDS) == 7


def test_the_dataclass_fields_are_exactly_the_declared_field_tuple():
    """Also asserted at import time inside the module; repeated here so the failure is a readable
    test rather than an ImportError during collection."""
    assert tuple(f.name for f in dataclasses.fields(fork_recipe.CreativeRecipe)) == \
        fork_recipe.VARIANT_RECIPE_FIELDS


def test_the_seed_bounds_are_the_positive_variation_seed_range():
    assert (fork_recipe.RECIPE_SEED_MIN, fork_recipe.RECIPE_SEED_MAX) == (1, 999_999)


def test_a_recipe_is_frozen():
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    with pytest.raises(dataclasses.FrozenInstanceError):
        recipe.seed = 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        recipe.motion_bias = 2


def test_recipes_compare_by_value():
    assert fork_recipe.CreativeRecipe(**GOOD) == fork_recipe.CreativeRecipe(**GOOD)
    assert fork_recipe.CreativeRecipe(**GOOD) != fork_recipe.CreativeRecipe(**_recipe(seed=1))


# ===========================================================================
# 2. CONSTRUCTION RAISES ON ANYTHING INVALID
# ===========================================================================


def test_a_valid_recipe_constructs():
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    assert recipe.seed == 381944
    assert recipe.motion_bias == 37


@pytest.mark.parametrize("seed", [0, -1, -999, 1_000_000, 10 ** 9, True, False, 1.0, 381944.0,
                                  "381944", None, [], object()])
def test_an_invalid_seed_refuses_to_construct(seed: Any):
    """Seed 0 is refused like every other invalid value, and for a product reason rather than a
    typing one: 0 is the planner's *legacy* branch, so a generated variant carrying it would not be
    a variant at all."""
    with pytest.raises(ValueError):
        fork_recipe.CreativeRecipe(**_recipe(seed=seed))


@pytest.mark.parametrize("field", fork_presets.CREATIVE_CONTROL_FIELDS)
@pytest.mark.parametrize("value", [-1, 101, 1000, True, False, 50.0, 37.5, "50", None, object()])
def test_an_invalid_control_refuses_to_construct(field: str, value: Any):
    """`bool` is refused explicitly because it subclasses `int`; `50.0` is refused because a recipe
    is an exact statement and accepting a float here would make `50.0` and `50` two spellings of
    one recipe that no longer compare equal as mappings."""
    with pytest.raises(ValueError):
        fork_recipe.CreativeRecipe(**_recipe(**{field: value}))


@pytest.mark.parametrize("value", [0, 50, 100])
@pytest.mark.parametrize("field", fork_presets.CREATIVE_CONTROL_FIELDS)
def test_the_control_range_ends_are_inclusive(field: str, value: int):
    assert getattr(fork_recipe.CreativeRecipe(**_recipe(**{field: value})), field) == value


@pytest.mark.parametrize("seed", [1, 2, 500_000, 999_999])
def test_the_seed_range_ends_are_inclusive(seed: int):
    assert fork_recipe.CreativeRecipe(**_recipe(seed=seed)).seed == seed


# ===========================================================================
# 3. from_mapping IS ALL-OR-NOTHING AND NEVER RAISES
# ===========================================================================


def test_a_good_mapping_round_trips():
    recipe = fork_recipe.CreativeRecipe.from_mapping(GOOD)
    assert recipe is not None
    assert recipe.as_mapping() == GOOD


@pytest.mark.parametrize("mapping", [
    None, 0, 1, "recipe", b"recipe", [], (), object(), [("seed", 1)],
])
def test_a_non_mapping_is_rejected_without_raising(mapping: Any):
    assert fork_recipe.CreativeRecipe.from_mapping(mapping) is None


def test_a_missing_field_rejects_the_whole_recipe():
    for field in fork_recipe.VARIANT_RECIPE_FIELDS:
        partial = {k: v for k, v in GOOD.items() if k != field}
        assert fork_recipe.CreativeRecipe.from_mapping(partial) is None, field


def test_an_extra_field_rejects_the_whole_recipe():
    """An unexpected key means the producer and this contract disagree about what a recipe is, and
    that disagreement is exactly what is worth failing on rather than ignoring."""
    for extra in ("master_seed", "spread", "algorithm_version", "preset", "bloom"):
        assert fork_recipe.CreativeRecipe.from_mapping({**GOOD, extra: 1}) is None, extra


@pytest.mark.parametrize("seed", [0, -5, 1_000_000, True, 1.0, "1", None])
def test_an_invalid_seed_rejects_the_whole_recipe(seed: Any):
    assert fork_recipe.CreativeRecipe.from_mapping(_recipe(seed=seed)) is None


@pytest.mark.parametrize("field", fork_presets.CREATIVE_CONTROL_FIELDS)
@pytest.mark.parametrize("value", [-1, 101, True, False, 37.5, 50.0, "50", None])
def test_one_bad_control_rejects_the_whole_recipe(field: str, value: Any):
    """The load-bearing assertion of this module: no partial normalisation, no fallback to 50, no
    clamping. A half-applied recipe is worse than a refused one because it looks intentional."""
    assert fork_recipe.CreativeRecipe.from_mapping(_recipe(**{field: value})) is None


def test_from_mapping_accepts_any_mapping_type():
    from types import MappingProxyType
    assert fork_recipe.CreativeRecipe.from_mapping(MappingProxyType(dict(GOOD))) is not None


def test_the_profile_stays_lenient_and_that_contrast_is_the_design():
    """`CreativeProfile` reads a possibly-stale internal bus and must survive it; `CreativeRecipe`
    guards a contract boundary. Same malformed value, deliberately opposite answers."""
    assert fork_recipe.CreativeRecipe.from_mapping(_recipe(motion_bias=140)) is None
    assert fork_creative.CreativeProfile.from_mapping({"motion_bias": 140}).motion_bias == 100


# ===========================================================================
# 4. as_mapping
# ===========================================================================


def test_as_mapping_is_a_fresh_plain_dict_each_time():
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    first, second = recipe.as_mapping(), recipe.as_mapping()

    assert first == second == GOOD
    assert first is not second
    assert type(first) is dict
    first["seed"] = 1
    assert recipe.seed == 381944
    assert recipe.as_mapping()["seed"] == 381944


def test_as_mapping_keys_are_in_field_order():
    assert tuple(fork_recipe.CreativeRecipe(**GOOD).as_mapping()) == \
        fork_recipe.VARIANT_RECIPE_FIELDS


def test_every_value_is_a_plain_int():
    for value in fork_recipe.CreativeRecipe(**GOOD).as_mapping().values():
        assert type(value) is int


# ===========================================================================
# 5. THE BRIDGE TO EXECUTION
# ===========================================================================


def test_to_profile_is_exactly_the_recipe():
    """No semantic difference — the whole reason a recipe may exist alongside a profile."""
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    assert recipe.to_profile().as_dict() == recipe.as_mapping()


@pytest.mark.parametrize("seed", [1, 7, 381944, 999_999])
@pytest.mark.parametrize("controls", [(0,) * 6, (50,) * 6, (100,) * 6, (30, 25, 65, 40, 30, 50)])
def test_to_profile_round_trips_across_the_whole_space(seed: int, controls: tuple):
    recipe = fork_recipe.CreativeRecipe(
        seed=seed, **dict(zip(fork_presets.CREATIVE_CONTROL_FIELDS, controls)))
    assert recipe.to_profile().as_dict() == recipe.as_mapping()


def test_to_profile_never_produces_a_legacy_render():
    """A positive seed is guaranteed by validation, so a recipe can never resolve to the planner's
    legacy branch — which is what makes "this is a variant" true by construction."""
    for seed in (1, 7, 381944, 999_999):
        profile = fork_recipe.CreativeRecipe(**_recipe(seed=seed)).to_profile()
        assert profile.seed == seed
        assert not profile.is_neutral()
        assert profile.filename_suffix() == f"_seed{seed}"


def test_a_neutral_looking_recipe_is_still_not_legacy():
    """All six controls at 50 but a positive seed: the controls are neutral, the render is not."""
    recipe = fork_recipe.CreativeRecipe(
        seed=1, **dict(zip(fork_presets.CREATIVE_CONTROL_FIELDS, (50,) * 6)))
    profile = recipe.to_profile()
    assert profile.is_neutral_cuts() and profile.is_neutral_scoring()
    assert not profile.is_neutral()
    assert profile.describe() != "legacy"


def test_creative_profile_gained_no_recipe_field():
    """Kept alongside `test_creative_profile.py`'s own guard: `CreativeRecipe` is a separate
    contract object and must never become a field on the execution object."""
    profile = fork_creative.CreativeProfile()
    for field in ("recipe", "creative_recipe", "master_seed", "spread", "algorithm_version",
                  "variant_lab", "ranges", "randomized"):
        assert not hasattr(profile, field), field
        assert field not in profile.as_dict(), field


# ===========================================================================
# 6. WHAT THE RECIPE DELIBERATELY IS NOT
# ===========================================================================


def test_the_recipe_carries_no_generator_provenance():
    """Master seed, spread, ranges, randomized fields and the algorithm version belong to
    `variant_lab.VariantLabResolution`. A future AI Director must be able to emit a recipe without
    inventing a master seed it never had."""
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    for field in ("master_seed", "spread", "ranges", "randomized_fields", "algorithm_version",
                  "config", "provenance"):
        assert not hasattr(recipe, field), field
        assert field not in recipe.as_mapping(), field


def test_the_recipe_carries_no_gui_concept():
    """Which named preset a tuple happens to match is a GUI read-out, not a property of the render."""
    recipe = fork_recipe.CreativeRecipe(**GOOD)
    for field in ("preset", "preset_label", "label", "report"):
        assert not hasattr(recipe, field), field


def test_describe_is_diagnostic_and_mentions_every_resolved_value():
    text = fork_recipe.CreativeRecipe(**GOOD).describe()
    for value in GOOD.values():
        assert str(value) in text
