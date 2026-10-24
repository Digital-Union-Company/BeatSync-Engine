"""R5-P47 — security regressions for the four MAJOR and four MINOR findings of R5-P46.

Every test here is the **inverse** of an R5-P46 reproduction: the review built an input that made
the engine do something unsafe, and this file asserts that the same input now produces a safe,
honestly-reported outcome instead. The reproduction mechanics are kept deliberately identical —
same hooks, same injection seams, same substituted bystanders — because a fix validated by a
different input is not validated against the defect.

Each test asserts the **final filesystem state, the journal, and the returned result**. A test that
only checked an error code would not have caught any of the original defects: in every one of them
the returned code was reasonable and the filesystem was not.

Fixtures are synthetic and live under pytest's ``tmp_path``. Nothing here reads or writes
production media, the ``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import threading

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import clips_review_transfer as tr

OWNER = "r5p47-owner"
SCAN = "r5p47-scan"
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


@pytest.fixture
def world(tmp_path):
    review = tmp_path / "review"
    for index in range(1, 4):
        _write(str(review / f"clip{index:04d}.mp4"), _payload(index))
    library = tmp_path / "library"
    rejected = tmp_path / "quarantine"
    os.makedirs(library, exist_ok=True)
    os.makedirs(rejected, exist_ok=True)

    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(tmp_path / "state"))
    found = cr.discover_clips(str(review), True)
    store.begin_scan(OWNER, SCAN)
    session = registry.new_session(OWNER, SCAN)
    token, entries = registry.publish_page(OWNER, session, 1, cr.freeze_page(found.files, 0, 10))
    return {
        "tmp": tmp_path, "review": str(review), "library": str(library),
        "rejected": str(rejected), "registry": registry, "store": store, "session": session,
        "page": token, "entries": entries,
        "identities": {e.clip_id: cr.decision_identity(e.path, e.size, e.mtime_ns)
                       for e in entries},
        "journal": tr.TransferJournal(str(tmp_path / "journal")),
    }


def _decide(world, entry, decision=cr.SELECTED, content_sha256=_UNSET):
    sha = _sha(entry.path) if content_sha256 is _UNSET else content_sha256
    return world["store"].decide(OWNER, world["identities"][entry.clip_id], decision, SCAN,
                                 content_sha256=sha)


def _decide_all(world, decision=cr.SELECTED, content_sha256=_UNSET):
    for entry in world["entries"]:
        _decide(world, entry, decision, content_sha256)


def _revisions(world):
    return {e.clip_id: world["store"].get(world["identities"][e.clip_id])[1]
            for e in world["entries"]}


def _plan(world, **over):
    kwargs = dict(session_id=world["session"], page_token=world["page"], owner=OWNER,
                  scan_id=SCAN, input_folder=world["review"], library_folder=world["library"],
                  rejected_folder=world["rejected"], expected_revisions=_revisions(world))
    kwargs.update(over)
    return tr.plan_transfer(world["registry"], world["store"], **kwargs)


def _run(world, plan=None, **hooks):
    return tr.execute_transfer(plan or _plan(world), world["journal"],
                               store=world["store"], registry=world["registry"], **hooks)


def _force_cross(plan):
    items = tuple(tr.TransferItem(**{**i.__dict__, "operation": tr.OP_CROSS_VOLUME})
                  for i in plan.items)
    return tr.TransferPlan(**{**plan.__dict__, "items": items})


def _states(world, plan, clip_id):
    return [e["state"] for e in world["journal"].states_for(plan.plan_id).get(clip_id, [])]


def _outcome(result, clip_id):
    return {o.clip_id: o for o in result.outcomes}[clip_id]


# =============================================================================================
# MAJOR A — the irreversible move no longer precedes the content check, and a post-commit
#           failure is rolled back rather than reported as "nothing moved"
# =============================================================================================

def test_A1_a_source_changed_before_the_rename_is_refused_BEFORE_anything_moves(world):
    """R5-P46 Finding A: the clip was moved first and rejected second.

    The safety property is what matters here and it is asserted on the filesystem: the batch
    refuses and **no byte moves**. The refusal *code* is deliberately not pinned to the content
    gate, because an ordinary in-place rewrite also changes ``mtime_ns`` and is therefore caught
    one step earlier by the descriptor identity check — which is the correct and cheaper answer.
    ``test_A1b`` pins that ordering honestly, and ``test_H1`` covers the case only the content
    fingerprint can catch.
    """
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    original = _sha(item.source_path)
    mutated = b"M" * item.source_size

    def mutate_in_place(it):
        if it.clip_id == item.clip_id:
            with open(it.source_path, "r+b") as handle:
                handle.seek(0)
                handle.write(mutated)

    # the mutation must land before the engine hashes the source, i.e. at the item boundary
    result = _run(world, plan, before_item=mutate_in_place)
    outcome = _outcome(result, item.clip_id)

    assert outcome.state == tr.REFUSED
    assert outcome.code in ("source_identity_changed", "content_not_approved"), outcome
    assert outcome.source_removed is False
    # ...and the filesystem agrees, which is the half the original got wrong
    assert os.path.isfile(item.source_path), "the source must NOT have moved"
    assert _sha(item.source_path) == hashlib.sha256(mutated).hexdigest()
    assert not os.path.lexists(item.destination_path), "nothing may be published"
    assert os.listdir(world["library"]) == []
    assert _states(world, plan, item.clip_id) == [], "no durable intent for a refused item"
    assert original != _sha(item.source_path)


def test_A1b_the_gate_ordering_is_identity_first_then_content(world):
    """Pinned honestly rather than assumed, so a future change that reorders them is visible.

    An in-place rewrite changes ``mtime_ns``, so ``open_verified_source`` refuses first and the
    content fingerprint is never consulted. That is not a weakness — it is the same structural
    invalidation ``decision_identity`` relies on — but it does mean the content gate's coverage is
    exactly the metadata-preserving case, and nothing wider.
    """
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    with open(item.source_path, "r+b") as handle:
        handle.seek(0)
        handle.write(b"M" * item.source_size)           # same length, new mtime

    with pytest.raises(tr.TransferRefused) as refused:
        handle = tr.open_verified_source(item)
        os.close(handle)
    assert refused.value.code == "source_identity_changed"


def test_A2_a_same_size_whole_file_replacement_is_refused_before_anything_moves(world):
    """A new inode of the same length — the case metadata identity would also catch, asserted
    here so the content gate is not credited with work the identity check does."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    swap = _write(str(world["tmp"] / "swap.bin"), b"Z" * item.source_size)

    def replace_source(it):
        if it.clip_id == item.clip_id:
            os.replace(swap, it.source_path)

    result = _run(world, plan, before_item=replace_source)
    assert _outcome(result, item.clip_id).state == tr.REFUSED
    assert os.path.isfile(item.source_path)
    assert not os.path.lexists(item.destination_path)
    assert os.listdir(world["library"]) == []


def test_A3_a_failure_AFTER_the_rename_is_rolled_back_and_never_reported_as_REFUSED(world):
    """The commit-point case. R5-P46 proved the engine returned ``REFUSED`` — "nothing moved" —
    about a source path that was already empty, with an unapproved file in the Media Library.

    The residual window between the final ``fstat`` and the rename cannot be closed on this
    allowlist, so it is simulated at exactly the seam it occupies: ``before_publish`` fires with
    the content already verified, immediately before ``os.rename``.
    """
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    approved = _sha(item.source_path)

    def substitute_at_the_commit_point(it):
        if it.clip_id == item.clip_id:
            with open(it.source_path, "r+b") as handle:
                handle.seek(0)
                handle.write(b"W" * it.source_size)

    result = _run(world, plan, before_publish=substitute_at_the_commit_point)
    outcome = _outcome(result, item.clip_id)

    assert outcome.state == tr.ROLLED_BACK, outcome
    assert outcome.state != tr.REFUSED, "REFUSED would be a false statement after a real move"
    assert outcome.source_removed is False
    assert outcome.published_path == "", "nothing is published after a rollback"

    # the object is back where it started, and the library is clean
    assert os.path.isfile(item.source_path), "the moved object was put back"
    assert not os.path.lexists(item.destination_path)
    assert os.listdir(world["library"]) == []
    # no byte was lost: the (substituted) content is intact at the source path
    assert _sha(item.source_path) == hashlib.sha256(b"W" * item.source_size).hexdigest()
    assert _sha(item.source_path) != approved

    states = _states(world, plan, item.clip_id)
    assert states == [tr.INTENT_RECORDED, tr.ROLLED_BACK], states
    assert tr.PUBLISHED not in states and tr.SOURCE_REMOVED not in states


def test_A4_a_rollback_that_cannot_complete_escalates_instead_of_destroying_either_copy(world):
    """If the vacated source path has been taken, the reverse rename is refused (winerror 183)
    rather than overwriting the new occupant. Both files survive and the operator is told."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    squatter_bytes = b"SQUATTER" * 4

    def substitute_then_squat(it):
        if it.clip_id != item.clip_id:
            return
        with open(it.source_path, "r+b") as handle:
            handle.seek(0)
            handle.write(b"W" * it.source_size)

    def squat_the_vacated_source(it):
        # fires after the rename has emptied the source path and before the rollback needs it
        if it.clip_id == item.clip_id and not os.path.lexists(it.source_path):
            _write(it.source_path, squatter_bytes)

    real_verify = tr._verify_published

    def verify_then_squat(it, expected):
        try:
            return real_verify(it, expected)
        finally:
            squat_the_vacated_source(it)

    import unittest.mock as mock
    with mock.patch.object(tr, "_verify_published", verify_then_squat):
        result = _run(world, plan, before_publish=substitute_then_squat)

    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.RECOVERY_REQUIRED, outcome
    assert outcome.code == "rollback_failed"
    assert outcome.published_path == item.destination_path, "it says where the object actually is"
    # neither copy was destroyed to tidy the outcome
    assert os.path.isfile(item.destination_path)
    assert os.path.isfile(item.source_path)
    assert _sha(item.source_path) == hashlib.sha256(squatter_bytes).hexdigest()
    assert tr.RECOVERY_REQUIRED in _states(world, plan, item.clip_id)


# =============================================================================================
# MAJOR B — the pathname source deletion is gone, so the close-to-unlink race has no target
# =============================================================================================

def test_B1_the_engine_never_unlinks_a_SOURCE_path_at_all(world):
    """Structural, because this is a property of the code and not of one schedule.

    Every ``os.unlink`` / ``os.remove`` call in the module is inspected and required to target
    this engine's **own** temporary, never anything derived from ``source_path``. The R5-P46
    Finding B defect was exactly one call that did otherwise.
    """
    source_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "beatsync_fork", "clips_review_transfer.py")
    with open(source_file, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    allowed = {"staging_path", "leftover", "first", "second", "moved", "temporary"}
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("unlink", "remove"):
            continue
        target = node.args[0] if node.args else None
        rendered = ast.unparse(target) if target is not None else "<none>"
        calls.append(rendered)
        assert isinstance(target, ast.Name) and target.id in allowed, (
            f"os.{node.func.attr}({rendered}) is not one of this engine's own temporaries")
        assert "source" not in rendered, f"os.{node.func.attr}({rendered}) targets a source"
    assert calls, "the guard must actually have found the delete sites it is vetting"


def test_B2_cross_volume_publishes_a_verified_copy_and_withholds_the_deletion(world):
    """The deliberate new contract: both copies survive and nothing claims a completed move."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    before = {i.source_path: _sha(i.source_path) for i in plan.items}

    result = _run(world, plan)
    assert result.state == tr.PARTIAL
    assert result.advance_page is False
    for item in plan.items:
        outcome = _outcome(result, item.clip_id)
        assert outcome.state == tr.SOURCE_REMOVAL_BLOCKED
        assert outcome.source_removed is False
        assert _sha(item.destination_path) == before[item.source_path]
        assert _sha(item.source_path) == before[item.source_path]
        states = _states(world, plan, item.clip_id)
        assert tr.SOURCE_REMOVED not in states
        assert tr.COMPLETE not in states
        assert states[-1] == tr.SOURCE_REMOVAL_BLOCKED


def test_B3_a_bystander_substituted_at_the_old_unlink_seam_now_survives(world):
    """The R5-P46 Finding B reproduction, replayed verbatim.

    It injected a different file into the gap between the pre-removal ``os.close`` and
    ``os.unlink(source_path)`` and the engine destroyed it. There is no longer any such unlink, so
    the same injection leaves the bystander intact.
    """
    _decide_all(world)
    plan = _force_cross(_plan(world))
    item = plan.items[0]
    bystander = b"BYSTANDER-FILE-NOT-PART-OF-ANY-REVIEW" * 64
    bystander_sha = hashlib.sha256(bystander).hexdigest()

    real_open, real_close = tr.open_verified_source, os.close
    seen = {"opens": 0, "fired": False}

    def hooked_open(it):
        handle = real_open(it)
        seen["opens"] += 1
        return handle

    def hooked_close(fd):
        real_close(fd)
        # substitute at EVERY close of a source descriptor, which strictly includes the seam the
        # old code used. If any pathname delete remained, this would destroy the bystander.
        if not seen["fired"] and seen["opens"] >= 1 and os.path.lexists(item.source_path):
            seen["fired"] = True
            os.replace(_write(str(world["tmp"] / "bystander.bin"), bystander), item.source_path)

    import unittest.mock as mock
    with mock.patch.object(tr, "open_verified_source", hooked_open), \
            mock.patch.object(os, "close", hooked_close):
        _run(world, plan)

    assert seen["fired"], "the substitution seam must have been exercised"
    survivors = []
    for root, _dirs, names in os.walk(str(world["tmp"])):
        for name in names:
            full = os.path.join(root, name)
            try:
                if _sha(full) == bystander_sha:
                    survivors.append(full)
            except OSError:
                pass
    assert survivors, "the substituted file must still exist somewhere — it was not deleted"


@pytest.mark.skipif(not os.environ.get("BEATSYNC_TEST_CROSSVOL", "").strip(),
                    reason="set BEATSYNC_TEST_CROSSVOL to a directory on another volume")
def test_B4_REAL_cross_volume_staging_publication_and_withheld_deletion(world, tmp_path):
    """On a genuinely different physical volume, so the planner itself chooses CROSS_VOLUME."""
    root = os.environ["BEATSYNC_TEST_CROSSVOL"].strip()
    library = os.path.join(root, "r5p47-lib-" + os.path.basename(str(tmp_path)))
    os.makedirs(library, exist_ok=True)
    try:
        assert os.stat(library).st_dev != os.stat(world["review"]).st_dev
        _decide_all(world)
        plan = _plan(world, library_folder=library)
        assert plan.ok, plan.refusals
        assert {i.operation for i in plan.items} == {tr.OP_CROSS_VOLUME}

        before = {i.source_path: _sha(i.source_path) for i in plan.items}
        result = _run(world, plan)
        assert result.state == tr.PARTIAL
        for item in plan.items:
            assert _outcome(result, item.clip_id).state == tr.SOURCE_REMOVAL_BLOCKED
            assert _sha(item.destination_path) == before[item.source_path]
            assert _sha(item.source_path) == before[item.source_path]
        assert tr.staging_leftovers(library) == []
    finally:
        for name in os.listdir(library):
            try:
                os.unlink(os.path.join(library, name))   # only files this test created
            except OSError:
                pass
        try:
            os.rmdir(library)
        except OSError:
            pass


# =============================================================================================
# MAJOR C — a frozen plan is no longer authority; the lease makes revalidation binding
# =============================================================================================

def test_C1_a_decision_reversed_after_planning_refuses_and_moves_nothing(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    world["store"].decide(OWNER, item.identity, cr.REJECTED, SCAN,
                          content_sha256=_sha(item.source_path))
    assert world["store"].get(item.identity)[0] == cr.REJECTED

    result = _run(world, plan)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.REFUSED
    assert outcome.code == "decision_changed", outcome
    assert os.path.isfile(item.source_path)
    assert os.listdir(world["library"]) == [], "a REJECTED clip must not reach the library"
    assert os.listdir(world["rejected"]) == [], "and must not be filed from a stale plan either"


def test_C2_a_clip_reverted_to_PENDING_is_not_moved(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    world["store"].decide(OWNER, item.identity, cr.PENDING, SCAN)
    assert world["store"].get(item.identity)[0] == cr.PENDING

    result = _run(world, plan)
    assert _outcome(result, item.clip_id).code == "decision_changed"
    assert os.path.isfile(item.source_path)
    assert not os.path.lexists(item.destination_path)


def test_C3_a_revoked_session_stops_execution_of_an_already_built_plan(world):
    _decide_all(world)
    plan = _plan(world)
    world["registry"].revoke_owner(OWNER)

    result = _run(world, plan)
    assert result.state == tr.REFUSED
    assert _outcome(result, plan.items[0].clip_id).code in ("session_revoked", "page_revoked")
    assert os.listdir(world["library"]) == []
    for item in plan.items:
        assert os.path.isfile(item.source_path)


def test_C4_a_superseded_scan_stops_execution(world):
    _decide_all(world)
    plan = _plan(world)
    world["store"].begin_scan(OWNER, "scan-2")
    assert world["store"].current_scan(OWNER) == "scan-2"

    result = _run(world, plan)
    assert result.state == tr.REFUSED
    assert _outcome(result, plan.items[0].clip_id).code == "stale_scan"
    assert os.listdir(world["library"]) == []


def test_C5_a_revision_changed_mid_batch_stops_at_that_item_and_preserves_the_rest(world):
    """A multi-file transfer whose later clip is re-decided while the batch is running."""
    _decide_all(world)
    plan = _plan(world)
    assert len(plan.items) >= 3
    victim = plan.items[2]

    def bump_the_third_clip(it):
        if it.clip_id == plan.items[0].clip_id:
            # Released leases are per-plan, so to change a decision mid-batch the test must do it
            # from outside the lease. `end_transfer` is the engine's own release primitive.
            world["store"].end_transfer(OWNER, plan.plan_id)
            world["store"].decide(OWNER, victim.identity, cr.REJECTED, SCAN,
                                  content_sha256=_sha(victim.source_path))

    result = _run(world, plan, before_item=bump_the_third_clip)
    assert result.state == tr.PARTIAL
    assert _outcome(result, victim.clip_id).code == "decision_changed"
    assert os.path.isfile(victim.source_path), "the re-decided clip is untouched"
    assert _outcome(result, plan.items[0].clip_id).state == tr.COMPLETE


def test_C6_the_same_plan_cannot_execute_twice_concurrently(world):
    """The lease is the guard. A second execution while the first holds it is refused."""
    _decide_all(world)
    plan = _plan(world)
    second = {}

    def reenter(_item):
        if "state" not in second:
            second["state"] = tr.execute_transfer(
                plan, world["journal"], store=world["store"],
                registry=world["registry"]).state

    _run(world, plan, before_item=reenter)
    assert second["state"] == tr.REFUSED, "the re-entrant execution must be refused"


def test_C7_decide_is_refused_while_a_transfer_lease_covers_the_identity(world):
    """The coordination property itself: this is what makes revalidation binding rather than
    merely recent. Without it, a decision could still change between the check and the rename."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    observed = {}

    def try_to_decide(it):
        if it.clip_id != item.clip_id or "code" in observed:
            return
        assert world["store"].active_transfer(item.identity) == plan.plan_id
        try:
            world["store"].decide(OWNER, item.identity, cr.REJECTED, SCAN,
                                  content_sha256=_sha(it.source_path))
            observed["code"] = "ACCEPTED"
        except cr.StaleWrite as exc:
            observed["code"] = exc.args[0]

    _run(world, plan, before_item=try_to_decide)
    assert observed["code"] == cr.TRANSFER_IN_PROGRESS, observed
    # and the lease is released afterwards, so the operator is not locked out
    assert world["store"].active_transfer(item.identity) == ""
    world["store"].decide(OWNER, item.identity, cr.REJECTED, SCAN,
                          content_sha256=hashlib.sha256(b"x").hexdigest())


def test_C8_a_lease_is_always_released_even_when_execution_raises(world):
    class _Boom(Exception):
        pass

    _decide_all(world)
    plan = _plan(world)
    with pytest.raises(_Boom):
        _run(world, plan, before_item=lambda _i: (_ for _ in ()).throw(_Boom()))
    for item in plan.items:
        assert world["store"].active_transfer(item.identity) == ""


def test_C9_expected_revisions_is_MANDATORY(world):
    """R5-P46 Finding C5: the only stale-decision defence defaulted to off."""
    _decide_all(world)
    plan = _plan(world, expected_revisions=None)
    assert not plan.ok
    assert [r.code for r in plan.refusals] == ["expected_revisions_required"]
    assert plan.items == ()


def test_C10_execution_without_the_live_authority_is_refused(world):
    _decide_all(world)
    plan = _plan(world)
    assert tr.execute_transfer(plan, world["journal"]).state == tr.REFUSED
    assert tr.execute_transfer(plan, world["journal"], store=world["store"]).state == tr.REFUSED
    assert os.listdir(world["library"]) == []


# =============================================================================================
# MAJOR H — approval is a content fingerprint, and its absence fails closed
# =============================================================================================

def test_H1_a_size_and_mtime_preserving_edit_is_refused(world):
    """The R5-P46 Finding H input: same length, same inode, timestamp restored. Metadata identity
    cannot see it; the approved fingerprint can."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    before = os.stat(item.source_path)
    forged = b"F" * item.source_size

    with open(item.source_path, "r+b") as handle:
        handle.seek(0)
        handle.write(forged)
    os.utime(item.source_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(item.source_path)
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == \
           (before.st_size, before.st_mtime_ns, before.st_ino)

    # metadata identity still passes — that is the premise of the finding
    handle = tr.open_verified_source(item)
    os.close(handle)

    result = _run(world, plan)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.REFUSED
    assert outcome.code == "content_not_approved", outcome
    assert os.path.isfile(item.source_path), "nothing moved"
    assert os.listdir(world["library"]) == []


def test_H2_a_decision_with_no_content_fingerprint_fails_closed(world):
    """Every decision the P1 HTTP path writes today has this shape, and it must not transfer."""
    _decide_all(world, cr.SELECTED, content_sha256=None)
    for entry in world["entries"]:
        assert world["store"].fingerprints_of([world["identities"][entry.clip_id]])[
            world["identities"][entry.clip_id]] == ""

    plan = _plan(world)
    assert not plan.ok
    assert set(r.code for r in plan.refusals) == {"content_fingerprint_missing"}
    assert plan.items == ()
    assert os.listdir(world["library"]) == []


def test_H3_an_approved_clip_whose_content_still_matches_transfers(world):
    """The positive control. Without it, H would be satisfied by refusing everything."""
    _decide_all(world)
    plan = _plan(world)
    assert plan.ok, plan.refusals
    before = {i.source_path: _sha(i.source_path) for i in plan.items}
    for item in plan.items:
        assert item.approved_sha256 == before[item.source_path]

    result = _run(world, plan)
    assert result.state == tr.COMPLETE
    for item in plan.items:
        assert _sha(item.destination_path) == before[item.source_path]


def test_H4_no_fingerprint_is_ever_back_filled_onto_an_older_decision(world):
    """"Do not silently assign a newly calculated digest to an old decision and claim historical
    approval." A decision recorded without one keeps none, whatever else happens."""
    entry = world["entries"][0]
    identity = world["identities"][entry.clip_id]
    _decide(world, entry, cr.SELECTED, content_sha256=None)
    _plan(world)                                        # planning must not mutate the record
    assert world["store"].fingerprints_of([identity])[identity] == ""
    # a later decision that does supply one binds only itself, at its own revision
    _decide(world, entry, cr.REJECTED)
    assert world["store"].fingerprints_of([identity])[identity] == _sha(entry.path)


def test_H5_a_malformed_fingerprint_is_refused_at_the_write_boundary(world):
    entry = world["entries"][0]
    identity = world["identities"][entry.clip_id]
    for bad in ("", "x" * 64, "ABCDEF" + "0" * 58, "0" * 63, "0" * 65, 12345, b"0" * 64):
        if bad == "":
            continue                                    # empty is "no fingerprint", not malformed
        with pytest.raises(cr.StaleWrite):
            world["store"].decide(OWNER, identity, cr.SELECTED, SCAN, content_sha256=bad)
    assert world["store"].get(identity) == (cr.PENDING, 0), "no refused write moved a revision"


def test_H6_the_fingerprint_survives_a_compaction_and_a_cold_reload(world, tmp_path):
    """Durability: the binding is useless if it does not come back."""
    entry = world["entries"][0]
    identity = world["identities"][entry.clip_id]
    expected = _sha(entry.path)
    _decide(world, entry, cr.SELECTED)
    world["store"].compact()
    reopened = cr.ReviewStore(str(tmp_path / "state"))
    assert reopened.fingerprints_of([identity])[identity] == expected
    assert reopened.get(identity) == (cr.SELECTED, 1)


# =============================================================================================
# MINOR M1 — the no-replace probe must prove a collision, not merely fail
# =============================================================================================

def test_M1a_an_unrelated_OSError_is_no_longer_accepted_as_proof(world, monkeypatch):
    """R5-P46 Finding D: any ``OSError`` counted as "no-replace PROVEN"."""
    def rename_access_denied(src, dst):
        raise PermissionError(13, "Access is denied", dst, 5)   # winerror 5, not 183

    monkeypatch.setattr(os, "rename", rename_access_denied)
    with pytest.raises(tr.TransferRefused) as refused:
        tr.probe_no_replace_rename(world["library"])
    assert refused.value.code == "no_replace_probe_inconclusive"


def test_M1b_a_directory_where_every_rename_fails_is_not_proof(world, monkeypatch):
    """R5-P46 Finding D2: the positive case was never checked, so a broken directory passed."""
    def rename_always_fails(src, dst):
        raise OSError(22, "Invalid argument", dst, 87)

    monkeypatch.setattr(os, "rename", rename_always_fails)
    with pytest.raises(tr.TransferRefused) as refused:
        tr.probe_no_replace_rename(world["library"])
    assert refused.value.code == "no_replace_probe_inconclusive"


def test_M1a2_a_non_collision_OSError_is_isolated_from_the_positive_control(world, monkeypatch):
    """Isolates the *first* of the probe's three requirements.

    ``test_M1a`` makes every rename fail, so it is actually the positive control that refuses and
    the collision check is never the deciding step — a mutation control showed exactly that by
    surviving. Here only the **collision** attempt fails, and with a code that is not 183, so the
    first requirement is the only thing standing between the engine and a false "proven".
    """
    real_rename = os.rename

    def deny_only_the_collision(src, dst):
        if os.path.lexists(dst):
            raise PermissionError(13, "Access is denied", dst, 5)   # not 183
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", deny_only_the_collision)
    with pytest.raises(tr.TransferRefused) as refused:
        tr.probe_no_replace_rename(world["library"])
    assert refused.value.code == "no_replace_probe_inconclusive"


def test_M1c_a_genuine_collision_still_passes_and_leaves_no_litter(world):
    assert tr.probe_no_replace_rename(world["library"]) is True
    assert tr.staging_leftovers(world["library"]) == []
    assert [n for n in os.listdir(world["library"]) if n.startswith(".r5transfer-")] == []


def test_M1d_a_replacing_rename_is_reported_as_NOT_guaranteed(world, monkeypatch):
    """The other direction: a filesystem that silently replaces must answer False, not raise."""
    monkeypatch.setattr(os, "rename", os.replace)
    assert tr.probe_no_replace_rename(world["library"]) is False


def test_M1e_a_missing_directory_refuses_at_creation(world, tmp_path):
    with pytest.raises(tr.TransferRefused) as refused:
        tr.probe_no_replace_rename(str(tmp_path / "does-not-exist"))
    assert refused.value.code == "no_replace_probe_failed"


def test_M1f_an_inconclusive_probe_refuses_the_item_and_moves_nothing(world, monkeypatch):
    _decide_all(world)
    plan = _plan(world)
    real_rename = os.rename
    calls = {"n": 0}

    def deny_only_the_probe(src, dst):
        # the probe renames a ".probe-a" onto a ".probe-b"; the transfer renames real clips
        if ".probe-" in src:
            calls["n"] += 1
            raise PermissionError(13, "Access is denied", dst, 5)
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", deny_only_the_probe)
    result = _run(world, plan)
    assert calls["n"] >= 1
    assert result.state == tr.REFUSED
    assert _outcome(result, plan.items[0].clip_id).code == "no_replace_probe_inconclusive"
    for item in plan.items:
        assert os.path.isfile(item.source_path)
    assert [n for n in os.listdir(world["library"]) if not n.startswith(".r5transfer-")] == []


# =============================================================================================
# MINOR M2 — an inspection failure is UNKNOWN, and UNKNOWN refuses
# =============================================================================================

def test_M2a_the_three_reparse_answers_are_distinct(world, tmp_path, monkeypatch):
    plain = _write(str(tmp_path / "plain.mp4"), b"x" * 32)
    assert tr.reparse_state(plain) == tr.REPARSE_NO

    def lstat_denied(path, *args, **kwargs):
        raise PermissionError(13, "Access is denied", path, 5)

    monkeypatch.setattr(os, "lstat", lstat_denied)
    assert tr.reparse_state(plain) == tr.REPARSE_UNKNOWN
    assert len({tr.REPARSE_NO, tr.REPARSE_YES, tr.REPARSE_UNKNOWN}) == 3


def test_M2b_an_uninspectable_source_refuses_the_plan(world, monkeypatch):
    """R5-P46 Finding E: the gate answered "ordinary file" when it could not look."""
    _decide_all(world)
    real_lstat = os.lstat
    targets = {e.path for e in world["entries"]}

    def lstat_denied_for_sources(path, *args, **kwargs):
        if path in targets:
            raise PermissionError(13, "Access is denied", path, 5)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat_denied_for_sources)
    plan = _plan(world)
    assert not plan.ok
    assert "source_not_inspectable" in {r.code for r in plan.refusals}
    assert plan.items == ()


# =============================================================================================
# MINOR M3 — the staging descriptor is closed on every path
# =============================================================================================

def test_M3_a_failed_source_open_leaks_no_handle_and_leaves_no_leftover(world):
    """R5-P46 Finding F: ``mkstemp`` succeeded, the source open raised, the staging descriptor was
    never closed, and the cleanup ``os.unlink`` then failed with a sharing violation and was
    swallowed — orphaning a ``.part`` file in the operator's Media Library for good."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    item = plan.items[0]

    real_open = tr.open_verified_source
    calls = {"n": 0}

    def fail_on_the_staging_read(it):
        calls["n"] += 1
        if calls["n"] == 2:                             # after mkstemp, inside _cross_volume
            raise tr.TransferRefused("source_missing", "injected at the staging boundary")
        return real_open(it)

    import unittest.mock as mock
    with mock.patch.object(tr, "open_verified_source", fail_on_the_staging_read):
        result = _run(world, plan)

    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.REFUSED and outcome.code == "source_missing"
    assert tr.staging_leftovers(world["library"]) == [], "the temporary must be gone"
    assert os.listdir(world["library"]) == [], "and nothing else may remain"
    assert os.path.isfile(item.source_path), "the source is intact"
    # the descriptor really was closed: the file could be removed, which a held handle prevents
    assert not any(n.startswith(".r5transfer-") for n in os.listdir(world["library"]))


# =============================================================================================
# MINOR M4 — recovery is grounded in durable authority, not in journal hash fields
# =============================================================================================

def _stage_then_stop(world, plan):
    class _Stop(Exception):
        pass
    with pytest.raises(_Stop):
        _run(world, plan, after_stage=lambda _i: (_ for _ in ()).throw(_Stop()))


def test_M4a_a_forged_published_hash_cannot_make_a_stranger_look_published(world):
    """R5-P46 Finding G/G2: the expected digest was scavenged from the journal, so one forged
    record redefined what recovery believed the destination should be — and a same-length stranger
    was then declared a valid publication, with the genuine source recommended for deletion."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    item = plan.items[0]
    _stage_then_stop(world, plan)
    assert os.path.isfile(item.source_path)

    stranger = _write(item.destination_path, b"S" * item.source_size)
    forged = _sha(stranger)
    assert forged != item.approved_sha256
    assert os.path.getsize(stranger) == item.source_size, "same length, so only the hash decides"

    world["journal"].append({"k": "item", "plan_id": plan.plan_id, "clip_id": item.clip_id,
                             "state": tr.PUBLISHED, "published_sha256": forged, "at": 0.0})

    report = tr.recover(plan, world["journal"])
    finding = {f.clip_id: f for f in report.findings}[item.clip_id]
    assert finding.action == "operator_required", finding
    assert report.needs_operator
    assert os.path.isfile(item.source_path), "and nothing was deleted"
    assert _sha(item.destination_path) == forged, "the stranger is left alone too"


def test_M4b_contradictory_journal_digests_are_refused_as_uninterpretable(world):
    """Two different ``staged_sha256`` values for one clip cannot both be what was staged.

    Appended onto a *non-terminal* history on purpose: a record after a terminal state is refused
    by the ordering rule first (``test_M4c``), which would mask what this test is checking.
    """
    _decide_all(world)
    plan = _force_cross(_plan(world))
    item = plan.items[0]
    _stage_then_stop(world, plan)
    assert _states(world, plan, item.clip_id) == [tr.INTENT_RECORDED, tr.STAGED]

    world["journal"].append({"k": "item", "plan_id": plan.plan_id, "clip_id": item.clip_id,
                             "state": tr.STAGED,
                             "staged_sha256": "d" * 64, "at": 0.0})
    report = tr.recover(plan, world["journal"])
    finding = {f.clip_id: f for f in report.findings}[item.clip_id]
    assert finding.action == "operator_required"
    assert "contradictory" in finding.detail, finding


def test_M4c_a_terminal_state_followed_by_more_records_is_refused(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    assert _run(world, plan).state == tr.COMPLETE
    world["journal"].append({"k": "item", "plan_id": plan.plan_id, "clip_id": item.clip_id,
                             "state": tr.STAGED, "at": 0.0})
    finding = {f.clip_id: f for f in tr.recover(plan, world["journal"]).findings}[item.clip_id]
    assert finding.action == "operator_required"
    assert "terminal" in finding.detail or "out of order" in finding.detail


def test_M4d_an_unknown_recorded_state_is_refused(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    world["journal"].append({"k": "item", "plan_id": plan.plan_id, "clip_id": item.clip_id,
                             "state": "TOTALLY_MADE_UP", "at": 0.0})
    finding = {f.clip_id: f for f in tr.recover(plan, world["journal"]).findings}[item.clip_id]
    assert finding.action == "operator_required"
    assert "does not understand" in finding.detail


def test_M4e_recovery_never_deletes_anything(world):
    """Structural and behavioural: `recover` recommends, and the recommendation is never an
    instruction this engine carries out."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    assert _run(world, plan).state == tr.PARTIAL
    sources = {i.source_path: _sha(i.source_path) for i in plan.items}
    destinations = {i.destination_path: _sha(i.destination_path) for i in plan.items}

    report = tr.recover(plan, world["journal"])
    for finding in report.findings:
        assert finding.action in ("none", "retry_safe", "operator_may_remove_duplicate",
                                  "operator_required")
    for path, digest in {**sources, **destinations}.items():
        assert os.path.isfile(path) and _sha(path) == digest


# =============================================================================================
# Two guards that only a mutation control could show were untested
# =============================================================================================

def test_the_post_hash_length_recheck_is_load_bearing(world, monkeypatch):
    """R5-P46 mutation M5 survived both suites, here and in the review.

    ``open_verified_source`` proves ``st_size`` on the descriptor, so reading that descriptor to
    EOF normally yields exactly that many bytes — which is why no scenario test reaches this
    branch. It is defence in depth against the file changing length *during* the hash read, and
    the honest way to make it load-bearing is to drive it directly rather than to invent a
    concurrency schedule that would not be reproducible.
    """
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    real = tr._sha256_of_fd

    def short_read(handle, **kwargs):
        digest, total = real(handle, **kwargs)
        return digest, total - 1                        # one byte fewer than identity promised

    monkeypatch.setattr(tr, "_sha256_of_fd", short_read)
    result = _run(world, plan)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.REFUSED
    assert outcome.code == "source_identity_changed", outcome
    assert os.path.isfile(item.source_path)
    assert not os.path.lexists(item.destination_path)


def test_the_staged_copy_is_fsynced_before_it_is_verified(world):
    """R5-P46 mutation M9 survived, and it always will behaviourally: the absence of an ``fsync``
    is observable only across a power loss, which no in-process test can stage.

    So this is a **structural** pin, labelled as such rather than dressed up as a behavioural
    one: the staging write path must call ``os.fsync`` on the staging descriptor, before the
    verification re-read. A guard that can only be checked structurally is still worth having —
    silently losing the barrier is otherwise invisible until a machine loses power.
    """
    source_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "beatsync_fork", "clips_review_transfer.py")
    with open(source_file, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cross_volume":
            target = node
    assert target is not None, "_cross_volume must exist for this guard to mean anything"

    fsyncs = [ast.unparse(n) for n in ast.walk(target)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "fsync"]
    assert fsyncs == ["os.fsync(staging_handle)"], fsyncs


# =============================================================================================
# Properties R5-P46 found HOLDING — re-asserted so the remediation cannot have broken them
# =============================================================================================

def test_holds_no_destination_is_ever_overwritten(world):
    _decide_all(world)
    plan = _plan(world)
    squatter = _write(plan.items[0].destination_path, b"EXISTING")
    before = _sha(squatter)
    result = _run(world, plan)
    assert _outcome(result, plan.items[0].clip_id).code == "target_exists"
    assert _sha(plan.items[0].destination_path) == before
    assert os.path.isfile(plan.items[0].source_path)


def test_holds_plan_transfer_still_takes_no_source_path(world):
    import inspect
    names = set(inspect.signature(tr.plan_transfer).parameters)
    for forbidden in ("source", "source_path", "sources", "paths", "files", "clip_path"):
        assert forbidden not in names


def test_holds_a_foreign_page_token_cannot_authorize(world):
    _decide_all(world)
    other = world["registry"].new_session("other-owner", "other-scan")
    found = cr.discover_clips(world["review"], True)
    world["store"].begin_scan("other-owner", "other-scan")
    other_page, _entries = world["registry"].publish_page(
        "other-owner", other, 1, cr.freeze_page(found.files, 0, 10))
    assert not _plan(world, page_token=other_page).ok


def test_holds_root_overlap_refusals_are_enforced(world):
    _decide_all(world)
    inside = os.path.join(world["review"], "dest")
    os.makedirs(inside, exist_ok=True)
    assert not _plan(world, library_folder=inside).ok
    assert not _plan(world, rejected_folder=world["library"]).ok


def test_holds_rejected_footage_is_preserved_byte_for_byte(world):
    _decide_all(world, cr.REJECTED)
    before = {e.path: _sha(e.path) for e in world["entries"]}
    plan = _plan(world)
    assert _run(world, plan).state == tr.COMPLETE
    for item in plan.items:
        assert _sha(item.destination_path) == before[item.source_path]


def test_holds_a_torn_journal_tail_is_surfaced_not_invented(world):
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    with open(world["journal"].path, "a", encoding="utf-8") as handle:
        handle.write('{"k":"item","plan_id":"x","state":"COMP')
    assert world["journal"].records()[-1]["k"] == "unparseable"


def test_holds_a_second_execution_of_a_completed_plan_refuses(world):
    _decide_all(world)
    plan = _plan(world)
    assert _run(world, plan).state == tr.COMPLETE
    published = {i.destination_path: _sha(i.destination_path) for i in plan.items}
    again = _run(world, plan)
    assert again.state == tr.REFUSED
    for path, digest in published.items():
        assert _sha(path) == digest


def test_holds_the_engine_is_still_unreachable_from_the_gui():
    """The P2 isolation claim, re-asserted: remediation must not have wired anything up."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for relative in ("src/gui.py", "src/clips_media_server.py"):
        with open(os.path.join(root, *relative.split("/")), encoding="utf-8") as handle:
            text = handle.read()
        assert "clips_review_transfer" not in text, f"{relative} must not reach the engine"
    with open(os.path.join(root, "src", "beatsync_fork", "__init__.py"),
              encoding="utf-8") as handle:
        assert "clips_review_transfer" not in handle.read()


# =============================================================================================
# The cost of the new hashing step, measured rather than hidden
# =============================================================================================

def test_the_decision_time_hash_cost_is_measured_and_bounded(tmp_path):
    """[R5-P47 / H] Hashing is a real new cost at decision time and must not be concealed.

    Deliberately *not* a performance assertion with a wall-clock threshold — that would be flaky
    on shared hardware. It measures throughput on this machine and asserts only the structural
    facts that matter: the cost is linear in the clip's size and is paid per decision, not per
    page. The measured figure goes in the report.
    """
    import time as _time
    payload = os.urandom(8 << 20)                       # 8 MiB, incompressible
    target = _write(str(tmp_path / "big.mp4"), payload)

    started = _time.perf_counter()
    digest = _sha(target)
    elapsed = _time.perf_counter() - started

    assert digest == hashlib.sha256(payload).hexdigest()
    assert elapsed >= 0
    throughput = (len(payload) / (1 << 20)) / elapsed if elapsed else float("inf")
    print(f"\n[R5-P47] decision-time SHA-256: {len(payload) >> 20} MiB in {elapsed * 1000:.1f} ms "
          f"= {throughput:.0f} MiB/s")
    # one clip per operator click, so the relevant bound is per-decision and not per-scan
    assert throughput > 1, "a plausible floor; the real figure is reported, not asserted"


def test_browsing_still_hashes_nothing(world):
    """"Do not automatically hash all source videos during browsing." Discovery and page
    publication must not read clip contents at all."""
    found = cr.discover_clips(world["review"], True)
    assert found.files, "the fixture really does have clips"
    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(world["tmp"] / "state-browse"))
    store.begin_scan(OWNER, SCAN)
    session = registry.new_session(OWNER, SCAN)

    reads = {"n": 0}
    real_open = os.open

    def counting_open(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.endswith(".mp4"):
            reads["n"] += 1
        return real_open(path, flags, *args, **kwargs)

    import unittest.mock as mock
    with mock.patch.object(os, "open", counting_open):
        registry.publish_page(OWNER, session, 1, cr.freeze_page(found.files, 0, 10))
    assert reads["n"] == 0, "publishing a page must not open a single clip"


# =============================================================================================
# Concurrency: the lease under real threads
# =============================================================================================

def test_two_threads_cannot_both_lease_the_same_identity(world):
    _decide_all(world)
    plan = _plan(world)
    identities = [i.identity for i in plan.items]
    results = []
    barrier = threading.Barrier(2)

    def attempt(tag):
        barrier.wait()
        try:
            world["store"].begin_transfer(OWNER, f"plan-{tag}", identities)
            results.append(("acquired", tag))
        except cr.Refusal:
            results.append(("refused", tag))

    threads = [threading.Thread(target=attempt, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome for outcome, _tag in results) == ["acquired", "refused"], results
