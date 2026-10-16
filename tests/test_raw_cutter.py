"""Raw Cutter V1: the pure shot-selection planner (quality-first, R1).

Ordinary imported-module tests, because `beatsync_fork.raw_cutter` is stdlib-only by rule. No
FFmpeg, no media, no filesystem — the whole selection policy is exercised here as arithmetic,
which is exactly what the pure/adapter split was for.

**The coverage-era assertions are gone on purpose.** The first version tiled the source and these
tests checked that every frame landed in some clip. That was the wrong product: merging across a
real scene change to reach the minimum length produces a clip that cuts in the middle. The
replacement contract is checked by `_check` on every plan this file builds — no clip spans a
scene change, every clip satisfies both bounds, clips stay inside the source and never overlap —
and **gaps between clips are expected output, not a defect**.
"""

from __future__ import annotations

import pytest

from beatsync_fork import raw_cutter as rc


def _check(plan: rc.SegmentPlan, duration: float, cuts=()) -> rc.SegmentPlan:
    """The quality-first guarantees, re-asserted independently of the planner's own check."""
    for a, b in zip(plan.segments, plan.segments[1:]):
        assert b.start >= a.end - rc.TIME_QUANTUM, "segments overlap"
    for segment in plan.segments:
        assert segment.duration > 0, "zero-length segment"
        assert -rc.TIME_QUANTUM <= segment.start < segment.end <= duration + rc.TIME_QUANTUM, (
            "segment leaves the source")
        assert segment.duration >= plan.min_seconds - 1e-9, (
            f"segment {segment.index} is under the minimum; it should have been skipped")
        assert segment.duration <= plan.max_seconds + 1e-9, "segment is over the maximum"
        for cut in cuts:
            assert not (segment.start + rc.TIME_QUANTUM < cut < segment.end - rc.TIME_QUANTUM), (
                f"segment {segment.index} spans the scene change at {cut}")
    assert [s.index for s in plan.segments] == list(range(1, len(plan.segments) + 1))
    assert plan.exported_duration + plan.skipped_duration == pytest.approx(duration, abs=0.01)
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


def test_a_positive_bound_that_rounds_away_to_zero_is_refused():
    """R1. Times are quantized to whole milliseconds, so 0.0004 arrives positive and leaves as
    0.0. The original check ran BEFORE quantization and let it through, which would have planned
    zero-length clips from a number the operator typed in good faith."""
    for tiny in (0.0004, 0.0001, 1e-9):
        with pytest.raises(rc.RawCutterError) as caught:
            rc.normalize_durations(tiny, 300)
        assert "millisecond" in str(caught.value)
        with pytest.raises(rc.RawCutterError) as caught:
            rc.normalize_durations(0.001, tiny)
        assert "millisecond" in str(caught.value)
    # One millisecond is the smallest value that survives, and it must still be accepted.
    assert rc.normalize_durations(0.001, 0.001) == (0.001, 0.001)


def test_a_bound_that_rounds_away_never_reaches_the_planner():
    for tiny in (0.0004, 1e-9):
        with pytest.raises(rc.RawCutterError):
            rc.plan_segments(60.0, [], tiny, 300)


def test_an_unusable_source_duration_is_refused():
    for bad in (0, -5, None, "abc", float("nan")):
        with pytest.raises(rc.RawCutterError):
            rc.plan_segments(bad, [1, 2, 3])


# =============================================================================================
# 3. The product owner's four worked examples
#
# These are the specification. If the planner ever disagrees with one of them, the planner is
# wrong -- not the example.
# =============================================================================================

def test_a_skipped_shot_is_not_also_reported_as_a_dropped_tail():
    """Caught in the browser smoke. `_divide_shot` reports the whole length as unused when a shot
    is skipped outright, and accumulating that into the tail counter made the report describe the
    same four seconds twice — once truthfully as a skipped shot, and once as "leftover tail
    dropped from the end of long shots", which it never was."""
    plan = rc.plan_segments(14.0, [10.0], 5, 300)
    assert len(plan.skipped_shots) == 1
    assert not any("tail" in note.lower() for note in plan.notes), plan.notes
    assert any("skipped" in note.lower() for note in plan.notes)


def test_a_genuine_leftover_tail_is_still_reported():
    """The positive control: a tail dropped from inside a long shot must still be named."""
    plan = rc.plan_segments(11.0, [], 6, 10)
    assert any("tail" in note.lower() for note in plan.notes), plan.notes


def test_example_A_a_short_tail_after_a_real_cut_is_skipped_not_rebalanced():
    """14s source, genuine cut at 10s, min 5 max 10.

    Export [0,10]; skip [10,14] because it is only 4s. Emphatically NOT 7+7, which would put a
    scene change in the middle of both clips to rescue four seconds nobody asked for.
    """
    plan = _check(rc.plan_segments(14.0, [10.0], 5, 10), 14.0, cuts=[10.0])
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0)]
    assert plan.count == 1
    assert len(plan.skipped_shots) == 1
    assert plan.skipped_shots[0].start == 10.0 and plan.skipped_shots[0].end == 14.0
    assert plan.skipped_shots[0].skip_reason == rc.SKIP_TOO_SHORT
    assert plan.exported_duration == pytest.approx(10.0)
    assert plan.skipped_duration == pytest.approx(4.0)


def test_example_B_a_two_second_tail_after_a_thirty_second_shot_is_skipped():
    """32s source, genuine cut at 30s, min 5 max 30. Export [0,30]; skip [30,32]."""
    plan = _check(rc.plan_segments(32.0, [30.0], 5, 30), 32.0, cuts=[30.0])
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 30.0)]
    assert plan.skipped_duration == pytest.approx(2.0)
    assert not any(s.exported for s in plan.skipped_shots)


def test_example_C_a_continuous_shot_may_be_divided_inside_itself():
    """One continuous 14s shot, min 5 max 10 -> [0,7] and [7,14].

    Legal precisely because both halves come from the SAME take: no scene change is crossed, so
    the division costs nothing in continuity.
    """
    plan = _check(rc.plan_segments(14.0, [], 5, 10), 14.0)
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 7.0), (7.0, 14.0)]
    assert all(s.shot_index == 1 for s in plan.segments)
    assert plan.skipped_duration == pytest.approx(0.0)


def test_example_D_a_four_second_source_exports_nothing_and_says_so():
    """One 4s shot against a 5s minimum: export nothing, report the skip, create no clip."""
    plan = _check(rc.plan_segments(4.0, [], 5, 300), 4.0)
    assert plan.count == 0
    assert plan.has_work is False
    assert len(plan.skipped_shots) == 1
    assert plan.skipped_shots[0].skip_reason == rc.SKIP_TOO_SHORT
    assert plan.skipped_duration == pytest.approx(4.0)
    assert any("nothing" in note.lower() for note in plan.notes)


# =============================================================================================
# 4. Shots are never merged across a confirmed boundary
# =============================================================================================

def test_adjacent_short_scenes_are_skipped_rather_than_combined():
    """The single most important behaviour change. Four 3-second shots against a 5s minimum used
    to become one 12-second clip containing three scene changes."""
    plan = _check(rc.plan_segments(12.0, [3.0, 6.0, 9.0], 5, 300), 12.0, cuts=[3.0, 6.0, 9.0])
    assert plan.count == 0, "short shots must not be glued together"
    assert len(plan.skipped_shots) == 4
    assert plan.skipped_duration == pytest.approx(12.0)


def test_a_burst_of_rapid_cuts_is_skipped_not_merged():
    rapid = [10.0, 10.5, 11.0, 11.5, 12.0, 12.5, 13.0]
    plan = _check(rc.plan_segments(40.0, rapid, 5, 300), 40.0, cuts=rapid)
    # [0,10] and [13,40] are long enough; everything between is sub-second and is dropped.
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0), (13.0, 40.0)]
    assert plan.skipped_duration == pytest.approx(3.0)


def test_a_good_shot_between_two_bad_ones_is_still_exported():
    """Skipping must be per-shot, not contagious."""
    plan = _check(rc.plan_segments(30.0, [2.0, 22.0], 5, 300), 30.0, cuts=[2.0, 22.0])
    assert [(s.start, s.end) for s in plan.segments] == [(2.0, 22.0), (22.0, 30.0)]
    assert len(plan.skipped_shots) == 1


def test_no_exported_clip_ever_spans_a_scene_change_across_many_shapes():
    """The structural guarantee, swept rather than spot-checked."""
    for cuts in ([10.0], [3.0, 7.0], [5.0, 50.0, 51.0], [1.0, 2.0, 3.0, 40.0],
                 [15.0, 30.0, 45.0, 60.0]):
        for low, high in ((5, 300), (2, 10), (1, 4), (8, 20)):
            plan = rc.plan_segments(70.0, cuts, low, high)
            _check(plan, 70.0, cuts=cuts)


# =============================================================================================
# 5. Dividing one long shot
# =============================================================================================

def test_a_long_continuous_shot_is_divided_evenly_when_that_clears_the_minimum():
    plan = _check(rc.plan_segments(300.0, [], 5, 60), 300.0)
    assert plan.count == 5
    assert all(s.duration == pytest.approx(60.0) for s in plan.segments)
    assert plan.skipped_duration == pytest.approx(0.0)


def test_an_even_split_is_preferred_over_a_ragged_tail():
    """22s at max 10 could be 10+10+2; an even 7.33x3 reads better and wastes nothing."""
    plan = _check(rc.plan_segments(22.0, [], 5, 10), 22.0)
    assert plan.count == 3
    assert plan.skipped_duration == pytest.approx(0.0)
    assert max(s.duration for s in plan.segments) <= 10.0 + 1e-9


def test_a_tail_that_cannot_clear_the_minimum_is_dropped_not_exported():
    """11s at min 6 max 10: an even split would be 5.5+5.5, both under the minimum. So one
    10-second clip is written and the 1-second tail is dropped."""
    plan = _check(rc.plan_segments(11.0, [], 6, 10), 11.0)
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0)]
    assert plan.skipped_duration == pytest.approx(1.0)
    assert any("tail" in note.lower() for note in plan.notes)


def test_every_piece_of_a_divided_shot_belongs_to_that_shot():
    plan = _check(rc.plan_segments(100.0, [40.0], 5, 15), 100.0, cuts=[40.0])
    first = [s for s in plan.segments if s.shot_index == 1]
    second = [s for s in plan.segments if s.shot_index == 2]
    assert first and second
    assert all(s.end <= 40.0 + 1e-9 for s in first)
    assert all(s.start >= 40.0 - 1e-9 for s in second)


def test_no_segment_exceeds_the_maximum_or_undercuts_the_minimum():
    for duration in (61.0, 100.0, 301.0, 1000.0, 3600.0):
        for low, high in ((5, 10), (5, 60), (30, 300), (1, 2)):
            plan = rc.plan_segments(duration, [], low, high)
            _check(plan, duration)


# =============================================================================================
# 6. Scene-time hygiene, and sources with nothing usable
# =============================================================================================

def test_well_spaced_scene_changes_become_one_clip_each():
    plan = _check(rc.plan_segments(60.0, [15.0, 30.0, 45.0], 5, 300), 60.0,
                  cuts=[15.0, 30.0, 45.0])
    assert [(s.start, s.end) for s in plan.segments] == \
        [(0.0, 15.0), (15.0, 30.0), (30.0, 45.0), (45.0, 60.0)]
    assert plan.detected_scene_count == 3
    assert plan.skipped_duration == pytest.approx(0.0)


def test_scene_times_outside_the_source_are_ignored():
    plan = _check(rc.plan_segments(30.0, [-5.0, 0.0, 10.0, 30.0, 99.0], 5, 300), 30.0,
                  cuts=[10.0])
    assert plan.detected_scene_count == 1


def test_duplicate_and_unordered_scene_times_are_handled():
    a = rc.plan_segments(60.0, [45.0, 15.0, 30.0, 15.0], 5, 300)
    b = rc.plan_segments(60.0, [15.0, 30.0, 45.0], 5, 300)
    assert [(s.start, s.end) for s in a.segments] == [(s.start, s.end) for s in b.segments]


@pytest.mark.parametrize("junk", [None, "abc", [], {}, float("nan"), float("inf")])
def test_a_malformed_scene_time_is_ignored_rather_than_fatal(junk):
    """The detector is an estimate; one bad timestamp must not cost the operator their plan."""
    plan = _check(rc.plan_segments(60.0, [15.0, junk, 45.0], 5, 300), 60.0, cuts=[15.0, 45.0])
    assert plan.detected_scene_count == 2


def test_a_source_with_no_scene_changes_is_one_shot():
    plan = _check(rc.plan_segments(120.0, [], 5, 300), 120.0)
    assert plan.count == 1
    assert len(plan.shots) == 1
    assert any("one continuous shot" in note for note in plan.notes)


def test_a_source_with_no_usable_footage_is_a_valid_empty_plan():
    """Not an exception, not an error -- a truthful answer the caller then declines to execute."""
    plan = _check(rc.plan_segments(2.0, [0.5, 1.0, 1.5], 5, 300), 2.0, cuts=[0.5, 1.0, 1.5])
    assert plan.count == 0
    assert plan.has_work is False
    assert plan.skipped_duration == pytest.approx(2.0)


# =============================================================================================
# 7. Determinism and reporting
# =============================================================================================

def test_identical_inputs_produce_an_identical_plan():
    scenes = [3.2, 9.9, 10.1, 44.0, 44.5, 90.0, 180.0]
    first = rc.plan_segments(240.0, scenes, 5, 60, 50)
    for _ in range(5):
        again = rc.plan_segments(240.0, list(scenes), 5, 60, 50)
        assert [(s.start, s.end, s.boundary_kind) for s in again.segments] == \
               [(s.start, s.end, s.boundary_kind) for s in first.segments]
        assert [(s.index, s.exported) for s in again.shots] == \
               [(s.index, s.exported) for s in first.shots]


def test_the_plan_is_independent_of_the_order_scene_times_arrive_in():
    scenes = [90.0, 3.2, 180.0, 44.0]
    a = rc.plan_segments(240.0, scenes, 5, 60)
    b = rc.plan_segments(240.0, sorted(scenes, reverse=True), 5, 60)
    assert [(s.start, s.end) for s in a.segments] == [(s.start, s.end) for s in b.segments]


def test_the_exported_and_skipped_durations_account_for_the_whole_source():
    """Gaps are allowed, but they must be REPORTED -- the two numbers have to add up."""
    for cuts in ([], [10.0], [3.0, 7.0, 40.0], [1.0, 2.0]):
        plan = rc.plan_segments(50.0, cuts, 5, 20)
        assert plan.exported_duration + plan.skipped_duration == pytest.approx(50.0, abs=0.01)


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


def test_the_summary_reports_kept_and_skipped_and_never_claims_full_coverage():
    """Replaces the coverage-era assertion. The old summary promised "no gaps or overlaps" over
    the whole source, which is now the opposite of the product: gaps are the feature."""
    source = rc.SourceInfo(path=r"C:\a.mkv", duration=60.0, width=1920, height=1080, fps=30.0)
    text = rc.plan_summary_text(rc.plan_segments(60.0, [15.0], 5, 300), source)
    assert "Nothing has been written yet" in text
    assert "EXPORT" in text and "SKIP" in text
    assert "1920x1080" in text
    for forbidden in ("no gaps", "whole source", "every frame is", "fully preserved"):
        assert forbidden not in text, f"the summary still claims {forbidden!r}"


def test_the_summary_explains_a_skip_rather_than_presenting_it_as_an_error():
    source = rc.SourceInfo(path=r"C:\a.mkv", duration=14.0)
    text = rc.plan_summary_text(rc.plan_segments(14.0, [10.0], 5, 10), source)
    assert "SKIP" in text
    assert "intentional" in text.lower()
    for forbidden in ("error", "failed", "warning"):
        assert forbidden not in text.lower(), f"a skip is reported as {forbidden!r}"


def test_the_summary_of_an_empty_plan_says_cut_will_decline():
    source = rc.SourceInfo(path=r"C:\a.mkv", duration=4.0)
    text = rc.plan_summary_text(rc.plan_segments(4.0, [], 5, 300), source)
    assert "nothing to export" in text.lower()
    assert "decline" in text.lower()


def test_the_table_has_one_row_per_shot_showing_kept_and_skipped():
    """Shots, not segments: a skipped shot produces no segment, so a segment table could not
    show the very thing the new policy is about."""
    plan = rc.plan_segments(14.0, [10.0], 5, 10)
    rows = rc.plan_table_rows(plan)
    assert len(rows) == len(plan.shots) == 2
    assert rows[0][4].startswith("export")
    assert rows[1][4].startswith("skip")
    assert "shorter than the minimum" in rows[1][4]
    assert all(len(row) == len(rc.PLAN_TABLE_HEADERS) for row in rows)


def test_a_divided_shot_says_so_on_its_row():
    plan = rc.plan_segments(14.0, [], 5, 10)
    rows = rc.plan_table_rows(plan)
    assert len(rows) == 1
    assert "2 parts" in rows[0][4]


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
