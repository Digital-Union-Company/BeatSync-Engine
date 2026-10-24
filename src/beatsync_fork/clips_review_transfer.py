"""Clips Preview Selector P2 — the safe-transfer core.

[R5-P45] The engine that moves reviewed clips to the Media Library or to quarantine. It is
deliberately **isolated**: nothing here is wired into ``gui.py``'s ``_on_clips_confirm``, no HTTP
route reaches it, and importing it cannot make P1 move a file. Confirm still reports readiness and
moves nothing. Wiring it up is a separate phase needing its own authorization after independent
review.

Per CLAUDE.md's hard rule this module imports **only** the standard library and other
``beatsync_fork`` modules. No ``gradio``, ``numpy``, ``cupy``, ``cv2``, ``librosa``, ``logger`` or
``paths``. It also stays inside the package's narrower stdlib allowlist, which is why there is no
``shutil`` (the copy is written here, with short writes handled), no ``ctypes`` (not needed — see
*The no-replace primitive* below), no ``pathlib`` and no ``errno`` (numeric constants instead, the
convention ``clips_review`` already uses).

The accepted specification is ``docs/claude/clips-preview-selector-v1.md`` §6 and the enforceable
contract is ``.claude/rules/clips-preview.md``.

The one idea everything rests on
--------------------------------
**The plan is server-authoritative.** Every source path, decision and destination in a
:class:`TransferPlan` is derived from the server's own :class:`~beatsync_fork.clips_review.ClipRegistry`
and :class:`~beatsync_fork.clips_review.ReviewStore`. :func:`plan_transfer` takes **no** source-path
parameter at all, so a browser request cannot nominate a file to move even by lying. The only
client-originated values it accepts are opaque tokens that the registry must already know.

The no-replace primitive
------------------------
The specification asks for an "atomic **no-replace** move (``MoveFileEx`` without
``REPLACE_EXISTING``)". On Windows ``os.rename`` **is** that call: CPython maps it to
``MoveFileExW`` without ``MOVEFILE_REPLACE_EXISTING``, so it raises ``FileExistsError`` when the
target exists instead of silently destroying it. ``os.replace`` is the replacing variant and is
therefore **never** used on a destination here.

That is a claim about behaviour, so :func:`probe_no_replace_rename` measures it on the actual
directory being written to, and :func:`execute_transfer` refuses when the guarantee cannot be
established. A preceding ``os.path.exists`` check is *not* relied on: it is a time-of-check /
time-of-use gap, and the atomic failure of the rename itself is what makes a collision safe.

What is *not* claimed
---------------------
Identity is proven by opening the source and calling ``os.fstat`` on the handle, not by trusting a
pathname. But a rename names a path, so a narrow window remains between the final ``fstat`` and the
rename in which the name could be repointed. That window cannot be closed with the primitives on
this package's allowlist, so it is **disclosed** rather than papered over: the no-replace rename
cannot overwrite anything, the published object is re-verified afterwards, and the source is removed
only once a verified publication exists. See ``untested_windows_conditions`` in the R5-P45 report.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from beatsync_fork.clips_review import (
    PENDING,
    REJECTED,
    ROLE_INPUT,
    ROLE_LIBRARY,
    ROLE_REJECTED,
    SELECTED,
    ClipEntry,
    Refusal,
    decision_identity,
    io_detail,
    validate_roots,
)

# --------------------------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------------------------

PLANNED = "PLANNED"
INTENT_RECORDED = "INTENT_RECORDED"
STAGED = "STAGED"
VERIFIED = "VERIFIED"
PUBLISHED = "PUBLISHED"
SOURCE_REMOVED = "SOURCE_REMOVED"
COMPLETE = "COMPLETE"
REFUSED = "REFUSED"
PARTIAL = "PARTIAL"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

#: Every state a transfer can be in. Ordered as the happy path progresses, then the terminals.
STATES = (PLANNED, INTENT_RECORDED, STAGED, VERIFIED, PUBLISHED, SOURCE_REMOVED, COMPLETE,
          REFUSED, PARTIAL, RECOVERY_REQUIRED)

#: The two transfer operations. The choice is made from the *measured* device of each side, never
#: from the drive letter: a junction or a ``subst`` can put two different letters on one volume.
OP_SAME_VOLUME = "SAME_VOLUME_NO_REPLACE_RENAME"
OP_CROSS_VOLUME = "CROSS_VOLUME_STAGE_VERIFY_PUBLISH_REMOVE"

#: Windows error numbers, as numeric constants because ``errno`` is not on the package allowlist
#: and ``clips_review`` already uses this convention.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183

_COPY_CHUNK = 1 << 20
_STAGING_PREFIX = ".r5transfer-"
_STAGING_SUFFIX = ".part"


class TransferRefused(Exception):
    """A whole plan or one item was refused. Carries a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class TransferItem:
    """One clip's immutable intent. ``source_path`` is server-resolved and never client-supplied."""

    clip_id: str
    index: int
    source_path: str
    source_size: int
    source_mtime_ns: int
    source_dev: int
    source_ino: int
    identity: str
    decision: str
    revision: int
    destination_role: str
    destination_dir: str
    destination_name: str
    destination_path: str
    operation: str

    @property
    def label(self) -> str:
        """The only name safe to show a browser. Never the basename."""
        return f"Clip {self.index:04d}"


@dataclass(frozen=True)
class PlanRefusal:
    code: str
    detail: str = ""
    clip_id: str = ""


@dataclass(frozen=True)
class TransferPlan:
    """Deterministic and immutable: one entry per clip, in page order."""

    plan_id: str
    session_id: str
    page_token: str
    scan_id: str
    owner: str
    created_at: float
    input_real: str
    library_real: str
    rejected_real: str
    items: tuple[TransferItem, ...] = ()
    refusals: tuple[PlanRefusal, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.refusals

    def to_event(self) -> dict:
        return {
            "k": "plan",
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "page_token": self.page_token,
            "scan_id": self.scan_id,
            "created_at": self.created_at,
            "input_real": self.input_real,
            "library_real": self.library_real,
            "rejected_real": self.rejected_real,
            "item_count": len(self.items),
            "refusals": [{"code": r.code, "detail": r.detail, "clip_id": r.clip_id}
                         for r in self.refusals],
        }


@dataclass(frozen=True)
class ItemOutcome:
    clip_id: str
    state: str
    code: str = ""
    detail: str = ""
    published_path: str = ""
    source_removed: bool = False
    bytes_copied: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class TransferResult:
    plan_id: str
    state: str
    outcomes: tuple[ItemOutcome, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def advance_page(self) -> bool:
        """**Only** a wholly COMPLETE batch may advance the gallery.

        An interrupted or partial transfer must not move the operator on, because the page is the
        unit of durable commitment and a partially committed page still has unprocessed clips.
        """
        return self.state == COMPLETE


@dataclass(frozen=True)
class RecoveryFinding:
    clip_id: str
    recorded_state: str
    observed: str
    action: str
    detail: str = ""


@dataclass(frozen=True)
class RecoveryReport:
    plan_id: str
    state: str
    findings: tuple[RecoveryFinding, ...] = ()

    @property
    def needs_operator(self) -> bool:
        return self.state == RECOVERY_REQUIRED


# --------------------------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------------------------

class TransferJournal:
    """Append-only JSONL intent/outcome log, one file per transfer journal directory.

    Deliberately the same durability shape ``ReviewStore`` already uses, because a second
    convention would be a second thing to get wrong:

    * ``O_APPEND`` plus ``os.fsync`` per record, so a reported write is on disk;
    * **every byte written**, because ``os.write`` may accept fewer bytes than offered and say so
      in its return value with no exception (the R5-P3 / F4 defect);
    * a record is only ever *appended* — nothing here rewrites or truncates history.

    A journal record never claims a destructive step completed before the filesystem has been
    verified: ``PUBLISHED`` is written after the published object is re-read, and
    ``SOURCE_REMOVED`` after the source is confirmed gone.
    """

    def __init__(self, directory: str, name: str = "transfer-journal.jsonl") -> None:
        self.directory = directory
        self.path = os.path.join(directory, name)

    def _write_all(self, handle: int, payload: bytes) -> int:
        """Write every byte. A positive short write is not an error and raises nothing."""
        written = 0
        while written < len(payload):
            count = os.write(handle, payload[written:])
            if count <= 0:
                raise TransferRefused("journal_write_stalled",
                                      f"os.write returned {count} with {len(payload) - written} left")
            written += count
        return written

    def append(self, event: dict) -> None:
        payload = (json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        try:
            os.makedirs(self.directory, exist_ok=True)
        except OSError as exc:
            raise TransferRefused("journal_unavailable", io_detail(exc)) from exc
        handle = None
        try:
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._write_all(handle, payload)
            os.fsync(handle)
        except OSError as exc:
            raise TransferRefused("journal_unavailable", io_detail(exc)) from exc
        finally:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass

    def records(self) -> list[dict]:
        """Every parseable record, in order. An unparseable tail line is reported, not guessed at.

        A torn final line is *skipped* rather than repaired here: this journal is append-only
        evidence for recovery, and inventing a record would be worse than having one fewer.
        """
        out: list[dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except ValueError:
                        out.append({"k": "unparseable", "raw_length": len(text)})
                        continue
                    if isinstance(event, dict):
                        out.append(event)
                    else:
                        out.append({"k": "unparseable", "raw_length": len(text)})
        except OSError as exc:
            if getattr(exc, "winerror", None) in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND) \
                    or getattr(exc, "errno", None) == _ERROR_FILE_NOT_FOUND:
                return []
            raise TransferRefused("journal_unavailable", io_detail(exc)) from exc
        return out

    def states_for(self, plan_id: str) -> dict[str, list[dict]]:
        """Per-clip record history for one plan, in journal order."""
        out: dict[str, list[dict]] = {}
        for event in self.records():
            if event.get("plan_id") != plan_id:
                continue
            clip_id = event.get("clip_id")
            if isinstance(clip_id, str) and clip_id:
                out.setdefault(clip_id, []).append(event)
        return out


# --------------------------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------------------------

def _is_reparse(path: str) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes       # Windows only
    except (OSError, AttributeError):
        return False
    return bool(attrs & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_within(child: str, parent: str) -> bool:
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return _norm(os.path.commonpath([c, p])) == p
    except ValueError:
        return False


def open_verified_source(item: TransferItem) -> int:
    """Open the source and prove its identity on the **descriptor**, not the pathname.

    Returns an owned fd the caller must close. Raises :class:`TransferRefused` with
    ``source_missing`` or ``source_identity_changed``. This is the same reasoning
    ``ClipRegistry.open_entry`` uses: checking a path and then opening it is a
    time-of-check/time-of-use gap, so the decisive comparison is made against the handle that will
    actually be read.
    """
    try:
        handle = os.open(item.source_path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND) \
                or getattr(exc, "errno", None) == _ERROR_FILE_NOT_FOUND:
            raise TransferRefused("source_missing", io_detail(exc)) from exc
        if code == _ERROR_ACCESS_DENIED:
            raise TransferRefused("source_access_denied", io_detail(exc)) from exc
        raise TransferRefused("source_unopenable", io_detail(exc)) from exc
    try:
        info = os.fstat(handle)
    except OSError as exc:
        os.close(handle)
        raise TransferRefused("source_unstattable", io_detail(exc)) from exc
    changed = (info.st_size != item.source_size
               or info.st_mtime_ns != item.source_mtime_ns
               or (item.source_ino not in (-1, 0) and info.st_ino != item.source_ino)
               or (item.source_dev not in (-1, 0) and info.st_dev != item.source_dev))
    if changed:
        os.close(handle)
        raise TransferRefused(
            "source_identity_changed",
            f"expected size={item.source_size} mtime_ns={item.source_mtime_ns} "
            f"ino={item.source_ino}; observed size={info.st_size} "
            f"mtime_ns={info.st_mtime_ns} ino={info.st_ino}")
    return handle


def probe_no_replace_rename(directory: str) -> bool:
    """Measure, on *this* directory, that ``os.rename`` refuses an existing destination.

    The specification's same-volume contract is an atomic no-replace move. Rather than assume
    CPython's Windows mapping holds for the filesystem in front of us, create two temporaries and
    try to rename one onto the other. Returns ``True`` only when the attempt raised.

    Cleans up only its **own** two temporaries, and never touches anything else.
    """
    first = second = None
    try:
        handle_a, first = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".probe-a", dir=directory)
        os.close(handle_a)
        handle_b, second = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".probe-b", dir=directory)
        os.close(handle_b)
    except OSError as exc:
        raise TransferRefused("no_replace_probe_failed", io_detail(exc)) from exc
    refused = False
    try:
        os.rename(first, second)
    except OSError:
        refused = True
    finally:
        for leftover in (first, second):
            if leftover:
                try:
                    os.unlink(leftover)                 # only ever this probe's own temporaries
                except OSError:
                    pass
    return refused


def _device_of(path: str) -> int:
    """``st_dev`` of an existing directory, used to choose same- vs cross-volume."""
    try:
        return os.stat(path).st_dev
    except OSError as exc:
        raise TransferRefused("destination_unstattable", io_detail(exc)) from exc


def _sha256_of_fd(handle: int, *, chunk: int = _COPY_CHUNK) -> tuple[str, int]:
    """Hash a descriptor from its current offset to EOF. Returns ``(hexdigest, bytes_read)``."""
    digest = hashlib.sha256()
    total = 0
    while True:
        block = os.read(handle, chunk)
        if not block:
            break
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def _write_all(handle: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(handle, payload[written:])
        if count <= 0:
            raise TransferRefused("staging_write_stalled",
                                  f"os.write returned {count} with {len(payload) - written} left")
        written += count


# --------------------------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------------------------

def plan_transfer(registry: Any, store: Any, *, session_id: str, page_token: str, owner: str,
                  scan_id: str, input_folder: str, library_folder: str, rejected_folder: str,
                  app_root: str = "", expected_revisions: dict[str, int] | None = None,
                  now: Callable[[], float] = time.time,
                  plan_id: str | None = None) -> TransferPlan:
    """Derive the immutable transfer plan from **server-side** review state.

    There is deliberately no source-path parameter: the sources come from
    ``registry.page_entries(session_id, page_token)`` and nowhere else, so a browser request cannot
    nominate a file to move. ``owner`` is a *scoping* key — the Gradio session hash — and is
    treated as such: it is compared against ``registry.owner_of(session_id)`` so a caller cannot
    claim another owner's session, but it is **not** an authenticated user identity and nothing
    here grants authority on the strength of it alone.

    Returns a plan whose ``refusals`` tuple is non-empty when the batch must not run. A refused
    plan still enumerates what it found, so the caller can report precisely why.
    """
    refusals: list[PlanRefusal] = []

    verdict, ids = validate_roots(input_folder, library_folder, rejected_folder, app_root)
    if not verdict.ok:
        return TransferPlan(
            plan_id=plan_id or _mint_plan_id(), session_id=str(session_id),
            page_token=str(page_token), scan_id=str(scan_id), owner=str(owner),
            created_at=now(), input_real="", library_real="", rejected_real="",
            refusals=(PlanRefusal(verdict.code, verdict.detail),))

    input_real = ids[ROLE_INPUT].real
    library_real = ids[ROLE_LIBRARY].real
    rejected_real = ids[ROLE_REJECTED].real

    try:
        entries: Sequence[ClipEntry] = registry.page_entries(session_id, page_token)
    except Refusal as exc:
        return TransferPlan(
            plan_id=plan_id or _mint_plan_id(), session_id=str(session_id),
            page_token=str(page_token), scan_id=str(scan_id), owner=str(owner), created_at=now(),
            input_real=input_real, library_real=library_real, rejected_real=rejected_real,
            refusals=(PlanRefusal(getattr(exc, "reason", "page_unavailable"), str(exc)),))

    try:
        registered_owner = registry.owner_of(session_id)
    except Refusal as exc:
        return TransferPlan(
            plan_id=plan_id or _mint_plan_id(), session_id=str(session_id),
            page_token=str(page_token), scan_id=str(scan_id), owner=str(owner), created_at=now(),
            input_real=input_real, library_real=library_real, rejected_real=rejected_real,
            refusals=(PlanRefusal("unknown_session", str(exc)),))
    if str(owner) != str(registered_owner):
        refusals.append(PlanRefusal("owner_mismatch", "the session belongs to another owner"))

    current_scan = ""
    try:
        current_scan = store.current_scan(str(registered_owner))
    except Exception:                                   # a store that cannot answer fails closed
        refusals.append(PlanRefusal("scan_unavailable"))
    if not refusals and str(scan_id) != str(current_scan):
        refusals.append(PlanRefusal("stale_scan", f"plan scan_id={scan_id} current={current_scan}"))

    identities = [decision_identity(e.path, e.size, e.mtime_ns) for e in entries]
    snapshot = store.snapshot_of(identities)

    items: list[TransferItem] = []
    claimed: dict[str, str] = {}                        # normalised destination -> clip_id

    for entry, identity in zip(entries, identities):
        decision, revision = snapshot.get(identity, (PENDING, 0))

        if decision == PENDING:
            refusals.append(PlanRefusal("pending_decision", entry.label, entry.clip_id))
            continue
        if decision not in (SELECTED, REJECTED):
            refusals.append(PlanRefusal("undecidable_state", decision, entry.clip_id))
            continue

        if expected_revisions is not None:
            expected = expected_revisions.get(entry.clip_id)
            if expected is None or isinstance(expected, bool) or not isinstance(expected, int):
                refusals.append(PlanRefusal("revision_required", entry.label, entry.clip_id))
                continue
            if expected != revision:
                refusals.append(PlanRefusal(
                    "stale_revision", f"expected {expected}, durable {revision}", entry.clip_id))
                continue

        # The source must still be the file that was reviewed. A changed file's decision does not
        # apply any more -- `decision_identity` folds size and mtime in, so this is structural.
        if not registry.live_identity_ok(entry):
            refusals.append(PlanRefusal("source_identity_changed", entry.label, entry.clip_id))
            continue

        if _is_reparse(entry.path):
            refusals.append(PlanRefusal("source_is_reparse_point", entry.label, entry.clip_id))
            continue
        if not _is_within(entry.path, input_real):
            # Catches a junction alias that put a page entry outside the confirmed input root.
            refusals.append(PlanRefusal("source_outside_input_root", entry.label, entry.clip_id))
            continue

        role = ROLE_LIBRARY if decision == SELECTED else ROLE_REJECTED
        dest_dir = library_real if decision == SELECTED else rejected_real
        dest_name = os.path.basename(entry.path)
        dest_path = os.path.join(dest_dir, dest_name)

        # An existing target is refused, never suffixed. Checked here so the operator learns about
        # it before anything moves; the atomic rename is what makes it *safe*, not this check.
        if os.path.lexists(dest_path):
            refusals.append(PlanRefusal("target_exists", dest_path, entry.clip_id))
            continue

        key = _norm(dest_path)
        if key in claimed:
            # Two reviewed clips with the same basename would collide in one destination. Refusing
            # is required: inventing a suffix would silently rename the operator's media.
            refusals.append(PlanRefusal(
                "duplicate_destination", f"{dest_path} also claimed by {claimed[key]}",
                entry.clip_id))
            continue
        claimed[key] = entry.clip_id

        operation = OP_SAME_VOLUME
        try:
            if _device_of(dest_dir) != entry.dev:
                operation = OP_CROSS_VOLUME
        except TransferRefused as exc:
            refusals.append(PlanRefusal(exc.code, exc.detail, entry.clip_id))
            continue

        items.append(TransferItem(
            clip_id=entry.clip_id, index=entry.index, source_path=entry.path,
            source_size=entry.size, source_mtime_ns=entry.mtime_ns, source_dev=entry.dev,
            source_ino=entry.ino, identity=identity, decision=decision, revision=revision,
            destination_role=role, destination_dir=dest_dir, destination_name=dest_name,
            destination_path=dest_path, operation=operation))

    return TransferPlan(
        plan_id=plan_id or _mint_plan_id(), session_id=str(session_id),
        page_token=str(page_token), scan_id=str(scan_id), owner=str(owner), created_at=now(),
        input_real=input_real, library_real=library_real, rejected_real=rejected_real,
        items=tuple(items), refusals=tuple(refusals))


def _mint_plan_id() -> str:
    """A plan id is journal bookkeeping, never a security token, so a time-ordered hash is right."""
    seed = f"{time.time_ns()}|{os.getpid()}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


# --------------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------------

def execute_transfer(plan: TransferPlan, journal: TransferJournal, *,
                     now: Callable[[], float] = time.time,
                     before_publish: Callable[[TransferItem], None] | None = None,
                     after_publish: Callable[[TransferItem], None] | None = None,
                     after_intent: Callable[[TransferItem], None] | None = None,
                     after_stage: Callable[[TransferItem], None] | None = None) -> TransferResult:
    """Execute an already-derived plan, recording durable intent and outcome per item.

    A refused plan is never executed. Each item is independent: one item's failure leaves the
    remaining items **unprocessed**, and the batch reports ``PARTIAL`` rather than pressing on into
    an unknown state.

    The four injectable hooks exist so the test matrix can interrupt a real transfer at each
    durable boundary without patching the filesystem. They are not part of the production contract
    and default to ``None``.
    """
    if not plan.ok:
        journal.append({**plan.to_event(), "state": REFUSED, "at": now()})
        return TransferResult(plan.plan_id, REFUSED, (),
                              {"refused": len(plan.refusals), "moved": 0, "remaining": len(plan.items)})

    journal.append({**plan.to_event(), "state": PLANNED, "at": now()})

    outcomes: list[ItemOutcome] = []
    moved = 0
    failed = 0

    for position, item in enumerate(plan.items):
        try:
            outcome = _transfer_one(item, plan, journal, now=now,
                                    before_publish=before_publish, after_publish=after_publish,
                                    after_intent=after_intent, after_stage=after_stage)
        except TransferRefused as exc:
            outcome = ItemOutcome(item.clip_id, REFUSED, exc.code, exc.detail)
        outcomes.append(outcome)
        if outcome.state == COMPLETE:
            moved += 1
        else:
            failed += 1
            # Stop at the first failure. The remaining clips stay untouched and are reported as
            # such, because a page is the unit of durable commitment.
            remaining = plan.items[position + 1:]
            for skipped in remaining:
                outcomes.append(ItemOutcome(skipped.clip_id, PLANNED, "not_attempted",
                                            "an earlier item in this page failed"))
            state = PARTIAL if moved else REFUSED
            journal.append({"k": "batch", "plan_id": plan.plan_id, "state": state,
                            "moved": moved, "failed": failed, "remaining": len(remaining),
                            "at": now()})
            return TransferResult(plan.plan_id, state, tuple(outcomes),
                                  {"moved": moved, "failed": failed, "remaining": len(remaining)})

    journal.append({"k": "batch", "plan_id": plan.plan_id, "state": COMPLETE,
                    "moved": moved, "failed": 0, "remaining": 0, "at": now()})
    return TransferResult(plan.plan_id, COMPLETE, tuple(outcomes),
                          {"moved": moved, "failed": 0, "remaining": 0})


def _item_event(item: TransferItem, plan: TransferPlan, state: str, **extra: Any) -> dict:
    event = {
        "k": "item",
        "plan_id": plan.plan_id,
        "clip_id": item.clip_id,
        "state": state,
        "identity": item.identity,
        "decision": item.decision,
        "operation": item.operation,
        "source_path": item.source_path,
        "source_size": item.source_size,
        "source_mtime_ns": item.source_mtime_ns,
        "source_ino": item.source_ino,
        "source_dev": item.source_dev,
        "destination_path": item.destination_path,
        "destination_role": item.destination_role,
    }
    event.update(extra)
    return event


def _transfer_one(item: TransferItem, plan: TransferPlan, journal: TransferJournal, *,
                  now: Callable[[], float],
                  before_publish: Callable[[TransferItem], None] | None,
                  after_publish: Callable[[TransferItem], None] | None,
                  after_intent: Callable[[TransferItem], None] | None,
                  after_stage: Callable[[TransferItem], None] | None) -> ItemOutcome:
    # ---- 1. the destination directory must exist and must prove its no-replace semantics -------
    if not os.path.isdir(item.destination_dir):
        raise TransferRefused("destination_not_a_directory", item.destination_dir)
    if not probe_no_replace_rename(item.destination_dir):
        # Without a proven atomic no-replace rename the contract cannot be met, so refuse rather
        # than fall back to something weaker.
        raise TransferRefused("no_replace_not_guaranteed", item.destination_dir)

    # ---- 2. prove the source identity on a descriptor -----------------------------------------
    handle = open_verified_source(item)
    try:
        digest, size = _sha256_of_fd(handle)
    finally:
        os.close(handle)
    if size != item.source_size:
        raise TransferRefused("source_identity_changed",
                              f"read {size} bytes, expected {item.source_size}")

    # ---- 3. durable intent, BEFORE anything moves ---------------------------------------------
    journal.append(_item_event(item, plan, INTENT_RECORDED, source_sha256=digest, at=now()))
    if after_intent is not None:
        after_intent(item)

    if item.operation == OP_SAME_VOLUME:
        return _same_volume(item, plan, journal, digest, now=now, before_publish=before_publish,
                            after_publish=after_publish)
    return _cross_volume(item, plan, journal, digest, now=now, before_publish=before_publish,
                         after_publish=after_publish, after_stage=after_stage)


def _same_volume(item: TransferItem, plan: TransferPlan, journal: TransferJournal, digest: str, *,
                 now: Callable[[], float],
                 before_publish: Callable[[TransferItem], None] | None,
                 after_publish: Callable[[TransferItem], None] | None) -> ItemOutcome:
    """Atomic no-replace move. One step, and it either happened or it did not."""
    if before_publish is not None:
        before_publish(item)
    try:
        os.rename(item.source_path, item.destination_path)
    except OSError as exc:
        code = getattr(exc, "winerror", None)
        if code == _ERROR_ALREADY_EXISTS or isinstance(exc, FileExistsError):
            raise TransferRefused("target_exists", item.destination_path) from exc
        if code == _ERROR_ACCESS_DENIED:
            raise TransferRefused("destination_access_denied", io_detail(exc)) from exc
        if code in (_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND):
            raise TransferRefused("source_missing", io_detail(exc)) from exc
        raise TransferRefused("move_failed", io_detail(exc)) from exc

    # Verify the published object before claiming anything, then record it.
    published = _verify_published(item, digest)
    journal.append(_item_event(item, plan, PUBLISHED, published_sha256=published[0],
                               published_bytes=published[1], at=now()))
    if after_publish is not None:
        after_publish(item)
    # A same-volume rename removes the source as part of the one atomic operation, so there is no
    # separate delete to record -- and nothing to recover between publication and removal.
    journal.append(_item_event(item, plan, SOURCE_REMOVED, note="atomic rename moved the source",
                               at=now()))
    journal.append(_item_event(item, plan, COMPLETE, at=now()))
    return ItemOutcome(item.clip_id, COMPLETE, published_path=item.destination_path,
                       source_removed=True, bytes_copied=published[1], sha256=published[0])


def _cross_volume(item: TransferItem, plan: TransferPlan, journal: TransferJournal, digest: str, *,
                  now: Callable[[], float],
                  before_publish: Callable[[TransferItem], None] | None,
                  after_publish: Callable[[TransferItem], None] | None,
                  after_stage: Callable[[TransferItem], None] | None) -> ItemOutcome:
    """STAGE -> VERIFY -> PUBLISH -> REMOVE SOURCE. Never one irreversible step.

    A crash before publication leaves the original intact and at worst a ``.part`` temporary
    behind. A crash after publication but before source removal leaves **both** copies, which
    recovery resolves by identity rather than by replaying a delete.
    """
    staging_path = ""
    try:
        try:
            staging_handle, staging_path = tempfile.mkstemp(
                prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX, dir=item.destination_dir)
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED:
                raise TransferRefused("destination_access_denied", io_detail(exc)) from exc
            raise TransferRefused("staging_unavailable", io_detail(exc)) from exc

        # ---- STAGE: copy source -> unique destination-local temporary --------------------------
        copied = 0
        copy_digest = hashlib.sha256()
        source_handle = open_verified_source(item)
        try:
            while True:
                block = os.read(source_handle, _COPY_CHUNK)
                if not block:
                    break
                _write_all(staging_handle, block)
                copy_digest.update(block)
                copied += len(block)
            os.fsync(staging_handle)
        finally:
            os.close(source_handle)
            os.close(staging_handle)

        journal.append(_item_event(item, plan, STAGED, staging_path=staging_path,
                                   staged_bytes=copied, staged_sha256=copy_digest.hexdigest(),
                                   at=now()))
        if after_stage is not None:
            after_stage(item)

        # ---- VERIFY: length AND hash, re-read from disk rather than trusting the write ---------
        observed = _hash_file(staging_path)
        if observed[1] != item.source_size or observed[0] != digest:
            raise TransferRefused(
                "staged_copy_mismatch",
                f"staged {observed[1]} bytes sha={observed[0]}; expected {item.source_size} "
                f"bytes sha={digest}")
        journal.append(_item_event(item, plan, VERIFIED, staging_path=staging_path,
                                   verified_bytes=observed[1], verified_sha256=observed[0],
                                   at=now()))

        # ---- PUBLISH: atomic, no replacement --------------------------------------------------
        if before_publish is not None:
            before_publish(item)
        try:
            os.rename(staging_path, item.destination_path)
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            if code == _ERROR_ALREADY_EXISTS or isinstance(exc, FileExistsError):
                raise TransferRefused("target_exists", item.destination_path) from exc
            raise TransferRefused("publish_failed", io_detail(exc)) from exc
        published_staging = staging_path
        staging_path = ""                               # it is the destination now, not a temp

        # ---- verify the PUBLISHED object, then record it --------------------------------------
        published = _verify_published(item, digest)
        journal.append(_item_event(item, plan, PUBLISHED, published_sha256=published[0],
                                   published_bytes=published[1],
                                   staging_path=published_staging, at=now()))
        if after_publish is not None:
            after_publish(item)

        # ---- REMOVE SOURCE: only now, and only after re-proving the original ------------------
        try:
            source_handle = open_verified_source(item)
        except TransferRefused as exc:
            # The published copy is verified and recorded, so the data is safe; the original is no
            # longer what we copied. Do NOT delete something whose identity we cannot prove.
            journal.append(_item_event(item, plan, RECOVERY_REQUIRED,
                                       reason="source_changed_before_removal", detail=exc.code,
                                       at=now()))
            return ItemOutcome(item.clip_id, RECOVERY_REQUIRED, "source_changed_before_removal",
                               exc.detail, published_path=item.destination_path,
                               source_removed=False, bytes_copied=published[1],
                               sha256=published[0])
        os.close(source_handle)
        try:
            os.unlink(item.source_path)
        except OSError as exc:
            journal.append(_item_event(item, plan, RECOVERY_REQUIRED,
                                       reason="source_removal_failed", detail=io_detail(exc),
                                       at=now()))
            return ItemOutcome(item.clip_id, RECOVERY_REQUIRED, "source_removal_failed",
                               io_detail(exc), published_path=item.destination_path,
                               source_removed=False, bytes_copied=published[1],
                               sha256=published[0])
        journal.append(_item_event(item, plan, SOURCE_REMOVED, at=now()))
        journal.append(_item_event(item, plan, COMPLETE, at=now()))
        return ItemOutcome(item.clip_id, COMPLETE, published_path=item.destination_path,
                           source_removed=True, bytes_copied=published[1], sha256=published[0])
    finally:
        # Only ever this attempt's own temporary, never the destination. The R5-P3 / F6 poster
        # defect was a failing attempt unlinking a perfectly valid published object.
        if staging_path:
            try:
                os.unlink(staging_path)
            except OSError:
                pass


def _hash_file(path: str) -> tuple[str, int]:
    handle = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return _sha256_of_fd(handle)
    finally:
        os.close(handle)


def _verify_published(item: TransferItem, expected_sha: str) -> tuple[str, int]:
    """Re-read the published object and require it to be the payload that was verified."""
    try:
        observed_sha, observed_size = _hash_file(item.destination_path)
    except OSError as exc:
        raise TransferRefused("published_unreadable", io_detail(exc)) from exc
    if observed_size != item.source_size or observed_sha != expected_sha:
        raise TransferRefused(
            "published_mismatch",
            f"published {observed_size} bytes sha={observed_sha}; expected {item.source_size} "
            f"bytes sha={expected_sha}")
    return observed_sha, observed_size


# --------------------------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------------------------

def recover(plan: TransferPlan, journal: TransferJournal, *,
            now: Callable[[], float] = time.time) -> RecoveryReport:
    """Reconcile recorded intent against the **actual** filesystem.

    This never replays a move or a delete on the strength of a historical status. A journal says
    what was intended and what had been verified at the time; only the filesystem says what is
    true now. Every decision below is therefore taken from observed identity, and anything
    ambiguous is escalated to the operator rather than resolved by guessing.
    """
    history = journal.states_for(plan.plan_id)
    findings: list[RecoveryFinding] = []
    needs_operator = False

    for item in plan.items:
        events = history.get(item.clip_id, [])
        recorded = events[-1].get("state", PLANNED) if events else PLANNED

        source_exists = os.path.lexists(item.source_path)
        dest_exists = os.path.lexists(item.destination_path)

        source_matches = False
        if source_exists:
            try:
                handle = open_verified_source(item)
                os.close(handle)
                source_matches = True
            except TransferRefused:
                source_matches = False

        dest_matches = False
        if dest_exists:
            expected_sha = ""
            for event in events:
                for key in ("source_sha256", "verified_sha256", "published_sha256"):
                    if isinstance(event.get(key), str) and event[key]:
                        expected_sha = event[key]
            if expected_sha:
                try:
                    observed_sha, observed_size = _hash_file(item.destination_path)
                    dest_matches = (observed_sha == expected_sha
                                    and observed_size == item.source_size)
                except OSError:
                    dest_matches = False

        observed = f"source={'match' if source_matches else ('present' if source_exists else 'absent')}," \
                   f"destination={'match' if dest_matches else ('present' if dest_exists else 'absent')}"

        if dest_matches and not source_exists:
            findings.append(RecoveryFinding(item.clip_id, recorded, observed, "none",
                                            "the move completed; nothing to do"))
            continue
        if dest_matches and source_matches:
            # Published and verified, but the source survived -- the crash-after-publication case.
            # The safe resolution is to remove the now-redundant source, and that is only safe
            # because the destination has been hash-matched against the recorded intent just now.
            findings.append(RecoveryFinding(
                item.clip_id, recorded, observed, "remove_redundant_source",
                "destination hash-matches the recorded intent and the source is unchanged"))
            continue
        if not dest_exists and source_matches:
            findings.append(RecoveryFinding(
                item.clip_id, recorded, observed, "retry_safe",
                "nothing was published and the source is intact"))
            continue
        if dest_exists and not dest_matches:
            needs_operator = True
            findings.append(RecoveryFinding(
                item.clip_id, recorded, observed, "operator_required",
                "a different file occupies the destination; it will not be overwritten"))
            continue
        if not source_exists and not dest_exists:
            needs_operator = True
            findings.append(RecoveryFinding(
                item.clip_id, recorded, observed, "operator_required",
                "neither the source nor the destination is present"))
            continue
        needs_operator = True
        findings.append(RecoveryFinding(item.clip_id, recorded, observed, "operator_required",
                                        "the source is present but its identity does not match"))

    state = RECOVERY_REQUIRED if needs_operator else COMPLETE
    journal.append({"k": "recovery", "plan_id": plan.plan_id, "state": state,
                    "findings": len(findings), "at": now()})
    return RecoveryReport(plan.plan_id, state, tuple(findings))


def staging_leftovers(directory: str) -> list[str]:
    """This engine's own abandoned ``.part`` temporaries in one destination directory.

    Uses ``os.listdir`` with an anchored prefix/suffix test rather than ``glob``, which is not on
    this package's stdlib allowlist. Reports; removes nothing.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(os.path.join(directory, name) for name in names
                  if name.startswith(_STAGING_PREFIX) and name.endswith(_STAGING_SUFFIX))


__all__ = [
    "COMPLETE",
    "INTENT_RECORDED",
    "ItemOutcome",
    "OP_CROSS_VOLUME",
    "OP_SAME_VOLUME",
    "PARTIAL",
    "PLANNED",
    "PUBLISHED",
    "PlanRefusal",
    "RECOVERY_REQUIRED",
    "REFUSED",
    "RecoveryFinding",
    "RecoveryReport",
    "SOURCE_REMOVED",
    "STAGED",
    "STATES",
    "TransferItem",
    "TransferJournal",
    "TransferPlan",
    "TransferRefused",
    "TransferResult",
    "VERIFIED",
    "execute_transfer",
    "open_verified_source",
    "plan_transfer",
    "probe_no_replace_rename",
    "recover",
    "staging_leftovers",
]
