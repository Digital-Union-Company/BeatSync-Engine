"""[FORK] Digital-Union (Raw Cutter R12): the live Activity & Performance read-out.

The problem this solves is narrow and real. The Raw Cutter already has a truthful progress bar
(`gui._cut_progress_html`, R1 progress P1) and it already reports per-clip and per-source counts.
What it could not say is **how long** — so an operator watching a five-minute scene-detection pass
saw a named phase, an indeterminate sweep, and no way to tell a slow job from a hung one.

This module is the whole policy of that read-out, and it is deliberately pure:

* **It reads no clock.** Every elapsed figure is computed from a reading the caller passes in, so
  the phase-reset rule, the freeze rule and the formatting are all testable on a bare interpreter
  without sleeping or patching `time`.
* **It measures nothing itself.** No subprocess, no FFmpeg, no GPU probe, no filesystem. The
  caller states what it knows; this renders exactly that and nothing more.
* **It never invents a percentage.** The bar above it owns the only measurable fraction (published
  clips over planned clips). There is no progress number in here at all, and no prediction of when
  anything will finish — segment lengths vary and the encoder is not linear in wall-clock.

The browser does the ticking. A panel carries its own elapsed figures *and* the tokens that
identify the job and the phase they belong to, so a one-second client-side interval can keep the
display advancing while the server is blocked inside FFmpeg, and can freeze it the moment a
terminal panel says the job is over. That is why the `data-*` attributes below are a contract
rather than decoration: `gui.CUT_ACTIVITY_JS` reads them, and so do the browser tests.

CLAUDE.md's hard rule applies: nothing here imports the upstream runtime. The one import is a
sibling fork module, for its phase vocabulary — restating those four strings here would create a
second vocabulary able to drift from the one the batch actually publishes.
"""
from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Dict, Optional

from . import raw_cutter_batch as _batch

#: The operator-visible status of the job, which is also the indicator colour.
STATUS_WORKING = "working"
STATUS_DONE = "done"
STATUS_STOPPING = "stopping"
STATUS_FAILED = "failed"

#: Subtle, and meaningful rather than decorative: an operator reads colour before text.
_STATUS_COLOURS: Dict[str, str] = {
    STATUS_WORKING: "#2ea043",     # green -- genuinely running
    STATUS_DONE: "#2ea043",        # green -- a terminal success
    STATUS_STOPPING: "#d29922",    # amber -- stopping, or stopped short
    STATUS_FAILED: "#f85149",      # red   -- a terminal failure
}
_STATUS_LABELS: Dict[str, str] = {
    STATUS_WORKING: "Working",
    STATUS_DONE: "Completed",
    # Past tense, deliberately. This value is only ever rendered on a TERMINAL frame -- every
    # running frame uses `STATUS_WORKING`, and a structural test pins that `STATUS_STOPPING`
    # reaches `_cut_activity_final` and nothing else. "Stopping" would therefore describe an
    # in-progress wind-down on a panel that is, by construction, already finished.
    STATUS_STOPPING: "Stopped",
    STATUS_FAILED: "Failed",
}
_NEUTRAL = "#8b949e"               # grey -- known to be not applicable, which is not a fault

# --------------------------------------------------------------------------------------------
# The phase vocabulary. These are the cutter's REAL operations and nothing else: a phase that
# names work the tool does not do would be exactly the decoration this panel exists to replace.
# --------------------------------------------------------------------------------------------

#: Single Video.
PHASE_PREPARING = "Preparing / probing"
PHASE_DETECTING = "Detecting scenes"
PHASE_PLANNING = "Planning clips"
PHASE_CUTTING = "Cutting / encoding"
#: There is no single-video `Finalizing` phase, deliberately. Reaping the export worker and
#: composing the report is real work, but it was **measured at 1 ms** in the browser, and
#: announcing it as a phase meant a finished job reported `Phase elapsed 00:00` for that
#: millisecond instead of how long its cutting had taken. A phase label that displaces a measured
#: figure with an unmeasurable one is the same defect as a percentage for an opaque phase. The
#: batch's equivalent step, `PHASE_WRITING_MANIFEST` below, is published because the batch already
#: emits a frame while it happens.

#: Folder Batch.
PHASE_DISCOVERING = "Discovering files"
PHASE_ANALYZING_SOURCE = "Analyzing current source"
PHASE_CUTTING_SOURCE = "Cutting current source"
PHASE_WRITING_MANIFEST = "Writing manifest"

#: Terminal.
PHASE_COMPLETED = "Completed"
PHASE_PARTIAL = "Partially completed"
PHASE_CANCELLED = "Cancelled"
PHASE_FAILED = "Failed"
#: A job refused before it started. Not a cancellation, and deliberately not spelled as one -- the
#: operator did not stop anything, so neither did this panel.
PHASE_NOT_RUN = "Did not run"

#: The batch publishes its own phase keys; this is the one place they become English. Probe,
#: detection and planning collapse into one analysis phase on purpose -- they are one operation
#: from the operator's side, the progress line beneath already distinguishes them, and collapsing
#: them is what stops the phase timer restarting three times per source.
BATCH_PHASE_LABELS: Dict[str, str] = {
    _batch.PHASE_PROBE: PHASE_ANALYZING_SOURCE,
    _batch.PHASE_DETECT: PHASE_ANALYZING_SOURCE,
    _batch.PHASE_PLAN: PHASE_ANALYZING_SOURCE,
    _batch.PHASE_EXPORT: PHASE_CUTTING_SOURCE,
    _batch.PHASE_MANIFEST: PHASE_WRITING_MANIFEST,
}

#: Whether an FFmpeg-family child is genuinely running during each batch phase. Stated from what
#: the batch actually does -- `probe_source` runs FFprobe, `detect_scenes` and `export_segments`
#: run FFmpeg, planning is pure arithmetic and the record is a JSON write -- rather than inferred
#: from "a job is running", which would report Active for work that is entirely in Python.
BATCH_FFMPEG_ACTIVE: Dict[str, bool] = {
    _batch.PHASE_PROBE: True,
    _batch.PHASE_DETECT: True,
    _batch.PHASE_PLAN: False,
    _batch.PHASE_EXPORT: True,
    _batch.PHASE_MANIFEST: False,
}


def batch_ffmpeg_active(phase: Optional[str]) -> bool:
    """An unpublished or absent phase answers False: before the first source is opened nothing
    has been started, and claiming Active there would be a guess."""
    return BATCH_FFMPEG_ACTIVE.get(str(phase or ""), False)


def batch_phase_label(phase: Optional[str]) -> str:
    """The English for a published batch phase. An absent phase is the discovery/startup frame,
    which is the only moment the batch has a position for nothing yet."""
    return BATCH_PHASE_LABELS.get(str(phase or ""), PHASE_DISCOVERING)


#: Qwen is not part of the Raw Cutter workflow at all. Saying so plainly is the honest answer; a
#: red "Offline" would report a component this tool never asks for as a fault.
QWEN_TEXT = "Not used in Raw Cutter"

#: GPU utilisation and VRAM are not measured. Nothing lightweight and already-supported in this
#: process can read them -- `gui.py` holds exactly two audited `subprocess.run` call sites and a
#: repeated `nvidia-smi` poll would be a third, plus sampling cost, for a cosmetic percentage. So
#: the panel says what is true instead of guessing, and the encoder is reported separately.
GPU_TEXT = "Unavailable"


#: The two encoders the cutter actually selects, in words an operator recognises. An unknown name
#: is passed through verbatim rather than guessed at: reporting a real FFmpeg encoder under a
#: friendlier label it may not deserve would be worse than reporting the label FFmpeg uses.
ENCODER_LABELS: Dict[str, str] = {
    "h264_nvenc": "NVENC H.264 (GPU)",
    "libx264": "libx264 (CPU)",
}


def encoder_label(name: Optional[str]) -> str:
    text = str(name or "").strip()
    return ENCODER_LABELS.get(text, text)


def format_elapsed(milliseconds: float) -> str:
    """`MM:SS`, widening to `H:MM:SS` past an hour. Truncates; never rounds up to a time
    that has not happened yet, and never goes backwards past zero."""
    total = int(milliseconds // 1000)
    if total < 0:
        total = 0
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%02d:%02d" % (minutes, seconds)


@dataclass(frozen=True)
class ActivitySnapshot:
    """One rendering of the panel: what is happening, for how long, and on what.

    Frozen, and every field is something the caller actually knew. `clip_done`/`clip_total` and
    `encoder` are `None` when the caller does not know them yet -- which is not the same as zero,
    and renders as an absent row rather than as a confident `0 / 0`.
    """

    phase: str
    status: str
    job_token: str
    phase_token: str
    job_ms: int
    phase_ms: int
    running: bool = True
    source: str = ""
    clip_done: Optional[int] = None
    clip_total: Optional[int] = None
    ffmpeg: Optional[bool] = None
    encoder: Optional[str] = None
    position: Optional[int] = None
    position_total: Optional[int] = None

    @property
    def status_label(self) -> str:
        return _STATUS_LABELS.get(self.status, self.status.title())

    @property
    def status_colour(self) -> str:
        return _STATUS_COLOURS.get(self.status, _NEUTRAL)


class ActivityClock:
    """The two timers, and the rule that the phase timer resets only on a real phase change.

    Pure: `start` and every later reading are supplied by the caller, in seconds from any
    monotonic source. The token is derived from the start reading rather than minted from a clock
    or a random number, so this class stays deterministic -- and two consecutive jobs in one
    browser session still get different tokens, because they cannot begin at the same millisecond.

    A note on why the token matters: the browser anchors its own interval on the first panel it
    sees for a token. Re-emitting the *same* token with a fresh elapsed figure therefore corrects
    the display without restarting it, which is precisely what a Gradio re-render must not do.
    """

    def __init__(self, start: float, prefix: str = "cut") -> None:
        self._start = float(start)
        self.token = "%s%d" % (prefix, round(self._start * 1000))
        self._phase = ""
        self._phase_key = ""
        self._phase_start = float(start)
        self._phase_index = 0

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def phase_index(self) -> int:
        return self._phase_index

    def enter(self, phase: str, now: float, key: Optional[str] = None) -> None:
        """Declare the phase that is about to run. Idempotent for an unchanged phase.

        `key` exists for the batch, where the same phase label recurs on a different source: the
        label is what the operator reads, and the key is what decides whether this is the same
        stretch of work. Passing a key of `(position, phase)` makes the phase timer answer "how
        long has THIS source been encoding", which is the useful reading; keying on the label
        alone would let it run across the whole batch.
        """
        identity = phase if key is None else key
        if identity == self._phase_key and self._phase_index:
            self._phase = phase
            return
        self._phase = phase
        self._phase_key = identity
        self._phase_start = float(now)
        self._phase_index += 1

    def snapshot(self, now: float, *, status: str = STATUS_WORKING, running: bool = True,
                 phase: Optional[str] = None, **facts) -> ActivitySnapshot:
        """Freeze the current reading into a record. Reads no clock of its own."""
        current = self._phase if phase is None else phase
        return ActivitySnapshot(
            phase=current or PHASE_PREPARING,
            status=status,
            job_token=self.token,
            phase_token="%s-%d" % (self.token, self._phase_index),
            job_ms=int(max(0.0, (float(now) - self._start)) * 1000),
            phase_ms=int(max(0.0, (float(now) - self._phase_start)) * 1000),
            running=running,
            **facts)

    def render(self, now: float, **kwargs) -> str:
        """The convenience the GUI uses: snapshot, then render, in one call."""
        return activity_html(self.snapshot(now, **kwargs))


def _row(label: str, value: str, *, colour: str = "", value_class: str = "",
         dot: str = "") -> str:
    """One `label: value` line. Everything is escaped; nothing here trusts a filename."""
    swatch = ("<span style=\"display:inline-block;width:7px;height:7px;border-radius:50%%;"
              "background:%s;margin-right:6px;vertical-align:middle;\"></span>" % dot) if dot \
        else ""
    style = "font-variant-numeric:tabular-nums;" + (("color:%s;" % colour) if colour else "")
    cls = (" class=\"%s\"" % value_class) if value_class else ""
    return ("<div style=\"display:flex;gap:8px;justify-content:space-between;\">"
            "<span style=\"opacity:.6;\">%s</span>"
            "<span%s style=\"%s\">%s%s</span></div>"
            % (_html.escape(label), cls, style, swatch, _html.escape(value)))


def activity_html(snapshot: ActivitySnapshot) -> str:
    """The compact panel, as an HTML fragment for the bottom of the progress panel.

    The `data-*` attributes are the stable contract: `data-running`, `data-job-token`,
    `data-job-ms`, `data-phase-token`, `data-phase-ms`, `data-phase`, `data-status`,
    `data-ffmpeg` and `data-gpu`. The browser timer reads the first five; the tests read all of
    them, so no assertion has to parse presentation.

    The elapsed figures are also written as **text**, server-side. That is deliberate: with the
    client-side interval blocked or not yet installed, the panel is still correct as of the moment
    it was emitted, rather than empty. The interval only ever overwrites a *running* panel.
    """
    rows = [_row("Current phase", snapshot.phase),
            _row("Status", snapshot.status_label, colour=snapshot.status_colour,
                 dot=snapshot.status_colour),
            _row("Elapsed", format_elapsed(snapshot.job_ms), value_class="bs-cut-job-elapsed"),
            _row("Phase elapsed", format_elapsed(snapshot.phase_ms),
                 value_class="bs-cut-phase-elapsed")]
    if snapshot.position and snapshot.position_total:
        rows.append(_row("Source", "%d / %d" % (snapshot.position, snapshot.position_total)))
    if snapshot.source:
        rows.append(_row("File", snapshot.source))
    if isinstance(snapshot.clip_done, int) and isinstance(snapshot.clip_total, int):
        rows.append(_row("Clip", "%d / %d" % (snapshot.clip_done, snapshot.clip_total)))
    if snapshot.ffmpeg is not None:
        rows.append(_row("FFmpeg", "Active" if snapshot.ffmpeg else "Idle",
                         dot=_STATUS_COLOURS[STATUS_WORKING] if snapshot.ffmpeg else _NEUTRAL))
    if snapshot.encoder:
        rows.append(_row("Encoder", snapshot.encoder))
    rows.append(_row("Qwen", QWEN_TEXT, colour=_NEUTRAL))
    rows.append(_row("GPU usage", GPU_TEXT, colour=_NEUTRAL))
    return (
        "<div class=\"bs-cut-activity\" data-running=\"%s\" data-job-token=\"%s\" "
        "data-job-ms=\"%d\" data-phase-token=\"%s\" data-phase-ms=\"%d\" data-phase=\"%s\" "
        "data-status=\"%s\" data-ffmpeg=\"%s\" data-gpu=\"unavailable\" "
        "style=\"margin-top:10px;padding-top:8px;border-top:1px solid rgba(255,255,255,.10);"
        "font-size:.85em;display:grid;gap:3px;\">"
        "<div style=\"opacity:.55;letter-spacing:.04em;text-transform:uppercase;"
        "font-size:.9em;margin-bottom:2px;\">Activity &amp; Performance</div>"
        "%s</div>"
        % ("1" if snapshot.running else "0",
           _html.escape(snapshot.job_token, quote=True),
           snapshot.job_ms,
           _html.escape(snapshot.phase_token, quote=True),
           snapshot.phase_ms,
           _html.escape(snapshot.phase, quote=True),
           _html.escape(snapshot.status, quote=True),
           "unknown" if snapshot.ffmpeg is None else ("active" if snapshot.ffmpeg else "idle"),
           "".join(rows)))
