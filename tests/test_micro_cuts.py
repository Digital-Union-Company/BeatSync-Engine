"""Micro Cuts — the Stage 4 rare-accent control (Creative Controls Extra).

Stage 4 has two layers. Cut Density shapes the **main rhythmic grid**; Micro Cuts governs the
**rare half-beat accent layer** that `add_rare_micro_cuts` adds on top of it, and nothing else. The
two must be separable: adding this control may not change Cut Density's output by a single cut, and
moving this control may not change the main grid at all.

Two fixtures, because the two obligations need different material:

* ``_micro_track`` is built to make the accent layer *reachable* — a calm main grid so the global
  cut-ratio band does not dominate, and isolated impact spikes sitting off the bar/phrase anchors at
  a tempo whose half-beat offset clears ``micro_min_gap``. Without all three the layer never fires
  and a monotonicity test would be vacuously green.
* ``test_cut_density`` 's own fixture is reused for the independence proof, so the comparison is
  against the material Core was actually calibrated on.

Stage 4 imports from ``auto_mode/__init__`` (librosa, cupy, logger), so the module is loaded onto a
synthesised parent package — the technique ``test_cut_density`` documents, reused here via its
``_load_stage4``.
"""

from __future__ import annotations

import ast
import os
import random

import pytest

np = pytest.importorskip("numpy", reason="Stage 4 is numpy-based")

from beatsync_fork import creative as fork_creative

from test_cut_density import _DURATION, _TEMPO, _fixture, _load_stage4

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUTO_MODE_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_STAGE4_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage4_select.py")


@pytest.fixture(scope="module")
def stage4():
    module, _ = _load_stage4()
    return module


@pytest.fixture(scope="module")
def shared():
    _, package = _load_stage4()
    return package


# ---------------------------------------------------------------------------
# A fixture where the rare-accent layer can actually fire
# ---------------------------------------------------------------------------

_MICRO_TEMPO = 80.0          # period 0.75s -> half-beat offset 0.375s, clear of micro_min_gap 0.34
_MICRO_BEATS = 400
_MICRO_DURATION = 300.0
_MICRO_PERIOD = 60.0 / _MICRO_TEMPO


def _micro_track():
    """Beat grid, features and sections tuned so the accent layer is reachable.

    Three properties are load-bearing and each one was necessary to get any extras at all:

    * **tempo** — at 0.75 s/beat the half-beat accent sits 0.375 s from its neighbours, above
      ``micro_min_gap`` (0.34 s). At a fast tempo every accent is rejected by the gap rule and the
      control looks dead through no fault of its own.
    * **spike placement** — impact peaks sit at ``idx % 16 == 7``, deliberately *off* the bar and
      phrase anchors, so the main selector does not already own those beats.
    * **calm baseline** — a low wave keeps the main grid sparse, with the wave lifted over the
      ``wave >= 0.88`` gate only at the spikes. That gate is a Stage-4 literal this control
      deliberately does not touch.
    """
    rng = random.Random(7)
    beat_times = np.array([i * _MICRO_PERIOD for i in range(_MICRO_BEATS)], dtype=float)
    idx = np.arange(_MICRO_BEATS)

    wave = np.clip(0.40 + np.array([rng.uniform(-0.03, 0.03) for _ in idx]), 0.0, 1.0)
    wave[idx % 16 == 7] = 0.93
    impact = np.clip(0.45 + np.array([rng.uniform(-0.05, 0.05) for _ in idx]), 0.0, 1.0)
    spikes = idx % 16 == 7
    impact[spikes] = np.clip(0.97 + np.array([rng.uniform(0, 0.03) for _ in idx[spikes]]), 0.0, 1.0)
    impact[idx % 8 == 3] = np.maximum(impact[idx % 8 == 3], 0.88)
    rhythm = np.clip(wave * 0.7, 0.0, 1.0)
    novelty = np.clip(np.array([rng.uniform(0.0, 1.0) for _ in idx]), 0.0, 1.0)

    features = {
        "wave": wave, "arc": np.clip(idx / _MICRO_BEATS, 0.0, 1.0),
        "impact_score": impact, "rhythm_score": rhythm, "novelty": novelty,
        "is_bar_anchor": (idx % 4 == 0), "is_phrase_anchor": (idx % 8 == 0),
        "is_strong_kick": (impact >= 0.7).astype(float),
        "is_strong_clap": (rhythm >= 0.7).astype(float),
        "is_strong_bass": (wave >= 0.7).astype(float),
        "is_strong_hihat": (novelty >= 0.7).astype(float),
    }
    span = _MICRO_DURATION / 4
    sections = [{"index": i, "type": kind, "start": i * span, "end": (i + 1) * span,
                 "duration": span, "energy": 0.45, "dominant_pattern": "mixed"}
                for i, kind in enumerate(["verse", "breakdown", "verse", "breakdown"])]
    return beat_times, features, sections


@pytest.fixture(scope="module")
def micro_track():
    return _micro_track()


@pytest.fixture(scope="module")
def main_grid(stage4, shared, micro_track):
    """The pre-accent main grid: exactly what `select_wave_cuts` builds before micro cuts."""
    beat_times, features, sections = micro_track
    selected = []
    for section in sections:
        idx = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]
        if idx.size:
            selected.extend(stage4.select_section_wave_cuts(
                idx, beat_times, features, section, shared.CONFIG))
    return np.asarray(selected, dtype=float)


def _cfg_for(shared, micro_value: int, base=None):
    """Exactly what `analyze_beats_auto` does for one Micro Cuts value."""
    profile = fork_creative.CreativeProfile(micro_cuts=micro_value)
    cfg = shared.CONFIG if base is None else base
    if profile.is_neutral_micro_cuts():
        return cfg
    return shared.micro_cut_scaled_config(cfg, profile)


def _extras(stage4, shared, micro_track, main_grid, micro_value: int):
    beat_times, features, _ = micro_track
    cfg = _cfg_for(shared, micro_value)
    out = stage4.add_rare_micro_cuts(main_grid.copy(), beat_times, features, _MICRO_DURATION, cfg)
    return out, out.size - main_grid.size


_SWEEP = (0, 25, 50, 75, 100)


# ===========================================================================
# 1. NEUTRAL IS CURRENT MAIN
# ===========================================================================


def test_neutral_micro_cuts_passes_the_config_through_untouched(shared):
    """Not "equal values" — the *same object*. A default render must not rebuild these fields."""
    assert _cfg_for(shared, 50) is shared.CONFIG


def test_neutral_micro_cuts_reproduces_the_rare_layer_exactly(stage4, shared, micro_track,
                                                              main_grid):
    """Against the production call with the untouched CONFIG, on the same grid."""
    beat_times, features, _ = micro_track
    expected = stage4.add_rare_micro_cuts(
        main_grid.copy(), beat_times, features, _MICRO_DURATION, shared.CONFIG)
    actual, _ = _extras(stage4, shared, micro_track, main_grid, 50)

    assert np.array_equal(actual, expected)


def test_a_bus_without_micro_cuts_resolves_neutral():
    """A Creative Controls Core profile carries no `micro_cuts`; it must read back at 50."""
    for mapping in ({"seed": 0}, {"seed": 0, "cut_density": 50, "energy_response": 50,
                                  "motion_bias": 50}):
        profile = fork_creative.CreativeProfile.from_mapping(mapping)
        assert profile.micro_cuts == fork_creative.DEFAULT_CONTROL
        assert profile.is_neutral_micro_cuts()


# ===========================================================================
# 2. THE CONTROL WORKS
# ===========================================================================


@pytest.fixture(scope="module")
def sweep(stage4, shared, micro_track, main_grid):
    return {m: _extras(stage4, shared, micro_track, main_grid, m) for m in _SWEEP}


def test_zero_adds_no_micro_cuts_at_all(sweep, main_grid):
    arr, added = sweep[0]

    assert added == 0
    assert np.array_equal(arr, main_grid), "the main grid must be returned untouched"


def test_zero_disables_the_layer_rather_than_scaling_it_down(shared):
    """Scaling alone cannot reach zero — 0.025/3 still rounds to an extra on a typical grid — so
    0 flips the existing enable flag instead."""
    cfg = _cfg_for(shared, 0)

    assert cfg.enable_rare_micro_cuts is False
    assert shared.CONFIG.enable_rare_micro_cuts is True, "the singleton must be untouched"


def test_extras_rise_monotonically_with_the_control(sweep):
    added = [sweep[m][1] for m in _SWEEP]

    assert all(a <= b for a, b in zip(added, added[1:])), added
    assert added[0] < added[2] < added[-1], added


def test_the_fixture_actually_exercises_the_layer(sweep):
    """Guards the suite against passing vacuously: if no setting ever adds an accent, every
    monotonicity assertion above is trivially satisfied and proves nothing."""
    assert sweep[50][1] > 0, "the neutral fixture produced no micro cuts at all"
    assert sweep[100][1] >= sweep[50][1] + 2, "the top end is not meaningfully more expressive"


def test_the_ratio_is_bounded_at_the_top_end(shared):
    cfg = _cfg_for(shared, 100)

    assert cfg.max_micro_cut_ratio <= fork_creative.MICRO_CUT_RATIO_CAP
    assert cfg.max_micro_cut_ratio == pytest.approx(0.075)
    for value in range(0, 101):
        derived = _cfg_for(shared, value)
        assert derived.max_micro_cut_ratio <= fork_creative.MICRO_CUT_RATIO_CAP, value


def test_the_minimum_gap_is_never_touched(shared):
    """`micro_min_gap` is the anti-flicker floor, not a creative dial."""
    for value in range(0, 101):
        assert _cfg_for(shared, value).micro_min_gap == shared.CONFIG.micro_min_gap, value


def test_the_percentile_stays_inside_its_declared_bounds(shared):
    for value in range(0, 101):
        cfg = _cfg_for(shared, value)
        if value == 0:
            continue  # layer disabled; percentile is irrelevant and left as-is
        assert fork_creative.MICRO_PERCENTILE_MIN <= cfg.micro_percentile <= fork_creative.MICRO_PERCENTILE_MAX


@pytest.mark.parametrize("micro", _SWEEP)
def test_every_setting_leaves_a_safe_timeline(stage4, shared, micro_track, main_grid, micro):
    """The accent layer may never produce an unsafe edit.

    The *final* cut count is deliberately not the assertion target: `final_wave_cleanup` applies a
    pre-existing global cut-ratio band that can clamp the total at either end independently of this
    control, so extras (asserted above) is the honest measure of what Micro Cuts owns.
    """
    beat_times, features, _ = micro_track
    arr, _ = sweep_entry = _extras(stage4, shared, micro_track, main_grid, micro)
    final = stage4.final_wave_cleanup(arr, beat_times, features, _MICRO_DURATION, shared.CONFIG)
    gaps = np.diff(np.sort(final))

    assert final.size > 0
    assert np.all(np.isfinite(final))
    assert np.array_equal(final, np.sort(final))
    assert np.all(gaps > 0.0), "duplicate cut times"
    assert float(np.min(gaps)) >= shared.CONFIG.peak_energy_min_interval
    assert float(np.max(final)) < _MICRO_DURATION
    assert sweep_entry[1] >= 0


def test_every_added_accent_sits_on_a_half_beat(stage4, shared, micro_track, main_grid):
    """Micro Cuts changes how many accents there are, never where an accent may go."""
    beat_times, _, _ = micro_track
    half_beats = beat_times[:-1] + 0.5 * np.diff(beat_times)

    for micro in (25, 75, 100):
        arr, added = _extras(stage4, shared, micro_track, main_grid, micro)
        extras = np.setdiff1d(arr, main_grid)
        assert extras.size == added
        for t in extras:
            assert float(np.min(np.abs(half_beats - t))) < 1e-9, (micro, t)


# ===========================================================================
# 3. THE CONFIG BOUNDARY
# ===========================================================================


def test_only_the_two_budget_fields_change(shared):
    base = shared.CONFIG
    for micro in (25, 75, 100):
        derived = _cfg_for(shared, micro)
        changed = {f for f in base.__dataclass_fields__
                   if getattr(derived, f) != getattr(base, f)}
        assert changed == {"max_micro_cut_ratio", "micro_percentile"}, (micro, changed)


def test_only_the_enable_flag_changes_at_zero(shared):
    base = shared.CONFIG
    derived = _cfg_for(shared, 0)
    changed = {f for f in base.__dataclass_fields__ if getattr(derived, f) != getattr(base, f)}

    assert changed == {"enable_rare_micro_cuts"}


def test_the_global_config_singleton_is_never_mutated(shared):
    before = {f: getattr(shared.CONFIG, f) for f in shared.CONFIG.__dataclass_fields__}
    for micro in range(0, 101, 5):
        _cfg_for(shared, micro)
    assert {f: getattr(shared.CONFIG, f) for f in shared.CONFIG.__dataclass_fields__} == before


def test_no_stage_1_to_3_field_is_touched(shared):
    base = shared.CONFIG
    for micro in (0, 25, 75, 100):
        derived = _cfg_for(shared, micro)
        for field in ("sr", "hop_length", "n_fft", "phrase_beats", "bar_beats",
                      "wave_smooth_beats", "section_min_seconds", "anchor_bonus", "phrase_bonus"):
            assert getattr(derived, field) == getattr(base, field), (micro, field)


# ===========================================================================
# 4. CUT DENSITY INDEPENDENCE — the load-bearing regression guard
# ===========================================================================


@pytest.fixture(scope="module")
def density_track():
    return _fixture()


def _select(stage4, shared, track, density: int, micro: int):
    """Stage 4 exactly as `analyze_beats_auto` composes it for one (density, micro) pair."""
    beat_times, features, sections = track
    profile = fork_creative.CreativeProfile(cut_density=density, micro_cuts=micro)
    if profile.is_neutral_cuts():
        cfg, factor = shared.CONFIG, None
    else:
        factor = profile.cut_density_factor()
        cfg = shared.density_scaled_config(shared.CONFIG, factor)
    if not profile.is_neutral_micro_cuts():
        cfg = shared.micro_cut_scaled_config(cfg, profile)
    return stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=cfg, density_factor=factor)


@pytest.mark.parametrize("density", [0, 50, 100])
def test_adding_the_micro_seam_did_not_change_cut_density(stage4, shared, density_track, density):
    """The Core algorithm, reproduced without any Micro Cuts involvement, must be bit-identical.

    This is what proves the new seam is additive: `micro_cuts=50` short-circuits before
    `micro_cut_scaled_config` is ever reached, so Cut Density behaves exactly as it did on main.
    """
    beat_times, features, sections = density_track
    profile = fork_creative.CreativeProfile(cut_density=density)
    if profile.is_neutral_cuts():
        core_cfg, factor = shared.CONFIG, None
    else:
        factor = profile.cut_density_factor()
        core_cfg = shared.density_scaled_config(shared.CONFIG, factor)
    expected, expected_info = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=core_cfg, density_factor=factor)

    actual, actual_info = _select(stage4, shared, density_track, density, 50)

    assert np.array_equal(actual, expected), f"Cut Density {density} drifted"
    assert [i["selected_count"] for i in actual_info] == [i["selected_count"] for i in expected_info]


@pytest.mark.parametrize("density", [0, 50, 100])
def test_micro_cuts_leaves_the_main_grid_untouched_at_any_density(stage4, shared, density_track,
                                                                  density):
    """Changing Micro Cuts may move only the rare-extra layer, never the section selection."""
    baseline_info = _select(stage4, shared, density_track, density, 50)[1]

    for micro in (0, 25, 75, 100):
        info = _select(stage4, shared, density_track, density, micro)[1]
        assert [i["selected_count"] for i in info] == [i["selected_count"] for i in baseline_info], \
            (density, micro)
        assert [i["beat_count"] for i in info] == [i["beat_count"] for i in baseline_info]
        assert [i["density"] for i in info] == [i["density"] for i in baseline_info]


def test_density_does_not_rewrite_micro_policy_and_micro_does_not_rewrite_the_grid(shared):
    """The independence contract, stated over the derived configs themselves.

    It is *not* a claim that the final counts are numerically independent — `max_extra` is a ratio of
    the main grid, so a denser grid still permits proportionally more accents. That pre-existing
    proportionality is documented and deliberately untouched.
    """
    micro_fields = ("enable_rare_micro_cuts", "max_micro_cut_ratio", "micro_min_gap",
                    "micro_percentile")
    grid_fields = ("low_energy_min_interval", "medium_energy_min_interval",
                   "high_energy_min_interval", "peak_energy_min_interval",
                   "low_energy_max_hold", "medium_energy_max_hold", "high_energy_max_hold",
                   "peak_energy_max_hold", "target_cut_ratio_min", "target_cut_ratio_max")

    for density in (0, 25, 75, 100):
        derived = shared.density_scaled_config(
            shared.CONFIG, fork_creative.CreativeProfile(cut_density=density).cut_density_factor())
        for field in micro_fields:
            assert getattr(derived, field) == getattr(shared.CONFIG, field), (density, field)

    for micro in (0, 25, 75, 100):
        derived = _cfg_for(shared, micro)
        for field in grid_fields:
            assert getattr(derived, field) == getattr(shared.CONFIG, field), (micro, field)


def test_composition_order_is_density_then_micro(shared):
    """Both non-neutral: the micro fields come from the density-derived config, and the grid fields
    survive the micro derivation."""
    profile = fork_creative.CreativeProfile(cut_density=75, micro_cuts=75)
    density_cfg = shared.density_scaled_config(shared.CONFIG, profile.cut_density_factor())
    composed = shared.micro_cut_scaled_config(density_cfg, profile)

    assert composed.low_energy_min_interval == density_cfg.low_energy_min_interval
    assert composed.target_cut_ratio_max == density_cfg.target_cut_ratio_max
    assert composed.max_micro_cut_ratio == pytest.approx(
        min(fork_creative.MICRO_CUT_RATIO_CAP,
            shared.CONFIG.max_micro_cut_ratio * profile.micro_cut_ratio_factor()))
    assert composed.micro_percentile == pytest.approx(shared.CONFIG.micro_percentile - 3.0)


def test_interleaved_renders_cannot_contaminate_each_other(stage4, shared, density_track):
    expected = {}
    grid = [(0, 50), (50, 50), (100, 50), (50, 0), (50, 100), (75, 75)]
    for density, micro in grid:
        expected[(density, micro)] = _select(stage4, shared, density_track, density, micro)[0]
    for density, micro in reversed(grid):
        assert np.array_equal(
            _select(stage4, shared, density_track, density, micro)[0],
            expected[(density, micro)]), (density, micro)


# ===========================================================================
# 5. THE SEAM
# ===========================================================================


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_add_rare_micro_cuts_still_only_reads_its_config():
    """The control is a derived config, not a new mechanism inside the accent algorithm.

    Executable statements only: the docstring legitimately names Cut Density and the Micro Cuts
    control in order to *state* the independence contract, so prose must not be able to fail this.
    """
    fn = _func(_tree(_STAGE4_PATH), "add_rare_micro_cuts")

    assert [arg.arg for arg in fn.args.args] == [
        "selected", "beat_times", "features", "audio_duration", "cfg"]
    # Everything it reads off the config, by name. `enable_rare_micro_cuts` contains the substring
    # "micro_cuts", so a naive text search would flag the legitimate field; this looks at the
    # attribute accesses themselves instead.
    read = {node.attr for node in ast.walk(fn)
            if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "cfg"}
    assert read == {"enable_rare_micro_cuts", "micro_percentile", "max_micro_cut_ratio",
                    "micro_min_gap"}, read

    # and it knows nothing about the creative layer at all
    names = {node.id for node in ast.walk(fn) if isinstance(node, ast.Name)}
    for forbidden in ("fork_creative", "micro_cut_scaled_config", "CreativeProfile", "profile"):
        assert forbidden not in names, f"add_rare_micro_cuts references {forbidden!r}"


def test_analyze_beats_auto_composes_density_then_micro_with_explicit_neutral_branches():
    body = ast.unparse(_func(_tree(_AUTO_MODE_PATH), "analyze_beats_auto"))

    assert "if profile.is_neutral_cuts():" in body
    assert "stage4_cfg = cfg" in body
    assert "if not profile.is_neutral_micro_cuts():" in body
    assert "stage4_cfg = micro_cut_scaled_config(stage4_cfg, profile)" in body
    # density must still be derived before micro layers on top of it
    assert body.index("density_scaled_config(cfg, density_factor)") < body.index(
        "micro_cut_scaled_config(stage4_cfg, profile)")


def test_stages_1_to_3_still_receive_the_original_config():
    fn = _func(_tree(_AUTO_MODE_PATH), "analyze_beats_auto")
    for call_name in ("detect_master_beat_grid", "analyze_wave_features", "analyze_sections"):
        call = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == call_name)
        rendered = ast.unparse(call)
        assert "stage4_cfg" not in rendered, call_name
        assert "micro" not in rendered.lower()


# ===========================================================================
# 6. DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_micro_cut_sweep(shared, sweep, main_grid):
    """Readable evidence. To read it::

        python -m pytest tests/test_micro_cuts.py -s -k practical_diagnostic
    """
    lines = []
    for micro in _SWEEP:
        arr, added = sweep[micro]
        cfg = _cfg_for(shared, micro)
        ratio = "disabled" if not cfg.enable_rare_micro_cuts else f"{cfg.max_micro_cut_ratio:.5f}"
        pct = "-" if not cfg.enable_rare_micro_cuts else f"{cfg.micro_percentile:.1f}"
        lines.append(f"  micro {micro:3d}  ratio {ratio:>8}  percentile {pct:>5}  "
                     f"accents added {added:3d}  grid {main_grid.size} -> {arr.size}")
    print(f"\nMicro Cuts on a {_MICRO_BEATS}-beat / {_MICRO_TEMPO:.0f} BPM fixture "
          f"(main grid {main_grid.size} cuts):\n" + "\n".join(lines))

    assert len(lines) == len(_SWEEP)


# ===========================================================================
# 6. FREESTYLE V1 — THE GLOBAL MICRO LAYER ON A HETEROGENEOUS TIMELINE
# ===========================================================================
#
# Micro Cuts stays **one global control**: `add_rare_micro_cuts` is called once, with the base
# config, after the per-section grids are composed. Nothing per-section may rewrite its four
# fields (`test_cut_density` pins that), and `add_rare_micro_cuts` itself is unchanged.
#
# What *did* need an answer is the accepted P0-R2 question: the legacy path leans on
# `final_wave_cleanup` after the micro pass, and the heterogeneous path does not call it. So
# `micro_extra_safety` runs instead, and it had better be doing real work — which is where the
# first measurement went wrong. A 4-cut grid gives `max_extra = round(4 * 0.025) = 0`, so the
# original collision fixture produced no extras at all and "no collision" was vacuous. The fixture
# below is **funded**: enough main-grid cuts that the ratio budget permits several extras, and two
# half-beat accents deliberately placed close enough to collide with each other.


#: 200 BPM. The tempo is the load-bearing choice and it took a measurement to find.
#:
#: `add_rare_micro_cuts` places an accent at the **half-beat after** a qualifying beat, so on a
#: uniform grid two accents from adjacent qualifying beats are exactly one *period* apart. The
#: layer's floor is `max(micro_min_gap, median_beat * 0.45)`, which is `micro_min_gap` (0.34 s) for
#: any period under ~0.756 s. A collision therefore needs `period < 0.34`, and the function's own
#: `median_beat < 0.22` guard sets the other end — so the window is period in [0.22, 0.34).
#:
#: At 0.75 s/beat (the `_micro_track` tempo) adjacent accents land 0.75 s apart and **no collision
#: is reachable at all**. The first version of this fixture used that tempo and measured nothing;
#: the calibration test below is what caught it.
_COLLIDE_PERIOD = 0.30
_COLLIDE_BEATS = 400
_COLLIDE_DURATION = _COLLIDE_BEATS * _COLLIDE_PERIOD
#: Every 8th beat, i.e. a cut every 2.4 s — sparse enough that an accent 1.05 s away clears the
#: floor against the main grid, so `add_rare_micro_cuts` accepts it and the only remaining question
#: is how it behaves against the other accepted accents.
_COLLIDE_GRID_STRIDE = 8
#: Adjacent qualifying pairs, each pair sitting mid-way between two main-grid cuts.
_COLLIDE_SPIKES = (3, 4, 11, 12, 19, 20, 27, 28)


def _funded_collision_fixture():
    """A main grid large enough to fund extras, with accent pairs that collide with EACH OTHER.

    The legacy hole needs four things at once, and the first attempt at this fixture satisfied only
    three:

    * **budget** — `max_extra = round(len(selected) * max_micro_cut_ratio)` must be >= 2. A 4-cut
      grid funds `round(4 * 0.025) = 0` extras and measures nothing.
    * **candidate accents closer to each other than the floor** — see the tempo note above.
    * **each accent far enough from the MAIN GRID to be accepted**, because that is the only
      distance `add_rare_micro_cuts` ever checks.
    * **nothing afterwards** — on the heterogeneous path there is no `final_wave_cleanup`, so the
      pair survives unless `micro_extra_safety` catches it.

    The main grid is constructed explicitly rather than selected, so the spacing the accents are
    judged against is a property of the fixture instead of an outcome of Stage 4's selector. That
    is deliberate: the thing under test is the accent layer, and letting the selector decide the
    grid is how the earlier probe lost control of the distances.
    """
    beat_times = np.array([i * _COLLIDE_PERIOD for i in range(_COLLIDE_BEATS)], dtype=float)
    idx = np.arange(_COLLIDE_BEATS)

    # A flat low baseline, lifted over the `wave >= 0.88` gate only at the spikes, so the candidate
    # set is exactly the spike beats whatever the percentile resolves to.
    wave = np.full(_COLLIDE_BEATS, 0.42, dtype=float)
    impact = np.full(_COLLIDE_BEATS, 0.45, dtype=float)
    for beat in _COLLIDE_SPIKES:
        wave[beat] = 0.95
        impact[beat] = 0.99
    rhythm = np.clip(wave * 0.7, 0.0, 1.0)
    novelty = np.full(_COLLIDE_BEATS, 0.3, dtype=float)

    features = {
        "wave": wave, "arc": np.clip(idx / _COLLIDE_BEATS, 0.0, 1.0),
        "impact_score": impact, "rhythm_score": rhythm, "novelty": novelty,
        "is_bar_anchor": (idx % 4 == 0), "is_phrase_anchor": (idx % 8 == 0),
        "is_strong_kick": (impact >= 0.7).astype(float),
        "is_strong_clap": (rhythm >= 0.7).astype(float),
        "is_strong_bass": (wave >= 0.7).astype(float),
        "is_strong_hihat": (novelty >= 0.7).astype(float),
    }
    main_grid = np.array(
        [float(beat_times[i]) for i in range(0, _COLLIDE_BEATS, _COLLIDE_GRID_STRIDE)],
        dtype=float)
    span = _COLLIDE_DURATION / 4
    sections = [{"index": i, "type": kind, "start": i * span, "end": (i + 1) * span,
                 "duration": span, "energy": 0.45, "dominant_pattern": "mixed"}
                for i, kind in enumerate(["verse", "chorus", "verse", "chorus"])]
    return beat_times, features, sections, main_grid


@pytest.fixture(scope="module")
def collision():
    return _funded_collision_fixture()


def _micro_floor(shared, beat_times, cfg=None):
    cfg = cfg if cfg is not None else shared.CONFIG
    diffs = np.diff(np.asarray(beat_times, dtype=float))
    return max(cfg.micro_min_gap, float(np.median(diffs)) * 0.45)


def _extras_of(grid, produced):
    grid_values = set(np.asarray(grid, dtype=float).tolist())
    return sorted(float(t) for t in np.asarray(produced, dtype=float).tolist()
                  if float(t) not in grid_values)


def _collision_run(stage4, shared, collision, micro_value: int = 100):
    """The explicit main grid, then the **real, unmodified** global micro pass."""
    beat_times, features, _, main_grid = collision
    cfg = _cfg_for(shared, micro_value)
    with_micro = stage4.add_rare_micro_cuts(
        main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    return main_grid, with_micro, cfg


# --- the fixture must not be vacuous -----------------------------------------------------


def test_the_collision_fixture_is_actually_funded(stage4, shared, collision):
    """The calibration that the first attempt failed. Without this, every assertion below would
    pass on an empty extras list."""
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    extras = _extras_of(main_grid, with_micro)

    budget = round(main_grid.size * cfg.max_micro_cut_ratio)
    assert main_grid.size >= 40, f"main grid too small to fund extras: {main_grid.size}"
    assert budget >= 2, f"max_extra = {budget}; the fixture funds no collision"
    assert len(extras) >= 2, f"only {len(extras)} extras were produced"


def test_the_legacy_micro_pass_keeps_its_own_floor_between_extras(stage4, shared, collision):
    """**The converted historical defect test. `add_rare_micro_cuts` is now self-safe.**

    HISTORY — this is the measurement the fix was built against, kept because the defect was real
    and shipped. Before Legacy Micro Cuts Safety R1, `add_rare_micro_cuts` built `selected_sorted`
    once before its loop and never returned an accepted extra to it, so each candidate was judged
    against the main grid **only**. Two accepted extras could therefore each clear the floor against
    the grid while violating it against each other. Measured on this exact fixture:

        before fix:  closest accepted extra pair 0.3000 s  against a 0.3400 s floor
                     (reproduced at Micro Cuts 75 and 100, budget fully funded)

    `final_wave_cleanup` did not close it either, and could not: it enforces
    `peak_energy_min_interval` (0.30 s, and *divided* by the density factor), which is a Cut Density
    gap. `micro_min_gap` is deliberately density-independent, so the two can never coincide.

    This test now asserts the **positive** invariant. It deliberately re-checks funded-ness first:
    without that, an empty or single-element extras list would satisfy the spacing assertion
    vacuously, which is exactly how the original version of this fixture failed.
    """
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    extras = _extras_of(main_grid, with_micro)
    floor = _micro_floor(shared, beat_times, cfg)

    budget = int(max(0, round(main_grid.size * cfg.max_micro_cut_ratio)))
    assert budget >= 2, f"max_extra = {budget}: the fixture no longer funds a collision"
    assert len(extras) >= 2, (
        f"only {len(extras)} extras produced; the spacing assertion below would be vacuous")

    closest = min((b - a for a, b in zip(extras, extras[1:])), default=float("inf"))
    assert closest >= floor - 1e-9, (
        f"two accepted extras are {closest:.4f}s apart, under the {floor:.4f}s micro floor — the "
        f"R1 occupied-set correction has regressed")
    # ...and the pre-fix value must no longer be reachable on this fixture.
    assert closest > 0.3000 + 1e-9, (
        f"closest pair {closest:.4f}s reproduces the pre-fix 0.3000s measurement")


# --- micro_extra_safety closes it on the Freestyle path ----------------------------------


def test_micro_extra_safety_closes_the_extra_to_extra_gap(stage4, shared, collision):
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    floor = _micro_floor(shared, beat_times, cfg)

    extras = _extras_of(main_grid, final)
    assert extras, "every extra was dropped; the filter is too aggressive to measure"
    for a, b in zip(extras, extras[1:]):
        assert b - a >= floor - 1e-9, f"extras {a:.4f} and {b:.4f} are {b - a:.4f}s apart"


def test_micro_extra_safety_also_respects_the_gap_to_the_main_grid(stage4, shared, collision):
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    floor = _micro_floor(shared, beat_times, cfg)

    for t in _extras_of(main_grid, final):
        assert float(np.min(np.abs(main_grid - t))) >= floor - 1e-9, t


def test_micro_extra_safety_never_removes_a_main_grid_cut(stage4, shared, collision):
    """The one thing it must never do. The main grid is each section's own Cut Density decision,
    already cleaned by that section's own band."""
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    assert set(main_grid.tolist()) <= set(final.tolist())
    assert final.size >= main_grid.size


def test_micro_extra_safety_keeps_the_earlier_of_a_colliding_pair(stage4, shared):
    """Deterministic in time order, same rule as `cross_section_safety`: keep earlier, drop later."""
    beat_times = np.array([i * 0.75 for i in range(40)], dtype=float)
    main_grid = np.array([0.0, 6.0, 12.0], dtype=float)
    with_micro = np.array([0.0, 3.0, 3.2, 6.0, 12.0], dtype=float)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, shared.CONFIG)
    assert list(final) == [0.0, 3.0, 6.0, 12.0], "3.2 should lose to the earlier 3.0"


def test_micro_extra_safety_never_compares_an_extra_against_itself(stage4, shared):
    """`occupied` starts as the main grid alone, and an accepted extra joins it only *after* its
    own check — so an extra can never measure a zero distance to itself.

    That is a real trap rather than a hypothetical: the P0-R2 probe excluded a cut from its own
    distance check *by value* against a rounded set, every extra matched itself, and it reported a
    spurious safety failure. Here a lone extra far from the grid must simply be accepted.
    """
    beat_times = np.array([i * 0.75 for i in range(40)], dtype=float)
    main_grid = np.array([0.0, 6.0], dtype=float)
    with_micro = np.array([0.0, 3.0, 6.0], dtype=float)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, shared.CONFIG)
    assert 3.0 in set(final.tolist()), "a lone extra was rejected by a comparison with itself"


def test_micro_extra_safety_measures_extras_against_the_whole_main_grid(stage4, shared):
    """`occupied` must be seeded with the grid, not built up from nothing. Starting empty would let
    the first extra sit anywhere, including on top of a main-grid cut."""
    beat_times = np.array([i * 0.75 for i in range(40)], dtype=float)
    main_grid = np.array([0.0, 3.0, 6.0], dtype=float)
    # 3.1 is 0.1s from the grid cut at 3.0 — far inside the 0.34s floor.
    with_micro = np.array([0.0, 3.0, 3.1, 6.0], dtype=float)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, shared.CONFIG)
    assert list(final) == [0.0, 3.0, 6.0], "an extra was accepted on top of a main-grid cut"


def test_micro_extra_safety_is_a_no_op_when_there_are_no_extras(stage4, shared):
    beat_times = np.array([i * 0.75 for i in range(40)], dtype=float)
    grid = np.array([0.0, 6.0, 12.0], dtype=float)
    assert list(stage4.micro_extra_safety(grid, grid, beat_times, shared.CONFIG)) == list(grid)
    assert list(stage4.micro_extra_safety(
        grid, np.array([], dtype=float), beat_times, shared.CONFIG)) == list(grid)


def test_micro_extra_safety_handles_an_empty_grid(stage4, shared):
    beat_times = np.array([i * 0.75 for i in range(40)], dtype=float)
    empty = np.array([], dtype=float)
    extras = np.array([3.0, 3.2, 9.0], dtype=float)
    final = stage4.micro_extra_safety(empty, extras, beat_times, shared.CONFIG)
    assert list(final) == [3.0, 9.0]


def test_micro_extra_safety_is_pure(stage4, shared, collision):
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    grid_before, micro_before = main_grid.copy(), with_micro.copy()
    stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    assert np.array_equal(main_grid, grid_before)
    assert np.array_equal(with_micro, micro_before)


def test_micro_extra_safety_is_deterministic(stage4, shared, collision):
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    first = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    second = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    assert np.array_equal(first, second)
    assert list(first) == sorted(first)


def test_the_floor_is_the_micro_layers_own_and_is_density_independent(shared):
    """`micro_min_gap` is rewritten by neither derived config, so a section's density cannot move
    the accent floor. Structural, from the configs themselves."""
    dense = shared.density_scaled_config(shared.CONFIG, 2.0)
    sparse = shared.density_scaled_config(shared.CONFIG, 0.5)
    assert dense.micro_min_gap == shared.CONFIG.micro_min_gap
    assert sparse.micro_min_gap == shared.CONFIG.micro_min_gap

    profile = __import__("beatsync_fork.creative", fromlist=["x"]).CreativeProfile(micro_cuts=100)
    assert shared.micro_cut_scaled_config(shared.CONFIG, profile).micro_min_gap == \
        shared.CONFIG.micro_min_gap


# --- the legacy path is untouched --------------------------------------------------------


def test_add_rare_micro_cuts_was_not_modified(stage4, shared, micro_track, main_grid):
    """Micro Cuts remains a derived config, not a new mechanism. `add_rare_micro_cuts` still takes
    exactly `(selected, beat_times, features, audio_duration, cfg)` and reads only `cfg`."""
    import inspect
    parameters = list(inspect.signature(stage4.add_rare_micro_cuts).parameters)
    assert parameters == ["selected", "beat_times", "features", "audio_duration", "cfg"]

    source = inspect.getsource(stage4.add_rare_micro_cuts).lower()
    for forbidden in ("freestyle", "section_settings", "micro_extra_safety", "declaration"):
        assert forbidden not in source, f"add_rare_micro_cuts references {forbidden!r}"


def test_the_legacy_uniform_path_output_is_unchanged_by_the_new_helper(stage4, shared,
                                                                      micro_track, main_grid):
    """`micro_extra_safety` exists only on the heterogeneous path. The uniform path must still be
    `add_rare_micro_cuts` -> `final_wave_cleanup`, with its frozen output."""
    beat_times, features, sections = micro_track
    cfg = _cfg_for(shared, 100)
    produced = stage4.add_rare_micro_cuts(main_grid, beat_times, features, _MICRO_DURATION, cfg)

    cuts, _ = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=_MICRO_TEMPO,
        audio_duration=_MICRO_DURATION, cfg=cfg)
    filtered = stage4.micro_extra_safety(main_grid, produced, beat_times, cfg)
    # Not an equality claim between the two pipelines — only that the uniform render did NOT go
    # through the new helper, which would have shown up as the helper's stricter extra spacing.
    assert cuts.size > 0
    assert filtered.size <= produced.size


def test_the_heterogeneous_path_calls_the_new_helper_and_the_legacy_one_does_not(stage4, shared,
                                                                                collision,
                                                                                monkeypatch):
    """Structural: which composition ran, asserted by tripwire rather than inferred from spacing."""
    beat_times, features, sections, _ = collision
    duration = _COLLIDE_DURATION
    calls = []
    real = stage4.micro_extra_safety

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(stage4, "micro_extra_safety", _counting)

    stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=80.0,
        audio_duration=duration, cfg=shared.CONFIG)
    assert calls == [], "the legacy path must not call micro_extra_safety"

    dense = shared.density_scaled_config(shared.CONFIG, 2.0)
    settings = {0: (dense, 2.0), 1: (shared.CONFIG, None),
                2: (shared.CONFIG, None), 3: (shared.CONFIG, None)}
    stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=80.0,
        audio_duration=duration, cfg=shared.CONFIG, section_settings=settings)
    assert calls == [1], "the heterogeneous path must call micro_extra_safety exactly once"


# ===========================================================================
# 7. FREESTYLE MICRO DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_micro_extra_collision(stage4, shared, collision):
    """The measurement behind the P0-R2 answer::

        python -m pytest tests/test_micro_cuts.py -s -k practical_diagnostic_micro_extra
    """
    beat_times = collision[0]
    main_grid, with_micro, cfg = _collision_run(stage4, shared, collision)
    final = stage4.micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    floor = _micro_floor(shared, beat_times, cfg)

    before = _extras_of(main_grid, with_micro)
    after = _extras_of(main_grid, final)
    worst_before = min((b - a for a, b in zip(before, before[1:])), default=float("inf"))
    worst_after = min((b - a for a, b in zip(after, after[1:])), default=float("inf"))

    print(f"\nMicro extras on a {main_grid.size}-cut funded grid (floor {floor:.4f}s):"
          f"\n  add_rare_micro_cuts : {len(before):2d} extras, closest pair {worst_before:.4f}s"
          f"\n  micro_extra_safety  : {len(after):2d} extras, closest pair {worst_after:.4f}s")

    assert worst_after >= floor - 1e-9


# ===========================================================================
# 8. LEGACY MICRO CUTS SAFETY R1 — THE SELF-SAFE ACCENT LAYER
# ===========================================================================
#
# R1 made `add_rare_micro_cuts` self-safe: an accepted extra joins the occupied set immediately, so
# every later candidate is measured against the main grid PLUS all earlier accepted extras.
#
# Two things these tests exist to stop regressing, and they pull in opposite directions:
#
#   * the SAFETY half — no accepted extra may sit closer than the floor to the grid or to another
#     accepted extra (that is the defect itself); and
#   * the BUDGET half — a rejected candidate must not consume budget, and the scan must continue, so
#     a funded render still delivers `max_extra` *safe* accents. Architecture B (post-filtering the
#     old output with `micro_extra_safety`) satisfies the first and silently fails the second:
#     measured 1 of 2 accents at Micro Cuts 75 and 2 of 4 at 100. `test_the_funded_budget_is_fully
#     _spent_on_safe_extras` is what makes a B-shaped regression loud.
#
# The floor is unchanged: `max(cfg.micro_min_gap, median_beat * 0.45)`. No new floor, nothing
# density-derived, and `micro_min_gap` / `max_micro_cut_ratio` / `micro_percentile` are untouched.


def _accepted_extras(stage4, shared, collision, micro_value):
    beat_times, features, _sections, main_grid = collision
    cfg = _cfg_for(shared, micro_value)
    produced = stage4.add_rare_micro_cuts(
        main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    return main_grid, cfg, _extras_of(main_grid, produced), produced


@pytest.mark.parametrize("micro", (75, 100))
def test_the_funded_fixture_still_funds_a_real_collision_opportunity(stage4, shared, collision,
                                                                     micro):
    """(A) Non-vacuity gate for everything below: the budget must permit >= 2 extras and the layer
    must actually produce them. An empty or single-element list must never be able to pass."""
    main_grid, cfg, extras, _ = _accepted_extras(stage4, shared, collision, micro)
    budget = int(max(0, round(main_grid.size * cfg.max_micro_cut_ratio)))
    assert budget >= 2, f"micro={micro}: max_extra={budget} funds no collision"
    assert len(extras) >= 2, f"micro={micro}: only {len(extras)} extras produced"


@pytest.mark.parametrize("micro", (25, 50, 75, 100))
def test_every_accepted_extra_clears_the_floor_against_the_main_grid(stage4, shared, collision,
                                                                     micro):
    """(B) extra-to-main safety. This half held before R1 too; it is pinned so the correction cannot
    be "achieved" by weakening it."""
    beat_times = collision[0]
    main_grid, cfg, extras, _ = _accepted_extras(stage4, shared, collision, micro)
    floor = _micro_floor(shared, beat_times, cfg)
    assert extras, f"micro={micro}: no extras, assertion would be vacuous"
    for t in extras:
        gap = float(np.min(np.abs(main_grid - t)))
        assert gap >= floor - 1e-9, f"micro={micro}: extra {t:.4f} is {gap:.4f}s from the grid"


@pytest.mark.parametrize("micro", (75, 100))
def test_every_accepted_extra_clears_the_floor_against_earlier_extras(stage4, shared, collision,
                                                                      micro):
    """(C) extra-to-extra safety — **the defect R1 fixed**.

    Asserted pairwise against every *earlier* accepted extra rather than only against the nearest
    neighbour in sorted order, because that is the actual invariant the occupied set provides.
    """
    beat_times = collision[0]
    main_grid, cfg, extras, _ = _accepted_extras(stage4, shared, collision, micro)
    floor = _micro_floor(shared, beat_times, cfg)
    assert len(extras) >= 2, f"micro={micro}: fewer than two extras, nothing to compare"
    for position, t in enumerate(extras):
        for earlier in extras[:position]:
            gap = abs(t - earlier)
            assert gap >= floor - 1e-9, (
                f"micro={micro}: extras {earlier:.4f} and {t:.4f} are {gap:.4f}s apart, "
                f"under the {floor:.4f}s floor")


@pytest.mark.parametrize("micro", (75, 100))
def test_the_funded_budget_is_fully_spent_on_safe_extras(stage4, shared, collision, micro):
    """(D) **The anti-Architecture-B test.** A rejected collider must not consume budget and the scan
    must continue, so a funded render delivers exactly `max_extra` accents — all of them safe.

    Post-filtering the old output would pass every safety test above and fail this one: measured
    1 of 2 at micro 75 and 2 of 4 at micro 100.
    """
    beat_times = collision[0]
    main_grid, cfg, extras, _ = _accepted_extras(stage4, shared, collision, micro)
    budget = int(max(0, round(main_grid.size * cfg.max_micro_cut_ratio)))
    floor = _micro_floor(shared, beat_times, cfg)
    assert len(extras) == budget, (
        f"micro={micro}: {len(extras)} accents delivered against a funded budget of {budget} — "
        f"a rejected candidate consumed budget, or the scan stopped early")
    closest = min((b - a for a, b in zip(extras, extras[1:])), default=float("inf"))
    assert closest >= floor - 1e-9, "the budget was spent on unsafe extras"


@pytest.mark.parametrize("micro", _SWEEP)
def test_the_main_grid_is_preserved_exactly(stage4, shared, collision, micro):
    """(E) The layer may append and may reject. It may never remove a main-grid cut or insert a
    main-grid anchor. Exact float identity: both sides are the same objects from the same
    arithmetic, so a tolerance would only add a way to miss a real change."""
    main_grid, cfg, _extras, produced = _accepted_extras(stage4, shared, collision, micro)
    grid_values = set(np.asarray(main_grid, dtype=float).tolist())
    produced_values = set(np.asarray(produced, dtype=float).tolist())
    assert grid_values <= produced_values, "a main-grid cut went missing"
    assert produced.size >= main_grid.size


@pytest.mark.parametrize("micro", _SWEEP)
def test_the_caller_arrays_are_never_mutated(stage4, shared, collision, micro):
    """(F) Input purity. `occupied` grows by `np.append`, which copies; nothing may write through to
    the caller's grid or beat array."""
    beat_times, features, _sections, main_grid = collision
    cfg = _cfg_for(shared, micro)
    grid_before = main_grid.copy()
    beats_before = beat_times.copy()
    impact_before = features["impact_score"].copy()
    stage4.add_rare_micro_cuts(main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    assert np.array_equal(main_grid, grid_before), "the selected grid was mutated"
    assert np.array_equal(beat_times, beats_before), "beat_times was mutated"
    assert np.array_equal(features["impact_score"], impact_before), "features were mutated"


def test_micro_zero_still_returns_the_caller_grid_object(stage4, shared, collision):
    """(G) The disabled-layer early return is untouched — and it must still hand back the caller's
    own object, not an equal rebuild, because that identity is what other suites assert."""
    beat_times, features, _sections, main_grid = collision
    cfg = _cfg_for(shared, 0)
    assert not cfg.enable_rare_micro_cuts, "micro 0 must disable the layer"
    out = stage4.add_rare_micro_cuts(main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    assert out is main_grid, "micro 0 no longer returns the caller's grid object"


@pytest.mark.parametrize("micro", _SWEEP)
def test_the_budget_bound_is_never_exceeded(stage4, shared, collision, micro):
    """(H) The correction may only ever *reject*, so it cannot raise the accent count above the
    funded budget."""
    main_grid, cfg, extras, _ = _accepted_extras(stage4, shared, collision, micro)
    budget = int(max(0, round(main_grid.size * cfg.max_micro_cut_ratio)))
    assert len(extras) <= budget, f"micro={micro}: {len(extras)} extras exceed budget {budget}"


def test_the_accent_layer_is_deterministic(stage4, shared, collision):
    """No RNG, no clock: two identical calls must agree exactly."""
    for micro in (75, 100):
        _g, _c, _e, first = _accepted_extras(stage4, shared, collision, micro)
        _g, _c, _e, second = _accepted_extras(stage4, shared, collision, micro)
        assert np.array_equal(first, second), f"micro={micro} is not deterministic"


# --- the compatibility oracle -------------------------------------------------------------
#
# The rule R1 is allowed to live by: an output may differ from pre-fix behaviour ONLY where the
# pre-fix run accepted an extra that violated the floor against the grid or against an earlier
# accepted extra. Everywhere else the arrays must be `np.array_equal`.
#
# The oracle has to be independent of the changed loop, so expected values are NOT regenerated from
# production. `_legacy_add_rare_micro_cuts_reference` below is a frozen transcription of the
# pre-R1 body — the single difference being that it measures against the ORIGINAL grid only.


def _legacy_add_rare_micro_cuts_reference(selected, beat_times, features, audio_duration, cfg):
    """LEGACY_REFERENCE_FOR_COMPATIBILITY_ONLY.

    A frozen copy of `add_rare_micro_cuts` **as it behaved before R1**: `selected_sorted` is built
    once and accepted extras never rejoin it. It exists so the compatibility matrix below has a
    reference that cannot drift with the production loop.

    Never imported by production, and `test_the_legacy_reference_is_test_only` pins that.
    """
    if not cfg.enable_rare_micro_cuts or len(beat_times) < 3 or selected.size == 0:
        return selected
    beat_diffs = np.diff(beat_times)
    median_beat = float(np.median(beat_diffs)) if beat_diffs.size else 0.5
    if median_beat < 0.22:
        return selected
    threshold = _safe_percentile_ref(features["impact_score"], cfg.micro_percentile, 0.97)
    max_extra = int(max(0, round(len(selected) * cfg.max_micro_cut_ratio)))
    if max_extra <= 0:
        return selected
    extras = []
    selected_sorted = np.sort(selected)
    candidates = np.where((features["impact_score"] >= threshold) & (features["wave"] >= 0.88))[0]
    for idx in candidates:
        if len(extras) >= max_extra or idx >= len(beat_times) - 1:
            break
        t = float(beat_times[idx] + 0.5 * (beat_times[idx + 1] - beat_times[idx]))
        if t <= 0.0 or t >= audio_duration:
            continue
        nearest = np.min(np.abs(selected_sorted - t)) if selected_sorted.size else 999.0
        if nearest >= max(cfg.micro_min_gap, median_beat * 0.45):
            extras.append(t)
    if not extras:
        return selected
    return np.concatenate([selected, np.asarray(extras, dtype=float)])


def _safe_percentile_ref(values, percentile, default):
    """The reference's own percentile, taken from the shared helper so the frozen copy cannot drift
    on an unrelated axis. Resolved lazily to keep the reference a plain module-level function."""
    _module, shared = _load_stage4()
    return shared._safe_percentile(values, percentile, default)


def _legacy_violates_floor(grid, produced, beat_times, cfg):
    """Did the PRE-FIX run accept an extra that broke the floor? This is the only licence to differ."""
    diffs = np.diff(np.asarray(beat_times, dtype=float))
    median_beat = float(np.median(diffs)) if diffs.size else 0.5
    floor = max(cfg.micro_min_gap, median_beat * 0.45)
    extras = _extras_of(grid, produced)
    for position, t in enumerate(extras):
        if float(np.min(np.abs(np.asarray(grid, dtype=float) - t))) < floor - 1e-12:
            return True
        for earlier in extras[:position]:
            if abs(t - earlier) < floor - 1e-12:
                return True
    return False


_COMPAT_DENSITIES = (0, 25, 50, 75, 100)
_COMPAT_MICRO = (0, 25, 50, 75, 100)


@pytest.mark.parametrize("density", _COMPAT_DENSITIES)
@pytest.mark.parametrize("micro", _COMPAT_MICRO)
def test_r1_changes_nothing_except_where_the_old_behaviour_violated_the_floor(stage4, shared,
                                                                             density, micro):
    """The compatibility rule, on the realistic 13-section fixture: 25 density x micro combinations
    through the FULL legacy Stage-4 path.

    For every combination the pre-R1 reference and today's production must agree **exactly**, unless
    the reference itself accepted a floor-violating extra — in which case production must be the one
    that is safe. Measured on this fixture: zero combinations differ, because the violation needs a
    fast tempo (the collision window is roughly 176-273 BPM) that this 123 BPM fixture never reaches.
    """
    beat_times, features, sections = _fixture()
    profile = fork_creative.CreativeProfile(cut_density=density, micro_cuts=micro)
    cfg, factor = (shared.CONFIG, None)
    if not profile.is_neutral_cuts():
        factor = profile.cut_density_factor()
        cfg = shared.density_scaled_config(shared.CONFIG, factor)
    if not profile.is_neutral_micro_cuts():
        cfg = shared.micro_cut_scaled_config(cfg, profile)

    grid = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=_TEMPO,
        audio_duration=_DURATION, cfg=cfg, density_factor=factor)[0]

    # the main grid alone, so extras can be identified on both sides
    bare = stage4.final_wave_cleanup(
        stage4.add_rare_micro_cuts(grid, beat_times, features, _DURATION, cfg),
        beat_times, features, _DURATION, cfg)
    legacy_micro = _legacy_add_rare_micro_cuts_reference(
        grid, beat_times, features, _DURATION, cfg)
    legacy = stage4.final_wave_cleanup(
        legacy_micro, beat_times, features, _DURATION, cfg)

    if _legacy_violates_floor(grid, legacy_micro, beat_times, cfg):
        current = stage4.add_rare_micro_cuts(grid, beat_times, features, _DURATION, cfg)
        assert not _legacy_violates_floor(grid, current, beat_times, cfg), (
            f"d={density} m={micro}: the old behaviour violated the floor and the new one still does")
    else:
        assert np.array_equal(bare, legacy), (
            f"d={density} m={micro}: output changed with no floor violation to justify it "
            f"({bare.size} vs {legacy.size} cuts)")


def test_the_compatibility_oracle_is_not_vacuous(stage4, shared, collision):
    """Calibration: the frozen reference must actually reproduce the pre-fix defect, or the matrix
    above is comparing production against itself."""
    beat_times, features, _sections, main_grid = collision
    cfg = _cfg_for(shared, 100)
    legacy = _legacy_add_rare_micro_cuts_reference(
        main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    current = stage4.add_rare_micro_cuts(
        main_grid, beat_times, features, _COLLIDE_DURATION, cfg)
    floor = _micro_floor(shared, beat_times, cfg)

    legacy_extras = _extras_of(main_grid, legacy)
    legacy_closest = min((b - a for a, b in zip(legacy_extras, legacy_extras[1:])),
                         default=float("inf"))
    assert legacy_closest < floor, (
        f"the frozen reference no longer reproduces the defect ({legacy_closest:.4f}s vs "
        f"{floor:.4f}s floor) — the compatibility matrix would be self-confirming")
    assert abs(legacy_closest - 0.3000) < 1e-9, (
        f"the reference reproduces {legacy_closest:.4f}s, not the recorded pre-fix 0.3000s")
    assert not np.array_equal(np.sort(legacy), np.sort(current)), (
        "reference and production agree on the funded fixture; the reference is not frozen pre-fix")


def test_the_legacy_reference_is_test_only():
    """The frozen reference must never be imported by production."""
    import ast
    for name in ("stage4_select.py", "__init__.py"):
        path = os.path.join(_REPO_ROOT, "src", "auto_mode", name)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        assert "_legacy_add_rare_micro_cuts_reference" not in source, path
        assert "LEGACY_REFERENCE_FOR_COMPATIBILITY_ONLY" not in source, path
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                assert "test_" not in ast.unparse(node), ast.unparse(node)


# --- the Freestyle heterogeneous path is unchanged ----------------------------------------


def _freestyle_hetero(stage4, shared, density, micro, rules):
    """The real heterogeneous composition, as `_select_wave_cuts_per_section` runs it."""
    from beatsync_fork import freestyle as fork_freestyle
    beat_times, features, sections = _fixture()
    # `build` is in the fixture but is not a `classify_section` outcome and cannot carry a rule;
    # `body` is the fallthrough-equivalent real type (see test_cut_density's uniform matrix).
    sections = [dict(s, type=("body" if s["type"] == "build" else s["type"])) for s in sections]
    profile = fork_creative.CreativeProfile(cut_density=density, micro_cuts=micro)
    declaration = fork_freestyle.FreestyleDeclaration(enabled=True, overrides=tuple(
        (t, fork_freestyle.SectionOverride(cut_density=d)) for t, d in sorted(
            rules.items(), key=lambda kv: fork_freestyle.SECTION_TYPES.index(kv[0]))))
    uniform, settings = shared._freestyle_stage4_plan(
        declaration, profile, shared.CONFIG, sections)
    if settings is None:
        return None, None, None, None
    resolved = int(profile.cut_density) if uniform is None else int(uniform)
    cfg, factor = shared._density_stage4_config(shared.CONFIG, profile, resolved)
    cuts, _info = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=_TEMPO,
        audio_duration=_DURATION, cfg=cfg, density_factor=factor, section_settings=settings)
    return cuts, cfg, beat_times, features


_FREESTYLE_RULES = ({"drop": 100}, {"chorus": 0}, {"intro": 0, "drop": 100},
                    {"intro": 100, "verse": 0, "chorus": 100, "drop": 0, "outro": 100})


@pytest.mark.parametrize("rules", _FREESTYLE_RULES)
@pytest.mark.parametrize("density", (0, 50, 100))
@pytest.mark.parametrize("micro", (0, 50, 100))
def test_the_freestyle_heterogeneous_path_is_unchanged_by_r1(stage4, shared, density, micro, rules):
    """R1 edits a function the Freestyle path also calls, so its output is pinned against the frozen
    pre-R1 reference. Measured: zero heterogeneous combinations change on realistic material."""
    current, cfg, beat_times, features = _freestyle_hetero(stage4, shared, density, micro, rules)
    if current is None:
        pytest.skip("this base/rule pair resolves uniformly, not to the heterogeneous path")

    real = stage4.add_rare_micro_cuts
    stage4.add_rare_micro_cuts = _legacy_add_rare_micro_cuts_reference
    try:
        legacy, _c, _b, _f = _freestyle_hetero(stage4, shared, density, micro, rules)
    finally:
        stage4.add_rare_micro_cuts = real
    assert np.array_equal(current, legacy), (
        f"d={density} m={micro} rules={rules}: Freestyle heterogeneous output changed "
        f"({current.size} vs {legacy.size} cuts)")


@pytest.mark.parametrize("micro", (25, 50, 75, 100))
def test_micro_extra_safety_is_now_a_no_op_on_the_funded_path(stage4, shared, collision, micro):
    """R1 makes the heterogeneous path's final safety pass redundant on tested inputs, and that
    redundancy is **deliberately retained** — `micro_extra_safety` stays in the composition as a
    preserved guard. This records the equivalence rather than acting on it; do not use it to justify
    removing the helper.
    """
    beat_times = collision[0]
    main_grid, cfg, _extras, produced = _accepted_extras(stage4, shared, collision, micro)
    after = stage4.micro_extra_safety(main_grid, produced, beat_times, cfg)
    assert np.array_equal(np.sort(produced), np.sort(after)), (
        f"micro={micro}: micro_extra_safety still removed something after R1")
