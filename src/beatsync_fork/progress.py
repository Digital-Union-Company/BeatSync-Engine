#!/usr/bin/env python3
"""Structured pipeline progress events.

Why this replaces status strings
-------------------------------
Before this module the GUI learned the current stage by regex-matching a sentence the pipeline had
printed (``re.search(r"Stage (\\d+) is processing", message)``), and everything the pipeline actually
knew — how many sources were analysed, how many clips were rendered, how fast — went to ``stdout``,
which ``gui.py`` redirects into a discarding ``QuietConsole``. A multi-hour Stage 5 looked identical to
a hung process.

An event carries the numbers instead of a sentence, so the UI formats them and nothing has to be
re-parsed out of prose.

Design constraints
------------------
* **Stdlib only.** No Gradio, NumPy, cupy, cv2, librosa or upstream module may be imported here (the
  hard rule in CLAUDE.md), so the whole progress core is testable on a bare interpreter.
* **Emission can never break a render.** :func:`emit` swallows every exception a callback raises. A
  broken status widget must not lose hours of analysis.
* **No global bus.** Callers pass a callback; nothing is registered globally, so concurrent runs and
  tests cannot interfere with each other.
* **Thread-safe by construction.** Events are frozen and the recommended sink is a ``queue.Queue``,
  which is already how ``gui.py`` gets data out of its worker thread. Stages 5 and 6 emit from worker
  threads, so nothing here may hold mutable shared state.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Mapping

PROGRESS_SCHEMA = "beatsync.progress/1"
"""Schema tag carried in :meth:`ProgressEvent.as_dict`, so a later phase that persists events into a
run manifest can tell versions apart."""


class Stage(int, Enum):
    """The pipeline stages a user sees. ``INPUT`` covers pre-Stage-1 source work."""

    INPUT = 0
    AUDIO_BEATS = 1
    FEATURES = 2
    SECTIONS = 3
    CUT_SELECTION = 4
    VIDEO_ANALYSIS = 5
    RENDER = 6


STAGE_TITLES: Mapping[int, str] = {
    Stage.INPUT: "Input",
    Stage.AUDIO_BEATS: "Beat Grid",
    Stage.FEATURES: "Energy & Rhythm",
    Stage.SECTIONS: "Sections",
    Stage.CUT_SELECTION: "Cut Selection",
    Stage.VIDEO_ANALYSIS: "Video Analysis",
    Stage.RENDER: "Rendering",
}


class EventKind(str, Enum):
    """What an event says. Deliberately small."""

    START = "start"
    PROGRESS = "progress"
    METRIC = "metric"
    STATE = "state"
    WARNING = "warning"
    ERROR = "error"
    END = "end"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """One immutable observation from the pipeline.

    ``current``/``total`` are only meaningful for :attr:`EventKind.PROGRESS` (and the final
    :attr:`EventKind.END` of a counted stage). Everything else leaves them ``None`` rather than
    inventing a denominator.

    No ETA field exists on purpose. Stages 1-4 finish in seconds, so an ETA there would be noise, and
    Stage 5's deterministic pass has per-video costs that vary with clip duration — a projected finish
    time would be a guess presented as a fact. Rendering does expose a measured ``rate``, which the UI
    can show without claiming to predict the end.
    """

    stage: int
    kind: EventKind
    message: str = ""
    current: int | None = None
    total: int | None = None
    elapsed_seconds: float | None = None
    rate: float | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    @property
    def stage_title(self) -> str:
        return STAGE_TITLES.get(self.stage, f"Stage {self.stage}")

    @property
    def percent(self) -> float | None:
        """Completion percentage, or ``None`` when this event is not counted."""
        if self.current is None or not self.total:
            return None
        return max(0.0, min(100.0, 100.0 * self.current / self.total))

    def is_counted(self) -> bool:
        return self.current is not None and self.total is not None

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly form. ``kind`` becomes its string value; ``data`` is copied."""
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["data"] = dict(self.data)
        payload["schema"] = PROGRESS_SCHEMA
        payload["stage_title"] = self.stage_title
        return payload

    def with_elapsed(self, elapsed_seconds: float) -> "ProgressEvent":
        return replace(self, elapsed_seconds=float(elapsed_seconds))


ProgressCallback = Callable[[ProgressEvent], None]


def emit(callback: ProgressCallback | None, event: ProgressEvent | None) -> None:
    """Deliver one event, swallowing anything the callback does wrong.

    Observability must never be able to fail a render: a closed queue, a disconnected browser or a bug
    in a formatter is strictly less important than the hours of analysis in flight. ``BaseException``
    is deliberately *not* caught, so ``KeyboardInterrupt`` and ``SystemExit`` still propagate.

    A ``None`` event is ignored, which is what makes the natural call-site idiom safe::

        emit(callback, counter.advance())   # advance() returns None when throttled

    Without this, every throttled advance would deliver ``None`` to the consumer.
    """
    if callback is None or event is None:
        return
    try:
        callback(event)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Constructors — keep call sites in the pipeline to one readable line
# ---------------------------------------------------------------------------


def start(stage: int, message: str = "", current: int | None = None,
          total: int | None = None, **data: Any) -> ProgressEvent:
    """Begin a stage.

    ``current``/``total`` are accepted as real fields (not ``data``) so a counted stage can publish
    its denominator immediately — the UI shows ``0 / 758`` from the first event instead of waiting for
    the first completion to learn how much work there is.
    """
    return ProgressEvent(
        stage=int(stage),
        kind=EventKind.START,
        message=message,
        current=current,
        total=total,
        data=dict(data),
    )


def progress(
    stage: int,
    current: int,
    total: int,
    message: str = "",
    elapsed_seconds: float | None = None,
    rate: float | None = None,
    **data: Any,
) -> ProgressEvent:
    """A counted step. ``current`` is clamped into ``[0, total]``.

    Clamping is defensive on purpose: a retry or a double-count upstream should show a slightly stale
    number, never ``761/758``, which would make the whole panel look untrustworthy.
    """
    total_int = max(0, int(total))
    current_int = max(0, min(int(current), total_int))
    return ProgressEvent(
        stage=int(stage),
        kind=EventKind.PROGRESS,
        message=message,
        current=current_int,
        total=total_int,
        elapsed_seconds=elapsed_seconds,
        rate=rate,
        data=dict(data),
    )


def metric(stage: int, message: str, **data: Any) -> ProgressEvent:
    return ProgressEvent(stage=int(stage), kind=EventKind.METRIC, message=message, data=dict(data))


def state(stage: int, message: str, **data: Any) -> ProgressEvent:
    """A named phase inside a stage that has no meaningful counter (e.g. final assembly)."""
    return ProgressEvent(stage=int(stage), kind=EventKind.STATE, message=message, data=dict(data))


def warning(stage: int, message: str, **data: Any) -> ProgressEvent:
    return ProgressEvent(stage=int(stage), kind=EventKind.WARNING, message=message, data=dict(data))


def error(stage: int, message: str, **data: Any) -> ProgressEvent:
    return ProgressEvent(stage=int(stage), kind=EventKind.ERROR, message=message, data=dict(data))


def end(
    stage: int,
    message: str = "",
    current: int | None = None,
    total: int | None = None,
    elapsed_seconds: float | None = None,
    **data: Any,
) -> ProgressEvent:
    return ProgressEvent(
        stage=int(stage),
        kind=EventKind.END,
        message=message,
        current=current,
        total=total,
        elapsed_seconds=elapsed_seconds,
        data=dict(data),
    )


# ---------------------------------------------------------------------------
# Counted-stage helper
# ---------------------------------------------------------------------------


class StageCounter:
    """Monotonic counter for a counted stage, with update throttling.

    Two jobs, both learned from the shapes Stage 5 and 6 actually have:

    * **Monotonic and bounded.** Stage 5 retries a failed parallel video serially and Stage 6 collects
      clips out of order via ``as_completed``; neither may be able to make the displayed count go
      backwards or exceed the total.
    * **Throttled.** 1216 clips would mean 1216 UI updates. :meth:`advance` returns an event only when
      the interval has elapsed, unless it is the final one — the last update is always delivered, so
      the panel cannot come to rest on a stale number.
    """

    __slots__ = ("stage", "total", "_current", "_started", "_last_emit", "min_interval")

    def __init__(self, stage: int, total: int, min_interval: float = 0.5,
                 started: float | None = None, initial: int = 0) -> None:
        self.stage = int(stage)
        self.total = max(0, int(total))
        self.min_interval = max(0.0, float(min_interval))
        self._current = max(0, min(int(initial), self.total))
        self._started = time.perf_counter() if started is None else float(started)
        # Seed so the first advance always emits.
        self._last_emit = float("-inf")

    @property
    def current(self) -> int:
        return self._current

    @property
    def elapsed(self) -> float:
        return max(0.0, time.perf_counter() - self._started)

    @property
    def rate(self) -> float | None:
        elapsed = self.elapsed
        if elapsed <= 0.0 or self._current <= 0:
            return None
        return self._current / elapsed

    def set_current(self, value: int) -> None:
        """Move the counter forward only. Regressions are ignored, not applied."""
        self._current = max(self._current, max(0, min(int(value), self.total)))

    def advance(self, step: int = 1, message: str = "", force: bool = False,
                **data: Any) -> ProgressEvent | None:
        """Add ``step`` completions; return an event if one should be shown now.

        Returns ``None`` when throttled. The caller emits only what it gets back, so a throttled
        advance still updates the internal count.
        """
        self.set_current(self._current + max(0, int(step)))
        now = time.perf_counter()
        is_final = self._current >= self.total
        if not (force or is_final or (now - self._last_emit) >= self.min_interval):
            return None
        self._last_emit = now
        return progress(
            self.stage,
            self._current,
            self.total,
            message=message,
            elapsed_seconds=self.elapsed,
            rate=self.rate,
            **data,
        )

    def snapshot(self, message: str = "", **data: Any) -> ProgressEvent:
        """An unthrottled event for the current count (used for the final state)."""
        return progress(
            self.stage,
            self._current,
            self.total,
            message=message,
            elapsed_seconds=self.elapsed,
            rate=self.rate,
            **data,
        )


# ---------------------------------------------------------------------------
# Formatting (used by the UI layer; kept here so it is testable without Gradio)
# ---------------------------------------------------------------------------


def format_duration(seconds: float | None) -> str:
    """``2m 07s`` / ``45s`` / ``1h 02m``. Returns ``""`` for ``None``."""
    if seconds is None:
        return ""
    total = int(max(0.0, float(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_rate(rate: float | None, unit: str = "items") -> str:
    if rate is None or rate <= 0:
        return ""
    return f"{rate:.1f} {unit}/s"


def format_event(event: ProgressEvent) -> str:
    """One concise human line for a single event."""
    head = f"Stage {event.stage} — {event.stage_title}"
    if event.kind is EventKind.WARNING:
        return f"⚠️ {head}: {event.message}"
    if event.kind is EventKind.ERROR:
        return f"❌ {head}: {event.message}"

    parts: list[str] = []
    if event.is_counted():
        percent = event.percent
        counted = f"{event.current} / {event.total}"
        if percent is not None and event.total:
            counted += f" ({percent:.1f}%)"
        parts.append(counted)
    if event.message:
        parts.append(event.message)
    rate_text = format_rate(event.rate)
    if rate_text:
        parts.append(rate_text)
    elapsed_text = format_duration(event.elapsed_seconds)
    if elapsed_text:
        parts.append(f"elapsed {elapsed_text}")

    return head if not parts else f"{head}: " + " · ".join(parts)


__all__ = [
    "PROGRESS_SCHEMA",
    "STAGE_TITLES",
    "EventKind",
    "ProgressCallback",
    "ProgressEvent",
    "Stage",
    "StageCounter",
    "emit",
    "end",
    "error",
    "format_duration",
    "format_event",
    "format_rate",
    "metric",
    "progress",
    "start",
    "state",
    "warning",
]
