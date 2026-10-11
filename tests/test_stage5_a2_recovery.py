"""A2-S1: the parent recovers finished jobs from this invocation's per-job partials.

The defect A2-S1 addresses is a *write-ordering* consequence, not a parsing bug. The shared Qwen
worker writes its response JSON once, after its entire job loop, so a worker that died on job 100 of
100 made the parent discard 99 jobs of finished GPU inference: `_complete_deferred_qwen_batch`'s
empty-response branch marked every job incomplete and returned. A1 made the worker publish one atomic
partial per finished job; this is the half that reads them.

**What is deliberately NOT built here**, because each would be a different contract:

* **no cross-invocation recovery.** Discovery is anchored on the response path of the worker this
  very call launched, and `load_partial` refuses a foreign ``invocation``. A *later* run still
  re-analyses any source that was never checkpointed.
* **no second completion rule.** `_recover_jobs_from_partials` rebuilds one response envelope and
  decides nothing. `_qwen_job_completed` and `_cache_entry_is_complete` stay the only rules and are
  applied by the *unchanged* per-job merge loop against the *live* `selected_by_job` candidate ids.
* **no durability upgrade and no cleanup.** A recovered partial is read and left on disk.

Technique, per `.claude/rules/test-harness.md`: `video_analysis.py` cannot be imported on a bare
interpreter, so the real `_complete_deferred_qwen_batch` and `_recover_jobs_from_partials` bodies are
AST-extracted and **executed** with only the worker subprocess stubbed — the recovery code under test
is the production code. The partials it reads are **real files written by the real
`beatsync_fork.qwen_partials.write_partial`**, so the filename contract, the atomic writer and the
reader are exercised end to end. No GPU, no portable runtime, no Qwen, no FFmpeg.

The worker stub fills `artifact_sink` exactly as production does — before "launching" — which is what
makes a dead-worker scenario reproducible at all.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import tempfile
import time
from typing import Any, Dict

import pytest

from beatsync_fork import qwen_partials as qp

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO, "src", "video_analysis.py")

# Everything the two extracted orchestration bodies resolve from their namespace. The coupling is the
# documented price of executing real upstream bodies (CLAUDE.md / test-harness.md): a helper those
# bodies call must appear here or the exec fails with `NameError`.
_DEPS = (
    "_safe_name", "_hash_text", "_same_source", "_coerce_count", "_is_count", "_reported_count",
    "_stored_ai_cache_is_consistent", "_qwen_job_completed", "_deterministic_analysis_completed",
    "_cache_entry_is_complete", "_load_cache", "_save_cache", "_checkpoint_cache",
    "_as_mapping", "_bounded_count", "_is_nonnegative_count", "_is_real_number",
    "_optional_telemetry_number", "_telemetry_seconds", "_telemetry_text", "_telemetry_total",
    "_fmt_seconds", "_qwen_max_windows",
    "_recover_jobs_from_partials", "_complete_deferred_qwen_batch",
)
_IDENTITY_DEPS = ("_safe_name", "_hash_text", "_bounded_fingerprint", "_video_signature",
                  "_cache_path", "_qwen_config_token", "_env_int", "_qwen_max_windows")
_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_QWEN_COMPLETED_KEY",
           "_QWEN_SINGLE_JOB_ID", "_DETERMINISTIC_SCORING_KEY",
           "_FINGERPRINT_CHUNK", "_FINGERPRINT_WHOLE_FILE_LIMIT", "_FINGERPRINT_DIGEST_SIZE",
           "_NO_AI_CONFIG_TOKEN")


def _source() -> str:
    with open(_VA, "r", encoding="utf-8") as handle:
        return handle.read()


def _tree() -> ast.Module:
    return ast.parse(_source(), filename=_VA)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in video_analysis.py")


def _body_code(node: ast.FunctionDef) -> str:
    """Executable statements only, so prose can neither satisfy nor fail a check."""
    return "\n".join(
        ast.unparse(stmt) for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    )


class _CountingPartials:
    """The real artifact contract, with every reader call counted.

    This is how "a usable response performs **no** partial lookup" becomes a runtime fact rather than
    an inference from where the call happens to sit in the source.
    """

    def __init__(self, real=qp, find_error=None, load_error=None):
        self._real = real
        self._find_error = find_error
        self._load_error = load_error
        self.find_calls = 0
        self.load_calls = 0

    def invocation_id(self, response_path):
        return self._real.invocation_id(response_path)

    def partial_glob(self, response_path):
        return self._real.partial_glob(response_path)

    def find_partials(self, response_path):
        self.find_calls += 1
        if self._find_error is not None:
            raise self._find_error
        return self._real.find_partials(response_path)

    def load_partial(self, path, *, expected_invocation):
        self.load_calls += 1
        if self._load_error is not None:
            raise self._load_error
        return self._real.load_partial(path, expected_invocation=expected_invocation)


def _namespace(worker_response, *, response_path=None, partials=None, names=_DEPS):
    """Execute the real production bodies with only the worker subprocess stubbed."""
    source = _source()
    tree = ast.parse(source)
    found = {node.name: ast.get_source_segment(source, node) for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names}
    missing = set(names) - set(found)
    assert not missing, f"could not extract {missing}"
    # Executed in source order rather than `literal_eval`'d, because
    # `_FINGERPRINT_WHOLE_FILE_LIMIT` is derived from `_FINGERPRINT_CHUNK` and is not a literal.
    consts: Dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _CONSTS for t in node.targets):
            module = ast.Module(body=[node], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, "<consts>", "exec"), consts)  # noqa: S102
    consts.pop("__builtins__", None)

    merged: list = []
    sink_seen: Dict[str, Any] = {}

    def worker(**kwargs):
        # Production fills the sink BEFORE the launch, so the paths survive every failure mode.
        artifact_sink = kwargs.get("artifact_sink")
        sink_seen["kwargs"] = sorted(kwargs)
        sink_seen["sink"] = artifact_sink
        if artifact_sink is not None and response_path is not None:
            artifact_sink["request_path"] = str(response_path).replace("response", "request")
            artifact_sink["response_path"] = str(response_path)
        return worker_response

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile, "math": math, "time": time,
        "hashlib": hashlib,
        "fork_partials": partials if partials is not None else qp,
        "Any": Any, "Dict": Dict, "List": list, "Sequence": list, "Iterable": list,
        "__builtins__": __builtins__,
        "_select_ai_candidates": lambda candidates, limit: list(candidates)[:limit],
        # faithful to the real `_merge_semantic`, which also sets `ai_analyzed`
        "_merge_semantic": lambda candidate, semantic: (
            merged.append(candidate["id"]),
            candidate.update({"semantic_action": semantic.get("action", 0.0),
                              "ai_analyzed": True}))[1],
        "_run_qwen_worker_batch": worker,
    }
    namespace.update(consts)
    for name in names:
        exec(compile("from __future__ import annotations\n" + found[name],  # noqa: S102
                     f"<{name}>", "exec"), namespace)
    os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)
    namespace["_merged"] = merged
    namespace["_sink_seen"] = sink_seen
    return namespace


def _record(ns, name, n=3, path=None):
    """A deferred (AI-pending) source record, the shape the batch path receives."""
    return {
        "analysis_version": ns["ANALYSIS_VERSION"], "cache_contract": ns["CACHE_CONTRACT_VERSION"],
        "video_file": path or rf"C:\src\{name}", "source_name": name,
        "duration": 30.0, "fps": 25.0, "width": 1280, "height": 720, "scene_changes": [1.0],
        "candidate_count": n,
        "candidates": [{"id": f"{name}-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                        "editorial_score": 0.5 - i * 0.01} for i in range(n)],
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0, "candidate_scoring_seconds": 0.4},
        "ai_enabled": False, "ai_deferred": True,
    }


def _run_stats():
    return {"qwen_jobs": 0, "qwen_completed_jobs": 0, "qwen_incomplete_jobs": 0,
            "qwen_requested_count": 0, "qwen_frame_count": 0, "qwen_tag_count": 0,
            "qwen_seconds": 0.0, "qwen_inference_seconds": 0.0}


def _semantics(name, count):
    return {f"{name}-{i}": {"action": 0.9, "emotion": "calm"} for i in range(count)}


def _timings(frames, tags):
    return {"frame_count": frames, "tag_count": tags,
            "inference_seconds": 5.0, "prefetch_seconds": 0.1}


def _write_partial(response_path, job_index, job_id, *, video_file, name, frames=3, tags=3,
                   records=3, backend=None, invocation=None, contract=None, raw=None):
    """Publish one partial through the REAL writer, optionally corrupting one declared field."""
    target = qp.partial_path(str(response_path), job_index, job_id)
    if raw is not None:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(raw)
        return target
    payload = qp.build_payload(
        response_path=str(response_path), job_index=job_index, job_total=4, job_id=job_id,
        video_file=video_file, semantics=_semantics(name, records),
        timings=_timings(frames, tags),
        backend=backend if backend is not None else {
            "model_id": "qwen3vl-2b", "batch_size": 4, "model_load_seconds": 8.0},
    )
    if invocation is not None:
        payload["invocation"] = invocation
    if contract is not None:
        payload["partial_contract"] = contract
    assert qp.write_partial(target, payload) == target
    return target


def _dead_worker():
    """What `_run_qwen_worker_batch` returns when the worker died, timed out or failed to launch."""
    return {}


def _usable_response(jobs):
    """jobs: {job_id: (frames, tags, records, name)}"""
    semantics, timings = {}, {}
    for job_id, (frames, tags, records, name) in jobs.items():
        semantics[job_id] = _semantics(name, records)
        timings[job_id] = _timings(frames, tags)
    return {"model_load_seconds": 8.0, "model_id": "live", "batch_size": len(jobs),
            "peak_vram_gb": 3.0, "semantics_by_job": semantics, "timings_by_job": timings}


def _four_jobs(ns, tmp_path):
    """Four deferred sources, their cache paths, and the job/record pairs the batch consumes."""
    items, paths, records = [], [], []
    for index in range(1, 5):
        name = f"s{index}.mp4"
        record = _record(ns, name, path=str(tmp_path / "src" / name))
        cache_file = str(tmp_path / f"cache{index}.json")
        items.append(({"index": index, "cache_file": cache_file}, record))
        paths.append(cache_file)
        records.append(record)
    return items, paths, records


# ---------------------------------------------------------------------------
# A. THE SEAM: the response path has to reach the caller at all
# ---------------------------------------------------------------------------


def test_a_the_worker_is_called_with_an_artifact_sink():
    ns = _namespace(_dead_worker())
    ns["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": ""}, _record(ns, "a.mp4"))],
        use_gpu=False, qwen_model_path="m", total_video_count=1)

    assert "artifact_sink" in ns["_sink_seen"]["kwargs"]
    assert isinstance(ns["_sink_seen"]["sink"], dict)


def test_a_the_sink_is_filled_before_the_launch_so_a_dead_worker_is_recoverable():
    """The load-bearing half: production fills it *before* `Popen`, not after a clean exit."""
    tree = _tree()
    runner = _func(tree, "_run_qwen_worker_batch")
    # Executable statements only - the docstring legitimately names `run_qwen_worker`.
    body = [stmt for stmt in runner.body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str))]
    fills = [i for i, stmt in enumerate(body)
             if isinstance(stmt, ast.If) and "artifact_sink is not None" in ast.unparse(stmt.test)]
    launches = [i for i, stmt in enumerate(body) if isinstance(stmt, ast.Try)]
    assert len(fills) == 1, "exactly one sink fill"
    assert launches and fills[0] < min(launches), "the sink must be filled before the launch"
    # ...and before the request file is even written, so nothing about the attempt can lose it.
    writes = [i for i, stmt in enumerate(body) if "open(request_path" in ast.unparse(stmt)]
    assert writes and fills[0] < writes[0]


def test_a_the_runner_keeps_its_signature_contract():
    runner = _func(_tree(), "_run_qwen_worker_batch")
    args = [a.arg for a in runner.args.args]
    kwonly = [a.arg for a in runner.args.kwonlyargs]

    assert "event_callback" in args, "progress forwarding must be untouched"
    assert kwonly == ["artifact_sink"], "the sink is keyword-only"
    assert runner.returns is not None and ast.unparse(runner.returns) == "Dict", (
        "the return type must stay the response mapping - two suites stub this call")


def test_a_find_partials_agrees_with_the_published_glob(tmp_path):
    """`partial_glob` stays the declared pattern; `find_partials` is it, applied without `glob`."""
    import glob as globmod

    response = str(tmp_path / "qwen_batch_response_tok.json")
    for index in (1, 2, 3):
        _write_partial(response, index, str(index), video_file="/v/x.mp4", name="x.mp4")
    # ...plus the two shapes that must never be returned
    with open(qp.partial_path(response, 4, "4") + ".q7x1.tmp", "w", encoding="utf-8") as handle:
        handle.write("{half")
    _write_partial(str(tmp_path / "qwen_batch_response_OTHER.json"), 1, "1",
                   video_file="/v/x.mp4", name="x.mp4")

    assert qp.find_partials(response) == sorted(globmod.glob(qp.partial_glob(response)))
    assert len(qp.find_partials(response)) == 3


# ---------------------------------------------------------------------------
# B. CASE 1 — zero partials means exactly today's behaviour
# ---------------------------------------------------------------------------


def test_b_no_partials_reproduces_the_original_empty_response_branch(tmp_path, capsys):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, records = _four_jobs(ns, tmp_path)
    stats = _run_stats()

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=stats)

    out = capsys.readouterr().out
    assert "returned no semantic response; AI analysis will retry on the next run." in out
    assert "Recovered" not in out
    for record in records:
        assert record["ai_enabled"] is False and record["ai_deferred"] is False
    assert not [p for p in paths if os.path.exists(p)], "nothing may be checkpointed"
    assert stats["qwen_jobs"] == 4 and stats["qwen_incomplete_jobs"] == 4
    assert stats["qwen_completed_jobs"] == 0
    assert stats["qwen_frame_count"] == 0 and stats["qwen_tag_count"] == 0


def test_b_an_absent_sink_path_is_simply_no_recovery(tmp_path):
    """`response_path=None` - the shape every pre-existing suite's stub produces."""
    ns = _namespace(_dead_worker(), response_path=None)
    items, paths, _records = _four_jobs(ns, tmp_path)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert not [p for p in paths if os.path.exists(p)]


# ---------------------------------------------------------------------------
# C. CASE 2 — the primary scenario: the worker died after two of four jobs
# ---------------------------------------------------------------------------


def test_c_death_after_two_of_four_recovers_exactly_those_two(tmp_path, capsys):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, records = _four_jobs(ns, tmp_path)
    response = str(tmp_path / "qwen_batch_response_tok.json")
    for index in (1, 2):
        _write_partial(response, index, str(index),
                       video_file=records[index - 1]["video_file"], name=f"s{index}.mp4")
    stats = _run_stats()

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=stats)

    out = capsys.readouterr().out
    assert "Recovered 2 of 4 Qwen job(s)" in out
    # the two that finished are AI-complete and durable
    for index in (1, 2):
        assert records[index - 1]["ai_enabled"] is True
        assert os.path.exists(paths[index - 1])
        assert ns["_load_cache"](paths[index - 1], require_ai=True,
                                 expected_video_file=records[index - 1]["video_file"]) is not None
    # the two that never ran are incomplete and are NOT cached
    for index in (3, 4):
        assert records[index - 1]["ai_enabled"] is False
        assert not os.path.exists(paths[index - 1])
    # submission counted 4, exactly 2 promoted - no double counting in either direction
    assert stats["qwen_jobs"] == 4
    assert stats["qwen_completed_jobs"] == 2
    assert stats["qwen_incomplete_jobs"] == 2
    assert stats["qwen_tag_count"] == 6, "3 merged candidates for each recovered job"
    assert stats["qwen_frame_count"] == 6


def test_c_the_recovered_semantics_are_really_merged_into_candidates(tmp_path):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, _paths, records = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=records[0]["video_file"], name="s1.mp4")

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert sorted(ns["_merged"]) == ["s1.mp4-0", "s1.mp4-1", "s1.mp4-2"]
    assert all(c.get("ai_analyzed") for c in records[0]["candidates"])
    assert not any(c.get("ai_analyzed") for c in records[2]["candidates"])


def test_c_a_recovered_job_still_goes_through_the_unchanged_completion_rule(tmp_path):
    """Recovery supplies evidence; `_qwen_job_completed` still decides. 3 requested, 2 tagged."""
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, records = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=records[0]["video_file"], name="s1.mp4",
                   frames=3, tags=2, records=2)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert records[0]["ai_enabled"] is False, "an incomplete job is still incomplete"
    assert not os.path.exists(paths[0])
    assert len(ns["_merged"]) == 2, "the tags that DID arrive are still merged"


# ---------------------------------------------------------------------------
# D. CASE 3 / 12 / 13 — every rejection shape, each independent of its siblings
# ---------------------------------------------------------------------------


def _reject_case(tmp_path, kind):
    response = str(tmp_path / "qwen_batch_response_tok.json")
    good_name, bad_name = "s1.mp4", "s2.mp4"
    good_video = str(tmp_path / "src" / good_name)
    bad_video = str(tmp_path / "src" / bad_name)
    if kind == "invalid_json":
        _write_partial(response, 2, "2", video_file=bad_video, name=bad_name, raw="not json at all")
    elif kind == "truncated_json":
        _write_partial(response, 2, "2", video_file=bad_video, name=bad_name,
                       raw='{"partial_contract": "stage5_job_partial_v1", "semantics": {"a"')
    elif kind == "json_not_an_object":
        _write_partial(response, 2, "2", video_file=bad_video, name=bad_name, raw="[1, 2, 3]")
    elif kind == "wrong_contract":
        _write_partial(response, 2, "2", video_file=bad_video, name=bad_name,
                       contract="stage5_job_partial_v99")
    elif kind == "wrong_invocation":
        _write_partial(response, 2, "2", video_file=bad_video, name=bad_name,
                       invocation="qwen_batch_response_SOMEONE_ELSE")
    elif kind == "unknown_job_id":
        _write_partial(response, 2, "99", video_file=bad_video, name=bad_name)
    elif kind == "wrong_source":
        _write_partial(response, 2, "2", video_file=str(tmp_path / "src" / "somebody_else.mp4"),
                       name=bad_name)
    elif kind == "semantics_not_a_mapping":
        target = qp.partial_path(response, 2, "2")
        payload = qp.build_payload(
            response_path=response, job_index=2, job_total=4, job_id="2", video_file=bad_video,
            semantics={}, timings=_timings(3, 3))
        payload["semantics"] = ["not", "a", "mapping"]
        qp.write_partial(target, payload)
    elif kind == "timings_not_a_mapping":
        target = qp.partial_path(response, 2, "2")
        payload = qp.build_payload(
            response_path=response, job_index=2, job_total=4, job_id="2", video_file=bad_video,
            semantics=_semantics(bad_name, 3), timings={})
        payload["timings"] = "12.0"
        qp.write_partial(target, payload)
    elif kind == "stale_other_invocation":
        _write_partial(str(tmp_path / "qwen_batch_response_PREVIOUS.json"), 2, "2",
                       video_file=bad_video, name=bad_name)
    elif kind == "leftover_temp":
        target = qp.partial_path(response, 2, "2")
        with open(target + ".a1b2c3.tmp", "w", encoding="utf-8") as handle:
            json.dump(qp.build_payload(
                response_path=response, job_index=2, job_total=4, job_id="2",
                video_file=bad_video, semantics=_semantics(bad_name, 3),
                timings=_timings(3, 3)), handle)
    else:  # pragma: no cover - guards the parametrisation itself
        raise AssertionError(kind)
    return response, good_video


@pytest.mark.parametrize("kind", [
    "invalid_json", "truncated_json", "json_not_an_object", "wrong_contract", "wrong_invocation",
    "unknown_job_id", "wrong_source", "semantics_not_a_mapping", "timings_not_a_mapping",
    "stale_other_invocation", "leftover_temp",
])
def test_d_one_bad_partial_never_costs_a_valid_sibling(tmp_path, kind):
    """Job 2's partial is broken in one declared way; job 1's must still be recovered."""
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, records = _four_jobs(ns, tmp_path)
    response, _good = _reject_case(tmp_path, kind)
    _write_partial(response, 1, "1", video_file=records[0]["video_file"], name="s1.mp4")

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert records[0]["ai_enabled"] is True, f"the valid sibling must survive {kind}"
    assert os.path.exists(paths[0])
    assert records[1]["ai_enabled"] is False, f"{kind} must be rejected"
    assert not os.path.exists(paths[1])


@pytest.mark.parametrize("kind", ["stale_other_invocation", "leftover_temp"])
def test_d_a_stale_or_temporary_file_is_never_even_enumerated(tmp_path, kind):
    """Rejection is good; not discovering it at all is the actual contract."""
    response, _good = _reject_case(tmp_path, kind)
    counting = _CountingPartials()
    ns = _namespace(_dead_worker(), response_path=response, partials=counting)
    items, _paths, _records = _four_jobs(ns, tmp_path)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert counting.find_calls == 1
    assert counting.load_calls == 0, f"{kind} must not be opened at all"


def test_d_a_foreign_invocation_id_is_refused_by_the_reader_itself(tmp_path):
    """Belt and braces: even a correctly *named* file must carry this invocation's id."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    target = _write_partial(response, 1, "1", video_file="/v/a.mp4", name="a.mp4",
                            invocation="qwen_batch_response_ELSEWHERE")

    assert qp.load_partial(target, expected_invocation="qwen_batch_response_tok") is None
    assert qp.load_partial(target,
                           expected_invocation="qwen_batch_response_ELSEWHERE") is not None


def test_d_the_reader_never_raises_on_any_hostile_input(tmp_path):
    directory = tmp_path / "d"
    directory.mkdir()
    for bad in [str(tmp_path / "missing.json"), str(directory), "", None, 12345]:
        assert qp.load_partial(bad, expected_invocation="x") is None
    target = str(tmp_path / "p.partial.json")
    with open(target, "wb") as handle:
        handle.write(b"\xff\xfe\x00broken")
    assert qp.load_partial(target, expected_invocation="x") is None
    for bad_invocation in ["", None, 7]:
        assert qp.load_partial(target, expected_invocation=bad_invocation) is None


# ---------------------------------------------------------------------------
# E. CASE 4 / 16 — the final response always wins
# ---------------------------------------------------------------------------


def test_e_a_usable_response_wins_and_no_partial_is_ever_looked_at(tmp_path, capsys):
    """Partials deliberately disagree with the response. The response must be used, unexamined."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    counting = _CountingPartials()
    ns = _namespace(_usable_response({"1": (3, 3, 3, "s1.mp4")}),
                    response_path=response, partials=counting)
    items, paths, records = _four_jobs(ns, tmp_path)
    # a contradictory partial for the SAME job, claiming a different model and different semantics
    _write_partial(response, 1, "1", video_file=records[0]["video_file"], name="s1.mp4",
                   backend={"model_id": "FROM-PARTIAL", "batch_size": 99,
                            "model_load_seconds": 999.0})

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert counting.find_calls == 0, "a usable response must not enumerate partials"
    assert counting.load_calls == 0, "a usable response must not read a partial"
    assert "Recovered" not in capsys.readouterr().out
    assert records[0]["ai_enabled"] is True and os.path.exists(paths[0])
    # telemetry came from the live response, never from the contradictory partial
    assert records[0]["timings"]["qwen_model_id"] == "live"
    assert records[0]["timings"]["qwen_concurrency"] == 1
    assert records[0]["timings"]["qwen_peak_vram_gb"] == 3.0


def test_e_a_usable_response_with_a_malformed_timing_envelope_still_uses_the_old_rule(tmp_path):
    """CASE 16: the response is usable, so recovery is irrelevant and completion is unchanged."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    counting = _CountingPartials()
    payload = _usable_response({"1": (3, 3, 3, "s1.mp4")})
    payload["timings_by_job"] = {"1": "not-a-mapping"}
    ns = _namespace(payload, response_path=response, partials=counting)
    items, paths, records = _four_jobs(ns, tmp_path)
    _write_partial(response, 1, "1", video_file=records[0]["video_file"], name="s1.mp4")

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert counting.find_calls == 0, "`semantics_by_job` was usable - do not touch partials"
    # no usable counts -> `_qwen_job_completed` refuses, exactly as before A2
    assert records[0]["ai_enabled"] is False
    assert not os.path.exists(paths[0])


def test_e_recovery_is_structurally_fenced_inside_the_unusable_response_guard():
    batch = _func(_tree(), "_complete_deferred_qwen_batch")
    calls = [n for n in ast.walk(batch) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_recover_jobs_from_partials"]
    guards = [n for n in ast.walk(batch) if isinstance(n, ast.If)
              and "not semantics_by_job" in ast.unparse(n.test)]

    assert len(calls) == 1 and guards
    assert any(any(n is calls[0] for n in ast.walk(guard)) for guard in guards)


# ---------------------------------------------------------------------------
# F. CASE 5 / 6 — completion and cache boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frames, tags, records, why", [
    (3, 0, 0, "every candidate's semantic failed"),
    (3, 2, 2, "partial semantic failure"),
    (0, 0, 0, "prefetch decoded nothing"),
    (2, 2, 2, "fewer frames decoded than requested"),
])
def test_f_an_incomplete_recovered_job_is_never_checkpointed(tmp_path, frames, tags,
                                                             records, why):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=recs[0]["video_file"], name="s1.mp4",
                   frames=frames, tags=tags, records=records)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert recs[0]["ai_enabled"] is False, why
    assert not os.path.exists(paths[0]), why
    assert recs[0]["candidates"], "deterministic candidates are always kept"


def test_f_an_existing_complete_record_is_not_overwritten_by_an_incomplete_recovery(tmp_path):
    """CASE 6: byte-for-byte. `_checkpoint_cache` refuses, so the good record never moves."""
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, recs = _four_jobs(ns, tmp_path)
    # a pre-existing, genuinely AI-complete record at job 1's cache path
    good = dict(recs[0])
    good["ai_deferred"] = False
    good["ai_enabled"] = True
    good["candidates"] = [dict(c, ai_analyzed=True) for c in recs[0]["candidates"]]
    good["timings"] = {"total_seconds": 12.0, "candidate_scoring_seconds": 0.4,
                       "qwen_frame_count": 3, "qwen_tag_count": 3}
    ns["_save_cache"](paths[0], good)
    with open(paths[0], "rb") as handle:
        before = handle.read()
    assert ns["_load_cache"](paths[0], require_ai=True,
                             expected_video_file=good["video_file"]) is not None
    # now recover an INCOMPLETE result for the same job
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=recs[0]["video_file"], name="s1.mp4", frames=3, tags=1, records=1)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    with open(paths[0], "rb") as handle:
        assert handle.read() == before, "a complete record must survive an incomplete recovery"


def test_f_the_completion_and_cache_primitives_are_untouched_by_a2():
    """Their bodies may not mention recovery in any form."""
    tree = _tree()
    for name in ("_qwen_job_completed", "_cache_entry_is_complete",
                 "_stored_ai_cache_is_consistent", "_checkpoint_cache", "_save_cache",
                 "_load_cache"):
        body = _body_code(_func(tree, name)).lower()
        for banned in ("partial", "recover", "invocation", "artifact_sink"):
            assert banned not in body, f"{name} must not mention {banned!r}"


def test_f_the_recovery_helper_decides_nothing():
    body = _body_code(_func(_tree(), "_recover_jobs_from_partials"))
    for banned in ("_qwen_job_completed", "_cache_entry_is_complete", "_checkpoint_cache",
                   "_save_cache", "ai_enabled", "ai_deferred", "frame_count", "tag_count"):
        assert banned not in body, f"recovery must not reference {banned}"


# ---------------------------------------------------------------------------
# G. CASE 7 — accounting
# ---------------------------------------------------------------------------


def test_g_run_stats_are_not_double_counted_by_recovery(tmp_path):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, _paths, recs = _four_jobs(ns, tmp_path)
    for index in (1, 2, 3):
        _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), index, str(index),
                       video_file=recs[index - 1]["video_file"], name=f"s{index}.mp4")
    stats = _run_stats()

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=stats)

    assert stats["qwen_jobs"] == 4, "submission truth, counted once"
    assert stats["qwen_completed_jobs"] == 3
    assert stats["qwen_incomplete_jobs"] == 1
    assert stats["qwen_completed_jobs"] + stats["qwen_incomplete_jobs"] == stats["qwen_jobs"]
    assert stats["qwen_requested_count"] == 12, "4 jobs x 3 candidates, counted at submission"
    assert stats["qwen_tag_count"] == 9
    assert stats["qwen_frame_count"] == 9
    assert stats["qwen_inference_seconds"] == pytest.approx(15.0), "3 recovered jobs x 5.0s"
    assert stats["qwen_seconds"] >= 0.0 and math.isfinite(stats["qwen_seconds"])


def test_g_recovery_adds_no_run_stats_key_and_no_increment():
    helper = _body_code(_func(_tree(), "_recover_jobs_from_partials"))
    assert "run_stats" not in helper, "recovery must not touch accounting at all"

    batch = _func(_tree(), "_complete_deferred_qwen_batch")
    blocks = [n for n in ast.walk(batch) if isinstance(n, ast.If)
              and "run_stats is not None" in ast.unparse(n.test)]
    assert len(blocks) == 2, "still exactly two accounting blocks: submission, then response"


def test_g_the_batch_wall_time_is_still_added_exactly_once(tmp_path):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, _paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=recs[0]["video_file"], name="s1.mp4")
    stats = _run_stats()

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=stats)

    batch = _func(_tree(), "_complete_deferred_qwen_batch")
    adds = [n for n in ast.walk(batch) if isinstance(n, ast.Call)
            and "batch_seconds" in ast.unparse(n) and "_telemetry_total" in ast.unparse(n)]
    assert len(adds) == 1, "one measured worker invocation, added once"


def test_g_a_non_recovered_job_takes_the_EXISTING_absent_envelope_path(tmp_path, capsys):
    """The one behavioural delta for jobs with no partial, asserted rather than assumed.

    Before A2 an unusable response returned early, so a job without evidence got only
    `ai_deferred=False`/`ai_enabled=False`. Now the merge loop runs, so such a job takes exactly the
    path a job *absent from a usable response* has always taken. The consequences are confined, and
    all three are pinned here:

    * `run_stats` - the current-run truth - is unaffected: `_bounded_count(None, n)` is 0;
    * the record gains in-memory `timings`, whose `qwen_frame_count` falls back to the requested
      count. That fallback is pre-existing and documented ("not evidence of decoding"), and it
      reaches no cache, because the record is never checkpointed;
    * one extra "semantic pass incomplete" console line per non-recovered job.
    """
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=recs[0]["video_file"], name="s1.mp4")
    stats = _run_stats()

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=stats)

    out = capsys.readouterr().out
    assert out.count("Qwen semantic pass incomplete") == 3
    # current-run truth is untouched by the fallback
    assert stats["qwen_frame_count"] == 3 and stats["qwen_tag_count"] == 3
    # the fallback is visible in memory only, and never persisted
    assert recs[2]["timings"]["qwen_frame_count"] == 3
    assert recs[2]["timings"]["qwen_tag_count"] == 0
    assert not os.path.exists(paths[2]), "and it reaches no cache record"
    assert recs[2]["ai_enabled"] is False


# ---------------------------------------------------------------------------
# H. CASE 8 / 9 — failure containment
# ---------------------------------------------------------------------------


def test_h_a_parent_failure_during_checkpointing_keeps_earlier_jobs_durable(tmp_path):
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"))
    items, paths, recs = _four_jobs(ns, tmp_path)
    for index in (1, 2, 3):
        _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), index, str(index),
                       video_file=recs[index - 1]["video_file"], name=f"s{index}.mp4")

    real_save = ns["_save_cache"]
    calls = {"n": 0}

    def exploding_save(path, data):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device")
        real_save(path, data)

    ns["_save_cache"] = exploding_save

    with pytest.raises(OSError):
        ns["_complete_deferred_qwen_batch"](
            video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert os.path.exists(paths[0]) and os.path.exists(paths[1]), (
        "jobs checkpointed before the failure stay durable - the D1 guarantee")
    assert not os.path.exists(paths[2])


@pytest.mark.parametrize("error", [
    OSError(13, "Permission denied"), RuntimeError("boom"), ValueError("nope"),
])
def test_h_a_discovery_failure_degrades_to_todays_behaviour(tmp_path, error, capsys):
    counting = _CountingPartials(find_error=error)
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"),
                    partials=counting)
    items, paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), 1, "1",
                   video_file=recs[0]["video_file"], name="s1.mp4")

    ns["_complete_deferred_qwen_batch"](  # must not raise
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    out = capsys.readouterr().out
    assert "recovery unavailable" in out
    assert "will retry on the next run" in out
    assert not [p for p in paths if os.path.exists(p)]
    for record in recs:
        assert record["ai_enabled"] is False and record["ai_deferred"] is False


def test_h_a_reader_failure_degrades_to_todays_behaviour(tmp_path):
    counting = _CountingPartials(load_error=RuntimeError("reader exploded"))
    ns = _namespace(_dead_worker(), response_path=str(tmp_path / "qwen_batch_response_tok.json"),
                    partials=counting)
    items, paths, recs = _four_jobs(ns, tmp_path)
    for index in (1, 2):
        _write_partial(str(tmp_path / "qwen_batch_response_tok.json"), index, str(index),
                       video_file=recs[index - 1]["video_file"], name=f"s{index}.mp4")

    ns["_complete_deferred_qwen_batch"](  # must not raise
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert not [p for p in paths if os.path.exists(p)]


def test_h_a_missing_cache_directory_is_simply_no_partials(tmp_path):
    ns = _namespace(_dead_worker(),
                    response_path=str(tmp_path / "nope" / "qwen_batch_response_tok.json"))
    items, paths, _recs = _four_jobs(ns, tmp_path)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert not [p for p in paths if os.path.exists(p)]
    assert qp.find_partials(str(tmp_path / "nope" / "qwen_batch_response_tok.json")) == []


def test_h_find_partials_never_raises_on_hostile_input(tmp_path):
    for bad in ["", None, 12345, str(tmp_path / "missing" / "x.json"), str(tmp_path)]:
        assert qp.find_partials(bad) == []


# ---------------------------------------------------------------------------
# I. CASE 11 / 14 — telemetry selection and duplicate handling
# ---------------------------------------------------------------------------


def test_i_backend_telemetry_comes_from_the_highest_validated_job_index(tmp_path):
    """`load_seconds` accumulates and `batch_size` is reassigned by a mid-batch slot restart."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, _paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(response, 1, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   backend={"model_id": "m", "batch_size": 8, "model_load_seconds": 8.0})
    _write_partial(response, 2, "2", video_file=recs[1]["video_file"], name="s2.mp4",
                   backend={"model_id": "m", "batch_size": 4, "model_load_seconds": 15.5})

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=_run_stats())

    # 15.5s of model load, amortised over the 4 submitted jobs
    assert recs[0]["timings"]["qwen_model_load_seconds_amortized"] == pytest.approx(15.5 / 4)
    assert recs[0]["timings"]["qwen_concurrency"] == 4, "the LAST partial's batch_size"
    assert recs[0]["timings"]["qwen_model_id"] == "m"


def test_i_telemetry_order_does_not_depend_on_filesystem_enumeration(tmp_path):
    """The selection key is the validated `job_index` in the payload, not the listing order."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, _paths, recs = _four_jobs(ns, tmp_path)
    # job 2 is written FIRST, and job 1's partial carries the later-looking numbers
    _write_partial(response, 2, "2", video_file=recs[1]["video_file"], name="s2.mp4",
                   backend={"model_id": "last", "batch_size": 2, "model_load_seconds": 40.0})
    _write_partial(response, 1, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   backend={"model_id": "first", "batch_size": 16, "model_load_seconds": 10.0})

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert recs[0]["timings"]["qwen_model_id"] == "last", "job_index 2 wins, not write order"
    assert recs[0]["timings"]["qwen_concurrency"] == 2


def test_i_peak_vram_is_unknown_rather_than_fabricated(tmp_path):
    """A partial carries no `peak_vram_gb`. `None` means unknown and must not become 0.0."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, _paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(response, 1, "1", video_file=recs[0]["video_file"], name="s1.mp4")

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert recs[0]["timings"]["qwen_peak_vram_gb"] is None
    helper = _body_code(_func(_tree(), "_recover_jobs_from_partials"))
    assert "peak_vram_gb" not in helper, "the field must be absent, not defaulted"


def test_i_a_missing_or_malformed_backend_group_leaves_telemetry_unknown(tmp_path):
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, paths, recs = _four_jobs(ns, tmp_path)
    target = qp.partial_path(response, 1, "1")
    payload = qp.build_payload(
        response_path=response, job_index=1, job_total=4, job_id="1",
        video_file=recs[0]["video_file"], semantics=_semantics("s1.mp4", 3),
        timings=_timings(3, 3), backend=None)
    qp.write_partial(target, payload)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    assert recs[0]["ai_enabled"] is True, "absent telemetry can never block completion"
    assert os.path.exists(paths[0])
    assert recs[0]["timings"]["qwen_model_id"] == ""
    assert recs[0]["timings"]["qwen_concurrency"] == 0
    assert recs[0]["timings"]["qwen_model_load_seconds_amortized"] == 0.0


def test_i_a_duplicate_job_id_is_rejected_deterministically_and_reported(tmp_path, capsys):
    """First valid wins - by `job_index` order, never by listing order - and the rest are refused."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, paths, recs = _four_jobs(ns, tmp_path)
    # two partials claiming job "1": index 2 is written first and carries different semantics
    _write_partial(response, 2, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   frames=3, tags=1, records=1)
    _write_partial(response, 1, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   frames=3, tags=3, records=3)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=_run_stats())

    out = capsys.readouterr().out
    assert "duplicate job id 1" in out, "the duplicate must be reported, not silently dropped"
    assert "the first valid partial already won" in out
    # job_index 1 sorts first, so the COMPLETE one won and the incomplete duplicate was refused
    assert recs[0]["ai_enabled"] is True
    assert os.path.exists(paths[0])
    assert len(ns["_merged"]) == 3


def test_i_duplicate_resolution_is_stable_under_the_reverse_write_order(tmp_path):
    """Same two files, written the other way round: the same partial must still win."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, paths, recs = _four_jobs(ns, tmp_path)
    _write_partial(response, 1, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   frames=3, tags=3, records=3)
    _write_partial(response, 2, "1", video_file=recs[0]["video_file"], name="s1.mp4",
                   frames=3, tags=1, records=1)

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4,
        run_stats=_run_stats())

    assert recs[0]["ai_enabled"] is True
    assert os.path.exists(paths[0])
    assert len(ns["_merged"]) == 3


# ---------------------------------------------------------------------------
# J. CASE 10 / 15 — identity invariance, and nothing is ever deleted
# ---------------------------------------------------------------------------


def test_j_cache_identity_is_byte_identical_with_and_without_partials(tmp_path, monkeypatch):
    """A2 is not allowed to perturb a single cache key."""
    ns = _namespace(_dead_worker(), names=_IDENTITY_DEPS)
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 8192)
    cache_dir = tmp_path / "cachedir"
    cache_dir.mkdir()
    monkeypatch.setenv("BEATSYNC_QWEN_MAX_WINDOWS", "120")
    ns["VIDEO_ANALYSIS_CACHE_DIR"] = str(cache_dir)

    before = ns["_cache_path"](str(source), False, None,
                               backend_token="tok", config_token=ns["_qwen_config_token"]())
    # litter the very directory recovery reads from
    response = str(cache_dir / "qwen_batch_response_tok.json")
    for index in (1, 2, 3):
        _write_partial(response, index, str(index), video_file=str(source), name="clip.mp4")
    after = ns["_cache_path"](str(source), False, None,
                              backend_token="tok", config_token=ns["_qwen_config_token"]())

    assert before is not None and before == after
    os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)


def test_j_the_identity_primitives_never_mention_recovery():
    tree = _tree()
    for name in ("_video_signature", "_cache_path", "_qwen_config_token", "_bounded_fingerprint"):
        body = _body_code(_func(tree, name)).lower()
        for banned in ("partial", "recover", "invocation", "artifact"):
            assert banned not in body, f"{name} must not mention {banned!r}"


def test_j_recovery_deletes_nothing(tmp_path):
    """CASE 15: every partial survives being recovered, and the module contains no removal."""
    response = str(tmp_path / "qwen_batch_response_tok.json")
    ns = _namespace(_dead_worker(), response_path=response)
    items, _paths, recs = _four_jobs(ns, tmp_path)
    targets = [
        _write_partial(response, index, str(index),
                       video_file=recs[index - 1]["video_file"], name=f"s{index}.mp4")
        for index in (1, 2)
    ]
    before = {t: open(t, "rb").read() for t in targets}

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", total_video_count=4)

    for target, payload in before.items():
        assert os.path.exists(target), "a recovered partial must survive"
        assert open(target, "rb").read() == payload, "and must not be rewritten"

    # The two reader functions perform no write and no removal. Executable code only: their
    # docstrings legitimately name `os.unlink`/`shutil.rmtree` in order to say the parent is the one
    # banned from them.
    import inspect

    reader_tree = ast.parse(
        "\n".join(inspect.getsource(fn) for fn in (qp.find_partials, qp.load_partial)))
    reader_code = "\n".join(
        _body_code(_func(reader_tree, name)) for name in ("find_partials", "load_partial"))
    for banned in ("os.remove", "os.unlink", "shutil", "os.replace", "'w'"):
        assert banned not in reader_code, f"the reader must not {banned}"


def test_j_the_parent_still_performs_no_directory_enumeration():
    """The "no cache sweeper" guard stays mechanically true: the listing lives in the fork module."""
    executable = ast.unparse(_tree())
    for banned in ("glob.glob", "os.scandir", "os.listdir", "os.walk", "shutil.rmtree",
                   "os.unlink"):
        assert banned not in executable, f"video_analysis.py must not contain {banned}"


# ---------------------------------------------------------------------------
# K. the legacy single-job path is untouched
# ---------------------------------------------------------------------------


def test_k_the_legacy_single_job_path_gained_nothing():
    tree = _tree()
    for name in ("_run_qwen_worker", "_complete_deferred_qwen", "_annotate_candidates_with_qwen"):
        body = _body_code(_func(tree, name))
        for banned in ("artifact_sink", "_recover_jobs_from_partials", "fork_partials",
                       "find_partials", "load_partial"):
            assert banned not in body, f"{name} must not gain {banned}"


def test_k_only_the_batch_runner_grew_a_sink():
    tree = _tree()
    single = _func(tree, "_run_qwen_worker")
    assert [a.arg for a in single.args.kwonlyargs] == [], (
        "the single-job runner keeps its exact signature")
    # and the retained request/response artifacts still have their empty `finally`
    for name in ("_run_qwen_worker", "_run_qwen_worker_batch"):
        body = ast.unparse(_func(tree, name))
        assert "Keep Qwen worker request/response files" in body or "pass" in body
