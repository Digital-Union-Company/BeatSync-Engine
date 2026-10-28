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
                     files: Iterable[tuple[int, str, int, int, int, int]],
                     servable: Iterable[str] = SERVABLE_SUFFIXES
                     ) -> tuple[str, list[ClipEntry]]:
        """Mint one page of clip ids, revoking the session's previous page.

        ``files`` is ``[(index, abs_path, size, mtime_ns, dev, ino)]`` in the caller's already
        frozen enumeration order. This function never scans, sorts or re-reads the filesystem.

        ``servable`` is the **allowlist** of suffixes a page may contain, and it is a parameter
        rather than a constant because R7.2's Audio Preview authorizes audio through this same
        registry. It defaults to :data:`SERVABLE_SUFFIXES`, so every R5 call site is unchanged and
        a video page still cannot contain anything but the Raw Cutter's own output suffixes. It is
        an allowlist in both uses -- never widened to "anything", and never derived from the
        request -- so the defence below keeps meaning what it meant: a manifest, a sidecar or a
        decision journal can never enter a page whatever discovery handed over.
        """
        key = _require_owner(owner)
        allowed = tuple(str(s).casefold() for s in servable)
        if not allowed:
            raise Refusal(403, "no_servable_suffixes")
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
                if suffix not in allowed:
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


#: One FFprobe per clip is cheap, but a page is up to 100 of them, so the pass is bounded exactly
#: like the poster pass and the answers are cached on the clip's identity.
DURATION_TIMEOUT_SECONDS = 10
DURATION_MAX_WORKERS = 4
DURATION_CACHE_MAX_ENTRIES = 4096
_DURATION_CACHE: dict[str, float | None] = {}


def build_duration_command(ffprobe: str, source: str) -> list[str]:
    """The one duration argv. Built here so the GUI never becomes a command-construction site.

    No ``-nostdin``: that is an **ffmpeg** option and ffprobe does not have it. Passing it does not
    error cleanly -- ffprobe treats the next token as its value and dies with
    ``Failed to set value '-show_entries' for option 'nostdin'``, so every clip probes as unknown.
    Measured; the poster command above legitimately uses it because that one really is ffmpeg.
    """
    return [str(ffprobe), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source)]


def run_duration_command(argv, timeout: int = DURATION_TIMEOUT_SECONDS) -> str:
    """Execute one already-built probe argv, windowless and bounded; return raw stdout.

    Here rather than in `gui.py` for the same reason `run_poster_command` is: two suites pin that
    module's `subprocess.run` site count at exactly two. A failure returns `""`, never an
    exception -- an unreadable duration must leave the clip classifiable.

    ``stdin=DEVNULL`` is load-bearing rather than tidiness. FFprobe has no ``-nostdin`` -- that is
    an ffmpeg option -- so without this the child inherits whatever stdin the parent happens to
    have and can block on it indefinitely. Measured: it hung the GUI integration suite until the
    timeout. Detaching stdin is what ``-nostdin`` buys the poster command, done portably.
    """
    import subprocess

    try:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=timeout, text=True,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if int(completed.returncode) != 0:
            return ""
        return completed.stdout or ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def parse_duration(text: str) -> float | None:
    """Turn FFprobe's `format=duration` output into seconds, or **None**.

    `None` is the whole point. `ffmpeg_processing.get_video_duration` answers a **fabricated
    10.0** when the probe fails, which is correct for its caller (a render needs *a* number to
    plan with) and completely wrong here: the gallery would print a confident `(00:10)` over a
    clip of unknown length. `N/A`, an empty probe, `nan`, `inf` and a negative are all refusals.
    """
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return value


def format_duration(seconds: float | None) -> str:
    """`(00:14)`, `(01:05)`, `(1:12:08)` -- and `–:–` when the length is genuinely unknown.

    Hours are only shown once there are hours, so a short clip is not padded out to `0:00:14`.
    """
    if seconds is None:
        return "–:–"
    whole = int(seconds)                      # truncate: a 14.9s clip is not yet 15s
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def ensure_durations(entries: Iterable[ClipEntry], ffprobe: str, runner,
                     workers: int = DURATION_MAX_WORKERS,
                     timeout: int = DURATION_TIMEOUT_SECONDS) -> dict[str, float | None]:
    """Probe each clip's real length, bounded and cached. Returns ``clip_id -> seconds or None``.

    ``runner`` is an injected ``(argv, timeout) -> str`` so the path is testable without FFprobe
    existing, the same shape ``ensure_posters`` uses.

    Cached on :func:`poster_key` -- the established clip identity of path, size and mtime -- so a
    Refresh re-renders from memory and an edited clip is re-probed rather than answered from a
    stale entry. A probe that fails is cached as ``None`` too: a file FFprobe cannot read will not
    become readable on the next repaint, and retrying 100 of those on every Refresh would make the
    page crawl.
    """
    from concurrent.futures import ThreadPoolExecutor

    entries = list(entries)
    keys = {entry.clip_id: poster_key(entry.path, entry.size, entry.mtime_ns)
            for entry in entries}
    pending = [entry for entry in entries if keys[entry.clip_id] not in _DURATION_CACHE]

    def one(entry: ClipEntry) -> tuple[str, float | None]:
        try:
            raw = runner(build_duration_command(ffprobe, entry.path), timeout)
        except Exception:                                  # noqa: BLE001 - a duration is optional
            raw = ""
        return keys[entry.clip_id], parse_duration(raw)

    if pending:
        lanes = max(1, min(int(workers), DURATION_MAX_WORKERS))
        if lanes == 1:
            probed = [one(entry) for entry in pending]
        else:
            with ThreadPoolExecutor(max_workers=lanes,
                                    thread_name_prefix="r5probe") as pool:
                probed = list(pool.map(one, pending))
        if len(_DURATION_CACHE) > DURATION_CACHE_MAX_ENTRIES:
            _DURATION_CACHE.clear()                        # bounded, and cheap to refill
        _DURATION_CACHE.update(probed)

    return {entry.clip_id: _DURATION_CACHE.get(keys[entry.clip_id]) for entry in entries}


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


def compare_destinations(library_folder: Any, rejected_folder: Any) -> Verdict:
    """Just the LIBRARY-versus-DISCARDED relationship: identical, or one inside the other.

    Exists because that question is answerable **without knowing the input folder**, and R7.2's
    Audio Preview has to answer it while the operator is still typing -- before anything has been
    scanned. `validate_roots` identifies all three roots and returns on the first failure, so with
    no input folder yet it refuses with `empty_path` and never reaches the comparison the operator
    actually needs to see.

    This is a strict subset of `validate_roots` and uses the same helpers, so the two cannot drift
    into disagreeing about what "the same folder" means. It is **not** a substitute for it: it says
    nothing about the input folder or the protected runtime list, so a caller that is about to move
    a file must still run the full check.
    """
    left, verdict = identify_root(ROLE_LIBRARY, library_folder)
    if not verdict.ok:
        return verdict
    right, verdict = identify_root(ROLE_REJECTED, rejected_folder)
    if not verdict.ok:
        return verdict

    if _same_location(left, right):                        # type: ignore[arg-type]
        return Verdict(False, "destinations_identical", f"{left.real} == {right.real}")
    if _is_within(left.real, right.real) or _is_within(right.real, left.real):
        return Verdict(False, "destinations_nested", f"{left.real} / {right.real}")
    return Verdict(True)


def validate_one_destination(input_folder: Any, destination: Any, role: str = ROLE_LIBRARY,
                             app_root: str = "") -> tuple[Verdict, dict[str, RootIdentity]]:
    """The subset of :func:`validate_roots` that applies when only ONE destination will be written.

    Exists for R7.2's audio review, where an operator who keeps everything and discards nothing
    should not have to name a quarantine folder to file a clip. Every rule that can be stated about
    a single destination is applied **through the same helpers** `validate_roots` uses -- real-path
    resolution, `commonpath` containment, the Windows `(st_dev, st_ino)` alias cross-check and the
    protected-runtime list -- so the two cannot drift into disagreeing about what "inside" means.

    What it deliberately cannot check is the destination *pair*: identical and nested destinations
    are relationships between two folders, so a caller that will write both must use
    :func:`validate_roots` instead. :func:`beatsync_fork.audio_review.validate_destinations` is the
    one place that decides which of the two applies.
    """
    ids: dict[str, RootIdentity] = {}
    for which, typed in ((ROLE_INPUT, input_folder), (role, destination)):
        ident, verdict = identify_root(which, typed)
        if not verdict.ok:
            return verdict, ids
        ids[which] = ident                                 # type: ignore[assignment]

    inp, dest = ids[ROLE_INPUT], ids[role]

    if _same_location(inp, dest):
        return Verdict(False, f"{role}_is_input", f"{dest.real} == {inp.real}"), ids
    if _is_within(dest.real, inp.real):
        return Verdict(False, "destination_inside_input", f"{role}: {dest.real}"), ids
    if _is_within(inp.real, dest.real):
        return Verdict(False, "input_inside_destination", f"{role}: {dest.real}"), ids

    for protected in protected_destinations(app_root):
        if _is_within(dest.real, protected) or _is_within(protected, dest.real):
            return Verdict(False, "protected_runtime_destination",
                           f"{role}: {dest.real} vs {protected}"), ids

    return Verdict(True), ids


# --------------------------------------------------------------------------------------------
# Gallery markup
# --------------------------------------------------------------------------------------------

_BADGES = {PENDING: "?", SELECTED: "✓", REJECTED: "✗"}
_CLASSES = {PENDING: "r5-pending", SELECTED: "r5-selected", REJECTED: "r5-rejected"}


# --------------------------------------------------------------------------------------------
# Sorting the decided clips into the two destination folders (V1: same volume only)
# --------------------------------------------------------------------------------------------

#: What happened to one clip. Every value except MOVED left the source exactly where it was.
MOVED = "moved"
MISSING = "missing"
CHANGED = "changed"
OCCUPIED = "occupied"
CROSS_VOLUME = "cross_volume"
IN_USE = "in_use"
FAILED = "failed"


@dataclass
class MoveOutcome:
    clip_id: str
    label: str
    decision: str
    status: str
    destination: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == MOVED


def move_decided_clip(entry: ClipEntry, destination_dir: str) -> MoveOutcome:
    """Relocate one clip into ``destination_dir``. Never overwrites, never deletes, never renames.

    The whole operation is a single ``os.rename``, and that is the design rather than an
    implementation detail:

    * **Same volume only.** ``os.rename`` across volumes raises instead of copying, so a
      cross-volume request cannot silently degrade into copy-then-delete -- the one shape that
      could destroy an original. The device is compared up front so the operator gets a sentence
      instead of a winerror, but the syscall is the real guarantee.
    * **No overwrite.** On Windows ``os.rename`` refuses an existing destination with
      ``FileExistsError``. The pre-check below exists for the message; the refusal is enforced by
      the call itself, so a file that appears in the gap is still not clobbered. ``os.replace``
      would overwrite and is deliberately never used here.
    * **No collision renaming.** A taken name is reported as :data:`OCCUPIED` and the clip stays
      put. Inventing ``clip (2).mp4`` would quietly produce a library the operator never approved.
    * **Nothing is deleted.** There is no ``unlink`` on any path through this function.

    The clip's identity is re-checked against what the scan recorded immediately before the move,
    so a file that was replaced or edited while the operator was reviewing is skipped rather than
    sorted on a stale decision.
    """
    result = MoveOutcome(clip_id=entry.clip_id, label=entry.label, decision="", status=FAILED)
    try:
        live = os.stat(entry.path)
    except FileNotFoundError:
        result.status, result.detail = MISSING, "the file is no longer at its scanned location"
        return result
    except OSError as exc:
        result.status, result.detail = FAILED, io_detail(exc)
        return result

    if (live.st_size != entry.size or live.st_mtime_ns != entry.mtime_ns
            or (entry.ino not in (-1, 0) and live.st_ino != entry.ino)):
        result.status = CHANGED
        result.detail = "the file changed after it was scanned, so the decision may not describe it"
        return result

    try:
        destination_dev = os.stat(destination_dir).st_dev
    except OSError as exc:
        result.status, result.detail = FAILED, io_detail(exc)
        return result
    if entry.dev not in (-1, 0) and destination_dev != entry.dev:
        result.status = CROSS_VOLUME
        result.detail = "the destination is on a different drive"
        return result

    target = os.path.join(destination_dir, os.path.basename(entry.path))
    result.destination = target
    if os.path.exists(target):
        result.status = OCCUPIED
        result.detail = "a file of that name is already there; nothing was overwritten"
        return result
    try:
        os.rename(entry.path, target)
    except FileExistsError:
        result.status = OCCUPIED
        result.detail = "a file of that name appeared there first; nothing was overwritten"
        return result
    except OSError as exc:
        # Cross-volume shows up here too on some filesystems; report it as what it is.
        if getattr(exc, "winerror", None) == 17 or getattr(exc, "errno", None) == 18:
            result.status, result.detail = CROSS_VOLUME, "the destination is on a different drive"
        elif getattr(exc, "winerror", None) == 32:
            # ERROR_SHARING_VIOLATION. Measured in the browser: hovering a tile mounts a <video>
            # on that clip, and Windows will not rename a file another handle still holds open,
            # so the last clip the operator hovered was the one that would not sort. The client
            # now releases the player before Confirm runs; this stays as the honest explanation
            # for any other holder (an editor, a player, an antivirus scan).
            result.status = IN_USE
            result.detail = "another program still has it open"
        else:
            result.status, result.detail = FAILED, io_detail(exc)
        return result
    result.status = MOVED
    return result


def sort_decided_clips(entries: Iterable[ClipEntry], decision_of, destinations: dict[str, str],
                       ) -> list[MoveOutcome]:
    """Move each decided clip to the folder its decision names. Returns one outcome per clip.

    ``decision_of(entry) -> str`` reads the **server's durable record**; nothing here looks at
    markup, tile classes or anything the browser sent. ``destinations`` maps
    :data:`SELECTED`/:data:`REJECTED` to a directory.

    One clip failing does not stop the others, and it does not undo the ones already moved: each
    rename is independently complete the moment it returns, so a partial batch is a real partial
    result and is reported as one. A :data:`PENDING` clip is never touched -- Confirm is not
    offered until the page has none, and this refuses to act on one even if it were.
    """
    outcomes: list[MoveOutcome] = []
    made: set[str] = set()
    for entry in entries:
        decision = decision_of(entry)
        destination_dir = destinations.get(decision)
        if destination_dir is None:
            outcomes.append(MoveOutcome(entry.clip_id, entry.label, decision, FAILED,
                                        detail="undecided clips are never sorted"))
            continue
        if destination_dir not in made:
            try:
                os.makedirs(destination_dir, exist_ok=True)
            except OSError as exc:
                outcomes.append(MoveOutcome(entry.clip_id, entry.label, decision, FAILED,
                                            detail=io_detail(exc)))
                continue
            made.add(destination_dir)
        outcome = move_decided_clip(entry, destination_dir)
        outcome.decision = decision
        outcomes.append(outcome)
    return outcomes


def tile_html(entry: ClipEntry, decision: str, revision: int, session_id: str,
              page_token: str, has_poster: bool, duration: float | None = None) -> str:
    """One tile.

    Every interpolated value is escaped, and the only text that comes anywhere near the clip is
    the server-generated ``Clip 0001`` label -- the basename is never inserted, so a filename
    cannot carry markup or script into the page. Status is carried by a badge glyph as well as a
    colour class, so colour is not the only signal.

    ``duration`` appends the clip's real length, as ``Clip 0001 (00:14)``. It is a number of
    seconds and discloses nothing about the file's name or location, so it is safe in markup that
    deliberately carries neither. ``None`` renders ``(–:–)`` rather than a guess -- see
    :func:`parse_duration` for why a fabricated length is worse than an honest unknown.
    """
    import html as _html

    decision = decision if decision in _CLASSES else PENDING
    clip = _html.escape(entry.clip_id, quote=True)
    label = f"{entry.label} ({format_duration(duration)})"
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
                 poster_exists, durations: dict[str, float | None] | None = None) -> str:
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
                               bool(poster_exists(entry)),
                               (durations or {}).get(entry.clip_id)))
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


#: [R5-P11 / N7] The one Windows code that genuinely means "that name is not in this directory".
#: ``FileNotFoundError`` is the class CPython maps **six** codes onto -- enumerated in
#: :meth:`ReviewStore._journal_tail_is_terminated` -- and five of them say *the pathname did not
#: resolve*: a share that stopped answering, a volume that went away, a bad UNC name. Reading all
#: six as "the file is not there" is how a journal still sitting on disk, holding every committed
#: decision, was recovered as an empty store.
_ERROR_FILE_NOT_FOUND = 2

#: [R5-P13 / N7-R2] Every event kind this journal format defines. The writer emits exactly these
#: three and nothing else, so a fourth kind is not a tolerable unknown -- it is corruption, or a
#: record written by a build this one cannot interpret. Folding it in as a silent no-op is one of
#: the two routes by which a committed decision became invisible at start-up and was then
#: truncated away by the next compaction.
_JOURNAL_SCAN = "scan"
_JOURNAL_DECIDE = "decide"

#: The durable, auditable evidence that an unterminated tail was deliberately closed off. It is
#: what makes the F4 torn-tail tolerance *provable* after the fact rather than positional; see
#: :meth:`ReviewStore._replay_journal` and :meth:`ReviewStore._terminate_damaged_tail`.
_JOURNAL_TAIL_REPAIRED = "tail-repaired"

#: The marker is appended **onto** the damaged fragment rather than onto a line of its own, so
#: this is the needle that finds it inside an unparsable line. It is the exact leading text of
#: ``json.dumps({"k": _JOURNAL_TAIL_REPAIRED, "at": ...}, separators=(",", ":"))``; dict order is
#: insertion order, so that spelling is stable.
_TAIL_REPAIR_PREFIX = '{"k":"' + _JOURNAL_TAIL_REPAIRED + '"'

#: [R5-P17 / N7-R6] The same needle as bytes, for the one reader that cannot work in text: a line
#: the writer tore apart **inside a multi-byte character** is not decodable at all, and the marker
#: that accounts for it sits after the broken sequence. The marker is pure ASCII -- it is
#: ``json.dumps`` output with ``ensure_ascii=False`` over an ASCII key and a float -- so searching
#: for it bytewise finds exactly what searching for it in text would have found, had the line been
#: text. See :meth:`ReviewStore._bytes_damage_is_accounted_for`.
_TAIL_REPAIR_PREFIX_BYTES = _TAIL_REPAIR_PREFIX.encode("utf-8")

#: [R5-P19] The marker up to and including its one field name. Everything the repair can emit is a
#: prefix of ``_TAIL_REPAIR_OPENING + <number> + "}"``, so this is the anchor that lets a damaged
#: line be split into "the fragment the writer tore" and "the evidence the writer appended to it".
#: It cannot occur inside a record by accident: a ``"`` inside a JSON string is escaped, so the
#: literal ``'{"k":"'`` sequence only ever appears where this writer started an object.
_TAIL_REPAIR_OPENING = _TAIL_REPAIR_PREFIX + ',"at":'

#: Every character ``repr`` of a finite float can produce. Deliberately *not* a JSON number parser:
#: this only has to recognise how far an interrupted ``at`` got, and anything it does not recognise
#: fails closed. ``NaN``/``Infinity`` are therefore not writer-shaped here -- ``time.time()`` cannot
#: produce them -- which is a refusal, never an acceptance, and leaves N7-R4's separate question
#: about marker *provenance* exactly where it was.
_TAIL_REPAIR_NUMBER = "0123456789+-.eE"

_JOURNAL_KINDS = (_JOURNAL_SCAN, _JOURNAL_DECIDE, _JOURNAL_TAIL_REPAIRED)

#: [R5-P15 / N7-R3] How an unterminated final journal line is classified before anything is
#: written after it. R5-P13 had no such classification -- *every* newline-less tail was treated
#: as an unfinished write and stamped with a repair marker -- and an independent review measured
#: where that leads: a committed decision that had been **reported successful**, whose line lost
#: only its terminating newline, was recovered correctly on the next start, then turned into
#: "damage this process accounted for" by the marker, then erased by the following ``compact()``.
#: The parent commit, which wrote a bare newline, preserved it. So the two shapes are now told
#: apart, and the one that costs nothing to keep is kept.
_TAIL_COMPLETE_EVENT = "complete_event"      # a whole, schema-valid record; only its newline is
#                                              missing. Complete it and preserve its exact bytes.
_TAIL_TORN_FRAGMENT = "torn_fragment"        # genuinely unfinished; the F4 torn tail, repaired
#                                              with the marker-first write, unchanged.
_TAIL_INVALID_EVENT = "invalid_event"        # parses whole, but describes no record this build
#                                              can account for. Fail closed.
_TAIL_TRAILING_BYTES = "trailing_bytes"      # a complete value followed by something
#                                              unidentifiable. Fail closed; see
#                                              :meth:`ReviewStore._classify_unterminated_tail`.

#: [R5-P19 / N7-R7] Damage that is **not** anything this writer can have left behind, so what it
#: holds cannot be reasoned about at all -- the shape an independent review built by inserting an
#: invalid byte *in front of* a whole committed event and removing that event's newline. Every
#: earlier revision of this classifier reached "torn fragment" for it by elimination, which stamped
#: a reported-successful decision as discardable damage and let the next ``compact()`` erase it.
#: Elimination is now a refusal: a fragment is torn only when it can be *shown* to be one. See
#: :meth:`ReviewStore._bytes_are_an_interrupted_write`.
_TAIL_UNACCOUNTED_DAMAGE = "unaccounted_damage"

#: Read granularity when walking backwards for the last newline. One block covers any record this
#: writer produces, so the ordinary case is a single read.
_TAIL_CHUNK_BYTES = 8192

#: [R5-P15 / N7-R3] The ceiling on that walk. A record is a few hundred bytes -- a 64-character
#: identity, a short scan token and two numbers -- so this is orders of magnitude above anything
#: the writer emits, and it is here so that classifying **one** line can never degrade into an
#: unbounded read of the whole journal. A final line past this bound cannot be classified, and an
#: unclassifiable line is refused rather than guessed at.
MAX_TAIL_INSPECT_BYTES = 1 << 20

#: [R5-P19 / N7-R7] How many *interior* ``{`` positions in one damaged fragment are probed for a
#: whole JSON value hiding behind the damage in front of it. A record this writer emits has **no**
#: interior object at all -- the schema is flat -- so the ordinary answer is zero probes and this
#: ceiling is never approached. It exists so that one pathological line cannot turn a linear scan
#: into a quadratic one, and exceeding it is a **refusal**: a fragment whose interior could not be
#: ruled out is not a fragment that may be discarded.
MAX_HIDDEN_RECORD_PROBES = 64

#: [R5-P21] Whitespace the JSON grammar allows between tokens. ``json.dumps`` with
#: ``separators=(",", ":")`` emits none of it, so accepting it costs nothing and keeps
#: :meth:`ReviewStore._structural_object_openings` a scanner for JSON rather than for one dumper's
#: exact spelling.
_JSON_SPACE = " \t\n\r"

#: [R5-P21] Every character a JSON number can be built from. Deliberately **not** a number
#: validator -- see :meth:`ReviewStore._structural_object_openings` for why leniency *inside* a
#: token can only ever cause a refusal, and why the strictness that matters is at the token
#: boundary that follows it.
_JSON_NUMBER = "-+.eE0123456789"

#: [R5-P21] The three bare words JSON allows where a value is expected. This writer emits none of
#: them today; recognising them keeps the scanner a JSON scanner.
_JSON_WORDS = ("true", "false", "null")


class _EventSchemaError(Exception):
    """[R5-P13 / N7-R2] One journal event does not describe what its kind promises to describe.

    Internal to :class:`ReviewStore`: :meth:`ReviewStore._replay_journal` converts it into a
    :class:`PersistenceError`, which is the typed, path-free class the GUI and the 503 boundary
    already handle. It carries no pathname and no record content -- only which field was wrong --
    because its text reaches the browser.
    """


def _require_number(event: dict, field: str, *, optional: bool = False) -> float:
    value = event.get(field)
    if value is None and optional:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _EventSchemaError(f"{field!r} is not a number")
    return float(value)


def _require_text(event: dict, field: str, *, allow_empty: bool = True) -> str:
    value = event.get(field)
    if not isinstance(value, str):
        raise _EventSchemaError(f"{field!r} is not a string")
    if not allow_empty and not value:
        raise _EventSchemaError(f"{field!r} is empty")
    return value


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
        #: [R5-P11 / N7] ``False`` until :meth:`_load` has rebuilt the **whole** record. A store
        #: that did not completely recover may never append and may never compact. Set before the
        #: first statement that can fail, so the guard is true even for a half-built object.
        self._recovered = False
        try:
            os.makedirs(self.dir, exist_ok=True)
        except OSError as exc:
            raise PersistenceError(
                f"could not open the review state directory ({_io_detail(exc)})") from exc
        self.journal_path = os.path.join(self.dir, "review-journal.jsonl")
        self.snapshot_path = os.path.join(self.dir, "review-snapshot.json")
        self._lock = threading.RLock()
        self._records: dict[str, Record] = {}
        self._scans: dict[str, str] = {}               # owner -> current scan id
        self.corrupt_records_discarded = 0
        self.journal_records = 0
        #: [R5-P3 / F4] How many times an unterminated tail was closed off before appending.
        self.journal_tails_repaired = 0
        #: [R5-P15 / N7-R3] How many times a **complete, schema-valid** final record was found
        #: missing only its terminating newline and simply completed. Counted separately from
        #: :attr:`journal_tails_repaired` because the two are different events: that one records
        #: damage being set aside, this one records a committed record being preserved.
        self.journal_tails_completed = 0
        self._load()

    # ---- durability ------------------------------------------------------------------

    def _absence_is_proven(self, path: str, exc: OSError) -> bool:
        """[R5-P11 / N7] Whether ``exc`` *proves* ``path``'s leaf name is not in :attr:`dir`.

        A genuinely fresh store has no snapshot and no journal and must still work, so "the file
        is not there" has to remain a usable answer. What it must **not** be is a guess, and the
        two ways of guessing it are exactly the ways this defect was reached:

        * ``os.path.exists`` / ``os.path.lexists`` answer ``False`` for a path that merely failed
          to *resolve* -- a disconnected share, a removed volume, an ACL that denies traversal --
          just as readily as for a name that is absent. The same confusion the N6 fix removed from
          the append path was still load-bearing on the read path.
        * ``FileNotFoundError`` is not "missing" either. CPython maps six Windows codes onto it
          and only ``ERROR_FILE_NOT_FOUND`` says the name is not in the directory.

        So absence is established from the **directory listing**, which is positive filesystem
        evidence rather than an inference from a failure: the error class and code must be the one
        that means an absent leaf, *and* the directory must be enumerable, *and* the basename must
        genuinely not be in it. A listing that cannot be read, or that does contain the name, is an
        unresolved condition and fails closed. Everything else -- a sharing violation, a denied
        ACL, a device error -- is not an answer at all.
        """
        if not isinstance(exc, FileNotFoundError):
            return False
        winerror = getattr(exc, "winerror", None)
        if winerror is not None and winerror != _ERROR_FILE_NOT_FOUND:
            return False
        try:
            names = os.listdir(self.dir)
        except OSError:
            return False
        return os.path.basename(path) not in names

    def _read_snapshot(self) -> tuple[dict[str, Record], dict[str, str]]:
        """The snapshot, into **fresh** dicts. Returns ``(records, scans)``.

        [R5-P11 / N7] Two outcomes, kept distinct because conflating them is how a committed
        decision was lost:

        * **genuinely absent** -- an empty contribution, and the journal is the whole record;
        * **unreadable** -- a lock, an ACL, a device error, an unresolved pathname. That is not an
          answer about what the snapshot holds, so it raises. The original code caught ``OSError``
          in the same clause as ``ValueError`` and therefore called a transient read failure
          "corruption", published an empty record set as authoritative, and allowed the next
          compaction to overwrite the real snapshot with it.

        [R5-P13 / N7-R1] **And a third outcome that used to be a fourth: readable content that
        this format cannot account for now raises as well.** R5-P3 tolerated it -- an unusable
        snapshot contributed nothing and the journal replayed over it -- and an independent review
        measured where that leads. A snapshot was damaged so that one entry lacked its
        ``decision`` while three committed records were still present in readable JSON. The
        comprehension below raised ``KeyError``, the whole snapshot was discarded, the empty
        record set was published as authoritative, and the next ``compact()`` -- including the
        automatic one, with no operator action at all -- wrote a 144-byte replacement over the
        381-byte original. All three recoverable decisions were gone for good.

        The reason tolerance cannot be repaired by reading *more* carefully is structural:
        ``compact()`` truncates the journal, so **whenever a snapshot exists at all, the journal is
        by construction only the delta written since that snapshot landed.** A readable journal, a
        plausible set of decisions and a matching count are therefore none of them proof that the
        journal holds the history the snapshot was holding. This format carries no genesis marker
        and no provenance, so that proof is not available, and inventing one implicitly is how the
        same defect comes back wearing a check. So this fails closed: nothing is written here, the
        damaged bytes and the journal both stay exactly as they are, and a tool or an operator can
        still recover from them by hand.

        Records are materialised **all at once**: the original loop assigned into the live
        ``self._records`` as it went, so a failure on the third of ten records left the first two
        installed and the rest silently absent.
        """
        try:
            handle = open(self.snapshot_path, "r", encoding="utf-8")
        except OSError as exc:
            if self._absence_is_proven(self.snapshot_path, exc):
                return {}, {}
            raise PersistenceError(
                f"could not read the review snapshot ({_io_detail(exc)})") from exc
        try:
            with handle:
                raw = handle.read()
        except OSError as exc:
            raise PersistenceError(
                f"could not read the review snapshot ({_io_detail(exc)})") from exc
        except UnicodeDecodeError as exc:
            raise PersistenceError(
                "the review snapshot is not valid UTF-8 text, so what it holds could not be "
                f"established (byte offset {exc.start}); nothing has been changed") from exc
        try:
            return self._materialise_snapshot(raw)
        except (_EventSchemaError, ValueError) as exc:
            raise PersistenceError(
                f"the review snapshot could not be read as a complete record ({exc}), so the "
                "decisions it holds cannot be established; nothing has been changed and both "
                "copies are exactly as they were") from exc

    @staticmethod
    def _materialise_snapshot(raw: str) -> tuple[dict[str, Record], dict[str, str]]:
        """[R5-P13 / N7-R1] Validate the snapshot in full, or raise. Writes nothing.

        Strict about everything that carries a decision's substance -- the decision itself, its
        revision, its scan -- because a snapshot entry this build cannot interpret may be an entry
        whose decision it is about to lose. ``updated_at`` is display metadata and is allowed to
        be absent, which is the one thing here that is *not* part of the record; and an identity
        is required only to be a *string*, for the writer-symmetry reason given in :meth:`_apply`.
        """
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise _EventSchemaError("the snapshot is not an object")
        entries = snapshot.get("records")
        if not isinstance(entries, dict):
            raise _EventSchemaError("'records' is not an object")
        records: dict[str, Record] = {}
        for identity, entry in entries.items():
            # Any string is a legitimate key -- see `_apply` on why emptiness is not checked.
            if not isinstance(identity, str):
                raise _EventSchemaError("a record identity is not a string")
            if not isinstance(entry, dict):
                raise _EventSchemaError("a record is not an object")
            decision = _require_text(entry, "decision", allow_empty=False)
            if decision not in DECISIONS:
                raise _EventSchemaError("a record's decision is not a known decision")
            revision = entry.get("revision")
            if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                raise _EventSchemaError("a record's revision is not a non-negative integer")
            records[identity] = Record(identity, decision, revision,
                                       _require_text(entry, "scan_id"),
                                       _require_number(entry, "updated_at", optional=True))
        raw_scans = snapshot.get("scans", {})
        if not isinstance(raw_scans, dict):
            raise _EventSchemaError("'scans' is not an object")
        scans: dict[str, str] = {}
        for owner, scan in raw_scans.items():
            if not isinstance(owner, str) or not isinstance(scan, str):
                raise _EventSchemaError("a scan pointer is not a string keyed by a string")
            scans[owner] = scan
        return records, scans

    def _replay_journal(self, records: dict[str, Record], scans: dict[str, str]
                        ) -> tuple[int, int]:
        """Replay the journal onto ``records``/``scans``. Returns ``(applied, discarded)``.

        [R5-P11 / N7] An ``OSError`` part way through the replay **raises**. The original code
        swallowed it and kept whatever had been applied so far, which published a truncated
        prefix of the journal as the authoritative record -- and then let the store be written to
        and compacted, turning a transient read failure into permanent loss.

        [R5-P13 / N7-R2] **An unparsable line is tolerated only where it can be *accounted for*.**
        R5-P3 discarded every unparsable line wherever it sat and continued, and its comment
        justified that with "a damaged line can only ever be a single line, because ``_append``
        terminates an unterminated tail before writing after it". That is true of damage this
        process caused; it says nothing about a line damaged by anything else. An independent
        review replaced one committed decision's line with malformed content, left the later valid
        decisions in place, and measured the consequence: the decision recovered as ``PENDING``,
        the store declared itself recovered, a new decision was accepted, and ``compact()`` wrote
        a snapshot without that decision and truncated the 313 retained bytes that still held it.

        So damage is tolerated in exactly two shapes, and both of them are *positive evidence*
        rather than an absence of complaint:

        * **the final line, when it is unterminated *and does not parse*** -- a record whose write
          did not reach its newline. ``_append`` writes a record and its newline in one
          ``_write_all``, which either writes every byte or raises, so a line this *writer* left
          unterminated is one whose decision was never reported successful; and nothing can follow
          it, so nothing committed is hiding behind it. This is the F4 torn tail, unchanged.

          [R5-P15 / N7-R3] **But "unterminated" alone was never the right test, and reading it as
          one destroyed data.** A newline-less final line that parses whole was *written* whole --
          only its terminator is missing, which external truncation of one byte is enough to do --
          and such a line is replayed here as the ordinary event it is. The repair path tells the
          two apart as well; see :meth:`_classify_unterminated_tail`. A malformed line that **is**
          terminated is a
          different animal: this writer only ever terminates a line it wrote in full, and a full
          record parses -- so terminated damage is external corruption of something that may well
          have been committed, wherever in the file it sits, and it refuses;
        * **damage carrying its own repair marker** -- an unparsable line that *ends* with a valid
          ``tail-repaired`` object. :meth:`_terminate_damaged_tail` appends that marker onto the
          fragment, so the evidence is attached to the damage rather than positioned near it, and
          the line is terminated and documented by the same write. Without it the previously
          accepted F4 sequence would break on the second restart: once a torn fragment has been
          closed off it is no longer the final line, and a rule that said "only the last line may
          be damaged" would refuse a store whose later decision had already been reported
          successful.

          [R5-P15 / N7-R4] **The marker is an auditable format convention, not authenticated
          evidence of its own origin**, and an earlier version of this paragraph claimed more than
          that: it said a marker "can only reach a line through that repair", which is a statement
          about this writer and not about the file. An independent review appended a well-formed
          marker to unrelated corrupted content and the discard was accepted. That is outside the
          accidental-corruption model this design targets -- random damage does not synthesise a
          valid marker object -- and closing it would need provenance this format does not carry,
          so it is a recorded limitation rather than a claim. What *is* true: the repair itself
          only ever fires on an unterminated tail, it never writes a marker onto a line that
          parses whole (:meth:`_classify_unterminated_tail`), and the amnesty is reached only
          after :meth:`_tail_hides_a_complete_record` has ruled out a whole record hiding in
          front of it.

        Anything else -- an unparsable line with a committed event after it -- means the record it
        held cannot be established, so recovery refuses and **nothing is written**. Both copies
        keep their bytes and the evidence stays recoverable.

        A line that parses as JSON is never torn: truncating one of this writer's records always
        leaves unbalanced JSON, so a *parsable* event with a schema this build cannot account for
        is corruption or foreign content wherever it sits, and :meth:`_apply` refuses it outright.

        [R5-P17 / N7-R6] **The journal is read as bytes and decoded one line at a time, because a
        torn write does not respect character boundaries and the old text layer could not survive
        one.** R5-P13 opened this file in text mode with strict UTF-8 and iterated it, so a single
        undecodable byte anywhere aborted the *whole* replay -- and the writer itself produces
        exactly that shape. An independent review interrupted a real ``decide()`` one byte into
        the UTF-8 encoding of ``é`` in an identity: the first decision was correctly refused, the
        classifier called the fragment torn damage, the next decision was **reported successful**,
        the repair marker was appended -- and then every restart refused the store, including the
        earlier decisions that had been committed before the tear. Writer and reader disagreed
        about what was recoverable, which is the one disagreement a durability contract cannot
        have: the append path published a success the read path could not honour. Measured again
        here for two-, three- and four-byte characters, at every proper prefix of each.

        So the loop below reads binary lines and decodes each one on its own. A line that decodes
        takes exactly the path it always took. A line that does **not** decode is still classified
        rather than fatal, by the same three questions in the same order, asked bytewise:
        :meth:`_bytes_hide_a_complete_record` (a whole record behind unidentifiable bytes refuses),
        then :meth:`_bytes_damage_is_accounted_for` (the writer's own repair evidence is honoured
        even though the fragment it accounts for is not text), then terminated-or-not. Only an
        undecodable, unterminated, unaccounted-for final line is the torn tail. Nothing is decoded
        leniently: there is no ``errors=`` handler anywhere on this path, and the one place a
        partial decode is consulted answers a question whose positive answer is a **refusal**.

        Two deliberate consequences of leaving text mode: byte offsets in the message below are
        now counted from the start of the file rather than from whichever decode buffer the text
        layer happened to be filling, and a lone ``\\r`` no longer separates records. This writer
        emits ``\\n`` through an ``O_BINARY`` descriptor and never emitted either shape, and
        content that relied on them is refused here either way.
        """
        try:
            handle = open(self.journal_path, "rb")
        except OSError as exc:
            if self._absence_is_proven(self.journal_path, exc):
                return 0, 0
            raise PersistenceError(
                f"could not read the review journal ({_io_detail(exc)})") from exc
        applied = damaged = 0
        torn_tail = False
        consumed = 0
        try:
            with handle:
                for chunk in handle:
                    offset, consumed = consumed, consumed + len(chunk)
                    undecodable: UnicodeDecodeError | None = None
                    try:
                        raw = chunk.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        undecodable = exc
                    else:
                        line = raw.strip()
                        if not line:
                            continue
                    if torn_tail:
                        # Unreachable against a real file -- an unterminated line ends the
                        # iteration -- and asserted anyway, because the alternative to this raise
                        # is the defect: skipping a damaged record that something follows.
                        raise PersistenceError(
                            "the review journal continues past an unfinished record, so what "
                            "that record held cannot be established; nothing has been changed "
                            "and both copies are exactly as they were")
                    if undecodable is not None:
                        fragment = chunk.strip()
                        if self._bytes_hide_a_complete_record(fragment):
                            # [R5-P17 / N7-R6] The same precedence the text path uses, and for the
                            # same reason: a whole record sitting in front of bytes nothing can
                            # identify may be a committed decision, so the marker amnesty below
                            # must not get the chance to discard it.
                            raise PersistenceError(
                                f"review journal record {applied + damaged + 1} is a complete "
                                "record followed by an unidentifiable partial write, so whether "
                                "it was committed cannot be established; nothing has been "
                                "changed and both copies are exactly as they were"
                            ) from undecodable
                        # [R5-P19 / N7-R7] **The marker amnesty is now conditional on the damage
                        # being damage this writer could have left.** The measured loss is a whole
                        # committed event behind an invalid byte with a repair marker already on
                        # the end of it: the amnesty discarded that line without ever being able
                        # to see what was inside it, because the readable prefix an invalid first
                        # byte leaves is empty. Gating the amnesty rather than preceding it keeps
                        # every existing refusal message -- including the N7-R2 byte-offset one
                        # below -- exactly where it was.
                        accountable = self._bytes_are_an_interrupted_write(fragment)
                        if accountable and self._bytes_damage_is_accounted_for(fragment):
                            damaged += 1
                            continue
                        if chunk.endswith(b"\n"):
                            raise PersistenceError(
                                "the review journal is not valid UTF-8 text at a record that was "
                                "written in full, so it is damage this process did not cause and "
                                "what it held cannot be established (byte offset "
                                f"{offset + undecodable.start}); nothing has been changed and "
                                "both copies are exactly as they were") from undecodable
                        if not accountable:
                            raise PersistenceError(
                                f"review journal record {applied + damaged + 1} is damaged in a "
                                "way this process cannot have caused, so whether a committed "
                                "decision is hidden behind the damage cannot be established "
                                f"(byte offset {offset + undecodable.start}); nothing has been "
                                "changed and both copies are exactly as they were"
                            ) from undecodable
                        torn_tail = True
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError as unreadable:
                        if self._tail_hides_a_complete_record(line):
                            # [R5-P15 / N7-R3] A complete JSON value with unidentifiable bytes
                            # after it. Checked BEFORE the marker amnesty, because a short write
                            # can land a whole marker and still miss its newline -- and then the
                            # amnesty would discard the complete record in front of it, which is
                            # the N7-R3 loss reached through the read path instead of the repair.
                            # This writer never puts a marker behind a complete record, so the
                            # shape is foreign however it arrived; refusing keeps every byte, and
                            # the decision is recoverable by hand.
                            raise PersistenceError(
                                f"review journal record {applied + damaged + 1} is a complete "
                                "record followed by an unidentifiable partial write, so whether "
                                "it was committed cannot be established; nothing has been "
                                "changed and both copies are exactly as they were"
                            ) from unreadable
                        # [R5-P19 / N7-R7] The readable half of the same rule. The byte path's
                        # loss has a text twin -- unidentifiable but *decodable* bytes in front of
                        # a whole committed event -- which `_tail_hides_a_complete_record` cannot
                        # see either, because it too only looks from offset zero.
                        accountable = self._text_is_an_interrupted_write(line)
                        if accountable and self._damage_is_accounted_for(line):
                            damaged += 1
                            continue
                        if raw.endswith("\n"):
                            raise PersistenceError(
                                f"review journal record {applied + damaged + 1} is unreadable "
                                "but was written in full, so it is damage this process did not "
                                "cause and what it held cannot be established; nothing has been "
                                "changed and both copies are exactly as they were"
                            ) from unreadable
                        if not accountable:
                            raise PersistenceError(
                                f"review journal record {applied + damaged + 1} is damaged in a "
                                "way this process cannot have caused, so whether a committed "
                                "decision is hidden behind the damage cannot be established; "
                                "nothing has been changed and both copies are exactly as they were"
                            ) from unreadable
                        torn_tail = True
                        continue
                    self._apply(event, records, scans)
                    applied += 1
        except OSError as exc:
            raise PersistenceError(
                f"could not replay the review journal ({_io_detail(exc)})") from exc
        except UnicodeDecodeError as exc:
            # [R5-P17 / N7-R6] No longer reachable: the loop decodes each line itself and
            # classifies the failure in place, which is the whole fix. Kept because the
            # alternative to a typed refusal here is a bare `UnicodeDecodeError` crossing the
            # store boundary -- the exact R5-P12 defect this handler was added for -- and because
            # it fails closed, so being wrong about its reachability costs availability, never a
            # decision.
            raise PersistenceError(
                "the review journal is not valid UTF-8 text, so it could not be replayed "
                f"(byte offset {exc.start}); nothing has been changed and both copies are "
                "exactly as they were") from exc
        except _EventSchemaError as exc:
            raise PersistenceError(
                f"review journal record {applied + damaged + 1} does not describe a complete "
                f"record ({exc}), so what it recorded cannot be established; nothing has been "
                "changed and both copies are exactly as they were") from exc
        # An unterminated final line is the torn tail: nothing committed can follow it.
        return applied, damaged + int(torn_tail)

    @staticmethod
    def _tail_hides_a_complete_record(line: str) -> bool:
        """[R5-P15 / N7-R3] Whether an unparsable ``line`` *begins* with a whole JSON value.

        Only a question about shape -- it says nothing about whether the value is a record this
        build understands, and deliberately so: a complete JSON object followed by bytes this
        writer would never put there is unexplained either way, and an unexplained line that may
        be holding a committed decision is refused rather than discarded.

        It does **not** fire on a repaired fragment. A torn record is unbalanced JSON from its
        first byte, so ``raw_decode`` fails at offset 0 for every genuine F4 tail and for every
        prefix of a marker written onto one -- which is what keeps the frozen interrupted-repair
        guarantee intact.
        """
        try:
            _value, end = json.JSONDecoder().raw_decode(line)
        except ValueError:
            return False
        return bool(line[end:].strip())

    @staticmethod
    def _damage_is_accounted_for(line: str) -> bool:
        """[R5-P13 / N7-R2] Whether an unparsable ``line`` carries this process's repair evidence.

        The marker is appended **onto** the damaged fragment, so it is a suffix of an unreadable
        line rather than a line of its own. That is deliberate, and it is what makes every
        interruption of the repair recover:

        * nothing written -- the fragment is still unterminated and still the trailing damage;
        * the marker part written -- still unterminated, so still the trailing damage, and the
          next append repairs again and appends a second, complete marker;
        * fully written -- the line is terminated *and* documented by the same bytes, so a later
          successful append behind it cannot make it look like historical corruption.

        A bare terminating newline cannot do this: the earlier draft of this fix wrote
        ``"\\n" + marker`` in one call, and an interruption after exactly one byte left the
        fragment closed off with no evidence at all. The next append then found a *terminated*
        tail, wrote no marker, and the restart after that refused a decision that had been
        reported successful. Measured by this correction's own interrupted-repair test.

        The last occurrence is the one examined, because a partly written marker can precede a
        complete one, and it must parse all the way to the end of the line.

        [R5-P15 / N7-R4] These bounds make the marker **unambiguous**, not **authentic**. It is a
        format convention: anything that can write to the journal can write a well-formed marker,
        and this returns ``True`` for it. That is outside the accidental-corruption model -- random
        damage does not produce a valid marker object -- and it is recorded as a limitation rather
        than described as proof of origin. :meth:`_replay_journal` consults
        :meth:`_tail_hides_a_complete_record` first, so the amnesty can no longer discard a whole
        record that is sitting in front of a marker.

        [R5-P17 / N7-R6] :meth:`_bytes_damage_is_accounted_for` is the entry point for a fragment
        that is not text at all; it delegates here, so there is exactly one marker validator and
        the two readers cannot drift apart.
        """
        cut = line.rfind(_TAIL_REPAIR_PREFIX)
        if cut < 0:
            return False
        try:
            marker = json.loads(line[cut:])
        except ValueError:
            return False
        if not isinstance(marker, dict) or marker.get("k") != _JOURNAL_TAIL_REPAIRED:
            return False
        try:
            _require_number(marker, "at")
        except _EventSchemaError:
            return False
        return True

    @staticmethod
    def _longest_text_prefix(raw: bytes) -> str:
        """[R5-P17 / N7-R6] The longest prefix of ``raw`` that is whole, valid UTF-8 text.

        ``UnicodeDecodeError.start`` is the index of the **first** byte the strict decoder could
        not account for, so everything before it is by definition complete characters and decoding
        that slice succeeds. The loop therefore runs at most twice and never scans backwards byte
        by byte.

        This is **not** lenient decoding, and the distinction is the whole safety argument. There
        is no ``errors=`` handler: no byte is dropped, replaced or smuggled through as a
        surrogate, and nothing decoded here is ever applied as a record. It exists to answer one
        question -- :meth:`_bytes_hide_a_complete_record` -- whose only positive answer is a
        **refusal**. Reading less of the line than is there can therefore only ever cause this
        code to miss a reason to refuse, never to manufacture a reason to accept.
        """
        cut = len(raw)
        while cut > 0:
            try:
                return raw[:cut].decode("utf-8")
            except UnicodeDecodeError as exc:
                cut = exc.start
        return ""

    @staticmethod
    def _bytes_hide_a_complete_record(raw: bytes) -> bool:
        """[R5-P17 / N7-R6] :meth:`_tail_hides_a_complete_record`, for a line that is not text.

        An undecodable line by construction has bytes after its decodable prefix, so there is no
        "and is there anything following it?" test to make: if a whole JSON value fits inside the
        text prefix, something unidentifiable follows it. That is the shape an independent review
        measured an already-initialized store accepting -- a complete, committed decision event
        with an invalid byte appended, marker-stamped as though the record itself were the damage,
        and a further decision then reported successful against it. It refuses here instead.

        A genuine interrupted write cannot reach this answer. The multi-byte character the tear
        landed inside is *inside* the record -- inside a JSON string, inside an unclosed object --
        so the text before it is always unbalanced and ``raw_decode`` always fails. Pinned by the
        real-writer matrix rather than argued from the shape.
        """
        text = ReviewStore._longest_text_prefix(raw).strip()
        if not text:
            return False
        try:
            json.JSONDecoder().raw_decode(text)
        except ValueError:
            return False
        return True

    @staticmethod
    def _bytes_damage_is_accounted_for(raw: bytes) -> bool:
        """[R5-P17 / N7-R6] :meth:`_damage_is_accounted_for`, for a line that is not text.

        The fragment in front of the marker is undecodable -- that is the entire reason this
        exists -- but the marker itself is ASCII, so the needle is found bytewise and only the
        suffix from the **last** occurrence is decoded. Validation is then the text validator,
        unchanged and unduplicated: the marker must decode strictly, parse as a lone JSON object,
        carry this build's kind and carry a numeric ``at``, all the way to the end of the line.

        Anything less is not evidence. A marker whose own write was interrupted leaves fewer bytes
        than the needle, or a needle with nothing parsable after it; either way this answers
        ``False``, the fragment is still unterminated, and the next append repairs it again.
        """
        cut = raw.rfind(_TAIL_REPAIR_PREFIX_BYTES)
        if cut < 0:
            return False
        try:
            suffix = raw[cut:].decode("utf-8")
        except UnicodeDecodeError:
            return False
        return ReviewStore._damage_is_accounted_for(suffix)

    # ---- [R5-P19 / N7-R7] is this damage something THIS writer can have left? ---------------
    #
    # Every question above asks what a damaged line *contains*. None of them asks the one question
    # that decides whether the line may be thrown away: **could this process have produced these
    # exact bytes?** R5-P17 answered it by elimination -- not a complete record in the readable
    # prefix, not carrying a marker, not terminated, therefore torn -- and an independent review
    # measured where elimination leads. It put one invalid byte in *front* of a whole committed
    # decision and removed that event's newline. The readable prefix is then empty, which is not
    # "no record is hiding" but "nothing could be read at all"; the classifier called it torn, the
    # repair stamped the committed record as accounted-for damage, the next decision was reported
    # successful against it, and ``compact()`` made the loss permanent.
    #
    # So the four helpers below replace elimination with a **positive** test, derived from the
    # writer rather than from the shape of the damage. ``_append`` writes exactly two things
    # through ``_write_all``, which writes every byte or raises:
    #
    #   * one record, ``json.dumps(event) + "\n"``, and
    #   * one repair marker, ``json.dumps({"k": ..., "at": ...}) + "\n"``, appended onto a fragment.
    #
    # The newline is the last byte of each, so an interrupted write leaves a **proper prefix** with
    # no newline in it, and the next append may add another such prefix. An unterminated line this
    # writer left is therefore, and only ever,
    #
    #     <proper prefix of one record>  [<truncated character>]  <prefixes of repair markers>
    #
    # with two consequences that are the whole discriminator. The record prefix always begins with
    # ``{`` and never parses whole. And the *only* undecodable bytes anywhere in it are the one
    # truncated multi-byte character the tear landed inside -- a marker is pure ASCII, and a record
    # is valid UTF-8 up to the cut. Anything that does not fit that template is external damage
    # whose contents cannot be reasoned about, and it is refused rather than discarded.

    @staticmethod
    def _is_repair_evidence_so_far(text: str) -> bool:
        """[R5-P19 / N7-R7] Whether ``text`` is only repair markers and interrupted repair markers.

        Not a second marker validator and deliberately weaker than one: :meth:`
        _damage_is_accounted_for` decides whether a *complete* marker accounts for damage, and that
        decision is untouched here. This answers the narrower structural question of whether the
        bytes the writer appended **after** a torn fragment are the only thing that could have been
        appended there, so that a complete record cannot be sitting among them.

        Accepts any run of marker *prefixes*, complete or not and in any order, because that is
        exactly what the writer can leave: each interrupted repair abandons its prefix in place
        and the next append starts a fresh marker directly against it. Two genuine shapes that an
        earlier draft of this grammar wrongly refused, both produced by the real write path and
        both measured here: ``…,"at":`` immediately followed by a whole second marker, and a
        12-byte stub ``{"k":"tail-r`` immediately followed by a whole second marker.

        The split is unambiguous because ``{`` occurs in the marker payload **only** as its first
        byte -- the rest is one ASCII key, one ASCII value and one number -- so a ``{`` anywhere
        in this suffix can only be where another repair attempt began.
        """
        if not text:
            return True
        if not text.startswith("{"):
            return False
        return all(ReviewStore._is_repair_marker_prefix("{" + piece)
                   for piece in text.split("{")[1:])

    @staticmethod
    def _is_repair_marker_prefix(piece: str) -> bool:
        """[R5-P19 / N7-R7] Whether ``piece`` is a prefix of one repair-marker payload."""
        if _TAIL_REPAIR_OPENING.startswith(piece):
            return True                          # interrupted inside the fixed opening
        if not piece.startswith(_TAIL_REPAIR_OPENING):
            return False
        rest = piece[len(_TAIL_REPAIR_OPENING):]
        number = rest[:-1] if rest.endswith("}") else rest
        return all(character in _TAIL_REPAIR_NUMBER for character in number)

    @staticmethod
    def _json_string_end(text: str, start: int) -> "int | None":
        """[R5-P21] The index just past the closing quote of the string opening at ``text[start]``.

        ``None`` means the string is still open when ``text`` runs out, which is the ordinary shape
        of a torn record: the tear usually lands inside the identity, so the final field is a
        string nobody ever closed.

        Only ``\\`` and ``"`` are interpreted, because only they can move the closing quote: an
        escape consumes the next character whatever it is, so ``\\"`` cannot close the string and
        ``\\\\"`` can. Nothing else about the contents is judged -- a ``{`` in here is text, a
        ``\\uXXXX`` body is text, and neither needs validating to find where the string ends.
        """
        walk = start + 1
        size = len(text)
        while walk < size:
            character = text[walk]
            if character == "\\":
                walk += 2                        # the escaped character, whatever it is
                continue
            if character == '"':
                return walk + 1
            walk += 1
        return None

    @staticmethod
    def _structural_object_openings(text: str) -> "tuple[int, ...] | None":
        """[R5-P21] Validate ``text`` as a proper prefix of one JSON object, and say where inside
        it an object *structurally* opens.

        ``None`` means ``text`` is **not** a proper prefix of a single JSON object: either some
        character is not grammatical where it stands, or the object finishes before the text does.
        Otherwise the answer is the indices, after index zero, at which a ``{`` opens a nested
        object -- the only positions at which a whole JSON value could be hiding.

        **Why a grammar and not a brace-skipper.** [R5-P20] The defect this replaces probed every
        interior ``{``, including one inside a string, so the real writer's own
        ``identity="clip-{}é"`` made ``{}`` look like a whole hidden object and a genuine torn
        write was refused. Merely *skipping* braces inside strings would fix that case and open a
        worse one: an external prefix can put a committed event where a quote-tracker reads it as
        string content. ``{"a":"`` in front of a whole ``{"k":"decide",...}`` is exactly that --
        the event's own first ``"`` closes the string the damage opened, and from there a
        brace-skipper is reading the event's bytes as if they were someone's text.

        Requiring the *whole* prefix to be grammatical closes that off, because there is then no
        damaged prefix left to hide anything behind: every character is accounted for by the
        grammar, so a string's boundaries are where the grammar says they are rather than where a
        heuristic guessed. The ``{"a":"{"k":...`` shape fails at the ``k`` -- a bare word where the
        grammar demands ``,`` or ``}`` -- and is refused, which is the behaviour R5-P19 bought and
        this correction must not spend.

        Leniency and strictness are placed deliberately. *Inside* a token this scanner is looser
        than JSON: ``_JSON_NUMBER`` accepts ``01`` and ``1e``, and a string's contents are not
        validated. That direction is safe, and safe in one direction only -- the caller has already
        established that :func:`json.JSONDecoder.raw_decode` could not parse a whole value at
        offset zero, so a token this scanner accepts but JSON would reject can only make the
        prefix look *finished* here (an empty stack, hence ``None``, hence a refusal) and never
        make unexplained bytes look accounted for. At every token *boundary* it is exact: what may
        follow a value, a key or a comma is the JSON grammar and nothing else.

        Iterative with an explicit container stack, so prefix depth costs no interpreter frames.
        """
        if not text.startswith("{"):
            return None
        stack: list[str] = []
        openings: list[int] = []
        expect = "value"
        index, size = 0, len(text)
        while True:
            while index < size and text[index] in _JSON_SPACE:
                index += 1
            if index >= size:
                # The text ran out. Still inside a container means a *proper* prefix, which is the
                # question. An empty stack means the value finished, which is not.
                return tuple(openings) if stack else None
            character = text[index]

            if expect == "close":
                # A value just ended; what may follow it belongs to the container holding it.
                expect = ("object-tail" if stack and stack[-1] == "object"
                          else "array-tail" if stack else "done")
                continue

            if expect in ("value", "first-value"):
                if character == "]" and expect == "first-value":
                    stack.pop()
                    index += 1
                    expect = "close"
                    continue
                if character == "{":
                    if stack:                    # index zero is the record itself, not an interior
                        openings.append(index)
                    stack.append("object")
                    index += 1
                    expect = "first-key"
                    continue
                if character == "[":
                    stack.append("array")
                    index += 1
                    expect = "first-value"
                    continue
                if character == '"':
                    end = ReviewStore._json_string_end(text, index)
                    if end is None:
                        return tuple(openings) if stack else None
                    index = end
                    expect = "close"
                    continue
                if character in "-0123456789":
                    walk = index
                    while walk < size and text[walk] in _JSON_NUMBER:
                        walk += 1
                    index = walk
                    expect = "close"
                    continue
                word = next((one for one in _JSON_WORDS if one[0] == character), None)
                if word is None:
                    return None
                if text.startswith(word, index):
                    index += len(word)
                    expect = "close"
                    continue
                if word.startswith(text[index:]):
                    return tuple(openings) if stack else None   # interrupted inside the word
                return None

            if expect in ("first-key", "key"):
                if character == "}" and expect == "first-key":
                    stack.pop()
                    index += 1
                    expect = "close"
                    continue
                if character != '"':
                    return None
                end = ReviewStore._json_string_end(text, index)
                if end is None:
                    return tuple(openings) if stack else None
                index = end
                expect = "colon"
                continue

            if expect == "colon":
                if character != ":":
                    return None
                index += 1
                expect = "value"
                continue

            if expect == "object-tail":
                if character == "}":
                    stack.pop()
                    index += 1
                    expect = "close"
                    continue
                if character == ",":
                    index += 1
                    expect = "key"
                    continue
                return None

            if expect == "array-tail":
                if character == "]":
                    stack.pop()
                    index += 1
                    expect = "close"
                    continue
                if character == ",":
                    index += 1
                    expect = "value"
                    continue
                return None

            return None                          # "done": a whole value with something after it

    @staticmethod
    def _is_an_unfinished_record(text: str) -> bool:
        """[R5-P19 / N7-R7] Whether ``text`` is a proper prefix of one of this writer's records.

        Three requirements, each of which rules out a measured loss shape:

        * it opens with ``{``, because every record and every marker does -- so unidentifiable
          bytes cannot be sitting in front of whatever follows them;
        * it does not parse whole, because a whole value is not an unfinished write and the caller
          refuses on that separately (:meth:`_tail_hides_a_complete_record`);
        * **it is grammatical all the way through, and no whole JSON value starts at any position
          where an object structurally opens inside it.** ``raw_decode`` is asked only at offset
          zero, so ``{x`` followed by an entire committed decision fails to parse at offset zero
          and was called torn; probing the interior refuses that outright.

        [R5-P21] The interior positions come from :meth:`_structural_object_openings`, which
        validates the prefix instead of scanning it for braces. The revision before it probed
        every interior ``{`` -- including one the writer itself put inside a string, which is how
        the real ``identity="clip-{}é"`` came to be read as hiding the whole object ``{}`` and a
        genuine torn write came to be refused. Positions inside a string are not candidates; a
        prefix that is not grammatical is not a torn record at all. Both halves of that are
        load-bearing, and the second is the one that keeps an external prefix from presenting a
        committed event as somebody's string content.

        A record this writer emits is flat, so a real fragment yields **zero** openings and the
        probe budget is never approached. The budget is a ceiling on *structural* candidates, and
        exceeding it is a refusal: a fragment whose interior could not be ruled out is not ruled
        out. Literal braces in a string value are not candidates and so never consume it.

        The probe is **not** a search for a record to trust -- the value found is discarded
        unexamined and nothing is ever recovered from it. Finding one is only ever a reason to
        refuse, which is why "it might be a JSON object" is a safe thing to act on here and would
        not be anywhere else.
        """
        if not text.startswith("{"):
            return False
        decoder = json.JSONDecoder()
        try:
            decoder.raw_decode(text)
        except ValueError:
            pass
        else:
            return False
        openings = ReviewStore._structural_object_openings(text)
        if openings is None:
            return False                         # not a prefix of one record; nothing to discard
        probes = 0
        for start in openings:
            probes += 1
            if probes > MAX_HIDDEN_RECORD_PROBES:
                return False                     # could not be ruled out, so it is not ruled out
            try:
                decoder.raw_decode(text, start)
            except ValueError:
                continue
            else:
                return False
        return True

    @staticmethod
    def _is_truncated_character(raw: bytes) -> bool:
        """[R5-P19 / N7-R7] Whether ``raw`` is a well-formed *proper* prefix of one UTF-8 character.

        The ranges are UTF-8's own, written out rather than inferred from a decoder, because the
        decoder cannot be asked this question: a strict decode of an incomplete sequence and a
        strict decode of an invalid one both raise, with the same ``start`` and ``end``. Reading
        the first as the second is how ``0xff`` came to be treated as an interrupted write.

        ``0xC0``/``0xC1`` (overlong two-byte), ``0xE0 0x80`` (overlong three-byte), ``0xED 0xA0``
        (surrogate), ``0xF4 0x90``/``0xF5``–``0xFF`` (past U+10FFFF) are rejected: no encoder this
        process uses can emit them, so a tear cannot have produced them either. A *complete*
        sequence is rejected too -- it would have decoded, so the line would not be here.
        """
        if not raw:
            return False
        lead = raw[0]
        if 0xC2 <= lead <= 0xDF:
            width, low, high = 2, 0x80, 0xBF
        elif 0xE0 <= lead <= 0xEF:
            width = 3
            low, high = ((0xA0, 0xBF) if lead == 0xE0
                         else (0x80, 0x9F) if lead == 0xED else (0x80, 0xBF))
        elif 0xF0 <= lead <= 0xF4:
            width = 4
            low, high = ((0x90, 0xBF) if lead == 0xF0
                         else (0x80, 0x8F) if lead == 0xF4 else (0x80, 0xBF))
        else:
            return False
        if len(raw) >= width:
            return False
        for index, byte in enumerate(raw[1:], start=1):
            lower, upper = (low, high) if index == 1 else (0x80, 0xBF)
            if not lower <= byte <= upper:
                return False
        return True

    @staticmethod
    def _text_is_an_interrupted_write(line: str) -> bool:
        """[R5-P19 / N7-R7] Whether a readable, unparsable ``line`` is one this writer left behind.

        The line splits into "the fragment the writer tore" and "the evidence the writer appended
        to it". Everything before the split must be an unfinished record; everything from it on
        must be repair evidence and nothing else.

        [R5-P21] The split is now found by *trying* the places a repair could have begun, instead
        of by searching for that one fixed opening. The old anchor was 26 characters long, and a
        repair interrupted before it got that far leaves a stub -- ``{"k``, ``{"k"``, ``{"k":`` --
        that the anchor cannot see, so the stub stayed glued to the fragment. That was harmless
        while the fragment only had to "not parse", and it is not harmless now that the fragment
        has to be *grammatical*: a record torn inside a string, which is the ordinary tear, has an
        unterminated string at its end, and the stub's own ``"`` closes it. ``…"decision":"SELE``
        followed by ``{"k`` then reads as the string ``SELE{`` followed by a bare ``k``, which is
        no longer a prefix of anything. That is a real, measured shape -- every prefix of the real
        46-byte marker over a real torn record -- so the boundary has to be found rather than
        assumed.

        Candidates are walked from the right, and the walk stops at the first suffix that is not
        repair evidence, which costs nothing: evidence validity is monotone in this direction.
        Extending a suffix leftwards to an earlier ``{`` keeps every piece the shorter suffix had,
        so a piece that already disqualified it disqualifies the longer one too.

        Neither half of an accepted split can conceal a committed decision. The evidence half must
        be nothing but repair-marker prefixes, and a ``decide`` or ``scan`` event is not a prefix
        of one. The fragment half must satisfy :meth:`_is_an_unfinished_record`, which is
        grammatical throughout and has no whole value at any structural interior position. The
        candidate count is bounded by :data:`MAX_HIDDEN_RECORD_PROBES` -- the same ceiling, used
        for the same reason, since each candidate is one interior ``{`` whose interpretation has
        to be ruled out -- and exhausting it refuses.
        """
        if ReviewStore._is_an_unfinished_record(line):
            return True                          # the whole line is the torn record; no repair ran
        probes = 0
        cut = line.rfind("{")
        while cut > 0:
            if not ReviewStore._is_repair_evidence_so_far(line[cut:]):
                return False                     # monotone: no earlier '{' can be evidence either
            probes += 1
            if probes > MAX_HIDDEN_RECORD_PROBES:
                return False                     # could not be ruled out, so it is not ruled out
            if ReviewStore._is_an_unfinished_record(line[:cut]):
                return True
            cut = line.rfind("{", 0, cut)
        return False

    @staticmethod
    def _bytes_are_an_interrupted_write(raw: bytes) -> bool:
        """[R5-P19 / N7-R7] The same question for a line that is not text. **The N7-R7 fix.**

        An undecodable line the writer left has exactly one undecodable region: the character the
        tear landed inside. So the line splits, byte-exactly and without dropping or substituting
        anything, into

            <readable prefix of a record> <truncated character> <ASCII repair evidence>

        and all three parts are checked. The readable prefix must be an unfinished record, which an
        **empty** prefix is not -- that is the whole of the N7-R7 defect, because an invalid first
        byte makes the prefix empty and "I could read nothing" was being scored as "nothing is
        hiding". The undecodable region must be a well-formed partial character rather than merely
        unreadable bytes, which is what refuses a leading ``0xff`` ahead of an intact committed
        event. And whatever follows it must be ASCII repair evidence, which is what refuses the
        same event hidden behind a genuine torn character.

        Nothing here recovers anything: the only outcomes are "this is a torn write, which the
        caller already knows how to repair" and "this cannot be accounted for", and the second one
        writes no byte.
        """
        text = ReviewStore._longest_text_prefix(raw)
        head = len(text.encode("utf-8"))
        if head == len(raw):
            return False                         # decodable in full; a different question
        if not ReviewStore._is_an_unfinished_record(text):
            return False
        rest = raw[head:]
        for held in (1, 2, 3):
            if held > len(rest):
                break
            if not ReviewStore._is_truncated_character(rest[:held]):
                continue
            tail = rest[held:]
            if any(byte > 0x7F for byte in tail):
                continue                         # a second undecodable region: not one tear
            if ReviewStore._is_repair_evidence_so_far(tail.decode("ascii")):
                return True
        return False

    def _load(self) -> None:
        """Rebuild memory from the snapshot and the journal: **completely, or not at all**.

        [R5-P11 / N7] This is the fix for a confirmed permanent-loss path, not a tidy-up. The
        original implementation treated every read failure as corruption and returned a store
        that looked healthy, so:

        1. a transiently unreadable snapshot or journal -- an exclusive share from a backup or an
           indexer is enough -- loaded as an **empty** record set, with no exception raised and
           nothing said to the operator;
        2. that store was fully writable and fully compactable;
        3. the next ``compact()``, including the automatic one at
           :data:`COMPACT_AFTER_RECORDS`, wrote that empty set over the real snapshot and
           truncated the journal.

        After step 3 the decisions were gone for good: the retained bytes that still held them
        had been replaced by a snapshot built from memory that was never authoritative. Measured
        on the pre-fix code with a real Windows exclusive-sharing lock *and* with independent
        fault injection, for the snapshot and for the journal, and through the real 2,000-record
        automatic threshold.

        So nothing is published until both halves have been read in full. Memory is assembled in
        local dicts and installed only on the last line, and ``_recovered`` -- the flag
        :meth:`_require_recovered` gates every mutation on -- is set only after that. A failure
        here propagates out of ``__init__`` as a :class:`PersistenceError`, which is strictly
        better than a store that cannot be told from a healthy one: there is no object to append
        to, so there is nothing to compact from. **Nothing in this method writes**, so a refused
        load leaves the snapshot and the journal byte-for-byte as they were and a later healthy
        attempt recovers everything.

        [R5-P13 / N7-R1 / N7-R2] ``_recovered`` means *the whole persisted record has been
        accounted for*, and after this commit it finally means only that. Reaching end of file is
        not the same statement, and neither is a corruption counter: the two measured loss routes
        both ended here with a tolerated discard, a ``True`` flag and a destructive compaction
        waiting. The two reads below now raise for any content they cannot fully justify, so the
        only damage that survives to this line is damage that cannot be hiding a committed
        decision -- a torn final record, or one already closed off by a repair marker. A partial
        history therefore never becomes the next snapshot, because there is no store holding it.
        """
        records, scans = self._read_snapshot()
        applied, damaged_lines = self._replay_journal(records, scans)
        self._records = records
        self._scans.update(scans)
        self.corrupt_records_discarded += damaged_lines
        self.journal_records = applied
        self._recovered = True

    def _require_recovered(self) -> None:
        """[R5-P11 / N7] Refuse to mutate a store whose start-up recovery was not complete.

        Defence in depth behind the constructor's refusal, and the reason the fix cannot be
        reduced to "raise from ``_load``": the destructive step is ``compact()``, which neither
        reads nor needs the files it is about to replace. Gating the two mutating entry points on
        the recovery flag means an incompletely recovered store can never become the snapshot,
        however it came to exist -- a partially constructed object, a subclass, a future reload
        path that resets state in place.
        """
        if not self._recovered:
            raise PersistenceError(
                "the review record was not completely recovered, so it is read-only: "
                "nothing may be appended to it and it may not be compacted")

    def _apply(self, event: dict, records: dict[str, Record] | None = None,
               scans: dict[str, str] | None = None) -> None:
        """Fold one journal event into ``records``/``scans``, defaulting to the live state.

        [R5-P11 / N7] The explicit targets are what let :meth:`_load` stage a replay without
        publishing it: a replay that fails half way through must leave the live record untouched
        rather than half updated.

        [R5-P13 / N7-R2] **Every field is validated, and anything this build cannot account for
        raises.** The original coerced instead: ``str(event.get("decision", PENDING))`` turned a
        missing decision into ``PENDING``, ``int(event.get("revision", 0))`` turned a missing
        revision into ``0``, and a ``decide`` event whose ``identity`` was not a string returned
        early -- a **silent no-op on a committed record**. An independent review replaced one
        committed decision with an identity-less ``decide`` event and measured the result: the
        decision recovered as ``PENDING``, ``corrupt_records_discarded`` stayed at ``0`` so even
        the counter said nothing was wrong, and compaction then truncated the 317 bytes that still
        held it. The same review found ``int("x")`` escaping as a bare ``ValueError`` and an
        unknown ``k`` ignored outright -- the first bypassing the typed refusal the GUI and the
        503 boundary are built on, the second discarding a record of a kind this build does not
        understand.

        A torn write cannot produce any of these: truncating one of this writer's records always
        leaves JSON that will not parse, so an event that *parses* was written whole. Strictness
        is therefore free of the torn-tail concern, and the two are kept apart deliberately --
        damage is :meth:`_replay_journal`'s business, meaning is this method's.

        ``at`` is the only optional field, because it is display metadata rather than part of the
        decision. Raises :class:`_EventSchemaError`, which the caller types.

        **A validator this strict must not be stricter than the writer**, or a decision this module
        accepted and reported successful would be refused on the next restart -- the same
        false-failure class the torn-tail repair exists to avoid. So ``identity`` is required to be
        a *string* and not required to be non-empty: production identities are 64-character
        SHA-256 digests, but :meth:`decide` does not itself forbid an empty one, and inventing a
        new write-time refusal to justify a read-time one would be the wrong way round. ``owner``
        *is* required non-empty, because ``_require_owner`` already guarantees that at the only
        site that writes it. ``test_n7_every_event_this_module_writes_satisfies_its_own_replay_validator``
        pins the agreement for all three event shapes.
        """
        records = self._records if records is None else records
        scans = self._scans if scans is None else scans
        if not isinstance(event, dict):
            raise _EventSchemaError("a journal event is not an object")
        kind = event.get("k")
        if kind not in _JOURNAL_KINDS:
            raise _EventSchemaError("a journal event has no kind this build understands")
        if kind == _JOURNAL_TAIL_REPAIRED:
            _require_number(event, "at")
            return
        if kind == _JOURNAL_SCAN:
            scans[_require_text(event, "owner", allow_empty=False)] = \
                _require_text(event, "scan_id")
            return
        identity = _require_text(event, "identity")
        decision = _require_text(event, "decision", allow_empty=False)
        if decision not in DECISIONS:
            raise _EventSchemaError("'decision' is not a known decision")
        revision = event.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise _EventSchemaError("'revision' is not a non-negative integer")
        records[identity] = Record(identity, decision, revision,
                                   _require_text(event, "scan_id"),
                                   _require_number(event, "at", optional=True))

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

    def _journal_tail_bytes(self) -> bytes:
        """[R5-P15 / N7-R3] The bytes after the journal's last newline. Bounded, or raises.

        Walked **backwards** in blocks from end of file, so the ordinary case is one read of one
        block rather than a pass over the whole journal: classifying one final line must never
        become an unbounded read, because the journal grows to
        :data:`COMPACT_AFTER_RECORDS` records between compactions.

        Two conditions are refusals rather than answers, for the same reason every other
        inspection failure on this path is:

        * an ``OSError`` -- the tail's content is not known, and an unknown tail is not a licence
          to write anything after it (the R5-P5 / N2 rule, applied to content as it already is to
          termination);
        * a final line longer than :data:`MAX_TAIL_INSPECT_BYTES` -- it cannot be classified
          within the bound, and the task this fix answers is explicit that an unclassifiable line
          is refused, not guessed at. No record this writer emits comes close to the bound.

        Opened by pathname while the append descriptor is held, exactly as
        :meth:`_journal_tail_is_terminated` already does; nothing has been written through that
        descriptor yet, so the bytes are stable.
        """
        try:
            with open(self.journal_path, "rb") as probe:
                probe.seek(0, os.SEEK_END)
                remaining = probe.tell()
                tail = b""
                while remaining > 0:
                    step = min(_TAIL_CHUNK_BYTES, remaining)
                    remaining -= step
                    probe.seek(remaining)
                    block = probe.read(step)
                    if len(block) != step:
                        raise PersistenceError(
                            "could not read the end of the review journal, so its final record "
                            "could not be identified; nothing has been changed")
                    cut = block.rfind(b"\n")
                    if cut >= 0:
                        tail = block[cut + 1:] + tail
                        break
                    tail = block + tail
                    # Bound the accumulation itself, so a journal with no newline at all cannot
                    # be read into memory whole.
                    self._require_inspectable_tail(tail)
        except OSError as exc:
            raise PersistenceError(
                f"could not read the review journal tail ({_io_detail(exc)})") from exc
        # And bound the result. The incremental check above cannot be the only one: the block
        # that *finds* the newline exits the walk, so a line a little over the ceiling would
        # otherwise be returned without ever having been measured. Caught by its own test.
        self._require_inspectable_tail(tail)
        return tail

    @staticmethod
    def _require_inspectable_tail(tail: bytes) -> None:
        if len(tail) > MAX_TAIL_INSPECT_BYTES:
            raise PersistenceError(
                "the review journal's final record is too large to identify within "
                f"{MAX_TAIL_INSPECT_BYTES} bytes, so whether it holds a committed decision "
                "cannot be established; nothing has been changed")

    def _classify_unterminated_tail(self, raw: bytes) -> str:
        """[R5-P15 / N7-R3] Which of the four shapes an unterminated final line is.

        The distinction rests on the same fact the strict replay validator rests on, read the
        other way round: **truncating one of this writer's records always leaves JSON that will
        not parse**, because the closing brace is the last byte before the newline. So a
        newline-less final line that *does* parse whole was written whole -- its decision may well
        have been reported successful -- and the one thing that must not happen to it is being
        relabelled as damage. R5-P13's docstrings asserted the converse ("an unterminated line is
        one whose decision was never reported successful"); that holds for this writer's own
        interrupted writes and not for the file, which is the gap N7-R3 occupied.

        ``_apply`` is called with **throwaway** dicts, never the live record: validating a tail
        must not publish it. Validation is deliberately the *same* code the replay path runs, so
        this can never accept a record the reader would later refuse.

        The two fail-closed shapes are worth stating explicitly:

        * :data:`_TAIL_INVALID_EVENT` -- complete JSON, but no record this build can account for.
          Completing it would publish an unaccountable record; marking it as repaired damage would
          destroy evidence of one. Neither is safe, so nothing is written.
        * :data:`_TAIL_TRAILING_BYTES` -- a complete value followed by something unidentifiable.
          This is the combined shape: a *previous* build's repair could have stamped a partial
          marker onto a valid record. The complete prefix may be a committed decision, so stamping
          a marker over it is exactly the N7-R3 loss with an extra step; but recovering it would
          mean trusting a line that parses only when trailing bytes are ignored, which is the
          laundering route :meth:`_damage_is_accounted_for` is bounded to prevent. The information
          to choose safely is not on disk, so this refuses and keeps every byte. **The fixed writer
          cannot produce this shape** -- it writes a lone newline after a valid record, and a
          one-byte write is all-or-nothing -- so no store this build wrote can reach it.

        [R5-P17 / N7-R6] **A line that is not text is still one of those four shapes, and this
        used to call every one of them torn.** R5-P15 read ``UnicodeDecodeError`` as proof of an
        unfinished write, which is true of an interruption inside a multi-byte character and false
        of a complete committed record with an invalid byte stuck on the end. An independent
        review appended ``0xff`` to a whole decision event and watched this answer
        ``torn_fragment``, the repair stamp the committed record as accounted-for damage, and the
        next decision come back successful -- against history the reader had already refused. So
        the undecodable branch below asks the same complete-prefix question the text branch asks,
        bytewise, and only a fragment with no whole record hiding in it is torn.
        """
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            fragment = raw.strip()
            if self._bytes_hide_a_complete_record(fragment):
                return _TAIL_TRAILING_BYTES
            if not self._bytes_are_an_interrupted_write(fragment):
                # [R5-P19 / N7-R7] The writer and the reader have to agree, so the append path
                # asks the same positive question :meth:`_replay_journal` now asks: not "have I
                # failed to find a reason to refuse?" but "can I show this writer produced these
                # bytes?". An invalid byte in front of an intact committed event answers no.
                return _TAIL_UNACCOUNTED_DAMAGE
            # A torn write can stop mid multi-byte sequence. Not text, and nothing whole is hiding
            # behind the broken bytes, so this is genuine damage, repaired like any other -- and
            # :meth:`_replay_journal` can now read the repaired result, which is what makes
            # repairing it honest rather than a success the next restart cannot honour.
            return _TAIL_TORN_FRAGMENT
        line = text.strip()
        if not line:
            return _TAIL_TORN_FRAGMENT
        try:
            event = json.loads(line)
        except ValueError:
            try:
                _value, end = json.JSONDecoder().raw_decode(line)
            except ValueError:
                if not self._text_is_an_interrupted_write(line):
                    return _TAIL_UNACCOUNTED_DAMAGE
                return _TAIL_TORN_FRAGMENT          # the ordinary F4 torn tail
            if line[end:].strip():
                return _TAIL_TRAILING_BYTES
            # Unreachable: json.loads tolerates trailing whitespace, so it would have parsed.
            return _TAIL_TORN_FRAGMENT
        try:
            self._apply(event, {}, {})
        except _EventSchemaError:
            return _TAIL_INVALID_EVENT
        return _TAIL_COMPLETE_EVENT

    def _terminate_damaged_tail(self, handle: int) -> bool:
        """[R5-P3 / F4] Close off a newline-less tail before appending after it.

        [R5-P15 / N7-R3] **A newline-less final line does not by itself mean an unfinished
        record**, and this method's first job is now to say which of the four shapes in
        :meth:`_classify_unterminated_tail` it is. Only a fragment that does not parse is repaired
        with the marker; a complete, schema-valid record missing only its terminator is *completed*
        with the one byte it lacks and keeps its exact original bytes; the two unidentifiable
        shapes write nothing at all. The paragraphs below describe the torn-fragment path, which
        is unchanged.

        A newline-less *unparsable* tail means a previous append died part way through its record
        -- either a short write that then failed, or the process dying mid-write. The original
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

        [R5-P13 / N7-R2] The repair now writes **durable evidence that it happened**, appended
        onto the damaged fragment itself and terminated in the same call. A bare newline left the
        damaged line looking exactly like corruption of an earlier historical record -- which is
        the thing :meth:`_replay_journal` must now refuse -- so without evidence the previously
        accepted F4 sequence would stop recovering on its second restart: the fragment is no
        longer last, and the decision written after it was already reported successful.

        The marker goes **onto** the fragment rather than onto a line of its own because
        ``_write_all`` guarantees all-the-bytes-or-a-raise per call but not atomicity across an
        interruption. Writing ``"\\n" + marker`` was the first attempt and it has a one-byte hole:
        interrupted after the newline alone, the fragment is closed off and undocumented, the next
        append sees a terminated tail and adds no marker, and the restart after that refuses a
        reported success. Appending the marker first closes that hole -- any prefix of this write
        leaves the fragment still unterminated, hence still the trailing damage, and the next
        append repairs it again. See :meth:`_damage_is_accounted_for`.
        """
        if self._journal_tail_is_terminated(handle):
            return False
        shape = self._classify_unterminated_tail(self._journal_tail_bytes())
        if shape == _TAIL_COMPLETE_EVENT:
            # [R5-P15 / N7-R3] A whole record that lost only its newline. Writing the newline and
            # nothing else keeps its original bytes parseable for ever; stamping a marker onto it
            # -- which is what R5-P13 did to every newline-less tail -- converted a decision that
            # had been reported successful into discardable damage, and the next compaction then
            # erased it. One byte, so there is no partially written state to recover from either.
            self._write_all(handle, b"\n")
            self.journal_tails_completed += 1
            return True
        if shape == _TAIL_INVALID_EVENT:
            raise PersistenceError(
                "the review journal's final record is complete but does not describe a record "
                "this build can account for, so it can be neither completed nor set aside "
                "safely; nothing has been changed and both copies are exactly as they were")
        if shape == _TAIL_TRAILING_BYTES:
            raise PersistenceError(
                "the review journal's final record is followed by an unidentifiable partial "
                "write, so whether it held a committed decision cannot be established; nothing "
                "has been changed and both copies are exactly as they were")
        if shape == _TAIL_UNACCOUNTED_DAMAGE:
            raise PersistenceError(
                "the review journal's final record is damaged in a way this process cannot have "
                "caused, so whether a committed decision is hidden behind the damage cannot be "
                "established; nothing has been changed and both copies are exactly as they were")
        marker = json.dumps({"k": _JOURNAL_TAIL_REPAIRED, "at": time.time()},
                            separators=(",", ":"), ensure_ascii=False)
        self._write_all(handle, (marker + "\n").encode("utf-8"))
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

        [R5-P11 / N7] It also includes not having *recovered* the journal: a store that could not
        read its own record in full must not add to it, because the record it would be adding to
        is not the one on disk.
        """
        self._require_recovered()
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

        [R5-P11 / N7] **Both of those steps destroy the previous copy of the record, so this is
        the one operation an incompletely recovered store may never perform.** Replacing the
        snapshot discards the bytes it was built from, and truncating the journal discards the
        replay that would have corrected an incomplete load. Memory is the only input, so a
        store that holds less than the disk did overwrites the disk with the loss -- which is
        precisely what turned a transient read failure into a permanent one. A measured
        consequence worth stating: the destructive step needed no new decision at all. A journal
        denied at start-up, then released, was already unrecoverable once this ran.
        """
        self._require_recovered()
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
    "compare_destinations",
    "validate_one_destination",
    "validate_roots",
]
