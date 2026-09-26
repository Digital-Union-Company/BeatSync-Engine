"""Phase 2B: the parent must see Qwen worker progress *while* the worker runs.

Every test here drives a real child process — a tiny fake worker written into ``tmp_path`` — because
the defect being fixed is a property of the process boundary, not of a parsing function. A mocked
``Popen`` would have happily "streamed" under the old ``capture_output=True`` code too.

The fake worker deliberately mirrors only the contract the parent depends on: human lines plus
namespaced machine lines, both flushed, then a response JSON, then an exit code. No model, no
llama.cpp, no OpenCV.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from beatsync_fork import qwen_progress as qp
from beatsync_fork.progress import EventKind


# ---------------------------------------------------------------------------
# fake workers
# ---------------------------------------------------------------------------


def _write_worker(tmp_path, body: str, name: str = "fake_worker.py") -> str:
    path = tmp_path / name
    path.write_text(
        "import json, os, sys, time\n"
        f"PREFIX = {qp.PROTOCOL_PREFIX!r}\n"
        "def emit(kind, **f):\n"
        "    p = {'v': 'beatsync.qwen-progress/1', 'kind': kind}\n"
        "    p.update(f)\n"
        "    sys.stdout.write(PREFIX + json.dumps(p) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "REQUEST = sys.argv[sys.argv.index('--request') + 1]\n"
        "RESPONSE = sys.argv[sys.argv.index('--response') + 1]\n"
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    return str(path)


def _argv(worker: str, tmp_path, request: dict | None = None) -> tuple[list[str], str]:
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    request_path.write_text(json.dumps(request or {"jobs": []}), encoding="utf-8")
    return (
        [sys.executable, worker, "--request", str(request_path), "--response", str(response_path)],
        str(response_path),
    )


STREAMING_WORKER = """
    emit('job_start', job_index=1, job_total=3, job_id='1', source_name='clip.mp4',
         requested_candidate_count=120)
    emit('job_progress', job_index=1, job_total=3, job_id='1', source_name='clip.mp4',
         current=32, total=120, candidates_per_second=2.14, batch_size=8)
    print('Qwen llama.cpp tagged 32/120 (2.14/s, batch 8)', flush=True)
    time.sleep(float(os.environ.get('FAKE_SLEEP', '1.0')))
    emit('job_progress', job_index=1, job_total=3, job_id='1', source_name='clip.mp4',
         current=120, total=120, candidates_per_second=2.09, batch_size=8)
    emit('job_end', job_index=1, job_total=3, job_id='1', source_name='clip.mp4',
         frame_count=120, tag_count=118, inference_seconds=57.4)
    json.dump({'semantics_by_job': {'1': {'c0': {'action_intensity': 0.5}}},
               'model_id': 'fake', 'batch_size': 8},
              open(RESPONSE, 'w', encoding='utf-8'))
"""


class Recorder:
    """Records events with the wall-clock offset at which the parent actually received them."""

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.events: list[tuple[float, object]] = []
        self.lock = threading.Lock()

    def __call__(self, event) -> None:
        with self.lock:
            self.events.append((time.perf_counter() - self.t0, event))

    @property
    def messages(self) -> list[str]:
        return [event.message for _, event in self.events]

    def first_at(self) -> float | None:
        return self.events[0][0] if self.events else None


# ---------------------------------------------------------------------------
# A / B — streaming before exit, then monotone follow-up
# ---------------------------------------------------------------------------


def test_first_progress_event_arrives_before_the_worker_exits(tmp_path):
    """A. The whole point of Phase 2B.

    Under ``subprocess.run(capture_output=True)`` this is impossible: the first line is observable
    only once the child is gone. ``red_capture_repro`` measured exactly that (first callback at
    0.857s == the 0.857s exit).
    """
    worker = _write_worker(tmp_path, STREAMING_WORKER)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()
    env = dict(os.environ, FAKE_SLEEP="1.0", PYTHONUNBUFFERED="1")

    result = qp.run_qwen_worker(argv, response_path, env=env, timeout=60,
                               event_callback=recorder)
    exit_at = result.outcome.seconds

    assert recorder.events, "no progress events were delivered at all"
    first = recorder.first_at()
    assert first is not None and first < exit_at, (
        f"first event at {first:.3f}s, child exited at {exit_at:.3f}s"
    )
    # Not merely "before exit" but before the sleep the worker spends mid-job.
    assert first < exit_at - 0.5, (
        f"first event at {first:.3f}s is not meaningfully earlier than exit {exit_at:.3f}s"
    )
    assert result.ok


def test_second_progress_updates_the_same_job_monotonically(tmp_path):
    """B. A later event advances the same job rather than starting a new one."""
    worker = _write_worker(tmp_path, STREAMING_WORKER)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    qp.run_qwen_worker(argv, response_path, env=dict(os.environ, FAKE_SLEEP="0.4"),
                       timeout=60, event_callback=recorder)

    progress = [event for _, event in recorder.events if event.data.get("current") is not None]
    assert len(progress) >= 2, recorder.messages
    assert [event.data["current"] for event in progress] == sorted(
        event.data["current"] for event in progress
    )
    assert {event.data["job_index"] for event in progress} == {1}
    assert progress[-1].data["current"] == 120
    # The two progress events are separated in time by the worker's own pause.
    times = [offset for offset, event in recorder.events if event.data.get("current") is not None]
    assert times[-1] - times[0] > 0.2, times


# ---------------------------------------------------------------------------
# C / D / E — the parser cannot be hurt by what the worker prints
# ---------------------------------------------------------------------------


def test_human_lines_are_not_parsed_as_protocol(tmp_path):
    """C. Ordinary stdout stays ordinary and is handed to the human sink instead."""
    worker = _write_worker(tmp_path, """
    print('Qwen llama.cpp model: Qwen3VL-2B-Instruct-Q8_0.gguf', flush=True)
    print('Qwen llama.cpp tagged 32/120 (2.14/s, batch 8)', flush=True)
    emit('job_progress', job_index=2, job_total=2, current=5, total=10,
         candidates_per_second=1.5, batch_size=4)
    print('', flush=True)
    json.dump({'semantics_by_job': {}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()
    human: list[str] = []

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder,
                               on_human_line=human.append)

    assert result.outcome.returncode == 0
    assert any("tagged 32/120" in line for line in human)
    assert any("model: Qwen3VL" in line for line in human)
    assert all(not qp.is_protocol_line(line) for line in human)
    assert all(line.strip() for line in human), "blank lines should not be forwarded"
    # The one machine line produced exactly one event; the prose produced none.
    assert len(recorder.events) == 1
    assert recorder.events[0][1].data["current"] == 5


def test_malformed_protocol_line_cannot_crash_the_worker_path(tmp_path):
    """D. Truncated/garbage JSON behind the namespace is counted and ignored."""
    worker = _write_worker(tmp_path, """
    sys.stdout.write(PREFIX + '{not json at all' + '\\n')
    sys.stdout.write(PREFIX + '[1, 2, 3]' + '\\n')
    sys.stdout.write(PREFIX + '"a string"' + '\\n')
    sys.stdout.write(PREFIX + '{}' + '\\n')
    sys.stdout.write(PREFIX + '\\n')
    sys.stdout.flush()
    emit('job_progress', job_index=1, job_total=1, current=7, total=7,
         candidates_per_second=3.0, batch_size=2)
    json.dump({'semantics_by_job': {}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()
    translator = qp.QwenProgressTranslator()

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder,
                               translator=translator)

    assert result.ok, result.outcome
    assert translator.malformed_lines == 5
    assert len(recorder.events) == 1, recorder.messages
    assert recorder.events[0][1].data["current"] == 7


def test_unknown_protocol_kind_is_ignored_safely(tmp_path):
    """E. A worker newer than this parent must not be able to break Stage 5."""
    worker = _write_worker(tmp_path, """
    emit('teleport_the_gpu', job_index=1, mystery=True)
    emit('job_end', job_index=1, job_total=1, frame_count=3, tag_count=3, inference_seconds=1.0)
    json.dump({'semantics_by_job': {}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()
    translator = qp.QwenProgressTranslator()

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder,
                               translator=translator)

    assert result.ok
    assert translator.unknown_kinds == 1
    assert len(recorder.events) == 1
    assert "3 / 3 tagged" in recorder.events[0][1].message


# ---------------------------------------------------------------------------
# F — both pipes drained concurrently
# ---------------------------------------------------------------------------


def test_heavy_stderr_does_not_deadlock_and_stays_bounded(tmp_path):
    """F. A child that fills the stderr pipe while writing stdout must not wedge the parent.

    ~600 KB of stderr is far beyond any OS pipe buffer (64 KB on Windows), so reading stdout to EOF
    first — or reading stderr only after ``wait()`` — deadlocks here. The test also proves the
    retained tail is bounded rather than accumulating in RAM.
    """
    worker = _write_worker(tmp_path, """
    emit('job_start', job_index=1, job_total=1, source_name='loud.mp4',
         requested_candidate_count=4)
    for i in range(6000):
        sys.stderr.write('ggml_vulkan: diagnostic spew line %05d padding padding padding\\n' % i)
    sys.stderr.flush()
    emit('job_end', job_index=1, job_total=1, frame_count=4, tag_count=4, inference_seconds=0.2)
    json.dump({'semantics_by_job': {'1': {}}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    started = time.perf_counter()
    result = qp.run_qwen_worker(argv, response_path, timeout=120, event_callback=recorder,
                               stderr_char_limit=2400)
    elapsed = time.perf_counter() - started

    assert result.ok, result.outcome
    assert elapsed < 60, f"suspiciously slow ({elapsed:.1f}s) — pipe contention"
    assert len(result.outcome.stderr_tail) <= 2400
    assert "diagnostic spew line 05999" in result.outcome.stderr_tail, "tail must be the END of stderr"
    assert len(recorder.events) == 2


# ---------------------------------------------------------------------------
# G — non-zero exit
# ---------------------------------------------------------------------------


def test_non_zero_exit_returns_fallback_with_bounded_tail(tmp_path):
    """G. Same fallback contract as before: no exception, empty dict, bounded diagnostics."""
    worker = _write_worker(tmp_path, """
    emit('job_start', job_index=1, job_total=1, source_name='broken.mp4',
         requested_candidate_count=9)
    print('Qwen llama.cpp model: fake.gguf', flush=True)
    sys.stderr.write('x' * 9000 + '\\nFATAL: vk::DeviceLostError\\n')
    sys.stderr.flush()
    json.dump({'semantics_by_job': {'1': {'c0': {}}}}, open(RESPONSE, 'w', encoding='utf-8'))
    sys.exit(3)
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder,
                               stderr_char_limit=2400)

    assert result.outcome.returncode == 3
    assert not result.ok
    assert result.response == {}, "a failed worker must not contribute semantics"
    assert 0 < len(result.outcome.stderr_tail) <= 2400
    assert "DeviceLostError" in result.outcome.stderr_tail
    # Progress seen before the failure still reached the UI.
    assert any("starting" in message for message in recorder.messages)


def test_response_json_is_ignored_when_the_worker_failed(tmp_path):
    """G (corollary). A usable response file on disk cannot rescue a non-zero exit."""
    worker = _write_worker(tmp_path, """
    json.dump({'semantics_by_job': {'1': {'c0': {'action_intensity': 0.9}}}},
              open(RESPONSE, 'w', encoding='utf-8'))
    sys.exit(1)
    """)
    argv, response_path = _argv(worker, tmp_path)

    result = qp.run_qwen_worker(argv, response_path, timeout=60)

    assert os.path.exists(response_path)
    assert result.response == {}


# ---------------------------------------------------------------------------
# H — timeout
# ---------------------------------------------------------------------------


def test_timeout_terminates_the_worker_and_returns_fallback(tmp_path):
    """H. ``subprocess.run(timeout=…)``'s guarantee is preserved: bounded wait, dead child.

    The worker writes a marker file only if it is allowed to finish, so the assertion that the marker
    never appears is direct evidence the process was really killed rather than merely abandoned.
    """
    marker = tmp_path / "worker_finished.txt"
    worker = _write_worker(tmp_path, f"""
    emit('job_start', job_index=1, job_total=1, source_name='slow.mp4',
         requested_candidate_count=2)
    time.sleep(30)
    open({str(marker)!r}, 'w', encoding='utf-8').write('finished')
    json.dump({{}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()
    baseline = threading.active_count()

    started = time.perf_counter()
    result = qp.run_qwen_worker(argv, response_path, timeout=1.5, event_callback=recorder)
    elapsed = time.perf_counter() - started

    assert result.outcome.timed_out
    assert not result.ok
    assert result.response == {}
    assert elapsed < 20, f"timeout path took {elapsed:.1f}s"
    assert result.outcome.reader_threads_alive == 0, "reader threads must be joined"

    # The child is gone: give it well past its own sleep and the marker still never appears.
    time.sleep(2.0)
    assert not marker.exists(), "worker survived the timeout and kept running"
    assert threading.active_count() <= baseline, "leaked reader threads"
    # Progress emitted before the timeout is still honest output.
    assert any("starting" in message for message in recorder.messages)


def test_timeout_still_reports_progress_seen_before_the_kill(tmp_path):
    """H (corollary). Streaming means a timeout is no longer an information-free outcome."""
    worker = _write_worker(tmp_path, """
    emit('job_progress', job_index=4, job_total=9, source_name='slow.mp4',
         current=64, total=120, candidates_per_second=0.4, batch_size=8)
    time.sleep(30)
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    qp.run_qwen_worker(argv, response_path, timeout=1.5, event_callback=recorder)

    assert recorder.events
    message = recorder.messages[0]
    assert "Qwen job 4 / 9" in message
    assert "64 / 120 candidates (53.3%)" in message


# ---------------------------------------------------------------------------
# I / J — the response JSON stays the semantic authority
# ---------------------------------------------------------------------------


def test_valid_response_json_is_loaded_after_streaming(tmp_path):
    """I."""
    worker = _write_worker(tmp_path, STREAMING_WORKER)
    argv, response_path = _argv(worker, tmp_path)

    result = qp.run_qwen_worker(argv, response_path, env=dict(os.environ, FAKE_SLEEP="0.2"),
                               timeout=60)

    assert result.ok
    assert result.response["semantics_by_job"] == {"1": {"c0": {"action_intensity": 0.5}}}
    assert result.response["model_id"] == "fake"
    with open(response_path, "r", encoding="utf-8") as handle:
        assert result.response == json.load(handle), "loaded dict must be the file, verbatim"


@pytest.mark.parametrize("body, label", [
    ("pass", "missing file"),
    ("open(RESPONSE, 'w', encoding='utf-8').write('{truncated')", "malformed json"),
    ("open(RESPONSE, 'w', encoding='utf-8').write('[1,2,3]')", "json array, not an object"),
])
def test_unusable_response_json_is_a_failure_not_a_success(tmp_path, body, label):
    """J. Exit code 0 is not enough — without a usable response there are no semantics."""
    worker = _write_worker(tmp_path, f"""
    emit('job_end', job_index=1, job_total=1, frame_count=1, tag_count=1, inference_seconds=0.1)
    {body}
    """)
    argv, response_path = _argv(worker, tmp_path)

    result = qp.run_qwen_worker(argv, response_path, timeout=60)

    assert result.outcome.returncode == 0
    assert result.response == {}, label
    assert not result.ok, label
    assert result.response_error, label


# ---------------------------------------------------------------------------
# K — a broken callback cannot break Qwen
# ---------------------------------------------------------------------------


def test_callback_exception_cannot_break_the_worker(tmp_path):
    """K. Hours of GPU work must not be lost to a status widget that has gone away."""
    worker = _write_worker(tmp_path, STREAMING_WORKER)
    argv, response_path = _argv(worker, tmp_path)
    calls = {"n": 0}

    def hostile(event):
        calls["n"] += 1
        raise RuntimeError("status queue is gone")

    result = qp.run_qwen_worker(argv, response_path, env=dict(os.environ, FAKE_SLEEP="0.2"),
                               timeout=60, event_callback=hostile)

    assert calls["n"] >= 2, "callback was never reached"
    assert result.ok
    assert result.response["model_id"] == "fake"


def test_hostile_human_line_sink_cannot_break_the_worker(tmp_path):
    """K (corollary). The stdout drain must survive a raising sink, or the pipe stops being read."""
    worker = _write_worker(tmp_path, """
    for i in range(200):
        print('Qwen llama.cpp tagged %d/200' % i, flush=True)
    emit('job_end', job_index=1, job_total=1, frame_count=200, tag_count=200,
         inference_seconds=1.0)
    json.dump({'semantics_by_job': {}}, open(RESPONSE, 'w', encoding='utf-8'))
    """)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    def hostile(_line):
        raise RuntimeError("console exploded")

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder,
                               on_human_line=hostile)

    assert result.ok
    assert result.outcome.stdout_lines == 201
    assert len(recorder.events) == 1


# ---------------------------------------------------------------------------
# L / M — both execution modes stream
# ---------------------------------------------------------------------------


SINGLE_WORKER = """
    request = json.load(open(REQUEST, encoding='utf-8'))
    cands = request.get('candidates') or []
    emit('job_start', job_index=1, job_total=1, job_id='single',
         source_name=os.path.basename(request.get('video_file', 'one.mp4')),
         requested_candidate_count=len(cands))
    emit('job_progress', job_index=1, job_total=1, job_id='single', current=1, total=len(cands),
         candidates_per_second=1.1, batch_size=1)
    time.sleep(0.6)
    emit('job_end', job_index=1, job_total=1, job_id='single', frame_count=len(cands),
         tag_count=len(cands), inference_seconds=0.6)
    json.dump({'semantics': {c['id']: {'action_intensity': 0.25} for c in cands},
               'timings_by_job': {'single': {'frame_count': len(cands),
                                             'tag_count': len(cands)}}},
              open(RESPONSE, 'w', encoding='utf-8'))
"""

BATCH_WORKER = """
    request = json.load(open(REQUEST, encoding='utf-8'))
    jobs = request['jobs']
    out = {}
    for i, job in enumerate(jobs, 1):
        emit('job_start', job_index=i, job_total=len(jobs), job_id=job['job_id'],
             source_name=os.path.basename(job['video_file']),
             requested_candidate_count=len(job['candidates']))
        emit('job_progress', job_index=i, job_total=len(jobs), job_id=job['job_id'],
             current=len(job['candidates']), total=len(job['candidates']),
             candidates_per_second=2.0 + i, batch_size=8)
        time.sleep(0.3)
        emit('job_end', job_index=i, job_total=len(jobs), job_id=job['job_id'],
             frame_count=len(job['candidates']), tag_count=len(job['candidates']),
             inference_seconds=0.3)
        out[job['job_id']] = {c['id']: {'action_intensity': 0.4} for c in job['candidates']}
    json.dump({'semantics_by_job': out, 'batch_size': 8},
              open(RESPONSE, 'w', encoding='utf-8'))
"""


def test_single_worker_mode_streams(tmp_path):
    """L. The legacy one-video path is not allowed to silently lose live progress."""
    worker = _write_worker(tmp_path, SINGLE_WORKER)
    argv, response_path = _argv(worker, tmp_path, request={
        "video_file": "C:/videos/one.mp4", "fps": 24.0,
        "candidates": [{"id": "c0", "start": 0, "end": 1}, {"id": "c1", "start": 1, "end": 2}],
    })
    recorder = Recorder()

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder)

    assert result.ok
    assert set(result.response["semantics"]) == {"c0", "c1"}
    assert recorder.first_at() < result.outcome.seconds - 0.3
    assert any("one.mp4" in message for message in recorder.messages)
    assert all(event.data.get("phase") == "qwen" for _, event in recorder.events)


def test_batch_worker_mode_streams_every_job(tmp_path):
    """M. Per-job resets are what break a counted phase; here they must all show up."""
    worker = _write_worker(tmp_path, BATCH_WORKER)
    argv, response_path = _argv(worker, tmp_path, request={"jobs": [
        {"job_id": "1", "video_file": "C:/v/a.mp4", "fps": 24.0,
         "candidates": [{"id": "a0", "start": 0, "end": 1}]},
        {"job_id": "2", "video_file": "C:/v/b.mp4", "fps": 24.0,
         "candidates": [{"id": "b0", "start": 0, "end": 1}, {"id": "b1", "start": 1, "end": 2}]},
        {"job_id": "3", "video_file": "C:/v/c.mp4", "fps": 24.0,
         "candidates": [{"id": "c0", "start": 0, "end": 1}]},
    ]})
    recorder = Recorder()

    result = qp.run_qwen_worker(argv, response_path, timeout=60, event_callback=recorder)

    assert result.ok
    assert set(result.response["semantics_by_job"]) == {"1", "2", "3"}
    seen_jobs = {event.data.get("job_index") for _, event in recorder.events}
    assert seen_jobs == {1, 2, 3}, recorder.messages
    assert recorder.first_at() < result.outcome.seconds - 0.3
    # Job 2's later, smaller count is not suppressed by job 1 having finished at 1/1.
    job2 = [event for _, event in recorder.events
            if event.data.get("job_index") == 2 and event.data.get("current") is not None]
    assert job2 and job2[-1].data["total"] == 2
    assert all("Qwen job" in message for message in recorder.messages)


# ---------------------------------------------------------------------------
# 22 — old capture path vs new streaming path
# ---------------------------------------------------------------------------


def _legacy_capture_path(argv, response_path, timeout):
    """Verbatim shape of the pre-Phase-2B parent boundary, for a like-for-like comparison."""
    result = subprocess.run(
        list(argv), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False, env=os.environ.copy(),
    )
    if result.returncode != 0:
        return {}
    with open(response_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


@pytest.mark.parametrize("body", [SINGLE_WORKER, BATCH_WORKER, STREAMING_WORKER])
def test_streaming_path_returns_exactly_what_the_capture_path_returned(tmp_path, body):
    """§22. The protocol is observability: identical response JSON ⇒ identical parent result."""
    worker = _write_worker(tmp_path, body)
    request = {
        "video_file": "C:/videos/one.mp4", "fps": 24.0,
        "candidates": [{"id": "c0", "start": 0, "end": 1}],
        "jobs": [{"job_id": "1", "video_file": "C:/v/a.mp4", "fps": 24.0,
                  "candidates": [{"id": "a0", "start": 0, "end": 1}]}],
    }
    old_dir = tmp_path / "old"
    new_dir = tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    old_argv, old_response = _argv(worker, old_dir, request)
    new_argv, new_response = _argv(worker, new_dir, request)
    env = dict(os.environ, FAKE_SLEEP="0.1")

    legacy = _legacy_capture_path(old_argv, old_response, timeout=60)
    streamed = qp.run_qwen_worker(new_argv, new_response, env=env, timeout=60,
                                  event_callback=Recorder())

    assert streamed.response == legacy, "streaming changed the semantic result"
    assert streamed.response != {} or legacy == {}


def test_result_is_identical_with_and_without_a_progress_callback(tmp_path):
    """§1. The same request must produce the same semantics whether anyone is watching or not."""
    worker = _write_worker(tmp_path, BATCH_WORKER)
    request = {"jobs": [{"job_id": "1", "video_file": "C:/v/a.mp4", "fps": 24.0,
                         "candidates": [{"id": "a0", "start": 0, "end": 1}]}]}
    with_dir = tmp_path / "with"
    without_dir = tmp_path / "without"
    with_dir.mkdir()
    without_dir.mkdir()
    argv_a, response_a = _argv(worker, with_dir, request)
    argv_b, response_b = _argv(worker, without_dir, request)

    watched = qp.run_qwen_worker(argv_a, response_a, timeout=60, event_callback=Recorder())
    unwatched = qp.run_qwen_worker(argv_b, response_b, timeout=60, event_callback=None)

    assert watched.response == unwatched.response
    assert watched.ok and unwatched.ok


# ---------------------------------------------------------------------------
# launch failure
# ---------------------------------------------------------------------------


def test_unlaunchable_worker_is_reported_not_raised(tmp_path):
    """A missing interpreter/script is the same class of failure as a non-zero exit."""
    argv = [os.path.join(str(tmp_path), "definitely-not-here.exe"), "--request", "x",
            "--response", "y"]
    outcome = qp.stream_worker_process(argv, timeout=10)

    assert outcome.launch_error
    assert not outcome.ok
    assert outcome.returncode is None


def test_events_carry_the_qwen_phase_and_no_counted_stage_fields(tmp_path):
    """Stage 5 / phase="qwen" / uncounted STATE — the Phase 2A contract these events must honour."""
    worker = _write_worker(tmp_path, STREAMING_WORKER)
    argv, response_path = _argv(worker, tmp_path)
    recorder = Recorder()

    qp.run_qwen_worker(argv, response_path, env=dict(os.environ, FAKE_SLEEP="0.1"),
                       timeout=60, event_callback=recorder)

    assert recorder.events
    for _, event in recorder.events:
        assert event.stage == 5
        assert event.kind is EventKind.STATE
        assert event.data["phase"] == "qwen"
        # Counted-field absence is what keeps ProgressView from inventing a Qwen percentage.
        assert event.current is None
        assert event.total is None
