"""R13 single-flight: the admission rule, executed rather than asserted about.

[FORK] Digital-Union. ``beatsync_fork.transfer_guard`` imports nothing but the stdlib, so every
rule in it runs here on a bare interpreter -- which is the whole point of putting the authority in
a pure module instead of inside the Gradio handler. The handler's use of it is
``test_clips_transfer_overlay.py``.
"""

from __future__ import annotations

import ast
import inspect
import threading

import pytest

from beatsync_fork import transfer_guard as tg


def test_the_first_caller_wins_the_slot_and_the_second_is_refused():
    guard = tg.TransferGuard()
    first = guard.begin("tab-a", 7, "sig-1", "k:/clips")

    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin("tab-a", 7, "sig-1", "k:/clips")

    assert refused.value.code == "transfer_in_progress"
    assert refused.value.active.token == first.token, \
        "the refusal must carry the record that is actually running"
    assert guard.refused == 1


def test_a_duplicate_press_is_refused_whatever_it_claims_about_the_batch():
    """A second submission does not become admissible by naming a different generation or
    signature. Admission is per OWNER: a duplicate is always a duplicate from the same owner, and
    letting a re-scan entitle a second concurrent transfer is exactly how one batch would move
    while another moved the same files."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-1", "k:/clips")
    for generation, signature, root in ((2, "sig-2", "k:/clips"), (1, "sig-1", "k:/other"),
                                        (None, "", "")):
        with pytest.raises(tg.TransferBusy) as refused:
            guard.begin("tab-a", generation, signature, root)
        assert refused.value.code == "transfer_in_progress"


def test_two_browser_sessions_sorting_one_folder_is_refused_across_owners():
    """Two tabs have two owner keys, so the owner rule alone would let both sort folder A at
    once. The folder is therefore exclusive too, and the refusal says which case it is -- an
    operator told "this page is already being sorted" about somebody else's session would go
    looking for a press they never made."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "k:/clips")

    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin("tab-b", 1, "sig-b", "k:/clips")

    assert refused.value.code == "folder_in_progress"
    assert refused.value.active.owner == "tab-a"


def test_two_sessions_on_different_folders_do_not_block_each_other():
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "k:/one")
    second = guard.begin("tab-b", 1, "sig-b", "k:/two")
    assert second.owner == "tab-b"
    assert guard.refused == 0


def test_an_unknown_folder_key_claims_no_folder():
    """An empty key is "I could not identify the folder", not "every unidentified folder". The
    owner rule still covers the duplicate-press case, so this fails safe rather than open --
    and treating every empty key as one claim would let one session's unresolvable root block a
    completely unrelated one."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "")
    guard.begin("tab-b", 1, "sig-b", "")
    assert guard.active_on_root("") is None
    assert guard.refused == 0


def test_releasing_frees_the_slot_and_is_idempotent():
    guard = tg.TransferGuard()
    reservation = guard.begin("tab-a", 1, "sig", "k:/clips")

    assert guard.end(reservation) is True
    assert guard.end(reservation) is False, "a second release must not report that it released"
    assert guard.active_for("tab-a") is None
    guard.begin("tab-a", 2, "sig", "k:/clips")           # the slot is genuinely free again


def test_a_late_release_cannot_clear_a_newer_transfer():
    """The identity discipline `_end_verify_session` uses, and for the same reason: from inside
    `end` a stale reservation and the live one are indistinguishable without the token, so a
    transfer that finished late would silently un-busy the page a *newer* one is sorting -- and a
    third press would then be admitted alongside it."""
    guard = tg.TransferGuard()
    stale = guard.begin("tab-a", 1, "sig-1", "k:/clips")
    guard.end(stale)
    fresh = guard.begin("tab-a", 2, "sig-2", "k:/clips")

    assert guard.end(stale) is False
    assert guard.active_for("tab-a").token == fresh.token, "the live transfer was released"


def test_a_late_progress_update_cannot_write_into_a_newer_record():
    guard = tg.TransferGuard()
    stale = guard.begin("tab-a", 1, "sig-1", "k:/clips")
    guard.end(stale)
    guard.begin("tab-a", 2, "sig-2", "k:/clips")

    assert guard.note_progress(stale, 99, 99) is False
    assert guard.active_for("tab-a").done == 0, "a finished transfer wrote into the live one"


def test_progress_on_a_released_reservation_does_not_resurrect_it():
    guard = tg.TransferGuard()
    reservation = guard.begin("tab-a", 1, "sig", "k:/clips")
    guard.end(reservation)

    assert guard.note_progress(reservation, 3, 10) is False
    assert guard.active_for("tab-a") is None, "a released slot came back"


def test_progress_is_readable_so_a_refusal_can_report_it():
    guard = tg.TransferGuard()
    reservation = guard.begin("tab-a", 1, "sig", "k:/clips")
    guard.note_progress(reservation, 12, 84)

    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin("tab-a", 1, "sig", "k:/clips")
    assert (refused.value.active.done, refused.value.active.total) == (12, 84)


def test_busy_reports_either_reason_and_is_advisory():
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "k:/clips")

    assert guard.busy("tab-a") is not None, "this owner's own transfer"
    assert guard.busy("tab-b", "k:/clips") is not None, "somebody else's, on this folder"
    assert guard.busy("tab-b", "k:/elsewhere") is None
    assert guard.busy("tab-b") is None
    # Reading it changes nothing -- it is not an acquisition.
    assert guard.refused == 0
    assert guard.begin("tab-b", 1, "sig-b", "k:/elsewhere").owner == "tab-b"


def test_only_one_of_many_concurrent_presses_is_ever_admitted():
    """The real property, under real threads: `begin` is a test-and-set, so however the presses
    interleave exactly one wins. `if not busy: start()` passes a single-threaded test and fails
    this one."""
    guard = tg.TransferGuard()
    admitted, refusals = [], []
    start = threading.Barrier(24)

    def press():
        start.wait()
        try:
            admitted.append(guard.begin("tab-a", 1, "sig", "k:/clips"))
        except tg.TransferBusy as busy:
            refusals.append(busy.code)

    threads = [threading.Thread(target=press) for _ in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 1, f"{len(admitted)} presses were admitted at once"
    assert len(refusals) == 23
    assert set(refusals) == {"transfer_in_progress"}


def test_concurrent_presses_from_many_owners_on_one_folder_admit_exactly_one():
    guard = tg.TransferGuard()
    admitted, refusals = [], []
    start = threading.Barrier(16)

    def press(index):
        start.wait()
        try:
            admitted.append(guard.begin(f"tab-{index}", 1, "sig", "k:/clips"))
        except tg.TransferBusy as busy:
            refusals.append(busy.code)

    threads = [threading.Thread(target=press, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 1
    assert set(refusals) == {"folder_in_progress"}


def test_a_full_cycle_repeated_leaks_nothing():
    guard = tg.TransferGuard()
    for round_ in range(200):
        reservation = guard.begin("tab-a", round_, "sig", "k:/clips")
        guard.note_progress(reservation, round_, 200)
        assert guard.end(reservation) is True
    assert guard.active_for("tab-a") is None
    assert guard.refused == 0


# ---- decision claims, and the F-1 invariant ------------------------------------------------
#
# The property every test in this section is really about:
#
#     once `begin` has returned a reservation, no conflicting durable decision can be in flight
#     and none can start until it is released.
#
# That is what makes a post-admission snapshot the batch's final truth. A snapshot re-read on its
# own does NOT establish it -- see `test_clips_transfer_overlay.py`'s case D, which places a
# decision's commit after such a re-read and watches it be ignored.

def test_a_decision_claim_is_visible_and_releases():
    guard = tg.TransferGuard()
    assert guard.deciding() == 0
    slot = guard.begin_decision("tab-a", "k:/clips")
    assert guard.deciding() == 1
    assert guard.end_decision(slot) is True
    assert guard.deciding() == 0


def test_admission_is_refused_while_this_owners_decision_is_in_flight():
    """The straddle: a classification past its checks and about to commit. Admitting here is the
    F-1 defect -- the decision becomes durable a moment later and the batch moves by the snapshot
    it took before."""
    guard = tg.TransferGuard()
    slot = guard.begin_decision("tab-a", "k:/clips")
    with pytest.raises(tg.DecisionInFlight) as refused:
        guard.begin("tab-a", 1, "sig", "k:/clips")
    assert refused.value.code == "decision_in_flight"
    assert guard.active_for("tab-a") is None, "a refused admission must claim nothing"

    guard.end_decision(slot)
    assert guard.begin("tab-a", 1, "sig", "k:/clips").owner == "tab-a", \
        "the exclusion is a window, not a latch"


def test_admission_is_refused_while_another_sessions_decision_on_this_folder_is_in_flight():
    """The second tab's clips are the ones about to move, so its in-flight decision excludes this
    transfer for exactly the reason its committed one would."""
    guard = tg.TransferGuard()
    guard.begin_decision("tab-b", "k:/clips")
    with pytest.raises(tg.DecisionInFlight):
        guard.begin("tab-a", 1, "sig", "k:/clips")


def test_an_unrelated_decision_does_not_delay_admission():
    """Exclusion is the same two rules admission uses, not a global stop-the-world. Two operators
    reviewing two folders must not be able to refuse each other."""
    guard = tg.TransferGuard()
    guard.begin_decision("tab-b", "k:/other")
    reservation = guard.begin("tab-a", 1, "sig", "k:/clips")
    assert reservation.root_key == "k:/clips"


def test_a_decision_is_refused_while_this_page_is_being_sorted():
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/clips")
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-a", "k:/clips")
    assert refused.value.code == "transfer_in_progress"
    assert guard.deciding() == 0, "a refused claim must register nothing"


def test_a_decision_is_refused_while_someone_else_sorts_this_folder():
    """The cross-owner half, and it keeps `begin`'s own code rather than inventing one: the reason
    really is that another session is sorting this folder."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/clips")
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-b", "k:/clips")
    assert refused.value.code == "folder_in_progress"


def test_a_decision_on_an_unrelated_folder_is_admitted_during_a_transfer():
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/clips")
    slot = guard.begin_decision("tab-b", "k:/other")
    assert slot.owner == "tab-b"


def test_an_unresolvable_folder_still_excludes_its_own_owner():
    """`_clips_transfer_key` answers `''` when a path cannot be resolved. That weakens only the
    cross-session folder rule; the per-owner rule does not depend on a path at all."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "")
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-a", "")
    assert refused.value.code == "transfer_in_progress"

    other = guard.begin_decision("tab-b", "")
    assert other.owner == "tab-b", "an empty key must not match every other empty key"


def test_releasing_a_decision_slot_is_idempotent_and_token_matched():
    guard = tg.TransferGuard()
    first = guard.begin_decision("tab-a", "k:/clips")
    assert guard.end_decision(first) is True
    assert guard.end_decision(first) is False, "a second release must not report success"

    second = guard.begin_decision("tab-a", "k:/clips")
    assert guard.end_decision(first) is False, \
        "a stale release cleared a newer slot -- the same identity bug `end` avoids"
    assert guard.deciding() == 1
    assert guard.end_decision(second) is True


def test_a_transfer_token_can_never_match_a_decision_token():
    """Both claims draw from one counter, so no release can be mistaken for the other kind."""
    guard = tg.TransferGuard()
    slot = guard.begin_decision("tab-a", "k:/clips")
    guard.end_decision(slot)
    reservation = guard.begin("tab-a", 1, "sig", "k:/clips")
    assert reservation.token != slot.token


def test_repeated_decision_cycles_accumulate_nothing():
    guard = tg.TransferGuard()
    for _ in range(500):
        slot = guard.begin_decision("tab-a", "k:/clips")
        assert guard.end_decision(slot) is True
    assert guard.deciding() == 0
    assert guard.refused == 0


def test_a_decision_slot_survives_an_exception_in_the_work_it_covers():
    """The caller releases in a `finally`, and this is the shape that matters: a slot that
    outlived a failed commit would refuse this owner's transfers until the app restarted."""
    guard = tg.TransferGuard()
    slot = guard.begin_decision("tab-a", "k:/clips")
    try:
        raise RuntimeError("the journal append failed")
    except RuntimeError:
        pass
    finally:
        guard.end_decision(slot)
    assert guard.deciding() == 0
    assert guard.begin("tab-a", 1, "sig", "k:/clips").owner == "tab-a"


class _Race:
    """One barrier, half the workers pressing Confirm and half classifying, for one owner.

    **Every worker outcome is recorded, and that is the whole point rather than a nicety:**

    * ``won`` -- the claims that succeeded, by kind, each with the claim object so the caller can
      release it *after* joining;
    * ``refused`` -- the claims the guard declined, which is a *correct* outcome and has to be
      seen to have happened, or the assertions are being made over a race that never collided;
    * ``failed`` -- **any other exception**. Review finding F-3 was that this list did not exist.
      A worker whose body raised something its ``except`` clause did not name simply died, the
      thread turned it into a ``PytestUnhandledThreadExceptionWarning``, and the parent's "they
      never both won" assertion was satisfied because one side had never run at all -- so the
      test passed against an implementation with **no decision claim whatsoever**;
    * ``finished`` -- proof that every worker reached the end of its body, so a hang or an early
      return cannot pass for a clean race either.

    **Nothing is released inside the round.** That is load-bearing for the mutual-exclusion
    assertion: the claims have to be held concurrently for "both kinds won" to mean "both kinds
    were held at once". Releasing inside a worker would make a sequential pass of sixteen
    successful decisions after one finished transfer look like a violation, and would make the
    test assert nothing about overlap. The *interval* property -- that nothing can start while a
    conflicting claim is held -- is a different statement and is asserted separately by
    :func:`test_no_conflicting_claims_ever_overlap_in_time`.
    """

    def __init__(self, guard, threads=32):
        self.guard = guard
        self.threads = threads
        self.won = []
        self.refused = []
        self.failed = []
        self.finished = []

    def run(self):
        start = threading.Barrier(self.threads)

        def press():
            try:
                start.wait(30)
                try:
                    claim = self.guard.begin("tab-a", 1, "sig", "k:/clips", ("id-1", "id-2"))
                except (tg.TransferBusy, tg.DecisionInFlight) as declined:
                    self.refused.append(("transfer", declined.code))
                else:
                    self.won.append(("transfer", claim))
            except BaseException as unexpected:          # noqa: BLE001 - recorded, see docstring
                self.failed.append(("transfer", repr(unexpected)))
            finally:
                self.finished.append("transfer")

        def classify():
            try:
                start.wait(30)
                try:
                    claim = self.guard.begin_decision("tab-a", "k:/clips", "id-1")
                except (tg.TransferBusy, tg.DecisionInFlight) as declined:
                    self.refused.append(("decision", declined.code))
                else:
                    self.won.append(("decision", claim))
            except BaseException as unexpected:          # noqa: BLE001 - recorded, see docstring
                self.failed.append(("decision", repr(unexpected)))
            finally:
                self.finished.append("decision")

        workers = [threading.Thread(target=press if index % 2 else classify, daemon=True)
                   for index in range(self.threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        assert not [w for w in workers if w.is_alive()], "a worker hung"
        return self

    @property
    def kinds(self):
        return [kind for kind, _ in self.won]

    def release(self):
        """Give every successful claim back, so one guard can run many rounds."""
        for kind, claim in self.won:
            if kind == "transfer":
                self.guard.end(claim)
            else:
                self.guard.end_decision(claim)
        self.won = []


def test_a_transfer_and_a_decision_racing_one_owner_never_both_win():
    """The whole correction in one assertion. Both operations test-and-set under the same lock, so
    however 32 threads interleave the pair is mutually exclusive -- which is exactly what the
    original `if not busy: commit()` could not promise.

    [F-3] Rewritten after review. Every worker's outcome is now captured and an unexpected
    exception is a FAILURE rather than a silent abstention: the previous shape passed against a
    guard with no `begin_decision` at all, because the sixteen classifying threads died on
    `AttributeError` and left the winners list holding only transfers. The two tests below are the
    standing proof that this one now discriminates, in both defect directions.
    """
    guard = tg.TransferGuard()
    for attempt in range(40):
        race = _Race(guard).run()

        assert not race.failed, \
            f"attempt {attempt}: a worker raised unexpectedly: {race.failed[:3]}"
        assert len(race.finished) == 32, \
            f"attempt {attempt}: only {len(race.finished)} of 32 workers ran"
        # Decisions do not exclude each other -- only a transfer excludes them -- so a round may
        # legally end with one transfer and no decision, or no transfer and several decisions.
        # What must never happen is the two being held at the same time.
        assert not ("transfer" in race.kinds and "decision" in race.kinds), \
            f"attempt {attempt}: a transfer and a decision both won: {race.kinds}"
        assert race.won, f"attempt {attempt}: nobody won at all"
        # Contention actually happened. With 32 threads at one barrier the losers are what prove
        # the exclusion did something, rather than that it was never exercised.
        assert race.refused, f"attempt {attempt}: no claim was refused, so nothing raced"
        assert all(code in ("transfer_in_progress", "folder_in_progress", "clip_in_progress",
                            "decision_in_flight") for _, code in race.refused), race.refused
        race.release()

    assert guard.deciding() == 0
    assert guard.active_for("tab-a") is None
    assert guard.refused > 0, "the refusal counter never moved, so the race never collided"


def test_the_race_is_not_vacuous_when_the_decision_claim_is_missing():
    """[F-3] Standing proof #1 that the race discriminates: the pre-F-1 shape.

    A guard with no decision claim must make the race FAIL, and fail *for the intended reason* --
    the classifying workers could not claim anything. Before the rewrite this exact configuration
    produced a green test and 1240 unhandled-thread warnings.
    """
    class _NoDecisionClaim(tg.TransferGuard):
        def begin_decision(self, *args, **kwargs):
            raise AttributeError("'TransferGuard' object has no attribute 'begin_decision'")

    race = _Race(_NoDecisionClaim()).run()

    assert len(race.finished) == 32, "a worker was lost instead of having its failure recorded"
    assert race.failed, "a defective guard produced no recorded failure -- still vacuous"
    assert all(kind == "decision" for kind, _ in race.failed), race.failed
    assert "AttributeError" in race.failed[0][1], race.failed[0]
    assert "decision" not in race.kinds, "a claim succeeded against a guard that has none"


def test_the_race_is_not_vacuous_when_the_exclusion_is_removed():
    """[F-3] Standing proof #2, and the one that matters more: a guard that hands out both claims.

    It raises nothing, so the exception bookkeeping cannot catch it -- only the mutual-exclusion
    assertion can. If this mutant never produced an overlap, the race test would be measuring
    thread scheduling rather than the guard.
    """
    class _NoExclusion(tg.TransferGuard):
        """Admission and decision claims that register without consulting anything."""

        def begin(self, owner, generation, signature, root_key, identities=()):
            with self._lock:
                self._token += 1
                record = tg.ActiveTransfer(owner=str(owner), generation=generation,
                                           signature=str(signature), root_key=str(root_key),
                                           token=self._token,
                                           identities=frozenset(identities))
                self._active[str(owner)] = record
                return tg.Reservation(owner=record.owner, generation=generation,
                                      signature=record.signature, root_key=record.root_key,
                                      token=record.token, identities=record.identities)

        def begin_decision(self, owner, root_key='', identity=''):
            with self._lock:
                self._token += 1
                slot = tg.DecisionSlot(owner=str(owner), root_key=str(root_key),
                                       token=self._token, identity=str(identity))
                self._deciding[slot.token] = slot
                return slot

    guard = _NoExclusion()
    overlapped = False
    for _ in range(40):
        race = _Race(guard).run()
        assert not race.failed and len(race.finished) == 32, "the mutant failed for the wrong reason"
        if "transfer" in race.kinds and "decision" in race.kinds:
            overlapped = True
            break
        race.release()
    assert overlapped, \
        "an unguarded implementation never overlapped -- the race cannot detect one"


def test_no_conflicting_claims_ever_overlap_in_time():
    """The INTERVAL property, which is a different statement from "never both won".

    The invariant says *none can start until it is released*, and a race whose claims are never
    released cannot test that half: once the first winner takes the floor every later caller is
    refused for the rest of the round. So here each claim is held for a real interval, released,
    and the round repeats -- and every pair of spans that conflict by the guard's own rules is
    checked for temporal overlap. A lock-protected counter supplies the total order, because wall
    clock readings from several threads cannot be compared safely.
    """
    guard = tg.TransferGuard()
    tick_lock = threading.Lock()
    clock = [0]
    spans = []
    roots = {"tab-a": "k:/clips", "tab-b": "k:/clips"}
    page = ("id-1", "id-2")

    def tick():
        with tick_lock:
            clock[0] += 1
            return clock[0]

    def record(kind, owner, root, opened, closed):
        with tick_lock:
            spans.append((kind, owner, root, opened, closed))

    def transfer(owner):
        for _ in range(25):
            try:
                claim = guard.begin(owner, 1, "sig", roots[owner], page)
            except (tg.TransferBusy, tg.DecisionInFlight):
                continue
            opened = tick()
            closed = tick()
            guard.end(claim)
            record("transfer", owner, roots[owner], opened, closed)

    def decision(owner):
        for _ in range(25):
            try:
                claim = guard.begin_decision(owner, roots[owner], "id-1")
            except (tg.TransferBusy, tg.DecisionInFlight):
                continue
            opened = tick()
            closed = tick()
            guard.end_decision(claim)
            record("decision", owner, roots[owner], opened, closed)

    gate = threading.Barrier(24)

    def worker(target, owner):
        gate.wait(30)
        target(owner)

    owners = ("tab-a", "tab-b")
    threads = [threading.Thread(
        target=worker,
        args=(transfer if index % 4 < 2 else decision, owners[index % 2]), daemon=True)
        for index in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not [t for t in threads if t.is_alive()], "a worker hung"

    assert len(spans) > 50, f"too little contention to mean anything: {len(spans)}"
    overlaps = []
    for index, first in enumerate(spans):
        for second in spans[index + 1:]:
            if first[0] == second[0] == "decision":
                continue                    # decisions deliberately do not exclude each other
            if first[3] < second[4] and second[3] < first[4]:
                overlaps.append((first, second))
    assert not overlaps, f"conflicting claims overlapped: {overlaps[:3]}"
    assert guard.deciding() == 0
    assert all(guard.active_for(owner) is None for owner in owners)


def test_many_owners_may_classify_at_once():
    """Bounded by the requests actually executing, not serialised into one at a time: a decision
    claim is not a global write lock, and sixteen operators on sixteen folders is ordinary."""
    guard = tg.TransferGuard()
    slots = [guard.begin_decision(f"tab-{index}", f"k:/clips{index}") for index in range(16)]
    assert guard.deciding() == 16
    for slot in slots:
        guard.end_decision(slot)
    assert guard.deciding() == 0


# ---- identity-based cross-root exclusion (F-2) ----------------------------------------------
#
# The owner rule asks "whose press is this" and the folder rule asks "which directory". Neither
# asks the only question that decides whether two operations collide: **which clips**. Two review
# sessions that scanned an ancestor and a descendant of one tree have two owners and two resolved
# roots, and the same clip in both batches under the same durable identity -- so a classification
# from the child page was accepted, made durable, and ignored by a sort already running on the
# parent page. The identity set is the exact conflict unit, and these tests are about that.

def test_a_decision_is_refused_when_its_clip_is_in_another_sessions_batch():
    """The F-2 defect, at the authority. Different owner, different root, same clip."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x", "id-y"))
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-b", "k:/parent/child", "id-x")
    assert refused.value.code == "clip_in_progress"
    assert refused.value.active.owner == "tab-a", "the refusal must name the transfer that caused it"
    assert guard.deciding() == 0, "a refused claim must register nothing"


def test_a_decision_on_a_clip_outside_that_batch_is_still_admitted():
    """Exclusion is per clip, not per tree. A child folder's OTHER clips stay classifiable."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))
    assert guard.begin_decision("tab-b", "k:/parent/child", "id-z").identity == "id-z"


def test_admission_is_refused_while_a_cross_root_decision_on_one_of_its_clips_is_in_flight():
    """The straddle, in the direction a decision-in-flight holds no `ActiveTransfer` to scan.

    `_conflicting_transfer` cannot see it -- there is no transfer -- so `begin` has to compare the
    pending slot's identity against the page it is about to claim. Without that half, the decision
    becomes durable a moment after admission and the batch moves by the snapshot it took before.
    """
    guard = tg.TransferGuard()
    guard.begin_decision("tab-b", "k:/parent/child", "id-x")
    with pytest.raises(tg.DecisionInFlight):
        guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x", "id-y"))
    assert guard.active_for("tab-a") is None


def test_an_in_flight_decision_on_an_unrelated_clip_does_not_delay_admission():
    guard = tg.TransferGuard()
    guard.begin_decision("tab-b", "k:/elsewhere", "id-q")
    assert guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",)).owner == "tab-a"


def test_two_transfers_over_an_overlapping_batch_cannot_run_at_once():
    """The hazard the owner/folder pair never excluded either: two sessions moving one file.

    Strictly worse than the classification case, because both would call `os.rename` on the same
    path -- one of them finding it already gone.
    """
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "k:/parent", ("id-x", "id-y"))
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin("tab-b", 1, "sig-b", "k:/parent/child", ("id-y",))
    assert refused.value.code == "clip_in_progress"


def test_two_transfers_over_disjoint_batches_remain_independent():
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig-a", "k:/one", ("id-x",))
    assert guard.begin("tab-b", 1, "sig-b", "k:/two", ("id-z",)).owner == "tab-b"


def test_the_identity_rule_still_applies_when_neither_root_resolved():
    """`_clips_transfer_key` answers `''` for a path it cannot resolve, which switches the folder
    rule off. The identity is still known, so the clip is still protected -- this is the case the
    folder rule was never able to cover at all."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "", ("id-x",))
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-b", "", "id-x")
    assert refused.value.code == "clip_in_progress"
    assert guard.begin_decision("tab-b", "", "id-z").owner == "tab-b", \
        "an unresolved root must not make every clip busy"


def test_an_empty_identity_set_disables_only_the_identity_rule():
    """The same fail-safe shape `root_key` has: an unknown page is not evidence about any clip,
    and every pre-F-2 four-argument caller keeps its exact behaviour."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent")              # no identities supplied
    assert guard.active_for("tab-a").identities == frozenset()
    # the owner rule is untouched
    with pytest.raises(tg.TransferBusy) as refused:
        guard.begin_decision("tab-a", "k:/parent", "id-x")
    assert refused.value.code == "transfer_in_progress"
    # the folder rule is untouched
    with pytest.raises(tg.TransferBusy) as folder:
        guard.begin_decision("tab-b", "k:/parent", "id-x")
    assert folder.value.code == "folder_in_progress"
    # and with nothing recorded, a cross-root clip claim is admitted exactly as it was pre-F-2
    assert guard.begin_decision("tab-b", "k:/parent/child", "id-x").identity == "id-x"


def test_the_refusal_order_is_most_specific_first():
    """Each code must be the most specific true statement: an operator told "another session is
    sorting this folder" about their own press goes looking for a click they never made."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))
    # self-collision, even though the folder and the clip also match
    with pytest.raises(tg.TransferBusy) as mine:
        guard.begin_decision("tab-a", "k:/parent", "id-x")
    assert mine.value.code == "transfer_in_progress"
    # folder collision, even though the clip also matches
    with pytest.raises(tg.TransferBusy) as folder:
        guard.begin_decision("tab-b", "k:/parent", "id-x")
    assert folder.value.code == "folder_in_progress"
    # only when neither of those applies is it reported as a clip overlap
    with pytest.raises(tg.TransferBusy) as clip:
        guard.begin_decision("tab-b", "k:/parent/child", "id-x")
    assert clip.value.code == "clip_in_progress"


def test_the_claimed_identity_set_is_frozen_against_the_callers_list():
    """The conflict decision was made against this set, so the record must not be able to change
    under it. A list the caller keeps appending to would let a transfer silently widen its own
    exclusion after admission -- or narrow it."""
    guard = tg.TransferGuard()
    page = ["id-x"]
    reservation = guard.begin("tab-a", 1, "sig", "k:/parent", page)
    page.append("id-z")

    assert isinstance(reservation.identities, frozenset)
    assert reservation.identities == frozenset({"id-x"})
    assert guard.active_for("tab-a").identities == frozenset({"id-x"})
    assert guard.begin_decision("tab-b", "k:/other", "id-z").identity == "id-z", \
        "the record grew after admission"


def test_progress_updates_do_not_disturb_the_claimed_identities():
    guard = tg.TransferGuard()
    reservation = guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))
    assert guard.note_progress(reservation, 3, 9) is True
    record = guard.active_for("tab-a")
    assert record.identities == frozenset({"id-x"}) and (record.done, record.total) == (3, 9)


def test_releasing_a_transfer_frees_its_clips_immediately():
    """A window, not a latch -- in the identity register as well."""
    guard = tg.TransferGuard()
    reservation = guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))
    with pytest.raises(tg.TransferBusy):
        guard.begin_decision("tab-b", "k:/parent/child", "id-x")
    assert guard.end(reservation) is True
    assert guard.begin_decision("tab-b", "k:/parent/child", "id-x").identity == "id-x"


def test_the_advisory_conflict_read_reports_the_guards_own_reason():
    """`conflict()` exists so the screen cannot keep its own copy of the rules. It must answer
    with the same code the claim would raise, and must not claim anything itself."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))

    assert guard.conflict("tab-a", "k:/parent", ("id-x",))[0] == "transfer_in_progress"
    assert guard.conflict("tab-b", "k:/parent", ("id-x",))[0] == "folder_in_progress"
    assert guard.conflict("tab-b", "k:/parent/child", ("id-x",))[0] == "clip_in_progress"
    assert guard.conflict("tab-b", "k:/elsewhere", ("id-z",)) is None
    assert guard.conflict("tab-b", "k:/parent/child") is None, \
        "without a page the identity rule must stay off"
    assert guard.deciding() == 0 and guard.refused == 0, "an advisory read claimed or counted"


def test_busy_without_a_page_answers_exactly_as_it_did_before_f2():
    """`busy()` gained an optional third argument. Every existing caller passes two, and must get
    the pre-F-2 answer: this owner's transfer, or one on this exact folder."""
    guard = tg.TransferGuard()
    guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x",))
    assert guard.busy("tab-a") is not None
    assert guard.busy("tab-b", "k:/parent") is not None
    assert guard.busy("tab-b", "k:/parent/child") is None
    assert guard.busy("tab-b") is None
    # and with a page it also answers for the clip
    assert guard.busy("tab-b", "k:/parent/child", ("id-x",)) is not None


def test_many_overlapping_claims_leak_nothing():
    guard = tg.TransferGuard()
    for _ in range(300):
        reservation = guard.begin("tab-a", 1, "sig", "k:/parent", ("id-x", "id-y"))
        with pytest.raises(tg.TransferBusy):
            guard.begin_decision("tab-b", "k:/child", "id-y")
        slot = guard.begin_decision("tab-b", "k:/child", "id-q")
        assert guard.end_decision(slot) is True
        assert guard.end(reservation) is True
    assert guard.deciding() == 0
    assert guard.active_for("tab-a") is None and guard.active_for("tab-b") is None


# ---- the progress-frame budget -------------------------------------------------------------

def test_a_small_batch_reports_every_clip():
    for total in (1, 2, 7, tg.PROGRESS_FRAME_BUDGET):
        assert tg.progress_stride(total) == 1
        emitted = [done for done in range(1, total + 1) if tg.should_emit(done, total)]
        assert emitted == list(range(1, total + 1)), total


def test_a_large_batch_is_bounded_but_always_ends_on_the_truth():
    for total in (84, 500, 1000, 12345):
        emitted = [done for done in range(1, total + 1) if tg.should_emit(done, total)]
        assert len(emitted) <= tg.PROGRESS_FRAME_BUDGET + 1, (total, len(emitted))
        assert emitted[-1] == total, \
            "the last outcome must emit, or the closing figure is a rounded stride"


def test_the_first_frame_is_not_claimed_by_the_progress_rule():
    """`done == 0` is the Working frame's own job, emitted before anything moved. The progress
    rule speaks only about recorded outcomes."""
    assert tg.should_emit(0, 84) is False
    assert tg.should_emit(0, 0) is True, "an empty batch has nothing left to report"


def test_a_negative_or_zero_total_never_hangs_the_rule():
    for total in (0, -1):
        assert tg.should_emit(0, total) is True
        assert tg.should_emit(5, total) is True


def _imported_modules() -> set[str]:
    """Top-level module names ``transfer_guard`` imports, read off the syntax tree."""
    tree = ast.parse(inspect.getsource(tg))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _called_names() -> set[str]:
    """Every name this module actually *calls*, dotted form included.

    Read off the syntax tree rather than scanned for as substrings, deliberately. This repository
    has tripped its own bans five times by writing the banned token in a comment explaining the
    ban -- and both guards below are about tokens whose explanation naturally contains them.
    """
    tree = ast.parse(inspect.getsource(tg))
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name):
            called.add(target.id)
        elif isinstance(target, ast.Attribute):
            called.add(ast.unparse(target))
            called.add(target.attr)
    return called


def test_the_emission_rule_reads_no_clock():
    """A throttle driven by elapsed seconds would make the frame sequence depend on how fast the
    disk is, which is precisely the unverifiable progress R13 exists to replace. Asserted
    structurally, because the absence of a clock cannot be observed from the return values."""
    assert "time" not in _imported_modules()
    assert "datetime" not in _imported_modules()
    assert "random" not in _imported_modules()
    for forbidden in ("perf_counter", "monotonic", "now", "sleep"):
        assert forbidden not in _called_names(), f"the emission rule calls {forbidden}"


def test_the_guard_touches_no_filesystem_and_starts_nothing():
    """It is a policy object. A path operation here would mean two places decide whether two
    spellings name one folder -- the caller normalises, and this module deliberately cannot. A
    thread or a process would mean the admission authority could outlive the request that took
    it."""
    imported = _imported_modules()
    for forbidden in ("os", "shutil", "pathlib", "subprocess", "tempfile"):
        assert forbidden not in imported, f"transfer_guard imports {forbidden}"
    assert imported <= {"__future__", "threading", "dataclasses", "typing"}, imported

    called = _called_names()
    for forbidden in ("open", "rename", "replace", "makedirs", "mkdir", "unlink", "remove",
                      "rmtree", "Popen", "run", "Thread", "start"):
        assert forbidden not in called, f"transfer_guard calls {forbidden}"


def test_the_only_synchronisation_primitive_is_one_plain_lock():
    """One lock, and a plain `Lock` rather than an `RLock`: nothing here re-enters, and an
    `RLock` would quietly permit a nested acquisition that ought to be a visible design
    question."""
    tree = ast.parse(inspect.getsource(tg))
    constructed = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)
                   and ast.unparse(n.func).startswith("threading.")]
    assert constructed == ["threading.Lock"], constructed
