"""R5-P49 — post-commit outcome truthfulness (F1) and append-only journal tail safety (F4).

Both findings come from the R5-P48 independent re-review, and both are the same mistake in
different clothes: **a reporting failure was allowed to contradict something the filesystem had
already done.**

* **F1** — every journal append after the commit point was unguarded. ``TransferJournal.append``
  converts any ``OSError`` into ``TransferRefused``, and ``execute_transfer``'s broad
  ``except TransferRefused`` turned that into ``ItemOutcome(REFUSED)`` whose dataclass defaults
  assert ``source_removed=False`` and ``published_path=""`` — reported about a clip that was
  already in the operator's Media Library with its source path empty. The batch-summary append sat
  outside the per-item ``try``, so a persistent failure raised and the caller got **no**
  ``TransferResult`` at all.
* **F4** — ``O_APPEND`` writes at the end of the *file*, not the end of the last *record*. With an
  unterminated fragment on disk the next append was glued onto it, the two became one unparseable
  line, and the newly committed event was silently lost — it did not survive a cold reopen.

The two are tested separately **and together**, because F4 is precisely the condition that makes
an F1 post-commit append fail in the first place.

Every test asserts the **real filesystem state, the durable journal after a cold reopen, and the
returned result**. A test that only checked a status code would not have caught either defect: in
both, the returned code was plausible and the disk said otherwise.

Fixtures are synthetic and live under pytest's ``tmp_path``. Nothing here reads or writes
production media, the ``F:`` journal backups or the ``J:`` retention archives.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import clips_review_transfer as tr

OWNER = "r5p49-owner"
SCAN = "r5p49-scan"
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


def _decide_all(world, decision=cr.SELECTED, content_sha256=_UNSET):
    for entry in world["entries"]:
        sha = _sha(entry.path) if content_sha256 is _UNSET else content_sha256
        world["store"].decide(OWNER, world["identities"][entry.clip_id], decision, SCAN,
                              content_sha256=sha)


def _revisions(world):
    return {e.clip_id: world["store"].get(world["identities"][e.clip_id])[1]
            for e in world["entries"]}


def _plan(world, **over):
    kwargs = dict(session_id=world["session"], page_token=world["page"], owner=OWNER,
                  scan_id=SCAN, input_folder=world["review"], library_folder=world["library"],
                  rejected_folder=world["rejected"], expected_revisions=_revisions(world))
    kwargs.update(over)
    return tr.plan_transfer(world["registry"], world["store"], **kwargs)


def _run(world, plan=None, journal=None, **hooks):
    return tr.execute_transfer(plan or _plan(world), journal or world["journal"],
                              store=world["store"], registry=world["registry"], **hooks)


def _force_cross(plan):
    items = tuple(tr.TransferItem(**{**i.__dict__, "operation": tr.OP_CROSS_VOLUME})
                  for i in plan.items)
    return tr.TransferPlan(**{**plan.__dict__, "items": items})


def _outcome(result, clip_id):
    return {o.clip_id: o for o in result.outcomes}[clip_id]


def _cold_states(journal, plan, clip_id):
    """Journal states as an INDEPENDENT reader sees them, from a fresh object over the file."""
    cold = tr.TransferJournal(journal.directory)
    return [e["state"] for e in cold.states_for(plan.plan_id).get(clip_id, [])]


def _cold_lines(journal):
    """Every line of the journal, parsed WITHOUT using the engine's reader at all.

    Deliberately reads bytes and splits on the terminator itself, because this is the independent
    oracle: if it borrowed ``records()`` it could not detect a reader that agrees with a broken
    writer. Decodes per line with ``errors="replace"`` so a torn multi-byte sequence is reported as
    unparseable rather than killing the oracle, and anything that is not a JSON **object** is
    unparseable too -- a bare array is not a record.
    """
    out = []
    try:
        with open(journal.path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return out
    for chunk in raw.split(b"\n"):
        text = chunk.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            out.append("<UNPARSEABLE>")
            continue
        out.append(parsed if isinstance(parsed, dict) else "<UNPARSEABLE>")
    return out


class FailAt(tr.TransferJournal):
    """A journal whose append fails for chosen states, with the exact exception a real OSError
    produces at that line. ``persistent`` keeps failing; otherwise only the first occurrence does.

    Raising ``TransferRefused("journal_unavailable")`` directly is behaviourally identical to the
    genuine fault: ``TransferJournal.append`` is the only thing that converts an ``OSError`` into
    that exception, and ``test_F1_a_REAL_windows_sharing_violation_produces_the_same_outcome``
    pins the equivalence against an actual share-denying handle.
    """

    def __init__(self, directory, states, persistent=False, code="journal_unavailable"):
        super().__init__(directory)
        self.states = set(states if isinstance(states, (set, list, tuple)) else [states])
        self.persistent = persistent
        self.code = code
        self.fired = set()
        self.attempted = []

    def append(self, event, **kwargs):
        state = event.get("state")
        self.attempted.append(state)
        if state in self.states and (self.persistent or state not in self.fired):
            self.fired.add(state)
            raise tr.TransferRefused(self.code, "errno 13")
        return super().append(event, **kwargs)


def _exclusive_handle(path):
    """A real Windows share-denying handle, so a later os.open on `path` fails for real."""
    import ctypes
    handle = ctypes.windll.kernel32.CreateFileW(
        ctypes.c_wchar_p(path), 0x80000000, 0, None, 3, 0, None)
    if handle == -1:
        raise OSError(f"could not take an exclusive handle on {path}")
    return handle


def _close_handle(handle):
    import ctypes
    ctypes.windll.kernel32.CloseHandle(handle)


windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows share-denying handle")


# =============================================================================================
# F1 — THE ONE INVARIANT
# =============================================================================================

def test_F1_no_outcome_may_claim_nothing_moved_after_a_commit(world):
    """The whole finding reduces to this, so it is asserted directly and over every state.

    An outcome whose state asserts a filesystem change may never also report
    ``source_removed=False`` *and* an empty ``published_path``, because that pair is exactly what
    ``REFUSED`` means and what the defect reported.
    """
    assert tr.PUBLISHED_UNJOURNALLED in tr.PUBLICATION_ASSERTED
    assert tr.COMPLETE in tr.PUBLICATION_ASSERTED
    assert tr.SOURCE_REMOVAL_BLOCKED in tr.PUBLICATION_ASSERTED
    assert tr.REFUSED not in tr.PUBLICATION_ASSERTED
    assert tr.ROLLED_BACK not in tr.PUBLICATION_ASSERTED, \
        "a completed rollback really did leave nothing published -- that is not the same claim"

    _decide_all(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED)
    plan = _plan(world)
    result = _run(world, plan, journal=journal)
    for outcome in result.outcomes:
        if outcome.state in tr.PUBLICATION_ASSERTED:
            assert outcome.published_path or outcome.source_removed, (
                f"{outcome.state} asserts a filesystem change but reports neither a published "
                f"path nor a removed source")


# =============================================================================================
# F1 — the mandatory failure matrix, every journal boundary
# =============================================================================================

def test_F1_01_PLANNED_append_failure_refuses_before_any_mutation(world):
    """Pre-mutation: refusing loudly is correct -- there is no outcome list to discard, and
    without a durable intent log no clip may move at all."""
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.PLANNED, persistent=True)
    with pytest.raises(tr.TransferRefused) as caught:
        _run(world, plan, journal=journal)
    assert caught.value.code == "journal_unavailable"
    for item in plan.items:
        assert os.path.isfile(item.source_path), "nothing moved"
        assert not os.path.lexists(item.destination_path)


def test_F1_02_INTENT_append_failure_refuses_before_any_mutation(world):
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.INTENT_RECORDED, persistent=True)
    result = _run(world, plan, journal=journal)
    first = _outcome(result, plan.items[0].clip_id)
    assert first.state == tr.REFUSED, "nothing moved, so REFUSED is the truth here"
    assert first.code == "journal_unavailable"
    assert first.source_removed is False and not first.published_path
    for item in plan.items:
        assert os.path.isfile(item.source_path)
        assert not os.path.lexists(item.destination_path)
    assert result.advance_page is False


def test_F1_03_STAGED_append_failure_leaves_the_original_and_publishes_nothing(world):
    _decide_all(world)
    plan = _force_cross(_plan(world))
    journal = FailAt(str(world["tmp"] / "journal"), tr.STAGED, persistent=True)
    result = _run(world, plan, journal=journal)
    first = _outcome(result, plan.items[0].clip_id)
    assert first.state == tr.REFUSED, "staging is pre-publication; nothing was published"
    assert first.source_removed is False and not first.published_path
    assert os.path.isfile(plan.items[0].source_path)
    assert not os.path.lexists(plan.items[0].destination_path)
    assert tr.staging_leftovers(world["library"]) == [], "the .part temporary was cleaned up"


def test_F1_04_VERIFIED_append_failure_publishes_nothing(world):
    _decide_all(world)
    plan = _force_cross(_plan(world))
    journal = FailAt(str(world["tmp"] / "journal"), tr.VERIFIED, persistent=True)
    result = _run(world, plan, journal=journal)
    first = _outcome(result, plan.items[0].clip_id)
    assert first.state == tr.REFUSED
    assert os.path.isfile(plan.items[0].source_path)
    assert not os.path.lexists(plan.items[0].destination_path)
    assert tr.staging_leftovers(world["library"]) == []


@pytest.mark.parametrize("failing_state", [tr.PUBLISHED, tr.SOURCE_REMOVED, tr.COMPLETE])
def test_F1_05_06_07_same_volume_post_commit_append_failure_tells_the_truth(world, failing_state):
    """Cases 5, 6 and 7: the rename succeeded and verified; one of the three records after it
    cannot be written. All three must report the publication, never retract it."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    approved = item.approved_sha256
    journal = FailAt(str(world["tmp"] / "journal"), failing_state, persistent=True)
    result = _run(world, plan, journal=journal)
    outcome = _outcome(result, item.clip_id)

    # the filesystem
    assert not os.path.lexists(item.source_path), "the rename really moved it"
    assert os.path.isfile(item.destination_path)
    assert _sha(item.destination_path) == approved, "and what is published is the approved content"

    # the outcome
    assert outcome.state == tr.PUBLISHED_UNJOURNALLED
    assert outcome.code == "post_commit_journal_failure"
    assert outcome.source_removed is True
    assert outcome.published_path == item.destination_path
    assert outcome.bytes_copied == item.source_size
    assert outcome.sha256 == approved
    assert "journal" in outcome.detail

    # the batch
    assert result.state == tr.PARTIAL
    assert result.advance_page is False
    assert result.counts.get("published_unjournalled") == 1
    assert result.counts.get("recovery_reason")
    assert result.counts["moved"] == 0, "an unjournalled publication is not a completed move"

    # remaining clips untouched and declared
    for skipped in plan.items[1:]:
        assert os.path.isfile(skipped.source_path)
        assert _outcome(result, skipped.clip_id).state == tr.PLANNED


@needs_crossvol
@pytest.mark.parametrize("failing_state", [tr.PUBLISHED, tr.SOURCE_REMOVAL_BLOCKED])
def test_F1_08_09_cross_volume_post_commit_append_failure_names_both_objects(
        world, tmp_path, failing_state):
    """Cases 8 and 9, on a REAL second volume. The verified copy exists and the original is
    deliberately retained, so the outcome must name both and claim neither a removal nor a
    completed move."""
    library = os.path.join(CROSSVOL, f"p49-lib-{failing_state.lower()}-{os.getpid()}")
    os.makedirs(library, exist_ok=True)
    try:
        _decide_all(world)
        plan = _plan(world, library_folder=library)
        assert plan.ok, [r.code for r in plan.refusals]
        item = plan.items[0]
        assert item.operation == tr.OP_CROSS_VOLUME, "fixture is not genuinely cross-volume"
        approved = item.approved_sha256
        journal = FailAt(str(world["tmp"] / "journal"), failing_state, persistent=True)
        result = _run(world, plan, journal=journal)
        outcome = _outcome(result, item.clip_id)

        assert os.path.isfile(item.source_path), "the original is deliberately retained"
        assert _sha(item.source_path) == approved
        assert os.path.isfile(item.destination_path), "the verified copy is published"
        assert _sha(item.destination_path) == approved

        assert outcome.state == tr.PUBLISHED_UNJOURNALLED
        assert outcome.source_removed is False, "never report SOURCE_REMOVED on this path"
        assert outcome.published_path == item.destination_path
        assert outcome.sha256 == approved
        assert outcome.bytes_copied == item.source_size

        assert result.state == tr.PARTIAL
        assert result.advance_page is False
        assert tr.SOURCE_REMOVED not in _cold_states(journal, plan, item.clip_id)
        assert tr.COMPLETE not in _cold_states(journal, plan, item.clip_id)
        assert tr.staging_leftovers(library) == []
    finally:
        for name in os.listdir(library):
            os.remove(os.path.join(library, name))
        os.rmdir(library)


def test_F1_10_ROLLED_BACK_append_failure_still_reports_the_rollback(world):
    """Case 10: the rollback succeeded, so the filesystem is unambiguous and benign -- but the
    outcome must say so rather than collapsing to REFUSED, and must flag the missing evidence."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    journal = FailAt(str(world["tmp"] / "journal"), tr.ROLLED_BACK, persistent=True)
    real_verify = tr._verify_published

    def fail_verification(it, expected):
        raise tr.TransferRefused("published_mismatch", "injected post-commit failure")

    tr._verify_published = fail_verification
    try:
        result = _run(world, plan, journal=journal)
    finally:
        tr._verify_published = real_verify
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.ROLLED_BACK, "never silently REFUSED"
    assert outcome.code == "rollback_unjournalled"
    assert "could not be written" in outcome.detail
    assert os.path.isfile(item.source_path), "the object was put back"
    assert not os.path.lexists(item.destination_path), "nothing left published"
    assert result.advance_page is False


def test_F1_11_RECOVERY_REQUIRED_append_failure_stays_recovery_required(world):
    """Case 11: the rollback itself failed AND the escalation could not be journalled. The most
    severe honest state must survive both failures."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    journal = FailAt(str(world["tmp"] / "journal"), tr.RECOVERY_REQUIRED, persistent=True)
    real_verify = tr._verify_published
    occupant = b"a new file took the vacated source path"

    def occupy_then_fail(it, expected):
        with open(it.source_path, "wb") as handle:
            handle.write(occupant)
        raise tr.TransferRefused("published_mismatch", "injected")

    tr._verify_published = occupy_then_fail
    try:
        result = _run(world, plan, journal=journal)
    finally:
        tr._verify_published = real_verify
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.RECOVERY_REQUIRED
    assert outcome.code == "rollback_failed"
    assert outcome.source_removed is True
    assert outcome.published_path == item.destination_path
    assert "could not be journalled" in outcome.detail
    with open(item.source_path, "rb") as handle:
        assert handle.read() == occupant, "the occupant was never overwritten"
    assert os.path.isfile(item.destination_path)


def test_F1_12_PARTIAL_batch_summary_append_failure_preserves_every_item_outcome(world):
    """Case 12: one item published, a later one failed, and the batch summary cannot be written.
    The per-item results are already decided and must all survive."""
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.PARTIAL, persistent=True)
    calls = {"n": 0}

    def fail_second(_item):
        calls["n"] += 1
        if calls["n"] == 2:
            raise tr.TransferRefused("injected", "second item fails before publication")

    result = _run(world, plan, journal=journal, before_publish=fail_second)
    assert result.state == tr.PARTIAL
    assert len(result.outcomes) == len(plan.items), "no outcome was discarded"
    assert _outcome(result, plan.items[0].clip_id).state == tr.COMPLETE
    assert _outcome(result, plan.items[1].clip_id).state == tr.REFUSED
    assert _outcome(result, plan.items[2].clip_id).state == tr.PLANNED
    assert result.counts["moved"] == 1 and result.counts["failed"] == 1
    assert result.counts["remaining"] == 1
    assert result.counts.get("summary_unjournalled"), "the lost summary is declared"
    assert result.advance_page is False


def test_F1_13_COMPLETE_batch_summary_append_failure_downgrades_and_does_not_advance(world):
    """Case 13: every item completed durably, but the batch summary did not. Durable completion
    of the *batch* cannot be proven, so COMPLETE is downgraded and the page must not advance."""
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.COMPLETE, persistent=True)
    # only the BATCH record is a {"k": "batch"} event; item COMPLETE records share the state name,
    # so fail on the batch summary alone by inspecting `k`
    class FailBatchOnly(tr.TransferJournal):
        def __init__(self, directory):
            super().__init__(directory)
            self.attempted = []

        def append(self, event, **kwargs):
            self.attempted.append((event.get("k"), event.get("state")))
            if event.get("k") == "batch":
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            return super().append(event, **kwargs)

    journal = FailBatchOnly(str(world["tmp"] / "journal"))
    result = _run(world, plan, journal=journal)
    assert all(o.state == tr.COMPLETE for o in result.outcomes), "every item really did complete"
    assert result.counts["moved"] == len(plan.items)
    assert result.state == tr.PARTIAL, "the batch cannot claim durable completion"
    assert result.advance_page is False
    assert result.counts.get("summary_unjournalled")
    assert result.counts.get("recovery_reason") == "batch_summary_journal_failure"
    for item in plan.items:
        assert os.path.isfile(item.destination_path)
        assert not os.path.lexists(item.source_path)


def test_F1_14_a_transient_journal_lock_records_the_condition_itself(world):
    """Case 14: only the PUBLISHED record fails and the journal then works again, so the
    best-effort PUBLISHED_UNJOURNALLED note lands and is durably readable."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED, persistent=False)
    result = _run(world, plan, journal=journal)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.PUBLISHED_UNJOURNALLED
    cold = _cold_states(journal, plan, item.clip_id)
    assert cold[-1] == tr.PUBLISHED_UNJOURNALLED, \
        "a transient failure still leaves durable evidence of the condition"
    assert tr.PUBLISHED not in cold, "the record that failed is genuinely absent"


def test_F1_15_a_persistent_journal_lock_still_returns_a_result(world):
    """Case 15: nothing can be written from the commit point onwards. The caller must still get a
    TransferResult -- the original raised and discarded everything."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]

    class Dead(tr.TransferJournal):
        def __init__(self, directory):
            super().__init__(directory)
            self.allow = True

        def append(self, event, **kwargs):
            if not self.allow:
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            if event.get("state") == tr.PUBLISHED:
                self.allow = False
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            return super().append(event, **kwargs)

    journal = Dead(str(world["tmp"] / "journal"))
    result = _run(world, plan, journal=journal)          # must NOT raise
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.PUBLISHED_UNJOURNALLED
    assert outcome.published_path == item.destination_path
    assert outcome.source_removed is True
    assert "could not be journalled either" in outcome.detail, \
        "the failed best-effort note is itself declared"
    assert result.state == tr.PARTIAL and result.advance_page is False
    assert not os.path.lexists(item.source_path) and os.path.isfile(item.destination_path)


def test_F1_16_a_positive_short_write_is_not_silently_accepted(world):
    """Case 16: disk-full / short-write. ``os.write`` may accept fewer bytes than offered and
    report it in its return value with no exception (the R5-P3 / F4 defect)."""
    journal = tr.TransferJournal(str(world["tmp"] / "journal"))
    real_write = os.write

    def short(handle, payload):
        return real_write(handle, payload[:1]) if len(payload) > 1 else 0

    os.write = short
    try:
        with pytest.raises(tr.TransferRefused) as caught:
            journal.append({"k": "item", "state": tr.PLANNED, "plan_id": "p"})
    finally:
        os.write = real_write
    assert caught.value.code == "journal_write_stalled"


def test_F1_17_an_fsync_failure_is_reported_as_an_unavailable_journal(world):
    journal = tr.TransferJournal(str(world["tmp"] / "journal"))
    real_fsync = os.fsync

    def boom(handle):
        raise OSError(5, "simulated fsync failure")

    os.fsync = boom
    try:
        with pytest.raises(tr.TransferRefused) as caught:
            journal.append({"k": "item", "state": tr.PLANNED, "plan_id": "p"})
    finally:
        os.fsync = real_fsync
    assert caught.value.code == "journal_unavailable"


def test_F1_18_interruption_after_the_rename_leaves_a_recoverable_filesystem(world):
    """Case 18: a non-TransferRefused interruption after the rename. It propagates (the caller
    asked for that by crashing), the lease is released, and recovery reads the truth."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]

    class Interrupted(BaseException):
        pass

    def die(_item):
        raise Interrupted("simulated process death")

    with pytest.raises(BaseException) as caught:
        _run(world, plan, after_publish=die)
    assert isinstance(caught.value, Interrupted)
    assert not os.path.lexists(item.source_path)
    assert os.path.isfile(item.destination_path)
    assert world["store"].active_transfer(item.identity) == "", "the lease was released"
    report = tr.recover(plan, tr.TransferJournal(world["journal"].directory))
    # [R5-P50 / F2] This assertion originally read `action == "none"` -- "recovery sees the
    # completed move". That was this file writing down the F2 defect: the journal here stops at
    # PUBLISHED because the interruption fired before the COMPLETE record, so the move is
    # physically done and **not durably confirmed**. Matching bytes plus an empty source path are
    # necessary for completion, never sufficient, since an unjournalled publication looks
    # identical on disk. Recovery therefore escalates instead of declaring it finished.
    assert [e["state"] for e in
            tr.TransferJournal(world["journal"].directory).states_for(plan.plan_id)[item.clip_id]] \
        == [tr.INTENT_RECORDED, tr.PUBLISHED], "no COMPLETE record was ever written"
    assert report.findings[0].action == "operator_required"
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False
    assert "cannot be shown to have completed durably" in report.findings[0].detail


def test_F1_19_interruption_after_publication_is_reconstructible_from_the_filesystem(world):
    """Case 19 / 20: the journal is missing its final publication record, and a COLD recovery must
    still establish the truth from the filesystem rather than from the absent record."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    journal = FailAt(str(world["tmp"] / "journal"), {tr.PUBLISHED, tr.PUBLISHED_UNJOURNALLED},
                     persistent=True)
    result = _run(world, plan, journal=journal)
    assert _outcome(result, item.clip_id).state == tr.PUBLISHED_UNJOURNALLED
    cold_journal = tr.TransferJournal(journal.directory)
    cold = cold_journal.states_for(plan.plan_id).get(item.clip_id, [])
    assert [e["state"] for e in cold] == [tr.INTENT_RECORDED], \
        "the journal genuinely lacks any publication record"
    report = tr.recover(plan, cold_journal)
    moved = [f for f in report.findings if f.clip_id == item.clip_id]
    # [R5-P50 / F2] This originally asserted `action == "none"` on the reasoning that recovery
    # "establishes the completed move from the filesystem, not from the journal". R5-P48 / F2
    # showed that reasoning is the defect: the filesystem cannot distinguish a durably completed
    # move from an unjournalled publication, and this very test constructs the latter. The
    # correct answer is to report what IS known -- the bytes are published and the source path is
    # empty -- while refusing to call it finished, because no durable record proves it.
    assert moved and moved[0].action == "operator_required"
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False, "an unjournalled publication is not a finished plan"
    assert report.needs_operator is True
    assert os.path.isfile(item.destination_path), "and the published object is left exactly alone"
    assert tr.PUBLISHED_UNJOURNALLED not in [
        e["state"] for e in cold_journal.states_for(plan.plan_id).get(item.clip_id, [])], \
        "the best-effort condition record could not be written either, which is why this escalates"


@windows_only
def test_F1_a_REAL_windows_sharing_violation_produces_the_same_outcome(world):
    """The injected ``TransferRefused`` used throughout this file is equivalent to a genuine OS
    fault. Proven here with a real ``CreateFileW`` share-denying handle on the journal file, which
    makes the engine's own ``os.open`` fail for real."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    holder = {"h": None}

    def lock_journal(_item):
        holder["h"] = _exclusive_handle(world["journal"].path)

    try:
        result = _run(world, plan, before_publish=lock_journal)
    finally:
        if holder["h"] is not None:
            _close_handle(holder["h"])
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.PUBLISHED_UNJOURNALLED, "a real OS fault, the same honest outcome"
    assert outcome.source_removed is True
    assert outcome.published_path == item.destination_path
    assert outcome.sha256 == item.approved_sha256
    assert result.state == tr.PARTIAL and result.advance_page is False
    assert not os.path.lexists(item.source_path) and os.path.isfile(item.destination_path)


# =============================================================================================
# F1 — the mandatory negative controls
# =============================================================================================

def test_F1_NEGATIVE_the_old_post_commit_REFUSED_behaviour_is_caught(world):
    """**The strongest required control.** Restore the original
    ``except TransferRefused -> REFUSED`` after the commit point and require this file to catch it.

    The restoration is done by replacing ``_published_unjournalled`` with the outcome the old code
    produced, which is exactly what ``execute_transfer``'s broad handler used to build.
    """
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    real = tr._published_unjournalled

    def old_behaviour(item_, plan_, journal_, cause, published, *, source_removed, now):
        return tr.ItemOutcome(item_.clip_id, tr.REFUSED, cause.code, cause.detail)

    tr._published_unjournalled = old_behaviour
    try:
        journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED, persistent=True)
        result = _run(world, plan, journal=journal)
        outcome = _outcome(result, item.clip_id)
        # the defect, restored
        assert outcome.state == tr.REFUSED
        assert outcome.source_removed is False and not outcome.published_path
        # ...and the filesystem says otherwise, which is what makes it a defect
        really_moved = (not os.path.lexists(item.source_path)
                        and os.path.isfile(item.destination_path))
        assert really_moved
        violated = (outcome.state in tr.PUBLICATION_ASSERTED
                    or (really_moved and not outcome.published_path
                        and outcome.source_removed is False))
        assert violated, "the control must be detectable by the invariant this suite asserts"
    finally:
        tr._published_unjournalled = real


def test_F1_NEGATIVE_an_unguarded_batch_summary_append_is_caught(world):
    """The second required control: restore the unguarded batch-summary append and show that it
    discards the whole outcome list by raising."""
    _decide_all(world)
    plan = _plan(world)
    real = tr._try_journal

    def unguarded(journal, event):
        journal.append(event)                 # the original, without the guard
        return ""

    tr._try_journal = unguarded
    try:
        class FailBatchOnly(tr.TransferJournal):
            def append(self, event, **kwargs):
                if event.get("k") == "batch":
                    raise tr.TransferRefused("journal_unavailable", "errno 13")
                return super().append(event, **kwargs)

        journal = FailBatchOnly(str(world["tmp"] / "journal"))
        with pytest.raises(tr.TransferRefused):
            _run(world, plan, journal=journal)
        # the files really moved, and the caller got no result at all -- the F1-B defect
        for item in plan.items:
            assert os.path.isfile(item.destination_path)
            assert not os.path.lexists(item.source_path)
    finally:
        tr._try_journal = real


# =============================================================================================
# F4 — journal tail classification
# =============================================================================================

def _journal_with_tail(tmp_path, name, tail_bytes, *, seed=True):
    journal = tr.TransferJournal(str(tmp_path / name))
    if seed:
        journal.append({"k": "item", "plan_id": "p", "clip_id": "c",
                        "state": tr.INTENT_RECORDED})
    if tail_bytes:
        with open(journal.path, "ab") as handle:
            handle.write(tail_bytes)
    return journal


def test_F4_01_a_healthy_newline_terminated_journal_needs_no_separator(tmp_path):
    journal = _journal_with_tail(tmp_path, "j1", b"")
    assert journal.tail_state() == (tr.TAIL_EMPTY, 0)
    before = open(journal.path, "rb").read()
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    assert journal.tails_separated == 0, "a healthy journal is never 'repaired'"
    assert open(journal.path, "rb").read().startswith(before)
    assert [e["state"] for e in journal.records()] == [tr.INTENT_RECORDED, tr.STAGED]


def test_F4_02_a_VALID_final_record_missing_only_its_newline_is_preserved(tmp_path):
    """Case 2 / 19. This is the case that must NOT be treated as damage: it is a committed record
    that lost its terminator, and misclassifying it would discard real history."""
    valid = json.dumps({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED},
                       separators=(",", ":"), sort_keys=True).encode("utf-8")
    journal = _journal_with_tail(tmp_path, "j2", valid)
    state, length = journal.tail_state()
    assert state == tr.TAIL_COMPLETE_UNTERMINATED and length == len(valid)
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.VERIFIED})
    cold = _cold_lines(journal)
    assert "<UNPARSEABLE>" not in cold, "nothing was damaged"
    assert [e["state"] for e in cold] == [tr.INTENT_RECORDED, tr.STAGED, tr.VERIFIED], \
        "the newline-less record survived as real history, in order"
    assert journal.damage() == [], "and it is NOT reported as damage"


def test_F4_03_an_INCOMPLETE_final_record_is_separated_and_the_new_event_survives(tmp_path):
    """Case 3 / 5 / 6 — the original F4 defect. The new event must be independently parseable
    after a cold reopen, and the torn bytes must be preserved, not truncated."""
    journal = _journal_with_tail(tmp_path, "j3", b'{"k":"item","plan_id":"p","clip_id":"c","sta')
    before = open(journal.path, "rb").read()
    assert journal.tail_state()[0] == tr.TAIL_INCOMPLETE
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED,
                    "marker": "R5P49"})
    after = open(journal.path, "rb").read()
    assert after.startswith(before), "original bytes are never truncated or rewritten"
    assert journal.tails_separated == 1
    cold = _cold_lines(journal)
    assert "<UNPARSEABLE>" in cold, "the torn fragment is isolated and surfaced"
    separators = [e for e in cold if e != "<UNPARSEABLE>" and e.get("k") == tr.TAIL_REPAIR_KIND]
    assert len(separators) == 1
    assert separators[0]["tail"] == tr.TAIL_INCOMPLETE
    assert separators[0]["fragment_bytes"] == 44
    assert "clip_id" not in separators[0] and "state" not in separators[0], \
        "a separator can never be mistaken for a transfer event"
    survivors = [e for e in cold if e != "<UNPARSEABLE>" and e.get("marker") == "R5P49"]
    assert survivors and survivors[0]["state"] == tr.STAGED, \
        "THE DEFECT: this event used to be silently lost"


def test_F4_04_a_MALFORMED_complete_final_record_is_surfaced_not_discarded(tmp_path):
    """Case 4 / 17 / 18: a complete line that is valid JSON but not a usable record."""
    journal = _journal_with_tail(tmp_path, "j4", b'[1,2,3]\n')
    assert journal.tail_state() == (tr.TAIL_EMPTY, 0), "it is newline-terminated"
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    records = journal.records()
    damaged = [r for r in records if r.get("damaged")]
    assert len(damaged) == 1, "a non-object line is surfaced as damage, never silently dropped"
    assert damaged[0]["line"] == 2
    assert journal.damage(), "and damage() reports it"
    assert [r["state"] for r in records if r.get("state")] == [tr.INTENT_RECORDED, tr.STAGED]


def test_F4_04b_a_malformed_UNTERMINATED_object_is_classified_as_malformed(tmp_path):
    journal = _journal_with_tail(tmp_path, "j4b", b'[1,2,3]')
    assert journal.tail_state()[0] == tr.TAIL_MALFORMED
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    cold = _cold_lines(journal)
    assert [e for e in cold if e != "<UNPARSEABLE>" and e.get("k") == tr.TAIL_REPAIR_KIND], \
        "a malformed complete record still gets an identifiable boundary"
    assert any(e != "<UNPARSEABLE>" and e.get("state") == tr.STAGED for e in cold)


def test_F4_05_a_torn_MULTIBYTE_sequence_cannot_be_a_complete_record(tmp_path):
    """A torn UTF-8 sequence must classify as incomplete, never crash the classifier
    (the R5-P17 lesson, restated for this journal)."""
    journal = _journal_with_tail(tmp_path, "j5", b'{"k":"item","note":"\xc4\x8d\xc5')
    assert journal.tail_state()[0] == tr.TAIL_INCOMPLETE
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    assert any(e != "<UNPARSEABLE>" and e.get("state") == tr.STAGED
               for e in _cold_lines(journal))


def test_F4_07_two_sequential_appends_after_a_repair_both_survive(tmp_path):
    """Case 7: and only ONE separator is written -- repair is idempotent, not per-append."""
    journal = _journal_with_tail(tmp_path, "j7", b'{"torn')
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.VERIFIED})
    cold = _cold_lines(journal)
    separators = [e for e in cold if e != "<UNPARSEABLE>" and e.get("k") == tr.TAIL_REPAIR_KIND]
    assert len(separators) == 1, "the second append found a clean boundary"
    assert journal.tails_separated == 1
    states = [e.get("state") for e in cold if e != "<UNPARSEABLE>"]
    assert tr.STAGED in states and tr.VERIFIED in states


def test_F4_08_a_cold_reload_after_repair_sees_exactly_the_same_history(tmp_path):
    journal = _journal_with_tail(tmp_path, "j8", b'{"torn')
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    warm = journal.records()
    cold = tr.TransferJournal(journal.directory).records()
    assert warm == cold
    assert [e["state"] for e in cold if e.get("state")] == [tr.INTENT_RECORDED, tr.STAGED]


def test_F4_09_an_interrupted_separator_write_creates_no_false_event(tmp_path):
    """Case 9 / 11: the separator is one payload, so a partial write leaves either a clean
    boundary or a torn separator -- and a torn separator is repaired by the next append and can
    never parse as a transfer event."""
    journal = _journal_with_tail(tmp_path, "j9", b'{"torn')
    real_write = os.write
    state = {"cut": True}

    def cut_once(handle, payload):
        if state["cut"] and b"tail_separator" in payload:
            state["cut"] = False
            return real_write(handle, payload[:len(payload) // 2])   # torn separator
        return real_write(handle, payload)

    os.write = cut_once
    try:
        journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    except tr.TransferRefused:
        pass                                   # a stalled write is an acceptable outcome here
    finally:
        os.write = real_write
    cold = _cold_lines(journal)
    assert not any(e != "<UNPARSEABLE>" and e.get("state") == tr.COMPLETE for e in cold), \
        "no false success was manufactured"
    # the next append must still succeed and be recoverable
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.VERIFIED})
    cold = _cold_lines(journal)
    assert any(e != "<UNPARSEABLE>" and e.get("state") == tr.VERIFIED for e in cold), \
        "a crash between separation and append remains recoverable"


def test_F4_12_a_zero_byte_write_raises_rather_than_looping(tmp_path):
    journal = tr.TransferJournal(str(tmp_path / "j12"))
    real_write = os.write

    os.write = lambda handle, payload: 0
    try:
        with pytest.raises(tr.TransferRefused) as caught:
            journal.append({"k": "item", "plan_id": "p", "state": tr.PLANNED})
    finally:
        os.write = real_write
    assert caught.value.code == "journal_write_stalled"


def test_F4_14_a_read_denied_tail_REFUSES_the_write(tmp_path):
    """Case 14 / 15 — case D. Being unable to inspect the tail is not evidence that the file ends
    on a record boundary, so the write is refused rather than risking another lost event."""
    journal = _journal_with_tail(tmp_path, "j14", b'{"torn')
    before = open(journal.path, "rb").read()
    real_open = open
    import builtins

    def denied(path, *a, **k):
        if isinstance(path, str) and path == journal.path and "b" in str(a[0] if a else k.get("mode", "")):
            raise PermissionError(13, "access denied")
        return real_open(path, *a, **k)

    builtins.open = denied
    try:
        assert journal.tail_state() == (tr.TAIL_UNKNOWN, 0)
        with pytest.raises(tr.TransferRefused) as caught:
            journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    finally:
        builtins.open = real_open
    assert caught.value.code == "journal_tail_not_inspectable"
    assert open(journal.path, "rb").read() == before, "nothing was written"


def test_F4_16_appends_are_serialised_within_the_process(tmp_path):
    """Case 16. Classify-then-append is not atomic, so it is serialised by a process-local lock.
    Cross-process concurrency is explicitly NOT claimed -- see the class docstring."""
    journal = _journal_with_tail(tmp_path, "j16", b'{"torn')
    errors = []

    def writer(index):
        try:
            for round_ in range(5):
                journal.append({"k": "item", "plan_id": "p", "clip_id": f"c{index}",
                                "state": tr.STAGED, "n": round_})
        except BaseException as exc:                     # pragma: no cover - diagnostic
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    cold = _cold_lines(journal)
    events = [e for e in cold if e != "<UNPARSEABLE>" and e.get("state") == tr.STAGED]
    assert len(events) == 20, "every concurrent append is individually parseable"
    separators = [e for e in cold if e != "<UNPARSEABLE>" and e.get("k") == tr.TAIL_REPAIR_KIND]
    assert len(separators) == 1, "the torn tail was separated exactly once"


def test_F4_17_a_MALFORMED_INTERIOR_record_is_not_mistaken_for_valid_history(tmp_path):
    journal = tr.TransferJournal(str(tmp_path / "j17"))
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.INTENT_RECORDED})
    with open(journal.path, "ab") as handle:
        handle.write(b'{"k":"item","plan_id":"p","clip_id":"c","state":"COMP\n')   # damaged
    journal.append({"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED})
    records = journal.records()
    assert [r.get("state") for r in records if r.get("state")] == [tr.INTENT_RECORDED, tr.STAGED]
    assert len(journal.damage()) == 1
    assert journal.damage()[0]["damaged"] is True


def test_F4_20_recovery_ESCALATES_when_the_journal_carries_damaged_records(world):
    """Case 20: a recovery reader must not silently infer a complete history from a journal whose
    damaged records it cannot attribute to any clip.

    Narrowly a journal-integrity escalation. R5-P48 / F2 -- ``recover()`` aggregating
    intentionally-unfinished work into COMPLETE -- is a DIFFERENT finding and remains OPEN.
    """
    _decide_all(world)
    plan = _plan(world)
    _run(world, plan)
    clean = tr.recover(plan, tr.TransferJournal(world["journal"].directory))
    assert clean.state == tr.COMPLETE and clean.needs_operator is False

    with open(world["journal"].path, "ab") as handle:
        handle.write(b'{"k":"item","plan_id":"x","state":"COMP')       # damaged, unattributable
    dirty_journal = tr.TransferJournal(world["journal"].directory)
    assert dirty_journal.damage(), "the damage is visible"
    dirty = tr.recover(plan, dirty_journal)
    assert dirty.needs_operator is True
    assert dirty.state == tr.RECOVERY_REQUIRED
    integrity = [f for f in dirty.findings if f.observed.startswith("journal=")]
    assert integrity and "incomplete" in integrity[0].detail


def test_F4_NEGATIVE_the_old_bare_O_APPEND_loses_the_event(tmp_path):
    """The required control: restore the original unguarded append and show it loses the event
    that the corrected implementation keeps."""
    torn = b'{"k":"item","plan_id":"p","clip_id":"c","sta'
    payload = {"k": "item", "plan_id": "p", "clip_id": "c", "state": tr.STAGED,
               "marker": "R5P49-CONTROL"}

    # --- the OLD behaviour, reconstructed exactly: no tail inspection, no separator
    old = _journal_with_tail(tmp_path, "old", torn)
    blob = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    handle = os.open(old.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(handle, blob)
        os.fsync(handle)
    finally:
        os.close(handle)
    old_cold = _cold_lines(old)
    assert not any(e != "<UNPARSEABLE>" and e.get("marker") == "R5P49-CONTROL"
                   for e in old_cold), "THE DEFECT: the committed event is unrecoverable"

    # --- the CORRECTED behaviour on identical input
    new = _journal_with_tail(tmp_path, "new", torn)
    new.append(dict(payload))
    new_cold = _cold_lines(new)
    assert any(e != "<UNPARSEABLE>" and e.get("marker") == "R5P49-CONTROL"
               for e in new_cold), "the fix keeps it"


# =============================================================================================
# F1 + F4 TOGETHER — F4 is the condition that makes an F1 post-commit append fail
# =============================================================================================

def test_COMBINED_a_publication_whose_journal_has_a_torn_tail_is_recorded_not_lost(world):
    """Source published, and the PUBLISHED append meets an incomplete tail. Safe tail handling
    succeeds, so the publication outcome IS durably recorded -- and would have been lost before."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    world["journal"].append({"k": "warmup", "plan_id": plan.plan_id, "state": tr.PLANNED})
    with open(world["journal"].path, "ab") as handle:
        handle.write(b'{"k":"item","plan_id":"torn","sta')            # torn tail, pre-existing
    before = open(world["journal"].path, "rb").read()

    result = _run(world, plan)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.COMPLETE, "the torn tail did not break the transfer"
    after = open(world["journal"].path, "rb").read()
    assert after.startswith(before), "the pre-existing torn bytes were preserved"
    cold = _cold_states(world["journal"], plan, item.clip_id)
    assert cold == [tr.INTENT_RECORDED, tr.PUBLISHED, tr.SOURCE_REMOVED, tr.COMPLETE], \
        "every post-commit record is durably readable despite the torn tail"
    assert world["journal"].tails_separated == 1


def test_COMBINED_an_uninspectable_tail_at_publish_time_reports_the_publication_truthfully(world):
    """Safe tail handling CANNOT proceed (tail unreadable) exactly at the post-commit append. The
    published object must still be reported truthfully as recovery-required."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]

    class UninspectableAtPublish(tr.TransferJournal):
        def append(self, event, **kwargs):
            if event.get("state") in (tr.PUBLISHED, tr.PUBLISHED_UNJOURNALLED):
                raise tr.TransferRefused("journal_tail_not_inspectable",
                                         "the journal's final bytes could not be read")
            return super().append(event, **kwargs)

    journal = UninspectableAtPublish(str(world["tmp"] / "journal"))
    result = _run(world, plan, journal=journal)
    outcome = _outcome(result, item.clip_id)
    assert outcome.state == tr.PUBLISHED_UNJOURNALLED
    assert outcome.published_path == item.destination_path
    assert outcome.source_removed is True
    assert outcome.sha256 == item.approved_sha256
    assert "journal_tail_not_inspectable" in outcome.detail
    assert result.advance_page is False
    assert not os.path.lexists(item.source_path)
    assert os.path.isfile(item.destination_path)


def test_COMBINED_a_journal_that_recovers_afterwards_can_record_the_next_item(world):
    """The journal becomes writable again after a failed post-commit append. The batch has already
    stopped (correctly), and a LATER cold recovery must identify the real state of every clip
    without overwriting or deleting anything."""
    _decide_all(world)
    plan = _plan(world)
    journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED, persistent=False)
    result = _run(world, plan, journal=journal)
    assert _outcome(result, plan.items[0].clip_id).state == tr.PUBLISHED_UNJOURNALLED
    assert result.state == tr.PARTIAL and result.advance_page is False

    before = {item.clip_id: (os.path.lexists(item.source_path),
                             os.path.lexists(item.destination_path))
              for item in plan.items}
    cold = tr.TransferJournal(journal.directory)
    cold.append({"k": "note", "plan_id": plan.plan_id, "state": tr.PLANNED})   # journal works
    report = tr.recover(plan, cold)
    after = {item.clip_id: (os.path.lexists(item.source_path),
                            os.path.lexists(item.destination_path))
             for item in plan.items}
    assert before == after, "recovery mutated nothing"
    actions = {f.clip_id: f.action for f in report.findings if f.clip_id}
    # [R5-P50 / F2] The first clip was published but its PUBLISHED record failed, so the journal
    # carries PUBLISHED_UNJOURNALLED as its last state -- terminal, but explicitly NOT completion.
    # This assertion previously read `== "none"` ("recognised as moved"), which is the inference
    # F2 forbids: recovery may report that the object is published without calling the transfer
    # finished.
    assert actions[plan.items[0].clip_id] == "operator_required", \
        "published, but with no durable proof of completion"
    for skipped in plan.items[1:]:
        assert actions[skipped.clip_id] == "retry_safe", "the untouched clips are retry-safe"
    assert report.state == tr.RECOVERY_REQUIRED
    assert report.plan_finished is False
    assert len(report.remaining_items) == len(plan.items), "no clip is established as finished"


def test_COMBINED_a_failed_batch_summary_never_erases_known_item_outcomes(world):
    """Both halves at once: a torn tail AND a batch-summary append failure, after a real move."""
    _decide_all(world)
    plan = _plan(world)

    class FailBatchOnly(tr.TransferJournal):
        def append(self, event, **kwargs):
            if event.get("k") == "batch":
                raise tr.TransferRefused("journal_unavailable", "errno 13")
            return super().append(event, **kwargs)

    journal = FailBatchOnly(str(world["tmp"] / "journal"))
    journal.append({"k": "warmup", "plan_id": plan.plan_id, "state": tr.PLANNED})
    with open(journal.path, "ab") as handle:
        handle.write(b'{"torn')
    result = _run(world, plan, journal=journal)
    assert len(result.outcomes) == len(plan.items)
    assert all(o.state == tr.COMPLETE for o in result.outcomes)
    assert result.state == tr.PARTIAL, "durable batch completion cannot be proven"
    assert result.advance_page is False
    assert result.counts["moved"] == len(plan.items)
    for item in plan.items:
        assert _cold_states(journal, plan, item.clip_id)[-1] == tr.COMPLETE, \
            "each item's own records are durable and readable despite the torn tail"


def test_COMBINED_no_second_filesystem_mutation_compensates_for_lost_evidence(world):
    """A journal failure must never be answered with another filesystem operation. Counted
    directly: the only rename on this path is the publication itself."""
    _decide_all(world)
    plan = _plan(world)
    item = plan.items[0]
    renames = []
    unlinks = []
    real_rename, real_unlink = os.rename, os.unlink

    def counting_rename(src, dst):
        if ".probe-" not in str(src):
            renames.append((src, dst))
        return real_rename(src, dst)

    def counting_unlink(path):
        if ".probe-" not in str(path):
            unlinks.append(path)
        return real_unlink(path)

    os.rename, os.unlink = counting_rename, counting_unlink
    try:
        journal = FailAt(str(world["tmp"] / "journal"), tr.PUBLISHED, persistent=True)
        result = _run(world, plan, journal=journal)
    finally:
        os.rename, os.unlink = real_rename, real_unlink
    assert _outcome(result, item.clip_id).state == tr.PUBLISHED_UNJOURNALLED
    assert renames == [(item.source_path, item.destination_path)], \
        "exactly one rename: the publication. No rollback, no retry, no compensating move."
    assert unlinks == [], "and nothing was deleted"
