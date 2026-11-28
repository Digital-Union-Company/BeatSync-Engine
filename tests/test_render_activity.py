"""UX-RENDER-ACTIVITY V1: a long render looks alive, and the panel never lies about progress.

[FORK] Digital-Union. The owner's report is that during a long render -- above all Stage 5 against
thousands of sources -- the Render tab's only sign of life is a four-line `Status` textbox, and a
textbox that has not changed for twenty minutes is indistinguishable from a frozen application.

The same observation Scan and Analyze already had answered (UX-PREP-VISUAL-PROGRESS) and the Raw
Cutter before them (R12), so this suite is organised around the two questions those items separated,
because conflating them is how a panel ends up lying:

    "is this running?"     -> a state chip, a CSS keyframe and a BROWSER-anchored clock. All three
                              cost no measurement and keep working with zero server frames.
    "how far has it got?"  -> ONLY `ProgressView.active_measurement()`. Absent otherwise.

Three tiers, as `CLAUDE.md` describes them, and they are not interchangeable:

1. the pure renderer (`beatsync_fork.render_activity`) and the new read-only `ProgressView`
   accessor, imported and executed as ordinary code on a bare interpreter;
2. the **shipped** `gui.py` seams -- the reporter class, the two interlock handlers and the two
   render wrappers -- reached by `ast` inspection and by executing real extracted bodies, so these
   are assertions about real source rather than about a mock;
3. GUI runtime behaviour -- how `gradio==6.19.0` actually renders and ticks this panel -- which is
   **not** asserted here and is reported separately as a served browser smoke.
"""

from __future__ import annotations

import ast
import functools
import html
import re
import time

import pytest

from beatsync_fork import progress as fork_progress
from beatsync_fork import render_activity as ra
from beatsync_fork.progress import EventKind, ProgressEvent, Stage
from beatsync_fork.progress_view import ProgressView

_GUI = "src/gui.py"
_MODULE = "src/beatsync_fork/render_activity.py"


@functools.lru_cache(maxsize=None)
def _gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


# `gui.py` is ~20k lines, and most tests here walk it. Parsing it once per *test* measured at
# roughly 0.4 s each, so the parse is cached and every caller that MUTATES a tree (the docstring
# stripper) is handed a fresh one.
@functools.lru_cache(maxsize=None)
def _cached_tree() -> ast.Module:
    return ast.parse(_gui_source(), filename=_GUI)


def _tree() -> ast.Module:
    return _cached_tree()


def _fresh_tree() -> ast.Module:
    """A tree nobody else holds, for the destructive docstring stripper."""
    return ast.parse(_gui_source(), filename=_GUI)


@functools.lru_cache(maxsize=None)
def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("%s not found in %s" % (name, _GUI))


@functools.lru_cache(maxsize=None)
def _body(name: str) -> str:
    """A function's body as source, with docstrings stripped.

    Stripping them is not cosmetic: several bans below are about what the code *does*, and a
    docstring that legitimately explains why a thing is forbidden would otherwise trip its own ban.
    """
    node = _func(name)
    return "\n".join(ast.unparse(item) for item in node.body
                     if not (isinstance(item, ast.Expr)
                             and isinstance(item.value, ast.Constant)
                             and isinstance(item.value.value, str)))


def _strip_docstrings(node):
    """Remove every module, class and function docstring, in place, and return the node.

    This is not tidiness, it is the discipline this repository records twice over: *"a substring
    scan cannot tell an explanation from a violation"*. Every ban below is about what the code
    DOES, and the module under test explains at length why it must not read a clock, invent an ETA
    or compute a second percentage -- so a raw-text ban on `perf_counter`, `eta` or `* 100.0` would
    be tripped by the very prose that documents the rule. Three of these tests were written that
    way first and failed on their own documentation.
    """
    for item in ast.walk(node):
        if isinstance(item, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(item, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                item.body = body[1:] or [ast.Pass()]
    return node


@functools.lru_cache(maxsize=None)
def _module_code() -> str:
    """The pure module's CODE, with every docstring removed."""
    return ast.unparse(_strip_docstrings(
        ast.parse(open(_MODULE, encoding="utf-8").read(), filename=_MODULE)))


@functools.lru_cache(maxsize=None)
def _gui_code() -> str:
    """All of `gui.py` as code: no comments (`ast` drops them) and no docstrings."""
    return ast.unparse(_strip_docstrings(_fresh_tree()))


def _word_absent(haystack: str, word: str) -> bool:
    """Whether `word` is absent as a WORD rather than as a substring.

    `eta` is inside `detail`; `cache` is inside nothing here but `speed` is inside `speedup`. A
    substring ban on any of them bans correct code for the wrong reason.
    """
    return re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(word),
                     haystack, re.IGNORECASE) is None


def _calls_named(root, name: str) -> list:
    """Every actual CALL of `name` -- not every mention of it.

    `gui.py` discusses `_RENDER_LOCK.acquire(blocking=False)` and `_RENDER_LOCK.locked()` in its
    own comments, so counting occurrences in raw text counts the prose as well as the code.
    """
    return [n for n in ast.walk(root)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == name]


@functools.lru_cache(maxsize=None)
def _class_code(name: str) -> str:
    root = _strip_docstrings(_fresh_tree())
    node = next(n for n in ast.walk(root) if isinstance(n, ast.ClassDef) and n.name == name)
    return ast.unparse(node)


def _classes(markup: str) -> str:
    """Just the root element's `class` attribute.

    `data-bs-render-completed-stages` contains the substring `bs-render-completed`, so a bare
    `"bs-render-completed" not in markup` reports a correct idle panel as a completed one. Measured
    the hard way: that assertion failed on every parametrisation before this helper existed.
    """
    return _attr(markup, "class")


def _attr(markup: str, name: str) -> str:
    """One attribute value out of a panel. The tests read the CONTRACT, not the presentation."""
    match = re.search(r'\b%s="([^"]*)"' % re.escape(name), markup)
    assert match, "no %s in %r" % (name, markup)
    return match.group(1)


def _text(markup: str) -> str:
    """Every word an operator can actually read: tags stripped, entities resolved."""
    return html.unescape(re.sub(r"<[^>]*>", " ", markup))


def _running(**kw) -> ra.RenderActivityState:
    kw.setdefault("status", ra.STATUS_RUNNING)
    return ra.RenderActivityState(**kw)


# =============================================================================================
# TIER 1 -- the pure renderer's honesty rules.
# =============================================================================================


def test_the_module_reads_no_clock_at_all():
    """Every elapsed figure is a reading the caller took. A formatter with its own clock could not
    be tested without sleeping, and would be measuring the wrong interval anyway."""
    tree = ast.parse(open(_MODULE, encoding="utf-8").read(), filename=_MODULE)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("time", "datetime", "random"):
        assert banned not in imported, banned
    code = _module_code()
    for banned in ("perf_counter", "monotonic", "time", "now"):
        assert _word_absent(code, banned), banned


def test_measured_percent_is_the_only_route_to_a_percentage():
    """A test the sibling module carries for the same reason: one arithmetic, one place."""
    assert _module_code().count("* 100.0") == 1


def test_there_is_no_eta_no_rate_and_no_throughput_anywhere():
    """Restated here because this is the surface where an operator would most want one, and where
    the data to compute one honestly does not exist."""
    code = _module_code()
    for banned in ("eta", "remaining", "rate", "per_second", "throughput", "speed",
                   "estimate", "predict"):
        assert _word_absent(code, banned), banned


@pytest.mark.parametrize("done,total", [
    (None, None), (0, None), (None, 10),         # nothing measured
    (True, True), (False, 10), (0, True),        # bool subclasses int and must fail closed FIRST
    (0, 0), (1, 0), (0, -1),                     # a non-positive denominator is not a measurement
    (-1, 10), (11, 10),                          # out of range
    ("0", "10"), (1.0, 10.0),                    # not integers
])
def test_a_determinate_bar_requires_two_real_measured_integers(done, total):
    state = _running(done=done, total=total)
    assert state.measured is None
    assert ra.measured_percent(state) is None
    markup = ra.activity_html(state)
    assert _attr(markup, "data-bs-render-determinate") == "0"
    assert _attr(markup, "data-bs-render-percent") == ""


def test_an_indeterminate_track_carries_no_fill_element_at_all():
    """UX-PREP-VISUAL-PROGRESS finding F2, inherited deliberately.

    A `width:100%` fill paints exactly what a genuinely complete `N / N` paints, so a phase nobody
    counted looked FINISHED -- the inverse of the defect this whole item exists to remove. A
    `width:0` fill is a quantity nobody measured. Emitting none is strictly better than dimming
    one, because then no element's width can be read as a figure.
    """
    markup = ra.activity_html(_running(done=None, total=None))
    assert "bs-render-track" in markup
    assert "bs-render-indeterminate" in markup
    assert "bs-render-fill" not in markup
    # ARIA's own rule for an indeterminate progressbar is to OMIT the value, because a screen
    # reader announces a zero as a measurement.
    assert "aria-valuenow" not in markup
    # The sweep is still there, so the panel is still visibly alive.
    assert "bs-render-sweep" in markup


def test_a_measured_zero_is_a_real_count_and_keeps_both_its_fill_and_its_sweep():
    """The owner's original complaint was a TRUE `0 / 6363`. It must not be suppressed, smoothed or
    started somewhere other than zero -- and the animation must still move, which is why the sweep
    is a SIBLING of the fill rather than inside it: on the fill, a zero-width element has nowhere
    to travel."""
    markup = ra.activity_html(_running(done=0, total=6363))
    assert 'width:0.0%' in markup
    assert "bs-render-sweep" in markup
    assert _attr(markup, "aria-valuenow") == "0.0"
    assert _attr(markup, "data-bs-render-determinate") == "1"
    assert "0 / 6363" in _text(markup)


def test_a_determinate_fill_is_exactly_the_measured_fraction():
    state = _running(done=758, total=3620, unit="sources")
    assert ra.measured_percent(state) == pytest.approx(20.939, abs=1e-3)
    markup = ra.activity_html(state)
    assert 'width:20.9%' in markup
    assert _attr(markup, "aria-valuenow") == "20.9"
    spoken = _text(markup)
    assert "758 / 3620 sources (20.9%)" in spoken


@pytest.mark.parametrize("status", ra.TERMINAL_STATUSES)
def test_a_terminal_panel_has_no_track_and_a_frozen_clock(status):
    """No bar at all: a full one on completion would be a measurement nobody took, and one on a
    failure would claim the work got somewhere. `data-running="0"` is what stops the browser
    advancing the figures."""
    markup = ra.activity_html(ra.RenderActivityState(
        status=status, job_ms=125000, done=5, total=5))
    assert "bs-render-track" not in markup
    assert "bs-render-sweep" not in markup
    assert _attr(markup, "data-running") == "0"
    assert "02:05" in _text(markup)


def test_idle_is_permanently_visible_claims_nothing_and_does_not_animate():
    """Unlike the preparation panel, this one is never blank: the brief asks for a permanently
    visible panel. So idle renders a real card -- with no track, no clock advance and no claim."""
    markup = ra.activity_html(ra.RenderActivityState())
    assert "bs-render-idle" in markup
    assert _attr(markup, "data-running") == "0"
    assert "bs-render-track" not in markup
    assert ra.DETAIL_IDLE in _text(markup)


@pytest.mark.parametrize("bogus", ["", None, "banana", "COMPLETED", "run", 0, 17, object()])
def test_an_unknown_status_fails_closed_to_idle(bogus):
    """Never to running -- an animation over a dead worker -- and never to completed, which is the
    one answer a caller must not receive by accident."""
    markup = ra.activity_html(ra.RenderActivityState(status=bogus))
    classes = _classes(markup)
    assert "bs-render-idle" in classes
    assert _attr(markup, "data-running") == "0"
    assert _attr(markup, "data-bs-render-status") == ra.STATUS_IDLE
    for never in ("bs-render-running", "bs-render-completed", "bs-render-starting",
                  "bs-render-queued", "bs-render-cancelling"):
        assert never not in classes, never


@pytest.mark.parametrize("status", ra.ACTIVE_STATUSES)
def test_every_active_status_keeps_the_browser_clock_running(status):
    assert ra.RenderActivityState(status=status).running is True
    assert _attr(ra.activity_html(ra.RenderActivityState(status=status)), "data-running") == "1"


@pytest.mark.parametrize("status", ra.TERMINAL_STATUSES + (ra.STATUS_IDLE,))
def test_no_terminal_or_idle_status_can_be_emitted_with_a_live_clock(status):
    """`running` is DERIVED from the status rather than passed in, so a caller that forgets a flag
    cannot emit a frozen panel with an advancing clock."""
    assert ra.RenderActivityState(status=status).running is False


def test_the_status_vocabulary_is_exactly_the_brief_plus_the_one_measured_extra():
    """Seven states the owner asked for, plus `queued`, which exists because of a measurement: a
    render click from a second browser session is neither refused nor run but WAITS. See the task
    evidence (M4) and `_render_interlock_pending_status`."""
    assert set(ra.PANEL_STATUSES) == {
        "idle", "starting", "queued", "running", "cancelling",
        "completed", "failed", "cancelled"}
    assert set(ra.STATUS_LABELS) == set(ra.PANEL_STATUSES)
    # Every state is readable as WORDS, so colour is never the only signal.
    for status in ra.PANEL_STATUSES:
        assert ra.STATUS_LABELS[status].strip()
        assert ra.STATUS_LABELS[status] in _text(
            ra.activity_html(ra.RenderActivityState(status=status)))


def test_active_and_terminal_statuses_are_disjoint_and_cover_everything_but_idle():
    assert not set(ra.ACTIVE_STATUSES) & set(ra.TERMINAL_STATUSES)
    assert set(ra.ACTIVE_STATUSES) | set(ra.TERMINAL_STATUSES) | {ra.STATUS_IDLE} == set(
        ra.PANEL_STATUSES)


# ---------------------------------------------------------------------------------------------
# Phase isolation -- "do not inherit counts, units or percentages across phase transitions".
# ---------------------------------------------------------------------------------------------


def test_nothing_can_be_inherited_across_a_phase_transition():
    """The module holds no state between calls at all, so there is no mechanism by which a count,
    total, unit, percentage or clock could survive a phase change. Asserted as a property of two
    independently rendered frames rather than as a claim about the code."""
    counted = ra.activity_html(_running(stage=5, stage_name="Video Analysis",
                                        done=3620, total=3620, unit="sources",
                                        phase_token="r1-7", phase_ms=40000))
    uncounted = ra.activity_html(_running(stage=5, stage_name="Video Analysis",
                                          detail="Qwen job 17 / 300",
                                          phase_token="r1-8", phase_ms=120))
    assert _attr(counted, "data-bs-render-percent") == "100.0"
    assert _attr(uncounted, "data-bs-render-percent") == ""
    assert _attr(uncounted, "data-bs-render-done") == ""
    assert _attr(uncounted, "data-bs-render-total") == ""
    assert "bs-render-fill" not in uncounted
    assert "100" not in _text(uncounted)
    # A new phase is a new token, which is what makes the BROWSER re-anchor the phase clock
    # instead of carrying the old phase's elapsed figure forward.
    assert _attr(counted, "data-phase-token") != _attr(uncounted, "data-phase-token")


def test_an_uncounted_phase_shows_its_description_and_no_figure():
    markup = ra.activity_html(_running(stage=5, stage_name="Video Analysis",
                                       detail="Qwen job 17 / 300 - 64 / 120 candidates"))
    spoken = _text(markup)
    assert "Qwen job 17 / 300" in spoken
    assert "%" not in spoken.replace("(", "").split("Qwen")[0]


def test_phase_labels_resolve_only_the_pipelines_own_keys():
    """An unknown phase key renders as NOTHING rather than a prettified version of itself: a key is
    an internal identifier, and showing `source_scan` tells an operator only that a programmer was
    here."""
    assert ra.phase_label("qwen") == "Semantic tagging (Qwen)"
    assert ra.phase_label("assembly") == "Assembling the timeline"
    for unknown in ("", None, "source_scan", "library_classify", "banana"):
        assert ra.phase_label(unknown) == ""


# ---------------------------------------------------------------------------------------------
# Stage indicators -- a labelled COUNT, never a second progress bar.
# ---------------------------------------------------------------------------------------------


def test_the_stage_denominator_is_derived_from_the_pipelines_own_vocabulary():
    """Not a number somebody typed. A second list of stage numbers is a second thing able to drift
    from what the pipeline publishes."""
    assert ra.PIPELINE_STAGES == tuple(sorted(fork_progress.STAGE_TITLES))
    assert ra.STAGE_COUNT == len(fork_progress.STAGE_TITLES)
    assert ra.PIPELINE_STAGES == (0, 1, 2, 3, 4, 5, 6)


def test_completed_stages_are_only_the_ones_reported_and_are_never_inferred():
    """"Stage 5 is active, therefore 1-4 are done" is an assumption, and it is how a SKIPPED stage
    would be reported as completed. Here stage 3 never finished and must stay pending."""
    markup = ra.activity_html(_running(stage=5, completed_stages=(0, 1, 2, 4)))
    pills = re.findall(r'class="bs-render-pill bs-render-pill-(\w+)">([^<]*)<', markup)
    kinds = {label: kind for kind, label in pills}
    assert kinds[ra.stage_label(0)] == "done"
    assert kinds[ra.stage_label(4)] == "done"
    assert kinds[ra.stage_label(5)] == "active"
    assert kinds[ra.stage_label(3)] == "pending"
    assert kinds[ra.stage_label(6)] == "pending"
    assert len(pills) == ra.STAGE_COUNT


def test_the_completed_stage_figure_is_explicitly_labelled_as_a_count():
    """Requirement 2 permits a completed-stage count "when explicitly labelled as such", and
    forbids a weighted stage-completion percentage. So it is words, and there is no second bar."""
    spoken = _text(ra.activity_html(_running(stage=5, completed_stages=(0, 1, 2, 3, 4))))
    assert "5 / %d stages completed" % ra.STAGE_COUNT in spoken
    assert spoken.count("bs-render-track") == 0
    markup = ra.activity_html(_running(stage=5, completed_stages=(0, 1, 2, 3, 4)))
    # Exactly one track in the whole panel: the active phase's. The pills are not a bar.
    assert markup.count("bs-render-track") == 1


def test_stage_labels_never_invent_a_name():
    assert ra.stage_label(5) == "Analysis"
    # No short name for stage 0 or 3, so the PIPELINE's own title is used rather than an invention.
    assert ra.stage_label(0) == fork_progress.STAGE_TITLES[0]
    assert ra.stage_label(3) == fork_progress.STAGE_TITLES[3]
    assert ra.stage_label(99) == "Stage 99"
    for bogus in (None, "", "five", object()):
        assert ra.stage_label(bogus) == ""


# ---------------------------------------------------------------------------------------------
# Variant Lab batch context.
# ---------------------------------------------------------------------------------------------


def test_a_single_render_has_no_candidate_context_and_none_is_invented():
    """`None` is NOT `1 / 1`. A single render has no candidate context, and rendering one would be
    inventing it."""
    markup = ra.activity_html(_running())
    assert "Candidate" not in _text(markup)
    assert _attr(markup, "data-bs-render-candidate") == ""
    assert _attr(markup, "data-bs-render-candidate-total") == ""


def test_a_batch_renders_its_candidate_position():
    markup = ra.activity_html(_running(candidate=2, candidate_total=4))
    assert "Candidate 2 / 4" in _text(markup)
    assert _attr(markup, "data-bs-render-candidate") == "2"
    assert _attr(markup, "data-bs-render-candidate-total") == "4"


@pytest.mark.parametrize("position,total", [(0, 4), (5, 4), (True, 4), (2, 0), (2, None), (None, 4)])
def test_an_unusable_candidate_position_is_omitted_rather_than_shown_wrong(position, total):
    assert "Candidate" not in _text(ra.activity_html(
        _running(candidate=position, candidate_total=total)))


# ---------------------------------------------------------------------------------------------
# Totality and escaping.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    None, "", "abc", float("inf"), float("-inf"), float("nan"), -1, -0.4, object(), [], {}])
def test_every_elapsed_reading_is_total(value):
    """`int(float('inf'))` raises `OverflowError` -- a real bug this repository has already paid
    for once in `music_playlist._usable_int`. A status panel must never be the thing that breaks a
    render, so every reading is clamped rather than trusted."""
    assert ra._ms(value) >= 0
    markup = ra.activity_html(_running(job_ms=value, phase_ms=value, update_ms=value))
    assert markup.startswith("<div")


def test_every_pure_entry_point_is_total():
    """Drive every public function with hostile input. None may raise."""
    hostile = [None, "", 0, -1, 3.5, True, float("nan"), object(), [], {}, (), "<script>"]
    for value in hostile:
        ra.stage_label(value)
        ra.phase_label(value)
        state = ra.RenderActivityState(
            status=value, job_ms=value, phase_ms=value, job_token=str(value),
            phase_token=str(value), stage=value, stage_name=str(value), detail=str(value),
            done=value, total=value, unit=str(value), candidate=value, candidate_total=value,
            completed_stages=(value,), update_ms=value, update_token=str(value))
        ra.measured_percent(state)
        assert ra.activity_html(state).startswith("<div")


def test_everything_interpolated_is_escaped_and_the_panel_stays_one_line():
    """Every value reaching this markup can carry an angle bracket or a newline: an FFmpeg error,
    a Windows path, a `ProgressView` message."""
    nasty = 'a<script>alert("x")</script>\nsecond\n\nthird'
    markup = ra.activity_html(_running(detail=nasty, stage=5, stage_name=nasty, unit=nasty,
                                       done=1, total=2))
    assert "<script>" not in markup
    assert "\n" not in markup
    assert "&lt;script&gt;" in markup


def test_a_hostile_status_cannot_break_out_of_an_attribute():
    markup = ra.activity_html(ra.RenderActivityState(status='x" onload="alert(1)'))
    assert "onload" not in markup


# =============================================================================================
# TIER 1b -- the new read-only ProgressView projection.
# =============================================================================================


def _event(stage, kind, **kw):
    return ProgressEvent(stage=stage, kind=kind, message=kw.pop("message", ""), **kw)


def test_completed_stages_reports_only_stages_that_really_reported_end():
    view = ProgressView()
    assert view.completed_stages() == ()
    view.apply(_event(Stage.INPUT, EventKind.START))
    assert view.completed_stages() == ()
    view.apply(_event(Stage.INPUT, EventKind.END))
    view.apply(_event(Stage.AUDIO_BEATS, EventKind.START))
    assert view.completed_stages() == (0,)
    view.apply(_event(Stage.AUDIO_BEATS, EventKind.END))
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START))
    # Stages 2-4 never ran. They must NOT be reported as completed.
    assert view.completed_stages() == (0, 1)


def test_completed_stages_is_a_copy_and_ascending():
    view = ProgressView()
    for stage in (Stage.VIDEO_ANALYSIS, Stage.INPUT, Stage.SECTIONS):
        view.apply(_event(stage, EventKind.START))
        view.apply(_event(stage, EventKind.END))
    reported = view.completed_stages()
    assert reported == (0, 3, 5)
    assert isinstance(reported, tuple)
    assert reported is not view.completed_stages() or reported == view.completed_stages()


def test_the_panels_figures_come_from_active_measurement_and_match_it_exactly():
    """"Measured phase progress matches ProgressView exactly" -- driven through real events rather
    than asserted about."""
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=3620,
                      message="analysing"))
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.PROGRESS, current=758, total=3620,
                      message="analyzed"))
    measurement = view.active_measurement()
    markup = ra.activity_html(_running(
        stage=measurement.stage, stage_name=measurement.stage_title,
        detail=measurement.message, done=measurement.current, total=measurement.total,
        unit=measurement.unit, completed_stages=view.completed_stages()))
    assert _attr(markup, "data-bs-render-done") == "758"
    assert _attr(markup, "data-bs-render-total") == "3620"
    assert _attr(markup, "data-bs-render-stage") == "5"
    assert measurement.stage_title in _text(markup)


def test_an_uncounted_qwen_phase_reaches_the_panel_as_indeterminate():
    """Stage 5's Qwen phase is deliberately an uncounted STATE event under `phase="qwen"`, so
    `active_measurement()` reports `None` counts and the panel must stay indeterminate."""
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=300))
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.PROGRESS, current=300, total=300))
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.STATE, message="Qwen job 17 / 300",
                      data={"phase": "qwen"}))
    measurement = view.active_measurement()
    assert measurement.phase == "qwen"
    assert measurement.current is None and measurement.total is None
    markup = ra.activity_html(_running(
        stage=measurement.stage, detail=measurement.message,
        done=measurement.current, total=measurement.total))
    assert "bs-render-fill" not in markup
    assert _attr(markup, "data-bs-render-determinate") == "0"
    # And the stale 300 / 300 from the previous phase has NOT come with it.
    assert "300 / 300" not in _text(markup)


# =============================================================================================
# TIER 2 -- the shipped gui.py seams.
# =============================================================================================


def test_the_browser_timer_contract_is_present_and_is_the_shared_one():
    """These attributes are a LIVE DOM contract here, which is why the panel is a `gr.HTML`."""
    markup = ra.activity_html(_running(job_token="render1", phase_token="render1-3",
                                       update_token="render1-u9", job_ms=1000, phase_ms=20,
                                       update_ms=5))
    for name in ("data-running", "data-job-token", "data-job-ms",
                 "data-phase-token", "data-phase-ms",
                 "data-update-token", "data-update-ms"):
        _attr(markup, name)
    # The field names are the shared timer's, verbatim, so one installer serves both surfaces.
    for child in ("bs-cut-job-elapsed", "bs-cut-phase-elapsed", "bs-cut-update-elapsed"):
        assert child in markup, child


def test_the_panel_does_not_masquerade_as_a_raw_cutter_panel():
    """Putting `bs-cut-activity` on this panel would have made it tick with no change to the
    installer at all -- tempting and wrong, because a shared timer that works because one surface
    pretends to be another is a thing nobody can safely rename later. The installer's selector
    names both classes instead."""
    markup = ra.activity_html(_running())
    assert ra.PANEL_CLASS == "bs-render-activity"
    assert "bs-cut-activity" not in markup.split(">")[0]


def test_the_shared_installer_ticks_both_surfaces_and_is_still_the_only_one():
    source = _gui_source()
    assert '.querySelectorAll(".bs-cut-activity, .bs-render-activity")' in source
    # Three uniform fields now: job, phase, and the measurement age.
    block = source[source.index("CUT_ACTIVITY_JS = r\"\"\""):]
    block = block[:block.index('"""', 20)]
    fields = block[block.index("const FIELDS = ["):block.index("];", block.index("const FIELDS"))]
    assert fields.count('"data-') == 6, "exactly three (token, ms) field pairs"
    assert '["data-update-token", "data-update-ms", ".bs-cut-update-elapsed"]' in fields
    # No new installer, and still no network call or second interval.
    assert source.count("app.load(fn=None") == 4
    assert block.count("setInterval") == 1
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "setTimeout"):
        assert banned not in block, banned


def test_the_elapsed_figures_are_also_written_as_text_server_side():
    """With the interval blocked, not yet installed, or simply between ticks, the panel must still
    be correct as of the moment it was emitted rather than empty."""
    spoken = _text(ra.activity_html(_running(job_ms=3_725_000, phase_ms=65_000)))
    assert "1:02:05" in spoken      # the job clock, widened past an hour
    assert "01:05" in spoken        # the phase clock


def test_the_panel_is_a_gr_html_and_sits_above_the_status_box():
    """`gr.Markdown` strips every `data-*` attribute on the pinned version, which would leave the
    shared timer nothing to read and no clock at all between server frames."""
    source = _gui_source()
    assert "render_activity = gr.HTML(" in source
    assert "gr.Markdown" not in source[source.index("render_activity = gr.HTML("):
                                       source.index("status_output = gr.Textbox")]
    assert source.index("render_activity = gr.HTML(") < source.index(
        "status_output = gr.Textbox"), "the panel must be ABOVE the Status textbox"
    assert "elem_id='render-activity-panel'" in source


def test_the_detailed_status_report_is_kept_exactly_as_it_was():
    """"Keep the existing detailed Status report available." Same widget, label, size and id."""
    source = _gui_source()
    assert ("status_output = gr.Textbox(label='Status', interactive=False, "
            "value=get_ready_status(") in source
    assert "lines=4, max_lines=4, elem_id='status-output-box')" in source


def test_process_videos_three_value_contract_and_its_unbounded_wait_are_untouched():
    """The panel's clocks are advanced BY THE BROWSER, and its animation is a CSS keyframe, so a
    silent Stage 5 stretch stays visibly alive with no server frames. That is why no poll, timer,
    coordinator or endpoint was added, and why this generator's cadence did not need to change."""
    body = _body("process_video")
    assert "status_queue.get()" in body
    assert "status_queue.get(timeout" not in body
    assert "queue.Empty" not in body
    core = _body("_process_video_guarded_unlocked")
    assert "for video, status, state in render_stream:" in core
    assert ast.unparse(_func("process_video").returns) == "Iterator[StatusResult]"


def test_the_panel_rides_on_session_state_exactly_as_the_two_report_readouts_do():
    """The worker thread must never touch a Gradio component, so the panel is rendered where
    `ProgressView` lives and projected onto the widget by the core's loop."""
    assert "RENDER_ACTIVITY_KEY = 'render_activity_panel'" in _gui_source()
    assert "_publish_render_activity(session_state, activity, view, lifecycle)" in _body(
        "process_video")
    assert "(state or {}).get(RENDER_ACTIVITY_KEY) or gr.skip()" in _body(
        "_process_video_guarded_unlocked")


def test_no_refusal_carries_an_activity_frame():
    """The rule both existing activity surfaces already follow: the Raw Cutter's `refuse(...)`
    renders no panel because it performed no work, and Prepare & Verify emits no activity frame for
    a declined click. A timer on a refusal would be counting a job that never began."""
    seen = 0
    for name in ("_process_video_guarded_unlocked", "process_video_guarded",
                 "render_selected_variants_guarded"):
        node = _func(name)
        for yielded in [n for n in ast.walk(node) if isinstance(n, ast.Yield)]:
            rendered = ast.unparse(yielded)
            if "❌" not in rendered and "_busy" not in rendered:
                continue
            # The LAST element of the yielded tuple is the activity panel. Read it off the AST
            # rather than off the unparsed text: `rendered.rstrip(')')` strips the call's own
            # parentheses as well as the tuple's, so a text test passes on anything.
            assert isinstance(yielded.value, ast.Tuple), rendered
            assert ast.unparse(yielded.value.elts[-1]) == "gr.skip()", rendered
            seen += 1
    assert seen == 5, (
        "the core's source-gate refusal and its playlist refusal, the batch's build-request "
        "refusal, and both wrappers' mutex-busy refusals")


def test_the_terminal_chip_is_derived_from_the_typed_outcome_never_from_prose():
    """The same typed outcome the lifecycle is marked from, so the chip and the lifecycle are two
    readings of one classification rather than two derivations."""
    body = _body("process_video_guarded")
    assert "if outcome_kind is RenderOutcomeKind.CANCELLED:" in body
    assert "terminal_activity = 'cancelled'" in body
    assert "terminal_activity = 'completed'" in body
    assert "terminal_activity = 'failed'" in body
    # Nothing is read out of the status text to decide it.
    for banned in ("status.startswith", "'❌' in", "last_status ==", "in last_status"):
        assert banned not in body, banned


def test_the_status_literals_in_the_wrappers_equal_the_real_constants():
    """Literals rather than `fork_render_activity.STATUS_*`, for the measured def-time/exec-time
    reason `repeat_music`'s integer literal records -- these bodies are executed against
    synthesised namespaces in which the fork modules are stubs. Pinned so they cannot drift."""
    assert ra.STATUS_STARTING == "starting"
    assert ra.STATUS_QUEUED == "queued"
    assert ra.STATUS_RUNNING == "running"
    assert ra.STATUS_CANCELLING == "cancelling"
    assert ra.STATUS_COMPLETED == "completed"
    assert ra.STATUS_FAILED == "failed"
    assert ra.STATUS_CANCELLED == "cancelled"
    assert "_render_activity_panel(activity, 'starting')" in _body("process_video_guarded")
    assert "_render_activity_panel(activity, 'starting')" in _body(
        "render_selected_variants_guarded")


def test_cancelling_comes_from_the_lifecycle_by_attribute_and_cancel_gains_no_widget():
    """Cancel's own `outputs` stay exactly `[status_output]`, so it gains no second widget and no
    new writer; the chip follows on the render's next frame."""
    body = _body("_render_activity_status")
    assert "lifecycle.cancel_requested()" in body
    assert "str(" not in body and "parse" not in body
    source = _gui_source()
    click = next(node for node in ast.walk(_tree())
                 if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == "cancel_render_btn.click")
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in click.keywords}
    assert kwargs["outputs"] == "[status_output]"


def test_a_broken_panel_can_never_break_a_render():
    """The rule `progress.emit()` is built on, for the same reason: a broken status widget must not
    be able to lose hours of analysis."""
    for name in ("_publish_render_activity", "_render_activity_panel", "_render_activity_status"):
        body = _body(name)
        assert "except Exception" in body, name
    assert "pass" in _body("_publish_render_activity")


# ---------------------------------------------------------------------------------------------
# The reporter: one per render event, clocks and tokens.
# ---------------------------------------------------------------------------------------------


def _reporter():
    """The shipped reporter class, imported from the real module.

    `gui` cannot be imported on a bare interpreter, so the class body is AST-extracted and executed
    against a namespace carrying the two real collaborators it actually uses -- the reused
    `ActivityClock` and the pure renderer. Nothing about either is stubbed, so this exercises the
    shipped arithmetic rather than a model of it.
    """
    from beatsync_fork import raw_cutter_activity as fork_activity
    node = next(n for n in ast.walk(_tree())
                if isinstance(n, ast.ClassDef) and n.name == "_RenderActivityReporter")
    namespace = {
        "fork_activity": fork_activity,
        "fork_render_activity": ra,
        "ProgressView": ProgressView,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<reporter>", "exec"), namespace)
    return namespace["_RenderActivityReporter"]


def test_exactly_two_reporters_are_ever_constructed_and_only_by_the_mutex_owners():
    """ONE reporter per top-level render event -- the requirement, not a symmetry with the
    lifecycle: a reporter per `process_video()` call would restart the batch's total clock on
    candidate 2."""
    calls = _calls_named(_tree(), "_new_render_activity")
    assert len(calls) == 3, \
        "two wrapper constructions, plus the interlock's own transient one for its Starting frame"
    for name in ("process_video_guarded", "render_selected_variants_guarded"):
        assert "activity = _new_render_activity()" in _body(name), name
    # No module below the wrappers may construct one.
    for name in ("process_video", "_process_video_guarded_unlocked", "_process_video_impl"):
        assert "_new_render_activity()" not in _body(name), name


def test_the_total_clock_spans_the_whole_batch_and_the_phase_clock_does_not():
    """"Total elapsed time spans the complete batch; current phase timing belongs to the active
    candidate." Driven on the real reporter with an injected clock."""
    reporter = _reporter()(0.0)
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=10))

    reporter.set_candidate(1, 2)
    first = reporter.frame(10.0, view, status=ra.STATUS_RUNNING)
    assert _attr(first, "data-job-ms") == "10000"

    # Candidate 2 begins. The TOTAL clock keeps going; the PHASE clock restarts, because the same
    # stage on a different candidate is a different stretch of work.
    reporter.set_candidate(2, 2)
    view2 = ProgressView()
    view2.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=10))
    second = reporter.frame(30.0, view2, status=ra.STATUS_RUNNING)
    assert _attr(second, "data-job-ms") == "30000", "the total clock must NOT restart"
    assert int(_attr(second, "data-phase-ms")) < 1000, "the phase clock must restart"
    assert _attr(second, "data-phase-token") != _attr(first, "data-phase-token")
    assert _attr(second, "data-job-token") == _attr(first, "data-job-token")
    assert "Candidate 2 / 2" in _text(second)


def test_an_advancing_counter_does_not_restart_the_phase_clock():
    """The phase signature excludes the counts deliberately: an advancing Stage 5 counter is
    progress within one phase, not a new phase."""
    reporter = _reporter()(0.0)
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=100))
    first = reporter.frame(1.0, view, status=ra.STATUS_RUNNING)
    for n in (1, 2, 3):
        view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.PROGRESS, current=n, total=100))
        later = reporter.frame(1.0 + n, view, status=ra.STATUS_RUNNING)
    assert _attr(later, "data-phase-token") == _attr(first, "data-phase-token")
    assert int(_attr(later, "data-phase-ms")) >= 3000


def test_a_real_phase_change_does_restart_the_phase_clock():
    reporter = _reporter()(0.0)
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=100))
    before = reporter.frame(50.0, view, status=ra.STATUS_RUNNING)
    view.apply(_event(Stage.RENDER, EventKind.START, current=0, total=40))
    after = reporter.frame(51.0, view, status=ra.STATUS_RUNNING)
    assert _attr(after, "data-phase-token") != _attr(before, "data-phase-token")
    assert int(_attr(after, "data-phase-ms")) < 1000
    assert int(_attr(after, "data-job-ms")) == 51000


def test_the_measurement_age_re_anchors_on_a_new_measurement_and_grows_without_one():
    """"Display the age of the last measured progress update without claiming that a silent phase
    has failed.\""""
    reporter = _reporter()(0.0)
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=100))
    first = reporter.frame(10.0, view, status=ra.STATUS_RUNNING)
    assert int(_attr(first, "data-update-ms")) == 0

    # A frame rendered later with NO new event: the age has grown, and its token is unchanged, so
    # the browser keeps advancing the same anchor rather than resetting it.
    quiet = reporter.simple(70.0, status=ra.STATUS_RUNNING)
    assert _attr(quiet, "data-job-ms") == "70000"

    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.PROGRESS, current=1, total=100))
    fresh = reporter.frame(80.0, view, status=ra.STATUS_RUNNING)
    assert int(_attr(fresh, "data-update-ms")) == 0, "a new measurement resets the age"
    assert _attr(fresh, "data-update-token") != _attr(first, "data-update-token")
    spoken = _text(fresh)
    assert "last update" in spoken
    for banned in ("failed", "stuck", "hung", "not responding"):
        assert banned not in spoken.lower(), banned


def test_the_reporter_renders_a_cancelling_chip_without_touching_the_measurement():
    reporter = _reporter()(0.0)
    view = ProgressView()
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=5, total=10))
    markup = reporter.frame(3.0, view, status=ra.STATUS_CANCELLING)
    assert "bs-render-cancelling" in markup
    assert _attr(markup, "data-running") == "1", "a cancelling render is still running"
    assert _attr(markup, "data-bs-render-done") == "5"


def test_a_terminal_frame_still_reports_which_stages_completed():
    """Found by the browser smoke, and it is the inverse of this item's own defect.

    `simple()` renders the wrapper's own frames and has no `ProgressView`, so it used to pass
    `completed_stages=()`. A successful render therefore came to rest with all seven stage pills
    grey -- a panel claiming that nothing completed, for a render that had just completed
    everything. The reporter remembers the last set the pipeline actually REPORTED; that is a
    remembered measurement, not an inference, and on a failure "6 / 7 stages completed" is exactly
    the useful thing to still be able to see.
    """
    reporter = _reporter()(0.0)
    view = ProgressView()
    for stage in (Stage.INPUT, Stage.AUDIO_BEATS, Stage.FEATURES):
        view.apply(_event(stage, EventKind.START))
        view.apply(_event(stage, EventKind.END))
    view.apply(_event(Stage.VIDEO_ANALYSIS, EventKind.START, current=0, total=4))
    reporter.frame(5.0, view, status=ra.STATUS_RUNNING)

    for status in ra.TERMINAL_STATUSES:
        markup = reporter.simple(9.0, status=status)
        assert _attr(markup, "data-bs-render-completed-stages") == "0,1,2", status
        assert "3 / %d stages completed" % ra.STAGE_COUNT in _text(markup), status
        # Still no track and still frozen -- the fix adds a remembered count, not a bar.
        assert "bs-render-track" not in markup, status
        assert _attr(markup, "data-running") == "0", status


def test_the_starting_frame_legitimately_reports_no_completed_stage():
    """The empty set before anything has run is a true reading, not the defect above."""
    reporter = _reporter()(0.0)
    markup = reporter.simple(0.1, status=ra.STATUS_STARTING)
    assert _attr(markup, "data-bs-render-completed-stages") == ""
    assert "stages completed" not in _text(markup)


def test_a_frame_before_any_event_is_indeterminate_and_claims_no_stage():
    reporter = _reporter()(0.0)
    markup = reporter.frame(2.0, ProgressView(), status=ra.STATUS_RUNNING)
    assert _attr(markup, "data-bs-render-stage") == ""
    assert "bs-render-fill" not in markup
    assert _attr(markup, "data-job-ms") == "2000"


# ---------------------------------------------------------------------------------------------
# The interlocks.
# ---------------------------------------------------------------------------------------------

#: The four controls the feature disables, in the ONE order both handlers return them.
_INTERLOCK = ["process_btn", "render_selected_variants_btn", "prep_verify_btn", "sample_btn"]


def test_the_interlock_set_is_exactly_the_non_render_render_lock_holders_it_can_honestly_restore():
    source = _gui_source()
    assert ("_render_interlock_outputs = [process_btn, render_selected_variants_btn,\n"
            "                                     prep_verify_btn, sample_btn]") in source
    node = next(n for n in ast.walk(_tree())
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "_render_interlock_outputs")
    assert [e.id for e in node.value.elts] == _INTERLOCK


def test_the_render_lock_holder_count_is_unchanged_so_the_interlock_set_is_still_complete():
    """If a seventh acquirer ever appears, this fails and someone has to decide whether its control
    belongs in the interlock. That is the point: the set above is derived from this one."""
    acquires = [c for c in _calls_named(_tree(), "_RENDER_LOCK.acquire")
                if any(kw.arg == "blocking" for kw in c.keywords)]
    assert len(acquires) == 6


def test_the_two_raw_cutter_buttons_and_the_audio_cutter_are_deliberately_left_alone():
    """Both cutter buttons already have their own server-managed interactive state, so a render
    event writing them would be a SECOND authority over one widget -- and restoring them would
    mean guessing `interactive=True`, which offers Run with no plan. The Audio Cutter holds no
    mutex at all, so disabling it would invent a conflict the admission architecture does not
    recognise."""
    for name in ("cut_run_btn", "cut_batch_start_btn", "audio_cut_btn"):
        assert name not in _INTERLOCK
    node = next(n for n in ast.walk(_tree())
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "_render_interlock_outputs")
    rendered = ast.unparse(node)
    for name in ("cut_run_btn", "cut_batch_start_btn", "audio_cut", "confirm_btn",
                 "cancel_render_btn", "open_output_folder_btn", "main_tabs",
                 "prep_scan_btn", "prep_analyze_btn"):
        assert name not in rendered, name
    # And the Audio Cutter really does hold no mutex -- measured, not assumed.
    assert "_RENDER_LOCK" not in _body("_on_audio_cut")


def test_both_render_buttons_disable_before_the_render_and_restore_after_it():
    root = _tree()
    for button, render_fn in (("process_btn", "process_video_guarded"),
                              ("render_selected_variants_btn",
                               "render_selected_variants_guarded")):
        clicks = [n for n in ast.walk(root)
                  if isinstance(n, ast.Call) and ast.unparse(n.func) == "%s.click" % button]
        fns = [ast.unparse(dict((kw.arg, kw.value) for kw in c.keywords)["fn"]) for c in clicks]
        assert fns == ["_render_interlock_lock", render_fn], (button, fns)
        # The lock event writes the four controls plus the panel, and reads nothing.
        lock_kwargs = {kw.arg: ast.unparse(kw.value) for kw in clicks[0].keywords}
        assert lock_kwargs["inputs"] == "[]"
        assert lock_kwargs["outputs"] == "_render_interlock_outputs + [render_activity]"
        assert lock_kwargs["concurrency_limit"] == "None"
        assert "concurrency_id" not in lock_kwargs


def test_the_restore_is_chained_so_it_cannot_run_before_the_worker_was_joined():
    """Measured on gradio==6.19.0: a `.then()` link runs AFTER the generator's `finally`. So
    `process_video`'s finalizer has joined the worker and the wrapper has released `_RENDER_LOCK`
    before the controls come back -- which a terminal yield could not promise, because it runs
    inside the `try`."""
    source = _gui_source()
    assert source.count("fn=_render_interlock_restore, inputs=[source_state]") == 2
    assert source.count("outputs=_render_interlock_outputs,") == 2
    # Chained onto the RENDER registration, after the notification drain.
    for anchor in ("fn=_notify_drain, inputs=[], outputs=[notify_envelope],\n"
                   "            show_progress='hidden', concurrency_limit=None,\n",):
        assert anchor in source
    # It must not write the activity panel: the render's own terminal frame owns that.
    restore_calls = [n for n in ast.walk(_tree())
                     if isinstance(n, ast.Call)
                     and any(kw.arg == "fn" and ast.unparse(kw.value)
                             == "_render_interlock_restore" for kw in n.keywords)]
    assert len(restore_calls) == 2
    for call in restore_calls:
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert "render_activity" not in kwargs["outputs"]
        assert kwargs["inputs"] == "[source_state]"


def _interlock_handlers():
    """The two shipped interlock handlers, executed against a namespace with a real `gr` stand-in.

    Nothing about the decision logic is stubbed: the slot, its lock and the `source_state`
    protocol are all real objects, and only `gr.update` is modelled -- by a recorder, so the test
    can read what the handler actually asked for.
    """
    import threading

    class _Update:
        def __init__(self, **kw):
            self.kw = kw

        def __repr__(self):
            return "update(%r)" % (self.kw,)

        def __eq__(self, other):
            return isinstance(other, _Update) and other.kw == self.kw

    class _Gr:
        @staticmethod
        def update(**kw):
            return _Update(**kw)

    namespace = {
        "gr": _Gr,
        "Tuple": tuple,
        "time": time,
        "threading": threading,
        "_ACTIVE_RENDER_SLOT_LOCK": threading.Lock(),
        "_active_render_slot": None,
        "fork_render_activity": ra,
        "_RENDER_INTERLOCK_WIDGET_COUNT": 4,
    }
    root = _tree()
    namespace["_RenderActivityReporter"] = _reporter()
    for name in ("_render_is_active", "_render_interlock_pending_status",
                 "_render_interlock_restore", "_render_interlock_lock",
                 "_new_render_activity", "_render_activity_panel", "_source_ui_updates"):
        node = _func(name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<interlock>", "exec"), namespace)
    namespace["_Update"] = _Update
    return namespace


class _FakeSourceState:
    def __init__(self, confirmed, can_confirm=True):
        self._confirmed = confirmed
        self._can_confirm = can_confirm
        self.report_text = "report"

    def is_confirmed(self):
        return self._confirmed

    def can_confirm(self):
        return self._can_confirm

    def confirm_button_label(self):
        return "Confirm"

    def confirmation_status_text(self):
        return "status"


def test_the_restore_re_derives_create_from_the_source_gate_and_never_blindly_enables_it():
    ns = _interlock_handlers()
    update = ns["_Update"]
    confirmed = ns["_render_interlock_restore"](_FakeSourceState(True))
    assert confirmed[0] == update(interactive=True)
    unconfirmed = ns["_render_interlock_restore"](_FakeSourceState(False))
    assert unconfirmed[0] == update(interactive=False), \
        "a render must not be offered over an unconfirmed source set"
    # The three diagnostics have no competing authority, so they come straight back.
    for element in unconfirmed[1:]:
        assert element == update(interactive=True)
    assert len(unconfirmed) == 4


def test_a_still_active_render_keeps_every_control_locked():
    """"Never let a delayed event from an older invocation unlock controls belonging to a newer
    active job." An older render's `.then()` arriving while a newer one is live changes nothing."""
    ns = _interlock_handlers()
    ns["_active_render_slot"] = ("newer-invocation", object())
    result = ns["_render_interlock_restore"](_FakeSourceState(True))
    assert len(result) == 4
    for element in result:
        assert element.kw == {}, "no change at all while a render is still active"


def test_an_unusable_source_state_fails_closed_on_the_one_control_that_starts_a_render():
    ns = _interlock_handlers()
    update = ns["_Update"]
    result = ns["_render_interlock_restore"](object())
    assert result[0].kw == {}, "no claim either way about whether a render may start"
    for element in result[1:]:
        assert element == update(interactive=True)


def test_the_lock_half_disables_all_four_and_puts_the_panel_live():
    ns = _interlock_handlers()
    update = ns["_Update"]
    result = ns["_render_interlock_lock"]()
    assert len(result) == 5
    for element in result[:4]:
        assert element == update(interactive=False)
    assert "bs-render-starting" in result[4]
    assert _attr(result[4], "data-running") == "1"


def test_a_second_session_clicking_during_a_render_is_told_it_is_queued():
    """The measured M4 behaviour, surfaced honestly: on the pinned version that click waits in the
    shared lane and is admitted after the first render releases, so `Running` would be false and
    showing nothing is the dead-button complaint inverted."""
    ns = _interlock_handlers()
    assert ns["_render_interlock_pending_status"]() == "starting"
    ns["_active_render_slot"] = ("someone-elses-render", object())
    assert ns["_render_interlock_pending_status"]() == "queued"
    panel = ns["_render_interlock_lock"]()[4]
    assert "bs-render-queued" in panel
    assert ra.DETAIL_QUEUED in _text(panel)


def test_the_source_gate_cannot_re_enable_create_underneath_a_running_render():
    """Scan and Confirm take no mutex, so they run happily while a render does -- and
    `_source_ui_updates` is the other authority over Create Music Video's enabled state. Without
    this, re-confirming mid-render undid the interlock from an unrelated handler."""
    ns = _interlock_handlers()
    update = ns["_Update"]
    idle = ns["_source_ui_updates"](_FakeSourceState(True))
    assert idle[3] == update(interactive=True)
    ns["_active_render_slot"] = ("a-live-render", object())
    during = ns["_source_ui_updates"](_FakeSourceState(True))
    assert during[3] == update(interactive=False)
    # Confirm itself is untouched -- it is not a conflicting operation.
    assert during[1].kw["interactive"] is True


def test_render_is_active_is_total_and_fails_towards_no_render():
    """A diagnostic that raised would be worse than one that is wrong: failing towards "no render"
    leaves every real guard exactly where it was."""
    ns = _interlock_handlers()

    class _Exploding:
        def __enter__(self):
            raise RuntimeError("boom")

        def __exit__(self, *a):
            return False

    ns["_ACTIVE_RENDER_SLOT_LOCK"] = _Exploding()
    assert ns["_render_is_active"]() is False
    assert ns["_render_interlock_pending_status"]() == "starting"


def test_ui_disabling_is_never_treated_as_an_authorization_boundary():
    """`_RENDER_LOCK.acquire()` remains the sole admission authority, and the check-then-act form
    this file bans outright is still absent."""
    # In CODE, not in prose: `gui.py` discusses `if not _RENDER_LOCK.locked()` in a comment
    # explaining why the check-then-act form is banned, so a raw-text search finds the explanation.
    assert not _calls_named(_tree(), "_RENDER_LOCK.locked")
    assert "_RENDER_LOCK.locked" not in _gui_code()
    for name in ("_render_interlock_lock", "_render_interlock_restore", "_render_is_active",
                 "_render_interlock_pending_status"):
        body = _body(name)
        for banned in ("_RENDER_LOCK", "acquire", "release", "request_cancel",
                       "mark_terminal", "session_state", "subprocess", "Popen"):
            assert banned not in body, (name, banned)


def test_the_feature_adds_no_coordinator_no_poll_and_no_subprocess_site():
    """"Do not introduce another progress coordinator, background worker or polling endpoint.\""""
    assert len(_calls_named(_tree(), "subprocess.run")) == 2, \
        "the delivery preview and the Director, still -- and nothing new"
    for name in ("_render_interlock_lock", "_render_interlock_restore",
                 "_publish_render_activity", "_render_activity_panel",
                 "_render_activity_status", "_render_is_active", "_new_render_activity"):
        body = _body(name)
        for banned in ("Thread", "ThreadPool", "Executor", "Queue", "setInterval",
                       "requests", "urlopen", "schedule", "Timer"):
            assert _word_absent(body, banned), (name, banned)
    reporter = _class_code("_RenderActivityReporter")
    for banned in ("Thread", "ThreadPool", "Executor", "Queue", "setInterval",
                   "requests", "urlopen", "schedule", "Timer", "subprocess"):
        assert _word_absent(reporter, banned), banned


def test_the_panel_is_an_output_only_and_never_a_render_input():
    root = _tree()
    for button in ("process_btn", "render_selected_variants_btn"):
        for call in [n for n in ast.walk(root)
                     if isinstance(n, ast.Call) and ast.unparse(n.func) == "%s.click" % button]:
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
            assert "render_activity" not in kwargs.get("inputs", "")
    # It is absent from the source gate and from preparation, so it can never clear a confirmation.
    for name in ("source_outputs", "prep_outputs"):
        node = next((n for n in ast.walk(root) if isinstance(n, ast.Assign)
                     and getattr(n.targets[0], "id", None) == name), None)
        if node is not None:
            assert "render_activity" not in ast.unparse(node)


def test_the_panel_is_appended_last_in_both_render_output_lists():
    """Appended LAST so every pre-existing position keeps its meaning -- the discipline every
    previous append in this file followed."""
    root = _tree()
    expected = {
        "process_btn": ["video_output", "status_output", "session_state", "audio_layers_report",
                        "smart_mix_report", "render_invocation_state", "render_activity"],
        "render_selected_variants_btn": ["video_output", "status_output", "session_state",
                                         "render_batch_summary", "render_invocation_state",
                                         "render_activity"],
    }
    for button, names in expected.items():
        call = next(n for n in ast.walk(root)
                    if isinstance(n, ast.Call) and ast.unparse(n.func) == "%s.click" % button
                    and any(kw.arg == "fn" and "guarded" in ast.unparse(kw.value)
                            for kw in n.keywords))
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert [e.id for e in kwargs["outputs"].elts] == names, button


def test_the_feature_touches_no_creative_playlist_or_frame_lock_behaviour():
    """A STOP boundary, asserted rather than promised."""
    code = _module_code()
    for banned in ("creative", "playlist", "seed", "cut_density", "fps", "frame_aligned",
                   "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "cache", "ffmpeg"):
        assert _word_absent(code, banned), banned
    reporter = _class_code("_RenderActivityReporter")
    for banned in ("creative", "playlist", "cache", "seed", "ffmpeg", "music_repeat"):
        assert _word_absent(reporter, banned), banned
