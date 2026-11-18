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
    percent = (done * 100.0) / total
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
