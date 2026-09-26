"""Phase 2B seen through the Phase 2A panel: what the user actually reads during Qwen.

These tests drive ``ProgressView`` with the real event stream a Qwen run produces — the Stage 5
deterministic source counter first, then translated worker protocol payloads — and assert the panel
stays truthful. The specific lies guarded against:

* a global ``1840 / 3620`` candidate percentage nobody can prove;
* the deterministic ``758 / 758`` source counter being displayed as Qwen's own progress;
* Qwen throughput labelled ``sources/s``;
* job 18 being ignored because job 17 counted higher.
"""

from __future__ import annotations

from beatsync_fork import progress as fp
from beatsync_fork import qwen_progress as qp
from beatsync_fork.progress_view import ProgressView


SOURCE_TOTAL = 758
QWEN_VIDEOS = 300


def _payload(kind: str, **fields):
    """A payload exactly as it arrives on the wire — encoded by the worker, decoded by the parent."""
    decoded = qp.decode(qp.encode(kind, **fields))
    assert decoded is not None
    return decoded


def _stage5_deterministic(view: ProgressView) -> None:
    """The counted part of Stage 5: 758 sources analysed before Qwen starts."""
    view.apply(fp.start(5, f"Analyzing {SOURCE_TOTAL} source video(s)",
                        current=0, total=SOURCE_TOTAL, unit="sources"))
    view.apply(fp.progress(5, SOURCE_TOTAL, SOURCE_TOTAL, "analyzed", unit="sources",
                           rate=1.8, rate_unit="analyzed sources", elapsed_seconds=420.0))
    view.apply(fp.state(5, f"Qwen semantic tagging started ({QWEN_VIDEOS} video(s))",
                        phase="qwen", qwen_videos=QWEN_VIDEOS,
                        qwen_live_progress_available=True))


def _feed(view: ProgressView, translator: qp.QwenProgressTranslator, kind: str, **fields):
    event = translator.handle_payload(_payload(kind, **fields))
    if event is not None:
        view.apply(event)
    return event


def _mid_run(min_interval: float = 0.0):
    """Panel state part-way through Qwen job 17 of 300."""
    view = ProgressView()
    translator = qp.QwenProgressTranslator(min_interval=min_interval)
    _stage5_deterministic(view)
    _feed(view, translator, "job_start", job_index=17, job_total=QWEN_VIDEOS, job_id="17",
          source_name="fight_scene.mp4", requested_candidate_count=120)
    _feed(view, translator, "job_progress", job_index=17, job_total=QWEN_VIDEOS, job_id="17",
          source_name="fight_scene.mp4", current=64, total=120,
          candidates_per_second=2.14, batch_size=8)
    return view, translator


# ---------------------------------------------------------------------------
# the Qwen phase is active and says what it knows
# ---------------------------------------------------------------------------


def test_qwen_is_the_active_phase_during_semantic_tagging():
    view, _ = _mid_run()
    assert view.active_stage() == 5
    assert view.active_phase(5) == "qwen"


def test_job_index_and_total_are_visible():
    view, _ = _mid_run()
    assert "Qwen job 17 / 300" in view.stage_line(5)


def test_candidate_current_and_total_are_visible_with_the_jobs_own_percentage():
    view, _ = _mid_run()
    line = view.stage_line(5)
    assert "64 / 120 candidates (53.3%)" in line, line


def test_candidate_rate_is_visible_and_labelled_candidates_per_second():
    view, _ = _mid_run()
    line = view.stage_line(5)
    assert "2.1 candidates/s" in line, line
    assert "sources/s" not in line, "Qwen throughput is candidates, not sources"


def test_batch_size_is_visible_when_the_worker_reports_it():
    view, _ = _mid_run()
    assert "batch 8" in view.stage_line(5)


def test_source_name_is_visible():
    view, _ = _mid_run()
    assert "fight_scene.mp4" in view.stage_line(5)


# ---------------------------------------------------------------------------
# what must NOT appear
# ---------------------------------------------------------------------------


def test_deterministic_source_count_is_not_shown_as_qwen_progress():
    """The 758/758 source counter belongs to the main phase and must not masquerade as Qwen's."""
    view, _ = _mid_run()
    line = view.stage_line(5)
    assert "758" not in line, line
    assert "100.0%" not in line, line
    assert "analyzed sources" not in line, line


def test_no_global_candidate_denominator_is_invented():
    """Only per-job numbers are ever displayed; no cross-job total is claimed."""
    view = ProgressView()
    translator = qp.QwenProgressTranslator(min_interval=0.0)
    _stage5_deterministic(view)
    for job in (1, 2, 3):
        _feed(view, translator, "job_start", job_index=job, job_total=3, job_id=str(job),
              source_name=f"v{job}.mp4", requested_candidate_count=100)
        _feed(view, translator, "job_progress", job_index=job, job_total=3, job_id=str(job),
              current=100, total=100, candidates_per_second=2.0, batch_size=8)
        _feed(view, translator, "job_end", job_index=job, job_total=3, job_id=str(job),
              frame_count=100, tag_count=98, inference_seconds=50.0)
    line = view.stage_line(5)
    assert "300" not in line, f"cross-job denominator leaked: {line}"
    assert "/ 100" in line or "100 " in line


def test_qwen_phase_carries_no_counter_so_no_counted_percentage_is_rendered():
    """Explicitly: per-job progress is uncounted STATE, which is why the numbers live in the text."""
    view, translator = _mid_run()
    event = translator.handle_payload(_payload(
        "job_progress", job_index=17, job_total=QWEN_VIDEOS, current=65, total=120,
        candidates_per_second=2.1, batch_size=8))
    assert event.kind is fp.EventKind.STATE
    assert event.current is None and event.total is None
    # The percentage the user sees is the job's own, composed by the translator, not ProgressView's.
    assert "(54.2%)" in event.message


# ---------------------------------------------------------------------------
# history and hand-off
# ---------------------------------------------------------------------------


def test_completed_deterministic_source_work_stays_visible_as_history():
    view, _ = _mid_run()
    rendered = view.render()
    assert "758 sources completed" in rendered, rendered
    assert "Stage 5" in rendered and "(running)" in rendered


def test_a_later_job_is_not_suppressed_by_an_earlier_higher_count():
    """The failure mode a counted phase would have: job 18 restarting lower looks stale."""
    view, translator = _mid_run()
    _feed(view, translator, "job_progress", job_index=17, job_total=QWEN_VIDEOS,
          current=120, total=120, candidates_per_second=2.09, batch_size=8)
    _feed(view, translator, "job_end", job_index=17, job_total=QWEN_VIDEOS, job_id="17",
          source_name="fight_scene.mp4", frame_count=120, tag_count=118, inference_seconds=57.4)
    _feed(view, translator, "job_start", job_index=18, job_total=QWEN_VIDEOS, job_id="18",
          source_name="quiet_scene.mp4", requested_candidate_count=130)
    _feed(view, translator, "job_progress", job_index=18, job_total=QWEN_VIDEOS, job_id="18",
          source_name="quiet_scene.mp4", current=4, total=130,
          candidates_per_second=1.1, batch_size=8)

    line = view.stage_line(5)
    assert "Qwen job 18 / 300" in line, line
    assert "4 / 130 candidates (3.1%)" in line, line
    assert "120 / 120" not in line, line


def test_qwen_finish_hands_over_cleanly_to_the_stage_5_end():
    view, translator = _mid_run()
    _feed(view, translator, "job_end", job_index=300, job_total=QWEN_VIDEOS, job_id="300",
          source_name="last.mp4", frame_count=90, tag_count=88, inference_seconds=41.0)
    view.apply(fp.metric(5, "Qwen tags 26400/27000 in 54.2m",
                         qwen_tag_count=26400, qwen_frame_count=27000))
    view.apply(fp.state(5, "Qwen semantic tagging finished", phase="qwen", qwen_seconds=3252.0))
    # While Qwen is still the active phase, its own closing message is what the panel shows.
    assert "Qwen semantic tagging finished" in view.stage_line(5)
    assert view.active_phase(5) == "qwen"

    view.apply(fp.end(5, "41200 visual moments, action=0.55, beauty=0.61, quality=0.72",
                      current=SOURCE_TOTAL, total=SOURCE_TOTAL, unit="sources",
                      elapsed_seconds=3980.0))

    # The stage END carries no phase, so the hand-off returns to Stage 5's own counter, which ends on
    # its real source total rather than on anything Qwen-shaped.
    assert view.active_phase(5) is None
    line = view.stage_line(5)
    assert f"{SOURCE_TOTAL} / {SOURCE_TOTAL} (100.0%)" in line, line
    assert "41200 visual moments" in line
    assert "Qwen" not in line, line
    rendered = view.render()
    assert "Stage 5" in rendered
    assert "elapsed 1h 06m" in rendered


def test_worker_state_covers_the_silent_model_load():
    """The long gap before the first candidate is now narrated instead of looking hung."""
    view = ProgressView()
    translator = qp.QwenProgressTranslator(min_interval=0.0)
    _stage5_deterministic(view)
    _feed(view, translator, "worker_state", state="loading_model",
          message="loading Qwen model (llama.cpp)")
    assert "loading Qwen model" in view.stage_line(5)
    _feed(view, translator, "worker_state", state="backend_ready", message="Qwen backend ready",
          batch_size=8, device="Vulkan0 (RTX 4070)")
    line = view.stage_line(5)
    assert "Qwen backend ready" in line
    assert "758" not in line


def test_prefetch_state_is_attributed_to_its_job():
    view = ProgressView()
    translator = qp.QwenProgressTranslator(min_interval=0.0)
    _stage5_deterministic(view)
    _feed(view, translator, "job_start", job_index=5, job_total=9, job_id="5",
          source_name="clip.mp4", requested_candidate_count=42)
    _feed(view, translator, "worker_state", state="prefetch",
          message="decoding 42 candidate frames", job_index=5, job_total=9, job_id="5",
          source_name="clip.mp4")
    line = view.stage_line(5)
    assert "Qwen job 5 / 9" in line
    assert "decoding 42 candidate frames" in line


# ---------------------------------------------------------------------------
# failure surfaces
# ---------------------------------------------------------------------------


def test_worker_failure_is_a_bounded_notice_not_a_stderr_dump():
    view = ProgressView()
    _stage5_deterministic(view)
    tail = "ggml_vulkan: device lost " * 400
    view.apply(fp.warning(5, f"Qwen batch worker failed (exit 3): {tail[-200:]}",
                          phase="qwen", qwen_returncode=3))
    rendered = view.render()
    assert "Qwen batch worker failed (exit 3)" in rendered
    assert len(rendered) < 2000, "the panel must not become a log sink"
    # Deterministic work already done is still reported.
    assert "758" in rendered


def test_timeout_warning_states_the_fallback():
    view = ProgressView()
    _stage5_deterministic(view)
    view.apply(fp.warning(5, "Qwen batch worker timed out; deterministic visual tags remain "
                             "active.", phase="qwen", qwen_timed_out=True))
    assert "deterministic visual tags remain active" in view.render()


# ---------------------------------------------------------------------------
# a fully cached run must not fabricate a Qwen phase
# ---------------------------------------------------------------------------


def test_fully_cached_stage5_shows_no_qwen_phase_at_all():
    """No worker is launched when every source is cached, so no Qwen state may appear."""
    view = ProgressView()
    view.apply(fp.start(5, "Analyzing 758 source video(s)", current=0, total=758, unit="sources"))
    for index in (200, 500, 758):
        view.apply(fp.progress(5, index, 758, "cached", cache_hits=index, unit="sources"))
    view.apply(fp.metric(5, "758 cached, 0 to analyze, 1 worker(s)", cache_hits=758))
    view.apply(fp.end(5, "41200 visual moments", current=758, total=758, unit="sources",
                      elapsed_seconds=3.0))

    rendered = view.render()
    assert view.active_phase(5) is None, "no qwen phase should exist"
    assert "Qwen" not in rendered, rendered
    assert "758 / 758 (100.0%)" in rendered
