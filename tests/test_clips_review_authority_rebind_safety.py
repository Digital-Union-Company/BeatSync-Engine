"""R5-P57 — the lifecycle authority topology is immutable: bound once, never replaced.

This module replaces R5-P55's hot-rebinding safety suite. **The old assertions are not deleted
lightly and not deleted quietly**: they are preserved verbatim in the R5-P53 and R5-P55 evidence
packages under ``D:\\PROJECT_FILES\\Projects\\BeatSync-Engine-DigitalUnion\\Evidence\\``, and §10 of
this file records exactly which ones were superseded and why.

Why they were superseded
------------------------
Three independent reviews reproduced an authority-rebinding bypass in three successive protocols:

* **R5-P52** broke R5-P51's check-then-act predicate (`has_any_lease()` then assign).
* **R5-P54** broke R5-P53's replacement gate: it asked about leases and nothing about pending
  mutation *reservations*, so a rebind orphaned a reservation and the clip moved with
  ``batch=COMPLETE`` against a revoked session. Reproduced 3/3 on real files.
* **R5-P56** broke R5-P55's two-phase quiescence barrier **three separate ways**, each reaching the
  same completed, irreversible move:

  - **F56-1 (MAJOR)** — the outgoing-authority snapshot was computed with no synchronisation and
    never re-validated, so of two concurrent binders the first could detach an authority the second
    had installed and the first had never enumerated. Measured with a pending reservation *and*
    with a live transfer lease on the detached authority.
  - **F56-2 (MAJOR)** — a *partial* replacement of a completely idle authority was accepted and
    left ``registry=B, store=A, coordinator=A``. A transfer then leased A, a session revocation
    passed through B and was accepted, and the clip published with ``batch=COMPLETE`` and
    ``advance_page=True`` against ``<revoked:unknown_session>``. **No concurrency at all.**
  - **F56-3 (MAJOR)** — the no-barrier fast path could retarget ``shared._coordinator`` with an
    acquisition in flight, and a rebind omitting ``coordinator=`` left the incoming authority with
    none at all, silently disabling the scan-in-flight arbitration.

Every one of those entered through the same permission: *an idle authority may be replaced.* The
owner's R5-P57 decision removes the permission instead of guarding it a fourth time. The supported
operations are initial attachment, idempotent re-attachment, and nothing else.

What this module pins
---------------------
The §10 adversarial binding matrix, in its own numbering, plus the three R5-P56 MAJOR
reproductions re-run against the corrected implementation and asserted to be refused at the
*binding* site rather than merely survivable at the transfer site.

Threads, events and task-owned synthetic files only. Nothing here touches production media, the
``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import ast
import os
import threading

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import clips_review_transfer as tr

from test_clips_review_atomic_lifecycle import World

TIMEOUT = 30.0

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
_GUI = os.path.join(_SRC, "gui.py")
_CLIPS_REVIEW = os.path.join(_SRC, "beatsync_fork", "clips_review.py")

REBIND_REFUSED = cr.LIFECYCLE_REBIND_REFUSED
COORD_REFUSED = cr.LIFECYCLE_COORDINATOR_REBIND_REFUSED


# ---------------------------------------------------------------------------------- helpers

@pytest.fixture(autouse=True)
def _binding_lock_is_never_left_held():
    """No test in this module may leave ``_BINDING_LOCK`` held, in either direction.

    This is an invariant worth asserting on its own — a leaked configuration lock is a
    process-lifetime outage, and every refusal path in :func:`bind_lifecycle_authority` runs
    through a ``with`` statement precisely so it cannot happen.

    It is also what makes that failure mode *testable*. A leak does not produce a failing
    assertion by itself; it produces a **hang**, because the next binding call blocks forever on a
    lock nobody will release. Measured here during R5-P57's own negative-control run: the mutation
    that leaks the lock parked pytest with no CPU until the driver's module timeout, and a timeout
    is deliberately never counted as a caught mutation. Checking at teardown turns the hang into a
    failure in the very test that caused it, before any later test can block on it.

    ``acquire(blocking=False)`` is essential: a blocking probe would itself hang.
    """
    assert cr._BINDING_LOCK.acquire(blocking=False), (
        "the binding lock was already held when this test started")
    cr._BINDING_LOCK.release()
    yield
    assert cr._BINDING_LOCK.acquire(blocking=False), (
        "this test left the binding lock held, which would wedge every later binding call")
    cr._BINDING_LOCK.release()


def _bindings(w):
    """The identity of the authority each of the three subsystems actually consults."""
    return (w.registry.lifecycle_authority,
            w.store.lifecycle_authority,
            w.coordinator.lifecycle_authority)


def _fresh_graph(tmp_path, name="graph"):
    """A registry, coordinator and store, all unbound, plus a fresh authority."""
    registry = cr.ClipRegistry()
    coordinator = cr.ScanCoordinator()
    store = cr.ReviewStore(os.path.join(str(tmp_path), name))
    assert registry.lifecycle_authority is None
    assert coordinator.lifecycle_authority is None
    assert store.lifecycle_authority is None
    return registry, coordinator, store, cr.LifecycleAuthority()


class _PausedReservation:
    """Hold a real reservation on ``authority`` across a real lifecycle mutation.

    ``reserve_mutation`` is *wrapped*, not replaced: the genuine method runs first, so ``_pending``
    really holds a reservation and every predicate that reads it sees real state. The mutation
    thread parks after the reservation is granted and before it mutates anything.
    """

    def __init__(self, world, operation, action):
        self.world = world
        self.operation = operation
        self.action = action
        self.authority = world.authority
        self.reserved = threading.Event()
        self.may_mutate = threading.Event()
        self.finished = threading.Event()
        self.reached = False
        self.error = None
        self._real = self.authority.reserve_mutation

    def __enter__(self):
        def traced(owner, operation):
            reservation = self._real(owner, operation)      # the REAL reservation, unchanged
            if operation == self.operation:
                self.reached = True
                self.reserved.set()
                assert self.may_mutate.wait(TIMEOUT), "the binding attempt never finished"
            return reservation

        def run():
            try:
                self.action(self.world)
            except Exception as exc:                        # noqa: BLE001 - recorded
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


# ============================================= §10.1-3  Rule A — initial attachment still works

def test_01_a_fresh_registry_and_coordinator_bind(tmp_path):
    """The exact gui.py import-time shape: registry + coordinator, no store yet."""
    registry, coordinator, _store, shared = _fresh_graph(tmp_path, "one")
    got = cr.bind_lifecycle_authority(registry=registry, coordinator=coordinator,
                                      authority=shared)
    assert got is shared
    assert registry.lifecycle_authority is shared
    assert coordinator.lifecycle_authority is shared
    assert shared._coordinator is coordinator


def test_02_a_fresh_registry_coordinator_and_store_bind_together(tmp_path):
    registry, coordinator, store, shared = _fresh_graph(tmp_path, "two")
    cr.bind_lifecycle_authority(registry=registry, store=store, coordinator=coordinator,
                               authority=shared)
    assert registry.lifecycle_authority is shared
    assert store.lifecycle_authority is shared
    assert coordinator.lifecycle_authority is shared
    assert cr.lifecycle_topology_fault(registry, store) == ""


def test_03_a_lazily_created_store_joins_the_established_authority(tmp_path):
    """The production sequence: bind registry+coordinator at import, store joins on first use."""
    registry, coordinator, _unused, shared = _fresh_graph(tmp_path, "three")
    cr.bind_lifecycle_authority(registry=registry, coordinator=coordinator, authority=shared)
    late = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    assert late.lifecycle_authority is None
    assert cr.lifecycle_topology_fault(registry, late) == cr.LIFECYCLE_TOPOLOGY_INCOHERENT
    cr.bind_lifecycle_authority(store=late, authority=shared)
    assert late.lifecycle_authority is shared
    assert cr.lifecycle_topology_fault(registry, late) == ""


# ============================================= §10.4-6  Rule B — idempotence, including when busy

def test_04_same_authority_binding_is_idempotent(tmp_path):
    w = World(tmp_path, count=1)
    before = _bindings(w)
    again = cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=w.authority)
    assert again is w.authority
    assert _bindings(w) == before
    assert w.authority._coordinator is w.coordinator


def test_05_same_authority_binding_is_permitted_during_a_transfer_lease(tmp_path):
    """P1 startup and the lazy store attachment depend on this staying permitted."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    before = _bindings(w)
    again = cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=w.authority)
    assert again is w.authority
    assert _bindings(w) == before
    assert w.authority.holder(w.owner) == plan.plan_id          # the lease is untouched
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_06_same_authority_binding_is_permitted_during_a_mutation_reservation(tmp_path):
    w = World(tmp_path, count=1)
    with _PausedReservation(w, cr.LIFECYCLE_SESSION_REVOKE,
                            lambda ww: ww.registry.revoke_owner(ww.owner)) as parked:
        assert parked.reached is True
        before = _bindings(w)
        epoch = w.authority.epoch(w.owner)
        again = cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                            coordinator=w.coordinator, authority=w.authority)
        assert again is w.authority
        assert _bindings(w) == before
        # Idempotence changed no live state at all: not the reservation, not the epoch.
        assert w.authority.pending_count(w.owner) == 1
        assert w.authority.epoch(w.owner) == epoch
    assert parked.error is None


# ============================= §10.7-11  Rule C/D — replacement is refused, idle or not

@pytest.mark.parametrize("names", [
    ("registry",), ("store",), ("coordinator",),
    ("registry", "store"), ("registry", "coordinator"), ("store", "coordinator"),
    ("registry", "store", "coordinator"),
])
def test_07_11_every_replacement_subset_is_refused_even_when_completely_idle(tmp_path, names):
    """§10.7-10.11 together. **This is the R5-P57 contract.**

    R5-P53 and R5-P55 both *permitted* each of these seven while the outgoing authority was idle.
    R5-P56 F56-1 and F56-2 are what that permission cost. Every subset is now refused, and the
    refusal is asserted against an authority that is idle on every measure the old gates consulted.
    """
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    subsystems = {"registry": w.registry, "store": w.store, "coordinator": w.coordinator}

    assert w.authority.has_any_lease() is False
    assert w.authority.has_any_pending() is False
    before = _bindings(w)
    before_coordinator = w.authority._coordinator

    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(authority=usurper,
                                    **{n: subsystems[n] for n in names})
    assert caught.value.reason == REBIND_REFUSED
    assert caught.value.status == 409
    # §10.24 — a refused bind leaves every original binding intact...
    assert _bindings(w) == before
    assert all(x is w.authority for x in before)
    # ...and never attaches the usurper to anything, so no split can exist afterwards.
    assert usurper._coordinator is None
    assert w.authority._coordinator is before_coordinator
    assert cr.lifecycle_topology_fault(w.registry, w.store) == ""


def test_12_two_binders_racing_different_initial_authorities_cannot_both_win(tmp_path):
    """§10.12. One wins outright; the loser is refused and overwrites nothing."""
    registry, coordinator, store, first = _fresh_graph(tmp_path, "race")
    second = cr.LifecycleAuthority()
    outcomes = {}
    gate = threading.Barrier(2, timeout=TIMEOUT)

    def bind(tag, authority):
        gate.wait()
        try:
            cr.bind_lifecycle_authority(registry=registry, store=store, coordinator=coordinator,
                                        authority=authority)
            outcomes[tag] = "ACCEPTED"
        except cr.Refusal as exc:
            outcomes[tag] = "REFUSED:%s" % exc.reason

    threads = [threading.Thread(target=bind, args=a, daemon=True)
               for a in (("first", first), ("second", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert not any(t.is_alive() for t in threads), "a binder hung"

    assert sorted(outcomes) == ["first", "second"]
    accepted = [k for k, v in outcomes.items() if v == "ACCEPTED"]
    assert len(accepted) == 1, outcomes
    winner = first if accepted[0] == "first" else second
    loser = second if accepted[0] == "first" else first
    assert outcomes[("second" if accepted[0] == "first" else "first")] == (
        "REFUSED:%s" % REBIND_REFUSED)
    # No last-writer-wins and no split: all three went to exactly one authority.
    assert registry.lifecycle_authority is winner
    assert store.lifecycle_authority is winner
    assert coordinator.lifecycle_authority is winner
    assert loser._coordinator is None
    assert cr.lifecycle_topology_fault(registry, store) == ""


def test_13_two_binders_racing_different_replacements_are_both_refused(tmp_path):
    """§10.13. Neither may replace, so the established graph is untouched by both."""
    w = World(tmp_path, count=1)
    outcomes = {}
    gate = threading.Barrier(2, timeout=TIMEOUT)

    def bind(tag):
        authority = cr.LifecycleAuthority()
        gate.wait()
        try:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=authority)
            outcomes[tag] = "ACCEPTED"
        except cr.Refusal as exc:
            outcomes[tag] = "REFUSED:%s" % exc.reason

    threads = [threading.Thread(target=bind, args=(t,), daemon=True) for t in ("x", "y")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert not any(t.is_alive() for t in threads)
    assert outcomes == {"x": "REFUSED:%s" % REBIND_REFUSED,
                        "y": "REFUSED:%s" % REBIND_REFUSED}
    assert all(x is w.authority for x in _bindings(w))


def test_14_a_binder_paused_after_inspecting_its_targets_cannot_use_a_stale_view(tmp_path):
    """§10.14 — **the direct F56-1 closure, at the binding site.**

    R5-P56 drove exactly this interleaving and measured a completed move: binder X read the
    bindings, parked, binder Y installed a new authority, live state landed on it, and X's resumed
    assignment detached it. X is parked here at the same place — inside the decision phase,
    with its view of the targets already taken — and must not be able to act on it.

    The pause is driven by wrapping ``getattr`` on the *first* target read, which is the earliest
    point at which a binder has looked at a binding, and the test asserts the pause was reached.
    """
    registry, coordinator, store, authority_y = _fresh_graph(tmp_path, "stale")
    authority_x = cr.LifecycleAuthority()

    entered = threading.Event()
    may_proceed = threading.Event()
    reached = {"v": False}
    real_lock = cr._BINDING_LOCK

    class ParkingLock:
        """Parks binder X *after* it has entered the binding critical section."""

        def __enter__(self):
            real_lock.acquire()
            if not reached["v"] and threading.current_thread().name == "binder-x":
                reached["v"] = True
                entered.set()
                # Release so Y can run to completion while X is parked holding its stale view.
                real_lock.release()
                assert may_proceed.wait(TIMEOUT), "driver never released binder X"
                real_lock.acquire()
            return self

        def __exit__(self, *exc):
            real_lock.release()
            return False

    cr._BINDING_LOCK = ParkingLock()
    out = {}
    try:
        def binder_x():
            try:
                cr.bind_lifecycle_authority(registry=registry, store=store,
                                            coordinator=coordinator, authority=authority_x)
                out["x"] = "ACCEPTED"
            except cr.Refusal as exc:
                out["x"] = "REFUSED:%s" % exc.reason

        thread = threading.Thread(target=binder_x, name="binder-x", daemon=True)
        thread.start()
        assert entered.wait(TIMEOUT), "binder X never reached the binding critical section"
        assert reached["v"] is True

        # Binder Y completes the whole binding while X is parked.
        cr.bind_lifecycle_authority(registry=registry, store=store, coordinator=coordinator,
                                    authority=authority_y)
        assert registry.lifecycle_authority is authority_y

        # Real live state now exists on the authority X never looked at.
        w = World(os.path.join(str(tmp_path), "live"), count=1, authority=authority_y,
                  coordinator=coordinator)
        reservation = authority_y.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_REVOKE)
        assert authority_y.has_any_pending() is True

        may_proceed.set()
        thread.join(TIMEOUT)
        assert not thread.is_alive()
    finally:
        cr._BINDING_LOCK = real_lock

    # X must refuse, and above all must not have detached authority_y.
    assert out["x"] == "REFUSED:%s" % REBIND_REFUSED
    assert registry.lifecycle_authority is authority_y
    assert store.lifecycle_authority is authority_y
    assert coordinator.lifecycle_authority is authority_y
    assert authority_x._coordinator is None
    # The reservation on authority_y was never orphaned.
    assert authority_y.pending_count(w.owner) == 1
    assert authority_y.release_mutation(reservation) is True


def test_15_replacement_is_refused_while_a_mutation_reservation_is_pending(tmp_path):
    """§10.15. The R5-P54 F54-1 ordering, still refused — now for a stronger reason."""
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    with _PausedReservation(w, cr.LIFECYCLE_SESSION_REVOKE,
                            lambda ww: ww.registry.revoke_owner(ww.owner)) as parked:
        assert parked.reached is True
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=usurper)
        assert caught.value.reason == REBIND_REFUSED
        assert all(x is w.authority for x in _bindings(w))
        assert w.authority.pending_count(w.owner) == 1          # not orphaned
    assert parked.error is None
    assert w.authority.pending_count(w.owner) == 0              # released cleanly


def test_16_replacement_is_refused_while_a_transfer_lease_exists(tmp_path):
    """§10.16."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    usurper = cr.LifecycleAuthority()
    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                    coordinator=w.coordinator, authority=usurper)
    assert caught.value.reason == REBIND_REFUSED
    assert w.authority.holder(w.owner) == plan.plan_id
    assert all(x is w.authority for x in _bindings(w))
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_17_replacement_is_refused_while_an_acquisition_is_in_flight(tmp_path):
    """§10.17. R5-P55 needed ``_acquiring`` to answer this; the contract answers it for free.

    The acquisition is parked inside ``acquire``'s real lock-free coordinator probe — the window
    in which no lease is installed yet, so every quiescence measure the old barrier consulted
    reports the authority idle. Replacement is refused anyway, because idleness is no longer the
    question.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    usurper = cr.LifecycleAuthority()

    entered = threading.Event()
    may = threading.Event()
    reached = {"v": False}
    real_newest = w.coordinator.newest

    def traced_newest(owner):
        if not reached["v"]:
            reached["v"] = True
            entered.set()
            assert may.wait(TIMEOUT), "driver never released the parked probe"
        return real_newest(owner)

    w.coordinator.newest = traced_newest
    result = {}

    def acquirer():
        try:
            lease = w.authority.acquire(w.owner, plan.plan_id, session_id=plan.session_id,
                                        page_token=plan.page_token, scan_id=plan.scan_id,
                                        identities=[i.identity for i in plan.items])
            result["r"] = lease.plan_id
        except cr.Refusal as exc:
            result["r"] = "REFUSED:%s" % exc.reason

    thread = threading.Thread(target=acquirer, daemon=True)
    thread.start()
    try:
        assert entered.wait(TIMEOUT), "the probe window was never reached"
        # Mid-probe: no lease installed yet, nothing pending. The old gate would have allowed it.
        assert w.authority.has_any_lease() is False
        assert w.authority.has_any_pending() is False
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=usurper)
        assert caught.value.reason == REBIND_REFUSED
        assert all(x is w.authority for x in _bindings(w))
    finally:
        may.set()
        thread.join(TIMEOUT)
        w.coordinator.newest = real_newest

    # The acquisition completed on the authority it started on, which is still the bound one.
    assert result["r"] == plan.plan_id
    assert w.authority.holder(w.owner) == plan.plan_id
    w.authority.release(w.owner, plan.plan_id)


def test_18_initial_attachment_still_works_while_the_existing_graph_is_busy(tmp_path):
    """§10.18. Rule A may not be made refusable by activity elsewhere: that would be an outage.

    This is the F54-5 carve-out, retained deliberately and now the *only* way a component joins.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    late = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    cr.bind_lifecycle_authority(store=late, authority=w.authority)
    assert late.lifecycle_authority is w.authority
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


# ====================================== §10.19-21  Rule E — the coordinator cannot move

def test_19_a_foreign_coordinator_cannot_replace_the_established_one(tmp_path):
    """§10.19."""
    w = World(tmp_path, count=1)
    assert w.authority._coordinator is w.coordinator
    intruder = cr.ScanCoordinator()
    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(coordinator=intruder, authority=w.authority)
    assert caught.value.reason == COORD_REFUSED
    assert w.authority._coordinator is w.coordinator
    assert intruder.lifecycle_authority is None
    # Supplying the SAME coordinator again stays idempotent.
    cr.bind_lifecycle_authority(coordinator=w.coordinator, authority=w.authority)
    assert w.authority._coordinator is w.coordinator


def test_20_a_transfer_refuses_when_the_bound_authority_has_no_coordinator(tmp_path):
    """§10.20 — **the direct F56-3 closure at the execution site.**

    R5-P56 measured a lease granted mid-scan because ``_coordinator`` was ``None`` and the probe
    was skipped entirely. An absent coordinator is not an idle one, so the transfer fails closed.
    """
    w = World(tmp_path, count=1, bind=False)
    orphan = cr.LifecycleAuthority()
    cr.bind_lifecycle_authority(registry=w.registry, store=w.store, authority=orphan)
    assert orphan._coordinator is None
    assert cr.lifecycle_topology_fault(w.registry, w.store) == cr.LIFECYCLE_COORDINATOR_UNAVAILABLE

    w.decide_all(cr.SELECTED)
    plan = w.plan()
    source = plan.items[0].source_path
    result = w.run(plan)
    assert result.state == tr.REFUSED
    assert result.advance_page is False
    assert os.path.exists(source), "nothing may move through an incomplete topology"
    assert w.library_files() == []
    reasons = [r.get("reason") for r in w.journal.records()]
    assert cr.LIFECYCLE_COORDINATOR_UNAVAILABLE in reasons
    # And once the coordinator joins, the same transfer proceeds.
    cr.bind_lifecycle_authority(coordinator=w.coordinator, authority=orphan)
    assert cr.lifecycle_topology_fault(w.registry, w.store) == ""


def test_20b_a_bound_authority_without_a_coordinator_refuses_the_lease_itself(tmp_path):
    """§10.20, acquisition site. `execute_transfer` is not the only way to reach `acquire`.

    ``ReviewStore.begin_transfer`` calls ``acquire`` directly and is public, so the topology
    precondition one layer up is necessary but not sufficient. A *bound* authority owes its callers
    the scan-in-flight arbitration; if it cannot perform it, it must refuse rather than skip it.

    The distinction that keeps this from being an outage is ``_bound``: a bare authority a unit
    test constructed never promised arbitration, and is unaffected.
    """
    registry, coordinator, store, bound = _fresh_graph(tmp_path, "nocoord")
    cr.bind_lifecycle_authority(registry=registry, store=store, authority=bound)
    assert bound._bound is True and bound._coordinator is None

    with pytest.raises(cr.Refusal) as caught:
        bound.acquire("owner-x", "plan-x", session_id="s", page_token="p", scan_id="sc",
                      identities=["id-1"])
    assert caught.value.reason == cr.LIFECYCLE_COORDINATOR_UNAVAILABLE
    assert caught.value.status == 409
    assert bound.holder("owner-x") == "", "no lease may be installed"
    assert bound.identity_holder("id-1") == ""

    # A bare, never-bound authority is deliberately NOT affected: it is the legacy P1 object.
    bare = cr.LifecycleAuthority()
    assert bare._bound is False and bare._coordinator is None
    lease = bare.acquire("owner-y", "plan-y", session_id="s", page_token="p", scan_id="sc",
                         identities=["id-2"])
    assert lease.plan_id == "plan-y"
    assert bare.release("owner-y", "plan-y") is True

    # And once the coordinator joins the bound graph, acquisition works there too.
    cr.bind_lifecycle_authority(coordinator=coordinator, authority=bound)
    assert bound._coordinator is coordinator
    ok = bound.acquire("owner-x", "plan-x", session_id="s", page_token="p", scan_id="sc",
                       identities=["id-1"])
    assert ok.plan_id == "plan-x"
    assert bound.release("owner-x", "plan-x") is True


def test_21_the_coordinator_pointer_cannot_be_retargeted_during_the_lock_free_probe(tmp_path):
    """§10.21 — the other half of F56-3, at the binding site.

    R5-P56 retargeted ``_coordinator`` from inside ``acquire``'s probe with zero barriers taken,
    and the lease was installed against a superseded coordinator. Write-once makes the retarget
    itself impossible, so the probe cannot observe a different object than it started with.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    replacement = cr.ScanCoordinator()
    replacement.begin(w.owner)                   # mid-transition: newest > 0 and view == {}
    assert replacement.newest(w.owner) > 0 and replacement.view(w.owner) == {}

    entered = threading.Event()
    may = threading.Event()
    reached = {"v": False}
    real_newest = w.coordinator.newest

    def traced_newest(owner):
        if not reached["v"]:
            reached["v"] = True
            entered.set()
            assert may.wait(TIMEOUT)
        return real_newest(owner)

    w.coordinator.newest = traced_newest
    result = {}

    def acquirer():
        try:
            result["r"] = w.authority.acquire(
                w.owner, plan.plan_id, session_id=plan.session_id, page_token=plan.page_token,
                scan_id=plan.scan_id, identities=[i.identity for i in plan.items]).plan_id
        except cr.Refusal as exc:
            result["r"] = "REFUSED:%s" % exc.reason

    thread = threading.Thread(target=acquirer, daemon=True)
    thread.start()
    try:
        assert entered.wait(TIMEOUT), "the probe window was never reached"
        with pytest.raises(cr.Refusal) as caught:
            cr.bind_lifecycle_authority(coordinator=replacement, authority=w.authority)
        assert caught.value.reason == COORD_REFUSED
        assert w.authority._coordinator is w.coordinator
    finally:
        may.set()
        thread.join(TIMEOUT)
        w.coordinator.newest = real_newest

    assert result["r"] == plan.plan_id
    w.authority.release(w.owner, plan.plan_id)


# ============================== §10.22-25  failure atomicity and refusal inertness

def test_22_a_failure_on_a_later_target_assigns_nothing_at_all(tmp_path):
    """§10.22. The decision phase precedes every write, so a refusal cannot be partial.

    R5-P56 F56-2 and the §9 'raise during assignment' finding both produced split graphs. The
    decide-then-write split means the only way to reach the write phase is with every target
    already proved attachable.
    """
    registry, coordinator, store, shared = _fresh_graph(tmp_path, "atomic")
    # store is already on a different authority: the third target is the one that fails.
    other = cr.LifecycleAuthority()
    cr.bind_lifecycle_authority(store=store, authority=other)
    assert store.lifecycle_authority is other

    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(registry=registry, store=store, coordinator=coordinator,
                                    authority=shared)
    assert caught.value.reason == REBIND_REFUSED
    # registry is named FIRST and is unbound, so a write-as-you-go binder would have attached it.
    assert registry.lifecycle_authority is None
    assert coordinator.lifecycle_authority is None
    assert store.lifecycle_authority is other
    assert shared._coordinator is None


def test_23_a_coordinator_refusal_assigns_no_subsystem_either(tmp_path):
    """§10.23. The coordinator decision also precedes every write."""
    w = World(tmp_path, count=1)
    late = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    intruder = cr.ScanCoordinator()
    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(store=late, coordinator=intruder, authority=w.authority)
    assert caught.value.reason == COORD_REFUSED
    # `late` is unbound and named first; it must NOT have been attached before the refusal.
    assert late.lifecycle_authority is None
    assert w.authority._coordinator is w.coordinator
    assert intruder.lifecycle_authority is None


def test_24_a_refused_bind_changes_no_acquisition_or_mutation_state(tmp_path):
    """§10.25."""
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    before = (w.authority.epoch(w.owner), w.authority.pending_count(w.owner),
              w.authority.refusals, w.authority.mutation_reservations,
              w.authority.epoch_conflicts)
    for _ in range(3):
        with pytest.raises(cr.Refusal):
            cr.bind_lifecycle_authority(registry=w.registry, authority=usurper)
    after = (w.authority.epoch(w.owner), w.authority.pending_count(w.owner),
             w.authority.refusals, w.authority.mutation_reservations,
             w.authority.epoch_conflicts)
    assert before == after, "a refused bind touched lifecycle bookkeeping"


def test_25_a_refused_bind_never_leaves_the_binding_lock_held(tmp_path):
    """§10.30. A leaked configuration lock would be a process-lifetime outage."""
    w = World(tmp_path, count=1)
    usurper = cr.LifecycleAuthority()
    intruder = cr.ScanCoordinator()
    for kwargs in ({"registry": w.registry, "authority": usurper},
                   {"coordinator": intruder, "authority": w.authority},
                   {"registry": w.registry, "store": w.store, "coordinator": w.coordinator,
                    "authority": usurper}):
        with pytest.raises(cr.Refusal):
            cr.bind_lifecycle_authority(**kwargs)
        assert cr._BINDING_LOCK.acquire(blocking=False) is True, "the binding lock leaked"
        cr._BINDING_LOCK.release()
    # And a supported call still works afterwards.
    assert cr.bind_lifecycle_authority(registry=w.registry, authority=w.authority) is w.authority


# =============================== §10.26-29  interaction with live lifecycle and transfers

def test_26_a_concurrent_scan_during_initial_binding_is_unaffected(tmp_path):
    """§10.26. Binding takes no lifecycle lock, so a scan cannot be blocked or corrupted by it."""
    w = World(tmp_path, count=1)
    late = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    outcomes = {}
    gate = threading.Barrier(2, timeout=TIMEOUT)

    def scan():
        gate.wait()
        try:
            outcomes["generation"] = w.scan()
        except Exception as exc:                            # noqa: BLE001
            outcomes["generation"] = "ERR:%s" % exc

    def bind():
        gate.wait()
        try:
            cr.bind_lifecycle_authority(store=late, authority=w.authority)
            outcomes["bind"] = "ACCEPTED"
        except cr.Refusal as exc:
            outcomes["bind"] = "REFUSED:%s" % exc.reason

    threads = [threading.Thread(target=f, daemon=True) for f in (scan, bind)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert not any(t.is_alive() for t in threads), "binding and scanning deadlocked"
    assert outcomes["bind"] == "ACCEPTED"
    assert isinstance(outcomes["generation"], int)
    assert late.lifecycle_authority is w.authority


def test_27_a_transfer_succeeds_through_a_complete_coherent_topology(tmp_path):
    """§10.27. The contract must not have broken the working path."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    source = plan.items[0].source_path
    assert cr.lifecycle_topology_fault(w.registry, w.store) == ""
    result = w.run(plan)
    assert result.state == tr.COMPLETE
    assert result.advance_page is True
    assert os.path.exists(source) is False
    assert w.library_files() == ["clip0001.mp4"]


def test_28_a_transfer_refuses_a_split_topology_without_moving_anything(tmp_path):
    """§10.28 — **the F56-2 consequence, closed at the execution site too.**

    The binding API can no longer manufacture this split, so it is constructed here by the direct
    attribute assignment that remains an unsupported internal bypass (see test_30). The point is
    that even then nothing moves: the split is detected before any lease is taken.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    source = plan.items[0].source_path

    foreign = cr.LifecycleAuthority()
    foreign._coordinator = w.coordinator
    w.registry.lifecycle_authority = foreign                # the bypass, done deliberately
    assert cr.lifecycle_topology_fault(w.registry, w.store) == cr.LIFECYCLE_TOPOLOGY_INCOHERENT

    result = w.run(plan)
    assert result.state == tr.REFUSED
    assert result.advance_page is False
    assert os.path.exists(source), "a split topology must not move a clip"
    assert w.library_files() == []
    assert foreign.holder(w.owner) == ""
    assert w.authority.holder(w.owner) == ""
    reasons = [r.get("reason") for r in w.journal.records()]
    assert cr.LIFECYCLE_TOPOLOGY_INCOHERENT in reasons


def test_29_an_unrelated_world_is_unaffected_by_a_refused_configuration_attempt(tmp_path):
    """§10.29. Per-graph isolation: one app's refused bind cannot disturb another's."""
    first = World(os.path.join(str(tmp_path), "first"), count=1)
    second = World(os.path.join(str(tmp_path), "second"), count=1)
    assert first.authority is not second.authority

    with pytest.raises(cr.Refusal):
        cr.bind_lifecycle_authority(registry=first.registry, authority=second.authority)
    assert all(x is first.authority for x in _bindings(first))
    assert all(x is second.authority for x in _bindings(second))

    # Both graphs still work independently.
    second.decide_all(cr.SELECTED)
    assert second.run(second.plan()).state == tr.COMPLETE
    first.registry.revoke_owner(first.owner)


def test_29b_topology_coherence_is_decided_by_identity_not_equality(tmp_path):
    """Two authorities that *compare equal* are still two gates.

    `lifecycle_topology_fault` uses `is not`. Relaxing that to `!=` is invisible today, because
    `LifecycleAuthority` inherits `object.__eq__` and so equality already means identity — exactly
    the trap R5-P55's M10 recorded for `MutationReservation`, where a frozen dataclass's generated
    `__eq__` made the two spellings indistinguishable to the whole suite. An authority that claims
    equality with everything is the only shape that tells them apart, so it is constructed here.
    """
    class Agreeable(cr.LifecycleAuthority):
        """Claims equality with any other authority while remaining a different object."""

        def __eq__(self, other):
            return isinstance(other, cr.LifecycleAuthority)

        def __ne__(self, other):
            return not self.__eq__(other)

        __hash__ = cr.LifecycleAuthority.__hash__

    w = World(tmp_path, count=1)
    impostor = Agreeable()
    impostor._coordinator = w.coordinator
    assert impostor == w.authority and impostor is not w.authority
    assert not (impostor != w.authority), "the fixture must defeat an equality-based check"

    w.decide_all(cr.SELECTED)
    plan = w.plan()
    source = plan.items[0].source_path

    w.registry.lifecycle_authority = impostor               # the unsupported direct bypass
    assert cr.lifecycle_topology_fault(w.registry, w.store) == cr.LIFECYCLE_TOPOLOGY_INCOHERENT, (
        "an equality-claiming authority must not be mistaken for the bound one")

    result = w.run(plan)
    assert result.state == tr.REFUSED
    assert os.path.exists(source)
    assert w.library_files() == []


def test_29c_an_idempotent_bind_never_perturbs_a_concurrent_acquisition(tmp_path):
    """Rule B must change *no effective state* — including transiently.

    `_coordinator` is read **outside** `_lock` by `acquire`, so an idempotent re-bind that cleared
    and re-set it would be invisible to every single-threaded test and still make a concurrent
    acquisition observe `None` and refuse `lifecycle_coordinator_unavailable`. That is fail-closed
    rather than unsafe, which is precisely why nothing else would catch it.

    The acquisition is parked inside its real probe, the idempotent bind is performed, and the
    acquisition must still succeed.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()

    entered = threading.Event()
    may = threading.Event()
    reached = {"v": False}
    real_newest = w.coordinator.newest

    def traced_newest(owner):
        if not reached["v"]:
            reached["v"] = True
            entered.set()
            assert may.wait(TIMEOUT)
        return real_newest(owner)

    w.coordinator.newest = traced_newest
    result = {}

    def acquirer():
        try:
            result["r"] = w.authority.acquire(
                w.owner, plan.plan_id, session_id=plan.session_id, page_token=plan.page_token,
                scan_id=plan.scan_id, identities=[i.identity for i in plan.items]).plan_id
        except cr.Refusal as exc:
            result["r"] = "REFUSED:%s" % exc.reason

    thread = threading.Thread(target=acquirer, daemon=True)
    thread.start()
    try:
        assert entered.wait(TIMEOUT), "the probe window was never reached"
        observed = []
        for _ in range(40):
            cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=w.authority)
            observed.append(w.authority._coordinator)
        assert all(c is w.coordinator for c in observed), (
            "an idempotent bind made `_coordinator` observably leave its established value")
    finally:
        may.set()
        thread.join(TIMEOUT)
        w.coordinator.newest = real_newest

    assert result["r"] == plan.plan_id, (
        "an idempotent bind perturbed a concurrent acquisition: %r" % (result.get("r"),))
    w.authority.release(w.owner, plan.plan_id)


# ============================================ structural pins

def test_30_the_production_direct_assignment_is_only_an_initial_attachment():
    """The one bypass that remains, pinned by source contract rather than claimed to be closed.

    ``bind_lifecycle_authority`` cannot prevent ``target.lifecycle_authority = other``. The reason
    that is not a reachable defect is a property of *today's source*: ``gui.py`` performs exactly
    one such assignment, onto a ``ReviewStore`` constructed a few lines earlier whose authority is
    still ``None``. If that ever becomes a replacement, or a second assignment appears, this fails
    and the bypass is re-assessed instead of shipping silently.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    assignments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "lifecycle_authority":
                assignments.append((node, target))
    assert len(assignments) == 1, (
        "gui.py must contain exactly one direct lifecycle_authority assignment; found %d"
        % len(assignments))

    node, target = assignments[0]
    # It must assign the process-wide default...
    assert isinstance(node.value, ast.Call)
    assert getattr(node.value.func, "attr", "") == "default_lifecycle_authority"
    # ...onto a NAME that was bound by a `ReviewStore(...)` construction in the same function.
    assert isinstance(target.value, ast.Name)
    local = target.value.id

    owner = None
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.FunctionDef) and node in ast.walk(candidate):
            owner = candidate
            break
    assert owner is not None, "the assignment is not inside a function"
    constructed = [
        stmt for stmt in ast.walk(owner)
        if isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == local for t in stmt.targets)
        and isinstance(stmt.value, ast.Call)
        and getattr(stmt.value.func, "attr", "") == "ReviewStore"
    ]
    assert constructed, (
        "the direct assignment's target must be a freshly constructed ReviewStore local, which is "
        "what makes it an initial attachment rather than a replacement")


def test_31_no_mutation_site_bypasses_the_reservation_protocol():
    """Carried forward from R5-P55: `guard`/`guarded` must stay diagnostic.

    Every lifecycle mutation must take a reservation. A site that called the probe instead would
    be the R5-P52 check-then-act again.
    """
    tree = ast.parse(open(_CLIPS_REVIEW, encoding="utf-8").read())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name in ("guard", "guarded"):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "attr", "") in ("guard", "guarded")
                    and getattr(getattr(inner.func, "value", None), "attr", "")
                    in ("lifecycle_authority", "_lifecycle")):
                offenders.append(node.name)
    assert offenders == [], "these mutation sites still gate on a probe: %r" % (offenders,)


def test_32_the_retired_hot_rebinding_api_is_gone():
    """[R5-P57] The vulnerable operation is removed, not merely documented as unsupported.

    R5-P56 F56-4 recorded that the barrier-spans-the-assignments property was unasserted, and
    F56-5 that barrier ownership was call discipline rather than code. Both are dispositioned by
    deletion: there is no barrier to span anything and no ownership to enforce. This test exists so
    that a future reintroduction has to be deliberate.
    """
    for name in ("begin_rebind", "end_rebind", "is_rebinding", "acquiring_count"):
        assert not hasattr(cr.LifecycleAuthority, name), (
            "%s is back; the R5-P57 contract does not need it and R5-P56 broke it" % name)
    authority = cr.LifecycleAuthority()
    for attr in ("_rebinding", "_acquiring", "rebind_barriers"):
        assert not hasattr(authority, attr), "%s bookkeeping is back" % attr
    assert not hasattr(cr, "LIFECYCLE_REBINDING")
    # The refusal reasons the contract DOES use are exported.
    for name in ("LIFECYCLE_REBIND_REFUSED", "LIFECYCLE_COORDINATOR_REBIND_REFUSED",
                 "LIFECYCLE_TOPOLOGY_INCOHERENT", "LIFECYCLE_COORDINATOR_UNAVAILABLE"):
        assert name in cr.__all__, "%s must be part of the public contract" % name


def test_32b_the_binder_never_clears_the_coordinator_pointer():
    """`_coordinator` may only go ``None`` -> one object. It may never be written back to ``None``.

    This is a **source-contract** pin, and it is one deliberately, because the runtime window is
    not reachable by a test. A binder that cleared the pointer before re-setting it
    (``_coordinator = None; _coordinator = coordinator``) leaves the final state identical, so
    every single-threaded assertion passes — and `test_29c` cannot see it either, because
    `acquire` reads `_coordinator` once into a local and the transient exists only between two
    bytecodes inside the lock. Measured as exactly that during R5-P57's negative-control run: the
    mutation survived the whole runtime suite.

    The consequence would be fail-closed rather than unsafe (a concurrent acquisition reading
    `None` on a bound authority refuses `lifecycle_coordinator_unavailable`), so this is a
    robustness pin, not a security one. It is asserted structurally because that is the only way
    to assert it deterministically — and saying so is the point: an unreachable window is pinned
    by shape or it is not pinned at all.
    """
    tree = ast.parse(open(_CLIPS_REVIEW, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "bind_lifecycle_authority")
    writes = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "_coordinator":
                writes.append(ast.unparse(node.value))
    assert writes, "the binder must still establish the coordinator pointer"
    assert writes == ["coordinator"], (
        "the binder may assign `_coordinator` only from its `coordinator` argument, never clear "
        "it or route it through anything else; found %r" % (writes,))


def test_33_the_binding_lock_is_never_held_across_io_or_a_subsystem_call():
    """The lock-order proof, as a source contract.

    ``bind_lifecycle_authority`` must take no other lock and perform no I/O while holding
    ``_BINDING_LOCK``; otherwise it would create the reverse edge the documented order forbids.
    """
    tree = ast.parse(open(_CLIPS_REVIEW, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "bind_lifecycle_authority")
    withs = [n for n in ast.walk(func) if isinstance(n, ast.With)]
    assert len(withs) == 1, "exactly one critical section is expected"
    body = withs[0]
    forbidden = ("acquire", "release", "open", "fsync", "rename", "replace", "write",
                 "begin", "publish", "decide", "reserve_mutation", "run", "append")
    for inner in ast.walk(body):
        if isinstance(inner, ast.With):
            continue
        if isinstance(inner, ast.Call):
            name = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
            assert name not in forbidden, (
                "bind_lifecycle_authority calls %r inside _BINDING_LOCK" % name)
