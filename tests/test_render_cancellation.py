"""C3-R1A: cancelling a render, across every module the token travels through.

`tests/test_render_worker.py` covers the pure contract in isolation. This file covers the part that
spans modules — and the part where a cancellation feature usually goes wrong: not in the flag, but
in *where it is observed*, *what still gets reaped*, and *which broad `except Exception` swallows it*.

None of the five runtime modules can be imported on a bare interpreter, so this file uses the house
techniques described in `.claude/rules/test-harness.md`: `ast` inspection of the real source, and
AST-extraction of real function bodies executed against a synthesised namespace. Where the property
is genuinely about a live process or a live thread pool, real children and a real
`ThreadPoolExecutor` are used — a mocked `Popen` would "stop" happily under code that never reaped
anything.

Sections, and what each is honestly worth:

1.  The propagation map — every seam takes an optional trailing `lifecycle`, structurally.
2.  `lifecycle is None` is today's behaviour, byte-for-byte. The whole back-compatibility claim.
3.  The two media runners, against REAL child processes: terminate -> grace -> kill -> **reap**,
    and `RenderCancelled` raised only once the reap returned.
4.  BOUNDARY_ONLY_CANCEL — the token never reaches Stage 5 / Qwen, and the post-Stage-5 boundary
    sits outside that stage's broad `except`.
5.  `RenderCancelled` survives every broad catch, pinned site by site.
6.  The clip executor: quiescence before propagation.
7.  `create_clip_parallel`, executed for real.
8.  A cancelled mixdown discards its partial master and still raises a typed cancellation.
9.  The capacity-one active-render slot and the Cancel handler, executed for real.
10. One lifecycle per top-level render event; terminal-marking only in the two wrappers.
11. Only a plain string crosses into `gr.State`; Cancel has its own lane and never `cancels=`.
12. Durable promotion is the ONE success commit point — including the frozen case where a
    cancellation lands during the ProRes preview *after* promotion and the render stays a SUCCESS.
13. Nothing about cache, schema or version identity moved.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
import time
import types

import pytest

from beatsync_fork import render_worker as rw
from beatsync_fork.render_worker import RenderCancelled, RenderLifecycle

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    return os.path.join(_REPO_ROOT, "src", *parts)


_FFMPEG = _src("ffmpeg_processing.py")
_MIXDOWN = _src("audio_mixdown.py")
_PROCESSOR = _src("video_processor.py")
_AUTO = _src("auto_mode", "__init__.py")
_GUI = _src("gui.py")
_ANALYSIS = _src("video_analysis.py")
_QWEN_WORKER = _src("auto_mode", "stage5_qwen_scene_worker.py")
_QWEN_PROGRESS = _src("beatsync_fork", "qwen_progress.py")


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _source(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _executable_source(path: str) -> str:
    """Source with every docstring stripped, recursively.

    Load-bearing for this file in particular: several of these modules' docstrings deliberately *name*
    the thing they must not do ("this module never imports `ffmpeg_processing._run_media_command`"),
    so prose must never be allowed to read as implementation.
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _func(path: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _body(path: str, name: str) -> str:
    """One function's body, docstrings stripped — prose must never read as implementation."""
    node = _func(path, name)
    return "\n".join(
        ast.unparse(stmt) for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)))


def _cancelled(invocation_id="inv-test"):
    life = RenderLifecycle(invocation_id=invocation_id)
    life.mark_running()
    life.request_cancel()
    return life


def _live(invocation_id="inv-test"):
    life = RenderLifecycle(invocation_id=invocation_id)
    life.mark_running()
    return life


# ===========================================================================
# 1. The propagation map
# ===========================================================================

#: Every function the lifecycle is threaded through, and the module it lives in. `create_clip_parallel`
#: is deliberately absent: it takes the lifecycle as a trailing *tuple element*, not a parameter,
#: because it is an `executor.submit` target with a single packed argument. Section 7 covers it.
SEAMS = (
    (_FFMPEG, "_run_media_command"),
    (_FFMPEG, "convert_to_prores_proxy"),
    (_FFMPEG, "extract_clip_segment_ffmpeg_detailed"),
    (_FFMPEG, "extract_prores_segment_random"),
    (_FFMPEG, "concatenate_videos_ffmpeg"),
    (_MIXDOWN, "_run"),
    (_MIXDOWN, "probe_duration"),
    (_MIXDOWN, "render_mixed_master"),
    (_MIXDOWN, "build_mixed_master"),
    (_PROCESSOR, "create_music_video"),
    (_AUTO, "analyze_beats_auto"),
    (_GUI, "_process_video_impl"),
    (_GUI, "process_video"),
    (_GUI, "_process_video_guarded_unlocked"),
)


@pytest.mark.parametrize("path,name", SEAMS, ids=[f"{os.path.basename(p)}:{n}" for p, n in SEAMS])
def test_every_seam_takes_lifecycle_last_and_defaulted(path, name):
    """**Appended LAST with a default `None`**, at every single seam.

    Both halves matter and for different reasons. *Defaulted* is the back-compatibility contract:
    the headless CLI, `video_processor.main`, and every test in this suite keep calling these
    functions exactly as before. *Last* is the positional contract: Gradio and several internal call
    sites pass arguments positionally, and inserting a parameter anywhere else would silently
    re-bind every argument after it.
    """
    node = _func(path, name)
    params = [a.arg for a in node.args.args]
    assert "lifecycle" in params, f"{name} does not accept a lifecycle"
    assert params[-1] == "lifecycle", f"{name} takes lifecycle at {params.index('lifecycle')}, not last"
    assert not node.args.kwonlyargs, f"{name} grew a keyword-only argument"

    # the default is literally None, not a factory and not a sentinel object
    default = node.args.defaults[-1]
    assert isinstance(default, ast.Constant) and default.value is None, ast.unparse(default)


def test_the_lifecycle_is_never_stored_on_a_record_or_a_module_global():
    """It is a runtime token, not state. Storing it would outlive the render that owns it."""
    for path in (_FFMPEG, _MIXDOWN, _PROCESSOR, _AUTO):
        for node in _tree(path).body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                assert "lifecycle" not in ast.unparse(node).lower(), \
                    f"{os.path.basename(path)} keeps a module-level lifecycle: {ast.unparse(node)}"
    # gui.py keeps exactly one module-level holder, and it holds a tuple-or-None, not a lifecycle
    gui_globals = [ast.unparse(n) for n in _tree(_GUI).body
                   if isinstance(n, (ast.Assign, ast.AnnAssign))
                   and "lifecycle" in ast.unparse(n).lower()]
    assert gui_globals == ["_active_render_slot: 'tuple[str, RenderLifecycle] | None' = None"], \
        gui_globals


def test_the_only_thing_any_seam_does_with_the_token_is_read_it():
    """No seam may advance the lifecycle's state. That belongs to the two mutex-owning wrappers."""
    for path, name in SEAMS:
        if path is _GUI:
            continue                        # the wrappers' own file; sections 9-10 pin it precisely
        body = _body(path, name)
        for forbidden in ("mark_running", "mark_cancelling", "mark_terminal", "request_cancel",
                          "_cancel_event", "_state_lock", "_transition"):
            assert forbidden not in body, f"{name} mutates the lifecycle via {forbidden}"
        assert ("lifecycle.raise_if_cancelled()" in body
                or "lifecycle.cancel_requested()" in body
                or "lifecycle=lifecycle" in body), f"{name} accepts a lifecycle but never uses it"


# ===========================================================================
# 2. `lifecycle is None` is today's behaviour, byte-for-byte
# ===========================================================================


@pytest.mark.parametrize("path,name,call", [
    (_FFMPEG, "_run_media_command",
     "subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)"),
    (_MIXDOWN, "_run",
     "subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)"),
])
def test_the_no_lifecycle_path_is_the_original_blocking_call(path, name, call):
    """**The whole back-compatibility claim rests on this one branch.**

    Each media runner opens with `if lifecycle is None: return <the original call>`. It must be the
    *first* statement after the docstring, so nothing — no Popen, no poll loop, no clock — can run
    ahead of it on the path every existing caller takes. And the returned call must be the original
    blocking `subprocess.run(..., timeout=timeout)`, not a reconstruction of it.
    """
    node = _func(path, name)
    statements = [s for s in node.body
                  if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                          and isinstance(s.value.value, str))]
    guard = statements[0]
    assert isinstance(guard, ast.If), ast.unparse(guard)
    assert ast.unparse(guard.test) == "lifecycle is None"
    assert not guard.orelse, "the None path must return, not fall into an else"
    assert len(guard.body) == 1 and isinstance(guard.body[0], ast.Return)
    assert ast.unparse(guard.body[0].value) == call, ast.unparse(guard.body[0].value)


def test_the_cancellable_path_is_the_only_thing_that_ever_creates_a_popen():
    """Two small independent runners, one `Popen` each, each owned by its own call frame.

    They are deliberately *not* one shared helper: a process handle belongs to the frame that
    created it, and sharing one runner across the two runtime modules would mean a child created on
    behalf of the mixdown was reaped by code owned by the renderer. Each module's own copy is cheap
    and keeps ownership local — so neither may start importing the other's.
    """
    for path, name in ((_FFMPEG, "_run_media_command"), (_MIXDOWN, "_run")):
        body = _body(path, name)
        assert body.count("subprocess.Popen(") == 1, f"{name} creates more than one child"

    # Executable source only: each module's prose names the other's runner precisely to say it does
    # not import it, and docstring text must never read as an import.
    assert "_run_media_command" not in _executable_source(_MIXDOWN)
    imports = {a.name for n in ast.walk(_tree(_FFMPEG)) if isinstance(n, ast.Import)
               for a in n.names}
    imports |= {n.module for n in ast.walk(_tree(_FFMPEG))
                if isinstance(n, ast.ImportFrom) and n.module}
    assert "audio_mixdown" not in imports, "ffmpeg_processing imported the mixdown's runner"


# ===========================================================================
# 3. The media runners, against REAL child processes
# ===========================================================================
#
# These are the cases a mocked `subprocess` cannot carry. The property is not "RenderCancelled was
# raised" — that is trivially fakeable — it is "the OS child was reaped BEFORE the exception left
# the function". So the real `subprocess` module is wrapped in a shim that records every `Popen` it
# creates, and the test interrogates those handles after the raise.


class _RecordingSubprocess:
    """The real `subprocess`, recording every `Popen` and whether `run` was used instead."""

    TimeoutExpired = subprocess.TimeoutExpired
    CompletedProcess = subprocess.CompletedProcess
    PIPE = subprocess.PIPE

    def __init__(self):
        self.children: list[subprocess.Popen] = []
        self.run_calls = 0

    def Popen(self, *args, **kwargs):           # noqa: N802 - mirrors the stdlib name
        child = subprocess.Popen(*args, **kwargs)
        self.children.append(child)
        return child

    def run(self, *args, **kwargs):
        self.run_calls += 1
        return subprocess.run(*args, **kwargs)


def _sleep_cmd(seconds: float) -> list[str]:
    """A long-lived, portable stand-in for `ffmpeg.exe`. Needs no FFmpeg and no media."""
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _load_ffmpeg_runner(subprocess_module):
    """The REAL `ffmpeg_processing._run_media_command`, over a recording `subprocess`."""
    tree = _tree(_FFMPEG)
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name == "_run_media_command")
              or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) in
                  ("_CANCEL_POLL_SECONDS", "_TERMINATE_GRACE_SECONDS"))]
    assert len(wanted) == 3, [ast.unparse(n) for n in wanted]
    namespace = {"subprocess": subprocess_module, "time": time, "os": os,
                 "RenderCancelled": RenderCancelled, "List": list}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _FFMPEG, "exec"), namespace)
    return namespace["_run_media_command"]


def _load_mixdown_runner(subprocess_module):
    """The REAL `audio_mixdown._run`, over a recording `subprocess`."""
    tree = _tree(_MIXDOWN)
    wanted = [n for n in tree.body
              if (isinstance(n, ast.FunctionDef) and n.name == "_run")
              or (isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) in
                  ("_CANCEL_POLL_SECONDS", "_TERMINATE_GRACE_SECONDS"))]
    assert len(wanted) == 3, [ast.unparse(n) for n in wanted]
    namespace = {"subprocess": subprocess_module, "time": time,
                 "RenderCancelled": RenderCancelled}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _MIXDOWN, "exec"), namespace)
    return namespace["_run"]


RUNNERS = (("ffmpeg_processing._run_media_command", _load_ffmpeg_runner),
           ("audio_mixdown._run", _load_mixdown_runner))


@pytest.mark.parametrize("label,loader", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_no_lifecycle_uses_subprocess_run_and_creates_no_popen(label, loader):
    """Measured, not inferred: the existing path does not even take the new code path."""
    shim = _RecordingSubprocess()
    runner = loader(shim)
    result = runner(_sleep_cmd(0.01), 30)
    assert result.returncode == 0
    assert shim.run_calls == 1
    assert shim.children == [], "the None path created a Popen"


@pytest.mark.parametrize("label,loader", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_an_uncancelled_lifecycle_still_returns_a_normal_completed_process(label, loader):
    """A lifecycle that is merely *present* must not change the outcome of a successful command."""
    shim = _RecordingSubprocess()
    runner = loader(shim)
    result = runner(_sleep_cmd(0.05), 30, lifecycle=_live())
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert len(shim.children) == 1
    assert shim.children[0].poll() is not None, "a successful command must still be reaped"


@pytest.mark.parametrize("label,loader", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_a_cancelled_command_terminates_and_reaps_the_child_before_raising(label, loader):
    """**The load-bearing one.** Quiescence is proven *before* the exception propagates.

    A real child that would otherwise run for 60 seconds is stopped in well under the command's own
    timeout, and — the part that matters — `Popen.poll()` is already non-`None` the moment the
    exception reaches this frame. If the runner raised while the child was still alive, an orphaned
    `ffmpeg.exe` would keep writing into the process-global processing directory that the *next*
    render is about to clear.
    """
    shim = _RecordingSubprocess()
    runner = loader(shim)
    life = _cancelled()

    started = time.perf_counter()
    with pytest.raises(RenderCancelled):
        runner(_sleep_cmd(60), 600, lifecycle=life)
    elapsed = time.perf_counter() - started

    assert len(shim.children) == 1, "exactly one child per command"
    child = shim.children[0]
    assert child.poll() is not None, \
        "RenderCancelled escaped while the FFmpeg-class child was still alive"
    assert elapsed < 30, f"cancellation took {elapsed:.1f}s; the poll loop is not polling"


@pytest.mark.parametrize("label,loader", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_cancellation_requested_mid_command_is_observed_without_waiting_for_the_child(label, loader):
    """Cancel arrives from another thread while the child is running, as it does in production."""
    shim = _RecordingSubprocess()
    runner = loader(shim)
    life = _live()

    def cancel_soon():
        time.sleep(0.4)
        life.request_cancel()

    canceller = threading.Thread(target=cancel_soon, daemon=True)
    canceller.start()
    try:
        with pytest.raises(RenderCancelled):
            runner(_sleep_cmd(60), 600, lifecycle=life)
        assert shim.children[0].poll() is not None
    finally:
        canceller.join(timeout=5)


@pytest.mark.parametrize("label,loader", RUNNERS, ids=[r[0] for r in RUNNERS])
def test_a_real_timeout_is_still_a_timeout_and_never_reported_as_cancellation(label, loader):
    """A timeout and a Stop are different facts. Conflating them would mislabel a stuck encode.

    Note the timeout here (1s) is far longer than the 0.15s poll interval, which is exactly the trap
    this case guards: the runner must measure the *command's* timeout independently of its own poll
    period, or every cancellable command would appear to time out on its first poll.
    """
    shim = _RecordingSubprocess()
    runner = loader(shim)
    with pytest.raises(subprocess.TimeoutExpired):
        runner(_sleep_cmd(60), 1, lifecycle=_live())
    assert shim.children[0].poll() is not None, "a timed-out child must also be reaped"


# ===========================================================================
# 4. BOUNDARY_ONLY_CANCEL — Stage 5 / Qwen is never reached and never hard-killed
# ===========================================================================


@pytest.mark.parametrize("path", [_ANALYSIS, _QWEN_WORKER, _QWEN_PROGRESS],
                         ids=["video_analysis", "stage5_qwen_scene_worker", "qwen_progress"])
def test_the_token_never_reaches_stage_five(path):
    """**A contract, not an omission.** An in-flight Qwen call is never hard-killed.

    Stage 5 can hold GPU minutes in one call. Terminating it mid-inference would abandon a model
    process and throw away work already paid for, and a cancellable Stage 5 would also need a
    cancellation token inside the persisted-semantics layer, where nothing creative or
    session-scoped is allowed to exist. So the token simply does not go there: cancellation becomes
    effective at the next boundary *after* Stage 5 returns normally.
    """
    source = _source(path)
    for forbidden in ("lifecycle", "RenderCancelled", "render_worker", "request_cancel",
                      "raise_if_cancelled", "cancel_requested"):
        assert forbidden not in source, f"{os.path.basename(path)} references {forbidden}"


def test_the_post_stage_five_boundary_sits_outside_that_stage_s_broad_except():
    """**The subtlest placement in the whole feature**, so it is pinned by position.

    Stage 5 is wrapped in a broad `except Exception` whose recovery path continues into fallback
    sampling. A `raise_if_cancelled()` placed *inside* that `try` would therefore be caught by it:
    the user's Stop would be reinterpreted as "video analysis failed", the run would continue, and a
    video would be produced from fallback samples after the user asked for nothing at all.
    """
    node = _func(_AUTO, "analyze_beats_auto")

    stage5_try = None
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Try) and "should_analyze_video" not in ast.unparse(stmt):
            rendered = ast.unparse(stmt)
            if "analyze_video_sources" in rendered or "video_analysis" in rendered:
                if any(isinstance(h.type, ast.Name) and h.type.id == "Exception"
                       for h in stmt.handlers):
                    stage5_try = stmt
                    break
    assert stage5_try is not None, "the Stage-5 try/except was not found"

    checks = [n for n in ast.walk(node) if isinstance(n, ast.Call)
              and ast.unparse(n) == "lifecycle.raise_if_cancelled()"]
    assert len(checks) == 4, f"expected 4 safe boundaries, found {len(checks)}"

    after = [c for c in checks if c.lineno > stage5_try.end_lineno]
    assert after, "no boundary check exists after the Stage-5 try/except"
    inside = [c for c in checks
              if stage5_try.lineno <= c.lineno <= stage5_try.end_lineno]
    assert inside == [], "a boundary check sits INSIDE the Stage-5 try/except"

    # ...and before Stage 6 ever sees a plan: nothing is returned between the check and the end
    body = _body(_AUTO, "analyze_beats_auto")
    assert body.index("energy_profile = {") > body.rindex("lifecycle.raise_if_cancelled()")


def test_stage_five_is_also_bracketed_from_before():
    """A cancellation that already landed must stop Stage 5 from *starting*, not merely from
    finishing — that is the difference between a few milliseconds and several GPU minutes."""
    node = _func(_AUTO, "analyze_beats_auto")
    checks = sorted(n.lineno for n in ast.walk(node) if isinstance(n, ast.Call)
                    and ast.unparse(n) == "lifecycle.raise_if_cancelled()")
    gate = next(n for n in ast.walk(node) if isinstance(n, ast.If)
                and ast.unparse(n.test) == "should_analyze_video")
    assert any(line < gate.lineno for line in checks)
    assert all(not (gate.lineno < line <= gate.end_lineno) for line in checks), \
        "a check inside the Stage-5 branch would be observed mid-analysis"


# ===========================================================================
# 5. `RenderCancelled` survives every broad catch
# ===========================================================================

#: Pinned by count per file, so a NEW broad `except Exception` on the render path cannot arrive
#: without someone revisiting this number — which is the only moment anybody would notice it needs
#: an `except RenderCancelled` of its own.
EXPECTED_HANDLER_COUNT = {
    _FFMPEG: 2,        # convert_to_prores_proxy, extract_clip_segment_ffmpeg_detailed
    _MIXDOWN: 1,       # build_mixed_master
    _PROCESSOR: 4,     # create_clip_parallel, the clip loop, and both assembly call sites
    # [R2] 3: _process_video_impl's translation boundary, process_video.worker's defensive one, and
    # the new post-commit ProRes preview handler -- the only one in the codebase that may swallow.
    _GUI: 3,
    _AUTO: 0,          # boundary checks only; it catches nothing
}


@pytest.mark.parametrize("path,expected", sorted(EXPECTED_HANDLER_COUNT.items()),
                         ids=[os.path.basename(p) for p in sorted(EXPECTED_HANDLER_COUNT)])
def test_the_number_of_cancellation_handlers_is_pinned_per_module(path, expected):
    handlers = [h for h in ast.walk(_tree(path)) if isinstance(h, ast.ExceptHandler)
                and isinstance(h.type, ast.Name) and h.type.id == "RenderCancelled"]
    assert len(handlers) == expected, \
        f"{os.path.basename(path)} has {len(handlers)} `except RenderCancelled`, expected {expected}"


@pytest.mark.parametrize("path", sorted(EXPECTED_HANDLER_COUNT),
                         ids=[os.path.basename(p) for p in sorted(EXPECTED_HANDLER_COUNT)])
def test_a_cancellation_handler_always_precedes_the_broad_one_it_protects(path):
    """Handler order is the entire mechanism. `except Exception` first would make it dead code."""
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Try):
            continue
        kinds = [getattr(h.type, "id", None) if isinstance(h.type, ast.Name) else
                 ("BARE" if h.type is None else "OTHER") for h in node.handlers]
        if "RenderCancelled" not in kinds:
            continue
        typed = kinds.index("RenderCancelled")
        for broad in ("Exception", "BaseException", "BARE"):
            if broad in kinds:
                assert typed < kinds.index(broad), \
                    f"`except {broad}` precedes `except RenderCancelled` at line {node.lineno}"


def _post_commit_preview_handlers() -> set[int]:
    """Line numbers of the `except RenderCancelled` handlers inside the post-promotion preview block.

    Located **structurally**, by walking the `if is_prores:` block that follows the durable
    promotion inside `_process_video_impl` — not by matching a token a future author could sprinkle
    anywhere to opt out of the no-swallow rule below.
    """
    impl = _func(_GUI, "_process_video_impl")
    preview = next(n for n in ast.walk(impl) if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "is_prores"
                   and "preview_cmd" in ast.unparse(n))
    # it must genuinely sit after the SUCCESS commit point, or this exception would be a loophole
    commit = next(n for n in ast.walk(impl) if isinstance(n, ast.Assign)
                  and ast.unparse(n) ==
                  "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS")
    assert commit.lineno < preview.lineno, \
        "the preview block no longer follows the SUCCESS commit point"
    return {h.lineno for h in ast.walk(preview) if isinstance(h, ast.ExceptHandler)
            and isinstance(h.type, ast.Name) and h.type.id == "RenderCancelled"}


def test_a_cancellation_handler_never_swallows_and_never_substitutes():
    """Every handler re-raises the same thing, stores it to re-raise, or translates it truthfully.

    **One exception, added by R2 and deliberately scoped to a single structural location**: the
    ProRes preview, which runs *after* the durable promotion already committed `SUCCESS`. There,
    swallowing is the correct behaviour and propagating would be the bug — the render is finished,
    the user owns the `.mov`, and cancellation is only stopping post-commit convenience work. Letting
    `RenderCancelled` escape from there would turn a completed render into a cancelled one.

    The exemption is located by walking the `is_prores` preview block (and asserting it really does
    follow the commit point), **not** by a token match — otherwise it would be an opt-out anybody
    could apply to a handler that genuinely must not swallow.
    """
    exempt = _post_commit_preview_handlers()
    assert len(exempt) == 1, f"expected exactly one post-commit preview handler, found {exempt}"

    for path in sorted(EXPECTED_HANDLER_COUNT):
        for handler in ast.walk(_tree(path)):
            if not (isinstance(handler, ast.ExceptHandler)
                    and isinstance(handler.type, ast.Name)
                    and handler.type.id == "RenderCancelled"):
                continue
            rendered = ast.unparse(handler)
            bare_reraise = any(isinstance(s, ast.Raise) and s.exc is None
                               for s in ast.walk(handler))
            # the one deferred case: the clip loop stores the exception and re-raises it after the
            # executor is quiescent (section 6), which is strictly stronger than re-raising here
            deferred = "cancelled_exc = exc" in rendered
            # the two GUI translation boundaries return a truthful typed status instead
            translated = "RenderOutcomeKind.CANCELLED" in rendered
            # the one post-commit case, identified by position in gui.py
            post_commit = path is _GUI and handler.lineno in exempt
            assert bare_reraise or deferred or translated or post_commit, \
                f"{os.path.basename(path)} line {handler.lineno} swallows a cancellation:\n{rendered}"
            for substitution in ("raise Exception(", "raise ValueError(", "raise RuntimeError(",
                                 "raise AudioMixError("):
                assert substitution not in rendered, \
                    f"{os.path.basename(path)} line {handler.lineno} re-types a cancellation"

            if post_commit:
                # and the exempt one must do exactly one thing: fall back to the durable output
                assert rendered.strip().endswith("preview_path = output_path"), rendered
                assert "RENDER_OUTCOME_KEY" not in rendered, \
                    "the post-commit handler must not touch the committed outcome"


def test_a_cancellation_is_never_narrated_as_a_failure_event():
    """A `ProgressEvent` must describe what happened. "Final assembly failed" did not happen.

    Both `concatenate_videos_ffmpeg` call sites sit inside a broad handler that emits
    `error(6, "Final assembly failed: …")` and then re-raises. The type always survived that (the
    re-raise is bare), but the *event* was a lie — so each site gained an `except RenderCancelled:
    raise` ahead of it that emits nothing at all.
    """
    node = _func(_PROCESSOR, "create_music_video")
    assembly_handlers = [
        h for h in ast.walk(node)
        if isinstance(h, ast.Try)
        and "concatenate_videos_ffmpeg(" in ast.unparse(h.body)
        and any(isinstance(x.type, ast.Name) and x.type.id == "Exception" for x in h.handlers)
    ]
    assert len(assembly_handlers) == 2, "expected the ProRes and the ordinary assembly call sites"
    for site in assembly_handlers:
        kinds = [getattr(h.type, "id", None) for h in site.handlers]
        assert kinds[0] == "RenderCancelled", kinds
        cancel_branch = ast.unparse(site.handlers[0])
        assert "emit" not in cancel_branch and "error(" not in cancel_branch, \
            f"the cancellation branch narrates something: {cancel_branch}"
        assert any(isinstance(s, ast.Raise) and s.exc is None
                   for s in ast.walk(site.handlers[0]))


def test_the_mixdown_cleans_up_its_partial_master_without_re_typing_the_cancellation():
    """Structural half of section 8: the cleanup runs, and the type still escapes."""
    handler = next(h for h in ast.walk(_func(_MIXDOWN, "build_mixed_master"))
                   if isinstance(h, ast.ExceptHandler)
                   and isinstance(h.type, ast.Name) and h.type.id == "RenderCancelled")
    rendered = ast.unparse(handler)
    assert "discard_master(output_path)" in rendered
    assert any(isinstance(s, ast.Raise) and s.exc is None for s in ast.walk(handler))


# ===========================================================================
# 6. The clip executor: quiescence before propagation
# ===========================================================================


def test_the_clip_loop_shuts_the_executor_down_explicitly_and_breaks():
    """Structural, over the real `create_music_video`."""
    node = _func(_PROCESSOR, "create_music_video")
    handler = next(h for h in ast.walk(node)
                   if isinstance(h, ast.ExceptHandler)
                   and isinstance(h.type, ast.Name) and h.type.id == "RenderCancelled"
                   and "shutdown" in ast.unparse(h))
    rendered = ast.unparse(handler)
    assert "executor.shutdown(wait=True, cancel_futures=True)" in rendered, rendered
    assert "cancelled_exc = exc" in rendered
    assert any(isinstance(s, ast.Break) for s in ast.walk(handler)), \
        "the loop must stop consuming futures, not continue"
    assert not any(isinstance(s, ast.Continue) for s in ast.walk(handler)), \
        "`continue` would keep draining futures after a cancellation"
    assert not any(isinstance(s, ast.Raise) for s in ast.walk(handler)), \
        "re-raising HERE would propagate from inside the `with` block"


def test_the_re_raise_happens_after_the_with_block_has_exited():
    """**The mutation-proof requirement**, pinned by position rather than by reading the comment.

    Moving `raise cancelled_exc` inside the `with` would still be *mostly* safe — the context
    manager's own `__exit__` calls `shutdown(wait=True)` — but it would reorder the two facts this
    code is arranged to keep in order: the executor is provably quiescent, and only then does
    cancellation leave the function. Pinning the line position is what makes that ordering
    non-negotiable instead of incidental.
    """
    node = _func(_PROCESSOR, "create_music_video")
    pool = next(w for w in ast.walk(node) if isinstance(w, ast.With)
                and "ThreadPoolExecutor(" in ast.unparse(w.items[0].context_expr))
    raises = [r for r in ast.walk(node) if isinstance(r, ast.Raise)
              and r.exc is not None and ast.unparse(r.exc) == "cancelled_exc"]
    assert len(raises) == 1, "exactly one re-raise of the stored cancellation"
    assert raises[0].lineno > pool.end_lineno, \
        "the stored cancellation is re-raised from INSIDE the executor's `with` block"

    # the guard around it reads the stored exception, never a boolean flag that could drift
    guard = next(n for n in ast.walk(node) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "cancelled_exc is not None")
    assert guard.lineno > pool.end_lineno
    assert not guard.orelse


def test_a_cancelled_pool_starts_no_further_work_and_leaves_nothing_running():
    """Behavioural, on a faithful stand-in — and honest about which half it proves.

    `create_music_video` cannot be executed here: it closes over ~40 runtime globals and a real
    processing directory. So this reconstructs the loop's exact shape over a real
    `ThreadPoolExecutor` and real worker threads, and the structural cases above pin that the
    shipped code has that shape.

    Two properties, and only one of them is `wait=True`'s:

    * **`cancel_futures=True` is independently load-bearing.** Without it, every future already
      submitted would still run — on a 1216-cut render that is the entire extraction continuing
      after the user pressed Stop. Asserted by measuring how many workers ever started.
    * **Nothing is still running when the exception leaves.** `wait=True` plus the re-raise sitting
      after the `with` block gives this; the context manager's implicit shutdown would too, which is
      why the explicit call is belt-and-braces rather than the sole mechanism.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    life = _live()
    started, finished = [], []
    gate = threading.Lock()

    def worker(index):
        with gate:
            started.append(index)
        if index == 0:
            # the first worker observes the cancellation, exactly as a real clip worker does
            life.request_cancel()
            raise RenderCancelled("clip cancelled")
        while not life.cancel_requested():
            time.sleep(0.005)
        with gate:
            finished.append(index)
        return index

    total = 64
    cancelled_exc = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(worker, i): i for i in range(total)}
        for future in as_completed(futures):
            try:
                future.result()
            except RenderCancelled as exc:
                executor.shutdown(wait=True, cancel_futures=True)
                cancelled_exc = exc
                break
            except Exception:                   # noqa: BLE001 - the generic clip-failure path
                continue
    if cancelled_exc is not None:
        raised = cancelled_exc
    else:                                        # pragma: no cover - the harness proved nothing
        raise AssertionError("the cancellation never reached the consumer loop")

    assert isinstance(raised, RenderCancelled)
    assert len(started) < total, \
        "cancel_futures=True did not stop pending futures from beginning work"
    # everything that did start has already come to rest before the exception left the block
    assert sorted(finished) == sorted(i for i in started if i != 0), \
        "a worker was still running when cancellation propagated"


# ===========================================================================
# 7. `create_clip_parallel`, executed for real
# ===========================================================================


def _load_clip_worker(extract):
    """The REAL `video_processor.create_clip_parallel`, over stubbed media calls."""
    node = _func(_PROCESSOR, "create_clip_parallel")
    calls: dict[str, list] = {"duration": [], "extract": []}

    def get_video_duration(path):
        calls["duration"].append(path)
        return 30.0

    def extract_clip_segment_ffmpeg_detailed(**kwargs):
        calls["extract"].append(kwargs)
        return extract(**kwargs)

    namespace = {
        "time": time, "os": os, "random": __import__("random"),
        "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="cafe")),
        "get_video_duration": get_video_duration,
        "extract_clip_segment_ffmpeg_detailed": extract_clip_segment_ffmpeg_detailed,
        "RenderCancelled": RenderCancelled,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _PROCESSOR, "exec"), namespace)
    return namespace["create_clip_parallel"], calls


_OK = (True, "")
_ARGS8 = (0, "a.mp4", 1.0, (1920, 1080), False, "none", "C:/tmp", 30.0)


def test_the_eight_and_nine_element_forms_are_unchanged():
    """Every pre-R1A caller packs 8 or 9 elements. Neither may acquire new behaviour."""
    worker, calls = _load_clip_worker(lambda **_k: _OK)
    index, path, _size, display, reason, elapsed = worker(_ARGS8)
    assert (index, reason) == (0, None) and path and display
    assert calls["duration"] == ["a.mp4"]
    assert calls["extract"][0]["lifecycle"] is None, "the absent lifecycle must pass through as None"

    worker, calls = _load_clip_worker(lambda **_k: _OK)
    planned = {"video_file": "b.mp4", "source_duration": 2.0, "start_time": 3.0}
    assert worker(_ARGS8 + (planned,))[4] is None
    assert calls["duration"] == ["b.mp4"]


def test_an_already_cancelled_worker_does_no_expensive_work_at_all():
    """A pending future that `cancel_futures` did not catch in time must still not probe or encode.

    The check is the FIRST statement in the `try`, before the duration probe — which on a large
    library is an ffprobe subprocess per clip.
    """
    worker, calls = _load_clip_worker(lambda **_k: _OK)
    with pytest.raises(RenderCancelled):
        worker(_ARGS8 + (None, _cancelled()))
    assert calls["duration"] == [], "a cancelled worker probed a video's duration"
    assert calls["extract"] == [], "a cancelled worker started an extraction"


def test_a_cancellation_from_the_extraction_escapes_instead_of_becoming_a_failed_clip():
    """**The failure mode this guard exists for.** `(False, reason)` is a *clip* failure.

    Stage 6 counts those, warns about them and ultimately refuses to concatenate an incomplete
    timeline. A cancellation reported that way would surface as "1 of 1216 clips failed" instead of
    "cancelled" — and worse, the consumer loop would `continue` and keep extracting.
    """
    def cancel(**_kwargs):
        raise RenderCancelled("ffmpeg command cancelled")

    worker, _calls = _load_clip_worker(cancel)
    with pytest.raises(RenderCancelled):
        worker(_ARGS8 + (None, _live()))


def test_an_ordinary_extraction_error_still_becomes_a_failed_clip_tuple():
    """The generic path is untouched: only `RenderCancelled` is special."""
    def boom(**_kwargs):
        raise ValueError("codec exploded")

    worker, _calls = _load_clip_worker(boom)
    index, path, _size, display, reason, _elapsed = worker(_ARGS8 + (None, _live()))
    assert (index, path, display) == (0, None, None)
    assert "codec exploded" in reason


def test_the_shared_lifecycle_reaches_the_extraction_call():
    """Every clip worker in one batch gets the SAME instance; none of them builds its own."""
    worker, calls = _load_clip_worker(lambda **_k: _OK)
    life = _live()
    worker(_ARGS8 + (None, life))
    assert calls["extract"][0]["lifecycle"] is life
    assert "RenderLifecycle(" not in _body(_PROCESSOR, "create_clip_parallel")


# ===========================================================================
# 8. A cancelled mixdown discards its partial master, behaviourally
# ===========================================================================


def test_a_cancelled_mixdown_removes_the_partial_wav_and_raises_a_typed_cancellation(tmp_path):
    """Executed against the REAL `build_mixed_master`, with a cancelling `render_mixed_master`.

    Two things must both be true, and C3-R0's code only had the first for `AudioMixError`: the
    partial WAV must not be left in `session_dir` (the next render's duration probe would read a
    truncated master), and the caller must still see a `RenderCancelled` rather than an
    `AudioMixError` — because `_process_video_impl` classifies an `AudioMixError` as
    `CANDIDATE_LOCAL` and would report a user's Stop as an Audio Layers failure.
    """
    from beatsync_fork import audio_mix as fork_audio_mix
    from test_audio_mixdown import load_mixdown

    mixdown = load_mixdown()
    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)

    discarded: list[str] = []
    partial = mixdown.master_path_for(session_dir)
    with open(partial, "wb") as handle:
        handle.write(b"RIFF-partial")

    # The exec globals the extracted bodies actually resolve names against. `load_mixdown` returns a
    # `SimpleNamespace`, whose `__dict__` is a *copy* — patching that would silently do nothing.
    namespace = mixdown.build_mixed_master.__globals__

    def cancelling_render(*_args, **_kwargs):
        raise RenderCancelled("audio mixdown command cancelled")

    def recording_discard(path):
        discarded.append(path)
        if os.path.exists(path):
            os.remove(path)

    namespace["render_mixed_master"] = cancelling_render
    namespace["discard_master"] = recording_discard

    with pytest.raises(RenderCancelled):
        namespace["build_mixed_master"](
            music_path="music.wav", music_duration=200.0,
            beat_times=[i * 0.5 for i in range(400)],
            sections=(fork_audio_mix.MusicSection(0.0, 200.0, "verse"),),
            voices=(fork_audio_mix.VoiceInput(0, "v.wav", 3.0),),
            config=fork_audio_mix.AudioMixConfig(), session_dir=session_dir,
            lifecycle=_live())

    assert discarded == [partial], f"the partial master was not discarded: {discarded}"
    assert not os.path.exists(partial)
    assert not [f for f in os.listdir(session_dir) if f.endswith(".wav")]


def test_an_already_cancelled_mixdown_never_plans_anything(tmp_path):
    """The boundary check is before the planner, so a cancelled render pays nothing for it."""
    from beatsync_fork import audio_mix as fork_audio_mix
    from test_audio_mixdown import load_mixdown

    mixdown = load_mixdown()
    with pytest.raises(RenderCancelled):
        mixdown.build_mixed_master(
            music_path="music.wav", music_duration=200.0,
            beat_times=[i * 0.5 for i in range(400)],
            sections=(fork_audio_mix.MusicSection(0.0, 200.0, "verse"),),
            voices=(fork_audio_mix.VoiceInput(0, "v.wav", 3.0),),
            config=fork_audio_mix.AudioMixConfig(),
            session_dir=str(tmp_path), lifecycle=_cancelled())
    assert [f for f in os.listdir(tmp_path) if f.endswith(".wav")] == [], \
        "a cancelled render that never planned anything still wrote a master"


# ===========================================================================
# 9. The capacity-one active-render slot and the Cancel handler, for real
# ===========================================================================


def _load_slot():
    """The REAL slot helpers and Cancel handler from `gui.py`.

    `_RENDER_LOCK` is deliberately **absent** from the namespace. That is not an oversight: it is
    the proof. If any of these four functions so much as named the render mutex, this would raise
    `NameError` instead of passing — which is a stronger statement than a token scan, because it
    survives a rename of the lock.
    """
    wanted = [n for n in _tree(_GUI).body
              if isinstance(n, ast.FunctionDef) and n.name in (
                  "_install_active_render", "_clear_active_render",
                  "_request_cancel_if_matching", "_on_cancel_render_click")]
    assert len(wanted) == 4, [n.name for n in wanted]
    namespace = {
        "threading": threading,
        "_ACTIVE_RENDER_SLOT_LOCK": threading.Lock(),
        "_active_render_slot": None,
        "RenderLifecycle": RenderLifecycle,
        "STATUS_CANCEL_REQUESTED": "⏹️ requested",
        "STATUS_CANCEL_NOTHING_ACTIVE": "nothing active",
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _GUI, "exec"), namespace)
    return types.SimpleNamespace(**{k: namespace[k] for k in (
        "_install_active_render", "_clear_active_render", "_request_cancel_if_matching",
        "_on_cancel_render_click")}, ns=namespace)


def test_a_matching_id_cancels_the_installed_lifecycle():
    slot = _load_slot()
    life = _live("inv-1")
    slot._install_active_render(life)

    assert slot._request_cancel_if_matching("inv-1") is True
    assert life.cancel_requested() is True
    assert slot._on_cancel_render_click("inv-1") == "⏹️ requested"


def test_an_empty_or_stale_id_is_a_silent_safe_no_op():
    """A reloaded tab, a finished render, or a batch that has moved on — never an error."""
    slot = _load_slot()
    life = _live("inv-1")
    slot._install_active_render(life)

    for bad in ("", "inv-other", "INV-1"):
        assert slot._request_cancel_if_matching(bad) is False
        assert life.cancel_requested() is False, f"{bad!r} cancelled the wrong render"
    assert slot._on_cancel_render_click("") == "nothing active"
    assert slot._on_cancel_render_click("inv-other") == "nothing active"


def test_cancelling_with_nothing_installed_does_nothing():
    slot = _load_slot()
    assert slot._request_cancel_if_matching("inv-1") is False
    assert slot._on_cancel_render_click("inv-1") == "nothing active"


def test_a_stale_finalizer_cannot_clear_a_newer_render_s_slot():
    """**The race this design exists for.**

    An abandoned render's `finally` can run long after a *new* render has installed itself. An
    unconditional clear there would unregister the live render, and from that moment the Cancel
    button would silently do nothing for the rest of that render.
    """
    slot = _load_slot()
    old, new = _live("inv-old"), _live("inv-new")
    slot._install_active_render(old)
    slot._install_active_render(new)             # a newer render takes the slot

    slot._clear_active_render("inv-old")         # the stale finalizer, arriving late
    assert slot.ns["_active_render_slot"] is not None, "the newer render was unregistered"
    assert slot._request_cancel_if_matching("inv-new") is True
    assert new.cancel_requested() is True
    assert old.cancel_requested() is False


def test_clearing_the_matching_id_empties_the_slot_exactly_once():
    slot = _load_slot()
    life = _live("inv-1")
    slot._install_active_render(life)
    slot._clear_active_render("inv-1")
    assert slot.ns["_active_render_slot"] is None
    slot._clear_active_render("inv-1")           # idempotent
    assert slot.ns["_active_render_slot"] is None
    assert slot._request_cancel_if_matching("inv-1") is False


def test_the_slot_holds_only_an_id_and_a_lifecycle():
    """Never a `Popen`, never a thread handle, never a history of past invocations."""
    slot = _load_slot()
    life = _live("inv-1")
    slot._install_active_render(life)
    held = slot.ns["_active_render_slot"]
    assert isinstance(held, tuple) and len(held) == 2
    assert held[0] == "inv-1" and held[1] is life
    # capacity exactly one: installing a second replaces rather than accumulates
    slot._install_active_render(_live("inv-2"))
    assert slot.ns["_active_render_slot"][0] == "inv-2"


def test_many_concurrent_cancel_clicks_signal_once_and_never_raise():
    """Cancel is a user-facing button; double-clicks and a stale tab are ordinary, not exceptional."""
    slot = _load_slot()
    life = _live("inv-1")
    slot._install_active_render(life)
    errors: list[BaseException] = []
    go = threading.Event()

    def click(invocation_id):
        go.wait(5)
        try:
            for _ in range(100):
                slot._on_cancel_render_click(invocation_id)
        except BaseException as exc:             # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=click, args=("inv-1",)) for _ in range(3)]
    threads += [threading.Thread(target=click, args=("inv-stale",)) for _ in range(3)]
    for thread in threads:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "the Cancel handler deadlocked"
    assert errors == [], errors
    assert life.cancel_requested() is True


def test_the_cancel_path_touches_nothing_but_the_slot():
    """Structural companion to `_load_slot`'s absent-`_RENDER_LOCK` proof."""
    for name in ("_on_cancel_render_click", "_request_cancel_if_matching",
                 "_install_active_render", "_clear_active_render"):
        body = _body(_GUI, name)
        for forbidden in ("_RENDER_LOCK", "RENDER_CONCURRENCY_ID", "RENDER_BUSY_MESSAGE",
                          "session_state", "source_state", "prep_state", "gr.", "subprocess",
                          "get_processing_dir", "LAST_OUTPUT_PATH_KEY", "RENDER_OUTCOME_KEY",
                          "os.", "open(", "Popen", "terminate", "kill"):
            assert forbidden not in body, f"{name} references {forbidden}"


def test_the_slot_lock_is_not_the_render_mutex():
    """Two different locks on purpose: `_RENDER_LOCK` is held for the whole render.

    Reading or clearing the slot under that lock would mean a Cancel click had to wait for the
    render it is trying to interrupt — the deadlock-shaped uselessness the separate lane also avoids
    at the Gradio layer.
    """
    source = _source(_GUI)
    assert "_ACTIVE_RENDER_SLOT_LOCK = threading.Lock()" in source
    assert "_RENDER_LOCK = threading.Lock()" in source
    assert source.count("_ACTIVE_RENDER_SLOT_LOCK = ") == 1


# ===========================================================================
# 10. One lifecycle per top-level render event
# ===========================================================================

WRAPPERS = ("process_video_guarded", "render_selected_variants_guarded")


def test_exactly_two_lifecycles_are_ever_constructed_and_both_by_a_mutex_owning_wrapper():
    """One per top-level render EVENT — and the batch's one spans BOTH candidates.

    A lifecycle per candidate would make the Cancel button a per-candidate control the UI never
    offered: stopping candidate 1 would leave candidate 2 to start under a fresh, uncancelled token.
    """
    source = _source(_GUI)
    assert source.count("RenderLifecycle(invocation_id=") == 2, \
        "a third lifecycle construction appeared"
    for wrapper in WRAPPERS:
        body = _body(_GUI, wrapper)
        assert body.count("RenderLifecycle(invocation_id=str(uuid.uuid4()))") == 1, wrapper
        assert "_install_active_render(lifecycle)" in body
        assert "_clear_active_render(lifecycle.invocation_id)" in body

    # nothing below the wrappers builds one
    for name in ("_process_video_guarded_unlocked", "process_video", "_process_video_impl"):
        assert "RenderLifecycle(" not in _body(_GUI, name), name
    for path in (_PROCESSOR, _FFMPEG, _MIXDOWN, _AUTO):
        assert "RenderLifecycle(" not in _source(path), os.path.basename(path)


def test_the_batch_threads_one_shared_instance_into_both_candidates():
    body = _body(_GUI, "render_selected_variants_guarded")
    assert body.count("lifecycle=lifecycle") == 1, \
        "the single core call inside the loop is what both candidates go through"
    loop = next(n for n in ast.walk(_func(_GUI, "render_selected_variants_guarded"))
                if isinstance(n, ast.For))
    rendered = ast.unparse(loop)
    assert "RenderLifecycle(" not in rendered, "a per-candidate lifecycle is being built"
    assert "lifecycle=lifecycle" in rendered


def test_the_candidate_boundary_is_checked_before_each_candidate_starts():
    """A Cancel landing in the gap between two candidates must stop the batch *there*.

    Otherwise it would only be honoured by the next internal safe boundary — i.e. somewhere inside
    the candidate that had already begun, after it had spent real analysis time.
    """
    loop = next(n for n in ast.walk(_func(_GUI, "render_selected_variants_guarded"))
                if isinstance(n, ast.For))
    guard = next(n for n in loop.body if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "lifecycle.cancel_requested()")
    assert loop.body.index(guard) == 0, "the boundary check must be the FIRST thing each iteration"
    assert "stopped = True" in ast.unparse(guard)
    assert any(isinstance(s, ast.Break) for s in ast.walk(guard))


def test_terminal_marking_happens_only_in_the_two_wrappers():
    """**Newly load-bearing in R1A.** `process_video.worker()` runs once per candidate.

    `RenderLifecycle._transition` silently no-ops once terminal, by design. So a `mark_terminal`
    inside `worker()` would freeze the batch's SHARED lifecycle on candidate 1's outcome and
    candidate 2's real result would never reach the lifecycle at all.
    """
    markers = [n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
               and ast.unparse(n.func).endswith("mark_terminal")]
    assert markers, "nothing marks the lifecycle terminal"

    owners = {}
    for func in ast.walk(_tree(_GUI)):
        if isinstance(func, ast.FunctionDef):
            for call in ast.walk(func):
                if isinstance(call, ast.Call) and ast.unparse(call.func).endswith(
                        ("mark_terminal", "mark_running", "mark_cancelling")):
                    owners.setdefault(func.name, set()).add(ast.unparse(call.func).split(".")[-1])

    assert set(owners) == set(WRAPPERS), f"lifecycle state is advanced in {sorted(owners)}"
    for wrapper in WRAPPERS:
        assert owners[wrapper] == {"mark_running", "mark_terminal"}, owners[wrapper]

    # and specifically not in the per-candidate worker
    assert "mark_terminal" not in _body(_GUI, "process_video")
    assert "mark_terminal" not in _body(_GUI, "_process_video_impl")


def test_the_terminal_state_is_derived_from_the_typed_outcome_not_from_prose():
    for wrapper in WRAPPERS:
        body = _body(_GUI, wrapper)
        assert "RenderOutcomeKind.CANCELLED" in body
        assert "RenderLifecycleState.CANCELLED" in body
        assert "RenderLifecycleState.FINISHED" in body
        assert "RenderLifecycleState.FAILED" in body
        for inferred in ("in last_status", "status.startswith", "'❌' in", "'✅' in"):
            assert inferred not in body, f"{wrapper} derives its terminal state from {inferred}"


def test_the_batch_states_its_own_terminal_cause_to_the_formatter():
    """**R2.** The boundary case is expressible nowhere else, so `gui.py` must state it.

    `RenderBatchOutcome.__post_init__` can derive a cancellation from a cancelled *candidate*, but the
    batch-boundary case has none: candidate 1 succeeded and candidate 2 was never attempted. If this
    argument went missing, the model's own tests would still pass and the UI would quietly go back to
    reporting ``stopped on candidate 1`` for a candidate that had just succeeded — so the call site is
    pinned here, with the authority it reads.
    """
    body = _body(_GUI, "render_selected_variants_guarded")
    assert ("batch_outcome_kind = RenderOutcomeKind.CANCELLED "
            "if lifecycle.cancel_requested() else None") in body, body
    assert "outcome_kind=batch_outcome_kind" in body, \
        "the batch outcome is built without stating its terminal cause"

    construction = next(n for n in ast.walk(_func(_GUI, "render_selected_variants_guarded"))
                        if isinstance(n, ast.Call)
                        and ast.unparse(n.func) == "fork_render_batch.RenderBatchOutcome")
    kwargs = {kw.arg for kw in construction.keywords}
    assert kwargs == {"requested_count", "outcomes", "stopped_on_failure", "outcome_kind"}, kwargs

    # derived from the cancellation Event, never from status prose or from `stopped`
    for inferred in ("outcome_kind=stopped", "in last_status", "'Cancelled' in"):
        assert inferred not in body, f"the batch cause is inferred from {inferred}"


def test_a_cancelled_batch_event_outranks_its_last_candidate_s_own_outcome():
    """The *event* was cancelled, even if the candidate that happened to be running succeeded.

    Checked by reading `cancel_requested()` first in the batch's `finally`, before the per-candidate
    SUCCESS branch is considered.
    """
    node = _func(_GUI, "render_selected_variants_guarded")
    closing = next(t for t in ast.walk(node)
                   if isinstance(t, ast.Try) and "_RENDER_LOCK.release()" in
                   ast.unparse(t.finalbody))
    chain = next(n for n in closing.finalbody if isinstance(n, ast.If))
    assert ast.unparse(chain.test) == "lifecycle.cancel_requested()"
    assert "RenderLifecycleState.CANCELLED" in ast.unparse(chain.body)


def test_abandonment_is_not_an_explicit_cancel():
    """**Frozen invariant.** No finalizer anywhere may request a cancellation.

    A dropped Gradio event, a closed stream or an exception while draining must keep waiting for the
    render in flight exactly as it did before R1A. A `request_cancel()` in a `finally` would turn
    every abandoned stream into a Stop the user never pressed — and `request_cancel` has exactly one
    caller in the whole module, the Cancel handler's slot lookup.
    """
    source = _source(_GUI)
    calls = [n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("request_cancel")]
    assert len(calls) == 1, f"{len(calls)} request_cancel call sites in gui.py"
    assert "slot[1].request_cancel()" in source

    for func in ast.walk(_tree(_GUI)):
        if isinstance(func, ast.FunctionDef):
            for node in ast.walk(func):
                if isinstance(node, ast.Try) and node.finalbody:
                    rendered = ast.unparse(node.finalbody)
                    assert "request_cancel" not in rendered, \
                        f"{func.name} cancels in a finally (line {node.lineno})"
                    for violent in ("terminate(", "kill(", "thread.join(timeout"):
                        assert violent not in rendered, f"{func.name} finalizer: {violent}"


# ===========================================================================
# 11. Only a plain string crosses into `gr.State`; Cancel owns its own lane
# ===========================================================================


def test_only_a_plain_string_enters_gr_state():
    """`gr.State` deep-copies and may serialize its value. A live `Event` must never go in.

    Checked over EVERY `gr.State(...)` in the module rather than just the new one, because the thing
    that would break this is a future author reaching for the convenient object.
    """
    states = [n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
              and ast.unparse(n.func) == "gr.State"]
    assert states, "no gr.State found; the walker is broken"
    for call in states:
        rendered = ast.unparse(call)
        for live in ("RenderLifecycle", "threading", "Event(", "Lock(", "Popen",
                     "lifecycle", "_cancel_event"):
            assert live not in rendered, f"a live object enters gr.State: {rendered}"

    assert "render_invocation_state = gr.State('')" in _source(_GUI), \
        "the invocation state must be declared as an empty string"


def test_the_published_invocation_id_is_always_a_string_or_empty():
    """Both wrappers publish `lifecycle.invocation_id` and clear it to `''`, never to `None`.

    `None` would reach the Cancel handler as a falsy non-string. `_request_cancel_if_matching`
    happens to handle it, but the state is declared as a string and keeping it one is cheaper than
    depending on that — so the last element of every yield in these three functions must be either
    `lifecycle.invocation_id`, the derived `invocation_id` name, or a string literal.
    """
    allowed = {"lifecycle.invocation_id", "invocation_id", "''"}
    for name in WRAPPERS + ("_process_video_guarded_unlocked",):
        node = _func(_GUI, name)
        yields = [y for y in ast.walk(node) if isinstance(y, ast.Yield) and y.value is not None]
        assert yields, f"{name} yields nothing"
        for yielded in yields:
            value = yielded.value
            if not isinstance(value, (ast.Tuple,)):
                continue                      # `yield from` and non-tuple yields carry no id
            last = ast.unparse(value.elts[-1])
            assert last in allowed, f"{name} yields {last!r} as its invocation id"

    core = _body(_GUI, "_process_video_guarded_unlocked")
    assert "invocation_id = lifecycle.invocation_id if lifecycle is not None else ''" in core


def test_cancel_has_its_own_concurrency_lane_and_never_gradio_cancels():
    """Two separate mistakes, both avoided, and each would make Cancel worse than absent.

    Sharing `RENDER_CONCURRENCY_ID` (limit 1) would queue the Cancel event behind the very render it
    exists to signal — it would run after that render finished. And Gradio's built-in `cancels=`
    kills the *event*, returning its slot while the daemon render worker keeps going: precisely the
    orphaned-worker hazard C3-R0 bounded itself to two renders to avoid.
    """
    source = _source(_GUI)
    assert "CANCEL_CONCURRENCY_ID = 'beatsync-cancel'" in source
    assert "RENDER_CONCURRENCY_ID = 'beatsync-render'" in source

    click = next(n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "click"
                 and getattr(n.func.value, "id", None) == "cancel_render_btn")
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in click.keywords}
    assert kwargs["fn"] == "_on_cancel_render_click"
    assert kwargs["concurrency_id"] == "CANCEL_CONCURRENCY_ID"
    assert kwargs["inputs"] == "[render_invocation_state]", "Cancel's only input is the id"
    assert kwargs["outputs"] == "[status_output]", "Cancel writes only the status line"

    # no Gradio event anywhere uses `cancels=`
    for call in ast.walk(_tree(_GUI)):
        if isinstance(call, ast.Call):
            assert "cancels" not in [kw.arg for kw in call.keywords], \
                f"Gradio's cancels= appeared: {ast.unparse(call.func)}"


def test_the_cancel_button_is_never_gated_by_the_source_confirmation():
    """It must stay interactive precisely while a render runs — that is its only purpose.

    It is also absent from `source_outputs` and `prep_outputs`: a render already in flight has
    nothing to do with whether a NEW one could start, and disabling it would make it unreachable at
    the only moment it matters.
    """
    source = _source(_GUI)
    assert "cancel_render_btn = gr.Button(LABEL_CANCEL_RENDER, variant='stop', size='sm')" in source
    for collection in ("source_outputs", "prep_outputs"):
        block = source[source.index(f"{collection} = ["):]
        block = block[:block.index("]")]
        assert "cancel_render_btn" not in block, f"{collection} writes the Cancel button"
        assert "render_invocation_state" not in block


def test_the_render_events_gained_the_invocation_state_as_their_last_output():
    """Positional alignment: Gradio matches a yielded tuple to `outputs` by index."""
    for button, expected_last in (("process_btn", "render_invocation_state"),
                                  ("render_selected_variants_btn", "render_invocation_state")):
        click = next(n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "click"
                     and getattr(n.func.value, "id", None) == button)
        outputs = next(kw.value for kw in click.keywords if kw.arg == "outputs")
        names = [n.id for n in outputs.elts if isinstance(n, ast.Name)]
        assert names[-1] == expected_last, f"{button} outputs end with {names[-1]}"
        assert names.count(expected_last) == 1


# ===========================================================================
# 12. Durable promotion is the ONE success commit point
# ===========================================================================


def test_success_is_written_exactly_once_and_only_after_the_promotion():
    impl = _body(_GUI, "_process_video_impl")
    assigns = [ast.unparse(n) for n in ast.walk(_func(_GUI, "_process_video_impl"))
               if isinstance(n, ast.Assign)
               and ast.unparse(n.targets[0]) == "session_state[RENDER_OUTCOME_KEY]"]
    assert assigns.count("session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS") == 1
    assert assigns[0] == "session_state[RENDER_OUTCOME_KEY] = None", \
        "the key must be cleared before anything else"
    assert impl.index("session_state[LAST_OUTPUT_PATH_KEY] = output_path") < \
        impl.index("session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS")
    assert impl.index("promotion_error = _promote_output_no_replace(result_path, output_path)") < \
        impl.index("session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS")


def test_nothing_after_the_commit_point_can_downgrade_success():
    """**Frozen invariant.** The ProRes preview is a convenience; the `.mov` is the deliverable.

    A cancellation arriving during preview generation skips *starting* the preview subprocess and
    must not touch the outcome: the user owns a finished video, and telling them it was cancelled
    would be false.
    """
    node = _func(_GUI, "_process_video_impl")
    preview = next(n for n in ast.walk(node) if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "is_prores"
                   and "preview_cmd" in ast.unparse(n))
    rendered = ast.unparse(preview)
    assert "RENDER_OUTCOME_KEY" not in rendered, \
        "the preview block touches the outcome key"
    assert "raise_if_cancelled" not in rendered, \
        "raising here would discard a render that already succeeded"

    # [R2] three explicit branches, and all three must exist. R1A had only the first two, which is
    # why an already-running preview could not be stopped at all.
    assert "if lifecycle is None:" in rendered, \
        "the no-lifecycle path must stay the original blocking call"
    assert "subprocess.run(preview_cmd, capture_output=True, text=True, timeout=180)" in rendered, \
        "the lifecycle-free preview call must be byte-identical to today's"
    assert "elif lifecycle.cancel_requested():" in rendered, \
        "an already-cancelled render must not START a preview child"
    assert "run_cancellable_media_command(preview_cmd, 180, lifecycle=lifecycle)" in rendered, \
        "an in-flight preview must go through the cancellable runner"
    # the cancellation lands here and goes no further
    assert "except RenderCancelled:" in rendered
    assert rendered.rstrip().endswith("preview_path = output_path")
    # and a genuine timeout stays a timeout on both paths -- never caught, never relabelled
    assert "except subprocess.TimeoutExpired" not in rendered, \
        "a stuck encode is a different fact from a user pressing Stop"

    impl = _body(_GUI, "_process_video_impl")
    assert impl.index("session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS") < \
        impl.index("if is_prores:")


def test_a_cancellation_during_the_prores_preview_still_reports_success(tmp_path):
    """The frozen invariant, measured against the REAL `_process_video_impl`.

    The render is cancelled from *inside* the stubbed `create_music_video`, which is the only way to
    reach the preview step with a cancellation pending — every boundary before it would have raised.
    What must come out: the `.mov` promoted into `output/`, the durable path recorded, `SUCCESS` on
    the outcome key, and no preview subprocess started.
    """
    from test_gui_guard_seam import (PROMOTION_HELPER, _gui_tree, _impl_namespace,
                                     _no_replace_rename)
    from conftest import write_file

    calls: list[str] = []
    namespace = _impl_namespace(tmp_path, calls, rename=_no_replace_rename,
                               dest_appears_during_render=False)

    life = _live("inv-prores")
    out_dir = str(tmp_path / "output")

    real_create = namespace["create_music_video"]

    def create_then_cancel(*args, output_file, **kwargs):
        result = real_create(*args, output_file=output_file, **kwargs)
        # the user presses Stop while the render is finishing; the durable promotion below has not
        # happened yet, so this is the latest possible moment that still reaches the preview step
        life.request_cancel()
        return result

    previews: list[list[str]] = []

    class _Subprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(cmd, **_kwargs):
            previews.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

    namespace["create_music_video"] = create_then_cancel
    namespace["subprocess"] = _Subprocess
    namespace["RenderCancelled"] = RenderCancelled
    namespace["RenderOutcomeKind"] = rw.RenderOutcomeKind
    namespace["RENDER_OUTCOME_KEY"] = "render_outcome_kind"

    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)

    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")
    state = {"session_dir": session_dir}

    preview_path, status, state = namespace["_process_video_impl"](
        audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
        processing_mode="prores_proxy", custom_fps=30.0, creative=None,
        session_state=state, lifecycle=life)

    produced = sorted(os.listdir(out_dir))
    assert len(produced) == 1 and produced[0].endswith(".mov"), produced
    durable = os.path.join(out_dir, produced[0])
    assert state["last_output_path"] == durable
    assert state["render_outcome_kind"] is rw.RenderOutcomeKind.SUCCESS, \
        "a cancellation after the durable promotion downgraded a finished render"
    assert status.startswith("✅"), status
    assert previews == [], "the preview subprocess was started despite a pending cancellation"
    assert preview_path == durable, \
        "with no preview generated, the durable output is what gets displayed"
    assert life.cancel_requested() is True, "the cancellation really was pending"


#: Filled in by the in-flight preview test below so the final report can quote a real measurement.
PREVIEW_CANCEL_LATENCY: dict = {}


def test_an_in_flight_prores_preview_child_is_cancelled_while_the_render_stays_success(tmp_path):
    """**The R2 defect-B case, with a REAL child process.**

    R1A only handled "cancellation already pending, so do not START the preview". It did nothing for
    the case that actually matters: the preview child is **already running**, the user clicks Cancel,
    and `subprocess.run(..., timeout=180)` stays blocked until FFmpeg exits — up to three minutes of
    a UI claiming the render was cancelled while an FFmpeg child kept working.

    This exercises the real `_process_video_impl` preview branch over the real cancellable runner
    from `ffmpeg_processing` (poll → terminate → grace → kill → reap) and a real long-lived child.
    Only the argv is swapped for a portable `python -c "sleep"` stand-in, because the bare suite has
    no FFmpeg — and the genuine preview argv is captured and asserted, so the swap cannot hide a
    command that was never built.

    `subprocess.run` is wired to a stub that fails loudly: reaching it would mean the blocking path
    was taken, which is the mutation this test exists to catch.
    """
    from test_gui_guard_seam import (PROMOTION_HELPER, _gui_tree, _impl_namespace,
                                     _no_replace_rename)
    from conftest import write_file

    calls: list[str] = []
    namespace = _impl_namespace(tmp_path, calls, rename=_no_replace_rename,
                               dest_appears_during_render=False)
    life = _live("inv-preview-inflight")
    out_dir = str(tmp_path / "output")

    # The REAL cancellable runner, over a recording `subprocess` so the child can be interrogated.
    shim = _RecordingSubprocess()
    real_runner = _load_ffmpeg_runner(shim)
    preview_argv: list[list[str]] = []

    def preview_runner(cmd, timeout, lifecycle=None):
        preview_argv.append(list(cmd))
        # real runner, real lifecycle, real poll/terminate/reap — only the argv is portable
        return real_runner(_sleep_cmd(60), timeout, lifecycle=lifecycle)

    class _BlockingPathTaken:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*_args, **_kwargs):                  # pragma: no cover - reaching this is the bug
            raise AssertionError(
                "the preview took the blocking subprocess.run path while a lifecycle was active")

    namespace["run_cancellable_media_command"] = preview_runner
    namespace["subprocess"] = _BlockingPathTaken
    namespace["RenderCancelled"] = RenderCancelled
    namespace["RenderOutcomeKind"] = rw.RenderOutcomeKind
    namespace["RENDER_OUTCOME_KEY"] = "render_outcome_kind"
    namespace["FFMPEG_PATH"] = r"C:\fake\ffmpeg.exe"
    namespace["NVENC_AVAILABLE"] = False

    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)

    observed: dict = {}

    def cancel_once_the_child_is_alive():
        """Wait for a REAL live child, prove it is running, then press Cancel."""
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            if shim.children and shim.children[0].poll() is None:
                observed["alive"] = True
                observed["pid"] = shim.children[0].pid
                observed["requested_at"] = time.perf_counter()
                life.request_cancel()
                return
            time.sleep(0.01)
        observed["alive"] = False                    # pragma: no cover - harness failure

    canceller = threading.Thread(target=cancel_once_the_child_is_alive, daemon=True)
    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")

    canceller.start()
    try:
        preview_path, status, state = namespace["_process_video_impl"](
            audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
            processing_mode="prores_proxy", custom_fps=30.0, creative=None,
            session_state={"session_dir": session_dir}, lifecycle=life)
    finally:
        life.request_cancel()                        # never leave a 60s child behind
        canceller.join(timeout=25)

    reaped_at = time.perf_counter()

    # 1-3. the preview child really started, really was alive, and Cancel arrived while it was
    assert observed.get("alive") is True, "the preview child was never observed alive"
    assert len(shim.children) == 1, f"expected one preview child, got {len(shim.children)}"
    assert life.cancel_requested() is True

    # 4-5. it exited and was reaped
    assert shim.children[0].poll() is not None, \
        "the preview FFmpeg child was still alive after _process_video_impl returned"
    latency = reaped_at - observed["requested_at"]
    PREVIEW_CANCEL_LATENCY["seconds"] = latency
    PREVIEW_CANCEL_LATENCY["pid"] = observed["pid"]
    assert latency < 20, f"cancel-to-quiescent took {latency:.2f}s"

    # the genuine preview command was built and handed to the real seam
    assert len(preview_argv) == 1
    assert preview_argv[0][0] == r"C:\fake\ffmpeg.exe"
    assert "-pix_fmt" in preview_argv[0] and "yuv420p" in preview_argv[0]

    # 6. the partial preview is not selected — the durable .mov is
    produced = sorted(os.listdir(out_dir))
    assert len(produced) == 1 and produced[0].endswith(".mov"), produced
    durable = os.path.join(out_dir, produced[0])
    assert preview_path == durable, \
        "a partial/absent preview was handed back instead of the durable output"
    assert not preview_path.endswith("_preview.mp4")

    # 7. the durable output survived untouched
    assert os.path.exists(durable)
    assert open(durable, "rb").read() == b"NEW"
    assert state["last_output_path"] == durable

    # 8. the committed outcome is still SUCCESS
    assert state["render_outcome_kind"] is rw.RenderOutcomeKind.SUCCESS, \
        "cancelling post-commit convenience work downgraded a finished render"

    # 9. no RenderCancelled escaped as the terminal result
    assert status.startswith("✅"), status
    assert "Cancelled" not in status
    assert calls == ["analyze_beats_auto", "create_music_video"]


def test_a_cancellation_before_the_promotion_reports_cancelled_and_promotes_nothing(tmp_path):
    """The other side of the same boundary, against the REAL `_process_video_impl`.

    `create_music_video` raises `RenderCancelled`, so nothing is promoted, `output/` stays empty,
    the durable path stays blank and the typed cause is `CANCELLED` — never `UNKNOWN_FATAL`, and
    never the generic `❌ Error:` string, which is what the handler ordering buys.
    """
    from test_gui_guard_seam import (PROMOTION_HELPER, _gui_tree, _impl_namespace,
                                     _no_replace_rename)
    from conftest import write_file

    calls: list[str] = []
    namespace = _impl_namespace(tmp_path, calls, rename=_no_replace_rename,
                               dest_appears_during_render=False)

    def cancel_mid_render(*_args, **_kwargs):
        calls.append("create_music_video")
        raise RenderCancelled("clip extraction cancelled")

    namespace["create_music_video"] = cancel_mid_render
    namespace["RenderCancelled"] = RenderCancelled
    namespace["RenderOutcomeKind"] = rw.RenderOutcomeKind
    namespace["RENDER_OUTCOME_KEY"] = "render_outcome_kind"
    namespace["STATUS_RENDER_CANCELLED"] = "⏹️ Cancelled. Nothing was rendered."

    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)

    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")

    preview_path, status, state = namespace["_process_video_impl"](
        audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
        processing_mode="cpu", custom_fps=30.0, creative=None,
        session_state={"session_dir": session_dir}, lifecycle=_live())

    assert calls == ["analyze_beats_auto", "create_music_video"]
    assert preview_path is None
    assert state["render_outcome_kind"] is rw.RenderOutcomeKind.CANCELLED
    assert state["last_output_path"] == "", "a cancelled render recorded a durable output"
    assert os.listdir(str(tmp_path / "output")) == [], "a cancelled render promoted something"
    assert "Cancelled" in status
    assert "❌ Error" not in status, "the cancellation fell through to the generic handler"


# ===========================================================================
# 13. Nothing about identity moved
# ===========================================================================


def test_no_cache_schema_or_version_constant_moved():
    """Cancellation is session-scoped runtime state. It must reach no persisted identity."""
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in _source(_ANALYSIS)
    assert ('ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"'
            in _source(_ANALYSIS))
    assert 'L2_CACHE_VERSION = "l2_stage3_v1"' in _source(_src("beatsync_fork", "stage_cache.py"))

    from beatsync_fork import render_batch as fork_render_batch
    assert fork_render_batch.RENDER_SELECTION_SIZE == 2, "R1A must not have unlocked R1B"


def test_cancellation_reaches_no_cache_identity_or_creative_state():
    for module in ("stage_cache.py", "creative.py", "creative_recipe.py", "variant_lab.py",
                   "variant_batch.py", "presets.py", "variation.py", "freestyle.py",
                   "audio_mix.py", "smart_mix.py", "director.py"):
        source = _source(_src("beatsync_fork", module))
        for forbidden in ("lifecycle", "RenderCancelled", "render_worker", "cancel_requested"):
            assert forbidden not in source, f"{module} references {forbidden}"
