"""Stage 5 reports what THIS run did, not what the cached library remembers.

Production proved the defect on a fully warm library: 845/845 cache hits, zero sources analysed,
zero Qwen workers launched — and a console summary reading `visual workers: 1`,
`Qwen performance: … 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`. Every one of those
numbers came from cached records. The five-line budget then dropped the only line that described the
run: `Analysis time: …, cache 845/845`.

`gui.py` needs gradio and `video_analysis.py` needs the whole portable runtime, so neither can be
imported on the bare interpreter this suite runs on. Two techniques are used instead:

* the pure, stdlib-only pieces (`StageConsoleLogger`, `_fmt_stage_seconds`, `_stage5_summary`,
  `_new_run_stats`) are extracted with `ast` and executed, so the behavioural tests drive the real
  production code rather than a copy of it. The two accounting sites are written *inline* inside
  `_annotate_candidates_with_qwen` and `_complete_deferred_qwen_batch` rather than in a shared
  helper, because both of those functions are themselves AST-extracted and executed by
  `tests/test_stage5_cache_completion.py` and so must stay self-contained; the R2 tests below
  execute those real bodies with only the worker subprocess stubbed;
* the orchestration invariants that cannot be executed without CUDA are asserted structurally, by
  function and keyword rather than by grep, so a refactor that re-broadens a current-run count or
  restores the misleading wording fails the suite.

The production figures (845 / 8704 / 3031.9) appear here as *inputs* only. Nothing in the production
code may special-case them.
"""

from __future__ import annotations

import ast
import io
import os
import re
import time
import typing

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _extract(path: str, names: set[str], namespace: dict) -> dict:
    """Execute the named top-level defs/classes from a real source file, nothing else."""
    tree = _tree(path)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
              and n.name in names]
    missing = names - {n.name for n in picked}
    assert not missing, f"could not extract {missing} from {path}"
    module = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, path, "exec"), namespace)  # noqa: S102 - real source, isolated namespace
    return namespace


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            got = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if got == name:
                found.append(sub)
    return found


def _body_code(node: ast.AST) -> str:
    """Executable statements only — never the docstring, which would satisfy a name check."""
    return "\n".join(
        ast.unparse(stmt) for stmt in getattr(node, "body", [])
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    )


@pytest.fixture(scope="module")
def gui():
    ns: dict = {"time": time, "os": os, "re": re, "Dict": typing.Dict, "annotations": None}
    _extract(_GUI, {"StageConsoleLogger", "_fmt_stage_seconds", "_stage5_summary"}, ns)
    return ns


@pytest.fixture(scope="module")
def va():
    ns: dict = {"Dict": typing.Dict, "Any": typing.Any}
    _extract(_VA, {"_new_run_stats"}, ns)
    return ns


_RUN_STATS_KEYS = ("qwen_jobs", "qwen_completed_jobs", "qwen_incomplete_jobs",
                   "qwen_requested_count", "qwen_frame_count", "qwen_tag_count",
                   "qwen_seconds", "qwen_inference_seconds")


def _recording_blocks(fn: ast.AST) -> list[ast.If]:
    """The `if run_stats is not None:` accounting blocks, in source order.

    The single/inline path has one. The batch path has two, by design: submission truth (recorded the
    moment the shared worker returns, before the empty-response branch) and response truth.
    """
    blocks = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and "run_stats is not None" in ast.unparse(n.test)]
    assert blocks, "accounting block missing"
    return sorted(blocks, key=lambda b: b.lineno)


def _recording_block(fn: ast.AST) -> ast.If:
    return _recording_blocks(fn)[0]


class Recorder:
    """Stands in for StageConsoleLogger when the five-line budget is not under test."""

    def __init__(self):
        self.lines: list[str] = []

    def line(self, message: str) -> None:
        self.lines.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# Historical library aggregates, exactly as a fully warm production run returns them.
HISTORICAL = {
    "qwen_frame_count": 8704,
    "qwen_tag_count": 8704,
    "qwen_seconds": 3031.9,
    "qwen_inference_seconds": 2788.4,
    "qwen_model_id": "Qwen3VL-2B-Instruct-Q8_0 (llama.cpp Vulkan)",
    "qwen_concurrency": 4,
    "qwen_peak_vram_gb": 3.1,
}


def warm_result(**over):
    data = {
        "source_count": 845, "cache_hits": 845, "worker_count": 0,
        "sources_analyzed_this_run": 0, "analysis_workers_used": 0,
        "qwen_jobs_this_run": 0, "qwen_completed_jobs_this_run": 0,
        "qwen_incomplete_jobs_this_run": 0, "qwen_requested_count_this_run": 0,
        "qwen_frame_count_this_run": 0,
        "qwen_tag_count_this_run": 0, "qwen_seconds_this_run": 0.0,
        "qwen_inference_seconds_this_run": 0.0,
        "ai_enabled": True, "analysis_seconds": 69.0,
        "summary": "8704 visual moments, action=0.50, beauty=0.65, quality=0.76",
        **HISTORICAL,
    }
    data.update(over)
    return data


# ------------------------------------------------------------------ A. FULL WARM
def test_a_warm_run_reports_zero_current_work_and_no_historical_masquerade(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result())
    text = rec.text

    assert "analyzed this run 0" in text
    assert "no inference this run" in text
    assert "cache 845/845" in text
    assert "Analysis time" in text

    # the historical numbers must never be presented as this run's Qwen work
    assert "8704/8704" not in text
    assert "3031.9" not in text
    assert "candidates/s" not in text
    assert "Qwen performance" not in text
    assert "Qwen this run" not in text
    # any mention of the cached library must be explicitly labelled
    for line in rec.lines:
        if "8704" in line and "visual moments" not in line:
            assert "Cached" in line or "cached" in line, f"unlabelled historical line: {line!r}"


def test_a_warm_run_never_claims_an_analysis_worker_was_used(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result())
    assert "visual workers: 1" not in rec.text
    assert "workers: 1" not in rec.text


# ------------------------------------------------------------------ B. MIXED
def test_b_mixed_run_reports_only_current_run_qwen(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=843, sources_analyzed_this_run=2, analysis_workers_used=2, worker_count=2,
        qwen_jobs_this_run=2, qwen_completed_jobs_this_run=2,
        qwen_requested_count_this_run=20,
        qwen_frame_count_this_run=20, qwen_tag_count_this_run=20,
        qwen_seconds_this_run=14.0, analysis_seconds=72.0,
    ))
    text = rec.text

    assert "cache 843/845" in text
    assert "analyzed this run 2" in text
    assert "Qwen this run" in text
    assert "20/20" in text
    assert "2 job(s)" in text
    # the other 843 cached records must not contribute
    assert "8704" not in text.replace("8704 visual moments", "")
    assert "3031.9" not in text
    assert "no inference this run" not in text


# ------------------------------------------------------------------ C. COLD
def test_c_cold_run_renders_through_current_run_fields(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        source_count=3, cache_hits=0, sources_analyzed_this_run=3, analysis_workers_used=2,
        qwen_jobs_this_run=3, qwen_completed_jobs_this_run=3, qwen_requested_count_this_run=30,
        qwen_frame_count_this_run=30, qwen_tag_count_this_run=30, qwen_seconds_this_run=40.0,
        qwen_frame_count=30, qwen_tag_count=30, qwen_seconds=40.0,
        analysis_seconds=55.0, summary="30 visual moments",
    ))
    text = rec.text
    assert "cache 0/3" in text
    assert "analyzed this run 3" in text
    assert "30/30" in text and "3 job(s)" in text


# ------------------------------------------------------------------ D. QWEN DISABLED
def test_d_qwen_disabled_is_concise_and_carries_no_semantic_metrics(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(ai_enabled=False))
    text = rec.text
    assert "Qwen: disabled" in text
    assert "tags" not in text
    assert "8704/8704" not in text
    assert "Cached library" not in text
    assert "cache 845/845" in text


# ------------------------------------------------------------------ E. INCOMPLETE / FAILED
def test_e_incomplete_qwen_states_current_run_failure_truth(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=843, sources_analyzed_this_run=2, analysis_workers_used=2,
        qwen_jobs_this_run=2, qwen_completed_jobs_this_run=1, qwen_incomplete_jobs_this_run=1,
        qwen_requested_count_this_run=20,
        qwen_frame_count_this_run=20, qwen_tag_count_this_run=19, qwen_seconds_this_run=13.0,
    ))
    text = rec.text
    assert "19/20" in text
    assert "incomplete" in text
    assert "not cached" in text
    # must not fall back to the historical aggregate to look successful
    assert "8704/8704" not in text
    assert "3031.9" not in text


def test_e_failure_without_provable_timing_omits_rather_than_understates(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=844, sources_analyzed_this_run=1, qwen_jobs_this_run=1,
        qwen_completed_jobs_this_run=0, qwen_incomplete_jobs_this_run=1,
        qwen_requested_count_this_run=10,
        qwen_frame_count_this_run=0, qwen_tag_count_this_run=0, qwen_seconds_this_run=0.0,
    ))
    qwen_line = next(line for line in rec.lines if line.startswith("Qwen this run"))
    assert " in " not in qwen_line, f"fabricated/understated timing: {qwen_line!r}"
    assert "incomplete" in qwen_line


# ------------------------------------------------------------------ F. FIVE-LINE BUDGET
def test_f_analysis_time_survives_the_five_line_budget_in_a_warm_run(gui):
    stream = io.StringIO()
    logger = gui["StageConsoleLogger"](stream, max_lines_per_stage=5)
    logger.start_stage(5)
    gui["_stage5_summary"](logger, warm_result())
    out = stream.getvalue()

    assert "Analysis time" in out, "the authoritative line was pushed out of the budget"
    assert "cache 845/845" in out
    assert "analyzed this run 0" in out
    assert "8704/8704" not in out
    assert logger.max_lines_per_stage == 5, "the budget itself must not be raised"


def test_f_budget_default_is_still_five(gui):
    logger = gui["StageConsoleLogger"](io.StringIO())
    assert logger.max_lines_per_stage == 5


# ------------------------------------------------------------------ G. WORKER TRUTH
def test_g_zero_jobs_reports_zero_workers_and_nonzero_policy_survives():
    tree = _tree(_VA)
    code = _body_code(_func(tree, "analyze_video_sources"))
    assert "workers = _video_analysis_workers(len(jobs)) if jobs else 0" in code, (
        "zero-job worker truth missing or reshaped")

    # the underlying policy function must be untouched: one job still means one worker
    ns: dict = {"os": os, "_env_int": lambda name, default, lo, hi: default}
    _extract(_VA, {"_video_analysis_workers"}, ns)
    assert ns["_video_analysis_workers"](1) == 1
    assert ns["_video_analysis_workers"](0) == 1, (
        "the helper's own contract must stay unchanged; the call site provides the truth")
    assert ns["_video_analysis_workers"](8) >= 1


def test_g_structured_end_event_carries_current_run_truth():
    tree = _tree(_VA)
    code = _body_code(_func(tree, "analyze_video_sources"))
    for field in ("sources_analyzed_this_run", "qwen_jobs_this_run",
                  "qwen_tag_count_this_run", "qwen_frame_count_this_run"):
        assert field in code, f"{field} missing from the Stage-5 result/event"


# ------------------------------------------------------------------ H. SERIAL INLINE ANTI-DRIFT
def test_h_serial_inline_qwen_is_counted_although_it_never_enters_deferred_jobs():
    """The inline path runs Qwen with `defer_ai=False`, so `len(deferred_jobs)` would miss it."""
    tree = _tree(_VA)
    code = _body_code(_func(tree, "analyze_video_sources"))
    # `deferred_jobs` legitimately appears in dispatch and progress wording; what must never happen
    # is the current-run Qwen job COUNT being derived from its length.
    for line in code.splitlines():
        if "qwen_jobs_this_run" in line:
            assert "deferred_jobs" not in line, (
                f"current-run Qwen jobs derived from deferred_jobs: {line!r}")
    assert "run_stats['qwen_jobs']" in code or 'run_stats["qwen_jobs"]' in code, (
        "the current-run job count must come from the explicit accounting sink")

    # the facade records the job, and the inline caller forwards the accounting sink
    facade = _func(tree, "_annotate_candidates_with_qwen")
    _recording_block(facade)  # raises if the facade stopped recording
    single = _func(tree, "_analyze_single_video")
    forwarded = [c for c in _calls(single, "_annotate_candidates_with_qwen")
                 if "run_stats" in {kw.arg for kw in c.keywords if kw.arg}]
    assert forwarded, "the inline path stopped forwarding run_stats"


def test_h_run_stats_starts_at_zero_on_every_key(va):
    stats = va["_new_run_stats"]()
    assert set(stats) == set(_RUN_STATS_KEYS)
    assert all(stats[k] == 0 for k in _RUN_STATS_KEYS)
    # a second invocation must not inherit the first's counts
    assert va["_new_run_stats"]() is not stats


@pytest.mark.parametrize("fn_name", ["_annotate_candidates_with_qwen",
                                     "_complete_deferred_qwen_batch"])
def test_h_both_recording_sites_tally_every_key_and_split_completed(fn_name):
    """Across its accounting blocks, each path must touch every counter and split completion."""
    code = "\n".join(ast.unparse(b) for b in _recording_blocks(_func(_tree(_VA), fn_name)))
    for key in _RUN_STATS_KEYS:
        assert f"'{key}'" in code or f'"{key}"' in code, f"{fn_name} does not tally {key}"
    assert "qwen_completed_jobs" in code and "qwen_incomplete_jobs" in code, (
        f"{fn_name} does not split completed vs incomplete")
    # requested candidates and wall time are submission facts and must be recorded unconditionally
    assert "qwen_requested_count" in code and "qwen_seconds" in code


def test_h_recording_is_guarded_so_a_missing_sink_is_a_no_op():
    for fn_name in ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch"):
        for block in _recording_blocks(_func(_tree(_VA), fn_name)):
            assert "run_stats is not None" in ast.unparse(block.test)


def test_h_a_missing_sink_really_is_a_no_op_at_runtime():
    """Executed, not asserted structurally: run_stats=None must not raise."""
    ns = _pipeline_ns(("_annotate_candidates_with_qwen",), _run_qwen_worker=lambda **kw: {})
    ns["_annotate_candidates_with_qwen"](
        video_file="v.mp4", fps=25.0,
        candidates=[{"id": "x", "start": 0, "end": 1}], qwen_model_path="m",
        use_gpu=False, audio_profile={})  # no run_stats at all


# ------------------------------------------------------------------ I. CANDIDATE-LESS ANTI-DRIFT
def test_i_no_semantic_request_is_not_counted_as_a_qwen_job():
    """A source that reaches the worker-less branches must not inflate the current-run count."""
    tree = _tree(_VA)
    facade = _func(tree, "_annotate_candidates_with_qwen")

    block = _recording_block(facade)
    worker_calls = _calls(facade, "_run_qwen_worker")
    assert worker_calls, "the facade no longer issues a worker request"
    assert block.lineno > worker_calls[0].lineno, (
        "the job must be recorded only after a request is actually issued")

    # the two early returns - configured skip and no selected candidates - must not record
    for node in ast.walk(facade):
        if isinstance(node, ast.Return) and node.lineno < worker_calls[0].lineno:
            assert "run_stats" not in ast.unparse(node)

    # the batch path skips candidate-less sources before building request_jobs
    batch = _func(tree, "_complete_deferred_qwen_batch")
    code = _body_code(batch)
    assert "if not ai_candidates:" in code and "continue" in code
    assert "request_jobs.append" in code


# ------------------------------------------------------------------ J. AGGREGATION BOUNDARY
def test_j_current_run_counts_never_aggregate_over_videos():
    tree = _tree(_VA)
    code = _body_code(_func(tree, "analyze_video_sources"))

    # the legacy aggregates still sum over `videos` - that is their (historical) job
    assert "for v in videos" in code

    # every current-run field must come from run_stats or the job list, never from `videos`
    for line in code.splitlines():
        if "_this_run" in line and "for v in videos" in line:
            raise AssertionError(f"current-run field aggregated over videos: {line!r}")
    for field in ("qwen_jobs_this_run", "qwen_tag_count_this_run", "qwen_frame_count_this_run",
                  "qwen_seconds_this_run", "qwen_inference_seconds_this_run"):
        assert field in code, f"{field} missing from the result"
    assert "run_stats[" in code, "current-run fields must be sourced from run_stats"
    assert "'sources_analyzed_this_run': len(jobs)" in code or \
           '"sources_analyzed_this_run": len(jobs)' in code or \
           "len(jobs)" in code, "sources analysed must come from the uncached job set"


def test_j_run_stats_is_invocation_scoped_not_module_state():
    tree = _tree(_VA)
    code = _body_code(_func(tree, "analyze_video_sources"))
    assert "run_stats = _new_run_stats()" in code, (
        "run_stats must be created per invocation, so a second call cannot inherit the first's counts")
    module_level = {t.id for n in tree.body if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
    assert "run_stats" not in module_level


def test_j_current_run_metrics_are_never_written_into_a_cache_payload():
    """The accounting dict is separate from video_data, so it cannot reach _checkpoint_cache."""
    tree = _tree(_VA)
    for name in ("_analyze_single_video", "_complete_deferred_qwen",
                 "_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        fn = _func(tree, name)
        for call in _calls(fn, "_checkpoint_cache"):
            args = " ".join(ast.unparse(a) for a in call.args) + " " + \
                   " ".join(ast.unparse(kw.value) for kw in call.keywords)
            assert "run_stats" not in args, f"{name} passes run_stats toward the cache writer"
    code = _body_code(_func(tree, "analyze_video_sources"))
    for line in code.splitlines():
        if "_checkpoint_cache" in line:
            assert "run_stats" not in line


# ------------------------------------------------------------------ K. START WORDING
def test_k_stage5_start_does_not_claim_all_sources_are_being_analyzed():
    tree = _tree(_VA)
    fn = _func(tree, "analyze_video_sources")
    starts = _calls(fn, "start")
    assert starts, "the Stage-5 start event disappeared"
    messages = []
    for call in starts:
        for arg in call.args:
            if isinstance(arg, ast.JoinedStr):
                messages.append("".join(
                    v.value for v in arg.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)))
            elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                messages.append(arg.value)
    joined = " ".join(messages)
    assert "Analyzing" not in joined, (
        "the pre-cache START event must not claim every source is being analyzed")
    assert "Checking" in joined

    # the post-scan metric remains the authority on real work
    code = _body_code(fn)
    assert "cached, " in code and "to analyze" in code


# ------------------------------------------------------------------ freeze
def test_cache_identity_and_completion_are_untouched():
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v2"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source
    tree = _tree(_VA)
    for name in ("_qwen_job_completed", "_stored_ai_cache_is_consistent",
                 "_cache_entry_is_complete", "_checkpoint_cache", "_video_signature",
                 "_qwen_config_token"):
        fn = _func(tree, name)
        assert "run_stats" not in _body_code(fn), f"{name} must not know about run accounting"


# ======================================================================================
# R2 - failure accounting. These execute the REAL `_annotate_candidates_with_qwen` and
# `_complete_deferred_qwen_batch` bodies with only the worker subprocess stubbed, which is
# what the R1 suite lacked: it asserted on shape and so never noticed that a shared-worker
# failure returned before any accounting and reported "no inference this run".
# ======================================================================================

import json as _json
import math as _math
import tempfile as _tempfile

_PIPE_DEPS = ("_safe_name", "_hash_text", "_same_source", "_coerce_count", "_is_count",
              "_reported_count", "_stored_ai_cache_is_consistent", "_qwen_job_completed",
              "_deterministic_analysis_completed", "_cache_entry_is_complete",
              "_load_cache", "_save_cache", "_checkpoint_cache", "_fmt_seconds",
              "_new_run_stats",
              # T1 telemetry-boundary helpers. The extracted orchestration bodies call these, so they
              # have to be extracted alongside - the same coupling CLAUDE.md records for this pattern.
              "_as_mapping", "_is_real_number", "_optional_telemetry_number", "_telemetry_seconds",
              "_telemetry_total", "_is_nonnegative_count", "_bounded_count", "_telemetry_text",
              "_record_telemetry", "_record_candidate_count")
_PIPE_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_QWEN_COMPLETED_KEY",
                "_QWEN_SINGLE_JOB_ID", "_DETERMINISTIC_SCORING_KEY")


def _pipeline_ns(extra_funcs, **stubs):
    """Execute real production bodies with the worker (and only the worker) stubbed."""
    tree = _tree(_VA)
    wanted = set(_PIPE_DEPS) | set(extra_funcs)
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in wanted]
    missing = wanted - {n.name for n in picked}
    assert not missing, f"could not extract {missing}"
    consts = [n for n in tree.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in _PIPE_CONSTS for t in n.targets)]
    module = ast.Module(body=consts + picked, type_ignores=[])
    ast.fix_missing_locations(module)
    merged: list = []
    ns: dict = {
        "os": os, "json": _json, "tempfile": _tempfile, "math": _math, "time": time,
        "Any": typing.Any, "Dict": typing.Dict, "List": list, "Sequence": list,
        "_select_ai_candidates": lambda candidates, limit: list(candidates)[:limit],
        # faithful to the real `_merge_semantic`, which also sets ai_analyzed
        "_merge_semantic": lambda candidate, semantic: (
            merged.append(candidate["id"]),
            candidate.update({"ai_analyzed": True, "semantic": semantic}))[1],
    }
    ns.update(stubs)
    exec(compile(module, _VA, "exec"), ns)  # noqa: S102
    ns["_merged"] = merged
    os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)
    return ns


def _deferred_record(ns, name, n=10, path=None):
    return {
        "analysis_version": ns["ANALYSIS_VERSION"], "cache_contract": ns["CACHE_CONTRACT_VERSION"],
        "video_file": path or rf"C:\src\{name}", "source_name": name,
        "duration": 30.0, "fps": 25.0, "width": 1280, "height": 720, "scene_changes": [1.0],
        "candidate_count": n,
        "candidates": [{"id": f"{name}-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                        "editorial_score": 0.5} for i in range(n)],
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0, "candidate_scoring_seconds": 0.4},
        "ai_enabled": False, "ai_deferred": True,
    }


def _semantics(name, count):
    return {f"{name}-{i}": {"action": 0.5, "emotion": "hype"} for i in range(count)}


# ------------------------------------------------------------------ A. BATCH WORKER FAILURE
@pytest.mark.parametrize("worker_response,label", [
    ({}, "worker returned nothing at all"),
    ({"semantics_by_job": {}}, "worker returned an empty envelope"),
])
def test_r2_a_shared_worker_failure_is_still_an_attempted_current_run_job(
        worker_response, label, tmp_path, gui):
    """The R1 blocker: a real attempt that fails must not report as zero work."""
    ns = _pipeline_ns(("_qwen_max_windows", "_complete_deferred_qwen_batch"),
                      _env_int=lambda name, default, lo, hi: default,
                      _run_qwen_worker_batch=lambda **kw: worker_response)
    stats = ns["_new_run_stats"]()
    items = [({"index": 1, "cache_file": str(tmp_path / "a.json")}, _deferred_record(ns, "a.mp4")),
             ({"index": 2, "cache_file": str(tmp_path / "b.json")}, _deferred_record(ns, "b.mp4"))]

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", audio_profile={},
        total_video_count=2, run_stats=stats)

    assert stats["qwen_jobs"] == 2, f"{label}: attempted jobs were not counted"
    assert stats["qwen_requested_count"] == 20
    assert stats["qwen_completed_jobs"] == 0
    assert stats["qwen_incomplete_jobs"] == 2
    assert stats["qwen_tag_count"] == 0
    assert stats["qwen_frame_count"] == 0, "no decode was proven, so none may be claimed"
    assert stats["qwen_seconds"] > 0.0, "the measured worker wall time is real evidence"

    # and the summary must not claim the run did nothing
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=843, sources_analyzed_this_run=2,
        qwen_jobs_this_run=stats["qwen_jobs"],
        qwen_requested_count_this_run=stats["qwen_requested_count"],
        qwen_tag_count_this_run=stats["qwen_tag_count"],
        qwen_incomplete_jobs_this_run=stats["qwen_incomplete_jobs"],
        qwen_seconds_this_run=stats["qwen_seconds"]))
    assert "no inference this run" not in rec.text
    assert "0/20 tags" in rec.text
    assert "2 incomplete" in rec.text and "not cached" in rec.text


# ------------------------------------------------------------------ B. SINGLE FAILURE, NO TIMING
def test_r2_b_single_worker_failure_claims_no_decoded_frames():
    ns = _pipeline_ns(("_annotate_candidates_with_qwen",), _run_qwen_worker=lambda **kw: {})
    stats = ns["_new_run_stats"]()
    candidates = [{"id": f"c{i}", "start": i, "end": i + 1} for i in range(10)]

    info = ns["_annotate_candidates_with_qwen"](
        video_file="v.mp4", fps=25.0, candidates=candidates, qwen_model_path="m",
        use_gpu=False, audio_profile={}, run_stats=stats)

    assert stats["qwen_jobs"] == 1
    assert stats["qwen_requested_count"] == 10
    assert stats["qwen_frame_count"] == 0, "requested count must not stand in for decoded frames"
    assert stats["qwen_tag_count"] == 0
    assert stats["qwen_incomplete_jobs"] == 1
    assert stats["qwen_completed_jobs"] == 0
    assert stats["qwen_inference_seconds"] == 0.0, "no timing was reported, so none may be invented"
    # the persisted/source-record field keeps its legacy fallback - untouched by R2
    assert info["qwen_frame_count"] == 10


# ------------------------------------------------------------------ C. DECODE-SHORT
def test_r2_c_decode_short_keeps_the_requested_denominator(gui):
    response = {
        "semantics": _semantics("c", 8),
        "timings_by_job": {"single": {"frame_count": 8, "tag_count": 8,
                                      "inference_seconds": 4.0, "prefetch_seconds": 0.2}},
    }
    ns = _pipeline_ns(("_annotate_candidates_with_qwen",), _run_qwen_worker=lambda **kw: response)
    stats = ns["_new_run_stats"]()
    candidates = [{"id": f"c-{i}", "start": i, "end": i + 1} for i in range(10)]

    ns["_annotate_candidates_with_qwen"](
        video_file="v.mp4", fps=25.0, candidates=candidates, qwen_model_path="m",
        use_gpu=False, audio_profile={}, run_stats=stats)

    assert stats["qwen_requested_count"] == 10
    assert stats["qwen_frame_count"] == 8
    assert stats["qwen_tag_count"] == 8
    assert stats["qwen_incomplete_jobs"] == 1, "2 requested candidates never decoded"
    assert stats["qwen_completed_jobs"] == 0
    assert stats["qwen_inference_seconds"] == pytest.approx(4.0)

    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=844, sources_analyzed_this_run=1, qwen_jobs_this_run=1,
        qwen_requested_count_this_run=10, qwen_tag_count_this_run=8,
        qwen_frame_count_this_run=8, qwen_incomplete_jobs_this_run=1,
        qwen_seconds_this_run=5.0))
    assert "8/10 tags" in rec.text, "decoded count must not become the success denominator"
    assert "8/8" not in rec.text


# ------------------------------------------------------------------ D. SEMANTIC-SHORT
def test_r2_d_semantic_short_reports_tagged_over_requested(gui):
    response = {
        "semantics": _semantics("d", 9),
        "timings_by_job": {"single": {"frame_count": 10, "tag_count": 9,
                                      "inference_seconds": 6.0, "prefetch_seconds": 0.2}},
    }
    ns = _pipeline_ns(("_annotate_candidates_with_qwen",), _run_qwen_worker=lambda **kw: response)
    stats = ns["_new_run_stats"]()
    candidates = [{"id": f"d-{i}", "start": i, "end": i + 1} for i in range(10)]

    ns["_annotate_candidates_with_qwen"](
        video_file="v.mp4", fps=25.0, candidates=candidates, qwen_model_path="m",
        use_gpu=False, audio_profile={}, run_stats=stats)

    assert stats["qwen_requested_count"] == 10
    assert stats["qwen_frame_count"] == 10
    assert stats["qwen_tag_count"] == 9
    assert stats["qwen_incomplete_jobs"] == 1

    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(
        cache_hits=844, sources_analyzed_this_run=1, qwen_jobs_this_run=1,
        qwen_requested_count_this_run=10, qwen_tag_count_this_run=9,
        qwen_frame_count_this_run=10, qwen_incomplete_jobs_this_run=1,
        qwen_seconds_this_run=7.0))
    assert "9/10 tags" in rec.text
    assert "1 incomplete" in rec.text


# ------------------------------------------------------------------ E. NO DOUBLE COUNT
def test_r2_e_successful_batch_counts_each_job_once_and_wall_time_once(tmp_path):
    response = {
        "model_load_seconds": 8.0, "model_id": "q", "batch_size": 2, "peak_vram_gb": 3.0,
        "semantics_by_job": {"1": _semantics("a.mp4", 10), "2": _semantics("b.mp4", 10)},
        "timings_by_job": {
            "1": {"frame_count": 10, "tag_count": 10, "inference_seconds": 5.0, "prefetch_seconds": 0.1},
            "2": {"frame_count": 10, "tag_count": 10, "inference_seconds": 6.0, "prefetch_seconds": 0.1},
        },
    }
    ns = _pipeline_ns(("_qwen_max_windows", "_complete_deferred_qwen_batch"),
                      _env_int=lambda name, default, lo, hi: default,
                      _run_qwen_worker_batch=lambda **kw: response)
    stats = ns["_new_run_stats"]()
    items = [({"index": 1, "cache_file": str(tmp_path / "a.json")}, _deferred_record(ns, "a.mp4")),
             ({"index": 2, "cache_file": str(tmp_path / "b.json")}, _deferred_record(ns, "b.mp4"))]

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", audio_profile={},
        total_video_count=2, run_stats=stats)

    assert stats["qwen_jobs"] == 2, "submission and response phases must not both count the job"
    assert stats["qwen_requested_count"] == 20
    assert stats["qwen_completed_jobs"] == 2
    assert stats["qwen_incomplete_jobs"] == 0
    assert stats["qwen_frame_count"] == 20
    assert stats["qwen_tag_count"] == 20
    assert stats["qwen_inference_seconds"] == pytest.approx(11.0)


# ------------------------------------------------------------------ G. WALL TIME SEMANTICS
def test_r2_g_batch_wall_time_is_one_shared_worker_duration(tmp_path):
    """Not a sum of per-source amortized costs, which would inflate with source count."""
    response = {
        "model_load_seconds": 40.0, "model_id": "q", "batch_size": 3, "peak_vram_gb": 3.0,
        "semantics_by_job": {str(i): _semantics(f"s{i}.mp4", 10) for i in (1, 2, 3)},
        "timings_by_job": {str(i): {"frame_count": 10, "tag_count": 10,
                                    "inference_seconds": 9.0, "prefetch_seconds": 1.0}
                           for i in (1, 2, 3)},
    }
    ns = _pipeline_ns(("_qwen_max_windows", "_complete_deferred_qwen_batch"),
                      _env_int=lambda name, default, lo, hi: default,
                      _run_qwen_worker_batch=lambda **kw: response)
    stats = ns["_new_run_stats"]()
    items = [({"index": i, "cache_file": str(tmp_path / f"s{i}.json")},
              _deferred_record(ns, f"s{i}.mp4")) for i in (1, 2, 3)]

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", audio_profile={},
        total_video_count=3, run_stats=stats)

    # per-source amortized cost would be ~3 * (9 + 1 + 40/3) = ~70s; the real shared invocation is
    # near-instant here because the worker is stubbed.
    assert stats["qwen_seconds"] < 5.0, "wall time was summed per source instead of counted once"
    assert stats["qwen_inference_seconds"] == pytest.approx(27.0)


def test_r2_g_malformed_worker_timings_do_not_raise(tmp_path):
    """Accounting is observability: it must never turn a survivable run into a crash."""
    response = {
        "model_load_seconds": None, "model_id": "q", "batch_size": 1,
        "semantics_by_job": {"1": _semantics("a.mp4", 2)},
        # `inference_seconds` used to stay numeric here on purpose: the merge loop did
        # `float(timing.get("inference_seconds") or 0.0)`, which raised on a non-numeric string, so
        # R2 could only prove that its own *new* accounting added no exception. T1 repaired that
        # boundary, so the string now belongs in this fixture - a malformed duration must degrade to
        # a zero contribution rather than taking Stage 5 down. The exhaustive malformed-shape matrix
        # lives in `tests/test_qwen_scalar_boundary.py`; this case only keeps the R2 accounting
        # assertions honest against data the runtime has to survive.
        "timings_by_job": {"1": {"frame_count": True, "tag_count": "nine",
                                 "inference_seconds": "not-a-number", "prefetch_seconds": None}},
    }
    ns = _pipeline_ns(("_qwen_max_windows", "_complete_deferred_qwen_batch"),
                      _env_int=lambda name, default, lo, hi: default,
                      _run_qwen_worker_batch=lambda **kw: response)
    stats = ns["_new_run_stats"]()
    items = [({"index": 1, "cache_file": str(tmp_path / "a.json")},
              _deferred_record(ns, "a.mp4", n=2))]

    ns["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m", audio_profile={},
        total_video_count=1, run_stats=stats)

    assert stats["qwen_jobs"] == 1
    # `frame_count: True` is a bool, not a count - it must be rejected, not counted as 1
    assert stats["qwen_frame_count"] == 0
    # T1: a malformed duration is not evidence, so it contributes nothing - and does not raise.
    assert stats["qwen_inference_seconds"] == pytest.approx(0.0)
    assert _math.isfinite(stats["qwen_inference_seconds"])
    # The job was still attempted, and its wall time is still the parent's own measurement.
    assert stats["qwen_seconds"] > 0.0 and _math.isfinite(stats["qwen_seconds"])


# ------------------------------------------------------------------ F/H. WARM + CACHE ISOLATION
def test_r2_f_warm_case_has_zero_requested_candidates(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, warm_result(qwen_requested_count_this_run=0))
    assert "no inference this run" in rec.text
    assert "tags" not in rec.text.split("Visual library")[0].split("Cached library")[0]


def test_r2_h_requested_count_never_enters_a_cache_payload():
    tree = _tree(_VA)
    for name in ("_analyze_single_video", "_complete_deferred_qwen",
                 "_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        fn = _func(tree, name)
        for call in _calls(fn, "_checkpoint_cache"):
            rendered = " ".join(ast.unparse(a) for a in call.args) + " " + \
                       " ".join(ast.unparse(kw.value) for kw in call.keywords)
            assert "run_stats" not in rendered and "qwen_requested_count" not in rendered
    # and the counter is only ever a run_stats key, never a video_data/timings key
    for name in ("_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        code = _body_code(_func(tree, name))
        for line in code.splitlines():
            if "qwen_requested_count" in line:
                assert "run_stats[" in line, f"requested count escaped run_stats: {line!r}"


def test_r2_submission_truth_precedes_the_empty_response_return():
    """Structural guard for the exact R1 blocker: accounting must not sit behind the early return."""
    fn = _func(_tree(_VA), "_complete_deferred_qwen_batch")
    blocks = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and "run_stats is not None" in ast.unparse(n.test)]
    assert len(blocks) == 2, "expected a submission block and a response block"
    submission = min(blocks, key=lambda b: b.lineno)
    empty_guard = [n for n in ast.walk(fn)
                   if isinstance(n, ast.If) and "not semantics_by_job" in ast.unparse(n.test)]
    assert empty_guard, "the empty-response branch disappeared"
    assert submission.lineno < empty_guard[0].lineno, (
        "submission accounting must run before the empty-response return")
    assert "qwen_jobs" in ast.unparse(submission)
    # the response block must NOT re-count the job
    response = max(blocks, key=lambda b: b.lineno)
    assert "qwen_jobs'] +=" not in ast.unparse(response) and \
           'qwen_jobs"] +=' not in ast.unparse(response), "successful jobs would be double-counted"
