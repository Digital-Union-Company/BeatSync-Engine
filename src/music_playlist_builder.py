#!/usr/bin/env python3
"""[FORK] Digital-Union (R16 Music Playlist Mode V1): the runtime half — building the soundtrack.

:mod:`beatsync_fork.music_playlist` decides *what* a playlist means (the control, the ordering
operations, the identity, the read-outs, the RIFF geometry arithmetic). This module does the things
that need the portable FFmpeg and a filesystem: it validates the operator's ordered selection,
decodes each track to one canonical PCM representation, joins them into a single continuous
soundtrack master, and **measures** where every track actually begins inside it.

The dependency is one-way: this module imports the pure one, never the reverse. It is
:mod:`music_repeater`'s structural sibling — same pure/runtime split, same ``session_dir`` scratch
ownership, same loud-failure discipline — and deliberately **not** part of it: a playlist failure is
about the operator's own music files or about this render's generated artifact, not about a repeat
count, so ``PlaylistError`` is a plain ``Exception`` and the existing
``except music_repeater.MusicRepeatError`` handler in the render body keeps meaning exactly what it
meant before.

===============================================================================
Three commands' worth of work, and why it is shaped this way
===============================================================================

::

    1. VALIDATE      the WHOLE ordered selection -- identity, extension, readability, duration
    2. DECODE EACH   ffmpeg -i <track> -vn -map 0:a:0 -c:a pcm_s24le -ar 48000 -ac 2  NN.wav
    3. JOIN          ffmpeg -f concat -safe 0 -i list.txt -c copy                     master.wav
    4. VERIFY        the master's SAMPLE COUNT equals the sum of the decoded tracks', exactly,
                     and its probed duration agrees to within one millisecond

**Decode-then-stream-copy, not one concat-filter pass.** The single-command alternative —
``-filter_complex "[0:a][1:a]concat=n=2:v=0:a=1"`` — would need no intermediates at all, and it was
rejected for the same reason ``-stream_loop`` was rejected for Repeat Music: it makes the result
something this feature cannot measure. Decoding each track to its own canonical WAV means every
track's contribution to the soundtrack is a **byte run whose sample count is a fact on disk**, so
step 4 is an equality check against measured reality rather than an assertion about what a
filtergraph probably did. It is also what makes the track boundaries in the render report real
measurements instead of a cumulative sum of container metadata.

**There is no gap between two tracks, by construction.** The master is a stream copy of consecutive
PCM runs — there is nothing between them for a gap to live in — and nothing is faded, aligned,
padded or loudness-matched. V1 joins the songs; it does not mix them. The operator's files are
opened read-only and are never modified, moved or deleted.

===============================================================================
Scratch, and what it costs
===============================================================================

Peak temporary use is **about twice the soundtrack** — every decoded track has to exist while the
join reads them — at the canonical 288 kB/s, so a 40-minute playlist peaks at roughly 1.4 GB. That
is bounded, it is checked up front (:func:`estimate_scratch_bytes` against the real volume), it is
stated in the Render tab's read-out before the operator commits, and the intermediates are removed
by this function's own ``finally`` on **every** path. Decoding into a growing file to halve the peak
was considered and rejected: it would replace the repository's verified concatenation mechanism with
a hand-written RIFF writer, which is a new correctness surface for a saving the operator can already
see coming.

Everything lives in ``session_dir``, **never** ``get_processing_dir()``, which
``create_music_video`` clears at the start of every render and which would therefore delete the
soundtrack moments after it was written. Same rule and same reason as ``audio_mixdown.master_path_for``
and ``music_repeater.repeated_master_path``.

===============================================================================
Failure is loud, and it is never partial
===============================================================================

If the operator queued five tracks and this can only deliver four, the render **stops**. A
four-track soundtrack would be a plausible-looking render that silently dropped a song — exactly
what ``prepare_voice_inputs`` refuses to do for voice, and for the same reason: the result looks
deliberate. So the selection is validated **whole** and the first unusable entry raises; the caller
never receives a subset.

Two exception types, because the render body classifies them differently and must not guess:

``PlaylistInputError``
    A queued track is missing, changed, unreadable, unsupported or empty. A statement about the
    operator's files, which are frozen for a whole Variant Lab batch -> ``SHARED_FATAL``.

``PlaylistBuildError``
    Producing or verifying the master failed — a timeout, a non-zero FFmpeg, a missing or empty
    output, a sample-count mismatch. Indistinguishable here from a broken binary or a full disk,
    and this repository has no structured FFmpeg diagnostic classification able to tell them apart,
    so it fails closed -> ``UNKNOWN_FATAL``.

``RenderCancelled`` propagates **unchanged** through both and is never re-typed. Re-typing it would
report a user's Stop as a broken music file, and in a batch would describe a cancelled candidate as
a problem with their playlist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass

from ffmpeg_processing import FFMPEG_PATH, FFPROBE_PATH

from beatsync_fork import music_playlist as fork_playlist
# [FORK] Digital-Union (C3-R1A/FUN Stage-5 Cancel): the pure cancellation contract. This module
# keeps its own small cancellable runner below rather than importing `ffmpeg_processing`'s,
# `audio_mixdown`'s or `music_repeater`'s -- the process handle always belongs to the call frame
# that creates it, which is the established convention those three already follow.
from beatsync_fork.render_worker import RenderCancelled

#: The soundtrack master's format. Identical to what `music_repeater` and `audio_mixdown` already
#: encode and what final assembly lays down, so no stage downstream has resampling or
#: requantisation work to do, and `music_repeater` can stream-copy this file's cycle verbatim when
#: Repeat Playlist is above 1x.
TRACK_SAMPLE_RATE = 48_000
TRACK_CHANNELS = 2
TRACK_CODEC = "pcm_s24le"
#: Stated so `probe_frame_count` can report a geometry without re-deriving the width from the
#: codec name. The decode asks for `pcm_s24le`, so this is what every artifact here carries.
TRACK_BITS = 24

#: How far the produced master's probed duration may differ from the sum of the decoded tracks'.
#: One PCM sample at 48 kHz is 0.021 ms; 1 ms is generous against ffprobe's reporting precision and
#: still far below one video frame, which is what matters because `create_music_video` derives the
#: frame-locked timeline from this file's duration. The **sample-count** check below is exact and is
#: the primary verification; this is the independent cross-check.
DURATION_TOLERANCE_SECONDS = 0.001

#: Decoding is linear in a track's length and a long playlist has many of them, so these are
#: deliberately generous rather than tight. A real timeout is still a stuck FFmpeg and is reported
#: as one; it is never confused with a Stop.
_DECODE_TIMEOUT_SECONDS = 900
_JOIN_TIMEOUT_SECONDS = 3600
_PROBE_TIMEOUT_SECONDS = 60

#: Same shape as the three existing runners' poll/grace pair, deliberately its own values.
_CANCEL_POLL_SECONDS = 0.15
_TERMINATE_GRACE_SECONDS = 5.0

#: Bounded so a loudly-failing FFmpeg cannot paste megabytes into a status panel.
_STDERR_TAIL = 1200

#: Headroom demanded on top of the estimated peak before any decoding starts. Small enough to be
#: noise on any volume a video tool is pointed at, large enough that the refusal lands before the
#: damage rather than after it -- the same reasoning `disk_guard.MIN_FREE_EXPORT_BYTES` records,
#: restated here rather than imported because that module is the Raw Cutter's and carries the
#: cutter's whole vocabulary with it.
MIN_FREE_SCRATCH_BYTES = 256 * 1024 * 1024

#: The WAV format ceiling. **One definition**, in the pure module, because R2 needs the same number
#: for two different artifacts — the joined playlist master and the repeated master R10 builds from
#: it — and two copies of a limit are two limits waiting to disagree. Re-exported here because this
#: is the module whose refusals cite it.
MAX_MASTER_BYTES = fork_playlist.MAX_MASTER_BYTES

#: How a track's geometry was established. A **measurement**, always — R2 removed the estimated
#: path entirely. `read_wav_geometry` is free and usually enough; when a header is shaped so the
#: `data` chunk sits past the bounded read, ffprobe measures the same file exactly
#: (`duration_ts` in a `1/sample_rate` time base IS the sample count, verified against a real
#: 288 000-frame WAV). If neither instrument can read it, the render is **refused** rather than
#: proceeding on container durations dressed up as boundaries.
MEASURED_BY_HEADER = "header"
MEASURED_BY_PROBE = "probe"


class PlaylistError(Exception):
    """Base: the ordered soundtrack could not be produced. Carries a short, bounded reason.

    Deliberately **not** a ``music_repeater.MusicRepeatError`` subclass and **not** an
    ``audio_mixdown.AudioMixError`` subclass. The three features fail for unrelated reasons and
    ``gui.py`` classifies them differently; the existing handlers in the render body must keep
    meaning exactly what they mean today.
    """


class PlaylistInputError(PlaylistError):
    """A queued track is missing, changed, unreadable, unsupported or empty."""


class PlaylistBuildError(PlaylistError):
    """Producing or verifying the soundtrack master failed."""


@dataclass(frozen=True)
class ValidatedTrack:
    """One queued track that passed the preflight, **carrying the identity that was validated**.

    [R2 / F2] The preflight runs before Stage 1 and the decode runs after Stage 5 — minutes apart
    on a cold library. A `(path, duration)` pair carried nothing the consumption boundary could
    re-check, so a track replaced in that window was decoded without comment and the operator got a
    soundtrack they did not ask for. Carrying the size and `mtime_ns` the preflight actually saw is
    what lets :func:`build_playlist_master` re-stat each file immediately before it decodes it.
    """

    path: str
    size: int
    mtime_ns: int
    source_duration_seconds: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path.replace("\\", "/")) or self.path

    def as_playlist_track(self):
        """The pure module's record, so one identity comparison serves the preflight and the decode."""
        return fork_playlist.PlaylistTrack(
            path=self.path, size=self.size, mtime_ns=self.mtime_ns,
            duration_seconds=self.source_duration_seconds)


@dataclass(frozen=True)
class PreparedTrack:
    """One track's place in the finished soundtrack. Immutable; carries no handle of any kind.

    ``start_seconds`` and ``duration_seconds`` are derived from **measured sample counts** of the
    decoded PCM, not from the source container's metadata — which is the whole reason the decode is
    per-track. ``source_duration_seconds`` is kept alongside so a report can show both when they
    disagree (a VBR MP3's container duration routinely differs from its decoded length by a few
    milliseconds).
    """

    index: int
    path: str
    name: str
    start_frame: int
    frames: int
    sample_rate: int
    source_duration_seconds: float

    @property
    def start_seconds(self) -> float:
        return self.start_frame / self.sample_rate if self.sample_rate else 0.0

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    def as_record(self) -> dict:
        """The plain-primitive form that rides `beat_info` for the render report."""
        return {
            "index": int(self.index),
            "name": self.name,
            "path": self.path,
            "start_frame": int(self.start_frame),
            "frames": int(self.frames),
            "sample_rate": int(self.sample_rate),
            "start_seconds": float(self.start_seconds),
            "duration_seconds": float(self.duration_seconds),
            "source_duration_seconds": float(self.source_duration_seconds),
        }


@dataclass(frozen=True)
class PlaylistMaster:
    """What one successful preparation produced.

    ``total_seconds`` is the master's own **measured** length, which is what the analysis and the
    frame-locked timeline are both derived from. When Repeat Playlist is above 1x this file becomes
    ``music_repeater``'s input, so this duration is the cycle R10 then measures for itself — which
    is exactly how "Repeat Playlist repeats the whole ordered playlist" falls out of the existing
    code rather than needing new code.
    """

    path: str
    tracks: tuple
    total_frames: int
    sample_rate: int
    total_seconds: float
    prepare_seconds: float
    #: Which instrument established the geometry -- :data:`MEASURED_BY_HEADER` or
    #: :data:`MEASURED_BY_PROBE`. Never absent: R2 removed the estimated path, so a master that
    #: exists was measured, and the only question left is by what.
    measurement: str = MEASURED_BY_HEADER

    @property
    def count(self) -> int:
        return len(self.tracks)

    def describe(self) -> str:
        word = "track" if self.count == 1 else "tracks"
        return (f"{self.count} {word} -> "
                f"{fork_playlist.format_clock(self.total_seconds)} soundtrack")

    def bus_record(self) -> dict:
        """The diagnostics record this feature leaves on the bus. Reporting only.

        Nothing in Stage 4, Stage 5, Stage 6 or any cache reads it —
        ``music_playlist.summary_line`` is its one reader, exactly as
        ``music_repeat.summary_line`` is the one reader of ``"music_repeat"``.
        """
        return {
            "tracks": [track.as_record() for track in self.tracks],
            "total_frames": int(self.total_frames),
            "sample_rate": int(self.sample_rate),
            "total_seconds": float(self.total_seconds),
            "measurement": str(self.measurement),
        }


def _tail(text) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed[-_STDERR_TAIL:]


def _run(command, timeout, lifecycle=None):
    """Run one FFmpeg/FFprobe command. ``lifecycle`` defaults to ``None`` (blocking, unchanged).

    Its own independent implementation of the cancellable shape ``ffmpeg_processing``,
    ``audio_mixdown`` and ``music_repeater`` each carry, so the child this frame creates is reaped
    by this frame. A real timeout still raises ``subprocess.TimeoutExpired``; only a matching
    cancellation raises ``RenderCancelled``, and only after the child is provably reaped.
    """
    if lifecycle is None:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              check=False)

    started = time.perf_counter()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=_CANCEL_POLL_SECONDS)
                return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if lifecycle.cancel_requested():
                    proc.terminate()
                    try:
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    raise RenderCancelled("playlist soundtrack preparation cancelled")
                if time.perf_counter() - started > timeout:
                    proc.kill()
                    proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
                    raise subprocess.TimeoutExpired(command, timeout)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
                proc.communicate(timeout=_TERMINATE_GRACE_SECONDS)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# argv builders -- dependency-free, so the suite can assert on them directly
# ---------------------------------------------------------------------------


def build_decode_command(track_path: str, output_path: str) -> list:
    """Decode one queued track to the canonical PCM representation.

    ``-vn`` and ``-map 0:a:0`` together are what make this a decode of *the music* rather than of
    whatever else the container holds: an MP3 with embedded cover art presents a video stream, and
    an M4A or MKV may carry several audio tracks. The first audio stream is the one an operator
    means by "this song", and it is the one a single-track render would have analysed.

    ``-nostdin`` because this runs inside a GUI worker thread with no console to read from, and
    ``-y`` because the destination is a uuid path this process just minted.
    """
    return [
        FFMPEG_PATH, "-nostdin", "-y",
        "-i", track_path,
        "-vn", "-map", "0:a:0",
        "-c:a", TRACK_CODEC,
        "-ar", str(TRACK_SAMPLE_RATE),
        "-ac", str(TRACK_CHANNELS),
        output_path,
    ]


def build_join_command(list_path: str, output_path: str) -> list:
    """Stream-copy the decoded tracks, in order, into one continuous soundtrack.

    ``-c copy`` is the whole point — the tracks are already in the canonical format, so there is no
    re-encode, no requantisation and nothing between two songs for a gap to live in. ``-safe 0`` is
    required because the list holds absolute paths.
    """
    return [
        FFMPEG_PATH, "-nostdin", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        output_path,
    ]


def build_join_list(decoded_paths) -> str:
    """The concat demuxer's list file contents: the decoded tracks, in playlist order.

    Separators are normalised to ``/`` — the demuxer accepts them on Windows and they need no
    escaping — and a literal single quote is escaped the way the demuxer specifies, so a path under
    a user profile containing an apostrophe cannot break the file or inject a directive.
    """
    lines = []
    for path in decoded_paths:
        quoted = str(path).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{quoted}'\n")
    return "".join(lines)


def master_path_for(session_dir: str) -> str:
    """A fresh soundtrack path per render, so a stale master can never be picked up by a later one."""
    return os.path.join(session_dir, f"beatsync_playlist_{uuid.uuid4().hex}.wav")


def estimate_scratch_bytes(total_seconds) -> int:
    """The peak temporary use of one preparation: the decoded tracks plus the joined master.

    Every decoded track has to exist while the join reads them, so the peak is **two** copies of
    the soundtrack, not one. Stated rather than discovered.
    """
    one = fork_playlist.master_bytes(total_seconds)
    return one * 2


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


def read_wav_geometry(path: str):
    """The PCM geometry of a canonical WAV from its header, or ``None`` when it cannot be read.

    Header-only and bounded: it reads :data:`music_playlist.WAV_HEADER_PROBE_BYTES` and hands the
    bytes to the pure parser. ``None`` means *this instrument could not read it* — the **first**
    of two, not the last word; see :func:`measure_frames`.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(fork_playlist.WAV_HEADER_PROBE_BYTES)
    except OSError:
        return None
    return fork_playlist.parse_wav_header(header)


def probe_frame_count(path: str, lifecycle=None):
    """The exact sample count of one PCM file, from ffprobe. ``None`` when it cannot be measured.

    [R2 / F2] The **secondary** measurement, and the reason R16 no longer has an estimated path.
    For a PCM WAV the stream's ``time_base`` is ``1/sample_rate`` and ``duration_ts`` is therefore
    the sample count itself — not a duration in seconds that would have to be multiplied back out
    and rounded. Verified against a real 288 000-frame 48 kHz master: the header said 288 000 and
    so did ffprobe, including on a file whose ``data`` chunk sat past the bounded header read.

    The time base is **checked, not assumed**: a stream whose ``time_base`` is anything other than
    ``1/sample_rate`` is reported as unmeasured rather than silently converted, because the
    conversion would reintroduce exactly the rounding this function exists to avoid.

    Never raises for a measurement failure — an instrument that cannot read must not become a
    verdict. It is the *caller* that decides a double failure is fatal.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    command = [FFPROBE_PATH, "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=duration_ts,time_base,sample_rate,channels",
               "-of", "default=noprint_wrappers=1", path]
    try:
        result = _run(command, _PROBE_TIMEOUT_SECONDS, lifecycle=lifecycle)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    fields: dict = {}
    for line in (result.stdout or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    try:
        duration_ts = int(fields["duration_ts"])
        sample_rate = int(fields["sample_rate"])
        channels = int(fields["channels"])
        numerator, _, denominator = fields["time_base"].partition("/")
        numerator, denominator = int(numerator), int(denominator)
    except (KeyError, TypeError, ValueError):
        return None
    if duration_ts <= 0 or sample_rate <= 0 or channels <= 0:
        return None
    if numerator != 1 or denominator != sample_rate:
        return None                                # not a sample-accurate time base; say so
    return fork_playlist.WavGeometry(channels=channels, sample_rate=sample_rate,
                                     bits_per_sample=TRACK_BITS, frames=duration_ts)


def measure_frames(path: str, lifecycle=None):
    """``(geometry, instrument)`` for one PCM artifact, or ``(None, '')`` if neither could read it.

    The header parse is free and answers for every file FFmpeg normally writes; ffprobe is the
    fallback and is exact. Two independent instruments is what lets R16 require a **real**
    measurement — the scope asked for the produced audio to be verified against the actual decoded
    tracks, and an estimated geometry presented as a verified one would not be that.
    """
    geometry = read_wav_geometry(path)
    if geometry is not None:
        return geometry, MEASURED_BY_HEADER
    geometry = probe_frame_count(path, lifecycle=lifecycle)
    if geometry is not None:
        return geometry, MEASURED_BY_PROBE
    return None, ""


def probe_duration(path: str, lifecycle=None) -> float:
    """Media duration in seconds, via the project's portable ffprobe.

    Raises rather than guessing: ``ffmpeg_processing.get_video_duration`` answers ``10.0`` when a
    probe fails, which is right for its caller and catastrophic here — a guessed track length would
    put every later track at the wrong offset in the report and, for the total, would misstate the
    whole soundtrack.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    command = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = _run(command, _PROBE_TIMEOUT_SECONDS, lifecycle=lifecycle)
    except subprocess.TimeoutExpired:
        raise PlaylistInputError(f"Timed out reading the duration of {os.path.basename(path)}")
    if result.returncode != 0:
        raise PlaylistInputError(
            f"Could not read {os.path.basename(path)}: {_tail(result.stderr) or 'ffprobe failed'}")
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise PlaylistInputError(f"Could not read a duration from {os.path.basename(path)}")
    if duration != duration or duration in (float("inf"), float("-inf")) or duration <= 0.0:
        raise PlaylistInputError(
            f"{os.path.basename(path)} reports an unusable duration ({result.stdout.strip()})")
    return duration


def discard_playlist_master(path) -> None:
    """Best-effort removal of a temporary artifact. Never raises: cleanup must not mask a result."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# validation -- the whole selection, never a subset
# ---------------------------------------------------------------------------


def prepare_playlist_tracks(entries, lifecycle=None) -> tuple:
    """Validate the operator's **whole** ordered selection and freshly measure every track.

    Runs before Stage 1 and before any decoding, so a fixable input problem costs no analysis and
    no FFmpeg work. Every queued entry must survive into validation: a non-empty playlist may never
    come back as a smaller valid subset, so the collection is taken whole, each entry is checked in
    **playlist order**, and the *first* unusable one raises — the caller never receives a partial
    soundtrack. This is `prepare_voice_inputs`' contract, restated for the primary input.

    What is checked, in this order, and why each is here:

    * **a usable path value** — a stale `gr.State` can hold anything;
    * **a supported extension** — a clear refusal beats an opaque FFmpeg error, though the decode
      remains the real authority (see `music_playlist.SUPPORTED_TRACK_EXTENSIONS`);
    * **the file exists and is a file** — the operator may have moved it since queueing it;
    * **it is not empty** — a zero-byte file is a download in progress, not a song;
    * **its recorded identity still holds** — size and `mtime_ns` as captured when it was added, so
      a file *replaced* between queueing and rendering is reported rather than silently rendered;
    * **ffprobe reports a usable duration** — a positive, finite number.

    Returns :class:`ValidatedTrack` records in playlist order, **each carrying the identity this
    function actually saw** so :func:`build_playlist_master` can re-check it at the moment of use.
    Durations are the source containers'; the authority on the soundtrack geometry is the measured
    decode.
    """
    playlist = fork_playlist.playlist_from_entries(entries)
    if not playlist.count:
        raise PlaylistInputError(fork_playlist.EMPTY_PLAYLIST_MESSAGE)

    prepared: list = []
    total = playlist.count
    for position, track in enumerate(playlist.tracks, start=1):
        if lifecycle is not None:
            lifecycle.raise_if_cancelled()
        where = f"Track {position} of {total}"
        path = track.path
        if not isinstance(path, str) or not path.strip():
            raise PlaylistInputError(f"{where} is not a usable file path")
        if not fork_playlist.has_supported_extension(path):
            raise PlaylistInputError(
                f"{where} ({track.name}) is not a supported type "
                f"({', '.join(fork_playlist.SUPPORTED_TRACK_EXTENSIONS)})")
        try:
            stat = os.stat(path)
        except OSError as exc:
            raise PlaylistInputError(
                f"{where} is missing or unreadable: {path}") from exc
        if not os.path.isfile(path):
            raise PlaylistInputError(f"{where} is not a file: {path}")
        if stat.st_size <= 0:
            raise PlaylistInputError(f"{where} ({track.name}) is empty")
        changed = fork_playlist.describe_identity_change(track, stat.st_size, stat.st_mtime_ns)
        if changed:
            raise PlaylistInputError(
                f"{where}: {changed}. Remove it and add it again, or press Add tracks to "
                f"re-measure the playlist.")
        # The identity recorded here is the one the CONSUMPTION boundary re-checks. Taken from the
        # `stat` above rather than from the queued entry, so a track added with no recorded
        # identity (a bare path) still gains one to be held to.
        prepared.append(ValidatedTrack(
            path=path,
            size=int(stat.st_size),
            mtime_ns=int(stat.st_mtime_ns),
            source_duration_seconds=probe_duration(path, lifecycle=lifecycle)))
    return tuple(prepared)


def _as_validated(track) -> ValidatedTrack:
    """Accept only a :class:`ValidatedTrack`. A bare pair would carry no identity to re-check.

    R2 deliberately tightened this rather than accepting both shapes: two accepted shapes is two
    interpretations of "a track that passed the preflight", and the one without an identity is the
    one that silently skips the consumption-boundary check.
    """
    if isinstance(track, ValidatedTrack):
        return track
    raise PlaylistBuildError(
        "The playlist soundtrack was asked to build from an unvalidated track record. "
        "prepare_playlist_tracks() must produce the tracks build_playlist_master() consumes.")


# ---------------------------------------------------------------------------
# the soundtrack master
# ---------------------------------------------------------------------------


def build_playlist_master(tracks, session_dir: str, lifecycle=None,
                          on_track=None, repeat=1) -> PlaylistMaster:
    """Produce the one continuous soundtrack master. Raises :class:`PlaylistError` on any failure.

    ``tracks`` is what :func:`prepare_playlist_tracks` returned — an already-validated, already-
    ordered sequence of ``(path, source_duration_seconds)`` pairs. Validating again here would be a
    second interpretation of what a usable track is; the split is deliberate so that the cheap check
    can run early in the render body and the expensive work can run late.

    ``on_track`` is an optional ``(position, total, name)`` callback for progress. It is a plain
    callable rather than a progress object because this module must know nothing about
    ``ProgressEvent`` or Gradio: the GUI decides what a decode means on screen.

    ``repeat`` is the Repeat Playlist count the render will apply **after** this function returns.
    It changes nothing about what is built here — the master is always one cycle — and exists for
    one reason: so the pre-admission check can refuse an impossible *total* before paying for the
    decode. That is an **estimate**, from the source containers' durations; the guaranteed check
    runs afterwards against the measured cycle, in
    :func:`playlist_repeat_capacity`.

    Every intermediate this function creates is its own and is removed before it returns, on every
    path. The master it returns is the caller's to discard; :func:`discard_playlist_master` is the
    one way to do that.

    ``RenderCancelled`` propagates **unchanged** — never re-typed into a :class:`PlaylistError`.
    Partial output is discarded on that path too, so a cancelled preparation leaves no truncated
    WAV behind for anything to read.
    """
    ordered = [_as_validated(track) for track in tracks]
    if not ordered:
        raise PlaylistInputError(fork_playlist.EMPTY_PLAYLIST_MESSAGE)
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    if not session_dir or not os.path.isdir(session_dir):
        raise PlaylistBuildError(
            "The render session folder is unavailable, so no playlist soundtrack can be written.")

    estimated_total = sum(track.source_duration_seconds for track in ordered)
    _refuse_impossible_sizes(estimated_total, session_dir, repeat=repeat)

    started = time.perf_counter()
    stem = uuid.uuid4().hex
    master_path = master_path_for(session_dir)

    # [R2 / F1] **Every invocation-owned temporary path is registered BEFORE anything can create
    # it.** The pre-R2 code appended a decode target to its cleanup list only *after* the decode
    # had succeeded and been validated — but FFmpeg creates its output the moment it starts
    # writing, so a decoder that failed, timed out or was cancelled left a partial WAV that nothing
    # ever removed. Measured on all four paths. `_owned` makes forgetting structurally harder than
    # remembering: a path that is not registered is a path that was never constructed here.
    #
    # The master is tracked separately because it is the one artifact a SUCCESS keeps.
    scratch: list = []

    def _owned(path: str) -> str:
        scratch.append(path)
        return path

    list_path = _owned(os.path.join(session_dir, f"beatsync_playlist_list_{stem}.txt"))
    succeeded = False

    try:
        # ---- 1. decode every queued track to one canonical PCM representation ------------
        decoded: list = []
        measured_frames: list = []
        instruments: set = set()
        # The canonical rate this module asks FFmpeg for, replaced by the first real measurement so
        # the offsets are derived from the artifact rather than from what was requested.
        sample_rate = TRACK_SAMPLE_RATE
        for position, track in enumerate(ordered, start=1):
            path = track.path
            if on_track is not None:
                on_track(position, len(ordered), track.name)
            where = f"track {position} of {len(ordered)}"

            # [R2 / F2] The CONSUMPTION boundary. The preflight may have run minutes ago, before a
            # cold Stage 5; this is the instant the bytes are actually read, so the identity it
            # validated is re-checked here rather than trusted. A track replaced in that window
            # must not silently become part of the soundtrack.
            try:
                now = os.stat(path)
            except OSError as exc:
                raise PlaylistInputError(
                    f"Could not read {where} ({track.name}) when the soundtrack was built: "
                    f"{path}") from exc
            changed = fork_playlist.describe_identity_change(
                track.as_playlist_track(), now.st_size, now.st_mtime_ns)
            if changed:
                # The pure module's sentence verbatim -- capitalising its first character would
                # capitalise the FILENAME it starts with, turning `victim.wav` into `Victim.wav`
                # and inventing a file the operator does not have.
                raise PlaylistInputError(
                    f"{where.capitalize()}: {changed}. It changed while this render was being "
                    f"prepared, so nothing was rendered. Add the track again and retry.")

            target = _owned(os.path.join(
                session_dir, f"beatsync_playlist_{stem}_{position:03d}.wav"))
            decoded.append(target)
            try:
                result = _run(build_decode_command(path, target),
                              _DECODE_TIMEOUT_SECONDS, lifecycle=lifecycle)
            except subprocess.TimeoutExpired:
                raise PlaylistBuildError(f"Timed out decoding {where} ({track.name})")
            if result.returncode != 0:
                # Deliberately an INPUT error: a non-zero decode of a file the operator chose is
                # overwhelmingly a problem with that file (a truncated download, a DRM-wrapped
                # M4A, a container FFmpeg cannot demux), and naming the track is what lets them
                # fix it. The join below is where a generated-artifact failure is a build error.
                raise PlaylistInputError(
                    f"Could not decode {where} ({track.name}): "
                    f"{_tail(result.stderr) or 'FFmpeg failed'}")
            if not os.path.isfile(target) or os.path.getsize(target) <= 0:
                raise PlaylistInputError(
                    f"Decoding {where} ({track.name}) produced no audio to add to the playlist")

            geometry, instrument = measure_frames(target, lifecycle=lifecycle)
            if geometry is None:
                # [R2 / F2] Both instruments failed on an artifact THIS function just produced.
                # Pre-R2 this fell through to container durations and reported them as boundaries;
                # that is estimated geometry presented as verified, which is the one thing the
                # scope's "verify against the actual decoded tracks" rules out. Refuse instead.
                raise PlaylistBuildError(
                    f"Could not measure the decoded audio for {where} ({track.name}): neither the "
                    f"WAV header nor ffprobe reported a usable sample count, so the soundtrack "
                    f"could not be verified against it.")
            sample_rate = geometry.sample_rate or sample_rate
            measured_frames.append(geometry.frames)
            instruments.add(instrument)

        # ---- 2. join them, in order, into one continuous soundtrack ----------------------
        with open(list_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_join_list(decoded))

        try:
            joined = _run(build_join_command(list_path, master_path),
                          _JOIN_TIMEOUT_SECONDS, lifecycle=lifecycle)
        except subprocess.TimeoutExpired:
            raise PlaylistBuildError(
                f"Timed out joining the {len(ordered)} playlist tracks into one soundtrack")
        if joined.returncode != 0:
            raise PlaylistBuildError(
                f"Could not join the {len(ordered)} playlist tracks: "
                f"{_tail(joined.stderr) or 'FFmpeg failed'}")
        if not os.path.isfile(master_path):
            raise PlaylistBuildError("The playlist soundtrack produced no output file")
        if os.path.getsize(master_path) <= 0:
            raise PlaylistBuildError("The playlist soundtrack produced an empty output file")

        # ---- 3. verify the soundtrack against the tracks that actually went into it ------
        produced = probe_master_duration(master_path, lifecycle=lifecycle)
        master_geometry, master_instrument = measure_frames(master_path, lifecycle=lifecycle)
        if master_geometry is None:
            raise PlaylistBuildError(
                "Could not measure the joined playlist soundtrack: neither the WAV header nor "
                "ffprobe reported a usable sample count, so it could not be verified against the "
                "tracks that went into it.")
        instruments.add(master_instrument)
        total_frames = _verify_sample_counts(master_geometry, measured_frames, len(ordered))
        sample_rate = master_geometry.sample_rate or sample_rate
        total_seconds = total_frames / sample_rate
        drift = abs(produced - total_seconds)
        if drift > DURATION_TOLERANCE_SECONDS:
            # Load-bearing: the frame-locked cut timeline is derived from this file's duration, and
            # the analysis is run on this exact file, so a master whose two independent
            # measurements disagree is a master nothing downstream can be trusted against.
            raise PlaylistBuildError(
                f"The playlist soundtrack measures {total_seconds:.3f}s by sample count but "
                f"{produced:.3f}s by probe ({drift * 1000:.1f} ms apart)")

        prepared_tracks = _place_tracks(ordered, measured_frames, sample_rate)
        succeeded = True
    finally:
        # [R2 / F1] ONE cleanup, covering EVERY terminal path. There is deliberately no `except`
        # clause: a typed failure, a cancellation, a `KeyboardInterrupt` and an unexpected
        # `OSError` from the middle of the join all leave this function the same way, and all of
        # them must leave the session folder as they found it.
        #
        # Not catching anything is also what preserves the two contracts above it — the
        # `PlaylistInputError` / `PlaylistBuildError` split that `gui.py` classifies on, and
        # `RenderCancelled` propagating unchanged so a Stop is never reported as a broken file.
        #
        # The children are already reaped: `_run` kills and `communicate()`s its own `Popen` in its
        # own `finally`, so by the time control reaches here no FFmpeg still holds any of these
        # files open — which on Windows is the difference between a delete and a sharing violation.
        for path in scratch:
            discard_playlist_master(path)
        if not succeeded:
            discard_playlist_master(master_path)

    return PlaylistMaster(
        path=master_path,
        tracks=prepared_tracks,
        total_frames=int(total_frames),
        sample_rate=int(sample_rate),
        total_seconds=float(total_seconds),
        prepare_seconds=time.perf_counter() - started,
        measurement=(MEASURED_BY_PROBE if MEASURED_BY_PROBE in instruments
                     else MEASURED_BY_HEADER),
    )


def probe_master_duration(path: str, lifecycle=None) -> float:
    """The generated master's own duration. A failure here is a BUILD error, not an input one.

    Separate from :func:`probe_duration` purely for that classification: the file being probed is
    this render's own generated artifact, so an unreadable one says nothing about the operator's
    music and must not be reported as though it did.
    """
    try:
        return probe_duration(path, lifecycle=lifecycle)
    except PlaylistInputError as exc:
        raise PlaylistBuildError(str(exc)) from exc


def free_space(session_dir: str):
    """Free bytes on the scratch volume, or ``None`` when it cannot be measured.

    Separated out so the capacity policy can be given a number — real or injected — rather than
    reaching for a disk itself. That is what lets the regression suite drive every refusal without
    generating multi-gigabyte fixtures or filling a physical drive.
    """
    try:
        return shutil.disk_usage(session_dir).free
    except OSError:
        return None


def playlist_repeat_capacity(cycle_seconds, repeat, session_dir: str, *,
                             free_bytes=None, ceiling_bytes=None):
    """The **guaranteed** capacity verdict, from the MEASURED cycle of a master that now exists.

    [R2 / F3] Called after the playlist master has been built and before R10 is handed it, which
    is the only moment both facts are available: the cycle is a measurement rather than a sum of
    container durations, and the repeat count is known. The pre-admission check inside
    :func:`build_playlist_master` has already refused the obviously impossible from an *estimate*;
    this is the one that can be stated without a qualifier.

    `free_bytes` and `ceiling_bytes` are injectable so the suite can drive both refusals with no
    real disk and no real multi-gigabyte file. Passing nothing measures the volume for real.
    """
    measured_free = free_space(session_dir) if free_bytes is None else free_bytes
    return fork_playlist.capacity_verdict(
        cycle_seconds, repeat,
        free_bytes=measured_free,
        guaranteed=True,
        min_free_bytes=MIN_FREE_SCRATCH_BYTES,
        ceiling_bytes=(fork_playlist.MAX_MASTER_BYTES if ceiling_bytes is None
                       else ceiling_bytes))


def raise_for_capacity(verdict) -> None:
    """Turn a refusing :class:`music_playlist.CapacityVerdict` into the right exception type.

    The two refusals are classified differently and deliberately so, because `gui.py` maps the
    types onto different batch semantics: a **format** refusal is a fact about what was requested,
    frozen for the whole batch, so every candidate would hit it identically (`SHARED_FATAL`); a
    **space** refusal is a fact about one volume at one instant, which a later candidate might not
    hit (`UNKNOWN_FATAL`, fail-closed).
    """
    if verdict.ok:
        return
    if verdict.reason == fork_playlist.REFUSAL_FORMAT:
        raise PlaylistInputError(verdict.message)
    raise PlaylistBuildError(verdict.message)


def _refuse_impossible_sizes(total_seconds, session_dir: str, repeat=1) -> None:
    """Refuse up front where the soundtrack cannot be written, rather than part-way through.

    Everything here is an **estimate** — it runs before any decode, so the only durations available
    are the source containers', and a VBR MP3's is routinely a few milliseconds out. It exists to
    stop an obviously impossible job before it costs a full decode; the guaranteed statement is
    :func:`playlist_repeat_capacity`, after the master is measured.

    Three impossibilities, deliberately distinguished:

    * **the joined master's own format ceiling** — one cycle above
      :data:`MAX_MASTER_BYTES` cannot be one WAV;
    * **the REPEATED master's format ceiling and its overlapping peak** (R2 / F3) — `N × cycle`
      has its own ceiling, and at the worst moment the playlist master, R10's copy of it and the
      repeated master all exist at once. Pre-R2 none of this was accounted for: a 20-minute
      playlist at 30x reached R10, wrote 10.4 GB past a 3.9 GB ceiling, and failed only on R10's
      own duration check — after the write;
    * **this volume, right now** — the build's own `2 ×` peak plus headroom.
    """
    estimated_master = fork_playlist.master_bytes(total_seconds)
    if estimated_master > MAX_MASTER_BYTES:
        raise PlaylistInputError(
            f"This playlist is about {fork_playlist.format_clock(total_seconds)} long, which is "
            f"more than one WAV soundtrack file can hold "
            f"({fork_playlist.format_bytes(MAX_MASTER_BYTES)}). Remove some tracks.")

    measured_free = free_space(session_dir)
    raise_for_capacity(fork_playlist.capacity_verdict(
        total_seconds, repeat, free_bytes=measured_free, guaranteed=False,
        min_free_bytes=MIN_FREE_SCRATCH_BYTES))

    if measured_free is None:
        return                                      # unmeasured is not a refusal
    needed = estimate_scratch_bytes(total_seconds) + MIN_FREE_SCRATCH_BYTES
    if measured_free < needed:
        raise PlaylistBuildError(
            f"Not enough temporary space to build the playlist soundtrack: it needs about "
            f"{fork_playlist.format_bytes(needed)} and "
            f"{fork_playlist.format_bytes(measured_free)} is free. Free some space, or remove "
            f"tracks.")


def _verify_sample_counts(master_geometry, measured_frames, count: int) -> int:
    """The exact verification: the master's frame count **is** the sum of its inputs'.

    The primary check, and the reason the decode is per-track: not within a tolerance — a stream
    copy of consecutive PCM runs either reproduced every sample or it did not, and a single dropped
    or duplicated frame would put every later track at the wrong offset.

    [R2 / F2] There is no longer an unmeasured answer to return. Every caller has already resolved
    a real geometry through :func:`measure_frames` — the header first, ffprobe second — and a
    double failure is refused at the point of measurement rather than carried here as `None`. That
    removes the one path on which estimated geometry could reach the render report labelled as a
    verified boundary.
    """
    expected = sum(measured_frames)
    if master_geometry.frames != expected:
        raise PlaylistBuildError(
            f"The playlist soundtrack holds {master_geometry.frames} audio samples but the "
            f"{count} decoded tracks hold {expected} together "
            f"({abs(master_geometry.frames - expected)} apart)")
    return expected


def _place_tracks(ordered, measured_frames, sample_rate: int) -> tuple:
    """Where each track begins inside the finished soundtrack — a cumulative SAMPLE offset.

    Exact, and the only honest way to say "track 4 starts at 11:02".

    [R2 / F2] The estimated branch this function used to carry is **gone**, not merely unused. It
    scaled the source containers' durations so the last one landed on the master's probed length
    and handed the result to the render report as track boundaries; that is an estimate wearing a
    measurement's clothes, which is precisely what the scope's "verify against the actual decoded
    tracks" rules out. A geometry that cannot be measured is now refused at the point of
    measurement, so by the time control reaches here every frame count is real.
    """
    tracks: list = []
    cursor = 0
    for index, track in enumerate(ordered):
        frames = int(measured_frames[index])
        tracks.append(PreparedTrack(
            index=index, path=track.path, name=track.name,
            start_frame=cursor, frames=frames, sample_rate=int(sample_rate),
            source_duration_seconds=float(track.source_duration_seconds)))
        cursor += frames
    return tuple(tracks)


__all__ = [
    "DURATION_TOLERANCE_SECONDS",
    "MAX_MASTER_BYTES",
    "MEASURED_BY_HEADER",
    "MEASURED_BY_PROBE",
    "MIN_FREE_SCRATCH_BYTES",
    "PlaylistBuildError",
    "PlaylistError",
    "PlaylistInputError",
    "PlaylistMaster",
    "PreparedTrack",
    "TRACK_BITS",
    "TRACK_CHANNELS",
    "TRACK_CODEC",
    "TRACK_SAMPLE_RATE",
    "ValidatedTrack",
    "build_decode_command",
    "build_join_command",
    "build_join_list",
    "build_playlist_master",
    "discard_playlist_master",
    "estimate_scratch_bytes",
    "free_space",
    "master_path_for",
    "measure_frames",
    "playlist_repeat_capacity",
    "prepare_playlist_tracks",
    "probe_duration",
    "probe_frame_count",
    "probe_master_duration",
    "raise_for_capacity",
    "read_wav_geometry",
]
