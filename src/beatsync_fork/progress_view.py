#!/usr/bin/env python3
"""Turns a stream of :class:`~beatsync_fork.progress.ProgressEvent` into status text.

Separated from :mod:`beatsync_fork.progress` so the pipeline side (emitting) and the UI side
(accumulating and rendering) can be tested independently, and so ``gui.py`` stays a wiring layer with
no formatting logic of its own.

Counted subphases
-----------------
A stage number is not a counter. Stage 6's ProRes path counts **sources** while converting and
**clips** while extracting, with different denominators, and then runs an assembly phase with no
counter at all; Stage 5 counts sources and then runs Qwen, which has no counter in Phase 2A. So
monotonicity is enforced per ``(stage, phase)``, never per stage:

* within a phase, a straggling lower event from another worker thread cannot rewind the display;
* across phases, nothing carries over — not the count, not the total, not the unit, not the rate,
  not the elapsed time.

Merging them produced impossible output such as ``758 / 100 (758%)``, and an extraction that appeared
to start 62% finished.

The phase comes from ``event.data["phase"]``; events without one belong to the stage's main counter
(the standard clip render, the Stage 5 deterministic pass, and the stage's own start/end).

Stdlib only — no Gradio — which is what makes the GUI seam testable without starting a web server.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from beatsync_fork.progress import (
    EventKind,
    ProgressEvent,
    format_duration,
    format_rate,
)

MAX_NOTICES = 6
"""How many warnings/errors to keep. Bounded on purpose: the point is to surface that something went
wrong and what, not to stream FFmpeg's stderr into a textbox."""

MAIN_PHASE = None
"""Key for events that carry no ``phase``: the stage's own counter."""


@dataclass
class _PhaseView:
    """One counted (or uncounted) subphase of a stage."""

    key: str | None
    unit: str = "items"
    current: int | None = None
    total: int | None = None
    elapsed_seconds: float | None = None
    rate: float | None = None
    last_message: str = ""

    def counted(self) -> bool:
        return self.current is not None and self.total is not None

    def percent(self) -> float | None:
        if not self.counted() or not self.total:
            return None
        return 100.0 * self.current / self.total

    def line(self) -> str:
        parts: list[str] = []
        if self.counted():
            text = f"{self.current} / {self.total}"
            percent = self.percent()
            if percent is not None:
                text += f" ({percent:.1f}%)"
            parts.append(text)
        if self.last_message:
            parts.append(self.last_message)
        rate = format_rate(self.rate, self.unit)
        if rate:
            parts.append(rate)
        elapsed = format_duration(self.elapsed_seconds)
        if elapsed:
            parts.append(f"elapsed {elapsed}")
        return " · ".join(parts)

    def completed_summary(self) -> str:
        """Short history form, deliberately *not* ``N / N`` and with no percentage.

        Used when an uncounted phase is active: the finished work stays visible without the running
        phase appearing to be complete.
        """
        if not self.counted():
            return ""
        return f"{self.current} {self.unit} completed"


@dataclass
class _StageView:
    """Accumulated state for one stage, keyed by subphase."""

    stage: int
    title: str
    started: bool = False
    finished: bool = False
    phases: dict[str | None, _PhaseView] = field(default_factory=dict)
    phase_order: list[str | None] = field(default_factory=list)
    active_phase: str | None = MAIN_PHASE
    metrics: list[str] = field(default_factory=list)
    end_message: str = ""
    end_elapsed: float | None = None

    def phase(self, key: str | None) -> _PhaseView:
        view = self.phases.get(key)
        if view is None:
            view = _PhaseView(key=key)
            self.phases[key] = view
            self.phase_order.append(key)
        return view

    def active(self) -> _PhaseView:
        return self.phase(self.active_phase)

    def last_counted_phase(self, exclude: str | None) -> _PhaseView | None:
        """Most recently seen counted phase other than ``exclude``."""
        for key in reversed(self.phase_order):
            if key == exclude:
                continue
            candidate = self.phases[key]
            if candidate.counted():
                return candidate
        return None


class ProgressView:
    """Accumulates events and renders the status panel.

    Stage identity comes from ``event.stage`` — an integer on the event — so nothing here parses
    sentences. That is the point of Phase 2A: the old path recovered the stage number by regex-matching
    ``"Stage 5 is processing"`` out of prose.
    """

    def __init__(self, max_notices: int = MAX_NOTICES) -> None:
        self._stages: dict[int, _StageView] = {}
        self._order: list[int] = []
        self._notices: list[str] = []
        self._max_notices = max(1, int(max_notices))
        self._total_elapsed: float | None = None

    # -- ingestion ---------------------------------------------------------

    def apply(self, event: ProgressEvent) -> None:
        """Fold one event into the view. Unknown kinds are ignored rather than raising."""
        stage = self._stages.get(event.stage)
        if stage is None:
            stage = _StageView(stage=event.stage, title=event.stage_title)
            self._stages[event.stage] = stage
            self._order.append(event.stage)

        phase_key = event.data.get("phase", MAIN_PHASE)

        # Metrics and notices are stage-level: they must not move the active phase, or a metric line
        # emitted between phases would silently reset what the panel is showing.
        if event.kind is EventKind.METRIC:
            if event.message:
                stage.metrics.append(event.message)
            return
        if event.kind in (EventKind.WARNING, EventKind.ERROR):
            prefix = "⚠️" if event.kind is EventKind.WARNING else "❌"
            self._notices.append(f"{prefix} Stage {event.stage}: {event.message}")
            del self._notices[: max(0, len(self._notices) - self._max_notices)]
            return

        phase = stage.phase(phase_key)
        stage.active_phase = phase_key

        unit = event.data.get("unit")
        if isinstance(unit, str) and unit:
            phase.unit = unit

        if event.kind is EventKind.START:
            stage.started = True
            stage.finished = False
            if event.message:
                phase.last_message = event.message
            if event.total is not None:
                phase.total = event.total
                phase.current = event.current if event.current is not None else 0

        elif event.kind is EventKind.PROGRESS:
            stage.started = True
            # Monotonic *within this phase only*. A late lower event from another worker thread must
            # not rewind the display, and a previous phase's larger count must not leak in here.
            if event.current is not None:
                phase.current = (
                    event.current if phase.current is None else max(phase.current, event.current)
                )
            if event.total is not None:
                phase.total = event.total
            phase.elapsed_seconds = event.elapsed_seconds
            phase.rate = event.rate
            if event.message:
                phase.last_message = event.message

        elif event.kind is EventKind.STATE:
            stage.started = True
            if event.message:
                phase.last_message = event.message

        elif event.kind is EventKind.END:
            stage.finished = True
            if event.current is not None:
                phase.current = (
                    event.current if phase.current is None else max(phase.current, event.current)
                )
            if event.total is not None:
                phase.total = event.total
            if event.elapsed_seconds is not None:
                phase.elapsed_seconds = event.elapsed_seconds
                stage.end_elapsed = event.elapsed_seconds
            if event.message:
                phase.last_message = event.message
                stage.end_message = event.message
            if event.data.get("total_elapsed_seconds") is not None:
                self._total_elapsed = float(event.data["total_elapsed_seconds"])

    # -- queries -----------------------------------------------------------

    @property
    def notices(self) -> tuple[str, ...]:
        return tuple(self._notices)

    def active_stage(self) -> int | None:
        """Highest-numbered stage that has started and not finished; else the highest seen."""
        running = [s for s in self._order if self._stages[s].started and not self._stages[s].finished]
        if running:
            return max(running)
        return max(self._order) if self._order else None

    def active_phase(self, stage: int) -> str | None:
        view = self._stages.get(stage)
        return None if view is None else view.active_phase

    def stage_line(self, stage: int) -> str:
        """Detail line for a stage's **active phase**, or ``""`` if the stage is unknown."""
        view = self._stages.get(stage)
        return "" if view is None else view.active().line()

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        """The status panel: the active stage in detail, then finished stages, then notices."""
        if not self._order:
            return "Waiting to start…"

        active = self.active_stage()
        lines: list[str] = []

        if active is not None:
            view = self._stages[active]
            header = f"Stage {active} — {view.title}"
            lines.append(header if view.finished else f"{header}  (running)")

            detail = view.active().line()
            if detail:
                lines.append(f"  {detail}")

            # When the running phase has no counter (assembly, Qwen), show the finished work as
            # history instead of letting the previous counter masquerade as this phase's progress.
            if not view.active().counted():
                previous = view.last_counted_phase(exclude=view.active_phase)
                summary = previous.completed_summary() if previous else ""
                if summary:
                    lines.append(f"  · {summary}")

            for item in view.metrics[-3:]:
                lines.append(f"  · {item}")

        done = [s for s in sorted(self._order) if self._stages[s].finished and s != active]
        if done:
            lines.append("")
            lines.append("Completed:")
            for stage in done:
                view = self._stages[stage]
                elapsed = format_duration(view.end_elapsed)
                suffix = f" ({elapsed})" if elapsed else ""
                counted = view.last_counted_phase(exclude="__none__")
                summary = view.end_message or (
                    f"{counted.current}/{counted.total}" if counted else "done"
                )
                lines.append(f"  ✓ Stage {stage} {view.title}: {summary}{suffix}")

        if self._notices:
            lines.append("")
            lines.append("Notices:")
            lines.extend(f"  {n}" for n in self._notices)

        if self._total_elapsed is not None:
            lines.append("")
            lines.append(f"Total: {format_duration(self._total_elapsed)}")

        return "\n".join(lines)


__all__ = ["MAIN_PHASE", "MAX_NOTICES", "ProgressView"]
