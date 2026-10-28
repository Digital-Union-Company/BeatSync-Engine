"""The render's Output Video Profile (R8 UX Refresh, Phase 6).

The defect this exists for: the render took its resolution from the **first source video** and
fitted every other clip to it with a bare ``scale=width:height``, so a portrait, 4:3 or anamorphic
source was **stretched**, not padded. There was also no way to ask for a particular delivery
format -- the only video setting was a free-text FPS box.

Two claims carry the whole feature and both are asserted here:

1. **Auto is the untouched historical path.** Not "similar to", not "equivalent in the cases we
   tried" -- structurally the same code, because every helper returns its caller's fallback
   unchanged and the geometry filter is the empty string, which the extractor treats as "emit what
   you always emitted".
2. **A fixed profile pads, never stretches.** The geometry comes from the Raw Cutter's measured
   chain, reused rather than reimplemented, and the extraction REPLACES the bare scale with it
   rather than appending (running both would fit the picture twice).

The pixel-level proof -- 4 profiles x 5 source shapes through real FFmpeg, checked with FFprobe --
is a separate manual verification recorded in `docs/claude/r8-ux-refresh.md`; what is here is
everything provable on a bare interpreter.
"""

from __future__ import annotations

import ast
import inspect
import os

import pytest

from beatsync_fork import raw_cutter_profiles
from beatsync_fork import render_profiles as profiles

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------------------------
# Auto is the untouched path
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key", [None, "", "   ", "auto", "AUTO", "nonsense", 30.0, 0, [], {}])
def test_anything_unrecognised_resolves_to_auto(key):
    """Falling back to the untouched legacy path is the safe direction: the worst outcome is a
    render that keeps the first source's geometry, never one reshaped by a half-understood
    setting."""
    assert profiles.resolve(key) is None


def test_auto_returns_the_callers_fallback_unchanged():
    """The structural guarantee behind "Auto preserves historical behaviour": these helpers do not
    compute anything under Auto, they hand back exactly what they were given.
    """
    sentinel = object()
    assert profiles.target_size(profiles.AUTO, sentinel) is sentinel
    assert profiles.target_fps(profiles.AUTO, sentinel) is sentinel
    assert profiles.target_size(None, (1234, 567)) == (1234, 567)
    assert profiles.target_fps(None, 23.976) == 23.976


def test_auto_produces_no_geometry_filter_at_all():
    """`""` means "the caller emits what it always emitted". It is deliberately NOT a no-op filter
    to splice in, because splicing anything would make Auto a new code path rather than the
    untouched one."""
    assert profiles.geometry_filter(profiles.AUTO) == ""
    assert profiles.geometry_filter(None) == ""


def test_auto_is_the_first_choice_and_therefore_the_default():
    choices = profiles.profile_choices()
    assert choices[0][1] == profiles.AUTO
    assert len(choices) == len(profiles.PROFILES) + 1


# ---------------------------------------------------------------------------------------------
# The profiles themselves
# ---------------------------------------------------------------------------------------------

def test_the_four_documented_profiles_are_offered():
    assert {p.key for p in profiles.PROFILES} == {"1080p30", "1080p25", "720p30", "720p25"}
    for profile in profiles.PROFILES:
        assert (profile.width, profile.height) in ((1920, 1080), (1280, 720))
        assert profile.fps in (25, 30)


def test_every_profile_resolves_to_its_own_numbers():
    for profile in profiles.PROFILES:
        assert profiles.resolve(profile.key) is profile
        assert profiles.target_size(profile.key, ("ignored",)) == (profile.width, profile.height)
        assert profiles.target_fps(profile.key, "ignored") == profile.fps
        assert profiles.label_for(profile.key) == profile.label


def test_the_profile_keys_are_stable_strings():
    """They ride on a Gradio radio and are recorded in `render_info`, so renaming one is a
    user-visible change rather than a refactor."""
    for profile in profiles.PROFILES:
        assert isinstance(profile.key, str) and profile.key.strip() == profile.key


# ---------------------------------------------------------------------------------------------
# Padding, never stretching -- and the geometry is REUSED, not reimplemented
# ---------------------------------------------------------------------------------------------

def test_the_geometry_comes_from_the_raw_cutters_measured_helper():
    """A second scale-and-pad chain is a second thing to get wrong, and the cutter's was measured
    against a real anamorphic fixture. Asserted by identity of output, not by reading the source.
    """
    for profile in profiles.PROFILES:
        assert profiles.geometry_filter(profile.key) == \
            raw_cutter_profiles.geometry_filter(profile.width, profile.height)


def test_the_chain_pads_squares_the_pixels_and_never_stretches():
    chain = profiles.geometry_filter("1080p30")
    # the sample-aspect squaring MUST come first, or a 720x576 SAR 16:15 frame is fitted as 5:4
    assert chain.startswith("scale=trunc(iw*sar/2)*2:ih,setsar=1,"), chain
    # fit by DECREASE then pad -- the two halves of "letterbox/pillarbox rather than distort"
    assert "force_original_aspect_ratio=decrease" in chain
    assert "pad=1920:1080:(ow-iw)/2:(oh-ih)/2" in chain
    # and the output declares square pixels, so the padded frame's DAR is the profile's own
    assert chain.endswith("setsar=1")
    # the one thing it must never be: a bare stretch
    assert "scale=1920:1080," not in chain + ","


def test_the_extraction_replaces_the_bare_scale_rather_than_appending_to_it():
    """Both would fit the picture twice. Asserted on the real `ffmpeg_processing` source, since
    that module cannot be imported on a bare interpreter."""
    path = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "extract_clip_segment_ffmpeg_detailed")
    rendered = ast.unparse(fn)
    # an if/elif, never two unconditional appends
    assert "if geometry:" in rendered
    assert "elif target_size:" in rendered, \
        "the bare scale must be the ELSE branch, or both chains would run"
    assert rendered.count("f'scale={width}:{height}'") == 1


def test_the_extractor_defaults_geometry_to_auto():
    """Every pre-existing caller still describes exactly the command it always did."""
    path = os.path.join(_REPO_ROOT, "src", "ffmpeg_processing.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "extract_clip_segment_ffmpeg_detailed")
    names = [a.arg for a in fn.args.args]
    assert names[-1] == "lifecycle", "the cancellation token must stay last"
    assert "geometry" in names
    index = names.index("geometry")
    # defaults align to the END of the parameter list
    offset = len(names) - len(fn.args.defaults)
    assert ast.literal_eval(fn.args.defaults[index - offset]) == ""


# ---------------------------------------------------------------------------------------------
# The module stays a pure table
# ---------------------------------------------------------------------------------------------

def test_it_runs_no_ffmpeg_and_touches_no_disk():
    tree = ast.parse(inspect.getsource(profiles))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("subprocess.run", "subprocess.Popen", "open", "os.makedirs",
                      "os.path.exists", "time.time", "random.random"):
        assert forbidden not in calls, f"{forbidden} is reachable from a profile table"


def test_describe_reports_what_was_produced_rather_than_what_was_asked_for():
    """Under Auto the resolution and frame rate are discovered from the source at render time, so
    a module that guessed them would be describing its own intentions instead of the result."""
    line = profiles.describe(profiles.AUTO, resolution="1440x1080", fps=23.976,
                             encoder="h264_nvenc", output_dir=r"J:\Finished")
    assert "1440x1080" in line
    assert "23.976 fps" in line
    assert "h264_nvenc" in line
    assert r"J:\Finished" in line


def test_describe_never_raises_on_partial_information():
    for fps in (None, "", "abc", 0, object()):
        assert isinstance(profiles.describe("1080p30", resolution="", fps=fps), str)
