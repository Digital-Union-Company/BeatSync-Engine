#!/usr/bin/env python3
"""[FORK] Digital-Union: render exactly two compared candidates (C3-R0).

C3 V1 let the user generate N candidate settings, compare them and apply **one**. The payoff of a
comparison, though, is watching the videos — and getting two meant two manual round-trips, with the
batch consumed by the first Apply and the base moved out from under the rest. C3-R0 closes that
loop::

    generate N  ->  compare N  ->  tick exactly TWO  ->  Render Selected Variants
                                                         candidate A, then candidate B

**Exactly two, sequential, no cancellation.** Those three are one decision, not three. There is no
safe stop channel in this architecture today — a cancelled Gradio event can return its slot while
the daemon render worker is still alive, and the next render would then wipe the live one's
*process-global* processing directory — so C3-R0 ships no Stop control at all and instead bounds
the commitment to two renders. Three or more, continue-after-failure and real cancellation are
C3-R1, behind an explicit worker lifecycle.

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
would compute an identical destination — and ``shutil.move`` silently overwrites, measured. So the
stem derived here carries the **candidate index and the candidate master**, plus a per-invocation
request tag::

    music_video_batch20261003_161234_123456_c01_m609591

The existing suffix is left entirely alone; this only makes the *base* distinct.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Selection contract
# ---------------------------------------------------------------------------

#: C3-R0 renders **exactly** two. Not a range with equal ends by accident — the number is the whole
#: safety argument. With no cancellation, a batch is an unbreakable commitment, and two is the
#: smallest commitment that delivers the thing C3's comparison was for: an A/B you can watch.
#:
#: Deliberately unrelated to ``variant_batch.CANDIDATE_COUNT_MAX`` (12). That bound is comparison
#: legibility and costs about a millisecond; this one is render minutes you cannot interrupt. Do not
#: let the two numbers learn about each other.
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
    """What one candidate actually produced. Deepcopy-safe; strings and ints only.

    **`durable_output_path` is the success authority**, not the preview and not the status prose.
    That distinction is load-bearing for ProRes, where the durable artifact is the ``.mov`` moved
    into `output/` while the path handed back for display is a session-temp ``_preview.mp4``: a
    preview step that fails afterwards must not retroactively turn a finished render into a
    failure.
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

    def report_lines(self) -> list:
        head = (f"Candidate {self.candidate_index + 1} · master {self.candidate_master_seed} · "
                f"Variation Seed {self.variation_seed} · "
                f"{'SUCCESS' if self.success else 'FAILED'}")
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
    output is deleted**. That is not a rollback decision taken lightly — the render boundary
    exposes no typed failure classification, so a batch cannot tell a shared-input failure (which
    would simply repeat) from a candidate-local one, and continuing would at best waste a render.
    Continue-on-failure waits for C3-R1 and a typed outcome model.
    """

    requested_count: int
    outcomes: tuple = ()
    stopped_on_failure: bool = False

    @property
    def succeeded(self) -> int:
        return sum(1 for o in self.outcomes if o.success)

    @property
    def attempted(self) -> int:
        return len(self.outcomes)

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
        if self.stopped_on_failure:
            failed = next((o for o in self.outcomes if not o.success), None)
            position = (failed.candidate_index + 1) if failed is not None else self.attempted
            return (f"{self.succeeded} / {self.requested_count} succeeded; "
                    f"stopped on candidate {position}")
        return f"{self.succeeded} / {self.requested_count} succeeded"

    def summary_text(self) -> str:
        """The whole read-out. `gui.py` formats none of this — one formatter per panel."""
        lines = [f"Render batch · {self.headline()}", ""]
        for outcome in self.outcomes:
            lines.extend(outcome.report_lines())
        if self.stopped_on_failure and self.attempted < self.requested_count:
            remaining = self.requested_count - self.attempted
            lines.append("")
            lines.append(f"{remaining} candidate(s) not attempted. Earlier successful output "
                         f"was kept.")
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
