"""[FORK] Digital-Union (UX-RENDER-ACTIVITY V1): the Render tab's live activity read-out.

The owner's complaint is narrow and measurable: during a long render -- above all Stage 5 against
thousands of sources -- the Render tab's only sign of life is a four-line `Status` textbox, and a
textbox that has not changed for twenty minutes is indistinguishable from a frozen application.

Scan and Analyze already had this answered by `prep_activity.py`, and the Raw Cutter by
`raw_cutter_activity.py`. This module is the same answer for the render, and it deliberately reuses
both of those contracts rather than inventing a third vocabulary:

* the **four tones** and the chip/dot shape come from the Raw Cutter panel, so the application does
  not teach an operator three colour languages;
* the **elapsed formatter** is imported, not redefined -- there is one `format_elapsed` in this
  codebase and `prep_activity` already imports the same one;
* the **browser timer contract** (`data-running`, `data-job-token`, `data-job-ms`,
  `data-phase-token`, `data-phase-ms`, and the `.bs-cut-*-elapsed` child classes) is the Raw
  Cutter's, unchanged, so `gui.CUT_ACTIVITY_JS` ticks this panel after a one-token broadening of
  its selector and **no new installer, timer, coordinator, worker or endpoint**.

What this module is
-------------------
Pure policy and formatting, stdlib-only, importing no upstream runtime (CLAUDE.md's hard rule for
`beatsync_fork`) -- so every honesty rule below is testable on a bare interpreter.

What this module is deliberately NOT
------------------------------------
* **Not a clock.** It reads no time source at all. Every elapsed figure is a reading the caller
  took from `time.perf_counter()`. A formatter with its own clock could not be tested without
  sleeping and would be measuring the wrong interval anyway.
* **Not a second progress protocol.** It emits no `ProgressEvent`, defines no stage, no
  `ProgressView` phase key, and **parses nothing out of rendered text**. Every number it prints was
  handed to it, and the only legitimate source of those numbers is
  `ProgressView.active_measurement()`.
* **Not a coordinator.** It starts nothing, owns no thread, process, queue or lock, and it never
  decides whether a render may run. `_RENDER_LOCK` remains the sole admission authority and
  `RenderLifecycle` the sole cancellation authority. Nothing here is ever an authorization
  boundary: this module can word a panel and do nothing else.
* **Not a progress estimator.** No ETA, no rate, no throughput, no weighted stage completion and no
  overall percentage -- see the honesty rules.

The honesty rules, which are the whole point
--------------------------------------------
* **A determinate bar requires two measured integers.** `done`/`total` render as a filled track
  only when both are real non-negative `int`s with `total > 0` and `done <= total`. `bool` is
  rejected first because it subclasses `int`. Anything else is **indeterminate**, and an
  indeterminate track carries **no fill element at all** -- not a full one (which paints exactly
  what a complete `N / N` paints, the UX-PREP-VISUAL-PROGRESS F2 defect) and not a zero-width one
  (which would be a measurement nobody took).
* **Nothing is inherited across a phase transition.** A `RenderActivityState` carries one phase's
  figures and `phase_token` identifies which phase they belong to. There is no code path here that
  can carry a count, a total, a unit, a percentage or a phase clock from one phase into the next,
  because this module holds no state between calls at all.
* **There is no overall percentage, and `measured_percent` is the only route to any percentage.**
  A test pins that `* 100.0` appears exactly once in this module. The completed-stage figure is a
  **count**, explicitly labelled as one, and never a bar.
* **Elapsed time is not evidence of progress.** The panel therefore reports the measured age of the
  last progress update separately, and says nothing about whether a silent phase has failed --
  because a silent Stage 5 phase is usually working perfectly.
* **A terminal panel freezes.** `running` is false for every terminal status, which is what tells
  the browser timer to write the server's frozen figure and stop advancing it.
* **An unknown status fails closed to idle** -- never to running (an animation over a dead worker)
  and never to completed (the one answer a caller must not receive by accident).
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from beatsync_fork.progress import STAGE_TITLES
from beatsync_fork.raw_cutter_activity import format_elapsed

__all__ = [
    "STATUS_IDLE", "STATUS_STARTING", "STATUS_QUEUED", "STATUS_RUNNING", "STATUS_CANCELLING",
    "STATUS_COMPLETED", "STATUS_FAILED", "STATUS_CANCELLED",
    "PANEL_STATUSES", "TERMINAL_STATUSES", "ACTIVE_STATUSES", "STATUS_LABELS",
    "PIPELINE_STAGES", "STAGE_COUNT", "stage_label",
    "PHASE_LABELS", "phase_label",
    "DETAIL_IDLE", "DETAIL_STARTING", "DETAIL_QUEUED",
    "RenderActivityState", "measured_percent", "activity_html",
    "PANEL_CLASS",
]

# ---------------------------------------------------------------------------------------------
# The lifecycle vocabulary the panel renders.
#
# Seven of these are the owner's own list. `STATUS_QUEUED` is an EIGHTH, and it exists because of a
# measured fact rather than a guess: on the pinned gradio==6.19.0, a render click from a SECOND
# browser session, made while another session owns `_RENDER_LOCK`, is neither refused nor run --
# it sits in the shared `concurrency_limit=1` lane and is admitted 33 ms after the first render
# releases the mutex (measured; see the task evidence). Showing that waiting session `Running`
# would be false and showing it nothing is the dead-button complaint this item exists to remove,
# so the state it is genuinely in gets a name.
# ---------------------------------------------------------------------------------------------

#: No render has been started in this session yet. Neutral: idle must not read as success or fault.
STATUS_IDLE = "idle"
#: Admitted and about to work -- the frame the wrapper emits before the source gate.
STATUS_STARTING = "starting"
#: Accepted by the browser, but another render owns the mutex. See the note above.
STATUS_QUEUED = "queued"
#: A render worker is alive right now.
STATUS_RUNNING = "running"
#: A cancellation has been requested and the worker has not yet reached a safe boundary.
STATUS_CANCELLING = "cancelling"
#: Terminal.
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: The three states in which a render worker may still be alive, so the clocks keep advancing.
#: `STATUS_QUEUED` is here because the operator's job genuinely has not finished -- it has not
#: started. `STATUS_STARTING` likewise.
ACTIVE_STATUSES = (STATUS_STARTING, STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLING)

#: The three states in which the figures are frozen and must never advance again.
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

PANEL_STATUSES = (STATUS_IDLE,) + ACTIVE_STATUSES + TERMINAL_STATUSES

#: The chip text. Present participle while something is happening, past tense once it has
#: happened, so the chip alone distinguishes "it is running" from "it ran".
STATUS_LABELS: Mapping[str, str] = {
    STATUS_IDLE: "Idle",
    STATUS_STARTING: "Starting",
    STATUS_QUEUED: "Queued",
    STATUS_RUNNING: "Running",
    STATUS_CANCELLING: "Cancelling",
    STATUS_COMPLETED: "Completed",
    STATUS_FAILED: "Failed",
    STATUS_CANCELLED: "Cancelled",
}

#: The same tones `prep_activity` and `raw_cutter_activity` already use. Colour is never the only
#: signal: the chip TEXT and the `bs-render-<status>` CLASS carry the state too, and both survive
#: into the DOM because this panel is rendered through `gr.HTML` rather than `gr.Markdown`.
_STATUS_TONES: Mapping[str, str] = {
    STATUS_IDLE: "#8b949e",          # grey  -- nothing to report, which is not a fault
    STATUS_STARTING: "#2f81f7",      # blue  -- accepted, work about to begin
    STATUS_QUEUED: "#d29922",        # amber -- accepted but waiting on another render
    STATUS_RUNNING: "#2f81f7",       # blue  -- genuinely running
    STATUS_CANCELLING: "#d29922",    # amber -- winding down, not yet stopped
    STATUS_COMPLETED: "#2ea043",     # green -- a terminal success
    STATUS_FAILED: "#f85149",        # red   -- a terminal failure
    STATUS_CANCELLED: "#d29922",     # amber -- stopped on request, which is not a failure
}

#: The panel's own class, which is also what `gui.CUT_ACTIVITY_JS`' selector matches on.
#:
#: Deliberately **not** also the Raw Cutter's `bs-cut-activity`. Putting both classes on the panel
#: would have made it tick with no change to the installer at all, which is tempting and wrong: the
#: render panel is not a cutter panel, and a shared timer that works because one surface pretends to
#: be another is a thing nobody can safely rename later. The installer's selector names both classes
#: explicitly instead, so what is shared is visible at the one place that does the sharing.
#:
#: The *field* names it reuses (`data-job-token`, `data-job-ms`, `data-phase-token`,
#: `data-phase-ms`, and the `.bs-cut-*-elapsed` child classes) ARE the cutter's, verbatim and on
#: purpose: those are the shared timer's parameter names rather than any one surface's property, and
#: a second set of them would be a second timer contract to keep in step. Each child also carries a
#: render-named class of its own for styling, so no stylesheet depends on the `bs-cut-` spelling.
PANEL_CLASS = "bs-render-activity"

# ---------------------------------------------------------------------------------------------
# Stage and phase names, in the operator's words.
#
# `PIPELINE_STAGES` is DERIVED from the pipeline's own `progress.STAGE_TITLES` rather than
# restated, because a second list of stage numbers is a second thing able to drift from what the
# pipeline publishes -- and because the completed/pending denominator then has a real declared
# property of the pipeline behind it instead of a number somebody typed. `progress` is a sibling
# fork module and stdlib-only, so this import does not weaken CLAUDE.md's hard rule.
# ---------------------------------------------------------------------------------------------

#: The stage numbers the pipeline declares, ascending. Stage 0 is the pre-Stage-1 input work.
PIPELINE_STAGES: Tuple[int, ...] = tuple(sorted(int(s) for s in STAGE_TITLES))
STAGE_COUNT = len(PIPELINE_STAGES)

#: Short names for the stage pills, because seven of these sit side by side in one narrow row.
#: This is **presentation only** and deliberately not a second naming authority: the full title in
#: the detail line comes from `ActiveMeasurement.stage_title`, i.e. from `STAGE_TITLES` itself, and
#: a stage with no short name here falls back to that same table rather than to an invention.
_STAGE_PILL_LABELS: Mapping[int, str] = {
    1: "Beats",
    2: "Energy",
    4: "Cuts",
    5: "Analysis",
    6: "Render",
}


def stage_label(stage: Optional[int]) -> str:
    """The pill name for a stage number.

    Falls back to the pipeline's own title, and then to the bare number -- never to a guessed name,
    because a label for a stage this module does not know would be a fabricated state.
    """
    try:
        number = int(stage)
    except (TypeError, ValueError):
        return ""
    short = _STAGE_PILL_LABELS.get(number)
    if short:
        return short
    return STAGE_TITLES.get(number) or "Stage %d" % number


#: `ProgressView` phase keys -> words. The keys are the pipeline's own (`prores_convert`,
#: `prores_extract`, `assembly`, `qwen`); anything else is the stage's main phase and has no
#: separate name, because the stage title already names it.
#:
#: An unknown key renders as NOTHING rather than as a prettified version of itself: a phase key is
#: an internal identifier, and showing `source_scan` to an operator tells them only that a
#: programmer was here.
PHASE_LABELS: Mapping[str, str] = {
    "qwen": "Semantic tagging (Qwen)",
    "assembly": "Assembling the timeline",
    "prores_convert": "Converting sources",
    "prores_extract": "Extracting clips",
}


def phase_label(phase: Optional[str]) -> str:
    return PHASE_LABELS.get(str(phase or ""), "")


#: Detail lines for the states that have no measurement of their own to describe.
DETAIL_IDLE = "No render running."
DETAIL_STARTING = "Preparing to render."
DETAIL_QUEUED = "Waiting for another render to finish."


def _usable_count(value) -> Optional[int]:
    """An `int` this module will treat as a measurement, or `None`.

    `bool` is rejected first because it subclasses `int` -- the discipline
    `prep_activity._measured_counts`, `library_prep.normalize_batch_size` and
    `variation.normalize_seed` all use. A value this function accepts can become a determinate
    figure on screen, so "nearly a number" must fail closed.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _measured_counts(done, total):
    """`(done, total)` when the backend really measured both, else `None`."""
    done_i = _usable_count(done)
    total_i = _usable_count(total)
    if done_i is None or total_i is None:
        return None
    if total_i <= 0 or done_i < 0 or done_i > total_i:
        return None
    return (done_i, total_i)


def _measured_position(position, total):
    """`(position, total)` for a usable ONE-BASED batch position, else `None`.

    Deliberately not `_measured_counts`, which is the rule for a *completion count* and therefore
    accepts `0 / N` -- a true and important reading for a phase that has finished nothing. A
    candidate position is an ordinal, so `Candidate 0 / 4` is not a modest reading of a real
    quantity, it is a position that does not exist. Caught by this module's own totality test
    before it could ever reach a panel.
    """
    counts = _measured_counts(position, total)
    if counts is None or counts[0] < 1:
        return None
    return counts


def _ms(value) -> int:
    """A non-negative whole number of milliseconds from a caller's reading.

    Total for every input a caller can realistically produce, including `inf` -- `int(float('inf'))`
    raises `OverflowError`, which is a real bug this repository has already paid for once in
    `music_playlist._usable_int`.
    """
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


@dataclass(frozen=True)
class RenderActivityState:
    """One in-flight observation of a render. Every field is something the caller measured.

    Frozen, because a reading is a fact about a moment rather than a mutable handle -- and because
    a consumer holding one must not be able to edit the panel's inputs through it.

    `done`/`total` are `None` for an uncounted phase, which is **not** zero: only two real measured
    integers may license a determinate bar. `candidate`/`candidate_total` are `None` for a single
    render, which is **not** `1 / 1`: a single render has no candidate context and claiming one
    would be inventing it.
    """

    status: str = STATUS_IDLE
    #: The job clock: total elapsed for the whole render event, including a whole 2-4 candidate
    #: batch. A caller's `time.perf_counter()` reading, in milliseconds.
    job_ms: float = 0.0
    #: The phase clock: elapsed for the CURRENT stage/phase of the CURRENT candidate.
    phase_ms: float = 0.0
    #: Identifies which render event the job clock belongs to, and which phase the phase clock
    #: belongs to. The browser anchors its own interval on these, which is what makes the display
    #: survive a Gradio re-render without restarting.
    job_token: str = ""
    phase_token: str = ""
    #: The active stage, its short name, and the words for the active phase.
    stage: Optional[int] = None
    stage_name: str = ""
    #: The current activity description -- `ActiveMeasurement.message`, i.e. the pipeline's own
    #: words about what it is doing, never a sentence composed here from a stage number.
    detail: str = ""
    #: The measured figures for the active phase, and the unit they count.
    done: Optional[int] = None
    total: Optional[int] = None
    unit: str = ""
    #: Variant Lab batch context. Both `None` for a single render.
    candidate: Optional[int] = None
    candidate_total: Optional[int] = None
    #: Which pipeline stages have actually reported END, as a count and as the set to light up.
    completed_stages: Tuple[int, ...] = ()
    #: The measured age of the last progress update, and a token that changes when a new
    #: measurement arrives so the browser re-anchors instead of advancing a stale reading.
    update_ms: Optional[float] = None
    update_token: str = ""

    @property
    def measured(self):
        """The determinate counts, or `None` for an uncounted phase."""
        return _measured_counts(self.done, self.total)

    @property
    def running(self) -> bool:
        """Whether the browser timer should keep advancing these figures.

        Derived from the status rather than passed in, so a terminal panel cannot be emitted with
        a live clock by a caller that forgot a flag.
        """
        return self.status in ACTIVE_STATUSES

    @property
    def completed_count(self) -> int:
        return len(self.completed_stages)


def measured_percent(state: RenderActivityState) -> Optional[float]:
    """Completion percentage of the ACTIVE PHASE, from its two measured integers, or `None`.

    The one and only route to a percentage in this module, and a test pins that `* 100.0` appears
    exactly once here. There is deliberately no overall-render percentage and no weighted
    stage-completion percentage: the stages have wildly different and data-dependent costs, so any
    single number spanning them would be a prediction dressed as a measurement.
    """
    counts = state.measured
    if counts is None:
        return None
    done, total = counts
    return (done * 100.0) / total


def _tone(status: str) -> str:
    return _STATUS_TONES.get(status, _STATUS_TONES[STATUS_IDLE])


def _safe_status(status) -> str:
    """A status this module will render, failing closed to `STATUS_IDLE`."""
    text = str(status or "")
    return text if text in PANEL_STATUSES else STATUS_IDLE


def _one_line(text) -> str:
    """Collapse every run of whitespace to one space.

    This panel goes into a `gr.HTML`, so a newline would not end the block the way it does in a
    Markdown component -- but every value reaching this markup can still contain one (an FFmpeg
    error, a Windows path, a `ProgressView` message), and a one-line status slot that silently
    became three lines would reflow the panel on every frame. Layout only: no word, number, order
    or character other than whitespace changes.
    """
    return " ".join(str(text or "").split())


def _chip(status: str) -> str:
    """The state chip: a tone-coloured dot, then the state in words."""
    tone = _tone(status)
    return ('<span class="bs-render-chip" style="color:%s;">'
            '<span class="bs-render-dot" style="background:%s;"></span>%s</span>'
            % (tone, tone, _html.escape(STATUS_LABELS.get(status, status.title()))))


def _track(state: RenderActivityState) -> str:
    """The bar, rendered ONLY while something is active.

    The two branches differ in **which elements exist**, not in a width:

    ```
    measured      <div class="bs-render-track" aria-valuenow="NN.N">  + bs-render-fill + sweep
    not measured  <div class="bs-render-track bs-render-indeterminate">               + sweep
    ```

    Three rules are inherited wholesale from `prep_activity._track`, each of which was a real
    defect there:

    * **No fill element at all when nothing measured the phase** (F2). A `width:100%` fill paints
      exactly what a genuinely complete `N / N` paints, so an uncounted phase looked finished; a
      `width:0` fill is a quantity nobody measured. Emitting none is strictly better than dimming
      one, because then no element's width can be read as a figure.
    * **The sweep is a SIBLING of the fill and spans the whole track**, never nested inside it. On
      the fill, a determinate `0 / 6363` renders a zero-width element and the animation that is
      supposed to mean "alive" has nowhere to travel -- dead at exactly the count being complained
      about.
    * **`aria-valuenow` is OMITTED when indeterminate**, which is ARIA's own rule, because a screen
      reader announces a zero as a measurement.

    A terminal panel renders no track at all: a full bar on completion would be a measurement
    nobody took, and a bar on a failure would claim the work got somewhere.
    """
    percent = measured_percent(state)
    sweep = '<div class="bs-render-sweep"></div>'
    if percent is None:
        return ('<div class="bs-render-track bs-render-indeterminate" role="progressbar" '
                'aria-valuemin="0" aria-valuemax="100">%s</div>' % sweep)
    bounded = max(0.0, min(100.0, percent))
    return ('<div class="bs-render-track" role="progressbar" aria-valuemin="0" '
            'aria-valuemax="100" aria-valuenow="%.1f">'
            '<div class="bs-render-fill" style="width:%.1f%%;"></div>%s</div>'
            % (bounded, bounded, sweep))


def _stage_pills(state: RenderActivityState) -> str:
    """One pill per declared pipeline stage: completed, active, or pending.

    This is the requirement's "completed and pending stage indicators", and it is a **count**
    rather than a bar -- which is exactly the licence requirement 2 grants ("a completed stage
    count is acceptable when explicitly labelled as such"). The denominator is
    `len(PIPELINE_STAGES)`, a real declared property of the pipeline, not a guess; and a stage that
    a particular run never reports simply stays pending, which is honest, because it never ran.

    `completed` means the stage reported END. It is never inferred from the active stage number:
    stages do not have to finish in order for a *reader* to assume they did, and assuming it is how
    a skipped stage would be reported as done.
    """
    done = set(int(s) for s in state.completed_stages if _usable_count(s) is not None)
    active = state.stage if _usable_count(state.stage) is not None else None
    cells = []
    for number in PIPELINE_STAGES:
        if number in done:
            kind = "done"
        elif active is not None and number == active:
            kind = "active"
        else:
            kind = "pending"
        cells.append('<span class="bs-render-pill bs-render-pill-%s">%s</span>'
                     % (kind, _html.escape(stage_label(number))))
    return '<div class="bs-render-pills">%s</div>' % "".join(cells)


def _detail_text(state: RenderActivityState) -> str:
    """The one-line activity description: the stage, the phase, and the measured figures.

    Every component is something the caller measured or the pipeline said. The counts are rendered
    as text as well as a bar deliberately: a bar may not show a quantity that does not exist, so
    when a phase is uncounted the absence has to be visible somewhere, and the difference between
    "measured 0 / 6363" and "nobody counted this" lives in this line.
    """
    bits = []
    if _usable_count(state.stage) is not None:
        name = _one_line(state.stage_name) or stage_label(state.stage)
        bits.append("Stage %d — %s" % (int(state.stage), name))
    detail = _one_line(state.detail)
    if detail:
        bits.append(detail)
    counts = state.measured
    if counts is not None:
        done, total = counts
        percent = measured_percent(state)
        unit = _one_line(state.unit)
        bits.append("%d / %d%s (%.1f%%)" % (done, total, (" " + unit) if unit else "", percent))
    return " · ".join(bits)


def activity_html(state: RenderActivityState) -> str:
    """The panel. One `gr.HTML` fragment; every interpolated value escaped.

    The `data-*` attributes are a **live DOM contract** here, not merely a server-side one, and
    that is the whole reason this panel is a `gr.HTML` and not a `gr.Markdown`: measured against
    the served `gradio==6.19.0`, a `gr.Markdown` strips every `data-*`, `role` and `aria-*`
    attribute, which would leave `gui.CUT_ACTIVITY_JS` with nothing to read and no clock at all.

    ```
    data-running        1 while a worker may be alive, 0 for a frozen terminal panel
    data-job-token      anchors the TOTAL elapsed clock; stable for the whole render event
    data-job-ms         the server's own measured reading of it
    data-phase-token    anchors the PHASE clock; changes on a real phase change
    data-phase-ms       the server's own measured reading of it
    data-update-token   anchors the "last update" age; changes when a new measurement arrives
    data-update-ms      the server's own measured reading of it
    ```

    Each elapsed figure is **also written as text, server-side**. That is deliberate: with the
    browser interval blocked, not yet installed, or simply between ticks, the panel is still
    correct as of the moment it was emitted rather than empty. The interval only ever overwrites a
    panel whose `data-running` is `1`.
    """
    status = _safe_status(state.status)
    counts = state.measured
    percent = measured_percent(state)
    terminal = status in TERMINAL_STATUSES

    if status == STATUS_IDLE:
        detail = DETAIL_IDLE
    elif status == STATUS_QUEUED:
        detail = DETAIL_QUEUED
    else:
        detail = _detail_text(state) or (DETAIL_STARTING if status == STATUS_STARTING else "")

    head = ['<div class="bs-render-head">', _chip(status)]
    cand = _measured_position(state.candidate, state.candidate_total)
    if cand is not None:
        head.append('<span class="bs-render-candidate">Candidate %d / %d</span>' % cand)
    head.append('<span class="bs-render-clocks">')
    head.append('<span class="bs-render-clock-label">Elapsed</span>'
                '<span class="bs-cut-job-elapsed bs-render-clock">%s</span>'
                % _html.escape(format_elapsed(_ms(state.job_ms))))
    if not terminal and status != STATUS_IDLE:
        head.append('<span class="bs-render-clock-label">Stage</span>'
                    '<span class="bs-cut-phase-elapsed bs-render-clock">%s</span>'
                    % _html.escape(format_elapsed(_ms(state.phase_ms))))
    head.append('</span>')
    head.append('</div>')

    rows = [
        '<div class="%s bs-render-%s" data-running="%s" '
        'data-job-token="%s" data-job-ms="%d" '
        'data-phase-token="%s" data-phase-ms="%d" '
        'data-update-token="%s" data-update-ms="%s" '
        'data-bs-render-status="%s" data-bs-render-stage="%s" '
        'data-bs-render-determinate="%s" data-bs-render-done="%s" data-bs-render-total="%s" '
        'data-bs-render-percent="%s" data-bs-render-candidate="%s" '
        'data-bs-render-candidate-total="%s" data-bs-render-completed-stages="%s" '
        'style="border-left:3px solid %s;">'
        % (PANEL_CLASS, _html.escape(status, quote=True),
           "0" if terminal or status == STATUS_IDLE else "1",
           _html.escape(state.job_token, quote=True), _ms(state.job_ms),
           _html.escape(state.phase_token, quote=True), _ms(state.phase_ms),
           _html.escape(state.update_token, quote=True),
           "" if state.update_ms is None else _ms(state.update_ms),
           _html.escape(status, quote=True),
           "" if _usable_count(state.stage) is None else int(state.stage),
           "1" if percent is not None else "0",
           "" if counts is None else counts[0],
           "" if counts is None else counts[1],
           "" if percent is None else format(percent, ".1f"),
           # The SAME `cand` the head rendered, so the machine-readable contract and the words an
           # operator reads can never disagree about whether there is a batch position at all.
           "" if cand is None else cand[0],
           "" if cand is None else cand[1],
           ",".join(str(int(s)) for s in state.completed_stages
                    if _usable_count(s) is not None),
           _tone(status)),
    ]
    rows.extend(head)
    # A track only while something may still be working. See `_track` for why a terminal panel
    # carries none at all.
    if not terminal and status != STATUS_IDLE:
        rows.append(_track(state))
    if detail:
        rows.append('<div class="bs-render-detail">%s</div>' % _html.escape(_one_line(detail)))
    rows.append(_stage_pills(state))
    foot = []
    if state.completed_stages:
        foot.append("%d / %d stages completed" % (state.completed_count, STAGE_COUNT))
    # The age of the last measurement, reported SEPARATELY from elapsed time and with no verdict
    # attached. A long gap during Stage 5 is normal -- a single source can take minutes -- so this
    # says how long it has been and deliberately does not say that anything is wrong.
    if state.update_ms is not None and not terminal and status not in (STATUS_IDLE, STATUS_QUEUED):
        foot.append('last update <span class="bs-cut-update-elapsed">%s</span> ago'
                    % _html.escape(format_elapsed(_ms(state.update_ms))))
    if foot:
        rows.append('<div class="bs-render-foot">%s</div>' % " · ".join(foot))
    rows.append('</div>')
    return "".join(rows)
