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
_FUNCS = ("_safe_name", "_hash_text", "_same_source", "_deterministic_analysis_completed",
          "_cache_entry_is_complete", "_load_cache", "_save_cache", "_checkpoint_cache")
_CONSTS = ("ANALYSIS_VERSION", "_QWEN_COMPLETED_KEY", "_QWEN_SINGLE_JOB_ID",
           "_DETERMINISTIC_SCORING_KEY")

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
        "_merge_semantic": lambda candidate, semantic: (
            calls["merged"].append(candidate["id"]),
            candidate.update({"semantic_action": semantic.get("action", 0.0)}))[1],
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


def _worker_ok(tag_count=3, semantics=None):
    """A structurally successful legacy-single worker response."""
    return {
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b", "batch_size": 1,
        "peak_vram_gb": 3.0, "total_seconds": 12.0,
        "timings_by_job": {"single": {"tag_count": tag_count, "frame_count": 3,
                                      "inference_seconds": 5.0}},
        "semantics": {"clip.mp4-0": {"action": 0.9}} if semantics is None else semantics,
    }


@pytest.fixture(scope="module")
def cache():
    return _load_primitives()


def _entry(av, *, video_file, candidates=None, ai_enabled=True, ai_deferred=False, **extra):
    entry = {
        "analysis_version": av,
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


def test_a_representative_pre_d1_entry_is_still_accepted(cache):
    """Existing cache entries must remain reusable: D1 changes no key and no required field."""
    legacy = {
        "analysis_version": cache["ANALYSIS_VERSION"],
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "duration": 41.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [2.0], "candidate_count": 2,
        "candidates": [{"id": "a", "action_score": 0.4}, {"id": "b", "action_score": 0.6}],
        "analysis_seconds": 4.4,
        "timings": {"total_seconds": 4.4, "qwen_tag_count": 2, "qwen_seconds": 2.3},
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


def test_a_finished_worker_with_zero_semantic_tags_counts_as_completed():
    """The worker publishes ``timings_by_job["single"]`` once the job finishes, whatever the tag
    count. A zero-tag *success* must not be mistaken for a failure, or every run repeats the whole
    Qwen pass for a source that genuinely has nothing to say."""
    ns = _load_pipeline(_worker_ok(tag_count=0, semantics={}))
    candidates = [{"id": f"clip.mp4-{i}"} for i in range(3)]

    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=candidates,
        qwen_model_path="m", use_gpu=False, audio_profile={})

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_tag_count"] == 0
    assert ns["_calls"]["merged"] == [], "nothing to merge, and nothing was invented"


def test_an_empty_worker_response_is_not_completed():
    """Every `_run_qwen_worker` failure path returns ``{}`` - launch error, non-zero exit, timeout
    or an unreadable response."""
    ns = _load_pipeline({})
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[{"id": "clip.mp4-0"}],
        qwen_model_path="m", use_gpu=False, audio_profile={})

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
        qwen_model_path="m", use_gpu=False, audio_profile={})
    assert info[ns["_QWEN_COMPLETED_KEY"]] is False, why


def test_tags_are_still_merged_when_the_worker_completes():
    ns = _load_pipeline(_worker_ok(tag_count=1))
    candidates = [{"id": f"clip.mp4-{i}"} for i in range(3)]

    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=candidates,
        qwen_model_path="m", use_gpu=False, audio_profile={})

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert ns["_calls"]["merged"] == ["clip.mp4-0"]
    assert candidates[0]["semantic_action"] == 0.9


def test_max_windows_zero_reports_not_completed_and_never_calls_the_worker():
    ns = _load_pipeline(_worker_ok(), max_windows=0)
    try:
        info = ns["_annotate_candidates_with_qwen"](
            video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[{"id": "clip.mp4-0"}],
            qwen_model_path="m", use_gpu=False, audio_profile={})
    finally:
        os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)

    assert info[ns["_QWEN_COMPLETED_KEY"]] is False
    assert ns["_calls"]["worker_requests"] == 0


def test_no_selected_candidates_is_a_completed_no_op():
    ns = _load_pipeline(_worker_ok())
    info = ns["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\clip.mp4", fps=25.0, candidates=[],
        qwen_model_path="m", use_gpu=False, audio_profile={})

    assert info[ns["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_tag_count"] == 0
    assert ns["_calls"]["worker_requests"] == 0


# ---------------------------------------------------------------------------
# R2: inline (serial) AI completion truth (T1, T2, T3, T4)
# ---------------------------------------------------------------------------


def _inline(ns, **kwargs):
    defaults = dict(video_file=r"C:\src\clip.mp4", use_gpu=False, enable_ai=True,
                    qwen_model_path="m", audio_profile={}, defer_ai=False)
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
    ns = _load_pipeline(_worker_ok(tag_count=2))
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
