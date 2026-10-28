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
# [R7.2 Phase 5] MP3 output
# =============================================================================================

def test_mp3_is_offered_as_a_third_format_and_nothing_was_reordered():
    """Appended, never inserted. WAV stays the default, which several callers rely on."""
    assert cutter.FORMATS == (cutter.FORMAT_WAV, cutter.FORMAT_FLAC, cutter.FORMAT_MP3)
    assert cutter.FORMAT_MP3 == "mp3"


def test_mp3_uses_libmp3lame_at_the_suggested_bitrate():
    segment = cutter.Segment(index=1, start=0.0, end=1.0)
    argv = cutter.build_extract_command("ffmpeg.exe", "in.mp3", segment, "o.mp3",
                                        cutter.FORMAT_MP3)
    assert argv[argv.index("-c:a") + 1] == "libmp3lame" == cutter.MP3_ENCODER
    assert argv[argv.index("-b:a") + 1] == "192k"
    assert cutter.DEFAULT_MP3_BITRATE_KBPS == 192
    assert argv[argv.index("-ar") + 1] == "48000", "a valid MPEG-1 Layer III rate"


def test_mp3_asks_for_the_gapless_header_so_timings_are_as_close_as_mp3_permits():
    """An MP3 frame is 1152 samples, so the encoder cannot end on an arbitrary sample. LAME
    records the delay and padding in the Xing/LAME header and a decoder that honours it reproduces
    the clip's original length; `-write_xing 1` makes that explicit rather than leaving it to the
    muxer's default. This is **not** sample-exactness and must not be described as such."""
    argv = cutter.build_extract_command("ffmpeg.exe", "in.mp3",
                                        cutter.Segment(1, 0.0, 1.0), "o.mp3", cutter.FORMAT_MP3)
    assert argv[argv.index("-write_xing") + 1] == "1"


@pytest.mark.parametrize("fmt", ["wav", "flac", "mp3"])
def test_no_format_ever_forces_a_channel_count(fmt):
    """Mono stays mono and stereo stays stereo, by omission. Forcing stereo on a mono voice clip
    would double the bitrate MP3 needs for the same quality."""
    argv = cutter.build_extract_command("ffmpeg.exe", "in.wav",
                                        cutter.Segment(1, 0.0, 1.0), f"o.{fmt}", fmt)
    assert "-ac" not in argv


def test_mp3_keeps_the_no_overwrite_and_first_audio_stream_rules():
    """Phase 5 must not change anything but the encoder."""
    argv = cutter.build_extract_command("ffmpeg.exe", "in.mkv",
                                        cutter.Segment(1, 2.0, 3.0), "o.mp3", cutter.FORMAT_MP3)
    assert "-n" in argv and "-y" not in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"
    assert "-vn" in argv
    assert argv.index("-ss") < argv.index("-i"), "the fast accurate seek"


def test_an_unknown_or_missing_format_falls_back_to_wav():
    """A `gr.Radio` can hand over `None`. Reaching an argv builder with an unknown format would
    write a file with an extension nothing can open."""
    for junk in (None, "", "ogg", "MP4", 7, object()):
        assert cutter.normalize_format(junk) == cutter.FORMAT_WAV
    # Case and whitespace are tolerated, because a stored preference may carry either.
    assert cutter.normalize_format(" MP3 ") == cutter.FORMAT_MP3
    assert cutter.normalize_format("FLAC") == cutter.FORMAT_FLAC


def test_the_encoder_probe_matches_a_whole_token_and_fails_closed():
    """An empty or failed probe must answer False. Matching a substring would make a missing
    encoder look present, and every clip would then fail with an FFmpeg error instead."""
    real = (" A....D flac                 FLAC (Free Lossless Audio Codec)\n"
            " A....D libmp3lame           libmp3lame MP3 (MPEG audio layer 3) (codec mp3)\n")
    assert cutter.encoder_available(real) is True
    assert cutter.encoder_available("") is False
    assert cutter.encoder_available(None) is False
    assert cutter.encoder_available(" A....D libmp3lame_ng  something ") is False
    assert cutter.encoder_available(real, "") is False


def test_the_encoder_probe_argv_is_a_plain_listing():
    argv = cutter.build_encoders_command("ffmpeg.exe")
    assert argv[0] == "ffmpeg.exe"
    assert "-encoders" in argv
    assert "-hide_banner" in argv


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


def test_the_mp3_choice_changes_the_extension_and_keeps_the_padding():
    """[R7.2] A wrong extension here would hand Audio Layers a file it cannot open -- and the
    zero-padding still has to survive, because Audio Layers plays files in filename order."""
    assert cutter.clip_filename("t", 1, cutter.FORMAT_MP3) == "t_part_001.mp3"
    names = [cutter.clip_filename("take", n, cutter.FORMAT_MP3) for n in (1, 2, 10, 100)]
    assert names == sorted(names)


def test_an_unknown_format_still_produces_an_openable_name():
    assert cutter.clip_filename("t", 1, "ogg").endswith(".wav")
    assert cutter.clip_filename("t", 1, None).endswith(".wav")


def test_mp3_is_accepted_by_audio_layers_so_exported_clips_are_usable_there():
    """[R7.2 Phase 5] "MP3 must work in Audio Layers" -- asserted against the real voice-input
    allowlist rather than assumed, because advertising a format the mixdown refuses would make the
    UI and the executor disagree about the contract."""
    from beatsync_fork import audio_mix

    assert f".{cutter.FORMAT_MP3}" in audio_mix.SUPPORTED_VOICE_EXTENSIONS
    for fmt in cutter.FORMATS:
        extension = os.path.splitext(cutter.clip_filename("t", 1, fmt))[1]
        assert extension in audio_mix.SUPPORTED_VOICE_EXTENSIONS, (
            f"the cutter can export {extension} but Audio Layers will not accept it")


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


def test_the_manifest_records_the_codec_format_and_bitrate():
    """[R7.2 Phase 5] "Which format is this folder in" is a question an operator asks about a
    library months later, and a `.wav` extension does not answer it -- it says nothing about the
    bit depth, and for MP3 nothing about the bitrate."""
    info = cutter.SourceInfo(path="in.mp3", duration=10.0, has_audio=True)
    segments = [cutter.Segment(1, 1.0, 2.0)]

    mp3 = cutter.manifest_payload(info, segments, {"format": "mp3"}, {1: "in_part_001.mp3"},
                                  cutter.encoding_record("mp3"))
    assert mp3["encoding"] == {
        "format": "mp3", "extension": "mp3", "sample_rate": 48000, "lossy": True,
        "codec": "libmp3lame", "bitrate_kbps": 192, "bitrate_mode": "cbr"}
    assert json.loads(json.dumps(mp3)) == mp3

    wav = cutter.encoding_record("wav")
    assert wav["codec"] == "pcm_s16le" and wav["lossy"] is False
    flac = cutter.encoding_record("flac")
    assert flac["codec"] == "flac" and flac["lossy"] is False


def test_the_encoding_block_is_derived_when_a_caller_does_not_supply_one():
    """A defaulted trailing parameter, so every existing caller and every existing reader is
    unaffected. The format already lived in `settings`, so nothing is guessed."""
    info = cutter.SourceInfo(path="in.wav", duration=1.0, has_audio=True)
    payload = cutter.manifest_payload(info, [], {"format": "flac"}, {})
    assert payload["encoding"]["codec"] == "flac"
    # And with no format at all it falls back to the documented default rather than raising.
    assert cutter.manifest_payload(info, [], {}, {})["encoding"]["format"] == "wav"


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
    """The move is a no-replace rename, and `os.replace` appears nowhere on the path.

    [FORK] R8 UX Refresh. This used to count `os.rename` CALLS in `audio_library.py` and require
    exactly one. R8 moved the rename loop into `beatsync_fork.collision`, which both review
    workflows now share, so `audio_library` *passes* `os.rename` rather than calling it -- and the
    old assertion would read zero and fail while the property it protects is intact.

    The property is therefore asserted where it now lives, in two halves, which is strictly
    stronger than the single count it replaces:

    1. `audio_library` hands over `os.rename` and nothing else -- never `os.replace`, never a
       `shutil` move, and never a second rename of its own.
    2. `collision.publish` performs exactly one call to the rename it was given, and `collision`
       contains no overwriting or deleting primitive at all.
    """
    library_path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "audio_library.py")
    with open(library_path, encoding="utf-8") as handle:
        library_tree = ast.parse(handle.read())

    # 1. The primitive audio_library hands to the publisher is os.rename, passed not called.
    publishes = [n for n in ast.walk(library_tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("collision.publish")]
    assert len(publishes) == 1, "there must be exactly one publication site"
    handed = {ast.unparse(a) for a in publishes[0].args}
    assert "os.rename" in handed, f"os.rename is not the primitive handed over: {handed}"
    assert "os.replace" not in handed, "os.replace would overwrite"

    # Asserted over CALLS, not over the rendered source. The module docstring legitimately names
    # `os.replace` in the sentence explaining why it is never used, and a ban that the
    # explanation of the ban cannot survive forces documentation to be written around a test.
    # This repository has paid for that mistake several times.
    library_calls = {ast.unparse(n.func) for n in ast.walk(library_tree)
                     if isinstance(n, ast.Call)}
    for forbidden in ("os.replace", "shutil.move", "shutil.copy", "shutil.copy2"):
        assert forbidden not in library_calls, \
            f"{forbidden} is reachable from the filing module"

    # 2. The publisher calls the injected rename exactly once, and owns no destructive primitive.
    collision_path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "collision.py")
    with open(collision_path, encoding="utf-8") as handle:
        collision_tree = ast.parse(handle.read())
    publish = next(n for n in ast.walk(collision_tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "publish")
    renames = [n for n in ast.walk(publish) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "rename"]
    assert len(renames) == 1, "the move must be exactly one call to the injected rename"

    collision_calls = {ast.unparse(n.func) for n in ast.walk(collision_tree)
                       if isinstance(n, ast.Call)}
    for forbidden in ("os.remove", "os.unlink", "os.rmdir", "os.replace", "shutil.rmtree",
                      "shutil.move", "shutil.copy", "shutil.copy2"):
        assert forbidden not in collision_calls, f"{forbidden} is reachable from the publisher"


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

def test_the_audio_cutter_is_a_top_level_tab_beside_the_other_cutter(gui_source: str):
    """R8 UX Refresh (Commit A). It used to be "the sixth tab, after Advanced Tools and before
    Clips Preview" -- a position chosen to keep two other suites' index arithmetic valid rather
    than for any reason an operator would recognise.

    The claim that survives, and the one the re-order was for: the Audio Cutter is a top-level
    destination of its own, and it sits directly beside the Raw Cutter, because the two are the
    same job on different media and an operator looking for one will look for the other.
    """
    assert "gr.Tab(LABEL_TAB_AUDIO_CUTTER" in gui_source
    audio = gui_source.index("gr.Tab(LABEL_TAB_AUDIO_CUTTER")
    raw = gui_source.index("gr.Tab(LABEL_TAB_RAW_CUTTER")
    assert raw < audio, "the two cutters must stay adjacent, Raw then Audio"
    mix = gui_source.index("gr.Tab(LABEL_TAB_AUDIO_MIX")
    assert audio < mix, "the Audio Cutter must come before Audio Mix"


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
                   # [R7.1] the review surface is a gallery, not a dropdown and two buttons
                   "audio_gallery", "audio_counts", "audio_decisions", "audio_confirm_btn",
                   "audio_enable_hover",
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


_AUDIO_BUTTONS = ("audio_analyze_btn", "audio_cut_btn", "audio_confirm_btn")


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
    for widget in _AUDIO_BUTTONS:
        event = "click"
        call = _registration(gui_tree, widget, event)
        rendered = " ".join(ast.unparse(kw.value) for kw in call.keywords
                            if kw.arg in {"inputs", "outputs"})
        for name in forbidden:
            assert name not in rendered, f"{widget}.{event} reads or writes {name}"


def test_the_audio_cutter_registers_exactly_its_own_three_events(gui_tree: ast.Module):
    """[R7.1] Analyze, Cut and Confirm. Classification needs no server event at all -- it happens
    in the page and rides to the server inside one hidden field with the Confirm click."""
    for widget in _AUDIO_BUTTONS:
        _registration(gui_tree, widget, "click")


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
    assert "audio_review_state = gr.State({})" in gui_source
    module_level = {target.id for node in gui_tree.body
                    if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
    holders = {name for name in module_level
               if name.upper().startswith("_AUDIO") and not name.startswith("LABEL")}
    # [R7.2] Four module-level `_AUDIO*` names now exist and none of them is an operator's review
    # list. They are allowed BY NAME rather than by a prefix pattern, so a future module-level
    # record cannot join them by being spelled similarly.
    allowed = {"_AUDIO_CUT_REGISTRY", "_AUDIO_PREVIEW_REGISTRY",
               "_AUDIO_SURFACE_CUT", "_AUDIO_SURFACE_PREVIEW",
               "_AUDIO_PREVIEW_OUTPUT_ORDER"}
    assert holders <= allowed, (
        f"unreviewed module-level Audio record(s): {sorted(holders - allowed)}")


def test_the_module_level_audio_registries_hold_no_decisions_and_are_owner_scoped(gui_tree,
                                                                                 gui_source):
    """[R7.2] The two registries are process-global, so the claim above needs a positive half.

    A `ClipRegistry` maps opaque ids to file identities and is scoped per owner internally -- the
    same object the Clips Preview has used since R5-P1. **No decision, no edited name and no path
    list is module-level**: those live in each tab's own `gr.State`, which is what keeps one
    browser session's review unreachable from another.
    """
    for registry in ("_AUDIO_CUT_REGISTRY", "_AUDIO_PREVIEW_REGISTRY"):
        assert f"{registry} = fork_clips.ClipRegistry()" in gui_source, registry
    assert "_AUDIO_CUT_REGISTRY" != "_AUDIO_PREVIEW_REGISTRY"

    # The two surfaces must not share a registry: `new_session` revokes an owner's previous
    # sessions, so a shared one would let an Audio Preview scan kill the cutter's live gallery.
    cut = ast.unparse(_func(gui_tree, "_on_audio_confirm"))
    preview = ast.unparse(_func(gui_tree, "_on_audio_preview_confirm"))
    assert "_AUDIO_SURFACE_CUT" in cut and "_AUDIO_SURFACE_PREVIEW" not in cut
    assert "_AUDIO_SURFACE_PREVIEW" in preview and "_AUDIO_SURFACE_CUT" not in preview


def _wired(gui_tree: ast.Module, button: str, which: str, event: str = "click") -> set:
    """The widget names an event reads or writes, resolving ONE level of list-variable indirection.

    [R7.2] Confirm, Proceed and Cancel share their `inputs=` and `outputs=` lists on purpose -- it
    is what makes "all three read and write the same things" structural rather than three lists
    kept in step by hand. A plain substring check would see only the variable name and pass
    **vacuously**, which is exactly the trap `expanded_names_in` exists for in the Variant Lab
    suites; this suite hit it on its first run against the new wiring.
    """
    call = _registration(gui_tree, button, event)
    node = next(kw for kw in call.keywords if kw.arg == which).value
    if isinstance(node, ast.Name):
        for assign in ast.walk(gui_tree):
            if (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                    and isinstance(assign.targets[0], ast.Name)
                    and assign.targets[0].id == node.id):
                node = assign.value
                break
        else:
            raise AssertionError(f"{button} {which}={node.id} was never assigned")
    assert isinstance(node, (ast.List, ast.Tuple)), ast.unparse(node)
    return {n.id for n in node.elts if isinstance(n, ast.Name)}


def _outputs_of(gui_tree: ast.Module, gui_source: str, button: str) -> set:
    return _wired(gui_tree, button, "outputs")


def test_confirm_repaints_everything_it_changed(gui_tree: ast.Module, gui_source: str):
    """[R7.1] One filing action. It must rewrite the report, the grid, the counters AND the
    server-side review list -- repainting the old page would leave already-moved clips on screen,
    and a second Confirm would then report them as missing.

    [R7.2] Plus the sweep gate's own two buttons and its signature, so a resolved batch cannot
    leave a Proceed on screen that is no longer attached to any counts.
    """
    written = _outputs_of(gui_tree, gui_source, "audio_confirm_btn")
    for widget in ("audio_file_report", "audio_gallery", "audio_counts", "audio_review_state",
                   "audio_armed_state", "audio_proceed_btn", "audio_sweep_cancel_btn"):
        assert widget in written, widget


def test_proceed_and_cancel_write_exactly_what_confirm_writes(gui_tree: ast.Module,
                                                              gui_source: str):
    """[R7.2 Phase 3] There must be no path on which Proceed moves a file Confirm would not have
    moved. Sharing the output set is half of that; `_on_audio_proceed` delegating to
    `_on_audio_confirm` is the other half, so the sweep cannot acquire its own policy."""
    confirm = _outputs_of(gui_tree, gui_source, "audio_confirm_btn")
    assert _outputs_of(gui_tree, gui_source, "audio_proceed_btn") == confirm
    assert _outputs_of(gui_tree, gui_source, "audio_sweep_cancel_btn") == confirm
    body = ast.unparse(_func(gui_tree, "_on_audio_proceed"))
    assert "_on_audio_confirm(" in body, "Proceed must BE Confirm, with the signature supplied"


def test_confirm_takes_the_decisions_only_through_the_hidden_field(gui_tree: ast.Module):
    """The browser supplies a decision and a name per clip id. It never supplies a PATH -- every
    file that moves is named by the server's own frozen page."""
    read = _wired(gui_tree, "audio_confirm_btn", "inputs")
    assert "audio_decisions" in read
    assert "audio_review_state" in read

    # [R7.2] The file that moves is `entry.path` off the registry's frozen ClipEntry, and the
    # decision is looked up BY that entry. Nothing is indexed out of the payload.
    body = ast.unparse(_func(gui_tree, "_audio_sort"))
    assert "entry.clip_id" in body
    assert "view.get('entries')" in body
    assert "payload" not in body, "the sort must not see the browser's payload at all"


def test_confirm_needs_one_kept_clip_and_never_sweeps_without_being_told(gui_tree: ast.Module):
    """[R7.2 Phase 3] The admission rule the owner asked for, and the gate that keeps it safe.

    R7.1 refused Confirm while anything was undecided, which made the operator click Reject on
    every clip they did not want. Confirm now needs one KEPT clip. The unmarked remainder is
    treated as discarded -- but only after an explicit, batch-specific confirmation, and the
    sweep is an explicit parameter so it cannot happen by omission.
    """
    body = ast.unparse(_func(gui_tree, "_audio_confirm_core"))

    # The admission rule is "something is kept", not "nothing is undecided".
    assert "plan.can_sort" in body
    assert "AUDIO_CONFIRM_DISABLED_TEXT" in body
    refusal = body[:body.index("_audio_sort")]
    assert "can_sort" in refusal, "the refusal must come before anything is filed"

    # The sweep is gated on a signature that matches THIS batch, and the gate returns before the
    # sort rather than falling through to it.
    assert "plan.has_unmarked" in body
    assert "plan.signature" in body
    assert "AUDIO_SWEEP_GATE_PROMPT" in body
    gate = body.index("plan.has_unmarked")
    assert gate < body.index("_audio_sort"), "the gate must precede the sort"

    # Sweeping is passed explicitly, so an unmarked clip can never be discarded by default.
    assert "sweep_unmarked=" in body
    sort_body = ast.unparse(_func(gui_tree, "_audio_sort"))
    assert "unmarked_as=" in sort_body
    assert "fork_audio_review.PENDING" in sort_body, (
        "a caller without confirmation must pass PENDING, which leaves unmarked clips alone")


def test_the_gallery_releases_its_players_before_confirm_moves_anything(gui_source: str):
    """Windows will not rename a file a player still holds open, and the clip the operator just
    listened to is exactly the one that would refuse to move. The listener is on `pointerdown` in
    the CAPTURE phase, so it runs before Gradio sends the click.

    [R7.2] Matched by CLASS rather than by one button id, because there are now four buttons that
    can cause a move -- both tabs' Confirm and both tabs' Proceed. A hard-coded id list would have
    to be kept in step with the markup by hand, and the one that got forgotten would be the one
    that failed with a sharing violation.
    """
    block = _gallery_block(gui_source)
    assert "pointerdown" in block
    assert ".r7-release-audio" in block
    assert "stop()" in block
    # Every button that can move a file carries the class.
    for button in ("audio_confirm_btn", "audio_proceed_btn",
                   "audio_preview_confirm_btn", "audio_preview_proceed_btn"):
        start = gui_source.index(f"{button} = gr.Button(")
        declaration = gui_source[start:gui_source.index(")", gui_source.index(
            "elem_id", start))]
        assert "r7-release-audio" in declaration, f"{button} does not release the player"


def test_the_cutters_destination_fields_validate_immediately_too(gui_tree: ast.Module):
    """[R7.2 Phase 4] The owner's reported defect was on this surface as well: both destination
    boxes set to the same folder, and sorting proceeding anyway. Each field gets its own binding,
    registered explicitly rather than in a loop so these per-widget assertions can find it."""
    for field in ("audio_library_folder", "audio_discarded_folder"):
        written = _wired(gui_tree, field, "outputs", "change")
        assert written == {"audio_destination_status"}, field
        read = _wired(gui_tree, field, "inputs", "change")
        assert read == {"audio_library_folder", "audio_discarded_folder", "audio_review_state"}

    handler = ast.unparse(_func(gui_tree, "_on_audio_destinations_change"))
    assert "destination_feedback" in handler
    # It reports; it never sorts, writes a folder or repaints the gallery.
    for forbidden in ("sort_clips", "file_entry", "os.makedirs", "_audio_render", "os.rename"):
        assert forbidden not in handler, f"the change handler reaches {forbidden}"


def test_the_cutter_no_longer_uses_the_single_destination_helper(gui_tree: ast.Module):
    """`audio_library.destination_problem` checks ONE folder, so it cannot express "these two are
    the same folder" at all -- which is exactly the check that was missing."""
    core_fn = ast.unparse(_func(gui_tree, "_audio_confirm_core"))
    assert "destination_problem" not in core_fn
    assert "fork_audio_review.validate_destinations" in core_fn


def test_the_sweep_gate_buttons_are_hidden_until_confirm_finds_unmarked_clips(gui_source: str):
    for button in ("audio_proceed_btn", "audio_sweep_cancel_btn"):
        start = gui_source.index(f"{button} = gr.Button(")
        declaration = gui_source[start:gui_source.index("elem_id", start)]
        assert "visible=False" in declaration, button


def test_the_gate_is_revealed_and_hidden_by_the_server_not_the_page(gui_tree: ast.Module):
    """A Proceed the browser could reveal would be a Proceed with no counts behind it."""
    for name in ("_on_audio_confirm", "_on_audio_cancel"):
        body = ast.unparse(_func(gui_tree, name))
        assert "gr.update(visible=" in body, name
    gallery_js_free = ast.unparse(_func(gui_tree, "_audio_confirm_core"))
    assert "visible" not in gallery_js_free, (
        "the core decides WHETHER to gate; the handler owns the widget update")


def test_the_cut_records_its_run_directory_so_the_results_can_be_opened_later(gui_tree: ast.Module):
    """"Open results in Audio Preview" needs the folder the cut actually wrote to, not a widget the
    operator may have edited since."""
    written = _wired(gui_tree, "audio_cut_btn", "outputs")
    assert "audio_run_dir_state" in written
    cut = _func(gui_tree, "_on_audio_cut")
    final = [node for node in ast.walk(cut) if isinstance(node, ast.Return)][-1]
    assert "run_directory" in ast.unparse(final)


def test_the_cut_refuses_mp3_up_front_when_the_encoder_is_missing(gui_tree: ast.Module):
    """Writing a folder of unplayable files and reporting success would be worse than refusing.
    The probe runs BEFORE anything is created."""
    cut = ast.unparse(_func(gui_tree, "_on_audio_cut"))
    assert "_audio_mp3_problem" in cut
    assert cut.index("_audio_mp3_problem") < cut.index("os.makedirs")

    problem = ast.unparse(_func(gui_tree, "_audio_mp3_problem"))
    assert "build_encoders_command" in problem
    assert "encoder_available" in problem
    # A probe that cannot be RUN must refuse, not pass.
    assert "FFmpeg is not available" in problem


def test_the_export_format_selector_offers_every_format_the_cutter_can_write(gui_tree: ast.Module):
    """[R7.2 Phase 5] The gap the first browser smoke test found.

    The core, the handler, the argv builder, the manifest and the encoder probe all supported MP3
    while the `gr.Radio` still listed two choices — so every unit test passed and the operator had
    no way to select it. The selector is now pinned against `FORMATS` **by value**, so adding a
    format to the core without offering it fails here rather than in somebody's hands.
    """
    body = _audio_tab_body(gui_tree)
    tree = ast.parse(body)
    radio = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and ast.unparse(node.func) == "gr.Radio"
                 and any(kw.arg == "elem_id" and "audio-format" in ast.unparse(kw.value)
                         for kw in node.keywords))
    choices = next(kw for kw in radio.keywords if kw.arg == "choices").value
    values = [ast.unparse(pair.elts[1]) for pair in choices.elts]
    assert values == [f"fork_audio.FORMAT_{name.upper()}" for name in cutter.FORMATS], values

    # WAV stays the default, and stays FIRST, because several callers rely on it.
    default = next(kw for kw in radio.keywords if kw.arg == "value")
    assert ast.unparse(default.value) == "fork_audio.FORMAT_WAV"
    assert values[0] == "fork_audio.FORMAT_WAV"

    # The MP3 label names the bitrate and says plainly that it is the lossy one.
    labels = [ast.unparse(pair.elts[0]) for pair in choices.elts]
    assert "lossy" in labels[-1].lower()
    assert "DEFAULT_MP3_BITRATE_KBPS" in labels[-1]


def test_the_cut_report_is_honest_about_mp3_being_a_lossy_re_encode(gui_tree: ast.Module):
    """The owner asked for this explicitly: the advantage is smaller files, not better audio, and
    MP3-to-MP3 is a re-encode rather than a stream copy."""
    line = ast.unparse(_func(gui_tree, "_audio_format_line"))
    assert "LOSSY" in line
    assert "including from an MP3 source" in line
    assert "not better audio" in line
    assert "no stream copy" in line


def test_the_manifest_written_by_a_cut_carries_the_encoding_block(gui_tree: ast.Module):
    cut = ast.unparse(_func(gui_tree, "_on_audio_cut"))
    assert "fork_audio.encoding_record(settings['format'])" in cut
    assert "manifest_payload(info, segments, settings, written, encoding)" in cut


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
    """A later phase is where that would live, and R7 is deliberately not it.

    R8 note: the span is now the Audio Cutter tab up to the tab that follows it, which the
    re-order changed from Clips Preview to Audio Mix. Scoping it to this tab is the point -- the
    old bound happened to sweep two unrelated tabs as well, which would have made an unrelated
    tab's wording fail this assertion.
    """
    audio = gui_source.index("gr.Tab(LABEL_TAB_AUDIO_CUTTER")
    block = gui_source[audio:gui_source.index("gr.Tab(LABEL_TAB_AUDIO_MIX")]
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


# =============================================================================================
# [R7.2] The review gallery LEFT this module, and the byte budget went with it
#
# R7.1 built the tile grid in `audio_cutter` and embedded each clip as a base64 data URI under a
# cumulative budget. The budget failed in real use exactly as a budget must -- a valid clip became
# unplayable because EARLIER clips had spent the page's allowance -- so R7.2 replaced it with lazy
# playback through an authorized endpoint and moved the gallery to `beatsync_fork.audio_review`,
# which now has two callers.
#
# The gallery's own behaviour is covered in `tests/test_audio_review.py`. What belongs HERE is the
# removal: this module must not keep a second, divergent implementation, and the cap must be gone
# rather than merely raised.
# =============================================================================================

@pytest.mark.parametrize("gone", ["gallery_markup", "decision_counts", "counts_line",
                                  "MAX_EMBEDDED_BYTES", "DEFAULT_PAGE_SIZE", "_audio_element",
                                  "PENDING", "SELECTED", "REJECTED", "DECISIONS"])
def test_the_cutter_keeps_no_second_gallery_or_decision_vocabulary(gone):
    """The whole point of Phase 1, plus the reason it is a move rather than a copy.

    `MAX_EMBEDDED_BYTES` must be **absent**, not raised: a bigger number is the same defect with a
    later threshold. And the decision words must live in exactly one module, because two copies of
    PENDING/SELECTED/REJECTED is how the two review surfaces come to disagree about what a tile
    means.
    """
    assert not hasattr(cutter, gone), (
        f"audio_cutter still defines {gone}; the gallery moved to audio_review and a "
        f"compatibility alias would leave two names for one concept")
    assert gone not in cutter.__all__, f"{gone} is still exported"


def test_the_grid_the_gui_renders_carries_urls_and_never_bytes(gui_source: str):
    """`preview_markup` survives as the single-clip fallback and keeps its own small cap. What must
    not survive is a *grid* of inlined clips, so no handler on the cut-and-review path may reach
    base64 at all."""
    tree = ast.parse(gui_source)
    for name in ("_on_audio_cut", "_audio_render", "_audio_load_view", "_audio_tiles"):
        body = ast.unparse(_func(tree, name))
        assert "base64" not in body, f"{name} reaches base64"
        assert "b64encode" not in body, f"{name} reaches b64encode"
    # And the markup builder itself hands out an opaque URL.
    from beatsync_fork import audio_review as review

    assert review.media_url("S", "P", "C").startswith("/r7")
    assert "base64" not in review.gallery_html(
        [review.AudioTile(clip_id="C", number=1, name="a.wav", duration=1.0)],
        {}, "S", "P")


# --- the interaction layer -------------------------------------------------------------------

def _gallery_block(gui_source: str) -> str:
    """The AUDIO_GALLERY_JS literal.

    Sliced from the ASSIGNMENT to the next `CLIPS_GALLERY_JS` **after** it. Searching from the
    start of the file was the first attempt and silently produced an empty string: this block's own
    comment mentions `CLIPS_GALLERY_JS` while explaining why the two scripts are separate, and that
    mention sorts earlier than the assignment.
    """
    start = gui_source.index("AUDIO_GALLERY_JS = ")
    return gui_source[start:gui_source.index("CLIPS_GALLERY_JS = ", start)]


@pytest.fixture(scope="module")
def gallery_js(gui_source: str) -> str:
    return _gallery_block(gui_source)


def test_only_one_clip_can_sound_at_a_time(gallery_js: str):
    """[R7.2] Now structural rather than bookkept: there is **one** `<audio>` in the document,
    created on first hover and re-pointed at whichever tile the pointer is over. R7.1 had one
    element per tile and tracked which was sounding; a single element cannot play two clips."""
    assert "stop();" in gallery_js
    assert "holder = null" in gallery_js
    assert gallery_js.count("document.createElement('audio')") == 1
    assert "if (player) { return player; }" in gallery_js, (
        "the player must be created once and reused, not per tile")


def test_hover_plays_and_loops_and_leaving_resets(gallery_js: str):
    assert "mouseover" in gallery_js and "mouseout" in gallery_js
    assert "player.loop = true" in gallery_js
    assert "currentTime = 0" in gallery_js


def test_leaving_a_tile_releases_the_source_not_just_the_playback(gallery_js: str):
    """[R7.2] Two things depend on the source actually being dropped: the HTTP fetch ends, and
    Windows releases the handle the server opened -- `os.rename` will not move a file another
    handle still has open, so the clip just listened to is the one that would refuse to sort.

    `src=''` is deliberately NOT the mechanism: in Chrome that re-requests the page URL.
    """
    assert "removeAttribute('src')" in gallery_js
    assert "player.load()" in gallery_js
    assert "player.src = ''" not in gallery_js


def test_the_player_is_lazy_and_range_capable(gallery_js: str):
    """`preload='metadata'` is what makes `duration` answerable for Shift+wheel without pulling
    the whole clip; the endpoint serves byte ranges, so a seek fetches only what it needs."""
    assert "preload = 'metadata'" in gallery_js
    assert "dataset.audio" in gallery_js, "a tile carries a URL, not bytes"


def test_a_real_playback_failure_is_reported_as_one(gallery_js: str):
    """[R7.2] There is no loading limit any more, so the only failures left are genuine ones. They
    must read as what they are, and the tile must stay classifiable -- an unplayable clip is still
    one the operator may want to keep."""
    assert "MediaError" in gallery_js or "player.error" in gallery_js
    assert "could not be decoded" in gallery_js
    assert "not playable here" in gallery_js
    assert "r7-failed" in gallery_js
    assert "not loaded for preview" not in gallery_js, (
        "the page-budget message must be gone, not reworded")


def test_the_three_mouse_gestures_are_bound(gallery_js: str):
    assert "contextmenu" in gallery_js and "preventDefault" in gallery_js
    assert "auxclick" in gallery_js
    assert "SELECTED" in gallery_js and "REJECTED" in gallery_js and "PENDING" in gallery_js


def test_the_keyboard_shortcuts_and_arrows_are_bound(gallery_js: str):
    for key in ("'s'", "'r'", "'p'", "arrowright", "arrowleft", "arrowdown", "arrowup"):
        assert key in gallery_js, key


def test_shift_wheel_uses_the_established_one_two_five_convention(gallery_js: str):
    """The same steps the Clips Preview uses, so the two galleries do not behave differently under
    the same gesture."""
    assert "seekStep" in gallery_js
    assert "duration < 20" in gallery_js and "duration <= 90" in gallery_js
    assert "passive: false" in gallery_js, "preventDefault is ignored on a passive wheel listener"
    assert "deltaX" in gallery_js, "Chrome re-routes a shifted wheel to deltaX on some devices"


def test_blocked_autoplay_is_handled_and_never_faked_with_muted_audio(gallery_js: str):
    """The owner needs to HEAR the clips. Falling back to muted playback would look like it worked
    while nothing came out.

    [R7.2] The banner is matched by CLASS now, because there are two of them -- one per review tab
    -- and a blocked autoplay in either must light up both: the browser's gesture requirement is
    per page, not per tab.
    """
    assert "NotAllowedError" in gallery_js, "the autoplay refusal must be told from a load failure"
    assert "audio.muted = false" in gallery_js
    assert "muted = true" not in gallery_js
    assert ".r7-enable-hover" in gallery_js
    assert gallery_js.count("querySelectorAll('.r7-enable-hover')") >= 2, (
        "both banners are lit on refusal and cleared on the gesture")


def test_the_name_box_never_classifies(gallery_js: str):
    """Typing in a filename must not keep or discard the clip under the cursor."""
    assert gallery_js.count("r7-name") >= 3


def test_the_script_installs_itself_once(gallery_js: str):
    assert "__r7Installed" in gallery_js


def test_the_gallery_script_is_separate_from_the_clips_one(gui_source: str):
    """Two registrations, not one merged blob: the galleries share no behaviour beyond being
    grids of tiles."""
    assert gui_source.count("js=AUDIO_GALLERY_JS") == 1
    assert gui_source.count("js=CLIPS_GALLERY_JS") == 1


# --- the payload ------------------------------------------------------------------------------

def _merge(gui_tree: ast.Module):
    """The real `_audio_merge_payload`, AST-extracted and executed against a stub namespace.

    `gui.py` cannot be imported on a bare interpreter, so the body is run against a synthesised
    `fork_audio_review` carrying only the decision vocabulary it reads. That is the real function,
    not a reimplementation of it.
    """
    from beatsync_fork import audio_review as review

    namespace = {"json": __import__("json"), "fork_audio_review": review}
    exec(ast.unparse(_func(gui_tree, "_audio_merge_payload")), namespace)
    return namespace["_audio_merge_payload"]


#: A view with two clips on the page, mapping the browser's per-page ids to stable identities.
_VIEW = {"identity": {"cA": "idA", "cB": "idB"}, "decisions": {}, "names": {}}


@pytest.mark.parametrize("junk", ["", None, "not json", "[]", '{"decisions": 5}', "null",
                                  '{"decisions": "SELECTED"}', '{"names": [1, 2]}'])
def test_a_malformed_payload_merges_nothing_and_discards_nothing(gui_tree: ast.Module, junk):
    """An unreadable payload must not guess -- and, [R7.2], must not CLEAR either.

    Merging nothing leaves the server's existing state standing. The opposite choice would let one
    malformed update silently discard a page of real decisions, which matters much more now that
    the record spans pages.
    """
    merge = _merge(gui_tree)
    standing = dict(_VIEW, decisions={"idA": "SELECTED"}, names={"idA": "keep me"})
    merged = merge(standing, junk)
    assert merged["decisions"] == {"idA": "SELECTED"}
    assert merged["names"] == {"idA": "keep me"}


def test_a_well_formed_payload_is_read_and_keyed_by_identity(gui_tree: ast.Module):
    """The browser sends per-page clip ids; the server stores by FILE identity. That translation
    is what makes a decision survive paging and stop applying when the bytes change."""
    merge = _merge(gui_tree)
    merged = merge(_VIEW, '{"decisions": {"cA": "SELECTED"}, "names": {"cA": "hello"}}')
    assert merged["decisions"] == {"idA": "SELECTED"}
    assert merged["names"] == {"idA": "hello"}


def test_a_clip_id_not_on_this_page_cannot_reach_the_record(gui_tree: ast.Module):
    """A payload must not be able to decide for a clip the operator is not looking at."""
    merge = _merge(gui_tree)
    merged = merge(_VIEW, '{"decisions": {"cZZZ": "SELECTED"}, "names": {"cZZZ": "x"}}')
    assert merged["decisions"] == {}
    assert merged["names"] == {}


def test_an_unknown_decision_word_is_stored_as_pending(gui_tree: ast.Module):
    merge = _merge(gui_tree)
    merged = merge(_VIEW, '{"decisions": {"cA": "NONSENSE"}}')
    assert merged["decisions"] == {"idA": "PENDING"}
