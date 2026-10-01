#!/usr/bin/env python3
"""[FORK] Digital-Union: named Creative Preset recipes (Creative Controls Extra PR3).

A preset is a **named set of slider values and nothing else**. There is no preset mode, no preset
branch in any stage, and no persisted preset semantics::

    PRESET                = a name for six numbers           (UI convenience, ephemeral)
    THE SIX SLIDERS       = the sole execution truth          (already the whole contract)

Selecting a preset writes the six controls and then stops existing. The name never reaches
``CreativeProfile``, ``beat_info["creative"]``, ``process_video_guarded``, Stage 4, Stage 5, Stage 6,
``render_info`` or the output filename — so the planner has no idea presets exist, and a render is
still fully described (and fully reproducible) by the six integers the user ended up with. If a
recipe is ever retuned, old renders remain reproducible from *their recorded values* rather than from
a historical name, which is the whole reason the name is not persisted.

Kept in ``beatsync_fork`` and stdlib-only (CLAUDE.md's hard rule): this is a table of 24 integers and
three total functions, so it is testable on a bare interpreter. It deliberately does **not** import
:mod:`beatsync_fork.creative` either — a recipe is inert data that the existing
``normalize_control`` boundary happens to accept unchanged, and a test proves that rather than this
module enforcing it by coercion.

===============================================================================
Why the values are what they are
===============================================================================

Calibrated on real material (41 sources, one 278 s / 123 BPM track) by running the production
Stage 4 and Stage 6 per recipe against a fully warm analysis cache. Each preset is a *coherent
recipe*, not a diagonal through every slider:

* **Cinematic** is the only preset that moves Semantic Emphasis, because it is the only name that
  genuinely implies contextual reading. It also leaves Source Diversity neutral: at ~20% fewer cuts
  the source-reuse pressure is already lower, so the control is not spent where it is not needed.
* **Dynamic** is motion-led. Source Diversity has to reach 75 rather than 65 because Motion Bias 70
  concentrates the edit on high-motion sources, and at 65 the measured source concentration was
  *worse* than neutral.
* **High Energy** is pacing-led, and its Cut Density is deliberately 100. Cut Density has a known
  pre-existing non-monotonic region around 72-82 on real material, so an intermediate value
  measured barely faster than Dynamic; 100 is what makes the strongest named pacing recipe actually
  distinct. Consuming all of that control's headroom is the accepted cost.
* **High Energy is not "all sliders at 100"**, and Semantic Emphasis stays neutral in both Dynamic
  and High Energy: Stage 5 motion-gates semantic action, so on the action material a dense edit
  selects, the semantic and deterministic readings nearly coincide and moving the control would be
  near-inert.

Do not retune these without new measurement.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

#: The six 0..100 controls a preset writes, in the order the GUI lays them out and in the order
#: :func:`preset_values` and :func:`matching_preset` use. The Variation Seed is deliberately absent:
#: it is independent creative state with its own randomiser, and no preset may move it.
CREATIVE_CONTROL_FIELDS = (
    "cut_density",
    "micro_cuts",
    "semantic_emphasis",
    "energy_response",
    "motion_bias",
    "source_diversity",
)

#: The neutral recipe, and the reset-to-current-behaviour entry.
BALANCED_PRESET = "Balanced"

#: Not a recipe. It is the selector's way of saying "the six live values match no named recipe", so
#: it has no entry in :data:`PRESETS` and selecting it changes nothing.
CUSTOM_PRESET = "Custom"


def _recipe(cut_density: int, micro_cuts: int, semantic_emphasis: int,
            energy_response: int, motion_bias: int, source_diversity: int) -> Mapping[str, int]:
    """One immutable recipe, written positionally so a transposed value is visible at a glance."""
    return MappingProxyType({
        "cut_density": cut_density,
        "micro_cuts": micro_cuts,
        "semantic_emphasis": semantic_emphasis,
        "energy_response": energy_response,
        "motion_bias": motion_bias,
        "source_diversity": source_diversity,
    })


#: The four named recipes. Plain literal integers on purpose — the values stay inspectable rather
#: than being silently coerced at definition time, and the tests prove each one is a real ``int`` in
#: ``0..100`` that ``creative.normalize_control`` returns unchanged. ``MappingProxyType`` at both
#: levels so no caller can edit the table it is reading.
#:
#:                         density  micro  semantic  energy  motion  diversity
PRESETS: Mapping[str, Mapping[str, int]] = MappingProxyType({
    "Balanced":    _recipe(50,     50,    50,       50,     50,     50),
    "Cinematic":   _recipe(30,     25,    65,       40,     30,     50),
    "Dynamic":     _recipe(65,     60,    50,       70,     70,     75),
    "High Energy": _recipe(100,    85,    50,       85,     80,     80),
})

#: Selector order: the four recipes, then Custom last because it is a state rather than a choice.
PRESET_NAMES = tuple(PRESETS) + (CUSTOM_PRESET,)


def resolve_preset(name: Any) -> dict[str, int] | None:
    """The six values of one named recipe, or ``None`` when there is no recipe to apply.

    ``None`` is returned for :data:`CUSTOM_PRESET`, for an unknown name, and for anything that is not
    a string at all — one answer for "nothing to write", so the caller needs a single branch.

    Matching is **exact and case-sensitive**: ``resolve_preset("cinematic")`` is ``None``. Silently
    accepting a different spelling would make the selector's displayed value and the applied recipe
    two separately-decided things, and the point of this layer is that they are one.

    Returns a fresh ``dict`` so a caller may freely mutate the result without reaching
    :data:`PRESETS`.
    """
    if not isinstance(name, str):
        return None
    recipe = PRESETS.get(name)
    if recipe is None:
        return None
    return dict(recipe)


def preset_values(name: Any) -> tuple[int, ...] | None:
    """One named recipe as a tuple ordered by :data:`CREATIVE_CONTROL_FIELDS`, or ``None``.

    The ordering is the contract the GUI relies on: Gradio writes an ``outputs`` list positionally,
    so this tuple and the slider list have to agree, and both are derived from the one field order
    rather than restated.
    """
    recipe = resolve_preset(name)
    if recipe is None:
        return None
    return tuple(recipe[field] for field in CREATIVE_CONTROL_FIELDS)


def _is_control_number(value: Any) -> bool:
    """A real ``int``/``float``, never a ``bool``.

    The same explicit type boundary as ``creative.normalize_control``, for the same reason: ``bool``
    subclasses ``int``, so ``True == 1`` would let a checkbox-shaped value match a recipe whose
    value happened to be 1. Floats are accepted because a Gradio slider reports ``50.0``.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def matching_preset(values: Any) -> str:
    """The name of the recipe these six live control values are, or :data:`CUSTOM_PRESET`.

    This is what makes the selector an honest read-out rather than remembered state: it is
    recomputed from the sliders, so it reports ``Cinematic`` whenever the six values *are*
    Cinematic — including after a manual edit that happens to land back on the recipe — and
    ``Custom`` the moment they are not. There is deliberately no "last selected preset" anywhere.

    Total by construction. ``None``, a string, a wrong-length sequence, a ``bool`` element, a
    non-numeric element, ``NaN``, an object whose ``len()`` raises — every one of them is simply not
    a recipe, so they all answer :data:`CUSTOM_PRESET`. Nothing here may raise during a UI
    interaction.
    """
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        # A string of the right length would otherwise be compared character by character, and a
        # mapping would iterate its keys. Neither is a control tuple; both are Custom.
        return CUSTOM_PRESET
    try:
        candidate = tuple(values)
    except Exception:
        # Deliberately broad, and only around the one step that touches the untrusted value: a
        # non-iterable raises `TypeError`, but an object whose `__iter__` raises something else
        # would otherwise escape a narrower catch and abort a UI interaction. `BaseException`
        # still propagates, so `KeyboardInterrupt` is unaffected — the same boundary
        # `progress.emit()` draws. Everything below this line operates on a plain tuple of
        # verified numbers and cannot raise.
        return CUSTOM_PRESET
    if len(candidate) != len(CREATIVE_CONTROL_FIELDS):
        return CUSTOM_PRESET
    if not all(_is_control_number(value) for value in candidate):
        return CUSTOM_PRESET

    for name in PRESETS:
        if candidate == preset_values(name):
            return name
    return CUSTOM_PRESET


__all__ = [
    "BALANCED_PRESET",
    "CREATIVE_CONTROL_FIELDS",
    "CUSTOM_PRESET",
    "PRESETS",
    "PRESET_NAMES",
    "matching_preset",
    "preset_values",
    "resolve_preset",
]
