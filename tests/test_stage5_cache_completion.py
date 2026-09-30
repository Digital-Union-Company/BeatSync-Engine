"""Stage 5 cache completion + writer behaviour (D1).

``video_analysis.py`` cannot be imported on a bare interpreter (it needs cv2/numpy/librosa and
mutates PATH), but the cache primitives D1 introduced are stdlib-only. So instead of asserting
*about* them with ``ast``, this suite lifts the real function definitions out of the production file
by their ``ast`` source ranges and executes them in a stdlib-only namespace. The bodies under test
are therefore provably the production bodies, and the assertions are behavioural rather than textual.

The orchestration wiring that *calls* these primitives needs the whole runtime and is pinned
structurally in ``test_stage5_cache_durability.py``.

Nothing here touches the real runtime cache: every path is inside pytest's ``tmp_path``.
"""

from __future__ import annotations

import ast
import json
import math
import os
import tempfile
import time
from typing import Any, Dict

import pytest

_VIDEO_ANALYSIS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "video_analysis.py")

# The exact set of cache primitives D1 owns. All stdlib-only by construction.
_FUNCS = ("_safe_name", "_hash_text", "_same_source", "_coerce_count", "_is_count",
          "_reported_count", "_stored_ai_cache_is_consistent", "_qwen_job_completed",
          "_deterministic_analysis_completed", "_cache_entry_is_complete",
          "_load_cache", "_save_cache", "_checkpoint_cache",
          # T1 telemetry-boundary helpers. Not cache primitives - they are here only because the
          # orchestration bodies extracted below call them, and an extracted body resolves every
          # name from this namespace. Exactly the transitive set those bodies need.
          # `_telemetry_total` is included as of R2: it is no longer aggregation-only, because the
          # orchestration bodies now use it for overflow-safe accumulation as well. The two genuinely
          # aggregation-only helpers (`_record_telemetry`, `_record_candidate_count`) are used solely
          # inside `analyze_video_sources`, which this suite does not extract, so they stay absent.
          "_as_mapping", "_bounded_count", "_is_nonnegative_count", "_is_real_number",
          "_optional_telemetry_number", "_telemetry_seconds", "_telemetry_text",
          "_telemetry_total")
_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_QWEN_COMPLETED_KEY",
           "_QWEN_SINGLE_JOB_ID", "_DETERMINISTIC_SCORING_KEY")


def _cache_contract_version() -> str:
    """Read CACHE_CONTRACT_VERSION straight out of production, so fixtures cannot drift from it."""
    tree = ast.parse(open(_VIDEO_ANALYSIS, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CACHE_CONTRACT_VERSION":
                    return ast.literal_eval(node.value)
    raise AssertionError("CACHE_CONTRACT_VERSION missing from video_analysis.py")


_CACHE_CONTRACT = _cache_contract_version()

# Functions that need a few pipeline collaborators stubbed. Their own bodies are still the real
# production bodies, so the Qwen-completion and candidate-analysis logic under test is genuine.
_PIPELINE_FUNCS = ("_fmt_seconds", "_annotate_candidates_with_qwen", "_analyze_single_video")


def _extract(names):
    """Return {name: verbatim source} for the requested top-level functions, plus the constants."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    found, consts = {}, {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _CONSTS:
                    consts[target.id] = ast.literal_eval(node.value)
    missing = [name for name in names if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"
    return found, consts


def _exec_into(namespace, found, order):
    for name in order:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    return namespace


def _load_primitives():
    """Exec the production cache definitions verbatim in an isolated stdlib-only namespace."""
    found, consts = _extract(_FUNCS)
    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile,
        "Any": Any, "Dict": Dict, "__builtins__": __builtins__,
    }
    namespace.update(consts)
    for name in _CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"
    return _exec_into(namespace, found, _FUNCS)


def _load_pipeline(worker_response, *, capture_opens=True, window_count=3, max_windows=None):
    """Exec the real `_analyze_single_video` / Qwen facade with their collaborators stubbed.

    Only the *collaborators* are fake (media metadata, OpenCV capture, window metrics, and the Qwen
    worker subprocess). The completion logic, the timings bookkeeping and the returned record shape
    are the production ones, so these tests exercise real behaviour rather than source text.
    """
    found, consts = _extract(_FUNCS + _PIPELINE_FUNCS)
    calls: Dict[str, Any] = {"merged": [], "worker_requests": 0}

    class _Capture:
        def isOpened(self):
            return capture_opens

        def release(self):
            calls["released"] = True

    def _run_qwen_worker(**kwargs):
        calls["worker_requests"] += 1
        return worker_response() if callable(worker_response) else worker_response

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile, "math": math, "time": time,
        "Any": Any, "Dict": Dict, "List": list, "Sequence": list,
        "__builtins__": __builtins__,
        # media + analysis collaborators
        "get_video_duration": lambda path: 30.0,
        "get_video_fps": lambda path: 25.0,
        "get_video_resolution": lambda path: (1280, 720),
        "detect_video_scene_changes": lambda *a, **k: [1.0, 5.0, 9.0],
        "_use_gpu_scene_detection": lambda use_gpu: False,
        "_use_gpu_candidate_metrics": lambda use_gpu: False,
        "_build_boundaries": lambda scene_changes, duration: [0.0, 10.0, 20.0, 30.0],
        "_make_candidate_windows": lambda boundaries, duration: [
            {"start": i * 2.0, "end": i * 2.0 + 2.0} for i in range(window_count)],
        "_open_video_capture": lambda path: _Capture(),
        "_measure_windows": lambda cap, fps, windows, use_gpu=False: [
            {"quality": 0.6} for _ in windows],
        "_build_candidate": lambda video_file, name, duration, i, window, metrics: {
            "id": f"{name}-{i}", "start": window["start"], "end": window["end"],
            "action_score": 0.5, "beauty_score": 0.4, "quality_score": 0.6,
            "editorial_score": 0.9 - i * 0.1, "tags": ["deterministic"]},
        # Qwen collaborators
        "_select_ai_candidates": lambda candidates, limit: list(candidates)[:limit],
        "_run_qwen_worker": _run_qwen_worker,
        # faithful to the real `_merge_semantic`, which also sets `ai_analyzed = True`
        "_merge_semantic": lambda candidate, semantic: (
            calls["merged"].append(candidate["id"]),
            candidate.update({"semantic_action": semantic.get("action", 0.0),
                              "ai_analyzed": True}))[1],
        "fork_progress": None,
    }
    namespace.update(consts)
    _exec_into(namespace, found, _FUNCS + _PIPELINE_FUNCS)
    if max_windows is None:
        os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)
    else:
        os.environ["BEATSYNC_QWEN_MAX_WINDOWS"] = str(max_windows)
    namespace["_calls"] = calls
    return namespace


def _semantic_records(count, prefix="clip.mp4"):
    return {f"{prefix}-{i}": {"action": 0.9, "emotion": "calm"} for i in range(count)}


def _worker_ok(frame_count=3, tag_count=None, semantics=None, job="single"):
    """A legacy-single worker response.

    Defaults to a *fully* successful job: every decoded frame item produced a semantic, which is what
    the worker's own `tag_count = len(semantics)` / `frame_count = len(frame_items)` pair means.
    """
    tag_count = frame_count if tag_count is None else tag_count
    return {
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b", "batch_size": 1,
        "peak_vram_gb": 3.0, "total_seconds": 12.0,
        "timings_by_job": {job: {"tag_count": tag_count, "frame_count": frame_count,
                                 "inference_seconds": 5.0, "prefetch_seconds": 0.1}},
        "semantics": _semantic_records(tag_count) if semantics is None else semantics,
    }


@pytest.fixture(scope="module")
def cache():
    return _load_primitives()


def _entry(av, *, video_file, candidates=None, ai_enabled=True, ai_deferred=False, **extra):
    """A cache payload. When it claims `ai_enabled=True` it is made *internally consistent* by
    default (R6): the Qwen counts and the per-candidate `ai_analyzed` markers agree, which is what a
    genuine AI-complete record written by the pipeline looks like. Tests that want an inconsistent
    stored record build it explicitly with `_stored(...)`."""
    entry = {
        "analysis_version": av,
        # D2: every record a run writes carries the cache contract, so fixtures must too
        "cache_contract": _CACHE_CONTRACT,
        "video_file": video_file,
        "source_name": os.path.basename(video_file),
        "duration": 30.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [1.0, 5.0],
        "candidates": [{"id": "c0", "start": 0.0, "end": 2.0, "action_score": 0.5,
                        "editorial_score": 0.5}] if candidates is None else candidates,
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0},
        "ai_enabled": ai_enabled,
        "ai_deferred": ai_deferred,
    }
    if ai_enabled and isinstance(entry["candidates"], list):
        usable = [c for c in entry["candidates"] if isinstance(c, dict) and isinstance(c.get("id"), str)]
        for candidate in usable:
            candidate["ai_analyzed"] = True
        if usable:
            entry["timings"]["qwen_frame_count"] = len(usable)
            entry["timings"]["qwen_tag_count"] = len(usable)
    entry["candidate_count"] = len(entry["candidates"])
    entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# the single completion rule
# ---------------------------------------------------------------------------


def test_deterministic_result_is_reusable_when_ai_is_not_required(cache):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4", ai_enabled=False)
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is True


def test_ai_run_requires_genuine_ai_completion_when_candidates_exist(cache):
    av = cache["ANALYSIS_VERSION"]
    complete = _entry(av, video_file=r"C:\src\a.mp4", ai_enabled=True)
    deterministic_only = _entry(av, video_file=r"C:\src\a.mp4", ai_enabled=False)

    assert cache["_cache_entry_is_complete"](complete, require_ai=True) is True
    assert cache["_cache_entry_is_complete"](deterministic_only, require_ai=True) is False


def test_candidate_less_result_is_complete_without_faking_ai_enabled(cache):
    """There is nothing for Qwen to annotate, so the source is finished.

    The pre-D1 behaviour re-analysed such a source on every single run, because the only way to be
    accepted under ``require_ai`` was ``ai_enabled=True`` - which would have been a lie. The
    completion rule carries that knowledge instead.

    R2: "no candidates" only means success when the deterministic pass actually ran, so the entry
    must carry the scoring evidence - see the OpenCV-open-failure tests below.
    """
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\empty.mp4",
                   candidates=[], ai_enabled=False)
    entry["timings"][cache["_DETERMINISTIC_SCORING_KEY"]] = 0.4

    assert entry["ai_enabled"] is False, "the flag must stay honest"
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_a_deferred_entry_is_never_complete_whatever_else_it_claims(cache):
    """The trap any checkpointing change falls into: ai_deferred outranks ai_enabled."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4",
                   ai_enabled=True, ai_deferred=True)

    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is False


@pytest.mark.parametrize("mutate, reason", [
    (lambda e: e.update(analysis_version="auto_av_analysis_v7_old"), "stale version"),
    (lambda e: e.pop("analysis_version"), "missing version"),
    (lambda e: e.pop("video_file"), "missing video_file"),
    (lambda e: e.update(video_file=None), "non-string video_file"),
    (lambda e: e.update(video_file=""), "empty video_file"),
    (lambda e: e.pop("candidates"), "missing candidates"),
    (lambda e: e.update(candidates="not-a-list"), "candidates is a str"),
    (lambda e: e.update(candidates={"a": 1}), "candidates is a dict"),
])
def test_malformed_entries_are_rejected(cache, mutate, reason):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4")
    mutate(entry)
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is False, reason


def test_non_dict_payload_is_rejected_without_raising(cache):
    for payload in ([], "text", 3, None, 4.5):
        assert cache["_cache_entry_is_complete"](payload, require_ai=False) is False


def test_unexpected_extra_fields_stay_allowed(cache):
    """Forward compatibility: D1 adds a completion rule, not a closed schema."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4",
                   something_added_later={"nested": [1, 2]}, another=5)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_a_representative_record_of_this_generation_is_accepted(cache):
    """Shaped after the *real* runtime records, which a read-only audit of all 2196 confirmed carry
    matching `qwen_frame_count`/`qwen_tag_count` and an `ai_analyzed` marker on each tagged candidate
    (``_merge_semantic`` sets it), plus the D2 `cache_contract`.

    D2 note: pre-D2 records carry no contract marker and are rejected — see
    ``test_legacy_1_a_d1_record_is_never_treated_as_d2_complete`` in
    ``test_stage5_cache_identity.py``. They are also unreachable, because the D2 signature re-keys.
    """
    legacy = {
        "analysis_version": cache["ANALYSIS_VERSION"],
        "cache_contract": _CACHE_CONTRACT,
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "duration": 41.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [2.0], "candidate_count": 2,
        "candidates": [{"id": "a", "action_score": 0.4, "ai_analyzed": True},
                       {"id": "b", "action_score": 0.6, "ai_analyzed": True}],
        "analysis_seconds": 4.4,
        "timings": {"total_seconds": 4.4, "qwen_frame_count": 2, "qwen_tag_count": 2,
                    "qwen_seconds": 2.3},
        "ai_enabled": True, "ai_deferred": False,
    }
    assert cache["_cache_entry_is_complete"](legacy, require_ai=True) is True
    assert cache["_cache_entry_is_complete"](legacy, require_ai=False) is True


# ---------------------------------------------------------------------------
# source identity of a payload
# ---------------------------------------------------------------------------


def test_same_source_normalises_case_and_relative_paths(cache, tmp_path):
    target = tmp_path / "Clip.mp4"
    target.write_bytes(b"x")
    assert cache["_same_source"](str(target), str(target)) is True
    assert cache["_same_source"](str(target).upper(), str(target)) is True
    assert cache["_same_source"](str(target), str(tmp_path / "other.mp4")) is False


def test_same_source_rejects_missing_or_non_string_values(cache):
    assert cache["_same_source"](None, r"C:\a.mp4") is False
    assert cache["_same_source"]("", r"C:\a.mp4") is False
    assert cache["_same_source"](123, r"C:\a.mp4") is False


def test_loader_rejects_a_payload_describing_a_different_source(cache, tmp_path):
    """A version-correct entry for a foreign video must not be reused for this one."""
    path = str(tmp_path / "entry.json")
    cache["_save_cache"](path, _entry(cache["ANALYSIS_VERSION"],
                                      video_file=r"C:\elsewhere\FOREIGN.mp4"))

    assert cache["_load_cache"](path, require_ai=True) is not None, "no expectation given"
    assert cache["_load_cache"](path, require_ai=True,
                               expected_video_file=r"C:\lib\ours.mp4") is None


def test_loader_round_trips_a_valid_entry(cache, tmp_path):
    source = str(tmp_path / "ours.mp4")
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=source)
    cache["_save_cache"](path, entry)

    loaded = cache["_load_cache"](path, require_ai=True, expected_video_file=source)
    assert loaded is not None
    assert loaded["video_file"] == source
    assert loaded["candidates"] == entry["candidates"]


def test_loader_rejects_deferred_and_malformed_entries_from_disk(cache, tmp_path):
    av = cache["ANALYSIS_VERSION"]
    source = str(tmp_path / "ours.mp4")

    deferred = str(tmp_path / "deferred.json")
    cache["_save_cache"](deferred, _entry(av, video_file=source, ai_enabled=True, ai_deferred=True))
    assert cache["_load_cache"](deferred, require_ai=True, expected_video_file=source) is None

    wrong_type = str(tmp_path / "wrong.json")
    cache["_save_cache"](wrong_type, _entry(av, video_file=source, candidates="not-a-list"))
    assert cache["_load_cache"](wrong_type, require_ai=True, expected_video_file=source) is None


def test_loader_survives_a_corrupt_file_without_raising(cache, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"analysis_version": "au', encoding="utf-8")
    assert cache["_load_cache"](str(path), require_ai=False) is None


def test_loader_returns_none_for_a_missing_file(cache, tmp_path):
    assert cache["_load_cache"](str(tmp_path / "absent.json"), require_ai=False) is None


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_save_uses_a_unique_temp_in_the_target_directory_and_leaves_none_behind(cache, tmp_path):
    """The pre-D1 writer used one shared ``path + '.tmp'``; two writers could then publish each
    other's payload. Unique names remove the shared resource entirely."""
    path = str(tmp_path / "entry.json")
    observed = []

    for index in range(5):
        cache["_save_cache"](path, _entry(cache["ANALYSIS_VERSION"],
                                          video_file=rf"C:\src\{index}.mp4"))
        observed.append(sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")))

    assert observed == [[]] * 5, f"temp files leaked: {observed}"
    assert not (tmp_path / "entry.json.tmp").exists(), "the fixed shared temp name must be gone"
    assert json.loads(open(path, encoding="utf-8").read())["video_file"] == r"C:\src\4.mp4"


def test_save_publishes_atomically_so_a_reader_never_sees_a_partial_final_file(cache, tmp_path):
    """Regression guard for the property os.replace already provided before D1."""
    av = cache["ANALYSIS_VERSION"]
    path = str(tmp_path / "entry.json")
    source = str(tmp_path / "a.mp4")
    cache["_save_cache"](path, _entry(av, video_file=source, writer="OLD"))

    # a temp that was fully written but never replaced (the crash-before-replace boundary)
    blob = json.dumps(_entry(av, video_file=source, writer="NEW"), indent=2)
    fd, tmp = tempfile.mkstemp(prefix="entry.json.", suffix=".tmp", dir=str(tmp_path))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(blob)

    still_old = cache["_load_cache"](path, require_ai=False)
    assert still_old is not None and still_old["writer"] == "OLD"

    os.replace(tmp, path)
    now_new = cache["_load_cache"](path, require_ai=False)
    assert now_new is not None and now_new["writer"] == "NEW"


def test_save_failure_is_reported_but_never_raises(cache, tmp_path, capsys):
    """A cache write must not be able to abort a render."""
    unwritable = str(tmp_path / "nope" / "deep")
    os.makedirs(unwritable, exist_ok=True)
    # a directory where the final file should be: os.replace will fail
    os.makedirs(os.path.join(unwritable, "entry.json"), exist_ok=True)

    cache["_save_cache"](os.path.join(unwritable, "entry.json"),
                         _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4"))

    assert "could not write video analysis cache" in capsys.readouterr().out
    leftovers = [n for n in os.listdir(unwritable) if n.endswith(".tmp")]
    assert leftovers == [], f"failed write left a temp behind: {leftovers}"


def test_save_writes_valid_utf8_json_with_non_ascii_content(cache, tmp_path):
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\clip.mp4",
                   source_name="ünïcödé – 素材.mp4")
    cache["_save_cache"](path, entry)

    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["source_name"] == "ünïcödé – 素材.mp4"


# ---------------------------------------------------------------------------
# the checkpoint guard
# ---------------------------------------------------------------------------


def test_checkpoint_writes_a_complete_source_immediately(cache, tmp_path):
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"))

    assert cache["_checkpoint_cache"](path, entry, require_ai=True) is True
    assert os.path.exists(path)


@pytest.mark.parametrize("entry_kwargs, why", [
    ({"ai_enabled": False}, "AI required but Qwen did not complete"),
    ({"ai_enabled": True, "ai_deferred": True}, "still deferred"),
    ({"candidates": "not-a-list"}, "malformed payload"),
])
def test_checkpoint_refuses_to_publish_an_incomplete_source(cache, tmp_path, entry_kwargs, why):
    """This is what makes early saving safe: the checkpoint cannot create a record that the
    loader would then accept as AI-complete."""
    path = str(tmp_path / "entry.json")
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"), **entry_kwargs)

    assert cache["_checkpoint_cache"](path, entry, require_ai=True) is False, why
    assert not os.path.exists(path), why


def test_checkpoint_without_a_cache_file_is_a_no_op(cache, tmp_path):
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=str(tmp_path / "a.mp4"))
    assert cache["_checkpoint_cache"](None, entry, require_ai=True) is False
    assert cache["_checkpoint_cache"]("", entry, require_ai=True) is False


# ---------------------------------------------------------------------------
# R2: zero-tag truth from the worker response envelope (T5, T6)
# ---------------------------------------------------------------------------


def test_every_decoded_frame_must_have_produced_a_semantic(cache):
    """T1: a fully successful submitted job — `tag_count == frame_count > 0`.

    R4 replaced an earlier assumption that a bare response envelope proved success. Read against the
    worker: `_run_semantics_for_video` always returns timings and `main` always records them, so the
    envelope only proves the job loop returned. Inside it, `_normalize_semantic` returns ``{}`` for
    invalid semantic content, `_run_inference_wave` treats ``{}`` as failed and retries, and a
    candidate still failing is absent from the returned semantics.
    """
    ns = _load_pipeline(_worker_ok(frame_count=3))
    candidates = [{"id": f"clip.mp4-{i}"} for i in range(3)]

    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=candidates,
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_tag_count"] == 3 and info["qwen_frame_count"] == 3
    assert ns["_calls"]["merged"] == ["clip.mp4-0", "clip.mp4-1", "clip.mp4-2"]


def test_all_semantic_inference_failed_is_not_completed():
    """T2: the job loop returned, but every candidate's semantic failed after retries."""
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=0, semantics={}))
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(3)],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["merged"] == [], "nothing to merge, and nothing was invented"


def test_partial_semantic_failure_is_not_completed():
    """T3: some decoded frames produced no valid semantic, so the AI work is incomplete."""
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=2,
                                   semantics=_semantic_records(2)))
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(3)],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["merged"] == ["clip.mp4-0", "clip.mp4-1"], (
        "the tags that did arrive are still merged; only the completion verdict changes")


def test_no_decoded_frames_is_not_completed():
    """T4: prefetch decoded nothing, so no AI frame work happened at all."""
    ns = _load_pipeline(_worker_ok(frame_count=0, tag_count=0, semantics={}))
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(3)],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False


def test_a_response_claiming_more_tags_than_it_returned_is_not_completed():
    """Basic count coherence: tag_count may not exceed the semantic records actually present."""
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=3,
                                   semantics=_semantic_records(1)))
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(3)],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False


@pytest.mark.parametrize("timing, why", [
    ({"frame_count": 3}, "tag_count missing -> treated as 0"),
    ({"tag_count": 3}, "frame_count missing -> no frame work proven"),
    ({"frame_count": "three", "tag_count": "three"}, "non-numeric counts"),
    ({"frame_count": -1, "tag_count": -1}, "negative counts"),
    ("not-a-dict", "malformed timing"),
])
def test_malformed_or_incomplete_timing_is_not_completed(timing, why):
    response = {
        "model_load_seconds": 8.0, "model_id": "q", "batch_size": 1, "peak_vram_gb": 3.0,
        "timings_by_job": {"single": timing},
        "semantics": _semantic_records(3),
    }
    ns = _load_pipeline(response)
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(3)],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False, why


def test_the_shared_completion_rule_is_used_by_both_paths(cache):
    """The arithmetic exists once; exercise it directly at its boundaries."""
    rule = cache["_qwen_job_completed"]
    abc = {"a", "b", "c"}

    assert rule({"frame_count": 3, "tag_count": 3}, True, abc, abc) is True
    assert rule({"frame_count": 3, "tag_count": 0}, True, abc, set()) is False
    assert rule({"frame_count": 3, "tag_count": 2}, True, abc, {"a", "b"}) is False
    assert rule({"frame_count": 0, "tag_count": 0}, True, abc, set()) is False
    assert rule({"frame_count": 3, "tag_count": 3}, False, abc, abc) is False, "no envelope"
    assert rule(None, True, abc, abc) is False
    assert rule({"frame_count": 3, "tag_count": 4}, True, abc, abc) is False, "more tags than frames"
    assert rule({"frame_count": 3, "tag_count": 3}, True, set(), set()) is False, "nothing requested"

    # R5: the decoded set must cover the requested set, and the ids must be ours
    assert rule({"frame_count": 2, "tag_count": 2}, True, abc, {"a", "b"}) is False, (
        "2 of 3 requested candidates decoded is not complete")
    assert rule({"frame_count": 3, "tag_count": 3}, True, abc, {"x", "y", "z"}) is False, (
        "foreign ids are not evidence that our candidates completed")
    assert rule({"frame_count": 3, "tag_count": 3}, True, abc, {"a", "b", "x"}) is False, (
        "one expected id replaced by a foreign one")
    assert rule({"frame_count": 3, "tag_count": 3}, True, abc, abc | {"x"}) is False, (
        "an extra id means the response does not match the request")

    # R5: bool is an int subclass, but it is not a count
    assert rule({"frame_count": True, "tag_count": True}, True, {"a"}, {"a"}) is False
    assert rule({"frame_count": 1, "tag_count": True}, True, {"a"}, {"a"}) is False


def test_count_type_validation_rejects_bools(cache):
    is_count = cache["_is_count"]
    assert is_count(3) is True and is_count(0) is True
    assert is_count(True) is False and is_count(False) is False
    assert is_count(3.0) is False and is_count("3") is False and is_count(None) is False


def test_a_reported_zero_count_is_not_rewritten_as_the_requested_count(cache):
    """R5: `timing.get(key) or default` turned a genuine frame_count=0 into the requested count."""
    reported = cache["_reported_count"]

    assert reported({"frame_count": 0}, "frame_count", 10) == 0, "a real zero must stay zero"
    assert reported({"tag_count": 0}, "tag_count", 7) == 0
    assert reported({"frame_count": 4}, "frame_count", 10) == 4
    # absent -> the caller's default is the only information available
    assert reported({}, "frame_count", 10) == 10
    assert reported(None, "frame_count", 10) == 10
    # present but unreadable is not evidence of anything
    assert reported({"frame_count": "three"}, "frame_count", 10) == 0


def test_an_empty_worker_response_is_not_completed():
    """Every `_run_qwen_worker` failure path returns ``{}`` - launch error, non-zero exit, timeout
    or an unreadable response."""
    ns = _load_pipeline({})
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[{"id": "clip.mp4-0"}],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False


@pytest.mark.parametrize("response, why", [
    ({"timings_by_job": {}}, "envelope present but this job never finished"),
    ({"timings_by_job": {"other": {}}}, "a different job id finished"),
    ({"semantics": {"clip.mp4-0": {"action": 0.9}}}, "tags but no completion envelope"),
    ({"timings_by_job": None}, "malformed envelope"),
    (None, "no response object at all"),
])
def test_completion_requires_this_jobs_envelope(response, why):
    ns = _load_pipeline(response)
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[{"id": "clip.mp4-0"}],
        qwen_model_path="m", use_gpu=False)
    assert info[ns["_QWEN_COMPLETED_KEY"]] is False, why


def test_semantic_values_are_actually_merged_onto_the_candidates():
    ns = _load_pipeline(_worker_ok(frame_count=3))
    candidates = [{"id": f"clip.mp4-{i}"} for i in range(3)]

    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=candidates,
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert ns["_calls"]["merged"] == ["clip.mp4-0", "clip.mp4-1", "clip.mp4-2"]
    assert all(c["semantic_action"] == 0.9 for c in candidates)


def test_max_windows_zero_reports_not_completed_and_never_calls_the_worker():
    ns = _load_pipeline(_worker_ok(), max_windows=0)
    try:
        info = ns["_annotate_candidates_with_qwen"](
            video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[{"id": "clip.mp4-0"}],
            qwen_model_path="m", use_gpu=False)
    finally:
        os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["worker_requests"] == 0


def test_no_selected_candidates_is_a_completed_no_op():
    ns = _load_pipeline(_worker_ok())
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[],
        qwen_model_path="m", use_gpu=False)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_tag_count"] == 0
    assert ns["_calls"]["worker_requests"] == 0


# ---------------------------------------------------------------------------
# R6: stored-record consistency for legacy AI cache entries
# ---------------------------------------------------------------------------


def _stored(av, *, candidate_count, frame_count, tag_count, analysed,
            analysed_ids=None, duplicate_analysed=False, video_file=r"C:\lib\clip.mp4"):
    """A persisted AI-complete record, shaped like the real cache entries."""
    candidates = []
    for i in range(candidate_count):
        candidate = {"id": f"clip.mp4-{i}", "start": i * 2.0, "end": i * 2.0 + 2.0,
                     "action_score": 0.5, "editorial_score": 0.9 - i * 0.01}
        candidates.append(candidate)
    ids = analysed_ids if analysed_ids is not None else [f"clip.mp4-{i}" for i in range(analysed)]
    if duplicate_analysed and ids:
        ids = list(ids[:-1]) + [ids[0]]
    by_id = {c["id"]: c for c in candidates}
    for cid in ids:
        target = by_id.get(cid)
        if target is None:                      # a foreign analysed id: attach a stray candidate
            candidates.append({"id": cid, "ai_analyzed": True, "action_score": 0.5})
        else:
            target["ai_analyzed"] = True
    if duplicate_analysed:
        # two candidate entries carrying the same analysed id
        candidates.append({"id": ids[0], "ai_analyzed": True, "action_score": 0.5})
    timings = {"total_seconds": 12.0, "candidate_scoring_seconds": 0.5,
               "qwen_model_id": "Qwen3VL-2B-Instruct-Q8_0 (llama.cpp Vulkan)"}
    if frame_count is not None:
        timings["qwen_frame_count"] = frame_count
    if tag_count is not None:
        timings["qwen_tag_count"] = tag_count
    return {
        "analysis_version": av, "cache_contract": _CACHE_CONTRACT, "video_file": video_file,
        "source_name": os.path.basename(video_file), "duration": 30.0, "fps": 25.0,
        "width": 1280, "height": 720, "scene_changes": [1.0],
        "candidate_count": len(candidates), "candidates": candidates,
        "analysis_seconds": 12.0, "timings": timings,
        "ai_enabled": True, "ai_deferred": False,
    }


def test_L1_a_stored_record_with_fewer_tags_than_decoded_frames_is_rejected(cache):
    """The exact shape of the 4 bad records found in the real runtime cache:
    frame_count=10, tag_count=9, 9 candidates marked ai_analyzed, ai_enabled=True.
    The pre-R6 loader accepted all four."""
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10,
                    tag_count=9, analysed=9)

    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False
    # still reusable for a deterministic-only run: its candidates are real work
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is True


def test_L2_a_stored_record_whose_ai_analyzed_count_disagrees_with_tag_count_is_rejected(cache):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10,
                    tag_count=10, analysed=9)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


def test_L3_duplicate_ai_analyzed_ids_are_rejected(cache):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10,
                    tag_count=10, analysed=10, duplicate_analysed=True)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


def test_L4_an_ai_analyzed_id_outside_the_candidate_set_is_rejected(cache):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10, tag_count=10,
                    analysed=0, analysed_ids=[f"clip.mp4-{i}" for i in range(9)] + ["foreign-1"])
    # the stray analysed id is appended as its own candidate, so the subset check is what must fire
    entry["candidates"] = [c for c in entry["candidates"] if c["id"] != "foreign-1"] + \
                          [{"id": "foreign-1", "ai_analyzed": True}]
    entry["candidates"] = [c for c in entry["candidates"] if c["id"] != "foreign-1"]
    entry["timings"]["qwen_tag_count"] = 10
    entry["timings"]["qwen_frame_count"] = 10
    # 9 analysed inside the set, tag_count claims 10 -> contradiction either way
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


def test_L5_a_stored_zero_frame_count_is_rejected(cache):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=0,
                    tag_count=0, analysed=0)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


@pytest.mark.parametrize("frame_count, tag_count", [(True, True), (True, 1), (1, True)])
def test_L6_bool_counts_in_a_stored_record_are_rejected(cache, frame_count, tag_count):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=1, frame_count=frame_count,
                    tag_count=tag_count, analysed=1)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


@pytest.mark.parametrize("drop", ["qwen_frame_count", "qwen_tag_count"])
def test_a_stored_record_missing_a_qwen_count_is_rejected(cache, drop):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=3, frame_count=3,
                    tag_count=3, analysed=3)
    del entry["timings"][drop]
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


def test_a_stored_frame_count_above_the_candidate_count_is_rejected(cache):
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=3, frame_count=5,
                    tag_count=5, analysed=3)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False


def test_L7_a_fully_consistent_legacy_record_is_accepted(cache):
    """The 2190 records the audit found provably complete."""
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10,
                    tag_count=10, analysed=10)
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_L8_an_internally_consistent_subset_record_is_accepted(cache):
    """Load-bearing: R6 must NOT pretend it can reconstruct the missing legacy requested set.

    These are the two real records with 398/525 candidates and 114/119 decoded+tagged. They may well
    be decoded subsets of a 120-candidate request, but ``requested_ids`` and the historical
    ``BEATSYNC_QWEN_MAX_WINDOWS`` were never stored, so R6 cannot prove it. Rejecting them would be
    inventing evidence; they are left for D2.
    """
    for candidate_count, covered in ((398, 114), (525, 119)):
        entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=candidate_count,
                        frame_count=covered, tag_count=covered, analysed=covered)
        assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True, candidate_count


def test_the_stored_rule_is_not_applied_when_ai_is_not_required(cache):
    """A deterministic-only run must still reuse these records: the candidates are real work."""
    entry = _stored(cache["ANALYSIS_VERSION"], candidate_count=10, frame_count=10,
                    tag_count=9, analysed=9)
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is True


def test_the_stored_rule_does_not_touch_the_candidate_less_path(cache):
    """No candidates means no Qwen counts to check; the deterministic evidence still governs."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\lib\empty.mp4",
                   candidates=[], ai_enabled=False)
    entry["timings"][cache["_DETERMINISTIC_SCORING_KEY"]] = 0.4
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True

    without_evidence = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\lib\broken.mp4",
                              candidates=[], ai_enabled=False)
    assert cache["_cache_entry_is_complete"](without_evidence, require_ai=True) is False


def test_the_stored_consistency_rule_reads_defensively(cache):
    rule = cache["_stored_ai_cache_is_consistent"]

    assert rule({"candidates": [], "timings": {}}) is False
    assert rule({"candidates": "nope", "timings": {}}) is False
    assert rule({"candidates": [{"id": "a"}], "timings": None}) is False
    assert rule({"candidates": [{"id": "a"}], "timings": {"qwen_frame_count": 1,
                                                          "qwen_tag_count": 1}}) is False, \
        "one candidate, one tag claimed, but nothing is marked ai_analyzed"
    ok = {"candidates": [{"id": "a", "ai_analyzed": True}],
          "timings": {"qwen_frame_count": 1, "qwen_tag_count": 1}}
    assert rule(ok) is True
    assert rule({"candidates": [{"id": None, "ai_analyzed": True}, {"id": None}],
                 "timings": {"qwen_frame_count": 1, "qwen_tag_count": 1}}) is False, \
        "unusable candidate ids cannot be compared"


# ---------------------------------------------------------------------------
# R5: the requested Qwen candidate set must be covered end to end
# ---------------------------------------------------------------------------


def _single(ns, candidate_count=3):
    return ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0,
        candidates=[{"id": f"clip.mp4-{i}"} for i in range(candidate_count)],
        qwen_model_path="m", use_gpu=False)


def test_a_decoded_subset_of_the_requested_candidates_is_not_complete():
    """R5's core defect: `_prefetch_candidate_frames` returns only the frames it could read, so
    requested=3 / decoded=2 / tagged=2 satisfied R4's `tag_count == frame_count` while one requested
    candidate silently had no semantics at all."""
    response = _worker_ok(frame_count=2, tag_count=2, semantics=_semantic_records(2))
    ns = _load_pipeline(response)

    info = _single(ns, candidate_count=3)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert info["qwen_frame_count"] == 2, "the worker's own count is reported, not the request"


def test_full_coverage_of_the_requested_set_is_complete():
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=3, semantics=_semantic_records(3)))
    info = _single(ns, candidate_count=3)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_frame_count"] == 3 and info["qwen_tag_count"] == 3


def test_semantics_for_foreign_candidate_ids_are_not_completion_evidence():
    """Counts look perfect, but none of the returned ids are ours."""
    foreign = {f"other.mp4-{i}": {"action": 0.9} for i in range(3)}
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=3, semantics=foreign))

    info = _single(ns, candidate_count=3)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["merged"] == [], "nothing of ours could be merged"


def test_one_requested_id_replaced_by_a_foreign_id_is_not_complete():
    mixed = {"clip.mp4-0": {"action": 0.9}, "clip.mp4-1": {"action": 0.9},
             "other.mp4-9": {"action": 0.9}}
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=3, semantics=mixed))

    info = _single(ns, candidate_count=3)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["merged"] == ["clip.mp4-0", "clip.mp4-1"], (
        "the two genuine tags are still merged; only the verdict changes")


def test_an_extra_foreign_id_alongside_a_full_set_is_not_complete():
    extra = dict(_semantic_records(3))
    extra["other.mp4-9"] = {"action": 0.9}
    ns = _load_pipeline(_worker_ok(frame_count=3, tag_count=3, semantics=extra))

    assert _single(ns, candidate_count=3)[ns["_QWEN_COMPLETED_KEY"]] is False


def test_a_reported_zero_frame_count_is_reported_back_as_zero():
    """R5: it used to be rewritten as the requested candidate count in the stored timings."""
    ns = _load_pipeline(_worker_ok(frame_count=0, tag_count=0, semantics={}))
    info = _single(ns, candidate_count=3)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert info["qwen_frame_count"] == 0, "a genuine zero must survive into the timings"
    assert info["qwen_tag_count"] == 0


def test_bool_counts_are_rejected_by_the_single_path():
    response = {
        "model_load_seconds": 8.0, "model_id": "q", "batch_size": 1, "peak_vram_gb": 3.0,
        "timings_by_job": {"single": {"frame_count": True, "tag_count": True}},
        "semantics": {"clip.mp4-0": {"action": 0.9}},
    }
    ns = _load_pipeline(response)
    assert _single(ns, candidate_count=1)[ns["_QWEN_COMPLETED_KEY"]] is False


# ---------------------------------------------------------------------------
# R4: batch per-job completion truth (T6, T7, T8)
# ---------------------------------------------------------------------------

_BATCH_FUNCS = ("_qwen_max_windows", "_complete_deferred_qwen_batch")


def _load_batch(worker_response):
    """Exec the real `_complete_deferred_qwen_batch` with the worker subprocess stubbed."""
    found, consts = _extract(_FUNCS + ("_fmt_seconds",) + _BATCH_FUNCS)
    calls: Dict[str, Any] = {"merged": []}
    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile, "math": math, "time": time,
        "Any": Any, "Dict": Dict, "List": list, "Sequence": list,
        "__builtins__": __builtins__,
        "_select_ai_candidates": lambda candidates, limit: list(candidates)[:limit],
        # faithful to the real `_merge_semantic`, which also sets `ai_analyzed = True`
        "_merge_semantic": lambda candidate, semantic: (
            calls["merged"].append(candidate["id"]),
            candidate.update({"semantic_action": semantic.get("action", 0.0),
                              "ai_analyzed": True}))[1],
        "_run_qwen_worker_batch": lambda **kwargs: worker_response,
    }
    namespace.update(consts)
    _exec_into(namespace, found, _FUNCS + ("_fmt_seconds",) + _BATCH_FUNCS)
    os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)
    namespace["_calls"] = calls
    return namespace


def _deferred_record(av, name, n=3, path=None):
    return {
        "analysis_version": av, "cache_contract": _CACHE_CONTRACT,
        "video_file": path or rf"C:\src\{name}", "source_name": name,
        "duration": 30.0, "fps": 25.0, "width": 1280, "height": 720, "scene_changes": [1.0],
        "candidate_count": n,
        "candidates": [{"id": f"{name}-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                        "editorial_score": 0.5 - i * 0.01} for i in range(n)],
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0, "candidate_scoring_seconds": 0.4},
        "ai_enabled": False, "ai_deferred": True,
    }


def _batch_response(jobs):
    """jobs: {job_id: (frame_count, tag_count, semantic_record_count, source_name)}"""
    semantics, timings = {}, {}
    for job_id, (frames, tags, records, name) in jobs.items():
        semantics[job_id] = _semantic_records(records, name)
        timings[job_id] = {"frame_count": frames, "tag_count": tags,
                           "inference_seconds": 5.0, "prefetch_seconds": 0.1}
    return {"model_load_seconds": 8.0, "model_id": "q", "batch_size": len(jobs),
            "peak_vram_gb": 3.0, "semantics_by_job": semantics, "timings_by_job": timings}


def test_a_fully_successful_batch_job_is_complete_and_checkpointed(cache, tmp_path):
    """T6."""
    ns = _load_batch(_batch_response({"1": (3, 3, 3, "ok.mp4")}))
    record = _deferred_record(cache["ANALYSIS_VERSION"], "ok.mp4")
    path = str(tmp_path / "ok.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": path}, record)],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert record["ai_enabled"] is True
    assert record["ai_deferred"] is False
    assert os.path.exists(path)
    assert cache["_load_cache"](path, require_ai=True,
                               expected_video_file=record["video_file"]) is not None


@pytest.mark.parametrize("frames, tags, records, why", [
    (3, 0, 0, "every candidate's semantic failed after retries"),
    (3, 2, 2, "partial semantic failure"),
    (0, 0, 0, "prefetch decoded nothing"),
])
def test_an_incomplete_batch_job_is_neither_complete_nor_checkpointed(cache, tmp_path, frames,
                                                                     tags, records, why):
    """T7."""
    ns = _load_batch(_batch_response({"1": (frames, tags, records, "bad.mp4")}))
    record = _deferred_record(cache["ANALYSIS_VERSION"], "bad.mp4")
    path = str(tmp_path / "bad.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": path}, record)],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert record["ai_enabled"] is False, why
    assert record["ai_deferred"] is False, "the deferral is resolved either way"
    assert not os.path.exists(path), why
    assert record["candidates"], "deterministic candidates are always kept"


def test_a_successful_sibling_survives_a_failing_one(cache, tmp_path):
    """T8: one job's failure must not cost a sibling that genuinely completed."""
    ns = _load_batch(_batch_response({
        "1": (3, 3, 3, "good.mp4"),      # fully successful
        "2": (3, 1, 1, "half.mp4"),      # partial -> incomplete
    }))
    good = _deferred_record(cache["ANALYSIS_VERSION"], "good.mp4")
    half = _deferred_record(cache["ANALYSIS_VERSION"], "half.mp4")
    good_path, half_path = str(tmp_path / "good.json"), str(tmp_path / "half.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": good_path}, good),
                     ({"index": 2, "cache_file": half_path}, half)],
        use_gpu=False, qwen_model_path="m", total_video_count=2)

    assert good["ai_enabled"] is True and os.path.exists(good_path)
    assert half["ai_enabled"] is False and not os.path.exists(half_path)


def test_a_batch_job_with_full_counts_but_foreign_ids_is_not_complete(cache, tmp_path):
    """R5 in the batch path: perfect counts, but the semantics are for someone else's candidates."""
    response = _batch_response({"1": (3, 3, 3, "good.mp4")})
    response["semantics_by_job"]["1"] = {f"other.mp4-{i}": {"action": 0.9} for i in range(3)}
    ns = _load_batch(response)
    record = _deferred_record(cache["ANALYSIS_VERSION"], "good.mp4")
    path = str(tmp_path / "good.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": path}, record)],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert record["ai_enabled"] is False
    assert not os.path.exists(path)


def test_a_batch_job_that_decoded_only_a_subset_is_not_complete(cache, tmp_path):
    """requested 3, decoded 2, tagged 2 - R4 accepted this."""
    ns = _load_batch(_batch_response({"1": (2, 2, 2, "part.mp4")}))
    record = _deferred_record(cache["ANALYSIS_VERSION"], "part.mp4")
    path = str(tmp_path / "part.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": path}, record)],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert record["ai_enabled"] is False
    assert not os.path.exists(path)
    assert record["timings"]["qwen_frame_count"] == 2, "the worker's own count is stored"


def test_one_complete_job_survives_two_differently_broken_siblings(cache, tmp_path):
    """§8: complete + decoded-subset + foreign-ids, all in one batch."""
    response = _batch_response({"1": (3, 3, 3, "ok.mp4"),
                                "2": (2, 2, 2, "subset.mp4"),
                                "3": (3, 3, 3, "foreign.mp4")})
    response["semantics_by_job"]["3"] = {f"elsewhere-{i}": {"action": 0.9} for i in range(3)}
    ns = _load_batch(response)

    records = {name: _deferred_record(cache["ANALYSIS_VERSION"], f"{name}.mp4")
               for name in ("ok", "subset", "foreign")}
    paths = {name: str(tmp_path / f"{name}.json") for name in records}

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": i, "cache_file": paths[name]}, records[name])
                     for i, name in enumerate(("ok", "subset", "foreign"), 1)],
        use_gpu=False, qwen_model_path="m", total_video_count=3)

    assert records["ok"]["ai_enabled"] is True and os.path.exists(paths["ok"])
    assert records["subset"]["ai_enabled"] is False and not os.path.exists(paths["subset"])
    assert records["foreign"]["ai_enabled"] is False and not os.path.exists(paths["foreign"])


def test_a_requested_job_absent_from_the_response_is_not_complete(cache, tmp_path):
    ns = _load_batch(_batch_response({"1": (3, 3, 3, "present.mp4")}))
    present = _deferred_record(cache["ANALYSIS_VERSION"], "present.mp4")
    missing = _deferred_record(cache["ANALYSIS_VERSION"], "missing.mp4")
    p1, p2 = str(tmp_path / "p1.json"), str(tmp_path / "p2.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": p1}, present),
                     ({"index": 2, "cache_file": p2}, missing)],
        use_gpu=False, qwen_model_path="m", total_video_count=2)

    assert present["ai_enabled"] is True and os.path.exists(p1)
    assert missing["ai_enabled"] is False and not os.path.exists(p2)


def test_total_batch_failure_still_marks_every_job_incomplete(cache, tmp_path):
    ns = _load_batch({})
    records = [_deferred_record(cache["ANALYSIS_VERSION"], f"t{i}.mp4") for i in range(2)]
    paths = [str(tmp_path / f"t{i}.json") for i in range(2)]

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": i + 1, "cache_file": paths[i]}, records[i]) for i in range(2)],
        use_gpu=False, qwen_model_path="m", total_video_count=2)

    for record, path in zip(records, paths):
        assert record["ai_enabled"] is False
        assert not os.path.exists(path)


def test_a_candidate_less_batch_job_is_still_the_no_qwen_work_case(cache, tmp_path):
    """No Qwen job is submitted, so the per-frame rule does not apply to it."""
    ns = _load_batch(_batch_response({"1": (3, 3, 3, "x.mp4")}))
    record = _deferred_record(cache["ANALYSIS_VERSION"], "empty.mp4", n=0)
    path = str(tmp_path / "empty.json")

    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": path}, record)],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert record["ai_enabled"] is False, "honest: no AI work was done"
    assert record["ai_deferred"] is False
    assert os.path.exists(path), "but it is complete and reusable via the completion rule"
    assert cache["_load_cache"](path, require_ai=True,
                               expected_video_file=record["video_file"]) is not None


# ---------------------------------------------------------------------------
# R2: inline (serial) AI completion truth (T1, T2, T3, T4)
# ---------------------------------------------------------------------------


def _inline(ns, **kwargs):
    defaults = dict(video_file=r"C:\src\clip.mp4", use_gpu=False, enable_ai=True,
                    qwen_model_path="m", defer_ai=False)
    defaults.update(kwargs)
    return ns["_analyze_single_video"](**defaults)


def test_inline_qwen_success_reports_ai_enabled():
    ns = _load_pipeline(_worker_ok(tag_count=3))
    record = _inline(ns)

    assert record["ai_enabled"] is True
    assert record["ai_deferred"] is False
    assert record["candidates"], "deterministic candidates must survive"


@pytest.mark.parametrize("response, why", [
    ({}, "worker failed / returned nothing"),
    ({"timings_by_job": {}}, "no completion envelope for this job"),
    ({"semantics": {"clip.mp4-0": {"action": 0.9}}}, "tags without a completion envelope"),
])
def test_inline_qwen_failure_must_not_report_ai_enabled(response, why):
    """The R2 defect: the serial path ignored the completion signal entirely and reported
    ``ai_enabled = enable_ai and not defer_ai``, so a failed Qwen run was cached as AI-complete."""
    ns = _load_pipeline(response)
    record = _inline(ns)

    assert record["ai_enabled"] is False, why
    assert record["candidates"], "deterministic work is still kept on Qwen failure"


def test_inline_qwen_raising_must_not_report_ai_enabled():
    def boom():
        raise RuntimeError("llama.cpp worker died")

    ns = _load_pipeline(boom)
    record = _inline(ns)

    assert record["ai_enabled"] is False
    assert record["candidates"]


def test_inline_max_windows_zero_must_not_report_ai_enabled():
    ns = _load_pipeline(_worker_ok(), max_windows=0)
    try:
        record = _inline(ns)
    finally:
        os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)

    assert record["ai_enabled"] is False, (
        "QWEN_MAX_WINDOWS is not part of cache identity, so a deliberately Qwen-less result must not "
        "be stored under an AI-keyed entry")


def test_the_private_completion_key_never_reaches_timings_or_the_cache(tmp_path):
    """T4: the bookkeeping flag must not leak into the stored payload through timings.update()."""
    ns = _load_pipeline(_worker_ok(frame_count=3))
    record = _inline(ns)
    key = ns["_QWEN_COMPLETED_KEY"]

    assert key not in record["timings"], record["timings"]
    assert key not in record

    path = str(tmp_path / "entry.json")
    ns["_save_cache"](path, record)
    stored = json.loads(open(path, encoding="utf-8").read())
    assert key not in stored.get("timings", {})
    assert key not in stored
    assert key not in open(path, encoding="utf-8").read()


def test_deferred_inline_request_stays_deferred():
    ns = _load_pipeline(_worker_ok())
    record = _inline(ns, defer_ai=True)

    assert record["ai_deferred"] is True
    assert record["ai_enabled"] is False
    assert ns["_calls"]["worker_requests"] == 0, "deferred means Qwen has not run yet"


def test_non_ai_analysis_is_unchanged():
    ns = _load_pipeline(_worker_ok())
    record = _inline(ns, enable_ai=False)

    assert record["ai_enabled"] is False
    assert record["ai_deferred"] is False
    assert ns["_calls"]["worker_requests"] == 0


# ---------------------------------------------------------------------------
# R2: legitimate empty candidates vs failed deterministic analysis (T7, T8, T9)
# ---------------------------------------------------------------------------


def test_a_genuine_candidate_less_analysis_records_scoring_evidence(cache):
    """Windows were measured but none were usable: a real, finished analysis."""
    ns = _load_pipeline(_worker_ok(), window_count=0)
    record = _inline(ns, enable_ai=False)

    assert record["candidates"] == []
    assert cache["_DETERMINISTIC_SCORING_KEY"] in record["timings"]
    assert cache["_cache_entry_is_complete"](record, require_ai=True) is True
    assert cache["_cache_entry_is_complete"](record, require_ai=False) is True


def test_an_opencv_open_failure_is_not_a_candidate_less_success(cache):
    """The R2 defect: both outcomes end with ``candidates == []``, and the D1 rule accepted both,
    so one transient decode failure retired a readable source permanently."""
    ns = _load_pipeline(_worker_ok(), capture_opens=False)
    record = _inline(ns, enable_ai=False)

    assert record["candidates"] == []
    assert cache["_DETERMINISTIC_SCORING_KEY"] not in record["timings"], (
        "the scoring step never ran, so it must leave no evidence behind")
    assert cache["_cache_entry_is_complete"](record, require_ai=True) is False
    assert cache["_cache_entry_is_complete"](record, require_ai=False) is False


def test_a_failed_deterministic_source_is_not_checkpointed_and_is_retried(cache, tmp_path):
    """T8 + T9: nothing is written, so the next run re-analyses the source."""
    ns = _load_pipeline(_worker_ok(), capture_opens=False)
    record = _inline(ns, enable_ai=False)
    path = str(tmp_path / "entry.json")

    assert cache["_checkpoint_cache"](path, record, require_ai=False) is False
    assert not os.path.exists(path)
    # next run: the cache lookup finds nothing, so the source is analysed again
    assert cache["_load_cache"](path, require_ai=False,
                               expected_video_file=r"C:\src\clip.mp4") is None


def test_a_genuine_candidate_less_source_is_checkpointed_and_reused(cache, tmp_path):
    ns = _load_pipeline(_worker_ok(), window_count=0)
    record = _inline(ns, enable_ai=False)
    path = str(tmp_path / "entry.json")

    assert cache["_checkpoint_cache"](path, record, require_ai=True) is True
    reused = cache["_load_cache"](path, require_ai=True,
                                  expected_video_file=record["video_file"])
    assert reused is not None
    assert reused["candidates"] == []


def test_deterministic_completion_evidence_is_read_defensively(cache):
    for payload in (None, [], "text", {}, {"timings": None}, {"timings": []},
                    {"timings": {"other": 1}}):
        assert cache["_deterministic_analysis_completed"](payload) is False
    assert cache["_deterministic_analysis_completed"](
        {"timings": {cache["_DETERMINISTIC_SCORING_KEY"]: 0.0}}) is True


def test_an_empty_candidate_list_alone_is_not_evidence_of_success(cache):
    """Guards the rule itself: the old `if not candidates: return True` shortcut is gone."""
    entry = _entry(cache["ANALYSIS_VERSION"], video_file=r"C:\src\a.mp4",
                   candidates=[], ai_enabled=False)
    entry["timings"] = {"total_seconds": 1.0}       # no scoring evidence

    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is False
    assert cache["_cache_entry_is_complete"](entry, require_ai=False) is False

    entry["timings"][cache["_DETERMINISTIC_SCORING_KEY"]] = 0.5
    assert cache["_cache_entry_is_complete"](entry, require_ai=True) is True


def test_checkpointed_sources_survive_while_a_later_source_is_abandoned(cache, tmp_path):
    """D1-A/B in behavioural form: the records a run has already finished are on disk, and a
    record abandoned mid-run is not - which is exactly what the pre-D1 terminal-only save lost."""
    av = cache["ANALYSIS_VERSION"]
    finished = []
    for index in (1, 2):
        source = str(tmp_path / f"s{index}.mp4")
        path = str(tmp_path / f"s{index}.json")
        assert cache["_checkpoint_cache"](path, _entry(av, video_file=source), require_ai=True)
        finished.append((source, path))

    # third source never completes -> nothing is written for it
    third = str(tmp_path / "s3.json")
    assert cache["_checkpoint_cache"](
        third, _entry(av, video_file=str(tmp_path / "s3.mp4"), ai_deferred=True),
        require_ai=True) is False

    for source, path in finished:
        assert cache["_load_cache"](path, require_ai=True, expected_video_file=source) is not None
    assert not os.path.exists(third)
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
