#!/usr/bin/env python3
"""[FORK] Digital-Union: render 2–4 compared candidates (C3-R0 → C3-R1B-b).

C3 V1 let the user generate N candidate settings, compare them and apply **one**. The payoff of a
comparison, though, is watching the videos — and getting two meant two manual round-trips, with the
batch consumed by the first Apply and the base moved out from under the rest. C3-R0 closed that
loop; C3-R1B-b finished it::

    generate N  ->  compare N  ->  tick 2 to 4  ->  Render Selected Variants
                                                    candidate A, then B, then C, then D

**Sequential, cancellable, and it continues past a candidate-local failure.** The three milestones
are worth keeping distinct, because each one bought the next:

* **C3-R0** shipped with no cancellation at all — a cancelled Gradio event could return its slot
  while the daemon render worker was still alive, and the next render would wipe the live one's
  *process-global* processing directory. Rather than ship a Stop button that could not stop FFmpeg,
  it shipped none and bounded the commitment to exactly two renders.
* **C3-R1A** added the explicit, shared ``beatsync_fork.render_worker.RenderLifecycle`` spanning the
  whole batch, so a Cancel click is observed at the next safe boundary instead of tearing down a
  live worker — BOUNDARY_ONLY_CANCEL: an FFmpeg-class subprocess stops within moments, an
  in-progress Stage-5 call is never hard-killed.
* **C3-R1B-a** made every failure cause truthfully typed, and deliberately spent none of it on
  continuing.
* **C3-R1B-b** (this contract) raises the bound to a *range* and consumes that classification:
  ``CANDIDATE_LOCAL`` is recorded and the batch carries on to the next selected candidate;
  ``SHARED_FATAL``, ``UNKNOWN_FATAL`` and ``CANCELLED`` still stop it. There is no
  continue-after-cancellation and no retry of a candidate.

One lifecycle still spans **all** selected candidates, however many, and earlier durable outputs are
never rolled back.

===============================================================================
This module decides; it never renders
===============================================================================

Stdlib-only (CLAUDE.md's hard rule) and deliberately ignorant of everything that runs: no Gradio, no
``video_processor``, no ``auto_mode``, no ``ffmpeg_processing``, no ``paths``, no filesystem, no
clock, no randomness. It validates a selection, derives candidate output identity, and formats the
summary. `gui.py` supplies the request tag and performs every side effect.

``variant_batch`` stays what C3 V1 made it — candidate *generation and comparison* state — and this
module is where render intent lives. The dependency is one-way::

    render_batch  ->  variant_batch / recipe data      (allowed)
    variant_batch ->  render_batch                     (NEVER)

`variant_batch.py`'s own guard still forbids every render concept inside it, at full strength, and
that guard is what keeps this boundary honest rather than aspirational.

===============================================================================
Candidate identity cannot rest on the Variation Seed
===============================================================================

C3 guarantees candidate **master** seeds are unique within a batch — ``candidate_master_seeds``
deduplicates them deliberately. It guarantees nothing about ``CreativeRecipe.seed``, which is an
independent draw per master, and collisions are real rather than theoretical: searching 28,235
roots × 12 candidates found root 5484, where masters 945730 and 862920 **both** resolve Variation
Seed 536635.

That matters because the existing render path names its output
``<stem>_<timestamp>_seed<VariationSeed><ext>``. Two such candidates rendered in the same second
would compute an identical destination. So the stem derived here carries the **candidate index and
the candidate master**, plus a per-invocation request tag::

    music_video_batch20261003_161234_123456_c01_m609591

The existing suffix is left entirely alone; this only makes the *base* distinct.

**What H1 changed about that motivation, and what it did not.** When C3-R0 wrote this, the render
path promoted with ``shutil.move``, which silently replaced an existing destination (measured), so
a stem collision meant one candidate *destroying* the other's video. H1 replaced that promotion
with an atomic no-replace ``os.rename``, so destructive replacement is no longer possible from any
GUI render. Distinct candidate stems are nevertheless still **required**, for the other half of the
reason: two candidates landing on one name would now make the second one legitimately *refuse*, and
a batch that was asked for four videos would return three. Identity here is what lets every
requested candidate succeed; the promotion is what guarantees none can be overwritten. Do not drop
one because the other exists.

That matters more under C3-R1B-b, not less: a promotion ``FileExistsError`` is classified
``CANDIDATE_LOCAL``, so the batch now **continues** past it. Distinct stems are what keep that a
genuine per-candidate fact rather than a collision the batch would hit again and again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# [FORK] Digital-Union (C3-R1A): the pure cancellation/outcome contract (stdlib-only fork module).
from beatsync_fork.render_worker import RenderOutcomeKind

# ---------------------------------------------------------------------------
# Selection contract
# ---------------------------------------------------------------------------

#: C3-R1B-b: the selection is a bounded **range**, not an exact count. C3-R0 shipped exactly two
#: because it had no stop channel and no way to tell a shared failure from a candidate-local one;
#: C3-R1A built the first and C3-R1B-a the second, so the bound is now purely *how much wall-clock
#: one click may commit*.
#:
#: **Two is still the floor**: one candidate is not a comparison, and ordinary Create Music Video
#: already covers that case.
#:
#: **Four is the ceiling, and it is a product contract rather than a guess.** Past candidate 1 the
#: cost is linear with no economy of scale -- the only real shared saving, the ~15.7 s of Stage 1-3,
#: is already fully banked at candidate 2 by the L2 process cache, so every later candidate costs
#: the same again. Derived in C3-R1B/P0 from measured per-candidate cost (≈67 s for the first,
#: ≈51 s for each subsequent one on the NVENC path at ~150 clips; materially more on the serial
#: ProRes path), which puts four candidates in the same order as the single render a user already
#: accepts rather than in a new category. Do **not** raise it, make it configurable, add an
#: "advanced" override or derive it from machine speed.
#:
#: Deliberately unrelated to ``variant_batch.CANDIDATE_COUNT_MAX`` (12). That bound is comparison
#: legibility and costs about a millisecond; this one is render minutes. Do not let the two numbers
#: learn about each other.
RENDER_SELECTION_MIN = 2
RENDER_SELECTION_MAX = 4


def normalize_selection(selection: Any, candidate_count: int) -> tuple:
    """The chosen candidate indices in **canonical ascending order**, or ``()`` if unusable.

    Ascending original index rather than tick order, so the render sequence is a property of the
    batch and not of how the user happened to click. Re-ticking the same candidates in a different
    order renders the same videos in the same order.

    Total and never raising — a ``CheckboxGroup`` may hand over anything. ``bool`` is rejected
    first because it subclasses ``int`` and ``True`` would otherwise read as index 1. Duplicates,
    fractional values, strings, out-of-range indices and any count outside
    :data:`RENDER_SELECTION_MIN`..:data:`RENDER_SELECTION_MAX` all answer ``()``, which the caller
    reports as a refusal rather than guessing at an intent.

    [FORK] Digital-Union (C3-R1B-b): the cardinality rule is the **only** thing that changed here —
    ``len(chosen) == 2`` became a range test. In particular an over-long selection is still
    **refused**, never truncated to the first four: silently dropping a candidate the user ticked
    would render something they did not ask for.
    """
    if isinstance(selection, (str, bytes)) or not isinstance(selection, Sequence):
        return ()
    chosen = []
    for item in selection:
        if isinstance(item, bool) or not isinstance(item, int):
            return ()
        if not 0 <= item < candidate_count:
            return ()
        chosen.append(item)
    if len(set(chosen)) != len(chosen):
        return ()
    if not RENDER_SELECTION_MIN <= len(chosen) <= RENDER_SELECTION_MAX:
        return ()
    return tuple(sorted(chosen))


def describe_selection_refusal(selection: Any, candidate_count: int) -> str:
    """Why a selection was refused, in the user's terms. Diagnostics only."""
    if not candidate_count:
        return "No candidates to render. Press Generate Variants first."
    count = len(selection) if isinstance(selection, Sequence) and not isinstance(
        selection, (str, bytes)) else 0
    if not RENDER_SELECTION_MIN <= count <= RENDER_SELECTION_MAX:
        return (f"Select {RENDER_SELECTION_MIN} to {RENDER_SELECTION_MAX} candidates to render — "
                f"{count} selected.")
    return (f"That selection could not be read. Re-tick {RENDER_SELECTION_MIN} to "
            f"{RENDER_SELECTION_MAX} candidates and try again.")


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def candidate_output_stem(user_base: str, request_tag: str, candidate_index: int,
                          candidate_master_seed: int) -> str:
    """The per-candidate output base name handed to the existing render path.

    Carries the request tag, the candidate index and the candidate **master** — never the Variation
    Seed, whose uniqueness C3 does not guarantee (see the module docstring). The existing path then
    appends its own ``_<timestamp>_seed<VariationSeed><ext>`` unchanged, so single-render naming is
    untouched and a batch simply starts from a distinct base.
    """
    base = (user_base or "music_video").strip() or "music_video"
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return f"{base}_batch{request_tag}_c{candidate_index + 1:02d}_m{candidate_master_seed}"


@dataclass(frozen=True)
class RenderCandidateRequest:
    """One candidate's resolved execution values plus its output identity. Deepcopy-safe.

    Carries the two **recipes** and nothing that runs: no paths, no voice files, no renderer state,
    no Gradio object. The recipes are already-validated frozen artifacts from C3 — this module
    re-resolves nothing and the candidate master is provenance only, never an execution value.
    """

    candidate_index: int
    candidate_master_seed: int
    creative_recipe: Any
    audio_recipe: Any
    output_stem: str

    def variation_seed(self) -> int:
        """The value the render actually uses as the Variation Seed."""
        return self.creative_recipe.seed

    def label(self) -> str:
        return f"Candidate {self.candidate_index + 1} · master {self.candidate_master_seed}"


@dataclass(frozen=True)
class RenderBatchRequest:
    """Execution authority for **one** Render Selected click, and only that click.

    Ordinary Create Music Video keeps the live visible widgets as its authority. A batch cannot:
    two or more candidates are never simultaneously on screen. So the candidate-specific half is frozen
    here at submission, and `gui.py` freezes the non-candidate half (audio, voice, SFX, source,
    output, encoder, FPS) from the same submitted event arguments.

    This is **not** the hidden mutable execution state C2 rejected: it is built explicitly from
    submitted values, lives only for this invocation, is never cached for a later render, and
    reaches no Stage-5 identity.
    """

    request_tag: str
    batch_root_master: int
    candidates: tuple

    def __post_init__(self) -> None:
        if not RENDER_SELECTION_MIN <= len(self.candidates) <= RENDER_SELECTION_MAX:
            raise ValueError(
                f"a render batch is {RENDER_SELECTION_MIN} to {RENDER_SELECTION_MAX} candidates, "
                f"got {len(self.candidates)}")
        indices = [c.candidate_index for c in self.candidates]
        if indices != sorted(indices) or len(set(indices)) != len(indices):
            raise ValueError("candidates must be distinct and in ascending index order")
        stems = [c.output_stem for c in self.candidates]
        if len(set(stems)) != len(stems):
            raise ValueError("candidate output stems must be distinct")

    @property
    def count(self) -> int:
        return len(self.candidates)


def build_request(batch: Any, selection: Any, user_base: str, request_tag: str):
    """``(RenderBatchRequest, "")`` or ``(None, refusal)``. The one construction path.

    `batch` is the live C3 :class:`variant_batch.VariantBatch`; its candidates are copied by
    reference into the request exactly as stored. Nothing is re-resolved, so what renders is
    precisely what the comparison table showed.
    """
    candidates = getattr(batch, "candidates", ()) or ()
    indices = normalize_selection(selection, len(candidates))
    if not indices:
        return None, describe_selection_refusal(selection, len(candidates))

    declaration = getattr(batch, "declaration", None)
    root = getattr(declaration, "root_master_seed", 0)
    chosen = tuple(
        RenderCandidateRequest(
            candidate_index=candidates[i].index,
            candidate_master_seed=candidates[i].master_seed,
            creative_recipe=candidates[i].creative_recipe,
            audio_recipe=candidates[i].audio_recipe,
            output_stem=candidate_output_stem(
                user_base, request_tag, candidates[i].index, candidates[i].master_seed),
        )
        for i in indices
    )
    return RenderBatchRequest(request_tag=request_tag, batch_root_master=root,
                              candidates=chosen), ""


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderCandidateOutcome:
    """What one candidate actually produced. Deepcopy-safe; strings, ints and one enum only.

    **`durable_output_path` is the success authority**, not the preview and not the status prose.
    That distinction is load-bearing for ProRes, where the durable artifact is the ``.mov`` moved
    into `output/` while the path handed back for display is a session-temp ``_preview.mp4``: a
    preview step that fails afterwards must not retroactively turn a finished render into a
    failure.

    [FORK] Digital-Union (C3-R1A): ``outcome_kind`` carries the typed cause. ``success`` is kept for
    compatibility and must always agree with it (``SUCCESS`` iff ``success`` is ``True``) --
    enforced in ``__post_init__`` rather than left to drift. A caller that omits ``outcome_kind``
    (every call site that predates this field) gets a conservative derivation: ``SUCCESS`` when
    ``success`` is ``True``, ``UNKNOWN_FATAL`` otherwise -- never a more specific class, because an
    omitted outcome_kind has proven nothing about which specific cause applied.
    """

    candidate_index: int
    candidate_master_seed: int
    variation_seed: int
    success: bool
    durable_output_path: str = ""
    preview_path: str = ""
    status_text: str = ""
    audio_layers_report: str = ""
    smart_mix_report: str = ""
    outcome_kind: "RenderOutcomeKind | None" = None

    def __post_init__(self) -> None:
        if self.outcome_kind is None:
            object.__setattr__(
                self, "outcome_kind",
                RenderOutcomeKind.SUCCESS if self.success else RenderOutcomeKind.UNKNOWN_FATAL)
            return
        agrees = (self.outcome_kind is RenderOutcomeKind.SUCCESS) == bool(self.success)
        if not agrees:
            raise ValueError(
                f"success={self.success!r} disagrees with outcome_kind={self.outcome_kind!r}")

    @property
    def cancelled(self) -> bool:
        return self.outcome_kind is RenderOutcomeKind.CANCELLED

    def report_lines(self) -> list:
        if self.cancelled:
            label = "CANCELLED"
        elif self.success:
            label = "SUCCESS"
        else:
            label = "FAILED"
        head = (f"Candidate {self.candidate_index + 1} · master {self.candidate_master_seed} · "
                f"Variation Seed {self.variation_seed} · {label}")
        lines = [head]
        if self.success and self.durable_output_path:
            lines.append(f"    output: {self.durable_output_path}")
        if not self.success and self.status_text:
            lines.append(f"    reason: {_one_line(self.status_text)}")
        elif self.success and self.status_text and not self.durable_output_path:
            lines.append(f"    note:   {_one_line(self.status_text)}")
        if self.audio_layers_report:
            lines.append(f"    Audio Layers: {_one_line(self.audio_layers_report)}")
        if self.smart_mix_report:
            lines.append(f"    Smart Mix:    {_one_line(self.smart_mix_report)}")
        return lines


def _one_line(text: str, limit: int = 200) -> str:
    """Collapse a multi-line diagnostic into one bounded line for the summary table."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


#: [FORK] Digital-Union (C3-R1B-b / R2): the **only** causes that can terminate a batch early.
#:
#: Deliberately a subset of ``RenderOutcomeKind``, because the two members left out are not batch
#: facts at all: ``SUCCESS`` is a candidate outcome (a finished batch reports counts, and has no
#: terminal cause — that is what ``None`` means), and ``CANDIDATE_LOCAL`` belongs to the candidate
#: that suffered it, which since R1B-b the batch *continues* past. See
#: :meth:`RenderBatchOutcome.__post_init__`, which rejects both.
_BATCH_TERMINAL_CAUSES = frozenset((
    RenderOutcomeKind.CANCELLED,
    RenderOutcomeKind.SHARED_FATAL,
    RenderOutcomeKind.UNKNOWN_FATAL,
))

#: [FORK] Digital-Union (C3-R1B-b / R3): the candidate classes that can be named as having **ended**
#: the batch — the two the R1B-b continuation policy actually stops on.
#:
#: This is :data:`_BATCH_TERMINAL_CAUSES` minus ``CANCELLED``, and the subtraction is the point: a
#: cancellation is a batch-level fact with its own wording ("cancelled during candidate N" /
#: "batch cancelled before candidate N"), never a candidate blamed for stopping the run.
#: ``CANDIDATE_LOCAL`` is absent because the policy **continues** past it — so it is a failure the
#: summary counts, never a stop cause, whatever arrives afterwards. See
#: :meth:`RenderBatchOutcome._terminal_candidate`.
_CANDIDATE_TERMINAL_CAUSES = frozenset((
    RenderOutcomeKind.SHARED_FATAL,
    RenderOutcomeKind.UNKNOWN_FATAL,
))


@dataclass(frozen=True)
class RenderBatchOutcome:
    """The whole batch's truthful record, and the one formatter for it.

    Prior success is never rolled back: **no earlier output is deleted**, whatever happens later.

    [FORK] Digital-Union (C3-R1B-b): this model no longer assumes that any failed candidate ended
    the batch. C3-R1B-a made the causes truthful; R1B-b spends that on continuing past exactly one
    of them. So "a candidate failed" and "the batch stopped" are now **independent facts**:

    ```
    candidate 1   CANDIDATE_LOCAL
    candidate 2   SUCCESS
    candidate 3   CANDIDATE_LOCAL
    candidate 4   SUCCESS
    -> a COMPLETED batch with two failures. Nothing stopped, nothing unattempted.
    ```

    Two consequences are load-bearing. `_failed_candidate()` was renamed to
    :meth:`_terminal_candidate` and now answers only the candidate that actually **ended** the batch
    — the last attempted one, and only when it was a genuine non-cancelled failure — because the
    *first* failure may well have been continued past. And R1A's ``stopped_on_failure`` boolean is
    gone: a name meaning "some failure occurred, therefore the batch stopped" cannot be true under
    R1B-b. Early termination is now read from the **typed** batch cause instead.

    [FORK] Digital-Union (C3-R1A / R2, extended by R1B-b): ``outcome_kind`` is the **batch-level
    terminal cause**, and it exists because the candidate-level one cannot express the batch
    boundary case. When a cancellation lands in the gap *between* two candidates:

    ```
    candidate 1   SUCCESS          (really did succeed; its output is on disk)
    candidate 2   NOT ATTEMPTED    (no outcome record exists, and none is fabricated)
    batch         CANCELLED        <- expressible nowhere else
    ```

    No candidate carries CANCELLED there, because no candidate was cancelled — the *batch* was. R1A
    reported that as ``1 / 2 succeeded; stopped on candidate 1``, which is false twice over:
    candidate 1 did not stop anything and nothing failed. A fake candidate-2 attempt labelled
    CANCELLED would be equally false, so the shape above is recorded literally instead.

    **A completed batch containing local failures has ``outcome_kind is None``.** It is deliberately
    *not* ``CANDIDATE_LOCAL``: that cause belongs to the candidate that suffered it, and the batch
    carried out its policy to the end. Only ``CANCELLED``, ``SHARED_FATAL`` and ``UNKNOWN_FATAL``
    are batch-terminal causes.
    """

    requested_count: int
    outcomes: tuple = ()
    outcome_kind: "RenderOutcomeKind | None" = None

    def __post_init__(self) -> None:
        """Validate the batch-level cause domain, then derive only what a candidate *proves*.

        [FORK] Digital-Union (C3-R1B-b / R2): the **domain check comes first**, because the field
        was documented as a batch-terminal cause while the type still accepted any
        ``RenderOutcomeKind``. Two members are not batch causes at all and are now rejected:

        * ``SUCCESS`` is a *candidate* outcome. A batch's success is its counts
          (``succeeded`` / ``failed`` / ``not_attempted``), not a terminal cause — a batch that
          finished has no cause to report, which is what ``None`` means here.
        * ``CANDIDATE_LOCAL`` belongs to the candidate that suffered it. Since C3-R1B-b the batch
          **continues** past one, so by definition it did not terminate the run. If the selection
          was exhausted the batch cause is ``None``; if something fatal stopped it later, the cause
          is that later terminal class.

        So the only legal non-``None`` values are the three that can actually end a batch early:
        ``CANCELLED``, ``SHARED_FATAL``, ``UNKNOWN_FATAL``.

        Exactly one derivation is then sound: a candidate that was itself cancelled proves the
        top-level render event was cancelled. Everything else stays ``None`` — meaning "not stated" —
        so every pre-R2 construction keeps byte-identical `headline()` / `summary_text()` output. In
        particular the batch-boundary case has **no** cancelled candidate to derive from, which is
        precisely why `gui.py` must state it explicitly; nothing here can rescue a caller that
        forgets to. The model deliberately does **not** derive ``SHARED_FATAL`` or ``UNKNOWN_FATAL``
        from candidate records: whether a fatal candidate actually ended the run depends on whether
        work remained, which is the orchestrator's knowledge, not the model's.
        """
        if self.outcome_kind is not None and self.outcome_kind not in _BATCH_TERMINAL_CAUSES:
            raise ValueError(
                f"{self.outcome_kind!r} is not a batch-level terminal cause; "
                f"expected None or one of "
                f"{sorted(k.name for k in _BATCH_TERMINAL_CAUSES)}")
        if self.outcome_kind is None and any(o.cancelled for o in self.outcomes):
            object.__setattr__(self, "outcome_kind", RenderOutcomeKind.CANCELLED)

    @property
    def cancelled(self) -> bool:
        """Whether the top-level render event was cancelled, candidate outcomes notwithstanding."""
        return self.outcome_kind is RenderOutcomeKind.CANCELLED

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def failed(self) -> int:
        """Attempted candidates that genuinely FAILED — the single source for the count.

        [FORK] Digital-Union (C3-R1B-b): excludes successes and excludes cancellations (a user's
        Stop is not their render breaking), and counts every attempted candidate that carries
        ``CANDIDATE_LOCAL``, ``SHARED_FATAL`` or ``UNKNOWN_FATAL``. Never derived from status prose,
        and never from ``requested_count`` — an unattempted candidate did not fail.
        """
        return sum(1 for o in self.outcomes if not o.success and not o.cancelled)

    @property
    def cancelled_count(self) -> int:
        """Attempted candidates cancelled mid-render. At most one, by construction."""
        return sum(1 for o in self.outcomes if o.cancelled)

    @property
    def attempted(self) -> int:
        return len(self.outcomes)

    @property
    def not_attempted(self) -> int:
        """Candidates the batch never started. Never negative, even on a malformed count."""
        return max(0, self.requested_count - self.attempted)

    @property
    def stopped_early(self) -> bool:
        """Whether a typed terminal cause ended the batch before its selection was exhausted.

        [FORK] Digital-Union (C3-R1B-b): read from the **typed** cause, never from "did anything
        fail" — which is exactly what R1A's `stopped_on_failure` boolean meant and what R1B-b makes
        untrue. A completed batch carrying two ``CANDIDATE_LOCAL`` failures is not stopped.
        """
        return self.outcome_kind is not None

    def _terminal_candidate(self):
        """The candidate that actually **ended** the batch early, or ``None``.

        [FORK] Digital-Union (C3-R1B-b): three conditions, and each rules out a false reading.

        * Only the **last attempted** candidate can have terminated the run. R1A searched for the
          *first* failure, which under R1B-b would name a candidate the batch cheerfully continued
          past — reporting "stopped on candidate 1" for a run that went on to render 2, 3 and 4.
        * **Something must actually have been left unrun.** A failure on the *final* selected
          candidate ended nothing — the batch completed its whole selection and simply had a
          failure in it, so that reads as a count, not as "stopped on candidate 4".
        * **Its class must be one that genuinely stops the batch**, i.e. one of
          :data:`_BATCH_TERMINAL_CAUSES` minus ``CANCELLED`` — see below.

        [FORK] Digital-Union (C3-R1B-b / R3): the class test replaced "any non-success,
        non-cancelled failure", which was the last surviving pre-R1B-b assumption in this module.
        It read *failure* as *terminal*, and since R1B-b those are different things:

        ```
        candidate 1   CANDIDATE_LOCAL      (a real failure -- and the batch would have CONTINUED)
        Cancel arrives before candidate 2
        -> "0 / 4 succeeded; stopped on candidate 1; batch cancelled"      <- FALSE
        -> "0 / 4 succeeded; 1 failed; batch cancelled before candidate 2"  <- true
        ```

        Candidate 1 did not stop anything; the user's Cancel did. A `CANDIDATE_LOCAL` is counted as
        a failure and **never** named as the stop cause, whatever arrives afterwards.

        So exactly two candidate classes can be terminal, and they are the two the R1B-b policy
        actually stops on:

        ```
        SHARED_FATAL     -> terminal; every remaining candidate would fail identically
        UNKNOWN_FATAL    -> terminal; unproven, so the batch fails closed
        CANDIDATE_LOCAL  -> NEVER terminal; the policy continues past it
        CANCELLED        -> NEVER terminal; cancellation has its own wording ("cancelled during
                            candidate N" / "batch cancelled before candidate N")
        SUCCESS          -> NEVER terminal; it stopped nothing
        ```

        The dual-truth case survives unchanged: a genuine fatal **plus** a cancellation still
        reports both — "stopped on candidate 3; batch cancelled" — because the fatal really was
        terminal. Only a *continued-past* local failure stops being mistaken for one.
        """
        if not self.outcomes or not self.not_attempted:
            return None
        last = self.outcomes[-1]
        if last.outcome_kind not in _CANDIDATE_TERMINAL_CAUSES:
            return None
        return last

    def latest_successful_preview(self) -> str:
        """The newest preview worth showing. Never blanks an earlier success for a later failure."""
        for outcome in reversed(self.outcomes):
            if outcome.success and outcome.preview_path:
                return outcome.preview_path
        return ""

    def durable_paths(self) -> tuple:
        return tuple(o.durable_output_path for o in self.outcomes
                     if o.success and o.durable_output_path)

    def headline(self) -> str:
        """One truthful line. Never names a candidate as the cause of something it did not cause.

        [FORK] Digital-Union (C3-R1B-b): the shape a completed-with-failures batch needs. A local
        failure the batch continued past is reported as a **count**, because no candidate stopped
        anything; only a genuine terminal cause earns "stopped on candidate N".

            4 / 4 succeeded
            3 / 4 succeeded; 1 failed                       <- completed, continued past one
            2 / 4 succeeded; 2 failed                       <- completed, continued past two
            1 / 4 succeeded; 1 failed; stopped on candidate 3   <- local failure THEN a fatal
            1 / 4 succeeded; cancelled during candidate 2
            2 / 4 succeeded; batch cancelled before candidate 3
        """
        base = f"{self.succeeded} / {self.requested_count} succeeded"
        terminal = self._terminal_candidate()
        # Failures the batch CONTINUED past -- i.e. every failure except a terminal one. Reported as
        # a count so the line never implies one of them ended the run.
        continued_failures = self.failed - (1 if terminal is not None else 0)
        parts = [base]
        if continued_failures > 0:
            parts.append(f"{continued_failures} failed")

        if self.cancelled:
            # Cancelled DURING a candidate: that candidate carries CANCELLED itself, so name it.
            if self.outcomes and self.outcomes[-1].cancelled:
                parts.append(f"cancelled during candidate "
                             f"{self.outcomes[-1].candidate_index + 1}")
                return "; ".join(parts)
            # A real failure ended the batch AND a cancellation landed. Both are true; say both,
            # rather than silently letting one hide the other.
            if terminal is not None:
                parts.append(f"stopped on candidate {terminal.candidate_index + 1}")
                parts.append("batch cancelled")
                return "; ".join(parts)
            # The batch boundary case: everything attempted finished, and the batch was cancelled
            # before the next candidate started.
            if self.not_attempted:
                parts.append(f"batch cancelled before candidate {self.attempted + 1}")
            else:
                parts.append("batch cancelled")
            return "; ".join(parts)

        if terminal is not None:
            parts.append(f"stopped on candidate {terminal.candidate_index + 1}")
            return "; ".join(parts)
        if self.not_attempted:
            # Stopped, and the last attempted candidate did not itself fail. Do not invent a
            # candidate failure to explain it -- pre-R2 code did exactly that.
            parts.append(f"stopped after candidate {self.attempted}")
        return "; ".join(parts)

    def summary_text(self) -> str:
        """The whole read-out. `gui.py` formats none of this — one formatter per panel.

        [FORK] Digital-Union (C3-R1B-b): one block per **attempted** candidate, exactly as before —
        and still never a fabricated record for one that was not attempted. The trailer distinguishes
        a batch that *stopped* from one that *completed with failures*: the latter must not claim any
        candidate went unattempted, because every selected candidate ran.
        """
        lines = [f"Render batch · {self.headline()}", ""]
        for outcome in self.outcomes:
            lines.extend(outcome.report_lines())

        if self.cancelled:
            lines.append("")
            lines.append("Batch CANCELLED.")
            if self.not_attempted:
                lines.append(f"{self.not_attempted} "
                             f"candidate{'' if self.not_attempted == 1 else 's'} not attempted.")
            if self.durable_paths():
                lines.append("Earlier successful output was kept.")
        elif self.not_attempted:
            lines.append("")
            lines.append(f"{self.not_attempted} "
                         f"candidate{'' if self.not_attempted == 1 else 's'} not attempted.")
            if self.durable_paths():
                lines.append("Earlier successful output was kept.")
        elif self.failed:
            # Completed the whole selection despite candidate-local failures. Saying "not attempted"
            # here would be false -- every selected candidate ran.
            lines.append("")
            lines.append(f"All {self.requested_count} selected candidates were attempted. "
                         f"{self.failed} failed; every successful output was kept.")
        return "\n".join(lines)


__all__ = [
    # [FORK] Digital-Union (C3-R1B-b): the exact-size constant is GONE, replaced by the range. There
    # is deliberately no `RENDER_SELECTION_SIZE` alias -- every consumer was internal to this module
    # (verified across src/ and tests/), and an ambiguous alias beside a range is how a future
    # caller silently reintroduces the two-candidate assumption.
    "RENDER_SELECTION_MAX",
    "RENDER_SELECTION_MIN",
    "RenderBatchOutcome",
    "RenderBatchRequest",
    "RenderCandidateOutcome",
    "RenderCandidateRequest",
    "build_request",
    "candidate_output_stem",
    "describe_selection_refusal",
    "normalize_selection",
]
