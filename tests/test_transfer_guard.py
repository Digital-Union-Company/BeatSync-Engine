"""R13 single-flight: the admission rule, executed rather than asserted about.

[FORK] Digital-Union. ``beatsync_fork.transfer_guard`` imports nothing but the stdlib, so every
rule in it runs here on a bare interpreter -- which is the whole point of putting the authority in
a pure module instead of inside the Gradio handler. The handler's use of it is
``test_clips_transfer_overlay.py``.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import threading
import time

import pytest

from beatsync_fork import transfer_guard as tg

# `time` and `contextlib` are used by the interval-property harness below, and only there. The
# "no clock" and "no filesystem" guards at the end of this file read `inspect.getsource(tg)` --
# the GUARD module -- not this file, so a test-side clock cannot weaken them: the ban is on
# `transfer_guard` reading a clock to decide a *frame*, not on a test holding a claim open for a
# measurable interval.


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


# =============================================================================================
# The INTERVAL property -- rebuilt after review finding F-6
# =============================================================================================
#
# [F-6] The first version of this section was VACUOUS, and it was shipped and documented as the
# proof of the invariant's second half. Independent review measured its catch rate against four
# defective guards and got **0/40 on every one of them**, including a guard with no exclusion
# whatsoever and the pre-F-1 guard whose twelve classifying workers all died on an uncaught
# `AttributeError` while the test still passed.
#
# The cause was one line too few. It did this:
#
#     opened = tick()
#     closed = tick()          # <- nothing in between
#
# with both ticks serialised by the same lock, so every recorded span was a pair of **adjacent
# integers** and the overlap predicate `a.opened < b.closed and b.opened < a.closed` could
# essentially never be true. Measured by replaying that span algebra with no guard involved at
# all: 300 of 300 spans zero-width, **0 overlaps observable**. The `len(spans) > 50` floor did
# not catch the dead-worker case either, because the transfer half alone clears 50.
#
# So there was no interval. What follows measures one, and the correctness argument is
# **coordination, not sleeping**: an Event handshake puts the conflicting admission attempt
# strictly inside a held claim, so on a defective guard the second span is *structurally* nested
# in the first and the overlap is not a matter of scheduling luck. A short bounded sleep is added
# on top, and is deliberately never the only thing making the window real.
#
# Four standing not-vacuous proofs follow the test, one per defect direction, and two of them
# (M3, M4) use **different owners and different root keys** on purpose: a probe that relied on a
# same-owner or same-root collision would be refused by rules 1 and 2 and would mask exactly the
# rules it is meant to be testing.


class _IntervalLog:
    """Logical spans with a lock-protected total order, plus full worker bookkeeping.

    The clock is logical rather than wall-clock because readings taken on several threads cannot
    be ordered safely against one another. Every mutation goes through the one lock, so `spans`
    and the four outcome lists are consistent without depending on `list.append` being atomic.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._clock = 0
        self.spans = []
        self.admitted = []
        self.refused = []
        self.failed = []
        self.finished = []

    def tick(self) -> int:
        with self._lock:
            self._clock += 1
            return self._clock

    def record_span(self, kind, owner, root_key, identities, opened, closed):
        with self._lock:
            self.spans.append({"kind": kind, "owner": owner, "root_key": root_key,
                               "identities": frozenset(identities),
                               "opened": opened, "closed": closed})

    def note(self, bucket, value):
        with self._lock:
            getattr(self, bucket).append(value)

    def note_refusal(self, label, who, code):
        """A refusal is an EVENT IN THE TOTAL ORDER, and stamping it is what makes this harness
        falsifiable.

        [F-6] The logical clock only advances when something happens to a claim. A refusal that
        did not tick left the holder's span spanning no events at all -- width 1 -- so there was
        nothing for an overlap or an enclosure check to see even though a real conflict had just
        been met inside a real interval. Stamping the refusal means the holder's span provably
        *contains* the moment its conflict was refused.
        """
        with self._lock:
            self._clock += 1
            self.refused.append({"label": label, "who": who, "code": code, "at": self._clock})

    def refusals_inside(self, span) -> list:
        """The refusals stamped strictly between this span's open and close ticks.

        Takes the lock like every other accessor here. In practice it is only ever called after
        the workers have been joined, but the class's whole claim is that one lock covers its
        state, and a reader that quietly opted out would be the next person's trap.
        """
        with self._lock:
            return [r for r in self.refused if span["opened"] < r["at"] < span["closed"]]


class _Latch:
    """A countdown every worker arrives at, so a holder can wait for *everybody's outcome*.

    This is what makes the contended phase deterministic: the winner does not hold its claim for
    an arbitrary time, it holds it until all the other presses have actually been through
    admission and reported. Their refusals are therefore known to have happened while the
    winner's span was open.
    """

    def __init__(self, count: int):
        self._remaining = count
        self._lock = threading.Lock()
        self._done = threading.Event()

    def arrive(self):
        with self._lock:
            self._remaining -= 1
            if self._remaining <= 0:
                self._done.set()

    def wait(self, timeout):
        return self._done.wait(timeout)


def _spans_conflict(first: dict, second: dict) -> bool:
    """Whether two recorded spans are a pair the guard's own rules forbid holding at once.

    This is deliberately the guard's rule set restated over spans -- owner, then folder, then
    identity overlap -- rather than a looser "any two claims". Two things follow, and both matter:

    * **decision vs decision is never a violation.** Concurrent classifications of one clip are
      permitted by design and are serialised by `ReviewStore.decide`'s own lock with an
      optimistic revision check, so flagging them would make this test assert something false.
    * **a bare identity overlap across two owners and two roots IS a violation**, which is the
      F-2 rule and the reason M3 below can be detected at all.
    """
    if first["kind"] == second["kind"] == "decision":
        return False
    if first["owner"] == second["owner"]:
        return True
    if first["root_key"] and first["root_key"] == second["root_key"]:
        return True
    return not first["identities"].isdisjoint(second["identities"])


def _overlapping(first: dict, second: dict) -> bool:
    """Strictly overlapping logical intervals.

    Strict on both sides, so two *sequential* claims -- the second opening at or after the first
    closed -- are never mistaken for a violation. That is the normal, correct outcome and the
    whole file would be noise if it counted.
    """
    return first["opened"] < second["closed"] and second["opened"] < first["closed"]


@contextlib.contextmanager
def _hold_transfer(guard, log, owner, root_key, identities):
    """Admit a transfer, hold it for a measurable logical interval, release it in `finally`.

    If `begin` raises there is nothing to release and nothing to record: the exception propagates
    to the caller, which records a refusal. The span is recorded *after* the close tick and the
    release, so a span exists only for a claim that was genuinely held.
    """
    claim = guard.begin(owner, 1, "sig", root_key, identities)
    opened = log.tick()
    try:
        yield claim
    finally:
        closed = log.tick()
        try:
            guard.end(claim)
        finally:
            log.record_span("transfer", owner, root_key, identities, opened, closed)


@contextlib.contextmanager
def _hold_decision(guard, log, owner, root_key, identity):
    """The same, for one durable decision slot."""
    claim = guard.begin_decision(owner, root_key, identity)
    opened = log.tick()
    try:
        yield claim
    finally:
        closed = log.tick()
        try:
            guard.end_decision(claim)
        finally:
            log.record_span("decision", owner, root_key, (identity,), opened, closed)


#: One tree, scanned by two sessions as an ancestor and a descendant. Two owners, two root keys,
#: one shared durable identity -- the configuration that rules 1 and 2 are both blind to.
_OWNER_PARENT, _OWNER_CHILD = "tab-parent", "tab-child"
_ROOT_PARENT, _ROOT_CHILD = "k:/clips", "k:/clips/sub"
_SHARED = "id-shared"
_PARENT_PAGE = (_SHARED, "id-parent-only")


def _probe_attack_inside_a_held_claim(guard, log, hold, attack, label):
    """Run `attack` strictly INSIDE the interval `hold` is holding, by Event handshake.

    Two Events and no sleeping: the holder publishes `holding` once its claim is open, the
    attacker waits for that before trying, and the holder does not close until the attacker has
    published `attacked`. So on a guard that wrongly admits the attacker, the attacker's span is
    nested inside the holder's and the overlap is structural.

    Every worker's exception is captured, and both Events are set on every path -- including the
    failure paths -- so a defective guard produces a recorded failure rather than a hung test.
    """
    holding, attacked = threading.Event(), threading.Event()

    def _holder():
        try:
            with hold(guard, log):
                log.note("admitted", (label, "holder"))
                holding.set()
                # A real interval, held open until the attacker has finished trying. The sleep is
                # supplementary: the handshake is what makes the window certain.
                time.sleep(0.001)
                if not attacked.wait(30):
                    log.note("failed", (label, "holder", "the attacker never reported"))
        except (tg.TransferBusy, tg.DecisionInFlight) as declined:
            log.note_refusal(label, "holder", declined.code)
        except BaseException as crash:               # noqa: BLE001 - a dead worker is a failure
            log.note("failed", (label, "holder", repr(crash)))
        finally:
            holding.set()                            # never leave the attacker waiting
            log.note("finished", (label, "holder"))

    def _attacker():
        try:
            if not holding.wait(30):
                log.note("failed", (label, "attacker", "the holder never reported"))
                return
            attack(guard, log)
        except BaseException as crash:               # noqa: BLE001 - a dead worker is a failure
            log.note("failed", (label, "attacker", repr(crash)))
        finally:
            attacked.set()                           # never leave the holder waiting
            log.note("finished", (label, "attacker"))

    threads = [threading.Thread(target=_holder, daemon=True),
               threading.Thread(target=_attacker, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return [t for t in threads if t.is_alive()]


def _attack_with_a_cross_root_transfer(guard, log):
    """A second SORT of the shared clip, arriving under another owner and another folder."""
    try:
        with _hold_transfer(guard, log, _OWNER_CHILD, _ROOT_CHILD, (_SHARED,)):
            log.note("admitted", ("cross-root transfer", "attacker"))
            time.sleep(0.001)
    except (tg.TransferBusy, tg.DecisionInFlight) as declined:
        log.note_refusal("cross-root transfer", "attacker", declined.code)


def _attack_with_a_cross_root_decision(guard, log):
    """A CLASSIFICATION of the shared clip, arriving under another owner and another folder."""
    try:
        with _hold_decision(guard, log, _OWNER_CHILD, _ROOT_CHILD, _SHARED):
            log.note("admitted", ("cross-root decision", "attacker"))
            time.sleep(0.001)
    except (tg.TransferBusy, tg.DecisionInFlight) as declined:
        log.note_refusal("cross-root decision", "attacker", declined.code)


def _attack_with_a_transfer_over_a_pending_decision(guard, log):
    """The reverse direction: admission attempted while a cross-root decision is in flight.

    This is the only probe that can see `begin` skipping the pending-decision map, because a
    decision in flight holds no `ActiveTransfer` for the identity rule to find.
    """
    try:
        with _hold_transfer(guard, log, _OWNER_PARENT, _ROOT_PARENT, _PARENT_PAGE):
            log.note("admitted", ("transfer over a pending decision", "attacker"))
            time.sleep(0.001)
    except (tg.TransferBusy, tg.DecisionInFlight) as declined:
        log.note_refusal("transfer over a pending decision", "attacker", declined.code)


def _attack_with_a_second_classification(guard, log):
    """A control, not an attack: a second classification of the shared clip under a third owner.

    It MUST be admitted and MAY overlap, because concurrent classifications are permitted. It is
    here so the test is known to distinguish a real violation from legitimate concurrency rather
    than merely never meeting any.
    """
    try:
        with _hold_decision(guard, log, "tab-third", "k:/elsewhere", _SHARED):
            log.note("admitted", ("concurrent classification", "attacker"))
            time.sleep(0.001)
    except (tg.TransferBusy, tg.DecisionInFlight) as declined:
        log.note_refusal("concurrent classification", "attacker", declined.code)


def _contended_phase(guard, log, presses=24, rounds=3):
    """Many sessions, each its OWN owner and OWN root key, all over one shared page.

    Deterministic by construction rather than by timing: a barrier makes every press attempt
    admission together, and the winner then holds its claim until a latch confirms **every** press
    has been through admission and recorded an outcome. The refusals are therefore known to have
    occurred inside the winner's open span.
    """
    for _ in range(rounds):
        gate = threading.Barrier(presses)
        latch = _Latch(presses)

        def _press(index):
            owner, root = f"tab-{index:02d}", f"k:/root-{index:02d}"
            try:
                gate.wait(30)
                try:
                    with _hold_transfer(guard, log, owner, root, _PARENT_PAGE):
                        log.note("admitted", ("contended", owner))
                        latch.arrive()               # report BEFORE waiting, or this deadlocks
                        latch.wait(30)
                        time.sleep(0.001)
                except (tg.TransferBusy, tg.DecisionInFlight) as declined:
                    log.note_refusal("contended", owner, declined.code)
                    latch.arrive()
            except BaseException as crash:           # noqa: BLE001 - a dead worker is a failure
                log.note("failed", ("contended", owner, repr(crash)))
                latch.arrive()
            finally:
                log.note("finished", ("contended", owner))

        threads = [threading.Thread(target=_press, args=(index,), daemon=True)
                   for index in range(presses)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        alive = [t for t in threads if t.is_alive()]
        assert not alive, f"{len(alive)} contended presses hung"


def _hold_parent_transfer(guard, log):
    """The PARENT session sorting its page, which contains the shared clip."""
    return _hold_transfer(guard, log, _OWNER_PARENT, _ROOT_PARENT, _PARENT_PAGE)


def _hold_child_decision(guard, log):
    """The CHILD session classifying the shared clip, under its own folder."""
    return _hold_decision(guard, log, _OWNER_CHILD, _ROOT_CHILD, _SHARED)


def _measure_interval_property(guard):
    """Measure the interval half of the invariant, and raise `AssertionError` on any violation.

    Factored out so the test below and the four standing not-vacuous proofs after it run **the
    same** measurement -- a control that exercised a different code path would prove nothing about
    the test it is supposed to be validating. Returns the log so a caller can inspect it.

    Assertion order is load-bearing, and was chosen by running it against all four mutants:

    1. **a dead worker first**, so a missing-API guard fails on its captured exception rather than
       slipping through to an overlap check with nothing to compare;
    2. then the worker census;
    3. then **the property itself**, so a guard that refuses nothing at all fails on the real
       overlap rather than on "nothing was refused" -- the latter is true of it, but it is the
       weaker statement and names the wrong defect;
    4. and only then the positive evidence that the harness actually met a conflict, which is
       what stops THIS function from ever becoming vacuous again.
    """
    log = _IntervalLog()
    probes = [
        (_hold_parent_transfer, _attack_with_a_cross_root_transfer, "cross-root transfer"),
        (_hold_parent_transfer, _attack_with_a_cross_root_decision, "cross-root decision"),
        (_hold_child_decision, _attack_with_a_transfer_over_a_pending_decision,
         "transfer over a pending decision"),
        # A CONTROL, not an attack, and deliberately decision-against-decision: a classification
        # arriving while that clip is being SORTED is correctly refused, so pairing this with a
        # transfer holder would assert the opposite of the contract. Two classifications of one
        # clip are permitted, must both be admitted, and their spans MUST overlap without being
        # flagged -- which is what proves `_spans_conflict` tells the two cases apart.
        (_hold_child_decision, _attack_with_a_second_classification,
         "concurrent classification (control)"),
    ]
    for hold, attack, label in probes:
        hung = _probe_attack_inside_a_held_claim(guard, log, hold, attack, label)
        assert not hung, f"{label}: a worker hung"
    _contended_phase(guard, log)

    # 1 -- no worker may die. [F-6] The old version had no such list at all, which is how it
    # passed against a guard whose decision API did not exist.
    assert not log.failed, f"a worker raised unexpectedly: {log.failed[:3]}"

    # 2 -- every worker reached the end of its body. 4 probes x 2 workers + 3 rounds x 24 presses.
    expected_workers = len(probes) * 2 + 3 * 24
    assert len(log.finished) == expected_workers, \
        f"only {len(log.finished)} of {expected_workers} workers finished"

    # 3 -- THE PROPERTY. Every pair the guard's own rules forbid holding at once, checked for
    # strict temporal overlap. Strict on both sides, so sequential claims are never miscounted.
    assert log.spans, "no claim was ever held, so there was nothing to measure"
    overlaps = [(first, second)
                for index, first in enumerate(log.spans)
                for second in log.spans[index + 1:]
                if _spans_conflict(first, second) and _overlapping(first, second)]
    assert not overlaps, f"conflicting claims overlapped in time: {overlaps[:3]}"

    # 4 -- the harness actually met a conflict, INSIDE a held interval. [F-6] This is the part
    # the old version had no way of knowing: it is not enough that no overlap was found, the
    # window has to have been open while a conflicting claim was attempted. A refusal stamps the
    # logical clock, so a holder's span strictly ENCLOSING a refusal is direct evidence that the
    # attempt happened during the hold rather than before or after it.
    assert log.admitted, "nothing was ever admitted, so no claim was ever held"
    assert log.refused, "nothing was ever refused, so no conflict was ever met"
    enclosing = [span for span in log.spans if log.refusals_inside(span)]
    assert enclosing, (
        "no held claim enclosed a single refusal -- every conflicting attempt happened outside "
        "the window, so this measurement would pass against an unguarded guard. That is F-6.")
    assert max(len(log.refusals_inside(span)) for span in enclosing) >= 20, (
        "the contended round's refusals did not land inside the winner's span: "
        f"{[len(log.refusals_inside(s)) for s in enclosing]}")

    # 5 -- nothing outstanding, so no span was left open and no claim leaked.
    assert guard.deciding() == 0, "a decision slot leaked"
    leaked = [s["owner"] for s in log.spans if guard.active_for(s["owner"]) is not None]
    assert not leaked, f"a reservation leaked: {leaked}"
    return log


def test_no_conflicting_claims_ever_overlap_in_time():
    """The INTERVAL property, which is a different statement from "never both won".

    The invariant says *none can start until it is released*. A race whose claims are never
    released cannot test that half -- once the first winner takes the floor every later caller is
    refused for the rest of the round -- so this holds each claim open for a measurable logical
    interval and checks every conflicting pair of spans for temporal overlap.

    [F-6] Rewritten after review. The previous version ticked its two boundaries back to back, so
    every span was zero width and the overlap check could not fire: measured at **0/40 against
    four defective guards**, one of which had no exclusion at all. The four proofs below are the
    standing evidence that this version discriminates, and the enclosure assertion inside
    `_measure_interval_property` is what stops the defect returning silently -- a window that was
    never open while a conflict was attempted now fails loudly instead of reporting success.
    """
    log = _measure_interval_property(tg.TransferGuard())

    # Positive evidence, so a future regression cannot make this pass by measuring nothing.
    assert len(log.spans) >= 5, f"too few held claims to mean anything: {len(log.spans)}"
    assert len(log.refused) >= 24, f"too little contention: {len(log.refused)} refusals"
    kinds = {span["kind"] for span in log.spans}
    assert kinds == {"transfer", "decision"}, f"one kind of claim was never held: {kinds}"
    # The permitted-concurrency control really was permitted, so the identity rule is not a
    # blanket freeze on classification: two sessions may classify one clip at the same time, and
    # `ReviewStore.decide`'s own lock and revision check are what order them.
    assert any(label == "concurrent classification" for label, *_ in log.admitted), \
        "a second classification of one clip was refused -- that is not the contract"
    # And the two overlapping decision spans were NOT reported as a violation, which is the
    # discrimination this harness would be worthless without.
    decisions = [span for span in log.spans if span["kind"] == "decision"]
    assert any(_overlapping(first, second)
               for index, first in enumerate(decisions)
               for second in decisions[index + 1:]), \
        "no two classifications ever overlapped, so the permitted case was never exercised"


# ---- [F-6] the four standing proofs that the interval test is not vacuous --------------------
#
# Each mutant is a subclass, so no production source is modified. Each proof asserts the interval
# test FAILS **and** that it failed for the intended reason, because a control that only checks
# "it failed" would be satisfied by a typo.


class _M1NoExclusionAtAll(tg.TransferGuard):
    """Both claims granted without consulting anything. Raises nothing, so only the overlap check
    can catch it."""

    def begin(self, owner, generation, signature, root_key, identities=()):
        with self._lock:
            self._token += 1
            record = tg.ActiveTransfer(owner=str(owner), generation=generation,
                                       signature=str(signature), root_key=str(root_key),
                                       token=self._token, identities=frozenset(identities))
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


class _M2MissingBeginDecision(tg.TransferGuard):
    """The pre-F-1 shape: the decision API does not exist."""

    def begin_decision(self, *args, **kwargs):
        raise AttributeError("'TransferGuard' object has no attribute 'begin_decision'")


class _M3NoIdentityOverlapRule(tg.TransferGuard):
    """F-2 undone. The owner and folder rules only, with the API shape kept so nothing raises."""

    def _conflicting_transfer(self, owner, root_key, identities=frozenset()):
        mine = self._active.get(owner)
        if mine is not None:
            return "transfer_in_progress", mine
        if root_key:
            for other in self._active.values():
                if other.root_key == root_key:
                    return "folder_in_progress", other
        return None


class _M4AdmissionIgnoresPendingDecisions(tg.TransferGuard):
    """F-1's other half undone: `begin` consults `_active` but never `_deciding`."""

    def begin(self, owner, generation, signature, root_key, identities=()):
        key = str(owner)
        root = str(root_key or '')
        claimed = frozenset(identities)
        with self._lock:
            conflict = self._conflicting_transfer(key, root, claimed)
            if conflict is not None:
                self.refused += 1
                raise tg.TransferBusy(*conflict)
            self._token += 1
            record = tg.ActiveTransfer(owner=key, generation=generation,
                                       signature=str(signature or ''),
                                       root_key=str(root_key or ''), token=self._token,
                                       identities=claimed)
            self._active[key] = record
            return tg.Reservation(owner=key, generation=generation, signature=record.signature,
                                  root_key=record.root_key, token=record.token,
                                  identities=claimed)


def _interval_failure_for(mutant):
    """Run the interval measurement against `mutant` and return the assertion it raised."""
    with pytest.raises(AssertionError) as failure:
        _measure_interval_property(mutant())
    return str(failure.value)


def test_the_interval_test_is_not_vacuous_when_there_is_no_exclusion_at_all():
    """M1. The mutant raises nothing, so the exception bookkeeping cannot catch it -- only a real
    overlap of two real intervals can, which is precisely what F-6 was unable to observe."""
    reason = _interval_failure_for(_M1NoExclusionAtAll)
    assert "conflicting claims overlapped in time" in reason, reason


def test_the_interval_test_is_not_vacuous_when_the_decision_claim_is_missing():
    """M2. It must fail on a CAPTURED worker exception, not pass quietly because the classifying
    workers never acquired anything -- that silent abstention was the whole of F-3 and F-6."""
    reason = _interval_failure_for(_M2MissingBeginDecision)
    assert "a worker raised unexpectedly" in reason, reason
    assert "AttributeError" in reason, reason


def test_the_interval_test_is_not_vacuous_when_the_identity_rule_is_removed():
    """M3, and the probe is deliberately cross-owner AND cross-root.

    Had it leaned on a same-owner or same-folder collision, rules 1 and 2 would have refused the
    attacker and the missing identity rule would have been invisible -- the mask that let F-2 ship
    in the first place.
    """
    reason = _interval_failure_for(_M3NoIdentityOverlapRule)
    assert "conflicting claims overlapped in time" in reason, reason


def test_the_interval_test_is_not_vacuous_when_admission_ignores_pending_decisions():
    """M4. A decision claim is held under the CHILD owner and the CHILD root while the PARENT
    owner attempts a transfer on the parent root over the same identity. Nothing is in `_active`,
    so only the pending-decision map can refuse it -- and this mutant does not look."""
    reason = _interval_failure_for(_M4AdmissionIgnoresPendingDecisions)
    assert "conflicting claims overlapped in time" in reason, reason


def test_the_interval_probe_really_does_place_the_attack_inside_the_hold():
    """The handshake, asserted directly rather than trusted.

    If the attacker ever ran before or after the hold, the probes above would be measuring
    sequential claims and M1 would escape. On the unguarded mutant the attacker IS admitted, so
    its span must be strictly nested inside the holder's.
    """
    log = _IntervalLog()
    guard = _M1NoExclusionAtAll()
    hung = _probe_attack_inside_a_held_claim(
        guard, log, _hold_parent_transfer, _attack_with_a_cross_root_transfer, "nesting")
    assert not hung and not log.failed, log.failed
    assert len(log.spans) == 2, log.spans
    attacker = next(s for s in log.spans if s["owner"] == _OWNER_CHILD)
    holder = next(s for s in log.spans if s["owner"] == _OWNER_PARENT)
    assert holder["opened"] < attacker["opened"] < attacker["closed"] < holder["closed"], \
        f"the attack was not inside the hold: holder={holder} attacker={attacker}"
    assert _spans_conflict(holder, attacker) and _overlapping(holder, attacker)


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
