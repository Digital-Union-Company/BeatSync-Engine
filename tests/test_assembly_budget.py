"""FUN R11: final assembly must not die on a constant that ignores the workload.

The incident. A 1h59m render finished extracting 2,187 segments (14,865,314,550 bytes) and
was then killed by ``subprocess.TimeoutExpired`` at exactly 300 s during final assembly,
having already written **16,922,008,592** of ~16,926,722,550 expected bytes. Forensics on
the abandoned file: ``ftyp`` then a 64-bit ``mdat`` whose declared size plus the 32-byte
``ftyp`` equals the file length exactly, and **no ``moov``**. So the mux had written every
byte and patched the box size, and only the index was missing -- the render died ~99.97 %
of the way through, and two hours of extraction were discarded.

``assembly_budget`` is stdlib-only and pure, so the arithmetic is tested directly here on a
bare interpreter (CLAUDE.md's hard rule for ``src/beatsync_fork/``).

``ffmpeg_processing`` needs the portable runtime and cannot be imported, so -- following
``tests/test_extraction_failure_context.py`` and ``tests/test_render_cancellation.py`` --
the behavioural tests compile and execute the **real** ``concatenate_videos_ffmpeg`` body
against a hand-built namespace. They assert what the shipped code does, not what a mock
returns.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import time
import types

import pytest

from beatsync_fork import assembly_budget as budget

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FFMPEG_SRC = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")

# The incident's measured numbers, used as the regression anchor.
INCIDENT_SEGMENT_BYTES = 14_865_314_550
INCIDENT_TIMELINE_SECONDS = 7157.666666666667
INCIDENT_PARTIAL_BYTES = 16_922_008_592
HISTORICAL_TIMEOUT = 300


# ===========================================================================
# 1. THE PURE BUDGET
# ===========================================================================

def test_the_incident_workload_now_gets_more_than_the_300s_that_killed_it():
    """The regression that matters: this exact workload must not be bounded at 300 s."""
    seconds = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES,
        audio_seconds=INCIDENT_TIMELINE_SECONDS,
        faststart=True,
    )
    assert seconds > HISTORICAL_TIMEOUT
    # And by a wide margin, not by one second.
    assert seconds > 10 * HISTORICAL_TIMEOUT


def test_the_incident_budget_covers_the_bytes_that_were_actually_written():
    """A budget that could not have finished the observed work would be no fix at all.

    The mux demonstrably moved ~31.8 GB of combined read+write in 300 s. At the
    conservative floor the budget must allow at least the traversals the argv performs.
    """
    seconds = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES,
        audio_seconds=INCIDENT_TIMELINE_SECONDS,
        faststart=True,
    )
    output_bytes = INCIDENT_SEGMENT_BYTES + budget.pcm_audio_bytes(
        INCIDENT_TIMELINE_SECONDS)
    io_bytes = INCIDENT_SEGMENT_BYTES + output_bytes + 2 * output_bytes
    implied = io_bytes / budget.ASSEMBLY_THROUGHPUT_FLOOR_BYTES_PER_SECOND
    assert seconds >= implied


def test_short_renders_keep_exactly_the_historical_prompt_failure():
    """A small render must still fail in 300 s. The floor IS the old constant."""
    assert budget.assembly_timeout_seconds(
        50 * 1024 * 1024, audio_seconds=60.0, faststart=True) == HISTORICAL_TIMEOUT
    assert budget.assembly_timeout_seconds(0, audio_seconds=None) == HISTORICAL_TIMEOUT
    assert budget.MIN_ASSEMBLY_TIMEOUT_SECONDS == HISTORICAL_TIMEOUT


def test_the_bound_is_finite_and_never_unbounded():
    """A stalled FFmpeg must always die. No input may produce None, 0 or infinity."""
    for payload in (0, 1, 10 ** 9, 10 ** 15, 10 ** 18):
        for seconds in (None, 0.0, 1.0, 10 ** 6, 10 ** 9):
            value = budget.assembly_timeout_seconds(
                payload, audio_seconds=seconds, faststart=True,
                reencode_seconds=seconds)
            assert isinstance(value, int)
            assert budget.MIN_ASSEMBLY_TIMEOUT_SECONDS <= value
            assert value <= budget.MAX_ASSEMBLY_TIMEOUT_SECONDS


def test_faststart_costs_strictly_more_than_no_faststart():
    """`+faststart` re-reads and rewrites the output. That pass is what the render died
    in, so it must be budgeted rather than absorbed."""
    without = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES, audio_seconds=INCIDENT_TIMELINE_SECONDS,
        faststart=False)
    with_fs = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES, audio_seconds=INCIDENT_TIMELINE_SECONDS,
        faststart=True)
    assert with_fs > without


def test_a_reencode_costs_strictly_more_than_a_stream_copy():
    copy = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES, audio_seconds=INCIDENT_TIMELINE_SECONDS)
    reencode = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES, audio_seconds=INCIDENT_TIMELINE_SECONDS,
        reencode_seconds=INCIDENT_TIMELINE_SECONDS)
    assert reencode > copy


def test_the_budget_grows_monotonically_with_measured_workload():
    previous = 0
    for gigabytes in (0, 1, 4, 16, 64):
        value = budget.assembly_timeout_seconds(
            gigabytes * 1024 ** 3, audio_seconds=600.0, faststart=True)
        assert value >= previous
        previous = value


def test_pcm_audio_bytes_matches_the_declared_audio_contract():
    """`-c:a pcm_s24le -ar 48000` stereo: 48000 * 3 * 2 bytes per second."""
    assert budget.pcm_audio_bytes(1.0) == 48_000 * 3 * 2
    assert budget.pcm_audio_bytes(INCIDENT_TIMELINE_SECONDS) == 2_061_408_000


@pytest.mark.parametrize("junk", [None, "", "abc", float("nan"), float("inf"), -5.0, 0.0,
                                  object()])
def test_a_budget_helper_never_raises_on_junk(junk):
    """A timeout helper must never be the reason a render fails to start."""
    assert budget.pcm_audio_bytes(junk) == 0
    value = budget.assembly_timeout_seconds(0, audio_seconds=junk, faststart=True)
    assert value == budget.MIN_ASSEMBLY_TIMEOUT_SECONDS


def test_measure_input_bytes_sums_real_files_and_skips_unstatable(tmp_path):
    a = tmp_path / "a.mp4"
    a.write_bytes(b"x" * 1000)
    b = tmp_path / "b.mp4"
    b.write_bytes(b"y" * 2345)
    assert budget.measure_input_bytes([str(a), str(b)]) == 3345
    assert budget.measure_input_bytes(
        [str(a), str(tmp_path / "missing.mp4"), None]) == 1000
    assert budget.measure_input_bytes(None) == 0
    assert budget.measure_input_bytes([]) == 0


def test_the_timeout_diagnostic_names_the_workload_it_measured():
    """A timeout that names no workload is how the 300 s constant survived."""
    seconds = budget.assembly_timeout_seconds(
        INCIDENT_SEGMENT_BYTES, audio_seconds=INCIDENT_TIMELINE_SECONDS,
        faststart=True)
    text = budget.describe_assembly_timeout(
        seconds, input_bytes=INCIDENT_SEGMENT_BYTES,
        audio_seconds=INCIDENT_TIMELINE_SECONDS, faststart=True)
    assert f"{seconds}s budget" in text
    assert "GiB of segments" in text
    assert "pcm_s24le" in text
    assert "faststart" in text
    assert "throughput floor" in text


def test_the_diagnostic_says_so_when_the_ceiling_clamped():
    seconds = budget.assembly_timeout_seconds(
        10 ** 15, audio_seconds=10 ** 6, faststart=True, reencode_seconds=10 ** 6)
    assert seconds == budget.MAX_ASSEMBLY_TIMEOUT_SECONDS
    assert "CLAMPED AT CEILING" in budget.describe_assembly_timeout(
        seconds, input_bytes=10 ** 15, audio_seconds=10 ** 6, faststart=True)


# ===========================================================================
# 2. THE REAL FUNCTION BODY
# ===========================================================================

def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


class _Cancelled(Exception):
    """Stands in for the real ``RenderCancelled``, also an ordinary ``Exception``."""


def _load_concat(runner):
    """The REAL ``concatenate_videos_ffmpeg`` over a stubbed media runner."""
    node = _func(_tree(_FFMPEG_SRC), "concatenate_videos_ffmpeg")
    calls = []

    def _run_media_command(cmd, timeout, lifecycle=None):
        calls.append({"cmd": list(cmd), "timeout": timeout, "lifecycle": lifecycle})
        return runner(list(cmd), timeout, lifecycle)

    namespace = {
        "os": os,
        "time": time,
        "uuid": types.SimpleNamespace(
            uuid4=lambda: types.SimpleNamespace(hex="feed")),
        "shutil": types.SimpleNamespace(move=lambda a, b: None),
        "subprocess": subprocess,
        "FFMPEG_PATH": r"C:\ffmpeg\ffmpeg.exe",
        "_run_media_command": _run_media_command,
        "_safe_remove_file": lambda p: None,
        "_short_ffmpeg_error": lambda s, n=2200: (s or "")[:n],
        "_fmt_seconds": lambda s: f"{s:.1f}s",
        "_env_flag": lambda name, default=True: default,
        "get_nvenc_quality_args": lambda enc, include_pix_fmt=False: ["-c:v", enc],
        "get_cpu_h264_quality_args": lambda include_pix_fmt=False: ["-c:v", "libx264"],
        "fork_assembly_budget": budget,
        "RenderCancelled": _Cancelled,
        "List": list,
        "print": lambda *a, **k: None,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _FFMPEG_SRC, "exec"),
         namespace)
    return namespace["concatenate_videos_ffmpeg"], calls


def _ok(cmd, timeout, lifecycle):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _fake_segments(tmp_path, count, each_bytes):
    paths = []
    for i in range(count):
        p = tmp_path / f"seg{i}.mp4"
        p.write_bytes(b"\0" * each_bytes)
        paths.append(str(p))
    return paths


def test_the_fast_concat_site_no_longer_passes_300(tmp_path):
    """The incident site must receive a workload-derived budget, not a constant."""
    segs = _fake_segments(tmp_path, 8, 400_000)
    out = tmp_path / "out.mp4"
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\0" * 2048)

    produced = None

    def runner(cmd, timeout, lifecycle):
        nonlocal produced
        out.write_bytes(b"x" * 10)
        produced = timeout
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fn, calls = _load_concat(runner)
    fn(segs, str(out), audio_file=str(audio), fps=30.0,
       temp_dir=str(tmp_path), timeline_seconds=7157.666666666667)

    assert len(calls) == 1
    cmd = calls[0]["cmd"]
    assert "+faststart" in cmd                     # this is the faststart site
    assert calls[0]["timeout"] > HISTORICAL_TIMEOUT


def test_the_fast_concat_budget_is_the_faststart_budget_for_the_real_workload(tmp_path):
    """Not merely >300: it must equal the budget derived WITH the faststart pass."""
    segs = _fake_segments(tmp_path, 4, 1_000_000)
    out = tmp_path / "out.mp4"
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\0" * 1024)

    def runner(cmd, timeout, lifecycle):
        out.write_bytes(b"x" * 10)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fn, calls = _load_concat(runner)
    fn(segs, str(out), audio_file=str(audio), fps=30.0, temp_dir=str(tmp_path),
       timeline_seconds=9000.0)

    expected = budget.assembly_timeout_seconds(
        4_000_000, audio_seconds=9000.0, faststart=True)
    assert calls[0]["timeout"] == expected


def test_a_genuine_timeout_propagates_and_never_silently_re_encodes(tmp_path):
    """A timeout must NOT fall through to a full re-encode of ~17 GB.

    The fallback is gated on ``returncode != 0``; a timeout raises instead of returning,
    so exactly ONE child is ever started.
    """
    segs = _fake_segments(tmp_path, 4, 500_000)
    out = tmp_path / "out.mp4"
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\0" * 1024)

    def runner(cmd, timeout, lifecycle):
        raise subprocess.TimeoutExpired(cmd, timeout)

    fn, calls = _load_concat(runner)
    with pytest.raises(subprocess.TimeoutExpired):
        fn(segs, str(out), audio_file=str(audio), fps=30.0, temp_dir=str(tmp_path),
           timeline_seconds=7157.666666666667)
    assert len(calls) == 1, "a timeout must not start a second, re-encoding child"


def test_cancellation_still_propagates_from_final_assembly(tmp_path):
    """RenderCancelled is an ordinary Exception; nothing here may swallow it."""
    segs = _fake_segments(tmp_path, 3, 100_000)
    out = tmp_path / "out.mp4"

    def runner(cmd, timeout, lifecycle):
        raise _Cancelled("cancelled")

    fn, calls = _load_concat(runner)
    with pytest.raises(_Cancelled):
        fn(segs, str(out), audio_file=None, fps=30.0, temp_dir=str(tmp_path),
           timeline_seconds=600.0)
    assert len(calls) == 1


def test_an_empty_output_is_never_published_as_a_successful_render(tmp_path):
    """rc=0 with a zero-byte file must not be accepted; it falls back to re-encode."""
    segs = _fake_segments(tmp_path, 3, 100_000)
    out = tmp_path / "out.mp4"
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\0" * 512)

    def runner(cmd, timeout, lifecycle):
        if "+faststart" in cmd:
            out.write_bytes(b"")           # rc=0 but nothing written
            return subprocess.CompletedProcess(cmd, 0, "", "")
        out.write_bytes(b"x" * 100)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fn, calls = _load_concat(runner)
    fn(segs, str(out), audio_file=str(audio), fps=30.0, temp_dir=str(tmp_path),
       timeline_seconds=600.0)
    assert len(calls) == 2, "an empty output must not be returned as success"


def test_the_audio_and_timeline_contract_is_unchanged(tmp_path):
    """`-shortest`, the planned `-t`, pcm_s24le@48k and `+faststart` all survive."""
    segs = _fake_segments(tmp_path, 3, 100_000)
    out = tmp_path / "out.mp4"
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\0" * 512)

    def runner(cmd, timeout, lifecycle):
        out.write_bytes(b"x" * 10)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    fn, calls = _load_concat(runner)
    fn(segs, str(out), audio_file=str(audio), fps=30.0, temp_dir=str(tmp_path),
       timeline_seconds=1234.5)
    cmd = calls[0]["cmd"]
    assert "-shortest" in cmd
    assert cmd[cmd.index("-t") + 1] == "1234.5"
    assert "pcm_s24le" in cmd
    assert cmd[cmd.index("-ar") + 1] == "48000"
    assert "+faststart" in cmd
    assert cmd[cmd.index("-c:v") + 1] == "copy"


def test_no_hardcoded_assembly_timeout_literal_survives_in_the_function():
    """The whole defect was a literal. Assert none of the old ones remain."""
    src = ast.unparse(_func(_tree(_FFMPEG_SRC), "concatenate_videos_ffmpeg"))
    for call in ("timeout=300", "timeout=120", "timeout=600"):
        assert call not in src.replace(" ", ""), f"{call} still present"


def test_every_runner_call_in_the_function_uses_a_budget_variable():
    """Structural: each ``_run_media_command`` gets a name, never a constant."""
    node = _func(_tree(_FFMPEG_SRC), "concatenate_videos_ffmpeg")
    found = 0
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Name)
                and call.func.id == "_run_media_command"):
            continue
        found += 1
        kw = {k.arg: k.value for k in call.keywords}
        assert "timeout" in kw, "timeout must be passed explicitly"
        assert isinstance(kw["timeout"], ast.Name), (
            "timeout must be a budget variable, not a literal")
    assert found >= 5, f"expected every assembly runner call, found {found}"


# ===========================================================================
# 3. A STALLED CHILD STILL DIES AND IS REAPED  (real subprocess, no FFmpeg)
# ===========================================================================

def _load_runner():
    node = _func(_tree(_FFMPEG_SRC), "_run_media_command")
    namespace = {
        "os": os, "time": time, "subprocess": subprocess,
        "RenderCancelled": _Cancelled,
        "_CANCEL_POLL_SECONDS": 0.05,
        "_TERMINATE_GRACE_SECONDS": 5.0,
        "List": list,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _FFMPEG_SRC, "exec"),
         namespace)
    return namespace["_run_media_command"]


_STALL = [sys.executable, "-c", "import time; time.sleep(600)"]


def test_a_stalled_child_times_out_and_is_reaped_on_the_blocking_path():
    """`lifecycle=None` is the historical path: subprocess.run kills and waits."""
    run = _load_runner()
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        run(_STALL, timeout=2)
    assert time.perf_counter() - started < 60


def test_a_stalled_child_times_out_and_is_reaped_on_the_cancellable_path():
    """With a lifecycle the runner polls, kills at the deadline and REAPS before raising."""
    run = _load_runner()
    lifecycle = types.SimpleNamespace(cancel_requested=lambda: False)
    started = time.perf_counter()
    with pytest.raises(subprocess.TimeoutExpired):
        run(_STALL, timeout=2, lifecycle=lifecycle)
    elapsed = time.perf_counter() - started
    assert 1.0 < elapsed < 60


def test_cancellation_kills_and_reaps_a_live_child_before_raising():
    run = _load_runner()
    lifecycle = types.SimpleNamespace(cancel_requested=lambda: True)
    started = time.perf_counter()
    with pytest.raises(_Cancelled):
        run(_STALL, timeout=600, lifecycle=lifecycle)
    assert time.perf_counter() - started < 30


# ===========================================================================
# 4. REAL FFMPEG  (opt-in: set BEATSYNC_TEST_FFMPEG to the portable binary)
# ===========================================================================

def _ffmpeg_binary():
    explicit = os.environ.get("BEATSYNC_TEST_FFMPEG")
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("ffmpeg")
    return found


def _sibling_ffprobe(ffmpeg_path):
    directory, name = os.path.split(ffmpeg_path)
    return os.path.join(directory, name.replace("ffmpeg", "ffprobe", 1))


_FFMPEG_BIN = _ffmpeg_binary()
_needs_ffmpeg = pytest.mark.skipif(
    not _FFMPEG_BIN, reason="set BEATSYNC_TEST_FFMPEG to the portable ffmpeg binary")


@_needs_ffmpeg
def test_real_concat_mux_publishes_exactly_the_planned_frames(tmp_path):
    """End to end with decodable media: plan == published, and A/V end together.

    Small by design (three segments, 5 s total) -- this is the contract test, not a
    production-length render.
    """
    ffmpeg = _FFMPEG_BIN
    ffprobe = _sibling_ffprobe(ffmpeg)
    if not os.path.isfile(ffprobe):
        pytest.skip("ffprobe not beside ffmpeg")

    fps = 25
    frames_each = [50, 50, 25]          # 125 frames total = 5.0 s at 25 fps
    segments = []
    for i, n in enumerate(frames_each):
        p = tmp_path / f"seg{i}.mp4"
        subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi",
             "-i", f"testsrc2=size=320x240:rate={fps}", "-frames:v", str(n),
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-fps_mode", "cfr", "-y", str(p)],
            check=True, capture_output=True, timeout=120)
        segments.append(str(p))

    audio = tmp_path / "music.wav"
    subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:sample_rate=48000:duration=6", "-c:a", "pcm_s16le",
         "-y", str(audio)],
        check=True, capture_output=True, timeout=120)

    total_frames = sum(frames_each)
    timeline_seconds = total_frames / float(fps)
    out = tmp_path / "assembled.mp4"

    def runner(cmd, timeout, lifecycle):
        real = [ffmpeg if a == r"C:\ffmpeg\ffmpeg.exe" else a for a in cmd]
        return subprocess.run(real, capture_output=True, text=True,
                              timeout=timeout, check=False)

    fn, calls = _load_concat(runner)
    produced = fn(segments, str(out), audio_file=str(audio), fps=float(fps),
                  temp_dir=str(tmp_path), timeline_seconds=timeline_seconds)

    assert produced == str(out)
    assert os.path.getsize(out) > 0
    assert len(calls) == 1, "the stream-copy path must succeed without re-encoding"
    # short workload -> historical floor preserved
    assert calls[0]["timeout"] == budget.MIN_ASSEMBLY_TIMEOUT_SECONDS

    counted = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of",
         "default=nw=1:nk=1", "--", str(out)],
        capture_output=True, text=True, timeout=300, check=True)
    assert int(counted.stdout.strip()) == total_frames

    durations = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "default=nw=1", "--", str(out)],
        capture_output=True, text=True, timeout=120, check=True).stdout
    assert "pcm_s24le" in subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name", "-of", "default=nw=1:nk=1", "--", str(out)],
        capture_output=True, text=True, timeout=120, check=True).stdout
    assert durations  # both streams described

    decoded = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(out), "-f", "null", "-"],
        capture_output=True, text=True, timeout=300)
    assert decoded.returncode == 0
    assert decoded.stderr.strip() == "", decoded.stderr[:400]
