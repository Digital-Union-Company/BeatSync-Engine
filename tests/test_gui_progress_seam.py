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
    assert "for video, status, state in process_video(" in core
    assert ("yield (video, status, state, (state or {}).get(AUDIO_LAYERS_REPORT_KEY, ''), "
            "(state or {}).get(SMART_MIX_REPORT_KEY, ''))") in core
