"""Raw Cutter Batch V1: the pure batch bookkeeping.

[FORK] Digital-Union. One Start Batch press turns a whole folder of source videos into clips with
no further operator interaction. This module owns the parts of that which are decisions rather than
actions: the per-source outcome vocabulary, the collision-safe output layout, the totals, the
manifest payload and every line of text the operator reads.

**It decides; it never acts.** Per CLAUDE.md's hard rule it imports nothing from the upstream
runtime, and beyond that it starts no process, reads no clock, touches no file and enumerates no
directory. The FFmpeg and filesystem work is :mod:`raw_cutter_export`; the loop, the mutex, the
cancellation and the progress yields are ``gui.py``.

What it deliberately does NOT do
--------------------------------
It does not re-implement any of Raw Cutter V1. Sensitivity mapping, the quality-first shot planner,
the minimum/maximum interpretation, the skip rules, segment naming and staging naming are all
:mod:`beatsync_fork.raw_cutter`, called unchanged. A batch is V1 applied to many files and nothing
more -- there is no second planner here, and no transition detection of any kind. A missed
cross-dissolve is an accepted V1 limitation (see `docs/claude/raw-cutter-edge-cleaner.md` and the
R2-A research record), not something this module tries to fix.

The one genuinely new policy is **collision safety across sources**. Two different input files can
easily reduce to the same output folder name -- ``cameraA/video01.mp4`` and ``cameraB/video01.mp4``
most obviously, but also any two names that differ only in characters a filesystem-safe stem
strips. Overwriting is not an option anywhere in Raw Cutter, so the layout is made unique for the
WHOLE frozen input list up front, deterministically, by :func:`plan_item_directories`.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence

from beatsync_fork import raw_cutter as cutter

# ---------------------------------------------------------------------------------------------
# Outcome vocabulary
# ---------------------------------------------------------------------------------------------

#: Every clip the plan asked for was written and validated.
OUTCOME_COMPLETED = "completed"
#: The source was read and planned successfully and genuinely has nothing worth exporting. A real
#: answer about the footage at these settings, not a failure.
OUTCOME_NO_CLIPS = "no_eligible_clips"
#: The probe refused it: missing, not a file, unsupported, no video stream, no usable duration.
OUTCOME_UNREADABLE = "unreadable_source"
#: Scene detection did not run. Distinct from "no cuts found" by construction -- that distinction
#: is the entire reason `raw_cutter_export.SceneResult` exists.
OUTCOME_DETECTION_FAILED = "detection_failed"
#: The planner refused the inputs. Separate from detection so a report never blames the detector
#: for an arithmetic refusal.
OUTCOME_PLAN_FAILED = "plan_failed"
#: Some clips were written, some were not. **Never reported as completed.**
OUTCOME_PARTIAL = "partial_export"
#: Clips were planned and none survived.
OUTCOME_EXPORT_FAILED = "export_failed"
#: Cancelled while this source was being exported. Whatever had already been validated is kept.
OUTCOME_CANCELLED = "cancelled"
#: Never started, because the operator cancelled first.
OUTCOME_NOT_ATTEMPTED = "not_attempted"
#: [FORK] R2 (F-3): the batch hit an UNEXPECTED fault while this source was being processed, and
#: no clip evidence survived to classify it more precisely. Distinct from every outcome above
#: because those all describe a decision the tool made about the footage; this one describes the
#: tool itself breaking. It exists so a source that was genuinely opened can never be backfilled
#: as `not_attempted`, which would claim the batch never touched it.
OUTCOME_UNEXPECTED_ERROR = "unexpected_error"

#: Display order and operator-facing wording. A plain mapping rather than a class so the report can
#: be rendered without the batch ever having run.
OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_COMPLETED: "completed",
    OUTCOME_NO_CLIPS: "no usable shots",
    OUTCOME_UNREADABLE: "unreadable source",
    OUTCOME_DETECTION_FAILED: "scene detection failed",
    OUTCOME_PLAN_FAILED: "planning refused",
    OUTCOME_PARTIAL: "PARTIAL - some clips failed",
    OUTCOME_EXPORT_FAILED: "export failed",
    OUTCOME_CANCELLED: "cancelled mid-export",
    OUTCOME_NOT_ATTEMPTED: "not attempted",
    OUTCOME_UNEXPECTED_ERROR: "stopped by an unexpected error",
}

#: Outcomes that mean the operator has usable new clips from that source.
_PRODUCED_CLIPS = frozenset({OUTCOME_COMPLETED, OUTCOME_PARTIAL, OUTCOME_CANCELLED})

#: Outcomes that are failures of the tool or the file, as opposed to a truthful "nothing here".
_FAILED = frozenset({OUTCOME_UNREADABLE, OUTCOME_DETECTION_FAILED, OUTCOME_PLAN_FAILED,
                     OUTCOME_PARTIAL, OUTCOME_EXPORT_FAILED, OUTCOME_UNEXPECTED_ERROR})

# ---------------------------------------------------------------------------------------------
# [FORK] R2 (F-2): how the WHOLE batch ended
# ---------------------------------------------------------------------------------------------
# A per-source outcome says what happened to one file. None of them says what happened to the
# RUN, and before R2 that distinction was carried by a single `cancelled` boolean -- which forced
# an unexpected fatal error to be reported through the one flag that means "the operator stopped
# this", because it was the only non-success terminal state the model could express.
#
# These four are additive. `cancelled` keeps its exact original meaning and its place in the
# manifest, so every existing reader stays correct; `terminal_status` is the new, finer answer.

#: Ran to the end of the frozen list, nothing failed.
TERMINAL_COMPLETED = "completed"
#: Ran to the end of the frozen list; some sources failed but the batch itself was fine.
TERMINAL_COMPLETED_WITH_FAILURES = "completed_with_failures"
#: The operator pressed Cancel Batch. Finished clips are kept.
TERMINAL_CANCELLED = "cancelled"
#: The batch stopped on an unexpected error. NOT a cancellation and never reported as one.
TERMINAL_FAILED = "failed"


def terminal_status(items: Sequence[BatchItem], discovered: int, cancelled: bool,
                    fatal_error: str = "") -> str:
    """Classify how the whole run ended. One rule, used by the report AND the manifest.

    Order is the point: a fatal error outranks a cancellation flag, because a batch that crashed
    after the operator pressed Cancel still crashed, and calling that a clean cancellation is the
    exact misreport R2 exists to remove.
    """
    if str(fatal_error or "").strip():
        return TERMINAL_FAILED
    if cancelled:
        return TERMINAL_CANCELLED
    if summarise(items, discovered).failed:
        return TERMINAL_COMPLETED_WITH_FAILURES
    return TERMINAL_COMPLETED

#: Phase names used in the live progress line. Detection and export are the two slow ones.
PHASE_PROBE = "probing"
PHASE_DETECT = "detecting scenes"
PHASE_PLAN = "planning"
PHASE_EXPORT = "exporting"

MANIFEST_SCHEMA = "beatsync-raw-cutter-batch-1"
MANIFEST_FILENAME = "batch_manifest.json"

BATCH_DIRECTORY_PREFIX = "RawCutter_Batch_"


class BatchError(ValueError):
    """A batch request refused before any work started. Carries operator-facing text."""


# ---------------------------------------------------------------------------------------------
# Output layout
# ---------------------------------------------------------------------------------------------

#: Directory components keep dots, unlike a file stem: a folder legitimately named ``camera.A`` must
#: not collapse to ``camera``. So this is deliberately NOT `raw_cutter.safe_stem`, which splits the
#: extension off by design.
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._ -]+")

#: Windows reserves these as device names in every directory, with or without an extension.
_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{digit}" for digit in range(1, 10)),
    *(f"lpt{digit}" for digit in range(1, 10)),
})


def safe_component(name: Any, fallback: str = "folder") -> str:
    """One filesystem-safe path component. Never empty, never ``.``/``..``, never reserved.

    Trailing dots and spaces are stripped because Windows silently drops them, which would turn two
    distinct names into one directory after the fact -- exactly the collision this layout exists to
    prevent.
    """
    text = _UNSAFE_COMPONENT.sub("_", str(name or "").strip())
    # A name consisting only of dots and spaces strips to nothing here and falls through to the
    # fallback, which is why `.` and `..` can never survive as a component.
    text = text.rstrip(". ").strip()
    if not text:
        return fallback
    if text.split(".", 1)[0].casefold() in _RESERVED:
        text = f"_{text}"
    return text[:60].rstrip(". ") or fallback


def batch_directory_name(timestamp: Any) -> str:
    """``RawCutter_Batch_<timestamp>`` -- the one new folder a batch creates.

    The timestamp is an ARGUMENT, exactly as in `raw_cutter.run_directory_name`: this module reads
    no clock. Uniqueness is the caller's to enforce by refusing an occupied directory.
    """
    return f"{BATCH_DIRECTORY_PREFIX}{str(timestamp).strip() or 'run'}"


def _relative_parts(source_path: str, scan_root: str) -> tuple[str, ...]:
    """The sanitised relative directory chain plus a per-video folder, for ONE source.

    ``cameraA/sub/video01.mp4`` under root ``R`` becomes ``('cameraA', 'sub', 'video01')``. A video
    directly in the root becomes ``('video01',)``.

    A path that does not actually sit under the root -- which enumeration should make impossible,
    but which must not be allowed to write outside the batch folder if it ever happens -- falls back
    to a hash-only name rather than to anything containing ``..``.
    """
    stem = cutter.safe_stem(source_path)
    try:
        relative = os.path.relpath(os.path.abspath(source_path), os.path.abspath(scan_root))
    except (ValueError, OSError):
        return (f"outside_{_token(source_path)}", stem)

    head, _ = os.path.split(relative)
    chain = [piece for piece in re.split(r"[\\/]+", head) if piece not in ("", ".")]
    if any(piece == ".." for piece in chain):
        return (f"outside_{_token(source_path)}", stem)
    return tuple(safe_component(piece) for piece in chain) + (stem,)


def _naming_is_synthetic(naming: Any) -> bool:
    """[FORK] R4-C: only a real :class:`raw_cutter.OutputNaming` asking for it switches the mode.

    Delegated to the one predicate in `raw_cutter`, so the batch layout and the filenames can
    never disagree about which mode a job is in -- a divergence there would produce a `File003`
    clip inside a `video01` folder, which leaks the basename anyway and makes the manifest's
    mapping harder to read rather than easier.
    """
    return isinstance(naming, cutter.OutputNaming) and bool(naming.synthetic)


def _token(source_path: Any) -> str:
    """A short deterministic tag for one source path. Stable across runs, never random."""
    normalised = os.path.normcase(os.path.abspath(str(source_path or "")))
    return hashlib.sha1(normalised.encode("utf-8", "replace")).hexdigest()[:8]


def plan_item_directories(source_paths: Sequence[str], scan_root: str, *,
                          naming: Any = None) -> tuple[tuple[str, ...], ...]:
    """Assign every source a UNIQUE relative output directory, deterministically.

    Uniqueness is decided across the whole frozen input list at once, which is the only way to get
    it: a per-file check could not know that a later file is about to sanitise down to the same
    name. Comparison is case-insensitive because the target filesystem is.

    First choice is the preserved input structure, so the output mirrors the input and is easy to
    navigate. On collision the colliding entry gains a deterministic tag derived from its own full
    path -- never a counter alone, which would depend on enumeration order, and never randomness.
    A numeric suffix is the last-resort tie-break for the astronomically unlikely tag collision.

    **[FORK] R4-C: with synthetic naming on, the layout is one flat ``File<SSS>`` folder per
    source and the input structure is deliberately NOT mirrored.** Mirroring is what the default
    mode is for, and it reproduces the source's own folder names -- ``cameraA/sub/video01`` --
    every one of which is exactly what this mode exists to keep out of the output. The relative
    chain is therefore dropped rather than sanitised, because sanitising a leak still leaks it.

    Uniqueness in that mode is structural rather than negotiated: the component is the source's
    1-based position in the frozen list, so two entries cannot collide and no tag, counter or
    hash is needed. Note the index deliberately comes from the position here and not from
    ``naming.source_index`` -- this function is answering for the whole list at once, so a
    job-level record aimed at one source would be the wrong input to read.

    **How wide that position is padded is read from the record, not recomputed here.**
    ``naming.for_source(position)`` returns the same single-source record the exporter will build
    its filenames from, so the folder and the clips inside it are padded by one shared number that
    only the caller's frozen job record supplies. Working the width out locally from
    ``len(source_paths)`` would be a second rule over the same data, and the day the two disagreed
    the result would be ``File0007`` clips sitting in a ``File007`` folder.
    """
    if _naming_is_synthetic(naming):
        return tuple((naming.for_source(position).source_tag,)
                     for position in range(1, len(source_paths) + 1))

    assigned: dict[tuple[str, ...], str] = {}
    layout: list[tuple[str, ...]] = []

    for path in source_paths:
        parts = _relative_parts(path, scan_root)
        key = tuple(piece.casefold() for piece in parts)
        if key in assigned:
            parts = parts[:-1] + (f"{parts[-1]}__{_token(path)}",)
            key = tuple(piece.casefold() for piece in parts)
            attempt = 2
            while key in assigned and attempt < 1000:
                parts = parts[:-1] + (f"{parts[-1]}_{attempt}",)
                key = tuple(piece.casefold() for piece in parts)
                attempt += 1
        assigned[key] = path
        layout.append(parts)

    return tuple(layout)


def output_root_conflict(scan_root: str, output_root: str) -> str:
    """Refuse an output root that lies inside the source tree, or vice versa. ``''`` means fine.

    Why it is refused rather than tolerated: the batch writes MP4 files, and the enumeration that
    feeds it accepts MP4 files. An output folder inside the scanned tree means a later run -- or a
    re-scan -- ingests this run's own clips and cuts them again. Nesting the other way round is
    refused too: a source tree inside the output folder is the same loop seen from the other end.

    **Both paths must already be fully resolved by the caller** (``os.path.realpath``), because the
    comparison has to see through a junction or a symlink: ``J:\\out`` may well BE
    ``J:\\footage\\out``, and a string comparison of the unresolved names would happily accept it.
    Resolution touches the filesystem, so it belongs in the adapter; the decision belongs here.
    """
    source = os.path.normcase(os.path.abspath(scan_root))
    target = os.path.normcase(os.path.abspath(output_root))

    if os.path.splitdrive(source)[0] != os.path.splitdrive(target)[0]:
        return ""
    if source == target:
        return ("The output folder is the source folder. Choose a different output folder so the "
                "batch cannot re-ingest the clips it writes.")
    try:
        shared = os.path.commonpath([source, target])
    except ValueError:
        return ""
    if shared == source:
        return ("The output folder is inside the source folder. Choose one outside it, so a later "
                "scan cannot pick up the clips this batch writes and cut them again.")
    if shared == target:
        return ("The source folder is inside the output folder. Choose an output folder that does "
                "not contain your footage, so clips and sources never mix.")
    return ""


# ---------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchSettings:
    """The execution settings for the whole batch. One set, applied to every source.

    They are shared with Single Video mode rather than duplicated: the same sensitivity, minimum,
    maximum and Remove Audio widgets feed both, so there is exactly one value per execution setting
    on the screen.
    """

    sensitivity: int = cutter.SENSITIVITY_DEFAULT
    threshold: float = cutter.THRESHOLD_AT_DEFAULT_SENSITIVITY
    min_seconds: float = cutter.MIN_CLIP_SECONDS_DEFAULT
    max_seconds: float = cutter.MAX_CLIP_SECONDS_DEFAULT
    remove_audio: bool = True
    encoder: str = ""
    recursive: bool = True
    #: [FORK] R4-C: APPENDED with a default, so every existing construction and every existing
    #: reader keeps working unchanged. Recorded here because it belongs in the manifest's
    #: `settings` block -- it is an execution setting the operator chose -- but note it is NOT
    #: how the naming reaches the exporter: that is `raw_cutter.OutputNaming`, built once at
    #: submission. Two representations of the same choice, each at the layer that needs it, and
    #: `gui.py` derives both from the one widget value.
    rename_output_clips: bool = False

    def describe(self) -> str:
        # [FORK] R4-C: the renaming clause is appended ONLY when the mode is on, so a default
        # batch's settings line -- which the report, the panel and several assertions read -- is
        # byte-for-byte what it was before this feature existed.
        text = (f"sensitivity {self.sensitivity} (threshold {self.threshold:g}), "
                f"{self.min_seconds:g}s-{self.max_seconds:g}s clips, audio "
                f"{'removed' if self.remove_audio else 'kept'}, encoder "
                f"{self.encoder or 'unknown'}")
        if self.rename_output_clips:
            text += ", output clips renamed"
        return text


@dataclass(frozen=True)
class BatchItem:
    """The outcome of one source video. Immutable once recorded."""

    index: int
    source_path: str
    relative_parts: tuple[str, ...] = ()
    outcome: str = OUTCOME_NOT_ATTEMPTED
    clips_planned: int = 0
    clips_written: int = 0
    exported_seconds: float = 0.0
    bytes_written: int = 0
    output_directory: str = ""
    error: str = ""
    seconds: float = 0.0

    @property
    def basename(self) -> str:
        return os.path.basename(self.source_path)

    @property
    def relative_output(self) -> str:
        return "/".join(self.relative_parts)

    @property
    def label(self) -> str:
        return OUTCOME_LABELS.get(self.outcome, self.outcome)


@dataclass(frozen=True)
class BatchTotals:
    discovered: int = 0
    attempted: int = 0
    completed: int = 0
    no_clips: int = 0
    failed: int = 0
    partial: int = 0
    cancelled: int = 0
    not_attempted: int = 0
    clips_written: int = 0
    clips_planned: int = 0
    exported_seconds: float = 0.0
    bytes_written: int = 0


def summarise(items: Sequence[BatchItem], discovered: int) -> BatchTotals:
    """Fold the per-source outcomes into the headline counts.

    `attempted` counts sources the batch actually started on -- anything that is not
    ``not_attempted`` -- so a cancelled batch never claims to have attempted files it never opened.
    A `partial` source is counted in BOTH `partial` and `failed`: it produced clips and it also lost
    some, and hiding either half would be the dishonest report this is written to avoid.
    """
    outcomes = [item.outcome for item in items]
    return BatchTotals(
        discovered=discovered,
        attempted=sum(1 for o in outcomes if o != OUTCOME_NOT_ATTEMPTED),
        completed=outcomes.count(OUTCOME_COMPLETED),
        no_clips=outcomes.count(OUTCOME_NO_CLIPS),
        failed=sum(1 for o in outcomes if o in _FAILED),
        partial=outcomes.count(OUTCOME_PARTIAL),
        cancelled=outcomes.count(OUTCOME_CANCELLED),
        not_attempted=outcomes.count(OUTCOME_NOT_ATTEMPTED),
        clips_written=sum(item.clips_written for item in items),
        clips_planned=sum(item.clips_planned for item in items),
        exported_seconds=round(sum(item.exported_seconds for item in items), 3),
        bytes_written=sum(item.bytes_written for item in items),
    )


# ---------------------------------------------------------------------------------------------
# Live progress
# ---------------------------------------------------------------------------------------------

def progress_text(position: int, total: int, basename: str, phase: str,
                  items: Sequence[BatchItem], batch_directory: str) -> str:
    """The live status line while a batch runs.

    Deliberately coarse: between files, and at the start of each slow phase. Per-frame progress
    would mean parsing FFmpeg's stderr for every clip of every file, which buys the operator
    nothing on a job measured in minutes per source.
    """
    totals = summarise(items, total)
    lines = [
        f"Batch running - source {position} of {total}",
        f"  {basename or '(unknown)'}",
        f"  phase: {phase}",
        "",
        f"completed {totals.completed}   no usable shots {totals.no_clips}   "
        f"failed {totals.failed}",
        f"clips written so far: {totals.clips_written}"
        + (f" ({cutter.format_size(totals.bytes_written)})" if totals.bytes_written else ""),
    ]
    if batch_directory:
        lines.extend(["", f"Output: {batch_directory}"])
    lines.extend(["", "Press Cancel Batch to stop. Finished clips are always kept."])
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------------------------

def summary_text(items: Sequence[BatchItem], discovered: int, settings: BatchSettings,
                 batch_directory: str, elapsed_seconds: float, cancelled: bool,
                 input_root: str = "", max_listed: int = 40, *,
                 fatal_error: str = "", manifest_error: str = "") -> str:
    """The end-of-batch report. Honest after failure and after cancellation.

    It never calls a partial source complete, never presents unattempted sources as clean, and
    never implies the whole source folder was preserved -- skipping unusable material is what the
    Raw Cutter is for.

    `fatal_error` and `manifest_error` are keyword-only and default to empty, so every pre-R2
    call site keeps its exact behaviour and output. They exist because this function used to
    assert two things it could not know:

    * that an unexpected crash was a cancellation (R1 finding F-2) -- `fatal_error` now outranks
      `cancelled`, because the caller passing a crash must not be able to render a Stop; and
    * that the manifest had been saved (R1 finding F-4) -- the sentence was unconditional, and
      was composed BEFORE the write had even been attempted. The caller now measures the write
      and passes the result in, so the claim reports rather than predicts.
    """
    totals = summarise(items, discovered)
    status = terminal_status(items, discovered, cancelled, fatal_error)
    headline, icon = {
        TERMINAL_FAILED: ("Batch stopped on an unexpected error", "❌"),
        TERMINAL_CANCELLED: ("Batch cancelled", "⏹️"),
        TERMINAL_COMPLETED_WITH_FAILURES: ("Batch finished with failures", "⚠️"),
        TERMINAL_COMPLETED: ("Batch finished", "✅"),
    }[status]

    lines = [
        f"{icon} {headline}",
        "",]
    if status == TERMINAL_FAILED:
        lines.extend([
            f"The batch did not run to the end: {str(fatal_error).strip()}",
            "Everything below is what had already been done when it stopped. Clips that were "
            "written are still on disk, and your source files were not modified.",
            "",
        ])
    lines.extend([
        f"Discovered       {totals.discovered} video(s)" + (f" under {input_root}"
                                                            if input_root else ""),
        f"Attempted        {totals.attempted}",
        f"Completed        {totals.completed}",
        f"No usable shots  {totals.no_clips}",
        f"Failed / partial {totals.failed}"
        + (f"   (of which partial: {totals.partial})" if totals.partial else ""),
    ])
    if totals.cancelled:
        lines.append(f"Cancelled        {totals.cancelled} (mid-export; finished clips kept)")
    if totals.not_attempted:
        lines.append(f"Not attempted    {totals.not_attempted}")
    lines.extend([
        "",
        f"Clips exported   {totals.clips_written}"
        + (f" of {totals.clips_planned} planned" if totals.clips_planned else ""),
        f"Footage exported {cutter.format_timecode(totals.exported_seconds)}"
        f"   ({cutter.format_size(totals.bytes_written)})",
        f"Elapsed          {cutter.format_timecode(max(0.0, elapsed_seconds))}",
        "",
        f"Output folder:\n{batch_directory}" if batch_directory
        else "No output folder was created.",
        "",
        f"Settings: {settings.describe()}",
    ])

    interesting = [item for item in items if item.outcome != OUTCOME_COMPLETED]
    if interesting:
        lines.extend(["", "Sources needing attention:"])
        for item in interesting[:max_listed]:
            detail = f" - {item.error}" if item.error else ""
            written = (f" [{item.clips_written}/{item.clips_planned} clips kept]"
                       if item.clips_written else "")
            lines.append(f"  {item.basename}: {item.label}{written}{detail}")
        if len(interesting) > max_listed:
            lines.append(f"  ... and {len(interesting) - max_listed} more "
                         f"(the manifest lists every source)")

    lines.append("")
    # [FORK] R2 (F-4): report the write, never predict it. A batch that exported clips is NOT
    # failed because its bookkeeping file could not be saved -- so a manifest failure is a
    # warning that names the clips as still good, and never changes a single source outcome.
    if not batch_directory:
        pass
    elif manifest_error:
        lines.extend([
            f"⚠️ The per-source record could NOT be saved: {manifest_error}",
            f"The clips counted above were still exported and are on disk. Only "
            f"{MANIFEST_FILENAME} is missing.",
        ])
    else:
        lines.append(f"A full per-source record was written to {MANIFEST_FILENAME} in the "
                     f"output folder.")
    lines.append("Your source files were not modified. Skipped footage is intentional - the Raw "
                 "Cutter keeps clean continuous shots and leaves the rest.")
    if not cancelled and totals.no_clips and not totals.clips_written:
        lines.append("Nothing was long enough to export at these settings. Lower the minimum clip "
                     "length, or lower the sensitivity so fewer scene changes are detected.")
    return "\n".join(lines)


def manifest_payload(items: Sequence[BatchItem], discovered: int, settings: BatchSettings,
                     batch_directory: str, input_root: str, started_at: str, finished_at: str,
                     elapsed_seconds: float, cancelled: bool, *,
                     fatal_error: str = "") -> dict:
    """The machine-readable record, as a plain dict of primitives.

    A dict rather than serialised text so this module needs no ``json`` import and the adapter owns
    the write. One record per DISCOVERED source, including the ones never attempted -- a manifest
    that silently omitted them would read as a complete run.

    [FORK] R2 (F-2): `terminal_status` and `fatal_error` are ADDED; `cancelled` is unchanged in
    both name and meaning, so an existing reader keeps working and keeps being right. The new
    field is what lets a reader tell `cancelled=False because it finished` from `cancelled=False
    because it crashed` -- which before R2 looked identical in the record.

    [FORK] R4-C: `settings.rename_output_clips` is ADDED and nothing is removed or redefined, so
    the schema name is unchanged and an existing reader is unaffected. **The provenance mapping
    this feature needs already existed and is deliberately not duplicated:** every source row
    carries its real `source` path beside the `relative_output` / `output_directory` the clips
    actually went to, which in synthetic mode is that source's `File<SSS>` folder. So the record
    maps original identity to synthetic identity without the exported names carrying it, which is
    the whole requirement -- and the new flag tells a reader which mode produced the row rather
    than leaving them to infer it from the shape of a folder name.
    """
    totals = summarise(items, discovered)
    return {
        "schema": MANIFEST_SCHEMA,
        "cancelled": bool(cancelled),
        "terminal_status": terminal_status(items, discovered, cancelled, fatal_error),
        "fatal_error": str(fatal_error or ""),
        "started_at": str(started_at),
        "finished_at": str(finished_at),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "input_root": str(input_root),
        "recursive": bool(settings.recursive),
        "output_directory": str(batch_directory),
        "settings": {
            "sensitivity": int(settings.sensitivity),
            "scene_threshold": float(settings.threshold),
            "min_seconds": float(settings.min_seconds),
            "max_seconds": float(settings.max_seconds),
            "remove_audio": bool(settings.remove_audio),
            "encoder": str(settings.encoder),
            "rename_output_clips": bool(settings.rename_output_clips),
        },
        "totals": {
            "discovered": totals.discovered,
            "attempted": totals.attempted,
            "completed": totals.completed,
            "no_eligible_clips": totals.no_clips,
            "failed_or_partial": totals.failed,
            "partial": totals.partial,
            "cancelled": totals.cancelled,
            "not_attempted": totals.not_attempted,
            "clips_written": totals.clips_written,
            "clips_planned": totals.clips_planned,
            "exported_seconds": totals.exported_seconds,
            "bytes_written": totals.bytes_written,
        },
        "sources": [
            {
                "index": item.index,
                "source": item.source_path,
                "relative_output": item.relative_output,
                "output_directory": item.output_directory,
                "outcome": item.outcome,
                "clips_planned": item.clips_planned,
                "clips_written": item.clips_written,
                "exported_seconds": round(float(item.exported_seconds), 3),
                "bytes_written": int(item.bytes_written),
                "seconds": round(float(item.seconds), 3),
                "error": item.error,
            }
            for item in items
        ],
    }


def discovery_text(count: int, root: str, recursive: bool, rejected: int = 0) -> str:
    """The optional read-only count shown before a batch starts. Reads nothing but the numbers."""
    if not count:
        return (f"No supported video files found under:\n{root}\n\n"
                f"Supported: {', '.join(cutter_supported_suffixes())}."
                + ("" if recursive else "\nSubfolders were not included - turn on "
                                        "'Include subfolders' if the videos are nested."))
    lines = [f"{count} video file(s) ready to process under:", root,
             f"Subfolders: {'included' if recursive else 'not included'}"]
    if rejected:
        lines.append(f"{rejected} other file(s) were skipped as unsupported or unreadable.")
    lines.append("")
    lines.append("Nothing has been read or written yet - this is only a file count.")
    return "\n".join(lines)


def cutter_supported_suffixes() -> tuple[str, ...]:
    """The accepted suffixes, restated for the count message only.

    Kept here so this pure module can render its own text, and pinned against the adapter's
    `SUPPORTED_SOURCE_SUFFIXES` by test rather than by hope -- the adapter owns the real list and
    importing it would drag FFmpeg into this module.
    """
    return (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")


__all__ = [
    "BATCH_DIRECTORY_PREFIX",
    "BatchError",
    "BatchItem",
    "BatchSettings",
    "BatchTotals",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "OUTCOME_CANCELLED",
    "OUTCOME_COMPLETED",
    "OUTCOME_DETECTION_FAILED",
    "OUTCOME_EXPORT_FAILED",
    "OUTCOME_LABELS",
    "OUTCOME_NOT_ATTEMPTED",
    "OUTCOME_NO_CLIPS",
    "OUTCOME_PARTIAL",
    "OUTCOME_PLAN_FAILED",
    "OUTCOME_UNEXPECTED_ERROR",
    "OUTCOME_UNREADABLE",
    "PHASE_DETECT",
    "PHASE_EXPORT",
    "PHASE_PLAN",
    "PHASE_PROBE",
    "TERMINAL_CANCELLED",
    "TERMINAL_COMPLETED",
    "TERMINAL_COMPLETED_WITH_FAILURES",
    "TERMINAL_FAILED",
    "batch_directory_name",
    "cutter_supported_suffixes",
    "discovery_text",
    "manifest_payload",
    "output_root_conflict",
    "plan_item_directories",
    "progress_text",
    "safe_component",
    "summarise",
    "summary_text",
    "terminal_status",
]
