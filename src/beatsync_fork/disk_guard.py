"""Raw Cutter R4-D: what a full output volume means, as pure policy.

[FORK] Digital-Union. The owner's incident of 2026-11-16 is the whole reason this module exists: a
15-source Folder Batch wrote 580 of 1,137 planned clips (8.2 GB), then FFmpeg returned
``-28 (No space left on device)`` over and over, nine sources were reported as failures as though
their footage were broken, and finally ``batch_manifest.json`` itself could not be saved with
``Errno 28``. Nothing in the trace supports a finding of corrupt source video. The tool had no way
to say *the drive is full*, so it said *this video failed* fifteen hundred times.

**It decides; it never acts.** Per CLAUDE.md's hard rule it imports nothing from the upstream
runtime, and beyond that it opens no file, starts no process, reads no clock and — importantly for a
module about free space — **calls no ``shutil.disk_usage``**. Measuring a volume is a filesystem
operation and lives in :mod:`raw_cutter_export`; what a measurement *means* lives here, which is what
keeps every branch of it testable on a bare interpreter with no disk to fill.

What it deliberately does NOT do
--------------------------------
It does not delete, relocate, truncate or "reclaim" anything. There is no automatic recovery in
R4-D: a full disk is reported, the job stops scheduling, and the clips that reached disk stay
exactly where they are. It also does not retry — a volume that is full does not empty itself while a
batch waits, and an unbounded retry per clip is precisely the behaviour the incident consisted of.

And it never claims a future write will succeed. A capacity verdict is a statement about one
instant, on a volume any other process may fill a moment later. :func:`evaluate` can refuse an
obviously unwritable job; it cannot promise a writable one, and :data:`HEADROOM_QUALIFIER` says so
in the operator's own language rather than leaving the panel to imply otherwise.
"""

from __future__ import annotations

import errno
import math
from dataclasses import dataclass
from typing import Any

from beatsync_fork import raw_cutter as cutter

# ---------------------------------------------------------------------------------------------
# The one floor
# ---------------------------------------------------------------------------------------------

#: How much free space the exporter insists on before it will attempt a clip at all.
#:
#: **A policy constant, not a measurement**, and deliberately a single one. The trade-off is real
#: and is stated rather than hidden: a clip that might just have fitted into less than this is
#: refused with a measured reason instead of attempted. That is the better failure. An MP4 is not
#: finished until its trailer and ``moov`` atom are written *after* the last packet, so a volume
#: this close to full is where a clip stops being a clip and becomes a file that exists, has
#: non-zero size, and is not playable — and `_validate_output` probes a stream header, so a
#: truncated tail is exactly the shape of damage it is least able to see.
#:
#: 64 MiB is small enough to be noise on any volume a video tool is pointed at, and large enough
#: that the refusal lands before the damage rather than after it.
MIN_FREE_EXPORT_BYTES = 64 * 1024 * 1024

#: Python's ``winerror``-to-``errno`` table already maps both of these to :data:`errno.ENOSPC`, so
#: an ordinary ``OSError`` raised by the stdlib is caught by the errno test alone. They are checked
#: anyway because an ``OSError`` constructed from a raw Win32 code — which ``ctypes.WinError`` and
#: several third-party wrappers produce — carries the ``winerror`` without the translation.
#:
#: ``ERROR_HANDLE_DISK_FULL`` (39) and ``ERROR_DISK_FULL`` (112).
WINDOWS_DISK_FULL_ERRORS = (39, 112)

#: Phrases that mean FFmpeg itself hit a full volume.
#:
#: Every one is a **whole sentence fragment**, and that is a deliberate restriction rather than
#: thoroughness for its own sake. FFmpeg quotes the source and destination paths in its own stderr,
#: so a marker short enough to occur inside a folder name — ``disk full``, ``enospc``, ``no space``
#: — can misdiagnose an ordinary encoder failure as a full drive, stop a batch that had no reason to
#: stop, and tell the operator something untrue about their hardware. The costs are not symmetric:
#: a **miss** means one more clip is attempted before the next failure confirms the same thing,
#: while a **false positive** stops a healthy run and reports a wrong cause. So the markers are
#: long, and the list is short.
DISK_FULL_MARKERS = (
    "no space left on device",
    "there is not enough space on the disk",
    "error number -28",
)

#: Appended to every headroom figure the operator is shown. The sentence exists because a preflight
#: that passes is routinely read as a guarantee, and it is not one: the number was true when it was
#: measured, on a volume this application does not own.
HEADROOM_QUALIFIER = (
    "Free space was measured just now and is not a guarantee — another program can use the drive "
    "while this job runs.")

#: What is recorded against the one clip whose failure proved the volume was full. Deliberately not
#: an encoder diagnosis: the clip did not fail because the footage or the encoder was wrong.
CLIP_STORAGE_FULL_REASON = "The output drive ran out of space while writing this clip."

#: The ceiling on how much of a reason string is carried alongside it.
MAX_DETAIL_CHARS = 240

#: Why a job was refused, as a value rather than as a sentence. Two causes, and conflating them is
#: exactly what R2's F1 was about: one is a **measurement of the volume**, the other is a
#: **policy margin this module chose**, and an operator who is told the first when the second is
#: true goes looking for a hardware problem that does not exist.
#:
#: :data:`REFUSAL_VOLUME_FULL` -- the volume reported **zero** bytes free. Physically full, measured,
#: nothing to argue with.
REFUSAL_VOLUME_FULL = "volume_full"

#: :data:`REFUSAL_INSUFFICIENT_HEADROOM` -- the volume has space, just less than
#: :data:`MIN_FREE_EXPORT_BYTES`. The drive is *not* full; this tool declines to write that close to
#: the edge, and the wording says so in those terms.
REFUSAL_INSUFFICIENT_HEADROOM = "insufficient_headroom"


# ---------------------------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------------------------

def is_disk_full_error(exc: Any) -> bool:
    """Is this exception the operating system saying the volume is full?

    Total: anything that is not an ``OSError`` is simply not a disk-full error, so a caller may
    hand this whatever it caught without a type test of its own.
    """
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.ENOSPC:
        return True
    return getattr(exc, "winerror", None) in WINDOWS_DISK_FULL_ERRORS


def reports_disk_full(text: Any) -> bool:
    """Does this FFmpeg stderr say the volume is full?

    Case-insensitive substring matching over :data:`DISK_FULL_MARKERS`. See that constant for why
    the markers are long phrases and why the list does not grow to cover every plausible spelling.
    """
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in DISK_FULL_MARKERS)


def clip_failure_text(detail: Any = "") -> str:
    """The reason recorded against the clip whose failure proved the drive was full.

    FFmpeg's own words are kept after the plain sentence rather than replaced by it: the operator
    gets the diagnosis, and whoever reads the manifest afterwards still gets the evidence.
    """
    extra = " ".join(str(detail or "").split())
    if len(extra) > MAX_DETAIL_CHARS:
        extra = extra[: MAX_DETAIL_CHARS - 1] + "…"
    return f"{CLIP_STORAGE_FULL_REASON} FFmpeg: {extra}" if extra else CLIP_STORAGE_FULL_REASON


# ---------------------------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------------------------

def estimate_bytes(source_size_bytes: Any, source_duration: Any, export_seconds: Any) -> int | None:
    """Roughly how large ``export_seconds`` of this source will be, or ``None``.

    The rate is the **source's own** average bytes per second. That is an honest estimate and
    deliberately not presented as a bound in either direction: the clips are re-encoded
    (``libx264 -crf 20`` or ``h264_nvenc -cq 21``), so a heavily compressed source can grow and a
    lightly compressed one usually shrinks. An output profile that downscales changes it again.

    ``None`` whenever any input is missing, non-numeric or non-positive — an estimate nobody can
    compute is reported as absent, never as zero. Zero would read as "this job needs no space",
    which is the one answer that could turn a refusal into an admission.
    """
    try:
        size = float(source_size_bytes)
        duration = float(source_duration)
        seconds = float(export_seconds)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(size) and math.isfinite(duration) and math.isfinite(seconds)):
        return None
    if size <= 0 or duration <= 0 or seconds <= 0:
        return None
    return int(size / duration * seconds)


# ---------------------------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class CapacityVerdict:
    """One instant's answer about one volume. See :func:`evaluate` for the rules."""

    free_bytes: int | None = None
    estimated_bytes: int | None = None
    largest_clip_bytes: int | None = None
    refusal: str = ""
    note: str = ""
    volume: str = ""
    #: [FORK] R4-D R2 (F1): which of the two refusal causes this is, as a value --
    #: :data:`REFUSAL_VOLUME_FULL`, :data:`REFUSAL_INSUFFICIENT_HEADROOM`, or ``''`` when the job
    #: was not refused. Published so that a caller, a manifest or a test can tell the two apart
    #: **without matching on the rendered sentence**, which is the habit R2 asked to be removed
    #: from the terminal-cause path and is no more defensible here.
    refusal_kind: str = ""

    @property
    def ok(self) -> bool:
        """May the job proceed? A verdict with no refusal, including an unmeasured one."""
        return not self.refusal

    @property
    def measured(self) -> bool:
        """Was free space actually read? ``False`` is *unknown*, never *zero*."""
        return self.free_bytes is not None

    @property
    def advisory(self) -> bool:
        """Does this verdict carry an estimate-derived warning the operator should read?

        An estimate can only ever warn (R2 F1), so this is never a reason to stop -- it is the
        answer to "did the arithmetic have something to say?", which the panel asks and the
        admission gate deliberately does not.
        """
        return bool(self.note) and (self.estimated_bytes is not None
                                    or self.largest_clip_bytes is not None)


def evaluate(free_bytes: Any, estimated_bytes: Any = None, largest_clip_bytes: Any = None, *,
             volume: str = "", floor: int | None = None) -> CapacityVerdict:
    """Classify one measured volume against the job in front of it.

    ``floor=None`` resolves to :data:`MIN_FREE_EXPORT_BYTES` **inside the body**, deliberately,
    rather than as a ``floor=MIN_FREE_EXPORT_BYTES`` default. A default expression is evaluated
    once at definition time, which would make this function and the exporter's between-clip gate
    two readers of the same constant that can disagree -- the gate reads the module attribute at
    call time. The GUI smoke found exactly that: raising the floor to inject an unwritable volume
    changed the gate's answer and left the preflight on the old value, so the job was admitted and
    then stopped three clips later instead of being refused. One floor, read in one way.

    **[FORK] R4-D R2 (F1): only a MEASUREMENT can refuse. An ESTIMATE can only ever warn.**
    This replaces the shipped candidate's third rule, which refused a job when the measured free
    space would not hold ``largest_clip_bytes`` plus the floor. That rule was wrong on its own
    stated terms. :func:`estimate_bytes` derives its number from the **source's** average bitrate,
    and the clips are *re-encoded* (``libx264 -crf 20`` / ``h264_nvenc -cq 21``) and may be
    downscaled by the output profile -- so the figure is not a bound in either direction, and the
    common case for a high-bitrate source is that it overstates the output by a wide margin. A
    refusal built on it declines jobs that would have finished, and does so with a sentence
    (``"Not enough room for even one clip"``) stated far more confidently than the arithmetic
    behind it. There is no corresponding risk in letting it proceed: if the drive really cannot
    hold the clip, the floor gate and the between-clip gate stop the job for a *measured* reason,
    and every clip already written is kept.

    So: two rules can refuse, and both read the measurement alone.

    1. **Unmeasured is not refused.** ``free_bytes is None`` means the volume could not be read --
       a removed drive, a permission, a path that resolved to nothing. The job proceeds with a note
       saying so. Refusing on absent evidence would fail jobs that are fine, and absence of
       evidence is not evidence here any more than anywhere else in this repository.
    2. **Below the floor: refuse** -- and say *which* of the two things is true, because they are
       not the same fact and the operator acts differently on each:

       * ``free == 0`` is :data:`REFUSAL_VOLUME_FULL`. The volume is **physically full**, measured,
         and the message says the drive is full.
       * ``0 < free < floor`` is :data:`REFUSAL_INSUFFICIENT_HEADROOM`. The drive is **not** full
         and the message does not claim it is; this tool declines to write that close to the edge,
         names the margin it wants, and says how much is actually there.

    Everything else proceeds, with a note. Both estimates are advisory and neither can refuse: the
    largest-clip figure warns that the job may stop almost immediately, the total warns that it may
    stop part-way, and both carry :data:`HEADROOM_QUALIFIER` so no note can be read as a promise.
    """
    free = _whole(free_bytes)
    estimate = _whole(estimated_bytes)
    largest = _whole(largest_clip_bytes)
    floor = max(0, int(MIN_FREE_EXPORT_BYTES if floor is None else floor))
    where = f" on {volume}" if volume else ""

    if free is None:
        return CapacityVerdict(
            estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            note=("Free space on the output drive could not be measured, so this job starts "
                  "without a capacity check."))

    measured = f"{cutter.format_size(free)} free{where}"
    if free <= 0:
        return CapacityVerdict(
            free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            refusal_kind=REFUSAL_VOLUME_FULL,
            refusal=(f"The output drive is full: {measured}. "
                     f"Nothing can be written to it. Free some space and start the job again."))

    if free < floor:
        return CapacityVerdict(
            free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            refusal_kind=REFUSAL_INSUFFICIENT_HEADROOM,
            refusal=(f"Not enough free space to write safely: {measured}. The drive is not full, "
                     f"but this tool needs at least {cutter.format_size(floor)} free before it "
                     f"will start a clip, so that a clip cannot be left half-written. Free some "
                     f"space and start the job again."))

    # Both of the following are ADVISORY. See the rule above: an estimate never refuses.
    if largest is not None and free < largest + floor:
        return CapacityVerdict(
            free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            note=(f"{measured}; the largest planned clip alone is estimated at about "
                  f"{cutter.format_size(largest)}, so this job may stop almost immediately. "
                  f"The estimate is based on the source's own bitrate and the clips are "
                  f"re-encoded, so it may be well off. Starting anyway; finished clips are always "
                  f"kept. {HEADROOM_QUALIFIER}"))

    if estimate is not None and free < estimate + floor:
        return CapacityVerdict(
            free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            note=(f"{measured}; this job is estimated at about {cutter.format_size(estimate)}, so "
                  f"it may stop part-way. Finished clips are always kept. "
                  f"{HEADROOM_QUALIFIER}"))

    if estimate is not None:
        return CapacityVerdict(
            free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
            note=(f"{measured}; this job is estimated at about {cutter.format_size(estimate)}. "
                  f"{HEADROOM_QUALIFIER}"))

    return CapacityVerdict(
        free_bytes=free, estimated_bytes=estimate, largest_clip_bytes=largest, volume=volume,
        note=f"{measured}. {HEADROOM_QUALIFIER}")


def exhaustion_text(free_bytes: Any = None, *, clips_written: Any = 0, detail: Any = "",
                    volume: str = "") -> str:
    """What the operator reads when a job stopped because the drive filled up.

    Three things it must say, because each of them is something the incident's report did not: the
    drive is full (and this is not a problem with the footage), what was already written is still
    there, and the next attempt needs a fresh output folder rather than this one.
    """
    kept = _whole(clips_written) or 0
    free = _whole(free_bytes)
    where = f" on {volume}" if volume else ""
    lines = [f"The output drive ran out of space{where}, so the job stopped."]
    if free is not None:
        lines.append(f"Measured free space: {cutter.format_size(free)}.")
    lines.append(
        f"{kept} clip(s) were finished before it stopped and are still on disk." if kept
        else "No clips were finished before it stopped.")
    lines.append("This is not a problem with your source video, and your source files were not "
                 "modified. Nothing was deleted to make room.")
    lines.append("Free some space, then start the job again — it will write into a new output "
                 "folder and will not touch this one.")
    extra = " ".join(str(detail or "").split())
    if extra:
        if len(extra) > MAX_DETAIL_CHARS:
            extra = extra[: MAX_DETAIL_CHARS - 1] + "…"
        lines.append(f"Reported by FFmpeg: {extra}")
    return "\n".join(lines)


def _whole(value: Any) -> int | None:
    """A whole non-negative byte count, or ``None``. Never a guess, never a silent zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


__all__ = [
    "CLIP_STORAGE_FULL_REASON",
    "DISK_FULL_MARKERS",
    "HEADROOM_QUALIFIER",
    "MAX_DETAIL_CHARS",
    "MIN_FREE_EXPORT_BYTES",
    "REFUSAL_INSUFFICIENT_HEADROOM",
    "REFUSAL_VOLUME_FULL",
    "WINDOWS_DISK_FULL_ERRORS",
    "CapacityVerdict",
    "clip_failure_text",
    "estimate_bytes",
    "evaluate",
    "exhaustion_text",
    "is_disk_full_error",
    "reports_disk_full",
]
