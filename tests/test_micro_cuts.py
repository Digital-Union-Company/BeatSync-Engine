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
