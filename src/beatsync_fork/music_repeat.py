#!/usr/bin/env python3
"""[FORK] Digital-Union (FUN R10 Repeat Music): the pure half of "play the song N times".

The owner's request is simple to state and easy to get wrong: a three-minute song at 5x should
produce a **fifteen-minute music video with newly planned shots all the way through** — not five
copies of one three-minute render glued together.

This module owns every decision that needs no FFmpeg, no filesystem and no clock:

* :func:`normalize_repeat` — what a widget/CLI value means, 1..30, with 1 as "no repeat";
* :func:`cycle_offsets` / :func:`total_seconds` — the deterministic repeated geometry;
* :func:`extend_beat_info` — **the whole timeline extension**: the music is analysed ONCE and each
  original cycle's time-dependent data is re-stamped at ``k * cycle_seconds``;
* :func:`estimate_markdown` — the "03:30 x 5 = 17:30" read-out the Render tab shows before a render.

:mod:`music_repeater` is the runtime sibling that actually produces the repeated audio master.
The dependency is one-way: that module imports this one, never the reverse.

===============================================================================
Why the extension happens here, after the analysis, and not inside the pipeline
===============================================================================

``analyze_beats_auto`` runs on the **original** music, unchanged, and Stage 5 has already finished by
the time anything here is called. Three consequences, and all three are the point:

* **Stage 1-5 are analysed once, never N times.** A 30x render costs exactly one beat grid, one
  feature pass, one section pass, one cut selection and one Stage-5 library.
* **The repeat count provably cannot reach Stage-5 cache identity, a Qwen request or the prompt.**
  It does not exist as a value anywhere in the pipeline until after Stage 5 has returned, so there
  is no route for it to key a record through. ``CACHE_CONTRACT_VERSION`` and ``ANALYSIS_VERSION``
  are untouched by this feature, and no cache payload gained a field.
* **1x is structurally the legacy render**, not a neutral-valued version of a new one. Every caller
  branches on ``repeat > 1`` before anything in this module is reached, so at 1x not one function
  here runs and not one array is rebuilt.

===============================================================================
What "extend" means, exactly
===============================================================================

``cycle_seconds`` is the **measured duration of one cycle of the audio master that will actually be
rendered**, never the analysis window's own ``len(y) / sr``. Those two differ by well under a
millisecond on a real track, but the frame-locked cut timeline is derived from the rendered file's
duration, so the offsets have to come from the same artifact or 30 cycles would accumulate the
difference.

::

    times          beat k of cycle c  ->  times[k] + c * cycle_seconds
    selected_times same rule -- the MUSICAL cut positions repeat exactly
    sections       start/end shifted; each cycle re-indexed; cycle c spans [c*D, (c+1)*D)
    beat series    tiled c times, preserving index alignment with `times`
    audio_duration repeat * cycle_seconds

**Musical cut positions repeat; the shots do not.** Stage 6 then plans one continuous timeline over
the whole extended duration, carrying its own running state (the recent-candidate and recent-source
windows, the usage counter and the seeded selection stream) across cycle boundaries — which is what
makes cycle 2 a different edit of the same music rather than a copy of cycle 1.

Only arrays whose length equals the **original** beat count are tiled. That rule is deliberate
rather than defensive: ``energy_profile`` legitimately carries three *frame-rate* curves
(``rms``, ``spectral_centroid``, ``zcr``) that are already not beat-aligned, and the planner's own
``_interp_feature`` alignment check already ignores them. Tiling those would invent an alignment
nobody has, and dropping them would change what a 1x render carries.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from beatsync_fork.audio_mix import format_clock

#: The owner's range, verbatim: 1x to 30x, 1x being today's render.
MIN_REPEAT = 1
MAX_REPEAT = 30
DEFAULT_REPEAT = 1

#: Bytes per second of the temporary repeated master. It is written as 48 kHz / stereo /
#: ``pcm_s24le`` so that :mod:`audio_mixdown` and final assembly — both of which already work in
#: exactly that format — have no resampling or requantisation to do. 48000 * 2 * 3.
TEMP_AUDIO_BYTES_PER_SECOND = 288_000

#: Above this the read-out warns about the output length and the temporary disk space, because a
#: 30x render of an ordinary song is an hour and a half of video and over a gigabyte of scratch WAV.
LONG_OUTPUT_WARNING_SECONDS = 15 * 60

#: ``energy_profile`` keys that are beat-synchronous and therefore tiled.
ENERGY_BEAT_SERIES = ("beat_energy", "energy_levels", "wave", "arc")

#: ``energy_profile`` keys that are **frame-rate curves** and are deliberately left exactly as a 1x
#: render carries them. See the module docstring.
ENERGY_FRAME_SERIES = ("rms", "spectral_centroid", "zcr")

#: ``rhythm_data`` keys, every one of them beat-synchronous.
RHYTHM_BEAT_SERIES = (
    "kick_strength",
    "clap_strength",
    "hihat_strength",
    "bass_strength",
    "combined_strength",
    "impact_strength",
    "novelty_strength",
    "is_strong_kick",
    "is_strong_clap",
    "is_strong_hihat",
    "is_strong_bass",
    "is_bar_anchor",
    "is_phrase_anchor",
)

#: The bus key carrying what this render actually did. Diagnostics and reporting only: nothing in
#: Stage 4, Stage 5, Stage 6 or any cache reads it.
BUS_KEY = "music_repeat"


# ---------------------------------------------------------------------------
# the control
# ---------------------------------------------------------------------------


def normalize_repeat(value: Any) -> int:
    """Coerce any UI/CLI value to a usable 1..30 repeat count; anything malformed is 1x.

    The same explicit type boundary as :func:`creative.normalize_control`, and for the same reasons:
    a Gradio slider yields floats, an emptied box yields ``None``, and a hand-typed or API-supplied
    value can be anything at all. None of it may raise in the middle of a render.

    Out-of-range values **clamp**, because the range has meaningful ends: ``100`` is an unambiguous
    request for "as long as you can" and becomes ``30``; ``0`` and ``-4`` become ``1``. A
    *fractional* value is different in kind — ``4.5`` is not a request for 4 repetitions, and
    flooring it would render a length the user never chose — so it falls back to
    :data:`DEFAULT_REPEAT`.

    ``bool`` is rejected first because it subclasses ``int``: ``True`` would otherwise read as 1x,
    which happens to be the right answer, and ``False`` as 0x, which is not a render at all.
    """
    if isinstance(value, bool):
        return DEFAULT_REPEAT
    if isinstance(value, int):
        return _clamp_repeat(value)
    if isinstance(value, float):
        # `is_integer()` is False for NaN and both infinities, so they need no separate guard.
        if value.is_integer():
            return _clamp_repeat(int(value))
        return DEFAULT_REPEAT
    if isinstance(value, str):
        text = value.strip()
        negative = text.startswith("-")
        digits = text[1:] if negative else text
        if digits.isdecimal():
            return _clamp_repeat(-int(digits) if negative else int(digits))
    return DEFAULT_REPEAT


def _clamp_repeat(value: int) -> int:
    return max(MIN_REPEAT, min(MAX_REPEAT, value))


def is_repeated(count: Any) -> bool:
    """True only when the music is genuinely played more than once.

    Every caller gates on this (or on the equivalent ``> 1``) *before* reaching any other function
    here, which is what makes 1x the untouched legacy path rather than a neutral new one.
    """
    return normalize_repeat(count) > MIN_REPEAT


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------


def _usable_seconds(value: Any) -> float | None:
    """A finite, strictly positive duration, or ``None`` for "not a measurement"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0.0:
        return None
    return number


def cycle_offsets(count: Any, cycle_seconds: Any) -> tuple:
    """``(0.0, D, 2D, ...)`` — where each repetition of the music begins.

    Multiplication rather than repeated addition, so the k-th offset carries one rounding step
    instead of k of them: a 30x render's last cycle must start at exactly ``29 * D``.
    """
    repeat = normalize_repeat(count)
    duration = _usable_seconds(cycle_seconds)
    if duration is None:
        return (0.0,)
    return tuple(index * duration for index in range(repeat))


def total_seconds(count: Any, cycle_seconds: Any) -> float:
    """The repeated master's exact duration, or ``0.0`` when the cycle is not a measurement."""
    duration = _usable_seconds(cycle_seconds)
    if duration is None:
        return 0.0
    return normalize_repeat(count) * duration


def temp_audio_bytes(seconds: Any) -> int:
    """How much scratch WAV a repeated master of this length occupies. Never negative."""
    duration = _usable_seconds(seconds)
    if duration is None:
        return 0
    return int(duration * TEMP_AUDIO_BYTES_PER_SECOND)


def format_bytes(value: Any) -> str:
    """Decimal MB/GB for one read-out line. Total; an unusable value answers ``--``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
        return "--"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f} GB"
    return f"{number / 1_000_000:.0f} MB"


def describe(count: Any, cycle_seconds: Any) -> str:
    """One short line for a console log or a success panel: ``5x - 03:30 -> 17:30``."""
    repeat = normalize_repeat(count)
    if repeat <= MIN_REPEAT:
        return "1x (no repeat)"
    return (f"{repeat}x - {format_clock(cycle_seconds)} -> "
            f"{format_clock(total_seconds(repeat, cycle_seconds))}")


def estimate_markdown(duration_seconds: Any, count: Any) -> str:
    """The Render tab's pre-render read-out. Total by contract: it runs on live widget values.

    Three states, deliberately distinguished rather than collapsed into one hedged sentence:

    * **no readable music length** — say so and say nothing about the output, because a number
      invented from an unreadable file is worse than no number;
    * **1x** — state the music's own length and that it plays once, so the control reads as
      "off" rather than as "unknown";
    * **2x..30x** — the arithmetic the owner asked for, verbatim, plus the two warnings a long
      render actually needs: how long the video will be, and how much temporary disk it wants.
    """
    repeat = normalize_repeat(count)
    music = _usable_seconds(duration_seconds)
    if music is None:
        return (f"**Repeat Music: {repeat}x** - load a music track to see the estimated "
                f"output length.")

    clock = format_clock(music)
    if repeat <= MIN_REPEAT:
        return f"**{clock}** x 1 = **{clock}** - the music plays once (no repeat)."

    total = total_seconds(repeat, music)
    lines = [f"**{clock}** x {repeat} = **{format_clock(total)}** of finished video."]
    if total >= LONG_OUTPUT_WARNING_SECONDS:
        lines.append(
            f"⚠️ That is a long render. It needs about "
            f"**{format_bytes(temp_audio_bytes(total))}** of temporary disk space for the repeated "
            f"audio master alone (plus the usual per-clip scratch), and it plans and extracts a "
            f"cut for every beat of all {repeat} repetitions.")
    return "  \n".join(lines)


# ---------------------------------------------------------------------------
# the timeline extension
# ---------------------------------------------------------------------------


def extend_times(times: Any, count: Any, cycle_seconds: Any) -> tuple:
    """Every beat time of every cycle, ascending. ``()`` when there is nothing usable to extend."""
    base = _finite_times(times)
    if not base:
        return ()
    return tuple(value + offset
                 for offset in cycle_offsets(count, cycle_seconds)
                 for value in base)


def _finite_times(times: Any) -> tuple:
    """Finite, non-negative times in their original order. Accepts a numpy array without numpy."""
    if not isinstance(times, Iterable) or isinstance(times, (str, bytes)):
        return ()
    out = []
    for value in times:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
            continue
        out.append(number)
    return tuple(out)


def extend_series(values: Any, count: Any, expected_length: int) -> Any:
    """Tile one beat-synchronous array ``count`` times, **or return it untouched**.

    The length check is the contract, not a guard: a series that is not already aligned with the
    original beat grid is something this feature does not understand, and tiling it would
    manufacture an alignment the analysis never produced. Returning it unchanged preserves exactly
    what a 1x render carries, including whatever the planner's own alignment check already does
    with it.

    Values are copied by reference, never coerced: a bool stays a bool, a numpy float stays
    whatever it was, and ``energy_levels``' strings stay strings.
    """
    repeat = normalize_repeat(count)
    if repeat <= MIN_REPEAT:
        return values
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
        return values
    try:
        items = list(values)
    except TypeError:
        return values
    if len(items) != int(expected_length):
        return values
    return tuple(items * repeat)


def extend_sections(sections: Any, count: Any, cycle_seconds: Any) -> list:
    """Every section of every cycle, re-stamped and re-indexed.

    Two properties are load-bearing, and both exist because ``stage6_av_planner._section_at`` does a
    half-open ``start <= t < end`` scan and falls back to the **last** section when nothing matches:

    * **cycle c is closed to exactly** ``[c * D, (c + 1) * D)``. Stage 3's own last section ends at
      the analysis window's ``len(y) / sr``, which is a hair away from the rendered cycle length, so
      without this each cycle boundary would leave a sub-millisecond hole in which every lookup
      resolved to the final cycle's final section. The interior boundaries are never moved — only
      the first start and the last end of each cycle are closed onto the grid, and only when that
      keeps the section non-empty.
    * **indices are unique across the whole extended list**, because
      ``beat_info["rhythm_patterns"]`` is keyed by section index and Stage 4's ``selection_info``
      points at section objects.

    Malformed entries are skipped, exactly as the two existing projections
    (``audio_mix.project_sections`` / ``smart_mix.project_structure``) skip them.
    """
    repeat = normalize_repeat(count)
    duration = _usable_seconds(cycle_seconds)
    usable = _usable_sections(sections)
    if not usable or duration is None:
        return list(usable)

    out: list = []
    for cycle, offset in enumerate(cycle_offsets(repeat, duration)):
        cycle_end = (cycle + 1) * duration
        last = len(usable) - 1
        for position, section in enumerate(usable):
            shifted = dict(section)
            shifted["index"] = len(out)
            start = float(section["start"]) + offset
            end = float(section["end"]) + offset
            if position == 0:
                start = offset
            if position == last and cycle_end > start:
                end = cycle_end
            shifted["start"] = start
            shifted["end"] = end
            shifted["duration"] = end - start
            out.append(shifted)
    return out


def _usable_sections(sections: Any) -> tuple:
    if not isinstance(sections, Iterable) or isinstance(sections, (str, bytes)):
        return ()
    out = []
    for item in sections:
        if not isinstance(item, Mapping):
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start != start or end != end or end <= start:
            continue
        out.append(item)
    return tuple(out)


def extend_selection_info(selection_info: Any, count: Any, sections: Sequence) -> list:
    """Stage 4's per-section read-out, tiled and re-pointed at the extended sections.

    Reporting only — the success panel is its one consumer — but it must not be left describing one
    cycle while the panel beside it describes thirty, and it must not keep pointing at the
    pre-extension section objects, which no longer describe where anything is.

    An entry is matched by the **recorded index of the section it describes**, not by its own
    position in the list: Stage 4 skips a section that contains no beats, so the two sequences are
    not guaranteed to line up and a positional mapping would silently re-point an entry at a
    neighbouring section. An entry whose section carries no usable index falls back to its
    position, and one that cannot be matched at all is tiled with its original payload rather than
    dropped — a read-out is allowed to be less specific, never to go missing.
    """
    repeat = normalize_repeat(count)
    if not isinstance(selection_info, Iterable) or isinstance(selection_info, (str, bytes)):
        return []
    try:
        base = list(selection_info)
    except TypeError:
        return []
    if not base or repeat <= MIN_REPEAT:
        return base

    out: list = []
    per_cycle = len(sections) // repeat if repeat else 0
    for cycle in range(repeat):
        for position, entry in enumerate(base):
            if not isinstance(entry, Mapping):
                out.append(entry)
                continue
            shifted = dict(entry)
            index = cycle * per_cycle + _section_index_of(entry, position)
            if per_cycle and 0 <= index < len(sections):
                shifted["section"] = sections[index]
            out.append(shifted)
    return out


def _section_index_of(entry: Mapping, fallback: int) -> int:
    """The index of the section one ``selection_info`` entry describes. Never raises."""
    section = entry.get("section")
    if isinstance(section, Mapping):
        value = section.get("index")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return fallback


def extend_beat_info(selected_times: Any, beat_info: Any, count: Any,
                     cycle_seconds: Any) -> tuple:
    """``(selected_times, beat_info)`` for the whole repeated timeline.

    Returns **new** containers and mutates nothing the caller handed in: a shallow copy of
    ``beat_info`` with the time-dependent keys replaced, and fresh ``energy_profile`` /
    ``rhythm_data`` dicts. The caller rebinds its own names, so an upstream cache entry (L2 holds
    deep copies anyway) and the original analysis result both stay exactly as they were.

    What is replaced, and nothing else:

    ======================  ========================================================
    ``times``               every beat of every cycle
    ``selected_times``      the same musical cuts, once per cycle
    ``sections``            re-stamped, re-indexed, each cycle closed onto the grid
    ``rhythm_patterns``     rebuilt from the extended sections
    ``energy_profile``      the four beat-synchronous series tiled
    ``rhythm_data``         all thirteen series tiled
    ``selection_info``      tiled, re-pointed at the extended sections
    ``audio_duration``      ``repeat * cycle_seconds``
    ``music_repeat``        the new diagnostics record
    ======================  ========================================================

    Deliberately **not** touched: ``tempo`` (the song's tempo is the song's tempo),
    ``audio_visual_profile`` (it describes the *track*, it was already consumed by Stage 5, and
    re-deriving it here would be a second authority on a dict that is finished), ``video_analysis``
    (the visual library is a fact about the sources), ``creative`` and ``freestyle`` (render intent,
    identical for every cycle by construction), and ``mode`` / ``auto_style``.
    """
    repeat = normalize_repeat(count)
    if not isinstance(beat_info, Mapping):
        return selected_times, beat_info
    if repeat <= MIN_REPEAT:
        return selected_times, beat_info

    duration = _usable_seconds(cycle_seconds)
    if duration is None:
        return selected_times, beat_info

    original_beats = len(_finite_times(beat_info.get("times")))
    extended = dict(beat_info)

    extended["times"] = extend_times(beat_info.get("times"), repeat, duration)
    extended_selected = extend_times(selected_times, repeat, duration)
    extended["selected_times"] = extended_selected

    sections = extend_sections(beat_info.get("sections"), repeat, duration)
    extended["sections"] = sections
    extended["rhythm_patterns"] = {
        int(section.get("index", position)): section.get("dominant_pattern", "mixed")
        for position, section in enumerate(sections)
    }
    extended["selection_info"] = extend_selection_info(
        beat_info.get("selection_info"), repeat, sections)

    energy = beat_info.get("energy_profile")
    if isinstance(energy, Mapping):
        extended["energy_profile"] = {
            key: (extend_series(value, repeat, original_beats)
                  if key in ENERGY_BEAT_SERIES else value)
            for key, value in energy.items()
        }

    rhythm = beat_info.get("rhythm_data")
    if isinstance(rhythm, Mapping):
        extended["rhythm_data"] = {
            key: (extend_series(value, repeat, original_beats)
                  if key in RHYTHM_BEAT_SERIES else value)
            for key, value in rhythm.items()
        }

    extended["audio_duration"] = total_seconds(repeat, duration)
    extended[BUS_KEY] = {
        "repeat": repeat,
        "cycle_seconds": duration,
        "total_seconds": total_seconds(repeat, duration),
        "beats_per_cycle": original_beats,
        "cuts_per_cycle": len(_finite_times(selected_times)),
    }
    return extended_selected, extended


def bus_record(beat_info: Any) -> Mapping | None:
    """The diagnostics record this feature left on the bus, or ``None`` on a 1x render.

    One reader for one key, exactly as ``stage6_av_planner.creative_profile`` is the one reader of
    ``"creative"``, so the repeat facts cannot fork into two independently-parsed views.
    """
    if not isinstance(beat_info, Mapping):
        return None
    record = beat_info.get(BUS_KEY)
    return record if isinstance(record, Mapping) else None


def summary_line(beat_info: Any) -> str:
    """``REPEAT MUSIC: 5x - 03:30 -> 17:30`` for the success panel, or ``''`` at 1x.

    Reads the bus record and formats it; it never recomputes, re-measures or re-derives anything,
    so the panel can never become a second authority on what the render actually did.
    """
    record = bus_record(beat_info)
    if record is None:
        return ""
    repeat = normalize_repeat(record.get("repeat"))
    if repeat <= MIN_REPEAT:
        return ""
    return f"🔁 REPEAT MUSIC: {describe(repeat, record.get('cycle_seconds'))}"


__all__ = [
    "BUS_KEY",
    "DEFAULT_REPEAT",
    "ENERGY_BEAT_SERIES",
    "ENERGY_FRAME_SERIES",
    "LONG_OUTPUT_WARNING_SECONDS",
    "MAX_REPEAT",
    "MIN_REPEAT",
    "RHYTHM_BEAT_SERIES",
    "TEMP_AUDIO_BYTES_PER_SECOND",
    "bus_record",
    "cycle_offsets",
    "describe",
    "estimate_markdown",
    "extend_beat_info",
    "extend_sections",
    "extend_selection_info",
    "extend_series",
    "extend_times",
    "format_bytes",
    "is_repeated",
    "normalize_repeat",
    "summary_line",
    "temp_audio_bytes",
    "total_seconds",
]
