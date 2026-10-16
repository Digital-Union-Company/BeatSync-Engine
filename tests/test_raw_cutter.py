"""Raw Cutter V1: the pure segmentation planner.

Ordinary imported-module tests, because `beatsync_fork.raw_cutter` is stdlib-only by rule. No
FFmpeg, no media, no filesystem — the whole segmentation policy is exercised here as arithmetic,
which is exactly what the pure/adapter split was for.

The four invariants are checked on *every* plan this file builds (see `_check`), not only in the
test that names them, so any case added later inherits the guarantee for free.
"""

from __future__ import annotations

import pytest

from beatsync_fork import raw_cutter as rc


def _check(plan: rc.SegmentPlan, duration: float) -> rc.SegmentPlan:
    """The four guarantees, re-asserted independently of the planner's own internal check."""
    assert plan.segments, "a plan must have at least one segment"
    assert plan.segments[0].start == 0.0
    assert abs(plan.segments[-1].end - duration) <= rc.TIME_QUANTUM
    for a, b in zip(plan.segments, plan.segments[1:]):
        assert abs(b.start - a.end) <= rc.TIME_QUANTUM, "gap or overlap"
    for segment in plan.segments:
        assert segment.duration > 0, "zero-length segment"
    assert abs(plan.covered_duration - duration) <= rc.TIME_QUANTUM * len(plan.segments)
    assert [s.index for s in plan.segments] == list(range(1, len(plan.segments) + 1))
    return plan


# =============================================================================================
# 1. Sensitivity -> threshold
# =============================================================================================

def test_the_default_sensitivity_is_the_detectors_own_threshold():
    """50 reproduces what the rest of BeatSync already uses, rather than a third number."""
    assert rc.sensitivity_to_threshold(50) == pytest.approx(0.27)
    assert rc.SENSITIVITY_DEFAULT == 50


def test_the_mapping_is_strictly_decreasing_across_the_whole_range():
    """More sensitivity must mean more cuts. A control that reads backwards is a usability bug,
    and checking only the three anchors would miss a non-monotonic middle."""
    values = [rc.sensitivity_to_threshold(v) for v in range(0, 101)]
    for lower, higher in zip(values, values[1:]):
        assert higher <= lower
    assert values[0] > values[50] > values[100]


def test_the_threshold_never_drops_to_zero():
    """At 0 the scene filter selects essentially every frame, which is not a creative cut."""
    assert rc.sensitivity_to_threshold(100) >= rc.THRESHOLD_FLOOR
    assert rc.sensitivity_to_threshold(100) == pytest.approx(0.05)


@pytest.mark.parametrize("junk", [None, "", "abc", [], {}, float("nan"), float("inf"), True])
def test_sensitivity_normalisation_is_total(junk):
    assert rc.normalize_sensitivity(junk) == rc.SENSITIVITY_DEFAULT
    assert rc.THRESHOLD_FLOOR <= rc.sensitivity_to_threshold(junk) <= 1.0


def test_sensitivity_is_clamped_not_wrapped():
    assert rc.normalize_sensitivity(-40) == 0
    assert rc.normalize_sensitivity(9999) == 100


# =============================================================================================
# 2. Duration validation
# =============================================================================================

def test_min_must_be_positive_and_max_at_least_min():
    with pytest.raises(rc.RawCutterError):
        rc.normalize_durations(0, 10)
    with pytest.raises(rc.RawCutterError):
        rc.normalize_durations(-1, 10)
    with pytest.raises(rc.RawCutterError):
        rc.normalize_durations(10, 5)
    assert rc.normalize_durations(5, 5) == (5.0, 5.0)


@pytest.mark.parametrize("junk", [None, "", "abc", [], float("nan"), float("inf")])
def test_junk_durations_are_refused_not_guessed(junk):
    with pytest.raises(rc.RawCutterError):
        rc.normalize_durations(junk, 300)
    with pytest.raises(rc.RawCutterError):
        rc.normalize_durations(5, junk)


def test_an_unusable_source_duration_is_refused():
    for bad in (0, -5, None, "abc", float("nan")):
        with pytest.raises(rc.RawCutterError):
            rc.plan_segments(bad, [1, 2, 3])


# =============================================================================================
# 3. Obvious scene boundaries
# =============================================================================================

def test_well_spaced_scene_changes_become_one_segment_each():
    plan = _check(rc.plan_segments(60.0, [15.0, 30.0, 45.0], 5, 300), 60.0)
    assert plan.count == 4
    assert [s.start for s in plan.segments] == [0.0, 15.0, 30.0, 45.0]
    assert [s.end for s in plan.segments] == [15.0, 30.0, 45.0, 60.0]
    assert all(s.boundary_kind in (rc.SCENE_CUT, rc.SOURCE_END) for s in plan.segments)
    assert plan.detected_scene_count == 3


def test_scene_times_outside_the_source_are_ignored():
    plan = _check(rc.plan_segments(30.0, [-5.0, 0.0, 10.0, 30.0, 99.0], 5, 300), 30.0)
    assert plan.detected_scene_count == 1
    assert [s.start for s in plan.segments] == [0.0, 10.0]


def test_duplicate_and_unordered_scene_times_are_handled():
    a = rc.plan_segments(60.0, [45.0, 15.0, 30.0, 15.0], 5, 300)
    b = rc.plan_segments(60.0, [15.0, 30.0, 45.0], 5, 300)
    assert [(s.start, s.end) for s in a.segments] == [(s.start, s.end) for s in b.segments]


@pytest.mark.parametrize("junk", [None, "abc", [], {}, float("nan"), float("inf")])
def test_a_malformed_scene_time_is_ignored_rather_than_fatal(junk):
    """The detector is an estimate; one bad timestamp must not cost the operator their plan."""
    plan = _check(rc.plan_segments(60.0, [15.0, junk, 45.0], 5, 300), 60.0)
    assert plan.detected_scene_count == 2


# =============================================================================================
# 4. Rapid scene changes shorter than the minimum
# =============================================================================================

def test_a_burst_of_rapid_cuts_becomes_one_segment_not_many_tiny_ones():
    """Nothing is discarded for being short: the burst is COMBINED."""
    rapid = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0]
    plan = _check(rc.plan_segments(40.0, rapid, 5, 300), 40.0)
    assert all(s.duration >= 5.0 for s in plan.segments), [s.duration for s in plan.segments]
    assert plan.count == 2
    assert plan.covered_duration == pytest.approx(40.0)


def test_every_frame_of_a_rapid_burst_is_still_covered():
    rapid = [i * 0.4 for i in range(1, 100)]
    plan = _check(rc.plan_segments(45.0, rapid, 5, 300), 45.0)
    assert plan.covered_duration == pytest.approx(45.0)


def test_a_short_final_scene_is_folded_back_rather_than_dropped():
    """A 2-second tail after a 20-second scene must not vanish."""
    plan = _check(rc.plan_segments(22.0, [20.0], 5, 300), 22.0)
    assert plan.count == 1
    assert plan.segments[0].end == pytest.approx(22.0)


def test_a_short_tail_that_cannot_be_folded_is_kept_and_reported():
    """When merging the tail would break the maximum, the tail stays -- flagged, never discarded."""
    plan = _check(rc.plan_segments(32.0, [30.0], 5, 30), 32.0)
    assert plan.covered_duration == pytest.approx(32.0)
    assert plan.short_segments, "the short tail must be flagged"
    assert any("shorter than" in note for note in plan.notes)


# =============================================================================================
# 5. One long scene exceeding the maximum
# =============================================================================================

def test_a_long_continuous_scene_is_split_by_time():
    plan = _check(rc.plan_segments(300.0, [], 5, 60), 300.0)
    assert plan.count == 5
    assert all(s.duration <= 60.0 + 1e-6 for s in plan.segments)
    assert any(s.boundary_kind == rc.TIME_CUT for s in plan.segments)


def test_a_long_scene_prefers_an_interior_scene_change_when_one_fits():
    """Scene-based beats time-based when a detected cut lands in the legal window."""
    plan = _check(rc.plan_segments(100.0, [52.0], 5, 60), 100.0)
    boundaries = [s.end for s in plan.segments[:-1]]
    assert 52.0 in boundaries, boundaries
    assert plan.segments[0].boundary_kind == rc.SCENE_CUT


def test_a_scene_change_outside_the_legal_window_is_not_used():
    """A cut at 2s cannot end a segment whose minimum is 5s, so the split falls back to time."""
    plan = _check(rc.plan_segments(100.0, [2.0], 5, 40), 100.0)
    assert all(s.duration <= 40.0 + 1e-6 for s in plan.segments)
    assert all(s.duration > 2.0 for s in plan.segments)


def test_no_segment_exceeds_the_maximum_across_many_shapes():
    for duration in (61.0, 100.0, 301.0, 1000.0, 3600.0):
        for maximum in (10.0, 30.0, 60.0, 300.0):
            plan = _check(rc.plan_segments(duration, [], 5, maximum), duration)
            assert all(s.duration <= maximum + 1e-6 for s in plan.segments), (duration, maximum)


# =============================================================================================
# 6. No detected scene changes at all
# =============================================================================================

def test_a_source_with_no_scene_changes_still_produces_a_plan():
    plan = _check(rc.plan_segments(120.0, [], 5, 300), 120.0)
    assert plan.count == 1
    assert plan.segments[0].duration == pytest.approx(120.0)
    assert plan.detected_scene_count == 0
    assert any("No scene changes" in note for note in plan.notes)


def test_a_source_shorter_than_the_minimum_is_one_flagged_segment_not_an_empty_plan():
    """Guarantee 3: footage is never discarded for failing a bound."""
    plan = _check(rc.plan_segments(3.0, [], 5, 300), 3.0)
    assert plan.count == 1
    assert plan.segments[0].duration == pytest.approx(3.0)
    assert plan.segments[0].below_minimum
    assert plan.short_segments


def test_a_very_short_source_with_cuts_is_still_fully_covered():
    plan = _check(rc.plan_segments(2.0, [0.5, 1.0, 1.5], 5, 300), 2.0)
    assert plan.covered_duration == pytest.approx(2.0)


# =============================================================================================
# 7. Determinism
# =============================================================================================

def test_identical_inputs_produce_an_identical_plan():
    scenes = [3.2, 9.9, 10.1, 44.0, 44.5, 90.0, 180.0]
    first = rc.plan_segments(240.0, scenes, 5, 60, 50)
    for _ in range(5):
        again = rc.plan_segments(240.0, list(scenes), 5, 60, 50)
        assert [(s.start, s.end, s.boundary_kind) for s in again.segments] == \
               [(s.start, s.end, s.boundary_kind) for s in first.segments]


def test_the_plan_is_independent_of_the_order_scene_times_arrive_in():
    scenes = [90.0, 3.2, 180.0, 44.0]
    a = rc.plan_segments(240.0, scenes, 5, 60)
    b = rc.plan_segments(240.0, sorted(scenes, reverse=True), 5, 60)
    assert [(s.start, s.end) for s in a.segments] == [(s.start, s.end) for s in b.segments]


# =============================================================================================
# 8. Naming, staleness and formatting
# =============================================================================================

def test_the_run_directory_is_named_after_the_source_and_the_run():
    name = rc.run_directory_name(r"J:\Footage\My Video.mkv", "20261016_203000")
    assert name == "My_Video_raw_parts_20261016_203000"


def test_segment_filenames_are_zero_padded_to_the_real_total():
    assert rc.segment_filename("clip.mkv", 7, 9) == "clip_part_007.mp4"
    assert rc.segment_filename("clip.mkv", 7, 1200) == "clip_part_0007.mp4"


@pytest.mark.parametrize("hostile", [
    r"..\..\escape.mkv", "con.mkv", "a/b/c.mkv", "   .mkv", "", None,
    "name with spaces & symbols!.mkv", "." * 40 + ".mkv",
])
def test_filenames_built_from_hostile_source_names_stay_safe(hostile):
    name = rc.segment_filename(hostile, 1, 1)
    assert "/" not in name and "\\" not in name
    assert ".." not in name
    assert name.endswith(".mp4") and len(name) > len("_part_001.mp4")


def test_the_plan_signature_changes_with_the_planning_inputs():
    source = rc.SourceInfo(path=r"C:\a.mkv", duration=100.0, size_bytes=10, mtime_ns=20)
    base = rc.plan_signature(source, 50, 5, 300)
    assert rc.plan_signature(source, 60, 5, 300) != base
    assert rc.plan_signature(source, 50, 6, 300) != base
    assert rc.plan_signature(source, 50, 5, 200) != base
    changed = rc.SourceInfo(path=r"C:\a.mkv", duration=100.0, size_bytes=11, mtime_ns=20)
    assert rc.plan_signature(changed, 50, 5, 300) != base
    retimed = rc.SourceInfo(path=r"C:\a.mkv", duration=100.0, size_bytes=10, mtime_ns=21)
    assert rc.plan_signature(retimed, 50, 5, 300) != base
    assert rc.plan_signature(source, 50, 5, 300) == base, "it must be stable for equal inputs"


def test_the_signature_is_case_insensitive_about_the_path_like_windows_is():
    low = rc.SourceInfo(path=r"C:\Footage\a.mkv", duration=1.0)
    up = rc.SourceInfo(path=r"C:\FOOTAGE\A.MKV", duration=1.0)
    assert rc.plan_signature(low, 50, 5, 300) == rc.plan_signature(up, 50, 5, 300)


def test_timecodes_are_readable_and_total():
    assert rc.format_timecode(0) == "0:00:00.000"
    assert rc.format_timecode(3661.5) == "1:01:01.500"
    assert rc.format_timecode(None) == "--:--:--"
    assert rc.format_timecode(-1) == "--:--:--"
    assert rc.format_timecode(float("nan")) == "--:--:--"


def test_the_table_has_one_row_per_segment_in_order():
    plan = rc.plan_segments(60.0, [15.0, 30.0], 5, 300)
    rows = rc.plan_table_rows(plan)
    assert len(rows) == plan.count
    assert [row[0] for row in rows] == ["1", "2", "3"]
    assert all(len(row) == len(rc.PLAN_TABLE_HEADERS) for row in rows)


def test_the_summary_states_coverage_and_promises_nothing_was_written():
    source = rc.SourceInfo(path=r"C:\a.mkv", duration=60.0, width=1920, height=1080, fps=30.0)
    text = rc.plan_summary_text(rc.plan_segments(60.0, [15.0], 5, 300), source)
    assert "Nothing has been written yet" in text
    assert "no gaps or overlaps" in text
    assert "1920x1080" in text


def test_the_planner_imports_nothing_that_could_act():
    """Belt and braces beside `test_no_runtime_dependency.py`: the planner must stay inert.

    Checked STRUCTURALLY, over the real import statements, rather than by scanning the text. The
    module's own docstring legitimately contains the words it is banned from importing -- in the
    sentences explaining that it imports none of them -- and a ban the explanation of the ban
    cannot survive forces documentation to be written around a test. This repository has made that
    mistake six times now; the AST does not have the problem.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(rc))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for forbidden in ("subprocess", "shutil", "ffmpeg_processing", "gradio", "numpy", "cv2",
                      "cupy", "librosa", "logger", "paths", "video_analysis", "threading"):
        assert forbidden not in imported, f"the planner imports {forbidden}"

    # `os` is permitted for path STRING manipulation only -- never for touching the filesystem.
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("open", "os.remove", "os.makedirs", "os.stat", "os.listdir", "os.rename",
                      "os.walk", "os.mkdir", "time.time", "datetime.now"):
        assert forbidden not in called, f"the planner calls {forbidden}"


def test_sizes_are_reported_in_a_unit_that_keeps_the_number_meaningful():
    """A fixed MB unit printed "0.0 MB" after a run that really did write two files, which reads
    as nothing having happened."""
    assert rc.format_size(0) == "0 bytes"
    assert rc.format_size(900) == "900 bytes"
    assert rc.format_size(5_632) == "5.5 KB"
    assert rc.format_size(3 * 1024 ** 2) == "3.0 MB"
    assert rc.format_size(2 * 1024 ** 3) == "2.0 GB"
    for junk in (None, "abc", -5, float("nan")):
        assert rc.format_size(junk) == "unknown size"
