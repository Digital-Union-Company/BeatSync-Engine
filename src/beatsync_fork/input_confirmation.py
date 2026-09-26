#!/usr/bin/env python3
"""Source-set identity, explicit confirmation, and the pre-render gate.

Why a snapshot rather than a count
----------------------------------
Confirming "701 files" is not enough: two different source lists can have the same length. A user who
confirms 701 files, then swaps a folder, must not get a render from the new set under the old
confirmation. So confirmation is over a **snapshot** — an ordered identity of the actual source set —
and the render gate compares snapshots, not counts.

Identity is deliberately cheap: normalised path + size + mtime_ns per entry, plus the mode and (for
folder mode) the scan root, the recursive flag, and the supported-but-unusable files in scope. **No
file contents are hashed here.** The head+tail fingerprint in :mod:`beatsync_fork.input_manager`
exists for duplicate *candidacy* and must not become a per-render requirement — on a 400 GB library
that would add gigabytes of reads to every click.

Why a *live* declaration
------------------------
Event-driven state is not a safe basis for a gate. Gradio delivers widget changes as separate queued
events, so when Create is clicked the stored session state can lag behind the widgets. The render
handler therefore submits the live control values as a :class:`LiveSourceDeclaration`, and the gate
checks those — see :func:`check_declaration`.

What this module is not
-----------------------
No Gradio, no UI, no upstream runtime imports (see the hard rule in CLAUDE.md). Everything here is a
pure function or a frozen dataclass over the standard library, so the gate is fully testable without
starting a web server.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence

SNAPSHOT_VERSION = "beatsync-source-snapshot-v2"
"""Namespace mixed into every digest. Bump if the identity construction changes, so digests can never
be compared across versions. v2 added :attr:`SourceSnapshot.excluded`."""


class SourceMode(str, Enum):
    """Where the source video list came from."""

    LOCAL_FOLDER = "local_folder"
    BROWSER_FILES = "browser_files"


class SourceSnapshotError(RuntimeError):
    """A snapshot could not be built because a source path is missing or unreadable."""

    def __init__(self, message: str, paths: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.paths: tuple[str, ...] = tuple(paths)


class ConfirmationError(RuntimeError):
    """An empty or otherwise unconfirmable source set was submitted for confirmation."""


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One source file's cheap identity."""

    path: str
    """Absolute, normalised path."""

    size: int
    mtime_ns: int

    def identity_line(self) -> str:
        """Canonical, case-insensitive-on-Windows serialisation of this entry."""
        return f"{os.path.normcase(self.path)}\x1f{self.size}\x1f{self.mtime_ns}"


@dataclass(frozen=True, slots=True)
class ExcludedEntry:
    """A supported-extension source file that the scan could not use.

    Tracked in the snapshot because the *scope* of a confirmed folder is "the supported video files in
    it", not merely the usable subset. On a live library a new ``.mp4`` is frequently 0 bytes or
    briefly unreadable for its first moments; it is then rejected, the ready list is unchanged, and a
    ready-only identity would report "no change" while a new source entry had in fact appeared.

    Only the stable rejection *reason code* is recorded — never the OS error text, which varies with
    locale and phrasing and would make the digest unstable.
    """

    path: str
    reason: str
    """Stable reason code, e.g. ``"empty_file"`` or ``"unreadable"``."""

    def identity_line(self) -> str:
        return f"{os.path.normcase(self.path)}\x1f{self.reason}"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """The confirmed-or-current identity of a whole source set.

    Ordering is part of the identity: the planner's candidate order derives from the input order, so a
    reordered list is a different render input, not the same one.
    """

    mode: SourceMode
    entries: tuple[SourceEntry, ...]
    scan_root: str = ""
    """Folder mode only; empty for browser mode."""

    recursive: bool = False
    """Folder mode only."""

    excluded: tuple[ExcludedEntry, ...] = ()
    """Supported-extension files present in scope but not usable. Part of identity, never rendered.
    Unsupported files (``.mp3``, ``.txt``, …) are deliberately absent: they are not video sources, so
    adding one must not invalidate a confirmation."""

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def paths(self) -> tuple[str, ...]:
        """The exact ordered path list to hand to the render pipeline."""
        return tuple(entry.path for entry in self.entries)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    def is_empty(self) -> bool:
        return not self.entries

    def digest(self) -> str:
        """Deterministic SHA-256 over the ordered source identity plus scope metadata."""
        hasher = hashlib.sha256()
        hasher.update(SNAPSHOT_VERSION.encode("ascii"))
        hasher.update(b"\x1e")
        hasher.update(self.mode.value.encode("utf-8"))
        hasher.update(b"\x1e")
        # Scan root and recursion are part of identity: the same files reached via a different root or
        # recursion setting are a different declared intent, and re-validation must notice.
        hasher.update(os.path.normcase(self.scan_root).encode("utf-8", errors="replace"))
        hasher.update(b"\x1e")
        hasher.update(b"1" if self.recursive else b"0")
        hasher.update(b"\x1e")
        hasher.update(str(len(self.entries)).encode("ascii"))
        for entry in self.entries:
            hasher.update(b"\x1e")
            hasher.update(entry.identity_line().encode("utf-8", errors="replace"))
        # Excluded entries live in their own section, so a path can never be confused between the two
        # lists.
        hasher.update(b"\x1d")
        hasher.update(str(len(self.excluded)).encode("ascii"))
        for item in self.excluded:
            hasher.update(b"\x1e")
            hasher.update(item.identity_line().encode("utf-8", errors="replace"))
        return hasher.hexdigest()

    def short_digest(self) -> str:
        return self.digest()[:12]

    def matches(self, other: "SourceSnapshot | None") -> bool:
        return other is not None and self.digest() == other.digest()


@dataclass(frozen=True, slots=True)
class LiveSourceDeclaration:
    """The source controls' values **as submitted with the render request**.

    This exists because event-driven state is not a safe basis for a gate. Gradio delivers widget
    changes as separate queued events, so at the moment Create is clicked the stored session state can
    lag behind the widgets: a file can finish uploading, or the folder textbox can change, without its
    ``change`` handler having run yet. Comparing a confirmation against the stored state alone would
    then approve a render for a source set the user is no longer declaring.
    """

    mode: SourceMode
    folder_path: str = ""
    recursive: bool = False
    browser_paths: tuple[str, ...] = ()

    @property
    def normalised_folder(self) -> str:
        return os.path.abspath(self.folder_path) if self.folder_path.strip() else ""


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


def entry_for_path(path: str) -> SourceEntry:
    """Build one entry, raising :class:`SourceSnapshotError` if the file is gone or unreadable."""
    absolute = os.path.abspath(path)
    try:
        info = os.stat(absolute)
    except OSError as exc:
        raise SourceSnapshotError(
            f"Source file is missing or unreadable: {absolute} ({exc})", [absolute]
        ) from exc
    return SourceEntry(path=absolute, size=info.st_size, mtime_ns=info.st_mtime_ns)


def snapshot_from_paths(
    paths: Iterable[str],
    mode: SourceMode,
    scan_root: str = "",
    recursive: bool = False,
) -> SourceSnapshot:
    """Snapshot an explicit ordered path list, stat'ing every entry.

    Raises:
        SourceSnapshotError: any path is missing or unreadable. Reported rather than skipped — a
            source that cannot be stat'ed must never be quietly dropped from a confirmed set.
    """
    entries: list[SourceEntry] = []
    missing: list[str] = []
    for path in paths:
        try:
            entries.append(entry_for_path(path))
        except SourceSnapshotError as exc:
            missing.extend(exc.paths)
    if missing:
        raise SourceSnapshotError(
            f"{len(missing)} source file(s) are missing or unreadable.", missing
        )
    return SourceSnapshot(
        mode=mode,
        entries=tuple(entries),
        scan_root=os.path.abspath(scan_root) if scan_root else "",
        recursive=bool(recursive),
    )


def snapshot_from_media_files(
    media_files: Sequence,
    scan_root: str,
    recursive: bool,
    excluded: Iterable = (),
) -> SourceSnapshot:
    """Snapshot a folder scan result without re-stat'ing anything.

    ``media_files`` are :class:`beatsync_fork.input_manager.MediaFile` objects, which already carry
    ``path``, ``size`` and ``mtime_ns`` from the scan's single ``stat`` per file. ``excluded`` are the
    scanner's supported-extension rejections (``RejectedFile``-shaped: ``.path`` and ``.reason``).
    Both are duck-typed rather than imported so this module stays independent of the scanner.

    The caller filters ``excluded`` down to *supported* rejections — unsupported extensions are not
    video sources and must not participate in identity.
    """
    entries = tuple(
        SourceEntry(path=item.path, size=item.size, mtime_ns=item.mtime_ns) for item in media_files
    )
    excluded_entries = tuple(
        sorted(
            (
                ExcludedEntry(
                    path=os.path.abspath(item.path),
                    reason=str(getattr(item.reason, "value", item.reason)),
                )
                for item in excluded
            ),
            key=lambda item: os.path.normcase(item.path),
        )
    )
    return SourceSnapshot(
        mode=SourceMode.LOCAL_FOLDER,
        entries=entries,
        scan_root=os.path.abspath(scan_root) if scan_root else "",
        recursive=bool(recursive),
        excluded=excluded_entries,
    )


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def confirm_snapshot(snapshot: SourceSnapshot | None) -> SourceSnapshot:
    """Accept a snapshot as the confirmed source set.

    Raises:
        ConfirmationError: there is nothing to confirm. An empty source set can never be confirmed —
            confirming "0 files" would hand the pipeline an empty list under an approving label.
    """
    if snapshot is None:
        raise ConfirmationError("There is no scanned source set to confirm.")
    if snapshot.is_empty():
        raise ConfirmationError("No usable source videos to confirm.")
    return snapshot


# ---------------------------------------------------------------------------
# Change description and the render gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotDelta:
    """What changed between a confirmed snapshot and the current one."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    modified: tuple[str, ...]
    """Same path, different size or mtime."""

    reordered: bool
    mode_changed: bool
    root_changed: bool
    recursive_changed: bool
    excluded_added: tuple[str, ...] = ()
    """Supported video files that appeared in scope but are not usable (e.g. a new 0-byte ``.mp4``)."""

    excluded_removed: tuple[str, ...] = ()
    excluded_reason_changed: tuple[str, ...] = ()

    def any_change(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.modified
            or self.reordered
            or self.mode_changed
            or self.root_changed
            or self.recursive_changed
            or self.excluded_added
            or self.excluded_removed
            or self.excluded_reason_changed
        )

    def summary(self) -> str:
        """Short human-readable reason, ordered most-structural first."""
        parts: list[str] = []
        if self.mode_changed:
            parts.append("input mode changed")
        if self.root_changed:
            parts.append("folder changed")
        if self.recursive_changed:
            parts.append("subfolder setting changed")
        if self.added:
            parts.append(f"{len(self.added)} added")
        if self.removed:
            parts.append(f"{len(self.removed)} removed")
        if self.modified:
            parts.append(f"{len(self.modified)} modified")
        if self.excluded_added:
            parts.append(f"{len(self.excluded_added)} unusable source file(s) appeared")
        if self.excluded_removed:
            parts.append(f"{len(self.excluded_removed)} unusable source file(s) disappeared")
        if self.excluded_reason_changed:
            parts.append(f"{len(self.excluded_reason_changed)} source file(s) changed status")
        if self.reordered and not (self.added or self.removed):
            parts.append("order changed")
        return ", ".join(parts) if parts else "no change"


def describe_change(
    confirmed: SourceSnapshot | None, current: SourceSnapshot | None
) -> SnapshotDelta:
    """Diff two snapshots. Absent snapshots are treated as empty sets."""
    confirmed_entries = {} if confirmed is None else {
        os.path.normcase(e.path): e for e in confirmed.entries
    }
    current_entries = {} if current is None else {
        os.path.normcase(e.path): e for e in current.entries
    }

    added = tuple(
        sorted(current_entries[k].path for k in current_entries.keys() - confirmed_entries.keys())
    )
    removed = tuple(
        sorted(confirmed_entries[k].path for k in confirmed_entries.keys() - current_entries.keys())
    )
    modified = tuple(
        sorted(
            current_entries[k].path
            for k in confirmed_entries.keys() & current_entries.keys()
            if (
                confirmed_entries[k].size != current_entries[k].size
                or confirmed_entries[k].mtime_ns != current_entries[k].mtime_ns
            )
        )
    )

    confirmed_order = () if confirmed is None else tuple(
        os.path.normcase(e.path) for e in confirmed.entries
    )
    current_order = () if current is None else tuple(
        os.path.normcase(e.path) for e in current.entries
    )
    reordered = (
        not added
        and not removed
        and confirmed_order != current_order
    )

    confirmed_excluded = {} if confirmed is None else {
        os.path.normcase(e.path): e for e in confirmed.excluded
    }
    current_excluded = {} if current is None else {
        os.path.normcase(e.path): e for e in current.excluded
    }
    excluded_added = tuple(
        sorted(current_excluded[k].path for k in current_excluded.keys() - confirmed_excluded.keys())
    )
    excluded_removed = tuple(
        sorted(
            confirmed_excluded[k].path for k in confirmed_excluded.keys() - current_excluded.keys()
        )
    )
    excluded_reason_changed = tuple(
        sorted(
            current_excluded[k].path
            for k in confirmed_excluded.keys() & current_excluded.keys()
            if confirmed_excluded[k].reason != current_excluded[k].reason
        )
    )

    return SnapshotDelta(
        added=added,
        removed=removed,
        modified=modified,
        reordered=reordered,
        mode_changed=(confirmed is not None and current is not None and confirmed.mode != current.mode),
        root_changed=(
            confirmed is not None
            and current is not None
            and os.path.normcase(confirmed.scan_root) != os.path.normcase(current.scan_root)
        ),
        recursive_changed=(
            confirmed is not None and current is not None and confirmed.recursive != current.recursive
        ),
        excluded_added=excluded_added,
        excluded_removed=excluded_removed,
        excluded_reason_changed=excluded_reason_changed,
    )


class GateReason(str, Enum):
    """Why the render gate allowed or denied the run."""

    OK = "ok"
    NOT_CONFIRMED = "not_confirmed"
    NO_SOURCES = "no_sources"
    SOURCE_CHANGED = "source_changed"
    SOURCE_UNAVAILABLE = "source_unavailable"


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The verdict of the pre-render check, plus the exact list to render from."""

    allowed: bool
    reason: GateReason
    message: str
    paths: tuple[str, ...] = ()

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


SOURCE_CHANGED_MESSAGE = "SOURCE INPUT CHANGED\nPlease Scan Folder and Confirm again."
NOT_CONFIRMED_MESSAGE = "SOURCE NOT CONFIRMED\nConfirm the source videos before creating a video."


def check_declaration(
    confirmed: SourceSnapshot | None, live: LiveSourceDeclaration
) -> GateDecision | None:
    """Compare a confirmation's *declared intent* against the live controls.

    Returns a denial when the declaration itself has moved on, or ``None`` when it still matches and
    the caller should proceed to the filesystem comparison. Catching a changed mode or folder here also
    means a folder the user has navigated away from is never scanned.
    """
    if confirmed is None:
        return GateDecision(False, GateReason.NOT_CONFIRMED, NOT_CONFIRMED_MESSAGE)

    if live.mode is not confirmed.mode:
        return GateDecision(
            False,
            GateReason.SOURCE_CHANGED,
            f"{SOURCE_CHANGED_MESSAGE}\nDetected: input mode changed.",
        )

    if confirmed.mode is SourceMode.LOCAL_FOLDER:
        if os.path.normcase(live.normalised_folder) != os.path.normcase(confirmed.scan_root):
            return GateDecision(
                False,
                GateReason.SOURCE_CHANGED,
                f"{SOURCE_CHANGED_MESSAGE}\nDetected: folder changed.",
            )
        if bool(live.recursive) != bool(confirmed.recursive):
            return GateDecision(
                False,
                GateReason.SOURCE_CHANGED,
                f"{SOURCE_CHANGED_MESSAGE}\nDetected: subfolder setting changed.",
            )

    return None


def evaluate_gate(
    confirmed: SourceSnapshot | None,
    current: SourceSnapshot | None,
) -> GateDecision:
    """Decide whether a render may start, comparing a freshly-taken snapshot to the confirmed one.

    ``current`` must be re-derived from the filesystem at call time by the caller. Passing back the
    snapshot taken at confirmation time would make this check meaningless.
    """
    if confirmed is None or confirmed.is_empty():
        return GateDecision(False, GateReason.NOT_CONFIRMED, NOT_CONFIRMED_MESSAGE)

    if current is None:
        return GateDecision(
            False,
            GateReason.SOURCE_UNAVAILABLE,
            "SOURCE INPUT UNAVAILABLE\nThe source videos could not be re-checked. "
            "Please Scan Folder and Confirm again.",
        )

    if current.is_empty():
        return GateDecision(
            False,
            GateReason.NO_SOURCES,
            "NO SOURCE VIDEOS\nThe confirmed source videos are no longer available.",
        )

    if current.matches(confirmed):
        return GateDecision(True, GateReason.OK, "Source input verified.", current.paths)

    delta = describe_change(confirmed, current)
    return GateDecision(
        False,
        GateReason.SOURCE_CHANGED,
        f"{SOURCE_CHANGED_MESSAGE}\nDetected: {delta.summary()}.",
    )


__all__ = [
    "NOT_CONFIRMED_MESSAGE",
    "SNAPSHOT_VERSION",
    "SOURCE_CHANGED_MESSAGE",
    "ConfirmationError",
    "ExcludedEntry",
    "GateDecision",
    "GateReason",
    "LiveSourceDeclaration",
    "SnapshotDelta",
    "SourceEntry",
    "SourceMode",
    "SourceSnapshot",
    "SourceSnapshotError",
    "check_declaration",
    "confirm_snapshot",
    "describe_change",
    "entry_for_path",
    "evaluate_gate",
    "snapshot_from_media_files",
    "snapshot_from_paths",
]
