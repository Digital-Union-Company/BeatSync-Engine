"""Audio Layers V1: the runtime mixdown executor.

`audio_mixdown` imports `ffmpeg_processing`, which imports `logger`, which loads librosa and mutates
`PATH`/`CUDA_PATH` — so the module cannot be imported on a bare interpreter. The suite therefore
uses the house technique: AST-extract the real function bodies and execute them against a stubbed
namespace. That keeps the *actual shipped code* under test rather than a reimplementation of it, and
the extraction list below is part of the contract — a function that grows a dependency outside it
fails here with `NameError` instead of quietly going untested.

The bulk of the suite stubs `subprocess`. A final section runs the **real** portable FFmpeg over
tiny synthetic tones when one is available, because the things most worth proving — exact duration,
sample-accurate delay, limiter latency compensation, clipping — are properties of the binary, not of
the string we hand it.
"""

from __future__ import annotations

import ast
import math
import os
import shutil
import struct
import subprocess
import sys
import types
import wave
from typing import Any

import pytest

from beatsync_fork import audio_mix as fork_audio_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIXDOWN = os.path.join(_REPO_ROOT, "src", "audio_mixdown.py")

#: Everything the suite executes. Module-level constants come along so the bodies see them.
_EXTRACTED = (
    "AudioMixError", "_run", "_tail", "probe_duration", "_selected_voice_paths",
    "prepare_voice_inputs", "build_duck_expression", "escape_filter_expression",
    "build_mix_command", "master_path_for", "render_mixed_master", "discard_master",
    "build_mixed_master",
)


def _module_tree():
    with open(_MIXDOWN, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_MIXDOWN)


def load_mixdown(subprocess_module=None, uuid_module=None):
    """The real `audio_mixdown` bodies, executed against stubbed runtime dependencies."""
    tree = _module_tree()
    wanted = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in _EXTRACTED:
            wanted.append(node)
        elif isinstance(node, ast.Assign) and all(
                isinstance(t, ast.Name) and t.id.isupper() or
                isinstance(t, ast.Name) and t.id.startswith("_")
                for t in node.targets):
            wanted.append(node)
    names = {n.name for n in wanted if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    missing = set(_EXTRACTED) - names
    assert not missing, f"not found at module level in audio_mixdown.py: {sorted(missing)}"

    fake_uuid = uuid_module or types.SimpleNamespace(
        uuid4=lambda: types.SimpleNamespace(hex="deadbeef"))
    namespace = {
        "os": os,
        "subprocess": subprocess_module or subprocess,
        "uuid": fake_uuid,
        "FFMPEG_PATH": r"C:\fake\ffmpeg.exe",
        "FFPROBE_PATH": r"C:\fake\ffprobe.exe",
        "fork_audio_mix": fork_audio_mix,
        "__name__": "audio_mixdown_under_test",
    }
    exec(compile(ast.Module(body=wanted, type_ignores=[]), _MIXDOWN, "exec"), namespace)
    return types.SimpleNamespace(**namespace)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeSubprocess:
    """Records every command and replays scripted results."""

    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def run(self, command, **kwargs):
        self.calls.append(list(command))
        if self._results:
            nxt = self._results.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return FakeCompleted(0, "10.0\n", "")


def plan_for(durations=(3.0,), starts=(5.0,), music_duration=30.0, percent=35):
    config = fork_audio_mix.AudioMixConfig(music_under_voice_percent=percent)
    placements = tuple(
        fork_audio_mix.VoicePlacement(
            index=i, path=f"C:/voices/{i:02d}_clip.wav", duration=d,
            start=s, end=s + d, section_type="verse",
            anchor=fork_audio_mix.ANCHOR_BEAT, shift_seconds=0.0)
        for i, (d, s) in enumerate(zip(durations, starts)))
    return fork_audio_mix.AudioMixPlan(
        music_duration=music_duration,
        placements=placements,
        duck_events=fork_audio_mix.build_duck_events(placements, music_duration, config),
        config=config,
    )


# ===========================================================================
# 1. STRUCTURE
# ===========================================================================


def test_the_pure_module_never_imports_the_runtime_executor():
    """The dependency is one-way. `audio_mix` must stay importable on a bare interpreter.

    Checked against its *imports*, not its prose: the module docstring draws the arrow
    `plan -> audio_mixdown -> mixed master` precisely to explain the direction.
    """
    import inspect
    tree = ast.parse(inspect.getsource(fork_audio_mix))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "audio_mixdown" not in alias.name, ast.unparse(node)
        elif isinstance(node, ast.ImportFrom):
            assert "audio_mixdown" not in ast.unparse(node), ast.unparse(node)


def test_the_executor_uses_the_projects_portable_binaries():
    source = ast.unparse(_module_tree())
    assert "from ffmpeg_processing import FFMPEG_PATH, FFPROBE_PATH" in source
    for forbidden in ("shutil.which", "'ffmpeg'", '"ffmpeg"', "os.environ"):
        assert forbidden not in source, f"audio_mixdown searches for a binary via {forbidden}"


def test_master_format_constants():
    mix = load_mixdown()
    assert mix.MASTER_SAMPLE_RATE == 48000
    assert mix.MASTER_CHANNELS == 2
    assert mix.MASTER_CODEC == "pcm_s24le"
    assert mix.DURATION_TOLERANCE_SECONDS == 0.001
    assert (mix.LIMITER_LIMIT, mix.LIMITER_ATTACK_MS, mix.LIMITER_RELEASE_MS) == (0.97, 1, 50)


# ===========================================================================
# 2. PROBING
# ===========================================================================


def test_probe_duration_command_and_value():
    fake = FakeSubprocess([FakeCompleted(0, "12.345\n", "")])
    mix = load_mixdown(fake)
    assert mix.probe_duration("C:/a b/music.mp3") == pytest.approx(12.345)
    command = fake.calls[0]
    assert command[0] == r"C:\fake\ffprobe.exe"
    assert "format=duration" in command
    assert command[-1] == "C:/a b/music.mp3"


@pytest.mark.parametrize("result,fragment", [
    (FakeCompleted(1, "", "No such file"), "Could not read"),
    (FakeCompleted(0, "not-a-number", ""), "Could not read a duration"),
    (FakeCompleted(0, "0", ""), "unusable duration"),
    (FakeCompleted(0, "-3", ""), "unusable duration"),
    (FakeCompleted(0, "nan", ""), "unusable duration"),
    (FakeCompleted(0, "inf", ""), "unusable duration"),
])
def test_probe_duration_failures_are_explicit(result, fragment: str):
    """Never a fallback value: `get_video_duration` answers 10.0 when probing fails, which would
    place speech at the wrong time and mis-duck the music."""
    mix = load_mixdown(FakeSubprocess([result]))
    with pytest.raises(Exception) as excinfo:
        mix.probe_duration("C:/voices/x.wav")
    assert fragment in str(excinfo.value)


def test_probe_duration_timeout_is_reported():
    fake = FakeSubprocess([subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="Timed out"):
        mix.probe_duration("C:/voices/x.wav")


def test_failure_text_is_bounded():
    mix = load_mixdown(FakeSubprocess([FakeCompleted(1, "", "E" * 50_000)]))
    with pytest.raises(Exception) as excinfo:
        mix.probe_duration("C:/voices/x.wav")
    assert len(str(excinfo.value)) < 2000


def test_prepare_voice_inputs_orders_probes_and_indexes(tmp_path):
    for name in ("03_c.wav", "01_a.wav", "02_b.wav"):
        (tmp_path / name).write_bytes(b"x")
    paths = [str(tmp_path / "03_c.wav"), str(tmp_path / "01_a.wav"), str(tmp_path / "02_b.wav")]
    fake = FakeSubprocess([FakeCompleted(0, "1.0", ""), FakeCompleted(0, "2.0", ""),
                           FakeCompleted(0, "3.0", "")])
    mix = load_mixdown(fake)
    prepared = mix.prepare_voice_inputs(paths)

    assert [os.path.basename(v.path) for v in prepared] == ["01_a.wav", "02_b.wav", "03_c.wav"]
    assert [v.index for v in prepared] == [0, 1, 2]
    assert [v.duration for v in prepared] == [1.0, 2.0, 3.0]


def test_prepare_voice_inputs_is_empty_for_no_selection():
    mix = load_mixdown(FakeSubprocess())
    assert mix.prepare_voice_inputs([]) == ()
    assert mix.prepare_voice_inputs(None) == ()


def test_prepare_voice_inputs_rejects_unsupported_and_missing(tmp_path):
    bad = tmp_path / "clip.m4a"
    bad.write_bytes(b"x")
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(Exception, match="not a supported type"):
        mix.prepare_voice_inputs([str(bad)])
    with pytest.raises(Exception, match="missing or unreadable"):
        mix.prepare_voice_inputs([str(tmp_path / "gone.wav")])


# ---------------------------------------------------------------------------
# R1-A: a selected voice clip may never be silently dropped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_name,arrangement", [
    ("02_b.wav", "missing sorts in the middle"),
    ("00_a.wav", "missing sorts first"),
    ("99_z.wav", "missing sorts last"),
])
def test_a_partially_missing_selection_fails_instead_of_shrinking(
        tmp_path, missing_name: str, arrangement: str):
    """The R1-A defect: `_as_existing_source_paths` filtered the selection, so a three-clip pick
    with one missing file became a silent two-clip render. The result must not depend on where the
    missing name sorts, so all three arrangements are covered."""
    present = []
    for name in ("01_x.wav", "50_m.wav"):
        (tmp_path / name).write_bytes(b"x")
        present.append(str(tmp_path / name))
    selection = present + [str(tmp_path / missing_name)]

    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "1.0", "")] * 5))
    with pytest.raises(Exception, match="missing or unreadable") as excinfo:
        mix.prepare_voice_inputs(selection)
    assert missing_name in str(excinfo.value), arrangement


def test_a_partially_missing_selection_never_returns_the_valid_subset(tmp_path):
    for name in ("01_a.wav", "03_c.wav"):
        (tmp_path / name).write_bytes(b"x")
    selection = [str(tmp_path / "01_a.wav"), str(tmp_path / "02_b.wav"),
                 str(tmp_path / "03_c.wav")]

    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "1.0", "")] * 5))
    try:
        result = mix.prepare_voice_inputs(selection)
    except Exception:
        return                      # the required outcome
    pytest.fail(f"returned a {len(result)}-clip subset of a 3-clip selection: "
                f"{[os.path.basename(v.path) for v in result]}")


@pytest.mark.parametrize("entry", [None, "", "   ", 7, object(), b"x.wav"])
def test_an_unusable_entry_fails_rather_than_being_dropped(tmp_path, entry):
    """`order_voice_paths` drops non-strings by design (it runs on widget values); the preflight
    must not inherit that, or a malformed entry would silently shrink the selection."""
    good = tmp_path / "01_a.wav"
    good.write_bytes(b"x")
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "1.0", "")] * 5))
    with pytest.raises(Exception, match="not a usable file path"):
        mix.prepare_voice_inputs([str(good), entry])


def test_the_selection_is_taken_whole_without_filtering(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    assert mix._selected_voice_paths(None) == []
    assert mix._selected_voice_paths([]) == []
    # a bare string is ONE selection, not an iterable of characters
    assert mix._selected_voice_paths("a/01.wav") == ["a/01.wav"]
    # nothing is filtered out at this stage, however malformed
    assert mix._selected_voice_paths(["a.wav", None, "", 7]) == ["a.wav", None, "", 7]


def test_a_complete_valid_selection_still_succeeds(tmp_path):
    for name in ("03_c.wav", "01_a.wav", "02_b.wav"):
        (tmp_path / name).write_bytes(b"x")
    selection = [str(tmp_path / n) for n in ("03_c.wav", "01_a.wav", "02_b.wav")]
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, str(1.0 + i), "") for i in range(3)]))
    prepared = mix.prepare_voice_inputs(selection)
    assert [os.path.basename(v.path) for v in prepared] == ["01_a.wav", "02_b.wav", "03_c.wav"]
    assert len(prepared) == len(selection)


def test_pathlike_entries_are_accepted(tmp_path):
    """Gradio gives strings, but the boundary must not shrink a collection it merely did not
    expect the type of."""
    for name in ("01_a.wav", "02_b.wav"):
        (tmp_path / name).write_bytes(b"x")
    mix = load_mixdown(FakeSubprocess([FakeCompleted(0, "1.0", "")] * 3))
    prepared = mix.prepare_voice_inputs([tmp_path / "01_a.wav", tmp_path / "02_b.wav"])
    assert len(prepared) == 2


# ===========================================================================
# 3. THE DUCK EXPRESSION
# ===========================================================================


def test_duck_expression_matches_the_pure_reference_everywhere():
    """The FFmpeg expression and `audio_mix.duck_gain_at` must agree — otherwise the tests measure
    one model and the render produces another."""
    plan = plan_for((2.0, 1.5), (5.0, 9.0))
    mix = load_mixdown()
    expression = mix.build_duck_expression(plan.duck_events, plan.config.music_floor)

    def evaluate(expr: str, t: float) -> float:
        scope = {"t": t,
                 "min": min, "max": max,
                 "clip": lambda x, lo, hi: max(lo, min(hi, x))}
        return eval(expr, {"__builtins__": {}}, scope)  # noqa: S307 - fixed, generated input

    for instant in [0.05 * k for k in range(0, 260)]:
        assert evaluate(expression, instant) == pytest.approx(
            fork_audio_mix.duck_gain_at(plan.duck_events, instant), abs=1e-9)


def test_duck_expression_uses_max_not_chained_ifs():
    """`if()` chains are order-dependent; `max` of duck amounts is not."""
    mix = load_mixdown()
    expression = mix.build_duck_expression(plan_for((2.0, 2.0), (5.0, 8.0)).duck_events, 0.35)
    assert "max(" in expression
    assert "if(" not in expression
    assert expression.startswith("1-0.650000*(")


def test_duck_expression_is_order_independent():
    mix = load_mixdown()
    plan = plan_for((2.0, 2.0), (5.0, 8.0))
    forward = mix.build_duck_expression(plan.duck_events, 0.35)
    reverse = mix.build_duck_expression(tuple(reversed(plan.duck_events)), 0.35)

    def evaluate(expr, t):
        return eval(expr, {"__builtins__": {}},  # noqa: S307
                    {"t": t, "min": min, "max": max,
                     "clip": lambda x, lo, hi: max(lo, min(hi, x))})

    for instant in [0.1 * k for k in range(150)]:
        assert evaluate(forward, instant) == pytest.approx(evaluate(reverse, instant))


def test_no_events_means_unity_gain():
    assert load_mixdown().build_duck_expression((), 0.35) == "1"


# ===========================================================================
# 4. THE COMMAND
# ===========================================================================


def test_command_shape_music_first_then_voices_in_plan_order():
    mix = load_mixdown()
    plan = plan_for((2.0, 1.0, 3.0), (4.0, 9.0, 14.0))
    command = mix.build_mix_command("C:/music/song.mp3", plan, "C:/out/mix.wav")

    assert command[0] == r"C:\fake\ffmpeg.exe"
    assert command[1] == "-y"
    inputs = [command[i + 1] for i, token in enumerate(command) if token == "-i"]
    assert inputs[0] == "C:/music/song.mp3"
    assert inputs[1:] == [p.path for p in plan.placements]
    assert command[-1] == "C:/out/mix.wav"


def test_filter_complex_is_a_single_argv_item():
    """No shell is involved, so commas/colons/quotes in the graph never need escaping."""
    mix = load_mixdown()
    command = mix.build_mix_command("m.mp3", plan_for(), "o.wav")
    index = command.index("-filter_complex")
    graph = command[index + 1]
    assert isinstance(graph, str)
    assert ";" in graph and "," in graph
    assert command.count("-filter_complex") == 1


def _graph(mix, plan, music="m.mp3", out="o.wav") -> str:
    command = mix.build_mix_command(music, plan, out)
    return command[command.index("-filter_complex") + 1]


def test_graph_resamples_and_formats_every_input():
    mix = load_mixdown()
    graph = _graph(mix, plan_for((2.0, 2.0), (4.0, 9.0)))
    assert graph.count("aresample=48000") == 3          # music + 2 voices
    # music, each voice, and the generated envelope stream
    assert graph.count("aformat=sample_fmts=fltp:channel_layouts=stereo") == 4


def test_the_envelope_is_a_per_sample_stream_not_a_volume_filter():
    """`volume` has no per-sample mode, and `eval=frame` measured as a ~5-step staircase across the
    250 ms attack (largest jump 0.22 linear). `aevalsrc` evaluates per sample; `amultiply` applies
    it. Measured deviation from the pure reference fell from 0.2245 to 0.0101."""
    mix = load_mixdown()
    graph = _graph(mix, plan_for())
    assert "aevalsrc='" in graph
    assert ":s=48000:c=stereo:d=30.000000" in graph
    assert "[mus][env]amultiply[music]" in graph
    assert "volume=" not in graph
    assert "eval=frame" not in graph


def test_the_envelope_expression_is_comma_escaped_for_the_graph_parser():
    """Every `min`/`max`/`clip` call contains commas, and the filtergraph parser splits option
    values on them."""
    mix = load_mixdown()
    canonical = mix.build_duck_expression(plan_for().duck_events, 0.35)
    assert "," in canonical
    escaped = mix.escape_filter_expression(canonical)
    assert "," not in escaped.replace(r"\,", "")
    assert escaped.count(r"\,") == canonical.count(",")
    assert escaped in _graph(mix, plan_for())


def test_the_envelope_stream_spans_the_whole_music():
    mix = load_mixdown()
    graph = _graph(mix, plan_for(music_duration=123.456789))
    assert "d=123.456789" in graph


@pytest.mark.parametrize("start,expected_ms", [
    (0.0, 0), (5.0, 5000), (12.345, 12345), (12.3456, 12346), (0.001, 1),
])
def test_adelay_is_the_exact_planned_millisecond(start: float, expected_ms: int):
    mix = load_mixdown()
    plan = plan_for((1.0,), (start,))
    command = mix.build_mix_command("m.mp3", plan, "o.wav")
    graph = command[command.index("-filter_complex") + 1]
    assert f"adelay={expected_ms}:all=1" in graph


def test_amix_policy():
    mix = load_mixdown()
    plan = plan_for((1.0, 1.0, 1.0), (2.0, 5.0, 8.0))
    command = mix.build_mix_command("m.mp3", plan, "o.wav")
    graph = command[command.index("-filter_complex") + 1]
    assert "amix=inputs=4:duration=longest:dropout_transition=0:normalize=0" in graph
    assert "normalize=1" not in graph


def test_limiter_parameters_including_latency_compensation():
    """`latency=1` is load-bearing: without it the limiter delays the whole master by 0.979 ms."""
    mix = load_mixdown()
    command = mix.build_mix_command("m.mp3", plan_for(), "o.wav")
    graph = command[command.index("-filter_complex") + 1]
    assert "alimiter=limit=0.97:attack=1:release=50:level=0:latency=1" in graph


def test_exact_duration_enforcement_and_output_encoding():
    mix = load_mixdown()
    plan = plan_for(music_duration=123.456789)
    command = mix.build_mix_command("m.mp3", plan, "o.wav")
    graph = command[command.index("-filter_complex") + 1]
    assert "apad,atrim=end=123.456789,asetpts=N/SR/TB[outa]" in graph
    assert command[command.index("-map") + 1] == "[outa]"
    assert command[command.index("-c:a") + 1] == "pcm_s24le"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-ac") + 1] == "2"


def test_a_voiceless_plan_still_builds_a_valid_command():
    mix = load_mixdown()
    graph = _graph(mix, plan_for((), ()))
    assert "amix=inputs=1" in graph
    assert "adelay" not in graph
    assert "aevalsrc='1'" in graph


def test_master_path_is_unique_and_in_the_session_dir():
    mix = load_mixdown()
    path = mix.master_path_for(r"C:\sessions\beatsync_abc")
    assert path.startswith(r"C:\sessions\beatsync_abc")
    assert os.path.basename(path).startswith("beatsync_mix_")
    assert path.endswith(".wav")


def test_master_path_never_uses_the_processing_dir():
    """`create_music_video` clears `get_processing_dir()` at startup, which would delete the master
    moments after it was written.

    Executable source only: the docstring names `get_processing_dir()` to say it is never used.
    """
    tree = _module_tree()
    for inner in ast.walk(tree):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    source = ast.unparse(ast.fix_missing_locations(tree))
    assert "get_processing_dir" not in source
    assert "PROCESSING_DIR" not in source
    # the master path is built from the caller-supplied session dir and nothing else
    assert "os.path.join(session_dir" in source


# ===========================================================================
# 5. EXECUTION AND FAILURE
# ===========================================================================


def test_successful_render_verifies_the_output(tmp_path):
    out = tmp_path / "mix.wav"
    out.write_bytes(b"x" * 1024)
    fake = FakeSubprocess([FakeCompleted(0, "", ""), FakeCompleted(0, "30.0", "")])
    mix = load_mixdown(fake)
    assert mix.render_mixed_master("m.mp3", plan_for(music_duration=30.0), str(out)) == str(out)
    assert len(fake.calls) == 2


def test_non_zero_exit_is_reported(tmp_path):
    fake = FakeSubprocess([FakeCompleted(1, "", "Invalid argument")])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="Audio mixdown failed"):
        mix.render_mixed_master("m.mp3", plan_for(), str(tmp_path / "mix.wav"))


def test_missing_output_is_reported(tmp_path):
    fake = FakeSubprocess([FakeCompleted(0, "", "")])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="no output file"):
        mix.render_mixed_master("m.mp3", plan_for(), str(tmp_path / "gone.wav"))


def test_empty_output_is_reported(tmp_path):
    out = tmp_path / "mix.wav"
    out.write_bytes(b"")
    fake = FakeSubprocess([FakeCompleted(0, "", "")])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="empty output file"):
        mix.render_mixed_master("m.mp3", plan_for(), str(out))


def test_duration_drift_beyond_tolerance_is_rejected(tmp_path):
    """Load-bearing: `create_music_video` derives the frame timeline from this file's duration."""
    out = tmp_path / "mix.wav"
    out.write_bytes(b"x" * 1024)
    fake = FakeSubprocess([FakeCompleted(0, "", ""), FakeCompleted(0, "29.900", "")])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="drift"):
        mix.render_mixed_master("m.mp3", plan_for(music_duration=30.0), str(out))


def test_duration_within_tolerance_is_accepted(tmp_path):
    out = tmp_path / "mix.wav"
    out.write_bytes(b"x" * 1024)
    fake = FakeSubprocess([FakeCompleted(0, "", ""), FakeCompleted(0, "30.0009", "")])
    mix = load_mixdown(fake)
    assert mix.render_mixed_master("m.mp3", plan_for(music_duration=30.0), str(out))


def test_mix_timeout_is_reported(tmp_path):
    fake = FakeSubprocess([subprocess.TimeoutExpired(cmd="ffmpeg", timeout=600)])
    mix = load_mixdown(fake)
    with pytest.raises(Exception, match="timed out"):
        mix.render_mixed_master("m.mp3", plan_for(), str(tmp_path / "mix.wav"))


def test_placement_failure_becomes_a_mix_error(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    voices = (fork_audio_mix.VoiceInput(0, "v.wav", 50.0),)
    with pytest.raises(Exception, match="no legal placement"):
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=200.0,
            beat_times=[i * 0.5 for i in range(400)],
            sections=(fork_audio_mix.MusicSection(0.0, 200.0, "drop"),),
            voices=voices, config=fork_audio_mix.AudioMixConfig(),
            session_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == [], "a failed plan must leave no master behind"


def test_a_failed_render_discards_its_partial_master(tmp_path):
    fake = FakeSubprocess([FakeCompleted(1, "", "boom")])
    mix = load_mixdown(fake)
    with pytest.raises(Exception):
        mix.build_mixed_master(
            music_path="m.mp3", music_duration=200.0,
            beat_times=[i * 0.5 for i in range(400)],
            sections=(fork_audio_mix.MusicSection(0.0, 200.0, "verse"),),
            voices=(fork_audio_mix.VoiceInput(0, "v.wav", 3.0),),
            config=fork_audio_mix.AudioMixConfig(), session_dir=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_discard_master_never_raises(tmp_path):
    mix = load_mixdown()
    mix.discard_master(None)
    mix.discard_master("")
    mix.discard_master(str(tmp_path / "never-existed.wav"))
    real = tmp_path / "m.wav"
    real.write_bytes(b"x")
    mix.discard_master(str(real))
    assert not real.exists()


# ===========================================================================
# 6. REAL FFMPEG INTEGRATION
#
# Synthetic tones under tmp_path only. Skipped when no binary is available, so the suite still runs
# on a bare interpreter per CLAUDE.md; set BEATSYNC_TEST_FFMPEG to point at the portable build.
# ===========================================================================


def _sibling_ffprobe(ffmpeg_path: str) -> str:
    """ffprobe beside ffmpeg. Only the basename is rewritten — replacing "ffmpeg" everywhere would
    also rename the containing `bin/ffmpeg` directory and point at a path that does not exist."""
    folder, name = os.path.split(ffmpeg_path)
    return os.path.join(folder, name.replace("ffmpeg", "ffprobe", 1))


def _ffmpeg_pair():
    explicit = os.environ.get("BEATSYNC_TEST_FFMPEG")
    if explicit and os.path.isfile(explicit):
        return explicit, _sibling_ffprobe(explicit)
    found = shutil.which("ffmpeg")
    if found:
        probe = shutil.which("ffprobe")
        if probe:
            return found, probe
    bundled = os.path.join(_REPO_ROOT, "bin", "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(bundled):
        return bundled, os.path.join(_REPO_ROOT, "bin", "ffmpeg", "ffprobe.exe")
    return None, None


FFMPEG_BIN, FFPROBE_BIN = _ffmpeg_pair()
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG_BIN and FFPROBE_BIN and os.path.isfile(FFPROBE_BIN)),
    reason="no FFmpeg available (set BEATSYNC_TEST_FFMPEG to the portable ffmpeg.exe)")

RATE = 48000


def write_tone(path, seconds, freq, amplitude, rate=RATE):
    frames = bytearray()
    for i in range(int(seconds * rate)):
        value = int(max(-1.0, min(1.0, amplitude * math.sin(2 * math.pi * freq * i / rate)))
                    * 32767)
        frames += struct.pack("<hh", value, value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return str(path)


def write_clicks(path, seconds, instants, amplitude=30000, rate=RATE):
    marks = {int(t * rate) for t in instants}
    frames = bytearray()
    for i in range(int(seconds * rate)):
        value = amplitude if i in marks else 0
        frames += struct.pack("<hh", value, value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return str(path)


def read_left(path):
    with wave.open(str(path), "rb") as handle:
        channels, width, frames = handle.getnchannels(), handle.getsampwidth(), handle.getnframes()
        raw = handle.readframes(frames)
    if width == 2:
        values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
        return [v / 32768.0 for v in values[::channels]]
    assert width == 3, width
    out = []
    for i in range(0, len(raw), 3 * channels):
        chunk = raw[i:i + 3]
        if len(chunk) < 3:
            break
        value = int.from_bytes(chunk, "little", signed=True)
        out.append(value / 8388608.0)
    return out


def real_probe_duration(path):
    result = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=60)
    return float(result.stdout.strip())


def real_mix(tmp_path, music, voices, starts, music_duration, out_name="mix.wav", percent=35):
    """Builds the command with the REAL shipped builder, then swaps in the real binary."""
    mix = load_mixdown()
    config = fork_audio_mix.AudioMixConfig(music_under_voice_percent=percent)
    placements = tuple(
        fork_audio_mix.VoicePlacement(i, str(v), real_probe_duration(v), s,
                                      s + real_probe_duration(v), "verse",
                                      fork_audio_mix.ANCHOR_BEAT, 0.0)
        for i, (v, s) in enumerate(zip(voices, starts)))
    plan = fork_audio_mix.AudioMixPlan(
        music_duration, placements,
        fork_audio_mix.build_duck_events(placements, music_duration, config), config)
    out = tmp_path / out_name
    command = mix.build_mix_command(str(music), plan, str(out))
    command[0] = FFMPEG_BIN
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    return out, plan


@needs_ffmpeg
def test_real_single_voice_mix_is_exact(tmp_path):
    music = write_tone(tmp_path / "music.wav", 12.0, 220, 0.5, rate=44100)
    voice = write_tone(tmp_path / "01 voice.wav", 3.0, 700, 0.6)
    duration = real_probe_duration(music)
    out, _ = real_mix(tmp_path, music, [voice], [4.0], duration)

    assert abs(real_probe_duration(out) - duration) <= 0.001
    result = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, timeout=60)
    assert "pcm_s24le" in result.stdout and "48000" in result.stdout and "2" in result.stdout


@needs_ffmpeg
def test_real_three_voices_with_mixed_sample_rates_and_spaces(tmp_path):
    folder = tmp_path / "with space"
    folder.mkdir()
    music = write_tone(folder / "music.wav", 20.0, 220, 0.4, rate=44100)
    voices = [
        write_tone(folder / "01 a.wav", 2.0, 700, 0.5, rate=22050),
        write_tone(folder / "02 b.wav", 1.5, 900, 0.5, rate=32000),
        write_tone(folder / "03 c.wav", 3.0, 1100, 0.5, rate=48000),
    ]
    duration = real_probe_duration(music)
    out, _ = real_mix(folder, music, voices, [2.0, 7.0, 13.0], duration, "mix out.wav")
    assert abs(real_probe_duration(out) - duration) <= 0.001


@needs_ffmpeg
def test_real_voice_lands_on_the_exact_planned_sample(tmp_path):
    """`adelay` is sample-exact and the limiter's latency is compensated, so an impulse placed at
    `start + offset` must come out at exactly that sample."""
    music = write_tone(tmp_path / "music.wav", 8.0, 220, 0.2)
    voice = write_clicks(tmp_path / "01 clicks.wav", 2.0, (0.25, 1.0))
    out, _ = real_mix(tmp_path, music, [voice], [3.0], 8.0)

    samples = read_left(out)
    peaks = [i for i, v in enumerate(samples) if abs(v) > 0.7]
    grouped = []
    for i in peaks:
        if not grouped or i - grouped[-1][-1] > 100:
            grouped.append([i])
        else:
            grouped[-1].append(i)
    firsts = [g[0] for g in grouped]
    assert firsts == [int(3.25 * RATE), int(4.0 * RATE)]


@needs_ffmpeg
def test_real_duck_envelope_matches_the_pure_reference(tmp_path):
    music = write_tone(tmp_path / "music.wav", 12.0, 220, 0.5)
    voice = write_tone(tmp_path / "01 v.wav", 3.0, 700, 0.0001)   # near-silent: isolate the music
    out, plan = real_mix(tmp_path, music, [voice], [5.0], 12.0)

    samples = read_left(out)

    def envelope_near(instant, window=0.02):
        lo = max(0, int((instant - window) * RATE))
        hi = min(len(samples), int((instant + window) * RATE))
        return max(abs(v) for v in samples[lo:hi])

    unducked = envelope_near(2.0)
    for instant, expected_gain in ((6.0, 0.35), (5.0, 0.35), (4.875, 0.675), (1.0, 1.0)):
        measured = envelope_near(instant) / unducked
        assert measured == pytest.approx(expected_gain, abs=0.06), (instant, measured)
        assert fork_audio_mix.duck_gain_at(plan.duck_events, instant) == pytest.approx(
            expected_gain, abs=1e-6)


@needs_ffmpeg
def test_real_loud_material_does_not_clip(tmp_path):
    """Full-scale music plus a full-scale voice sums to 1.35 and clips 7.9% of samples without the
    limiter; the ceiling is what makes the default safe."""
    music = write_tone(tmp_path / "music.wav", 8.0, 220, 1.0)
    voice = write_tone(tmp_path / "01 v.wav", 3.0, 700, 1.0)
    out, _ = real_mix(tmp_path, music, [voice], [3.0], 8.0)

    samples = read_left(out)
    assert max(abs(v) for v in samples) <= 0.9702
    assert sum(1 for v in samples if abs(v) >= 0.99999) == 0


@needs_ffmpeg
def test_real_non_clipping_material_is_left_alone(tmp_path):
    """The limiter is a ceiling, not normalisation: a quiet mix must keep its level."""
    music = write_tone(tmp_path / "music.wav", 8.0, 220, 0.45)
    voice = write_tone(tmp_path / "01 v.wav", 2.0, 700, 0.45)
    out, _ = real_mix(tmp_path, music, [voice], [3.0], 8.0)

    samples = read_left(out)
    quiet = max(abs(v) for v in samples[:int(2.0 * RATE)])
    assert quiet == pytest.approx(0.45, abs=0.01), quiet


@needs_ffmpeg
def test_real_voice_never_extends_the_master(tmp_path):
    music = write_tone(tmp_path / "music.wav", 6.0, 220, 0.4)
    voice = write_tone(tmp_path / "01 v.wav", 4.0, 700, 0.4)
    # deliberately overrunning; the planner forbids this, the graph must not extend regardless
    out, _ = real_mix(tmp_path, music, [voice], [5.0], 6.0)
    assert abs(real_probe_duration(out) - 6.0) <= 0.001
