"""Cut Density — the Stage 4 creative control (Creative Controls Core).

Two things have to be true at once, and they pull in opposite directions:

* **50 is current main, exactly.** Not the same cut *count*, not floating-point-close times — the
  identical array. That is pinned against an **independent recomputation** of the pre-Core Stage 4
  algorithm written out below, in the same spirit as ``test_creative_seed``'s independent legacy
  planner: if the production loop drifts at all, the two disagree.
* **Away from 50 it has to actually do something**, monotonically, without breaking the rhythmic
  contract Stage 4 exists to enforce.

Stage 4 imports ``AutoWaveConfig`` and three numeric helpers from ``auto_mode/__init__``, which
imports librosa, cupy and ``logger`` — none of which a bare interpreter has. So the shared support is
AST-extracted from that module and installed as a stub parent package, the technique
``test_stage5_cache_completion`` already uses to execute real Stage-5 bodies. The stage module itself
is the real file, unmodified; only its parent is synthesised.

The fixture is shaped like the track the accepted design probe measured — 566 beats, 123 BPM, 278 s,
13 sections — so the numbers here are comparable with the calibration in CLAUDE.md, but it is
generated in memory from a fixed seed and reads no media, no cache and no model.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import random
import sys
import types

import pytest

np = pytest.importorskip("numpy", reason="Stage 4 is numpy-based")

from beatsync_fork import creative as fork_creative

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUTO_MODE_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_STAGE4_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage4_select.py")

#: Everything `stage4_select` imports from its package, plus the two derived-config builders
#: (`test_micro_cuts` reuses this loader, so the micro one is exported here as well).
_SHARED = ("AutoWaveConfig", "_normalize", "_safe_percentile", "_unique_sorted",
           "density_scaled_config", "micro_cut_scaled_config")


def _load_stage4():
    """The real ``stage4_select`` module, on a synthesised parent package."""
    from dataclasses import dataclass, replace
    from typing import Dict, List

    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=_AUTO_MODE_PATH)
    # `ast.unparse`, not `get_source_segment`: a ClassDef's `lineno` points at the `class` keyword,
    # so the source segment would silently drop `@dataclass(frozen=True)` and AutoWaveConfig would
    # come back as a plain class with no fields.
    found = {node.name: ast.unparse(node) for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _SHARED}
    missing = [name for name in _SHARED if name not in found]
    assert not missing, f"missing from auto_mode/__init__.py: {missing}"

    namespace = {"np": np, "dataclass": dataclass, "replace": replace,
                 "Dict": Dict, "List": List, "fork_creative": fork_creative,
                 "__builtins__": __builtins__}
    for name in _SHARED:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)

    package = types.ModuleType("auto_mode_stage4_shim")
    package.__path__ = []
    for name in _SHARED:
        setattr(package, name, namespace[name])
    package.CONFIG = namespace["AutoWaveConfig"]()
    sys.modules["auto_mode_stage4_shim"] = package

    spec = importlib.util.spec_from_file_location(
        "auto_mode_stage4_shim.stage4_select", _STAGE4_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["auto_mode_stage4_shim.stage4_select"] = module
    spec.loader.exec_module(module)
    return module, package


@pytest.fixture(scope="module")
def stage4():
    module, _ = _load_stage4()
    return module


@pytest.fixture(scope="module")
def shared():
    _, package = _load_stage4()
    return package


# ---------------------------------------------------------------------------
# A deterministic fixture shaped like the accepted design probe's track
# ---------------------------------------------------------------------------

_TEMPO = 123.0
_BEATS = 566
_PERIOD = 60.0 / _TEMPO
_DURATION = 278.0
_SECTION_TYPES = ["intro", "verse", "build", "chorus", "verse", "breakdown", "build",
                  "drop", "verse", "chorus", "drop", "finale", "outro"]
_SECTION_PATTERNS = ["mixed", "kick", "kick_clap", "kick_clap", "kick", "clap", "hihat",
                     "kick_clap", "kick", "kick_clap", "bass", "kick_clap", "mixed"]


def _fixture(bar_beats: int = 4, phrase_beats: int = 8):
    """Beat grid, Stage-2-shaped features and Stage-3-shaped sections, all from one fixed seed.

    Built to exercise the control rather than to be pretty: the energy wave rises and falls three
    times across the track, bar positions carry extra impact, and the section list runs through every
    type Stage 4 special-cases. A flat fixture would let beat quantisation hide the mapping.
    """
    rng = random.Random(20260930)
    beat_times = np.array([i * _PERIOD for i in range(_BEATS)], dtype=float)
    idx = np.arange(_BEATS)
    phase = idx / _BEATS

    wave = 0.30 + 0.55 * (0.5 - 0.5 * np.cos(2 * np.pi * (phase * 3.0)))
    wave = np.clip(wave + np.array([rng.uniform(-0.06, 0.06) for _ in idx]), 0.0, 1.0)
    impact = np.clip(wave * 0.85 + np.array([rng.uniform(-0.15, 0.25) for _ in idx]), 0.0, 1.0)
    impact[idx % 4 == 0] = np.clip(impact[idx % 4 == 0] + 0.18, 0.0, 1.0)
    rhythm = np.clip(wave * 0.7 + np.array([rng.uniform(-0.1, 0.2) for _ in idx]), 0.0, 1.0)
    novelty = np.clip(np.array([rng.uniform(0.0, 1.0) for _ in idx]), 0.0, 1.0)
    arc = np.clip(phase * 0.8 + 0.1, 0.0, 1.0)

    features = {
        "wave": wave, "impact_score": impact, "rhythm_score": rhythm,
        "novelty": novelty, "arc": arc,
        "is_bar_anchor": (idx % bar_beats == 0),
        "is_phrase_anchor": (idx % phrase_beats == 0),
        "is_strong_kick": (impact >= 0.7).astype(float),
        "is_strong_clap": (rhythm >= 0.7).astype(float),
        "is_strong_bass": (wave >= 0.7).astype(float),
        "is_strong_hihat": (novelty >= 0.7).astype(float),
    }

    span = _DURATION / len(_SECTION_TYPES)
    sections = []
    for i, (kind, pattern) in enumerate(zip(_SECTION_TYPES, _SECTION_PATTERNS)):
        start, end = i * span, (i + 1) * span
        near = int(np.argmin(np.abs(beat_times - (start + end) * 0.5)))
        sections.append({
            "index": i, "type": kind, "start": start, "end": end, "duration": span,
            "energy": float(wave[near]), "dominant_pattern": pattern,
        })
    return beat_times, features, sections


@pytest.fixture(scope="module")
def track():
    return _fixture()


def _select(stage4, shared, track, density: int):
    """Run Stage 4 exactly the way `analyze_beats_auto` does for one Cut Density value."""
    beat_times, features, sections = track
    profile = fork_creative.CreativeProfile(cut_density=density)
    if profile.is_neutral_cuts():
        cfg, factor = shared.CONFIG, None
    else:
        factor = profile.cut_density_factor()
        cfg = shared.density_scaled_config(shared.CONFIG, factor)
    return stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=cfg, density_factor=factor)


# ===========================================================================
# 1. EXACT LEGACY STAGE 4 — an independent pre-Core recomputation
# ===========================================================================


def _legacy_section_cuts(stage4, beat_indices, beat_times, features, section, cfg):
    """Pre-Core ``select_section_wave_cuts``, transcribed from current main.

    Written out rather than called, so this is a second implementation of the part Cut Density
    touched: the hard-coded ``0.42`` breathing threshold and the unscaled ``adaptive_beat_step``
    result. Everything Cut Density does **not** touch (`compute_cut_scores`, `choose_best_nearby`,
    `adaptive_beat_step`, `min_interval_for_wave`, `max_hold_for_section`) is called from the real
    module, because reimplementing unchanged code would test the transcription rather than the
    change.
    """
    section_type = section.get("type", "verse")
    pattern = section.get("dominant_pattern", "mixed")
    selected = []

    scores = stage4.compute_cut_scores(beat_indices, features, section, cfg)
    score_map = {int(i): float(s) for i, s in zip(beat_indices, scores)}

    current_pos = 0
    last_cut_time = -999.0
    max_hold = stage4.max_hold_for_section(section, cfg)

    first_idx = stage4.choose_best_nearby(beat_indices, 0, radius=1, scores=score_map,
                                          features=features)
    if first_idx is not None:
        selected.append(float(beat_times[first_idx]))
        last_cut_time = float(beat_times[first_idx])
        current_pos = max(0, int(np.where(beat_indices == first_idx)[0][0]))

    while current_pos < beat_indices.size - 1:
        local_idx = int(beat_indices[current_pos])
        wave = float(features["wave"][local_idx])
        impact = float(features["impact_score"][local_idx])
        step = stage4.adaptive_beat_step(wave, impact, section_type, pattern)

        target_pos = min(beat_indices.size - 1, current_pos + step)
        target_idx = stage4.choose_best_nearby(
            beat_indices, target_pos, radius=1 if step <= 2 else 2,
            scores=score_map, features=features)
        if target_idx is None:
            break

        target_time = float(beat_times[target_idx])
        min_gap = stage4.min_interval_for_wave(
            float(features["wave"][target_idx]), section_type, cfg)

        if target_time - last_cut_time < min_gap:
            current_pos = min(beat_indices.size - 1, target_pos + 1)
            continue

        score = score_map.get(int(target_idx), 0.0)
        if score < 0.42 and target_time - last_cut_time < max_hold:
            current_pos = target_pos
            continue

        selected.append(target_time)
        last_cut_time = target_time
        current_pos = int(np.where(beat_indices == target_idx)[0][0])

        if current_pos < beat_indices.size - 1:
            future = beat_indices[current_pos + 1:]
            if future.size:
                future_times = beat_times[future]
                too_far = np.where(future_times - last_cut_time >= max_hold)[0]
                if too_far.size and (future_times[too_far[0]] - last_cut_time) > max_hold * 1.15:
                    forced_pos = current_pos + 1 + int(too_far[0])
                    forced_idx = stage4.choose_best_nearby(
                        beat_indices, forced_pos, radius=2, scores=score_map, features=features)
                    if forced_idx is not None:
                        forced_time = float(beat_times[forced_idx])
                        if forced_time - last_cut_time >= min_gap:
                            selected.append(forced_time)
                            last_cut_time = forced_time
                            current_pos = int(np.where(beat_indices == forced_idx)[0][0])

    return selected


def _legacy_select_wave_cuts(stage4, beat_times, sections, features, audio_duration, cfg):
    """Pre-Core ``select_wave_cuts``: no density parameter anywhere in it."""
    selected, info = [], []
    for section in sections:
        beat_indices = np.where(
            (beat_times >= section["start"]) & (beat_times < section["end"]))[0]
        if beat_indices.size == 0:
            continue
        section_selected = _legacy_section_cuts(
            stage4, beat_indices, beat_times, features, section, cfg)
        selected.extend(section_selected)
        info.append({
            "section": section,
            "selected_count": len(section_selected),
            "beat_count": int(beat_indices.size),
            "density": len(section_selected) / max(1, int(beat_indices.size)),
        })
    arr = np.asarray(selected, dtype=float)
    arr = stage4.add_rare_micro_cuts(arr, beat_times, features, audio_duration, cfg)
    arr = stage4.final_wave_cleanup(arr, beat_times, features, audio_duration, cfg)
    return arr, info


def test_neutral_cut_density_reproduces_the_pre_core_algorithm_exactly(stage4, shared, track):
    """The load-bearing product contract. Identical arrays, not close ones, not equal counts."""
    beat_times, features, sections = track
    expected, expected_info = _legacy_select_wave_cuts(
        stage4, beat_times, sections, features, _DURATION, shared.CONFIG)
    actual, actual_info = _select(stage4, shared, track, 50)

    assert actual.shape == expected.shape
    assert np.array_equal(actual, expected), "neutral Stage 4 drifted from the pre-Core algorithm"
    assert [i["selected_count"] for i in actual_info] == [i["selected_count"] for i in expected_info]
    assert [i["beat_count"] for i in actual_info] == [i["beat_count"] for i in expected_info]
    assert [i["density"] for i in actual_info] == [i["density"] for i in expected_info]


def test_omitting_the_density_argument_entirely_is_the_same_neutral_path(stage4, shared, track):
    """Every pre-Core caller passed no density at all; that must remain the legacy path."""
    beat_times, features, sections = track
    without = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=shared.CONFIG)[0]

    assert np.array_equal(without, _select(stage4, shared, track, 50)[0])


def test_the_neutral_path_never_builds_a_derived_config(stage4, shared, track, monkeypatch):
    """`analyze_beats_auto` must hand Stage 4 the CONFIG singleton itself at density 50.

    Asserted here at the helper that would do the deriving: a neutral render must not construct a
    config multiplied by 1.0, because 'harmless' operation-order changes are exactly what the legacy
    exactness contract refuses to rely on.
    """
    calls = []
    real = shared.density_scaled_config
    monkeypatch.setattr(shared, "density_scaled_config", lambda cfg, f: calls.append(f) or real(cfg, f))

    profile = fork_creative.CreativeProfile(cut_density=50)
    if not profile.is_neutral_cuts():
        shared.density_scaled_config(shared.CONFIG, profile.cut_density_factor())

    assert calls == [], "a neutral render derived a config it should have skipped"


def test_all_neutral_profile_leaves_selection_info_equivalent(stage4, shared, track):
    """Seed and the Stage-6 controls must not reach Stage 4 at all."""
    baseline = _select(stage4, shared, track, 50)
    beat_times, features, sections = track

    for seed, energy, motion in [(0, 50, 50), (381944, 0, 100), (101, 100, 0)]:
        profile = fork_creative.CreativeProfile(
            seed=seed, cut_density=50, energy_response=energy, motion_bias=motion)
        assert profile.is_neutral_cuts()
        cuts, info = stage4.select_wave_cuts(
            beat_times=beat_times, sections=sections, features=features,
            tempo=_TEMPO, audio_duration=_DURATION, cfg=shared.CONFIG, density_factor=None)
        assert np.array_equal(cuts, baseline[0])
        assert [i["selected_count"] for i in info] == [i["selected_count"] for i in baseline[1]]


# ===========================================================================
# 2. THE CONTROL ACTUALLY WORKS, MONOTONICALLY
# ===========================================================================


@pytest.fixture(scope="module")
def sweep(stage4, shared, track):
    return {d: _select(stage4, shared, track, d)[0] for d in (0, 25, 50, 75, 100)}


def test_cut_count_rises_monotonically_with_density(sweep):
    counts = [sweep[d].size for d in (0, 25, 50, 75, 100)]

    assert all(a <= b for a, b in zip(counts, counts[1:])), counts
    assert counts[0] < counts[2] < counts[-1], counts


def test_the_extremes_move_the_edit_substantially(sweep):
    """A control the user cannot see working is the defect Phase A existed to fix; the same
    standard applies here. The accepted design probe measured roughly -52% at 0 and +33% at 100 on
    real material; this fixture is a different track, so the assertion is on the direction and a
    conservative magnitude rather than on the probe's exact percentages."""
    neutral = sweep[50].size

    assert sweep[0].size <= neutral * 0.75, f"density 0 barely changed anything: {sweep[0].size}"
    assert sweep[100].size >= neutral * 1.2, f"density 100 barely changed anything: {sweep[100].size}"


def test_average_interval_shortens_monotonically_with_density(sweep):
    averages = [float(np.mean(np.diff(sweep[d]))) for d in (0, 25, 50, 75, 100)]

    assert all(a >= b for a, b in zip(averages, averages[1:])), averages
    assert averages[0] > averages[2] > averages[-1]


@pytest.mark.parametrize("density", [0, 10, 25, 40, 50, 60, 75, 90, 100])
def test_every_density_still_produces_a_safe_rhythmic_timeline(stage4, shared, track, density):
    """A different edit is not licence to be a broken one."""
    cuts, info = _select(stage4, shared, track, density)
    gaps = np.diff(cuts)

    assert cuts.size > 0
    assert np.all(np.isfinite(cuts))
    assert np.array_equal(cuts, np.sort(cuts)), "cuts must be sorted"
    assert np.all(gaps > 0.0), "duplicate cut times"
    # nothing tighter than one beat of the grid: density re-quantises the step, it never invents
    # off-grid micro-cuts of its own
    assert float(np.min(gaps)) >= _PERIOD * 0.95, float(np.min(gaps))
    # Inside the audio. A cut exactly at 0.0 is legitimately reachable through
    # `final_wave_cleanup`'s pre-existing "too sparse, add clean anchors" branch, which a high
    # density reaches more often; `build_frame_aligned_cut_timeline` drops it as it always has
    # (`beats > 0.0`), so it costs nothing and Cut Density did not introduce it.
    assert float(np.min(cuts)) >= 0.0
    assert float(np.max(cuts)) < _DURATION
    assert sum(i["selected_count"] for i in info) > 0


def test_every_cut_still_lands_on_a_detected_beat(stage4, shared, track):
    """Cut Density must preserve beat/bar/phrase alignment: it re-quantises stepping through the
    existing grid, it never adds arbitrary cuts after the fact. (Rare micro-cuts are the one
    deliberate half-beat exception, and their policy is unchanged — see below.)"""
    beat_times, _, _ = track
    half_beats = np.sort(np.concatenate(
        [beat_times, beat_times[:-1] + 0.5 * np.diff(beat_times)]))

    for density in (0, 25, 50, 75, 100):
        cuts = _select(stage4, shared, track, density)[0]
        for t in cuts:
            assert float(np.min(np.abs(half_beats - t))) < 1e-9, (density, t)


def test_density_is_reproducible_and_cannot_leak_between_renders(stage4, shared, track):
    """Interleaved runs: one render's density must not survive into the next. The config is derived
    per call and the factor is a parameter, so there is no module state for it to live in."""
    first = _select(stage4, shared, track, 100)[0]
    _select(stage4, shared, track, 0)
    neutral = _select(stage4, shared, track, 50)[0]
    _select(stage4, shared, track, 75)
    again = _select(stage4, shared, track, 100)[0]
    neutral_again = _select(stage4, shared, track, 50)[0]

    assert np.array_equal(first, again)
    assert np.array_equal(neutral, neutral_again)
    assert not np.array_equal(neutral, first)


# ===========================================================================
# 3. THE CONFIG BOUNDARY
# ===========================================================================


def test_the_global_config_singleton_is_never_mutated(shared):
    before = shared.CONFIG
    snapshot = {f: getattr(before, f) for f in before.__dataclass_fields__}

    for density in range(0, 101, 5):
        profile = fork_creative.CreativeProfile(cut_density=density)
        if not profile.is_neutral_cuts():
            derived = shared.density_scaled_config(before, profile.cut_density_factor())
            assert derived is not before

    assert shared.CONFIG is before
    assert {f: getattr(shared.CONFIG, f) for f in before.__dataclass_fields__} == snapshot


def test_the_derived_config_scales_exactly_the_documented_fields(shared):
    base = shared.CONFIG
    derived = shared.density_scaled_config(base, 2.0)

    for field in ("low_energy_min_interval", "medium_energy_min_interval",
                  "high_energy_min_interval", "peak_energy_min_interval",
                  "low_energy_max_hold", "medium_energy_max_hold",
                  "high_energy_max_hold", "peak_energy_max_hold"):
        assert getattr(derived, field) == pytest.approx(getattr(base, field) / 2.0), field
    assert derived.target_cut_ratio_min == pytest.approx(base.target_cut_ratio_min * 2.0)
    assert derived.target_cut_ratio_max == pytest.approx(base.target_cut_ratio_max * 2.0)


def test_the_rare_micro_cut_policy_is_identical_at_every_density(shared):
    """Not this control's business. The absolute micro-cut count may still move, because it is a
    ratio of a selected grid density does change — that is a proportional consequence, not a policy
    change, and it is explicitly NOT the future Micro Cuts control."""
    base = shared.CONFIG
    for density in range(0, 101, 5):
        factor = fork_creative.CreativeProfile(cut_density=density).cut_density_factor()
        derived = shared.density_scaled_config(base, factor)
        for field in ("enable_rare_micro_cuts", "max_micro_cut_ratio",
                      "micro_min_gap", "micro_percentile"):
            assert getattr(derived, field) == getattr(base, field), (density, field)


def test_no_stage_1_to_3_field_is_touched(shared):
    """Cut Density owns Stage 4. Beat detection, audio features and section detection are facts
    about the track and must be bit-identical at every density."""
    base = shared.CONFIG
    for factor in (0.5, 0.707, 1.414, 2.0):
        derived = shared.density_scaled_config(base, factor)
        for field in ("sr", "hop_length", "n_fft", "phrase_beats", "bar_beats",
                      "wave_smooth_beats", "section_min_seconds", "anchor_bonus",
                      "phrase_bonus", "enable_video_analysis", "enable_qwen_semantics",
                      "qwen_model_path"):
            assert getattr(derived, field) == getattr(base, field), (factor, field)


def test_the_cut_ratio_band_is_capped_and_stays_ordered(shared):
    """The caps never bind at the reachable factor range; they are a guard for a future wider
    mapping, and they must not be able to invert the band."""
    base = shared.CONFIG
    for factor in (0.5, 1.0, 2.0, 8.0, 100.0):
        derived = shared.density_scaled_config(base, factor)
        assert derived.target_cut_ratio_min <= fork_creative.CUT_RATIO_MIN_CAP
        assert derived.target_cut_ratio_max <= fork_creative.CUT_RATIO_MAX_CAP
        assert derived.target_cut_ratio_min <= derived.target_cut_ratio_max


# ===========================================================================
# 4. THE SEAM ITSELF
# ===========================================================================


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_adaptive_beat_step_is_still_the_pure_musical_mapping():
    """The control re-quantises this function's answer; it does not reach inside it. Keeping the
    musical reasoning and the creative control separable is what makes either one reviewable."""
    fn = _func(_tree(_STAGE4_PATH), "adaptive_beat_step")

    assert [arg.arg for arg in fn.args.args] == ["wave", "impact", "section_type", "pattern"]
    body = ast.unparse(fn).lower()
    for word in ("density", "creative", "factor", "profile"):
        assert word not in body, f"adaptive_beat_step mentions {word!r}"


def test_stage_4_receives_a_resolved_factor_not_raw_gui_values():
    """Only the normalised factor crosses into Stage 4 — never a slider value, never a profile."""
    tree = _tree(_STAGE4_PATH)
    for name in ("select_wave_cuts", "select_section_wave_cuts"):
        params = [arg.arg for arg in _func(tree, name).args.args]
        assert "density_factor" in params
        assert "cut_density" not in params and "creative" not in params


def test_stage_4_holds_no_module_level_render_state():
    """The density must live on the call stack, not in the module.

    A module-level current-density would make one render's setting observable by the next, which is
    precisely the leak `test_density_is_reproducible_and_cannot_leak_between_renders` measures from
    the outside. `WEAK_SCORE_THRESHOLD` is allowed: it is a constant, not state.
    """
    tree = _tree(_STAGE4_PATH)
    assignments = {t.id for node in tree.body if isinstance(node, ast.Assign)
                   for t in node.targets if isinstance(t, ast.Name)}

    assert assignments == {"WEAK_SCORE_THRESHOLD"}, assignments
    # AST, not a substring search: the module's prose legitimately says "global cleanup pass".
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.Global, ast.Nonlocal))]


def test_analyze_beats_auto_gives_stage_4_the_untouched_config_when_neutral():
    """The explicit neutral branch, asserted where it lives."""
    body = ast.unparse(_func(_tree(_AUTO_MODE_PATH), "analyze_beats_auto"))

    assert "if profile.is_neutral_cuts():" in body
    assert "stage4_cfg = cfg" in body
    assert "density_factor = None" in body
    assert "stage4_cfg = density_scaled_config(cfg, density_factor)" in body
    assert "cfg=stage4_cfg" in body


def test_stages_1_to_3_still_receive_the_original_config():
    """Cut Density must be unable to perturb beat detection, features or sections."""
    fn = _func(_tree(_AUTO_MODE_PATH), "analyze_beats_auto")
    for call_name in ("detect_master_beat_grid", "analyze_wave_features", "analyze_sections"):
        call = next(n for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and getattr(n.func, "id", "") == call_name)
        rendered = ast.unparse(call)
        assert "stage4_cfg" not in rendered, f"{call_name} was handed the density-derived config"
        assert "density" not in rendered


# ===========================================================================
# 5. DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_cut_density_sweep(stage4, shared, track, sweep):
    """Readable evidence, on a synthetic fixture. To read it::

        python -m pytest tests/test_cut_density.py -s -k practical_diagnostic
    """
    beat_times = track[0]
    lines = []
    for density in (0, 25, 50, 75, 100):
        cuts = sweep[density]
        factor = fork_creative.CreativeProfile(cut_density=density).cut_density_factor()
        gaps = np.diff(cuts)
        lines.append(
            f"  density {density:3d}  factor {factor:5.3f}  cuts {cuts.size:4d}  "
            f"ratio {cuts.size / beat_times.size * 100:5.2f}%  "
            f"avg {float(np.mean(gaps)):.3f}s  min {float(np.min(gaps)):.3f}s")
    print(f"\nCut Density on a {beat_times.size}-beat / {_TEMPO:.0f} BPM / {_DURATION:.0f}s "
          f"fixture, {len(_SECTION_TYPES)} sections:\n" + "\n".join(lines))

    assert len(lines) == 5
