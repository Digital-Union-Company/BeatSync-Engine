#!/usr/bin/env python3
"""Local-folder source discovery for BeatSync.

Why this exists
---------------
The upstream GUI hands ``gr.File`` values straight to the pipeline, and the backend accepts whatever
list it is given (``gui._as_existing_source_paths`` silently drops anything that fails ``isfile``, and
nothing compares the result against an expected count). A partially-uploaded selection therefore
renders successfully from a truncated source set with no warning — observed in practice as 329 of ~701
selected files reaching Stage 5.

This module is the backend-authoritative alternative: it enumerates a local folder itself, so every
count it reports is exact by construction rather than inferred from browser state. It performs
**discovery and accounting only**.

Deliberate non-goals
--------------------
No media copying, no FFmpeg, no OpenCV, no Qwen, no rendering, no full-file hashing, and no Gradio
import. Stdlib only, so this is testable without the portable runtime.

Status
------
Core only. **Not wired into ``gui.py``.**
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Iterator, Sequence

SUPPORTED_VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mkv"})
"""Extensions recognised as source video, matched case-insensitively.

Deliberately identical to upstream's two accepted sets — ``gr.File(file_types=['.mp4', '.mkv'])`` in
``gui.py`` and ``get_video_files()`` in ``video_processor.py`` — so folder mode cannot accept a file
the existing pipeline would choke on. Widening this needs fixture-tested Stage 5 + Stage 6 support
first, which is why it is a parameter rather than a constant to edit.
"""

FINGERPRINT_CHUNK_BYTES: int = 1 << 20
"""Bytes read from the head and from the tail when fingerprinting (1 MiB each)."""

FINGERPRINT_STRATEGY_VERSION = "beatsync-fp-v1"
"""Namespace mixed into every digest. Bump if the fingerprint construction changes, so that digests
persisted by a later phase (edit plans, run manifests) can never be compared across strategies."""


class InputScanError(ValueError):
    """The scan root is missing, is not a directory, or cannot be listed."""


class RejectReason(str, Enum):
    """Why a discovered filesystem entry did not become a ready source file."""

    UNSUPPORTED_EXTENSION = "unsupported_extension"
    EMPTY_FILE = "empty_file"
    NOT_A_FILE = "not_a_file"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class MediaFile:
    """A source file that passed every cheap validation check."""

    path: str
    """Absolute, normalised path."""

    size: int
    mtime_ns: int
    extension: str
    """Lower-cased, including the leading dot."""


@dataclass(frozen=True, slots=True)
class RejectedFile:
    """A discovered entry excluded from the ready set, with the reason kept."""

    path: str
    reason: RejectReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Cheap content fingerprint used only for duplicate *candidacy*."""

    size: int
    digest: str
    strategy: str
    """``"whole"`` for small files, ``"head_tail"`` for large ones."""


@dataclass(frozen=True, slots=True)
class FingerprintError:
    """A file whose fingerprint could not be computed.

    Recorded rather than swallowed: an unreadable file must never be silently treated as equal to
    anything. Files listed here stay in the ready set — a fingerprint failure means "duplication
    unknown", not "unusable".
    """

    path: str
    error: str


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    """Two or more ready files sharing a size and a fingerprint."""

    fingerprint: Fingerprint
    paths: tuple[str, ...]

    @property
    def extra_copies(self) -> int:
        """Files beyond the first in this group."""
        return max(0, len(self.paths) - 1)


@dataclass(frozen=True, slots=True)
class InputSet:
    """The complete, explicit result of scanning one folder.

    Counting invariant, asserted by the test suite::

        discovered_count == len(files) + len(rejected) + path_collisions
    """

    root: str
    recursive: bool
    extensions: frozenset[str]
    files: tuple[MediaFile, ...]
    """Ready files, in deterministic order. Duplicates are **included** — they are reported, never
    removed on the user's behalf."""

    rejected: tuple[RejectedFile, ...]
    duplicate_groups: tuple[DuplicateGroup, ...]
    fingerprint_errors: tuple[FingerprintError, ...]
    discovered_count: int
    path_collisions: int
    """Entries that resolved to a file already accounted for (e.g. a symlink beside its target).
    Counted, never multiplied into the ready set; the first entry in deterministic walk order is
    the one kept."""

    duplicates_checked: bool
    scan_seconds: float

    @property
    def ready_count(self) -> int:
        return len(self.files)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @property
    def supported_count(self) -> int:
        """Discovered entries whose extension is supported, before validity checks."""
        unsupported = sum(
            1 for item in self.rejected if item.reason is RejectReason.UNSUPPORTED_EXTENSION
        )
        return self.discovered_count - unsupported

    @property
    def total_ready_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def duplicate_extra_files(self) -> int:
        """Total redundant copies across all duplicate groups."""
        return sum(group.extra_copies for group in self.duplicate_groups)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files)

    def rejected_by_reason(self) -> dict[RejectReason, int]:
        counts: dict[RejectReason, int] = {reason: 0 for reason in RejectReason}
        for item in self.rejected:
            counts[item.reason] += 1
        return counts

    def is_ready(self) -> bool:
        """At least one usable source file was found."""
        return bool(self.files)


# ---------------------------------------------------------------------------
# Ordering and identity
# ---------------------------------------------------------------------------


def order_key(path: str) -> tuple[str, str]:
    """Deterministic, case-insensitive sort key for a path.

    Separators are normalised so ordering does not depend on the host separator, and the raw
    normalised path is the tie-breaker so that two paths differing only in case still have a total
    order. Without that tie-breaker, sorting would fall back to ``os.walk`` order, which is not
    guaranteed stable.
    """
    normalised = os.path.normpath(path).replace("\\", "/")
    return (normalised.casefold(), normalised)


def _identity_key(path: str) -> str:
    """Filesystem identity of a path, for collapsing duplicate discoveries of one file."""
    try:
        resolved = os.path.realpath(path)
    except OSError:
        resolved = os.path.abspath(path)
    return os.path.normcase(resolved)


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def fingerprint_file(
    path: str,
    size: int | None = None,
    chunk_bytes: int = FINGERPRINT_CHUNK_BYTES,
) -> Fingerprint:
    """Fingerprint a file cheaply: size plus its first and last ``chunk_bytes``.

    Design notes, after reviewing the roadmap proposal against real usage:

    * The size is mixed into the digest, so files of different lengths can never collide even if
      their head and tail bytes match. That matters for container formats where thousands of files
      can share a byte-identical header prefix.
    * Files of ``2 * chunk_bytes`` or less are hashed whole. This costs no more than the head+tail
      read would have, and it removes the head/tail overlap that would otherwise double-count bytes
      for small files.
    * A 400 GB library is never fully hashed: at most 2 MiB is read per file, and the caller
      (:func:`find_duplicate_groups`) only fingerprints files whose size is not already unique.

    Raises ``OSError`` on read failure; callers record that rather than assuming equality.
    """
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")

    if size is None:
        size = os.path.getsize(path)

    digest = hashlib.sha256()
    digest.update(FINGERPRINT_STRATEGY_VERSION.encode("ascii"))
    digest.update(b"\x00")
    digest.update(str(size).encode("ascii"))
    digest.update(b"\x00")

    with open(path, "rb") as handle:
        if size <= 2 * chunk_bytes:
            strategy = "whole"
            while True:
                block = handle.read(chunk_bytes)
                if not block:
                    break
                digest.update(block)
        else:
            strategy = "head_tail"
            digest.update(handle.read(chunk_bytes))
            handle.seek(-chunk_bytes, os.SEEK_END)
            digest.update(handle.read(chunk_bytes))

    return Fingerprint(size=size, digest=digest.hexdigest(), strategy=strategy)


def find_duplicate_groups(
    files: Sequence[MediaFile],
    chunk_bytes: int = FINGERPRINT_CHUNK_BYTES,
) -> tuple[tuple[DuplicateGroup, ...], tuple[FingerprintError, ...]]:
    """Group files that are *candidates* for being the same content.

    Two-stage, cheapest first: size is already known from ``stat``, so a file whose size is unique
    within the set cannot be a content duplicate and is never opened. Only size collisions are
    fingerprinted.

    Returns ``(groups, errors)``. Groups are ordered by their first member; members are ordered by
    :func:`order_key`. Nothing is deleted, and files that failed to fingerprint are excluded from
    grouping and reported instead.
    """
    by_size: dict[int, list[MediaFile]] = defaultdict(list)
    for item in files:
        by_size[item.size].append(item)

    by_digest: dict[tuple[int, str], list[MediaFile]] = defaultdict(list)
    fingerprints: dict[tuple[int, str], Fingerprint] = {}
    errors: list[FingerprintError] = []

    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        for item in candidates:
            try:
                fingerprint = fingerprint_file(item.path, size=size, chunk_bytes=chunk_bytes)
            except OSError as exc:
                errors.append(FingerprintError(path=item.path, error=str(exc)))
                continue
            key = (fingerprint.size, fingerprint.digest)
            fingerprints[key] = fingerprint
            by_digest[key].append(item)

    groups = [
        DuplicateGroup(
            fingerprint=fingerprints[key],
            paths=tuple(item.path for item in sorted(members, key=lambda f: order_key(f.path))),
        )
        for key, members in by_digest.items()
        if len(members) > 1
    ]
    groups.sort(key=lambda group: order_key(group.paths[0]))
    errors.sort(key=lambda error: order_key(error.path))
    return tuple(groups), tuple(errors)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _iter_candidate_paths(root: str, recursive: bool) -> Iterator[str]:
    """Yield every file entry under ``root`` in a deterministic order.

    Directory symlinks are not followed, so a self-referential link cannot make the walk unbounded.
    """
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)
        return

    try:
        with os.scandir(root) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as exc:
        raise InputScanError(f"Cannot list folder: {root} ({exc})") from exc
    for name in names:
        candidate = os.path.join(root, name)
        if os.path.isfile(candidate):
            yield candidate


def scan_folder(
    root: str,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
    detect_duplicates: bool = True,
    chunk_bytes: int = FINGERPRINT_CHUNK_BYTES,
) -> InputSet:
    """Enumerate source video files under ``root`` and account for every entry seen.

    Args:
        root: Folder to scan. Must exist and be a directory.
        recursive: Descend into subdirectories.
        extensions: Override the accepted extensions (matched case-insensitively, leading dot
            optional). Defaults to :data:`SUPPORTED_VIDEO_EXTENSIONS`.
        detect_duplicates: Compute duplicate candidates. Set ``False`` to skip all file reads.
        chunk_bytes: Head/tail size for fingerprinting.

    Returns:
        An :class:`InputSet`. Nothing is copied, moved, modified or deleted.

    Raises:
        InputScanError: ``root`` is empty, missing, not a directory, or unlistable.
    """
    started = time.perf_counter()

    if not root or not str(root).strip():
        raise InputScanError("No folder was provided.")

    resolved_root = os.path.abspath(os.path.expanduser(str(root)))
    if not os.path.exists(resolved_root):
        raise InputScanError(f"Folder does not exist: {resolved_root}")
    if not os.path.isdir(resolved_root):
        raise InputScanError(f"Not a folder: {resolved_root}")

    accepted = _normalise_extensions(extensions)

    ready: list[MediaFile] = []
    rejected: list[RejectedFile] = []
    seen_identities: set[str] = set()
    discovered = 0
    collisions = 0

    for candidate in _iter_candidate_paths(resolved_root, recursive):
        discovered += 1
        absolute = os.path.abspath(candidate)

        extension = os.path.splitext(absolute)[1].casefold()
        if extension not in accepted:
            # Checked before stat: an extension we do not accept is rejected for that reason
            # regardless of whether the file is also empty or unreadable, which keeps the
            # per-reason counts unambiguous.
            rejected.append(
                RejectedFile(absolute, RejectReason.UNSUPPORTED_EXTENSION, extension or "(none)")
            )
            continue

        try:
            info = os.stat(absolute)
        except OSError as exc:
            rejected.append(RejectedFile(absolute, RejectReason.UNREADABLE, str(exc)))
            continue

        if not stat_module.S_ISREG(info.st_mode):
            rejected.append(RejectedFile(absolute, RejectReason.NOT_A_FILE, "not a regular file"))
            continue

        if info.st_size == 0:
            rejected.append(RejectedFile(absolute, RejectReason.EMPTY_FILE, "0 bytes"))
            continue

        identity = _identity_key(absolute)
        if identity in seen_identities:
            collisions += 1
            continue
        seen_identities.add(identity)

        ready.append(
            MediaFile(
                path=absolute,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                extension=extension,
            )
        )

    ready.sort(key=lambda item: order_key(item.path))
    rejected.sort(key=lambda item: order_key(item.path))

    if detect_duplicates:
        duplicate_groups, fingerprint_errors = find_duplicate_groups(ready, chunk_bytes=chunk_bytes)
    else:
        duplicate_groups, fingerprint_errors = (), ()

    return InputSet(
        root=resolved_root,
        recursive=recursive,
        extensions=accepted,
        files=tuple(ready),
        rejected=tuple(rejected),
        duplicate_groups=duplicate_groups,
        fingerprint_errors=fingerprint_errors,
        discovered_count=discovered,
        path_collisions=collisions,
        duplicates_checked=detect_duplicates,
        scan_seconds=time.perf_counter() - started,
    )


def _normalise_extensions(extensions: Iterable[str] | None) -> frozenset[str]:
    """Normalise user-supplied extensions to lower-case, dot-prefixed form."""
    if extensions is None:
        return SUPPORTED_VIDEO_EXTENSIONS

    normalised: set[str] = set()
    for raw in extensions:
        value = str(raw).strip().casefold()
        if not value:
            continue
        if not value.startswith("."):
            value = "." + value
        normalised.add(value)
    if not normalised:
        raise InputScanError("No usable file extensions were provided.")
    return frozenset(normalised)


__all__ = [
    "FINGERPRINT_CHUNK_BYTES",
    "FINGERPRINT_STRATEGY_VERSION",
    "SUPPORTED_VIDEO_EXTENSIONS",
    "DuplicateGroup",
    "Fingerprint",
    "FingerprintError",
    "InputScanError",
    "InputSet",
    "MediaFile",
    "RejectReason",
    "RejectedFile",
    "find_duplicate_groups",
    "fingerprint_file",
    "order_key",
    "scan_folder",
]
