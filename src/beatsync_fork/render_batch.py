#!/usr/bin/env python3
"""[FORK] Digital-Union: render exactly two compared candidates (C3-R0).

C3 V1 let the user generate N candidate settings, compare them and apply **one**. The payoff of a
comparison, though, is watching the videos — and getting two meant two manual round-trips, with the
batch consumed by the first Apply and the base moved out from under the rest. C3-R0 closes that
loop::

    generate N  ->  compare N  ->  tick exactly TWO  ->  Render Selected Variants
                                                         candidate A, then candidate B

**Exactly two, sequential — with cancellation since C3-R1A.** C3-R0 shipped with no cancellation at
all: a cancelled Gradio event could return its slot while the daemon render worker was still alive,
and the next render would then wipe the live one's *process-global* processing directory — so
C3-R0 shipped no Stop control and instead bounded the commitment to two renders. C3-R1A closed that
gap with an explicit, shared ``beatsync_fork.render_worker.RenderLifecycle`` spanning the whole
batch, so a Cancel click is observed at the next safe boundary instead of tearing down a live
worker — BOUNDARY_ONLY_CANCEL: an FFmpeg-class subprocess stops within moments, an in-progress
Stage-5 call is never hard-killed. The count is still bounded to exactly two. Three or more, and
continuing to the next candidate after a cancellation or failure, remain C3-R1B.

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
a batch that was asked for two videos would return one. Identity here is what lets both requested
candidates succeed; the promotion is what guarantees neither can be overwritten. Do not drop one
because the other exists.
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

#: C3-R0 renders **exactly** two. Not a range with equal ends by accident — the number is the whole
#: safety argument. Cancellation since C3-R1A makes a batch interruptible at safe boundaries, but
#: never instant, and two is still the smallest commitment that delivers the thing C3's comparison
#: was for: an A/B you can watch.
#:
#: Deliberately unrelated to ``variant_batch.CANDIDATE_COUNT_MAX`` (12). That bound is comparison
#: legibility and costs about a millisecond; this one is render minutes, still bounded to two even
#: with cancellation available. Do not let the two numbers learn about each other.
RENDER_SELECTION_SIZE = 2


def normalize_selection(selection: Any, candidate_count: int) -> tuple:
    """The chosen candidate indices in **canonical ascending order**, or ``()`` if unusable.

    Ascending original index rather than tick order, so the render sequence is a property of the
    batch and not of how the user happened to click. Re-ticking the same pair in the other order
    renders the same two videos in the same order.

    Total and never raising — a ``CheckboxGroup`` may hand over anything. ``bool`` is rejected
    first because it subclasses ``int`` and ``True`` would otherwise read as index 1. Duplicates,
    fractional values, strings, out-of-range indices and any count other than
    :data:`RENDER_SELECTION_SIZE` all answer ``()``, which the caller reports as a refusal rather
    than guessing at an intent.
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
    if len(chosen) != RENDER_SELECTION_SIZE:
        return ()
    return tuple(sorted(chosen))


def describe_selection_refusal(selection: Any, candidate_count: int) -> str:
    """Why a selection was refused, in the user's terms. Diagnostics only."""
    if not candidate_count:
        return "No candidates to render. Press Generate Variants first."
    count = len(selection) if isinstance(selection, Sequence) and not isinstance(
        selection, (str, bytes)) else 0
    if count != RENDER_SELECTION_SIZE:
        return (f"Select exactly {RENDER_SELECTION_SIZE} candidates to render — "
                f"{count} selected.")
    return "That selection could not be read. Re-tick two candidates and try again."


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
    two candidates are never simultaneously on screen. So the candidate-specific half is frozen
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
        if len(self.candidates) != RENDER_SELECTION_SIZE:
            raise ValueError(
                f"a render batch is exactly {RENDER_SELECTION_SIZE} candidates, "
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


@dataclass(frozen=True)
class RenderBatchOutcome:
    """The whole batch's truthful record, and the one formatter for it.

    Fail-fast with prior success preserved: a failed candidate stops the batch and **no earlier
    output is deleted**.

    [FORK] Digital-Union (C3-R1B-a): this used to say the render boundary exposed no typed failure
    classification, so a batch could not tell a shared-input failure from a candidate-local one.
    That half is no longer true — C3-R1B-a gave every reachable producer an explicit
    `RenderOutcomeKind`, `SHARED_FATAL` has real proven producers, and `gui.py` now threads the
    **full** class into every :class:`RenderCandidateOutcome` instead of preserving only
    ``CANCELLED``. What has **not** changed is the behaviour: the batch still stops after every
    non-success candidate, ``CANDIDATE_LOCAL`` included, and `RENDER_SELECTION_SIZE` is still 2.
    Continue-after-``CANDIDATE_LOCAL`` and rendering three or four candidates are C3-R1B-b, which
    will consume this classification; nothing in this module branches on it.

    [FORK] Digital-Union (C3-R1A / R2): ``outcome_kind`` is the **batch-level** terminal cause, and
    it exists because the candidate-level one cannot express the batch boundary case. When a
    cancellation lands in the gap *between* candidate 1 and candidate 2:

    ```
    candidate 1   SUCCESS          (really did succeed; its output is on disk)
    candidate 2   NOT ATTEMPTED    (no outcome record exists, and none is fabricated)
    batch         CANCELLED        <- expressible nowhere else
    ```

    No candidate carries CANCELLED there, because no candidate was cancelled — the *batch* was. R1A
    reported that as ``1 / 2 succeeded; stopped on candidate 1``, which is false twice over:
    candidate 1 did not stop anything and nothing failed. A fake candidate-2 attempt labelled
    CANCELLED would be equally false, so the shape above is recorded literally instead.
    """

    requested_count: int
    outcomes: tuple = ()
    stopped_on_failure: bool = False
    outcome_kind: "RenderOutcomeKind | None" = None

    def __post_init__(self) -> None:
        """Derive the batch cause only where a candidate *proves* it; otherwise leave it unstated.

        Exactly one derivation is sound: a candidate that was itself cancelled proves the top-level
        render event was cancelled. Everything else stays ``None`` — meaning "not stated" — so every
        pre-R2 construction keeps byte-identical `headline()` / `summary_text()` output. In
        particular the batch-boundary case has **no** cancelled candidate to derive from, which is
        precisely why `gui.py` must state it explicitly; nothing here can rescue a caller that
        forgets to.
        """
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
    def attempted(self) -> int:
        return len(self.outcomes)

    @property
    def not_attempted(self) -> int:
        """Candidates the batch never started. Never negative, even on a malformed count."""
        return max(0, self.requested_count - self.attempted)

    def _failed_candidate(self):
        """The first candidate that genuinely FAILED — a cancelled one is not a failure.

        Load-bearing for truthfulness: a cancelled candidate also has ``success is False``, so the
        pre-R2 ``next(o for o in outcomes if not o.success)`` happily reported a user's Stop as
        ``stopped on candidate N`` with no mention of cancellation at all.
        """
        return next((o for o in self.outcomes if not o.success and not o.cancelled), None)

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
        """One truthful line. Never names a candidate as the cause of something it did not cause."""
        base = f"{self.succeeded} / {self.requested_count} succeeded"
        failed = self._failed_candidate()

        if self.cancelled:
            # Cancelled DURING a candidate: that candidate carries CANCELLED itself, so name it.
            if self.outcomes and self.outcomes[-1].cancelled:
                return (f"{base}; cancelled during candidate "
                        f"{self.outcomes[-1].candidate_index + 1}")
            # A real failure stopped the batch AND a cancellation landed. Both are true; say both,
            # rather than silently letting one hide the other.
            if failed is not None:
                return (f"{base}; stopped on candidate {failed.candidate_index + 1}; "
                        f"batch cancelled")
            # The batch boundary case: everything attempted succeeded, and the batch was cancelled
            # before the next candidate started.
            if self.not_attempted:
                return f"{base}; batch cancelled before candidate {self.attempted + 1}"
            return f"{base}; batch cancelled"

        if failed is not None:
            return f"{base}; stopped on candidate {failed.candidate_index + 1}"
        if self.stopped_on_failure:
            # Stopped, nothing failed, not cancelled. Do not invent a candidate failure to explain
            # it — the pre-R2 code did exactly that and produced "stopped on candidate 1" for a
            # candidate that had succeeded.
            return f"{base}; stopped after candidate {self.attempted}"
        return base

    def summary_text(self) -> str:
        """The whole read-out. `gui.py` formats none of this — one formatter per panel."""
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
        elif self.stopped_on_failure and self.not_attempted:
            lines.append("")
            lines.append(f"{self.not_attempted} candidate(s) not attempted. Earlier successful "
                         f"output was kept.")
        return "\n".join(lines)


__all__ = [
    "RENDER_SELECTION_SIZE",
    "RenderBatchOutcome",
    "RenderBatchRequest",
    "RenderCandidateOutcome",
    "RenderCandidateRequest",
    "build_request",
    "candidate_output_stem",
    "describe_selection_refusal",
    "normalize_selection",
]
