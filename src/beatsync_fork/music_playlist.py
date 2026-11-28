#!/usr/bin/env python3
"""[FORK] Digital-Union (R16 Music Playlist Mode V1): the pure half of "several songs, one soundtrack".

The owner's request: instead of one `audio_input` file, allow an **ordered list** of music files and
treat it as **one continuous soundtrack** for beat analysis, shot planning and rendering. Single
Track stays the default and is untouched.

This module owns every decision that needs no FFmpeg, no filesystem and no clock:

* :func:`normalize_mode` — what the Music Source radio value means, with `single` as the default;
* :class:`PlaylistTrack` / :class:`Playlist` — the ordered soundtrack and its cheap identity;
* the seven editing operations the owner asked for — :func:`add_tracks`, :func:`remove_tracks`,
  :func:`move_up`, :func:`move_down`, :func:`shuffle`, :func:`sort_by_name`, :func:`clear` — each
  total, each returning a **new** playlist, and none of them silently deduplicating;
* :func:`freeze_entries` / :func:`entries_of` — the plain-primitive crossing used at the render
  boundary, so the render body never names a fork record;
* :func:`parse_wav_header` — the byte-level RIFF geometry read the sample-count verification rests
  on (bytes in, numbers out: no file is opened here);
* the read-outs: :func:`summary_markdown`, :func:`repeat_estimate_markdown`, :func:`summary_line`.

:mod:`music_playlist_builder` is the runtime sibling that decodes, concatenates, measures and
verifies the actual soundtrack master. The dependency is one-way: that module imports this one,
never the reverse. It is deliberately the same pure/runtime split, with the same module names and
the same loud-failure discipline, as :mod:`music_repeat` / :mod:`music_repeater`.

===============================================================================
The playlist master is simply "the music file", and that is the whole architecture
===============================================================================

::

    tracks (ordered, frozen)  --decode each to canonical PCM, concat--> PLAYLIST MASTER WAV
    PLAYLIST MASTER           --Stages 1-5 (ONCE)------------------>  selected_beats + beat_info
    PLAYLIST MASTER           --Repeat Playlist N x (R10, unchanged)-> repeated master
    create_music_video(whichever master, extended beats, extended beat_info)

Nothing downstream of the master knows a playlist happened. `analyze_beats_auto` still receives
`local_audio_path`; `music_repeater.build_repeated_music` still receives `music_path=local_audio_path`
and still measures its own cycle. That is what makes **Repeat Playlist repeat the whole ordered
playlist** rather than each song: the cycle R10 measures *is* the complete soundtrack, because by
the time R10 runs the playlist has already become one file. No change was needed in `music_repeat.py`
or `music_repeater.py` for that, and none was made.

It is also why `video_processor.py` needed no playlist awareness at all, exactly as it needed no
repeat awareness: the frame-locked timeline is derived from whichever audio file it is handed.

===============================================================================
What is deliberately NOT here
===============================================================================

* **No crossfade, no DJ mixing, no loudness mastering.** V1 joins the songs; it does not mix them.
  The original files are opened read-only and are never modified, moved or deleted.
* **No artificial gap.** Tracks abut exactly. Concatenating decoded PCM leaves nothing between two
  runs for a gap to live in — the same argument `music_repeater` records for repetitions.
* **No silent deduplication.** A track listed twice is an intentional repeat of that song inside the
  soundtrack; dropping it would render a shorter video than the operator asked for.
* **No hidden reshuffle.** :func:`shuffle` is an explicit, seeded, one-time reorder performed when
  the operator presses the button. The order shown is the order frozen at render admission, and the
  seed is recorded so the arrangement can be reproduced.
* **No filesystem access.** Validation that a track still exists, still has the recorded size and
  mtime, and still decodes is the runtime sibling's job; this module only says what a mismatch
  *means* (:func:`describe_identity_change`).
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from beatsync_fork.audio_mix import format_clock, voice_order_key
# The repeat count has exactly one definition in this repository, and Repeat Playlist is the same
# control reading the same 1..30 range -- so the normaliser is imported rather than restated. The
# dependency is one-way and acyclic: `music_repeat` knows nothing about a playlist, and must not,
# because R10's cycle is whatever audio file it was handed.
from beatsync_fork.music_repeat import MIN_REPEAT, normalize_repeat

# ---------------------------------------------------------------------------
# the Music Source control
# ---------------------------------------------------------------------------

#: The two Music Source values. `single` is the default and **is** the pre-R16 render.
MODE_SINGLE = "single"
MODE_PLAYLIST = "playlist"
MODE_VALUES = (MODE_SINGLE, MODE_PLAYLIST)
DEFAULT_MODE = MODE_SINGLE

#: Track types the picker offers. The *authority* on whether a file is usable is the decode, not
#: this tuple: the soundtrack master is produced by FFmpeg, so the list is a convenience filter and
#: a clear refusal message, never a claim about what FFmpeg can read. Deliberately wider than
#: `audio_input`'s three browser types, because a locally selected file never travels through an
#: upload control and FFmpeg decodes all of these.
SUPPORTED_TRACK_EXTENSIONS = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma")

#: An upper bound on the queue, so one mis-click in a multi-select dialog cannot ask for a decode of
#: ten thousand files. Generous: a 60-track playlist is a legitimate four-hour soundtrack.
#:
#: **The limit is VISIBLE, which means it is enforced where the operator can see it** — Add tracks
#: refuses past it and says so. It is deliberately *not* applied silently at the render-admission
#: freeze: a queue that somehow arrives longer than this is a tampered or corrupted state, and
#: quietly rendering the first 200 of it would be exactly the silent omission this feature refuses
#: everywhere else. See :func:`freeze_queue`.
MAX_TRACKS = 200

#: A WAV ``data`` chunk size is a 32-bit field, so one WAV cannot hold more than 4 GiB of samples.
#: At the canonical 288 kB/s that is about 4.1 hours. This ceiling sits just below it: a master
#: above it would be written with a wrapped header, every later measurement would read a wrong
#: frame count, and the operator would get an hours-long render that desynchronises.
#:
#: It lives here rather than in the runtime sibling because it is **format arithmetic**, and
#: because R16 R2 needs the same number for two different artifacts — the joined playlist master
#: and the repeated master R10 builds from it.
MAX_MASTER_BYTES = 3_900_000_000

#: Bytes per second of the canonical soundtrack master: 48 kHz, stereo, `pcm_s24le`. The same format
#: `music_repeater` and `audio_mixdown` already write, so no stage downstream has resampling or
#: requantisation work to do. 48000 * 2 * 3.
MASTER_BYTES_PER_SECOND = 288_000

#: The bus key carrying what this render's soundtrack actually was. Diagnostics and reporting only:
#: nothing in Stage 4, Stage 5, Stage 6 or any cache reads it.
BUS_KEY = "music_playlist"

#: Shown by the gate when playlist mode is armed with nothing in the queue. A refusal, never a
#: silent fall back to the Single Track picker — which would render music the operator did not ask
#: for, or nothing at all, without saying which.
EMPTY_PLAYLIST_MESSAGE = (
    "Music Source is set to Playlist, but the playlist is empty. Add at least one track, or "
    "switch Music Source back to Single Track."
)


def normalize_mode(value: Any) -> str:
    """Coerce any widget/API value to `single` or `playlist`; anything unrecognised is `single`.

    The same explicit boundary as :func:`music_repeat.normalize_repeat`, and for the same reason: a
    queued Gradio event, a stale browser tab or a direct API call can supply anything at all, and
    none of it may raise in the middle of a render. **Unrecognised falls back to the DEFAULT, not to
    playlist** — the legacy render is the safe answer, and an operator who never touched the control
    must get exactly what they got before R16.
    """
    if isinstance(value, bool):
        return DEFAULT_MODE
    if isinstance(value, str) and value.strip().lower() in MODE_VALUES:
        return value.strip().lower()
    return DEFAULT_MODE


def is_playlist(value: Any) -> bool:
    """True only when the operator is genuinely declaring a playlist render."""
    return normalize_mode(value) == MODE_PLAYLIST


def has_supported_extension(path: Any) -> bool:
    """Total: a non-path value is simply not supported rather than an error."""
    if not isinstance(path, str):
        return False
    return os.path.splitext(path)[1].lower() in SUPPORTED_TRACK_EXTENSIONS


# ---------------------------------------------------------------------------
# the ordered soundtrack
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaylistTrack:
    """One song in the soundtrack, with its cheap identity and its measured length.

    Identity is the same construction :class:`input_confirmation.SourceEntry` uses — normalised
    path + size + `mtime_ns`, **no content hashing** — and for the same reason: it is cheap enough
    to re-check on every render and strong enough to notice that a file was replaced between the
    moment the operator built the queue and the moment they pressed Create Music Video.

    `duration_seconds` is what the *source file* reports. It is an estimate for the read-out only.
    The authority on how long the soundtrack is, and on where each track begins inside it, is the
    **measured** geometry of the decoded PCM — see :class:`music_playlist_builder.PreparedTrack`.
    """

    path: str
    size: int = 0
    mtime_ns: int = 0
    duration_seconds: float = 0.0

    @property
    def name(self) -> str:
        return os.path.basename(self.path.replace("\\", "/")) or self.path

    def identity_line(self) -> str:
        """Canonical, case-insensitive-on-Windows serialisation of this track's identity."""
        return f"{os.path.normcase(self.path)}\x1f{self.size}\x1f{self.mtime_ns}"

    def as_entry(self) -> tuple:
        """The plain-primitive form that crosses into `gr.State` and into the render body."""
        return (self.path, int(self.size), int(self.mtime_ns), float(self.duration_seconds))


@dataclass(frozen=True, slots=True)
class Playlist:
    """The ordered soundtrack. Immutable; every operation returns a new one.

    **Order is identity.** Two playlists holding the same files in a different order are different
    soundtracks, not the same one — the beats, the sections and therefore the whole edit differ. So
    :attr:`identity` is built from the ordered identity lines and the position of each track.
    """

    tracks: tuple = ()

    @property
    def count(self) -> int:
        return len(self.tracks)

    @property
    def total_seconds(self) -> float:
        """The estimated soundtrack length. `0.0` when nothing usable has been probed."""
        total = 0.0
        for track in self.tracks:
            value = _usable_seconds(track.duration_seconds)
            if value is not None:
                total += value
        return total

    @property
    def identity(self) -> str:
        """An ordered, position-stamped identity string. Not a digest: callers compare equality."""
        return "\x1e".join(f"{index}\x1f{track.identity_line()}"
                           for index, track in enumerate(self.tracks))

    def as_entries(self) -> tuple:
        """The whole playlist as plain primitives — what `gr.State` holds and the render freezes."""
        return tuple(track.as_entry() for track in self.tracks)


EMPTY = Playlist()


def _usable_seconds(value: Any) -> float | None:
    """A finite, strictly positive duration, or `None` for "not a measurement"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number <= 0.0:
        return None
    return number


def _usable_int(value: Any) -> int:
    """A non-negative whole number, or `0`. Identity fields must never raise on a stale state.

    `OverflowError` is caught alongside the usual two because `int(float("inf"))` raises it and
    neither of the others — a `gr.State` really can hold an infinity, and a totality helper that
    raises is not one.
    """
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number >= 0 else 0


def track_from_entry(entry: Any) -> PlaylistTrack | None:
    """One :class:`PlaylistTrack` from a plain entry, or `None` when it is not one.

    Accepts the 4-tuple :meth:`PlaylistTrack.as_entry` produces, a mapping with the same keys, and
    a bare path string. Returning `None` rather than raising is the contract: the value arrives
    from `gr.State`, which is untrusted input, and a malformed row must cost the operator that row
    rather than the Render tab.
    """
    if isinstance(entry, PlaylistTrack):
        return entry if entry.path else None
    if isinstance(entry, str):
        return PlaylistTrack(path=entry) if entry.strip() else None
    if isinstance(entry, Mapping):
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            return None
        return PlaylistTrack(path=path,
                             size=_usable_int(entry.get("size")),
                             mtime_ns=_usable_int(entry.get("mtime_ns")),
                             duration_seconds=_usable_seconds(entry.get("duration_seconds")) or 0.0)
    if isinstance(entry, Sequence) and not isinstance(entry, (bytes, bytearray)):
        items = list(entry)
        if not items or not isinstance(items[0], str) or not items[0].strip():
            return None
        return PlaylistTrack(
            path=items[0],
            size=_usable_int(items[1]) if len(items) > 1 else 0,
            mtime_ns=_usable_int(items[2]) if len(items) > 2 else 0,
            duration_seconds=(_usable_seconds(items[3]) or 0.0) if len(items) > 3 else 0.0)
    return None


def playlist_from_entries(entries: Any) -> Playlist:
    """A :class:`Playlist` from whatever `gr.State` is holding. Total; never raises.

    Malformed rows are skipped exactly as the two existing projections
    (`audio_mix.project_sections` / `smart_mix.project_structure`) skip them, and the result is
    truncated to :data:`MAX_TRACKS`. **Order is preserved and duplicates are kept.**
    """
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
        return EMPTY
    out: list = []
    for entry in entries:
        track = track_from_entry(entry)
        if track is not None:
            out.append(track)
        if len(out) >= MAX_TRACKS:
            break
    return Playlist(tracks=tuple(out))


def entries_of(playlist: Any) -> tuple:
    """Plain primitives out of a playlist, a list of entries, or anything else (`()`)."""
    if isinstance(playlist, Playlist):
        return playlist.as_entries()
    return playlist_from_entries(playlist).as_entries()


def count_rows(entries: Any) -> int:
    """How many rows the state actually held, **without judging any of them**.

    Deliberately separate from :func:`playlist_from_entries`, which is a *projection* and skips
    what it cannot read. Comparing the two is the only way to notice that a row went missing, which
    is why the freeze below returns both numbers rather than just the usable ones.
    """
    if not isinstance(entries, Iterable) or isinstance(entries, (str, bytes)):
        return 0
    try:
        return sum(1 for _ in entries)
    except TypeError:
        return 0


@dataclass(frozen=True, slots=True)
class FrozenQueue:
    """What the render-admission freeze saw, including **what it could not use**.

    `entries` alone would be a lie by omission: a malformed row is skipped by the projection, so a
    three-row queue can freeze as two and render a soundtrack the operator never asked for. That is
    the exact failure this feature refuses everywhere else — a shorter soundtrack that looks
    deliberate — so the freeze reports the discrepancy and the gate refuses on it.
    """

    entries: tuple = ()
    submitted: int = 0
    over_limit: bool = False

    @property
    def usable(self) -> int:
        return len(self.entries)

    @property
    def dropped(self) -> int:
        return max(0, self.submitted - self.usable)

    @property
    def is_lossless(self) -> bool:
        """True only when every submitted row survived and none was truncated away."""
        return not self.over_limit and self.dropped == 0

    def refusal(self) -> str:
        """The operator-facing reason this queue cannot be rendered, or `''` when it can."""
        if self.over_limit:
            return (f"The playlist holds {self.submitted} tracks, which is more than the "
                    f"{MAX_TRACKS}-track limit. Remove some tracks and try again.")
        if self.dropped:
            # The noun agrees with `submitted`, which is what it is counting -- "1 of the 3 track"
            # is the kind of small wrongness that makes an operator doubt the number beside it.
            word = "track" if self.submitted == 1 else "tracks"
            return (f"{self.dropped} of the {self.submitted} {word} in the playlist could not be "
                    f"read. Clear the playlist and add the tracks again rather than rendering a "
                    f"soundtrack that is missing some of them.")
        if not self.entries:
            return EMPTY_PLAYLIST_MESSAGE
        return ""


def freeze_queue(mode: Any, entries: Any) -> FrozenQueue:
    """**The render-admission freeze.** An empty queue unless the operator declares a playlist.

    Called once, at the render boundary, beside the creative profile and both mix configs — the
    same seam :func:`music_repeat.normalize_repeat` is called at. `entries` is a tuple of 4-tuples
    of primitives: immutable, deep-copyable, and nameable inside `_process_video_impl` without that
    function resolving a single fork module. (The same reason Freestyle crosses the render wrappers
    as a plain tuple rather than as a `FreestyleDeclaration`: several suites AST-extract those
    bodies and execute them against synthesised namespaces.)

    Total by contract: it runs on live widget values, and nothing here may raise mid-render. An
    empty result in playlist mode is a **refusal** for the gate to report, never a fall back to the
    Single Track picker — and so, since R2, is a queue that lost a row to the projection or that
    arrived longer than :data:`MAX_TRACKS`. The 200-track limit is still enforced; what changed is
    that it is enforced **visibly**, by refusing, rather than by silently rendering the first 200.
    """
    if not is_playlist(mode):
        return FrozenQueue()
    submitted = count_rows(entries)
    return FrozenQueue(entries=entries_of(entries),
                       submitted=submitted,
                       over_limit=submitted > MAX_TRACKS)


# ---------------------------------------------------------------------------
# the seven editing operations
# ---------------------------------------------------------------------------


def _normalized_indices(playlist: Playlist, indices: Any) -> tuple:
    """Ascending, de-duplicated, in-range positions. Accepts one int or any iterable of them.

    De-duplicating *indices* is not de-duplicating *tracks*: position 3 selected twice is still
    position 3. The playlist itself keeps every repeat the operator asked for.
    """
    if indices is None:
        return ()
    if isinstance(indices, bool):
        return ()
    raw: list = [indices] if isinstance(indices, int) else []
    if not raw:
        if not isinstance(indices, Iterable) or isinstance(indices, (str, bytes)):
            return ()
        raw = list(indices)
    out: set = set()
    for value in raw:
        if isinstance(value, bool):
            continue
        try:
            position = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= position < playlist.count:
            out.add(position)
    return tuple(sorted(out))


def add_tracks(playlist: Any, probed: Any) -> Playlist:
    """Append newly chosen tracks, in **filename order**, keeping everything already queued.

    Ordering the *addition* is deliberate rather than cosmetic. A native multi-select dialog
    returns whatever order the OS supplies, which is not the operator's click order — exactly the
    measured defect `audio_mix.voice_order_key` exists for, so that key is reused here rather than
    a second ordering rule being invented. The operator then has Move Up / Move Down / Sort by Name
    / Shuffle to say what they actually want; what they must never get is an arrangement nobody
    chose and nobody can predict.

    Existing tracks are **not** reordered, and a file already in the queue is **not** rejected: the
    same song twice is a legitimate soundtrack.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    additions = playlist_from_entries(probed).tracks
    ordered = sorted(additions, key=lambda track: voice_order_key(track.path))
    return Playlist(tracks=tuple(current.tracks + tuple(ordered))[:MAX_TRACKS])


def remove_tracks(playlist: Any, indices: Any) -> tuple:
    """`(playlist, selection)` with the selected positions removed. Selection comes back empty.

    Returning the selection as well is the same discipline `_cut_handoff` records for the record
    and the button that reads it: a stale selection pointing at positions that no longer exist is
    how the next press removes the wrong track.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    drop = set(_normalized_indices(current, indices))
    if not drop:
        return current, ()
    kept = tuple(track for position, track in enumerate(current.tracks) if position not in drop)
    return Playlist(tracks=kept), ()


def move_up(playlist: Any, indices: Any) -> tuple:
    """`(playlist, selection)` with each selected track swapped one position earlier.

    A selection already containing position 0 cannot move, and neither can the contiguous block
    behind it — the block is shifted as a whole or not at all, so a multiple selection never gets
    silently torn apart against the top of the list. The selection **follows the tracks**, which is
    what makes repeated presses work.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    selected = _normalized_indices(current, indices)
    if not selected:
        return current, selected
    order = list(current.tracks)
    moved: list = []
    for position in selected:                       # ascending: the earliest moves first
        if position == 0 or (position - 1) in moved:
            moved.append(position)                  # blocked: it keeps its own place
            continue
        order[position - 1], order[position] = order[position], order[position - 1]
        moved.append(position - 1)
    return Playlist(tracks=tuple(order)), tuple(sorted(moved))


def move_down(playlist: Any, indices: Any) -> tuple:
    """`(playlist, selection)` with each selected track swapped one position later."""
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    selected = _normalized_indices(current, indices)
    if not selected:
        return current, selected
    order = list(current.tracks)
    last = current.count - 1
    moved: list = []
    for position in reversed(selected):             # descending: the latest moves first
        if position == last or (position + 1) in moved:
            moved.append(position)
            continue
        order[position + 1], order[position] = order[position], order[position + 1]
        moved.append(position + 1)
    return Playlist(tracks=tuple(order)), tuple(sorted(moved))


def shuffle(playlist: Any, seed: Any) -> tuple:
    """`(playlist, selection)` reordered **once**, deterministically, from `seed`.

    Two properties, and the owner asked for both:

    * **It happens now, not at render time.** The result is written straight into the queue the
      operator is looking at, and the render freezes whatever is in that queue. There is no
      randomisation anywhere on the render path — a test asserts this module is the only place in
      the feature that mentions `random`, and that nothing in the render body reaches it.
    * **It is reproducible.** `random.Random(seed)` with a recorded seed means an arrangement the
      operator liked can be recreated, which a bare `random.shuffle` could not offer.

    An unusable seed falls back to `0` rather than to system entropy, so the function stays total
    and the recorded seed always describes the arrangement.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    if current.count < 2:
        return current, ()
    order = list(current.tracks)
    random.Random(_usable_int(seed)).shuffle(order)
    return Playlist(tracks=tuple(order)), ()


def sort_by_name(playlist: Any) -> tuple:
    """`(playlist, selection)` in stable, case-insensitive filename order.

    The same key :func:`add_tracks` uses, so pressing Sort by Name after an Add is a no-op on the
    added block rather than a second, differently-ordered answer.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    ordered = sorted(current.tracks, key=lambda track: voice_order_key(track.path))
    return Playlist(tracks=tuple(ordered)), ()


def clear() -> tuple:
    """`(playlist, selection)` — an empty queue and an empty selection."""
    return EMPTY, ()


# ---------------------------------------------------------------------------
# identity changes
# ---------------------------------------------------------------------------


def describe_identity_change(track: PlaylistTrack, size: Any, mtime_ns: Any) -> str:
    """`''` when the observed file still matches the queued identity, else what changed.

    The runtime sibling does the `stat`; this says what a mismatch *means*. A recorded identity of
    `0/0` is "never probed" rather than "probed as empty", so it is not treated as a change —
    which is what lets a playlist restored from a bare path list still render.
    """
    if not isinstance(track, PlaylistTrack):
        return ""
    if track.size <= 0 and track.mtime_ns <= 0:
        return ""
    observed_size = _usable_int(size)
    observed_mtime = _usable_int(mtime_ns)
    if track.size > 0 and observed_size != track.size:
        return (f"{track.name} has changed since it was added to the playlist "
                f"({track.size} bytes then, {observed_size} now)")
    if track.mtime_ns > 0 and observed_mtime != track.mtime_ns:
        return f"{track.name} has been modified since it was added to the playlist"
    return ""


# ---------------------------------------------------------------------------
# RIFF geometry — bytes in, numbers out
# ---------------------------------------------------------------------------

#: How much of a WAV header the runtime sibling reads before giving up on finding `data`. FFmpeg
#: writes `fmt ` and a short `LIST INFO` before it, so this is generous by three orders of
#: magnitude; the point is only that the read is bounded and header-only.
WAV_HEADER_PROBE_BYTES = 8192


@dataclass(frozen=True, slots=True)
class WavGeometry:
    """The exact PCM geometry of one canonical WAV, from its header alone."""

    channels: int
    sample_rate: int
    bits_per_sample: int
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def parse_wav_header(header: Any) -> WavGeometry | None:
    """:class:`WavGeometry` from the leading bytes of a RIFF/WAVE file, or `None`.

    Why a hand-rolled chunk walk rather than :mod:`wave`: the verification this feeds is a
    **sample-count equality** check, and it has to be able to answer "I could not measure this"
    distinctly from "this is wrong". :mod:`wave` raises for several shapes a real FFmpeg-written
    24-bit file can take, and an exception is the one answer that must not be read as a mismatch —
    the same rule `frame_lock.FrameCountVerdict` records: **an unmeasured count is never a
    refusal**. So: walk the chunks, return the geometry when `fmt ` and `data` are both present and
    consistent, and `None` for every other shape.

    Returning `None` also covers the deliberately-bounded read: if `data` has not been reached
    within :data:`WAV_HEADER_PROBE_BYTES` the answer is "unmeasured", never "zero frames".
    """
    if not isinstance(header, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(header)
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None

    channels = sample_rate = bits = 0
    offset = 12
    while offset + 8 <= len(raw):
        chunk_id = raw[offset:offset + 4]
        chunk_size = int.from_bytes(raw[offset + 4:offset + 8], "little")
        body = offset + 8
        if chunk_id == b"fmt " and chunk_size >= 16 and body + 16 <= len(raw):
            channels = int.from_bytes(raw[body + 2:body + 4], "little")
            sample_rate = int.from_bytes(raw[body + 4:body + 8], "little")
            bits = int.from_bytes(raw[body + 14:body + 16], "little")
        elif chunk_id == b"data":
            frame_bytes = channels * (bits // 8)
            if channels <= 0 or sample_rate <= 0 or bits <= 0 or frame_bytes <= 0:
                return None
            # A streamed WAV can carry 0 or 0xFFFFFFFF here, neither of which is a measurement.
            if chunk_size <= 0 or chunk_size == 0xFFFFFFFF:
                return None
            return WavGeometry(channels=channels, sample_rate=sample_rate,
                               bits_per_sample=bits, frames=chunk_size // frame_bytes)
        # Chunks are word-aligned: an odd size is followed by one pad byte.
        offset = body + chunk_size + (chunk_size & 1)
    return None


# ---------------------------------------------------------------------------
# read-outs
# ---------------------------------------------------------------------------

#: Above this the Repeat Playlist read-out warns about the output length and the temporary disk
#: space, for the same reason and at the same threshold `music_repeat` warns.
LONG_OUTPUT_WARNING_SECONDS = 15 * 60


def format_bytes(value: Any) -> str:
    """Decimal MB/GB for one read-out line. Total; an unusable value answers `--`."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
        return "--"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f} GB"
    return f"{number / 1_000_000:.0f} MB"


def master_bytes(seconds: Any) -> int:
    """How much scratch WAV a soundtrack master of this length occupies. Never negative."""
    duration = _usable_seconds(seconds)
    if duration is None:
        return 0
    return int(duration * MASTER_BYTES_PER_SECOND)


def choice_labels(playlist: Any) -> list:
    """`[("1. track.mp3 (03:21)", 0), ...]` for the Dropdown that selects tracks to act on.

    The position is the **value**, so Remove / Move Up / Move Down act on an index rather than on a
    name — which is what lets the same song appear twice and still be edited unambiguously.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    return [(f"{position + 1}. {track.name} ({format_clock(track.duration_seconds)})", position)
            for position, track in enumerate(current.tracks)]


def summary_markdown(playlist: Any) -> str:
    """The queue itself: a table of position, track and length, then the count and the total.

    Deliberately a read-only Markdown table rather than a `gr.Dataframe`: it occupies no input slot
    the operator could type into, so the `gr.State` beside it stays the only authority on what the
    soundtrack is.
    """
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    if not current.count:
        return ("**Playlist is empty.** Press **Add tracks** to choose one or more music files. "
                "They play in the order shown, top to bottom.")
    lines = ["| # | Track | Length |", "|---:|:---|---:|"]
    for position, track in enumerate(current.tracks, start=1):
        lines.append(f"| {position} | {_escape_cell(track.name)} | "
                     f"{format_clock(track.duration_seconds)} |")
    word = "track" if current.count == 1 else "tracks"
    lines.append("")
    lines.append(f"**{current.count} {word} · {format_clock(current.total_seconds)} total "
                 f"soundtrack**")
    return "  \n".join(lines)


def _escape_cell(text: Any) -> str:
    """A filename containing `|` must not break the table it is being shown in."""
    return str(text).replace("|", "\\|")


def repeat_estimate_markdown(playlist: Any, count: Any) -> str:
    """The pre-render read-out **and** the Repeat Playlist label, in playlist mode.

    Three states, distinguished rather than hedged — the same three
    :func:`music_repeat.estimate_markdown` distinguishes, so the two modes read alike:

    * **empty queue** — say so and say nothing about the output;
    * **1x** — state the soundtrack's own length and that it plays once;
    * **2x..30x** — the arithmetic, plus the two warnings a long render actually needs.

    `count` is normalised here rather than trusted, and the normaliser is imported from
    :mod:`music_repeat` so there is still exactly one definition of what a repeat count means.
    """
    repeat = normalize_repeat(count)
    current = playlist if isinstance(playlist, Playlist) else playlist_from_entries(playlist)
    if not current.count:
        return (f"**Repeat Playlist: {repeat}x** - add tracks to see the estimated output length.")

    word = "track" if current.count == 1 else "tracks"
    clock = format_clock(current.total_seconds)
    if repeat <= MIN_REPEAT:
        return (f"**{current.count} {word} · {clock}** x 1 = **{clock}** - the playlist plays "
                f"once (no repeat).")

    total = total_seconds_for(current.total_seconds, repeat)
    lines = [f"**{current.count} {word} · {clock}** x {repeat} = **{format_clock(total)}** of "
             f"finished video. The **whole playlist** repeats, in order - not each song."]
    # [R2 / F3] The format ceiling is not a warning, it is a refusal the render will make -- so the
    # operator must see it on the slider rather than after a long wait. The arithmetic is the one
    # `capacity_verdict` uses, so the read-out and the refusal cannot disagree.
    verdict = capacity_verdict(current.total_seconds, repeat)
    if verdict.reason == REFUSAL_FORMAT:
        lines.append(f"❌ **This will be refused.** {verdict.message}")
    elif total >= LONG_OUTPUT_WARNING_SECONDS:
        lines.append(
            f"⚠️ That is a long render. At the worst moment it needs about "
            f"**{format_bytes(repeat_peak_bytes(current.total_seconds, repeat))}** of temporary "
            f"disk space — the playlist master, the repeat's own copy of it and the repeated "
            f"master all exist at once — plus the usual per-clip scratch.")
    return "  \n".join(lines)


# ---------------------------------------------------------------------------
# capacity and format limits (R2 / F3)
# ---------------------------------------------------------------------------

#: Refusal codes. Each is a different fact about the request, and the GUI classifies them
#: differently: a format refusal is about what was asked for and every candidate of a batch would
#: hit it identically; a space refusal is about this volume at this instant and is not.
REFUSAL_NONE = ""
REFUSAL_FORMAT = "format_ceiling"
REFUSAL_SPACE = "insufficient_space"


@dataclass(frozen=True, slots=True)
class CapacityVerdict:
    """Whether a requested soundtrack — repeated or not — can actually be written.

    **`guaranteed` is the whole point of this record.** Before anything is decoded the only figures
    available are the source containers' own durations, which are an *estimate*: a VBR MP3's
    container duration is routinely a few milliseconds out, and the decode is what settles it. Once
    the playlist master exists its cycle length is a **measurement**. The same arithmetic serves
    both, and the verdict says which it was given, so a refusal can be honest about its own
    certainty and an *estimated* pass is never reported as a promise.

    Nothing here is a reservation either. Free space is one instant on a volume any other process
    may fill a moment later; this can refuse an impossible job, never promise a possible one.
    """

    reason: str = REFUSAL_NONE
    message: str = ""
    guaranteed: bool = False
    repeat: int = 1
    cycle_seconds: float = 0.0
    output_seconds: float = 0.0
    output_bytes: int = 0
    peak_bytes: int = 0
    free_bytes: int = -1

    @property
    def ok(self) -> bool:
        return self.reason == REFUSAL_NONE


def repeat_output_bytes(cycle_seconds: Any, count: Any) -> int:
    """How large the finished (possibly repeated) audio master will be, in bytes."""
    return master_bytes(total_seconds_for(cycle_seconds, count))


def total_seconds_for(cycle_seconds: Any, count: Any) -> float:
    """`repeat × cycle`, with the repeat normalised through the one normaliser."""
    duration = _usable_seconds(cycle_seconds)
    if duration is None:
        return 0.0
    return normalize_repeat(count) * duration


def repeat_peak_bytes(cycle_seconds: Any, count: Any) -> int:
    """Peak temporary bytes for a **repeated** playlist render, counting what overlaps.

    Three artifacts coexist at the worst moment, and leaving any of them out is how a check passes
    and the write then fails:

    * the joined **playlist master**, which is already on disk and is R10's input — `1 × cycle`;
    * R10's own decoded **cycle copy**, which it makes from that master — `1 × cycle`;
    * the **repeated master** it concatenates — `N × cycle`.

    So `(N + 2) × cycle`. At 1x R10 is never entered, so the repeat stage contributes nothing and
    the playlist build's own `2 ×` peak (see :func:`music_playlist_builder.estimate_scratch_bytes`)
    is the binding figure instead.
    """
    repeat = normalize_repeat(count)
    if repeat <= MIN_REPEAT:
        return 0
    one = master_bytes(cycle_seconds)
    return one * (repeat + 2)


def capacity_verdict(cycle_seconds: Any, count: Any, free_bytes: Any = None, *,
                     guaranteed: bool = False, min_free_bytes: int = 0,
                     ceiling_bytes: int = MAX_MASTER_BYTES) -> CapacityVerdict:
    """Can this repeat request be written? Refuse the impossible; never promise the possible.

    Two independent impossibilities, deliberately distinguished because they are different facts
    with different fixes and different batch semantics:

    * **the format's own ceiling** — `N × cycle` above `ceiling_bytes` cannot be one WAV at all, on
      any volume, ever. Lowering the repeat count is the fix, and every candidate of a batch would
      hit it identically;
    * **this volume, right now** — the overlapping peak plus the caller's reserve against the free
      space actually measured. Freeing space or lowering the repeat is the fix, and it may be
      transient.

    `free_bytes` of `None` or a negative value means **unmeasured**, which is never a refusal: the
    format check still runs, and the space check is simply not performed. That is the same rule the
    sample-count verification follows — an instrument that could not read must not become a verdict.
    """
    repeat = normalize_repeat(count)
    duration = _usable_seconds(cycle_seconds) or 0.0
    output_seconds = total_seconds_for(duration, repeat)
    output = master_bytes(output_seconds)
    peak = repeat_peak_bytes(duration, repeat)
    free = _usable_int(free_bytes) if _as_measured(free_bytes) else -1
    common = dict(guaranteed=bool(guaranteed), repeat=repeat, cycle_seconds=float(duration),
                  output_seconds=float(output_seconds), output_bytes=int(output),
                  peak_bytes=int(peak), free_bytes=int(free))

    if output > ceiling_bytes:
        qualifier = "" if guaranteed else "about "
        return CapacityVerdict(
            reason=REFUSAL_FORMAT,
            message=(f"Repeat Playlist {repeat}x of a {format_clock(duration)} playlist is "
                     f"{qualifier}{format_clock(output_seconds)} of audio "
                     f"({format_bytes(output)}), which is more than one WAV soundtrack file can "
                     f"hold ({format_bytes(ceiling_bytes)}). Lower the repeat count to "
                     f"{max(1, int(ceiling_bytes // max(1, master_bytes(duration))))}x or less, "
                     f"or shorten the playlist."),
            **common)

    if free >= 0 and peak and free < peak + max(0, int(min_free_bytes)):
        needed = peak + max(0, int(min_free_bytes))
        qualifier = "needs" if guaranteed else "needs about"
        return CapacityVerdict(
            reason=REFUSAL_SPACE,
            message=(f"Not enough temporary space for Repeat Playlist {repeat}x: it {qualifier} "
                     f"{format_bytes(needed)} while the playlist master, the repeat's own copy of "
                     f"it and the repeated master all exist at once, and {format_bytes(free)} is "
                     f"free. Lower the repeat count, shorten the playlist, or free some space."),
            **common)

    return CapacityVerdict(**common)


def _as_measured(value: Any) -> bool:
    """True only for a real, non-negative byte count. `None` is *unmeasured*, not zero.

    `OverflowError` is caught for the same reason :func:`_usable_int` catches it: an infinity is
    not a measurement, and finding that out must not raise in the middle of a render.
    """
    if value is None or isinstance(value, bool):
        return False
    try:
        return int(value) >= 0
    except (TypeError, ValueError, OverflowError):
        return False


def bus_record(beat_info: Any) -> Mapping | None:
    """The diagnostics record this feature left on the bus, or `None` on a single-track render.

    One reader for one key, exactly as `music_repeat.bus_record` is the one reader of
    `"music_repeat"`, so the playlist facts cannot fork into two independently-parsed views.
    """
    if not isinstance(beat_info, Mapping):
        return None
    record = beat_info.get(BUS_KEY)
    return record if isinstance(record, Mapping) else None


def summary_line(beat_info: Any) -> str:
    """The success panel's playlist line, or `''` when the render was not a playlist render.

    Reads the bus record and formats it; it never recomputes, re-measures or re-derives anything,
    so the panel can never become a second authority on what the render actually did. The
    boundaries it prints are the **measured** offsets of the decoded PCM, which is the whole reason
    they are worth printing.
    """
    record = bus_record(beat_info)
    if record is None:
        return ""
    tracks = record.get("tracks")
    # `str` and `bytes` are Sequences, so excluding them explicitly is not defensive noise: a bus
    # whose `tracks` is a string would otherwise iterate character by character and print a
    # confident, nonsensical track list.
    if (not isinstance(tracks, Sequence) or isinstance(tracks, (str, bytes))
            or not tracks):
        return ""
    total = _usable_seconds(record.get("total_seconds")) or 0.0
    word = "track" if len(tracks) == 1 else "tracks"
    lines = [f"🎼 MUSIC PLAYLIST: {len(tracks)} {word} · {format_clock(total)} soundtrack"]
    for position, entry in enumerate(tracks, start=1):
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name") or "(track)"
        start = _usable_seconds(entry.get("start_seconds"))
        start_text = format_clock(0.0 if start is None else start)
        lines.append(f"   {position}. {start_text}  {name}  "
                     f"({format_clock(entry.get('duration_seconds'))})")
    return "\n".join(lines)


__all__ = [
    "BUS_KEY",
    "DEFAULT_MODE",
    "EMPTY",
    "EMPTY_PLAYLIST_MESSAGE",
    "LONG_OUTPUT_WARNING_SECONDS",
    "MASTER_BYTES_PER_SECOND",
    "MAX_MASTER_BYTES",
    "MAX_TRACKS",
    "MODE_PLAYLIST",
    "MODE_SINGLE",
    "MODE_VALUES",
    "REFUSAL_FORMAT",
    "REFUSAL_NONE",
    "REFUSAL_SPACE",
    "SUPPORTED_TRACK_EXTENSIONS",
    "WAV_HEADER_PROBE_BYTES",
    "CapacityVerdict",
    "FrozenQueue",
    "Playlist",
    "PlaylistTrack",
    "WavGeometry",
    "add_tracks",
    "bus_record",
    "capacity_verdict",
    "choice_labels",
    "clear",
    "count_rows",
    "describe_identity_change",
    "entries_of",
    "format_bytes",
    "freeze_queue",
    "has_supported_extension",
    "is_playlist",
    "master_bytes",
    "move_down",
    "move_up",
    "normalize_mode",
    "parse_wav_header",
    "playlist_from_entries",
    "remove_tracks",
    "repeat_estimate_markdown",
    "repeat_output_bytes",
    "repeat_peak_bytes",
    "shuffle",
    "sort_by_name",
    "summary_line",
    "summary_markdown",
    "total_seconds_for",
    "track_from_entry",
]
