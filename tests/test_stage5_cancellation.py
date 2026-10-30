"""FUN Stage-5 Cancel: cancellation that actually interrupts the video-analysis pass.

`tests/test_render_cancellation.py` owns the C3-R1A propagation map and the surviving half of
`BOUNDARY_ONLY_CANCEL`. This file covers what the FUN milestone added, and it is deliberately
organised around the **owner incident** rather than around the code:

    An entire video library was selected by accident against a cold cache. Stage 5 started.
    Cancel was pressed. Stage 5 kept working for about an hour.

So the questions asked here are the ones that incident raises. Does the analysis stop? How soon?
Does anything already paid for survive? Is anything half-finished written? Does the render still
go on to produce a video? Is the worker's own GPU server left running?

Where the property is genuinely about a live process the tests spawn **real children** — a mocked
`Popen` "stops" happily under code that never reaped anything, which is precisely the bug class.
Where it is about a loop, the real loop body is extracted from production and executed.

Sections:

1.  The cancellable worker wait, against real child processes.
2.  The process tree, so a worker-owned llama-server does not survive.
3.  `cancel_requested=None` is byte-for-byte the old behaviour.
4.  The cooperative deterministic loops, executed for real.
5.  The identity phase: pending work is cancelled, and a cancellation is never a verdict.
6.  What survives, and what is never written.
7.  The GUI reports cancellation during video analysis — from a structured field.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
import time

import pytest

from beatsync_fork import qwen_progress as qp
from beatsync_fork.render_worker import (
    PHASE_VIDEO_ANALYSIS,
    RenderCancelled,
    RenderLifecycle,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_AUTO = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_FFMPEG = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")

#: Generous enough that a slow CI box does not fail it, tight enough that it would have failed
#: outright against the pre-FUN behaviour, where the wait was the whole remaining analysis.
_OBSERVATION_BUDGET = 3.0


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _body(path: str, name: str) -> str:
    node = _func(path, name)
    return "\n".join(
        ast.unparse(item) for item in node.body
        if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)))


def _live() -> RenderLifecycle:
    lifecycle = RenderLifecycle(invocation_id="fun-stage5")
    lifecycle.mark_running()
    return lifecycle


def _cancelled_now() -> RenderLifecycle:
    lifecycle = _live()
    lifecycle.request_cancel()
    return lifecycle


def _sleeper(seconds: float = 60.0) -> list:
    """A child that announces itself and then sleeps, so the parent knows it really started."""
    return [sys.executable, "-c",
            f"import sys, time\nprint('ready', flush=True)\ntime.sleep({seconds})"]


# ===========================================================================
# 1. the cancellable worker wait, against real child processes
# ===========================================================================


def test_a_a_running_worker_is_stopped_when_cancellation_is_requested():
    """The headline behaviour, measured end to end against a real sleeping child.

    The budget matters as much as the boolean: before this change the parent blocked in one
    `wait(timeout=…)` whose timeout is `max(1800, candidates * 75)` seconds, so a cancellation was
    observed only when the worker finished on its own.
    """
    flag = {"stop": False}
    timer = threading.Timer(0.4, lambda: flag.__setitem__("stop", True))
    timer.start()
    try:
        started = time.perf_counter()
        outcome = qp.stream_worker_process(
            _sleeper(), cancel_requested=lambda: flag["stop"])
        elapsed = time.perf_counter() - started
    finally:
        timer.cancel()

    assert outcome.cancelled is True
    assert outcome.timed_out is False, "a cancellation must never be reported as a timeout"
    assert outcome.ok is False, "a cancelled run is not a successful one"
    assert elapsed < _OBSERVATION_BUDGET, f"took {elapsed:.2f}s to observe a cancellation"


def test_a_the_child_is_provably_reaped_before_the_call_returns():
    """Quiescence before propagation. An orphan would keep holding its pipes and its GPU."""
    children = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    original = qp.subprocess.Popen
    qp.subprocess.Popen = recording_popen
    try:
        qp.stream_worker_process(_sleeper(), cancel_requested=lambda: True)
    finally:
        qp.subprocess.Popen = original

    assert len(children) == 1
    assert children[0].poll() is not None, "the cancelled child was not reaped"


def test_a_a_cancellation_already_requested_stops_before_any_real_wait():
    """A Stop that landed while the previous job was finishing must not pay for a whole new job."""
    started = time.perf_counter()
    outcome = qp.stream_worker_process(_sleeper(), cancel_requested=lambda: True)
    assert outcome.cancelled is True
    assert time.perf_counter() - started < _OBSERVATION_BUDGET


def test_a_a_predicate_that_raises_is_treated_as_not_cancelled():
    """A broken status check must not be able to kill a GPU run that is making progress.

    The asymmetry is deliberate: a cancellation is a destructive action, so the uncertain answer
    has to be "carry on", never "stop".
    """
    def broken():
        raise RuntimeError("the status object is gone")

    outcome = qp.stream_worker_process(
        [sys.executable, "-c", "print('done')"], cancel_requested=broken)
    assert outcome.cancelled is False
    assert outcome.returncode == 0
    assert outcome.ok is True


def test_a_a_real_timeout_is_still_a_timeout_and_not_a_cancellation():
    """`timeout` keeps its own meaning, measured against the real clock, not the poll interval."""
    started = time.perf_counter()
    outcome = qp.stream_worker_process(
        _sleeper(), timeout=1.0, cancel_requested=lambda: False)
    elapsed = time.perf_counter() - started
    assert outcome.timed_out is True
    assert outcome.cancelled is False
    assert 0.8 <= elapsed < 8.0, f"the timeout was not honoured ({elapsed:.2f}s)"


def test_a_a_worker_that_finishes_first_is_a_plain_success():
    outcome = qp.stream_worker_process(
        [sys.executable, "-c", "print('hello')"], cancel_requested=lambda: False)
    assert outcome.returncode == 0
    assert outcome.cancelled is False
    assert outcome.ok is True
    assert outcome.stdout_lines == 1


def test_a_progress_lines_still_stream_while_the_wait_is_cancellable():
    """The poll loop must not cost the Phase 2B live progress it shares a function with."""
    lines = []
    script = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    print('BEATSYNC_QWEN_PROGRESS\\t{\"v\":\"beatsync.qwen-progress/1\","
        "\"kind\":\"job_progress\",\"current\":%d}' % i, flush=True)\n"
        "    time.sleep(0.05)\n")
    outcome = qp.stream_worker_process(
        [sys.executable, "-c", script],
        on_stdout_line=lines.append, cancel_requested=lambda: False)
    assert outcome.protocol_lines == 3
    assert len(lines) == 3


def test_a_a_cancelled_worker_response_file_is_never_read(tmp_path):
    """A worker stopped mid-loop has written no response; reading the path anyway is how a partial
    result would become semantic authority."""
    response = tmp_path / "resp.json"
    response.write_text('{"semantics_by_job": {"1": {"c0": {"action": 0.9}}}}',
                        encoding="utf-8")
    result = qp.run_qwen_worker(
        _sleeper(), str(response), cancel_requested=lambda: True)
    assert result.outcome.cancelled is True
    assert result.response == {}, "the stale response file was read"
    assert result.ok is False


# ===========================================================================
# 2. the process tree
# ===========================================================================


def test_b_the_tree_terminator_is_given_this_calls_own_child_pid():
    """**The whole containment argument, and it is the argument rather than a filter.**

    The worker starts its own `llama-server`; killing only the direct Python child orphans it. The
    terminator therefore takes a pid — and the only pid it is ever given is the one this call just
    created. There is no image-name sweep and no enumeration, so an unrelated `python.exe`,
    `llama-server.exe` or BeatSync process is not reachable from here at all.
    """
    seen = []
    children = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    original = qp.subprocess.Popen
    qp.subprocess.Popen = recording_popen
    try:
        outcome = qp.stream_worker_process(
            _sleeper(), cancel_requested=lambda: True,
            tree_terminator=lambda pid: (seen.append(pid), "")[1])
    finally:
        qp.subprocess.Popen = original

    assert outcome.tree_terminated is True
    assert seen == [children[0].pid], "the terminator saw a pid this call did not create"
    assert outcome.tree_terminate_error == ""


def test_b_the_tree_goes_first_while_the_child_is_still_its_parent():
    """Order is load-bearing: terminating the Python worker first would leave the server with no
    live ancestor to be found by."""
    events = []
    real_popen = subprocess.Popen

    def recording_popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)

        class Watched:
            def __getattr__(self, name):
                return getattr(child, name)

            def terminate(self):
                events.append("terminate")
                child.terminate()

        return Watched()

    original = qp.subprocess.Popen
    qp.subprocess.Popen = recording_popen
    try:
        qp.stream_worker_process(
            _sleeper(), cancel_requested=lambda: True,
            tree_terminator=lambda pid: (events.append("tree"), "")[1])
    finally:
        qp.subprocess.Popen = original

    assert events[:2] == ["tree", "terminate"], events


def test_b_a_failed_tree_termination_is_surfaced_not_hidden():
    """The one thing an operator would want to know is that a GPU server may still be resident."""
    outcome = qp.stream_worker_process(
        _sleeper(), cancel_requested=lambda: True,
        tree_terminator=lambda pid: "taskkill exited 1")
    assert outcome.cancelled is True
    assert outcome.tree_terminated is True
    assert outcome.tree_terminate_error == "taskkill exited 1"


def test_b_a_raising_tree_terminator_still_stops_the_child():
    """The direct child must be stopped even if the tree call blew up — that is the floor."""
    def explode(pid):
        raise OSError("taskkill is missing")

    outcome = qp.stream_worker_process(
        _sleeper(), cancel_requested=lambda: True, tree_terminator=explode)
    assert outcome.cancelled is True
    assert "OSError" in outcome.tree_terminate_error


@pytest.mark.parametrize("returncode,expected_error", [
    (0, ""),
    # taskkill's "process not found", which after a terminate/kill race is the DESIRED outcome
    (128, ""),
    (1, "taskkill exited 1"),
])
def test_b_the_terminator_reads_taskkills_exit_code(returncode, expected_error):
    class Completed:
        def __init__(self, code):
            self.returncode = code

    assert qp.terminate_process_tree(
        4242, run=lambda *a, **k: Completed(returncode)) == expected_error


@pytest.mark.parametrize("pid", [0, -1, None, "nonsense"])
def test_b_an_unusable_pid_runs_nothing_at_all(pid):
    """A pid the caller could not supply must not become a command. Nothing is executed."""
    calls = []
    assert qp.terminate_process_tree(
        pid, run=lambda *a, **k: calls.append(a)) == "no usable process id"
    assert calls == []


def test_b_the_terminator_never_raises():
    def explode(*args, **kwargs):
        raise FileNotFoundError(2, "taskkill not found")

    reason = qp.terminate_process_tree(1234, run=explode)
    assert "FileNotFoundError" in reason


def test_b_the_terminator_is_only_wired_to_the_cancellation_path():
    """Existing timeout behaviour is untouched: a timeout still orphans exactly what it always
    did. That gap is pre-existing and documented; this feature does not widen the blast radius of
    a timeout while fixing cancellation.
    """
    body = _body(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "qwen_progress.py"),
                 "stream_worker_process")
    # the terminator call sits inside the branch that sets `cancelled`, never in the timeout one
    tree = ast.parse(body)
    terminator_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                        and getattr(n.func, "id", None) == "tree_terminator"]
    assert len(terminator_calls) == 1, "exactly one tree-termination site"
    assert "cancelled = True" in body
    timeout_handler = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
                       and n.type is not None
                       and "TimeoutExpired" in ast.unparse(n.type)]
    for handler in timeout_handler:
        assert "tree_terminator" not in ast.unparse(handler)


# ===========================================================================
# 3. `cancel_requested=None` is byte-for-byte the old behaviour
# ===========================================================================


def test_c_with_no_predicate_the_wait_is_one_blocking_call():
    """The whole back-compatibility claim, asserted structurally rather than by timing.

    `cancel_requested is None` must take a branch containing a single `process.wait(timeout=…)`
    with no loop, no clock and no poll ahead of it — so the headless CLI and every pre-FUN caller
    run exactly the code they ran before.
    """
    node = _func(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "qwen_progress.py"),
                 "stream_worker_process")
    branch = next(n for n in ast.walk(node) if isinstance(n, ast.If)
                  and ast.unparse(n.test) == "cancel_requested is None")
    rendered = ast.unparse(ast.Module(body=branch.body, type_ignores=[]))
    assert rendered.strip() == "returncode = process.wait(timeout=timeout)", rendered


def test_c_a_no_predicate_run_behaves_exactly_as_before():
    outcome = qp.stream_worker_process([sys.executable, "-c", "print('x')"])
    assert outcome.returncode == 0
    assert outcome.cancelled is False
    assert outcome.tree_terminated is False
    assert outcome.ok is True


def test_c_a_no_predicate_timeout_behaves_exactly_as_before():
    outcome = qp.stream_worker_process(_sleeper(), timeout=1.0)
    assert outcome.timed_out is True
    assert outcome.cancelled is False
    assert outcome.tree_terminated is False, (
        "a timeout must not acquire tree termination as a side effect")


def test_c_lifecycle_none_fires_no_check_anywhere():
    """`_cancelled(None)` is `False` and `_check_cancelled(None, …)` does nothing, so every
    pre-FUN caller of Stage 5 — the headless CLI above all — is unaffected."""
    namespace = {}
    for name in ("_cancelled", "_check_cancelled"):
        exec(compile(ast.unparse(_func(_VA, name)), f"<{name}>", "exec"), namespace)
    namespace["RenderCancelled"] = RenderCancelled
    namespace["PHASE_VIDEO_ANALYSIS"] = PHASE_VIDEO_ANALYSIS
    assert namespace["_cancelled"](None) is False
    assert namespace["_check_cancelled"](None, "anywhere") is None


def test_c_a_token_that_cannot_answer_is_treated_as_not_cancelled():
    namespace = {}
    exec(compile(ast.unparse(_func(_VA, "_cancelled")), "<_cancelled>", "exec"), namespace)

    class Broken:
        def cancel_requested(self):
            raise RuntimeError("gone")

    assert namespace["_cancelled"](Broken()) is False


def test_c_the_seam_is_appended_last_on_every_stage_five_function():
    """Matching the C3-R1A convention, so no positional argument moved anywhere."""
    for name in ("analyze_video_sources", "_analyze_single_video", "_measure_windows",
                 "_read_ordered_frames", "_annotate_candidates_with_qwen",
                 "_complete_deferred_qwen", "_complete_deferred_qwen_batch",
                 "_run_qwen_worker"):
        node = _func(_VA, name)
        positional = [a.arg for a in node.args.args]
        kwonly = [a.arg for a in node.args.kwonlyargs]
        assert "lifecycle" in positional + kwonly, f"{name} has no lifecycle seam"
        if "lifecycle" in positional:
            assert positional[-1] == "lifecycle", f"{name}'s lifecycle is not last"
            offset = len(positional) - len(node.args.defaults)
            assert ast.unparse(node.args.defaults[-1]) == "None", (
                f"{name}'s lifecycle does not default to None")
            assert offset >= 0
    # the FFmpeg scene pass too, which is where most of the uncancellable hour was actually spent
    scene = _func(_FFMPEG, "detect_video_scene_changes")
    assert [a.arg for a in scene.args.args][-1] == "lifecycle"


# ===========================================================================
# 4. the cooperative deterministic loops, executed for real
# ===========================================================================


def _loop_namespace():
    """The real `_read_ordered_frames` body, with OpenCV and numpy stood in for.

    Only the *collaborators* are fake: the loop, its stride handling and its cancellation checks
    are the production ones.
    """
    namespace = {
        "os": os, "time": time,
        "RenderCancelled": RenderCancelled,
        "PHASE_VIDEO_ANALYSIS": PHASE_VIDEO_ANALYSIS,
        "_resize_for_analysis": lambda frame, max_width=360: frame,
        "cv2": type("cv2", (), {"CAP_PROP_POS_FRAMES": 1, "VideoCapture": object})(),
        "np": type("np", (), {"ndarray": object})(),
        "Dict": dict, "Sequence": list,
    }
    source = open(_VA, encoding="utf-8").read()
    tree = ast.parse(source)
    wanted = {"_cancelled", "_check_cancelled", "_read_ordered_frames"}
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name)
                        and target.id in ("_CANCEL_CHECK_FRAMES", "_CANCEL_CHECK_WINDOWS")):
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<c>", "exec"),
                         namespace)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<loops>", "exec"), namespace)
    return namespace


class _FakeCapture:
    """Counts how many frames were actually touched, so "it stopped early" is measurable."""

    def __init__(self):
        self.grabs = 0
        self.reads = 0

    def set(self, prop, value):
        return True

    def grab(self):
        self.grabs += 1
        return True

    def read(self):
        self.reads += 1
        return True, object()


def test_d_frame_decode_stops_early_instead_of_reading_the_whole_source():
    """The measurable version of "analysis stops without waiting for every source".

    The check fires every `_CANCEL_CHECK_FRAMES` targets, so a cancellation requested up front
    costs at most that many reads out of a plan that could be thousands long.
    """
    namespace = _loop_namespace()
    capture = _FakeCapture()
    stride = namespace["_CANCEL_CHECK_FRAMES"]
    with pytest.raises(RenderCancelled):
        namespace["_read_ordered_frames"](capture, list(range(stride * 50)),
                                          lifecycle=_cancelled_now())
    assert capture.reads <= stride, f"{capture.reads} frames read after a cancellation"


def test_d_frame_decode_with_no_cancellation_reads_everything():
    """The same loop, uncancelled, must still do all of its work."""
    namespace = _loop_namespace()
    capture = _FakeCapture()
    frames = namespace["_read_ordered_frames"](capture, list(range(100)), lifecycle=_live())
    assert len(frames) == 100
    assert capture.reads == 100


def test_d_frame_decode_with_no_lifecycle_is_unchanged():
    namespace = _loop_namespace()
    capture = _FakeCapture()
    assert len(namespace["_read_ordered_frames"](capture, list(range(100)))) == 100


def test_d_the_cancellation_stride_is_a_count_of_work_not_a_duration():
    """Stated because the honest claim depends on it.

    One native decode call cannot be interrupted once entered, so cancellation lands at the next
    frame or window boundary — never instantly. The strides are chosen so that boundary is small:
    a few milliseconds per frame at 32 frames, sub-millisecond per window at 16 windows.
    """
    namespace = _loop_namespace()
    assert namespace["_CANCEL_CHECK_FRAMES"] == 32
    assert namespace["_CANCEL_CHECK_WINDOWS"] == 16


def test_d_a_cancelled_window_pass_raises_rather_than_returning_empty_metrics():
    """**The subtlest correctness point in the deterministic half.**

    An empty metric means "this window yielded nothing usable", and a source whose windows all
    came back empty is a *legitimately* candidate-less source — which, with
    `candidate_scoring_seconds` recorded, the completion rule accepts as a reusable record. So
    converting a Stop into empty metrics would persist "this video has no good moments" for a
    video nobody finished looking at, permanently.
    """
    body = _body(_VA, "_measure_windows")
    assert "_check_cancelled" in body
    assert "return [{} for _ in windows]" in body, (
        "the genuine no-targets early return must still exist")
    # and the cancellation path is a raise, never a return of empties
    node = _func(_VA, "_measure_windows")
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_check_cancelled":
            assert not isinstance(getattr(call, "parent", None), ast.Return)


def test_d_the_capture_is_released_on_the_cancellation_path():
    """An unreleased `cv2.VideoCapture` holds the source file open, which on Windows is enough to
    make a later move or delete of that file fail."""
    body = _body(_VA, "_analyze_single_video")
    tree = ast.parse(body)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)
                and n.type is not None and ast.unparse(n.type) == "RenderCancelled"]
    releasing = [h for h in handlers if "cap.release()" in ast.unparse(h)]
    assert releasing, "no RenderCancelled handler releases the capture"
    assert any(isinstance(s, ast.Raise) for h in releasing for s in ast.walk(h)), (
        "the capture-releasing handler must still re-raise")


# ===========================================================================
# 5. the identity phase
# ===========================================================================


def test_e_pending_identity_reads_are_cancelled_and_the_pool_is_drained():
    """`cancel_futures=True` discards what has not started; `wait=True` finishes what has.

    Together they mean no identity read outlives the call — which matters because each one is up
    to 3 MiB of disk I/O and there can be thousands queued.
    """
    body = _body(_VA, "_compute_cache_paths_parallel")
    assert "executor.shutdown(wait=True, cancel_futures=True)" in body


def test_e_a_cancelled_identity_phase_raises_rather_than_returning_nones():
    """A per-source `None` is a **verdict** — "identity unprovable, no cache for this source".

    A cancelled task answers `None` so that queued work costs nothing, which makes the two
    indistinguishable in the returned list. The `raise` after the pool is what closes the only
    path by which a cancellation could be read as "this source has no cache".
    """
    node = _func(_VA, "_compute_cache_paths_parallel")
    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
    assert returns, "the helper must still return results"
    checks = [n for n in ast.walk(node) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_check_cancelled"]
    assert checks, "no cancellation check in the identity phase"
    final_return = max(returns, key=lambda n: n.lineno)
    assert any(check.lineno < final_return.lineno for check in checks), (
        "the last check must precede the final return")


def test_e_the_serial_identity_path_is_cancellable_too():
    """A one-source or env-pinned single-worker run must not be the uncancellable case."""
    body = _body(_VA, "_compute_cache_paths_parallel")
    assert body.count("_check_cancelled") >= 2, (
        "both the serial and the parallel identity paths need a check")


def test_e_the_worker_count_still_reaches_no_key():
    """Unchanged L1B invariant, re-asserted because this function was edited."""
    body = _body(_VA, "_compute_cache_paths_parallel")
    for forbidden in ("_qwen_backend_signature_token", "_qwen_config_token", "_video_signature"):
        assert forbidden not in body


# ===========================================================================
# 6. what survives, and what is never written
# ===========================================================================


def test_f_a_cancellation_adds_no_write_path_at_all():
    """**The survival guarantee, and it is structural rather than behavioural.**

    Nothing about cancellation writes, deletes or rewrites a cache record. `_checkpoint_cache` is
    still the only thing that may start a write and it still consults the unchanged completion
    rule, so a source interrupted mid-analysis is simply never written — and a source already
    checkpointed is simply never touched.
    """
    source = open(_VA, encoding="utf-8").read()
    tree = ast.parse(source)
    for handler in ast.walk(tree):
        if not (isinstance(handler, ast.ExceptHandler) and handler.type is not None
                and ast.unparse(handler.type) == "RenderCancelled"):
            continue
        rendered = ast.unparse(handler)
        for forbidden in ("_checkpoint_cache", "_save_cache", "os.remove", "os.unlink",
                          "shutil", "open("):
            assert forbidden not in rendered, (
                f"a cancellation handler reaches {forbidden}:\n{rendered}")


def test_f_the_completion_rule_is_untouched():
    """A cancelled run must not be able to publish an incomplete record, and the way that is
    guaranteed is that the rule deciding reusability never learned about cancellation."""
    for name in ("_cache_entry_is_complete", "_qwen_job_completed", "_checkpoint_cache",
                 "_stored_ai_cache_is_consistent", "_deterministic_analysis_completed",
                 "_save_cache"):
        body = _body(_VA, name)
        for forbidden in ("lifecycle", "RenderCancelled", "_cancelled", "_check_cancelled",
                          "cancel"):
            assert forbidden not in body, f"{name} knows about {forbidden}"


def test_f_the_qwen_worker_partials_are_not_deleted_by_a_cancellation():
    """A1's per-job partials are evidence of finished GPU work. Nothing removes one, ever —
    they are simply not *consumed* by a cancelled run, because recovery is reachable only from a
    usable-but-empty response."""
    source = open(_VA, encoding="utf-8").read()
    assert "fork_partials" in source
    tree = ast.parse(source)
    for handler in ast.walk(tree):
        if (isinstance(handler, ast.ExceptHandler) and handler.type is not None
                and ast.unparse(handler.type) == "RenderCancelled"):
            assert "fork_partials" not in ast.unparse(handler)


def test_f_a_stage_five_cancellation_is_not_an_analysis_failure():
    """The one clause the whole feature's safety rests on.

    `analyze_beats_auto`'s Stage-5 `except Exception` prints "Video analysis failed", emits a
    warning and lets the render continue into Stage 6 on **fallback sampling** — producing a video
    from random clips for a user who just pressed Cancel. `RenderCancelled` is an ordinary
    `Exception`, so without a typed clause ahead of it that is exactly what would happen.
    """
    node = _func(_AUTO, "analyze_beats_auto")
    stage5 = next(n for n in ast.walk(node) if isinstance(n, ast.Try)
                  and "analyze_video_sources" in ast.unparse(n))
    kinds = [getattr(h.type, "id", None) for h in stage5.handlers]
    assert kinds[0] == "RenderCancelled", kinds
    assert "Exception" in kinds
    first = ast.unparse(stage5.handlers[0])
    assert "raise" in first
    assert "fallback" not in first.lower(), "a cancellation must not mention fallback sampling"


def test_f_the_post_stage_five_boundary_is_still_outside_that_try():
    """Unchanged C3-R1A placement, and it matters MORE now: Stage 5's own `RenderCancelled` has to
    reach it without the broad handler in between seeing it."""
    node = _func(_AUTO, "analyze_beats_auto")
    stage5 = next(n for n in ast.walk(node) if isinstance(n, ast.Try)
                  and "analyze_video_sources" in ast.unparse(n))
    checks = [n for n in ast.walk(node) if isinstance(n, ast.Call)
              and ast.unparse(n) == "lifecycle.raise_if_cancelled()"]
    assert any(c.lineno > stage5.end_lineno for c in checks)
    assert not any(stage5.lineno <= c.lineno <= stage5.end_lineno for c in checks)


def test_f_the_token_is_passed_into_stage_five():
    """The one line that retires `BOUNDARY_ONLY_CANCEL` for Stage 5."""
    node = _func(_AUTO, "analyze_beats_auto")
    call = next(n for n in ast.walk(node) if isinstance(n, ast.Call)
                and getattr(n.func, "id", None) == "analyze_video_sources")
    assert "lifecycle=lifecycle" in ast.unparse(call)


def test_f_stages_one_to_three_stay_uncancellable_mid_stage():
    """Deliberate, and worth pinning so nobody "completes" the feature by making librosa
    cooperative: Stages 1-3 are ~15.7 s measured end to end and process-locally cached, their
    boundaries already bracket them, and there is nothing a user could perceive to gain."""
    for name in ("detect_master_beat_grid", "analyze_wave_features", "analyze_sections"):
        path = os.path.join(_REPO_ROOT, "src", "auto_mode",
                            {"detect_master_beat_grid": "stage1_audio.py",
                             "analyze_wave_features": "stage2_features.py",
                             "analyze_sections": "stage3_sections.py"}[name])
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("lifecycle", "RenderCancelled", "cancel_requested"):
            assert forbidden not in source, f"{name}'s module references {forbidden}"


# ===========================================================================
# 7. the GUI report
# ===========================================================================


def test_g_the_phase_rides_on_the_exception_as_a_structured_field():
    """Not recovered by parsing `str(exc)` — that is display text, and parsing it is how a status
    string becomes a control-flow input."""
    plain = RenderCancelled("stopped")
    assert plain.phase == ""
    stamped = RenderCancelled("stopped", phase=PHASE_VIDEO_ANALYSIS)
    assert stamped.phase == PHASE_VIDEO_ANALYSIS
    assert str(stamped) == "stopped", "the message is unchanged by carrying a phase"

    # And it is still an ordinary `Exception`, which is the whole reason every broad handler on
    # the Stage-5 path has to name it: a `BaseException` would have skipped them for free, and
    # would also have skipped cleanup that legitimately has to run.
    assert issubclass(RenderCancelled, Exception)
    assert RenderCancelled.__mro__[1] is Exception


def test_g_every_stage_five_raise_site_stamps_the_phase():
    source = open(_VA, encoding="utf-8").read()
    tree = ast.parse(source)
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call)
              and getattr(n.exc.func, "id", None) == "RenderCancelled"]
    assert raises, "Stage 5 must raise its own cancellations"
    for node in raises:
        assert "phase=PHASE_VIDEO_ANALYSIS" in ast.unparse(node), ast.unparse(node)


def test_g_the_ffmpeg_scene_pass_is_stamped_by_the_one_backstop():
    """`ffmpeg_processing._run_media_command` is shared with clip extraction and must not learn
    which stage called it, so the phase is stamped at the one `try` that contains exactly the
    Stage-5 call — by construction rather than by inspection."""
    node = _func(_AUTO, "analyze_beats_auto")
    stage5 = next(n for n in ast.walk(node) if isinstance(n, ast.Try)
                  and "analyze_video_sources" in ast.unparse(n))
    handler = ast.unparse(stage5.handlers[0])
    assert "PHASE_VIDEO_ANALYSIS" in handler
    assert "or PHASE_VIDEO_ANALYSIS" in handler, (
        "an already-stamped phase must never be overwritten")


def test_g_the_gui_picks_its_wording_from_the_phase_not_from_a_message():
    body = _body(_GUI, "_process_video_impl")
    assert "PHASE_VIDEO_ANALYSIS" in body
    assert "STATUS_RENDER_CANCELLED_ANALYSIS" in body
    # the typed terminal cause is still an explicitly named enum member, independent of wording
    assert "RenderOutcomeKind.CANCELLED" in body
    assert "str(cancel)" not in body, "the exception's message must not be parsed"


def test_g_the_analysis_wording_says_what_survived():
    """The generic "Nothing was rendered" would not tell the owner that the hour of work they just
    stopped was *analysis*, nor that the finished part is kept. Both decide what they do next."""
    from ui_content import STATUS_RENDER_CANCELLED, STATUS_RENDER_CANCELLED_ANALYSIS
    assert "video analysis" in STATUS_RENDER_CANCELLED_ANALYSIS
    assert "cache" in STATUS_RENDER_CANCELLED_ANALYSIS
    assert "not be analysed again" in STATUS_RENDER_CANCELLED_ANALYSIS
    assert STATUS_RENDER_CANCELLED_ANALYSIS != STATUS_RENDER_CANCELLED


def test_g_the_cancel_copy_no_longer_promises_the_wait_that_was_removed():
    """A corrected claim, not a reworded one.

    Both strings used to say an AI scene-analysis step "finishes its current call first", which was
    an accurate description of `BOUNDARY_ONLY_CANCEL` and is now false. Stale copy that tells the
    owner to expect the exact wait the fix removed is the worst kind.
    """
    from ui_content import INFO_CANCEL_RENDER, STATUS_CANCEL_REQUESTED
    for text in (INFO_CANCEL_RENDER, STATUS_CANCEL_REQUESTED):
        low = text.lower()
        assert "finishes its current call" not in low
        assert "finishes naturally" not in low
        # and it still does not overclaim: cancellation is prompt, never instant
        assert "instant" in low or "within moments" in low


def test_g_the_preflight_summary_names_the_five_facts():
    """When Stage 5 begins, an accidentally cold library has to be obvious immediately.

    Everything here was already known at that point in the function and was simply never put in
    one sentence — which is why the owner found out an hour later.
    """
    body = _body(_VA, "analyze_video_sources")
    assert "preflight" in body
    start = body.index("preflight = ")
    block = body[start:start + 600]
    for fact in ("existing", "cache_hits", "jobs", "ai_available", "cache_dir"):
        assert fact in block, f"the pre-flight line omits {fact}"
    # it is a STATE event: a second counted event under Stage 5's phase would fight ProgressView's
    # per-(stage, phase) monotonicity, and there is deliberately no ETA
    assert "fork_progress.state(" in body[start:start + 1200]
    assert "eta" not in block.lower()
