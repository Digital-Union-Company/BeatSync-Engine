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

Approval is content, not metadata
--------------------------------
[R5-P47 / H] A decision is only transferable if it carries a **content fingerprint** recorded when
the operator classified the clip, and the bytes on disk still hash to it. Metadata identity — size,
``mtime_ns``, ``st_ino``, ``st_dev`` — cannot distinguish the reviewed file from a same-length edit
whose timestamp was restored, and ``.claude/rules/clips-preview.md`` accepts that limitation
explicitly *because P1 mutates nothing*: "the residual case costs a stale preview, never a wrong
move". This engine performs the wrong move, so it cannot inherit that reasoning. A decision with no
fingerprint is **refused**, which is every decision the P1 HTTP path writes today. Nothing here
back-fills a digest onto an old decision and calls it consent.

Why the source is not deleted
-----------------------------
[R5-P47 / B] The cross-volume path publishes a verified copy and then **withholds the deletion**,
reporting :data:`SOURCE_REMOVAL_BLOCKED`. This is not an oversight and not a bug to be tidied up
later; it is the only honest outcome available, and the alternatives were measured rather than
assumed:

* ``os.unlinkat``, ``os.renameat`` and ``dir_fd`` support are **absent** on Windows, so there is no
  directory-relative delete;
* holding the verified descriptor across the delete does not work either — a normal ``O_RDONLY``
  handle makes the path undeletable (winerror 32), which is *why* the original closed it first and
  *why* a substitution could land in the gap;
* ``os.open(path, O_RDONLY | O_TEMPORARY)`` genuinely **is** a handle-anchored delete: it opens with
  ``FILE_SHARE_DELETE`` and the object is removed when the handle closes. But the disposition cannot
  be cancelled without ``SetFileInformationByHandle``, i.e. ``ctypes``. So the only race-free
  ordering commits to deleting whatever the pathname named *before* its identity could be checked,
  turning a rare wrong delete into a certain one.

Closing this properly needs a capability outside the approved dependency rules, which is an owner
decision and not one this module may make for itself. Until then: the operator keeps both copies,
the obligation is journalled, and no state ever claims a move completed when it did not.

Same-volume is different, and the difference is the whole reason A is fixable and B is not: a
no-replace ``os.rename`` **relocates the object** rather than deleting it, so a wrong outcome can be
undone by renaming it back (measured: the reverse rename succeeds onto the vacated path and refuses
with winerror 183 if anything has taken it). A wrong delete cannot be undone by anything.

What is *not* claimed
---------------------
Identity is proven on a descriptor, never on a pathname — but a rename names a path, so a narrow
window remains between the final ``fstat`` and the rename in which the name could be repointed. That
window is not closed; it is **contained**. The no-replace rename cannot overwrite anything, the
published object is re-verified against the approved fingerprint, and a failure past the commit
point is rolled back and reported as such rather than as "nothing happened". The transfer lease
closes the *decision* race completely (``ReviewStore.decide`` refuses a leased identity); it does
nothing about the filesystem one, and must not be described as if it did.
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

#: [R5-P47 / A] The destination was published but did not verify, and the moved object was put back
#: by an atomic no-replace rename. Nothing was lost and nothing unapproved remains published.
ROLLED_BACK = "ROLLED_BACK"

#: [R5-P47 / B] The copy is published and verified; the original is **deliberately still there**,
#: because this engine cannot prove a pathname delete would remove the object it verified. Not a
#: failure of the copy — a withheld deletion. See *Why the source is not deleted* in the docstring.
SOURCE_REMOVAL_BLOCKED = "SOURCE_REMOVAL_BLOCKED"

#: Every state a transfer can be in. Ordered as the happy path progresses, then the terminals.
STATES = (PLANNED, INTENT_RECORDED, STAGED, VERIFIED, PUBLISHED, SOURCE_REMOVED, COMPLETE,
          REFUSED, PARTIAL, RECOVERY_REQUIRED, ROLLED_BACK, SOURCE_REMOVAL_BLOCKED)

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
    #: [R5-P47 / H] SHA-256 of the bytes the operator classified, taken from the durable decision
    #: record at plan time. Never computed from the file at transfer time: a digest first measured
    #: during the move proves only that the copy matches the thing being moved, which is not the
    #: question. ``plan_transfer`` refuses an item that has no fingerprint, so this is never empty
    #: on an item that reaches execution.
    approved_sha256: str
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

#: [R5-P47 / M2] The three answers a reparse-point inspection can give. The original returned a
#: bare ``bool``, so "I could not look" and "I looked, and it is an ordinary file" were the same
#: value — a gate whose whole purpose is to refuse answered *safe* when it could not tell.
REPARSE_NO = "ORDINARY_FILE"
REPARSE_YES = "REPARSE_POINT"
REPARSE_UNKNOWN = "INSPECTION_FAILED"


def reparse_state(path: str) -> str:
    """Classify *path* as an ordinary file, a reparse point, or **not inspectable**.

    [R5-P47 / M2] Returns one of :data:`REPARSE_NO`, :data:`REPARSE_YES`,
    :data:`REPARSE_UNKNOWN`. The caller must treat ``UNKNOWN`` as a refusal: a path whose
    attributes cannot be read may be a junction pointing anywhere, and this engine's containment
    reasoning is only meaningful for files whose real location is known.

    ``AttributeError`` is kept distinct from ``OSError`` on purpose, and the two get different
    answers. ``OSError`` means *this path* could not be inspected, which is ``UNKNOWN`` and
    refuses. ``AttributeError`` means ``st_file_attributes`` does not exist — a non-Windows
    interpreter — and there the portable ``os.path.islink`` is a genuine inspection of the same
    question, so it answers rather than refusing. This module is Windows-only in production, but
    CLAUDE.md requires the suite to run on a bare interpreter, and a check that degraded to
    ``UNKNOWN`` off Windows would refuse every transfer there instead of testing one. Being unable
    to *run* the check is still never permission to skip it — that is what the ``OSError`` arm is.
    """
    try:
        attrs = os.lstat(path).st_file_attributes       # Windows only
    except AttributeError:
        try:
            return REPARSE_YES if os.path.islink(path) else REPARSE_NO
        except OSError:
            return REPARSE_UNKNOWN
    except OSError:
        return REPARSE_UNKNOWN
    flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not flag:
        return REPARSE_UNKNOWN
    return REPARSE_YES if bool(attrs & flag) else REPARSE_NO


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

    [R5-P47 / M1] **Any failure used to count as proof.** The original caught a bare ``OSError``
    and concluded the guarantee held, so an access-denied destination, a sharing violation or a
    transient device error all read as *no-replace PROVEN* — the engine's stated precondition was
    established by the very conditions that should have refused it. And nothing checked the
    positive case at all, so a directory in which *every* rename fails passed.

    Three things are now required, and all three are necessary:

    1. the collision attempt must fail **specifically** because the target exists —
       ``FileExistsError`` or winerror 183. Any other ``OSError`` raises
       ``no_replace_probe_inconclusive`` rather than returning an answer;
    2. a rename onto a **free** name in the same directory must *succeed*, so a wholly broken
       directory cannot pass by failing everything;
    3. both probe files must still hold their original bytes afterwards, so a filesystem that
       "refused" while actually merging or truncating cannot pass either.
    """
    first = second = moved = None
    marker_a = b"R5P47-PROBE-A"
    marker_b = b"R5P47-PROBE-B-DIFFERENT-LENGTH"
    try:
        handle_a, first = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".probe-a", dir=directory)
        try:
            _write_all(handle_a, marker_a)
        finally:
            os.close(handle_a)
        handle_b, second = tempfile.mkstemp(prefix=_STAGING_PREFIX, suffix=".probe-b", dir=directory)
        try:
            _write_all(handle_b, marker_b)
        finally:
            os.close(handle_b)
    except OSError as exc:
        raise TransferRefused("no_replace_probe_failed", io_detail(exc)) from exc

    try:
        # ---- 1. the collision must be refused, and refused for the RIGHT reason
        collided = False
        try:
            os.rename(first, second)
        except FileExistsError:
            collided = True
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_ALREADY_EXISTS:
                collided = True
            else:
                # Not evidence about no-replace semantics at all. Refusing to answer is the only
                # honest outcome: returning False would read as "replacement is possible" and
                # returning True would be the M1 defect.
                raise TransferRefused("no_replace_probe_inconclusive", io_detail(exc)) from exc
        if not collided:
            return False                                # the rename replaced the target

        # ---- 2. the positive control: a rename onto a FREE name must work
        moved = second + ".moved"
        try:
            os.rename(second, moved)
        except OSError as exc:
            raise TransferRefused("no_replace_probe_inconclusive",
                                  f"a rename onto a free name failed: {io_detail(exc)}") from exc
        second = None                                   # it lives at `moved` now

        # ---- 3. neither probe payload may have been altered by the refused rename
        try:
            with open(first, "rb") as handle:
                kept_a = handle.read()
            with open(moved, "rb") as handle:
                kept_b = handle.read()
        except OSError as exc:
            raise TransferRefused("no_replace_probe_inconclusive", io_detail(exc)) from exc
        if kept_a != marker_a or kept_b != marker_b:
            raise TransferRefused("no_replace_probe_inconclusive",
                                  "a refused rename altered the probe payloads")
        return True
    finally:
        for leftover in (first, second, moved):
            if leftover:
                try:
                    os.unlink(leftover)                 # only ever this probe's own temporaries
                except OSError:
                    pass


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

    [R5-P47 / C] ``expected_revisions`` is **mandatory**. It used to default to ``None``, which
    skipped the optimistic-concurrency check entirely — so the only defence against a stale
    decision was opt-in, and a caller that simply omitted it got none. ``None`` is now a refusal
    for the whole batch, and a missing or non-integer entry is a refusal for that clip.

    [R5-P47 / H] Every item additionally requires a **decision-time content fingerprint** that
    matches the bytes on disk now. Metadata identity (size, mtime, inode) cannot tell a
    same-length edit with a restored timestamp from the reviewed file, and this engine performs
    irreversible operations on the strength of the decision. No fingerprint means no transfer.
    """
    refusals: list[PlanRefusal] = []

    if expected_revisions is None:
        return TransferPlan(
            plan_id=plan_id or _mint_plan_id(), session_id=str(session_id),
            page_token=str(page_token), scan_id=str(scan_id), owner=str(owner),
            created_at=now(), input_real="", library_real="", rejected_real="",
            refusals=(PlanRefusal(
                "expected_revisions_required",
                "P2 execution requires the revision each decision is believed to be at"),))

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
    # [R5-P47 / H] A store too old to answer this fails closed rather than transferring unbound
    # content. `getattr` is not a compatibility shim here: it is the fail-closed branch.
    fingerprints: dict[str, str] = {}
    fingerprints_of = getattr(store, "fingerprints_of", None)
    if fingerprints_of is None:
        refusals.append(PlanRefusal(
            "content_fingerprints_unavailable",
            "this review store records no decision-time content fingerprint"))
    else:
        try:
            fingerprints = fingerprints_of(identities)
        except Exception:                               # a store that cannot answer fails closed
            refusals.append(PlanRefusal("content_fingerprints_unavailable"))

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

        # [R5-P47 / H] ...and metadata identity is not enough, because this engine then does
        # something irreversible. The decision must carry the digest of the bytes the operator
        # classified, and those bytes must still be what is on disk.
        approved_sha = fingerprints.get(identity, "")
        if not approved_sha:
            refusals.append(PlanRefusal(
                "content_fingerprint_missing", entry.label, entry.clip_id))
            continue

        inspection = reparse_state(entry.path)
        if inspection != REPARSE_NO:
            # [R5-P47 / M2] UNKNOWN refuses exactly as YES does. A path whose attributes could not
            # be read may be a junction to anywhere, and every containment check below would then
            # be reasoning about the wrong file.
            refusals.append(PlanRefusal(
                "source_is_reparse_point" if inspection == REPARSE_YES
                else "source_not_inspectable",
                entry.label, entry.clip_id))
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
            approved_sha256=approved_sha,
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

def _revalidate(item: TransferItem, plan: TransferPlan, store: Any, registry: Any) -> None:
    """[R5-P47 / C] Re-prove, under the transfer lease, that the plan still reflects authority.

    A frozen plan used to be the sole authority at execution time: ``execute_transfer`` consulted
    neither the store nor the registry, so a decision reversed after planning still executed, a
    clip put back to ``PENDING`` still moved, and a revoked session or superseded scan changed
    nothing. This re-reads all four facts from the server.

    It is called **while the lease is held**, which is what makes it binding rather than merely
    recent: :meth:`ReviewStore.decide` refuses a leased identity, so the decision cannot move
    between this check and the irreversible step that follows it. Without the lease this would be
    a smaller window, not a closed one.

    Raises :class:`TransferRefused`; never touches the filesystem.
    """
    # -- the session must still be live and still belong to the planning owner
    try:
        current_owner = registry.owner_of(plan.session_id)
    except Refusal as exc:
        raise TransferRefused("session_revoked", str(exc)) from exc
    if str(current_owner) != str(plan.owner):
        raise TransferRefused("owner_mismatch", "the session now belongs to another owner")
    try:
        registry.page_entries(plan.session_id, plan.page_token)
    except Refusal as exc:
        raise TransferRefused("page_revoked", str(exc)) from exc

    # -- the scan generation must still be the one the plan was built under
    try:
        current_scan = store.current_scan(str(plan.owner))
    except Exception as exc:                            # a store that cannot answer fails closed
        raise TransferRefused("scan_unavailable", str(exc)) from exc
    if str(current_scan) != str(plan.scan_id):
        raise TransferRefused("stale_scan",
                              f"plan scan_id={plan.scan_id} current={current_scan}")

    # -- the durable decision and its revision must be unchanged
    try:
        snapshot = store.snapshot_of([item.identity])
    except Exception as exc:
        raise TransferRefused("decision_unavailable", str(exc)) from exc
    decision, revision = snapshot.get(item.identity, (PENDING, 0))
    if decision != item.decision or revision != item.revision:
        raise TransferRefused(
            "decision_changed",
            f"planned {item.decision}@{item.revision}, durable {decision}@{revision}")

    # -- and the approved content fingerprint must still be the one the plan carries
    fingerprints_of = getattr(store, "fingerprints_of", None)
    if fingerprints_of is None:
        raise TransferRefused("content_fingerprints_unavailable", item.label)
    try:
        current_sha = fingerprints_of([item.identity]).get(item.identity, "")
    except Exception as exc:
        raise TransferRefused("content_fingerprints_unavailable", str(exc)) from exc
    if current_sha != item.approved_sha256:
        raise TransferRefused("content_fingerprint_changed", item.label)


def execute_transfer(plan: TransferPlan, journal: TransferJournal, *,
                     store: Any = None, registry: Any = None,
                     now: Callable[[], float] = time.time,
                     before_publish: Callable[[TransferItem], None] | None = None,
                     after_publish: Callable[[TransferItem], None] | None = None,
                     after_intent: Callable[[TransferItem], None] | None = None,
                     after_stage: Callable[[TransferItem], None] | None = None,
                     before_item: Callable[[TransferItem], None] | None = None) -> TransferResult:
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

    # [R5-P47 / C] The store and the registry are the live authority and are therefore required.
    # They are keyword arguments with a `None` default only so this refusal can be returned as a
    # typed result instead of a TypeError; `None` is never an accepted value.
    if store is None or registry is None:
        journal.append({**plan.to_event(), "state": REFUSED,
                        "reason": "live_authority_required", "at": now()})
        return TransferResult(plan.plan_id, REFUSED, (),
                              {"refused": 1, "moved": 0, "remaining": len(plan.items)})

    # [R5-P47 / C] One exclusive lease over every identity in the plan, for the whole batch. While
    # it is held `decide()` refuses those identities, so `_revalidate` is binding; and a second
    # execution of this plan -- or of any overlapping plan -- cannot acquire it.
    identities = [i.identity for i in plan.items]
    try:
        store.begin_transfer(plan.owner, plan.plan_id, identities)
    except Refusal as exc:
        journal.append({**plan.to_event(), "state": REFUSED,
                        "reason": getattr(exc, "reason", "transfer_lease_refused"), "at": now()})
        return TransferResult(plan.plan_id, REFUSED, (),
                              {"refused": 1, "moved": 0, "remaining": len(plan.items)})

    try:
        journal.append({**plan.to_event(), "state": PLANNED, "at": now()})

        outcomes: list[ItemOutcome] = []
        moved = 0
        published_only = 0
        failed = 0

        for position, item in enumerate(plan.items):
            try:
                if before_item is not None:
                    before_item(item)
                _revalidate(item, plan, store, registry)
                outcome = _transfer_one(item, plan, journal, now=now,
                                        before_publish=before_publish,
                                        after_publish=after_publish,
                                        after_intent=after_intent, after_stage=after_stage)
            except TransferRefused as exc:
                outcome = ItemOutcome(item.clip_id, REFUSED, exc.code, exc.detail)
            outcomes.append(outcome)

            if outcome.state == COMPLETE:
                moved += 1
                continue
            if outcome.state == SOURCE_REMOVAL_BLOCKED:
                # The copy is published and verified and the original is intact -- nothing is
                # ambiguous, so the remaining clips can still be processed. The batch can never
                # report COMPLETE though, which is what keeps `advance_page` false.
                published_only += 1
                continue

            failed += 1
            # Stop at the first genuine failure. The remaining clips stay untouched and are
            # reported as such, because a page is the unit of durable commitment.
            remaining = plan.items[position + 1:]
            for skipped in remaining:
                outcomes.append(ItemOutcome(skipped.clip_id, PLANNED, "not_attempted",
                                            "an earlier item in this page failed"))
            state = PARTIAL if (moved or published_only) else REFUSED
            journal.append({"k": "batch", "plan_id": plan.plan_id, "state": state,
                            "moved": moved, "published_only": published_only,
                            "failed": failed, "remaining": len(remaining), "at": now()})
            return TransferResult(plan.plan_id, state, tuple(outcomes),
                                  {"moved": moved, "published_only": published_only,
                                   "failed": failed, "remaining": len(remaining)})

        state = PARTIAL if published_only else COMPLETE
        journal.append({"k": "batch", "plan_id": plan.plan_id, "state": state,
                        "moved": moved, "published_only": published_only,
                        "failed": 0, "remaining": 0, "at": now()})
        return TransferResult(plan.plan_id, state, tuple(outcomes),
                              {"moved": moved, "published_only": published_only,
                               "failed": 0, "remaining": 0})
    finally:
        # Always released, including on an unexpected exception: a lease outliving its execution
        # would be a decision nobody can change and nobody can unblock.
        try:
            store.end_transfer(plan.owner, plan.plan_id)
        except Exception:                               # pragma: no cover - release must not mask
            pass


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

    # ---- 2b. [R5-P47 / H] the bytes must be the bytes the operator APPROVED --------------------
    # This is the check metadata identity cannot make. ``digest`` came from the same descriptor
    # whose ``fstat`` proved size/mtime/inode, and ``approved_sha256`` came from the durable
    # decision record — so a same-length edit that restored the original timestamp is caught here
    # and nowhere else in the system.
    if not item.approved_sha256:
        # Unreachable through `plan_transfer`, which refuses first. Kept because this function is
        # the last gate before an irreversible step, and a gate that trusts its caller is not one.
        raise TransferRefused("content_fingerprint_missing", item.label)
    if digest != item.approved_sha256:
        raise TransferRefused(
            "content_not_approved",
            f"on-disk sha={digest} does not match the approved sha={item.approved_sha256}")

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
    """Atomic no-replace move, with a **rollback** when the published object does not verify.

    [R5-P47 / A] The original renamed first and verified second, and turned a verification failure
    into ``REFUSED`` — a status meaning *nothing moved*, reported about a source path that was by
    then empty while an unapproved file sat in the operator's Media Library. Two corrections, and
    the second is what makes the first honest:

    * content is checked against the **approved fingerprint before** the rename
      (``_transfer_one`` step 2b), so the ordinary changed-source case never reaches this function;
    * if the published object still fails to verify — the residual window between the final
      ``fstat`` and the rename, which this allowlist cannot close — the rename is **undone** by
      renaming the destination back onto the now-vacated source path.

    The rollback is itself a no-replace rename, which is what makes it safe: measured on this
    platform it succeeds onto the vacated path, and raises ``FileExistsError``/winerror 183 if
    anything has taken that path meanwhile, so it can never overwrite a new occupant. A successful
    rollback reports ``ROLLED_BACK``; a failed one reports ``RECOVERY_REQUIRED`` with both paths
    recorded. Neither claims nothing happened, and neither leaves unapproved content published.

    A same-volume rename **relocates the object** rather than deleting it, which is why this path
    is recoverable and the cross-volume one is not. See *Why the source is not deleted*.
    """
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

    # The rename is a COMMIT POINT. Past here a failure may not be reported as "nothing moved".
    try:
        published = _verify_published(item, digest)
    except TransferRefused as exc:
        return _rollback_same_volume(item, plan, journal, exc, now=now)

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


def _rollback_same_volume(item: TransferItem, plan: TransferPlan, journal: TransferJournal,
                          cause: TransferRefused, *,
                          now: Callable[[], float]) -> ItemOutcome:
    """[R5-P47 / A] Put the moved object back, and report what actually happened.

    Never retried and never forced: the reverse rename is no-replace, so if the source path has
    been taken meanwhile the rollback refuses and the operator is told, rather than one of the two
    files being destroyed to make the outcome look tidy.
    """
    try:
        os.rename(item.destination_path, item.source_path)
    except OSError as exc:
        journal.append(_item_event(
            item, plan, RECOVERY_REQUIRED, reason="rollback_failed",
            detail=io_detail(exc), publication_failure=cause.code, at=now()))
        return ItemOutcome(
            item.clip_id, RECOVERY_REQUIRED, "rollback_failed",
            f"{cause.code}; the object is at the destination and could not be put back: "
            f"{io_detail(exc)}",
            published_path=item.destination_path, source_removed=True)

    journal.append(_item_event(item, plan, ROLLED_BACK, reason="published_object_unverified",
                               detail=cause.code, at=now()))
    return ItemOutcome(item.clip_id, ROLLED_BACK, "published_object_unverified", cause.detail,
                       published_path="", source_removed=False)


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
    staging_handle = -1
    try:
        try:
            staging_handle, staging_path = tempfile.mkstemp(
                prefix=_STAGING_PREFIX, suffix=_STAGING_SUFFIX, dir=item.destination_dir)
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_ACCESS_DENIED:
                raise TransferRefused("destination_access_denied", io_detail(exc)) from exc
            raise TransferRefused("staging_unavailable", io_detail(exc)) from exc

        # ---- STAGE: copy source -> unique destination-local temporary --------------------------
        # [R5-P47 / M3] The staging descriptor is closed by a `finally` that begins the statement
        # after `mkstemp` returned it. Previously the only `finally` closing it was *inside* the
        # block guarded by `open_verified_source`, so a source that failed to open left the
        # descriptor open — and the outer cleanup's `os.unlink` then failed with a sharing
        # violation (winerror 32) and was swallowed, leaving an orphaned `.part` file in the
        # operator's Media Library that nothing would ever remove.
        try:
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
        finally:
            if staging_handle >= 0:
                try:
                    os.close(staging_handle)
                except OSError:
                    pass
                staging_handle = -1

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

        # ---- SOURCE REMOVAL: deliberately NOT performed --------------------------------------
        # [R5-P47 / B] This is where the original proved the source's identity on a descriptor,
        # closed it, and then called `os.unlink(item.source_path)` — a **pathname** delete. An
        # independent review substituted a different file into the close-to-unlink gap and the
        # engine destroyed it while reporting COMPLETE.
        #
        # There is no stdlib primitive that closes this. Measured on this platform, CPython 3.14:
        #
        #   * `os.unlinkat` / `os.renameat` / `dir_fd` support   -- absent on Windows;
        #   * a normal `O_RDONLY` handle makes the path undeletable (winerror 32), so the
        #     descriptor cannot simply be held across the delete;
        #   * `os.open(path, O_RDONLY | O_TEMPORARY)` *is* a genuinely handle-anchored delete: it
        #     opens with FILE_SHARE_DELETE and the file is gone when the handle closes. But the
        #     disposition **cannot be cancelled** without `SetFileInformationByHandle`, which needs
        #     `ctypes`. So the only ordering that is race-free deletes whatever the pathname
        #     happened to name *before* its identity could be checked, which converts a rare wrong
        #     delete into a guaranteed one. That is strictly worse, and it is why this is not used.
        #
        # So the copy is published and verified, and the deletion is **withheld**. Nothing is
        # lost: the operator has the clip in the Media Library and still has the original. The
        # obligation is recorded so a later authorized phase — or an operator — can act on it.
        # A safe failure is preferable to an unsafe successful move.
        journal.append(_item_event(
            item, plan, SOURCE_REMOVAL_BLOCKED,
            reason="no_handle_anchored_delete_available",
            published_sha256=published[0], published_bytes=published[1],
            source_sha256=digest, source_still_present=True, at=now()))
        return ItemOutcome(
            item.clip_id, SOURCE_REMOVAL_BLOCKED, "source_removal_withheld",
            "the copy is published and verified; the original was NOT deleted because this engine "
            "cannot prove a pathname delete would remove the object it verified",
            published_path=item.destination_path, source_removed=False,
            bytes_copied=published[1], sha256=published[0])
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

#: [R5-P47 / M4] The order in which this engine writes per-item states. Used to decide whether a
#: recorded history is one this engine can have produced; it is **not** a permission table, and
#: nothing is replayed from it.
_ITEM_STATE_ORDER = (INTENT_RECORDED, STAGED, VERIFIED, PUBLISHED, SOURCE_REMOVED, COMPLETE)
_ITEM_TERMINALS = (COMPLETE, RECOVERY_REQUIRED, ROLLED_BACK, SOURCE_REMOVAL_BLOCKED)


def _history_fault(events: Sequence[dict]) -> str:
    """Is this per-item journal history one this engine can have written? Returns why not, or ``""``.

    [R5-P47 / M4] Recovery used to read whatever fields the history offered without ever asking
    whether the history made sense. Four shapes are now refused, because for each of them the
    meaning of the record cannot be established and guessing favours the destructive reading:

    * a state this build does not know;
    * a **terminal** state that is not the last record, or more than one terminal;
    * progress states that run **backwards** through the write order;
    * contradictory digests for the same state — two different ``published_sha256`` values for one
      clip cannot both be what was published.

    Being unable to interpret the history is an escalation, never a licence to act on part of it.
    """
    if not events:
        return ""
    seen_digests: dict[str, set] = {}
    highest = -1
    for position, event in enumerate(events):
        state = event.get("state")
        if not isinstance(state, str) or state not in STATES:
            return "the journal records a state this build does not understand"
        if state in _ITEM_TERMINALS and position != len(events) - 1:
            return f"a terminal state ({state}) is followed by further records"
        if state in _ITEM_STATE_ORDER:
            rank = _ITEM_STATE_ORDER.index(state)
            if rank < highest:
                return "the journal records progress states out of order"
            highest = rank
        for key in ("source_sha256", "staged_sha256", "verified_sha256", "published_sha256"):
            value = event.get(key)
            if isinstance(value, str) and value:
                seen = seen_digests.setdefault(key, set())
                seen.add(value)
                if len(seen) > 1:
                    return f"the journal records contradictory {key} values"
    return ""


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

        # [R5-P47 / M4] The history itself must be interpretable before anything is concluded from
        # it. A contradictory, duplicated, forged or out-of-order sequence is escalated rather than
        # read for whichever fields happen to suit.
        history_fault = _history_fault(events)
        if history_fault:
            needs_operator = True
            findings.append(RecoveryFinding(
                item.clip_id, recorded, "history=uninterpretable", "operator_required",
                history_fault))
            continue

        source_exists = os.path.lexists(item.source_path)
        dest_exists = os.path.lexists(item.destination_path)

        source_matches = False
        if source_exists:
            try:
                handle = open_verified_source(item)
                try:
                    source_sha, _read = _sha256_of_fd(handle)
                finally:
                    os.close(handle)
                # Metadata identity AND the approved content, for the same reason the transfer
                # path requires both: a size-preserving edit with a restored mtime passes the first
                # and must not pass the second.
                source_matches = (source_sha == item.approved_sha256)
            except (TransferRefused, OSError):
                source_matches = False

        # [R5-P47 / M4] The expected digest is the plan's **approved** fingerprint, which comes
        # from the durable decision record. It is no longer scavenged from the journal by scanning
        # every event for the last populated hash field — a forged or corrupted record carrying a
        # different `published_sha256` used to redefine what recovery believed the destination
        # should be, and a same-length stranger then passed as a valid publication.
        dest_matches = False
        if dest_exists and item.approved_sha256:
            try:
                observed_sha, observed_size = _hash_file(item.destination_path)
                dest_matches = (observed_sha == item.approved_sha256
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
            # Published and verified while the source survived -- the crash-after-publication case,
            # and also the ordinary outcome of the cross-volume path, which withholds the delete.
            # Recovery **recommends** and never deletes: this engine has no delete it can prove
            # correct, so neither does its recovery. The operator decides.
            findings.append(RecoveryFinding(
                item.clip_id, recorded, observed, "operator_may_remove_duplicate",
                "both copies match the approved content fingerprint; this engine will not delete "
                "either, because it cannot prove a pathname delete removes the verified object"))
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
    "REPARSE_NO",
    "REPARSE_UNKNOWN",
    "REPARSE_YES",
    "ROLLED_BACK",
    "RecoveryFinding",
    "RecoveryReport",
    "SOURCE_REMOVAL_BLOCKED",
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
    "reparse_state",
    "staging_leftovers",
]
