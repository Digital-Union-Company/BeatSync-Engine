"""[FORK] R7.3 — Audio Layers voice overflow: Strict / Skip / Trim.

Three policies for one question: what happens when a voice clip has nowhere legal left to play.
`strict` is V1 and the default; `skip` drops the clip and carries on; `trim` additionally allows
**one** partial clip and then stops.

The suite is deliberately split the way the feature is:

1. **the pure planner** (`beatsync_fork.audio_mix`) — ordinary imports, bare interpreter;
2. **the executor seam** (`src/audio_mixdown.py`) — AST-extracted real bodies, per the house
   technique in `test_audio_mixdown.py`, which this file reuses rather than reimplements;
3. **the GUI wiring** — AST assertions over the real `src/gui.py`;
4. **real FFmpeg** — because "the planner shortened a number" and "the voice actually stops" are
   different claims, and only the second one is the feature. Skipped without a binary.

The measurement that matters most, and the reason section 4 is shaped the way it is: when the
**music end** is what bounds a trim, a trimmed mix is acoustically identical to the old
overrun-then-master-trim behaviour (`apad`+`atrim=end=D` already cut the tail). The trim is only
*observable* when something else bounds the room — i.e. an avoided `drop`/`finale` span. So the
decisive real-audio test bounds the room with an avoided section and then proves the voice is
silent after the planned end. A test that bounded it with the music end would have passed whether
or not the `atrim` was ever emitted.
"""

from __future__ import annotations

import ast
import os
import subprocess

import pytest

from beatsync_fork import audio_mix as fork_audio_mix

from test_audio_mixdown import (
    FFMPEG_BIN,
    RATE,
    FakeCompleted,
    FakeSubprocess,
    load_mixdown,
    needs_ffmpeg,
    read_left,
    real_probe_duration,
    write_tone,
)

M = fork_audio_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_AUDIO_MIX = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "audio_mix.py")


# ===========================================================================
# fixtures: one deterministic 35 s track, beats every 0.5 s
# ===========================================================================

MUSIC = 35.0
BEATS = tuple(i * 0.5 for i in range(71))          # 0.0 .. 35.0

#: No avoided span, so the music end is the only bound — and deliberately ONE section, so there is
#: no interior section start for the bounded anchor preference to pull a clip onto. That preference
#: is V1 behaviour with its own coverage in `test_audio_mix.py`; mixing it in here would make every
#: cursor assertion below depend on it, which is how an overflow test ends up asserting the wrong
#: arithmetic and looking like a planner bug.
PLAIN_SECTIONS = M.project_sections([
    {"start": 0.0, "end": 35.0, "type": "verse"},
])

#: A `drop` from 20 s to 26 s — the bound that makes a trim observable.
DROP_SECTIONS = M.project_sections([
    {"start": 0.0, "end": 8.0, "type": "intro"},
    {"start": 8.0, "end": 20.0, "type": "verse"},
    {"start": 20.0, "end": 26.0, "type": "drop"},
    {"start": 26.0, "end": 35.0, "type": "verse"},
])


def voices(*durations):
    return tuple(M.VoiceInput(index=i, path=f"C:/voice/{i + 1:02d}_clip.wav", duration=d)
                 for i, d in enumerate(durations))


def plan(policy, durations, sections=PLAIN_SECTIONS, avoid_drops=True,
         start_delay=2.0, min_gap=1.0, music=MUSIC):
    config = M.AudioMixConfig(start_delay_seconds=start_delay, min_gap_seconds=min_gap,
                              avoid_drops=avoid_drops, voice_overflow=policy)
    return M.plan_voice_placements(music, BEATS, sections, voices(*durations), config)


def names(items):
    return [os.path.basename(i.path) for i in items]


# ===========================================================================
# 1. NORMALISATION — unrecognised must mean STRICT, never permission
# ===========================================================================


@pytest.mark.parametrize("policy", M.OVERFLOW_POLICIES)
def test_each_declared_policy_round_trips(policy):
    assert M.normalize_overflow_policy(policy) == policy
    assert M.AudioMixConfig(voice_overflow=policy).voice_overflow == policy


@pytest.mark.parametrize("value", [
    None, "", "STRICT", "Skip", "skip ", "trim\n", 0, 1, True, False, 2.5, [], {},
    "strict;skip", "Skip clips that do not fit", object(),
])
def test_anything_unrecognised_resolves_to_strict(value):
    """The conservative direction, and the only safe one.

    A malformed widget value must never be read as permission to drop or shorten the user's
    speech — so this is not "fall back to something sensible", it is "fall back to the behaviour
    that refuses". Note the near-misses in the list: case variants, a trailing space, and the
    *display label* of a real policy. Matching is exact on the internal name precisely so no
    handler ever has to turn prose back into a policy.
    """
    assert M.normalize_overflow_policy(value) == M.OVERFLOW_STRICT
    assert M.AudioMixConfig(voice_overflow=value).voice_overflow == M.OVERFLOW_STRICT


def test_the_default_is_strict_and_the_legacy_construction_is_untouched():
    """Backward compatibility, stated as the two things that could actually break.

    An existing *keyword* construction must still build the V1 config, and an existing *positional*
    one must still land every value in the field it landed in before — which is what "appended
    last" buys and what an inserted field would silently destroy.
    """
    assert M.DEFAULT_OVERFLOW_POLICY == M.OVERFLOW_STRICT
    assert M.AudioMixConfig().voice_overflow == M.OVERFLOW_STRICT

    positional = M.AudioMixConfig(3.5, 2.5, False, 60)
    assert (positional.start_delay_seconds, positional.min_gap_seconds,
            positional.avoid_drops, positional.music_under_voice_percent) == (3.5, 2.5, False, 60)
    assert positional.voice_overflow == M.OVERFLOW_STRICT

    r71 = M.AudioMixConfig(2.0, 1.0, True, 35, True, 2.0)
    assert (r71.auto_balance, r71.music_return_seconds) == (True, 2.0)
    assert r71.voice_overflow == M.OVERFLOW_STRICT


def test_the_policy_name_is_readable_and_total():
    for policy in M.OVERFLOW_POLICIES:
        assert M.OVERFLOW_POLICY_NAMES[policy]
        assert M.AudioMixConfig(voice_overflow=policy).overflow_policy_name \
            == M.OVERFLOW_POLICY_NAMES[policy]


# ===========================================================================
# 2. WHEN EVERYTHING FITS, ALL THREE POLICIES AGREE
# ===========================================================================


def test_all_three_policies_place_identically_when_every_clip_fits():
    """The acceptance condition the owner named first, asserted on the whole plan rather than on a
    count: identical starts, identical ends, identical anchors, identical duck envelopes, nothing
    trimmed and nothing skipped. If this ever diverges, changing the setting changes the *sound* of
    a render that never overflowed, which is the one thing it must never do.
    """
    plans = [plan(p, [4.0, 4.0, 3.0]) for p in M.OVERFLOW_POLICIES]
    for other in plans[1:]:
        assert [(x.index, x.start, x.end, x.duration, x.anchor, x.trimmed)
                for x in other.placements] == \
               [(x.index, x.start, x.end, x.duration, x.anchor, x.trimmed)
                for x in plans[0].placements]
        assert other.duck_events == plans[0].duck_events
        assert other.skipped == ()
        assert other.trimmed_count == 0
    assert plans[0].voice_count == 3


def test_a_no_overflow_plan_reports_zero_trimmed_and_zero_skipped():
    p = plan(M.OVERFLOW_SKIP, [4.0, 4.0])
    assert (p.voices_supplied, p.whole_count, p.trimmed_count, p.skipped_count) == (2, 2, 0, 0)
    assert "2 clips supplied, 2 used in full, 0 trimmed, 0 skipped" in p.overflow_summary_line()


# ===========================================================================
# 3. STRICT IS V1, CHARACTER FOR CHARACTER
# ===========================================================================


def test_strict_still_returns_the_historical_placement_failure():
    """The owner's actual error, reproduced in shape: clip number, duration, cursor.

    The message is asserted as a whole string, not by substring, because `gui.py` surfaces it
    verbatim and `audio_mixdown` turns it into an `AudioMixPlanError` without rewording it. R7.3
    adds policies; it must not reword the refusal anyone has already seen.
    """
    failure = plan(M.OVERFLOW_STRICT, [4.0, 80.0, 3.0])
    assert isinstance(failure, M.PlacementFailure)
    assert failure.index == 1
    assert failure.duration == 80.0
    assert failure.after_seconds == 7.0
    assert failure.reason == "Voice 2 (80.0s) has no legal placement after 7.0s"


def test_strict_refuses_at_the_FIRST_impossible_clip_and_plans_nothing_after_it():
    """A refusal is not a partial plan. Nothing downstream may receive placements from a run that
    failed, which is what makes the `PlacementFailure` return type (rather than a plan with an
    error field) the right shape."""
    failure = plan(M.OVERFLOW_STRICT, [4.0, 80.0, 3.0, 2.0])
    assert isinstance(failure, M.PlacementFailure)
    assert not hasattr(failure, "placements")


def test_strict_never_trims_even_when_room_exists():
    """There is 23 s of legal room after the cursor, and `strict` must use none of it."""
    assert isinstance(plan(M.OVERFLOW_STRICT, [4.0, 4.0, 25.0]), M.PlacementFailure)


# ===========================================================================
# 4. SKIP — and the rule that makes it worth having
# ===========================================================================


def test_skip_drops_only_the_clip_that_does_not_fit():
    p = plan(M.OVERFLOW_SKIP, [4.0, 80.0, 3.0])
    assert not isinstance(p, M.PlacementFailure)
    assert names(p.placements) == ["01_clip.wav", "03_clip.wav"]
    assert names(p.skipped) == ["02_clip.wav"]
    assert (p.voices_supplied, p.whole_count, p.trimmed_count, p.skipped_count) == (3, 2, 0, 1)


def test_a_skipped_clip_does_not_push_the_next_one_out_of_the_song():
    """**The point of the whole policy**, and the easiest thing to get wrong.

    A skip must not advance the cursor: the cursor is the end of the last *placed* clip plus the
    gap, and a clip that was never placed has no end. Here clip 2 (30 s) cannot fit after 7.0 s
    (it would need to start by 5.0 s), and clip 3 (3 s) must therefore land at exactly 7.0 s — the
    same instant clip 2 was tried at — not at some position derived from the clip that failed.
    """
    p = plan(M.OVERFLOW_SKIP, [4.0, 30.0, 3.0])
    assert names(p.placements) == ["01_clip.wav", "03_clip.wav"]
    assert p.placements[0].start == 2.0
    assert p.placements[1].start == 7.0
    assert p.skipped[0].after_seconds == 7.0


def test_skip_keeps_evaluating_every_later_clip_not_just_the_next_one():
    """Three long clips in a row must not end the search; clip 5 still gets its chance.

    All of 34/33/32 s exceed the 28 s of room left after 7.0 s, so each is skipped in turn and the
    3 s clip behind them still lands — at 7.0 s, because none of the three moved the cursor.
    """
    p = plan(M.OVERFLOW_SKIP, [4.0, 34.0, 33.0, 32.0, 3.0])
    assert names(p.placements) == ["01_clip.wav", "05_clip.wav"]
    assert names(p.skipped) == ["02_clip.wav", "03_clip.wav", "04_clip.wav"]
    assert p.placements[1].start == 7.0


def test_skip_never_reorders_and_never_shortens():
    """Order is the user's filename order and stays that way; `skip` shortens nothing at all."""
    p = plan(M.OVERFLOW_SKIP, [4.0, 34.0, 3.0, 2.0])
    assert [x.index for x in p.placements] == sorted(x.index for x in p.placements)
    assert all(not x.trimmed for x in p.placements)
    assert all(x.duration == x.source_duration for x in p.placements)


def test_skip_never_lets_voice_run_past_the_music():
    p = plan(M.OVERFLOW_SKIP, [4.0, 34.0, 10.0, 8.0, 6.0, 4.0, 2.0])
    for placement in p.placements:
        assert placement.end <= MUSIC + 1e-9, placement


def test_skip_respects_avoid_drops_rather_than_treating_it_as_advisory():
    """A clip that cannot fit *outside* the drop is skipped, never slid into it.

    With the drop at 20..26 and the cursor at 7.0, an 11 s clip has 13 s of room before the drop
    and lands; a 15 s clip does not fit before it, must not overlap it, and does not fit in the 9 s
    after it either — so it is skipped. The protected span is never used.
    """
    p = plan(M.OVERFLOW_SKIP, [4.0, 15.0, 11.0], sections=DROP_SECTIONS)
    assert names(p.placements) == ["01_clip.wav", "03_clip.wav"]
    assert names(p.skipped) == ["02_clip.wav"]
    for placement in p.placements:
        assert not (placement.start < 26.0 and placement.end > 20.0), placement


def test_turning_avoid_drops_off_lets_the_same_clip_fit():
    """The control still means what it meant.

    Same input as the test above with `avoid_drops=False`: the clip that was skipped is now placed
    **and runs straight through the drop**. That is the control doing its job, and it proves the
    skip above was caused by the protected span rather than by some unrelated bound — which is the
    part a bounds-only assertion could not establish.
    """
    p = plan(M.OVERFLOW_SKIP, [4.0, 15.0, 11.0], sections=DROP_SECTIONS, avoid_drops=False)
    assert "02_clip.wav" in names(p.placements)
    assert p.skipped == ()
    through = next(x for x in p.placements if x.index == 1)
    assert through.start < 26.0 and through.end > 20.0, through


# ===========================================================================
# 5. TRIM — one partial clip, then stop
# ===========================================================================


def test_trim_shortens_the_first_clip_that_does_not_fit():
    p = plan(M.OVERFLOW_TRIM, [4.0, 4.0, 25.0])
    assert names(p.placements) == ["01_clip.wav", "02_clip.wav", "03_clip.wav"]
    trimmed = p.placements[-1]
    assert trimmed.trimmed is True
    assert trimmed.start == 12.0
    assert trimmed.duration == pytest.approx(23.0)          # 35.0 - 12.0
    assert trimmed.source_duration == 25.0
    assert trimmed.trimmed_away_seconds == pytest.approx(2.0)
    assert trimmed.anchor == M.ANCHOR_TRIMMED
    assert trimmed.end == pytest.approx(MUSIC)


def test_trim_records_both_the_original_and_the_played_duration():
    """"Record original and actual played duration" — and `duration` is the PLAYED one, which is
    what every existing consumer (`end`, the duck envelope, `adelay`, the balance measurement)
    already means by it. Reversing that would have silently mis-ducked every trimmed clip."""
    trimmed = plan(M.OVERFLOW_TRIM, [4.0, 4.0, 25.0]).placements[-1]
    assert trimmed.duration < trimmed.source_duration
    assert trimmed.end == trimmed.start + trimmed.duration


def test_trim_spends_itself_once_and_skips_everything_after():
    """"Trim final usable clip, then skip remaining" — asserted as *skipped with a reason*, not
    merely absent, so a future change cannot drop the tail silently.

    Clip 3 is 3 s and clip 4 is 2 s — both would comfortably have fitted somewhere in a 35 s song.
    They are skipped because the trim was spent, not because they were too long, which is exactly
    the behaviour the owner asked for and the reason the skip reasons are asserted too.
    """
    p = plan(M.OVERFLOW_TRIM, [4.0, 30.0, 3.0, 2.0])
    assert p.trimmed_count == 1
    assert names(p.skipped) == ["03_clip.wav", "04_clip.wav"]
    for skip in p.skipped:
        assert "after the trimmed clip" in skip.reason
    assert (p.voices_supplied, p.whole_count, p.trimmed_count, p.skipped_count) == (4, 1, 1, 2)


def test_trim_places_whole_clips_whole_right_up_to_the_first_overflow():
    """A trim is a last resort, not a style: every clip before it is untouched."""
    p = plan(M.OVERFLOW_TRIM, [4.0, 4.0, 4.0, 25.0])
    whole = p.placements[:-1]
    assert all(not x.trimmed for x in whole)
    assert [x.duration for x in whole] == [4.0, 4.0, 4.0]
    assert [x.start for x in whole] == [2.0, 7.0, 12.0]


def test_trim_starts_on_a_beat():
    p = plan(M.OVERFLOW_TRIM, [4.0, 4.0, 25.0])
    assert p.placements[-1].start in BEATS


def test_trim_uses_only_the_room_avoid_drops_leaves_and_stops_before_the_protected_span():
    """The rule most worth not weakening, now for a partial clip.

    With the drop at 20..26 and the cursor at 7.0, a 20 s clip cannot fit anywhere legal. Trimming
    may use the 13 s before the drop and **not one sample of the drop itself** — so the retained
    clip is exactly 13 s, ending precisely where the protected span begins (half-open, so touching
    the boundary is legal). A trim bounded by the music end instead would have been 28 s and would
    have played straight through the drop.
    """
    p = plan(M.OVERFLOW_TRIM, [4.0, 20.0], sections=DROP_SECTIONS)
    trimmed = p.placements[-1]
    assert trimmed.trimmed is True
    assert trimmed.start == 7.0
    assert trimmed.duration == pytest.approx(13.0)
    assert trimmed.end == pytest.approx(20.0)
    assert trimmed.source_duration == 20.0


def test_trim_skips_rather_than_keeping_a_useless_sliver():
    """Below `MIN_TRIMMED_VOICE_SECONDS` a partial clip is a click, not a word.

    The cursor is driven to 34.8 s by a start delay, leaving 0.2 s of music — under the floor — so
    the clip is skipped and the reason says why, naming the floor rather than reporting a generic
    "does not fit".
    """
    p = plan(M.OVERFLOW_TRIM, [10.0], start_delay=34.8)
    assert p.placements == ()
    assert p.skipped_count == 1
    assert "could be kept by trimming" in p.skipped[0].reason
    assert M.MIN_TRIMMED_VOICE_SECONDS == 0.5


def test_a_sliver_skip_does_not_close_the_sequence_the_way_a_real_trim_does():
    """A sliver skip is a SKIP, not a spent trim — and the difference is observable in the reasons.

    With the cursor driven past the end of the music, no clip can be usefully trimmed, so all three
    are skipped. Each must carry its own "could be kept by trimming" reason; **none** may carry the
    "after the trimmed clip" reason, because no trim ever happened. If a failed trim wrongly closed
    the sequence, the second and third clips would be reported as collateral of a trim that does not
    exist — a plausible-looking report about a thing that never occurred.
    """
    p = plan(M.OVERFLOW_TRIM, [10.0, 8.0, 6.0], start_delay=34.8)
    assert p.placements == ()
    assert p.trimmed_count == 0
    assert p.skipped_count == 3
    for skip in p.skipped:
        assert "could be kept by trimming" in skip.reason, skip
        assert "after the trimmed clip" not in skip.reason, skip


def test_trim_never_extends_beyond_the_music():
    for durations in ([40.0], [4.0, 40.0], [4.0, 4.0, 25.0], [34.0, 34.0]):
        p = plan(M.OVERFLOW_TRIM, durations)
        assert not isinstance(p, M.PlacementFailure)
        for placement in p.placements:
            assert placement.end <= MUSIC + 1e-9, (durations, placement)


# ===========================================================================
# 6. REPORTING — filenames, not only numbers
# ===========================================================================


def test_the_accounting_always_adds_up():
    for policy in (M.OVERFLOW_SKIP, M.OVERFLOW_TRIM):
        for durations in ([4.0, 4.0], [4.0, 25.0, 3.0], [4.0, 34.0, 33.0, 3.0], [40.0, 3.0]):
            p = plan(policy, durations)
            assert p.whole_count + p.trimmed_count + p.skipped_count == p.voices_supplied
            assert p.voices_supplied == len(durations), (policy, durations)


def test_the_report_names_every_skipped_file_with_a_reason():
    """"The report should identify the actual filenames, not only numbers." Asserted on the real
    `report_lines()` output, which is the exact text `gui.py` puts in the read-out and the console
    log — the GUI formats nothing of its own."""
    p = plan(M.OVERFLOW_SKIP, [4.0, 34.0, 33.0, 3.0])
    text = "\n".join(p.report_lines())
    assert "02_clip.wav" in text
    assert "03_clip.wav" in text
    assert "Skipped 2 clips:" in text
    assert "no legal placement" in text
    assert "4 clips supplied, 2 used in full, 0 trimmed, 2 skipped" in text
    assert "Music length: 00:35" in text


def test_the_report_marks_a_trimmed_row_with_both_durations():
    p = plan(M.OVERFLOW_TRIM, [4.0, 4.0, 25.0])
    text = "\n".join(p.report_lines())
    assert "TRIMMED: 23.0s of 25.0s played" in text
    assert "03_clip.wav" in text


def test_the_report_states_the_policy_and_the_accounting_even_when_nothing_overflowed():
    """Unconditional, on purpose. A user who supplied 30 clips must be able to read how many played
    *without* having to notice the absence of a warning — "nothing to report" and "I did not look"
    have to produce different output."""
    text = "\n".join(plan(M.OVERFLOW_SKIP, [4.0, 4.0]).report_lines())
    assert "When voice does not fit: Skip clips that do not fit" in text
    assert "2 clips supplied, 2 used in full, 0 trimmed, 0 skipped" in text
    assert "Skipped" not in text, "a clean plan must not grow 'Skipped 0 clips' noise"


def test_the_legacy_summary_line_is_unchanged():
    """The pre-R7.3 panel line is preserved character for character; the accounting is a second
    line. An operator who never hits overflow should not find their familiar line reworded."""
    p = plan(M.OVERFLOW_STRICT, [4.0, 4.0])
    assert p.summary_line() == "Audio Layers: 2 voice clips · music 35% · 2 duck events"


def test_the_overflow_line_matches_the_shape_the_owner_asked_for():
    p = plan(M.OVERFLOW_TRIM, [4.0, 30.0, 3.0])
    assert p.overflow_summary_line() == (
        "Audio Layers: 3 clips supplied, 1 used in full, 1 trimmed, 1 skipped. "
        "Music length: 00:35.")


@pytest.mark.parametrize("seconds,expected", [
    (0.0, "00:00"), (5.0, "00:05"), (35.0, "00:35"), (62.4, "01:02"), (192.0, "03:12"),
    (3599.0, "59:59"), (3600.0, "1:00:00"), (3661.0, "1:01:01"),
])
def test_the_clock_formatter(seconds, expected):
    assert M.format_clock(seconds) == expected


@pytest.mark.parametrize("bad", [None, "x", float("nan"), float("inf"), -1.0, [], {}])
def test_an_unreadable_duration_says_so_instead_of_asserting_a_zero_length_song(bad):
    assert M.format_clock(bad) == "--:--"


def test_a_plan_built_without_overflow_knowledge_still_reports_a_truthful_supply_count():
    """`voices_supplied` is back-filled from `used + skipped` when it was not given, so a plan
    constructed anywhere that predates R7.3 reports 3 clips rather than 0. A supplied positive
    value always wins, which is what keeps `dataclasses.replace` (used twice in
    `build_mixed_master`) from resetting it."""
    p = plan(M.OVERFLOW_STRICT, [4.0, 4.0, 3.0])
    bare = M.AudioMixPlan(p.music_duration, p.placements, p.duck_events, p.config)
    assert bare.voices_supplied == 3

    import dataclasses
    assert dataclasses.replace(p, sfx_placements=("x",)).voices_supplied == 3


def test_source_duration_defaults_to_the_played_duration_for_a_legacy_placement():
    legacy = M.VoicePlacement(0, "a.wav", 4.0, 2.0, 6.0, "verse", M.ANCHOR_BEAT, 0.0)
    assert legacy.source_duration == 4.0
    assert legacy.trimmed is False
    assert legacy.trimmed_away_seconds == 0.0


# ===========================================================================
# 7. THE EXECUTOR SEAM — a real atrim, and only where it belongs
# ===========================================================================


def trimmed_plan(played=3.0, source=20.0, start=5.0, music=30.0, policy=M.OVERFLOW_TRIM):
    config = M.AudioMixConfig(voice_overflow=policy)
    placements = (M.VoicePlacement(
        index=0, path="C:/voice/01_clip.wav", duration=played, start=start,
        end=start + played, section_type="verse", anchor=M.ANCHOR_TRIMMED,
        shift_seconds=0.0, source_duration=source, trimmed=True),)
    return M.AudioMixPlan(music, placements,
                          M.build_duck_events(placements, music, config), config)


def whole_plan(duration=3.0, start=5.0, music=30.0):
    config = M.AudioMixConfig()
    placements = (M.VoicePlacement(
        index=0, path="C:/voice/01_clip.wav", duration=duration, start=start,
        end=start + duration, section_type="verse", anchor=M.ANCHOR_BEAT,
        shift_seconds=0.0),)
    return M.AudioMixPlan(music, placements,
                          M.build_duck_events(placements, music, config), config)


def filtergraph(command):
    return command[command.index("-filter_complex") + 1]


def test_a_trimmed_voice_input_really_carries_an_atrim():
    """The CRITICAL half of the feature. Updating the planned duration alone would leave the music
    returning on schedule while the untrimmed file talked over it to the end of the song."""
    mix = load_mixdown()
    graph = filtergraph(mix.build_mix_command("m.mp3", trimmed_plan(played=3.0), "out.wav"))
    assert "atrim=end=3.000000" in graph


def test_the_atrim_sits_before_the_delay_not_after_it():
    """Order is load-bearing: `adelay` must shift the already-shortened stream. Asserted on the
    voice branch alone, so the master's own trailing `atrim=end=<music>` cannot satisfy it."""
    mix = load_mixdown()
    graph = filtergraph(mix.build_mix_command("m.mp3", trimmed_plan(played=3.0), "out.wav"))
    branch = next(part for part in graph.split(";") if part.startswith("[1:a]"))
    assert branch.index("atrim=end=3.000000") < branch.index("adelay=")


def test_an_untrimmed_voice_input_carries_no_trim_filter_at_all():
    """"Do not apply trim filters to normal untrimmed voice clips." Not "applies a harmless one"."""
    mix = load_mixdown()
    graph = filtergraph(mix.build_mix_command("m.mp3", whole_plan(), "out.wav"))
    branch = next(part for part in graph.split(";") if part.startswith("[1:a]"))
    assert "atrim" not in branch
    assert "anull" not in branch


def test_the_strict_path_emits_the_byte_identical_legacy_command():
    """Strict produces the legacy filtergraph, compared argv-for-argv against a config built the
    pre-R7.3 way — the same technique R7.1 used to prove Auto Balance OFF costs nothing."""
    mix = load_mixdown()
    config_legacy = M.AudioMixConfig(start_delay_seconds=2.0, min_gap_seconds=1.0,
                                     avoid_drops=True, music_under_voice_percent=35)
    config_strict = M.AudioMixConfig(start_delay_seconds=2.0, min_gap_seconds=1.0,
                                     avoid_drops=True, music_under_voice_percent=35,
                                     voice_overflow=M.OVERFLOW_STRICT)
    assert config_legacy == config_strict

    def built(config):
        placements = M.plan_voice_placements(MUSIC, BEATS, PLAIN_SECTIONS,
                                             voices(4.0, 4.0), config)
        return mix.build_mix_command("m.mp3", placements, "out.wav")

    assert built(config_legacy) == built(config_strict)


def test_the_duck_envelope_matches_the_SHORTENED_playback():
    """The envelope has to agree with what is actually heard. A trimmed clip's duck must release
    from its trimmed end, not from where the untrimmed file would have finished."""
    trimmed = trimmed_plan(played=3.0, source=20.0, start=5.0)
    assert len(trimmed.duck_events) == 1
    event = trimmed.duck_events[0]
    assert event.voice_end == pytest.approx(8.0)
    assert event.voice_end != pytest.approx(25.0)
    mix = load_mixdown()
    graph = filtergraph(mix.build_mix_command("m.mp3", trimmed, "out.wav"))
    assert "8.000000" in graph


def test_a_placement_failure_still_becomes_a_plan_error_under_strict(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixPlanError, match="no legal placement"):
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=MUSIC, beat_times=BEATS,
            sections=PLAIN_SECTIONS, voices=voices(4.0, 80.0),
            config=M.AudioMixConfig(voice_overflow=M.OVERFLOW_STRICT),
            session_dir=str(tmp_path))


def test_zero_placed_clips_fails_loudly_instead_of_pretending_voice_was_included(tmp_path):
    """"If zero voice clips fit, report this clearly. Do not pretend voices were included."

    A plan with no placements out of a non-empty selection would mix a master byte-identical to the
    music and then report "Audio Layers" as though something had happened. It is the one overflow
    outcome that stays fatal — "some clips did not fit" is survivable, "none of them did" is a
    configuration problem the user has to see — and the message names the count and the reason.
    """
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixPlanError) as caught:
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=MUSIC, beat_times=BEATS,
            sections=PLAIN_SECTIONS, voices=voices(80.0, 90.0),
            config=M.AudioMixConfig(voice_overflow=M.OVERFLOW_SKIP),
            session_dir=str(tmp_path))
    message = str(caught.value)
    assert "None of the 2 voice clips fit" in message
    assert "00:35" in message
    assert "Avoid drops" in message, "the message must say what the user can change"


def test_trim_legitimately_rescues_the_same_selection_rather_than_failing(tmp_path):
    """The companion fact, stated so the asymmetry above is not mistaken for inconsistency.

    The identical two over-long clips do NOT fail under `trim`: the first one is shortened to the
    room that exists, so the plan has a placement and the render proceeds. `trim` reaches the
    zero-placement failure only when there is genuinely no usable room at all — covered below.
    """
    fake = FakeSubprocess([FakeCompleted(0, "", ""), FakeCompleted(0, f"{MUSIC}\n", "")])
    mix = load_mixdown(fake)
    (tmp_path / "beatsync_mix_deadbeef.wav").write_bytes(b"x" * 64)

    _path, plan_out = mix.build_mixed_master(
        music_path="m.mp3", music_duration=MUSIC, beat_times=BEATS,
        sections=PLAIN_SECTIONS, voices=voices(80.0, 90.0),
        config=M.AudioMixConfig(voice_overflow=M.OVERFLOW_TRIM),
        session_dir=str(tmp_path))
    assert plan_out.trimmed_count == 1
    assert plan_out.skipped_count == 1


def test_trim_with_no_usable_room_at_all_also_fails_loudly(tmp_path):
    """The start delay pushes the cursor past the end of the music, so not even 0.5 s can be kept.
    `trim` must then fail exactly as `skip` does rather than mix a voiceless "Audio Layers" master.
    """
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(mix.AudioMixPlanError) as caught:
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=MUSIC, beat_times=BEATS,
            sections=PLAIN_SECTIONS, voices=voices(10.0, 8.0),
            config=M.AudioMixConfig(start_delay_seconds=34.8, min_gap_seconds=1.0,
                                    voice_overflow=M.OVERFLOW_TRIM),
            session_dir=str(tmp_path))
    assert "None of the 2 voice clips fit" in str(caught.value)


def test_a_partially_skipped_plan_reaches_the_executor_instead_of_raising(tmp_path):
    """The whole point, at the seam: `skip` must get past `build_mixed_master` and actually render.
    Asserted by the FFmpeg command being issued with the surviving clips as real inputs."""
    fake = FakeSubprocess([FakeCompleted(0, "", ""), FakeCompleted(0, f"{MUSIC}\n", "")])
    mix = load_mixdown(fake)
    master = tmp_path / "beatsync_mix_deadbeef.wav"
    master.write_bytes(b"x" * 64)

    path, plan_out = mix.build_mixed_master(
        music_path="m.mp3", music_duration=MUSIC, beat_times=BEATS,
        sections=PLAIN_SECTIONS, voices=voices(4.0, 80.0, 3.0),
        config=M.AudioMixConfig(voice_overflow=M.OVERFLOW_SKIP),
        session_dir=str(tmp_path))

    assert path == str(master)
    assert plan_out.skipped_count == 1
    assert plan_out.voice_count == 2
    issued = fake.calls[0]
    assert issued.count("-i") == 3                      # music + the two surviving clips
    assert "C:/voice/02_clip.wav" not in issued


# ===========================================================================
# 8. GUI WIRING — one zero-writer control, threaded into BOTH render paths
# ===========================================================================


def gui_tree():
    with open(_GUI, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read())


def func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def click_inputs(tree, button):
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "click"
                and getattr(call.func.value, "id", None) == button):
            kw = next(k for k in call.keywords if k.arg == "inputs")
            return [getattr(n, "id", None) for n in kw.value.elts]
    raise AssertionError(f"no {button}.click")


def test_the_widget_exists_stores_internal_policy_names_and_defaults_to_strict():
    """`(label, value)` choices so the stored value IS the internal policy name — no handler ever
    maps display prose back to a policy, so the two cannot drift."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "voice_overflow = gr.Radio(" in source
    block = source[source.index("voice_overflow = gr.Radio("):][:1200]
    for policy in ("OVERFLOW_STRICT", "OVERFLOW_SKIP", "OVERFLOW_TRIM"):
        assert f"fork_audio_mix.{policy}" in block, policy
    assert "value=fork_audio_mix.DEFAULT_OVERFLOW_POLICY" in block
    assert "info=INFO_VOICE_OVERFLOW" in block, "the control needs its tooltip"


def test_the_tooltip_explains_all_three_modes():
    import ui_content
    info = ui_content.INFO_VOICE_OVERFLOW
    for phrase in ("Strict", "Skip", "Trim", "0.5", "Avoid drops", "filename"):
        assert phrase in info, phrase
    assert "never modified" in info, "it must say the user's audio files are left alone"


@pytest.mark.parametrize("handler", [
    "_process_video_guarded_unlocked", "process_video_guarded",
    "render_selected_variants_guarded",
])
def test_every_render_handler_takes_the_policy_last_and_defaulted(handler):
    """Appended, never inserted — so no pre-existing positional index moved and an existing caller
    that omits it plans exactly as it planned before."""
    node = func(gui_tree(), handler)
    args = [a.arg for a in node.args.args]
    assert "voice_overflow" in args, handler
    defaulted = args[len(args) - len(node.args.defaults):] if node.args.defaults else []
    assert "voice_overflow" in defaulted, handler


def test_the_inner_core_defaults_are_literals_that_cannot_drift():
    """Several suites AST-extract the two exec'd handlers and run them against a namespace where
    `fork_audio_mix` is a stub, so their defaults must be literals rather than module lookups.
    That is a real drift risk, so it is pinned: the literal must equal the real constant."""
    tree = gui_tree()
    for handler in ("_process_video_guarded_unlocked", "render_selected_variants_guarded"):
        node = func(tree, handler)
        args = [a.arg for a in node.args.args]
        default = node.args.defaults[len(node.args.defaults) - (len(args) - args.index(
            "voice_overflow"))]
        assert isinstance(default, ast.Constant), (handler, ast.unparse(default))
        assert default.value == M.DEFAULT_OVERFLOW_POLICY, (handler, default.value)


@pytest.mark.parametrize("button,handler", [
    ("process_btn", "process_video_guarded"),
    ("render_selected_variants_btn", "render_selected_variants_guarded"),
])
def test_both_click_lists_pass_the_policy_at_the_same_index_as_the_signature(button, handler):
    """Gradio matches positionally, so the click list and the signature are one contract. A batch
    that refused candidate 1 on overflow while a single render of the same screen skipped and
    finished would make the setting untrustworthy."""
    tree = gui_tree()
    inputs = click_inputs(tree, button)
    args = [a.arg for a in func(tree, handler).args.args]
    assert "voice_overflow" in inputs, button
    assert inputs.index("voice_overflow") == args.index("voice_overflow"), (button, handler)


def test_the_batch_freezes_the_policy_from_its_own_arguments_never_from_a_recipe():
    """It is placement configuration, not a generated level. The batch must pass its own frozen
    parameter, so every candidate sees the identical policy."""
    body = ast.unparse(func(gui_tree(), "render_selected_variants_guarded"))
    assert "voice_overflow=voice_overflow" in body
    for forbidden in ("audio.voice_overflow", "recipe.voice_overflow",
                      "audio_recipe.voice_overflow"):
        assert forbidden not in body, forbidden


def test_the_config_is_built_from_the_widget_at_the_render_boundary():
    body = ast.unparse(func(gui_tree(), "_process_video_guarded_unlocked"))
    assert "voice_overflow=voice_overflow" in body


def test_the_status_panel_reports_the_accounting_from_the_plans_own_formatter():
    """The GUI must not reformat the counts — one formatter, in the pure module."""
    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "audio_plan.overflow_summary_line()" in source
    for invented in ("used in full", "clips supplied", "Music length:"):
        assert invented not in source, \
            f"gui.py formats {invented!r} itself instead of using the plan's line"


def test_the_pure_module_stays_stdlib_only():
    """CLAUDE.md's hard rule, re-asserted locally because R7.3 touched this file."""
    tree = ast.parse(open(_AUDIO_MIX, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in (
                    "logger", "paths", "gradio", "cupy", "cv2", "librosa", "numpy"), alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in (
                "logger", "paths", "gradio", "cupy", "cv2", "librosa", "numpy"), node.module


# ===========================================================================
# 9. REAL FFMPEG — the voice actually stops where the plan says
# ===========================================================================


def _run_real(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]


def _loudest(samples, start, end):
    lo, hi = int(start * RATE), min(len(samples), int(end * RATE))
    window = samples[lo:hi]
    return max((abs(v) for v in window), default=0.0)


@needs_ffmpeg
def test_a_trimmed_voice_is_silent_after_its_planned_end_in_the_real_master(tmp_path):
    """The decisive measurement, and the one the task singles out.

    Why the room is bounded by an **avoided span** rather than by the music end: with the music end
    as the bound, the master's own `apad`+`atrim=end=<music>` already cuts an overrunning voice, so
    a trimmed mix and an untrimmed one are byte-equivalent and the test would pass whether or not
    the `atrim` was ever emitted. Bounding the room at 12 s inside a 30 s master makes the claim
    falsifiable: after 12 s there must be voice-level silence, and the control below confirms the
    same file WOULD have kept talking.

    Music is deliberately near-silent so the voice is the only thing measured.
    """
    music = write_tone(tmp_path / "music.wav", 30.0, 60.0, 0.002)
    voice = write_tone(tmp_path / "01_clip.wav", 20.0, 1000.0, 0.6)
    assert real_probe_duration(voice) == pytest.approx(20.0, abs=0.01)

    mix = load_mixdown()
    plan_trimmed = trimmed_plan(played=7.0, source=20.0, start=5.0, music=30.0)
    plan_trimmed = type(plan_trimmed)(
        30.0,
        (M.VoicePlacement(0, str(voice), 7.0, 5.0, 12.0, "verse", M.ANCHOR_TRIMMED, 0.0,
                          source_duration=20.0, trimmed=True),),
        plan_trimmed.duck_events, plan_trimmed.config)

    out = tmp_path / "trimmed.wav"
    command = mix.build_mix_command(str(music), plan_trimmed, str(out))
    command[0] = FFMPEG_BIN
    _run_real(command)

    assert real_probe_duration(out) == pytest.approx(30.0, abs=0.001)
    samples = read_left(out)
    assert _loudest(samples, 6.0, 11.5) > 0.3, "the retained speech is missing"
    assert _loudest(samples, 12.5, 29.5) < 0.02, \
        "the voice kept playing past its planned end -- the atrim is not reaching FFmpeg"


@needs_ffmpeg
def test_the_control_proves_the_same_file_would_otherwise_have_kept_talking(tmp_path):
    """Without this, the assertion above could be passing because the source was short, the gain
    was wrong, or the window was empty. Same file, same start, same graph builder — only `trimmed`
    is False — and the voice is loud exactly where the trimmed mix was silent."""
    music = write_tone(tmp_path / "music.wav", 30.0, 60.0, 0.002)
    voice = write_tone(tmp_path / "01_clip.wav", 20.0, 1000.0, 0.6)

    mix = load_mixdown()
    config = M.AudioMixConfig()
    placements = (M.VoicePlacement(0, str(voice), 20.0, 5.0, 25.0, "verse",
                                   M.ANCHOR_BEAT, 0.0),)
    untrimmed = M.AudioMixPlan(30.0, placements,
                               M.build_duck_events(placements, 30.0, config), config)
    out = tmp_path / "untrimmed.wav"
    command = mix.build_mix_command(str(music), untrimmed, str(out))
    command[0] = FFMPEG_BIN
    _run_real(command)

    samples = read_left(out)
    assert _loudest(samples, 12.5, 24.5) > 0.3, \
        "the control did not reproduce the un-trimmed overrun, so the trim test proves nothing"


@needs_ffmpeg
def test_a_skipped_clip_is_absent_from_the_real_master(tmp_path):
    """`skip` has to be real in the audio, not only in the report: the skipped file's own tone must
    not appear anywhere in the master. Two tones an octave apart, measured by band energy."""
    music = write_tone(tmp_path / "music.wav", 35.0, 60.0, 0.002)
    kept = write_tone(tmp_path / "01_clip.wav", 4.0, 440.0, 0.6)
    short = write_tone(tmp_path / "03_clip.wav", 3.0, 880.0, 0.6)
    dropped = write_tone(tmp_path / "02_clip.wav", 30.0, 220.0, 0.6)

    config = M.AudioMixConfig(start_delay_seconds=2.0, min_gap_seconds=1.0,
                              voice_overflow=M.OVERFLOW_SKIP)
    selection = (M.VoiceInput(0, str(kept), 4.0),
                 M.VoiceInput(1, str(dropped), 30.0),
                 M.VoiceInput(2, str(short), 3.0))
    plan_out = M.plan_voice_placements(35.0, BEATS, PLAIN_SECTIONS, selection, config)
    assert plan_out.skipped_count == 1
    assert plan_out.skipped[0].path == str(dropped)

    mix = load_mixdown()
    out = tmp_path / "skipped.wav"
    command = mix.build_mix_command(str(music), plan_out, str(out))
    command[0] = FFMPEG_BIN
    assert str(dropped) not in command, "a skipped clip must not even be an FFmpeg input"
    _run_real(command)

    assert real_probe_duration(out) == pytest.approx(35.0, abs=0.001)
    samples = read_left(out)
    assert _loudest(samples, 2.5, 5.5) > 0.3            # clip 1 played
    assert _loudest(samples, 7.5, 9.5) > 0.3            # clip 3 played at the untouched cursor
    assert _loudest(samples, 12.0, 34.0) < 0.02         # nothing else speaks
