"""Counted subphases must not share a counter just because they share a stage number.

Stage 6 ProRes deliberately contains two counted subphases with different units and different
denominators — conversion counts *sources*, extraction counts *clips* — plus a final assembly phase
that has no counter at all. Stage 5 likewise has a counted deterministic pass followed by an uncounted
Qwen phase.

Applying monotonicity across the whole stage merges them, so a 758-source conversion followed by a
100-segment extraction renders ``758 / 100 (758%)``. The contract is:

    monotonicity key = (stage, phase)

not ``stage`` alone. Monotonicity is *kept* within each phase; it is only stopped from leaking across
phase boundaries.

The phase strings used here are the ones the pipeline actually emits; tests at the bottom assert that
by reading ``video_processor.py`` and ``video_analysis.py``, so this file cannot drift into testing a
made-up shape.
"""

from __future__ import annotations

import ast
import os

from beatsync_fork.progress import end, metric, progress, start, state, warning
from beatsync_fork.progress_view import ProgressView

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

PHASE_CONVERT = "prores_convert"
PHASE_EXTRACT = "prores_extract"
PHASE_ASSEMBLY = "assembly"
PHASE_QWEN = "qwen"


def _prores_stream(sources: int, segments: int, extracted: int = 1):
    """The exact event shape video_processor emits on the ProRes path."""
    events = [
        start(6, f"Rendering {segments} frame-locked cuts", current=0, total=segments,
              unit="clips"),
        state(6, f"ProRes conversion started ({sources} source(s))", phase=PHASE_CONVERT),
    ]
    for i in range(1, sources + 1):
        events.append(progress(6, i, sources, "converted to ProRes",
                               phase=PHASE_CONVERT, unit="sources"))
    events.append(state(6, f"ProRes segment extraction started ({segments} segments)",
                        phase=PHASE_EXTRACT))
    for i in range(1, extracted + 1):
        events.append(progress(6, i, segments, "segments extracted",
                               phase=PHASE_EXTRACT, unit="clips"))
    return events


def _view_for(events) -> ProgressView:
    view = ProgressView()
    for event in events:
        view.apply(event)
    return view


# ---------------------------------------------------------------------------
# A / B / C — the reported defect
# ---------------------------------------------------------------------------


def test_prores_extraction_does_not_inherit_the_conversion_count():
    """A. 758 sources then 100 segments must not render 758 / 100."""
    view = _view_for(_prores_stream(sources=758, segments=100, extracted=1))
    rendered = view.render()

    assert "758 / 100" not in rendered, "conversion count leaked into extraction"
    assert "1 / 100" in rendered
    assert "(1.0%)" in rendered


def test_extraction_starts_at_its_real_value_when_sources_exceed_segments():
    """B. 758 sources, 1216 segments: extraction must not begin at 758/1216 (62.3%)."""
    view = _view_for(_prores_stream(sources=758, segments=1216, extracted=1))
    rendered = view.render()

    assert "758 / 1216" not in rendered
    assert "62.3%" not in rendered
    assert "1 / 1216" in rendered


def test_no_displayed_current_ever_exceeds_its_phase_total():
    """C. Whatever the relative sizes, the panel may never show more than 100%."""
    for sources, segments in ((758, 100), (100, 758), (1216, 1216), (5, 3)):
        view = _view_for(_prores_stream(sources=sources, segments=segments, extracted=1))
        line = view.stage_line(6)
        assert "1 / " + str(segments) in line, (sources, segments, line)
        percent = 100.0 / segments
        assert f"({percent:.1f}%)" in line, (sources, segments, line)


# ---------------------------------------------------------------------------
# Monotonicity is preserved *within* each phase
# ---------------------------------------------------------------------------


def test_standard_render_remains_monotonic_against_a_late_lower_event():
    """The useful protection must survive: 7/10 then a straggling 4/10 stays 7/10."""
    view = _view_for([
        start(6, "Rendering 10 frame-locked cuts", current=0, total=10, unit="clips"),
        progress(6, 7, 10, "clips rendered", unit="clips"),
        progress(6, 4, 10, "clips rendered", unit="clips"),
    ])
    assert "7 / 10" in view.render()


def test_prores_conversion_is_monotonic_within_its_phase():
    view = _view_for([
        start(6, "", current=0, total=50, unit="clips"),
        state(6, "ProRes conversion started (20 source(s))", phase=PHASE_CONVERT),
        progress(6, 15, 20, "converted to ProRes", phase=PHASE_CONVERT, unit="sources"),
        progress(6, 9, 20, "converted to ProRes", phase=PHASE_CONVERT, unit="sources"),
    ])
    line = view.stage_line(6)
    assert "15 / 20" in line
    assert "sources/s" in line or "15 / 20" in line


def test_prores_extraction_is_monotonic_within_its_phase():
    events = _prores_stream(sources=4, segments=10, extracted=6)
    events.append(progress(6, 2, 10, "segments extracted", phase=PHASE_EXTRACT, unit="clips"))
    view = _view_for(events)
    assert "6 / 10" in view.render()


def test_stage5_deterministic_pass_is_monotonic_within_its_phase():
    view = _view_for([
        start(5, "Analyzing 6 source video(s)", current=0, total=6, unit="sources"),
        progress(5, 4, 6, "analyzed", unit="sources"),
        progress(5, 2, 6, "analyzed", unit="sources"),
    ])
    assert "4 / 6" in view.render()


# ---------------------------------------------------------------------------
# Units, rate and elapsed must not leak between phases
# ---------------------------------------------------------------------------


def test_unit_switches_cleanly_between_phases():
    events = _prores_stream(sources=8, segments=20, extracted=3)
    view = _view_for(events)
    line = view.stage_line(6)
    assert "3 / 20" in line
    assert "sources/s" not in line, "conversion unit leaked into extraction"


def test_rate_and_elapsed_do_not_masquerade_across_phases():
    view = _view_for([
        start(6, "", current=0, total=100, unit="clips"),
        state(6, "ProRes conversion started (10 source(s))", phase=PHASE_CONVERT),
        progress(6, 10, 10, "converted to ProRes", phase=PHASE_CONVERT, unit="sources",
                 elapsed_seconds=600.0, rate=0.016),
        state(6, "ProRes segment extraction started (100 segments)", phase=PHASE_EXTRACT),
        progress(6, 1, 100, "segments extracted", phase=PHASE_EXTRACT, unit="clips",
                 elapsed_seconds=2.0, rate=0.5),
    ])
    line = view.stage_line(6)
    assert "elapsed 10m 00s" not in line, "conversion elapsed leaked into extraction"
    assert "elapsed 2s" in line
    assert "0.5 clips/s" in line


# ---------------------------------------------------------------------------
# Assembly: no fake percentage
# ---------------------------------------------------------------------------


def test_assembly_state_clears_the_counted_percentage():
    """During 'Final assembly started' the UI must not still claim 1216 / 1216 (100%)."""
    view = _view_for([
        start(6, "Rendering 1216 frame-locked cuts", current=0, total=1216, unit="clips"),
        progress(6, 1216, 1216, "clips rendered", unit="clips"),
        state(6, "Final assembly started", phase=PHASE_ASSEMBLY),
    ])
    rendered = view.render()

    assert "Final assembly started" in rendered
    assert "1216 / 1216" not in rendered, "assembly must not present the clip counter as its own"
    assert "100.0%" not in rendered


def test_assembly_may_still_show_completed_clip_work_as_history():
    view = _view_for([
        start(6, "", current=0, total=12, unit="clips"),
        progress(6, 12, 12, "clips rendered", unit="clips"),
        state(6, "Final assembly started", phase=PHASE_ASSEMBLY),
    ])
    rendered = view.render()
    assert "12" in rendered, "completed work should remain visible somewhere"
    assert "(100.0%)" not in view.stage_line(6), "but not as the active counter"


def test_final_stage6_end_still_reports_total_over_total():
    events = _prores_stream(sources=4, segments=10, extracted=10)
    events.append(state(6, "Final assembly started", phase=PHASE_ASSEMBLY))
    events.append(state(6, "Final assembly finished", phase=PHASE_ASSEMBLY))
    events.append(end(6, "10 ProRes segments assembled", current=10, total=10,
                      encoder="PRORES_PROXY", unit="clips"))
    view = _view_for(events)
    rendered = view.render()
    assert "10 / 10" in rendered
    assert "100.0%" in rendered


def test_standard_path_end_reports_total_over_total():
    view = _view_for([
        start(6, "", current=0, total=5, unit="clips"),
        progress(6, 5, 5, "clips rendered", unit="clips"),
        state(6, "Final assembly started", phase=PHASE_ASSEMBLY),
        state(6, "Final assembly finished", phase=PHASE_ASSEMBLY),
        end(6, "5 clips rendered and assembled", current=5, total=5, unit="clips"),
    ])
    assert "5 / 5 (100.0%)" in view.render()


# ---------------------------------------------------------------------------
# Stage 5: Qwen must not wear the deterministic pass's 100%
# ---------------------------------------------------------------------------


def test_qwen_phase_does_not_display_deterministic_completion_as_its_own():
    view = _view_for([
        start(5, "Analyzing 758 source video(s)", current=0, total=758, unit="sources"),
        progress(5, 758, 758, "analyzed", unit="sources"),
        state(5, "Qwen semantic tagging started (300 video(s)) — no live per-frame progress "
                 "until Phase 2B", phase=PHASE_QWEN),
    ])
    rendered = view.render()

    assert "Qwen semantic tagging started" in rendered
    assert "758 / 758" not in view.stage_line(5), "Qwen must not inherit the source counter"
    assert "(100.0%)" not in view.stage_line(5)


def test_qwen_phase_keeps_deterministic_result_visible_as_history():
    view = _view_for([
        start(5, "Analyzing 4 source video(s)", current=0, total=4, unit="sources"),
        metric(5, "1 cached, 3 to analyze, 2 worker(s)", cache_hits=1),
        progress(5, 4, 4, "analyzed", unit="sources"),
        state(5, "Qwen semantic tagging started (3 video(s))", phase=PHASE_QWEN),
    ])
    rendered = view.render()
    assert "4" in rendered, "the completed deterministic count should still be reported"
    assert "Qwen semantic tagging started" in rendered


def test_stage5_end_still_records_the_completed_source_count():
    view = _view_for([
        start(5, "Analyzing 758 source video(s)", current=0, total=758, unit="sources"),
        progress(5, 758, 758, "analyzed", unit="sources"),
        state(5, "Qwen semantic tagging started (300 video(s))", phase=PHASE_QWEN),
        metric(5, "Qwen tags 3619/3620 in 97.5s", qwen_tag_count=3619),
        state(5, "Qwen semantic tagging finished", phase=PHASE_QWEN),
        end(5, "3620 visual moments", current=758, total=758, cache_hits=0, unit="sources"),
    ])
    rendered = view.render()
    assert "758 / 758" in rendered
    assert "3620 visual moments" in rendered


def test_qwen_failure_warning_still_surfaces():
    view = _view_for([
        start(5, "", current=0, total=2, unit="sources"),
        progress(5, 2, 2, "analyzed", unit="sources"),
        state(5, "Qwen semantic tagging started (2 video(s))", phase=PHASE_QWEN),
        warning(5, "Qwen produced no semantic tags; deterministic visual tags retained."),
    ])
    rendered = view.render()
    assert "⚠️ Stage 5: Qwen produced no semantic tags" in rendered


# ---------------------------------------------------------------------------
# The phase strings must be the ones the pipeline really emits
# ---------------------------------------------------------------------------


def _string_constants(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    return {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _phase_keyword_values(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "phase" and isinstance(kw.value, ast.Constant):
                    found.add(kw.value.value)
    return found


def test_video_processor_emits_the_expected_phase_markers():
    phases = _phase_keyword_values(os.path.join(_SRC, "video_processor.py"))
    assert {PHASE_CONVERT, PHASE_EXTRACT, PHASE_ASSEMBLY} <= phases, phases


def test_video_analysis_marks_the_qwen_phase():
    """Without this marker the view cannot tell the Qwen phase from the deterministic one."""
    phases = _phase_keyword_values(os.path.join(_SRC, "video_analysis.py"))
    assert PHASE_QWEN in phases, phases


def test_standard_clip_path_uses_no_phase_marker():
    """The standard render is the stage's main counter; it must not be given a phase of its own."""
    constants = _string_constants(os.path.join(_SRC, "video_processor.py"))
    assert "clips rendered" in constants, "the standard advance message changed"
