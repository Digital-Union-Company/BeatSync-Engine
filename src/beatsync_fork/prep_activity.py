"""In-flight activity text for the Prepare & Verify operations (UX-PREP-ACTIVITY).

[FORK] Digital-Union. Scan Library and Analyze already stream structured `ProgressEvent`s through
`ProgressView`, so they have both an immediate frame and real phases. **Verify Sources and Sample
Decode had neither**: each was a plain function returning one final string, wired with
`show_progress='hidden'`, so from the operator's side an accepted click and a dead button looked
identical for as long as the work took. That is the roadmap's complaint, and this module is the
vocabulary for answering it.

What this module is
-------------------
Pure policy and formatting: the phase labels, the pulse, and the rendered in-flight block. It is
stdlib-only and imports no upstream runtime (CLAUDE.md's hard rule for `beatsync_fork`), so every
rule below is testable on a bare interpreter.

What this module is deliberately NOT
------------------------------------
* **Not a coordinator, job manager or scheduler.** It starts nothing, owns no thread, no process,
  no queue and no lock, and it never decides whether an operation may run. `_RENDER_LOCK` remains
  the sole admission authority for Verify and Sample, exactly as `source-verification.md` states.
* **Not a terminal-state renderer.** The four terminal answers — the report, the failure text, the
  busy refusal and the superseded notice — are existing strings that callers return **verbatim**,
  because tests compare them by equality. This module renders *in-flight* frames only. Requirement
  5's "separate terminal states" is satisfied by those four strings already being distinct; adding
  decoration here would change them.
* **Not a second progress protocol.** It emits no `ProgressEvent`, defines no stage, no phase key
  for `ProgressView`, and parses nothing. Scan and Analyze keep `ProgressView` as their renderer;
  this module only supplies their activity *notice* line.

The honesty rules, which are the whole point
--------------------------------------------
* **Elapsed is MEASURED or absent.** `elapsed_ms` is a real reading taken by the caller from
  `time.perf_counter()`. There is no default, no estimate and no clock inside this module — a
  formatter that read its own clock could not be tested without sleeping, and would be measuring
  the wrong interval anyway.
* **Counts are shown only when the backend measured them.** `done`/`total` are optional and are
  rendered only when *both* are real integers with `total > 0`. Anything else renders the
  indeterminate form. A percentage is derived from measured counts or not shown at all.
* **No ETA, no rate, no throughput, ever.** Not even when counts are known. The same ban
  `progress-events.md` places on invented ETAs, restated here because this surface is where an
  operator would most want one and where the data to compute one honestly does not exist.
* **An indeterminate phase still has to look alive.** `pulse` advances a visible marker, so two
  consecutive frames differ even within the same whole second. That matters mechanically, not just
  cosmetically: a Gradio update whose value is byte-identical to the previous one is not delivered
  (the same hazard the R5 grid nonce exists for), so a frame that looked "the same" would simply
  not arrive and the panel would freeze while work continued.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Optional

from beatsync_fork.raw_cutter_activity import format_elapsed

__all__ = [
    "OUTCOME_COMPLETED", "OUTCOME_FAILED", "OUTCOME_REFUSED", "OUTCOME_SUPERSEDED",
    "OUTCOMES",
    "OPERATION_VERIFY", "OPERATION_SAMPLE", "OPERATION_SCAN", "OPERATION_ANALYZE",
    "PHASE_STARTING", "PHASE_WORKING", "PHASE_ENUMERATING", "PHASE_FINGERPRINTING",
    "PHASE_CACHE_RECORDS", "PHASE_MEDIA_HEADERS", "PHASE_STREAM_METADATA",
    "PHASE_SAMPLE_DECODE", "PHASE_REPORT",
    "PULSE_FRAMES", "ActivityState", "activity_line", "working_text", "pulse_marker",
    # UX-PREP-VISUAL-PROGRESS R1 -- the visual panel for the same observations.
    "STATUS_IDLE", "STATUS_RUNNING", "PANEL_STATUSES", "STATUS_LABELS",
    "measured_percent", "terminal_status",
    "activity_panel_html", "status_panel_html",
]

# --------------------------------------------------------------------------------------------
# Terminal vocabulary (requirement 5). These are LABELS for tests and callers to agree on, not
# rendered text -- each operation's four terminal strings already exist and are returned verbatim.
# --------------------------------------------------------------------------------------------

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_REFUSED = "refused"
OUTCOME_SUPERSEDED = "superseded"

#: All four, deliberately distinct. A caller that collapses two of these is losing information the
#: operator needs: "declined, nothing ran" and "ran and broke" lead somewhere different.
OUTCOMES = (OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_REFUSED, OUTCOME_SUPERSEDED)

# --------------------------------------------------------------------------------------------
# Operation names, in the operator's words rather than the handler's.
# --------------------------------------------------------------------------------------------

OPERATION_VERIFY = "Verify Sources"
OPERATION_SAMPLE = "Sample Decode"
OPERATION_SCAN = "Scan Library"
OPERATION_ANALYZE = "Analyze"

# --------------------------------------------------------------------------------------------
# Phase labels. Plain language, present tense, no jargon and no internal function names -- an
# operator reading "Checking saved analysis records" knows what is happening; "classify_library_
# sources" tells them only that a programmer was here.
#
# Each label must correspond to work the handler can actually observe itself starting. A label for
# a phase nobody reports would be a fabricated state, which is the failure this whole item exists
# to remove.
# --------------------------------------------------------------------------------------------

PHASE_STARTING = "Starting"
#: Scan and Analyze stream real `ProgressEvent`s, so the detailed phase and any measured counts are
#: already in their report box through `ProgressView`. Their activity notice therefore carries this
#: generic label plus the pulse and the measured clock -- its job is to prove the operation is alive
#: during a stretch that emits no event, not to restate what `ProgressView` is already saying.
PHASE_WORKING = "Working"
PHASE_ENUMERATING = "Listing files in the folder"
PHASE_FINGERPRINTING = "Recording file sizes and timestamps"
PHASE_CACHE_RECORDS = "Checking saved analysis records"
PHASE_MEDIA_HEADERS = "Reading media headers"
PHASE_STREAM_METADATA = "Reading stream metadata"
PHASE_SAMPLE_DECODE = "Decoding a short sample"
PHASE_REPORT = "Writing the report"

#: The visible pulse. Four frames so a one-second cadence reads as motion rather than a glitch,
#: and deliberately plain ASCII: this lands in a `gr.Textbox`, which renders no markup, and a
#: spinner built from box-drawing characters would depend on the operator's font.
PULSE_FRAMES = (".  ", ".. ", "...", " ..")


def pulse_marker(pulse: int) -> str:
    """One pulse frame. Total for any integer, including a negative one."""
    try:
        index = int(pulse)
    except (TypeError, ValueError):
        index = 0
    return PULSE_FRAMES[index % len(PULSE_FRAMES)]


def _measured_counts(done: Optional[int], total: Optional[int]):
    """``(done, total)`` when the backend really measured both, else ``None``.

    Strict on purpose, and `bool` is rejected first because it subclasses `int` -- the same
    discipline `library_prep.normalize_batch_size` and `variation.normalize_seed` use. A count this
    function accepts becomes a determinate progress figure on screen, so "nearly a number" must
    fail closed into the indeterminate form rather than render `True / 1`.
    """
    for value in (done, total):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    if total <= 0 or done < 0 or done > total:
        return None
    return (done, total)


@dataclass(frozen=True)
class ActivityState:
    """One in-flight observation. Every field is something the caller actually measured."""

    operation: str
    phase: str
    elapsed_ms: float
    done: Optional[int] = None
    total: Optional[int] = None
    pulse: int = 0

    @property
    def measured(self):
        """The determinate counts, or ``None`` for an indeterminate phase."""
        return _measured_counts(self.done, self.total)


def measured_percent(state: ActivityState) -> Optional[float]:
    """Completion percentage derived from the two measured integers, or ``None``.

    [FORK] Digital-Union (UX-PREP-VISUAL-PROGRESS R1): extracted so the one-line form and the
    visual panel cannot drift into two different arithmetics for the same pair of integers. There
    is deliberately no other way to obtain a percentage from this module.
    """
    counts = state.measured
    if counts is None:
        return None
    done, total = counts
    return (done * 100.0) / total


def activity_line(state: ActivityState) -> str:
    """The one-line summary: operation, phase, measured counts if any, measured elapsed.

    Determinate and indeterminate differ by what is *present*, never by a fabricated stand-in. The
    percentage is computed from the two measured integers and from nothing else; with no counts
    there is no percentage, no bar and no estimate -- just the pulse and the clock, both of which
    are honest.
    """
    phase = str(state.phase or PHASE_STARTING)
    counts = state.measured
    elapsed = format_elapsed(max(0.0, float(state.elapsed_ms or 0.0)))
    if counts is None:
        return f"{state.operation} — {phase}{pulse_marker(state.pulse)}  ({elapsed} elapsed)"
    done, total = counts
    percent = measured_percent(state)
    return (f"{state.operation} — {phase}  {done} / {total} ({percent:.1f}%)"
            f"  ({elapsed} elapsed)")


#: The reassurance line. It states what is true of every operation this module serves: all four are
#: read-only with respect to records and media, so an operator watching a long phase knows that
#: abandoning it costs them nothing. Verify and Sample write nothing at all; Scan creates the cache
#: directory and no record, and Analyze's writes go through the existing checkpoint path.
WORKING_FOOTER = "Working — nothing has been changed on disk yet."

#: Scan and Analyze stream real `ProgressEvent`s, so their notice carries the activity line only;
#: this footer belongs to the two operations whose report box is otherwise empty while they run.
WORKING_HINT = "This box will show the full report when the operation finishes."


def working_text(state: ActivityState, *, footer: bool = True, hint: bool = True) -> str:
    """The in-flight block for a report textbox.

    `footer` and `hint` are separable because the two consumers differ: Verify and Sample own their
    whole box and show both, while Scan and Analyze put this line into their small status notice
    beside a report box that `ProgressView` is already filling.
    """
    parts = [activity_line(state)]
    if footer:
        parts.append(WORKING_FOOTER)
    if hint:
        parts.append(WORKING_HINT)
    return "\n".join(parts)


# =============================================================================================
# UX-PREP-VISUAL-PROGRESS R1 -- the same observations, as a visible panel
#
# [FORK] Digital-Union. The honesty rules above were already right; what was wrong was that the
# only visible evidence of a running Scan was a small text line BELOW a ten-line report box, and
# the first thing that line's report box said about a 6,363-source library was
#
#     Stage 0 - Input  (running)
#       0 / 6363 (0.0%)
#
# -- which is a true reading, taken from a real `ProgressEvent`, and which then stayed exactly
# that for several minutes. The measured cause is `classify_library_sources`: it emits its
# `start` event with `current=0, total=len(sources)` and then runs `_compute_cache_paths_parallel`
# over the whole library, which emits NOTHING until the per-source verdict loop begins. So a
# determinate bar sitting at 0.0% is simultaneously honest about the count and silent about the
# work, and an operator reads the pair as a dead button.
#
# The answer is therefore NOT to invent a number for that stretch. It is to separate two
# questions the old line conflated:
#
#     "is this running?"      -> answered by a state chip and a CSS animation, which are true for
#                                as long as the worker is alive and cost no measurement at all
#     "how far has it got?"   -> answered ONLY from measured counts, and left ABSENT otherwise
#
# A determinate fill and the running animation are not alternatives here: a 0%-filled bar still
# carries the sweep, because the count being zero is not evidence that nothing is happening.
#
# What this section is deliberately NOT
# -------------------------------------
# * **Not a second progress protocol.** It emits no `ProgressEvent`, defines no stage and no
#   phase key, and parses nothing out of rendered text. Every value it prints was handed to it.
# * **Not a clock.** Like the rest of this module it reads no time source; `elapsed_ms` is the
#   caller's `time.perf_counter()` reading or it is absent.
# * **Not a browser-side timer.** There is no script here and none is implied. The animation is a
#   CSS keyframe declared once in the application stylesheet, which is why it keeps moving
#   between two server frames without anything polling anything.
# =============================================================================================

#: No operation has run yet, or an input change invalidated the last one. Neutral: an idle panel
#: must not read as a success and must not read as a fault.
STATUS_IDLE = "idle"
#: A worker is alive right now. The ONE status that animates.
STATUS_RUNNING = "running"

#: Every status the panel can render. The four terminal ones are `OUTCOMES` -- reused rather than
#: redefined, because a second four-label vocabulary for the same four answers is exactly the
#: duplication this module's docstring forbids. Here they are machine-readable state (a `data-`
#: attribute and a class suffix); the words an operator reads come from `STATUS_LABELS`.
PANEL_STATUSES = (STATUS_IDLE, STATUS_RUNNING) + OUTCOMES

#: The chip text. Past tense for every terminal state and present participle for the running one,
#: so the chip alone distinguishes "it is happening" from "it happened".
STATUS_LABELS = {
    STATUS_IDLE: "Idle",
    STATUS_RUNNING: "Running",
    OUTCOME_COMPLETED: "Completed",
    OUTCOME_FAILED: "Failed",
    OUTCOME_REFUSED: "Declined",
    OUTCOME_SUPERSEDED: "Superseded",
}

#: The same four tones the Raw Cutter panel already uses, so the two activity surfaces in this
#: application do not teach an operator two different colour languages. Colour is never the only
#: signal: the chip text, the `data-bs-prep-status` attribute and the panel's own `aria-label` all
#: name the state in words.
_STATUS_TONES = {
    STATUS_IDLE: "#8b949e",             # grey  -- nothing to report, which is not a fault
    STATUS_RUNNING: "#2f81f7",          # blue  -- work is genuinely running
    OUTCOME_COMPLETED: "#2ea043",       # green -- a terminal success
    OUTCOME_FAILED: "#f85149",          # red   -- a terminal failure
    OUTCOME_REFUSED: "#d29922",         # amber -- declined; nothing ran, which is not a failure
    OUTCOME_SUPERSEDED: "#d29922",      # amber -- an answer withheld, for the same reason
}

# There is deliberately NO default reassurance footer for this panel, and the absence is the
# point rather than an omission.
#
# [FORK] Digital-Union (UX-PREP-VISUAL-PROGRESS R1 remediation, PR #111 finding F1). R1 shipped
# `PANEL_READONLY_FOOTER = "Nothing has been changed on disk yet."` as the default, on the stated
# belief that it was "true of all four operations". **It is false for Analyze**, and provably so:
# `video_analysis.analyze_video_sources` calls `_checkpoint_cache` -> `_save_cache` -> `json.dump`
# inside its `as_completed` loop, checkpointing each source's record the moment that source
# completes. So from the first finished source onward, the running Analyze panel was telling the
# operator that nothing had been written while records were landing on disk behind it. The
# cancellation contract says so in as many words: *"Records checkpointed before the Stop stay on
# disk."*
#
# That is the worst class of defect this module exists to prevent -- a confident claim the backend
# contradicts -- and it is exactly the mistake the honesty rules above forbid about *numbers*,
# made about a sentence instead.
#
# The fix is removal, not rewording per operation. A footer whose truth depends on which operation
# is running is a conditional claim that the next operation to gain a write path silently
# falsifies, and `activity_panel_html` cannot see which backend it is describing. `footer` is now
# an explicit, defaulted-empty argument: a caller that wants a reassurance must supply it and owns
# its truth. Scan and Analyze supply none.
#
# `WORKING_FOOTER` above is untouched and stays correct, because its only consumers are Verify and
# Sample, which genuinely write nothing at all.


def terminal_status(*, failed: bool = False, refused: bool = False) -> str:
    """Map two observed facts onto one terminal status. Refusal wins, then failure.

    Deliberately takes booleans the caller measured rather than a report string to classify: a
    renderer that decided "did this fail" by looking for words in prose would be the prose-parsing
    `progress-events.md` deleted, moved to the last step of the pipeline instead of the first.

    Refusal is checked first because a declined operation **did not run**, so it cannot have
    failed; reporting it as a failure would send an operator looking for a fault in their library.
    """
    if refused:
        return OUTCOME_REFUSED
    if failed:
        return OUTCOME_FAILED
    return OUTCOME_COMPLETED


def _tone(status: str) -> str:
    return _STATUS_TONES.get(status, _STATUS_TONES[STATUS_IDLE])


def _one_line(text) -> str:
    """Collapse every run of whitespace to one space, so an interpolated string is one line.

    Escaping is not sufficient on its own here, and that is a measured point rather than a
    precaution: `html.escape` leaves a newline alone, this fragment lands in a **Markdown**
    component, and a blank line inside a raw HTML block is exactly how a Markdown renderer decides
    the block ended -- so one multi-line string would split the panel in half and show the rest as
    visible angle brackets. Every value reaching this markup can contain one: an FFmpeg error, a
    Windows path, an operator-typed folder.

    This is layout, not rewording. No word, number, order or character other than whitespace
    changes, and the notice slot it serves is a *one-line* status by its own contract
    (:attr:`library_prep.PrepSessionState.notice`); the full multi-line report has its own box.
    """
    return " ".join(str(text or "").split())


def _safe_status(status) -> str:
    """A status this module will render, failing closed to :data:`STATUS_IDLE`.

    Unknown input becomes *idle*, never running and never completed. Both of those would be a
    claim: "still working" keeps an animation alive over a dead worker, and "completed" is the one
    answer a caller must never receive by accident.
    """
    text = str(status or "")
    return text if text in PANEL_STATUSES else STATUS_IDLE


def _chip(status: str) -> str:
    """The state chip: a tone-coloured dot, then the state in words."""
    return (f'<span class="bs-prep-chip" style="color:{_tone(status)};">'
            f'<span class="bs-prep-dot" style="background:{_tone(status)};"></span>'
            f'{_html.escape(STATUS_LABELS.get(status, status.title()))}</span>')


def _track(state: ActivityState) -> str:
    """The bar, rendered ONLY while something is running.

    Determinate when -- and only when -- `state.measured` accepted two real integers; the fill is
    then exactly that fraction and `aria-valuenow` carries it. Otherwise the fill spans the track
    and carries no value at all: ARIA's own rule for an indeterminate progressbar is to OMIT
    `aria-valuenow`, which is a far better answer than a zero, because a screen reader announces a
    zero as a measurement.

    **The sweep is a sibling of the fill and spans the whole track, not the filled part.** That is
    requirement 4 as code, and it is the second attempt: with the sweep ON the fill, a determinate
    `0 / 6363` renders a zero-width element, so the animation that is supposed to say "alive" had
    nowhere to travel and the panel looked as dead as the line it replaced -- at exactly the count
    the owner reported. A measured zero is a true count and not evidence of idleness, so the
    motion is attached to the thing whose width is not a measurement.

    **An indeterminate track carries NO fill element at all** (PR #111 finding F2). R1 rendered
    `width:100%` there, which paints the same solid bar a genuinely complete `N / N` paints -- so
    a phase nobody had counted looked finished, which is the determinate-looking-indeterminate
    inverse of the defect this whole item exists to remove. Emitting no fill is strictly better
    than dimming one: there is then no element whose width could be read as a quantity, and the
    sweep moving across an empty track is unambiguous. The track, the sweep and the running chip
    are untouched, so the state is still obvious.
    """
    percent = measured_percent(state)
    sweep = '<div class="bs-prep-sweep"></div>'
    if percent is None:
        return ('<div class="bs-prep-track bs-prep-indeterminate" role="progressbar" '
                'aria-valuemin="0" aria-valuemax="100">'
                f'{sweep}</div>')
    bounded = max(0.0, min(100.0, percent))
    return ('<div class="bs-prep-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" '
            f'aria-valuenow="{bounded:.1f}">'
            f'<div class="bs-prep-fill" style="width:{bounded:.1f}%;"></div>{sweep}</div>')


def _panel(*, status: str, operation: str, elapsed_ms: Optional[float], detail: str,
           footer: str, summary: str, track: str, pulse: int,
           done: Optional[int], total: Optional[int], percent: Optional[float]) -> str:
    """The shared markup. One line of HTML, every interpolated value escaped.

    One line because this lands in a Markdown component: a blank line inside a raw HTML block is
    how a Markdown renderer decides the block ended, and a fragment split in half there renders as
    visible angle brackets.

    `data-bs-prep-*` is the stable contract the tests and the browser read, so no assertion has to
    parse presentation. `data-bs-prep-pulse` is also mechanical rather than decorative: a Gradio
    update whose value is byte-identical to the previous one is **not delivered** (the hazard the
    R5 grid nonce exists for), so without a guaranteed per-frame difference the panel would freeze
    while work continued -- and an HTML panel cannot carry the text pulse the one-line form uses.
    """
    tone = _tone(status)
    operation = _one_line(operation)
    detail = _one_line(detail)
    footer = _one_line(footer)
    # The summary is the caller's own one-line form of the same observation, emitted as the
    # panel's `aria-label`. Absent when the caller has no one-line form -- an empty `aria-label`
    # is worse than none at all.
    #
    # MEASURED CAVEAT, so nobody reads this as a live accessibility feature: the consumer renders
    # through a `gr.Markdown`, and gradio 6.19.0's sanitizer strips every `aria-*`, `role` and
    # `data-*` attribute before the browser sees it. `class` and `style` survive and nothing else
    # does. These attributes are still emitted because they are correct, cost nothing, and become
    # live the moment a consumer renders this through `gr.HTML` -- but what a screen reader
    # ACTUALLY gets today is the chip text and the detail line, which is why the state is always
    # written in words and the measurement is always written as text.
    summary = _one_line(summary)
    label = (f'aria-label="{_html.escape(summary, quote=True)}" ' if summary else '')
    rows = [
        f'<div {label}class="bs-prep-panel bs-prep-{_html.escape(status, quote=True)}" '
        f'data-bs-prep-status="{_html.escape(status, quote=True)}" '
        f'data-bs-prep-running="{"1" if status == STATUS_RUNNING else "0"}" '
        f'data-bs-prep-operation="{_html.escape(operation, quote=True)}" '
        f'data-bs-prep-elapsed-ms="{"" if elapsed_ms is None else int(max(0.0, elapsed_ms))}" '
        f'data-bs-prep-determinate="{"1" if percent is not None else "0"}" '
        f'data-bs-prep-done="{"" if done is None else int(done)}" '
        f'data-bs-prep-total="{"" if total is None else int(total)}" '
        f'data-bs-prep-percent="{"" if percent is None else format(percent, ".1f")}" '
        f'data-bs-prep-pulse="{int(pulse)}" '
        f'style="border-left:3px solid {tone};">',
        '<div class="bs-prep-head">',
        _chip(status),
    ]
    if operation:
        rows.append(f'<span class="bs-prep-operation">{_html.escape(operation)}</span>')
    if elapsed_ms is not None:
        rows.append('<span class="bs-prep-clock">'
                    f'{_html.escape(format_elapsed(max(0.0, float(elapsed_ms))))}</span>')
    rows.append('</div>')
    rows.append(track)
    if detail:
        rows.append(f'<div class="bs-prep-detail">{_html.escape(detail)}</div>')
    if footer:
        rows.append(f'<div class="bs-prep-foot">{_html.escape(footer)}</div>')
    rows.append('</div>')
    return "".join(rows)


def activity_panel_html(state: ActivityState, *, summary: str = "",
                        footer: str = "") -> str:
    """The RUNNING panel for one in-flight observation.

    `state` is the same record `activity_line` renders, so the panel can say nothing the one-line
    form could not. `summary` is that one-line form, passed in rather than recomputed here: it
    becomes the panel's `aria-label` and the call site then visibly shows that there is exactly one
    renderer for "what is this operation doing right now". See `_panel` for the measured caveat
    about which attributes survive the consumer's Markdown sanitizer.

    The detail line is the phase the operation itself published plus the counts something measured,
    and nothing else -- no estimate, no rate, no throughput and no remaining time, for any of which
    the data does not exist.

    **`footer` defaults to empty and this renderer never supplies one.** It cannot see which
    backend it is describing, so it cannot know whether a reassurance about disk writes is true;
    a caller that wants one must supply it and owns its truth. See the note above
    :func:`terminal_status` for the Analyze checkpoint that made the previous default false.
    """
    counts = state.measured
    percent = measured_percent(state)
    detail = str(state.phase or PHASE_STARTING)
    if counts is not None:
        done, total = counts
        detail = f"{detail} · {done} / {total} ({percent:.1f}%)"
    return _panel(
        status=STATUS_RUNNING,
        operation=str(state.operation or ""),
        elapsed_ms=float(state.elapsed_ms or 0.0),
        detail=detail,
        footer=footer,
        summary=summary,
        track=_track(state),
        pulse=int(state.pulse or 0),
        done=None if counts is None else counts[0],
        total=None if counts is None else counts[1],
        percent=percent,
    )


def status_panel_html(*, status: str, operation: str = "", message: str = "",
                      elapsed_ms: Optional[float] = None) -> str:
    """The terminal or idle panel. No bar, no animation, and no fabricated completion.

    `message` is the operation's own notice, rendered **verbatim** (escaped, never reworded): the
    four terminal strings are compared by equality in accepted suites and "declined, nothing ran"
    leads an operator somewhere different from "ran and broke", so this decorates none of them.

    **No track is rendered here at all.** Painting a full bar on completion would be inventing a
    measurement -- a finished Scan knows how many sources it classified, not that some progress
    figure reached 100 -- and painting one on a failure would claim the work got somewhere.

    An idle panel with nothing to say renders as the empty string rather than as an empty card,
    which is what keeps the preparation section from carrying a permanent blank box.
    """
    resolved = _safe_status(status)
    if resolved == STATUS_IDLE and not str(message or "").strip():
        return ""
    return _panel(
        status=resolved,
        operation=str(operation or ""),
        elapsed_ms=None if elapsed_ms is None else float(elapsed_ms),
        detail=str(message or ""),
        footer="",
        summary="",
        track="",
        pulse=0,
        done=None,
        total=None,
        percent=None,
    )
