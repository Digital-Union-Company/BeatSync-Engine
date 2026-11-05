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
the design-of-record placed it: an existing A1 test constrains that module's imports to a **subset**
of ``{__future__, os, stat, dataclasses}`` (`test_library_inventory.py`,
``test_j_the_module_imports_the_standard_library_only``, whose assertion is ``imported <= {…}``, not
an exact pin), and a record reader needs ``json`` — which is not in that allowed set. Adding it there
would mean amending a safety assertion that exists to stop exactly that kind of creep. So A3 is its
own file and **reuses** A1/A2 through ordinary intra-package imports — no second resolver, no second
scanner, no second containment rule.

Stdlib only (``json``, ``os``) plus A1/A2, so CLAUDE.md's hard rule holds and every branch is
testable on a bare interpreter. No clock and no randomness: the observation window comes from an
injected ``clock`` or is simply absent.

----

**What this module does NOT guarantee.** Stated here because a read-only inventory whose report
overclaims is worse than one that reports less. Each of these was independently reproduced, and each
is now bounded and disclosed rather than hidden.

* **Record identity is stat identity, never content equivalence.** Rung 4 detects a file that moved
  underneath the pass by comparing ``(st_size, st_ino, st_dev)`` captured at the size gate — measured
  40/40 against the only writer this product has (`video_analysis._save_cache` publishes through a
  unique temp file and ``os.replace``, so the inode always changes), versus 5/40 for
  ``st_mtime_ns``, which is why mtime is deliberately not the discriminator. It is **not** proof of
  content equality: a same-size rewrite performed **in place** — a hand-edit; no writer in this
  product does that — changes neither size nor inode and is undetectable here. A known residual, not
  a claim.
* **The per-record ceiling is enforced on the bytes this pass consumed**, so a record that grows after
  its size gate is refused rather than parsed, and never parsed as a truncated prefix. The *read* is
  bounded in **characters** (``max_record_bytes + 1``), because the single ``open`` here is text mode;
  worst-case I/O is therefore up to four bytes per character under UTF-8. The *verdict* is exact
  regardless: the ceiling is compared against the consumed text's encoded byte length.
* **The delegated transport re-read cannot be bounded from here at all.** `analysis_cache.looks_reusable`
  reopens the path itself, and changing that is outside this module's contract. Its cost is charged as
  a **stat-time estimate**, a growing file can exceed that estimate during delegation, and the identity
  re-observation afterwards can only *report* the change — it cannot retract bytes already read.
* **Rung 6 is lexical containment inside a positively observed root.** It never stats a source, so it
  says nothing whatsoever about whether that source exists.
* ``listdir`` materialises one whole directory before any per-entry ceiling can apply, so the floor
  cost of a pass is the size of that listing. No figure here is a wall-clock or peak-memory bound.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from dataclasses import dataclass, field

from beatsync_fork.library_inventory import (
    ACCESS_DENIED,
    PRESENT,
    _LINK_ALIAS,
    _LINK_PLAIN,
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

#: The recorded source resolves inside one of the **positively observed** inspection roots — a root
#: A1 actually saw (:data:`library_inventory.PRESENT`, or :data:`library_inventory.ACCESS_DENIED`:
#: there and merely refused us). This is a *containment* claim about that root's storage and nothing
#: more: it does not assert the source exists, and A3 never stats it. A root that A1 could not
#: positively observe — unreachable, configured-but-never-created, or simply not inspected —
#: contributes no identity at all, so it can neither manufacture this answer nor
#: :data:`SCOPE_OUT`; see :func:`_root_is_verified`.
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
    #: Parsed records whose completion authority **refused to answer** (it raised). Counted here
    #: rather than in :attr:`complete_counts`, which carries only verdicts the authority actually
    #: gave, so that ``sum(complete_counts.values()) + completion_unavailable`` accounts for every
    #: parsed record. A raising authority is the absence of a decision, and
    #: :attr:`complete_records` withholds the folder total while any of these exist.
    completion_unavailable: int = 0

    # rung 6
    scope_counts: dict = field(default_factory=dict)
    scope_assessed: bool = False
    #: The ``root_key`` of every inspection root A1 **positively observed**. These, and only these,
    #: are what rung 6 compared against.
    inspection_roots: tuple = ()
    #: Inputs that were supplied as inspection roots but are **not** verified root identities: a
    #: bare path string (never probed), or a ``RootState`` whose own verdict is not a positive
    #: observation of its storage. They are reported so the caller can see they were passed, and
    #: they deliberately take **no** part in any scope verdict.
    unverified_roots: tuple = ()

    #: Did the entry loop run to completion? ``False`` means it stopped early — a ceiling, or the
    #: caller asking it to stop — so the per-entry figures cover only what it reached. This is
    #: **enumeration**, which is a different question from whether every requested rung answered
    #: (:attr:`assessments_complete`) and from whether the detail lists are exhaustive
    #: (:attr:`details_complete`).
    entries_complete: bool = False
    #: Did every rung the caller **requested** actually answer, for every record this pass parsed?
    #: ``False`` when a byte budget prevented a delegated transport check, when a file moved while
    #: being inspected, or when a completion authority raised. The counts remain honest either way —
    #: they say ``transport_unavailable`` and :attr:`completion_unavailable` — but a consumer that
    #: needs every rung answered must read this rather than infer it from the counts.
    assessments_complete: bool = False

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
    def completion_accounted(self) -> int:
        """Parsed records whose completion question reached an outcome of **any** kind.

        The invariant worth asserting is ``completion_accounted == records_parsed`` whenever rung 5
        was requested: every parsed record is either a verdict in :attr:`complete_counts` or an
        authority that refused to answer in :attr:`completion_unavailable`. Without this term the
        verdict counter could silently under-total.
        """
        return int(sum(self.complete_counts.values())) + int(self.completion_unavailable)

    @property
    def complete_records(self) -> int | None:
        """How many records are complete, or ``None`` when that figure was not established.

        ``None`` rather than ``0`` is deliberate and is the same discipline A2's ``totals`` uses: a
        figure that was not established must not be readable as a measurement of zero. Two distinct
        situations withhold it, and neither may be rounded to a number:

        * rung 5 was never assessed at all (no authority, or no mode), and
        * at least one parsed record's authority **raised**, so the count of complete records is
          not a count of this folder's complete records. The verdicts that *were* given remain
          available, explicitly labelled, in :attr:`complete_counts`.

        A partial pass — one a ceiling or a cooperative stop cut short — is different again: every
        record it reached was genuinely judged, so the figure IS reported, and
        :attr:`authoritative` is what says it is not a folder total.
        """
        if not self.completeness_assessed:
            return None
        if self.completion_unavailable:
            return None
        return int(self.complete_counts.get(COMPLETE_YES, 0))

    @property
    def details_complete(self) -> bool:
        """Are both detail lists exhaustive — the problem names AND the per-record details?"""
        return not (self.names_truncated or self.records_truncated)

    @property
    def counts_exact(self) -> bool:
        """Do the aggregate counts cover the WHOLE folder, whatever the detail lists show?

        This is the figure a large cache needs. With more records than ``max_reported_names`` the
        per-record detail list is a shortened *display* — :attr:`records_truncated` says so — while
        every count (:attr:`entries_listed`, :attr:`records_recognised`, :attr:`parse_counts`,
        :attr:`contract_counts`, :attr:`transport_counts`, :attr:`complete_counts`,
        :attr:`scope_counts`) still totals the entire directory. Reading a shortened display as the
        whole cache, or reading a withheld figure as zero, are the two misreadings this property
        exists to prevent.
        """
        return bool(self.entries_complete and self.state in (CACHE_PRESENT, CACHE_EMPTY))

    @property
    def authoritative(self) -> bool:
        """May these counts be published as totals for this folder? Only a complete pass may.

        Deliberately conservative, and deliberately **not** the same question as
        :attr:`counts_exact`. The two truncation indicators are not symmetric, and the asymmetry is
        a judgement about evidence rather than an oversight:

        * :attr:`names_truncated` cuts a *problem-name* list. For an alias, an escape, undecidable
          link metadata, a vanished entry or a changed one, that list is the **only** record that the
          entry existed at all — those entries never reach :attr:`records`. Truncating it destroys
          evidence that exists nowhere else, so the pass must not call itself complete. It raises
          :data:`BOUND_NAMES`, which makes :attr:`complete` — and therefore this — ``False``.
        * :attr:`records_truncated` shortens the per-record detail *display*, every element of which
          is already counted in the aggregate counters. It carries no unique evidence, so it does
          **not** raise a bound and does not clear this flag. :attr:`details_complete` reports it,
          and a consumer rendering :attr:`records` must consult it before implying the list is the
          whole cache.

        So: ``authoritative`` answers "may I publish these as folder totals *and* show my full
        working?"; :attr:`counts_exact` answers "do the numbers cover the whole folder?"; and
        :attr:`assessments_complete` answers "did every rung I asked for actually answer?".
        """
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


def _identity(info, size: int) -> tuple:
    """One file's stat identity: ``(size, inode, device)``, each defaulted rather than assumed.

    Size alone is **not** an identity. A record replaced by a different record of exactly equal
    byte length is invisible to a size comparison, and `video_analysis._save_cache` publishes every
    record by ``os.replace`` of a fresh temp file, so the inode is the field that actually moves:
    measured 40/40 on that writer, against 5/40 for ``st_mtime_ns``, whose resolution on this
    platform is coarse enough that a fast write can land inside one tick. mtime is therefore
    deliberately absent from this tuple.
    """
    return (int(size),
            int(getattr(info, "st_ino", -1)),
            int(getattr(info, "st_dev", -1)))


def _observe_identity(path, lstat) -> tuple:
    """Re-observe :func:`_identity` now. A failure answers a tuple that cannot compare equal.

    An identity that cannot be established is not evidence that nothing changed, so it must not be
    allowed to pass a comparison. ``(-1, -1, -1)`` is unreachable for a real entry, which is what
    makes "we could not look" fail closed into :data:`TRANSPORT_UNAVAILABLE`.
    """
    try:
        info = lstat(path)
    except OSError:
        return (-1, -1, -1)
    return _identity(info, int(getattr(info, "st_size", -1)))


def _root_is_verified(item) -> bool:
    """Did A1 **positively observe** this root's own storage? Only then may it anchor rung 6.

    `RootState.positively_observed` is A1's own gate -- added by A1's finding F-2, which was exactly
    this mistake one layer down: a root carrying ``anchor_reachable=True`` while its own verdict was
    a hold was accepted as "the storage is still there". ``root_key`` cannot stand in for it:
    ``root_key`` is a *derived* property (``_norm(real)``), and `probe_root` populates ``real`` for
    :data:`library_inventory.ROOT_UNREACHABLE`, ``CONFIGURED_NOT_CREATED`` and ``UNKNOWN`` verdicts
    too. A root that was never seen can neither contain a source nor fail to contain one, so letting
    it answer would manufacture both :data:`SCOPE_IN` and :data:`SCOPE_OUT` out of nothing.

    A ``RootState`` built by hand may legitimately omit the derived ``anchor`` while still recording
    a positive verdict, so a :data:`library_inventory.PRESENT` or
    :data:`library_inventory.ACCESS_DENIED` verdict with an established ``root_key`` is accepted as
    the same claim. For every state `probe_root` actually returns the two tests agree: it sets
    ``anchor`` from the resolved location on both of its positive paths. ``ACCESS_DENIED`` counts
    because the folder is demonstrably there and merely refused us -- A1 includes it for the same
    reason.
    """
    if not _clean(getattr(item, "root_key", "")):
        return False
    if getattr(item, "positively_observed", False):
        return True
    return getattr(item, "verdict", "") in (PRESENT, ACCESS_DENIED)


def _root_keys(inspection_roots, realpath) -> tuple:
    """``(verified_keys, unverified_inputs)`` -- and only the first may answer rung 6.

    Identity is always the **resolved** location, which is A1's F-1 lesson: a root selected as
    ``E:\\LibraryLink`` may be a junction onto ``D:\\ActualLibrary``, and a scope answer about the
    typed path would be an answer about the wrong storage. A1 has already resolved its own roots, so
    a ``RootState`` contributes its ``root_key`` directly.

    A plain string is accepted for compatibility but is **never promoted to a verified root
    identity**: nobody probed it, so it is resolved for reporting and returned as an unverified
    input. Callers that want rung 6 answered pass A1 ``RootState`` objects.
    """
    verified: list = []
    unverified: list = []

    def remember(bucket: list, value: str) -> None:
        if value and value not in bucket:
            bucket.append(value)

    for item in inspection_roots or ():
        if isinstance(item, str):
            text = _clean(item)
            if text:
                remember(unverified, _resolve(text, realpath) or text)
            continue
        key = _clean(getattr(item, "root_key", ""))
        if _root_is_verified(item):
            remember(verified, key)
        else:
            remember(unverified, key)
    return tuple(verified), tuple(unverified)


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
        A1 ``RootState`` objects. Only a root A1 **positively observed** — it listed
        (:data:`library_inventory.PRESENT`), or it is there and merely refused us
        (:data:`library_inventory.ACCESS_DENIED`) — may anchor rung 6; see :func:`_root_is_verified`
        for why ``root_key`` alone is not enough. Anything else, including a bare path string that
        nobody probed, is reported in :attr:`RecordInventory.unverified_roots` and takes no part in
        any verdict. With no verified root, rung 6 is unassessed: "outside the library" is not a
        fact anyone established.

    The bounds charge **work attempted**, not results obtained: a refused, vanished, malformed or
    oversize file costs exactly what a good one costs. They are validated before any filesystem
    call, so a bad ceiling cannot be discovered half way through a folder.

    **What the bounds are and are not.** ``max_record_bytes`` governs the bytes this module's own
    parser consumes — the read is bounded and an over-length result is refused unparsed — but it
    cannot govern the delegated transport re-read, because `analysis_cache.looks_reusable` reopens
    the path itself; that cost is charged as a stat-time estimate and a growing file can exceed it.
    ``listdir`` materialises the whole directory before any per-entry ceiling can apply, so the
    floor cost of a pass is the size of one listing regardless of ``max_entries``. Neither this nor
    any other ceiling here is a wall-clock or peak-memory bound, and no figure derived from them
    claims to be. The module docstring lists every such limit in one place.

    **Reading the result's truthfulness.** Four separate questions, deliberately not collapsed:
    :attr:`RecordInventory.entries_complete` (was every entry classified?),
    :attr:`RecordInventory.assessments_complete` (did every requested rung answer?),
    :attr:`RecordInventory.details_complete` (are the detail lists exhaustive?) and
    :attr:`RecordInventory.counts_exact` (do the counts cover the whole folder?).
    :attr:`RecordInventory.authoritative` is the conservative conjunction a publisher should check.
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
    root_keys, unverified_roots = _root_keys(inspection_roots, realpath)
    assess_contract = isinstance(contract, str) and bool(contract)
    assess_complete = callable(is_complete) and require_ai is not None
    # ONE effective mode, shared by the authority call and the label, so the figure and its label
    # cannot disagree about which mode was judged. Normalised only AFTER the line above has
    # distinguished `require_ai=None` ("no mode established", rung 5 unassessed) from
    # `require_ai=False` ("judge the no-AI mode") -- that distinction is load-bearing and survives.
    # A truthy non-bool therefore selects the require-AI mode in BOTH places rather than relying on
    # two independent truth tests happening to agree.
    if assess_complete:
        require_ai = bool(require_ai)
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
        kwargs.setdefault("unverified_roots", unverified_roots)
        # An early return never began enumerating, so neither claim is available to it. A listed
        # empty folder is the one exception and says so explicitly.
        kwargs.setdefault("entries_complete", False)
        kwargs.setdefault("assessments_complete", False)
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
                      entries_listed=0, entries_examined=0,
                      entries_complete=True, assessments_complete=True)

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
    completion_unavailable = 0
    assessments_complete = True
    enumerated_every_entry = True
    bound = BOUND_NONE

    def note(bucket: list, value: str) -> None:
        nonlocal names_truncated
        if len(bucket) < max_reported_names:
            bucket.append(value)
        else:
            names_truncated = True

    def reader(path: str) -> str:
        """Read ONE record, **bounded**. The byte verdict is the caller's, on what came back.

        The default path reads at most ``max_record_bytes + 1`` characters, so a file that grew
        after its size gate can no longer be slurped whole: the old unbounded ``handle.read()``
        turned a stat-time ceiling into no ceiling at all, and was measured consuming 5 000 190
        bytes under a 1 000-byte limit. The limit is expressed in characters because this is the
        module's one text-mode ``open``, so the worst case is four bytes per character under UTF-8 --
        a real bound, and documented as an approximate one in the module docstring. Reading one
        character *past* the limit is what makes "too large" decidable without a second stat.

        ``newline=""`` disables universal-newline translation so the returned text's encoded length
        is the file's own byte length rather than a CRLF-shortened one. JSON is newline-insensitive,
        so nothing downstream changes.

        The injected ``read_text`` seam is a **different trust boundary**: a test supplies the bytes,
        so the bound above does not apply to it. Its result is byte-checked identically, which is
        what keeps the verdict consistent across both paths.
        """
        if read_text is not None:
            return read_text(path)
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read(max_record_bytes + 1)

    for name in names:
        if should_stop is not None and should_stop():
            bound = bound or BOUND_STOPPED
            enumerated_every_entry = False
            break
        if entries_examined >= max_entries:
            bound = bound or BOUND_ENTRIES
            enumerated_every_entry = False
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
            # The identity is captured HERE, from the very observation whose `st_size` becomes
            # `size` -- i.e. before this pass reads the file. Taking it later instead was measured
            # to detect nothing: by then a replacement has already happened and every subsequent
            # stat agrees with itself. This tuple is what rung 4 compares against.
            gate_identity = _identity(info, size)
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
            bound = bound or BOUND_RECORDS
            enumerated_every_entry = False
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
            bound = bound or BOUND_TOTAL_BYTES
            enumerated_every_entry = False
            break

        records_attempted += 1
        payload = None
        status = PARSE_OK
        detail = ""
        # Charged BEFORE the read, so a read that fails part way through is not free; over-charging
        # a file that turned out to be unreadable is the harmless direction. The *stat* size is only
        # an estimate of what the read will cost, though, so the charge is reconciled against the
        # bytes actually consumed immediately below -- which is what makes :attr:`bytes_charged`
        # truthful for a file that grew. It is named for what it is: bytes charged, not bytes
        # successfully parsed.
        bytes_charged += size
        try:
            raw = reader(full)
            consumed = len(raw.encode("utf-8"))
            if consumed > size:
                # It grew between its size gate and this read. Charge what was really consumed; the
                # budget cannot un-read bytes, so the honest move is to record them.
                bytes_charged += consumed - size
            if consumed > max_record_bytes:
                # The ceiling applies to the bytes THIS PASS CONSUMED, not only to the earlier stat.
                # Refused here rather than parsed, and deliberately NOT parsed as a truncated
                # prefix: a prefix of a JSON object is either invalid or -- worse -- a different,
                # smaller object. Reported as both an oversize refusal and instability, because the
                # file demonstrably moved after it was measured.
                status = PARSE_TOO_LARGE
                detail = (f"grew past the {max_record_bytes}-byte ceiling after it was measured "
                          f"at {size} bytes")
                note(oversize, name)
                note(changed, name)
                assessments_complete = False
            else:
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
            # Guarded on PARSE_OK because the oversize refusal above also leaves the try block
            # without an exception, with `payload` still None -- and `isinstance(None, dict)` would
            # otherwise relabel a refused-too-large record as "not a JSON object".
            if status == PARSE_OK and not isinstance(payload, dict):
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
                    # A resource ceiling stopped a requested observation, so the pass must say so.
                    # Recording TRANSPORT_UNAVAILABLE while leaving `bound` empty let the result
                    # publish itself as a complete, authoritative total with `bound=BOUND_NONE` --
                    # the one thing a bounded reader must never do. `or` is deliberate: the FIRST
                    # bound reached is the one that gets reported, never overwritten.
                    record_transport = TRANSPORT_UNAVAILABLE
                    bound = bound or BOUND_TOTAL_BYTES
                    assessments_complete = False
                    detail = detail or "transport check skipped: byte budget exhausted"
                else:
                    current = _observe_identity(full, lstat)
                    if current != gate_identity:
                        record_transport = TRANSPORT_UNAVAILABLE
                        note(changed, name)
                        assessments_complete = False
                        detail = detail or "the file changed between being measured and checked"
                    else:
                        bytes_charged += size
                        try:
                            ok, reason = looks_reusable(full, contract)
                        except Exception as exc:
                            record_transport = TRANSPORT_UNAVAILABLE
                            detail = detail or f"transport check failed: {type(exc).__name__}"
                            assessments_complete = False
                        else:
                            # Re-observe on BOTH outcomes. The helper opens the path itself, so its
                            # answer describes whatever bytes were there during ITS read -- and an
                            # ACCEPTED verdict needs that checked exactly as much as a refused one
                            # does. Verifying only the refusal branch meant a record replaced by a
                            # different record of identical length was published as a stable
                            # TRANSPORT_OK for a payload this pass never parsed.
                            #
                            # The discriminator is a fresh observation of the file, never a match on
                            # the helper's wording: its reason strings are its own to change, and a
                            # reader that branched on them would break silently when they did.
                            after = _observe_identity(full, lstat)
                            if after != gate_identity:
                                record_transport = TRANSPORT_UNAVAILABLE
                                note(changed, name)
                                assessments_complete = False
                                detail = detail or (
                                    "the file changed while the transport check read it")
                            elif ok:
                                record_transport = TRANSPORT_OK
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
                    # Counted, so the verdict counter cannot silently under-total. Previously
                    # nothing was recorded at all, which made `complete_records` read as a measured
                    # zero: "no records are complete" rather than "the authority would not answer".
                    # It is counted HERE rather than as a NOT_ASSESSED key in `complete_counts`
                    # because that dict carries only verdicts the authority actually gave -- see
                    # `completion_accounted` for the invariant that ties the two together.
                    completion_unavailable += 1
                    assessments_complete = False
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
    #
    # Note which truncation this is, and why `records_truncated` is deliberately NOT folded in
    # here -- the full reasoning is on `RecordInventory.authoritative`. A problem-name list is the
    # ONLY place an alias, an escape, undecidable link metadata, a vanished entry or a changed one
    # is ever named: those entries never reach `records`, so cutting the list destroys evidence held
    # nowhere else. The per-record detail list carries no such unique evidence -- every element of
    # it is already in the aggregate counters -- so it is disclosed through `records_truncated` and
    # `details_complete` instead of silently reclassifying every production-sized cache as an
    # incomplete observation.
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
        completion_unavailable=completion_unavailable,
        entries_complete=enumerated_every_entry, assessments_complete=assessments_complete,
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
