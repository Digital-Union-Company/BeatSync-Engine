#!/usr/bin/env python3
"""Audio-visual clip planner for Auto Mode."""

from __future__ import annotations

import hashlib
import random
from collections import Counter, deque
from typing import Dict, List, Sequence

import numpy as np

# [FORK] Digital-Union: seeded creative variation (stdlib-only fork module).
from beatsync_fork import variation as fork_variation
# [FORK] Digital-Union: the resolved Creative Profile (stdlib-only fork module).
from beatsync_fork import creative as fork_creative
# [FORK] Digital-Union: the pre-Qwen deterministic candidate view (stdlib-only fork module).
from beatsync_fork import deterministic_view as fork_deterministic
# [FORK] Digital-Union (Freestyle V1): section-scoped creative modulation (stdlib-only fork module).
# The declaration and the sparse-override composition live there; this module only looks a segment's
# section type up and plans with whatever profile comes back.
from beatsync_fork import freestyle as fork_freestyle


def _clamp(value, lo: float = 0.0, hi: float = 1.0, default: float = 0.0) -> float:
    try:
        v = float(value)
    except Exception:
        v = default
    if not np.isfinite(v):
        v = default
    return max(lo, min(hi, v))


def _stable_rng(*parts) -> random.Random:
    raw = "|".join(str(p) for p in parts)
    seed = int(hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12], 16)
    return random.Random(seed)


def creative_profile(beat_info: Dict | None) -> fork_creative.CreativeProfile:
    """[FORK] Digital-Union: read the resolved Creative Profile off the shared ``beat_info`` bus.

    The single reader. Absent, malformed or stale creative state resolves to the all-neutral profile
    — current main — because a planner must never raise on a bus it did not write. Older buses still
    resolve correctly with whatever they do not carry left neutral: a Phase A ``{"seed": n}`` dict,
    and a Creative Controls Core dict without ``source_diversity``/``micro_cuts``.

    Stage 6 is the only stage that reads any of this, which is why the profile rides on ``beat_info``
    instead of being threaded through the analysis signatures: it must never reach Stage 5's cache
    identity.
    """
    if not isinstance(beat_info, dict):
        return fork_creative.NEUTRAL_PROFILE
    return fork_creative.CreativeProfile.from_mapping(beat_info.get("creative"))


def freestyle_declaration(beat_info: Dict | None):
    """[FORK] Digital-Union (Freestyle V1): the render's section rules, off the shared bus.

    One reader for one key, exactly as `creative_profile` is the one reader of `"creative"`, so the
    Freestyle state cannot fork into two independently-parsed views. Total: an absent, stale or
    foreign value degrades to an inactive declaration and the planner takes its global path.
    """
    value = (beat_info or {}).get("freestyle")
    if isinstance(value, fork_freestyle.FreestyleDeclaration):
        return value
    return fork_freestyle.FreestyleDeclaration()


def creative_seed(beat_info: Dict | None) -> int:
    """[FORK] Digital-Union: the user's variation seed. Absent/malformed/non-positive means legacy.

    Kept as the named compatibility surface it has been since Phase A (``video_processor`` reports
    it, and it is the seed's public reader), now expressed through the one profile reader above so
    the creative state cannot fork into two independently-parsed views of the same dict.
    """
    return creative_profile(beat_info).seed


def build_planned_clip_sequence(
    cut_times: Sequence[float],
    segment_durations: Sequence[float],
    beat_info: Dict | None,
    video_files: Sequence[str],
) -> List[Dict]:
    """Build exact source clip choices for every output segment.

    Returns an empty list when no visual library is present, which tells the
    renderer to keep its old fallback sampling.
    """
    beat_info = beat_info or {}
    video_analysis = beat_info.get("video_analysis") or {}
    candidates = list(video_analysis.get("candidates") or [])
    candidates = [c for c in candidates if c.get("video_file")]
    if not candidates:
        return []

    cut_times_arr = np.asarray(cut_times, dtype=float)
    durations_arr = np.asarray(segment_durations, dtype=float)
    if cut_times_arr.size < 2 or durations_arr.size == 0:
        return []

    profiles = _build_segment_profiles(cut_times_arr, durations_arr, beat_info)
    # [FORK] Digital-Union (Creative Controls Core): one resolved profile for the whole call. The
    # scoring controls are render-scoped constants, so they are bound explicitly here and passed
    # down — there is deliberately no module-global creative state, which is what keeps one render's
    # settings from leaking into the next in a long-lived process.
    profile_settings = creative_profile(beat_info)
    seed = profile_settings.seed
    controls = profile_settings.scoring_controls()
    # [FORK] Digital-Union (Creative Controls Extra): Source Diversity is the DYNAMIC half. It reads
    # `usage` and `recent_videos`, so it is resolved here but deliberately kept out of
    # `ScoringControls` and out of the static table below - a diversity change must not trigger any
    # static-score work at all. `None` means the exact legacy source penalties.
    source_diversity_factor = (
        None if profile_settings.is_neutral_source_diversity()
        else profile_settings.source_diversity_factor()
    )

    # [FORK] Digital-Union (Freestyle V1): the per-section layer. `profile_settings` above stays the
    # GLOBAL base and the seed stays global — Freestyle varies only the four Stage-6 controls, and
    # it does so by section TYPE, which `_build_segment_profiles` has already attached to every
    # segment as `profile["section_type"]`. So this needs no new section resolution at all.
    #
    # Resolution is memoised per distinct section type, not per segment: a thirteen-section track
    # with two rules resolves two effective profiles. A section with no rule gets back the base
    # object itself, so it reuses the base controls by identity rather than an equal-but-separate
    # copy — which is what keeps the distinct-controls count honest.
    declaration = freestyle_declaration(beat_info)
    freestyle_active = declaration.is_active()
    if freestyle_active:
        resolved_by_type: dict = {}

        def _section_controls(section_type):
            if section_type not in resolved_by_type:
                effective = fork_freestyle.effective_profile(
                    profile_settings, section_type, declaration)
                if effective is profile_settings:
                    resolved_by_type[section_type] = (controls, source_diversity_factor)
                else:
                    resolved_by_type[section_type] = (
                        effective.scoring_controls(),
                        None if effective.is_neutral_source_diversity()
                        else effective.source_diversity_factor(),
                    )
            return resolved_by_type[section_type]

        segment_controls = [
            _section_controls(profile.get("section_type")) for profile in profiles]
    else:
        segment_controls = [(controls, source_diversity_factor) for _ in profiles]

    # [FORK] Digital-Union (L1A): the static half of the score, computed once per
    # (candidate, target) instead of once per (candidate, segment). A real run measured 148 segments
    # × 9241 candidates = 1,367,668 evaluations across at most five distinct targets, so this is
    # `candidates × distinct targets` work instead — the candidate pool, its order, the targets and
    # every penalty are untouched. `dict.fromkeys` keeps first-seen target order, so the work is
    # deterministic; the default matches `_score_candidate`'s own `profile.get("target", "flow")`.
    # This runs inside `build_planned_clip_sequence`, so the L0 planner timing in
    # `video_processor.create_music_video` still describes the whole call, precomputation included.
    #
    # [FORK] Digital-Union (Creative Controls Core): Energy Response and Motion Bias do NOT add a
    # dimension to this table — they are constants for the render, so the shape stays
    # `candidates × distinct targets`. Energy Response needs one extra column, "flow", because it
    # blends each target score against the generic one; that column is `candidates × 1` and is only
    # built when the control is non-neutral. Neutral scoring keeps today's dict comprehension
    # verbatim rather than running the new arithmetic with neutral coefficients.
    segment_targets = [profile.get("target", "flow") for profile in profiles]
    deterministic_views: tuple | None = None
    deterministic_by_candidate: dict = {}
    if freestyle_active:
        # [FORK] Digital-Union (Freestyle V1): the heterogeneous table. Keyed by
        # `(ScoringControls, target)` and built LAZILY on first use, never by segment — so the work
        # is bounded by `min(distinct controls × distinct targets, segments)` and can never reach
        # the pre-L1A `candidates × segments` shape. Measured on the real 9241-candidate /
        # 148-segment library: one candidates-wide column is 61 ms, today's global case is 5 columns
        # (0.30 s), ten distinct profiles lazily is 10 columns (0.61 s), and the pre-L1A shape was
        # 148 columns (9.02 s).
        #
        # `ScoringControls` is a frozen dataclass of three floats/None, so it is hashable and
        # equality-deduping: two sections that resolve to the same controls share one table, and no
        # new score-table identity type was invented.
        distinct_controls = tuple(dict.fromkeys(item[0] for item in segment_controls))
        # The deterministic views are the one genuinely expensive part of Semantic Emphasis, and
        # they depend on the CANDIDATE only — never on the control values — so they are built once
        # for the whole call if ANY active profile needs them, and reused by every table.
        if any(item.needs_deterministic_views for item in distinct_controls):
            deterministic_views = tuple(
                fork_deterministic.deterministic_candidate_view(candidate)
                for candidate in candidates
            )
            deterministic_by_candidate = {
                id(candidate): view for candidate, view in zip(candidates, deterministic_views)
            }

        # Energy Response blends each target against the generic "flow" score computed under the
        # SAME controls, so a flow column is cached per distinct controls tuple and never shared
        # across different ones — sharing would blend two different interpretations together.
        flow_by_controls: dict = {}
        base_scores_by_key: dict = {}

        def _view_at(position):
            return None if deterministic_views is None else deterministic_views[position]

        def _table_for(scoring, target):
            key = (scoring, target)
            table = base_scores_by_key.get(key)
            if table is not None:
                return table
            if scoring.is_neutral:
                table = tuple(_static_base_score(candidate, target) for candidate in candidates)
            else:
                flow_scores = flow_by_controls.get(scoring)
                if scoring.needs_flow_column and flow_scores is None:
                    flow_scores = tuple(
                        _semantic_adjusted_score(candidate, "flow", scoring, _view_at(position))
                        for position, candidate in enumerate(candidates)
                    )
                    flow_by_controls[scoring] = flow_scores
                table = tuple(
                    _effective_base_score(
                        candidate, target, scoring,
                        flow_score=None if flow_scores is None else flow_scores[position],
                        deterministic=_view_at(position),
                    )
                    for position, candidate in enumerate(candidates)
                )
            base_scores_by_key[key] = table
            return table

        base_scores_by_target = None
    elif controls.is_neutral:
        base_scores_by_target = {
            target: tuple(_static_base_score(candidate, target) for candidate in candidates)
            for target in dict.fromkeys(segment_targets)
        }
    else:
        # [FORK] Digital-Union (Creative Controls Extra PR2): the deterministic views are built
        # ONCE per candidate here and reused for every target, for the flow column and for the
        # per-clip diagnostic score. Rebuilding them per (candidate, target) would multiply the one
        # genuinely expensive part of Semantic Emphasis by the target count for no benefit. Keyed by
        # `id()` for the materialise lookup because candidates are dicts (unhashable) whose ids are
        # not guaranteed unique across a large library — the list holds them alive for the call.
        if controls.needs_deterministic_views:
            deterministic_views = tuple(
                fork_deterministic.deterministic_candidate_view(candidate)
                for candidate in candidates
            )
            deterministic_by_candidate = {
                id(candidate): view for candidate, view in zip(candidates, deterministic_views)
            }
        # The flow column Energy Response blends against is the SEMANTIC-ADJUSTED flow, so both
        # sides of that blend describe the same candidate under the same interpretation.
        flow_scores = (
            tuple(
                _semantic_adjusted_score(
                    candidate, "flow", controls,
                    None if deterministic_views is None else deterministic_views[position],
                )
                for position, candidate in enumerate(candidates)
            )
            if controls.needs_flow_column else None
        )
        base_scores_by_target = {
            target: tuple(
                _effective_base_score(
                    candidate, target, controls,
                    flow_score=None if flow_scores is None else flow_scores[position],
                    deterministic=(None if deterministic_views is None
                                   else deterministic_views[position]),
                )
                for position, candidate in enumerate(candidates)
            )
            for target in dict.fromkeys(segment_targets)
        }

    recent_ids = deque(maxlen=10)
    recent_videos = deque(maxlen=5)
    usage = Counter()
    planned: List[Dict] = []

    for i, profile in enumerate(profiles):
        # [FORK] Digital-Union (Freestyle V1): this segment's effective scoring controls and its
        # Source Diversity factor. On a global render both are the base values for every segment,
        # so the loop is unchanged in shape. The seed is NOT per-segment: it stays global, so
        # `_stable_rng(seed, index, target, start)` keeps the exact stream it has always had.
        segment_scoring, segment_diversity = segment_controls[i]
        base_scores = (_table_for(segment_scoring, segment_targets[i]) if freestyle_active
                       else base_scores_by_target[segment_targets[i]])
        candidate = _choose_candidate(
            candidates=candidates,
            profile=profile,
            recent_ids=recent_ids,
            recent_videos=recent_videos,
            usage=usage,
            index=i,
            seed=seed,
            base_scores=base_scores,
            controls=segment_scoring,
            source_diversity_factor=segment_diversity,
        )
        if not candidate:
            continue
        planned_clip = _materialize_clip(
            candidate=candidate,
            profile=profile,
            index=i,
            # The recorded diagnostic score must describe what actually selected this clip, so it
            # uses THIS segment's effective controls — not the global base a section override
            # replaced.
            controls=segment_scoring,
            deterministic=deterministic_by_candidate.get(id(candidate)),
        )
        planned.append(planned_clip)
        recent_ids.append(candidate.get("id"))
        recent_videos.append(candidate.get("video_file"))
        usage[candidate.get("id")] += 1
        usage[candidate.get("video_file")] += 1

    if len(planned) != len(durations_arr):
        return []
    return planned


def summarize_clip_plan(plan: Sequence[Dict], seed: int = 0, creative=None) -> Dict:
    # [FORK] Digital-Union: `seed` is optional and defaults to legacy, so existing callers are
    # unchanged. It is reported, never re-derived — the plan itself carries no seed.
    #
    # [FORK] Digital-Union (Creative Controls Core): `creative` is the resolved profile (a
    # `CreativeProfile` or its `as_dict()`), also optional. When it is absent the summary describes
    # a seed-only profile, which is exactly what this function reported before. Reporting only: no
    # part of the plan is re-derived from it.
    if creative is None:
        profile = fork_creative.CreativeProfile.from_widgets(seed=seed)
    elif isinstance(creative, fork_creative.CreativeProfile):
        profile = creative
    else:
        profile = fork_creative.CreativeProfile.from_mapping(creative)
    seed = profile.seed
    summary = {
        "clip_count": 0,
        "targets": {},
        "ai_tagged": 0,
        "seed": seed,
        "variation": fork_variation.describe(seed),
        "creative": profile.as_dict(),
        "creative_text": profile.describe(),
    }
    if not plan:
        return summary
    targets = Counter(str(item.get("target", "flow")) for item in plan)
    summary.update({
        "clip_count": len(plan),
        "targets": dict(targets),
        "ai_tagged": sum(1 for item in plan if item.get("ai_analyzed")),
        "source_count": len(set(item.get("video_file") for item in plan)),
    })
    return summary


def _build_segment_profiles(cut_times: np.ndarray, segment_durations: np.ndarray, beat_info: Dict) -> List[Dict]:
    beat_times = np.asarray(beat_info.get("times", []), dtype=float)
    energy_profile = beat_info.get("energy_profile") or {}
    rhythm_data = beat_info.get("rhythm_data") or {}
    sections = beat_info.get("sections") or []

    wave = np.asarray(energy_profile.get("wave", []), dtype=float)
    arc = np.asarray(energy_profile.get("arc", []), dtype=float)
    impact = np.asarray(rhythm_data.get("impact_strength", []), dtype=float)
    rhythm = np.asarray(rhythm_data.get("combined_strength", []), dtype=float)
    novelty = np.asarray(rhythm_data.get("novelty_strength", []), dtype=float)

    profiles: List[Dict] = []
    for i, duration in enumerate(segment_durations):
        start = float(cut_times[i])
        end = float(cut_times[i + 1])
        mid = (start + end) * 0.5
        local_wave = _interp_feature(mid, beat_times, wave, 0.5)
        local_arc = _interp_feature(mid, beat_times, arc, 0.5)
        local_impact = _interp_feature(start, beat_times, impact, 0.5)
        local_rhythm = _interp_feature(start, beat_times, rhythm, 0.5)
        local_novelty = _interp_feature(start, beat_times, novelty, 0.4)
        section = _section_at(sections, mid)
        target = _target_for_segment(section, local_wave, local_impact, local_rhythm, local_novelty, local_arc)
        profiles.append({
            "index": i,
            "start": start,
            "end": end,
            "duration": float(duration),
            "mid": mid,
            "wave": local_wave,
            "impact": local_impact,
            "rhythm": local_rhythm,
            "novelty": local_novelty,
            "arc": local_arc,
            "section": section,
            "section_type": section.get("type", "body") if section else "body",
            "target": target,
        })
    return profiles


def _interp_feature(time_s: float, beat_times: np.ndarray, values: np.ndarray, default: float) -> float:
    if beat_times.size == 0 or values.size != beat_times.size:
        return default
    return _clamp(np.interp(time_s, beat_times, values, left=float(values[0]), right=float(values[-1])), default=default)


def _section_at(sections: Sequence[Dict], time_s: float) -> Dict:
    for section in sections:
        if float(section.get("start", 0.0)) <= time_s < float(section.get("end", 0.0)):
            return section
    return sections[-1] if sections else {}


def _target_for_segment(section: Dict, wave: float, impact: float, rhythm: float, novelty: float, arc: float) -> str:
    section_type = section.get("type", "body")
    if section_type in {"drop", "finale"} and (wave >= 0.58 or impact >= 0.55):
        return "drop"
    if impact >= 0.76 or (wave >= 0.78 and rhythm >= 0.62):
        return "drop"
    if section_type in {"breakdown", "intro", "outro"} and wave <= 0.54:
        return "soft"
    if wave <= 0.32 and impact <= 0.48:
        return "soft"
    if section_type in {"bridge", "hook"} or novelty >= 0.68 or (arc >= 0.62 and wave >= 0.48):
        return "build"
    if wave >= 0.58 and rhythm >= 0.54:
        return "rhythm"
    return "flow"


def _adjusted_score(
    candidate: Dict,
    profile: Dict,
    recent_ids: deque,
    recent_videos: deque,
    usage: Counter,
    base_score: float | None = None,
    controls: "fork_creative.ScoringControls | None" = None,
    source_diversity_factor: float | None = None,
) -> float:
    """The planner's score for one candidate, repeat and duration penalties applied.

    [FORK] Digital-Union: lifted verbatim out of ``_choose_candidate`` so the legacy argmax and the
    seeded variation branch score identically — the seed changes only which of the good candidates
    wins, never what "good" means. The arithmetic and its order are unchanged from current main.

    [FORK] Digital-Union (L1A): everything below the first line is the **dynamic** half — it depends
    on what this plan has already chosen (`recent_ids`, `recent_videos`, `usage`) and on this
    segment's duration, so it must still run per segment and is untouched. Only the static base score
    may be supplied by the caller; ``base_score=None`` keeps the original behaviour of computing it
    here, which is what every existing caller and unit test gets.

    [FORK] Digital-Union (Creative Controls Core): ``controls`` rides alongside ``base_score``
    precisely so the *fallback* stays correct. If the table is absent or rejected as misaligned,
    recomputing the static score here must produce the same number the table would have held —
    otherwise a defensive path would silently drop back to legacy scoring on a render the user
    configured. ``None`` is neutral, so every existing caller is unchanged.

    [FORK] Digital-Union (Creative Controls Extra): ``source_diversity_factor`` scales the two
    **source-video-level** reuse penalties, and only those. The two candidate-level protections —
    ``-0.28`` for a recently used candidate id and the capped ``usage[id] * 0.10`` — are identical
    in both branches at every setting: Source Diversity decides how willing the plan is to return to
    the same *source video*, and must never be able to buy a repeated *moment*. ``None`` is neutral
    and takes the untouched legacy expressions; the two branches keep the same operation order so
    the neutral one is arithmetically identical to current main, not merely equal in principle.
    """
    score = _score_candidate(candidate, profile, controls) if base_score is None else float(base_score)
    cid = candidate.get("id")
    video_file = candidate.get("video_file")

    if source_diversity_factor is None:
        if cid in recent_ids:
            score -= 0.28
        if video_file in recent_videos:
            score -= 0.10
        score -= min(0.28, usage[cid] * 0.10)
        score -= min(0.18, usage[video_file] * 0.012)
    else:
        if cid in recent_ids:
            score -= 0.28
        if video_file in recent_videos:
            score -= 0.10 * source_diversity_factor
        score -= min(0.28, usage[cid] * 0.10)
        score -= source_diversity_factor * min(0.18, usage[video_file] * 0.012)

    required_source = max(0.05, profile["duration"])
    candidate_duration = max(0.05, float(candidate.get("duration", required_source)))
    if candidate_duration < required_source * 0.55:
        score -= 0.18

    return score


def _choose_candidate(
    candidates: Sequence[Dict],
    profile: Dict,
    recent_ids: deque,
    recent_videos: deque,
    usage: Counter,
    index: int,
    seed: int = 0,
    base_scores: Sequence[float] | None = None,
    controls: "fork_creative.ScoringControls | None" = None,
    source_diversity_factor: float | None = None,
) -> Dict | None:
    # [FORK] Digital-Union (L1A): `base_scores` is the precomputed static score for THIS segment's
    # target, aligned with `candidates` by position — candidate ids are not used as the key, because
    # they are not guaranteed unique across a 900-source library. The alignment is checked rather
    # than assumed; a mismatched table is ignored and every score is computed the old way, so a
    # future caller can never silently score against the wrong candidates.
    if base_scores is not None and len(base_scores) != len(candidates):
        base_scores = None

    # [FORK] Digital-Union: seed 0 is the legacy path and must stay bit-identical to current main —
    # including the RNG stream, which is why it still hashes exactly `(index, target, start)` with no
    # seed component. A positive seed takes the variation branch below.
    if not fork_variation.is_variation(seed):
        best_candidate = None
        best_score = -999.0
        rng = _stable_rng(index, profile.get("target"), profile.get("start"))

        # Iteration order, comparison and the one `rng.random()` draw per candidate are unchanged;
        # only where the base score comes from differs.
        for position, candidate in enumerate(candidates):
            score = _adjusted_score(
                candidate, profile, recent_ids, recent_videos, usage,
                base_score=None if base_scores is None else base_scores[position],
                controls=controls,
                source_diversity_factor=source_diversity_factor,
            )
            score += rng.random() * 0.015
            if score > best_score:
                best_score = score
                best_candidate = candidate

        return best_candidate

    if not candidates:
        return None

    # The tiny legacy jitter is dropped here rather than stacked: seeded selection subsumes it.
    scores = [
        _adjusted_score(
            candidate, profile, recent_ids, recent_videos, usage,
            base_score=None if base_scores is None else base_scores[position],
            controls=controls,
            source_diversity_factor=source_diversity_factor,
        )
        for position, candidate in enumerate(candidates)
    ]
    rng = _stable_rng(seed, index, profile.get("target"), profile.get("start"))
    return candidates[fork_variation.select_index(scores, rng)]


def _score_candidate(candidate: Dict, profile: Dict,
                     controls: "fork_creative.ScoringControls | None" = None,
                     deterministic: Dict | None = None) -> float:
    """The planner's base score for one candidate under one segment profile.

    [FORK] Digital-Union (L1A): kept as the compatibility surface every existing caller uses
    (`_materialize_clip`, tests, any internal caller). It now only resolves the target and delegates,
    because the arithmetic below reads **nothing else** from the profile.

    [FORK] Digital-Union (Creative Controls Core): ``controls`` defaults to ``None`` — neutral —
    so every existing caller is unchanged and gets the legacy score exactly.
    """
    return _effective_base_score(candidate, profile.get("target", "flow"), controls,
                                 deterministic=deterministic)


def _candidate_motion(candidate: Dict) -> float:
    """The candidate's motion in ``[0, 1]``, with the planner's existing semantic fallback.

    [FORK] Digital-Union (Creative Controls Core): extracted so Motion Bias reads motion the same
    way the base score already does — the deterministic ``motion`` metric when present, the Qwen
    ``camera_motion`` semantic otherwise, ``0.0`` when neither is. Motion Bias must weight the value
    the planner already believes; deriving it any other way would be a second, silently diverging
    definition of what "dynamic" means.
    """
    semantic = candidate.get("semantic") or {}
    return _clamp(candidate.get("motion", semantic.get("camera_motion", 0.0)))


def _semantic_adjusted_score(candidate: Dict, target: str,
                             controls: "fork_creative.ScoringControls",
                             deterministic: Dict | None = None) -> float:
    """Steps 1-2 of the static ordering: the legacy score, then Semantic Emphasis.

    [FORK] Digital-Union (Creative Controls Extra PR2). Stage 5 fuses Qwen's reading into the
    candidate's scores and does not keep the pre-fusion values, so "deterministic evidence only" has
    to be *reconstructed* from the raw CV primitives that survive — see
    ``beatsync_fork.deterministic_view``. Both readings then go through the **same**
    ``_static_base_score``; there is deliberately no second scorer, so they can never disagree about
    anything except the candidate each was handed.

    ``deterministic`` is the precomputed view for this candidate. Supplying it is what keeps the
    reconstruction at one per candidate per plan rather than one per (candidate, target).

    Factored out rather than inlined because Energy Response needs this same quantity for its
    ``flow`` reference: blending a semantic-adjusted target against a *legacy* flow score would mix
    two different interpretations of one candidate.
    """
    full = _static_base_score(candidate, target)
    if controls.semantic_factor is None:
        return full
    view = (deterministic if deterministic is not None
            else fork_deterministic.deterministic_candidate_view(candidate))
    det = _static_base_score(view, target)
    return det + controls.semantic_factor * (full - det)


def _effective_base_score(candidate: Dict, target: str,
                          controls: "fork_creative.ScoringControls | None" = None,
                          *, flow_score: float | None = None,
                          deterministic: Dict | None = None) -> float:
    """The static score after Semantic Emphasis, Energy Response and Motion Bias.

    [FORK] Digital-Union (Creative Controls Core + Extra). One explicit, documented ordering:

    1. the legacy static score for the segment's actual target;
    2. **Semantic Emphasis** — blend that against the score the same candidate would get from its
       reconstructed *deterministic* view: ``det + factor * (full - det)``. ``factor`` runs
       0.0 … 2.0, so 0 scores on visual metrics alone and 2 doubles whatever the semantics added.
       This is interpretation, not analysis: Stage 5 is untouched and 0 does **not** disable Qwen;
    3. **Energy Response** — blend against the same candidate's ``flow`` score:
       ``flow + factor * (target - flow)``, ``factor`` 0.40 … 1.60. The flow reference is the
       **semantic-adjusted** one, so both sides of the blend describe the same candidate under the
       same interpretation. At ``factor == 1.0`` this is algebraically the target score, which is
       exactly why 50 takes the neutral branch instead: algebraic identity is not floating-point
       identity;
    4. **Motion Bias** — add ``centered * 0.15 * (2 * motion - 1)``, so calm settings reward
       low-motion material and dynamic settings reward high-motion material, symmetrically about
       ``motion = 0.5``. It reads the *persisted* motion, a raw CV primitive Stage 5 never fuses, so
       Semantic Emphasis cannot move it;
    5. clamp back into the planner's existing ``[-1.0, 2.0]`` score range.

    No control depends on the seed, and none is consulted before the legacy score exists — they
    modulate scoring, they do not replace it. The seed still decides the winner afterwards.

    ``flow_score`` and ``deterministic`` let the caller supply precomputed columns, so each control
    costs table work proportional to ``candidates`` and never to ``segments``.

    When ``controls`` is ``None`` or neutral this returns ``_static_base_score`` untouched: no
    reconstruction, no blend, no shift, no second clamp.
    """
    if controls is None or controls.is_neutral:
        return _static_base_score(candidate, target)

    # `_semantic_adjusted_score` computes the legacy score itself (step 1) — computing it here too
    # would double every cell of the static table for any non-neutral render.
    score = _semantic_adjusted_score(candidate, target, controls, deterministic)
    if controls.energy_factor is not None:
        flow = (_semantic_adjusted_score(candidate, "flow", controls, deterministic)
                if flow_score is None else float(flow_score))
        score = flow + controls.energy_factor * (score - flow)
    if controls.motion_centered is not None:
        score += (controls.motion_centered * fork_creative.MOTION_BIAS_COEFFICIENT
                  * (2.0 * _candidate_motion(candidate) - 1.0))
    return _clamp(score, lo=-1.0, hi=2.0)


def _static_base_score(candidate: Dict, target: str) -> float:
    """The candidate-and-target-only half of the planner's score.

    [FORK] Digital-Union (L1A): this signature *is* the contract. The score of a candidate depends on
    the candidate's own features and on the segment's target — nothing else — so it is constant for a
    given (candidate, target) pair and can be computed once per plan instead of once per segment. The
    measured run that motivated this was 148 segments × 9241 candidates = 1,367,668 evaluations of
    arithmetic with only five distinct target values behind it.

    Because the profile is deliberately **not** a parameter, a future scoring term that depends on
    anything else in the profile (duration, position, section, energy) cannot be added here by
    accident: it has nowhere to read it from, and belongs in the dynamic half, `_adjusted_score`.
    Everything that already varies per segment — repeat penalties, usage penalties, duration
    suitability, the legacy jitter and seeded selection — stays there and is unchanged.
    """
    semantic = candidate.get("semantic") or {}
    tags = {str(t).lower() for t in candidate.get("tags", [])}
    quality = _clamp(candidate.get("quality_score", semantic.get("visual_quality", 0.5)), default=0.5)
    action = _clamp(candidate.get("action_score", semantic.get("action_intensity", 0.0)))
    beauty = _clamp(candidate.get("beauty_score", semantic.get("beauty_score", 0.0)))
    tension = _clamp(candidate.get("tension_score", 0.0))
    soft = _clamp(candidate.get("soft_score", 0.0))
    motion = _clamp(candidate.get("motion", semantic.get("camera_motion", 0.0)))
    character = _clamp(semantic.get("character_focus", 0.0))
    combat = _clamp(semantic.get("combat", 0.0))
    chase = _clamp(semantic.get("chase", 0.0))
    explosion = _clamp(semantic.get("explosion", 0.0))

    tag_bonus = 0.0
    if target in tags:
        tag_bonus += 0.08
    if target == "drop" and tags.intersection({"action", "combat", "chase", "explosion", "hype"}):
        tag_bonus += 0.12
    if target == "soft" and tags.intersection({"soft", "beauty", "sad"}):
        tag_bonus += 0.10
    if target == "build" and tags.intersection({"tension", "transition"}):
        tag_bonus += 0.10

    if target == "drop":
        match = 0.46 * action + 0.16 * motion + 0.12 * combat + 0.10 * chase + 0.08 * explosion + 0.08 * quality
    elif target == "soft":
        match = 0.45 * beauty + 0.18 * soft + 0.13 * character + 0.14 * (1.0 - action) + 0.10 * quality
    elif target == "build":
        match = 0.40 * tension + 0.18 * motion + 0.15 * character + 0.14 * action + 0.13 * quality
    elif target == "rhythm":
        match = 0.30 * action + 0.24 * motion + 0.18 * quality + 0.16 * tension + 0.12 * beauty
    else:
        match = 0.28 * quality + 0.24 * beauty + 0.20 * action + 0.16 * tension + 0.12 * soft

    brightness = _clamp(candidate.get("brightness", 0.5), default=0.5)
    visibility_penalty = 0.0
    if brightness < 0.13:
        visibility_penalty += 0.18
    if quality < 0.24:
        visibility_penalty += 0.16

    return _clamp(match + tag_bonus + 0.12 * quality - visibility_penalty, lo=-1.0, hi=2.0)


def _materialize_clip(candidate: Dict, profile: Dict, index: int,
                      controls: "fork_creative.ScoringControls | None" = None,
                      deterministic: Dict | None = None) -> Dict:
    final_duration = max(0.05, float(profile["duration"]))
    source_duration = final_duration
    video_duration = max(source_duration, float(candidate.get("video_duration", source_duration)))
    target = profile.get("target", "flow")

    if target == "drop":
        anchor = float(candidate.get("peak_time", candidate.get("center", candidate.get("start", 0.0))))
        align = 0.36
    elif target == "soft":
        anchor = float(candidate.get("center", candidate.get("start", 0.0)))
        align = 0.50
    elif target == "build":
        anchor = float(candidate.get("peak_time", candidate.get("center", candidate.get("start", 0.0))))
        align = 0.48
    else:
        anchor = float(candidate.get("center", candidate.get("start", 0.0)))
        align = 0.44

    start_time = anchor - source_duration * align
    start_time = max(0.0, min(start_time, max(0.0, video_duration - source_duration)))

    return {
        "index": index,
        "video_file": candidate.get("video_file"),
        "source_name": candidate.get("source_name"),
        "start_time": start_time,
        "source_duration": source_duration,
        "final_duration": final_duration,
        "target": target,
        # [FORK] Digital-Union (Creative Controls Core): diagnostic, but it must agree with the
        # score selection actually used — a plan reporting the legacy score for a render that chose
        # on a modified one would be a quietly misleading record. Clip timing and anchoring above
        # are untouched by any creative control.
        "score": _score_candidate(candidate, profile, controls, deterministic),
        "candidate_id": candidate.get("id"),
        "tags": list(candidate.get("tags", [])),
        "ai_analyzed": bool(candidate.get("ai_analyzed")),
        "audio_start": profile.get("start"),
        "audio_end": profile.get("end"),
        "wave": profile.get("wave"),
        "impact": profile.get("impact"),
    }
