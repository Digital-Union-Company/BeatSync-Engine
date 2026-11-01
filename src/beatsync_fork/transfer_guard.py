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

**Decisions are claimed too, and that is the F-1 correction.** Excluding a second *transfer* was
never sufficient, because the other operation that must not overlap a transfer is a
**classification** -- a durable decision written while a batch is already being sorted from an
earlier snapshot. The original R13 code refused such a classification by reading :meth:`busy` and
then committing, which is the very check-then-act shape :meth:`begin` exists to remove: a decision
that passed the read *before* admission still committed *after* it, was answered ``ok: True``, was
durable, and was ignored by the batch -- both halves reporting success while a clip the operator
marked Keep landed in Discarded.

A post-admission re-read of the decision snapshot does **not** close that on its own, and this was
measured rather than assumed: a classification suspended inside its own commit re-read as
*unchanged*, then became durable immediately afterwards, and the transfer moved the file by the
stale snapshot anyway. So the two operations are made mutually exclusive instead:

* :meth:`begin_decision` claims a short-lived slot that covers one durable ``decide``, and refuses
  while a conflicting transfer holds the floor;
* :meth:`begin` refuses while a conflicting decision is still in flight;
* both decide under the **same** lock, so neither can slip past the other however they interleave.

What that buys the caller is the property the whole feature needs, and it is worth stating as an
invariant because every test below is really a test of it:

    once :meth:`begin` has returned a reservation, no conflicting durable decision can be in
    flight and none can start until it is released -- so a snapshot taken *after* admission is
    the batch's final truth, and a decision accepted by the server can never be silently ignored
    by a transfer already running.

Neither call ever waits: both raise, and the caller turns that into an explicit retry. A conflict
is a sub-10-millisecond coincidence between two human-paced actions, so queueing it would add a
blocking path to save nobody any time, and a busy-wait would add a stall with no bound at all.
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


class DecisionInFlight(Exception):
    """A durable decision is still being written, so a transfer may not be admitted yet.

    Deliberately **not** a :class:`TransferBusy`: that one always carries the active transfer it
    refused against, and here there is no transfer at all -- the refusal is the opposite case, a
    classification the caller must let finish. Giving it its own type is what stops a caller
    reaching for ``.active`` and finding nothing, and lets the UI say the one thing that is
    actually true and actionable: press Confirm again.

    ``code`` is stable and is what a test should assert on.
    """

    def __init__(self, code: str = "decision_in_flight"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Reservation:
    """Proof that this caller won the slot. Opaque, and the only thing that can release it."""

    owner: str
    generation: Any
    signature: str
    root_key: str
    token: int


@dataclass(frozen=True)
class DecisionSlot:
    """Proof that this caller may commit one durable decision. Opaque, like :class:`Reservation`.

    It is held for the duration of ONE ``decide`` -- a journal append and an in-memory update --
    and is not a lock, a permission or a transaction. It confers no authority: the decision
    endpoint still revalidates the page, the transition, the live identity and the expected
    revision exactly as before, and a slot-holder whose checks fail writes nothing.
    """

    owner: str
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
        #: Durable decisions currently being written, keyed by token. Keyed by token rather than
        #: by owner because several owners may legitimately be classifying at once -- only a
        #: *conflicting* pair is excluded, and the conflict rule is the same pair of rules
        #: admission uses.
        self._deciding: dict[int, DecisionSlot] = {}
        #: One counter for both kinds of claim, so a transfer token and a decision token can
        #: never collide and a stale release of either cannot match the other.
        self._token = 0
        #: Refusals, for diagnosis. A duplicate submission that was correctly refused is the
        #: feature working, so this is a counter rather than a warning.
        self.refused = 0

    # ---- admission -------------------------------------------------------------------

    def _conflicting_transfer(self, owner: str, root_key: str) -> tuple[str, ActiveTransfer] | None:
        """The transfer that excludes ``(owner, root_key)``, with its code. **Call under the lock.**

        Factored out because admission and decision claims must apply *exactly* the same two
        rules -- one transfer per owner, one per folder across owners -- and two copies of that
        pair would eventually disagree. The order is load-bearing: a self-collision is reported as
        one rather than as somebody else's folder.
        """
        mine = self._active.get(owner)
        if mine is not None:
            return "transfer_in_progress", mine
        if root_key:
            for other in self._active.values():
                if other.root_key == root_key:
                    return "folder_in_progress", other
        return None

    def begin(self, owner: str, generation: Any, signature: str,
              root_key: str) -> Reservation:
        """Claim the slot, or raise :class:`TransferBusy`. **One locked test-and-set.**

        The two refusal codes are distinct because they mean different things to an operator:

        * ``transfer_in_progress`` -- this very page is already being sorted. Almost always a
          double-click or a replayed event.
        * ``folder_in_progress`` -- somebody else's review session is sorting this same folder.
          Rarer, and worth saying plainly rather than reporting as a self-collision.

        [F-1] It also raises :class:`DecisionInFlight` while a conflicting durable decision is
        still being written. That is the half of the correction that cannot be done on the caller's
        side at all: the caller cannot know whether a classification has *passed its own checks*
        and is about to commit, and a snapshot re-read after admission provably does not find out
        (a decision suspended inside its commit re-reads as unchanged). Refusing admission for the
        few milliseconds a journal append takes is what makes the post-admission snapshot final.
        """
        key = str(owner)
        root = str(root_key or '')
        with self._lock:
            conflict = self._conflicting_transfer(key, root)
            if conflict is not None:
                self.refused += 1
                raise TransferBusy(*conflict)
            for pending in self._deciding.values():
                if pending.owner == key or (root and pending.root_key == root):
                    self.refused += 1
                    raise DecisionInFlight()
            self._token += 1
            record = ActiveTransfer(owner=key, generation=generation,
                                    signature=str(signature or ''), root_key=str(root_key or ''),
                                    token=self._token)
            self._active[key] = record
            return Reservation(owner=key, generation=generation,
                               signature=record.signature, root_key=record.root_key,
                               token=record.token)

    # ---- decision claims (F-1) -------------------------------------------------------

    def begin_decision(self, owner: str, root_key: str = '') -> DecisionSlot:
        """Claim the right to commit ONE durable decision, or raise :class:`TransferBusy`.

        The mirror of :meth:`begin`, deciding under the same lock and by the same two rules, which
        is what makes the pair mutually exclusive rather than merely both careful. The refusal
        codes are :meth:`begin`'s, because the reason really is a transfer: this page is being
        sorted, or somebody else is sorting this folder.

        This **replaces** a :meth:`busy` read followed by a commit. That shape refused the common
        case and missed the one that mattered -- a classification that passed the read before
        admission and committed after it. Here the claim *is* the check, so there is no instant
        between them for a transfer to be admitted in.

        Release it in a ``finally``: see :meth:`end_decision`.
        """
        key = str(owner)
        root = str(root_key or '')
        with self._lock:
            conflict = self._conflicting_transfer(key, root)
            if conflict is not None:
                self.refused += 1
                raise TransferBusy(*conflict)
            self._token += 1
            slot = DecisionSlot(owner=key, root_key=root, token=self._token)
            self._deciding[slot.token] = slot
            return slot

    def end_decision(self, slot: DecisionSlot) -> bool:
        """Release ``slot``. Idempotent, token-matched, and never raises.

        Called from a ``finally`` for the same reason :meth:`end` is: the commit it covers can
        refuse, raise :class:`~beatsync_fork.clips_review.StaleWrite`, or fail durably, and a slot
        that outlived its decision would refuse every subsequent transfer for this owner with no
        way back short of a restart. Keyed by token, so releasing twice is harmless and releasing
        somebody else's is impossible.
        """
        with self._lock:
            return self._deciding.pop(slot.token, None) is not None

    def deciding(self) -> int:
        """How many durable decisions are in flight. For diagnosis and for one specific test.

        The map is bounded by the number of decision requests actually executing -- each one is
        claimed and released inside a single call -- so this is the figure that shows the
        correction cannot accumulate state. It is deliberately a count, not the slots: a caller
        has no business inspecting somebody else's claim.
        """
        with self._lock:
            return len(self._deciding)

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
        race :meth:`begin` exists to remove; its callers use it to choose a *message* and nothing
        else. Admission is :meth:`begin`, a decision claim is :meth:`begin_decision`, and those two
        are the only authorities.

        [F-1] It used to be documented as also refusing a classification. It was, and that was the
        defect: a read cannot refuse anything, because the state it reports may change before the
        caller acts on it. Every mutation is now gated by a claim, and this is left as presentation
        only -- which is all a greyed-out button or a "already being sorted" line ever needed.
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
