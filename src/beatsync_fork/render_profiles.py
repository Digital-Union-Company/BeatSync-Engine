"""What a finished music video should look like: the render's Output Video Profile.

Before R8 the render had one video setting -- a free-text "Custom FPS" box -- and took its
**resolution from the first source video**, then fitted every other clip to it with a bare
``scale=width:height``. Two things follow from that, and the second is a real defect:

* there was no way to ask for a particular delivery format; and
* **any source whose aspect ratio differed from the first one was stretched.** A portrait phone
  clip in a 1920x1080 timeline came out squashed, not pillarboxed, and a 4:3 or anamorphic source
  the same.

A profile fixes both: it states the exact output dimensions and frame rate, and the geometry is
produced by the **measured** scale-and-pad chain the Raw Cutter already used --
:func:`beatsync_fork.raw_cutter_profiles.geometry_filter`, reused rather than reimplemented.

### Auto is not a profile, and that is the compatibility contract

:func:`resolve` answers ``None`` for :data:`AUTO`, and every caller must then take its existing
path **untouched**: first-source resolution, auto-detected frame rate, bare ``scale``. The legacy
behaviour is not re-derived from a "source profile" -- it is simply not entered. That is the same
contract `raw_cutter_profiles.SAME_AS_SOURCE` carries, for the same reason, and it is what makes
"Auto must preserve the historical behaviour" a structural fact rather than a hope.

### Why this is a separate module from the Raw Cutter's

The two workflows are distinct and must stay so. A Raw Cutter export profile describes one clip
cut out of one recording; a render output profile describes the single assembled music video. They
have different defaults (`Same as source` versus `Auto`), different manifests, and different
reasons to grow -- the cutter may one day want "match the source exactly", which is meaningless
here. What they genuinely share is the pixel geometry, and that is the one thing imported.

Pure argument assembly -- no FFmpeg, no subprocess, no filesystem. Stdlib only, so the fork
package's no-runtime-import rule still holds.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import raw_cutter_profiles

#: The identifier carried on the GUI radio and recorded in `render_info`.
AUTO = "auto"

AUTO_LABEL = "Auto — match the first source (current behaviour)"


@dataclass(frozen=True)
class RenderProfile:
    key: str
    label: str
    width: int
    height: int
    fps: int

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"


#: Deliberately the four common delivery shapes the owner asked for first, and deliberately no
#: 60 fps yet. Adding one is a row here plus nothing else: the choices, the resolution and the
#: filter chain are all derived from this table.
PROFILES: tuple[RenderProfile, ...] = (
    RenderProfile("1080p30", "1080p · 30 fps", 1920, 1080, 30),
    RenderProfile("1080p25", "1080p · 25 fps", 1920, 1080, 25),
    RenderProfile("720p30", "720p · 30 fps", 1280, 720, 30),
    RenderProfile("720p25", "720p · 25 fps", 1280, 720, 25),
)

_BY_KEY = {profile.key: profile for profile in PROFILES}


def profile_choices() -> list[tuple[str, str]]:
    """`(label, value)` pairs for a Gradio radio, with Auto first and therefore default."""
    return [(AUTO_LABEL, AUTO)] + [(p.label, p.key) for p in PROFILES]


def resolve(key) -> RenderProfile | None:
    """The profile for `key`, or **None** meaning *leave the render exactly as it was*.

    Anything unrecognised -- ``None``, ``""``, a number, a stale value from an older session --
    resolves to ``None``. Falling back to the untouched legacy path is the safe direction: the
    worst outcome is a render that keeps the first source's geometry, never a render reshaped by a
    half-understood setting.
    """
    if not isinstance(key, str):
        return None
    return _BY_KEY.get(key.strip())


def label_for(key) -> str:
    profile = resolve(key)
    return profile.label if profile else AUTO_LABEL


def target_size(key, fallback) -> tuple:
    """The output dimensions: the profile's own, or `fallback` under Auto.

    `fallback` is whatever the caller already computed -- in practice the first source's
    resolution -- and is returned **unchanged and un-inspected** under Auto, so Auto cannot be
    altered by anything in this module.
    """
    profile = resolve(key)
    return (profile.width, profile.height) if profile else fallback


def target_fps(key, fallback):
    """The output frame rate: the profile's own, or `fallback` under Auto. Same contract."""
    profile = resolve(key)
    return profile.fps if profile else fallback


def geometry_filter(key) -> str:
    """The scale-and-pad chain for a fixed profile, or ``""`` under Auto.

    ``""`` means "the caller emits whatever it emitted before" -- it is **not** a no-op filter to
    splice in, because splicing anything into the Auto path would make Auto a new code path rather
    than the untouched one.

    The chain itself comes from the Raw Cutter's measured helper. Fitting is by padding, never
    stretching, and the source's sample aspect ratio is squared first -- so a portrait, 4:3 or
    anamorphic source is centred with its proportions intact instead of being distorted.
    """
    profile = resolve(key)
    if profile is None:
        return ""
    return raw_cutter_profiles.geometry_filter(profile.width, profile.height)


def describe(key, *, resolution: str = "", fps=None, encoder: str = "",
             output_dir: str = "") -> str:
    """The one line the Render tab shows: what was actually produced, and where.

    Every field is supplied by the caller rather than derived here, because the point of the line
    is to report the **real** outcome -- under Auto the resolution and frame rate are discovered
    from the source at render time, and a module that guessed them would be describing its own
    intentions instead of the result.
    """
    parts = []
    if resolution:
        parts.append(str(resolution))
    if fps:
        try:
            parts.append(f"{float(fps):g} fps")
        except (TypeError, ValueError):
            pass
    if encoder:
        parts.append(str(encoder))
    head = " · ".join(parts) if parts else label_for(key)
    if output_dir:
        return f"{head}\n{output_dir}"
    return head


__all__ = ["AUTO", "AUTO_LABEL", "RenderProfile", "PROFILES", "profile_choices", "resolve",
           "label_for", "target_size", "target_fps", "geometry_filter", "describe"]
