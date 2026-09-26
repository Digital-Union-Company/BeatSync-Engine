#!/usr/bin/env python3
"""Source-set identity, explicit confirmation, and the pre-render gate.

Why a snapshot rather than a count
----------------------------------
Confirming "701 files" is not enough: two different source lists can have the same length. A user who
confirms 701 files, then swaps a folder, must not get a render from the new set under the old
confirmation. So confirmation is over a **snapshot** — an ordered identity of the actual source set —
and the render gate compares snapshots, not counts.

Identity is deliberately cheap: normalised path + size + mtime_ns per entry, plus the mode and (for
folder mode) the scan root and recursive flag. **No file contents are hashed here.** The
head+tail fingerprint in :mod:`beatsync_fork.input_manager` exists for duplicate *candidacy* and must
not become a per-render requirement — on a 400 GB library that would add gigabytes of reads to every
click.

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

SNAPSHOT_VERSION = "beatsync-source-snapshot-v1"
"""Namespace mixed into every digest. Bump if the identity construction changes, so digests can never
be compared across versions."""


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

    def is_empty(self) -> bool:
        return not self.entries

    def digest(self) -> str:
        """Deterministic SHA-256 over the ordered source identity plus mode metadata."""
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
        return hasher.hexdigest()

    def short_digest(self) -> str:
        return self.digest()[:12]

    def matches(self, other: "SourceSnapshot | None") -> bool:
        return other is not None and self.digest() == other.digest()


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
) -> SourceSnapshot:
    """Snapshot a folder scan result without re-stat'ing anything.

    ``media_files`` are :class:`beatsync_fork.input_manager.MediaFile` objects, which already carry
    ``path``, ``size`` and ``mtime_ns`` from the scan's single ``stat`` per file. Duck-typed rather
    than imported so this module stays independent of the scanner.
    """
    entries = tuple(
        SourceEntry(path=item.path, size=item.size, mtime_ns=item.mtime_ns) for item in media_files
    )
    return SourceSnapshot(
        mode=SourceMode.LOCAL_FOLDER,
        entries=entries,
        scan_root=os.path.abspath(scan_root) if scan_root else "",
        recursive=bool(recursive),
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

    def any_change(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.modified
            or self.reordered
            or self.mode_changed
            or self.root_changed
            or self.recursive_changed
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
    "SNAPSHOT_VERSION",
    "ConfirmationError",
    "GateDecision",
    "GateReason",
    "SnapshotDelta",
    "SourceEntry",
    "SourceMode",
    "SourceSnapshot",
    "SourceSnapshotError",
    "confirm_snapshot",
    "describe_change",
    "entry_for_path",
    "evaluate_gate",
    "snapshot_from_media_files",
    "snapshot_from_paths",
]
