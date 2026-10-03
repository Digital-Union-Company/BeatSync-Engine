"""C3-R0: the pure model behind rendering exactly two compared candidates.

`beatsync_fork/render_batch.py` is stdlib-only, so it is imported and exercised as ordinary code.
Four properties carry this feature and each has a section:

1. **Exactly two, in canonical ascending index order.** The selection contract is the whole safety
   story — with no cancellation, a batch is an unbreakable commitment, so it must be a commitment
   the user explicitly and unambiguously made.
2. **Candidate identity cannot rest on the Variation Seed.** C3 deduplicates candidate *masters*
   and says nothing about `CreativeRecipe.seed`; collisions are real, and the existing render path
   names its output `_seed<VariationSeed>`.
3. **Fail fast, preserve prior success.** A failed candidate stops the batch and deletes nothing.
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
# 1. SELECTION — exactly two, canonical order
# ===========================================================================


def test_the_render_selection_size_is_two_and_is_not_the_comparison_bound():
    assert rb.RENDER_SELECTION_SIZE == 2
    assert rb.RENDER_SELECTION_SIZE != fork_batch.CANDIDATE_COUNT_MAX
    # the two numbers answer different questions and must not learn about each other
    source = _executable_source(_MODULE)
    assert "CANDIDATE_COUNT_MAX" not in source
    assert "variant_batch" not in source


@pytest.mark.parametrize("selection,expected", [
    ([0, 1], (0, 1)),
    ([1, 0], (0, 1)),          # tick order must not decide render order
    ([4, 2], (2, 4)),
    ((3, 0), (0, 3)),
])
def test_a_valid_selection_is_canonically_ascending(selection, expected):
    assert rb.normalize_selection(selection, 5) == expected


@pytest.mark.parametrize("selection", [
    [], [0], [0, 1, 2], [0, 1, 2, 3],          # wrong counts
    [0, 0], [3, 3],                             # duplicates
    [0, 5], [-1, 0], [0, 99],                   # out of range
    [True, 1], [0, False],                      # bool subclasses int
    [0, 1.0], [0, "1"], ["0", "1"],             # wrong types
    None, 0, "01", b"01", {0: 1},               # not a usable sequence
])
def test_an_unusable_selection_is_refused_and_never_raises(selection):
    assert rb.normalize_selection(selection, 5) == ()


def test_the_refusal_text_names_the_actual_problem():
    assert "Generate Variants" in rb.describe_selection_refusal([0, 1], 0)
    assert "exactly 2" in rb.describe_selection_refusal([0], 5)
    assert "exactly 2" in rb.describe_selection_refusal([0, 1, 2], 5)


def test_build_request_refuses_rather_than_guessing():
    for bad in ([], [0], [0, 1, 2], [0, 0], None, "01"):
        request, refusal = _request(selection=bad) if isinstance(bad, (list, tuple)) else \
            rb.build_request(_batch(), bad, user_base="x", request_tag=TAG)
        assert request is None
        assert refusal


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
    request, _ = _request()
    one = request.candidates[:1]
    with pytest.raises(ValueError):
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1, candidates=one)
    with pytest.raises(ValueError):
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=(request.candidates[1], request.candidates[0]))
    with pytest.raises(ValueError):
        rb.RenderBatchRequest(request_tag=TAG, batch_root_master=1,
                              candidates=(request.candidates[0], request.candidates[0]))


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
    assert not outcome.stopped_on_failure


def test_a_first_candidate_failure_stops_after_one_attempt():
    outcome = rb.RenderBatchOutcome(requested_count=2, outcomes=(
        _outcome(0, 111, 11, False, status="FFmpeg extraction failed"),
    ), stopped_on_failure=True)
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
    ), stopped_on_failure=True)
    assert outcome.succeeded == 1
    assert outcome.headline() == "1 / 2 succeeded; stopped on candidate 2"
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
    ), stopped_on_failure=True)
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


def test_the_module_imports_only_stdlib():
    with open(_MODULE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "collections", "dataclasses", "typing"}, sorted(imported)


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


def test_no_cancellation_machinery_exists():
    """C3-R0 ships no Stop control; a fake one would be worse than none."""
    source = _executable_source(_MODULE).lower()
    for forbidden in ("cancel", "stop_flag", "stop_event", "threading", "terminate", "abort"):
        assert forbidden not in source, f"render_batch references {forbidden!r}"


def test_the_public_surface_is_explicit():
    assert set(rb.__all__) <= set(dir(rb))
    for name in ("RENDER_SELECTION_SIZE", "RenderBatchRequest", "RenderBatchOutcome",
                 "RenderCandidateRequest", "RenderCandidateOutcome",
                 "build_request", "candidate_output_stem", "normalize_selection"):
        assert name in rb.__all__


def test_variant_batch_is_untouched_by_this_feature():
    """The dependency is one-way, and `variant_batch` keeps its own render ban at full strength."""
    variant = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_batch.py")
    source = _executable_source(variant).lower()
    for forbidden in ("render_batch", "renderbatch", "render_selected", "output_stem"):
        assert forbidden not in source, f"variant_batch learned about rendering: {forbidden!r}"
