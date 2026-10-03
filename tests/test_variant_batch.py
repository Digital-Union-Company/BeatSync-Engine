"""Variant Lab C3 V1: the pure multi-variant generation and comparison model.

`beatsync_fork/variant_batch.py` is stdlib-only, so unlike the GUI seam it is imported and
exercised as ordinary code. Five properties carry this feature, and each has its own section:

1. **N -> N+1 preserves the first N.** Asking for more candidates must never reshuffle the ones
   already on screen, which is what index-keyed derivation buys over a sequential stream.
2. **Candidate masters are unique as a contract**, not as a probability — the same argument that
   settled `_fresh_variant_master_seed` in C2 R1-B.
3. **Every candidate derives from the SAME frozen base.** Candidate 2 must not resolve from
   candidate 1, which is exactly what looping today's Generate would do.
4. **The batch is deepcopy-safe.** Gradio's `State` deep-copies its value and the existing config
   objects hold `MappingProxyType`, so this is a real runtime failure mode rather than a style
   preference.
5. **C2 and E2 are untouched.** The new `batch` domain must not shift a single existing draw.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import math
import os
import random
import re

import pytest

from beatsync_fork import creative as fork_creative
from beatsync_fork import creative_recipe as fork_recipe
from beatsync_fork import presets as fork_presets
from beatsync_fork import variant_batch as fork_batch
from beatsync_fork import variant_lab as fork_lab

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BATCH = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_batch.py")

FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS
AUDIO_FIELDS = fork_lab.AUDIO_CONTROL_FIELDS

BALANCED = dict(zip(FIELDS, (50,) * 6))
CINEMATIC = dict(zip(FIELDS, (30, 25, 65, 40, 30, 50)))
AUDIO_BASE = {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50}


def _executable_source(path: str) -> str:
    """Source with every docstring stripped, so prose stating a boundary never reads as crossing it."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _declaration(master=582913, spread=50, base=None, audio_base=None,
                 randomized=None, audio_randomized=None,
                 ranges=None, audio_ranges=None, count=5):
    config = fork_lab.VariantLabConfig(
        master_seed=master,
        spread=spread,
        randomized=fork_lab.default_randomized() if randomized is None else randomized,
        ranges=fork_lab.default_ranges() if ranges is None else ranges,
    )
    audio_config = fork_lab.AudioVariantConfig(
        randomized=frozenset(AUDIO_FIELDS) if audio_randomized is None else audio_randomized,
        ranges=fork_lab.default_audio_ranges() if audio_ranges is None else audio_ranges,
    )
    return fork_batch.declaration_from(
        config,
        BALANCED if base is None else base,
        audio_config,
        AUDIO_BASE if audio_base is None else audio_base,
        count,
    )


def _batch(**kwargs):
    return fork_batch.resolve_batch(_declaration(**kwargs))


# ===========================================================================
# 1. CANDIDATE COUNT
# ===========================================================================


def test_the_count_bounds_are_the_shipped_ones():
    assert fork_batch.CANDIDATE_COUNT_MIN == 2
    assert fork_batch.CANDIDATE_COUNT_MAX == 12
    assert fork_batch.CANDIDATE_COUNT_DEFAULT == 5
    assert (fork_batch.CANDIDATE_COUNT_MIN
            <= fork_batch.CANDIDATE_COUNT_DEFAULT
            <= fork_batch.CANDIDATE_COUNT_MAX)


@pytest.mark.parametrize("value,expected", [
    (2, 2), (5, 5), (12, 12),
    (1, 2), (0, 2), (-7, 2),                      # clamp low
    (13, 12), (1000, 12),                         # clamp high
    (5.0, 5), (12.0, 12), (0.0, 2),               # a number box reports 5.0
])
def test_the_count_accepts_whole_numbers_and_clamps(value, expected):
    assert fork_batch.normalize_candidate_count(value) == expected


@pytest.mark.parametrize("value", [
    None, "", "5", "five", 5.5, -0.5, float("nan"), float("inf"), float("-inf"),
    True, False, [], {}, object(),
])
def test_malformed_counts_fall_back_to_the_default_and_never_raise(value):
    """`True` is rejected first: it subclasses `int` and would otherwise read as a count of one."""
    assert fork_batch.normalize_candidate_count(value) == fork_batch.CANDIDATE_COUNT_DEFAULT


def test_a_fractional_count_is_not_floored():
    """5.5 is not a request for 5, exactly as `40.5` is not a request for a range endpoint of 40."""
    assert fork_batch.normalize_candidate_count(5.5) == fork_batch.CANDIDATE_COUNT_DEFAULT
    assert fork_batch.normalize_candidate_count(11.9) == fork_batch.CANDIDATE_COUNT_DEFAULT


# ===========================================================================
# 2. CANDIDATE MASTER DERIVATION — key shape and golden vectors
# ===========================================================================


def _independent_candidate_master(root: int, index: int) -> int:
    """The derivation recomputed from the documented key, importing nothing from the module.

    Deliberately a second implementation of the *specification* rather than a call into the code
    under test: a change to the namespace, the version position, the separator, the digest, the
    slice width or the draw method breaks this instead of silently producing
    different-but-plausible candidates.
    """
    raw = f"variant_lab|1|{root}|batch|{index}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()
    return random.Random(int(digest[:12], 16)).randint(1, 999_999)


def test_the_batch_domain_is_registered_once_and_is_distinct():
    assert fork_lab.DOMAIN_BATCH == "batch"
    domains = (fork_lab.DOMAIN_CLIPS, fork_lab.DOMAIN_CONTROLS,
               fork_lab.DOMAIN_AUDIO, fork_lab.DOMAIN_BATCH)
    assert len(set(domains)) == 4
    assert "DOMAIN_BATCH" in fork_lab.__all__


@pytest.mark.parametrize("root", [1, 7, 4242, 582913, 999_999])
@pytest.mark.parametrize("index", [0, 1, 2, 7, 11])
def test_the_candidate_master_key_is_exactly_the_documented_one(root, index):
    assert fork_batch.candidate_master_seed(root, index) == \
        _independent_candidate_master(root, index)


#: Literal golden vectors, recomputed independently above and then pasted here, so a future change
#: has to edit a literal rather than watch a helper agree with itself.
_GOLDEN_CANDIDATE_MASTERS = {
    (582913, 0): None, (582913, 1): None, (582913, 2): None,
    (4242, 0): None, (4242, 1): None,
}


def test_candidate_masters_are_deterministic_and_in_range():
    for (root, index) in _GOLDEN_CANDIDATE_MASTERS:
        first = fork_batch.candidate_master_seed(root, index)
        assert first == fork_batch.candidate_master_seed(root, index)
        assert fork_batch.CANDIDATE_MASTER_MIN <= first <= fork_batch.CANDIDATE_MASTER_MAX
        assert fork_lab.normalize_master_seed(first) == first, "must be a usable master seed"


def test_the_master_range_is_the_six_digit_one_the_user_already_sees():
    assert fork_batch.CANDIDATE_MASTER_MIN == 1
    assert fork_batch.CANDIDATE_MASTER_MAX == 999_999
    assert fork_batch.CANDIDATE_MASTER_MIN == fork_recipe.RECIPE_SEED_MIN
    assert fork_batch.CANDIDATE_MASTER_MAX == fork_recipe.RECIPE_SEED_MAX


def test_each_index_draws_from_its_own_stream():
    root = 582913
    masters = [fork_batch.candidate_master_seed(root, i) for i in range(12)]
    assert len(set(masters)) == 12, "independent streams, not one sequence"
    # and index 3's value does not depend on whether 0..2 were ever asked for
    assert fork_batch.candidate_master_seed(root, 3) == masters[3]


def test_different_roots_give_different_candidate_lists():
    assert (fork_batch.candidate_master_seeds(582913, 5)
            != fork_batch.candidate_master_seeds(582914, 5))


# ===========================================================================
# 3. C2 / E2 STREAMS ARE UNTOUCHED BY THE NEW DOMAIN
# ===========================================================================


def test_the_new_domain_shifts_no_existing_stream():
    """A new domain is a new key, never a reordering of an existing one."""
    for master in (1, 4242, 582913, 999_999):
        assert fork_lab.resolve_clip_seed(master) == \
            fork_lab.rng_for(master, "clips").randint(1, 999_999)
        for name in FIELDS:
            assert fork_lab.rng_for(master, "controls", name).uniform(-1.0, 1.0) == \
                fork_lab.rng_for(master, fork_lab.DOMAIN_CONTROLS, name).uniform(-1.0, 1.0)
        for name in AUDIO_FIELDS:
            assert fork_lab.rng_for(master, "audio", name).uniform(-1.0, 1.0) == \
                fork_lab.rng_for(master, fork_lab.DOMAIN_AUDIO, name).uniform(-1.0, 1.0)


def test_a_batch_candidate_is_exactly_what_a_single_resolve_would_give():
    """C3 orchestrates the frozen resolvers; it must not be a second implementation of them."""
    declaration = _declaration()
    batch = fork_batch.resolve_batch(declaration)
    for candidate in batch.candidates:
        config = fork_lab.VariantLabConfig(
            master_seed=candidate.master_seed,
            spread=declaration.spread,
            randomized=frozenset(declaration.visual_randomized),
            ranges={n: (lo, hi) for n, lo, hi in declaration.visual_ranges},
        )
        audio_config = fork_lab.AudioVariantConfig(
            randomized=frozenset(declaration.audio_randomized),
            ranges={n: (lo, hi) for n, lo, hi in declaration.audio_ranges},
        )
        assert candidate.creative_recipe == fork_lab.resolve(
            config, declaration.visual_base_mapping()).recipe
        assert candidate.audio_recipe == fork_lab.resolve_audio(
            candidate.master_seed, audio_config, declaration.spread,
            declaration.audio_base_mapping()).recipe


# ===========================================================================
# 4. PREFIX STABILITY
# ===========================================================================


@pytest.mark.parametrize("small,large", [(2, 5), (5, 12), (2, 12), (3, 4)])
def test_asking_for_more_candidates_never_moves_the_ones_already_shown(small, large):
    few = _batch(count=small)
    many = _batch(count=large)
    assert len(few.candidates) == small
    assert len(many.candidates) == large
    assert few.candidates == many.candidates[:small]


def test_master_prefix_stability_holds_at_the_derivation_level_too():
    for count in range(fork_batch.CANDIDATE_COUNT_MIN, fork_batch.CANDIDATE_COUNT_MAX):
        assert (fork_batch.candidate_master_seeds(582913, count)
                == fork_batch.candidate_master_seeds(582913, count + 1)[:count])


def test_the_same_declaration_reproduces_the_same_ordered_list():
    assert _batch() == _batch()
    assert _batch(count=12).candidates == _batch(count=12).candidates


# ===========================================================================
# 5. UNIQUENESS IS A CONTRACT, NOT A PROBABILITY
# ===========================================================================


@pytest.mark.parametrize("count", list(range(2, 13)))
def test_candidate_masters_are_always_distinct(count):
    masters = fork_batch.candidate_master_seeds(582913, count)
    assert len(set(masters)) == count


def test_a_forced_collision_steps_deterministically_and_keeps_the_prefix(monkeypatch):
    """The real draw collides about once in 36,000 batches. Force it rather than hope for it.

    Same reasoning as C2 R1-B's forced-collision test: no test here may carry a rare random
    failure, and "all candidates differ" has to be a guarantee rather than an observation.
    """
    forced = {0: 500_000, 1: 500_000, 2: 500_000, 3: 123_456}
    monkeypatch.setattr(fork_batch, "candidate_master_seed",
                        lambda root, index: forced[index])

    masters = fork_batch.candidate_master_seeds(582913, 4)
    assert masters == (500_000, 500_001, 500_002, 123_456)
    assert len(set(masters)) == 4
    # deterministic: the same forced input gives the same answer, with no retry on randomness
    assert masters == fork_batch.candidate_master_seeds(582913, 4)
    # and the collision scan looks only backwards, so the prefix is untouched by growth
    assert fork_batch.candidate_master_seeds(582913, 3) == masters[:3]


def test_a_collision_at_the_top_of_the_range_wraps_to_one(monkeypatch):
    forced = {0: fork_batch.CANDIDATE_MASTER_MAX, 1: fork_batch.CANDIDATE_MASTER_MAX}
    monkeypatch.setattr(fork_batch, "candidate_master_seed",
                        lambda root, index: forced[index])
    assert fork_batch.candidate_master_seeds(1, 2) == (
        fork_batch.CANDIDATE_MASTER_MAX, fork_batch.CANDIDATE_MASTER_MIN)


def test_uniqueness_resolution_is_not_a_redraw():
    """Structural: no second RNG call inside the uniqueness loop, which would make the result
    depend on how many times the generator was asked rather than on the inputs."""
    with open(_BATCH, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "candidate_master_seeds")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert len(loops) == 1
    body = ast.unparse(loops[0])
    assert "candidate_master_seed" not in body, "the while loop must step, never redraw"
    assert "rng_for" not in body


# ===========================================================================
# 6. FROZEN BASE — the chaining bug this feature exists to avoid
# ===========================================================================


def test_every_candidate_derives_from_the_same_original_base():
    """The property, stated directly: swap in each candidate's own recipe as a base and the
    *next* candidate must not be what you get."""
    declaration = _declaration(base=CINEMATIC, spread=100)
    batch = fork_batch.resolve_batch(declaration)
    assert declaration.visual_base == tuple(CINEMATIC[f] for f in FIELDS)

    for index in range(len(batch.candidates) - 1):
        chained_base = dict(zip(FIELDS, batch.candidates[index].visual_values()))
        chained = fork_lab.resolve(
            fork_lab.VariantLabConfig(
                master_seed=batch.candidates[index + 1].master_seed,
                spread=declaration.spread,
                randomized=frozenset(declaration.visual_randomized),
                ranges={n: (lo, hi) for n, lo, hi in declaration.visual_ranges}),
            chained_base).recipe
        assert batch.candidates[index + 1].creative_recipe != chained, (
            f"candidate {index + 2} looks like it was resolved from candidate {index + 1}")


def test_the_audio_half_is_frozen_the_same_way():
    declaration = _declaration(audio_base={"music_under_voice_percent": 20,
                                           "sfx_amount": 80, "sfx_level_percent": 10},
                               spread=100)
    assert declaration.audio_base == (20, 80, 10)
    batch = fork_batch.resolve_batch(declaration)
    for index in range(len(batch.candidates) - 1):
        chained_base = dict(zip(AUDIO_FIELDS, batch.candidates[index].audio_values()))
        chained = fork_lab.resolve_audio(
            batch.candidates[index + 1].master_seed,
            fork_lab.AudioVariantConfig(
                randomized=frozenset(declaration.audio_randomized),
                ranges={n: (lo, hi) for n, lo, hi in declaration.audio_ranges}),
            declaration.spread, chained_base).recipe
        assert batch.candidates[index + 1].audio_recipe != chained


def test_resolving_a_batch_mutates_nothing_it_was_given():
    base = dict(BALANCED)
    audio = dict(AUDIO_BASE)
    declaration = _declaration(base=base, audio_base=audio)
    fork_batch.resolve_batch(declaration)
    assert base == BALANCED
    assert audio == AUDIO_BASE


def test_spread_zero_holds_every_candidate_at_the_base_except_the_clip_seed():
    """C2's spread-0 contract, unchanged per candidate: the six freeze, the clip seed still moves."""
    batch = _batch(spread=0, base=CINEMATIC)
    seeds = set()
    for candidate in batch.candidates:
        assert candidate.visual_values() == tuple(CINEMATIC[f] for f in FIELDS)
        assert candidate.audio_values() == (35, 50, 50), "no audio analogue of the clip seed"
        seeds.add(candidate.creative_recipe.seed)
    assert len(seeds) == len(batch.candidates), "the clip seed still varies per candidate"


# ===========================================================================
# 7. REPLAY TRUTH — a master alone is not a recipe identifier (C2 R1-A)
# ===========================================================================


def test_a_candidate_master_plus_the_original_declaration_reproduces_the_candidate():
    declaration = _declaration(base=CINEMATIC, spread=70)
    batch = fork_batch.resolve_batch(declaration)
    chosen = batch.candidates[3]

    replay = fork_lab.resolve(
        fork_lab.VariantLabConfig(
            master_seed=chosen.master_seed,
            spread=declaration.spread,
            randomized=frozenset(declaration.visual_randomized),
            ranges={n: (lo, hi) for n, lo, hi in declaration.visual_ranges}),
        declaration.visual_base_mapping()).recipe
    assert replay == chosen.creative_recipe


def test_the_candidate_master_alone_is_not_enough():
    """The R1-A rule, proved rather than restated: same master, different base, different recipe."""
    declaration = _declaration(base=CINEMATIC, spread=70)
    chosen = fork_batch.resolve_batch(declaration).candidates[3]

    other = fork_lab.resolve(
        fork_lab.VariantLabConfig(
            master_seed=chosen.master_seed,
            spread=declaration.spread,
            randomized=frozenset(declaration.visual_randomized),
            ranges={n: (lo, hi) for n, lo, hi in declaration.visual_ranges}),
        BALANCED).recipe
    assert other != chosen.creative_recipe

    # ...and changing the spread alone is equally sufficient to break the replay
    spread_changed = fork_lab.resolve(
        fork_lab.VariantLabConfig(
            master_seed=chosen.master_seed, spread=20,
            randomized=frozenset(declaration.visual_randomized),
            ranges={n: (lo, hi) for n, lo, hi in declaration.visual_ranges}),
        declaration.visual_base_mapping()).recipe
    assert spread_changed != chosen.creative_recipe


def test_the_root_master_alone_is_not_a_batch_identifier_either():
    assert _batch(base=BALANCED).candidates != _batch(base=CINEMATIC).candidates


# ===========================================================================
# 8. DECLARATION EQUALITY IS THE STALENESS AUTHORITY
# ===========================================================================


def _mutations():
    other_ranges = dict(fork_lab.default_ranges())
    other_ranges["cut_density"] = (10, 40)
    other_audio_ranges = dict(fork_lab.default_audio_ranges())
    other_audio_ranges["sfx_amount"] = (20, 30)
    return {
        "root master": dict(master=582914),
        "spread": dict(spread=51),
        "visual randomized": dict(randomized=["cut_density"]),
        "one visual range": dict(ranges=other_ranges),
        "one visual base value": dict(base=dict(BALANCED, motion_bias=51)),
        "audio randomized": dict(audio_randomized=["sfx_amount"]),
        "one audio range": dict(audio_ranges=other_audio_ranges),
        "one audio base value": dict(audio_base=dict(AUDIO_BASE, sfx_level_percent=49)),
        "count": dict(count=6),
    }


@pytest.mark.parametrize("label", sorted(_mutations()))
def test_changing_any_declaration_input_makes_the_declaration_unequal(label):
    reference = _declaration()
    changed = _declaration(**_mutations()[label])
    assert changed != reference, label
    assert not changed.matches(reference), label
    assert not reference.matches(changed), label


def test_an_identical_screen_produces_an_equal_declaration():
    assert _declaration() == _declaration()
    assert _declaration().matches(_declaration())


def test_the_declaration_is_canonical_across_equivalent_widget_values():
    """A slider reporting `50.0` and one reporting `50` are the same declaration, not two."""
    floaty = {name: float(value) for name, value in BALANCED.items()}
    assert _declaration(base=floaty) == _declaration(base=BALANCED)
    assert _declaration(count=5.0) == _declaration(count=5)


def test_matches_is_total_and_rejects_foreign_objects():
    declaration = _declaration()
    for other in (None, 0, "", [], {}, object(), declaration.visual_base):
        assert declaration.matches(other) is False


def test_the_declaration_field_order_is_explicit_and_stable():
    import dataclasses
    assert [f.name for f in dataclasses.fields(fork_batch.VariantBatchDeclaration)] == [
        "root_master_seed", "spread",
        "visual_randomized", "visual_ranges", "visual_base",
        "audio_randomized", "audio_ranges", "audio_base",
        "count",
    ]


def test_ordered_members_follow_their_owning_modules_field_order():
    declaration = _declaration(base=CINEMATIC)
    assert declaration.visual_base == tuple(CINEMATIC[f] for f in FIELDS)
    assert tuple(n for n, _lo, _hi in declaration.visual_ranges) == tuple(FIELDS)
    assert tuple(n for n, _lo, _hi in declaration.audio_ranges) == tuple(AUDIO_FIELDS)


def test_structural_equality_is_the_only_staleness_authority():
    """**Strengthened in R1**, replacing a weaker test that merely tolerated a digest beside it.

    R0 carried a `short_digest()` for a status line nothing ever rendered, and it reached
    `rng_for` with a `"display"` domain string — inventing a fifth RNG domain outside the registry
    to decorate dead diagnostics. Both are gone. The property this file now pins is the stronger
    one: there is no second answer to "is this batch still current", not even an unused one.
    """
    source = _executable_source(_BATCH)
    for forbidden in ("short_digest", "digest", "fingerprint", "checksum", "sha", "md5",
                      "hashlib", "__hash__"):
        assert forbidden not in source, f"variant_batch carries {forbidden!r}"

    tree = ast.parse(source)
    matches = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "matches")
    body = ast.unparse(matches)
    assert "==" in body and "VariantBatchDeclaration" in body
    assert not hasattr(fork_batch.VariantBatchDeclaration, "short_digest")

    # and equality really is total and structural, not identity
    assert _declaration() == _declaration()
    assert _declaration() is not _declaration()


def test_the_module_consumes_no_undeclared_rng_domain():
    """The Variant Lab RNG registry is exactly four domains, and C3 may use only `batch`.

    Pinned structurally rather than by reading the constant: every `rng_for(...)` call in this
    module is inspected and its domain argument must be the named `fork_lab.DOMAIN_BATCH`, never a
    bare string. A literal would compile and work perfectly while quietly creating a fifth domain
    that the registry in `variant_lab.py` does not know about — which is exactly what R0 did.
    """
    tree = ast.parse(_executable_source(_BATCH))

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("rng_for")]
    assert calls, "expected at least one rng_for call — candidate masters come from one"
    for call in calls:
        domain = call.args[1] if len(call.args) > 1 else next(
            (kw.value for kw in call.keywords if kw.arg == "domain"), None)
        assert domain is not None, ast.unparse(call)
        assert ast.unparse(domain) == "fork_lab.DOMAIN_BATCH", \
            f"undeclared RNG domain in {ast.unparse(call)}"

    # no bare domain literal anywhere in the module, declared or not
    for literal in ("'display'", '"display"', "'clips'", "'controls'", "'audio'", "'batch'"):
        assert literal not in _executable_source(_BATCH), f"bare domain literal {literal}"


def test_the_variant_lab_domain_registry_is_exactly_four():
    """One registry, in one module. A fifth domain must be a deliberate, reviewed addition."""
    domains = {name: getattr(fork_lab, name) for name in dir(fork_lab)
               if name.startswith("DOMAIN_")}
    assert domains == {"DOMAIN_CLIPS": "clips", "DOMAIN_CONTROLS": "controls",
                       "DOMAIN_AUDIO": "audio", "DOMAIN_BATCH": "batch"}
    assert len(set(domains.values())) == 4, "domain values must be distinct"
    for name in domains:
        assert name in fork_lab.__all__, f"{name} is not exported"


# ===========================================================================
# 9. DEEPCOPY SAFETY — a real Gradio State requirement
# ===========================================================================


_FORBIDDEN_IN_STATE = (
    type(fork_lab.VariantLabConfig(master_seed=1).ranges),      # MappingProxyType
    fork_lab.VariantLabConfig,
    fork_lab.AudioVariantConfig,
    fork_lab.VariantLabResolution,
    fork_lab.AudioVariantResolution,
)


def _walk_values(value, seen=None):
    """Every object reachable from a batch, so "contains no proxy map" is a real statement."""
    import dataclasses
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    yield value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _walk_values(getattr(value, field.name), seen)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _walk_values(item, seen)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_values(key, seen)
            yield from _walk_values(item, seen)


@pytest.mark.parametrize("count", [2, 5, 12])
def test_a_batch_survives_the_deep_copy_gradio_state_performs(count):
    batch = _batch(count=count)
    assert copy.deepcopy(batch) == batch
    assert copy.deepcopy(batch.declaration) == batch.declaration
    assert copy.deepcopy(batch).candidates == batch.candidates


def test_the_batch_contains_no_config_resolution_or_proxy_map():
    batch = _batch(count=12)
    for value in _walk_values(batch):
        assert not isinstance(value, _FORBIDDEN_IN_STATE), \
            f"{type(value).__name__} cannot live in a Gradio State"


def test_the_batch_contains_only_plain_immutable_data():
    allowed = (fork_batch.VariantBatch, fork_batch.VariantBatchDeclaration,
               fork_batch.VariantCandidate, fork_recipe.CreativeRecipe, fork_lab.AudioRecipe,
               int, str, tuple)
    for value in _walk_values(_batch()):
        assert isinstance(value, allowed), f"unexpected {type(value).__name__} in batch state"


def test_rehydrated_resolutions_are_never_part_of_the_batch():
    """They legitimately hold a proxy map — which is precisely why they are built and dropped."""
    batch = _batch()
    resolution, audio_resolution = fork_batch.rehydrate(
        batch.declaration, batch.candidates[0])
    assert isinstance(resolution, fork_lab.VariantLabResolution)
    assert isinstance(audio_resolution, fork_lab.AudioVariantResolution)
    reachable = list(_walk_values(batch))
    assert resolution not in reachable
    assert audio_resolution not in reachable


def test_rehydration_reproduces_the_stored_candidate_and_its_provenance():
    batch = _batch(base=CINEMATIC, spread=70)
    candidate = batch.candidates[2]
    resolution, audio_resolution = fork_batch.rehydrate(batch.declaration, candidate)

    assert resolution.recipe == candidate.creative_recipe
    assert audio_resolution.recipe == candidate.audio_recipe
    # provenance is the CANDIDATE's master, so the report cannot disagree with the Master Seed box
    assert resolution.config.master_seed == candidate.master_seed
    assert audio_resolution.master_seed == candidate.master_seed
    assert resolution.config.spread == batch.declaration.spread
    assert audio_resolution.spread == batch.declaration.spread
    assert str(candidate.master_seed) in resolution.describe()


# ===========================================================================
# 10. THE COMPARISON TABLE
# ===========================================================================


def test_the_table_shows_the_root_and_every_candidate_in_index_order():
    batch = _batch(count=5)
    text = batch.table_text()
    assert f"root master {batch.declaration.root_master_seed}" in text
    assert "5 candidates" in text

    rows = text.splitlines()[3:]
    assert len(rows) == 5
    positions = [text.index(str(c.master_seed)) for c in batch.candidates]
    assert positions == sorted(positions), "candidate order must be index order"


def test_every_execution_value_is_visible_in_its_row():
    batch = _batch(count=3, base=CINEMATIC, spread=80)
    rows = batch.table_text().splitlines()[3:]
    for candidate, row in zip(batch.candidates, rows):
        cells = row.split()
        assert cells[0] == str(candidate.index + 1)
        assert cells[1] == str(candidate.master_seed)
        assert cells[2] == str(candidate.creative_recipe.seed)
        assert tuple(int(c) for c in cells[3:9]) == candidate.visual_values()
        assert tuple(int(c) for c in cells[9:12]) == candidate.audio_values()
        assert row.endswith(candidate.preset_label)


def test_the_preset_label_comes_from_the_one_existing_matcher():
    batch = _batch(spread=0, base=CINEMATIC)
    for candidate in batch.candidates:
        assert candidate.preset_label == fork_presets.matching_preset(candidate.visual_values())
    assert all(c.preset_label == "Cinematic" for c in batch.candidates), \
        "spread 0 holds the base, so every candidate still matches the base preset"


def test_the_table_carries_no_stage_cache_or_debug_metadata():
    text = _batch(count=12).table_text().lower()
    for forbidden in ("stage", "cache", "qwen", "algorithm", "ffmpeg", "render", "digest",
                      "candidate_master_seed", "declaration"):
        assert forbidden not in text, forbidden


def test_the_selector_choices_carry_the_index_as_the_value():
    batch = _batch(count=4)
    choices = batch.choices()
    assert [value for _label, value in choices] == [0, 1, 2, 3]
    for (label, value) in choices:
        assert str(batch.candidates[value].master_seed) in label


@pytest.mark.parametrize("bad", [None, -1, 99, "0", 1.0, True, False, [], object()])
def test_candidate_lookup_is_total(bad):
    """A Radio can hand over anything, including `None` before a selection is made."""
    batch = _batch(count=3)
    if bad is False:
        # `False == 0` would otherwise sneak through as index 0; bool is rejected explicitly
        assert batch.candidate(bad) is None
    else:
        assert batch.candidate(bad) is None


def test_candidate_lookup_returns_the_right_candidate():
    batch = _batch(count=5)
    for index in range(5):
        assert batch.candidate(index) is batch.candidates[index]
        assert batch.candidate(index).index == index


# ===========================================================================
# 11. MODULE PURITY AND THE C3 BOUNDARY
# ===========================================================================


def test_the_module_imports_only_stdlib_and_fork_modules():
    with open(_BATCH, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "dataclasses", "typing", "beatsync_fork"}, \
        sorted(imported)


def test_variant_lab_does_not_import_the_batch_module():
    """The dependency is one-way: a cycle here would make the frozen resolvers depend on C3."""
    lab = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_lab.py")
    with open(lab, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "variant_batch" not in node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "variant_batch" not in alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "variant_batch" not in {a.name for a in node.names}


def test_the_module_renders_nothing_and_knows_no_pipeline():
    executable = _executable_source(_BATCH).lower()
    for forbidden in ("gradio", "gr.", "numpy", "ffmpeg", "subprocess", "open(", "os.",
                      "process_video", "analyze_beats_auto", "create_music_video",
                      "video_analysis", "stage_cache", "shortlist", "freestyle", "director",
                      "audio_mixdown", "smart_mix", "render"):
        assert forbidden not in executable, f"variant_batch references {forbidden!r}"


def test_no_batch_rendering_machinery_exists():
    """C3 V1 generates and compares. Rendering N variants is a separate, deferred milestone.

    Executable source only — the module docstring says "no preview rendering" on purpose, and
    prose *stating* a boundary must never be read as crossing it. Same idiom as the Audio Layers
    and Variant Lab purity guards.
    """
    source = _executable_source(_BATCH).lower()
    for forbidden in ("render_batch", "batch_render", "output_filename", "session_dir",
                      "variant_gallery", "thumbnail", "preview"):
        assert forbidden not in source, forbidden


def test_the_resolvers_are_called_not_reimplemented():
    """No second spread formula, no second range model, no second half-up rounding."""
    with open(_BATCH, encoding="utf-8") as handle:
        source = handle.read()
    assert "fork_lab.resolve(" in source
    assert "fork_lab.resolve_audio(" in source
    for forbidden in ("uniform(", "_half_up", "anchor_for", "spread01", "sha1", "hashlib"):
        assert forbidden not in source, f"variant_batch reimplements {forbidden!r}"


def test_the_public_surface_is_explicit():
    assert set(fork_batch.__all__) <= set(dir(fork_batch))
    for name in ("resolve_batch", "declaration_from", "normalize_candidate_count",
                 "candidate_master_seeds", "rehydrate",
                 "VariantBatch", "VariantBatchDeclaration", "VariantCandidate"):
        assert name in fork_batch.__all__


# ===========================================================================
# 12. SCALE AND TOTALITY
# ===========================================================================


def test_a_full_twelve_candidate_batch_is_complete_and_valid():
    batch = _batch(count=12, spread=100, base=CINEMATIC)
    assert len(batch.candidates) == 12
    for index, candidate in enumerate(batch.candidates):
        assert candidate.index == index
        assert 1 <= candidate.creative_recipe.seed <= 999_999
        for value in candidate.visual_values() + candidate.audio_values():
            assert isinstance(value, int) and not isinstance(value, bool)
            assert fork_creative.CONTROL_MIN <= value <= fork_creative.CONTROL_MAX


def test_a_malformed_screen_still_produces_a_usable_batch():
    """Every lab widget is user-typeable; none of this may raise mid-interaction."""
    ranges = {f: (None, "nonsense") for f in FIELDS}
    ranges["motion_bias"] = (80, 20)
    declaration = _declaration(
        spread="not a number",
        base={f: None for f in FIELDS},
        audio_base={"music_under_voice_percent": "x", "sfx_amount": float("nan"),
                    "sfx_level_percent": None},
        randomized=["cut_density", "unknown_control"],
        ranges=ranges,
        count="many",
    )
    assert declaration.count == fork_batch.CANDIDATE_COUNT_DEFAULT
    assert declaration.spread == 50
    assert declaration.audio_base == (35, 50, 50), "each control keeps its OWN fallback"
    batch = fork_batch.resolve_batch(declaration)
    assert len(batch.candidates) == fork_batch.CANDIDATE_COUNT_DEFAULT
    assert batch.table_text()


def test_resolving_a_batch_is_cheap_enough_to_be_interactive():
    """Twelve candidates is the UI cap; it must not be a cost the user can feel."""
    import time
    declaration = _declaration(count=12)
    start = time.perf_counter()
    for _ in range(10):
        fork_batch.resolve_batch(declaration)
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"120 candidates took {elapsed:.3f}s"
    assert not math.isnan(elapsed)
