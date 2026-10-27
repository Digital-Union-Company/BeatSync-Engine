"""Audio Layers Comfort Mix (R7.1): loudness measurement, bounded gains, and the legacy guarantee.

The single most important assertion in this file is
:func:`test_auto_balance_off_emits_the_identical_filtergraph` — with the feature off, the FFmpeg
argv must be *byte-identical* to what it was, not merely equivalent. Everything else is about the
gains being bounded in the directions that matter: never amplifying near-silence, never pushing a
measured true peak above the ceiling, and never claiming a correction that did not happen.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import audio_mix as mix
from beatsync_fork import loudness as loud

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_MIXDOWN = os.path.join(_REPO_ROOT, "src", "audio_mixdown.py")


# =============================================================================================
# Measuring
# =============================================================================================

_REAL_BLOCK = """
[Parsed_loudnorm_1 @ 000001]
{
	"input_i" : "-23.45",
	"input_tp" : "-5.60",
	"input_lra" : "7.20",
	"input_thresh" : "-33.80",
	"output_i" : "-24.00",
	"normalization_type" : "dynamic"
}
"""


def test_a_real_loudnorm_block_is_read():
    reading = loud.parse_loudness(_REAL_BLOCK)
    assert reading.ok
    assert reading.integrated_lufs == pytest.approx(-23.45)
    assert reading.true_peak_dbtp == pytest.approx(-5.60)


def test_a_warning_printed_after_the_block_does_not_break_it():
    text = _REAL_BLOCK + "\n[out#0 @ 0001] video:0kB audio:1kB\nsize=N/A time=00:01:00.00\n"
    assert loud.parse_loudness(text).ok


def test_the_last_block_wins_when_ffmpeg_prints_two():
    first = _REAL_BLOCK.replace("-23.45", "-30.00")
    reading = loud.parse_loudness(first + _REAL_BLOCK)
    assert reading.integrated_lufs == pytest.approx(-23.45)


@pytest.mark.parametrize("text", ["", None, "no json here", "{}", '{"input_i": "abc"}'])
def test_unreadable_output_is_unknown_not_zero(text):
    """`ok=False` means *we do not know*. Rounding that to 0.0 LUFS would make a silent file look
    like a loud one and invite a 30 dB cut."""
    reading = loud.parse_loudness(text)
    assert reading.ok is False
    assert reading.integrated_lufs == 0.0


def test_an_infinite_reading_is_not_a_number():
    """`loudnorm` legitimately reports `-inf` for digital silence."""
    assert loud.parse_loudness(_REAL_BLOCK.replace('"-23.45"', '"-inf"')).ok is False


def test_the_measure_command_writes_nothing_and_decodes_no_video():
    argv = loud.build_measure_command("ffmpeg.exe", "a.wav")
    assert "-vn" in argv
    assert "loudnorm=print_format=json" in " ".join(argv)
    assert argv[-3:] == ["-f", "null", "-"]
    for forbidden in ("-y", "-c:a"):
        assert forbidden not in argv


def test_measure_reports_a_failure_instead_of_raising():
    def runner(argv, timeout):
        return 1, "ffmpeg: No such file or directory"
    reading = loud.measure("ffmpeg.exe", "missing.wav", runner=runner)
    assert reading.ok is False
    assert "No such file" in reading.detail


def test_measure_survives_a_runner_that_explodes():
    def runner(argv, timeout):
        raise OSError("boom")
    assert loud.measure("ffmpeg.exe", "x.wav", runner=runner).ok is False


# =============================================================================================
# Gains
# =============================================================================================

def _reading(lufs, peak=-6.0):
    return loud.Loudness(ok=True, integrated_lufs=lufs, true_peak_dbtp=peak)


def test_a_quiet_voice_against_loud_music_is_corrected_towards_the_targets():
    """The owner's actual complaint, as numbers: a mastered track at -9 LUFS and a phone-recorded
    voice at -28 are 19 dB apart before any ducking happens."""
    plan = loud.plan_gains(_reading(-9.0, -1.5), _reading(-28.0, -12.0))
    assert plan.applied
    assert plan.music_gain_db < 0, "loud music must come down"
    assert plan.voice_gain_db > 0, "quiet voice must come up"
    assert plan.music_gain < 1.0 and plan.voice_gain > 1.0


def test_a_boost_is_bounded():
    plan = loud.plan_gains(_reading(-18.0), _reading(loud.SILENCE_FLOOR_LUFS + 1, -40.0))
    assert plan.voice_gain_db <= loud.MAX_BOOST_DB + 1e-9


def test_a_genuinely_quiet_recording_is_still_helped():
    """The case the feature exists for, and the one an over-high silence floor would refuse.

    Measured on a real fixture: a deliberately quiet spoken take came out at -48 LUFS. With the
    floor at -45 that was dismissed as silence; at -60 it is correctly treated as quiet speech.
    """
    quiet = _reading(-48.0, -42.0)
    assert quiet.is_silent is False
    plan = loud.plan_gains(_reading(-16.0, -1.5), quiet)
    assert plan.applied and plan.voice_gain_db > 0


def test_a_cut_is_bounded():
    plan = loud.plan_gains(_reading(10.0, -30.0), _reading(-16.0))
    assert plan.music_gain_db >= -loud.MAX_CUT_DB - 1e-9


def test_a_near_silent_voice_is_never_amplified():
    """The single most damaging thing an automatic balancer can do is lift a clip that is mostly
    room tone. It is left at unity and said so."""
    silent = _reading(-70.0, -60.0)
    assert silent.is_silent
    plan = loud.plan_gains(_reading(-18.0), silent)
    assert plan.voice_gain == pytest.approx(1.0)
    assert plan.voice_gain_db == 0.0
    assert any("silent" in note for note in plan.notes)


def test_a_boost_respects_the_measured_true_peak():
    """An automatic correction must never be the thing that makes a mix clip."""
    hot = _reading(-25.0, -0.5)          # quiet programme, almost no headroom
    plan = loud.plan_gains(_reading(-18.0), hot)
    assert plan.voice_gain_db <= max(0.0, loud.TRUE_PEAK_CEILING_DBTP - (-0.5)) + 1e-9
    assert any("dBTP" in note for note in plan.notes)


@pytest.mark.parametrize("music_ok,voice_ok", [(False, True), (True, False), (False, False)])
def test_nothing_is_applied_when_either_side_is_unmeasured(music_ok, voice_ok):
    music = _reading(-18.0) if music_ok else loud.Loudness(ok=False, detail="x")
    voice = _reading(-16.0) if voice_ok else loud.Loudness(ok=False, detail="x")
    plan = loud.plan_gains(music, voice)
    assert plan.applied is False
    assert plan.music_gain == 1.0 and plan.voice_gain == 1.0


def test_an_unmeasured_side_is_reported_as_not_normalised():
    """It must not claim normalisation happened when it did not."""
    text = loud.describe_plan(loud.Loudness(ok=False, detail="unreadable"), _reading(-16.0),
                              loud.plan_gains(loud.Loudness(ok=False), _reading(-16.0)))
    assert "did nothing" in text
    assert "Applied:" not in text


def test_material_already_at_target_is_left_alone():
    plan = loud.plan_gains(_reading(loud.TARGET_MUSIC_LUFS), _reading(loud.TARGET_VOICE_LUFS))
    assert plan.applied and plan.is_noop
    assert "already at their targets" in loud.describe_plan(
        _reading(loud.TARGET_MUSIC_LUFS), _reading(loud.TARGET_VOICE_LUFS), plan)


def test_db_and_linear_agree():
    assert loud.db_to_linear(0.0) == pytest.approx(1.0)
    assert loud.db_to_linear(-6.0) == pytest.approx(0.5011872)
    assert loud.db_to_linear(6.0) == pytest.approx(1.9952623)


# =============================================================================================
# The config, and the legacy guarantee
# =============================================================================================

def test_a_default_config_is_exactly_the_legacy_one():
    config = mix.AudioMixConfig()
    assert config.auto_balance is False
    assert config.music_return_seconds == mix.DUCK_RELEASE_SECONDS == 0.400


def test_comfort_mode_falls_back_to_the_comfort_value_not_the_legacy_one():
    """A comfort mix with a 0.4s release is the one combination that would quietly do nothing."""
    assert mix.AudioMixConfig(auto_balance=True,
                              music_return_seconds=None).music_return_seconds \
        == mix.COMFORT_MUSIC_RETURN_SECONDS


@pytest.mark.parametrize("value,expected", [(0.1, 0.5), (0.5, 0.5), (2.0, 2.0), (5.0, 5.0),
                                            (99.0, 5.0), (-3.0, 0.5)])
def test_comfort_mode_clamps_into_the_offered_range(value, expected):
    assert mix.AudioMixConfig(auto_balance=True,
                              music_return_seconds=value).music_return_seconds == expected


def test_legacy_mode_does_not_clamp_and_does_not_invent():
    assert mix.AudioMixConfig(music_return_seconds=0.4).music_return_seconds == 0.4
    assert mix.AudioMixConfig(music_return_seconds="junk").music_return_seconds == 0.4


def test_the_release_but_not_the_attack_follows_the_control():
    """The complaint was about the music coming BACK. Lengthening the attack would start ducking
    earlier and bury the lead-in instead of fixing anything."""
    placement = mix.VoicePlacement(0, "v.wav", 2.0, 10.0, 12.0, "verse", "beat", 0.0)
    comfort = mix.AudioMixConfig(auto_balance=True, music_return_seconds=2.0)
    event = mix.build_duck_events([placement], 60.0, comfort)[0]
    assert event.release == 2.0
    assert event.attack == mix.DUCK_ATTACK_SECONDS == 0.250
    assert event.end == pytest.approx(14.0)

    legacy = mix.build_duck_events([placement], 60.0, mix.AudioMixConfig())[0]
    assert legacy.release == 0.400
    assert legacy.end == pytest.approx(12.4)


def test_a_longer_return_never_moves_the_voice_itself():
    """Placement is unchanged: only the music's recovery ramp lengthens."""
    placement = mix.VoicePlacement(0, "v.wav", 2.0, 10.0, 12.0, "verse", "beat", 0.0)
    for seconds in (0.4, 1.0, 2.0, 5.0):
        event = mix.build_duck_events(
            [placement], 60.0,
            mix.AudioMixConfig(auto_balance=True, music_return_seconds=seconds))[0]
        assert event.voice_start == 10.0 and event.voice_end == 12.0


def test_the_return_ramp_is_clamped_to_the_music_length():
    placement = mix.VoicePlacement(0, "v.wav", 2.0, 8.0, 10.0, "verse", "beat", 0.0)
    event = mix.build_duck_events(
        [placement], 11.0, mix.AudioMixConfig(auto_balance=True, music_return_seconds=5.0))[0]
    assert event.end == pytest.approx(11.0)


def test_the_report_states_the_release_actually_in_force():
    placement = mix.VoicePlacement(0, "v.wav", 2.0, 10.0, 12.0, "verse", "beat", 0.0)
    comfort = mix.AudioMixConfig(auto_balance=True, music_return_seconds=2.0)
    plan = mix.AudioMixPlan(music_duration=60.0, placements=(placement,),
                            duck_events=mix.build_duck_events([placement], 60.0, comfort),
                            config=comfort)
    text = "\n".join(plan.report_lines())
    assert "250 / 2000 ms" in text
    assert "comfort" in text

    legacy_config = mix.AudioMixConfig()
    legacy = mix.AudioMixPlan(music_duration=60.0, placements=(placement,),
                              duck_events=mix.build_duck_events([placement], 60.0,
                                                                legacy_config),
                              config=legacy_config)
    legacy_text = "\n".join(legacy.report_lines())
    assert "250 / 400 ms" in legacy_text
    assert "comfort" not in legacy_text


def test_balance_report_defaults_to_empty():
    """Empty means *no measurement was taken*, which is the truthful state with the feature off."""
    plan = mix.AudioMixPlan(music_duration=10.0, placements=(), duck_events=(),
                            config=mix.AudioMixConfig())
    assert plan.balance_report == ""


# =============================================================================================
# The filtergraph
# =============================================================================================

_WANTED = {"build_mix_command", "build_duck_expression", "escape_filter_expression"}


def _builders(source: str) -> dict:
    from beatsync_fork import smart_mix as fork_smart_mix
    namespace = {"fork_smart_mix": fork_smart_mix, "os": os, "FFMPEG_PATH": "ffmpeg.exe"}
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            try:
                exec(compile(ast.Module([node], []), "<const>", "exec"), namespace)
            except Exception:                                 # noqa: BLE001
                pass
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED:
            exec(compile(ast.Module([node], []), "<fn>", "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def current():
    with open(_MIXDOWN, encoding="utf-8") as handle:
        return _builders(handle.read())


#: The exact argv the pre-R7.1 builder produced for `_plan()` and for a music-only plan,
#: captured from commit `06c1362` (Raw Audio Cutter V1) and frozen here.
#:
#: **Frozen rather than read from git, and that is the whole point.** The first version of
#: this suite loaded the historical function with `git show HEAD:src/audio_mixdown.py` and
#: skipped when HEAD already contained the new builder. It worked perfectly until R7.1 was
#: committed -- at which point the three most important assertions in this file began
#: SKIPPING instead of running, silently and without failing. A frozen literal cannot do
#: that: it needs no git, no history and no network, and it keeps asserting the legacy
#: shape for as long as the file exists.
#:
#: If a future change legitimately alters the Auto-Balance-OFF command, THIS is what has to
#: be edited, deliberately, with the reason recorded. That friction is the point.
_FROZEN_LEGACY_ARGV = \
['ffmpeg.exe',
 '-y',
 '-i',
 'music.mp3',
 '-i',
 'v1.wav',
 '-i',
 'v2.wav',
 '-filter_complex',
 "aevalsrc='1-0.650000*(max(min(clip((t-4.750000)/0.250000\\,0\\,1)\\,clip((7.000000+0.400000-t)/0.400000\\,0\\,1))\\,min(clip((t-19.750000)/0.250000\\,0\\,1)\\,clip((21.500000+0.400000-t)/0.400000\\,0\\,1))))':s=48000:c=stereo:d=60.000000,aformat=sample_fmts=fltp:channel_layouts=stereo[env];[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[mus];[mus][env]amultiply[music];[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=5000:all=1[v1];[2:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay=20000:all=1[v2];[music][v1][v2]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0[mixed];[mixed]alimiter=limit=0.97:attack=1:release=50:level=0:latency=1,apad,atrim=end=60.000000,asetpts=N/SR/TB[outa]",
 '-map',
 '[outa]',
 '-c:a',
 'pcm_s24le',
 '-ar',
 '48000',
 '-ac',
 '2',
 'out.wav']

_FROZEN_LEGACY_MUSIC_ONLY_ARGV = \
['ffmpeg.exe',
 '-y',
 '-i',
 'music.mp3',
 '-filter_complex',
 '[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[music];[music]amix=inputs=1:duration=longest:dropout_transition=0:normalize=0[mixed];[mixed]alimiter=limit=0.97:attack=1:release=50:level=0:latency=1,apad,atrim=end=60.000000,asetpts=N/SR/TB[outa]',
 '-map',
 '[outa]',
 '-c:a',
 'pcm_s24le',
 '-ar',
 '48000',
 '-ac',
 '2',
 'out.wav']


@pytest.fixture(scope="module")
def previous():
    """The frozen pre-R7.1 argv, shaped like the builder it replaces."""
    return {
        "build_mix_command": lambda music, plan, out, **_kw: (
            list(_FROZEN_LEGACY_MUSIC_ONLY_ARGV) if not plan.placements
            else list(_FROZEN_LEGACY_ARGV)),
    }


def _plan(config=None):
    config = config or mix.AudioMixConfig()
    first = mix.VoicePlacement(0, "v1.wav", 2.0, 5.0, 7.0, "verse", "beat", 0.0)
    second = mix.VoicePlacement(1, "v2.wav", 1.5, 20.0, 21.5, "chorus", "beat", 0.0)
    return mix.AudioMixPlan(
        music_duration=60.0, placements=(first, second),
        duck_events=mix.build_duck_events([first, second], 60.0, config), config=config)


def test_auto_balance_off_emits_the_identical_filtergraph(current, previous):
    """**The legacy guarantee, asserted against the committed historical builder.**

    Not "equivalent", not "a unity gain that cancels out" — the same argv, string for string. The
    inserts are leading-comma fragments that collapse to the empty string, so with no balance every
    f-string produces exactly the text it produced before R7.1.
    """
    plan = _plan()
    before = previous["build_mix_command"]("music.mp3", plan, "out.wav")
    assert current["build_mix_command"]("music.mp3", plan, "out.wav") == before
    assert current["build_mix_command"]("music.mp3", plan, "out.wav", balance=None) == before


def test_a_plan_that_was_not_applied_is_also_the_legacy_graph(current, previous):
    class NotApplied:
        applied = False

    plan = _plan()
    assert current["build_mix_command"]("music.mp3", plan, "out.wav", balance=NotApplied()) \
        == previous["build_mix_command"]("music.mp3", plan, "out.wav")


def test_the_music_only_branch_is_identical_too(current, previous):
    """No voice means no duck envelope at all; that branch must not have grown a filter either."""
    empty = mix.AudioMixPlan(music_duration=60.0, placements=(), duck_events=(),
                             config=mix.AudioMixConfig())
    assert current["build_mix_command"]("music.mp3", empty, "out.wav", balance=None) \
        == previous["build_mix_command"]("music.mp3", empty, "out.wav")


def test_no_unity_volume_filter_is_ever_emitted(current):
    argv = current["build_mix_command"]("music.mp3", _plan(), "out.wav", balance=None)
    assert "volume=1.000000" not in " ".join(argv)


def test_an_applied_balance_inserts_both_gains_before_the_mix(current):
    plan_obj = loud.GainPlan(applied=True, music_gain=0.5, voice_gain=2.0,
                             music_gain_db=-6.0, voice_gain_db=6.0)
    argv = current["build_mix_command"]("music.mp3", _plan(), "out.wav", balance=plan_obj)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "stereo,volume=0.500000[mus]" in graph, "the music gain is missing or misplaced"
    assert graph.count("stereo,volume=2.000000,adelay") == 2, "each voice input needs the gain"
    # ...and before amix, which is the entire point of balancing rather than mastering.
    assert graph.index("volume=0.500000") < graph.index("amix=")


def test_smart_mix_sfx_are_not_rebalanced(current):
    """Their level is already the operator's own slider; touching it would be a second authority."""
    plan_obj = loud.GainPlan(applied=True, music_gain=0.5, voice_gain=2.0)
    argv = current["build_mix_command"]("music.mp3", _plan(), "out.wav", balance=plan_obj)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "[s" not in graph, "this fixture has no SFX, so no SFX chain should exist"


def test_the_master_limiter_and_duck_depth_are_untouched_by_balancing(current):
    """Comfort mixing must not quietly become mastering: the limiter settings and the duck floor
    are the same with and without it."""
    plan_obj = loud.GainPlan(applied=True, music_gain=0.5, voice_gain=2.0)
    off = current["build_mix_command"]("music.mp3", _plan(), "out.wav", balance=None)
    on = current["build_mix_command"]("music.mp3", _plan(), "out.wav", balance=plan_obj)
    for argv in (off, on):
        graph = argv[argv.index("-filter_complex") + 1]
        assert "alimiter=limit=" in graph
    off_graph = off[off.index("-filter_complex") + 1]
    on_graph = on[on.index("-filter_complex") + 1]
    limiter_off = off_graph[off_graph.index("alimiter"):]
    limiter_on = on_graph[on_graph.index("alimiter"):]
    assert limiter_off == limiter_on


# =============================================================================================
# The GUI seam
# =============================================================================================

@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def gui_tree(gui_source: str) -> ast.Module:
    return ast.parse(gui_source)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def test_both_controls_exist_and_default_to_legacy(gui_source: str):
    assert "voice_auto_balance = gr.Checkbox(" in gui_source
    assert "voice_music_return = gr.Number(" in gui_source
    block = gui_source[gui_source.index("voice_auto_balance = gr.Checkbox("):]
    assert "value=False" in block[:400], "Auto Balance must be OFF by default"
    assert "fork_audio_mix.DEFAULT_MUSIC_RETURN_SECONDS" in block[:900], \
        "the music-return control must start at the legacy value"


def test_the_controls_ride_at_the_end_of_the_positional_contract(gui_tree: ast.Module):
    """Gradio matches `inputs` to parameters positionally, so appending at the end of both is what
    keeps every pre-existing index where it was."""
    handler = _func(gui_tree, "process_video_guarded")
    names = [a.arg for a in handler.args.args]
    assert names[-2:] == ["voice_auto_balance", "voice_music_return"]

    for call in ast.walk(gui_tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr == "click"
                and getattr(call.func.value, "id", None) == "process_btn"):
            inputs = [n.id for n in next(kw for kw in call.keywords
                                         if kw.arg == "inputs").value.elts
                      if isinstance(n, ast.Name)]
            break
    else:
        raise AssertionError("process_btn.click not found")
    assert inputs[-2:] == ["voice_auto_balance", "voice_music_return"]
    assert len(inputs) == len(names)


def test_the_config_carries_both_controls(gui_tree: ast.Module):
    source = ast.unparse(_func(gui_tree, "_process_video_guarded_unlocked"))
    assert "auto_balance=voice_auto_balance" in source
    assert "music_return_seconds=voice_music_return" in source


def test_measurement_only_runs_when_the_operator_asked_for_it(gui_source: str):
    """With Auto Balance off no analysis pass runs at all, so an ordinary render costs what it
    always did."""
    with open(_MIXDOWN, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    body = ast.unparse(_func(tree, "build_mixed_master"))
    assert "measure_balance" in body
    assert "auto_balance" in body
    guard = body[:body.index("measure_balance")]
    assert "if getattr(config, 'auto_balance', False)" in guard.replace('"', "'")


def test_the_feature_adds_no_process_launch_to_the_view_layer(gui_tree: ast.Module):
    runs = [n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]
    assert len(runs) == 2, f"{len(runs)} subprocess.run sites in gui.py"
