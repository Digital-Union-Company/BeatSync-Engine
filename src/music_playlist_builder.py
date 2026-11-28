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

#: A WAV's `data` chunk size is a 32-bit field, so a single master cannot exceed 4 GiB. At the
#: canonical 288 kB/s that is about 4.1 hours of soundtrack. Refusing above a safe bound is the
#: point: FFmpeg would write a file whose header wrapped, the verification would read a wrong frame
#: count, and the operator would get an hours-long render that desynchronises. (The same ceiling
#: applies to a 30x Repeat Playlist, which is why the read-out states the figure.)
MAX_MASTER_BYTES = 3_900_000_000


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
    measured: bool = True

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
            "measured": bool(self.measured),
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
    bytes to the pure parser. ``None`` means **unmeasured**, which is a genuine third answer and is
    never read as zero frames — the same rule ``frame_lock.FrameCountVerdict`` records, for the same
    reason: an unmeasured count must not become a refusal.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(fork_playlist.WAV_HEADER_PROBE_BYTES)
    except OSError:
        return None
    return fork_playlist.parse_wav_header(header)


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

    Returns ``(path, source_duration_seconds)`` pairs in playlist order. Durations are the source
    containers'; the authority on the soundtrack geometry is the measured decode.
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
        prepared.append((path, probe_duration(path, lifecycle=lifecycle)))
    return tuple(prepared)


# ---------------------------------------------------------------------------
# the soundtrack master
# ---------------------------------------------------------------------------


def build_playlist_master(tracks, session_dir: str, lifecycle=None,
                          on_track=None) -> PlaylistMaster:
    """Produce the one continuous soundtrack master. Raises :class:`PlaylistError` on any failure.

    ``tracks`` is what :func:`prepare_playlist_tracks` returned — an already-validated, already-
    ordered sequence of ``(path, source_duration_seconds)`` pairs. Validating again here would be a
    second interpretation of what a usable track is; the split is deliberate so that the cheap check
    can run early in the render body and the expensive work can run late.

    ``on_track`` is an optional ``(position, total, name)`` callback for progress. It is a plain
    callable rather than a progress object because this module must know nothing about
    ``ProgressEvent`` or Gradio: the GUI decides what a decode means on screen.

    Every intermediate this function creates is its own and is removed before it returns, on every
    path. The master it returns is the caller's to discard; :func:`discard_playlist_master` is the
    one way to do that.

    ``RenderCancelled`` propagates **unchanged** — never re-typed into a :class:`PlaylistError`.
    Partial output is discarded on that path too, so a cancelled preparation leaves no truncated
    WAV behind for anything to read.
    """
    ordered = [(str(path), duration) for path, duration in tracks]
    if not ordered:
        raise PlaylistInputError(fork_playlist.EMPTY_PLAYLIST_MESSAGE)
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    if not session_dir or not os.path.isdir(session_dir):
        raise PlaylistBuildError(
            "The render session folder is unavailable, so no playlist soundtrack can be written.")

    estimated_total = sum(duration for _path, duration in ordered)
    _refuse_impossible_sizes(estimated_total, session_dir)

    started = time.perf_counter()
    stem = uuid.uuid4().hex
    list_path = os.path.join(session_dir, f"beatsync_playlist_list_{stem}.txt")
    master_path = master_path_for(session_dir)
    decoded: list = []

    try:
        # ---- 1. decode every queued track to one canonical PCM representation ------------
        measured_frames: list = []
        # The canonical rate this module asks FFmpeg for, replaced by the first real measurement so
        # the offsets are derived from the artifact rather than from what was requested.
        sample_rate = TRACK_SAMPLE_RATE
        unmeasured = False
        for position, (path, source_duration) in enumerate(ordered, start=1):
            if on_track is not None:
                on_track(position, len(ordered), os.path.basename(path))
            target = os.path.join(session_dir,
                                  f"beatsync_playlist_{stem}_{position:03d}.wav")
            try:
                result = _run(build_decode_command(path, target),
                              _DECODE_TIMEOUT_SECONDS, lifecycle=lifecycle)
            except subprocess.TimeoutExpired:
                raise PlaylistBuildError(
                    f"Timed out decoding track {position} of {len(ordered)} "
                    f"({os.path.basename(path)})")
            if result.returncode != 0:
                # Deliberately an INPUT error: a non-zero decode of a file the operator chose is
                # overwhelmingly a problem with that file (a truncated download, a DRM-wrapped
                # M4A, a container FFmpeg cannot demux), and naming the track is what lets them
                # fix it. The join below is where a generated-artifact failure is a build error.
                raise PlaylistInputError(
                    f"Could not decode track {position} of {len(ordered)} "
                    f"({os.path.basename(path)}): {_tail(result.stderr) or 'FFmpeg failed'}")
            if not os.path.isfile(target) or os.path.getsize(target) <= 0:
                raise PlaylistInputError(
                    f"Track {position} of {len(ordered)} ({os.path.basename(path)}) produced no "
                    f"audio to add to the playlist")
            decoded.append(target)

            geometry = read_wav_geometry(target)
            if geometry is None:
                unmeasured = True
                measured_frames.append(None)
            else:
                sample_rate = geometry.sample_rate or sample_rate
                measured_frames.append(geometry.frames)

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
        master_geometry = read_wav_geometry(master_path)
        total_frames, measured = _verify_sample_counts(
            master_geometry, measured_frames, unmeasured, len(ordered))
        if total_frames is not None and master_geometry is not None:
            total_seconds = total_frames / (master_geometry.sample_rate or sample_rate)
            sample_rate = master_geometry.sample_rate or sample_rate
        else:
            total_seconds = produced
            total_frames = int(round(produced * sample_rate))
        drift = abs(produced - total_seconds)
        if drift > DURATION_TOLERANCE_SECONDS:
            # Load-bearing: the frame-locked cut timeline is derived from this file's duration, and
            # the analysis is run on this exact file, so a master whose two independent
            # measurements disagree is a master nothing downstream can be trusted against.
            raise PlaylistBuildError(
                f"The playlist soundtrack measures {total_seconds:.3f}s by sample count but "
                f"{produced:.3f}s by probe ({drift * 1000:.1f} ms apart)")

        prepared_tracks = _place_tracks(ordered, measured_frames, sample_rate, produced, measured)
    except (PlaylistError, RenderCancelled):
        # Both paths discard the partial master: a truncated WAV left in `session_dir` would be
        # read by nothing today, and must not become readable by anything tomorrow. The exception
        # itself is re-raised unchanged -- a cancellation stays a cancellation.
        discard_playlist_master(master_path)
        raise
    finally:
        discard_playlist_master(list_path)
        for path in decoded:
            discard_playlist_master(path)

    return PlaylistMaster(
        path=master_path,
        tracks=prepared_tracks,
        total_frames=int(total_frames),
        sample_rate=int(sample_rate),
        total_seconds=float(total_seconds),
        prepare_seconds=time.perf_counter() - started,
        measured=measured,
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


def _refuse_impossible_sizes(total_seconds, session_dir: str) -> None:
    """Refuse up front where the soundtrack cannot be written, rather than part-way through.

    Two different impossibilities, deliberately distinguished:

    * **the format's own ceiling** — a WAV `data` chunk size is 32 bits, so a soundtrack above
      :data:`MAX_MASTER_BYTES` would be written with a wrapped header and would desynchronise;
    * **this volume, right now** — the estimated peak plus headroom against the actual free space.

    The second is a statement about one instant on a volume any other process may fill a moment
    later. It can refuse an obviously impossible job; it cannot promise a possible one, and the
    loud failures above are what cover the rest.
    """
    estimated_master = fork_playlist.master_bytes(total_seconds)
    if estimated_master > MAX_MASTER_BYTES:
        raise PlaylistInputError(
            f"This playlist is about {fork_playlist.format_clock(total_seconds)} long, which is "
            f"more than one WAV soundtrack file can hold "
            f"({fork_playlist.format_bytes(MAX_MASTER_BYTES)}). Remove some tracks.")
    needed = estimate_scratch_bytes(total_seconds) + MIN_FREE_SCRATCH_BYTES
    try:
        free = shutil.disk_usage(session_dir).free
    except OSError:
        return                                      # unmeasured is not a refusal
    if free < needed:
        raise PlaylistBuildError(
            f"Not enough temporary space to build the playlist soundtrack: it needs about "
            f"{fork_playlist.format_bytes(needed)} and "
            f"{fork_playlist.format_bytes(free)} is free. Free some space, or remove tracks.")


def _verify_sample_counts(master_geometry, measured_frames, unmeasured: bool, count: int):
    """``(total_frames, measured)`` — the exact verification, with an honest unmeasured answer.

    The primary check, and the reason the decode is per-track: the master's own frame count must
    equal the **sum** of the decoded tracks' frame counts, exactly. Not within a tolerance — a
    stream copy of consecutive PCM runs either reproduced every sample or it did not, and a single
    dropped or duplicated frame would put every later track at the wrong offset.

    An **unmeasured** count is never a refusal. If any track's header or the master's could not be
    parsed, this returns ``(None, False)`` and the caller falls back to the probed duration, with
    ``measured=False`` recorded on the result and in the report. Refusing a render because a header
    was shaped unusually would be a worse failure than reporting that the strongest check could not
    run.
    """
    if unmeasured or master_geometry is None or any(f is None for f in measured_frames):
        return None, False
    expected = sum(measured_frames)
    if master_geometry.frames != expected:
        raise PlaylistBuildError(
            f"The playlist soundtrack holds {master_geometry.frames} audio samples but the "
            f"{count} decoded tracks hold {expected} together "
            f"({abs(master_geometry.frames - expected)} apart)")
    return expected, True


def _place_tracks(ordered, measured_frames, sample_rate: int, produced: float,
                  measured: bool) -> tuple:
    """Where each track begins inside the finished soundtrack.

    From the **measured** frame counts when every track was measurable — a cumulative sample offset
    is exact, and is the only honest way to say "track 4 starts at 11:02". When measurement was not
    possible the offsets fall back to the source containers' own durations, scaled so the last one
    still lands on the master's probed length, and ``measured`` on the result records which of the
    two the report is showing.
    """
    if measured:
        tracks: list = []
        cursor = 0
        for index, (path, source_duration) in enumerate(ordered):
            frames = int(measured_frames[index])
            tracks.append(PreparedTrack(
                index=index, path=path,
                name=os.path.basename(path.replace("\\", "/")) or path,
                start_frame=cursor, frames=frames, sample_rate=int(sample_rate),
                source_duration_seconds=float(source_duration)))
            cursor += frames
        return tuple(tracks)

    estimated = sum(duration for _path, duration in ordered) or produced or 1.0
    scale = (produced / estimated) if estimated else 1.0
    tracks = []
    cursor = 0.0
    for index, (path, source_duration) in enumerate(ordered):
        scaled = float(source_duration) * scale
        tracks.append(PreparedTrack(
            index=index, path=path,
            name=os.path.basename(path.replace("\\", "/")) or path,
            start_frame=int(round(cursor * sample_rate)),
            frames=int(round(scaled * sample_rate)),
            sample_rate=int(sample_rate),
            source_duration_seconds=float(source_duration)))
        cursor += scaled
    return tuple(tracks)


__all__ = [
    "DURATION_TOLERANCE_SECONDS",
    "MAX_MASTER_BYTES",
    "MIN_FREE_SCRATCH_BYTES",
    "PlaylistBuildError",
    "PlaylistError",
    "PlaylistInputError",
    "PlaylistMaster",
    "PreparedTrack",
    "TRACK_CHANNELS",
    "TRACK_CODEC",
    "TRACK_SAMPLE_RATE",
    "build_decode_command",
    "build_join_command",
    "build_join_list",
    "build_playlist_master",
    "discard_playlist_master",
    "estimate_scratch_bytes",
    "master_path_for",
    "prepare_playlist_tracks",
    "probe_duration",
    "probe_master_duration",
    "read_wav_geometry",
]
