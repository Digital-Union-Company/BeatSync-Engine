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
import threading
import time

import pytest

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
    items = []
    items += [{"path": f"J:/new{i}.mp4", "status": "NEEDS_ANALYSIS",
               "reason": "NEW_OR_CHANGED"} for i in range(new_or_changed)]
    items += [{"path": f"J:/inc{i}.mp4", "status": "NEEDS_ANALYSIS",
               "reason": "INCOMPLETE_OR_INVALID"} for i in range(incomplete)]
    items += [{"path": f"J:/gone{i}.mp4", "status": "SOURCE_IDENTITY_UNAVAILABLE",
               "reason": ""} for i in range(unavailable)]
    items += [{"path": f"J:/ok{i}.mp4", "status": "PREPARED", "reason": ""}
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
        if item["status"] != "PREPARED":
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

def _load_concurrency_seam():
    """Execute the REAL `_on_verify_sources_click` over a real Lock and a stubbed impl."""
    tree = _tree(_GUI)
    nodes = [_func(tree, name) for name in
             ("_mark_verification", "_busy_message", "_on_verify_sources_click")]
    state = {"impl_calls": [], "impl": lambda folder, recursive: "REPORT"}
    namespace = {
        "threading": threading,
        "_RENDER_LOCK": threading.Lock(),
        "_VERIFY_FLAG_LOCK": threading.Lock(),
        "_verification_running": False,
        "RENDER_BUSY_MESSAGE": "RENDER_BUSY",
        "VERIFY_BUSY_MESSAGE": "VERIFY_BUSY",
        "PREP_VERIFY_RENDER_BUSY_TEXT": "VERIFY_REFUSED_RENDER_ACTIVE",
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
    assert ns["_busy_message"]() == "RENDER_BUSY", "the verification flag leaked"


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
    assert ns["_busy_message"]() == "RENDER_BUSY"


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
    assert ns["_busy_message"]() == "RENDER_BUSY"


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
    assert second == "VERIFY_REFUSED_RENDER_ACTIVE"
    assert results == ["REPORT"]


def test_the_click_handler_takes_only_the_live_folder_and_recursive_widgets(gui_tree):
    """No `prep_state`: a queued change event must not let it verify the previous folder."""
    handler = _func(gui_tree, "_on_verify_sources_click")
    arg_names = [a.arg for a in handler.args.args]
    assert arg_names == ["folder_path", "recursive"], arg_names

    source = ast.unparse(gui_tree)
    call = next(seg for seg in source.split("prep_verify_btn.click(")[1:])
    wiring = call.split(")")[0]
    assert "inputs=[prep_folder, prep_recursive]" in wiring, wiring
    assert "prep_state" not in wiring, "verification must not read the recorded scan"


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
    assert source.count("_RENDER_LOCK.acquire(blocking=False)") == 3, \
        "two render sites plus verification, each a single atomic acquire"
    assert "_RENDER_LOCK.locked()" not in source, "check-then-act is the race this avoids"
    assert "_RENDER_LOCK.acquire(blocking=True)" not in source
    assert "_RENDER_LOCK.acquire()" not in source
    # Every acquire has a matching release, and verification's is in a finally.
    assert source.count("_RENDER_LOCK.release()") == 3
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
    # Neither function takes or returns any Gradio state object.
    for name in ("_on_verify_sources_click", "_verify_sources_impl"):
        node = _func(gui_tree, name)
        assert [a.arg for a in node.args.args] == ["folder_path", "recursive"]


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
