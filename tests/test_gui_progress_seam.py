"""The gui.py progress seam, without importing the runtime.

``src/gui.py`` cannot be imported on a bare interpreter (its prologue runs
``logger.setup_environment()``, which imports librosa and mutates ``PATH``, then it imports gradio,
cupy and cv2). So, exactly as with the source-confirmation seam, this file reconstructs the
generator's event-draining loop over the real :class:`ProgressView` and asserts ``gui.py``'s own wiring
separately by reading its source.

The property under test: **stage identity and counts reach the status UI without any regex over prose.**
Before Phase 2A the generator recovered the stage number with
``re.search(r"Stage (\\d+) is processing", message)``.
"""

from __future__ import annotations

import ast
import os
import queue
import threading

from beatsync_fork.progress import (
    ProgressEvent,
    StageCounter,
    end,
    error,
    progress,
    start,
    state,
    warning,
)
from beatsync_fork.progress_view import ProgressView

try:  # the enum `gui.process_video` references when formatting stage events
    from beatsync_fork.progress import EventKind as _EventKind
except ImportError:  # pragma: no cover - name differs across versions
    _EventKind = type('EventKind', (), {})

_GUI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "gui.py"
)


def _drain(items, session_state=None):
    """Faithful stand-in for gui.process_video's event-draining generator loop.

    Mirrors the real loop: structured events fold into a ProgressView; a legacy string is only shown
    while no structured event has arrived yet.
    """
    session_state = {} if session_state is None else session_state
    sink: queue.Queue = queue.Queue()
    for item in items:
        sink.put(item)
    sink.put(None)

    view = ProgressView()
    yields: list[tuple] = []
    last = "Starting…"
    yields.append((None, last, session_state))

    while True:
        item = sink.get()
        if item is None:
            break
        if isinstance(item, ProgressEvent):
            view.apply(item)
            rendered = view.render()
        else:
            if view.active_stage() is not None:
                continue
            rendered = str(item)
        if rendered != last:
            last = rendered
            yields.append((None, rendered, session_state))
    return yields, view


# ---------------------------------------------------------------------------
# Structured events reach the status text
# ---------------------------------------------------------------------------


def test_status_text_updates_from_structured_events():
    yields, _view = _drain([
        start(5, "Analyzing 758 source video(s)", current=0, total=758, unit="sources"),
        progress(5, 531, 758, "analyzed", rate=1.4, elapsed_seconds=380.0, cache_hits=420,
                 unit="sources"),
    ])
    assert len(yields) >= 3, "each distinct status must produce a yield"
    final = yields[-1][1]
    assert "Stage 5 — Video Analysis" in final
    assert "531 / 758" in final
    assert "70.1%" in final


def test_current_and_total_are_shown_for_rendering():
    yields, _ = _drain([
        start(6, "Rendering 1216 frame-locked cuts", current=0, total=1216, unit="clips"),
        progress(6, 612, 1216, "clips rendered", rate=4.83, elapsed_seconds=127.0, unit="clips"),
    ])
    final = yields[-1][1]
    assert "612 / 1216 (50.3%)" in final
    assert "4.8 clips/s" in final
    assert "elapsed 2m 07s" in final


def test_stage_change_is_visible_without_regex_parsing():
    """No event carries the sentence 'Stage N is processing'; identity is the integer field."""
    events = [
        start(1, "Detecting beat grid"),
        end(1, "259 beats at 152.0 BPM"),
        start(5, "Analyzing 4 source video(s)", current=0, total=4, unit="sources"),
        progress(5, 2, 4, "analyzed", unit="sources"),
    ]
    for event in events:
        assert "is processing" not in event.message

    yields, view = _drain(events)
    assert view.active_stage() == 5
    final = yields[-1][1]
    assert "Stage 5 — Video Analysis" in final
    assert "✓ Stage 1" in final, "the finished stage should be summarised"


def test_warning_is_surfaced_to_the_ui():
    yields, view = _drain([
        start(6, "Rendering 3 frame-locked cuts", current=0, total=3, unit="clips"),
        warning(6, "Clip 2 failed: FFmpeg error"),
    ])
    final = yields[-1][1]
    assert "Notices:" in final
    assert "⚠️ Stage 6: Clip 2 failed" in final
    assert len(view.notices) == 1


def test_error_and_refusal_are_surfaced():
    yields, _ = _drain([
        start(6, "", current=0, total=6, unit="clips"),
        progress(6, 5, 6, "clips rendered", unit="clips"),
        error(6, "1 of 6 clip(s) failed; refusing to concatenate an incomplete timeline."),
    ])
    final = yields[-1][1]
    assert "refusing to concatenate an incomplete timeline" in final
    assert "5 / 6" in final, "the true count must remain visible, not be rounded up"


def test_qwen_state_is_shown_without_a_fabricated_counter():
    yields, _ = _drain([
        start(5, "Analyzing 2 source video(s)", current=0, total=2, unit="sources"),
        state(5, "Qwen semantic tagging started (2 video(s)) — no live per-frame progress until "
                 "Phase 2B"),
    ])
    final = yields[-1][1]
    assert "Qwen semantic tagging started" in final
    assert "0 / 2" in final, "the only counted numbers are the source counter's"


def test_final_result_still_returns_normally():
    yields, _ = _drain([
        start(6, "", current=0, total=2, unit="clips"),
        progress(6, 2, 2, "clips rendered", unit="clips"),
        end(6, "2 clips rendered and assembled", current=2, total=2, total_elapsed_seconds=217.0),
    ])
    final = yields[-1][1]
    assert "Total: 3m 37s" in final
    assert yields[-1][0] is None  # no preview path from a status yield


def test_session_state_is_passed_through_unchanged():
    session = {"local_audio_path": "a.mp3"}
    yields, _ = _drain([start(1, "go")], session_state=session)
    assert all(item[2] is session for item in yields)


# ---------------------------------------------------------------------------
# Legacy string compatibility
# ---------------------------------------------------------------------------


def test_legacy_string_status_still_shows_before_any_event():
    """Headless callers that only pass progress_callback keep working."""
    yields, _ = _drain(["Stage 1 is processing. Please wait."])
    assert yields[-1][1] == "Stage 1 is processing. Please wait."


def test_legacy_string_cannot_overwrite_structured_output():
    yields, _ = _drain([
        start(5, "Analyzing 4 source video(s)", current=0, total=4, unit="sources"),
        progress(5, 3, 4, "analyzed", unit="sources"),
        "Stage 5 is processing. Please wait.",
    ])
    final = yields[-1][1]
    assert "3 / 4" in final
    assert "is processing" not in final


# ---------------------------------------------------------------------------
# Thread-safety of the queue hand-off
# ---------------------------------------------------------------------------


def test_events_from_many_threads_are_all_delivered_in_order_per_thread():
    """Stage 5/6 emit from worker threads; the queue is the only cross-thread channel."""
    sink: queue.Queue = queue.Queue()
    counter = StageCounter(6, 40, min_interval=0.0)

    def work():
        for _ in range(10):
            event = counter.advance(1, "clips rendered", unit="clips")
            if event is not None:
                sink.put(event)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    view = ProgressView()
    seen = 0
    while not sink.empty():
        event = sink.get_nowait()
        view.apply(event)
        seen = max(seen, event.current)
    assert counter.current == 40
    assert seen == 40
    assert "40 / 40" in view.render()


# ---------------------------------------------------------------------------
# gui.py wiring, asserted by reading the source
# ---------------------------------------------------------------------------


def _gui_source() -> str:
    with open(_GUI_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def test_gui_no_longer_regex_parses_stage_identity():
    source = _gui_source()
    assert "Stage (\\d+) is processing" not in source, (
        "gui.py still recovers the stage number by regex; structured events must be primary"
    )


def test_gui_process_video_wires_an_event_callback():
    tree = ast.parse(_gui_source(), filename=_GUI_PATH)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "process_video"
    )
    names = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}

    assert "event_callback" in names, "process_video must define an event_callback"
    assert "ProgressView" in names, "process_video must fold events through ProgressView"
    assert "apply" in attrs and "render" in attrs


def test_gui_impl_forwards_the_event_callback_to_both_halves():
    """analyze_beats_auto (stages 1-5) and create_music_video (stage 6) must both receive it."""
    tree = ast.parse(_gui_source(), filename=_GUI_PATH)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_process_video_impl"
    )
    assert any(a.arg == "event_callback" for a in func.args.args), (
        "_process_video_impl must accept event_callback"
    )

    forwarded = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if any(kw.arg == "event_callback" for kw in node.keywords):
                forwarded.add(node.func.id)
    assert {"analyze_beats_auto", "create_music_video"} <= forwarded, forwarded


def test_gui_does_not_touch_gradio_components_from_the_worker_thread():
    """The worker may only put on the queue; components are updated by the generator."""
    tree = ast.parse(_gui_source(), filename=_GUI_PATH)
    process_video = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "process_video"
    )
    event_cb = next(
        n for n in ast.walk(process_video)
        if isinstance(n, ast.FunctionDef) and n.name == "event_callback"
    )
    calls = {
        n.func.attr for n in ast.walk(event_cb)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert calls == {"put"}, f"event_callback must only enqueue, found {calls}"


# ===========================================================================
# C3-R0: the batch prefixes, it never replaces
# ===========================================================================


def _gui_tree() -> ast.Module:
    with open(_GUI_PATH, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _gui_func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_gui_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in gui.py")


def _gui_body(name: str) -> str:
    node = next(n for n in ast.walk(_gui_tree())
                if isinstance(n, ast.FunctionDef) and n.name == name)
    return "\n".join(
        ast.unparse(stmt) for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    )


def test_the_batch_adds_no_new_progress_protocol():
    """C3-R0 wraps; it does not invent a second progress channel.

    The inner `process_video` stream stays authoritative — same stages, same phases, same
    counters, same `ProgressView`. A batch is two of today's renders in sequence, and the user's
    status panel should read exactly as it always has, with one line of context above it.
    """
    body = _gui_body("render_selected_variants_guarded")
    for forbidden in ("ProgressEvent(", "ProgressView(", "StageCounter(", "emit(",
                      "event_callback", "progress_callback", "stage=", "phase=",
                      "ETA", "eta", "estimated"):
        assert forbidden not in body, f"the batch introduces {forbidden}"


def test_the_batch_prefixes_the_existing_status_text_without_parsing_it():
    body = _gui_body("render_selected_variants_guarded")
    # the ordinal line, then the untouched inner text
    # `ast.unparse` normalises f-strings to single quotes, hence the spelling here.
    assert "f'Rendering candidate {position} / {request.count}'" in body
    assert "f'{prefix}" + chr(92) + "n" + chr(92) + "n{last_status}'" in body
    # nothing reads stage identity back out of prose — the mistake Phase 2A deleted
    for forbidden in ("re.search", "re.match", "Stage (", "\\d+", ".index('Stage",
                      'split("Stage"'):
        assert forbidden not in body, f"the batch parses status text: {forbidden}"


def test_the_batch_never_blanks_a_preview_on_a_status_only_yield():
    """A later candidate's progress must not wipe an earlier candidate's finished video."""
    body = _gui_body("render_selected_variants_guarded")
    assert "gr.skip() if video is None else video" in body, \
        "status-only yields must skip the video output, not clear it"
    # and the closing yield prefers the newest success, falling back to skip
    assert "outcome.latest_successful_preview()" in body
    assert "preview if preview else gr.skip()" in body


def test_the_inner_render_stream_is_unchanged():
    """`process_video` keeps its 3-value contract and the core keeps the 5-value projection."""
    source = open(_GUI_PATH, encoding="utf-8").read()
    assert source.count("def process_video(") == 1
    core = _gui_body("_process_video_guarded_unlocked")
    assert "render_stream = process_video(" in core
    assert "for video, status, state in render_stream:" in core
    assert ("yield (video, status, state, (state or {}).get(AUDIO_LAYERS_REPORT_KEY, ''), "
            "(state or {}).get(SMART_MIX_REPORT_KEY, ''))") in core


# ===========================================================================
# C3-R0 R1: the render mutex protects WORKER lifetime, not the generator frame
# ===========================================================================
#
# R0 shipped a process-global `_RENDER_LOCK` and claimed "two renders can never overlap". That
# held only while a render generator was consumed to exhaustion. `process_video` starts a daemon
# worker and had no `try/finally` around its yields, so abandoning the stream — `gen.close()`, a
# dropped Gradio event, an exception while draining — unwound the frame immediately and left the
# worker running. The wrapper's `finally: _RENDER_LOCK.release()` then handed the lock to a second
# render while `create_music_video` still owned the process-global processing directory.
#
# These are runtime tests with a real `threading.Thread` and a real `generator.close()`, because
# this is a concurrency property: an AST assertion can only show the finalizer is *written*, not
# that closing actually blocks. The structural guards below protect it from being deleted later.

import time as _time


class _RenderHarness:
    """The real extracted `process_video`, with `_process_video_impl` blocked on an Event."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.generator = None

    def _impl(self, **kwargs):
        self.started.set()
        kwargs["event_callback"](ProgressEvent(stage=1, kind="state", message="rendering"))
        # a real render holds here for minutes; the test holds until it says so
        self.release.wait(timeout=30)
        self.finished.set()
        return "preview.mp4", "done", kwargs["session_state"]

    def namespace(self):
        class _Quiet:
            def write(self, *_a, **_k):
                return 0

            def flush(self):
                pass

        class _Logger:
            def __init__(self, *_a, **_k):
                pass

            def __getattr__(self, _name):
                return lambda *_a, **_k: None

        return {
            "threading": threading, "queue": queue, "contextlib": __import__("contextlib"),
            "sys": __import__("sys"), "os": os, "time": _time,
            "ProgressEvent": ProgressEvent, "ProgressView": ProgressView,
            "EventKind": _EventKind, "_fmt_stage_seconds": lambda v: f"{v:.1f}s",
            "QuietConsole": _Quiet, "StageConsoleLogger": _Logger,
            "_process_video_impl": self._impl,
            "VideoFilesInput": object,
            "Iterator": __import__("typing").Iterator,
            "StatusResult": __import__("typing").Tuple,
            "fork_creative": type("X", (), {"CreativeProfile": object}),
            "fork_audio_mix": type("X", (), {"AudioMixConfig": object}),
            "fork_smart_mix": type("X", (), {"SmartMixConfig": object}),
        }

    def extract(self, *names, extra=None):
        tree = _gui_tree()
        wanted = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        assert len(wanted) == len(names), [n.name for n in wanted]
        ns = self.namespace()
        ns.update(extra or {})
        exec(compile(ast.Module(body=wanted, type_ignores=[]), _GUI_PATH, "exec"), ns)
        return ns


def _close_in_thread(generator):
    """Close from another thread so the test can observe whether close() is still blocked."""
    done = threading.Event()

    def run():
        generator.close()
        done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done, thread


def test_closing_process_video_early_waits_for_its_worker():
    """**R1, the load-bearing one.** `process_video.close()` must not return while its worker runs.

    Fails against R0, where the generator had no finalizer and close() returned immediately.
    """
    harness = _RenderHarness()
    ns = harness.extract("process_video")
    gen = ns["process_video"](audio_file="a.wav", video_files=["v.mp4"],
                              output_filename="o.mp4", processing_mode="cpu",
                              custom_fps=None, session_state={})
    try:
        next(gen)                                        # "Starting…"
        assert harness.started.wait(5), "the worker never started"

        closed, closer = _close_in_thread(gen)

        # 1. close() must still be blocked while the worker is blocked
        assert not closed.wait(1.0), "close() returned while the render worker was still alive"
        assert not harness.finished.is_set(), "the worker finished early; the test proved nothing"

        # 2. release the worker; close() must then return
        harness.release.set()
        assert closed.wait(10), "close() never returned after the worker finished"
        assert harness.finished.is_set()
        closer.join(timeout=5)
    finally:
        harness.release.set()


def test_the_render_mutex_is_held_until_the_worker_exits():
    """**R1.** The wrapper's lock must outlive the worker, not the generator frame.

    This is the property the mutex was introduced for: `create_music_video` clears one
    process-global processing directory per render, so a second render starting while the first
    worker lives would delete its in-flight clips.
    """
    harness = _RenderHarness()
    lock = threading.Lock()
    ns = harness.extract(
        "process_video", "_process_video_guarded_unlocked", "process_video_guarded",
        extra={
            "_RENDER_LOCK": lock,
            "RENDER_BUSY_MESSAGE": "busy",
            "gr": type("gr", (), {"skip": staticmethod(lambda: "SKIP")}),
            "AUDIO_LAYERS_REPORT_KEY": "audio_layers_report",
            "SMART_MIX_REPORT_KEY": "smart_mix_report",
            "LAST_OUTPUT_PATH_KEY": "last_output_path",
            "time": _time,
            "resolve_for_render": lambda *_a, **_k: type(
                "D", (), {"allowed": True, "paths": ["v.mp4"], "message": ""})(),
            "live_declaration": lambda *_a, **_k: object(),
            "fork_creative": type("X", (), {
                "CreativeProfile": type("P", (), {
                    "from_widgets": staticmethod(lambda **_k: object())})}),
            "fork_audio_mix": type("X", (), {"AudioMixConfig": lambda **_k: object()}),
            "fork_smart_mix": type("X", (), {"SmartMixConfig": lambda **_k: object()}),
            "GuardedResult": __import__("typing").Tuple,
        })

    gen = ns["process_video_guarded"](
        "a.wav", None, 2.0, 1.0, True, 35, "", [], 50, 50,
        "Local folder", "folder", False, None, "o.mp4", "cpu", None,
        0, 50, 50, 50, 50, 50, 50, {}, object())
    try:
        next(gen)                                        # drives through the gate into the render
        assert harness.started.wait(5), "the worker never started"
        assert lock.locked(), "the wrapper must hold the mutex while rendering"

        closed, closer = _close_in_thread(gen)

        # while the worker is blocked: close() pending, lock still held, a second render refused
        assert not closed.wait(1.0), "close() returned while the render worker was still alive"
        assert lock.locked(), "the mutex became available while the worker was still rendering"
        assert not lock.acquire(blocking=False), "a second render could have started"

        harness.release.set()
        assert closed.wait(10), "close() never returned"
        closer.join(timeout=5)
        assert harness.finished.is_set()
        # only now may the mutex be free
        assert not lock.locked(), "the wrapper must release the mutex once the worker is done"
    finally:
        harness.release.set()


def test_closing_the_batch_mid_candidate_waits_for_that_candidate_worker():
    """**R1.** The same chain one level up: batch -> candidate core -> process_video -> join."""
    harness = _RenderHarness()
    lock = threading.Lock()
    from beatsync_fork import presets as _presets
    from beatsync_fork import render_batch as _render_batch
    from beatsync_fork import variant_batch as _variant_batch
    from beatsync_fork import variant_lab as _variant_lab

    fields = _presets.CREATIVE_CONTROL_FIELDS
    config = _variant_lab.VariantLabConfig(
        master_seed=582913, spread=50, randomized=_variant_lab.default_randomized(),
        ranges=_variant_lab.default_ranges())
    audio_config = _variant_lab.AudioVariantConfig(
        randomized=frozenset(_variant_lab.AUDIO_CONTROL_FIELDS))
    batch = _variant_batch.resolve_batch(_variant_batch.declaration_from(
        config, dict(zip(fields, (50,) * 6)), audio_config,
        {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50}, 5))

    ns = harness.extract(
        "process_video", "_process_video_guarded_unlocked",
        "_render_batch_request_tag", "render_selected_variants_guarded",
        extra={
            "_RENDER_LOCK": lock,
            "RENDER_BUSY_MESSAGE": "busy",
            "gr": type("gr", (), {"skip": staticmethod(lambda: "SKIP"),
                                  "update": staticmethod(lambda **k: k)}),
            "AUDIO_LAYERS_REPORT_KEY": "audio_layers_report",
            "SMART_MIX_REPORT_KEY": "smart_mix_report",
            "LAST_OUTPUT_PATH_KEY": "last_output_path",
            "time": _time, "datetime": __import__("datetime"),
            "fork_render_batch": _render_batch,
            "resolve_for_render": lambda *_a, **_k: type(
                "D", (), {"allowed": True, "paths": ["v.mp4"], "message": ""})(),
            "live_declaration": lambda *_a, **_k: object(),
            "fork_creative": type("X", (), {
                "CreativeProfile": type("P", (), {
                    "from_widgets": staticmethod(lambda **_k: object())})}),
            "fork_audio_mix": type("X", (), {"AudioMixConfig": lambda **_k: object()}),
            "fork_smart_mix": type("X", (), {"SmartMixConfig": lambda **_k: object()}),
            "GuardedResult": __import__("typing").Tuple,
            "Tuple": __import__("typing").Tuple,
        })

    gen = ns["render_selected_variants_guarded"](
        batch, [0, 2], "a.wav", None, 2.0, 1.0, True, "", [],
        "Local folder", "folder", False, None, "o.mp4", "cpu", None, {}, object())
    try:
        next(gen)
        assert harness.started.wait(5), "candidate 1's worker never started"
        assert lock.locked()

        closed, closer = _close_in_thread(gen)
        assert not closed.wait(1.0), "the batch closed while candidate 1 was still rendering"
        assert lock.locked(), "the batch released the mutex mid-candidate"

        harness.release.set()
        assert closed.wait(10), "the batch close never returned"
        closer.join(timeout=5)
        assert not lock.locked(), "the batch must release the mutex once the worker is done"
    finally:
        harness.release.set()


# --- structural guards: a later refactor must not quietly delete the finalizers ---


def test_process_video_owns_a_finalizer_that_joins_its_worker():
    node = _gui_func("process_video")
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "process_video must wrap its yields in try/finally"
    joiner = next((t for t in tries if "thread.join()" in ast.unparse(t.finalbody)), None)
    assert joiner is not None, "the finalizer must join the worker thread"

    # the finalizer must not try to kill or time out the worker — C3-R0 has no cancellation
    final = ast.unparse(joiner.finalbody)
    for forbidden in ("terminate(", "kill(", "timeout=", "cancel", "Event("):
        assert forbidden not in final, f"the finalizer {forbidden}"

    # every yield after thread.start() is inside that try. Compared by line number against the
    # joining Try specifically: the nested `worker()` has a try of its own that appears earlier.
    start_line = next(n.lineno for n in ast.walk(node) if isinstance(n, ast.Call)
                      and ast.unparse(n) == "thread.start()")
    assert start_line < joiner.lineno, "the finalizer must cover everything after thread.start()"
    guarded_yields = len([n for n in ast.walk(joiner) if isinstance(n, ast.Yield)])
    total_yields = len([n for n in ast.walk(node) if isinstance(n, ast.Yield)])
    assert guarded_yields == total_yields, "a yield escapes the worker-lifetime finalizer"


def test_both_wrappers_close_their_nested_stream_before_releasing_the_mutex():
    core = _gui_func("_process_video_guarded_unlocked")
    core_body = ast.unparse(core)
    assert "render_stream = process_video(" in core_body, "the core must own its stream"
    core_try = next(t for t in ast.walk(core)
                    if isinstance(t, ast.Try) and "render_stream.close()" in
                    ast.unparse(t.finalbody))
    assert core_try is not None

    batch = _gui_func("render_selected_variants_guarded")
    batch_body = ast.unparse(batch)
    assert "candidate_stream = _process_video_guarded_unlocked(" in batch_body
    inner = next(t for t in ast.walk(batch)
                 if isinstance(t, ast.Try) and "candidate_stream.close()" in
                 ast.unparse(t.finalbody))
    outer = next(t for t in ast.walk(batch)
                 if isinstance(t, ast.Try) and "_RENDER_LOCK.release()" in
                 ast.unparse(t.finalbody))
    # the candidate close must be nested INSIDE the lock-holding try, so it runs first
    assert inner is not outer
    assert any(n is inner for n in ast.walk(outer)), \
        "the candidate stream must be closed before the mutex is released"
