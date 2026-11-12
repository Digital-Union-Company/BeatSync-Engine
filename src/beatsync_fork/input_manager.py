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

A scan is authoritative only if traversal completed, so the contract is deliberately all-or-nothing::

    complete traversal   -> InputSet
    incomplete traversal -> InputScanError

A returned :class:`InputSet` therefore means every directory in the tree was read. Individual files
that cannot be stat'ed are a different matter: those are recorded as ``UNREADABLE`` rejections and
remain visible in the report, because a named rejection is not a silent loss.

A scan is also authoritative only if everything it counted really is *in* the selected folder, so
the same all-or-nothing rule covers containment: a nested junction or mount point is never
descended, and because its exclusion cannot be reported through :class:`InputSet`, finding one
refuses the scan instead of publishing a complete-looking total. See
:func:`_iter_candidate_paths` for why a nested symlink is deliberately treated differently.

A caller that cannot afford an unbounded tree may pass ``max_entries``, which keeps the contract
intact by **refusing** rather than returning a short result — the third way to fail, alongside an
unlistable directory and a nested junction, and never a fourth way to succeed. It defaults to
``None``, every caller in this repository leaves it there, and :func:`scan_folder` documents exactly
what it does and does not bound.

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
from typing import Callable, Iterable, Iterator, Sequence

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


class InputScanLimitExceeded(InputScanError):
    """The tree holds more entries than the caller allowed itself to read.

    A **subclass**, deliberately, so that every existing ``except InputScanError`` keeps working
    unchanged — all seven call sites in this repository already surface that refusal to the
    operator, and a containment refusal and a limit refusal need the same handling: the scan did
    not complete, so nothing it saw may be published as a total.

    ``limit`` is the cap that was passed; ``counted_at_least`` is how many entries had been
    accounted for when the cap was reached. ``counted_at_least`` is a **lower bound and never a
    total** — the tree holds more than that, which is precisely why the scan was refused.
    """

    def __init__(self, message: str, *, limit: int, counted_at_least: int) -> None:
        super().__init__(message)
        self.limit = int(limit)
        self.counted_at_least = int(counted_at_least)


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


def _walk_error_raiser(root: str) -> Callable[[OSError], None]:
    """Build the ``onerror`` callback that turns a partial walk into a hard failure.

    ``os.walk`` **ignores** ``scandir`` failures unless ``onerror`` is supplied. Without this, an
    unreadable, vanished or disconnected subdirectory is skipped in silence and the scan still returns
    an ``InputSet`` that can report INPUT READY — the same silent-truncation failure this module exists
    to prevent, just sourced from the filesystem instead of the browser. A scan is authoritative only
    if traversal completed, so an incomplete traversal raises.

    ``os.walk`` passes the callback only the error, not the path it was walking, so the error's own
    ``filename`` is used when present and the scan root is the fallback context.
    """

    def raise_scan_error(error: OSError) -> None:
        location = getattr(error, "filename", None) or root
        raise InputScanError(
            f"Cannot scan folder tree: traversal failed at {location} ({error})"
        ) from error

    return raise_scan_error


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
"""``FILE_ATTRIBUTE_REPARSE_POINT``. A junction, a volume mount point and a symlink all carry it."""

_PLAIN_DIRECTORY = ""
_NESTED_SYMLINK = "symlink"
_NESTED_REPARSE = "reparse"


def _nested_link_kind(path: str) -> str:
    """Classify a child directory for traversal safety, from **link-level** metadata only.

    Returns one of :data:`_PLAIN_DIRECTORY`, :data:`_NESTED_SYMLINK` or :data:`_NESTED_REPARSE`.

    Raises:
        InputScanError: the entry's link metadata could not be read, so whether reading it would
            leave the selected folder is **undecidable**. It fails closed, because treating an
            unreadable attribute as "an ordinary folder" is precisely the assumption this function
            exists to remove.

    Deliberately **not** ``library_inventory.is_reparse_point`` or ``cache_admin._is_reparse``: both
    answer ``False`` when the attribute cannot be read. That is the right shape for a *reporting*
    predicate and the wrong shape for a *gate* — an unreadable attribute is not evidence of a plain
    directory. ``os.lstat`` is resolved from the ``os`` namespace at call time so a test can inject a
    failure the same way the traversal-error suite injects ``os.scandir``.
    """
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise InputScanError(
            f"Cannot scan folder tree: cannot tell whether {path} is a real folder or a link "
            f"({exc}), so it is unknown whether reading it would leave the selected folder."
        ) from exc

    if stat_module.S_ISLNK(info.st_mode):
        return _NESTED_SYMLINK

    attributes = getattr(info, "st_file_attributes", None)
    if attributes is None:
        if os.name == "nt":
            raise InputScanError(
                f"Cannot scan folder tree: {path} reported no link metadata, and on this platform "
                f"a junction cannot be told apart from an ordinary folder without it, so reading "
                f"it cannot be shown to stay inside the selected folder."
            )
        return _PLAIN_DIRECTORY

    return _NESTED_REPARSE if attributes & _FILE_ATTRIBUTE_REPARSE_POINT else _PLAIN_DIRECTORY


def _iter_candidate_paths(root: str, recursive: bool) -> Iterator[str]:
    """Yield every file entry under ``root`` in a deterministic order.

    Nested directory links are never descended, so a self-referential link cannot make the walk
    unbounded **and** no file stored outside the selected folder can reach the classifier. Two kinds
    are distinguished, because only one of them was ever actually contained:

    * A **symlink** is already excluded by ``os.walk``, which consults ``os.path.islink()``. That
      silent exclusion is long-standing, intended, and pinned by
      ``test_symlinked_directory_is_not_traversed``, so it is preserved exactly.
    * A **junction or volume mount point** is *not* excluded: ``os.path.islink()`` is ``False`` for
      one on Windows, so ``followlinks=False`` does not stop it and ``os.walk`` descends into its
      target. Measured on CPython 3.14.3: an unpruned junction was entered, and the files beneath it
      were ``stat``-ed and counted as part of the selected folder — 1776 of 1808 reported bytes came
      from outside the root. Pruning ``dirnames`` in place is what stops the descent, which is the
      same policy ``cache_admin._walk_contained`` already applies to the cache tree.

    A junction therefore makes the scan **refuse**, rather than quietly excluding it. Quietly
    excluding it would shrink the operator's source set while still reporting a complete-looking
    total, which is the silent-truncation failure this module exists to prevent; and the excluded
    directory cannot be reported through :class:`InputSet` without a new field that every caller
    would have to learn to display. The refusal names the link and says what to do about it, and
    every existing caller already surfaces ``InputScanError`` to the operator.

    Raises ``InputScanError`` if any directory cannot be listed, if a nested junction is found, or if
    a child directory's link metadata cannot be read: the walk is all-or-nothing, never partial. Note
    that ``os.walk`` swallows ``entry.is_dir()`` failures internally and treats such an entry as a
    file; that entry then reaches the classifier and is accounted for as an ``UNREADABLE`` rejection,
    so it is still reported rather than lost.

    Both modes follow the same rule for *entries*: this function decides only "directory or not", and
    anything that is not a directory is passed to the classifier, which owns extension, ``stat``,
    regular-file, empty-file and ``UNREADABLE`` handling. No entry may be dropped here, because an
    entry dropped here is invisible in every count the report publishes.

    The selected ``root`` itself is never classified, so a folder the operator explicitly chose stays
    usable even when it is itself a junction: the decision being made here is only ever about a
    directory the operator did *not* choose.
    """
    if recursive:
        walk = os.walk(root, onerror=_walk_error_raiser(root), followlinks=False)
        for dirpath, dirnames, filenames in walk:
            kept: list[str] = []
            refused: str | None = None
            for name in sorted(dirnames):
                child = os.path.join(dirpath, name)
                kind = _nested_link_kind(child)
                if kind == _NESTED_SYMLINK:
                    continue
                if kind == _NESTED_REPARSE:
                    if refused is None:
                        refused = child
                    continue
                kept.append(name)
            # Assigned before the refusal below, so the walk cannot descend a link even if this
            # generator is resumed or abandoned: containment is structural, not a side effect of
            # raising. `kept` is built from sorted(dirnames), so ordering stays deterministic.
            dirnames[:] = kept
            if refused is not None:
                raise InputScanError(
                    f"Cannot scan folder tree: {refused} is a junction or mount point inside the "
                    f"selected folder, so the files it leads to are stored somewhere else. They "
                    f"were not counted, and the scan was refused rather than reporting a total "
                    f"that mixes in files from another location. To include them, select that "
                    f"target as a source folder in its own right."
                )
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)
        return

    entries: list[os.DirEntry] = []
    try:
        with os.scandir(root) as scanner:
            for entry in scanner:
                entries.append(entry)
    except OSError as exc:
        raise InputScanError(f"Cannot list folder: {root} ({exc})") from exc

    for entry in sorted(entries, key=lambda item: item.name):
        # Exclude directories only. Everything else — including an entry whose metadata cannot be
        # read — is handed to the classifier, which accounts for it explicitly (UNREADABLE /
        # NOT_A_FILE / EMPTY_FILE) instead of dropping it.
        #
        # This must not be written as `if os.path.isfile(entry.path)`: os.path.isfile() *suppresses*
        # stat/access errors and returns False, so a locked or vanished top-level source file
        # disappeared before the classifier saw it — absent from ready, absent from rejected, and
        # missing from discovered_count, so even the counting invariant could not detect the loss.
        try:
            is_directory = entry.is_dir()
        except OSError:
            # Same policy as os.walk, which treats an entry it cannot classify as a non-directory:
            # account for it downstream rather than silently skip it.
            is_directory = False
        if not is_directory:
            yield entry.path


def scan_folder(
    root: str,
    recursive: bool = True,
    extensions: Iterable[str] | None = None,
    detect_duplicates: bool = True,
    chunk_bytes: int = FINGERPRINT_CHUNK_BYTES,
    max_entries: int | None = None,
) -> InputSet:
    """Enumerate source video files under ``root`` and account for every entry seen.

    Args:
        root: Folder to scan. Must exist and be a directory.
        recursive: Descend into subdirectories.
        extensions: Override the accepted extensions (matched case-insensitively, leading dot
            optional). Defaults to :data:`SUPPORTED_VIDEO_EXTENSIONS`.
        detect_duplicates: Compute duplicate candidates. Set ``False`` to skip all file reads.
        chunk_bytes: Head/tail size for fingerprinting.
        max_entries: Optional ceiling on how many discovered entries this caller is willing to
            classify. ``None`` — the default, and what every caller in this repository passes —
            means no ceiling and behaviour identical to before the parameter existed. See
            *Bounding* below.

    Returns:
        An :class:`InputSet` covering a **complete** traversal. Nothing is copied, moved, modified or
        deleted.

    Raises:
        ValueError: ``max_entries`` is neither ``None`` nor a positive ``int``. Checked **before any
            filesystem access**, because passing ``0`` or ``-1`` is a programming error in the
            caller, not an observation about the folder, and must not be reported as one.
        InputScanLimitExceeded: the tree holds **more than** ``max_entries`` entries.
        InputScanError: ``root`` is empty, missing, not a directory or unlistable, **or** any
            directory in the tree could not be listed. Traversal is all-or-nothing: a returned
            :class:`InputSet` always means every directory was read successfully, so an incomplete
            scan can never masquerade as a smaller library.

    Bounding, and what ``max_entries`` honestly is
    ----------------------------------------------
    It is a **classification-entry ceiling**. It is *not* a wall-clock bound and *not* a peak-memory
    bound, and it must never be described as either.

    * It counts **discovered entries**, not supported media files. Cost is incurred per entry seen,
      so a ceiling on accepted files alone would leave a directory of ten million ``.txt`` files
      entirely unbounded.
    * Exactly ``max_entries`` entries **succeeds**; the entry *after* that refuses. Proving there is
      a further entry requires pulling one, which is what separates "there are more" from "there are
      exactly this many".
    * Reaching it **raises** rather than returning a short :class:`InputSet`. The all-or-nothing
      contract is the point of this module, and a truncated success would also be a *containment*
      lie: measured, a ceiling reached before a nested junction means the junction is never seen, so
      a short result is no evidence that the tree is contained.
    * **What it cannot bound.** ``os.walk`` materialises one directory's entries completely before
      it yields the first of them, and :func:`_nested_link_kind` takes one ``os.lstat`` per child
      directory before any of that directory's files are yielded. Measured on CPython 3.14.3: a
      single directory of 20,000 files with a ceiling of 1 still enumerated all 20,000 entries at
      identical peak memory, and a root holding 4,000 subdirectories still performed 4,000 ``lstat``
      calls. The irreducible floor is therefore O(entries in the largest single directory) +
      O(subdirectories of each visited directory), and only the per-entry classification cost above
      that floor is actually bounded.
    """
    started = time.perf_counter()

    # Before any filesystem access, so an invalid ceiling is always the same deterministic error
    # regardless of whether `root` happens to exist. `isinstance(True, int)` is True in Python, so
    # bool is excluded explicitly rather than being silently accepted as 1.
    if max_entries is not None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int):
            raise ValueError(
                f"max_entries must be a positive integer or None, not {type(max_entries).__name__}"
            )
        if max_entries < 1:
            raise ValueError(f"max_entries must be a positive integer or None, not {max_entries}")

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
        if max_entries is not None and discovered >= max_entries:
            # Checked BEFORE the increment, so `discovered` and the accounting below stay exactly
            # consistent with one another, and so this entry is never half-counted. Reaching here
            # at all means the generator produced one more entry than the ceiling allowed, which
            # is the proof that the tree is larger -- a ceiling equal to the entry count exhausts
            # the generator instead and succeeds.
            raise InputScanLimitExceeded(
                f"Cannot scan folder tree: {resolved_root} holds more than the {max_entries} "
                f"entries this inspection allowed itself to read, so the inventory was not "
                f"completed and no count for this folder is a total. Narrow the folder, or raise "
                f"the limit.",
                limit=max_entries,
                counted_at_least=discovered,
            )
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
    "InputScanLimitExceeded",
    "InputSet",
    "MediaFile",
    "RejectReason",
    "RejectedFile",
    "find_duplicate_groups",
    "fingerprint_file",
    "order_key",
    "scan_folder",
]
