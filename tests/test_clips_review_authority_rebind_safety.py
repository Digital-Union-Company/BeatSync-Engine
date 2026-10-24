"""R5-P55 — authority rebinding may never orphan a reservation or a lease (F54-1).

Independent review R5-P54 returned ``R5_P54_PR75_CHANGES_REQUIRED`` on one reproduced MAJOR.
``bind_lifecycle_authority`` refused a rebind that would orphan a live *lease*
(``has_any_lease()``) but asked nothing about a pending mutation *reservation*. Rebinding across
that window reinstated the R5-P52 F53-A outcome in full, measured 3/3 on real files: the clip
moved, the batch reported ``COMPLETE``, ``advance_page`` was ``True``, there was no refusal record
in the journal — and the session and page the operation had been planned against were already
revoked.

**The naive repair is not the fix, and R5-P54 said so.** Adding ``has_any_pending()`` to the old
predicate and then assigning is check-then-act: a reservation granted between the question and the
assignment is orphaned exactly as thoroughly. That is the same defect class R5-P52 proved fatal one
layer down, which is why the correction here is a **two-phase quiescence barrier** with a stated
linearization point rather than one more predicate.

What this module pins, in the work order's own case lettering:

* **A** — a reservation taken first makes the rebind refuse, leaves all three bindings on the old
  authority, lets the mutation complete, and permits the rebind once the reservation releases.
* **B** — the binder taken first wins: a reservation or an acquisition attempted *inside* the
  window between the barrier and the assignments is refused. Both sub-orderings are driven, and
  each test asserts the pause was genuinely reached — a race test that never reaches its program
  point proves nothing, and R5-P54 recorded exactly that trap for the obsolete ``guard`` tests.
* **C** — an acquisition in flight is part of quiescence. ``acquire`` releases ``_lock`` to probe
  the coordinator, so "no lease installed" is not "no acquisition in flight"; a rebinding that
  began and finished inside that window would leave the lease on a detached authority, which is
  F54-1 with the lease and the reservation exchanged.
* **D** — initial attachment, lazy store attachment and same-authority idempotence all still work,
  including under a live reservation and under a live lease, and independent worlds stay
  independent.
* **E** — all seven partial target sets refuse while the old authority is non-quiescent.

Plus the two structural pins that keep the gate from being bypassed instead of broken: no mutation
site may treat the barrier as advisory, and ``gui.py``'s single direct attribute assignment must
stay an *initial* attachment, which is the only reason it is not a reachable bypass today.

Threads, events and task-owned synthetic files only. Nothing here touches production media, the
``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import ast
import os
import threading

import pytest

from beatsync_fork import clips_review as cr

from test_clips_review_atomic_lifecycle import World

TIMEOUT = 30.0

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_GUI = os.path.join(_SRC, "gui.py")
_CLIPS_REVIEW = os.path.join(_SRC, "beatsync_fork", "clips_review.py")


# ---------------------------------------------------------------------------------- helpers

#: The lifecycle operations whose reservation must block a rebind. The three R5-P54 reproduced
#: directly come first; the remaining two are included because the defect was never specific to an
#: operation — it was in the binding predicate, which is operation-blind.
REBIND_BLOCKING_OPS = {
    cr.LIFECYCLE_SESSION_REVOKE: lambda w: w.registry.revoke_owner(w.owner),
    cr.LIFECYCLE_SCAN_BEGIN: lambda w: w.coordinator.begin(w.owner),
    cr.LIFECYCLE_SESSION_NEW: lambda w: w.registry.new_session(w.owner, "r5p55-intruder"),
    cr.LIFECYCLE_SCAN_RECORD: lambda w: w.store.begin_scan(w.owner, "r5p55-intruder"),
    cr.LIFECYCLE_PAGE_PUBLISH: lambda w: w.registry.publish_page(w.owner, w.session, 2, w.frozen),
}


def _bindings(w):
    """The identity of the authority each of the three subsystems actually consults."""
    return (w.registry.lifecycle_authority,
            w.store.lifecycle_authority,
            w.coordinator.lifecycle_authority)


class _PausedReservation:
    """Hold a real reservation on ``authority`` across a real lifecycle mutation.

    The mutation thread is parked **after its reservation has been granted and before it mutates
    anything** — the same program point R5-P53's own suite uses, and the one R5-P54 proved is the
    valid replacement for the removed ``guard`` return.
    """

    def __init__(self, world, operation):
        self.world = world
        self.operation = operation
        self.authority = world.authority
        self.reserved = threading.Event()
        self.may_mutate = threading.Event()
        self.finished = threading.Event()
        self.error = None
        self._real = self.authority.reserve_mutation
        self._thread = None

    def __enter__(self):
        def traced(owner, operation):
            reservation = self._real(owner, operation)      # the REAL reservation, unchanged
            if operation == self.operation:
                self.reserved.set()
                assert self.may_mutate.wait(TIMEOUT), "the rebind attempt never finished"
            return reservation

        def run():
            try:
                REBIND_BLOCKING_OPS[self.operation](self.world)
            except Exception as exc:                        # noqa: BLE001 - recorded, not swallowed
                self.error = exc
            finally:
                self.finished.set()

        self.authority.reserve_mutation = traced
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert self.reserved.wait(TIMEOUT), (
            "the reservation boundary was never reached, so this test proves nothing")
        assert self.authority.pending_count(self.world.owner) == 1
        assert self.authority.has_any_pending() is True
        assert self.authority.has_any_lease() is False, "a lease would confound the finding"
        return self

    def release(self):
        self.may_mutate.set()
        assert self.finished.wait(TIMEOUT)
        self._thread.join(TIMEOUT)
        self.authority.reserve_mutation = self._real

    def __exit__(self, *exc):
        if not self.finished.is_set():
            self.release()
        return False


# ======================================================== Case A — the reservation comes first

@pytest.mark.parametrize("operation", list(REBIND_BLOCKING_OPS))
def test_a_rebinding_is_refused_while_a_mutation_reservation_is_pending(tmp_path, operation):
    """Case A, and the direct closure of R5-P54 F54-1.

    R5-P54's measurement on this exact sequence was ``rebind=ACCEPTED`` with ``A_pending=1``. The
    rebind must refuse, and refusing has to be *total*: every binding unchanged, and — the part a
    looser fix would miss — neither authority's coordinator pointer touched either.
    """
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    before_bindings = _bindings(w)
    old_coordinator = w.authority._coordinator
    assert usurper._coordinator is None

    with _PausedReservation(w, operation) as held:
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=usurper)
        assert caught.value.status == 409
        assert caught.value.reason == cr.LIFECYCLE_REBIND_REFUSED

        # A refusal leaves every existing binding exactly as it was.
        assert _bindings(w) == before_bindings
        assert all(a is w.authority for a in _bindings(w))
        # ...including the coordinator pointer on BOTH authorities.
        assert w.authority._coordinator is old_coordinator
        assert usurper._coordinator is None
        # The reservation is still live on the authority that is still in force.
        assert w.authority.pending_count(w.owner) == 1
        # And no barrier was left behind on either side.
        assert w.authority.is_rebinding() is False
        assert usurper.is_rebinding() is False

        held.release()

    assert held.error is None, "the reserved mutation must still complete normally"
    assert w.authority.pending_count(w.owner) == 0

    # Once quiescent again, the very same rebind is permitted. The gate is a quiescence
    # condition, not a permanent prohibition.
    assert cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                       coordinator=w.coordinator,
                                       authority=usurper) is usurper
    assert all(a is usurper for a in _bindings(w))
    assert usurper._coordinator is w.coordinator


def test_a_the_reserved_mutation_still_excludes_the_transfer_after_the_refusal(tmp_path):
    """Case A, end to end on the real filesystem — the F54-1 outcome itself.

    R5-P54 measured: clip moved, ``batch=COMPLETE``, ``advance_page=True``, no refusal record. The
    point of refusing the rebind is that the *transfer* is still refused, so this asserts the
    filesystem and the batch, not merely the binding.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    assert plan.ok, plan.refusals
    before_review = w.review_files()

    with _PausedReservation(w, cr.LIFECYCLE_SESSION_REVOKE) as held:
        usurper = cr.LifecycleAuthority()
        with pytest.raises(cr.Refusal):
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=usurper)

        # The acquisition is refused for the intended reason, not by a timeout or a generic error.
        with pytest.raises(cr.Refusal) as refused:
            w.lease(plan)
        assert refused.value.status == 409
        assert refused.value.reason == cr.LIFECYCLE_PENDING

        # Executed as a batch rather than acquired directly, it refuses and does not advance.
        outcome = w.run(plan)
        assert outcome.state == "REFUSED"
        assert outcome.advance_page is False
        # Nothing moved, and the usurper never became a usable gate.
        assert w.library_files() == []
        assert w.review_files() == before_review
        assert usurper.holder(w.owner) == ""
        assert w.authority.holder(w.owner) == ""
        held.release()


# ======================================================== Case B — the binder comes first

class _PausedRebind:
    """Park ``bind_lifecycle_authority`` inside its barrier window.

    ``pause_on`` selects which authority's ``begin_rebind`` blocks, which selects *where* in the
    protocol the pause lands:

    * the outgoing authority → after it is barriered, before the incoming one is, and before any
      assignment;
    * the incoming authority → after the **last** barrier, i.e. immediately past the linearization
      point and immediately before the first attribute assignment.

    Both are windows in which a concurrent reservation or acquisition must be refused.
    """

    def __init__(self, pause_on):
        self.pause_on = pause_on
        self.inside = threading.Event()
        self.may_finish = threading.Event()
        self.reached = False
        self._real = pause_on.begin_rebind

    def __enter__(self):
        def traced():
            installed = self._real()
            if installed:
                self.reached = True
                self.inside.set()
                assert self.may_finish.wait(TIMEOUT), "the racing operation never completed"
            return installed

        self.pause_on.begin_rebind = traced
        return self

    def restore(self):
        self.pause_on.begin_rebind = self._real

    def __exit__(self, *exc):
        self.may_finish.set()
        self.restore()
        return False


def _rebind_in_thread(w, authority):
    """Run the rebind on its own thread; returns (thread, result-dict)."""
    out = {}

    def run():
        try:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=authority)
            out["result"] = "ACCEPTED"
        except cr.Refusal as exc:
            out["result"] = "REFUSED:%s" % exc.reason

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


@pytest.mark.parametrize("pause_where", ["outgoing", "incoming"])
def test_b_a_reservation_attempted_inside_the_barrier_window_is_refused(tmp_path, pause_where):
    """Case B — the binder confirms quiescence, then pauses; a mutation tries to reserve.

    This is the test R5-P54 asked for by name and the reason ``has_any_pending()`` alone is not
    the fix: a naive implementation would be *inside exactly this window* with the authority still
    accepting reservations. The barrier is installed in the same critical section that observed
    quiescence, so the reservation is refused and the binder wins.
    """
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    paused_on = w.authority if pause_where == "outgoing" else usurper

    with _PausedRebind(paused_on) as parked:
        thread, out = _rebind_in_thread(w, usurper)
        assert parked.inside.wait(TIMEOUT), (
            "the binder never reached the window between its quiescence check and its "
            "assignment, so this test proves nothing")
        assert parked.reached is True

        # The barrier really is installed on the authority that was paused.
        assert paused_on.is_rebinding() is True

        # A lifecycle mutation may not reserve here. It is refused, and named as a rebinding.
        with pytest.raises(cr.Refusal) as caught:
            paused_on.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
        assert caught.value.status == 409
        assert caught.value.reason == cr.LIFECYCLE_REBINDING
        assert paused_on.pending_count(w.owner) == 0, "a refused reservation records nothing"

        # The non-raising form skips rather than raising, as eviction paths require.
        assert paused_on.try_reserve_mutation(w.owner, cr.LIFECYCLE_OWNER_EVICT) is None

        # A real lifecycle mutation through a real subsystem is refused too, and changes nothing.
        before = w.snapshot()
        if paused_on is w.authority:
            with pytest.raises(cr.Refusal) as mutation:
                w.registry.revoke_owner(w.owner)
            assert mutation.value.reason == cr.LIFECYCLE_REBINDING
            assert w.snapshot() == before

        parked.may_finish.set()

    thread.join(TIMEOUT)
    assert out["result"] == "ACCEPTED"
    # The binder won cleanly: one coherent binding, no split, no barrier left behind.
    assert all(a is usurper for a in _bindings(w))
    assert w.authority.is_rebinding() is False
    assert usurper.is_rebinding() is False
    assert usurper.pending_count(w.owner) == 0
    assert w.authority.pending_count(w.owner) == 0


def test_b_a_reservation_granted_on_the_incoming_authority_fails_the_whole_rebind(tmp_path):
    """Case B, the other ordering — quiescence is re-checked per authority, so a reservation that
    lands on the *incoming* authority between the two barriers refuses the whole operation.

    This is the interleaving a single combined predicate would miss. The barrier on the outgoing
    authority succeeded; the one on the incoming authority must still fail closed, and the
    already-installed barrier must be released by the ``finally``.
    """
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    before_bindings = _bindings(w)
    smuggled = {}
    real_begin = w.authority.begin_rebind

    def traced():
        installed = real_begin()
        if installed and "reservation" not in smuggled:
            # Exactly the race: between barriering the outgoing authority and barriering the
            # incoming one, a lifecycle mutation reserves on the incoming one.
            smuggled["reservation"] = usurper.reserve_mutation(w.owner,
                                                               cr.LIFECYCLE_SESSION_REVOKE)
        return installed

    w.authority.begin_rebind = traced
    try:
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=usurper)
        assert caught.value.reason == cr.LIFECYCLE_REBIND_REFUSED
    finally:
        w.authority.begin_rebind = real_begin

    assert "reservation" in smuggled, "the interleaving never happened; the test proves nothing"
    assert _bindings(w) == before_bindings
    assert usurper._coordinator is None
    # The barrier taken on the outgoing authority was released on the refusal path.
    assert w.authority.is_rebinding() is False
    assert usurper.is_rebinding() is False
    # ...and the authority is not left permanently closed to lifecycle operations.
    reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
    assert w.authority.release_mutation(reservation) is True
    assert usurper.release_mutation(smuggled["reservation"]) is True


def test_b_the_barrier_never_blocks_a_release(tmp_path):
    """A barrier that blocked ``release`` would be a deadlock, not a safety property.

    ``begin_rebind`` refuses rather than waiting, and the two release paths stay open while it is
    held, so a live operation can always drain and an operator retry always succeeds.
    """
    w = World(tmp_path, count=1)
    assert w.authority.begin_rebind() is True
    try:
        # Both releases are reachable under the barrier and neither blocks.
        assert w.authority.release_mutation(None) is False
        assert w.authority.release(w.owner, "no-such-plan") is False
        # A second binder cannot take the same barrier...
        assert w.authority.begin_rebind() is False
        # ...and acquisition is closed while it is held.
        with pytest.raises(cr.Refusal) as caught:
            w.authority.acquire(w.owner, "plan-x", session_id=w.session, page_token=w.page,
                                scan_id=w.scan_id, identities=["i1"])
        assert caught.value.reason == cr.LIFECYCLE_REBINDING
    finally:
        w.authority.end_rebind()
    assert w.authority.is_rebinding() is False
    # end_rebind is idempotent.
    w.authority.end_rebind()
    assert w.authority.is_rebinding() is False


def test_b_a_non_quiescent_authority_cannot_be_barriered_at_all(tmp_path):
    """The barrier primitive's own contract, asserted directly rather than through the binder."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)

    # ...with a reservation held
    reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
    assert w.authority.has_any_pending() is True
    assert w.authority.begin_rebind() is False
    assert w.authority.release_mutation(reservation) is True

    # ...with a lease held
    plan = w.lease()
    assert w.authority.has_any_lease() is True
    assert w.authority.begin_rebind() is False
    w.store.end_transfer(plan.owner, plan.plan_id)

    # ...and quiescent again
    assert w.authority.has_any_pending() is False
    assert w.authority.has_any_lease() is False
    assert w.authority.acquiring_count() == 0
    assert w.authority.begin_rebind() is True
    w.authority.end_rebind()


# ======================================================== Case C — acquisition races the rebind

def test_c_rebinding_during_a_live_lease_is_still_refused(tmp_path):
    """Case C, first row — R5-P53's original property, re-driven rather than assumed."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    usurper = cr.LifecycleAuthority()
    before_bindings = _bindings(w)
    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                    coordinator=w.coordinator, authority=usurper)
    assert caught.value.reason == cr.LIFECYCLE_REBIND_REFUSED
    assert _bindings(w) == before_bindings
    assert usurper._coordinator is None
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_c_an_acquisition_in_flight_refuses_the_rebind(tmp_path):
    """Case C, the sharp row — a rebind attempted **between** ``acquire``'s two arbitrations.

    ``acquire`` deliberately drops ``_lock`` to probe the coordinator, so in that window no lease
    is installed yet and ``has_any_lease()`` reports the authority quiescent. A rebinding accepted
    here would install the lease on an authority nothing consults — F54-1 with the lease and the
    reservation exchanged. Driven from inside the real probe, so the window is genuinely occupied.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    usurper = cr.LifecycleAuthority()
    seen = {}
    real_view = w.coordinator.view

    def probing_view(owner):
        if "rebind" not in seen:
            seen["acquiring_during_probe"] = w.authority.acquiring_count()
            seen["lease_during_probe"] = w.authority.has_any_lease()
            try:
                cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                            coordinator=w.coordinator, authority=usurper)
                seen["rebind"] = "ACCEPTED"
            except cr.Refusal as exc:
                seen["rebind"] = "REFUSED:%s" % exc.reason
        return real_view(owner)

    w.coordinator.view = probing_view
    try:
        plan = w.lease()                       # a real begin_transfer -> real acquire
    finally:
        w.coordinator.view = real_view

    assert "rebind" in seen, "the probe window was never entered; the test proves nothing"
    # The window really was the one claimed: in flight, with no lease installed yet.
    assert seen["acquiring_during_probe"] == 1
    assert seen["lease_during_probe"] is False
    # And the rebind was refused on exactly that basis.
    assert seen["rebind"] == "REFUSED:%s" % cr.LIFECYCLE_REBIND_REFUSED
    assert all(a is w.authority for a in _bindings(w))
    assert usurper._coordinator is None
    # The acquisition itself was unaffected: a binding attempt must not break an in-flight one.
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)
    assert w.authority.acquiring_count() == 0


def test_c_a_rebind_during_an_epoch_retry_is_refused(tmp_path):
    """Case C — the same window, entered on an epoch-conflict retry rather than the first pass.

    A retry re-enters the probe, so the in-flight marker must still be set; if it were cleared and
    re-taken per pass, a rebind could slip into the gap between two attempts.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    usurper = cr.LifecycleAuthority()
    calls = {"n": 0}
    seen = {}
    real_view = w.coordinator.view

    def probing_view(owner):
        calls["n"] += 1
        if calls["n"] == 1:
            # Move the epoch so the first arbitration loses and `acquire` retries.
            bump = w.authority.reserve_mutation(owner, cr.LIFECYCLE_SCAN_RECORD)
            w.authority.release_mutation(bump)
        elif calls["n"] == 2 and "rebind" not in seen:
            seen["acquiring_on_retry"] = w.authority.acquiring_count()
            try:
                cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                            coordinator=w.coordinator, authority=usurper)
                seen["rebind"] = "ACCEPTED"
            except cr.Refusal as exc:
                seen["rebind"] = "REFUSED:%s" % exc.reason
        return real_view(owner)

    w.coordinator.view = probing_view
    try:
        plan = w.lease()
    finally:
        w.coordinator.view = real_view

    assert calls["n"] >= 2, "the retry path was never taken; the test proves nothing"
    assert seen.get("acquiring_on_retry") == 1, "the in-flight marker did not span the retry"
    assert seen["rebind"] == "REFUSED:%s" % cr.LIFECYCLE_REBIND_REFUSED
    assert all(a is w.authority for a in _bindings(w))
    assert w.authority.epoch_conflicts >= 1
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_c_an_acquisition_entered_under_a_barrier_is_refused_before_it_probes(tmp_path):
    """Case C — and the mirror of Case B: the barrier closes acquisition at the door.

    The refusal has to land before the coordinator is consulted, so a barriered authority never
    leaves a half-entered acquisition for ``begin_rebind`` to trip over.
    """
    w = World(tmp_path, count=1)
    probes = {"n": 0}
    real_view = w.coordinator.view

    def counting_view(owner):
        probes["n"] += 1
        return real_view(owner)

    w.coordinator.view = counting_view
    assert w.authority.begin_rebind() is True
    try:
        with pytest.raises(cr.Refusal) as caught:
            w.authority.acquire(w.owner, "plan-y", session_id=w.session, page_token=w.page,
                                scan_id=w.scan_id, identities=["i1"])
        assert caught.value.reason == cr.LIFECYCLE_REBINDING
    finally:
        w.authority.end_rebind()
        w.coordinator.view = real_view
    assert probes["n"] == 0, "a refused acquisition must not have probed the coordinator"
    assert w.authority.acquiring_count() == 0, "a refused entry must not leak the in-flight marker"


def test_c_a_barrier_appearing_during_the_probe_refuses_at_the_final_arbitration(tmp_path):
    """Case C — defence in depth, and the reason the barrier lives in the *shared* predicate.

    ``_acquiring`` already stops a legitimate ``begin_rebind`` from landing inside the probe
    window, so this forces the state the barrier would have if that first line of defence were
    ever bypassed — a future caller, a wider quiescence rule, a refactor — and requires the
    acquisition to refuse anyway. Checking the barrier only at ``acquire``'s door would pass this
    while leaving the final arbitration weaker than the first, which is precisely the drift
    ``_reject_if_unavailable`` was factored out to prevent.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    seen = {}
    real_view = w.coordinator.view

    def probing_view(owner):
        if "forced" not in seen:
            seen["forced"] = True
            # Force the barrier mid-probe, past acquire's entry check.
            with w.authority._lock:
                w.authority._rebinding = True
        return real_view(owner)

    w.coordinator.view = probing_view
    try:
        with pytest.raises(cr.Refusal) as caught:
            w.lease()
        assert caught.value.reason == cr.LIFECYCLE_REBINDING
    finally:
        w.coordinator.view = real_view
        with w.authority._lock:
            w.authority._rebinding = False

    assert seen.get("forced") is True, "the probe never ran; the test proves nothing"
    assert w.authority.has_any_lease() is False, "no lease may be installed under a barrier"
    assert w.authority.acquiring_count() == 0
    assert w.library_files() == []


def test_c_a_failed_acquisition_does_not_leak_the_in_flight_marker(tmp_path):
    """Every ``acquire`` exit path must clear ``_acquiring``, or the authority becomes
    permanently un-rebindable — a leak that would be invisible until a rebind refused forever."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)

    # (a) refused for a pending reservation
    reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
    with pytest.raises(cr.Refusal):
        w.authority.acquire(w.owner, "p1", session_id=w.session, page_token=w.page,
                            scan_id=w.scan_id, identities=["i1"])
    assert w.authority.acquiring_count() == 0
    assert w.authority.release_mutation(reservation) is True

    # (b) refused because a scan is in flight
    generation = w.coordinator.begin(w.owner)
    with pytest.raises(cr.Refusal) as caught:
        w.authority.acquire(w.owner, "p2", session_id=w.session, page_token=w.page,
                            scan_id=w.scan_id, identities=["i2"])
    assert caught.value.reason == "scan_in_flight"
    assert w.authority.acquiring_count() == 0
    # `discard` deliberately leaves an honest *empty* view rather than resurrecting the older
    # page, so the owner stays in transition until a scan publishes again. Re-scan to restore a
    # published generation; otherwise case (c) would refuse as `scan_in_flight` and never reach
    # the epoch logic it exists to exercise.
    w.coordinator.discard(w.owner, generation)
    w.scan()
    assert w.coordinator.view(w.owner) != {}

    # (c) refused by epoch exhaustion
    real_view = w.coordinator.view

    def always_moving(owner):
        bump = w.authority.reserve_mutation(owner, cr.LIFECYCLE_SCAN_RECORD)
        w.authority.release_mutation(bump)
        return real_view(owner)

    w.coordinator.view = always_moving
    try:
        with pytest.raises(cr.Refusal) as caught:
            w.authority.acquire(w.owner, "p3", session_id=w.session, page_token=w.page,
                                scan_id=w.scan_id, identities=["i3"])
        assert caught.value.reason == cr.LIFECYCLE_EPOCH_MOVED
    finally:
        w.coordinator.view = real_view
    assert w.authority.acquiring_count() == 0

    # (d) an invalid owner raises before the marker is taken
    with pytest.raises(cr.Refusal):
        w.authority.acquire("", "p4", session_id=w.session, page_token=w.page,
                            scan_id=w.scan_id, identities=["i4"])
    assert w.authority.acquiring_count() == 0

    # Quiescent, so a rebind is still possible. This is the property the leak would destroy.
    assert w.authority.begin_rebind() is True
    w.authority.end_rebind()


# ======================================================== Case D — initial and idempotent binding

def test_d_a_fresh_initial_binding_takes_no_barrier(tmp_path):
    """Case D — nothing is detached, so nothing can be orphaned and no barrier is warranted.

    This is why ``gui.py``'s import-time bind cannot be made to refuse, which matters: a startup
    path that can fail closed on a safety check it does not need is an outage, not a safeguard.
    """
    registry = cr.ClipRegistry()
    coordinator = cr.ScanCoordinator()
    store = cr.ReviewStore(os.path.join(str(tmp_path), "fresh"))
    assert registry.lifecycle_authority is None
    authority = cr.LifecycleAuthority()
    assert cr.bind_lifecycle_authority(registry=registry, store=store,
                                       coordinator=coordinator, authority=authority) is authority
    assert registry.lifecycle_authority is authority
    assert store.lifecycle_authority is authority
    assert coordinator.lifecycle_authority is authority
    assert authority._coordinator is coordinator
    assert authority.rebind_barriers == 0, "an initial attachment must not barrier anything"


def test_d_the_lazy_store_attachment_still_works(tmp_path):
    """Case D — the ``gui.py`` shape: registry and coordinator bound eagerly, store later."""
    registry = cr.ClipRegistry()
    coordinator = cr.ScanCoordinator()
    shared = cr.bind_lifecycle_authority(registry=registry, coordinator=coordinator,
                                         authority=cr.LifecycleAuthority())
    store = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    assert store.lifecycle_authority is None
    cr.bind_lifecycle_authority(store=store, authority=shared)
    assert store.lifecycle_authority is shared
    assert shared.rebind_barriers == 0


@pytest.mark.parametrize("hold", ["reservation", "lease"])
def test_d_same_authority_rebinding_stays_idempotent_under_live_activity(tmp_path, hold):
    """Case D — idempotence must survive both kinds of live activity.

    Rebinding an authority to itself detaches nothing, so refusing it would buy no safety and
    would break P1 startup and the lazy attachment. Under a reservation *and* under a lease.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = None
    reservation = None
    if hold == "lease":
        plan = w.lease()
        assert w.authority.has_any_lease() is True
    else:
        reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
        assert w.authority.has_any_pending() is True

    again = cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=w.authority)
    assert again is w.authority
    assert all(a is w.authority for a in _bindings(w))
    assert w.authority.rebind_barriers == 0, "idempotent rebinding must take no barrier"
    # The live activity is untouched.
    if hold == "lease":
        assert w.authority.holder(w.owner) == plan.plan_id
        w.store.end_transfer(plan.owner, plan.plan_id)
    else:
        assert w.authority.pending_count(w.owner) == 1
        assert w.authority.release_mutation(reservation) is True


def test_d_independent_worlds_do_not_block_each_others_rebinding(tmp_path):
    """Case D — the barrier is per-authority, so one reviewer-owned world's live reservation must
    not refuse an unrelated world's rebind. A process-wide flag would fail this."""
    first = World(tmp_path / "w1", count=1)
    second = World(tmp_path / "w2", count=1)
    assert first.authority is not second.authority

    with _PausedReservation(first, cr.LIFECYCLE_SESSION_REVOKE) as held:
        other = cr.LifecycleAuthority()
        # The unrelated world rebinds happily...
        assert cr.bind_lifecycle_authority(registry=second.registry, store=second.store,
                                           coordinator=second.coordinator,
                                           authority=other) is other
        assert all(a is other for a in _bindings(second))
        # ...while the world holding the reservation still refuses.
        with pytest.raises(cr.Refusal):
            cr.bind_lifecycle_authority(registry=first.registry, store=first.store,
                                        coordinator=first.coordinator,
                                        authority=cr.LifecycleAuthority())
        held.release()


# ======================================================== Case E — incomplete target sets

_TARGET_SETS = [
    ("registry",), ("store",), ("coordinator",),
    ("registry", "store"), ("registry", "coordinator"), ("store", "coordinator"),
    ("registry", "store", "coordinator"),
]


@pytest.mark.parametrize("names", _TARGET_SETS, ids=["+".join(n) for n in _TARGET_SETS])
def test_e_every_partial_target_set_refuses_while_a_reservation_is_pending(tmp_path, names):
    """Case E — all seven subsets, because a gate that only covers the full set is not a gate.

    Partial binding is the *production* arrangement (``gui.py`` binds registry and coordinator,
    then the store joins later), so it stays supported for initial attachment. What may not happen
    is a partial **replacement** that leaves one subsystem consulting a different gate from its
    siblings while a mutation is mid-flight.
    """
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    before_bindings = _bindings(w)
    subsystems = {"registry": w.registry, "store": w.store, "coordinator": w.coordinator}

    with _PausedReservation(w, cr.LIFECYCLE_SESSION_REVOKE) as held:
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(authority=usurper,
                                        **{n: subsystems[n] for n in names})
        assert caught.value.reason == cr.LIFECYCLE_REBIND_REFUSED
        # No subsystem moved -- not even one outside the requested set.
        assert _bindings(w) == before_bindings
        assert usurper._coordinator is None
        assert w.authority.is_rebinding() is False
        assert usurper.is_rebinding() is False
        held.release()

    # ...and the same partial set is accepted once the authority is quiescent again.
    cr.bind_lifecycle_authority(authority=usurper, **{n: subsystems[n] for n in names})
    for name in names:
        assert subsystems[name].lifecycle_authority is usurper


# ======================================================== release discipline

def test_a_reservation_release_is_by_identity_not_by_equality(tmp_path):
    """A release must free *exactly* the reservation it was given.

    ``MutationReservation`` is a frozen dataclass, so it has a generated ``__eq__`` and two
    reservations carrying the same four field values compare **equal** while being different
    objects. ``release_mutation`` matches with ``is not``, which is the whole protection — and the
    R5-P53 suite's own forged-token case cannot see it, because its forgery differs in
    ``operation`` and so fails an equality test too. Measured for R5-P55: relaxing ``is not`` to
    ``!=`` survives that suite untouched. This pins the field-for-field forgery, which is the only
    shape that distinguishes the two.
    """
    w = World(tmp_path, count=1)
    real = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)

    forged = cr.MutationReservation(real.owner, real.token, real.operation, real.epoch)
    assert forged == real, "the forgery must be equal, or it proves nothing about identity"
    assert forged is not real

    assert w.authority.release_mutation(forged) is False, (
        "an equal-but-distinct reservation released the real one")
    assert w.authority.pending_count(w.owner) == 1, "the real reservation must still be held"

    # ...and a transfer is still excluded, which is what the reservation was protecting.
    assert w.authority.has_any_pending() is True
    assert w.authority.begin_rebind() is False

    # The genuine object still releases, and only once.
    assert w.authority.release_mutation(real) is True
    assert w.authority.release_mutation(real) is False
    assert w.authority.pending_count(w.owner) == 0


# ======================================================== structural pins

def test_no_lifecycle_mutation_site_bypasses_the_reservation_protocol():
    """The R5-P53 pin, restated for R5-P55: the barrier is enforced *inside* the reservation and
    acquisition primitives, so it cannot be bypassed by a mutation site that forgets it.

    Asserted structurally because a behavioural test can only cover the sites that exist today.
    """
    tree = ast.parse(open(_CLIPS_REVIEW, encoding="utf-8").read())
    authority = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.ClassDef) and n.name == "LifecycleAuthority")
    methods = {n.name: n for n in authority.body if isinstance(n, ast.FunctionDef)}

    for name in ("begin_rebind", "end_rebind", "has_any_pending", "is_rebinding",
                 "acquiring_count"):
        assert name in methods, "%s is part of the F54-1 contract" % name

    def reads_rebinding(node):
        return any(isinstance(n, ast.Attribute) and n.attr == "_rebinding"
                   for n in ast.walk(node))

    # Every gate that may create live state must consult the barrier itself.
    for name in ("reserve_mutation", "try_reserve_mutation", "_reject_if_unavailable", "acquire"):
        assert reads_rebinding(methods[name]), (
            "%s may create or grant live state and must honour the rebinding barrier" % name)

    # The barrier must be tested in the shared predicate, which is what makes it apply at the
    # FINAL arbitration and not merely before the coordinator probe.
    assert reads_rebinding(methods["_reject_if_unavailable"])

    # `begin_rebind` must require all three quiescence conditions, not just the lease map.
    source = ast.get_source_segment(open(_CLIPS_REVIEW, encoding="utf-8").read(),
                                    methods["begin_rebind"])
    for fragment in ("_leases", "_pending", "_acquiring", "_rebinding"):
        assert fragment in source, "begin_rebind must consider %s" % fragment


def test_gui_binds_once_and_its_direct_assignment_is_only_an_initial_attachment():
    """The §7 bypass assessment, pinned.

    A direct ``target.lifecycle_authority = other`` assignment bypasses
    ``bind_lifecycle_authority`` entirely, so the only reason F54-1 was never reachable in the
    shipped application is that the one direct assignment ``gui.py`` performs is an *initial*
    attachment, on a store constructed three lines earlier whose authority is still ``None``.
    Initial attachment detaches nothing and therefore cannot orphan anything.

    That is a property of today's source, not of the API, so it is pinned here: if someone later
    adds a second binding call, or turns this assignment into a replacement, this test fails and
    the bypass gets re-assessed instead of shipping silently.
    """
    source = open(_GUI, encoding="utf-8").read()
    tree = ast.parse(source)

    binds = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "bind_lifecycle_authority"]
    assert len(binds) == 1, "gui.py must bind exactly once; found %d" % len(binds)

    assignments = [(n, t) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   for t in n.targets
                   if isinstance(t, ast.Attribute) and t.attr == "lifecycle_authority"]
    assert len(assignments) == 1, (
        "gui.py must contain exactly one direct lifecycle_authority assignment; found %d"
        % len(assignments))

    assign, target = assignments[0]
    # It assigns the process-wide default -- the same object the single bind call above binds.
    assert isinstance(assign.value, ast.Call)
    assert isinstance(assign.value.func, ast.Attribute)
    assert assign.value.func.attr == "default_lifecycle_authority"
    # Its target is a plain local, not a module global or a record field.
    assert isinstance(target.value, ast.Name)
    local = target.value.id

    enclosing = next(fn for fn in ast.walk(tree)
                     if isinstance(fn, ast.FunctionDef)
                     and any(node is assign for node in ast.walk(fn)))
    constructed = [n for n in ast.walk(enclosing) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == local for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and isinstance(n.value.func, ast.Attribute)
                   and n.value.func.attr == "ReviewStore"]
    assert constructed, (
        "the direct assignment must target a freshly constructed ReviewStore -- that is what "
        "makes it an initial attachment rather than a replacement, and therefore not a bypass")
    assert constructed[0].lineno < assign.lineno
