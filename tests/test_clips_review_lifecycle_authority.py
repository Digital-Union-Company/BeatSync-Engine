"""R5-P51 — the binding lifecycle authority (R5-P48 finding F3).

``execute_transfer`` revalidated the decision, its revision, the scan generation and the session
**per item**, and the R5-P47 lease made ``decide`` refuse a leased identity — so the *decision*
could not move across the commit point. Nothing else was held. Measured on the pre-R5-P51
candidate, every one of ``revoke_owner``, ``new_session``, ``begin_scan``, ``publish_page``,
``ScanCoordinator.begin`` and a whole new scan was **accepted** after ``_revalidate()`` returned
and before ``os.rename``; the clip published anyway and reported ``COMPLETE``. In two of those the
session was already revoked at the commit point, and in another the gallery was already blank.

The owner-selected strategy is a **binding lease**, not a narrower recheck: the conflicting
lifecycle operation is refused, *before it changes any authoritative state*, and may be retried
once the transfer releases. A pre-commit revalidation would only shrink the window.

``ScanCoordinator.begin`` is the guard that matters most and the one a naive fix misses: taking a
generation alone makes every older view non-current, so guarding only ``ReviewStore.begin_scan``
would refuse the scan *after* the operator's gallery had already gone blank.

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

OWNER = "r5p51-owner"
OTHER = "r5p51-other"
_UNSET = object()


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
    """A full P1 world wired the way ``gui.py`` wires it, optionally bound."""

    def __init__(self, root, count=2, owner=OWNER, bind=True, coordinator=None,
                 authority=None, max_owners=cr.MAX_OWNERS):
        self.owner = owner
        self.review = str(root / "review")
        self.library = str(root / "library")
        self.rejected = str(root / "quarantine")
        for index in range(1, count + 1):
            _write(os.path.join(self.review, f"clip{index:04d}.mp4"), _payload(index))
        os.makedirs(self.library, exist_ok=True)
        os.makedirs(self.rejected, exist_ok=True)
        self.coordinator = coordinator or cr.ScanCoordinator(max_owners=max_owners)
        self.registry = cr.ClipRegistry()
        self.store = cr.ReviewStore(str(root / "state"))
        self.authority = None
        if bind:
            # A FRESH authority per world unless one is passed in. `bind_lifecycle_authority`
            # defaults to the process-wide instance, which production wants and tests must not
            # share: a lease leaked by one test would refuse every later test's scan.
            self.authority = cr.bind_lifecycle_authority(
                registry=self.registry, store=self.store, coordinator=self.coordinator,
                authority=authority or cr.LifecycleAuthority())
        self.journal = tr.TransferJournal(str(root / "journal"))
        self.found = cr.discover_clips(self.review, True)
        self.scan_id = "r5p51-scan-1"
        self.scan()

    def scan(self, scan_id=None):
        """The real flow: begin → authority → begin_scan → new_session → publish_page → publish."""
        scan_id = scan_id or self.scan_id
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

    def authority_snapshot(self):
        try:
            owner_of = self.registry.owner_of(self.session)
        except cr.Refusal:
            owner_of = "<revoked>"
        try:
            self.registry.page_entries(self.session, self.page)
            page = "live"
        except cr.Refusal:
            page = "<revoked>"
        return {"owner_of": owner_of, "page": page,
                "scan": self.store.current_scan(self.owner),
                "generation": self.coordinator.newest(self.owner),
                "view": self.coordinator.view(self.owner)}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _at_commit_boundary(world, action, plan=None):
    """Fire *action* after ``_revalidate()`` and immediately before ``os.rename``.

    ``before_publish`` is the engine's own seam at exactly that point, so the window probed is the
    real one rather than one the test invented.
    """
    plan = plan or world.plan()
    captured = {}

    def hook(_item):
        captured["lease"] = world.authority.holder(world.owner) if world.authority else ""
        try:
            action()
            captured["outcome"] = "ACCEPTED"
        except cr.Refusal as refusal:
            captured["outcome"] = f"REFUSED:{refusal.status}:{refusal.reason}"
        captured["authority"] = world.authority_snapshot()

    result = world.run(plan, before_publish=hook)
    return plan, result, captured


# =============================================================================================
# THE CONTRACT
# =============================================================================================

def test_the_authority_is_the_leaf_lock_and_never_calls_out_while_holding_it():
    """The documented lock order only holds if nothing in the authority acquires another lock
    while holding its own. ``acquire`` is the one method that must consult the coordinator, and it
    does so **before** taking ``_lock`` — asserted on the source, because a cycle introduced here
    would be a deadlock rather than a test failure."""
    import ast
    import inspect
    source = inspect.getsource(cr.LifecycleAuthority.acquire)
    tree = ast.parse(source.lstrip())
    body = tree.body[0].body
    probe_index = with_index = None
    for position, node in enumerate(body):
        text = ast.unparse(node)
        if "coordinator" in text and probe_index is None and not isinstance(node, ast.With):
            probe_index = position
        if isinstance(node, ast.With) and "self._lock" in ast.unparse(node.items[0].context_expr):
            with_index = position
            break
    assert probe_index is not None and with_index is not None
    assert probe_index < with_index, \
        "the coordinator probe must happen BEFORE self._lock is taken, or the lock order cycles"


def test_lifecycle_conflict_is_the_same_token_as_the_decision_lease():
    assert cr.LIFECYCLE_CONFLICT == cr.TRANSFER_IN_PROGRESS == "transfer_in_progress"
    assert set(cr.LIFECYCLE_OPERATIONS) == {
        cr.LIFECYCLE_SCAN_BEGIN, cr.LIFECYCLE_SCAN_RECORD, cr.LIFECYCLE_SESSION_NEW,
        cr.LIFECYCLE_SESSION_REVOKE, cr.LIFECYCLE_PAGE_PUBLISH, cr.LIFECYCLE_OWNER_EVICT}


def test_binding_without_an_explicit_authority_uses_the_process_wide_default(tmp_path):
    """Production binds the default so a lazily-created store can join the same gate with no
    shared variable to thread through — which is exactly what ``gui.py``'s ``_clips_store`` does.
    Tests pass their own instance instead, for isolation."""
    registry = cr.ClipRegistry()
    coordinator = cr.ScanCoordinator()
    shared = cr.bind_lifecycle_authority(registry=registry, coordinator=coordinator)
    assert shared is cr.default_lifecycle_authority()
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.lifecycle_authority = cr.default_lifecycle_authority()   # the gui.py pattern
    assert store.lifecycle_authority is registry.lifecycle_authority is coordinator.lifecycle_authority
    assert shared.holder(OWNER) == "", "the default must not be carrying a lease"


def test_an_unbound_subsystem_behaves_exactly_as_before(tmp_path):
    """Binding is explicit, so the existing P1 suites that build bare objects keep working. This
    is also why production must bind — and `gui.py` does."""
    world = World(tmp_path, bind=False)
    assert world.registry.lifecycle_authority is None
    assert world.store.lifecycle_authority is None
    assert world.coordinator.lifecycle_authority is None
    world.registry.revoke_owner(OWNER)              # no guard, no refusal
    assert cr.ReviewStore.lifecycle_authority is None, "the class default stays None"


# =============================================================================================
# 1-4 — the four original races, at the real commit boundary
# =============================================================================================

@pytest.mark.parametrize("name,action", [
    ("revoke_owner", lambda w: w.registry.revoke_owner(w.owner)),
    ("new_session", lambda w: w.registry.new_session(w.owner, w.scan_id)),
    ("begin_scan", lambda w: w.store.begin_scan(w.owner, "r5p51-midflight")),
    ("publish_page", lambda w: w.registry.publish_page(w.owner, w.session, 2, w.frozen)),
])
def test_01_04_the_four_original_races_are_refused_and_the_transfer_completes(world, name, action):
    world.decide_all()
    before = world.authority_snapshot()
    plan, result, captured = _at_commit_boundary(world, lambda: action(world))

    assert captured["lease"], "the lease was held at the boundary"
    assert captured["outcome"] == f"REFUSED:409:{cr.LIFECYCLE_CONFLICT}", captured["outcome"]

    # the authority the transfer depends on was still intact AT the commit point
    at_commit = captured["authority"]
    assert at_commit["owner_of"] == OWNER
    assert at_commit["page"] == "live"
    assert at_commit["scan"] == world.scan_id
    assert at_commit["generation"] == before["generation"]
    assert at_commit["view"] == before["view"], "a refused scan must not blank a valid gallery"

    # and the transfer finished normally
    assert result.state == tr.COMPLETE
    assert result.advance_page is True
    for item in plan.items:
        assert not os.path.lexists(item.source_path)
        assert _sha(item.destination_path) == item.approved_sha256


# =============================================================================================
# 5-7 — ScanCoordinator, the part a begin_scan-only guard misses
# =============================================================================================

def test_05_scan_coordinator_begin_during_a_transfer_is_refused_before_it_takes_a_generation(world):
    world.decide_all()
    before = world.authority_snapshot()
    plan, result, captured = _at_commit_boundary(world, lambda: world.coordinator.begin(OWNER))
    assert captured["outcome"] == f"REFUSED:409:{cr.LIFECYCLE_CONFLICT}"
    # the decisive assertion: NOTHING changed, so the gallery is still current
    assert captured["authority"]["generation"] == before["generation"], "no generation was taken"
    assert captured["authority"]["view"] == before["view"], "the view is untouched"
    assert world.coordinator.view(OWNER) != {}, "and it is still non-empty after the batch"
    assert result.state == tr.COMPLETE


def test_06_a_transfer_cannot_acquire_while_a_scan_generation_is_in_flight(world):
    """§6 inverse: ``begin`` makes a generation newest *before* the session and page are replaced,
    so there is an interval where ``ReviewStore`` still reports the OLD scan id while its page is
    already logically superseded. Treating that as valid authority is the mistake §6 names."""
    world.decide_all()
    plan = world.plan()
    generation = world.coordinator.begin(OWNER)          # in flight, not yet published
    assert world.coordinator.view(OWNER) == {}, "logically superseded"
    assert world.store.current_scan(OWNER) == world.scan_id, "but the store still says the old one"

    result = world.run(plan)
    assert result.state == tr.REFUSED
    assert result.outcomes == (), "acquisition was refused, so no item was attempted"
    assert world.authority.holder(OWNER) == "", "and no lease leaked"
    for item in plan.items:
        assert os.path.isfile(item.source_path), "nothing moved"
        assert not os.path.lexists(item.destination_path)
    reasons = [e.get("reason") for e in world.journal.records() if e.get("reason")]
    assert "scan_in_flight" in reasons

    # ...and it is retryable once the transition completes
    world.coordinator.publish(OWNER, generation, {"session": world.session, "page": world.page,
                                                  "scan_id": world.scan_id, "entries": 2})
    assert world.run(world.plan()).state == tr.COMPLETE


def test_07_a_scan_cannot_start_during_a_long_source_hash(world):
    """The window is O(file size) because the source is hashed between the check and the rename.
    ``after_intent`` fires after that hash, so a scan attempt spanning it must still be refused."""
    world.decide_all()
    seen = {}

    def during_hash(_item):
        try:
            world.coordinator.begin(OWNER)
            seen["outcome"] = "ACCEPTED"
        except cr.Refusal as refusal:
            seen["outcome"] = f"REFUSED:{refusal.reason}"

    result = world.run(world.plan(), after_intent=during_hash)
    assert seen["outcome"] == f"REFUSED:{cr.LIFECYCLE_CONFLICT}"
    assert result.state == tr.COMPLETE


def test_08_page_replacement_during_the_lease_preserves_the_token_and_entries(world):
    world.decide_all()
    plan = world.plan()
    before_entries = [e.clip_id for e in world.registry.page_entries(world.session, world.page)]
    _plan, result, captured = _at_commit_boundary(
        world, lambda: world.registry.publish_page(OWNER, world.session, 9, world.frozen), plan)
    assert captured["outcome"] == f"REFUSED:409:{cr.LIFECYCLE_CONFLICT}"
    after_entries = [e.clip_id for e in world.registry.page_entries(world.session, world.page)]
    assert after_entries == before_entries, "the existing page token and entries survive"
    assert result.state == tr.COMPLETE


# =============================================================================================
# 9-11 — revocation through the three eviction caps
# =============================================================================================

def test_09_page_cap_eviction_cannot_revoke_a_leased_page(world):
    """Page-cap eviction only ever drops the *same owner's* older pages, and the owner is leased,
    so `publish_page` is refused before any eviction can happen."""
    world.decide_all()
    plan = world.plan()
    refusals = []

    def hook(_item):
        for index in range(cr.MAX_PAGES_PER_SESSION + 2):
            try:
                world.registry.publish_page(OWNER, world.session, 20 + index, world.frozen)
                refusals.append("ACCEPTED")
            except cr.Refusal as refusal:
                refusals.append(refusal.reason)

    result = world.run(plan, before_publish=hook)
    assert set(refusals) == {cr.LIFECYCLE_CONFLICT}, refusals
    assert world.registry.page_entries(world.session, world.page), "the leased page is alive"
    assert result.state == tr.COMPLETE


def test_10_session_cap_eviction_cannot_revoke_a_leased_session(world):
    world.decide_all()
    plan = world.plan()
    refusals = []

    def hook(_item):
        for _ in range(cr.MAX_SESSIONS_PER_OWNER + 2):
            try:
                world.registry.new_session(OWNER, world.scan_id)
                refusals.append("ACCEPTED")
            except cr.Refusal as refusal:
                refusals.append(refusal.reason)

    result = world.run(plan, before_publish=hook)
    assert set(refusals) == {cr.LIFECYCLE_CONFLICT}, refusals
    assert world.registry.owner_of(world.session) == OWNER, "the leased session is alive"
    assert result.state == tr.COMPLETE


def test_11_owner_cap_eviction_skips_a_leased_owner(tmp_path):
    """Owner-cap eviction revokes *another* owner's sessions, so the guard cannot refuse it — the
    eviction must skip the leased owner and take the oldest unleased one instead."""
    coordinator = cr.ScanCoordinator(max_owners=3)
    authority = cr.LifecycleAuthority()
    world = World(tmp_path / "a", coordinator=coordinator, authority=authority)
    world.decide_all()
    plan = world.plan()
    survived = {}

    def hook(_item):
        # OWNER is the oldest tracked owner AND is leased; pile on new owners past the cap
        for index in range(cr.MAX_OWNERS + 3):
            world.registry.new_session(f"filler-{index}", "scan-filler")
        try:
            survived["owner_of"] = world.registry.owner_of(world.session)
        except cr.Refusal as refusal:
            survived["owner_of"] = f"<revoked:{refusal.reason}>"

    result = world.run(plan, before_publish=hook)
    assert survived["owner_of"] == OWNER, \
        "a leased owner must not be evicted to satisfy the owner cap"
    assert result.state == tr.COMPLETE
    for item in plan.items:
        assert _sha(item.destination_path) == item.approved_sha256


def test_11b_scan_coordinator_prune_skips_a_leased_owner(tmp_path):
    coordinator = cr.ScanCoordinator(max_owners=2)
    authority = cr.LifecycleAuthority()
    world = World(tmp_path / "a", coordinator=coordinator, authority=authority)
    world.decide_all()
    plan = world.plan()
    seen = {}

    def hook(_item):
        for index in range(6):
            try:
                coordinator.begin(f"other-{index}")
            except cr.Refusal:
                pass
        seen["generation"] = coordinator.newest(OWNER)
        seen["view"] = coordinator.view(OWNER)

    before = coordinator.view(OWNER)
    result = world.run(plan, before_publish=hook)
    assert seen["generation"] == world.generation, "the leased owner's generation survived pruning"
    assert seen["view"] == before, "and so did its authoritative view"
    assert result.state == tr.COMPLETE


# =============================================================================================
# 12-14 — overlapping plans and owner isolation
# =============================================================================================

def test_12_two_overlapping_plans_cannot_execute_simultaneously(world):
    world.decide_all()
    first, second = world.plan(), world.plan(plan_id="second-plan")
    gate = threading.Barrier(2, timeout=30)
    outcomes = {}

    def slow(_item):
        try:
            gate.wait()
        except threading.BrokenBarrierError:            # pragma: no cover
            pass

    def run_first():
        outcomes["first"] = world.run(first, before_publish=slow)

    def run_second():
        try:
            gate.wait()
        except threading.BrokenBarrierError:            # pragma: no cover
            pass
        outcomes["second"] = world.run(second)

    threads = [threading.Thread(target=run_first), threading.Thread(target=run_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "bounded completion: no deadlock"
    assert outcomes["first"].state == tr.COMPLETE
    assert outcomes["second"].state == tr.REFUSED
    published = [n for n in os.listdir(world.library) if not n.startswith(".r5transfer-")]
    assert len(published) == len(first.items), "each clip published exactly once"


def test_13_two_owners_transfer_concurrently_without_blocking_each_other(tmp_path):
    authority = cr.LifecycleAuthority()
    coordinator = cr.ScanCoordinator()
    a = World(tmp_path / "a", owner=OWNER, coordinator=coordinator, authority=authority)
    b = World(tmp_path / "b", owner=OTHER, coordinator=coordinator, authority=authority)
    a.decide_all()
    b.decide_all()
    plan_a, plan_b = a.plan(), b.plan()
    gate = threading.Barrier(2, timeout=30)
    results = {}

    def both(_item):
        try:
            gate.wait()                                  # force genuine overlap
        except threading.BrokenBarrierError:             # pragma: no cover
            pass

    def run(world, plan, key):
        results[key] = world.run(plan, before_publish=both)

    threads = [threading.Thread(target=run, args=(a, plan_a, "a")),
               threading.Thread(target=run, args=(b, plan_b, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "bounded completion: no deadlock between owners"
    assert results["a"].state == tr.COMPLETE
    assert results["b"].state == tr.COMPLETE, "an unrelated owner is never blocked"


def test_14_another_owner_can_scan_while_one_owner_transfers(tmp_path):
    authority = cr.LifecycleAuthority()
    coordinator = cr.ScanCoordinator()
    a = World(tmp_path / "a", owner=OWNER, coordinator=coordinator, authority=authority)
    b = World(tmp_path / "b", owner=OTHER, coordinator=coordinator, authority=authority)
    a.decide_all()
    seen = {}

    def hook(_item):
        try:
            b.scan("other-scan-2")
            seen["b"] = "ACCEPTED"
        except cr.Refusal as refusal:
            seen["b"] = f"REFUSED:{refusal.reason}"
        try:
            a.coordinator.begin(OWNER)
            seen["a"] = "ACCEPTED"
        except cr.Refusal as refusal:
            seen["a"] = f"REFUSED:{refusal.reason}"

    result = a.run(a.plan(), before_publish=hook)
    assert seen["b"] == "ACCEPTED", "owner B's scan must not be blocked by owner A's lease"
    assert seen["a"] == f"REFUSED:{cr.LIFECYCLE_CONFLICT}", "owner A's own scan is refused"
    assert result.state == tr.COMPLETE


def test_26_owner_b_scan_is_not_blocked_merely_because_owner_a_holds_a_lease(tmp_path):
    """Case 26 stated as its own assertion: the guard is owner-scoped, not global."""
    authority = cr.LifecycleAuthority()
    coordinator = cr.ScanCoordinator()
    a = World(tmp_path / "a", owner=OWNER, coordinator=coordinator, authority=authority)
    a.decide_all()
    lease = authority.acquire(OWNER, "plan-x", session_id=a.session, page_token=a.page,
                              scan_id=a.scan_id, identities=[a.identity(e) for e in a.entries])
    try:
        assert authority.holder(OWNER) == "plan-x"
        assert authority.holder(OTHER) == ""
        b = World(tmp_path / "b", owner=OTHER, coordinator=coordinator, authority=authority)
        assert b.entries, "owner B scanned normally"
        with pytest.raises(cr.Refusal):
            coordinator.begin(OWNER)
    finally:
        authority.release(OWNER, "plan-x")
    assert lease.owner == OWNER and lease.plan_id == "plan-x"


# =============================================================================================
# 15-18 — acquisition failure, exceptions, release discipline
# =============================================================================================

def test_15_a_failed_acquisition_rolls_back_only_the_callers_own_leases(world):
    world.decide_all()
    plan = world.plan()
    world.authority.acquire(OWNER, "someone-else", session_id="s", page_token="p",
                            scan_id=world.scan_id, identities=["unrelated-identity"])
    try:
        result = world.run(plan)
        assert result.state == tr.REFUSED
        assert world.authority.holder(OWNER) == "someone-else", \
            "the pre-existing lease is untouched"
        assert world.store.active_transfer(world.identity(world.entries[0])) == "", \
            "and no identity lease leaked from the refused attempt"
    finally:
        world.authority.release(OWNER, "someone-else")
    assert world.authority.holder(OWNER) == ""


@pytest.mark.parametrize("terminal", ["complete", "refused", "rolled_back", "recovery",
                                      "exception"])
def test_16_the_lease_is_released_on_every_terminal_path(world, terminal):
    world.decide_all()
    plan = world.plan()
    real_verify = tr._verify_published

    if terminal == "complete":
        world.run(plan)
    elif terminal == "refused":
        world.store.decide(OWNER, world.identity(world.entries[0]), cr.REJECTED, world.scan_id,
                           content_sha256=_sha(world.entries[0].path))
        world.run(plan)
    elif terminal == "rolled_back":
        tr._verify_published = lambda i, e: (_ for _ in ()).throw(
            tr.TransferRefused("published_mismatch", "injected"))
        try:
            world.run(plan)
        finally:
            tr._verify_published = real_verify
    elif terminal == "recovery":
        def occupy(item, _expected):
            with open(item.source_path, "wb") as handle:
                handle.write(b"occupant")
            raise tr.TransferRefused("published_mismatch", "injected")
        tr._verify_published = occupy
        try:
            world.run(plan)
        finally:
            tr._verify_published = real_verify
    else:
        with pytest.raises(RuntimeError):
            world.run(plan, before_item=lambda _i: (_ for _ in ()).throw(RuntimeError("boom")))

    assert world.authority.holder(OWNER) == "", f"lease leaked on the {terminal} path"
    world.coordinator.begin(OWNER)      # and the lifecycle is usable again


def test_15b_a_failed_identity_map_releases_the_lifecycle_lease_it_just_took(world):
    """§7: *"Failed acquisition rolls back only the caller's newly acquired leases."*

    ``begin_transfer`` takes the owner-scoped lifecycle lease first and the identity map second,
    so the identity step can still refuse after the lifecycle lease has been taken. Without the
    rollback the lifecycle lease **leaks for the lifetime of the process**, and because it is
    owner-scoped that permanently refuses every future scan, session and page for that operator —
    a denial of service on the gallery, from a transfer that never ran.

    The asymmetry is reachable through the public API: release through the authority rather than
    through ``end_transfer`` and the store's identity map still holds the identity. A negative
    control that removed the rollback survived the rest of this file, so this case is the one that
    pins it.
    """
    world.decide_all()
    identities = [world.identity(e) for e in world.entries]

    world.store.begin_transfer(OWNER, "plan-1", identities, session_id=world.session,
                               page_token=world.page, scan_id=world.scan_id)
    assert world.authority.holder(OWNER) == "plan-1"
    # release ONLY the lifecycle half, leaving the store's identity map holding the identities
    assert world.authority.release(OWNER, "plan-1") is True
    assert world.authority.holder(OWNER) == ""
    assert world.store.active_transfer(identities[0]) == "plan-1", "identity still leased"

    with pytest.raises(cr.Refusal) as refused:
        world.store.begin_transfer(OWNER, "plan-2", identities, session_id=world.session,
                                   page_token=world.page, scan_id=world.scan_id)
    assert refused.value.reason == cr.TRANSFER_IN_PROGRESS

    assert world.authority.holder(OWNER) == "", \
        "the lifecycle lease taken by the refused call must be rolled back, not leaked"
    # and the proof that it matters: the owner's lifecycle still works
    world.coordinator.begin(OWNER)
    world.registry.revoke_owner(OWNER)


def test_17_journal_failure_after_publication_still_releases_the_lease(world):
    world.decide_all()

    class FailPublished(tr.TransferJournal):
        def append(self, event, **kw):
            if event.get("state") == tr.PUBLISHED:
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            return super().append(event, **kw)

    world.journal = FailPublished(world.journal.directory)
    result = world.run(world.plan())
    assert result.outcomes[0].state == tr.PUBLISHED_UNJOURNALLED, "R5-P49 contract preserved"
    assert world.authority.holder(OWNER) == ""


def test_18_a_recovery_required_outcome_still_releases_the_lease(world):
    world.decide_all()
    plan = world.plan()
    real_verify = tr._verify_published

    def occupy(item, _expected):
        with open(item.source_path, "wb") as handle:
            handle.write(b"occupant")
        raise tr.TransferRefused("published_mismatch", "injected")

    tr._verify_published = occupy
    try:
        result = world.run(plan)
    finally:
        tr._verify_published = real_verify
    assert result.outcomes[0].state == tr.RECOVERY_REQUIRED
    assert world.authority.holder(OWNER) == ""


def test_one_plan_cannot_release_another_plans_lease(world):
    world.decide_all()
    identities = [world.identity(e) for e in world.entries]
    world.authority.acquire(OWNER, "plan-A", session_id=world.session, page_token=world.page,
                            scan_id=world.scan_id, identities=identities)
    try:
        assert world.authority.release(OWNER, "plan-B") is False, "wrong plan id releases nothing"
        assert world.authority.release("someone-else", "plan-A") is False, "owner mismatch too"
        assert world.authority.holder(OWNER) == "plan-A"
        with pytest.raises(cr.Refusal):
            world.coordinator.begin(OWNER)
    finally:
        assert world.authority.release(OWNER, "plan-A") is True
    assert world.authority.release(OWNER, "plan-A") is False, "releasing twice is not an error"
    world.coordinator.begin(OWNER)


# =============================================================================================
# 19-25 — after release, and what a refusal must not disturb
# =============================================================================================

def test_19_a_stale_scan_after_release_behaves_normally(world):
    world.decide_all()
    plan = world.plan()
    assert world.run(plan).state == tr.COMPLETE
    assert world.authority.holder(OWNER) == ""
    world.store.begin_scan(OWNER, "after-release")       # now permitted
    assert world.store.current_scan(OWNER) == "after-release"
    stale = world.plan()
    assert not stale.ok and any(r.code == "stale_scan" for r in stale.refusals)


def test_20_a_revoked_page_never_reappears_as_current(world):
    world.decide_all()
    assert world.run(world.plan()).state == tr.COMPLETE
    old_page = world.page
    world.found = cr.discover_clips(world.review, True)
    world.scan("after-release-2")
    assert world.page != old_page
    with pytest.raises(cr.Refusal):
        world.registry.page_entries(world.session, old_page)
    view = world.coordinator.view(OWNER)
    assert view.get("page") == world.page, "only the newest page is current"


def test_21_a_refused_scan_does_not_alter_the_durable_scan_journal(world):
    world.decide_all()
    plan = world.plan()
    journal_path = os.path.join(world.store.dir, "review-journal.jsonl")
    before = open(journal_path, "rb").read()
    captured = {}

    def hook(_item):
        try:
            world.store.begin_scan(OWNER, "must-not-be-recorded")
        except cr.Refusal as refusal:
            captured["reason"] = refusal.reason
        captured["bytes"] = open(journal_path, "rb").read()

    world.run(plan, before_publish=hook)
    assert captured["reason"] == cr.LIFECYCLE_CONFLICT
    assert captured["bytes"] == before, "a refused scan appended no durable record"
    assert world.store.current_scan(OWNER) == world.scan_id


def test_22_a_refused_scan_mints_no_credentials(world):
    world.decide_all()
    plan = world.plan()
    captured = {}

    def hook(_item):
        for action in (lambda: world.registry.new_session(OWNER, "x"),
                       lambda: world.coordinator.begin(OWNER)):
            try:
                action()
                captured.setdefault("accepted", []).append(True)
            except cr.Refusal:
                pass
        captured["sessions"] = world.registry.stats()["sessions"]
        captured["generation"] = world.coordinator.newest(OWNER)

    before_sessions = world.registry.stats()["sessions"]
    before_generation = world.coordinator.newest(OWNER)
    world.run(plan, before_publish=hook)
    assert "accepted" not in captured
    assert captured["sessions"] == before_sessions, "no session was minted"
    assert captured["generation"] == before_generation, "no generation was minted"


def test_23_a_refused_page_replacement_preserves_the_token_and_entries(world):
    world.decide_all()
    plan = world.plan()
    before = [e.clip_id for e in world.registry.page_entries(world.session, world.page)]
    _p, result, captured = _at_commit_boundary(
        world, lambda: world.registry.publish_page(OWNER, world.session, 7, world.frozen), plan)
    assert captured["outcome"].endswith(cr.LIFECYCLE_CONFLICT)
    assert [e.clip_id for e in world.registry.page_entries(world.session, world.page)] == before
    assert result.state == tr.COMPLETE


def test_24_the_transfer_succeeds_when_no_conflicting_lifecycle_event_occurs(world):
    world.decide_all()
    plan = world.plan()
    result = world.run(plan)
    assert result.state == tr.COMPLETE and result.advance_page is True
    for item in plan.items:
        assert not os.path.lexists(item.source_path)
        assert _sha(item.destination_path) == item.approved_sha256
    assert world.authority.holder(OWNER) == ""


def test_25_every_lifecycle_operation_succeeds_after_the_lease_is_released(world):
    world.decide_all()
    assert world.run(world.plan()).state == tr.COMPLETE
    assert world.authority.holder(OWNER) == ""
    world.coordinator.begin(OWNER)
    world.store.begin_scan(OWNER, "post-release")
    session = world.registry.new_session(OWNER, "post-release")
    world.registry.publish_page(OWNER, session, 3, world.frozen)
    world.registry.revoke_owner(OWNER)


# =============================================================================================
# bounded stress — repeated interleavings, measured for deadlock
# =============================================================================================

def test_bounded_stress_repeated_interleavings_never_deadlock_and_never_double_publish(tmp_path):
    """Many rounds of transfer-versus-lifecycle contention. Asserts bounded completion rather
    than relying on sleep timing: every thread must join, and every clip must be published
    exactly once with the approved bytes."""
    rounds = 12
    for index in range(rounds):
        root = tmp_path / f"round{index}"
        world = World(root, count=2)
        world.decide_all()
        plan = world.plan()
        start = threading.Barrier(2, timeout=30)
        errors = []
        refusals = []

        def transfer():
            try:
                start.wait()
                refusals.append(("transfer", world.run(plan).state))
            except BaseException as exc:                 # pragma: no cover - diagnostic
                errors.append(exc)

        def lifecycle():
            try:
                start.wait()
                for attempt in range(6):
                    for action in (lambda: world.coordinator.begin(OWNER),
                                   lambda: world.registry.revoke_owner(OWNER),
                                   lambda: world.store.begin_scan(OWNER, f"s{attempt}"),
                                   lambda: world.registry.new_session(OWNER, "s")):
                        try:
                            action()
                            refusals.append(("lifecycle", "ACCEPTED"))
                        except cr.Refusal as refusal:
                            refusals.append(("lifecycle", refusal.reason))
            except BaseException as exc:                 # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=transfer), threading.Thread(target=lifecycle)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive(), f"round {index}: thread did not finish (deadlock)"
        assert not errors, f"round {index}: {errors}"

        transfer_state = [s for kind, s in refusals if kind == "transfer"][0]
        if transfer_state == tr.COMPLETE:
            published = [n for n in os.listdir(world.library)
                         if not n.startswith(".r5transfer-")]
            assert len(published) == len(plan.items), f"round {index}: double publication"
            for item in plan.items:
                assert _sha(item.destination_path) == item.approved_sha256
        # whichever way the interleaving fell, no lease may survive the round
        assert world.authority.holder(OWNER) == "", f"round {index}: lease leaked"


def test_the_guard_never_blocks_and_never_queues(world):
    """Fail-fast: a conflicting operation raises immediately rather than waiting. Measured as
    bounded wall-clock over many attempts, with no sleeps anywhere in the path."""
    world.decide_all()
    identities = [world.identity(e) for e in world.entries]
    world.authority.acquire(OWNER, "p", session_id=world.session, page_token=world.page,
                            scan_id=world.scan_id, identities=identities)
    try:
        import time
        start = time.perf_counter()
        for _ in range(2000):
            try:
                world.coordinator.begin(OWNER)
            except cr.Refusal:
                pass
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"2000 refusals took {elapsed:.2f}s - something is waiting"
    finally:
        world.authority.release(OWNER, "p")
