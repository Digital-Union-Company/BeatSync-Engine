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
import dataclasses
import hashlib
import json
import math
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


# =============================================================================================
# [FORK] Digital-Union (frame-boundary P8): variable-frame-rate preservation
#
# P7 established that exporting a genuinely variable-rate source re-timed it to the container's
# nominal `r_frame_rate` and threw away the frames between the grid points -- 38 of the 78 in the
# measured interval -- on stock main, on P5-S1 and on P6 alike. The export now states
# `-fps_mode:v passthrough` and `-enc_time_base:v demux`; these are the behavioural tests for it.
#
# The identity used here is each frame's own PRESENTATION TIMESTAMP, read as integer time-base
# units. That is an exact per-frame identifier -- no two source frames share one -- so comparing
# the clip's timestamps against the source's proves membership, ordering, spacing and non-collapse
# in a single assertion, which a frame COUNT alone cannot: passthrough without the encoder time
# base produced the right count with 38 frames sharing their predecessor's timestamp.
# =============================================================================================

#: Represents 1/60, 1/30, 1/15, 1/12 and 1/10 exactly, which is what lets an authored variable
#: timeline be an ORACLE rather than a second approximation. 23.976 and 29.97 do not divide it and
#: are authored as ordinary constant-rate sources instead.
_P8_TIMESCALE = 15360

#: One AAC frame at 48 kHz. The audio bound can only land on a frame boundary, so this is the
#: documented quantum within which the two streams are allowed to disagree. Measured on the P8
#: fixtures the skew is exactly 0.0; the tolerance states a contract rather than a coincidence.
_P8_AUDIO_QUANTUM = 1024 / 48000


def _p8_time_base(path):
    """The video stream's time base as `(num, den)` -- exact integers, never a rounded float."""
    result = _run([FFPROBE, '-v', 'error', '-select_streams', 'v:0',
                   '-show_entries', 'stream=time_base', '-of', 'csv=p=0', str(path)])
    assert result.returncode == 0, result.stderr[-1000:]
    text = (result.stdout or "").strip().splitlines()[0].strip()
    assert "/" in text, f"no time base for {path}: {text!r}"
    num, den = text.split("/", 1)
    return int(num), int(den)


def _p8_frame_pts(path):
    """Every frame's presentation time in seconds, ascending, from INTEGER time-base units.

    Deliberately not FFprobe's `pts_time`: that is already rounded to six decimals, so
    differencing it turns a perfectly regular 1/60 grid into an alternating 0.016666 / 0.016667
    pair and "distinct spacings" can no longer tell real jitter from print rounding. Sorting is
    what recovers PRESENTATION order -- `-show_frames` emits DECODE order, and on a long-GOP
    B-frame source the two differ.
    """
    num, den = _p8_time_base(path)
    result = _run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                   '-show_entries', 'frame=pts', '-of', 'csv=p=0', str(path)], timeout=600)
    assert result.returncode == 0, result.stderr[-1000:]
    units = []
    for line in (result.stdout or "").splitlines():
        field = line.strip().split(",")[0].strip()
        if not field:
            continue
        try:
            units.append(int(field))
        except ValueError:
            continue
    units.sort()
    return [unit * num / den for unit in units]


def _p8_planner_pts(path):
    """Frame presentation times on the PLANNER axis, where the file's first frame is 0.0.

    FFprobe answers on the CONTAINER axis, which begins at the stream's own `start_time` and need
    not be zero; an MP4 edit list delays presentation without moving `start_time` at all.
    """
    times = _p8_frame_pts(path)
    origin = times[0] if times else 0.0
    return [t - origin for t in times]


def _p8_expected(source_pts, start, duration):
    """The source frames of the half-open planner interval `[start, start + duration)`.

    Returned RELATIVE to the first frame in the interval, which is the axis the exported clip is
    on after `setpts=PTS-STARTPTS`.
    """
    inside = [t for t in source_pts if start - 1e-9 <= t < start + duration - 1e-9]
    return [t - inside[0] for t in inside] if inside else []


def _p8_assert_preserved(clip, expected, label, quantum):
    """The whole preservation contract, in one place: count, order, spacing, no collapse.

    `quantum` is the output container's own time-base step. Timestamps are compared within half of
    it because that is the finest distinction the container can store -- anything tighter would be
    asserting against rounding, and anything looser would stop detecting a one-frame shift.
    """
    actual = _p8_planner_pts(clip)
    assert len(actual) == len(expected), (
        f"{label}: exported {len(actual)} frames, expected {len(expected)}")
    assert len(set(actual)) == len(actual), (
        f"{label}: {len(actual) - len(set(actual))} frames share a timestamp with another")
    assert all(b > a for a, b in zip(actual, actual[1:])), (
        f"{label}: timestamps are not strictly increasing")
    worst = max((abs(a - e) for a, e in zip(actual, expected)), default=0.0)
    assert worst <= quantum / 2 + 1e-12, (
        f"{label}: a frame moved by {worst:.9f} s, more than half the "
        f"{quantum:.9f} s container quantum")


def _p8_quantum(path):
    num, den = _p8_time_base(path)
    return num / den


def _p8_ms(value):
    """Floor to the millisecond, which is the only kind of start the planner ever emits.

    `build_segment_command` formats `-ss` with three decimals and has since P1, so a start carrying
    sub-millisecond precision is rounded -- and if it rounds UP past the frame it meant to open on,
    that frame is seeked past. A test that picks a start straight off a frame's own timestamp will
    see that: measured at 23.976 fps, start 2.2105417 became `-ss 2.211` and exported 47 frames
    instead of 48, **identically on frozen main, on P6 and on this candidate**. Flooring reproduces
    what `plan_segments` does (`_quantize_floor` / `_quantize_ceil`), so the interval under test is
    one the product can actually ask for.
    """
    return math.floor(value * 1000) / 1000.0


def _p8_vfr_setpts(blocks, timescale=_P8_TIMESCALE):
    """`settb` plus a nested-`if` `setpts` placing frame N at an exact integer unit count.

    `blocks` is `[(frame_count, step_units), ...]`. Authoring in units rather than float seconds is
    load-bearing, not fussiness: the obvious float spelling was measured to jitter individual
    frames by one 1/60 step, so four frames in every fifteen of a 60 fps passage collided with
    their predecessor -- the MP4 muxer stored that as a 1-unit delta followed by a 511-unit one,
    because it cannot write a zero duration. A fixture carrying the very defect under test cannot
    be the oracle for it.
    """
    bounds, at, time = [], 0, 0
    for count, step in blocks:
        bounds.append((at, at + count, time, step))
        at += count
        time += count * step
    expression = str(bounds[-1][2])
    for start, end, base, step in reversed(bounds):
        expression = "if(lt(N,%d),%d+(N-%d)*%d,%s)" % (end, base, start, step, expression)
    # The expression is single-quoted because it contains commas, which otherwise separate
    # FILTERS: unquoted, `setpts=if(lt(N,108),...)` makes FFmpeg look for a filter called `108)`.
    return "settb=1/%d,setpts='%s'" % (timescale, expression)


def _make_vfr_exact(path, blocks, with_audio=False, timescale=_P8_TIMESCALE, container=None):
    """A genuinely variable-rate source whose timestamps are exact integers of `1/timescale`.

    `blocks` is `[(frame_count, step_units), ...]`; each block is one solid shot, so the shots are
    both distinguishable by luminance and authored at different rates.
    """
    path = str(path)
    inputs, chains = [], []
    for index, (count, _step) in enumerate(blocks):
        inputs.extend(['-f', 'lavfi', '-i',
                       "color=%s:s=%s:r=60" % (_COLOURS[index % len(_COLOURS)], _SIZE)])
        chains.append("[%d:v]trim=end_frame=%d,setpts=PTS-STARTPTS[s%d];" % (index, count, index))
    joins = "".join("[s%d]" % i for i in range(len(blocks)))
    seconds = sum(count * step for count, step in blocks) / timescale

    command = [FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', *inputs]
    if with_audio:
        command.extend(['-f', 'lavfi', '-i',
                        "sine=frequency=440:sample_rate=48000:duration=%.6f" % seconds])
    command.extend([
        '-filter_complex',
        "".join(chains) + "%sconcat=n=%d:v=1:a=0[v];[v]%s[out]" % (
            joins, len(blocks), _p8_vfr_setpts(blocks, timescale)),
        '-map', '[out]'])
    if with_audio:
        command.extend(['-map', '%d:a:0' % len(blocks), '-c:a', 'aac', '-b:a', '128k',
                        '-ar', '48000'])
    command.extend(['-fps_mode', 'passthrough', '-enc_time_base:v', '1:%d' % timescale,
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '0',
                    '-pix_fmt', 'yuv420p', '-g', '30'])
    if container != 'matroska':
        command.extend(['-video_track_timescale', str(timescale)])
    command.append(path)
    result = _run(command, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]

    # The fixture must actually BE variable and must carry no duplicate timestamp of its own, or
    # it could prove nothing about either property.
    times = _p8_planner_pts(path)
    authored = sum(count for count, _ in blocks)
    assert len(times) == authored, (
        "the fixture decoded %d frames, not the %d it was authored with" % (len(times), authored))
    assert len(set(times)) == len(times), "the fixture itself has duplicate timestamps"
    spacings = {round(b - a, 6) for a, b in zip(times, times[1:])}
    assert len(spacings) >= len({step for _, step in blocks}), (
        "the authored timeline is not variable as intended: %s" % sorted(spacings))
    return path


def _p8_one_segment_plan(probe_source, start, duration):
    """A one-segment plan for exactly `[start, start + duration)`.

    Built directly rather than detected, so the planner, the coarse detector and the 0.20 s scene
    guard are all out of the picture: what gets measured is the export, which is the only thing P8
    changed. P6's boundary policy is verified separately and must not be re-litigated here.
    """
    plan = rc.plan_segments(probe_source.duration, [], 0.05, 10_000.0, 50)
    template = plan.segments[0]
    segment = dataclasses.replace(template, index=1, start=start,
                                  end=round(start + duration, 6))
    return dataclasses.replace(plan, segments=(segment,))


def _p8_export_one(adapter, path, tmp_path, name, start, duration,
                   encoder='libx264', remove_audio=True, naming=None):
    """Export exactly one interval through the REAL adapter and return its clip path.

    [FORK] R4-C: `naming` is threaded into BOTH the run-directory preparation and the export, so
    a caller measuring the synthetic mode measures the whole shipped path -- the folder name, the
    staging name and the published name -- rather than only the last of the three.
    """
    probe = adapter["probe_source"](path)
    assert probe.ok, probe.error
    plan = _p8_one_segment_plan(probe.source, start, duration)
    run_dir, error = adapter["prepare_run_directory"](str(tmp_path / name), path, name,
                                                      naming=naming)
    assert not error, error
    results = adapter["export_segments"](source=probe.source, plan=plan, run_directory=run_dir,
                                         remove_audio=remove_audio, encoder=encoder,
                                         naming=naming)
    if encoder == 'h264_nvenc' and not all(r.ok for r in results):
        pytest.skip("no usable NVENC encoder on this machine")
    assert all(r.ok for r in results), [r.error for r in results if not r.ok]
    return results[0].path


#: The P7 source: 108 frames at 30 fps, then 84 at 15, then 78 at 60. 108/30 + 84/15 = 9.200
#: exactly, so the 60 fps passage begins on the interval P7 measured and its 78 frames are the
#: same 78 frames here.
_P8_VFR_BLOCKS = [(108, 512), (84, 1024), (78, 256)]
_P8_VFR_START, _P8_VFR_DURATION = 9.200, 1.300


def test_a_variable_rate_export_keeps_every_frame_of_the_requested_interval(adapter, tmp_path):
    """[FORK] P8. The headline: the exact 78 frames, at the source's own times.

    This is the measurement P7 reported as a severe pre-existing defect. Before the correction the
    same interval exported 40 frames on P6 and 39 on stock main, re-timed to the container's
    nominal 1/30 grid.
    """
    path = _make_vfr_exact(tmp_path / "vfr270.mp4", _P8_VFR_BLOCKS, with_audio=True)
    source_pts = _p8_planner_pts(path)
    assert len(source_pts) == 270, len(source_pts)

    expected = _p8_expected(source_pts, _P8_VFR_START, _P8_VFR_DURATION)
    assert len(expected) == 78, (
        f"the fixture holds {len(expected)} frames in [9.200, 10.500), not the 78 P7 measured")

    clip = _p8_export_one(adapter, path, tmp_path, "vfr_exact",
                          _P8_VFR_START, _P8_VFR_DURATION)
    _p8_assert_preserved(clip, expected, "60 fps passage", _p8_quantum(clip))


def test_a_variable_rate_export_reproduces_the_sources_own_spacing_not_a_nominal_rate(
        adapter, tmp_path):
    """The 60 fps passage must come out spaced 1/60, not at the container's nominal 1/30.

    Asserted as the multiset of adjacent differences rather than as a single average, because an
    average cannot tell "every frame 1/60 apart" from "half the frames, 1/30 apart".
    """
    path = _make_vfr_exact(tmp_path / "vfr270_s.mp4", _P8_VFR_BLOCKS)
    expected = _p8_expected(_p8_planner_pts(path), _P8_VFR_START, _P8_VFR_DURATION)
    clip = _p8_export_one(adapter, path, tmp_path, "vfr_spacing",
                          _P8_VFR_START, _P8_VFR_DURATION)
    actual = _p8_planner_pts(clip)

    wanted = sorted(round(b - a, 5) for a, b in zip(expected, expected[1:]))
    got = sorted(round(b - a, 5) for a, b in zip(actual, actual[1:]))
    assert got == wanted, (
        f"spacing changed: {sorted(set(got))} against the source's {sorted(set(wanted))}")
    assert sorted(set(got)) == [round(1 / 60, 5)], sorted(set(got))


def test_a_variable_rate_export_keeps_audio_and_video_the_same_length(adapter, tmp_path):
    """The A/V skew P7 recorded: 1.333333 s of video against 1.300000 s of audio.

    The video ran long because 40 frames were written on a 1/30 grid. Both streams must now
    describe the interval the planner asked for, within one audio frame.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_av.mp4", _P8_VFR_BLOCKS, with_audio=True)
    clip = _p8_export_one(adapter, path, tmp_path, "vfr_av",
                          _P8_VFR_START, _P8_VFR_DURATION, remove_audio=False)

    kinds = [s.get('codec_type') for s in _streams(clip)]
    assert 'audio' in kinds and 'video' in kinds, kinds
    result = _run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,duration',
                   '-of', 'json', str(clip)])
    assert result.returncode == 0, result.stderr[-1000:]
    lengths = {s['codec_type']: float(s['duration'])
               for s in json.loads(result.stdout)['streams'] if s.get('duration')}
    assert set(lengths) == {'video', 'audio'}, lengths
    assert abs(lengths['video'] - lengths['audio']) <= _P8_AUDIO_QUANTUM, lengths
    assert abs(lengths['video'] - _P8_VFR_DURATION) <= _P8_AUDIO_QUANTUM, lengths


def test_alternating_fast_and_slow_passages_are_every_one_preserved(adapter, tmp_path):
    """Six rate changes inside one requested interval, 60/10 fps alternating.

    A correction that only held when the whole interval shared one rate would pass the 78-frame
    case and fail here, so this is where the "not forced to the average rate" clause is actually
    tested: the average over this interval is neither 60 nor 10.
    """
    blocks = [(40, 256), (20, 1536), (40, 256), (20, 1536), (40, 256), (20, 1536)]
    path = _make_vfr_exact(tmp_path / "vfr_alt.mp4", blocks)
    source_pts = _p8_planner_pts(path)
    assert len(source_pts) == 180, len(source_pts)

    duration = round(source_pts[-1] + 1 / 60, 6)
    expected = _p8_expected(source_pts, 0.0, duration)
    assert len(expected) == 180, len(expected)

    clip = _p8_export_one(adapter, path, tmp_path, "vfr_alt_out", 0.0, duration)
    _p8_assert_preserved(clip, expected, "alternating 60/10 fps", _p8_quantum(clip))
    assert sorted({round(b - a, 5) for a, b in zip(_p8_planner_pts(clip),
                                                   _p8_planner_pts(clip)[1:])}) == \
        sorted({round(1 / 60, 5), round(1 / 10, 5)})


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc"])
@pytest.mark.parametrize("remove_audio", [True, False])
def test_the_variable_rate_preservation_holds_on_both_encoders(adapter, tmp_path, encoder,
                                                               remove_audio):
    """Timing is decided before the encoder, so neither encoder nor Remove Audio may move it.

    NVENC is skipped rather than failed where no capable GPU is present -- its absence is an
    environment fact, not a defect.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_enc.mp4", _P8_VFR_BLOCKS, with_audio=True)
    expected = _p8_expected(_p8_planner_pts(path), _P8_VFR_START, _P8_VFR_DURATION)
    name = f"vfr_{encoder}_{int(remove_audio)}"
    clip = _p8_export_one(adapter, path, tmp_path, name, _P8_VFR_START, _P8_VFR_DURATION,
                          encoder=encoder, remove_audio=remove_audio)
    _p8_assert_preserved(clip, expected, f"{encoder}/remove_audio={remove_audio}",
                         _p8_quantum(clip))


def test_a_matroska_variable_rate_source_is_preserved_within_its_own_precision(adapter, tmp_path):
    """Matroska's millisecond time base cannot hold 1/60 exactly, which is the point.

    `demux` asks for the SOURCE's time base, so the clip is as precise as the source was and no
    more. A container whose own grid is coarser than the content's spacing is where a
    timestamp-preserving export has the least room, so it is where it has to be measured -- and
    the frames must still all be there, in order, none sharing a timestamp.
    """
    path = _make_vfr_exact(tmp_path / "vfr270.mkv", _P8_VFR_BLOCKS, container='matroska')
    source_pts = _p8_planner_pts(path)
    assert len(source_pts) == 270, len(source_pts)
    expected = _p8_expected(source_pts, _P8_VFR_START, _P8_VFR_DURATION)
    assert len(expected) == 78, len(expected)

    clip = _p8_export_one(adapter, path, tmp_path, "mkv_vfr", _P8_VFR_START, _P8_VFR_DURATION)
    _p8_assert_preserved(clip, expected, "matroska VFR", max(_p8_quantum(clip),
                                                             _p8_quantum(path)))


@pytest.mark.parametrize("rate,frames", [("24000/1001", 160), ("25", 165), ("30000/1001", 200),
                                         ("30", 200), ("60", 400)])
def test_a_constant_rate_export_keeps_exactly_the_frames_it_always_did(adapter, tmp_path, rate,
                                                                       frames):
    """The CFR non-regression: same membership, same spacing, no duplicated frame.

    A rate-preserving option pair must not make a constant-rate source any different. 23.976 and
    29.97 are in here deliberately -- their frame interval is not a round number of any sensible
    time base, so they are where a time-base change would show up first.
    """
    tag = rate.replace("/", "-")
    path = _make_three_shots(tmp_path / f"cfr_{tag}.mp4", [frames], rate=rate)
    source_pts = _p8_planner_pts(path)
    assert len(source_pts) == frames, len(source_pts)

    start, duration = _p8_ms(source_pts[len(source_pts) // 3]), 2.0
    expected = _p8_expected(source_pts, start, duration)
    assert len(expected) > 20, len(expected)

    clip = _p8_export_one(adapter, path, tmp_path, f"cfr_out_{tag}", start, duration)
    _p8_assert_preserved(clip, expected, f"{rate} CFR", _p8_quantum(clip))

    spacings = {round(b - a, 5) for a, b in zip(_p8_planner_pts(clip), _p8_planner_pts(clip)[1:])}
    assert len(spacings) <= 2, f"a constant-rate clip came out unevenly spaced: {spacings}"


def test_a_long_gop_b_frame_source_is_preserved_from_a_non_keyframe_start(adapter, tmp_path):
    """GOP 250 with three B-frames: decode order differs from presentation order throughout.

    The requested start deliberately does not land on a keyframe, so FFmpeg must seek back and
    decode forward -- the case where a timestamp-preserving export could most easily emit the
    frames in the wrong order or keep the discarded ones.
    """
    path = str(tmp_path / "longgop.mp4")
    result = _run([
        FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
        '-f', 'lavfi', '-i', "color=black:s=%s:r=30" % _SIZE,
        '-f', 'lavfi', '-i', "color=white:s=%s:r=30" % _SIZE,
        '-filter_complex',
        "[0:v]trim=end_frame=182,setpts=PTS-STARTPTS[a];"
        "[1:v]trim=end_frame=239,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]",
        '-map', '[v]', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '0',
        '-pix_fmt', 'yuv420p', '-g', '250', '-bf', '3',
        '-x264-params', 'bframes=3:b-pyramid=2', '-r', '30', path], timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]

    source_pts = _p8_planner_pts(path)
    assert len(source_pts) == 421, len(source_pts)
    start = _p8_ms(source_pts[137])    # mid-GOP, not a keyframe
    expected = _p8_expected(source_pts, start, 1.5)
    clip = _p8_export_one(adapter, path, tmp_path, "longgop_out", start, 1.5)
    _p8_assert_preserved(clip, expected, "long GOP, B-pyramid", _p8_quantum(clip))


def test_a_nonzero_timestamp_origin_exports_the_same_frames_as_a_zero_origin_one(adapter,
                                                                                 tmp_path):
    """The planner axis is the file's first frame, whatever the container says it is.

    An output timestamp offset must change nothing about membership -- the same control P6 ran for
    the boundary, re-run for the timing options because `demux` now reads the source's time base.
    """
    plain = _make_three_shots(tmp_path / "origin_plain.mp4", [200], rate="30")
    shifted = str(tmp_path / "origin_shifted.mp4")
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', '-i', plain,
                   '-map', '0:v:0', '-c:v', 'copy', '-output_ts_offset', '7.25', shifted])
    assert result.returncode == 0, result.stderr[-2000:]

    for name, path in (("plain", plain), ("shifted", shifted)):
        source_pts = _p8_planner_pts(path)
        expected = _p8_expected(source_pts, 2.0, 2.0)
        assert len(expected) == 60, (name, len(expected))
        clip = _p8_export_one(adapter, path, tmp_path, f"origin_out_{name}", 2.0, 2.0)
        _p8_assert_preserved(clip, expected, f"origin {name}", _p8_quantum(clip))


def test_an_mp4_edit_list_exports_the_same_frames_as_the_plain_file(adapter, tmp_path):
    """An edit list delays presentation without moving `start_time`, so it is a second,
    independent way for the container axis to disagree with the planner axis."""
    plain = _make_three_shots(tmp_path / "el_plain.mp4", [200], rate="30")
    delayed = str(tmp_path / "el_delayed.mp4")
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error',
                   '-itsoffset', '0.5', '-i', plain, '-map', '0:v:0', '-c:v', 'copy',
                   '-movflags', '+faststart', delayed])
    assert result.returncode == 0, result.stderr[-2000:]

    counts = []
    for name, path in (("plain", plain), ("delayed", delayed)):
        expected = _p8_expected(_p8_planner_pts(path), 2.0, 2.0)
        clip = _p8_export_one(adapter, path, tmp_path, f"el_out_{name}", 2.0, 2.0)
        _p8_assert_preserved(clip, expected, f"edit list {name}", _p8_quantum(clip))
        counts.append(len(expected))
    assert counts[0] == counts[1], counts


def test_a_short_segment_at_the_minimum_threshold_is_still_exact(adapter, tmp_path):
    """The shortest clip the planner will ever ask for, on a variable-rate source.

    Short intervals are where an off-by-one in frame membership is proportionally largest and
    where a muxer's own start-up quantisation has the least content to hide in.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_short.mp4", _P8_VFR_BLOCKS)
    source_pts = _p8_planner_pts(path)
    for name, (start, duration) in (("min_5", (9.200, 0.05)),
                                    ("tiny", (9.200, 1 / 60 + 1e-6)),
                                    ("slow", (3.600, 0.2))):
        expected = _p8_expected(source_pts, start, duration)
        assert expected, (name, "the interval holds no frame, so it proves nothing")
        clip = _p8_export_one(adapter, path, tmp_path, f"short_{name}", start, duration)
        _p8_assert_preserved(clip, expected, name, _p8_quantum(clip))


def test_negative_control_passthrough_without_the_encoder_time_base_collapses_timestamps(
        adapter, tmp_path):
    """The behavioural control for the second half of the fix, run through real FFmpeg.

    `-fps_mode:v passthrough` alone keeps all 78 frames, which is why a frame-count assertion
    would have called it a fix. It is not one: the encoder still quantises to a nominal-rate time
    base and 38 of those frames are handed their predecessor's timestamp. This asserts the defect
    is really there without the second option, so the passing test above is known to be measuring
    something.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_ctl.mp4", _P8_VFR_BLOCKS)
    expected = _p8_expected(_p8_planner_pts(path), _P8_VFR_START, _P8_VFR_DURATION)
    assert len(expected) == 78, len(expected)

    actual = _p8_without(adapter, path, str(tmp_path / "collapsed.mp4"), ['-enc_time_base:v'])
    assert len(actual) == len(expected), (
        "passthrough alone should still keep every frame; the control is not reproducing the "
        "measured case")
    collapsed = len(actual) - len(set(actual))
    assert collapsed > 0, (
        "without the encoder time base the timestamps must collapse -- they did not, so the "
        "positive test above may be passing for a reason other than the fix")


def _p8_without(adapter, path, destination, options, start=_P8_VFR_START,
                duration=_P8_VFR_DURATION):
    """Run the shipped command with `options` deleted, and return the clip's planner timestamps.

    Deleting from the REAL argv rather than hand-writing a command: a transcription could drift
    from what ships, and then the control would be measuring a command nobody runs.
    """
    argv = adapter["build_segment_command"](path, destination, start, duration,
                                            True, 'libx264', False)
    for option in options:
        at = argv.index(option)
        argv = argv[:at] + argv[at + 2:]
        assert option not in argv, option
    result = _run(argv, timeout=600)
    assert result.returncode == 0, result.stderr[-2000:]
    return _p8_planner_pts(destination)


def test_negative_control_the_pre_p8_command_drops_variable_rate_frames(adapter, tmp_path):
    """The defect P7 measured, reproduced from the shipped command by deleting both options.

    So the suite carries the before as well as the after: 40 of 78 frames, re-timed to the
    container's nominal 1/30 grid. If this ever stops losing frames, either FFmpeg changed or the
    fixture stopped being variable, and the passing test above would no longer mean what it says.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_ctl2.mp4", _P8_VFR_BLOCKS)
    expected = _p8_expected(_p8_planner_pts(path), _P8_VFR_START, _P8_VFR_DURATION)
    assert len(expected) == 78, len(expected)

    actual = _p8_without(adapter, path, str(tmp_path / "dropped.mp4"),
                         ['-fps_mode:v', '-enc_time_base:v'])
    assert len(actual) < len(expected), (
        f"the pre-P8 command exported {len(actual)} of {len(expected)} frames; it was measured at "
        f"40 of 78, so this control is no longer reproducing the defect")
    assert sorted({round(b - a, 5) for a, b in zip(actual, actual[1:])}) == [round(1 / 30, 5)], (
        "the pre-P8 command should re-time the clip to the nominal 1/30 grid")


def test_the_encoder_time_base_alone_happens_to_be_enough_which_is_why_both_are_stated(
        adapter, tmp_path):
    """The asymmetry between the two options, pinned so it is not rediscovered as a surprise.

    Measured: deleting `-fps_mode:v` and keeping `-enc_time_base:v demux` still preserves all 78
    frames at the source's spacing. `-fps_mode auto` evidently stops converting once the encoder
    time base matches the source. Deleting the time base and keeping passthrough does NOT work --
    that is the collapsed-timestamp control above.

    So on these fixtures only the time base is strictly load-bearing, and `passthrough` is stated
    anyway because `auto` is a heuristic over the muxer's capabilities and the stream's apparent
    rate, not a contract. This test exists so that reading is explicit: if a later FFmpeg makes
    `auto` convert again, the shipped command is already immune and this test simply flips to
    telling us the heuristic moved.
    """
    path = _make_vfr_exact(tmp_path / "vfr270_ctl3.mp4", _P8_VFR_BLOCKS)
    expected = _p8_expected(_p8_planner_pts(path), _P8_VFR_START, _P8_VFR_DURATION)
    actual = _p8_without(adapter, path, str(tmp_path / "auto_mode.mp4"), ['-fps_mode:v'])
    assert len(actual) == len(expected), (
        f"`-fps_mode auto` with the source time base exported {len(actual)} of {len(expected)} "
        f"frames. That is not a defect in the shipped command, which states `passthrough`, but the "
        f"recorded asymmetry no longer holds and the docstring in raw_cutter_export.py should be "
        f"corrected.")
    assert len(set(actual)) == len(actual), "auto collapsed timestamps"


# =============================================================================================
# [FORK] Digital-Union (Raw Cutter R4-C): synthetic output naming, measured against the binary
#
# The naming mode must change the output PATH and nothing else. The pure and seam tiers can show
# that the argv differs only in its last element; only the real binary can show that the frames,
# their presentation times, the geometry and the stream layout come out identical -- and that no
# second encode or extra pass was added to achieve it.
#
# Every test below is a PAIRED comparison: the same interval of the same fixture exported twice,
# once in each mode, with the legacy export as the control. An absolute assertion about the
# synthetic clip alone could be satisfied by an export that was wrong in both modes.
# =============================================================================================

#: 18 October 2026, matching the owner's worked examples. Fixed rather than read from a clock, so
#: the expected spelling is a constant and not something the test recomputes the way the code did.
_R4C_NAMING = rc.OutputNaming(synthetic=True, source_index=1, job_date="18102026")

#: [FORK] R4-C correction: source 7 of a frozen 1,200-source batch. This is the record shape the
#: job-wide width actually changes -- `File0007`, not `File007` -- and it is exercised against the
#: real binary because the width is decided in the name builder that feeds FFmpeg's output path.
_R4C_WIDE_NAMING = rc.OutputNaming(synthetic=True, source_index=7, job_date="18102026",
                                   source_total=1200)


def _r4c_geometry(path):
    """Resolution, sample aspect ratio, display aspect ratio, pixel format and codec."""
    result = _run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                   'stream=codec_name,width,height,sample_aspect_ratio,display_aspect_ratio,'
                   'pix_fmt', '-of', 'json', str(path)])
    assert result.returncode == 0, result.stderr[-1000:]
    streams = json.loads(result.stdout)['streams']
    assert len(streams) == 1, streams
    return streams[0]


def _r4c_decodes_cleanly(path):
    """Decode the whole clip and count the frames. A clip FFmpeg cannot read is not a clip."""
    result = _run([FFMPEG, '-nostdin', '-hide_banner', '-v', 'error', '-i', str(path),
                   '-f', 'null', '-'], timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert not result.stderr.strip(), f"decoding emitted errors: {result.stderr[-800:]}"
    return len(_p8_frame_pts(path))


def _r4c_pair(adapter, path, tmp_path, label, start, duration, **kw):
    """Export the SAME interval twice -- legacy naming, then synthetic. Returns both clip paths."""
    legacy = _p8_export_one(adapter, path, tmp_path, label + "_legacy", start, duration, **kw)
    synthetic = _p8_export_one(adapter, path, tmp_path, label + "_synthetic", start, duration,
                               naming=_R4C_NAMING, **kw)
    return legacy, synthetic


@needs_ffmpeg
def test_a_synthetic_clip_is_named_as_specified_and_leaks_no_basename(adapter, tmp_path):
    """The real filesystem, through the real adapter: folder, clip name, and nothing left behind.

    The source is deliberately given a distinctive basename, so a leak anywhere in the tree -- a
    stray staging artifact included -- fails here rather than being argued about.
    """
    source = _make_three_shots(tmp_path / "private_wedding_footage.mp4", [90, 90, 90])
    clip = _p8_export_one(adapter, source, tmp_path, "named", 0.5, 1.0, naming=_R4C_NAMING)

    run_dir = os.path.dirname(clip)
    assert os.path.basename(run_dir) == "RawCutter_Single_named"
    assert os.path.basename(clip) == "File001_18102026_001.mp4"
    listing = os.listdir(run_dir)
    assert listing == ["File001_18102026_001.mp4"], listing
    for name in [os.path.basename(run_dir)] + listing:
        assert "wedding" not in name.casefold(), name
        assert "private" not in name.casefold(), name
        assert "footage" not in name.casefold(), name


@needs_ffmpeg
def test_the_legacy_name_is_still_exactly_what_it_always_was(adapter, tmp_path):
    """The compatibility control, measured on the real filesystem rather than only as a string."""
    source = _make_three_shots(tmp_path / "private_wedding_footage.mp4", [90, 90, 90])
    clip = _p8_export_one(adapter, source, tmp_path, "unnamed", 0.5, 1.0)
    assert os.path.basename(os.path.dirname(clip)) == \
        "private_wedding_footage_raw_parts_unnamed"
    assert os.path.basename(clip) == "private_wedding_footage_part_001.mp4"


@needs_ffmpeg
def test_a_wide_batch_clip_is_named_for_the_frozen_total_and_is_otherwise_identical(
        adapter, tmp_path):
    """[FORK] R4-C correction, against the binary: source 7 of 1,200 is `File0007`.

    The width change happens in the builder that produces FFmpeg's output path, so this is the one
    assertion that the corrected name is what the encoder actually wrote -- staging file included
    -- rather than something computed beside it. Paired with the three-digit record so the only
    difference measured is the padding.
    """
    source = _make_three_shots(tmp_path / "private_wedding_footage.mp4", [90, 90, 90])
    before = (_sha256(source), os.path.getsize(source))
    narrow = _p8_export_one(adapter, source, tmp_path, "narrow", 0.5, 1.5,
                            naming=_R4C_NAMING)
    wide = _p8_export_one(adapter, source, tmp_path, "wide", 0.5, 1.5,
                          naming=_R4C_WIDE_NAMING)

    assert os.path.basename(narrow) == "File001_18102026_001.mp4"
    assert os.path.basename(wide) == "File0007_18102026_001.mp4"
    # The one encode wrote that name directly: nothing else survives in the run directory, so no
    # staging artifact and no post-publication rename of a differently padded name occurred.
    wide_dir = os.path.dirname(wide)
    assert os.listdir(wide_dir) == ["File0007_18102026_001.mp4"]
    for name in [os.path.basename(wide_dir)] + os.listdir(wide_dir):
        for token in ("wedding", "private", "footage"):
            assert token not in name.casefold(), name

    # Padding is the ONLY difference: same geometry, same frames, same presentation times.
    assert _r4c_geometry(wide) == _r4c_geometry(narrow)
    assert _p8_frame_pts(wide) == _p8_frame_pts(narrow)
    assert _r4c_decodes_cleanly(wide) == _r4c_decodes_cleanly(narrow) > 0
    # And neither export touched the source.
    assert (_sha256(source), os.path.getsize(source)) == before


@needs_ffmpeg
def test_a_wide_batch_clip_adds_no_second_encode_or_extra_pass(adapter, tmp_path):
    """The cost claim for the corrected path: one encode command, whose argv differs from the
    three-digit export's in nothing but the output path.

    Taken from the shipped `build_segment_command` with the two staging names the shipped builder
    produces, so this compares what the exporter would really run rather than a reconstruction.
    """
    source = _make_three_shots(tmp_path / "cfr.mp4", [90, 90, 90])
    names = [rc.staging_filename(source, 1, 1, "tok12345", naming=naming)
             for naming in (_R4C_NAMING, _R4C_WIDE_NAMING)]
    assert names[0] == ".File001_18102026_001.tok12345.part.mp4"
    assert names[1] == ".File0007_18102026_001.tok12345.part.mp4"

    argvs = [adapter["build_segment_command"](str(source), os.path.join(str(tmp_path), name),
                                              0.5, 1.0, True, 'libx264', False)
             for name in names]
    assert argvs[0][:-1] == argvs[1][:-1], "only the output path may differ"
    assert argvs[0].count('-i') == argvs[1].count('-i') == 1, "one input, one pass"
    assert os.path.basename(argvs[1][-1]) == names[1]
    # The corrected padding really is in the command the encoder is handed.
    assert "File0007" in argvs[1][-1] and "File0007" not in argvs[0][-1]


@needs_ffmpeg
def test_a_wide_batch_clip_keeps_variable_rate_timing_exactly(adapter, tmp_path):
    """The VFR guarantee has to survive the correction too: the P8 fixture's 78-frame interval,
    at the source's own times, identical between the two record widths."""
    source = _make_vfr_exact(tmp_path / "vfr_wide.mp4", _P8_VFR_BLOCKS)
    narrow = _p8_export_one(adapter, source, tmp_path, "vfr_narrow",
                            _P8_VFR_START, _P8_VFR_DURATION, naming=_R4C_NAMING)
    wide = _p8_export_one(adapter, source, tmp_path, "vfr_wide_out",
                          _P8_VFR_START, _P8_VFR_DURATION, naming=_R4C_WIDE_NAMING)
    assert os.path.basename(wide) == "File0007_18102026_001.mp4"
    assert _p8_planner_pts(wide) == _p8_planner_pts(narrow)
    assert len(_p8_frame_pts(wide)) == 78


@needs_ffmpeg
def test_a_synthetic_clip_decodes_cleanly_and_keeps_the_sources_geometry(adapter, tmp_path):
    """Resolution, SAR, DAR, pixel format and codec, against the legacy export of the same
    interval. `trim`/`setpts` say nothing about geometry and the naming mode says nothing at all,
    so the two clips must agree exactly."""
    source = _make_three_shots(tmp_path / "cfr.mp4", [90, 90, 90])
    legacy, synthetic = _r4c_pair(adapter, source, tmp_path, "geom", 0.5, 2.0)

    assert _r4c_geometry(synthetic) == _r4c_geometry(legacy)
    # And both agree with the SOURCE on every geometry field, which is the real claim: the clip
    # preserves the footage's shape. `codec_name` is excluded only because it is not geometry.
    origin = _r4c_geometry(source)
    for field in ('width', 'height', 'sample_aspect_ratio', 'display_aspect_ratio', 'pix_fmt'):
        assert _r4c_geometry(synthetic).get(field) == origin.get(field), field
    assert _r4c_decodes_cleanly(synthetic) == _r4c_decodes_cleanly(legacy) > 0


@needs_ffmpeg
def test_a_constant_rate_export_keeps_identical_frame_timing_in_both_modes(adapter, tmp_path):
    """CFR. Same frame count, same presentation times, to the container's own quantum."""
    source = _make_three_shots(tmp_path / "cfr_timing.mp4", [90, 90, 90])
    legacy, synthetic = _r4c_pair(adapter, source, tmp_path, "cfr", 0.5, 2.0)

    reference = _p8_planner_pts(legacy)
    assert len(reference) > 1, reference
    _p8_assert_preserved(synthetic, reference, "CFR under synthetic naming",
                         _p8_quantum(synthetic))
    assert sorted({round(b - a, 5) for a, b in zip(reference, reference[1:])}) == \
        sorted({round(b - a, 5) for a, b in zip(_p8_planner_pts(synthetic),
                                                _p8_planner_pts(synthetic)[1:])})


@needs_ffmpeg
def test_a_variable_rate_export_keeps_identical_frame_timing_in_both_modes(adapter, tmp_path):
    """VFR, on the P8 fixture that caught the original re-timing defect.

    This is the one that would actually break if the mode had reached the command: the P8
    `-fps_mode:v passthrough -enc_time_base:v demux` pair is what preserves these 78 frames, and
    an export that lost them would lose them here too.
    """
    source = _make_vfr_exact(tmp_path / "vfr_named.mp4", _P8_VFR_BLOCKS)
    expected = _p8_expected(_p8_planner_pts(source), _P8_VFR_START, _P8_VFR_DURATION)
    assert len(expected) == 78, len(expected)

    legacy, synthetic = _r4c_pair(adapter, source, tmp_path, "vfr",
                                  _P8_VFR_START, _P8_VFR_DURATION)
    _p8_assert_preserved(legacy, expected, "VFR control", _p8_quantum(legacy))
    _p8_assert_preserved(synthetic, expected, "VFR under synthetic naming",
                         _p8_quantum(synthetic))
    assert _p8_planner_pts(synthetic) == _p8_planner_pts(legacy)
    assert sorted({round(b - a, 5) for a, b in zip(_p8_planner_pts(synthetic),
                                                   _p8_planner_pts(synthetic)[1:])}) == \
        [round(1 / 60, 5)]


@needs_ffmpeg
def test_keeping_the_audio_works_identically_under_synthetic_naming(adapter, tmp_path):
    """The audio path is untouched by the mode, so both clips must carry the same streams and the
    same two lengths."""
    source = _make_vfr_exact(tmp_path / "vfr_named_av.mp4", _P8_VFR_BLOCKS, with_audio=True)
    legacy, synthetic = _r4c_pair(adapter, source, tmp_path, "vfr_av",
                                  _P8_VFR_START, _P8_VFR_DURATION, remove_audio=False)

    def lengths(path):
        result = _run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,duration',
                       '-of', 'json', str(path)])
        assert result.returncode == 0, result.stderr[-1000:]
        return {s['codec_type']: round(float(s['duration']), 3)
                for s in json.loads(result.stdout)['streams'] if s.get('duration')}

    assert set(lengths(synthetic)) == {'video', 'audio'}
    assert lengths(synthetic) == lengths(legacy)


@needs_ffmpeg
def test_removing_the_audio_still_removes_the_stream_under_synthetic_naming(adapter, tmp_path):
    source = _make_video(tmp_path / "with_audio.mp4", [3, 3, 3], with_audio=True)
    clip = _p8_export_one(adapter, source, tmp_path, "an", 0.5, 1.0, naming=_R4C_NAMING)
    assert os.path.basename(clip) == "File001_18102026_001.mp4"
    kinds = [s.get('codec_type') for s in _streams(clip)]
    assert 'video' in kinds
    assert 'audio' not in kinds, kinds


@needs_ffmpeg
def test_the_naming_mode_adds_no_transcode_and_no_extra_ffmpeg_pass(adapter, tmp_path,
                                                                    monkeypatch):
    """Counted at the seam the adapter actually runs its children through.

    Three independent measurements, because any one of them alone could be satisfied by a wrong
    implementation: the same number of child processes, the same number of ENCODE commands, and
    argv that differs only in the file being written.
    """
    source = _make_three_shots(tmp_path / "passes.mp4", [90, 90, 90])
    recorded = []
    real = adapter["_run_media_command"]

    def counting(cmd, timeout, lifecycle=None):
        recorded.append(list(cmd))
        return real(cmd, timeout)

    monkeypatch.setitem(adapter, "_run_media_command", counting)

    _p8_export_one(adapter, source, tmp_path, "pass_legacy", 0.5, 1.0)
    legacy = list(recorded)
    recorded.clear()
    _p8_export_one(adapter, source, tmp_path, "pass_synthetic", 0.5, 1.0, naming=_R4C_NAMING)
    synthetic = list(recorded)

    assert len(legacy) == len(synthetic), (len(legacy), len(synthetic))
    legacy_encodes = [c for c in legacy if os.path.basename(c[0]).startswith("ffmpeg")]
    synthetic_encodes = [c for c in synthetic if os.path.basename(c[0]).startswith("ffmpeg")]
    assert len(legacy_encodes) == len(synthetic_encodes) == 1, \
        "one encode per clip, either way -- the mode must not add a pass"
    assert legacy_encodes[0][:-1] == synthetic_encodes[0][:-1], \
        "the mode changed an encoding argument"
    assert legacy_encodes[0][-1] != synthetic_encodes[0][-1]
    # And nothing renames a finished file afterwards: the ONE encode wrote the synthetic staging
    # name directly, so no source-revealing name ever existed on disk.
    assert "File001_18102026_001" in synthetic_encodes[0][-1]
    assert "passes" not in os.path.basename(synthetic_encodes[0][-1])


@needs_ffmpeg
def test_a_synthetic_export_leaves_the_source_file_byte_identical(adapter, tmp_path):
    source = _make_three_shots(tmp_path / "untouched.mp4", [90, 90, 90])
    before = (_sha256(source), os.path.getsize(source))
    _p8_export_one(adapter, source, tmp_path, "untouched_out", 0.5, 1.0, naming=_R4C_NAMING)
    assert (_sha256(source), os.path.getsize(source)) == before
