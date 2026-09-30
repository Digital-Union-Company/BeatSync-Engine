"""The four controls together: Stage 4 cuts feeding Stage 6 planning.

Cut Density changes the cut timeline; Energy Response and Motion Bias change how the planner scores
candidates on whatever timeline it is given; the seed decides the winner. They are owned by different
stages and must compose without interfering — so this runs the real Stage 4 selector, derives the
segment timeline from its output the way ``build_frame_aligned_cut_timeline`` does, and plans on it.

A deliberately small grid, not a combinatorial explosion: the per-control behaviour is measured in
``test_cut_density`` and ``test_creative_scoring``. What is checked here is that combining them stays
deterministic, keeps producing a complete plan, and never silently falls back.

Stage 4's parent package pulls in librosa/cupy/logger, so the shared support is AST-extracted and the
stage module loaded onto a stub package — the same technique ``test_cut_density`` documents.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

np = pytest.importorskip("numpy", reason="Stage 4 and Stage 6 are numpy-based")

from beatsync_fork import creative as fork_creative

from test_cut_density import _DURATION, _TEMPO, _fixture, _load_stage4

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLANNER_PATH = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py")


@pytest.fixture(scope="module")
def pipeline():
    """``(stage4_module, shared_package, planner_module)`` — the two real stage modules."""
    stage4, shared = _load_stage4()
    spec = importlib.util.spec_from_file_location("stage6_creative_interactions", _PLANNER_PATH)
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    return stage4, shared, planner


@pytest.fixture(scope="module")
def track():
    return _fixture()


def _candidate_pool(count: int = 40) -> list[dict]:
    """A library wide enough that the planner has real choices at every target."""
    pool = []
    for i in range(count):
        pool.append({
            "id": f"cand_{i:03d}",
            "video_file": f"C:/library/source_{i % 11:02d}.mp4",
            "source_name": f"source_{i % 11:02d}",
            "duration": 6.0,
            "video_duration": 120.0,
            "center": 12.0 + i, "peak_time": 12.4 + i, "start": 9.0 + i,
            "quality_score": 0.55 + (i % 7) * 0.02,
            "action_score": 0.30 + (i % 9) * 0.06,
            "beauty_score": 0.35 + (i % 5) * 0.08,
            "tension_score": 0.25 + (i % 6) * 0.07,
            "soft_score": 0.20 + (i % 4) * 0.09,
            "motion": (i % 10) / 9.0,
            "brightness": 0.45 + (i % 3) * 0.12,
            "tags": [["action"], ["soft"], ["tension"], ["beauty"]][i % 4],
            "semantic": {"character_focus": 0.30 + (i % 4) * 0.1,
                         "combat": (i % 5) * 0.12, "chase": (i % 3) * 0.14,
                         "explosion": (i % 7) * 0.08},
            "ai_analyzed": i % 3 != 0,
        })
    return pool


def _run(pipeline, track, profile: fork_creative.CreativeProfile):
    """Stage 4 -> segment timeline -> Stage 6, exactly as the pipeline composes them."""
    stage4, shared, planner = pipeline
    beat_times, features, sections = track

    if profile.is_neutral_cuts():
        cfg, factor = shared.CONFIG, None
    else:
        factor = profile.cut_density_factor()
        cfg = shared.density_scaled_config(shared.CONFIG, factor)
    if not profile.is_neutral_micro_cuts():
        cfg = shared.micro_cut_scaled_config(cfg, profile)
    selected, _ = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=cfg, density_factor=factor)

    # `build_frame_aligned_cut_timeline`'s contract: boundaries at 0 and the audio end, internal
    # boundaries from the selected beats, durations as differences. Frame quantisation needs the
    # runtime, and it cannot change which candidate wins a segment, so it is left out.
    internal = selected[(selected > 0.0) & (selected < _DURATION)]
    cut_times = np.concatenate(([0.0], internal, [_DURATION]))
    durations = np.diff(cut_times)

    plan = planner.build_planned_clip_sequence(
        cut_times=cut_times, segment_durations=durations,
        beat_info={
            "times": beat_times,
            "sections": sections,
            "energy_profile": {"wave": features["wave"], "arc": features["arc"]},
            "rhythm_data": {"impact_strength": features["impact_score"],
                            "combined_strength": features["rhythm_score"],
                            "novelty_strength": features["novelty"]},
            "video_analysis": {"candidates": _candidate_pool()},
            "creative": profile.as_dict(),
        },
        video_files=[])
    return cut_times, durations, plan


#: Small and purposeful: all-neutral, each control at both extremes, one combined profile, and the
#: same combined profile under a positive seed.
_GRID = [
    fork_creative.CreativeProfile(),
    fork_creative.CreativeProfile(cut_density=0),
    fork_creative.CreativeProfile(cut_density=100),
    fork_creative.CreativeProfile(energy_response=0),
    fork_creative.CreativeProfile(energy_response=100),
    fork_creative.CreativeProfile(motion_bias=0),
    fork_creative.CreativeProfile(motion_bias=100),
    fork_creative.CreativeProfile(cut_density=70, energy_response=80, motion_bias=30),
    fork_creative.CreativeProfile(seed=101, cut_density=70, energy_response=80, motion_bias=30),
    # Creative Controls Extra: each new control alone, then the full combined profile.
    fork_creative.CreativeProfile(source_diversity=0),
    fork_creative.CreativeProfile(source_diversity=100),
    fork_creative.CreativeProfile(micro_cuts=0),
    fork_creative.CreativeProfile(micro_cuts=100),
    fork_creative.CreativeProfile(seed=101, cut_density=75, energy_response=80, motion_bias=25,
                                  source_diversity=80, micro_cuts=70),
]


@pytest.mark.parametrize("profile", _GRID, ids=lambda p: p.describe().replace(" · ", ",")[:48])
def test_every_profile_in_the_grid_renders_a_complete_valid_plan(pipeline, track, profile):
    cut_times, durations, plan = _run(pipeline, track, profile)

    assert len(plan) == len(durations), "an empty or short plan means Stage 6 fell back"
    assert np.all(np.diff(cut_times) > 0.0), "the cut timeline is not strictly increasing"
    assert cut_times[0] == 0.0 and cut_times[-1] == pytest.approx(_DURATION)
    for index, item in enumerate(plan):
        assert item["index"] == index
        assert item["video_file"]
        assert item["final_duration"] > 0.0
        assert item["start_time"] >= 0.0
        assert item["target"] in {"soft", "flow", "build", "rhythm", "drop"}
        assert -1.0 <= item["score"] <= 2.0


@pytest.mark.parametrize("profile", _GRID, ids=lambda p: p.describe().replace(" · ", ",")[:48])
def test_every_profile_in_the_grid_is_deterministic(pipeline, track, profile):
    first = _run(pipeline, track, profile)
    second = _run(pipeline, track, profile)

    assert np.array_equal(first[0], second[0])
    assert [i["candidate_id"] for i in first[2]] == [i["candidate_id"] for i in second[2]]
    assert [i["score"] for i in first[2]] == [i["score"] for i in second[2]]


def test_the_all_neutral_grid_entry_is_the_no_creative_state_render(pipeline, track):
    """The whole grid's baseline: neutral must equal supplying nothing at all."""
    stage4, shared, planner = pipeline
    neutral_cuts, _, neutral_plan = _run(pipeline, track, fork_creative.CreativeProfile())

    beat_times, features, sections = track
    bare, _ = stage4.select_wave_cuts(
        beat_times=beat_times, sections=sections, features=features,
        tempo=_TEMPO, audio_duration=_DURATION, cfg=shared.CONFIG)
    bare_internal = bare[(bare > 0.0) & (bare < _DURATION)]
    bare_cuts = np.concatenate(([0.0], bare_internal, [_DURATION]))
    bare_plan = planner.build_planned_clip_sequence(
        cut_times=bare_cuts, segment_durations=np.diff(bare_cuts),
        beat_info={
            "times": beat_times, "sections": sections,
            "energy_profile": {"wave": features["wave"], "arc": features["arc"]},
            "rhythm_data": {"impact_strength": features["impact_score"],
                            "combined_strength": features["rhythm_score"],
                            "novelty_strength": features["novelty"]},
            "video_analysis": {"candidates": _candidate_pool()},
        },
        video_files=[])

    assert np.array_equal(neutral_cuts, bare_cuts)
    assert [i["candidate_id"] for i in neutral_plan] == [i["candidate_id"] for i in bare_plan]


def test_only_the_stage_4_controls_change_the_segment_count(pipeline, track):
    """Stage ownership, end to end: every Stage-6 control and the seed must leave the timeline
    alone, and Cut Density must move it."""
    neutral_cuts = _run(pipeline, track, fork_creative.CreativeProfile())[0]

    for profile in (fork_creative.CreativeProfile(energy_response=0),
                    fork_creative.CreativeProfile(energy_response=100),
                    fork_creative.CreativeProfile(motion_bias=0),
                    fork_creative.CreativeProfile(motion_bias=100),
                    fork_creative.CreativeProfile(source_diversity=0),
                    fork_creative.CreativeProfile(source_diversity=100),
                    fork_creative.CreativeProfile(seed=381944)):
        assert np.array_equal(_run(pipeline, track, profile)[0], neutral_cuts), profile.describe()

    sparse = _run(pipeline, track, fork_creative.CreativeProfile(cut_density=0))[0]
    dense = _run(pipeline, track, fork_creative.CreativeProfile(cut_density=100))[0]
    assert sparse.size < neutral_cuts.size < dense.size


def test_source_diversity_spreads_the_plan_without_touching_the_timeline(pipeline, track):
    """The two Extra controls, end to end and in their own stages."""
    low_cuts, _, low_plan = _run(pipeline, track, fork_creative.CreativeProfile(source_diversity=0))
    high_cuts, _, high_plan = _run(
        pipeline, track, fork_creative.CreativeProfile(source_diversity=100))

    assert np.array_equal(low_cuts, high_cuts), "diversity moved the Stage-4 timeline"
    assert [i["target"] for i in low_plan] == [i["target"] for i in high_plan]
    assert len({i["video_file"] for i in high_plan}) >= len({i["video_file"] for i in low_plan})


def test_a_positive_seed_still_varies_and_reproduces_on_a_retimed_edit(pipeline, track):
    """The seed and Cut Density are independent: a denser edit is still reproducible by seed, and a
    seed still visibly changes the plan on it."""
    combined = dict(cut_density=70, energy_response=80, motion_bias=30)
    legacy = [i["candidate_id"] for i in _run(
        pipeline, track, fork_creative.CreativeProfile(**combined))[2]]
    seeded = [i["candidate_id"] for i in _run(
        pipeline, track, fork_creative.CreativeProfile(seed=101, **combined))[2]]
    again = [i["candidate_id"] for i in _run(
        pipeline, track, fork_creative.CreativeProfile(seed=101, **combined))[2]]
    other = [i["candidate_id"] for i in _run(
        pipeline, track, fork_creative.CreativeProfile(seed=202, **combined))[2]]

    assert len(seeded) == len(legacy)
    assert seeded == again, "the seed stopped reproducing once the other controls moved"
    assert seeded != legacy and other != legacy
    assert seeded != other


def test_interleaved_renders_cannot_contaminate_each_other(pipeline, track):
    """Every control is per-call state. Running the grid in a different order must not change any
    individual result — the property a module-level 'current profile' would break."""
    expected = {}
    for profile in _GRID:
        expected[profile] = [i["candidate_id"] for i in _run(pipeline, track, profile)[2]]

    for profile in reversed(_GRID):
        assert [i["candidate_id"] for i in _run(pipeline, track, profile)[2]] == expected[profile], \
            profile.describe()


def test_practical_diagnostic_the_full_creative_matrix(pipeline, track):
    """The compact end-to-end diagnostic. Synthetic fixtures only — no Qwen, no render, no cache.
    To read it::

        python -m pytest tests/test_creative_controls_interactions.py -s -k practical_diagnostic
    """
    rows = []
    for label, profile in [
        ("Legacy      ", fork_creative.CreativeProfile()),
        ("Sparse      ", fork_creative.CreativeProfile(cut_density=0)),
        ("Dense       ", fork_creative.CreativeProfile(cut_density=100)),
        ("Calm        ", fork_creative.CreativeProfile(motion_bias=0)),
        ("Dynamic     ", fork_creative.CreativeProfile(motion_bias=100)),
        ("Strong energy", fork_creative.CreativeProfile(energy_response=100)),
    ]:
        cut_times, durations, plan = _run(pipeline, track, profile)
        motion = [c for c in _candidate_pool()]
        by_id = {c["id"]: c for c in motion}
        mean_motion = sum(by_id[i["candidate_id"]]["motion"] for i in plan) / len(plan)
        rows.append(
            f"  {label}  D{profile.cut_density:3d} E{profile.energy_response:3d} "
            f"M{profile.motion_bias:3d}  segments {len(durations):4d}  "
            f"avg segment {float(np.mean(durations)):.3f}s  clips {len(plan):4d}  "
            f"mean motion {mean_motion:.3f}  "
            f"sources {len({i['video_file'] for i in plan}):2d}")
    print("\nCreative Controls Core — one synthetic track, one synthetic 40-moment library:\n"
          + "\n".join(rows))

    assert len(rows) == 6
