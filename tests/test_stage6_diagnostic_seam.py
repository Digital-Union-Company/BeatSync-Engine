"""Phase 3B seam: the diagnostic must stay wired, and the render path must stay untouched.

``ffmpeg_processing`` and ``video_processor`` need the whole portable runtime (cv2, numpy, logger,
paths), so neither can be imported on a bare interpreter. They are inspected with ``ast`` instead —
which is also the stronger check here, because what matters is *which* function is called at a
specific site and with which arguments, not what a mock happened to return.

Two directions are pinned:

* the failure reason reaches ``create_clip_parallel``'s error field (the propagation this phase adds);
* nothing about the command, the encoder settings, the frame arithmetic, or the complete-timeline
  refusal moved (the guarantee this phase must not break).
"""

from __future__ import annotations

import ast
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FFMPEG = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
_PROCESSOR = os.path.join(_REPO_ROOT, "src", "video_processor.py")


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node, name):
    found = []
    for item in ast.walk(node):
        if isinstance(item, ast.Call):
            got = (item.func.id if isinstance(item.func, ast.Name)
                   else item.func.attr if isinstance(item.func, ast.Attribute) else "")
            if got == name:
                found.append(item)
    return found


@pytest.fixture(scope="module")
def ffmpeg_tree():
    return _tree(_FFMPEG)


@pytest.fixture(scope="module")
def processor_tree():
    return _tree(_PROCESSOR)


# ---------------------------------------------------------------------------
# the boolean API must survive
# ---------------------------------------------------------------------------


def test_boolean_extract_function_still_exists_and_still_returns_bool(ffmpeg_tree):
    """Only one in-repo caller exists, but the boolean signature is a compatibility surface.

    It is a module-level function in an upstream file; changing its return type would break any
    legacy or upstream consumer for no benefit, since the detailed variant serves the new need.
    """
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg")

    assert ast.unparse(fn.returns) == "bool"
    params = [a.arg for a in fn.args.args]
    assert params == ["video_file", "start_time", "duration", "output_file", "fps",
                      "target_size", "use_nvenc", "gpu_encoder"]
    # It delegates rather than duplicating the command builder: one implementation, no drift.
    assert len(_calls(fn, "extract_clip_segment_ffmpeg_detailed")) == 1
    returns = [ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
    assert returns == ["success"]


def test_detailed_function_returns_success_and_reason(ffmpeg_tree):
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")

    assert ast.unparse(fn.returns) == "Tuple[bool, str]"
    assert [a.arg for a in fn.args.args] == [
        "video_file", "start_time", "duration", "output_file", "fps",
        "target_size", "use_nvenc", "gpu_encoder"]
    returns = {ast.unparse(n.value) for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value}
    # Every exit carries a reason, and success carries an empty one.
    assert "(True, '')" in returns
    assert all(r.startswith("(") for r in returns), returns


# ---------------------------------------------------------------------------
# the diagnostic is only consulted on failure
# ---------------------------------------------------------------------------


def test_stderr_is_summarised_only_when_the_return_code_is_non_zero(ffmpeg_tree):
    """The decisive success-path guarantee.

    On driver 617.14 every successful clip emits a scary-looking nvdec fallback warning on stderr. If
    the summariser were consulted unconditionally, all 150 valid clips of the validated render would
    have acquired a "reason" - and anything keying off a non-empty reason would call them failures.
    """
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")

    summaries = _calls(fn, "summarize_ffmpeg_failure")
    assert len(summaries) == 1

    guard = None
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and any(
            call is summaries[0] for call in _calls(node, "summarize_ffmpeg_failure")
        ):
            guard = node
            break
    assert guard is not None, "the summary call must live inside a conditional"
    test_src = ast.unparse(guard.test)
    assert "returncode" in test_src and "!= 0" in test_src, test_src


def test_missing_and_empty_output_use_the_dedicated_describer(ffmpeg_tree):
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")

    assert len(_calls(fn, "describe_output_problem")) == 1
    # The pre-existing failure condition is unchanged: missing OR zero bytes.
    source = ast.unparse(fn)
    assert "os.path.exists(output_file)" in source
    assert "os.path.getsize(output_file) == 0" in source


def test_exception_path_produces_a_bounded_reason_and_cannot_escape(ffmpeg_tree):
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")

    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "the broad except must remain"
    assert len(_calls(fn, "describe_exception")) == 1
    # A failed clip stays a failed clip; the handler must not re-raise.
    assert not any(isinstance(n, ast.Raise) for h in handlers for n in ast.walk(h))


def test_full_stderr_still_reaches_the_console_unchanged(ffmpeg_tree):
    """The bounded summary is additive. Console behaviour is a debugging lifeline, not replaced."""
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")

    prints = _calls(fn, "print")
    dumped = " ".join(ast.unparse(p) for p in prints)
    assert "result.stderr" in dumped, "the unabridged stderr print must stay"


# ---------------------------------------------------------------------------
# create_clip_parallel propagates it
# ---------------------------------------------------------------------------


def test_create_clip_parallel_uses_the_detailed_extractor(processor_tree):
    fn = _func(processor_tree, "create_clip_parallel")

    detailed = _calls(fn, "extract_clip_segment_ffmpeg_detailed")
    assert len(detailed) == 1
    assert _calls(fn, "extract_clip_segment_ffmpeg") == [], "must not call the boolean variant too"
    # Same keyword bundle as before - no argument was added, removed or renamed.
    assert [kw.arg for kw in detailed[0].keywords] == [None]


def test_the_failure_tuple_carries_the_reason(processor_tree):
    fn = _func(processor_tree, "create_clip_parallel")

    failure_returns = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
        and len(n.value.elts) == 6 and isinstance(n.value.elts[1], ast.Constant)
        and n.value.elts[1].value is None
    ]
    assert failure_returns, "the (i, None, ...) failure tuple must still exist"
    error_field = ast.unparse(failure_returns[0].value.elts[4])
    assert error_field == "detail", error_field

    # `detail` keeps the historical wording as a prefix and appends the reason.
    assigns = [ast.unparse(n) for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "detail" for t in n.targets)]
    assert assigns, "detail must be built explicitly"
    assert "FFmpeg extraction failed" in assigns[0]
    assert "reason" in assigns[0]


def test_the_extract_kwargs_bundle_is_unchanged(processor_tree):
    """Any change here would change the FFmpeg command, which this phase must not do."""
    fn = _func(processor_tree, "create_clip_parallel")

    bundles = [n for n in ast.walk(fn)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "extract_kwargs" for t in n.targets)]
    assert len(bundles) == 1
    keys = [k.value for k in bundles[0].value.keys if isinstance(k, ast.Constant)]
    assert keys == ["video_file", "start_time", "duration", "output_file", "fps",
                    "target_size", "use_nvenc", "gpu_encoder"]


# ---------------------------------------------------------------------------
# what must NOT have changed
# ---------------------------------------------------------------------------


def test_the_complete_timeline_refusal_is_intact(processor_tree):
    """Any failed clip still refuses concatenation. Diagnostics must not soften the guard."""
    fn = _func(processor_tree, "create_music_video")
    source = ast.unparse(fn)

    assert "refusing to concatenate an incomplete timeline" in source
    raises = [n for n in ast.walk(fn)
              if isinstance(n, ast.Raise) and "refusing to concatenate" in ast.unparse(n)]
    assert len(raises) == 1, "exactly one refusal, unchanged"
    assert "first_failures=clip_failures[:3]" in source, "bounded failure list still published"
    # Counted structurally: ast.unparse normalises the genexp's parentheses.
    failed_count = [n for n in ast.walk(fn)
                    if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "failed_count" for t in n.targets)]
    assert len(failed_count) == 1
    assert "clip_files" in ast.unparse(failed_count[0]) and "is None" in ast.unparse(failed_count[0])
    # And the per-clip reason is still bounded before it reaches the UI.
    assert "_short_error(error)" in source


def test_the_ffmpeg_command_and_encoder_settings_are_untouched(ffmpeg_tree):
    fn = _func(ffmpeg_tree, "extract_clip_segment_ffmpeg_detailed")
    source = ast.unparse(fn)

    # frame arithmetic
    assert "seconds_to_frame_count(duration, fps)" in source
    assert "frame_count_to_seconds(source_frame_count, fps)" in source
    # filter chain, in order
    assert "f'trim=duration={exact_source_duration}'" in source
    assert "'setpts=PTS-STARTPTS'" in source
    assert "f'scale={width}:{height}'" in source
    assert "f'fps={fps}'" in source
    # hwaccel and encoder selection
    assert "['-hwaccel', 'cuda']" in source
    assert "['-hwaccel', 'auto']" in source
    assert "get_nvenc_quality_args(gpu_encoder, include_pix_fmt=True)" in source
    assert "get_cpu_h264_quality_args(include_pix_fmt=True)" in source
    # frame-accurate output flags
    assert "'-vframes'" in source and "'-fps_mode', 'cfr'" in source
    assert "_run_media_command(cmd, timeout=120)" in source


def test_nvenc_and_cpu_quality_args_are_byte_for_byte_unchanged(ffmpeg_tree):
    """These decide output quality; Phase 3B is diagnostics only."""
    nvenc = ast.unparse(_func(ffmpeg_tree, "get_nvenc_quality_args"))
    cpu = ast.unparse(_func(ffmpeg_tree, "get_cpu_h264_quality_args"))

    for token in ("'-preset', 'p7'", "'-rc', 'vbr'", "'-b:v', '0'", "'-cq', NVENC_QUALITY_CQ",
                  "'-multipass', 'fullres'", "'-rc-lookahead', NVENC_LOOKAHEAD",
                  "'-spatial_aq', '1'", "'-temporal_aq', '1'",
                  "'-aq-strength', NVENC_AQ_STRENGTH", "'-b_ref_mode', 'middle'"):
        assert token in nvenc, token
    for token in ("'libx264'", "'-preset', 'ultrafast'", "'-crf', '0'"):
        assert token in cpu, token


def test_frame_lock_timeline_builder_is_untouched(processor_tree):
    fn = _func(processor_tree, "build_frame_aligned_cut_timeline")
    source = ast.unparse(fn)

    assert "np.rint" in source, "absolute quantisation to output frames must remain"
    assert "np.diff" in source or "diff" in source
