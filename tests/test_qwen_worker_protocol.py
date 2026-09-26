"""Anti-drift: the real worker really emits the protocol, and the real parent really streams it.

The streaming tests in ``test_qwen_stream.py`` prove the machinery works against a fake worker. That
leaves two ways for Phase 2B to rot silently:

* ``stage5_qwen_scene_worker.py`` stops emitting (or renames) the machine lines, so the parent streams
  a channel nobody writes to;
* ``video_analysis.py`` reverts one of the two launches to ``subprocess.run(capture_output=True)``, so
  live progress quietly works in batch mode only.

Neither file is importable on a bare interpreter — the worker needs cv2/PIL, ``video_analysis`` needs
the whole portable runtime — so both are inspected with ``ast``. That is deliberately stronger than
grep: call sites are matched by function, keyword arguments are checked by name, and the version probe
is distinguished from the worker launches by its argv, not by a line number.
"""

from __future__ import annotations

import ast
import json
import os

from beatsync_fork import qwen_progress as qp

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage5_qwen_scene_worker.py")
_PARENT = os.path.join(_REPO_ROOT, "src", "video_analysis.py")

REQUIRED_KINDS = {"job_start", "job_progress", "job_end"}


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _calls(tree: ast.AST, func_name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else ""
            )
            if name == func_name:
                found.append(node)
    return found


def _kwargs(call: ast.Call) -> set[str]:
    return {kw.arg for kw in call.keywords if kw.arg}


def _literal_kinds(calls: list[ast.Call]) -> set[str]:
    kinds = set()
    for call in calls:
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            kinds.add(call.args[0].value)
    return kinds


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


# ---------------------------------------------------------------------------
# the emitter helper serialises exactly what the parent parses
# ---------------------------------------------------------------------------


def test_encode_produces_one_namespaced_json_line():
    line = qp.encode("job_progress", job_index=17, job_total=300, current=64, total=120,
                     candidates_per_second=2.14, batch_size=8)

    assert line.startswith(qp.PROTOCOL_PREFIX)
    assert "\n" not in line and "\r" not in line
    payload = json.loads(line[len(qp.PROTOCOL_PREFIX):])
    assert payload["v"] == qp.PROTOCOL_VERSION
    assert payload["kind"] == "job_progress"
    assert payload["current"] == 64 and payload["total"] == 120
    assert payload["candidates_per_second"] == 2.14
    assert payload["batch_size"] == 8


def test_encoded_payload_survives_embedded_newlines_and_unicode():
    """A source filename can contain anything; the protocol must stay one parseable line."""
    line = qp.encode("job_start", source_name="clip\nwith\r\nbreaks — ünïcode.mp4",
                     job_index=1, job_total=1, requested_candidate_count=3)

    assert line.count("\n") == 0 and line.count("\r") == 0
    assert line.isascii(), "ensure_ascii keeps the line safe under any console code page"
    payload = qp.decode(line)
    assert payload["source_name"] == "clip\nwith\r\nbreaks — ünïcode.mp4"
    # And the translator flattens it before it can break a single-line status panel.
    event = qp.QwenProgressTranslator(min_interval=0.0).handle_payload(payload)
    assert "\n" not in event.message and "\r" not in event.message


def test_round_trip_for_every_kind_the_worker_emits():
    translator = qp.QwenProgressTranslator(min_interval=0.0)
    samples = {
        "job_start": dict(job_index=1, job_total=2, job_id="1", source_name="a.mp4",
                          requested_candidate_count=120),
        "job_progress": dict(job_index=1, job_total=2, current=8, total=120,
                             candidates_per_second=2.0, batch_size=8),
        "job_end": dict(job_index=1, job_total=2, frame_count=120, tag_count=119,
                        inference_seconds=54.0, prefetch_seconds=3.2),
        "worker_state": dict(state="backend_ready", message="Qwen backend ready", batch_size=8,
                             device="Vulkan0"),
    }
    for kind, fields in samples.items():
        payload = qp.decode(qp.encode(kind, **fields))
        assert payload is not None and payload["kind"] == kind
        event = translator.handle_payload(payload)
        assert event is not None, kind
        assert event.stage == 5 and event.data["phase"] == "qwen"


# ---------------------------------------------------------------------------
# §19 — the real worker emits it
# ---------------------------------------------------------------------------


def test_worker_defines_a_guarded_protocol_emitter():
    tree = _tree(_WORKER)
    emitter = _function(tree, "_emit_progress")

    # It must be import-guarded and exception-proof: a status line can never cost a Qwen run.
    assert any(isinstance(node, ast.Try) for node in ast.walk(emitter)), \
        "_emit_progress must swallow its own failures"
    source_names = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "beatsync_fork.qwen_progress" in source_names
    guarded = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Try)
        and any(isinstance(child, ast.ImportFrom) for child in node.body)
    ]
    assert guarded, "the fork import must be guarded so a partial checkout cannot break Qwen"


def test_worker_emits_every_required_protocol_kind():
    calls = _calls(_tree(_WORKER), "_emit_progress")
    kinds = _literal_kinds(calls)

    assert REQUIRED_KINDS <= kinds, f"missing kinds: {sorted(REQUIRED_KINDS - kinds)}"
    assert "worker_state" in kinds


def test_worker_job_progress_reports_the_live_denominator_and_measured_rate():
    """The numbers must be the worker's own: ``len(frame_items)`` and the computed ``rate``."""
    calls = [call for call in _calls(_tree(_WORKER), "_emit_progress")
             if _literal_kinds([call]) == {"job_progress"}]
    assert len(calls) == 1
    call = calls[0]
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}

    assert {"current", "total", "candidates_per_second", "batch_size"} <= set(keywords)
    # current is the loop index; total is the actual prefetched frame count, not a request count.
    assert isinstance(keywords["current"], ast.Name) and keywords["current"].id == "idx"
    total = keywords["total"]
    assert isinstance(total, ast.Call) and isinstance(total.func, ast.Name) and total.func.id == "len"
    assert isinstance(total.args[0], ast.Name) and total.args[0].id == "frame_items"
    # The rate is the worker's measured throughput, rounded, never recomputed by the parent.
    rate = keywords["candidates_per_second"]
    assert isinstance(rate, ast.Call) and rate.func.id == "round"
    assert "rate" in ast.dump(rate)
    # Job identity is forwarded, so every line can be attributed.
    assert any(isinstance(kw.value, ast.Name) and kw.value.id == "job_context"
               for kw in call.keywords if kw.arg is None)


def test_worker_job_start_reports_requested_count_and_job_identity():
    calls = [call for call in _calls(_tree(_WORKER), "_emit_progress")
             if _literal_kinds([call]) == {"job_start"}]
    assert len(calls) == 1
    assert "requested_candidate_count" in _kwargs(calls[0])

    # job_index / job_total / job_id / source_name travel in the job_context mapping.
    tree = _tree(_WORKER)
    main = _function(tree, "main")
    contexts = [
        node for node in ast.walk(main)
        if isinstance(node, ast.Dict)
        and {key.value for key in node.keys if isinstance(key, ast.Constant)}
        >= {"job_index", "job_total", "job_id", "source_name"}
    ]
    assert contexts, "job_context must carry job_index/job_total/job_id/source_name"


def test_worker_job_end_reports_actual_counts_and_elapsed_inference():
    calls = [call for call in _calls(_tree(_WORKER), "_emit_progress")
             if _literal_kinds([call]) == {"job_end"}]
    assert len(calls) == 1
    assert {"frame_count", "tag_count", "inference_seconds"} <= _kwargs(calls[0])


def test_worker_keeps_its_human_readable_lines():
    """The machine channel is additive. A CLI user must still see the same prose."""
    with open(_WORKER, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "Qwen llama.cpp tagged {idx}/{len(frame_items)} " in source
    assert "Qwen llama.cpp semantic inference total:" in source
    assert "Qwen llama.cpp job {job_index}/{len(jobs)}:" in source


def test_worker_llama_server_popen_lifecycle_is_untouched():
    """The child's own llama-server Popen is pre-existing and is not the parent streaming boundary."""
    tree = _tree(_WORKER)
    popens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    assert len(popens) == 1, "Phase 2B must not add or remove a Popen inside the worker"
    owner = _function(tree, "_start")
    assert any(node is popens[0] for node in ast.walk(owner)), \
        "the one Popen must remain LlamaServerClient._start's"


# ---------------------------------------------------------------------------
# §12 — both parent launches stream, the version probe does not
# ---------------------------------------------------------------------------


STREAMING_FUNCTIONS = ("_run_qwen_worker_batch", "_run_qwen_worker")


def test_both_qwen_launches_use_the_streaming_runner():
    tree = _tree(_PARENT)
    for name in STREAMING_FUNCTIONS:
        func = _function(tree, name)
        streamed = _calls(func, "run_qwen_worker")
        assert len(streamed) == 1, f"{name} must launch the worker through the streaming runner"
        assert "event_callback" in _kwargs(streamed[0]), f"{name} must forward event_callback"
        assert "timeout" in _kwargs(streamed[0]), f"{name} must keep a bounded wait"
        assert not _calls(func, "run"), f"{name} must not fall back to subprocess.run"


def test_the_llama_version_probe_is_still_a_plain_subprocess_run():
    """Explicitly out of scope: a 10-second synchronous probe needs no streaming."""
    tree = _tree(_PARENT)
    probe = _function(tree, "_llama_version_token")
    runs = _calls(probe, "run")

    assert len(runs) == 1
    assert "capture_output" in _kwargs(runs[0])
    assert "timeout" in _kwargs(runs[0])
    assert not _calls(probe, "run_qwen_worker")


def test_the_parent_adds_no_popen_of_its_own():
    """The parent's streaming goes through the tested fork runner, not an ad-hoc Popen."""
    tree = _tree(_PARENT)
    popens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ((isinstance(node.func, ast.Attribute) and node.func.attr == "Popen")
             or (isinstance(node.func, ast.Name) and node.func.id == "Popen"))
    ]
    assert popens == []


def test_event_callback_reaches_every_qwen_orchestrator_seam():
    """§12: live progress must not silently work for only one execution mode."""
    tree = _tree(_PARENT)
    for name in (
        "analyze_video_sources",
        "_analyze_single_video",
        "_complete_deferred_qwen",
        "_complete_deferred_qwen_batch",
        "_annotate_candidates_with_qwen",
        "_run_qwen_worker",
        "_run_qwen_worker_batch",
    ):
        func = _function(tree, name)
        args = [arg.arg for arg in func.args.args + func.args.kwonlyargs]
        assert "event_callback" in args, f"{name} cannot forward progress"

    # And the orchestrator actually passes it down both Qwen routes.
    orchestrator = _function(tree, "analyze_video_sources")
    for callee in ("_complete_deferred_qwen", "_complete_deferred_qwen_batch"):
        calls = _calls(orchestrator, callee)
        assert calls, f"{callee} is not called from analyze_video_sources"
        assert all("event_callback" in _kwargs(call) for call in calls), callee


def test_analyze_single_video_is_called_with_event_callback_in_the_right_position():
    """``_analyze_single_video`` is called positionally, so a reordered parameter is a silent bug.

    The same failure mode ``test_gui_guard_seam.py`` pins for the Gradio handler: nothing here raises
    if ``event_callback`` lands on ``total``, it just quietly corrupts the argument it displaces.
    """
    tree = _tree(_PARENT)
    params = [arg.arg for arg in _function(tree, "_analyze_single_video").args.args]
    expected = params.index("event_callback")

    # Direct calls, plus the ThreadPoolExecutor form where the function is an argument to submit() and
    # every positional argument is therefore shifted by one.
    sites = [(call.args, call.keywords) for call in _calls(tree, "_analyze_single_video")]
    for call in _calls(tree, "submit"):
        if call.args and isinstance(call.args[0], ast.Name) \
                and call.args[0].id == "_analyze_single_video":
            sites.append((call.args[1:], call.keywords))

    assert len(sites) == 3, f"expected 3 call sites (parallel submit, serial retry, serial): {len(sites)}"
    for args, keywords in sites:
        assert len(args) <= len(params), "more positional args than parameters"
        for index, arg in enumerate(args):
            if isinstance(arg, ast.Name) and arg.id == "event_callback":
                assert index == expected, (
                    f"event_callback passed at position {index}, parameter is at {expected}"
                )
                break
        else:
            assert any(kw.arg == "event_callback" for kw in keywords), \
                "a call site does not forward event_callback at all"


def test_failure_paths_emit_a_bounded_warning_and_return_fallback():
    tree = _tree(_PARENT)
    for name in STREAMING_FUNCTIONS:
        func = _function(tree, name)
        dumped = ast.dump(func)
        assert "_short_qwen_error" in dumped, f"{name} must bound the text it shows the UI"
        assert "TimeoutExpired" in dumped, f"{name} must keep timeout fallback semantics"
        # Fallback is still an empty dict, never a partial or reconstructed result.
        returns = [
            node for node in ast.walk(func)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
            and not node.value.keys
        ]
        assert len(returns) >= 3, f"{name} must return {{}} on failure, timeout and error"


def test_analysis_version_and_cache_identity_are_untouched():
    """A progress-only change must not invalidate a 758-video analysis cache."""
    tree = _tree(_PARENT)
    assigned = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = node.value.value
    assert assigned.get("ANALYSIS_VERSION") == "auto_av_analysis_v8_llama_vulkan_batched"

    # The cache key still mixes exactly the same tokens, with no progress-related field.
    signature = _function(tree, "_video_signature")
    dumped = ast.dump(signature)
    for token in ("ANALYSIS_VERSION", "st_size", "st_mtime", "model_token"):
        assert token in dumped, token
    assert "progress" not in dumped.lower()


def test_request_and_response_files_are_still_retained():
    """PCBUS-HK-v1 project override: the worker JSON files are deliberately kept for debugging."""
    tree = _tree(_PARENT)
    for name in STREAMING_FUNCTIONS:
        func = _function(tree, name)
        tries = [node for node in ast.walk(func) if isinstance(node, ast.Try) and node.finalbody]
        assert tries, f"{name} lost its finally block"
        for node in tries:
            for statement in node.finalbody:
                assert isinstance(statement, ast.Pass), \
                    f"{name} must not start deleting request/response files"
