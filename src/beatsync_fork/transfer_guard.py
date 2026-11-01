"""Server-side single-flight for the Video Review transfer (R13).

[FORK] Digital-Union. Pure policy: stdlib only, and nothing from the upstream runtime --
CLAUDE.md's hard rule for ``src/beatsync_fork/``. It touches no filesystem, starts no thread and
knows nothing about clips, so every rule below is executed directly on a bare interpreter rather
than asserted about structurally.

**What this is the authority for.** Pressing Confirm (or Proceed) in Video Review starts moving
files. A second press, a replayed event, a second browser tab or any caller that never saw the UI
must not run the *same* transfer again, and a disabled HTML button cannot promise that -- it is a
courtesy that does not survive a second tab or a hand-made request. So admission is a
**test-and-set under one lock**, exactly as ``_RENDER_LOCK`` is for renders, and for the same
reason: ``if not busy: start()`` is a check-then-act race, while taking the slot *is* the check.

**Operation identity, stated explicitly, because the lock scope is a design decision and not an
implementation detail.** A transfer is identified by

```
(owner, generation, signature, root_key)
```

* ``owner`` -- the review session's owner key, the same per-browser-session scoping key
  :class:`~beatsync_fork.clips_review.ScanCoordinator` uses. **One transfer per owner** is the
  primary rule: a duplicate submission is always a duplicate from the same owner.
* ``generation`` -- the coordinator generation of the page the transfer was admitted on. Recorded
  so the caller can tell, when the transfer finishes, whether the page it acted on is still the
  current one. It is **not** part of admission: re-scanning does not entitle a second concurrent
  transfer.
* ``signature`` -- the batch signature the plan was derived from, recorded for diagnosis.
* ``root_key`` -- a caller-normalised key for the folder being sorted. **Also exclusive**, across
  owners: two browser sessions that scanned the same folder would otherwise move the same files
  at the same time. The caller normalises it (this module performs no path operations at all, so
  it cannot be fooled into thinking two spellings of one directory are different -- that
  responsibility is explicitly the caller's).

**What it is NOT.** It is not an authorization boundary, not a queue, and not a cancellation
mechanism. It confers no permission: the caller still re-derives the plan from the server's own
snapshot and re-validates the destinations on every press, exactly as before. Holding a
reservation permits nothing that would otherwise be refused -- it only excludes a *second*
concurrent transfer.

**The lock is never held across a file operation.** :meth:`TransferGuard.begin` takes the lock,
installs the record and returns; the record in ``_active`` is what marks the owner busy. Moving
files happens with no lock held, and :meth:`TransferGuard.note_progress` re-takes it only for the
duration of two integer assignments. A lock held across a batch of renames would block the very
inspection the decision endpoint performs on every classification.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

#: How many progress frames one transfer may emit, however many clips it moves.
#:
#: A frame per clip is correct for a handful and wasteful for a thousand -- each one is a Gradio
#: round trip. The budget bounds the cost without ever bounding the *truth*: the last outcome is
#: always emitted, so the final figure is never an extrapolation of a skipped frame.
PROGRESS_FRAME_BUDGET = 50


class TransferBusy(Exception):
    """A transfer is already running for this owner, or on this folder for somebody else.

    Carries the active record so the caller can say *what* is running rather than only that
    something is. ``code`` is stable and is what a test should assert on.
    """

    def __init__(self, code: str, active: "ActiveTransfer"):
        super().__init__(code)
        self.code = code
        self.active = active


@dataclass(frozen=True)
class Reservation:
    """Proof that this caller won the slot. Opaque, and the only thing that can release it."""

    owner: str
    generation: Any
    signature: str
    root_key: str
    token: int


@dataclass
class ActiveTransfer:
    """The record that marks an owner busy. Plain data -- no lock, thread, handle or descriptor.

    ``done`` / ``total`` are the last figures :meth:`TransferGuard.note_progress` was given, so a
    refusal can report real progress rather than a guess. They are **reported** numbers, never
    predicted ones; see :func:`should_emit`.
    """

    owner: str
    generation: Any
    signature: str
    root_key: str
    token: int
    total: int = 0
    done: int = 0


def progress_stride(total: int) -> int:
    """How many recorded outcomes one progress frame is worth.

    ``1`` whenever the whole batch fits inside :data:`PROGRESS_FRAME_BUDGET`, so a small batch
    reports every clip.
    """
    if total <= PROGRESS_FRAME_BUDGET:
        return 1
    return -(-int(total) // PROGRESS_FRAME_BUDGET)


def should_emit(done: int, total: int) -> bool:
    """Whether the frame after ``done`` recorded outcomes should be shown.

    ``done`` counts **recorded outcomes** -- clips the transfer has actually finished attempting --
    so it is never ahead of what happened. The final outcome always emits, which is what makes the
    closing figure a measurement rather than a rounding of the last stride.

    There is deliberately **no clock here**. A time-based throttle would make the frame sequence
    depend on how fast the disk is, which is exactly the kind of untestable progress this feature
    exists to replace.
    """
    if total <= 0:
        return True
    if done >= total:
        return True
    if done <= 0:
        return False
    return done % progress_stride(total) == 0


class TransferGuard:
    """One concurrent transfer per owner, and one per folder across owners.

    Locking is a single process-global :class:`threading.Lock` rather than a lock per owner, and
    that is deliberate: the cross-owner folder rule has to be decided against *every* active
    record, so a per-owner lock could not make the decision atomically. Every critical section
    here is a handful of dictionary operations over a map bounded by the number of live review
    sessions, so contention is not a concern -- and no file operation ever happens inside one.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, ActiveTransfer] = {}
        self._token = 0
        #: Refusals, for diagnosis. A duplicate submission that was correctly refused is the
        #: feature working, so this is a counter rather than a warning.
        self.refused = 0

    # ---- admission -------------------------------------------------------------------

    def begin(self, owner: str, generation: Any, signature: str,
              root_key: str) -> Reservation:
        """Claim the slot, or raise :class:`TransferBusy`. **One locked test-and-set.**

        The two refusal codes are distinct because they mean different things to an operator:

        * ``transfer_in_progress`` -- this very page is already being sorted. Almost always a
          double-click or a replayed event.
        * ``folder_in_progress`` -- somebody else's review session is sorting this same folder.
          Rarer, and worth saying plainly rather than reporting as a self-collision.
        """
        key = str(owner)
        with self._lock:
            mine = self._active.get(key)
            if mine is not None:
                self.refused += 1
                raise TransferBusy("transfer_in_progress", mine)
            if root_key:
                for other in self._active.values():
                    if other.root_key == root_key:
                        self.refused += 1
                        raise TransferBusy("folder_in_progress", other)
            self._token += 1
            record = ActiveTransfer(owner=key, generation=generation,
                                    signature=str(signature or ''), root_key=str(root_key or ''),
                                    token=self._token)
            self._active[key] = record
            return Reservation(owner=key, generation=generation,
                               signature=record.signature, root_key=record.root_key,
                               token=record.token)

    # ---- the record ------------------------------------------------------------------

    def note_progress(self, reservation: Reservation, done: int, total: int) -> bool:
        """Record real progress against ``reservation``. Returns whether it was still ours.

        **Token-matched**, like the release below. A late update from a reservation that has
        already ended must not write into a *newer* transfer's record, and must not resurrect a
        finished one -- that is the same identity discipline ``_end_verify_session`` uses, and for
        the same reason: the two are indistinguishable from inside the function otherwise.
        """
        with self._lock:
            record = self._active.get(reservation.owner)
            if record is None or record.token != reservation.token:
                return False
            record.total = int(total)
            record.done = int(done)
            return True

    def active_for(self, owner: str) -> ActiveTransfer | None:
        """The owner's active transfer, or ``None``. Never raises for an unknown owner."""
        with self._lock:
            return self._active.get(str(owner))

    def active_on_root(self, root_key: str) -> ActiveTransfer | None:
        """Any owner's active transfer on ``root_key``, or ``None``.

        An empty ``root_key`` answers ``None`` rather than matching every record whose key is also
        empty: an unknown folder is not evidence that a particular folder is busy.
        """
        if not root_key:
            return None
        with self._lock:
            for record in self._active.values():
                if record.root_key == root_key:
                    return record
            return None

    def busy(self, owner: str, root_key: str = '') -> ActiveTransfer | None:
        """Either reason this owner cannot classify or sort right now. Advisory only.

        **This is not an admission gate.** Reading it and then acting would be the check-then-act
        race :meth:`begin` exists to remove; its callers use it to choose a *message*, and to
        refuse a classification that would otherwise change a snapshot already in flight. Admission
        is :meth:`begin` and nothing else.
        """
        return self.active_for(owner) or self.active_on_root(root_key)

    # ---- release ---------------------------------------------------------------------

    def end(self, reservation: Reservation) -> bool:
        """Release ``reservation``. Idempotent, token-matched, and never raises.

        Called from a ``finally``, so it has to be safe on every terminal path -- success, a
        partial batch, an exception, and an abandoned generator. Returns whether this call was the
        one that released it.
        """
        with self._lock:
            record = self._active.get(reservation.owner)
            if record is None or record.token != reservation.token:
                return False
            del self._active[reservation.owner]
            return True
