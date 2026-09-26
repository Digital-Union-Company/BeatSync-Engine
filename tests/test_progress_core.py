"""The structured progress core: event shape, safe emission, counter invariants, formatting."""

from __future__ import annotations

import json
import queue
import threading

import pytest

from beatsync_fork.progress import (
    PROGRESS_SCHEMA,
    STAGE_TITLES,
    EventKind,
    ProgressEvent,
    Stage,
    StageCounter,
    emit,
    end,
    error,
    format_duration,
    format_event,
    format_rate,
    metric,
    progress,
    start,
    state,
    warning,
)


# ---------------------------------------------------------------------------
# Event structure
# ---------------------------------------------------------------------------


def test_event_is_immutable():
    event = start(1, "go")
    with pytest.raises(Exception):
        event.stage = 2  # type: ignore[misc]
    with pytest.raises(Exception):
        event.message = "changed"  # type: ignore[misc]


def test_event_defaults_are_absent_not_zero():
    """An uncounted event must not pretend to be 0/0 — that would render as 0%."""
    event = state(5, "Qwen started")
    assert event.current is None and event.total is None
    assert event.percent is None
    assert event.is_counted() is False


def test_counted_event_exposes_percent():
    event = progress(6, 612, 1216)
    assert event.is_counted() is True
    assert event.percent == pytest.approx(50.328, abs=0.01)


def test_percent_is_none_when_total_is_zero():
    assert progress(6, 0, 0).percent is None


def test_stage_titles_cover_every_stage():
    for stage in Stage:
        assert stage.value in STAGE_TITLES
    assert progress(5, 1, 2).stage_title == "Video Analysis"
    assert ProgressEvent(stage=99, kind=EventKind.STATE).stage_title == "Stage 99"


def test_as_dict_is_json_serialisable():
    event = progress(5, 3, 10, "analyzed", elapsed_seconds=1.5, rate=2.0, cache_hits=2)
    payload = event.as_dict()
    decoded = json.loads(json.dumps(payload))

    assert decoded["stage"] == 5
    assert decoded["kind"] == "progress"
    assert decoded["current"] == 3 and decoded["total"] == 10
    assert decoded["data"]["cache_hits"] == 2
    assert decoded["schema"] == PROGRESS_SCHEMA
    assert decoded["stage_title"] == "Video Analysis"


def test_event_data_is_read_only():
    """`frozen=True` only stops field rebinding; the data mapping must be read-only too.

    Regression: `event.data["injected"] = "yes"` used to succeed, so an event handed to several
    consumers (queue reader, console logger, a later run manifest) could be edited under the others.
    """
    event = progress(5, 1, 2, "x", cache_hits=7)
    with pytest.raises(TypeError):
        event.data["injected"] = "yes"          # type: ignore[index]
    with pytest.raises(TypeError):
        del event.data["cache_hits"]           # type: ignore[attr-defined]
    assert dict(event.data) == {"cache_hits": 7}, "data must still be readable"


def test_event_data_is_isolated_from_the_caller_dict():
    """Mutating the dict passed in must not change the event afterwards."""
    payload = {"cache_hits": 1}
    event = metric(5, "m", **payload)
    payload["cache_hits"] = 999
    assert event.data["cache_hits"] == 1


def test_frozen_event_still_supports_dataclasses_replace():
    """with_elapsed uses dataclasses.replace; the read-only mapping must not break it."""
    from dataclasses import replace as dc_replace

    event = progress(6, 1, 2, "x", unit="clips")
    updated = dc_replace(event, current=2)
    assert updated.current == 2
    assert dict(updated.data) == {"unit": "clips"}
    with pytest.raises(TypeError):
        updated.data["unit"] = "sources"       # type: ignore[index]


def test_as_dict_copies_data_so_mutation_cannot_leak():
    event = metric(1, "m", beats=10)
    payload = event.as_dict()
    payload["data"]["beats"] = 999
    assert event.data["beats"] == 10


def test_with_elapsed_returns_a_new_event():
    original = start(1)
    updated = original.with_elapsed(2.5)
    assert original.elapsed_seconds is None
    assert updated.elapsed_seconds == 2.5
    assert updated is not original


def test_constructors_set_the_right_kind():
    assert start(1).kind is EventKind.START
    assert progress(1, 1, 2).kind is EventKind.PROGRESS
    assert metric(1, "m").kind is EventKind.METRIC
    assert state(1, "s").kind is EventKind.STATE
    assert warning(1, "w").kind is EventKind.WARNING
    assert error(1, "e").kind is EventKind.ERROR
    assert end(1).kind is EventKind.END


# ---------------------------------------------------------------------------
# current / total invariants
# ---------------------------------------------------------------------------


def test_progress_clamps_current_into_range():
    """A double-count upstream must show a stale number, never 761/758."""
    assert progress(5, 999, 758).current == 758
    assert progress(5, -5, 758).current == 0


def test_progress_handles_zero_and_negative_total():
    assert progress(5, 3, 0).total == 0
    assert progress(5, 3, 0).current == 0
    assert progress(5, 3, -7).total == 0


# ---------------------------------------------------------------------------
# Safe emission
# ---------------------------------------------------------------------------


def test_emit_with_no_callback_is_a_noop():
    emit(None, start(1))  # must not raise


def test_emit_delivers_to_callback():
    seen: list[ProgressEvent] = []
    emit(seen.append, start(3, "sections"))
    assert len(seen) == 1 and seen[0].stage == 3


def test_callback_exception_cannot_break_the_pipeline():
    """Observability must never fail a render: hours of analysis outrank a status widget."""
    def exploding(_event):
        raise RuntimeError("status widget is gone")

    emit(exploding, start(6))  # must not raise


def test_emit_does_not_swallow_keyboard_interrupt():
    """BaseException must still propagate, or Ctrl-C would be ignored mid-render."""
    def interrupted(_event):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        emit(interrupted, start(6))


def test_emit_ignores_a_none_event():
    """Makes `emit(cb, counter.advance())` safe: advance() returns None when throttled.

    Regression: without this the consumer received None on every throttled advance, which a real
    pipeline run surfaced immediately (AttributeError on NoneType in the event sink).
    """
    seen: list = []
    emit(seen.append, None)
    assert seen == []


def test_emit_forwards_a_throttled_counter_advance_correctly():
    counter = StageCounter(6, 100, min_interval=999.0)
    seen: list = []
    emit(seen.append, counter.advance(1))   # first advance always emits
    emit(seen.append, counter.advance(1))   # throttled -> None -> ignored
    assert len(seen) == 1
    assert counter.current == 2


def test_emit_into_a_queue_is_the_supported_sink():
    sink: queue.Queue = queue.Queue()
    emit(sink.put, progress(6, 1, 2))
    assert sink.get_nowait().current == 1


def test_emit_is_safe_from_many_threads():
    """Stage 5 and 6 emit from worker threads; nothing here may hold mutable shared state."""
    sink: queue.Queue = queue.Queue()
    threads = [
        threading.Thread(target=lambda n=n: emit(sink.put, progress(6, n, 50)))
        for n in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sink.qsize() == 50


# ---------------------------------------------------------------------------
# StageCounter
# ---------------------------------------------------------------------------


def test_counter_is_monotonic_and_bounded():
    counter = StageCounter(5, 3, min_interval=0.0)
    counter.advance(1)
    counter.set_current(0)          # a regression must be ignored, not applied
    assert counter.current == 1
    counter.advance(99)
    assert counter.current == 3     # never exceeds total


def test_counter_first_advance_always_emits():
    counter = StageCounter(6, 10, min_interval=999.0)
    assert counter.advance(1) is not None


def test_counter_throttles_intermediate_updates():
    counter = StageCounter(6, 100, min_interval=999.0)
    assert counter.advance(1) is not None          # first always emits
    assert counter.advance(1) is None              # throttled
    assert counter.advance(1) is None
    assert counter.current == 3


def test_counter_always_emits_the_final_event():
    """The panel must never come to rest on a stale number."""
    counter = StageCounter(6, 3, min_interval=999.0)
    counter.advance(1)
    assert counter.advance(1) is None
    final = counter.advance(1)
    assert final is not None
    assert final.current == 3 and final.total == 3


def test_counter_force_bypasses_throttling():
    counter = StageCounter(6, 100, min_interval=999.0)
    counter.advance(1)
    assert counter.advance(1, force=True) is not None


def test_counter_snapshot_is_unthrottled():
    """snapshot() reports the true count even when the last advance was throttled away."""
    counter = StageCounter(6, 100, min_interval=999.0)
    counter.advance(1)
    assert counter.advance(1) is None, "second advance should be throttled"
    snap = counter.snapshot("done")
    assert snap.current == 2 and snap.total == 100


def test_counter_seeded_initial_is_respected():
    """Stage 5 seeds the counter with cache hits rather than restarting from zero."""
    counter = StageCounter(5, 758, min_interval=0.0, initial=420)
    assert counter.current == 420
    event = counter.advance(1)
    assert event.current == 421 and event.total == 758


def test_counter_rate_and_elapsed_are_sane():
    counter = StageCounter(6, 10, min_interval=0.0)
    assert counter.rate is None, "no rate before any completion"
    event = counter.advance(1)
    assert event.elapsed_seconds is not None and event.elapsed_seconds >= 0.0
    assert event.rate is None or event.rate > 0


def test_counter_with_zero_total_does_not_divide_by_zero():
    counter = StageCounter(5, 0, min_interval=0.0)
    event = counter.advance(1)
    assert event is not None
    assert event.current == 0 and event.total == 0
    assert event.percent is None


def test_counter_passes_extra_data_through():
    counter = StageCounter(5, 2, min_interval=0.0)
    event = counter.advance(1, "analyzed", cache_hits=7, unit="sources")
    assert event.data["cache_hits"] == 7
    assert event.data["unit"] == "sources"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, ""), (0, "0s"), (45, "45s"), (127, "2m 07s"), (3720, "1h 02m"), (-5, "0s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_rate():
    assert format_rate(None) == ""
    assert format_rate(0) == ""
    assert format_rate(4.83, "clips") == "4.8 clips/s"


def test_format_event_for_counted_progress():
    text = format_event(progress(6, 612, 1216, "clips rendered", elapsed_seconds=127.0, rate=4.83))
    assert "Stage 6 — Rendering" in text
    assert "612 / 1216" in text and "50.3%" in text
    assert "4.8 items/s" in text
    assert "elapsed 2m 07s" in text


def test_format_event_marks_warnings_and_errors():
    assert format_event(warning(5, "fallback")).startswith("⚠️")
    assert format_event(error(6, "refused")).startswith("❌")


def test_format_event_for_a_bare_start():
    assert format_event(start(3)) == "Stage 3 — Sections"
