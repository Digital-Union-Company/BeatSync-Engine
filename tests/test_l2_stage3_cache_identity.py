"""L2 V1 — the Stage-3 cache *inside the real pipeline*.

``analyze_beats_auto`` cannot be imported on a bare interpreter (``auto_mode/__init__.py`` imports
librosa, cupy and ``logger``, which mutates ``PATH``/``CUDA_PATH`` at import time), so — exactly as
``test_cut_density.py`` and ``test_stage5_cache_identity.py`` do — the real function body and the
helpers it reaches are lifted out by their ``ast`` source ranges and executed against a synthesised
namespace. What runs here is therefore **production's own orchestration**, not a description of it.

Three things are real rather than stubbed, which is what makes the behavioural claims mean anything:

* ``beatsync_fork.stage_cache`` — the actual cache, key and copying;
* ``beatsync_fork.creative`` / ``presets`` / ``freestyle`` — the actual controls and declarations;
* ``stage4_select`` — the actual Stage 4, loaded on a synthesised parent package, so "Stage 4 still
  runs and its output may differ" is measured against the real selector.

Only the four things that need real audio are stubbed: ``librosa.load`` / ``normalize`` / ``hpss``
and the three Stage 1-3 functions. Stubbing those is not a convenience — **counting their calls is
the whole point**, because the claim under test is that a cache hit does not execute them.

Nothing here reads media, writes a cache file, launches Qwen, renders, or touches the Stage-5 cache.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import os
import subprocess
import sys
import types

import pytest

np = pytest.importorskip("numpy", reason="the pipeline is numpy-based")

from beatsync_fork import creative as fork_creative
from beatsync_fork import freestyle as fork_freestyle
from beatsync_fork import presets as fork_presets
from beatsync_fork import progress as fork_progress
from beatsync_fork import stage_cache as fork_stage_cache

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUTO_MODE_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_STAGE4_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage4_select.py")
_STAGE1_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage1_audio.py")
_STAGE2_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage2_features.py")
_STAGE3_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage3_sections.py")

_BASE_SHA = "768ef9da002c5a355eaad7237cfc1d94555a5afb"
_SECOND = 1_700_000_000_000_000_000

#: Everything the extracted `analyze_beats_auto` body reaches inside its own module. Listed
#: explicitly so a future edit that makes it call something new fails here with a clear NameError
#: rather than silently drifting.
_SHARED = (
    "AutoWaveConfig",
    "_to_float", "_normalize", "_smooth", "_safe_percentile", "_unique_sorted",
    "density_scaled_config", "micro_cut_scaled_config", "_resolve_freestyle",
    "_density_stage4_config", "_freestyle_stage4_plan", "_interp_to_beats",
    "_build_audio_visual_profile", "_notify_progress", "_notify_console", "_emit",
    "analyze_beats_auto",
)
#: The L2 additions. Absent at BASE, which is exactly how the differential harness tells the two
#: revisions apart without being told.
_L2_ONLY = ("_stage3_cache_key", "_stage3_cache_get", "_stage3_cache_put",
            "_emit_cached_stage123")


# ===========================================================================
# HARNESS
# ===========================================================================


def _source(revision: str | None) -> str:
    """The module source at `revision`, or the working tree when `revision is None`.

    `encoding="utf-8"` is **not** optional, and the explicit `open(..., encoding="utf-8")` above is
    why: `auto_mode/__init__.py` contains emoji in its console strings. With `text=True` alone,
    `subprocess` decodes with `locale.getpreferredencoding()` — `cp1252` on a default Windows shell —
    the reader thread dies on the first non-Latin-1 byte, `result.stdout` comes back `None`, and
    `ast.parse(None)` fails with a `TypeError` that says nothing about encoding. The repo's own
    entry points set `PYTHONUTF8=1` (`run.bat`) or `-X utf8`, which masked this; a bare
    `python -m pytest`, which `.claude/rules/test-harness.md` documents as the way to run the suite,
    did not.
    """
    if revision is None:
        with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
            return handle.read()
    return subprocess.run(
        ["git", "show", f"{revision}:src/auto_mode/__init__.py"],
        capture_output=True, text=True, encoding="utf-8", check=True, cwd=_REPO_ROOT).stdout


def _load_stage4(shared: dict):
    """The real ``stage4_select``, on a synthesised parent carrying the real shared helpers."""
    package_name = f"auto_mode_l2_shim_{id(shared)}"
    package = types.ModuleType(package_name)
    package.__path__ = []
    for name in ("AutoWaveConfig", "_normalize", "_safe_percentile", "_unique_sorted"):
        setattr(package, name, shared[name])
    package.CONFIG = shared["CONFIG"]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(f"{package_name}.stage4_select", _STAGE4_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package_name}.stage4_select"] = module
    spec.loader.exec_module(module)
    return module


class _Calls:
    """A tally of the operations a cache hit must not perform."""

    NAMES = ("load", "normalize", "hpss", "stage1", "stage2", "stage3", "stage4",
             "cache_get", "cache_put", "cache_hit")

    def __init__(self):
        self.counts = {name: 0 for name in self.NAMES}
        self.load_args = []

    def bump(self, name):
        self.counts[name] += 1

    def __getitem__(self, name):
        return self.counts[name]

    def __repr__(self):
        return repr({k: v for k, v in self.counts.items() if v})


def _deterministic_stage123(calls: _Calls, *, fail_stage: str | None = None):
    """Stage 1-3 stand-ins that are pure functions of the audio they are handed.

    Determinism is what lets a cache hit be compared for *exact* equality against the miss that
    produced it, and what lets the BASE and candidate revisions be compared against each other.
    """
    tempo = 123.0
    period = 60.0 / tempo

    def stage1(y_percussive, sr, cfg):
        calls.bump("stage1")
        if fail_stage == "stage1":
            raise RuntimeError("stage 1 exploded")
        beats = int(len(y_percussive) / sr / period)
        beat_times = np.arange(beats, dtype=float) * period
        beat_frames = (beat_times * sr / cfg.hop_length).astype(int)
        onset_env = np.abs(np.sin(np.arange(beats, dtype=float) * 0.7))
        return beat_times, tempo, beat_frames, onset_env

    def stage2(y, y_percussive, sr, beat_times, beat_frames, onset_env, cfg, use_gpu):
        calls.bump("stage2")
        if fail_stage == "stage2":
            raise RuntimeError("stage 2 exploded")
        n = len(beat_times)
        index = np.arange(n, dtype=float)
        wave = 0.5 + 0.45 * np.sin(index / max(1.0, float(cfg.wave_smooth_beats)))
        ramp = np.linspace(0.1, 0.9, n) if n else np.zeros(0)
        return {
            "wave": wave,
            "arc": ramp,
            "energy": wave * 0.9,
            "energy_levels": np.clip((wave * 4).astype(int), 0, 3),
            "rms_curve": ramp,
            "centroid_curve": ramp,
            "flux_curve": ramp,
            "kick": wave, "clap": ramp, "hihat": ramp, "bass": wave,
            "rhythm_score": wave, "impact_score": wave, "novelty": ramp,
            "is_strong_kick": wave > 0.6, "is_strong_clap": ramp > 0.6,
            "is_strong_hihat": ramp > 0.7, "is_strong_bass": wave > 0.5,
            "is_bar_anchor": (index % cfg.bar_beats) == 0,
            "is_phrase_anchor": (index % cfg.phrase_beats) == 0,
        }

    def stage3(y, y_harmonic, y_percussive, sr, beat_times, features, cfg):
        calls.bump("stage3")
        if fail_stage == "stage3":
            raise RuntimeError("stage 3 exploded")
        if len(beat_times) == 0:
            return []
        span = float(beat_times[-1]) + period
        count = max(1, int(span // max(1.0, float(cfg.section_min_seconds))))
        kinds = ("intro", "verse", "chorus", "drop", "bridge", "outro")
        edges = np.linspace(0.0, span, count + 1)
        return [
            {"index": i, "type": kinds[i % len(kinds)],
             "start": float(edges[i]), "end": float(edges[i + 1]),
             "duration": float(edges[i + 1] - edges[i]),
             "dominant_pattern": "mixed", "energy": float(0.3 + 0.1 * (i % 5))}
            for i in range(count)
        ]

    return stage1, stage2, stage3


def _fake_librosa(calls: _Calls, *, seconds=90.0, sr=22050, fail=None):
    librosa = types.ModuleType("librosa")

    def load(path, sr=sr, offset=0.0, duration=None, mono=True):
        calls.bump("load")
        calls.load_args.append({"path": path, "sr": sr, "offset": offset, "duration": duration})
        if fail == "load":
            raise RuntimeError("load exploded")
        span = seconds if duration is None else min(float(duration), seconds)
        span = max(0.0, span - float(offset or 0.0)) if duration is None else span
        n = max(1, int(span * sr))
        # content depends on the window, so a different trim yields a different analysis
        t = np.arange(n, dtype=float) / sr + float(offset or 0.0)
        return np.sin(2 * np.pi * 110.0 * t).astype(float), sr

    util = types.ModuleType("librosa.util")

    def normalize(y):
        calls.bump("normalize")
        peak = float(np.max(np.abs(y))) or 1.0
        return y / peak

    effects = types.ModuleType("librosa.effects")

    def hpss(y):
        calls.bump("hpss")
        return y * 0.5, y * 0.5

    util.normalize = normalize
    effects.hpss = hpss
    librosa.util = util
    librosa.effects = effects
    librosa.load = load
    return librosa


def _build(revision: str | None = None, *, seconds=90.0, fail_stage=None, fail_load=None):
    """A namespace whose ``analyze_beats_auto`` is the real body of ``revision``."""
    source = _source(revision)
    tree = ast.parse(source, filename=_AUTO_MODE_PATH)
    wanted = set(_SHARED) | set(_L2_ONLY)
    # `ast.unparse`, not `get_source_segment`: a ClassDef's `lineno` points at the `class` keyword,
    # so the segment silently drops `@dataclass(frozen=True)` and AutoWaveConfig loses its fields.
    found = {node.name: ast.unparse(node) for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in wanted}
    missing = [name for name in _SHARED if name not in found]
    assert not missing, f"missing from auto_mode/__init__.py@{revision or 'HEAD'}: {missing}"

    from dataclasses import dataclass, replace
    from typing import Callable, Dict, List, Tuple

    calls = _Calls()
    stage1, stage2, stage3 = _deterministic_stage123(calls, fail_stage=fail_stage)

    namespace = {
        "np": np, "os": os, "time": __import__("time"),
        "dataclass": dataclass, "replace": replace,
        "Callable": Callable, "Dict": Dict, "List": List, "Tuple": Tuple,
        "librosa": _fake_librosa(calls, seconds=seconds, fail=fail_load),
        "fork_progress": fork_progress,
        "fork_creative": fork_creative,
        "fork_freestyle": fork_freestyle,
        "fork_stage_cache": fork_stage_cache,
        "GPU_AVAILABLE": False,
        "clear_gpu_memory": lambda: None,
        "detect_master_beat_grid": stage1,
        "analyze_wave_features": stage2,
        "analyze_sections": stage3,
        "__builtins__": __builtins__,
    }
    for name in [n for n in (*_SHARED, *_L2_ONLY) if n in found]:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    namespace["CONFIG"] = namespace["AutoWaveConfig"]()

    stage4 = _load_stage4(namespace)
    real_select = stage4.select_wave_cuts

    def select_wave_cuts(**kwargs):
        calls.bump("stage4")
        return real_select(**kwargs)

    namespace["select_wave_cuts"] = select_wave_cuts
    namespace["_CALLS"] = calls

    # observe the cache seam without changing it
    if "_stage3_cache_get" in namespace:
        real_get = namespace["_stage3_cache_get"]
        real_put = namespace["_stage3_cache_put"]

        def counting_get(key):
            calls.bump("cache_get")
            hit = real_get(key)
            if hit is not None:
                calls.bump("cache_hit")
            return hit

        def counting_put(key, bundle):
            calls.bump("cache_put")
            return real_put(key, bundle)

        namespace["_stage3_cache_get"] = counting_get
        namespace["_stage3_cache_put"] = counting_put

    return namespace


@pytest.fixture(autouse=True)
def _cold_cache():
    """Every test starts with an empty process-local cache, and leaves one behind."""
    fork_stage_cache.STAGE3_CACHE.clear()
    yield
    fork_stage_cache.STAGE3_CACHE.clear()


@pytest.fixture
def pipeline():
    return _build()


@pytest.fixture
def track(tmp_path):
    path = str(tmp_path / "Nero - Satisfy.mp3")
    with open(path, "wb") as handle:
        handle.write(b"ID3" + b"\x7f" * 8192)
    os.utime(path, ns=(_SECOND, _SECOND))
    return path


def _run(namespace, track, **kwargs):
    return namespace["analyze_beats_auto"](track, **kwargs)


_FACTS = ("beat_times", "tempo", "features", "sections")
_DOWNSTREAM = ("selected_beats", "selection_info", "audio_visual_profile")


def _collect(selected, info):
    """Every output this milestone promises parity for."""
    return {
        "beat_times": info["times"],
        "tempo": info["tempo"],
        "features": {"energy_profile": info["energy_profile"], "rhythm_data": info["rhythm_data"]},
        "sections": info["sections"],
        "selected_beats": selected,
        "selection_info": info["selection_info"],
        "audio_visual_profile": info["audio_visual_profile"],
        "audio_duration": info["audio_duration"],
    }


def _assert_same(left, right, label=""):
    """Array-exact equality across the whole nested output shape."""
    def same(a, b, path):
        if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
            assert np.array_equal(np.asarray(a), np.asarray(b)), f"{label}{path}"
            return
        if isinstance(a, dict):
            assert isinstance(b, dict) and set(a) == set(b), f"{label}{path}: keys"
            for k in a:
                same(a[k], b[k], f"{path}.{k}")
            return
        if isinstance(a, (list, tuple)):
            assert type(a) is type(b) and len(a) == len(b), f"{label}{path}: length"
            for i, (x, y) in enumerate(zip(a, b)):
                same(x, y, f"{path}[{i}]")
            return
        assert a == b, f"{label}{path}: {a!r} != {b!r}"

    same(left, right, "")


# ===========================================================================
# §12  STRUCTURAL CONFIG-DRIFT GUARD
# ===========================================================================


def _cfg_reads(path: str, *, function: str | None = None) -> set[str]:
    """Every ``cfg.<field>`` attribute read in a file, or in one function of it."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    root = tree
    if function is not None:
        root = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == function)
    return {
        node.attr for node in ast.walk(root)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "cfg"
    }


def _frontend_cfg_reads() -> set[str]:
    """``cfg.<field>`` reads in ``analyze_beats_auto`` up to and including Stage 3.

    Located by the Stage-3 call rather than by a line number, so reordering inside the region cannot
    make this guard quietly stop covering part of it.
    """
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_AUTO_MODE_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "analyze_beats_auto")
    stage3_line = max(
        node.lineno for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "analyze_sections")
    return {
        node.attr for node in ast.walk(fn)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "cfg" and node.lineno <= stage3_line
    }


def test_drift_every_stage1to3_config_read_is_represented_in_the_key():
    """The guard that makes a future Stage 1-3 edit loud.

    It re-derives, from the real current source, every ``AutoWaveConfig`` field the audio front end
    and Stages 1-3 actually read, and requires each to participate in the Stage-3 analysis-config
    identity. If a future implementation starts reading another field, this fails until the cache
    identity has been consciously reconciled — rather than the cache silently serving an artifact
    computed under different settings.
    """
    stage_reads = (
        _cfg_reads(_STAGE1_PATH)
        | _cfg_reads(_STAGE2_PATH)
        | _cfg_reads(_STAGE3_PATH)
    )
    frontend_reads = _frontend_cfg_reads()
    actual = stage_reads | frontend_reads
    represented = set(fork_stage_cache.STAGE3_ANALYSIS_CONFIG_FIELDS)

    assert actual, "no cfg reads discovered — the guard would be vacuous"
    assert actual <= represented, (
        "Stage 1-3 reads config the Stage-3 cache key does not cover: "
        f"{sorted(actual - represented)}")


def test_drift_the_expected_current_set_is_pinned():
    """The set as measured at this milestone. A change here is a deliberate reconciliation, and the
    guard above is what forces this line to be revisited."""
    stage_reads = (_cfg_reads(_STAGE1_PATH) | _cfg_reads(_STAGE2_PATH) | _cfg_reads(_STAGE3_PATH))
    assert stage_reads == {"hop_length", "n_fft", "wave_smooth_beats", "phrase_beats",
                           "bar_beats", "section_min_seconds"}
    assert _frontend_cfg_reads() == {"sr"}
    assert set(fork_stage_cache.STAGE3_ANALYSIS_CONFIG_FIELDS) == stage_reads | {"sr"}


def test_drift_no_stage4_field_is_represented():
    """The other direction: an over-broad identity would needlessly invalidate the artifact."""
    stage4_only = {"low_energy_min_interval", "medium_energy_min_interval",
                   "high_energy_min_interval", "peak_energy_min_interval",
                   "low_energy_max_hold", "medium_energy_max_hold", "high_energy_max_hold",
                   "peak_energy_max_hold", "enable_rare_micro_cuts", "max_micro_cut_ratio",
                   "micro_min_gap", "micro_percentile", "target_cut_ratio_min",
                   "target_cut_ratio_max", "enable_video_analysis", "enable_qwen_semantics",
                   "qwen_model_path", "anchor_bonus", "phrase_bonus"}
    assert stage4_only.isdisjoint(set(fork_stage_cache.STAGE3_ANALYSIS_CONFIG_FIELDS))
    # and those fields really are Stage-4's, i.e. unread by Stages 1-3
    assert stage4_only.isdisjoint(
        _cfg_reads(_STAGE1_PATH) | _cfg_reads(_STAGE2_PATH) | _cfg_reads(_STAGE3_PATH))


def test_drift_the_real_config_class_exposes_every_identity_field(pipeline):
    """The identity helper runs against the real ``AutoWaveConfig`` here, not a test double."""
    cfg = pipeline["CONFIG"]
    identity = fork_stage_cache.analysis_config_identity(cfg)
    assert [name for name, _ in identity] == list(
        fork_stage_cache.STAGE3_ANALYSIS_CONFIG_FIELDS)
    assert dict(identity) == {
        "sr": 22050, "hop_length": 512, "n_fft": 2048, "wave_smooth_beats": 16,
        "phrase_beats": 8, "bar_beats": 4, "section_min_seconds": 10.0}


def test_the_pipelines_own_trim_rule_is_what_the_key_receives():
    """§9's effective window. The end-time rule stays the pipeline's; this proves the real inline
    expression and asserts the key is built from its result, so there is no second trim contract."""
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_AUTO_MODE_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "analyze_beats_auto")
    body = "\n".join(ast.unparse(s) for s in fn.body)
    assert "duration = None" in body
    assert "if end_time and end_time > start_time:" in body
    assert "duration = end_time - start_time" in body
    # the key is handed `duration`, i.e. the already-resolved effective value
    key_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "_stage3_cache_key"]
    assert len(key_calls) == 1
    assert [ast.unparse(a) for a in key_calls[0].args] == [
        "audio_file", "start_time", "duration", "cfg", "use_gpu"]

    # and the rule itself behaves as documented
    def effective(start_time, end_time):
        duration = None
        if end_time and end_time > start_time:
            duration = end_time - start_time
        return duration

    assert effective(0.0, None) is None
    assert effective(0.0, 0) is None
    assert effective(10.0, 5.0) is None
    assert effective(10.0, 10.0) is None
    assert effective(10.0, 40.0) == 30.0


# ===========================================================================
# §33  THE ORCHESTRATION: MISS THEN HIT
# ===========================================================================


def test_first_invocation_runs_everything_and_publishes_once(pipeline, track):
    selected, info = _run(pipeline, track)
    calls = pipeline["_CALLS"]

    assert calls["load"] == 1
    assert calls["normalize"] == 1
    assert calls["hpss"] == 1
    assert calls["stage1"] == 1
    assert calls["stage2"] == 1
    assert calls["stage3"] == 1
    assert calls["stage4"] == 1
    assert calls["cache_get"] == 1 and calls["cache_hit"] == 0
    assert calls["cache_put"] == 1, "published exactly once, after Stage 3"
    assert selected.size > 0 and info["sections"]


def test_second_invocation_for_the_same_key_skips_the_front_end_and_stages_1to3(pipeline, track):
    _run(pipeline, track)
    calls = pipeline["_CALLS"]
    baseline = dict(calls.counts)

    _run(pipeline, track)

    assert calls["cache_hit"] == 1, "the second call must hit"
    for skipped in ("load", "normalize", "hpss", "stage1", "stage2", "stage3"):
        assert calls[skipped] == baseline[skipped], f"{skipped} must not run again"
    assert calls["stage4"] == baseline["stage4"] + 1, "Stage 4 always reruns"
    assert calls["cache_put"] == baseline["cache_put"], "a hit must not republish"


def test_a_hit_is_served_across_separate_namespaces_sharing_the_process(track):
    """Process-local, not call-local: the second *caller* benefits, which is what makes C3
    candidate 2 hit with no C3 code change at all."""
    first = _build()
    _run(first, track)
    second = _build()
    _run(second, track)

    assert second["_CALLS"]["cache_hit"] == 1
    assert second["_CALLS"]["stage1"] == 0
    assert second["_CALLS"]["stage4"] == 1


# ===========================================================================
# §34 / §35  OUTPUT PARITY
# ===========================================================================


def test_the_miss_path_reproduces_base_behaviour_exactly(track):
    """A differential test against the authorized base, not an assertion about the base.

    Both revisions' real ``analyze_beats_auto`` bodies run against identical deterministic stubs;
    every output this milestone promises must match. The cache wraps the computation, so a first
    render must be behaviourally indistinguishable from pre-L2 main.
    """
    base = _build(_BASE_SHA)
    base_out = _collect(*_run(base, track))
    assert base["_CALLS"]["cache_get"] == 0, "the base revision has no cache seam"

    fork_stage_cache.STAGE3_CACHE.clear()
    head = _build()
    head_out = _collect(*_run(head, track))
    assert head["_CALLS"]["cache_hit"] == 0, "this must be the miss path"

    _assert_same(base_out, head_out, label="miss-vs-base ")


@pytest.mark.parametrize("kwargs", [
    {},
    {"start_time": 5.0},
    {"start_time": 2.0, "end_time": 40.0},
    {"creative": {"cut_density": 70, "micro_cuts": 80, "seed": 404}},
    {"freestyle": None},
])
def test_the_miss_path_matches_base_across_input_shapes(track, kwargs):
    base_out = _collect(*_run(_build(_BASE_SHA), track, **kwargs))
    fork_stage_cache.STAGE3_CACHE.clear()
    head_out = _collect(*_run(_build(), track, **kwargs))
    _assert_same(base_out, head_out, label=f"{kwargs} ")


def test_a_cache_hit_is_exactly_equal_to_the_miss_that_produced_it(pipeline, track):
    miss = _collect(*_run(pipeline, track))
    hit = _collect(*_run(pipeline, track))
    assert pipeline["_CALLS"]["cache_hit"] == 1
    _assert_same(miss, hit, label="hit-vs-miss ")


def test_a_hit_hands_out_an_isolated_bundle_so_the_next_hit_is_unspoiled(pipeline, track):
    """The defensive-copy contract, observed through the pipeline rather than the cache object:
    downstream legitimately mutates `beat_info`, and that must not corrupt a later hit."""
    first_selected, first_info = _run(pipeline, track)
    reference = copy.deepcopy(_collect(first_selected, first_info))

    second_selected, second_info = _run(pipeline, track)
    # vandalise everything a downstream consumer could reach
    second_info["sections"][0]["type"] = "vandalised"
    second_info["sections"].append({"index": 999, "type": "appended"})
    second_info["times"][0] = -1.0
    second_info["energy_profile"]["wave"][0] = -1.0

    third = _collect(*_run(pipeline, track))
    _assert_same(reference, third, label="third-vs-first ")


# ===========================================================================
# §36 / §37  WHAT A CREATIVE CHANGE DOES AND DOES NOT INVALIDATE
# ===========================================================================


_STAGE6_CONTROLS = ("seed", "semantic_emphasis", "energy_response", "motion_bias",
                    "source_diversity")
_STAGE4_CONTROLS = ("cut_density", "micro_cuts")


@pytest.mark.parametrize("control", _STAGE6_CONTROLS)
def test_a_stage6_control_change_hits_stage3_and_still_reruns_stage4(pipeline, track, control):
    first = _collect(*_run(pipeline, track))
    calls = pipeline["_CALLS"]
    baseline = dict(calls.counts)

    value = 404 if control == "seed" else 80
    second_selected, second_info = _run(pipeline, track, creative={control: value})

    assert calls["cache_hit"] == 1, f"{control} must not invalidate the Stage-3 artifact"
    for skipped in ("load", "normalize", "hpss", "stage1", "stage2", "stage3"):
        assert calls[skipped] == baseline[skipped], skipped
    assert calls["stage4"] == baseline["stage4"] + 1

    # the Stage 1-3 facts are the cached ones, exactly
    for fact in _FACTS:
        _assert_same(first[fact], _collect(second_selected, second_info)[fact], label=f"{control} ")
    assert second_info["creative"][control] == value


@pytest.mark.parametrize("control,value", [("cut_density", 100), ("cut_density", 0),
                                           ("micro_cuts", 100), ("micro_cuts", 0)])
def test_a_stage4_control_change_hits_stage3_and_reruns_stage4(pipeline, track, control, value):
    """Stage 4 is deliberately uncached in V1 — measured ~7.6 ms — so a Cut Density or Micro Cuts
    change reuses the ~15.7 s of Stage 1-3 work and recomputes only the selection."""
    first = _collect(*_run(pipeline, track))
    calls = pipeline["_CALLS"]
    baseline = dict(calls.counts)

    second_selected, second_info = _run(pipeline, track, creative={control: value})

    assert calls["cache_hit"] == 1
    for skipped in ("load", "normalize", "hpss", "stage1", "stage2", "stage3"):
        assert calls[skipped] == baseline[skipped], skipped
    assert calls["stage4"] == baseline["stage4"] + 1, "Stage 4 must always execute"

    second = _collect(second_selected, second_info)
    for fact in _FACTS:
        _assert_same(first[fact], second[fact], label=f"{control}={value} ")


def test_cut_density_actually_changes_the_stage4_output_on_a_cache_hit(pipeline, track):
    """The hit must not accidentally freeze Stage 4's answer along with its inputs."""
    neutral = _collect(*_run(pipeline, track))
    dense = _collect(*_run(pipeline, track, creative={"cut_density": 100}))
    assert pipeline["_CALLS"]["cache_hit"] == 1
    assert len(dense["selected_beats"]) != len(neutral["selected_beats"]), (
        "Stage 4 output should differ at a different density")


def test_every_creative_control_reaches_one_identical_stage3_key(pipeline, track):
    """§31: the seven controls are resolved before the lookup and must not enter the key."""
    cfg = pipeline["CONFIG"]
    base_key = fork_stage_cache.stage3_cache_key(track, 0.0, None, cfg, False)
    assert base_key is not None

    _run(pipeline, track)
    calls = pipeline["_CALLS"]
    for control in (*_STAGE4_CONTROLS, *_STAGE6_CONTROLS):
        for value in (0, 25, 75, 100, 7777):
            before = calls["cache_hit"]
            _run(pipeline, track, creative={control: value})
            assert calls["cache_hit"] == before + 1, f"{control}={value} missed"
    assert calls["stage1"] == 1, "Stage 1 must have run exactly once, for the first miss"


def test_a_whole_preset_recipe_reaches_the_same_stage3_key(pipeline, track):
    _run(pipeline, track)
    calls = pipeline["_CALLS"]
    for name in fork_presets.PRESETS:
        before = calls["cache_hit"]
        _run(pipeline, track, creative=fork_presets.resolve_preset(name))
        assert calls["cache_hit"] == before + 1, name
    assert calls["stage1"] == 1


# ===========================================================================
# §32  FREESTYLE ISOLATION
# ===========================================================================


def _declaration(styles, enabled=True):
    return fork_freestyle.FreestyleDeclaration.from_styles(enabled, styles)


def test_every_freestyle_declaration_reuses_the_stage3_artifact(pipeline, track):
    """Freestyle is resolved *after* Stage 3, so no declaration can invalidate the artifact. That
    says nothing about Stage 4, which still reruns and may legitimately differ."""
    declarations = [
        None,
        fork_freestyle.FreestyleDeclaration(),
        _declaration({}),
        _declaration({"intro": fork_freestyle.BASE_STYLE, "drop": fork_freestyle.BASE_STYLE}),
        _declaration({"drop": "High Energy"}),
        _declaration({"intro": "Cinematic", "drop": "High Energy", "verse": "Dynamic"}),
    ]
    _run(pipeline, track)
    calls = pipeline["_CALLS"]

    for declaration in declarations:
        before = calls["cache_hit"]
        _run(pipeline, track, freestyle=declaration)
        assert calls["cache_hit"] == before + 1, repr(declaration)
    assert calls["stage1"] == 1, "Stage 1-3 ran exactly once for every declaration"
    assert calls["stage4"] == 1 + len(declarations), "Stage 4 reran every time"


def test_a_uniform_non_base_freestyle_density_still_hits_stage3(pipeline, track):
    """The uniform-density path reaches Stage 4 with a different config and still reuses Stage 3."""
    first = _collect(*_run(pipeline, track))
    sections = {str(s["type"]) for s in first["sections"]}
    uniform = _declaration({kind: "High Energy" for kind in sections
                            if kind in fork_freestyle.SECTION_TYPES})

    selected, info = _run(pipeline, track, freestyle=uniform)
    assert pipeline["_CALLS"]["cache_hit"] == 1
    for fact in _FACTS:
        _assert_same(first[fact], _collect(selected, info)[fact], label="uniform ")


def test_the_stage3_key_is_built_before_freestyle_can_influence_anything(pipeline):
    """Structural: the lookup takes the audio, the window, the config and the GPU request — and
    neither the resolved profile nor the declaration."""
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_AUTO_MODE_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "analyze_beats_auto")
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "_stage3_cache_key")
    rendered = ast.unparse(call)
    for banned in ("profile", "freestyle", "creative", "video_files", "qwen",
                   "enable_video_analysis", "event_callback"):
        assert banned not in rendered, f"{banned} must not reach the Stage-3 key: {rendered}"


# ===========================================================================
# §38 / §39  WHAT MUST MISS
# ===========================================================================


def test_changed_audio_content_misses(pipeline, track):
    """Exact-mtime restore with different bytes: the content fingerprint is what catches this."""
    _run(pipeline, track)
    calls = pipeline["_CALLS"]
    baseline = dict(calls.counts)

    with open(track, "wb") as handle:
        handle.write(b"ID3" + b"\x01" * 8192)
    os.utime(track, ns=(_SECOND, _SECOND))

    _run(pipeline, track)
    assert calls["cache_hit"] == 0, "stale Stage 1-3 facts must not be reused"
    assert calls["stage1"] == baseline["stage1"] + 1
    assert calls["stage3"] == baseline["stage3"] + 1


def test_a_moved_track_misses(pipeline, track, tmp_path):
    _run(pipeline, track)
    moved = str(tmp_path / "elsewhere" / os.path.basename(track))
    os.makedirs(os.path.dirname(moved), exist_ok=True)
    with open(track, "rb") as src, open(moved, "wb") as dst:
        dst.write(src.read())
    os.utime(moved, ns=(_SECOND, _SECOND))

    _run(pipeline, moved)
    assert pipeline["_CALLS"]["cache_hit"] == 0, "identity is location + content"


@pytest.mark.parametrize("kwargs", [
    {"start_time": 5.0},
    {"end_time": 40.0},
    {"start_time": 5.0, "end_time": 40.0},
    {"start_time": 5.0, "end_time": 50.0},
])
def test_a_different_effective_window_misses(pipeline, track, kwargs):
    _run(pipeline, track)
    before = pipeline["_CALLS"]["stage1"]
    _run(pipeline, track, **kwargs)
    assert pipeline["_CALLS"]["cache_hit"] == 0, kwargs
    assert pipeline["_CALLS"]["stage1"] == before + 1


@pytest.mark.parametrize("kwargs", [
    {"end_time": None},
    {"end_time": 0},
    {"end_time": 0.0},
])
def test_an_unbounded_or_degenerate_end_time_still_hits(pipeline, track, kwargs):
    """These produce the same effective window as the default, so they must reuse."""
    _run(pipeline, track)
    _run(pipeline, track, **kwargs)
    assert pipeline["_CALLS"]["cache_hit"] == 1, kwargs


@pytest.mark.parametrize("field,value", [
    ("sr", 44100), ("hop_length", 256), ("n_fft", 4096), ("wave_smooth_beats", 8),
    ("phrase_beats", 16), ("bar_beats", 3), ("section_min_seconds", 5.0),
])
def test_a_changed_stage1to3_config_field_misses(pipeline, track, field, value):
    from dataclasses import replace as dc_replace

    _run(pipeline, track)
    before = pipeline["_CALLS"]["stage1"]
    pipeline["CONFIG"] = dc_replace(pipeline["CONFIG"], **{field: value})

    _run(pipeline, track)
    assert pipeline["_CALLS"]["cache_hit"] == 0, field
    assert pipeline["_CALLS"]["stage1"] == before + 1


def test_a_changed_use_gpu_request_misses(pipeline, track):
    """Conservative: Stage 2 has a CPU/CuPy branch and no byte-exact parity contract exists."""
    _run(pipeline, track, use_gpu=False)
    before = pipeline["_CALLS"]["stage2"]
    _run(pipeline, track, use_gpu=True)
    assert pipeline["_CALLS"]["cache_hit"] == 0
    assert pipeline["_CALLS"]["stage2"] == before + 1


def test_an_unreadable_track_disables_the_cache_rather_than_failing(tmp_path):
    """No identity means the cache is simply unavailable, and the render takes the uncached path."""
    namespace = _build()
    missing = str(tmp_path / "never-existed.mp3")
    selected, info = _run(namespace, missing)

    calls = namespace["_CALLS"]
    assert calls["cache_get"] == 1 and calls["cache_hit"] == 0
    assert calls["cache_put"] == 1, "put is still attempted; a None key makes it a no-op"
    assert calls["stage1"] == 1 and selected.size > 0

    # and nothing was stored under a fabricated identity
    assert fork_stage_cache.STAGE3_CACHE.has_entry() is False
    _run(namespace, missing)
    assert calls["cache_hit"] == 0, "an unprovable track must never produce a hit"
    assert calls["stage1"] == 2


# ===========================================================================
# §40  A FAILED COMPUTATION MUST NOT POISON THE CACHE
# ===========================================================================


@pytest.mark.parametrize("failing", ["stage2", "stage3"])
def test_a_failed_stage_publishes_nothing(track, failing):
    broken = _build(fail_stage=failing)
    with pytest.raises(RuntimeError, match="exploded"):
        _run(broken, track)

    assert broken["_CALLS"]["cache_put"] == 0, "nothing may be published before Stage 3 succeeds"
    assert fork_stage_cache.STAGE3_CACHE.has_entry() is False

    # a later successful call recomputes rather than inheriting a partial result
    healthy = _build()
    _run(healthy, track)
    assert healthy["_CALLS"]["cache_hit"] == 0
    assert healthy["_CALLS"]["stage3"] == 1
    assert healthy["_CALLS"]["cache_put"] == 1


def test_a_failed_stage1_keeps_its_existing_exception(track):
    broken = _build(fail_stage="stage1")
    with pytest.raises(RuntimeError, match="exploded"):
        _run(broken, track)
    assert broken["_CALLS"]["cache_put"] == 0
    assert fork_stage_cache.STAGE3_CACHE.has_entry() is False


def test_a_failed_audio_load_keeps_its_existing_exception(track):
    """L2 must not weaken a librosa failure into a cache miss with a different error."""
    broken = _build(fail_load="load")
    with pytest.raises(RuntimeError, match="load exploded"):
        _run(broken, track)
    assert broken["_CALLS"]["cache_put"] == 0
    assert fork_stage_cache.STAGE3_CACHE.has_entry() is False


def test_the_empty_audio_guard_still_raises_its_own_valueerror(track):
    namespace = _build(seconds=0.0)
    with pytest.raises(ValueError):
        _run(namespace, track)
    assert namespace["_CALLS"]["cache_put"] == 0


def test_a_failure_after_a_successful_publication_leaves_the_good_entry(track):
    """A later broken call must not evict a healthy artifact, because it never publishes."""
    healthy = _build()
    good = _collect(*_run(healthy, track))

    broken = _build(fail_stage="stage3")
    # the broken namespace hits the cache before it can fail, so force a miss by changing the window
    with pytest.raises(RuntimeError):
        _run(broken, track, start_time=7.0)

    again = _build()
    reused = _collect(*_run(again, track))
    assert again["_CALLS"]["cache_hit"] == 1
    _assert_same(good, reused, label="after-failure ")


# ===========================================================================
# §41  CACHE MACHINERY FAILURE FAILS OPEN — for an ORDINARY failure
#
# R2 made the boundary two-class, because R1's "`Exception` only, so `MemoryError` propagates" was
# simply false in Python: `MemoryError` subclasses `Exception`, so a lone `except Exception`
# swallowed it. Both halves are pinned permanently below.
#
#   ordinary Exception  ->  fail open   (no cache / miss / reuse lost)
#   MemoryError         ->  propagate
#
# `KeyboardInterrupt` and `SystemExit` propagate for an unrelated reason — they are `BaseException`,
# not `Exception` — and nothing in the seam catches `BaseException`.
# ===========================================================================


def test_memoryerror_is_an_exception_subclass_which_is_why_r2_exists():
    """The premise, stated mechanically so the rest of this section cannot be misread.

    `except Exception` is NOT a filter that lets memory exhaustion through. Anyone tempted to
    collapse the two handlers back into one should read this first.
    """
    assert issubclass(MemoryError, Exception) is True
    assert issubclass(KeyboardInterrupt, Exception) is False
    assert issubclass(SystemExit, Exception) is False

    caught = None
    try:
        raise MemoryError("exhausted")
    except Exception as exc:          # noqa: BLE001 - demonstrating the hazard on purpose
        caught = type(exc)
    assert caught is MemoryError, "a bare `except Exception` swallows MemoryError"


def test_a_broken_key_builder_falls_back_to_the_uncached_path(track, monkeypatch):
    namespace = _build()

    def exploding(*_args, **_kwargs):
        raise RuntimeError("key builder exploded")

    monkeypatch.setattr(fork_stage_cache, "stage3_cache_key", exploding)
    selected, info = _run(namespace, track)

    assert namespace["_CALLS"]["stage1"] == 1
    assert namespace["_CALLS"]["stage4"] == 1
    assert selected.size > 0 and info["sections"]


def test_a_broken_lookup_falls_back_to_the_uncached_path(track, monkeypatch):
    namespace = _build()
    _run(namespace, track)

    class _BrokenGet:
        def get(self, _key):
            raise RuntimeError("get exploded")

        def put(self, _key, _value):
            return None

    monkeypatch.setattr(fork_stage_cache, "STAGE3_CACHE", _BrokenGet())
    before = namespace["_CALLS"]["stage1"]
    selected, _info = _run(namespace, track)

    assert namespace["_CALLS"]["stage1"] == before + 1, "a broken get must recompute, not fail"
    assert selected.size > 0


def test_a_broken_store_loses_reuse_but_not_the_current_render(track, monkeypatch):
    namespace = _build()

    class _BrokenPut:
        def get(self, _key):
            return None

        def put(self, _key, _value):
            raise RuntimeError("put exploded")

        def has_entry(self):
            return False

    monkeypatch.setattr(fork_stage_cache, "STAGE3_CACHE", _BrokenPut())
    selected, info = _run(namespace, track)

    assert selected.size > 0 and info["sections"], "the current render must still complete"
    assert namespace["_CALLS"]["cache_put"] == 1

    second_selected, _ = _run(namespace, track)
    assert namespace["_CALLS"]["stage1"] == 2, "reuse is lost, correctness is not"
    assert np.array_equal(selected, second_selected)


def test_a_broken_deepcopy_falls_back_rather_than_sharing_state(track, monkeypatch):
    """If the isolation guarantee cannot be honoured, the answer is a miss — never a shared graph."""
    namespace = _build()
    _run(namespace, track)

    real_deepcopy = copy.deepcopy

    def exploding(value, memo=None):
        raise RuntimeError("deepcopy exploded")

    monkeypatch.setattr(fork_stage_cache.copy, "deepcopy", exploding)
    before = namespace["_CALLS"]["stage1"]
    selected, _info = _run(namespace, track)
    monkeypatch.setattr(fork_stage_cache.copy, "deepcopy", real_deepcopy)

    assert namespace["_CALLS"]["stage1"] == before + 1
    assert selected.size > 0


def test_an_incomplete_stored_bundle_degrades_to_a_miss(track):
    """A malformed artifact must not enter the pipeline as half a result."""
    namespace = _build()
    _run(namespace, track)
    key = fork_stage_cache.stage3_cache_key(
        track, 0.0, None, namespace["CONFIG"], False)
    assert key is not None

    fork_stage_cache.STAGE3_CACHE.put(key, {"tempo": 120.0})   # missing four fields
    before = namespace["_CALLS"]["stage1"]
    _run(namespace, track)

    assert namespace["_CALLS"]["cache_hit"] == 0
    assert namespace["_CALLS"]["stage1"] == before + 1


# ===========================================================================
# R2  MEMORY EXHAUSTION IS NOT AN ORDINARY OPTIMIZATION FAILURE
#
# Turning a failed allocation into a cache miss starts the ~15.7 s front end plus Stages 1-3 under
# memory pressure — doing substantially MORE work at the moment the process has least room for it.
# So `MemoryError` is re-raised ahead of the ordinary handler at all three seams.
# ===========================================================================


_SEAM_WRAPPERS = ("_stage3_cache_key", "_stage3_cache_get", "_stage3_cache_put")


class _MemoryErrorCache:
    """A cache whose chosen operation exhausts memory; every other operation is ordinary."""

    def __init__(self, failing: str):
        self.failing = failing
        self.calls = {"get": 0, "put": 0}

    def get(self, _key):
        self.calls["get"] += 1
        if self.failing == "get":
            raise MemoryError("cannot allocate the bundle copy")
        return None

    def put(self, _key, _value):
        self.calls["put"] += 1
        if self.failing == "put":
            raise MemoryError("cannot allocate the bundle copy")
        return None

    def has_entry(self):
        return False


def test_memoryerror_from_the_key_builder_propagates(track, monkeypatch):
    """No fallback: the render stops rather than beginning the expensive uncached path."""
    namespace = _build()

    def exhausted(*_args, **_kwargs):
        raise MemoryError("cannot allocate the key")

    monkeypatch.setattr(fork_stage_cache, "stage3_cache_key", exhausted)
    with pytest.raises(MemoryError, match="cannot allocate the key"):
        _run(namespace, track)

    calls = namespace["_CALLS"]
    assert calls["stage1"] == 0, "Stage 1 must not begin as a fallback"
    assert calls["load"] == 0 and calls["hpss"] == 0
    assert calls["stage4"] == 0


def test_memoryerror_from_the_lookup_propagates_and_starts_no_analysis(track, monkeypatch):
    """**The load-bearing regression case.**

    The hit path's deep copy is where an allocation is most likely to fail. R1 answered "miss" there
    and then ran the whole front end plus Stages 1-3 — the single most expensive thing it could do
    with no memory. Nothing may be recomputed.
    """
    namespace = _build()
    broken = _MemoryErrorCache("get")
    monkeypatch.setattr(fork_stage_cache, "STAGE3_CACHE", broken)

    with pytest.raises(MemoryError, match="cannot allocate the bundle copy"):
        _run(namespace, track)

    calls = namespace["_CALLS"]
    assert broken.calls["get"] == 1, "the lookup must have been attempted"
    assert calls["stage1"] == 0, "Stage 1 must NOT execute as a fallback"
    assert calls["stage2"] == 0, "Stage 2 must NOT execute"
    assert calls["stage3"] == 0, "Stage 3 must NOT execute"
    assert calls["load"] == 0 and calls["normalize"] == 0 and calls["hpss"] == 0
    assert calls["stage4"] == 0
    assert broken.calls["put"] == 0, "nothing may be published after a failed lookup"


def test_memoryerror_from_the_store_propagates(track, monkeypatch):
    """The one case where the current render is deliberately *not* protected.

    An ordinary store failure costs the next call its reuse and nothing else. A store failing for
    want of memory says the process is out of memory, and reporting a completed render while
    continuing into Stage 4 and a full FFmpeg render would claim a success the machine cannot
    deliver. This test deliberately does **not** assert the current render completes.
    """
    namespace = _build()
    broken = _MemoryErrorCache("put")
    monkeypatch.setattr(fork_stage_cache, "STAGE3_CACHE", broken)

    with pytest.raises(MemoryError, match="cannot allocate the bundle copy"):
        _run(namespace, track)

    calls = namespace["_CALLS"]
    # the miss path ran in full — the store is the last statement of that branch
    assert calls["load"] == 1 and calls["hpss"] == 1
    assert calls["stage1"] == 1 and calls["stage2"] == 1 and calls["stage3"] == 1
    assert broken.calls["put"] == 1
    assert calls["stage4"] == 0, "the exception must stop the render before Stage 4"


def test_a_memoryerror_raised_by_the_real_deepcopy_propagates(track, monkeypatch):
    """Through the *real* cache object rather than a stand-in, so the seam is what is under test.

    `test_a_broken_deepcopy_falls_back_rather_than_sharing_state` above pins the ordinary half with
    a `RuntimeError`; this is the same injection point with the exception that must not be absorbed.
    """
    namespace = _build()
    _run(namespace, track)
    baseline = dict(namespace["_CALLS"].counts)

    def exhausted(_value, memo=None):
        raise MemoryError("deepcopy out of memory")

    monkeypatch.setattr(fork_stage_cache.copy, "deepcopy", exhausted)
    with pytest.raises(MemoryError, match="deepcopy out of memory"):
        _run(namespace, track)

    calls = namespace["_CALLS"]
    assert calls["stage1"] == baseline["stage1"], "no recomputation under memory pressure"
    assert calls["stage3"] == baseline["stage3"]


def test_memoryerror_does_not_poison_the_entry_and_a_later_call_still_hits(track, monkeypatch):
    """Recovery: once memory is available again, the artifact stored before the failure is reusable."""
    namespace = _build()
    _run(namespace, track)                      # populates the entry
    assert namespace["_CALLS"]["cache_put"] == 1

    def exhausted(_value, memo=None):
        raise MemoryError("transient exhaustion")

    monkeypatch.setattr(fork_stage_cache.copy, "deepcopy", exhausted)
    with pytest.raises(MemoryError):
        _run(namespace, track)
    monkeypatch.undo()

    later = _build()
    _run(later, track)
    assert later["_CALLS"]["cache_hit"] == 1, "the surviving entry must still be reusable"
    assert later["_CALLS"]["stage1"] == 0


@pytest.mark.parametrize("name", _SEAM_WRAPPERS)
def test_the_seam_re_raises_memoryerror_before_the_ordinary_handler(name):
    """Structural, so a later "simplification" back to one broad `except Exception` cannot pass.

    Order matters as much as presence: `except Exception` placed first would shadow the
    `MemoryError` clause entirely, which is exactly the defect R2 corrects.
    """
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_AUTO_MODE_PATH)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
    assert tries, f"{name} must still have an exception boundary"

    for node in tries:
        kinds = [ast.unparse(h.type) if h.type else "<bare>" for h in node.handlers]
        assert "<bare>" not in kinds, f"{name}: bare except"
        assert "BaseException" not in kinds, f"{name}: must not catch BaseException"
        assert "MemoryError" in kinds, f"{name}: no explicit MemoryError clause ({kinds})"
        assert "Exception" in kinds, f"{name}: ordinary fail-open handler is missing ({kinds})"
        assert kinds.index("MemoryError") < kinds.index("Exception"), (
            f"{name}: MemoryError must be handled BEFORE Exception, else it is shadowed: {kinds}")

        memory_handler = node.handlers[kinds.index("MemoryError")]
        assert [type(stmt) for stmt in memory_handler.body] == [ast.Raise], (
            f"{name}: the MemoryError clause must re-raise, not absorb: "
            f"{[ast.unparse(s) for s in memory_handler.body]}")
        assert memory_handler.body[0].exc is None, (
            f"{name}: re-raise the original exception, do not construct a new one")


def test_no_seam_claims_that_except_exception_lets_memoryerror_through():
    """R1's docstrings asserted "`Exception` only - a `KeyboardInterrupt` or a `MemoryError` must
    still reach the caller", which is false for `MemoryError`. The claim must not come back."""
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_AUTO_MODE_PATH)
    for name in _SEAM_WRAPPERS:
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        doc = (ast.get_docstring(fn) or "").replace("\n", " ")
        normalised = " ".join(doc.split()).lower()
        assert "`exception` only - a `keyboardinterrupt` or a `memoryerror`" not in normalised, (
            f"{name}: the retired false claim is back")


# ===========================================================================
# §20 / §21  TRUTHFUL PROGRESS AND CONSOLE ON A HIT
# ===========================================================================


def test_a_hit_still_reports_stages_1_2_and_3(pipeline, track):
    _run(pipeline, track)

    events = []
    legacy = []
    _run(pipeline, track,
         event_callback=events.append,
         progress_callback=legacy.append)

    assert pipeline["_CALLS"]["cache_hit"] == 1
    for stage in (1, 2, 3):
        kinds = [e.kind.value for e in events if e.stage == stage]
        assert kinds == ["start", "end"], f"stage {stage}: {kinds}"
        for event in (e for e in events if e.stage == stage):
            assert event.data.get("cached") is True, f"stage {stage} must declare itself cached"
            assert event.elapsed_seconds is None, (
                f"stage {stage} must not invent a fresh Stage 1-3 timing")
    # Stage 4 is real work and still reports a real elapsed time
    stage4_end = next(e for e in events if e.stage == 4 and e.kind.value == "end")
    assert stage4_end.elapsed_seconds is not None
    assert stage4_end.data.get("cached") is None

    assert len(legacy) >= 4, "the legacy callback must still walk stages 1-4"
    assert "Stage 1" in legacy[0] and "Stage 4" in legacy[3]


def test_cached_events_carry_the_cached_facts(pipeline, track):
    miss_events = []
    _run(pipeline, track, event_callback=miss_events.append)
    hit_events = []
    _run(pipeline, track, event_callback=hit_events.append)

    def end(events, stage):
        return next(e for e in events if e.stage == stage and e.kind.value == "end")

    assert end(hit_events, 1).data["beats"] == end(miss_events, 1).data["beats"]
    assert end(hit_events, 1).data["tempo"] == end(miss_events, 1).data["tempo"]
    assert end(hit_events, 2).data["average_wave"] == end(miss_events, 2).data["average_wave"]
    assert end(hit_events, 3).data["sections"] == end(miss_events, 3).data["sections"]
    assert end(hit_events, 3).data["section_types"] == end(miss_events, 3).data["section_types"]


def test_cached_messages_say_reusing_and_never_claim_fresh_work(pipeline, track):
    _run(pipeline, track)
    events = []
    _run(pipeline, track, event_callback=events.append)

    starts = {e.stage: e.message for e in events if e.kind.value == "start" and e.stage in (1, 2, 3)}
    assert starts[1] == "Reusing cached beat grid"
    assert starts[2] == "Reusing cached energy and rhythm features"
    assert starts[3] == "Reusing cached musical sections"
    for stage in (1, 2, 3):
        assert "(cached)" in next(
            e.message for e in events if e.stage == stage and e.kind.value == "end")


def test_the_console_line_claims_only_what_is_true(pipeline, track, capsys):
    _run(pipeline, track)
    capsys.readouterr()
    _run(pipeline, track)
    out = capsys.readouterr().out

    assert "Reusing process-local audio analysis cache (Stages 1-3)" in out
    lowered = out.lower()
    for overclaim in ("disk cache", "persistent", "stage 5", "stage 6", "saved to",
                      "written to"):
        assert overclaim not in lowered, overclaim
    # and it does not pretend the front end ran
    assert "Loading audio" not in out
    assert "Step 1: Detecting stable beat grid" not in out


def test_the_miss_path_keeps_its_original_events(pipeline, track):
    """A miss must look exactly as it always did — real timings, no `cached` marker."""
    events = []
    _run(pipeline, track, event_callback=events.append)
    for stage in (1, 2, 3):
        for event in (e for e in events if e.stage == stage):
            assert event.data.get("cached") is None, stage
        assert next(e for e in events if e.stage == stage
                    and e.kind.value == "end").elapsed_seconds is not None


# ===========================================================================
# §22 / §23 / §24  SCOPE BOUNDARIES
# ===========================================================================


def test_no_stage4_cache_was_invented():
    """V1 caches the post-Stage-3 artifact and nothing else. Stage 4 measured ~7.6 ms, so the
    resolved-Stage-4-key design stays documentation-only future work."""
    with open(_AUTO_MODE_PATH, "r", encoding="utf-8") as handle:
        auto_mode = handle.read()
    with open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "stage_cache.py"),
              "r", encoding="utf-8") as handle:
        module = handle.read()
    for banned in ("Stage4CacheKey", "stage4_cache", "STAGE4_CACHE",
                   "Stage6CacheKey", "stage6_cache", "STAGE6_CACHE",
                   "plan_cache", "PLAN_CACHE"):
        assert banned not in auto_mode, banned
        assert banned not in module, banned


def test_stage5_and_the_renderer_were_not_modified():
    """L2 must know nothing about video files, the candidate library, Qwen or the Stage-5 contract."""
    changed = subprocess.run(
        ["git", "diff", "--name-only", _BASE_SHA, "HEAD"],
        capture_output=True, text=True, encoding="utf-8", check=True,
        cwd=_REPO_ROOT).stdout.split()
    for untouchable in ("src/video_analysis.py", "src/video_processor.py", "src/gui.py",
                        "src/ui_content.py", "src/auto_mode/stage1_audio.py",
                        "src/auto_mode/stage2_features.py", "src/auto_mode/stage3_sections.py",
                        "src/auto_mode/stage4_select.py", "src/auto_mode/stage6_av_planner.py",
                        "src/auto_mode/stage5_qwen_scene_worker.py",
                        "src/beatsync_fork/variant_lab.py",
                        "src/beatsync_fork/variant_batch.py",
                        "src/beatsync_fork/render_batch.py",
                        "src/beatsync_fork/director.py"):
        assert untouchable not in changed, f"{untouchable} must not be modified"


def test_a_source_library_change_cannot_invalidate_the_audio_artifact(pipeline, track):
    """The two caches are independent: video sources are absent from the Stage-3 key."""
    _run(pipeline, track, video_files=None)
    _run(pipeline, track, video_files=["/lib/a.mp4", "/lib/b.mp4"], enable_video_analysis=False)
    assert pipeline["_CALLS"]["cache_hit"] == 1
    _run(pipeline, track, video_files=["/lib/completely", "/lib/different"],
         enable_video_analysis=False)
    assert pipeline["_CALLS"]["cache_hit"] == 2
    assert pipeline["_CALLS"]["stage1"] == 1


def test_stage6_is_never_cached(pipeline, track):
    """Every hit still rebuilds the audio-visual profile Stage 6 reads."""
    first = _run(pipeline, track)[1]
    second = _run(pipeline, track)[1]
    assert pipeline["_CALLS"]["cache_hit"] == 1
    assert first["audio_visual_profile"] is not second["audio_visual_profile"]
    assert first["creative"] is not second["creative"]
