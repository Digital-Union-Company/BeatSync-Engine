"""What is physically present in one Stage-5 cache folder, and nothing more.

[FORK] Digital-Union (R14-A stage A3). A bounded, read-only, **record-level** inventory of the
files in one explicitly selected analysis-cache directory. It answers six separate questions about
each file it finds, and it refuses to answer anything it was not given the authority to decide.

The six rungs, each a **separate claim** rather than a step in one verdict:

===  ==========================  =====================================================
 1   ``RECOGNISED_NAME``         the injected ``analysis_cache.is_record_name`` shape rule
 2   ``PARSEABLE``               a bounded read, then "is it a JSON object?"
 3   ``CONTRACT``                the record's own ``cache_contract`` against the injected one
 4   ``TRANSPORT``               the injected ``analysis_cache.looks_reusable``, delegated
 5   ``COMPLETE``                the injected ``video_analysis._cache_entry_is_complete``
 6   ``SOURCE_SCOPE``            A1 containment of the recorded ``video_file`` in a verified root
===  ==========================  =====================================================

**Every authoritative decision is injected, never restated.** That is the whole architectural point
of this module: rung 1 is `analysis_cache.is_record_name`, rung 4 is
`analysis_cache.looks_reusable`, rung 5 is `video_analysis._cache_entry_is_complete`, and the
contract string is a parameter. There is deliberately no local copy of any of them, because a
second completion rule that drifts from the first is precisely how a half-finished analysis gets
reused forever (the defect `_cache_entry_is_complete` was centralised to fix).

**What this module must never do, and does not:**

* it **computes no cache identity**. It does not decode a record filename, call ``_cache_path``,
  ``_video_signature`` or ``_bounded_fingerprint``, and it cannot: those live in `video_analysis.py`
  and this package may not import it. A record filename is an opaque recognised *shape* here. So
  this module answers questions about the files that **are** in the folder, never about which
  sources *ought* to have a record;
* it **writes nothing** — no record, no temporary, no preference, no directory. There is no
  ``os.remove``, ``os.unlink``, ``os.rmdir``, ``rmtree``, ``rename``, ``replace``, ``makedirs`` or
  truncating ``open`` anywhere in this file, and no resolver call that could create one;
* it **opens no media and loads no model**. It reads JSON from recognised record files and nothing
  else — no ``.mp4``, no GGUF, no subprocess, no FFmpeg;
* it **judges no record obsolete**. A different contract generation is *another generation*, and a
  source outside the inspected library is :data:`SCOPE_OUT`, a statement about this inspection's
  reach. A shared cache is read by installations that are not running, so "I cannot see its source"
  is never evidence about the record.

**Why a separate module rather than `read_records` inside `library_inventory.py`,** which is where
the design-of-record placed it: that module's imports are pinned by an existing A1 test to exactly
``{__future__, os, stat, dataclasses}`` (`test_library_inventory.py`, the "stdlib set a1 pinned"
guard), and a record reader needs ``json``. Adding it there would mean amending a safety assertion
that exists to stop exactly that kind of creep. So A3 is its own file and **reuses** A1/A2 through
ordinary intra-package imports — no second resolver, no second scanner, no second containment rule.

Stdlib only (``json``, ``os``) plus A1/A2, so CLAUDE.md's hard rule holds and every branch is
testable on a bare interpreter. No clock and no randomness: the observation window comes from an
injected ``clock`` or is simply absent.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from dataclasses import dataclass, field

from beatsync_fork.library_inventory import (
    _LINK_ALIAS,
    _LINK_PLAIN,
    _LINK_UNDECIDABLE,
    _link_evidence,
    is_within,
)

# ---------------------------------------------------------------------------------------------
# directory-level outcomes
# ---------------------------------------------------------------------------------------------

#: The selected directory was listed and holds at least one entry.
CACHE_PRESENT = "cache_present"
#: Listed successfully and holds nothing. A first run, **not** a fault and not an error.
CACHE_EMPTY = "cache_empty"
#: The directory is configured but is not there. Distinct from empty: one means "no records yet",
#: the other means "this is not the folder you think it is".
CACHE_ABSENT = "cache_absent"
#: It is there as far as we can tell, but we may not look inside.
CACHE_ACCESS_DENIED = "cache_access_denied"
#: The drive, share or mount holding it could not be reached.
CACHE_UNREACHABLE = "cache_unreachable"
#: No directory was identified at all: a blank selection, or a path that would not resolve. This is
#: deliberately NOT the working directory -- ``os.path.abspath("")`` answers the CWD, which has
#: already cost this repository one defect (an empty batch folder cut a whole install), so a blank
#: selection is refused here rather than resolved.
CACHE_UNRESOLVED = "cache_unresolved"

CACHE_STATES = (CACHE_PRESENT, CACHE_EMPTY, CACHE_ABSENT, CACHE_ACCESS_DENIED,
                CACHE_UNREACHABLE, CACHE_UNRESOLVED)

# ---------------------------------------------------------------------------------------------
# rung 2 -- what the bounded read established
# ---------------------------------------------------------------------------------------------

#: Read and parsed as a JSON object.
PARSE_OK = "parse_ok"
#: Valid JSON, but not an object -- an array or a scalar. A real observed shape, not a theoretical
#: one, and it is explicitly not "malformed": the file parsed perfectly and is simply not a record.
PARSE_NOT_OBJECT = "parse_not_object"
#: The bytes are not valid JSON.
PARSE_MALFORMED = "parse_malformed"
#: The file could not be read for a reason that is not absence -- permissions, a device error, a
#: sharing violation.
PARSE_UNREADABLE = "parse_unreadable"
#: It was listed and then was not there. Concurrency, not a defect in the record: a shared cache
#: legitimately has other installations writing into it.
PARSE_VANISHED = "parse_vanished"
#: Larger than the per-record ceiling, so it was **refused rather than read**. No Stage-5 record is
#: this large; reading it anyway is how a byte budget stops meaning anything.
PARSE_TOO_LARGE = "parse_too_large"
#: Never attempted -- a bound was reached or the caller asked to stop. Not zero, not failed.
PARSE_NOT_READ = "parse_not_read"

PARSE_STATUSES = (PARSE_OK, PARSE_NOT_OBJECT, PARSE_MALFORMED, PARSE_UNREADABLE, PARSE_VANISHED,
                  PARSE_TOO_LARGE, PARSE_NOT_READ)

# ---------------------------------------------------------------------------------------------
# rung 3 -- contract generation
# ---------------------------------------------------------------------------------------------

#: The record's ``cache_contract`` equals the contract the caller is running.
CONTRACT_CURRENT = "contract_current"
#: It records a different generation. **Another generation, not an obsolete file** -- another
#: installation may be running it, and nothing here may call it dead weight.
CONTRACT_OTHER = "contract_other"
#: No usable ``cache_contract`` field: written before the marker existed, or hand-edited.
CONTRACT_ABSENT = "contract_absent"

# ---------------------------------------------------------------------------------------------
# rung 5 -- completion, delegated
# ---------------------------------------------------------------------------------------------

#: The injected completion authority accepted the payload **for the stated mode**.
COMPLETE_YES = "complete"
#: It declined the payload for that mode. Note what this is not: a reason. The authority answers a
#: bool and this module does not reconstruct why, because reconstructing it would be a second rule.
COMPLETE_NO = "incomplete"

# ---------------------------------------------------------------------------------------------
# rung 4 -- transport, delegated
# ---------------------------------------------------------------------------------------------

#: The injected transport rule accepted the file.
TRANSPORT_OK = "transport_ok"
#: The injected transport rule declined it, and its own reason is carried verbatim.
TRANSPORT_REFUSED = "transport_refused"
#: The delegation could not be performed against the same bytes this pass parsed -- the file
#: changed size, vanished, or the budget could not cover the helper's own read. **Not** a verdict
#: about the record: an observation about the folder moving underneath us.
TRANSPORT_UNAVAILABLE = "transport_unavailable"

# ---------------------------------------------------------------------------------------------
# rung 6 -- source scope
# ---------------------------------------------------------------------------------------------

#: The recorded source resolves inside one of the supplied, verified inspection roots.
SCOPE_IN = "scope_in"
#: It resolves outside every supplied root. A statement about this inspection's **reach**; never
#: "missing", never "obsolete", never "deletable".
SCOPE_OUT = "scope_out"
#: The recorded path could not be resolved safely, or no usable path was recorded. A hold.
SCOPE_UNKNOWN = "scope_unknown"

#: Used by rungs 3-6 alike: the question was not put, so there is no answer to report. It is a
#: first-class value rather than a silent ``False``, because "no records are complete" and "nobody
#: told me which mode to judge completeness for" are different facts.
NOT_ASSESSED = "not_assessed"

# ---------------------------------------------------------------------------------------------
# why the pass stopped being a total
# ---------------------------------------------------------------------------------------------

BOUND_NONE = ""
BOUND_ENTRIES = "entries"
BOUND_RECORDS = "records"
BOUND_TOTAL_BYTES = "total_bytes"
BOUND_NAMES = "names"
BOUND_STOPPED = "stopped_by_request"
BOUND_DIRECTORY = "directory_not_listed"

BOUNDS = (BOUND_NONE, BOUND_ENTRIES, BOUND_RECORDS, BOUND_TOTAL_BYTES, BOUND_NAMES,
          BOUND_STOPPED, BOUND_DIRECTORY)

# ---------------------------------------------------------------------------------------------
# the ceilings, and what each one actually bounds
# ---------------------------------------------------------------------------------------------

#: Directory entries **classified**. Set to A2's ``MAX_DIAGNOSTIC_ENTRIES`` so the two passes over
#: the same kind of folder cannot disagree about what "too many" means. A Stage-5 cache folder also
#: holds the retained Qwen request/response JSON (deliberately kept for debugging) and the per-job
#: partials, so entries run at roughly three times the record count; 50 000 covers a cache several
#: times larger than the production store this feature was asked for.
MAX_CACHE_ENTRIES = 50_000

#: Recognised records **attempted**, successes and failures alike.
MAX_CACHE_RECORDS = 20_000

#: One record's maximum size, in bytes. A record is one ``candidates`` list; a candidate is ~30
#: numeric fields plus the source's absolute path and its tags, so ~1.5 KB serialised at the top of
#: the range. The largest records this repository has documented carry 398 and 525 candidates
#: (`video_analysis._stored_ai_cache_is_consistent`'s own docstring), i.e. well under 1 MB. 4 MB is
#: therefore several times the largest real record, and a file beyond it is refused unread rather
#: than being allowed to spend the whole byte budget.
MAX_RECORD_BYTES = 4_000_000

#: Total JSON bytes this pass may read, across every record **and** every delegated transport
#: re-read. Chosen so the realistic production store (thousands of records at a median far below
#: the per-record ceiling) is a *complete* pass rather than a truncated one, while a folder that is
#: not a cache at all cannot be read without limit.
MAX_TOTAL_RECORD_BYTES = 1_000_000_000

#: Problem names reported **per category**, matching A2's ``MAX_DIAGNOSTIC_NAMES``. Note what this
#: is: a cap on names *listed*, not on entries classified -- the counts stay authoritative when it
#: is reached, and ``names_truncated`` says so.
MAX_REPORTED_RECORD_NAMES = 200


@dataclass(frozen=True)
class CacheRecord:
    """One recognised record file, with each rung recorded **separately**.

    Six independent fields rather than one verdict, because collapsing them is how a report starts
    saying "obsolete" about a file whose source simply lives in another library.
    """

    name: str = ""
    size: int = 0
    parse: str = PARSE_NOT_READ
    contract: str = NOT_ASSESSED
    transport: str = NOT_ASSESSED
    complete: str = NOT_ASSESSED
    scope: str = NOT_ASSESSED
    recorded_contract: str = ""
    source_path: str = ""
    scope_root: str = ""
    detail: str = ""

    @property
    def parsed(self) -> bool:
        return self.parse == PARSE_OK


@dataclass(frozen=True)
class RecordInventory:
    """What one cache folder holds, as observed, with its own truthfulness attached.

    :attr:`complete` is the load-bearing field. When it is ``False`` **no count here is a total** --
    they are observations of the part of the folder this pass reached, and :attr:`bound` names what
    stopped it.
    """

    path: str = ""
    resolved: str = ""
    state: str = CACHE_UNRESOLVED
    complete: bool = False
    bound: str = BOUND_NONE
    detail: str = ""

    # what the listing contained
    entries_listed: int = 0
    entries_examined: int = 0
    other_files: int = 0
    other_bytes: int = 0

    # rung 1
    records_recognised: int = 0
    records_attempted: int = 0

    # rung 2
    parse_counts: dict = field(default_factory=dict)
    #: Bytes charged against the total-byte budget: the observed size of every record this pass
    #: opened or attempted to open, **plus** the delegated transport re-read of the same file. Named
    #: for what it is rather than "bytes_read", because a file that vanished or was refused mid-read
    #: is still charged -- the budget's safety property is that it is never under-charged.
    bytes_charged: int = 0

    # rung 3
    contract_counts: dict = field(default_factory=dict)
    contract_assessed: bool = False

    # rung 4
    transport_counts: dict = field(default_factory=dict)
    transport_assessed: bool = False

    # rung 5
    complete_counts: dict = field(default_factory=dict)
    completeness_assessed: bool = False
    analysis_mode: str = ""

    # rung 6
    scope_counts: dict = field(default_factory=dict)
    scope_assessed: bool = False
    inspection_roots: tuple = ()

    # containment refusals -- never read, by construction
    skipped_aliases: tuple = ()
    skipped_escapes: tuple = ()
    skipped_undecidable: tuple = ()
    skipped_entries: int = 0

    # instability observed while reading
    vanished: tuple = ()
    changed: tuple = ()

    # problem names, per category, each capped
    malformed: tuple = ()
    unreadable: tuple = ()
    oversize: tuple = ()
    names_truncated: bool = False

    records: tuple = ()
    records_truncated: bool = False

    observed_from: str = ""
    observed_to: str = ""

    @property
    def records_parsed(self) -> int:
        return int(self.parse_counts.get(PARSE_OK, 0))

    @property
    def complete_records(self) -> int | None:
        """How many records are complete, or ``None`` when that was never assessed.

        ``None`` rather than ``0`` is deliberate and is the same discipline A2's ``totals`` uses: a
        figure that was not established must not be readable as a measurement of zero.
        """
        if not self.completeness_assessed:
            return None
        return int(self.complete_counts.get(COMPLETE_YES, 0))

    @property
    def authoritative(self) -> bool:
        """May these counts be published as totals for this folder? Only a complete pass may."""
        return bool(self.complete and self.state in (CACHE_PRESENT, CACHE_EMPTY))

    @property
    def unstable(self) -> bool:
        """Did the folder demonstrably move underneath this pass?"""
        return bool(self.vanished or self.changed)


def _positive_int(value, name: str) -> int:
    """A ceiling must be a real positive integer, and ``True`` is not one.

    ``bool`` is rejected explicitly because it is a subclass of ``int``: ``max_records=True`` would
    silently mean "one record" and the pass would report a one-record folder as a bound being hit.
    A2's review raised exactly this, so the guard is stated the same way here.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, not {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be at least 1, not {value}")
    return value


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bump(counter: dict, key) -> None:
    counter[key] = counter.get(key, 0) + 1


def _state_for_listing_error(exc) -> tuple:
    """``(state, detail)`` for a failed listing of the selected directory itself.

    Classified by exception **type**, not by ``errno`` or ``winerror``: A1 measured that all three
    absent cases answer ``errno=2``, and a winerror allow-list has already failed in this
    repository once. Anything this function cannot positively classify is
    :data:`CACHE_UNREACHABLE`, which is a hold rather than a finding about the folder's contents.
    """
    detail = f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}"
    if isinstance(exc, PermissionError):
        return CACHE_ACCESS_DENIED, detail
    if isinstance(exc, FileNotFoundError):
        return CACHE_ABSENT, detail
    if isinstance(exc, NotADirectoryError):
        return CACHE_UNRESOLVED, detail
    return CACHE_UNREACHABLE, detail


def _resolve(path: str, realpath) -> str:
    try:
        resolved = realpath(path)
    except (OSError, ValueError):
        return ""
    return resolved if isinstance(resolved, str) else ""


def _root_keys(inspection_roots, realpath) -> tuple:
    """The resolved identity of each supplied root, in A1's ``root_key`` form.

    Accepts A1 ``RootState`` objects -- the normal caller -- or plain strings, and takes the
    **resolved** location in both cases. Resolving before deriving identity is A1's F-1 lesson: a
    root selected as ``E:\\LibraryLink`` may be a junction onto ``D:\\ActualLibrary``, and a scope
    answer about the typed path would be an answer about the wrong storage.

    A root with no establishable resolved location does not participate: it cannot contain anything
    we could prove, so folding it in would manufacture a scope verdict out of nothing.
    """
    keys: list = []
    for item in inspection_roots or ():
        key = _clean(getattr(item, "root_key", ""))
        if not key:
            real = _clean(getattr(item, "real", "")) or (item if isinstance(item, str) else "")
            real = _clean(real)
            if real:
                resolved = _resolve(real, realpath)
                key = resolved or real
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _classify_scope(payload, root_keys, realpath) -> tuple:
    """Rung 6: ``(verdict, source_path, matching_root)``. Resolves a path; **stats nothing**.

    This is a containment statement and only ever that. It does not ask whether the source exists,
    and must never be read as doing so -- an out-of-scope record is not a missing one, and A3 is
    explicitly forbidden from checking. Source *state* is a later stage's business.
    """
    recorded = payload.get("video_file") if isinstance(payload, dict) else None
    source = recorded.strip() if isinstance(recorded, str) else ""
    if not root_keys:
        # No root was supplied, so "outside the library" is not a fact anybody established. This
        # ordering is deliberate: it answers NOT_ASSESSED even for a record with no usable path,
        # because the question was never put.
        return NOT_ASSESSED, source, ""
    if not source:
        return SCOPE_UNKNOWN, "", ""
    resolved = _resolve(source, realpath)
    if not resolved:
        return SCOPE_UNKNOWN, source, ""
    for key in root_keys:
        try:
            inside = is_within(resolved, key)
        except Exception:
            inside = False
        if inside:
            return SCOPE_IN, source, key
    return SCOPE_OUT, source, ""


def read_records(cache_dir, *, is_record_name, contract=None, is_complete=None, require_ai=None,
                 looks_reusable=None, inspection_roots=(), listdir=os.listdir, lstat=os.lstat,
                 realpath=os.path.realpath, read_text=None, should_stop=None, clock=None,
                 max_entries=MAX_CACHE_ENTRIES, max_records=MAX_CACHE_RECORDS,
                 max_record_bytes=MAX_RECORD_BYTES, max_total_bytes=MAX_TOTAL_RECORD_BYTES,
                 max_reported_names=MAX_REPORTED_RECORD_NAMES) -> RecordInventory:
    """Inspect ONE explicitly selected cache directory. Rungs 1-6, bounded, read-only.

    ``cache_dir`` is **handed in**. This function discovers nothing: it does not read the shared
    configuration, does not call ``analysis_cache.resolve_location`` (which creates the folder it is
    asked about -- the measured hazard the design-of-record opens with), and does not fall back to
    the working directory when handed a blank string.

    Every authority is a parameter:

    ``is_record_name``
        **Required.** `analysis_cache.is_record_name`. There is no local shape rule to fall back
        to, which is why it has no default.
    ``contract``
        `video_analysis.CACHE_CONTRACT_VERSION`. ``None`` leaves rung 3 unassessed.
    ``is_complete`` / ``require_ai``
        `video_analysis._cache_entry_is_complete` and the mode to judge for. **Both** are needed:
        a completeness figure for an unestablished mode would be a figure for a question nobody
        asked, so ``require_ai=None`` leaves rung 5 unassessed even when the callable is supplied.
    ``looks_reusable``
        `analysis_cache.looks_reusable`. Optional, and off by default because it **re-reads the
        file from the path** -- that second read is charged to the same byte budget, so enabling it
        roughly halves the number of records a given budget covers.
    ``inspection_roots``
        A1 ``RootState`` objects (or plain paths). Empty leaves rung 6 unassessed: with no root,
        "outside the library" is not a fact anyone established.

    The bounds charge **work attempted**, not results obtained: a refused, vanished, malformed or
    oversize file costs exactly what a good one costs. They are validated before any filesystem
    call, so a bad ceiling cannot be discovered half way through a folder.

    **What the bounds are not.** ``listdir`` materialises the whole directory before any per-entry
    ceiling can apply, so the floor cost of a pass is the size of one listing regardless of
    ``max_entries``. Neither this nor any other ceiling here is a wall-clock or peak-memory bound,
    and no figure derived from them claims to be.
    """
    max_entries = _positive_int(max_entries, "max_entries")
    max_records = _positive_int(max_records, "max_records")
    max_record_bytes = _positive_int(max_record_bytes, "max_record_bytes")
    max_total_bytes = _positive_int(max_total_bytes, "max_total_bytes")
    max_reported_names = _positive_int(max_reported_names, "max_reported_names")
    if not callable(is_record_name):
        raise TypeError("is_record_name must be callable -- A3 defines no local record-shape rule")

    def _now() -> str:
        if clock is None:
            return ""
        try:
            return str(clock())
        except Exception:
            return ""

    started = _now()
    target = _clean(cache_dir)
    root_keys = _root_keys(inspection_roots, realpath)
    assess_contract = isinstance(contract, str) and bool(contract)
    assess_complete = callable(is_complete) and require_ai is not None
    # The mode label is part of the completeness figure, not a separate fact: naming a mode while
    # answering nothing for it would report a question as though it had an answer. So it is stated
    # only when rung 5 was actually assessed -- a mode supplied without an authority (or an
    # authority without a mode) leaves both the figure and its label absent together.
    mode = ("require_ai" if require_ai else "no_ai") if assess_complete else ""
    assess_transport = callable(looks_reusable) and assess_contract
    assess_scope = bool(root_keys)

    def finish(**kwargs) -> RecordInventory:
        kwargs.setdefault("path", target)
        kwargs.setdefault("observed_from", started)
        kwargs["observed_to"] = _now()
        kwargs.setdefault("contract_assessed", assess_contract)
        kwargs.setdefault("transport_assessed", assess_transport)
        kwargs.setdefault("completeness_assessed", assess_complete)
        kwargs.setdefault("analysis_mode", mode)
        kwargs.setdefault("scope_assessed", assess_scope)
        kwargs.setdefault("inspection_roots", root_keys)
        return RecordInventory(**kwargs)

    if not target:
        # A blank selection is refused, never resolved. `os.path.abspath("")` is the working
        # directory, and inspecting the CWD because a field was empty is a defect this repository
        # has already shipped once.
        return finish(state=CACHE_UNRESOLVED, complete=False, bound=BOUND_DIRECTORY,
                      detail="No cache folder was selected.")

    resolved = _resolve(target, realpath)
    if not resolved:
        return finish(state=CACHE_UNRESOLVED, complete=False, bound=BOUND_DIRECTORY,
                      detail=f"The path could not be resolved: {target}")

    try:
        entries = listdir(target)
    except OSError as exc:
        state, detail = _state_for_listing_error(exc)
        return finish(resolved=resolved, state=state, complete=False, bound=BOUND_DIRECTORY,
                      detail=detail)

    names = sorted(str(name) for name in entries)
    if not names:
        # Reachable and empty: a complete observation of a folder with nothing in it. This is the
        # one empty answer that IS a total.
        return finish(resolved=resolved, state=CACHE_EMPTY, complete=True, bound=BOUND_NONE,
                      entries_listed=0, entries_examined=0)

    parse_counts: dict = {}
    contract_counts: dict = {}
    transport_counts: dict = {}
    complete_counts: dict = {}
    scope_counts: dict = {}
    aliases: list = []
    escapes: list = []
    undecidable: list = []
    vanished: list = []
    changed: list = []
    malformed: list = []
    unreadable: list = []
    oversize: list = []
    details: list = []

    entries_examined = 0
    records_recognised = 0
    records_attempted = 0
    other_files = other_bytes = 0
    skipped_entries = 0
    bytes_charged = 0
    names_truncated = False
    records_truncated = False
    bound = BOUND_NONE

    def note(bucket: list, value: str) -> None:
        nonlocal names_truncated
        if len(bucket) < max_reported_names:
            bucket.append(value)
        else:
            names_truncated = True

    def reader(path: str) -> str:
        if read_text is not None:
            return read_text(path)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    for name in names:
        if should_stop is not None and should_stop():
            bound = BOUND_STOPPED
            break
        if entries_examined >= max_entries:
            bound = BOUND_ENTRIES
            break
        entries_examined += 1

        full = os.path.join(target, name)

        # Containment, before anything is opened. Two independent halves, and both are necessary:
        # link-level evidence that the entry is an ordinary file (A2's F-3 gate -- containment alone
        # cannot tell a *contained* alias from a real entry), and resolved-path containment inside
        # the selected folder. An explicitly selected junction root is fine: `resolved` is its
        # target, so entries beneath it resolve inside `resolved` and are read normally.
        evidence = _link_evidence(full, lstat=lstat)
        if evidence == _LINK_ALIAS:
            note(aliases, name)
            skipped_entries += 1
            continue
        if evidence != _LINK_PLAIN:
            # Fail closed on anything that is not positively an ordinary entry -- today that is
            # :data:`_LINK_UNDECIDABLE`, and a fourth value added upstream would land here too
            # rather than being read by default. Unreadable link metadata is not evidence of an
            # ordinary file, and an alias read by mistake is a read outside the folder we were
            # given.
            note(undecidable, name)
            skipped_entries += 1
            continue

        entry_real = _resolve(full, realpath)
        if not entry_real or not is_within(entry_real, resolved):
            note(escapes, name)
            skipped_entries += 1
            continue

        try:
            info = lstat(full)
            size = int(getattr(info, "st_size", 0))
            is_file = bool(stat_module.S_ISREG(getattr(info, "st_mode", 0)))
        except FileNotFoundError:
            # Listed and then gone. Instability, reported as such -- never silently dropped, and
            # never counted as a record that failed.
            note(vanished, name)
            skipped_entries += 1
            continue
        except OSError as exc:
            note(unreadable, f"{name}: {type(exc).__name__}")
            skipped_entries += 1
            continue

        if not is_file:
            # A Stage-5 cache has no subdirectories. One that appears is counted, not entered:
            # there is no recursion in this module at all.
            skipped_entries += 1
            continue

        if not is_record_name(name):
            # Everything else in the folder: the retained Qwen request/response JSON, the per-job
            # partials, anything unrecognised. Counted by metadata only -- never opened, never
            # parsed as a record, and never labelled orphaned or obsolete on the strength of a
            # filename. They are kept deliberately, for debugging.
            other_files += 1
            other_bytes += size
            continue

        records_recognised += 1

        if records_attempted >= max_records:
            # No PARSE_NOT_READ count is recorded here on purpose: one more unread record would be
            # a figure, and the number actually left unread is unknown because the loop stops
            # classifying at the same moment. `bound` carries that truth instead.
            bound = BOUND_RECORDS
            break
        if size > max_record_bytes:
            records_attempted += 1
            _bump(parse_counts, PARSE_TOO_LARGE)
            note(oversize, name)
            if len(details) < max_reported_names:
                details.append(CacheRecord(
                    name=name, size=size, parse=PARSE_TOO_LARGE,
                    detail=f"larger than the {max_record_bytes}-byte ceiling"))
            else:
                records_truncated = True
            continue
        if bytes_charged + size > max_total_bytes:
            bound = BOUND_TOTAL_BYTES
            break

        records_attempted += 1
        payload = None
        status = PARSE_OK
        detail = ""
        # Charged BEFORE the read, so a read that fails part way through is not free. The budget's
        # safety property is that it is never under-charged; over-charging a file that turned out
        # to be unreadable is the harmless direction, and :attr:`bytes_charged` is named for what
        # it actually is rather than implying every charged byte was successfully parsed.
        bytes_charged += size
        try:
            raw = reader(full)
            payload = json.loads(raw)
        except FileNotFoundError as exc:
            status, detail = PARSE_VANISHED, f"{type(exc).__name__}"
            note(vanished, name)
        except OSError as exc:
            status = PARSE_UNREADABLE
            detail = f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}"
            note(unreadable, name)
        except ValueError as exc:
            status, detail = PARSE_MALFORMED, f"{type(exc).__name__}"
            note(malformed, name)
        else:
            if not isinstance(payload, dict):
                status = PARSE_NOT_OBJECT
                detail = f"JSON {type(payload).__name__}, not an object"
                payload = None
                note(malformed, name)
        _bump(parse_counts, status)

        record_contract = NOT_ASSESSED
        record_transport = NOT_ASSESSED
        record_complete = NOT_ASSESSED
        record_scope = NOT_ASSESSED
        recorded_contract = ""
        source_path = ""
        scope_root = ""

        if payload is not None:
            stored = payload.get("cache_contract")
            recorded_contract = stored if isinstance(stored, str) else ""
            if assess_contract:
                if not recorded_contract:
                    record_contract = CONTRACT_ABSENT
                elif recorded_contract == contract:
                    record_contract = CONTRACT_CURRENT
                else:
                    record_contract = CONTRACT_OTHER
                _bump(contract_counts, record_contract)

            if assess_transport:
                # Delegated, never restated. The helper reads the file from the path again, so the
                # second read is charged here too -- and because it is a *separate* read it may see
                # different bytes. A size that moved, or a budget that cannot cover the re-read, is
                # reported as TRANSPORT_UNAVAILABLE: an observation about the folder, not a verdict
                # about the record.
                if bytes_charged + size > max_total_bytes:
                    record_transport = TRANSPORT_UNAVAILABLE
                    detail = detail or "transport check skipped: byte budget exhausted"
                else:
                    try:
                        current = int(getattr(lstat(full), "st_size", -1))
                    except OSError:
                        current = -1
                    if current != size:
                        record_transport = TRANSPORT_UNAVAILABLE
                        note(changed, name)
                        detail = detail or "the file changed size during inspection"
                    else:
                        bytes_charged += size
                        try:
                            ok, reason = looks_reusable(full, contract)
                        except Exception as exc:
                            record_transport = TRANSPORT_UNAVAILABLE
                            detail = detail or f"transport check failed: {type(exc).__name__}"
                        else:
                            if ok:
                                record_transport = TRANSPORT_OK
                            else:
                                # A refusal is taken at face value UNLESS the file demonstrably
                                # moved between this pass's parse and the helper's own read. The
                                # discriminator is a fresh observation of the file, never a match
                                # on the helper's wording: its reason strings are its own to
                                # change, and a reader that branched on them would break silently
                                # when they did.
                                try:
                                    after = int(getattr(lstat(full), "st_size", -1))
                                except OSError:
                                    after = -1
                                if after != size:
                                    record_transport = TRANSPORT_UNAVAILABLE
                                    note(changed, name)
                                    detail = detail or (
                                        "the file changed while the transport check read it")
                                else:
                                    record_transport = TRANSPORT_REFUSED
                                    detail = detail or str(reason)
                _bump(transport_counts, record_transport)

            if assess_complete:
                try:
                    verdict = bool(is_complete(payload, require_ai))
                except Exception as exc:
                    # The one authority refused to answer. That is NOT_ASSESSED, never
                    # "incomplete": an exception is the absence of a verdict, and recording it as
                    # one would attribute a decision to an authority that did not make it.
                    record_complete = NOT_ASSESSED
                    detail = detail or f"completion authority raised {type(exc).__name__}"
                else:
                    record_complete = COMPLETE_YES if verdict else COMPLETE_NO
                    _bump(complete_counts, record_complete)

            record_scope, source_path, scope_root = _classify_scope(payload, root_keys, realpath)
            if assess_scope:
                _bump(scope_counts, record_scope)

        if len(details) < max_reported_names:
            details.append(CacheRecord(
                name=name, size=size, parse=status, contract=record_contract,
                transport=record_transport, complete=record_complete, scope=record_scope,
                recorded_contract=recorded_contract, source_path=source_path,
                scope_root=scope_root, detail=detail))
        else:
            records_truncated = True

    # Truncating the reported names means the pass can no longer show its work in full, so the
    # result is not a complete observation even if every entry was classified. A2's mutant C6
    # survived precisely because this term was missing there; it is asserted directly below.
    if bound == BOUND_NONE and names_truncated:
        bound = BOUND_NAMES

    complete_pass = bound == BOUND_NONE and not names_truncated

    return finish(
        resolved=resolved, state=CACHE_PRESENT, complete=complete_pass, bound=bound,
        entries_listed=len(names), entries_examined=entries_examined,
        other_files=other_files, other_bytes=other_bytes,
        records_recognised=records_recognised, records_attempted=records_attempted,
        parse_counts=parse_counts, bytes_charged=bytes_charged,
        contract_counts=contract_counts, transport_counts=transport_counts,
        complete_counts=complete_counts, scope_counts=scope_counts,
        skipped_aliases=tuple(aliases), skipped_escapes=tuple(escapes),
        skipped_undecidable=tuple(undecidable), skipped_entries=skipped_entries,
        vanished=tuple(vanished), changed=tuple(changed),
        malformed=tuple(malformed), unreadable=tuple(unreadable), oversize=tuple(oversize),
        names_truncated=names_truncated, records=tuple(details),
        records_truncated=records_truncated)


__all__ = [
    "BOUNDS", "BOUND_DIRECTORY", "BOUND_ENTRIES", "BOUND_NAMES", "BOUND_NONE", "BOUND_RECORDS",
    "BOUND_STOPPED", "BOUND_TOTAL_BYTES",
    "CACHE_ABSENT", "CACHE_ACCESS_DENIED", "CACHE_EMPTY", "CACHE_PRESENT", "CACHE_STATES",
    "CACHE_UNREACHABLE", "CACHE_UNRESOLVED",
    "COMPLETE_NO", "COMPLETE_YES",
    "CONTRACT_ABSENT", "CONTRACT_CURRENT", "CONTRACT_OTHER",
    "CacheRecord", "RecordInventory",
    "MAX_CACHE_ENTRIES", "MAX_CACHE_RECORDS", "MAX_RECORD_BYTES", "MAX_REPORTED_RECORD_NAMES",
    "MAX_TOTAL_RECORD_BYTES",
    "NOT_ASSESSED",
    "PARSE_MALFORMED", "PARSE_NOT_OBJECT", "PARSE_NOT_READ", "PARSE_OK", "PARSE_STATUSES",
    "PARSE_TOO_LARGE", "PARSE_UNREADABLE", "PARSE_VANISHED",
    "SCOPE_IN", "SCOPE_OUT", "SCOPE_UNKNOWN",
    "TRANSPORT_OK", "TRANSPORT_REFUSED", "TRANSPORT_UNAVAILABLE",
    "read_records",
]
