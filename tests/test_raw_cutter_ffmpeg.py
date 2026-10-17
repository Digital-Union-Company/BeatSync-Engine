"""Raw Cutter V1: the real-FFmpeg tier.

Opt-in, exactly like `tests/test_audio_mixdown.py`'s final section: these build tiny synthetic
videos with hard scene cuts and run the actual binary over them, because the things most worth
proving here are properties of FFmpeg and of real containers, not of the strings we hand it —
does `-an` really remove the stream, is every exported part playable, is the original untouched.

Skipped when no FFmpeg is available, so the default suite still runs on a bare interpreter.
Fixtures are a few hundred kilobytes and live in pytest's own `tmp_path`; the owner's media
library is never touched.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

from beatsync_fork import raw_cutter as rc

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPORT = os.path.join(_REPO_ROOT, "src", "raw_cutter_export.py")


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


FFMPEG, FFPROBE = _ffmpeg_pair()
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE and os.path.isfile(FFMPEG) and os.path.isfile(FFPROBE)),
    reason="no FFmpeg available; set BEATSYNC_TEST_FFMPEG to the portable build")


def _run(cmd, timeout=180):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------------------------

#: Flat colours, ordered so that every ADJACENT pair is near-complementary. The scene score is a
#: frame-difference measure, so what matters is the contrast across each cut, not how distinct the
#: palette is overall: an early draft used red/green/blue in that order and FFmpeg scored the
#: red->green transition below the 0.27 threshold, which made the fixture -- not the detector --
#: the thing under test.
_COLOURS = ["black", "white", "red", "cyan", "yellow", "blue", "green", "magenta"]


def _make_video(path, shots, with_audio=False, fps=10, size="160x120"):
    """Concatenate solid-colour shots into one file. `shots` is a list of seconds."""
    parts = []
    inputs = []
    for index, seconds in enumerate(shots):
        colour = _COLOURS[index % len(_COLOURS)]
        inputs.extend(['-f', 'lavfi', '-t', f"{seconds}",
                       '-i', f"color=c={colour}:s={size}:r={fps}"])
        parts.append(f"[{index}:v]")
    filter_complex = "".join(parts) + f"concat=n={len(shots)}:v=1:a=0[v]"
    cmd = [FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', *inputs,
           '-filter_complex', filter_complex, '-map', '[v]']
    if with_audio:
        total = sum(shots)
        cmd[4:4] = ['-f', 'lavfi', '-t', f"{total}", '-i', 'sine=frequency=440:sample_rate=44100']
        # the sine is input 0, so every video index shifted by one
        filter_complex = "".join(f"[{i + 1}:v]" for i in range(len(shots))) + \
            f"concat=n={len(shots)}:v=1:a=0[v]"
        cmd = [FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
               '-f', 'lavfi', '-t', f"{total}", '-i', 'sine=frequency=440:sample_rate=44100']
        for index, seconds in enumerate(shots):
            colour = _COLOURS[index % len(_COLOURS)]
            cmd.extend(['-f', 'lavfi', '-t', f"{seconds}",
                        '-i', f"color=c={colour}:s={size}:r={fps}"])
        cmd.extend(['-filter_complex', filter_complex, '-map', '[v]', '-map', '0:a',
                    '-c:a', 'aac', '-shortest'])
    cmd.extend(['-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)])
    result = _run(cmd)
    assert result.returncode == 0, result.stderr[-2000:]
    assert os.path.getsize(path) > 0
    return str(path)


def _streams(path):
    result = _run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,codec_name',
                   '-of', 'json', str(path)])
    assert result.returncode == 0, result.stderr[-1000:]
    return json.loads(result.stdout)['streams']


def _duration(path):
    result = _run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'csv=p=0', str(path)])
    assert result.returncode == 0
    return float(result.stdout.strip())


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# The adapter, loaded without importing the runtime
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def adapter():
    """Execute the REAL adapter functions against a namespace holding the real binaries.

    `raw_cutter_export` imports `ffmpeg_processing` -> `logger` -> librosa, which is not importable
    on a bare interpreter, so the house technique applies: extract the shipped bodies and run them.
    `_run_media_command` is the one seam replaced, by a plain `subprocess.run` with the same
    signature and the same `CompletedProcess` return.
    """
    with open(_EXPORT, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    wanted = ("CutterCancelled", "CutterJob", "ProbeResult", "SceneResult", "SegmentResult",
              "_parse_rate", "probe_source", "detect_scenes", "build_segment_command",
              "_validate_output", "export_segments", "_discard_staging",
              "prepare_run_directory",
              # [FORK] frame-boundary P6: `detect_scenes` is one coarse pass again. The per-cut
              # native refinement helpers P1-P5 needed here are gone, because the machinery they
              # supported is gone -- the planner now stands a fixed margin back from every
              # reported cut instead. See `beatsync_fork.raw_cutter.SCENE_GUARD_SECONDS`.
              "SCENE_TIMEOUT_SECONDS", "PROBE_TIMEOUT_SECONDS", "EXPORT_TIMEOUT_SECONDS",
              "ANALYSIS_FPS", "ANALYSIS_WIDTH", "SUPPORTED_SOURCE_SUFFIXES")
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in wanted)
             or (isinstance(n, ast.Assign) and len(n.targets) == 1
                 and getattr(n.targets[0], "id", None) in wanted)]
    found = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    missing = set(wanted) - found
    assert not missing, f"the adapter no longer defines {missing}"

    def run_media(cmd, timeout, lifecycle=None):
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding='utf-8', errors='replace')

    def short_error(stderr, max_chars=2200):
        return (stderr or "")[-max_chars:]

    def extract_scene_times(stderr):
        import re
        times = sorted({round(float(m), 3)
                        for m in re.findall(r'pts_time:([\d.]+)', stderr or "")})
        cleaned = []
        for value in times:
            if not cleaned or value - cleaned[-1] >= 0.08:
                cleaned.append(value)
        return cleaned

    namespace = {
        "os": os, "re": __import__("re"), "subprocess": subprocess,
        "uuid": __import__("uuid"),
        "threading": __import__("threading"), "time": __import__("time"),
        "dataclass": __import__("dataclasses").dataclass,
        "Callable": object, "Sequence": object,
        "fork_cutter": rc,
        "FFMPEG_PATH": FFMPEG, "FFPROBE_PATH": FFPROBE, "MAX_THREADS": 4,
        "_run_media_command": run_media,
        "_short_ffmpeg_error": short_error,
        "_extract_scene_times": extract_scene_times,
    }
    namespace["__annotations__"] = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _EXPORT, "exec"), namespace)
    return namespace


pytestmark = needs_ffmpeg


# =============================================================================================
# Probe
# =============================================================================================

def test_probe_reports_real_geometry_duration_and_audio(adapter, tmp_path):
    path = _make_video(tmp_path / "shots.mp4", [2, 2, 2])
    result = adapter["probe_source"](path)
    assert result.ok, result.error
    assert result.source.width == 160 and result.source.height == 120
    assert result.source.duration == pytest.approx(6.0, abs=0.35)
    assert result.source.fps == pytest.approx(10.0, abs=0.1)
    assert result.source.has_audio is False
    assert result.source.size_bytes > 0 and result.source.mtime_ns > 0


def test_probe_detects_a_real_audio_stream(adapter, tmp_path):
    path = _make_video(tmp_path / "withaudio.mp4", [2, 2], with_audio=True)
    assert any(s['codec_type'] == 'audio' for s in _streams(path)), "fixture has no audio"
    result = adapter["probe_source"](path)
    assert result.ok, result.error
    assert result.source.has_audio is True


@pytest.mark.parametrize("bad,hint", [
    ("", "Enter the full path"),
    ("   ", "Enter the full path"),
    (None, "Enter the full path"),
])
def test_probe_refuses_an_empty_request(adapter, bad, hint):
    result = adapter["probe_source"](bad)
    assert not result.ok and hint in result.error


def test_probe_refuses_a_missing_file(adapter, tmp_path):
    result = adapter["probe_source"](str(tmp_path / "nope.mp4"))
    assert not result.ok and "not found" in result.error.lower()


def test_probe_refuses_a_directory_and_an_unsupported_type(adapter, tmp_path):
    assert not adapter["probe_source"](str(tmp_path)).ok
    text = tmp_path / "notes.txt"
    text.write_text("hello", encoding="utf-8")
    result = adapter["probe_source"](str(text))
    assert not result.ok and "Unsupported" in result.error


def test_probe_refuses_a_file_that_is_not_really_video(adapter, tmp_path):
    """Invalid input: the extension says mp4, the bytes do not."""
    fake = tmp_path / "broken.mp4"
    fake.write_bytes(b"this is not an mp4 file at all" * 50)
    result = adapter["probe_source"](str(fake))
    assert not result.ok
    assert "could not read" in result.error.lower() or "no video stream" in result.error.lower()


# =============================================================================================
# Scene detection
# =============================================================================================

def test_hard_cuts_between_solid_colours_are_detected(adapter, tmp_path):
    path = _make_video(tmp_path / "four.mp4", [3, 3, 3, 3])
    result = adapter["detect_scenes"](path, 0.27)
    assert result.succeeded, result.error
    assert len(result.times) >= 3, result.times
    for expected in (3.0, 6.0, 9.0):
        assert any(abs(t - expected) < 0.6 for t in result.times), (expected, result.times)


def test_a_single_shot_reports_success_with_no_cuts(adapter, tmp_path):
    """The distinction this whole wrapper exists for: zero cuts, but the detector DID run."""
    path = _make_video(tmp_path / "one.mp4", [6])
    result = adapter["detect_scenes"](path, 0.27)
    assert result.succeeded is True
    assert result.times == ()
    assert result.error == ""


def test_a_failed_detector_is_reported_as_failure_not_as_zero_cuts(adapter, tmp_path):
    """The defect the task called out. A broken input must not look like a clean single-shot."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 4096)
    result = adapter["detect_scenes"](str(broken), 0.27)
    assert result.succeeded is False
    assert result.error
    assert result.times == ()


def test_higher_sensitivity_never_finds_fewer_cuts(adapter, tmp_path):
    path = _make_video(tmp_path / "mixed.mp4", [2, 2, 2, 2, 2])
    low = adapter["detect_scenes"](path, rc.sensitivity_to_threshold(10))
    high = adapter["detect_scenes"](path, rc.sensitivity_to_threshold(90))
    assert low.succeeded and high.succeeded
    assert len(high.times) >= len(low.times), (low.times, high.times)


# =============================================================================================
# Export
# =============================================================================================

def _plan_for(adapter, path, minimum=1.0, maximum=30.0, sensitivity=50):
    probe = adapter["probe_source"](path)
    assert probe.ok, probe.error
    scenes = adapter["detect_scenes"](path, rc.sensitivity_to_threshold(sensitivity))
    assert scenes.succeeded, scenes.error
    plan = rc.plan_segments(probe.source.duration, scenes.times, minimum, maximum, sensitivity)
    return probe.source, plan


def test_every_exported_segment_is_playable_and_the_original_is_untouched(adapter, tmp_path):
    path = _make_video(tmp_path / "src.mp4", [3, 3, 3])
    before_hash, before_mtime = _sha256(path), os.stat(path).st_mtime_ns
    source, plan = _plan_for(adapter, path)

    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / "out"), path, "20261016_1200")
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')

    assert results and all(r.ok for r in results), [r.error for r in results if not r.ok]
    assert len(results) == plan.count
    for result, segment in zip(results, plan.segments):
        assert os.path.isfile(result.path)
        assert os.path.getsize(result.path) > 0
        assert any(s['codec_type'] == 'video' for s in _streams(result.path))
        assert _duration(result.path) == pytest.approx(segment.duration, abs=0.5)

    assert _sha256(path) == before_hash, "the source file was modified"
    assert os.stat(path).st_mtime_ns == before_mtime


def test_remove_audio_removes_the_stream_itself_not_just_the_signal(adapter, tmp_path):
    """The control promises stream removal. A silenced-but-present stream would not be that."""
    path = _make_video(tmp_path / "loud.mp4", [3, 3], with_audio=True)
    assert any(s['codec_type'] == 'audio' for s in _streams(path))
    source, plan = _plan_for(adapter, path)

    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / "silent"), path, "t1")
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    for result in results:
        kinds = [s['codec_type'] for s in _streams(result.path)]
        assert 'audio' not in kinds, f"{result.path} still has an audio stream: {kinds}"


def test_keeping_audio_produces_a_real_aac_stream(adapter, tmp_path):
    path = _make_video(tmp_path / "keep.mp4", [3, 3], with_audio=True)
    source, plan = _plan_for(adapter, path)
    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / "kept"), path, "t2")
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=False, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    for result in results:
        streams = _streams(result.path)
        audio = [s for s in streams if s['codec_type'] == 'audio']
        assert audio, f"{result.path} lost its audio"
        assert audio[0]['codec_name'] == 'aac'


def test_a_source_without_audio_exports_cleanly_with_audio_kept(adapter, tmp_path):
    """Remove Audio OFF on a silent source must still work -- FFmpeg must not be asked to map a
    stream that does not exist."""
    path = _make_video(tmp_path / "silent_src.mp4", [3, 3])
    source, plan = _plan_for(adapter, path)
    assert source.has_audio is False
    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / "noaudio"), path, "t3")
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=False, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]


def test_the_exported_segments_preserve_the_source_geometry(adapter, tmp_path):
    path = _make_video(tmp_path / "geo.mp4", [3, 3], size="320x180")
    source, plan = _plan_for(adapter, path)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "geoout"), path, "t4")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results)
    for result in results:
        info = _run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                     '-show_entries', 'stream=width,height', '-of', 'csv=p=0', result.path])
        assert info.stdout.strip().startswith("320,180"), info.stdout


def test_the_exported_segments_account_for_the_source_duration(adapter, tmp_path):
    """[FORK] P6. The old form of this test asserted the clips TILE the source to within 0.6 s.

    Under the owner's conservative-trim policy that is no longer true and must not be asserted:
    four shots mean three detected cuts, and each costs two guards. Measured, this fixture
    exports 6.8 s of its 8.0 s -- and the missing 1.2 s is exactly `3 x 2 x 0.2`.

    So the expectation is replaced rather than loosened: the exported total plus what the plan
    SAYS it discarded must equal the source. A tolerance wide enough to absorb the guard would
    have hidden a real coverage bug of the same size, which is the failure mode this avoids.
    """
    path = _make_video(tmp_path / "cover.mp4", [2, 2, 2, 2])
    source, plan = _plan_for(adapter, path)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "coverout"), path, "t5")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results)
    total = sum(_duration(r.path) for r in results)

    assert plan.guard_discarded == pytest.approx(
        2 * plan.detected_scene_count * rc.SCENE_GUARD_SECONDS, abs=1e-9)
    assert total + plan.skipped_duration == pytest.approx(source.duration, abs=0.6)
    # And the discarded footage is guard, not quietly dropped material.
    assert plan.skipped_shot_duration == 0.0 and plan.dropped_tail_duration == 0.0, (
        plan.skipped_shot_duration, plan.dropped_tail_duration)


# =============================================================================================
# Collisions, refusal and cancellation
# =============================================================================================

def test_an_existing_run_directory_is_refused_never_reused(adapter, tmp_path):
    root = tmp_path / "out"
    first, error = adapter["prepare_run_directory"](str(root), "clip.mkv", "same_stamp")
    assert not error and os.path.isdir(first)
    (os.path.join(first, "keep_me.txt"))
    with open(os.path.join(first, "keep_me.txt"), "w", encoding="utf-8") as handle:
        handle.write("previous run")

    second, error = adapter["prepare_run_directory"](str(root), "clip.mkv", "same_stamp")
    assert second == "" and "already exists" in error
    assert os.path.isfile(os.path.join(first, "keep_me.txt")), "the earlier run was disturbed"


def test_a_filename_collision_refuses_instead_of_overwriting(adapter, tmp_path):
    path = _make_video(tmp_path / "coll.mp4", [3, 3])
    source, plan = _plan_for(adapter, path)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "collout"), path, "t6")

    # Pre-create the first expected output with known content.
    first_name = rc.segment_filename(source.path, 1, plan.count)
    victim = os.path.join(run_dir, first_name)
    with open(victim, "wb") as handle:
        handle.write(b"ORIGINAL CONTENT")

    # Stated as a precondition rather than assumed: if detection found no cut there would be one
    # segment, `results[1:]` would be empty, and the "the rest still export" assertion below would
    # pass or fail for a reason that has nothing to do with collisions.
    assert plan.count >= 2, f"fixture produced {plan.count} segment(s); need at least two"

    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert not results[0].ok and "already exists" in results[0].error
    with open(victim, "rb") as handle:
        assert handle.read() == b"ORIGINAL CONTENT", "an existing file was overwritten"
    assert any(r.ok for r in results[1:]), "the remaining segments should still export"


def test_cancellation_stops_the_run_and_keeps_completed_clips(adapter, tmp_path):
    path = _make_video(tmp_path / "cancel.mp4", [2, 2, 2, 2, 2, 2])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=3.0)
    assert plan.count >= 3, plan.count
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "cancelout"), path, "t7")

    job = adapter["CutterJob"]()
    seen = []

    def progress(index, total, name):
        seen.append(index)
        if index == 2:
            job.cancel()

    with pytest.raises(adapter["CutterCancelled"]):
        adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                   remove_audio=True, encoder='libx264', job=job,
                                   on_progress=progress)

    written = sorted(os.listdir(run_dir))
    assert written, "clips completed before the cancellation must be kept"
    assert len(written) < plan.count, "cancellation did not stop the run"
    for name in written:
        full = os.path.join(run_dir, name)
        assert os.path.getsize(full) > 0
        assert any(s['codec_type'] == 'video' for s in _streams(full))
    assert _sha256(path), "the source must still be readable"


def test_a_cancelled_run_leaves_no_partial_file(adapter, tmp_path):
    path = _make_video(tmp_path / "partial.mp4", [2, 2, 2, 2])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=3.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "partialout"), path, "t8")
    job = adapter["CutterJob"]()
    job.cancel()
    with pytest.raises(adapter["CutterCancelled"]):
        adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                   remove_audio=True, encoder='libx264', job=job)
    assert os.listdir(run_dir) == [], "a cancelled-before-start run wrote something"


def test_an_export_failure_is_reported_and_leaves_no_broken_file(adapter, tmp_path):
    """Export failure: the source disappears between planning and cutting."""
    path = _make_video(tmp_path / "vanish.mp4", [3, 3])
    source, plan = _plan_for(adapter, path)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "vanishout"), path, "t9")
    os.remove(path)

    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert results and not any(r.ok for r in results)
    assert all(r.error for r in results)
    assert os.listdir(run_dir) == [], "a failed export left a file behind"


def test_the_run_directory_is_named_from_the_source_and_timestamp(adapter, tmp_path):
    created, error = adapter["prepare_run_directory"](
        str(tmp_path / "root"), r"J:\F\Holiday Clip.mkv", "20261016_203000")
    assert not error
    assert os.path.basename(created) == "Holiday_Clip_raw_parts_20261016_203000"


# =============================================================================================
# Quality-first selection, through the real detector and the real encoder (R1)
# =============================================================================================

def test_a_short_tail_shot_is_skipped_rather_than_merged_end_to_end(adapter, tmp_path):
    """The owner's Example A against real footage: 10s shot + 4s shot, min 5 max 10.

    Exactly one clip is written, it is the 10-second shot, and the 4-second tail is left on the
    floor -- not glued on to make a 14-second clip with a cut in the middle.
    """
    path = _make_video(tmp_path / "tail.mp4", [10, 4])
    source, plan = _plan_for(adapter, path, minimum=5.0, maximum=10.0)

    assert plan.count == 1, [(s.start, s.end) for s in plan.segments]
    assert len(plan.skipped_shots) == 1
    assert plan.skipped_shots[0].skip_reason == rc.SKIP_TOO_SHORT

    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / "out"), path, "qf1")
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    written = sorted(os.listdir(run_dir))
    assert len(written) == 1, written
    assert _duration(os.path.join(run_dir, written[0])) == pytest.approx(10.0, abs=0.5)


def test_a_source_of_only_short_shots_exports_nothing_at_all(adapter, tmp_path):
    """The owner's Example D, end to end: four 3-second shots against a 5-second minimum."""
    path = _make_video(tmp_path / "allshort.mp4", [3, 3, 3, 3])
    source, plan = _plan_for(adapter, path, minimum=5.0, maximum=300.0)
    assert plan.count == 0
    assert plan.has_work is False

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "none"), path, "qf2")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert results == []
    assert os.listdir(run_dir) == [], "an empty plan must write nothing"


def test_a_continuous_shot_is_divided_without_crossing_any_cut(adapter, tmp_path):
    """Example C: one continuous take, max 10 -> two 7-second clips from inside it."""
    path = _make_video(tmp_path / "long.mp4", [14])
    source, plan = _plan_for(adapter, path, minimum=5.0, maximum=10.0)
    assert plan.count == 2
    assert all(s.shot_index == 1 for s in plan.segments)

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "split"), path, "qf3")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    total = sum(_duration(r.path) for r in results)
    assert total == pytest.approx(source.duration, abs=0.6)


# =============================================================================================
# File ownership (R1 finding B)
# =============================================================================================

def test_a_failed_export_never_removes_a_foreign_file_at_the_destination(adapter, tmp_path):
    """The exact defect. The earlier version deleted the DESTINATION path whenever a segment
    failed -- so a file that appeared there after the existence check, belonging to some other
    process, was destroyed by a failure that had nothing to do with it.

    Reproduced by letting the collision check pass and then making the export fail: the source is
    removed after planning, so FFmpeg cannot read it, and a foreign file is planted at the
    destination in the meantime.
    """
    path = _make_video(tmp_path / "own.mp4", [6, 6])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=30.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "foreign"), path, "fo1")
    os.remove(path)                                   # guarantees every export fails

    foreign = os.path.join(run_dir, rc.segment_filename(source.path, 1, plan.count))
    with open(foreign, "wb") as handle:
        handle.write(b"SOMEBODY ELSE'S FILE")

    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert not any(r.ok for r in results)
    assert os.path.isfile(foreign), "a failed export deleted a file it did not own"
    with open(foreign, "rb") as handle:
        assert handle.read() == b"SOMEBODY ELSE'S FILE"


def test_a_failed_export_leaves_no_staging_artifact_behind(adapter, tmp_path):
    path = _make_video(tmp_path / "stale.mp4", [6, 6])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=30.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "staging"), path, "st1")
    os.remove(path)

    adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                               remove_audio=True, encoder='libx264')
    leftovers = [n for n in os.listdir(run_dir) if ".part." in n]
    assert leftovers == [], f"staging artifacts were left behind: {leftovers}"


def test_a_successful_export_leaves_no_staging_artifact_either(adapter, tmp_path):
    path = _make_video(tmp_path / "clean.mp4", [6, 6])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=30.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "cleanout"), path, "st2")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results)
    assert not [n for n in os.listdir(run_dir) if ".part." in n]
    assert all(n.endswith(".mp4") and not n.startswith(".") for n in os.listdir(run_dir))


# =============================================================================================
# Cancellation accounting (R1 finding A)
# =============================================================================================

def test_cancellation_reports_the_files_that_were_actually_written(adapter, tmp_path):
    """`export_segments` raises, which discards its return value -- so the completed clips are
    only knowable through the callback. This asserts the callback saw exactly what is on disk."""
    path = _make_video(tmp_path / "count.mp4", [3, 3, 3, 3, 3, 3])
    source, plan = _plan_for(adapter, path, minimum=1.0, maximum=3.0)
    assert plan.count >= 3, plan.count
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "countout"), path, "cc1")

    job = adapter["CutterJob"]()
    completed = []

    def finished(result):
        completed.append(result)
        if len([r for r in completed if r.ok]) == 2:
            job.cancel()

    with pytest.raises(adapter["CutterCancelled"]):
        adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                   remove_audio=True, encoder='libx264', job=job,
                                   on_complete=finished)

    written = sorted(n for n in os.listdir(run_dir) if not n.startswith("."))
    kept = [r for r in completed if r.ok]
    assert len(kept) == len(written) == 2, (len(kept), written)
    assert sorted(os.path.basename(r.path) for r in kept) == written
    for name in written:
        assert _duration(os.path.join(run_dir, name)) > 0

# =============================================================================================
# [FORK] Digital-Union (frame-boundary P6): the CONSERVATIVE GUARD, measured on decoded pixels.
#
# The property the owner cares about is unchanged and is still the first thing asserted here: no
# exported clip may contain a frame of a neighbouring shot. What changed is how it is delivered.
# P1-P5 tried to find the cut's exact frame with a second native-frame FFmpeg pass per cut; P6
# stands a fixed `SCENE_GUARD_SECONDS` margin back from the coarse estimate on both sides and
# discards the footage in between.
#
# So the frame-conservation expectations in this section are deliberately gone: frames around a
# detected cut ARE now intentionally lost, and the replacement assertions say exactly which ones
# and prove that nothing outside those bands went missing. A clip that merely has about the right
# duration still proves nothing; every test below decodes real pixels.
# =============================================================================================

_SIZE = "320x240"
_W, _H = 320, 240


def _make_frame_exact_cut(path, rate, frames_a, frames_b,
                          colour_a="black", colour_b="white"):
    """Exactly `frames_a` frames of one colour, a hard cut, then `frames_b` of another.

    `trim=end_frame=` counts FRAMES, so the cut lands on a known frame index rather than on a
    duration that the muxer may round.
    """
    result = _run([
        FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', f"color={colour_a}:s={_SIZE}:r={rate}",
        '-f', 'lavfi', '-i', f"color={colour_b}:s={_SIZE}:r={rate}",
        '-filter_complex',
        f"[0:v]trim=end_frame={frames_a},setpts=PTS-STARTPTS[a];"
        f"[1:v]trim=end_frame={frames_b},setpts=PTS-STARTPTS[b];"
        f"[a][b]concat=n=2:v=1:a=0[v]",
        '-map', '[v]', '-c:v', 'libx264', '-preset', 'veryslow', '-crf', '0',
        '-pix_fmt', 'yuv420p', '-g', '30', '-r', str(rate), str(path)])
    assert result.returncode == 0, result.stderr[-2000:]
    return str(path)


def _frame_labels(path):
    """Every DECODED frame as 'A' (dark) or 'B' (bright), in presentation order.

    Raw gray output, so this is the frame's own pixels -- not a container duration, not a
    keyframe index, and not something a seek could fabricate.

    `-fps_mode passthrough` is load-bearing on a variable-rate clip: without it FFmpeg resamples
    the rawvideo output to the container's nominal `r_frame_rate`, and a 60 fps passage counts as
    half its real length. Measured on the VFR fixture: 317 frames instead of 270. That would be
    indistinguishable from the exporter losing frames.
    """
    result = subprocess.run(
        [FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', '-i', str(path),
         '-map', '0:v:0', '-fps_mode', 'passthrough',
         '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
        capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    raw, size, out = result.stdout, _W * _H, []
    for offset in range(0, len(raw) - size + 1, size):
        mean = sum(raw[offset:offset + size]) / size
        out.append("A" if mean <= 40 else ("B" if mean >= 215 else "?"))
    return "".join(out)


def _cut_and_label(adapter, path, tmp_path, name, minimum=1.0, maximum=300.0,
                   encoder='libx264', remove_audio=True):
    source, plan = _plan_for(adapter, path, minimum=minimum, maximum=maximum)
    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / name), path, name)
    assert not error, error
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=remove_audio, encoder=encoder)
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    return plan, [(_frame_labels(r.path), r) for r in results]


def _source_frame_times(path):
    """Every source frame's presentation time, on the PLANNER axis (first frame = 0.0).

    FFprobe answers on the CONTAINER axis, which begins at the stream's own `start_time` and
    need not be zero. The plan is expressed in planner time. Comparing the two directly is what
    makes a correct non-zero-origin export look as if it had lost 150 frames when it lost 12.
    """
    result = _run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'frame=pts_time', '-of', 'csv=p=0', str(path)], timeout=600)
    values = sorted(float(v) for v in re.findall(r"([0-9]+\.?[0-9]*)", result.stdout or ""))
    return [round(v - values[0], 6) for v in values] if values else []


def _guard_audit(adapter, path, plan, labelled):
    """Attribute every source frame that reached no clip to exactly one declared cause.

    Returns ``(by_cause, unexplained)``. "Not exported" is not a defect under this policy; being
    unable to say WHY is. `unexplained` is the number that has to be zero.
    """
    guard = plan.guard_seconds
    cuts = rc._usable_scene_times(
        [c for c in _detected(adapter, path)], plan.source_duration)
    bands = [(c - guard, c + guard) for c in cuts]
    kept = [(s.start, s.end) for s in plan.segments]
    skipped = [(s.start, s.end) for s in plan.shots if not s.exported]
    exported = [(s.start, s.end) for s in plan.shots if s.exported]

    by_cause = {"guard_band": 0, "skipped_shot": 0, "dropped_tail": 0}
    unexplained = []
    for moment in _source_frame_times(path):
        if any(a - 1e-9 <= moment < b - 1e-9 for a, b in kept):
            continue
        if any(a - 1e-9 <= moment <= b + 1e-9 for a, b in bands):
            by_cause["guard_band"] += 1
        elif any(a - 1e-9 <= moment < b + 1e-9 for a, b in skipped):
            by_cause["skipped_shot"] += 1
        elif any(a - 1e-9 <= moment < b + 1e-9 for a, b in exported):
            by_cause["dropped_tail"] += 1
        else:
            unexplained.append(moment)
    return by_cause, unexplained


_DETECTED_CACHE = {}


def _detected(adapter, path):
    if path not in _DETECTED_CACHE:
        scenes = adapter["detect_scenes"](path, 0.27)
        assert scenes.succeeded, scenes.error
        _DETECTED_CACHE[path] = list(scenes.times)
    return _DETECTED_CACHE[path]


# ------------------------------------------------------------------ fixture builders carried
# forward from the P3/P5 sections, unchanged in behaviour: the fixtures were never the problem.

def _make_offset_cut(path, rate, frames_a, frames_b, offset,
                     colour_a="black", colour_b="white"):
    """A frame-exact hard cut in a container whose timestamps START AT `offset` seconds.

    `-output_ts_offset` on a stream copy moves the stream's `start_time` without touching a single
    frame, so the cut is still at frame `frames_a` of the file and still at `frames_a / rate` on
    the planner's axis -- only the container's own numbering changes.
    """
    plain = _make_frame_exact_cut(path.parent / (path.name + ".plain.mp4"), rate,
                                  frames_a, frames_b, colour_a, colour_b)
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
                   '-i', plain, '-map', '0:v:0', '-c:v', 'copy',
                   '-output_ts_offset', str(offset), str(path)])
    assert result.returncode == 0, result.stderr[-2000:]
    return str(path)


def _make_edit_list_cut(path, rate, frames_a, frames_b, delay=0.5):
    """The same cut in an MP4 that delays presentation with an EDIT LIST rather than a pts shift.

    A different mechanism from `_make_offset_cut` and worth covering separately: an edit list
    leaves `start_time` alone and is applied by the demuxer, so a reader that only consults
    `start_time` sees a zero-based file that does not behave like one.
    """
    plain = _make_frame_exact_cut(path.parent / (path.name + ".plain.mp4"), rate,
                                  frames_a, frames_b)
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
                   '-itsoffset', str(delay), '-i', plain, '-map', '0:v:0', '-c:v', 'copy',
                   '-movflags', '+faststart', str(path)])
    assert result.returncode == 0, result.stderr[-2000:]
    return str(path)


def _make_three_shots(path, counts, rate=30):
    """Frame-exact hard cuts between N DISTINCT solid shots."""
    inputs = []
    chains = []
    for index, count in enumerate(counts):
        inputs.extend(['-f', 'lavfi', '-i',
                       "color=%s:s=%s:r=%s" % (_COLOURS[index % len(_COLOURS)], _SIZE, rate)])
        chains.append("[%d:v]trim=end_frame=%d,setpts=PTS-STARTPTS[s%d];" % (index, count, index))
    joins = "".join("[s%d]" % i for i in range(len(counts)))
    result = _run([
        FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', *inputs,
        '-filter_complex',
        "".join(chains) + "%sconcat=n=%d:v=1:a=0[v]" % (joins, len(counts)),
        '-map', '[v]', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '0',
        '-pix_fmt', 'yuv420p', '-g', '30', '-r', str(rate), str(path)])
    assert result.returncode == 0, result.stderr[-2000:]
    return str(path)


def _make_vfr_source(path, counts, rates):
    """One file whose shots were genuinely authored at DIFFERENT frame rates.

    Built by concatenating separately-encoded parts with `-c copy` and `-fps_mode passthrough`,
    so the result really carries three different inter-frame intervals rather than one nominal
    rate with duplicated frames. Verified by the caller through the decoded timestamps.
    """
    parts = []
    for index, (count, rate) in enumerate(zip(counts, rates)):
        part = path.parent / ("_vfr_%d.mp4" % index)
        result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
                       '-f', 'lavfi', '-i',
                       "color=%s:s=%s:r=%s" % (_COLOURS[index % len(_COLOURS)], _SIZE, rate),
                       '-frames:v', str(count), '-c:v', 'libx264', '-preset', 'veryfast',
                       '-crf', '0', '-pix_fmt', 'yuv420p', '-g', '30', str(part)])
        assert result.returncode == 0, result.stderr[-2000:]
        parts.append(part)
    listing = path.parent / (path.name + ".txt")
    with open(listing, "w", encoding="utf-8") as handle:
        for part in parts:
            handle.write("file '%s'\n" % str(part).replace("\\", "/"))
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
                   '-f', 'concat', '-safe', '0', '-i', str(listing),
                   '-map', '0:v:0', '-c:v', 'copy', '-fps_mode', 'passthrough', str(path)])
    assert result.returncode == 0, result.stderr[-2000:]
    return str(path)


def _coarse_native(adapter, path, threshold=0.27):
    """Every ADJACENT-frame transition above the threshold, whole file, no `fps` filter.

    Used by fixtures to prove they contain the transitions they claim to -- so a test that passes
    because its fixture was inert is distinguishable from one that passes because the code is
    right.
    """
    result = _run([
        FFMPEG, '-nostdin', '-hide_banner', '-threads', '2', '-i', path,
        '-vf', "scale=%s:-2:flags=fast_bilinear,select='gt(scene,%s)',showinfo"
               % (adapter["ANALYSIS_WIDTH"], threshold),
        '-an', '-sn', '-dn', '-f', 'null', '-'], timeout=600)
    return [float(m) for m in re.findall(r"pts_time:([0-9]+\.?[0-9]*)", result.stderr or "")]


# ------------------------------------------------------------------ N-way frame provenance
#
# `_frame_labels` answers A/B, which is enough for a two-shot fixture. Three or more shots need
# the frame attributed to WHICH shot, and the shot's appearance is measured from the source
# itself rather than assumed from the colour name -- a fixture that drifted would otherwise look
# like a leak.

def _frame_means(path):
    """Mean luminance of every DECODED frame, in presentation order. See `_frame_labels` on
    why `-fps_mode passthrough` is required."""
    result = subprocess.run(
        [FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', '-i', str(path),
         '-map', '0:v:0', '-fps_mode', 'passthrough',
         '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
        capture_output=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    raw, size, out = result.stdout, _W * _H, []
    for offset in range(0, len(raw) - size + 1, size):
        out.append(sum(raw[offset:offset + size]) / size)
    return out


def _shot_profile(source, counts):
    """Each shot's own mean luminance, measured from the SOURCE at its known frame range."""
    means = _frame_means(source)
    assert len(means) == sum(counts), (
        "the fixture decoded %d frames, not the %d it was authored with" % (len(means),
                                                                            sum(counts)))
    profile, at = [], 0
    for count in counts:
        block = means[at:at + count]
        profile.append(sum(block) / len(block))
        at += count
    # The shots must be distinguishable, or a leak could not be seen.
    for i in range(len(profile)):
        for j in range(i + 1, len(profile)):
            assert abs(profile[i] - profile[j]) > 12.0, (
                "shots %d and %d look alike (%.1f vs %.1f), so this fixture cannot detect a leak"
                % (i, j, profile[i], profile[j]))
    return profile, counts


def _classify(values, profile, tolerance=6.0):
    """Map each frame mean onto the index of the shot it came from, or -1 if it matches none."""
    out = []
    for value in values:
        best = min(range(len(profile)), key=lambda i: abs(value - profile[i]))
        out.append(best if abs(value - profile[best]) <= tolerance else -1)
    return out


def _rate_value(rate):
    """`"30000/1001"` -> 29.97. A fixture literal, parsed rather than evaluated."""
    text = str(rate)
    if "/" in text:
        num, den = text.split("/", 1)
        return float(num) / float(den)
    return float(text)


# ------------------------------------------------------------------ the core provenance tests

@pytest.mark.parametrize("rate,frames_a,frames_b", [
    ("30", 92, 98),
    ("60", 184, 190),
    ("25", 77, 80),
    ("24000/1001", 74, 80),
    ("30000/1001", 92, 95),
])
def test_no_frame_of_the_next_scene_is_decoded_into_the_clip_before_it(
        adapter, tmp_path, rate, frames_a, frames_b):
    """THE regression, and the one assertion the whole policy exists to keep true."""
    path = _make_frame_exact_cut(tmp_path / "cut.mp4", rate, frames_a, frames_b)
    plan, labelled = _cut_and_label(adapter, path, tmp_path, "boundary")
    assert len(labelled) == 2, [s.boundary_kind for s in plan.segments]

    first, second = labelled[0][0], labelled[1][0]
    assert "B" not in first, (
        f"{first.count('B')} frame(s) of the following scene were decoded into the end of the "
        f"first clip: {first[-12:]}")
    assert "A" not in second, (
        f"{second.count('A')} frame(s) of the previous scene leaked into the second clip: "
        f"{second[:12]}")
    assert "?" not in first + second, "a frame was neither scene A nor scene B"


@pytest.mark.parametrize("rate,frames_a,frames_b", [
    ("30", 92, 98),
    ("60", 184, 190),
    ("25", 77, 80),
])
def test_every_frame_the_two_clips_do_not_carry_lies_inside_a_guard_band(
        adapter, tmp_path, rate, frames_a, frames_b):
    """[FORK] P6 **replaces** `test_the_two_clips_account_for_every_source_frame`.

    That test asserted the two clips tile the source exactly. Under the owner's policy that is no
    longer the contract and asserting it would be asserting the old product: frames around a
    detected cut are now discarded on purpose.

    What replaces it is strictly more informative. Every source frame that reached no clip is
    attributed to a declared cause, and the only cause available here is the guard band -- there
    is no short shot and no dropped tail in this fixture. A frame lost anywhere else would be an
    unintended omission, which is still a defect, and it is counted rather than tolerated.
    """
    path = _make_frame_exact_cut(tmp_path / "tile.mp4", rate, frames_a, frames_b)
    plan, labelled = _cut_and_label(adapter, path, tmp_path, "tiling")
    by_cause, unexplained = _guard_audit(adapter, path, plan, labelled)

    assert not unexplained, (
        f"{len(unexplained)} source frame(s) reached no clip and lie in no declared guard band, "
        f"skipped shot or dropped tail: {unexplained[:8]}")
    assert by_cause["skipped_shot"] == 0 and by_cause["dropped_tail"] == 0, by_cause

    exported = sum(len(text) for text, _r in labelled)
    total = frames_a + frames_b
    assert exported + by_cause["guard_band"] == total, (
        f"{exported} exported + {by_cause['guard_band']} guarded != {total} source frames")

    # The guard is two margins wide, so the loss is bounded by what the policy declares -- it may
    # not quietly grow. One frame of slack each side for where the grid falls.
    nominal = 2 * plan.guard_seconds
    rate_value = _rate_value(rate)
    assert by_cause["guard_band"] <= round(nominal * rate_value) + 2, (
        by_cause["guard_band"], nominal, rate_value)


def test_no_clip_ever_contains_frames_of_two_different_shots(adapter, tmp_path):
    """Three distinct shots, so a leak in either direction is visible as a label change."""
    path = _make_three_shots(tmp_path / "interior.mp4", [182, 239, 179])
    source, plan = _plan_for(adapter, path, minimum=1.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "three"), path, "t3")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]

    profile, counts = _shot_profile(path, [182, 239, 179])
    for position, result in enumerate(results, start=1):
        shots_seen = {s for s in _classify(_frame_means(result.path), profile) if s >= 0}
        assert len(shots_seen) == 1, (
            f"clip {position} carries frames of shots {sorted(shots_seen)}; a clip must come "
            f"from exactly one shot")


def test_the_detected_cut_is_the_coarse_estimate_and_the_guard_covers_its_error(adapter,
                                                                                 tmp_path):
    """[FORK] P6 **replaces** `test_the_detected_cut_is_the_real_frame_time_not_the_analysis_grid`.

    That test asserted the reported cut is the real frame time, which was the refinement pass's
    whole job. It is deliberately false now: `detect_scenes` returns the coarse grid label and
    claims nothing more.

    The property that replaces it is the one the policy actually depends on -- the label's error
    is inside the guard. Measured here rather than assumed, on a cut whose true frame time is
    known exactly by construction.
    """
    path = _make_frame_exact_cut(tmp_path / "grid.mp4", "30", 92, 98)
    reported = _detected(adapter, path)
    assert reported, "the detector found nothing"

    true_cut = 92 / 30.0
    nearest = min(reported, key=lambda t: abs(t - true_cut))
    grid = 1.0 / adapter["ANALYSIS_FPS"]

    # It IS the grid label -- that is the point, and it is what makes the pass cheap.
    assert abs(nearest / grid - round(nearest / grid)) < 1e-6, (
        f"{nearest} is not on the {grid}s analysis grid; something is still refining it")
    # And the error is comfortably inside the guard, which is what makes the label safe to use.
    assert abs(nearest - true_cut) < rc.SCENE_GUARD_SECONDS, (
        f"the coarse estimate {nearest} is {abs(nearest - true_cut) * 1000:.1f} ms from the real "
        f"cut {true_cut}, outside the {rc.SCENE_GUARD_SECONDS}s guard")


def test_the_guard_handles_the_detection_error_that_defeated_refinement(adapter, tmp_path):
    """[FORK] P6. **The case P5-S1 flagged for review, and why the guard is not just cheaper.**

    P5-S1 measured a hard cut at 5.066667 that the coarse pass reported at 5.125, and could not
    fix it: refinement has to IDENTIFY the cut frame, and in that fixture the real cut's own
    inter-frame difference did not clear the sensitivity while an unrelated intra-shot frame did,
    so every candidate-selection rule returned the wrong frame and the exported clip opened with
    two frames of the outgoing shot.

    This fixture reproduces the DETECTION ERROR exactly -- true cut 5.066667, coarse report
    5.125, the same 58.3 ms -- and asserts the guard absorbs it. It does **not** reproduce the
    low-contrast detector failure that produced that error in P5-S1; the cut here scores 1.0
    natively. That distinction is deliberate and is stated rather than glossed: the guard's
    correctness does not depend on why the estimate was wrong, only on its error being bounded,
    which is precisely why it covers a case candidate selection could not.
    """
    path = _make_frame_exact_cut(tmp_path / "p5s1shape.mp4", "30", 152, 150)
    true_cut = 152 / 30.0
    reported = _detected(adapter, path)
    nearest = min(reported, key=lambda t: abs(t - true_cut))

    # Positive control: the estimate must really be LATE, or the fixture proves nothing.
    assert nearest > true_cut + 1e-6, (
        f"the coarse estimate {nearest} is not late of the real cut {true_cut}")

    plan, labelled = _cut_and_label(adapter, path, tmp_path, "p5s1shape")
    assert len(labelled) == 2, [s.boundary_kind for s in plan.segments]
    first, second = labelled[0][0], labelled[1][0]
    assert "B" not in first, f"clip 1 ends on {first.count('B')} frame(s) of the next shot"
    assert "A" not in second, (
        f"clip 2 opens with {second.count('A')} frame(s) of the previous shot -- the exact "
        f"symptom P5-S1 measured and could not remove")

    # And the margin really is a margin, not a near miss.
    assert true_cut - plan.segments[0].end >= 0.1, plan.segments[0].end
    assert plan.segments[1].start - true_cut >= 0.1, plan.segments[1].start


# ------------------------------------------------------------------ time splits lose nothing

def test_a_continuous_shot_split_only_by_the_maximum_loses_no_frame_at_all(adapter, tmp_path):
    """The owner's worked example, end to end on decoded pixels.

    No scene change anywhere, so no guard anywhere: the pieces must tile the source exactly. This
    is the half of the policy that is easy to get wrong by applying the margin uniformly.
    """
    path = _make_three_shots(tmp_path / "onelong.mp4", [900])
    source, plan = _plan_for(adapter, path, minimum=5.0, maximum=7.0)
    assert plan.detected_scene_count == 0, plan.detected_scene_count
    assert plan.guard_discarded == 0.0, "a time split is not a scene change"
    assert all(s.boundary_kind in ("time", "source-end") for s in plan.segments)

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "onelong_out"), path, "ol")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    counts = [len(_frame_labels(r.path)) for r in results]
    assert sum(counts) == 900, (
        f"the pieces exported {counts} = {sum(counts)} frames instead of tiling the source's "
        f"900 -- a frame was lost or duplicated at a time split")


def test_several_interior_time_splits_tile_the_shot_they_divide(adapter, tmp_path):
    """[FORK] P5's frame-conservation test, kept but re-scoped to the GUARDED shot.

    The shot's own end is a scene cut, so it legitimately loses a guard there. Everything inside
    it must still tile exactly: an interior time boundary is arithmetic, not an estimate, and a
    frame lost at one would be an unintended omission.
    """
    path = _make_three_shots(tmp_path / "tiles.mp4", [620, 300], rate="30000/1001")
    source, plan = _plan_for(adapter, path, minimum=5.0, maximum=6.0)
    pieces = [s for s in plan.segments if s.shot_index == 1]
    assert len(pieces) >= 3, [s.boundary_kind for s in plan.segments]
    # Positive control: the interior boundaries must really be time splits.
    assert [p.boundary_kind for p in pieces[:-1]] == ["time"] * (len(pieces) - 1)
    for first, second in zip(pieces, pieces[1:]):
        assert second.start == first.end, "an interior time split opened a gap"

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "tiles_out"), path, "tl")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    own = [len(_frame_labels(r.path)) for r in results[:len(pieces)]]

    shot_one = plan.shots[0]
    expected = sum(1 for t in _source_frame_times(path)
                   if shot_one.start - 1e-9 <= t < shot_one.end - 1e-9)
    assert sum(own) == expected, (
        f"the {len(pieces)} pieces exported {own} = {sum(own)} frames, but the guarded shot "
        f"[{shot_one.start}, {shot_one.end}) holds {expected}")


def test_a_maximum_duration_split_that_ends_on_a_cut_keeps_the_next_shot_out(adapter, tmp_path):
    """[FORK] P5, closing P4-2, re-measured under the guard.

    A clip whose START is a time split and whose END is a scene cut was the shape that leaked the
    next shot's first frame. The guard now puts 200 ms between them, but the assertion stays
    pixel-level rather than being assumed safe by arithmetic.
    """
    path = _make_three_shots(tmp_path / "split.mp4", [451, 300])
    _source, plan = _plan_for(adapter, path, minimum=5.0, maximum=7.0)
    kinds = [s.boundary_kind for s in plan.segments]
    assert kinds[:2] == ["time", "time"], kinds

    _plan2, labelled = _cut_and_label(adapter, path, tmp_path, "split",
                                      minimum=5.0, maximum=7.0)
    profile, _counts = _shot_profile(path, [451, 300])
    for position, (_text, result) in enumerate(labelled, start=1):
        shots_seen = {s for s in _classify(_frame_means(result.path), profile) if s >= 0}
        assert len(shots_seen) == 1, (
            f"clip {position} holds frames of shots {sorted(shots_seen)}")


# ------------------------------------------------------------------ container edge cases

@pytest.mark.parametrize("offset", [5.0, 12.5, 30.0])
def test_a_source_whose_timestamps_do_not_start_at_zero_needs_no_special_handling(
        adapter, tmp_path, offset):
    """[FORK] P6 **replaces** the FIX A tests, and records why they are gone.

    P3's FIX A existed because refinement read candidates under `-copyts` (container time) and
    compared them against a window in planner time, so at a 5-second origin every refinement
    silently found nothing. Removing refinement removes the mixed axis: the coarse pass already
    reports planner time, and there is no second reading to convert.

    The property to assert is therefore no longer "the origin probe ran" but "the origin does not
    matter". Measured against the zero-origin result on the same content: identical, frame for
    frame.
    """
    plain = _make_frame_exact_cut(tmp_path / "zero.mp4", "30", 92, 98)
    shifted = _make_offset_cut(tmp_path / f"origin_{offset}.mp4", "30", 92, 98, offset)

    _p1, base = _cut_and_label(adapter, plain, tmp_path, f"base_{offset}")
    _p2, moved = _cut_and_label(adapter, shifted, tmp_path, f"moved_{offset}")

    assert [t for t, _r in moved] == [t for t, _r in base], (
        f"a {offset}s timestamp origin changed the exported frames: "
        f"{[len(t) for t, _r in moved]} against {[len(t) for t, _r in base]}")
    first, second = moved[0][0], moved[1][0]
    assert "B" not in first and "A" not in second


def test_an_mp4_edit_list_does_not_move_the_guard(adapter, tmp_path):
    """An edit list delays presentation without changing the samples, so the clips must match the
    plain file's exactly -- the same control as the timestamp-origin test, a different mechanism."""
    plain = _make_frame_exact_cut(tmp_path / "plain_el.mp4", "30", 92, 98)
    delayed = _make_edit_list_cut(tmp_path / "editlist.mp4", "30", 92, 98, 0.5)

    _p1, base = _cut_and_label(adapter, plain, tmp_path, "el_base")
    _p2, shifted = _cut_and_label(adapter, delayed, tmp_path, "el_shifted")
    assert [t for t, _r in shifted] == [t for t, _r in base], (
        f"an edit list changed the exported frames: {[len(t) for t, _r in shifted]} against "
        f"{[len(t) for t, _r in base]}")


def test_a_genuinely_variable_frame_rate_source_still_keeps_its_shots_apart(adapter, tmp_path):
    """Three shots authored at 30, 15 and 60 fps in one file.

    The provenance property holds. What this test deliberately does NOT assert is frame
    conservation on the 60 fps passage: the export re-encodes a VFR source at the container's
    nominal rate, which is pre-existing behaviour -- measured identical on stock main, on the
    P5-S1 candidate and here -- and is recorded as a known limitation rather than pinned as if
    it were intended.
    """
    path = _make_vfr_source(tmp_path / "vfr.mp4", [90, 90, 90], [30, 15, 60])
    source, plan = _plan_for(adapter, path, minimum=1.0)
    assert plan.detected_scene_count == 2, plan.detected_scene_count

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "vfr_out"), path, "vfr")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]

    profile, _counts = _shot_profile(path, [90, 90, 90])
    for position, result in enumerate(results, start=1):
        shots_seen = {s for s in _classify(_frame_means(result.path), profile) if s >= 0}
        assert len(shots_seen) == 1, (
            f"VFR clip {position} carries frames of shots {sorted(shots_seen)}")


def test_two_hard_cuts_closer_than_two_guards_are_handled_without_a_bad_clip(adapter, tmp_path):
    """A five-frame shot between two real cuts: the guards consume it entirely.

    It must be skipped, its neighbours must not be merged across either cut, and both exported
    clips must still come from exactly one shot.
    """
    path = _make_three_shots(tmp_path / "near.mp4", [48, 5, 187])
    source, plan = _plan_for(adapter, path, minimum=1.0)
    assert plan.detected_scene_count == 2, (
        f"the coarse pass reported {plan.detected_scene_count} cut(s), so this fixture cannot "
        f"exercise overlapping guards")
    middle = plan.shots[1]
    assert not middle.exported and middle.duration == 0.0, middle

    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "near_out"), path, "nr")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    profile, _counts = _shot_profile(path, [48, 5, 187])
    for position, result in enumerate(results, start=1):
        shots_seen = {s for s in _classify(_frame_means(result.path), profile) if s >= 0}
        assert len(shots_seen) == 1, (
            f"clip {position} carries frames of shots {sorted(shots_seen)}")


# ------------------------------------------------------------------ encoder and audio paths

@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
@pytest.mark.parametrize("remove_audio", [True, False])
def test_the_guard_is_identical_on_both_encoders_and_both_audio_settings(
        adapter, tmp_path, encoder, remove_audio):
    """The boundary is a planning decision, so neither the encoder nor Remove Audio may move it.

    NVENC is skipped rather than failed where no capable GPU is present -- its absence is an
    environment fact, not a defect in the plan.
    """
    path = _make_video(tmp_path / "av.mp4", [4, 4], with_audio=True, fps=30, size=_SIZE)
    name = f"av_{encoder}_{int(remove_audio)}"
    source, plan = _plan_for(adapter, path, minimum=1.0)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / name), path, name)
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=remove_audio, encoder=encoder)
    if encoder == 'h264_nvenc' and not all(r.ok for r in results):
        pytest.skip("no usable NVENC encoder on this machine")
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]

    by_index = {s.index: s for s in plan.segments}
    for result in results:
        kinds = [s.get('codec_type') for s in _streams(result.path)]
        if remove_audio:
            assert 'audio' not in kinds, "`-an` must drop the stream itself, not just the signal"
        else:
            assert 'audio' in kinds, "the audio stream was requested and is missing"
        # The boundary is a PLANNING decision: whatever the encoder and whatever happens to the
        # audio, the clip must still be the planned length. Recorded per combination so the four
        # runs can be compared against each other in the evidence.
        planned = by_index[result.index].duration
        assert abs(_duration(result.path) - planned) < 0.25, (
            f"{encoder}/remove_audio={remove_audio}: clip {result.index} is "
            f"{_duration(result.path):.3f}s against a planned {planned:.3f}s")

    # And the guard really was applied at the one detected cut, on every combination.
    assert plan.detected_scene_count == 1, plan.detected_scene_count
    assert plan.guard_discarded == pytest.approx(2 * rc.SCENE_GUARD_SECONDS, abs=1e-9)


# ------------------------------------------------------------------ cost

def test_scene_detection_runs_one_ffmpeg_child_whatever_the_cut_count(adapter, tmp_path):
    """[FORK] P6 **replaces** the two FIX C tests, which bounded the per-cut refinement decode.

    There is no per-cut decode to bound now, so the thing to measure is that there is none. Two
    sources, one with a single cut and one with eleven, must cost the same number of children.
    Measured by counting real subprocess launches, not by timing.
    """
    launches = []
    original = adapter["_run_media_command"]

    def counting(cmd, timeout, lifecycle=None):
        launches.append(list(cmd))
        return original(cmd, timeout)

    counts = {}
    for name, shots in (("one", [60, 60]), ("many", [30] * 12)):
        path = _make_three_shots(tmp_path / f"cost_{name}.mp4", shots)
        launches.clear()
        adapter["_run_media_command"] = counting
        try:
            scenes = adapter["detect_scenes"](path, 0.27)
        finally:
            adapter["_run_media_command"] = original
        assert scenes.succeeded, scenes.error
        counts[name] = (len(launches), len(scenes.times))

    assert counts["many"][1] > counts["one"][1], (
        f"the twelve-shot fixture reported {counts['many'][1]} cuts against "
        f"{counts['one'][1]}, so this cannot measure cost against cut count")
    assert counts["one"][0] == counts["many"][0] == 1, (
        f"scene detection launched {counts['one'][0]} child(ren) for "
        f"{counts['one'][1]} cut(s) and {counts['many'][0]} for {counts['many'][1]} -- the cost "
        f"must not scale with the number of cuts")
