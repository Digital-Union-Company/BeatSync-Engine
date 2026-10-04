"""L0 — scale diagnostics: the numbers a 5,000-source library will be judged by.

This PR measures; it optimises nothing. So the tests are about *truthfulness and placement*, not
speed: no wall-clock assertion appears here, because a threshold that passes on one machine and
fails on another would be worse than no test at all.

Three techniques, picked per target:

* ``beatsync_fork.input_report`` is stdlib-only, so it is imported and executed directly;
* ``gui._stage5_summary`` / ``_stage6_summary`` / ``StageConsoleLogger`` need Gradio to *import* but
  not to *run*, so they are AST-extracted and executed — the behavioural tests drive real production
  code rather than a copy of it;
* ``video_analysis.py`` and ``video_processor.py`` need the whole portable runtime (cv2, cupy,
  librosa), so the placement invariants there are asserted structurally, by function and call site.

The structural assertions are the load-bearing ones for this PR: what matters is that the timers
wrap the **existing** operations without changing them, and that none of the new fields can reach
cache identity or a cache payload.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import os
import re
import time
import typing

import pytest

from conftest import write_file

from beatsync_fork.input_manager import scan_folder
from beatsync_fork.input_report import InputReport, format_seconds

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_VP = os.path.join(_REPO_ROOT, "src", "video_processor.py")


# ---------------------------------------------------------------------------
# helpers (same shape as the other AST suites in this repo)
# ---------------------------------------------------------------------------


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


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


def _extract(path: str, names: set[str], namespace: dict) -> dict:
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


class _Describe:
    """Stands in for ``beatsync_fork.variation.describe`` inside the extracted GUI summary."""

    @staticmethod
    def describe(seed) -> str:
        return "seed legacy (0)" if not seed else f"seed {int(seed)}"


@pytest.fixture(scope="module")
def gui():
    from beatsync_fork.progress import EventKind, ProgressEvent  # stdlib-only, importable here

    ns: dict = {
        "time": time, "os": os, "re": re, "Dict": typing.Dict,
        "fork_variation": _Describe,
        "EventKind": EventKind, "ProgressEvent": ProgressEvent,
    }
    _extract(_GUI, {"StageConsoleLogger", "_fmt_stage_seconds", "_stage5_summary",
                    "_stage6_summary", "_scale_diagnostics_block"}, ns)
    return ns


class Recorder:
    def __init__(self):
        self.lines: list[str] = []

    def line(self, message: str) -> None:
        self.lines.append(message)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# ======================================================================================
# A. folder scan time is exposed, and it is the scan's own number
# ======================================================================================


def test_a_render_text_reports_the_scan_time(tmp_path):
    root = str(tmp_path / "lib")
    write_file(os.path.join(root, "a.mp4"), b"1234")
    write_file(os.path.join(root, "b.mkv"), b"12345")

    report = InputReport.from_input_set(scan_folder(root))
    text = report.render_text()

    line = next(l for l in text.splitlines() if l.startswith("Scan time:"))
    assert line.endswith("s")
    # the report never invents a measurement: it prints exactly what the scan reported
    assert line == f"Scan time:   {format_seconds(report.scan_seconds)}"
    assert report.scan_seconds >= 0.0


def test_a_scan_time_is_truthful_not_recomputed():
    """A report rendered from a known ``scan_seconds`` must show that value, not a fresh clock."""
    blank = InputReport(
        root="C:/lib", recursive=True, discovered=845, supported=845, ready=845, rejected=0,
        rejected_by_reason={}, path_collisions=0, duplicate_groups=0, duplicate_extra_files=0,
        fingerprint_errors=0, duplicates_checked=False, total_ready_bytes=1024,
        scan_seconds=4.07, is_ready=True,
    )
    assert "Scan time:   4.1s" in blank.render_text()
    assert dataclasses.replace(blank, scan_seconds=123.456).render_text().count("123.5s") == 1
    # and the serialised form keeps its existing 3-decimal contract
    assert blank.as_dict()["scan_seconds"] == 4.07


@pytest.mark.parametrize("value,shown", [
    (0.0, "0.0s"), (4.07, "4.1s"), (12.0, "12.0s"), (-1.0, "0.0s"),
    (float("nan"), "0.0s"), (float("inf"), "0.0s"), ("bad", "0.0s"), (None, "0.0s"),
])
def test_a_format_seconds_is_total_and_deterministic(value, shown):
    """A diagnostic must never raise inside a UI callback, and must render the same way twice."""
    assert format_seconds(value) == shown
    assert format_seconds(value) == format_seconds(value)


def test_a_report_stays_stdlib_only_and_ui_agnostic():
    source = open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "input_report.py"),
                  encoding="utf-8").read()
    for forbidden in ("import gradio", "import cv2", "import numpy", "from logger", "from paths"):
        assert forbidden not in source, forbidden


# ======================================================================================
# B/C. Stage 5 cache-scan timings and count truth
# ======================================================================================


_ORCHESTRATORS = ("analyze_video_sources", "classify_library_sources")


def _identity_phase_statements(name: str) -> tuple[int, int, int, list[str]]:
    """(open index, phase index, close index, lines) for the bounded identity phase.

    L1B replaced the per-source serial accumulation with ONE wall-clock interval around the whole
    phase, so the timer is located as three statements rather than as a pair inside a loop.
    """
    code = _body_code(_func(_tree(_VA), name))
    lines = code.splitlines()
    open_at = next(i for i, l in enumerate(lines) if "identity_started = time.perf_counter()" in l)
    phase_at = next(i for i, l in enumerate(lines) if "_compute_cache_paths_parallel(" in l)
    close_at = next(i for i, l in enumerate(lines)
                    if "cache_identity_seconds = time.perf_counter() - identity_started" in l)
    return open_at, phase_at, close_at, lines


@pytest.mark.parametrize("name", _ORCHESTRATORS)
def test_b_identity_is_the_wall_clock_latency_of_the_whole_bounded_phase(name):
    """L1B's load-bearing telemetry semantic.

    Before L1B the serial accumulation *happened* to equal wall time, because only one identity call
    ran at a time. With bounded parallelism those two stop being the same number, and summing
    per-worker task durations would make an 8.77x speed-up report as a slow-down. The metric is
    therefore defined as the latency the caller actually waited for the phase: exactly one
    `perf_counter()` before it, exactly one elapsed subtraction after it, and nothing accumulated in
    between.
    """
    open_at, phase_at, close_at, lines = _identity_phase_statements(name)
    assert open_at < phase_at <= close_at, (open_at, phase_at, close_at)

    code = "\n".join(lines)
    assert "cache_identity_seconds +=" not in code, (
        "identity must be ONE wall-clock interval, never a sum of per-task durations")
    assert code.count("identity_started = time.perf_counter()") == 1, "exactly one phase clock"
    assert code.count("cache_identity_seconds = time.perf_counter() - identity_started") == 1

    # and the clock is not opened or closed inside the per-source loop or a future handler
    fn = _func(_tree(_VA), name)
    for loop in [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While, ast.With))]:
        rendered = ast.unparse(loop)
        assert "identity_started" not in rendered, (
            "the phase clock must not be opened or closed per source / per future")
        assert "cache_identity_seconds" not in rendered


@pytest.mark.parametrize("name", _ORCHESTRATORS)
def test_b_record_lookup_stays_a_serial_accumulation(name):
    """`cache_lookup_seconds` keeps its original meaning, because `_load_cache` is still serial.

    The two costs grow differently with the library, so one combined number would be useless — and
    conflating a parallel phase latency with a serial per-record sum would be worse still.
    """
    code = _body_code(_func(_tree(_VA), name))
    assert "lookup_started = time.perf_counter()" in code
    assert "cache_lookup_seconds += time.perf_counter() - lookup_started" in code
    assert "cache_lookup_seconds = 0.0" in code, (
        "the serial accumulator starts at zero, so an empty library reports 0.0")

    lines = code.splitlines()
    lookup_open = next(i for i, l in enumerate(lines) if "lookup_started = time.perf" in l)
    lookup_close = next(i for i, l in enumerate(lines) if "cache_lookup_seconds +=" in l)
    load_line = next(i for i, l in enumerate(lines) if "_load_cache(" in l)
    assert lookup_open < load_line <= lookup_close

    # the identity phase closes before the record phase opens: they never overlap
    _, _, identity_close, _ = _identity_phase_statements(name)
    assert identity_close < lookup_open


def test_b_the_existing_cache_operations_are_unchanged():
    """Orchestration only. The identity primitive and the loader are the same calls with the same
    arguments; what changed is that identity is computed as a bounded-parallel phase first."""
    code = _body_code(_func(_tree(_VA), "analyze_video_sources"))

    assert "cache_files = [None] * len(existing) if ai_cache_disabled" in code, (
        "an AI-cache-disabled run must still perform no identity work at all")
    assert "_load_cache(cache_file, require_ai=ai_available, expected_video_file=video_file)" in code
    # identity is delegated once; the loader is still called once, still inside the per-source loop
    tree = _tree(_VA)
    fn = _func(tree, "analyze_video_sources")
    assert len(_calls(fn, "_cache_path")) == 0, "the per-source call now lives in the helper"
    assert len(_calls(fn, "_compute_cache_paths_parallel")) == 1
    assert len(_calls(fn, "_load_cache")) == 1
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
    assert any(_calls(loop, "_load_cache") for loop in loops), "record lookup stays in the loop"


def test_b_only_identity_was_parallelised():
    """The executor owns `_cache_path`. Record loading, hit/miss accounting, job construction,
    progress and the analysis/Qwen passes keep the concurrency they already had."""
    tree = _tree(_VA)
    helper = _func(tree, "_compute_cache_paths_parallel")
    rendered = ast.unparse(helper)
    for forbidden in ("_load_cache", "_analyze_single_video", "_complete_deferred_qwen",
                      "_run_qwen_worker", "cache_hits", "jobs", "results_by_index",
                      "source_counter", "classifications", "fork_progress"):
        assert forbidden not in rendered, f"the identity phase must not reach {forbidden}"

    # the pre-existing analysis pool is untouched: still keyed off `_video_analysis_workers`
    orchestrator = _body_code(_func(tree, "analyze_video_sources"))
    assert "workers = _video_analysis_workers(len(jobs)) if jobs else 0" in orchestrator
    assert "ThreadPoolExecutor(max_workers=workers)" in orchestrator


def test_b_returned_timings_are_floats_and_counts_are_ints():
    """Finite and non-negative by construction: monotonic-clock deltas accumulated from 0.0."""
    code = _body_code(_func(_tree(_VA), "analyze_video_sources"))

    assert "'cache_identity_seconds': float(cache_identity_seconds)" in code
    assert "'cache_lookup_seconds': float(cache_lookup_seconds)" in code
    assert "'cache_lookups': int(cache_lookups)" in code


def test_c_candidate_count_is_the_length_of_the_returned_candidate_list():
    """Directly ``len(all_candidates)`` — never re-derived from telemetry, which can be malformed."""
    code = _body_code(_func(_tree(_VA), "analyze_video_sources"))

    assert "'candidate_count': len(all_candidates)" in code
    assert "'candidates': all_candidates" in code, "the list it counts must still be returned"

    # structurally: the returned value for that key is exactly len(all_candidates) - no telemetry
    # helper, no bounded count, nothing that could disagree with the list Stage 6 receives
    fn = _func(_tree(_VA), "analyze_video_sources")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)]
    payload = next(d.value for d in returns
                   if any(isinstance(k, ast.Constant) and k.value == "candidate_count"
                          for k in d.value.keys))
    by_key = {k.value: v for k, v in zip(payload.keys, payload.values)
              if isinstance(k, ast.Constant)}
    assert ast.unparse(by_key["candidate_count"]) == "len(all_candidates)"
    assert ast.unparse(by_key["candidates"]) == "all_candidates"


def test_c_existing_count_truth_is_preserved():
    """R1's current-run / historical separation must survive this PR untouched."""
    code = _body_code(_func(_tree(_VA), "analyze_video_sources"))

    assert "'source_count': len(existing)" in code
    assert "'cache_hits': cache_hits" in code
    assert "'sources_analyzed_this_run': len(jobs)" in code
    for line in code.splitlines():
        if "_this_run" in line and "for v in videos" in line:
            raise AssertionError(f"current-run field aggregated over videos: {line!r}")


# ======================================================================================
# D. the new fields are invocation metadata — never identity, never a payload
# ======================================================================================


_NEW_STAGE5_FIELDS = ("cache_identity_seconds", "cache_lookup_seconds", "cache_lookups",
                      "identity_started", "lookup_started")
# Note on ``candidate_count``: a per-source cache record has carried a field of that name since long
# before this PR (``'candidate_count': len(candidates)`` in ``_analyze_single_video``). The new
# invocation-level ``candidate_count`` is a different scope — the whole library's moment count on the
# returned top-level dict — so the payload tests below check that the per-source field is *unchanged*
# rather than forbidding the name outright.


def test_d_timings_never_enter_cache_identity_or_the_completion_contract():
    tree = _tree(_VA)
    for name in ("_video_signature", "_cache_path", "_compute_cache_paths_parallel",
                 "_cache_identity_workers", "_qwen_config_token",
                 "_qwen_backend_signature_token", "_cache_entry_is_complete",
                 "_qwen_job_completed", "_stored_ai_cache_is_consistent", "_checkpoint_cache",
                 "_save_cache"):
        body = _body_code(_func(tree, name))
        for field in _NEW_STAGE5_FIELDS:
            assert field not in body, f"{name} must not know about {field}"


def test_d_timings_never_reach_a_per_source_cache_payload():
    """A record is born in ``_analyze_single_video``; nothing here may be written into one."""
    tree = _tree(_VA)
    for name in ("_analyze_single_video", "_complete_deferred_qwen",
                 "_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        body = _body_code(_func(tree, name))
        for field in _NEW_STAGE5_FIELDS:
            assert field not in body, f"{name} must not carry {field}"

    # the pre-existing per-source candidate count is untouched (different scope, same name)
    born = _body_code(_func(tree, "_analyze_single_video"))
    assert "'candidate_count': len(candidates)" in born

    # and the checkpoint call sites in the orchestrator still pass only the source record
    code = _body_code(_func(tree, "analyze_video_sources"))
    for line in code.splitlines():
        if "_checkpoint_cache" in line:
            for field in _NEW_STAGE5_FIELDS + ("candidate_count",):
                assert field not in line, line


def test_d_cache_contract_and_analysis_version_are_untouched():
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


# ======================================================================================
# Stage 5 reporting — the summary the user actually reads
# ======================================================================================


def _warm_result(**over):
    data = {
        "source_count": 845, "cache_hits": 845, "worker_count": 0,
        "sources_analyzed_this_run": 0, "analysis_workers_used": 0,
        "qwen_jobs_this_run": 0, "qwen_requested_count_this_run": 0,
        "qwen_tag_count_this_run": 0, "qwen_incomplete_jobs_this_run": 0,
        "qwen_seconds_this_run": 0.0,
        "ai_enabled": True, "analysis_seconds": 9.4,
        "candidate_count": 14816,
        "cache_identity_seconds": 3.24, "cache_lookup_seconds": 1.81, "cache_lookups": 845,
        "summary": "14816 visual moments, action=0.50, beauty=0.65, quality=0.76",
        "qwen_tag_count": 8704, "qwen_frame_count": 8704,
    }
    data.update(over)
    return data


def test_stage5_summary_reports_the_cache_scan_cost(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, _warm_result())
    text = rec.text

    assert "Cache check: identity 3.2s, records 1.8s" in text
    # the pre-existing truths are still there and still current-run
    assert "Sources: 845, cache 845/845, analyzed this run 0" in text
    assert "no inference this run" in text
    assert "Analysis time: 9.4s, cache 845/845" in text
    assert "14816 visual moments" in text
    # and no historical Qwen work is presented as this run's
    assert "8704/8704" not in text


def test_stage5_summary_omits_the_line_when_nothing_was_measured(gui):
    rec = Recorder()
    gui["_stage5_summary"](rec, _warm_result(cache_identity_seconds=None,
                                             cache_lookup_seconds=None))
    assert "Cache check" not in rec.text


def test_stage5_summary_reports_a_zero_measurement_rather_than_hiding_it(gui):
    """0.0 is a measurement (an empty or AI-cache-disabled run), not an absence."""
    rec = Recorder()
    gui["_stage5_summary"](rec, _warm_result(cache_identity_seconds=0.0,
                                             cache_lookup_seconds=0.0))
    assert "Cache check: identity 0.0s, records 0.0s" in rec.text


def test_stage5_summary_still_fits_the_five_line_budget_with_the_truth_first(gui):
    """The budget is not raised; the historical line is what yields, as it must."""
    stream = io.StringIO()
    logger = gui["StageConsoleLogger"](stream, max_lines_per_stage=5)
    logger.start_stage(5)
    gui["_stage5_summary"](logger, _warm_result())
    out = stream.getvalue()

    assert logger.max_lines_per_stage == 5, "the budget itself must not be raised"
    for required in ("Sources: 845", "Qwen:", "Cache check:", "Analysis time:", "Visual library:"):
        assert required in out, required
    assert "Cached library" not in out, "the optional historical line is the one that yields"


# ======================================================================================
# E/F. Stage 6 planner scale facts
# ======================================================================================


def test_e_planner_is_timed_and_records_its_scale():
    code = _body_code(_func(_tree(_VP), "create_music_video"))

    assert "planner_started = time.perf_counter()" in code
    assert "planner_seconds = time.perf_counter() - planner_started" in code
    assert "'planner_seconds': float(planner_seconds)" in code
    assert "'planner_candidate_count': int(planner_candidate_count)" in code
    assert "'planner_segment_count': int(total_clips)" in code

    lines = code.splitlines()
    opened = next(i for i, l in enumerate(lines) if "planner_started = time.perf" in l)
    called = next(i for i, l in enumerate(lines) if "planned_clip_sequence = build_planned" in l)
    closed = next(i for i, l in enumerate(lines) if "planner_seconds = time.perf" in l)
    assert opened < called < closed, "only the planner call may be inside the measurement"


def test_e_planner_candidate_count_is_moments_not_source_files():
    """The whole point of the number: 5,000 sources can carry ~100,000 candidate moments."""
    code = _body_code(_func(_tree(_VP), "create_music_video"))

    assignment = next(l for l in code.splitlines() if l.startswith("planner_candidate_count ="))
    assert "'candidates'" in assignment and "video_analysis" in assignment
    assert "video_files" not in assignment, "this must never degrade into a source-file count"


def _planner_count_statement() -> ast.Assign:
    """The real ``planner_candidate_count = …`` statement, lifted out of ``create_music_video``."""
    fn = _func(_tree(_VP), "create_music_video")
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "planner_candidate_count" for t in node.targets
        ):
            return node
    raise AssertionError("planner_candidate_count assignment not found")


def _count_candidates(beat_info) -> int:
    """Execute that real statement against a synthetic ``beat_info``."""
    module = ast.Module(body=[_planner_count_statement()], type_ignores=[])
    ast.fix_missing_locations(module)
    ns: dict = {"beat_info": beat_info}
    exec(compile(module, _VP, "exec"), ns)  # noqa: S102 - real source, isolated namespace
    return ns["planner_candidate_count"]


def test_e_planner_candidate_count_counts_only_the_pool_stage6_scores():
    """``build_planned_clip_sequence`` drops candidates without ``video_file`` before scoring.

    Counting the raw list overstated the work: a loaded-but-unusable candidate was reported as
    evaluated. The diagnostic applies the planner's own existing predicate instead.
    """
    candidates = [
        {"id": "a", "video_file": "C:/lib/one.mp4"},
        {"id": "b"},                                    # no video_file - the planner drops it
        {"id": "c", "video_file": "C:/lib/two.mp4"},
    ]
    beat_info = {"video_analysis": {"candidates": candidates}}

    assert _count_candidates(beat_info) == 2
    # the list itself is untouched - counting must never filter in place
    assert [c.get("id") for c in beat_info["video_analysis"]["candidates"]] == ["a", "b", "c"]
    assert len(candidates) == 3

    # the same predicate the planner uses, so the two can never disagree
    planner_src = open(os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py"),
                       encoding="utf-8").read()
    assert 'candidates = [c for c in candidates if c.get("video_file")]' in planner_src


@pytest.mark.parametrize("beat_info,expected", [
    (None, 0),
    ({}, 0),
    ({"video_analysis": None}, 0),
    ({"video_analysis": {}}, 0),
    ({"video_analysis": {"candidates": []}}, 0),
    ({"video_analysis": {"candidates": [{"video_file": "x.mp4"}]}}, 1),
    ({"video_analysis": {"candidates": [{"video_file": ""}]}}, 0),
])
def test_e_planner_candidate_count_edge_shapes(beat_info, expected):
    """Only the shapes Stage 6 already contracts for - no extra hardening was added here."""
    assert _count_candidates(beat_info) == expected


def test_e_planner_seconds_and_segment_count_are_unaffected_by_the_count_fix():
    code = _body_code(_func(_tree(_VP), "create_music_video"))

    assert "planner_seconds = time.perf_counter() - planner_started" in code
    assert "'planner_segment_count': int(total_clips)" in code
    # the count is computed before the timer opens, so it cannot inflate the measured planner time
    lines = code.splitlines()
    counted = next(i for i, l in enumerate(lines) if l.startswith("planner_candidate_count ="))
    opened = next(i for i, l in enumerate(lines) if "planner_started = time.perf" in l)
    assert counted < opened


def test_e_segment_count_is_the_frame_aligned_segment_count():
    code = _body_code(_func(_tree(_VP), "create_music_video"))

    assert "total_clips = len(segment_durations)" in code
    assert "'render_cuts': int(total_clips)" in code, "the existing render diagnostics stay"
    assert "'planner_segment_count': int(total_clips)" in code


def test_f_the_planner_call_itself_is_untouched():
    """Diagnostics must not change planner inputs, behaviour or the resulting plan."""
    tree = _tree(_VP)
    fn = _func(tree, "create_music_video")

    calls = _calls(fn, "build_planned_clip_sequence")
    assert len(calls) == 1, "exactly one planner invocation, as before"
    assert [kw.arg for kw in calls[0].keywords] == [
        "cut_times", "segment_durations", "beat_info", "video_files"]
    assert not calls[0].args, "no positional argument was smuggled in"

    code = _body_code(fn)
    # the plan still flows to the same consumers, unmodified
    assert "if planned_clip_sequence:" in code
    # Creative Controls Core added the resolved profile to the summary; the plan itself is still
    # handed over whole and unmodified, which is what this test is protecting.
    assert "summarize_clip_plan(planned_clip_sequence, seed=variation_seed" in code
    assert "render_info['plan_summary'] = plan_summary" in code


def test_f_the_planner_module_is_not_touched_by_this_pr():
    """L0 measures Stage 6; it does not shortlist, cache or reorder anything inside it."""
    planner = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")
    code = open(planner, encoding="utf-8").read()
    for forbidden in ("planner_seconds", "planner_candidate_count", "shortlist"):
        assert forbidden not in code, forbidden


def test_e_existing_render_diagnostics_are_preserved():
    code = _body_code(_func(_tree(_VP), "create_music_video"))
    for field in ("'render_cuts'", "'timeline_frames'", "'encoder'", "'output_fps'",
                  "'clip_workers'", "'creative_seed'"):
        assert field in code, field
    assert "render_info['audio_duration'] = float(audio_duration)" in code
    assert "render_info['final_assembly_seconds'] = float(assembly_seconds)" in code


def test_stage6_summary_shows_candidates_and_planner_time(gui):
    rec = Recorder()
    gui["_stage6_summary"](rec, {"render_info": {
        "render_cuts": 282, "timeline_frames": 50000, "output_fps": 30.0,
        "planner_seconds": 1.42, "planner_candidate_count": 14816, "planner_segment_count": 282,
        "plan_summary": {"clip_count": 282, "source_count": 143, "ai_tagged": 4096, "seed": 202},
    }})
    planner_line = next(l for l in rec.lines if l.startswith("Planner:"))

    assert "282 clips" in planner_line
    assert "143 sources" in planner_line
    assert "14816 candidates" in planner_line
    assert "1.4s" in planner_line
    assert "seed 202" in planner_line


def test_stage6_summary_is_unchanged_when_stage6_reported_no_planner_scale(gui):
    """An older/other producer of ``render_info`` renders exactly as it did before this PR."""
    rec = Recorder()
    gui["_stage6_summary"](rec, {"render_info": {
        "render_cuts": 10, "timeline_frames": 300,
        "plan_summary": {"clip_count": 10, "source_count": 3, "ai_tagged": 4, "seed": 0},
    }})
    planner_line = next(l for l in rec.lines if l.startswith("Planner:"))

    assert planner_line == "Planner: 10 clips, 3 sources, AI moments 4, seed legacy (0)"


def test_stage6_summary_reports_planner_cost_even_when_the_plan_was_empty(gui):
    """A planner that ran and fell back still spent the time; the measurement must not vanish."""
    rec = Recorder()
    gui["_stage6_summary"](rec, {"render_info": {
        "render_cuts": 282, "planner_seconds": 2.1,
        "planner_candidate_count": 18000, "planner_segment_count": 300,
    }})
    assert "Planner: fallback, 300 segments over 18000 candidates, 2.1s" in rec.text


# ======================================================================================
# H. the confirmation gate is measured, not weakened
# ======================================================================================


#: [FORK] Digital-Union (C3-R0): the shared live-source-gate + render core, which owns the ONE
#: authoritative `resolve_for_render` call and times it. `process_video_guarded` is the
#: single-render mutex wrapper and `render_selected_variants_guarded` the batch one; both reach
#: this same timed gate, so each batch candidate is re-verified independently.
GATE_CORE = "_process_video_guarded_unlocked"


def test_h_verification_is_timed_around_the_existing_gate_call():
    """**Re-pointed by C3-R0.** Gate timing is a property of the gate, not of the wrapper's name.

    C3-R0 moved the source gate into one shared core that both the single-render wrapper and the
    two-candidate batch wrapper call, so the timer moved with it. That makes this assertion
    stronger rather than weaker: every batch candidate is independently re-verified and gets its
    own `verification_seconds`, measured by this one timed call.
    """
    tree = _tree(_GUI)
    fn = _func(tree, GATE_CORE)
    code = _body_code(fn)

    assert len(_calls(fn, "resolve_for_render")) == 1, "one gate call, as before"
    # ...and it is the only one in the module: the wrappers delegate, they do not re-gate
    assert open(_GUI, encoding="utf-8").read().count("resolve_for_render(") == 1
    wrapper = _body_code(_func(tree, "process_video_guarded"))
    for duplicated in ("resolve_for_render(", "live_declaration(",
                       "verification_started = time.perf_counter()"):
        assert duplicated not in wrapper, f"the mutex wrapper duplicates {duplicated}"
    assert "verification_started = time.perf_counter()" in code
    assert "verification_seconds = time.perf_counter() - verification_started" in code

    lines = code.splitlines()
    opened = next(i for i, l in enumerate(lines) if "verification_started" in l)
    resolved = next(i for i, l in enumerate(lines) if l.startswith("decision = resolve_for_render"))
    closed = next(i for i, l in enumerate(lines) if "verification_seconds = time.perf" in l)
    denial = next(i for i, l in enumerate(lines) if "if not decision.allowed:" in l)
    assert opened < resolved < closed < denial, "the timer must not reorder the gate"


def test_h_the_gate_decision_and_the_verified_list_are_unchanged():
    code = _body_code(_func(_tree(_GUI), GATE_CORE))

    assert "live_declaration(source_mode, source_folder, source_recursive, video_input)" in code
    assert "if not decision.allowed:" in code
    assert "video_files=list(decision.paths)" in code
    # the measurement is reporting only: the timing statements may not read the decision, and the
    # decision statements may not read the timing
    for line in code.splitlines():
        if "verification_started" in line or line.startswith("verification_seconds ="):
            assert "decision" not in line, f"the measurement touched the gate: {line!r}"
        if line.startswith("decision =") or "if not decision.allowed" in line:
            assert "verification" not in line, f"the gate read the measurement: {line!r}"


def test_h_no_verification_result_is_cached_between_renders():
    """Nothing may be stored for reuse — every render re-verifies from the filesystem."""
    source = open(_GUI, encoding="utf-8").read()
    assert "verification_seconds" in source
    for forbidden in ("_verification_cache", "_last_verification", "cached_decision"):
        assert forbidden not in source, forbidden


def test_h_verification_is_reported_as_stage_zero_input_work():
    code = _body_code(_func(_tree(_GUI), "process_video"))

    assert "if verification_seconds is not None:" in code
    assert "stage=0" in code, "Stage.INPUT already exists for pre-Stage-1 source work"
    assert "Source verification:" in code
    assert "status_queue.put" in code, "the existing queue is the only cross-thread channel"


def test_h_stage_zero_event_renders_in_the_console_with_its_own_measurement(gui):
    """The logger never saw Stage 0 start, so it must use the event's elapsed, not its own clock."""
    from beatsync_fork.progress import EventKind, ProgressEvent

    stream = io.StringIO()
    logger = gui["StageConsoleLogger"](stream)
    logger.apply_event(ProgressEvent(
        stage=0, kind=EventKind.END,
        message="Source verification: 3.8s for 845 files",
        elapsed_seconds=3.8,
    ))
    out = stream.getvalue()

    assert "Source verification: 3.8s for 845 files" in out
    assert "Stage 0 ended in 4 seconds." in out, out


def test_h_normal_stages_still_time_themselves(gui):
    """The elapsed override must only apply to a stage this logger never saw start."""
    from beatsync_fork.progress import EventKind, ProgressEvent

    stream = io.StringIO()
    logger = gui["StageConsoleLogger"](stream)
    logger.apply_event(ProgressEvent(stage=5, kind=EventKind.START, message="Checking 3 sources"))
    logger.apply_event(ProgressEvent(
        stage=5, kind=EventKind.END, message="done", elapsed_seconds=3600.0))
    out = stream.getvalue()

    assert "Stage 5 ended in 0 seconds." in out, (
        "a stage the logger timed itself must keep its own measurement")


# ======================================================================================
# G. creative variation is untouched
# ======================================================================================


def test_g_variation_constants_are_unchanged():
    from beatsync_fork import variation

    assert variation.TOP_K == 6
    assert variation.SCORE_WINDOW == 0.12
    assert variation._WEIGHT_FLOOR == 0.25
    assert variation.LEGACY_SEED == 0


def test_g_this_pr_did_not_touch_seeded_selection():
    planner = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")
    code = open(planner, encoding="utf-8").read()

    # seed 0 keeps its original RNG stream (no seed component); a positive seed prepends it
    assert '_stable_rng(index, profile.get("target"), profile.get("start"))' in code
    assert '_stable_rng(seed, index, profile.get("target"), profile.get("start"))' in code
    assert "fork_variation.select_index(scores, rng)" in code
    variation_src = open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variation.py"),
                         encoding="utf-8").read()
    for forbidden in ("planner_seconds", "candidate_count", "diagnostic"):
        assert forbidden not in variation_src, forbidden


# ======================================================================================
# L0.1. the final success panel carries the baseline, because the console may not
# ======================================================================================


def _real_run_beat_info():
    """The first real L0 run: 41 sources, fully warm, no Qwen inference."""
    return {
        "video_analysis": {
            "source_count": 41, "cache_hits": 41,
            "cache_identity_seconds": 3.4, "cache_lookup_seconds": 0.2,
            "candidate_count": 742,
        },
        "render_info": {
            "planner_seconds": 0.4, "planner_candidate_count": 742,
            "planner_segment_count": 148,
        },
    }


def test_l01_final_block_carries_the_measurements_the_console_may_have_dropped(gui):
    block = gui["_scale_diagnostics_block"](0.0, _real_run_beat_info())

    assert block.splitlines() == [
        "Scale diagnostics:",
        "Source verification: 0.0s",
        "Stage 5: identity 3.4s · records 0.2s · cache 41/41 · 742 candidates",
        "Planner: 148 segments × 742 candidates · 0.4s",
    ]


def test_l01_a_measured_zero_is_shown_not_treated_as_missing(gui):
    """The real run verified 41 confirmed sources in 0.0s. That is a result, not an absence."""
    block = gui["_scale_diagnostics_block"](0.0, _real_run_beat_info())
    assert "Source verification: 0.0s" in block

    # and the same rule applies to every other measured zero
    zeroed = {
        "video_analysis": {"cache_identity_seconds": 0.0, "cache_lookup_seconds": 0.0,
                           "cache_hits": 0, "source_count": 0, "candidate_count": 0},
        "render_info": {"planner_seconds": 0.0, "planner_candidate_count": 0,
                        "planner_segment_count": 0},
    }
    text = gui["_scale_diagnostics_block"](0.0, zeroed)
    assert "Stage 5: identity 0.0s · records 0.0s · cache 0/0 · 0 candidates" in text
    assert "Planner: 0 segments × 0 candidates · 0.0s" in text


def test_l01_missing_metrics_are_omitted_never_fabricated(gui):
    partial = {
        "video_analysis": {"cache_identity_seconds": 3.4, "candidate_count": 742},
        "render_info": {"planner_seconds": 0.4},
    }
    block = gui["_scale_diagnostics_block"](None, partial)

    assert "Source verification" not in block, "an unsupplied measurement must not render as 0.0s"
    assert block.splitlines() == [
        "Scale diagnostics:",
        "Stage 5: identity 3.4s · 742 candidates",
        "Planner: 0.4s",
    ]
    assert "records" not in block and "cache " not in block and "segments" not in block


@pytest.mark.parametrize("beat_info", [None, {}, {"video_analysis": None, "render_info": None},
                                       {"video_analysis": [], "render_info": "nope"},
                                       {"video_analysis": {}, "render_info": {}}])
def test_l01_nothing_measured_means_no_block_at_all(gui, beat_info):
    """A direct/legacy caller that provides none of it leaves the success message untouched."""
    assert gui["_scale_diagnostics_block"](None, beat_info) == ""


def test_l01_candidate_count_comes_from_stage5_metadata(gui):
    """The Stage 5 line reads ``video_analysis``, never render_info or a recomputed list."""
    info = _real_run_beat_info()
    info["video_analysis"]["candidate_count"] = 999
    stage5_line = next(l for l in gui["_scale_diagnostics_block"](0.0, info).splitlines()
                       if l.startswith("Stage 5:"))
    assert "999 candidates" in stage5_line

    # removing it drops only that fragment; the rest of the Stage 5 line survives
    del info["video_analysis"]["candidate_count"]
    stage5_line = next(l for l in gui["_scale_diagnostics_block"](0.0, info).splitlines()
                       if l.startswith("Stage 5:"))
    assert "candidates" not in stage5_line
    assert "identity 3.4s" in stage5_line and "cache 41/41" in stage5_line


def test_l01_planner_figures_come_from_render_info_and_are_not_recomputed(gui):
    """Stage 6's own numbers win: the block must not re-derive them from Stage 5's candidates."""
    info = _real_run_beat_info()
    info["render_info"]["planner_candidate_count"] = 700   # deliberately != Stage 5's 742
    info["render_info"]["planner_segment_count"] = 148
    planner_line = next(l for l in gui["_scale_diagnostics_block"](0.0, info).splitlines()
                        if l.startswith("Planner:"))

    assert planner_line == "Planner: 148 segments × 700 candidates · 0.4s"
    assert "742" not in planner_line


def test_l01_block_is_appended_to_the_existing_success_message():
    """The success statistics are unchanged; the block is added after them."""
    code = _body_code(_func(_tree(_GUI), "_process_video_impl"))

    assert "status_msg = get_success_message_auto(" in code, "the existing builder still runs"
    assert "diagnostics = _scale_diagnostics_block(verification_seconds, beat_info)" in code
    assert "status_msg = f'{status_msg}\\n\\n{diagnostics}'" in code
    lines = code.splitlines()
    built = next(i for i, l in enumerate(lines) if "status_msg = get_success_message_auto(" in l)
    appended = next(i for i, l in enumerate(lines) if "_scale_diagnostics_block(" in l)
    assert built < appended, "the block is appended, never substituted"


def test_l01_verification_seconds_is_threaded_without_remeasuring():
    """It is invocation metadata already known to ``process_video`` — not measured again here."""
    tree = _tree(_GUI)
    impl = _func(tree, "_process_video_impl")
    assert "verification_seconds" in [a.arg for a in impl.args.args]

    body = _body_code(impl)
    assert "resolve_for_render" not in body, "the impl must never re-run the gate"
    assert "verification_seconds = " not in body, "it is received, never recomputed"

    # process_video passes through the value it already received
    worker = _body_code(_func(tree, "process_video"))
    assert "verification_seconds=verification_seconds" in worker


def test_l01_reads_only_values_l0_already_measured():
    """No rescan, no cache access, no analysis call inside the block builder."""
    body = _body_code(_func(_tree(_GUI), "_scale_diagnostics_block"))

    for forbidden in ("scan_folder", "_cache_path", "_load_cache", "os.stat", "os.path.getsize",
                      "build_planned_clip_sequence", "analyze_video_sources", "open("):
        assert forbidden not in body, forbidden
    # presence is `is not None`, so a measured zero survives and an absent value is dropped
    assert "is not None" in body
    assert "or 0" not in body, "a missing value must never be coerced to a fabricated zero"


def test_l01_pipeline_cache_and_planner_semantics_are_untouched():
    """L0.1 is presentation only."""
    with open(_VA, "r", encoding="utf-8") as handle:
        va = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in va
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in va

    vp_tree = _tree(_VP)
    calls = _calls(_func(vp_tree, "create_music_video"), "build_planned_clip_sequence")
    assert len(calls) == 1
    assert [kw.arg for kw in calls[0].keywords] == [
        "cut_times", "segment_durations", "beat_info", "video_files"]

    # the gate still resolves exactly once, in the shared core, before any delegation
    guard = _body_code(_func(_tree(_GUI), GATE_CORE))
    assert guard.count("resolve_for_render(") == 1
    assert "if not decision.allowed:" in guard

    # the block builder cannot mutate what it reads
    block = _body_code(_func(_tree(_GUI), "_scale_diagnostics_block"))
    for forbidden in ("beat_info[", "analysis[", "render_info[", ".pop(", ".update("):
        if forbidden in ("analysis[", "render_info["):
            continue  # read-only subscripts are fine; assignment is what matters
        assert forbidden not in block, forbidden
    assigns = [ast.unparse(n) for n in ast.walk(_func(_tree(_GUI), "_scale_diagnostics_block"))
               if isinstance(n, (ast.Assign, ast.AugAssign))]
    for statement in assigns:
        assert not statement.startswith(("beat_info[", "analysis[", "render_info[")), statement


# ======================================================================================
# 13. one readable end-to-end shape, with synthetic numbers and no thresholds
# ======================================================================================


def test_readable_diagnostic_shape(gui, capsys):
    """Prints the shape a 1,000-source run would produce. Numbers are synthetic by construction."""
    rec = Recorder()
    gui["_stage5_summary"](rec, _warm_result(
        source_count=1000, cache_hits=1000, analysis_seconds=12.7,
        candidate_count=18000, cache_identity_seconds=4.2, cache_lookup_seconds=1.7,
        cache_lookups=1000, summary="18000 visual moments, action=0.51, beauty=0.63, quality=0.74",
    ))
    gui["_stage6_summary"](rec, {"render_info": {
        "render_cuts": 300, "timeline_frames": 54000, "output_fps": 30.0,
        "planner_seconds": 2.1, "planner_candidate_count": 18000, "planner_segment_count": 300,
        "plan_summary": {"clip_count": 300, "source_count": 220, "ai_tagged": 5000, "seed": 202},
    }})
    print("\n" + rec.text)

    text = rec.text
    assert "Sources: 1000, cache 1000/1000, analyzed this run 0" in text
    assert "Cache check: identity 4.2s, records 1.7s" in text
    assert "18000 visual moments" in text
    assert "300 clips" in text and "18000 candidates" in text and "2.1s" in text
    # no threshold, no warning, no cap anywhere in the rendered diagnostics
    for forbidden in ("too many", "too large", "too slow", "limit", "exceed"):
        assert forbidden not in text.lower(), forbidden
