"""Raw Audio Cutter V1 (R7): the silence planner, the argv, the filing rules, and the GUI seam.

`beatsync_fork.audio_cutter` and `beatsync_fork.audio_library` are stdlib-only, so most of this is
ordinary imported-code testing. The GUI half is `ast` over real `gui.py` source, like every other
seam suite here, because `gui.py` cannot be imported on a bare interpreter.

The assertions worth reading are the ones about **arithmetic that is easy to get subtly wrong**:
padding that must not make two clips overlap, a trailing silence that emits no `silence_end` line,
and a leading silence that must not produce a zero-length first clip.
"""

from __future__ import annotations

import ast
import json
import os

import pytest

from beatsync_fork import audio_cutter as cutter
from beatsync_fork import audio_library as library
from beatsync_fork import desktop_folders as folders

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def gui_tree(gui_source: str) -> ast.Module:
    return ast.parse(gui_source)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


# =============================================================================================
# Parsing the detector's log
# =============================================================================================

_LOG = """
[silencedetect @ 0000] silence_start: 0
[silencedetect @ 0000] silence_end: 1.5 | silence_duration: 1.5
[silencedetect @ 0000] silence_start: 3.2
[silencedetect @ 0000] silence_end: 4.0 | silence_duration: 0.8
"""


def test_a_plain_log_yields_its_silences_in_order():
    assert cutter.parse_silence_log(_LOG, 10.0) == [(0.0, 1.5), (3.2, 4.0)]


def test_a_trailing_silence_with_no_end_line_runs_to_the_duration():
    """The real shape of a recording that ends quiet: the filter never sees the level come back
    up, so it emits a `silence_start` and nothing else. Reading that as "no silence" would append
    the trailing quiet to the last clip."""
    log = "[silencedetect] silence_start: 7.5\n"
    assert cutter.parse_silence_log(log, 10.0) == [(7.5, 10.0)]


def test_a_trailing_start_beyond_the_duration_is_discarded():
    assert cutter.parse_silence_log("silence_start: 12.0\n", 10.0) == []


def test_an_interleaved_warning_cannot_mis_pair_a_silence():
    log = ("silence_start: 1.0\n"
           "[mp3 @ 0000] Estimating duration from bitrate, this may be inaccurate\n"
           "silence_end: 2.0 | silence_duration: 1.0\n")
    assert cutter.parse_silence_log(log, 5.0) == [(1.0, 2.0)]


def test_an_end_before_its_start_is_not_a_silence():
    assert cutter.parse_silence_log("silence_start: 3.0\nsilence_end: 3.0\n", 5.0) == []


def test_junk_and_empty_logs_are_simply_no_silences():
    for text in ("", None, "not a log at all", "silence_end: 4.0\n"):
        assert cutter.parse_silence_log(text, 10.0) == []


# =============================================================================================
# The complement: silences -> sounds
# =============================================================================================

def test_the_sounds_are_the_gaps_between_the_silences():
    assert cutter.sound_intervals(10.0, [(0.0, 1.5), (3.2, 4.0)]) == [(1.5, 3.2), (4.0, 10.0)]


def test_leading_and_trailing_silence_produce_no_empty_interval():
    """The requirement, asserted directly. A file that is quiet at both ends must yield exactly
    the sound in the middle and nothing of zero length at either edge."""
    sounds = cutter.sound_intervals(10.0, [(0.0, 2.0), (8.0, 10.0)])
    assert sounds == [(2.0, 8.0)]
    assert all(end > start for start, end in sounds)


def test_a_wholly_silent_file_has_no_sound_at_all():
    assert cutter.sound_intervals(10.0, [(0.0, 10.0)]) == []


def test_a_file_with_no_silence_is_one_continuous_sound():
    assert cutter.sound_intervals(10.0, []) == [(0.0, 10.0)]


def test_overlapping_and_unsorted_silences_are_merged_by_the_cursor():
    assert cutter.sound_intervals(10.0, [(6.0, 8.0), (0.0, 2.0), (1.0, 3.0)]) == [(3.0, 6.0),
                                                                                  (8.0, 10.0)]


def test_a_zero_length_source_plans_nothing():
    assert cutter.sound_intervals(0.0, []) == []


# =============================================================================================
# Planning: padding, the minimum, and the numbering
# =============================================================================================

def test_padding_grows_a_clip_on_both_sides():
    segments = cutter.plan_segments(10.0, [(0.0, 2.0), (8.0, 10.0)], pad=0.1, min_clip=0.3)
    assert len(segments) == 1
    assert segments[0].start == pytest.approx(1.9)
    assert segments[0].end == pytest.approx(8.1)


def test_padding_never_makes_two_clips_overlap():
    """The invariant the whole padding design exists to guarantee.

    A 0.4 s gap with a 0.5 s pad setting would, naively padded, make the two neighbours overlap by
    0.6 s. Each side is capped at half the gap it reaches into, so they meet exactly at the
    midpoint instead.
    """
    silences = [(2.0, 2.4)]
    segments = cutter.plan_segments(10.0, silences, pad=0.5, min_clip=0.1)
    assert len(segments) == 2
    first, second = segments
    assert first.end <= second.start, f"{first} overlaps {second}"
    assert first.end == pytest.approx(2.2)
    assert second.start == pytest.approx(2.2)


@pytest.mark.parametrize("gap", [0.05, 0.2, 0.5, 1.0, 3.0])
@pytest.mark.parametrize("pad", [0.0, 0.1, 0.5, 2.0])
def test_no_pair_of_planned_clips_ever_overlaps(gap, pad):
    """Swept rather than spot-checked: for every gap and every pad, consecutive clips must be
    non-overlapping and in order."""
    silences = [(2.0, 2.0 + gap), (5.0, 5.0 + gap)]
    segments = cutter.plan_segments(12.0, silences, pad=pad, min_clip=0.0)
    for earlier, later in zip(segments, segments[1:]):
        assert earlier.end <= later.start, f"pad={pad} gap={gap}: {earlier} overlaps {later}"
        assert earlier.start < earlier.end


def test_padding_is_clamped_to_the_file_at_both_ends():
    segments = cutter.plan_segments(5.0, [], pad=1.0, min_clip=0.1)
    assert segments[0].start == 0.0
    assert segments[0].end == 5.0


def test_clips_shorter_than_the_minimum_are_dropped():
    # three sounds: 0.2s, 1.0s, 0.2s -- only the middle one survives a 0.3s minimum
    silences = [(0.2, 1.0), (2.0, 3.0), (3.2, 4.0)]
    segments = cutter.plan_segments(4.2, silences, pad=0.0, min_clip=0.3)
    assert [round(s.duration, 3) for s in segments] == [1.0]


def test_the_survivors_are_renumbered_from_one_with_no_holes():
    silences = [(0.2, 1.0), (2.0, 3.0), (3.2, 4.0)]
    segments = cutter.plan_segments(4.2, silences, pad=0.0, min_clip=0.3)
    assert [s.index for s in segments] == [1]

    # Four sounds exist in this fixture: (0, 0.2), (1.0, 2.0), (3.0, 3.2) and (4.0, 4.2).
    segments = cutter.plan_segments(4.2, silences, pad=0.0, min_clip=0.0)
    assert [s.index for s in segments] == [1, 2, 3, 4]


def test_a_wholly_silent_file_plans_no_clips():
    assert cutter.plan_segments(10.0, [(0.0, 10.0)], pad=0.1, min_clip=0.3) == []


def test_a_long_continuous_passage_stays_one_segment():
    """Stated in the docs and asserted here: no silence inside means no split, however long."""
    segments = cutter.plan_segments(600.0, [], pad=0.1, min_clip=0.3)
    assert len(segments) == 1
    assert segments[0].duration == pytest.approx(600.0)


def test_every_planned_segment_is_inside_the_source():
    segments = cutter.plan_segments(9.0, [(1.0, 1.5), (4.0, 4.5)], pad=5.0, min_clip=0.0)
    for segment in segments:
        assert 0.0 <= segment.start < segment.end <= 9.0


def test_junk_settings_fall_back_rather_than_raising():
    assert cutter.plan_segments(None, None if False else [], pad=None, min_clip=None) == []
    assert cutter.plan_segments(5.0, [], pad=-1.0, min_clip=-1.0)[0].duration == pytest.approx(5.0)


# =============================================================================================
# Probe
# =============================================================================================

def test_the_probe_argv_never_carries_the_ffmpeg_only_stdin_switch():
    """FFprobe has no such option and does not reject it cleanly -- it reads the NEXT token as its
    value and dies, so every source would probe as unreadable. A measured trap already paid for
    once in this repository."""
    argv = cutter.build_probe_command("ffprobe.exe", "a.wav")
    assert "-nostdin" not in argv
    assert argv[0] == "ffprobe.exe"
    assert argv[-1] == "a.wav"
    assert "-of" in argv and "json" in argv


def test_a_probe_with_an_audio_stream_is_read():
    payload = json.dumps({
        "streams": [{"codec_name": "pcm_s16le", "sample_rate": "48000", "channels": 2}],
        "format": {"duration": "12.5"},
    })
    info = cutter.parse_probe(payload, path="x.wav")
    assert info.has_audio and info.duration == pytest.approx(12.5)
    assert info.codec == "pcm_s16le" and info.sample_rate == 48000 and info.channels == 2


def test_a_file_with_no_audio_stream_is_reported_not_raised():
    info = cutter.parse_probe(json.dumps({"streams": [], "format": {"duration": "9"}}), "v.mp4")
    assert info.has_audio is False
    assert "No audio stream" in cutter.plan_summary(info, (), (), {})


@pytest.mark.parametrize("text", ["", "not json", "null", "[]", '{"streams": "nonsense"}'])
def test_unreadable_probe_output_is_no_audio_rather_than_an_exception(text):
    assert cutter.parse_probe(text, "x").has_audio is False


def test_a_nan_or_infinite_duration_is_treated_as_zero():
    for value in ("nan", "inf", "-inf"):
        info = cutter.parse_probe(
            json.dumps({"streams": [{"codec_name": "aac"}], "format": {"duration": value}}), "x")
        assert info.duration == 0.0


# =============================================================================================
# The detector and extractor argv
# =============================================================================================

def test_the_detector_writes_no_file_and_decodes_no_video():
    argv = cutter.build_silence_command("ffmpeg.exe", "in.mkv", -35.0, 0.5)
    assert argv[-2:] == ["-f", "null"] or argv[-3:-1] == ["-f", "null"]
    assert "-vn" in argv
    assert "silencedetect=noise=-35dB:d=0.5" in " ".join(argv)
    for forbidden in ("-y", "-c:a", "-map"):
        assert forbidden not in argv, f"the detector must not {forbidden}"


def test_the_extractor_refuses_to_overwrite_rather_than_forcing():
    """`-n`, never `-y`. Overwriting a file in a library the operator is building is the one
    failure this tool must not have."""
    segment = cutter.Segment(index=1, start=1.0, end=2.5)
    argv = cutter.build_extract_command("ffmpeg.exe", "in.wav", segment, "out.wav")
    assert "-n" in argv
    assert "-y" not in argv


def test_the_extractor_seeks_before_the_input_and_takes_only_the_first_audio_stream():
    segment = cutter.Segment(index=1, start=1.0, end=2.5)
    argv = cutter.build_extract_command("ffmpeg.exe", "in.mkv", segment, "out.wav")
    assert argv.index("-ss") < argv.index("-i"), "-ss after -i is the slow seek"
    assert argv[argv.index("-t") + 1] == "1.500000"
    assert "-vn" in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"


def test_wav_is_sixteen_bit_pcm_at_forty_eight_kilohertz():
    segment = cutter.Segment(index=1, start=0.0, end=1.0)
    argv = cutter.build_extract_command("ffmpeg.exe", "in.wav", segment, "o.wav",
                                        cutter.FORMAT_WAV)
    assert argv[argv.index("-c:a") + 1] == "pcm_s16le"
    assert argv[argv.index("-ar") + 1] == "48000"


def test_flac_is_offered_and_is_lossless_at_the_same_rate():
    segment = cutter.Segment(index=1, start=0.0, end=1.0)
    argv = cutter.build_extract_command("ffmpeg.exe", "in.wav", segment, "o.flac",
                                        cutter.FORMAT_FLAC)
    assert argv[argv.index("-c:a") + 1] == "flac"
    assert argv[argv.index("-ar") + 1] == "48000"


# =============================================================================================
# Names and the manifest
# =============================================================================================

def test_clip_numbers_are_zero_padded_so_filename_order_is_clip_order():
    """Audio Layers plays voice files in FILENAME order, so `part_10` sorting before `part_2`
    would silently reorder someone's voice-over."""
    names = [cutter.clip_filename("take", n) for n in (1, 2, 10, 100)]
    assert names == ["take_part_001.wav", "take_part_002.wav",
                     "take_part_010.wav", "take_part_100.wav"]
    assert names == sorted(names)


def test_the_stem_comes_from_the_source_and_is_filename_safe():
    assert cutter.safe_stem(r"J:\Takes\my interview #2.wav").startswith("my_interview")
    assert cutter.safe_stem("") == "audio"
    for ch in '<>:"/\\|?*':
        assert ch not in cutter.safe_stem(f"a{ch}b.wav")


def test_the_flac_choice_changes_the_extension():
    assert cutter.clip_filename("t", 1, cutter.FORMAT_FLAC).endswith(".flac")


def test_the_manifest_records_the_source_the_timings_and_the_outputs():
    info = cutter.SourceInfo(path="in.wav", duration=10.0, has_audio=True,
                            codec="pcm_s16le", sample_rate=48000, channels=1)
    segments = [cutter.Segment(1, 1.0, 2.0), cutter.Segment(2, 4.0, 5.0)]
    payload = cutter.manifest_payload(info, segments, {"threshold_db": -35},
                                      {1: "in_part_001.wav"})
    assert payload["schema"] == cutter.MANIFEST_SCHEMA == "beatsync-raw-audio-cutter-1"
    assert payload["source"]["path"] == "in.wav"
    assert payload["totals"] == {"planned": 2, "written": 1}
    assert payload["segments"][0] == {"index": 1, "start": 1.0, "end": 2.0, "duration": 1.0,
                                      "output": "in_part_001.wav", "written": True}
    assert payload["segments"][1]["written"] is False, "an unwritten clip must be visibly absent"
    assert json.loads(json.dumps(payload)) == payload, "the manifest must be JSON-serialisable"


# =============================================================================================
# Honest reporting
# =============================================================================================

def test_the_summary_refuses_to_claim_word_or_speech_detection():
    info = cutter.SourceInfo(path="a.wav", duration=10.0, has_audio=True)
    segments = cutter.plan_segments(10.0, [(4.0, 5.0)])
    text = cutter.plan_summary(info, [(4.0, 5.0)], segments, {
        "threshold_db": -35, "min_silence": 0.5, "pad": 0.1, "min_clip": 0.3})
    assert "does not detect words" in text
    assert "does not tell speech from music" in text


def test_a_plan_with_no_clips_says_so_and_suggests_what_to_change():
    info = cutter.SourceInfo(path="a.wav", duration=10.0, has_audio=True)
    text = cutter.plan_summary(info, [(0.0, 10.0)], [], {
        "threshold_db": -35, "min_silence": 0.5, "pad": 0.1, "min_clip": 0.3})
    assert "Nothing would be exported" in text
    assert "threshold" in text


def test_timecodes_are_readable_at_both_scales():
    assert cutter.format_timecode(9.25) == "0:09.2"
    assert cutter.format_timecode(75.0) == "1:15.0"
    assert cutter.format_timecode(3725.0).startswith("1:02:")


# =============================================================================================
# Filing: KEEP and DISCARD
# =============================================================================================

def test_a_kept_clip_is_moved_and_its_bytes_are_unchanged(tmp_path):
    source = tmp_path / "run" / "a_part_001.wav"
    source.parent.mkdir()
    source.write_bytes(b"RIFF-payload")
    destination = tmp_path / "library"
    destination.mkdir()

    landed = library.file_clip(str(source), str(destination))
    assert os.path.isfile(landed)
    assert open(landed, "rb").read() == b"RIFF-payload"
    assert not source.exists(), "a move leaves nothing behind"


def test_an_occupied_destination_name_is_refused_and_nothing_is_overwritten(tmp_path):
    source = tmp_path / "run" / "a_part_001.wav"
    source.parent.mkdir()
    source.write_bytes(b"new")
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "a_part_001.wav").write_bytes(b"original")

    with pytest.raises(library.FilingError) as failure:
        library.file_clip(str(source), str(destination))
    assert "already there" in str(failure.value)
    assert (destination / "a_part_001.wav").read_bytes() == b"original", "it was overwritten"
    assert source.exists(), "the source was lost on a refusal"


def test_no_collision_renaming_is_invented(tmp_path):
    """A taken name is reported, never worked around with a `_1` suffix: the operator decides."""
    source = tmp_path / "run" / "a.wav"
    source.parent.mkdir()
    source.write_bytes(b"x")
    destination = tmp_path / "library"
    destination.mkdir()
    (destination / "a.wav").write_bytes(b"y")
    with pytest.raises(library.FilingError):
        library.file_clip(str(source), str(destination))
    assert sorted(p.name for p in destination.iterdir()) == ["a.wav"]


def test_an_edited_name_is_used_but_the_real_extension_always_wins(tmp_path):
    source = tmp_path / "run" / "a_part_001.wav"
    source.parent.mkdir()
    source.write_bytes(b"x")
    destination = tmp_path / "library"
    destination.mkdir()

    landed = library.file_clip(str(source), str(destination), "01 greeting.mp3")
    assert os.path.basename(landed) == "01 greeting.wav", (
        "a wrong extension would hand Audio Layers a file it cannot open")


def test_an_empty_or_unusable_edited_name_falls_back_to_the_clip_name():
    assert library.target_name("", "x/a_part_001.wav") == "a_part_001.wav"
    assert library.target_name("   ", "x/a_part_001.wav") == "a_part_001.wav"
    assert library.target_name('<>:"|?*', "x/a_part_001.wav") == "a_part_001.wav"


def test_a_directory_separator_cannot_escape_the_destination():
    assert library.target_name(r"..\..\evil", "a.wav") == "evil.wav"
    assert library.target_name("sub/evil", "a.wav") == "evil.wav"


def test_discarding_deletes_nothing(tmp_path):
    source = tmp_path / "run" / "a.wav"
    source.parent.mkdir()
    source.write_bytes(b"payload")
    quarantine = tmp_path / "discarded"
    quarantine.mkdir()
    landed = library.file_clip(str(source), str(quarantine))
    assert open(landed, "rb").read() == b"payload", "quarantine must be a move, not a delete"


def test_the_module_contains_no_deletion_primitive_at_all():
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "audio_library.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("os.remove", "os.unlink", "os.rmdir", "shutil.rmtree", "os.replace"):
        assert forbidden not in calls, f"{forbidden} is reachable from the filing module"


def test_os_replace_is_never_used_because_it_would_overwrite():
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "audio_library.py")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    renames = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "os.rename"]
    assert len(renames) == 1, "the move must be exactly one os.rename"


def test_a_missing_destination_folder_is_refused_without_creating_it(tmp_path):
    missing = tmp_path / "nope"
    problem = library.destination_problem(str(missing), "voice library")
    assert "not a folder that exists" in problem
    assert not missing.exists(), "validation created the folder"


def test_a_blank_destination_names_the_field_it_wants(tmp_path):
    assert "voice library" in library.destination_problem("", "voice library")


def test_a_destination_inside_the_reviewed_folder_is_refused(tmp_path):
    run = tmp_path / "run"
    inner = run / "keep"
    inner.mkdir(parents=True)
    problem = library.destination_problem(str(inner), "voice library", str(run))
    assert "must not be inside" in problem


def test_a_destination_beside_the_reviewed_folder_is_fine(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    beside = tmp_path / "run_library"          # shares a prefix but is NOT inside
    beside.mkdir()
    assert library.destination_problem(str(beside), "voice library", str(run)) == ""


# =============================================================================================
# The native FILE dialog
# =============================================================================================

def test_the_file_dialog_is_opened_single_threaded_apartment():
    """`OpenFileDialog` is a COM control and silently fails on PowerShell 5.1's MTA default."""
    argv = folders.build_file_picker_command()
    assert "-STA" in argv
    assert "-NoProfile" in argv
    assert "OpenFileDialog" in argv[-1]
    assert "$dlg.Multiselect = $false" in argv[-1]


def test_neither_the_start_path_nor_the_filter_reaches_the_command_line():
    """A pathname can contain quotes, `$` and backticks, every one of which is live PowerShell."""
    argv = folders.build_file_picker_command()
    joined = " ".join(argv)
    assert "$env:BEATSYNC_PICK_START" in joined
    assert "$env:BEATSYNC_PICK_FILTER" in joined
    hostile = r'C:\a"; Remove-Item C:\ -Recurse; "'
    environment = folders.file_picker_environment(hostile, "x|*.x", base={})
    assert hostile not in " ".join(folders.build_file_picker_command())
    assert environment.get("BEATSYNC_PICK_START") in (None, "")


def test_the_start_directory_of_a_file_is_its_folder(tmp_path):
    media = tmp_path / "take.wav"
    media.write_bytes(b"x")
    environment = folders.file_picker_environment(str(media), base={})
    assert environment["BEATSYNC_PICK_START"] == str(tmp_path)


def test_the_filter_always_ends_with_an_all_files_entry():
    """The suffix list is a convenience, never a gate -- the real check is whether FFprobe finds
    an audio stream, so an unusual container must still be selectable."""
    text = folders.file_filter("Audio and video", cutter.SOURCE_SUFFIXES)
    assert text.endswith("All files|*.*")
    assert "*.wav" in text and "*.mp4" in text and "*.mkv" in text


def test_a_cancelled_file_dialog_yields_no_path():
    def runner(argv, environment, timeout):
        return folders.PICKED, "CANCELLED"
    assert folders.pick_file(runner=runner) == (folders.CANCELLED, "")


def test_a_picked_file_comes_back_absolute():
    def runner(argv, environment, timeout):
        return folders.PICKED, "PICKED:" + os.path.join(os.getcwd(), "a.wav")
    status, path = folders.pick_file(runner=runner)
    assert status == folders.PICKED and os.path.isabs(path)


# =============================================================================================
# The GUI seam
# =============================================================================================

def test_the_audio_cutter_is_the_sixth_top_level_tab_before_clips_preview(gui_source: str):
    assert "gr.Tab(LABEL_TAB_AUDIO_CUTTER" in gui_source
    audio = gui_source.index("gr.Tab(LABEL_TAB_AUDIO_CUTTER")
    assert gui_source.index("gr.Tab(LABEL_TAB_ADVANCED") < audio, "it must follow Advanced Tools"
    assert audio < gui_source.index("gr.Tab(LABEL_TAB_CLIPS_PREVIEW"), \
        "Clips Preview must stay LAST"


def _audio_tab_body(tree: ast.Module) -> str:
    """The tab's own `with` body, unparsed.

    Taken from the AST rather than from a source slice, and that is not fastidiousness: a slice
    running to the next tab also swallows that tab's explanatory comment, and these assertions are
    substring checks. Prose naming a banned token trips them exactly as real code would -- a trap
    this repository has paid for several times. `ast.unparse` drops comments entirely.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
                "gr.Tab(LABEL_TAB_AUDIO_CUTTER" in ast.unparse(item.context_expr)
                for item in node.items):
            return "\n".join(ast.unparse(statement) for statement in node.body)
    raise AssertionError("the Audio Cutter tab is not a `with gr.Tab(...)` block")


def test_the_audio_tab_hides_none_of_its_controls(gui_tree: ast.Module):
    block = _audio_tab_body(gui_tree)
    assert "gr.Accordion" not in block, "an operator must see the four settings without expanding"
    for widget in ("audio_source", "audio_output", "audio_threshold", "audio_min_silence",
                   "audio_pad", "audio_min_clip", "audio_format", "audio_analyze_btn",
                   "audio_cut_btn", "audio_plan", "audio_table", "audio_report",
                   "audio_clip_picker", "audio_player", "audio_keep_btn", "audio_discard_btn",
                   "audio_library_folder", "audio_discarded_folder"):
        assert f"{widget} = gr." in block, f"{widget} is not in the Audio Cutter tab"


def test_the_feature_adds_no_process_launch_to_the_view_layer(gui_tree: ast.Module):
    """Both launches live in the fork core. Two suites pin this module's count at exactly two, and
    R7 adds none."""
    runs = [n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]
    popens = [n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
              and ast.unparse(n.func) in {"subprocess.Popen", "Popen"}]
    assert len(runs) == 2, f"{len(runs)} subprocess.run sites in gui.py"
    assert popens == []


def test_analyze_writes_nothing(gui_tree: ast.Module):
    """The requirement: Analyze detects and plans, and must not create a file or a directory."""
    node = _func(gui_tree, "_on_audio_analyze")
    calls = {ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
    for forbidden in ("os.makedirs", "os.mkdir", "open", "os.rename", "os.replace", "os.remove",
                      "json.dump"):
        assert forbidden not in calls, f"Analyze calls {forbidden}"


def test_the_cut_handler_creates_its_run_directory_exclusively(gui_tree: ast.Module):
    """`exist_ok=False`: a fresh directory per cut is what makes overwriting impossible without an
    existence check that a second press could race."""
    node = _func(gui_tree, "_on_audio_cut")
    makedirs = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "os.makedirs"]
    assert len(makedirs) == 1
    keywords = {kw.arg: ast.unparse(kw.value) for kw in makedirs[0].keywords}
    assert keywords.get("exist_ok") == "False", "the run directory must not be reused"


_AUDIO_BUTTONS = ("audio_analyze_btn", "audio_cut_btn", "audio_keep_btn", "audio_discard_btn")


def _registration(tree: ast.Module, widget: str, event: str = "click") -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == widget
                and node.func.attr == event):
            return node
    raise AssertionError(f"no {widget}.{event} registration")


def test_no_audio_handler_touches_a_render_or_source_widget(gui_tree: ast.Module):
    """Isolation, asserted on the real `inputs`/`outputs` of each registration.

    Deliberately NOT a source slice: the comment above the wiring legitimately names every one of
    these widgets while explaining that it does not touch them, and a substring check over raw
    source cannot tell the explanation from the code.
    """
    forbidden = ("status_output", "video_output", "process_btn", "source_state",
                 "source_outputs", "prep_outputs", "session_state", "cut_run_btn",
                 "cut_report", "render_invocation_state", "variation_seed",
                 "creative_preset", "_RENDER_LOCK", "RENDER_CONCURRENCY_ID")
    for widget in _AUDIO_BUTTONS + ("audio_clip_picker",):
        event = "change" if widget == "audio_clip_picker" else "click"
        call = _registration(gui_tree, widget, event)
        rendered = " ".join(ast.unparse(kw.value) for kw in call.keywords
                            if kw.arg in {"inputs", "outputs"})
        for name in forbidden:
            assert name not in rendered, f"{widget}.{event} reads or writes {name}"


def test_the_audio_cutter_registers_exactly_its_own_five_events(gui_tree: ast.Module):
    for widget in _AUDIO_BUTTONS:
        _registration(gui_tree, widget, "click")
    _registration(gui_tree, "audio_clip_picker", "change")


def test_no_audio_event_joins_the_render_concurrency_lane(gui_tree: ast.Module):
    """Measured on the pinned gradio 6.19.0: a full concurrency group leaves an event QUEUED
    rather than delivering it, so these carry `concurrency_limit=None` like the Clips Preview
    registrations."""
    for widget in _AUDIO_BUTTONS:
        call = _registration(gui_tree, widget, "click")
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert keywords.get("concurrency_limit") == "None", widget
        assert "concurrency_id" not in keywords, widget


def test_the_review_list_is_per_session_state_not_a_module_record(gui_tree: ast.Module,
                                                                 gui_source: str):
    """One operator's review list must not be reachable from another browser session.

    Asserted by walking real module-level assignments. A name-substring ban over raw source was
    tried first and was wrong: `_AUDIO_REVIEW` is a substring of the perfectly legitimate UI label
    `LABEL_AUDIO_REVIEW`, so the ban fired on a piece of prose.
    """
    assert "audio_review_state = gr.State([])" in gui_source
    module_level = {target.id for node in gui_tree.body
                    if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
    holders = {name for name in module_level
               if name.upper().startswith("_AUDIO") and not name.startswith("LABEL")}
    assert holders == set(), f"module-level Audio Cutter record(s): {sorted(holders)}"


def test_the_keep_and_discard_registrations_write_the_same_five_outputs(gui_source: str):
    """They are one operation to two folders, so a divergence here would mean one of them leaves
    the screen describing a clip that has already moved."""
    for button in ("audio_keep_btn", "audio_discard_btn"):
        start = gui_source.index(f"{button}.click(")
        block = gui_source[start:gui_source.index("concurrency_limit", start)]
        assert "audio_file_report" in block and "audio_review_state" in block
        assert "audio_clip_picker" in block and "audio_player" in block


def test_the_audio_layers_tip_is_present_and_names_the_real_controls(gui_source: str):
    """R7 must not redesign Audio Layers; it must explain how to feed it."""
    assert "INFO_AUDIO_LAYERS_TIP" in gui_source
    content_path = os.path.join(_REPO_ROOT, "src", "ui_content.py")
    with open(content_path, encoding="utf-8") as handle:
        content = handle.read()
    tip = content[content.index("INFO_AUDIO_LAYERS_TIP"):]
    for fragment in ("Audio Layers", "filename", "Start Delay", "Minimum Gap", "Music Under"):
        assert fragment in tip, f"the tip does not mention {fragment}"


def test_the_feature_claims_no_transcription_or_classifier(gui_source: str):
    """R8 is where that would live, and R7 is deliberately not it."""
    audio = gui_source.index("gr.Tab(LABEL_TAB_AUDIO_CUTTER")
    block = gui_source[audio:gui_source.index("gr.Tab(LABEL_TAB_CLIPS_PREVIEW")]
    for forbidden in ("whisper", "Whisper", "transcri", "qwen", "Qwen", "classif"):
        assert forbidden not in block, f"the Audio Cutter tab mentions {forbidden}"


# =============================================================================================
# The in-page preview
# =============================================================================================

def test_a_clip_is_inlined_rather_than_served_from_its_path(tmp_path):
    """Measured, and the whole reason this is markup rather than a file component.

    Gradio serves files only from its own cache, the working directory or the system temp
    directory, so an exported clip in the operator's own folder raises `InvalidPathError`. The
    alternative fix -- widening `allowed_paths` to a media root -- exposes that whole tree to every
    request, which this application has already refused once.
    """
    clip = tmp_path / "take_part_001.wav"
    clip.write_bytes(b"RIFF0000WAVEfmt ")
    markup = cutter.preview_markup(str(clip), "take_part_001.wav")
    assert "<audio controls" in markup
    assert "src=\"data:audio/wav;base64," in markup
    assert str(tmp_path) not in markup, "the operator's path leaked into the page"


def test_the_flac_preview_declares_the_right_media_type(tmp_path):
    clip = tmp_path / "a.flac"
    clip.write_bytes(b"fLaC")
    assert "data:audio/flac;base64," in cutter.preview_markup(str(clip))


def test_a_missing_clip_says_so_instead_of_rendering_a_broken_player(tmp_path):
    markup = cutter.preview_markup(str(tmp_path / "gone.wav"), "gone.wav")
    assert "<audio" not in markup
    assert "no longer on disk" in markup


def test_an_oversized_clip_is_described_rather_than_embedded(tmp_path, monkeypatch):
    clip = tmp_path / "huge.wav"
    clip.write_bytes(b"x" * 2048)
    monkeypatch.setattr(cutter, "MAX_PREVIEW_BYTES", 1024)
    markup = cutter.preview_markup(str(clip), "huge.wav")
    assert "<audio" not in markup
    assert "too large to preview" in markup
    assert "was still exported" in markup, "it must not read as a failure"


def test_a_filename_cannot_carry_markup_into_the_page(tmp_path):
    clip = tmp_path / "x.wav"
    clip.write_bytes(b"RIFF")
    markup = cutter.preview_markup(str(clip), '<img src=x onerror="alert(1)">')
    assert "<img" not in markup
    assert "&lt;img" in markup


def test_the_player_is_html_not_a_file_component(gui_tree: ast.Module):
    """A file component would hand Gradio a path it refuses to serve."""
    block = _audio_tab_body(gui_tree)
    assert "audio_player = gr.HTML(" in block
    assert "gr.Audio(" not in block
