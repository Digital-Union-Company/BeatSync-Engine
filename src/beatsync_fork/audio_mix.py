#!/usr/bin/env python3
"""[FORK] Digital-Union: Audio Layers V1 — the pure voice-placement planner (D).

Audio Layers lets a render carry spoken voice over the music **without touching the edit the music
produced**::

    ORIGINAL MUSIC  ->  Stages 1-5  ->  selected_beats + beat_info     (the video edit)
    ORIGINAL MUSIC + voice + beat_info projection  ->  this planner    (where speech lands)
    plan  ->  audio_mixdown  ->  one mixed master WAV                  (final render audio ONLY)

That ordering is the load-bearing part. The mixed master is never analysed: tempo, the beat grid,
sections, energy, cut selection, Qwen and the visual targets all continue to see the original music,
so adding a voice clip can never change which shots were chosen or where the cuts fall.

This module owns the *decisions* and nothing else — no FFmpeg, no subprocess, no probing, no
filesystem access beyond pure path-string ordering. It is stdlib-only (CLAUDE.md's hard rule), so
every placement rule is testable on a bare interpreter. The runtime half lives in
``src/audio_mixdown.py`` and may import this module; this module must never import it.

===============================================================================
What V1 decides
===============================================================================

Voice clips play in **deterministic filename order**, never in browser multi-select order — the
HTML File API hands back whatever the OS dialog supplies, which is not the order the user clicked.

**R7.3.1 made that literally true.** Until then the sort delegated to
:func:`input_manager.order_key`, which orders the *whole* normalised path — correct for a folder
scan, where every file shares a parent, and wrong for the browser picker, where Gradio gives each
uploaded file its own randomly-named temporary directory. Measured in the R7.3 playtest, a selection
named ``01_a 02_b 03_long 04_c 05_long`` played ``04_c 05_long 01_a 03_long 02_b``. Ordering is now
:func:`voice_order_key` — filename first, full path only as a tie-break — and ``order_key`` is
unchanged for video sources and the Smart Mix SFX pools.

Placement is deterministic and seedless. There is no RNG, no Variation Seed and no Master Creative
Seed: the same music structure, voice durations and :class:`AudioMixConfig` always produce the same
plan.

**Variant Lab reaches audio since E2 V1 — and this planner still knows nothing about it.** That is
not a contradiction, because what E2 added happens entirely *upstream* of here. Variant Lab resolves
one Audio Layers value, ``music_under_voice_percent``, and writes the existing *Music under voice*
widget with it; the GUI then builds an :class:`AudioMixConfig` from that visible widget at
render-click time, exactly as it did before E2 existed. Nothing hands this module a generated
object: no master seed, no ``"audio"`` RNG stream, no ``AudioRecipe``, no ``AudioVariantConfig`` and
no Variant Lab range ever arrives here. The planner receives one already-normalised config, and a
config is a config however its number was chosen — so every placement rule below, and the duck
model, remain exactly as deterministic and as seedless as they were in V1.

:class:`AudioMixConfig` is therefore where a varied value *lands*, not a destination still waiting
on future work, and it is still emphatically not a field of ``CreativeRecipe``. E2's other two
levels are Smart Mix controls and never enter this planner either.

===============================================================================
Two rules worth stating in full
===============================================================================

**Avoid drops means the WHOLE spoken interval.** A clip that merely *starts* before a drop has not
avoided it: measured on real material, a 12 s clip starting 0.44 s before a drop puts 11.56 s —
96% of its speech — inside that drop. So the rule is interval disjointness against every ``drop``
and ``finale`` span, not a test on the start instant.

**Preference is bounded, never a leap.** Snapping to "the nicest section start anywhere ahead" was
measured on the real track to throw the first clip 27 s forward and the second 83 s forward, leaving
84 s holes and wasting the song. The planner therefore finds the earliest legal beat first and only
looks :data:`PLACEMENT_LOOKAHEAD_SECONDS` beyond it for a better musical anchor.

===============================================================================
R7.3: what happens when the voice does not fit
===============================================================================

V1 answered this one way — :class:`PlacementFailure`, the whole render refused — and the owner hit
it on real material: ``Voice 24 (17.9s) has no legal placement after 187.6s`` threw away an
otherwise finished music video because clip 24 of 30 ran past the end of the song. R7.3 makes the
answer a **policy**, with the V1 answer as the default:

``strict``
    Exactly V1. The first clip that cannot be placed returns :class:`PlacementFailure`, with the
    identical message, and nothing is rendered. This is the default, so a config built the old way
    plans the old way.

``skip``
    The clip is dropped, recorded as a :class:`VoiceSkip` naming the file and the reason, and the
    planner **continues** — with the cursor left exactly where it was, which is the load-bearing
    detail: a 17.9 s clip that does not fit after 187.6 s says nothing about the 3 s clip behind it,
    and leaving the cursor alone is what lets that shorter clip still land. Order is never
    rearranged and nothing is shortened.

``trim``
    ``skip``, plus **one** partial clip. When a clip cannot be placed whole, the planner looks for
    the earliest legal beat at or after the cursor with at least
    :data:`MIN_TRIMMED_VOICE_SECONDS` of legal room, keeps that much of the clip and marks the
    placement ``trimmed``. Everything after it is skipped — the owner asked for "trim the final
    usable clip, then skip remaining", and a trim means the music genuinely ran out, so continuing
    to hunt would be pretending otherwise.

**A trimmed clip gets no anchor preference, deliberately.** The bounded lookahead above exists to
buy a nicer musical start without stranding the track; for a trimmed clip the thing worth
maximising is how much speech survives, and the available room shrinks monotonically as the start
moves later inside a span. So the rule is simply "the earliest beat with enough room", which is both
the maximal and the simplest answer.

**Skipping is a planning decision; trimming is also an execution one.** A trimmed placement carries
``source_duration`` (what the file is) alongside ``duration`` (what is played), and
``src/audio_mixdown.py`` must put a real ``atrim`` on that voice input — updating the planned number
alone would place the duck envelope correctly and then let the untrimmed file run past it. Nothing
here ever touches the user's audio files.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# [FORK] R7.3.1: `input_manager` is no longer imported here, and nothing replaced it. The voice
# order is filename-first (`voice_order_key`), which `order_key`'s whole-path ordering cannot
# express -- see that function for the measured reason. `order_key` is untouched and still orders
# video sources and the Smart Mix SFX pools, so this is a second rule for a genuinely different
# input, not a replacement.
#
# This module deliberately imports **no `os`** either. `test_the_module_imports_nothing_that_could
# _make_it_nondeterministic` holds the import set to `{__future__, collections, dataclasses,
# typing, beatsync_fork}` on the stated grounds of "no RNG, no clock, no uuid, no process" -- and
# `os` supplies all four (`urandom`, `times`, `getpid`, `system`), so importing the whole module to
# reach one pure path helper would weaken a guard that exists to keep placement a pure function of
# its inputs. Separator normalisation is two `str` operations; see `voice_order_key`.

# ---------------------------------------------------------------------------
# Fixed V1 constants — deliberately NOT user controls
# ---------------------------------------------------------------------------

#: How long the music takes to drop to the floor *before* the first syllable, and to come back
#: *after* the last one. Fixed so V1 ships four controls rather than six; the envelope completing
#: before speech starts is what stops the first word being buried.
DUCK_ATTACK_SECONDS = 0.250
DUCK_RELEASE_SECONDS = 0.400

#: How far past the earliest legal beat the planner may look for a nicer musical anchor. Bounded on
#: measured evidence: unbounded preference leaps tens of seconds and strands the rest of the track.
PLACEMENT_LOOKAHEAD_SECONDS = 4.0

#: Section types the planner prefers to start a clip on, read literally from Stage 3's vocabulary
#: (``stage3_sections.classify_section``). No second classifier is invented here.
PREFERRED_SECTION_TYPES = frozenset({"intro", "verse", "breakdown", "bridge", "outro"})

#: Section types speech may not overlap when ``avoid_drops`` is on. Deliberately only these two:
#: ``hook`` and ``chorus`` are energetic but common, and excluding them would make placement fail on
#: chorus-heavy tracks for no real gain.
AVOIDED_SECTION_TYPES = frozenset({"drop", "finale"})

#: Accepted voice extensions — exactly the music input's set (``gui.py``'s audio picker and the CLI
#: both say MP3/WAV/FLAC). ``.m4a`` is excluded on purpose: advertising a format the main audio
#: input refuses would make the UI and the executor disagree about the contract.
SUPPORTED_VOICE_EXTENSIONS = (".mp3", ".wav", ".flac")

# ---------------------------------------------------------------------------
# Control ranges. Seconds are genuinely fractional, so these deliberately do NOT reuse
# `creative.normalize_control`, which exists for 0..100 integer creative controls.
# ---------------------------------------------------------------------------

DEFAULT_START_DELAY_SECONDS = 2.0
START_DELAY_MIN_SECONDS = 0.0
START_DELAY_MAX_SECONDS = 60.0

DEFAULT_MIN_GAP_SECONDS = 1.0
MIN_GAP_MIN_SECONDS = 0.0
MIN_GAP_MAX_SECONDS = 30.0

DEFAULT_MUSIC_UNDER_VOICE_PERCENT = 35
MUSIC_UNDER_VOICE_MIN = 0
MUSIC_UNDER_VOICE_MAX = 100

# [FORK] R7.1 -- Comfort Mix. Two additions, both OFF/legacy by default.
#
# `music_return_seconds` IS the duck release. It defaults to `DUCK_RELEASE_SECONDS`, so a config
# built the old way produces the identical envelope and the identical filtergraph -- the legacy
# path is not re-derived from a "legacy profile", it is simply not touched.
#
# The owner's report is the reason it is adjustable at all: with a 0.4 s release the music does not
# so much return as reappear, and the louder the track the more that reads as a jump. 2.0 s is the
# suggested comfort value, not a new default.
DEFAULT_MUSIC_RETURN_SECONDS = DUCK_RELEASE_SECONDS
COMFORT_MUSIC_RETURN_SECONDS = 2.0
MUSIC_RETURN_MIN_SECONDS = 0.5
MUSIC_RETURN_MAX_SECONDS = 5.0

#: Anchor labels, reported so a shifted clip can explain itself.
ANCHOR_PREFERRED_SECTION = "preferred section start"
ANCHOR_SECTION = "section start"
ANCHOR_BEAT = "next legal beat"
#: [FORK] R7.3: the anchor of a clip that was shortened to fit. A distinct label rather than a flag
#: smuggled into an existing one, so the placement report can say *why* a row looks short.
ANCHOR_TRIMMED = "trimmed to fit"

# ---------------------------------------------------------------------------
# [FORK] R7.3 -- what to do when a voice clip does not fit inside the music.
#
# `strict` is the default and IS V1: the same refusal, the same message. The two other policies
# exist because refusing a whole finished render over clip 24 of 30 is not the trade the owner
# wants, and because the alternative must be loud about what it dropped rather than quietly
# producing a shorter voice-over that looks deliberate.
# ---------------------------------------------------------------------------

OVERFLOW_STRICT = "strict"
OVERFLOW_SKIP = "skip"
OVERFLOW_TRIM = "trim"

#: Declaration order is the UI's order and the report's order. Not a set: the UI renders it.
OVERFLOW_POLICIES = (OVERFLOW_STRICT, OVERFLOW_SKIP, OVERFLOW_TRIM)
DEFAULT_OVERFLOW_POLICY = OVERFLOW_STRICT

#: Short operator-readable names, used by the report. The GUI owns its own labels (`ui_content.py`)
#: because those carry emoji and markdown; these are the plain-text ones a report line needs.
OVERFLOW_POLICY_NAMES = {
    OVERFLOW_STRICT: "Strict / Error",
    OVERFLOW_SKIP: "Skip clips that do not fit",
    OVERFLOW_TRIM: "Trim final usable clip, then skip remaining",
}

#: The least speech worth keeping from a trimmed clip. Below this a partial clip is a click, not a
#: word, so the clip is skipped instead. Not a user control in R7.3 — one fewer thing to get wrong.
MIN_TRIMMED_VOICE_SECONDS = 0.5


# ---------------------------------------------------------------------------
# normalisation — total, never raising
# ---------------------------------------------------------------------------


def normalize_seconds(value: Any, default: float, minimum: float, maximum: float) -> float:
    """Coerce a user-entered duration in seconds into ``[minimum, maximum]``.

    A seconds control is not a creative 0..100 integer: ``2.5`` is a perfectly good start delay, so
    fractional values are *accepted* here where ``creative.normalize_control`` rejects them. What is
    shared is the explicit type boundary — ``bool`` is refused first because it subclasses ``int``,
    and a malformed value becomes the default rather than acquiring a guessed meaning. ``NaN`` and
    both infinities are refused because clamping them would silently invent a bound, and an infinite
    delay would otherwise become an enormous FFmpeg ``adelay``.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN fails both comparisons; infinities clamp to a real bound, which is not a reading of
        # what the user typed, so both are refused outright.
        if number != number or number in (float("inf"), float("-inf")):
            return default
        return max(minimum, min(maximum, number))
    return default


def normalize_start_delay(value: Any) -> float:
    return normalize_seconds(value, DEFAULT_START_DELAY_SECONDS,
                             START_DELAY_MIN_SECONDS, START_DELAY_MAX_SECONDS)


def normalize_min_gap(value: Any) -> float:
    return normalize_seconds(value, DEFAULT_MIN_GAP_SECONDS,
                             MIN_GAP_MIN_SECONDS, MIN_GAP_MAX_SECONDS)


def normalize_music_under_voice(value: Any) -> int:
    """Percent of the normal music level during speech. A whole 0..100 integer, clamped."""
    if isinstance(value, bool):
        return DEFAULT_MUSIC_UNDER_VOICE_PERCENT
    if isinstance(value, int):
        return max(MUSIC_UNDER_VOICE_MIN, min(MUSIC_UNDER_VOICE_MAX, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return DEFAULT_MUSIC_UNDER_VOICE_PERCENT
        return max(MUSIC_UNDER_VOICE_MIN, min(MUSIC_UNDER_VOICE_MAX, int(round(value))))
    return DEFAULT_MUSIC_UNDER_VOICE_PERCENT


def normalize_music_return(value: Any, auto_balance: Any = False) -> float:
    """The duck release, in seconds.

    **With Auto Balance off this answers exactly `DUCK_RELEASE_SECONDS` unless the caller asked
    for something else**, which is what keeps the legacy mix bit-for-bit what it was. With it on,
    an unusable value falls back to the comfort suggestion rather than to the legacy 0.4 s, because
    a comfort mix with a legacy release is the one combination that would quietly do nothing.
    """
    fallback = COMFORT_MUSIC_RETURN_SECONDS if normalize_avoid_drops(auto_balance) \
        else DEFAULT_MUSIC_RETURN_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return fallback
    if seconds != seconds:                                  # NaN
        return fallback
    if not normalize_avoid_drops(auto_balance):
        # Legacy mode clamps nothing and invents nothing: whatever the caller passed, including the
        # historical default, is used as-is once it is a finite positive number.
        return seconds if seconds > 0 else DEFAULT_MUSIC_RETURN_SECONDS
    return max(MUSIC_RETURN_MIN_SECONDS, min(MUSIC_RETURN_MAX_SECONDS, seconds))


def normalize_avoid_drops(value: Any) -> bool:
    """Only a real ``bool`` is honoured; anything else is the protective default (``True``)."""
    return value if isinstance(value, bool) else True


def normalize_overflow_policy(value: Any) -> str:
    """[FORK] R7.3: the voice-overflow policy, as one of :data:`OVERFLOW_POLICIES`.

    **Anything unrecognised becomes `strict`**, which is the conservative direction: an unreadable
    widget value must never silently grant permission to drop or shorten the user's speech. Matching
    is exact on the internal name, not case-folded prose — the GUI stores the internal name as the
    radio's *value* precisely so no label text ever has to be parsed back into a policy.
    """
    if isinstance(value, str) and value in OVERFLOW_POLICIES:
        return value
    return DEFAULT_OVERFLOW_POLICY


def format_clock(seconds: Any) -> str:
    """``MM:SS`` (or ``H:MM:SS`` past an hour) for a report line. Total, never raising.

    [FORK] R7.3. An unusable value answers ``--:--`` rather than ``00:00``: a report that cannot
    read the music length must say so, not assert a zero-length song.
    """
    try:
        total = float(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if total != total or total in (float("inf"), float("-inf")) or total < 0.0:
        return "--:--"
    whole = int(total)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def voice_order_key(path: str) -> tuple:
    """[FORK] R7.3.1: the voice play order — **filename first**, full path only as a tie-break.

    The UI promises "clips play in filename order — name them ``01_intro.wav``, ``02_quote.wav``",
    and until R7.3.1 this delegated to :func:`input_manager.order_key`, which sorts the **whole**
    normalised path. That is right for a folder scan, where every file shares a parent and the path
    order *is* the filename order. It is wrong for the browser picker the help text is attached to:
    Gradio stores each uploaded file under its own randomly-named temporary directory, so the parent
    component is random noise that dominates the sort. Measured in the R7.3 playtest, a selection
    named ``01_a 02_b 03_long 04_c 05_long`` actually played::

        04_c  ->  05_long  ->  01_a  ->  03_long  ->  02_b

    which is not a cosmetic problem: it decides which clip overflows, and therefore which clip the
    ``skip``/``trim`` policies drop or shorten.

    So the key is ``(filename case-folded, path case-folded, path)``:

    * **filename, case-folded** is the promise, and the only thing an operator can control by
      naming their files;
    * **path case-folded** then **path raw** make the order *total* rather than merely stable — two
      clips with the same basename in different folders, and two paths differing only in case, each
      get a defined position instead of inheriting whatever order the widget happened to supply.

    ``order_key`` itself is deliberately **not** changed: it still orders video sources and the Smart
    Mix SFX pools, where whole-path ordering is the documented and wanted behaviour. This is a second
    rule because the two inputs genuinely differ, not because one of them was wrong.

    Separators are normalised to ``/`` before the basename is taken, so a Windows path, a
    forward-slash path and a mix of the two all split at the right place. That is a plain
    ``str.replace`` rather than ``os.path.normpath`` for two reasons, and the second is the stronger
    one:

    * ``normpath`` is **host-dependent** — on Windows it rewrites ``/`` to ``\\``, on POSIX it
      leaves a ``\\`` alone — so ``order_key`` has always had to follow it with the same explicit
      ``replace`` anyway. Doing only the replace is the portable half, and the half that matters.
    * reaching ``normpath`` means importing ``os``, which supplies randomness, a clock, the pid and
      ``system``. A test holds this module's import set down on exactly those grounds, and widening
      it to borrow one pure path helper would weaken a guard that keeps placement a pure function of
      its inputs.

    What is given up is ``..``/``.`` collapsing *inside the tie-break component only*, which can
    matter solely when two selected clips share a basename AND one of their paths is written
    non-canonically. The order stays total and reproducible either way.

    Pure string work: no ``stat``, no filesystem access, and the **original string is what gets
    sorted and returned** — normalisation is for comparison only.
    """
    normalised = path.replace("\\", "/")
    name = normalised.rsplit("/", 1)[-1]
    return (name.casefold(), normalised.casefold(), normalised)


def order_voice_paths(paths: Any) -> tuple:
    """Voice clips in deterministic **filename** order — the answer to "what order do these play
    in?".

    Browser multi-select order is not trustworthy, so it is not used; see :func:`voice_order_key`
    for why the sort is filename-first rather than whole-path.

    **Every usable entry survives.** Non-string and empty entries are dropped rather than raising,
    because this runs on a raw widget value — but that is the only thing ever removed, and
    ``prepare_voice_inputs`` re-checks the count afterwards so a shrunken selection can never be
    mistaken for a deliberate one. Duplicate filenames are kept, both of them, in a defined order.
    """
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Iterable):
        return ()
    usable = [p for p in paths if isinstance(p, str) and p]
    return tuple(sorted(usable, key=voice_order_key))


def has_supported_voice_extension(path: Any) -> bool:
    if not isinstance(path, str):
        return False
    lowered = path.casefold()
    return any(lowered.endswith(ext) for ext in SUPPORTED_VOICE_EXTENSIONS)


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioMixConfig:
    """The four Audio Layers controls, normalised on construction."""

    start_delay_seconds: float = DEFAULT_START_DELAY_SECONDS
    min_gap_seconds: float = DEFAULT_MIN_GAP_SECONDS
    avoid_drops: bool = True
    music_under_voice_percent: int = DEFAULT_MUSIC_UNDER_VOICE_PERCENT
    # [FORK] R7.1: APPENDED with legacy defaults, so every existing construction -- positional or
    # keyword -- builds exactly the config it built before and no existing index moves.
    auto_balance: bool = False
    music_return_seconds: float = DEFAULT_MUSIC_RETURN_SECONDS
    # [FORK] R7.3: APPENDED again, with the legacy policy as its default, for exactly the reason
    # R7.1's two fields were -- every existing construction, positional or keyword, keeps building
    # the config it built before, and no existing field index moves.
    voice_overflow: str = DEFAULT_OVERFLOW_POLICY

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_delay_seconds",
                           normalize_start_delay(self.start_delay_seconds))
        object.__setattr__(self, "min_gap_seconds", normalize_min_gap(self.min_gap_seconds))
        object.__setattr__(self, "avoid_drops", normalize_avoid_drops(self.avoid_drops))
        object.__setattr__(self, "music_under_voice_percent",
                           normalize_music_under_voice(self.music_under_voice_percent))
        object.__setattr__(self, "auto_balance", normalize_avoid_drops(self.auto_balance))
        object.__setattr__(self, "music_return_seconds",
                           normalize_music_return(self.music_return_seconds, self.auto_balance))
        object.__setattr__(self, "voice_overflow",
                           normalize_overflow_policy(self.voice_overflow))

    @property
    def music_floor(self) -> float:
        """Linear music gain under speech — ``35`` percent means gain ``0.35``, **not** -35 dB."""
        return self.music_under_voice_percent / 100.0

    @property
    def overflow_policy_name(self) -> str:
        """The resolved policy as operator-readable plain text, for the placement report."""
        return OVERFLOW_POLICY_NAMES.get(self.voice_overflow, self.voice_overflow)


@dataclass(frozen=True)
class VoiceInput:
    """One voice clip, already ordered and already probed by the runtime half."""

    index: int
    path: str
    duration: float


@dataclass(frozen=True)
class MusicSection:
    """The only three section fields placement needs. Stage 3's ``type`` is used literally."""

    start: float
    end: float
    section_type: str

    def overlaps(self, start: float, end: float) -> bool:
        """Half-open ``[start, end)`` overlap. Boundary-touching is **not** an overlap."""
        return start < self.end and end > self.start


@dataclass(frozen=True)
class VoicePlacement:
    """One placed clip. ``duration`` is **what plays**, which is what every consumer already means.

    [FORK] R7.3: two appended, defaulted fields rather than a changed meaning for ``duration``.
    That direction is deliberate — ``end``, the duck envelope, ``adelay`` and the balance
    measurement all derive from ``duration``, so making it the *played* length keeps every one of
    them correct for a trimmed clip without a single arithmetic change. ``source_duration`` carries
    what the file actually is, so the report can say "4.0s of 17.9s" and the executor knows an
    ``atrim`` is required.

    ``source_duration`` defaults to ``duration`` rather than to ``0.0``: every construction that
    predates R7.3 places whole clips, so "the file is as long as what plays" is the truthful answer
    for all of them, and no caller has to be updated to stay honest.
    """

    index: int
    path: str
    duration: float
    start: float
    end: float
    section_type: str
    anchor: str
    shift_seconds: float
    source_duration: float = 0.0
    trimmed: bool = False

    def __post_init__(self) -> None:
        source = self.source_duration
        if not isinstance(source, (int, float)) or isinstance(source, bool) \
                or source != source or source <= 0.0:
            object.__setattr__(self, "source_duration", self.duration)

    @property
    def trimmed_away_seconds(self) -> float:
        """How much of the file was not played. ``0.0`` for every untrimmed placement."""
        return max(0.0, self.source_duration - self.duration)


@dataclass(frozen=True)
class VoiceSkip:
    """[FORK] R7.3: one clip the plan deliberately left out, and why.

    Deliberately a *record*, not a message appended to some status string: the reason has to reach
    the placement report, the console log and the success panel, and all three read the same field.
    The field names mirror :class:`PlacementFailure` on purpose — it is the same fact, no longer
    fatal — so anything that already knew how to describe a failure can describe a skip.
    """

    index: int
    path: str
    duration: float
    after_seconds: float
    reason: str


@dataclass(frozen=True)
class DuckEvent:
    """One music-ducking envelope. ``start``/``end`` include the attack and release ramps."""

    voice_start: float
    voice_end: float
    start: float
    end: float
    floor: float
    attack: float
    release: float


@dataclass(frozen=True)
class PlacementFailure:
    """No legal placement exists. V1 never drops, truncates or overlaps a clip to avoid this."""

    index: int
    path: str
    duration: float
    after_seconds: float
    reason: str


@dataclass(frozen=True)
class AudioMixPlan:
    """The final audio plan: voice placements, their duck envelopes, and any Smart Mix SFX.

    [FORK] Digital-Union (Smart Mix V1 / E): ``sfx_placements`` is the **one** field E added, and it
    is last and defaulted so every pre-existing positional construction
    ``AudioMixPlan(duration, placements, duck_events, config)`` stays valid. It deliberately carries
    only *resolved placements*: Smart Mix's own config, amount, level and library root stay on
    :class:`smart_mix.SmartMixPlan`, because the executor needs the events, not the generator that
    produced them. The SFX gain reaches FFmpeg as a separate execution argument for the same reason.
    There is no parallel final-audio plan type.
    """

    music_duration: float
    placements: tuple
    duck_events: tuple
    config: AudioMixConfig
    sfx_placements: tuple = ()
    # [FORK] R7.1: what Auto Balance measured, as operator-readable prose. LAST and defaulted for
    # the same reason `sfx_placements` is, so every positional construction stays valid. Empty
    # string means no measurement was taken -- which is the truthful state whenever Auto Balance
    # is off, and must never be rendered as "normalised".
    balance_report: str = ""
    # [FORK] R7.3: the clips this plan deliberately left out, and how many were supplied. Appended
    # and defaulted, again for the same reason.
    #
    # `voices_supplied` defaults to 0 and is then back-filled to `used + skipped` by __post_init__,
    # so a plan built anywhere that does not know about overflow still reports a truthful supply
    # count instead of a zero. An explicitly supplied positive value is always preserved, which is
    # what makes `dataclasses.replace` (used twice in `audio_mixdown.build_mixed_master`) safe.
    skipped: tuple = ()
    voices_supplied: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.voices_supplied, int) or isinstance(self.voices_supplied, bool) \
                or self.voices_supplied <= 0:
            object.__setattr__(self, "voices_supplied",
                               len(self.placements) + len(self.skipped))

    @property
    def voice_count(self) -> int:
        return len(self.placements)

    @property
    def sfx_count(self) -> int:
        return len(self.sfx_placements)

    @property
    def trimmed_count(self) -> int:
        """[FORK] R7.3: placed clips that were shortened. At most 1 under the `trim` policy."""
        return sum(1 for p in self.placements if getattr(p, "trimmed", False))

    @property
    def whole_count(self) -> int:
        """[FORK] R7.3: placed clips that play in full.

        ``whole_count + trimmed_count + skipped_count == voices_supplied`` — the counts are
        **exclusive**, so the four numbers in the report add up and a trimmed clip is never also
        counted as used in full. The owner's own example line
        (``30 supplied, 23 used, 1 trimmed, 6 skipped``) reads exactly this way.
        """
        return self.voice_count - self.trimmed_count

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    @property
    def trimmed_placements(self) -> tuple:
        return tuple(p for p in self.placements if getattr(p, "trimmed", False))

    def summary_line(self) -> str:
        """One line for the success panel. Only ever shown when voice was actually used.

        **Unchanged by R7.3**, character for character. The overflow accounting is a *second* line
        (:meth:`overflow_summary_line`) rather than a rewrite of this one: this line describes the
        mix, the new one describes the selection, and an operator who never hits overflow should not
        find their familiar line reworded.
        """
        return (f"Audio Layers: {self.voice_count} voice clip"
                f"{'' if self.voice_count == 1 else 's'} · "
                f"music {self.config.music_under_voice_percent}% · "
                f"{len(self.duck_events)} duck event"
                f"{'' if len(self.duck_events) == 1 else 's'}")

    def overflow_summary_line(self) -> str:
        """[FORK] R7.3: the one line that must never be omitted from a completed plan.

        Every completed Audio Layers plan states how many clips were supplied, used in full,
        trimmed and skipped, plus the music length — whether or not anything overflowed. A report
        that silently said nothing when six clips were dropped is the exact failure this exists to
        prevent, and making the line unconditional is what stops "nothing to report" and "I did not
        look" from producing the same output.
        """
        return (f"Audio Layers: {self.voices_supplied} clip"
                f"{'' if self.voices_supplied == 1 else 's'} supplied, "
                f"{self.whole_count} used in full, "
                f"{self.trimmed_count} trimmed, "
                f"{self.skipped_count} skipped. "
                f"Music length: {format_clock(self.music_duration)}.")

    def report_lines(self) -> tuple:
        """The read-only placement report. Deliberately no FFmpeg filtergraph in normal UI."""
        header = [
            "Audio Layers",
            f"Music + {self.voice_count} voice clip{'' if self.voice_count == 1 else 's'}",
            f"Music under voice: {self.config.music_under_voice_percent}%",
            f"Duck events: {len(self.duck_events)}",
            f"Attack / release: {int(DUCK_ATTACK_SECONDS * 1000)} / "
            f"{int(self.config.music_return_seconds * 1000)} ms"
            + ("  (comfort)" if self.config.auto_balance else ""),
            # [FORK] R7.3: the policy and the accounting, both unconditional. The *detail* lines
            # below appear only when there is something to detail -- the Smart Mix diagnostics
            # convention -- so a render where everything fitted gains two header lines and no noise.
            f"When voice does not fit: {self.config.overflow_policy_name}",
            self.overflow_summary_line(),
            "",
        ]
        rows = []
        for placement in self.placements:
            name = placement.path.replace("\\", "/").rsplit("/", 1)[-1]
            row = (f"{placement.index + 1} · {name} · {placement.duration:.1f}s · "
                   f"{placement.start:.1f} → {placement.end:.1f} · "
                   f"{placement.section_type} · {placement.anchor}")
            if placement.shift_seconds >= 0.05:
                row += f" (+{placement.shift_seconds:.1f}s)"
            if getattr(placement, "trimmed", False):
                row += (f"  [TRIMMED: {placement.duration:.1f}s of "
                        f"{placement.source_duration:.1f}s played]")
            rows.append(row)

        tail = []
        if self.skipped:
            tail.append("")
            tail.append(f"Skipped {self.skipped_count} clip"
                        f"{'' if self.skipped_count == 1 else 's'}:")
            for skip in self.skipped:
                name = skip.path.replace("\\", "/").rsplit("/", 1)[-1]
                tail.append(f"  {skip.index + 1} · {name} · {skip.duration:.1f}s · {skip.reason}")
        return tuple(header + rows + tail)


# ---------------------------------------------------------------------------
# projection
# ---------------------------------------------------------------------------


def project_sections(sections: Any) -> tuple:
    """The small immutable section view the planner needs, from Stage 3's section mappings.

    Takes the ``beat_info["sections"]`` *list*, never the whole mutable ``beat_info`` dict — the
    planner has no business reading energy, impact, brightness or the dominant pattern, so it is
    not handed them. Malformed entries are skipped rather than raising.
    """
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
        if not (start == start and end == end) or end <= start:
            continue
        section_type = item.get("type")
        out.append(MusicSection(start, end,
                                section_type if isinstance(section_type, str) else "body"))
    return tuple(out)


def project_beat_times(beat_times: Any) -> tuple:
    """Finite, non-negative, ascending beat times. Accepts a numpy array without importing numpy."""
    if not isinstance(beat_times, Iterable) or isinstance(beat_times, (str, bytes)):
        return ()
    out = []
    for value in beat_times:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
            continue
        out.append(number)
    return tuple(sorted(out))


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


def _avoided_spans(sections: Sequence, avoid_drops: bool) -> tuple:
    if not avoid_drops:
        return ()
    return tuple(s for s in sections if s.section_type in AVOIDED_SECTION_TYPES)


def _is_legal(start: float, duration: float, music_duration: float, avoided: Sequence) -> bool:
    """Legal when the whole clip fits inside the music and touches no avoided section.

    Half-open intervals throughout: a clip ending exactly where a drop begins is legal, and so is
    one starting exactly where a drop ends.
    """
    end = start + duration
    if start < 0.0 or end > music_duration:
        return False
    return not any(section.overlaps(start, end) for section in avoided)


def _section_type_at(sections: Sequence, instant: float) -> str:
    for section in sections:
        if section.start <= instant < section.end:
            return section.section_type
    return "body"


def _legal_room_from(start: float, music_duration: float, avoided: Sequence) -> float:
    """[FORK] R7.3: how long a clip starting at ``start`` could legally play.

    The exact complement of :func:`_is_legal`, and that agreement is the point: a start that is
    itself inside an avoided span has **no** room at all (not "room up to the span's end"), and
    otherwise the room runs to whichever comes first — the end of the music, or the start of the
    next avoided span. Half-open throughout, so a clip ending exactly where a drop begins still
    fits, exactly as :func:`_is_legal` already allows.

    This is what makes a trim respect *Avoid drops* instead of merely respecting the music length.
    """
    if start < 0.0 or start >= music_duration:
        return 0.0
    limit = music_duration
    for section in avoided:
        if section.start <= start < section.end:
            return 0.0
        if start < section.start < limit:
            limit = section.start
    return max(0.0, limit - start)


def _earliest_whole_beat(voice, cursor: float, beats: Sequence,
                         music_duration: float, avoided: Sequence):
    """The earliest beat at or after ``cursor`` where the WHOLE clip is legal, or ``None``."""
    for beat in beats:
        if beat + 1e-9 < cursor:
            continue
        if _is_legal(beat, voice.duration, music_duration, avoided):
            return beat
    return None


def _trimmed_placement(voice, cursor: float, beats: Sequence, sections: Sequence,
                       music_duration: float, avoided: Sequence):
    """[FORK] R7.3: the one partial placement the ``trim`` policy allows, or ``None``.

    The earliest beat at or after the cursor with at least :data:`MIN_TRIMMED_VOICE_SECONDS` of
    legal room wins, and the retained duration is that room. **No bounded anchor preference is
    applied here** — see the module docstring: inside a span the available room shrinks as the start
    moves later, so "earliest" already maximises how much speech survives, and preferring a nicer
    section start could only throw speech away to buy a label.
    """
    for beat in beats:
        if beat + 1e-9 < cursor:
            continue
        room = _legal_room_from(beat, music_duration, avoided)
        kept = min(voice.duration, room)
        if kept + 1e-9 < MIN_TRIMMED_VOICE_SECONDS:
            continue
        return VoicePlacement(
            index=voice.index, path=voice.path, duration=kept,
            start=beat, end=beat + kept,
            section_type=_section_type_at(sections, beat),
            anchor=ANCHOR_TRIMMED, shift_seconds=beat - cursor,
            source_duration=voice.duration, trimmed=True,
        )
    return None


def plan_voice_placements(music_duration: float, beat_times: Sequence,
                          sections: Sequence, voices: Sequence,
                          config: AudioMixConfig):
    """Place every voice clip, in the order given. Returns an :class:`AudioMixPlan` or a
    :class:`PlacementFailure`.

    Deterministic and seedless. The clip order is the caller's and is never rearranged for musical
    convenience — the planner decides only *where* each ordered clip lands.

    [FORK] R7.3: and, when a clip will not fit, what ``config.voice_overflow`` says to do about it.
    Three things are worth stating because a future change could quietly break any of them:

    * **The whole-clip search is untouched.** The earliest-legal-beat scan and the bounded
      section-anchor preference below are byte-for-byte the V1 code, so a clip that fits is placed
      in the identical position under all three policies. That is what makes "if everything fits,
      all three modes sound the same" structural rather than merely tested.
    * **A skip does not advance the cursor.** The cursor is the end of the last *placed* clip plus
      the minimum gap; a clip that was never placed has no end. Advancing it would mean a 17.9 s
      clip that did not fit also pushed the 3 s clip behind it out of the song, which is the
      opposite of the point.
    * **``strict`` returns from inside the loop, exactly as V1 did**, with the identical message —
      so nothing downstream can tell the refusal apart from the one it already handled.
    """
    avoided = _avoided_spans(sections, config.avoid_drops)
    beats = tuple(beat_times)
    section_starts = tuple((s.start, s.section_type) for s in sections
                           if s.section_type not in AVOIDED_SECTION_TYPES or not config.avoid_drops)
    # [FORK] R7.3: read as a plain attribute, deliberately NOT through `getattr(config, "...")`.
    # `test_placement_feasibility_reads_only_batch_frozen_configuration` re-derives this function's
    # config reads by AST on every run, and a `getattr` with a string literal would be invisible to
    # it -- i.e. a new placement input would slip past the guard that proves `AudioMixPlanError` is
    # still SHARED_FATAL. The policy IS batch-frozen (it is a live widget frozen by the batch
    # handler's own arguments, exactly like `avoid_drops`), so the classification is unchanged; the
    # point is that the guard must be able to see it and say so.
    policy = normalize_overflow_policy(config.voice_overflow)
    voices = tuple(voices)

    placements = []
    skipped = []
    cursor = config.start_delay_seconds
    # `trim` spends its single partial clip once and then stops placing: the owner's rule is "trim
    # final usable clip, THEN skip remaining", and a trim is evidence the music ran out.
    overflow_spent = False

    for voice in voices:
        earliest = (None if overflow_spent
                    else _earliest_whole_beat(voice, cursor, beats, music_duration, avoided))

        if earliest is None:
            # ---- the clip does not fit (or the trim already closed the sequence) ----
            if policy == OVERFLOW_STRICT:
                return PlacementFailure(
                    index=voice.index, path=voice.path, duration=voice.duration,
                    after_seconds=cursor,
                    reason=(f"Voice {voice.index + 1} ({voice.duration:.1f}s) has no legal "
                            f"placement after {cursor:.1f}s"),
                )

            if overflow_spent:
                reason = (f"skipped: the music was already full after the trimmed clip "
                          f"at {cursor:.1f}s")
            elif policy == OVERFLOW_TRIM:
                trimmed = _trimmed_placement(voice, cursor, beats, sections,
                                             music_duration, avoided)
                if trimmed is not None:
                    placements.append(trimmed)
                    cursor = trimmed.end + config.min_gap_seconds
                    overflow_spent = True
                    continue
                reason = (f"skipped: no legal placement after {cursor:.1f}s, and under "
                          f"{MIN_TRIMMED_VOICE_SECONDS:.1f}s could be kept by trimming")
            else:
                reason = f"skipped: no legal placement after {cursor:.1f}s"

            skipped.append(VoiceSkip(
                index=voice.index, path=voice.path, duration=voice.duration,
                after_seconds=cursor, reason=reason,
            ))
            # The cursor is deliberately NOT advanced -- a shorter later clip may still fit here.
            continue

        chosen, anchor = earliest, ANCHOR_BEAT
        # Bounded preference: look only a little way past the earliest legal beat, so a nicer
        # anchor can win but a distant one can never strand the rest of the track.
        limit = earliest + PLACEMENT_LOOKAHEAD_SECONDS
        for label, wanted in ((ANCHOR_PREFERRED_SECTION, PREFERRED_SECTION_TYPES),
                              (ANCHOR_SECTION, None)):
            candidates = [
                start for start, section_type in section_starts
                if start + 1e-9 >= cursor and start <= limit + 1e-9
                and (wanted is None or section_type in wanted)
                and _is_legal(start, voice.duration, music_duration, avoided)
            ]
            if candidates:
                chosen, anchor = min(candidates), label
                break

        placements.append(VoicePlacement(
            index=voice.index, path=voice.path, duration=voice.duration,
            start=chosen, end=chosen + voice.duration,
            section_type=_section_type_at(sections, chosen),
            anchor=anchor, shift_seconds=chosen - cursor,
        ))
        cursor = chosen + voice.duration + config.min_gap_seconds

    return AudioMixPlan(
        music_duration=music_duration,
        placements=tuple(placements),
        duck_events=build_duck_events(tuple(placements), music_duration, config),
        config=config,
        skipped=tuple(skipped),
        voices_supplied=len(voices),
    )


# ---------------------------------------------------------------------------
# ducking
# ---------------------------------------------------------------------------


def build_duck_events(placements: Sequence, music_duration: float,
                      config: AudioMixConfig) -> tuple:
    """One envelope per placement. Windows are clamped to the music, ramps are not rescaled."""
    floor = config.music_floor
    # [FORK] R7.1: the RELEASE is now the configured music-return time. Its default is
    # `DUCK_RELEASE_SECONDS`, so a legacy config yields the identical envelope. The ATTACK is
    # deliberately unchanged: the complaint was about the music coming BACK, and lengthening the
    # attack would start ducking earlier and bury the lead-in instead of fixing anything.
    release = getattr(config, "music_return_seconds", DUCK_RELEASE_SECONDS)
    return tuple(
        DuckEvent(
            voice_start=p.start,
            voice_end=p.end,
            start=max(0.0, p.start - DUCK_ATTACK_SECONDS),
            end=min(music_duration, p.end + release),
            floor=floor,
            attack=DUCK_ATTACK_SECONDS,
            release=release,
        )
        for p in placements
    )


def _duck_amount_at(event: DuckEvent, instant: float) -> float:
    """How fully this event is ducking at ``instant``: 0 outside, 1 during the spoken body."""
    rising = (instant - (event.voice_start - event.attack)) / event.attack
    falling = ((event.voice_end + event.release) - instant) / event.release
    return max(0.0, min(1.0, min(max(0.0, min(1.0, rising)), max(0.0, min(1.0, falling)))))


def duck_gain_at(events: Sequence, instant: float, floor: float | None = None) -> float:
    """The music gain at ``instant`` — the **minimum** gain any active event asks for.

    Equivalently, the deepest duck wins. That is what makes overlap well defined without a hidden
    "min gap must exceed attack + release" constraint: if one event is still releasing while the
    next is already attacking, the music simply stays down rather than bouncing back up. It is also
    why the FFmpeg expression is built from ``max()`` of duck amounts and never from a chain of
    ``if()`` where whichever event matched first would win.
    """
    if not events:
        return 1.0
    resolved_floor = events[0].floor if floor is None else floor
    deepest = max(_duck_amount_at(event, instant) for event in events)
    return 1.0 - (1.0 - resolved_floor) * deepest


__all__ = [
    "ANCHOR_BEAT",
    "ANCHOR_PREFERRED_SECTION",
    "ANCHOR_SECTION",
    "ANCHOR_TRIMMED",
    "AVOIDED_SECTION_TYPES",
    # [FORK] R7.3: the voice-overflow policy.
    "DEFAULT_OVERFLOW_POLICY",
    "MIN_TRIMMED_VOICE_SECONDS",
    "OVERFLOW_POLICIES",
    "OVERFLOW_POLICY_NAMES",
    "OVERFLOW_SKIP",
    "OVERFLOW_STRICT",
    "OVERFLOW_TRIM",
    "VoiceSkip",
    "format_clock",
    "normalize_overflow_policy",
    "DEFAULT_MIN_GAP_SECONDS",
    "DEFAULT_MUSIC_UNDER_VOICE_PERCENT",
    "DEFAULT_START_DELAY_SECONDS",
    "DUCK_ATTACK_SECONDS",
    "DUCK_RELEASE_SECONDS",
    "DEFAULT_MUSIC_RETURN_SECONDS",
    "COMFORT_MUSIC_RETURN_SECONDS",
    "MUSIC_RETURN_MIN_SECONDS",
    "MUSIC_RETURN_MAX_SECONDS",
    "normalize_music_return",
    "MIN_GAP_MAX_SECONDS",
    "MIN_GAP_MIN_SECONDS",
    "MUSIC_UNDER_VOICE_MAX",
    "MUSIC_UNDER_VOICE_MIN",
    "PLACEMENT_LOOKAHEAD_SECONDS",
    "PREFERRED_SECTION_TYPES",
    "START_DELAY_MAX_SECONDS",
    "START_DELAY_MIN_SECONDS",
    "SUPPORTED_VOICE_EXTENSIONS",
    "AudioMixConfig",
    "AudioMixPlan",
    "DuckEvent",
    "MusicSection",
    "PlacementFailure",
    "VoiceInput",
    "VoicePlacement",
    "build_duck_events",
    "duck_gain_at",
    "has_supported_voice_extension",
    "normalize_avoid_drops",
    "normalize_min_gap",
    "normalize_music_under_voice",
    "normalize_seconds",
    "normalize_start_delay",
    "order_voice_paths",
    "voice_order_key",
    "plan_voice_placements",
    "project_beat_times",
    "project_sections",
]
