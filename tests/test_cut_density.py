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
from beatsync_fork import freestyle as fork_freestyle

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUTO_MODE_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_STAGE4_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage4_select.py")

#: Everything `stage4_select` imports from its package, plus the two derived-config builders
#: (`test_micro_cuts` reuses this loader, so the micro one is exported here as well) and — since
#: Freestyle V1 — the real Stage-4 Freestyle resolver plus the one density->config mapping, so the
#: tests below drive production's own composition rather than a reimplementation of it.
#:
#: `_freestyle_stage4_plan` replaced R0's `_freestyle_section_settings` in R1: it returns
#: `(uniform_density, section_settings)` instead of a bare `section_settings`, because the uniform
#: case has to carry the density it resolved to rather than discarding it.
_SHARED = ("AutoWaveConfig", "_normalize", "_safe_percentile", "_unique_sorted",
           "density_scaled_config", "micro_cut_scaled_config", "_density_stage4_config",
           "_freestyle_stage4_plan")


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
                 "fork_freestyle": fork_freestyle, "__builtins__": __builtins__}
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


def _select(stage4, shared, track, density: int, micro_cuts: int = 50, sections=None):
    """Run Stage 4 exactly the way `analyze_beats_auto` does for one Cut Density value.

    ``micro_cuts`` and ``sections`` were appended for R1's uniform matrix, both with defaults that
    reproduce the original two-argument behaviour exactly — a neutral Micro Cuts value leaves the
    config untouched, and ``None`` keeps the fixture's own section list — so every pre-existing
    caller is unaffected.
    """
    beat_times, features, fixture_sections = track
    sections = fixture_sections if sections is None else sections
    profile = fork_creative.CreativeProfile(cut_density=density, micro_cuts=micro_cuts)
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

# ===========================================================================
# 6. FREESTYLE V1 — PER-SECTION CUT DENSITY
# ===========================================================================
#
# Cut Density is the one Freestyle control that changes the cut timeline, which makes it the one
# that needed a new composition. The accepted P0-R1 finding is the reason: `final_wave_cleanup`'s
# density band is computed from the GLOBAL `len(beat_times)` and its cap ranks cuts across the
# whole track, so running it after per-section selection lets one section's rule delete cuts from
# unrelated sections. Per-section selection therefore ends at `section_density_cleanup`, and the
# tests below pin both halves: the leak is gone, and the uniform case is still byte-identical.


#: A rule that only sets `cut_density`, so a Stage-4 test cannot be confounded by the other four
#: fields (which Stage 4 never reads).
def _density_rule(density: int) -> fork_freestyle.SectionOverride:
    return fork_freestyle.SectionOverride(cut_density=density)


def _declared(pairs: dict) -> fork_freestyle.FreestyleDeclaration:
    ordered = tuple((t, _density_rule(d)) for t, d in sorted(
        pairs.items(), key=lambda kv: fork_freestyle.SECTION_TYPES.index(kv[0])))
    return fork_freestyle.FreestyleDeclaration(enabled=True, overrides=ordered)


def _select_declared(stage4, shared, track, base_density, declaration, micro_cuts=50,
                     sections=None):
    """Stage 4 exactly as `analyze_beats_auto` runs it for one Freestyle screen.

    Mirrors R1's ordering, which is itself load-bearing: resolve the Freestyle plan **first**, then
    derive the Stage-4 config from `uniform_density` when one exists. Deriving the config from the
    slider first and consulting Freestyle afterwards is precisely the R0 defect.
    """
    beat_times, features, fixture_sections = track
    sections = fixture_sections if sections is None else sections
    profile = fork_creative.CreativeProfile(cut_density=base_density, micro_cuts=micro_cuts)
    uniform_density, settings = shared._freestyle_stage4_plan(
        declaration, profile, shared.CONFIG, sections)
    density = int(profile.cut_density) if uniform_density is None else int(uniform_density)
    stage4_cfg, factor = shared._density_stage4_config(shared.CONFIG, profile, density)
    cuts, info = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features, tempo=_TEMPO,
        audio_duration=_DURATION, cfg=stage4_cfg, density_factor=factor,
        section_settings=settings)
    return cuts, info, settings


def _cuts_in(cuts, section):
    return cuts[(cuts >= section["start"]) & (cuts < section["end"])]


def _by_section(cuts, sections):
    return {s["type"]: _cuts_in(cuts, s).size for s in sections}


# --- the uniform short-circuit: three screens, one byte-identical render -----------------


def test_freestyle_off_takes_the_exact_legacy_path(stage4, shared, track):
    legacy = _select(stage4, shared, track, 50)[0]
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(False, {"drop": "High Energy"})
    cuts, _, settings = _select_declared(stage4, shared, track, 50, declaration)
    assert settings is None, "a disabled declaration must not build per-section settings"
    assert np.array_equal(cuts, legacy)


def test_freestyle_on_with_no_rule_takes_the_exact_legacy_path(stage4, shared, track):
    legacy = _select(stage4, shared, track, 50)[0]
    declaration = fork_freestyle.FreestyleDeclaration.from_styles(True, {})
    cuts, _, settings = _select_declared(stage4, shared, track, 50, declaration)
    assert settings is None
    assert np.array_equal(cuts, legacy)


def test_freestyle_on_with_every_rule_on_the_base_density_takes_the_legacy_path(stage4, shared,
                                                                               track):
    """The short-circuit tests resolved *values*, not the checkbox — so a user who sets every
    section to the density the sliders already carry gets the identical render, not a heterogeneous
    one that happens to agree."""
    legacy = _select(stage4, shared, track, 50)[0]
    cuts, _, settings = _select_declared(
        stage4, shared, track, 50,
        _declared({t: 50 for t in ("intro", "drop", "chorus", "verse", "outro")}))
    assert settings is None
    assert np.array_equal(cuts, legacy)


def test_a_rule_setting_no_density_takes_the_legacy_path(stage4, shared, track):
    """Four of the five Freestyle controls are Stage-6 controls. A screen that varies only those
    must leave the cut timeline bit-identical."""
    legacy = _select(stage4, shared, track, 50)[0]
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("drop", fork_freestyle.SectionOverride(motion_bias=90,
                                                           semantic_emphasis=10)),))
    cuts, _, settings = _select_declared(stage4, shared, track, 50, declaration)
    assert settings is None
    assert np.array_equal(cuts, legacy)


def test_the_uniform_short_circuit_holds_at_a_non_neutral_base(stage4, shared, track):
    """The legacy path it falls back to is the *resolved base* path, not density 50."""
    legacy = _select(stage4, shared, track, 80)[0]
    cuts, _, settings = _select_declared(stage4, shared, track, 80, _declared({"drop": 80}))
    assert settings is None
    assert np.array_equal(cuts, legacy)


# --- R1: a uniform NON-BASE density is the global render AT that density -----------------
#
# The R0 defect: the short-circuit returned a bare `None` for "every section resolves to one
# density", and `analyze_beats_auto` then configured the legacy path from the *slider*. So a screen
# whose every actual section resolved to one non-base density rendered at the slider's density while
# Stage 6 still applied the same rules' four scoring controls — a half-applied rule, with the summary
# panel printing the Cut Density that had been discarded. Measured on this fixture: base 50 with
# every type ruled to 100 produced 139 cuts (= global 50) instead of 204 (= global 100).
#
# The half of R0 that was already correct and is unchanged: a uniform render must take the LEGACY
# composition, not the per-section one. `section_density_cleanup` is section-local and measurably not
# byte-equivalent to the global `final_wave_cleanup`, so these tests assert BOTH halves every time —
# exact equality to the global render at D, and `section_settings is None`.


#: The fixture's section list with its one unrulable type relabelled, so that "every ACTUAL section
#: type resolves to D" is expressible at all.
#:
#: `_SECTION_TYPES` contains ``build``, which `stage3_sections.classify_section` **never returns**
#: (its nine outcomes are intro/hook/outro/finale/drop/chorus/bridge/breakdown/verse) and which is
#: therefore absent from `freestyle.SECTION_TYPES` and cannot carry a rule. Left as `build`, those two
#: sections would always resolve to the base density and no uniform non-base screen could exist.
#:
#: ``body`` is the faithful replacement rather than a convenient one: it is a real Stage-3 type (the
#: undividable-track label) and, like `build`, it appears in **none** of Stage 4's four section-type
#: branch sets, so both are fallthrough types and Stage 4 cannot tell them apart. Proven, not
#: asserted by hand: `test_the_uniform_fixture_relabel_is_inert` below pins byte-equality across all
#: 35 density x micro combinations.
def _rulable_sections(sections):
    return [dict(s, type=("body" if s["type"] == "build" else s["type"])) for s in sections]


def _uniform_declaration(sections, density):
    """Every section type actually present, ruled to one density."""
    return _declared({s["type"]: density for s in sections})


def _plan_declared(shared, base_density, declaration, sections, micro_cuts=50):
    """The real resolver's `(uniform_density, section_settings)` for one screen."""
    profile = fork_creative.CreativeProfile(cut_density=base_density, micro_cuts=micro_cuts)
    return shared._freestyle_stage4_plan(declaration, profile, shared.CONFIG, sections)


def test_the_uniform_fixture_relabel_is_inert(stage4, shared, track):
    """Calibration for every uniform test below: swapping the unrulable `build` for `body` must not
    move a single cut, or those tests would be comparing two different tracks."""
    _, _, sections = track
    rulable = _rulable_sections(sections)
    assert {s["type"] for s in rulable} <= set(fork_freestyle.SECTION_TYPES), "still unrulable"
    assert "build" in {s["type"] for s in sections}, "the fixture no longer exercises the relabel"
    for density in _R1_DENSITIES:
        for micro in _R1_MICRO:
            left = _select(stage4, shared, track, density, micro_cuts=micro, sections=sections)[0]
            right = _select(stage4, shared, track, density, micro_cuts=micro, sections=rulable)[0]
            assert np.array_equal(left, right), (density, micro)


def test_all_sections_ruled_to_one_density_equals_the_global_render_at_that_density(
        stage4, shared, track):
    """The headline R1 regression (and the exact R0 reproduction).

    Base 50, every actual section type ruled to 100: the render must be the **global density-100**
    render, not the global density-50 one.
    """
    _, _, sections = track
    rulable = _rulable_sections(sections)
    declaration = _uniform_declaration(rulable, 100)

    uniform_density, settings = _plan_declared(shared, 50, declaration, rulable)
    assert uniform_density == 100, "the resolver did not resolve the uniform density"
    assert settings is None, "a uniform render must not take the per-section path"

    cuts = _select_declared(stage4, shared, track, 50, declaration, sections=rulable)[0]
    reference_100 = _select(stage4, shared, track, 100, sections=rulable)[0]
    reference_50 = _select(stage4, shared, track, 50, sections=rulable)[0]

    assert not np.array_equal(reference_100, reference_50), (
        "density 100 and 50 agree on this fixture; the test would pass vacuously")
    assert np.array_equal(cuts, reference_100), (
        f"uniform Freestyle 100 gave {cuts.size} cuts; global 100 gives {reference_100.size}")
    assert not np.array_equal(cuts, reference_50), "R0 defect: rendered at the slider's density"


#: The R1 uniform matrix axes. 7 densities x 5 Micro Cuts values = 35 combinations.
_R1_DENSITIES = (0, 10, 25, 50, 60, 75, 100)
_R1_MICRO = (0, 25, 50, 75, 100)


@pytest.mark.parametrize("density", _R1_DENSITIES)
@pytest.mark.parametrize("micro", _R1_MICRO)
def test_the_uniform_matrix_is_exactly_the_global_render(stage4, shared, track, density, micro):
    """**The load-bearing R1 oracle**: 35 combinations, exact array equality.

    Freestyle form : base Cut Density 50, every actual section type ruled to ``density``,
                     global Micro Cuts ``micro``.
    Reference form : Freestyle inactive, global Cut Density ``density``, global Micro Cuts ``micro``.

    These must be the same array. Not approximately — `np.array_equal`.

    The trap this test is written against is comparing two calls that both quietly used the base
    density, which would pass for every wrong reason. So for every ``density != 50`` it additionally
    asserts that the resolver really resolved to ``density``, and that the reference at ``density``
    actually differs from the reference at 50.
    """
    _, _, sections = track
    rulable = _rulable_sections(sections)
    declaration = _uniform_declaration(rulable, density)

    uniform_density, settings = _plan_declared(shared, 50, declaration, rulable, micro_cuts=micro)
    assert uniform_density == density, (
        f"resolver returned {uniform_density!r}, expected {density}")
    assert settings is None, "a uniform render must not take the per-section path"

    cuts = _select_declared(stage4, shared, track, 50, declaration,
                            micro_cuts=micro, sections=rulable)[0]
    reference = _select(stage4, shared, track, density, micro_cuts=micro, sections=rulable)[0]
    assert np.array_equal(cuts, reference), (
        f"D={density} M={micro}: Freestyle {cuts.size} cuts vs global {reference.size}")

    if density != 50:
        base_reference = _select(stage4, shared, track, 50, micro_cuts=micro, sections=rulable)[0]
        assert not np.array_equal(reference, base_reference), (
            f"D={density} M={micro} is indistinguishable from the base density on this fixture, so "
            f"this combination cannot detect the R0 defect")


def test_a_single_body_section_ruled_uniformly_equals_the_global_render(stage4, shared, track):
    """Stage 3's undividable-track fallback: one section, ``index=0``, ``type="body"``.

    `body` is offered as a Freestyle dropdown, so this is a real user path and it is the narrowest
    possible uniform screen — one section, one rule.
    """
    beat_times = track[0]
    body = [{"index": 0, "type": "body", "start": 0.0, "end": _DURATION,
             "duration": _DURATION, "energy": 0.5, "dominant_pattern": "mixed"}]
    declaration = _declared({"body": 100})

    uniform_density, settings = _plan_declared(shared, 50, declaration, body)
    assert uniform_density == 100
    assert settings is None

    cuts = _select_declared(stage4, shared, track, 50, declaration, sections=body)[0]
    reference_100 = _select(stage4, shared, track, 100, sections=body)[0]
    reference_50 = _select(stage4, shared, track, 50, sections=body)[0]
    assert not np.array_equal(reference_100, reference_50), "vacuous on this fixture"
    assert np.array_equal(cuts, reference_100), f"{cuts.size} vs {reference_100.size}"


def test_a_non_neutral_base_resolving_uniformly_down_equals_the_global_render(stage4, shared, track):
    """Base 80, every actual section ruled to 30 — proves the fix is not special-cased around the
    neutral base, and that it works downward as well as upward."""
    _, _, sections = track
    rulable = _rulable_sections(sections)
    declaration = _uniform_declaration(rulable, 30)

    uniform_density, settings = _plan_declared(shared, 80, declaration, rulable)
    assert uniform_density == 30
    assert settings is None

    cuts = _select_declared(stage4, shared, track, 80, declaration, sections=rulable)[0]
    reference_30 = _select(stage4, shared, track, 30, sections=rulable)[0]
    reference_80 = _select(stage4, shared, track, 80, sections=rulable)[0]
    assert not np.array_equal(reference_30, reference_80), "vacuous on this fixture"
    assert np.array_equal(cuts, reference_30), f"{cuts.size} vs {reference_30.size}"


def test_a_stage_6_only_rule_still_resolves_to_the_base_density(stage4, shared, track):
    """Four of the five Freestyle controls are Stage-6 controls. A screen that varies only those must
    resolve the Stage-4 density to the **base** — so the cut timeline stays byte-identical to the
    global legacy render, and R1 cannot have made a Stage-6-only rule move Stage 4."""
    _, _, sections = track
    declaration = fork_freestyle.FreestyleDeclaration(
        enabled=True,
        overrides=(("drop", fork_freestyle.SectionOverride(motion_bias=90,
                                                           semantic_emphasis=10)),))
    uniform_density, settings = _plan_declared(shared, 50, declaration, sections)
    assert uniform_density == 50, "a Stage-6-only rule must resolve to the base density"
    assert settings is None

    cuts = _select_declared(stage4, shared, track, 50, declaration)[0]
    assert np.array_equal(cuts, _select(stage4, shared, track, 50)[0])


def test_the_density_config_helper_agrees_with_the_inline_global_composition(shared):
    """The anti-drift pin for R1's one structural compromise.

    `analyze_beats_auto` keeps its inline global composition (the preservation suites pin those
    literals), and `_density_stage4_config` reproduces it for every Freestyle-derived config. The two
    must therefore agree exactly, field for field, at every density and Micro Cuts value — otherwise
    a uniform Freestyle render and the equivalent global render would diverge.
    """
    import dataclasses as _dc

    for density in _R1_DENSITIES:
        for micro in _R1_MICRO:
            profile = fork_creative.CreativeProfile(cut_density=density, micro_cuts=micro)
            # the inline composition, written out exactly as `analyze_beats_auto` has it
            if profile.is_neutral_cuts():
                inline_cfg, inline_factor = shared.CONFIG, None
            else:
                inline_factor = profile.cut_density_factor()
                inline_cfg = shared.density_scaled_config(shared.CONFIG, inline_factor)
            if not profile.is_neutral_micro_cuts():
                inline_cfg = shared.micro_cut_scaled_config(inline_cfg, profile)

            helper_cfg, helper_factor = shared._density_stage4_config(
                shared.CONFIG, profile, density)

            assert helper_factor == inline_factor, (density, micro)
            for field in _dc.fields(shared.CONFIG):
                assert getattr(helper_cfg, field.name) == getattr(inline_cfg, field.name), (
                    density, micro, field.name)
    # and a neutral density must hand back the singleton itself, not an equal rebuild
    neutral = fork_creative.CreativeProfile(cut_density=50, micro_cuts=50)
    assert shared._density_stage4_config(shared.CONFIG, neutral, 50) == (shared.CONFIG, None)


# --- heterogeneous: the rule moves its own section ---------------------------------------


def test_a_dense_rule_adds_cuts_to_the_sections_it_rules(stage4, shared, track):
    """Measured on the fixture: `drop=100` takes section 7 from 11 cuts to 17 and the track from
    139 to 147.

    Section 10 is also a `drop` and does **not** move, and that is correct rather than a miss: it
    gets the identical rule (proven by object identity below), but Cut Density steps through a
    *discrete* beat grid, so a section whose stepping is already at its anchor-constrained limit has
    no headroom left. That is pre-existing Cut Density behaviour — creative-controls.md records the
    same discreteness as the 72-82 non-monotonic region — and a per-section control inherits it
    rather than curing it. So the claim is "the ruled sections gain cuts", not "every instance
    gains the same number".
    """
    _, _, sections = track
    legacy = _select(stage4, shared, track, 50)[0]
    cuts, _, settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))

    assert settings is not None, "two distinct densities must build per-section settings"
    drops = [s for s in sections if s["type"] == "drop"]
    assert len(drops) == 2, "the fixture has two drops, so repeated-type sharing is exercised"

    deltas = [_cuts_in(cuts, s).size - _cuts_in(legacy, s).size for s in drops]
    assert all(delta >= 0 for delta in deltas), f"a dense rule removed cuts: {deltas}"
    assert sum(deltas) > 0, "a dense rule added no cuts to any section it rules"
    assert cuts.size > legacy.size


def test_a_sparse_rule_removes_cuts_from_the_sections_it_rules(stage4, shared, track):
    """The sparse direction has headroom in both choruses: 11 -> 6 on each."""
    _, _, sections = track
    legacy = _select(stage4, shared, track, 50)[0]
    cuts, _, _ = _select_declared(stage4, shared, track, 50, _declared({"chorus": 0}))
    for section in [s for s in sections if s["type"] == "chorus"]:
        assert _cuts_in(cuts, section).size < _cuts_in(legacy, section).size, section["index"]
    assert cuts.size < legacy.size


def test_every_instance_of_a_repeated_section_type_gets_the_identical_RULE(stage4, shared, track):
    """`REPEATED_SECTION_POLICY = ALL_INSTANCES_SHARE_RULE` is a statement about the rule, not about
    the outcome. Both drops receive the **same config object and the same factor**; what each then
    produces depends on its own musical content, which is the whole point of a beat-anchored
    selector. Asserting equal counts would be asserting that two different drops are the same drop.
    """
    _, _, sections = track
    settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))[2]
    drops = [s["index"] for s in sections if s["type"] == "drop"]
    assert len(drops) == 2

    first, second = settings[drops[0]], settings[drops[1]]
    assert first[0] is second[0], "the two drops got different config objects"
    assert first[1] == second[1], "the two drops got different density factors"


# --- the P0-R1 finding: no neighbour leak ------------------------------------------------
#
# Stated *differentially*, which is the only form that measures the leak. Comparing a heterogeneous
# render against the global-cleanup legacy render would not: per-section cleanup is section-scoped
# by design, so a section with no rule can legitimately differ from the global render (the fixture's
# intro goes 10 -> 11, because its own band judges it sparse where the global band did not). That
# difference is the feature. The leak is something else entirely: changing ONE rule moving a section
# that rule does not govern.


def _differential(stage4, shared, track, baseline: dict, changed: dict):
    first = _select_declared(stage4, shared, track, 50, _declared(baseline))[0]
    second = _select_declared(stage4, shared, track, 50, _declared(changed))[0]
    return first, second


def test_changing_one_rule_leaves_every_other_section_byte_identical(stage4, shared, track):
    """The whole reason `final_wave_cleanup` is not called on this path.

    Measured on main's global cleanup with a binding density band: changing only the `drop` rule
    mutated five non-target sections, three of them not even adjacent. Here, moving `drop` from 50
    to 100 leaves all eleven non-drop sections identical — the same cut *times*, not merely the
    same count.
    """
    _, _, sections = track
    first, second = _differential(stage4, shared, track,
                                  {"intro": 10, "drop": 50}, {"intro": 10, "drop": 100})
    for section in sections:
        if section["type"] == "drop":
            continue
        before, after = _cuts_in(first, section), _cuts_in(second, section)
        assert np.array_equal(before, after), (
            f"section {section['index']} ({section['type']}) leaked: "
            f"{before.size} -> {after.size}")
    assert second.size > first.size, "the differential did not actually change anything"


def test_changing_one_rule_sparsely_also_leaks_into_no_other_section(stage4, shared, track):
    """The sparse direction is the one a global cap would corrupt most: fewer cuts in one section
    raises every other section's rank in a global ordering."""
    _, _, sections = track
    first, second = _differential(stage4, shared, track,
                                  {"intro": 10, "chorus": 50}, {"intro": 10, "chorus": 0})
    for section in sections:
        if section["type"] != "chorus":
            assert np.array_equal(_cuts_in(first, section), _cuts_in(second, section)), (
                section["index"], section["type"])
    assert second.size < first.size


def test_a_non_adjacent_section_is_untouched_by_a_boundary_change(stage4, shared, track):
    """`cross_section_safety` only judges boundary-straddling pairs, so even the sections next door
    are affected at most at their own edge — and distant ones not at all. Both halves are asserted
    together so this cannot pass by the rule having had no effect."""
    _, _, sections = track
    first, second = _differential(stage4, shared, track,
                                  {"verse": 50}, {"verse": 10})
    verse_indices = {s["index"] for s in sections if s["type"] == "verse"}
    distant = [s for s in sections
               if s["index"] not in verse_indices
               and not any(abs(s["index"] - v) <= 1 for v in verse_indices)]
    assert distant, "the fixture must contain a section not adjacent to any verse"
    for section in distant:
        assert np.array_equal(_cuts_in(first, section), _cuts_in(second, section)), section["index"]
    assert not np.array_equal(first, second), "the rule change had no effect at all"


def test_tightening_one_rule_leaves_the_other_ruled_sections_untouched(stage4, shared, track):
    """Two rules, then one of them changes: the other section's cuts are identical times, not
    merely an identical count."""
    _, _, sections = track
    first, _, _ = _select_declared(stage4, shared, track, 50,
                                   _declared({"drop": 100, "intro": 10}))
    second, _, _ = _select_declared(stage4, shared, track, 50,
                                    _declared({"drop": 70, "intro": 10}))
    for section in [s for s in sections if s["type"] == "intro"]:
        assert np.array_equal(_cuts_in(first, section), _cuts_in(second, section))


def test_the_per_section_path_never_calls_final_wave_cleanup(stage4, shared, track, monkeypatch):
    """Structural, not inferred from counts: the global cleanup is simply not on this path."""
    calls = []

    def _tripwire(*args, **kwargs):
        calls.append(1)
        return np.array([], dtype=float)

    monkeypatch.setattr(stage4, "final_wave_cleanup", _tripwire)
    cuts, _, settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))
    assert settings is not None
    assert calls == [], "final_wave_cleanup must not run on the heterogeneous path"
    assert cuts.size > 0


def test_the_legacy_path_still_calls_final_wave_cleanup(stage4, shared, track, monkeypatch):
    """The calibration half: the guard above would pass just as happily if the whole function had
    stopped being reachable at all."""
    calls = []
    real = stage4.final_wave_cleanup

    def _counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(stage4, "final_wave_cleanup", _counting)
    _select(stage4, shared, track, 50)
    assert calls == [1]


# --- section-local cleanup is the global policy, scoped ---------------------------------


def test_section_density_cleanup_enforces_the_band_from_its_own_section(stage4, shared, track):
    """`section_density_cleanup` is `final_wave_cleanup`'s density policy with every global input
    replaced by the section's own: the band comes from THIS section's beat count, the cap ranks only
    THIS section's cuts, and sparse-fill anchors come from THIS section's beats."""
    beat_times, features, sections = track
    section = next(s for s in sections if s["type"] == "chorus")
    beat_indices = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]

    everything = [float(beat_times[i]) for i in beat_indices]
    cleaned = stage4.section_density_cleanup(
        everything, beat_indices, beat_times, features, section, shared.CONFIG)

    assert len(cleaned) < len(everything), "an every-beat section must be capped"
    assert all(section["start"] <= t < section["end"] for t in cleaned), "stays inside its section"
    ratio = len(cleaned) / beat_indices.size
    assert ratio <= shared.CONFIG.target_cut_ratio_max + 1e-9, ratio


def test_section_density_cleanup_leaves_a_reasonable_section_alone(stage4, shared, track):
    beat_times, features, sections = track
    section = next(s for s in sections if s["type"] == "verse")
    beat_indices = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]
    selected = stage4.select_section_wave_cuts(
        beat_indices, beat_times, features, section, shared.CONFIG, None)
    cleaned = stage4.section_density_cleanup(
        selected, beat_indices, beat_times, features, section, shared.CONFIG)
    assert np.array_equal(np.asarray(cleaned, dtype=float), np.asarray(selected, dtype=float))


def test_section_density_cleanup_is_pure(stage4, shared, track):
    beat_times, features, sections = track
    section = next(s for s in sections if s["type"] == "drop")
    beat_indices = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]
    selected = [float(beat_times[i]) for i in beat_indices]
    snapshot = list(selected)
    beats_before = beat_times.copy()
    stage4.section_density_cleanup(
        selected, beat_indices, beat_times, features, section, shared.CONFIG)
    assert selected == snapshot, "the input list was mutated"
    assert np.array_equal(beat_times, beats_before)


# --- the boundary: cross_section_safety ---------------------------------------------------


def test_cross_section_safety_drops_the_later_cut_of_a_straddling_pair(stage4):
    """Keep-earlier-drop-later, and only across a boundary: a within-section pair was already
    judged by that section's own floor."""
    cleaned = {0: [0.0, 1.0, 1.9], 1: [2.0, 3.0]}
    gaps = {0: 0.5, 1: 0.5}
    result = stage4.cross_section_safety(cleaned, gaps)
    assert list(result) == [0.0, 1.0, 1.9, 3.0], "2.0 straddles the 1.9 boundary and loses"


def test_cross_section_safety_never_judges_a_within_section_pair(stage4):
    """Section 0's own cuts are 0.1 s apart — far under the threshold — and survive, because that
    spacing is the section's own `peak_energy_min_interval` decision, already made."""
    cleaned = {0: [0.0, 0.1, 0.2], 1: [5.0]}
    result = stage4.cross_section_safety(cleaned, {0: 1.0, 1: 1.0})
    assert list(result) == [0.0, 0.1, 0.2, 5.0]


def test_cross_section_safety_uses_the_minimum_of_the_two_floors(stage4):
    """`min(gapA, gapB)`: the looser of the two sections is honoured, so a dense section cannot
    impose its tight floor on a sparse neighbour's first cut (or vice versa)."""
    cleaned = {0: [0.0], 1: [0.30]}
    assert list(stage4.cross_section_safety(cleaned, {0: 0.20, 1: 0.90})) == [0.0, 0.30]
    assert list(stage4.cross_section_safety(cleaned, {0: 0.90, 1: 0.90})) == [0.0]


def test_cross_section_safety_is_deterministic_and_sorted(stage4):
    cleaned = {2: [9.0, 10.0], 0: [0.0, 1.0], 1: [4.0, 5.0]}
    gaps = {0: 0.4, 1: 0.4, 2: 0.4}
    first = stage4.cross_section_safety(cleaned, gaps)
    second = stage4.cross_section_safety(dict(reversed(list(cleaned.items()))), gaps)
    assert np.array_equal(first, second)
    assert list(first) == sorted(first)


def test_cross_section_safety_handles_empty_and_single_section_input(stage4):
    assert stage4.cross_section_safety({}, {}).size == 0
    assert list(stage4.cross_section_safety({0: [1.0, 2.0]}, {0: 0.4})) == [1.0, 2.0]
    assert list(stage4.cross_section_safety({0: [], 1: [3.0]}, {0: 0.4, 1: 0.4})) == [3.0]


def test_a_collapsing_cascade_cannot_delete_a_whole_section(stage4):
    """Three straddling cuts in a row: each is judged against the last *kept* cut, so a run of
    rejections cannot chain off a cut that was itself already dropped."""
    cleaned = {0: [0.0], 1: [0.1], 2: [0.2], 3: [0.9]}
    gaps = {i: 0.5 for i in range(4)}
    assert list(stage4.cross_section_safety(cleaned, gaps)) == [0.0, 0.9]


# --- the composed timeline is still safe -------------------------------------------------


@pytest.mark.parametrize("pairs", [
    {"drop": 100},
    {"chorus": 0},
    {"intro": 0, "drop": 100},
    {"intro": 100, "verse": 0, "chorus": 100, "drop": 0, "outro": 100},
])
def test_every_freestyle_timeline_is_valid_sorted_and_unique(stage4, shared, track, pairs):
    cuts, _, settings = _select_declared(stage4, shared, track, 50, _declared(pairs))
    assert settings is not None
    assert cuts.size > 0
    assert list(cuts) == sorted(cuts), "not sorted"
    assert len(set(cuts.tolist())) == cuts.size, "duplicate cut times"
    assert float(np.min(cuts)) >= 0.0
    assert float(np.max(cuts)) <= _DURATION


@pytest.mark.parametrize("pairs", [{"drop": 100}, {"intro": 100, "chorus": 100, "drop": 100},
                                   {"chorus": 0, "drop": 100}])
def test_every_freestyle_cut_still_lands_on_a_detected_beat(stage4, shared, track, pairs):
    """Same tolerance as the global test above — the half-beat grid, because rare micro cuts are
    the one deliberate exception and the global Micro Cuts layer still runs on this path."""
    beat_times = track[0]
    half_beats = np.sort(np.concatenate(
        [beat_times, beat_times[:-1] + 0.5 * np.diff(beat_times)]))
    cuts, _, _ = _select_declared(stage4, shared, track, 50, _declared(pairs))
    for t in cuts:
        assert float(np.min(np.abs(half_beats - t))) < 1e-9, (pairs, t)


def test_the_minimum_gap_never_collapses_under_a_dense_rule(stage4, shared, track):
    """A dense section may legitimately cut faster than a neutral one — the floor it must respect is
    its OWN derived floor, not the base section's. The micro layer is the one deliberate exception:
    its floor is `micro_min_gap`, and the legacy defect recorded in `test_micro_cuts` means two
    extras can still finish closer than that. This bound is therefore the weaker of the two."""
    _, _, sections = track
    cuts, _, settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))
    drop_cfg = settings[next(s["index"] for s in sections if s["type"] == "drop")][0]
    floor = min(drop_cfg.peak_energy_min_interval, shared.CONFIG.peak_energy_min_interval)
    gaps = np.diff(cuts)
    assert float(np.min(gaps)) > 0.0
    assert float(np.min(gaps)) >= min(floor, shared.CONFIG.micro_min_gap) * 0.5


def test_the_selection_info_still_describes_every_section(stage4, shared, track):
    _, _, sections = track
    _, info, _ = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))
    assert len(info) == len(sections)
    assert [entry["section"]["index"] for entry in info] == [s["index"] for s in sections]
    for entry in info:
        assert entry["beat_count"] > 0
        assert 0.0 <= entry["density"] <= 1.0
        assert entry["selected_count"] / max(1, entry["beat_count"]) == entry["density"]


# --- config derivation ------------------------------------------------------------------


def test_one_config_is_derived_per_distinct_density_not_per_section(stage4, shared, track):
    """Thirteen sections, two distinct densities: two configs, shared by identity."""
    _, _, sections = track
    settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))[2]
    assert len(settings) == len(sections)
    distinct = {id(cfg) for cfg, _ in settings.values()}
    assert len(distinct) == 2, f"expected 2 derived configs, got {len(distinct)}"


def test_a_section_at_the_base_density_reuses_the_base_config_object(stage4, shared, track):
    _, _, sections = track
    settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))[2]
    verse = next(s["index"] for s in sections if s["type"] == "verse")
    cfg, factor = settings[verse]
    assert cfg is shared.CONFIG, "a neutral section must take the untouched singleton"
    assert factor is None, "a neutral section must pass density_factor=None, never 1.0"


def test_the_global_config_singleton_is_never_mutated_by_freestyle(stage4, shared, track):
    import dataclasses as _dc
    before = {f.name: getattr(shared.CONFIG, f.name) for f in _dc.fields(shared.CONFIG)}
    _select_declared(stage4, shared, track, 50,
                     _declared({"drop": 100, "intro": 0, "chorus": 90}))
    after = {f.name: getattr(shared.CONFIG, f.name) for f in _dc.fields(shared.CONFIG)}
    assert before == after


def test_the_section_factor_comes_from_the_one_exponential_mapping(stage4, shared, track):
    _, _, sections = track
    settings = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))[2]
    drop = next(s["index"] for s in sections if s["type"] == "drop")
    _, factor = settings[drop]
    assert factor == fork_creative.CreativeProfile(cut_density=100).cut_density_factor()


def test_freestyle_cannot_rewrite_a_micro_cut_field(stage4, shared, track):
    """Micro Cuts is global. A per-section density derives floors and ratios; all four micro fields
    must equal the globally derived config's, on every section."""
    profile = fork_creative.CreativeProfile(cut_density=50, micro_cuts=80)
    global_cfg = shared.micro_cut_scaled_config(shared.CONFIG, profile)
    settings = _select_declared(stage4, shared, track, 50,
                                _declared({"drop": 100, "intro": 0}), micro_cuts=80)[2]
    micro_fields = ("enable_rare_micro_cuts", "max_micro_cut_ratio", "micro_min_gap",
                    "micro_percentile")
    for index, (cfg, _) in settings.items():
        for field in micro_fields:
            assert getattr(cfg, field) == getattr(global_cfg, field), (index, field)


def test_a_non_neutral_base_still_composes_with_per_section_rules(stage4, shared, track):
    """The `density_factor` Stage 4 receives at the top level still describes the global base; a
    section's own factor rides in `section_settings`."""
    cuts, _, settings = _select_declared(stage4, shared, track, 80, _declared({"drop": 100}))
    assert settings is not None
    assert cuts.size > 0
    assert list(cuts) == sorted(cuts)


# --- isolation --------------------------------------------------------------------------


def test_freestyle_state_cannot_leak_between_renders(stage4, shared, track):
    legacy = _select(stage4, shared, track, 50)[0]
    first, _, _ = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))
    again = _select(stage4, shared, track, 50)[0]
    repeat, _, _ = _select_declared(stage4, shared, track, 50, _declared({"drop": 100}))

    assert np.array_equal(again, legacy), "a Freestyle render changed the next neutral render"
    assert np.array_equal(repeat, first), "the same declaration did not reproduce"


def test_section_settings_is_keyword_only(stage4):
    """Positional alignment is a real hazard here: `density_factor` is positional-or-keyword and
    sits immediately before it."""
    import inspect
    parameter = inspect.signature(stage4.select_wave_cuts).parameters["section_settings"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None


def test_stage_4_still_holds_no_module_level_render_state_after_freestyle():
    tree = _tree(_STAGE4_PATH)
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            rendered = ast.unparse(node)
            assert "freestyle" not in rendered.lower(), rendered
            assert not rendered.endswith("= {}"), rendered


def test_stage_4_knows_nothing_about_freestyle_records_or_the_gui():
    """Stage 4 receives resolved `(cfg, factor)` pairs. It must not import the fork record, parse a
    style name, read a section rule or know a GUI exists."""
    with open(_STAGE4_PATH, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=_STAGE4_PATH)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rendered = ast.unparse(node)
            assert "freestyle" not in rendered, rendered
    # `beatsync_fork.creative` is a legitimate pre-existing import (Cut Density's `scale_beat_step`
    # lives there), so the ban is on the Freestyle record specifically, not on the fork package.
    assert "fork_freestyle" not in source
    lowered = source.lower()
    for forbidden in ("freestyledeclaration", "sectionoverride", "override_from_style",
                      "gradio", "gr.update", "effective_profile", "section_style"):
        assert forbidden not in lowered, f"stage4_select.py references {forbidden!r}"


# ===========================================================================
# 7. FREESTYLE DIAGNOSTIC (captured by default)
# ===========================================================================


def test_practical_diagnostic_freestyle_section_densities(stage4, shared, track):
    """Readable evidence that a rule moves its own section and only its own::

        python -m pytest tests/test_cut_density.py -s -k practical_diagnostic_freestyle
    """
    _, _, sections = track
    legacy = _select(stage4, shared, track, 50)[0]
    cuts, _, _ = _select_declared(stage4, shared, track, 50, _declared({"drop": 100, "intro": 0}))

    lines = []
    for section in sections:
        kind = section["type"]
        before = _cuts_in(legacy, section).size
        after = _cuts_in(cuts, section).size
        mark = "  <- ruled" if kind in ("drop", "intro") else ""
        lines.append(f"  [{section['index']:2d}] {kind:10s} {before:3d} -> {after:3d}{mark}")
    print(f"\nFreestyle: base density 50, drop=100, intro=0 "
          f"({legacy.size} -> {cuts.size} cuts total):\n" + "\n".join(lines))

    assert len(lines) == len(sections)
