"""UX-ZERO-KEEP: Video Review can file a page on which nothing is worth keeping.

[FORK] Digital-Union. The deployed keep-only workflow is right when at least one clip is good:
mark those, press Confirm, and agree once to sweeping the rest into Discarded. It had no answer
for the ordinary case of a bad shoot, because the shared ``SortPlan.can_sort`` wanted a Keep the
operator had no reason to make -- so marking every clip Discard still left the button dark.

Two routes forward are added, and this suite is about both of them plus everything they must not
disturb:

```
0 Keeps, >= 1 explicit Discard   ->  Confirm, then the ordinary batch-bound Proceed
0 Keeps, no decision at all      ->  "Discard all loaded clips", then the same Proceed
>= 1 Keep                        ->  unchanged, and the bulk action REFUSES rather than overriding
```

The handler is reached exactly as ``test_clips_gui_integration.py`` and
``test_clips_transfer_overlay.py`` reach it -- ``ast``-extracted from the shipped ``gui.py`` and
executed against a synthesised parent namespace, with the harness *imported* from the first of
those rather than copied, so all three suites drive one namespace. The sort, the durable store,
the registry, the single-flight guard and the filesystem are all real.

What is asserted here:

* zero Keeps plus one explicit Discard proceeds, through the unchanged two-step gate;
* zero decisions proceeds **only** through the explicit bulk action;
* the bulk action never overrides a Keep, and writes no durable decision;
* the gate's counts and destination are the real ones, and Cancel moves nothing;
* a stale page, a retired page, a changed decision and a changed destination all refuse;
* duplicate and concurrent presses admit exactly one transfer;
* a partial move reports accurate outcomes and leaves the remaining clips in place;
* the bulk action means the LOADED page -- never the folder, a later page or another session;
* and the keep-only workflow is byte-for-byte what it was.
"""

from __future__ import annotations

import ast
import os
import subprocess
import threading
from dataclasses import dataclass

import pytest

from beatsync_fork import clips_review as cr
from test_clips_gui_integration import (_GUI, _Gr, _build, _no_poster, _tree_of_clips,  # noqa: F401
                                        ui)

_SELECTED, _REJECTED, _PENDING = cr.SELECTED, cr.REJECTED, cr.PENDING

# Frame positions, named once so no assertion below counts on its fingers.
_REPORT, _GRID, _COUNTS, _CONFIRM, _MESSAGE, _PROCEED, _CANCEL, _ARMED = range(8)


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


def _confirm_frames(world, owner, armed=""):
    return list(world["ns"]["_clips_confirm_frames"](armed, world["request"](session_hash=owner)))


def _bulk_frames(world, owner, armed=""):
    """The "Discard all loaded clips" entry point, drained."""
    return list(world["ns"]["_clips_discard_all_frames"](
        armed, world["request"](session_hash=owner)))


def _decide(world, owner, entry, to):
    """One decision through the real endpoint, at whatever revision the store actually holds."""
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    identity = view["identity"][entry.clip_id]
    _, revision = namespace["_clips_store"]().get(identity)
    answer = namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                        {"to": to, "rev": revision})
    assert answer["ok"] is True, answer
    return answer


@dataclass(frozen=True)
class _Tile:
    """Everything `plan_sort` reads off an item, and deliberately nothing else.

    The pure plan serves both review surfaces precisely because it looks at `clip_id` and at no
    other attribute; a `ClipEntry` here would assert against a video-specific record and hide
    that. (`test_audio_review.py` makes the same point from the audio side.)
    """

    clip_id: str


def _junction(link, target):
    """A real NTFS directory junction, or skip. `mklink /J` needs no elevation; a symlink does."""
    made = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.normpath(str(link)),
                           os.path.normpath(str(target))],
                          capture_output=True, text=True, shell=False)
    if made.returncode != 0 or not os.path.exists(str(link)):
        pytest.skip(f"mklink refused: {made.stdout.strip()} {made.stderr.strip()}")


def _payload_of(entries):
    return {entry.clip_id: open(entry.path, "rb").read() for entry in entries}


def _filed(folder, entry):
    return os.path.join(folder, os.path.basename(entry.path))


# =============================================================================================
# 1 — zero Keeps plus one explicit Discard
# =============================================================================================

def test_the_plan_names_the_zero_keep_rule_without_widening_can_sort():
    """The rule lives on the shared plan, and `can_sort` is deliberately untouched.

    `gui._on_audio_confirm` is admitted by `can_sort` too, so widening it here would change a
    second review surface nobody asked to change -- one copy of a safety rule with two meanings,
    which is the same hazard as two copies with one.
    """
    tiles = [_Tile(f"c{n}") for n in range(3)]

    nothing = cr.plan_sort(tiles, {})
    assert nothing.can_sort is False
    assert nothing.can_sort_discard_only is False, "no decision at all is not a discard decision"
    assert nothing.can_discard_whole_page is True, "the bulk action is what acts on this page"

    one_discard = cr.plan_sort(tiles, {"c0": cr.REJECTED})
    assert one_discard.can_sort is False, "can_sort must still mean exactly one thing"
    assert one_discard.can_sort_discard_only is True
    assert one_discard.has_rejected is True
    assert one_discard.can_discard_whole_page is True

    one_keep = cr.plan_sort(tiles, {"c0": cr.SELECTED})
    assert one_keep.can_sort is True
    assert one_keep.can_sort_discard_only is False, "a kept clip is not a discard-only page"
    assert one_keep.can_discard_whole_page is False, "the bulk action must never touch a Keep"


def test_zero_keeps_and_one_discard_reaches_the_gate_and_then_files_the_page(world):
    """The owner's case, end to end on a real filesystem.

    One explicit Discard admits the press; the two unmarked clips still need the ordinary
    batch-bound Proceed before they move. Every clip ends up in Discarded with its bytes intact,
    and the Media Library is never created -- nothing was going there.
    """
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    payload = _payload_of(entries)
    _decide(world, owner, entries[0], _REJECTED)

    gated = _confirm_frames(world, owner)
    assert len(gated) == 1, "the gate must not stream"
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in gated[0][_MESSAGE]
    assert gated[0][_PROCEED] == {"visible": True} and gated[0][_ARMED]
    for entry in entries:
        assert os.path.isfile(entry.path), "the gate moved a file"

    final = _confirm_frames(world, owner, armed=gated[0][_ARMED])[-1]
    for entry in entries:
        assert os.path.isfile(_filed(world["rejected"], entry)), final[_MESSAGE]
        assert open(_filed(world["rejected"], entry), "rb").read() == payload[entry.clip_id]
        assert not os.path.exists(entry.path)
    assert not os.path.exists(world["library"]), (
        "an all-discard batch must not create the Media Library folder")
    assert "Sorted all 3" in final[_MESSAGE], final[_MESSAGE]


def test_zero_keeps_and_every_clip_explicitly_discarded_needs_no_sweep_gate(world):
    """With nothing unmarked there is nothing to sweep, so Confirm files the page directly --
    exactly as it does for a fully decided keep-and-discard page. The admission rule changed; the
    gate's own trigger did not."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    for entry in entries:
        _decide(world, owner, entry, _REJECTED)

    frames = _confirm_frames(world, owner)

    assert len(frames) > 1, "a real sort streams Working, progress and a terminal frame"
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] not in frames[-1][_MESSAGE]
    assert "Sorted all 3" in frames[-1][_MESSAGE], frames[-1][_MESSAGE]
    for entry in entries:
        assert os.path.isfile(_filed(world["rejected"], entry))


def test_a_page_with_no_decision_at_all_still_refuses_an_ordinary_confirm(world):
    """The property that must NOT be relaxed: a page nobody has judged is not filable by one
    press. The bulk action is the only route, and the refusal names it."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])

    frames = _confirm_frames(world, owner)

    assert len(frames) == 1, "a refusal must not stream"
    assert world["ui"]["CLIPS_CONFIRM_DISABLED_TEXT"].split(".")[0] in frames[0][_MESSAGE]
    assert "Discard all loaded clips" in frames[0][_MESSAGE], (
        "the refusal must name the action that does work here")
    assert frames[0][_PROCEED] == {"visible": False} and frames[0][_ARMED] == ""
    assert world["ns"]["_CLIPS_TRANSFERS"].active_for(owner) is None, "a refusal claimed a slot"
    for entry in entries:
        assert os.path.isfile(entry.path)
    assert not os.path.exists(world["library"]) and not os.path.exists(world["rejected"])


# =============================================================================================
# 2 — the explicit bulk action
# =============================================================================================

def test_discard_all_gates_a_page_with_no_decisions_and_then_files_it(world):
    """The second route: no clip classified at all, one explicit action, one confirmation."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    payload = _payload_of(entries)

    gated = _bulk_frames(world, owner)
    assert len(gated) == 1, "the gate must not stream, and must not announce work"
    assert world["ui"]["CLIPS_DISCARD_ALL_GATE_HEADING"] in gated[0][_MESSAGE]
    assert gated[0][_PROCEED] == {"visible": True} and gated[0][_CANCEL] == {"visible": True}
    assert gated[0][_ARMED].startswith(cr.DISCARD_ALL_ARM_PREFIX), (
        "the arming must record WHICH confirmation the operator gave")
    for entry in entries:
        assert os.path.isfile(entry.path), "the bulk gate moved a file"

    final = _confirm_frames(world, owner, armed=gated[0][_ARMED])[-1]
    for entry in entries:
        assert os.path.isfile(_filed(world["rejected"], entry)), final[_MESSAGE]
        assert open(_filed(world["rejected"], entry), "rb").read() == payload[entry.clip_id]
        assert not os.path.exists(entry.path)
    assert not os.path.exists(world["library"])
    assert "Sorted all 3" in final[_MESSAGE], final[_MESSAGE]


def test_the_bulk_gate_is_unconditional_even_when_every_clip_is_already_discarded(world):
    """An ordinary Confirm needs no gate for a fully decided page. The bulk action always shows
    one anyway: it is a statement about clips as a batch, and the counts and the destination are
    exactly what the operator has to see before it happens."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    for entry in entries:
        _decide(world, owner, entry, _REJECTED)

    frames = _bulk_frames(world, owner)

    assert len(frames) == 1, "the bulk action must confirm before it files"
    assert world["ui"]["CLIPS_DISCARD_ALL_GATE_HEADING"] in frames[0][_MESSAGE]
    for entry in entries:
        assert os.path.isfile(entry.path)


def test_the_bulk_action_refuses_a_page_carrying_a_keep_and_overrides_nothing(world):
    """Requirement, stated as the refusal it is: "Discard all" never means "and the ones you
    marked Keep as well". The operator is told, and the Keep is still a Keep afterwards."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    _decide(world, owner, entries[0], _SELECTED)

    frames = _bulk_frames(world, owner)

    assert len(frames) == 1, frames
    assert world["ui"]["CLIPS_DISCARD_ALL_HAS_KEEPS_TEXT"].format(kept=1) in frames[0][_MESSAGE]
    assert frames[0][_PROCEED] == {"visible": False}, "a refusal must not open the gate"
    assert frames[0][_ARMED] == "", "a refusal must arm nothing"
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None
    for entry in entries:
        assert os.path.isfile(entry.path), "the refusal moved a file"
    assert not os.path.exists(world["library"]) and not os.path.exists(world["rejected"])
    # and the decision is exactly as the operator left it
    store = namespace["_clips_store"]()
    assert store.get(view["identity"][entries[0].clip_id])[0] == _SELECTED


def test_a_keep_made_after_the_bulk_gate_was_armed_stops_the_batch(world):
    """The arming is a change detector, so a Keep arriving between the gate and the Proceed voids
    it. The alternative -- honouring an agreement the operator has since contradicted -- is the
    "accepted but ignored" shape R13's F-1 exists to prevent, reached through the gate."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])

    armed = _bulk_frames(world, owner)[0][_ARMED]
    _decide(world, owner, entries[0], _SELECTED)

    frames = _confirm_frames(world, owner, armed=armed)

    assert len(frames) == 1, frames
    assert world["ui"]["CLIPS_DISCARD_ALL_HAS_KEEPS_TEXT"].format(kept=1) in frames[0][_MESSAGE]
    for entry in entries:
        assert os.path.isfile(entry.path), "a voided arming moved a file"


def test_the_bulk_sweep_writes_no_durable_decision(world):
    """"For this batch only" is the contract the sweep has always had, and the bulk action is the
    same sweep. A clip that was never classified must come back undecided, not as a rejection the
    operator never made -- which is exactly what the roadmap's "not pre-written as durable
    decisions merely to enable the bulk action" requires."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    view = namespace["_clips_view"](owner)
    identities = namespace["_clips_identities"](view)

    armed = _bulk_frames(world, owner)[0][_ARMED]
    store = namespace["_clips_store"]()
    assert all(store.get(identity) == (_PENDING, 0) for identity in identities), (
        "arming the gate wrote a decision")

    _confirm_frames(world, owner, armed=armed)

    snapshot = store.snapshot_of(identities)
    for identity in identities:
        assert snapshot.get(identity, (_PENDING, 0))[0] == _PENDING, (
            "the bulk sweep wrote a REJECTED decision the operator never made")


def test_cancelling_the_bulk_gate_changes_absolutely_nothing(world):
    """Cancel is the existing one, reused: it closes the gate, clears the arming, and touches
    neither a decision nor a file."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    before = {entry.path: open(entry.path, "rb").read() for entry in entries}

    _bulk_frames(world, owner)
    cancelled = namespace["_on_clips_sweep_cancel"](world["request"](session_hash=owner))

    assert world["ui"]["CLIPS_SWEEP_CANCELLED_TEXT"] in cancelled[_MESSAGE]
    assert cancelled[_PROCEED] == {"visible": False} and cancelled[_CANCEL] == {"visible": False}
    assert cancelled[_ARMED] == "", "Cancel must clear the arming"
    for path, payload in before.items():
        assert os.path.isfile(path) and open(path, "rb").read() == payload
    assert not os.path.exists(world["library"]) and not os.path.exists(world["rejected"])


# =============================================================================================
# 3 — the gate says what will really happen
# =============================================================================================

def test_the_bulk_gate_states_the_exact_counts_and_the_real_destination(world):
    """A one-time confirmation is worth nothing if it describes a different batch. The counts come
    from the server's own plan and the folder is the RESOLVED destination the moves will use, not
    the text that was typed."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    _decide(world, owner, entries[0], _REJECTED)

    message = _bulk_frames(world, owner)[0][_MESSAGE]

    assert "0 clip(s) selected to KEEP." in message, message
    assert "1 clip(s) explicitly rejected." in message, message
    assert "2 unmarked clip(s) will ALSO be moved to Discarded." in message, message
    assert "Nothing will be permanently deleted." in message, message
    assert os.path.realpath(world["rejected"]) in message, message
    assert os.path.realpath(world["library"]) not in message, (
        "nothing is going to the Media Library, so the gate must not name it")


def test_a_destination_that_stopped_validating_refuses_before_the_gate(world):
    """Destination validation runs on every press, against the folders carried on the view -- not
    re-read from the widgets, and not trusted because the scan once accepted it.

    The destination becomes a junction into the review folder *after* the scan, which is the
    realistic shape of this: containment is decided on resolved real paths, so an alias created
    later is caught exactly as a typed one would have been.
    """
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    assert not os.path.exists(world["rejected"]), "mklink needs the link name to be free"
    _junction(world["rejected"], world["media"])

    frames = _bulk_frames(world, owner)

    assert len(frames) == 1, frames
    assert "no longer validate" in frames[0][_MESSAGE], frames[0][_MESSAGE]
    assert frames[0][_ARMED] == "" and frames[0][_PROCEED] == {"visible": False}
    for entry in entries:
        assert os.path.isfile(entry.path)


def test_an_arming_from_a_different_batch_cannot_authorise_a_bulk_discard(world):
    """The signature is a change detector, not a credential. A stale or forged one simply fails to
    match a freshly derived plan, which shows the gate again rather than filing anything."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])

    for armed in (cr.DISCARD_ALL_ARM_PREFIX + "not-this-batch",
                  "not-this-batch",
                  cr.DISCARD_ALL_ARM_PREFIX):
        frames = _bulk_frames(world, owner, armed=armed)
        assert len(frames) == 1, (armed, frames)
        assert world["ui"]["CLIPS_DISCARD_ALL_GATE_HEADING"] in frames[0][_MESSAGE], armed
        for entry in entries:
            assert os.path.isfile(entry.path), f"{armed!r} moved a file"


def test_the_arming_token_round_trips_and_is_total():
    """Text in, text out. Anything that is not a usable token reads as "nothing is armed", which
    leads to the gate -- the direction in which nothing moves."""
    assert cr.read_arm_token(cr.arm_token("abc")) == ("abc", False)
    assert cr.read_arm_token(cr.arm_token("abc", discard_all=True)) == ("abc", True)
    assert cr.arm_token("abc") == "abc", "an ordinary arming is the bare signature, as before"
    assert cr.arm_token("", discard_all=True) == "", "an empty signature arms nothing"
    for rubbish in (None, 0, 3, [], {}, b"abc", ""):
        assert cr.read_arm_token(rubbish) == ("", False), rubbish


# =============================================================================================
# 4 — the loaded page, and nothing else
# =============================================================================================

def test_the_bulk_action_files_only_the_loaded_page_and_not_the_folder(world, tmp_path):
    """"All loaded clips" means the clips the server is holding, which is one page. A second page
    of the same folder, and the folder's own subdirectory, must be untouched."""
    owner = "tab-a"
    nested = _tree_of_clips(tmp_path / "media_nested", count=2)
    os.rename(nested, os.path.join(world["media"], "more"))
    deeper = [os.path.join(world["media"], "more", name)
              for name in sorted(os.listdir(os.path.join(world["media"], "more")))]
    _scan(world, owner, page_size=2)                       # page 1 of a 5-clip recursive scan
    page = list(world["ns"]["_clips_view"](owner)["entries"])
    assert len(page) == 2, "this test needs a page smaller than the folder"
    untouched = [path for path in
                 [os.path.join(world["media"], name)
                  for name in sorted(os.listdir(world["media"]))
                  if os.path.isfile(os.path.join(world["media"], name))] + deeper
                 if path not in {entry.path for entry in page}]
    assert untouched, "this test needs clips outside the loaded page"

    armed = _bulk_frames(world, owner)[0][_ARMED]
    final = _confirm_frames(world, owner, armed=armed)[-1]

    assert "Sorted all 2" in final[_MESSAGE], final[_MESSAGE]
    for entry in page:
        assert os.path.isfile(_filed(world["rejected"], entry))
    for path in untouched:
        assert os.path.isfile(path), f"{path} was filed although it was not on the loaded page"

    # And the review advances: the republished page is the NEXT batch of what is really on disk.
    advanced = world["ns"]["_clips_view"](owner)["entries"]
    assert advanced, "a completed batch must publish the next one"
    assert {entry.path for entry in advanced} <= set(untouched), (
        "the next batch must be clips that are still there")
    assert not {entry.path for entry in advanced} & {entry.path for entry in page}


def test_another_browser_sessions_page_is_untouched_by_a_bulk_discard(world, tmp_path):
    """Pages are per owner. One operator discarding their whole page must not reach into another
    session's page, and the other session's decisions must survive."""
    elsewhere = _tree_of_clips(tmp_path / "elsewhere", count=2)
    namespace = world["ns"]
    _scan(world, "tab-a")
    world["ns"]["_on_clips_scan"](elsewhere, True, world["library"],
                                  str(tmp_path / "other-rejected"), 100,
                                  world["request"](session_hash="tab-b"))
    mine = list(namespace["_clips_view"]("tab-a")["entries"])
    theirs = list(namespace["_clips_view"]("tab-b")["entries"])
    _decide(world, "tab-b", theirs[0], _SELECTED)

    armed = _bulk_frames(world, "tab-a")[0][_ARMED]
    _confirm_frames(world, "tab-a", armed=armed)

    for entry in mine:
        assert os.path.isfile(_filed(world["rejected"], entry))
    for entry in theirs:
        assert os.path.isfile(entry.path), "another session's clip was filed"
    assert namespace["_clips_store"]().get(
        namespace["_clips_view"]("tab-b")["identity"][theirs[0].clip_id])[0] == _SELECTED


def test_a_retired_page_refuses_the_bulk_action(world):
    """R11 / F-1: the Raw Cutter's "Review these clips" retires the page the operator is looking
    at. Every route to a transfer must then refuse because the server holds no page -- not because
    a button went grey, and the bulk action is no exception."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    armed = _bulk_frames(world, owner)[0][_ARMED]
    namespace["_CLIPS_SCANS"].invalidate(owner)

    for frames in (_bulk_frames(world, owner), _confirm_frames(world, owner, armed=armed)):
        assert len(frames) == 1, frames
        assert "Nothing has been scanned on this page." in frames[0][_MESSAGE], frames[0]
        assert frames[0][_ARMED] == ""
    for entry in entries:
        assert os.path.isfile(entry.path), "a retired page filed a clip"


def test_a_bulk_discard_on_a_superseded_page_repaints_the_authoritative_one(world, tmp_path):
    """The R13/R11 interaction, through the new entry point: the moves are not abandoned, but the
    finished transfer must not hand the browser a page the server no longer holds."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    armed = _bulk_frames(world, owner)[0][_ARMED]
    elsewhere = _tree_of_clips(tmp_path / "elsewhere", count=2)

    stream = namespace["_clips_confirm_frames"](armed, world["request"](session_hash=owner))
    next(stream)                                            # the Working frame
    _scan(world, owner, folder=elsewhere)                   # the operator scans another folder
    final = list(stream)[-1]

    assert "Sorted all 3" in final[_MESSAGE], final[_MESSAGE]
    for entry in entries:
        assert not os.path.exists(entry.path)
    assert world["ui"]["CLIPS_TRANSFER_STALE_NOTE_TEXT"].strip() in final[_MESSAGE]
    assert namespace["_clips_view"](owner)["root"] == os.path.realpath(elsewhere)


# =============================================================================================
# 5 — single-flight, duplicates and partial results
# =============================================================================================

def test_a_second_bulk_press_during_the_sort_is_refused_by_the_server(world):
    """Single-flight is `_CLIPS_TRANSFERS`, and the bulk action reaches it through exactly the
    code Confirm does. A disabled button is a courtesy that does not survive a second tab."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    armed = _bulk_frames(world, owner)[0][_ARMED]

    stream = namespace["_clips_confirm_frames"](armed, world["request"](session_hash=owner))
    next(stream)                                            # a transfer now holds the slot
    refused = _bulk_frames(world, owner, armed=armed)
    rest = list(stream)

    assert len(refused) == 1, refused
    assert world["ui"]["CLIPS_TRANSFER_BUSY_TEXT"].splitlines()[0].split("(")[0].strip() \
        in refused[0][_MESSAGE], refused[0][_MESSAGE]
    assert "Sorted all 3" in rest[-1][_MESSAGE], rest[-1][_MESSAGE]
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, "the slot leaked"


def test_many_concurrent_bulk_presses_admit_exactly_one_sort(world):
    """Eight real presses against the real handler, overlapping deterministically.

    The winner is held at its first `note_progress` -- the call immediately before the Working
    frame, with the reservation already taken -- until every press has been through admission.
    Without that hold a three-clip sort can finish before a straggler reaches `begin`, the
    straggler is then *legitimately* admitted, and the assertion would be about scheduling luck
    rather than about concurrency. The same technique, and the same reason, as
    `test_clips_transfer_overlay.py::test_many_concurrent_presses_admit_exactly_one_sort`.
    """
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    armed = _bulk_frames(world, owner)[0][_ARMED]
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
        if done == 0:
            assert everyone_tried.wait(30), "not every press reached admission"
        return real_progress(reservation, done, total)

    guard.begin = counting_begin
    guard.note_progress = holding_progress
    results, start = [], threading.Barrier(presses)

    def press():
        start.wait(30)
        results.append(list(namespace["_clips_discard_all_frames"](
            armed, world["request"](session_hash=owner))))

    threads = [threading.Thread(target=press, daemon=True) for _ in range(presses)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not [t for t in threads if t.is_alive()], "a press hung"

    assert len(attempted) == presses, f"only {len(attempted)} of {presses} reached admission"
    sorted_runs = [r for r in results if any("Moving " in f[_MESSAGE] for f in r)]
    assert len(sorted_runs) == 1, f"{len(sorted_runs)} presses sorted at once"
    assert namespace["_CLIPS_TRANSFERS"].active_for(owner) is None, "the slot leaked"
    # Each clip was filed exactly once, under its own name: no duplicate transfer, and no
    # invented suffix papering over a second attempt at the same batch.
    assert sorted(os.listdir(world["rejected"])) == sorted(
        os.path.basename(entry.path) for entry in entries), os.listdir(world["rejected"])


def test_a_partial_bulk_move_reports_truthfully_and_keeps_the_rest_in_place(world):
    """A taken destination name is a refusal this code makes on purpose: that clip stays exactly
    where it is, the others are filed, and the report says moved / skipped / failed of total
    rather than collapsing a healthy no-overwrite refusal into a malfunction."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])
    blocked = entries[0]
    os.makedirs(world["rejected"], exist_ok=True)
    with open(_filed(world["rejected"], blocked), "wb") as handle:
        handle.write(b"i was here first")
    original = open(blocked.path, "rb").read()

    armed = _bulk_frames(world, owner)[0][_ARMED]
    final = _confirm_frames(world, owner, armed=armed)[-1]

    assert world["ui"]["CLIPS_TRANSFER_TALLY_TEXT"].format(
        moved=2, skipped=1, failed=0, total=3) in final[_MESSAGE], final[_MESSAGE]
    assert os.path.isfile(blocked.path), "a skipped clip must stay where it is"
    assert open(blocked.path, "rb").read() == original
    assert open(_filed(world["rejected"], blocked), "rb").read() == b"i was here first", (
        "the occupant was overwritten")
    for entry in entries[1:]:
        assert os.path.isfile(_filed(world["rejected"], entry))
        assert not os.path.exists(entry.path)
    # The remaining clip is still reviewable: the republished page is what is really on disk.
    remaining = namespace["_clips_view"](owner)["entries"]
    assert [entry.path for entry in remaining] == [blocked.path], (
        "the unfiled clip must stay retryable on screen")


# =============================================================================================
# 6 — the keep-only workflow is unchanged
# =============================================================================================

def test_the_keep_only_workflow_is_unchanged(world):
    """The regression that matters most: the deployed workflow is exactly what it was. One Keep,
    Confirm, the gate, Proceed -- kept clip to the Media Library, the rest to Discarded."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    payload = _payload_of(entries)
    _decide(world, owner, entries[0], _SELECTED)

    gated = _confirm_frames(world, owner)
    assert len(gated) == 1
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in gated[0][_MESSAGE]
    assert not gated[0][_ARMED].startswith(cr.DISCARD_ALL_ARM_PREFIX), (
        "an ordinary sweep arming must stay the bare signature")
    assert os.path.realpath(world["library"]) in gated[0][_MESSAGE], (
        "a kept clip means the Media Library is named")

    final = _confirm_frames(world, owner, armed=gated[0][_ARMED])[-1]
    assert open(_filed(world["library"], entries[0]), "rb").read() == payload[entries[0].clip_id]
    for entry in entries[1:]:
        assert os.path.isfile(_filed(world["rejected"], entry))
    assert "Nothing was deleted" in final[_MESSAGE], final[_MESSAGE]


def test_a_mixed_page_still_files_keeps_and_discards_to_their_own_folders(world):
    """Mixed Keep/Discard, with nothing unmarked: no gate, two destinations, no sweep sentence."""
    owner = "tab-a"
    _scan(world, owner)
    entries = list(world["ns"]["_clips_view"](owner)["entries"])
    _decide(world, owner, entries[0], _SELECTED)
    for entry in entries[1:]:
        _decide(world, owner, entry, _REJECTED)

    final = _confirm_frames(world, owner)[-1]

    assert os.path.isfile(_filed(world["library"], entries[0])), final[_MESSAGE]
    for entry in entries[1:]:
        assert os.path.isfile(_filed(world["rejected"], entry))
    assert "unmarked" not in final[_MESSAGE], final[_MESSAGE]


def test_confirm_is_enabled_by_a_discard_as_well_as_by_a_keep(world):
    """[R5-P3 / F8] The button still moves only from the durable record, in the two places that
    always moved it -- and the record now admits a page whose only decisions are Discards."""
    owner = "tab-a"
    _scan(world, owner)
    namespace = world["ns"]
    entries = list(namespace["_clips_view"](owner)["entries"])

    assert namespace["_on_clips_refresh"](world["request"](session_hash=owner))[2] == \
        {"interactive": False}, "a page with no decision must leave Confirm dark"

    _decide(world, owner, entries[0], _REJECTED)
    assert namespace["_on_clips_refresh"](world["request"](session_hash=owner))[2] == \
        {"interactive": True}, "one explicit Discard must admit the page"

    _decide(world, owner, entries[0], _PENDING)
    assert namespace["_on_clips_refresh"](world["request"](session_hash=owner))[2] == \
        {"interactive": False}, "withdrawing the only decision must fail closed again"


# =============================================================================================
# 7 — structure: one transfer path, not two
# =============================================================================================

def test_the_bulk_entry_point_adds_no_second_sorting_path():
    """The whole safety argument for a third button: it sets one keyword-only flag and delegates.
    Every check, refusal, claim and move is the one `_clips_confirm_frames` performs.

    Asserted over the syntax tree rather than as a substring, because the function's own docstring
    explains what it must not reach and a substring ban would fire on the prose explaining it --
    a trap this repository has tripped five times.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_clips_discard_all_frames")
    # The docstring, then exactly one statement: the delegation and nothing else.
    statements = [s for s in node.body if not (isinstance(s, ast.Expr)
                                               and isinstance(s.value, ast.Constant))]
    assert len(statements) == 1, ast.unparse(node)
    assert ast.unparse(statements[0]) == \
        "yield from _clips_confirm_frames(armed, request, discard_all=True)", \
        ast.unparse(statements[0])

    called = {n.func.id for n in ast.walk(node)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert called == {"_clips_confirm_frames"}, called
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    for forbidden in ("_CLIPS_TRANSFERS", "_CLIPS_SCANS", "_clips_store", "fork_clips",
                      "fork_transfer", "os", "_clips_confirm_only", "_clips_working_frame"):
        assert forbidden not in names, (
            f"the bulk entry point must not reach {forbidden} itself")


def test_the_bulk_flag_is_keyword_only_and_is_not_a_widget_value():
    """`discard_all` must be unreachable from the browser. Keyword-only means Gradio -- which
    passes a click's `inputs` positionally -- cannot supply it, and the click that does set it
    goes through the entry point above rather than through the generator directly."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_clips_confirm_frames")
    assert [arg.arg for arg in node.args.args] == ["armed", "request"], ast.unparse(node.args)
    assert [arg.arg for arg in node.args.kwonlyargs] == ["discard_all"], ast.unparse(node.args)

    source = open(_GUI, encoding="utf-8").read()
    registration = source[source.index("clips_discard_all_btn.click("):]
    registration = registration[:registration.index(")\n")]
    assert "fn=_clips_discard_all_frames" in registration, registration
    assert "inputs=[clips_armed_state]" in registration, registration
    assert "concurrency_limit=None" in registration, (
        "a lane of 1 would QUEUE a duplicate press instead of letting the server refuse it")
