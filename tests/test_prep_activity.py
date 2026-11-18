"""UX-PREP-ACTIVITY: Prepare & Verify shows trustworthy in-flight state.

[FORK] Digital-Union. Scan Library and Analyze already streamed structured progress. **Verify
Sources and Sample Decode did not**: each was a plain function returning one final string, wired
with `show_progress='hidden'`, so an accepted click and a dead button were indistinguishable for as
long as the work took -- and the work is a bounded sweep over a library that can hold thousands of
files.

Two halves are asserted here, and they are deliberately separate:

* **the pure module** (`beatsync_fork.prep_activity`) -- the phase vocabulary, the pulse and every
  honesty rule about what may and may not appear in a frame. Ordinary imported code on a bare
  interpreter, which is what the stdlib-only hard rule buys;
* **the shipped handlers**, reached exactly as `test_media_verify.py` reaches them: the real mutex,
  the real holder token, the real revision pin and the real staleness comparison, AST-extracted
  into a synthesised namespace with only the disk work stubbed. The seam loader is *imported* from
  that suite rather than copied, so both suites drive one namespace definition.

What is asserted about the handlers, in the task's own terms: an accepted request says so
immediately; a long phase that emits no event still produces visibly-advancing frames carrying a
measured clock; counters appear only when something measured them; the four terminal states stay
distinct and verbatim; a refusal performs no work and emits no activity; a stale answer is
withheld; a worker exception is reported and releases everything; the mutex is released exactly
once and never twice; duplicate presses admit exactly one; and no phase from an earlier operation
can leak into a later one.
"""

from __future__ import annotations

import ast
import re
import threading
import time

import pytest

from beatsync_fork import prep_activity as pa
from test_media_verify import _FakeRequest, _func, _load_concurrency_seam, _tree

_GUI = "src/gui.py"


def _code_only(node):
    """`ast.unparse` of `node` with every docstring removed.

    This repository has shipped the same defect five times: a substring ban tripped by the prose
    explaining the hazard the ban exists for. `ast.unparse` already drops comments, so a docstring
    is the remaining way for an explanation to look like code -- and the explanations in the
    functions below really do have to say "ETA" and "schedules nothing" to be worth reading.
    """
    import copy
    clone = copy.deepcopy(node)
    for inner in ast.walk(clone):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            inner.body = body[1:] or [ast.Pass()]
    return ast.unparse(clone)

# =============================================================================================
# 1 - the pure module: what a frame may and may not say
# =============================================================================================


def test_an_indeterminate_frame_carries_no_percentage_rate_or_estimate():
    """The central honesty rule. With nothing measured, nothing numeric may be implied.

    An operator shown "47%" for a phase nobody counted would act on a number that does not exist,
    which is the same class of defect as `media_verify` printing "0 reusable" for a cache it could
    not look up.
    """
    text = pa.working_text(pa.ActivityState(
        operation=pa.OPERATION_VERIFY, phase=pa.PHASE_CACHE_RECORDS, elapsed_ms=73_000))
    assert "%" not in text, text
    assert "/" not in text, "no count may appear when nothing counted one"
    for forbidden in ("ETA", "eta", "remaining", "estimated", "estimate", "per second",
                      "/s", "throughput", "rate", "about ", "approx", "~"):
        assert forbidden not in text, f"{forbidden!r} in {text!r}"


def test_a_determinate_frame_shows_the_measured_counts_and_only_derives_from_them():
    text = pa.activity_line(pa.ActivityState(
        operation=pa.OPERATION_SCAN, phase=pa.PHASE_WORKING, elapsed_ms=5_000,
        done=7, total=20))
    assert "7 / 20" in text, text
    assert "35.0%" in text, "the percentage is computed from the two measured integers"
    for forbidden in ("ETA", "remaining", "estimated", "per second", "/s"):
        assert forbidden not in text, forbidden


@pytest.mark.parametrize("done,total", [
    (True, 5),            # bool subclasses int and must be rejected first
    (3, True),
    (None, 10),
    (3, None),
    (3.0, 10),            # a float is not a measured integer count
    (3, 10.0),
    ("3", 10),
    (3, 0),               # a zero denominator is not a measurement
    (-1, 10),
    (11, 10),             # more done than total is not a measurement either
])
def test_anything_short_of_two_real_integers_falls_back_to_indeterminate(done, total):
    """Fail closed into honesty. A count this module accepts becomes a figure on screen."""
    state = pa.ActivityState(operation="X", phase=pa.PHASE_WORKING, elapsed_ms=0,
                             done=done, total=total)
    assert state.measured is None, (done, total)
    assert "%" not in pa.activity_line(state)


def test_elapsed_is_rendered_from_the_measured_reading_and_never_rounded_up():
    """It truncates: a frame must not display a second that has not happened yet."""
    assert "(00:00 elapsed)" in pa.activity_line(
        pa.ActivityState("X", pa.PHASE_WORKING, 999))
    assert "(00:01 elapsed)" in pa.activity_line(
        pa.ActivityState("X", pa.PHASE_WORKING, 1_999))
    assert "(1:01:01 elapsed)" in pa.activity_line(
        pa.ActivityState("X", pa.PHASE_WORKING, 3_661_000))
    # A negative reading is impossible from a monotonic clock, but must not render a negative time.
    assert "(00:00 elapsed)" in pa.activity_line(
        pa.ActivityState("X", pa.PHASE_WORKING, -5_000))


def test_the_pulse_makes_consecutive_frames_differ_and_is_total():
    """Not cosmetic. A Gradio update whose value is byte-identical to the previous one is NOT
    delivered -- the same hazard the R5 grid nonce exists for -- so two frames that rendered the
    same would simply not arrive and the panel would freeze while the work continued."""
    frames = [pa.activity_line(pa.ActivityState("X", pa.PHASE_WORKING, 1_000, pulse=n))
              for n in range(len(pa.PULSE_FRAMES))]
    assert len(set(frames)) == len(pa.PULSE_FRAMES), frames
    # Total for any integer, including a wrap and a negative, and never raising.
    for pulse in (-3, 0, 1, 999, 10 ** 9):
        assert pa.pulse_marker(pulse) in pa.PULSE_FRAMES
    for junk in (None, "x", 1.5, object()):
        assert pa.pulse_marker(junk) in pa.PULSE_FRAMES


def test_the_four_terminal_outcomes_are_distinct():
    """Requirement 5. "Declined, nothing ran" and "ran and broke" lead somewhere different."""
    assert len(set(pa.OUTCOMES)) == 4
    assert set(pa.OUTCOMES) == {pa.OUTCOME_COMPLETED, pa.OUTCOME_FAILED,
                                pa.OUTCOME_REFUSED, pa.OUTCOME_SUPERSEDED}


def test_the_phase_labels_are_plain_language_not_function_names():
    """An operator reads these. `classify_library_sources` tells them only that a programmer was
    here; "Checking saved analysis records" tells them what is happening."""
    labels = [pa.PHASE_STARTING, pa.PHASE_WORKING, pa.PHASE_ENUMERATING,
              pa.PHASE_FINGERPRINTING, pa.PHASE_CACHE_RECORDS, pa.PHASE_MEDIA_HEADERS,
              pa.PHASE_STREAM_METADATA, pa.PHASE_SAMPLE_DECODE, pa.PHASE_REPORT]
    assert len(set(labels)) == len(labels), "a duplicate label would make two phases look like one"
    for label in labels:
        assert label and label[0].isupper(), label
        assert "_" not in label, f"{label!r} reads like an identifier"
        assert not re.search(r"\(\)", label), label


def test_the_module_imports_no_upstream_runtime():
    """CLAUDE.md's hard rule, which is what makes everything above testable on a bare interpreter."""
    tree = _tree("src/beatsync_fork/prep_activity.py")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("logger", "paths", "gradio", "gr", "cupy", "cv2", "librosa", "numpy",
                      "video_analysis", "gui"):
        assert forbidden not in imported, f"{forbidden} is an upstream runtime import"


def test_the_module_reads_no_clock_of_its_own():
    """Every reading is passed in. A formatter with its own clock could not be tested without
    sleeping, and would be measuring the wrong interval anyway."""
    source = open("src/beatsync_fork/prep_activity.py", encoding="utf-8").read()
    tree = ast.parse(source)
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for forbidden in ("time.time", "time.perf_counter", "time.monotonic", "datetime.now"):
        assert forbidden not in calls, f"{forbidden} is called inside the pure module"


def test_it_reuses_the_existing_elapsed_formatter_rather_than_defining_a_second():
    """One elapsed format across both activity surfaces. A second implementation is a second
    rounding rule waiting to disagree with the first."""
    from beatsync_fork import raw_cutter_activity
    assert pa.format_elapsed is raw_cutter_activity.format_elapsed


# =============================================================================================
# 2 - the shipped handlers: an accepted request says so immediately
# =============================================================================================

def _frames(namespace, folder="J:/lib", recursive=True, session="S"):
    return list(namespace["_verify_sources_frames"](
        folder, recursive, _FakeRequest(session)))


def test_an_accepted_verification_emits_an_activity_frame_before_it_answers():
    """Requirement 1, and the whole point of the item: a function cannot say "working" and then
    work, so the accepted path has to stream."""
    namespace, state = _load_concurrency_seam()
    frames = _frames(namespace)

    assert len(frames) >= 2, frames
    assert pa.OPERATION_VERIFY in frames[0], frames[0]
    assert frames[-1] == "REPORT", "the terminal answer is the report, verbatim"
    assert "REPORT" not in frames[0], "the first frame must not pretend to be the answer"
    assert state["impl_calls"] == [("J:/lib", True)], "the live widget values, once"


def test_a_long_phase_that_emits_no_event_still_produces_advancing_measured_frames():
    """Requirement 4. The failure this removes: a phase with no progress event looked frozen.

    The impl blocks for several poll intervals, so the stream has to keep speaking on its own --
    with a clock that was read, not predicted.
    """
    namespace, state = _load_concurrency_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    release = threading.Event()

    def impl(folder, recursive):
        release.wait(timeout=tick * 3.5)
        return "SLOW_REPORT"

    state["impl"] = impl
    frames = _frames(namespace)

    activity = [f for f in frames if pa.OPERATION_VERIFY in f]
    assert len(activity) >= 3, f"a silent phase must keep reporting: {frames}"
    assert len(set(activity)) == len(activity), "every frame must differ or it is not delivered"
    seconds = [_elapsed_of(f) for f in activity]
    assert seconds == sorted(seconds), f"the clock must never go backwards: {seconds}"
    assert seconds[-1] >= 1, "a multi-second phase must show a measured figure above zero"
    assert frames[-1] == "SLOW_REPORT"


def _elapsed_of(frame: str) -> int:
    """Seconds from a rendered frame's `(MM:SS elapsed)`."""
    match = re.search(r"\((?:(\d+):)?(\d{2}):(\d{2}) elapsed\)", frame)
    assert match, f"no measured elapsed in {frame!r}"
    hours = int(match.group(1) or 0)
    return hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))


def test_the_real_phases_reach_the_operator_in_plain_language():
    """Requirement 2. The phase is published by the work itself, not guessed at by the stream."""
    namespace, state = _load_concurrency_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    seen = []

    def impl(folder, recursive):
        for phase in (pa.PHASE_ENUMERATING, pa.PHASE_CACHE_RECORDS, pa.PHASE_MEDIA_HEADERS):
            namespace["_mark_prep_phase"](phase)
            seen.append(phase)
            time.sleep(tick * 1.4)
        return "REPORT"

    state["impl"] = impl
    frames = _frames(namespace)

    rendered = "\n".join(frames)
    for phase in seen:
        assert phase in rendered, f"{phase!r} never reached a frame:\n{rendered}"


def test_no_count_is_invented_for_an_operation_that_measured_none():
    """Requirement 3 from the other side. Verify publishes phases, not counters -- so no frame may
    show one. A determinate bar here would be a number nobody computed."""
    namespace, state = _load_concurrency_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    state["impl"] = lambda folder, recursive: (time.sleep(tick * 2.2), "REPORT")[1]
    frames = _frames(namespace)

    for frame in (f for f in frames if pa.OPERATION_VERIFY in f):
        assert "%" not in frame, frame
        assert "/" not in frame, frame


# =============================================================================================
# 3 - the four terminal states stay distinct, and verbatim
# =============================================================================================

def test_a_refused_verification_emits_exactly_one_frame_and_no_activity():
    """Requirement 5 (refused) plus the R13 rule: no working indicator for work that was declined.

    It must also leave no trace -- a refused click captures no revision and pins nothing, which is
    what `source-verification.md` means by "a refused click leaves no state at all".
    """
    namespace, state = _load_concurrency_seam()
    namespace["_RENDER_LOCK"].acquire()          # somebody else holds the mutex
    try:
        frames = _frames(namespace)
    finally:
        namespace["_RENDER_LOCK"].release()

    assert len(frames) == 1, f"a refusal is one frame: {frames}"
    assert frames[0] == "BUSY_UNKNOWN_HOLDER", frames
    assert pa.OPERATION_VERIFY not in frames[0], "a declined request must not announce work"
    assert state["impl_calls"] == [], "a refusal performs no work"
    assert namespace["_verify_revisions"] == {}, "a refusal pins and captures nothing"


def test_a_superseded_verification_withholds_its_report_verbatim():
    """Requirement 5 (superseded). The folder changed while this ran, so the answer describes a
    library the operator has moved on from -- and the activity text must not leak into the notice."""
    namespace, state = _load_concurrency_seam()

    def impl(folder, recursive):
        namespace["_bump_verify_revision"](
            namespace["_scoped_key"]("session-A", namespace["_SCOPE_VERIFY"]))
        return "REPORT_FOR_OLD_FOLDER"

    state["impl"] = impl
    frames = _frames(namespace, session="session-A")

    assert frames[-1] == "STALE_FOLDER_CHANGED", frames
    assert "REPORT_FOR_OLD_FOLDER" not in frames[-1]
    assert pa.OPERATION_VERIFY not in frames[-1], "the terminal answer carries no activity text"


def test_a_worker_exception_is_reported_and_releases_everything():
    """Requirement 5 (failed) plus cleanup. A verification that raised must report rather than
    vanish behind Gradio's generic error -- and must not strand the mutex, the token or the pin."""
    namespace, state = _load_concurrency_seam()

    def impl(folder, recursive):
        raise OSError("the drive went away")

    state["impl"] = impl
    frames = _frames(namespace)

    assert "SOURCE VERIFICATION FAILED" in frames[-1], frames[-1]
    assert "OSError" in frames[-1] and "the drive went away" in frames[-1]
    assert "No analysis, cache change, media change or render was performed." in frames[-1]
    assert not namespace["_RENDER_LOCK"].locked(), "the mutex must be released on the error path"
    assert namespace["_lock_holder_now"]() == "", "the holder token must be cleared"
    assert namespace["_prep_phase_now"]() == "", "the phase slot must be cleared"


def test_the_four_terminal_answers_are_all_different_strings():
    """Taken together: completed, refused, superseded and failed must not be confusable."""
    answers = set()

    namespace, state = _load_concurrency_seam()
    answers.add(_frames(namespace)[-1])                                    # completed

    namespace, state = _load_concurrency_seam()
    namespace["_RENDER_LOCK"].acquire()
    try:
        answers.add(_frames(namespace)[-1])                                # refused
    finally:
        namespace["_RENDER_LOCK"].release()

    namespace, state = _load_concurrency_seam()
    state["impl"] = lambda f, r: namespace["_bump_verify_revision"](
        namespace["_scoped_key"]("S", namespace["_SCOPE_VERIFY"])) or "X"
    answers.add(_frames(namespace)[-1])                                    # superseded

    namespace, state = _load_concurrency_seam()
    state["impl"] = lambda f, r: (_ for _ in ()).throw(RuntimeError("boom"))
    answers.add(_frames(namespace)[-1])                                    # failed

    assert len(answers) == 4, answers


# =============================================================================================
# 4 - the mutex, the duplicate press, and the queue that must not form
# =============================================================================================

def test_the_mutex_is_released_exactly_once_on_every_path():
    """A double release would raise and leave the app refusing every later render."""
    class _CountingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.releases = 0

        def acquire(self, blocking=True):
            return self._lock.acquire(blocking)

        def release(self):
            self.releases += 1
            self._lock.release()

        def locked(self):
            return self._lock.locked()

    for impl in (lambda f, r: "REPORT",
                 lambda f, r: (_ for _ in ()).throw(OSError("x"))):
        namespace, state = _load_concurrency_seam()
        counting = _CountingLock()
        namespace["_RENDER_LOCK"] = counting
        state["impl"] = impl
        _frames(namespace)
        assert counting.releases == 1, counting.releases
        assert not counting.locked()


def test_two_concurrent_presses_admit_exactly_one_and_refuse_the_other():
    """The duplicate press. One non-blocking acquire is the test AND the acquisition, so there is
    no check-then-act window -- and the loser is refused rather than queued."""
    namespace, state = _load_concurrency_seam()
    inside = threading.Event()
    release = threading.Event()

    def impl(folder, recursive):
        inside.set()
        release.wait(timeout=10)
        return "REPORT"

    state["impl"] = impl
    results = {}

    first = threading.Thread(target=lambda: results.__setitem__(
        "first", list(namespace["_verify_sources_frames"]("J:/a", True, _FakeRequest("A")))))
    first.start()
    assert inside.wait(timeout=10), "the first press never entered the work"

    results["second"] = list(
        namespace["_verify_sources_frames"]("J:/b", True, _FakeRequest("B")))
    release.set()
    first.join(timeout=10)

    assert len(results["second"]) == 1, results["second"]
    assert results["second"][0] == "VERIFY_REFUSED_ALREADY_RUNNING", results["second"]
    assert results["first"][-1] == "REPORT"
    assert state["impl_calls"] == [("J:/a", True)], \
        "the refused press must never run, then or later"


def test_a_refused_press_does_not_run_afterwards_when_the_mutex_frees():
    """The S2-A R3 defect, restated for the activity stream: a refusal is TERMINAL. It must not
    become a queued job that executes against click-time values once the holder finishes."""
    namespace, state = _load_concurrency_seam()
    namespace["_RENDER_LOCK"].acquire()
    try:
        frames = _frames(namespace, folder="J:/declined")
    finally:
        namespace["_RENDER_LOCK"].release()

    assert len(frames) == 1 and state["impl_calls"] == []
    # The mutex is free now. Nothing may have been scheduled by the refusal.
    time.sleep(namespace["_ACTIVITY_TICK_SECONDS"] * 1.5)
    assert state["impl_calls"] == [], "a refusal scheduled work for later"
    assert not namespace["_RENDER_LOCK"].locked()


# =============================================================================================
# 5 - nothing leaks from one operation into the next
# =============================================================================================

def test_a_later_operation_never_shows_an_earlier_operations_phase():
    """The straggler rule, applied to the phase slot. A fresh operation must read `Starting`,
    never whatever the previous one left behind."""
    namespace, state = _load_concurrency_seam()

    state["impl"] = lambda f, r: (namespace["_mark_prep_phase"](pa.PHASE_MEDIA_HEADERS),
                                  "REPORT")[1]
    _frames(namespace)
    assert namespace["_prep_phase_now"]() == "", "the slot is cleared when an operation ends"

    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    state["impl"] = lambda f, r: (time.sleep(tick * 2.2), "REPORT_2")[1]
    frames = _frames(namespace)

    rendered = "\n".join(f for f in frames if pa.OPERATION_VERIFY in f)
    assert pa.PHASE_MEDIA_HEADERS not in rendered, \
        f"a previous operation's phase leaked into a new one:\n{rendered}"
    assert pa.PHASE_STARTING in rendered


def test_the_collected_form_returns_exactly_the_last_frame():
    """`_on_verify_sources_click` is what every non-Gradio caller drives, and its answer must be
    byte-for-byte what it was before the activity stream existed."""
    for impl, expected in (
            (lambda f, r: "REPORT", "REPORT"),
            (lambda f, r: (_ for _ in ()).throw(OSError("x")), None),
    ):
        namespace, state = _load_concurrency_seam()
        state["impl"] = impl
        collected = namespace["_on_verify_sources_click"]("J:/lib", True, _FakeRequest("S"))
        assert isinstance(collected, str) and collected
        assert pa.OPERATION_VERIFY not in collected, "the collected answer is terminal, not activity"
        if expected:
            assert collected == expected
        else:
            assert "SOURCE VERIFICATION FAILED" in collected


def test_an_interrupt_propagates_and_is_never_reported_as_a_file_problem():
    """`KeyboardInterrupt` is not evidence about the operator's media. The pre-activity handler
    caught `Exception` only, and that boundary is preserved -- while still releasing everything."""
    namespace, state = _load_concurrency_seam()
    state["impl"] = lambda f, r: (_ for _ in ()).throw(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _frames(namespace)

    assert not namespace["_RENDER_LOCK"].locked(), "the mutex must still be released"
    assert namespace["_lock_holder_now"]() == ""
    assert namespace["_prep_phase_now"]() == ""


# =============================================================================================
# 6 - structure: one stream, no second coordinator
# =============================================================================================

def test_the_activity_stream_joins_its_worker_before_the_mutex_is_released():
    """Load-bearing, and the reason it is asserted structurally as well as behaviourally.

    `progress-events.md` states the rule for `process_video`: a generator's lifetime is its
    worker's lifetime, because the caller's mutex is released the moment the generator returns. The
    verification stream holds `_RENDER_LOCK` across the whole stream, so if an abandoned event
    released that mutex while the worker was still issuing FFprobe reads, a render could start into
    exactly the disk contention the mutex exists to prevent.

    With `yield from`, closing the outer generator closes the inner one first -- so the join runs
    in `_activity_frames`' `finally` and the release runs afterwards in the caller's.
    """
    tree = _tree(_GUI)
    stream = _func(tree, "_activity_frames")
    finallies = [n for n in ast.walk(stream) if isinstance(n, ast.Try) and n.finalbody]
    assert finallies, "the join must sit in a finally"
    joined = "\n".join(ast.unparse(s) for s in finallies[0].finalbody)
    assert "thread.join()" in joined, joined
    # And nothing is yielded from it: yielding during a close raises RuntimeError, which would
    # replace a clean join with a second failure.
    # Walk the finalbody STATEMENTS, not the whole `Try`: the Try's own body legitimately yields
    # every activity frame, so walking the node would always find one and the assertion would be
    # vacuous in exactly the direction that matters.
    assert not [n for statement in finallies[0].finalbody
                for n in ast.walk(statement) if isinstance(n, ast.Yield)], \
        "nothing may be yielded from the finally"

    for name in ("_verify_sources_frames", "_sample_decode_frames"):
        body = ast.unparse(_func(tree, name))
        assert "yield from _activity_frames(" in body, f"{name} must reuse the one stream"
        assert body.index("_activity_frames(") < body.index("_RENDER_LOCK.release()"), name


def test_abandoning_the_event_joins_the_worker_before_releasing_the_mutex():
    """The same property as the structural test above, measured on the SHIPPED generator.

    This is the one that would actually bite. `progress-events.md`: a generator's lifetime is its
    worker's lifetime, because the caller's mutex is released the moment the generator returns. If
    an abandoned Gradio event released `_RENDER_LOCK` while the worker was still issuing FFprobe
    reads, a render could start straight into the D0 disk contention the mutex exists to prevent.

    So: take two activity frames, abandon the stream, and require that the work actually finished
    before the lock became free -- and that the lock did become free.
    """
    namespace, state = _load_concurrency_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    order = []

    def impl(folder, recursive):
        time.sleep(tick * 2.2)
        order.append("work-finished")
        return "REPORT"

    state["impl"] = impl
    stream = namespace["_verify_sources_frames"]("J:/lib", True, _FakeRequest("S"))

    assert pa.OPERATION_VERIFY in next(stream), "the immediate activity frame"
    next(stream)
    assert namespace["_RENDER_LOCK"].locked(), "the mutex is held while the stream is live"

    stream.close()                     # exactly what Gradio does to a dropped event
    order.append("stream-closed")

    assert order == ["work-finished", "stream-closed"], (
        f"the worker must be joined BEFORE the close completes, got {order}")
    assert not namespace["_RENDER_LOCK"].locked(), "the mutex must not be stranded"
    assert namespace["_lock_holder_now"]() == ""
    assert namespace["_prep_phase_now"]() == ""
    assert state["impl_calls"] == [("J:/lib", True)], "the work ran once and completed"


def test_the_worker_touches_no_gradio_component():
    """Requirement 9. The worker's only cross-thread action is one `queue.put`; every widget update
    is done by the generator, exactly as `process_video` and `_run_prep_in_worker` do it."""
    worker = next(n for n in ast.walk(_func(_tree(_GUI), "_activity_frames"))
                  if isinstance(n, ast.FunctionDef) and n.name == "worker")
    body = ast.unparse(worker)
    for forbidden in ("gr.update", "gr.skip", "gr.Textbox", "gr.Info", "gr.Warning", ".click("):
        assert forbidden not in body, f"the worker reaches {forbidden}"
    calls = [ast.unparse(n.func) for n in ast.walk(worker) if isinstance(n, ast.Call)]
    assert "done.put" in calls, calls


def test_no_second_coordinator_job_manager_or_background_service_is_introduced():
    """Requirement 7. One thread per call, owned by the generator that started it -- no registry,
    no scheduler, no pool, and nothing that outlives the request."""
    tree = _tree(_GUI)
    for name in ("_activity_frames", "_verify_sources_frames", "_sample_decode_frames"):
        body = _code_only(_func(tree, name))
        for forbidden in ("ThreadPoolExecutor", "ProcessPoolExecutor", "submit(",
                          "schedule", "Timer(", "atexit", "RenderLifecycle",
                          "gr.State", "session_state", "_verify_revisions["):
            assert forbidden not in body, f"{name} reaches {forbidden}"
    # The pure module starts nothing at all.
    pure = _code_only(_tree("src/beatsync_fork/prep_activity.py"))
    for forbidden in ("Thread", "queue", "Lock", "subprocess", "Popen"):
        assert forbidden not in pure, f"the pure module mentions {forbidden}"


def test_the_two_buttons_are_wired_to_the_generators_and_their_outputs_are_unchanged():
    """The activity lives inside the ONE existing report textbox, so the pinned isolation contract
    survives untouched: verification still writes its own report and nothing else."""
    source = ast.unparse(_tree(_GUI))
    verify = source.split("prep_verify_btn.click(")[1].split(")")[0]
    assert "fn=_verify_sources_frames" in verify, verify
    assert "outputs=[prep_verify_report]" in verify, verify
    assert "inputs=[prep_folder, prep_recursive]" in verify, verify
    sample = source.split("sample_btn.click(")[1].split(")")[0]
    assert "fn=_sample_decode_frames" in sample, sample
    assert "outputs=[sample_report]" in sample, sample


def test_the_preparation_event_wait_is_bounded_so_a_silent_phase_still_speaks():
    """Scan and Analyze: the unbounded `status_queue.get()` is what left the panel frozen through a
    phase that emits no event. It is now a bounded poll that emits a measured activity notice."""
    body = _code_only(_func(_tree(_GUI), "_run_prep_in_worker"))
    assert "status_queue.get(timeout=_ACTIVITY_TICK_SECONDS)" in body, body
    assert "queue.Empty" in body, "a tick with no event must be handled, not crash"
    assert "fork_prep_activity.activity_line" in body, "the notice must be the shared renderer"
    for forbidden in ("ETA", "estimated", "remaining"):
        assert forbidden not in body, forbidden
