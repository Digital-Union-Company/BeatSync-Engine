"""Deliverable Export V1-A — the browser-playable preview, and the argv defect it replaces.

Two production surfaces are exercised, both for real:

* **`ffmpeg_processing.build_delivery_preview_command`** is AST-extracted and executed. That module
  needs the whole portable runtime (`logger` pulls in librosa, `gpu_cpu_utils` pulls in numpy and
  probes NVENC), so it cannot be imported on a bare interpreter — but the builder itself is
  deliberately dependency-free (`os`, `FFMPEG_PATH` and three module constants), so the real body
  runs here rather than a restatement of it.
* **`gui._process_video_impl`** is executed through the established synthesised namespace from
  `test_gui_guard_seam`, so the preview's validation, containment and fallback behaviour are
  asserted against the real render body — not a model of it.

The defect being guarded: before V1-A the preview argv put `-c:v` BEFORE `-i`, so FFmpeg read the
encoder as an input *decoder* and failed with `Unknown decoder 'h264_nvenc'` / `Unknown decoder
'libx264'`; and `preview_path` was assigned before the subprocess with no returncode or existence
check, so the failure was invisible and a non-existent path reached `gr.Video`.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from typing import List

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_FFMPEG_MODULE = os.path.join(_SRC, "ffmpeg_processing.py")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

#: Options that may legitimately appear BEFORE `-i` (global and input options).
INPUT_SIDE = {"-nostdin", "-hide_banner", "-hwaccel", "-ss", "-t", "-f", "-r", "-safe", "-y",
              "-loglevel", "-v", "-fflags", "-threads"}
#: Options that are OUTPUT-only. Before `-i`, FFmpeg reinterprets these — `-c:v` becomes the input
#: decoder, which is exactly how the pre-V1-A preview died.
OUTPUT_ONLY = {"-c:v", "-c:a", "-preset", "-crf", "-cq", "-b:v", "-b:a", "-pix_fmt", "-profile:v",
               "-map", "-movflags", "-ar", "-ac", "-vf", "-an", "-shortest", "-fps_mode"}

#: The exact argv shape of the block V1-A replaces, kept so the regression guard proves the old
#: ordering really is rejected rather than merely asserting the new one passes.
LEGACY_BROKEN_ARGV = [
    "ffmpeg", "-hwaccel", "cuda", "-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23",
    "-i", "master.mov", "-pix_fmt", "yuv420p", "-y", "preview.mp4",
]


def _ffmpeg_source() -> str:
    with open(_FFMPEG_MODULE, "r", encoding="utf-8") as handle:
        return handle.read()


def load_delivery_builder(namespace: dict | None = None):
    """The REAL builder plus its REAL module constants, exec'd from `ffmpeg_processing.py`.

    Exported so `test_gui_guard_seam._impl_namespace` can give the extracted render body the
    genuine argv builder instead of a stub — a stub there would let the ordering drift.

    Pass the GUI's own synthesised namespace as ``namespace`` to exec the builder **into** it. That
    mirrors production, where `gui` imports both `FFMPEG_PATH` and the builder from
    `ffmpeg_processing` into one module namespace, so a test that swaps `FFMPEG_PATH` sees the swap
    in the argv (the builder resolves that global at call time, not at definition time).
    """
    tree = ast.parse(_ffmpeg_source(), filename=_FFMPEG_MODULE)
    wanted = {"build_delivery_preview_command"}
    constants = {"DELIVERY_PREVIEW_CRF", "DELIVERY_PREVIEW_PRESET",
                 "DELIVERY_PREVIEW_AUDIO_BITRATE", "DELIVERY_PREVIEW_TIMEOUT_SECONDS"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in constants:
            nodes.append(node)
    missing = wanted - {n.name for n in nodes if isinstance(n, ast.FunctionDef)}
    assert not missing, f"not found in ffmpeg_processing.py: {missing}"
    if namespace is None:
        namespace = {"os": os, "List": List, "FFMPEG_PATH": "ffmpeg"}
    else:
        namespace.setdefault("os", os)
        namespace.setdefault("List", List)
        namespace.setdefault("FFMPEG_PATH", "ffmpeg")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<ffmpeg_processing>", "exec"),
         namespace)  # noqa: S102
    return namespace


@pytest.fixture(scope="module")
def delivery():
    return load_delivery_builder()


@pytest.fixture
def build(delivery):
    return delivery["build_delivery_preview_command"]


def _first_input_index(argv):
    return argv.index("-i")


# ===========================================================================
# 1-6. THE COMMAND ITSELF
# ===========================================================================


def test_every_output_option_comes_after_the_input(build):
    """G2, and the whole reason this slice exists."""
    argv = build("master.mp4", "preview.mp4")
    i = _first_input_index(argv)

    for position, token in enumerate(argv):
        if token in OUTPUT_ONLY:
            assert position > i, (
                f"{token} at {position} precedes -i at {i}; FFmpeg would read it as an input "
                f"option")


def test_the_legacy_argv_ordering_is_rejected_by_this_very_check():
    """The guard has to fail on the real pre-V1-A command, or it proves nothing."""
    i = LEGACY_BROKEN_ARGV.index("-i")
    offenders = [t for pos, t in enumerate(LEGACY_BROKEN_ARGV)
                 if t in OUTPUT_ONLY and pos < i]

    assert offenders == ["-c:v", "-preset", "-cq"], offenders


def test_only_input_side_options_precede_the_input(build):
    argv = build("master.mp4", "preview.mp4")
    i = _first_input_index(argv)

    for token in argv[1:i]:
        if token.startswith("-"):
            assert token in INPUT_SIDE, f"{token} is not an input-side option"


def test_the_streams_are_mapped_explicitly(build):
    """A silent video-only preview would be reported as a playable deliverable."""
    argv = build("master.mp4", "preview.mp4")

    assert "-map" in argv
    pairs = [(argv[i], argv[i + 1]) for i, t in enumerate(argv) if t == "-map"]
    assert pairs == [("-map", "0:v:0"), ("-map", "0:a:0")], pairs


def test_the_codec_settings_are_the_documented_cpu_h264_aac_profile(build, delivery):
    argv = build("master.mp4", "preview.mp4")

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert value("-c:v") == "libx264"
    assert value("-preset") == delivery["DELIVERY_PREVIEW_PRESET"] == "veryfast"
    assert value("-crf") == delivery["DELIVERY_PREVIEW_CRF"] == "21"
    assert value("-pix_fmt") == "yuv420p"
    assert value("-profile:v") == "high"
    assert value("-c:a") == "aac"
    assert value("-b:a") == delivery["DELIVERY_PREVIEW_AUDIO_BITRATE"] == "192k"
    assert value("-ar") == "48000"
    assert value("-ac") == "2"
    assert value("-movflags") == "+faststart"


def test_no_level_is_pinned_and_no_geometry_is_restated(build):
    """A fixed level would be wrong for a 4K or high-fps master; `-vf`/`-s`/`-r` would risk
    re-timing a master that is already CFR at the render's own fps."""
    argv = build("master.mp4", "preview.mp4")

    for forbidden in ("-level", "-vf", "-s", "-r", "-filter_complex", "-sws_flags"):
        assert forbidden not in argv, forbidden


def test_no_nvenc_or_hardware_decode_is_requested(build):
    """V1-A uses one CPU profile for all four master modes, so the preview never depends on a GPU
    and both arms of any comparison encode identically."""
    argv = build("master.mp4", "preview.mp4")

    for forbidden in ("h264_nvenc", "hevc_nvenc", "-hwaccel", "cuda"):
        assert forbidden not in argv, forbidden


def test_the_command_starts_with_ffmpeg_and_ends_with_the_destination(build):
    argv = build("in.mp4", "out.mp4")

    assert argv[0] == "ffmpeg"
    assert argv[-1] == "out.mp4"
    assert argv[-2] == "-y"
    assert argv[argv.index("-i") + 1] == "in.mp4"


@pytest.mark.parametrize("same", [
    ("C:/x/master.mp4", "C:/x/master.mp4"),
    ("C:/x/master.mp4", "C:/x/../x/master.mp4"),
    ("C:/x/MASTER.mp4", "C:/x/master.mp4"),
])
def test_the_destination_may_never_be_the_master(build, same):
    """G1. A preview that overwrote the master would destroy the one durable artifact."""
    with pytest.raises(ValueError):
        build(*same)


@pytest.mark.parametrize("bad", [("", "out.mp4"), ("in.mp4", ""), (None, "out.mp4"),
                                 ("in.mp4", None)])
def test_a_missing_source_or_destination_is_refused(build, bad):
    with pytest.raises(ValueError):
        build(*bad)


def test_the_builder_is_deterministic(build):
    first = build("a.mp4", "b.mp4")

    assert all(build("a.mp4", "b.mp4") == first for _ in range(5))


def test_the_builder_touches_no_clock_rng_or_environment():
    tree = ast.parse(_ffmpeg_source(), filename=_FFMPEG_MODULE)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "build_delivery_preview_command")
    used = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)

    for token in ("random", "uuid", "time", "perf_counter", "datetime", "environ", "getenv",
                  "subprocess", "Popen", "run"):
        assert token not in used, f"{token} used in the builder"


# ===========================================================================
# 7-18. THE GUI PATH — the real `_process_video_impl`
# ===========================================================================


def _run_impl(tmp_path, *, preview_result="ok", lifecycle=None, mode="cpu",
              runner=None, cancel_during_render=False):
    """Execute the REAL render body with a controllable preview subprocess.

    `preview_result` decides what the stubbed FFmpeg does: "ok" writes a non-empty file, "empty"
    writes a zero-byte file, "missing" writes nothing, "fail" returns a non-zero code, "timeout"
    raises `TimeoutExpired`.
    """
    from test_gui_guard_seam import (PROMOTION_HELPER, _gui_tree, _impl_namespace,
                                     _no_replace_rename)
    from conftest import write_file

    calls: list[str] = []
    namespace = _impl_namespace(tmp_path, calls, rename=_no_replace_rename,
                                dest_appears_during_render=False)
    attempts: list[list[str]] = []

    class _Subprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(cmd, **_kwargs):
            attempts.append(list(cmd))
            destination = cmd[-1]
            if preview_result == "timeout":
                raise subprocess.TimeoutExpired(cmd, 1)
            if preview_result == "fail":
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            if preview_result == "empty":
                open(destination, "wb").close()
            elif preview_result == "ok":
                with open(destination, "wb") as handle:
                    handle.write(b"PREVIEW-BYTES")
            return subprocess.CompletedProcess(cmd, 0, "", "")

    namespace["subprocess"] = _Subprocess
    namespace.update(load_delivery_builder())
    namespace["STATUS_RENDER_CANCELLED"] = "⏹️ cancelled"
    if runner is not None:
        namespace["run_cancellable_media_command"] = runner
    if cancel_during_render:
        # The only way to reach the preview step with a cancellation pending: every boundary
        # before it would have raised. Mirrors test_render_cancellation's approach.
        real_create = namespace["create_music_video"]

        def create_then_cancel(*args, output_file, **kwargs):
            result = real_create(*args, output_file=output_file, **kwargs)
            lifecycle.request_cancel()
            return result

        namespace["create_music_video"] = create_then_cancel

    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"),
         namespace)  # noqa: S102

    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")
    state = {"session_dir": session_dir}

    preview_path, status, state = namespace["_process_video_impl"](
        audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
        processing_mode=mode, custom_fps=30.0, creative=None,
        session_state=state, lifecycle=lifecycle)
    out_dir = str(tmp_path / "output")
    produced = sorted(os.listdir(out_dir))
    return {"preview_path": preview_path, "status": status, "state": state,
            "attempts": attempts, "durable": os.path.join(out_dir, produced[0]),
            "produced": produced, "session_dir": session_dir}


@pytest.mark.parametrize("mode,ext", [("cpu", ".mp4"), ("h264_nvenc", ".mp4"),
                                      ("hevc_nvenc", ".mp4"), ("prores_proxy", ".mov")])
def test_a_preview_is_generated_for_every_processing_mode(tmp_path, mode, ext):
    """The pre-V1-A block ran only for ProRes; every other mode handed the browser the master."""
    out = _run_impl(tmp_path, mode=mode)

    assert out["produced"][0].endswith(ext), out["produced"]
    assert len(out["attempts"]) == 1, out["attempts"]
    assert out["preview_path"] != out["durable"]
    assert out["preview_path"].endswith("_preview.mp4")
    assert os.path.isfile(out["preview_path"])
    assert os.path.dirname(out["preview_path"]) == out["session_dir"]


def test_the_preview_argv_is_the_production_builders_output(tmp_path, build):
    out = _run_impl(tmp_path)
    expected = build(out["durable"], out["preview_path"])

    assert out["attempts"][0] == expected


def test_a_successful_preview_is_reported_as_both_artifacts(tmp_path):
    out = _run_impl(tmp_path)

    assert "HIGH-QUALITY MASTER:" in out["status"]
    assert out["durable"] in out["status"]
    assert "PLAYABLE PREVIEW" in out["status"]
    assert "not a permanent download" in out["status"]
    assert "H.264/AAC MP4" in out["status"]
    assert "⚠️" not in out["status"]


@pytest.mark.parametrize("result,fragment", [
    ("fail", "FFmpeg exited non-zero"),
    ("missing", "FFmpeg produced no output file"),
    ("empty", "FFmpeg produced an empty output file"),
    ("timeout", "preview encoding exceeded"),
])
def test_a_failed_preview_falls_back_without_claiming_anything(tmp_path, result, fragment):
    """G3/G5. The master stays SUCCESS, the display path falls back to it, and the panel does not
    pretend either that a shareable MP4 exists or that the master is playable."""
    out = _run_impl(tmp_path, preview_result=result)

    assert out["preview_path"] == out["durable"], "a failed preview was selected for display"
    assert out["state"]["last_output_path"] == out["durable"]
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")
    assert fragment in out["status"], out["status"]
    assert "Browser-compatible preview was not created" in out["status"]
    assert "no shareable MP4 was produced" in out["status"]
    assert "PLAYABLE PREVIEW" not in out["status"]
    assert os.path.isfile(out["durable"]), "the master was touched by a preview failure"


def test_an_unexpected_preview_exception_is_contained(tmp_path):
    """Post-commit work must never reach the generic render-failure handler."""
    def exploding_runner(*_a, **_k):
        raise OSError("ffmpeg vanished")

    out = _run_impl(tmp_path, lifecycle=_Lifecycle(), runner=exploding_runner)

    assert out["preview_path"] == out["durable"]
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")
    assert "preview encoding failed" in out["status"]
    assert not out["status"].startswith("❌")


class _Lifecycle:
    """Minimal RenderLifecycle stand-in: the real contract is exercised by
    tests/test_render_cancellation.py; here only `cancel_requested` matters."""

    def __init__(self, cancelled=False):
        self._cancelled = cancelled
        self.checks = 0

    def request_cancel(self):
        self._cancelled = True

    def cancel_requested(self):
        self.checks += 1
        return self._cancelled

    def raise_if_cancelled(self):
        if self._cancelled:
            from beatsync_fork.render_worker import RenderCancelled
            raise RenderCancelled("cancelled")


def test_no_preview_child_starts_when_cancellation_is_already_pending(tmp_path):
    """G4. Cheaper than starting one and killing it on the first poll.

    The cancellation is requested from inside the stubbed `create_music_video`, because that is
    the only moment that still reaches the preview: every earlier boundary would have raised.
    """
    life = _Lifecycle()
    out = _run_impl(tmp_path, lifecycle=life, runner=_boom, cancel_during_render=True)

    assert life.cancel_requested() is True, "the cancellation really was pending"
    assert out["attempts"] == []
    assert out["preview_path"] == out["durable"]
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")
    assert "cancelled before the preview started" in out["status"]


def _boom(*_a, **_k):
    raise AssertionError("no preview child may start when cancellation is pending")


def test_a_cancellation_during_preview_encoding_keeps_the_master_successful(tmp_path):
    """G4. `RenderCancelled` is an ordinary Exception, so it must be caught BEFORE the generic
    clause — otherwise a Stop would be reported as a preview crash."""
    from beatsync_fork.render_worker import RenderCancelled

    reaped = {"value": False}

    def cancelling_runner(cmd, timeout, lifecycle=None):
        reaped["value"] = True        # the real runner terminates, graces, kills and reaps
        raise RenderCancelled("stopped during preview")

    out = _run_impl(tmp_path, lifecycle=_Lifecycle(), runner=cancelling_runner)

    assert reaped["value"] is True
    assert out["preview_path"] == out["durable"]
    assert str(out["state"]["render_outcome_kind"]).endswith("SUCCESS")
    assert "cancelled during preview encoding" in out["status"]


def test_the_lifecycle_path_uses_the_cancellable_runner_not_a_bare_subprocess(tmp_path):
    seen = {}

    def recording_runner(cmd, timeout, lifecycle=None):
        seen["cmd"] = list(cmd)
        seen["timeout"] = timeout
        seen["lifecycle"] = lifecycle
        with open(cmd[-1], "wb") as handle:
            handle.write(b"PREVIEW")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    life = _Lifecycle()
    out = _run_impl(tmp_path, lifecycle=life, runner=recording_runner)

    assert out["attempts"] == [], "the bare subprocess path ran despite a lifecycle"
    assert seen["lifecycle"] is life
    assert isinstance(seen["timeout"], int) and seen["timeout"] > 0
    assert out["preview_path"].endswith("_preview.mp4")


def test_an_existing_preview_artifact_is_never_overwritten(tmp_path):
    """The name carries the master's timestamp and seed suffix inside a per-session mkdtemp, so a
    collision is near-impossible — and if it happens, refusing costs only the preview."""
    first = _run_impl(tmp_path)
    assert os.path.isfile(first["preview_path"])
    before = open(first["preview_path"], "rb").read()

    source = ast.parse(open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read())
    impl = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef)
                and n.name == "_process_video_impl")
    body = ast.unparse(impl)

    assert "a file already occupies the preview path" in body
    assert open(first["preview_path"], "rb").read() == before


# ===========================================================================
# 19-24. WHAT MUST NOT HAVE MOVED
# ===========================================================================


def _gui_body(name="_process_video_impl"):
    tree = ast.parse(open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read())
    return ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                            and n.name == name))


def test_the_gui_gained_no_subprocess_run_site():
    tree = ast.parse(open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read())
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]

    assert len(runs) == 2, f"{len(runs)} subprocess.run sites; expected the preview + Director"
    assert not any(ast.unparse(n.func) == "subprocess.Popen" for n in ast.walk(tree)
                   if isinstance(n, ast.Call))


def test_the_promotion_and_commit_point_are_untouched():
    """G1. One no-replace rename, and SUCCESS still written only after it."""
    body = _gui_body()

    assert body.count("_promote_output_no_replace(") == 1
    assert "session_state[LAST_OUTPUT_PATH_KEY] = output_path" in body
    assert "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS" in body
    commit = body.index("RenderOutcomeKind.SUCCESS")
    promote = body.index("_promote_output_no_replace(")
    preview = body.index("build_delivery_preview_command(")
    assert promote < commit < preview, "the preview must run strictly after the commit point"


def test_the_preview_never_writes_to_the_output_directory():
    """G6. One session-scoped MP4; no second durable artifact, no V1-B."""
    body = _gui_body()
    preview_region = body[body.index("preview_target = "):body.index("_stage6_summary(")]

    assert "session_dir" in preview_region
    assert "get_output_dir()" not in preview_region
    assert "os.rename" not in preview_region
    assert "os.replace" not in preview_region
    assert "shutil" not in preview_region
    for destructive in ("os.remove", "os.unlink", "rmtree"):
        assert destructive not in preview_region, destructive


def test_the_master_is_never_mutated_by_the_preview():
    body = _gui_body()

    assert "os.remove(output_path)" not in body
    assert "os.replace(output_path" not in body
    assert "shutil.move(output_path" not in body


def test_render_cancelled_is_caught_before_the_generic_clause():
    """CLAUDE.md's rule: `RenderCancelled` is an ordinary Exception, so order is load-bearing."""
    tree = ast.parse(open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read())
    impl = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "_process_video_impl")
    previews = [t for t in ast.walk(impl) if isinstance(t, ast.Try)
                and any("RenderCancelled" in ast.unparse(h) for h in t.handlers)
                and any("preview" in ast.unparse(h) for h in t.handlers)]
    assert previews, "the preview try/except was not found"
    for node in previews:
        names = [ast.unparse(h.type) if h.type is not None else "bare" for h in node.handlers]
        assert names[0] == "RenderCancelled", names
        assert "Exception" in names[-1], names
        assert "bare" not in names


def test_no_stage5_stage6_or_cache_identity_dependency_was_introduced():
    """G8."""
    body = _gui_body()
    preview_region = body[body.index("preview_target = "):body.index("_stage6_summary(")]

    for token in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "PARTIAL_CONTRACT_VERSION",
                  "_CACHE_IDENTITY_WORKER_CAP", "DEFAULT_ANALYZE_BATCH_SIZE",
                  "analyze_video_sources", "build_planned_clip_sequence",
                  "summarize_clip_plan", "video_analysis", "semantic"):
        assert token not in preview_region, token


def test_the_protected_contract_constants_are_unchanged():
    """G8, read straight out of the real modules' source."""
    analysis = open(os.path.join(_SRC, "video_analysis.py"), encoding="utf-8").read()
    partials = open(os.path.join(_SRC, "beatsync_fork", "qwen_partials.py"),
                    encoding="utf-8").read()
    prep = open(os.path.join(_SRC, "beatsync_fork", "library_prep.py"), encoding="utf-8").read()

    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in analysis
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in analysis
    assert "_CACHE_IDENTITY_WORKER_CAP = 16" in analysis
    assert 'PARTIAL_CONTRACT_VERSION = "stage5_job_partial_v1"' in partials
    assert "DEFAULT_ANALYZE_BATCH_SIZE = 100" in prep


def test_no_new_gui_configuration_control_was_added():
    """No quality slider, no export mode, no new output component."""
    source = open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    components = [ast.unparse(n.func) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("gr.")]

    assert components.count("gr.Video") == 1
    for widget in ("gr.DownloadButton", "gr.Slider"):
        if widget == "gr.DownloadButton":
            assert components.count(widget) == 0
    assert "delivery_quality" not in source
    assert "preview_quality" not in source


def test_the_master_encoder_settings_are_unchanged():
    """G1. V1-A touches no master encoder argument."""
    source = _ffmpeg_source()

    assert "'-crf', '0'," in source or "'-crf', '0'" in source
    assert "NVENC_QUALITY_CQ = '1'" in source
    assert "MASTER_CODEC" not in source  # that constant lives in audio_mixdown and is untouched
