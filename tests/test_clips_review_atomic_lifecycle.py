"""R5-P53 — atomic lifecycle mutation reservations and epoch-validated transfer acquisition.

Independent review R5-P52 reproduced two MAJOR races against the R5-P51 candidate and returned
``R5_P52_PR75_CHANGES_REQUIRED``:

**F53-A — the reverse-order lifecycle guard race.** Every guarded lifecycle site was a
check-then-act: ``guard()`` took the authority lock, read the lease map and *released* it, and the
mutation then happened under a different lock. ``acquire()`` took no subsystem lock at all. So on
all five operations — ``ScanCoordinator.begin``, ``ReviewStore.begin_scan``,
``ClipRegistry.new_session``, ``ClipRegistry.publish_page``, ``ClipRegistry.revoke_owner`` — the
ordering *guard succeeds → transfer acquires → transfer revalidates → mutation executes →
transfer publishes* was accepted: the clip moved, the batch reported ``COMPLETE`` with
``advance_page=True``, and the authority the operator was acting under had been destroyed (gallery
blank, session revoked, page revoked, or ``current_scan`` replaced).

**F53-B — the coordinator probe/acquire race.** ``acquire()`` probed ``ScanCoordinator`` outside
its own lock and only then recorded the lease, so a scan taking a generation in between left the
transfer holding a lease in exactly the state the ``scan_in_flight`` probe exists to refuse.

The correction is a **reservation**, not a tighter check. A lifecycle mutation reserves its right
to mutate at the same arbitration point that installs a transfer lease, and holds that reservation
*across* the mutation; a transfer refuses while any reservation is pending, and a reservation
refuses while a lease is held. Granting a reservation bumps a per-owner monotonic epoch, and
``acquire`` re-validates that epoch after its lock-free probe, so a stale probe can never be
accepted.

Threads, barriers and task-owned synthetic files only. Nothing here touches production media, the
``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import hashlib
import os
import threading

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import clips_review_transfer as tr

OWNER = "r5p53-owner"
OTHER = "r5p53-other"
TIMEOUT = 30.0


# ---------------------------------------------------------------------------------- fixtures

def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _payload(index, size=4096):
    return (bytes([index % 251]) + bytes(range(256))) * (size // 257 + 1)


class World:
    """A full P1 world wired the way ``gui.py`` wires it."""

    def __init__(self, root, count=1, owner=OWNER, bind=True, coordinator=None,
                 authority=None, max_owners=cr.MAX_OWNERS, clip_size=4096):
        root = str(root)
        self.owner = owner
        self.review = os.path.join(root, "review")
        self.library = os.path.join(root, "library")
        self.rejected = os.path.join(root, "quarantine")
        for index in range(1, count + 1):
            _write(os.path.join(self.review, "clip%04d.mp4" % index), _payload(index, clip_size))
        os.makedirs(self.library, exist_ok=True)
        os.makedirs(self.rejected, exist_ok=True)
        self.coordinator = coordinator or cr.ScanCoordinator(max_owners=max_owners)
        self.registry = cr.ClipRegistry()
        self.store = cr.ReviewStore(os.path.join(root, "state"))
        self.authority = None
        if bind:
            # A fresh authority per world unless one is passed: binding the process-wide default
            # would let one test's leaked lease refuse another test's scan.
            self.authority = cr.bind_lifecycle_authority(
                registry=self.registry, store=self.store, coordinator=self.coordinator,
                authority=authority or cr.LifecycleAuthority())
        self.journal = tr.TransferJournal(os.path.join(root, "journal"))
        self.found = cr.discover_clips(self.review, True)
        self.scan_id = "r5p53-scan-1"
        self.scan()

    def scan(self, scan_id=None):
        """The real production order: begin → authority → begin_scan → new_session →
        publish_page → publish."""
        scan_id = scan_id or "r5p53-scan-%d" % (self.coordinator.newest(self.owner) + 1)
        generation = self.coordinator.begin(self.owner)
        frozen = cr.freeze_page(self.found.files, 0, 50)
        with self.coordinator.authority(self.owner, generation):
            self.store.begin_scan(self.owner, scan_id)
            self.session = self.registry.new_session(self.owner, scan_id)
            self.page, self.entries = self.registry.publish_page(
                self.owner, self.session, 1, frozen)
        self.coordinator.publish(self.owner, generation, {
            "session": self.session, "page": self.page, "scan_id": scan_id,
            "entries": len(self.entries)})
        self.scan_id = scan_id
        self.generation = generation
        self.frozen = frozen
        return generation

    def identity(self, entry):
        return cr.decision_identity(entry.path, entry.size, entry.mtime_ns)

    def decide_all(self, decision=cr.SELECTED):
        for entry in self.entries:
            self.store.decide(self.owner, self.identity(entry), decision, self.scan_id,
                              content_sha256=_sha(entry.path))

    def plan(self, **over):
        kwargs = dict(session_id=self.session, page_token=self.page, owner=self.owner,
                      scan_id=self.scan_id, input_folder=self.review,
                      library_folder=self.library, rejected_folder=self.rejected,
                      expected_revisions={e.clip_id: self.store.get(self.identity(e))[1]
                                          for e in self.entries})
        kwargs.update(over)
        return tr.plan_transfer(self.registry, self.store, **kwargs)

    def run(self, plan=None, **hooks):
        return tr.execute_transfer(plan or self.plan(), self.journal, store=self.store,
                                   registry=self.registry, **hooks)

    def lease(self, plan=None):
        plan = plan or self.plan()
        self.store.begin_transfer(plan.owner, plan.plan_id, [i.identity for i in plan.items],
                                  session_id=plan.session_id, page_token=plan.page_token,
                                  scan_id=plan.scan_id)
        return plan

    def snapshot(self):
        try:
            owner_of = self.registry.owner_of(self.session)
        except cr.Refusal as exc:
            owner_of = "<revoked:%s>" % exc.reason
        try:
            self.registry.page_entries(self.session, self.page)
            page = "live"
        except cr.Refusal as exc:
            page = "<revoked:%s>" % exc.reason
        return {"owner_of": owner_of, "page": page,
                "view_empty": self.coordinator.view(self.owner) == {},
                "newest": self.coordinator.newest(self.owner),
                "current_scan": self.store.current_scan(self.owner),
                "holder": self.authority.holder(self.owner) if self.authority else "",
                "epoch": self.authority.epoch(self.owner) if self.authority else 0,
                "pending": self.authority.pending_count(self.owner) if self.authority else 0}

    def library_files(self):
        return sorted(os.listdir(self.library))

    def review_files(self):
        return sorted(f for f in os.listdir(self.review)
                      if os.path.isfile(os.path.join(self.review, f)))


# ============================================================ §11.1-5  F53-A reverse order

LIFECYCLE_OPS = {
    cr.LIFECYCLE_SCAN_BEGIN: lambda w: w.coordinator.begin(w.owner),
    cr.LIFECYCLE_SCAN_RECORD: lambda w: w.store.begin_scan(w.owner, "r5p53-intruder"),
    cr.LIFECYCLE_SESSION_NEW: lambda w: w.registry.new_session(w.owner, "r5p53-intruder"),
    cr.LIFECYCLE_PAGE_PUBLISH: lambda w: w.registry.publish_page(w.owner, w.session, 2, w.frozen),
    cr.LIFECYCLE_SESSION_REVOKE: lambda w: w.registry.revoke_owner(w.owner),
}


@pytest.mark.parametrize("operation", list(LIFECYCLE_OPS))
def test_01_05_reverse_order_the_transfer_cannot_acquire(tmp_path, operation):
    """§11.1-5 — the exact R5-P52 F53-A ordering, closed.

    The lifecycle operation is paused **after its reservation has been granted and before it
    mutates anything** — the program point that used to be "guard returned, nothing held". The
    transfer then attempts the acquisition that R5-P52 measured as succeeding. It must now refuse,
    and no file may move.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    assert plan.ok, plan.refusals

    before = w.snapshot()
    assert before["page"] == "live" and not before["view_empty"]

    reserved = threading.Event()
    may_mutate = threading.Event()
    done = threading.Event()
    seen = {}

    real_reserve = w.authority.reserve_mutation

    def paused_reserve(owner, op):
        reservation = real_reserve(owner, op)      # the REAL reservation, unchanged
        if op == operation:
            seen["pending_at_pause"] = w.authority.pending_count(owner)
            seen["epoch_at_pause"] = w.authority.epoch(owner)
            reserved.set()
            assert may_mutate.wait(TIMEOUT), "the transfer never finished"
        return reservation

    w.authority.reserve_mutation = paused_reserve
    outcome = {}

    def lifecycle():
        try:
            LIFECYCLE_OPS[operation](w)
            outcome["result"] = "MUTATED"
        except cr.Refusal as exc:
            outcome["result"] = "REFUSED:%s" % exc.reason
        except BaseException as exc:               # noqa: BLE001 - diagnostic
            outcome["result"] = "ERROR:%r" % (exc,)
        finally:
            done.set()

    thread = threading.Thread(target=lifecycle, name="lifecycle-" + operation)
    thread.start()
    try:
        assert reserved.wait(TIMEOUT), "the lifecycle operation never took its reservation"
        # Ground truth: a reservation really is held, and the epoch really did move.
        assert seen["pending_at_pause"] >= 1
        assert seen["epoch_at_pause"] > 0

        with pytest.raises(cr.Refusal) as caught:
            w.lease(plan)
        assert caught.value.status == 409
        assert caught.value.reason == cr.LIFECYCLE_PENDING

        # Nothing leaked, and the transfer never ran.
        assert w.authority.holder(w.owner) == ""
        assert w.library_files() == []
        assert w.review_files() == ["clip0001.mp4"]
    finally:
        may_mutate.set()
        thread.join(TIMEOUT)
        w.authority.reserve_mutation = real_reserve
    assert not thread.is_alive()
    assert outcome["result"] == "MUTATED", outcome


@pytest.mark.parametrize("operation", list(LIFECYCLE_OPS))
def test_01_05b_reverse_order_an_executed_transfer_moves_nothing(tmp_path, operation):
    """§11.29 for each operation: a transfer whose acquisition is refused moves no file and the
    batch reports REFUSED rather than COMPLETE."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()

    reserved = threading.Event()
    may_mutate = threading.Event()
    done = threading.Event()
    real_reserve = w.authority.reserve_mutation

    def paused_reserve(owner, op):
        reservation = real_reserve(owner, op)
        if op == operation:
            reserved.set()
            assert may_mutate.wait(TIMEOUT)
        return reservation

    w.authority.reserve_mutation = paused_reserve

    def lifecycle():
        try:
            LIFECYCLE_OPS[operation](w)
        except cr.Refusal:
            pass
        finally:
            done.set()

    thread = threading.Thread(target=lifecycle)
    thread.start()
    try:
        assert reserved.wait(TIMEOUT)
        result = w.run(plan)
        assert result.state == tr.REFUSED, result.state
        assert result.advance_page is False
        assert w.library_files() == []
        assert w.review_files() == ["clip0001.mp4"]
    finally:
        may_mutate.set()
        thread.join(TIMEOUT)
        w.authority.reserve_mutation = real_reserve
    assert not thread.is_alive()


# ============================================================ §11.6-8  F53-B probe/acquire

def test_06_a_scan_between_the_probe_and_the_lease_record_is_detected(tmp_path):
    """§11.6 — the exact R5-P52 F53-B ordering, closed by epoch validation."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()

    probed = threading.Event()
    scan_done = threading.Event()
    real_view = w.coordinator.view
    seen = {}

    def paused_view(owner):
        value = real_view(owner)                   # the real probe, unchanged
        if not probed.is_set():
            seen["epoch_before_scan"] = w.authority.epoch(owner)
            probed.set()
            assert scan_done.wait(TIMEOUT), "the scan never began"
        return value

    w.coordinator.view = paused_view
    scan_outcome = {}

    def scan():
        assert probed.wait(TIMEOUT), "acquire never probed the coordinator"
        try:
            scan_outcome["result"] = "GENERATION:%d" % w.coordinator.begin(w.owner)
        except cr.Refusal as exc:
            scan_outcome["result"] = "REFUSED:%s" % exc.reason
        finally:
            scan_done.set()

    thread = threading.Thread(target=scan)
    thread.start()
    result = w.run(plan)
    thread.join(TIMEOUT)
    assert not thread.is_alive()
    w.coordinator.view = real_view

    # The scan was allowed (no lease existed when it reserved), the epoch moved, and the transfer
    # therefore refused instead of committing against the authority its probe described.
    assert scan_outcome["result"].startswith("GENERATION")
    assert w.authority.epoch(w.owner) > seen["epoch_before_scan"]
    assert result.state == tr.REFUSED
    assert result.advance_page is False
    assert w.library_files() == []
    assert w.review_files() == ["clip0001.mp4"]
    assert w.authority.holder(w.owner) == ""


def test_07_a_started_but_unpublished_scan_refuses_acquisition(tmp_path):
    """§11.7 — scan-in-flight remains observable and binding after `begin` returns."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    generation = w.coordinator.begin(w.owner)
    assert w.coordinator.view(w.owner) == {}
    with pytest.raises(cr.Refusal) as caught:
        w.lease()
    assert caught.value.reason == "scan_in_flight"
    assert w.authority.holder(w.owner) == ""
    # ... and it stays binding right through to publication.
    w.coordinator.publish(w.owner, generation, {"session": w.session, "page": w.page})
    plan = w.lease()
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_08_a_scan_completing_during_acquisition_never_yields_a_stale_lease(tmp_path):
    """§11.8 — whichever way the interleaving falls, the lease must not describe a superseded
    page. The bounded epoch retry makes success legitimate; a refusal is equally acceptable."""
    w = World(tmp_path, count=2)
    w.decide_all(cr.SELECTED)
    outcomes = []
    for round_index in range(12):
        plan = w.plan()
        barrier = threading.Barrier(2, timeout=TIMEOUT)
        result = {}

        def rescan():
            barrier.wait()
            try:
                w.scan("r5p53-concurrent-%d" % round_index)
                result["scan"] = "OK"
            except cr.Refusal as exc:
                result["scan"] = "REFUSED:%s" % exc.reason

        thread = threading.Thread(target=rescan)
        thread.start()
        barrier.wait()
        try:
            w.lease(plan)
            result["lease"] = "ACQUIRED"
        except cr.Refusal as exc:
            result["lease"] = "REFUSED:%s" % exc.reason
        thread.join(TIMEOUT)
        assert not thread.is_alive()

        if result.get("lease") == "ACQUIRED":
            # A lease was granted, so the page it names must still be the authoritative one.
            assert w.store.current_scan(w.owner) == plan.scan_id, result
            assert w.registry.owner_of(plan.session_id) == w.owner
            w.store.end_transfer(plan.owner, plan.plan_id)
        outcomes.append((result.get("scan"), result.get("lease")))
    assert len(outcomes) == 12
    assert w.authority.holder(w.owner) == ""


# ============================================================ §11.9-12  transfer wins first

def test_09_12_a_held_lease_refuses_every_lifecycle_mutation(tmp_path):
    """§11.9-12 — scan begin, session revocation, page replacement and durable scan record."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    before = w.snapshot()

    for operation, call in LIFECYCLE_OPS.items():
        with pytest.raises(cr.Refusal) as caught:
            call(w)
        assert caught.value.status == 409
        assert caught.value.reason == cr.LIFECYCLE_CONFLICT, operation

    after = w.snapshot()
    # Byte-identical authority: a refused mutation changes nothing, including the epoch.
    assert after["owner_of"] == before["owner_of"] == w.owner
    assert after["page"] == before["page"] == "live"
    assert after["view_empty"] is False
    assert after["newest"] == before["newest"]
    assert after["current_scan"] == before["current_scan"]
    assert after["epoch"] == before["epoch"], "a refused mutation moved the epoch"
    assert after["pending"] == 0
    w.store.end_transfer(plan.owner, plan.plan_id)


# ============================================================ §11.13-15  reservation rules

def test_13_a_pending_reservation_refuses_acquisition(tmp_path):
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    try:
        with pytest.raises(cr.Refusal) as caught:
            w.lease()
        assert caught.value.reason == cr.LIFECYCLE_PENDING
    finally:
        assert w.authority.release_mutation(reservation) is True
    plan = w.lease()                                # and it works once released
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_14_a_refused_reservation_does_not_move_the_epoch(tmp_path):
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    epoch = w.authority.epoch(w.owner)
    for _ in range(3):
        with pytest.raises(cr.Refusal):
            w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    assert w.authority.epoch(w.owner) == epoch, "a refused reservation incremented the epoch"
    assert w.authority.pending_count(w.owner) == 0
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_15_an_exception_releases_the_reservation_without_undoing_valid_changes(tmp_path):
    """§11.15 — `publish_page` raising on a bad session must still release its reservation."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    before = w.snapshot()
    with pytest.raises(cr.Refusal):
        w.registry.publish_page(w.owner, "not-a-real-session", 2, w.frozen)
    assert w.authority.pending_count(w.owner) == 0, "a reservation leaked on the exception path"
    # The epoch moved (a reservation was granted) but no authoritative state changed.
    assert w.authority.epoch(w.owner) > before["epoch"]
    after = w.snapshot()
    assert after["owner_of"] == w.owner and after["page"] == "live"
    # And a transfer can still acquire afterwards.
    plan = w.lease()
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_15b_a_stale_or_foreign_token_releases_nothing(tmp_path):
    w = World(tmp_path, count=1)
    first = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    second = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SESSION_NEW)
    assert w.authority.pending_count(w.owner) == 2
    assert w.authority.release_mutation(first) is True
    assert w.authority.release_mutation(first) is False      # repeat frees nothing
    assert w.authority.pending_count(w.owner) == 1           # second survives
    forged = cr.MutationReservation(w.owner, second.token, "forged", second.epoch)
    assert w.authority.release_mutation(forged) is False, "a forged token released a reservation"
    assert w.authority.pending_count(w.owner) == 1
    assert w.authority.release_mutation(second) is True
    assert w.authority.pending_count(w.owner) == 0


# ============================================================ §11.16-18  concurrency

def test_16_two_same_owner_lifecycle_mutations_are_not_serialized(tmp_path):
    """§11.16 + §7 — reservations must not serialise each other, or the scan-generation contract
    (a newer scan supersedes a slower older one) would be broken."""
    w = World(tmp_path, count=1)
    first = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    second = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    assert first.token != second.token
    assert second.epoch > first.epoch
    assert w.authority.pending_count(w.owner) == 2
    w.authority.release_mutation(first)
    w.authority.release_mutation(second)
    # And two real concurrent scans still both complete, newest winning.
    errors = []

    def rescan(tag):
        try:
            w.scan("r5p53-par-%s" % tag)
        except BaseException as exc:                # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=rescan, args=(tag,)) for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT)
    assert all(not t.is_alive() for t in threads)
    assert not errors, errors
    assert w.coordinator.newest(w.owner) >= 3


def test_17_18_unrelated_owners_are_independent(tmp_path):
    """§11.17-18 — no cross-owner serialization for either mutations or transfers."""
    shared = cr.LifecycleAuthority()
    coordinator = cr.ScanCoordinator()
    a = World(os.path.join(str(tmp_path), "a"), count=1, owner=OWNER,
              coordinator=coordinator, authority=shared)
    b = World(os.path.join(str(tmp_path), "b"), count=1, owner=OTHER,
              coordinator=coordinator, authority=shared)
    a.decide_all(cr.SELECTED)
    b.decide_all(cr.SELECTED)

    plan_a = a.lease()
    # B's lifecycle still works while A is leased...
    b.scan("owner-b-2")
    b.decide_all(cr.SELECTED)
    # ... and B can transfer concurrently.
    plan_b = b.lease()
    assert shared.holder(OWNER) == plan_a.plan_id
    assert shared.holder(OTHER) == plan_b.plan_id
    a.store.end_transfer(plan_a.owner, plan_a.plan_id)
    b.store.end_transfer(plan_b.owner, plan_b.plan_id)
    assert shared.holder(OWNER) == "" and shared.holder(OTHER) == ""


# ============================================================ §11.19-23  eviction surfaces

def test_19_21_eviction_cannot_revoke_a_leased_owner(tmp_path):
    """§11.19+21 — registry owner-cap and session-cap eviction under a lease."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    for index in range(cr.MAX_OWNERS + 4):
        w.registry.new_session("filler-%03d" % index)
    assert w.registry.owner_of(w.session) == w.owner, "a leased owner was evicted"
    assert len(w.registry.page_entries(w.session, w.page)) == 1
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_19b_owner_cap_eviction_cannot_be_raced_by_a_lease(tmp_path):
    """§11.19 the hard way — the eviction *window*, not just the eviction outcome.

    A `guarded()`-probe-then-delete still skips a leased owner, so the ordinary test above passes
    either way. What it cannot see is the window: the probe and the three deletions happen under
    ``ClipRegistry._lock``, which ``acquire()`` never takes, so a transfer can acquire the victim's
    authority in between. This directs exactly that collision by pausing victim selection and
    leasing the victim during the pause.

    Either outcome is acceptable as long as it is consistent: the lease is refused (because the
    victim is reserved), or the lease is granted and the victim is therefore **not** evicted. What
    must never happen is both.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()                                 # built BEFORE the eviction thread starts,
    assert plan.ok, plan.refusals                   # so leasing needs no registry lock

    # Fill the registry so that the next new owner triggers owner-cap eviction, with our owner
    # the oldest and therefore the first candidate.
    for index in range(cr.MAX_OWNERS - 1):
        w.registry.new_session("filler-%03d" % index)

    selected = threading.Event()
    release = threading.Event()
    real_select = w.registry._reserve_evictable_owner

    def paused_select():
        chosen = real_select()
        if chosen[0] == w.owner:
            selected.set()
            assert release.wait(TIMEOUT), "the lease attempt never finished"
        return chosen

    w.registry._reserve_evictable_owner = paused_select
    evicting = {}

    def evict():
        try:
            w.registry.new_session("trigger-eviction")
            evicting["result"] = "OK"
        except cr.Refusal as exc:
            evicting["result"] = "REFUSED:%s" % exc.reason
        except BaseException as exc:                # noqa: BLE001
            evicting["result"] = "ERROR:%r" % (exc,)

    thread = threading.Thread(target=evict, name="owner-cap-eviction")
    thread.start()
    lease_outcome = {}
    try:
        assert selected.wait(TIMEOUT), "the eviction never selected our owner"
        try:
            w.lease(plan)
            lease_outcome["result"] = "ACQUIRED"
        except cr.Refusal as exc:
            lease_outcome["result"] = "REFUSED:%s" % exc.reason
    finally:
        release.set()
        thread.join(TIMEOUT)
        w.registry._reserve_evictable_owner = real_select
    assert not thread.is_alive()

    try:
        w.registry.owner_of(w.session)
        victim_live = True
    except cr.Refusal:
        victim_live = False

    if lease_outcome["result"] == "ACQUIRED":
        assert victim_live, (
            "a lease was granted over an owner that owner-cap eviction then revoked: "
            "lease=%s evict=%s" % (lease_outcome, evicting))
        w.store.end_transfer(plan.owner, plan.plan_id)
    else:
        # Refused because the victim was reserved for eviction -- the safe outcome.
        assert lease_outcome["result"] == "REFUSED:%s" % cr.LIFECYCLE_PENDING, lease_outcome
        assert w.authority.holder(w.owner) == ""


def test_20_page_replacement_is_refused_so_no_page_eviction_occurs(tmp_path):
    """§11.20 — page-cap eviction only happens inside `publish_page`, which refuses under a
    lease, so the leased page cannot be evicted."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    for _ in range(cr.MAX_PAGES_PER_SESSION + 2):
        with pytest.raises(cr.Refusal) as caught:
            w.registry.publish_page(w.owner, w.session, 2, w.frozen)
        assert caught.value.reason == cr.LIFECYCLE_CONFLICT
    assert len(w.registry.page_entries(w.session, w.page)) == 1
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_22_coordinator_pruning_skips_a_leased_owner(tmp_path):
    """§11.22 — and the skip is now a reservation, not a probe."""
    coordinator = cr.ScanCoordinator(max_owners=2)
    w = World(tmp_path, count=1, coordinator=coordinator, max_owners=2)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    for extra in ("owner-b", "owner-c", "owner-d"):
        coordinator.begin(extra)
    assert coordinator.newest(w.owner) == w.generation, "a leased owner was pruned"
    assert w.registry.owner_of(w.session) == w.owner
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_23_forget_is_refused_during_a_transfer(tmp_path):
    """§11.23 — R5-P52 recorded `forget()` as the one coordinator mutator with neither a lease
    check nor a caller. It is reserved now."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    before = w.snapshot()
    with pytest.raises(cr.Refusal) as caught:
        w.coordinator.forget(w.owner)
    assert caught.value.reason == cr.LIFECYCLE_CONFLICT
    after = w.snapshot()
    assert after["newest"] == before["newest"] != 0
    assert after["view_empty"] is False
    assert after["epoch"] == before["epoch"]
    w.store.end_transfer(plan.owner, plan.plan_id)
    w.coordinator.forget(w.owner)                   # and it works once released
    assert w.coordinator.newest(w.owner) == 0


# ============================================================ §11.24  rebinding

def test_24_rebinding_during_an_active_lease_is_refused(tmp_path):
    """[R5-P57] Superseded tail, replaced rather than deleted.

    R5-P53 shipped this test asserting that *"once released, rebinding is permitted again"* --
    an idle authority could be replaced. R5-P56 then reproduced three MAJOR bypasses that all
    entered through exactly that permission (F56-1, F56-2, F56-3), and the owner's R5-P57
    decision retires the capability: replacement is refused whether or not anything is idle.
    So the refusal half of this test is unchanged and the permission half is now its negation.
    The original assertion is preserved verbatim in the R5-P53/R5-P55 evidence packages.
    """
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    usurper = cr.LifecycleAuthority()
    with pytest.raises(cr.Refusal) as caught:
        cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                    coordinator=w.coordinator, authority=usurper)
    assert caught.value.reason == "lifecycle_authority_rebind_refused"
    # All three bindings are unchanged, so no subsystem was left pointing at a different gate.
    assert w.registry.lifecycle_authority is w.authority
    assert w.store.lifecycle_authority is w.authority
    assert w.coordinator.lifecycle_authority is w.authority
    # The lease is still enforced: the usurper cannot be used to bypass it.
    with pytest.raises(cr.Refusal):
        w.registry.revoke_owner(w.owner)
    w.store.end_transfer(plan.owner, plan.plan_id)
    # [R5-P57] And releasing the lease does NOT unlock replacement: the authority is now idle on
    # every measure the old gate consulted, and the refusal is identical.
    assert w.authority.has_any_lease() is False
    assert w.authority.has_any_pending() is False
    with pytest.raises(cr.Refusal) as idle:
        cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                    coordinator=w.coordinator, authority=usurper)
    assert idle.value.reason == "lifecycle_authority_rebind_refused"
    assert w.registry.lifecycle_authority is w.authority
    assert w.store.lifecycle_authority is w.authority
    assert w.coordinator.lifecycle_authority is w.authority
    # The lifecycle still works afterwards: a refused bind is inert, not a wedge.
    w.registry.revoke_owner(w.owner)


def test_24b_rebinding_the_same_authority_is_idempotent_even_under_a_lease(tmp_path):
    """P1 startup and the lazy store attachment depend on this staying permitted."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.lease()
    again = cr.bind_lifecycle_authority(registry=w.registry, store=w.store,
                                        coordinator=w.coordinator, authority=w.authority)
    assert again is w.authority
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_24c_a_lazily_created_store_joins_the_established_authority(tmp_path):
    """The gui.py shape: registry+coordinator bound eagerly, store attached later."""
    registry = cr.ClipRegistry()
    coordinator = cr.ScanCoordinator()
    shared = cr.bind_lifecycle_authority(registry=registry, coordinator=coordinator,
                                         authority=cr.LifecycleAuthority())
    store = cr.ReviewStore(os.path.join(str(tmp_path), "late"))
    assert store.lifecycle_authority is None
    cr.bind_lifecycle_authority(store=store, authority=shared)
    assert store.lifecycle_authority is shared
    assert registry.lifecycle_authority is shared
    assert coordinator.lifecycle_authority is shared


# ============================================================ §11.25-30  sequencing & liveness

def test_25_a_lease_is_available_after_a_mutation_completes(tmp_path):
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    w.scan("r5p53-after-mutation")
    w.decide_all(cr.SELECTED)
    assert w.authority.pending_count(w.owner) == 0
    plan = w.lease()
    assert w.authority.holder(w.owner) == plan.plan_id
    w.store.end_transfer(plan.owner, plan.plan_id)


def test_26_a_mutation_is_available_after_a_transfer_completes(tmp_path):
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    result = w.run()
    assert result.state == tr.COMPLETE
    assert w.authority.holder(w.owner) == ""
    generation = w.coordinator.begin(w.owner)       # no longer refused
    assert generation > w.generation
    assert w.authority.pending_count(w.owner) == 0


def test_27_a_post_commit_journal_failure_still_releases_the_lease(tmp_path):
    """§11.27 — F1's failure path must not leak the lease or a reservation."""
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    plan = w.plan()
    real_append = w.journal.append
    state = {"armed": False}

    def failing(event, **kw):
        if state["armed"] and event.get("state") == tr.PUBLISHED:
            raise tr.TransferRefused("journal_unavailable", "injected post-commit failure")
        return real_append(event, **kw)

    def before_publish(item):
        state["armed"] = True

    w.journal.append = failing
    result = w.run(plan, before_publish=before_publish)
    w.journal.append = real_append

    assert result.outcomes[0].state == tr.PUBLISHED_UNJOURNALLED
    assert w.authority.holder(w.owner) == "", "the lease leaked on the failure path"
    assert w.authority.pending_count(w.owner) == 0
    for item in plan.items:
        assert w.store.active_transfer(item.identity) == ""
    assert w.library_files() == ["clip0001.mp4"]    # filesystem truth preserved


def test_28_a_failed_acquisition_leaves_no_reservation_or_lease_behind(tmp_path):
    w = World(tmp_path, count=1)
    w.decide_all(cr.SELECTED)
    reservation = w.authority.reserve_mutation(w.owner, cr.LIFECYCLE_SCAN_BEGIN)
    with pytest.raises(cr.Refusal):
        w.lease()
    w.authority.release_mutation(reservation)
    assert w.authority.pending_count(w.owner) == 0
    assert w.authority.holder(w.owner) == ""
    for entry in w.entries:
        assert w.authority.identity_holder(w.identity(entry)) == ""
        assert w.store.active_transfer(w.identity(entry)) == ""


def test_30_a_valid_uncontested_transfer_still_completes(tmp_path):
    """§11.30 — the liveness check. None of the above may cost the ordinary path."""
    w = World(tmp_path, count=3)
    w.decide_all(cr.SELECTED)
    result = w.run()
    assert result.state == tr.COMPLETE, result.state
    assert result.advance_page is True
    assert [o.state for o in result.outcomes] == [tr.COMPLETE] * 3
    assert len(w.library_files()) == 3
    assert w.review_files() == []
    assert w.authority.holder(w.owner) == ""
    assert w.authority.pending_count(w.owner) == 0


def test_30b_a_rejected_clip_still_reaches_quarantine(tmp_path):
    w = World(tmp_path, count=2)
    w.decide_all(cr.REJECTED)
    result = w.run()
    assert result.state == tr.COMPLETE
    assert sorted(os.listdir(w.rejected)) == ["clip0001.mp4", "clip0002.mp4"]
    assert w.library_files() == []


# ============================================================ §12  bounded stress matrix

def test_stress_sixty_barrier_directed_interleavings(tmp_path):
    """§12 — 60 independently synchronized interleavings over both orderings and all five
    operations, each with its own fresh world and its own barrier.

    The invariant asserted every round is the one that actually matters, and it is asserted
    against the real filesystem rather than an exception string:

        a clip may be published ONLY IF the authority it was planned against is still intact.

    R5-P52's 0/60 unaided result is not evidence of safety — it is evidence that the ordering is
    hard to hit by luck. Each round here *directs* the collision with a barrier.
    """
    operations = list(LIFECYCLE_OPS)
    rounds = []
    violations = []

    # A bare barrier is NOT enough: released simultaneously, the transfer reliably wins and the
    # lifecycle side is refused, so one of the two boundaries never gets exercised (measured --
    # 10/10 rounds went the same way). Each round therefore *directs* its collision: half park the
    # lifecycle operation between its reservation and its mutation (the F53-A boundary), half take
    # the lease first (the transfer-wins boundary).
    for index in range(60):
        operation = operations[index % len(operations)]
        lifecycle_reserves_first = (index // len(operations)) % 2 == 0
        w = World(os.path.join(str(tmp_path), "s%02d" % index), count=1)
        w.decide_all(cr.SELECTED)
        plan = w.plan()
        assert plan.ok, plan.refusals
        outcome = {"direction": "lifecycle_first" if lifecycle_reserves_first else "transfer_first"}

        if lifecycle_reserves_first:
            reserved = threading.Event()
            release = threading.Event()
            real_reserve = w.authority.reserve_mutation

            def paused_reserve(owner, op, _real=real_reserve):
                reservation = _real(owner, op)
                if op == operation:
                    reserved.set()
                    assert release.wait(TIMEOUT)
                return reservation

            w.authority.reserve_mutation = paused_reserve

            def lifecycle():
                try:
                    LIFECYCLE_OPS[operation](w)
                    outcome["lifecycle"] = "MUTATED"
                except cr.Refusal as exc:
                    outcome["lifecycle"] = "REFUSED:%s" % exc.reason
                except BaseException as exc:        # noqa: BLE001
                    outcome["lifecycle"] = "ERROR:%r" % (exc,)

            thread = threading.Thread(target=lifecycle, name="stress-%02d" % index)
            thread.start()
            try:
                assert reserved.wait(TIMEOUT), "round %d never reserved" % index
                result = w.run(plan)
                outcome["transfer"] = result.state
                outcome["expected_refusal"] = cr.LIFECYCLE_PENDING
                outcome["codes"] = sorted({o.code for o in result.outcomes if o.code})
            finally:
                release.set()
                thread.join(TIMEOUT)
                w.authority.reserve_mutation = real_reserve
            # The transfer attempted acquisition while a reservation was pending: it must refuse.
            assert outcome["transfer"] == tr.REFUSED, (index, outcome)
        else:
            held = w.lease(plan)
            outcome["transfer"] = "LEASED"

            def lifecycle_after():
                try:
                    LIFECYCLE_OPS[operation](w)
                    outcome["lifecycle"] = "MUTATED"
                except cr.Refusal as exc:
                    outcome["lifecycle"] = "REFUSED:%s" % exc.reason
                except BaseException as exc:        # noqa: BLE001
                    outcome["lifecycle"] = "ERROR:%r" % (exc,)

            thread = threading.Thread(target=lifecycle_after, name="stress-%02d" % index)
            thread.start()
            thread.join(TIMEOUT)
            w.store.end_transfer(held.owner, held.plan_id)
            # The lifecycle operation ran while a lease was held: it must refuse.
            assert outcome["lifecycle"] == "REFUSED:%s" % cr.LIFECYCLE_CONFLICT, (index, outcome)

        assert not thread.is_alive(), "round %d hung" % index

        moved = w.library_files()
        snapshot = w.snapshot()
        authority_intact = (snapshot["owner_of"] == w.owner and snapshot["page"] == "live"
                            and snapshot["current_scan"] == plan.scan_id
                            and not snapshot["view_empty"])
        if moved and not authority_intact:
            violations.append({"round": index, "operation": operation,
                               "direction": outcome["direction"], "moved": moved,
                               "snapshot": snapshot, "outcome": dict(outcome)})
        assert snapshot["holder"] == "", "round %d leaked a lease" % index
        assert snapshot["pending"] == 0, "round %d leaked a reservation" % index
        rounds.append((operation, outcome["direction"], outcome.get("lifecycle"),
                       outcome.get("transfer"), bool(moved)))

    assert len(rounds) == 60
    assert not violations, (
        "a clip was published against invalidated authority in %d/60 directed rounds: %s"
        % (len(violations), violations[0]))
    # Both boundaries must actually have been exercised, or the matrix proved only half of it.
    lifecycle_first = [r for r in rounds if r[1] == "lifecycle_first"]
    transfer_first = [r for r in rounds if r[1] == "transfer_first"]
    assert len(lifecycle_first) == 30 and len(transfer_first) == 30
    assert all(r[3] == tr.REFUSED for r in lifecycle_first), \
        "a transfer acquired despite a pending reservation"
    assert all(r[2] == "REFUSED:%s" % cr.LIFECYCLE_CONFLICT for r in transfer_first), \
        "a lifecycle mutation ran despite a held lease"
    # And every operation appeared in both directions.
    for operation in operations:
        assert sum(1 for r in rounds if r[0] == operation) == 12


# ============================================================ structural guarantees

def test_the_arbitration_lock_is_never_held_across_a_subsystem_call():
    """`acquire` must probe the coordinator OUTSIDE its own lock, or the documented
    coordinator → registry → store → authority order gains a reverse edge and a cycle becomes
    constructible. A cycle would hang rather than fail, so this is asserted on the source AST."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cr.LifecycleAuthority.acquire)))
    body = ast.unparse(tree)
    probe = body.index("coordinator.newest")
    # The probe must not be inside a `with self._lock` block. Every `with self._lock:` in the
    # method opens after the probe, or is a separate short critical section that does not contain
    # it -- so the cheapest robust assertion is that the probe statement is not nested under one.
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and "self._lock" in ast.unparse(node.items[0].context_expr):
            inner = ast.unparse(node)
            assert "coordinator.newest" not in inner, \
                "acquire() probes the coordinator while holding the arbitration lock"
            assert "coordinator.view" not in inner, \
                "acquire() probes the coordinator while holding the arbitration lock"
    assert probe > 0


def test_no_lifecycle_mutation_site_still_uses_the_check_only_guard():
    """Every mutation site must reserve. A site that reverted to `guard()` would reintroduce
    exactly the R5-P52 check-then-act, and `guard` is retained only as a diagnostic probe."""
    import ast
    import inspect
    import textwrap

    sites = (cr.ScanCoordinator.begin, cr.ScanCoordinator.forget, cr.ClipRegistry.new_session,
             cr.ClipRegistry.publish_page, cr.ClipRegistry.revoke_owner, cr.ReviewStore.begin_scan)
    for function in sites:
        body = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(function))))
        assert "reserve_mutation" in body, "%s does not reserve" % function.__qualname__
        assert "release_mutation" in body, "%s does not release" % function.__qualname__
        assert ".guard(" not in body, \
            "%s still uses the check-only guard" % function.__qualname__
