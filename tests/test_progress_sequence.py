"""Event sequencing and the status-panel view, without running any media workload.

These prove the properties a reviewer actually cares about — stages are ordered, counts only go up,
totals do not wobble, cache hits are represented, failures do not report false completion — by feeding
synthetic event streams shaped exactly like the ones the pipeline emits.
"""

from __future__ import annotations

from beatsync_fork.progress import (
    EventKind,
    ProgressEvent,
    StageCounter,
    end,
    error,
    metric,
    progress,
    start,
    state,
    warning,
)
from beatsync_fork.progress_view import ProgressView


def _normal_run(sources: int = 4, cache_hits: int = 2, clips: int = 6) -> list[ProgressEvent]:
    """A synthetic happy-path stream matching the real emission order of stages 1-6."""
    events: list[ProgressEvent] = []

    events.append(start(1, "Detecting beat grid"))
    events.append(end(1, "259 beats at 152.0 BPM", beats=259, tempo=152.0))

    events.append(start(2, "Reading energy and rhythm features"))
    events.append(end(2, "259 beats profiled"))

    events.append(start(3, "Detecting musical sections"))
    events.append(end(3, "5 sections", sections=5))

    events.append(start(4, "Selecting rhythmic cuts"))
    events.append(end(4, "96 cuts from 259 beats (37.1%)", current=96, total=259))

    events.append(start(5, f"Analyzing {sources} source video(s)", current=0, total=sources,
                       unit="sources"))
    counter = StageCounter(5, sources, min_interval=0.0)
    for _ in range(cache_hits):
        events.append(counter.advance(1, "cached", cache_hits=cache_hits, unit="sources"))
    events.append(metric(5, f"{cache_hits} cached, {sources - cache_hits} to analyze, 2 worker(s)",
                         cache_hits=cache_hits, workers=2))
    for _ in range(sources - cache_hits):
        events.append(counter.advance(1, "analyzed", cache_hits=cache_hits, unit="sources"))
    events.append(end(5, "804 visual moments", current=sources, total=sources,
                      cache_hits=cache_hits, unit="sources"))

    events.append(start(6, f"Rendering {clips} frame-locked cuts", current=0, total=clips,
                       unit="clips"))
    clip_counter = StageCounter(6, clips, min_interval=0.0)
    for _ in range(clips):
        events.append(clip_counter.advance(1, "clips rendered", unit="clips"))
    events.append(state(6, "Final assembly started", phase="assembly"))
    events.append(state(6, "Final assembly finished", phase="assembly"))
    events.append(end(6, f"{clips} clips rendered and assembled", current=clips, total=clips,
                      unit="clips"))
    return [e for e in events if e is not None]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_each_stage_starts_before_it_ends():
    events = _normal_run()
    for stage in (1, 2, 3, 4, 5, 6):
        kinds = [e.kind for e in events if e.stage == stage]
        assert kinds, f"stage {stage} emitted nothing"
        assert kinds[0] is EventKind.START, f"stage {stage} did not start first"
        assert kinds[-1] is EventKind.END, f"stage {stage} did not end last"


def test_stage_numbers_are_ordered_one_to_six():
    first_seen: list[int] = []
    for event in _normal_run():
        if event.stage not in first_seen:
            first_seen.append(event.stage)
    assert first_seen == [1, 2, 3, 4, 5, 6]


def test_no_stage_emits_after_its_end_in_the_normal_path():
    events = _normal_run()
    ended: set[int] = set()
    for event in events:
        assert event.stage not in ended, f"stage {event.stage} emitted after END"
        if event.kind is EventKind.END:
            ended.add(event.stage)


# ---------------------------------------------------------------------------
# Monotonic counts and stable totals
# ---------------------------------------------------------------------------


def test_stage5_completed_count_is_monotonic():
    seen = 0
    for event in _normal_run(sources=6, cache_hits=2):
        if event.stage == 5 and event.current is not None:
            assert event.current >= seen, "stage 5 count went backwards"
            seen = event.current


def test_stage6_completed_count_is_monotonic():
    seen = 0
    for event in _normal_run(clips=10):
        if event.stage == 6 and event.current is not None:
            assert event.current >= seen, "stage 6 count went backwards"
            seen = event.current


def test_total_never_changes_within_a_stage():
    totals: dict[int, int] = {}
    for event in _normal_run():
        if event.total is None:
            continue
        if event.stage in totals:
            assert event.total == totals[event.stage], f"stage {event.stage} total changed"
        else:
            totals[event.stage] = event.total


def test_current_never_exceeds_total():
    for event in _normal_run():
        if event.is_counted():
            assert event.current <= event.total


def test_final_stage5_and_stage6_events_reach_total():
    events = _normal_run(sources=5, cache_hits=1, clips=7)
    final5 = [e for e in events if e.stage == 5 and e.kind is EventKind.END][-1]
    final6 = [e for e in events if e.stage == 6 and e.kind is EventKind.END][-1]
    assert final5.current == final5.total == 5
    assert final6.current == final6.total == 7


def test_cache_hits_are_represented_and_counted_as_progress():
    """A fully cached run must show completion, not sit at 0 while doing nothing."""
    events = _normal_run(sources=4, cache_hits=4)
    counted = [e for e in events if e.stage == 5 and e.is_counted()]
    assert counted[-1].current == 4
    assert any(e.data.get("cache_hits") == 4 for e in events if e.stage == 5)


def test_retry_does_not_double_count_a_source():
    """A parallel failure followed by a serial retry advances the counter exactly once."""
    counter = StageCounter(5, 3, min_interval=0.0)
    stream = [
        start(5, "Analyzing 3 source video(s)", current=0, total=3, unit="sources"),
        counter.advance(1, "analyzed"),
        warning(5, "b.mp4 failed in parallel mode; retrying serially"),
        counter.advance(1, "analyzed"),   # the retried video, counted once
        counter.advance(1, "analyzed"),
    ]
    assert counter.current == 3
    assert [e.current for e in stream if e.is_counted()] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_clip_failures_do_not_report_false_completion():
    """5 of 6 clips succeed: the count must stop at 5 and an error must be present."""
    counter = StageCounter(6, 6, min_interval=0.0)
    stream = [start(6, "Rendering 6 frame-locked cuts", current=0, total=6, unit="clips")]
    for _ in range(5):
        stream.append(counter.advance(1, "clips rendered"))
    stream.append(warning(6, "Clip 6 failed: FFmpeg error"))
    stream.append(counter.snapshot("clip extraction complete", failed_clips=1))
    stream.append(error(6, "1 of 6 clip(s) failed; refusing to concatenate an incomplete timeline.",
                        failed_clips=1, total_clips=6))

    counted = [e for e in stream if e.is_counted()]
    assert counted[-1].current == 5, "must not claim 6/6 when a clip failed"
    assert counted[-1].current < counted[-1].total
    assert any(e.kind is EventKind.ERROR for e in stream)
    assert not any(e.kind is EventKind.END for e in stream), "no END on the refusal path"


def test_stage5_failure_emits_warning_then_end():
    stream = [
        start(5, "Analyzing 2 source video(s)", current=0, total=2, unit="sources"),
        warning(5, "Video analysis failed; renderer will use fallback sampling: boom"),
        end(5, "Video analysis unavailable"),
    ]
    assert any(e.kind is EventKind.WARNING for e in stream)
    final = stream[-1]
    assert final.kind is EventKind.END
    assert final.current is None, "a failed stage must not claim a completed count"


# ---------------------------------------------------------------------------
# ProgressView
# ---------------------------------------------------------------------------


def test_view_reports_stage_identity_without_parsing_strings():
    """The whole point of Phase 2A: stage identity comes from event.stage, not a regex."""
    view = ProgressView()
    view.apply(start(5, "Analyzing 758 source video(s)", current=0, total=758, unit="sources"))
    view.apply(progress(5, 531, 758, "analyzed", rate=1.4, elapsed_seconds=380.0,
                        cache_hits=420, unit="sources"))

    rendered = view.render()
    assert view.active_stage() == 5
    assert "Stage 5 — Video Analysis" in rendered
    assert "531 / 758" in rendered and "70.1%" in rendered
    assert "1.4 sources/s" in rendered
    assert "elapsed 6m 20s" in rendered


def test_view_shows_render_progress_shape():
    view = ProgressView()
    view.apply(start(6, "Rendering 1216 frame-locked cuts", current=0, total=1216, unit="clips"))
    view.apply(progress(6, 612, 1216, "clips rendered", rate=4.83, elapsed_seconds=127.0,
                        unit="clips"))

    rendered = view.render()
    assert "Stage 6 — Rendering" in rendered
    assert "612 / 1216 (50.3%)" in rendered
    assert "4.8 clips/s" in rendered
    assert "elapsed 2m 07s" in rendered


def test_view_is_monotonic_against_out_of_order_events():
    """Stage 5/6 emit from worker threads; a late lower event must not rewind the panel."""
    view = ProgressView()
    view.apply(start(6, "", current=0, total=10, unit="clips"))
    view.apply(progress(6, 7, 10))
    view.apply(progress(6, 4, 10))  # a straggler from another thread
    assert "7 / 10" in view.render()


def test_view_moves_finished_stages_into_a_completed_list():
    view = ProgressView()
    for event in _normal_run(sources=2, cache_hits=0, clips=2):
        view.apply(event)

    rendered = view.render()
    assert "Completed:" in rendered
    for stage in (1, 2, 3, 4, 5):
        assert f"✓ Stage {stage}" in rendered
    assert view.active_stage() == 6


def test_view_surfaces_warnings_and_errors():
    view = ProgressView()
    view.apply(start(6, "", current=0, total=3, unit="clips"))
    view.apply(warning(6, "Clip 2 failed: FFmpeg error"))
    view.apply(error(6, "1 of 3 clip(s) failed; refusing to concatenate an incomplete timeline."))

    rendered = view.render()
    assert "Notices:" in rendered
    assert "⚠️ Stage 6: Clip 2 failed" in rendered
    assert "❌ Stage 6: 1 of 3 clip(s) failed" in rendered
    assert len(view.notices) == 2


def test_view_bounds_the_notice_list():
    """Concise diagnostics, not a log dump."""
    view = ProgressView(max_notices=3)
    for i in range(20):
        view.apply(warning(6, f"failure {i}"))
    assert len(view.notices) == 3
    assert "failure 19" in view.notices[-1]


def test_view_shows_qwen_state_without_inventing_progress():
    view = ProgressView()
    view.apply(start(5, "", current=0, total=2, unit="sources"))
    view.apply(state(5, "Qwen semantic tagging started (2 video(s)) — no live per-frame progress "
                        "until Phase 2B", qwen_live_progress_available=False))

    rendered = view.render()
    assert "Qwen semantic tagging started" in rendered
    # No fabricated N/T for Qwen: the only counted numbers belong to the source counter.
    assert "0 / 2" in rendered


def test_view_handles_an_empty_stream():
    assert ProgressView().render() == "Waiting to start…"


def test_view_ignores_unknown_stage_numbers_gracefully():
    view = ProgressView()
    view.apply(ProgressEvent(stage=42, kind=EventKind.STATE, message="odd"))
    assert "Stage 42" in view.render()


def test_view_records_total_elapsed_from_the_final_event():
    view = ProgressView()
    view.apply(start(6, "", current=0, total=1, unit="clips"))
    view.apply(end(6, "done", current=1, total=1, total_elapsed_seconds=217.0))
    assert "Total: 3m 37s" in view.render()
