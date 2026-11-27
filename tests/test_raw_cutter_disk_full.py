"""Raw Cutter R4-D: what happens when the output volume fills up.

[FORK] Digital-Union. The owner's incident of 2026-11-16 is the subject: a 15-source Folder Batch
wrote 580 of 1,137 planned clips (8.2 GB), then FFmpeg returned ``-28 (No space left on device)``
for every remaining clip, nine sources were reported as failures as though their footage were
broken, and ``batch_manifest.json`` itself could not be saved with ``Errno 28``.

Three tiers, kept apart on purpose:

1. **The pure policy** -- `beatsync_fork.disk_guard`, imported normally and tested as ordinary
   code. No disk is filled and none needs to be: the module deliberately performs no measurement.
2. **The real adapter**, reached the way this repository always reaches upstream modules it cannot
   import: `src/raw_cutter_export.py`'s own function bodies are AST-extracted and executed against
   a synthesised namespace. The filesystem work is REAL -- `measure_free_space` runs its actual
   `shutil.disk_usage`, the staging writes and the no-replace rename act on real files in
   `tmp_path` -- and only the FFmpeg child is replaced. **No drive is ever filled**: a full volume
   is injected either as FFmpeg's own disk-full stderr or as a real `OSError(ENOSPC)`, which is
   what the condition actually looks like to this code.
3. The GUI handlers' own storage paths live with their existing harnesses, in
   `tests/test_raw_cutter_batch.py` (folder batch: counters, `not_attempted`, the manifest, the
   panel, the handoff, the one notification) and `tests/test_cut_handoff.py` (single video).

The injected stderr is FFmpeg's real wording, and that matters -- the classifier matches on those
phrases, so a fabricated message would test the fixture rather than the product.
"""

from __future__ import annotations

import ast
import dataclasses
import errno
import os
import shutil
import subprocess
import uuid

import pytest

from beatsync_fork import disk_guard as dg
from beatsync_fork import raw_cutter as rc
from beatsync_fork import raw_cutter_batch as batch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXPORT = os.path.join(_REPO_ROOT, "src", "raw_cutter_export.py")

#: What FFmpeg really prints when the destination volume fills up mid-mux.
DISK_FULL_STDERR = (
    "av_interleaved_write_frame(): No space left on device\n"
    "[out#0/mp4 @ 000001f0aaaa0000] Error muxing a packet\n"
    "[out#0/mp4 @ 000001f0aaaa0000] Error writing trailer: No space left on device\n"
    "Conversion failed!\n")

#: An ordinary encoder failure. The NEGATIVE control for every classification case below: without
#: it, "a full drive stops the job" would be indistinguishable from "any failure stops the job",
#: which would be a regression dressed as a feature.
ORDINARY_STDERR = (
    "[h264_nvenc @ 000001f0bbbb0000] No capable devices found\n"
    "[vost#0:0/h264_nvenc @ 000001f0bbbb0000] Error while opening encoder\n"
    "Conversion failed!\n")


# =============================================================================================
# 1. The pure policy
# =============================================================================================

def test_enospc_is_recognised_however_the_oserror_was_built():
    """`errno` covers the stdlib's own `OSError`; `winerror` covers one built from a raw Win32
    code, which `ctypes.WinError` and several wrappers produce without the errno translation."""
    assert dg.is_disk_full_error(OSError(errno.ENOSPC, "No space left on device"))
    for code in dg.WINDOWS_DISK_FULL_ERRORS:
        exc = OSError(0, "disk full", None, code)
        assert dg.is_disk_full_error(exc), code


@pytest.mark.parametrize("exc", [
    OSError(errno.EACCES, "Permission denied"),
    OSError(errno.ENOENT, "No such file or directory"),
    PermissionError("denied"),
    RuntimeError("something else"),
    ValueError("not even an OSError"),
    None,
])
def test_nothing_else_is_mistaken_for_a_full_volume(exc):
    """Total by contract: a caller may hand this whatever it caught, including a non-exception."""
    assert dg.is_disk_full_error(exc) is False


def test_ffmpegs_own_disk_full_wording_is_classified():
    assert dg.reports_disk_full(DISK_FULL_STDERR)
    assert dg.reports_disk_full("Error number -28 occurred")
    assert dg.reports_disk_full("THERE IS NOT ENOUGH SPACE ON THE DISK")


def test_an_ordinary_encoder_failure_is_not_a_full_volume():
    """The whole feature rests on this distinction, so it is asserted directly rather than implied
    by the stop cases below."""
    assert dg.reports_disk_full(ORDINARY_STDERR) is False
    assert dg.reports_disk_full("") is False
    assert dg.reports_disk_full(None) is False
    assert dg.reports_disk_full("moov atom not found") is False


@pytest.mark.parametrize("path_text", [
    r"C:\footage\disk full\clip.mp4",
    r"D:\no space\take 3.mkv",
    r"E:\enospc\a.mp4",
])
def test_a_folder_name_cannot_be_mistaken_for_a_full_drive(path_text):
    """FFmpeg quotes its input and output paths in stderr, so a marker short enough to occur
    inside a folder name would stop a healthy batch and report a wrong cause. The markers are
    therefore whole sentence fragments -- this is the case that keeps them that way."""
    stderr = (f"[in#0 @ 0000] Error opening input file {path_text}.\n"
              f"Error opening input files: Invalid data found when processing input\n")
    assert dg.reports_disk_full(stderr) is False


def test_an_unmeasured_volume_is_not_refused():
    """Absence of evidence is not evidence. A drive that could not be read starts the job."""
    verdict = dg.evaluate(None)
    assert verdict.ok
    assert verdict.measured is False
    assert verdict.free_bytes is None
    assert "could not be measured" in verdict.note


def test_a_volume_below_the_floor_is_refused():
    verdict = dg.evaluate(1024, volume="E:")
    assert not verdict.ok
    assert "full" in verdict.refusal
    assert "E:" in verdict.refusal


def test_a_volume_with_no_room_for_the_largest_clip_is_refused():
    free = dg.MIN_FREE_EXPORT_BYTES + 5 * 1024 * 1024
    verdict = dg.evaluate(free, estimated_bytes=10 ** 9, largest_clip_bytes=200 * 1024 * 1024)
    assert not verdict.ok
    assert "even one clip" in verdict.refusal


def test_a_total_estimate_that_does_not_fit_is_a_WARNING_and_never_a_refusal():
    """The estimate comes from the source's own bitrate and is not reliable enough to cancel a job
    the operator asked for. They are told it will probably stop part-way, and it starts."""
    free = dg.MIN_FREE_EXPORT_BYTES + 500 * 1024 * 1024
    verdict = dg.evaluate(free, estimated_bytes=10 ** 10, largest_clip_bytes=1024)
    assert verdict.ok
    assert "stop part-way" in verdict.note
    assert verdict.refusal == ""


def test_every_headroom_figure_carries_the_not_a_guarantee_qualifier():
    """A preflight that passes is routinely read as a guarantee. It is not one, and no call site
    can show a figure without the sentence, because the module composes them together."""
    for verdict in (dg.evaluate(10 ** 12),
                    dg.evaluate(10 ** 12, estimated_bytes=10 ** 6),
                    dg.evaluate(dg.MIN_FREE_EXPORT_BYTES + 1, estimated_bytes=10 ** 12)):
        assert dg.HEADROOM_QUALIFIER in verdict.note, verdict


def test_an_estimate_nobody_can_compute_is_absent_and_never_zero():
    """Zero would read as "this job needs no space", which is the one answer that could turn a
    refusal into an admission."""
    assert dg.estimate_bytes(0, 60.0, 10.0) is None
    assert dg.estimate_bytes(10 ** 6, 0, 10.0) is None
    assert dg.estimate_bytes(10 ** 6, 60.0, 0) is None
    assert dg.estimate_bytes(None, None, None) is None
    assert dg.estimate_bytes("big", 60.0, 10.0) is None
    assert dg.estimate_bytes(float("inf"), 60.0, 10.0) is None
    assert dg.estimate_bytes(600, 60.0, 10.0) == 100


def test_the_exhaustion_text_says_the_three_things_the_incident_report_did_not():
    text = dg.exhaustion_text(3 * 1024 * 1024, clips_written=580,
                              detail="av_interleaved_write_frame(): No space left on device")
    assert "ran out of space" in text
    assert "580 clip(s) were finished" in text
    assert "not a problem with your source video" in text
    assert "new output folder" in text
    assert "Nothing was deleted" in text


def test_the_exhaustion_text_does_not_invent_clips_it_did_not_see():
    text = dg.exhaustion_text(None, clips_written=0)
    assert "No clips were finished" in text
    assert "Measured free space" not in text, "an unmeasured volume must report no figure"


def test_the_policy_module_performs_no_measurement_and_no_mutation():
    """Structural: `disk_guard` decides, it never acts. A `disk_usage` here would make the whole
    policy need a disk to test, and anything destructive would make a module about a full drive
    able to empty one.

    Asserted over the **code**, with docstrings and comments stripped. This file's own prose names
    `shutil.disk_usage` several times -- to say where it deliberately is NOT -- and a raw-text scan
    duly failed on its own explanation. That trap is recorded repeatedly in this repository and it
    caught this test too: a guard that reads prose is not a guard on behaviour.
    """
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "disk_guard.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(ast.fix_missing_locations(tree))
    for forbidden in ("shutil", "disk_usage", "os.remove", "os.unlink", "os.rmdir", "rmtree",
                      "open(", "subprocess", "time.", "random"):
        assert forbidden not in code, forbidden


def test_the_floor_is_read_at_CALL_time_by_every_reader(monkeypatch):
    """One floor, read one way. `evaluate` must resolve `MIN_FREE_EXPORT_BYTES` in its body, not
    as a default expression -- a default is evaluated once at definition time, which would make
    this function and the exporter's between-clip gate (which reads the module attribute at call
    time) two readers of the same constant that can disagree.

    Found by the GUI smoke: raising the floor to inject an unwritable volume changed the gate's
    answer and left the preflight on the old value, so the job was admitted and then stopped
    three clips in rather than being refused.
    """
    free = dg.MIN_FREE_EXPORT_BYTES * 4
    assert dg.evaluate(free).ok
    monkeypatch.setattr(dg, "MIN_FREE_EXPORT_BYTES", free * 10)
    assert not dg.evaluate(free).ok, "evaluate must read the floor at call time"
    # An explicit floor still wins, which is what lets a test pin a figure without patching.
    assert dg.evaluate(free, floor=1024).ok


def test_the_checkpoint_cadence_is_bounded_and_deterministic():
    assert batch.checkpoint_every(1) == 1
    assert batch.checkpoint_every(batch.CHECKPOINT_MAX_WRITES) == 1
    assert batch.checkpoint_every(6000) == 120
    # However large the batch, the number of checkpoint writes stays inside the cap.
    for total in (1, 7, 50, 51, 999, 1137, 6000, 20000):
        every = batch.checkpoint_every(total)
        writes = sum(1 for p in range(1, total + 1) if batch.should_checkpoint(p, total))
        assert writes <= batch.CHECKPOINT_MAX_WRITES, (total, every, writes)


def test_the_last_source_gets_no_checkpoint_of_its_own():
    """Finalisation follows it immediately and carries the same information with
    ``complete: true``, so a checkpoint there would write a document about to be superseded."""
    assert batch.should_checkpoint(3, 3) is False
    assert batch.should_checkpoint(1, 1) is False
    assert batch.should_checkpoint(0, 5) is False
    assert batch.should_checkpoint(1, 5) is True


# =============================================================================================
# 2. The batch vocabulary
# =============================================================================================

def test_a_storage_stop_is_its_own_terminal_class():
    assert batch.terminal_status([], 3, False, storage_full=True) == batch.TERMINAL_STORAGE_FULL
    assert batch.TERMINAL_STORAGE_FULL != batch.TERMINAL_FAILED
    assert batch.TERMINAL_STORAGE_FULL != batch.TERMINAL_CANCELLED


def test_a_crash_still_outranks_a_storage_stop():
    """A batch that crashed after the drive filled still crashed, and that is what to report."""
    assert batch.terminal_status([], 3, False, "boom", storage_full=True) == \
        batch.TERMINAL_FAILED


def test_a_storage_stop_outranks_the_cancellation_flag():
    """A Cancel pressed while watching the run stop is the operator reacting to the stop, not
    causing it. Reporting a clean cancellation would hide the only fact worth acting on."""
    assert batch.terminal_status([], 3, True, storage_full=True) == batch.TERMINAL_STORAGE_FULL


def test_pre_r4d_classifications_are_byte_identical():
    """Every existing caller omits the keyword, so every existing answer must be unchanged."""
    items = [batch.BatchItem(index=1, source_path="a.mp4", outcome=batch.OUTCOME_COMPLETED)]
    assert batch.terminal_status(items, 1, False) == batch.TERMINAL_COMPLETED
    assert batch.terminal_status(items, 1, True) == batch.TERMINAL_CANCELLED
    assert batch.terminal_status(items, 1, False, "boom") == batch.TERMINAL_FAILED
    failed = [batch.BatchItem(index=1, source_path="a.mp4",
                              outcome=batch.OUTCOME_EXPORT_FAILED)]
    assert batch.terminal_status(failed, 1, False) == batch.TERMINAL_COMPLETED_WITH_FAILURES


def test_a_storage_stopped_source_counts_as_a_loss_but_is_labelled_by_the_DRIVE():
    """The distinction R4-D draws is in the reason given, not in whether it counts. A source the
    operator got no clips from is a loss either way -- it just was not the footage's fault."""
    items = [batch.BatchItem(index=1, source_path="a.mp4",
                             outcome=batch.OUTCOME_STORAGE_FULL)]
    assert batch.summarise(items, 1).failed == 1
    label = batch.OUTCOME_LABELS[batch.OUTCOME_STORAGE_FULL]
    assert "drive" in label
    assert "video" not in label and "source" not in label


def test_the_summary_names_the_drive_and_keeps_the_clips():
    items = [batch.BatchItem(index=1, source_path="a.mp4", outcome=batch.OUTCOME_PARTIAL,
                             clips_planned=10, clips_written=7, bytes_written=700),
             batch.BatchItem(index=2, source_path="b.mp4",
                             outcome=batch.OUTCOME_NOT_ATTEMPTED)]
    text = batch.summary_text(items, 2, batch.BatchSettings(), "D:/out/B", 12.0, False,
                              storage_full=True, storage_detail="Measured free space: 3.0 MB.")
    assert "output drive is full" in text
    assert "not a problem with your source videos" in text
    assert "were not attempted" in text
    assert "new output folder" in text
    assert "Measured free space: 3.0 MB." in text
    assert "Not attempted    1" in text


def test_a_storage_stop_does_not_blame_the_operators_clip_length_settings():
    """A storage stop can easily end with sources reporting no usable shots and no clips written,
    which is exactly the condition that triggers the "lower the minimum clip length" advice. The
    settings were fine; the volume was not."""
    items = [batch.BatchItem(index=1, source_path="a.mp4", outcome=batch.OUTCOME_NO_CLIPS)]
    advice = "Lower the minimum clip length"
    assert advice in batch.summary_text(items, 1, batch.BatchSettings(), "D:/o", 1.0, False)
    assert advice not in batch.summary_text(items, 1, batch.BatchSettings(), "D:/o", 1.0, False,
                                            storage_full=True)


def test_a_checkpoint_payload_cannot_be_mistaken_for_a_finished_run():
    items = [batch.BatchItem(index=1, source_path="a.mp4", outcome=batch.OUTCOME_COMPLETED)]
    running = batch.manifest_payload(items, 3, batch.BatchSettings(), "D:/o/B", "D:/in",
                                     "2026-11-27T10:00:00", "", 5.0, False, in_progress=True)
    assert running["complete"] is False
    assert running["terminal_status"] == batch.TERMINAL_IN_PROGRESS
    assert running["terminal_status"] not in (
        batch.TERMINAL_COMPLETED, batch.TERMINAL_COMPLETED_WITH_FAILURES,
        batch.TERMINAL_CANCELLED, batch.TERMINAL_FAILED, batch.TERMINAL_STORAGE_FULL)
    assert running["finished_at"] == ""


def test_a_checkpoint_never_claims_a_terminal_cause_it_cannot_know():
    """A run that is still going has not been cancelled, has not crashed and has not met a full
    volume. Carrying a caller's in-flight flags into a partial record is how a checkpoint would
    start asserting an outcome nobody measured."""
    running = batch.manifest_payload([], 3, batch.BatchSettings(), "D:/o/B", "D:/in", "s", "f",
                                     1.0, True, fatal_error="boom", storage_full=True,
                                     in_progress=True)
    assert running["cancelled"] is False
    assert running["fatal_error"] == ""
    assert running["output_storage_full"] is False


def test_the_final_write_is_complete_and_records_the_cause_as_a_field():
    final = batch.manifest_payload([], 3, batch.BatchSettings(), "D:/o/B", "D:/in", "s", "f",
                                   1.0, False, storage_full=True)
    assert final["complete"] is True
    assert final["output_storage_full"] is True
    assert final["terminal_status"] == batch.TERMINAL_STORAGE_FULL
    assert final["schema"] == batch.MANIFEST_SCHEMA, "the schema name is unchanged"


def test_the_checkpoint_has_its_own_filename():
    """Two properties fall out of it: the authoritative name never holds an incomplete document,
    and the final write's no-replace rename cannot collide with a checkpoint -- which would have
    made every checkpointed batch report its own record as lost."""
    assert batch.MANIFEST_CHECKPOINT_FILENAME != batch.MANIFEST_FILENAME
    assert batch.MANIFEST_CHECKPOINT_FILENAME.endswith(".json")


# =============================================================================================
# 3. The real adapter
# =============================================================================================

def _adapter_namespace(run_media):
    """`src/raw_cutter_export.py`'s own bodies, executed against a synthesised namespace.

    REAL: `measure_free_space` and its `shutil.disk_usage`, `capacity_for_export`,
    `_validate_output`, `_discard_staging`, the staging writes, the no-replace rename, the
    containment check, `write_batch_manifest`, `write_batch_checkpoint`,
    `discard_batch_checkpoint`, `prepare_item_directory`, and `disk_guard` itself. Replaced: the
    FFmpeg child, and nothing else.
    """
    with open(_EXPORT, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_EXPORT)
    wanted = ("SegmentResult", "CutterCancelled", "CutterJob", "StorageExhausted",
              "measure_free_space", "capacity_for_export", "_volume_label",
              "export_segments", "_discard_staging", "_validate_output",
              "build_segment_command", "write_batch_manifest", "write_batch_checkpoint",
              "discard_batch_checkpoint", "prepare_item_directory", "prepare_batch_directory",
              "EXPORT_TIMEOUT_SECONDS", "PROBE_TIMEOUT_SECONDS")
    nodes = [n for n in tree.body
             if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in wanted)
             or (isinstance(n, ast.Assign) and len(n.targets) == 1
                 and getattr(n.targets[0], "id", None) in wanted)]
    found = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    assert not set(wanted) - found, f"the adapter no longer defines {set(wanted) - found}"

    namespace = {
        "os": os, "shutil": shutil, "subprocess": subprocess, "uuid": uuid,
        "threading": __import__("threading"), "time": __import__("time"),
        "dataclass": dataclasses.dataclass, "Callable": object, "Sequence": object,
        "fork_cutter": rc, "fork_disk": dg,
        "FFMPEG_PATH": "ffmpeg", "FFPROBE_PATH": "ffprobe", "MAX_THREADS": 4,
        "_run_media_command": run_media,
        "_short_ffmpeg_error": lambda stderr, max_chars=400: (stderr or "")[-max_chars:],
        "__annotations__": {},
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), _EXPORT, "exec"),   # noqa: S102
         namespace)
    return namespace


class _Runner:
    """A fake FFmpeg that succeeds until told to fail, and records every encode it was asked for.

    `encodes` is the count the whole feature turns on: the defect was that a confirmed full volume
    produced one ordinary clip failure and the loop went on to attempt every remaining clip.
    """

    def __init__(self, *, fail_from=None, stderr=DISK_FULL_STDERR, raise_oserror=False,
                 bytes_each=2048):
        self.fail_from = fail_from
        self.stderr = stderr
        self.raise_oserror = raise_oserror
        self.bytes_each = bytes_each
        self.encodes: list[list[str]] = []
        self.probes: list[list[str]] = []

    def __call__(self, cmd, timeout, lifecycle=None):
        argv = list(cmd)
        if os.path.basename(argv[0]).startswith("ffprobe"):
            self.probes.append(argv)
            destination = argv[-1]
            if os.path.exists(destination) and os.path.getsize(destination) > 0:
                return subprocess.CompletedProcess(argv, 0, "video", "")
            return subprocess.CompletedProcess(argv, 1, "", "no such file")
        self.encodes.append(argv)
        if self.fail_from is not None and len(self.encodes) >= self.fail_from:
            if self.raise_oserror:
                raise OSError(errno.ENOSPC, "No space left on device")
            return subprocess.CompletedProcess(argv, 1, "", self.stderr)
        with open(argv[-1], "wb") as handle:
            handle.write(b"\0" * self.bytes_each)
        return subprocess.CompletedProcess(argv, 0, "", "")


def _plan(count, length=4.0):
    segments = tuple(rc.Segment(index=i + 1, start=i * length, end=(i + 1) * length)
                     for i in range(count))
    return rc.SegmentPlan(segments=segments, source_duration=count * length)


def _source(tmp_path, name="long_recording.mkv"):
    path = tmp_path / "src" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"source bytes" * 4096)
    stat = path.stat()
    return rc.SourceInfo(path=str(path), duration=60.0, width=1920, height=1080, fps=25.0,
                         has_audio=False, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _export(namespace, tmp_path, runner, clips=6, encoder="libx264", run_dir="run"):
    target = tmp_path / run_dir
    target.mkdir(parents=True, exist_ok=True)
    written: list = []
    raised = None
    try:
        namespace["export_segments"](
            source=_source(tmp_path), plan=_plan(clips), run_directory=str(target),
            remove_audio=True, encoder=encoder, on_complete=written.append)
    except BaseException as exc:                                   # noqa: BLE001 - reported
        raised = exc
    # A harness fault is not a finding.
    assert not isinstance(raised, (AttributeError, NameError, TypeError, ImportError)), raised
    return written, raised, target


# --- the control arm: without it the stop cases prove nothing --------------------------------

def test_a_clean_export_is_unchanged(tmp_path):
    runner = _Runner()
    namespace = _adapter_namespace(runner)
    written, raised, target = _export(namespace, tmp_path, runner)
    assert raised is None
    assert len(runner.encodes) == 6
    assert [r.ok for r in written] == [True] * 6
    assert len(os.listdir(target)) == 6
    assert all(r.storage_full is False for r in written)


def test_an_ordinary_encoder_failure_still_attempts_every_remaining_clip(tmp_path):
    """The NEGATIVE control, and the one the single bad passage in an otherwise usable source
    depends on. Without this, "a full drive stops the job" would be indistinguishable from "any
    failure stops the job"."""
    runner = _Runner(fail_from=3, stderr=ORDINARY_STDERR)
    namespace = _adapter_namespace(runner)
    written, raised, _target = _export(namespace, tmp_path, runner)
    assert raised is None, "an ordinary failure must not stop the export"
    assert len(runner.encodes) == 6
    assert [r.ok for r in written] == [True, True, False, False, False, False]
    assert all(r.storage_full is False for r in written)
    assert dg.CLIP_STORAGE_FULL_REASON not in (written[2].error or "")


# --- storage exhaustion after a successful publication ---------------------------------------

def test_a_confirmed_full_drive_stops_the_export_at_once(tmp_path):
    """The measured defect: on the pristine tree this attempted all 6 clips and produced 4
    identical failures. It now attempts 3 -- the third being the one that diagnosed it."""
    runner = _Runner(fail_from=3)
    namespace = _adapter_namespace(runner)
    written, raised, target = _export(namespace, tmp_path, runner)
    assert isinstance(raised, namespace["StorageExhausted"])
    assert len(runner.encodes) == 3, "no clip may be attempted after a confirmed full volume"
    assert [r.ok for r in written] == [True, True, False]
    assert written[2].storage_full is True
    assert dg.CLIP_STORAGE_FULL_REASON in written[2].error
    # FFmpeg's own words are KEPT after the diagnosis: the operator gets the reason and the
    # manifest still carries the evidence.
    assert "No space left on device" in written[2].error
    assert raised.clips_written == 2


def test_the_clips_that_reached_disk_are_untouched_by_the_stop(tmp_path):
    runner = _Runner(fail_from=4)
    namespace = _adapter_namespace(runner)
    written, _raised, target = _export(namespace, tmp_path, runner)
    published = sorted(os.listdir(target))
    assert len(published) == 3, published
    assert all(os.path.getsize(os.path.join(target, name)) == 2048 for name in published)
    # ...and only this attempt's own staging artifact was discarded: nothing in the folder is a
    # leftover `.part`, and no published clip was removed to "recover" space.
    assert not [n for n in published if ".part" in n or n.startswith(".")]
    assert [r.path for r in written if r.ok] == [
        os.path.join(str(target), n) for n in published]


def test_the_callback_is_the_evidence_a_caller_keeps(tmp_path):
    """The exception discards `export_segments`' return value, so `on_complete` is the only record
    of what reached disk -- the same contract cancellation already relies on."""
    runner = _Runner(fail_from=5)
    namespace = _adapter_namespace(runner)
    written, raised, _target = _export(namespace, tmp_path, runner)
    assert raised is not None
    assert sum(1 for r in written if r.ok) == 4


# --- storage exhaustion before the first clip ------------------------------------------------

def test_a_full_drive_before_the_first_clip_attempts_nothing(tmp_path):
    runner = _Runner(fail_from=1)
    namespace = _adapter_namespace(runner)
    written, raised, target = _export(namespace, tmp_path, runner)
    assert isinstance(raised, namespace["StorageExhausted"])
    assert len(runner.encodes) == 1, "exactly the one clip that diagnosed it"
    assert sum(1 for r in written if r.ok) == 0
    assert raised.clips_written == 0
    assert os.listdir(target) == [], "no partial file may be left under a real clip name"


def test_the_between_clip_gate_refuses_before_spending_an_encode(tmp_path, monkeypatch):
    """The capacity re-check runs before the FIRST clip too, and it refuses rather than attempting.
    Below the floor an MP4's trailer has nowhere to go, and a clip whose tail is lost still exists,
    still has non-zero size and still probes as having a video stream."""
    runner = _Runner()
    namespace = _adapter_namespace(runner)
    namespace["measure_free_space"] = lambda path: 1024
    written, raised, target = _export(namespace, tmp_path, runner)
    assert isinstance(raised, namespace["StorageExhausted"])
    assert runner.encodes == [], "not one FFmpeg child may be started"
    assert written == []
    assert os.listdir(target) == []
    assert raised.free_bytes == 1024


def test_an_unmeasured_volume_does_not_stop_the_export(tmp_path):
    """`measure_free_space` answers `None` for a drive it cannot read. Refusing on that would stop
    jobs that are fine."""
    runner = _Runner()
    namespace = _adapter_namespace(runner)
    namespace["measure_free_space"] = lambda path: None
    written, raised, _target = _export(namespace, tmp_path, runner)
    assert raised is None
    assert len(runner.encodes) == 6
    assert all(r.ok for r in written)


# --- the shared error boundary: NVENC and CPU ------------------------------------------------

@pytest.mark.parametrize("encoder", ["h264_nvenc", "libx264"])
def test_both_encoders_hit_the_same_classification_boundary(tmp_path, encoder):
    """The two paths differ in the argv and in nothing below it: the return code and stderr are
    read at one boundary, so the classification covers both without knowing which ran."""
    runner = _Runner(fail_from=2)
    namespace = _adapter_namespace(runner)
    written, raised, _target = _export(namespace, tmp_path, runner, encoder=encoder,
                                       run_dir=f"run_{encoder}")
    assert isinstance(raised, namespace["StorageExhausted"])
    assert len(runner.encodes) == 2
    assert encoder in runner.encodes[0], "the harness must really have driven this encoder"
    assert written[-1].storage_full is True


@pytest.mark.parametrize("encoder", ["h264_nvenc", "libx264"])
def test_an_ordinary_failure_is_distinct_on_both_encoders(tmp_path, encoder):
    runner = _Runner(fail_from=2, stderr=ORDINARY_STDERR)
    namespace = _adapter_namespace(runner)
    written, raised, _target = _export(namespace, tmp_path, runner, encoder=encoder,
                                       run_dir=f"ord_{encoder}")
    assert raised is None
    assert len(runner.encodes) == 6
    assert all(r.storage_full is False for r in written)


def test_a_real_enospc_oserror_from_the_child_launch_is_classified(tmp_path):
    """Not every full-volume failure comes back as a return code: the launch itself can raise."""
    runner = _Runner(fail_from=2, raise_oserror=True)
    namespace = _adapter_namespace(runner)
    written, raised, _target = _export(namespace, tmp_path, runner)
    assert isinstance(raised, namespace["StorageExhausted"])
    assert len(runner.encodes) == 2
    assert written[-1].storage_full is True


# --- no overwrite, and cancellation, both unchanged ------------------------------------------

def test_a_storage_stop_never_replaces_an_existing_destination(tmp_path):
    """The no-replace publication is untouched: the storage stop added a raise, not a new write
    path. An existing name is still refused, and refused without an encode."""
    runner = _Runner()
    namespace = _adapter_namespace(runner)
    target = tmp_path / "preoccupied"
    target.mkdir()
    taken = os.path.join(str(target), rc.segment_filename(
        str(tmp_path / "src" / "long_recording.mkv"), 1, 6))
    with open(taken, "wb") as handle:
        handle.write(b"somebody else's clip")
    written, raised, _t = _export(namespace, tmp_path, runner, run_dir="preoccupied")
    assert raised is None
    assert written[0].ok is False and "already exists" in written[0].error
    assert written[0].storage_full is False
    with open(taken, "rb") as handle:
        assert handle.read() == b"somebody else's clip", "the other file was replaced"


def test_cancellation_still_wins_over_a_capacity_check(tmp_path):
    """The cancellation check is the FIRST statement of each iteration, so a Stop is never
    outranked by the gate below it."""
    runner = _Runner()
    namespace = _adapter_namespace(runner)
    namespace["measure_free_space"] = lambda path: 1024
    job = namespace["CutterJob"]()
    job.cancel()
    target = tmp_path / "cancelled"
    target.mkdir()
    with pytest.raises(namespace["CutterCancelled"]):
        namespace["export_segments"](
            source=_source(tmp_path), plan=_plan(4), run_directory=str(target),
            remove_audio=True, encoder="libx264", job=job)
    assert runner.encodes == []


# --- the measurement itself ------------------------------------------------------------------

def test_free_space_is_measured_on_a_real_volume(tmp_path):
    namespace = _adapter_namespace(_Runner())
    free = namespace["measure_free_space"](str(tmp_path))
    assert isinstance(free, int) and free > 0
    assert free == shutil.disk_usage(os.path.realpath(str(tmp_path))).free


def test_a_destination_that_does_not_exist_yet_is_measured_at_its_nearest_ancestor(tmp_path):
    """What lets a doomed batch be refused WITHOUT first creating the folder it would write into."""
    namespace = _adapter_namespace(_Runner())
    future = tmp_path / "not" / "created" / "yet"
    assert not future.exists()
    assert namespace["measure_free_space"](str(future)) == \
        namespace["measure_free_space"](str(tmp_path))
    assert not future.exists(), "measuring must not create anything"


def test_an_unreadable_path_is_unknown_and_never_zero():
    namespace = _adapter_namespace(_Runner())
    assert namespace["measure_free_space"]("") is None
    assert namespace["measure_free_space"](None) is None
    # A drive letter that does not exist on this machine: whatever the OS says, the answer is
    # `None` or a real figure -- never `0`, which would turn every such case into a refusal.
    assert namespace["measure_free_space"](r"\\?\NoSuchVolume\x") != 0


def test_the_capacity_verdict_uses_the_plan_to_estimate(tmp_path):
    namespace = _adapter_namespace(_Runner())
    verdict = namespace["capacity_for_export"](str(tmp_path), source=_source(tmp_path),
                                               plan=_plan(6))
    assert verdict.ok, verdict.refusal
    assert verdict.measured
    assert verdict.estimated_bytes is not None and verdict.estimated_bytes > 0
    assert verdict.largest_clip_bytes is not None
    # 24 s of a 60 s source: the total estimate is four times the one-clip estimate.
    assert verdict.estimated_bytes == pytest.approx(verdict.largest_clip_bytes * 6, rel=0.01)


def test_the_capacity_verdict_works_with_no_source_at_all(tmp_path):
    """The folder batch asks before it has probed anything, so there is no estimate to compare
    against -- only the floor, which is the one check that can honestly refuse at that point."""
    namespace = _adapter_namespace(_Runner())
    verdict = namespace["capacity_for_export"](str(tmp_path))
    assert verdict.ok
    assert verdict.estimated_bytes is None
    assert verdict.largest_clip_bytes is None


# --- the manifest and its checkpoint ---------------------------------------------------------

def _payload(**over):
    base = dict(items=[], discovered=2, settings=batch.BatchSettings(),
                batch_directory="D:/o/B", input_root="D:/in", started_at="s", finished_at="f",
                elapsed_seconds=1.0, cancelled=False)
    base.update(over)
    return batch.manifest_payload(**base)


def test_a_checkpoint_then_a_final_write_both_succeed(tmp_path):
    """The regression this filename split exists for. With the checkpoint written to the
    authoritative name, the final write's no-replace `os.rename` would hit it and every
    checkpointed batch would end by reporting its own record as lost."""
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()
    assert namespace["write_batch_checkpoint"](str(directory), _payload(in_progress=True)) == ""
    assert (directory / batch.MANIFEST_CHECKPOINT_FILENAME).exists()
    assert namespace["write_batch_manifest"](str(directory), _payload()) == ""
    assert (directory / batch.MANIFEST_FILENAME).exists()
    namespace["discard_batch_checkpoint"](str(directory))
    assert not (directory / batch.MANIFEST_CHECKPOINT_FILENAME).exists()
    assert (directory / batch.MANIFEST_FILENAME).exists(), "the real record must survive"


def test_a_later_checkpoint_supersedes_an_earlier_one(tmp_path):
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()
    namespace["write_batch_checkpoint"](str(directory), _payload(discovered=2,
                                                                in_progress=True))
    namespace["write_batch_checkpoint"](str(directory), _payload(discovered=9,
                                                                in_progress=True))
    import json
    with open(directory / batch.MANIFEST_CHECKPOINT_FILENAME, encoding="utf-8") as handle:
        assert json.load(handle)["totals"]["discovered"] == 9
    # No staging temp survives. `endswith`, not `in`: the checkpoint itself is
    # `batch_manifest.partial.json`, which contains the substring `.part` and is supposed to be
    # there -- a containment test here asserted the opposite of what it meant.
    assert [n for n in os.listdir(directory) if n.endswith(".part")] == []
    assert os.listdir(directory) == [batch.MANIFEST_CHECKPOINT_FILENAME]


def test_a_full_volume_during_the_final_write_is_reported_in_plain_words(tmp_path):
    """The sentence the 2026-11-16 report ended on was `[Errno 28]`, which is the one thing in it
    nobody could act on."""
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()

    def _full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    namespace["open"] = _full
    message = namespace["write_batch_manifest"](str(directory), _payload())
    assert "the output drive is full" in message
    assert batch.MANIFEST_FILENAME in message
    assert not (directory / batch.MANIFEST_FILENAME).exists()
    assert [n for n in os.listdir(directory)] == [], "no staging artifact may survive"


def test_an_ordinary_manifest_failure_keeps_its_existing_wording(tmp_path):
    """Pre-R4-D behaviour, unchanged: only the disk-full case gained a sentence."""
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()

    def _denied(*_a, **_k):
        raise OSError(errno.EACCES, "Permission denied")

    namespace["open"] = _denied
    message = namespace["write_batch_manifest"](str(directory), _payload())
    assert message.startswith(f"Could not write {batch.MANIFEST_FILENAME}")
    assert "output drive is full" not in message


def test_a_checkpoint_write_failure_never_raises(tmp_path):
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()

    def _full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    namespace["open"] = _full
    message = namespace["write_batch_checkpoint"](str(directory), _payload(in_progress=True))
    assert message
    assert not (directory / batch.MANIFEST_CHECKPOINT_FILENAME).exists()
    assert os.listdir(directory) == []


def test_discarding_a_checkpoint_that_is_not_there_is_harmless(tmp_path):
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()
    namespace["discard_batch_checkpoint"](str(directory))      # must not raise
    assert os.listdir(directory) == []


def test_a_full_volume_at_the_item_directory_raises_rather_than_refusing(tmp_path):
    """An error string is a statement about THIS source, and the caller rightly carries on to the
    next -- which for a full drive means carrying on to fail identically on every one of them."""
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()

    def _full(*_a, **_k):
        raise OSError(errno.ENOSPC, "No space left on device")

    namespace["os"] = type("_OS", (), {
        **{name: getattr(os, name) for name in dir(os) if not name.startswith("__")},
        "makedirs": staticmethod(_full)})
    with pytest.raises(namespace["StorageExhausted"]):
        namespace["prepare_item_directory"](str(directory), ("cameraA", "clip01"))


def test_every_other_item_directory_failure_still_returns_an_error_string(tmp_path):
    """Unchanged: only the disk-full case changed shape, so no existing refusal moved."""
    namespace = _adapter_namespace(_Runner())
    directory = tmp_path / "B"
    directory.mkdir()

    def _denied(*_a, **_k):
        raise OSError(errno.EACCES, "Permission denied")

    namespace["os"] = type("_OS", (), {
        **{name: getattr(os, name) for name in dir(os) if not name.startswith("__")},
        "makedirs": staticmethod(_denied)})
    path, error = namespace["prepare_item_directory"](str(directory), ("cameraA", "clip01"))
    assert path == ""
    assert "Could not create the clip folder" in error


def test_a_subsequent_batch_cannot_reuse_the_earlier_partial_run(tmp_path):
    """"A later attempt must use a fresh output directory." The creation IS the test, so a
    storage-stopped batch's folder can never be adopted by the retry."""
    namespace = _adapter_namespace(_Runner())
    root = tmp_path / "out"
    first, error = namespace["prepare_batch_directory"](str(root), "20261127_120000")
    assert error == "" and os.path.isdir(first)
    again, error = namespace["prepare_batch_directory"](str(root), "20261127_120000")
    assert again == "" and "already exists" in error
    later, error = namespace["prepare_batch_directory"](str(root), "20261127_120500")
    assert error == "" and later != first


# =============================================================================================
# 4. Structure: the stop is named before the generic handler, everywhere
# =============================================================================================

def _gui_function(name):
    path = os.path.join(_REPO_ROOT, "src", "gui.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert node is not None, name
    return node


@pytest.mark.parametrize("handler", ["_on_cut_run", "_on_cut_batch_run"])
def test_the_storage_stop_is_caught_before_every_generic_exception_clause(handler):
    """`StorageExhausted` is an ordinary `Exception`, so a generic clause listed first would report
    a correctly diagnosed and correctly handled full drive as a crash -- which is the misreport
    this whole feature exists to remove, one level up from where it was."""
    node = _gui_function(handler)
    seen = 0
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Try):
            continue
        order = [ast.unparse(h.type) if h.type is not None else "bare"
                 for h in inner.handlers]
        if not any("StorageExhausted" in text for text in order):
            continue
        seen += 1
        storage_at = next(i for i, text in enumerate(order) if "StorageExhausted" in text)
        for index, text in enumerate(order):
            if text in ("Exception", "BaseException", "bare"):
                assert index > storage_at, (handler, order)
    assert seen, f"{handler} no longer handles StorageExhausted at all"


def test_the_exporter_raises_only_after_recording_the_clip():
    """`record()` fires `on_complete`, which is the only evidence a caller interrupted by this
    exception will have. Raising first would lose the diagnosing clip from the accounting.

    There are deliberately TWO raise sites, and only one of them is the subject here. The
    between-clip gate raises **without** recording anything, correctly: it refuses a clip that was
    never attempted, so there is no result to report. The first draft of this test took the first
    `raise` it found, which was the gate, and duly concluded the ordering was wrong.
    """
    with open(_EXPORT, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=_EXPORT)
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "export_segments")
    raises = [n for n in ast.walk(node) if isinstance(n, ast.Raise)
              and "StorageExhausted" in ast.unparse(n)]
    assert len(raises) == 2, [ast.unparse(n) for n in raises]
    gate, after_failure = sorted(raises, key=lambda n: n.lineno)

    # The gate's own block records nothing: it is reached before the clip exists at all.
    gate_block = next(n for n in ast.walk(node)
                      if isinstance(n, ast.If) and gate in ast.walk(n))
    assert "record(" not in ast.unparse(gate_block)

    # The failure path's block records FIRST. Both statements live in the same `if failure:` body,
    # so their order there is the contract.
    failure_block = next(n for n in ast.walk(node)
                         if isinstance(n, ast.If) and after_failure in ast.walk(n)
                         and ast.unparse(n.test) == "failure")
    lines = [ast.unparse(statement) for statement in failure_block.body]
    record_at = next(i for i, text in enumerate(lines) if text.startswith("record("))
    raise_at = next(i for i, text in enumerate(lines) if "raise StorageExhausted" in text)
    assert record_at < raise_at, lines


def test_nothing_in_the_adapter_deletes_a_published_clip_to_recover_space():
    """There is no automatic recovery in R4-D, and that is a product decision rather than an
    omission: a tool that frees space by removing the operator's footage is worse than one that
    stops."""
    with open(_EXPORT, encoding="utf-8") as handle:
        source = handle.read()
    for forbidden in ("shutil.rmtree", "os.rmdir", "os.removedirs", "shutil.move"):
        assert forbidden not in source, forbidden
    # The only removal is `_discard_staging`, and the only things handed to it are this attempt's
    # own staging file and this batch's own superseded checkpoint.
    tree = ast.parse(source)
    targets = {ast.unparse(call.args[0]) for call in ast.walk(tree)
               if isinstance(call, ast.Call)
               and getattr(call.func, "id", "") == "_discard_staging"}
    assert targets == {"staging",
                       "os.path.join(os.path.abspath(batch_directory), "
                       "fork_batch.MANIFEST_CHECKPOINT_FILENAME)"}, targets
