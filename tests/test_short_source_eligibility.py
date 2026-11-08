"""Short-Source Eligibility R1: a planned segment may only be given a source that can fill it.

The field defect this closes, reported 2026-11-08 and reproduced here as pure data: a 5.133-second
source was allocated a 5.280-second frame-locked slot at 25 fps, so the extraction worker refused
128 of the 132 output frames the timeline had committed to, and the render stopped with three failed
clips rather than publishing a desynchronised timeline.

Three things had to be true at once for that to happen, and each gets its own section below:

1. ``_adjusted_score``'s only duration term is a **penalty below 55 %** of the slot. 5.133 / 5.280 is
   97 %, so the planner had no signal at all — see :func:`test_the_legacy_penalty_is_silent_at_the_field_ratio`.
2. That term reads ``candidate["duration"]``, which is the Stage-5 analysis **window** and is capped,
   not the length of the file. Above the cap every candidate in any library reads as short; just
   below it a file barely longer than its window reads as unlimited. The file length was on every
   candidate all along as ``video_duration`` and was never consulted.
3. ``_materialize_clip`` then inflates the shortfall away with ``max(source_duration,
   video_duration)``, so the plan records a window the file does not contain.

**What is asserted and what is not.** Everything here is synthetic and media-free: no FFmpeg, no
probe, no file. The frame counts are therefore ``round(seconds * fps)`` **estimates**, exactly as the
planner's own guarantee is — they are not decoded frame counts, and nothing in this file proves a
rendered video plays correctly. The worker's pre-extraction guard, which does probe the file, remains
the authority and is asserted to still be there.

The planner needs numpy, so it is loaded by path and skips cleanly where numpy is absent; it has no
relative imports, so this does not drag in ``auto_mode/__init__`` (librosa, cupy, logger).
``video_processor.py`` cannot be imported here at all and is read with ``ast``.
"""

from __future__ import annotations

import ast
import importlib.util
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")
_VP_PATH = os.path.join(_REPO_ROOT, "src", "video_processor.py")


@pytest.fixture(scope="module")
def planner():
    pytest.importorskip("numpy", reason="the Stage 6 planner is numpy-based")
    spec = importlib.util.spec_from_file_location("stage6_planner_short_source", _PLANNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures: a library whose files differ in LENGTH, with realistic capped windows
# ---------------------------------------------------------------------------

#: Stage 5 caps an analysis window, so a long file's window is short while a short file's window is
#: the whole file. Both halves of that asymmetry matter, so the pool carries both.
_WINDOW_CAP = 5.2


def _candidate(cid, file_seconds, *, quality=0.6, motion=0.5, window=None, start=0.0, **extra):
    span = min(_WINDOW_CAP, file_seconds) if window is None else window
    candidate = {
        "id": cid,
        "video_file": f"C:/library/{cid}.mp4",
        "source_name": f"{cid}.mp4",
        "video_duration": file_seconds,
        "duration": span,
        "start": start,
        "center": start + span / 2.0,
        "peak_time": start + span * 0.6,
        "quality": quality,
        "motion": motion,
        "brightness": 0.5,
        "scene_index": 0,
        "tags": ["action"],
        "ai_analyzed": True,
    }
    candidate.update(extra)
    return candidate


def _pool(file_lengths):
    """One candidate per source length, the shortest file deliberately the most attractive."""
    count = max(1, len(file_lengths) - 1)
    return [
        _candidate(
            f"src{position:02d}",
            seconds,
            quality=0.95 - 0.70 * (position / count),
            motion=0.90 - 0.70 * (position / count),
        )
        for position, seconds in enumerate(file_lengths)
    ]


def _beat_info(candidates, seed=None, output_fps=None):
    info = {
        "times": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        "sections": [{"type": "verse", "start": 0.0, "end": 30.0, "energy": 0.5}],
        "energy_profile": {"wave": [0.4] * 7, "arc": [0.4] * 7},
        "rhythm_data": {
            "impact_strength": [0.4] * 7,
            "combined_strength": [0.4] * 7,
            "novelty_strength": [0.3] * 7,
        },
        "video_analysis": {"candidates": candidates},
    }
    if seed is not None:
        info["creative"] = {"seed": seed}
    if output_fps is not None:
        info["output_fps"] = output_fps
    return info


def _timeline(durations):
    cuts = [0.0]
    for duration in durations:
        cuts.append(cuts[-1] + duration)
    return cuts


def _plan(planner, candidates, durations, *, seed=None, output_fps=None, on_bus=False):
    bus_fps = output_fps if on_bus else None
    return planner.build_planned_clip_sequence(
        cut_times=_timeline(durations),
        segment_durations=durations,
        beat_info=_beat_info(candidates, seed=seed, output_fps=bus_fps),
        video_files=["C:/library/src00.mp4"],
        **({} if on_bus else {"output_fps": output_fps}),
    )


def _profile(duration, *, index=0, target="flow"):
    start = 20.0 + index
    return {
        "duration": duration,
        "start": start,
        "end": start + duration,
        "target": target,
        "wave": 0.5,
        "impact": 0.5,
        "section_type": "verse",
    }


def _pick(planner, candidates, profile, *, index=0, seed=0, output_fps=None):
    from collections import Counter, deque

    return planner._choose_candidate(
        candidates=candidates,
        profile=profile,
        recent_ids=deque(maxlen=10),
        recent_videos=deque(maxlen=5),
        usage=Counter(),
        index=index,
        seed=seed,
        output_fps=output_fps,
    )


def _frames(seconds, fps):
    return int(round(seconds * fps))


def _tree(path):
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree, name):
    return next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name)


# ===========================================================================
# 1. THE FIELD DEFECT, REPRODUCED AS PURE DATA
# ===========================================================================

#: The reported incident, to the millisecond. `424.mp4` was 5.133 s; its slot was 5.280 s at 25 fps.
_FIELD_SOURCE_SECONDS = 5.133
_FIELD_SLOT_SECONDS = 5.280
_FIELD_FPS = 25.0


def test_the_reported_arithmetic_is_exactly_132_against_128():
    """The deficit the worker reported is four output frames, from durations alone."""
    assert _frames(_FIELD_SLOT_SECONDS, _FIELD_FPS) == 132
    assert _frames(_FIELD_SOURCE_SECONDS, _FIELD_FPS) == 128
    assert _frames(_FIELD_SLOT_SECONDS, _FIELD_FPS) - _frames(
        _FIELD_SOURCE_SECONDS, _FIELD_FPS) == 4


def test_the_legacy_penalty_is_silent_at_the_field_ratio(planner):
    """**Why the planner never saw it.** The only duration term fires below 55 % of the slot.

    5.133 / 5.280 = 97.2 %, so the score is untouched — the short source was not merely preferred
    despite a penalty, it was never penalised at all.
    """
    from collections import Counter, deque

    short = _candidate("short", _FIELD_SOURCE_SECONDS)
    profile = _profile(_FIELD_SLOT_SECONDS)
    scored = planner._adjusted_score(
        short, profile, deque(maxlen=10), deque(maxlen=5), Counter())
    unpenalised = planner._score_candidate(short, profile)

    assert _FIELD_SOURCE_SECONDS >= _FIELD_SLOT_SECONDS * 0.55
    assert scored == pytest.approx(unpenalised), "the 55 % term must be silent here"

    # and it is a real term, not a dead one: a window below the threshold does cost 0.18
    starved = _candidate("starved", _FIELD_SOURCE_SECONDS, window=2.0)
    assert planner._adjusted_score(
        starved, profile, deque(maxlen=10), deque(maxlen=5), Counter()
    ) == pytest.approx(planner._score_candidate(starved, profile) - 0.18)


def test_the_planner_no_longer_allocates_the_short_source(planner):
    """The headline behaviour: a longer file wins the field segment at every seed."""
    short = _candidate("short", _FIELD_SOURCE_SECONDS, quality=0.95, motion=0.95)
    longer = _candidate("longer", 40.0, quality=0.20, motion=0.20)
    profile = _profile(_FIELD_SLOT_SECONDS, index=4)

    for seed in (0, 381944, 101, 4242):
        chosen = _pick(planner, [short, longer], profile,
                       index=4, seed=seed, output_fps=_FIELD_FPS)
        assert chosen is longer, seed
        # ...and the unguarded planner is free to choose either, which is the defect
        assert _pick(planner, [short, longer], profile, index=4, seed=seed) in (short, longer)


def test_the_materialised_plan_no_longer_asserts_a_window_the_file_lacks(planner):
    """``_materialize_clip``'s ``max()`` is what masked the shortfall; nothing reaches it now."""
    short = _candidate("short", _FIELD_SOURCE_SECONDS, quality=0.99, motion=0.99)
    longer = _candidate("longer", 40.0, quality=0.10, motion=0.10)
    durations = [_FIELD_SLOT_SECONDS] * 4

    plan = _plan(planner, [short, longer], durations, seed=101, output_fps=_FIELD_FPS)

    assert len(plan) == len(durations)
    for clip in plan:
        source = next(c for c in (short, longer) if c["video_file"] == clip["video_file"])
        assert clip["start_time"] + clip["source_duration"] <= source["video_duration"] + 1e-9
        assert _frames(source["video_duration"], _FIELD_FPS) >= _frames(
            clip["final_duration"], _FIELD_FPS)

    # the masking expression is still the one being compensated for, stated as the record of WHY
    # this guarantee has to live in selection rather than in materialisation
    body = ast.unparse(_func(_tree(_PLANNER_PATH), "_materialize_clip"))
    assert "max(source_duration, float(candidate.get('video_duration'" in body


# ===========================================================================
# 2. THE WINDOW IS NOT THE FILE
# ===========================================================================


def test_capacity_reads_the_file_length_and_never_the_analysis_window(planner):
    """A 60-second file whose window is capped at 5.2 s can still fill an 8-second slot.

    This is the half of the defect a naive "penalise short candidates harder" fix would get wrong:
    above the window cap *every* candidate looks short, so such a fix refuses every render whose
    segments exceed the cap, however long the actual sources are.
    """
    capped = _candidate("capped", 60.0, window=_WINDOW_CAP)
    assert capped["duration"] == _WINDOW_CAP < 8.0

    assert planner._source_frames(capped, 25.0) == _frames(60.0, 25.0)
    assert planner._can_fill_segment(capped, _profile(8.0), 25.0) is True

    plan = _plan(planner, [capped], [8.0, 8.0], output_fps=25.0)
    assert [clip["source_name"] for clip in plan] == ["capped.mp4", "capped.mp4"]


def test_a_file_barely_longer_than_its_window_is_still_refused(planner):
    """The other direction: an uncapped 5.133 s window says nothing about filling 5.28 s."""
    short = _candidate("short", _FIELD_SOURCE_SECONDS)
    assert short["duration"] == pytest.approx(_FIELD_SOURCE_SECONDS)  # NOT capped

    assert planner._can_fill_segment(short, _profile(_FIELD_SLOT_SECONDS), _FIELD_FPS) is False
    assert planner._can_fill_segment(short, _profile(5.00), _FIELD_FPS) is True


def test_the_slot_is_measured_the_way_the_plan_will_record_it(planner):
    """``_segment_frames`` mirrors ``_materialize_clip``'s ``final_duration`` floor exactly."""
    assert planner._segment_frames(_profile(_FIELD_SLOT_SECONDS), _FIELD_FPS) == 132
    # the 0.05 floor is shared, so a degenerate segment cannot ask for zero frames
    assert planner._segment_frames(_profile(0.0), _FIELD_FPS) == _frames(0.05, _FIELD_FPS)
    assert planner._segment_frames(_profile(-3.0), _FIELD_FPS) == _frames(0.05, _FIELD_FPS)


@pytest.mark.parametrize("fps", [25.0, 30.0, 23.976, 60.0])
def test_eligibility_is_decided_at_the_real_output_rate(planner, fps):
    """Not a hardcoded 25 or 30: the same pair flips with the rate when the frames do."""
    source = _candidate("edge", 4.0)
    for slot in (3.0, 3.9, 4.0, 4.1, 5.0):
        expected = _frames(4.0, fps) >= _frames(slot, fps)
        assert planner._can_fill_segment(source, _profile(slot), fps) is expected


# ===========================================================================
# 3. THE RNG AND THE SCORING OF ALREADY-VIABLE RENDERS ARE PRESERVED
# ===========================================================================

_MIXED_LENGTHS = [2.4, 3.1, 4.05, 5.133, 5.28, 6.0, 7.5, 9.25, 12.0, 20.0, 40.0, 61.7]
_MIXED_DURATIONS = [1.0, 1.44, 1.92, 2.4, 2.88, 3.36, 3.84, 4.32, 4.8, 5.28, 5.76, 6.24]


@pytest.mark.parametrize("seed", [0, 381944, 101, 4242])
@pytest.mark.parametrize("fps", [25.0, 30.0])
def test_an_eligible_winner_is_never_taken_away_from_it(planner, seed, fps):
    """**The preservation guarantee, measured.** Where the unguarded planner already chose a source
    that can fill the slot, the guarded planner chooses the identical candidate — same object.

    This is the property that makes the fix safe to ship: a render that works today keeps its exact
    edit. It holds because an ineligible candidate is denied the *win*, never the *draw* — see
    :func:`test_the_legacy_branch_draws_before_it_judges` for the structural half.
    """
    pool = _pool(_MIXED_LENGTHS)
    changed = kept = repaired = 0
    for index, duration in enumerate(_MIXED_DURATIONS):
        profile = _profile(duration, index=index)
        legacy = _pick(planner, pool, profile, index=index, seed=seed)
        guarded = _pick(planner, pool, profile, index=index, seed=seed, output_fps=fps)

        assert guarded is None or planner._can_fill_segment(guarded, profile, fps), index
        if planner._can_fill_segment(legacy, profile, fps):
            kept += guarded is legacy
            changed += guarded is not legacy
        else:
            repaired += guarded is not None

    assert changed == 0, f"{changed} already-viable segments changed winner"
    assert kept + repaired == len(_MIXED_DURATIONS)
    assert repaired, "this pool must actually exercise the repair, or it proves only inertness"


@pytest.mark.parametrize("seed", [0, 381944, 101, 4242])
def test_the_guarantee_is_inert_when_every_source_is_long_enough(planner, seed):
    """An all-eligible render must be bit-identical with and without the guarantee."""
    pool = _pool([40.0, 48.0, 55.0, 61.7, 70.0, 90.0])

    unguarded = _plan(planner, pool, _MIXED_DURATIONS, seed=seed)
    guarded = _plan(planner, pool, _MIXED_DURATIONS, seed=seed, output_fps=25.0)

    assert unguarded == guarded


@pytest.mark.parametrize("seed", [0, 381944])
def test_the_bus_and_the_argument_are_the_same_authority(planner, seed):
    """The renderer publishes the rate on ``beat_info``; a caller may pass it directly. Same plan."""
    pool = _pool(_MIXED_LENGTHS)
    direct = _plan(planner, pool, _MIXED_DURATIONS, seed=seed, output_fps=25.0)
    on_bus = _plan(planner, pool, _MIXED_DURATIONS, seed=seed, output_fps=25.0, on_bus=True)

    assert direct == on_bus
    # the explicit argument wins over the bus, so a caller can always override it
    assert planner.output_frame_rate({"output_fps": 30.0}, 25.0) == 25.0
    assert planner.output_frame_rate({"output_fps": 30.0}) == 30.0


def test_the_legacy_branch_draws_before_it_judges(planner):
    """Structural: the jitter draw must precede the eligibility ``continue``, not follow it.

    If the order were reversed, an ineligible candidate would stop consuming its draw and every
    later candidate in that segment would receive a different jitter value — which is exactly how a
    pre-filter silently re-edits renders that were already fine.
    """
    branch = ast.unparse(_func(_tree(_PLANNER_PATH), "_choose_candidate"))
    loop_body = branch[branch.index("for position, candidate in enumerate(candidates)"):]
    draw = loop_body.index("rng.random()")
    judge = loop_body.index("_can_fill_segment")

    assert draw < judge, "eligibility is consulted after the per-candidate draw, never before"


def test_the_seeded_branch_draws_against_the_unchanged_score_list(planner):
    """Structural: ``select_index`` sees every candidate's score first; only a losing winner is
    re-drawn, from the same ``rng``. No reshaping of the field before the first draw."""
    whole = ast.unparse(_func(_tree(_PLANNER_PATH), "_choose_candidate"))
    # the legacy branch has its own `_can_fill_segment`, so the seeded half must be isolated first
    seeded = whole[whole.index("return best_candidate") + len("return best_candidate"):]

    assert seeded.index("select_index(scores, rng)") < seeded.index("_can_fill_segment")
    assert whole.count("_stable_rng(seed") == 1, "no second RNG was seeded for the re-draw"
    assert "select_index([scores[position] for position in eligible], rng)" in seeded
    # the re-draw reuses the same generator object: `rng` is bound exactly once in this branch
    assert seeded.count("rng = ") == 1


def test_no_new_import_or_module_level_state_was_added():
    """The planner's dependency surface and its statelessness are both unchanged."""
    tree = _tree(_PLANNER_PATH)
    imports = set()
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            imports.update(alias.name for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            imports.update(f"{item.module}.{alias.name}" for alias in item.names)

    assert imports == {
        "hashlib", "random", "numpy",
        "__future__.annotations",
        "collections.Counter", "collections.deque",
        "typing.Dict", "typing.List", "typing.Sequence",
        "beatsync_fork.variation", "beatsync_fork.creative",
        "beatsync_fork.deterministic_view", "beatsync_fork.freestyle",
    }, sorted(imports)
    assert not {target.id for node in tree.body if isinstance(node, ast.Assign)
                for target in node.targets if isinstance(target, ast.Name)}


def test_the_dynamic_scorer_gained_no_penalty(planner):
    """The guarantee is a disqualification, not a tenth reuse penalty: still nine ``score -=``."""
    source = ast.unparse(_func(_tree(_PLANNER_PATH), "_adjusted_score"))

    assert source.count("score -=") == 9
    assert "_can_fill_segment" not in source
    assert "video_duration" not in source


# ===========================================================================
# 4. NO ELIGIBLE SOURCE IS AN ACTIONABLE REFUSAL, NEVER A SILENT FALLBACK
# ===========================================================================


def test_an_unfillable_segment_refuses_before_any_worker(planner):
    """The whole point: stop, with a message the operator can act on."""
    pool = _pool([2.0, 2.2, 2.4, 2.6])

    with pytest.raises(planner.SegmentSourceCapacityError) as caught:
        _plan(planner, pool, [6.0, 6.0, 6.0], seed=101, output_fps=25.0)

    message = str(caught.value)
    assert "Segment 1 (index 0)" in message          # the segment, 1-based and 0-based
    assert "150 output frames" in message            # what it needed
    assert "6.000s at 25 fps" in message             # in seconds, at the real rate
    assert "65 frames" in message                    # the best capacity actually available
    assert "short by 85" in message                  # the shortfall
    assert "not decoded frame counts" in message     # the estimate is labelled as one
    assert "Cut Density" in message                  # and what to do about it


def test_the_refusal_names_the_offending_segment_not_merely_the_first(planner):
    """A later segment is reported as itself, so the operator knows which cut is impossible."""
    pool = [_candidate("ok", 40.0, quality=0.2), _candidate("tiny", 1.0, quality=0.9)]

    with pytest.raises(planner.SegmentSourceCapacityError) as caught:
        _plan(planner, pool, [2.0, 2.0, 90.0], seed=0, output_fps=25.0)

    assert "Segment 3 (index 2)" in str(caught.value)


def test_a_refusal_is_raised_rather_than_shortening_the_plan(planner):
    """The hazard a naive fix walks into.

    ``build_planned_clip_sequence`` discards the **whole** plan when it is shorter than the
    timeline, and the renderer then samples sources at random with no view of their length. So
    dropping an unfillable segment would be strictly worse than today: it would replace one honest
    worker refusal with a duration-blind plan for the entire video.
    """
    body = ast.unparse(_func(_tree(_PLANNER_PATH), "build_planned_clip_sequence"))

    assert "raise SegmentSourceCapacityError" in body
    # the length check that makes a short plan fatal is still there, and is what is being avoided
    assert "if len(planned) != len(durations_arr)" in body
    # the refusal is an ordinary exception, so no broad handler has to learn a new type to report
    # it and no caller can mistake it for a cancellation
    assert issubclass(planner.SegmentSourceCapacityError, RuntimeError)
    assert issubclass(planner.SegmentSourceCapacityError, Exception)


def test_an_empty_library_still_falls_back_exactly_as_before(planner):
    """No candidates at all is not a capacity problem, and must not become a refusal."""
    assert _plan(planner, [], [6.0, 6.0], output_fps=25.0) == []
    assert _plan(planner, [], [6.0, 6.0], output_fps=25.0, on_bus=True) == []
    # a candidate without a video_file is dropped before scoring, as it always was
    assert _plan(planner, [_candidate("nofile", 40.0, video_file=None)], [6.0],
                 output_fps=25.0) == []


def test_without_a_rate_the_planner_is_untouched_even_when_sources_are_short(planner):
    """No frame-rate authority means no claim: the legacy plan is produced, not a refusal."""
    pool = _pool([2.0, 2.2, 2.4])

    plan = _plan(planner, pool, [6.0, 6.0], seed=101)

    assert len(plan) == 2, "the unguarded planner still plans impossible segments"


# ===========================================================================
# 5. UNMEASURED IS NOT SHORT, AND AN UNUSABLE RATE FAILS CLOSED
# ===========================================================================


#: Values a record can carry that say nothing usable about a file's length. Split by what the
#: PRE-EXISTING ``_materialize_clip`` can survive, which is a different question from what the new
#: eligibility layer can read — see
#: :func:`test_a_non_numeric_file_length_still_breaks_materialisation_as_it_always_did`.
_UNUSABLE_NUMERIC = [None, 0.0, -1.0, float("nan"), float("inf"), float("-inf")]
_UNUSABLE_NON_NUMERIC = ["", "abc", [], {}]


@pytest.mark.parametrize("value", _UNUSABLE_NUMERIC + _UNUSABLE_NON_NUMERIC)
def test_an_unmeasurable_file_length_is_never_a_refusal(planner, value):
    """R10's rule, reused: an unmeasured count must never become a refusal.

    ``get_video_duration`` returns a 10.0-second fallback when its probe fails, so an absent or
    absurd ``video_duration`` says nothing trustworthy in either direction. The planner declines to
    judge, and the worker — which actually probes the file — decides.
    """
    unknown = _candidate("unknown", 40.0)
    unknown["video_duration"] = value
    if value is None:
        del unknown["video_duration"]

    assert planner._source_frames(unknown, 25.0) is None
    assert planner._can_fill_segment(unknown, _profile(90.0), 25.0) is True


@pytest.mark.parametrize("value", _UNUSABLE_NUMERIC)
def test_an_unmeasurable_length_plans_rather_than_refusing(planner, value):
    """End to end: the segment is planned, not refused, and the worker will decide."""
    unknown = _candidate("unknown", 40.0)
    unknown["video_duration"] = value
    if value is None:
        del unknown["video_duration"]

    assert len(_plan(planner, [unknown], [90.0], output_fps=25.0)) == 1


@pytest.mark.parametrize("value", _UNUSABLE_NON_NUMERIC)
def test_a_non_numeric_file_length_still_breaks_materialisation_as_it_always_did(planner, value):
    """**A pre-existing defect, recorded rather than quietly widened into this change.**

    ``_materialize_clip`` coerces ``candidate["video_duration"]`` with a bare ``float()``, so a
    record carrying a string, a list or a dict there raises ``TypeError``/``ValueError`` during
    planning. That is true on pristine ``main`` and is untouched here: it is a malformed-record
    defect, not a source-capacity one, and fixing it is outside this change's authorized scope.

    Pinned so the finding cannot be lost, and so that whoever does fix it has to delete this test
    deliberately rather than discover the behaviour again.
    """
    unknown = _candidate("unknown", 40.0)
    unknown["video_duration"] = value

    # the new eligibility layer is NOT the thing that breaks: it reads the same field safely
    assert planner._can_fill_segment(unknown, _profile(90.0), 25.0) is True

    with pytest.raises((TypeError, ValueError)):
        _plan(planner, [unknown], [90.0], output_fps=25.0)
    # ...and identically with no frame-rate authority at all, which is what makes it pre-existing
    with pytest.raises((TypeError, ValueError)):
        _plan(planner, [unknown], [90.0])


def test_an_unmeasurable_length_is_excluded_from_the_reported_best_capacity(planner):
    """It is eligible, so a pool containing one can never reach a refusal at all."""
    pool = [_candidate("tiny", 1.0), _candidate("unknown", 40.0)]
    del pool[1]["video_duration"]

    plan = _plan(planner, pool, [90.0], seed=0, output_fps=25.0)

    assert [clip["source_name"] for clip in plan] == ["unknown.mp4"]


@pytest.mark.parametrize("rate", ["abc", [], {}, object()])
def test_an_unparseable_rate_fails_closed(planner, rate):
    with pytest.raises(ValueError, match="not a number"):
        planner.output_frame_rate(None, rate)


@pytest.mark.parametrize("rate", [0, 0.0, -1.0, -25.0, float("nan"), float("inf"),
                                   float("-inf")])
def test_an_unusable_rate_fails_closed(planner, rate):
    """A guarantee in output frames cannot be stated against a rate that is not one."""
    with pytest.raises(ValueError, match="not a usable positive frame rate"):
        planner.output_frame_rate(None, rate)
    with pytest.raises(ValueError, match="not a usable positive frame rate"):
        planner.output_frame_rate({"output_fps": rate})


def test_an_absent_rate_is_absence_not_failure(planner):
    """Absent means "the guarantee was not requested", which is every legacy caller."""
    assert planner.output_frame_rate(None) is None
    assert planner.output_frame_rate({}) is None
    assert planner.output_frame_rate({"output_fps": None}) is None
    assert planner.output_frame_rate("not a dict") is None
    assert planner.output_frame_rate({"output_fps": "25"}) == 25.0


# ===========================================================================
# 6. THE RENDERER'S OWN GUARDS ARE THE BACKSTOP AND ARE UNCHANGED
# ===========================================================================


def test_the_renderer_publishes_the_effective_rate_it_actually_used():
    """The planner's rate must be the frame-locked timeline's rate, not a second opinion."""
    body = ast.unparse(_func(_tree(_VP_PATH), "create_music_video"))

    assert "beat_info['output_fps'] = float(fps)" in body
    # the same `fps` builds the frame-locked timeline and is reported on the stage-6 event
    assert "build_frame_aligned_cut_timeline(beat_times, audio_duration, fps)" in body
    assert "output_fps=float(fps)" in body
    # published before the planner runs, or the planner would read a stale bus
    assert body.index("beat_info['output_fps']") < body.index("build_planned_clip_sequence(")


def test_the_pinned_planner_call_bundle_is_deliberately_unchanged():
    """Three suites pin this call's keywords by exact equality, which is why the rate travels on
    the bus. Asserted here too, so the reason survives alongside the thing it explains."""
    calls = [node for node in ast.walk(_func(_tree(_VP_PATH), "create_music_video"))
             if isinstance(node, ast.Call)
             and getattr(node.func, "id", None) == "build_planned_clip_sequence"]

    assert len(calls) == 1
    assert [keyword.arg for keyword in calls[0].keywords] == [
        "cut_times", "segment_durations", "beat_info", "video_files"]
    assert not calls[0].args


def test_the_workers_pre_extraction_guard_is_still_the_authority():
    """A stale or fallback ``video_duration`` must still be caught, so this guard stays."""
    body = ast.unparse(_func(_tree(_VP_PATH), "create_clip_parallel"))

    assert "available_frames < planned_frames" in body
    assert "planned_frames = int(round(float(final_duration) * float(fps)))" in body
    assert "available_frames = int(round(float(source_duration) * float(fps)))" in body
    assert "Source is too short for its planned segment" in body
    # it still clamps to the PROBED duration rather than trusting the plan
    assert "source_duration = max(0.05, min(source_duration, video_duration))" in body


def test_nothing_pads_loops_stretches_or_truncates_a_source():
    """A refusal, never a substituted frame. The frame-locked timeline is untouched."""
    planner_source = open(_PLANNER_PATH, "r", encoding="utf-8").read()
    # the FFmpeg primitives that would manufacture a missing frame. Deliberately not the bare word
    # "loop": the planner's own comments legitimately describe its candidate loop.
    for forbidden in ("stream_loop", "tpad", "setpts", "atempo", "concat", "trim="):
        assert forbidden not in planner_source, forbidden

    timeline = ast.unparse(_func(_tree(_VP_PATH), "build_frame_aligned_cut_timeline"))
    assert "np.rint" in timeline
    assert "_can_fill_segment" not in timeline
    assert "video_duration" not in timeline
