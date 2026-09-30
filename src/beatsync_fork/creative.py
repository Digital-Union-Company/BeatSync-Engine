#!/usr/bin/env python3
"""[FORK] Digital-Union: the resolved Creative Profile (B0 + Creative Controls Core).

P2 drew the architectural line this module sits on::

    STAGE 5                    = INTRINSIC MEDIA TRUTH     (persistent, cached)
    STAGE 6 / creative layers   = CREATIVE INTERPRETATION   (ephemeral, per render)

Phase A put a single creative control — the variation seed — on ``beat_info["creative"]`` as
``{"seed": n}``. B0 generalises that one-key dict into a *profile*: the seed plus three new
0..100 controls, normalised once at the pipeline boundary and read only by the stages that own
them.

===============================================================================
Stage ownership
===============================================================================

======================  ==========================================================
Control                 Owned by
======================  ==========================================================
Variation Seed          Stage 6 — which candidate wins among the good ones
Cut Density             Stage 4 — how many beats become cuts
Energy Response         Stage 6 — how hard scoring follows the segment's target
Motion Bias             Stage 6 — calm vs. dynamic material preference
======================  ==========================================================

Stages 1-3 read **none** of it (beat grid, audio features and sections are facts about the track),
and **Stage 5 reads none of it either** — that is the B0 invariant. Creative state never enters
``_qwen_config_token``, ``_video_signature``, ``_cache_path``, a Qwen request, the Qwen prompt or a
persisted semantic payload, so no creative control can ever trigger a cache rebuild.

Cut Density legitimately changes ``selected_beats``, and therefore may legitimately change
``audio_visual_profile`` (``average_cut_interval``, ``cut_count``, even ``smart_preset``). That is
allowed: since P2 the audio profile has **zero executable effect** inside Stage 5, so nothing it
derives can reach a cache key. The invariant is not "Cut Density must not change the audio profile";
it is "whatever Cut Density changes upstream must never change Stage-5 cache identity".

===============================================================================
50 is today
===============================================================================

``seed=0, cut_density=50, energy_response=50, motion_bias=50`` reproduces current main **exactly** —
the same selected cut times, the same segment targets, the same candidate choices, the same legacy
RNG stream and the same output filenames. That is a product contract, not an approximation: every
neutral control takes an explicit branch that calls the pre-existing legacy path rather than running
a neutral-valued version of the new arithmetic, because even an operation-order change would make
"upgrade and change nothing" untrue.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule) because the whole thing is
arithmetic over four small integers: it is testable on a bare interpreter with no numpy, no Gradio,
no CUDA and no FFmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from beatsync_fork import variation as fork_variation

#: The neutral value of every 0..100 control. 50 means "exactly what BeatSync does today".
DEFAULT_CONTROL = 50

#: Inclusive bounds of every 0..100 control.
CONTROL_MIN = 0
CONTROL_MAX = 100

# ---------------------------------------------------------------------------
# Cut Density (Stage 4)
# ---------------------------------------------------------------------------

#: Density factor at the extremes: ``f = 2 ** ((cut_density - 50) / 50)``, so 0 -> 0.5, 50 -> 1.0
#: and 100 -> 2.0. Minimum intervals and maximum holds are divided by ``f`` and the global cut-ratio
#: band is multiplied by it, so a larger factor means "cut more often" throughout Stage 4.
_DENSITY_HALF_RANGE = 50.0

#: Caps on the scaled global cut-ratio band. At the reachable factor range (0.5 .. 2.0) neither cap
#: actually binds — 0.46 * 2 = 0.92 — so they are a guard against a future wider mapping rather than
#: part of the measured behaviour.
CUT_RATIO_MIN_CAP = 0.95
CUT_RATIO_MAX_CAP = 0.98

#: Stage 4's beat-step spacing is a small integer, so it is scaled and re-quantised rather than
#: divided. These are the bounds the selector's own ``adaptive_beat_step`` already lives inside.
BEAT_STEP_MIN = 1
BEAT_STEP_MAX = 8

#: Stage 4's "let a weak beat breathe" threshold. Scaled by ``1 / f``, so a denser edit tolerates
#: weaker beats as cuts. The literal is Stage 4's own existing constant, repeated here only as the
#: thing being scaled — ``stage4_select`` still owns it.
WEAK_SCORE_THRESHOLD = 0.42

# ---------------------------------------------------------------------------
# Energy Response (Stage 6)
# ---------------------------------------------------------------------------

#: How far Energy Response may pull the target-match blend away from 1.0. ``factor = 1 + d * 0.6``
#: with ``d = (energy_response - 50) / 50``, so 0 -> 0.40, 50 -> 1.00, 100 -> 1.60.
ENERGY_RESPONSE_SPAN = 0.6

# ---------------------------------------------------------------------------
# Motion Bias (Stage 6)
# ---------------------------------------------------------------------------

#: Motion Bias shift coefficient: ``centered * 0.15 * (2 * motion - 1)``.
#:
#: 0.15 is large enough to be visible against the seeded selector's ``SCORE_WINDOW = 0.12``, and
#: materially smaller than the planner's own ``0.28`` "seen recently" penalty — so a bias can move
#: the winner among comparable candidates but can never overturn a deliberate anti-repeat decision.
MOTION_BIAS_COEFFICIENT = 0.15


def normalize_control(value: Any) -> int:
    """Coerce any UI/CLI value to a usable 0..100 control; anything malformed is neutral.

    The same explicit type boundary as :func:`variation.normalize_seed`, for the same reasons: a
    Gradio slider yields floats, an emptied box yields ``None``, and a hand-typed or API-supplied
    value can be anything at all. None of it may raise in the middle of a render.

    Where this deliberately differs from the seed is **clamping**. A control has a real range with a
    meaningful end, so ``120`` is an unambiguous request for "as dense as possible" and becomes
    ``100``; ``-10`` becomes ``0``. A *fractional* value is different in kind — ``50.5`` is not a
    request for 50, and flooring it would silently render a setting the user never chose — so it
    falls back to :data:`DEFAULT_CONTROL` rather than acquiring a guessed meaning.

    ``bool`` is rejected first because it subclasses ``int``, so ``True`` would otherwise be read as
    ``1`` — "almost maximally sparse" — from a widget that never meant to say that.
    """
    if isinstance(value, bool):
        return DEFAULT_CONTROL
    if isinstance(value, int):
        return _clamp_control(value)
    if isinstance(value, float):
        # `is_integer()` is False for NaN and both infinities, so they need no separate guard.
        if value.is_integer():
            return _clamp_control(int(value))
        return DEFAULT_CONTROL
    if isinstance(value, str):
        text = value.strip()
        negative = text.startswith("-")
        digits = text[1:] if negative else text
        if digits.isdecimal():
            # A negative string is accepted (and clamped) so `--cut-density=-10` means the same
            # thing as the integer `-10`; only a plain decimal integer is accepted, never "50.0".
            return _clamp_control(-int(digits) if negative else int(digits))
    return DEFAULT_CONTROL


def _clamp_control(value: int) -> int:
    return max(CONTROL_MIN, min(CONTROL_MAX, value))


@dataclass(frozen=True)
class ScoringControls:
    """The Stage-6 scoring half of a resolved profile, as the planner needs it.

    ``None`` means *neutral*, and that is load-bearing rather than a stylistic default: it lets every
    scoring seam take an explicit "do exactly what main does" branch instead of running the new
    arithmetic with a neutral-valued coefficient. ``flow + 1.0 * (target - flow)`` is algebraically
    ``target`` but is not the same floating-point expression, and ``score + 0.0`` is a different
    operation from ``score``.

    Render-scoped: both values are constants for the whole plan, which is why they add no dimension
    to the L1A ``candidate × target`` precompute table.
    """

    energy_factor: float | None = None
    motion_centered: float | None = None

    @property
    def is_neutral(self) -> bool:
        return self.energy_factor is None and self.motion_centered is None

    @property
    def needs_flow_column(self) -> bool:
        """Energy Response blends each target against the generic "flow" score, so that column of
        the static table has to exist even when no segment in this plan actually targets flow."""
        return self.energy_factor is not None


#: The scoring controls of an all-neutral render: current main, untouched.
NEUTRAL_SCORING = ScoringControls()


@dataclass(frozen=True)
class CreativeProfile:
    """One render's resolved creative intent. Immutable, normalised on construction.

    Every field is normalised in ``__post_init__``, so an instance obtained by *any* route — the
    dataclass constructor, :meth:`from_widgets`, :meth:`from_mapping`, or a round trip through
    :meth:`as_dict` — is always valid. There is no such thing as a half-trusted profile.
    """

    seed: int = fork_variation.LEGACY_SEED
    cut_density: int = DEFAULT_CONTROL
    energy_response: int = DEFAULT_CONTROL
    motion_bias: int = DEFAULT_CONTROL

    def __post_init__(self) -> None:
        # Seed normalisation is delegated, never reimplemented: `variation` is the single authority
        # on what a seed means, and seed 0 legacy behaviour is frozen there.
        object.__setattr__(self, "seed", fork_variation.normalize_seed(self.seed))
        object.__setattr__(self, "cut_density", normalize_control(self.cut_density))
        object.__setattr__(self, "energy_response", normalize_control(self.energy_response))
        object.__setattr__(self, "motion_bias", normalize_control(self.motion_bias))

    # -- construction -------------------------------------------------------

    @classmethod
    def from_widgets(cls, seed: Any = None, cut_density: Any = None,
                     energy_response: Any = None, motion_bias: Any = None) -> "CreativeProfile":
        """Collapse four raw UI/CLI values into one profile. ``None`` anywhere means neutral."""
        return cls(
            seed=fork_variation.LEGACY_SEED if seed is None else seed,
            cut_density=DEFAULT_CONTROL if cut_density is None else cut_density,
            energy_response=DEFAULT_CONTROL if energy_response is None else energy_response,
            motion_bias=DEFAULT_CONTROL if motion_bias is None else motion_bias,
        )

    @classmethod
    def from_mapping(cls, mapping: Any) -> "CreativeProfile":
        """Read a profile off the ``beat_info["creative"]`` bus, defensively.

        Anything that is not a mapping — absent, ``None``, a string, a stale Phase A value — is a
        neutral profile, because every downstream reader must survive an old or malformed bus
        without raising mid-render. A Phase A ``{"seed": n}`` dict still resolves correctly: the
        three missing keys simply stay neutral.
        """
        if not isinstance(mapping, Mapping):
            return cls()
        return cls.from_widgets(
            seed=mapping.get("seed"),
            cut_density=mapping.get("cut_density"),
            energy_response=mapping.get("energy_response"),
            motion_bias=mapping.get("motion_bias"),
        )

    # -- transport ----------------------------------------------------------

    def as_dict(self) -> dict:
        """The authoritative serialisation, used for ``beat_info["creative"]`` and diagnostics.

        A plain ``dict`` of plain ``int``s: it rides on the shared bus, lands in ``render_info`` and
        in the plan summary, and must stay JSON-friendly and mutation-safe (a fresh dict each call).
        """
        return {
            "seed": self.seed,
            "cut_density": self.cut_density,
            "energy_response": self.energy_response,
            "motion_bias": self.motion_bias,
        }

    # -- neutrality ---------------------------------------------------------

    def is_neutral_cuts(self) -> bool:
        """True when Stage 4 must run its untouched legacy path."""
        return self.cut_density == DEFAULT_CONTROL

    def is_neutral_scoring(self) -> bool:
        """True when Stage 6's static scoring must be today's arithmetic, unmodified."""
        return (self.energy_response == DEFAULT_CONTROL
                and self.motion_bias == DEFAULT_CONTROL)

    def is_neutral(self) -> bool:
        """True when this whole render is current main: legacy seed and three neutral controls."""
        return (self.seed == fork_variation.LEGACY_SEED
                and self.is_neutral_cuts()
                and self.is_neutral_scoring())

    # -- derived values -----------------------------------------------------

    def cut_density_factor(self) -> float:
        """``2 ** ((cut_density - 50) / 50)`` — 0.5 at 0, exactly 1.0 at 50, 2.0 at 100.

        Exponential rather than linear so the control is symmetric in *ratio*: 25 halves the way to
        0.5 and 75 doubles the way to 2.0, which is how cut spacing is actually perceived. Callers
        must still branch on :meth:`is_neutral_cuts` rather than comparing this to 1.0.
        """
        return 2.0 ** ((self.cut_density - DEFAULT_CONTROL) / _DENSITY_HALF_RANGE)

    def energy_factor(self) -> float:
        """``1 + ((energy_response - 50) / 50) * 0.6`` — 0.40 at 0, 1.00 at 50, 1.60 at 100."""
        return 1.0 + ((self.energy_response - DEFAULT_CONTROL) / _DENSITY_HALF_RANGE) * ENERGY_RESPONSE_SPAN

    def motion_centered(self) -> float:
        """``(motion_bias - 50) / 50`` — -1.0 at 0 (calm), 0.0 at 50, +1.0 at 100 (dynamic)."""
        return (self.motion_bias - DEFAULT_CONTROL) / _DENSITY_HALF_RANGE

    def scoring_controls(self) -> ScoringControls:
        """The Stage-6 half, with ``None`` for each control that is neutral.

        Each control is decided independently, so a render can be non-neutral in Energy Response
        while Motion Bias still costs exactly nothing.
        """
        if self.is_neutral_scoring():
            return NEUTRAL_SCORING
        return ScoringControls(
            energy_factor=(None if self.energy_response == DEFAULT_CONTROL else self.energy_factor()),
            motion_centered=(None if self.motion_bias == DEFAULT_CONTROL else self.motion_centered()),
        )

    # -- reporting ----------------------------------------------------------

    def describe_seed(self) -> str:
        """``legacy`` or ``seed 381944`` — delegated, so seed wording never forks."""
        return fork_variation.describe(self.seed)

    def filename_suffix(self) -> str:
        """``_seed381944`` for a seeded render, empty otherwise.

        The three new controls deliberately contribute **nothing** to the filename: today's names
        must not change, and the resolved profile is recorded in the render diagnostics instead.
        """
        return fork_variation.filename_suffix(self.seed)

    def describe(self) -> str:
        """One compact line for the console and the plan summary.

        ``legacy`` when the whole profile is current main, so an ordinary render's console output is
        character-for-character what it is today.
        """
        if self.is_neutral():
            return "legacy"
        seed_text = f"Seed {self.seed}" if self.seed else "Seed default"
        return " · ".join([
            seed_text,
            f"Cut Density {self.cut_density}",
            f"Energy Response {self.energy_response}",
            f"Motion Bias {self.motion_bias}",
        ])


#: The all-neutral profile: seed 0 plus three 50s, i.e. exactly current main.
NEUTRAL_PROFILE = CreativeProfile()


def scale_beat_step(step: int, factor: float) -> int:
    """Re-quantise one of Stage 4's small integer beat steps for a density factor.

    Stage 4 moves through a section in whole beats, so density cannot be applied to the step by
    plain division — the result has to land back on the grid. ``floor(step / factor + 0.5)`` is
    explicit **half-up** rounding rather than :func:`round`, whose banker's rounding would send
    ``step=3, factor=2`` (1.5) to 2 and ``step=5, factor=2`` (2.5) to 2 as well, quietly flattening
    two different musical situations onto the same spacing.

    The result stays inside ``[1, 8]``: 1 is a cut on every beat, and 8 is one phrase, which is as
    far as the selector's own ``adaptive_beat_step`` ever reaches.
    """
    scaled = int(step / factor + 0.5)
    return max(BEAT_STEP_MIN, min(BEAT_STEP_MAX, scaled))


def scale_weak_score_threshold(factor: float) -> float:
    """Stage 4's breathing threshold under a density factor: ``0.42 / f``.

    Denser edits accept weaker beats as cuts; sparser edits demand stronger ones. Expressed as a
    division so the neutral factor 1.0 is the untouched constant — though the neutral *path* never
    calls this at all.
    """
    return WEAK_SCORE_THRESHOLD / factor


__all__ = [
    "CONTROL_MAX",
    "CONTROL_MIN",
    "CUT_RATIO_MAX_CAP",
    "CUT_RATIO_MIN_CAP",
    "DEFAULT_CONTROL",
    "ENERGY_RESPONSE_SPAN",
    "MOTION_BIAS_COEFFICIENT",
    "NEUTRAL_PROFILE",
    "NEUTRAL_SCORING",
    "WEAK_SCORE_THRESHOLD",
    "CreativeProfile",
    "ScoringControls",
    "normalize_control",
    "scale_beat_step",
    "scale_weak_score_threshold",
]
