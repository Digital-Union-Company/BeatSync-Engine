"""[FORK] Digital-Union (MEDIA-VERIFY-S2-A): read-only source health verification.

Stdlib only, per CLAUDE.md's hard rule for ``src/beatsync_fork/`` — no ``logger``, ``paths``,
``gradio``, ``cupy``, ``cv2``, ``librosa`` or ``numpy``. The probe *subprocess* lives in
``src/ffmpeg_processing.py``; everything here is the pure part: statuses, result records,
the bounded work policy, aggregation and the operator report.

What this module is for
-----------------------
The owner sometimes overwrites previously analysed video files and needs to know, quickly, whether
the library on disk still corresponds to reusable Stage-5 cache records, and whether those files
have basic technical problems. Rebuilding Qwen cache to find out costs GPU hours; this answers the
question with one bounded FFprobe metadata read per source.

**Two independent questions, two independent verdicts.** Cache reuse and media health are not the
same property and are never merged into one score. A source can be perfectly readable and have no
reusable cache (it was overwritten, so it re-keyed), or be unreadable while a stale record still
sits under its old identity. The report keeps them in separate sections for that reason.

The honesty boundary that matters most
--------------------------------------
``MEDIA_METADATA_OK`` means **the container header parsed and a video stream was described**. It is
not proof that every frame decodes. FFprobe reads metadata; it does not decode the picture. The
D0-class failure this milestone was motivated by — a truncated MP4 with no ``moov`` atom — *is*
caught by a metadata read, but a file whose header is intact and whose middle is shredded is not.
:data:`METADATA_NOT_DECODE_TEXT` is the one sentence every report carries so that distinction
cannot be lost between here and the operator, and no code path in this module ever upgrades a
metadata read into a decode claim.

A probe timeout is likewise **not** a corruption verdict. A 5400-RPM drive under a concurrent read
load times out for reasons that have nothing to do with the file's bytes, so a timeout is reported
as a timeout and the source is left unverified.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Sequence, Tuple

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_PROBE_TIMEOUT_SECONDS",
    "DEFAULT_PROBE_WORKERS",
    "MAX_PROBE_WORKERS",
    "MAX_REPORT_DETAIL_LINES",
    "METADATA_NOT_DECODE_TEXT",
    "MediaStatus",
    "MediaVerdict",
    "ProbeOutcome",
    "SourceFingerprint",
    "VerificationRun",
    "WorkBudget",
    "aggregate_media",
    "classify_probe",
    "fingerprint_source",
    "format_verification_report",
    "resolve_worker_count",
    "verify_media_health",
]


# ---------------------------------------------------------------------------
# bounds
# ---------------------------------------------------------------------------

#: Hard ceiling on concurrent FFprobe children. D0 measured a 41-file sample on the owner's
#: mechanical source drive: 1.50 s sequential, 0.59 s at four workers. Beyond four, a 5400-RPM
#: spindle is seeking rather than reading, and verification would start competing with itself.
#: This is a CAP, not a target — :func:`resolve_worker_count` clamps into it.
MAX_PROBE_WORKERS = 4

#: The measured default. Deliberately equal to the cap rather than lower: four is the configuration
#: that was actually measured, and a slower unmeasured default would be a guess dressed as caution.
DEFAULT_PROBE_WORKERS = 4

#: Per-file wall clock for one FFprobe child. Generous against the ~37 ms/file D0 measured, because
#: the bound exists to stop a *stalled* probe (a dying drive, a disconnected network share), not to
#: race a healthy one. A file that exceeds it is reported ``MEDIA_PROBE_TIMEOUT``, never corrupt.
DEFAULT_PROBE_TIMEOUT_SECONDS = 20

#: Overall work budget for one verification click. Once exceeded, no NEW probe is scheduled and
#: every unreached source is reported ``MEDIA_NOT_CHECKED`` — the run becomes explicitly PARTIAL
#: rather than silently shorter. D0's ~30 s extrapolation for ~2,000 sources was never measured on
#: the whole library, so this is sized well above it and is NOT a performance promise.
DEFAULT_BUDGET_SECONDS = 600

#: How many per-source problem lines each report section prints before summarising the remainder.
#: Bounded because 2,000 failures must not become a 2,000-line textbox; a displayed failure's own
#: path is never shortened, and the number omitted is always stated.
MAX_REPORT_DETAIL_LINES = 25

#: The sentence that keeps a metadata read from being read as a decode guarantee. Carried verbatim
#: in every report.
METADATA_NOT_DECODE_TEXT = "Metadata readable — full video decoding not verified."


class MediaStatus(str, Enum):
    """Read-only media-health reporting labels.

    These are **report** labels. None of them is a cache-generation token, none reaches a cache key,
    a Stage-5 payload, the completion rule or any persisted record, and nothing in this module
    writes them anywhere but the operator's report.
    """

    METADATA_OK = "MEDIA_METADATA_OK"
    UNREADABLE = "MEDIA_UNREADABLE"
    NO_VIDEO_STREAM = "MEDIA_NO_VIDEO_STREAM"
    INVALID_METADATA = "MEDIA_INVALID_METADATA"
    PROBE_TIMEOUT = "MEDIA_PROBE_TIMEOUT"
    CHANGED_DURING_CHECK = "MEDIA_CHANGED_DURING_CHECK"
    NOT_CHECKED = "MEDIA_NOT_CHECKED"


#: Every status except ``METADATA_OK`` needs operator attention. Derived, never hand-maintained, so
#: a status added later cannot be silently omitted from the problem list.
_PROBLEM_STATUSES = tuple(s for s in MediaStatus if s is not MediaStatus.METADATA_OK)


def _bounded(text: Any, limit: int = 200) -> str:
    """One whitespace-collapsed line, bounded, with truncation marked."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# source stability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceFingerprint:
    """``(size, mtime_ns)`` for one source, or :data:`None` where it could not be stat-ed.

    Deliberately the same cheap pair the source-confirmation snapshot uses, and deliberately **not**
    presented as proof of byte identity. It detects the overwrite this milestone exists to find — a
    file replaced by different content — and it does not detect an in-place edit that preserves both
    size and mtime. Stage-5's bounded fingerprint identity has exactly the same character; neither
    is a whole-file hash and this module never claims otherwise.
    """

    size: int
    mtime_ns: int


def fingerprint_source(path: str, *, stat=os.stat) -> SourceFingerprint | None:
    """Fingerprint one source, or ``None`` if it cannot be stat-ed (missing, locked, denied)."""
    try:
        info = stat(path)
    except (OSError, ValueError):
        return None
    return SourceFingerprint(size=int(info.st_size), mtime_ns=int(info.st_mtime_ns))


# ---------------------------------------------------------------------------
# work budget
# ---------------------------------------------------------------------------

class WorkBudget:
    """A monotonic deadline for one verification click.

    Takes its clock as an argument rather than reading one, so the partial-run behaviour is testable
    without sleeping. It only ever *stops scheduling*: work already handed to a worker is always
    awaited and reaped, because abandoning a live FFprobe child would leak the process this whole
    module exists to keep bounded.
    """

    def __init__(self, seconds: float, *, started: float = 0.0) -> None:
        self.seconds = float(seconds)
        self.started = float(started)

    def exhausted(self, now: float) -> bool:
        return (float(now) - self.started) >= self.seconds

    def remaining(self, now: float) -> float:
        return max(0.0, self.seconds - (float(now) - self.started))


def resolve_worker_count(requested: Any = None) -> int:
    """Clamp a requested worker count into ``1..MAX_PROBE_WORKERS``.

    Total: a junk value falls back to the measured default rather than raising, because a bad tuning
    value must not be able to fail a read-only verification click.
    """
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = DEFAULT_PROBE_WORKERS
    if value < 1:
        value = 1
    return min(value, MAX_PROBE_WORKERS)


# ---------------------------------------------------------------------------
# probe results
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbeOutcome:
    """What the execution seam in ``ffmpeg_processing`` hands back for one source.

    ``timed_out`` is a distinct field rather than a sentinel return code: a timeout is a different
    fact from a non-zero exit, and collapsing them is how a slow drive becomes a corruption report.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str = ""
    """Set when the seam itself failed (executable missing, OS refusal) — not a verdict on the file."""


@dataclass(frozen=True)
class MediaVerdict:
    """One source's media-health result. ``status`` is the only authority; the rest is detail."""

    path: str
    status: MediaStatus
    detail: str = ""
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    duration: float = 0.0
    probe_seconds: float = 0.0

    @property
    def is_problem(self) -> bool:
        return self.status is not MediaStatus.METADATA_OK


def _finite(value: Any) -> float:
    """A finite float, or ``0.0``. Never raises, never returns NaN or an infinity."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out or out in (float("inf"), float("-inf")):
        return 0.0
    return out


def _parse_frame_rate(raw: Any) -> float:
    """FFprobe's ``r_frame_rate`` is a rational string like ``30000/1001``."""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num, _, den = text.partition("/")
        denominator = _finite(den)
        if denominator == 0.0:
            return 0.0
        return _finite(num) / denominator
    return _finite(text)


def classify_probe(path: str, outcome: ProbeOutcome, *,
                   probe_seconds: float = 0.0) -> MediaVerdict:
    """Turn one raw FFprobe outcome into a verdict. Pure: no I/O, no clock, no subprocess.

    Order is deliberate — the cheapest and most certain disqualifications come first, and a
    structural failure is never allowed to fall through into a field-level complaint:

    1. ``timed_out`` -> ``PROBE_TIMEOUT``. Checked first and never reinterpreted.
    2. seam ``error`` -> ``NOT_CHECKED``. Our tooling failed, so we hold no opinion on the file.
    3. non-zero exit -> ``UNREADABLE``. FFprobe could not open or parse the container.
    4. unparseable stdout -> ``INVALID_METADATA``.
    5. no video stream -> ``NO_VIDEO_STREAM`` (an audio-only or data-only file).
    6. unusable geometry/duration -> ``INVALID_METADATA``.
    7. otherwise ``METADATA_OK`` — metadata only, decoding NOT verified.
    """
    if outcome.timed_out:
        return MediaVerdict(
            path=path, status=MediaStatus.PROBE_TIMEOUT, probe_seconds=probe_seconds,
            detail="FFprobe exceeded its per-file timeout; the file is UNVERIFIED, not corrupt.")

    if outcome.error:
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED, probe_seconds=probe_seconds,
            detail=f"Probe could not be run: {_bounded(outcome.error)}")

    if outcome.returncode != 0:
        return MediaVerdict(
            path=path, status=MediaStatus.UNREADABLE, probe_seconds=probe_seconds,
            detail=f"FFprobe exit {outcome.returncode}: "
                   f"{_bounded(outcome.stderr) or 'no error output'}")

    try:
        payload = json.loads(outcome.stdout or "")
    except (ValueError, TypeError):
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            detail="FFprobe exited 0 but its metadata was not parseable JSON.")
    if not isinstance(payload, dict):
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            detail="FFprobe metadata was not a JSON object.")

    streams = payload.get("streams")
    streams = streams if isinstance(streams, list) else []
    video = next((s for s in streams
                  if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        return MediaVerdict(
            path=path, status=MediaStatus.NO_VIDEO_STREAM, probe_seconds=probe_seconds,
            detail=f"Container parsed, but it declares no video stream "
                   f"({len(streams)} stream(s) total).")

    container = payload.get("format")
    container = container if isinstance(container, dict) else {}
    codec = str(video.get("codec_name") or "")
    width = int(_finite(video.get("width")))
    height = int(_finite(video.get("height")))
    fps = _parse_frame_rate(video.get("r_frame_rate"))
    # The stream's own duration first, the container's as the fallback: a remuxed file often carries
    # one and not the other, and neither being present is itself the finding.
    duration = _finite(video.get("duration")) or _finite(container.get("duration"))

    missing = []
    if width <= 0 or height <= 0:
        missing.append("frame size")
    if duration <= 0:
        missing.append("duration")
    if missing:
        return MediaVerdict(
            path=path, status=MediaStatus.INVALID_METADATA, probe_seconds=probe_seconds,
            codec=codec, width=width, height=height, fps=fps, duration=duration,
            detail=f"Video stream present but its {' and '.join(missing)} is missing or "
                   f"non-positive.")

    return MediaVerdict(
        path=path, status=MediaStatus.METADATA_OK, probe_seconds=probe_seconds,
        codec=codec, width=width, height=height, fps=fps, duration=duration,
        detail=METADATA_NOT_DECODE_TEXT)


# ---------------------------------------------------------------------------
# the bounded run
# ---------------------------------------------------------------------------

@dataclass
class VerificationRun:
    """The media-health half of one verification click."""

    verdicts: List[MediaVerdict] = field(default_factory=list)
    scheduled: int = 0
    budget_exhausted: bool = False
    elapsed_seconds: float = 0.0
    workers: int = DEFAULT_PROBE_WORKERS

    @property
    def complete(self) -> bool:
        """True only when every source got a real verdict. Drives the PARTIAL banner."""
        return not self.budget_exhausted and not any(
            v.status is MediaStatus.NOT_CHECKED for v in self.verdicts)


def _verify_one(path: str, probe, per_file_timeout: float, clock) -> MediaVerdict:
    """Fingerprint, probe, re-fingerprint. Total by construction.

    The stability check brackets the probe rather than preceding it, because the window that matters
    is exactly the one in which we were reading the file. A source that changed inside that window
    gets ``CHANGED_DURING_CHECK`` **whatever the probe said** — including a clean read, because the
    bytes FFprobe described are not the bytes now on disk, and the cache classification taken
    earlier in the click describes neither with certainty.
    """
    started = clock()
    before = fingerprint_source(path)
    if before is None:
        return MediaVerdict(
            path=path, status=MediaStatus.UNREADABLE,
            probe_seconds=float(clock() - started),
            detail="Source could not be opened for inspection (missing, locked or denied).")

    try:
        outcome = probe(path, timeout=per_file_timeout)
    except BaseException as exc:  # noqa: BLE001 — a worker that raised must not kill the run
        # Our own tooling failed, so we have NO verdict on this file. Reported as not-checked with
        # the cause stated, never as a clean read and never as corruption.
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED,
            probe_seconds=float(clock() - started),
            detail=f"Verification worker raised {type(exc).__name__}: {_bounded(exc, 120)}")

    probe_seconds = float(clock() - started)
    if not isinstance(outcome, ProbeOutcome):
        return MediaVerdict(
            path=path, status=MediaStatus.NOT_CHECKED, probe_seconds=probe_seconds,
            detail=f"Probe seam returned {type(outcome).__name__}, not a ProbeOutcome.")

    after = fingerprint_source(path)
    if after != before:
        return MediaVerdict(
            path=path, status=MediaStatus.CHANGED_DURING_CHECK, probe_seconds=probe_seconds,
            detail="The file changed on disk while it was being verified; this result is "
                   "inconclusive and its cache classification may describe different bytes.")

    return classify_probe(path, outcome, probe_seconds=probe_seconds)


def verify_media_health(sources: Sequence[str], probe: Callable[..., ProbeOutcome], *,
                        workers: Any = None,
                        per_file_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
                        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
                        clock: Callable[[], float] = None) -> VerificationRun:
    """Probe every source under a bounded worker pool and a bounded overall budget.

    ``probe`` is injected — the real one is ``ffmpeg_processing.probe_media_metadata``. That is what
    lets the whole policy (ordering, budget, partial reporting, worker-exception containment) be
    tested on a bare interpreter with no FFmpeg present.

    Three properties the implementation is shaped around:

    * **Deterministic order.** Results are written into a pre-sized list at each source's own
      index, so the report order is the scan order no matter which probe finishes first. A duplicate
      path therefore stays two independent positions with two independent verdicts, exactly as the
      Stage-5 classifier treats position as the authority.
    * **Bounded in-flight work.** Sources are submitted through a sliding window rather than all at
      once, so a 2,000-source library never has 2,000 queued work items and never more than
      ``workers`` live FFprobe children.
    * **Everything started is reaped.** The budget stops *scheduling*; it never abandons a running
      child. ``ThreadPoolExecutor``'s context manager exit waits, and the loop drains ``pending``
      before returning, so no verdict is reported while a probe it describes is still alive.
    """
    clock = clock or time.perf_counter
    paths = [str(p) for p in sources]
    worker_count = resolve_worker_count(workers)
    run = VerificationRun(workers=worker_count)
    started = clock()
    budget = WorkBudget(budget_seconds, started=started)

    if not paths:
        run.elapsed_seconds = float(clock() - started)
        return run

    verdicts: List[MediaVerdict | None] = [None] * len(paths)
    # Two per worker: enough that a worker never idles waiting for the scheduler, small enough that
    # the budget check below is reached promptly instead of after thousands of submissions.
    window = max(1, worker_count * 2)
    remaining = iter(list(enumerate(paths)))
    pending: Dict[Any, int] = {}

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        drained = False
        while True:
            while not drained and not run.budget_exhausted and len(pending) < window:
                if budget.exhausted(clock()):
                    run.budget_exhausted = True
                    break
                try:
                    index, path = next(remaining)
                except StopIteration:
                    drained = True
                    break
                pending[executor.submit(
                    _verify_one, path, probe, per_file_timeout, clock)] = index
                run.scheduled += 1

            if not pending:
                break
            done, _still_running = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    verdicts[index] = future.result()
                except BaseException as exc:  # noqa: BLE001 — _verify_one is total, but never trust that
                    verdicts[index] = MediaVerdict(
                        path=paths[index], status=MediaStatus.NOT_CHECKED,
                        detail=f"Verification worker raised {type(exc).__name__}: "
                               f"{_bounded(exc, 120)}")

    # Anything the budget stopped us reaching is explicitly not-checked. A skipped probe never
    # becomes a successful one, and the run is reported PARTIAL because of these entries.
    run.verdicts = [
        v if v is not None else MediaVerdict(
            path=paths[i], status=MediaStatus.NOT_CHECKED,
            detail="Not checked: the verification work budget was exhausted before this source.")
        for i, v in enumerate(verdicts)
    ]
    run.elapsed_seconds = float(clock() - started)
    return run


# ---------------------------------------------------------------------------
# aggregation and the operator report
# ---------------------------------------------------------------------------

def aggregate_media(verdicts: Sequence[MediaVerdict]) -> Dict[str, int]:
    """Count verdicts by status. Keys are the :class:`MediaStatus` values."""
    counts = {status.value: 0 for status in MediaStatus}
    for verdict in verdicts:
        counts[verdict.status.value] = counts.get(verdict.status.value, 0) + 1
    return counts


def _detail_block(title: str, rows: Sequence[Tuple[str, str]],
                  limit: int = MAX_REPORT_DETAIL_LINES) -> List[str]:
    """Up to ``limit`` ``path -- reason`` lines, then an explicit count of what was omitted.

    A displayed row's path is never shortened: the path IS the identification the operator needs in
    order to act, and S1's MAJOR-1 was precisely a diagnostic that truncated away its own subject.
    The bound is on the NUMBER of rows, and the remainder is stated rather than dropped.
    """
    if not rows:
        return []
    lines = [f"  {title} ({len(rows)}):"]
    for path, reason in rows[:limit]:
        lines.append(f"    - {path}")
        if reason:
            lines.append(f"        {reason}")
    omitted = len(rows) - limit
    if omitted > 0:
        lines.append(f"    ... and {omitted} more not shown "
                     f"(showing the first {limit} of {len(rows)}).")
    return lines


def format_verification_report(*, folder: str, recursive: bool, verified_at: str,
                               source_count: int,
                               cache: Dict[str, Any] | None,
                               cache_classifications: Sequence[Dict[str, str]] = (),
                               run: VerificationRun | None = None,
                               folder_scan_seconds: float = 0.0,
                               cache_seconds: float = 0.0,
                               cache_unverifiable_reason: str = "") -> str:
    """The operator's read-only report: two independent sections, one snapshot header.

    The header names the folder, the recursive setting and the verification time so a report can
    never be mistaken for a different library snapshot — the owner has more than one source folder
    and a stale textbox that looks current is a wrong answer, not a cosmetic issue.
    """
    lines: List[str] = []
    lines.append("SOURCE VERIFICATION (read-only)")
    lines.append(f"  Folder: {folder or '(none)'}")
    lines.append(f"  Include subfolders: {'yes' if recursive else 'no'}")
    lines.append(f"  Verified at: {verified_at}")
    lines.append(f"  Supported source files found: {source_count}"
                 f"  [folder scan {folder_scan_seconds:.2f}s]")
    lines.append("")

    # ---- cache ------------------------------------------------------------
    lines.append("CACHE VERIFICATION")
    if cache_unverifiable_reason:
        # An unprovable Qwen backend identity is NOT evidence of a damaged cache. No per-source
        # verdict can exist in this state, so none is invented.
        lines.append("  UNVERIFIABLE — the Stage-5 cache question cannot be answered right now.")
        lines.append(f"    {cache_unverifiable_reason}")
        lines.append("    This says nothing about whether the existing cache is good; it means the "
                     "identity needed to look it up could not be established.")
    elif cache is None:
        lines.append("  UNVERIFIABLE — no classification was produced.")
    else:
        prepared = int(cache.get("prepared_count") or 0)
        needs = int(cache.get("needs_analysis_count") or 0)
        unavailable = int(cache.get("unavailable_count") or 0)
        new_or_changed = sum(1 for item in cache_classifications
                             if (item or {}).get("reason") == "NEW_OR_CHANGED")
        incomplete = sum(1 for item in cache_classifications
                         if (item or {}).get("reason") == "INCOMPLETE_OR_INVALID")
        lines.append(f"  Sources classified:            {int(cache.get('source_count') or 0)}")
        lines.append(f"  Reusable cache (PREPARED):     {prepared}")
        lines.append(f"  New or changed:                {new_or_changed}")
        lines.append(f"  Incomplete / invalid record:   {incomplete}")
        lines.append(f"  Source identity unavailable:   {unavailable}")
        if needs != new_or_changed + incomplete:
            lines.append(f"  (needs analysis total: {needs})")
        lines.append(f"  Cache verification time:       {cache_seconds:.2f}s")

        changed_rows = [(str(i.get("path") or ""),
                         "No reusable record under the current identity — new, or overwritten "
                         "since it was analysed.")
                        for i in cache_classifications
                        if (i or {}).get("reason") == "NEW_OR_CHANGED"]
        incomplete_rows = [(str(i.get("path") or ""),
                            "A record exists but is deferred, wrong-contract or AI-inconsistent.")
                           for i in cache_classifications
                           if (i or {}).get("reason") == "INCOMPLETE_OR_INVALID"]
        unavailable_rows = [(str(i.get("path") or ""),
                             "Source identity could not be computed (file missing or unreadable).")
                            for i in cache_classifications
                            if (i or {}).get("status") == "SOURCE_IDENTITY_UNAVAILABLE"]
        lines.extend(_detail_block("Needs analysis - new or changed", changed_rows))
        lines.extend(_detail_block("Needs analysis - incomplete record", incomplete_rows))
        lines.extend(_detail_block("Identity unavailable", unavailable_rows))
        if not (changed_rows or incomplete_rows or unavailable_rows):
            lines.append("  Every source has a reusable Stage-5 cache record.")
    lines.append("")

    # ---- media health -----------------------------------------------------
    lines.append("MEDIA HEALTH")
    lines.append(f"  {METADATA_NOT_DECODE_TEXT}")
    if run is None:
        lines.append("  Not run.")
        return "\n".join(lines)

    counts = aggregate_media(run.verdicts)
    lines.append(f"  Metadata verified:             {counts[MediaStatus.METADATA_OK.value]}")
    lines.append(f"  Unreadable:                    {counts[MediaStatus.UNREADABLE.value]}")
    lines.append(f"  No video stream:               {counts[MediaStatus.NO_VIDEO_STREAM.value]}")
    lines.append(f"  Invalid metadata:              {counts[MediaStatus.INVALID_METADATA.value]}")
    lines.append(f"  Probe timeout (unverified):    {counts[MediaStatus.PROBE_TIMEOUT.value]}")
    lines.append(f"  Changed during verification:   "
                 f"{counts[MediaStatus.CHANGED_DURING_CHECK.value]}")
    lines.append(f"  Not checked:                   {counts[MediaStatus.NOT_CHECKED.value]}")
    lines.append(f"  Media verification time:       {run.elapsed_seconds:.2f}s "
                 f"({run.workers} probe worker(s))")

    if not run.complete:
        lines.append("  STATUS: PARTIAL / INCOMPLETE — not every source was verified. "
                     "Unchecked sources have NO health verdict.")
    else:
        lines.append("  STATUS: COMPLETE — every source was probed (metadata only).")

    for status in _PROBLEM_STATUSES:
        rows = [(v.path, v.detail) for v in run.verdicts if v.status is status]
        lines.extend(_detail_block(status.value, rows))
    if not any(v.is_problem for v in run.verdicts):
        lines.append("  No metadata problems found in any source.")

    return "\n".join(lines)
