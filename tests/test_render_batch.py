"""C3-R0 → C3-R1B-b: the pure model behind rendering 2–4 compared candidates.

`beatsync_fork/render_batch.py` is stdlib-only, so it is imported and exercised as ordinary code.
Four properties carry this feature and each has a section:

1. **A bounded range of 2 to 4, in canonical ascending index order.** The selection contract is the
   whole safety story — a batch costs render minutes, and C3-R1A's cancellation makes it
   interruptible at safe boundaries but never instant, so it must still be a commitment the user
   explicitly and unambiguously made. An over-long selection is **refused**, never truncated.
2. **Candidate identity cannot rest on the Variation Seed.** C3 deduplicates candidate *masters*
   and says nothing about `CreativeRecipe.seed`; collisions are real, and the existing render path
   names its output `_seed<VariationSeed>`.
3. **Continue past a candidate-local failure; stop on anything else — and never roll back.**
   C3-R1B-a made the causes truthful and C3-R1B-b spends that on exactly one of them. So "a
   candidate failed" and "the batch stopped" are now independent facts, which is why
   `stopped_on_failure` is gone and `_terminal_candidate()` names only the candidate that ended the
   run *with work still outstanding*.
4. **Deepcopy-safe and runtime-free.** These records cross a Gradio event boundary and must carry
   nothing that runs.
"""

from __future__ import annotations

import ast
import copy
import os

import pytest

from beatsync_fork import presets as fork_presets
from beatsync_fork import render_batch as rb
from beatsync_fork import render_worker as rw
from beatsync_fork import variant_batch as fork_batch
from beatsync_fork import variant_lab as fork_lab

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "render_batch.py")

FIELDS = fork_presets.CREATIVE_CONTROL_FIELDS
AUDIO_FIELDS = fork_lab.AUDIO_CONTROL_FIELDS
BALANCED = dict(zip(FIELDS, (50,) * 6))
AUDIO_BASE = {"music_under_voice_percent": 35, "sfx_amount": 50, "sfx_level_percent": 50}

TAG = "20261003_161234_123456"


def _executable_source(path: str) -> str:
    """Source with docstrings stripped, so prose stating a boundary never reads as crossing it."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _batch(count=5, master=582913):
    config = fork_lab.VariantLabConfig(
        master_seed=master, spread=50, randomized=fork_lab.default_randomized(),
        ranges=fork_lab.default_ranges())
    audio_config = fork_lab.AudioVariantConfig(randomized=frozenset(AUDIO_FIELDS))
    return fork_batch.resolve_batch(fork_batch.declaration_from(
        config, BALANCED, audio_config, AUDIO_BASE, count))


def _request(selection=(0, 1), count=5, base="music_video.mp4", tag=TAG):
    return rb.build_request(_batch(count), list(selection), user_base=base, request_tag=tag)


# ===========================================================================
# 1. SELECTION — a bounded range of 2..4, canonical order (C3-R1B-b)
# ===========================================================================


def test_the_render_selection_range_is_two_to_four_and_is_not_the_comparison_bound():
    """[C3-R1B-b] The exact-size constant became a range, and the cap is frozen at 4.

    MAX is a product contract derived in C3-R1B/P0 from measured per-candidate wall-clock, not a
    number to tune: past candidate 1 the cost is linear (Stage 1-3 reuse is already fully banked at
    candidate 2), so four candidates is the largest commitment one click may make.
    """
    assert rb.RENDER_SELECTION_MIN == 2
    assert rb.RENDER_SELECTION_MAX == 4
    assert rb.RENDER_SELECTION_MIN < rb.RENDER_SELECTION_MAX, "it must be a real range"
    # the exact-size constant is GONE -- no ambiguous alias beside the range
    assert not hasattr(rb, "RENDER_SELECTION_SIZE"), \
        "RENDER_SELECTION_SIZE survived; a future caller will reintroduce the two-candidate rule"
    # still unrelated to the comparison bound: generating costs a millisecond, rendering costs
    # minutes, and the two numbers must not learn about each other
    assert rb.RENDER_SELECTION_MAX != fork_batch.CANDIDATE_COUNT_MAX
    source = _executable_source(_MODULE)
    assert "CANDIDATE_COUNT_MAX" not in source
    assert "variant_batch" not in source


def test_the_generation_bound_was_not_touched():
    """R1B-b raised the RENDER cap only. Candidate generation still allows up to 12."""
    assert fork_batch.CANDIDATE_COUNT_MAX == 12


@pytest.mark.parametrize("selection,expected", [
    ([0, 1], (0, 1)),
    ([1, 0], (0, 1)),          # tick order must not decide render order
    ([4, 2], (2, 4)),
    ((3, 0), (0, 3)),
    # [C3-R1B-b] three and four are now valid, and canonical order still wins
    ([0, 1, 2], (0, 1, 2)),
    ([2, 0, 1], (0, 1, 2)),
    ([0, 1, 2, 3], (0, 1, 2, 3)),
    ([3, 1, 0, 2], (0, 1, 2, 3)),
    ((4, 2, 0), (0, 2, 4)),
])
def test_a_valid_selection_is_canonically_ascending(selection, expected):
    assert rb.normalize_selection(selection, 5) == expected


def test_an_over_long_selection_is_refused_never_truncated():
    """[C3-R1B-b] Five ticks is a refusal, not "render the first four".

    Silently dropping a candidate the user explicitly ticked would render something they did not
    ask for — and would do it after they committed to the wait.
    """
    assert rb.normalize_selection([0, 1, 2, 3, 4], 5) == ()
    assert rb.normalize_selection([0, 1, 2, 3, 4, 5], 6) == ()


@pytest.mark.parametrize("selection", [
    [], [0],                                    # below MIN
    [0, 1, 2, 3, 4],                            # above MAX
    [0, 1, 2, 3, 4, 5],                         # well above MAX
    [0, 0], [3, 3], [0, 1, 1], [0, 1, 2, 2],    # duplicates, at every valid length
    [0, 5], [-1, 0], [0, 99], [0, 1, 2, 7],     # out of range
    [True, 1], [0, False], [0, 1, True],        # bool subclasses int
    [0, 1.0], [0, "1"], ["0", "1"], [0, 1, 2.5],  # wrong types
    None, 0, "01", b"01", {0: 1},               # not a usable sequence
])
def test_an_unusable_selection_is_refused_and_never_raises(selection):
    assert rb.normalize_selection(selection, 5) == ()


def test_the_refusal_text_names_the_actual_problem():
    """[C3-R1B-b] Range-based copy. "exactly two" is now a lie and must not return."""
    assert "Generate Variants" in rb.describe_selection_refusal([0, 1], 0)
    for bad, count in (([0], 1), ([], 0), ([0, 1, 2, 3, 4], 5)):
        message = rb.describe_selection_refusal(bad, 5)
        assert "2 to 4" in message, message
        assert f"{count} selected" in message, message
        assert "exactly" not in message.lower(), message
    # a VALID count that still failed to normalise gets the other message, also range-based
    unreadable = rb.describe_selection_refusal([0, 0, 1], 5)
    assert "2 to 4" in unreadable and "could not be read" in unreadable


def test_the_refusal_text_never_says_exactly_two_anywhere():
    """A single structural sweep, so the phrase cannot survive in one branch."""
    for selection in ([], [0], [0, 1], [0, 1, 2], [0, 1, 2, 3], [0, 1, 2, 3, 4], None, "01"):
        message = rb.describe_selection_refusal(selection, 5)
        assert "exactly two" not in message.lower()
        assert "exactly 2" not in message


def test_build_request_refuses_rather_than_guessing():
    for bad in ([], [0], [0, 1, 2, 3, 4], [0, 0], None, "01"):
        request, refusal = _request(selection=bad) if isinstance(bad, (list, tuple)) else \
            rb.build_request(_batch(), bad, user_base="x", request_tag=TAG)
        assert request is None
        assert refusal


@pytest.mark.parametrize("selection,expected_count", [
    ([0, 1], 2),
    ([0, 1, 2], 3),
    ([0, 1, 2, 3], 4),
])
def test_build_request_accepts_two_three_and_four(selection, expected_count):
    """[C3-R1B-b] The whole authorized range reaches a constructed request."""
    request, refusal = _request(selection=selection)
    assert request is not None, refusal
    assert request.count == expected_count
    assert [c.candidate_index for c in request.candidates] == sorted(selection)
    # distinct stems at every length -- what lets every requested candidate succeed
    stems = [c.output_stem for c in request.candidates]
    assert len(set(stems)) == expected_count


def test_a_missing_batch_is_refused():
    request, refusal = rb.build_request(None, [0, 1], user_base="x", request_tag=TAG)
    assert request is None and "Generate Variants" in refusal


def test_the_request_copies_the_selected_candidates_exactly():
    batch = _batch(5)
    request, _ = rb.build_request(batch, [3, 1], user_base="music_video", request_tag=TAG)
    assert [c.candidate_index for c in request.candidates] == [1, 3]
    for chosen, source_index in zip(request.candidates, (1, 3)):
        original = batch.candidates[source_index]
        assert chosen.candidate_master_seed == original.master_seed
        assert chosen.creative_recipe == original.creative_recipe
        assert chosen.audio_recipe == original.audio_recipe
        assert chosen.variation_seed() == original.creative_recipe.seed
    assert request.batch_root_master == batch.declaration.root_master_seed
    assert request.count == 2


def test_a_request_cannot_be_constructed_with_the_wrong_shape():
    request, _ = _request(selection=(0, 1, 2, 3))
    with pytest.raises(ValueError):                      # 0 candidates
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1, candidates=())
    with pytest.raises(ValueError):                      # 1 candidate, below MIN
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=request.candidates[:1])
    with pytest.raises(ValueError):                      # 5 candidates, above MAX
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=request.candidates + (request.candidates[0],))
    with pytest.raises(ValueError):                      # descending order
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=(request.candidates[1], request.candidates[0]))
    with pytest.raises(ValueError):                      # duplicate candidate
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=(request.candidates[0], request.candidates[0]))


@pytest.mark.parametrize("size", [2, 3, 4])
def test_a_request_accepts_every_authorized_size(size):
    request, _ = _request(selection=tuple(range(size)))
    rebuilt = rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                                    candidates=request.candidates[:size])
    assert rebuilt.count == size


# ===========================================================================
# 2. OUTPUT IDENTITY — must survive equal Variation Seeds
# ===========================================================================


def test_the_stem_carries_index_master_and_request_tag():
    request, _ = _request(selection=(0, 2), base="music_video.mp4")
    first, second = request.candidates
    assert "music_video" in first.output_stem
    assert TAG in first.output_stem
    assert "_c01_" in first.output_stem
    assert "_c03_" in second.output_stem
    assert str(first.candidate_master_seed) in first.output_stem
    assert str(second.candidate_master_seed) in second.output_stem
    assert first.output_stem != second.output_stem
    # the user's extension is dropped; the render path appends its own
    assert not first.output_stem.endswith(".mp4")


def test_identical_variation_seeds_still_produce_distinct_stems():
    """**The measured reason this feature does not name outputs by Variation Seed.**

    C3 deduplicates candidate *masters* deliberately; `CreativeRecipe.seed` is an independent draw
    per master and is not deduplicated anywhere. Scanning real batches found root 5484, where
    masters 945730 and 862920 both resolve Variation Seed 536635. The existing render path names
    its file `_seed<VariationSeed>`, so two such candidates rendered in the same second would
    compute one identical destination if identity rested on that seed.

    **Re-stated after H1, and still required.** Under C3-R0 the render promoted with `shutil.move`
    and a collision meant one candidate silently *destroying* the other's video. H1 made GUI
    promotion an atomic no-replace `os.rename`, so that destruction can no longer happen — but a
    collision now makes the second candidate legitimately *refuse*, and a batch asked for two
    videos would deliver one. Equal Variation Seeds must therefore still yield distinct candidate
    stems, so both selected renders can coexist and succeed.
    """
    stems = {
        rb.candidate_output_stem("music_video", TAG, index, master)
        for index, master in ((0, 945730), (1, 862920))
    }
    assert len(stems) == 2, "equal Variation Seeds must not collapse two candidates onto one name"

    # and the same holds through the whole request path, with the colliding pair made explicit
    colliding = [fork_lab.resolve_clip_seed(945730), fork_lab.resolve_clip_seed(862920)]
    assert colliding[0] == colliding[1] == 536635, "the measured collision still reproduces"


def test_a_different_request_tag_changes_stems_but_no_recipe_data():
    one, _ = _request(tag="20261003_161234_000001")
    two, _ = _request(tag="20261003_161235_000002")
    assert [c.output_stem for c in one.candidates] != [c.output_stem for c in two.candidates]
    assert [c.creative_recipe for c in one.candidates] == \
        [c.creative_recipe for c in two.candidates]
    assert [c.audio_recipe for c in one.candidates] == [c.audio_recipe for c in two.candidates]
    assert [c.candidate_master_seed for c in one.candidates] == \
        [c.candidate_master_seed for c in two.candidates]


@pytest.mark.parametrize("base,expected", [
    ("music_video.mp4", "music_video"),
    ("music_video", "music_video"),
    ("my.video.mov", "my.video"),
    ("", "music_video"),
    ("   ", "music_video"),
    (None, "music_video"),
])
def test_the_user_base_is_normalised_totally(base, expected):
    stem = rb.candidate_output_stem(base, TAG, 0, 123)
    assert stem.startswith(expected + "_batch")


def test_the_index_is_one_based_and_zero_padded_for_sorting():
    assert "_c01_" in rb.candidate_output_stem("v", TAG, 0, 1)
    assert "_c10_" in rb.candidate_output_stem("v", TAG, 9, 1)


# ===========================================================================
# 3. OUTCOMES — fail fast, preserve prior success
# ===========================================================================


def _outcome(index, master, seed, success, durable="", preview="", status="",
             audio="", smart=""):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=master, variation_seed=seed,
        success=success, durable_output_path=durable, preview_path=preview,
        status_text=status, audio_layers_report=audio, smart_mix_report=smart)


def test_two_successes_report_two_of_two():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, True, durable="C:/out/a.mp4", preview="C:/out/a.mp4"),
        _outcome(1, 222, 22, True, durable="C:/out/b.mp4", preview="C:/out/b.mp4"),
    ))
    assert outcome.succeeded == 2
    assert outcome.headline() == "2 / 2 succeeded"
    assert outcome.durable_paths() == ("C:/out/a.mp4", "C:/out/b.mp4")
    assert outcome.latest_successful_preview() == "C:/out/b.mp4"
    assert outcome.failed == 0
    assert outcome.not_attempted == 0
    assert outcome.stopped_early is False


def test_a_first_candidate_failure_stops_after_one_attempt():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, False, status="FFmpeg extraction failed"),
    ))
    assert outcome.attempted == 1
    assert outcome.succeeded == 0
    assert "0 / 2 succeeded" in outcome.headline()
    assert "stopped on candidate 1" in outcome.headline()
    assert outcome.durable_paths() == ()
    assert "not attempted" in outcome.summary_text()


def test_a_second_candidate_failure_preserves_the_first_output():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, True, durable="C:/out/a.mp4", preview="C:/out/a.mp4"),
        _outcome(1, 222, 22, False, status="no legal placement for voice clip 2"),
    ))
    assert outcome.succeeded == 1
    # [C3-R1B-b] the final selected candidate failed, so nothing was stopped -- a count, not a blame
    assert outcome.headline() == "1 / 2 succeeded; 1 failed"
    assert outcome.durable_paths() == ("C:/out/a.mp4",), "prior success is never discarded"
    assert outcome.latest_successful_preview() == "C:/out/a.mp4", \
        "a later failure must not blank an earlier preview"
    text = outcome.summary_text()
    assert "C:/out/a.mp4" in text
    assert "no legal placement" in text


def test_durable_output_can_coexist_with_a_missing_preview():
    """ProRes: the `.mov` is moved into output/ before the preview is generated at all."""
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, True, durable="C:/out/a.mov", preview="",
                 status="preview generation timed out"),
    ))
    assert outcome.succeeded == 1
    assert outcome.durable_paths() == ("C:/out/a.mov",)
    assert outcome.latest_successful_preview() == "", "no preview to show, but the render stands"
    assert "a.mov" in outcome.summary_text()


def test_per_candidate_diagnostics_stay_with_their_own_candidate():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, True, durable="a.mp4", audio="AUDIO-ONE", smart="SMART-ONE"),
        _outcome(1, 222, 22, True, durable="b.mp4", audio="AUDIO-TWO", smart="SMART-TWO"),
    ))
    text = outcome.summary_text()
    assert text.index("AUDIO-ONE") < text.index("AUDIO-TWO")
    assert text.index("SMART-ONE") < text.index("SMART-TWO")
    assert text.index("111") < text.index("222")
    first = outcome.outcomes[0].report_lines()
    assert any("AUDIO-ONE" in line for line in first)
    assert not any("AUDIO-TWO" in line for line in first)


def test_the_summary_names_every_attempted_candidate_with_its_provenance():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 945730, 536635, True, durable="C:/out/a.mp4"),
        _outcome(1, 862920, 536635, True, durable="C:/out/b.mp4"),
    ))
    text = outcome.summary_text()
    for token in ("Candidate 1", "Candidate 2", "945730", "862920", "536635",
                  "SUCCESS", "C:/out/a.mp4", "C:/out/b.mp4"):
        assert token in text, token


def test_multiline_diagnostics_are_collapsed_to_one_line_each():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 1, 2, True, durable="a.mp4", audio="line one\nline two\nline three"),
    ))
    for line in outcome.summary_text().splitlines():
        assert "\n" not in line
    assert "line one line two line three" in outcome.summary_text()


def test_an_empty_outcome_set_is_still_formattable():
    outcome = rb.RenderBatchOutcome(requested_count=2)
    assert outcome.succeeded == 0 and outcome.attempted == 0
    assert outcome.summary_text()
    assert outcome.latest_successful_preview() == ""


# ===========================================================================
# 4. IMMUTABILITY AND PURITY
# ===========================================================================


def test_requests_and_outcomes_survive_a_deep_copy():
    request, _ = _request()
    assert copy.deepcopy(request) == request
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 1, 2, True, durable="a.mp4"),
        _outcome(1, 3, 4, False, status="boom"),
    ))
    assert copy.deepcopy(outcome) == outcome


def test_the_request_carries_no_runtime_object():
    import dataclasses
    request, _ = _request()
    allowed = (rb.RenderBatchRequest, rb.RenderCandidateRequest,
               type(request.candidates[0].creative_recipe),
               type(request.candidates[0].audio_recipe),
               int, str, tuple)
    seen = set()

    def walk(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        assert isinstance(value, allowed), f"unexpected {type(value).__name__} in a request"
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            for field in dataclasses.fields(value):
                walk(getattr(value, field.name))
        elif isinstance(value, tuple):
            for item in value:
                walk(item)

    walk(request)


def test_the_models_are_frozen():
    request, _ = _request()
    for target, field, value in ((request, "request_tag", "x"),
                                 (request.candidates[0], "output_stem", "x")):
        with pytest.raises(Exception):
            setattr(target, field, value)


def test_the_module_imports_only_stdlib_and_one_named_sibling_fork_module():
    """**Amended by C3-R1A**, and deliberately pinned tighter rather than loosened.

    The hard rule in CLAUDE.md is "no upstream runtime", not "no fork sibling": `render_batch` now
    imports `RenderOutcomeKind` from `beatsync_fork.render_worker`, which is itself stdlib-only, so
    the bare-interpreter property this guard protects is intact. Rather than widen the allowlist to
    "any `beatsync_fork`", the exact fork module is named — a second, unreviewed fork dependency
    appearing here would still fail, which is what the original one-line allowlist was for.
    """
    with open(_MODULE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    stdlib, fork = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                (fork if alias.name.startswith("beatsync_fork") else stdlib).add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("beatsync_fork"):
                fork.add(node.module)
            else:
                stdlib.add(node.module)
    assert {m.split(".")[0] for m in stdlib} <= {
        "__future__", "collections", "dataclasses", "typing"}, sorted(stdlib)
    assert fork == {"beatsync_fork.render_worker"}, sorted(fork)


def test_the_module_reaches_no_runtime_and_renders_nothing():
    """It decides; `gui.py` performs every side effect."""
    source = _executable_source(_MODULE).lower()
    # Substring tokens: unambiguous on their own.
    for forbidden in ("gradio", "gr.", "numpy", "subprocess", "ffmpeg", "open(", "os.path",
                      "shutil", "time.", "datetime", "random", "uuid",
                      "video_processor", "auto_mode", "ffmpeg_processing",
                      "create_music_video", "analyze_beats_auto", "process_video",
                      "stage_cache", "shortlist", "thumbnail", "gallery"):
        assert forbidden not in source, f"render_batch references {forbidden!r}"
    # Word tokens: `paths` as a module, never as part of `durable_paths`. Imports are pinned
    # separately above; this catches a late `from paths import ...` inside a function body.
    import re as _re
    for forbidden in ("paths", "os", "sys"):
        assert not _re.search(rf"(import|from)\s+{forbidden}", source), forbidden


def test_this_module_owns_no_cancellation_machinery():
    """**Amended by C3-R1A.** It *records* a cancellation; it must never *perform* one.

    C3-R0's version banned the word "cancel" outright, because C3-R0 shipped no Stop control at all
    and a fake one would have been worse than none. R1A makes a batch genuinely cancellable, so the
    typed cause now legitimately appears here — `outcome_kind`, `.cancelled`, the `CANCELLED` report
    label. What has NOT changed is the division of labour this module exists to enforce: it decides
    and reports, `gui.py` performs every side effect. So every piece of actual cancellation
    *machinery* stays banned, and the list is widened rather than shortened — a `threading.Event`,
    a `terminate`/`kill`, a signal or a `Popen` appearing here would mean the pure decision layer
    had grown a runtime.
    """
    source = _executable_source(_MODULE).lower()
    for forbidden in ("stop_flag", "stop_event", "threading", "terminate", "abort",
                      "kill", "signal", "popen", "event(", "lock(", "is_set", "wait("):
        assert forbidden not in source, f"render_batch performs cancellation: {forbidden!r}"

    # And the cancellation surface it *does* own is exactly the typed one, nothing more.
    assert "renderoutcomekind.cancelled" in source, \
        "the typed cancelled cause is how this module may speak about cancellation"
    assert not hasattr(rb, "request_cancel"), "render_batch must expose no cancel entry point"
    assert "request_cancel" not in source


# ---------------------------------------------------------------------------
# C3-R1A: the typed outcome rides alongside `success` and must never disagree with it
# ---------------------------------------------------------------------------


def _typed(success=True, kind=None, path="C:/out/a.mp4"):
    return rb.RenderCandidateOutcome(
        candidate_index=0, candidate_master_seed=1, variation_seed=2,
        durable_output_path=(path if success else ""),
        success=success, status_text="s", outcome_kind=kind)


def test_an_omitted_outcome_kind_is_derived_conservatively():
    """Every call site predating C3-R1A omits it, and must not be guessed into a specific cause."""
    assert _typed(success=True).outcome_kind is rw.RenderOutcomeKind.SUCCESS
    assert _typed(success=False).outcome_kind is rw.RenderOutcomeKind.UNKNOWN_FATAL
    # never a more specific class than "we do not know"
    assert _typed(success=False).outcome_kind is not rw.RenderOutcomeKind.CANDIDATE_LOCAL
    assert _typed(success=False).outcome_kind is not rw.RenderOutcomeKind.CANCELLED


def test_a_supplied_outcome_kind_must_agree_with_success():
    """The two fields are one truth. Drift between them is how a cancelled render reads as done."""
    for kind in (rw.RenderOutcomeKind.CANCELLED, rw.RenderOutcomeKind.CANDIDATE_LOCAL,
                 rw.RenderOutcomeKind.SHARED_FATAL, rw.RenderOutcomeKind.UNKNOWN_FATAL):
        assert _typed(success=False, kind=kind).outcome_kind is kind
        with pytest.raises(ValueError):
            _typed(success=True, kind=kind)
    assert _typed(success=True, kind=rw.RenderOutcomeKind.SUCCESS).success
    with pytest.raises(ValueError):
        _typed(success=False, kind=rw.RenderOutcomeKind.SUCCESS)


def test_a_cancelled_candidate_reports_cancelled_not_failed():
    """A user who pressed Stop must not be told their render failed."""
    cancelled = _typed(success=False, kind=rw.RenderOutcomeKind.CANCELLED)
    assert cancelled.cancelled is True
    head = cancelled.report_lines()[0]
    assert "CANCELLED" in head
    assert "FAILED" not in head and "SUCCESS" not in head

    failed = _typed(success=False)
    assert failed.cancelled is False
    assert "FAILED" in failed.report_lines()[0]

    done = _typed(success=True)
    assert done.cancelled is False
    assert "SUCCESS" in done.report_lines()[0]


# ---------------------------------------------------------------------------
# C3-R1A / R2: the BATCH-level terminal cause, and the boundary case it exists for
# ---------------------------------------------------------------------------


def _batch_outcome(*outcomes, requested=2, kind=None):
    """[C3-R1B-b] `stopped_on_failure` is gone: a failed candidate no longer implies a
    stopped batch, so a boolean meaning "something failed, therefore we stopped" cannot be
    true. Early termination is read from the typed batch cause plus `not_attempted`."""
    return rb.RenderBatchOutcome(requested_count=requested, outcomes=tuple(outcomes),
                                 outcome_kind=kind)


def _success(index, durable="C:/out/a.mov"):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=True, durable_output_path=durable, preview_path=durable, status_text="✅ done")


def _cancelled_candidate(index):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=False, status_text="⏹️ Cancelled. Nothing was rendered.",
        outcome_kind=rw.RenderOutcomeKind.CANCELLED)


def _failed_candidate(index, reason="FFmpeg extraction failed"):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=False, status_text=reason)


def test_a_cancellation_between_candidates_reports_the_batch_as_cancelled():
    """**The R2 defect-A case, behaviourally.** Candidate 1 succeeded; the BATCH was cancelled.

    This is the one shape that cannot be expressed at candidate level: candidate 1 really did
    succeed, candidate 2 was never attempted so no record of it exists, and the thing that was
    cancelled is the top-level render event. R1A reported it as
    ``1 / 2 succeeded; stopped on candidate 1`` — false twice over, since candidate 1 neither failed
    nor stopped anything.
    """
    outcome = _batch_outcome(_success(0), kind=rw.RenderOutcomeKind.CANCELLED)

    assert outcome.succeeded == 1
    assert outcome.attempted == 1
    assert outcome.not_attempted == 1
    assert outcome.cancelled is True

    # candidate 1 is untouched and still a success; no candidate 2 record was fabricated
    assert len(outcome.outcomes) == 1, "a fake candidate-2 attempt was invented"
    assert outcome.outcomes[0].success is True
    assert outcome.outcomes[0].outcome_kind is rw.RenderOutcomeKind.SUCCESS
    assert outcome.outcomes[0].cancelled is False

    headline = outcome.headline()
    assert "cancelled" in headline.lower(), headline
    assert "1 / 2 succeeded" in headline
    assert "batch cancelled before candidate 2" in headline, headline
    # and it must not blame candidate 1 for anything
    assert "stopped on candidate 1" not in headline, headline
    assert "failed" not in headline.lower(), headline

    summary = outcome.summary_text()
    assert "SUCCESS" in summary
    assert "Batch CANCELLED." in summary
    assert "1 candidate not attempted." in summary, summary
    assert "Earlier successful output was kept." in summary
    assert "C:/out/a.mov" in summary, "the earlier durable output must still be named"
    assert "FAILED" not in summary, summary
    assert outcome.durable_paths() == ("C:/out/a.mov",)
    assert outcome.latest_successful_preview() == "C:/out/a.mov"


def test_a_cancellation_during_a_candidate_is_both_candidate_and_batch_cancelled():
    """The other cancellation shape: that candidate carries CANCELLED **and** so does the batch."""
    outcome = _batch_outcome(_success(0), _cancelled_candidate(1),
                             kind=rw.RenderOutcomeKind.CANCELLED)
    assert outcome.cancelled is True
    assert outcome.outcomes[1].cancelled is True
    headline = outcome.headline()
    assert "cancelled during candidate 2" in headline, headline
    assert "stopped on candidate 2" not in headline, "a cancelled candidate is not a failure"
    summary = outcome.summary_text()
    assert "CANCELLED" in summary and "FAILED" not in summary
    assert "Earlier successful output was kept." in summary


def test_a_cancelled_candidate_alone_derives_the_batch_cause():
    """The one sound derivation: a cancelled candidate proves the event was cancelled.

    Stated or derived, the read-out must agree — so a caller that forgets the batch-level field in
    the mid-candidate case still cannot report a cancellation as a failure.
    """
    derived = _batch_outcome(_cancelled_candidate(0))
    assert derived.outcome_kind is rw.RenderOutcomeKind.CANCELLED
    assert derived.cancelled is True
    assert "cancelled during candidate 1" in derived.headline()
    # the boundary case has no such candidate, which is exactly why gui.py must state it
    unstated = _batch_outcome(_success(0))
    assert unstated.outcome_kind is None
    assert unstated.cancelled is False


def test_a_genuine_failure_still_names_the_candidate_that_stopped_the_batch():
    """A failure with work still outstanding keeps the pre-R1B-b wording, unchanged.

    [C3-R1B-b] The second case below moved, and moved to the truth: a failure on the *final*
    selected candidate stopped nothing, so it is now a count rather than "stopped on candidate 2".
    """
    first = _batch_outcome(_failed_candidate(0))     # 1 of 2 attempted -> candidate 2 left unrun
    assert first.headline() == "0 / 2 succeeded; stopped on candidate 1"
    assert "not attempted" in first.summary_text()
    assert "Batch CANCELLED." not in first.summary_text()

    # the final selected candidate failed: the batch completed its selection
    second = _batch_outcome(_success(0), _failed_candidate(1, "no legal placement"))
    assert second.headline() == "1 / 2 succeeded; 1 failed"
    assert "stopped on" not in second.headline()
    assert second.durable_paths() == ("C:/out/a.mov",)
    assert "no legal placement" in second.summary_text()

    clean = _batch_outcome(_success(0), _success(1, "C:/out/b.mov"))
    assert clean.headline() == "2 / 2 succeeded"
    assert clean.cancelled is False
    assert "Batch CANCELLED." not in clean.summary_text()


def test_a_failure_and_a_cancellation_together_report_both_truths():
    """Rare, but neither fact may hide the other: the batch was cancelled AND a candidate failed."""
    outcome = _batch_outcome(_failed_candidate(0),
                             kind=rw.RenderOutcomeKind.CANCELLED)
    headline = outcome.headline()
    assert "stopped on candidate 1" in headline
    assert "batch cancelled" in headline
    assert "Batch CANCELLED." in outcome.summary_text()
    # nothing succeeded, so nothing is claimed to have been kept
    assert "Earlier successful output was kept." not in outcome.summary_text()


def test_stopped_with_nothing_failed_and_nothing_cancelled_invents_no_failure():
    """The exact bug R2 fixed, isolated: an early stop with no failed candidate.

    Pre-R2 this returned `stopped on candidate 1` by falling back to `attempted` when its
    `next(... if not o.success)` search found nothing. If a future caller reaches this state, it must
    still not name an innocent candidate.
    """
    outcome = _batch_outcome(_success(0))
    headline = outcome.headline()
    assert "stopped on candidate 1" not in headline, headline
    assert headline == "1 / 2 succeeded; stopped after candidate 1"


# ---------------------------------------------------------------------------
# C3-R1B-b: the N-candidate model, where "a candidate failed" and "the batch
# stopped" became independent facts
# ---------------------------------------------------------------------------


def _local_failure(index, reason="Smart Mix: SFX library folder does not exist"):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=False, status_text=reason,
        outcome_kind=rw.RenderOutcomeKind.CANDIDATE_LOCAL)


def _shared_failure(index, reason="Audio Layers: Voice clip is missing or unreadable"):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=False, status_text=reason, outcome_kind=rw.RenderOutcomeKind.SHARED_FATAL)


def _unknown_failure(index, reason="Error: something broke"):
    return rb.RenderCandidateOutcome(
        candidate_index=index, candidate_master_seed=100 + index, variation_seed=200 + index,
        success=False, status_text=reason, outcome_kind=rw.RenderOutcomeKind.UNKNOWN_FATAL)


def test_four_successes_report_four_of_four():
    outcome = _batch_outcome(*[_success(i, f"C:/out/{i}.mov") for i in range(4)], requested=4)
    assert outcome.headline() == "4 / 4 succeeded"
    assert (outcome.succeeded, outcome.failed, outcome.cancelled_count) == (4, 0, 0)
    assert outcome.attempted == 4 and outcome.not_attempted == 0
    assert outcome.stopped_early is False
    assert outcome.outcome_kind is None
    assert outcome.latest_successful_preview() == "C:/out/3.mov"
    assert "not attempted" not in outcome.summary_text()


def test_one_local_failure_in_a_completed_batch_is_counted_not_blamed():
    """**The headline shape R1B-b exists for.** The batch continued past candidate 1 and finished.

    Saying "stopped on candidate 1" would be false: candidates 2, 3 and 4 all rendered afterwards.
    """
    outcome = _batch_outcome(_local_failure(0), _success(1, "C:/out/b.mov"),
                             _success(2, "C:/out/c.mov"), _success(3, "C:/out/d.mov"),
                             requested=4)
    assert outcome.headline() == "3 / 4 succeeded; 1 failed"
    assert (outcome.succeeded, outcome.failed) == (3, 1)
    assert outcome.attempted == 4 and outcome.not_attempted == 0
    assert outcome.outcome_kind is None, "a completed batch has no terminal cause of its own"
    assert outcome.stopped_early is False
    assert "stopped on" not in outcome.headline()
    summary = outcome.summary_text()
    assert "not attempted" not in summary, "every selected candidate WAS attempted"
    assert "All 4 selected candidates were attempted." in summary, summary
    assert "1 failed" in summary
    assert outcome.durable_paths() == ("C:/out/b.mov", "C:/out/c.mov", "C:/out/d.mov")


def test_two_local_failures_in_a_completed_batch():
    outcome = _batch_outcome(_success(0, "C:/out/a.mov"), _local_failure(1),
                             _success(2, "C:/out/c.mov"), _local_failure(3), requested=4)
    assert outcome.headline() == "2 / 4 succeeded; 2 failed"
    assert (outcome.succeeded, outcome.failed) == (2, 2)
    assert outcome.not_attempted == 0
    assert outcome.outcome_kind is None
    # a trailing local failure must not blank the earlier preview
    assert outcome.latest_successful_preview() == "C:/out/c.mov"


def test_a_local_failure_on_the_final_candidate_completes_the_batch():
    """[§30 case J] Nothing remained to attempt, so this is a completed batch, not a stopped one.

    The failure is reported as a COUNT, not as "stopped on candidate 2" — nothing was stopped.
    """
    outcome = _batch_outcome(_success(0, "C:/out/a.mov"), _local_failure(1), requested=2)
    assert outcome.attempted == 2 and outcome.not_attempted == 0
    assert outcome.outcome_kind is None
    assert outcome.stopped_early is False
    assert outcome.headline() == "1 / 2 succeeded; 1 failed"
    assert "stopped on" not in outcome.headline()
    assert outcome.durable_paths() == ("C:/out/a.mov",)


def test_a_fatal_on_the_final_candidate_also_ends_nothing():
    """Same reasoning for a shared/unknown fatal: there was no remaining work to stop."""
    outcome = _batch_outcome(_success(0, "C:/out/a.mov"), _shared_failure(1), requested=2)
    assert outcome.not_attempted == 0
    assert outcome.headline() == "1 / 2 succeeded; 1 failed"
    assert "stopped on" not in outcome.headline()
    assert "not attempted" not in outcome.summary_text()


@pytest.mark.parametrize("terminal,expected_kind", [
    (_shared_failure, rw.RenderOutcomeKind.SHARED_FATAL),
    (_unknown_failure, rw.RenderOutcomeKind.UNKNOWN_FATAL),
])
def test_a_fatal_after_a_local_failure_names_the_terminal_candidate(terminal, expected_kind):
    """Both facts, neither hiding the other: one failure was continued past, one ended the batch."""
    outcome = _batch_outcome(_local_failure(0), _success(1, "C:/out/b.mov"), terminal(2),
                             requested=4, kind=expected_kind)
    headline = outcome.headline()
    assert "1 / 4 succeeded" in headline
    assert "1 failed" in headline, "the CONTINUED-past failure is counted"
    assert "stopped on candidate 3" in headline, headline
    assert "stopped on candidate 1" not in headline, "candidate 1 did not stop anything"
    assert outcome.failed == 2, "both failures count"
    assert outcome.attempted == 3 and outcome.not_attempted == 1
    assert outcome.outcome_kind is expected_kind
    assert outcome.stopped_early is True
    summary = outcome.summary_text()
    assert "1 candidate not attempted." in summary, summary
    assert "Earlier successful output was kept." in summary
    assert outcome.durable_paths() == ("C:/out/b.mov",), "prior success is never discarded"


def test_the_batch_cause_is_never_candidate_local():
    """A local failure belongs to its candidate. The batch carried out its policy."""
    completed = _batch_outcome(_local_failure(0), _success(1), _success(2), requested=3)
    assert completed.outcome_kind is None
    assert completed.outcome_kind is not rw.RenderOutcomeKind.CANDIDATE_LOCAL
    # and the candidate still owns it
    assert completed.outcomes[0].outcome_kind is rw.RenderOutcomeKind.CANDIDATE_LOCAL


# ---------------------------------------------------------------------------
# C3-R1B-b / R2: the BATCH-level cause DOMAIN is only three members wide
# ---------------------------------------------------------------------------
#
# The field was documented as a batch-terminal cause while the type still accepted any
# `RenderOutcomeKind`. Two members are not batch facts at all, and a model that accepts them is a
# model a future orchestrator can quietly misuse.


@pytest.mark.parametrize("kind", [
    rw.RenderOutcomeKind.SUCCESS,
    rw.RenderOutcomeKind.CANDIDATE_LOCAL,
])
def test_an_invalid_batch_level_cause_is_rejected(kind):
    """`SUCCESS` is a candidate outcome; `CANDIDATE_LOCAL` is the candidate's, and is continued past.

    Neither can terminate a batch, so neither may be stated as one. A finished batch reports counts
    and carries `None`.
    """
    with pytest.raises(ValueError, match="not a batch-level terminal cause"):
        _batch_outcome(_success(0), _success(1), kind=kind)
    # and the rejection does not depend on what the candidates happen to be
    with pytest.raises(ValueError, match="not a batch-level terminal cause"):
        rb.RenderBatchOutcome(requested_count=2, outcomes=(), outcome_kind=kind)


@pytest.mark.parametrize("kind", [
    rw.RenderOutcomeKind.CANCELLED,
    rw.RenderOutcomeKind.SHARED_FATAL,
    rw.RenderOutcomeKind.UNKNOWN_FATAL,
])
def test_every_real_batch_terminal_cause_is_accepted(kind):
    outcome = _batch_outcome(_success(0), requested=4, kind=kind)
    assert outcome.outcome_kind is kind
    assert outcome.stopped_early is True


def test_none_remains_accepted_and_is_what_a_completed_batch_carries():
    outcome = _batch_outcome(_success(0), _success(1), requested=2, kind=None)
    assert outcome.outcome_kind is None
    assert outcome.stopped_early is False


def test_the_cancelled_candidate_derivation_still_works_from_none():
    """The one sound model derivation survives the new domain check, which runs before it."""
    derived = _batch_outcome(_cancelled_candidate(0), requested=2)
    assert derived.outcome_kind is rw.RenderOutcomeKind.CANCELLED
    assert derived.cancelled is True


def test_the_model_derives_no_fatal_cause_from_candidate_records():
    """Whether a fatal candidate ENDED the run depends on whether work remained — the
    orchestrator's knowledge, not the model's. So the model must leave it unstated."""
    for failure in (_shared_failure(1), _unknown_failure(1)):
        unstated = _batch_outcome(_success(0), failure, requested=4)
        assert unstated.outcome_kind is None, \
            "the model invented a batch cause from a candidate record"
    source = _executable_source(_MODULE)
    for derived in ("RenderOutcomeKind.SHARED_FATAL", "RenderOutcomeKind.UNKNOWN_FATAL"):
        assert f'object.__setattr__(self, "outcome_kind", {derived})' not in source


# ---------------------------------------------------------------------------
# C3-R1B-b / R3: a CONTINUED-PAST local failure is never the stop cause
# ---------------------------------------------------------------------------
#
# The last pre-R1B-b assumption in this module read *failure* as *terminal*. Since R1B-b those are
# different things: a CANDIDATE_LOCAL is a real failure the policy CONTINUES past, so it can never
# have ended the run -- whatever arrives afterwards.


def test_a_local_failure_then_a_boundary_cancel_blames_the_cancel_not_the_candidate():
    """[§9 case A] The defect R3 fixes, at the model.

    Candidate 1 failed locally and the batch *would have continued*; the user's Cancel is what
    ended the event. "stopped on candidate 1" was false in exactly one material phrase.
    """
    outcome = _batch_outcome(_local_failure(0), requested=4,
                             kind=rw.RenderOutcomeKind.CANCELLED)
    assert (outcome.succeeded, outcome.failed, outcome.cancelled_count) == (0, 1, 0)
    assert outcome.attempted == 1 and outcome.not_attempted == 3
    headline = outcome.headline()
    assert headline == "0 / 4 succeeded; 1 failed; batch cancelled before candidate 2", headline
    assert "stopped on candidate 1" not in headline, \
        "a local failure the batch would have continued past was blamed for stopping it"
    assert "stopped on" not in headline
    # the failure is still COUNTED and still visible
    assert "FAILED" in outcome.summary_text()
    assert "Batch CANCELLED." in outcome.summary_text()
    assert "3 candidates not attempted." in outcome.summary_text()


def test_two_local_failures_then_a_boundary_cancel_count_both_and_blame_neither():
    """[§9 case B] Scaling the same truth: every local failure is counted, none is blamed."""
    outcome = _batch_outcome(_local_failure(0), _local_failure(1), requested=4,
                             kind=rw.RenderOutcomeKind.CANCELLED)
    headline = outcome.headline()
    assert headline == "0 / 4 succeeded; 2 failed; batch cancelled before candidate 3", headline
    assert outcome.failed == 2
    assert outcome.not_attempted == 2
    for blamed in ("stopped on candidate 1", "stopped on candidate 2", "stopped on"):
        assert blamed not in headline, blamed


def test_a_local_failure_then_a_real_fatal_then_a_cancel_names_only_the_fatal():
    """[§9 case C] The local failure is counted; the fatal is named; the cancel is reported."""
    outcome = _batch_outcome(_local_failure(0), _shared_failure(1), requested=4,
                             kind=rw.RenderOutcomeKind.CANCELLED)
    headline = outcome.headline()
    assert "1 failed" in headline, "the continued-past local failure must still be counted"
    assert "stopped on candidate 2" in headline, headline
    assert "batch cancelled" in headline, headline
    assert "stopped on candidate 1" not in headline, "candidate 1 stopped nothing"
    assert outcome.failed == 2, "both failures count"


def test_a_fatal_plus_a_cancel_still_reports_both_truths():
    """[§9 case D] The dual-truth contract R3 must NOT weaken to make the local case pass.

    An `UNKNOWN_FATAL` genuinely did terminate the remaining work, so naming it is correct even
    though a cancellation also landed.
    """
    outcome = _batch_outcome(_unknown_failure(0), requested=4,
                             kind=rw.RenderOutcomeKind.CANCELLED)
    headline = outcome.headline()
    assert "stopped on candidate 1" in headline, headline
    assert "batch cancelled" in headline, headline
    # the fatal is represented by the stop phrase, so it is not double-counted as "1 failed"
    assert outcome.failed == 1
    assert "1 failed" not in headline, headline


@pytest.mark.parametrize("kind,terminal", [
    (rw.RenderOutcomeKind.SHARED_FATAL, True),
    (rw.RenderOutcomeKind.UNKNOWN_FATAL, True),
    (rw.RenderOutcomeKind.CANDIDATE_LOCAL, False),
    (rw.RenderOutcomeKind.CANCELLED, False),
    (rw.RenderOutcomeKind.SUCCESS, False),
])
def test_only_a_genuinely_terminal_class_can_be_named_as_the_stop(kind, terminal):
    """[§12] Behavioural domain guard over the whole candidate vocabulary.

    `_terminal_candidate()` may name only the two classes the R1B-b policy actually stops on.
    `CANDIDATE_LOCAL` is continued past; `CANCELLED` has its own wording; `SUCCESS` stopped nothing.
    """
    candidate = rb.RenderCandidateOutcome(
        candidate_index=0, candidate_master_seed=100, variation_seed=200,
        success=(kind is rw.RenderOutcomeKind.SUCCESS),
        durable_output_path=("C:/out/a.mov" if kind is rw.RenderOutcomeKind.SUCCESS else ""),
        status_text="x", outcome_kind=kind)
    # `requested=4` with one attempted candidate leaves work outstanding, so the only thing that
    # can disqualify it from being terminal is its CLASS.
    outcome = _batch_outcome(candidate, requested=4)
    named = outcome._terminal_candidate()
    assert (named is not None) is terminal, f"{kind} terminality is {named is not None}"
    assert ("stopped on candidate 1" in outcome.headline()) is terminal, outcome.headline()


def test_the_candidate_terminal_cause_domain_is_exactly_two_members():
    """[§12] Pinned as an exact set: widening it must be a deliberate decision.

    It is the batch-cause domain minus CANCELLED — a cancellation is a batch-level fact with its
    own wording, never a candidate blamed for stopping the run.
    """
    assert rb._CANDIDATE_TERMINAL_CAUSES == frozenset((
        rw.RenderOutcomeKind.SHARED_FATAL,
        rw.RenderOutcomeKind.UNKNOWN_FATAL,
    ))
    assert rw.RenderOutcomeKind.CANDIDATE_LOCAL not in rb._CANDIDATE_TERMINAL_CAUSES
    assert rw.RenderOutcomeKind.CANCELLED not in rb._CANDIDATE_TERMINAL_CAUSES
    assert rw.RenderOutcomeKind.SUCCESS not in rb._CANDIDATE_TERMINAL_CAUSES
    assert rb._CANDIDATE_TERMINAL_CAUSES == (
        rb._BATCH_TERMINAL_CAUSES - {rw.RenderOutcomeKind.CANCELLED})


def test_the_batch_terminal_cause_domain_is_exactly_three_members():
    """Pinned as an exact set, so widening it is a decision someone has to make deliberately."""
    assert rb._BATCH_TERMINAL_CAUSES == frozenset((
        rw.RenderOutcomeKind.CANCELLED,
        rw.RenderOutcomeKind.SHARED_FATAL,
        rw.RenderOutcomeKind.UNKNOWN_FATAL,
    ))
    assert rw.RenderOutcomeKind.SUCCESS not in rb._BATCH_TERMINAL_CAUSES
    assert rw.RenderOutcomeKind.CANDIDATE_LOCAL not in rb._BATCH_TERMINAL_CAUSES
    # the candidate-level field keeps the FULL five-member vocabulary
    for kind in rw.RenderOutcomeKind:
        assert _typed(success=(kind is rw.RenderOutcomeKind.SUCCESS),
                      kind=kind).outcome_kind is kind


def test_cancelled_candidates_are_not_counted_as_failures():
    outcome = _batch_outcome(_success(0, "C:/out/a.mov"), _cancelled_candidate(1), requested=4,
                             kind=rw.RenderOutcomeKind.CANCELLED)
    assert outcome.failed == 0, "a user's Stop is not their render breaking"
    assert outcome.cancelled_count == 1
    assert outcome.succeeded == 1
    assert "failed" not in outcome.headline().lower(), outcome.headline()
    assert "cancelled during candidate 2" in outcome.headline()


@pytest.mark.parametrize("outcomes,requested", [
    ((), 2),
    (("s0",), 2),
    (("s0", "l1", "s2", "s3"), 4),
    (("l0", "l1"), 2),
])
def test_the_counting_invariant_holds(outcomes, requested):
    """succeeded + failed + cancelled == attempted, and not_attempted closes the gap."""
    built = []
    for token in outcomes:
        index = int(token[1])
        built.append({"s": _success, "l": _local_failure, "c": _cancelled_candidate}[token[0]](index))
    outcome = _batch_outcome(*built, requested=requested)
    assert outcome.attempted == len(built) == len(outcome.outcomes)
    assert outcome.succeeded + outcome.failed + outcome.cancelled_count == outcome.attempted
    assert outcome.not_attempted == requested - outcome.attempted
    assert outcome.not_attempted >= 0


@pytest.mark.parametrize("sequence,expected", [
    (("s0:a", "l1", "s2:c"), "c"),      # SUCCESS, LOCAL, SUCCESS -> candidate 3
    (("s0:a", "l1"), "a"),              # SUCCESS, LOCAL          -> candidate 1
    (("l0", "s1:b"), "b"),              # LOCAL, SUCCESS          -> candidate 2
    (("s0:a", "l1", "l2"), "a"),        # two trailing locals     -> candidate 1
])
def test_the_latest_successful_preview_survives_later_local_failures(sequence, expected):
    built = []
    for token in sequence:
        if token.startswith("s"):
            index, name = token[1], token.split(":")[1]
            built.append(_success(int(index), f"C:/out/{name}.mov"))
        else:
            built.append(_local_failure(int(token[1])))
    outcome = _batch_outcome(*built, requested=4)
    assert outcome.latest_successful_preview() == f"C:/out/{expected}.mov"


def test_no_outcome_record_is_fabricated_for_an_unattempted_candidate():
    outcome = _batch_outcome(_success(0), _shared_failure(1), requested=4,
                             kind=rw.RenderOutcomeKind.SHARED_FATAL)
    assert len(outcome.outcomes) == 2, "a record was invented for candidate 3 or 4"
    assert [o.candidate_index for o in outcome.outcomes] == [0, 1]
    assert outcome.not_attempted == 2
    assert "2 candidates not attempted." in outcome.summary_text()


def test_the_batch_outcome_is_still_deepcopy_safe_with_the_batch_level_enum():
    outcome = _batch_outcome(_success(0), kind=rw.RenderOutcomeKind.CANCELLED)
    clone = copy.deepcopy(outcome)
    assert clone == outcome
    assert clone.outcome_kind is rw.RenderOutcomeKind.CANCELLED
    assert clone.headline() == outcome.headline()
    with pytest.raises(Exception):
        outcome.outcome_kind = rw.RenderOutcomeKind.SUCCESS


def test_not_attempted_never_goes_negative():
    outcome = _batch_outcome(_success(0), _success(1), requested=1)
    assert outcome.not_attempted == 0


def test_the_outcome_record_is_still_deepcopy_safe_with_the_enum():
    """It crosses a Gradio event boundary; an `Enum` member is the only non-scalar allowed."""
    cancelled = _typed(success=False, kind=rw.RenderOutcomeKind.CANCELLED)
    clone = copy.deepcopy(cancelled)
    assert clone == cancelled
    # Enum identity survives deepcopy, which is what makes `is` comparisons safe downstream.
    assert clone.outcome_kind is rw.RenderOutcomeKind.CANCELLED
    with pytest.raises(Exception):
        cancelled.outcome_kind = rw.RenderOutcomeKind.SUCCESS


def test_the_public_surface_is_explicit():
    assert set(rb.__all__) <= set(dir(rb))
    for name in ("RENDER_SELECTION_MIN", "RENDER_SELECTION_MAX",
                 "RenderBatchRequest", "RenderBatchOutcome",
                 "RenderCandidateRequest", "RenderCandidateOutcome",
                 "build_request", "candidate_output_stem", "normalize_selection"):
        assert name in rb.__all__
    # [C3-R1B-b] and the retired exact-size constant is not re-exported under any guise
    assert "RENDER_SELECTION_SIZE" not in rb.__all__


def test_variant_batch_is_untouched_by_this_feature():
    """The dependency is one-way, and `variant_batch` keeps its own render ban at full strength."""
    variant = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_batch.py")
    source = _executable_source(variant).lower()
    for forbidden in ("render_batch", "renderbatch", "render_selected", "output_stem"):
        assert forbidden not in source, f"variant_batch learned about rendering: {forbidden!r}"
