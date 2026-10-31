"""FUN R10 Final Frame Fix: a segment requesting N output frames produces N, or fails clearly.

Three tiers, deliberately kept apart because they prove different things:

1. **The pure contract.** `beatsync_fork.frame_lock` is stdlib-only, so it is imported normally and
   tested as ordinary code -- the progress reading, the recovery arithmetic, the wording.
2. **The real seams.** `ffmpeg_processing` and `video_processor` cannot be imported on a bare
   interpreter, so the house techniques apply: AST assertions for structure, and AST-extracted real
   function bodies executed against a stubbed namespace for behaviour. The retry, the single
   confirmation probe, the attempt bound and the cancellation escape are all proved against the
   *shipped* extractor body, not a reimplementation of it.
3. **Real FFmpeg, opt-in.** The reported defect is a property of the binary's seek behaviour, not
   of the string handed to it, so the exact reported case and its controls are re-measured against
   actual FFmpeg/FFprobe when one is available (`BEATSYNC_TEST_FFMPEG`), and skipped otherwise.

The reported case, for the record. `src_b_1080p.mp4`: 30.000000 s, 30 fps, 900 frames. A 720p25
render planned `start=28.840000` for `1.160000` and asked for 29 output frames; FFmpeg wrote 28. At
30 fps the same window asked for 35 and got 34. The cause is accurate `-ss` snapping the window
start forward to the next source frame, which at the end of a source has no content behind it to
make up -- so the shortfall reaches the output. It reproduced identically on pristine `main`, so it
is pre-existing and not a Repeat Music defect.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import types
from typing import Any, List

import pytest

from beatsync_fork import frame_lock as fork_frame_lock
from beatsync_fork import render_worker as fork_render_worker

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FFMPEG_SRC = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
_PROCESSOR_SRC = os.path.join(_REPO_ROOT, "src", "video_processor.py")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


@pytest.fixture(scope="module")
def ffmpeg_tree() -> ast.Module:
    return _tree(_FFMPEG_SRC)


@pytest.fixture(scope="module")
def processor_tree() -> ast.Module:
    return _tree(_PROCESSOR_SRC)


# ===========================================================================
# 1. The pure contract
# ===========================================================================

def _progress(*blocks: str) -> str:
    return "\n".join(blocks)


def _block(frame: Any, marker: str = "end") -> str:
    lines = []
    if frame is not None:
        lines.append(f"frame={frame}")
    lines.extend(["fps=25.00", "out_time_ms=1160000", f"progress={marker}"])
    return "\n".join(lines)


def test_an_exact_progress_count_is_exact():
    verdict = fork_frame_lock.frames_from_progress(29, _block(29))
    assert verdict.measured and verdict.exact
    assert verdict.produced == 29
    assert verdict.short_by == 0 and verdict.over_by == 0
    assert verdict.measured_by == fork_frame_lock.MEASURED_BY_PROGRESS


def test_the_reported_defect_reads_as_one_frame_short():
    verdict = fork_frame_lock.frames_from_progress(29, _block(28))
    assert verdict.measured and not verdict.exact
    assert verdict.short_by == 1 and verdict.over_by == 0


def test_a_long_segment_is_reported_rather_than_clamped():
    """`-vframes` makes this impossible, so a non-zero `over_by` is evidence, not an oddity."""
    verdict = fork_frame_lock.frames_from_progress(29, _block(30))
    assert verdict.produced == 30, "the count is rendered verbatim, never clamped to the request"
    assert verdict.over_by == 1 and verdict.short_by == 0
    assert not verdict.exact


def test_a_stream_whose_last_block_is_not_terminal_is_unmeasured():
    """A truncated record is not a smaller valid one. It must not pass and must not fail."""
    verdict = fork_frame_lock.frames_from_progress(29, _block(29, marker="continue"))
    assert not verdict.measured and verdict.produced is None
    assert not verdict.exact, "exactness requires affirmative evidence"
    assert verdict.short_by is None, "unmeasured is not zero"
    assert verdict.measured_by == fork_frame_lock.UNMEASURED


def test_a_terminal_block_carrying_no_frame_count_is_unmeasured():
    """The hardened block semantics: nothing is inherited across a boundary."""
    verdict = fork_frame_lock.frames_from_progress(29, _progress(_block(29, "continue"),
                                                                _block(None, "end")))
    assert not verdict.measured, "the 29 belongs to an earlier block and may not be reused"


def test_the_last_completed_block_wins_over_an_earlier_terminal_one():
    verdict = fork_frame_lock.frames_from_progress(29, _progress(_block(29, "end"),
                                                                _block(28, "end")))
    assert verdict.produced == 28


@pytest.mark.parametrize("raw", [None, "", b"", "garbage", b"\xff\xfe not utf8",
                                "frame=\nprogress=end", "frame=-1\nprogress=end",
                                "frame=28.5\nprogress=end"])
def test_unreadable_progress_never_raises_and_never_fabricates(raw):
    verdict = fork_frame_lock.frames_from_progress(29, raw)
    assert verdict.requested == 29
    assert verdict.produced is None and not verdict.exact


def test_the_container_reading_is_its_own_provenance():
    verdict = fork_frame_lock.from_container_count(29, 29)
    assert verdict.exact and verdict.measured_by == fork_frame_lock.MEASURED_BY_CONTAINER
    assert not fork_frame_lock.from_container_count(29, None).measured


@pytest.mark.parametrize("junk", [None, "", "x", -1, True, False, 28.5, float("nan"),
                                  float("inf")])
def test_a_junk_count_is_unmeasured_not_zero(junk):
    assert not fork_frame_lock.from_container_count(29, junk).measured


# --- the recovery arithmetic ------------------------------------------------

def test_the_reported_case_recovers_to_one_source_frame_before_the_anchor():
    """start=28.84, need 1.16 s, 30.000000 s source at 30 fps.

    The anchor is `30.0 - 1.16 = 28.84` -- the window already ends exactly at the source end -- and
    the margin is one SOURCE frame, 1/30 s, because that is what accurate `-ss` rounding can
    consume. Measured against real FFmpeg below: the corrected window yields 29 output frames at
    25 fps and 35 at 30 fps, both exact.
    """
    recovered = fork_frame_lock.recovery_seek(28.84, 1.16, 30.0, 30.0)
    assert recovered == pytest.approx(28.84 - 1.0 / 30.0, abs=1e-9)
    assert recovered < 28.84


def test_the_thirty_fps_case_also_undoes_the_planner_overhang():
    """At 30 fps the quantised need is 35/30 = 1.166667 s, longer than the 1.16 the planner
    clamped the start with, so the window overhangs the source before the seek even rounds. The
    anchor term fixes that; the margin fixes the rounding."""
    needed = 35 / 30.0
    recovered = fork_frame_lock.recovery_seek(28.84, needed, 30.0, 30.0)
    assert recovered == pytest.approx(30.0 - needed - 1.0 / 30.0, abs=1e-9)


def test_the_margin_is_one_source_frame_not_one_output_frame():
    """A 24 fps source feeding a 30 fps render needs 1/24 s of lead-in; 1/30 would not be enough."""
    recovered = fork_frame_lock.recovery_seek(10.0, 1.0, 11.0, 24.0)
    assert recovered == pytest.approx(10.0 - 1.0 / 24.0, abs=1e-9)


def test_a_source_shorter_than_the_segment_has_no_recovery():
    """No seek can conjure content that is not in the file. This must not be silently retried."""
    assert fork_frame_lock.recovery_seek(0.0, 2.0, 0.5, 30.0) is None


def test_a_window_already_at_zero_has_no_recovery():
    assert fork_frame_lock.recovery_seek(0.0, 1.0, 30.0, 30.0) is None


def test_recovery_never_returns_a_position_that_is_not_earlier():
    """Retrying the same window is not a recovery, so it is refused rather than spent."""
    for start in (0.0, 1e-9, 0.01):
        recovered = fork_frame_lock.recovery_seek(start, 1.0, 1.0, 30.0)
        assert recovered is None or recovered < start


def test_recovery_never_returns_a_negative_seek():
    recovered = fork_frame_lock.recovery_seek(0.02, 1.0, 30.0, 1.0)
    assert recovered is None or recovered >= 0.0


@pytest.mark.parametrize("args", [
    (None, 1.16, 30.0, 30.0), ("x", 1.16, 30.0, 30.0), (28.84, None, 30.0, 30.0),
    (28.84, 1.16, None, 30.0), (28.84, 1.16, 30.0, None), (28.84, 0.0, 30.0, 30.0),
    (28.84, 1.16, 0.0, 30.0), (28.84, 1.16, 30.0, 0.0), (28.84, 1.16, 30.0, -5.0),
    (-1.0, 1.16, 30.0, 30.0), (float("nan"), 1.16, 30.0, 30.0),
    (28.84, float("inf"), 30.0, 30.0),
])
def test_recovery_is_total_and_refuses_junk(args):
    assert fork_frame_lock.recovery_seek(*args) is None


def test_a_mid_file_window_is_never_moved_by_the_anchor_term():
    """`min(start, duration - needed)` must not drag a window that already fits toward the end."""
    recovered = fork_frame_lock.recovery_seek(5.0, 1.0, 30.0, 30.0)
    assert recovered == pytest.approx(5.0 - 1.0 / 30.0, abs=1e-9)


# --- the wording -----------------------------------------------------------

def test_the_shortfall_sentence_states_the_measurement_and_its_provenance():
    verdict = fork_frame_lock.from_container_count(29, 28)
    text = fork_frame_lock.describe_shortfall(verdict, 2)
    assert "28" in text and "29" in text
    assert "container" in text
    assert "2 attempts" in text
    assert len(text) <= 240


def test_the_shortfall_sentence_claims_nothing_about_the_source_bytes():
    """A short segment is a short segment. This module has established nothing about the file."""
    for produced in (0, 28, None):
        verdict = fork_frame_lock.FrameCountVerdict(29, produced,
                                                    fork_frame_lock.MEASURED_BY_PROGRESS)
        text = fork_frame_lock.describe_shortfall(verdict).lower()
        for forbidden in ("corrupt", "damaged", "broken", "invalid file", "unreadable"):
            assert forbidden not in text, text


def test_a_long_segment_gets_its_own_sentence():
    verdict = fork_frame_lock.from_container_count(29, 31)
    text = fork_frame_lock.describe_shortfall(verdict)
    assert "LONG" in text and "31" in text


def test_an_unmeasured_verdict_does_not_claim_a_count():
    text = fork_frame_lock.describe_shortfall(fork_frame_lock.FrameCountVerdict(29))
    assert "none reported" in text
    assert " 0 " not in text, "unmeasured must never render as zero frames"


def test_the_truncated_plan_sentence_names_both_numbers():
    text = fork_frame_lock.describe_truncated_plan(29, 13, 0.52)
    assert "29" in text and "13" in text and "0.520s" in text
    assert "refused rather than truncated" in text
    assert len(text) <= 240


def test_every_description_is_bounded_and_truncates_visibly():
    verdict = fork_frame_lock.from_container_count(29, 28)
    short = fork_frame_lock.describe_shortfall(verdict, 2, budget=40)
    assert len(short) == 40 and short.endswith("...")


def test_the_module_runs_nothing_and_touches_no_disk():
    """Pure by contract: the decisions are here, the child processes are in `ffmpeg_processing`."""
    with open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "frame_lock.py"),
              "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("subprocess.run", "subprocess.Popen", "open", "os.stat", "os.path.exists",
                      "os.remove", "time.time", "time.perf_counter", "random.random"):
        assert forbidden not in calls, f"{forbidden} is reachable from a pure contract module"
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, (ast.Import,))}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree)
                 if isinstance(n, ast.ImportFrom)}
    assert imported <= {"__future__", "math", "dataclasses", "typing", "beatsync_fork"}, imported


def test_the_attempt_bound_is_two():
    """One planned window, at most one corrected one. A third would be a retry loop."""
    assert fork_frame_lock.MAX_EXTRACTION_ATTEMPTS == 2


# ===========================================================================
# 2. The real seams -- structure
# ===========================================================================

def test_the_extractor_gained_no_parameter(ffmpeg_tree):
    """The whole correction fits inside the existing signature, which several suites pin exactly.

    That is not an accident of taste: the measured shortfall IS the trigger, so nothing needs to be
    told to the extractor in advance. A `source_duration` parameter would have been a second
    source of truth for a number the call site already probes.
    """
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")
    assert [a.arg for a in fn.args.args] == [
        "video_file", "start_time", "duration", "output_file", "fps",
        "target_size", "use_nvenc", "gpu_encoder", "geometry", "lifecycle"]
    assert not fn.args.kwonlyargs


def test_the_extractor_asks_ffmpeg_for_its_own_frame_count(ffmpeg_tree):
    """`-progress pipe:1` on stdout: free, because stdout was already captured and unused."""
    source = ast.unparse(_func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed"))
    assert "'-progress', 'pipe:1'" in source
    assert "frames_from_progress(output_frame_count, result.stdout)" in source
    # stderr is the failure summariser's input and must not be redirected to carry progress
    assert "pipe:2" not in source


def test_the_confirmation_probe_is_metadata_only(ffmpeg_tree):
    """A full decode of every suspect clip is exactly the expense this must not become.

    Asserted over the **argv literal**, never over the unparsed source. A text scan here fails on
    the docstring that explains *why* the expensive variant was rejected -- naming the rejected
    flag in prose is not using it, and this repository has tripped that same trap before.
    """
    fn = _func(ffmpeg_tree, "_probe_output_frame_count")
    argv = [n for n in ast.walk(fn) if isinstance(n, ast.List)]
    assert len(argv) == 1, "exactly one command is built here"
    tokens = [e.value for e in argv[0].elts if isinstance(e, ast.Constant)]
    assert "stream=nb_frames" in tokens
    for flag in ("-count_frames", "-show_frames", "-f", "null"):
        assert flag not in tokens, f"{flag} would make this a decode, not a header read"


def test_the_confirmation_probe_returns_none_rather_than_zero(ffmpeg_tree):
    """A probe that could not read the header has no opinion; zero would be a verdict."""
    source = ast.unparse(_func(ffmpeg_tree, "_probe_output_frame_count"))
    assert "return None" in source
    assert "return 0" not in source


def test_the_confirmation_probe_uses_a_narrow_handler(ffmpeg_tree):
    """`tests/test_render_cancellation.py` pins the count of BROAD handlers in this module, so a
    new `except Exception` here would be an unnoticed fourth. The narrow tuple is also the more
    honest one -- these are the only failures a header read has."""
    fn = _func(ffmpeg_tree, "_probe_output_frame_count")
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)]
    assert len(handlers) == 1
    rendered = ast.unparse(handlers[0].type)
    assert "Exception" not in rendered.replace("SubprocessError", ""), rendered
    for expected in ("OSError", "ValueError", "subprocess.SubprocessError"):
        assert expected in rendered, rendered


def test_the_retry_is_bounded_by_the_shared_constant(ffmpeg_tree):
    """The bound lives in the pure module, where it is one testable number, not a local literal."""
    source = ast.unparse(_func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed"))
    assert "fork_frame_lock.MAX_EXTRACTION_ATTEMPTS" in source
    assert "while True" in source, "the bound is the counter, so the loop needs no condition"


def test_an_unmeasured_segment_is_not_refused(ffmpeg_tree):
    """Absence of evidence is not evidence. A build that emits no usable count must still render."""
    source = ast.unparse(_func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed"))
    assert "verdict.exact or not verdict.measured" in source
    assert "verdict.measured and (not verdict.exact)" in source \
        or "verdict.measured and not verdict.exact" in source


def test_the_prores_path_verifies_its_frame_count_too(ffmpeg_tree):
    """It carried the identical defect: same `-ss`, same trim, same `-vframes`, and an acceptance
    test of `returncode == 0` plus a non-empty file."""
    source = ast.unparse(_func(ffmpeg_tree, "extract_prores_segment_random"))
    assert "_probe_output_frame_count(output_file)" in source
    assert "fork_frame_lock.recovery_seek" in source
    assert "build_cmd(fast_seek, seek_time)" in source


def test_the_prores_reseek_is_spent_once(ffmpeg_tree):
    source = ast.unparse(_func(ffmpeg_tree, "extract_prores_segment_random"))
    assert "reseek_spent" in source
    assert source.count("plan.append") == 1


def test_the_two_pre_existing_prores_attempts_are_unchanged(ffmpeg_tree):
    source = ast.unparse(_func(ffmpeg_tree, "extract_prores_segment_random"))
    assert "('fast intra-frame seek', True, 180)" in source
    assert "('safe accurate seek retry', False, 360)" in source


def test_the_prores_path_refuses_a_source_too_short_for_its_slot(ffmpeg_tree):
    """Its `else` branch shortened `duration` to the source length, and `frame_count` is derived
    from `duration` -- so it silently shortened the segment exactly as the worker's clamp did."""
    fn = _func(ffmpeg_tree, "extract_prores_segment_random")
    source = ast.unparse(fn)
    assert "fork_frame_lock.describe_truncated_plan" in source
    raises = [n for n in ast.walk(fn) if isinstance(n, ast.Raise)
              and "describe_truncated_plan" in ast.unparse(n)]
    assert len(raises) == 1, "it is refused, and this path reports by raising"


def test_the_truncated_plan_sentence_has_exactly_two_consumers():
    """One per extraction path. The NVENC/CPU worker builds its own inline, because its body is
    executed against a namespace holding only the names it uses -- a module-level call there would
    be a `NameError` in three suites. That asymmetry is deliberate and is why the wording is
    asserted on both sides separately."""
    ffmpeg_source = ast.unparse(_tree(_FFMPEG_SRC))
    processor_source = ast.unparse(_tree(_PROCESSOR_SRC))
    assert ffmpeg_source.count("describe_truncated_plan") == 1
    assert "describe_truncated_plan" not in processor_source
    assert "too short for its planned segment" in processor_source


def test_the_frame_locked_timeline_builder_is_untouched(processor_tree):
    """The correction is confined to extraction. The timeline arithmetic is not part of it."""
    source = ast.unparse(_func(processor_tree, "build_frame_aligned_cut_timeline"))
    assert "np.rint(raw_cut_times * fps)" in source
    assert "segment_frames = np.diff(cut_frames).astype(int)" in source
    assert "segment_durations = segment_frames / fps" in source
    assert "frame_lock" not in source, "the builder gained nothing; it was already correct"


def test_the_worker_guard_adds_no_probe_subprocess_or_loop(processor_tree):
    """Pinned by `tests/test_extraction_failure_context.py` too; restated here because the R10
    guard is the change that could have broken it."""
    fn = _func(processor_tree, "create_clip_parallel")
    source = ast.unparse(fn)
    for forbidden in ("subprocess", "Popen", "_run_media_command", "ffprobe", "FFMPEG_PATH",
                      "fork_frame_lock", "frame_lock"):
        assert forbidden not in source, forbidden
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))]
    extracts = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "extract_clip_segment_ffmpeg_detailed"]
    assert len(extracts) == 1, "still exactly one attempt in the worker"


def test_the_repeat_music_timeline_is_not_touched():
    """Scope boundary: this milestone is the extraction contract, not Repeat Music."""
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "music_repeat.py")
    if not os.path.isfile(path):
        pytest.skip("music_repeat module not present")
    with open(path, "r", encoding="utf-8") as handle:
        body = handle.read()
    assert "frame_lock" not in body


def test_stage_five_cache_identity_is_untouched():
    """A frame-count correction may not re-key a GPU analysis cache."""
    tree = _tree(os.path.join(_REPO_ROOT, "src", "video_analysis.py"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    assert found.get("CACHE_CONTRACT_VERSION") == "stage5_cache_v3"
    assert found.get("ANALYSIS_VERSION") == "auto_av_analysis_v8_llama_vulkan_batched"


# ===========================================================================
# 3. The real seams -- behaviour, over the SHIPPED extractor body
# ===========================================================================

_EXTRACTED = ("seconds_to_frame_count", "frame_count_to_seconds",
              "get_cpu_h264_quality_args", "_probe_output_frame_count",
              "extract_clip_segment_ffmpeg_detailed")


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _load_extractor(runs, *, probe_counts=None, duration=30.0, source_fps=30.0,
                    real_runner=None, ffmpeg="C:/bin/ffmpeg.exe", ffprobe="C:/bin/ffprobe.exe",
                    write_output=True):
    """The REAL `extract_clip_segment_ffmpeg_detailed`, over a recording runner.

    `runs` is either a list of `_Result` (one per expected attempt) or a callable taking the argv.
    The namespace is hand-built, so a body that grows a dependency outside this list fails here
    with `NameError` instead of quietly going untested.
    """
    tree = _tree(_FFMPEG_SRC)
    nodes = [_func(tree, name) for name in _EXTRACTED]
    calls = {"media": [], "duration": [], "fps": [], "probe": []}
    queue = list(runs) if isinstance(runs, (list, tuple)) else None
    probe_queue = list(probe_counts) if probe_counts is not None else None

    def _run_media_command(cmd, timeout=None, lifecycle=None):
        calls["media"].append({"cmd": list(cmd), "timeout": timeout, "lifecycle": lifecycle})
        if real_runner is not None:
            return real_runner(cmd, timeout)
        if queue is None:
            return runs(cmd)
        if not queue:
            raise AssertionError(f"unexpected extra child process: {cmd}")
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        # a successful run leaves a non-empty file behind, as the real binary would
        if write_output and result.returncode == 0:
            out = cmd[-1]
            with open(out, "wb") as handle:
                handle.write(b"x" * 1024)
        return result

    def get_video_duration(path):
        calls["duration"].append(path)
        return duration

    def get_video_fps(path):
        calls["fps"].append(path)
        return source_fps

    namespace = {
        "os": os,
        "subprocess": subprocess,
        "List": List,
        "Tuple": tuple,
        "MAX_THREADS": 8,
        "FFMPEG_PATH": ffmpeg,
        "FFPROBE_PATH": ffprobe,
        "_run_media_command": _run_media_command,
        "get_video_duration": get_video_duration,
        "get_video_fps": get_video_fps,
        "get_nvenc_quality_args": lambda *_a, **_k: ["-c:v", "h264_nvenc"],
        "fork_diagnostics": types.SimpleNamespace(
            summarize_ffmpeg_failure=lambda stderr: f"summary:{str(stderr)[:40]}",
            describe_output_problem=lambda exists, size: f"output exists={exists} size={size}",
            describe_exception=lambda exc: f"exception:{exc}"),
        "fork_frame_lock": fork_frame_lock,
        "RenderCancelled": fork_render_worker.RenderCancelled,
    }
    if probe_queue is not None:
        def _probe_output_frame_count(path):
            calls["probe"].append(path)
            if not probe_queue:
                return None
            return probe_queue.pop(0)
        namespace["_probe_output_frame_count"] = _probe_output_frame_count
        nodes = [n for n in nodes if n.name != "_probe_output_frame_count"]

    exec(compile(ast.Module(body=nodes, type_ignores=[]), _FFMPEG_SRC, "exec"), namespace)
    if probe_queue is None:
        original = namespace["_probe_output_frame_count"]

        def _counting(path):
            calls["probe"].append(path)
            return original(path)
        namespace["_probe_output_frame_count"] = _counting
    return namespace["extract_clip_segment_ffmpeg_detailed"], calls


def _extract_kwargs(tmp_path, start=28.84, dur=1.16, fps=25.0, **over):
    kwargs = dict(video_file="J:/lib/src_b_1080p.mp4", start_time=start, duration=dur,
                  output_file=str(tmp_path / "clip.mp4"), fps=fps, target_size=(1280, 720),
                  use_nvenc=False, gpu_encoder="none", geometry="", lifecycle=None)
    kwargs.update(over)
    return kwargs


def _seeks(calls):
    out = []
    for entry in calls["media"]:
        cmd = entry["cmd"]
        if "-ss" in cmd:
            out.append(float(cmd[cmd.index("-ss") + 1]))
    return out


def test_an_exact_first_attempt_costs_one_child_and_no_probe(tmp_path):
    """The happy path must stay exactly as cheap as it was. This is the compatibility claim."""
    extract, calls = _load_extractor([_Result(0, _block(29))], probe_counts=[])
    assert extract(**_extract_kwargs(tmp_path)) == (True, "")
    assert len(calls["media"]) == 1, "one FFmpeg run"
    assert calls["probe"] == [], "no confirmation probe on an exact count"
    assert calls["duration"] == [] and calls["fps"] == [], "no recovery probing either"
    assert _seeks(calls) == [28.84], "the first attempt seeks exactly where it was told to"


def test_the_first_attempt_argv_carries_progress_and_the_unchanged_seek(tmp_path):
    extract, calls = _load_extractor([_Result(0, _block(29))], probe_counts=[])
    extract(**_extract_kwargs(tmp_path))
    cmd = calls["media"][0]["cmd"]
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"
    assert cmd.index("-ss") < cmd.index("-i"), "input-side accurate seek, unchanged"
    assert cmd[cmd.index("-vframes") + 1] == "29"
    assert cmd[cmd.index("-t") + 1] == str(29 / 25.0)
    assert calls["media"][0]["timeout"] == 120


def test_a_short_first_attempt_is_confirmed_then_re_seeked_and_succeeds(tmp_path):
    """The reported defect, end to end over the shipped body."""
    extract, calls = _load_extractor(
        [_Result(0, _block(28)), _Result(0, _block(29))], probe_counts=[28])
    assert extract(**_extract_kwargs(tmp_path)) == (True, "")
    assert len(calls["media"]) == 2, "exactly one corrected retry"
    assert len(calls["probe"]) == 1, "the shortfall is confirmed once, not twice"
    seeks = _seeks(calls)
    assert seeks[0] == 28.84
    assert seeks[1] == pytest.approx(28.84 - 1.0 / 30.0, abs=1e-9)
    assert seeks[1] < seeks[0]


def test_the_retry_reuses_every_other_argument_unchanged(tmp_path):
    extract, calls = _load_extractor(
        [_Result(0, _block(28)), _Result(0, _block(29))], probe_counts=[28])
    extract(**_extract_kwargs(tmp_path))
    first, second = (e["cmd"] for e in calls["media"])
    ss = first.index("-ss")
    assert first[:ss] == second[:ss], "nothing before the seek changed"
    assert first[ss + 2:] == second[ss + 2:], "only the seek value differs"


def test_a_shortfall_that_survives_the_retry_fails_clearly(tmp_path):
    extract, calls = _load_extractor(
        [_Result(0, _block(28)), _Result(0, _block(28))], probe_counts=[28, 28])
    ok, reason = extract(**_extract_kwargs(tmp_path))
    assert ok is False
    assert "SHORT" in reason and "28" in reason and "29" in reason
    assert len(calls["media"]) == 2, "bounded at two attempts, never a third"


def test_a_source_too_short_to_recover_fails_without_spending_a_retry(tmp_path):
    """`recovery_seek` answers None, and None means report -- not try the same window again."""
    extract, calls = _load_extractor([_Result(0, _block(10))], probe_counts=[10],
                                     duration=0.5)
    ok, reason = extract(**_extract_kwargs(tmp_path))
    assert ok is False and "SHORT" in reason
    assert len(calls["media"]) == 1, "no pointless second extraction"


def test_a_long_segment_is_refused_without_a_retry(tmp_path):
    """A re-seek cannot address a long segment, so spending one would be theatre."""
    extract, calls = _load_extractor([_Result(0, _block(31))], probe_counts=[31])
    ok, reason = extract(**_extract_kwargs(tmp_path))
    assert ok is False and "LONG" in reason
    assert len(calls["media"]) == 1
    assert calls["fps"] == [], "no recovery arithmetic was even attempted"


def test_a_progress_stream_the_build_did_not_emit_falls_back_to_the_container(tmp_path):
    extract, calls = _load_extractor([_Result(0, "")], probe_counts=[29])
    assert extract(**_extract_kwargs(tmp_path)) == (True, "")
    assert len(calls["probe"]) == 1, "the file itself answered"
    assert len(calls["media"]) == 1


def test_an_unmeasurable_segment_keeps_the_historical_behaviour(tmp_path):
    """Neither mechanism produced a number. Refusing on a missing measurement would be worse than
    the defect: it would fail renders that are fine."""
    extract, calls = _load_extractor([_Result(0, "")], probe_counts=[None])
    assert extract(**_extract_kwargs(tmp_path)) == (True, "")
    assert len(calls["media"]) == 1
    assert calls["fps"] == [], "no recovery attempted on an absence of evidence"


def test_a_progress_count_that_disagrees_with_the_file_defers_to_the_file(tmp_path):
    """Two readings of one artifact: the container is a reading of what was actually written."""
    extract, calls = _load_extractor([_Result(0, _block(28))], probe_counts=[29])
    assert extract(**_extract_kwargs(tmp_path)) == (True, "")
    assert len(calls["media"]) == 1, "no retry: the clip is fine"


def test_a_non_zero_exit_is_the_unchanged_failure_path(tmp_path):
    extract, calls = _load_extractor([_Result(1, "", "Conversion failed!")])
    ok, reason = extract(**_extract_kwargs(tmp_path))
    assert ok is False and reason.startswith("summary:")
    assert calls["probe"] == [], "a failed run is not measured for frames"
    assert len(calls["media"]) == 1


def test_a_missing_output_is_the_unchanged_failure_path(tmp_path):
    extract, calls = _load_extractor([_Result(0, _block(29))], write_output=False)
    ok, reason = extract(**_extract_kwargs(tmp_path))
    assert ok is False and "exists=False" in reason
    assert len(calls["media"]) == 1


def test_a_cancellation_during_the_first_attempt_escapes_unchanged(tmp_path):
    extract, calls = _load_extractor([fork_render_worker.RenderCancelled("stop")])
    with pytest.raises(fork_render_worker.RenderCancelled):
        extract(**_extract_kwargs(tmp_path))
    assert len(calls["media"]) == 1


def test_a_cancellation_during_the_retry_escapes_unchanged(tmp_path):
    """The retry is inside the same `try`, so the typed handler must still re-raise from it."""
    extract, calls = _load_extractor(
        [_Result(0, _block(28)), fork_render_worker.RenderCancelled("stop")],
        probe_counts=[28])
    with pytest.raises(fork_render_worker.RenderCancelled):
        extract(**_extract_kwargs(tmp_path))
    assert len(calls["media"]) == 2


def test_the_lifecycle_reaches_every_attempt(tmp_path):
    life = object()
    extract, calls = _load_extractor(
        [_Result(0, _block(28)), _Result(0, _block(29))], probe_counts=[28])
    extract(**_extract_kwargs(tmp_path, lifecycle=life))
    assert [e["lifecycle"] for e in calls["media"]] == [life, life], \
        "a cancellable render must stay cancellable through the corrected attempt"


def test_consecutive_segments_do_not_influence_each_other(tmp_path):
    """Each call resolves its own seek from its own argument; nothing is carried over."""
    for index, start in enumerate((5.0, 28.84, 10.0)):
        extract, calls = _load_extractor(
            [_Result(0, _block(28)), _Result(0, _block(29))], probe_counts=[28])
        extract(**_extract_kwargs(tmp_path, start=start,
                                  output_file=str(tmp_path / f"c{index}.mp4")))
        assert _seeks(calls)[0] == start


# --- the worker's too-short-source guard, over the SHIPPED worker body -----

def _load_worker(extract, duration=30.0):
    node = _func(_tree(_PROCESSOR_SRC), "create_clip_parallel")
    calls = {"duration": [], "extract": []}

    def get_video_duration(path):
        calls["duration"].append(path)
        return duration

    def extract_clip_segment_ffmpeg_detailed(**kwargs):
        calls["extract"].append(kwargs)
        return extract(**kwargs)

    namespace = {
        "time": __import__("time"), "os": os, "random": __import__("random"),
        "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="cafe")),
        "get_video_duration": get_video_duration,
        "extract_clip_segment_ffmpeg_detailed": extract_clip_segment_ffmpeg_detailed,
        "RenderCancelled": fork_render_worker.RenderCancelled,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _PROCESSOR_SRC, "exec"), namespace)
    return namespace["create_clip_parallel"], calls


def _worker_args(final_duration=1.16, fps=25.0, planned=None):
    args = [3, "J:/lib/src.mp4", final_duration, (1280, 720), False, "none", "C:/tmp", fps]
    if planned is not None:
        args.extend([planned, None, ""])
    return tuple(args)


def test_a_source_long_enough_for_its_slot_is_extracted_normally():
    worker, calls = _load_worker(lambda **_k: (True, ""), duration=30.0)
    planned = {"video_file": "J:/lib/src.mp4", "start_time": 28.84, "source_duration": 1.16}
    result = worker(_worker_args(planned=planned))
    assert result[4] is None, result[4]
    assert len(calls["extract"]) == 1


def test_a_source_shorter_than_its_planned_segment_is_refused_not_truncated():
    """The clamp to the real duration shrank the SEGMENT, not just the read. That shortfall is
    exactly what the frame-locked timeline cannot absorb."""
    worker, calls = _load_worker(lambda **_k: (True, ""), duration=0.52)
    planned = {"video_file": "J:/lib/short.mp4", "start_time": 0.0, "source_duration": 1.16}
    result = worker(_worker_args(planned=planned))
    assert result[1] is None, "it must be a failure tuple"
    detail = result[4]
    assert "too short for its planned segment" in detail
    assert "29" in detail and "13" in detail
    assert "0.520s" in detail
    assert calls["extract"] == [], "nothing was extracted, so nothing can reach assembly"


def test_the_refusal_names_the_source_through_the_existing_describer():
    """S1's context, reused rather than re-invented -- the operator needs the path to act."""
    worker, _ = _load_worker(lambda **_k: (True, ""), duration=0.52)
    planned = {"video_file": r"J:\New folder\Cuts\short.mp4", "start_time": 0.0,
               "source_duration": 1.16}
    detail = worker(_worker_args(planned=planned))[4]
    assert r"J:\New folder\Cuts\short.mp4" in detail


def test_the_unplanned_fallback_is_guarded_too():
    """The random-sample branch clamps the same way, so it can truncate the same way."""
    worker, calls = _load_worker(lambda **_k: (True, ""), duration=0.52)
    result = worker(_worker_args())
    assert "too short for its planned segment" in result[4]
    assert calls["extract"] == []


def test_a_sub_frame_rounding_difference_is_not_treated_as_truncation():
    """Both numbers are quantised to output frames before comparison, so float noise in
    `frames / fps` cannot invent a refusal."""
    worker, calls = _load_worker(lambda **_k: (True, ""), duration=1.16)
    planned = {"video_file": "J:/lib/exact.mp4", "start_time": 0.0,
               "source_duration": 1.1599999999}
    result = worker(_worker_args(final_duration=1.1600000001, planned=planned))
    assert result[4] is None, result[4]
    assert len(calls["extract"]) == 1


# ===========================================================================
# 4. Real FFmpeg -- opt-in. The defect is a property of the binary.
# ===========================================================================

def _sibling_ffprobe(ffmpeg_path: str) -> str:
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
    not (FFMPEG_BIN and FFPROBE_BIN and os.path.isfile(str(FFPROBE_BIN))),
    reason="no FFmpeg available (set BEATSYNC_TEST_FFMPEG to the portable ffmpeg.exe)")


def _real_runner(cmd, timeout=None):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout or 180, check=False)


def _synthesise(path, seconds, fps, size="320x240", vsync="cfr"):
    """A deterministic CFR source of an exact length, so frame counts are arithmetic."""
    cmd = [FFMPEG_BIN, "-v", "error", "-nostdin",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
           "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
           "-pix_fmt", "yuv420p", "-fps_mode", vsync, "-r", str(fps), "-y", str(path)]
    subprocess.run(cmd, capture_output=True, check=True, timeout=180)
    return str(path)


def _decoded_frames(path):
    """A FULL decode count -- the strongest available reading, used only in tests."""
    out = subprocess.run([FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                          "-count_frames", "-show_entries", "stream=nb_read_frames",
                          "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=False, timeout=180)
    return int(out.stdout.strip())


@pytest.fixture(scope="module")
def real_extractor():
    extract, _calls = _load_extractor([], real_runner=_real_runner,
                                      ffmpeg=FFMPEG_BIN, ffprobe=FFPROBE_BIN)
    return extract


@pytest.fixture(scope="module")
def source_30fps(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fl30")
    return _synthesise(folder / "src30.mp4", 30.0, 30)


@needs_ffmpeg
@pytest.mark.parametrize("fps,want", [(25.0, 29), (30.0, 35)])
def test_the_exact_reported_eof_case_now_produces_every_requested_frame(
        real_extractor, source_30fps, tmp_path, fps, want):
    """start=28.84 for 1.16 s against a 30.000000 s / 30 fps source.

    Before this fix: 28 of 29 at 25 fps, 34 of 35 at 30 fps -- identical on pristine `main`.
    """
    out = tmp_path / f"eof_{int(fps)}.mp4"
    ok, reason = real_extractor(
        video_file=source_30fps, start_time=28.84, duration=1.16, output_file=str(out),
        fps=fps, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
        geometry="", lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == want


@needs_ffmpeg
@pytest.mark.parametrize("start", [0.0, 5.0, 10.0, 27.84])
def test_a_segment_ending_well_before_eof_is_exact_and_unchanged(
        real_extractor, source_30fps, tmp_path, start):
    """These were already correct and must stay correct from the same first-attempt seek."""
    out = tmp_path / f"mid_{start}.mp4"
    ok, reason = real_extractor(
        video_file=source_30fps, start_time=start, duration=1.16, output_file=str(out),
        fps=25.0, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
        geometry="", lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == 29


@needs_ffmpeg
def test_a_segment_whose_window_ends_exactly_at_eof_is_exact(
        real_extractor, source_30fps, tmp_path):
    """The boundary case by construction: `duration - needed` to the microsecond."""
    needed = 29 / 25.0
    out = tmp_path / "flush.mp4"
    ok, reason = real_extractor(
        video_file=source_30fps, start_time=30.0 - needed, duration=needed,
        output_file=str(out), fps=25.0, target_size=(320, 240), use_nvenc=False,
        gpu_encoder="none", geometry="", lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == 29


@needs_ffmpeg
def test_a_source_shorter_than_the_requested_segment_fails_clearly(real_extractor, tmp_path):
    """No seek can recover this, so it must be reported rather than quietly shortened."""
    short = _synthesise(tmp_path / "short.mp4", 0.5, 30)
    out = tmp_path / "impossible.mp4"
    ok, reason = real_extractor(
        video_file=short, start_time=0.0, duration=1.16, output_file=str(out),
        fps=25.0, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
        geometry="", lifecycle=None)
    assert ok is False
    assert "SHORT" in reason and "29" in reason


@needs_ffmpeg
@pytest.mark.parametrize("src_fps,out_fps,want", [(24, 25.0, 29), (50, 25.0, 29),
                                                  (24, 30.0, 35), (60, 25.0, 29)])
def test_the_eof_case_is_exact_across_source_output_rate_pairs(
        real_extractor, tmp_path, src_fps, out_fps, want):
    """The lead-in margin is one SOURCE frame, which is what makes a coarse source work."""
    src = _synthesise(tmp_path / f"s{src_fps}.mp4", 30.0, src_fps)
    out = tmp_path / f"p{src_fps}_{int(out_fps)}.mp4"
    ok, reason = real_extractor(
        video_file=src, start_time=28.84, duration=1.16, output_file=str(out),
        fps=out_fps, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
        geometry="", lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == want


@needs_ffmpeg
def test_a_non_integer_source_rate_at_eof_is_exact(real_extractor, tmp_path):
    """29.97 (30000/1001): the rate where frame boundaries never land on round seconds."""
    src = _synthesise(tmp_path / "ntsc.mp4", 30.0, "30000/1001")
    out = tmp_path / "ntsc_out.mp4"
    ok, reason = real_extractor(
        video_file=src, start_time=28.84, duration=1.16, output_file=str(out),
        fps=25.0, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
        geometry="", lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == 29


@needs_ffmpeg
def test_a_true_vfr_source_at_eof(real_extractor, tmp_path):
    """Genuinely variable intervals, not a re-stamped CFR stream.

    Worth its own case because `get_video_fps` reads `r_frame_rate`, which for VFR is the **base /
    maximum** rate -- so the one-source-frame margin is computed from a rate the file does not
    sustain, and is not *guaranteed* to cover the real gap before the last frame. It is exact here
    (measured: 29/29 at 25 fps, 35/35 at 30 fps on a 513-frame source averaging ~17.2 fps with
    gaps up to 4x the minimum). What the contract guarantees in general is the weaker and more
    important property this asserts: the segment is either complete or **refused** -- never short
    and reported as success.
    """
    src = tmp_path / "vfr.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-v", "error", "-nostdin", "-f", "lavfi",
         "-i", "testsrc=size=320x240:rate=30", "-t", "30",
         "-vf", "select='gte(mod(n\\,7),3)'", "-fps_mode", "passthrough",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
         "-y", str(src)], capture_output=True, check=True, timeout=180)
    declared = subprocess.run(
        [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(src)], capture_output=True, text=True, check=False, timeout=60).stdout.strip()
    duration = float(declared)

    for out_fps, want in ((25.0, 29), (30.0, 35)):
        out = tmp_path / f"vfr_{int(out_fps)}.mp4"
        ok, reason = real_extractor(
            video_file=str(src), start_time=duration - 1.16, duration=1.16,
            output_file=str(out), fps=out_fps, target_size=(320, 240), use_nvenc=False,
            gpu_encoder="none", geometry="", lifecycle=None)
        if ok:
            assert _decoded_frames(out) == want, "a successful segment must be complete"
        else:
            assert "SHORT" in reason, reason


@needs_ffmpeg
@pytest.mark.parametrize("profile,geometry,fps,want", [
    ("720p25", "scale=1280:720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:-1:-1:color=black,setsar=1", 25.0, 29),
    ("1080p30", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:-1:-1:color=black,setsar=1", 30.0, 35),
])
def test_the_fixed_output_profiles_are_exact_at_eof(
        real_extractor, source_30fps, tmp_path, profile, geometry, fps, want):
    """The profile chain replaces the bare scale and must not interact with the correction."""
    out = tmp_path / f"{profile}.mp4"
    ok, reason = real_extractor(
        video_file=source_30fps, start_time=28.84, duration=1.16, output_file=str(out),
        fps=fps, target_size=(1280, 720), use_nvenc=False, gpu_encoder="none",
        geometry=geometry, lifecycle=None)
    assert ok, reason
    assert _decoded_frames(out) == want


@needs_ffmpeg
def test_consecutive_segments_each_get_their_full_frame_count(
        real_extractor, source_30fps, tmp_path):
    """Four back-to-back segments, the last flush against EOF. The total is what the timeline
    expects, which is the property a one-frame loss broke."""
    needed = 29 / 25.0
    starts = [28.84 - 3 * needed, 28.84 - 2 * needed, 28.84 - needed, 28.84]
    total = 0
    for index, start in enumerate(starts):
        out = tmp_path / f"seq{index}.mp4"
        ok, reason = real_extractor(
            video_file=source_30fps, start_time=start, duration=1.16, output_file=str(out),
            fps=25.0, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
            geometry="", lifecycle=None)
        assert ok, f"segment {index}: {reason}"
        count = _decoded_frames(out)
        assert count == 29, f"segment {index} produced {count}"
        total += count
    assert total == 4 * 29


@needs_ffmpeg
def test_the_container_header_and_a_full_decode_agree_on_every_segment(
        source_30fps, tmp_path):
    """The justification for not decoding on the render path. Re-measured, not assumed."""
    extract, calls = _load_extractor([], real_runner=_real_runner,
                                     ffmpeg=FFMPEG_BIN, ffprobe=FFPROBE_BIN)
    for start, fps in ((28.84, 25.0), (28.84, 30.0), (10.0, 25.0), (0.0, 30.0)):
        out = tmp_path / f"agree_{start}_{fps}.mp4"
        ok, reason = extract(
            video_file=source_30fps, start_time=start, duration=1.16, output_file=str(out),
            fps=fps, target_size=(320, 240), use_nvenc=False, gpu_encoder="none",
            geometry="", lifecycle=None)
        assert ok, reason
        counted = _decoded_frames(out)
        declared = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True, check=False, timeout=60).stdout.strip()
        assert int(declared) == counted, f"{declared} declared vs {counted} decoded at {start}"


@needs_ffmpeg
def test_cancellation_during_a_real_extraction_still_propagates(source_30fps, tmp_path):
    """The correction must not have made the extraction uncancellable: a lifecycle that reports a
    request mid-run still terminates and reaps the child before `RenderCancelled` escapes."""
    nodes = [_func(_tree(_FFMPEG_SRC), name) for name in
             ("seconds_to_frame_count", "frame_count_to_seconds", "get_cpu_h264_quality_args",
              "_probe_output_frame_count", "_run_media_command",
              "extract_clip_segment_ffmpeg_detailed")]
    namespace = {
        "os": os, "subprocess": subprocess, "time": __import__("time"), "List": List,
        "Tuple": tuple, "MAX_THREADS": 8,
        "FFMPEG_PATH": FFMPEG_BIN, "FFPROBE_PATH": FFPROBE_BIN,
        "_CANCEL_POLL_SECONDS": 0.01, "_TERMINATE_GRACE_SECONDS": 5.0,
        "get_video_duration": lambda _p: 30.0, "get_video_fps": lambda _p: 30.0,
        "get_nvenc_quality_args": lambda *_a, **_k: ["-c:v", "h264_nvenc"],
        "fork_diagnostics": types.SimpleNamespace(
            summarize_ffmpeg_failure=lambda s: "summary",
            describe_output_problem=lambda e, s: "output problem",
            describe_exception=lambda e: f"exception:{e}"),
        "fork_frame_lock": fork_frame_lock,
        "RenderCancelled": fork_render_worker.RenderCancelled,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _FFMPEG_SRC, "exec"), namespace)

    life = fork_render_worker.RenderLifecycle("inv-frame-lock")
    life.request_cancel()
    with pytest.raises(fork_render_worker.RenderCancelled):
        namespace["extract_clip_segment_ffmpeg_detailed"](
            video_file=source_30fps, start_time=0.0, duration=20.0,
            output_file=str(tmp_path / "cancelled.mp4"), fps=25.0, target_size=(1920, 1080),
            use_nvenc=False, gpu_encoder="none", geometry="", lifecycle=life)


@needs_ffmpeg
def test_the_source_file_is_never_modified(real_extractor, source_30fps, tmp_path):
    before = (os.path.getsize(source_30fps), os.stat(source_30fps).st_mtime_ns)
    ok, reason = real_extractor(
        video_file=source_30fps, start_time=28.84, duration=1.16,
        output_file=str(tmp_path / "ro.mp4"), fps=25.0, target_size=(320, 240),
        use_nvenc=False, gpu_encoder="none", geometry="", lifecycle=None)
    assert ok, reason
    assert (os.path.getsize(source_30fps), os.stat(source_30fps).st_mtime_ns) == before
