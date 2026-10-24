"""[R5-P45] Clips Preview Selector P2 — the safe-transfer core.

Every case here runs against **real files on the real filesystem**, driven through the real
``ClipRegistry`` and ``ReviewStore``, because the whole point of this component is what it does to
a disk. Mocked filesystem calls would prove nothing about ``os.rename``'s no-replace semantics,
which is the primitive the accepted specification names.

The fixtures only ever create and delete files beneath pytest's ``tmp_path``. Nothing here reads or
writes production media, the production review journal, F:, J: or any other task's workspace — and
a test asserts that.

One arm is opt-in: a genuinely cross-volume move needs a second volume, which ``tmp_path`` cannot
provide. Set ``BEATSYNC_TEST_CROSSVOL`` to a writable directory on a *different* volume to exercise
it for real, following the same convention ``tests/test_audio_mixdown.py`` uses for real FFmpeg.
Without it those cases skip and say so; the STAGE→VERIFY→PUBLISH→REMOVE sequence is still exercised
end to end on one volume by forcing the operation, so the code path is never untested.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from beatsync_fork import clips_review as cr                       # noqa: E402
from beatsync_fork import clips_review_transfer as tr              # noqa: E402

OWNER = "session-hash-a"
SCAN = "scan-1"


# =============================================================================================
# Fixtures
# =============================================================================================

def _write(path: str, payload: bytes) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _sha(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _payload(index: int, size: int = 4096) -> bytes:
    """Distinct, media-like bytes so a mix-up cannot pass by coincidence."""
    return (bytes([index % 251]) + bytes(range(256))) * (size // 257 + 1)


@pytest.fixture
def world(tmp_path):
    """Four reviewed clips, a library, a quarantine, a state dir and a journal dir."""
    review = tmp_path / "review"
    for index in range(1, 5):
        _write(str(review / f"clip{index:04d}.mp4"), _payload(index))

    library = tmp_path / "library"
    rejected = tmp_path / "quarantine"
    os.makedirs(library, exist_ok=True)
    os.makedirs(rejected, exist_ok=True)

    # A sentinel that is deliberately NOT part of the review, to prove nothing strays.
    outside = _write(str(tmp_path / "elsewhere" / "private.mp4"), b"DO-NOT-TOUCH" * 64)

    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(tmp_path / "state"))
    found = cr.discover_clips(str(review), True)
    store.begin_scan(OWNER, SCAN)
    session = registry.new_session(OWNER, SCAN)
    token, entries = registry.publish_page(OWNER, session, 1, cr.freeze_page(found.files, 0, 10))

    identities = {e.clip_id: cr.decision_identity(e.path, e.size, e.mtime_ns) for e in entries}
    journal = tr.TransferJournal(str(tmp_path / "journal"))

    return {
        "tmp": tmp_path, "review": str(review), "library": str(library),
        "rejected": str(rejected), "registry": registry, "store": store,
        "session": session, "page": token, "entries": entries, "identities": identities,
        "journal": journal, "outside": outside,
        "outside_sha": _sha(outside),
    }


def _decide(world, entry, decision):
    return world["store"].decide(OWNER, world["identities"][entry.clip_id], decision, SCAN)


def _decide_all(world, decision=cr.SELECTED):
    for entry in world["entries"]:
        _decide(world, entry, decision)


def _plan(world, **overrides):
    kwargs = dict(
        session_id=world["session"], page_token=world["page"], owner=OWNER, scan_id=SCAN,
        input_folder=world["review"], library_folder=world["library"],
        rejected_folder=world["rejected"],
    )
    kwargs.update(overrides)
    return tr.plan_transfer(world["registry"], world["store"], **kwargs)


def _run(world, plan=None, **hooks):
    return tr.execute_transfer(plan or _plan(world), world["journal"], **hooks)


def _codes(plan):
    return sorted({r.code for r in plan.refusals})


def _crossvol_root():
    return os.environ.get("BEATSYNC_TEST_CROSSVOL", "").strip()


# =============================================================================================
# Case 1 — successful SELECTED same-volume move
# =============================================================================================

def test_01_selected_clips_move_to_the_library(world):
    _decide_all(world, cr.SELECTED)
    before = {e.path: _sha(e.path) for e in world["entries"]}

    plan = _plan(world)
    assert plan.ok, _codes(plan)
    assert len(plan.items) == 4
    assert {i.operation for i in plan.items} == {tr.OP_SAME_VOLUME}
    assert {i.destination_role for i in plan.items} == {cr.ROLE_LIBRARY}

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.COMPLETE
    assert result.counts == {"moved": 4, "failed": 0, "remaining": 0}
    assert result.advance_page is True

    for item in plan.items:
        assert not os.path.exists(item.source_path), "the source must be gone"
        assert os.path.isfile(item.destination_path), "the clip must be in the library"
        assert _sha(item.destination_path) == before[item.source_path], "bytes must be identical"
    assert sorted(os.listdir(world["library"])) == [
        "clip0001.mp4", "clip0002.mp4", "clip0003.mp4", "clip0004.mp4"]
    assert os.listdir(world["rejected"]) == [], "quarantine must be untouched"


# =============================================================================================
# Case 2 — successful REJECTED move into quarantine
# =============================================================================================

def test_02_rejected_clips_move_to_quarantine_and_are_not_deleted(world):
    _decide_all(world, cr.REJECTED)
    before = {e.path: _sha(e.path) for e in world["entries"]}

    plan = _plan(world)
    assert {i.destination_role for i in plan.items} == {cr.ROLE_REJECTED}
    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.COMPLETE

    assert sorted(os.listdir(world["rejected"])) == [
        "clip0001.mp4", "clip0002.mp4", "clip0003.mp4", "clip0004.mp4"]
    assert os.listdir(world["library"]) == []
    for item in plan.items:
        assert _sha(item.destination_path) == before[item.source_path]


def test_02b_a_mixed_page_splits_between_library_and_quarantine(world):
    entries = world["entries"]
    _decide(world, entries[0], cr.SELECTED)
    _decide(world, entries[1], cr.REJECTED)
    _decide(world, entries[2], cr.SELECTED)
    _decide(world, entries[3], cr.REJECTED)

    result = _run(world)
    assert result.state == tr.COMPLETE
    assert sorted(os.listdir(world["library"])) == ["clip0001.mp4", "clip0003.mp4"]
    assert sorted(os.listdir(world["rejected"])) == ["clip0002.mp4", "clip0004.mp4"]


# =============================================================================================
# Case 3 — PENDING prevents transfer
# =============================================================================================

def test_03_a_single_pending_clip_refuses_the_whole_page(world):
    for entry in world["entries"][:3]:
        _decide(world, entry, cr.SELECTED)
    # the fourth stays PENDING

    plan = _plan(world)
    assert not plan.ok
    assert "pending_decision" in _codes(plan)

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.REFUSED
    assert result.advance_page is False
    assert os.listdir(world["library"]) == [] and os.listdir(world["rejected"]) == []
    for entry in world["entries"]:
        assert os.path.isfile(entry.path), "no clip may move while any is undecided"


# =============================================================================================
# Case 4 — stale revision prevents transfer
# =============================================================================================

def test_04_a_stale_expected_revision_refuses_that_clip(world):
    _decide_all(world, cr.SELECTED)
    entries = world["entries"]
    expected = {e.clip_id: 1 for e in entries}
    expected[entries[2].clip_id] = 0                    # the caller's view is one behind

    plan = _plan(world, expected_revisions=expected)
    assert not plan.ok
    assert "stale_revision" in _codes(plan)
    assert tr.execute_transfer(plan, world["journal"]).state == tr.REFUSED
    assert os.listdir(world["library"]) == []


def test_04b_a_matching_revision_set_is_accepted(world):
    _decide_all(world, cr.SELECTED)
    expected = {e.clip_id: 1 for e in world["entries"]}
    plan = _plan(world, expected_revisions=expected)
    assert plan.ok, _codes(plan)
    assert tr.execute_transfer(plan, world["journal"]).state == tr.COMPLETE


def test_04c_a_missing_or_non_integer_revision_is_refused_not_coerced(world):
    _decide_all(world, cr.SELECTED)
    entries = world["entries"]
    for bad in ({}, {e.clip_id: True for e in entries}, {e.clip_id: 1.0 for e in entries}):
        plan = _plan(world, expected_revisions=bad)
        assert not plan.ok
        assert {"revision_required", "stale_revision"} & set(_codes(plan)), _codes(plan)


# =============================================================================================
# Case 5 — revoked session / page prevents transfer
# =============================================================================================

def test_05_a_revoked_owner_refuses_the_plan(world):
    _decide_all(world, cr.SELECTED)
    world["registry"].revoke_owner(OWNER)
    plan = _plan(world)
    assert not plan.ok
    assert tr.execute_transfer(plan, world["journal"]).state == tr.REFUSED
    assert os.listdir(world["library"]) == []


def test_05b_a_superseded_page_refuses_the_plan(world):
    _decide_all(world, cr.SELECTED)
    found = cr.discover_clips(world["review"], True)
    world["registry"].publish_page(OWNER, world["session"], 2, cr.freeze_page(found.files, 0, 10))
    # the original page token is no longer the active one; a stale confirm must not transfer
    plan = _plan(world)
    if plan.ok:
        pytest.fail("a superseded page token must not yield an executable plan")
    assert os.listdir(world["library"]) == []


def test_05c_a_stale_scan_id_refuses_the_plan(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world, scan_id="scan-999")
    assert not plan.ok
    assert "stale_scan" in _codes(plan)


def test_05d_another_owner_cannot_plan_this_session(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world, owner="session-hash-b")
    assert not plan.ok
    assert "owner_mismatch" in _codes(plan)


# =============================================================================================
# Case 6 — source missing before execution
# =============================================================================================

def test_06_a_source_deleted_after_planning_refuses_that_item(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    assert plan.ok
    os.unlink(plan.items[1].source_path)                # vanishes between plan and execute

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.PARTIAL
    outcomes = {o.clip_id: o for o in result.outcomes}
    assert outcomes[plan.items[1].clip_id].code == "source_missing"
    assert result.advance_page is False
    # the first item completed; the rest were not attempted
    assert outcomes[plan.items[0].clip_id].state == tr.COMPLETE
    assert outcomes[plan.items[2].clip_id].code == "not_attempted"
    assert outcomes[plan.items[3].clip_id].code == "not_attempted"


# =============================================================================================
# Case 7 — source modified after review
# =============================================================================================

def test_07_a_source_modified_after_review_is_refused_at_plan_time(world):
    _decide_all(world, cr.SELECTED)
    target = world["entries"][0].path
    with open(target, "ab") as handle:
        handle.write(b"appended")                       # size and mtime both change

    plan = _plan(world)
    assert not plan.ok
    assert "source_identity_changed" in _codes(plan)
    assert os.listdir(world["library"]) == []


def test_07b_a_source_modified_between_plan_and_execute_is_refused_at_execute_time(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    assert plan.ok
    with open(plan.items[0].source_path, "ab") as handle:
        handle.write(b"later")

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "source_identity_changed"
    assert os.path.isfile(plan.items[0].source_path), "a refused item must stay put"
    assert os.listdir(world["library"]) == []


# =============================================================================================
# Case 8 — same-size replacement (a size check alone would miss it)
# =============================================================================================

def test_08_a_same_size_replacement_is_still_caught(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    item = plan.items[0]
    size = os.path.getsize(item.source_path)

    # Replace with a DIFFERENT file of the SAME length, via a real rename: new inode, new mtime.
    replacement = _write(str(world["tmp"] / "swap.bin"), b"Z" * size)
    os.replace(replacement, item.source_path)
    assert os.path.getsize(item.source_path) == size, "the swap kept the length identical"

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "source_identity_changed"
    assert os.listdir(world["library"]) == []


# =============================================================================================
# Case 9 — target collision
# =============================================================================================

def test_09_an_existing_target_is_refused_never_overwritten(world):
    _decide_all(world, cr.SELECTED)
    squatter = _write(os.path.join(world["library"], "clip0001.mp4"), b"EXISTING-FILE")
    before = _sha(squatter)

    plan = _plan(world)
    assert not plan.ok
    assert "target_exists" in _codes(plan)
    assert _sha(squatter) == before, "the existing file must be untouched"
    assert os.path.isfile(world["entries"][0].path), "the source must stay put"


def test_09b_a_target_that_appears_between_plan_and_execute_is_still_refused(world):
    """The atomic rename is what makes this safe, not the plan-time existence check."""
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    assert plan.ok
    squatter = _write(plan.items[0].destination_path, b"APPEARED-LATE")
    before = _sha(squatter)

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "target_exists"
    assert _sha(plan.items[0].destination_path) == before, "no overwrite"
    assert os.path.isfile(plan.items[0].source_path), "the source survives"


# =============================================================================================
# Case 10 — duplicate source basenames
# =============================================================================================

def test_10_two_clips_with_the_same_basename_are_refused_not_suffixed(world, tmp_path):
    nested = tmp_path / "review" / "sub"
    _write(str(nested / "clip0001.mp4"), _payload(9))   # same basename, different folder

    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(tmp_path / "state2"))
    found = cr.discover_clips(str(tmp_path / "review"), True)
    store.begin_scan(OWNER, SCAN)
    session = registry.new_session(OWNER, SCAN)
    token, entries = registry.publish_page(OWNER, session, 1, cr.freeze_page(found.files, 0, 20))
    for entry in entries:
        store.decide(OWNER, cr.decision_identity(entry.path, entry.size, entry.mtime_ns),
                     cr.SELECTED, SCAN)

    plan = tr.plan_transfer(registry, store, session_id=session, page_token=token, owner=OWNER,
                            scan_id=SCAN, input_folder=str(tmp_path / "review"),
                            library_folder=world["library"], rejected_folder=world["rejected"])
    assert not plan.ok
    assert "duplicate_destination" in _codes(plan)
    assert os.listdir(world["library"]) == [], "nothing moved"
    # and nothing was silently renamed to dodge the collision
    assert not any(name.startswith("clip0001 (") for name in os.listdir(world["library"]))


# =============================================================================================
# Case 11 — input/destination overlap
# =============================================================================================

@pytest.mark.parametrize("which", ["library", "rejected"])
def test_11_a_destination_inside_the_input_root_is_refused(world, which):
    _decide_all(world, cr.SELECTED)
    inside = os.path.join(world["review"], "dest")
    os.makedirs(inside, exist_ok=True)
    plan = _plan(world, **{f"{which}_folder": inside})
    assert not plan.ok
    assert "destination_inside_input" in _codes(plan)


def test_11b_the_input_inside_a_destination_is_refused(world, tmp_path):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world, library_folder=str(tmp_path))
    assert not plan.ok
    assert "input_inside_destination" in _codes(plan)


def test_11c_identical_or_nested_destinations_are_refused(world):
    _decide_all(world, cr.SELECTED)
    same = _plan(world, rejected_folder=world["library"])
    assert "destinations_identical" in _codes(same)
    nested_dir = os.path.join(world["library"], "inner")
    os.makedirs(nested_dir, exist_ok=True)
    nested = _plan(world, rejected_folder=nested_dir)
    assert "destinations_nested" in _codes(nested)


def test_11d_a_protected_runtime_destination_is_refused(world, tmp_path):
    _decide_all(world, cr.SELECTED)
    app_root = tmp_path / "app"
    output = app_root / "output"
    os.makedirs(output, exist_ok=True)
    plan = _plan(world, library_folder=str(output), app_root=str(app_root))
    assert not plan.ok
    assert "protected_runtime_destination" in _codes(plan)


# =============================================================================================
# Cases 12 and 13 — reparse points
# =============================================================================================

def _try_junction(link: str, target: str) -> bool:
    import subprocess
    try:
        done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                              capture_output=True, timeout=20)
        return done.returncode == 0 and os.path.exists(link)
    except (OSError, subprocess.SubprocessError):
        return False


def test_12_a_junction_alias_to_the_input_root_cannot_become_a_destination(world, tmp_path):
    """A destination reached through a junction must still be recognised as the input root."""
    _decide_all(world, cr.SELECTED)
    link = str(tmp_path / "alias")
    if not _try_junction(link, world["review"]):
        pytest.skip("creating a directory junction is unavailable in this environment")
    plan = _plan(world, library_folder=link)
    assert not plan.ok
    assert {"library_is_input", "destination_inside_input", "input_inside_destination"} \
        & set(_codes(plan)), _codes(plan)
    assert os.listdir(world["library"]) == []


def test_13_the_reparse_detector_sees_a_real_junction(tmp_path):
    """Measured on a real reparse point, not asserted from a flag."""
    plain = _write(str(tmp_path / "plain.mp4"), b"x" * 32)
    assert tr._is_reparse(plain) is False
    target = str(tmp_path / "target")
    os.makedirs(target, exist_ok=True)
    link = str(tmp_path / "linkdir")
    if not _try_junction(link, target):
        pytest.skip("creating a directory junction is unavailable in this environment")
    assert tr._is_reparse(link) is True


def test_13b_a_source_replaced_by_a_reparse_point_is_refused_and_nothing_moves(world, tmp_path):
    """Whatever the code, the safety property is that the batch refuses and no byte moves."""
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    item = plan.items[0]
    elsewhere = str(tmp_path / "elsewhere")
    os.makedirs(elsewhere, exist_ok=True)

    os.unlink(item.source_path)
    if not _try_junction(item.source_path, elsewhere):
        pytest.skip("replacing the source with a junction is unavailable here")

    replanned = _plan(world)
    assert not replanned.ok, "a source standing on a reparse point must never be transferred"
    assert os.listdir(world["library"]) == [] and os.listdir(world["rejected"]) == []
    assert tr.execute_transfer(replanned, world["journal"]).state == tr.REFUSED


def test_13c_identity_invalidation_fires_before_the_reparse_gate(world, tmp_path):
    """An honest note about gate ordering, pinned so a future reorder is noticed.

    Swapping a reviewed file for a junction changes its size and mtime, so
    ``decision_identity`` no longer matches and the clip reads as PENDING -- a *structural*
    invalidation that precedes the dedicated reparse check. The reparse gate is therefore
    defence-in-depth for a path that is a reparse point while its identity still matches, which
    this environment cannot construct for a plain file. The gate's predicate is measured directly
    in ``test_13``; its reachability through ``plan_transfer`` is NOT claimed.
    """
    _decide_all(world, cr.SELECTED)
    item = _plan(world).items[0]
    elsewhere = str(tmp_path / "elsewhere2")
    os.makedirs(elsewhere, exist_ok=True)
    os.unlink(item.source_path)
    if not _try_junction(item.source_path, elsewhere):
        pytest.skip("replacing the source with a junction is unavailable here")
    codes = _codes(_plan(world))
    assert "pending_decision" in codes, codes
    assert "source_is_reparse_point" not in codes, (
        "if this ever fires first the ordering changed; re-read the gate sequence")


# =============================================================================================
# Case 14 — permission-denied destination
# =============================================================================================

def test_14_an_unwritable_destination_refuses_without_losing_the_source(world, tmp_path):
    _decide_all(world, cr.SELECTED)
    missing = str(tmp_path / "no-such-library")
    plan = _plan(world, library_folder=missing)
    # A non-existent library is allowed by validate_roots (only input must exist), so execution is
    # where it must refuse -- and it must refuse without touching a source.
    if plan.ok:
        result = tr.execute_transfer(plan, world["journal"])
        assert result.state == tr.REFUSED
        assert result.outcomes[0].code in ("destination_not_a_directory",
                                           "destination_access_denied",
                                           "no_replace_not_guaranteed")
    for entry in world["entries"]:
        assert os.path.isfile(entry.path), "no source may be lost to a destination problem"


# =============================================================================================
# Cases 15, 16, 17 — staging and verification failures (cross-volume path)
# =============================================================================================

def _force_cross(plan):
    """Same plan, every item forced down the cross-volume path.

    The sequence under test -- stage, hash-verify, atomically publish, re-verify, remove source --
    is identical whatever the device numbers say. Forcing it exercises the real code against the
    real filesystem; only the device *difference* is simulated, and the genuinely cross-volume arm
    below covers that when a second volume is offered.
    """
    items = tuple(tr.TransferItem(**{**i.__dict__, "operation": tr.OP_CROSS_VOLUME})
                  for i in plan.items)
    return tr.TransferPlan(**{**plan.__dict__, "items": items})


def test_15_the_cross_volume_sequence_completes_on_real_files(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    before = {i.source_path: _sha(i.source_path) for i in plan.items}

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.COMPLETE
    for item in plan.items:
        assert not os.path.exists(item.source_path)
        assert _sha(item.destination_path) == before[item.source_path]
    assert tr.staging_leftovers(world["library"]) == [], "no .part temporary may survive"

    states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[plan.items[0].clip_id]]
    assert states == [tr.INTENT_RECORDED, tr.STAGED, tr.VERIFIED, tr.PUBLISHED,
                      tr.SOURCE_REMOVED, tr.COMPLETE], states


def test_16_a_corrupted_staged_payload_refuses_and_keeps_the_source(world):
    """Verification re-reads from disk, so a payload corrupted after the write is caught."""
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))

    def corrupt(item):
        for leftover in tr.staging_leftovers(item.destination_dir):
            with open(leftover, "r+b") as handle:
                handle.seek(0)
                handle.write(b"XXXX")                   # same length, different bytes

    result = tr.execute_transfer(plan, world["journal"], after_stage=corrupt)
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "staged_copy_mismatch"
    assert os.path.isfile(plan.items[0].source_path), "the original must survive"
    assert not os.path.exists(plan.items[0].destination_path), "nothing was published"
    assert tr.staging_leftovers(world["library"]) == [], "the failed attempt cleaned its own temp"


def test_16b_the_published_object_is_re_read_and_a_wrong_payload_is_caught(world):
    """The publication check must be load-bearing, not decorative.

    ``before_publish`` fires after the staged copy has been verified and before it is renamed into
    place, so overwriting the staging file there publishes a payload nothing has vouched for. Only
    the post-publication re-read can catch that -- and a negative control (M2) proved the matrix
    was blind to it until this case existed.
    """
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))

    def swap_staged_payload(item):
        for leftover in tr.staging_leftovers(item.destination_dir):
            size = os.path.getsize(leftover)
            with open(leftover, "wb") as handle:
                handle.write(b"Q" * size)               # same length, different bytes

    result = tr.execute_transfer(plan, world["journal"], before_publish=swap_staged_payload)
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "published_mismatch", result.outcomes[0]
    assert os.path.isfile(plan.items[0].source_path), "the original must survive"
    states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[plan.items[0].clip_id]]
    assert tr.PUBLISHED not in states, "PUBLISHED must not be recorded for an unverified object"


def test_16c_verify_published_rejects_both_a_wrong_length_and_wrong_bytes(world, tmp_path):
    """Direct unit coverage of the predicate, so the two failure modes are distinguished."""
    _decide_all(world, cr.SELECTED)
    item = _plan(world).items[0]
    good_sha = _sha(item.source_path)

    _write(item.destination_path, b"Z" * (item.source_size + 1))     # wrong length
    with pytest.raises(tr.TransferRefused) as caught:
        tr._verify_published(item, good_sha)
    assert caught.value.code == "published_mismatch"

    _write(item.destination_path, b"Z" * item.source_size)           # right length, wrong bytes
    with pytest.raises(tr.TransferRefused) as caught:
        tr._verify_published(item, good_sha)
    assert caught.value.code == "published_mismatch"

    with open(item.source_path, "rb") as handle:                     # the genuine payload passes
        _write(item.destination_path, handle.read())
    digest, size = tr._verify_published(item, good_sha)
    assert digest == good_sha and size == item.source_size


def test_17_a_destination_that_appears_during_verification_cannot_be_replaced(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))

    def squat(item):
        _write(item.destination_path, b"SQUATTER")

    result = tr.execute_transfer(plan, world["journal"], before_publish=squat)
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "target_exists"
    assert open(plan.items[0].destination_path, "rb").read() == b"SQUATTER", "no overwrite"
    assert os.path.isfile(plan.items[0].source_path), "the source survives"
    assert tr.staging_leftovers(world["library"]) == []


# =============================================================================================
# Cases 18-21 — interruption at each durable boundary
# =============================================================================================

class _Interrupt(Exception):
    pass


def _interrupt(_item):
    raise _Interrupt()


def test_18_interruption_after_durable_intent_leaves_the_source_intact(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_intent=_interrupt)

    assert os.path.isfile(plan.items[0].source_path), "intent is not a move"
    assert not os.path.exists(plan.items[0].destination_path)
    history = world["journal"].states_for(plan.plan_id)[plan.items[0].clip_id]
    assert [e["state"] for e in history] == [tr.INTENT_RECORDED]


def test_19_interruption_after_staging_leaves_the_source_intact(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_stage=_interrupt)

    assert os.path.isfile(plan.items[0].source_path), "a crash before publication keeps the source"
    assert not os.path.exists(plan.items[0].destination_path), "nothing was published"
    states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[plan.items[0].clip_id]]
    assert states == [tr.INTENT_RECORDED, tr.STAGED]
    # The staged temporary is cleaned up by the engine's own finally block.
    assert tr.staging_leftovers(world["library"]) == []


def test_20_interruption_after_publication_leaves_both_copies_and_is_recoverable(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_publish=_interrupt)

    item = plan.items[0]
    assert os.path.isfile(item.destination_path), "the verified copy is published"
    assert os.path.isfile(item.source_path), "the source has not been removed yet"
    assert _sha(item.destination_path) == _sha(item.source_path)
    states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[item.clip_id]]
    assert states == [tr.INTENT_RECORDED, tr.STAGED, tr.VERIFIED, tr.PUBLISHED]

    report = tr.recover(plan, world["journal"])
    finding = {f.clip_id: f for f in report.findings}[item.clip_id]
    assert finding.action == "remove_redundant_source"
    assert report.state == tr.RECOVERY_REQUIRED or not report.needs_operator


def test_21_a_crash_before_source_removal_never_loses_data(world):
    """Both copies exist and hash-match, so no byte is at risk either way."""
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_publish=_interrupt)
    item = plan.items[0]
    assert os.path.isfile(item.source_path) and os.path.isfile(item.destination_path)
    assert _sha(item.source_path) == _sha(item.destination_path)


# =============================================================================================
# Case 22 — idempotent recovery without overwriting
# =============================================================================================

def test_22_recovery_is_idempotent_and_never_overwrites(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.COMPLETE

    published = {i.destination_path: _sha(i.destination_path) for i in plan.items}
    first = tr.recover(plan, world["journal"])
    second = tr.recover(plan, world["journal"])
    assert [f.action for f in first.findings] == ["none"] * 4
    assert [f.action for f in second.findings] == ["none"] * 4
    for path, digest in published.items():
        assert _sha(path) == digest, "recovery must not rewrite a published object"


def test_22b_recovery_escalates_rather_than_guessing_when_a_stranger_holds_the_destination(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_stage=_interrupt)
    # a different file now occupies the destination
    _write(plan.items[0].destination_path, b"NOT-THE-CLIP")

    report = tr.recover(plan, world["journal"])
    assert report.needs_operator
    finding = {f.clip_id: f for f in report.findings}[plan.items[0].clip_id]
    assert finding.action == "operator_required"
    assert open(plan.items[0].destination_path, "rb").read() == b"NOT-THE-CLIP"


def test_22c_recovery_never_replays_a_delete_from_a_historical_status(world):
    """A journal says what was intended; only the filesystem says what is true now."""
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    with pytest.raises(_Interrupt):
        tr.execute_transfer(plan, world["journal"], after_publish=_interrupt)
    item = plan.items[0]
    # The journal records PUBLISHED. Now make the destination wrong and re-run recovery: a naive
    # implementation would trust the status and delete the source.
    _write(item.destination_path, b"WRONG" * 10)
    report = tr.recover(plan, world["journal"])
    assert os.path.isfile(item.source_path), "the source must NOT be deleted on a stale status"
    finding = {f.clip_id: f for f in report.findings}[item.clip_id]
    assert finding.action == "operator_required"


# =============================================================================================
# Cases 23, 24, 30 — batch behaviour
# =============================================================================================

def test_23_a_mixed_success_and_failure_reports_partial(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    _write(plan.items[2].destination_path, b"BLOCKED")  # third item will collide

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.PARTIAL
    assert result.counts["moved"] == 2
    assert result.counts["failed"] == 1
    assert result.counts["remaining"] == 1
    assert sorted(os.listdir(world["library"])) == ["clip0001.mp4", "clip0002.mp4",
                                                    "clip0003.mp4"]


def test_24_a_failure_preserves_every_unprocessed_clip(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    _write(plan.items[1].destination_path, b"BLOCKED")

    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.PARTIAL
    assert os.path.isfile(plan.items[1].source_path), "the failed clip stays put"
    assert os.path.isfile(plan.items[2].source_path), "unprocessed clips stay put"
    assert os.path.isfile(plan.items[3].source_path)
    outcomes = {o.clip_id: o for o in result.outcomes}
    assert outcomes[plan.items[2].clip_id].code == "not_attempted"


def test_30_a_partial_failure_never_advances_the_page(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    _write(plan.items[1].destination_path, b"BLOCKED")
    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.PARTIAL
    assert result.advance_page is False

    clean = _plan(world)                                # a fresh, fully clean page
    if clean.ok:
        assert tr.execute_transfer(clean, world["journal"]).advance_page is True


def test_30b_a_refused_plan_never_advances_the_page(world):
    plan = _plan(world)                                 # everything PENDING
    assert tr.execute_transfer(plan, world["journal"]).advance_page is False


# =============================================================================================
# Case 25 — a second attempt cannot duplicate a completed move
# =============================================================================================

def test_25_replaying_a_completed_plan_cannot_duplicate_the_move(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    assert tr.execute_transfer(plan, world["journal"]).state == tr.COMPLETE
    published = {i.destination_path: _sha(i.destination_path) for i in plan.items}

    again = tr.execute_transfer(plan, world["journal"])  # the very same immutable plan
    assert again.state == tr.REFUSED
    assert again.outcomes[0].code == "source_missing"
    for path, digest in published.items():
        assert _sha(path) == digest, "a replay must not disturb a published object"
    assert len(os.listdir(world["library"])) == 4, "no duplicate appeared"


# =============================================================================================
# Cases 26, 27 — the two rules that do not bend
# =============================================================================================

def test_26_no_source_is_removed_before_a_verified_publication(world):
    """Asserted from the journal order, which is the durable record of what happened."""
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    assert tr.execute_transfer(plan, world["journal"]).state == tr.COMPLETE

    for item in plan.items:
        states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[item.clip_id]]
        assert states.index(tr.VERIFIED) < states.index(tr.PUBLISHED) < states.index(tr.SOURCE_REMOVED)
        assert states.index(tr.INTENT_RECORDED) == 0, "intent precedes everything"


def test_27_rejected_footage_is_quarantined_and_never_deleted(world):
    _decide_all(world, cr.REJECTED)
    before = {e.path: (os.path.getsize(e.path), _sha(e.path)) for e in world["entries"]}
    plan = _plan(world)
    assert tr.execute_transfer(plan, world["journal"]).state == tr.COMPLETE

    assert len(os.listdir(world["rejected"])) == 4
    for item in plan.items:
        size, digest = before[item.source_path]
        assert os.path.getsize(item.destination_path) == size
        assert _sha(item.destination_path) == digest, "every rejected byte is preserved"


def test_27b_the_module_contains_no_unconditional_delete_of_a_destination(world):
    """A structural guard: the only unlink targets are a source or this engine's own temporary."""
    import ast
    source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "beatsync_fork", "clips_review_transfer.py")
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=source_path)
    unlink_args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "unlink":
            unlink_args.append(ast.unparse(node.args[0]) if node.args else "")
    assert unlink_args, "the engine does delete something, so this guard is live"
    for argument in unlink_args:
        assert argument in ("item.source_path", "leftover", "staging_path"), argument
    # os.replace must never be aimed at a destination: it is the REPLACING variant.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "replace" and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "os":
            pytest.fail("os.replace must not be used; it would overwrite a destination")


# =============================================================================================
# Case 28 — source paths never come from a browser request
# =============================================================================================

def test_28_plan_transfer_accepts_no_source_path_parameter():
    """Structural, because this is the property the whole design rests on."""
    import inspect
    signature = inspect.signature(tr.plan_transfer)
    names = set(signature.parameters)
    for forbidden in ("source", "source_path", "sources", "paths", "clip_path", "files"):
        assert forbidden not in names, f"plan_transfer must not accept {forbidden}"
    assert {"session_id", "page_token", "owner", "scan_id"} <= names
    # The three roots ARE parameters, because the operator types them -- but they are validated by
    # validate_roots and the *sources* still come only from the registry.
    assert {"input_folder", "library_folder", "rejected_folder"} <= names


def test_28b_a_forged_path_cannot_enter_a_plan(world, tmp_path):
    """Even naming a real file outside the page cannot get it moved."""
    _decide_all(world, cr.SELECTED)
    victim = _write(str(tmp_path / "victim" / "precious.mp4"), b"PRECIOUS" * 100)
    before = _sha(victim)

    plan = _plan(world)
    assert all(i.source_path != victim for i in plan.items)
    assert all(tr._is_within(i.source_path, plan.input_real) for i in plan.items)
    tr.execute_transfer(plan, world["journal"])
    assert os.path.isfile(victim) and _sha(victim) == before


# =============================================================================================
# Case 29 — nothing strays outside the fixture
# =============================================================================================

def test_29_nothing_outside_the_fixture_is_touched(world, tmp_path):
    _decide_all(world, cr.SELECTED)
    assert tr.execute_transfer(_plan(world), world["journal"]).state == tr.COMPLETE
    assert os.path.isfile(world["outside"])
    assert _sha(world["outside"]) == world["outside_sha"], "the sentinel is untouched"
    # every path the plan ever named lives under tmp_path
    plan = _plan(world)
    for item in plan.items:
        assert tr._is_within(item.source_path, str(tmp_path))
        assert tr._is_within(item.destination_path, str(tmp_path))


def test_29b_the_engine_names_no_production_or_foreign_root():
    """A static guard: no absolute production, archive or other-task path is written into the core."""
    source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "beatsync_fork", "clips_review_transfer.py")
    with open(source_path, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("I:\\\\GITHUB", "I:/GITHUB", "J:\\\\", "J:/", "F:\\\\", "F:/",
                      "PROJECT_FILES", "PROJECT_WORKSPACES", "clips-review"):
        assert forbidden not in text, f"the transfer core must not name {forbidden}"


# =============================================================================================
# The no-replace primitive, measured rather than assumed
# =============================================================================================

def test_the_no_replace_rename_primitive_is_measured_on_the_real_filesystem(tmp_path):
    first = _write(str(tmp_path / "a.bin"), b"A" * 10)
    second = _write(str(tmp_path / "b.bin"), b"B" * 20)
    with pytest.raises(OSError) as caught:
        os.rename(first, second)
    assert isinstance(caught.value, (FileExistsError, OSError))
    assert os.path.getsize(second) == 20, "the target must be untouched"
    assert os.path.isfile(first), "the source must be untouched"
    assert tr.probe_no_replace_rename(str(tmp_path)) is True


def test_the_probe_cleans_up_only_its_own_temporaries(tmp_path):
    bystander = _write(str(tmp_path / "bystander.bin"), b"KEEP")
    assert tr.probe_no_replace_rename(str(tmp_path)) is True
    assert os.path.isfile(bystander), "the probe must not remove anything else"
    assert tr.staging_leftovers(str(tmp_path)) == []


def test_execution_refuses_when_no_replace_cannot_be_proven(world, monkeypatch):
    """NEGATIVE CONTROL: if the primitive cannot be demonstrated, the engine must refuse."""
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    monkeypatch.setattr(tr, "probe_no_replace_rename", lambda directory: False)
    result = tr.execute_transfer(plan, world["journal"])
    assert result.state == tr.REFUSED
    assert result.outcomes[0].code == "no_replace_not_guaranteed"
    for entry in world["entries"]:
        assert os.path.isfile(entry.path), "a weaker contract must never be substituted"


# =============================================================================================
# Journal durability
# =============================================================================================

def test_the_journal_is_append_only_and_records_every_state(world):
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    tr.execute_transfer(plan, world["journal"])

    raw = open(world["journal"].path, encoding="utf-8").read()
    lines = [line for line in raw.splitlines() if line.strip()]
    assert all(json.loads(line) for line in lines), "every line is a whole JSON object"
    assert raw.endswith("\n"), "a complete record always ends terminated"

    seen = {json.loads(line).get("state") for line in lines}
    for required in (tr.PLANNED, tr.INTENT_RECORDED, tr.STAGED, tr.VERIFIED, tr.PUBLISHED,
                     tr.SOURCE_REMOVED, tr.COMPLETE):
        assert required in seen, required

    # appending again only grows the file
    length = len(raw)
    tr.recover(plan, world["journal"])
    assert len(open(world["journal"].path, encoding="utf-8").read()) > length


def test_a_journal_record_never_precedes_the_filesystem_state_it_claims(world):
    """PUBLISHED is only written after the published object has been re-read and hash-matched."""
    _decide_all(world, cr.SELECTED)
    plan = _force_cross(_plan(world))
    observed = []

    def at_publish(item):
        states = [e["state"] for e in world["journal"].states_for(plan.plan_id)[item.clip_id]]
        observed.append((states[-1], os.path.isfile(item.destination_path)))

    tr.execute_transfer(plan, world["journal"], after_publish=at_publish)
    assert observed, "the hook ran"
    for state, exists in observed:
        assert state == tr.PUBLISHED and exists, "the record follows the verified filesystem state"


def test_an_unusable_journal_directory_refuses_before_any_move(world, tmp_path):
    _decide_all(world, cr.SELECTED)
    blocked = _write(str(tmp_path / "blocked"), b"this is a file, not a directory")
    journal = tr.TransferJournal(blocked)
    plan = _plan(world)
    with pytest.raises(tr.TransferRefused) as caught:
        tr.execute_transfer(plan, journal)
    assert caught.value.code == "journal_unavailable"
    for entry in world["entries"]:
        assert os.path.isfile(entry.path), "no clip moves without a durable journal"


def test_a_torn_journal_tail_is_reported_not_invented(world):
    _decide_all(world, cr.SELECTED)
    plan = _plan(world)
    tr.execute_transfer(plan, world["journal"])
    with open(world["journal"].path, "a", encoding="utf-8") as handle:
        handle.write('{"k":"item","plan_id":"x","state":"COMP')      # torn
    records = world["journal"].records()
    assert records[-1]["k"] == "unparseable", "a torn tail is surfaced, never guessed at"


# =============================================================================================
# The genuinely cross-volume arm — opt-in
# =============================================================================================

@pytest.mark.skipif(not _crossvol_root(),
                    reason="set BEATSYNC_TEST_CROSSVOL to a directory on another volume")
def test_a_real_cross_volume_move_completes_and_removes_the_source(world):
    root = _crossvol_root()
    library = os.path.join(root, "r5p45-library")
    os.makedirs(library, exist_ok=True)
    try:
        _decide_all(world, cr.SELECTED)
        plan = _plan(world, library_folder=library)
        assert plan.ok, _codes(plan)
        assert {i.operation for i in plan.items} == {tr.OP_CROSS_VOLUME}, (
            "the devices must genuinely differ for this arm to mean anything")
        before = {i.source_path: _sha(i.source_path) for i in plan.items}

        result = tr.execute_transfer(plan, world["journal"])
        assert result.state == tr.COMPLETE
        for item in plan.items:
            assert not os.path.exists(item.source_path)
            assert _sha(item.destination_path) == before[item.source_path]
        assert tr.staging_leftovers(library) == []
    finally:
        for name in os.listdir(library):               # only this test's own files
            try:
                os.unlink(os.path.join(library, name))
            except OSError:
                pass
        try:
            os.rmdir(library)
        except OSError:
            pass


@pytest.mark.skipif(not _crossvol_root(),
                    reason="set BEATSYNC_TEST_CROSSVOL to a directory on another volume")
def test_a_real_cross_volume_crash_after_publication_keeps_both_copies(world):
    root = _crossvol_root()
    library = os.path.join(root, "r5p45-library-crash")
    os.makedirs(library, exist_ok=True)
    try:
        _decide_all(world, cr.SELECTED)
        plan = _plan(world, library_folder=library)
        assert {i.operation for i in plan.items} == {tr.OP_CROSS_VOLUME}
        with pytest.raises(_Interrupt):
            tr.execute_transfer(plan, world["journal"], after_publish=_interrupt)
        item = plan.items[0]
        assert os.path.isfile(item.source_path) and os.path.isfile(item.destination_path)
        assert _sha(item.source_path) == _sha(item.destination_path)
        report = tr.recover(plan, world["journal"])
        finding = {f.clip_id: f for f in report.findings}[item.clip_id]
        assert finding.action == "remove_redundant_source"
    finally:
        for name in os.listdir(library):
            try:
                os.unlink(os.path.join(library, name))
            except OSError:
                pass
        try:
            os.rmdir(library)
        except OSError:
            pass


# =============================================================================================
# Isolation from P1 — the transfer core must not be reachable from the GUI or the HTTP layer
# =============================================================================================

def test_the_transfer_core_is_not_wired_into_the_gui():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gui = open(os.path.join(root, "src", "gui.py"), encoding="utf-8").read()
    assert "clips_review_transfer" not in gui, (
        "P2 is an isolated core; wiring it into the GUI needs its own authorization")
    assert "execute_transfer" not in gui
    assert "plan_transfer" not in gui


def test_the_media_server_still_contains_no_mutating_filesystem_call():
    """P1's guarantee must survive P2 existing beside it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, "src", "clips_media_server.py"), encoding="utf-8").read()
    for forbidden in ("os.rename", "os.replace", "os.remove", "os.unlink", "shutil.move",
                      "shutil.copy", "shutil.rmtree", "os.rmdir", "os.makedirs", "O_WRONLY",
                      "O_CREAT", "MoveFileEx", "clips_review_transfer"):
        assert forbidden not in source, f"the media layer can {forbidden}"


def test_confirm_still_moves_nothing(world):
    """``_on_clips_confirm`` reports readiness. Nothing in P2 changes that."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gui = open(os.path.join(root, "src", "gui.py"), encoding="utf-8").read()
    start = gui.index("def _on_clips_confirm(")
    body = gui[start:start + 4000]
    for forbidden in ("os.rename", "os.replace", "os.unlink", "os.remove", "shutil",
                      "execute_transfer", "plan_transfer"):
        assert forbidden not in body, f"_on_clips_confirm must not {forbidden}"


def test_the_transfer_core_imports_only_stdlib_and_the_fork_package():
    import ast
    allowed = {"__future__", "copy", "hashlib", "json", "os", "random", "re", "stat", "statistics",
               "subprocess", "tempfile", "threading", "time", "collections", "concurrent",
               "dataclasses", "enum", "secrets", "html", "math", "types", "typing",
               "beatsync_fork"}
    source_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "beatsync_fork", "clips_review_transfer.py")
    with open(source_path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=source_path)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= allowed, sorted(imported - allowed)


def test_all_ten_journal_states_exist_and_are_distinct():
    assert len(tr.STATES) == 10
    assert len(set(tr.STATES)) == 10
    for name in ("PLANNED", "INTENT_RECORDED", "STAGED", "VERIFIED", "PUBLISHED",
                 "SOURCE_REMOVED", "COMPLETE", "REFUSED", "PARTIAL", "RECOVERY_REQUIRED"):
        assert getattr(tr, name) in tr.STATES
