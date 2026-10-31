"""FUN R10 — Repeat Music 1x..30x.

The owner asked for one thing: play the song up to thirty times and render a full-length music
video whose later repetitions contain **newly planned shots**. Concatenating thirty copies of one
render is explicitly not acceptable.

So this suite is organised around the claims that make that true rather than around the files that
implement it:

1.  the control normalises like every other control in this repository;
2.  the repeated geometry is exact and deterministic at 30x, not merely close;
3.  the timeline extension preserves **index alignment** with the beat grid, closes every cycle
    boundary, and leaves alone everything it has no business touching;
4.  the extended bus still satisfies the two **real** pure consumers — `smart_mix.project_structure`
    and `audio_mix.project_sections`/`project_beat_times` — so Audio Layers and Smart Mix place
    against the full timeline rather than the first repetition;
5.  the **real** `build_frame_aligned_cut_timeline` quantises the extended timeline with no
    accumulated drift across cycles;
6.  the **real** Stage 6 planner, run over an extended timeline, gives cycle 2 a different visual
    sequence from cycle 1 while staying deterministic for the same seed;
7.  **1x is structurally the legacy render** — every statement the feature adds to `gui.py` sits
    behind a repeat guard, which is asserted over the real AST rather than reasoned about;
8.  the repeat count reaches no cache identity, no Qwen request, no creative recipe and no source
    identity.

Loading techniques are the established ones: the pure fork module imports normally; the Stage 6
planner is loaded by path (numpy, no relative imports); `video_processor`'s frame-lock function and
`ffmpeg_processing`'s frame helper are AST-extracted from the real sources, never reimplemented; and
`music_repeater` — which imports `ffmpeg_processing` and therefore the portable runtime — is reached
by AST inspection plus execution of its dependency-free argv builders.
"""

from __future__ import annotations

import ast
import importlib.util
import os

import pytest

from beatsync_fork import audio_mix as fork_audio_mix
from beatsync_fork import music_repeat as M
from beatsync_fork import smart_mix as fork_smart_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")
_GUI = os.path.join(_SRC, "gui.py")
_PROCESSOR = os.path.join(_SRC, "video_processor.py")
_FFMPEG = os.path.join(_SRC, "ffmpeg_processing.py")
_REPEATER = os.path.join(_SRC, "music_repeater.py")
_PLANNER_PATH = os.path.join(_SRC, "auto_mode", "stage6_av_planner.py")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(_tree(path))
                if isinstance(node, ast.FunctionDef) and node.name == name)


def _body(path: str, name: str) -> str:
    return "\n".join(ast.unparse(statement) for statement in _func(path, name).body)


def _click_kwargs(path: str, button: str) -> dict:
    call = next(node for node in ast.walk(_tree(path))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "click"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == button)
    return {kw.arg: kw.value for kw in call.keywords}


def _click_inputs(path: str, button: str) -> list:
    return [getattr(node, "id", None) for node in _click_kwargs(path, button)["inputs"].elts]


# ===========================================================================
# 1. THE CONTROL
# ===========================================================================


def test_the_range_is_the_owners_range():
    assert (M.MIN_REPEAT, M.MAX_REPEAT, M.DEFAULT_REPEAT) == (1, 30, 1)


@pytest.mark.parametrize("value,expected", [
    (1, 1), (2, 2), (5, 5), (30, 30),
    # clamped, because the range has meaningful ends
    (31, 30), (1000, 30), (0, 1), (-7, 1),
    # whole floats are a slider's natural output
    (5.0, 5), (30.0, 30),
    # fractional is different in kind: not a request for 4
    (4.5, 1), (0.5, 1), (29.9, 1),
    # non-numbers
    (None, 1), ("", 1), ("abc", 1), ([], 1), ({}, 1), (object(), 1),
    # plain decimal strings, including a negative one, mirror `normalize_control`
    ("7", 7), ("40", 30), ("-3", 1), ("  12  ", 12), ("12.0", 1),
    # NaN and both infinities: `is_integer()` is False for all three
    (float("nan"), 1), (float("inf"), 1), (float("-inf"), 1),
])
def test_normalize_repeat_is_total(value, expected):
    assert M.normalize_repeat(value) == expected


def test_bool_is_rejected_before_int():
    """`True` would otherwise read as 1x (harmlessly) and `False` as 0x, which is not a render."""
    assert M.normalize_repeat(True) == M.DEFAULT_REPEAT
    assert M.normalize_repeat(False) == M.DEFAULT_REPEAT


@pytest.mark.parametrize("value,repeated", [
    (1, False), (1.0, False), (True, False), (None, False), ("nonsense", False),
    (2, True), (30, True), (31, True), ("5", True),
])
def test_is_repeated_answers_only_for_a_genuine_repeat(value, repeated):
    assert M.is_repeated(value) is repeated


# ===========================================================================
# 2. GEOMETRY — exact at 30x, not merely close
# ===========================================================================


def test_cycle_offsets_are_multiples_never_accumulated_sums():
    """A 30x render's last cycle must start at exactly 29 * D, with one rounding step, not 29."""
    duration = 0.1  # deliberately not representable in binary
    offsets = M.cycle_offsets(30, duration)

    assert len(offsets) == 30
    assert offsets[0] == 0.0
    for index, value in enumerate(offsets):
        assert value == index * duration

    # The accumulated alternative really does differ, so this is a measurement rather than taste.
    accumulated = 0.0
    for _ in range(29):
        accumulated += duration
    assert accumulated != offsets[29]


def test_offsets_and_total_degrade_honestly_on_an_unusable_cycle():
    for bad in (None, 0.0, -3.0, float("nan"), float("inf"), "x"):
        assert M.cycle_offsets(5, bad) == (0.0,)
        assert M.total_seconds(5, bad) == 0.0


def test_total_seconds_is_the_repeat_times_the_cycle():
    assert M.total_seconds(5, 210.0) == pytest.approx(1050.0)
    assert M.total_seconds(1, 210.0) == pytest.approx(210.0)
    assert M.total_seconds(30, 180.0) == pytest.approx(5400.0)
    # a malformed count still produces a usable length rather than raising
    assert M.total_seconds("nonsense", 180.0) == pytest.approx(180.0)


def test_temp_audio_bytes_matches_the_documented_master_format():
    """48 kHz x stereo x 24-bit = 288000 B/s, which is what the read-out's warning quotes."""
    assert M.TEMP_AUDIO_BYTES_PER_SECOND == 48_000 * 2 * 3
    assert M.temp_audio_bytes(1.0) == 288_000
    assert M.temp_audio_bytes(5400.0) == 1_555_200_000
    for bad in (None, 0, -1, float("nan"), "x"):
        assert M.temp_audio_bytes(bad) == 0


def test_format_bytes_is_decimal_and_total():
    assert M.format_bytes(1_555_200_000) == "1.6 GB"
    assert M.format_bytes(52_000_000) == "52 MB"
    for bad in (None, "x", float("nan"), -1):
        assert M.format_bytes(bad) == "--"


# ===========================================================================
# 3. THE PRE-RENDER READ-OUT
# ===========================================================================


def test_the_estimate_shows_the_owners_arithmetic():
    """The example from the request: 03:30 x 5 = 17:30."""
    text = M.estimate_markdown(210.0, 5)
    assert "03:30" in text
    assert "x 5" in text
    assert "17:30" in text


def test_the_estimate_says_so_when_the_music_length_is_unknown():
    for unknown in (None, 0.0, -1.0, float("nan"), "x"):
        text = M.estimate_markdown(unknown, 5)
        assert "load a music track" in text
        # and invents no output figure at all
        assert "=" not in text


def test_the_estimate_describes_1x_as_playing_once():
    text = M.estimate_markdown(210.0, 1)
    assert "x 1" in text and "no repeat" in text
    assert "⚠️" not in text


def test_a_long_render_warns_about_length_and_temporary_disk():
    quiet = M.estimate_markdown(210.0, 2)          # 07:00 — under the threshold
    loud = M.estimate_markdown(210.0, 30)          # 1:45:00 — well over it
    assert "⚠️" not in quiet
    assert "⚠️" in loud
    assert "GB" in loud
    assert "1:45:00" in loud


def test_the_warning_threshold_is_the_published_constant():
    assert M.LONG_OUTPUT_WARNING_SECONDS == 15 * 60
    below = M.estimate_markdown(M.LONG_OUTPUT_WARNING_SECONDS / 2.0 - 1.0, 2)
    at = M.estimate_markdown(M.LONG_OUTPUT_WARNING_SECONDS / 2.0, 2)
    assert "⚠️" not in below
    assert "⚠️" in at


def test_the_read_out_never_raises_on_anything():
    for duration in (None, "x", -5, float("inf"), 210.0):
        for count in (None, "x", True, 0, 1, 7, 99, 4.5):
            assert isinstance(M.estimate_markdown(duration, count), str)


# ===========================================================================
# 4. TILING THE TIME-DEPENDENT DATA
# ===========================================================================


def test_extend_times_restamps_each_cycle():
    times = (0.5, 1.0, 1.5)
    extended = M.extend_times(times, 3, 2.0)
    assert extended == (0.5, 1.0, 1.5, 2.5, 3.0, 3.5, 4.5, 5.0, 5.5)


def test_extend_times_is_ascending_for_every_count_up_to_the_maximum():
    times = tuple(index * 0.5 for index in range(1, 9))     # 0.5 .. 4.0, cycle length 4.25
    for count in range(1, M.MAX_REPEAT + 1):
        extended = M.extend_times(times, count, 4.25)
        assert len(extended) == len(times) * count
        assert list(extended) == sorted(extended)


def test_extend_times_degrades_to_empty_rather_than_guessing():
    for bad in (None, "", 5, object()):
        assert M.extend_times(bad, 4, 2.0) == ()


def test_extend_series_tiles_only_what_is_already_beat_aligned():
    aligned = [0.1, 0.2, 0.3]
    assert M.extend_series(aligned, 2, 3) == (0.1, 0.2, 0.3, 0.1, 0.2, 0.3)
    # a frame-rate curve is NOT tiled -- it is handed back as the same object
    curve = [0.0] * 97
    assert M.extend_series(curve, 2, 3) is curve


def test_extend_series_preserves_value_types_exactly():
    flags = [True, False, True]
    tiled = M.extend_series(flags, 2, 3)
    assert tiled == (True, False, True, True, False, True)
    assert all(isinstance(value, bool) for value in tiled)

    labels = ["peak", "low", "medium"]
    assert M.extend_series(labels, 2, 3) == ("peak", "low", "medium", "peak", "low", "medium")


def test_extend_series_at_1x_returns_the_caller_s_own_object():
    """1x must not rebuild an equal container: the legacy path hands on what it was given."""
    values = [1.0, 2.0]
    assert M.extend_series(values, 1, 2) is values


def test_extend_sections_shifts_reindexes_and_closes_every_cycle():
    sections = [
        {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "type": "intro"},
        # Stage 3's last section ends at the ANALYSIS window, a hair short of the rendered cycle.
        {"index": 1, "start": 4.0, "end": 9.998, "duration": 5.998, "type": "outro",
         "dominant_pattern": "sparse"},
    ]
    extended = M.extend_sections(sections, 3, 10.0)

    assert [s["index"] for s in extended] == [0, 1, 2, 3, 4, 5]
    assert [s["type"] for s in extended] == ["intro", "outro"] * 3
    # cycle c spans exactly [c*10, (c+1)*10) -- no hole at a boundary for `_section_at` to fall
    # through, which would otherwise resolve to the FINAL cycle's final section.
    assert extended[0]["start"] == 0.0
    assert extended[1]["end"] == 10.0
    assert extended[2]["start"] == 10.0
    assert extended[3]["end"] == 20.0
    assert extended[4]["start"] == 20.0
    assert extended[5]["end"] == 30.0
    for section in extended:
        assert section["duration"] == pytest.approx(section["end"] - section["start"])
    # interior boundaries are not moved
    assert extended[2]["end"] == pytest.approx(14.0)
    # payload the extension has no business touching rides along
    assert extended[5]["dominant_pattern"] == "sparse"


def test_extend_sections_leaves_no_gap_between_consecutive_sections():
    sections = [
        {"index": 0, "start": 0.0, "end": 3.0, "type": "intro"},
        {"index": 1, "start": 3.0, "end": 7.0, "type": "verse"},
        {"index": 2, "start": 7.0, "end": 9.97, "type": "outro"},
    ]
    extended = M.extend_sections(sections, 5, 10.0)
    assert len(extended) == 15
    for previous, following in zip(extended, extended[1:]):
        assert following["start"] == pytest.approx(previous["end"])
    assert extended[-1]["end"] == pytest.approx(50.0)


def test_extend_sections_skips_malformed_entries_like_the_existing_projections():
    sections = [
        {"index": 0, "start": 0.0, "end": 5.0, "type": "intro"},
        "not a section",
        {"index": 1, "start": 5.0},                       # no end
        {"index": 2, "start": 7.0, "end": 7.0},            # empty
        {"index": 3, "start": 5.0, "end": 10.0, "type": "outro"},
    ]
    extended = M.extend_sections(sections, 2, 10.0)
    assert [s["type"] for s in extended] == ["intro", "outro", "intro", "outro"]


def test_extend_sections_degrades_without_a_cycle_length():
    sections = [{"index": 0, "start": 0.0, "end": 5.0, "type": "intro"}]
    assert M.extend_sections(sections, 4, None) == sections
    assert M.extend_sections(None, 4, 10.0) == []


def test_extend_selection_info_is_tiled_and_repointed():
    sections = M.extend_sections(
        [{"index": 0, "start": 0.0, "end": 5.0, "type": "intro"},
         {"index": 1, "start": 5.0, "end": 10.0, "type": "outro"}], 2, 10.0)
    info = [{"section": {"index": 0}, "selected_count": 3, "beat_count": 9},
            {"section": {"index": 1}, "selected_count": 4, "beat_count": 10}]

    extended = M.extend_selection_info(info, 2, sections)
    assert len(extended) == 4
    assert [entry["section"]["index"] for entry in extended] == [0, 1, 2, 3]
    assert [entry["selected_count"] for entry in extended] == [3, 4, 3, 4]
    # the originals are untouched
    assert info[0]["section"] == {"index": 0}


def test_extend_selection_info_keeps_entries_it_cannot_repoint():
    extended = M.extend_selection_info(["odd", {"selected_count": 1}], 2, [])
    assert len(extended) == 4


def test_extend_selection_info_matches_by_recorded_index_not_by_position():
    """Stage 4 skips a section with no beats, so the two sequences need not line up — and a
    positional mapping would silently re-point an entry at a neighbouring section."""
    sections = M.extend_sections(
        [{"index": 0, "start": 0.0, "end": 4.0, "type": "intro"},
         {"index": 1, "start": 4.0, "end": 7.0, "type": "verse"},
         {"index": 2, "start": 7.0, "end": 10.0, "type": "outro"}], 2, 10.0)
    # Stage 4 produced no entry for the middle section, so entry 1 describes section INDEX 2
    info = [{"section": {"index": 0}, "selected_count": 3},
            {"section": {"index": 2}, "selected_count": 2}]

    extended = M.extend_selection_info(info, 2, sections)

    assert [entry["section"]["index"] for entry in extended] == [0, 2, 3, 5]
    assert [entry["section"]["type"] for entry in extended] == \
        ["intro", "outro", "intro", "outro"]


def test_extend_selection_info_falls_back_to_position_without_a_usable_index():
    sections = M.extend_sections(
        [{"index": 0, "start": 0.0, "end": 5.0, "type": "intro"},
         {"index": 1, "start": 5.0, "end": 10.0, "type": "outro"}], 2, 10.0)
    for bad in ({"index": True}, {"index": "1"}, {"index": -4}, {}, "not a mapping"):
        info = [{"section": bad, "selected_count": 1}, {"section": bad, "selected_count": 1}]
        extended = M.extend_selection_info(info, 2, sections)
        assert [entry["section"]["index"] for entry in extended] == [0, 1, 2, 3], bad


# ===========================================================================
# 5. THE WHOLE BUS
# ===========================================================================


def _bus(beats: int = 8, cycle: float = 8.0) -> tuple:
    """A realistically-shaped `beat_info` with every key the extension reads or must not touch."""
    times = [index * (cycle / beats) for index in range(beats)]
    selected = [times[0], times[3], times[6]]
    beat_series = [round(0.1 * index, 3) for index in range(beats)]
    flags = [index % 2 == 0 for index in range(beats)]
    beat_info = {
        "times": list(times),
        "selected_times": list(selected),
        "tempo": 123.4,
        "sections": [
            {"index": 0, "start": 0.0, "end": 4.0, "duration": 4.0, "type": "intro",
             "dominant_pattern": "sparse"},
            {"index": 1, "start": 4.0, "end": cycle - 0.002, "duration": cycle - 4.002,
             "type": "drop", "dominant_pattern": "kick"},
        ],
        "energy_profile": {
            "beat_energy": list(beat_series),
            "energy_levels": ["low"] * beats,
            "wave": list(beat_series),
            "arc": list(beat_series),
            # frame-rate curves: deliberately a different length
            "rms": [0.0] * 97,
            "spectral_centroid": [0.0] * 97,
            "zcr": [0.0] * 97,
        },
        "rhythm_data": {key: (list(flags) if key.startswith("is_") else list(beat_series))
                        for key in M.RHYTHM_BEAT_SERIES},
        "rhythm_patterns": {0: "sparse", 1: "kick"},
        "selection_info": [{"section": {"index": 0}, "selected_count": 2},
                           {"section": {"index": 1}, "selected_count": 1}],
        "audio_visual_profile": {"tempo": 123.4, "cut_count": 3, "beat_count": beats,
                                 "smart_preset": "rhythmic_flow_gmv_amv"},
        "video_analysis": {"candidates": [{"id": "c0", "video_file": "a.mp4"}]},
        "audio_duration": cycle - 0.002,
        "mode": "auto_v4_audio_visual_rhythmic_planner",
        "creative": {"seed": 0, "cut_density": 50},
        "freestyle": "a frozen declaration",
    }
    return selected, beat_info


def test_the_bus_extension_rewrites_exactly_the_time_dependent_keys():
    selected, beat_info = _bus()
    new_selected, extended = M.extend_beat_info(selected, beat_info, 4, 8.0)

    assert len(extended["times"]) == 8 * 4
    assert len(new_selected) == 3 * 4
    assert extended["selected_times"] == tuple(new_selected)
    assert extended["audio_duration"] == pytest.approx(32.0)
    assert len(extended["sections"]) == 8
    assert sorted(extended["rhythm_patterns"]) == list(range(8))

    for key in M.ENERGY_BEAT_SERIES:
        assert len(extended["energy_profile"][key]) == 8 * 4, key
    for key in M.RHYTHM_BEAT_SERIES:
        assert len(extended["rhythm_data"][key]) == 8 * 4, key
    for key in M.ENERGY_FRAME_SERIES:
        assert len(extended["energy_profile"][key]) == 97, key


def test_the_bus_extension_leaves_everything_else_alone():
    selected, beat_info = _bus()
    _, extended = M.extend_beat_info(selected, beat_info, 4, 8.0)

    for untouched in ("tempo", "audio_visual_profile", "video_analysis", "mode", "creative",
                     "freestyle"):
        assert extended[untouched] is beat_info[untouched], untouched


def test_the_bus_extension_mutates_nothing_it_was_handed():
    selected, beat_info = _bus()
    before_times = list(beat_info["times"])
    before_sections = [dict(section) for section in beat_info["sections"]]
    before_wave = list(beat_info["energy_profile"]["wave"])

    M.extend_beat_info(selected, beat_info, 7, 8.0)

    assert beat_info["times"] == before_times
    assert beat_info["sections"] == before_sections
    assert beat_info["energy_profile"]["wave"] == before_wave
    assert beat_info["audio_duration"] == pytest.approx(7.998)
    assert M.BUS_KEY not in beat_info


def test_the_bus_extension_is_a_no_op_at_1x_and_on_unusable_input():
    selected, beat_info = _bus()
    for count, cycle in ((1, 8.0), (1.0, 8.0), (True, 8.0), (5, None), (5, 0.0), (5, "x")):
        new_selected, extended = M.extend_beat_info(selected, beat_info, count, cycle)
        assert new_selected is selected
        assert extended is beat_info
    # a foreign bus shape is handed straight back rather than raising
    assert M.extend_beat_info(selected, "not a mapping", 5, 8.0) == (selected, "not a mapping")


def test_the_bus_record_states_what_the_render_did():
    selected, beat_info = _bus()
    _, extended = M.extend_beat_info(selected, beat_info, 5, 8.0)

    record = M.bus_record(extended)
    assert record == {
        "repeat": 5,
        "cycle_seconds": 8.0,
        "total_seconds": 40.0,
        "beats_per_cycle": 8,
        "cuts_per_cycle": 3,
    }
    assert M.bus_record(beat_info) is None
    assert M.bus_record(None) is None
    assert M.bus_record({M.BUS_KEY: "nonsense"}) is None


def test_the_summary_line_reports_only_a_real_repeat():
    selected, beat_info = _bus()
    _, extended = M.extend_beat_info(selected, beat_info, 5, 210.0 / 5)

    line = M.summary_line(extended)
    assert line.startswith("🔁 REPEAT MUSIC: 5x")
    assert "03:30" in line          # the 5x total
    assert M.summary_line(beat_info) == ""
    assert M.summary_line(None) == ""
    assert M.summary_line({M.BUS_KEY: {"repeat": 1, "cycle_seconds": 10.0}}) == ""


def test_the_extension_is_deterministic():
    selected, beat_info = _bus()
    first = M.extend_beat_info(selected, beat_info, 9, 8.0)
    second = M.extend_beat_info(selected, beat_info, 9, 8.0)
    assert first[0] == second[0]
    assert first[1]["times"] == second[1]["times"]
    assert first[1]["sections"] == second[1]["sections"]
    assert first[1]["rhythm_data"] == second[1]["rhythm_data"]


def test_the_extension_holds_at_the_maximum_repeat():
    """30x on a realistic grid: alignment, monotonicity and the closing boundary."""
    selected, beat_info = _bus(beats=400, cycle=180.0)
    new_selected, extended = M.extend_beat_info(selected, beat_info, 30, 180.0)

    assert len(extended["times"]) == 12_000
    assert list(extended["times"]) == sorted(extended["times"])
    assert len(new_selected) == len(selected) * 30
    assert extended["audio_duration"] == pytest.approx(5400.0)
    assert extended["sections"][-1]["end"] == pytest.approx(5400.0)
    for key in M.RHYTHM_BEAT_SERIES:
        assert len(extended["rhythm_data"][key]) == 12_000


# ===========================================================================
# 6. THE EXTENDED BUS STILL SATISFIES THE REAL PURE CONSUMERS
#
# Requirement 7: voice and SFX must be placed against the EXTENDED timeline. Both planners read the
# bus through their own projections, and `project_structure` RAISES on a misaligned array rather
# than zipping to the shorter one -- so these are the tests that prove the tiling preserved index
# alignment, using the production projections rather than a restatement of them.
# ===========================================================================


def test_smart_mix_reads_the_extended_structure_without_misalignment():
    selected, beat_info = _bus(beats=64, cycle=64.0)
    _, extended = M.extend_beat_info(selected, beat_info, 6, 64.0)

    structure = fork_smart_mix.project_structure(extended)

    assert len(structure.beat_times) == 64 * 6
    assert len(structure.impact_strength) == 64 * 6
    assert len(structure.is_bar_anchor) == 64 * 6
    assert len(structure.is_phrase_anchor) == 64 * 6
    assert structure.music_duration == pytest.approx(384.0)
    # the sections reach the end of the LAST repetition, not the end of the first
    assert max(section[1] for section in structure.sections) == pytest.approx(384.0)
    # and a lookup late in the music resolves to a real section rather than falling through
    assert structure.section_index_at(380.0) >= 0


def test_smart_mix_would_have_raised_on_a_half_extended_bus():
    """Proves the test above is not vacuous: misalignment really is detected by the production code."""
    selected, beat_info = _bus(beats=64, cycle=64.0)
    _, extended = M.extend_beat_info(selected, beat_info, 6, 64.0)
    broken = dict(extended)
    broken["rhythm_data"] = dict(extended["rhythm_data"])
    broken["rhythm_data"]["impact_strength"] = beat_info["rhythm_data"]["impact_strength"]

    with pytest.raises(fork_smart_mix.SmartMixStructureError):
        fork_smart_mix.project_structure(broken)


def test_audio_layers_sees_the_whole_repeated_music():
    selected, beat_info = _bus(beats=64, cycle=64.0)
    _, extended = M.extend_beat_info(selected, beat_info, 3, 64.0)

    sections = fork_audio_mix.project_sections(extended["sections"])
    beats = fork_audio_mix.project_beat_times(extended["times"])

    assert len(sections) == 6
    assert sections[-1].end == pytest.approx(192.0)
    assert len(beats) == 64 * 3
    assert beats[-1] < 192.0


def test_a_voice_clip_can_be_placed_beyond_the_first_music_cycle():
    """Requirement 9D, at the level the pure planner owns: a clip that cannot fit inside one cycle
    is placed legally once the music is repeated, and the placement really is past the first cycle.
    """
    selected, beat_info = _bus(beats=64, cycle=64.0)
    _, extended = M.extend_beat_info(selected, beat_info, 5, 64.0)
    sections = fork_audio_mix.project_sections(extended["sections"])
    beats = fork_audio_mix.project_beat_times(extended["times"])
    config = fork_audio_mix.AudioMixConfig(start_delay_seconds=0.0, min_gap_seconds=1.0,
                                           avoid_drops=False)
    voices = tuple(
        fork_audio_mix.VoiceInput(index=index, path=f"{index:02d}.wav", duration=50.0)
        for index in range(5)
    )

    plan = fork_audio_mix.plan_voice_placements(
        music_duration=extended["audio_duration"], beat_times=beats, sections=sections,
        voices=voices, config=config)

    assert not isinstance(plan, fork_audio_mix.PlacementFailure)
    assert len(plan.placements) >= 4
    assert max(p.start for p in plan.placements) > 64.0, "no voice reached a later repetition"
    for placement in plan.placements:
        assert placement.start + placement.duration <= extended["audio_duration"] + 1e-9


# ===========================================================================
# 7. THE REAL FRAME-LOCK, OVER AN EXTENDED TIMELINE
#
# Requirement 6: quantise ABSOLUTE positions against the output FPS and accumulate no drift. The
# production function is AST-extracted rather than reimplemented, so this cannot pass against a
# paraphrase of it.
# ===========================================================================


@pytest.fixture(scope="module")
def frame_lock():
    pytest.importorskip("numpy", reason="the frame-locked timeline is numpy-based")
    import numpy

    namespace = {"np": numpy}
    wanted = {"seconds_to_frame_count": _func(_FFMPEG, "seconds_to_frame_count"),
              "build_frame_aligned_cut_timeline": _func(
                  _PROCESSOR, "build_frame_aligned_cut_timeline")}
    module = ast.Module(body=list(wanted.values()), type_ignores=[])
    exec(compile(module, _PROCESSOR, "exec"), namespace)  # noqa: S102
    return namespace["build_frame_aligned_cut_timeline"]


@pytest.mark.parametrize("fps", [25.0, 30.0, 23.976])
@pytest.mark.parametrize("repeat", [1, 2, 3, 5, 30])
def test_the_extended_timeline_quantises_without_accumulating_drift(frame_lock, fps, repeat):
    import numpy

    cycle = 7.3                                 # not frame-aligned at any of the three rates
    cuts = tuple(round(0.37 * index, 4) for index in range(1, 20) if 0.37 * index < cycle)
    extended = M.extend_times(cuts, repeat, cycle)
    total = M.total_seconds(repeat, cycle)

    cut_times, segment_frames, segment_durations, _dropped = frame_lock(
        numpy.asarray(extended, dtype=float), total, fps)

    # [FORK] Digital-Union (FUN R10 A/V End Alignment): AMENDED. This read `int(round(total * fps))`
    # and asserted the timeline ends exactly there -- which is the behaviour the milestone corrected,
    # not a property to preserve. `cycle = 7.3` is deliberately not frame-aligned at any of the three
    # rates, so `round` lands PAST the audio on many of these combinations, and final assembly's
    # `-shortest` then discarded the overhang. The timeline now stops at the last frame that still
    # ends within the audio, so the expectation is the clamped end rather than the rounded one.
    end_frame = int(round(total * fps))
    if end_frame > 1 and end_frame / fps > total + 1e-9:
        end_frame -= 1
    assert end_frame / fps <= total + 1e-9, "the timeline must never end past the audio"
    # every segment length is a FRAME DIFFERENCE, so the sum is the end frame exactly
    assert int(sum(segment_frames)) == end_frame
    assert len(segment_durations) == len(segment_frames) == len(cut_times) - 1
    # and the absolute positions are the quantised originals -- no cumulative error at cycle 30
    assert cut_times[0] == 0.0
    assert cut_times[-1] == pytest.approx(end_frame / fps)
    for value in cut_times[1:-1]:
        frame = round(value * fps)
        assert abs(value - frame / fps) < 1e-9
    assert list(cut_times) == sorted(cut_times)


def test_a_repeated_timeline_produces_proportionally_more_segments(frame_lock):
    import numpy

    cycle, fps = 7.3, 30.0
    cuts = tuple(round(0.37 * index, 4) for index in range(1, 20) if 0.37 * index < cycle)
    counts = {}
    for repeat in (1, 2, 5):
        extended = numpy.asarray(M.extend_times(cuts, repeat, cycle), dtype=float)
        _, frames, _, _ = frame_lock(extended, M.total_seconds(repeat, cycle), fps)
        counts[repeat] = len(frames)

    assert counts[2] > counts[1]
    assert counts[5] > counts[2]
    # roughly linear: a 5x render plans about five cycles' worth of cuts, never one cycle's
    assert counts[5] >= 4 * counts[1]


# ===========================================================================
# 8. THE REAL STAGE 6 PLANNER OVER AN EXTENDED TIMELINE
#
# Requirements 6 (new shots, not a repeat of cycle 1), I (same seed -> same plan) and J (cycle 2 is
# not a byte-for-byte duplicate of cycle 1 when the library offers enough choices).
# ===========================================================================


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_planner_r10", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate_pool(count: int = 60) -> list:
    return [{
        "id": f"cand_{index:03d}",
        "video_file": f"C:/library/source_{index % 12:02d}.mp4",
        "source_name": f"source_{index % 12:02d}",
        "scene_index": index % 5,
        "duration": 4.0,
        "video_duration": 90.0,
        "start": 5.0 + index,
        "center": 6.0 + index,
        "peak_time": 6.5 + index,
        "quality_score": 0.50 + (index % 9) * 0.02,
        "action_score": 0.40 + (index % 7) * 0.03,
        "beauty_score": 0.35 + (index % 5) * 0.04,
        "tension_score": 0.30 + (index % 11) * 0.02,
        "soft_score": 0.25 + (index % 4) * 0.05,
        "motion": 0.20 + (index % 6) * 0.06,
        "brightness": 0.45 + (index % 3) * 0.05,
        "tags": ["action"] if index % 3 == 0 else ["soft"],
        "ai_analyzed": index % 2 == 0,
    } for index in range(count)]


def _plan_over_repeat(planner, frame_lock, repeat, seed=0, pool=None):
    import numpy

    cycle, fps = 24.0, 30.0
    selected, beat_info = _bus(beats=96, cycle=cycle)
    beat_info = dict(beat_info)
    beat_info["video_analysis"] = {"candidates": pool if pool is not None else _candidate_pool()}
    beat_info["creative"] = {"seed": seed}
    # a realistic cut grid: every fourth beat
    selected = [beat_info["times"][index] for index in range(0, 96, 4)]

    new_selected, extended = M.extend_beat_info(selected, beat_info, repeat, cycle)
    if repeat <= 1:
        new_selected, extended = selected, beat_info
    total = M.total_seconds(repeat, cycle) or (cycle - 0.002)

    cut_times, _frames, durations, _dropped = frame_lock(
        numpy.asarray(list(new_selected), dtype=float), total, fps)
    plan = planner.build_planned_clip_sequence(
        cut_times=cut_times, segment_durations=durations, beat_info=extended,
        video_files=[f"C:/library/source_{index:02d}.mp4" for index in range(12)])
    return plan, cut_times


def test_the_planner_covers_the_whole_extended_timeline(planner, frame_lock):
    """Requirement 6: Stage 6 plans across ALL of 0..N*D, not just the first cycle."""
    plan, cut_times = _plan_over_repeat(planner, frame_lock, 5)

    assert len(plan) == len(cut_times) - 1
    assert plan[-1]["audio_end"] == pytest.approx(cut_times[-1])
    assert plan[-1]["audio_start"] > 4 * 24.0, "the last planned cut is not in the last repetition"
    # and every segment has a real source moment
    assert all(item["video_file"] for item in plan)


def test_later_cycles_are_not_a_duplicate_of_the_first(planner, frame_lock):
    """Requirement J. The MUSICAL cut positions repeat; the visual sequence must not.

    The comparison is over the per-cut `(video_file, candidate_id)` pairs of cycle 1 against cycle 2,
    aligned cut for cut — so a reordering, a shift or a partial change all count as different, and
    only a genuinely identical re-selection would fail this.
    """
    plan, _cut_times = _plan_over_repeat(planner, frame_lock, 3)
    per_cycle = len(plan) // 3
    assert per_cycle >= 20, "the fixture must plan enough cuts for this to mean anything"

    def shots(cycle):
        return [(item["video_file"], item["candidate_id"])
                for item in plan[cycle * per_cycle:(cycle + 1) * per_cycle]]

    first, second, third = shots(0), shots(1), shots(2)
    assert second != first
    assert third != first
    assert third != second
    # and it is not a near-miss either: most of the second cycle's cuts differ
    differing = sum(1 for a, b in zip(first, second) if a != b)
    assert differing >= per_cycle // 2, (differing, per_cycle)


def test_the_same_seed_and_inputs_reproduce_the_same_extended_plan(planner, frame_lock):
    """Requirement I, at every seed branch: legacy argmax (0) and the seeded top-K draw."""
    for seed in (0, 381944):
        first, _ = _plan_over_repeat(planner, frame_lock, 4, seed=seed)
        second, _ = _plan_over_repeat(planner, frame_lock, 4, seed=seed)
        assert [(i["video_file"], i["candidate_id"], i["start_time"]) for i in first] == \
               [(i["video_file"], i["candidate_id"], i["start_time"]) for i in second]


def test_a_repeated_render_keeps_spreading_across_sources(planner, frame_lock):
    """Requirement 6: normal source diversity and repetition avoidance survive the longer timeline."""
    plan, _ = _plan_over_repeat(planner, frame_lock, 4)
    sources = {item["video_file"] for item in plan}
    assert len(sources) >= 8, sources
    # no two ADJACENT cuts come from the same candidate moment
    for previous, following in zip(plan, plan[1:]):
        assert previous["candidate_id"] != following["candidate_id"]


def test_a_one_shot_library_repeats_because_it_has_nothing_else_and_that_is_honest(planner,
                                                                                  frame_lock):
    """The J requirement is conditional — "when the source library offers enough choices".

    With a single candidate there is nothing to vary, so the cycles ARE identical. Pinning that
    explicitly stops the test above from being read as a promise the planner cannot keep, and proves
    the difference it measures comes from the library rather than from randomness.
    """
    pool = [dict(_candidate_pool(1)[0])]
    plan, _ = _plan_over_repeat(planner, frame_lock, 2, pool=pool)
    per_cycle = len(plan) // 2
    assert {item["candidate_id"] for item in plan} == {"cand_000"}
    assert [i["video_file"] for i in plan[:per_cycle]] == \
           [i["video_file"] for i in plan[per_cycle:per_cycle * 2]]


# ===========================================================================
# 9. THE RUNTIME MODULE — argv, structure, and loud failure
# ===========================================================================


@pytest.fixture(scope="module")
def repeater_builders():
    """The three dependency-free argv/list builders, executed out of the real source."""
    wanted = {name: _func(_REPEATER, name)
              for name in ("build_decode_command", "build_concat_command", "build_concat_list")}
    namespace = {
        "FFMPEG_PATH": r"C:\bin\ffmpeg.exe",
        "CYCLE_CODEC": "pcm_s24le",
        "CYCLE_SAMPLE_RATE": 48_000,
        "CYCLE_CHANNELS": 2,
    }
    module = ast.Module(body=list(wanted.values()), type_ignores=[])
    exec(compile(module, _REPEATER, "exec"), namespace)  # noqa: S102
    return namespace


def test_the_decode_argv_takes_the_music_stream_and_the_master_format(repeater_builders):
    argv = repeater_builders["build_decode_command"](r"C:\music\song.mp3", r"C:\tmp\cycle.wav")

    assert argv[0] == r"C:\bin\ffmpeg.exe"
    assert argv[-1] == r"C:\tmp\cycle.wav"
    assert "-nostdin" in argv and "-y" in argv
    # cover art in an MP3 presents a video stream, and an MKV may carry several audio tracks
    assert "-vn" in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert argv[argv.index("-c:a") + 1] == "pcm_s24le"
    assert argv[argv.index("-ar") + 1] == "48000"
    assert argv[argv.index("-ac") + 1] == "2"
    # the input comes before the encoder options -- the V1-A preview defect, not repeated here
    assert argv.index("-i") < argv.index("-c:a")


def test_the_concat_argv_is_a_stream_copy_of_the_list(repeater_builders):
    argv = repeater_builders["build_concat_command"](r"C:\tmp\list.txt", r"C:\tmp\out.wav")
    assert argv[argv.index("-f") + 1] == "concat"
    assert argv[argv.index("-safe") + 1] == "0"
    assert argv[argv.index("-i") + 1] == r"C:\tmp\list.txt"
    assert argv[argv.index("-c") + 1] == "copy"
    assert argv[-1] == r"C:\tmp\out.wav"
    # a re-encode here would requantise every repetition and could introduce a boundary artefact
    for forbidden in ("-c:a", "pcm_s24le", "-ar", "-af"):
        assert forbidden not in argv, forbidden


def test_the_concat_list_names_the_cycle_exactly_once_per_repetition(repeater_builders):
    text = repeater_builders["build_concat_list"](r"C:\tmp\cycle.wav", 5)
    lines = text.splitlines()
    assert len(lines) == 5
    assert all(line == "file 'C:/tmp/cycle.wav'" for line in lines)


def test_the_concat_list_escapes_a_quote_in_the_path(repeater_builders):
    text = repeater_builders["build_concat_list"]("/tmp/o'brien/cycle.wav", 2)
    assert text.splitlines() == ["file '/tmp/o'\\''brien/cycle.wav'"] * 2


def test_the_repeater_verifies_the_arithmetic_against_the_artifact():
    """A master that is not exactly N cycles long would shift every cut after the first repetition,
    so the drift check is a contract rather than a sanity check."""
    body = _body(_REPEATER, "build_repeated_music")
    assert "DURATION_TOLERANCE_SECONDS" in body
    assert "probe_duration(master_path" in body
    assert "total_seconds(count, cycle_seconds)" in body
    assert "ms drift" in body


def test_the_repeater_refuses_1x_rather_than_silently_doing_nothing():
    body = _body(_REPEATER, "build_repeated_music")
    assert "MIN_REPEAT" in body
    assert body.index("MIN_REPEAT") < body.index("started = time.perf_counter()")


def test_a_cancellation_is_never_retyped_as_a_repeat_failure():
    """A user's Stop must not be reported as a problem with their repeat count."""
    node = _func(_REPEATER, "build_repeated_music")
    handlers = [ast.unparse(handler.type) for handler in ast.walk(node)
                if isinstance(handler, ast.ExceptHandler) and handler.type is not None]
    assert "(MusicRepeatError, RenderCancelled)" in handlers
    # that clause re-raises bare, so the TYPE survives it
    for handler in ast.walk(node):
        if (isinstance(handler, ast.ExceptHandler)
                and ast.unparse(handler.type) == "(MusicRepeatError, RenderCancelled)"):
            raises = [n for n in handler.body if isinstance(n, ast.Raise)]
            assert len(raises) == 1 and raises[0].exc is None
            break
    else:
        raise AssertionError("the combined handler was not found")


def test_the_repeater_cleans_up_its_own_intermediates_on_every_path():
    node = _func(_REPEATER, "build_repeated_music")
    finals = [t for t in ast.walk(node) if isinstance(t, ast.Try) and t.finalbody]
    assert finals, "no try/finally"
    text = "\n".join(ast.unparse(statement) for t in finals for statement in t.finalbody)
    assert "discard_repeated_master(cycle_path)" in text
    assert "discard_repeated_master(list_path)" in text


def _executable_source(path: str) -> str:
    """The module's code with every docstring and comment removed.

    Needed because these modules deliberately *name* in prose the things they must never *do* —
    `music_repeater`'s docstring explains why the master is not in `get_processing_dir()` and why it
    does not import `audio_mixdown`'s runner — so a substring check over the raw file would be
    satisfied by the explanation itself. Unparsing the AST drops comments, and the docstrings are
    stripped explicitly.
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_master_never_goes_into_the_processing_directory():
    """`create_music_video` clears `get_processing_dir()` at the start of every render."""
    code = _executable_source(_REPEATER)
    assert "get_processing_dir" not in code
    assert "PROCESSING_DIR" not in code
    assert "session_dir" in _body(_REPEATER, "repeated_master_path")


def test_the_repeat_error_is_its_own_type_not_an_audio_layers_one():
    """The three `except audio_mixdown.AudioMixError` handlers in the render body must keep meaning
    exactly what they mean today: a problem with the operator's voice clips or SFX library."""
    tree = _tree(_REPEATER)
    classes = {node.name: [ast.unparse(base) for base in node.bases]
               for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert classes["MusicRepeatError"] == ["Exception"]
    code = _executable_source(_REPEATER)
    assert "audio_mixdown" not in code
    assert "AudioMixError" not in code


def test_the_repeater_owns_exactly_one_popen():
    """Each runtime module keeps its own small cancellable runner; a handle belongs to its frame."""
    code = _executable_source(_REPEATER)
    assert code.count("subprocess.Popen") == 1
    assert "_run_media_command" not in code
    imported = {alias.name for node in ast.walk(_tree(_REPEATER))
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(_tree(_REPEATER))
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "audio_mixdown" not in imported


def test_the_pure_module_imports_nothing_but_the_stdlib_and_one_sibling():
    """`beatsync_fork` is stdlib-only by rule; the one sibling is reused so there is a single clock
    formatter rather than two that can disagree. (The package-wide guard in
    `test_no_runtime_dependency.py` enforces the rule itself; this records the intent.)"""
    tree = _tree(os.path.join(_SRC, "beatsync_fork", "music_repeat.py"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    assert modules == {"__future__", "typing", "beatsync_fork.audio_mix"}, modules


def test_the_pure_module_performs_no_io_and_reads_no_clock():
    code = _executable_source(os.path.join(_SRC, "beatsync_fork", "music_repeat.py"))
    for forbidden in ("subprocess", "open(", "os.path", "os.makedirs", "numpy", "time.",
                      "random.", "Popen", "unlink", "remove("):
        assert forbidden not in code, forbidden


# ===========================================================================
# 10. THE GUI SEAM — 1x IS STRUCTURALLY THE LEGACY RENDER
# ===========================================================================


REPEAT_GLOBALS = ("music_repeater", "fork_music_repeat")


def test_every_repeat_statement_in_the_render_body_is_behind_a_repeat_guard():
    """**The most important test in R10.**

    "1x changes nothing" must be a property of the code's shape, not of the values flowing through
    it. Every reference to either new module inside `_process_video_impl` has to sit inside an `if`
    whose condition is a repeat guard — `music_repeat > 1` for the work, `repeated_master_path` for
    the cleanup and the panel line — so a 1x render does not resolve either module, build any file,
    rebuild any array or execute one added statement.

    This is also what keeps the established exec-based harnesses valid: several suites run the real
    `_process_video_impl` against a synthesised namespace that contains neither module, and they
    pass precisely because a 1x render never looks them up.
    """
    impl = _func(_GUI, "_process_video_impl")
    allowed_tests = {"music_repeat > 1", "repeated_master_path"}

    guarded: set[int] = set()
    for node in ast.walk(impl):
        if isinstance(node, ast.If) and ast.unparse(node.test) in allowed_tests:
            for inner in ast.walk(node):
                guarded.add(id(inner))

    found = 0
    for node in ast.walk(impl):
        if isinstance(node, ast.Name) and node.id in REPEAT_GLOBALS:
            found += 1
            assert id(node) in guarded, (
                f"{node.id} is referenced outside a repeat guard at line {node.lineno}")
    assert found >= 4, "the repeat block was not found at all"


def test_the_analysis_runs_before_the_repeat_and_is_handed_the_original_music():
    """Stages 1-5 see the operator's own file, once, whatever the repeat count is."""
    source = _body(_GUI, "_process_video_impl")
    analyse = source.index("analyze_beats_auto(")
    repeat = source.index("music_repeater.build_repeated_music(")
    render = source.index("create_music_video(")
    assert analyse < repeat < render

    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "analyze_beats_auto":
            rendered = ast.unparse(node)
            assert ast.unparse(node.args[0]) == "local_audio_path"
            for forbidden in ("music_repeat", "repeat", "repeated_master_path",
                              "music_repeater"):
                assert forbidden not in rendered, rendered
            break
    else:
        raise AssertionError("analyze_beats_auto is not called")


def test_the_repeated_master_is_written_into_the_session_dir_and_discarded_in_the_finally():
    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and "build_repeated_music" in ast.unparse(node.func):
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            assert kwargs["session_dir"] == "session_dir"
            assert kwargs["music_path"] == "local_audio_path"
            assert kwargs["lifecycle"] == "lifecycle"
            break
    else:
        raise AssertionError("build_repeated_music is not called")

    source = _body(_GUI, "_process_video_impl")
    assert source.index("repeated_master_path = None") < source.index("try:")
    finals = [t for t in ast.walk(impl) if isinstance(t, ast.Try) and t.finalbody]
    text = "\n".join(ast.unparse(statement) for t in finals for statement in t.finalbody)
    assert "music_repeater.discard_repeated_master(repeated_master_path)" in text


def test_a_repeat_failure_stops_the_render_and_never_falls_back_to_one_repetition():
    impl = _func(_GUI, "_process_video_impl")
    for handler in ast.walk(impl):
        if (isinstance(handler, ast.ExceptHandler) and handler.type is not None
                and ast.unparse(handler.type) == "music_repeater.MusicRepeatError"):
            block = "\n".join(ast.unparse(statement) for statement in handler.body)
            assert "RenderOutcomeKind.UNKNOWN_FATAL" in block
            assert "return (None," in block
            assert "Repeat Music" in block
            # no silent fallback: nothing here resumes with the original music
            assert "pass" not in block
            break
    else:
        raise AssertionError("no MusicRepeatError handler in the render body")


def test_the_mixdown_and_the_renderer_receive_the_repeated_master():
    """Requirement 7: voice and SFX are placed against the music that will actually play."""
    impl = _func(_GUI, "_process_video_impl")
    calls = {}
    for node in ast.walk(impl):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if name.endswith("build_mixed_master"):
                calls["mix"] = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            elif name == "create_music_video":
                calls["render"] = ast.unparse(node.args[0])
    assert calls["mix"]["music_path"] == "render_audio_path"
    assert calls["mix"]["music_duration"] == "music_duration"
    assert calls["mix"]["beat_times"] == "beat_times"
    assert calls["render"] == "render_audio_path"

    source = _body(_GUI, "_process_video_impl")
    # `beat_times` is read off the bus AFTER the extension, so it is the extended grid
    assert source.index("music_repeater.build_repeated_music(") < \
        source.index("beat_times = beat_info.get('times', selected_beats)")


def test_the_extension_rebinds_both_the_cuts_and_the_bus():
    source = _body(_GUI, "_process_video_impl")
    assert ("selected_beats, beat_info = fork_music_repeat.extend_beat_info("
            in source.replace("\n", "").replace("    ", " ")) or \
        "fork_music_repeat.extend_beat_info(" in source
    impl = _func(_GUI, "_process_video_impl")
    for node in ast.walk(impl):
        if (isinstance(node, ast.Assign)
                and "extend_beat_info" in ast.unparse(node.value)):
            assert ast.unparse(node.targets[0]) == "(selected_beats, beat_info)"
            # and it is given the MEASURED cycle length, never the analysis window's duration
            assert "repeated.cycle_seconds" in ast.unparse(node.value)
            assert "audio_duration" not in ast.unparse(node.value)
            break
    else:
        raise AssertionError("extend_beat_info is not called")


# ===========================================================================
# 11. THE WIRING CONTRACT
# ===========================================================================


@pytest.mark.parametrize("button,handler", [
    ("process_btn", "process_video_guarded"),
    ("render_selected_variants_btn", "render_selected_variants_guarded"),
])
def test_both_render_events_pass_the_repeat_at_the_same_index_as_the_signature(button, handler):
    """Gradio matches positionally, so the click list and the signature are one contract."""
    inputs = _click_inputs(_GUI, button)
    args = [arg.arg for arg in _func(_GUI, handler).args.args]
    assert "repeat_music" in inputs, button
    assert inputs.index("repeat_music") == args.index("repeat_music"), (button, handler)
    # appended LAST in both, so no pre-existing positional index moved
    assert inputs[-1] == "repeat_music"
    assert args[-1] == "repeat_music"


def test_r7_3s_policy_did_not_move():
    """The regression this ordering discipline exists to prevent, stated directly."""
    for button, handler in (("process_btn", "process_video_guarded"),
                            ("render_selected_variants_btn",
                             "render_selected_variants_guarded")):
        inputs = _click_inputs(_GUI, button)
        args = [arg.arg for arg in _func(_GUI, handler).args.args]
        assert inputs.index("voice_overflow") == args.index("voice_overflow")
        assert inputs.index("voice_overflow") == len(inputs) - 2


@pytest.mark.parametrize("handler", [
    "_process_video_guarded_unlocked", "process_video_guarded",
    "render_selected_variants_guarded",
])
def test_the_exec_safe_defaults_are_literals_that_cannot_drift(handler):
    """All three handlers are AST-extracted and exec'd against namespaces where the fork modules are
    stubs, so a default that dereferenced `fork_music_repeat` raises at def-time. Measured, not
    assumed: the first draft used the real constant in `process_video_guarded` and took the
    progress-seam suite down."""
    node = _func(_GUI, handler)
    args = [arg.arg for arg in node.args.args]
    index = args.index("repeat_music")
    default = node.args.defaults[len(node.args.defaults) - (len(args) - index)]
    assert isinstance(default, ast.Constant), (handler, ast.unparse(default))
    assert default.value == M.DEFAULT_REPEAT, (handler, default.value)


def test_the_widget_value_is_normalised_once_at_the_render_boundary():
    """The same seam the creative profile and both mix configs use, so nothing downstream ever sees
    an untrusted widget value — and `_process_video_impl` branches on a plain int."""
    body = _body(_GUI, "_process_video_guarded_unlocked")
    assert "fork_music_repeat.normalize_repeat(repeat_music)" in body
    assert "music_repeat = 1" in body
    assert "music_repeat=music_repeat" in body
    # exactly one normalisation, so there is no second interpretation of what a repeat means
    assert body.count("normalize_repeat(") == 1
    # and it is the only place in gui.py that normalises one
    with open(_GUI, "r", encoding="utf-8") as handle:
        assert handle.read().count("normalize_repeat(") == 1


def test_the_batch_freezes_the_repeat_from_its_own_arguments_never_from_a_recipe():
    """A batch whose candidates rendered different lengths would make the comparison meaningless."""
    body = ast.unparse(_func(_GUI, "render_selected_variants_guarded"))
    assert "repeat_music=repeat_music" in body
    for forbidden in ("recipe.repeat", "audio.repeat", "candidate.repeat",
                      "recipe.music_repeat", "audio.music_repeat"):
        assert forbidden not in body, forbidden


def test_the_count_is_threaded_through_without_reinterpretation():
    for handler in ("process_video", "_process_video_guarded_unlocked"):
        assert "music_repeat=music_repeat" in _body(_GUI, handler), handler


def test_the_estimate_handler_writes_only_its_own_read_out():
    tree = _tree(_GUI)
    registrations = [node for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "change"
                     and any(kw.arg == "fn"
                             and ast.unparse(kw.value) == "_on_repeat_music_change"
                             for kw in node.keywords)]
    assert len(registrations) == 2, "expected the slider and the audio file to drive the read-out"
    owners = sorted(getattr(node.func.value, "id", "?") for node in registrations)
    assert owners == ["audio_input", "repeat_music"]
    for node in registrations:
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        assert [getattr(e, "id", None) for e in kwargs["inputs"].elts] == \
            ["audio_input", "repeat_music"]
        assert [getattr(e, "id", None) for e in kwargs["outputs"].elts] == \
            ["repeat_music_estimate"]
        # not a render: it must never share the render lane
        assert "concurrency_id" not in kwargs
        assert ast.unparse(kwargs["concurrency_limit"]) == "None"


def test_the_estimate_handler_probes_rather_than_guessing():
    body = _body(_GUI, "_on_repeat_music_change")
    assert "music_repeater.probe_duration(" in body
    assert "fork_music_repeat.estimate_markdown(" in body
    # never `get_video_duration`, which answers 10.0 on failure
    assert "get_video_duration" not in body
    # and a failure is swallowed: a status line must not break the Render tab
    assert "duration = None" in body


# ===========================================================================
# 12. REPEAT IS A RENDER REQUEST — NOT SOURCE IDENTITY, NOT ANALYSIS INPUT
# ===========================================================================


def test_the_widget_is_absent_from_source_and_preparation_wiring():
    tree = _tree(_GUI)
    for listname in ("source_outputs", "prep_outputs"):
        assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                          and getattr(node.targets[0], "id", None) == listname)
        names = {getattr(element, "id", None) for element in assignment.value.elts}
        assert "repeat_music" not in names, listname
        assert "repeat_music_estimate" not in names, listname

    for handler in ("_on_source_mode_change", "_on_folder_path_change", "_on_recursive_change",
                    "_on_scan_click", "_on_browser_files_change", "_on_confirm_click",
                    "live_declaration" if False else "_on_prep_folder_change"):
        node = _func(_GUI, handler)
        assert "repeat_music" not in [arg.arg for arg in node.args.args], handler
        assert "repeat_music" not in ast.unparse(node), handler


def test_the_live_declaration_knows_nothing_about_a_repeat():
    """Changing the repeat count must not clear a confirmed source set."""
    core = _body(_GUI, "_process_video_guarded_unlocked")
    declaration = core[core.index("live_declaration("):]
    assert "repeat_music" not in declaration.split(")")[0]


def test_nothing_about_a_repeat_reaches_the_variant_lab_or_the_presets():
    """Variant Lab must never vary it: a repeat count is the operator's decision about the length of
    the video, not a creative value to randomise."""
    for module in ("creative.py", "creative_recipe.py", "variant_lab.py", "variant_batch.py",
                   "presets.py", "director.py", "freestyle.py", "render_batch.py"):
        path = os.path.join(_SRC, "beatsync_fork", module)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        # Deliberately the two exact names rather than a bare "repeat": `freestyle.py` legitimately
        # owns `REPEATED_SECTION_POLICY`, which is about a section TYPE occurring more than once in
        # one track and has nothing to do with playing the song again.
        for forbidden in ("repeat_music", "music_repeat", "MusicRepeat", "MAX_REPEAT"):
            assert forbidden not in source, f"{module} mentions {forbidden}"


def test_the_slider_has_no_writer_anywhere_in_the_app():
    """Zero writers, like every other operator decision that is not a generated value."""
    tree = _tree(_GUI)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("click", "change", "input", "select", "submit")):
            outputs = next((kw.value for kw in node.keywords if kw.arg == "outputs"), None)
            if outputs is None:
                continue
            names = {getattr(inner, "id", None) for inner in ast.walk(outputs)
                     if isinstance(inner, ast.Name)}
            assert "repeat_music" not in names, (
                f"{getattr(node.func.value, 'id', '?')}.{node.func.attr} writes repeat_music")


def test_no_repeat_name_reaches_stage_5_or_its_cache():
    """The count does not exist as a value until after Stage 5 has returned, so there is no route
    for it to key a record through. Asserted as absence in the files that own identity."""
    for relative in (("video_analysis.py",), ("auto_mode", "stage5_qwen_scene_worker.py"),
                     ("auto_mode", "__init__.py"), ("auto_mode", "stage4_select.py"),
                     ("auto_mode", "stage6_av_planner.py"),
                     ("beatsync_fork", "stage_cache.py"),
                     ("beatsync_fork", "analysis_cache.py"),
                     ("beatsync_fork", "library_prep.py")):
        path = os.path.join(_SRC, *relative)
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("repeat_music", "music_repeat", "RepeatedMusic", "repeated_master"):
            assert forbidden not in source, f"{relative[-1]} mentions {forbidden}"


def test_the_cache_contract_constants_are_untouched():
    with open(os.path.join(_SRC, "video_analysis.py"), "r", encoding="utf-8") as handle:
        source = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_the_renderer_itself_needed_no_repeat_awareness():
    """`create_music_video` renders whatever audio file and whatever beat array it is handed, which
    is exactly why R10 needed no second render engine. The ONLY mentions in `video_processor.py` are
    in the CLI parity path."""
    tree = _tree(_PROCESSOR)
    for name in ("create_music_video", "build_frame_aligned_cut_timeline",
                 "create_clip_parallel"):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        rendered = ast.unparse(node)
        for forbidden in ("repeat_music", "music_repeat", "fork_music_repeat", "music_repeater"):
            assert forbidden not in rendered, f"{name} mentions {forbidden}"


# ===========================================================================
# 13. CLI PARITY
# ===========================================================================


def test_the_cli_exposes_the_repeat_with_no_argparse_type():
    """Same discipline as the six creative flags: the value goes through `normalize_repeat`, so a
    malformed one clamps or falls back instead of aborting the run inside argparse."""
    node = _func(_PROCESSOR, "parse_arguments")
    for call in ast.walk(node):
        if (isinstance(call, ast.Call) and ast.unparse(call.func).endswith("add_argument")
                and call.args and ast.unparse(call.args[0]) == "'--repeat-music'"):
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            assert kwargs["default"] == "None"
            assert "type" not in kwargs
            break
    else:
        raise AssertionError("--repeat-music is not declared")


def test_the_cli_normalises_builds_extends_and_cleans_up():
    body = _body(_PROCESSOR, "main")
    assert "fork_music_repeat.normalize_repeat(args.repeat_music)" in body
    assert "music_repeater.build_repeated_music(" in body
    assert "fork_music_repeat.extend_beat_info(" in body
    assert "music_repeater.discard_repeated_master(" in body
    assert body.index("analyze_beats_auto(") < body.index("build_repeated_music(")
    # and the temporary directory is removed too
    assert "shutil.rmtree(repeat_dir, ignore_errors=True)" in body


def test_the_cli_refuses_a_repeat_combined_with_a_trim_window():
    """`-s`/`-e` trim the analysis window, and `create_music_video` would re-apply the same window
    to the repeated master and clamp it back to one excerpt. Refused loudly rather than given an
    invented meaning."""
    body = _body(_PROCESSOR, "main")
    assert "args.start_time or args.end_time" in body
    assert "SystemExit" in body
    assert body.index("args.start_time or args.end_time") < body.index("build_repeated_music(")


def test_the_cli_hands_the_renderer_the_repeated_master():
    body = _body(_PROCESSOR, "_main_render")
    assert "create_music_video(\n    render_audio_path," in body or \
        "create_music_video(render_audio_path," in body.replace("\n    ", "")
    assert "args.mp3_file" not in body.split("create_music_video(")[1].split(")")[0]
