"""EBU R128 loudness measurement, and the bounded gains that follow from it.

This is the measuring half of **Auto Balance Voice & Music** (R7.1). It answers one question —
*how loud is this file, really* — using FFmpeg's own `loudnorm` analysis pass, and then turns two
such answers into two linear gains to apply **before** mixing.

**Why measured loudness rather than another percentage slider.** `Music under Voice` already
expresses a *relationship*: duck the music to 35 % while speech plays. What it cannot know is where
the two files start. A quiet phone-recorded voice against a loud mastered track is already 15 dB
apart before any ducking happens, so 35 % of a loud track is still louder than the speech, and the
operator's only recourse is to duck harder — which is exactly what makes the music's return feel
like a jump. Measuring both sides first removes the cause instead of fighting the symptom.

**What this deliberately is not.** No compression, no multiband anything, no mastering chain, and
no normalisation of the finished mix — the master's own limiter is untouched and the duck envelope
keeps its full depth. Two numbers in, two gains out.

Stdlib only (`json`, `re`, `subprocess`, `dataclasses`, `math`), so the fork package's
no-runtime-import rule still holds.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass

# ---------------------------------------------------------------------------------------------
# Targets and bounds
# ---------------------------------------------------------------------------------------------

#: Starting recommendations for real-world testing, not mastering law. Voice sits ~2 LU above the
#: music so speech reads as foreground even before the duck envelope does its work.
TARGET_MUSIC_LUFS = -18.0
TARGET_VOICE_LUFS = -16.0

#: Positive gain is bounded hard. A quiet clip may be quiet because it is *mostly room tone*, and
#: an unbounded correction would lift its noise floor into the mix -- the single most damaging
#: thing an automatic balancer can do. 12 dB is generous for a genuinely quiet recording and still
#: far short of amplifying silence.
MAX_BOOST_DB = 12.0

#: Attenuation is bounded too, for a different reason: a 30 dB cut on a mis-measured file would
#: silently remove the music altogether, and a wrong-but-audible mix is easier to diagnose than a
#: wrong-and-absent one.
MAX_CUT_DB = 12.0

#: Below this, a file is treated as having no usable programme loudness: left at unity and
#: reported, never boosted.
#:
#: **-60, not -45, and the difference was found by testing rather than reasoning.** `loudnorm`
#: reports around -70 LUFS for digital silence, so -45 looked like a comfortable margin -- until a
#: deliberately quiet spoken fixture measured **-48 LUFS** and was refused as "effectively silent".
#: That is exactly the recording this feature exists to rescue: a genuinely quiet phone-recorded
#: voice lives at -40 to -50 LUFS. A floor that excludes it protects nothing and helps nobody.
SILENCE_FLOOR_LUFS = -60.0

#: True-peak ceiling, in dBTP. A positive gain is reduced so the measured true peak cannot be
#: pushed above this -- the master limiter should be a safety net, not the thing shaping the mix.
TRUE_PEAK_CEILING_DBTP = -1.0

MEASURE_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Loudness:
    """One file's measured programme loudness. `ok` false means *we do not know*, not *it is 0*."""

    ok: bool
    integrated_lufs: float = 0.0
    true_peak_dbtp: float = 0.0
    threshold_lufs: float = 0.0
    detail: str = ""

    @property
    def is_silent(self) -> bool:
        return self.ok and self.integrated_lufs <= SILENCE_FLOOR_LUFS

    def describe(self) -> str:
        if not self.ok:
            return f"not measured ({self.detail or 'no reading'})"
        if self.is_silent:
            return f"{self.integrated_lufs:.1f} LUFS (effectively silent)"
        return f"{self.integrated_lufs:.1f} LUFS, true peak {self.true_peak_dbtp:.1f} dBTP"


@dataclass(frozen=True)
class GainPlan:
    """What to apply, and whether it is worth saying so.

    `music_gain` and `voice_gain` are **linear** multipliers ready for FFmpeg's `volume` filter.
    `applied` is false when nothing could be measured, in which case both gains are exactly 1.0 and
    the caller must report that normalisation did **not** happen rather than implying it did.
    """

    applied: bool
    music_gain: float = 1.0
    voice_gain: float = 1.0
    music_gain_db: float = 0.0
    voice_gain_db: float = 0.0
    notes: tuple = ()

    @property
    def is_noop(self) -> bool:
        return abs(self.music_gain - 1.0) < 1e-6 and abs(self.voice_gain - 1.0) < 1e-6


# ---------------------------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------------------------

def build_measure_command(ffmpeg: str, path: str,
                          sample_rate: int = 48000) -> list[str]:
    """The analysis argv. Decodes and writes nothing.

    `loudnorm`'s first pass prints a JSON block to **stderr** and `-f null -` throws the audio away,
    so this measures without producing a file. `-vn` keeps a video source's pictures out of the
    decode entirely.
    """
    return [str(ffmpeg), "-hide_banner", "-nostdin",
            "-i", str(path),
            "-vn",
            "-af", f"aresample={int(sample_rate)},loudnorm=print_format=json",
            "-f", "null", "-"]


_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def parse_loudness(text: str) -> Loudness:
    """Read the `loudnorm` JSON out of FFmpeg's stderr.

    The block is found by pattern rather than by taking the tail of the output, because FFmpeg
    happily prints warnings after it. Anything missing, unparseable or non-finite answers
    `ok=False` -- *unknown*, which the caller must not round to zero.
    """
    blob = (text or "")
    match = None
    for candidate in _JSON_BLOCK.finditer(blob):
        match = candidate                       # keep the last complete block
    if match is None:
        return Loudness(ok=False, detail="FFmpeg printed no loudness measurement")

    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return Loudness(ok=False, detail="the loudness measurement was not readable")

    integrated = _finite(payload.get("input_i"))
    peak = _finite(payload.get("input_tp"))
    threshold = _finite(payload.get("input_thresh"))
    if integrated is None:
        return Loudness(ok=False, detail="no integrated loudness was reported")

    return Loudness(ok=True,
                    integrated_lufs=integrated,
                    true_peak_dbtp=peak if peak is not None else 0.0,
                    threshold_lufs=threshold if threshold is not None else 0.0)


def _finite(value):
    """`loudnorm` legitimately reports `-inf` for silence; that is not a usable number here."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def measure(ffmpeg: str, path: str, *, runner=None,
            timeout: int = MEASURE_TIMEOUT_SECONDS) -> Loudness:
    """Measure one file. Never raises: a failure is a `Loudness` with `ok=False` and a reason."""
    run = runner or run_measure_command
    try:
        code, text = run(build_measure_command(ffmpeg, path), timeout)
    except Exception as failure:                   # noqa: BLE001 - measuring is never fatal
        return Loudness(ok=False, detail=str(failure))
    reading = parse_loudness(text)
    if reading.ok:
        return reading
    if code != 0:
        tail = [line for line in (text or "").strip().splitlines() if line.strip()]
        return Loudness(ok=False, detail=tail[-1] if tail else f"FFmpeg exited {code}")
    return reading


def run_measure_command(argv, timeout: int = MEASURE_TIMEOUT_SECONDS) -> tuple:
    """Execute one analysis pass; return `(returncode, stderr)`.

    **stderr**, because that is where `loudnorm` prints. Here rather than in `gui.py` for the same
    reason every other launcher in this package is: two suites pin that module's `subprocess.run`
    site count at exactly two.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return 1, f"loudness measurement timed out after {timeout}s"
    except (OSError, ValueError, subprocess.SubprocessError) as failure:
        return 1, str(failure)
    return int(completed.returncode), completed.stderr or ""


# ---------------------------------------------------------------------------------------------
# From measurements to gains
# ---------------------------------------------------------------------------------------------

def db_to_linear(db: float) -> float:
    return float(10.0 ** (float(db) / 20.0))


def _bounded(delta_db: float, peak_dbtp: float, measured: bool) -> tuple:
    """Clamp one correction, and say why if it was clamped."""
    notes = []
    gain_db = max(-MAX_CUT_DB, min(MAX_BOOST_DB, float(delta_db)))
    if gain_db != delta_db:
        notes.append(f"correction limited to {gain_db:+.1f} dB")
    if gain_db > 0 and measured:
        headroom = TRUE_PEAK_CEILING_DBTP - float(peak_dbtp)
        if headroom < gain_db:
            gain_db = max(0.0, headroom)
            notes.append(f"reduced to {gain_db:+.1f} dB to stay under "
                         f"{TRUE_PEAK_CEILING_DBTP:.0f} dBTP")
    return gain_db, tuple(notes)


def plan_gains(music: Loudness, voice: Loudness,
               music_target: float = TARGET_MUSIC_LUFS,
               voice_target: float = TARGET_VOICE_LUFS) -> GainPlan:
    """Two measurements in, two bounded linear gains out.

    Rules, each of which exists because its absence breaks a real case:

    * **Either side unmeasured means nothing is applied.** Correcting one side against an unknown
      other is worse than leaving both alone, and the caller reports honestly that no normalisation
      happened.
    * **A silent or near-silent voice is never boosted.** It is left at unity and reported. Lifting
      a clip that is mostly room tone by 12 dB raises its noise floor into the mix.
    * **Positive gain respects the measured true peak**, so an automatic correction cannot be the
      thing that makes a mix clip.
    """
    notes = []
    if not music.ok or not voice.ok:
        unmeasured = " and ".join(
            part for part in (("music" if not music.ok else ""),
                              ("voice" if not voice.ok else "")) if part)
        return GainPlan(applied=False,
                        notes=(f"Auto Balance did nothing: the {unmeasured} loudness could not be "
                               f"measured, so no gain was applied.",))

    music_db, music_notes = _bounded(music_target - music.integrated_lufs,
                                     music.true_peak_dbtp, True)
    notes += [f"music {note}" for note in music_notes]

    if voice.is_silent:
        voice_db = 0.0
        notes.append("the voice clips measured as effectively silent, so they were left "
                     "untouched rather than amplified")
    else:
        voice_db, voice_notes = _bounded(voice_target - voice.integrated_lufs,
                                         voice.true_peak_dbtp, True)
        notes += [f"voice {note}" for note in voice_notes]

    return GainPlan(applied=True,
                    music_gain=db_to_linear(music_db),
                    voice_gain=db_to_linear(voice_db),
                    music_gain_db=music_db,
                    voice_gain_db=voice_db,
                    notes=tuple(notes))


def describe_plan(music: Loudness, voice: Loudness, plan: GainPlan) -> str:
    """The operator-facing report. Never claims a correction that did not happen."""
    lines = [f"Music measured: {music.describe()}",
             f"Voice measured: {voice.describe()}"]
    if not plan.applied:
        lines.append("")
        lines += list(plan.notes)
        return "\n".join(lines)

    lines.append("")
    lines.append(f"Applied: music {plan.music_gain_db:+.1f} dB, voice {plan.voice_gain_db:+.1f} dB "
                 f"(targets {TARGET_MUSIC_LUFS:.0f} / {TARGET_VOICE_LUFS:.0f} LUFS)")
    if plan.is_noop:
        lines.append("Both were already at their targets, so nothing was changed.")
    lines += list(plan.notes)
    return "\n".join(lines)


__all__ = ["TARGET_MUSIC_LUFS", "TARGET_VOICE_LUFS", "MAX_BOOST_DB", "MAX_CUT_DB",
           "SILENCE_FLOOR_LUFS", "TRUE_PEAK_CEILING_DBTP", "MEASURE_TIMEOUT_SECONDS",
           "Loudness", "GainPlan",
           "build_measure_command", "parse_loudness", "measure", "run_measure_command",
           "db_to_linear", "plan_gains", "describe_plan"]
