#!/usr/bin/env python3
"""[FORK] Digital-Union (FUN R10 Repeat Music): the runtime half — producing the repeated master.

:mod:`beatsync_fork.music_repeat` decides *what* a repeat means (the control, the geometry, the
timeline extension). This module does the one thing that needs the portable FFmpeg: it turns the
operator's music file into a temporary audio master that contains that music ``N`` times, exactly,
and reports the measured length of **one** cycle so the pure module can re-stamp the analysis
against the artifact that will actually be rendered.

The dependency is one-way: this module imports the pure one, never the reverse.

===============================================================================
Two commands, and why it is not one
===============================================================================

::

    1. DECODE ONCE   ffmpeg -i <music> -vn -map 0:a:0 -c:a pcm_s24le -ar 48000 -ac 2 cycle.wav
    2. CONCATENATE   ffmpeg -f concat -safe 0 -i list.txt -c copy repeated.wav
    3. VERIFY        the repeated file's measured duration == N * the cycle's, within one frame

The obvious one-command form, ``-stream_loop N-1 -i <music> …``, was rejected: it loops by seeking
the *input* back to the start, so for a compressed source the decoder's delay/padding handling and
the loop's timestamp continuity both become part of the result, and neither is something this
feature can measure or promise. Decoding **once** to PCM and then concatenating that exact byte
run makes every repetition bit-identical to the first by construction, leaves no gap at a
repetition boundary (there is nothing between two PCM runs), and makes the total sample count an
exact multiple — which is what "deterministic repeated duration" has to mean.

Step 3 is not ceremony. The whole frame-locked cut timeline is derived from the rendered audio
file's duration, and the pure module stamps 30 cycles at ``k * cycle_seconds``; if the master were
not exactly ``N`` cycles long, every later cut would drift against the music. So the arithmetic is
checked against the artifact rather than assumed, and a mismatch **fails the render** instead of
producing a plausible-looking video that slides out of sync.

``pcm_s24le`` / 48 kHz / stereo is the same format :mod:`audio_mixdown` writes its mixed master in
and the same one final assembly lays down, so neither stage has any resampling or requantisation to
do and nothing lossy is introduced between the operator's file and the finished video.

===============================================================================
Where the file lives, and who deletes it
===============================================================================

The master goes in ``session_dir`` — **never** ``get_processing_dir()``, which
``create_music_video`` clears at the start of every render and which would therefore delete this
file moments after it was written, long before assembly reads it. Same rule, same reason, as
``audio_mixdown.master_path_for``.

It is invocation-owned scratch: a fresh uuid path per render, and :func:`discard_repeated_master`
removes it on every terminal path — success, failure and cancellation alike. The operator's own
music file is opened read-only and is never moved, rewritten or deleted, and nothing is ever
written into their Media Library.

===============================================================================
Failure is loud
===============================================================================

If the operator asked for 5x and this cannot deliver 5x, the render stops. Falling back to one
repetition would produce a finished three-minute video for someone who asked for fifteen minutes —
a plausible-looking result that silently ignored the request, which is exactly the failure mode
:mod:`audio_mixdown` refuses for voice.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import dataclass

from ffmpeg_processing import FFMPEG_PATH, FFPROBE_PATH

from beatsync_fork import music_repeat as fork_music_repeat
# [FORK] Digital-Union (C3-R1A/FUN Stage-5 Cancel): the pure cancellation contract. This module
# keeps its own small cancellable runner below rather than importing `ffmpeg_processing` or
# `audio_mixdown`'s -- the process handle always belongs to the call frame that creates it, which is
# the same reason those two runtime modules do not share one either.
from beatsync_fork.render_worker import RenderCancelled

#: The temporary master's format. Identical to what `audio_mixdown` and final assembly already
#: encode, so no stage downstream has resampling or requantisation work to do.
CYCLE_SAMPLE_RATE = 48_000
CYCLE_CHANNELS = 2
CYCLE_CODEC = "pcm_s24le"

#: How far the produced master may differ from ``repeat * cycle``. One PCM sample at 48 kHz is
#: 0.021 ms; 1 ms is generous against ffprobe's reporting precision and still far below one video
#: frame, which is what matters because `create_music_video` derives the frame-locked timeline from
#: this file's duration.
DURATION_TOLERANCE_SECONDS = 0.001

#: Decoding and concatenating is linear in the music's length and in the repeat count, and at 30x
#: it writes well over a gigabyte, so this is deliberately generous rather than tight. A real
#: timeout is still a stuck FFmpeg and is reported as one; it is never confused with a Stop.
_DECODE_TIMEOUT_SECONDS = 900
_CONCAT_TIMEOUT_SECONDS = 1800
_PROBE_TIMEOUT_SECONDS = 60

#: Same shape as the two existing runners' poll/grace pair, deliberately its own values.
_CANCEL_POLL_SECONDS = 0.15
_TERMINATE_GRACE_SECONDS = 5.0

#: Bounded so a loudly-failing FFmpeg cannot paste megabytes into a status panel.
_STDERR_TAIL = 1200


class MusicRepeatError(Exception):
    """Producing or verifying the repeated master failed. Carries a short, bounded reason.

    Deliberately **not** an ``audio_mixdown.AudioMixError`` subclass. The two features fail for
    unrelated reasons and `gui.py` classifies them differently: a repeat failure is about this
    render's own generated artifact and the operator's repeat count, not about their voice clips or
    their SFX library, and the three ``except audio_mixdown.AudioMixError`` handlers in the render
    body must keep meaning exactly what they mean today.
    """


@dataclass(frozen=True)
class RepeatedMusic:
    """What one successful preparation produced. Immutable, and carries no handle of any kind.

    ``cycle_seconds`` is the **measured** duration of one repetition of the master on disk, which
    is the value the pure module must stamp its offsets with — not the analysis window's own
    ``len(y) / sr``, and not a number derived from the source file's container metadata.
    """

    path: str
    repeat: int
    cycle_seconds: float
    total_seconds: float
    prepare_seconds: float

    def describe(self) -> str:
        return fork_music_repeat.describe(self.repeat, self.cycle_seconds)


def _tail(text) -> str:
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    return collapsed[-_STDERR_TAIL:]


def _run(command, timeout, lifecycle=None):
    """Run one FFmpeg/FFprobe command. ``lifecycle`` defaults to ``None`` (blocking, unchanged).

    Mirrors ``audio_mixdown._run``'s cancellable shape exactly, as its own independent
    implementation, so the child this frame creates is reaped by this frame. A real timeout still
    raises ``subprocess.TimeoutExpired``; only a matching cancellation raises ``RenderCancelled``,
    and only after the child is provably reaped.
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
                    raise RenderCancelled("repeated music preparation cancelled")
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


def repeated_master_path(session_dir: str) -> str:
    """A fresh master path per render, so a stale repeat can never be picked up by a later one."""
    return os.path.join(session_dir, f"beatsync_repeat_{uuid.uuid4().hex}.wav")


def build_decode_command(music_path: str, output_path: str) -> list:
    """The step-1 argv: decode the operator's music to one canonical PCM cycle.

    ``-vn`` and ``-map 0:a:0`` together are what make this a decode of *the music* rather than of
    whatever else the container holds: an MP3 with embedded cover art presents a video stream, and
    an MKV may carry several audio tracks. The first audio stream is the one BeatSync analysed, so
    it is the one that must be repeated.

    ``-nostdin`` because this runs inside a GUI worker thread with no console to read from, and
    ``-y`` because the destination is a uuid path this process just minted.
    """
    return [
        FFMPEG_PATH, "-nostdin", "-y",
        "-i", music_path,
        "-vn", "-map", "0:a:0",
        "-c:a", CYCLE_CODEC,
        "-ar", str(CYCLE_SAMPLE_RATE),
        "-ac", str(CYCLE_CHANNELS),
        output_path,
    ]


def build_concat_command(list_path: str, output_path: str) -> list:
    """The step-2 argv: stream-copy the one decoded cycle ``N`` times into the master.

    ``-c copy`` is the whole point — the repetitions are byte-identical runs of the same PCM, so
    there is no re-encode, no requantisation and nothing between two cycles for a gap to live in.
    ``-safe 0`` is required because the list holds an absolute path.
    """
    return [
        FFMPEG_PATH, "-nostdin", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        output_path,
    ]


def build_concat_list(cycle_path: str, repeat: int) -> str:
    """The concat demuxer's list file contents: the same decoded cycle, ``repeat`` times.

    Separators are normalised to ``/`` — the demuxer accepts them on Windows and they need no
    escaping — and a literal single quote is escaped the way the demuxer specifies, so a path under
    a user profile containing an apostrophe cannot break the file or inject a directive.
    """
    quoted = str(cycle_path).replace("\\", "/").replace("'", "'\\''")
    return "".join(f"file '{quoted}'\n" for _ in range(max(1, int(repeat))))


def probe_duration(path: str, lifecycle=None) -> float:
    """Media duration in seconds, via the project's portable ffprobe.

    Raises rather than guessing: ``ffmpeg_processing.get_video_duration`` answers ``10.0`` when a
    probe fails, which is right for its caller and catastrophic here — a guessed cycle length would
    stamp every repetition at the wrong offset and desynchronise the whole video.
    """
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    command = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", path]
    try:
        result = _run(command, _PROBE_TIMEOUT_SECONDS, lifecycle=lifecycle)
    except subprocess.TimeoutExpired:
        raise MusicRepeatError(f"Timed out reading the duration of {os.path.basename(path)}")
    if result.returncode != 0:
        raise MusicRepeatError(
            f"Could not read {os.path.basename(path)}: {_tail(result.stderr) or 'ffprobe failed'}")
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise MusicRepeatError(f"Could not read a duration from {os.path.basename(path)}")
    if duration != duration or duration in (float("inf"), float("-inf")) or duration <= 0.0:
        raise MusicRepeatError(
            f"{os.path.basename(path)} reports an unusable duration ({result.stdout.strip()})")
    return duration


def discard_repeated_master(path) -> None:
    """Best-effort removal of a temporary master. Never raises: cleanup must not mask a result."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def build_repeated_music(music_path: str, repeat, session_dir: str,
                         lifecycle=None) -> RepeatedMusic:
    """Produce the repeated audio master. Raises :class:`MusicRepeatError` on any failure.

    Callers must already have decided that a repeat is wanted — ``repeat`` is normalised here
    anyway, but a value of 1 is a programming error rather than a no-op, because the 1x render path
    must never reach this module at all. It raises instead of quietly returning the original file,
    so a mis-wired caller is loud rather than silently non-repeating.

    The two intermediates this function creates (the decoded cycle and the concat list) are its own
    and are removed before it returns, on every path. The master it returns is the caller's to
    discard; :func:`discard_repeated_master` is the one way to do that.

    ``RenderCancelled`` propagates **unchanged** — never re-typed into a
    :class:`MusicRepeatError`. Re-typing it would report a user's Stop as a repeat failure, and in a
    Variant Lab batch would describe a cancelled candidate as a problem with their repeat count.
    Partial output is discarded on that path too, so a cancelled preparation leaves no truncated WAV
    behind for anything to read.
    """
    count = fork_music_repeat.normalize_repeat(repeat)
    if count <= fork_music_repeat.MIN_REPEAT:
        raise MusicRepeatError(
            "Repeat Music is 1x; the original music file is the render audio and no repeated "
            "master may be produced.")
    if lifecycle is not None:
        lifecycle.raise_if_cancelled()
    if not music_path or not os.path.isfile(music_path):
        raise MusicRepeatError(
            f"Could not read the music file to repeat: {music_path or '(none)'}")
    if not session_dir or not os.path.isdir(session_dir):
        raise MusicRepeatError(
            "The render session folder is unavailable, so no repeated master can be written.")

    started = time.perf_counter()
    stem = uuid.uuid4().hex
    cycle_path = os.path.join(session_dir, f"beatsync_repeat_cycle_{stem}.wav")
    list_path = os.path.join(session_dir, f"beatsync_repeat_list_{stem}.txt")
    master_path = repeated_master_path(session_dir)

    try:
        # ---- 1. decode the music to one canonical PCM cycle -------------------------------
        try:
            decoded = _run(build_decode_command(music_path, cycle_path),
                           _DECODE_TIMEOUT_SECONDS, lifecycle=lifecycle)
        except subprocess.TimeoutExpired:
            raise MusicRepeatError("Timed out decoding the music for Repeat Music")
        if decoded.returncode != 0:
            raise MusicRepeatError(
                f"Could not decode the music for Repeat Music: "
                f"{_tail(decoded.stderr) or 'FFmpeg failed'}")
        if not os.path.isfile(cycle_path) or os.path.getsize(cycle_path) <= 0:
            raise MusicRepeatError("Repeat Music produced no decoded audio to repeat")

        cycle_seconds = probe_duration(cycle_path, lifecycle=lifecycle)

        # ---- 2. concatenate it `count` times --------------------------------------------
        with open(list_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(build_concat_list(cycle_path, count))

        try:
            joined = _run(build_concat_command(list_path, master_path),
                          _CONCAT_TIMEOUT_SECONDS, lifecycle=lifecycle)
        except subprocess.TimeoutExpired:
            raise MusicRepeatError(f"Timed out repeating the music {count}x")
        if joined.returncode != 0:
            raise MusicRepeatError(
                f"Could not repeat the music {count}x: {_tail(joined.stderr) or 'FFmpeg failed'}")
        if not os.path.isfile(master_path):
            raise MusicRepeatError("Repeat Music produced no output file")
        if os.path.getsize(master_path) <= 0:
            raise MusicRepeatError("Repeat Music produced an empty output file")

        # ---- 3. verify the arithmetic against the artifact ------------------------------
        produced = probe_duration(master_path, lifecycle=lifecycle)
        expected = fork_music_repeat.total_seconds(count, cycle_seconds)
        drift = abs(produced - expected)
        if drift > DURATION_TOLERANCE_SECONDS:
            # Load-bearing: the frame-locked cut timeline comes from this file's duration and the
            # analysis is stamped at multiples of `cycle_seconds`, so a master that is not exactly
            # `count` cycles long would shift every cut after the first repetition.
            raise MusicRepeatError(
                f"Repeated audio is {produced:.3f}s but {count}x "
                f"{cycle_seconds:.3f}s is {expected:.3f}s ({drift * 1000:.1f} ms drift)")
    except (MusicRepeatError, RenderCancelled):
        # Both paths discard the partial master: a truncated WAV left in `session_dir` would be
        # read by nothing today, and must not become readable by anything tomorrow. The exception
        # itself is re-raised unchanged -- a cancellation stays a cancellation.
        discard_repeated_master(master_path)
        raise
    finally:
        discard_repeated_master(cycle_path)
        discard_repeated_master(list_path)

    return RepeatedMusic(
        path=master_path,
        repeat=count,
        cycle_seconds=cycle_seconds,
        total_seconds=expected,
        prepare_seconds=time.perf_counter() - started,
    )


__all__ = [
    "CYCLE_CHANNELS",
    "CYCLE_CODEC",
    "CYCLE_SAMPLE_RATE",
    "DURATION_TOLERANCE_SECONDS",
    "MusicRepeatError",
    "RepeatedMusic",
    "build_concat_command",
    "build_concat_list",
    "build_decode_command",
    "build_repeated_music",
    "discard_repeated_master",
    "probe_duration",
    "repeated_master_path",
]
