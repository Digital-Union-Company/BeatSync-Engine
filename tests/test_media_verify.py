"""MEDIA-VERIFY-S2-A: read-only source verification must be bounded and truthful.

Two things this suite is built to prevent, because both are easy to ship and hard to notice:

1. **A fabricated clean verdict.** A skipped probe, a timeout, a worker crash or a file that
   changed mid-check must never aggregate as "metadata verified". Every status path is executed and
   asserted, not inspected.
2. **A metadata read sold as a decode guarantee.** FFprobe parses headers; it does not decode
   frames. The report must say so, every time.

``beatsync_fork.media_verify`` is stdlib-only (CLAUDE.md's hard rule) so it is imported normally and
tested as ordinary code. The pieces that necessarily live in the runtime — the FFprobe argv builder,
the probe seam, and the GUI's concurrency integration — are reached the way the rest of this suite
reaches upstream source: by ``ast`` inspection and by executing real function bodies against a
synthesised namespace. Those are real assertions about real shipped source, but they are seam
assertions, not integration runs: no FFmpeg, no Qwen and no Gradio is started anywhere here.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time
import types

import pytest

from beatsync_fork.library_prep import NeedReason, PrepStatus
from beatsync_fork.media_verify import (
    DEFAULT_BUDGET_SECONDS,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    DEFAULT_PROBE_WORKERS,
    MAX_PROBE_WORKERS,
    MAX_REPORT_DETAIL_LINES,
    METADATA_NOT_DECODE_TEXT,
    MediaStatus,
    ProbeOutcome,
    SourceFingerprint,
    WorkBudget,
    aggregate_media,
    classify_probe,
    fingerprint_source,
    format_verification_report,
    resolve_worker_count,
    verify_media_health,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_FFMPEG = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")


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
    unparsed text — the docstrings here legitimately discuss what the code does not do, and a
    substring ban over prose fails on its own explanation. (This repository has tripped that trap
    repeatedly; the fix is always to look at the AST.)
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


@pytest.fixture(scope="module")
def gui_tree():
    return _tree(_GUI)


@pytest.fixture(scope="module")
def ffmpeg_tree():
    return _tree(_FFMPEG)


# ---------------------------------------------------------------------------
# helpers: a real file on disk, and synthetic ffprobe payloads
# ---------------------------------------------------------------------------

def _video(tmp_path, name="clip.mp4", content=b"\x00" * 64):
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def _payload(*, codec="h264", width=1920, height=1080, rate="30000/1001",
             stream_duration="12.5", container_duration="12.5", extra_streams=()):
    streams = []
    if codec is not None:
        stream = {"codec_type": "video", "codec_name": codec,
                  "width": width, "height": height, "r_frame_rate": rate}
        if stream_duration is not None:
            stream["duration"] = stream_duration
        streams.append(stream)
    streams.extend(extra_streams)
    out = {"streams": streams, "format": {}}
    if container_duration is not None:
        out["format"]["duration"] = container_duration
    return json.dumps(out)


def _ok_probe(**kwargs):
    def probe(path, timeout=None):
        return ProbeOutcome(returncode=0, stdout=_payload(**kwargs))
    return probe


# ---------------------------------------------------------------------------
# classify_probe: every status, exercised
# ---------------------------------------------------------------------------

def test_valid_metadata_is_ok_but_only_ever_claims_metadata():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))
    assert verdict.status is MediaStatus.METADATA_OK
    assert verdict.codec == "h264"
    assert (verdict.width, verdict.height) == (1920, 1080)
    assert verdict.fps == pytest.approx(29.97, abs=0.01)
    assert verdict.duration == pytest.approx(12.5)
    assert verdict.detail == METADATA_NOT_DECODE_TEXT
    assert not verdict.is_problem
    # The one claim that must never appear.
    assert "decoded" not in verdict.detail.lower()
    assert "intact" not in verdict.detail.lower()


def test_a_nonzero_ffprobe_exit_is_unreadable_with_its_stderr():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(
        returncode=1, stderr="moov atom not found"))
    assert verdict.status is MediaStatus.UNREADABLE
    assert "moov atom not found" in verdict.detail
    assert "exit 1" in verdict.detail


def test_malformed_metadata_json_is_invalid_not_unreadable():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout="{not json"))
    assert verdict.status is MediaStatus.INVALID_METADATA
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout="[1, 2, 3]"))
    assert verdict.status is MediaStatus.INVALID_METADATA


def test_an_audio_only_file_has_no_video_stream():
    stdout = json.dumps({"streams": [{"codec_type": "audio", "codec_name": "aac"}],
                         "format": {"duration": "30.0"}})
    verdict = classify_probe("J:/a.m4a", ProbeOutcome(returncode=0, stdout=stdout))
    assert verdict.status is MediaStatus.NO_VIDEO_STREAM
    assert "no video stream" in verdict.detail.lower()


def test_an_empty_streams_list_has_no_video_stream():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(
        returncode=0, stdout=json.dumps({"streams": [], "format": {}})))
    assert verdict.status is MediaStatus.NO_VIDEO_STREAM


@pytest.mark.parametrize("kwargs,missing", [
    ({"width": 0}, "frame size"),
    ({"height": 0}, "frame size"),
    ({"width": -1920}, "frame size"),
    ({"stream_duration": None, "container_duration": None}, "duration"),
    ({"stream_duration": "0", "container_duration": "0"}, "duration"),
])
def test_unusable_geometry_or_duration_is_invalid_metadata(kwargs, missing):
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload(**kwargs)))
    assert verdict.status is MediaStatus.INVALID_METADATA
    assert missing in verdict.detail


def test_the_container_duration_is_the_fallback_when_the_stream_lacks_one():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(
        returncode=0, stdout=_payload(stream_duration=None, container_duration="7.25")))
    assert verdict.status is MediaStatus.METADATA_OK
    assert verdict.duration == pytest.approx(7.25)


def test_a_timeout_is_reported_as_unverified_and_never_as_corruption():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(timed_out=True))
    assert verdict.status is MediaStatus.PROBE_TIMEOUT
    assert "UNVERIFIED" in verdict.detail
    assert "not corrupt" in verdict.detail
    for word in ("corrupt video", "damaged", "broken"):
        assert word not in verdict.detail.lower().replace("not corrupt", "")


def test_a_timeout_wins_over_every_other_signal():
    """A timed-out probe may still carry a stale exit code; the timeout is the truth."""
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(
        timed_out=True, returncode=1, stderr="whatever", stdout=_payload()))
    assert verdict.status is MediaStatus.PROBE_TIMEOUT


def test_a_seam_failure_yields_no_opinion_about_the_file():
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(error="FileNotFoundError: ffprobe.exe"))
    assert verdict.status is MediaStatus.NOT_CHECKED
    assert "ffprobe.exe" in verdict.detail


@pytest.mark.parametrize("rate,expected", [
    ("30/1", 30.0), ("30000/1001", 29.97), ("25", 25.0),
    ("0/0", 0.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
])
def test_frame_rate_parsing_never_raises(rate, expected):
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(
        returncode=0, stdout=_payload(rate=rate)))
    assert verdict.fps == pytest.approx(expected, abs=0.01)


def test_non_finite_metadata_degrades_to_zero_rather_than_raising():
    stdout = json.dumps({"streams": [{"codec_type": "video", "codec_name": "h264",
                                      "width": "NaN", "height": "Infinity",
                                      "r_frame_rate": "30/1"}],
                         "format": {"duration": "10"}})
    verdict = classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout=stdout))
    assert verdict.status is MediaStatus.INVALID_METADATA


# ---------------------------------------------------------------------------
# source stability
# ---------------------------------------------------------------------------

def test_a_missing_source_is_unreadable_and_is_never_probed(tmp_path):
    calls = []

    def probe(path, timeout=None):
        calls.append(path)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([str(tmp_path / "gone.mp4")], probe)
    assert run.verdicts[0].status is MediaStatus.UNREADABLE
    assert "missing" in run.verdicts[0].detail
    assert calls == [], "a source that cannot be stat-ed must not cost a subprocess"


def test_a_file_changed_during_the_probe_is_inconclusive_even_on_a_clean_read(tmp_path):
    path = _video(tmp_path)

    def probe(p, timeout=None):
        # Overwrite with different content WHILE the probe is notionally reading it.
        with open(p, "wb") as handle:
            handle.write(b"\x01" * 4096)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([path], probe)
    verdict = run.verdicts[0]
    assert verdict.status is MediaStatus.CHANGED_DURING_CHECK, \
        "a clean FFprobe read of bytes that no longer exist is not a clean verdict"
    assert "inconclusive" in verdict.detail
    assert verdict.status is not MediaStatus.METADATA_OK


def test_a_file_changed_during_a_failing_probe_is_also_inconclusive(tmp_path):
    path = _video(tmp_path)

    def probe(p, timeout=None):
        with open(p, "wb") as handle:
            handle.write(b"\x02" * 8192)
        return ProbeOutcome(returncode=1, stderr="invalid data")

    run = verify_media_health([path], probe)
    assert run.verdicts[0].status is MediaStatus.CHANGED_DURING_CHECK


def test_an_unchanged_file_is_not_flagged_as_changed(tmp_path):
    path = _video(tmp_path)
    run = verify_media_health([path], _ok_probe())
    assert run.verdicts[0].status is MediaStatus.METADATA_OK


def test_fingerprint_is_size_and_mtime_and_is_none_when_unstattable(tmp_path):
    path = _video(tmp_path, content=b"abc")
    print_ = fingerprint_source(path)
    assert isinstance(print_, SourceFingerprint)
    assert print_.size == 3
    assert fingerprint_source(str(tmp_path / "nope.mp4")) is None


def test_fingerprint_is_not_presented_as_byte_identity():
    """The docstring must keep the limit explicit; a size+mtime pair is not a hash."""
    import beatsync_fork.media_verify as mv
    doc = (SourceFingerprint.__doc__ or "") + (mv.__doc__ or "")
    assert "byte identity" in doc or "whole-file" in doc


# ---------------------------------------------------------------------------
# the bounded run
# ---------------------------------------------------------------------------

def test_an_empty_library_is_handled_without_starting_a_pool():
    run = verify_media_health([], _ok_probe())
    assert run.verdicts == []
    assert run.scheduled == 0
    assert run.complete


def test_all_readable_sources_report_metadata_ok(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(12)]
    run = verify_media_health(paths, _ok_probe())
    assert [v.status for v in run.verdicts] == [MediaStatus.METADATA_OK] * 12
    assert run.complete
    assert aggregate_media(run.verdicts)[MediaStatus.METADATA_OK.value] == 12


def test_results_stay_in_source_order_however_probes_finish(tmp_path):
    """Completion order is deliberately inverted; report order must not move."""
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(8)]
    order = {p: i for i, p in enumerate(paths)}

    def probe(path, timeout=None):
        # Later sources return first.
        time.sleep(0.004 * (len(paths) - order[path]))
        return ProbeOutcome(returncode=0, stdout=_payload(codec=f"codec{order[path]}"))

    run = verify_media_health(paths, probe, workers=4)
    assert [v.path for v in run.verdicts] == paths
    assert [v.codec for v in run.verdicts] == [f"codec{i}" for i in range(len(paths))]


def test_a_duplicate_path_stays_two_independent_positions(tmp_path):
    path = _video(tmp_path)
    seen = {"n": 0}

    def probe(p, timeout=None):
        seen["n"] += 1
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([path, path, path], probe, workers=1)
    assert len(run.verdicts) == 3
    assert [v.path for v in run.verdicts] == [path] * 3
    assert seen["n"] == 3, "position is the authority; a duplicate is not deduplicated away"


def test_a_worker_exception_is_contained_and_never_becomes_a_clean_verdict(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(4)]

    def probe(path, timeout=None):
        if path.endswith("c1.mp4"):
            raise RuntimeError("probe seam exploded")
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(paths, probe, workers=2)
    statuses = [v.status for v in run.verdicts]
    assert statuses[1] is MediaStatus.NOT_CHECKED
    assert "RuntimeError" in run.verdicts[1].detail
    assert "probe seam exploded" in run.verdicts[1].detail
    assert statuses[0] is statuses[2] is statuses[3] is MediaStatus.METADATA_OK
    assert not run.complete, "a run containing an unchecked source is not complete"


def test_a_baseexception_in_a_worker_is_also_contained(tmp_path):
    path = _video(tmp_path)

    def probe(p, timeout=None):
        raise KeyboardInterrupt("ctrl-c inside a worker")

    run = verify_media_health([path], probe)
    assert run.verdicts[0].status is MediaStatus.NOT_CHECKED


def test_a_probe_returning_the_wrong_type_is_not_checked(tmp_path):
    path = _video(tmp_path)
    run = verify_media_health([path], lambda p, timeout=None: "surprise")
    assert run.verdicts[0].status is MediaStatus.NOT_CHECKED
    assert "ProbeOutcome" in run.verdicts[0].detail


def test_an_exhausted_budget_stops_scheduling_and_reports_partial(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(40)]
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 0.5
        return ticks["t"]

    run = verify_media_health(paths, _ok_probe(), workers=1, budget_seconds=3.0, clock=clock)
    assert run.budget_exhausted, "the budget must have stopped scheduling"
    assert run.scheduled < len(paths)
    assert not run.complete
    counts = aggregate_media(run.verdicts)
    assert counts[MediaStatus.NOT_CHECKED.value] > 0
    assert counts[MediaStatus.NOT_CHECKED.value] == len(paths) - run.scheduled
    # Every source still has an entry, in order, and no skipped source became a success.
    assert [v.path for v in run.verdicts] == paths
    for verdict in run.verdicts:
        if verdict.status is MediaStatus.NOT_CHECKED:
            assert "budget was exhausted" in verdict.detail


def test_a_timed_out_probe_is_a_timeout_not_a_success(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(3)]

    def probe(path, timeout=None):
        if path.endswith("c1.mp4"):
            return ProbeOutcome(timed_out=True)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(paths, probe)
    assert run.verdicts[1].status is MediaStatus.PROBE_TIMEOUT
    counts = aggregate_media(run.verdicts)
    assert counts[MediaStatus.METADATA_OK.value] == 2
    assert counts[MediaStatus.PROBE_TIMEOUT.value] == 1


def test_a_slow_probe_does_not_block_the_other_workers(tmp_path):
    """A hanging source must not stop the rest of the library being verified."""
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(6)]
    release = threading.Event()

    def probe(path, timeout=None):
        if path.endswith("c0.mp4"):
            release.wait(5)
            return ProbeOutcome(timed_out=True)
        return ProbeOutcome(returncode=0, stdout=_payload())

    done = threading.Event()
    result = {}

    def drive():
        result["run"] = verify_media_health(paths, probe, workers=4)
        done.set()

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    time.sleep(0.2)
    release.set()
    assert done.wait(10), "the run never finished"
    run = result["run"]
    assert run.verdicts[0].status is MediaStatus.PROBE_TIMEOUT
    assert all(v.status is MediaStatus.METADATA_OK for v in run.verdicts[1:])


def test_the_per_file_timeout_is_passed_to_every_probe(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(3)]
    seen = []

    def probe(path, timeout=None):
        seen.append(timeout)
        return ProbeOutcome(returncode=0, stdout=_payload())

    verify_media_health(paths, probe, per_file_timeout=7)
    assert seen == [7, 7, 7], "every probe must be bounded"


def test_concurrency_never_exceeds_the_cap_even_when_asked_for_more(tmp_path):
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(60)]
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def probe(path, timeout=None):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.003)
        with guard:
            live["now"] -= 1
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(paths, probe, workers=512)
    assert run.workers <= MAX_PROBE_WORKERS
    assert live["peak"] <= MAX_PROBE_WORKERS, \
        f"peak concurrent probes {live['peak']} exceeded the cap {MAX_PROBE_WORKERS}"


def test_a_large_library_never_has_more_than_the_cap_in_flight(tmp_path):
    """More than a thousand sources, with no unbounded subprocess submission."""
    path = _video(tmp_path)
    paths = [path] * 1200
    live = {"now": 0, "peak": 0}
    guard = threading.Lock()

    def probe(p, timeout=None):
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        with guard:
            live["now"] -= 1
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(paths, probe, workers=4)
    assert len(run.verdicts) == 1200
    assert run.scheduled == 1200
    assert live["peak"] <= MAX_PROBE_WORKERS
    assert run.complete


@pytest.mark.parametrize("requested,expected", [
    (None, DEFAULT_PROBE_WORKERS), (0, 1), (1, 1), (3, 3), (4, 4),
    (5, MAX_PROBE_WORKERS), (9999, MAX_PROBE_WORKERS), (-7, 1),
    ("nonsense", DEFAULT_PROBE_WORKERS), (2.9, 2),
])
def test_worker_count_is_always_clamped_into_the_cap(requested, expected):
    assert resolve_worker_count(requested) == expected


def test_the_cap_and_the_defaults_are_the_measured_bounds():
    assert MAX_PROBE_WORKERS == 4, "D0 measured four workers on the mechanical source drive"
    assert DEFAULT_PROBE_WORKERS <= MAX_PROBE_WORKERS
    assert DEFAULT_PROBE_TIMEOUT_SECONDS > 0
    assert DEFAULT_BUDGET_SECONDS > 0


def test_the_budget_only_stops_scheduling_and_never_abandons_live_work(tmp_path):
    """Every probe that STARTED must have been reaped before the run reports."""
    paths = [_video(tmp_path, f"c{i}.mp4") for i in range(24)]
    started, finished = [], []
    guard = threading.Lock()
    ticks = {"t": 0.0}

    def clock():
        ticks["t"] += 0.25
        return ticks["t"]

    def probe(path, timeout=None):
        with guard:
            started.append(path)
        time.sleep(0.002)
        with guard:
            finished.append(path)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(paths, probe, workers=2, budget_seconds=2.0, clock=clock)
    assert run.budget_exhausted
    assert len(started) == len(finished), "a started probe was not reaped"
    assert sorted(started) == sorted(finished)


def test_work_budget_arithmetic():
    budget = WorkBudget(10.0, started=100.0)
    assert not budget.exhausted(105.0)
    assert budget.remaining(105.0) == pytest.approx(5.0)
    assert budget.exhausted(110.0)
    assert budget.remaining(999.0) == 0.0


# ---------------------------------------------------------------------------
# unicode, spaces and long paths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "clip with spaces.mp4",
    "\u30d5\u30a1\u30a4\u30eb\u540d.mp4",
    "f\u00fcnf \u00e4\u00df \u2014 clip (1).mp4",
    "\u30af\u30ea\u30c3\u30d7 space \u00fc.mkv",
])
def test_unicode_and_spaced_windows_filenames_round_trip(tmp_path, name):
    path = _video(tmp_path, name)
    run = verify_media_health([path], _ok_probe())
    assert run.verdicts[0].status is MediaStatus.METADATA_OK
    assert run.verdicts[0].path == path


def test_a_long_failing_path_is_never_shortened_in_the_report():
    """S1's MAJOR-1 restated for this surface: the path IS the identification."""
    long_path = "J:/library/" + "d" * 900 + "/0_279-overwritten.mp4"
    run = _run_with([
        (long_path, ProbeOutcome(returncode=1, stderr="moov atom not found")),
    ])
    report = format_verification_report(
        folder="J:/library", recursive=True, verified_at="2026-10-14 01:00:00",
        source_count=1, cache=None, run=run)
    assert long_path in report, "the complete failing path must survive into the report"
    assert "\u2026" not in report.split(long_path)[0][-40:]


def _run_with(pairs):
    """Build a VerificationRun from explicit (path, outcome) pairs, without touching the disk."""
    from beatsync_fork.media_verify import MediaVerdict, VerificationRun
    verdicts = [classify_probe(path, outcome) for path, outcome in pairs]
    run = VerificationRun(verdicts=verdicts, scheduled=len(verdicts))
    assert all(isinstance(v, MediaVerdict) for v in verdicts)
    return run


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _cache(prepared=0, new_or_changed=0, incomplete=0, unavailable=0):
    """Classifier-shaped output: the enum **values** `classify_library_sources` actually emits.

    Built from `PrepStatus` / `NeedReason` rather than from literals, which is the whole of R2's
    MINOR-1. The previous spelling used the enum *member names* (`"NEEDS_ANALYSIS"`,
    `"NEW_OR_CHANGED"`, ...) — a shape the producer never emits and that *neither* consumer
    handles: the report's filters silently matched none of it, and
    `library_prep.SourceClassification.from_mapping` fail-closes it to
    `SOURCE_IDENTITY_UNAVAILABLE` and discards the reason. Because the old fixture agreed with the
    old uppercase comparisons it was self-consistently blind, and that is precisely why four MAJOR
    reporting defects survived a passing suite. Keep this derived from the enums.
    """
    items = []
    items += [{"path": f"J:/new{i}.mp4", "status": PrepStatus.NEEDS_ANALYSIS.value,
               "reason": NeedReason.NEW_OR_CHANGED.value} for i in range(new_or_changed)]
    items += [{"path": f"J:/inc{i}.mp4", "status": PrepStatus.NEEDS_ANALYSIS.value,
               "reason": NeedReason.INCOMPLETE_OR_INVALID.value} for i in range(incomplete)]
    items += [{"path": f"J:/gone{i}.mp4", "status": PrepStatus.SOURCE_IDENTITY_UNAVAILABLE.value,
               "reason": ""} for i in range(unavailable)]
    items += [{"path": f"J:/ok{i}.mp4", "status": PrepStatus.PREPARED.value, "reason": ""}
              for i in range(prepared)]
    return {
        "source_count": len(items),
        "prepared_count": prepared,
        "needs_analysis_count": new_or_changed + incomplete,
        "unavailable_count": unavailable,
        "classifications": items,
    }, items


def test_the_report_has_two_independent_sections_and_a_snapshot_header():
    cache, items = _cache(prepared=3, new_or_changed=2, incomplete=1, unavailable=1)
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/library", recursive=False, verified_at="2026-10-14 01:02:03",
        source_count=7, cache=cache, cache_classifications=items, run=run,
        folder_scan_seconds=1.25, cache_seconds=2.5)

    assert "CACHE VERIFICATION" in report
    assert "MEDIA HEALTH" in report
    assert report.index("CACHE VERIFICATION") < report.index("MEDIA HEALTH")
    # The snapshot header: which library, which setting, when.
    assert "J:/library" in report
    assert "Include subfolders: no" in report
    assert "2026-10-14 01:02:03" in report
    # Cache counts, each reported separately.
    assert "Reusable cache (PREPARED):     3" in report
    assert "New or changed:                2" in report
    assert "Incomplete / invalid record:   1" in report
    assert "Source identity unavailable:   1" in report
    # Elapsed times are real, not omitted.
    assert "1.25s" in report and "2.50s" in report
    # Every problem path is named.
    for item in items:
        if item["status"] != PrepStatus.PREPARED.value:
            assert item["path"] in report


def test_the_report_always_carries_the_metadata_not_decode_sentence():
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=run)
    assert METADATA_NOT_DECODE_TEXT in report
    assert "full video decoding not verified" in report


def test_an_unverifiable_backend_is_not_reported_as_a_damaged_cache():
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache={"source_count": 1, "prepared_count": 0, "needs_analysis_count": 0,
               "unavailable_count": 0, "classifications": []},
        run=run,
        cache_unverifiable_reason="The Qwen backend identity could not be verified.")
    assert "UNVERIFIABLE" in report
    assert "says nothing about whether the existing cache is good" in report
    # It must NOT present a fabricated zero-reuse verdict.
    assert "Reusable cache (PREPARED):     0" not in report
    # Media health is still reported: the two questions are independent.
    assert "MEDIA HEALTH" in report
    assert "Metadata verified:             1" in report


def test_a_partial_media_run_is_labelled_partial():
    run = _run_with([
        ("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload())),
        ("J:/b.mp4", ProbeOutcome(error="seam failed")),
    ])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=2,
        cache=None, run=run)
    assert "PARTIAL / INCOMPLETE" in report
    assert "NO health verdict" in report
    assert "COMPLETE \u2014 every source" not in report


def test_a_budget_exhausted_run_is_labelled_partial_even_with_no_unchecked_entries():
    from beatsync_fork.media_verify import VerificationRun
    run = VerificationRun(
        verdicts=[classify_probe("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))],
        scheduled=1, budget_exhausted=True)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=run)
    assert "PARTIAL / INCOMPLETE" in report


def test_a_fully_clean_run_says_complete_and_nothing_stronger():
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=run)
    assert "COMPLETE" in report
    assert "metadata only" in report
    assert "No metadata problems found" in report
    for forbidden in ("no corruption", "files are intact", "fully verified", "all frames"):
        assert forbidden not in report.lower()


def test_many_failures_are_bounded_with_an_explicit_omitted_count():
    pairs = [(f"J:/broken{i:04d}.mp4", ProbeOutcome(returncode=1, stderr="bad"))
             for i in range(200)]
    run = _run_with(pairs)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=200,
        cache=None, run=run)
    shown = sum(1 for p, _ in pairs if p in report)
    assert shown == MAX_REPORT_DETAIL_LINES, f"expected a bounded list, showed {shown}"
    assert f"and {200 - MAX_REPORT_DETAIL_LINES} more not shown" in report
    assert "MEDIA_UNREADABLE (200)" in report, "the true total must still be stated"


def test_every_problem_status_gets_its_own_report_block():
    run = _run_with([
        ("J:/ok.mp4", ProbeOutcome(returncode=0, stdout=_payload())),
        ("J:/unreadable.mp4", ProbeOutcome(returncode=1, stderr="moov atom not found")),
        ("J:/audio.m4a", ProbeOutcome(returncode=0, stdout=json.dumps(
            {"streams": [{"codec_type": "audio"}], "format": {}}))),
        ("J:/bad.mp4", ProbeOutcome(returncode=0, stdout="{oops")),
        ("J:/slow.mp4", ProbeOutcome(timed_out=True)),
        ("J:/noseam.mp4", ProbeOutcome(error="ffprobe missing")),
    ])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=6,
        cache=None, run=run)
    for status in (MediaStatus.UNREADABLE, MediaStatus.NO_VIDEO_STREAM,
                   MediaStatus.INVALID_METADATA, MediaStatus.PROBE_TIMEOUT,
                   MediaStatus.NOT_CHECKED):
        assert status.value in report, status
    assert "J:/ok.mp4" not in report, "a healthy source is counted, not listed"


def test_the_clean_cache_case_says_so():
    cache, items = _cache(prepared=5)
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=5,
        cache=cache, cache_classifications=items, run=run)
    assert "Every source has a reusable Stage-5 cache record." in report


def test_aggregate_media_covers_every_status():
    counts = aggregate_media([])
    assert set(counts) == {s.value for s in MediaStatus}
    assert all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# the FFprobe seam in ffmpeg_processing.py
# ---------------------------------------------------------------------------

def _load_probe_seam():
    """Execute the REAL `probe_media_metadata` + argv builder over a stubbed runner."""
    import subprocess as sp
    tree = _tree(_FFMPEG)
    node_cmd = _func(tree, "build_ffprobe_metadata_command")
    node_probe = _func(tree, "probe_media_metadata")
    calls = []

    def runner(cmd, timeout=None, lifecycle=None):
        calls.append({"cmd": cmd, "timeout": timeout, "lifecycle": lifecycle})
        return behaviour["fn"](cmd, timeout)

    behaviour = {"fn": lambda cmd, timeout: sp.CompletedProcess(cmd, 0, "{}", "")}
    namespace = {
        "FFPROBE_PATH": r"C:\bin\ffmpeg\ffprobe.exe",
        "_run_media_command": runner,
        "_MEDIA_VERIFY_PROBE_TIMEOUT": DEFAULT_PROBE_TIMEOUT_SECONDS,
        "ProbeOutcome": ProbeOutcome,
        "subprocess": sp,
        "List": list,
    }
    exec(compile(ast.Module(body=[node_cmd, node_probe], type_ignores=[]), _FFMPEG, "exec"),
         namespace)
    return namespace, calls, behaviour


def test_the_ffprobe_command_is_an_argv_array_reading_metadata_only():
    ns, _calls, _b = _load_probe_seam()
    cmd = ns["build_ffprobe_metadata_command"](r"J:\lib\a b\clip ü.mp4")
    assert isinstance(cmd, list) and all(isinstance(part, str) for part in cmd)
    assert cmd[0].endswith("ffprobe.exe")
    assert cmd[-1] == r"J:\lib\a b\clip ü.mp4", "the path is one argv element, never quoted inline"
    assert "-print_format" in cmd and "json" in cmd
    assert "-show_streams" in cmd and "-show_format" in cmd
    # Nothing that would decode, write or transcode.
    for forbidden in ("-f", "-c:v", "-vf", "-y", "-frames:v", "-i"):
        assert forbidden not in cmd, forbidden
    assert not any("ffmpeg.exe" in part for part in cmd)


def test_the_probe_seam_passes_a_finite_timeout_and_no_lifecycle():
    ns, calls, _b = _load_probe_seam()
    ns["probe_media_metadata"]("J:/a.mp4", timeout=11)
    assert calls[0]["timeout"] == 11
    assert calls[0]["lifecycle"] is None, "verification is not a render; no cancellation lifecycle"
    ns["probe_media_metadata"]("J:/a.mp4")
    assert calls[1]["timeout"] == DEFAULT_PROBE_TIMEOUT_SECONDS
    assert calls[1]["timeout"] > 0


def test_the_probe_seam_maps_each_failure_mode_distinctly():
    import subprocess as sp
    ns, _calls, behaviour = _load_probe_seam()

    behaviour["fn"] = lambda cmd, timeout: sp.CompletedProcess(cmd, 0, '{"streams":[]}', "")
    out = ns["probe_media_metadata"]("J:/a.mp4")
    assert (out.returncode, out.timed_out, out.error) == (0, False, "")

    behaviour["fn"] = lambda cmd, timeout: sp.CompletedProcess(cmd, 1, "", "moov atom not found")
    out = ns["probe_media_metadata"]("J:/a.mp4")
    assert out.returncode == 1 and "moov" in out.stderr and not out.timed_out

    def raise_timeout(cmd, timeout):
        raise sp.TimeoutExpired(cmd, timeout)

    behaviour["fn"] = raise_timeout
    out = ns["probe_media_metadata"]("J:/a.mp4")
    assert out.timed_out is True and out.error == ""

    def raise_oserror(cmd, timeout):
        raise FileNotFoundError("ffprobe.exe is missing")

    behaviour["fn"] = raise_oserror
    out = ns["probe_media_metadata"]("J:/a.mp4")
    assert out.timed_out is False
    assert "FileNotFoundError" in out.error and "missing" in out.error


def test_the_existing_extraction_and_render_commands_are_untouched(ffmpeg_tree):
    """The additive seam must not have edited a render-path builder."""
    source = ast.unparse(ffmpeg_tree)
    assert "timeout=120" in source, "the extraction bound is unchanged"
    for name in ("extract_clip_segment_ffmpeg_detailed", "extract_clip_segment_ffmpeg",
                 "concatenate_videos_ffmpeg", "convert_to_prores_proxy"):
        _func(ffmpeg_tree, name)
    # The new probe goes through the EXISTING bounded runner and adds no subprocess site of its
    # own. Asserted over referenced names, not source text.
    probe = _referenced(_func(ffmpeg_tree, "probe_media_metadata"))
    assert "_run_media_command" in probe
    for forbidden in ("remove", "unlink", "rmtree", "move", "copy", "open",
                      "_save_cache", "_checkpoint_cache", "run", "Popen", "system"):
        assert forbidden not in probe, forbidden


# ---------------------------------------------------------------------------
# the GUI integration: concurrency, live inputs, output isolation
# ---------------------------------------------------------------------------

class _FakeRequest:
    """What Gradio injects: an object carrying a `session_hash` and nothing this code needs.

    Deliberately not a Gradio import — these tests run on a bare interpreter. The real injection
    (and the fact that `session_hash` is per-session and distinct between two clients) is measured
    against the pinned gradio==6.19.0 outside the suite; what is asserted here is the BEHAVIOUR the
    handler builds on top of it.
    """

    def __init__(self, session_hash):
        self.session_hash = session_hash


class _FakeGradio:
    """Stands in for the `gr` module so the real signatures' annotations can be evaluated."""

    Request = _FakeRequest


#: The verification concurrency seam, in the order the shipped module defines it.
#
# [FORK] Digital-Union (MEDIA-VERIFY-S2-B P2): `_mark_lock_holder` / `_lock_holder_now` /
# `_scoped_key` joined the seam when the two-holder boolean became a three-way holder token and the
# revision key gained a scope. `_mark_verification` is retained as the verification path's own
# spelling and still extracted, so every assertion below still exercises the shipped call chain.
_CONCURRENCY_SEAM_FUNCTIONS = (
    "_mark_lock_holder", "_lock_holder_now", "_mark_verification",
    "_busy_message", "_verify_busy_message", "_sample_busy_message",
    "_verify_session_key", "_scoped_key",
    "_touch_verify_revision_locked", "_verify_revision",
    "_bump_verify_revision", "_begin_verify_session", "_end_verify_session",
    "_on_verify_sources_click", "_on_verify_invalidate",
)


def _module_constant(tree, name):
    """The shipped module-level value of `name`, as a literal.

    [FORK] Digital-Union (MEDIA-VERIFY-S2-A R5, accepted R4 review finding): the seam used to
    inject `_VERIFY_REVISION_CAP = 512` as a literal of its own. That made the eviction tests
    blind to the production constant -- the R4 reviewer mutated the shipped cap to 2, which makes
    the stale-publication defect trivially reachable, and the whole file stayed green. Reading the
    real assignment is what makes a cap change observable here.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"module-level assignment {name} not found in {_GUI}")


def _load_concurrency_seam(cap=None):
    """Execute the REAL verification concurrency seam over a real Lock and a stubbed impl.

    Every function in the chain is the shipped source, extracted by AST: the mutex gate, the two
    busy-message helpers and the whole session-revision mechanism including the R5 active-session
    pin. Only `_verify_sources_impl` (the disk work) is stubbed, because that is the part that
    would need a library.

    `_VERIFY_REVISION_CAP` and `_verify_active_session` come from the shipped assignments, not from
    literals here. `cap` overrides the capacity for the tests that need eviction pressure to be
    cheap -- it shrinks the bound in the TEST namespace only and never edits production source.
    """
    tree = _tree(_GUI)
    nodes = [_func(tree, name) for name in _CONCURRENCY_SEAM_FUNCTIONS]
    state = {"impl_calls": [], "impl": lambda folder, recursive: "REPORT"}
    namespace = {
        "gr": _FakeGradio,
        "threading": threading,
        "_RENDER_LOCK": threading.Lock(),
        "_VERIFY_FLAG_LOCK": threading.Lock(),
        # [FORK] S2-B P2: the holder token and its three shipped values, read from the module
        # rather than restated, for the same reason `_VERIFY_REVISION_CAP` is.
        # [FORK] S2-B P2 (Option B): the holder lookup reads the EXISTING active-render slot as
        # positive evidence of a render, so the seam needs the slot and its own lock. The two are
        # never held together -- the token is copied and released, then the slot is.
        "_ACTIVE_RENDER_SLOT_LOCK": threading.Lock(),
        "_active_render_slot": None,
        "_lock_holder": _module_constant(tree, "_lock_holder"),
        "_LOCK_HOLDER_RENDER": _module_constant(tree, "_LOCK_HOLDER_RENDER"),
        "_LOCK_HOLDER_VERIFY": _module_constant(tree, "_LOCK_HOLDER_VERIFY"),
        "_LOCK_HOLDER_SAMPLE": _module_constant(tree, "_LOCK_HOLDER_SAMPLE"),
        "_SCOPE_VERIFY": _module_constant(tree, "_SCOPE_VERIFY"),
        "_SCOPE_SAMPLE": _module_constant(tree, "_SCOPE_SAMPLE"),
        "_verify_revisions": dict(_module_constant(tree, "_verify_revisions") or {}),
        "_VERIFY_REVISION_CAP": (_module_constant(tree, "_VERIFY_REVISION_CAP")
                                 if cap is None else int(cap)),
        "_verify_active_session": _module_constant(tree, "_verify_active_session"),
        "RENDER_BUSY_MESSAGE": "RENDER_BUSY",
        "VERIFY_BUSY_MESSAGE": "VERIFY_BUSY",
        "SAMPLE_BUSY_MESSAGE": "SAMPLE_BUSY",
        "MEDIA_BUSY_UNKNOWN_TEXT": "BUSY_UNKNOWN_HOLDER",
        "PREP_VERIFY_RENDER_BUSY_TEXT": "VERIFY_REFUSED_RENDER_ACTIVE",
        "PREP_VERIFY_ALREADY_RUNNING_TEXT": "VERIFY_REFUSED_ALREADY_RUNNING",
        "PREP_VERIFY_SAMPLE_BUSY_TEXT": "VERIFY_REFUSED_SAMPLE_ACTIVE",
        "PREP_VERIFY_STALE_TEXT": "STALE_FOLDER_CHANGED",
        "PREP_SAMPLE_RENDER_BUSY_TEXT": "SAMPLE_REFUSED_RENDER_ACTIVE",
        "PREP_SAMPLE_VERIFY_BUSY_TEXT": "SAMPLE_REFUSED_VERIFY_ACTIVE",
        "PREP_SAMPLE_ALREADY_RUNNING_TEXT": "SAMPLE_REFUSED_ALREADY_RUNNING",
        "PREP_SAMPLE_STALE_TEXT": "STALE_SAMPLE_CHANGED",
    }

    def impl(folder, recursive):
        state["impl_calls"].append((folder, recursive))
        return state["impl"](folder, recursive)

    namespace["_verify_sources_impl"] = impl
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _GUI, "exec"), namespace)
    return namespace, state


def test_verification_refuses_immediately_while_a_render_holds_the_mutex():
    ns, state = _load_concurrency_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "precondition: a render takes the lock"
    # [FORK] S2-B P2: a real render marks itself after acquiring, so the simulation must too. An
    # acquire with no holder published is a genuinely DIFFERENT state, covered by its own tests.
    ns["_mark_lock_holder"](ns["_LOCK_HOLDER_RENDER"])
    try:
        out = ns["_on_verify_sources_click"]("J:/lib", True)
    finally:
        ns["_RENDER_LOCK"].release()
    assert out == "VERIFY_REFUSED_RENDER_ACTIVE"
    assert state["impl_calls"] == [], "no verification work may begin while a render runs"


def test_a_render_is_refused_while_verification_holds_the_mutex():
    """The other direction, through the REAL busy-message helper."""
    ns, state = _load_concurrency_seam()
    observed = {}

    def impl(folder, recursive):
        # Mid-verification: this is exactly the moment a render click would arrive.
        observed["render_can_start"] = ns["_RENDER_LOCK"].acquire(blocking=False)
        observed["message"] = ns["_busy_message"]()
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/lib", True) == "REPORT"
    assert observed["render_can_start"] is False, \
        "a render must not be able to take the mutex while verification holds it"
    assert observed["message"] == "VERIFY_BUSY", "the refusal must name the real holder"


def test_the_mutex_is_released_after_success():
    ns, _state = _load_concurrency_seam()
    ns["_on_verify_sources_click"]("J:/lib", True)
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the lock leaked after a successful run"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "the holder token leaked after a success"


def test_the_mutex_is_released_after_a_failure():
    ns, state = _load_concurrency_seam()

    def boom(folder, recursive):
        raise RuntimeError("scanner exploded")

    state["impl"] = boom
    out = ns["_on_verify_sources_click"]("J:/lib", True)
    assert "SOURCE VERIFICATION FAILED" in out
    assert "RuntimeError" in out and "scanner exploded" in out
    assert "No analysis, cache change, media change or render was performed." in out
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the lock leaked after an exception"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "the holder token leaked after an exception"


def test_the_mutex_is_released_even_when_the_impl_raises_basexception():
    ns, state = _load_concurrency_seam()

    def abandoned(folder, recursive):
        raise KeyboardInterrupt("request abandoned")

    state["impl"] = abandoned
    with pytest.raises(KeyboardInterrupt):
        ns["_on_verify_sources_click"]("J:/lib", True)
    assert ns["_RENDER_LOCK"].acquire(blocking=False), \
        "an abandoned request must not leave the app permanently busy"
    ns["_RENDER_LOCK"].release()
    assert ns["_lock_holder_now"]() == "", "a BaseException must clear the holder token too"


def test_two_concurrent_verifications_do_not_overlap():
    ns, state = _load_concurrency_seam()
    inside = threading.Event()
    hold = threading.Event()
    results = []

    def impl(folder, recursive):
        inside.set()
        hold.wait(5)
        return "REPORT"

    state["impl"] = impl
    first = threading.Thread(target=lambda: results.append(
        ns["_on_verify_sources_click"]("J:/lib", True)), daemon=True)
    first.start()
    assert inside.wait(5)
    second = ns["_on_verify_sources_click"]("J:/lib", True)
    hold.set()
    first.join(5)
    # R3: the refusal now names the REAL holder. A second verification is not a render, and
    # saying "a render is running" when none is would be a wrong answer.
    assert second == "VERIFY_REFUSED_ALREADY_RUNNING"
    assert results == ["REPORT"]


def test_the_click_handler_takes_only_the_live_folder_and_recursive_widgets(gui_tree):
    """No `prep_state`: a queued change event must not let it verify the previous folder."""
    handler = _func(gui_tree, "_on_verify_sources_click")
    arg_names = [a.arg for a in handler.args.args]
    # R3 added `request`, which is Gradio's INJECTED `gr.Request` and not a widget: it never
    # appears in `inputs`, and it carries only the session identity.
    assert arg_names == ["folder_path", "recursive", "request"], arg_names
    annotation = ast.unparse(handler.args.args[2].annotation)
    assert annotation == "gr.Request", annotation
    assert ast.unparse(handler.args.defaults[-1]) == "None", \
        "the request must default to None so the handler stays callable without Gradio"

    source = ast.unparse(gui_tree)
    call = next(seg for seg in source.split("prep_verify_btn.click(")[1:])
    wiring = call.split(")")[0]
    assert "inputs=[prep_folder, prep_recursive]" in wiring, wiring
    assert "prep_state" not in wiring, "verification must not read the recorded scan"
    assert "request" not in wiring, "the injected request is not a widget input"


def test_the_verification_handler_writes_only_its_own_report(gui_tree):
    source = ast.unparse(gui_tree)
    wiring = source.split("prep_verify_btn.click(")[1].split("show_progress")[0]
    assert "outputs=[prep_verify_report]" in wiring, wiring
    for forbidden in ("status_output", "video_output", "process_btn", "source_state",
                      "render_invocation_state", "director_proposal_state",
                      "variant_batch_state", "prep_outputs", "source_outputs",
                      "prep_state", "prep_report", "prep_status", "prep_analyze_btn",
                      "session_state"):
        assert forbidden not in wiring, f"{forbidden} must not be a verification output"


def test_the_verification_report_is_not_in_the_preparation_or_source_output_lists(gui_tree):
    source = ast.unparse(gui_tree)
    prep_outputs = source.split("prep_outputs = [")[1].split("]")[0]
    assert "prep_verify_report" not in prep_outputs
    source_outputs = source.split("source_outputs = [")[1].split("]")[0]
    assert "prep_verify_report" not in source_outputs
    assert "prep_verify_btn" not in source_outputs
    assert "prep_verify_btn" not in prep_outputs


def test_changing_the_folder_or_recursive_setting_clears_the_stale_report(gui_tree):
    source = ast.unparse(gui_tree)
    for widget in ("prep_folder", "prep_recursive"):
        blocks = [seg for seg in source.split(f"{widget}.change(")[1:]]
        invalidators = [b for b in blocks if "_on_verify_invalidate" in b.split(")")[0]]
        assert invalidators, f"{widget} must clear the verification report"
        wiring = invalidators[0].split(")")[0]
        assert "outputs=[prep_verify_report]" in wiring, wiring
        # It must not have been bolted onto the preparation state machine.
        assert "prep_state" not in wiring
        assert "prep_outputs" not in wiring


def test_the_preparation_event_wiring_is_unchanged(gui_tree):
    """Scan and Analyze must be exactly as they were: same inputs, same outputs."""
    source = ast.unparse(gui_tree)
    assert "prep_outputs = [prep_report, prep_status, prep_analyze_btn, prep_state]" in source
    scan = source.split("prep_scan_btn.click(")[1].split(")")[0]
    assert "inputs=[prep_folder, prep_recursive, prep_state]" in scan
    assert "outputs=prep_outputs" in scan
    analyze = source.split("prep_analyze_btn.click(")[1].split(")")[0]
    assert "inputs=[prep_folder, prep_recursive, prep_batch_size, prep_state]" in analyze
    assert "outputs=prep_outputs" in analyze
    # Verification is not wired into either.
    assert "_on_verify_sources_click" not in scan
    assert "_on_verify_sources_click" not in analyze


def test_the_render_gate_still_refuses_on_a_single_nonblocking_acquire(gui_tree):
    """The exclusion must stay a test-and-set, never a `locked()` check-then-act."""
    source = ast.unparse(gui_tree)
    # [FORK] S2-B P2: counted STRUCTURALLY over `ast.Call` nodes, not by substring.
    # `ast.unparse` emits docstrings too, so a census over the unparsed text counted the mutex
    # contract explained in `_lock_holder_now`'s own docstring as a fifth call site. A ban or a
    # census that searches prose fails on the prose that documents it.
    acquires = [node for node in ast.walk(gui_tree)
                if isinstance(node, ast.Call)
                and ast.unparse(node).startswith("_RENDER_LOCK.acquire")]
    assert len(acquires) == 4, \
        f"two render sites plus verification plus sample decode: {[ast.unparse(a) for a in acquires]}"
    for call in acquires:
        rendered = ast.unparse(call)
        assert rendered == "_RENDER_LOCK.acquire(blocking=False)", \
            f"every acquire must be the single non-blocking test-and-set, got {rendered}"
    locked = [node for node in ast.walk(gui_tree)
              if isinstance(node, ast.Call) and ast.unparse(node) == "_RENDER_LOCK.locked()"]
    assert not locked, "check-then-act is the race this avoids"
    # Every acquire has a matching release, and verification's is in a finally.
    releases = [node for node in ast.walk(gui_tree)
                if isinstance(node, ast.Call) and ast.unparse(node) == "_RENDER_LOCK.release()"]
    assert len(releases) == 4, f"one release per acquire, got {len(releases)}"
    handler = ast.unparse(_func(gui_tree, "_on_verify_sources_click"))
    tried = [n for n in ast.walk(_func(gui_tree, "_on_verify_sources_click"))
             if isinstance(n, ast.Try) and n.finalbody]
    assert tried, "the release must sit in a finally"
    assert "_RENDER_LOCK.release()" in ast.unparse(tried[0].finalbody[-1]) or \
           any("_RENDER_LOCK.release()" in ast.unparse(s) for s in tried[0].finalbody)
    assert "RenderLifecycle" not in handler, "verification owns no render lifecycle"


def test_no_lock_thread_or_process_object_is_stored_in_gradio_state(gui_tree):
    handler = _referenced(_func(gui_tree, "_on_verify_sources_click"))
    impl = _referenced(_func(gui_tree, "_verify_sources_impl"))
    for forbidden in ("gr.State", "State", "session_state", "Thread", "Popen", "Event",
                      "RenderLifecycle", "_install_active_render", "_clear_active_render",
                      "_active_render_slot"):
        assert forbidden not in handler, forbidden
        assert forbidden not in impl, forbidden
    # Neither function takes or returns any Gradio state object. R3's `request` is Gradio's
    # injected `gr.Request`, which is a per-request value object, not session state -- and the
    # handler reads exactly one attribute off it.
    assert [a.arg for a in _func(gui_tree, "_verify_sources_impl").args.args] == \
        ["folder_path", "recursive"]
    click = _func(gui_tree, "_on_verify_sources_click")
    assert [a.arg for a in click.args.args] == ["folder_path", "recursive", "request"]
    key_node = _func(gui_tree, "_verify_session_key")
    attributes_read = {n.value for n in ast.walk(key_node)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "session_hash" in attributes_read, \
        "the session key must come from the request's session_hash"
    session_key = _referenced(key_node)
    for forbidden in ("gr.State", "Thread", "Popen", "Lock", "RenderLifecycle",
                      "prep_state", "source_state"):
        assert forbidden not in session_key, forbidden
    # The revision map holds integers only: there is no code path that could put a live
    # synchronisation object, thread or handle into it.
    for name in ("_verify_revision", "_bump_verify_revision"):
        referenced = _referenced(_func(gui_tree, name))
        for forbidden in ("Thread", "Popen", "Event", "RenderLifecycle", "gr.State",
                          "session_state", "_RENDER_LOCK"):
            assert forbidden not in referenced, (name, forbidden)


def test_verification_never_launches_qwen_analysis_or_writes_anything(gui_tree):
    impl = ast.unparse(_func(gui_tree, "_verify_sources_impl"))
    # It asks the cache question through the EXISTING read-only classifier.
    assert "classify_library_sources" in impl
    for forbidden in (
        "analyze_video_sources", "_analyze_single_video", "_save_cache", "_checkpoint_cache",
        "_load_cache", "stage5_qwen_scene_worker", "subprocess", "Popen", "os.remove",
        "os.unlink", "shutil.rmtree", "shutil.move", "create_music_video",
        "analyze_beats_auto", "build_planned_clip_sequence", "open(",
    ):
        assert forbidden not in impl, forbidden
    # And it reuses the existing runtime resolution rather than inventing a second one.
    assert "_resolve_prep_qwen_runtime" in impl
    assert "scan_library_folder" in impl


def test_the_impl_reports_an_unverifiable_backend_without_inventing_counts(gui_tree):
    impl = ast.unparse(_func(gui_tree, "_verify_sources_impl"))
    assert "ai_cache_disabled" in impl
    assert "cache_unverifiable_reason" in impl


def test_the_impl_reads_the_live_widgets_and_rescans_the_folder(gui_tree):
    impl_node = _func(gui_tree, "_verify_sources_impl")
    assert [a.arg for a in impl_node.args.args] == ["folder_path", "recursive"]
    impl = ast.unparse(impl_node)
    assert "scan_library_folder(folder, recursive=recursive" in impl, \
        "the live recursive flag must drive a fresh enumeration"
    assert "detect_duplicates=False" in impl


def test_the_verification_button_is_inside_the_preparation_accordion(gui_tree):
    source = ast.unparse(gui_tree)
    assert "prep_verify_btn = gr.Button(LABEL_PREP_VERIFY" in source
    assert "elem_id='prep-verify-button'" in source
    assert "elem_id='prep-verify-report-box'" in source
    # Ordering inside the UI block: after the preparation controls it describes.
    assert source.index("prep_folder = gr.Textbox(") < source.index("prep_verify_btn = gr.Button(")


def test_the_ui_strings_never_promise_decode_verification():
    tree = _tree(_UI_CONTENT)
    strings = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    strings[target.id] = node.value.value
    for name in ("LABEL_PREP_VERIFY", "INFO_PREP_VERIFY", "PREP_VERIFY_INITIAL_TEXT",
                 "PREP_VERIFY_STALE_TEXT", "PREP_VERIFY_RENDER_BUSY_TEXT",
                 "VERIFY_BUSY_MESSAGE", "PREP_VERIFY_NO_FOLDER_TEXT",
                 "LABEL_PREP_VERIFY_REPORT"):
        assert name in strings, name
    info = strings["INFO_PREP_VERIFY"].lower()
    assert "metadata only" in info and "does not decode every frame" in info
    assert "read-only" in info
    for forbidden in ("repair", "fix", "rebuild", "delete"):
        assert f"will {forbidden}" not in info


# ---------------------------------------------------------------------------
# protected contracts
# ---------------------------------------------------------------------------

def test_the_protected_cache_constants_are_unchanged():
    expected = {
        ("video_analysis.py", "CACHE_CONTRACT_VERSION"): "stage5_cache_v3",
        ("video_analysis.py", "ANALYSIS_VERSION"): "auto_av_analysis_v8_llama_vulkan_batched",
        ("video_analysis.py", "_CACHE_IDENTITY_WORKER_CAP"): 16,
        ("qwen_partials.py", "PARTIAL_CONTRACT_VERSION"): "stage5_job_partial_v1",
        ("library_prep.py", "DEFAULT_ANALYZE_BATCH_SIZE"): 100,
    }
    roots = {"video_analysis.py": os.path.join(_REPO_ROOT, "src"),
             "qwen_partials.py": os.path.join(_REPO_ROOT, "src", "beatsync_fork"),
             "library_prep.py": os.path.join(_REPO_ROOT, "src", "beatsync_fork")}
    found = {}
    for (filename, const), want in expected.items():
        tree = _tree(os.path.join(roots[filename], filename))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == const:
                        found[(filename, const)] = node.value.value
    assert found == expected


def test_media_verify_imports_no_runtime_module():
    """The hard rule, asserted locally as well as by the package-wide guard."""
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "media_verify.py")
    tree = _tree(path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("logger", "paths", "gradio", "cupy", "cv2", "librosa", "numpy",
                      "video_analysis", "ffmpeg_processing", "gui", "ui_content"):
        assert forbidden not in imported, forbidden


def test_media_verify_starts_no_subprocess_itself():
    """The probe child belongs to the runtime seam; the policy module stays pure.

    Structural, over referenced names — the module's own docstring describes where the child
    process lives, and a text search would match that explanation rather than any use of it.
    """
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "media_verify.py")
    referenced = _referenced(_tree(path))
    for forbidden in ("subprocess", "Popen", "system", "remove", "unlink", "rmtree",
                      "shutil", "open", "write_text", "write_bytes", "mkdir"):
        assert forbidden not in referenced, forbidden
    # The only OS call it makes is the stat behind the stability fingerprint.
    assert "os.stat" in referenced


# ===========================================================================
# MEDIA-VERIFY-S2-A R3 regressions
# ===========================================================================
#
# Two independently reproduced MAJOR defects, each with the behaviour that was wrong asserted
# directly rather than its wiring:
#
# MAJOR-1  A source overwritten between its cache classification and its probe produced PREPARED
#          beside METADATA_OK -- two confident verdicts about two different versions of one file,
#          under one "Verified at" header and a COMPLETE banner.
# MAJOR-2  The Verify event shared the render concurrency lane, so Gradio QUEUED it behind a render
#          instead of refusing it, then ran it automatically against the click-time folder and
#          overwrote the newer "folder changed" notice.
#
# The Gradio-side event mechanisms (`concurrency_limit=None` reaching the handler during a render,
# `gr.Request` injection into both the click and the `inputs=None` change listener, per-session
# `session_hash`) were measured against the pinned gradio==6.19.0 in an isolated fixture outside
# this suite -- this suite stays importable on a bare interpreter, as CLAUDE.md requires. What is
# asserted below is the behaviour the shipped handlers build on top of those mechanisms, executed
# as real extracted source.

from beatsync_fork.media_verify import (  # noqa: E402
    CHANGE_DETECTION_LIMIT_TEXT,
    NO_BASELINE,
    ChangeWindow,
    MediaVerdict,
    VerificationRun,
    fingerprint_sources,
)


def _verify_one_body():
    """The real `_verify_one`, reached through the module (it is private but shipped)."""
    from beatsync_fork.media_verify import _verify_one
    return _verify_one


def _rewrite(path, content=b"\xff" * 4096):
    """Replace a source's bytes AND guarantee a different (size, mtime_ns)."""
    with open(path, "wb") as handle:
        handle.write(content)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))


# ---------------------------------------------------------------------------
# MAJOR-1: the cross-phase window
# ---------------------------------------------------------------------------

def test_a_source_changed_after_classification_is_not_a_clean_current_verdict(tmp_path):
    """THE regression. PREPARED + METADATA_OK for two different files must be impossible."""
    source = _video(tmp_path)
    baseline = fingerprint_source(source)          # what the cache phase saw
    _rewrite(source)                               # the operator overwrites it
    run = verify_media_health([source], _ok_probe(), baselines=[baseline])

    verdict = run.verdicts[0]
    assert verdict.status is MediaStatus.CHANGED_DURING_CHECK, verdict.status
    assert verdict.status is not MediaStatus.METADATA_OK
    assert verdict.change_window is ChangeWindow.CROSS_PHASE
    assert verdict.changed and verdict.changed_before_probe
    assert verdict.is_problem


def test_the_two_change_windows_are_distinguished_not_merged(tmp_path):
    """A report that cannot tell the two apart cannot say which verdict it outdated."""
    before_only = _video(tmp_path, "a.mp4")
    baseline_a = fingerprint_source(before_only)
    _rewrite(before_only)

    during_only = _video(tmp_path, "b.mp4")
    baseline_b = fingerprint_source(during_only)

    def probe(path, timeout=None):
        if path == during_only:
            _rewrite(path)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([before_only, during_only], probe,
                              baselines=[baseline_a, baseline_b], workers=1)
    assert run.verdicts[0].change_window is ChangeWindow.CROSS_PHASE
    assert run.verdicts[1].change_window is ChangeWindow.DURING_PROBE
    assert run.verdicts[1].changed and not run.verdicts[1].changed_before_probe


def test_a_change_in_both_windows_is_reported_as_both(tmp_path):
    source = _video(tmp_path)
    baseline = fingerprint_source(source)
    _rewrite(source)

    def probe(path, timeout=None):
        _rewrite(path, b"\x01" * 8192)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([source], probe, baselines=[baseline])
    verdict = run.verdicts[0]
    assert verdict.change_window is ChangeWindow.BOTH
    assert verdict.changed_before_probe, "BOTH must still outdate the cache verdict"
    assert "after its Stage-5 cache classification" in verdict.detail
    assert "while it was being probed" in verdict.detail


def test_an_unchanged_source_with_a_baseline_is_still_metadata_ok(tmp_path):
    """No false positives: the correction must not flag a stable library."""
    sources = [_video(tmp_path, f"s{i}.mp4") for i in range(5)]
    baselines = fingerprint_sources(sources)
    run = verify_media_health(sources, _ok_probe(), baselines=baselines)
    assert all(v.status is MediaStatus.METADATA_OK for v in run.verdicts)
    assert all(v.change_window is ChangeWindow.NONE for v in run.verdicts)
    assert run.complete
    assert run.changed_before_probe == [] and run.changed_during_probe == []


def test_a_source_that_disappeared_after_classification_cannot_be_metadata_ok(tmp_path):
    source = _video(tmp_path)
    baseline = fingerprint_source(source)
    os.remove(source)
    probed = []

    def probe(path, timeout=None):
        probed.append(path)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health([source], probe, baselines=[baseline])
    verdict = run.verdicts[0]
    assert verdict.status is MediaStatus.UNREADABLE
    assert verdict.status is not MediaStatus.METADATA_OK
    assert verdict.change_window is ChangeWindow.CROSS_PHASE, \
        "it was readable when classified, so that verdict is inconclusive too"
    assert verdict.changed_before_probe
    assert probed == [], "a vanished source is never probed"


def test_a_source_replaced_by_a_different_file_is_inconclusive(tmp_path):
    """Replaced, not edited: a new file at the same path with different size."""
    source = _video(tmp_path, "clip.mp4", content=b"\x00" * 64)
    baseline = fingerprint_source(source)
    os.remove(source)
    with open(source, "wb") as handle:
        handle.write(b"\x02" * 999)
    run = verify_media_health([source], _ok_probe(), baselines=[baseline])
    assert run.verdicts[0].status is MediaStatus.CHANGED_DURING_CHECK
    assert run.verdicts[0].change_window is ChangeWindow.CROSS_PHASE


def test_an_unavailable_initial_fingerprint_does_not_fabricate_a_change(tmp_path):
    """`None` baseline means "we looked and it was not there" -- not "we never looked"."""
    missing = str(tmp_path / "never-existed.mp4")
    run = verify_media_health([missing], _ok_probe(), baselines=[None])
    assert run.verdicts[0].status is MediaStatus.UNREADABLE
    assert run.verdicts[0].change_window is ChangeWindow.NONE, \
        "absent then, absent now: nothing changed"


def test_a_source_that_appeared_after_classification_is_inconclusive(tmp_path):
    """The mirror: unreadable when classified, readable now. The classification cannot stand."""
    source = str(tmp_path / "late.mp4")
    baselines = fingerprint_sources([source])
    assert baselines == [None]
    with open(source, "wb") as handle:
        handle.write(b"\x00" * 64)
    run = verify_media_health([source], _ok_probe(), baselines=baselines)
    assert run.verdicts[0].status is MediaStatus.CHANGED_DURING_CHECK
    assert run.verdicts[0].change_window is ChangeWindow.CROSS_PHASE


def test_duplicate_paths_keep_independent_positional_baselines(tmp_path):
    """Position is the authority, exactly as the Stage-5 classifier treats it."""
    source = _video(tmp_path)
    good = fingerprint_source(source)
    stale = SourceFingerprint(size=good.size + 1, mtime_ns=good.mtime_ns)
    run = verify_media_health([source, source], _ok_probe(),
                              baselines=[good, stale], workers=1)
    assert len(run.verdicts) == 2
    assert run.verdicts[0].status is MediaStatus.METADATA_OK
    assert run.verdicts[0].change_window is ChangeWindow.NONE
    assert run.verdicts[1].status is MediaStatus.CHANGED_DURING_CHECK
    assert run.verdicts[1].change_window is ChangeWindow.CROSS_PHASE


def test_fingerprint_sources_is_positional_and_stats_once_per_source(tmp_path):
    sources = [_video(tmp_path, "a.mp4"), _video(tmp_path, "a.mp4"),
               str(tmp_path / "gone.mp4")]
    calls = []

    def counting_stat(path):
        calls.append(path)
        return os.stat(path)

    observed = fingerprint_sources(sources, stat=counting_stat)
    assert len(observed) == len(sources), "one entry per POSITION, never deduplicated"
    assert len(calls) == 3, "exactly one stat per source and nothing else"
    assert observed[0] == observed[1] and observed[2] is None


def test_a_mismatched_baseline_length_degrades_instead_of_raising(tmp_path):
    """A bad caller must not be able to fail a read-only verification click."""
    sources = [_video(tmp_path, f"s{i}.mp4") for i in range(3)]
    short = verify_media_health(sources, _ok_probe(),
                                baselines=[fingerprint_source(sources[0])])
    assert [v.status for v in short.verdicts] == [MediaStatus.METADATA_OK] * 3
    overlong = verify_media_health(sources, _ok_probe(),
                                   baselines=fingerprint_sources(sources) + [None, None])
    assert [v.status for v in overlong.verdicts] == [MediaStatus.METADATA_OK] * 3


def test_an_empty_library_accepts_baselines_without_starting_a_pool():
    run = verify_media_health([], _ok_probe(), baselines=[])
    assert run.verdicts == [] and run.scheduled == 0
    assert run.changed_before_probe == []


def test_omitting_baselines_reproduces_the_original_probe_bracket_exactly(tmp_path):
    """S2-A behaviour is the default: no baseline, no cross-phase comparison."""
    source = _video(tmp_path)
    _rewrite(source)
    run = verify_media_health([source], _ok_probe())
    assert run.verdicts[0].status is MediaStatus.METADATA_OK
    assert run.verdicts[0].change_window is ChangeWindow.NONE
    # And the private worker's default is the sentinel, not None.
    import inspect
    assert inspect.signature(_verify_one_body()).parameters["baseline"].default is NO_BASELINE


@pytest.mark.parametrize("window", [ChangeWindow.CROSS_PHASE, ChangeWindow.DURING_PROBE,
                                    ChangeWindow.BOTH])
def test_an_observed_change_can_never_carry_a_clean_status(window, tmp_path):
    """The invariant behind every claim above: changed implies not-OK, for every window."""
    verdict = MediaVerdict(path="J:/x.mp4", status=MediaStatus.CHANGED_DURING_CHECK,
                           change_window=window)
    assert verdict.changed and verdict.is_problem
    # And no code path in the real worker produces METADATA_OK with a window set.
    source = _video(tmp_path)
    baseline = fingerprint_source(source)
    if window in (ChangeWindow.CROSS_PHASE, ChangeWindow.BOTH):
        _rewrite(source)

    def probe(path, timeout=None):
        if window in (ChangeWindow.DURING_PROBE, ChangeWindow.BOTH):
            _rewrite(path, b"\x03" * 2048)
        return ProbeOutcome(returncode=0, stdout=_payload())

    produced = verify_media_health([source], probe, baselines=[baseline]).verdicts[0]
    assert produced.change_window is window
    assert produced.status is not MediaStatus.METADATA_OK


def test_a_worker_exception_still_carries_the_cross_phase_fact(tmp_path):
    """A change is a fact about the file; our tooling failing does not erase it."""
    source = _video(tmp_path)
    baseline = fingerprint_source(source)
    _rewrite(source)

    def boom(path, timeout=None):
        raise RuntimeError("probe seam exploded")

    verdict = verify_media_health([source], boom, baselines=[baseline]).verdicts[0]
    assert verdict.status is MediaStatus.NOT_CHECKED
    assert verdict.change_window is ChangeWindow.CROSS_PHASE


def test_the_cross_phase_check_adds_no_file_open(tmp_path):
    """One extra `os.stat` per source. Nothing opened, nothing written."""
    import builtins
    source = _video(tmp_path)
    baselines = fingerprint_sources([source])
    opened = []
    real_open = builtins.open

    def watching_open(*args, **kwargs):
        opened.append(args[0] if args else None)
        return real_open(*args, **kwargs)

    builtins.open = watching_open
    try:
        verify_media_health([source], _ok_probe(), baselines=baselines)
    finally:
        builtins.open = real_open
    assert opened == [], "the cross-phase comparison must not open any file"


# ---------------------------------------------------------------------------
# MAJOR-1: the report must not present an outdated cache verdict as current
# ---------------------------------------------------------------------------

def _changed_run(window=ChangeWindow.CROSS_PHASE, path="J:/ok0.mp4"):
    return VerificationRun(verdicts=[
        MediaVerdict(path=path, status=MediaStatus.CHANGED_DURING_CHECK,
                     change_window=window, detail="changed"),
    ], scheduled=1)


def test_the_report_withdraws_the_clean_cache_claim_when_a_source_changed():
    cache, items = _cache(prepared=1)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=items, run=_changed_run())
    assert "Every source has a reusable Stage-5 cache record." not in report, \
        "that sentence is a claim about the CURRENT files"
    assert "when it was classified" in report
    assert "INCONCLUSIVE" in report
    assert "J:/ok0.mp4" in report


def test_the_report_preserves_the_classifier_counts_verbatim():
    """Qualify the counts, never silently recompute them: that would publish a classification
    nobody ran."""
    cache, items = _cache(prepared=1)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=items, run=_changed_run())
    assert "Reusable cache (PREPARED):     1" in report, "the real count stays"
    assert "Reusable cache (PREPARED):     0" not in report
    assert "classifier's own output" in report
    assert "Nothing was reclassified" in report


def test_the_report_names_the_outdated_classification_for_each_changed_source():
    cache, items = _cache(prepared=1)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=items, run=_changed_run())
    assert "Changed after classification - cache verdict INCONCLUSIVE (1)" in report
    assert "Classified as PREPARED, then the file changed" in report


def test_a_during_probe_only_change_does_not_claim_the_classification_was_outdated():
    cache, items = _cache(prepared=1)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=items,
        run=_changed_run(window=ChangeWindow.DURING_PROBE))
    assert "Changed after classification" not in report
    # ...but it is still reported as inconclusive on the media side.
    assert "INCONCLUSIVE:" in report
    assert "1 changed while being probed" in report


def test_the_report_never_says_no_metadata_problems_when_a_source_changed():
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=_changed_run())
    assert "No metadata problems found" not in report
    assert "MEDIA_CHANGED_DURING_CHECK" in report


def test_a_complete_sweep_with_a_changed_source_still_says_inconclusive():
    """COMPLETE is about probe coverage. It must not read as "everything is fine"."""
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=_changed_run())
    assert "STATUS: COMPLETE" in report, "coverage really was complete"
    assert "INCONCLUSIVE: 1 source(s) changed between the cache phase and their probe" in report
    assert "NO current media verdict" in report


def test_the_report_states_the_bound_of_size_and_mtime_comparison():
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=None, run=_changed_run())
    assert CHANGE_DETECTION_LIMIT_TEXT in report
    assert "not whole-file byte-integrity proof" in report
    for overclaim in ("no corruption", "files are intact", "fully verified", "all frames"):
        assert overclaim not in report.lower()


def test_a_misaligned_classification_list_is_not_attributed_to_the_wrong_source():
    """Position is the authority, and the recorded path is the guard on that assumption."""
    cache, _items = _cache(prepared=1)
    wrong = [{"path": "J:/a-completely-different-file.mp4",
              "status": PrepStatus.PREPARED.value, "reason": ""}]
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=wrong, run=_changed_run())
    assert "could not be matched by position" in report
    assert "Classified as PREPARED" not in report


def test_an_unverifiable_backend_still_reports_the_change_and_invents_no_cache_verdict():
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache={"source_count": 1, "prepared_count": 0, "needs_analysis_count": 0,
               "unavailable_count": 0, "classifications": []},
        run=_changed_run(),
        cache_unverifiable_reason="The Qwen backend identity could not be verified.")
    assert "UNVERIFIABLE" in report
    assert "Reusable cache (PREPARED)" not in report, "no verdict exists to qualify"
    assert "INCONCLUSIVE:" in report, "the media-side change is still reported"


def test_a_clean_run_report_is_unchanged_apart_from_the_new_limit_sentence():
    """No regression in the common case: a stable library still reads as it did."""
    cache, items = _cache(prepared=2)
    run = _run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))])
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=2,
        cache=cache, cache_classifications=items, run=run)
    assert "Every source has a reusable Stage-5 cache record." in report
    assert "No metadata problems found" in report
    assert "INCONCLUSIVE" not in report
    assert "Changed after classification" not in report


# ---------------------------------------------------------------------------
# R2: the report must render the producer's real vocabulary, and a positive
# cache claim must rest on affirmative evidence
#
# The defect this section exists to prevent: `classify_library_sources` emits
# PrepStatus/NeedReason *values* (lowercase), the report compared against their
# member *names*, so five filters were unreachable -- the two reason counts
# always rendered 0, the three detail blocks were always empty, and the report
# asserted that every source had a reusable record while naming none of the
# sources that did not. Four MAJOR findings, all invisible to a suite whose
# fixture used the same unreachable spelling.
# ---------------------------------------------------------------------------

_ALL_PREPARED = "Every source has a reusable Stage-5 cache record."
_WAS_PREPARED = "Every source had a reusable Stage-5 cache record when it was classified"


def test_a_library_with_no_reusable_records_is_never_called_prepared():
    """R2 MAJOR-1/2/3, and the exact shape the D2 production smoke hit."""
    cache, items = _cache(new_or_changed=3)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=3,
        cache=cache, cache_classifications=items, run=_run_with(
            [(item["path"], ProbeOutcome(returncode=0, stdout=_payload())) for item in items]))
    assert _ALL_PREPARED not in report, "0 of 3 prepared may never read as a warm library"
    # The detail count is the classifier's truth, not a zero from a dead filter.
    assert "Reusable cache (PREPARED):     0" in report
    assert "New or changed:                3" in report
    # ...and every affected source is named, because the path IS the actionable part.
    for item in items:
        assert item["path"] in report


def test_each_classification_kind_reaches_its_own_bounded_detail_block():
    """R2 MAJOR-2/3: one source of each kind, each in the right block, each named."""
    cache, items = _cache(prepared=2, new_or_changed=1, incomplete=1, unavailable=1)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=5,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert "New or changed:                1" in report
    assert "Incomplete / invalid record:   1" in report
    assert "Needs analysis - new or changed (1):" in report
    assert "Needs analysis - incomplete record (1):" in report
    assert "Identity unavailable (1):" in report
    assert "J:/new0.mp4" in report
    assert "J:/inc0.mp4" in report
    assert "J:/gone0.mp4" in report
    assert _ALL_PREPARED not in report
    # The prepared sources are counted, never listed as problems.
    assert "J:/ok0.mp4" not in report


def test_the_detail_blocks_stay_bounded_and_state_what_they_omitted():
    """The R2 filters must feed the EXISTING bound, not bypass it."""
    over = MAX_REPORT_DETAIL_LINES + 4
    cache, items = _cache(new_or_changed=over)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=over,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert f"Needs analysis - new or changed ({over}):" in report
    assert f"... and 4 more not shown (showing the first {MAX_REPORT_DETAIL_LINES} of {over})." \
        in report
    # The bound is on rows shown; the total is still stated truthfully.
    assert f"New or changed:                {over}" in report


def test_a_genuinely_prepared_library_keeps_its_positive_statement():
    """Anti-regression: the correction must not silence the legitimate case."""
    cache, items = _cache(prepared=3)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=3,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert _ALL_PREPARED in report


def test_a_prepared_library_with_a_cross_phase_change_keeps_the_qualified_wording():
    """Genuinely all-prepared + a later change: the historical claim is still true."""
    cache, items = _cache(prepared=2)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=2,
        cache=cache, cache_classifications=items, run=VerificationRun(verdicts=[
            MediaVerdict(path="J:/ok0.mp4", status=MediaStatus.CHANGED_DURING_CHECK,
                         change_window=ChangeWindow.CROSS_PHASE, detail="changed"),
            MediaVerdict(path="J:/ok1.mp4", status=MediaStatus.METADATA_OK),
        ], scheduled=2))
    assert _WAS_PREPARED in report
    assert _ALL_PREPARED not in report, "that sentence is a claim about the CURRENT files"
    assert "Changed after classification" in report


def test_a_mixed_library_with_a_cross_phase_change_claims_neither_wording():
    """R2 MAJOR-4: a later change must not upgrade a partial classification.

    The qualified sentence is still a universal claim -- "every source *had* a record" -- so it
    needs the same affirmative evidence as the unqualified one. Only 1 of 3 was prepared here.
    """
    cache, items = _cache(prepared=1, new_or_changed=2)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=3,
        cache=cache, cache_classifications=items, run=VerificationRun(verdicts=[
            MediaVerdict(path=items[0]["path"], status=MediaStatus.CHANGED_DURING_CHECK,
                         change_window=ChangeWindow.CROSS_PHASE, detail="changed"),
            MediaVerdict(path=items[1]["path"], status=MediaStatus.METADATA_OK),
            MediaVerdict(path=items[2]["path"], status=MediaStatus.METADATA_OK),
        ], scheduled=3))
    assert _WAS_PREPARED not in report
    assert _ALL_PREPARED not in report
    # The cross-phase disclosure itself is preserved, and the outstanding work is still named.
    assert "Changed after classification" in report
    assert "Nothing was reclassified" in report
    assert "Needs analysis - new or changed (2):" in report


def test_an_empty_supported_library_claims_nothing_positive():
    """R2 MAJOR-4: zero sources is not a warm library. Survives the vocabulary fix alone."""
    cache, items = _cache()
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=0,
        cache=cache, cache_classifications=items, run=_run_with([]))
    assert _ALL_PREPARED not in report
    assert _WAS_PREPARED not in report


@pytest.mark.parametrize("field,value,why", [
    ("prepared_count", 2, "prepared_count disagrees with the per-item classifications"),
    ("needs_analysis_count", 1, "needs analysis cannot be outstanding in a prepared library"),
    ("unavailable_count", 1, "an unreadable source is neither prepared nor analysable"),
    ("source_count", 2, "the classifier counted a different number of sources"),
])
def test_inconsistent_classifier_totals_never_earn_a_positive_claim(field, value, why):
    """R2 MAJOR-4. The totals are still printed verbatim -- MINOR-3 is deferred, not fixed."""
    cache, items = _cache(prepared=3)
    cache = dict(cache)
    cache[field] = value
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=3,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert _ALL_PREPARED not in report, why
    # Deferred MINOR-3: the contradictory number is reported as the classifier gave it.
    assert f"{cache['prepared_count']}" in report


def test_a_folder_count_that_exceeds_the_classified_set_claims_nothing():
    """A partial classification describes a subset; a library-wide claim may not be read off it."""
    cache, items = _cache(prepared=2)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=5,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert _ALL_PREPARED not in report


def test_an_unverifiable_backend_still_fabricates_no_cache_verdict():
    """R2: the UNVERIFIABLE branch must stay ahead of the affirmative predicate."""
    cache, items = _cache(prepared=3)
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=3,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]),
        cache_unverifiable_reason="The Qwen backend identity could not be verified.")
    assert "UNVERIFIABLE" in report
    assert _ALL_PREPARED not in report, "an unprovable identity cannot prove reuse either"
    assert "Reusable cache (PREPARED)" not in report
    # The two questions stay independent.
    assert "MEDIA HEALTH" in report


def test_a_recognised_status_renders_readably_and_an_unknown_one_stays_unknown():
    """R2 MINOR-2, both directions."""
    cache, _items = _cache(prepared=1)
    known = [{"path": "J:/ok0.mp4", "status": PrepStatus.PREPARED.value, "reason": ""}]
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=known, run=VerificationRun(verdicts=[
            MediaVerdict(path="J:/ok0.mp4", status=MediaStatus.CHANGED_DURING_CHECK,
                         change_window=ChangeWindow.CROSS_PHASE, detail="changed"),
        ], scheduled=1))
    assert "Classified as PREPARED, then the file changed" in report
    assert "Classified as prepared," not in report, "the raw value is not the display form"

    # An unrecognised verdict must stay visibly unknown, never promoted to a cache state.
    odd = [{"path": "J:/ok0.mp4", "status": "something_new", "reason": ""}]
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=odd, run=VerificationRun(verdicts=[
            MediaVerdict(path="J:/ok0.mp4", status=MediaStatus.CHANGED_DURING_CHECK,
                         change_window=ChangeWindow.CROSS_PHASE, detail="changed"),
        ], scheduled=1))
    assert "Classified as something_new, then the file changed" in report
    assert "PREPARED" not in report.split("Changed after classification")[1]
    assert _ALL_PREPARED not in report


def test_the_cache_fixture_is_built_from_the_authoritative_vocabulary():
    """R2 MINOR-1, asserted by enum identity and AST -- never by scanning source text.

    Two independent checks, because either alone can be satisfied by accident:

    1. **Enum identity.** Every status and reason the helper emits must round-trip through the
       authoritative enums. `PrepStatus(value)` raising is the whole assertion.
    2. **AST.** No string literal in the helper's body may equal an enum member *name*. That is
       the spelling the defect was written in, and a fixture that reintroduces it is back to
       testing an input shape production cannot produce.

    The AST pass deliberately skips the docstring: this helper's own documentation discusses the
    historical spelling by name, and a check that read prose would fail on its own explanation.
    This repository has shipped that exact mistake four times -- assert over structure, not text.
    """
    statuses = {item.value for item in PrepStatus}
    reasons = {item.value for item in NeedReason}

    _cache_dict, items = _cache(prepared=1, new_or_changed=1, incomplete=1, unavailable=1)
    assert items, "the helper must actually produce classifications"
    for item in items:
        assert PrepStatus(item["status"]).value in statuses
        assert item["status"] == PrepStatus(item["status"]).value, "value, not member name"
        if item["reason"]:
            assert NeedReason(item["reason"]).value in reasons
            assert item["reason"] == NeedReason(item["reason"]).value

    member_names = {item.name for item in PrepStatus} | {item.name for item in NeedReason}
    node = _func(_tree(os.path.abspath(__file__)), "_cache")
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                      # the docstring is prose, not a fixture value
    literals = {sub.value for statement in body for sub in ast.walk(statement)
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str)}
    assert not (literals & member_names), (
        f"fixture reintroduced enum member names as literals: "
        f"{sorted(literals & member_names)}")


# ---------------------------------------------------------------------------
# R2 negative controls: each mutation must be caught, for its intended reason
#
# The point is not that "some test fails" -- a mutation that merely breaks an
# unrelated assertion proves nothing about the regression we added. Each case
# below names the regression that must catch it, and asserts that that one
# fails. The shipped module is never modified: a copy of its source is mutated
# in memory and executed, which is how the rest of this suite reaches source it
# must not alter.
# ---------------------------------------------------------------------------

_MEDIA_VERIFY_SRC = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "media_verify.py")


def _formatter_with(old, new):
    """`format_verification_report` from an in-memory mutated copy of the real module.

    The replacement must actually apply: a mutation that silently matched nothing would make the
    negative control pass for the wrong reason, which is the failure mode these tests exist to
    exclude in the first place.
    """
    with open(_MEDIA_VERIFY_SRC, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count(old) >= 1, f"mutation target not present: {old!r}"
    mutated = source.replace(old, new)
    assert mutated != source, "mutation did not change the source"
    # A real module object, registered while it executes: the module uses
    # `from __future__ import annotations`, so `@dataclass` resolves its field annotations by
    # looking the defining module up in `sys.modules`. A bare dict namespace is not reachable
    # that way and the dataclasses machinery fails on the first deferred annotation.
    name = "beatsync_fork._r2_mutated_media_verify"
    module = types.ModuleType(name)
    module.__file__ = _MEDIA_VERIFY_SRC
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(mutated, _MEDIA_VERIFY_SRC, "exec"), module.__dict__)
        return module.__dict__["format_verification_report"]
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def _d2_case():
    cache, items = _cache(new_or_changed=3)
    run = _run_with([(item["path"], ProbeOutcome(returncode=0, stdout=_payload()))
                     for item in items])
    return cache, items, run


def test_negative_control_an_uppercase_reason_comparison_is_caught():
    """Mutation: compare the reason against the enum member name again.

    Caught by `test_a_library_with_no_reusable_records_is_never_called_prepared`, on its count
    clause (`New or changed: 3`) and its per-path clauses — the detail count collapses to 0 and
    every outstanding source stops being named.

    Note what this mutation does *not* restore: the false positive claim. The affirmative
    predicate is an independent defence and still refuses it on `prepared_count == 0`. That is
    the intended design — the two R2 corrections overlap deliberately, and recovering the full
    original symptom needs both mutations, which
    `test_negative_control_the_absence_of_error_lists_gate_is_caught` covers.
    """
    formatter = _formatter_with("== NeedReason.NEW_OR_CHANGED.value",
                                '== "NEW_OR_CHANGED"')
    cache, items, run = _d2_case()
    report = formatter(folder="J:/l", recursive=True, verified_at="t", source_count=3,
                       cache=cache, cache_classifications=items, run=run)
    assert "New or changed:                3" not in report, "the count regression is the catch"
    assert "New or changed:                0" in report
    assert all(item["path"] not in report for item in items), "paths stop being named"
    # The second defence holds, so this mutation alone is not enough to fabricate reuse.
    assert _ALL_PREPARED not in report


def test_negative_control_an_uppercase_unavailable_status_comparison_is_caught():
    """Mutation: compare the unavailable status against the enum member name again.

    Caught by `test_each_classification_kind_reaches_its_own_bounded_detail_block`, whose
    `Identity unavailable (1):` block disappears.
    """
    formatter = _formatter_with("== PrepStatus.SOURCE_IDENTITY_UNAVAILABLE.value",
                                '== "SOURCE_IDENTITY_UNAVAILABLE"')
    cache, items = _cache(prepared=2, new_or_changed=1, incomplete=1, unavailable=1)
    report = formatter(folder="J:/l", recursive=True, verified_at="t", source_count=5,
                       cache=cache, cache_classifications=items,
                       run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0,
                                                                stdout=_payload()))]))
    assert "Identity unavailable (1):" not in report
    assert "J:/gone0.mp4" not in report
    # The dict-derived count survives, which is exactly why the defect read as cosmetic.
    assert "Source identity unavailable:   1" in report


def test_negative_control_the_absence_of_error_lists_gate_is_caught():
    """Mutation: restore the original negative gate.

    Caught by `test_an_empty_supported_library_claims_nothing_positive` and by
    `test_inconsistent_classifier_totals_never_earn_a_positive_claim` -- neither of which the
    vocabulary fix alone can satisfy.
    """
    formatter = _formatter_with(
        "if _all_sources_prepared(cache, cache_classifications, source_count):",
        "if not (changed_rows or incomplete_rows or unavailable_rows):")

    # Empty library: three empty lists are not evidence of reuse.
    cache, items = _cache()
    report = formatter(folder="J:/l", recursive=True, verified_at="t", source_count=0,
                       cache=cache, cache_classifications=items, run=_run_with([]))
    assert _ALL_PREPARED in report, "the mutation must reproduce the empty-library false claim"

    # Contradictory totals: prepared_count and needs_analysis_count cannot both hold.
    cache, items = _cache(prepared=3)
    cache = dict(cache)
    cache["needs_analysis_count"] = 3
    report = formatter(folder="J:/l", recursive=True, verified_at="t", source_count=3,
                       cache=cache, cache_classifications=items,
                       run=_run_with([("J:/a.mp4", ProbeOutcome(returncode=0,
                                                                stdout=_payload()))]))
    assert _ALL_PREPARED in report


def test_negative_control_a_fixture_that_loses_vocabulary_fidelity_is_caught():
    """Mutation: build the fixture from enum member names again.

    Caught by `test_the_cache_fixture_is_built_from_the_authoritative_vocabulary`'s enum-identity
    clause -- `PrepStatus("NEEDS_ANALYSIS")` raises, because that is a member name and the
    producer emits values. This is asserted on a local stand-in so the shipped fixture stays
    intact.
    """
    def _unfaithful_cache():
        return [{"path": "J:/new0.mp4", "status": "NEEDS_ANALYSIS",
                 "reason": "NEW_OR_CHANGED"}]

    items = _unfaithful_cache()
    with pytest.raises(ValueError):
        PrepStatus(items[0]["status"])
    with pytest.raises(ValueError):
        NeedReason(items[0]["reason"])

    # And such a fixture is invisible to the corrected report: no count, no path, false claim.
    cache = {"source_count": 1, "prepared_count": 0, "needs_analysis_count": 1,
             "unavailable_count": 0, "classifications": items}
    report = format_verification_report(
        folder="J:/l", recursive=True, verified_at="t", source_count=1,
        cache=cache, cache_classifications=items,
        run=_run_with([("J:/new0.mp4", ProbeOutcome(returncode=0, stdout=_payload()))]))
    assert "New or changed:                1" not in report
    assert "J:/new0.mp4" not in report
    # ...but the affirmative predicate still refuses the positive claim, so R2's two corrections
    # are genuinely independent defences rather than one restated twice.
    assert _ALL_PREPARED not in report


# ---------------------------------------------------------------------------
# MAJOR-1: the baseline is captured BEFORE the cache phase, in the shipped impl
# ---------------------------------------------------------------------------

def test_the_impl_captures_baselines_before_it_calls_the_classifier(gui_tree):
    """Ordering IS the fix. A baseline taken after classification measures nothing."""
    impl = _func(gui_tree, "_verify_sources_impl")
    order = []
    for node in ast.walk(impl):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.endswith("fingerprint_sources"):
            order.append(("baseline", node.lineno))
        elif name.endswith("classify_library_sources"):
            order.append(("classify", node.lineno))
        elif name.endswith("verify_media_health"):
            order.append(("media", node.lineno))
    stages = [n for n, _ in sorted(order, key=lambda pair: pair[1])]
    assert stages == ["baseline", "classify", "media"], stages


def test_the_impl_passes_the_baselines_into_the_media_phase(gui_tree):
    impl = _func(gui_tree, "_verify_sources_impl")
    call = next(n for n in ast.walk(impl)
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(
                    "verify_media_health"))
    keywords = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert keywords.get("baselines") == "baselines", keywords


def test_the_cross_phase_fix_adds_no_cache_write_and_no_qwen(gui_tree):
    impl = ast.unparse(_func(gui_tree, "_verify_sources_impl"))
    assert "fingerprint_sources" in impl
    for forbidden in ("_save_cache", "_checkpoint_cache", "_load_cache", "_cache_path",
                      "_video_signature", "analyze_video_sources", "_analyze_single_video",
                      "stage5_qwen_scene_worker", "subprocess", "Popen", "open("):
        assert forbidden not in impl, forbidden


# ---------------------------------------------------------------------------
# MAJOR-2: the Verify event is out of the render lane
# ---------------------------------------------------------------------------

def test_the_verify_event_has_its_own_unlimited_lane(gui_tree):
    """Sharing the render lane is what made Gradio queue instead of refuse."""
    source = ast.unparse(gui_tree)
    wiring = source.split("prep_verify_btn.click(")[1].split(")")[0]
    assert "concurrency_id" not in wiring, \
        "a shared lane queues the event and the mutex is never reached"
    assert "RENDER_CONCURRENCY_ID" not in wiring
    assert "concurrency_limit=None" in wiring, \
        "a lane of 1 would queue a SECOND verify click instead of refusing it"


def _click_keywords(tree, widget):
    """The keyword arguments of `<widget>.click(...)`, read from the AST rather than from text.

    The render registrations carry dozens of inputs, so a text window around the call name is not
    a reliable way to reach their keywords.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "click" or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != widget:
            continue
        return {k.arg: ast.unparse(k.value) for k in node.keywords}
    raise AssertionError(f"{widget}.click(...) not found")


def test_both_render_events_keep_the_shared_render_lane(gui_tree):
    for widget in ("process_btn", "render_selected_variants_btn"):
        keywords = _click_keywords(gui_tree, widget)
        assert keywords.get("concurrency_id") == "RENDER_CONCURRENCY_ID", (widget, keywords)
        assert keywords.get("concurrency_limit") == "1", (widget, keywords)


def test_the_verify_event_shares_no_lane_with_the_renders(gui_tree):
    """The same assertion from the other side, through the AST rather than a text window."""
    keywords = _click_keywords(gui_tree, "prep_verify_btn")
    assert "concurrency_id" not in keywords, keywords
    assert keywords.get("concurrency_limit") == "None", keywords
    assert keywords.get("inputs") == "[prep_folder, prep_recursive]", keywords
    assert keywords.get("outputs") == "[prep_verify_report]", keywords


def test_the_cancel_lane_is_untouched(gui_tree):
    source = ast.unparse(gui_tree)
    assert "CANCEL_CONCURRENCY_ID = 'beatsync-cancel'" in source
    assert "concurrency_id=CANCEL_CONCURRENCY_ID" in source
    assert "concurrency_id=CANCEL_CONCURRENCY_ID" not in \
        source.split("prep_verify_btn.click(")[1][:2000]


def test_a_refused_verification_does_no_work_and_does_not_run_later():
    """The queued-then-auto-run behaviour, asserted as absent: a refusal is terminal."""
    ns, state = _load_concurrency_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "precondition: a render holds the mutex"
    ns["_mark_lock_holder"](ns["_LOCK_HOLDER_RENDER"])
    out = ns["_on_verify_sources_click"]("J:/lib", True)
    assert out == "VERIFY_REFUSED_RENDER_ACTIVE"
    assert state["impl_calls"] == []
    # The render ends. Nothing re-runs by itself; the refused click is simply gone.
    ns["_RENDER_LOCK"].release()
    assert state["impl_calls"] == [], "a refused verification must not resume on its own"
    # And a fresh click now works, proving the refusal left nothing broken behind it.
    assert ns["_on_verify_sources_click"]("J:/lib", True) == "REPORT"
    assert state["impl_calls"] == [("J:/lib", True)]


def test_the_verify_refusal_names_a_render_or_another_verification():
    ns, state = _load_concurrency_seam()
    ns["_RENDER_LOCK"].acquire(blocking=False)
    try:
        ns["_mark_lock_holder"](ns["_LOCK_HOLDER_RENDER"])
        assert ns["_on_verify_sources_click"]("J:/l", True) == "VERIFY_REFUSED_RENDER_ACTIVE"
    finally:
        ns["_RENDER_LOCK"].release()

    seen = {}

    def impl(folder, recursive):
        seen["second"] = ns["_on_verify_sources_click"]("J:/l", True)
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/l", True) == "REPORT"
    assert seen["second"] == "VERIFY_REFUSED_ALREADY_RUNNING", \
        "a verification is not a render and must not be described as one"


def test_neither_side_terminates_the_other():
    """A refusal is a refusal: nothing is signalled, cancelled or killed."""
    click = _referenced(_func(_tree(_GUI), "_on_verify_sources_click"))
    for forbidden in ("terminate", "kill", "request_cancel", "mark_terminal",
                      "RenderCancelled", "RenderLifecycle"):
        assert forbidden not in click, forbidden


def _ui_strings():
    tree = _tree(_UI_CONTENT)
    strings = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    strings[target.id] = node.value.value
    return strings


def test_the_busy_messages_never_promise_queuing():
    """Both refusals are REFUSALS. Promising that the request will wait its turn describes the
    defect R3 removed, and it is the same class of inaccuracy as over-claiming a decode check."""
    strings = _ui_strings()
    for name in ("VERIFY_BUSY_MESSAGE", "PREP_VERIFY_RENDER_BUSY_TEXT",
                 "PREP_VERIFY_ALREADY_RUNNING_TEXT"):
        assert name in strings, name
        text = strings[name].lower()
        for forbidden in ("wait for it", "renders wait", "queued", "in the queue",
                          "will start automatically", "starts automatically", "your turn",
                          "will run when", "once it is your"):
            assert forbidden not in text, (name, forbidden)
    # And each one says what actually happened to the request.
    assert "was not started" in strings["VERIFY_BUSY_MESSAGE"].lower()
    assert "will not start" in strings["PREP_VERIFY_RENDER_BUSY_TEXT"].lower()
    already = strings["PREP_VERIFY_ALREADY_RUNNING_TEXT"].lower()
    assert "already running" in already
    assert "nothing was started or cancelled" in already


def test_the_two_verify_refusals_are_distinct_messages():
    strings = _ui_strings()
    assert strings["PREP_VERIFY_RENDER_BUSY_TEXT"] != \
        strings["PREP_VERIFY_ALREADY_RUNNING_TEXT"]
    assert "render is running" in strings["PREP_VERIFY_RENDER_BUSY_TEXT"].lower()
    assert "render" not in strings["PREP_VERIFY_ALREADY_RUNNING_TEXT"].lower(), \
        "the already-running refusal must not blame a render that is not running"


# ---------------------------------------------------------------------------
# MAJOR-2: the stale-report race, session-scoped
# ---------------------------------------------------------------------------

def test_an_invalidation_during_verification_withholds_the_stale_report():
    """THE regression: the operator changed folder, so the older answer must not be published."""
    ns, state = _load_concurrency_seam()
    request = _FakeRequest("session-A")

    def impl(folder, recursive):
        # The operator retypes the folder while this is running. Its listener runs in another
        # Gradio lane and completes first.
        assert ns["_on_verify_invalidate"](request) == "STALE_FOLDER_CHANGED"
        return "REPORT for folder=A"

    state["impl"] = impl
    out = ns["_on_verify_sources_click"]("J:/A", True, request)
    assert out == "STALE_FOLDER_CHANGED", \
        "a report for the previous folder must not overwrite the newer notice"
    assert state["impl_calls"] == [("J:/A", True)], "the work itself still ran and was bounded"


def test_a_recursive_flag_change_during_verification_also_withholds_the_report():
    ns, state = _load_concurrency_seam()
    request = _FakeRequest("session-A")

    def impl(folder, recursive):
        ns["_on_verify_invalidate"](request)
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/A", False, request) == "STALE_FOLDER_CHANGED"


def test_both_change_listeners_share_the_one_invalidating_handler(gui_tree):
    source = ast.unparse(gui_tree)
    for widget in ("prep_folder", "prep_recursive"):
        blocks = [seg for seg in source.split(f"{widget}.change(")[1:]]
        wiring = next(b.split(")")[0] for b in blocks
                      if "_on_verify_invalidate" in b.split(")")[0])
        assert "outputs=[prep_verify_report]" in wiring
        assert "inputs=None" in wiring, "the request is injected, not taken from a widget"


def test_repeated_folder_changes_during_verification_still_withhold_the_report():
    ns, state = _load_concurrency_seam()
    request = _FakeRequest("session-A")

    def impl(folder, recursive):
        for _ in range(4):
            ns["_on_verify_invalidate"](request)
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/A", True, request) == "STALE_FOLDER_CHANGED"


def test_a_failed_verification_is_also_withheld_after_an_invalidation():
    """The failure text names a folder too, so it is stale for exactly the same reason."""
    ns, state = _load_concurrency_seam()
    request = _FakeRequest("session-A")

    def impl(folder, recursive):
        ns["_on_verify_invalidate"](request)
        raise RuntimeError("scanner exploded")

    state["impl"] = impl
    out = ns["_on_verify_sources_click"]("J:/A", True, request)
    assert out == "STALE_FOLDER_CHANGED"
    assert "scanner exploded" not in out
    # The mutex is still released, and before the staleness decision.
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the lock leaked on the stale path"
    ns["_RENDER_LOCK"].release()


def test_an_invalidation_after_the_report_was_published_does_not_rewrite_it():
    """The check is a point-in-time comparison, not a subscription."""
    ns, _state = _load_concurrency_seam()
    request = _FakeRequest("session-A")
    assert ns["_on_verify_sources_click"]("J:/A", True, request) == "REPORT"
    assert ns["_on_verify_invalidate"](request) == "STALE_FOLDER_CHANGED"
    # A later verification is unaffected by the earlier bump.
    assert ns["_on_verify_sources_click"]("J:/A", True, request) == "REPORT"


def test_two_sessions_do_not_invalidate_each_others_reports():
    """A single process-global counter would fail this: it is why one was not used."""
    ns, state = _load_concurrency_seam()
    session_a = _FakeRequest("session-A")
    session_b = _FakeRequest("session-B")

    def impl(folder, recursive):
        # Another operator, in another browser, edits THEIR folder.
        ns["_on_verify_invalidate"](session_b)
        return f"REPORT for {folder}"

    state["impl"] = impl
    out = ns["_on_verify_sources_click"]("J:/A", True, session_a)
    assert out == "REPORT for J:/A", \
        "session B's folder edit must not invalidate session A's legitimate verification"


def test_a_sessions_own_edit_still_invalidates_when_another_session_also_edits():
    ns, state = _load_concurrency_seam()
    session_a = _FakeRequest("session-A")
    session_b = _FakeRequest("session-B")

    def impl(folder, recursive):
        ns["_on_verify_invalidate"](session_b)
        ns["_on_verify_invalidate"](session_a)
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/A", True, session_a) == "STALE_FOLDER_CHANGED"


def test_a_request_without_a_session_hash_falls_into_one_shared_bucket():
    """The fallback must be ONE bucket: a unique key per request could never match."""
    ns, state = _load_concurrency_seam()
    assert ns["_verify_session_key"](None) == ns["_verify_session_key"](_FakeRequest(None))

    def impl(folder, recursive):
        ns["_on_verify_invalidate"](None)
        return "REPORT"

    state["impl"] = impl
    assert ns["_on_verify_sources_click"]("J:/A", True, None) == "STALE_FOLDER_CHANGED"


def test_rapid_verify_clicks_do_not_overlap_and_the_winner_publishes():
    ns, state = _load_concurrency_seam()
    request = _FakeRequest("session-A")
    inside = threading.Event()
    hold = threading.Event()
    overlapping = []
    results = []

    def impl(folder, recursive):
        overlapping.append(1)
        inside.set()
        hold.wait(5)
        assert len(overlapping) == 1, "two verifications overlapped"
        overlapping.pop()
        return "REPORT"

    state["impl"] = impl
    winner = threading.Thread(target=lambda: results.append(
        ns["_on_verify_sources_click"]("J:/A", True, request)), daemon=True)
    winner.start()
    assert inside.wait(5)
    refused = [ns["_on_verify_sources_click"]("J:/A", True, request) for _ in range(5)]
    hold.set()
    winner.join(5)
    assert refused == ["VERIFY_REFUSED_ALREADY_RUNNING"] * 5
    assert results == ["REPORT"], "the one that won the mutex publishes a current report"
    assert state["impl_calls"] == [("J:/A", True)], "only one did any work"


def test_the_revision_map_is_bounded_and_evicts_least_recently_touched():
    ns, _state = _load_concurrency_seam()
    cap = ns["_VERIFY_REVISION_CAP"]
    for index in range(cap + 50):
        ns["_bump_verify_revision"](f"session-{index}")
    assert len(ns["_verify_revisions"]) == cap, len(ns["_verify_revisions"])
    assert "session-0" not in ns["_verify_revisions"], "the oldest must be evicted"
    assert f"session-{cap + 49}" in ns["_verify_revisions"]


def test_reading_a_revision_never_creates_an_entry():
    """A session that never edits its folder must never occupy a slot."""
    ns, _state = _load_concurrency_seam()
    assert ns["_verify_revision"]("never-seen") == 0
    assert ns["_verify_revisions"] == {}


def test_touching_a_revision_protects_it_from_eviction():
    """A REPEATEDLY READ entry survives churn, which is a property of the touch, not of a run.

    [FORK] Digital-Union (MEDIA-VERIFY-S2-A R5): the loop below used to be commented "an active
    session keeps reading its own", which described a lifecycle that does not exist -- a running
    verification reads its revision exactly twice, once at the start and once at the end, and
    reads nothing in between. That mistaken model is why eviction of a RUNNING verification's
    entry looked impossible. What actually protects a running verification is the pin, covered by
    `test_an_active_verification_survives_*` below; this test covers only the touch itself.
    """
    ns, _state = _load_concurrency_seam()
    cap = ns["_VERIFY_REVISION_CAP"]
    ns["_bump_verify_revision"]("long-lived")
    for index in range(cap + 20):
        ns["_bump_verify_revision"](f"filler-{index}")
        ns["_verify_revision"]("long-lived")
    assert "long-lived" in ns["_verify_revisions"]


def test_the_revision_map_holds_integers_only():
    ns, _state = _load_concurrency_seam()
    ns["_bump_verify_revision"]("s")
    ns["_on_verify_invalidate"](_FakeRequest("t"))
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for v in ns["_verify_revisions"].values())
    assert all(isinstance(k, str) for k in ns["_verify_revisions"])


def test_the_active_session_pin_is_a_plain_string():
    """No lock, thread, event, handle or lifecycle object may become the pin."""
    ns, _state = _load_concurrency_seam()
    assert ns["_verify_active_session"] == ""
    ns["_begin_verify_session"]("abc")
    assert isinstance(ns["_verify_active_session"], str)
    assert ns["_verify_active_session"] == "abc"
    ns["_end_verify_session"]("abc", 0)
    assert ns["_verify_active_session"] == ""


# ---------------------------------------------------------------------------
# [FORK] Digital-Union (MEDIA-VERIFY-S2-A R5): LRU eviction of an ACTIVE verification's revision.
# ---------------------------------------------------------------------------
#
# The R4 independent review reproduced a stale report being published as current. `_verify_revision`
# returns 0 for an absent key, so eviction of a running verification's entry made "we were
# invalidated" indistinguishable from "nothing has changed":
#
#   A captures 0 (no entry yet) -> A's folder changes, storing A = 1 -> `_VERIFY_REVISION_CAP`
#   other sessions invalidate and evict A -> the terminal read finds A absent and gets 0 ->
#   `0 == 0` -> the report for the folder the operator has already left is published.
#
# Measured on the pre-correction branch: 511 other sessions were safe, 512 published the stale
# report. The ABA form -- evicted at revision N, recreated at N by a later invalidation -- did the
# same thing from a non-zero start.
#
# These tests drive the SHIPPED `_on_verify_sources_click` with a barrier inside the stubbed impl,
# so the invalidation and the eviction pressure land while the verification is genuinely running,
# and they assert the TERMINAL DECISION (what the operator is shown), never that a helper was
# called. `_load_concurrency_seam(cap=...)` shrinks the bound in the test namespace so the
# pressure is cheap; the production constant is never edited.


def _verification_with_interference(interfere, *, cap=None, session="A", pre_bumps=0):
    """Run one real verification click; `interfere(ns)` runs while it is inside the impl.

    Returns `(returned_text, ns, state)`. The impl blocks on an Event so the interference is
    guaranteed to happen in the "running, not yet published" window rather than racing it.
    """
    entered = threading.Event()
    release = threading.Event()
    ns, state = _load_concurrency_seam(cap=cap)

    def blocking_impl(_folder, _recursive):
        entered.set()
        assert release.wait(30), "the test never released the verification"
        return "REPORT"

    state["impl"] = blocking_impl
    # [FORK] S2-B P2: the handler now keys the map by (session, scope), so a pre-bump that wants to
    # be the SAME entry the click will capture has to use the scoped key too. Keying it raw would
    # silently make `pre_bumps` touch a different entry and quietly destroy the ABA premise below.
    scoped = ns["_scoped_key"](session, ns["_SCOPE_VERIFY"])
    for _ in range(pre_bumps):
        ns["_bump_verify_revision"](scoped)

    out = {}

    def click():
        out["result"] = ns["_on_verify_sources_click"](
            "J:/lib", True, _FakeRequest(session))

    worker = threading.Thread(target=click, daemon=True)
    worker.start()
    try:
        assert entered.wait(30), "the verification never started"
        out["pinned_while_running"] = ns["_verify_active_session"]
        interfere(ns)
        out["present_while_running"] = scoped in ns["_verify_revisions"]
        out["map_len_while_running"] = len(ns["_verify_revisions"])
    finally:
        release.set()
        worker.join(30)
    assert not worker.is_alive(), "the verification must finish"
    return out, ns, state


def _evict_with_other_sessions(count):
    def interfere(ns):
        ns["_on_verify_invalidate"](_FakeRequest("A"))          # the operator edits A's folder
        for index in range(count):                              # other sessions churn the map
            ns["_bump_verify_revision"](f"other-{index}")
    return interfere


@pytest.mark.parametrize("others", [0, 1, 7, 8, 9, 16, 40])
def test_an_active_verification_survives_any_number_of_other_invalidations(others):
    """Scenario A, at and far beyond the cap. The stale report must ALWAYS be suppressed.

    `cap=8` makes "more other sessions than the whole map can hold" cheap to express; the
    production cap is asserted separately by `test_the_revision_cap_is_the_shipped_constant`.
    """
    out, ns, _state = _verification_with_interference(
        _evict_with_other_sessions(others), cap=8)
    assert out["result"] == "STALE_FOLDER_CHANGED", (
        f"{others} other sessions invalidated and the stale report was published anyway: "
        f"{out['result']!r}")
    assert out["pinned_while_running"] == ns["_scoped_key"]("A", ns["_SCOPE_VERIFY"]), (
        "the pin must be the SCOPED key the click captured")
    assert out["present_while_running"], "the active session's entry must survive eviction"
    assert out["map_len_while_running"] <= 8, "the cap must hold INCLUDING the pinned entry"


@pytest.mark.parametrize("others", [511, 512, 513, 1024])
def test_the_reproduced_defect_thresholds_are_all_suppressed_at_the_real_cap(others):
    """The exact numbers R4 measured, against the real shipped cap.

    Pre-correction: 511 withheld, 512 and above published the stale report. All four must now
    withhold it.
    """
    out, _ns, _state = _verification_with_interference(_evict_with_other_sessions(others))
    assert out["result"] == "STALE_FOLDER_CHANGED", f"{others} others -> {out['result']!r}"
    assert out["present_while_running"]


@pytest.mark.parametrize("captured", [1, 2, 9])
def test_the_aba_recreation_case_is_suppressed(captured):
    """Scenario B: a non-zero captured revision, eviction pressure, then another invalidation.

    Before R5 the entry could be dropped and recreated at the same integer, so the comparison
    saw no change. The pin makes the eviction impossible, so the second invalidation increments
    from the surviving value instead of from zero.
    """
    out, ns, _state = _verification_with_interference(
        _evict_with_other_sessions(40), cap=8, pre_bumps=captured)
    assert out["result"] == "STALE_FOLDER_CHANGED"
    assert ns["_verify_revisions"][ns["_scoped_key"]("A", ns["_SCOPE_VERIFY"])] == captured + 1, (
        "the invalidation must increment the surviving revision, not restart from 0")


def test_an_evicting_storm_without_any_invalidation_still_publishes():
    """The other direction: eviction pressure alone must not fabricate a stale notice."""
    def interfere(ns):
        for index in range(40):
            ns["_bump_verify_revision"](f"other-{index}")

    out, _ns, _state = _verification_with_interference(interfere, cap=8)
    assert out["result"] == "REPORT", "no invalidation happened, so the report is current"


def test_an_unaffected_session_is_not_invalidated_by_another_sessions_edit():
    """Session isolation survives the pin: B's edit plus heavy churn must not stale A's report."""
    def interfere(ns):
        ns["_on_verify_invalidate"](_FakeRequest("B"))
        for index in range(40):
            ns["_bump_verify_revision"](f"other-{index}")

    out, _ns, _state = _verification_with_interference(interfere, cap=8)
    assert out["result"] == "REPORT"


def test_multiple_edits_of_the_active_session_stay_detectable():
    def interfere(ns):
        for _ in range(5):
            ns["_on_verify_invalidate"](_FakeRequest("A"))
        for index in range(40):
            ns["_bump_verify_revision"](f"other-{index}")

    out, ns, _state = _verification_with_interference(interfere, cap=8)
    assert out["result"] == "STALE_FOLDER_CHANGED"
    assert ns["_verify_revisions"][ns["_scoped_key"]("A", ns["_SCOPE_VERIFY"])] == 5


def test_the_terminal_decision_is_made_before_the_pin_is_released():
    """The ordering requirement, driven at the last possible instant.

    The invalidation AND the eviction storm happen inside the impl, i.e. after the baseline was
    captured and before the comparison. An implementation that unpinned first and compared second
    would read A back as 0 here and publish.
    """
    def interfere(ns):
        ns["_on_verify_invalidate"](_FakeRequest("A"))
        for index in range(200):
            ns["_bump_verify_revision"](f"late-{index}")

    out, ns, _state = _verification_with_interference(interfere, cap=8)
    assert out["result"] == "STALE_FOLDER_CHANGED"
    assert ns["_verify_active_session"] == "", "the pin must be released afterwards"


def test_a_finished_verifications_entry_becomes_evictable_again():
    """The pin is for the span of one run, not a permanent reservation."""
    out, ns, _state = _verification_with_interference(
        _evict_with_other_sessions(0), cap=8)
    assert out["result"] == "STALE_FOLDER_CHANGED"
    scoped_a = ns["_scoped_key"]("A", ns["_SCOPE_VERIFY"])
    assert scoped_a in ns["_verify_revisions"]
    for index in range(20):
        ns["_bump_verify_revision"](f"after-{index}")
    assert scoped_a not in ns["_verify_revisions"], (
        "once the run is over the former active entry must evict like any other")
    assert len(ns["_verify_revisions"]) <= 8


def test_a_refused_verification_leaves_no_pin_and_no_entry():
    """Requirement J: a click that never got the mutex must leave nothing behind."""
    ns, state = _load_concurrency_seam()
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "precondition: a render holds the mutex"
    ns["_mark_lock_holder"](ns["_LOCK_HOLDER_RENDER"])
    try:
        out = ns["_on_verify_sources_click"]("J:/lib", True, _FakeRequest("A"))
    finally:
        ns["_RENDER_LOCK"].release()
    assert out == "VERIFY_REFUSED_RENDER_ACTIVE"
    assert ns["_verify_active_session"] == "", "a refusal must not pin"
    assert ns["_verify_revisions"] == {}, "a refusal must not insert"
    assert state["impl_calls"] == []


def test_rapid_verify_clicks_never_create_a_second_pin():
    entered = threading.Event()
    release = threading.Event()
    ns, state = _load_concurrency_seam()
    observed = []

    def blocking_impl(_folder, _recursive):
        observed.append(ns["_verify_active_session"])
        entered.set()
        assert release.wait(30)
        return "REPORT"

    state["impl"] = blocking_impl
    results = []
    workers = [threading.Thread(
        target=lambda index=index: results.append(ns["_on_verify_sources_click"](
            "J:/lib", True, _FakeRequest(f"S{index}"))), daemon=True)
        for index in range(6)]
    for worker in workers:
        worker.start()
    try:
        assert entered.wait(30)
    finally:
        release.set()
        for worker in workers:
            worker.join(30)
    assert len(state["impl_calls"]) == 1, "the mutex must admit exactly one"
    assert len(set(observed)) == 1, f"only one session may ever be pinned: {set(observed)}"
    assert ns["_verify_active_session"] == ""
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "the mutex must not leak"


def test_the_pin_is_cleared_and_the_mutex_freed_when_the_work_raises():
    ns, state = _load_concurrency_seam()

    def boom(_folder, _recursive):
        raise RuntimeError("verification blew up")

    state["impl"] = boom
    out = ns["_on_verify_sources_click"]("J:/lib", True, _FakeRequest("A"))
    assert "SOURCE VERIFICATION FAILED" in out
    assert ns["_verify_active_session"] == ""
    assert ns["_RENDER_LOCK"].acquire(blocking=False)


def test_the_pin_is_cleared_when_a_base_exception_propagates():
    """Requirement H's hardest path: not an `Exception`, so the inner handler cannot catch it."""
    ns, state = _load_concurrency_seam()

    def abandoned(_folder, _recursive):
        raise KeyboardInterrupt("request abandoned")

    state["impl"] = abandoned
    with pytest.raises(KeyboardInterrupt):
        ns["_on_verify_sources_click"]("J:/lib", True, _FakeRequest("A"))
    assert ns["_verify_active_session"] == "", "a BaseException must still release the pin"
    assert ns["_RENDER_LOCK"].acquire(blocking=False), "and still release the mutex"


def test_a_stale_suppressed_verification_also_clears_the_pin():
    out, ns, _state = _verification_with_interference(
        _evict_with_other_sessions(3), cap=8)
    assert out["result"] == "STALE_FOLDER_CHANGED"
    assert ns["_verify_active_session"] == ""
    assert ns["_RENDER_LOCK"].acquire(blocking=False)


def test_the_revision_cap_is_the_shipped_constant_not_a_test_literal(gui_tree):
    """Closes the R4 review's test-seam finding.

    The seam used to inject `512` itself, so mutating the production constant was invisible and
    the suite stayed green with the cap at 2. The cap must come from `src/gui.py`.
    """
    shipped = _module_constant(gui_tree, "_VERIFY_REVISION_CAP")
    ns, _state = _load_concurrency_seam()
    assert ns["_VERIFY_REVISION_CAP"] == shipped
    assert shipped == 512, (
        "the documented bound is 512; changing it changes how many sessions the map remembers "
        "and must be a deliberate, documented decision")


def test_the_eviction_loop_actually_exempts_the_active_session():
    """The mechanism, asserted directly rather than only through the handler.

    A bare `del _verify_revisions[next(iter(...))]` -- the pre-correction body -- drops the
    oldest key unconditionally, which is the active one here.
    """
    ns, _state = _load_concurrency_seam(cap=4)
    ns["_bump_verify_revision"]("A")            # A is the OLDEST entry
    ns["_begin_verify_session"]("A")
    for index in range(20):
        ns["_bump_verify_revision"](f"other-{index}")
    assert "A" in ns["_verify_revisions"], "the oldest key was evicted despite being pinned"
    assert len(ns["_verify_revisions"]) <= 4
    ns["_end_verify_session"]("A", 1)
    for index in range(20):
        ns["_bump_verify_revision"](f"later-{index}")
    assert "A" not in ns["_verify_revisions"]


def test_begin_and_end_are_each_one_atomic_step(gui_tree):
    """Requirement I: no read/pin or unpin/compare gap may exist in the shipped source.

    One lock block each is what actually closes the gap. Because `_bump_verify_revision` needs the
    same lock, nothing can be evicted part-way through either function, which is why the single
    block -- not the statement order inside it -- is the load-bearing property. The order is
    pinned separately by `test_the_end_step_reads_before_it_unpins` so a later refactor that
    splits the block cannot inherit a dangerous order.
    """
    for name in ("_begin_verify_session", "_end_verify_session"):
        node = _func(gui_tree, name)
        withs = [n for n in ast.walk(node) if isinstance(n, ast.With)]
        assert len(withs) == 1, f"{name} must do its work under exactly one lock block"
        guard = ast.dump(withs[0].items[0].context_expr)
        assert "_VERIFY_FLAG_LOCK" in guard, f"{name} must use _VERIFY_FLAG_LOCK"


def test_the_end_step_reads_before_it_unpins(gui_tree):
    """Requirement G, as source order rather than as a comment.

    Inside one critical section the two orders behave identically, so no runtime test can tell
    them apart -- an independent review's mutation of exactly this swap survived the suite. The
    order still has to be fixed: if the lock block is ever split, reading after the unpin is the
    original defect again, and this is the assertion that refuses the swap today.
    """
    node = _func(gui_tree, "_end_verify_session")
    block = next(n for n in ast.walk(node) if isinstance(n, ast.With))
    read_at = unpin_at = None
    for index, statement in enumerate(block.body):
        dumped = ast.dump(statement)
        if read_at is None and "_touch_verify_revision_locked" in dumped:
            read_at = index
        if unpin_at is None and isinstance(statement, ast.If) \
                and "_verify_active_session" in dumped:
            unpin_at = index
    assert read_at is not None, "_end_verify_session must read the current revision"
    assert unpin_at is not None, "_end_verify_session must release the pin"
    assert read_at < unpin_at, (
        "the revision must be read while the entry is still pinned, before the pin is released")


def test_only_the_begin_end_pair_ever_assigns_the_pin(gui_tree):
    """The pin must have exactly one writer at each end of a run, and no other.

    A third writer somewhere else would be a second lifecycle for the same fact, which is how the
    unpin-then-compare ordering gets reintroduced by accident.
    """
    writers = {}
    for node in ast.walk(gui_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            targets = []
            if isinstance(inner, ast.Assign):
                targets = inner.targets
            elif isinstance(inner, ast.AnnAssign):
                targets = [inner.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "_verify_active_session":
                    writers.setdefault(node.name, 0)
                    writers[node.name] += 1
    assert set(writers) == {"_begin_verify_session", "_end_verify_session"}, (
        f"unexpected writers of the active-session pin: {sorted(writers)}")
    assert writers["_begin_verify_session"] == 1
    assert writers["_end_verify_session"] == 1


def test_the_click_captures_its_baseline_after_taking_the_mutex(gui_tree):
    """Requirement A/J, structurally: the capture must not precede the acquire."""
    body = ast.unparse(_func(gui_tree, "_on_verify_sources_click"))
    acquire_at = body.index("_RENDER_LOCK.acquire(blocking=False)")
    begin_at = body.index("_begin_verify_session(session)")
    end_at = body.index("_end_verify_session(session, revision)")
    release_at = body.index("_RENDER_LOCK.release()")
    assert acquire_at < begin_at, "a refused click must not capture or pin"
    assert begin_at < end_at
    assert end_at < release_at, (
        "the staleness decision must be made before the mutex is released, while still pinned")


# ---------------------------------------------------------------------------
# resource bounds survive the correction
# ---------------------------------------------------------------------------

def test_the_submitted_work_window_stays_bounded_whatever_the_library_size(tmp_path):
    """Closes R2's MINOR-5: the sliding SUBMISSION window had no assertion of its own.

    Counting probes that have *entered* does not work -- the thread pool caps that at `workers`
    whether a window exists or not, which is exactly why the earlier attempt at this test passed
    with the window deleted. What the window actually bounds is the number of WORK ITEMS handed to
    the executor, so that is what is counted here, by recording `submit` calls on the real
    executor while every worker is blocked.
    """
    import beatsync_fork.media_verify as module
    sources = [_video(tmp_path, f"s{i:04d}.mp4") for i in range(400)]
    release = threading.Event()
    submitted = []
    entered = []
    guard = threading.Lock()
    real_executor = module.ThreadPoolExecutor

    class _RecordingExecutor(real_executor):
        def submit(self, fn, /, *args, **kwargs):
            with guard:
                submitted.append(1)
            return super().submit(fn, *args, **kwargs)

    def blocking_probe(path, timeout=None):
        with guard:
            entered.append(path)
        release.wait(15)
        return ProbeOutcome(returncode=0, stdout=_payload())

    module.ThreadPoolExecutor = _RecordingExecutor
    worker = threading.Thread(target=lambda: verify_media_health(
        sources, blocking_probe, workers=4, baselines=fingerprint_sources(sources)),
        daemon=True)
    try:
        worker.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with guard:
                if len(entered) >= MAX_PROBE_WORKERS:
                    break
            time.sleep(0.01)
        time.sleep(0.3)          # let any unbounded submission burst finish
        with guard:
            outstanding = len(submitted)
    finally:
        release.set()
        worker.join(60)
        module.ThreadPoolExecutor = real_executor
    assert not worker.is_alive(), "the run must drain and return"
    assert outstanding <= MAX_PROBE_WORKERS * 2, \
        f"{outstanding} work items were submitted while every worker was blocked; the window is " \
        f"workers*2 = {MAX_PROBE_WORKERS * 2}"
    assert outstanding < len(sources), "the whole library must not be submitted up front"


def test_baselines_do_not_change_the_bounds(tmp_path):
    sources = [_video(tmp_path, f"s{i:03d}.mp4") for i in range(60)]
    live = {"now": 0, "peak": 0}
    timeouts = set()
    guard = threading.Lock()

    def probe(path, timeout=None):
        timeouts.add(timeout)
        with guard:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.001)
        with guard:
            live["now"] -= 1
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(sources, probe, workers=99,
                              baselines=fingerprint_sources(sources))
    assert live["peak"] <= MAX_PROBE_WORKERS and run.workers == MAX_PROBE_WORKERS
    assert timeouts == {DEFAULT_PROBE_TIMEOUT_SECONDS}
    assert [v.path for v in run.verdicts] == sources, "source order survives"
    assert DEFAULT_BUDGET_SECONDS == 600, "the global scheduling budget is unchanged"


def test_a_budget_exhausted_run_with_baselines_still_reports_not_checked(tmp_path):
    sources = [_video(tmp_path, f"s{i:03d}.mp4") for i in range(20)]
    ticks = iter([0.0] * 9 + [10_000.0] * 100_000)
    started, finished = [], []

    def probe(path, timeout=None):
        started.append(path)
        finished.append(path)
        return ProbeOutcome(returncode=0, stdout=_payload())

    run = verify_media_health(sources, probe, workers=1, budget_seconds=5.0,
                              baselines=fingerprint_sources(sources),
                              clock=lambda: next(ticks))
    assert run.budget_exhausted and not run.complete
    assert any(v.status is MediaStatus.NOT_CHECKED for v in run.verdicts)
    assert run.scheduled < len(sources)
    assert started == finished, "every started probe was reaped before anything was reported"
