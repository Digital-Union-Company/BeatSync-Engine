#!/usr/bin/env python3
"""Stage 4: deliberate rhythmic cut selection and final cleanup."""

from typing import Dict, List, Tuple
import numpy as np

from . import AutoWaveConfig
from . import _normalize, _safe_percentile, _unique_sorted

# [FORK] Digital-Union (Creative Controls Core): Cut Density is a Stage 4 control. Only the
# normalised *factor* is threaded in — never a raw GUI value and never module-global render state.
from beatsync_fork import creative as fork_creative

#: Stage 4's own "let a weak beat breathe" threshold. Cut Density scales it by ``1 / factor``; the
#: neutral path uses this literal unchanged, which is what it has always done.
WEAK_SCORE_THRESHOLD = fork_creative.WEAK_SCORE_THRESHOLD


def select_wave_cuts(beat_times: np.ndarray, sections: List[Dict], features: Dict,
                     tempo: float, audio_duration: float,
                     cfg: AutoWaveConfig,
                     density_factor: float | None = None,
                     *,
                     section_settings: Dict | None = None) -> Tuple[np.ndarray, List[Dict]]:
    """Select the deliberate subset of beats that become cuts.

    [FORK] Digital-Union (Creative Controls Core): ``density_factor`` is the Cut Density control,
    already resolved to ``2 ** ((cut_density - 50) / 50)`` by :class:`CreativeProfile`. ``None``
    means **neutral** and is the default, so every existing caller — and every neutral render —
    takes exactly the path this function has always taken. ``None`` rather than ``1.0`` on purpose:
    a neutral render must not run the density arithmetic at all, not even with a neutral value.

    The caller also supplies a ``cfg`` already derived for this density (intervals and holds divided
    by the factor, the global cut-ratio band multiplied by it). Both halves are needed because
    density lives partly in the config's safety floors and partly in the selector's beat stepping.

    [FORK] Digital-Union (Freestyle V1): ``section_settings`` is the **heterogeneous** path and is
    keyword-only, so no positional caller can reach it by accident. ``None`` — the default, and what
    `analyze_beats_auto` passes whenever every section resolves to the *same* effective Cut Density
    — takes the exact legacy composition below: one grid, one global ``add_rare_micro_cuts``, one
    global ``final_wave_cleanup``. That short-circuit is load-bearing rather than an optimisation:
    the per-section cleanup is **not** a refactor-equivalent of the global one (measured: a section
    can fall under its own local ratio floor and gain anchors even where the global count was fine,
    so densities 10/25/50/60 differ), and "Freestyle off changes nothing" has to be exact.

    When it *is* supplied it maps ``section index -> (cfg, density_factor)``: plain render-local
    immutable data, never a callable resolver, never module-global state, never a live GUI value and
    never anything from Stage 5.
    """
    if section_settings:
        return _select_wave_cuts_per_section(
            beat_times, sections, features, audio_duration, cfg, section_settings)

    selected: List[float] = []
    info: List[Dict] = []

    for section in sections:
        beat_indices = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]
        if beat_indices.size == 0:
            continue

        section_selected = select_section_wave_cuts(
            beat_indices, beat_times, features, section, cfg, density_factor)
        selected.extend(section_selected)
        info.append({
            "section": section,
            "selected_count": len(section_selected),
            "beat_count": int(beat_indices.size),
            "density": len(section_selected) / max(1, int(beat_indices.size)),
        })

    # Rare micro-cuts only after the stable main grid is built.
    selected_arr = np.asarray(selected, dtype=float)
    selected_arr = add_rare_micro_cuts(selected_arr, beat_times, features, audio_duration, cfg)

    # Global cleanup: fewer cuts, exact rhythm, no jitter.
    selected_arr = final_wave_cleanup(selected_arr, beat_times, features, audio_duration, cfg)
    return selected_arr, info


def _select_wave_cuts_per_section(beat_times: np.ndarray, sections: List[Dict], features: Dict,
                                  audio_duration: float, cfg: AutoWaveConfig,
                                  section_settings: Dict) -> Tuple[np.ndarray, List[Dict]]:
    """[FORK] Digital-Union (Freestyle V1): the heterogeneous-density composition.

    ::

        per section:  select_section_wave_cuts(that section's cfg and factor)
        per section:  section_density_cleanup(that section's cfg)
        concatenate
                      cross_section_safety           main-grid boundaries only
                      add_rare_micro_cuts            UNCHANGED, one global pass
                      micro_extra_safety             extras only, never the grid
                      validity / sort

    **`final_wave_cleanup` is deliberately NOT called here**, and that is the whole point. Its
    density band is computed from the *global* ``len(beat_times)`` and its cap ranks every cut
    across the whole track, so running it after per-section selection makes one section's override
    delete cuts from other sections — measured on a binding-band fixture: changing only the ``drop``
    rule mutated five non-target sections, three of them not even adjacent. The band belongs to
    main-grid Cut Density, which is now per-section, so it is enforced per section instead.

    Micro Cuts stays one global control: its policy comes from ``cfg`` (the base-derived config) and
    nothing per-section may rewrite it.
    """
    cleaned: Dict[int, List[float]] = {}
    gaps: Dict[int, float] = {}
    info: List[Dict] = []

    for section in sections:
        index = int(section.get("index", len(cleaned)))
        beat_indices = np.where((beat_times >= section["start"]) & (beat_times < section["end"]))[0]
        if beat_indices.size == 0:
            continue
        section_cfg, section_factor = section_settings.get(index, (cfg, None))

        section_selected = select_section_wave_cuts(
            beat_indices, beat_times, features, section, section_cfg, section_factor)
        section_cuts = section_density_cleanup(
            section_selected, beat_indices, beat_times, features, section, section_cfg)
        cleaned[index] = [float(t) for t in section_cuts]
        gaps[index] = float(section_cfg.peak_energy_min_interval)
        info.append({
            "section": section,
            "selected_count": len(cleaned[index]),
            "beat_count": int(beat_indices.size),
            "density": len(cleaned[index]) / max(1, int(beat_indices.size)),
        })

    main_grid = cross_section_safety(cleaned, gaps)
    with_micro = add_rare_micro_cuts(main_grid, beat_times, features, audio_duration, cfg)
    final = micro_extra_safety(main_grid, with_micro, beat_times, cfg)
    return final, info


def select_section_wave_cuts(beat_indices: np.ndarray, beat_times: np.ndarray,
                             features: Dict, section: Dict,
                             cfg: AutoWaveConfig,
                             density_factor: float | None = None) -> List[float]:
    section_type = section.get("type", "verse")
    pattern = section.get("dominant_pattern", "mixed")
    selected: List[float] = []

    scores = compute_cut_scores(beat_indices, features, section, cfg)
    score_map = {int(idx): float(score) for idx, score in zip(beat_indices, scores)}

    current_pos = 0
    last_cut_time = -999.0
    max_hold = max_hold_for_section(section, cfg)

    # [FORK] Digital-Union (Creative Controls Core): resolved once per section, not per beat. The
    # neutral branch is the untouched literal — no division by 1.0 anywhere on a default render.
    if density_factor is None:
        weak_score_threshold = WEAK_SCORE_THRESHOLD
    else:
        weak_score_threshold = fork_creative.scale_weak_score_threshold(density_factor)

    # First cut in a section: use section start if there is a good downbeat nearby.
    first_idx = choose_best_nearby(beat_indices, 0, radius=1, scores=score_map, features=features)
    if first_idx is not None:
        selected.append(float(beat_times[first_idx]))
        last_cut_time = float(beat_times[first_idx])
        current_pos = max(0, int(np.where(beat_indices == first_idx)[0][0]))

    while current_pos < beat_indices.size - 1:
        local_idx = int(beat_indices[current_pos])
        wave = float(features["wave"][local_idx])
        impact = float(features["impact_score"][local_idx])
        step = adaptive_beat_step(wave, impact, section_type, pattern)
        # [FORK] Digital-Union (Creative Controls Core): `adaptive_beat_step` stays exactly the
        # musical mapping it has always been; density re-quantises its answer afterwards, so the
        # rhythmic reasoning and the creative control remain separable. `radius` below still keys
        # off the resulting step, so a denser edit keeps its tighter anchor search.
        if density_factor is not None:
            step = fork_creative.scale_beat_step(step, density_factor)

        target_pos = min(beat_indices.size - 1, current_pos + step)
        target_idx = choose_best_nearby(
            beat_indices,
            target_pos,
            radius=1 if step <= 2 else 2,
            scores=score_map,
            features=features,
        )
        if target_idx is None:
            break

        target_time = float(beat_times[target_idx])
        min_gap = min_interval_for_wave(float(features["wave"][target_idx]), section_type, cfg)

        # If too close, move one more rhythmic step forward instead of cutting fast.
        if target_time - last_cut_time < min_gap:
            current_pos = min(beat_indices.size - 1, target_pos + 1)
            continue

        # If the target score is weak and we are not exceeding max hold, let the
        # shot breathe until the next cleaner beat.
        score = score_map.get(int(target_idx), 0.0)
        if score < weak_score_threshold and target_time - last_cut_time < max_hold:
            current_pos = target_pos
            continue

        selected.append(target_time)
        last_cut_time = target_time
        current_pos = int(np.where(beat_indices == target_idx)[0][0])

        # Safety: if we somehow hold too long, force a clean bar/downbeat.
        if current_pos < beat_indices.size - 1:
            future = beat_indices[current_pos + 1:]
            if future.size:
                future_times = beat_times[future]
                too_far = np.where(future_times - last_cut_time >= max_hold)[0]
                if too_far.size and (future_times[too_far[0]] - last_cut_time) > max_hold * 1.15:
                    forced_pos = current_pos + 1 + int(too_far[0])
                    forced_idx = choose_best_nearby(beat_indices, forced_pos, radius=2, scores=score_map, features=features)
                    if forced_idx is not None:
                        forced_time = float(beat_times[forced_idx])
                        if forced_time - last_cut_time >= min_gap:
                            selected.append(forced_time)
                            last_cut_time = forced_time
                            current_pos = int(np.where(beat_indices == forced_idx)[0][0])

    return selected


def adaptive_beat_step(wave: float, impact: float, section_type: str, pattern: str) -> int:
    """Map musical energy to beat-step spacing. Bigger wave => faster, but still grid-like."""
    # Base steps are intentionally calmer than V3/V3.1.
    if section_type in {"intro", "outro", "breakdown"}:
        if wave > 0.82 and impact > 0.76:
            return 2
        if wave > 0.55:
            return 3
        return 4

    if section_type in {"verse", "bridge", "hook"}:
        if wave > 0.84 and impact > 0.72:
            return 2
        if wave > 0.56:
            return 3
        return 4

    if section_type in {"chorus"}:
        if wave > 0.88 and impact > 0.78:
            return 1
        if wave > 0.58:
            return 2
        return 3

    if section_type in {"drop", "finale"}:
        if wave > 0.90 and impact > 0.82 and pattern in {"kick_clap", "kick", "bass"}:
            return 1
        if wave > 0.62:
            return 2
        return 3

    return 3


def choose_best_nearby(beat_indices: np.ndarray, target_pos: int, radius: int,
                       scores: Dict[int, float], features: Dict) -> int | None:
    if beat_indices.size == 0:
        return None
    lo = max(0, target_pos - radius)
    hi = min(beat_indices.size, target_pos + radius + 1)
    candidates = beat_indices[lo:hi]
    if candidates.size == 0:
        return None

    def candidate_score(idx: int) -> float:
        s = scores.get(int(idx), 0.0)
        # Strongly prefer bar/phrase anchors when nearby. This makes cuts feel
        # locked to the music instead of slightly mismatched.
        if bool(features["is_phrase_anchor"][idx]):
            s += 0.22
        elif bool(features["is_bar_anchor"][idx]):
            s += 0.12
        # Do not move too far from the intended rhythmic target.
        pos_penalty = abs(int(np.where(beat_indices == idx)[0][0]) - target_pos) * 0.06
        return s - pos_penalty

    best = max(candidates.tolist(), key=candidate_score)
    return int(best)


def compute_cut_scores(beat_indices: np.ndarray, features: Dict, section: Dict,
                       cfg: AutoWaveConfig) -> np.ndarray:
    idx = beat_indices
    impact = features["impact_score"][idx]
    wave = features["wave"][idx]
    rhythm = features["rhythm_score"][idx]
    novelty = features["novelty"][idx]

    score = 0.36 * impact + 0.27 * rhythm + 0.20 * wave + 0.10 * novelty + 0.07 * features["arc"][idx]

    score = score.copy()
    score[features["is_bar_anchor"][idx]] += cfg.anchor_bonus
    score[features["is_phrase_anchor"][idx]] += cfg.phrase_bonus

    pattern = section.get("dominant_pattern", "mixed")
    if pattern == "kick_clap":
        score += 0.12 * features["is_strong_kick"][idx] + 0.10 * features["is_strong_clap"][idx]
    elif pattern == "kick":
        score += 0.16 * features["is_strong_kick"][idx]
    elif pattern == "bass":
        score += 0.16 * features["is_strong_bass"][idx]
    elif pattern == "clap":
        score += 0.14 * features["is_strong_clap"][idx]
    elif pattern == "hihat":
        # Hi-hat alone should not cause frantic switching.
        score += 0.05 * features["is_strong_hihat"][idx]

    return _normalize(score)


def min_interval_for_wave(wave: float, section_type: str, cfg: AutoWaveConfig) -> float:
    if section_type in {"intro", "outro", "breakdown"}:
        return cfg.low_energy_min_interval
    if wave >= 0.90 and section_type in {"drop", "finale"}:
        return cfg.peak_energy_min_interval
    if wave >= 0.68:
        return cfg.high_energy_min_interval
    if wave >= 0.38:
        return cfg.medium_energy_min_interval
    return cfg.low_energy_min_interval


def max_hold_for_section(section: Dict, cfg: AutoWaveConfig) -> float:
    section_type = section.get("type", "verse")
    energy = float(section.get("energy", 0.5))
    if section_type in {"intro", "outro", "breakdown"}:
        return cfg.low_energy_max_hold
    if section_type in {"drop", "finale"} and energy >= 0.78:
        return cfg.peak_energy_max_hold
    if section_type in {"chorus", "drop", "finale"}:
        return cfg.high_energy_max_hold
    if energy >= 0.55:
        return cfg.medium_energy_max_hold
    return cfg.low_energy_max_hold


def add_rare_micro_cuts(selected: np.ndarray, beat_times: np.ndarray, features: Dict,
                        audio_duration: float, cfg: AutoWaveConfig) -> np.ndarray:
    """Add extremely rare half-beat cuts only for huge impacts, not normal density.

    [FORK] Digital-Union (Creative Controls Core): Cut Density deliberately does **not** touch this
    policy. ``enable_rare_micro_cuts``, ``max_micro_cut_ratio``, ``micro_min_gap`` and
    ``micro_percentile`` are identical at every density. The absolute number of micro-cuts still
    moves, because ``max_extra`` is a ratio of the main selected grid, which density does change —
    that is a proportional consequence, not a policy change. **This is not the future Micro Cuts
    control**, which would be a separate creative control over these fields themselves.
    """
    if not cfg.enable_rare_micro_cuts or len(beat_times) < 3 or selected.size == 0:
        return selected

    beat_diffs = np.diff(beat_times)
    median_beat = float(np.median(beat_diffs)) if beat_diffs.size else 0.5
    if median_beat < 0.22:
        return selected

    threshold = _safe_percentile(features["impact_score"], cfg.micro_percentile, 0.97)
    max_extra = int(max(0, round(len(selected) * cfg.max_micro_cut_ratio)))
    if max_extra <= 0:
        return selected

    extras: List[float] = []
    selected_sorted = np.sort(selected)

    candidates = np.where((features["impact_score"] >= threshold) & (features["wave"] >= 0.88))[0]
    for idx in candidates:
        if len(extras) >= max_extra or idx >= len(beat_times) - 1:
            break
        t = float(beat_times[idx] + 0.5 * (beat_times[idx + 1] - beat_times[idx]))
        if t <= 0.0 or t >= audio_duration:
            continue
        nearest = np.min(np.abs(selected_sorted - t)) if selected_sorted.size else 999.0
        if nearest >= max(cfg.micro_min_gap, median_beat * 0.45):
            extras.append(t)

    if not extras:
        return selected
    return np.concatenate([selected, np.asarray(extras, dtype=float)])


def section_density_cleanup(selected, beat_indices: np.ndarray, beat_times: np.ndarray,
                            features: Dict, section: Dict,
                            cfg: AutoWaveConfig) -> np.ndarray:
    """[FORK] Digital-Union (Freestyle V1): ``final_wave_cleanup``'s density policy, for ONE section.

    The same four steps in the same order with the same arithmetic and the same anchor scoring — but
    scoped, which is the entire difference:

    * the ratio band comes from **this section's** beat count, not ``len(beat_times)``;
    * the cap ranks only **this section's** cuts, never the whole track;
    * anchor insertion may draw only on **this section's** beat indices;
    * nothing outside ``[section["start"], section["end"])`` is read or written.

    That scoping is what makes a Freestyle override local. The global version cannot be reused here
    at any density: its top-N ranking and its anchor pool both span the track, so one section's rule
    would silently reshape the others.

    Deterministic and pure: no RNG, no clock, no global state.
    """
    arr = np.asarray(selected, dtype=float).reshape(-1)
    arr = arr[np.isfinite(arr)]
    start = float(section["start"])
    end = float(section["end"])
    arr = arr[(arr >= start) & (arr < end)]
    if arr.size == 0:
        return arr

    arr = _unique_sorted(arr, cfg.peak_energy_min_interval)

    section_beats = int(np.asarray(beat_indices).size)
    max_allowed = int(max(1, round(section_beats * cfg.target_cut_ratio_max)))
    min_allowed = int(max(1, round(section_beats * cfg.target_cut_ratio_min)))

    # Section-local ratio cap: keep the strongest/most anchored cuts of THIS section.
    if arr.size > max_allowed:
        keep_scores = []
        for t in arr:
            idx = int(np.argmin(np.abs(beat_times - t)))
            s = float(features["impact_score"][idx])
            if bool(features["is_phrase_anchor"][idx]):
                s += 0.55
            elif bool(features["is_bar_anchor"][idx]):
                s += 0.32
            keep_scores.append(s)
        order = np.argsort(keep_scores)[::-1][:max_allowed]
        arr = np.sort(arr[order])

    # Section-local floor: add clean phrase/bar anchors from THIS section only.
    if arr.size < min_allowed and section_beats > 0:
        candidates = []
        for idx in np.asarray(beat_indices).reshape(-1):
            idx = int(idx)
            if bool(features["is_phrase_anchor"][idx]) or bool(features["is_bar_anchor"][idx]):
                candidates.append((float(features["impact_score"][idx]), float(beat_times[idx])))
        for _, t in sorted(candidates, reverse=True):
            if arr.size >= min_allowed:
                break
            if arr.size == 0 or np.min(np.abs(arr - t)) >= cfg.medium_energy_min_interval:
                arr = np.sort(np.append(arr, t))

    return _unique_sorted(arr, cfg.peak_energy_min_interval)


def cross_section_safety(cleaned: Dict, gaps: Dict) -> np.ndarray:
    """[FORK] Digital-Union (Freestyle V1): main-grid safety across section boundaries, only.

    Each section's cuts are already internally clean, so the one thing left unchecked is a pair of
    adjacent cuts that **straddle** a boundary. Only such pairs are examined. A single global
    ``_unique_sorted`` over the concatenated array would instead re-apply one gap *everywhere* and
    thin a dense section with a sparse section's policy — that is the locality leak, not a fix for
    it.

    Threshold is ``min(gap of the earlier section, gap of the later section)``: the denser side's
    own policy already permits that spacing, so enforcing the sparser side's larger gap would delete
    a cut the dense section legitimately produced.

    Resolution is **keep the earlier cut, drop the later one** — the same rule ``_unique_sorted``
    already documents ("V3.2 prefers stable downbeat timing over squeezing in nearby cuts"). If
    dropping the later section's first cut exposes another violating one, the scan continues
    deterministically, so a boundary ends safe or that section ends with no cut. Internal cuts of
    either already-cleaned section are never thinned.
    """
    flat = []
    for index in sorted(cleaned):
        for t in cleaned[index]:
            flat.append((float(t), int(index)))
    flat.sort()
    if not flat:
        return np.asarray([], dtype=float)

    kept = [flat[0]]
    for time_s, index in flat[1:]:
        previous_time, previous_index = kept[-1]
        if index == previous_index:
            kept.append((time_s, index))
            continue
        threshold = min(gaps.get(previous_index, 0.0), gaps.get(index, 0.0))
        if time_s - previous_time >= threshold:
            kept.append((time_s, index))
        # else: drop this later cut and compare the next one against the same earlier cut, which is
        # what makes "continue until the boundary is safe" a loop property rather than one check.
    return np.asarray([t for t, _index in kept], dtype=float)


def micro_extra_safety(main_grid: np.ndarray, with_micro: np.ndarray,
                       beat_times: np.ndarray, cfg: AutoWaveConfig) -> np.ndarray:
    """[FORK] Digital-Union (Freestyle V1): filter micro EXTRAS only. Never touches the main grid.

    ``add_rare_micro_cuts`` is left exactly as it is — including a measured pre-existing hole: it
    computes ``selected_sorted`` once, before its loop, and never adds an accepted extra back, so
    two extras can be accepted closer to each other than the layer's own floor. On the legacy
    uniform path that behaviour is frozen by contract and is **not** fixed here. On the
    heterogeneous path there is no ``final_wave_cleanup`` afterwards to lean on, so this pass closes
    it properly: extras are considered in deterministic time order against the main grid **plus the
    extras already accepted**, and each accepted extra joins the occupied set before the next is
    judged.

    The floor is the micro layer's **own** declared floor, ``max(cfg.micro_min_gap,
    median_beat * 0.45)`` — not an invented density floor. ``micro_min_gap`` is rewritten by neither
    ``density_scaled_config`` nor ``micro_cut_scaled_config``, so the floor is density-independent by
    construction.

    Main-grid cuts are identified by **exact float identity against the supplied grid**. Both arrays
    come out of the same float arithmetic, so equality is exact and a tolerance would only add a
    way to misclassify a near-grid extra as part of the grid. The choice is defensive rather than
    observable, and the honest reason to state it is the adjacent trap it avoids: the P0-R2 probe
    excluded a cut from its own distance check *by value* against a rounded set, so each extra
    matched itself and reported a zero gap. Here that cannot happen by construction — ``occupied``
    starts as the grid alone and an accepted extra is appended only *after* its own check.

    That same construction makes the ``grid_values`` filter **redundant for the output**, and it is
    kept for clarity rather than safety: a grid cut left in ``extras`` measures zero distance to
    itself in ``occupied`` and is rejected, while the grid is re-attached at the end regardless.
    Measured as an equivalent mutant — emptying ``grid_values`` changes no result on any fixture.
    Do not read the filter as a guard; the seeded ``occupied`` set is the guard.
    """
    grid = np.asarray(main_grid, dtype=float).reshape(-1)
    produced = np.asarray(with_micro, dtype=float).reshape(-1)
    if produced.size == 0:
        return np.sort(grid)

    grid_values = set(grid.tolist())
    extras = sorted(float(t) for t in produced.tolist() if float(t) not in grid_values)
    if not extras:
        return np.sort(grid)

    beat_diffs = np.diff(np.asarray(beat_times, dtype=float))
    median_beat = float(np.median(beat_diffs)) if beat_diffs.size else 0.5
    micro_floor = max(cfg.micro_min_gap, median_beat * 0.45)

    occupied = np.sort(grid)
    accepted: List[float] = []
    for t in extras:
        if occupied.size and float(np.min(np.abs(occupied - t))) < micro_floor:
            continue
        accepted.append(t)
        occupied = np.sort(np.append(occupied, t))

    if not accepted:
        return np.sort(grid)
    return np.sort(np.concatenate([grid, np.asarray(accepted, dtype=float)]))


def final_wave_cleanup(selected: np.ndarray, beat_times: np.ndarray, features: Dict,
                       audio_duration: float, cfg: AutoWaveConfig) -> np.ndarray:
    arr = np.asarray(selected, dtype=float)
    arr = arr[np.isfinite(arr)]
    arr = arr[(arr > 0.0) & (arr < audio_duration)]
    if arr.size == 0:
        return arr

    # Main global no-flicker pass. The effective minimum changes with energy, but
    # this conservative base removes most too-fast switches.
    base_gap = cfg.peak_energy_min_interval
    arr = _unique_sorted(arr, base_gap)

    # Global ratio cap: if still too dense, keep strongest/most anchored cuts.
    max_allowed = int(max(1, round(len(beat_times) * cfg.target_cut_ratio_max)))
    min_allowed = int(max(1, round(len(beat_times) * cfg.target_cut_ratio_min)))
    if arr.size > max_allowed:
        keep_scores = []
        for t in arr:
            idx = int(np.argmin(np.abs(beat_times - t)))
            s = float(features["impact_score"][idx])
            if bool(features["is_phrase_anchor"][idx]):
                s += 0.55
            elif bool(features["is_bar_anchor"][idx]):
                s += 0.32
            keep_scores.append(s)
        order = np.argsort(keep_scores)[::-1][:max_allowed]
        arr = np.sort(arr[order])

    # Make sure it does not become *too* sparse by adding clean phrase/bar anchors.
    if arr.size < min_allowed and len(beat_times) > 0:
        candidates = []
        for idx, t in enumerate(beat_times):
            if bool(features["is_phrase_anchor"][idx]) or bool(features["is_bar_anchor"][idx]):
                candidates.append((float(features["impact_score"][idx]), float(t)))
        for _, t in sorted(candidates, reverse=True):
            if arr.size >= min_allowed:
                break
            if np.min(np.abs(arr - t)) >= cfg.medium_energy_min_interval:
                arr = np.sort(np.append(arr, t))

    # Last pass with a slightly relaxed gap so high-energy drops can still breathe fast.
    arr = _unique_sorted(arr, cfg.peak_energy_min_interval)
    return arr
