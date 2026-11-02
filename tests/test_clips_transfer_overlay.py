"""R13 Video Review Transfer Working Overlay: the real handler, driven frame by frame.

[FORK] Digital-Union. The pure admission rule is ``test_transfer_guard.py``; this file is about
``gui.py``'s use of it, and it reaches the handler the same way ``test_clips_gui_integration.py``
does -- ``ast``-extracted from the shipped source and executed against a synthesised parent
namespace, because ``gui.py`` cannot be imported on a bare interpreter. The harness is *imported*
from that suite rather than copied, which is what keeps the two files testing one namespace: a
stub that drifted would let this file pass against handlers the app does not ship.

What is asserted here, in the brief's own terms:

* a visible Working state **before** the first file moves, and none at all for a press that moves
  nothing;
* progress counted from recorded ``MoveOutcome``s;
* server-side single-flight against repeated clicks, two browser sessions and a replayed event;
* classification refused while a batch is being sorted;
* the two-step confirmation preserved;
* ``collision.py``'s policies reused, with nothing overwritten;
* a safe terminal state on success, partial failure and an exception;
* and the R11 interaction -- what a transfer may publish when the page moved on under it.
"""

from __future__ import annotations

import ast
import os
import threading

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import collision
from beatsync_fork import transfer_guard as tg
from test_clips_gui_integration import (_GUI, _Gr, _build, _no_poster, _tree_of_clips, ui)  # noqa: F401

_SELECTED, _REJECTED, _PENDING = cr.SELECTED, cr.REJECTED, cr.PENDING


@pytest.fixture
def world(tmp_path, ui):                                    # noqa: F811 - the imported fixture
    media = _tree_of_clips(tmp_path, count=3)
    namespace = _build(str(tmp_path / "state"), ui, poster_runner=_no_poster)
    return {
        "ns": namespace, "media": media, "tmp": tmp_path, "ui": ui,
        "library": str(tmp_path / "library"), "rejected": str(tmp_path / "rejected"),
        "request": _Gr.Request,
    }


def _scan(world, owner, folder=None, page_size=100):
    return world["ns"]["_on_clips_scan"](
        folder or world["media"], True, world["library"], world["rejected"], page_size,
        world["request"](session_hash=owner))


def _keep_all(world, owner):
    """Mark every clip on the owner's page KEPT, through the real decision endpoint.

    The expected revision is read from the store rather than assumed to be 0, because decisions
    are keyed on the *file's* identity: a second browser session scanning the same folder meets
    the revisions the first one already moved, and the optimistic-concurrency check is strict by
    design (a coerced or skipped revision is the R5-P3 / F2 fail-open).
    """
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    store = namespace["_clips_store"]()
    for entry in view["entries"]:
        identity = view["identity"][entry.clip_id]
        current, revision = store.get(identity)
        if current == _SELECTED:
            continue
        answer = namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                            {"to": _SELECTED, "rev": revision})
        assert answer["ok"] is True, answer
    return view


def _entries_in(folder):
    """Real `ClipEntry` records for every file in `folder`, in name order.

    Deterministic ids rather than the registry's opaque ones, so the two forms below can be
    compared position for position -- the point of that test is the *order and the outcomes*, and
    randomly minted ids would make it compare nothing.
    """
    entries = []
    for index, name in enumerate(sorted(os.listdir(folder)), start=1):
        path = os.path.join(folder, name)
        stat = os.stat(path)
        entries.append(cr.ClipEntry(
            clip_id=f"c{index:04d}", index=index, path=path, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, dev=getattr(stat, "st_dev", -1),
            ino=getattr(stat, "st_ino", -1), suffix=os.path.splitext(path)[1]))
    return entries


def _frames(world, owner, armed=""):
    return list(world["ns"]["_clips_confirm_frames"](armed, world["request"](session_hash=owner)))


# Frame positions, named once so no assertion below counts on its fingers.
_REPORT, _GRID, _COUNTS, _CONFIRM, _MESSAGE, _PROCEED, _CANCEL, _ARMED = range(8)


# =============================================================================================
# 1 — the Working state, and where it may NOT appear
# =============================================================================================

def test_a_real_sort_shows_working_before_the_first_file_moves(world):
    """The complaint R13 answers: a long Confirm looked like a dead button. The first frame has
    to arrive before anything has moved, which is why the handler is a generator -- a function
    cannot say "working" and then work."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    sources = [entry.path for entry in view["entries"]]

    stream = world["ns"]["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    first = next(stream)

    assert world["ui"]["CLIPS_WORKING_STARTING_TEXT"].splitlines()[0].format(total=3) \
        in first[_MESSAGE] or "Working" in first[_MESSAGE], first[_MESSAGE]
    assert first[_CONFIRM] == {"interactive": False}, "Confirm must go away while it works"
    assert first[_PROCEED] == {"visible": False} and first[_CANCEL] == {"visible": False}
    assert first[_ARMED] == "", "a spent batch must not leave a live signature"
    for source in sources:
        assert os.path.isfile(source), "a file moved BEFORE the Working frame was emitted"
    assert not os.path.exists(world["library"]), "the destination was created too early"

    remaining = list(stream)
    assert remaining, "the sort never ran"
    for source in sources:
        assert not os.path.exists(source), "the sort did not actually move the clips"


def test_the_working_frame_leaves_the_grid_and_counts_alone(world):
    """The operator is watching the batch being sorted. Blanking the page they pressed Confirm on
    would hide it mid-flight -- and the grid staying clickable is safe because the SERVER refuses
    a classification while a transfer is active, which a disabled tile could never promise."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    working = _frames(world, owner)[0]
    for position in (_REPORT, _GRID, _COUNTS):
        assert working[position] == {}, f"frame position {position} was repainted mid-sort"


def test_the_sweep_gate_emits_exactly_one_frame_and_no_working_state(world):
    """Requirement: do not show Working when Confirm merely presents the confirmation gate and
    moves nothing. A spinner for an operation that has not been authorised yet is the dead-button
    complaint inverted."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    first = view["entries"][0]
    assert namespace["_clips_decide"](owner, view["session"], view["page"], first,
                                       {"to": _SELECTED, "rev": 0})["ok"] is True

    frames = _frames(world, owner)

    assert len(frames) == 1, "the gate must not stream"
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in frames[0][_MESSAGE]
    assert frames[0][_PROCEED] == {"visible": True}
    assert frames[0][_ARMED], "the gate must arm the batch it showed"
    for entry in view["entries"]:
        assert os.path.isfile(entry.path), "the gate moved a file"


@pytest.mark.parametrize("prepare", ["nothing_scanned", "no_identity", "nothing_kept"])
def test_every_refusal_emits_exactly_one_frame_and_claims_no_slot(world, prepare):
    """A refusal changes nothing, so it must not announce work and must not leave the page
    marked busy -- the same discipline `ScanCoordinator` follows, where a scan rejected for a bad
    folder never takes a generation."""
    owner = "tab-a"
    if prepare == "nothing_kept":
        _scan(world, owner)
    elif prepare == "nothing_scanned":
        pass
    if prepare == "no_identity":
        frames = list(world["ns"]["_clips_confirm_frames"]("", world["request"](None)))
    else:
        frames = _frames(world, owner)

    assert len(frames) == 1, frames
    assert frames[0][_PROCEED] == {"visible": False}, "a refusal must not open the gate"
    assert world["ns"]["_CLIPS_TRANSFERS"].active_for(owner) is None, \
        "a refusal claimed the single-flight slot"


def test_the_collected_accessor_returns_the_last_frame(world):
    """`_on_clips_confirm` is the pre-R13 shape, kept because the answer a press settles on is a
    useful thing to ask for. It must be the TERMINAL frame, never the Working one."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    final = world["ns"]["_on_clips_confirm"]("", world["request"](session_hash=owner))
    assert "Sorted all 3" in final[_MESSAGE], final[_MESSAGE]
    assert final[_ARMED] == ""


# =============================================================================================
# 2 — truthful progress
# =============================================================================================

def test_progress_is_counted_from_recorded_outcomes(world):
    """`Moving 2 / 3` must mean two clips have actually been attempted. Every intermediate frame
    is checked against the disk, so a number that ran ahead of the renames would fail here."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    total = len(view["entries"])

    seen = []
    for frame in world["ns"]["_clips_confirm_frames"]("", world["request"](session_hash=owner)):
        gone = sum(1 for entry in view["entries"] if not os.path.exists(entry.path))
        seen.append((frame[_MESSAGE], gone))

    numbers = [int(message.split("Moving ")[1].split(" /")[0])
               for message, _ in seen if "Moving " in message]
    assert numbers == list(range(1, total + 1)), numbers
    for message, gone in seen:
        if "Moving " not in message:
            continue
        done = int(message.split("Moving ")[1].split(" /")[0])
        assert done == gone, f"{message!r} claimed {done} with {gone} actually moved"


def test_the_closing_report_states_moved_skipped_and_failed(world):
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    message = _frames(world, owner)[-1][_MESSAGE]
    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=3, skipped=0, failed=0, total=3) in message, message


def test_a_skipped_clip_is_reported_as_skipped_and_not_as_a_failure(world):
    """A taken filename is a refusal this code made on purpose -- `collision.SKIP` reports it and
    moves nothing. Counting it as a failure would make the no-overwrite guarantee read as a
    malfunction."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    os.makedirs(world["library"], exist_ok=True)
    blocked = view["entries"][1]
    occupant = os.path.join(world["library"], os.path.basename(blocked.path))
    with open(occupant, "wb") as handle:
        handle.write(b"i was here first")

    message = _frames(world, owner)[-1][_MESSAGE]

    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=2, skipped=1, failed=0, total=3) in message, message
    assert open(occupant, "rb").read() == b"i was here first", "the occupant was overwritten"
    assert os.path.isfile(blocked.path), "the blocked clip must stay where it is"


def test_a_partial_batch_keeps_the_real_denominator(world):
    """An abandoned or crashed transfer has fewer outcomes than clips, and reporting against the
    outcomes it collected would silently rebase the denominator -- "2 of 2" for a batch of 3."""
    tally = cr.TransferTally(total=3)
    tally.record(cr.MoveOutcome("a", "Clip 0001", _SELECTED, cr.MOVED))
    tally.record(cr.MoveOutcome("b", "Clip 0002", _SELECTED, cr.MOVED))
    assert (tally.done, tally.total) == (2, 3)
    assert cr.tally_outcomes([], total=3).total == 3
    assert cr.tally_outcomes([]).total == 0, "no outcomes and no stated total is an empty batch"


def test_no_frame_ever_predicts_anything(world):
    """No rate, no percentage, no estimate, no time remaining. The panel says what happened."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    for frame in _frames(world, owner):
        lowered = frame[_MESSAGE].lower()
        for forbidden in ("remaining", "eta", "estimat", "about a minute", "/s", "per second",
                          "%"):
            assert forbidden not in lowered, f"{forbidden!r} in {frame[_MESSAGE]!r}"


def test_the_progress_frame_count_is_bounded_for_a_large_batch(world, tmp_path):
    """One Gradio round trip per clip is right for three and wasteful for a thousand. The budget
    bounds the frames without ever bounding the truth -- the last outcome always emits."""
    big = _tree_of_clips(tmp_path / "big", count=120)
    owner = "tab-a"
    _scan(world, owner, folder=big, page_size=200)
    _keep_all(world, owner)

    frames = _frames(world, owner)
    progress = [f for f in frames if "Moving " in f[_MESSAGE]]

    assert len(progress) <= tg.PROGRESS_FRAME_BUDGET + 1, len(progress)
    assert "Moving 120 / 120" in progress[-1][_MESSAGE], progress[-1][_MESSAGE]
    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=120, skipped=0, failed=0, total=120) in frames[-1][_MESSAGE]


# =============================================================================================
# 3 — server-side single-flight
# =============================================================================================

def test_a_second_press_during_a_sort_is_refused_by_the_server(world):
    """The authority is the guard, not a disabled button. Driven by starting the second press
    from inside the first's progress loop, which is exactly the double-click the operator makes."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    duplicate = []
    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)                                            # the Working frame: the slot is held
    duplicate = list(namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner)))
    rest = list(stream)

    assert len(duplicate) == 1, "a refused duplicate must not stream"
    assert world["ui"]["CLIPS_TRANSFER_BUSY_TEXT"].splitlines()[0].split("(")[0] \
        in duplicate[0][_MESSAGE], duplicate[0][_MESSAGE]
    assert "Sorted all 3" in rest[-1][_MESSAGE], "the first press must still finish"
    # Exactly one copy of each clip reached the library, under its own name.
    names = sorted(os.listdir(world["library"]))
    assert names == sorted(os.path.basename(e.path) for e in view["entries"]), names
    assert not os.path.exists(world["rejected"]) or os.listdir(world["rejected"]) == []


def test_a_duplicate_press_produces_no_extra_collision_suffix(world):
    """The named hazard: under SUFFIX a second concurrent run of the same batch could take the
    next free name and leave `clip0000_01.mp4` beside `clip0000.mp4`. Single-flight is what
    makes that unreachable, so the assertion is on the destination listing."""
    owner = "tab-a"
    namespace = _build(str(world["tmp"] / "state2"), world["ui"], poster_runner=_no_poster)
    namespace["_collision_policy"] = lambda: collision.SUFFIX
    world["ns"] = namespace
    _scan(world, owner)
    view = _keep_all(world, owner)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    list(namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner)))
    list(stream)

    names = sorted(os.listdir(world["library"]))
    assert names == sorted(os.path.basename(e.path) for e in view["entries"]), names
    assert not any("_01" in name for name in names), f"a duplicate minted a suffix: {names}"


def test_a_replayed_event_is_refused_exactly_like_a_second_click(world):
    """A replayed submission is indistinguishable from a second click at this layer, which is the
    point: the server does not need to tell them apart, because admission is a test-and-set and
    not a judgement about where a request came from."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    replay = list(namespace["_clips_confirm_frames"]("captured-signature-from-an-old-body",
                                                      world["request"](session_hash=owner)))
    list(stream)

    assert len(replay) == 1
    assert "already being sorted" in replay[0][_MESSAGE], replay[0][_MESSAGE]


def test_two_browser_sessions_cannot_sort_one_folder_at_once(world):
    """Two tabs are two owner keys, so the per-owner rule alone would admit both. The refusal
    names the real case rather than reporting somebody else's press as this page's own."""
    namespace = world["ns"]
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    _scan(world, "tab-b")
    _keep_all(world, "tab-b")

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    other = list(namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-b")))
    list(stream)

    assert len(other) == 1
    assert "another review session" in other[0][_MESSAGE], other[0][_MESSAGE]


def test_many_concurrent_presses_admit_exactly_one_sort(world):
    """Real threads against the real handler. `concurrency_limit=None` means Gradio delivers all
    of them, so this is the shape the shipped app actually sees.

    **The overlap is now enforced rather than hoped for.** A barrier only guarantees that the
    presses *start* together, and a three-clip sort finishes in microseconds -- so the winner could
    release before a straggler reached `begin`, the straggler was then legitimately admitted, and
    the assertion below saw "2 presses sorted at once". Measured at **4 failures in 30 runs** on
    the pristine R13 candidate: a pre-existing race in this test, not in the guard, and the one
    thing a single-flight test must not be is occasionally right.

    So the winner is held at its first `note_progress` -- the call immediately before the Working
    frame, with the reservation already taken -- until every one of the eight presses has been
    through admission. Then the presses genuinely overlap, "exactly one sorted" is a statement
    about concurrency rather than about scheduling luck, and the test is deterministic.
    """
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    presses = 8
    guard = namespace["_CLIPS_TRANSFERS"]
    real_begin, real_progress = guard.begin, guard.note_progress
    attempted, everyone_tried = [], threading.Event()

    def counting_begin(*args, **kwargs):
        try:
            return real_begin(*args, **kwargs)
        finally:
            attempted.append(1)
            if len(attempted) >= presses:
                everyone_tried.set()

    def holding_progress(reservation, done, total):
        # `done == 0` is the frame before the first file moves, so holding here keeps the
        # reservation taken for the whole admission window without delaying any move afterwards.
        if done == 0:
            assert everyone_tried.wait(30), "not every press reached admission"
        return real_progress(reservation, done, total)

    guard.begin = counting_begin
    guard.note_progress = holding_progress
    results, start = [], threading.Barrier(presses)

    def press():
        start.wait(30)
        results.append(list(namespace["_clips_confirm_frames"](
            "", world["request"](session_hash=owner))))

    threads = [threading.Thread(target=press, daemon=True) for _ in range(presses)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not [t for t in threads if t.is_alive()], "a press hung"

    assert len(attempted) == presses, f"only {len(attempted)} of {presses} reached admission"
    sorted_runs = [r for r in results if any("Moving " in f[_MESSAGE] for f in r)]
    assert len(sorted_runs) == 1, f"{len(sorted_runs)} presses sorted at once"
    names = sorted(os.listdir(world["library"]))
    assert names == sorted(os.path.basename(e.path) for e in view["entries"]), names


def test_the_slot_is_released_so_a_retry_is_possible(world):
    """A leaked slot would mean the page could never be sorted again, which is why the release
    sits in a `finally`. The retry is genuinely *admitted* here, not refused as busy.

    What the retry finds is pre-R13 behaviour and is left exactly as it was: emptying the folder
    makes the post-sort re-enumeration refuse before it takes a generation, which deliberately
    does **not** blank the current page (`.claude/rules/clips-preview.md` -- an empty enumeration
    may not destroy an otherwise valid view). So the second press re-attempts the same entries and
    reports every one of them as no longer where the scan found it. Nothing moves, nothing is
    overwritten, and that is the safe retry the requirement asks for."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    _frames(world, owner)

    assert world["ns"]["_CLIPS_TRANSFERS"].active_for(owner) is None, \
        "the slot leaked, so the page can never be sorted again"

    library_before = sorted(os.listdir(world["library"]))
    again = _frames(world, owner)

    assert any("Moving " in f[_MESSAGE] for f in again), \
        "the retry was refused as busy rather than admitted"
    final = again[-1][_MESSAGE]
    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=0, skipped=3, failed=0, total=3) in final, final
    assert world["ui"]["CLIPS_MOVE_REASONS"]["missing"] in final, final
    assert sorted(os.listdir(world["library"])) == library_before, \
        "the retry moved something a second time"
    assert len(library_before) == len(view["entries"])


# =============================================================================================
# 4 — classification protection
# =============================================================================================

def test_a_classification_is_refused_while_the_batch_is_being_sorted(world):
    """A transfer captured its snapshot before it started moving. A decision accepted now would
    be honoured by the screen and ignored by the batch -- the operator would watch a clip they
    just marked Keep land in Discarded."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    answer = namespace["_clips_decide"](owner, view["session"], view["page"],
                                         view["entries"][2], {"to": _REJECTED, "rev": 1})
    list(stream)

    assert answer["ok"] is False
    assert answer["code"] == "transfer_in_progress", answer
    assert answer["decision"] == _SELECTED, "the refusal must disclose the server's own record"
    assert answer["rev"] == 1, "a refusal must not move the revision"


def test_a_refused_classification_writes_nothing_durable(world):
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    identity = view["identity"][view["entries"][0].clip_id]
    before = namespace["_clips_store"]().get(identity)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": _REJECTED, "rev": before[1]})
    assert namespace["_clips_store"]().get(identity) == before, \
        "a refusal reached the durable store"
    list(stream)


def test_another_sessions_classification_on_the_same_folder_is_refused_too(world):
    """The second tab never saw a greyed-out tile, which is exactly why the check cannot live in
    the browser. Its clips are the ones being moved, so its decisions are refused as well."""
    namespace = world["ns"]
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    _scan(world, "tab-b")
    other = namespace["_clips_view"]("tab-b")

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    answer = namespace["_clips_decide"]("tab-b", other["session"], other["page"],
                                         other["entries"][0], {"to": _SELECTED, "rev": 0})
    list(stream)

    assert answer["ok"] is False and answer["code"] == "transfer_in_progress", answer


def test_classification_works_again_the_moment_the_sort_finishes(world):
    """The protection is a window, not a latch. A latch that outlived the batch would be a new
    way to strand the page."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    _frames(world, owner)
    namespace = world["ns"]

    # The post-sort re-scan published the next page: here, the now-empty folder.
    assert namespace["_CLIPS_TRANSFERS"].busy(owner) is None
    _scan(world, owner, folder=_tree_of_clips(world["tmp"] / "more", count=1))
    fresh = namespace["_clips_view"](owner)
    answer = namespace["_clips_decide"](owner, fresh["session"], fresh["page"],
                                         fresh["entries"][0], {"to": _SELECTED, "rev": 0})
    assert answer["ok"] is True, answer


# =============================================================================================
# 4b — F-1: a decision accepted by the server can never be silently ignored by a transfer
# =============================================================================================
#
# Section 4 above tests the easy half: a classification arriving *while* a batch is being sorted.
# The defect was in the window before that -- between the transfer reading its decision snapshot
# and claiming its slot -- where `busy()` legitimately reported nothing and a decision was
# accepted, made durable, and then ignored by the batch, both halves reporting success.
#
# Every test here places an interleaving by construction rather than by racing the clock: the
# admission hook fires in the instant between the snapshot and the claim, and `_SuspendedCommit`
# suspends the one place a decision becomes durable. Real store, real handler, real files.

def _hook_admission(world, before):
    """Run ``before()`` ONCE, in the instant between the transfer's snapshot and its admission.

    Wrapping the guard instance is what makes this a *scheduler* rather than a race: the handler
    looks `begin` up on the object when it calls it, so this is the shipped code path with one
    extra statement at an exactly known point.

    One-shot deliberately. It models one operator click landing in one window, and a hook that
    fired on every admission would also fire on the *retry* -- re-classifying each time, so the
    retry would be refused for the same reason, for ever. That is an artefact of the hook, not of
    the handler: a refusal says "press Confirm again", and the second press only meets a changed
    snapshot if somebody really did classify again inside it.
    """
    guard = world["ns"]["_CLIPS_TRANSFERS"]
    real = guard.begin
    fired = []

    def hooked(*args, **kwargs):
        if not fired:
            fired.append(True)
            before()
        return real(*args, **kwargs)

    guard.begin = hooked
    return guard


class _SuspendedCommit:
    """Hold a classification open *inside* its durable commit.

    `ReviewStore.decide` is the only place a decision becomes durable, so suspending it is how a
    commit is placed after an instant the transfer has already passed -- which is the one thing a
    post-admission snapshot re-read cannot defend against on its own.
    """

    def __init__(self, world):
        self.store = world["ns"]["_clips_store"]()
        self._real = self.store.decide
        self.entered = threading.Event()
        self.may_commit = threading.Event()
        self.committed = threading.Event()
        self.store.decide = self._wrapped

    def _wrapped(self, *args, **kwargs):
        self.entered.set()
        assert self.may_commit.wait(10), "the test never released the suspended commit"
        try:
            return self._real(*args, **kwargs)
        finally:
            self.committed.set()

    def classify_in_background(self, world, owner, view, entry, to, rev):
        answer = {}

        def run():
            answer["reply"] = world["ns"]["_clips_decide"](
                owner, view["session"], view["page"], entry, {"to": to, "rev": rev})

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert self.entered.wait(10), "the classification never reached its durable commit"
        return thread, answer


def _where(world, name):
    if os.path.exists(os.path.join(world["library"], name)):
        return "library"
    if os.path.exists(os.path.join(world["rejected"], name)):
        return "rejected"
    return "unmoved"


def _landed(world, role="library"):
    """What is in a destination, treating "never created" as "nothing arrived".

    A press refused before its first move never reaches `iter_sorted_clips`, so the destination
    may legitimately not exist yet -- which is itself the strongest form of "nothing moved", and
    must not read as a test error.
    """
    try:
        return sorted(os.listdir(world[role]))
    except FileNotFoundError:
        return []


def test_case_a_a_decision_committed_before_the_snapshot_is_in_the_plan(world):
    """The ordinary case, stated as a control: a classification that finished before Confirm was
    pressed is simply part of the batch. The correction must not turn this into a refusal."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    target = view["entries"][1]
    identity = view["identity"][target.clip_id]
    _, revision = namespace["_clips_store"]().get(identity)

    answer = namespace["_clips_decide"](owner, view["session"], view["page"], target,
                                        {"to": _REJECTED, "rev": revision})
    assert answer["ok"] is True
    frames = _frames(world, owner)

    assert _where(world, os.path.basename(target.path)) == "rejected", \
        "the accepted decision was not the one the transfer acted on"
    assert world["ui"]["CLIPS_DECISIONS_CHANGED_TEXT"] not in frames[-1][_MESSAGE], \
        "a decision that predates the snapshot must not be reported as a late change"
    assert len(frames) > 1, "a real sort must still show the Working state"


def test_case_b_a_delayed_durable_write_cannot_be_overtaken_by_the_transfer(world):
    """The straddle, and the heart of F-1.

    The classification is past every check and inside its journal append; the transfer then tries
    to start. Admitting it would move files by a snapshot that is about to be contradicted, while
    the classification reports success. So the transfer is refused instead -- explicitly, in one
    frame, having moved nothing.
    """
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    target = view["entries"][1]
    identity = view["identity"][target.clip_id]
    _, revision = namespace["_clips_store"]().get(identity)
    suspended = _SuspendedCommit(world)
    held = {}

    def during_the_window():
        held["thread"], held["answer"] = suspended.classify_in_background(
            world, owner, view, target, _REJECTED, revision)

    _hook_admission(world, during_the_window)
    frames = _frames(world, owner)
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    assert len(frames) == 1, f"a refused press must move nothing and show one frame: {len(frames)}"
    assert world["ui"]["CLIPS_DECISION_IN_FLIGHT_TEXT"] == frames[0][_MESSAGE], frames[0][_MESSAGE]
    assert _landed(world) == [], "a refused transfer moved a file"
    assert held["answer"]["reply"]["ok"] is True, \
        "the classification was the one holding the floor; it must still succeed"
    assert namespace["_clips_store"]().get(identity) == (_REJECTED, revision + 1)
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0, "a decision slot leaked"
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, "a reservation leaked"


def test_case_c_a_classification_after_admission_does_not_advance_the_revision(world):
    """Section 4 asserts the refusal; this asserts the two things that make it safe -- no durable
    revision moved, and no decision slot was taken by a request that was refused."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    target = view["entries"][2]
    identity = view["identity"][target.clip_id]
    before = namespace["_clips_store"]().get(identity)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    answer = namespace["_clips_decide"](owner, view["session"], view["page"], target,
                                        {"to": _REJECTED, "rev": before[1]})
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0, \
        "a refused classification registered a decision slot"
    list(stream)

    assert answer["ok"] is False and answer["code"] == "transfer_in_progress"
    assert namespace["_clips_store"]().get(identity) == before, \
        "a refusal reached the durable record"


def test_case_d_no_decision_is_ever_accepted_and_then_ignored(world):
    """The invariant itself, asserted as the conjunction that must never hold: accepted, durable,
    and contradicted by where the file actually went.

    This is the case that proves a post-admission snapshot re-read is not sufficient on its own.
    Measured against a probe carrying only that re-read, the commit landed immediately after the
    comparison, the re-read saw nothing, and the file moved by the stale snapshot. The claim in
    `TransferGuard.begin_decision` is what closes it.
    """
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    target = view["entries"][1]
    identity = view["identity"][target.clip_id]
    name = os.path.basename(target.path)
    _, revision = namespace["_clips_store"]().get(identity)
    suspended = _SuspendedCommit(world)
    held = {}

    _hook_admission(world, lambda: held.update(zip(
        ("thread", "answer"),
        suspended.classify_in_background(world, owner, view, target, _REJECTED, revision))))
    frames = _frames(world, owner)
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    accepted = held["answer"]["reply"]["ok"] is True
    durable = namespace["_clips_store"]().get(identity)[0]
    landed = _where(world, name)
    assert not (accepted and landed == "library" and durable == _REJECTED), (
        f"accepted={accepted} durable={durable} landed={landed}: a decision the server accepted "
        f"was silently ignored by the transfer, and both reported success")
    assert landed == "unmoved", f"the refused transfer moved the clip to {landed}"
    assert len(frames) == 1


def test_case_e_an_acknowledged_keep_never_lands_in_discarded(world):
    """The operator's own account of the bug: they marked a clip Keep, were told Keep, and watched
    it go to Discarded -- because the sweep had already planned it as unmarked.

    The classification runs to completion inside the admission window, so it holds no slot by the
    time `begin` runs and admission is right to succeed. The post-admission re-read is what
    catches it, which is why both halves of the correction are needed.
    """
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    store = namespace["_clips_store"]()

    kept, late = view["entries"][0], view["entries"][1]
    _, revision = store.get(view["identity"][kept.clip_id])
    namespace["_clips_decide"](owner, view["session"], view["page"], kept,
                               {"to": _SELECTED, "rev": revision})

    gate = _frames(world, owner)
    assert len(gate) == 1 and gate[0][_ARMED], "the sweep gate did not arm"
    armed = gate[0][_ARMED]

    answer = {}

    def during_the_window():
        _, current = store.get(view["identity"][late.clip_id])
        answer["reply"] = namespace["_clips_decide"](
            owner, view["session"], view["page"], late, {"to": _SELECTED, "rev": current})

    _hook_admission(world, during_the_window)
    frames = _frames(world, owner, armed=armed)

    assert answer["reply"]["ok"] is True, "the Keep was not acknowledged at all"
    assert _where(world, os.path.basename(late.path)) != "rejected", \
        "a clip the server accepted as KEEP was swept into Discarded"
    assert _where(world, os.path.basename(late.path)) == "unmoved"
    assert len(frames) == 1 and frames[0][_MESSAGE] == world["ui"]["CLIPS_DECISIONS_CHANGED_TEXT"]
    assert _landed(world) == [], "the refused sweep moved something"


def test_case_f_an_acknowledged_reject_never_lands_in_the_library(world):
    """The same defect in the other direction, which matters because it has no sweep in it: a
    plain Confirm, and a clip re-classified from Keep to Discard inside the window."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    store = namespace["_clips_store"]()
    late = view["entries"][1]
    answer = {}

    def during_the_window():
        _, current = store.get(view["identity"][late.clip_id])
        answer["reply"] = namespace["_clips_decide"](
            owner, view["session"], view["page"], late, {"to": _REJECTED, "rev": current})

    _hook_admission(world, during_the_window)
    frames = _frames(world, owner)

    assert answer["reply"]["ok"] is True
    assert _where(world, os.path.basename(late.path)) != "library", \
        "a clip the server accepted as REJECTED was moved into the Media Library"
    assert len(frames) == 1 and frames[0][_MESSAGE] == world["ui"]["CLIPS_DECISIONS_CHANGED_TEXT"]
    assert _landed(world) == [], \
        "nothing may move on a press that refused before the Working frame"

    # And the retry sorts the page as it now stands, which is what makes the refusal honest
    # rather than a dead end.
    again = _frames(world, owner)
    assert len(again) > 1, "the retry was refused too"
    assert _where(world, os.path.basename(late.path)) == "rejected"


def test_case_g_two_sessions_on_one_folder_exclude_each_others_decisions_and_sorts(world):
    """Neither tab ever saw the other's greyed-out button, which is the whole reason this lives on
    the server. The second tab's in-flight decision refuses the first tab's transfer."""
    namespace = world["ns"]
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    _scan(world, "tab-b")
    other = namespace["_clips_view"]("tab-b")
    target = other["entries"][0]
    _, revision = namespace["_clips_store"]().get(other["identity"][target.clip_id])
    suspended = _SuspendedCommit(world)
    held = {}

    _hook_admission(world, lambda: held.update(zip(
        ("thread", "answer"),
        suspended.classify_in_background(world, "tab-b", other, target, _REJECTED, revision))))
    frames = _frames(world, "tab-a")
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    assert len(frames) == 1
    assert frames[0][_MESSAGE] == world["ui"]["CLIPS_DECISION_IN_FLIGHT_TEXT"]
    assert _landed(world) == []
    assert held["answer"]["reply"]["ok"] is True
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0


def test_case_h_a_decision_on_a_different_folder_never_delays_a_sort(world):
    """Exclusion is the two rules admission already used, not a global stop-the-world. Two
    operators on two folders must not be able to refuse each other."""
    namespace = world["ns"]
    elsewhere = _tree_of_clips(world["tmp"] / "elsewhere", count=2)
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    namespace["_on_clips_scan"](elsewhere, True, str(world["tmp"] / "lib2"),
                               str(world["tmp"] / "rej2"), 100,
                               world["request"](session_hash="tab-b"))
    other = namespace["_clips_view"]("tab-b")
    target = other["entries"][0]
    _, revision = namespace["_clips_store"]().get(other["identity"][target.clip_id])
    suspended = _SuspendedCommit(world)
    held = {}

    _hook_admission(world, lambda: held.update(zip(
        ("thread", "answer"),
        suspended.classify_in_background(world, "tab-b", other, target, _SELECTED, revision))))
    frames = _frames(world, "tab-a")
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    assert len(frames) > 1, "an unrelated folder's classification refused this sort"
    assert len(_landed(world)) == 3, "the sort did not complete"


def test_case_h_alias_spellings_of_one_folder_are_one_key(world):
    """Two spellings of one directory must not look like two folders, or the cross-session rule
    could be walked around by typing the path differently."""
    key = world["ns"]["_clips_transfer_key"]
    canonical = key(world["media"])
    assert canonical
    for spelling in (os.path.join(world["media"], ""),
                     os.path.join(world["media"], "."),
                     os.path.join(world["media"], "..", os.path.basename(world["media"])),
                     world["media"] + os.sep):
        assert key(spelling) == canonical, spelling


def test_case_i_a_failing_durable_write_leaks_no_decision_slot(world):
    """The slot is released in a `finally`, and this is why that matters: a slot that outlived a
    failed commit would refuse this owner's transfers until the app restarted -- a worse failure
    than the one being corrected."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    store = namespace["_clips_store"]()
    target = view["entries"][0]
    real = store.decide

    def exploding(*args, **kwargs):
        raise OSError("the journal could not be appended")

    store.decide = exploding
    try:
        with pytest.raises(OSError):
            namespace["_clips_decide"](owner, view["session"], view["page"], target,
                                       {"to": _REJECTED, "rev": 1})
    finally:
        store.decide = real

    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0, \
        "a decision slot survived a failed durable write"
    assert len(_frames(world, owner)) > 1, "the owner's transfers are now permanently refused"


def test_case_i_a_stale_write_refusal_leaks_no_decision_slot(world):
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    answer = namespace["_clips_decide"](owner, view["session"], view["page"],
                                        view["entries"][0], {"to": _REJECTED, "rev": 99})
    assert answer["ok"] is False and answer["code"] == cr.REVISION_CONFLICT
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0


def test_case_i_an_abandoned_generator_releases_the_reservation(world):
    """Gradio closing a dropped event raises `GeneratorExit` at the yield. The refusal added for
    F-1 sits inside the same `try`, so neither path can strand the slot."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    stream.close()

    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0


def test_case_i_a_refused_late_change_releases_the_reservation(world):
    """The new refusal returns from inside the `try` whose `finally` releases the slot, rather
    than releasing by hand -- so a later edit cannot reintroduce a leak by adding a path."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    store = namespace["_clips_store"]()
    late = view["entries"][1]

    def during_the_window():
        _, current = store.get(view["identity"][late.clip_id])
        namespace["_clips_decide"](owner, view["session"], view["page"], late,
                                   {"to": _REJECTED, "rev": current})

    _hook_admission(world, during_the_window)
    frames = _frames(world, owner)

    assert len(frames) == 1 and frames[0][_MESSAGE] == world["ui"]["CLIPS_DECISIONS_CHANGED_TEXT"]
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, \
        "the late-change refusal leaked a reservation"


def test_case_j_the_sweep_gate_claims_neither_a_slot_nor_a_decision(world):
    """The gate moves nothing, so it must leave the page unmarked in both registers -- otherwise a
    gate the operator never answers would refuse their next classification."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    _, revision = namespace["_clips_store"]().get(view["identity"][view["entries"][0].clip_id])
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": _SELECTED, "rev": revision})

    gate = _frames(world, owner)

    assert len(gate) == 1 and gate[0][_ARMED]
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0
    answer = namespace["_clips_decide"](owner, view["session"], view["page"],
                                        view["entries"][1], {"to": _SELECTED, "rev": 0})
    assert answer["ok"] is True, "the unanswered gate refused a later classification"


# =============================================================================================
# 4c — F-2: the conflict unit is the CLIP, not the folder
# =============================================================================================
#
# Section 4b closes the window between a snapshot and an admission. It closes it for operations
# the guard can *see* collide -- same owner, or same resolved folder. Neither question is the one
# that decides whether two operations touch the same file.
#
# Two review sessions that scanned an ancestor and a descendant of one tree have two owner keys
# and two different `root_key`s, and the same clip in both batches under the SAME durable decision
# identity. So a classification accepted from the child page was answered `ok: True`, was durable,
# and was ignored by a sort already running on the parent page, which filed the clip by its older
# snapshot -- the same "accepted but ignored" conjunction as F-1, reached by a route neither of the
# first two rules could see. Independent review reproduced it on the F-1 tree.
#
# These tests use the real store, real files and two real scans. Several of them deliberately use
# **no synchronisation hook at all**: starting the generator and stopping at its first yield is
# enough to hold a reservation, so the overlap is placed by ordinary control flow.

def _nested_world(world, child_clips=1):
    """A parent tree with a subfolder, and a second destination pair for the other session.

    Returns ``(parent, child)``. The parent already holds three clips from the fixture, so after
    this a recursive scan of the parent sees four and a scan of the child sees one -- and that one
    clip is in both batches.
    """
    parent = world["media"]
    child = os.path.join(parent, "sub")
    os.makedirs(child, exist_ok=True)
    for index in range(child_clips):
        with open(os.path.join(child, f"nested{index:04d}.mp4"), "wb") as handle:
            handle.write(b"n" * (777 + index))
    world["library2"] = str(world["tmp"] / "library2")
    world["rejected2"] = str(world["tmp"] / "rejected2")
    return parent, child


def _scan_child(world, owner, child, recursive=True):
    """Scan ``child`` as ``owner``, into that session's OWN destinations.

    Separate destinations on purpose: it is what makes a misfiling visible as "the clip went to
    the other session's Media Library" rather than being hidden by a shared folder.
    """
    return world["ns"]["_on_clips_scan"](
        child, recursive, world["library2"], world["rejected2"], 100,
        world["request"](session_hash=owner))


def _shared_clip(world, parent_view, child_view):
    """The one clip present in both pages, with its single durable identity."""
    name = "nested0000.mp4"
    in_parent = [e for e in parent_view["entries"] if os.path.basename(e.path) == name]
    in_child = [e for e in child_view["entries"] if os.path.basename(e.path) == name]
    assert in_parent, "the nested clip is not in the parent's recursive batch"
    assert in_child, "the nested clip is not in the child's batch"
    identity = child_view["identity"][in_child[0].clip_id]
    assert identity == parent_view["identity"][in_parent[0].clip_id], \
        "the two pages disagree on the clip's identity -- this suite would prove nothing"
    return in_child[0], identity, name


def _two_sessions_on_nested_roots(world, recursive=True):
    """Owner A on the parent (recursively), owner B on the child. Everything A sees is KEPT."""
    parent, child = _nested_world(world)
    _scan(world, "tab-a", folder=parent)
    parent_view = _keep_all(world, "tab-a")
    _scan_child(world, "tab-b", child, recursive=recursive)
    child_view = world["ns"]["_clips_view"]("tab-b")
    assert world["ns"]["_clips_transfer_key"](parent) \
        != world["ns"]["_clips_transfer_key"](child), "this suite needs two distinct root keys"
    return parent_view, child_view


def test_f2_case_1_a_cross_root_classification_in_flight_refuses_the_sort(world):
    """Scenario 1 -- classification first. Owner B's decision is suspended inside its journal
    append; owner A then presses Confirm on the parent page.

    Admitting would be the defect: B's decision becomes durable a moment later and A's batch moves
    by the snapshot it took before. `_conflicting_transfer` cannot see it -- a decision in flight
    holds no active transfer -- so `begin` compares the pending slot's identity against the page
    it is about to claim.
    """
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    target, identity, name = _shared_clip(world, parent_view, child_view)
    _, revision = namespace["_clips_store"]().get(identity)
    suspended = _SuspendedCommit(world)
    held = {}

    _hook_admission(world, lambda: held.update(zip(
        ("thread", "answer"),
        suspended.classify_in_background(world, "tab-b", child_view, target, _REJECTED,
                                         revision))))
    frames = _frames(world, "tab-a")
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    assert len(frames) == 1, f"a refused press must emit one frame, got {len(frames)}"
    assert frames[0][_MESSAGE] == world["ui"]["CLIPS_DECISION_IN_FLIGHT_TEXT"], frames[0][_MESSAGE]
    assert _landed(world) == [], "a refused transfer moved a file"
    assert held["answer"]["reply"]["ok"] is True, \
        "the classification held the floor; it must still succeed"
    assert namespace["_clips_store"]().get(identity) == (_REJECTED, revision + 1)
    assert _where(world, name) == "unmoved"
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0, "a decision slot leaked"
    assert namespace["_CLIPS_TRANSFERS"].active_for("tab-a") is None, "a reservation leaked"


def test_f2_case_2_a_cross_root_classification_is_refused_once_the_sort_holds_the_floor(world):
    """Scenario 2 -- transfer first. **No synchronisation hook**: the generator's first yield IS
    the reservation being held, so the overlap is placed by ordinary control flow.

    This is the direction that was reproduced as accepted-but-ignored: B's classification used to
    be admitted, committed and reported `ok: True` while A's batch filed the clip by its older
    snapshot.
    """
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    target, identity, name = _shared_clip(world, parent_view, child_view)
    before = namespace["_clips_store"]().get(identity)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)                                  # the Working frame: the reservation is held
    answer = namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                        target, {"to": _REJECTED, "rev": before[1]})
    assert namespace["_CLIPS_TRANSFERS"].deciding() == 0, \
        "a refused classification registered a decision slot"
    list(stream)

    assert answer["ok"] is False and answer["code"] == "transfer_in_progress", answer
    assert namespace["_clips_store"]().get(identity) == before, \
        "a REFUSED classification advanced the durable revision"
    # and the sort did what the record says, which is the property that actually matters
    assert _where(world, name) == "library", \
        "the clip A sorted as KEPT did not land in the library"
    assert namespace["_clips_store"]().get(identity)[0] == _SELECTED


def test_f2_case_3_two_cross_root_transfers_cannot_run_at_once(world):
    """Scenario 3 -- two concurrent transfer submissions over an overlapping batch.

    Strictly worse than the classification case: both sorts would `os.rename` the same path, and
    the loser would find the file already gone. The owner and folder rules never excluded this
    either, so it is part of the same correction.
    """
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    _shared_clip(world, parent_view, child_view)
    # B keeps its single clip so its own press would otherwise be admissible
    for entry in child_view["entries"]:
        identity = child_view["identity"][entry.clip_id]
        current, revision = namespace["_clips_store"]().get(identity)
        if current != _SELECTED:
            namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                       entry, {"to": _SELECTED, "rev": revision})

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    other = _frames(world, "tab-b")
    list(stream)

    assert len(other) == 1, f"the overlapping sort was admitted: {len(other)} frames"
    # A's figures are deterministic here: the Working frame `next(stream)` consumed is
    # emitted immediately after `note_progress(reservation, 0, tally.total)`, and A's
    # recursive page holds four clips -- so the refusal must quote 0 of 4, not a parsed guess.
    assert other[0][_MESSAGE] == world["ui"]["CLIPS_TRANSFER_CLIP_BUSY_TEXT"].format(
        done=0, total=4), other[0][_MESSAGE]
    assert "different folder" in other[0][_MESSAGE], \
        "the refusal must not claim the folders are the same"
    assert not os.path.exists(world["library2"]) or os.listdir(world["library2"]) == [], \
        "the refused session moved a file"


def test_f2_case_4_the_same_identity_through_two_owner_sessions_is_one_clip(world):
    """Scenario 4. The protection is keyed on the durable identity, so it cannot be walked around
    by arriving through a different session -- which is the whole reason it is not in the browser.
    """
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    target, identity, _ = _shared_clip(world, parent_view, child_view)
    guard = namespace["_CLIPS_TRANSFERS"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    assert identity in guard.active_for("tab-a").identities, \
        "the reservation does not cover the clip it is about to move"
    assert guard.conflict("tab-b", namespace["_clips_transfer_key"](
        os.path.dirname(target.path)), [identity])[0] == "clip_in_progress"
    list(stream)
    assert guard.conflict("tab-b", "", [identity]) is None, "the exclusion outlived the transfer"


def test_f2_case_5_unrelated_folders_remain_fully_independent(world):
    """Scenario 5. Exclusion is per clip, not a global stop-the-world: two operators reviewing two
    trees must not be able to refuse each other."""
    namespace = world["ns"]
    elsewhere = _tree_of_clips(world["tmp"] / "elsewhere", count=2)
    world["library2"] = str(world["tmp"] / "library2")
    world["rejected2"] = str(world["tmp"] / "rejected2")
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    _scan_child(world, "tab-b", elsewhere)
    other = namespace["_clips_view"]("tab-b")
    for entry in other["entries"]:
        identity = other["identity"][entry.clip_id]
        _, revision = namespace["_clips_store"]().get(identity)
        namespace["_clips_decide"]("tab-b", other["session"], other["page"], entry,
                                   {"to": _SELECTED, "rev": revision})

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    unrelated = _frames(world, "tab-b")
    list(stream)

    assert len(unrelated) > 1, "an unrelated folder's sort was refused"
    assert len(_landed(world)) == 3 and len(os.listdir(world["library2"])) == 2


def test_f2_case_6_nested_folders_with_no_shared_clip_stay_independent(world):
    """Scenario 6. The rule is identity overlap, NOT an ancestor test on the paths.

    Owner A scans the parent **non-recursively**, so the nested clip is not on its page at all.
    The folders are still nested; the batches are disjoint; both sorts must proceed. A path-prefix
    rule would have refused this, which is why the conflict unit is the clip.
    """
    namespace = world["ns"]
    parent, child = _nested_world(world)
    namespace["_on_clips_scan"](parent, False, world["library"], world["rejected"], 100,
                                world["request"](session_hash="tab-a"))
    parent_view = _keep_all(world, "tab-a")
    assert all(os.path.basename(e.path) != "nested0000.mp4" for e in parent_view["entries"]), \
        "the non-recursive scan picked up the nested clip"
    _scan_child(world, "tab-b", child)
    child_view = namespace["_clips_view"]("tab-b")
    for entry in child_view["entries"]:
        identity = child_view["identity"][entry.clip_id]
        _, revision = namespace["_clips_store"]().get(identity)
        namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"], entry,
                                   {"to": _SELECTED, "rev": revision})

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    nested = _frames(world, "tab-b")
    list(stream)

    assert len(nested) > 1, "a nested folder with no shared clip was refused"
    assert len(_landed(world)) == 3
    assert os.listdir(world["library2"]) == ["nested0000.mp4"]


def test_f2_case_7_identical_roots_and_alias_spellings_are_still_excluded_first(world):
    """Scenario 7. The folder rule is not weakened and still answers FIRST, with its own code:
    told "another session is sorting a different folder that contains them" about the very same
    folder, an operator would go looking for a folder that does not exist."""
    namespace = world["ns"]
    _scan(world, "tab-a")
    _keep_all(world, "tab-a")
    aliased = os.path.join(world["media"], "..", os.path.basename(world["media"]))
    assert namespace["_clips_transfer_key"](aliased) \
        == namespace["_clips_transfer_key"](world["media"])
    namespace["_on_clips_scan"](aliased, True, str(world["tmp"] / "library2"),
                                str(world["tmp"] / "rejected2"), 100,
                                world["request"](session_hash="tab-b"))
    other = namespace["_clips_view"]("tab-b")
    for entry in other["entries"]:
        identity = other["identity"][entry.clip_id]
        _, revision = namespace["_clips_store"]().get(identity)
        if namespace["_clips_store"]().get(identity)[0] != _SELECTED:
            namespace["_clips_decide"]("tab-b", other["session"], other["page"], entry,
                                       {"to": _SELECTED, "rev": revision})

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    alias = _frames(world, "tab-b")
    list(stream)

    assert len(alias) == 1
    assert "this folder" in alias[0][_MESSAGE], alias[0][_MESSAGE]
    assert alias[0][_MESSAGE] != world["ui"]["CLIPS_TRANSFER_CLIP_BUSY_TEXT"], \
        "an alias of one folder was reported as a different folder"


def test_f2_case_8_an_unresolvable_root_still_protects_the_clip(world):
    """Scenario 8 -- unknown `root_key`, matching identity.

    `_clips_transfer_key` answers `''` for a path it cannot resolve, which switches the folder rule
    off entirely. Before F-2 that left the clip unprotected across owners; the identity is still
    known, so it is protected now. Asserted at the authority, because a view cannot be published
    with an unresolvable root.
    """
    namespace = world["ns"]
    guard = namespace["_CLIPS_TRANSFERS"]
    _scan(world, "tab-a")
    view = _keep_all(world, "tab-a")
    identities = namespace["_clips_identities"](view)
    assert namespace["_clips_transfer_key"]("") == ''

    reservation = guard.begin("tab-a", view.get("generation"), "sig", '', identities)
    try:
        with pytest.raises(tg.TransferBusy) as refused:
            guard.begin_decision("tab-b", '', identities[0])
        assert refused.value.code == "clip_in_progress"
        assert guard.begin_decision("tab-b", '', "an-identity-nobody-is-sorting").owner == "tab-b"
    finally:
        guard.end(reservation)


def test_f2_case_9_a_suspended_cross_root_commit_is_never_overtaken(world):
    """Scenario 9, stated as the invariant's own conjunction: accepted, durable, and contradicted
    by where the file actually went. This is the exact shape independent review measured on the
    F-1 tree as `accepted=True durable=REJECTED landed=library`."""
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    target, identity, name = _shared_clip(world, parent_view, child_view)
    _, revision = namespace["_clips_store"]().get(identity)
    suspended = _SuspendedCommit(world)
    held = {}

    _hook_admission(world, lambda: held.update(zip(
        ("thread", "answer"),
        suspended.classify_in_background(world, "tab-b", child_view, target, _REJECTED,
                                         revision))))
    frames = _frames(world, "tab-a")
    suspended.may_commit.set()
    assert suspended.committed.wait(10)
    held["thread"].join(10)

    accepted = held["answer"]["reply"]["ok"] is True
    durable = namespace["_clips_store"]().get(identity)[0]
    landed = _where(world, name)
    assert not (accepted and durable == _REJECTED and landed == "library"), (
        f"accepted={accepted} durable={durable} landed={landed}: a decision the server accepted "
        f"was silently ignored by a transfer under a different root, and both reported success")
    assert landed == "unmoved" and len(frames) == 1


def test_f2_case_10_admission_then_a_direct_classification_request(world):
    """Scenario 10, and deliberately **hook-free**: the reservation is held by stopping the
    generator at its first yield, and the classification is an ordinary call -- the same thing a
    second browser tab's POST is at this layer. No `_hook_admission`, no `_SuspendedCommit`.
    """
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    target, identity, name = _shared_clip(world, parent_view, child_view)
    before = namespace["_clips_store"]().get(identity)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    replies = [namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                          target, {"to": _REJECTED, "rev": before[1]})
               for _ in range(5)]
    list(stream)

    assert all(r["ok"] is False and r["code"] == "transfer_in_progress" for r in replies), replies
    assert namespace["_clips_store"]().get(identity) == before, \
        "five refused classifications advanced the durable revision"
    assert _where(world, name) == "library"
    # the refusal discloses the server's own record so the tile resyncs rather than guessing
    assert replies[0]["decision"] == before[0] and replies[0]["rev"] == before[1]
    # And the exclusion is a window, not a latch: once the sort finishes the guard refuses
    # nothing. B's page is now genuinely stale -- A moved the file -- so the SAME request is
    # refused for a different and truthful reason, on the opened descriptor rather than by a
    # claim. That distinction is the point: `identity_changed` means the clip really moved;
    # `transfer_in_progress` meant somebody was moving it.
    guard = namespace["_CLIPS_TRANSFERS"]
    assert guard.active_for("tab-a") is None and guard.deciding() == 0
    assert guard.conflict("tab-b", "k:/child", [identity]) is None, \
        "the clip-level exclusion outlived the transfer that took it"
    again = namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                       target, {"to": _REJECTED, "rev": before[1]})
    assert again["ok"] is False and again["code"] == "identity_changed", again


@pytest.mark.parametrize("policy", [collision.SKIP, collision.SUFFIX])
def test_f2_case_11_collision_handling_is_untouched_by_the_identity_rule(world, policy):
    """Scenario 11. The no-overwrite guarantee is enforced by the `os.rename` call itself, and
    nothing about the conflict model touches it. Both policies are exercised against an occupied
    destination, after a cross-root refusal, so the refusal path cannot have perturbed them."""
    namespace = _build(str(world["tmp"] / "state-collide"), world["ui"],
                       poster_runner=_no_poster)
    namespace["_collision_policy"] = lambda: policy
    world["ns"] = namespace
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    target, identity, name = _shared_clip(world, parent_view, child_view)

    # occupy the destination with a DIFFERENT file of the same name
    os.makedirs(world["library"], exist_ok=True)
    with open(os.path.join(world["library"], name), "wb") as handle:
        handle.write(b"squatter")

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    refused = namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                         target, {"to": _REJECTED, "rev": 0})
    frames = list(stream)

    assert refused["ok"] is False, "the cross-root classification was admitted"
    with open(os.path.join(world["library"], name), "rb") as handle:
        assert handle.read() == b"squatter", "the occupying file was overwritten"
    names = sorted(os.listdir(world["library"]))
    if policy == collision.SKIP:
        assert not any("_01" in n for n in names), names
        assert "skipped 1" in frames[-1][_MESSAGE], frames[-1][_MESSAGE]
    else:
        assert "nested0000_01.mp4" in names, names
    assert frames, "the sort produced no terminal frame"


def test_f2_case_12_cross_root_refusals_leak_no_claim(world):
    """Scenario 12, all three terminal shapes, each measured on the guard afterwards."""
    parent_view, child_view = _two_sessions_on_nested_roots(world)
    namespace = world["ns"]
    guard = namespace["_CLIPS_TRANSFERS"]
    target, identity, _ = _shared_clip(world, parent_view, child_view)
    store = namespace["_clips_store"]()

    # (a) an abandoned generator releases the reservation, so the clip frees up
    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    assert guard.conflict("tab-b", "k:/child", [identity])[0] == "clip_in_progress"
    stream.close()
    assert guard.active_for("tab-a") is None and guard.deciding() == 0
    assert guard.conflict("tab-b", "k:/child", [identity]) is None

    # (b) a durable write failure inside a cross-root claim releases the slot
    real = store.decide
    store.decide = lambda *a, **k: (_ for _ in ()).throw(OSError("journal append failed"))
    try:
        with pytest.raises(OSError):
            namespace["_clips_decide"]("tab-b", child_view["session"], child_view["page"],
                                       target, {"to": _REJECTED, "rev": 0})
    finally:
        store.decide = real
    assert guard.deciding() == 0, "a failed cross-root commit kept its slot"

    # (c) a stale reservation cannot clear a newer one, and cannot free the clip early
    first = guard.begin("tab-a", 1, "sig", "k:/parent", [identity])
    guard.end(first)
    second = guard.begin("tab-a", 2, "sig", "k:/parent", [identity])
    assert guard.end(first) is False, "a stale release cleared a newer reservation"
    assert guard.conflict("tab-b", "k:/child", [identity])[0] == "clip_in_progress"
    assert guard.end(second) is True
    assert guard.conflict("tab-b", "k:/child", [identity]) is None


def test_f2_the_handler_claims_exactly_the_identities_it_will_act_on(world):
    """The thing excluded must be the thing done. If the claim covered a different set from the
    one `decision_of` reaches, the protection would be aimed at the wrong clips -- so the
    reservation's identities are compared against the page's own identity map."""
    namespace = world["ns"]
    parent, _ = _nested_world(world)
    _scan(world, "tab-a", folder=parent)
    view = _keep_all(world, "tab-a")

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash="tab-a"))
    next(stream)
    record = namespace["_CLIPS_TRANSFERS"].active_for("tab-a")
    assert record.identities == frozenset(
        view["identity"][entry.clip_id] for entry in view["entries"])
    assert len(record.identities) == 4, "the recursive page should carry four clips"
    assert isinstance(record.identities, frozenset), "the claimed set must be immutable"
    list(stream)


def test_f2_one_refusal_code_maps_to_one_sentence(world):
    """`_clips_transfer_busy_text` is the ONE place a code becomes a sentence, so a screen cannot
    name a reason the guard did not give. An unknown code still refuses rather than raising."""
    text = world["ns"]["_clips_transfer_busy_text"]
    assert text("transfer_in_progress") == world["ui"]["CLIPS_TRANSFER_BUSY_TEXT"]
    assert text("folder_in_progress") == world["ui"]["CLIPS_TRANSFER_FOLDER_BUSY_TEXT"]
    assert text("clip_in_progress") == world["ui"]["CLIPS_TRANSFER_CLIP_BUSY_TEXT"]
    assert text("something_invented_later") == world["ui"]["CLIPS_TRANSFER_BUSY_TEXT"]
    for code in ("transfer_in_progress", "folder_in_progress", "clip_in_progress"):
        formatted = text(code).format(done=2, total=7)
        assert "{" not in formatted, f"{code} left an unfilled field"
        assert "2 of 7" in formatted


def test_f2_the_clip_refusal_names_a_different_folder_and_discloses_no_path(world):
    """Operator-visible wording. It must not say "this folder" -- the folders really differ -- and
    it must not print the other session's root, which may not even be readable by this operator."""
    message = world["ui"]["CLIPS_TRANSFER_CLIP_BUSY_TEXT"].format(done=1, total=4)
    assert "different folder" in message
    assert "this folder" not in message
    for leak in (":\\", ":/", "C:", "/home/"):
        assert leak not in message, f"the refusal discloses a path fragment: {leak}"
    assert message.count("\n") == 1, "the refusal should stay two lines like its siblings"


def test_refresh_does_not_light_confirm_back_up_mid_sort(world):
    """Presentation, not authority -- but a screen that contradicts the Working state invites a
    press the server is going to refuse. It can only ever disable."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    _grid, _counts, confirm = namespace["_on_clips_refresh"](
        world["request"](session_hash=owner))
    list(stream)

    assert confirm == {"interactive": False}, confirm


# =============================================================================================
# 5 — the two-step confirmation is preserved
# =============================================================================================

def test_unmarked_clips_still_need_an_explicit_proceed(world):
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": _SELECTED, "rev": 0})

    gated = _frames(world, owner)
    assert len(gated) == 1 and gated[0][_PROCEED] == {"visible": True}
    signature = gated[0][_ARMED]
    for entry in view["entries"]:
        assert os.path.isfile(entry.path), "the gate moved a file"

    swept = _frames(world, owner, armed=signature)
    assert any("Moving " in f[_MESSAGE] for f in swept), "Proceed did not sort"
    assert len(os.listdir(world["library"])) == 1
    assert len(os.listdir(world["rejected"])) == 2, "the unmarked clips went to Discarded"


def test_a_signature_that_does_not_match_re_opens_the_gate_and_claims_no_slot(world):
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": _SELECTED, "rev": 0})

    frames = _frames(world, owner, armed="not-the-signature-for-this-batch")

    assert len(frames) == 1
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in frames[0][_MESSAGE]
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, \
        "an unanswered gate left the page marked busy"
    for entry in view["entries"]:
        assert os.path.isfile(entry.path)


def test_the_sweep_still_writes_no_durable_decision(world):
    """R8's rule, re-asserted through the streaming handler: an unmarked clip swept to Discarded
    stays PENDING in the store, so re-scanning shows it undecided rather than attributing a
    decision to the operator."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": _SELECTED, "rev": 0})
    unmarked = view["identity"][view["entries"][1].clip_id]

    signature = _frames(world, owner)[0][_ARMED]
    _frames(world, owner, armed=signature)

    assert namespace["_clips_store"]().get(unmarked)[0] == _PENDING, \
        "the sweep wrote a decision the operator never made"


# =============================================================================================
# 6 — collision behaviour is reused, never reimplemented
# =============================================================================================

def test_the_suffix_policy_still_takes_the_next_free_name(world):
    owner = "tab-a"
    namespace = _build(str(world["tmp"] / "state3"), world["ui"], poster_runner=_no_poster)
    namespace["_collision_policy"] = lambda: collision.SUFFIX
    world["ns"] = namespace
    _scan(world, owner)
    view = _keep_all(world, owner)
    os.makedirs(world["library"], exist_ok=True)
    taken = os.path.basename(view["entries"][0].path)
    with open(os.path.join(world["library"], taken), "wb") as handle:
        handle.write(b"first")

    message = _frames(world, owner)[-1][_MESSAGE]

    stem, suffix = os.path.splitext(taken)
    assert os.path.isfile(os.path.join(world["library"], f"{stem}_01{suffix}")), \
        os.listdir(world["library"])
    assert open(os.path.join(world["library"], taken), "rb").read() == b"first", \
        "the occupant was overwritten"
    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=3, skipped=0, failed=0, total=3) in message, message


def test_the_handler_names_no_second_naming_engine(world):
    """One collision implementation. A date stamp, a hash or a hand-rolled counter here would be
    a second policy for the operator to discover."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_clips_confirm_frames")
    body = ast.unparse(node)
    assert "_collision_policy()" in body, "the operator's saved policy must still be the input"
    for forbidden in ("strftime", "uuid", "sha256", "md5", "_01", "copy2", "copyfile",
                      "os.replace", "shutil"):
        assert forbidden not in body, f"the handler reaches {forbidden}"


def test_the_transfer_engine_is_one_implementation():
    """`sort_decided_clips` is now a list of what `iter_sorted_clips` yields. Two loops would be
    two orderings, two sets of refusals and two collision paths to keep in step."""
    source = ast.parse(open(os.path.join(os.path.dirname(_GUI), "beatsync_fork",
                                         "clips_review.py"), encoding="utf-8").read())
    collected = next(n for n in source.body
                     if isinstance(n, ast.FunctionDef) and n.name == "sort_decided_clips")
    body = [s for s in collected.body if not (isinstance(s, ast.Expr)
                                              and isinstance(s.value, ast.Constant))]
    assert len(body) == 1 and isinstance(body[0], ast.Return)
    assert "iter_sorted_clips" in ast.unparse(body[0])
    assert [a.arg for a in collected.args.args] == \
           [a.arg for a in next(n for n in source.body if isinstance(n, ast.FunctionDef)
                                and n.name == "iter_sorted_clips").args.args], \
        "the two must take the same arguments, or one of them is a second engine"


def test_the_collected_and_streaming_forms_agree(tmp_path):
    """Same entries, same order, same outcomes -- asserted against a real filesystem rather than
    argued from the refactor."""
    entries = _entries_in(_tree_of_clips(tmp_path, count=6))
    streamed = list(cr.iter_sorted_clips(entries, lambda e: _SELECTED,
                                         {_SELECTED: str(tmp_path / "out")}))
    assert [o.status for o in streamed] == [cr.MOVED] * 6
    assert [o.clip_id for o in streamed] == [e.clip_id for e in entries]

    entries2 = _entries_in(_tree_of_clips(tmp_path / "two", count=6))
    collected = cr.sort_decided_clips(entries2, lambda e: _SELECTED,
                                      {_SELECTED: str(tmp_path / "out2")})
    assert [o.status for o in collected] == [o.status for o in streamed]
    assert [o.clip_id for o in collected] == [o.clip_id for o in streamed]
    assert [o.decision for o in collected] == [o.decision for o in streamed]


# =============================================================================================
# 7 — safe terminal behaviour
# =============================================================================================

def test_an_exception_releases_the_slot_and_restores_the_controls(world, monkeypatch):
    """The one thing that must not happen on a defect: a surface left permanently disabled with
    files already moved and nothing on screen saying so."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    real = cr.move_decided_clip
    calls = []

    def exploding(entry, destination_dir, policy=collision.DEFAULT_POLICY):
        calls.append(entry)
        if len(calls) == 2:
            raise RuntimeError("a defect inside the transfer")
        return real(entry, destination_dir, policy)

    monkeypatch.setattr(cr, "move_decided_clip", exploding)
    frames = _frames(world, owner)
    monkeypatch.undo()

    final = frames[-1]
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, "the slot leaked on a defect"
    assert final[_CONFIRM] != {"interactive": False} or final[_CONFIRM] == {}, final[_CONFIRM]
    assert "a defect inside the transfer" in final[_MESSAGE], final[_MESSAGE]
    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=1, skipped=0, failed=0, total=3) in final[_MESSAGE], final[_MESSAGE]
    assert final[_ARMED] == "" and final[_PROCEED] == {"visible": False}
    # The clip that did move really moved; the other two are untouched, so a retry is safe.
    assert os.path.isfile(view["entries"][1].path) and os.path.isfile(view["entries"][2].path)
    assert _frames(world, owner), "a retry after a defect was refused"


def test_abandoning_the_stream_releases_the_slot(world):
    """Gradio closes a dropped event, which raises `GeneratorExit` at the yield. Nothing is
    undone -- each rename was independently complete -- but the page must not stay busy forever."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is not None
    stream.close()

    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, \
        "an abandoned stream left the page marked busy"


def test_a_partial_batch_republishes_what_is_really_on_disk(world):
    """The pre-R13 rule, unchanged: repainting the old page would leave already-moved clips on
    screen, and pressing Confirm again would then report them as missing."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    os.makedirs(world["library"], exist_ok=True)
    blocked = view["entries"][0]
    with open(os.path.join(world["library"], os.path.basename(blocked.path)), "wb") as handle:
        handle.write(b"taken")

    final = _frames(world, owner)[-1]

    assert os.path.basename(blocked.path) not in final[_GRID], \
        "the grid must be re-enumerated, not repainted"
    assert "Enumerated 1 clip" in final[_REPORT], final[_REPORT]
    # Still kept, because a decision is keyed on identity and that has not changed.
    assert "Kept 1" in final[_COUNTS] or "1" in final[_COUNTS], final[_COUNTS]


def test_the_report_says_nothing_was_overwritten_or_deleted(world):
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    os.makedirs(world["library"], exist_ok=True)
    with open(os.path.join(world["library"],
                           os.path.basename(view["entries"][0].path)), "wb") as handle:
        handle.write(b"taken")
    message = _frames(world, owner)[-1][_MESSAGE]
    assert "Nothing was overwritten, renamed or deleted" in message, message


def test_no_frame_leaks_a_filesystem_path_the_operator_did_not_type(world):
    """The boundary this whole surface is built on. The destinations the operator typed may of
    course appear; nothing else may."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    for frame in _frames(world, owner):
        message = frame[_MESSAGE]
        assert "state" + os.sep + "clips-review" not in message, message
        assert "posters" not in message, message


# =============================================================================================
# 8 — the R11 interaction: what a transfer may publish when the page moved on under it
# =============================================================================================

def test_a_transfer_whose_page_was_retired_mid_sort_publishes_no_page(world):
    """[R11 / F-1 interaction] The moves are not abandoned -- the operator pressed Confirm on
    that page. What the transfer must NOT do is re-scan the old root and hand the browser a page
    the server no longer holds, which is precisely the defect R11 closed."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    namespace["_CLIPS_SCANS"].invalidate(owner)             # the "Review these clips" handoff
    rest = list(stream)
    final = rest[-1]

    # The sort finished and really moved the clips.
    assert "Sorted all 3" in final[_MESSAGE], final[_MESSAGE]
    for entry in view["entries"]:
        assert not os.path.exists(entry.path)
    # But no page was published, and the screen says so rather than showing a stale grid.
    assert namespace["_clips_view"](owner) == {}, "the retired page was resurrected"
    assert world["ui"]["CLIPS_TRANSFER_PAGE_MOVED_ON_TEXT"] in final[_REPORT], final[_REPORT]
    assert world["ui"]["CLIPS_TRANSFER_STALE_NOTE_TEXT"].strip() in final[_MESSAGE]
    assert final[_CONFIRM] == {"interactive": False}
    assert "r5-tile" not in final[_GRID], "a retired page was repainted as current"


def test_a_transfer_superseded_by_a_newer_scan_does_not_republish_the_old_root(world):
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    elsewhere = _tree_of_clips(world["tmp"] / "elsewhere", count=2)

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    _scan(world, owner, folder=elsewhere)                   # the operator scans another folder
    final = list(stream)[-1]

    current = namespace["_clips_view"](owner)
    assert current["root"] == os.path.realpath(elsewhere), \
        "the finished transfer overwrote the newer page"
    assert len(current["entries"]) == 2
    assert world["ui"]["CLIPS_TRANSFER_STALE_NOTE_TEXT"].strip() in final[_MESSAGE]
    # And it still reports what it really did.
    assert "Sorted all 3" in final[_MESSAGE]
    for entry in view["entries"]:
        assert not os.path.exists(entry.path)


def test_a_handoff_during_a_sort_does_not_stop_the_sort(world):
    """Stated as an assertion because the alternative is a defensible design that was NOT
    chosen: aborting mid-batch would leave a partial sort for an unrelated navigation action."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    namespace["_CLIPS_SCANS"].invalidate(owner)
    list(stream)

    assert sorted(os.listdir(world["library"])) == \
        sorted(os.path.basename(e.path) for e in view["entries"])


def test_a_retirement_mid_sort_leaves_the_durable_decisions_intact(world):
    """The coordinator holds pages, not decisions -- R11's rule, re-asserted across a transfer."""
    owner = "tab-a"
    _scan(world, owner)
    view = _keep_all(world, owner)
    namespace = world["ns"]
    identities = [view["identity"][e.clip_id] for e in view["entries"]]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    namespace["_CLIPS_SCANS"].invalidate(owner)
    list(stream)

    store = namespace["_clips_store"]()
    for identity in identities:
        assert store.get(identity)[0] == _SELECTED, "a retirement destroyed a decision"


def test_no_coordinator_lock_is_held_across_the_moves(world):
    """Two locks, two spans, never nested. A coordinator lock held across a batch of renames
    would block the very `_clips_view` read the decision endpoint performs on every
    classification -- so a tile click would hang instead of being refused."""
    owner = "tab-a"
    _scan(world, owner)
    _keep_all(world, owner)
    namespace = world["ns"]

    stream = namespace["_clips_confirm_frames"]("", world["request"](session_hash=owner))
    next(stream)
    # Both of these take the coordinator's own locks. If one were held for the batch, these
    # would block and the test would time out rather than fail.
    assert namespace["_clips_view"](owner) != {}
    assert namespace["_CLIPS_SCANS"].newest(owner) > 0
    assert namespace["_CLIPS_SCANS"].invalidate(owner) > 0
    list(stream)


def test_the_handler_introduces_no_second_notion_of_the_current_page(world):
    """Reuse, not a parallel lifecycle. The page question is answered by the coordinator's
    generation and the transfer question by the guard; neither is reimplemented here."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    body = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                            and n.name == "_clips_confirm_frames"))
    assert "_CLIPS_SCANS.is_current(owner, generation)" in body
    for forbidden in ("_CLIPS_SCANS.begin", "_CLIPS_SCANS.publish", "_CLIPS_SCANS.discard",
                      "_CLIPS_SCANS.invalidate", "_CLIPS_SCANS.forget", "global ",
                      "new_session", "revoke"):
        assert forbidden not in body, f"the transfer reaches {forbidden}"


def test_the_guard_is_claimed_once_and_released_in_a_finally(world):
    """Structural, because the ordering is what makes every terminal path safe and no single
    behavioural test can schedule all of them."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_clips_confirm_frames")
    calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
    assert calls.count("_CLIPS_TRANSFERS.begin") == 1, "one admission gate, not two"
    assert calls.count("_CLIPS_TRANSFERS.end") == 1, "one release, not two"

    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
    assert any("_CLIPS_TRANSFERS.end" in ast.unparse(t.finalbody) for t in tries), \
        "the release must be in a finally, or an abandoned generator leaks the slot"
    for attempt in tries:
        assert not any(isinstance(n, ast.Yield) for n in ast.walk(ast.Module(
            body=list(attempt.finalbody), type_ignores=[]))), \
            "yielding from a finally raises RuntimeError when the generator is closed"
