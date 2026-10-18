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
    guard = rc.SCENE_GUARD_SECONDS
    plan = _check(rc.plan_segments(14.0, [10.0], 5, 10), 14.0, cuts=[10.0])
    # [FORK] P6: the SHAPE of the answer is the specification and is unchanged -- one clip, one
    # skipped tail, nothing rebalanced across the cut. The guard moves both boundaries away from
    # the cut by a fixed amount, so the numbers are written against it rather than re-hardcoded.
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0 - guard)]
    assert plan.count == 1
    assert len(plan.skipped_shots) == 1
    assert plan.skipped_shots[0].start == 10.0 + guard and plan.skipped_shots[0].end == 14.0
    assert plan.skipped_shots[0].skip_reason == rc.SKIP_TOO_SHORT
    assert plan.exported_duration == pytest.approx(10.0 - guard)
    assert plan.skipped_duration == pytest.approx(4.0 + guard)
    assert plan.guard_discarded == pytest.approx(2 * guard)


def test_example_B_a_two_second_tail_after_a_thirty_second_shot_is_skipped():
    """32s source, genuine cut at 30s, min 5 max 30. Export [0,30]; skip [30,32]."""
    guard = rc.SCENE_GUARD_SECONDS
    plan = _check(rc.plan_segments(32.0, [30.0], 5, 30), 32.0, cuts=[30.0])
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 30.0 - guard)]
    assert plan.skipped_duration == pytest.approx(2.0 + guard)
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
    # [FORK] P6: each of the six interior shots is 0.5 s detected and 0.1 s after its two guards,
    # so they are still skipped for the same reason -- and the two keepers stand back from the
    # outermost cuts.
    guard = rc.SCENE_GUARD_SECONDS
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0 - guard),
                                                         (13.0 + guard, 40.0)]
    assert plan.skipped_duration == pytest.approx(3.0 + 2 * guard)


def test_a_good_shot_between_two_bad_ones_is_still_exported():
    """Skipping must be per-shot, not contagious."""
    guard = rc.SCENE_GUARD_SECONDS
    plan = _check(rc.plan_segments(30.0, [2.0, 22.0], 5, 300), 30.0, cuts=[2.0, 22.0])
    assert [(s.start, s.end) for s in plan.segments] == [(2.0 + guard, 22.0 - guard),
                                                         (22.0 + guard, 30.0)]
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
    # [FORK] P6: one clip per shot, still -- each standing a guard clear of the cuts that bound
    # it. The source's own start and end are untouched, so only the six interior edges move.
    guard = rc.SCENE_GUARD_SECONDS
    assert [(s.start, s.end) for s in plan.segments] == \
        [(0.0, 15.0 - guard), (15.0 + guard, 30.0 - guard),
         (30.0 + guard, 45.0 - guard), (45.0 + guard, 60.0)]
    assert plan.guard_discarded == pytest.approx(6 * guard)
    assert plan.detected_scene_count == 3
    # [FORK] P6: nothing is skipped as a SHOT -- the only unexported footage is the guard itself,
    # which is the distinction the report has to be able to make.
    assert plan.skipped_shot_duration == pytest.approx(0.0)
    assert plan.dropped_tail_duration == pytest.approx(0.0)
    assert plan.skipped_duration == pytest.approx(6 * guard)


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


# =============================================================================================
# [FORK] Digital-Union (frame-boundary P1): a scene cut must never round FORWARD
#
# The planner's cut points double as the exclusive end of the clip before them. Rounding one up
# by half a millisecond is enough for export's `-t` to keep the first frame of the NEXT shot,
# which the operator sees as a one-frame flash at the end of the clip. These are pure-arithmetic
# guards; `tests/test_raw_cutter_ffmpeg.py` proves the same thing on real decoded frames.
# =============================================================================================

def test_a_scene_cut_is_quantized_downwards_never_past_the_frame_it_stops_before():
    """3.066667 s is frame 92 of a 30 fps source. Rounding it to 3.067 put the boundary one
    millisecond PAST that frame, so export kept it. Flooring stops before it."""
    assert rc._quantize_floor(3.066667) == 3.066
    assert rc._quantize(3.066667) == 3.067, "the round-half quantizer is still the old behaviour"
    assert rc._quantize_floor(3.066667) < 3.066667


@pytest.mark.parametrize("value", [
    0.0005, 1.9999, 3.066667, 3.0333333, 4.999999, 10.000999, 123.456789,
])
def test_flooring_never_moves_a_cut_forward(value):
    assert rc._quantize_floor(value) <= value
    assert value - rc._quantize_floor(value) < rc.TIME_QUANTUM


def test_a_floored_cut_still_keeps_every_frame_of_the_shot_before_it():
    """Flooring must not cost the previous shot its last frame. The margin is under a
    millisecond and a frame is never that short, for any real frame rate."""
    for fps in (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 120.0, 240.0):
        for frame_index in (1, 7, 92, 1000):
            cut = frame_index / fps
            floored = rc._quantize_floor(cut)
            previous_frame = (frame_index - 1) / fps
            assert previous_frame < floored, (fps, frame_index, previous_frame, floored)
            assert floored <= cut


def test_the_segment_before_a_scene_cut_ends_a_full_guard_before_it():
    """[FORK] P6 replaces the old sub-millisecond expectation with the guard-band one.

    Flooring is still what stops the boundary landing past the frame (the assertions below still
    measure it), but it is no longer what keeps the next shot out of the clip -- the guard is. The
    clip now ends a full `SCENE_GUARD_SECONDS` before the floored cut, and the distance is checked
    exactly rather than as "less than a millisecond".
    """
    plan = rc.plan_segments(10.0, [3.066667], min_seconds=1.0, max_seconds=300.0)
    first = plan.segments[0]
    assert first.boundary_kind == rc.SCENE_CUT
    assert first.end < 3.066667, first.end
    assert first.end == pytest.approx(3.066 - rc.SCENE_GUARD_SECONDS, abs=1e-9), first.end
    assert 3.066667 - first.end >= rc.SCENE_GUARD_SECONDS


def test_the_segment_after_a_scene_cut_starts_a_full_guard_past_it():
    """[FORK] P6. **The expectation this replaces is the one the owner's policy reversed.**

    It used to read "the following clip still starts ON that cut", on the reasoning that a
    sub-millisecond floor lands on the same source frame. That is no longer the contract: the
    detected time is an estimate that can be ~100 ms out in either direction, so the following
    clip deliberately starts a full guard past it and the footage in between is discarded.

    The frames in that interval are intentionally lost. What must still hold is that the gap is
    EXACTLY two guards wide -- no more, so nothing is lost that the policy did not ask for, and no
    less, so the margin is really there.
    """
    plan = rc.plan_segments(10.0, [3.066667], min_seconds=1.0, max_seconds=300.0)
    first, second = plan.segments[0], plan.segments[1]
    assert second.start > first.end, "there must be a real gap, not a shared boundary"
    assert second.start - first.end == pytest.approx(2 * rc.SCENE_GUARD_SECONDS, abs=1e-9)
    assert second.start == pytest.approx(3.066 + rc.SCENE_GUARD_SECONDS, abs=1e-9)
    assert second.start - 3.066667 >= rc.SCENE_GUARD_SECONDS - rc.TIME_QUANTUM
    assert plan.guard_discarded == pytest.approx(2 * rc.SCENE_GUARD_SECONDS, abs=1e-9)


def test_adjacent_segments_still_do_not_overlap_after_flooring():
    plan = rc.plan_segments(30.0, [3.066667, 9.0333333, 20.9999], min_seconds=1.0,
                            max_seconds=300.0)
    for before, after in zip(plan.segments, plan.segments[1:]):
        assert after.start >= before.end, (before.end, after.start)


def test_a_time_split_inside_a_continuous_shot_is_unaffected_by_the_floor():
    """Only SCENE boundaries are cut points. An internal time split is arithmetic on the shot and
    must keep tiling it exactly."""
    plan = rc.plan_segments(30.0, [], min_seconds=1.0, max_seconds=10.0)
    assert [s.boundary_kind for s in plan.segments][:-1] == [rc.TIME_CUT] * (plan.count - 1)
    assert plan.segments[0].start == 0.0
    assert plan.segments[-1].end == 30.0
    for before, after in zip(plan.segments, plan.segments[1:]):
        assert after.start == before.end, "a time split must tile, with no gap"


def _export_module_constants(*names):
    """Named top-level constants of the adapter, evaluated without importing the runtime."""
    import ast
    import os
    export = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "raw_cutter_export.py")
    tree = ast.parse(open(export, encoding="utf-8").read())
    namespace = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in names:
                exec(f"{name} = {ast.unparse(node.value)}", namespace)
    missing = set(names) - set(namespace)
    assert not missing, f"the adapter no longer defines {missing}"
    return namespace


def test_the_scene_guard_always_covers_one_coarse_analysis_period():
    """[FORK] frame-boundary P6. **The guard and the analysis rate are coupled, and this is the
    coupling the whole policy rests on.**

    The coarse pass resamples to ``ANALYSIS_FPS`` before scoring, so the time it reports is a label
    on that grid. P3 measured how far the real cut can sit from the label -- the resampler carries
    a source frame LATER than its own grid time by a quarter to a half of a period -- and bounded
    it at one period before the label and half a period after it. A guard shorter than one
    analysis period therefore could not cover the detector's own worst case, and clips would again
    open or close on a frame of the neighbouring shot.

    This pins the RELATIONSHIP rather than the number, exactly as the refinement-window test it
    replaces did: lowering ``ANALYSIS_FPS`` widens the error bound, and the guard has to widen with
    it or the policy quietly stops being conservative.
    """
    period = 1.0 / _export_module_constants("ANALYSIS_FPS")["ANALYSIS_FPS"]
    assert rc.SCENE_GUARD_SECONDS >= period, (
        f"the {rc.SCENE_GUARD_SECONDS}s guard is shorter than the {period}s analysis period, so "
        f"it cannot cover the coarse detector's measured error")

    # The two measured bounds themselves, stated as numbers so a reader can check the margin:
    # the real cut lies in [reported - 0.100, reported + 0.063].
    assert rc.SCENE_GUARD_SECONDS >= 0.100, rc.SCENE_GUARD_SECONDS
    assert rc.SCENE_GUARD_SECONDS >= 0.063, rc.SCENE_GUARD_SECONDS


def test_the_guard_is_applied_at_scene_changes_and_only_there():
    """The distinction the owner made mandatory, on one plan that contains all four boundary kinds.

    A 60 s source with one detected cut at 20 s and a 15 s maximum: the first shot divides into
    time splits and ends on the cut, the second starts after the cut and divides again before
    ending at the source end. Only the two boundaries AT the cut may move.
    """
    plan = rc.plan_segments(60.0, [20.0], min_seconds=1.0, max_seconds=15.0)
    g = rc.SCENE_GUARD_SECONDS

    # Start of source: untouched.
    assert plan.segments[0].start == 0.0, "the start of the source is not an estimate"
    # End of source: untouched.
    assert plan.segments[-1].end == 60.0, "the end of the source is not an estimate"

    before = [s for s in plan.segments if s.end <= 20.0]
    after = [s for s in plan.segments if s.start >= 20.0 - g]
    assert before and after
    assert before[-1].end == pytest.approx(20.0 - g, abs=1e-9), before[-1].end
    assert after[0].start == pytest.approx(20.0 + g, abs=1e-9), after[0].start

    # Every INTERIOR time split tiles exactly -- no guard, no gap, not one millisecond.
    for first, second in zip(before, before[1:]):
        assert second.start == first.end, "a time split inside a shot must not lose a frame"
    for first, second in zip(after, after[1:]):
        assert second.start == first.end, "a time split inside a shot must not lose a frame"


def test_a_continuous_shot_divided_by_time_alone_loses_nothing_at_all():
    """The owner's worked example: 30 s uninterrupted, three 10 s clips, no loss at 10 s or 20 s."""
    plan = rc.plan_segments(30.0, [], min_seconds=1.0, max_seconds=10.0)
    assert [(s.start, s.end) for s in plan.segments] == [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)]
    assert plan.guard_discarded == 0.0, "a time split is not a scene change"
    assert plan.exported_duration == 30.0
    assert plan.skipped_duration == 0.0


def test_no_segment_may_reach_inside_a_guard_band():
    """The structural invariant, asserted against a hand-built plan that violates it.

    `_assert_invariants` runs in production, so this is the thing that would catch a future
    regression before an hour of encoding does -- the same reasoning the span check was written
    under, one step stronger.
    """
    g = rc.SCENE_GUARD_SECONDS
    cut = 10.0
    # A segment that stops a hair INSIDE the band: legal under the old span rule, illegal now.
    intruder = (rc.Segment(index=1, start=0.0, end=cut - g / 2.0),)
    with pytest.raises(rc.RawCutterError, match="safety margin"):
        rc._assert_invariants(intruder, (), 30.0, 1.0, 300.0, [cut], g)
    # The same segment stopped at the band edge is fine.
    ok = (rc.Segment(index=1, start=0.0, end=cut - g),)
    rc._assert_invariants(ok, (), 30.0, 1.0, 300.0, [cut], g)


# 0.001 is the smallest separation that is still TWO cuts: below `TIME_QUANTUM` they quantize onto
# the same millisecond and `_usable_scene_times` correctly reports one, which is a different case.
@pytest.mark.parametrize("separation", [0.001, 0.05, 0.2, 0.39, 0.4, 0.401, 1.0])
def test_cuts_closer_than_two_guards_collapse_deterministically(separation):
    """Overlapping bands must never produce a negative duration, an overlap or an invented frame.

    The shot between two cuts closer than twice the guard has nothing left of it. It is recorded
    as skipped -- so the report names it -- and its neighbours are never merged across either cut
    to recover the footage.
    """
    plan = rc.plan_segments(40.0, [10.0, 10.0 + separation], min_seconds=1.0, max_seconds=300.0)
    assert plan.detected_scene_count == 2, (
        f"the fixture collapsed to {plan.detected_scene_count} cut(s), so it cannot exercise "
        f"overlapping guards")
    for shot in plan.shots:
        assert shot.duration >= 0.0, (shot.index, shot.duration)
        assert shot.start <= shot.end
        assert 0.0 <= shot.start <= 40.0 and 0.0 <= shot.end <= 40.0
    for first, second in zip(plan.segments, plan.segments[1:]):
        assert second.start >= first.end, "guard overlap produced overlapping clips"
    for segment in plan.segments:
        assert segment.duration > 0

    middle = plan.shots[1]
    if separation <= 2 * rc.SCENE_GUARD_SECONDS:
        assert not middle.exported and middle.duration == 0.0, (separation, middle)
    else:
        assert middle.duration == pytest.approx(separation - 2 * rc.SCENE_GUARD_SECONDS, abs=1e-9)


def test_the_three_unexported_categories_always_sum_to_the_total():
    """Guard band, shot too short and leftover tail must be distinguishable AND exhaustive.

    One combined "not exported" number would let a safety margin hide inside a skip, which is the
    conflation the P6 policy explicitly forbids in the report.
    """
    for cuts, low, high in (
            ([10.0], 1.0, 300.0),
            ([10.0, 10.3], 1.0, 300.0),
            ([5.0, 7.0, 25.0], 3.0, 300.0),
            ([], 1.0, 7.0),
            ([12.0], 5.0, 4.5),
    ):
        try:
            plan = rc.plan_segments(40.0, cuts, min_seconds=low, max_seconds=high)
        except rc.RawCutterError:
            continue
        parts = plan.guard_discarded + plan.skipped_shot_duration + plan.dropped_tail_duration
        assert parts == pytest.approx(plan.skipped_duration, abs=0.002), (
            cuts, low, high, plan.guard_discarded, plan.skipped_shot_duration,
            plan.dropped_tail_duration, plan.skipped_duration)


def test_a_guarded_shot_under_the_minimum_is_skipped_not_exported_short():
    """The bounds still mean what they said. A shot that only clears the minimum BEFORE the guard
    is skipped, never exported undersized and never merged across the cut that shortened it."""
    # Detected shot 10.0 -> 15.2 is 5.2 s; guarded it is 4.8 s, under a 5 s minimum.
    plan = rc.plan_segments(40.0, [10.0, 15.2], min_seconds=5.0, max_seconds=300.0)
    middle = plan.shots[1]
    assert not middle.exported and middle.skip_reason == rc.SKIP_TOO_SHORT
    assert middle.detected_duration == pytest.approx(5.2, abs=1e-9)
    assert middle.duration == pytest.approx(4.8, abs=1e-9)
    assert all(s.shot_index != 2 for s in plan.segments), "an undersized clip was exported"
    for segment in plan.segments:
        assert segment.duration >= 5.0 - 1e-9


def test_the_detected_shot_is_recoverable_from_the_guard_fields():
    """Reporting depends on it: the guard is auditable only if the pre-guard interval survives."""
    plan = rc.plan_segments(40.0, [8.0, 20.0], min_seconds=1.0, max_seconds=300.0)
    detected = [(round(s.start - s.guard_before, 3), round(s.end + s.guard_after, 3))
                for s in plan.shots]
    assert detected == [(0.0, 8.0), (8.0, 20.0), (20.0, 40.0)]
    assert [s.guard_before for s in plan.shots] == [0.0, 0.2, 0.2]
    assert [s.guard_after for s in plan.shots] == [0.2, 0.2, 0.0]


def test_the_guard_rounds_away_from_the_cut_never_towards_it():
    """Millisecond quantization may only ever make the margin larger.

    A guarded start rounds up and a guarded end rounds down, so no amount of rounding can put a
    clip edge closer to the detected cut than the guard allows -- which is what lets the invariant
    be arithmetic rather than a tolerance.
    """
    g = rc.SCENE_GUARD_SECONDS
    # A small minimum, so both sides of every cut survive and the rounding is what is measured.
    for cut in (3.0665, 5.0005, 7.1234, 12.9999, 0.7505, 19.4445):
        plan = rc.plan_segments(40.0, [cut], min_seconds=0.1, max_seconds=300.0)
        floored = rc._quantize_floor(cut)
        ends = [s.end for s in plan.segments if s.end <= floored]
        starts = [s.start for s in plan.segments if s.start >= floored]
        assert ends and starts, (cut, [(s.start, s.end) for s in plan.segments])
        assert max(ends) <= floored - g + 1e-12, (cut, max(ends))
        assert min(starts) >= floored + g - 1e-12, (cut, min(starts))


def test_the_guard_is_not_an_operator_control():
    """It is a fixed product decision, so it must not re-key a plan the way the three real
    planning controls do -- and a negative must clamp rather than invert the policy."""
    source = rc.SourceInfo(path="x.mp4", duration=30.0, size_bytes=10, mtime_ns=20)
    assert rc.plan_signature(source, 50, 5.0, 300.0) == rc.plan_signature(source, 50, 5.0, 300.0)
    assert rc._resolve_guard(None) == rc.SCENE_GUARD_SECONDS
    assert rc._resolve_guard("nonsense") == rc.SCENE_GUARD_SECONDS
    assert rc._resolve_guard(float("nan")) == rc.SCENE_GUARD_SECONDS
    assert rc._resolve_guard(-1.0) == 0.0, "a negative guard would reach TOWARDS the cut"
    assert rc._resolve_guard(0.0) == 0.0


def test_a_zero_guard_reproduces_the_pre_p6_boundaries_exactly():
    """The guard is the only behaviour change: at zero the planner is the P5 planner.

    Worth pinning, because it is what makes the P6 delta reviewable -- any other difference in
    shot selection would show up here.
    """
    cuts = [3.5, 11.25, 11.4, 28.0]
    plan = rc.plan_segments(40.0, cuts, min_seconds=1.0, max_seconds=9.0, guard_seconds=0.0)
    assert plan.guard_discarded == 0.0
    boundaries = [0.0] + rc._usable_scene_times(cuts, 40.0) + [40.0]
    for shot, (start, end) in zip(plan.shots, zip(boundaries, boundaries[1:])):
        assert (shot.start, shot.end) == (start, end), (shot, start, end)
    assert plan.exported_duration + plan.skipped_duration == pytest.approx(40.0, abs=0.002)


# =============================================================================================
# [FORK] Digital-Union (frame-boundary P3, FIX D): flooring must not cost a millisecond that was
# never overshoot.
#
# `value / TIME_QUANTUM` is binary floating point, so for a timestamp that is already an exact
# number of milliseconds the quotient can land just below the integer it should be and a plain
# floor drops a whole millisecond. The error is in the SAFE direction, so it never leaked a frame
# -- what it cost was a shot whose true length was exactly the operator's minimum, which measured
# one millisecond short and was silently skipped.
# =============================================================================================

@pytest.mark.parametrize("value", [
    0.001, 0.043, 0.086, 3.002, 3.006, 3.010, 5.0, 10.004, 128.004, 599.998,
])
def test_an_exact_millisecond_cut_is_left_exactly_where_it_is(value):
    """The property P1's own guard could not see: its parameters were all non-exact values, and
    for 3.002 the subtraction `value - floored` evaluates to 0.0009999999999998899, which slips
    under a `< TIME_QUANTUM` assertion even when a whole millisecond has been lost."""
    assert rc._quantize_floor(value) == pytest.approx(value, abs=1e-12), (
        f"{value} was floored to {rc._quantize_floor(value)}, a millisecond it never overshot")


def test_flooring_an_exact_millisecond_is_idempotent():
    """A boundary that has already been floored must survive being floored again -- the planner
    quantizes a value that may already be quantized."""
    for raw in (3.066667, 1.4333333, 6.0, 0.0335, 12.3456789):
        once = rc._quantize_floor(raw)
        assert rc._quantize_floor(once) == pytest.approx(once, abs=1e-12), (raw, once)


def test_the_floor_tolerance_cannot_push_a_boundary_onto_a_later_frame():
    """The tolerance is applied to the QUOTIENT, so it is a picosecond of video -- far below any
    real timestamp's resolution, and far above the division's own representation error."""
    for fps in (23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0, 120.0, 240.0):
        for frame_index in (1, 7, 92, 150, 1000, 2999):
            cut = frame_index / fps
            floored = rc._quantize_floor(cut)
            assert floored <= cut + 1e-12, (fps, frame_index, floored, cut)
            assert floored > (frame_index - 1) / fps, (fps, frame_index, floored)


def test_a_shot_of_exactly_the_minimum_length_still_exports():
    """[FORK] P3 FIX D. The one consequence the representation error actually had.

    An interior shot whose true length is exactly the minimum measured a millisecond short once
    both of its boundaries were floored, so the planner skipped it. Measured on P1: 1.1 % to 3.1 %
    of frame-aligned positions at 25/30/60 fps; with exact flooring it is 0 %.
    """
    for fps in (25.0, 30.0, 60.0):
        for minimum in (3.0, 5.0):
            shot = int(round(minimum * fps))
            for start_frame in range(0, 400):
                first = start_frame / fps
                second = (start_frame + shot) / fps
                measured = rc._quantize_floor(second) - rc._quantize_floor(first)
                assert measured >= minimum - 1e-9, (
                    f"{minimum}s shot at {fps} fps from frame {start_frame} measured "
                    f"{measured}, so the planner would skip footage that qualifies")


def test_a_minimum_length_shot_survives_the_whole_planner():
    """The same property through `plan_segments`, not just the quantizer.

    [FORK] P6: the shot is sized so it is exactly the minimum AFTER the guard, which is what this
    test has always been about -- a one-millisecond representation error in the flooring must not
    make a qualifying shot measure short and be skipped. The guard is a separate, deliberate and
    much larger subtraction, so the fixture allows for it rather than pretending it is absent;
    `test_a_guarded_shot_under_the_minimum_is_skipped_not_exported_short` pins the other side.
    """
    second_cut = round(5.004 + 2 * rc.SCENE_GUARD_SECONDS, 3)
    plan = rc.plan_segments(60.0, [0.004, second_cut], min_seconds=5.0, max_seconds=300.0)
    kinds = [s.boundary_kind for s in plan.segments]
    assert rc.SCENE_CUT in kinds, (
        f"the 5.000 s guarded shot between 0.004 and {second_cut} was dropped: "
        f"{[(s.start, s.end, s.boundary_kind) for s in plan.segments]}")
    scene = next(s for s in plan.segments if s.boundary_kind == rc.SCENE_CUT)
    assert scene.end - scene.start >= 5.0 - 1e-9, (scene.start, scene.end)


def test_the_maximum_and_time_splits_are_unaffected_by_the_floor_tolerance():
    plan = rc.plan_segments(30.0, [], min_seconds=1.0, max_seconds=10.0)
    assert plan.segments[0].start == 0.0
    assert plan.segments[-1].end == 30.0
    for before, after in zip(plan.segments, plan.segments[1:]):
        assert after.start == before.end, "a time split must tile, with no gap"
    for segment in plan.segments:
        assert segment.end - segment.start <= 10.0 + 1e-9, (segment.start, segment.end)


@pytest.mark.parametrize("junk", [None, "abc", float("nan"), float("inf"), float("-inf"),
                                  -5.0, -0.0005, 0.0])
def test_negative_and_nonfinite_cuts_are_still_absorbed(junk):
    """The tolerance must not turn a rejected input into an accepted one."""
    assert rc._usable_scene_times([junk], 10.0) == []


# =============================================================================================
# [FORK] P6: the adapter's scene-analysis cost, checked structurally without running FFmpeg.
#
# P1-P5 ran a second native-frame decode per detected cut on top of the coarse pass. The owner's
# policy removed it in favour of a planner-side margin, so the property to pin is no longer "the
# extra decode is correctly bounded" but "there is no extra decode". Both tests read the AST --
# never the file's text -- so this file may describe the machinery it forbids without tripping
# over its own prose.
# =============================================================================================

def _export_tree():
    import ast
    import os
    export = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "raw_cutter_export.py")
    return ast.parse(open(export, encoding="utf-8").read())


def test_scene_analysis_launches_exactly_one_child_per_source():
    """The cost is one FFmpeg process for the whole file, not one plus one per cut.

    Counted on the call graph rather than by timing: every helper reachable from `detect_scenes`
    is inspected, so a per-cut decode could not be reintroduced behind a new name.
    """
    import ast
    tree = _export_tree()
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    seen, pending, launches = set(), ["detect_scenes"], []
    while pending:
        name = pending.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "_run_media_command":
                    launches.append(name)
                else:
                    pending.append(node.func.id)

    assert launches == ["detect_scenes"], (
        f"scene analysis issues media commands from {sorted(set(launches))}; it must issue "
        f"exactly one, from detect_scenes itself")


def test_no_per_cut_analysis_loop_survives_in_scene_detection():
    """No loop over the detected cuts may remain anywhere scene analysis can reach.

    That is the shape the removed design had -- iterate the coarse times, decode a window for each
    -- and it is the shape whose cost scaled with the number of cuts. A loop here is not
    automatically wrong, so this asserts the specific thing that is: a loop whose body reaches a
    subprocess.
    """
    import ast
    tree = _export_tree()
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    seen, pending = set(), ["detect_scenes"]
    while pending:
        name = pending.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
                for inner in ast.walk(node):
                    if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                            and inner.func.id == "_run_media_command"):
                        raise AssertionError(
                            f"{name} runs a media command inside a loop, so its cost scales with "
                            f"the number of detected cuts")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                pending.append(node.func.id)


# =============================================================================================
# [FORK] P5: the EXPORT's own FFmpeg arguments, checked without running FFmpeg.
# =============================================================================================

def _export_builder():
    """`build_segment_command` AST-extracted and executed against a namespace holding nothing but
    the one constant it is allowed to name -- the same trick the seam suite uses, and the reason
    the read guard is written inline rather than as a module constant."""
    import ast
    import os
    export = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "raw_cutter_export.py")
    tree = ast.parse(open(export, encoding="utf-8").read())
    builder = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "build_segment_command")
    namespace = {"FFMPEG_PATH": "ffmpeg.exe"}
    exec(ast.unparse(builder), namespace)
    return namespace["build_segment_command"]


def test_the_export_bounds_the_read_on_the_input_side_and_the_content_by_timestamp():
    """[FORK] P5, closing P4-2 and P4-3.

    An output-side `-t` cannot express the half-open interval a segment means: its effective end
    is `start + duration` except when the first frame decoded sits under half a frame interval
    after `-ss`, where the muxer re-zeroes the clip's timeline and the window slides later by that
    gap. The gap is sub-millisecond for a floored scene cut and most of a frame interval for a
    maximum-duration time split, so no single duration is right for both. The read is therefore
    bounded on the INPUT side and the content by `trim` on the frames' own timestamps.
    """
    build = _export_builder()
    argv = build("in.mp4", "out.mp4", 10.022, 5.011, remove_audio=True, encoder="libx264",
                 has_audio=False)

    assert argv.index('-t') < argv.index('-i'), (
        "-t must precede -i: as an output option it bounds the content, which is the defect")
    assert argv.index('-ss') < argv.index('-t'), "the seek must precede the read bound"
    read_window = float(argv[argv.index('-t') + 1])
    assert read_window > 5.011, (
        "the input read must reach past the segment's end, or trim cannot see its last frame")

    chain = argv[argv.index('-vf') + 1]
    assert chain == "trim=end=5.011000,setpts=PTS-STARTPTS", chain
    assert "setpts=PTS-STARTPTS" in chain, "the clip's own timeline must start at zero"
    for forbidden in ("scale=", "setsar", "setdar", "fps="):
        assert forbidden not in chain, (
            "%s would change the geometry or rate the export must preserve" % forbidden)
    assert '-s' not in argv and '-r' not in argv


def test_the_export_asks_for_the_planned_duration_and_shaves_nothing_off_it():
    """[FORK] P5. P3 passed `duration - TIME_QUANTUM`, which dropped a real frame whenever one sat
    inside that millisecond below an interior time boundary. The planned length is now what is
    asked for, and `trim` decides membership."""
    build = _export_builder()
    for duration in (5.011, 6.517, 7.967, 0.04, 300.0):
        argv = build("in.mp4", "out.mp4", 1.234, duration, remove_audio=True,
                     encoder="libx264", has_audio=False)
        chain = argv[argv.index('-vf') + 1]
        assert chain.startswith("trim=end=%.6f," % duration), (duration, chain)
        assert ("%.6f" % (duration - 0.001)) not in chain, (
            "a shaved duration is back in the export bound: %s" % chain)


def test_keeping_audio_trims_the_audio_on_the_same_bound():
    """[FORK] P5. The video is bounded by `trim`, so the audio has to be bounded the same way or
    the two streams would describe different intervals."""
    build = _export_builder()
    kept = build("in.mp4", "out.mp4", 6.066, 7.967, remove_audio=False, encoder="libx264",
                 has_audio=True)
    assert '-af' in kept, "audio kept but never bounded"
    assert kept[kept.index('-af') + 1] == "atrim=end=7.967000,asetpts=PTS-STARTPTS"
    assert 'aac' in kept

    stripped = build("in.mp4", "out.mp4", 6.066, 7.967, remove_audio=True, encoder="libx264",
                     has_audio=True)
    assert '-af' not in stripped and '-an' in stripped
    silent = build("in.mp4", "out.mp4", 6.066, 7.967, remove_audio=False, encoder="libx264",
                   has_audio=False)
    assert '-af' not in silent and '-an' in silent
