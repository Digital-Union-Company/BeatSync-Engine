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
import stat as stat_module

import pytest

from beatsync_fork.input_manager import SUPPORTED_VIDEO_EXTENSIONS
from beatsync_fork.media_verify import DEFAULT_PROBE_TIMEOUT_SECONDS, ProbeOutcome, WorkBudget
from beatsync_fork.media_sample import (
    DEFAULT_SAMPLE_BUDGET_SECONDS,
    DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS,
    DEFAULT_SAMPLE_FRAMES,
    MAX_SAMPLE_FRAMES,
    SAMPLE_ONLY_TEXT,
    SOURCE_COUNT_CAP,
    WORKER_CAP,
    _EXECUTION_TEXT,
    DecodeExecution,
    DurationSource,
    FrameEvidence,
    SampleChangeWindow,
    SampleDecodeOutcome,
    SampleStatus,
    SampleVerdict,
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
    """A synthetic ``os.stat_result``: the stability pair plus the mode the core now inspects.

    ``st_mode`` defaults to a regular file because that is what every source in this suite is
    meant to be; the non-regular cases pass a directory or device mode explicitly, which is the
    only way to exercise the ``S_ISREG`` gate without creating a device node.
    """

    def __init__(self, size, mtime_ns, mode=stat_module.S_IFREG | 0o644):
        self.st_size = size
        self.st_mtime_ns = mtime_ns
        self.st_mode = mode


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


@pytest.mark.parametrize("frames", [0, -1, None, "", "abc", 2.5, True,
                                    float("nan"), float("inf")])
def test_a_request_for_a_nonsense_frame_count_is_invalid(frames):
    verdict, launched = _check(requested_frames=frames)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert launched == [], "no child may be started for an invalid request"


@pytest.mark.parametrize("frames", [MAX_SAMPLE_FRAMES + 1, 31, 1000, 10 ** 20, 2 ** 63])
def test_a_request_above_the_frame_cap_never_reaches_the_decoder(frames):
    """The cap is the bound, not a default. The pure core owns it, not a future GUI validator."""
    verdict, launched = _check(requested_frames=frames)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert launched == [], "an oversized request must not widen the work a diagnostic performs"
    assert str(MAX_SAMPLE_FRAMES) in verdict.detail
    assert verdict.execution is DecodeExecution.NOT_REQUESTED


@pytest.mark.parametrize("frames", [1, 2, 29, MAX_SAMPLE_FRAMES])
def test_every_frame_count_inside_the_cap_is_accepted(frames):
    verdict, launched = _check(requested_frames=frames,
                               outcome=_decoded(stdout=_progress(frames=frames, blocks=1)))
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK
    assert launched[0]["requested_frames"] == frames


def test_the_cap_equals_the_approved_policy_and_is_not_merely_the_default():
    assert MAX_SAMPLE_FRAMES == DEFAULT_SAMPLE_FRAMES == 30


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
    assert SAMPLE_ONLY_TEXT not in verdict.detail, \
        "the coverage sentence belongs to the report footer once, not also to the detail"
    assert verdict.execution is DecodeExecution.REQUESTED_FRAMES_PRODUCED
    assert verdict.decoder_was_launched is True
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

def test_multiple_progress_blocks_resolve_to_the_last_COMPLETED_block():
    evidence = parse_progress_output(
        "frame=10\nprogress=continue\nframe=20\nprogress=continue\n"
        "frame=30\ndup_frames=0\ndrop_frames=0\nprogress=end\n")
    assert evidence.frames == 30
    assert evidence.saw_end is True
    assert evidence.blocks == 3


def test_a_terminal_block_without_a_frame_key_inherits_nothing_and_cannot_succeed():
    """The regression that makes the sole positive status unreachable on unverified evidence.

    ``frame=30`` belongs to a block that said ``progress=continue``. The terminal block carried no
    count at all, so there is no output-frame evidence — and a clean verdict assembled from an
    earlier block's number would be a verdict about a measurement nobody made.
    """
    evidence = parse_progress_output("frame=30\nprogress=continue\nprogress=end\n")
    assert evidence.frames is None, "an earlier block's count is not the terminal block's"
    assert evidence.saw_end is True
    assert evidence.malformed_frame is False, "no frame key was in that block, so nothing malformed"
    status, detail = classify_decode(_decoded(stdout="irrelevant", stderr=""), evidence,
                                     DEFAULT_SAMPLE_FRAMES)
    assert status is SampleStatus.EVIDENCE_UNTRUSTWORTHY
    assert "no frame count" in detail


def test_terminal_block_counters_are_independent_of_earlier_blocks():
    """``dup_frames=0`` seen earlier is not a measurement of the final block."""
    evidence = parse_progress_output(
        "frame=29\ndup_frames=0\ndrop_frames=0\nprogress=continue\n"
        "frame=30\nprogress=end\n")
    assert evidence.frames == 30
    assert evidence.dup_frames is None and evidence.drop_frames is None
    report = format_sample_report(SampleVerdict(
        path="J:/a.mp4", status=SampleStatus.OUTPUT_FRAMES_OK, observed_frames=30,
        dup_frames=evidence.dup_frames, execution=DecodeExecution.REQUESTED_FRAMES_PRODUCED))
    assert "UNKNOWN is not a measurement of zero" in report


def test_a_malformed_terminal_count_is_not_replaced_by_an_earlier_valid_one():
    evidence = parse_progress_output("frame=30\nprogress=continue\nframe=abc\nprogress=end\n")
    assert evidence.frames is None and evidence.malformed_frame is True
    status, _ = classify_decode(_decoded(stdout="x"), evidence, DEFAULT_SAMPLE_FRAMES)
    assert status is SampleStatus.EVIDENCE_UNTRUSTWORTHY


def test_an_earlier_terminal_marker_cannot_authorize_a_later_contradiction():
    evidence = parse_progress_output(
        "frame=30\ndup_frames=0\ndrop_frames=0\nprogress=end\n"
        "frame=0\ndup_frames=0\ndrop_frames=0\nprogress=end\n")
    assert evidence.frames == 0, "the last completed block wins, even over an earlier end"
    status, _ = classify_decode(_decoded(stdout="x", stderr=""), evidence, DEFAULT_SAMPLE_FRAMES)
    assert status is SampleStatus.ZERO_FRAMES


def test_a_lower_final_count_is_not_raised_by_an_earlier_higher_one():
    evidence = parse_progress_output(
        "frame=30\nprogress=continue\nframe=7\ndup_frames=0\ndrop_frames=0\nprogress=end\n")
    assert evidence.frames == 7
    status, _ = classify_decode(_decoded(stdout="x", stderr=""), evidence, DEFAULT_SAMPLE_FRAMES)
    assert status is SampleStatus.UNDERFILLED_UNVERIFIED


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


def test_partial_trailing_output_completes_no_block_and_contributes_nothing():
    """A half-written block is not evidence, and must not displace the block that completed.

    This previously asserted ``frames == 2`` — reading a count out of a block whose ``progress``
    marker never arrived. The trailing write is discarded instead, so the last COMPLETED block
    remains the evidence.
    """
    evidence = parse_progress_output("frame=10\nprogress=continue\nframe=2")
    assert evidence.frames == 10, "the completed block's count, not the partial one's"
    assert evidence.saw_end is False
    assert evidence.blocks == 1, "a partial write completes no block"


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
    assert roots <= {"__future__", "json", "os", "stat", "time", "dataclasses", "enum", "typing",
                     "beatsync_fork"}, roots

    used = _referenced(tree)
    for banned in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "qwen", "llama",
                   "stage5", "subprocess", "open", "os.remove", "shutil"):
        assert banned not in used, banned


def test_the_only_os_call_in_the_pure_core_is_the_default_stat():
    """``os`` is imported for exactly one reason: ``st_mode`` is unreachable without a stat call.

    The module performs no other filesystem operation, and this pins that — an ``os.`` attribute
    creeping in later is how a read-only diagnostic quietly acquires the ability to write.
    """
    tree = _tree(_MEDIA_SAMPLE)
    os_calls = {ast.unparse(node) for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "os"}
    assert os_calls == {"os.stat"}, os_calls


# ---------------------------------------------------------------------------
# totality: an ordinary failure anywhere becomes a verdict, never an escape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [OSError("refused"), ValueError("bad"), RuntimeError("boom"),
                                 AttributeError("attr"), KeyError("k"), TypeError("type"),
                                 Exception("plain")])
def test_a_probe_that_raises_becomes_a_no_opinion_verdict(exc):
    """``decode`` was contained and ``probe`` was not. An injected callable may raise anything."""
    verdict, launched = _check(probe=lambda _p: (_ for _ in ()).throw(exc))
    assert verdict.status is SampleStatus.NOT_RUN, "tooling failed; we hold no opinion on the file"
    assert type(exc).__name__ in verdict.detail
    assert launched == []
    assert verdict.execution is DecodeExecution.NOT_REQUESTED
    assert verdict.decoder_was_launched is False


def test_the_probe_detail_distinguishes_a_refusal_from_an_internal_error():
    refused, _ = _check(probe=lambda _p: (_ for _ in ()).throw(OSError("ffprobe denied")))
    internal, _ = _check(probe=lambda _p: (_ for _ in ()).throw(RuntimeError("logic slip")))
    assert "could not be run" in refused.detail
    assert "unexpected internal error" in internal.detail


@pytest.mark.parametrize("exc", [RuntimeError("stat exploded"), AttributeError("mode"),
                                 MemoryError()])
def test_an_unexpected_source_observation_failure_fails_closed(exc):
    verdict, launched = _check(stat=_stat_seq(exc))
    assert verdict.status is SampleStatus.SOURCE_MISSING
    assert launched == []


def test_an_unexpected_post_decode_observation_failure_never_grants_a_clean_result():
    verdict, launched = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), RuntimeError("vanished")))
    assert verdict.status is SampleStatus.EVIDENCE_UNTRUSTWORTHY
    assert verdict.stability is Stability.UNVERIFIED
    assert len(launched) == 1


def test_a_timeout_survives_an_unverifiable_post_decode_observation():
    """D: real timeout and FFmpeg-failure evidence outranks the stability gate."""
    timeout, _ = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), OSError("locked")),
                        outcome=SampleDecodeOutcome(timed_out=True))
    assert timeout.status is SampleStatus.TIMEOUT
    assert timeout.stability is Stability.UNVERIFIED
    failed, _ = _check(stat=_stat_seq(_St(10, 7), _St(10, 7), OSError("locked")),
                       outcome=_decoded(returncode=1, stderr="boom"))
    assert failed.status is SampleStatus.FFMPEG_FAILED


@pytest.mark.parametrize("returncode", ["x", "", None, object(), 1.5])
def test_a_malformed_probe_exit_code_is_never_read_as_success(returncode):
    class _Outcome:
        error = ""
        timed_out = False
        stdout = ""
        stderr = "nothing useful"

    _Outcome.returncode = returncode
    verdict, launched = _check(probe=lambda _p: _Outcome())
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    assert launched == []


@pytest.mark.parametrize("returncode", ["x", object(), None, 1.5])
def test_a_malformed_decode_exit_code_becomes_a_seam_failure_not_a_zero(returncode):
    verdict, _ = _check(outcome=SampleDecodeOutcome(
        returncode=returncode, stdout=_progress(), stderr=""))
    assert verdict.status is SampleStatus.NOT_RUN
    assert verdict.returncode is None
    assert "malformed exit code" in verdict.detail
    assert verdict.execution is DecodeExecution.START_NOT_CONFIRMED
    assert verdict.decoder_was_launched is False


def test_a_malformed_decoder_outcome_object_is_reported_not_raised():
    class _Junk:
        stdout = ""
        stderr = ""

    verdict, _ = _check(outcome=_Junk())
    assert verdict.status is SampleStatus.NOT_RUN
    assert "missing" in verdict.detail
    assert verdict.decoder_was_launched is False


def test_a_decode_that_returns_none_is_reported_not_raised():
    verdict, _ = _check(decode=lambda _p, **_kw: None)
    assert verdict.status is SampleStatus.NOT_RUN
    assert "NoneType" in verdict.detail
    assert verdict.decoder_was_launched is False


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_base_exceptions_still_propagate_from_every_seam(exc):
    """Containment is for ordinary failures. A cancelled operation is not a file problem.

    ``_stat_seq`` cannot carry these — it re-raises ``Exception`` instances only, and neither of
    these is one — so the stat seam gets its own raiser here.
    """
    def raiser(*_a, **_kw):
        raise exc()

    with pytest.raises(exc):
        _check(probe=raiser)
    with pytest.raises(exc):
        _check(decode=raiser)
    with pytest.raises(exc):
        _check(stat=raiser)


# ---------------------------------------------------------------------------
# decoder_was_launched: "requested" is not "a child started"
# ---------------------------------------------------------------------------

def test_decoder_was_launched_is_false_wherever_no_child_is_known_to_have_started():
    cases = {
        "probe seam refused": {"probe": lambda _p: ProbeOutcome(error="FileNotFoundError")},
        "probe raised": {"probe": lambda _p: (_ for _ in ()).throw(OSError("x"))},
        "invalid request": {"requested_frames": 0},
        "source missing": {"stat": _stat_seq(OSError("gone"))},
        "metadata unusable": {"probe": lambda _p: ProbeOutcome(returncode=1, stderr="moov")},
        "seek out of range": {"seek_seconds": 999.0},
        "budget exhausted": {"budget_seconds": 10, "decode_timeout": 30},
        "cross-phase change": {"stat": _stat_seq(_St(10, 7), _St(99, 8), _St(99, 8))},
        "spawn refused": {"outcome": SampleDecodeOutcome(error="OSError: denied")},
        "seam raised": {"decode": lambda _p, **_kw: (_ for _ in ()).throw(RuntimeError("h"))},
    }
    for label, kw in cases.items():
        verdict, _ = _check(**kw)
        assert verdict.decoder_was_launched is False, label
        assert verdict.execution in (DecodeExecution.NOT_REQUESTED,
                                     DecodeExecution.START_NOT_CONFIRMED), label


def test_decoder_was_launched_is_true_only_once_a_child_demonstrably_ran():
    for kw in ({}, {"outcome": _decoded(returncode=1, stderr="x")},
               {"outcome": SampleDecodeOutcome(timed_out=True)},
               {"outcome": _decoded(stdout=_progress(frames=4))}):
        verdict, _ = _check(**kw)
        assert verdict.decoder_was_launched is True, kw
    timed_out, _ = _check(outcome=SampleDecodeOutcome(timed_out=True))
    assert timed_out.execution is DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, \
        "a child had to start in order to be killed by its own timeout"


def test_decoder_was_launched_reads_the_execution_field_not_a_status_label():
    """Structural: inferring a launch from NOT_RUN is exactly the defect being prevented.

    Asserted over REFERENCED names, not over unparsed text — the docstring legitimately explains
    which status label must not be consulted, and a substring ban would trip on its own
    explanation.
    """
    used = _referenced(_func(_tree(_MEDIA_SAMPLE), "decoder_was_launched"))
    assert "execution" in used and "DecodeExecution" in used
    assert "SampleStatus" not in used, "a status label is not launch evidence"


# ---------------------------------------------------------------------------
# attached pictures survive into the report
# ---------------------------------------------------------------------------

def test_a_lone_attached_picture_is_disclosed_all_the_way_into_the_report():
    verdict, launched = _check(
        probe=lambda _p: _probe_ok(_stream(index=0, disposition={"attached_pic": 1})),
        outcome=_decoded(stdout=_progress(frames=1, blocks=1)))
    assert verdict.attached_pic is True, "computed at selection and carried, not discarded"
    report = format_sample_report(verdict)
    assert "attached picture" in report
    assert "cover art" in report
    assert "underfill" in report, "a one-frame cover art explains a short result"
    assert verdict.status is SampleStatus.UNDERFILLED_UNVERIFIED
    assert len(launched) == 1


def test_an_ordinary_stream_is_never_described_as_an_attached_picture():
    verdict, _ = _check()
    assert verdict.attached_pic is False
    assert "attached picture" not in format_sample_report(verdict)


def test_multiple_video_streams_with_an_attached_picture_still_fail_closed():
    """The accepted ambiguity policy is unchanged by the disclosure fix."""
    verdict, launched = _check(probe=lambda _p: _probe_ok(
        _stream(index=0, width=1920, height=1080),
        _stream(index=1, width=600, height=600, disposition={"attached_pic": 1})))
    assert verdict.status is SampleStatus.STREAM_AMBIGUOUS
    assert verdict.equivalence is StreamEquivalence.NOT_ESTABLISHED
    assert launched == []


# ---------------------------------------------------------------------------
# request and path validation, before any child
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [None, 123, b"J:/lib/clip.mp4", object(), ["J:/a.mp4"]])
def test_a_path_that_is_not_a_string_is_an_invalid_request_not_a_TypeError(path):
    verdict, launched = _check(path=path)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert "must be a string" in verdict.detail
    assert launched == []


@pytest.mark.parametrize("path", ["", "   "])
def test_an_empty_path_is_an_invalid_request(path):
    verdict, launched = _check(path=path)
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert launched == []


@pytest.mark.parametrize("path", ["J:/lib/clip.mp4", "J:/lib/clip.MP4", "J:/lib/show.mkv",
                                  "J:/lib/show.MKV", r"J:\lib\sub dir\ロゴ 01.mkv"])
def test_supported_extensions_are_accepted_case_insensitively(path):
    verdict, launched = _check(path=path)
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK, path
    assert len(launched) == 1


@pytest.mark.parametrize("path", ["J:/lib/notes.txt", "J:/lib/clip.avi", "J:/lib/clip.mp4.part",
                                  "J:/lib/clip", "J:/lib/.mkv", "J:/lib/clip.mp4/"])
def test_an_unsupported_extension_is_refused_before_any_child(path):
    verdict, launched = _check(path=path)
    assert verdict.status is SampleStatus.INVALID_REQUEST, path
    assert launched == []
    assert verdict.execution is DecodeExecution.NOT_REQUESTED


def test_the_extension_list_is_imported_not_restated():
    """Structural: a third copy of ".mp4/.mkv" is a drift waiting to happen."""
    tree = _tree(_MEDIA_SAMPLE)
    assert "SUPPORTED_VIDEO_EXTENSIONS" in _referenced(tree)
    literals = {node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value.lower() in (".mp4", ".mkv")}
    assert literals == set(), literals
    assert SUPPORTED_VIDEO_EXTENSIONS == frozenset({".mp4", ".mkv"})


@pytest.mark.parametrize("mode", [stat_module.S_IFDIR | 0o755, stat_module.S_IFCHR | 0o666,
                                  stat_module.S_IFIFO | 0o644, 0])
def test_a_non_regular_source_is_refused_before_any_child(mode):
    verdict, launched = _check(stat=_stat_seq(_St(4096, 7, mode=mode)))
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert "not a regular file" in verdict.detail
    assert launched == []


def test_a_real_directory_with_a_video_suffix_is_refused(tmp_path):
    """Injected success must not be able to bless a directory."""
    folder = tmp_path / "season one.mkv"
    folder.mkdir()
    verdict = run_sample_check(str(folder), 1.0, probe=lambda _p: _probe_ok(_stream()),
                               decode=lambda _p, **_kw: _decoded(), clock=iter_clock())
    assert verdict.status is SampleStatus.INVALID_REQUEST
    assert verdict.decoder_was_launched is False


def test_a_real_regular_file_passes_the_new_gates(tmp_path):
    source = tmp_path / "clip.MKV"
    source.write_bytes(b"not really a video")
    verdict = run_sample_check(str(source), 1.0, probe=lambda _p: _probe_ok(_stream()),
                               decode=lambda _p, **_kw: _decoded(), clock=iter_clock())
    assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK


# ---------------------------------------------------------------------------
# duplicate absolute stream indices
# ---------------------------------------------------------------------------

def test_duplicate_absolute_video_indices_fail_closed():
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=1, width=1920, height=1080),
        _stream(index=1, width=640, height=360)))
    assert not selection.ok
    assert "distinct absolute index" in selection.ambiguous_reason
    assert selection.invalid_reason == ""


def test_a_duplicate_index_container_never_reaches_the_decoder():
    verdict, launched = _check(probe=lambda _p: _probe_ok(
        _stream(index=2, width=1920, height=1080),
        _stream(index=2, width=1280, height=720)))
    assert verdict.status is SampleStatus.STREAM_AMBIGUOUS
    assert verdict.equivalence is StreamEquivalence.NOT_ESTABLISHED
    assert launched == []


def test_a_resolution_tie_between_distinct_indices_is_still_resolvable():
    """The documented lowest-index rule is unchanged; only true duplicates fail closed."""
    selection = select_renderer_equivalent_stream(_payload(
        _stream(index=3, width=1920, height=1080),
        _stream(index=1, width=1920, height=1080)))
    assert selection.ok
    assert selection.stream.index == 1
    assert selection.stream.equivalence is StreamEquivalence.BY_HIGHEST_RESOLUTION


# ---------------------------------------------------------------------------
# timing means one thing per field
# ---------------------------------------------------------------------------

def test_elapsed_covers_the_whole_check_on_every_path_and_the_decode_is_its_own_field():
    """One field that silently switched between "whole check" and "decode only" is unreadable."""
    # 1 s per clock read: the early return still bills the probe phase it actually spent.
    early, _ = _check(seek_seconds=999.0, clock=iter_clock(step=1.0))
    assert early.elapsed_seconds > 0
    assert early.decode_seconds is None, "no decode was requested, so there is no decode time"
    assert _field(format_sample_report(early), "Decode call") == "not reached"

    full, _ = _check(clock=iter_clock(step=1.0))
    assert full.decode_seconds == pytest.approx(1.0)
    assert full.elapsed_seconds > full.decode_seconds, \
        "the whole check includes the probe and the stat observations, not just the child"
    report = format_sample_report(full)
    assert _field(report, "Elapsed (whole check)").endswith("s")
    assert _field(report, "Decode call").endswith("s")


def test_an_unselected_stream_is_never_reported_as_a_zero_measurement():
    verdict, _ = _check(probe=lambda _p: ProbeOutcome(returncode=0, stdout="not json"))
    assert verdict.status is SampleStatus.METADATA_UNUSABLE
    report = format_sample_report(verdict)
    assert _field(report, "Stream codec / size / fps") == "unknown / unknown / unknown"
    assert _field(report, "Reported duration") == "not established"
    assert "0x0" not in report and "0.000s (from" not in report


def test_a_selected_stream_still_reports_its_real_geometry():
    verdict, _ = _check()
    report = format_sample_report(verdict)
    assert _field(report, "Stream codec / size / fps") == "h264 / 1920x1080 / 30.000"
    assert "60.000s (from stream)" in _field(report, "Reported duration")


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

def test_the_coverage_statement_is_hypothetical_and_appears_exactly_once():
    """It is unconditional, so it must be true of a report whose decode never started.

    The previous wording — "One sampled window of one stream WAS DECODED..." — was a false
    statement of fact on every non-executed outcome, and it was appended to the footer while also
    being embedded in the success detail, so a clean result carried it twice.
    """
    for status in SampleStatus:
        report = format_sample_report(SampleVerdict(path="J:/a.mp4", status=status, detail="d"))
        assert report.count(SAMPLE_ONLY_TEXT) == 1, status
        assert "size and modification time only" in report
        assert report.lower().count("even a successful sample") == 1, status


@pytest.mark.parametrize("execution", list(DecodeExecution))
def test_no_report_claims_a_decode_that_did_not_happen(execution):
    """No past-tense successful-decode wording outside the stages where a child provably ran."""
    report = format_sample_report(SampleVerdict(
        path="J:/a.mp4", status=SampleStatus.NOT_RUN, detail="d", execution=execution))
    for lie in ("was decoded to a discarded output", "frames were produced at the requested",
                "window of one stream was decoded"):
        assert lie not in report, f"{execution.value} claims: {lie}"
    assert _field(report, "Decode") , "every report states what actually happened"


#: The five outcomes where a terminal block reports the FULL requested count and the sample is
#: still non-clean, because an earlier precedence tier outranked the frame tiers. Each is the exact
#: shape that makes a frame-completeness claim on the ``Decode:`` line false, so they are kept in
#: one table and reused by every execution-truthfulness test below.
#:
#: Stored as kwarg FACTORIES, not literal kwargs: ``_stat_seq`` returns a stateful closure whose
#: call counter clamps to the last result once exhausted, so a shared instance would silently
#: report a stable source on its second use and quietly turn these cases back into clean successes.
_FULL_COUNT_BUT_NOT_CLEAN = [
    ("decoder errors at -v error",
     lambda: {"outcome": _decoded(stderr="decoder reported an error")},
     SampleStatus.OUTPUT_WITH_DECODER_ERRORS),
    ("source changed during the decode",
     lambda: {"stat": _stat_seq(_St(10, 7), _St(10, 7), _St(99, 99))},
     SampleStatus.SOURCE_CHANGED),
    ("post-decode observation could not be taken",
     lambda: {"stat": _stat_seq(_St(10, 7), _St(10, 7), OSError("vanished"))},
     SampleStatus.EVIDENCE_UNTRUSTWORTHY),
    ("timeout after partial progress reached the full count",
     lambda: {"outcome": SampleDecodeOutcome(
         timed_out=True, stdout=_progress(end=False).encode("utf-8"))},
     SampleStatus.TIMEOUT),
    ("non-zero exit after the full count was reported",
     lambda: {"outcome": _decoded(returncode=1, stderr="muxer failed")},
     SampleStatus.FFMPEG_FAILED),
]


def test_every_status_report_states_its_execution_stage_factually():
    cases = [
        ({"seek_seconds": 999.0}, DecodeExecution.NOT_REQUESTED, "no decode was requested"),
        ({"requested_frames": 0}, DecodeExecution.NOT_REQUESTED, "no decode was requested"),
        ({"outcome": SampleDecodeOutcome(error="OSError: nope")},
         DecodeExecution.START_NOT_CONFIRMED, "no FFmpeg child was confirmed to have started"),
        ({"outcome": SampleDecodeOutcome(timed_out=True)},
         DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, "no clean sample verdict was established"),
        ({"outcome": _decoded(stdout=_progress(frames=3))},
         DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, "no clean sample verdict was established"),
        ({"outcome": _decoded(stdout=_progress(frames=0, blocks=1))},
         DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, "no clean sample verdict was established"),
        ({}, DecodeExecution.REQUESTED_FRAMES_PRODUCED, "produced the requested output frames"),
    ]
    # The axis that matters most, and the one an earlier version of this test never varied: a
    # NON-CLEAN status whose frame count REACHED the request. Underfilled cases alone cannot
    # detect a Decode line that claims underproduction, because there it happens to be true.
    cases += [(make_kw(), DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, "no clean sample verdict was "
               "established") for _label, make_kw, _status in _FULL_COUNT_BUT_NOT_CLEAN]
    for kw, expected, needle in cases:
        verdict, _ = _check(**kw)
        assert verdict.execution is expected, (kw, verdict.status)
        assert needle in _field(format_sample_report(verdict), "Decode"), kw


@pytest.mark.parametrize("label,make_kw,expected_status", _FULL_COUNT_BUT_NOT_CLEAN,
                         ids=[c[0] for c in _FULL_COUNT_BUT_NOT_CLEAN])
def test_a_full_frame_count_with_a_non_clean_status_is_never_reported_as_underproduction(
        label, make_kw, expected_status):
    """The R3 blocker: five paths where the status is right and the execution label lied.

    ``classify_decode``'s tiers 1-6 outrank every frame tier, so "did not earn the sole positive
    status" and "produced fewer frames than requested" are different facts. Deriving the second
    from the first put a false sentence in the report -- on OUTPUT_WITH_DECODER_ERRORS it sat in
    the same report as a Detail line confirming all the requested frames came out.
    """
    verdict, launched = _check(**make_kw())
    assert verdict.status is expected_status, label
    assert len(launched) == 1, "a child was requested in every one of these cases"
    assert verdict.observed_frames == DEFAULT_SAMPLE_FRAMES, \
        f"{label}: the premise is a terminal block reporting the full count"
    assert verdict.requested_frames == DEFAULT_SAMPLE_FRAMES

    assert verdict.execution is DecodeExecution.RAN_WITHOUT_CLEAN_RESULT, label
    assert verdict.decoder_was_launched is True, "the child demonstrably ran"

    report = format_sample_report(verdict)
    decode_line = _field(report, "Decode")
    # The line may not assert a shortfall the frame evidence in the same report contradicts...
    for lie in ("did not produce the full", "did not produce the requested",
                "fewer than", "underproduc", "incomplete output"):
        assert lie not in decode_line.lower(), f"{label}: Decode line claims {lie!r}"
    # ...nor may it claim the success it did not earn.
    for overclaim in ("produced the requested output frames", "succeeded", "verified"):
        assert overclaim not in decode_line.lower(), f"{label}: Decode line claims {overclaim!r}"
    assert "no clean sample verdict was established" in decode_line.lower(), label
    assert _field(report, "Observed output frames") == str(DEFAULT_SAMPLE_FRAMES), label
    assert _field(report, "Result") == expected_status.value, label


#: Every case the report-consistency matrix walks, keyed by label. Kept as a module-level table of
#: FACTORIES for the same reason :data:`_FULL_COUNT_BUT_NOT_CLEAN` is (``_stat_seq`` is stateful),
#: and shared so the rendered-line matrix and the field matrix cannot drift apart.
_REPORT_MATRIX_CASES = {
    "clean success": lambda: {},
    "underfilled": lambda: {"outcome": _decoded(stdout=_progress(frames=3))},
    "zero frames": lambda: {"outcome": _decoded(stdout=_progress(frames=0, blocks=1))},
    "no terminal block": lambda: {"outcome": _decoded(stdout="frame=30\nfps=30\n")},
    "malformed frame count": lambda: {"outcome": _decoded(stdout="frame=abc\nprogress=end\n")},
    "count above the request": lambda: {
        "outcome": _decoded(stdout=_progress(frames=31, blocks=1))},
    "dup frames positive": lambda: {"outcome": _decoded(stdout=_progress(dup=4))},
    "timeout, no output": lambda: {"outcome": SampleDecodeOutcome(timed_out=True)},
    "refused spawn": lambda: {"outcome": SampleDecodeOutcome(error="WinError 2")},
    "malformed exit code": lambda: {"outcome": SampleDecodeOutcome(
        returncode="x", stdout=_progress(), stderr="junk on stderr")},
    "seek out of range": lambda: {"seek_seconds": 999.0},
    "invalid request": lambda: {"requested_frames": 0},
    "budget exhausted": lambda: {"budget_seconds": 1.0},
}


def _matrix_cases():
    """Fresh kwargs for every case, including the five full-count-but-not-clean shapes."""
    cases = {label: make_kw() for label, make_kw in _REPORT_MATRIX_CASES.items()}
    cases.update({label: make_kw() for label, make_kw, _ in _FULL_COUNT_BUT_NOT_CLEAN})
    return cases


def test_the_decode_line_never_contradicts_the_observed_frame_evidence():
    """Full execution-state x status matrix: no report may argue with itself.

    Walks every reachable (status, execution) pair and cross-checks the ``Decode:`` line against
    the ``Observed output frames`` line in the SAME report. A shortfall claim is permitted only
    where the evidence actually shows one; a production claim only where the count reached the
    request. Anything else is a report that contradicts itself.

    Both frame quantities are read from the **rendered report**, not from the verdict, which is the
    whole point: this test's job is to police what the operator is shown. An earlier version read
    ``verdict.observed_frames`` while its docstring claimed otherwise, and a formatter that clamped
    the displayed count to the request survived the entire suite as a result. See
    :func:`test_the_rendered_observed_frame_line_is_the_verdicts_own_evidence` for the direct pin.
    """
    seen_executions = set()
    for label, kw in _matrix_cases().items():
        verdict, _ = _check(**kw)
        seen_executions.add(verdict.execution)
        report = format_sample_report(verdict)
        decode_line = _field(report, "Decode").lower()

        # Read BOTH quantities off the report. "not established" is the rendered unknown.
        shown = _field(report, "Observed output frames")
        requested = _field(report, "Requested output frames")
        assert requested.isdigit(), f"{label}: requested count must always render as a number"
        observed = None if shown == "not established" else int(shown)
        assert shown == "not established" or shown.isdigit(), \
            f"{label}: observed count renders as a number or 'not established', got {shown!r}"
        shortfall_known = observed is not None and observed < int(requested)

        if "did not produce the full" in decode_line or "underproduc" in decode_line:
            assert shortfall_known, f"{label}: shortfall claimed, report shows {shown!r}"
        if "produced the requested output frames" in decode_line:
            assert observed is not None and observed >= int(requested), \
                f"{label}: production claimed, report shows {shown!r}"
            assert _field(report, "Result") == SampleStatus.OUTPUT_FRAMES_OK.value, label
        # A Decode line that DENIES confirmed child-start evidence may not print a count either.
        if "no ffmpeg child was confirmed to have started" in decode_line:
            assert shown == "not established", \
                (f"{label}: report denies a confirmed child start while showing {shown!r} "
                 f"output frames from that child's own progress stream")
        # decoder_was_launched must agree with the execution field on every single pair.
        assert verdict.decoder_was_launched is (
            verdict.execution in (DecodeExecution.RAN_WITHOUT_CLEAN_RESULT,
                                  DecodeExecution.REQUESTED_FRAMES_PRODUCED)), label

    assert seen_executions == set(DecodeExecution), \
        f"matrix must exercise every execution state, missed {set(DecodeExecution) - seen_executions}"


def test_the_rendered_observed_frame_line_is_the_verdicts_own_evidence():
    """``Observed output frames`` renders ``observed_frames`` verbatim -- never clamped, never faked.

    The gap this closes: every other report-truthfulness test either compared the Decode line to
    the verdict *field*, or pinned the rendered line only where it happened to equal the request.
    A formatter rendering ``min(observed, requested)`` therefore passed all 241 tests while hiding
    the one case the pin exists for -- a count ABOVE the request, which is not a cosmetic oddity
    but the actual evidence behind ``SAMPLE_EVIDENCE_UNTRUSTWORTHY``.
    """
    for label, kw in _matrix_cases().items():
        verdict, _ = _check(**kw)
        report = format_sample_report(verdict)
        expected = ("not established" if verdict.observed_frames is None
                    else str(verdict.observed_frames))
        assert _field(report, "Observed output frames") == expected, label
        assert _field(report, "Requested output frames") == str(verdict.requested_frames), label
        assert _field(report, "Result") == verdict.status.value, label


def test_an_output_count_above_the_request_is_shown_not_clamped():
    """The mandatory case: 31 observed against 30 requested renders as 31.

    ``-frames:v 30`` bounds output frames, so 31 contradicts the flag and the counter itself is
    what stops being trustworthy. Clamping the display to 30 would conceal the anomaly behind a
    number that looks correct, leaving an operator a status with no visible reason.
    """
    verdict, launched = _check(outcome=_decoded(stdout=_progress(frames=31, blocks=1)))
    assert verdict.status is SampleStatus.EVIDENCE_UNTRUSTWORTHY
    assert verdict.requested_frames == DEFAULT_SAMPLE_FRAMES == 30
    assert verdict.observed_frames == 31, "the verdict keeps the real count"
    assert len(launched) == 1

    report = format_sample_report(verdict)
    assert _field(report, "Observed output frames") == "31", "shown verbatim, never clamped to 30"
    assert _field(report, "Requested output frames") == "30"
    assert _field(report, "Result") == "SAMPLE_EVIDENCE_UNTRUSTWORTHY"
    assert "31" in _field(report, "Detail") and "30" in _field(report, "Detail"), \
        "the detail names both counts so the contradiction is legible"
    # And the execution line must not turn a counter anomaly into a shortfall or a success.
    decode_line = _field(report, "Decode").lower()
    assert "no clean sample verdict was established" in decode_line
    for lie in ("did not produce the full", "underproduc", "produced the requested output frames"):
        assert lie not in decode_line


@pytest.mark.parametrize("returncode", ["x", None, 1.5, [0], object()])
def test_a_malformed_exit_code_discards_its_untrusted_progress_evidence(returncode):
    """MINOR-1: a record whose exit code cannot be read is not a completed decode record.

    Its ``stdout`` carried a full 30-frame terminal block straight into the report, which then
    showed ``Observed output frames: 30`` beside a ``Decode:`` line denying any confirmed child
    start. Same defect class as deriving an execution label from a status: one report, two lines,
    sourced from facts of different strength. The progress stream is now dropped with the exit
    code, so the count reads "not established" exactly as it does for a refused spawn.
    """
    verdict, launched = _check(outcome=SampleDecodeOutcome(
        returncode=returncode, stdout=_progress(), stderr="some bounded stderr text"))
    assert len(launched) == 1, "the decode WAS requested"
    assert verdict.status is SampleStatus.NOT_RUN
    assert verdict.execution is DecodeExecution.START_NOT_CONFIRMED
    assert verdict.decoder_was_launched is False
    assert verdict.returncode is None, "never fabricated as a zero"
    assert "malformed exit code" in verdict.detail
    assert repr(returncode) in verdict.detail, "the malformed value itself is quoted"

    assert verdict.observed_frames is None, "untrusted progress is not evidence"
    assert verdict.dup_frames is None and verdict.drop_frames is None

    report = format_sample_report(verdict)
    assert _field(report, "Observed output frames") == "not established"
    assert _field(report, "Duplicated / dropped") == "unknown / unknown"
    assert _field(report, "FFmpeg exit code") == "not reached"
    assert "no FFmpeg child was confirmed to have started" in _field(report, "Decode")
    # stderr is bounded diagnostic context, not a measurement -- it SURVIVES.
    assert verdict.diagnostic == "some bounded stderr text"
    assert "some bounded stderr text" in report


def test_dropping_malformed_progress_does_not_touch_the_legitimate_paths():
    """The MINOR-1 guard is narrow: only an unreadable exit code loses its progress stream.

    A timeout's partial progress is the evidence most worth having, and a legitimate non-zero exit
    still has a readable code, so neither enters that branch. Pinned because the obvious over-broad
    fix -- discarding stdout whenever the result is not clean -- would destroy both.
    """
    timed_out, _ = _check(outcome=SampleDecodeOutcome(
        timed_out=True, stdout=_progress(frames=17, end=False).encode("utf-8")))
    assert timed_out.status is SampleStatus.TIMEOUT
    assert timed_out.observed_frames == 17, "a timeout KEEPS its partial progress"
    assert _field(format_sample_report(timed_out), "Observed output frames") == "17"

    failed, _ = _check(outcome=_decoded(returncode=1, stderr="muxer failed"))
    assert failed.status is SampleStatus.FFMPEG_FAILED
    assert failed.observed_frames == DEFAULT_SAMPLE_FRAMES, "a real exit code KEEPS its evidence"

    partial, _ = _check(outcome=_decoded(returncode=1, stdout=_progress(frames=4, blocks=1)))
    assert partial.status is SampleStatus.FFMPEG_FAILED
    assert partial.observed_frames == 4

    clean, _ = _check()
    assert clean.status is SampleStatus.OUTPUT_FRAMES_OK
    assert clean.observed_frames == DEFAULT_SAMPLE_FRAMES


def test_the_execution_model_stays_four_states_over_fifteen_statuses():
    """The R4 correction renamed one member; it did not grow the model or the taxonomy.

    Pinned because the cheap way to "fix" a contradicting label is to add a fifth state for
    "ran, completeness unknown", which would split the one question operators ask -- did a child
    run? -- across two members and give ``decoder_was_launched`` a third thing to read.
    """
    assert [m.name for m in DecodeExecution] == [
        "NOT_REQUESTED", "START_NOT_CONFIRMED", "RAN_WITHOUT_CLEAN_RESULT",
        "REQUESTED_FRAMES_PRODUCED"]
    assert len(list(SampleStatus)) == 15, "the status taxonomy is unchanged by this correction"
    assert set(_EXECUTION_TEXT) == set(DecodeExecution), "every state has operator prose"


@pytest.mark.parametrize("execution", list(DecodeExecution))
def test_no_execution_prose_asserts_a_frame_count_it_cannot_know(execution):
    """Only the one evidence-backed member may speak about frames produced.

    ``REQUESTED_FRAMES_PRODUCED`` is assigned solely from ``SAMPLE_OUTPUT_FRAMES_OK``, which
    already requires ``frame == requested`` in a completed terminal block, so it alone has the
    evidence. Every other member describes process state only.
    """
    prose = _EXECUTION_TEXT[execution].lower()
    if execution is DecodeExecution.REQUESTED_FRAMES_PRODUCED:
        assert "produced the requested output frames" in prose
        return
    for quantitative in ("did not produce the full", "fewer", "underproduc", "all frames",
                         "frames were produced"):
        assert quantitative not in prose, f"{execution.value} asserts {quantitative!r}"


def test_decoder_was_launched_contract_is_exact_over_all_four_states():
    launched = {DecodeExecution.RAN_WITHOUT_CLEAN_RESULT,
                DecodeExecution.REQUESTED_FRAMES_PRODUCED}
    for execution in DecodeExecution:
        verdict = SampleVerdict(path="J:/a.mp4", status=SampleStatus.NOT_RUN, detail="d",
                                execution=execution)
        assert verdict.decoder_was_launched is (execution in launched), execution


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


# ===========================================================================
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): the GUI integration.
# ===========================================================================
#
# Three tiers, and the distinction matters because only one of them proves concurrency:
#
#   1. A REAL `threading.Lock` driven by real threads, over the SHIPPED handler source extracted
#      by `ast` and exec'd against a namespace. This is what proves the three-way exclusion, the
#      holder attribution and the scoped revision contract -- not an AST reading.
#   2. AST assertions over the shipped `gui.py` for wiring facts a running test cannot see: which
#      lane the event joins, which widgets it reads, which components it writes, and -- the
#      resource contract -- that the `run_sample_check` call carries NO frame/timeout/budget
#      argument at all.
#   3. The already-imported pure core for the verdicts themselves.
#
# No Gradio, no FFmpeg, no FFprobe and no GUI is started anywhere below.

_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


class _FakeSampleRequest:
    def __init__(self, session_hash):
        self.session_hash = session_hash


class _FakeSampleGradio:
    """Just enough `gr` for the extracted handlers' annotations."""

    Request = _FakeSampleRequest


#: The sample-decode concurrency seam, in the order `gui.py` defines it. `_sample_decode_impl` and
#: `_verify_sources_impl` are the only members stubbed, because they are the parts that would need
#: FFmpeg and a library folder.
_SAMPLE_SEAM_FUNCTIONS = (
    "_mark_lock_holder", "_lock_holder_now", "_mark_verification",
    "_busy_message", "_verify_busy_message", "_sample_busy_message",
    "_verify_session_key", "_scoped_key",
    "_touch_verify_revision_locked", "_verify_revision",
    "_bump_verify_revision", "_begin_verify_session", "_end_verify_session",
    "_on_verify_sources_click", "_on_verify_invalidate",
    "_on_sample_decode_click", "_on_sample_invalidate",
)


def _gui_constant(tree, name):
    """The shipped module-level literal for `name` -- read, never restated.

    Same reasoning as S2-A's `_module_constant`: injecting a literal scope name or cap would make
    these tests blind to a change in the shipped value, which is how a mutated constant stays green.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not a module-level literal in gui.py")


def _load_p2_seam(cap=None):
    """Execute the REAL S2-B + S2-A concurrency seam over a real Lock and stubbed impls."""
    import threading
    tree = _tree(_GUI)
    nodes = [_func(tree, name) for name in _SAMPLE_SEAM_FUNCTIONS]
    state = {"sample_calls": [], "verify_calls": [],
             "sample_impl": lambda path, seek: "SAMPLE_REPORT",
             "verify_impl": lambda folder, recursive: "VERIFY_REPORT"}
    namespace = {
        "gr": _FakeSampleGradio,
        "threading": threading,
        "_RENDER_LOCK": threading.Lock(),
        "_VERIFY_FLAG_LOCK": threading.Lock(),
        # [FORK] S2-B P2 (Option B): the holder lookup reads the EXISTING active-render slot as
        # positive evidence of a render, so the seam needs the slot and its own lock. The two are
        # never held together -- the token is copied and released, then the slot is.
        "_ACTIVE_RENDER_SLOT_LOCK": threading.Lock(),
        "_active_render_slot": None,
        "_lock_holder": _gui_constant(tree, "_lock_holder"),
        "_LOCK_HOLDER_RENDER": _gui_constant(tree, "_LOCK_HOLDER_RENDER"),
        "_LOCK_HOLDER_VERIFY": _gui_constant(tree, "_LOCK_HOLDER_VERIFY"),
        "_LOCK_HOLDER_SAMPLE": _gui_constant(tree, "_LOCK_HOLDER_SAMPLE"),
        "_SCOPE_VERIFY": _gui_constant(tree, "_SCOPE_VERIFY"),
        "_SCOPE_SAMPLE": _gui_constant(tree, "_SCOPE_SAMPLE"),
        "_verify_revisions": dict(_gui_constant(tree, "_verify_revisions") or {}),
        "_VERIFY_REVISION_CAP": (_gui_constant(tree, "_VERIFY_REVISION_CAP")
                                 if cap is None else int(cap)),
        "_verify_active_session": _gui_constant(tree, "_verify_active_session"),
        "RENDER_BUSY_MESSAGE": "BUSY_RENDER_TO_RENDER",
        "VERIFY_BUSY_MESSAGE": "BUSY_RENDER_TO_VERIFY",
        "SAMPLE_BUSY_MESSAGE": "BUSY_RENDER_TO_SAMPLE",
        "MEDIA_BUSY_UNKNOWN_TEXT": "BUSY_UNKNOWN",
        "PREP_VERIFY_RENDER_BUSY_TEXT": "BUSY_VERIFY_TO_RENDER",
        "PREP_VERIFY_ALREADY_RUNNING_TEXT": "BUSY_VERIFY_TO_VERIFY",
        "PREP_VERIFY_SAMPLE_BUSY_TEXT": "BUSY_VERIFY_TO_SAMPLE",
        "PREP_VERIFY_STALE_TEXT": "STALE_VERIFY",
        "PREP_SAMPLE_RENDER_BUSY_TEXT": "BUSY_SAMPLE_TO_RENDER",
        "PREP_SAMPLE_VERIFY_BUSY_TEXT": "BUSY_SAMPLE_TO_VERIFY",
        "PREP_SAMPLE_ALREADY_RUNNING_TEXT": "BUSY_SAMPLE_TO_SAMPLE",
        "PREP_SAMPLE_STALE_TEXT": "STALE_SAMPLE",
    }

    def sample_impl(path, seek):
        state["sample_calls"].append((path, seek))
        return state["sample_impl"](path, seek)

    def verify_impl(folder, recursive):
        state["verify_calls"].append((folder, recursive))
        return state["verify_impl"](folder, recursive)

    namespace["_sample_decode_impl"] = sample_impl
    namespace["_verify_sources_impl"] = verify_impl
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _GUI, "exec"), namespace)
    return namespace, state


def _hold_as(ns, holder):
    """Take the shipped mutex the way a real holder does: acquire, THEN publish the holder."""
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "precondition: the mutex was free"
    ns["_mark_lock_holder"](holder)


def _drop(ns):
    ns["_mark_lock_holder"]("")
    ns["_RENDER_LOCK"].release()


# ---------------------------------------------------------------------------
# the full nine-cell caller x holder attribution table, plus the unknown holder
# ---------------------------------------------------------------------------

def test_the_three_way_busy_attribution_table_is_complete_and_never_guesses():
    """Nine cells, nine distinct truthful answers -- and an empty token is its own honest tenth.

    The boolean this replaced could express only two holders, and it derived "a render" from
    `False` BY ELIMINATION because the render path never marked itself. With three holders that
    inference is false in both new directions: a render refused during a sample would have claimed
    another render was running, and S2-A would have called a sample a render.
    """
    ns, _state = _load_p2_seam()
    callers = {"render": ns["_busy_message"],
               "verify": ns["_verify_busy_message"],
               "sample": ns["_sample_busy_message"]}
    expected = {
        ("render", "render"): "BUSY_RENDER_TO_RENDER",
        ("render", "verify"): "BUSY_RENDER_TO_VERIFY",
        ("render", "sample"): "BUSY_RENDER_TO_SAMPLE",
        ("verify", "render"): "BUSY_VERIFY_TO_RENDER",
        ("verify", "verify"): "BUSY_VERIFY_TO_VERIFY",
        ("verify", "sample"): "BUSY_VERIFY_TO_SAMPLE",
        ("sample", "render"): "BUSY_SAMPLE_TO_RENDER",
        ("sample", "verify"): "BUSY_SAMPLE_TO_VERIFY",
        ("sample", "sample"): "BUSY_SAMPLE_TO_SAMPLE",
    }
    for (caller, holder), want in expected.items():
        ns["_mark_lock_holder"](holder)
        got = callers[caller]()
        assert got == want, f"{caller} refused while {holder} holds -> {got!r}, want {want!r}"
    # No two cells for one caller may collapse: a refusal that cannot tell the operator which
    # operation to wait for is the defect this table exists to prevent.
    for caller in callers:
        texts = [expected[(caller, holder)] for holder in ("render", "verify", "sample")]
        assert len(set(texts)) == 3, (caller, texts)


def test_an_unpublished_holder_is_reported_honestly_and_never_as_a_render():
    """The real one-statement window: the mutex is held, the token is not set yet.

    `acquire()` and `_mark_lock_holder()` are two statements, so a caller that fails the acquire in
    between genuinely sees `''`. Naming a render there would be a fabricated attribution, and it is
    exactly what the old boolean did, since `False` meant both "nobody marked" and "a render".
    """
    ns, _state = _load_p2_seam()
    assert ns["_lock_holder_now"]() == "", "the shipped initial token is empty"
    for caller in ("_busy_message", "_verify_busy_message", "_sample_busy_message"):
        got = ns[caller]()
        assert got == "BUSY_UNKNOWN", f"{caller} guessed {got!r} for an unpublished holder"
        assert "RENDER" not in got, f"{caller} inferred a render from an empty token"


def test_the_holder_token_is_not_an_admission_authority():
    """A stale or wrong token must never admit or refuse on its own: the mutex decides."""
    ns, state = _load_p2_seam()
    # Marked 'sample' while the mutex is FREE -> a verification must still be admitted.
    ns["_mark_lock_holder"](ns["_LOCK_HOLDER_SAMPLE"])
    assert ns["_on_verify_sources_click"]("J:/lib", True) == "VERIFY_REPORT"
    assert state["verify_calls"] == [("J:/lib", True)]
    # Empty token while the mutex is HELD -> nobody may be admitted.
    assert ns["_RENDER_LOCK"].acquire(blocking=False)
    try:
        ns["_mark_lock_holder"]("")
        assert ns["_on_sample_decode_click"]("J:/a.mp4", 1.0) == "BUSY_UNKNOWN"
        assert state["sample_calls"] == [], "an unknown holder is still a refusal"
    finally:
        ns["_RENDER_LOCK"].release()


# ---------------------------------------------------------------------------
# three-way mutual exclusion, over a real Lock and real threads
# ---------------------------------------------------------------------------

def test_a_sample_is_refused_immediately_while_a_render_holds_the_mutex():
    ns, state = _load_p2_seam()
    _hold_as(ns, ns["_LOCK_HOLDER_RENDER"])
    try:
        out = ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, _FakeSampleRequest("S"))
    finally:
        _drop(ns)
    assert out == "BUSY_SAMPLE_TO_RENDER"
    assert state["sample_calls"] == [], "no decode work may begin while a render runs"
    assert ns["_verify_active_session"] == "", "a refusal must not pin"
    assert ns["_verify_revisions"] == {}, "a refusal must not insert a revision"


def test_a_sample_is_refused_immediately_while_a_verification_holds_the_mutex():
    ns, state = _load_p2_seam()
    _hold_as(ns, ns["_LOCK_HOLDER_VERIFY"])
    try:
        out = ns["_on_sample_decode_click"]("J:/a.mp4", 1.0)
    finally:
        _drop(ns)
    assert out == "BUSY_SAMPLE_TO_VERIFY"
    assert state["sample_calls"] == []


def test_a_verification_is_refused_while_a_sample_holds_the_mutex():
    ns, state = _load_p2_seam()
    _hold_as(ns, ns["_LOCK_HOLDER_SAMPLE"])
    try:
        out = ns["_on_verify_sources_click"]("J:/lib", True)
    finally:
        _drop(ns)
    assert out == "BUSY_VERIFY_TO_SAMPLE"
    assert state["verify_calls"] == []


def test_a_render_refused_during_a_sample_does_not_claim_another_render_is_running():
    """The precise failure the holder token exists to prevent.

    With the old boolean, `_verification_running` was `False` during a sample decode, so the render
    gate's refusal said "a render is already running" -- about a render that did not exist.
    """
    ns, _state = _load_p2_seam()
    _hold_as(ns, ns["_LOCK_HOLDER_SAMPLE"])
    try:
        assert not ns["_RENDER_LOCK"].acquire(blocking=False), "the render gate must refuse"
        refusal = ns["_busy_message"]()
    finally:
        _drop(ns)
    assert refusal == "BUSY_RENDER_TO_SAMPLE"
    assert refusal != "BUSY_RENDER_TO_RENDER", "a sample was mislabelled as a render"


def test_two_simultaneous_sample_requests_never_overlap_and_only_one_runs():
    """Requirement H, with real threads: the loser is refused, not queued."""
    import threading
    ns, state = _load_p2_seam()
    inside = threading.Event()
    hold = threading.Event()
    results = []

    def impl(path, seek):
        inside.set()
        assert hold.wait(10), "the test never released the sample"
        return "SAMPLE_REPORT"

    state["sample_impl"] = impl
    first = threading.Thread(
        target=lambda: results.append(
            ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, _FakeSampleRequest("A"))),
        daemon=True)
    first.start()
    try:
        assert inside.wait(10), "the first sample never started"
        second = ns["_on_sample_decode_click"]("J:/b.mp4", 2.0, _FakeSampleRequest("B"))
    finally:
        hold.set()
        first.join(10)
    assert not first.is_alive()
    assert second == "BUSY_SAMPLE_TO_SAMPLE", f"the second click got {second!r}"
    assert len(state["sample_calls"]) == 1, "exactly one decode may run"
    assert results == ["SAMPLE_REPORT"]


def test_a_refused_sample_does_no_work_and_does_not_run_later():
    """Requirement R: a refusal is terminal. The queued-then-auto-run defect, asserted absent."""
    ns, state = _load_p2_seam()
    _hold_as(ns, ns["_LOCK_HOLDER_RENDER"])
    try:
        out = ns["_on_sample_decode_click"]("J:/a.mp4", 1.0)
    finally:
        _drop(ns)
    assert out == "BUSY_SAMPLE_TO_RENDER"
    assert state["sample_calls"] == []
    # Releasing the mutex must not make the refused request execute retroactively.
    assert state["sample_calls"] == [], "a refused sample must never run afterwards"
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the mutex must be free again"
    ns["_RENDER_LOCK"].release()


def test_the_sample_mutex_and_holder_are_released_after_success():
    ns, _state = _load_p2_seam()
    assert ns["_on_sample_decode_click"]("J:/a.mp4", 1.0) == "SAMPLE_REPORT"
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the mutex leaked after a successful sample"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "the holder token leaked after a successful sample"
    assert ns["_verify_active_session"] == "", "the pin leaked after a successful sample"


def test_the_sample_mutex_and_holder_are_released_after_an_exception():
    ns, state = _load_p2_seam()

    def boom(path, seek):
        raise RuntimeError("decode seam exploded")

    state["sample_impl"] = boom
    out = ns["_on_sample_decode_click"]("J:/a.mp4", 1.0)
    assert "SAMPLE DECODE FAILED" in out
    assert "RuntimeError" in out and "decode seam exploded" in out
    assert "No analysis, cache change, media change or render was performed." in out
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the mutex leaked after an exception"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "the holder token leaked after an exception"
    assert ns["_verify_active_session"] == ""


def test_the_sample_mutex_and_holder_survive_a_baseexception():
    """An abandoned request must not leave the app permanently refusing renders.

    `BaseException` deliberately propagates, so the `finally` is the only thing that can release
    the mutex, the holder token and the pin.
    """
    ns, state = _load_p2_seam()

    def abandoned(path, seek):
        raise KeyboardInterrupt("request abandoned")

    state["sample_impl"] = abandoned
    with pytest.raises(KeyboardInterrupt):
        ns["_on_sample_decode_click"]("J:/a.mp4", 1.0)
    assert ns["_RENDER_LOCK"].acquire(blocking=False), \
        "an abandoned sample must not leave the app permanently busy"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "a BaseException must still clear the holder token"
    assert ns["_verify_active_session"] == "", "a BaseException must still release the pin"


def test_the_holder_is_cleared_before_the_mutex_is_released():
    """Ordering requirement: decide staleness, clear the holder, THEN release.

    Releasing first would leave a window in which the mutex is free while the token still names a
    finished sample, so the next caller to fail an acquire would be told to wait for something that
    had already ended.
    """
    tree = _tree(_GUI)
    handler = _func(tree, "_on_sample_decode_click")
    body = [ast.unparse(node) for node in handler.body]
    finallies = [n for n in ast.walk(handler) if isinstance(n, ast.Try) and n.finalbody]
    assert finallies, "the release must sit in a finally"
    final = [ast.unparse(statement) for statement in finallies[0].finalbody]
    clear_at = next(i for i, s in enumerate(final) if "_mark_lock_holder('')" in s)
    release_at = next(i for i, s in enumerate(final) if "_RENDER_LOCK.release()" in s)
    stale_at = next(i for i, s in enumerate(final) if "_end_verify_session" in s)
    assert stale_at < clear_at < release_at, final
    assert any("_RENDER_LOCK.acquire(blocking=False)" in s for s in body), \
        "the gate must be a single non-blocking acquire"


def test_the_sample_marks_the_holder_only_after_acquiring():
    """Marking before the acquire would publish a holder that does not hold anything."""
    tree = _tree(_GUI)
    body = [ast.unparse(node) for node in _func(tree, "_on_sample_decode_click").body]
    acquire_at = next(i for i, s in enumerate(body)
                      if "_RENDER_LOCK.acquire(blocking=False)" in s)
    mark_at = next(i for i, s in enumerate(body) if "_mark_lock_holder(_LOCK_HOLDER_SAMPLE)" in s)
    assert acquire_at < mark_at, body


# ---------------------------------------------------------------------------
# scoped stale-result protection
# ---------------------------------------------------------------------------
#
# One bounded map, one pin, two scopes. Nothing about the eviction algorithm is duplicated here --
# the S2-A functions are reused verbatim and simply receive a longer key, so the R5 correction
# (the active entry is exempt from eviction) protects a sample exactly as it protects a
# verification. The reason that still holds is `_RENDER_LOCK`: at most one of render / verify /
# sample runs at a time, so at most one revision is ever load-bearing.

def _sample_with_interference(interfere, *, cap=None, session="A", pre_bumps=0):
    """Run one real sample click; `interfere(ns)` runs while it is inside the impl."""
    import threading
    entered = threading.Event()
    release = threading.Event()
    ns, state = _load_p2_seam(cap=cap)

    def blocking_impl(_path, _seek):
        entered.set()
        assert release.wait(30), "the test never released the sample"
        return "SAMPLE_REPORT"

    state["sample_impl"] = blocking_impl
    scoped = ns["_scoped_key"](session, ns["_SCOPE_SAMPLE"])
    for _ in range(pre_bumps):
        ns["_bump_verify_revision"](scoped)

    out = {}

    def click():
        out["result"] = ns["_on_sample_decode_click"](
            "J:/a.mp4", 1.0, _FakeSampleRequest(session))

    worker = threading.Thread(target=click, daemon=True)
    worker.start()
    try:
        assert entered.wait(30), "the sample never started"
        out["pinned_while_running"] = ns["_verify_active_session"]
        out["holder_while_running"] = ns["_lock_holder_now"]()
        interfere(ns)
        out["present_while_running"] = scoped in ns["_verify_revisions"]
        out["map_len_while_running"] = len(ns["_verify_revisions"])
    finally:
        release.set()
        worker.join(30)
    assert not worker.is_alive(), "the sample must finish"
    return out, ns, state


def test_changing_the_source_path_during_a_decode_withholds_the_stale_report():
    """Requirement J: an answer about a file the operator has already left is never published."""
    out, _ns, _state = _sample_with_interference(
        lambda ns: ns["_on_sample_invalidate"](_FakeSampleRequest("A")))
    assert out["result"] == "STALE_SAMPLE", f"the stale report was published: {out['result']!r}"
    assert out["holder_while_running"] == "sample", "the holder must name the sample while it runs"


def test_changing_the_seek_during_a_decode_withholds_the_stale_report():
    """Requirement K. Both change listeners share one handler, so this is the same bump."""
    out, _ns, _state = _sample_with_interference(
        lambda ns: ns["_on_sample_invalidate"](_FakeSampleRequest("A")))
    assert out["result"] == "STALE_SAMPLE"


def test_a_failed_sample_is_also_withheld_after_an_invalidation():
    """The failure text names a path too, so it is withheld for the same reason."""
    import threading
    entered = threading.Event()
    release = threading.Event()
    ns, state = _load_p2_seam()

    def boom(_path, _seek):
        entered.set()
        assert release.wait(30)
        raise RuntimeError("decode seam exploded")

    state["sample_impl"] = boom
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(
            result=ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, _FakeSampleRequest("A"))),
        daemon=True)
    worker.start()
    try:
        assert entered.wait(30)
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
    finally:
        release.set()
        worker.join(30)
    assert out["result"] == "STALE_SAMPLE", "a failure report about a stale path must be withheld"


def test_another_session_editing_its_inputs_does_not_invalidate_this_sample():
    """Requirement L: session-scoped, so one browser cannot clear another's answer."""
    out, _ns, _state = _sample_with_interference(
        lambda ns: ns["_on_sample_invalidate"](_FakeSampleRequest("B")))
    assert out["result"] == "SAMPLE_REPORT", "another session's edit invalidated this sample"


def test_a_sessions_own_edit_still_invalidates_when_another_session_also_edits():
    def interfere(ns):
        ns["_on_sample_invalidate"](_FakeSampleRequest("B"))
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
    out, _ns, _state = _sample_with_interference(interfere)
    assert out["result"] == "STALE_SAMPLE"


def test_a_folder_edit_does_not_invalidate_a_running_sample():
    """Cross-scope isolation, direction one.

    S2-A's folder/recursive invalidation belongs to the 'verify' scope. A sample answers a
    different question about a different selection, so clearing it would be a wrong answer -- not
    merely a conservative one.
    """
    out, _ns, _state = _sample_with_interference(
        lambda ns: ns["_on_verify_invalidate"](_FakeSampleRequest("A")))
    assert out["result"] == "SAMPLE_REPORT", "a folder edit invalidated a sample decode"


def test_a_sample_input_edit_does_not_invalidate_a_running_verification():
    """Cross-scope isolation, direction two -- asserted through the S2-A handler itself."""
    import threading
    entered = threading.Event()
    release = threading.Event()
    ns, state = _load_p2_seam()

    def blocking_verify(_folder, _recursive):
        entered.set()
        assert release.wait(30)
        return "VERIFY_REPORT"

    state["verify_impl"] = blocking_verify
    out = {}
    worker = threading.Thread(
        target=lambda: out.update(
            result=ns["_on_verify_sources_click"]("J:/lib", True, _FakeSampleRequest("A"))),
        daemon=True)
    worker.start()
    try:
        assert entered.wait(30)
        assert ns["_lock_holder_now"]() == "verify"
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
    finally:
        release.set()
        worker.join(30)
    assert out["result"] == "VERIFY_REPORT", "a seek edit invalidated a source verification"


def test_the_two_scopes_are_separate_entries_for_one_session():
    """The structural fact the isolation rests on: distinct keys, independent counters."""
    ns, _state = _load_p2_seam()
    verify_key = ns["_scoped_key"]("A", ns["_SCOPE_VERIFY"])
    sample_key = ns["_scoped_key"]("A", ns["_SCOPE_SAMPLE"])
    assert verify_key != sample_key
    ns["_on_verify_invalidate"](_FakeSampleRequest("A"))
    assert ns["_verify_revisions"] == {verify_key: 1}, ns["_verify_revisions"]
    ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
    assert ns["_verify_revisions"] == {verify_key: 1, sample_key: 1}, ns["_verify_revisions"]
    ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
    assert ns["_verify_revisions"][sample_key] == 2
    assert ns["_verify_revisions"][verify_key] == 1, "the verify scope must not move"


def test_a_scope_cannot_forge_another_scopes_key():
    """A session_hash is hex and the fallback is `__no_session__`; neither can contain a NUL."""
    ns, _state = _load_p2_seam()
    assert "\x00" in ns["_scoped_key"]("A", ns["_SCOPE_SAMPLE"])
    assert ns["_scoped_key"]("A", "sample") != ns["_scoped_key"]("A\x00sample", "verify")
    assert ns["_verify_session_key"](_FakeSampleRequest("abc")) == "abc"
    assert "\x00" not in ns["_verify_session_key"](_FakeSampleRequest("abc"))


def test_a_sample_request_without_a_session_hash_falls_into_one_shared_bucket():
    """Requirement: the fallback must not be unique, or an invalidation could never match."""
    ns, _state = _load_p2_seam()
    assert ns["_verify_session_key"](None) == ns["_verify_session_key"](_FakeSampleRequest(None))
    assert ns["_verify_session_key"](_FakeSampleRequest("")) == "__no_session__"
    out, _ns2, _state2 = _sample_with_interference(
        lambda ns2: ns2["_on_sample_invalidate"](None), session="__no_session__")
    assert out["result"] == "STALE_SAMPLE", "the shared bucket must still self-invalidate"


@pytest.mark.parametrize("others", [0, 1, 7, 8, 9, 16, 40])
def test_an_active_sample_survives_any_number_of_other_invalidations(others):
    """The R5 eviction exemption, re-proved for the sample scope at and beyond the cap."""
    def interfere(ns):
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
        for index in range(others):
            ns["_bump_verify_revision"](f"other-{index}")

    out, _ns, _state = _sample_with_interference(interfere, cap=8)
    assert out["result"] == "STALE_SAMPLE", (
        f"{others} other sessions churned and the stale sample report was published: "
        f"{out['result']!r}")
    assert out["pinned_while_running"] == "A\x00sample"
    assert out["present_while_running"], "the active sample's entry must survive eviction"
    assert out["map_len_while_running"] <= 8, "the cap must hold INCLUDING the pinned entry"


@pytest.mark.parametrize("others", [511, 512, 513, 1024])
def test_the_sample_scope_is_suppressed_at_the_real_shipped_cap(others):
    """The R4-measured thresholds, against the real cap read from `gui.py`."""
    def interfere(ns):
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
        for index in range(others):
            ns["_bump_verify_revision"](f"other-{index}")

    out, _ns, _state = _sample_with_interference(interfere)
    assert out["result"] == "STALE_SAMPLE", f"{others} others -> {out['result']!r}"
    assert out["present_while_running"]


@pytest.mark.parametrize("captured", [1, 2, 9])
def test_the_aba_recreation_case_is_suppressed_for_the_sample_scope(captured):
    """Scenario B: a non-zero captured revision, eviction pressure, then an invalidation.

    Without the pin the entry could be dropped and recreated at the same integer, so the terminal
    comparison would see no change and publish. `_verify_revision` returns 0 for an ABSENT key, so
    absence and "revision zero" must never become indistinguishable for the running operation.
    """
    def interfere(ns):
        ns["_on_sample_invalidate"](_FakeSampleRequest("A"))
        for index in range(40):
            ns["_bump_verify_revision"](f"other-{index}")

    out, ns, _state = _sample_with_interference(interfere, cap=8, pre_bumps=captured)
    assert out["result"] == "STALE_SAMPLE"
    key = ns["_scoped_key"]("A", ns["_SCOPE_SAMPLE"])
    assert ns["_verify_revisions"][key] == captured + 1, (
        "the invalidation must increment the surviving revision, not restart from 0")


def test_a_finished_samples_entry_becomes_evictable_again():
    """The pin is for the span of one run, not a permanent reservation."""
    out, ns, _state = _sample_with_interference(
        lambda ns2: ns2["_on_sample_invalidate"](_FakeSampleRequest("A")), cap=8)
    assert out["result"] == "STALE_SAMPLE"
    key = ns["_scoped_key"]("A", ns["_SCOPE_SAMPLE"])
    assert key in ns["_verify_revisions"]
    assert ns["_verify_active_session"] == "", "the pin must be released afterwards"
    for index in range(20):
        ns["_bump_verify_revision"](f"after-{index}")
    assert key not in ns["_verify_revisions"], (
        "once the run is over the former active entry must evict like any other")
    assert len(ns["_verify_revisions"]) <= 8


def test_an_invalidation_after_the_sample_was_published_does_not_rewrite_it():
    """A bump that arrives after the answer was returned cannot retroactively withhold it."""
    ns, _state = _load_p2_seam()
    request = _FakeSampleRequest("A")
    assert ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, request) == "SAMPLE_REPORT"
    assert ns["_on_sample_invalidate"](request) == "STALE_SAMPLE"
    assert ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, request) == "SAMPLE_REPORT"


def test_rapid_sample_clicks_never_create_a_second_pin():
    import threading
    ns, state = _load_p2_seam()
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def blocking_impl(_path, _seek):
        observed.append(ns["_verify_active_session"])
        entered.set()
        assert release.wait(30)
        return "SAMPLE_REPORT"

    state["sample_impl"] = blocking_impl
    results = []
    workers = [threading.Thread(
        target=lambda index=index: results.append(ns["_on_sample_decode_click"](
            "J:/a.mp4", 1.0, _FakeSampleRequest(f"S{index}"))), daemon=True)
        for index in range(6)]
    for worker in workers:
        worker.start()
    try:
        assert entered.wait(30)
    finally:
        release.set()
        for worker in workers:
            worker.join(30)
    assert len(state["sample_calls"]) == 1, "exactly one sample may run"
    assert results.count("SAMPLE_REPORT") == 1
    assert results.count("BUSY_SAMPLE_TO_SAMPLE") == 5
    assert len(observed) == 1 and observed[0].endswith("\x00sample")
    assert ns["_verify_active_session"] == ""


# ---------------------------------------------------------------------------
# UI wiring, the resource contract, and isolation from everything else
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def p2_gui_tree():
    return _tree(_GUI)


def _p2_click_keywords(tree, widget, attr="click"):
    """The keyword arguments of `<widget>.<attr>(...)`, read from the AST, not from a text window."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != attr or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != widget:
            continue
        return {keyword.arg: ast.unparse(keyword.value) for keyword in node.keywords}
    raise AssertionError(f"{widget}.{attr}(...) not found")


def test_the_sample_event_has_its_own_unlimited_lane(p2_gui_tree):
    """Sharing the render lane is what made Gradio queue S2-A instead of refusing it.

    Measured on the pinned gradio==6.19.0 during S2-A's R3 correction: `Queue.get_events()` skips a
    concurrency group that is at its limit and leaves the event QUEUED, so the handler was entered
    only after the render finished and then ran automatically against click-time values. A lane of
    1 would re-create that one step in -- a second Sample Decode click would queue behind the first
    instead of being refused by the mutex.
    """
    keywords = _p2_click_keywords(p2_gui_tree, "sample_btn")
    assert "concurrency_id" not in keywords, keywords
    assert keywords.get("concurrency_limit") == "None", keywords
    assert keywords.get("fn") == "_on_sample_decode_click", keywords
    assert keywords.get("inputs") == "[sample_path, sample_seek]", keywords
    assert keywords.get("outputs") == "[sample_report]", keywords
    assert keywords.get("show_progress") == "'hidden'", keywords


def test_the_sample_event_joins_neither_the_render_nor_the_cancel_lane(p2_gui_tree):
    source = ast.unparse(p2_gui_tree)
    wiring = source.split("sample_btn.click(")[1].split(")")[0]
    assert "RENDER_CONCURRENCY_ID" not in wiring
    assert "CANCEL_CONCURRENCY_ID" not in wiring
    assert "concurrency_id" not in wiring


def test_both_render_events_keep_the_shared_render_lane(p2_gui_tree):
    """The render lane is untouched by P2 -- asserted from the sample side as well."""
    for widget in ("process_btn", "render_selected_variants_btn"):
        keywords = _p2_click_keywords(p2_gui_tree, widget)
        assert keywords.get("concurrency_id") == "RENDER_CONCURRENCY_ID", (widget, keywords)
        assert keywords.get("concurrency_limit") == "1", (widget, keywords)


def test_the_verify_event_wiring_is_unchanged_by_p2(p2_gui_tree):
    keywords = _p2_click_keywords(p2_gui_tree, "prep_verify_btn")
    assert "concurrency_id" not in keywords, keywords
    assert keywords.get("concurrency_limit") == "None", keywords
    assert keywords.get("inputs") == "[prep_folder, prep_recursive]", keywords
    assert keywords.get("outputs") == "[prep_verify_report]", keywords


def test_the_click_takes_only_the_live_sample_widgets(p2_gui_tree):
    """The live widget values, not `gr.State`: a queued change event must not be read as current.

    `request` is injected by annotation and must NOT be an input -- listing it would consume a
    widget slot and shift every positional argument.
    """
    keywords = _p2_click_keywords(p2_gui_tree, "sample_btn")
    assert keywords.get("inputs") == "[sample_path, sample_seek]"
    assert "request" not in keywords.get("inputs", "")
    handler = _func(p2_gui_tree, "_on_sample_decode_click")
    names = [argument.arg for argument in handler.args.args]
    assert names == ["video_path", "seek_seconds", "request"], names
    annotation = ast.unparse(handler.args.args[-1].annotation)
    assert annotation == "gr.Request", annotation
    assert ast.unparse(handler.args.defaults[-1]) == "None", "request must default to None"


def test_both_sample_change_listeners_share_the_one_invalidating_handler(p2_gui_tree):
    for widget in ("sample_path", "sample_seek"):
        keywords = _p2_click_keywords(p2_gui_tree, widget, attr="change")
        assert keywords.get("fn") == "_on_sample_invalidate", (widget, keywords)
        assert keywords.get("inputs") == "None", (widget, keywords)
        assert keywords.get("outputs") == "[sample_report]", (widget, keywords)


def test_no_change_listener_can_start_disk_work(p2_gui_tree):
    """A change listener must never probe, decode, stat or scan -- it only invalidates."""
    referenced = _referenced(_func(p2_gui_tree, "_on_sample_invalidate"))
    for forbidden in ("probe_media_metadata", "sample_decode_video", "run_sample_check",
                      "scan_library_folder", "os.stat", "stat", "subprocess", "_RENDER_LOCK"):
        assert forbidden not in referenced, f"{forbidden} is reachable from a change listener"


def test_the_sample_report_is_the_only_component_the_sample_events_write(p2_gui_tree):
    source = ast.unparse(p2_gui_tree)
    for block in ("sample_btn.click(", "sample_path.change(", "sample_seek.change("):
        wiring = source.split(block)[1].split(")")[0]
        assert "outputs=[sample_report]" in wiring, block
        for protected in ("source_outputs", "prep_outputs", "process_btn", "source_state",
                          "prep_state", "render_invocation_state", "video_output",
                          "status_output", "prep_verify_report", "prep_report"):
            assert protected not in wiring, (block, protected)


def test_the_sample_report_is_in_no_shared_output_list(p2_gui_tree):
    """It cannot touch the Create Music Video gate or the preparation state machine."""
    source = ast.unparse(p2_gui_tree)
    for listname in ("source_outputs = ", "prep_outputs = "):
        assert listname in source, listname
        declaration = source.split(listname)[1].split("]")[0]
        assert "sample_report" not in declaration, listname


def test_the_resource_policy_cannot_be_widened_from_the_gui(p2_gui_tree):
    """Requirement Q, and it is enforced BY OMISSION rather than by validation.

    The call passes `probe=` and `decode=` and nothing else, so `requested_frames`,
    `decode_timeout` and `budget_seconds` all come from the module constants. There is no argument
    for a widget value to reach -- which is a stronger guarantee than clamping one would be.
    """
    impl = _func(p2_gui_tree, "_sample_decode_impl")
    calls = [n for n in ast.walk(impl) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith("run_sample_check")]
    assert len(calls) == 1, "exactly one orchestrator call"
    keywords = {keyword.arg for keyword in calls[0].keywords}
    assert keywords == {"probe", "decode"}, f"unexpected keywords: {keywords}"
    for forbidden in ("requested_frames", "decode_timeout", "budget_seconds", "stat", "clock"):
        assert forbidden not in keywords, forbidden
    assert len(calls[0].args) == 2, "path and seek positionally, nothing else"


def test_no_frame_timeout_or_budget_control_exists_in_the_ui(p2_gui_tree):
    """No slider, number or textbox may offer to change the approved policy."""
    source = ast.unparse(p2_gui_tree)
    block = source[source.index("INFO_PREP_SAMPLE"):source.index("sample_btn.click(")]
    for forbidden in ("MAX_SAMPLE_FRAMES", "DEFAULT_SAMPLE_FRAMES", "requested_frames",
                      "decode_timeout", "budget_seconds", "DEFAULT_SAMPLE_DECODE_TIMEOUT_SECONDS",
                      "DEFAULT_SAMPLE_BUDGET_SECONDS", "gr.Slider"):
        assert forbidden not in block, f"{forbidden} is exposed in the sample UI block"


def test_the_gui_adds_no_second_runner_parser_selector_or_classifier(p2_gui_tree):
    """The accepted core decides everything; this layer only calls it and shows the result."""
    for name in ("_sample_decode_impl", "_on_sample_decode_click", "_on_sample_invalidate"):
        referenced = _referenced(_func(p2_gui_tree, name))
        for forbidden in ("subprocess", "Popen", "_run_media_command", "classify_decode",
                          "parse_progress_output", "select_renderer_equivalent_stream",
                          "build_sample_decode_command", "SampleStatus", "DecodeExecution"):
            assert forbidden not in referenced, f"{name} reaches {forbidden}"


def test_the_impl_calls_the_accepted_core_and_renders_its_report(p2_gui_tree):
    referenced = _referenced(_func(p2_gui_tree, "_sample_decode_impl"))
    assert "run_sample_check" in referenced
    assert "format_sample_report" in referenced
    assert "probe_media_metadata" in referenced
    assert "sample_decode_video" in referenced
    assert "fork_media_sample" in referenced, "the core is reached through the fork package"


def test_the_sample_handler_stores_nothing_concurrency_shaped_in_gradio_state(p2_gui_tree):
    for name in ("_on_sample_decode_click", "_sample_decode_impl", "_on_sample_invalidate"):
        referenced = _referenced(_func(p2_gui_tree, name))
        for forbidden in ("gr.State", "State", "session_state", "Thread", "Popen", "Event",
                          "RenderLifecycle", "_verify_revisions", "Lock"):
            assert forbidden not in referenced, f"{name} reaches {forbidden}"


def test_the_sample_touches_no_cache_qwen_analysis_or_render(p2_gui_tree):
    """Unlike `_verify_sources_impl`, the sample consults no classifier at all."""
    for name in ("_sample_decode_impl", "_on_sample_decode_click"):
        referenced = _referenced(_func(p2_gui_tree, name))
        for forbidden in ("classify_library_sources", "_resolve_prep_qwen_runtime",
                          "video_analysis", "create_music_video", "analyze_beats_auto",
                          "scan_library_folder", "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION"):
            assert forbidden not in referenced, f"{name} reaches {forbidden}"


def test_the_sample_controls_sit_inside_the_preparation_accordion(p2_gui_tree):
    source = ast.unparse(p2_gui_tree)
    accordion_at = source.index("gr.Accordion(label=LABEL_PREP_SECTION, open=False)")
    sample_at = source.index("elem_id='prep-sample-button'")
    verify_at = source.index("elem_id='prep-verify-report-box'")
    output_at = source.index("### 📺 Output")
    assert accordion_at < verify_at < sample_at < output_at, (
        "the sample block must follow Verify Sources and stay inside the accordion")


def test_the_sample_button_is_always_interactive(p2_gui_tree):
    """The mutex, not a disabled button, is what prevents a sample during a render.

    Interactivity is per-session UX; the mutex is process-global truth. A disabled button would
    also have to be re-enabled on every terminal path, which is a second state machine.
    """
    source = ast.unparse(p2_gui_tree)
    block = source[source.index("sample_btn = gr.Button"):source.index("sample_report = gr.Textbox")]
    assert "interactive=False" not in block, block


def test_the_sample_report_textbox_is_read_only(p2_gui_tree):
    source = ast.unparse(p2_gui_tree)
    block = source[source.index("sample_report = gr.Textbox"):]
    block = block[:block.index(")")]
    assert "interactive=False" in block
    assert "LABEL_PREP_SAMPLE_REPORT" in block
    assert "PREP_SAMPLE_INITIAL_TEXT" in block


def test_the_seek_control_is_seconds_with_a_conservative_default(p2_gui_tree):
    source = ast.unparse(p2_gui_tree)
    block = source[source.index("sample_seek = gr.Number"):source.index("sample_btn = gr.Button")]
    assert "value=1.0" in block, block
    assert "minimum=0" in block, block
    assert "LABEL_PREP_SAMPLE_SEEK" in block
    # A frame-number control would require frames/fps arithmetic, which the core forbids.
    for forbidden in ("frame", "fps"):
        assert forbidden not in block.lower(), f"the seek control mentions {forbidden}"


def test_an_empty_path_is_refused_before_any_core_call(p2_gui_tree):
    """Fail closed with no child, and say which control to use."""
    ns, state = _load_p2_seam()
    # The seam stubs the impl, so drive the REAL impl directly against the real core instead.
    assert state["sample_calls"] == []
    for empty in ("", "   "):
        verdict = run_sample_check(
            empty, 1.0,
            probe=lambda _p: ProbeOutcome(returncode=0, stdout="{}"),
            decode=lambda *a, **k: SampleDecodeOutcome(returncode=0),
            stat=lambda _p: (_ for _ in ()).throw(AssertionError("stat must not be reached")))
        assert verdict.status is SampleStatus.INVALID_REQUEST, empty


@pytest.mark.parametrize("seek", [None, "", -1.0, float("inf"), float("nan"), "abc"])
def test_an_invalid_seek_fails_closed_with_no_child(seek):
    """The GUI performs no validation of its own: the accepted core owns this, and already did."""
    launched = []

    def decode(path, **kwargs):
        launched.append(path)
        return SampleDecodeOutcome(returncode=0)

    verdict = run_sample_check(
        "J:/a.mp4", seek,
        probe=lambda _p: _probe_ok(_stream()),
        decode=decode,
        stat=_stat_seq(_St(10, 7), _St(10, 7), _St(10, 7)),
        clock=iter_clock())
    assert verdict.status is SampleStatus.INVALID_REQUEST, (seek, verdict.status)
    assert launched == [], "no decode child may be started for an invalid seek"
    assert format_sample_report(verdict), "an invalid request still produces a report"


def test_the_report_shown_is_the_cores_own_report_not_a_ui_summary():
    """No green/red badge, no "healthy": the operator sees `format_sample_report` verbatim."""
    verdict, _launched = _check()
    report = format_sample_report(verdict)
    assert report.startswith("Sample decode check")
    assert SAMPLE_ONLY_TEXT in report, "the sample-only limit must survive into the UI"
    low = report.lower()
    for overclaim in ("healthy", "fully verified", "video is fine", "no problems found"):
        assert overclaim not in low, overclaim


def test_the_ui_text_distinguishes_output_frames_from_decoded_frames():
    """`-frames:v` bounds OUTPUT frames; decoder pre-roll is unbounded and invisible.

    Claiming "30 frames were decoded" would be a statement the diagnostic cannot support, so the
    operator-facing text has to say "output frames".
    """
    with open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8") as handle:
        content = handle.read()
    block = content[content.index("INFO_PREP_SAMPLE = ("):]
    block = block[:block.index("\n)")]
    assert "output frames" in block, block
    assert "up to 30" in block, "the cap must be stated as a bound, not as a promise"
    assert "does not verify the entire video" in block
    for overclaim in ("healthy", "fully verified", "guarantee"):
        assert overclaim not in block.lower(), overclaim


def test_the_budget_is_never_described_as_a_wall_clock_deadline():
    with open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8") as handle:
        content = handle.read()
    sample_text = content[content.index("LABEL_PREP_SAMPLE = "):content.index(
        "def get_gpu_status_info")]
    low = sample_text.lower()
    for forbidden in ("60 second", "60s", "deadline", "within a minute", "guaranteed"):
        assert forbidden not in low, f"the UI text promises {forbidden!r}"


def test_the_unrelated_render_and_source_wiring_is_untouched(p2_gui_tree):
    """Requirement P: the Create Music Video gate and the render events are unchanged."""
    source = ast.unparse(p2_gui_tree)
    clicks = [n for n in ast.walk(p2_gui_tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "click" and isinstance(n.func.value, ast.Name)
              and n.func.value.id == "process_btn"]
    assert len(clicks) == 1, "exactly one process_btn.click registration"
    keywords = {keyword.arg for keyword in clicks[0].keywords}
    assert "concurrency_id" in keywords and "inputs" in keywords and "outputs" in keywords
    # Read the keywords from the AST: the render registration carries dozens of inputs, so a text
    # window around the call name is not a reliable way to find the end of it.
    render_wiring = " ".join(_p2_click_keywords(p2_gui_tree, "process_btn").values())
    for forbidden in ("sample_report", "sample_path", "sample_seek", "sample_btn"):
        assert forbidden not in render_wiring, f"{forbidden} leaked into the render request"
    assert "_RENDER_LOCK.locked()" not in source, "check-then-act is the race this avoids"
    assert "_RENDER_LOCK.acquire(blocking=True)" not in source
    assert "RenderLifecycle" not in ast.unparse(_func(p2_gui_tree, "_on_sample_decode_click")), \
        "the sample owns no render lifecycle"


def test_the_render_holder_marks_do_not_disturb_the_generator_contracts(p2_gui_tree):
    """The marks are standalone statements: no yield is added, moved or reshaped.

    The render wrappers stream fixed-width tuples to Gradio. A holder mark that became part of a
    yield -- or that shifted one -- would change the output contract, so the yield shapes are
    asserted directly.
    """
    for name, width in (("process_video_guarded", 6), ("_render_selected_variants", 5)):
        try:
            func = _func(p2_gui_tree, name)
        except AssertionError:
            continue
        yields = [n for n in ast.walk(func) if isinstance(n, ast.Yield) and n.value is not None]
        tuples = [n.value for n in yields if isinstance(n.value, ast.Tuple)]
        assert tuples, f"{name} must still yield tuples"
        assert {len(t.elts) for t in tuples} == {width}, (
            f"{name} yield width changed: {sorted({len(t.elts) for t in tuples})}")
        for node in yields:
            assert "_mark_lock_holder" not in ast.unparse(node), \
                f"{name} folded the holder mark into a yield"


def _calls_named(tree, rendered):
    """Every `ast.Call` whose unparsed form equals `rendered`.

    Structural on purpose. `ast.unparse` emits docstrings, so counting occurrences in the unparsed
    TEXT also counts the mutex contract that `_lock_holder_now`'s docstring explains -- a census
    that searches prose fails on the prose documenting it. (Measured: it reported five acquire
    sites for four.)
    """
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and ast.unparse(node) == rendered]


def test_the_mutex_census_is_four_acquires_and_four_releases(p2_gui_tree):
    """Two renders, one verification, one sample -- each a single atomic test-and-set."""
    assert len(_calls_named(p2_gui_tree, "_RENDER_LOCK.acquire(blocking=False)")) == 4
    assert len(_calls_named(p2_gui_tree, "_RENDER_LOCK.release()")) == 4
    assert not _calls_named(p2_gui_tree, "_RENDER_LOCK.locked()"), \
        "check-then-act is the race the non-blocking acquire avoids"
    assert not _calls_named(p2_gui_tree, "_RENDER_LOCK.acquire()")
    assert not _calls_named(p2_gui_tree, "_RENDER_LOCK.acquire(blocking=True)")


def test_the_render_wrappers_contain_no_holder_marking_at_all(p2_gui_tree):
    """Option B's defining property: the render path is untouched.

    Render attribution comes from the EXISTING active-render slot, so the wrappers need no mark.
    A mark reappearing here would mean the wrappers had diverged from the pinned implementation,
    which is what keeps every render lifecycle, cancellation and generator regression valid.
    """
    for name in ("process_video_guarded", "render_selected_variants_guarded",
                 "_process_video_guarded_unlocked"):
        rendered = ast.unparse(_func(p2_gui_tree, name))
        assert "_mark_lock_holder" not in rendered, f"{name} marks the holder"
        assert "_lock_holder" not in rendered, f"{name} touches the holder token"


def test_only_the_diagnostics_mark_the_holder_and_each_clears_before_release(p2_gui_tree):
    """Exactly two marking paths, and neither may release before clearing."""
    marks = _calls_named(p2_gui_tree, "_mark_lock_holder(_LOCK_HOLDER_SAMPLE)")
    assert len(marks) == 1, "only the sample marks itself directly"
    assert len(_calls_named(p2_gui_tree, "_mark_lock_holder('')")) == 1, \
        "only the sample clears directly; the verification clears via _mark_verification(False)"
    assert len(_calls_named(p2_gui_tree, "_mark_verification(True)")) == 1
    assert len(_calls_named(p2_gui_tree, "_mark_verification(False)")) == 1
    # Both diagnostic handlers must clear inside the same finally that releases, clear-then-release.
    for handler, clear in (("_on_sample_decode_click", "_mark_lock_holder('')"),
                           ("_on_verify_sources_click", "_mark_verification(False)")):
        finallies = [n for n in ast.walk(_func(p2_gui_tree, handler))
                     if isinstance(n, ast.Try) and n.finalbody]
        assert finallies, handler
        final = [ast.unparse(statement) for statement in finallies[0].finalbody]
        clear_at = next(i for i, s in enumerate(final) if clear in s)
        release_at = next(i for i, s in enumerate(final) if "_RENDER_LOCK.release()" in s)
        assert clear_at < release_at, (handler, final)


def test_p2_adds_no_import_time_subprocess_activity(p2_gui_tree):
    """A2 NOTE-1 carried forward: P2 must not add a new import-time child.

    `ffmpeg_processing`, `logger` and `gpu_cpu_utils` are already module-level imports, so the
    existing `check_nvenc()` probe fires today regardless. The S2-B core is stdlib-only and the
    EXECUTION seam is imported inside the impl, so nothing new runs at import.
    """
    module_level = []
    for node in p2_gui_tree.body:
        if isinstance(node, (ast.Expr, ast.Assign)):
            module_level.append(ast.unparse(node))
    joined = "\n".join(module_level)
    for forbidden in ("run_sample_check", "probe_media_metadata", "sample_decode_video",
                      "subprocess.run", "subprocess.Popen"):
        assert forbidden not in joined, f"{forbidden} is called at import time"
    impl = _func(p2_gui_tree, "_sample_decode_impl")
    local_imports = [ast.unparse(n) for n in ast.walk(impl) if isinstance(n, ast.ImportFrom)]
    assert any("probe_media_metadata" in line and "sample_decode_video" in line
               for line in local_imports), \
        "the execution seam must be imported locally, mirroring _verify_sources_impl"


# ---------------------------------------------------------------------------
# Option B: render attribution from the EXISTING active-render slot
# ---------------------------------------------------------------------------
#
# The render wrappers are byte-identical to the pinned implementation, so "a render holds the
# mutex" is read from the slot they already install. These tests drive the real `_lock_holder_now`
# over a real slot and a real token, because the whole point is a behavioural contract rather than
# a string present in the source.

class _FakeLifecycleSlot:
    """Stands in for the `(invocation_id, RenderLifecycle)` tuple the wrappers install."""

    def __init__(self, invocation_id="inv-1"):
        self.invocation_id = invocation_id


def _install_slot(ns, invocation_id="inv-1"):
    """What `_install_active_render` does, without importing RenderLifecycle."""
    with ns["_ACTIVE_RENDER_SLOT_LOCK"]:
        ns["_active_render_slot"] = (invocation_id, _FakeLifecycleSlot(invocation_id))


def _clear_slot(ns):
    with ns["_ACTIVE_RENDER_SLOT_LOCK"]:
        ns["_active_render_slot"] = None


def test_an_installed_render_slot_positively_identifies_the_render_holder():
    """The substitute for the removed render mark: positive evidence, not elimination."""
    ns, _state = _load_p2_seam()
    assert ns["_lock_holder_now"]() == "", "no token and no slot is unknown, not render"
    _install_slot(ns)
    assert ns["_lock_holder_now"]() == "render", "an installed slot IS the render evidence"
    assert ns["_busy_message"]() == "BUSY_RENDER_TO_RENDER"
    assert ns["_verify_busy_message"]() == "BUSY_VERIFY_TO_RENDER"
    assert ns["_sample_busy_message"]() == "BUSY_SAMPLE_TO_RENDER"


def test_an_absent_render_slot_produces_neutral_attribution_never_render():
    """Requirement: absence of evidence is reported as unknown.

    This is the exact inaccuracy the old boolean shipped -- `False` meant both "nobody published"
    and "a render", so a refusal during a sample decode named a render that did not exist.
    """
    ns, _state = _load_p2_seam()
    _clear_slot(ns)
    assert ns["_lock_holder_now"]() == ""
    for caller in ("_busy_message", "_verify_busy_message", "_sample_busy_message"):
        got = ns[caller]()
        assert got == "BUSY_UNKNOWN", f"{caller} invented {got!r} with no evidence at all"
        assert "RENDER" not in got


def test_the_slot_is_cleared_before_the_release_so_attribution_cannot_go_stale():
    """Window 3: slot cleared, then mutex released. A freed mutex must not still read 'render'."""
    ns, _state = _load_p2_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False)
    _install_slot(ns)
    assert ns["_lock_holder_now"]() == "render"
    # the wrappers' own order: clear the slot, THEN release
    _clear_slot(ns)
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "a released mutex must not still be attributed"


def test_the_window_between_acquire_and_slot_install_is_unknown_not_render():
    """Window 1: the mutex is taken but the lifecycle is not installed yet.

    One statement wide in the shipped wrapper. Reporting it as unknown is honest; reporting it as a
    render would be a guess that happens to be right, and the same guess is wrong for a sample.
    """
    ns, _state = _load_p2_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False)
    try:
        assert ns["_lock_holder_now"]() == "", "no evidence yet -- must be unknown"
        assert ns["_sample_busy_message"]() == "BUSY_UNKNOWN"
    finally:
        ns["_RENDER_LOCK"].release()


def test_an_explicit_diagnostic_token_outranks_the_render_slot():
    """A stale slot must never mislabel a diagnostic that has published itself.

    The slot cannot legitimately be installed while a sample holds the mutex, so this pins the
    precedence rather than a reachable state: explicit evidence wins over inferred evidence.
    """
    ns, _state = _load_p2_seam()
    _install_slot(ns)
    for token, expected in (("sample", "sample"), ("verify", "verify")):
        ns["_mark_lock_holder"](token)
        assert ns["_lock_holder_now"]() == expected, token
    ns["_mark_lock_holder"]("")
    assert ns["_lock_holder_now"]() == "render", "clearing the token falls back to the slot"


def test_a_sample_during_an_installed_render_slot_is_refused_as_a_render():
    """Window 2 end-to-end: a real render holds the mutex AND has its lifecycle installed."""
    ns, state = _load_p2_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False)
    _install_slot(ns)
    try:
        out = ns["_on_sample_decode_click"]("J:/a.mp4", 1.0, _FakeSampleRequest("S"))
    finally:
        _clear_slot(ns)
        ns["_RENDER_LOCK"].release()
    assert out == "BUSY_SAMPLE_TO_RENDER"
    assert state["sample_calls"] == []


def test_a_render_finishing_lets_a_new_sample_start_while_a_third_is_refused():
    """Window 7: the handover must not leave a stale attribution or a leaked mutex."""
    import threading
    ns, state = _load_p2_seam()
    # a render holds the mutex with its slot installed
    assert ns["_RENDER_LOCK"].acquire(blocking=False)
    _install_slot(ns)
    assert ns["_on_sample_decode_click"]("J:/a.mp4", 1.0) == "BUSY_SAMPLE_TO_RENDER"
    # the render finishes exactly as the wrappers do
    _clear_slot(ns)
    ns["_RENDER_LOCK"].release()
    # now a sample may start, and a concurrent second one is refused as a SAMPLE, not a render
    inside = threading.Event()
    hold = threading.Event()

    def impl(path, seek):
        inside.set()
        assert hold.wait(10)
        return "SAMPLE_REPORT"

    state["sample_impl"] = impl
    worker = threading.Thread(
        target=lambda: ns["_on_sample_decode_click"]("J:/b.mp4", 2.0), daemon=True)
    worker.start()
    try:
        assert inside.wait(10)
        assert ns["_lock_holder_now"]() == "sample", "the handover must re-attribute"
        assert ns["_on_sample_decode_click"]("J:/c.mp4", 3.0) == "BUSY_SAMPLE_TO_SAMPLE"
    finally:
        hold.set()
        worker.join(10)
    assert len(state["sample_calls"]) == 1
    assert ns["_lock_holder_now"]() == ""


def test_the_holder_lookup_never_nests_the_two_attribution_locks():
    """Requirement: the token lock and the slot lock are taken SEQUENTIALLY.

    Nesting them would create an acquisition order to get wrong. Asserted structurally: each `with`
    in `_lock_holder_now` guards exactly one lock, and no `with` body contains another `with`.
    """
    tree = _tree(_GUI)
    lookup = _func(tree, "_lock_holder_now")
    withs = [node for node in ast.walk(lookup) if isinstance(node, ast.With)]
    assert len(withs) == 2, [ast.unparse(w.items[0].context_expr) for w in withs]
    guarded = {ast.unparse(w.items[0].context_expr) for w in withs}
    assert guarded == {"_VERIFY_FLAG_LOCK", "_ACTIVE_RENDER_SLOT_LOCK"}, guarded
    for block in withs:
        assert len(block.items) == 1, "one lock per with-statement"
        nested = [n for n in ast.walk(block) if isinstance(n, ast.With) and n is not block]
        assert not nested, "the two attribution locks must never be held together"
    # And neither lock may be held while the decision is made.
    returns = [ast.unparse(n) for n in ast.walk(lookup) if isinstance(n, ast.Return)]
    assert any("_LOCK_HOLDER_RENDER if render_active" in r for r in returns), returns


def test_the_holder_lookup_reads_the_slot_only_under_its_own_lock():
    """NC3's target: an unsynchronized read of the slot is a data race, not a shortcut."""
    tree = _tree(_GUI)
    lookup = _func(tree, "_lock_holder_now")
    slot_reads = [n for n in ast.walk(lookup)
                  if isinstance(n, ast.Name) and n.id == "_active_render_slot"]
    assert slot_reads, "the lookup must consult the slot"
    guarded_source = ""
    for block in [n for n in ast.walk(lookup) if isinstance(n, ast.With)]:
        if "_ACTIVE_RENDER_SLOT_LOCK" in ast.unparse(block.items[0].context_expr):
            guarded_source = "\n".join(ast.unparse(s) for s in block.body)
    assert "_active_render_slot" in guarded_source, \
        "the slot must be read inside the _ACTIVE_RENDER_SLOT_LOCK block"


def test_the_lookup_is_diagnostic_only_and_admits_nothing():
    """It must not acquire, release or test the admission mutex."""
    referenced = _referenced(_func(_tree(_GUI), "_lock_holder_now"))
    for forbidden in ("acquire", "release", "locked", "_RENDER_LOCK"):
        assert forbidden not in referenced, f"the lookup reaches {forbidden}"
