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


def test_a_transfer_and_a_decision_racing_one_owner_never_both_win():
    """The whole correction in one assertion. Both operations test-and-set under the same lock, so
    however 32 threads interleave the pair is mutually exclusive -- which is exactly what the
    original `if not busy: commit()` could not promise."""
    for attempt in range(40):
        guard = tg.TransferGuard()
        won = []
        start = threading.Barrier(32)

        def press():
            start.wait()
            try:
                guard.begin("tab-a", 1, "sig", "k:/clips")
                won.append("transfer")
            except (tg.TransferBusy, tg.DecisionInFlight):
                pass

        def classify():
            start.wait()
            try:
                guard.begin_decision("tab-a", "k:/clips")
                won.append("decision")
            except tg.TransferBusy:
                pass

        threads = [threading.Thread(target=press if index % 2 else classify)
                   for index in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Decisions do not exclude each other -- only a transfer excludes them -- so the legal
        # outcomes are "one transfer and no decision" or "no transfer and any number of
        # decisions". What must never happen is a transfer alongside a decision.
        assert not ("transfer" in won and "decision" in won), \
            f"attempt {attempt}: a transfer and a decision both won: {won}"
        assert won.count("transfer") <= 1, f"attempt {attempt}: {won}"
        assert won, f"attempt {attempt}: nobody won at all"


def test_many_owners_may_classify_at_once():
    """Bounded by the requests actually executing, not serialised into one at a time: a decision
    claim is not a global write lock, and sixteen operators on sixteen folders is ordinary."""
    guard = tg.TransferGuard()
    slots = [guard.begin_decision(f"tab-{index}", f"k:/clips{index}") for index in range(16)]
    assert guard.deciding() == 16
    for slot in slots:
        guard.end_decision(slot)
    assert guard.deciding() == 0


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
