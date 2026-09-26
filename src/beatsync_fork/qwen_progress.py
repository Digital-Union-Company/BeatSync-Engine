#!/usr/bin/env python3
"""Live Qwen worker progress: wire protocol, translator, and the streaming runner.

Phase 2A made the pipeline emit structured :class:`~beatsync_fork.progress.ProgressEvent`s, but
Stage 5's semantic pass stayed opaque: ``video_analysis`` launched
``auto_mode/stage5_qwen_scene_worker.py`` with ``subprocess.run(capture_output=True)``, so the
worker's ``Qwen llama.cpp tagged 32/120`` lines reached the parent only *after* the worker exited.
On a 300-video batch that is the longest apparently-idle stretch of the whole run. Phase 2B closes
that one boundary.

Three pieces live here, all stdlib-only so the whole path is testable on a bare interpreter without
CUDA, llama.cpp, OpenCV or a GGUF model:

``encode`` / ``decode`` / :data:`PROTOCOL_PREFIX`
    A namespaced one-line JSON protocol the worker writes to stdout **in addition to** its existing
    human-readable lines. Phase 2A deliberately deleted prose parsing from the GUI
    (``re.search(r"Stage (\\d+) is processing", …)``); reintroducing it at the subprocess boundary
    would repeat the same mistake one layer down, so the machine channel is explicit and versioned.
    The human lines are untouched — they are what a CLI user reads.

:class:`QwenProgressTranslator`
    Folds decoded payloads into Stage 5 ``phase="qwen"`` events.

:func:`stream_worker_process`
    Runs the worker with ``Popen``, drains stdout **and** stderr on separate threads, and preserves
    the timeout / return-code / bounded-stderr semantics ``subprocess.run`` gave us.

Scope note: ``stage5_qwen_scene_worker.py`` already used ``subprocess.Popen`` before Phase 2B to
manage its internal ``llama-server``. That is the *child's* business and is untouched. The boundary
this module changes is the parent → Python-worker one, and only for the two long-running launches
(``_run_qwen_worker_batch``, ``_run_qwen_worker``). The short ``llama-mtmd-cli --version`` probe
stays a plain ``subprocess.run``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Sequence

from beatsync_fork.progress import (
    ProgressEvent,
    format_duration,
    format_rate,
    state,
)

PROTOCOL_VERSION = "beatsync.qwen-progress/1"
"""Bumped only on an incompatible wire change. Unknown versions are still parsed: the reader keys off
``kind`` and ignores fields it does not know, so an older parent never crashes on a newer worker."""

PROTOCOL_PREFIX = "BEATSYNC_QWEN_PROGRESS\t"
"""Namespace marker. A tab separator keeps the payload trivially splittable and cannot appear inside
``json.dumps`` output, which escapes control characters."""

KIND_JOB_START = "job_start"
KIND_JOB_PROGRESS = "job_progress"
KIND_JOB_END = "job_end"
KIND_WORKER_STATE = "worker_state"

QWEN_PHASE = "qwen"
"""Stage 5 subphase these events belong to. Matches the Phase 2A key, so the deterministic source
counter and the Qwen stream stay separate counters that never contaminate each other."""

RATE_UNIT = "candidates"
"""What Qwen throughput actually measures. Stage 5's *source* counter is a different quantity with a
different rate; labelling both ``sources/s`` was the Phase 2A-R3 bug, and labelling per-candidate
inference ``sources/s`` would be the same lie in the other direction."""

MAX_STATE_MESSAGE = 240
"""Worker-supplied text is truncated: the panel is a status line, not a log sink."""


# ---------------------------------------------------------------------------
# wire protocol
# ---------------------------------------------------------------------------


def encode(kind: str, **fields: Any) -> str:
    """Serialise one protocol line (no trailing newline).

    Guaranteed single-line and ASCII-safe: ``json.dumps`` escapes newlines inside strings and
    ``ensure_ascii`` keeps the payload valid under any console code page, which matters because the
    worker's stdout is read as UTF-8 text with ``errors="replace"``.
    """
    payload: Dict[str, Any] = {"v": PROTOCOL_VERSION, "kind": str(kind)}
    payload.update(fields)
    return PROTOCOL_PREFIX + json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def is_protocol_line(line: str) -> bool:
    """True for machine lines. Everything else is ordinary worker stdout and stays ordinary."""
    return isinstance(line, str) and line.startswith(PROTOCOL_PREFIX)


def decode(line: str) -> Dict[str, Any] | None:
    """Parse one protocol line, or return ``None``.

    ``None`` means "nothing usable here" for every failure mode — not a protocol line, truncated
    JSON, a JSON scalar instead of an object, a missing ``kind``. A malformed machine line must never
    be able to abort a semantic tagging run that has already cost minutes of GPU time.
    """
    if not is_protocol_line(line):
        return None
    raw = line[len(PROTOCOL_PREFIX):].strip()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    return payload


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return result


def _as_text(value: Any, limit: int = MAX_STATE_MESSAGE) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


# ---------------------------------------------------------------------------
# translation into Phase 2A events
# ---------------------------------------------------------------------------


@dataclass
class _JobState:
    job_index: int | None = None
    job_total: int | None = None
    job_id: str = ""
    source_name: str = ""
    requested: int | None = None


class QwenProgressTranslator:
    """Turns decoded protocol payloads into Stage 5 ``phase="qwen"`` events.

    Deliberately emits **uncounted** ``STATE`` events rather than counted ``PROGRESS`` events:

    * The only denominator the worker can prove is the current job's ``len(frame_items)``, which it
      knows after frame prefetch. A cross-job total is not available before the run — the parent's
      requested candidate counts are an upper bound, not the live denominator — so a global
      ``1840 / 3620`` would be invented. ``ProgressView`` is not bent into a false percentage for
      visual symmetry; §4 of the Phase 2B brief says truth beats prettier presentation.
    * A counted phase would also fight ``ProgressView``'s per-``(stage, phase)`` monotonicity: job 18
      restarting at ``1 / 130`` after job 17 reached ``120 / 120`` looks exactly like the stale
      straggler that rule exists to reject, and the panel would freeze on the old job.
    * Staying uncounted keeps the deterministic Stage 5 history line (``· 758 sources completed``)
      visible, because ``ProgressView`` shows it precisely when the active phase has no counter.

    The per-job numbers are not lost: they are rendered into the message *and* carried as structured
    fields in ``event.data`` for any other consumer.
    """

    def __init__(
        self,
        stage: int = 5,
        phase: str = QWEN_PHASE,
        min_interval: float = 0.35,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stage = int(stage)
        self.phase = phase
        self.min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._job = _JobState()
        self._last_emit = 0.0
        self._started = False
        self._first_progress_pending = False
        """A job's first progress update is never throttled: it is the one that replaces "starting"
        with a real count, and the throttle window would otherwise swallow it — the worker emits its
        first wave result well inside 350ms of the job beginning."""
        self.malformed_lines = 0
        self.unknown_kinds = 0
        self.candidates_done = 0
        """Candidates actually tagged across *finished* jobs — worker-reported, never estimated."""

    # -- ingestion ------------------------------------------------------

    def handle(self, line: str) -> ProgressEvent | None:
        """Consume one stdout line. Returns an event, or ``None`` when there is nothing to say.

        ``None`` is the normal throttled case, so ``emit(callback, translator.handle(line))`` is the
        intended call shape — :func:`beatsync_fork.progress.emit` ignores a ``None`` event.
        """
        payload = decode(line)
        if payload is None:
            if is_protocol_line(line):
                # Namespaced but unusable. Counted for diagnostics, never raised, never printed as
                # if it were worker prose.
                self.malformed_lines += 1
            return None
        return self.handle_payload(payload)

    def handle_payload(self, payload: Mapping[str, Any]) -> ProgressEvent | None:
        kind = payload.get("kind")
        self._absorb_job_fields(payload)
        if kind == KIND_JOB_START:
            return self._job_start(payload)
        if kind == KIND_JOB_PROGRESS:
            return self._job_progress(payload)
        if kind == KIND_JOB_END:
            return self._job_end(payload)
        if kind == KIND_WORKER_STATE:
            return self._worker_state(payload)
        # A worker newer than this parent may emit kinds we have no rendering for. Ignoring them is
        # correct; crashing Stage 5 over an unrecognised status line is not.
        self.unknown_kinds += 1
        return None

    # -- helpers --------------------------------------------------------

    def _absorb_job_fields(self, payload: Mapping[str, Any]) -> None:
        job_index = _as_int(payload.get("job_index"))
        if job_index is not None:
            self._job.job_index = job_index
        job_total = _as_int(payload.get("job_total"))
        if job_total is not None:
            self._job.job_total = job_total
        job_id = _as_text(payload.get("job_id"), 64)
        if job_id:
            self._job.job_id = job_id
        source_name = _as_text(payload.get("source_name"), 120)
        if source_name:
            self._job.source_name = source_name

    def _job_prefix(self) -> str:
        if self._job.job_index is None:
            return "Qwen"
        if self._job.job_total:
            return f"Qwen job {self._job.job_index} / {self._job.job_total}"
        return f"Qwen job {self._job.job_index}"

    def _base_data(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"phase": self.phase, "qwen_live": True}
        if self._job.job_index is not None:
            data["job_index"] = self._job.job_index
        if self._job.job_total is not None:
            data["job_total"] = self._job.job_total
        if self._job.job_id:
            data["job_id"] = self._job.job_id
        if self._job.source_name:
            data["source_name"] = self._job.source_name
        return data

    def _throttled(self, force: bool) -> bool:
        now = self._clock()
        if force or not self._started or self.min_interval <= 0.0:
            self._last_emit = now
            self._started = True
            return False
        if now - self._last_emit < self.min_interval:
            return True
        self._last_emit = now
        return False

    # -- kinds ----------------------------------------------------------

    def _job_start(self, payload: Mapping[str, Any]) -> ProgressEvent:
        requested = _as_int(payload.get("requested_candidate_count"))
        self._job.requested = requested
        data = self._base_data()
        if requested is not None:
            data["requested_candidate_count"] = requested
        parts = [self._job_prefix()]
        if self._job.source_name:
            parts.append(self._job.source_name)
        # "requested" is honest wording: the live denominator is whatever prefetch actually decoded,
        # which the worker only knows a moment later.
        parts.append(
            f"starting ({requested} candidates requested)" if requested is not None else "starting"
        )
        self._throttled(force=True)
        self._first_progress_pending = True
        return state(self.stage, " · ".join(parts), **data)

    def _job_progress(self, payload: Mapping[str, Any]) -> ProgressEvent | None:
        current = _as_int(payload.get("current"))
        total = _as_int(payload.get("total"))
        rate = _as_float(payload.get("candidates_per_second"))
        batch_size = _as_int(payload.get("batch_size"))
        final = current is not None and total is not None and total > 0 and current >= total
        if self._throttled(force=bool(final) or self._first_progress_pending):
            return None
        self._first_progress_pending = False

        data = self._base_data()
        if current is not None:
            data["current"] = current
        if total is not None:
            data["total"] = total
        if rate is not None:
            data["rate"] = rate
            data["rate_unit"] = RATE_UNIT
        if batch_size is not None:
            data["batch_size"] = batch_size
        data["unit"] = RATE_UNIT

        parts = [self._job_prefix()]
        if current is not None and total:
            percent = 100.0 * current / total
            parts.append(f"{current} / {total} candidates ({percent:.1f}%)")
        elif current is not None:
            parts.append(f"{current} candidates")
        rate_text = format_rate(rate, RATE_UNIT)
        if rate_text:
            parts.append(rate_text)
        if batch_size:
            parts.append(f"batch {batch_size}")
        if self._job.source_name:
            parts.append(self._job.source_name)
        return state(self.stage, " · ".join(parts), **data)

    def _job_end(self, payload: Mapping[str, Any]) -> ProgressEvent:
        frame_count = _as_int(payload.get("frame_count"))
        tag_count = _as_int(payload.get("tag_count"))
        inference_seconds = _as_float(payload.get("inference_seconds"))
        if tag_count is not None:
            self.candidates_done += max(0, tag_count)

        data = self._base_data()
        if frame_count is not None:
            data["frame_count"] = frame_count
        if tag_count is not None:
            data["tag_count"] = tag_count
        if inference_seconds is not None:
            data["inference_seconds"] = inference_seconds
        data["candidates_done"] = self.candidates_done

        parts = [self._job_prefix()]
        if tag_count is not None and frame_count is not None:
            parts.append(f"{tag_count} / {frame_count} tagged")
        elif tag_count is not None:
            parts.append(f"{tag_count} tagged")
        elapsed = format_duration(inference_seconds)
        if elapsed:
            parts.append(f"inference {elapsed}")
        if self._job.source_name:
            parts.append(self._job.source_name)
        self._throttled(force=True)
        return state(self.stage, " · ".join(parts), **data)

    def _worker_state(self, payload: Mapping[str, Any]) -> ProgressEvent | None:
        message = _as_text(payload.get("message"))
        name = _as_text(payload.get("state"), 64)
        if not message and not name:
            return None
        if self._throttled(force=True):  # pragma: no cover - force never throttles
            return None
        data = self._base_data()
        if name:
            data["worker_state"] = name
        for key in ("batch_size", "slots"):
            value = _as_int(payload.get(key))
            if value is not None:
                data[key] = value
        device = _as_text(payload.get("device"), 120)
        if device:
            data["device"] = device
        parts = [self._job_prefix() if self._job.job_index is not None else "Qwen"]
        parts.append(message or name)
        return state(self.stage, " · ".join(parts), **data)


# ---------------------------------------------------------------------------
# streaming process runner
# ---------------------------------------------------------------------------


class _BoundedTail:
    """Keeps at most ``limit`` characters from the end of a stream.

    The parent only ever printed ``stderr.strip()[-2400:]``, so retaining more would be dead weight —
    and a worker that fails inside llama.cpp can produce megabytes of Vulkan diagnostics. Trimming as
    we read keeps memory flat regardless of how loudly the child fails.
    """

    __slots__ = ("_limit", "_chunks", "_length", "_lock")

    def __init__(self, limit: int) -> None:
        self._limit = max(0, int(limit))
        self._chunks: list[str] = []
        self._length = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        if not text or self._limit <= 0:
            return
        with self._lock:
            self._chunks.append(text)
            self._length += len(text)
            while self._length > self._limit and len(self._chunks) > 1:
                dropped = self._chunks.pop(0)
                self._length -= len(dropped)
            if self._length > self._limit:
                head = self._chunks[0]
                self._chunks[0] = head[self._length - self._limit:]
                self._length = self._limit

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)


@dataclass(frozen=True)
class WorkerOutcome:
    """What happened to the worker process. Mirrors what ``subprocess.run`` gave the old code."""

    returncode: int | None = None
    timed_out: bool = False
    killed: bool = False
    launch_error: str = ""
    stderr_tail: str = ""
    stdout_lines: int = 0
    protocol_lines: int = 0
    seconds: float = 0.0
    reader_threads_alive: int = 0

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.launch_error
            and self.returncode == 0
        )


def _drain(stream, handler: Callable[[str], None]) -> None:
    """Read one pipe to EOF, one line at a time, never letting a handler error stop the drain.

    A handler exception here would leave the pipe unread — which is the deadlock this whole design
    exists to avoid — so it is swallowed per line. ``KeyboardInterrupt``/``SystemExit`` still
    propagate and end the thread.
    """
    try:
        while True:
            try:
                line = stream.readline()
            except (ValueError, OSError):
                break
            if not line:
                break
            try:
                handler(line)
            except Exception:
                pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def stream_worker_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: float | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    stderr_char_limit: int = 2400,
    terminate_grace: float = 5.0,
    reader_join_timeout: float = 10.0,
    clock: Callable[[], float] = time.perf_counter,
) -> WorkerOutcome:
    """Run a child process, streaming its stdout lines while they happen.

    Invocation semantics are kept identical to the ``subprocess.run(capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=…, check=False, env=…)`` call this replaces: same
    argv, same environment, same UTF-8-with-replacement decoding, same "never raise on a non-zero
    exit" contract, same bounded-stderr diagnostics, same "timeout is not an exception the caller
    has to handle" outcome. No ``shell=True``, and no new ``creationflags``.

    Both pipes are drained by their own thread while the main thread owns ``wait(timeout=…)``. That
    ordering is the point: reading stdout inline and stderr afterwards deadlocks the moment a failing
    llama.cpp run fills the stderr pipe buffer, which is exactly when the diagnostics matter.

    ``on_stdout_line`` receives each line with its trailing newline stripped, from the reader thread.
    Handing it ``queue.put`` (or :func:`beatsync_fork.progress.emit`, which cannot raise) is safe; a
    Gradio component must never be touched from here — that is the generator's job, as in Phase 2A.
    """
    started = clock()
    counters = {"stdout": 0, "protocol": 0}
    tail = _BoundedTail(stderr_char_limit)

    def handle_stdout(raw: str) -> None:
        line = raw.rstrip("\r\n")
        counters["stdout"] += 1
        if is_protocol_line(line):
            counters["protocol"] += 1
        if on_stdout_line is not None:
            on_stdout_line(line)

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, never shell
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=dict(env) if env is not None else None,
            cwd=cwd,
        )
    except Exception as exc:
        return WorkerOutcome(
            launch_error=f"{type(exc).__name__}: {exc}",
            seconds=clock() - started,
        )

    pipes = [(process.stdout, handle_stdout), (process.stderr, tail.append)]
    readers = [
        threading.Thread(target=_drain, args=(pipe, handler), name=f"qwen-worker-{index}",
                         daemon=True)
        for index, (pipe, handler) in enumerate(pipes)
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    killed = False
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Same guarantee subprocess.run(timeout=…) gave: the caller never waits forever. Terminate
        # first, escalate only if the worker ignores it.
        try:
            process.terminate()
        except Exception:
            pass
        try:
            returncode = process.wait(timeout=max(0.0, terminate_grace))
        except subprocess.TimeoutExpired:
            killed = True
            try:
                process.kill()
            except Exception:
                pass
            try:
                returncode = process.wait(timeout=max(0.0, terminate_grace))
            except subprocess.TimeoutExpired:
                returncode = process.poll()
    finally:
        # Join against ONE shared deadline, not a per-thread bound. A surviving grandchild (the
        # worker's llama-server inherits these pipe handles) keeps both pipes open after the direct
        # child is gone, so two sequential 10s joins made a 2s timeout take 20s to return — measured,
        # not theorised. The threads are daemons, so a reader still blocked at the deadline can never
        # hold up interpreter exit either.
        join_deadline = clock() + max(0.0, reader_join_timeout)
        for reader in readers:
            reader.join(timeout=max(0.0, join_deadline - clock()))
        # Each reader closes its OWN pipe (see `_drain`), and that ownership is load-bearing rather
        # than tidy: closing a pipe from here while its reader is blocked inside `readline()` waits on
        # the buffer's internal lock, which turned a 2s timeout into a 120s return whenever a
        # surviving grandchild held the write end open. Measured. So only ever close a pipe whose
        # reader has already finished — a still-running daemon reader will close it at EOF.
        for reader, (pipe, _handler) in zip(readers, pipes):
            if pipe is None or reader.is_alive():
                continue
            try:
                pipe.close()
            except Exception:
                pass

    return WorkerOutcome(
        returncode=returncode,
        timed_out=timed_out,
        killed=killed,
        stderr_tail=tail.text().strip()[-stderr_char_limit:] if stderr_char_limit > 0 else "",
        stdout_lines=counters["stdout"],
        protocol_lines=counters["protocol"],
        seconds=clock() - started,
        reader_threads_alive=sum(1 for reader in readers if reader.is_alive()),
    )


@dataclass(frozen=True)
class QwenWorkerResult:
    """Streaming-worker result in the shape the two Qwen call sites need."""

    response: Dict[str, Any] = field(default_factory=dict)
    outcome: WorkerOutcome = field(default_factory=WorkerOutcome)
    response_error: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome.ok and not self.response_error


def run_qwen_worker(
    argv: Sequence[str],
    response_path: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    event_callback: Callable[[ProgressEvent], None] | None = None,
    on_human_line: Callable[[str], None] | None = None,
    translator: QwenProgressTranslator | None = None,
    stderr_char_limit: int = 2400,
) -> QwenWorkerResult:
    """Launch the Qwen worker, stream its progress, then load its response JSON.

    The response JSON remains the **only** source of semantic truth: nothing is reconstructed from
    stdout, and no tag is ever parsed out of a progress line. The protocol is observability, so a run
    with ``event_callback=None`` must return exactly what the old capture path returned.
    """
    from beatsync_fork.progress import emit as emit_event

    active = translator if translator is not None else QwenProgressTranslator()

    def handle_line(line: str) -> None:
        if is_protocol_line(line):
            emit_event(event_callback, active.handle(line))
            return
        if on_human_line is not None and line.strip():
            on_human_line(line)

    outcome = stream_worker_process(
        argv,
        env=env,
        timeout=timeout,
        on_stdout_line=handle_line,
        stderr_char_limit=stderr_char_limit,
    )
    if not outcome.ok:
        return QwenWorkerResult(response={}, outcome=outcome)

    try:
        with open(response_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        return QwenWorkerResult(
            response={}, outcome=outcome, response_error=f"{type(exc).__name__}: {exc}"
        )
    if not isinstance(data, dict):
        # The old code silently collapsed a non-object payload to {}. The value returned to the
        # caller is unchanged; naming it means the run is no longer reported as a success.
        return QwenWorkerResult(
            response={}, outcome=outcome,
            response_error=f"response JSON is not an object ({type(data).__name__})",
        )
    return QwenWorkerResult(response=data, outcome=outcome)


__all__ = [
    "KIND_JOB_END",
    "KIND_JOB_PROGRESS",
    "KIND_JOB_START",
    "KIND_WORKER_STATE",
    "MAX_STATE_MESSAGE",
    "PROTOCOL_PREFIX",
    "PROTOCOL_VERSION",
    "QWEN_PHASE",
    "QwenProgressTranslator",
    "QwenWorkerResult",
    "RATE_UNIT",
    "WorkerOutcome",
    "decode",
    "encode",
    "is_protocol_line",
    "run_qwen_worker",
    "stream_worker_process",
]
