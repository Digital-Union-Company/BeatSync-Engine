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


def test_the_decode_line_never_contradicts_the_observed_frame_evidence():
    """Full execution-state x status matrix: no report may argue with itself.

    Walks every reachable (status, execution) pair and cross-checks the ``Decode:`` line against
    the ``Observed output frames`` line in the SAME report. A shortfall claim is permitted only
    where the evidence actually shows one; a production claim only where the count reached the
    request. Anything else is a report that contradicts itself.
    """
    cases = {
        "clean success": {},
        "underfilled": {"outcome": _decoded(stdout=_progress(frames=3))},
        "zero frames": {"outcome": _decoded(stdout=_progress(frames=0, blocks=1))},
        "no terminal block": {"outcome": _decoded(stdout="frame=30\nfps=30\n")},
        "malformed frame count": {"outcome": _decoded(stdout="frame=abc\nprogress=end\n")},
        "count above the request": {"outcome": _decoded(stdout=_progress(frames=31, blocks=1))},
        "dup frames positive": {"outcome": _decoded(stdout=_progress(dup=4))},
        "timeout, no output": {"outcome": SampleDecodeOutcome(timed_out=True)},
        "refused spawn": {"outcome": SampleDecodeOutcome(error="WinError 2")},
        "seek out of range": {"seek_seconds": 999.0},
        "invalid request": {"requested_frames": 0},
        "budget exhausted": {"budget_seconds": 1.0},
    }
    cases.update({label: make_kw() for label, make_kw, _ in _FULL_COUNT_BUT_NOT_CLEAN})

    seen_executions = set()
    for label, kw in cases.items():
        verdict, _ = _check(**kw)
        seen_executions.add(verdict.execution)
        report = format_sample_report(verdict)
        decode_line = _field(report, "Decode").lower()
        observed = verdict.observed_frames
        shortfall_known = observed is not None and observed < verdict.requested_frames

        if "did not produce the full" in decode_line or "underproduc" in decode_line:
            assert shortfall_known, f"{label}: shortfall claimed, evidence says {observed}"
        if "produced the requested output frames" in decode_line:
            assert observed is not None and observed >= verdict.requested_frames, \
                f"{label}: production claimed, evidence says {observed}"
            assert verdict.status is SampleStatus.OUTPUT_FRAMES_OK, label
        # decoder_was_launched must agree with the execution field on every single pair.
        assert verdict.decoder_was_launched is (
            verdict.execution in (DecodeExecution.RAN_WITHOUT_CLEAN_RESULT,
                                  DecodeExecution.REQUESTED_FRAMES_PRODUCED)), label

    assert seen_executions == set(DecodeExecution), \
        f"matrix must exercise every execution state, missed {set(DecodeExecution) - seen_executions}"


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
