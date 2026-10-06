#!/usr/bin/env python3
"""[FORK] Digital-Union (C3-R1A): the pure render-lifecycle/cancellation contract.

Stdlib-only (CLAUDE.md's hard rule): no Gradio, no subprocess, no filesystem, no upstream runtime.
This module decides nothing about what runs — it owns cancellation intent, lifecycle state and
invocation identity only. Every runtime layer (gui.py, video_processor.py, ffmpeg_processing.py,
audio_mixdown.py, auto_mode/__init__.py) creates and reaps its own process/thread handles; none of
that ever lives here.

===============================================================================
BOUNDARY_ONLY_CANCEL
===============================================================================

A :class:`RenderLifecycle` carries one cancellation intent for one top-level render event — an
ordinary Create Music Video click, or the whole existing two-candidate C3-R0 batch (one lifecycle
spans both candidates, never one per candidate). FFmpeg-class subprocess work may observe the
request promptly, mid-call. An already-running Stage-5 (deterministic or Qwen) call is never
hard-killed; the request becomes effective only at the next safe boundary, after that call returns
normally. Neither property is implemented here — this module only exposes the one flag every layer
checks, `cancel_requested()`/`raise_if_cancelled()`, and leaves *when* to check it entirely to the
caller.

===============================================================================
What a lifecycle owns, and what it explicitly does not
===============================================================================

Owns: cancellation intent (one `threading.Event`), lifecycle state (one small state machine) and
invocation identity (one opaque string, minted once at construction).

Owns NO Gradio object, filesystem path, `Popen`, FFmpeg command, Qwen model/process, Stage data,
media identity or cache identity — and carries no module-level registry of past or present
lifecycles. Exactly one lifecycle is ever reachable from the caller that constructed it; `gui.py`'s
own capacity-one active-render slot (not this module) is what makes a lifecycle reachable from a
separate Cancel click.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RenderCancelled(Exception):
    """Raised when a safe boundary observes a matching cancellation request.

    An ordinary ``Exception`` subclass, deliberately — never ``BaseException``. Cancellation is a
    typed outcome of normal control flow, not an interpreter-level escape; every catch site on the
    render path must name it explicitly rather than rely on it skipping broad handlers it has no
    business skipping.
    """


class RenderOutcomeKind(Enum):
    """The five typed terminal causes a render (or one C3 candidate) may reach.

    Deliberately not inferred from a status string anywhere: every producer of a
    :class:`RenderOutcome` names one of these explicitly.
    """

    SUCCESS = "success"
    CANDIDATE_LOCAL = "candidate_local"
    SHARED_FATAL = "shared_fatal"
    CANCELLED = "cancelled"
    UNKNOWN_FATAL = "unknown_fatal"


class RenderLifecycleState(Enum):
    """The lifecycle states one top-level render invocation may pass through.

    ``STARTING -> RUNNING -> [CANCEL_REQUESTED -> CANCELLING] -> FINISHED | FAILED | CANCELLED``.
    The three terminal states are exactly that: no further transition is accepted once one of them
    is reached, enforced by :meth:`RenderLifecycle.mark_terminal`.
    """

    STARTING = "starting"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    FINISHED = "finished"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset((
    RenderLifecycleState.FINISHED,
    RenderLifecycleState.FAILED,
    RenderLifecycleState.CANCELLED,
))

#: Monotonic transition table. A transition not listed here is rejected rather than silently
#: ignored, so a future caller cannot invent a shortcut this contract never reviewed.
_ALLOWED_TRANSITIONS = {
    RenderLifecycleState.STARTING: frozenset((
        RenderLifecycleState.RUNNING,
        RenderLifecycleState.FAILED,
        RenderLifecycleState.CANCELLED,
    )),
    RenderLifecycleState.RUNNING: frozenset((
        RenderLifecycleState.CANCEL_REQUESTED,
        RenderLifecycleState.FINISHED,
        RenderLifecycleState.FAILED,
        RenderLifecycleState.CANCELLED,
    )),
    RenderLifecycleState.CANCEL_REQUESTED: frozenset((
        RenderLifecycleState.CANCELLING,
        RenderLifecycleState.FINISHED,
        RenderLifecycleState.FAILED,
        RenderLifecycleState.CANCELLED,
    )),
    RenderLifecycleState.CANCELLING: frozenset((
        RenderLifecycleState.FINISHED,
        RenderLifecycleState.FAILED,
        RenderLifecycleState.CANCELLED,
    )),
}


@dataclass(frozen=True)
class RenderOutcome:
    """One typed terminal truth. Deepcopy-safe: strings and an ``Enum`` member only.

    The producing boundary names the cause explicitly; nothing downstream may infer one from
    ``message``, which is display text only and never re-parsed.
    """

    kind: RenderOutcomeKind
    message: str = ""

    @property
    def success(self) -> bool:
        """Back-compatible boolean projection: ``True`` iff the typed cause is SUCCESS."""
        return self.kind is RenderOutcomeKind.SUCCESS

    @property
    def cancelled(self) -> bool:
        return self.kind is RenderOutcomeKind.CANCELLED


class RenderLifecycle:
    """One top-level render invocation's cancellation intent, state and identity.

    One instance spans the WHOLE top-level render event — an ordinary Create Music Video click, or
    the existing two-candidate C3-R0 batch end to end — never one per candidate and never one per
    internal `process_video` call. The mutex-owning wrappers (`process_video_guarded`,
    `render_selected_variants_guarded`) construct exactly one and thread it down; nothing below them
    constructs a second.

    Not frozen — state legitimately advances — but every mutating method is internally
    lock-protected, so concurrent reads from a Cancel click and writes from the render thread never
    race.
    """

    def __init__(self, invocation_id: str) -> None:
        self._invocation_id = str(invocation_id)
        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._state = RenderLifecycleState.STARTING

    @property
    def invocation_id(self) -> str:
        return self._invocation_id

    @property
    def state(self) -> RenderLifecycleState:
        with self._state_lock:
            return self._state

    # -- cancellation ---------------------------------------------------------

    def request_cancel(self) -> None:
        """Set the cancellation intent. Idempotent: a second call is a safe no-op.

        Never raises, never blocks, and never itself performs a state transition beyond the one
        advisory CANCEL_REQUESTED step from RUNNING — the render thread is the only mover into
        CANCELLING and the terminal states. Setting an already-set `Event` is a no-op by the stdlib
        contract, so repeated Cancel clicks need no extra guard here.
        """
        self._cancel_event.set()
        with self._state_lock:
            if self._state is RenderLifecycleState.RUNNING:
                self._state = RenderLifecycleState.CANCEL_REQUESTED

    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`RenderCancelled` at a safe boundary if cancellation was requested.

        The one call every safe-boundary check site makes. Total and cheap: an `Event.is_set()`
        read, never a lock beyond that.
        """
        if self._cancel_event.is_set():
            raise RenderCancelled(f"render {self._invocation_id} cancelled by request")

    # -- lifecycle state --------------------------------------------------------

    def mark_running(self) -> None:
        self._transition(RenderLifecycleState.RUNNING)

    def mark_cancelling(self) -> None:
        self._transition(RenderLifecycleState.CANCELLING)

    def mark_terminal(self, state: RenderLifecycleState) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError(f"{state} is not a terminal state")
        self._transition(state)

    def is_terminal(self) -> bool:
        with self._state_lock:
            return self._state in _TERMINAL_STATES

    def _transition(self, target: RenderLifecycleState) -> None:
        with self._state_lock:
            if self._state in _TERMINAL_STATES:
                # No reset/reuse after a terminal state -- a late, out-of-order caller (e.g. an
                # abandoned stream's finalizer racing the normal completion path) simply has nothing
                # left to do, and must never resurrect a finished lifecycle.
                return
            allowed = _ALLOWED_TRANSITIONS.get(self._state, frozenset())
            if target not in allowed:
                raise ValueError(f"illegal lifecycle transition {self._state} -> {target}")
            self._state = target


__all__ = [
    "RenderCancelled",
    "RenderLifecycle",
    "RenderLifecycleState",
    "RenderOutcome",
    "RenderOutcomeKind",
]
