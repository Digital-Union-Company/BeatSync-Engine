"""C3-R1A: the pure render-lifecycle / cancellation contract.

`beatsync_fork/render_worker.py` is stdlib-only, so it is imported and exercised as ordinary code on
a bare interpreter. Five properties carry this module, and each has a section:

1. **`RenderCancelled` is an ordinary `Exception`, and that is a decision with consequences.**
   It is *deliberately* catchable by a broad `except Exception`, so every catch site on the render
   path has to name it explicitly before its generic handler. A `BaseException` would have skipped
   those handlers for free — and would also have skipped cleanup that legitimately needs to run.
2. **Lifecycle state is monotonic, and terminal means terminal.** An illegal transition raises; a
   transition requested *after* a terminal state silently does nothing. Those two behaviours look
   inconsistent and are not: the first is a programming error, the second is an unavoidable race
   (an abandoned stream's finalizer running after the normal completion path) that must not
   resurrect a finished lifecycle.
3. **Cancellation intent is one `threading.Event`, and setting it never fails.** `request_cancel()`
   comes from a *different Gradio event* than the render, so it must be total: idempotent, never
   blocking, never raising, and never dependent on what state the render happens to be in.
4. **One lifecycle is one invocation.** Identity is minted once and immutable; two lifecycles share
   no state.
5. **It owns nothing that runs.** No Gradio object, no `Popen`, no path, no media or cache identity,
   and no module-level registry of lifecycles.

What this file deliberately does NOT test: *where* the boundary checks are placed in the pipeline,
and whether a real FFmpeg child actually stops. That is `tests/test_render_cancellation.py` — this
module exposes the flag and says nothing about when anyone reads it.
"""

from __future__ import annotations

import ast
import copy
import os
import threading

import pytest

from beatsync_fork import render_worker as rw

_MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "beatsync_fork", "render_worker.py",
)

STATE = rw.RenderLifecycleState
KIND = rw.RenderOutcomeKind
TERMINAL = (STATE.FINISHED, STATE.FAILED, STATE.CANCELLED)


def _lifecycle(invocation_id="inv-1"):
    return rw.RenderLifecycle(invocation_id=invocation_id)


def _running(invocation_id="inv-1"):
    life = _lifecycle(invocation_id)
    life.mark_running()
    return life


# ===========================================================================
# 1. RenderCancelled — an ordinary Exception, on purpose
# ===========================================================================


def test_render_cancelled_is_an_ordinary_exception_not_a_base_exception():
    """The whole catch-site discipline in the pipeline rests on this."""
    assert issubclass(rw.RenderCancelled, Exception)
    # Not a BaseException-only escape, and not one of the three that already are.
    for sibling in (KeyboardInterrupt, SystemExit, GeneratorExit):
        assert not issubclass(rw.RenderCancelled, sibling)


def test_render_cancelled_is_caught_by_a_broad_except_exception():
    """Measured rather than assumed, because the consequence is the opposite of intuition.

    Being catchable is exactly why `ffmpeg_processing`, `audio_mixdown`, `video_processor`,
    `auto_mode/__init__` and `gui.py` each need an explicit `except RenderCancelled:` placed BEFORE
    their generic handler. If this ever stopped being true, every one of those clauses would become
    dead code and would be deleted by the next person who noticed — and a future broad handler added
    without one would silently start swallowing cancellations again.
    """
    caught = None
    try:
        raise rw.RenderCancelled("stop")
    except Exception as exc:          # noqa: BLE001 - that is the property under test
        caught = exc
    assert isinstance(caught, rw.RenderCancelled)

    # and a named clause placed first wins, which is the pattern the pipeline uses
    order = []
    try:
        try:
            raise rw.RenderCancelled("stop")
        except rw.RenderCancelled:
            order.append("typed")
            raise
        except Exception:             # noqa: BLE001 - unreachable by design
            order.append("generic")   # pragma: no cover
    except rw.RenderCancelled:
        order.append("propagated")
    assert order == ["typed", "propagated"]


def test_render_cancelled_carries_the_invocation_id_in_its_message():
    """A log line must say *which* render stopped, not merely that one did."""
    life = _running("inv-abc-123")
    life.request_cancel()
    with pytest.raises(rw.RenderCancelled) as info:
        life.raise_if_cancelled()
    assert "inv-abc-123" in str(info.value)


# ===========================================================================
# 2. Lifecycle state — monotonic, and terminal means terminal
# ===========================================================================


def test_the_five_outcome_kinds_and_their_stable_values():
    """The values are persisted nowhere, but they are compared and reported; pin them anyway."""
    assert {k.name for k in KIND} == {
        "SUCCESS", "CANDIDATE_LOCAL", "SHARED_FATAL", "CANCELLED", "UNKNOWN_FATAL"}
    assert KIND.SUCCESS.value == "success"
    assert KIND.CANCELLED.value == "cancelled"
    assert KIND.CANDIDATE_LOCAL.value == "candidate_local"
    assert KIND.SHARED_FATAL.value == "shared_fatal"
    assert KIND.UNKNOWN_FATAL.value == "unknown_fatal"


def test_the_seven_lifecycle_states_and_exactly_three_terminal_ones():
    assert {s.name for s in STATE} == {
        "STARTING", "RUNNING", "CANCEL_REQUESTED", "CANCELLING",
        "FINISHED", "FAILED", "CANCELLED"}
    assert rw._TERMINAL_STATES == frozenset(TERMINAL)
    # a terminal state has no outgoing edges at all
    for state in TERMINAL:
        assert state not in rw._ALLOWED_TRANSITIONS


def test_a_fresh_lifecycle_starts_at_starting_and_is_not_terminal():
    life = _lifecycle()
    assert life.state is STATE.STARTING
    assert life.is_terminal() is False
    assert life.cancel_requested() is False
    life.raise_if_cancelled()          # must not raise


@pytest.mark.parametrize("target", [STATE.RUNNING, STATE.FAILED, STATE.CANCELLED])
def test_starting_may_advance_to_running_or_straight_to_a_terminal_state(target):
    """A render refused at the gate never runs, so STARTING -> terminal must be legal."""
    life = _lifecycle()
    if target is STATE.RUNNING:
        life.mark_running()
    else:
        life.mark_terminal(target)
    assert life.state is target


def test_starting_may_not_skip_to_cancelling():
    """CANCELLING describes work being wound down. Nothing has started yet."""
    life = _lifecycle()
    with pytest.raises(ValueError):
        life.mark_cancelling()
    assert life.state is STATE.STARTING, "a rejected transition must not mutate the state"


def test_the_documented_happy_path_and_the_documented_cancel_path():
    done = _lifecycle()
    done.mark_running()
    done.mark_terminal(STATE.FINISHED)
    assert done.state is STATE.FINISHED and done.is_terminal()

    stopped = _lifecycle()
    stopped.mark_running()
    stopped.request_cancel()                      # RUNNING -> CANCEL_REQUESTED
    assert stopped.state is STATE.CANCEL_REQUESTED
    stopped.mark_cancelling()                     # CANCEL_REQUESTED -> CANCELLING
    assert stopped.state is STATE.CANCELLING
    stopped.mark_terminal(STATE.CANCELLED)
    assert stopped.state is STATE.CANCELLED and stopped.is_terminal()


def test_a_cancelling_render_may_still_finish_or_fail():
    """Boundary-only cancellation means the work in flight may well complete first."""
    for ending in (STATE.FINISHED, STATE.FAILED):
        life = _running()
        life.request_cancel()
        life.mark_cancelling()
        life.mark_terminal(ending)
        assert life.state is ending


def test_going_backwards_is_rejected_rather_than_ignored():
    """A programming error, not a race — so it must be loud."""
    life = _running()
    life.request_cancel()
    life.mark_cancelling()
    with pytest.raises(ValueError):
        life.mark_running()
    assert life.state is STATE.CANCELLING


@pytest.mark.parametrize("terminal", TERMINAL)
@pytest.mark.parametrize("later", [STATE.FINISHED, STATE.FAILED, STATE.CANCELLED])
def test_a_transition_after_a_terminal_state_silently_does_nothing(terminal, later):
    """**Load-bearing, and deliberately NOT a raise.**

    Both mutex-owning wrappers mark terminal on the normal path and again, defensively, in a
    `finally` — and an abandoned generator's finalizer can run after a newer code path already
    finished the lifecycle. That race is unavoidable and harmless, so the late caller must find
    nothing to do rather than explode inside a `finally` (where the exception would mask whatever
    was actually propagating). What it must NEVER do is resurrect or rewrite a finished lifecycle.
    """
    life = _running()
    life.mark_terminal(terminal)
    life.mark_terminal(later)                     # no raise
    assert life.state is terminal, "a terminal lifecycle was rewritten"
    assert life.is_terminal()
    # the non-terminal movers are equally inert
    life.mark_running()
    life.mark_cancelling()
    assert life.state is terminal


@pytest.mark.parametrize("not_terminal", [STATE.STARTING, STATE.RUNNING,
                                          STATE.CANCEL_REQUESTED, STATE.CANCELLING])
def test_mark_terminal_refuses_a_non_terminal_state(not_terminal):
    """It is the one method whose name is a promise about the argument."""
    life = _running()
    with pytest.raises(ValueError):
        life.mark_terminal(not_terminal)
    assert life.state is STATE.RUNNING


def test_every_edge_in_the_table_is_reachable_and_nothing_outside_it_is():
    """Exhaustive over the declared table, so a hand-edited entry cannot go unexercised."""
    for source, allowed in rw._ALLOWED_TRANSITIONS.items():
        for target in STATE:
            life = _lifecycle()
            # drive the lifecycle to `source` along the documented path
            for step in {
                STATE.STARTING: (),
                STATE.RUNNING: (STATE.RUNNING,),
                STATE.CANCEL_REQUESTED: (STATE.RUNNING, STATE.CANCEL_REQUESTED),
                STATE.CANCELLING: (STATE.RUNNING, STATE.CANCEL_REQUESTED, STATE.CANCELLING),
            }[source]:
                if step is STATE.CANCEL_REQUESTED:
                    life.request_cancel()
                else:
                    life._transition(step)
            assert life.state is source

            if target in allowed:
                life._transition(target)
                assert life.state is target
            else:
                with pytest.raises(ValueError):
                    life._transition(target)
                assert life.state is source


# ===========================================================================
# 3. Cancellation intent — one Event, and setting it is total
# ===========================================================================


def test_request_cancel_is_idempotent_and_never_raises():
    life = _running()
    for _ in range(5):
        life.request_cancel()
    assert life.cancel_requested() is True
    assert life.state is STATE.CANCEL_REQUESTED


@pytest.mark.parametrize("state_setup", ["starting", "running", "cancelling", "finished",
                                         "failed", "cancelled"])
def test_request_cancel_sets_the_flag_from_any_state_including_terminal(state_setup):
    """The Cancel click arrives from another Gradio event and cannot know the render's state.

    Setting the flag must therefore be unconditional. Whether anybody still *reads* it is a
    different question — a finished render simply has no boundary left to check it at — but
    `request_cancel()` must not raise, and must not be a no-op that depends on timing.
    """
    life = _lifecycle()
    if state_setup != "starting":
        life.mark_running()
    if state_setup == "cancelling":
        life.request_cancel()
        life.mark_cancelling()
        life._cancel_event.clear()            # prove the next call is what sets it
    elif state_setup in ("finished", "failed", "cancelled"):
        life.mark_terminal({"finished": STATE.FINISHED, "failed": STATE.FAILED,
                            "cancelled": STATE.CANCELLED}[state_setup])

    before = life.state
    life.request_cancel()
    assert life.cancel_requested() is True

    if state_setup == "running":
        assert life.state is STATE.CANCEL_REQUESTED, "the one advisory nudge"
    else:
        assert life.state is before, \
            "request_cancel must only ever nudge RUNNING -> CANCEL_REQUESTED"


def test_the_state_nudge_happens_only_from_running():
    """The render thread owns CANCELLING and the terminal states; Cancel owns the flag."""
    for setup, expected in (
        (STATE.STARTING, STATE.STARTING),
        (STATE.RUNNING, STATE.CANCEL_REQUESTED),
    ):
        life = _lifecycle()
        if setup is STATE.RUNNING:
            life.mark_running()
        life.request_cancel()
        assert life.state is expected


def test_raise_if_cancelled_is_the_one_boundary_call():
    life = _running()
    life.raise_if_cancelled()                 # not requested -> total no-op
    life.request_cancel()
    with pytest.raises(rw.RenderCancelled):
        life.raise_if_cancelled()
    # repeatable: a boundary check never consumes the request
    with pytest.raises(rw.RenderCancelled):
        life.raise_if_cancelled()
    assert life.cancel_requested() is True


def test_reaching_a_terminal_state_does_not_clear_the_request():
    """A batch derives its terminal state from `cancel_requested()` AFTER the candidates ran.

    `render_selected_variants_guarded`'s `finally` asks "was this event cancelled?" and lets that
    win over the last candidate's individual outcome. If finishing cleared the flag, a cancellation
    that landed while the final candidate was wrapping up would be silently forgotten.
    """
    life = _running()
    life.request_cancel()
    life.mark_terminal(STATE.CANCELLED)
    assert life.cancel_requested() is True


def test_concurrent_cancel_and_state_moves_never_raise_and_settle_once():
    """A real race: the Cancel click and the render thread touch one object from two threads."""
    life = _running()
    errors: list[BaseException] = []
    start = threading.Event()

    def cancel():
        start.wait(5)
        try:
            for _ in range(200):
                life.request_cancel()
                life.cancel_requested()
                life.state
                life.is_terminal()
        except BaseException as exc:           # noqa: BLE001
            errors.append(exc)

    def render():
        start.wait(5)
        try:
            for _ in range(200):
                life.cancel_requested()
            life.mark_terminal(STATE.CANCELLED)
            for _ in range(50):
                life.mark_terminal(STATE.FAILED)   # late finalizers: silent no-ops
        except BaseException as exc:           # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cancel) for _ in range(3)]
    threads += [threading.Thread(target=render)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive(), "a lifecycle method deadlocked"

    assert errors == [], errors
    assert life.state is STATE.CANCELLED, "the first terminal state must win"
    assert life.cancel_requested() is True


def test_the_state_property_does_not_hold_the_lock_across_a_transition():
    """Sanity against a self-deadlock: the readers and the writer take the same lock."""
    life = _running()
    assert life.state is STATE.RUNNING
    life.request_cancel()
    assert life.is_terminal() is False
    life.mark_terminal(STATE.CANCELLED)
    assert life.state is STATE.CANCELLED


# ===========================================================================
# 4. One lifecycle is one invocation
# ===========================================================================


def test_the_invocation_id_is_minted_once_and_is_read_only():
    life = _lifecycle("inv-xyz")
    assert life.invocation_id == "inv-xyz"
    with pytest.raises(AttributeError):
        life.invocation_id = "other"
    assert life.invocation_id == "inv-xyz"


def test_the_invocation_id_is_coerced_to_a_plain_string():
    """Only a string ever crosses into `gr.State`, so the source of truth must be one too."""
    assert rw.RenderLifecycle(invocation_id=12345).invocation_id == "12345"
    assert isinstance(rw.RenderLifecycle(invocation_id=12345).invocation_id, str)


def test_two_lifecycles_share_no_state():
    """The batch shares ONE object across all selected candidates; the two distinct lifecycle
    objects this test builds must remain independent."""
    first, second = _running("a"), _running("b")
    first.request_cancel()
    assert first.cancel_requested() is True
    assert second.cancel_requested() is False
    second.raise_if_cancelled()
    first.mark_terminal(STATE.CANCELLED)
    assert second.state is STATE.RUNNING
    assert first._cancel_event is not second._cancel_event
    assert first._state_lock is not second._state_lock


def test_a_lifecycle_exposes_no_reset_or_reuse_entry_point():
    """"No reset after terminal" must not be reachable by a public method either."""
    for forbidden in ("reset", "restart", "reuse", "clear", "clear_cancel", "uncancel",
                      "set_state", "state_setter"):
        assert not hasattr(rw.RenderLifecycle, forbidden), forbidden


# ===========================================================================
# 5. RenderOutcome — one typed truth, deepcopy-safe
# ===========================================================================


def test_render_outcome_projects_success_and_cancelled_from_the_typed_cause():
    assert rw.RenderOutcome(kind=KIND.SUCCESS).success is True
    assert rw.RenderOutcome(kind=KIND.SUCCESS).cancelled is False
    assert rw.RenderOutcome(kind=KIND.CANCELLED).cancelled is True
    assert rw.RenderOutcome(kind=KIND.CANCELLED).success is False
    for fatal in (KIND.CANDIDATE_LOCAL, KIND.SHARED_FATAL, KIND.UNKNOWN_FATAL):
        assert rw.RenderOutcome(kind=fatal).success is False
        assert rw.RenderOutcome(kind=fatal).cancelled is False


def test_render_outcome_is_frozen_and_deepcopy_safe():
    outcome = rw.RenderOutcome(kind=KIND.CANCELLED, message="stopped at a safe point")
    with pytest.raises(Exception):
        outcome.kind = KIND.SUCCESS
    clone = copy.deepcopy(outcome)
    assert clone == outcome
    assert clone.kind is KIND.CANCELLED, "Enum identity must survive deepcopy"


def test_the_message_is_display_text_and_nothing_derives_from_it():
    """A typed cause exists precisely so no one re-parses prose to recover it."""
    lying = rw.RenderOutcome(kind=KIND.SUCCESS, message="❌ Error: cancelled and failed")
    assert lying.success is True and lying.cancelled is False
    assert rw.RenderOutcome(kind=KIND.SUCCESS).message == ""


# ===========================================================================
# 6. It owns nothing that runs
# ===========================================================================


def _module_source() -> str:
    with open(_MODULE, encoding="utf-8") as handle:
        return handle.read()


def _executable_source() -> str:
    """Docstrings stripped — this module's prose names FFmpeg and Qwen to say it owns neither."""
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_the_module_imports_only_four_stdlib_names():
    """CLAUDE.md's hard rule, pinned as an exact set rather than an absence of known-bad names."""
    tree = ast.parse(_module_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "threading", "dataclasses", "enum", "typing"}, sorted(imported)


def test_the_module_owns_nothing_that_runs_and_no_identity():
    """It decides cancellation intent. Everything that *executes* belongs to its callers.

    The forbidden set is wider than "no upstream runtime" on purpose. A `Popen`, a path or an FFmpeg
    argv appearing here would mean the pure contract had absorbed the thing each runtime layer is
    supposed to own and reap for itself — and a cache or media identity would mean a cancellation
    could start influencing what gets analysed or re-used.
    """
    source = _executable_source().lower()
    for forbidden in (
        # upstream runtime / UI
        "gradio", "gr.", "cupy", "cv2", "librosa", "numpy", "logger", "paths",
        # anything that runs or touches the machine
        "subprocess", "popen", "os.", "open(", "shutil", "tempfile", "signal",
        "multiprocessing", "concurrent", "ffmpeg", "nvenc", "qwen", "llama",
        # pipeline reach
        "video_processor", "video_analysis", "auto_mode", "create_music_video",
        "analyze_beats_auto", "process_video", "stage",
        # identity / persistence
        "cache_contract_version", "analysis_version", "l2_cache_version",
        "hashlib", "blake2", "json", "pickle", "mtime", "st_size",
        # clock / randomness: a lifecycle has no timeout and no generated identity of its own
        "time.", "perf_counter", "datetime", "random", "uuid",
    ):
        assert forbidden not in source, f"render_worker references {forbidden!r}"


def test_there_is_no_module_level_registry_of_lifecycles():
    """Reachability from a Cancel click is `gui.py`'s capacity-one slot, not a registry here.

    A dict or list of lifecycles at module scope would be a second, unreviewed render authority and
    an unbounded leak of one object per render for the life of the process.
    """
    tree = ast.parse(_module_source())
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            rendered = ast.unparse(node)
            for container in ("{}", "[]", "dict(", "list(", "defaultdict", "WeakValueDictionary"):
                assert container not in rendered, f"module-level container: {rendered}"
    assert "_active" not in _executable_source()
    assert "_registry" not in _executable_source()


def test_a_lifecycle_holds_only_the_three_things_it_is_allowed_to():
    """Measured on a live instance, not read off the source."""
    life = _running("inv-1")
    assert set(vars(life)) == {"_invocation_id", "_cancel_event", "_state_lock", "_state"}
    assert isinstance(life._invocation_id, str)
    assert isinstance(life._cancel_event, threading.Event)
    assert isinstance(life._state, rw.RenderLifecycleState)
    # the lock is a plain non-reentrant Lock: nothing here re-enters
    assert life._state_lock.__class__ is threading.Lock().__class__


def test_the_public_surface_is_explicit():
    assert set(rw.__all__) <= set(dir(rw))
    for name in ("RenderCancelled", "RenderLifecycle", "RenderLifecycleState",
                 "RenderOutcome", "RenderOutcomeKind"):
        assert name in rw.__all__
