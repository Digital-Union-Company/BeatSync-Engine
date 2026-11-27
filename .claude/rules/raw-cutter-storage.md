---
paths:
  - src/beatsync_fork/disk_guard.py
  - src/raw_cutter_export.py
  - src/beatsync_fork/raw_cutter_batch.py
  - tests/test_raw_cutter_disk_full.py
---

> Scoped rule. The always-loaded constitution is the root `CLAUDE.md`; operating policies are in
> `.claude/rules/operating-policies.md`. For the rest of the Raw Cutter's GUI seams — the Activity
> panel, the handoff, the progress wrapper — see `.claude/rules/gui-integration.md`.

# Raw Cutter: a full output volume (R4-D)

## Why this exists

**Owner incident, 2026-11-16.** A Folder Batch attempted 15 sources; 5 completed, 1 was partial,
**9 were reported as failures as though their footage were broken**. FFmpeg returned
`-28 (No space left on device)` repeatedly; 580 of 1,137 planned clips (8.2 GB) were written, and
then `batch_manifest.json` could not be saved — with `[Errno 28]` as the whole explanation.

The tool had no vocabulary for *the drive is full*, so it used the one it had: *this video failed*,
once per remaining clip. **Reproduced on pristine `71403afa`** with FFmpeg's real disk-full stderr
injected at clip 3 of a 6-clip plan: **6 export attempts, 4 identical failures, no stop.**

## The architectural split, and what each half may not do

```
beatsync_fork/disk_guard.py   DECIDES    classification, the one floor, the estimate, the wording
raw_cutter_export.py          MEASURES   exactly one shutil.disk_usage, and acts
raw_cutter_batch.py           RECORDS    the outcome, the terminal class, the checkpoint payload
gui.py                        REPORTS    six renderings of one class
```

- **`disk_guard` performs no measurement.** It imports no `shutil`, calls no `disk_usage`, opens no
  file, starts no process, reads no clock. A test asserts that over the **code** with docstrings
  stripped — the module's own prose names `shutil.disk_usage` several times, to say where it
  deliberately is not, and a raw-text scan duly failed on its own explanation. *A guard that reads
  prose is not a guard on behaviour.*
- **It deletes nothing, and neither does anything else in R4-D.** There is no automatic recovery: a
  full drive is reported, scheduling stops, and every clip already on disk stays exactly where it
  is. A tool that frees space by removing the operator's footage is worse than one that stops.
- **It never retries.** A volume that is full does not empty itself while a batch waits, and an
  unbounded retry per clip *is* the incident.

## Classification is positive, and the markers are long

Two triggers, and nothing else may be added:

1. FFmpeg's own disk-full wording in stderr (`reports_disk_full`);
2. an `OSError` the OS marked `ENOSPC`, or carrying `ERROR_DISK_FULL` / `ERROR_HANDLE_DISK_FULL`
   as `winerror` — both checked, because Python's own table maps those codes to `ENOSPC` but an
   `OSError` built from a raw Win32 code (`ctypes.WinError`) carries only the `winerror`.

**Never infer it from a return code, a missing output file or an empty output** — all three are
also what an ordinary encoder failure looks like, and misreading one as a full drive stops a
healthy run and tells the operator something untrue about their hardware.

**The stderr markers are whole sentence fragments on purpose.** FFmpeg quotes its input and output
paths, so a marker short enough to occur inside a folder name — `disk full`, `no space`, `enospc` —
can misdiagnose an ordinary failure. The costs are not symmetric: a **miss** means one more clip is
attempted before the next failure confirms the same thing; a **false positive** stops a healthy run
and reports a wrong cause. So the markers stay long and the list stays short. A test drives three
plausible folder names (`C:\footage\disk full\clip.mp4` among them) through the classifier and
requires a negative.

## One floor, and the trade-off is stated

**The floor is read at CALL time by every reader.** `evaluate` resolves `MIN_FREE_EXPORT_BYTES`
inside its body from a `floor=None` parameter, and never as a `floor=MIN_FREE_EXPORT_BYTES`
default — a default expression is evaluated once at definition time, which would make the preflight
and the exporter's between-clip gate two readers of one constant that can disagree, because the
gate reads the module attribute. The GUI smoke found exactly that: raising the floor to inject an
unwritable volume changed the gate and left the preflight on the old value, so the job was admitted
and then stopped three clips in instead of being refused. A test pins call-time resolution.

`MIN_FREE_EXPORT_BYTES = 64 MiB` is a **policy constant, not a measurement**. Below it nothing is
attempted, which means a clip that *might* have fitted into less is refused with a measured reason
rather than attempted. That is the better failure, and the argument is specific: an MP4 is not
finished until its trailer and `moov` atom are written **after** the last packet, so a volume this
close to full is where a clip stops being a clip and becomes a file that exists, has non-zero size
and is not playable — which is precisely the shape of damage `_validate_output`'s stream-header
probe is least able to see.

Three rules, in order, and only the first two may refuse:

| condition | answer |
|---|---|
| free space unreadable (`None`) | **proceed**, with a note. Absence of evidence is not evidence |
| free `<` floor | **refuse** — nothing can be written safely |
| free `<` largest planned clip + floor | **refuse** — that clip can never be produced |
| free `<` total estimate + floor | **warn**, never refuse |

**The total estimate may not refuse a job.** It is the source's own average bytes per second applied
to the exported footage, and the clips are re-encoded, so it is neither a floor nor a ceiling. An
estimate nobody can compute is `None`, **never 0** — zero would read as "this job needs no space",
the one answer that could turn a refusal into an admission.

**Every headroom figure carries `HEADROOM_QUALIFIER`**, composed into the note by the module rather
than by each call site, so no screen can show the number without also saying it was measured just
now and is not a guarantee. A passing preflight is routinely read as a promise; it is not one.

## Measurement: resolve, then measure

`measure_free_space` has three properties, each of which something in this repository has
previously got wrong somewhere:

- **`realpath` first.** An `output\` that is a junction onto a larger drive is a legitimate,
  measured-working setup here (see the share-publication rule in `gui-integration.md`), so
  measuring the drive letter the operator typed would report the wrong volume's headroom for
  exactly that configuration.
- **A destination that does not exist yet is measured at its nearest existing ancestor**, which is
  what lets a doomed batch be refused *without first creating the folder it would have written
  into*. Measuring must create nothing — a test asserts the path is still absent afterwards.
- **An empty path is tested for BEFORE `abspath`.** `os.path.abspath("")` returns the **current
  working directory**, which for this application is its own install folder: a blank destination
  would be measured on the wrong volume and could admit a job that should have been refused. Found
  by this feature's own test. The same trap `_normalize_folder_text` was written for.

**Staging needs no second measurement, and that is structural.** `export_segments` writes its
staging file *inside* `run_directory` — which is why the publication is a same-directory rename and
`EXDEV` is impossible there — so one volume holds the staged bytes, the published clip and the batch
record alike. **If staging ever moves out of the run directory, `capacity_for_export` needs a second
measurement on the same day.**

## The stop

- **`StorageExhausted` is an ordinary `Exception`** — the same choice `CutterCancelled` and
  `RenderCancelled` make — **so every broad `except Exception` on this path must name it first.**
  Both `_on_cut_run` and `_on_cut_batch_run` do, and a structural test walks every `try` in both
  handlers and requires the storage clause to precede any `Exception` / `BaseException` / bare
  clause. Without that ordering a correctly diagnosed and correctly handled full drive is reported
  as a crash, which is the incident reproduced one layer up.
- **The clip is recorded BEFORE the raise.** `record()` fires `on_complete`, which is the only
  evidence a caller interrupted by the exception has — the exception discards `export_segments`'
  return value, exactly as cancellation does. There are deliberately **two** raise sites, and only
  one records: the between-clip gate refuses a clip that was never attempted, so there is nothing
  to report for it.
- **The between-clip gate runs before the FIRST clip too**, and before `on_progress`, so a clip
  that will not be attempted is never announced as starting. It costs one `disk_usage` per clip —
  microseconds against an encode measured in seconds, so there is no case for sampling it less
  often.
- **No child process is running when it raises.** Both triggers sit between clips, and
  `_run_media_command` has already reaped the one it ran. Nothing new terminates anything.
- **An ordinary encoder failure is untouched**: recorded, and the loop carries on to the next clip.
  That is the behaviour a single bad passage in an otherwise usable source depends on, and it is
  the negative control in every classification test — including against the real binary, where it
  still attempts all 6 of 6.
- **`prepare_item_directory` raises** on a full volume and keeps returning `(path, error)` for
  every other `OSError`. The asymmetry is the point: an error string is a statement about *this
  source*, so the caller rightly carries on — which for a full drive means carrying on to fail
  identically on every remaining source.

## The vocabulary: five terminal classes, and one that is not terminal

```
OUTCOME_STORAGE_FULL        this source produced nothing because the DRIVE filled
TERMINAL_STORAGE_FULL       the RUN stopped scheduling because the drive filled
TERMINAL_IN_PROGRESS        checkpoint-only; `terminal_status` can never return it
```

- **`OUTCOME_STORAGE_FULL` is deliberately not `OUTCOME_EXPORT_FAILED`**, which means "the encoder
  could not produce these clips" — a statement about the footage or the encoder. Nine such rows is
  what made the incident's report read as nine broken videos. Its label names the **drive** and
  never the video, and a test asserts exactly that.
- **A source that DID publish clips before the stop is `OUTCOME_PARTIAL`**, not the storage
  outcome: it produced results, and the vocabulary already has the honest word. The ladder puts the
  storage branch **below** the complete check, so a source whose every planned clip reached disk is
  still complete whatever stopped the batch afterwards.
- **`TERMINAL_STORAGE_FULL` is a fifth class, not a flavour of `TERMINAL_FAILED`.** The tool did
  not break. Ordering in `terminal_status`: `fatal_error` → `storage_full` → `cancelled` →
  failures → completed. A crash after the drive filled still crashed; a Cancel pressed while
  watching the run stop is the operator reacting to the stop rather than causing it.
- **Six renderings of that one class, never a reading of a status string**: the summary headline,
  the progress panel's title/tone, the activity read-out (`_CUT_BATCH_ACTIVITY`), the handoff
  outcome (`_CUT_BATCH_HANDOFF`), the manifest (`terminal_status` + `output_storage_full`), and the
  one notification. Four of those are keyed tables; **a new terminal class must be added to every
  one of them**, and `summary_text`'s headline dict is a plain index that raises `KeyError` without
  it. Tests in `test_cut_handoff.py` and `test_desktop_notifications.py` assert that the tables
  partition what the classifier can actually *produce* — measured by calling it, not by listing
  names.
- **`TERMINAL_IN_PROGRESS` is excluded from those partitions**, and that is accuracy rather than a
  weakening: `terminal_status` can never return it, so `sink['terminal']` cannot hold it and a row
  for it would be an unreachable branch. Asserted by calling the classifier.
- **The notification can never be SUCCESS.** The storage branch in `_notify_cut_batch` is a third
  rule and deliberately **not** a table: a stop after twelve good sources is genuinely partial, one
  before the first is a failure, and only the counts can tell those apart — while
  `_CUT_BATCH_RAN_TO_END`'s fold *can* answer SUCCESS, which a run stopped part-way through its
  frozen list must never be announced as. The success answer is absent by construction rather than
  filtered afterwards.
- **A storage stop suppresses the "lower your minimum clip length" advice.** It can easily end with
  sources reporting no usable shots and no clips written, which is exactly that advice's trigger.
  The settings were fine; the volume was not.

## The checkpoint

The recoverable-progress half, and the reason the incident's record was lost at all.

- **One builder.** `manifest_payload(..., in_progress=True)` — no second coordinator, no second
  schema, no second set of totals.
- **Its own filename**, `MANIFEST_CHECKPOINT_FILENAME`, and that is the mechanism rather than a
  convention. Two properties fall out of it: the authoritative name never holds an incomplete
  document, **and** the final write's no-replace `os.rename` cannot collide with a checkpoint. Had
  the checkpoint used the real name, every checkpointed batch would have ended by reporting its own
  record as lost.
- **`os.replace` is permitted there and nowhere else in Raw Cutter.** Each checkpoint supersedes
  the previous one, and both are this run's own file inside a batch directory that was exclusively
  created (`exist_ok=False`), so there is no other writer whose work could be replaced.
- **It cannot be mistaken for a finished run**: `complete: false`, a non-terminal status, and
  `cancelled` / `fatal_error` / `output_storage_full` forced empty — a run that is still going has
  not been cancelled, has not crashed and has not met a full volume, and carrying a caller's
  in-flight flags into a partial record is how a checkpoint would start asserting an outcome nobody
  measured.
- **Bounded**: `CHECKPOINT_MAX_WRITES = 50` per batch however many sources, through the pure
  deterministic `checkpoint_every` / `should_checkpoint` — no clock, so a batch's cadence is a fact
  about that batch and not about how fast the machine was. The **last** source gets none;
  finalisation follows it immediately with the same information and `complete: true`.
- **Discarded when the final write succeeds, KEPT when it fails.** The failure is the case it exists
  for. `discard_batch_checkpoint` is as narrow as `_discard_staging`: one fixed filename inside this
  batch's own directory, no glob, no sweep.
- **One failure is final.** `checkpointing` is switched off for good — a full volume does not empty
  itself while a batch waits, and one failed write per remaining source is the unbounded retry R4-D
  forbids. A bookkeeping failure never changes a source outcome.

## Testing it

**No drive is ever filled, and none needs to be.** The condition is injected at the seam that
classifies it — FFmpeg's own disk-full stderr, or a real `OSError(ENOSPC)`. Filling a volume would
be slow, machine-specific and would prove nothing the injection does not.

- `disk_guard` is imported and tested as ordinary code.
- `src/raw_cutter_export.py` is reached by AST-extracting its own function bodies into a
  synthesised namespace, with the **real** `shutil`, the real `disk_guard`, real staging writes and
  the real no-replace rename. Only the FFmpeg child is replaced. Adding a name to that module means
  adding it to `_adapter_namespace` in `tests/test_raw_cutter_disk_full.py` and to the `exporter`
  fixture in `tests/test_raw_cutter_naming.py`.
- The handlers are driven through the existing harnesses in `tests/test_raw_cutter_batch.py` and
  `tests/test_cut_handoff.py`. Both AST-extract the **real** `StorageExhausted` rather than
  defining a look-alike: the handlers' correctness is that they name *that* class before their
  generic clause, so a stand-in would pass against a tree where it had been renamed or promoted to
  `BaseException`. Both extractors answer `None` when it is absent instead of asserting, so the
  files still collect against a pre-R4-D tree and each case fails on its own assertion — the same
  reasoning that produced `_OPTIONAL` in `tests/test_settings_pref_consistency.py`.
- **A real-FFmpeg smoke verifies clips by decode, never by exit code.** `rc=0` plus a non-empty MP4
  is explicitly not proof in this repository (FUN R10 Final Frame Fix), so the smoke FFprobes with
  `-count_frames` *and* runs a full decode to null, requiring clean stderr.
- **An oracle that crashes must refuse to report a finding.** Both the reproduction script and the
  export helper raise on `AttributeError` / `TypeError` / `NameError` / `ImportError` rather than
  counting them as the defect reproducing — the first draft of the reproduction reported 4 of 4
  from its own fixture bug. And the reproduction clears its scratch directory first: a leftover
  clip from the previous run is **refused without an encode**, which silently removed two attempts
  from the count and made a clean before/after look like a behaviour change.

## What R4-D is not

It adds no render, progress or notification coordinator; no new activity phase (a storage stop
renders `PHASE_PARTIAL`, which is what it is); no new environment variable; and no change to the
segmentation planner, the scene detector, the shared render mutex, cancellation semantics, the
frame timing, the staging contract or the no-overwrite publication. It grants no cleanup, deletion,
migration or retention authority of any kind.
