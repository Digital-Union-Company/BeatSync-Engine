"""MEDIA-VERIFY-S2-B P1: one sampled decode must be bounded, total and truthful.

Four things this suite exists to prevent, because each is easy to ship and hard to notice:

1. **A fabricated clean verdict.** A timeout, a non-zero exit, a missing progress block, a
   zero-frame output or a file that moved mid-decode must never become
   ``SAMPLE_OUTPUT_FRAMES_OK``. Every one of the fifteen statuses is executed and asserted.
2. **A short sample sold as damage.** Underproduction has many innocent causes (variable frame
   rate, an overstated container duration, a seek landing past the last decodable frame), so it is
   inconclusive — never "corrupt", never "confirmed end-of-file".
3. **A verdict about the wrong stream.** S2-A validates the FIRST video stream; the renderer gets
   the HIGHEST-RESOLUTION one. The selected stream must be validated on its own metadata.
4. **An arithmetic trap.** ``METADATA_OK`` can carry ``fps=0``, so any ``frames/fps`` predicate
   would divide by zero on a file S2-A calls fine. Nothing here may divide by a frame rate.

``beatsync_fork.media_sample`` is stdlib-only (CLAUDE.md's hard rule), so it is imported normally
and tested as ordinary code. The argv builder and the execution seam necessarily live in the
runtime, so they are reached the way the rest of this suite reaches upstream source: by ``ast``
extraction of the real function bodies, executed against a synthesised namespace. Those are real
assertions about real shipped source — but seam assertions, not integration runs. No FFmpeg, no
FFprobe, no Qwen and no Gradio is started anywhere in this file.
"""

from __future__ import annotations

import ast
import json
import math
import os

import pytest

from beatsync_fork.media_verify import DEFAULT_PROBE_TIMEOUT_SECONDS, ProbeOutcome, WorkBudget
from beatsync_fork.media_sample import (
    DEFAULT_SAMPLE_BUDGET_SECONDS,
    DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS,
    DEFAULT_SAMPLE_FRAMES,
    SAMPLE_ONLY_TEXT,
    SOURCE_COUNT_CAP,
    WORKER_CAP,
    DurationSource,
    FrameEvidence,
    SampleChangeWindow,
    SampleDecodeOutcome,
    SampleStatus,
    Stability,
    StreamEquivalence,
    classify_decode,
    coerce_stream_text,
    format_sample_report,
    parse_progress_output,
    run_sample_check,
    select_renderer_equivalent_stream,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FFMPEG = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
_MEDIA_SAMPLE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "media_sample.py")


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _referenced(node) -> set:
    """Every name this code actually REFERENCES: bare names and dotted attribute paths.

    Structural on purpose. A banned token must be banned by *usage*, never by searching the
    unparsed text — the docstrings in this repository legitimately discuss what the code does not
    do, and a substring ban over prose fails on its own explanation. Docstrings are
    ``ast.Constant`` nodes and so never reach this set.
    """
    names = set()

    def dotted(n):
        if isinstance(n, ast.Name):
            return n.id
        if isinstance(n, ast.Attribute):
            base = dotted(n.value)
            return f"{base}.{n.attr}" if base else n.attr
        return ""

    for child in ast.walk(node):
        if isinstance(child, (ast.Name, ast.Attribute)):
            path = dotted(child)
            if path:
                names.add(path)
                names.add(path.split(".")[-1])
        elif isinstance(child, ast.Import):
            names.update(alias.name.split(".")[0] for alias in child.names)
            names.update(alias.asname for alias in child.names if alias.asname)
        elif isinstance(child, ast.ImportFrom):
            if child.module:
                names.add(child.module.split(".")[0])
                names.add(child.module)
            names.update(alias.name for alias in child.names)
    return names


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def _stream(index=0, **over):
    s = {"index": index, "codec_type": "video", "codec_name": "h264",
         "width": 1920, "height": 1080, "r_frame_rate": "30/1", "duration": "60.0"}
    s.update(over)
    return {k: v for k, v in s.items() if v is not _ABSENT}


_ABSENT = object()


def _payload(*streams, container=None):
    out = {"streams": list(streams)}
    out["format"] = {"duration": "60.0"} if container is None else container
    return out


def _probe_ok(*streams, container=None):
    return ProbeOutcome(returncode=0, stdout=json.dumps(_payload(*streams, container=container)))


def _progress(frames=DEFAULT_SAMPLE_FRAMES, end=True, dup=0, drop=0, blocks=2, newline="\n"):
    """Realistic repeated ``key=value`` blocks, terminal marker last."""
    chunks = []
    for i in range(max(0, blocks - 1)):
        chunks.append(f"frame={max(0, frames - (blocks - 1 - i))}\nfps=30\nprogress=continue\n")
    tail = ""
    if frames is not None:
        tail += f"frame={frames}\n"
    if dup is not None:
        tail += f"dup_frames={dup}\n"
    if drop is not None:
        tail += f"drop_frames={drop}\n"
    tail += "progress=end\n" if end else "progress=continue\n"
    chunks.append(tail)
    return "".join(chunks).replace("\n", newline)


def _decoded(stdout=None, stderr="", returncode=0, **kw):
    return SampleDecodeOutcome(
        returncode=returncode, stdout=_progress() if stdout is None else stdout,
        stderr=stderr, **kw)


class _St:
    def __init__(self, size, mtime_ns):
        self.st_size = size
        self.st_mtime_ns = mtime_ns


def _stat_seq(*results):
    """A ``stat`` whose successive calls return/raise the given results."""
    state = {"i": 0}

    def stat(_path):
        i = state["i"]
        state["i"] = i + 1
        item = results[min(i, len(results) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    stat.calls = state
    return stat


_STABLE = _stat_seq(_St(10, 7), _St(10, 7), _St(10, 7))


def _check(**kw):
    """``run_sample_check`` with every injectable defaulted to a benign, stable, successful run."""
    launched = []
    outcome = kw.pop("outcome", None)

    def decode(path, **dkw):
        launched.append({"path": path, **dkw})
        return _decoded() if outcome is None else outcome

    params = {
        "path": "J:/lib/clip.mp4",
        "seek_seconds": 1.0,
        "probe": lambda _p: _probe_ok(_stream()),
        "decode": decode,
        "stat": _STABLE,
        "clock": iter_clock(),
    }
    params.update(kw)
    verdict = run_sample_check(params.pop("path"), params.pop("seek_seconds"), **params)
    return verdict, launched


def _field(report: str, label: str) -> str:
    """One report field's value, read by label so the column padding stays free to change."""
    for line in report.splitlines():
        if line.strip().startswith(label):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{label!r} not in report")


def iter_clock(step=0.5):
    """A deterministic monotonic clock: no wall time, no ``time`` dependency in assertions."""
    state = {"t": 1000.0}

    def clock():
        state["t"] += step
        return state["t"]

    return clock


# ---------------------------------------------------------------------------
# the fifteen statuses, each reached for its own reason
# ---------------------------------------------------------------------------

def test_the_taxonomy_is_exactly_fifteen_statuses_with_one_positive():
    assert len(list(SampleStatus)) == 15
    assert len({s.value for s in SampleStatus}) == 15
    positive = [s for s in SampleStatus if s is SampleStatus.OUTPUT_FRAMES_OK]
    assert len(positive) == 1
    assert all(s.value.startswith("SAMPLE_") for s in SampleStatus)


@pytest.mark.parametrize("frames", [0, -1, None, "", "abc", 2.5, True])
def test_a_request_for_a_nonsense_frame_count_is_invalid(frames):
    verdict, launched = _check(requested_frames=frames)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert launched == [], "no child may be started for an invalid request"


@pytest.mark.parametrize("seek", [-1.0, -0.001, float("nan"), float("inf"), "abc", None])
def test_a_nonsense_seek_is_invalid_and_never_decoded(seek):
    verdict, launched = _check(seek_seconds=seek)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert launched == []


def test_an_unobservable_source_is_missing_and_is_never_probed():
    probed = []

    def probe(path):
        probed.append(path)
        return _probe_ok(_stream())

    verdict, launched = _check(stat=_stat_seq(OSError("gone")), probe=probe)
    assert verdict.status is SampleStatus.SOURCE_MISSING
    assert probed == [] and launched == []


def test_a_source_that_disappears_before_the_decode_is_missing_not_changed():
    verdict, launched = _check(stat=_stat_seq(_St(10, 7), OSError("gone")))
    assert verdict.status is SampleStatus.SOURCE_MISSING
    assert launched == [], "a vanished source must not be handed to the decoder"


@pytest.mark.parametrize("outcome,needle", [
    (ProbeOutcome(timed_out=True), "unverified, not corrupt"),
    (ProbeOutcome(returncode=1, stderr="moov atom not found"), "moov"),
    (ProbeOutcome(returncode=0, stdout="not json"), "parseable JSON"),
    (ProbeOutcome(returncode=0, stdout="[]"), "JSON object"),
])
def test_unusable_metadata_blocks_the_decode_without_claiming_damage(outcome, needle):
    verdict, launched = _check(probe=lambda _p: outcome)
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    assert needle in verdict.detail
    assert launched == []


def test_a_probe_seam_failure_yields_no_opinion_at_all():
    verdict, launched = _check(probe=lambda _p: ProbeOutcome(error="FileNotFoundError: ffprobe"))
    assert verdict.status is SampleStatus.NOT_RUN
    assert launched == []


def test_an_audio_only_container_has_no_video_stream_to_sample():
    verdict, _ = _check(probe=lambda _p: _probe_ok({"index": 0, "codec_type": "audio"}))
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    assert "no video stream" in verdict.detail


def test_a_seek_at_or_past_the_selected_streams_duration_is_refused():
    verdict, launched = _check(seek_seconds=60.0,
                               probe=lambda _p: _probe_ok(_stream(duration="60.0")))
    assert verdict.status is SampleStatus.SEEK_OUT_OF_RANGE
    assert launched == []
    assert "60.000" in verdict.detail and verdict.duration_source is DurationSource.STREAM


def test_a_seek_inside_the_duration_is_accepted():
    verdict, launched = _check(seek_seconds=59.9)
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert len(launched) == 1


def test_the_decode_is_not_started_without_room_for_its_own_timeout():
    verdict, launched = _check(budget_seconds=10, decode_timeout=30)
    assert verdict.status is SampleStatus.BUDGET_EXHAUSTED
    assert launched == [], "no child may start that the budget could not let finish"
    assert "30s decode timeout" in verdict.detail


def test_the_budget_boundary_is_measured_with_the_real_workbudget_signature():
    # The shipped class takes the clock as an ARGUMENT; there is no no-argument form.
    budget = WorkBudget(60, started=100.0)
    assert budget.remaining(100.0) == 60.0
    assert budget.remaining(140.0) == 20.0
    assert budget.remaining(999.0) == 0.0
    assert budget.exhausted(160.0) and not budget.exhausted(159.0)
    with pytest.raises(TypeError):
        budget.remaining()


def test_a_budget_with_room_for_the_timeout_does_start_the_decode():
    verdict, launched = _check(budget_seconds=120, decode_timeout=30)
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert len(launched) == 1


def test_a_decode_seam_failure_is_not_run_and_not_a_file_verdict():
    verdict, _ = _check(outcome=SampleDecodeOutcome(error="FileNotFoundError: ffmpeg.exe"))
    assert verdict.status is SampleStatus.NOT_RUN
    assert verdict.returncode is None


def test_a_seam_exception_becomes_a_reported_outcome_never_a_raise():
    def decode(_path, **_kw):
        raise RuntimeError("handle closed")

    verdict = run_sample_check("J:/a.mp4", 1.0, probe=lambda _p: _probe_ok(_stream()),
                               decode=decode, stat=_STABLE, clock=iter_clock())
    assert verdict.status is SampleStatus.NOT_RUN
    assert "RuntimeError" in verdict.detail


def test_a_timeout_is_never_a_success_even_when_partial_progress_reached_the_total():
    verdict, _ = _check(outcome=SampleDecodeOutcome(
        timed_out=True, stdout=_progress(frames=DEFAULT_SAMPLE_FRAMES).encode("utf-8")))
    assert verdict.status is SampleStatus.TIMEOUT
    assert verdict.observed_frames == DEFAULT_SAMPLE_FRAMES, "the evidence is still reported"
    assert verdict.returncode is None
    assert "not a successful decode" in verdict.detail


def test_a_timeout_without_any_output_is_still_a_timeout():
    verdict, _ = _check(outcome=SampleDecodeOutcome(timed_out=True, stdout=None, stderr=None))
    assert verdict.status is SampleStatus.TIMEOUT
    assert verdict.observed_frames is None
    assert "no frame count" in verdict.detail


def test_a_nonzero_exit_outranks_partial_progress():
    verdict, _ = _check(outcome=_decoded(stdout=_progress(frames=12, end=False),
                                         stderr="Invalid NAL unit size", returncode=1))
    assert verdict.status is SampleStatus.FFMPEG_FAILED
    assert "Invalid NAL unit size" in verdict.detail


def test_a_full_clean_sample_is_the_only_positive_status():
    verdict, launched = _check()
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert verdict.observed_frames == DEFAULT_SAMPLE_FRAMES
    assert verdict.stability is Stability.STABLE
    assert verdict.change_window is SampleChangeWindow.NONE
    assert SAMPLE_ONLY_TEXT in verdict.detail
    assert launched[0]["requested_frames"] == DEFAULT_SAMPLE_FRAMES
    assert launched[0]["stream_index"] == 0


def test_zero_output_frames_is_never_success_and_outranks_the_stderr_tier():
    verdict, _ = _check(outcome=_decoded(stdout=_progress(frames=0, blocks=1),
                                         stderr="error while decoding MB 0 0"))
    assert verdict.status is SampleStatus.ZERO_FRAMES


def test_an_underfilled_sample_is_inconclusive_and_reports_both_counts():
    verdict, _ = _check(outcome=_decoded(stdout=_progress(frames=7)))
    assert verdict.status is SampleStatus.UNDERFILLED_UNVERIFIED
    assert verdict.observed_frames == 7 and verdict.requested_frames == DEFAULT_SAMPLE_FRAMES
    assert "7 of 30" in verdict.detail
    assert "INCONCLUSIVE" in verdict.detail


def test_an_underfilled_sample_is_not_attributed_to_end_of_file_or_damage():
    verdict, _ = _check(outcome=_decoded(stdout=_progress(frames=1)))
    assert verdict.status is SampleStatus.UNDERFILLED_UNVERIFIED
    lowered = verdict.detail.lower()
    assert "not attributed to end-of-file" in lowered
    assert "not attributed to damage" in lowered


@pytest.mark.parametrize("stdout,why", [
    (_progress(end=False), "no terminal"),
    ("fps=30\nprogress=end\n", "no frame count"),
    ("frame=abc\nprogress=end\n", "malformed frame count"),
    (_progress(frames=DEFAULT_SAMPLE_FRAMES + 1), "contradicts -frames:v"),
    (_progress(dup=3), "dup=3"),
    (_progress(drop=2), "drop=2"),
])
def test_untrustworthy_evidence_never_becomes_a_clean_result(stdout, why):
    verdict, _ = _check(outcome=_decoded(stdout=stdout))
    assert verdict.status is SampleStatus.EVIDENCE_UNTRUSTWORTHY
    assert why in verdict.detail


def test_decoder_errors_at_exit_zero_qualify_an_otherwise_complete_sample():
    verdict, _ = _check(outcome=_decoded(stderr="[h264] error while decoding MB 12 7"))
    assert verdict.status is SampleStatus.OUTPUT_WITH_DECODER_ERRORS
    assert "error while decoding" in verdict.detail
    assert verdict.observed_frames == DEFAULT_SAMPLE_FRAMES


def test_a_source_changed_before_the_decode_blocks_the_decoder_entirely():
    verdict, launched = _check(stat=_stat_seq(_St(10, 7), _St(99, 8), _St(99, 8)))
    assert verdict.status is SampleStatus.SOURCE_CHANGED
    assert verdict.change_window is SampleChangeWindow.CROSS_PHASE
    assert verdict.stability is Stability.CHANGED
    assert launched == [], "stale metadata chose the -map target; it must not be decoded"
    assert "NOT launched" in verdict.detail


def test_a_source_changed_during_the_decode_outranks_a_clean_frame_count():
    verdict, launched = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), _St(42, 9)))
    assert verdict.status is SampleStatus.SOURCE_CHANGED
    assert verdict.change_window is SampleChangeWindow.DURING_DECODE
    assert verdict.stability is Stability.CHANGED
    assert len(launched) == 1, "it was only observable after the child ran"


def test_a_timeout_and_a_change_retain_both_facts():
    verdict, _ = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), _St(42, 9)),
                        outcome=SampleDecodeOutcome(timed_out=True, stdout=b"frame=3\n"))
    assert verdict.status is SampleStatus.TIMEOUT, "the timeout must not hide behind the change"
    assert verdict.change_window is SampleChangeWindow.DURING_DECODE
    assert verdict.stability is Stability.CHANGED


def test_an_ffmpeg_failure_and_a_change_retain_both_facts():
    verdict, _ = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), _St(42, 9)),
                        outcome=_decoded(returncode=69, stderr="boom"))
    assert verdict.status is SampleStatus.FFMPEG_FAILED
    assert verdict.change_window is SampleChangeWindow.DURING_DECODE


def test_a_failed_post_decode_observation_fails_closed():
    verdict, launched = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), OSError("locked")))
    assert verdict.status is SampleStatus.EVIDENCE_UNTRUSTWORTHY
    assert verdict.stability is Stability.UNVERIFIED
    assert verdict.change_window is SampleChangeWindow.NONE, "no change was observed; none claimed"
    assert len(launched) == 1
    assert "post-decode source observation" in verdict.detail


def test_the_post_decode_observation_is_attempted_on_every_terminal_path():
    for outcome in (_decoded(),
                    _decoded(returncode=1, stderr="x"),
                    SampleDecodeOutcome(timed_out=True),
                    SampleDecodeOutcome(error="OSError: nope")):
        stat = _stat_seq(_St(10, 7), _St(10, 7), _St(10, 7))
        _check(stat=stat, outcome=outcome)
        assert stat.calls["i"] == 3, f"three observations required for {outcome}"


# ---------------------------------------------------------------------------
# renderer-equivalent stream selection
# ---------------------------------------------------------------------------

def test_a_lone_video_stream_is_trivially_renderer_equivalent():
    selection = select_renderer_equivalent_stream(_payload(_stream(index=0)))
    assert selection.ok
    assert selection.stream.index == 0
    assert selection.stream.equivalence is StreamEquivalence.SINGLE_VIDEO_STREAM


def test_the_highest_resolution_video_stream_wins_not_the_first():
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=0, width=640, height=360),
        _stream(index=1, width=3840, height=2160),
    ))
    assert selection.ok
    assert selection.stream.index == 1, "the renderer passes no -map; resolution decides"
    assert selection.stream.equivalence is StreamEquivalence.BY_HIGHEST_RESOLUTION


def test_an_equal_resolution_tie_is_resolved_by_the_lowest_index_not_called_ambiguous():
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=3, width=1920, height=1080),
        _stream(index=1, width=1920, height=1080),
        _stream(index=2, width=1920, height=1080),
    ))
    assert selection.ok, "a tie is resolvable: the documented rule picks the lowest index"
    assert selection.stream.index == 1
    assert selection.stream.equivalence is StreamEquivalence.BY_HIGHEST_RESOLUTION


def test_a_non_video_stream_never_competes_for_selection():
    selection = select_renderer_equivalent_stream(_payload(
        {"index": 0, "codec_type": "audio", "width": 9999, "height": 9999},
        _stream(index=1, width=640, height=480),
    ))
    assert selection.ok and selection.stream.index == 1


def test_multiple_video_streams_with_an_attached_picture_fail_closed():
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=0, width=1920, height=1080),
        _stream(index=1, width=600, height=600, disposition={"attached_pic": 1}),
    ))
    assert not selection.ok
    assert "attached" in selection.ambiguous_reason
    assert not selection.invalid_reason


def test_a_lone_attached_picture_is_still_what_the_renderer_would_select():
    selection = select_renderer_equivalent_stream(
        _payload(_stream(index=0, disposition={"attached_pic": 1})))
    assert selection.ok, "with one candidate there is nothing to be ambiguous about"
    assert selection.stream.attached_pic is True
    assert selection.stream.equivalence is StreamEquivalence.SINGLE_VIDEO_STREAM


def test_an_unrankable_competitor_makes_the_choice_ambiguous():
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=0, width=1920, height=1080),
        _stream(index=1, width=_ABSENT, height=_ABSENT),
    ))
    assert not selection.ok
    assert "frame size" in selection.ambiguous_reason


@pytest.mark.parametrize("index", [_ABSENT, None, "abc", -1, 1.5])
def test_a_video_stream_without_a_usable_absolute_index_is_ambiguous(index):
    selection = select_renderer_equivalent_stream(_payload(_stream(index=index)))
    assert not selection.ok
    assert "absolute index" in selection.ambiguous_reason


def test_a_lone_stream_with_unusable_geometry_is_bad_metadata_not_ambiguity():
    selection = select_renderer_equivalent_stream(_payload(_stream(width=0, height=0)))
    assert not selection.ok
    assert selection.invalid_reason and not selection.ambiguous_reason


def test_the_container_duration_is_the_fallback_and_its_provenance_is_recorded():
    stream_level = select_renderer_equivalent_stream(_payload(_stream(duration="12.5")))
    assert stream_level.stream.duration == pytest.approx(12.5)
    assert stream_level.stream.duration_source is DurationSource.STREAM

    fallback = select_renderer_equivalent_stream(
        _payload(_stream(duration=_ABSENT), container={"duration": "33.0"}))
    assert fallback.stream.duration == pytest.approx(33.0)
    assert fallback.stream.duration_source is DurationSource.CONTAINER


def test_no_duration_anywhere_is_bad_metadata_for_the_selected_stream():
    selection = select_renderer_equivalent_stream(
        _payload(_stream(duration=_ABSENT), container={}))
    assert not selection.ok
    assert "no usable duration" in selection.invalid_reason


def test_a_bad_duration_on_an_UNSELECTED_first_stream_does_not_block_the_sample():
    """S2-A would call this file invalid; the stream the renderer uses is fine."""
    verdict, launched = _check(probe=lambda _p: _probe_ok(
        _stream(index=0, width=320, height=240, duration=_ABSENT),
        _stream(index=1, width=1920, height=1080, duration="48.0"),
        container={},
    ))
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert verdict.stream_index == 1
    assert verdict.duration == pytest.approx(48.0)
    assert launched[0]["stream_index"] == 1


def test_a_valid_first_stream_cannot_vouch_for_an_invalid_selected_stream():
    verdict, launched = _check(probe=lambda _p: _probe_ok(
        _stream(index=0, width=640, height=480, duration="60.0"),
        _stream(index=1, width=3840, height=2160, duration=_ABSENT),
        container={},
    ))
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    assert "Video stream 1" in verdict.detail
    assert launched == [], "the stream we would decode was never validated"


def test_the_selected_streams_own_metadata_is_what_gets_reported():
    verdict, _ = _check(probe=lambda _p: _probe_ok(
        _stream(index=0, width=320, height=240, codec_name="mjpeg", r_frame_rate="1/1"),
        _stream(index=1, width=1920, height=1080, codec_name="hevc", r_frame_rate="24000/1001"),
    ))
    assert verdict.stream_index == 1
    assert verdict.codec == "hevc"
    assert (verdict.width, verdict.height) == (1920, 1080)
    assert verdict.fps == pytest.approx(23.976, abs=0.001)


@pytest.mark.parametrize("rate", [_ABSENT, "", "0/0", "30/0", "abc", None, "0/1"])
def test_an_unusable_frame_rate_never_raises_and_never_blocks_the_sample(rate):
    verdict, launched = _check(probe=lambda _p: _probe_ok(_stream(r_frame_rate=rate)))
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK, "fps is context, never a gate"
    assert verdict.fps == 0.0
    assert len(launched) == 1


def test_a_variable_frame_rate_source_that_underfills_is_inconclusive_not_damaged():
    verdict, _ = _check(probe=lambda _p: _probe_ok(_stream(r_frame_rate="1000/1")),
                        outcome=_decoded(stdout=_progress(frames=4)))
    assert verdict.status is SampleStatus.UNDERFILLED_UNVERIFIED


def test_no_classifier_divides_by_a_frame_rate():
    """Structural: a ``/`` whose right operand is an fps-ish name would reintroduce the bug."""
    tree = _tree(_MEDIA_SAMPLE)
    for name in ("classify_decode", "run_sample_check", "select_renderer_equivalent_stream"):
        node = _func(tree, name)
        for child in ast.walk(node):
            if isinstance(child, ast.BinOp) and isinstance(child.op, ast.Div):
                right = ast.unparse(child.right).lower()
                assert "fps" not in right and "frame_rate" not in right, \
                    f"{name} divides by {right}"


# ---------------------------------------------------------------------------
# the progress parser
# ---------------------------------------------------------------------------

def test_multiple_progress_blocks_resolve_to_the_last_values():
    evidence = parse_progress_output(
        "frame=10\nprogress=continue\nframe=20\nprogress=continue\n"
        "frame=30\ndup_frames=0\ndrop_frames=0\nprogress=end\n")
    assert evidence.frames == 30
    assert evidence.saw_end is True
    assert evidence.blocks == 3


def test_crlf_and_lf_are_both_understood():
    lf = parse_progress_output(_progress(frames=5, newline="\n"))
    crlf = parse_progress_output(_progress(frames=5, newline="\r\n"))
    assert lf.frames == crlf.frames == 5
    assert lf.saw_end and crlf.saw_end


@pytest.mark.parametrize("raw,frames", [
    (None, None),
    ("", None),
    (b"", None),
    (b"frame=9\nprogress=end\n", 9),
    # Undecodable bytes on their OWN line are skipped; the clean evidence still reads.
    (b"\xff\xfe\nframe=4\nprogress=end\n", 4),
])
def test_the_parser_accepts_bytes_none_and_invalid_utf8(raw, frames):
    evidence = parse_progress_output(raw)
    assert evidence.frames == frames


def test_invalid_bytes_fused_into_a_key_yield_no_frame_evidence():
    """A corrupted key is not a frame count.

    ``b"\\xff\\xfe frame=4"`` decodes to ``"\\ufffd\\ufffd frame=4"``, whose key is
    ``"\\ufffd\\ufffd frame"`` — not ``"frame"``. Reading it as a frame count anyway would be
    accepting evidence we know arrived damaged, so the line is skipped and the result falls through
    to the untrustworthy tier rather than to a number.
    """
    evidence = parse_progress_output(b"\xff\xfe frame=4\nprogress=end\n")
    assert evidence.frames is None
    assert evidence.malformed_frame is False, "no frame key was ever seen, so nothing malformed"
    status, _ = classify_decode(_decoded(stdout="x"), evidence, 30)
    assert status is SampleStatus.EVIDENCE_UNTRUSTWORTHY


def test_partial_trailing_output_is_not_a_terminal_block():
    evidence = parse_progress_output("frame=10\nprogress=continue\nframe=2")
    assert evidence.frames == 2
    assert evidence.saw_end is False


def test_junk_lines_on_the_progress_channel_cannot_corrupt_the_evidence():
    evidence = parse_progress_output(
        "a banner line\n\x00\x01binary\nframe=30\n=novalue\n  \nprogress=end\n")
    assert evidence.frames == 30 and evidence.saw_end is True


@pytest.mark.parametrize("raw", ["frame=-3\nprogress=end\n", "frame=2.5\nprogress=end\n",
                                "frame=\nprogress=end\n", "frame=1e3\nprogress=end\n"])
def test_a_malformed_frame_count_is_unavailable_not_coerced(raw):
    evidence = parse_progress_output(raw)
    assert evidence.frames is None
    assert evidence.malformed_frame is True


def test_missing_dup_and_drop_counters_are_unknown_never_zero():
    evidence = parse_progress_output("frame=30\nprogress=end\n")
    assert evidence.dup_frames is None and evidence.drop_frames is None
    assert evidence.frames == 30


def test_unknown_counters_are_compatible_with_the_positive_status():
    verdict, _ = _check(outcome=_decoded(stdout="frame=30\nprogress=end\n"))
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert verdict.dup_frames is None and verdict.drop_frames is None


def test_an_unknown_counter_is_never_presented_as_a_measurement_of_zero():
    verdict, _ = _check(outcome=_decoded(stdout="frame=30\nprogress=end\n"))
    report = format_sample_report(verdict)
    assert "unknown / unknown" in report
    assert "UNKNOWN is not a measurement of zero" in report


@pytest.mark.parametrize("dup,drop", [(1, 0), (0, 1), (5, 5)])
def test_an_explicitly_positive_counter_downgrades_the_result(dup, drop):
    verdict, _ = _check(outcome=_decoded(stdout=_progress(dup=dup, drop=drop)))
    assert verdict.status is SampleStatus.EVIDENCE_UNTRUSTWORTHY


def test_a_malformed_counter_is_unknown_and_does_not_fabricate_a_downgrade():
    evidence = parse_progress_output("frame=30\ndup_frames=N/A\nprogress=end\n")
    assert evidence.dup_frames is None
    status, _ = classify_decode(_decoded(stdout="irrelevant"), evidence, 30)
    assert status is SampleStatus.OUTPUT_FRAMES_OK


def test_coerce_stream_text_is_total():
    assert coerce_stream_text(None) == ""
    assert coerce_stream_text(b"ok") == "ok"
    assert coerce_stream_text("ok") == "ok"
    assert "\ufffd" in coerce_stream_text(b"\xff")
    assert coerce_stream_text(17) == "17"


# ---------------------------------------------------------------------------
# precedence, asserted directly on the pure classifier
# ---------------------------------------------------------------------------

def test_timeout_outranks_every_progress_signal():
    perfect = parse_progress_output(_progress())
    status, _ = classify_decode(SampleDecodeOutcome(timed_out=True), perfect, 30)
    assert status is SampleStatus.TIMEOUT


def test_a_seam_error_outranks_a_timeout_flag():
    status, _ = classify_decode(
        SampleDecodeOutcome(error="boom", timed_out=True), FrameEvidence(), 30)
    assert status is SampleStatus.NOT_RUN


def test_a_nonzero_exit_outranks_a_perfect_frame_count():
    perfect = parse_progress_output(_progress())
    status, _ = classify_decode(_decoded(returncode=1, stderr="x"), perfect, 30)
    assert status is SampleStatus.FFMPEG_FAILED


def test_a_change_during_decode_outranks_a_perfect_frame_count():
    perfect = parse_progress_output(_progress())
    status, _ = classify_decode(_decoded(), perfect, 30,
                                change_window=SampleChangeWindow.DURING_DECODE)
    assert status is SampleStatus.SOURCE_CHANGED


def test_a_cross_phase_only_window_does_not_reach_the_decode_classifier():
    perfect = parse_progress_output(_progress())
    status, _ = classify_decode(_decoded(), perfect, 30,
                                change_window=SampleChangeWindow.CROSS_PHASE)
    assert status is SampleStatus.OUTPUT_FRAMES_OK, \
        "the cross-phase gate already refused earlier; it is not re-judged here"


def test_a_post_stat_failure_outranks_a_perfect_frame_count():
    perfect = parse_progress_output(_progress())
    status, _ = classify_decode(_decoded(), perfect, 30, post_stat_ok=False)
    assert status is SampleStatus.EVIDENCE_UNTRUSTWORTHY


def test_decoder_errors_only_qualify_a_complete_sample_never_an_underfilled_one():
    partial = parse_progress_output(_progress(frames=4))
    status, _ = classify_decode(_decoded(stderr="error while decoding"), partial, 30)
    assert status is SampleStatus.UNDERFILLED_UNVERIFIED


def test_every_status_reachable_through_the_classifier_carries_a_detail():
    cases = [
        SampleDecodeOutcome(error="e"),
        SampleDecodeOutcome(timed_out=True),
        _decoded(returncode=2, stderr="x"),
        _decoded(stdout=_progress(frames=0, blocks=1)),
        _decoded(stdout=_progress(frames=3)),
        _decoded(stdout=_progress(end=False)),
        _decoded(stderr="bad"),
        _decoded(),
    ]
    for outcome in cases:
        status, detail = classify_decode(
            outcome, parse_progress_output(getattr(outcome, "stdout", "")), 30)
        assert detail.strip(), status


# ---------------------------------------------------------------------------
# the argv contract -- the REAL builder, executed from source
# ---------------------------------------------------------------------------

def _load_sample_seam():
    """Execute the REAL `sample_decode_video` + argv builder over a stubbed runner."""
    import subprocess as sp
    tree = _tree(_FFMPEG)
    nodes = [_func(tree, "build_sample_decode_command"), _func(tree, "sample_decode_video")]
    calls = []
    behaviour = {"fn": lambda cmd, timeout: sp.CompletedProcess(cmd, 0, _progress(), "")}

    def runner(cmd, timeout=None, lifecycle=None):
        calls.append({"cmd": cmd, "timeout": timeout, "lifecycle": lifecycle})
        return behaviour["fn"](cmd, timeout)

    namespace = {
        "FFMPEG_PATH": r"C:\bin\ffmpeg\ffmpeg.exe",
        "_run_media_command": runner,
        "_MEDIA_SAMPLE_FRAMES": DEFAULT_SAMPLE_FRAMES,
        "_MEDIA_SAMPLE_DECODE_TIMEOUT": DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS,
        "SampleDecodeOutcome": SampleDecodeOutcome,
        "subprocess": sp,
        "List": list,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _FFMPEG, "exec"), namespace)
    return namespace, calls, behaviour


def test_the_sample_command_is_an_argv_array_with_the_path_as_one_element():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"](r"J:\lib\a b\clip ü.mp4", 12.5, 1, 30)
    assert isinstance(cmd, list) and all(isinstance(part, str) for part in cmd)
    assert cmd[0].endswith("ffmpeg.exe")
    assert r"J:\lib\a b\clip ü.mp4" in cmd, "the path is one argv element, never quoted inline"


def test_exactly_one_input_seek_placed_before_the_input():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 12.5, 0, 30)
    assert cmd.count("-ss") == 1, "a second output-side seek is not part of this contract"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "12.5"


def test_the_selected_stream_is_pinned_by_absolute_index():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 7, 30)
    assert cmd[cmd.index("-map") + 1] == "0:7"
    assert "0:v:0" not in cmd, "the renderer does not select the first video stream"


def test_the_frame_bound_and_the_passthrough_mode_are_present():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 0, 30)
    assert cmd[cmd.index("-frames:v") + 1] == "30"
    assert cmd[cmd.index("-fps_mode") + 1] == "passthrough", \
        "cfr could reach the count by duplicating one good frame"
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"


def test_the_output_target_is_the_null_muxer_and_no_file():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 0, 30)
    assert cmd[-3:] == ["-f", "null", "-"]
    for part in cmd[1:]:
        assert not part.lower().endswith((".mp4", ".mov", ".mkv", ".avi", ".ts", ".webm")) \
            or part == "J:/a.mp4"


@pytest.mark.parametrize("forbidden", [
    "-vf", "-filter:v", "-filter_complex", "-c:v", "-y", "-xerror",
    "-noaccurate_seek", "-movflags", "-r", "-t",
])
def test_the_sample_command_carries_no_render_or_abort_option(forbidden):
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 0, 30)
    assert forbidden not in cmd


def test_the_sample_command_requests_no_hardware_encoder():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 0, 30)
    joined = " ".join(cmd).lower()
    for token in ("nvenc", "hwaccel", "cuda", "vulkan", "libx264"):
        assert token not in joined, token


def test_the_sample_command_drops_every_non_video_stream():
    ns, _c, _b = _load_sample_seam()
    cmd = ns["build_sample_decode_command"]("J:/a.mp4", 1.0, 0, 30)
    for flag in ("-an", "-sn", "-dn", "-nostdin", "-hide_banner"):
        assert flag in cmd
    assert cmd[cmd.index("-v") + 1] == "error"


# ---------------------------------------------------------------------------
# the execution seam
# ---------------------------------------------------------------------------

def test_the_seam_passes_a_finite_timeout_and_no_lifecycle():
    ns, calls, _b = _load_sample_seam()
    ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0, timeout=11)
    assert calls[0]["timeout"] == 11
    assert calls[0]["lifecycle"] is None, "a diagnostic is not a render; no cancellation lifecycle"
    ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0)
    assert calls[1]["timeout"] == DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS > 0


def test_the_seam_reports_a_clean_run_verbatim():
    ns, _c, _b = _load_sample_seam()
    outcome = ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0)
    assert outcome.returncode == 0 and outcome.timed_out is False and outcome.error == ""
    assert parse_progress_output(outcome.stdout).frames == DEFAULT_SAMPLE_FRAMES


def test_the_seam_preserves_partial_bytes_from_a_timeout():
    import subprocess as sp
    ns, _c, behaviour = _load_sample_seam()

    def timeout(cmd, _timeout):
        raise sp.TimeoutExpired(cmd, 30, output=b"frame=5\nprogress=continue\n", stderr=b"warn")

    behaviour["fn"] = timeout
    outcome = ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0)
    assert outcome.timed_out is True
    assert parse_progress_output(outcome.stdout).frames == 5, \
        "run()'s TimeoutExpired carries the partial capture; it must not be discarded"


def test_the_seam_survives_a_timeout_that_captured_nothing():
    import subprocess as sp
    ns, _c, behaviour = _load_sample_seam()

    def timeout(cmd, _timeout):
        raise sp.TimeoutExpired(cmd, 30)

    behaviour["fn"] = timeout
    outcome = ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0)
    assert outcome.timed_out is True
    assert parse_progress_output(outcome.stdout).frames is None


@pytest.mark.parametrize("exc", [OSError("no exe"), ValueError("bad argv")])
def test_a_spawn_failure_becomes_an_error_outcome_not_an_exception(exc):
    ns, _c, behaviour = _load_sample_seam()

    def boom(_cmd, _timeout):
        raise exc

    behaviour["fn"] = boom
    outcome = ns["sample_decode_video"]("J:/a.mp4", seek_seconds=1.0, stream_index=0)
    assert outcome.error and outcome.timed_out is False
    assert type(exc).__name__ in outcome.error


def test_the_seam_adds_no_second_subprocess_implementation():
    """Structural: the one public runner is reused, and no new Popen/run site is introduced."""
    tree = _tree(_FFMPEG)
    used = _referenced(_func(tree, "sample_decode_video"))
    assert "_run_media_command" in used
    for banned in ("subprocess.Popen", "subprocess.run", "subprocess.check_output",
                   "os.system", "os.popen"):
        assert banned not in used, banned


def test_the_seam_creates_no_render_lifecycle_and_no_cancellation_authority():
    tree = _tree(_FFMPEG)
    for name in ("build_sample_decode_command", "sample_decode_video"):
        used = _referenced(_func(tree, name))
        for banned in ("RenderLifecycle", "RenderCancelled", "lifecycle"):
            assert banned not in used, f"{name} references {banned}"


# ---------------------------------------------------------------------------
# isolation: nothing is written, nothing is cached, no model is reached
# ---------------------------------------------------------------------------

def test_a_full_check_writes_nothing_and_leaves_the_source_untouched(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not really a video")
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    listing_before = sorted(p.name for p in tmp_path.iterdir())

    verdict = run_sample_check(
        str(source), 1.0,
        probe=lambda _p: _probe_ok(_stream()),
        decode=lambda _p, **_kw: _decoded(),
        clock=iter_clock())

    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert (source.stat().st_size, source.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing_before
    assert source.read_bytes() == b"not really a video"


def test_the_real_stat_is_used_when_none_is_injected(tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x")
    verdict = run_sample_check(str(source), 1.0, probe=lambda _p: _probe_ok(_stream()),
                               decode=lambda _p, **_kw: _decoded(), clock=iter_clock())
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert verdict.stability is Stability.STABLE

    missing = run_sample_check(str(tmp_path / "nope.mp4"), 1.0,
                               probe=lambda _p: _probe_ok(_stream()),
                               decode=lambda _p, **_kw: _decoded(), clock=iter_clock())
    assert missing.status is SampleStatus.SOURCE_MISSING


def test_the_pure_core_reaches_no_cache_no_model_and_no_runtime():
    """Structural: the module's imports stay inside the stdlib plus the fork package."""
    tree = _tree(_MEDIA_SAMPLE)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "json", "time", "dataclasses", "enum", "typing",
                     "beatsync_fork"}, roots

    used = _referenced(tree)
    for banned in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "qwen", "llama",
                   "stage5", "subprocess", "open", "os.remove", "shutil"):
        assert banned not in used, banned


def test_the_bounds_are_the_approved_conservative_defaults():
    assert DEFAULT_SAMPLE_FRAMES == 30
    assert DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS == 30
    assert DEFAULT_SAMPLE_BUDGET_SECONDS == 60
    assert DEFAULT_PROBE_TIMEOUT_SECONDS == 20, "the FFprobe bound is S2-A's, reused not redefined"
    assert SOURCE_COUNT_CAP == 1 and WORKER_CAP == 1
    assert DEFAULT_SAMPLE_BUDGET_SECONDS >= DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def test_the_report_states_the_sample_only_boundary_in_every_outcome():
    for kw in ({}, {"outcome": _decoded(stdout=_progress(frames=2))},
               {"outcome": SampleDecodeOutcome(timed_out=True)},
               {"seek_seconds": 999.0}):
        verdict, _ = _check(**kw)
        report = format_sample_report(verdict)
        assert SAMPLE_ONLY_TEXT in report
        assert "size and modification time only" in report


def test_the_report_never_claims_health_or_full_verification():
    verdict, _ = _check()
    report = format_sample_report(verdict).lower()
    for claim in ("healthy", "fully verified", "no corruption", "guaranteed",
                  "frames decoded", "frames verified"):
        assert claim not in report, claim


def test_the_report_keeps_the_full_source_path_and_names_the_tested_stream():
    long_path = "J:/library/anime/" + "d" * 90 + "/episode 01 [1080p].mkv"
    verdict, _ = _check(path=long_path,
                        probe=lambda _p: _probe_ok(_stream(index=0, width=640, height=360),
                                                   _stream(index=4, width=1920, height=1080)))
    report = format_sample_report(verdict)
    assert long_path in report, "a truncated path is useless for finding the file again"
    assert _field(report, "Tested stream index") == "4"
    assert _field(report, "Renderer equivalence") == StreamEquivalence.BY_HIGHEST_RESOLUTION.value


def test_the_report_bounds_the_ffmpeg_diagnostic_text():
    verdict, _ = _check(outcome=_decoded(returncode=1, stderr="E" * 5000))
    report = format_sample_report(verdict)
    assert len(verdict.diagnostic) < 500
    assert "…" in verdict.diagnostic
    assert len(report) < 3000, "a status surface is not a log sink"


def test_an_unestablished_frame_count_is_reported_as_such_not_as_zero():
    verdict, _ = _check(outcome=SampleDecodeOutcome(timed_out=True))
    report = format_sample_report(verdict)
    assert _field(report, "Observed output frames") == "not established"
    assert _field(report, "FFmpeg exit code") == "not reached"


def test_the_report_names_the_duration_provenance():
    verdict, _ = _check(probe=lambda _p: _probe_ok(_stream(duration=_ABSENT),
                                                   container={"duration": "33.0"}))
    assert "from container" in format_sample_report(verdict)
    verdict2, _ = _check()
    assert "from stream" in format_sample_report(verdict2)


def test_a_changed_source_names_its_window_in_the_report():
    verdict, _ = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), _St(42, 9)))
    report = format_sample_report(verdict)
    assert SampleChangeWindow.DURING_DECODE.value in report
    assert Stability.CHANGED.value in report


def test_math_helpers_never_leak_a_nonfinite_number_into_a_report():
    verdict, _ = _check(probe=lambda _p: _probe_ok(_stream(duration="nan"), container={}),
                        seek_seconds=0.0)
    # A NaN duration is not a usable duration, so it is reported as unusable metadata.
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    report = format_sample_report(verdict)
    assert "nan" not in report.lower()
    assert all(math.isfinite(v) for v in
               (verdict.duration, verdict.fps, verdict.requested_seek, verdict.elapsed_seconds))
