#!/usr/bin/env python3
"""Turns a stream of :class:`~beatsync_fork.progress.ProgressEvent` into status text.

Separated from :mod:`beatsync_fork.progress` so the pipeline side (emitting) and the UI side
(accumulating and rendering) can be tested independently, and so ``gui.py`` stays a wiring layer with
no formatting logic of its own.

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


@dataclass
class _StageView:
    """Accumulated state for one stage."""

    stage: int
    title: str
    started: bool = False
    finished: bool = False
    current: int | None = None
    total: int | None = None
    elapsed_seconds: float | None = None
    rate: float | None = None
    unit: str = "items"
    last_message: str = ""
    metrics: list[str] = field(default_factory=list)

    def counted(self) -> bool:
        return self.current is not None and self.total is not None


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
        view = self._stages.get(event.stage)
        if view is None:
            view = _StageView(stage=event.stage, title=event.stage_title)
            self._stages[event.stage] = view
            self._order.append(event.stage)

        unit = event.data.get("unit")
        if isinstance(unit, str) and unit:
            view.unit = unit

        if event.kind is EventKind.START:
            view.started = True
            view.finished = False
            if event.message:
                view.last_message = event.message
            if event.total is not None:
                view.total = event.total
                view.current = event.current if event.current is not None else 0

        elif event.kind is EventKind.PROGRESS:
            view.started = True
            # Monotonic in the view too: a late, lower-numbered event from another worker thread must
            # not make the panel count backwards.
            if event.current is not None:
                view.current = event.current if view.current is None else max(view.current, event.current)
            if event.total is not None:
                view.total = event.total
            view.elapsed_seconds = event.elapsed_seconds
            view.rate = event.rate
            if event.message:
                view.last_message = event.message

        elif event.kind is EventKind.METRIC:
            if event.message:
                view.metrics.append(event.message)

        elif event.kind is EventKind.STATE:
            view.started = True
            if event.message:
                view.last_message = event.message

        elif event.kind in (EventKind.WARNING, EventKind.ERROR):
            prefix = "⚠️" if event.kind is EventKind.WARNING else "❌"
            self._notices.append(f"{prefix} Stage {event.stage}: {event.message}")
            del self._notices[: max(0, len(self._notices) - self._max_notices)]

        elif event.kind is EventKind.END:
            view.finished = True
            if event.current is not None:
                view.current = event.current if view.current is None else max(view.current, event.current)
            if event.total is not None:
                view.total = event.total
            if event.elapsed_seconds is not None:
                view.elapsed_seconds = event.elapsed_seconds
            if event.message:
                view.last_message = event.message
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

    def stage_line(self, stage: int) -> str:
        """The detail line for one stage, or ``""`` if that stage is unknown."""
        view = self._stages.get(stage)
        if view is None:
            return ""
        parts: list[str] = []
        if view.counted():
            text = f"{view.current} / {view.total}"
            if view.total:
                text += f" ({100.0 * view.current / view.total:.1f}%)"
            parts.append(text)
        if view.last_message:
            parts.append(view.last_message)
        rate = format_rate(view.rate, view.unit)
        if rate:
            parts.append(rate)
        elapsed = format_duration(view.elapsed_seconds)
        if elapsed:
            parts.append(f"elapsed {elapsed}")
        return " · ".join(parts)

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
            detail = self.stage_line(active)
            if detail:
                lines.append(f"  {detail}")
            for item in view.metrics[-3:]:
                lines.append(f"  · {item}")

        done = [s for s in sorted(self._order) if self._stages[s].finished and s != active]
        if done:
            lines.append("")
            lines.append("Completed:")
            for stage in done:
                view = self._stages[stage]
                elapsed = format_duration(view.elapsed_seconds)
                suffix = f" ({elapsed})" if elapsed else ""
                summary = view.last_message or (
                    f"{view.current}/{view.total}" if view.counted() else "done"
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


__all__ = ["MAX_NOTICES", "ProgressView"]
