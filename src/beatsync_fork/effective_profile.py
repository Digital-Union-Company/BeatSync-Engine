"""One truthful sentence describing what the next render will actually do creatively.

The problem this exists for, stated as the owner found it: the Creative Preset selector can read
`Balanced` while Freestyle rules, a non-default Variation Seed, voice clips or SFX are all active.
Every one of those statements is individually true — the preset genuinely *is* a name for the six
slider values, and those values genuinely are Balanced — and the combination is misleading, because
nothing on screen says the render will not be a plain Balanced render.

So this module answers one question: **given the live creative state, what is the shortest honest
description of it?**

```
Balanced                                    six sliders match Balanced, nothing else active
Cinematic + Freestyle (2 section rules)     a named base, with an override named
Custom                                      the six match no recipe, nothing else active
Custom + Variation Seed 7 + Smart Mix       the six match no recipe, two overrides named
```

### Why there is no "based on Balanced"

The R8 task's examples included `Custom — based on Balanced`. That is **not implementable
truthfully here**, and the reason is a deliberate pre-existing design decision rather than an
oversight: this application stores no "last selected preset" anywhere, by rule and by test
(`.claude/rules/creative-presets.md`; `test_creative_presets.py`). Six integers describe a render
completely, so a preset name is recomputed from them and then stops existing — which is what lets
an old render reproduce from *its recorded values* instead of from a label whose recipe may since
have been retuned.

Introducing a remembered base purely to label the screen would reintroduce exactly the second
source of truth that decision removed, and would then be able to *disagree* with the sliders. So a
base is named **only when it is verifiable**: when the six live values match a recipe, that recipe
IS the base and is named; when they match none, there is no base to name and the answer is
`Custom`. Both answers are recomputed from live state every time, so neither can go stale.

### What counts as an override

Only things that are **observably active**, each checked against the authority that owns it rather
than by a local rule of thumb:

* **Freestyle** — through the caller's resolved declaration's own `is_active()`. Ten sections set
  to `Base` is *not* active, because `Base` means "inherit the live global value" and the renderer
  then takes the ordinary path. Reading the checkbox alone would claim an edit that will not
  happen.
* **Variation Seed** — non-zero, since 0 is the documented precision baseline.
* **Audio Layers** — voice clips actually supplied.
* **Smart Mix / SFX** — the fork record's own `plans_anything`, plus a folder to read from.
  `Amount 0` is a hard off-branch however many roles are ticked.

Everything here is **total**: it runs on live widget values during a UI interaction, so `None`, a
string where a number was expected, a mapping, and an object whose `__iter__` raises all produce a
sentence rather than an exception. A read-out that can crash a tab selection is worse than a
read-out that says little.

Stdlib only, and it imports `presets` for the one preset-label path that already exists — never
re-deriving it. It renders nothing, resolves nothing and reaches no stage: this is a **view**, and
the six sliders plus the declarations remain the only execution truth.
"""

from __future__ import annotations

from . import presets

#: What the summary says when the six controls match a recipe and nothing else is on.
#: Deliberately just the recipe's name: adding "and nothing else" to the common case would make
#: the one line an operator reads most often the longest one.
CUSTOM_LABEL = "Custom"

#: Separates the base from its overrides. A plus sign rather than prose, so the line stays short
#: enough to sit in a one-line read-out beside the render button.
_JOIN = " + "


def base_label(controls) -> str:
    """The name of the recipe the six live values match, or :data:`CUSTOM_LABEL`.

    Delegates to `presets.matching_preset`, which is already total and already the one
    preset-label path in the application -- a second implementation here could disagree with the
    selector on the Creative Settings tab, which is the exact class of defect this module exists
    to remove.
    """
    matched = presets.matching_preset(controls)
    return matched if isinstance(matched, str) and matched else CUSTOM_LABEL


def _seed_of(value) -> int:
    try:
        if isinstance(value, bool):
            return 0
        number = int(float(value))
    except (TypeError, ValueError):
        return 0
    return number


def _count_of(value) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def overrides(*, freestyle_declaration=None, variation_seed=0, voice_clips=None,
              smart_mix_active=False) -> list[str]:
    """The active overrides, in a fixed order, as short phrases.

    Order is fixed rather than incidental: the same state must always render the same string, or
    an operator comparing two screens would see a difference that is not there.

    `freestyle_declaration` is the caller's already-resolved declaration -- this module does not
    build one, because building it needs the ten-section vocabulary and the Random resolver, and
    duplicating that is how a read-out starts disagreeing with the renderer. Anything without a
    truthy `is_active()` counts as inactive, which is also what `None` means.

    `smart_mix_active` is likewise a verdict the caller obtained from the fork record, not a raw
    Amount this module would have to interpret.
    """
    active: list[str] = []

    rules = 0
    try:
        if freestyle_declaration is not None and freestyle_declaration.is_active():
            rules = _count_of(getattr(freestyle_declaration, "overrides", None) or ())
            active.append(f"Freestyle ({rules} section rule{'s' if rules != 1 else ''})"
                          if rules else "Freestyle")
    except Exception:                      # noqa: BLE001 - a read-out must never raise
        pass

    seed = _seed_of(variation_seed)
    if seed:
        active.append(f"Variation Seed {seed}")

    clips = _count_of(voice_clips) if voice_clips else 0
    if clips:
        active.append(f"Audio Layers ({clips} voice clip{'s' if clips != 1 else ''})")

    if smart_mix_active:
        active.append("Smart Mix")

    return active


def summary(controls, *, freestyle_declaration=None, variation_seed=0, voice_clips=None,
            smart_mix_active=False) -> str:
    """The one line. Never empty, never raises.

    The base is always stated, including when it is `Custom`, because "Freestyle" on its own does
    not tell an operator what the six controls are doing underneath it.
    """
    parts = [base_label(controls)]
    parts.extend(overrides(freestyle_declaration=freestyle_declaration,
                           variation_seed=variation_seed,
                           voice_clips=voice_clips,
                           smart_mix_active=smart_mix_active))
    return _JOIN.join(parts)


def summary_labelled(controls, *, freestyle_declaration=None, variation_seed=0, voice_clips=None,
                     smart_mix_active=False) -> str:
    """The same facts as :func:`summary`, labelled over one or two lines.

    [FORK] R8 Owner Fixes (P1). The owner confirmed the one-line form was *correct* --
    ``High Energy + Freestyle (10 section rules) + Variation Seed 367319`` -- and asked only that
    it be easier to read. Chaining four clauses with ``+`` makes the base preset and the things
    layered on top of it look like peers, when they are not: one is the recipe, the rest modify
    it.

    So this is a presentation of the SAME computation, not a second one. It calls
    :func:`base_label` and :func:`overrides` exactly as :func:`summary` does, which is what keeps
    the two incapable of disagreeing. :func:`summary` is unchanged and still used wherever a
    single line is wanted.

    With nothing layered on, the second line is omitted rather than printed empty -- "Active
    variations: none" invites the reader to look for something that is not there.
    """
    active = overrides(freestyle_declaration=freestyle_declaration,
                       variation_seed=variation_seed,
                       voice_clips=voice_clips,
                       smart_mix_active=smart_mix_active)
    head = f"Base preset: {base_label(controls)}"
    if not active:
        return head
    return f"{head}\nActive variations: {', '.join(active)}"


def is_modified(controls, *, freestyle_declaration=None, variation_seed=0, voice_clips=None,
                smart_mix_active=False) -> bool:
    """True when the render is not a plain named-preset render.

    Either the six values match no recipe, or something else is active. This is the predicate the
    UI uses to decide whether to offer a reset, and it is deliberately **not** derived by parsing
    :func:`summary` -- a string a human reads and a boolean a control depends on should not share
    a parser.
    """
    if base_label(controls) == CUSTOM_LABEL:
        return True
    return bool(overrides(freestyle_declaration=freestyle_declaration,
                          variation_seed=variation_seed,
                          voice_clips=voice_clips,
                          smart_mix_active=smart_mix_active))


__all__ = ["CUSTOM_LABEL", "base_label", "overrides", "summary", "is_modified"]
