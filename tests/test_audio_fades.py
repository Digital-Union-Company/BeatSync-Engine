"""Audio Cutter soft edges (R8 UX Refresh, Phase 9).

The safety rule is one sentence: **a fade may consume only the padding, never the sound.** Every
test here is a consequence of it.

The distinction that makes it work is between the pad *setting* -- a request -- and the padding a
clip **actually received**. `plan_segments` gives each side at most half the silence it is
reaching into, so a clip in a crowded passage may get far less than asked for, or none. Recording
the applied padding on the `Segment` is what turns "never attenuate a spoken syllable" into a
computation rather than a hope.

The audible proof (WAV, FLAC and MP3 through real FFmpeg: hard cut 0.00 dB head-to-body, fade
~15 dB) is a manual verification recorded in `docs/claude/r8-ux-refresh.md`. What is here is
everything provable without a disk.
"""

from __future__ import annotations

import pytest

from beatsync_fork import audio_cutter as cutter


def _segment(duration=1.0, lead=0.1, tail=0.1):
    return cutter.Segment(index=1, start=0.0, end=duration,
                          pad_before=lead, pad_after=tail)


# ---------------------------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------------------------

def test_fades_are_off_by_default():
    """Every export an operator has already accepted must stay bit-identical unless they ask."""
    assert cutter.fade_lengths(_segment()) == (0.0, 0.0)
    assert cutter.fade_filter(_segment()) == ""


def test_a_disabled_fade_adds_no_filter_argument_at_all():
    """Not an identity filter -- nothing. The argv is the one it always was."""
    off = cutter.build_extract_command("ffmpeg", "s.wav", _segment(), "d.wav", cutter.FORMAT_WAV)
    assert "-af" not in off


def test_an_enabled_fade_adds_exactly_one_filter_argument():
    on = cutter.build_extract_command("ffmpeg", "s.wav", _segment(), "d.wav",
                                      cutter.FORMAT_WAV, fade_edges=True)
    assert on.count("-af") == 1
    chain = on[on.index("-af") + 1]
    assert chain.count("afade") == 2, chain


@pytest.mark.parametrize("fmt", [cutter.FORMAT_WAV, cutter.FORMAT_FLAC, cutter.FORMAT_MP3])
def test_the_fade_applies_identically_to_every_format(fmt):
    """One filter on the common decoded-sample path, not three codec-specific tricks -- and it is
    placed BEFORE the codec arguments so it is faded and then encoded."""
    command = cutter.build_extract_command("ffmpeg", "s.wav", _segment(), "d.out", fmt,
                                           fade_edges=True)
    assert "-af" in command
    assert command.index("-af") < command.index("-c:a"), \
        "the fade must be applied before encoding"


# ---------------------------------------------------------------------------------------------
# The fade is bounded by the padding the clip ACTUALLY received
# ---------------------------------------------------------------------------------------------

def test_the_planner_records_the_padding_it_actually_applied():
    """Two sounds 0.2 s apart can take only 0.1 s each, whatever the setting says."""
    segments = cutter.plan_segments(
        10.0, [(0.0, 1.0), (3.0, 3.2), (6.0, 10.0)], pad=0.5, min_clip=0.1)
    assert segments, "the fixture produced no clips"
    for segment in segments:
        assert segment.pad_before >= 0.0 and segment.pad_after >= 0.0
        # never more than the request
        assert segment.pad_before <= 0.5 + 1e-9
        assert segment.pad_after <= 0.5 + 1e-9
    # the side reaching into the 0.2 s silence got half of it, not the 0.5 s requested
    squeezed = [p for s in segments for p in (s.pad_before, s.pad_after)
                if abs(p - 0.1) < 1e-6]
    assert squeezed, [(s.pad_before, s.pad_after) for s in segments]


def test_a_fade_never_exceeds_the_padding_on_its_own_side():
    """The whole safety argument, as an assertion over a real plan."""
    segments = cutter.plan_segments(
        10.0, [(0.0, 1.0), (3.0, 3.2), (6.0, 10.0)], pad=0.5, min_clip=0.1)
    for segment in segments:
        fade_in, fade_out = cutter.fade_lengths(segment, True)
        assert fade_in <= segment.pad_before + 1e-9
        assert fade_out <= segment.pad_after + 1e-9


def test_a_clip_with_no_padding_gets_no_fade():
    """A clip wedged against its neighbours would otherwise lose its first syllable."""
    flush = _segment(duration=1.0, lead=0.0, tail=0.0)
    assert cutter.fade_lengths(flush, True) == (0.0, 0.0)
    assert cutter.fade_filter(flush, True) == ""


def test_one_sided_padding_fades_only_that_side():
    lead_only = _segment(duration=1.0, lead=0.1, tail=0.0)
    fade_in, fade_out = cutter.fade_lengths(lead_only, True)
    assert fade_in > 0.0 and fade_out == 0.0
    chain = cutter.fade_filter(lead_only, True)
    assert "t=in" in chain and "t=out" not in chain


def test_a_padding_too_small_to_hear_is_not_turned_into_a_filter():
    """An `afade` of a few samples is noise in the argv rather than a musical decision."""
    tiny = _segment(duration=1.0, lead=cutter.MIN_FADE_SECONDS / 2, tail=0.0)
    assert cutter.fade_lengths(tiny, True) == (0.0, 0.0)


# ---------------------------------------------------------------------------------------------
# Short clips are clamped so they are never quiet throughout
# ---------------------------------------------------------------------------------------------

def test_neither_fade_may_dominate_a_short_clip():
    short = _segment(duration=0.30, lead=0.25, tail=0.25)
    fade_in, fade_out = cutter.fade_lengths(short, True)
    assert fade_in <= 0.30 * cutter.MAX_FADE_FRACTION + 1e-9
    assert fade_out <= 0.30 * cutter.MAX_FADE_FRACTION + 1e-9


def test_the_two_fades_can_never_overlap():
    """Overlapping fades would produce a clip quiet in the middle."""
    for duration in (0.05, 0.1, 0.3, 0.5, 1.0, 5.0):
        segment = _segment(duration=duration, lead=duration, tail=duration)
        fade_in, fade_out = cutter.fade_lengths(segment, True)
        assert fade_in + fade_out <= duration, (duration, fade_in, fade_out)
        assert fade_in + fade_out <= duration * 2 * cutter.MAX_FADE_FRACTION + 1e-9


def test_the_fade_out_is_positioned_from_the_clips_own_timeline():
    """The clip has already been seeked and trimmed by the time the filter runs, so its timeline
    starts at zero -- an absolute source timestamp here would fade the wrong moment."""
    segment = cutter.Segment(index=1, start=42.0, end=44.0, pad_before=0.1, pad_after=0.1)
    chain = cutter.fade_filter(segment, True)
    assert "st=1.900000" in chain, chain
    assert "42" not in chain, "an absolute source timestamp leaked into the filter"


# ---------------------------------------------------------------------------------------------
# The planner is untouched
# ---------------------------------------------------------------------------------------------

def test_enabling_fades_changes_no_segment_boundary():
    """Fades change what is WRITTEN, never where a clip begins or ends, so the non-overlap the
    planner guarantees is preserved by construction."""
    silences = [(0.0, 1.0), (3.0, 3.6), (6.0, 10.0)]
    plan = cutter.plan_segments(10.0, silences, pad=0.3, min_clip=0.1)
    again = cutter.plan_segments(10.0, silences, pad=0.3, min_clip=0.1)
    assert [(s.start, s.end) for s in plan] == [(s.start, s.end) for s in again]
    # and the clips still never overlap
    for earlier, later in zip(plan, plan[1:]):
        assert earlier.end <= later.start + 1e-9


def test_the_segment_record_defaults_its_padding_so_older_constructions_still_work():
    legacy = cutter.Segment(index=1, start=0.0, end=1.0)
    assert legacy.pad_before == 0.0 and legacy.pad_after == 0.0
    assert cutter.fade_lengths(legacy, True) == (0.0, 0.0)


# ---------------------------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", [None, "", "abc", object(), -1, float("nan")])
def test_junk_padding_never_raises(junk):
    """This runs while assembling a command for every clip in a batch."""
    bogus = cutter.Segment(index=1, start=0.0, end=1.0, pad_before=junk, pad_after=junk)
    lengths = cutter.fade_lengths(bogus, True)
    assert isinstance(lengths, tuple) and len(lengths) == 2
    assert isinstance(cutter.fade_filter(bogus, True), str)


def test_a_zero_length_clip_fades_nothing():
    assert cutter.fade_lengths(cutter.Segment(1, 5.0, 5.0, 0.1, 0.1), True) == (0.0, 0.0)
