"""Output video profiles for the Raw Cutter: what an exported clip should look like.

A folder of recordings is usually a folder of *different* recordings — 1080p30 from one camera,
720p25 from another, something anamorphic from a phone. The Raw Cutter has always exported each
clip with its source's own geometry and timing, which is right for archiving and wrong for
building a library you can cut with. A profile says "make every clip in this job look like this".

**Same as source is the default and is not a profile at all.** :func:`resolve` answers ``None``
for it, and the caller must then emit its existing command unchanged. That is the whole
compatibility contract: the legacy path is not re-derived from a "source profile", it is simply
not touched.

Pure argument assembly — no FFmpeg, no subprocess, no filesystem. Stdlib only, so the fork
package's no-runtime-import rule still holds.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The identifier stored in the batch manifest and carried on the GUI radio.
SAME_AS_SOURCE = "source"


@dataclass(frozen=True)
class Profile:
    key: str
    label: str
    width: int
    height: int
    fps: int


#: Deliberately four, and deliberately no 60 fps yet: the owner asked for the common delivery
#: shapes first. Adding one is a row here plus a line in the choices.
PROFILES: tuple[Profile, ...] = (
    Profile("1080p30", "1080p · 30 fps", 1920, 1080, 30),
    Profile("1080p25", "1080p · 25 fps", 1920, 1080, 25),
    Profile("720p30", "720p · 30 fps", 1280, 720, 30),
    Profile("720p25", "720p · 25 fps", 1280, 720, 25),
)

_BY_KEY = {profile.key: profile for profile in PROFILES}

SAME_AS_SOURCE_LABEL = "Same as source (no conversion)"


def profile_choices() -> list[tuple[str, str]]:
    """`(label, value)` pairs for a Gradio radio, with Same as source first."""
    return [(SAME_AS_SOURCE_LABEL, SAME_AS_SOURCE)] + [(p.label, p.key) for p in PROFILES]


def resolve(key) -> Profile | None:
    """The profile for `key`, or **None** meaning *leave the export exactly as it was*.

    Anything unrecognised — ``None``, ``""``, a stale value from an old session — resolves to
    ``None``. Falling back to the untouched legacy path is the safe direction: the worst outcome
    is a clip that keeps its source geometry, never a clip converted by a half-understood setting.
    """
    if not isinstance(key, str):
        return None
    return _BY_KEY.get(key.strip())


def label_for(key) -> str:
    profile = resolve(key)
    return profile.label if profile else SAME_AS_SOURCE_LABEL


def manifest_value(key) -> str:
    """What goes in `batch_manifest.json`. Always a string, always one of the known keys."""
    profile = resolve(key)
    return profile.key if profile else SAME_AS_SOURCE


def geometry_filter(width: int, height: int) -> str:
    """The pixel-geometry half of a fixed profile: fit into `width`x`height` WITHOUT stretching.

    Extracted from :func:`video_filter` by the R8 UX Refresh so the music-video renderer can reuse
    the *measured* chain instead of growing a second, differently-wrong one. The renderer's
    extraction path had only ``scale=width:height``, which distorts any source whose aspect ratio
    differs from the target -- a portrait phone clip rendered into a 16:9 timeline came out
    squashed rather than pillarboxed.

    Nothing about this chain changed in the extraction: :func:`video_filter` composes it and
    produces a byte-identical string to the one it produced before, which is asserted by test. The
    two workflows stay distinct -- the cutter owns its ``trim``, the renderer owns its own -- and
    only this pure geometry step is shared.

    In order, and every step is here for a measured reason:

    * ``scale=trunc(iw*sar/2)*2:ih,setsar=1`` squares the pixels **before** any aspect maths. A
      720x576 frame with SAR 16:15 is a 4:3 picture, not a 5:4 one; fitting it by coded size would
      letterbox it wrongly and squash the content. Measured on an anamorphic fixture: with this
      step the result pillarboxes as 4:3; without it the picture is distorted.
    * ``force_original_aspect_ratio=decrease`` then ``pad`` centres the picture in the target
      frame. **Padding, never stretching** -- a 9:16 phone clip becomes a pillarboxed 16:9 frame
      with its proportions intact.
    * ``setsar=1`` so the output declares square pixels, and the padded frame's DAR is the
      target's own.

    ``trunc(.../2)*2`` keeps every intermediate dimension even, which yuv420p requires.
    """
    return (
        f"scale=trunc(iw*sar/2)*2:ih,setsar=1,"
        f"scale={int(width)}:{int(height)}:force_original_aspect_ratio=decrease,"
        f"pad={int(width)}:{int(height)}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1"
    )


def video_filter(profile: Profile, duration: float) -> str:
    """The `-vf` chain for a fixed profile, in the one order that is correct.

    ``trim``/``setpts`` stay **first and unchanged**, so the profile cannot move a cut: the clip's
    membership is still decided by the frames' own timestamps exactly as the legacy path decides
    it, and only then is the result reshaped.

    Then :func:`geometry_filter` squares the pixels and pads to the target, and finally ``fps``
    converts to the constant target rate -- last, and it duplicates or drops frames to get there;
    it does **not** retime, so playback speed is unchanged.
    """
    return (
        f"trim=end={duration:.6f},setpts=PTS-STARTPTS,"
        f"{geometry_filter(profile.width, profile.height)},"
        f"fps={profile.fps}"
    )


def timing_options(profile: Profile) -> list[str]:
    """The output timing options for a fixed profile.

    **`-enc_time_base:v demux` is deliberately absent, and that is the whole point of this
    function.** The legacy Same-as-source command needs it, because there the goal is to keep the
    source's own irregular timestamps and the source's time base is the only base that can
    represent them. A fixed profile wants the opposite — one regular grid — and carrying the
    option across produces nonsense. Measured on this FFmpeg, exporting 4.0 s at 25 fps:

    ====================================== ================= ============ =================
    output options                         avg_frame_rate    frames       frame spacing
    ====================================== ================= ============ =================
    ``-fps_mode:v cfr -enc_time_base:v demux``  15360/1       **61440**    ~0.00006 s
    ``-fps_mode:v cfr``                         **25/1**      **100**      **0.04000 s**
    ====================================== ================= ============ =================

    The first row is not a rounding artefact: the clip is stamped on the demuxer's tick rate, so
    every tick becomes a frame. The second is exactly 4.0 s of genuine CFR. So the fixed branch
    states ``cfr`` and says nothing about the time base, letting the muxer derive one from the
    frame rate it was actually given.
    """
    return ["-fps_mode:v", "cfr"]


__all__ = ["SAME_AS_SOURCE", "SAME_AS_SOURCE_LABEL", "Profile", "PROFILES", "profile_choices",
           "resolve", "label_for", "manifest_value", "geometry_filter", "video_filter",
           "timing_options"]
