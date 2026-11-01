"""R11: the record that connects a finished Raw Cutter job to Video Review.

The workflow the owner asked for is one line: **cut video → finish → review the exported clips**.
The navigation half already shipped in R8 — one button selects Media Library → Video Review, fills
in the folder box and deliberately scans nothing. What was missing is *which* folder arrives: the
button read the parent output widget, because the Raw Cutter kept no run-directory state. This
module is that state, and it is deliberately a **pure record** rather than a handler:

```
{'directory': str, 'recursive': bool, 'clips': int, 'mode': 'single'|'batch', 'outcome': str}
```

Three properties are structural rather than enforced by the caller remembering to check:

* **:func:`build` answers ``None`` unless clips actually reached disk.** Not "unless the job
  succeeded" — a cancelled or partially failed job that wrote four clips is worth reviewing, and a
  *successful* job that wrote none is not. ``clips`` therefore has to be a count of **published**
  clips; passing a planned count would make the one thing this gate exists to prevent (offering
  review of an empty folder) unreachable from the outside and reachable from the inside.
* **:func:`recursive_for` is a function of the job's shape, not an operator preference.** A batch
  writes one subfolder per source, so subfolders are included; a single-video run writes its clips
  directly into one folder and does not need them.
* **:func:`read` validates anything coming back from session state.** A record is a plain dict of
  primitives that has been round-tripped through the UI layer, so it is untrusted input on the way
  back in: a malformed, truncated or foreign value reads as "no handoff", never as a directory.

Nothing here touches the filesystem — not a ``stat``, not a listing. Whether the recorded directory
still exists is a question for the caller at the moment the operator asks, and it is deliberately
asked *then* rather than cached here, because a record is a statement about a job that finished and
not a promise about a disk that may have changed since. Stdlib only, so the fork package's
no-runtime-import rule still holds.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: A single-video run: its clips land directly in one run directory.
MODE_SINGLE = "single"
#: A folder batch: one subfolder per source underneath the batch directory.
MODE_BATCH = "batch"

#: Every planned clip was written.
OUTCOME_COMPLETED = "completed"
#: Some clips were written and some were not — a failed clip, or a source the batch could not use.
OUTCOME_PARTIAL = "partial"
#: The operator stopped the job after some clips had already reached disk.
OUTCOME_CANCELLED = "cancelled"

_MODES = (MODE_SINGLE, MODE_BATCH)
_OUTCOMES = (OUTCOME_COMPLETED, OUTCOME_PARTIAL, OUTCOME_CANCELLED)

#: Recursion follows the job's shape. Stated as a table rather than a conditional so that adding a
#: third job shape has to make a decision here instead of inheriting a boolean by accident.
_RECURSIVE = {MODE_SINGLE: False, MODE_BATCH: True}

#: What the report says about each outcome. The wording is the point of the distinction: a stopped
#: job must not be described as finished, and a job with failures must not be described as clean.
_SUMMARY = {
    OUTCOME_COMPLETED: "{clips} clip(s) written",
    OUTCOME_PARTIAL: "{clips} clip(s) written, and some did not finish",
    OUTCOME_CANCELLED: "{clips} clip(s) written before you stopped the job",
}


def recursive_for(mode) -> bool:
    """Whether Video Review should include subfolders for a job of this shape.

    An unrecognised mode answers ``False``, which is the conservative direction: it reviews exactly
    the named folder instead of silently walking a tree the operator did not ask about.
    """
    return bool(_RECURSIVE.get(mode, False))


def build(directory, *, mode, clips, outcome) -> Optional[Dict[str, Any]]:
    """The handoff record for a finished job, or ``None`` when there is nothing to review.

    ``clips`` **must** be the number of clips that were published, counted from the export's own
    completion evidence. ``None`` is returned — meaning "offer nothing" — whenever:

    * the job never obtained an output directory, so there is no folder to name;
    * no clip reached disk, whatever the job's outcome was;
    * the mode or outcome is not one this module knows, which is a programming error and must fail
      closed rather than produce a record with an unprintable verdict.

    Returning ``None`` rather than raising is deliberate: this is called from the terminal frame of
    a generator that has already done the operator's work, and an exception there would replace a
    truthful report about a finished job with a crash.
    """
    if not isinstance(directory, str):
        return None
    folder = directory.strip()
    if not folder:
        return None
    if mode not in _MODES or outcome not in _OUTCOMES:
        return None
    try:
        published = int(clips)
    except (TypeError, ValueError):
        return None
    if published < 1:
        return None
    return {
        "directory": folder,
        "recursive": recursive_for(mode),
        "clips": published,
        "mode": mode,
        "outcome": outcome,
    }


def read(record) -> Optional[Dict[str, Any]]:
    """Validate a record handed back from session state; ``None`` if it cannot be trusted.

    Rebuilt through :func:`build` rather than checked field by field, so a stored record and a
    fresh one are subject to exactly one set of rules — including the published-clip gate. A
    record whose ``recursive`` had been tampered with is therefore corrected to the value its mode
    implies rather than honoured.
    """
    if not isinstance(record, dict):
        return None
    return build(record.get("directory"), mode=record.get("mode"),
                 clips=record.get("clips"), outcome=record.get("outcome"))


def is_offer(record) -> bool:
    """Whether there is a reviewable job to offer. The Review button's availability."""
    return read(record) is not None


def summary(record) -> str:
    """One truthful clause describing what the job left on disk, for the cutter's own report."""
    valid = read(record)
    if valid is None:
        return ""
    return _SUMMARY[valid["outcome"]].format(clips=valid["clips"])


def offer_text(record, action: str) -> str:
    """The line the cutter's report adds when review is available.

    `action` is the button's own label, passed in rather than imported, so the prompt cannot drift
    from the control it names and this module stays free of UI content.
    """
    described = summary(record)
    if not described:
        return ""
    return f"▶ {described} — press “{action}” to review them."


def loaded_text(record) -> str:
    """What Video Review's report says once the folder has been filled in.

    It states the three facts the operator needs and then stops: which folder, whether subfolders
    are included, and that scanning is still their own deliberate action. It never claims anything
    was scanned, moved or read — because nothing was.
    """
    valid = read(record)
    if valid is None:
        return ""
    scope = ("Subfolders are included, because each source in the batch has its own folder."
             if valid["recursive"] else
             "Subfolders are excluded: this job wrote its clips into that one folder.")
    return ("Loaded the clips from your finished cut into Media Library → Video Review:\n"
            f"{valid['directory']}\n"
            f"{summary(record)}.\n"
            f"{scope}\n"
            "Press Scan Folder there when you are ready to review.")
