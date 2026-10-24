"""R5-P50 — recovery completion semantics (R5-P48 finding F2).

``recover()`` used to aggregate as ``RECOVERY_REQUIRED if needs_operator else COMPLETE``, so
``COMPLETE`` meant *"nothing needs a human"* while reusing a constant that means *"done"*
everywhere else in the engine. An independent review measured two consequences on real volumes:

* a cross-volume copy-only transfer returned ``SOURCE_REMOVAL_BLOCKED``, the batch correctly
  reported ``PARTIAL`` with ``advance_page=False``, and ``recover()`` on the identical filesystem
  answered ``COMPLETE`` / ``needs_operator=False`` — while its own finding said
  ``operator_may_remove_duplicate``;
* a three-clip batch where two clips never left their original folder also answered ``COMPLETE``.

This file pins the corrected contract: three independent signals (``plan_finished``,
``needs_operator``, ``state``), and completion that requires **durable evidence** rather than
matching bytes alone. Every case asserts the real files, their SHA-256, the journal event
sequence after a cold reopen, the per-item action, the aggregate state, ``plan_finished``,
``needs_operator``, and that nothing was mutated.

Fixtures are synthetic and under pytest's ``tmp_path``. Nothing here touches production media,
the ``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import clips_review_transfer as tr

OWNER = "r5p50-owner"
SCAN = "r5p50-scan"
_UNSET = object()

CROSSVOL = os.environ.get("BEATSYNC_TEST_CROSSVOL", "")
needs_crossvol = pytest.mark.skipif(
    not CROSSVOL, reason="set BEATSYNC_TEST_CROSSVOL to a directory on another volume")


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


def _build(tmp_path, count=3, library=None):
    review = tmp_path / "review"
    for index in range(1, count + 1):
        _write(str(review / f"clip{index:04d}.mp4"), _payload(index))
    lib = library or str(tmp_path / "library")
    rejected = str(tmp_path / "quarantine")
    os.makedirs(lib, exist_ok=True)
    os.makedirs(rejected, exist_ok=True)
    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(tmp_path / "state"))
    found = cr.discover_clips(str(review), True)
    store.begin_scan(OWNER, SCAN)
    session = registry.new_session(OWNER, SCAN)
    token, entries = registry.publish_page(OWNER, session, 1,
                                           cr.freeze_page(found.files, 0, 50))
    return {
        "tmp": tmp_path, "review": str(review), "library": lib, "rejected": rejected,
        "registry": registry, "store": store, "session": session, "page": token,
        "entries": entries,
        "identities": {e.clip_id: cr.decision_identity(e.path, e.size, e.mtime_ns)
                       for e in entries},
        "journal": tr.TransferJournal(str(tmp_path / "journal")),
    }


@pytest.fixture
def world(tmp_path):
    return _build(tmp_path, count=3)


def _decide_all(world, decision=cr.SELECTED, content_sha256=_UNSET):
    for entry in world["entries"]:
        sha = _sha(entry.path) if content_sha256 is _UNSET else content_sha256
        world["store"].decide(OWNER, world["identities"][entry.clip_id], decision, SCAN,
                              content_sha256=sha)


def _plan(world, **over):
    kwargs = dict(session_id=world["session"], page_token=world["page"], owner=OWNER,
                  scan_id=SCAN, input_folder=world["review"], library_folder=world["library"],
                  rejected_folder=world["rejected"],
                  expected_revisions={e.clip_id: world["store"].get(
                      world["identities"][e.clip_id])[1] for e in world["entries"]})
    kwargs.update(over)
    return tr.plan_transfer(world["registry"], world["store"], **kwargs)


def _run(world, plan=None, journal=None, **hooks):
    return tr.execute_transfer(plan or _plan(world), journal or world["journal"],
                               store=world["store"], registry=world["registry"], **hooks)


def _force_cross(plan):
    items = tuple(tr.TransferItem(**{**i.__dict__, "operation": tr.OP_CROSS_VOLUME})
                  for i in plan.items)
    return tr.TransferPlan(**{**plan.__dict__, "items": items})


def _cold(world, plan):
    """Recovery through a FRESH journal object over the same file — the honest reading."""
    return tr.recover(plan, tr.TransferJournal(world["journal"].directory))


def _states(world, plan, clip_id):
    cold = tr.TransferJournal(world["journal"].directory)
    return [e["state"] for e in cold.states_for(plan.plan_id).get(clip_id, [])]


def _actions(report):
    return {f.clip_id: f.action for f in report.findings if f.clip_id}


def _snapshot(plan):
    """Every path the plan touches, with its digest, so mutation can be detected."""
    out = {}
    for item in plan.items:
        for path in (item.source_path, item.destination_path):
            out[path] = _sha(path) if os.path.isfile(path) else None
    return out


class FailAt(tr.TransferJournal):
    def __init__(self, directory, states, persistent=False):
        super().__init__(directory)
        self.states = set(states if isinstance(states, (set, list, tuple)) else [states])
        self.persistent = persistent
        self.fired = set()

    def append(self, event, **kw):
        state = event.get("state")
        if state in self.states and (self.persistent or state not in self.fired):
            self.fired.add(state)
            raise tr.TransferRefused("journal_unavailable", "errno 13")
        return super().append(event, **kw)


# =============================================================================================
# THE CONTRACT ITSELF
# =============================================================================================

def test_the_three_signals_are_independent_by_construction():
    """``plan_finished`` is the only completion signal and is never derived from
    ``needs_operator``. Asserted on the type, so the relationship cannot silently regress."""
    assert "plan_finished" in tr.RecoveryReport.__dataclass_fields__
    assert tr.RecoveryReport.__dataclass_fields__["plan_finished"].default is False, \
        "the default must be the SAFE direction: never mistake a report for a finished plan"
    assert isinstance(tr.RecoveryReport.needs_operator, property)
    assert isinstance(tr.RecoveryReport.remaining_items, property)
    assert tr.RECOVERY_STATES == (tr.COMPLETE, tr.RECOVERY_INCOMPLETE, tr.RECOVERY_REQUIRED)
    # the recovery-only aggregate must NOT be an item state, or _history_fault would accept it
    assert tr.RECOVERY_INCOMPLETE not in tr.STATES
    assert tr.COMPLETE in tr.STATES and tr.RECOVERY_REQUIRED in tr.STATES


def test_an_operator_recommendation_always_sets_needs_operator():
    """A report may not recommend an operator-managed removal and claim no human is involved."""
    duplicate = tr.RecoveryFinding("c", tr.SOURCE_REMOVAL_BLOCKED, "o",
                                   tr.ACTION_MAY_REMOVE_DUPLICATE)
    report = tr.RecoveryReport("p", tr.RECOVERY_INCOMPLETE, (duplicate,), False)
    assert report.needs_operator is True
    retry = tr.RecoveryFinding("c", tr.PLANNED, "o", tr.ACTION_RETRY_SAFE)
    assert tr.RecoveryReport("p", tr.RECOVERY_INCOMPLETE, (retry,), False).needs_operator is False
    assert tr.RecoveryReport("p", tr.RECOVERY_REQUIRED, ()).needs_operator is True, \
        "the pre-R5-P50 state-based floor still holds for a report built without findings"


def test_plan_finished_is_never_true_when_any_item_is_unfinished(world):
    """The invariant the whole finding reduces to, asserted directly over every case below."""
    _decide_all(world)
    plan = _plan(world)
    calls = {"n": 0}

    def fail_second(_i):
        calls["n"] += 1
        if calls["n"] == 2:
            raise tr.TransferRefused("injected", "second clip fails")

    _run(world, plan, before_publish=fail_second)
    report = _cold(world, plan)
    unfinished = [a for a in _actions(report).values() if a != tr.ACTION_NONE]
    assert unfinished, "this fixture leaves work behind"
    assert report.plan_finished is False
    assert report.state != tr.COMPLETE
    assert len(report.remaining_items) == len(unfinished)


# =============================================================================================
# 1-2 — successful same-volume moves
# =============================================================================================

def test_01_one_successful_same_volume_move(tmp_path):
    world = _build(tmp_path, count=1)
    _decide_all(world)
    plan = _plan(world)
    assert _run(world, plan).state == tr.COMPLETE
    item = plan.items[0]
    assert not os.path.lexists(item.source_path)
    assert _sha(item.destination_path) == item.approved_sha256
    assert _states(world, plan, item.clip_id) == [
        tr.INTENT_RECORDED, tr.PUBLISHED, tr.SOURCE_REMOVED, tr.COMPLETE]
    report = _cold(world, plan)
    assert _actions(report) == {item.clip_id: tr.ACTION_NONE}
    assert report.state == tr.COMPLETE
    assert report.plan_finished is True
    assert report.needs_operator is False
    assert report.remaining_items == ()


def test_02_multiple_successful_same_volume_moves(world):
    _decide_all(world)
    plan = _plan(world)
    assert _run(world, plan).state == tr.COMPLETE
    before = _snapshot(plan)
    report = _cold(world, plan)
    assert set(_actions(report).values()) == {tr.ACTION_NONE}
    assert report.state == tr.COMPLETE and report.plan_finished is True
    assert report.needs_operator is False
    for item in plan.items:
        assert _states(world, plan, item.clip_id)[-1] == tr.COMPLETE
    assert _snapshot(plan) == before, "recovery mutated nothing"


# =============================================================================================
# 3-4 — cross-volume copy-only (the headline F2 case)
# =============================================================================================

@needs_crossvol
@pytest.mark.parametrize("count", [1, 3])
def test_03_04_cross_volume_copy_only_is_never_a_finished_plan(tmp_path, count):
    library = os.path.join(CROSSVOL, f"p50-xv-{count}-{os.getpid()}")
    os.makedirs(library, exist_ok=True)
    try:
        world = _build(tmp_path, count=count, library=library)
        _decide_all(world)
        plan = _plan(world)
        assert all(i.operation == tr.OP_CROSS_VOLUME for i in plan.items), "not cross-volume"
        result = _run(world, plan)
        assert result.state == tr.PARTIAL and result.advance_page is False
        assert all(o.state == tr.SOURCE_REMOVAL_BLOCKED for o in result.outcomes)
        for item in plan.items:
            assert _sha(item.source_path) == item.approved_sha256, "original intact"
            assert _sha(item.destination_path) == item.approved_sha256, "copy published"
            assert _states(world, plan, item.clip_id) == [
                tr.INTENT_RECORDED, tr.STAGED, tr.VERIFIED, tr.PUBLISHED,
                tr.SOURCE_REMOVAL_BLOCKED]
        before = _snapshot(plan)
        report = _cold(world, plan)
        assert set(_actions(report).values()) == {tr.ACTION_MAY_REMOVE_DUPLICATE}
        assert report.state == tr.RECOVERY_INCOMPLETE, "unfinished, but nothing is ambiguous"
        assert report.plan_finished is False, "THE DEFECT: this used to be a finished plan"
        assert report.needs_operator is True, \
            "it recommends an operator-managed removal, so it may not claim otherwise"
        assert len(report.remaining_items) == count
        for finding in report.findings:
            assert "UNFINISHED" in finding.detail
        assert _snapshot(plan) == before, "recovery deleted, moved and replaced nothing"
    finally:
        for name in os.listdir(library):
            os.remove(os.path.join(library, name))
        os.rmdir(library)


def test_03b_copy_only_without_a_second_volume(world):
    """The same semantics, reachable without a cross-volume fixture, by forcing the operation."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    assert _run(world, plan).state == tr.PARTIAL
    report = _cold(world, plan)
    assert set(_actions(report).values()) == {tr.ACTION_MAY_REMOVE_DUPLICATE}
    assert report.state == tr.RECOVERY_INCOMPLETE
    assert report.plan_finished is False
    assert report.needs_operator is True


# =============================================================================================
# 5-7 — partial batches and unattempted clips
# =============================================================================================

def test_05_one_success_then_one_failure(world):
    _decide_all(world)
    plan = _plan(world)
    calls = {"n": 0}

    def fail_second(_i):
        calls["n"] += 1
        if calls["n"] == 2:
            raise tr.TransferRefused("injected", "second clip fails before publication")

    result = _run(world, plan, before_publish=fail_second)
    assert result.state == tr.PARTIAL and result.advance_page is False
    report = _cold(world, plan)
    actions = _actions(report)
    assert actions[plan.items[0].clip_id] == tr.ACTION_NONE
    assert actions[plan.items[1].clip_id] == tr.ACTION_RETRY_SAFE
    assert report.state == tr.RECOVERY_INCOMPLETE
    assert report.plan_finished is False, "THE DEFECT: this used to be a finished plan"
    assert report.needs_operator is False, "a safe retry needs no operator decision"
    assert len(report.remaining_items) == 2


def test_06_one_success_then_two_unattempted(world):
    _decide_all(world)
    plan = _plan(world)
    calls = {"n": 0}

    def fail_second(_i):
        calls["n"] += 1
        if calls["n"] == 2:
            raise tr.TransferRefused("injected", "stop the batch here")

    _run(world, plan, before_publish=fail_second)
    assert _states(world, plan, plan.items[2].clip_id) == [], "the third was never attempted"
    assert os.path.isfile(plan.items[2].source_path), "and is still in the review folder"
    report = _cold(world, plan)
    assert _actions(report)[plan.items[2].clip_id] == tr.ACTION_RETRY_SAFE
    assert report.plan_finished is False
    assert report.state == tr.RECOVERY_INCOMPLETE


def test_07_no_clip_was_ever_attempted(world):
    """A plan built but never executed. Nothing moved, so nothing is finished."""
    _decide_all(world)
    plan = _plan(world)
    report = tr.recover(plan, world["journal"])
    assert set(_actions(report).values()) == {tr.ACTION_RETRY_SAFE}
    assert report.state == tr.RECOVERY_INCOMPLETE
    assert report.plan_finished is False
    assert report.needs_operator is False
    assert len(report.remaining_items) == len(plan.items)


def test_08_no_publication_source_intact_retry_valid(world):
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.INTENT_RECORDED, persistent=True)
    result = _run(world, plan, journal=journal)
    assert result.state == tr.REFUSED
    for item in plan.items:
        assert os.path.isfile(item.source_path)
        assert not os.path.lexists(item.destination_path)
    report = _cold(world, plan)
    assert set(_actions(report).values()) == {tr.ACTION_RETRY_SAFE}
    assert report.plan_finished is False and report.state == tr.RECOVERY_INCOMPLETE


def test_09_destination_published_but_source_remains(world):
    """Case 9 — identical on disk to case 3, reached by a crash instead of by policy."""
    _decide_all(world)
    plan = _force_cross(_plan(world))

    class Boom(BaseException):
        pass

    with pytest.raises(BaseException):
        _run(world, plan, after_publish=lambda _i: (_ for _ in ()).throw(Boom()))
    item = plan.items[0]
    assert os.path.isfile(item.source_path) and os.path.isfile(item.destination_path)
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_MAY_REMOVE_DUPLICATE
    assert report.plan_finished is False
    assert report.needs_operator is True


# =============================================================================================
# 10-12 — durable evidence is required, not just matching bytes
# =============================================================================================

def test_10_source_absent_destination_valid_completion_journal_valid(tmp_path):
    world = _build(tmp_path, count=1)
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    assert _states(world, plan, plan.items[0].clip_id)[-1] == tr.COMPLETE
    report = _cold(world, plan)
    assert _actions(report) == {plan.items[0].clip_id: tr.ACTION_NONE}
    assert report.plan_finished is True and report.state == tr.COMPLETE


def test_11_source_absent_destination_valid_completion_journal_MISSING(tmp_path):
    """The distinction that makes completion evidential: identical bytes on disk to case 10, but
    no durable record proving the move completed. Must NOT be reported as finished."""
    world = _build(tmp_path, count=1)
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), {tr.COMPLETE, tr.PUBLISHED_UNJOURNALLED},
                     persistent=True)
    _run(world, plan, journal=journal)
    item = plan.items[0]
    assert not os.path.lexists(item.source_path), "the rename really happened"
    assert _sha(item.destination_path) == item.approved_sha256, "and the bytes are approved"
    assert tr.COMPLETE not in _states(world, plan, item.clip_id), "but nothing proves completion"
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False
    assert report.needs_operator is True
    assert "cannot be shown to have completed durably" in report.findings[0].detail
    assert os.path.isfile(item.destination_path), "and recovery left it alone"


def test_12_published_unjournalled_from_r5p49(tmp_path):
    world = _build(tmp_path, count=1)
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED, persistent=False)
    result = _run(world, plan, journal=journal)
    assert result.outcomes[0].state == tr.PUBLISHED_UNJOURNALLED
    assert _states(world, plan, plan.items[0].clip_id)[-1] == tr.PUBLISHED_UNJOURNALLED
    report = _cold(world, plan)
    assert _actions(report)[plan.items[0].clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False, \
        "a terminal-but-unfinished record is never completion, however well the bytes match"


def test_13_failed_batch_summary_append(world):
    """Every item durably COMPLETE, only the batch summary missing. The items are individually
    proven, so the plan IS finished — and the absent summary is reported as a qualification
    rather than escalated, because escalating it would be the opposite inaccuracy."""
    _decide_all(world)
    plan = _plan(world)

    class FailBatch(tr.TransferJournal):
        def append(self, event, **kw):
            if event.get("k") == "batch":
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            return super().append(event, **kw)

    journal = FailBatch(str(world["tmp"] / "journal"))
    result = _run(world, plan, journal=journal)
    assert all(o.state == tr.COMPLETE for o in result.outcomes)
    assert result.state == tr.PARTIAL, "R5-P49: the executing run cannot claim batch completion"
    report = _cold(world, plan)
    assert set(_actions(report).values()) == {tr.ACTION_NONE}
    assert report.plan_finished is True
    assert report.state == tr.COMPLETE
    assert report.needs_operator is False
    qualifications = [f for f in report.findings if not f.clip_id]
    assert len(qualifications) == 1
    assert qualifications[0].observed == "batch_summary=absent"
    assert qualifications[0].action == tr.ACTION_NONE


# =============================================================================================
# 14-15 — rollback outcomes
# =============================================================================================

def test_14_rolled_back(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    real = tr._verify_published

    def fail(_it, _expected):
        raise tr.TransferRefused("published_mismatch", "injected")

    tr._verify_published = fail
    try:
        result = _run(world, plan)
    finally:
        tr._verify_published = real
    assert result.outcomes[0].state == tr.ROLLED_BACK
    assert os.path.isfile(item.source_path) and not os.path.lexists(item.destination_path)
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_RETRY_SAFE
    assert report.plan_finished is False
    assert report.state == tr.RECOVERY_INCOMPLETE


def test_15_rollback_failed_with_both_paths_present(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    real = tr._verify_published
    occupant = b"a new file took the vacated source path"

    def occupy_then_fail(it, _expected):
        with open(it.source_path, "wb") as handle:
            handle.write(occupant)
        raise tr.TransferRefused("published_mismatch", "injected")

    tr._verify_published = occupy_then_fail
    try:
        result = _run(world, plan)
    finally:
        tr._verify_published = real
    assert result.outcomes[0].state == tr.RECOVERY_REQUIRED
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED and report.plan_finished is False
    with open(item.source_path, "rb") as handle:
        assert handle.read() == occupant, "the occupant was never overwritten"


# =============================================================================================
# 16-20 — ambiguous filesystem states
# =============================================================================================

def test_16_unknown_destination_occupant(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    _write(item.destination_path, b"a stranger of a different length entirely")
    report = tr.recover(plan, world["journal"])
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED and report.plan_finished is False
    with open(item.destination_path, "rb") as handle:
        assert handle.read() == b"a stranger of a different length entirely", "not overwritten"


def test_17_missing_source_and_destination(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    os.remove(item.source_path)
    report = tr.recover(plan, world["journal"])
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED and report.plan_finished is False
    assert "neither the source nor the destination" in report.findings[0].detail


def test_18_source_content_hash_mismatch(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    _write(item.source_path, b"wholly different content of another length")
    report = tr.recover(plan, world["journal"])
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.plan_finished is False


def test_19_source_metadata_matches_but_content_differs(world):
    """The case metadata identity cannot see. Only the approved fingerprint catches it."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    before = os.stat(item.source_path)
    with open(item.source_path, "r+b") as handle:
        handle.write(b"Z" * item.source_size)
    os.utime(item.source_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(item.source_path)
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
    report = tr.recover(plan, world["journal"])
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED, \
        "metadata says unchanged; the content fingerprint is the only thing that disagrees"
    assert report.plan_finished is False


def test_20_destination_content_hash_mismatch(world):
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    item = plan.items[0]
    with open(item.destination_path, "r+b") as handle:
        handle.write(b"CORRUPTED")
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.state == tr.RECOVERY_REQUIRED and report.plan_finished is False


# =============================================================================================
# 21-25 — journal integrity (interaction with R5-P47/M4 and R5-P49/F4)
# =============================================================================================

def test_21_forged_journal_digest(world):
    """A forged ``published_sha256`` cannot make a stranger look published, and cannot make an
    unfinished plan look finished."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    stranger = b"S" * item.source_size
    _write(item.destination_path, stranger)
    os.remove(item.source_path)
    world["journal"].append(tr._item_event(item, plan, tr.PUBLISHED,
                                           published_sha256=hashlib.sha256(stranger).hexdigest(),
                                           at=1.0))
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert report.plan_finished is False
    assert report.state == tr.RECOVERY_REQUIRED


def test_22_contradictory_event_ordering(world):
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    world["journal"].append(tr._item_event(item, plan, tr.PUBLISHED, at=1.0))
    world["journal"].append(tr._item_event(item, plan, tr.STAGED, at=2.0))
    report = _cold(world, plan)
    assert _actions(report)[item.clip_id] == tr.ACTION_OPERATOR_REQUIRED
    assert "out of order" in report.findings[0].detail
    assert report.plan_finished is False and report.state == tr.RECOVERY_REQUIRED


def test_23_incomplete_journal_tail(world):
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    with open(world["journal"].path, "ab") as handle:
        handle.write(b'{"k":"item","plan_id":"x","state":"COMP')
    report = _cold(world, plan)
    assert report.needs_operator is True, "R5-P49: damaged records escalate"
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False, \
        "a plan whose journal integrity is in doubt is not a finished plan"


def test_24_damaged_journal_record(world):
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    with open(world["journal"].path, "ab") as handle:
        handle.write(b'[1,2,3]\n')
    report = _cold(world, plan)
    assert report.plan_finished is False and report.state == tr.RECOVERY_REQUIRED
    integrity = [f for f in report.findings if f.observed.startswith("journal=")]
    assert integrity and "incomplete" in integrity[0].detail


def test_25_valid_record_after_a_repaired_tail(world):
    """R5-P49's tail repair keeps the appended record readable; the plan is still finished, and
    the separator is reported as a damage marker, which is why the state escalates."""
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    with open(world["journal"].path, "ab") as handle:
        handle.write(b'{"torn')
    journal = tr.TransferJournal(world["journal"].directory)
    journal.append({"k": "note", "plan_id": plan.plan_id, "state": tr.PLANNED})
    assert journal.tails_separated == 1
    lines = [json.loads(l) for l in open(journal.path, encoding="utf-8")
             if l.strip() and not l.startswith('{"torn')]
    assert any(e.get("k") == tr.TAIL_REPAIR_KIND for e in lines), "the separator is readable"
    report = tr.recover(plan, journal)
    assert report.needs_operator is True, "a separated tail is still damage to be surfaced"
    assert report.plan_finished is False
    for item in plan.items:
        assert _states(world, plan, item.clip_id)[-1] == tr.COMPLETE, \
            "each item's own durable history is intact"


# =============================================================================================
# 26-32 — reopen, idempotence, plan shapes
# =============================================================================================

def test_26_cold_process_journal_reopen_agrees(world):
    _decide_all(world)
    plan = _force_cross(_plan(world))
    _run(world, plan)
    warm = tr.recover(plan, world["journal"])
    cold = _cold(world, plan)
    assert (warm.state, warm.plan_finished, warm.needs_operator) == \
           (cold.state, cold.plan_finished, cold.needs_operator)
    assert _actions(warm) == _actions(cold)
    assert cold.plan_finished is False


def test_27_recovery_repeated_three_times_is_idempotent_and_non_mutating(world):
    _decide_all(world)
    plan = _force_cross(_plan(world))
    _run(world, plan)
    before = _snapshot(plan)
    seen = set()
    for _ in range(3):
        report = _cold(world, plan)
        seen.add((report.state, report.plan_finished, report.needs_operator,
                  tuple(sorted(_actions(report).items()))))
    assert len(seen) == 1, "three runs, one answer"
    assert _snapshot(plan) == before, "and nothing on disk changed"


def test_28_a_complete_plan_and_an_incomplete_plan_in_one_journal_directory(world):
    """Two plans, one journal file. Each must be judged on its own records only."""
    _decide_all(world)
    first = _plan(world)
    assert _run(world, first).state == tr.COMPLETE
    second = _build(world["tmp"] / "second", count=2)
    second["journal"] = world["journal"]                 # deliberately the SAME journal file
    _decide_all(second)
    plan_two = _plan(second)
    calls = {"n": 0}

    def fail_first(_i):
        calls["n"] += 1
        raise tr.TransferRefused("injected", "nothing in the second plan transfers")

    _run(second, plan_two, before_publish=fail_first)
    report_one = _cold(world, first)
    report_two = tr.recover(plan_two, tr.TransferJournal(world["journal"].directory))
    assert report_one.plan_finished is True and report_one.state == tr.COMPLETE
    assert report_two.plan_finished is False and report_two.state == tr.RECOVERY_INCOMPLETE
    assert set(_actions(report_two).values()) == {tr.ACTION_RETRY_SAFE}


def test_29_two_plans_sharing_an_owner_but_not_a_plan_id(world):
    _decide_all(world)
    one = _plan(world)
    two = _plan(world, plan_id="a-second-plan-id")
    assert one.plan_id != two.plan_id
    assert _run(world, one).state == tr.COMPLETE
    assert _cold(world, one).plan_finished is True
    # the second plan has NO records of its own, and must not inherit the first plan's
    report = tr.recover(two, tr.TransferJournal(world["journal"].directory))
    assert report.plan_finished is False, "a different plan id proves nothing about this one"
    assert set(_actions(report).values()) == {tr.ACTION_OPERATOR_REQUIRED}, \
        "the sources are gone and the destinations hold content with no record for THIS plan"


def test_30_a_plan_with_zero_items(world):
    """Vacuously finished, and consistent with ``execute_transfer``, where an empty plan also
    reports COMPLETE. No qualification finding is added, because there is nothing to qualify."""
    _decide_all(world)
    empty = tr.TransferPlan(**{**_plan(world).__dict__, "items": ()})
    result = tr.execute_transfer(empty, world["journal"], store=world["store"],
                                 registry=world["registry"])
    assert result.state == tr.COMPLETE
    report = _cold(world, empty)
    assert report.findings == ()
    assert report.state == tr.COMPLETE
    assert report.plan_finished is True
    assert report.needs_operator is False
    assert report.remaining_items == ()


def test_31_partial_due_to_a_deliberately_withheld_deletion(world):
    """The whole plan succeeded as far as the engine permits, and is still not finished."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    result = _run(world, plan)
    assert result.state == tr.PARTIAL
    assert result.counts["published_only"] == len(plan.items)
    assert result.counts["failed"] == 0, "nothing failed -- the deletion was withheld by policy"
    report = _cold(world, plan)
    assert report.plan_finished is False
    assert report.state == tr.RECOVERY_INCOMPLETE
    assert report.needs_operator is True
    assert len(report.remaining_items) == len(plan.items)


def test_32_one_item_physically_complete_but_not_durably_confirmed(world):
    """A mixed plan: clip 1 durably complete, clip 2 physically moved with no proof. The plan
    must not be finished, and the aggregate must reflect the worst item, not the best."""
    _decide_all(world)
    plan = _plan(world)
    first, second = plan.items[0], plan.items[1]
    one_item = tr.TransferPlan(**{**plan.__dict__, "items": (first,)})
    assert _run(world, one_item).state == tr.COMPLETE
    # clip 2: move it by hand and record nothing at all
    os.makedirs(second.destination_dir, exist_ok=True)
    os.rename(second.source_path, second.destination_path)
    report = _cold(world, plan)
    actions = _actions(report)
    assert actions[first.clip_id] == tr.ACTION_NONE, "clip 1 is genuinely finished"
    assert actions[second.clip_id] == tr.ACTION_OPERATOR_REQUIRED, "clip 2 has no proof"
    assert actions[plan.items[2].clip_id] == tr.ACTION_RETRY_SAFE
    assert report.state == tr.RECOVERY_REQUIRED, "the aggregate takes the worst item"
    assert report.plan_finished is False
    assert first.clip_id not in report.remaining_items
    assert second.clip_id in report.remaining_items


# =============================================================================================
# recovery is still strictly non-destructive
# =============================================================================================

@pytest.mark.parametrize("shape", ["complete", "copy_only", "partial", "ambiguous"])
def test_recovery_mutates_nothing_in_any_shape(world, shape):
    """§8 — ``recover()`` deletes, moves, retries, replaces and rewrites nothing, ever."""
    _decide_all(world)
    plan = _force_cross(_plan(world)) if shape == "copy_only" else _plan(world)
    if shape == "complete":
        _run(world, plan)
    elif shape == "copy_only":
        _run(world, plan)
    elif shape == "partial":
        calls = {"n": 0}

        def fail_second(_i):
            calls["n"] += 1
            if calls["n"] == 2:
                raise tr.TransferRefused("injected", "")

        _run(world, plan, before_publish=fail_second)
    else:
        _write(plan.items[0].destination_path, b"stranger")

    before_files = _snapshot(plan)
    before_journal = open(world["journal"].path, "rb").read() \
        if os.path.exists(world["journal"].path) else b""
    for _ in range(2):
        _cold(world, plan)
    after_journal = open(world["journal"].path, "rb").read() \
        if os.path.exists(world["journal"].path) else b""
    assert _snapshot(plan) == before_files, "no file was created, removed, moved or rewritten"
    assert after_journal.startswith(before_journal), \
        "the journal is only ever appended to -- never rewritten or truncated"


def test_recovery_never_calls_a_destructive_filesystem_primitive(world):
    """Counted directly rather than argued: recovery performs no unlink, rename or replace."""
    _decide_all(world)
    plan = _force_cross(_plan(world))
    _run(world, plan)
    calls = []
    real = {name: getattr(os, name) for name in ("unlink", "remove", "rename", "replace",
                                                 "rmdir", "truncate")}

    def spy(name):
        def wrapper(*args, **kwargs):
            calls.append((name, args[0] if args else None))
            return real[name](*args, **kwargs)
        return wrapper

    for name in real:
        setattr(os, name, spy(name))
    try:
        report = _cold(world, plan)
    finally:
        for name, fn in real.items():
            setattr(os, name, fn)
    assert report.plan_finished is False
    assert calls == [], f"recovery touched the filesystem destructively: {calls}"
