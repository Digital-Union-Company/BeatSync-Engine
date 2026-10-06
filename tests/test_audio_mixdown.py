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
import dataclasses
import math
import os
import shutil
import struct
import subprocess
import sys
import time
import types
import wave
from typing import Any

import pytest

from beatsync_fork import audio_mix as fork_audio_mix
from beatsync_fork import render_worker as fork_render_worker
from beatsync_fork import smart_mix as fork_smart_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MIXDOWN = os.path.join(_REPO_ROOT, "src", "audio_mixdown.py")

#: Everything the suite executes. Module-level constants come along so the bodies see them.
_EXTRACTED = (
    "AudioMixError", "_run", "_tail", "probe_duration", "_selected_voice_paths",
    "prepare_voice_inputs", "build_duck_expression", "escape_filter_expression",
    "build_mix_command", "master_path_for", "render_mixed_master", "discard_master",
    "build_mixed_master",
    # [FORK] Digital-Union (Smart Mix V1 / E): the library preflight and its two helpers.
    "_role_relative_component", "prepare_sfx_inputs", "_walk_error",
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
        "fork_smart_mix": fork_smart_mix,
        "dataclasses": dataclasses,
        # [C3-R1A] `_run`'s cancellable branch needs a clock and the typed exception, and
        # `build_mixed_master` names `RenderCancelled` in an except clause. Both were *accidentally*
        # unnecessary before — Python only resolves an except expression when something propagates to
        # it, and no case here reached the lifecycle path — so this harness would have raised
        # `NameError` the moment one did. That contradicts this file's own claim that the extraction
        # list is the contract, so the namespace is completed rather than left to luck.
        "time": time,
        "RenderCancelled": fork_render_worker.RenderCancelled,
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


def _graph(mix, plan, music="m.mp3", out="o.wav", **kwargs) -> str:
    command = mix.build_mix_command(music, plan, out, **kwargs)
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
    """**Amended by Smart Mix V1 / E.**

    D synthesised a constant ``aevalsrc='1'`` envelope and ``amultiply``'d the music by it even
    with no voice — an exact no-op that still generated a full-length 48 kHz stereo stream. E
    routes the music directly when there are no duck events, which an SFX-only mix needs anyway.
    The test was not deleted: it now pins the *stronger* property, that a voiceless graph contains
    no envelope machinery at all while remaining a complete, valid command.
    """
    mix = load_mixdown()
    graph = _graph(mix, plan_for((), ()))
    assert "amix=inputs=1" in graph
    assert "adelay" not in graph
    assert "aevalsrc" not in graph
    assert "amultiply" not in graph
    assert ("[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[music]"
            in graph)
    # still exactly one limiter and one exact-duration boundary
    assert graph.count("alimiter") == 1
    assert graph.count("amix=") == 1


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


# ===========================================================================
# SMART MIX V1 (E): the library preflight
# ===========================================================================


def _sfx_library(tmp_path, layout):
    """Build a folder tree; `layout` maps a relative path to its bytes."""
    for relative, payload in layout.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return str(tmp_path)


_OK = b"x"
_ALL_ROLES = fork_smart_mix.ALL_ROLES


def _probes(count, duration="2.0"):
    return [FakeCompleted(0, duration, "")] * count


def test_prepare_sfx_inputs_classifies_orders_and_probes(tmp_path):
    root = _sfx_library(tmp_path, {
        "Impacts/02_b.wav": _OK, "Impacts/01_a.wav": _OK,
        "Risers/sub/deep.wav": _OK,
        "Ambience/pad.flac": _OK,
        "Transitions/t.mp3": _OK,
        "VocalShots/v.wav": _OK,
    })
    mix = load_mixdown(FakeSubprocess(_probes(10)))
    assets, diagnostics = mix.prepare_sfx_inputs(root, _ALL_ROLES)

    by_role = {}
    for item in assets:
        by_role.setdefault(item.role, []).append(os.path.basename(item.path))
    assert by_role["impact"] == ["01_a.wav", "02_b.wav"], "filename order, not walk order"
    assert by_role["riser"] == ["deep.wav"], "nested files inherit the first-level role"
    assert by_role["atmosphere"] == ["pad.flac"], "Ambience is a frozen atmosphere alias"
    assert set(by_role) == {"impact", "riser", "atmosphere", "transition", "vocal_shot"}
    assert all(item.duration == 2.0 for item in assets)
    assert dict(diagnostics.per_role)["impact"] == 2
    assert diagnostics.root == root
    assert not diagnostics.has_ignores, "a clean library reports nothing ignored"


def test_unknown_folders_and_root_files_are_reported_and_never_probed(tmp_path):
    root = _sfx_library(tmp_path, {
        "Impacts/a.wav": _OK,
        "Bogus/b.wav": _OK,
        "my impacts/c.wav": _OK,
        "loose.wav": _OK,
        "Impacts/notes.txt": _OK,
    })
    fake = FakeSubprocess(_probes(10))
    mix = load_mixdown(fake)
    assets, diagnostics = mix.prepare_sfx_inputs(root, _ALL_ROLES)

    assert [os.path.basename(a.path) for a in assets] == ["a.wav"]
    # already deterministically ordered by the scanner, not by os.walk
    assert diagnostics.unknown_folders == ("Bogus", "my impacts")
    assert diagnostics.root_level_files == 1
    assert diagnostics.unsupported_files == 1
    probed = [c[-1] for c in fake.calls]
    assert len(probed) == 1 and probed[0].endswith("a.wav")


def test_disabled_role_assets_are_neither_returned_nor_probed(tmp_path):
    root = _sfx_library(tmp_path, {"Impacts/a.wav": _OK, "Risers/b.wav": _OK})
    fake = FakeSubprocess(_probes(10))
    mix = load_mixdown(fake)
    assets, diagnostics = mix.prepare_sfx_inputs(root, ["impact"])

    assert [a.role for a in assets] == ["impact"]
    assert diagnostics.skipped_disabled_files == 1
    assert len(fake.calls) == 1, "a disabled role must cost no ffprobe"


@pytest.mark.parametrize("roles", [["impact"], ["riser", "vocal_shot"], _ALL_ROLES])
def test_a_complete_enabled_selection_never_silently_shrinks(tmp_path, roles):
    layout = {f"{folder}/{i}.wav": _OK
              for folder in ("Impacts", "Risers", "VocalShots") for i in range(3)}
    root = _sfx_library(tmp_path, layout)
    mix = load_mixdown(FakeSubprocess(_probes(40)))
    assets, _scan = mix.prepare_sfx_inputs(root, roles)
    wanted = fork_smart_mix.normalize_roles(roles)
    present = {"impact", "riser", "vocal_shot"} & wanted
    assert len(assets) == 3 * len(present)


def test_a_missing_root_is_fatal(tmp_path):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(Exception, match="does not exist"):
        mix.prepare_sfx_inputs(str(tmp_path / "nope"), _ALL_ROLES)


def test_a_non_directory_root_is_fatal(tmp_path):
    target = tmp_path / "a_file.wav"
    target.write_bytes(_OK)
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(Exception, match="not a folder"):
        mix.prepare_sfx_inputs(str(target), _ALL_ROLES)


@pytest.mark.parametrize("root", ["", "   ", None, 7])
def test_an_unusable_root_value_is_fatal(root):
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(Exception, match="no SFX library folder"):
        mix.prepare_sfx_inputs(root, _ALL_ROLES)


def test_zero_enabled_recognised_assets_is_fatal(tmp_path):
    root = _sfx_library(tmp_path, {"Bogus/a.wav": _OK, "loose.mp3": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(5)))
    with pytest.raises(Exception, match="no usable audio"):
        mix.prepare_sfx_inputs(root, _ALL_ROLES)


def test_only_unsupported_extensions_is_fatal(tmp_path):
    root = _sfx_library(tmp_path, {"Impacts/a.m4a": _OK, "Impacts/b.ogg": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(5)))
    with pytest.raises(Exception, match="no usable audio"):
        mix.prepare_sfx_inputs(root, _ALL_ROLES)


@pytest.mark.parametrize("result,match", [
    (FakeCompleted(1, "", "moov atom not found"), "Could not read"),
    (FakeCompleted(0, "not-a-number", ""), "Could not read a duration"),
    (FakeCompleted(0, "0", ""), "unusable duration"),
    (FakeCompleted(0, "-3.0", ""), "unusable duration"),
    (FakeCompleted(0, "nan", ""), "unusable duration"),
    (FakeCompleted(0, "inf", ""), "unusable duration"),
])
def test_a_corrupt_asset_in_an_enabled_role_is_fatal(tmp_path, result, match):
    root = _sfx_library(tmp_path, {"Impacts/a.wav": _OK})
    mix = load_mixdown(FakeSubprocess([result]))
    with pytest.raises(Exception, match=match):
        mix.prepare_sfx_inputs(root, _ALL_ROLES)


def test_a_probe_timeout_is_fatal(tmp_path):
    root = _sfx_library(tmp_path, {"Impacts/a.wav": _OK})

    class Timeout(FakeSubprocess):
        def run(self, command, **kwargs):
            raise subprocess.TimeoutExpired(command, 30)

    mix = load_mixdown(Timeout())
    with pytest.raises(Exception, match="Timed out"):
        mix.prepare_sfx_inputs(root, _ALL_ROLES)


def test_one_empty_enabled_role_is_not_fatal(tmp_path):
    """A role with no assets reports zero placements later; it does not fail the render."""
    root = _sfx_library(tmp_path, {"Impacts/a.wav": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(5)))
    assets, diagnostics = mix.prepare_sfx_inputs(root, _ALL_ROLES)
    assert len(assets) == 1
    assert dict(diagnostics.per_role)["riser"] == 0


def test_the_role_component_is_the_first_one_under_the_root():
    mix = load_mixdown(FakeSubprocess())
    root = os.path.join("C:", os.sep, "sfx")
    assert mix._role_relative_component(root, os.path.join(root, "Impacts", "a.wav")) == "Impacts"
    assert mix._role_relative_component(
        root, os.path.join(root, "Impacts", "deep", "a.wav")) == "Impacts"
    assert mix._role_relative_component(root, os.path.join(root, "a.wav")) is None


def test_an_unreadable_directory_is_never_silently_skipped():
    mix = load_mixdown(FakeSubprocess())
    with pytest.raises(OSError):
        mix._walk_error(OSError("permission denied"))


# ===========================================================================
# SMART MIX V1 (E): the executor graph
# ===========================================================================


def sfx(role, start, duration, trimmed=False, play=None, name="s"):
    play = play if play is not None else duration
    return fork_smart_mix.SfxPlacement(
        role=role, path=f"C:/sfx/{role}/{name}.wav", source_duration=duration,
        play_duration=play, start=start, end=start + play,
        anchor="x", reason="y", trimmed=trimmed)


def plan_with_sfx(durations=(), starts=(), placements=(), duration=30.0):
    """`plan_for`'s voice plan plus resolved SFX, attached exactly as the executor does."""
    return dataclasses.replace(plan_for(durations, starts, duration),
                               sfx_placements=tuple(placements))


def test_input_order_is_music_then_voice_then_sfx():
    mix = load_mixdown()
    plan = plan_with_sfx(
        durations=(2.0, 2.0), starts=(1.0, 5.0),
        placements=(sfx("impact", 12.0, 0.3, name="i"), sfx("riser", 18.0, 3.0, name="r")))
    command = mix.build_mix_command("C:/music.mp3", plan, "C:/out.wav", sfx_level_percent=50)
    inputs = [command[i + 1] for i, token in enumerate(command) if token == "-i"]
    assert inputs == ["C:/music.mp3", "C:/voices/00_clip.wav", "C:/voices/01_clip.wav",
                      "C:/sfx/impact/i.wav", "C:/sfx/riser/r.wav"]


def test_each_sfx_stream_index_matches_its_own_delay_and_path():
    mix = load_mixdown()
    placements = (sfx("impact", 2.0, 0.3, name="i"),
                  sfx("transition", 12.5, 1.0, name="t"),
                  sfx("vocal_shot", 20.25, 0.8, name="v"))
    plan = plan_with_sfx(durations=(2.0,), starts=(1.0,), placements=placements)
    command = mix.build_mix_command("C:/music.mp3", plan, "C:/out.wav")
    graph = command[command.index("-filter_complex") + 1]
    inputs = [command[i + 1] for i, token in enumerate(command) if token == "-i"]
    for offset, placement in enumerate(placements):
        index = 2 + offset                      # 0 music, 1 voice, then SFX
        assert inputs[index] == placement.path
        assert f"[{index}:a]" in graph
        segment = graph.split(f"[{index}:a]")[1].split(";")[0]
        assert f"adelay={int(round(placement.start * 1000))}:all=1" in segment


@pytest.mark.parametrize("level,expected", [(0, "0.000000"), (50, "0.500000"),
                                            (100, "1.000000"), (35, "0.350000")])
def test_sfx_gain_is_the_linear_level(level, expected):
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("impact", 2.0, 0.3),))
    graph = _graph(mix, plan, sfx_level_percent=level)
    assert f"volume={expected}" in graph


def test_a_malformed_sfx_level_falls_back_to_the_default():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("impact", 2.0, 0.3),))
    for bad in (None, "50", 50.5, True, float("nan")):
        assert "volume=0.500000" in _graph(mix, plan, sfx_level_percent=bad)


def test_atrim_appears_only_for_a_trimmed_atmosphere():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(
        sfx("atmosphere", 1.0, 40.0, trimmed=True, play=20.0, name="bed"),
        sfx("impact", 25.0, 0.3, name="hit"),
    ))
    graph = _graph(mix, plan)
    bed = graph.split("[1:a]")[1].split(";")[0]
    hit = graph.split("[2:a]")[1].split(";")[0]
    assert "atrim=end=20.000000" in bed
    assert "atrim" not in hit
    # the only other atrim in the graph is the master's exact-duration boundary
    assert graph.count("atrim=") == 2


def test_an_untrimmed_atmosphere_gets_no_atrim():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("atmosphere", 1.0, 5.0, trimmed=False, name="bed"),))
    graph = _graph(mix, plan)
    assert graph.count("atrim=") == 1


def test_sfx_only_renders_without_any_envelope():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("impact", 2.0, 0.3), sfx("riser", 8.0, 3.0)))
    graph = _graph(mix, plan)
    assert "aevalsrc" not in graph and "amultiply" not in graph
    assert "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[music]" in graph
    assert "amix=inputs=3" in graph


def test_voice_and_sfx_keep_the_duck_envelope_on_the_music_only():
    mix = load_mixdown()
    plan = plan_with_sfx(durations=(2.0,), starts=(1.0,),
                         placements=(sfx("impact", 10.0, 0.3),))
    graph = _graph(mix, plan)
    assert "aevalsrc='1-" in graph
    assert "[mus][env]amultiply[music]" in graph
    sfx_segment = graph.split("[2:a]")[1].split(";")[0]
    assert "amultiply" not in sfx_segment
    voice_segment = graph.split("[1:a]")[1].split(";")[0]
    assert "volume=" not in voice_segment, "voice is never attenuated by the SFX level"


def test_exactly_one_amix_and_one_limiter_with_sfx():
    mix = load_mixdown()
    plan = plan_with_sfx(durations=(2.0,), starts=(1.0,),
                         placements=tuple(sfx("impact", 5.0 + i, 0.3, name=f"i{i}")
                                          for i in range(8)))
    graph = _graph(mix, plan)
    assert graph.count("amix=") == 1
    assert graph.count("alimiter") == 1
    assert graph.count("[outa]") == 1
    assert "amix=inputs=10" in graph


def test_the_master_boundary_is_unchanged_by_sfx():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("impact", 2.0, 0.3),), duration=123.456789)
    graph = _graph(mix, plan)
    assert "apad,atrim=end=123.456789,asetpts=N/SR/TB[outa]" in graph


def test_sfx_paths_with_spaces_survive_as_one_argv_entry():
    mix = load_mixdown()
    placement = fork_smart_mix.SfxPlacement(
        role="impact", path="C:\\My SFX\\big hit.wav", source_duration=0.3, play_duration=0.3,
        start=2.0, end=2.3, anchor="x", reason="y", trimmed=False)
    plan = plan_with_sfx(placements=(placement,))
    command = mix.build_mix_command("C:/music.mp3", plan, "C:/out.wav")
    assert "C:\\My SFX\\big hit.wav" in command


def test_build_mixed_master_attaches_sfx_and_forwards_the_level():
    """The executor never plans SFX — it receives resolved placements and attaches them.

    The render itself cannot complete against a stubbed subprocess (no file is produced), so the
    command is captured on its way out and the expected failure is allowed to surface.
    """
    placements = (sfx("impact", 2.0, 0.3),)
    captured = []

    class Recorder(FakeSubprocess):
        def run(self, command, **kwargs):
            captured.append(list(command))
            return super().run(command, **kwargs)

    mix = load_mixdown(Recorder([FakeCompleted(0, "", "")]))
    with pytest.raises(Exception, match="produced no output file"):
        mix.build_mixed_master(
            music_path="C:/music.mp3", music_duration=30.0, beat_times=[0.0, 1.0],
            sections=(), voices=(), config=fork_audio_mix.AudioMixConfig(),
            session_dir="C:/session", sfx_placements=placements, sfx_level_percent=70)

    graph = captured[0][captured[0].index("-filter_complex") + 1]
    assert "volume=0.700000" in graph, "the resolved level must reach FFmpeg"
    assert "C:/sfx/impact/s.wav" in captured[0]
    assert "aevalsrc" not in graph, "no voice -> no envelope"


def test_build_mixed_master_plans_no_sfx_itself():
    """Structural: SFX planning lives in the pure module and is never re-done by the executor."""
    source = ast.unparse(_module_tree())
    for forbidden in ("plan_sfx", "project_structure", "amount_params", "_place_"):
        assert forbidden not in source, f"audio_mixdown re-implements planning via {forbidden}"


def test_build_mix_command_defaults_the_level_for_direct_callers():
    mix = load_mixdown()
    plan = plan_with_sfx(placements=(sfx("impact", 2.0, 0.3),))
    graph = _graph(mix, plan)
    assert "volume=0.500000" in graph


def test_a_plan_without_the_sfx_field_still_builds():
    """`AudioMixPlan`'s new field is defaulted, so every pre-existing construction stays valid."""
    mix = load_mixdown()
    plan = fork_audio_mix.AudioMixPlan(30.0, (), (), fork_audio_mix.AudioMixConfig())
    assert plan.sfx_placements == ()
    graph = _graph(mix, plan)
    assert "amix=inputs=1" in graph


# ===========================================================================
# SMART MIX V1 (E): real-FFmpeg integration
# ===========================================================================


def real_sfx_mix(tmp_path, music, music_duration, placements, voices=(), starts=(),
                 sfx_level_percent=50, out_name="sfxmix.wav", percent=35):
    """Builds with the REAL shipped builder, then swaps in the real binary."""
    mix = load_mixdown()
    config = fork_audio_mix.AudioMixConfig(music_under_voice_percent=percent)
    voice_placements = tuple(
        fork_audio_mix.VoicePlacement(i, str(v), real_probe_duration(v), s,
                                      s + real_probe_duration(v), "verse",
                                      fork_audio_mix.ANCHOR_BEAT, 0.0)
        for i, (v, s) in enumerate(zip(voices, starts)))
    plan = fork_audio_mix.AudioMixPlan(
        music_duration, voice_placements,
        fork_audio_mix.build_duck_events(voice_placements, music_duration, config),
        config, tuple(placements))
    out = tmp_path / out_name
    command = mix.build_mix_command(str(music), plan, str(out),
                                    sfx_level_percent=sfx_level_percent)
    command[0] = FFMPEG_BIN
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-1500:]
    return out, plan


def real_sfx(role, path, start, trimmed=False, play=None):
    duration = real_probe_duration(path)
    play = play if play is not None else duration
    return fork_smart_mix.SfxPlacement(
        role=role, path=str(path), source_duration=duration, play_duration=play,
        start=start, end=start + play, anchor="x", reason="y", trimmed=trimmed)


@needs_ffmpeg
def test_real_sfx_only_master_is_exact_and_needs_no_envelope(tmp_path):
    music = write_tone(tmp_path / "music.wav", 12.0, 220, 0.4, rate=44100)
    hit = write_clicks(tmp_path / "impact.wav", 0.5, (0.0,))
    duration = real_probe_duration(music)
    out, _ = real_sfx_mix(tmp_path, music, duration, [real_sfx("impact", hit, 4.0)])

    assert abs(real_probe_duration(out) - duration) <= 0.001
    result = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, timeout=60)
    assert "pcm_s24le" in result.stdout and "48000" in result.stdout and "2" in result.stdout


@needs_ffmpeg
def test_real_sfx_lands_on_the_exact_planned_sample(tmp_path):
    music = write_tone(tmp_path / "music.wav", 10.0, 220, 0.2)
    hit = write_clicks(tmp_path / "impact.wav", 0.5, (0.0,))
    out, _ = real_sfx_mix(tmp_path, music, 10.0,
                          [real_sfx("impact", hit, 3.0), real_sfx("impact", hit, 6.5)],
                          sfx_level_percent=100)

    samples = read_left(out)
    peaks = [i for i, v in enumerate(samples) if abs(v) > 0.7]
    grouped = []
    for i in peaks:
        if not grouped or i - grouped[-1][-1] > 100:
            grouped.append([i])
        else:
            grouped[-1].append(i)
    assert [g[0] for g in grouped] == [int(3.0 * RATE), int(6.5 * RATE)]


@needs_ffmpeg
@pytest.mark.parametrize("level,expected", [(100, 1.0), (50, 0.5), (0, 0.0)])
def test_real_sfx_level_is_a_linear_gain(tmp_path, level, expected):
    """A silent music bed makes the SFX peak the only signal, so the level is measurable."""
    music = write_tone(tmp_path / "music.wav", 6.0, 220, 0.0)
    hit = write_clicks(tmp_path / "impact.wav", 0.5, (0.0,), amplitude=32000)
    out, _ = real_sfx_mix(tmp_path, music, 6.0, [real_sfx("impact", hit, 2.0)],
                          sfx_level_percent=level, out_name=f"g{level}.wav")
    samples = read_left(out)
    window = samples[int(1.9 * RATE):int(2.2 * RATE)]
    peak = max(abs(v) for v in window) if window else 0.0
    assert peak == pytest.approx(expected * (32000 / 32768.0), abs=0.02)


@needs_ffmpeg
def test_real_trimmed_atmosphere_stops_at_its_planned_length(tmp_path):
    """`atrim` is applied before the delay, so a trimmed bed ends exactly where the plan says."""
    music = write_tone(tmp_path / "music.wav", 14.0, 220, 0.0)
    bed = write_tone(tmp_path / "bed.wav", 8.0, 1500, 0.6)
    placement = real_sfx("atmosphere", bed, 2.0, trimmed=True, play=3.0)
    out, _ = real_sfx_mix(tmp_path, music, 14.0, [placement], sfx_level_percent=100)

    samples = read_left(out)

    def energy(a, b):
        chunk = samples[int(a * RATE):int(b * RATE)]
        return max(abs(v) for v in chunk) if chunk else 0.0

    assert energy(2.5, 4.5) > 0.3, "the bed must play for its trimmed length"
    assert energy(5.5, 7.5) < 0.05, "and must be silent after it"


@needs_ffmpeg
def test_real_voice_and_sfx_share_one_master_with_voice_ducking_music_only(tmp_path):
    music = write_tone(tmp_path / "music.wav", 16.0, 220, 0.5, rate=44100)
    voice = write_tone(tmp_path / "01 voice.wav", 2.0, 700, 0.6)
    hit = write_clicks(tmp_path / "impact.wav", 0.5, (0.0,), amplitude=32000)
    duration = real_probe_duration(music)
    out, _ = real_sfx_mix(tmp_path, music, duration,
                          [real_sfx("impact", hit, 10.0)],
                          voices=[voice], starts=[4.0], sfx_level_percent=100)

    assert abs(real_probe_duration(out) - duration) <= 0.001
    samples = read_left(out)
    # the SFX impulse is outside the duck window and keeps its full level
    window = samples[int(9.9 * RATE):int(10.2 * RATE)]
    assert max(abs(v) for v in window) > 0.8


@needs_ffmpeg
def test_real_many_loud_sfx_do_not_clip_the_master(tmp_path):
    """The one existing limiter is still the only safety ceiling, with SFX summed in."""
    music = write_tone(tmp_path / "music.wav", 12.0, 220, 0.9, rate=44100)
    hit = write_tone(tmp_path / "impact.wav", 0.4, 180, 1.0)
    placements = [real_sfx("impact", hit, 1.0 + i) for i in range(9)]
    out, _ = real_sfx_mix(tmp_path, music, real_probe_duration(music), placements,
                          sfx_level_percent=100)

    samples = read_left(out)
    assert max(abs(v) for v in samples) <= 0.9701, "the limiter ceiling must still hold"


@needs_ffmpeg
def test_real_sfx_never_extends_the_master(tmp_path):
    """A tail running past the music is cut by the master boundary, not allowed to grow it."""
    music = write_tone(tmp_path / "music.wav", 6.0, 220, 0.3)
    bed = write_tone(tmp_path / "long.wav", 10.0, 900, 0.5)
    out, _ = real_sfx_mix(tmp_path, music, 6.0,
                          [real_sfx("atmosphere", bed, 4.0)], sfx_level_percent=100)
    assert abs(real_probe_duration(out) - 6.0) <= 0.001


@needs_ffmpeg
def test_real_sfx_paths_with_spaces_and_mixed_rates(tmp_path):
    folder = tmp_path / "my sfx folder"
    folder.mkdir()
    music = write_tone(folder / "music.wav", 14.0, 220, 0.4, rate=44100)
    a = write_tone(folder / "big hit.wav", 0.4, 180, 0.6, rate=22050)
    b = write_tone(folder / "soft riser.wav", 2.0, 1200, 0.5, rate=32000)
    duration = real_probe_duration(music)
    out, _ = real_sfx_mix(folder, music, duration,
                          [real_sfx("impact", a, 3.0), real_sfx("riser", b, 8.0)],
                          out_name="mix out.wav")
    assert abs(real_probe_duration(out) - duration) <= 0.001


# ===========================================================================
# SMART MIX V1 (E, R1): library diagnostics survive to the report
# ===========================================================================
#
# The R1 defect was NOT that the scan failed to collect these — it collected all four and then
# only `root` was ever read, so a typo'd folder was invisible to the user. Asserting on the
# scanner's own return value would therefore have passed throughout the bug. These tests run the
# REAL production chain instead: prepare_sfx_inputs -> plan_sfx -> SmartMixPlan.report_lines().


def _structure_for_report():
    sections = ((0.0, 29.0, "intro"), (29.0, 53.0, "intro"), (53.0, 74.0, "drop"),
                (74.0, 100.0, "breakdown"))
    beats = tuple(i * 0.5 for i in range(200))
    count = len(beats)
    return fork_smart_mix.MusicStructure(
        music_duration=100.0, beat_times=beats,
        is_bar_anchor=tuple(i % 4 == 0 for i in range(count)),
        is_phrase_anchor=tuple(i % 8 == 0 for i in range(count)),
        impact_strength=tuple((i % 13) / 13.0 for i in range(count)),
        sections=sections)


def _report_for(tmp_path, layout, roles=None):
    """The real chain: scan the real tree, plan with the real planner, render the real report."""
    root = _sfx_library(tmp_path, layout)
    mix = load_mixdown(FakeSubprocess(_probes(40)))
    assets, diagnostics = mix.prepare_sfx_inputs(
        root, fork_smart_mix.ALL_ROLES if roles is None else roles)
    plan = fork_smart_mix.plan_sfx(
        _structure_for_report(), assets,
        fork_smart_mix.SmartMixConfig(enabled_roles=(fork_smart_mix.ALL_ROLES
                                                     if roles is None else roles)),
        library_root=root, library_diagnostics=diagnostics)
    return "\n".join(plan.report_lines()), plan, diagnostics


def test_a_typo_folder_is_visible_in_the_final_report(tmp_path):
    """The exact scenario from the contract: `Impats/` beside a valid `Risers/`.

    Preflight succeeds because one usable enabled asset exists, so without R1 the user was told
    only that there "happened to be no impacts".
    """
    text, _plan, _diag = _report_for(tmp_path, {
        "Impats/typo.wav": _OK,
        "Risers/valid.wav": _OK,
        "loose.wav": _OK,
        "Risers/notes.txt": _OK,
    })
    assert "Impats" in text, "the typo'd folder must be named in the report"
    assert "Ignored unknown role folders: Impats" in text
    assert "Ignored root-level files: 1" in text
    assert "Ignored unsupported files: 1" in text


def test_several_unknown_folders_are_listed_deterministically(tmp_path):
    text, _plan, diagnostics = _report_for(tmp_path, {
        "zeta/a.wav": _OK, "Misc/b.wav": _OK, "Impats/c.wav": _OK, "alpha/d.wav": _OK,
        "Risers/valid.wav": _OK,
    })
    assert diagnostics.unknown_folders == ("alpha", "Impats", "Misc", "zeta")
    assert "Ignored unknown role folders: alpha, Impats, Misc, zeta" in text


def test_disabled_role_files_are_counted_neutrally_and_never_probed(tmp_path):
    root = _sfx_library(tmp_path, {
        "Risers/valid.wav": _OK,
        "Impacts/a.wav": _OK, "Impacts/b.wav": _OK,
    })
    fake = FakeSubprocess(_probes(10))
    mix = load_mixdown(fake)
    assets, diagnostics = mix.prepare_sfx_inputs(root, ["riser"])
    plan = fork_smart_mix.plan_sfx(
        _structure_for_report(), assets,
        fork_smart_mix.SmartMixConfig(enabled_roles=["riser"]),
        library_root=root, library_diagnostics=diagnostics)
    text = "\n".join(plan.report_lines())

    assert len(fake.calls) == 1, "disabled-role files must still cost no ffprobe"
    assert "Files in disabled roles skipped: 2" in text
    for loaded in ("error", "warning", "invalid", "wrong"):
        assert loaded not in text.casefold(), "disabling a role is a choice, not a mistake"


def test_a_clean_library_adds_no_ignored_noise(tmp_path):
    text, _plan, diagnostics = _report_for(tmp_path, {
        "Impacts/a.wav": _OK, "Risers/b.wav": _OK, "Atmosphere/c.wav": _OK,
        "Transitions/d.wav": _OK, "VocalShots/e.wav": _OK,
    })
    assert not diagnostics.has_ignores
    assert "Ignored" not in text
    assert "disabled roles" not in text


def test_the_zero_placement_report_still_explains_itself_and_shows_diagnostics(tmp_path):
    """A valid library that yields no musical placements keeps every existing explanation."""
    structure = fork_smart_mix.MusicStructure(
        music_duration=60.0, beat_times=(0.0, 1.0),
        is_bar_anchor=(False, False), is_phrase_anchor=(False, False),
        impact_strength=(0.0, 0.0), sections=((0.0, 60.0, "verse"),))
    root = _sfx_library(tmp_path, {"Risers/valid.wav": _OK, "Impats/typo.wav": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(10)))
    assets, diagnostics = mix.prepare_sfx_inputs(root, fork_smart_mix.ALL_ROLES)
    plan = fork_smart_mix.plan_sfx(structure, assets, fork_smart_mix.SmartMixConfig(),
                                   library_root=root, library_diagnostics=diagnostics)
    text = "\n".join(plan.report_lines())

    assert plan.total == 0
    assert "Smart Mix" in text and "Amount:" in text and "Total SFX: 0" in text
    assert "No SFX placed" in text
    assert "enabled but the library has no assets for it" in text
    assert "Ignored unknown role folders: Impats" in text


def test_the_diagnostics_reach_the_plan_without_touching_placements(tmp_path):
    """Diagnostics are report provenance: identical placements with and without them."""
    root = _sfx_library(tmp_path, {
        "Risers/valid.wav": _OK, "Impats/typo.wav": _OK, "loose.wav": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(10)))
    assets, diagnostics = mix.prepare_sfx_inputs(root, fork_smart_mix.ALL_ROLES)
    structure = _structure_for_report()
    config = fork_smart_mix.SmartMixConfig()

    with_diag = fork_smart_mix.plan_sfx(structure, assets, config,
                                        library_root=root, library_diagnostics=diagnostics)
    without = fork_smart_mix.plan_sfx(structure, assets, config, library_root=root)
    assert with_diag.placements == without.placements
    assert with_diag.library_diagnostics.unknown_folders == ("Impats",)
    assert without.library_diagnostics.unknown_folders == ()


def test_prepare_sfx_inputs_returns_the_pure_diagnostic_value(tmp_path):
    """The scan hands back an immutable value, not a loose dict only `root` was read from."""
    root = _sfx_library(tmp_path, {"Risers/valid.wav": _OK})
    mix = load_mixdown(FakeSubprocess(_probes(5)))
    _assets, diagnostics = mix.prepare_sfx_inputs(root, fork_smart_mix.ALL_ROLES)
    assert isinstance(diagnostics, fork_smart_mix.SfxLibraryDiagnostics)
    with pytest.raises(Exception):
        diagnostics.root_level_files = 3        # frozen
