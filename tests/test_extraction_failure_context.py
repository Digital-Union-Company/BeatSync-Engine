"""MEDIA-VERIFY-S1: a failed clip must name the source video and the seek it attempted.

A D0 forensic investigation of a real 2,038-source render could establish *that* output segment 3
was killed (its retained temp MP4 had no ``moov`` atom) but not *which source video* it was reading,
because the failure string carried only the segment index and the elapsed time. The clip plan is not
persisted and the temp filename's suffix is a uuid4, so the source identity was unrecoverable after
the fact. This milestone closes that gap and changes nothing else.

``video_processor`` needs the whole portable runtime (cv2, numpy, logger, paths), so it cannot be
imported on a bare interpreter. Following the pattern in ``tests/test_render_cancellation.py``, the
behavioural tests here compile and execute the **real** ``create_clip_parallel`` body against a
hand-built namespace, so they assert what the shipped code does rather than what a mock returns.
``create_music_video`` genuinely cannot be executed that way — it closes over dozens of runtime
globals — so the surfaces inside it are pinned structurally with ``ast``.
"""

from __future__ import annotations

import ast
import os
import time
import types

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROCESSOR = os.path.join(_REPO_ROOT, "src", "video_processor.py")
_FFMPEG = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
_ANALYSIS = os.path.join(_REPO_ROOT, "src", "video_analysis.py")


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _body(tree, name):
    return ast.unparse(_func(tree, name))


@pytest.fixture(scope="module")
def processor_tree():
    return _tree(_PROCESSOR)


class _Cancelled(Exception):
    """Stands in for the real ``RenderCancelled``, which is also an ordinary ``Exception``."""


def _load_worker(extract, duration=30.0, duration_error=None):
    """The REAL ``create_clip_parallel``, over stubbed media calls.

    ``duration_error`` makes the source-duration probe raise, which is the pre-extraction failure
    path: the worker never reaches the extractor and has no resolved seek position.
    """
    node = _func(_tree(_PROCESSOR), "create_clip_parallel")
    calls: dict[str, list] = {"duration": [], "extract": []}

    def get_video_duration(path):
        calls["duration"].append(path)
        if duration_error is not None:
            raise duration_error
        return duration

    def extract_clip_segment_ffmpeg_detailed(**kwargs):
        calls["extract"].append(kwargs)
        return extract(**kwargs)

    namespace = {
        "time": time,
        "os": os,
        "random": __import__("random"),
        "uuid": types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex="cafe")),
        "get_video_duration": get_video_duration,
        "extract_clip_segment_ffmpeg_detailed": extract_clip_segment_ffmpeg_detailed,
        "RenderCancelled": _Cancelled,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), _PROCESSOR, "exec"), namespace)
    return namespace["create_clip_parallel"], calls


_OK = (True, "")
_TIMEOUT = (False, "FFmpeg timed out after 120s")


def _args(source="J:/lib/clip.mp4", index=3, target=1.96, fps=25.0,
          use_nvenc=True, encoder="h264_nvenc", temp_dir="C:/tmp/proc"):
    return (index, source, target, (1280, 720), use_nvenc, encoder, temp_dir, fps)


def _fields(detail):
    """``'A | B: c | D: e'`` -> ``{'B': 'c', 'D': 'e'}`` plus ``_head`` for the first segment."""
    parts = [p.strip() for p in detail.split("|")]
    out = {"_head": parts[0]}
    for part in parts[1:]:
        key, _, value = part.partition(":")
        out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------------
# the diagnostic itself
# ---------------------------------------------------------------------------


def test_a_timeout_names_the_exact_source_video():
    """The whole point of S1: D0 could not answer this question at all."""
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    source = r"J:\New folder\Cuts\Cuts4\0_279.mp4"
    detail = worker(_args(source=source))[4]

    assert source in detail
    assert _fields(detail)["Source"] == source


def test_the_planned_source_is_reported_not_the_fallback_argument():
    """When a planned moment overrides the argument, the diagnostic must name what was READ."""
    worker, calls = _load_worker(lambda **_k: _TIMEOUT)
    planned = {"video_file": "J:/lib/planned.mp4", "source_duration": 2.0, "start_time": 7.5}
    detail = worker(_args(source="J:/lib/fallback.mp4") + (planned,))[4]

    assert calls["extract"][0]["video_file"] == "J:/lib/planned.mp4"
    assert _fields(detail)["Source"] == "J:/lib/planned.mp4"
    assert "fallback.mp4" not in detail


def test_the_attempted_seek_position_is_reported():
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    planned = {"video_file": "J:/lib/a.mp4", "source_duration": 1.96, "start_time": 12.48}
    detail = worker(_args() + (planned,))[4]

    assert _fields(detail)["Source start"] == "12.480s"


def test_source_and_target_durations_are_distinguished():
    """They are different quantities and a diagnostic that fused them would mislead.

    The planned source window is clamped against the real source duration; the target segment
    duration is the frame-locked output length the planner asked for.
    """
    worker, _ = _load_worker(lambda **_k: _TIMEOUT, duration=5.0)
    planned = {"video_file": "J:/lib/a.mp4", "source_duration": 9.0, "start_time": 0.0}
    detail = worker(_args(target=1.96) + (planned,))[4]
    fields = _fields(detail)

    # 9.0 was clamped to the 5.0-second source; the target segment is still 1.96.
    assert fields["Source duration"] == "5.000s"
    assert fields["Target segment duration"] == "1.960s"
    assert fields["Source duration"] != fields["Target segment duration"]


def test_fps_and_encoder_are_reported():
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    nvenc = _fields(worker(_args(fps=25.0, use_nvenc=True, encoder="h264_nvenc"))[4])
    assert nvenc["Output FPS"] == "25.000"
    assert nvenc["Encoder"] == "h264_nvenc"

    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    cpu = _fields(worker(_args(use_nvenc=False, encoder="h264_nvenc"))[4])
    assert cpu["Encoder"] == "cpu-h264", "an encoder name must not be claimed when NVENC is off"


def test_the_timeout_reason_survives_alongside_the_context():
    """A diagnostic that evicts the diagnosis is the one outcome S1 must not produce."""
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    detail = worker(_args())[4]

    assert "FFmpeg extraction failed" in detail, "the historical wording is still the prefix"
    assert "FFmpeg timed out after 120s" in detail
    assert _fields(detail)["Reason"].endswith("FFmpeg timed out after 120s")


def test_the_clip_number_is_one_based_and_the_internal_index_is_stated():
    """D0 spent real effort reconciling "Clip 4" against temp clip index 3."""
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    detail = worker(_args(index=3))[4]

    assert "Clip 4 (internal index 3)" in detail
    assert worker(_args(index=0))[4].startswith("Clip 1 (internal index 0)")


def test_elapsed_time_is_reported():
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    head = _fields(worker(_args())[4])["_head"]
    assert "failed after" in head and head.endswith("s")


# ---------------------------------------------------------------------------
# honesty on the pre-extraction path
# ---------------------------------------------------------------------------


def test_a_failed_duration_probe_reports_unavailable_rather_than_a_fabricated_zero():
    """The worker never reached a seek position, so it must not claim it tried 0.000s.

    This is the §5(E) path: `get_video_duration` raises before `clip_start` or `source_duration`
    exist. Reporting 0.000s would read as "we sought to the beginning and that failed", which is a
    different and untrue story.
    """
    worker, calls = _load_worker(
        lambda **_k: _OK, duration_error=OSError("cannot open source"))
    index, path, _size, display, detail, _elapsed = worker(_args(source="J:/lib/gone.mp4"))

    assert (index, path, display) == (3, None, None)
    assert calls["extract"] == [], "the extractor must never have been called"
    fields = _fields(detail)
    assert fields["Source"] == "J:/lib/gone.mp4", "the source is known even here"
    assert fields["Source start"] == "unavailable"
    assert fields["Source duration"] == "unavailable"
    # Scoped to the TIMING fields: the elapsed figure in the head may legitimately be ~0s here,
    # because the stubbed probe raises immediately. What must never appear is a fabricated seek.
    assert "0" not in fields["Source start"] and "0" not in fields["Source duration"]
    assert "cannot open source" in detail, "the real cause is still carried"
    # The target duration IS known pre-probe, so it must still be reported.
    assert fields["Target segment duration"] == "1.960s"


def test_a_non_finite_fps_degrades_to_unavailable_instead_of_raising():
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    detail = worker(_args(fps=float("nan")))[4]
    assert _fields(detail)["Output FPS"] == "unavailable"


# ---------------------------------------------------------------------------
# path shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", [
    "J:/lib/with spaces/clip one.mp4",
    "J:/lib/\u00fcnicode-\u00e4\u00f6\u00df/\u30af\u30ea\u30c3\u30d7.mp4",
    "J:/" + "/".join(f"deep{n:03d}" for n in range(28)) + "/final-clip-name.mp4",
    r"\\?\J:\New folder\Cuts\Cuts4\0_279.mp4",
])
def test_awkward_paths_are_reported_verbatim(source):
    """Long, spaced and non-ASCII Windows paths must survive intact, not be mangled."""
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    detail = worker(_args(source=source))[4]

    assert _fields(detail)["Source"] == source
    assert "FFmpeg timed out after 120s" in detail, "a long path must not evict the reason"


def test_a_very_long_path_still_leaves_the_reason_inside_the_widened_budget(processor_tree):
    """The composed line must fit the budget the consumption site applies."""
    budget = None
    for node in processor_tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CLIP_FAILURE_DETAIL_MAX_CHARS":
                    budget = node.value.value
    assert isinstance(budget, int) and budget > 240

    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    source = "J:/" + "x" * 240 + "/clip.mp4"
    detail = worker(_args(source=source))[4]
    assert len(detail) <= budget, "a realistic worst-case path must fit without truncation"
    assert source in detail and "FFmpeg timed out after 120s" in detail


# ---------------------------------------------------------------------------
# everything that must NOT have changed
# ---------------------------------------------------------------------------


def test_a_successful_clip_is_untouched():
    worker, calls = _load_worker(lambda **_k: _OK)
    index, path, size, display, detail, elapsed = worker(_args())

    assert index == 3
    assert path and path == display
    assert detail is None, "success must still carry no reason"
    assert size == (1280, 720)
    assert isinstance(elapsed, float)
    assert len(calls["extract"]) == 1


def test_render_cancelled_stays_typed_and_is_never_described():
    """A Stop must not be dressed up as an FFmpeg failure."""
    def cancel(**_kwargs):
        raise _Cancelled("stop")

    worker, _ = _load_worker(cancel)
    with pytest.raises(_Cancelled):
        worker(_args())


def test_the_failure_tuple_shape_is_unchanged():
    worker, _ = _load_worker(lambda **_k: _TIMEOUT)
    result = worker(_args())
    assert len(result) == 6
    index, path, size, display, detail, elapsed = result
    assert (index, path, display) == (3, None, None)
    assert size == (1280, 720) and isinstance(detail, str) and isinstance(elapsed, float)


def test_the_eight_nine_and_ten_element_arg_forms_all_still_work():
    for extra in ((), (None,), (None, None)):
        worker, _ = _load_worker(lambda **_k: _OK)
        assert worker(_args() + extra)[4] is None


def test_temp_clip_naming_keeps_its_uuid_attempt_uniqueness(processor_tree):
    """S1 deliberately does NOT change the naming scheme.

    A path digest is not reversible into a path and deterministic naming alone loses attempt
    uniqueness, so the fix is to report the real path at the failure boundary instead. Asserted
    structurally because the harness stubs the uuid module.
    """
    body = _body(processor_tree, "create_clip_parallel")
    assert "f'temp_clip_{i}_{uuid.uuid4().hex}.mp4'" in body.replace('"', "'")


def test_the_diagnostic_writes_nothing():
    """No file, no probe, no subprocess may be created to describe a failure."""
    worker, calls = _load_worker(lambda **_k: _TIMEOUT)
    before = set(os.listdir("."))
    worker(_args())
    assert set(os.listdir(".")) == before
    # exactly one duration probe (the normal one) and one extraction attempt
    assert len(calls["duration"]) == 1
    assert len(calls["extract"]) == 1


def test_no_new_subprocess_or_retry_appeared_in_the_worker(processor_tree):
    fn = _func(processor_tree, "create_clip_parallel")
    body = ast.unparse(fn)
    for forbidden in ("subprocess", "Popen", "_run_media_command", "ffprobe", "FFMPEG_PATH"):
        assert forbidden not in body, f"create_clip_parallel must not reference {forbidden}"
    extract_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and ast.unparse(n.func) == "extract_clip_segment_ffmpeg_detailed"]
    assert len(extract_calls) == 1, "still exactly one attempt: S1 adds no retry"
    assert not [n for n in ast.walk(fn) if isinstance(n, (ast.For, ast.While))], \
        "no loop belongs in the worker; a retry would need one"


def test_the_extraction_timeout_and_command_are_untouched():
    """S1 is diagnostics-only: the 120s bound and the argv stay exactly as they were."""
    ffmpeg = _tree(_FFMPEG)
    fn = _func(ffmpeg, "extract_clip_segment_ffmpeg_detailed")
    source = ast.unparse(fn)

    assert "timeout=120" in source
    assert "'-ss', str(start_time)" in source
    assert "'-t', str(exact_source_duration)" in source
    assert "'-vframes', str(output_frame_count)" in source
    assert "'-fps_mode', 'cfr'" in source
    assert "seconds_to_frame_count(duration, fps)" in source


def test_the_frame_locked_timeline_arithmetic_is_untouched(processor_tree):
    fn = _func(processor_tree, "build_frame_aligned_cut_timeline")
    source = ast.unparse(fn)
    assert "np.diff(cut_frames)" in source
    assert "segment_durations = segment_frames / fps" in source
    assert "np.rint(raw_cut_times * fps)" in source


def test_source_selection_is_untouched(processor_tree):
    """The worker still resolves its source exactly as before; no second choice is made."""
    body = _body(processor_tree, "create_clip_parallel")
    assert "planned_clip.get('video_file') or video_file" in body.replace('"', "'")
    randoms = [n for n in ast.walk(_func(processor_tree, "create_clip_parallel"))
               if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("random.")]
    assert len(randoms) == 1, "exactly the one pre-existing fallback sample, no new draw"
    assert ast.unparse(randoms[0].func) == "random.uniform"


def test_the_protected_cache_constants_are_unchanged():
    """S1 touches no cache identity. Read from the real module source."""
    tree = _tree(_ANALYSIS)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value.value
    assert found["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"
    assert found["ANALYSIS_VERSION"] == "auto_av_analysis_v8_llama_vulkan_batched"


# ---------------------------------------------------------------------------
# the operator-facing surfaces, pinned structurally
# ---------------------------------------------------------------------------


def test_the_terminal_refusal_carries_the_first_failure(processor_tree):
    """The RuntimeError becomes the operator's error panel, replacing the progress line.

    Without this the identifying context lives only in the progress ERROR event's
    ``first_failures``, which is gone the moment the render ends.
    """
    fn = _func(processor_tree, "create_music_video")
    raises = [n for n in ast.walk(fn)
              if isinstance(n, ast.Raise) and "refusing to concatenate" in ast.unparse(n)]
    assert len(raises) == 1, "exactly one refusal, unchanged"
    text = ast.unparse(raises[0])
    assert "refusing to concatenate an incomplete timeline." in text
    assert "first_failure" in text, "the first failure's context must reach the terminal error"

    source = ast.unparse(fn)
    assert "clip_failures[0]" in source
    # Exactly one failure is promoted, so the bounded-reporting policy is intact.
    assert "clip_failures[:3]" in source


def test_the_bounded_first_failures_policy_is_unchanged(processor_tree):
    source = ast.unparse(_func(processor_tree, "create_music_video"))
    assert "first_failures=clip_failures[:3]" in source
    assert "len(clip_failures) <= 3" in source
    assert "len(clip_failures) % 25 == 0" in source


def test_no_new_progress_stage_or_status_writer_appeared(processor_tree):
    """Diagnostics ride the existing Stage 6 events; no new surface was invented."""
    source = ast.unparse(_func(processor_tree, "create_music_video"))
    for forbidden in ("gr.", "gradio", "status_output", "fork_progress.start(7", "stage=7"):
        assert forbidden not in source, forbidden
    # the two clip-failure emissions are still a warning and the refusal still an error
    assert "fork_progress.warning(" in source
    assert "fork_progress.error(" in source


def test_the_failure_is_still_a_failure(processor_tree):
    """No diagnostic may turn a refused render into a success.

    Counted structurally: ``ast.unparse`` normalises the genexp's parentheses, so matching the
    source text of that assignment is unreliable — the same trap the Phase 3B seam test documents.
    """
    fn = _func(processor_tree, "create_music_video")
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "failed_count" for t in n.targets)]
    assert len(assigns) == 1, "exactly one failed-clip count"
    text = ast.unparse(assigns[0])
    assert "clip_files" in text and "is None" in text
    assert "if failed_count:" in ast.unparse(fn)
