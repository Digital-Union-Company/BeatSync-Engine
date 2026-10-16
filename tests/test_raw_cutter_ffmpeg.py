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


def test_the_exported_segments_tile_the_source_duration(adapter, tmp_path):
    path = _make_video(tmp_path / "cover.mp4", [2, 2, 2, 2])
    source, plan = _plan_for(adapter, path)
    run_dir, _ = adapter["prepare_run_directory"](str(tmp_path / "coverout"), path, "t5")
    results = adapter["export_segments"](source=source, plan=plan, run_directory=run_dir,
                                         remove_audio=True, encoder='libx264')
    assert all(r.ok for r in results)
    total = sum(_duration(r.path) for r in results)
    assert total == pytest.approx(source.duration, abs=0.6)


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
