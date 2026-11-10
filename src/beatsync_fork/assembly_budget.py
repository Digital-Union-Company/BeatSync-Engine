"""Final-assembly timeout budget, derived from measured workload instead of a constant.

[FORK] Digital-Union (FUN R11 Assembly Budget).

**The incident this exists to prevent.** Final assembly ran under a hardcoded
``timeout=300``. A 1h59m render (2,187 segments, 14,865,314,550 bytes of clips, a
7157.666667 s frame-locked timeline) was killed by ``subprocess.TimeoutExpired`` at
exactly 300 s having already written **16,922,008,592 of ~16,926,722,550** expected
bytes -- the mdat box was complete and its size patched, and only the ``moov`` index was
missing. The render was ~99.97 % done and the whole two hours of extraction were
discarded. 300 s was never a property of the workload; it was a constant that happened
to fit short renders.

**Why a constant cannot work here.** Assembly cost is dominated by bytes moved, and the
bytes are set by the operator's music length and source resolution -- quantities this
module can *measure* before the child starts. So the bound scales with them, and the
module stays stdlib-only and pure so the arithmetic is testable on a bare interpreter
(CLAUDE.md's hard rule for ``src/beatsync_fork/``).

**What is deliberately NOT done.** The timeout is never removed and never unbounded: a
genuinely stalled FFmpeg must still die, be reaped and be reported. ``MAX`` is a finite
ceiling, ``MIN`` preserves today's prompt failure for short renders exactly, and nothing
here decides whether an output is *valid* -- a bound is not an acceptance test.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional

#: Today's constant, preserved as the FLOOR. Any render whose measured workload implies a
#: smaller budget keeps exactly the historical 300 s prompt-failure behaviour, so short
#: renders are unchanged by this module.
MIN_ASSEMBLY_TIMEOUT_SECONDS = 300

#: A finite ceiling. Long renders must be allowed to finish; they must not be allowed to
#: hang forever. Six hours comfortably exceeds the measured worst case below while still
#: guaranteeing the process is terminated and reaped.
MAX_ASSEMBLY_TIMEOUT_SECONDS = 21_600

#: Process start, container finalisation, ``moov`` construction and index writing -- real
#: work that does not scale with payload size.
ASSEMBLY_OVERHEAD_SECONDS = 120

#: Pessimistic sustained throughput floor. Measured on the incident volume the mux moved
#: ~31.8 GB of combined read+write in 300 s (~106 MB/s aggregate) on a SATA SSD. 16 MiB/s
#: is therefore roughly a 6x safety margin, chosen low enough that a healthy-but-contended
#: mechanical drive never trips it and high enough that a stalled child still dies.
ASSEMBLY_THROUGHPUT_FLOOR_BYTES_PER_SECOND = 16 * 1024 * 1024

#: A mux reads its inputs once and writes its output once.
BASE_IO_PASSES = 2

#: ``-movflags +faststart`` re-reads the finished file and rewrites it to move ``moov``
#: ahead of ``mdat``. That is a whole extra read and write of the OUTPUT, and it is the
#: pass the incident died in -- so it is budgeted explicitly rather than absorbed.
FASTSTART_IO_PASSES = 2

#: ``-c:a pcm_s24le -ar 48000`` with two channels: 48000 * 3 bytes * 2 ch.
PCM_S24LE_48K_STEREO_BYTES_PER_SECOND = 48_000 * 3 * 2

#: Wall-clock seconds permitted per second of output when the fallback path re-encodes
#: every frame. Encoding is compute-bound rather than I/O-bound, so it gets its own term;
#: 4.0 means a 1080p timeline may take four times its own length to encode.
REENCODE_SECONDS_PER_OUTPUT_SECOND = 4.0


def pcm_audio_bytes(seconds: Optional[float]) -> int:
    """Bytes the muxed ``pcm_s24le`` audio stream will occupy for ``seconds``.

    Returns 0 for ``None``, a non-number or a non-positive value -- an absent or unusable
    duration contributes nothing rather than raising, because a budget helper must never
    be the reason a render fails to start.
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return 0
    if value != value or value in (float('inf'), float('-inf')) or value <= 0:
        return 0
    return int(value * PCM_S24LE_48K_STEREO_BYTES_PER_SECOND)


def measure_input_bytes(paths: Optional[Iterable[str]]) -> int:
    """Total size of the segment files that will be read.

    This is the *measured* workload authority, not an estimate from frame counts. A path
    that cannot be stat-ed is skipped: the budget is allowed to be an underestimate of a
    broken input set, because the extraction guards -- not this function -- are what
    refuse a missing segment.
    """
    if not paths:
        return 0
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path)
        except (OSError, TypeError, ValueError):
            continue
    return total


def assembly_timeout_seconds(input_bytes: int = 0, *,
                             audio_seconds: Optional[float] = None,
                             faststart: bool = False,
                             reencode_seconds: Optional[float] = None) -> int:
    """Bounded timeout for one final-assembly child process.

    ``input_bytes``      measured bytes of the segments being read (0 for an audio-only step)
    ``audio_seconds``    timeline length, used for the muxed PCM stream's size
    ``faststart``        whether ``+faststart`` will re-read and rewrite the output
    ``reencode_seconds`` output length when every frame is re-encoded, else ``None``

    Always within ``[MIN, MAX]``. Never ``None``, never 0, never unbounded.
    """
    try:
        payload = max(0, int(input_bytes))
    except (TypeError, ValueError):
        payload = 0
    audio_bytes = pcm_audio_bytes(audio_seconds)
    output_bytes = payload + audio_bytes

    # BASE_IO_PASSES expressed concretely: the mux reads the segments once (payload) and
    # writes the muxed result once (output_bytes). faststart then re-reads and rewrites
    # that output, which is FASTSTART_IO_PASSES more traversals of output_bytes.
    io_bytes = payload + output_bytes
    if faststart:
        io_bytes += FASTSTART_IO_PASSES * output_bytes
    io_seconds = io_bytes / float(ASSEMBLY_THROUGHPUT_FLOOR_BYTES_PER_SECOND)

    encode_seconds = 0.0
    if reencode_seconds is not None:
        try:
            value = float(reencode_seconds)
            if value == value and value > 0:
                encode_seconds = value * REENCODE_SECONDS_PER_OUTPUT_SECOND
        except (TypeError, ValueError):
            encode_seconds = 0.0

    budget = ASSEMBLY_OVERHEAD_SECONDS + io_seconds + encode_seconds
    return max(MIN_ASSEMBLY_TIMEOUT_SECONDS,
               min(MAX_ASSEMBLY_TIMEOUT_SECONDS, int(budget) + 1))


def describe_assembly_timeout(timeout_seconds: int, *, input_bytes: int = 0,
                              audio_seconds: Optional[float] = None,
                              faststart: bool = False,
                              reencode_seconds: Optional[float] = None) -> str:
    """One line naming the workload a budget was derived from.

    A timeout that fires must say what it was measuring, or the next operator relearns
    the incident from scratch. Reported at the point of failure, not only on success.
    """
    parts = [f"{timeout_seconds}s budget"]
    if input_bytes:
        parts.append(f"{input_bytes / 1024 ** 3:.2f} GiB of segments")
    audio_bytes = pcm_audio_bytes(audio_seconds)
    if audio_bytes:
        parts.append(f"{audio_bytes / 1024 ** 3:.2f} GiB pcm_s24le"
                     f" ({float(audio_seconds):.1f}s)")
    if faststart:
        parts.append("+faststart rewrite pass")
    if reencode_seconds:
        parts.append(f"full re-encode of {float(reencode_seconds):.1f}s")
    floor = ASSEMBLY_THROUGHPUT_FLOOR_BYTES_PER_SECOND / 1024 ** 2
    parts.append(f"throughput floor {floor:.0f} MiB/s")
    if timeout_seconds >= MAX_ASSEMBLY_TIMEOUT_SECONDS:
        parts.append("CLAMPED AT CEILING")
    elif timeout_seconds <= MIN_ASSEMBLY_TIMEOUT_SECONDS:
        parts.append("at minimum floor")
    return "; ".join(parts)
