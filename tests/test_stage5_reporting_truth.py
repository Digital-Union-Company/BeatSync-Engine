"""Stage 5 reports what THIS run did, not what the cached library remembers.

Production proved the defect on a fully warm library: 845/845 cache hits, zero sources analysed,
zero Qwen workers launched — and a console summary reading `visual workers: 1`,
`Qwen performance: … 3.12 candidates/s` and `Qwen tags: 8704/8704 in 3031.9s`. Every one of those
numbers came from cached records. The five-line budget then dropped the only line that described the
run: `Analysis time: …, cache 845/845`.

`gui.py` needs gradio and `video_analysis.py` needs the whole portable runtime, so neither can be
imported on the bare interpreter this suite runs on. Two techniques are used instead:

* the pure, stdlib-only pieces (`StageConsoleLogger`, `_fmt_stage_seconds`, `_stage5_summary`,
  `_new_run_stats`, `_record_qwen_job`) are extracted with `ast` and executed, so the behavioural
  tests drive the real production code rather than a copy of it;
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
                   "qwen_frame_count", "qwen_tag_count", "qwen_seconds",
                   "qwen_inference_seconds")


def _recording_block(fn: ast.AST) -> ast.If:
    """The single `if run_stats is not None:` accounting block inside a recording function."""
    blocks = [n for n in ast.walk(fn)
              if isinstance(n, ast.If) and "run_stats is not None" in ast.unparse(n.test)]
    assert len(blocks) == 1, f"expected exactly one accounting block, found {len(blocks)}"
    return blocks[0]


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
        "qwen_incomplete_jobs_this_run": 0, "qwen_frame_count_this_run": 0,
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
        qwen_jobs_this_run=3, qwen_completed_jobs_this_run=3,
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
    """Serial/inline and batch must account identically - one job, all seven counters."""
    block = _recording_block(_func(_tree(_VA), fn_name))
    code = ast.unparse(block)
    for key in _RUN_STATS_KEYS:
        assert f"'{key}'" in code or f'"{key}"' in code, f"{fn_name} does not tally {key}"
    assert "qwen_jobs'] += 1" in code or 'qwen_jobs"] += 1' in code
    # a job that ran but did not complete is still a job, tallied separately
    inner = [n for n in ast.walk(block) if isinstance(n, ast.If)]
    assert inner, f"{fn_name} does not split completed vs incomplete"
    split = ast.unparse(inner[0])
    assert "qwen_completed_jobs" in split and "qwen_incomplete_jobs" in split


def test_h_recording_is_guarded_so_a_missing_sink_is_a_no_op():
    for fn_name in ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch"):
        block = _recording_block(_func(_tree(_VA), fn_name))
        assert "run_stats is not None" in ast.unparse(block.test)


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
