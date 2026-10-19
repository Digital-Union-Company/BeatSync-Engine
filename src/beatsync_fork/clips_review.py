#!/usr/bin/env python3
"""Clips Preview Selector (R5) — the secure core.

Everything a post-cut review session needs that is *not* HTTP and *not* Gradio: enumeration,
opaque identity, per-session authorization, read-only path-relationship validation and the durable
decision record. Stdlib only, per ``beatsync_fork``'s hard rule, so the whole security model is
testable on a bare interpreter with no portable runtime, no browser and no FFmpeg.

The one idea the rest of the feature rests on: **a filesystem path never travels through the
browser.** The server mints a random opaque id per enumerated file and keeps the path in a
server-side map. A request carries only `(owner, session, page, clip)` tokens, so no URL string is
ever joined to a path and traversal is impossible by construction rather than by filtering.

What the identity check does and does not promise
-------------------------------------------------
A clip's identity is ``(absolute real path, size, mtime_ns, st_dev, st_ino)``, captured at
enumeration and revalidated **on the opened file descriptor** before any byte is served. That
detects: the path being replaced by a different file (different ``st_ino``), truncation or growth
(size), and ordinary rewrites (mtime). It is **not** a content hash, so a same-size edit that also
restores the original mtime and inode -- deliberate tampering, not an accident -- is *not* detected.
Hashing every clip on every request would re-read gigabytes per page, which is why it is not done;
the guarantee is stated here rather than overclaimed. Nothing in R5-P1 mutates a file, so the
consequence of the residual case is a stale preview, never a wrong move.

Who an "owner" is, stated accurately
------------------------------------
[R5-P3 / F3] An owner key is the Gradio browser session hash. ``gr.Request`` *injects* it into the
handler -- it is not a widget and is not listed in any click's ``inputs`` -- but the **value
originates in the Gradio client**: ``gradio/route_utils.py`` reads it from the request body, and
the browser mints it with ``Math.random().toString(36).substring(2)``, which is not a CSPRNG. It
is therefore **not** a cryptographically authenticated operator identity, and a non-conforming
local client can choose any owner key it likes.

What does *not* depend on it: the HTTP media layer authorizes on server-minted
``secrets.token_urlsafe`` session, page and clip identifiers and recovers the owner server-side
via :meth:`ClipRegistry.owner_of`, so no owner identity is ever read from an HTTP request. The
owner key scopes one browser's review session against another's; it is not an authentication
boundary. The application remains localhost-only and claims no multi-user authentication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat as stat_module
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------------------------

PENDING = "PENDING"
SELECTED = "SELECTED"
REJECTED = "REJECTED"
DECISIONS = (PENDING, SELECTED, REJECTED)

#: Exactly the Raw Cutter's own source suffixes. R5 reviews what the cutter produces, so the two
#: must accept the same set; see ``raw_cutter_export.SUPPORTED_SOURCE_SUFFIXES``.
SERVABLE_SUFFIXES = (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")

#: Opaque tokens are URL-safe base64. Anything outside this alphabet is refused before lookup, so
#: a drive letter, a separator or a percent-escape never reaches a dictionary key.
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

#: Bounds. A review session is one operator looking at one folder; these exist so a long-lived
#: process cannot accumulate unbounded page maps or file identities.
MAX_OWNERS = 32
MAX_SESSIONS_PER_OWNER = 2
MAX_PAGES_PER_SESSION = 3
MAX_CLIPS_PER_PAGE = 500
COMPACT_AFTER_RECORDS = 2000


class Refusal(Exception):
    """A request that must not be served. ``reason`` is a stable machine token, safe to return."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = int(status)
        self.reason = str(reason)


class PersistenceError(Exception):
    """A decision could not be durably recorded. The caller must report, never pretend success."""


class SupersededScan(Exception):
    """[R5-P3 / F1] A newer scan for this owner has already started.

    Raised by :class:`ScanCoordinator` when an older generation tries to change authoritative
    state. The older scan must abandon its result -- it must **not** fall back to publishing it,
    and it must not hand the browser markup for the page it built, because that page's session was
    revoked the moment the newer scan opened its own.
    """

    def __init__(self, generation: int, newest: int):
        super().__init__(f"scan generation {generation} superseded by {newest}")
        self.generation = int(generation)
        self.newest = int(newest)


#: [R5-P3 / F2] Stable refusal codes for the mandatory expected-revision contract.
REVISION_REQUIRED = "revision_required"
MALFORMED_REVISION = "malformed_revision"
REVISION_CONFLICT = "revision_conflict"


class RevisionError(Exception):
    """[R5-P3 / F2] The client's expected revision is absent or not a usable integer.

    Separate from :class:`StaleWrite` on purpose: a *conflict* means the client's view is merely
    out of date, while this means the client did not state a view at all. Collapsing the two is
    exactly the defect this type exists to prevent -- the original code coerced every malformed
    revision to ``None``, which the store reads as "no concurrency check requested".
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code)


def parse_expected_revision(payload: Any) -> int:
    """[R5-P3 / F2] The strict, mandatory expected revision. **Fails closed.**

    A decision must state which revision it believes it is replacing. Accepted: a genuine,
    non-negative JSON integer, and nothing else.

    Rejected, each with a stable code rather than a silent downgrade to "no check":

    * a missing ``rev`` key, and ``None``                      -> ``revision_required``
    * a string, including a numeric one like ``"3"``           -> ``malformed_revision``
    * a bool -- ``True`` is an ``int`` subclass in Python, so it is excluded explicitly, or
      ``{"rev": true}`` would be read as revision 1
    * a float, including an integral one like ``3.0``, because JSON cannot distinguish an intended
      integer from a truncation artefact; ``0.9`` previously became ``0``
    * a list, dict or any other type
    * a negative integer

    The failure direction is the whole point. ``int(payload.get('rev'))`` inside a
    ``try/except (TypeError, ValueError)`` that assigns ``None`` turns every malformed value into
    an explicit request to skip the concurrency check, which is a fail-*open*.
    """
    if not isinstance(payload, dict) or "rev" not in payload:
        raise RevisionError(REVISION_REQUIRED)
    raw = payload["rev"]
    if raw is None:
        raise RevisionError(REVISION_REQUIRED)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RevisionError(MALFORMED_REVISION)
    if raw < 0:
        raise RevisionError(MALFORMED_REVISION)
    return int(raw)


# --------------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------------

def decision_identity(path: str, size: int, mtime_ns: int) -> str:
    """The key a decision is stored against.

    Folds size and mtime in deliberately: a file that changed since it was classified hashes to a
    different key, so its old decision simply does not apply any more. Invalidation is therefore
    structural rather than a check somebody has to remember to run.
    """
    key = f"{os.path.normcase(os.path.realpath(path))}|{int(size)}|{int(mtime_ns)}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _valid_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_TOKEN_RE.match(value))


def _require_owner(owner: Any) -> str:
    """Owner identity is the Gradio session hash. Absent or malformed identity **fails closed**:
    there is deliberately no shared anonymous registry to fall back to."""
    if not isinstance(owner, str) or not owner.strip():
        raise Refusal(403, "no_session_identity")
    text = owner.strip()
    if len(text) > 128 or not re.match(r"\A[A-Za-z0-9_.:-]+\Z", text):
        raise Refusal(403, "malformed_session_identity")
    return text


@dataclass(frozen=True)
class ClipEntry:
    """One enumerated file, frozen at scan time. ``path`` never leaves the process."""

    clip_id: str
    index: int
    path: str
    size: int
    mtime_ns: int
    dev: int
    ino: int
    suffix: str

    @property
    def label(self) -> str:
        """The only name the browser is ever told. Never the basename."""
        return f"Clip {self.index:04d}"


@dataclass
class Page:
    page_token: str
    page_index: int
    clips: "OrderedDict[str, ClipEntry]" = field(default_factory=OrderedDict)
    revoked: bool = False
    created_at: float = 0.0


@dataclass
class Session:
    session_id: str
    owner: str
    created_at: float
    pages: "OrderedDict[str, Page]" = field(default_factory=OrderedDict)
    active_page_token: str | None = None
    revoked: bool = False
    scan_id: str = ""


# --------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------

class ClipRegistry:
    """Server-authoritative map from opaque ids to enumerated files, **scoped per owner**.

    The P0 prototype kept one process-global session and revoked *everything* on a new scan. That
    is wrong for a server that can have two browser tabs open: one operator rescanning would have
    silently destroyed the other's authorization. Here every session belongs to an owner (the
    Gradio session hash) and a rescan revokes only that owner's sessions.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owners: "OrderedDict[str, OrderedDict[str, Session]]" = OrderedDict()
        #: session_id -> owner. The HTTP layer authorizes on the server-minted session id and the
        #: owner is looked up HERE, so no owner identity is ever taken from a request. A browser
        #: cannot name a session it was not given, and cannot claim to be another owner.
        self._session_owner: dict[str, str] = {}
        self.serve_count = 0
        self.refusals: list[tuple[str, str]] = []

    # ---- lifecycle -------------------------------------------------------------------

    def new_session(self, owner: Any, scan_id: str = "") -> str:
        """Open a review session for ``owner``, revoking that owner's previous sessions only."""
        key = _require_owner(owner)
        with self._lock:
            sessions = self._owners.get(key)
            if sessions is None:
                sessions = OrderedDict()
                self._owners[key] = sessions
                while len(self._owners) > MAX_OWNERS:
                    _, dropped = self._owners.popitem(last=False)
                    for session in dropped.values():
                        self._mark_revoked(session)
            self._owners.move_to_end(key)
            for session in sessions.values():
                self._mark_revoked(session)
            session_id = secrets.token_urlsafe(18)
            sessions[session_id] = Session(session_id=session_id, owner=key,
                                           created_at=time.time(), scan_id=str(scan_id))
            self._session_owner[session_id] = key
            while len(sessions) > MAX_SESSIONS_PER_OWNER:
                _, old = sessions.popitem(last=False)
                self._mark_revoked(old)
            return session_id

    def _mark_revoked(self, session: Session) -> None:
        session.revoked = True
        self._session_owner.pop(session.session_id, None)
        for page in session.pages.values():
            page.revoked = True
            page.clips.clear()          # drop the path map: a revoked page holds no identities

    def publish_page(self, owner: Any, session_id: str, page_index: int,
                     files: Iterable[tuple[int, str, int, int, int, int]]
                     ) -> tuple[str, list[ClipEntry]]:
        """Mint one page of clip ids, revoking the session's previous page.

        ``files`` is ``[(index, abs_path, size, mtime_ns, dev, ino)]`` in the caller's already
        frozen enumeration order. This function never scans, sorts or re-reads the filesystem.
        """
        key = _require_owner(owner)
        with self._lock:
            session = self._session(key, session_id)
            if session.active_page_token:
                previous = session.pages.get(session.active_page_token)
                if previous is not None:
                    previous.revoked = True
                    previous.clips.clear()
            token = secrets.token_urlsafe(18)
            page = Page(page_token=token, page_index=int(page_index), created_at=time.time())
            entries: list[ClipEntry] = []
            for index, path, size, mtime_ns, dev, ino in files:
                suffix = os.path.splitext(path)[1].casefold()
                if suffix not in SERVABLE_SUFFIXES:
                    # Defence in depth. Discovery already filters, but a servable set must never
                    # be able to contain a manifest, a sidecar or anything else non-media.
                    continue
                if len(entries) >= MAX_CLIPS_PER_PAGE:
                    break
                entry = ClipEntry(clip_id=secrets.token_urlsafe(16), index=int(index),
                                  path=path, size=int(size), mtime_ns=int(mtime_ns),
                                  dev=int(dev), ino=int(ino), suffix=suffix)
                page.clips[entry.clip_id] = entry
                entries.append(entry)
            session.pages[token] = page
            session.active_page_token = token
            while len(session.pages) > MAX_PAGES_PER_SESSION:
                _, old = session.pages.popitem(last=False)
                old.revoked = True
                old.clips.clear()
            return token, entries

    def revoke_owner(self, owner: Any) -> None:
        key = _require_owner(owner)
        with self._lock:
            for session in self._owners.pop(key, OrderedDict()).values():
                self._mark_revoked(session)

    def _session(self, owner: str, session_id: Any) -> Session:
        if not _valid_token(session_id):
            raise Refusal(403, "malformed_token")
        sessions = self._owners.get(owner)
        if sessions is None:
            raise Refusal(403, "unknown_session")
        session = sessions.get(session_id)
        if session is None:
            raise Refusal(403, "unknown_session")
        if session.revoked:
            raise Refusal(403, "revoked_session")
        return session

    def _session_by_id(self, session_id: Any) -> Session:
        """Look a session up by its server-minted id and recover its owner **server-side**.

        This is what the HTTP layer uses. The owner is never read from a request header, cookie or
        URL segment, so a browser cannot assert whose session it is holding -- it can only present
        an unguessable id that the server already bound to one owner.
        """
        if not _valid_token(session_id):
            raise Refusal(403, "malformed_token")
        with self._lock:
            owner = self._session_owner.get(session_id)
            if owner is None:
                raise Refusal(403, "unknown_session")
            return self._session(owner, session_id)

    def owner_of(self, session_id: Any) -> str:
        return self._session_by_id(session_id).owner

    # ---- authorization ---------------------------------------------------------------

    def resolve(self, session_id: Any, page_token: Any, clip_id: Any) -> ClipEntry:
        """Six independent gates, none of which touches the filesystem."""
        if not (_valid_token(page_token) and _valid_token(clip_id)):
            raise Refusal(403, "malformed_token")
        with self._lock:
            session = self._session_by_id(session_id)
            page = session.pages.get(page_token)
            if page is None:
                raise Refusal(403, "unknown_page_token")
            if page.revoked:
                raise Refusal(403, "expired_page_token")
            if not secrets.compare_digest(page.page_token, page_token):
                raise Refusal(403, "page_token_mismatch")
            entry = page.clips.get(clip_id)
            if entry is None:
                raise Refusal(403, "clip_not_in_page")
            return entry

    def page_entries(self, session_id: Any, page_token: Any) -> list[ClipEntry]:
        if not _valid_token(page_token):
            raise Refusal(403, "malformed_token")
        with self._lock:
            session = self._session_by_id(session_id)
            page = session.pages.get(page_token)
            if page is None or page.revoked:
                raise Refusal(403, "unknown_or_expired_page")
            return list(page.clips.values())

    def open_authorized(self, session_id: Any, page_token: Any,
                        clip_id: Any) -> tuple[int, ClipEntry]:
        """Authorize, then open, then **revalidate the opened descriptor**.

        Checking the path and *then* opening it is a time-of-check/time-of-use gap: between the
        two, the name can be repointed at another file by a rename or a reparse point. So the
        decisive comparison is made with ``os.fstat`` on the handle that will actually be read --
        the same object, not the same name. Returns an owned fd; the caller must close it.
        """
        entry = self.resolve(session_id, page_token, clip_id)
        return self.open_entry(entry)

    @staticmethod
    def open_entry(entry: ClipEntry) -> tuple[int, ClipEntry]:
        """Open an already-resolved entry and revalidate the **descriptor**, not the pathname.

        One implementation, used by both the serve path and the decision path, so the two can
        never drift into checking different things.
        """
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(entry.path, flags)
        except OSError:
            raise Refusal(410, "source_unavailable") from None
        try:
            info = os.fstat(fd)
            if not stat_module.S_ISREG(info.st_mode):
                raise Refusal(403, "not_a_regular_file")
            if info.st_size != entry.size or info.st_mtime_ns != entry.mtime_ns:
                raise Refusal(409, "identity_changed")
            # st_ino/st_dev from a Windows handle come from GetFileInformationByHandle, so a
            # swapped file is caught even when size and mtime were copied across.
            if entry.ino and info.st_ino and info.st_ino != entry.ino:
                raise Refusal(409, "identity_changed")
            if entry.dev and info.st_dev and info.st_dev != entry.dev:
                raise Refusal(409, "identity_changed")
        except BaseException:
            os.close(fd)
            raise
        return fd, entry

    def live_identity_ok(self, entry: ClipEntry) -> bool:
        """Non-throwing identity probe, for the decision path (which serves no bytes)."""
        try:
            fd, _ = self.open_entry(entry)
        except Refusal:
            return False
        os.close(fd)
        return True

    # ---- telemetry -------------------------------------------------------------------

    def note_refusal(self, reason: str, detail: str = "") -> None:
        with self._lock:
            self.refusals.append((str(reason), str(detail)[:200]))
            del self.refusals[:-200]

    def note_serve(self) -> None:
        with self._lock:
            self.serve_count += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                "owners": len(self._owners),
                "sessions": sum(len(s) for s in self._owners.values()),
                "live_pages": sum(1 for s in self._owners.values() for sess in s.values()
                                  for p in sess.pages.values() if not p.revoked),
                "served": self.serve_count,
                "refusals": list(self.refusals[-40:]),
            }


# --------------------------------------------------------------------------------------------
# Scan lifecycle  [R5-P3 / F1]
# --------------------------------------------------------------------------------------------

class _ScanAuthority:
    """The critical section in which one scan may change authoritative state.

    Entering re-checks the generation **while holding the owner's lock** and raises
    :class:`SupersededScan` if a newer scan has started. That ordering is the fix: the original
    code had no critical section at all, so "am I still newest?" and "revoke the previous session"
    were two separate moments with a window between them.
    """

    def __init__(self, coordinator: "ScanCoordinator", owner: str, generation: int):
        self._coordinator = coordinator
        self._owner = owner
        self._generation = int(generation)
        self._lock = None

    def __enter__(self) -> int:
        self._lock = self._coordinator._owner_lock(self._owner)
        self._lock.acquire()
        try:
            newest = self._coordinator.newest(self._owner)
            if newest != self._generation:
                raise SupersededScan(self._generation, newest)
        except BaseException:
            self._lock.release()
            self._lock = None
            raise
        return self._generation

    def __exit__(self, *exc_info) -> bool:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        return False


class ScanCoordinator:
    """Per-owner monotonic scan generations plus the authoritative per-owner view.

    The defect this replaces (independent review F1): ``_on_clips_scan`` ran
    ``begin_scan -> new_session -> publish_page -> ensure_posters -> remember`` with no lock
    spanning it and no notion of which scan was newest. ``new_session`` revokes the owner's
    previous sessions, and the poster pass takes seconds, so an older scan finishing last
    republished its own already-revoked view -- leaving every tile answering ``403
    unknown_session`` until the operator scanned again. Reachable with a double-click, because
    the Gradio event carries ``concurrency_limit=None`` (no limit).

    Two things are needed and neither alone is sufficient:

    * a **generation** taken before any authoritative state changes, so "newest" is knowable
      even while the newest scan is still running -- comparing against the last *completed* view
      would not see a scan that has started and not yet finished;
    * a **critical section** (:meth:`authority`) in which the generation check and the
      authority-changing calls happen together.

    Publication is then conditional: :meth:`publish` stores a view only if its generation is
    still the newest, so a late result is discarded rather than installed.

    The owner's lock is held only across the fast authoritative calls. The poster pass runs
    outside it, and no render lock or concurrency group is involved -- a read-only gallery is not
    a render.
    """

    def __init__(self, max_owners: int = MAX_OWNERS) -> None:
        self._lock = threading.RLock()
        self._max_owners = max(1, int(max_owners))
        self._counter = 0
        self._newest: "OrderedDict[str, int]" = OrderedDict()
        self._views: "OrderedDict[str, dict]" = OrderedDict()
        self._locks: dict[str, threading.RLock] = {}
        self.superseded_scans = 0

    # ---- generations -----------------------------------------------------------------

    def begin(self, owner: Any) -> int:
        """Take the next generation for ``owner`` and record it as the newest started scan.

        Call this only once a scan is actually going to change state: a scan refused for a bad
        folder or an empty enumeration never takes a generation, so it cannot supersede a running
        one.
        """
        key = _require_owner(owner)
        with self._lock:
            self._counter += 1
            generation = self._counter
            self._newest[key] = generation
            self._newest.move_to_end(key)
            self._prune()
            return generation

    def newest(self, owner: Any) -> int:
        key = _require_owner(owner)
        with self._lock:
            return self._newest.get(key, 0)

    @staticmethod
    def _strict_generation(generation: Any) -> int | None:
        """A generation must be a genuine ``int``, never something coerced into one.

        Same reasoning as :func:`parse_expected_revision`, and for the same reason: ``int("1")``
        is 1 and ``int(True)`` is 1, so a coercing comparison would let a value that is not a
        generation at all match the newest one. Generations are minted internally, so this is
        defence in depth rather than input validation -- but a coercion here would be the exact
        shape of the defect this class exists to close.
        """
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        return int(generation)

    def is_current(self, owner: Any, generation: Any) -> bool:
        wanted = self._strict_generation(generation)
        if wanted is None:
            return False
        try:
            return self.newest(owner) == wanted
        except Refusal:
            return False

    def authority(self, owner: Any, generation: Any) -> _ScanAuthority:
        """Critical section for the authority-changing part of a scan. See :class:`_ScanAuthority`."""
        wanted = self._strict_generation(generation)
        if wanted is None:
            raise SupersededScan(-1, self.newest(owner))
        return _ScanAuthority(self, _require_owner(owner), wanted)

    def _owner_lock(self, key: str) -> threading.RLock:
        """One lock per owner, so two operators never block each other."""
        with self._lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.RLock()
            return lock

    def _prune(self) -> None:
        while len(self._newest) > self._max_owners:
            dropped, _ = self._newest.popitem(last=False)
            self._views.pop(dropped, None)
            self._locks.pop(dropped, None)

    # ---- the authoritative view ------------------------------------------------------

    def publish(self, owner: Any, generation: Any, view: dict) -> bool:
        """Install ``view`` as the owner's current page, **only if** still the newest generation.

        Returns ``False`` for a superseded scan, whose result is then discarded entirely.
        """
        key = _require_owner(owner)
        wanted = self._strict_generation(generation)
        if wanted is None:
            return False
        with self._lock:
            if self._newest.get(key) != wanted:
                self.superseded_scans += 1
                return False
            stored = dict(view)
            stored["generation"] = wanted
            self._views[key] = stored
            self._views.move_to_end(key)
            while len(self._views) > self._max_owners:
                self._views.popitem(last=False)
            return True

    def view(self, owner: Any) -> dict:
        """The owner's current page, or ``{}``. Never raises for an unknown owner.

        [R5-P9 / N4] **A stored view is current only while its own generation is still the
        newest.** The window this closes is structural rather than incidental: :meth:`begin`
        makes a new scan the newest *before* that scan enters :meth:`authority` and calls
        ``new_session``, which revokes the owner's previous session -- and the poster pass between
        that revocation and :meth:`publish` takes seconds. For that whole interval the stored view
        named a session the registry had already revoked, so every caller of this method
        (``_clips_view``, Refresh, and the ``/r5state`` read) described a dead page as current and
        every tile in the repainted grid answered ``403 unknown_session``.

        A transition therefore reads as **empty**, which is the honest answer: there is no current
        page until the newest scan publishes one. That is deliberately the same shape as "no scan
        yet" -- an empty gallery is a state the operator can act on, whereas a page of revoked
        credentials is not.

        Two properties this must not break, and does not:

        * a scan refused *before* :meth:`begin` takes a generation never moves ``_newest``, so a
          bad folder or an empty enumeration cannot blank an otherwise valid current view;
        * a newest scan that fails still calls :meth:`discard`, and this check additionally means
          an older page cannot look current even if that discard were ever missed.
        """
        try:
            key = _require_owner(owner)
        except Refusal:
            return {}
        with self._lock:
            view = self._views.get(key)
            if view is None:
                return {}
            if view.get("generation") != self._newest.get(key):
                # Superseded by a scan that has started and has not published yet.
                return {}
            self._views.move_to_end(key)
            return view

    def discard(self, owner: Any, generation: Any) -> bool:
        """Clear the owner's view if ``generation`` is still the newest.

        For a newest scan that **failed**: the operator must be left with an honest empty state,
        never with an older page silently resurrected as though it were current.
        """
        key = _require_owner(owner)
        wanted = self._strict_generation(generation)
        if wanted is None:
            return False
        with self._lock:
            if self._newest.get(key) != wanted:
                return False
            self._views.pop(key, None)
            return True

    def forget(self, owner: Any) -> None:
        key = _require_owner(owner)
        with self._lock:
            self._newest.pop(key, None)
            self._views.pop(key, None)
            self._locks.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {"owners": len(self._newest), "views": len(self._views),
                    "generations_issued": self._counter,
                    "superseded_scans": self.superseded_scans}


# --------------------------------------------------------------------------------------------
# Range parsing
# --------------------------------------------------------------------------------------------

MAX_RANGE_HEADER = 256


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive ``(start, end)``, ``None`` for a full body, or raise ``Refusal(416)``.

    Deliberately strict: one ``bytes=`` range only. RFC 9110 says an *unparsable* Range must be
    treated as absent, while a parsable-but-unsatisfiable one is a real 416 -- those two cases are
    different and are kept different here. Multipart ranges are refused rather than
    half-supported.
    """
    if not header:
        return None
    if len(header) > MAX_RANGE_HEADER:
        raise Refusal(416, "range_header_too_long")
    text = header.strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[6:].strip()
    if "," in spec:
        raise Refusal(416, "multipart_range_unsupported")
    if "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix <= 0:
                raise Refusal(416, "unsatisfiable_suffix_range")
            return (max(0, size - suffix), max(0, size - 1))
        start = int(first)
        if start < 0:
            return None
        end = size - 1 if last == "" else int(last)
        if end < 0:
            raise Refusal(416, "negative_range_end")
    except ValueError:
        return None
    if size == 0:
        raise Refusal(416, "empty_resource")
    if start >= size:
        raise Refusal(416, "range_start_beyond_eof")
    if end < start:
        raise Refusal(416, "range_end_before_start")
    return (start, min(end, size - 1))


_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".webm": "video/webm",
}


def content_type_for(suffix: str) -> str:
    return _CONTENT_TYPES.get(str(suffix).casefold(), "application/octet-stream")


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Discovery:
    root: str = ""
    recursive: bool = True
    files: tuple = ()
    rejected: tuple = ()
    discovered_count: int = 0
    scan_seconds: float = 0.0
    error: str = ""


def discover_clips(folder: Any, recursive: Any) -> Discovery:
    """Enumerate reviewable clips in the Raw Cutter's own deterministic order.

    This does **not** walk the tree itself. It calls the same
    ``input_manager.scan_folder`` that ``raw_cutter_export.discover_batch_sources`` calls, with the
    same suffix set, so R5 and the cutter cannot disagree about "the files, in order" -- and the
    manifest the cutter leaves in its output folder is excluded for free, because
    ``batch_manifest.json`` is not a supported extension.
    """
    from beatsync_fork import input_manager

    if not isinstance(folder, str) or not folder.strip():
        return Discovery(recursive=bool(recursive), error="Enter the folder that holds the clips.")
    root = os.path.abspath(os.path.expandvars(os.path.expanduser(folder.strip().strip('"'))))
    if not os.path.exists(root):
        return Discovery(root=root, recursive=bool(recursive), error=f"Folder not found:\n{root}")
    if not os.path.isdir(root):
        return Discovery(root=root, recursive=bool(recursive), error=f"Not a folder:\n{root}")
    try:
        found = input_manager.scan_folder(root, recursive=bool(recursive),
                                          extensions=SERVABLE_SUFFIXES,
                                          detect_duplicates=False)
    except input_manager.InputScanError as exc:
        return Discovery(root=root, recursive=bool(recursive), error=str(exc))
    except OSError as exc:
        return Discovery(root=root, recursive=bool(recursive),
                         error=f"Could not read the folder: {exc}")
    return Discovery(root=found.root, recursive=bool(recursive), files=found.files,
                     rejected=found.rejected, discovered_count=found.discovered_count,
                     scan_seconds=found.scan_seconds)


def freeze_page(files: Iterable[Any], start: int, size: int
                ) -> list[tuple[int, str, int, int, int, int]]:
    """Turn a slice of discovery into the tuples :meth:`ClipRegistry.publish_page` wants.

    ``st_dev``/``st_ino`` are captured here so the serve path can compare the *opened handle*
    against them. A file that cannot be stat'ed now is skipped rather than published with a
    guessed identity.
    """
    out: list[tuple[int, str, int, int, int, int]] = []
    for offset, media in enumerate(list(files)[start:start + size]):
        try:
            info = os.stat(media.path)
        except OSError:
            continue
        out.append((start + offset + 1, media.path, media.size, media.mtime_ns,
                    info.st_dev, info.st_ino))
    return out


# --------------------------------------------------------------------------------------------
# Poster frames
# --------------------------------------------------------------------------------------------

#: One cheap frame, scaled to grid size. `-ss` before `-i` seeks rather than decoding, which is
#: why the cost is independent of clip duration (measured: 73.6 ms for a 15 s 1080p clip and
#: 73.7 ms for a 30 s one).
POSTER_SEEK_SECONDS = 1.0
POSTER_WIDTH = 320
POSTER_TIMEOUT_SECONDS = 30
#: Bounded concurrency. Four is the ceiling, chosen because D0 traced clip-extraction timeouts to
#: four concurrent FFmpeg-class readers on one mechanical drive, so this must never exceed it.
POSTER_MAX_WORKERS = 4
#: The cache is bounded by count, not just by directory size, so a long-lived process cannot
#: accumulate posters for every folder an operator ever browsed.
POSTER_CACHE_MAX_FILES = 4000


def poster_key(path: str, size: int, mtime_ns: int) -> str:
    """Cache key. Folds size and mtime in, so a changed clip gets a new poster rather than a
    stale one from the file that used to be there."""
    raw = f"{os.path.normcase(path)}|{int(size)}|{int(mtime_ns)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def poster_path_for(cache_dir: str, entry: ClipEntry) -> str:
    return os.path.join(cache_dir, poster_key(entry.path, entry.size, entry.mtime_ns) + ".jpg")


#: [R5-P3 / F6] A *published* poster is exactly ``<32 hex>.jpg``. Generation attempts write
#: ``<32 hex>.<16 hex>.part.jpg`` in the same directory, so the two are never confused: a prune
#: pass will not delete somebody's in-flight attempt, and an abandoned attempt can never be
#: mistaken for a cache hit.
_PUBLISHED_POSTER_RE = re.compile(r"\A[0-9a-f]{32}\.jpg\Z")

#: Markers a complete JPEG must carry. Truncation is the realistic failure (a killed or
#: timed-out FFmpeg), and truncation removes the trailing marker, so checking both ends is what
#: distinguishes "finished" from "stopped half way".
_JPEG_SOI = b"\xff\xd8\xff"
_JPEG_EOI = b"\xff\xd9"


def poster_attempt_path(cache_dir: str, destination: str) -> str:
    """A unique, exclusively created attempt file beside ``destination``.

    Same directory, so publication is an ``os.replace`` on one filesystem and therefore atomic.
    ``O_EXCL`` means two concurrent attempts for the same cache key cannot pick the same name.
    """
    base = os.path.basename(destination)
    key = base[:-4] if base.endswith(".jpg") else base
    for _ in range(8):
        candidate = os.path.join(cache_dir, f"{key}.{secrets.token_hex(8)}.part.jpg")
        try:
            handle = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(handle)
        return candidate
    raise OSError("could not create a unique poster attempt file")


def is_complete_jpeg(path: str) -> bool:
    """[R5-P3 / F6] Whether ``path`` holds a *complete* JPEG, not merely a non-empty file.

    The original cache test was ``os.path.getsize(destination) > 0``, so a truncated poster left
    behind by a killed FFmpeg counted as a permanent hit and that clip showed a broken image
    forever. A size check cannot distinguish the two; the end-of-image marker can.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(_JPEG_SOI))
            if head != _JPEG_SOI:
                return False
            handle.seek(0, os.SEEK_END)
            if handle.tell() < len(_JPEG_SOI) + len(_JPEG_EOI):
                return False
            handle.seek(-len(_JPEG_EOI), os.SEEK_END)
            return handle.read(len(_JPEG_EOI)) == _JPEG_EOI
    except OSError:
        return False


def build_poster_command(ffmpeg: str, source: str, destination: str,
                         at_seconds: float = POSTER_SEEK_SECONDS) -> list[str]:
    """The one poster argv. Built here so the GUI never becomes a command-construction site.

    ``-f image2`` and ``-c:v mjpeg`` are explicit rather than inferred from the extension: the
    destination is an attempt file now, and a poster writer must never depend on filename guessing
    for what format it produces.
    """
    return [str(ffmpeg), "-v", "error", "-nostdin", "-y",
            "-ss", f"{float(at_seconds):.2f}", "-i", str(source),
            "-frames:v", "1", "-vf", f"scale={POSTER_WIDTH}:-2", "-q:v", "6",
            "-c:v", "mjpeg", "-f", "image2",
            str(destination)]


def run_poster_command(argv, timeout: int = POSTER_TIMEOUT_SECONDS) -> int:
    """Execute one already-built poster argv, windowless and bounded.

    Lives here rather than in `gui.py` because two separate suites pin the GUI's `subprocess.run`
    site count at exactly two (the delivery preview and the Director). A third one there would
    break an invariant that exists to keep command construction and process launching out of the
    view layer -- so the poster pass follows the same rule, and the GUI only hands over a path.
    `qwen_progress` already launches subprocesses from this package, so this is the established
    place for it.

    A failure is a return code, never an exception: a missing poster must leave the clip
    classifiable rather than break the page.
    """
    import subprocess

    try:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return int(completed.returncode)
    except (OSError, ValueError, subprocess.SubprocessError):
        return -1


@dataclass
class PosterOutcome:
    hits: int = 0
    generated: int = 0
    failures: int = 0
    seconds: float = 0.0
    workers: int = 1


def ensure_posters(entries: Iterable[ClipEntry], cache_dir: str, ffmpeg: str, runner,
                   workers: int = POSTER_MAX_WORKERS,
                   at_seconds: float = POSTER_SEEK_SECONDS) -> PosterOutcome:
    """Fill the poster cache for one page, with bounded concurrency.

    ``runner`` is an injected ``(argv, timeout) -> int`` so this whole path is testable without
    FFmpeg existing -- the same shape ``media_sample`` uses for its probe and decode callables.
    A failure is **not** an error: the tile falls back to "preview unavailable" and stays
    classifiable, because an undecodable clip still has to be reviewable.
    """
    from concurrent.futures import ThreadPoolExecutor

    os.makedirs(cache_dir, exist_ok=True)
    pending: list[tuple[ClipEntry, str]] = []
    outcome = PosterOutcome(workers=max(1, min(int(workers), POSTER_MAX_WORKERS)))
    for entry in entries:
        destination = poster_path_for(cache_dir, entry)
        # [R5-P3 / F6] A *complete* JPEG is a hit. An incomplete one is a miss and is
        # regenerated, which is how a poster truncated by an earlier killed FFmpeg heals.
        if is_complete_jpeg(destination):
            outcome.hits += 1
            continue
        pending.append((entry, destination))

    if not pending:
        return outcome

    started = time.perf_counter()

    def one(job: tuple[ClipEntry, str]) -> bool:
        """Generate into an attempt file, validate it, then publish atomically.

        [R5-P3 / F6] FFmpeg never writes the published name. Three consequences, each of which
        was a defect before: a killed process cannot leave a partial file at the cache path; a
        reader can never observe a half-written poster; and a failing attempt cannot delete a
        *valid* published poster, which the previous ``os.unlink(destination)`` did.
        """
        entry, destination = job
        try:
            attempt = poster_attempt_path(cache_dir, destination)
        except OSError:
            return False
        try:
            argv = build_poster_command(ffmpeg, entry.path, attempt, at_seconds)
            try:
                code = runner(argv, POSTER_TIMEOUT_SECONDS)
            except Exception:                              # noqa: BLE001 - a poster is optional
                code = -1
            if code != 0 or not is_complete_jpeg(attempt):
                return False
            try:
                os.replace(attempt, destination)
            except OSError:
                return False
            return True
        finally:
            # Only ever this attempt's own file. Another attempt's temporary belongs to it.
            try:
                os.unlink(attempt)
            except OSError:
                pass

    if outcome.workers == 1:
        results = [one(job) for job in pending]
    else:
        with ThreadPoolExecutor(max_workers=outcome.workers,
                                thread_name_prefix="r5poster") as pool:
            results = list(pool.map(one, pending))
    outcome.generated = sum(1 for ok in results if ok)
    outcome.failures = sum(1 for ok in results if not ok)
    outcome.seconds = time.perf_counter() - started
    prune_poster_cache(cache_dir)
    return outcome


def prune_poster_cache(cache_dir: str, limit: int = POSTER_CACHE_MAX_FILES) -> int:
    """Keep the cache bounded, oldest first. Returns how many files were removed.

    [R5-P3 / F6] Only *published* posters are candidates. An in-flight attempt file is owned by
    the attempt that created it, so pruning must not touch one.
    """
    try:
        names = [n for n in os.listdir(cache_dir) if _PUBLISHED_POSTER_RE.match(n)]
    except OSError:
        return 0
    if len(names) <= limit:
        return 0
    aged: list[tuple[float, str]] = []
    for name in names:
        full = os.path.join(cache_dir, name)
        try:
            aged.append((os.path.getmtime(full), full))
        except OSError:
            continue
    aged.sort()
    removed = 0
    for _, full in aged[:max(0, len(aged) - limit)]:
        try:
            os.unlink(full)
            removed += 1
        except OSError:
            continue
    return removed


# --------------------------------------------------------------------------------------------
# Path-relationship validation (read-only)
# --------------------------------------------------------------------------------------------

ROLE_INPUT = "input"
ROLE_LIBRARY = "library"
ROLE_REJECTED = "rejected"


@dataclass(frozen=True)
class RootIdentity:
    role: str
    typed: str
    real: str
    dev: int
    ino: int
    exists: bool
    is_reparse: bool


@dataclass(frozen=True)
class Verdict:
    ok: bool
    code: str = "ok"
    detail: str = ""


def _is_reparse(path: str) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes      # Windows only
    except (OSError, AttributeError):
        return False
    return bool(attrs & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_within(child: str, parent: str) -> bool:
    """``child`` is ``parent`` or lies beneath it. Uses ``commonpath``, never a string prefix, so
    ``C:\\media-evil`` is not treated as being inside ``C:\\media``."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return _norm(os.path.commonpath([c, p])) == p
    except ValueError:
        return False                                   # different drives


def identify_root(role: str, typed: Any) -> tuple[RootIdentity | None, Verdict]:
    """Canonicalise one operator-selected root. A root whose identity cannot be *proven* is
    refused -- including a reparse point that ``realpath`` could not resolve, which is the
    fail-closed path for the alias cases this environment cannot create for testing."""
    if not isinstance(typed, str) or not typed.strip():
        return None, Verdict(False, "empty_path", role)
    cleaned = os.path.expandvars(os.path.expanduser(typed.strip().strip('"')))
    try:
        absolute = os.path.abspath(cleaned)
        real = os.path.realpath(absolute, strict=False)
    except (OSError, ValueError) as exc:
        return None, Verdict(False, "unresolvable_path", f"{role}: {exc}")

    reparse = _is_reparse(absolute)
    if reparse and _norm(real) == _norm(absolute) and _is_reparse(real):
        # The link could not be followed to a different real location, so its true target is
        # unknown and every containment comparison below would be meaningless. Refuse.
        return None, Verdict(False, "unresolved_reparse_point", f"{role}: {absolute}")

    exists = os.path.isdir(real)
    dev = ino = -1
    if exists:
        try:
            info = os.stat(real)
            dev, ino = info.st_dev, info.st_ino
        except OSError as exc:
            return None, Verdict(False, "identity_unprovable", f"{role}: {exc}")
    elif role == ROLE_INPUT:
        return None, Verdict(False, "input_not_a_directory", real)

    return RootIdentity(role=role, typed=typed, real=real, dev=dev, ino=ino,
                        exists=exists, is_reparse=reparse), Verdict(True)


def _same_location(a: RootIdentity, b: RootIdentity) -> bool:
    if _norm(a.real) == _norm(b.real):
        return True
    # Catches any alias ``realpath`` leaves distinct -- hard links, and a ``subst`` drive on a
    # platform or Python build where ``realpath`` does not collapse it.
    if a.exists and b.exists and a.ino not in (-1, 0) and b.ino not in (-1, 0):
        return (a.dev, a.ino) == (b.dev, b.ino)
    return False


def protected_destinations(app_root: str) -> tuple[str, ...]:
    """Runtime locations a destination may never be, per the R5 specification."""
    if not app_root:
        return ()
    return (
        os.path.join(app_root, "bin"),
        os.path.join(app_root, "input", "video_analysis_cache"),
        os.path.join(app_root, "input", "audio"),
        os.path.join(app_root, "input", "video"),
        os.path.join(app_root, "input", "gradio_uploads"),
        os.path.join(app_root, "output"),
        os.path.join(app_root, "state"),
    )


def validate_roots(input_folder: Any, library_folder: Any, rejected_folder: Any,
                   app_root: str = "") -> tuple[Verdict, dict[str, RootIdentity]]:
    """Full read-only relationship check across the three roots.

    Nothing here creates, moves or removes anything -- not even a destination directory. The first
    refusal found is returned with a stable code.
    """
    ids: dict[str, RootIdentity] = {}
    for role, typed in ((ROLE_INPUT, input_folder), (ROLE_LIBRARY, library_folder),
                        (ROLE_REJECTED, rejected_folder)):
        ident, verdict = identify_root(role, typed)
        if not verdict.ok:
            return verdict, ids
        ids[role] = ident                              # type: ignore[assignment]

    inp, lib, rej = ids[ROLE_INPUT], ids[ROLE_LIBRARY], ids[ROLE_REJECTED]

    if _same_location(lib, rej):
        return Verdict(False, "destinations_identical", f"{lib.real} == {rej.real}"), ids
    if _same_location(inp, lib):
        return Verdict(False, "library_is_input", f"{lib.real} == {inp.real}"), ids
    if _same_location(inp, rej):
        return Verdict(False, "rejected_is_input", f"{rej.real} == {inp.real}"), ids

    for dest in (lib, rej):
        if _is_within(dest.real, inp.real):
            return Verdict(False, "destination_inside_input", f"{dest.role}: {dest.real}"), ids
        if _is_within(inp.real, dest.real):
            return Verdict(False, "input_inside_destination", f"{dest.role}: {dest.real}"), ids

    if _is_within(lib.real, rej.real) or _is_within(rej.real, lib.real):
        return Verdict(False, "destinations_nested", f"{lib.real} / {rej.real}"), ids

    for protected in protected_destinations(app_root):
        for root in (lib, rej):
            if _is_within(root.real, protected) or _is_within(protected, root.real):
                return Verdict(False, "protected_runtime_destination",
                               f"{root.role}: {root.real} vs {protected}"), ids

    return Verdict(True), ids


# --------------------------------------------------------------------------------------------
# Gallery markup
# --------------------------------------------------------------------------------------------

_BADGES = {PENDING: "?", SELECTED: "✓", REJECTED: "✗"}
_CLASSES = {PENDING: "r5-pending", SELECTED: "r5-selected", REJECTED: "r5-rejected"}


def tile_html(entry: ClipEntry, decision: str, revision: int, session_id: str,
              page_token: str, has_poster: bool) -> str:
    """One tile.

    Every interpolated value is escaped, and the only text that comes anywhere near the clip is
    the server-generated ``Clip 0001`` label -- the basename is never inserted, so a filename
    cannot carry markup or script into the page. Status is carried by a badge glyph as well as a
    colour class, so colour is not the only signal.
    """
    import html as _html

    decision = decision if decision in _CLASSES else PENDING
    clip = _html.escape(entry.clip_id, quote=True)
    label = entry.label
    poster_url = f"/r5poster/{session_id}/{page_token}/{entry.clip_id}"
    video_url = f"/r5clip/{session_id}/{page_token}/{entry.clip_id}"
    media = (f'<img class="r5-poster" loading="lazy" decoding="async" '
             f'src="{_html.escape(poster_url, quote=True)}" '
             f'alt="{_html.escape(label, quote=True)}">'
             if has_poster else
             '<div class="r5-nopreview"><span class="r5-nopreview-main">preview unavailable</span>'
             '<span class="r5-nopreview-sub">still classifiable</span></div>')
    return (
        f'<div class="r5-tile {_CLASSES[decision]}" tabindex="0" role="button"'
        f' data-clip="{clip}" data-rev="{int(revision)}"'
        f' data-video="{_html.escape(video_url, quote=True)}"'
        f' aria-label="{_html.escape(label, quote=True)}, {decision.lower()}">'
        f'<div class="r5-media">{media}<div class="r5-slot"></div></div>'
        f'<div class="r5-bar"><span class="r5-label">{_html.escape(label)}</span>'
        f'<span class="r5-badge" aria-hidden="true">{_BADGES[decision]}</span>'
        f'<button class="r5-reset" type="button" data-reset="{clip}"'
        f' aria-label="Mark {_html.escape(label, quote=True)} undecided"'
        f' title="Back to undecided">↺</button></div></div>'
    )


def gallery_html(entries: Iterable[ClipEntry], decisions: dict[str, tuple[str, int]],
                 identities: dict[str, str], session_id: str, page_token: str,
                 poster_exists) -> str:
    """The whole grid.

    Carries a nonce because a ``gr.HTML`` update whose markup is byte-identical to the previous
    value is **not delivered** by Gradio 6.19.0 -- measured in R5-P0, where it made "re-read the
    server's record" look like it had done nothing at all.
    """
    tiles = []
    for entry in entries:
        identity = identities.get(entry.clip_id, "")
        decision, revision = decisions.get(identity, (PENDING, 0))
        tiles.append(tile_html(entry, decision, revision, session_id, page_token,
                               bool(poster_exists(entry))))
    if not tiles:
        return '<div class="r5-empty">Nothing to review on this page.</div>'
    nonce = secrets.token_hex(4)
    return (f'<div class="r5-grid" id="r5-grid" data-session="{session_id}"'
            f' data-page="{page_token}" data-nonce="{nonce}">{"".join(tiles)}</div>')


def counts_text(counts: dict[str, int]) -> str:
    return (f"Kept {counts.get(SELECTED, 0)}   ·   Discarded {counts.get(REJECTED, 0)}"
            f"   ·   Undecided {counts.get(PENDING, 0)}   ·   "
            f"Total {counts.get('TOTAL', 0)}")


# --------------------------------------------------------------------------------------------
# Durable decision record
# --------------------------------------------------------------------------------------------

class StaleWrite(Exception):
    """The caller's view is out of date; it must re-read before deciding again."""

    def __init__(self, code: str, current: str, revision: int):
        super().__init__(code)
        self.code = code
        self.current = current
        self.revision = int(revision)


def _io_detail(exc: BaseException) -> str:
    """[R5-P7] A numbers-only description of an I/O failure, safe to show a client.

    Every :class:`PersistenceError` raised by :class:`ReviewStore` reaches the browser: the
    middleware answers 503 with ``{"detail": str(exc)[:200]}``. ``str(OSError)`` appends
    ``filename`` -- and ``filename2`` -- whenever the failing call was made **by pathname**, so
    interpolating the exception text put the journal's absolute path in that body. An independent
    review reproduced it twice without fault injection: a read-only ACL on the journal, and
    another process holding it with no sharing, both of which make ``os.open`` fail with the path
    attached. The descriptor-based calls (``os.write``, ``os.fsync``) normally carry no filename,
    but they are formatted the same way here rather than relying on that.

    ``winerror`` is included when present because Windows collapses distinct conditions onto one
    errno -- a genuinely missing file and ``ERROR_BAD_NETPATH`` are both ``errno 2`` -- so the
    native code is the diagnosable one. The full exception, path included, stays reachable
    internally through ``raise ... from exc``.
    """
    number = getattr(exc, "errno", None)
    detail = f"errno {number}" if number is not None else "errno unknown"
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        detail += f", winerror {winerror}"
    return detail


def io_detail(exc: BaseException) -> str:
    """[R5-P9] The public spelling of :func:`_io_detail`, for callers outside this module.

    ``gui.py`` reports a failed scan and a failed optional compaction in the operator's status
    box. Those messages interpolated the exception, and a bare ``OSError`` stringifies with
    ``filename`` -- and ``filename2`` -- attached, so the journal or snapshot path reached the
    screen by exactly the route the 503 body was corrected for. The rule is one rule, so it has
    one implementation rather than a second copy in the view layer.

    ``PersistenceError`` and :class:`Refusal` are *not* passed through here: their own messages
    are already path-free by construction and say more than a number does.
    """
    return _io_detail(exc)


@dataclass(slots=True)
class Record:
    identity: str
    decision: str
    revision: int
    scan_id: str
    updated_at: float


class ReviewStore:
    """Append-only JSONL journal plus a compacting snapshot.

    Chosen over SQLite on grounds other than speed (both are well under a millisecond per
    decision): it is stdlib-only, so the core stays importable on a bare interpreter; and the
    later transfer phase needs *intent before the mutation, outcome after*, which is natively an
    append-only log rather than a row to update.
    """

    def __init__(self, directory: str) -> None:
        self.dir = os.path.abspath(directory)
        os.makedirs(self.dir, exist_ok=True)
        self.journal_path = os.path.join(self.dir, "review-journal.jsonl")
        self.snapshot_path = os.path.join(self.dir, "review-snapshot.json")
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._scans: dict[str, str] = {}               # owner -> current scan id
        self.corrupt_records_discarded = 0
        self.journal_records = 0
        #: [R5-P3 / F4] How many times an unterminated tail was closed off before appending.
        self.journal_tails_repaired = 0
        self._load()

    # ---- durability ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.snapshot_path):
            try:
                with open(self.snapshot_path, "r", encoding="utf-8") as handle:
                    snapshot = json.load(handle)
                for identity, raw in snapshot.get("records", {}).items():
                    self._records[identity] = Record(identity, raw["decision"],
                                                     int(raw["revision"]), raw.get("scan_id", ""),
                                                     float(raw.get("updated_at", 0.0)))
                self._scans.update(snapshot.get("scans", {}))
            except (OSError, ValueError, KeyError, TypeError):
                self.corrupt_records_discarded += 1     # unusable snapshot; the journal replays
        if not os.path.exists(self.journal_path):
            return
        try:
            with open(self.journal_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        # [R5-P3 / F4] An unparsable record is discarded rather than guessed at.
                        # A damaged line can only ever be a *single* line, because `_append`
                        # terminates an unterminated tail before writing after it -- so a later
                        # healthy record can no longer be swallowed by a partial one. Every
                        # fully committed record before the damage is preserved.
                        self.corrupt_records_discarded += 1
                        continue
                    self._apply(event)
                    self.journal_records += 1
        except OSError:
            self.corrupt_records_discarded += 1

    def _apply(self, event: dict) -> None:
        kind = event.get("k")
        if kind == "scan":
            owner = event.get("owner")
            if isinstance(owner, str):
                self._scans[owner] = str(event.get("scan_id", ""))
        elif kind == "decide":
            identity = event.get("identity")
            if not isinstance(identity, str):
                return
            self._records[identity] = Record(identity, str(event.get("decision", PENDING)),
                                             int(event.get("revision", 0)),
                                             str(event.get("scan_id", "")),
                                             float(event.get("at", 0.0)))

    def _write_all(self, handle: int, payload: bytes) -> int:
        """[R5-P3 / F4] Write every byte, or raise. Returns the number of bytes written.

        ``os.write`` is permitted to accept fewer bytes than offered and say so in its return
        value, with no exception. The original code discarded that return value, so a positive
        short write left a truncated record on disk while the API reported the decision recorded
        -- the one thing the durability contract says must never happen.

        [R5-P7] The byte count stays in the message -- it is the diagnostically useful part --
        but the exception's own text does not; see :func:`_io_detail`.
        """
        written = 0
        while written < len(payload):
            try:
                count = os.write(handle, payload[written:])
            except OSError as exc:
                raise PersistenceError(
                    f"could not durably write the decision after {written} byte(s) "
                    f"({_io_detail(exc)})") from exc
            if count is None or count <= 0:
                # A zero-byte write is not progress; looping on it would spin forever.
                raise PersistenceError(
                    f"the review journal accepted no bytes after {written} of {len(payload)}")
            written += count
        return written

    def _journal_tail_is_terminated(self, open_handle: int | None = None) -> bool:
        """Whether the journal is empty or ends in a newline. Raises if it cannot be determined.

        Pass ``open_handle`` when an append is in flight. It is the descriptor :meth:`_append`
        already holds, and it is **evidence, not a flag**: a caller that owns an open journal
        cannot truthfully claim the journal is absent, so there is nothing for a caller to get
        wrong by mistake. It is not read through -- the inspection below is still by pathname.

        [R5-P5 / N2] An ``OSError`` here is **not an answer**, and the original code read it as
        one: it returned ``True`` -- "already terminated" -- so :meth:`_terminate_damaged_tail`
        skipped the repair and the append landed directly against a possibly damaged tail. That
        reinstated the exact F4 defect the repair exists to prevent: the two lines merged into one
        unparsable record, the API reported the decision recorded, and it recovered as ``PENDING``
        after a restart.

        The comment that justified it -- "``_load`` already treats that as corruption" -- was
        reasoning about the wrong moment. ``_load``'s tolerance is for *start-up recovery*, where
        discarding an unreadable record is the only sensible option and nothing is being promised
        to anyone. Here the consequence is a *false success at append time*, which the durability
        contract forbids outright.

        So this fails **closed**. If the tail cannot be inspected, the journal's state is unknown,
        and an unknown journal state is not a licence to append -- nor to assume damage and write
        a repair newline into a file whose condition could not be read.

        [R5-P7 / N6] **A missing journal is only an answer when no append is in flight, and
        ``FileNotFoundError`` does not mean "missing".** R5-P5 carved out ``FileNotFoundError``
        and justified it with "``_append`` opens with ``O_CREAT`` before calling this, so the
        production path never sees it". That reasoning was wrong in both directions, and an
        independent review measured both halves:

        * ``FileNotFoundError`` is the class Python maps **six** Windows codes onto --
          ``ERROR_FILE_NOT_FOUND`` (2), ``ERROR_PATH_NOT_FOUND`` (3), ``ERROR_INVALID_DRIVE``
          (15), ``ERROR_BAD_NETPATH`` (53), ``ERROR_BAD_NET_NAME`` (67) and
          ``ERROR_BAD_PATHNAME`` (161). Five of those say "the pathname did not resolve", not
          "the file is not there": a share that stopped answering or a volume that went away
          produces them while the journal is still sitting on disk with a torn tail. The
          carve-out read all six as proof of a terminated tail, so the false success came back
          for every one of them, through all three I/O steps below.
        * The one code it was *designed* for is the one that cannot occur here. With the append
          descriptor open, Windows refuses to unlink or rename the file at all (sharing
          violation), and if the name had already gone, ``O_CREAT`` would have recreated it and
          ``getsize`` would answer 0 honestly.

        So the carve-out is now conditioned on the descriptor rather than on the error class.
        ``os.path.exists``/``lexists`` deliberately play no part: they answer "no" for an
        unresolvable path just as readily, which is the very confusion being removed.

        The messages carry numbers only -- see :func:`_io_detail`.
        """
        try:
            if os.path.getsize(self.journal_path) == 0:
                return True
            with open(self.journal_path, "rb") as probe:
                probe.seek(-1, os.SEEK_END)
                return probe.read(1) == b"\n"
        except OSError as exc:
            if open_handle is None and isinstance(exc, FileNotFoundError):
                # No append in flight, and nothing on disk: there is no tail to terminate and
                # nothing a later append could merge with. A definite answer, not a failed read.
                return True
            raise PersistenceError(
                f"could not verify the review journal tail ({_io_detail(exc)})") from exc

    def _terminate_damaged_tail(self, handle: int) -> bool:
        """[R5-P3 / F4] Close off a newline-less tail before appending after it.

        A newline-less final line means a previous append died part way through its record --
        either a short write that then failed, or the process dying mid-write. The original
        docstring claimed "a torn write can only damage the last line"; it does not. Because the
        journal is opened ``O_APPEND`` and a record is one line, the next append lands directly
        against the damaged bytes and the two become a single unparsable line, so a *later,
        fully successful* decision is silently destroyed too.

        Writing one newline first makes the damaged record a self-contained line that ``_load``
        discards, and leaves every fully committed record before it intact. Returns whether a
        repair was performed.

        [R5-P5 / N2] Raises :class:`PersistenceError` when the tail cannot be inspected at all --
        the repair is only safe to skip when the tail is *known* to be terminated.

        [R5-P7 / N6] ``handle`` is forwarded to the probe, which is what tells it an append is in
        flight. Every reader of this method holds an open journal, so no inspection failure
        reaching it -- ``FileNotFoundError`` included -- may be read as "terminated".
        """
        if self._journal_tail_is_terminated(handle):
            return False
        self._write_all(handle, b"\n")
        self.journal_tails_repaired += 1
        return True

    def _append(self, event: dict) -> None:
        """One record, fully written, then fsync'd.

        A failure here is raised, never swallowed: the caller must report that the decision was
        not durably recorded rather than show the operator a tick. A reported success means the
        complete record is on disk and recoverable.

        [R5-P5 / N2] That now includes not being able to *inspect* the journal: an unreadable tail
        raises before any byte is written, so no record is appended, the in-memory state is not
        advanced -- :meth:`decide` updates it only after this returns -- and the open handle is
        still closed by the ``finally`` below.

        [R5-P7 / N6] The descriptor opened here is passed down to the tail probe, and that is the
        whole guarantee: **once this function holds the journal open, no inspection failure of any
        class may be read as "the tail is terminated".** There is no remaining error for which the
        append proceeds against an unverified tail.
        """
        payload = (json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        try:
            handle = os.open(self.journal_path, flags, 0o600)
        except OSError as exc:
            raise PersistenceError(
                f"could not open the review journal ({_io_detail(exc)})") from exc
        try:
            self._terminate_damaged_tail(handle)
            self._write_all(handle, payload)
            try:
                os.fsync(handle)
            except OSError as exc:
                raise PersistenceError(
                    f"could not flush the review journal ({_io_detail(exc)})") from exc
        finally:
            os.close(handle)
        self.journal_records += 1

    def compact(self) -> None:
        """Snapshot atomically, then truncate the journal.

        Crash-safe in either order: the snapshot lands via ``os.replace``, and a journal that
        still holds records simply replays them onto an already-current snapshot, which is
        idempotent per identity.
        """
        with self._lock:
            snapshot = {
                "scans": dict(self._scans),
                "records": {i: {"decision": r.decision, "revision": r.revision,
                                "scan_id": r.scan_id, "updated_at": r.updated_at}
                            for i, r in self._records.items()},
            }
            handle, temporary = tempfile.mkstemp(dir=self.dir, prefix=".snapshot-", suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(snapshot, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.snapshot_path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            with open(self.journal_path, "w", encoding="utf-8"):
                pass
            self.journal_records = 0

    def maybe_compact(self) -> bool:
        with self._lock:
            if self.journal_records < COMPACT_AFTER_RECORDS:
                return False
        self.compact()
        return True

    # ---- scans -----------------------------------------------------------------------

    def begin_scan(self, owner: str, scan_id: str) -> None:
        """Register a new enumeration for one owner. Decisions quoting an earlier scan of the
        same owner become refusable; another owner's scan is unaffected."""
        key = _require_owner(owner)
        with self._lock:
            self._append({"k": "scan", "owner": key, "scan_id": str(scan_id), "at": time.time()})
            self._scans[key] = str(scan_id)

    def current_scan(self, owner: str) -> str:
        with self._lock:
            return self._scans.get(_require_owner(owner), "")

    # ---- decisions -------------------------------------------------------------------

    def get(self, identity: str) -> tuple[str, int]:
        with self._lock:
            record = self._records.get(identity)
            return (record.decision, record.revision) if record else (PENDING, 0)

    def decide(self, owner: str, identity: str, decision: str, scan_id: str,
               expected_revision: int | None = None) -> tuple[str, int]:
        """Record one decision durably, then return it.

        ``expected_revision`` is optimistic concurrency: a second tab holding a stale view sends
        an old revision and is refused with the current value rather than silently overwriting it.
        The journal append happens *before* the in-memory update, so a reported success always
        means the record is on disk.

        [R5-P3 / F2] ``None`` still means "no check" for internal callers, but it may no longer be
        *arrived at* by coercion. A non-integer ``expected_revision`` is now refused outright
        rather than passed through ``int()``, which used to turn ``0.9`` into ``0`` and would turn
        ``True`` into ``1``. The HTTP path never passes ``None`` derived from client input: it
        validates with :func:`parse_expected_revision` first and refuses before reaching here.
        """
        key = _require_owner(owner)
        if decision not in DECISIONS:
            raise StaleWrite("invalid_decision", *self.get(identity))
        if expected_revision is not None and (isinstance(expected_revision, bool)
                                              or not isinstance(expected_revision, int)):
            raise StaleWrite(MALFORMED_REVISION, *self.get(identity))
        with self._lock:
            if str(scan_id) != self._scans.get(key, ""):
                record = self._records.get(identity)
                raise StaleWrite("stale_scan", record.decision if record else PENDING,
                                 record.revision if record else 0)
            record = self._records.get(identity)
            current = record.decision if record else PENDING
            revision = record.revision if record else 0
            if expected_revision is not None and int(expected_revision) != revision:
                raise StaleWrite(REVISION_CONFLICT, current, revision)
            revision += 1
            event = {"k": "decide", "identity": identity, "decision": decision,
                     "revision": revision, "scan_id": str(scan_id), "at": time.time()}
            self._append(event)
            self._records[identity] = Record(identity, decision, revision, str(scan_id),
                                             event["at"])
            return decision, revision

    def counts(self, identities: Iterable[str]) -> dict[str, int]:
        with self._lock:
            out = {PENDING: 0, SELECTED: 0, REJECTED: 0}
            total = 0
            for identity in identities:
                record = self._records.get(identity)
                out[record.decision if record else PENDING] += 1
                total += 1
            out["TOTAL"] = total
            return out

    def snapshot_of(self, identities: Iterable[str]) -> dict[str, tuple[str, int]]:
        with self._lock:
            result = {}
            for identity in identities:
                record = self._records.get(identity)
                result[identity] = (record.decision, record.revision) if record else (PENDING, 0)
            return result


__all__ = [
    "COMPACT_AFTER_RECORDS",
    "DECISIONS",
    "MALFORMED_REVISION",
    "REVISION_CONFLICT",
    "REVISION_REQUIRED",
    "RevisionError",
    "ScanCoordinator",
    "SupersededScan",
    "build_poster_command",
    "ensure_posters",
    "is_complete_jpeg",
    "parse_expected_revision",
    "poster_attempt_path",
    "poster_key",
    "poster_path_for",
    "prune_poster_cache",
    "Discovery",
    "MAX_CLIPS_PER_PAGE",
    "MAX_OWNERS",
    "MAX_PAGES_PER_SESSION",
    "MAX_SESSIONS_PER_OWNER",
    "PENDING",
    "PersistenceError",
    "REJECTED",
    "ROLE_INPUT",
    "ROLE_LIBRARY",
    "ROLE_REJECTED",
    "Record",
    "Refusal",
    "ReviewStore",
    "RootIdentity",
    "SELECTED",
    "SERVABLE_SUFFIXES",
    "StaleWrite",
    "ClipEntry",
    "ClipRegistry",
    "Verdict",
    "content_type_for",
    "decision_identity",
    "discover_clips",
    "freeze_page",
    "identify_root",
    "io_detail",
    "parse_range",
    "protected_destinations",
    "validate_roots",
]
