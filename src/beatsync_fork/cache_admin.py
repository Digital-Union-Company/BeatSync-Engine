"""Inspecting and clearing the application's own caches, and nothing else.

BeatSync writes four kinds of disposable file and gives the operator no way to see any of them.
The remedy is deliberately small: measure what is there, let the operator choose categories, and
delete only inside a directory the *caller* named. The point of this module is the safety
argument, so that argument is stated rather than implied.

**Containment is the whole mechanism.** Nothing here discovers a directory to clean. A category's
root is supplied by `gui.py` from its own constants, and every path this module touches is proven
to lie inside that root by `realpath` + `commonpath` — never by a string prefix, which would treat
`output-old/` as part of `output/`. A path that cannot be proven to be inside is skipped, not
deleted. So the list of things that must never be cleared is enforced by *construction* rather
than by a blocklist: original input media, finished exports, Media Libraries, Discarded folders,
models, the portable runtime, saved preferences and the review decision journals are never handed
in as roots, and no code path here can reach them.

**Reparse points are never followed.** `os.walk` descends an NTFS junction, which would let a
junction inside the poster cache turn "clear 40 MB of thumbnails" into "delete whatever it points
at". Directory entries that are reparse points are therefore reported and skipped, never entered
and never removed. (Measured previously in this repository: `os.walk` really does descend
junctions, so this is a live hazard rather than a theoretical one.)

**The expensive category is separated from the cheap ones.** Re-deriving a poster is one FFmpeg
frame grab; re-deriving the Stage-5 analysis cache is the GPU/Qwen pass the cache exists to avoid.
They are different decisions, so `expensive` is a field on the category and the UI must say so.

**Measuring is not clearing**, and clearing is not recursive demolition: files are removed
individually and only directories that are empty afterwards are pruned, so a failure part-way
leaves a coherent tree rather than a half-deleted one. The root itself is always kept — the app
expects it to exist.

Stdlib only (`os`, `stat`), so the fork package's no-runtime-import rule still holds. No clock, no
randomness, and no knowledge of Gradio.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field

#: Category keys. Stable strings: they are what a checkbox group sends back and what a report
#: names, so renaming one is a UI-visible change rather than a refactor.
POSTERS = "posters"
PROCESSING = "processing"
RETIRED_UPLOADS = "retired_uploads"
ANALYSIS = "analysis"


@dataclass(frozen=True)
class Category:
    """One clearable cache. `root` is absolute and comes from the caller, never from here."""

    key: str
    label: str
    explanation: str
    root: str
    expensive: bool = False


@dataclass
class Measurement:
    """What one category currently holds. `skipped` names what was deliberately not counted."""

    key: str
    label: str
    exists: bool = False
    files: int = 0
    total_bytes: int = 0
    skipped: list = field(default_factory=list)
    error: str = ""
    expensive: bool = False


@dataclass
class ClearOutcome:
    """What one category's clear actually did. Counts are of real removals, never of intentions."""

    key: str
    label: str
    files_removed: int = 0
    bytes_removed: int = 0
    directories_removed: int = 0
    skipped: list = field(default_factory=list)
    failures: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _is_within(child: str, parent: str) -> bool:
    """`child` is `parent` or lies beneath it, by `commonpath` rather than string prefix."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return _norm(os.path.commonpath([c, p])) == p
    except ValueError:
        return False                                   # different drives


def _is_reparse(path: str) -> bool:
    try:
        attrs = os.lstat(path).st_file_attributes       # Windows only
    except (OSError, AttributeError):
        return False
    return bool(attrs & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _resolved_root(root: str) -> str | None:
    """The root's real location, or `None` if it cannot be established.

    `realpath` runs first so a root that is itself a junction — a legitimate, measured setup when
    someone points a cache at a bigger drive — is followed rather than refused. Containment is
    then measured on the resolved location, which is the same ordering `_publish_share_no_replace`
    uses in `gui.py` and for the same reason.
    """
    text = str(root or "").strip()
    if not text:
        return None
    try:
        return os.path.realpath(text)
    except OSError:
        return None


def format_bytes(count) -> str:
    """`1.4 GB`, `812 KB`, `0 B`. Decimal units, because that is what a file manager shows."""
    try:
        value = float(count)
    except (TypeError, ValueError):
        return "0 B"
    if value < 0:
        return "0 B"
    for unit, limit in (("B", 1000.0), ("KB", 1000.0), ("MB", 1000.0), ("GB", 1000.0)):
        if value < limit or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= limit
    return f"{value:.1f} TB"


def _walk_contained(root: str, resolved: str):
    """Yield `(directory, filenames)` for every real directory inside `resolved`.

    Reparse-point directories are yielded as skips instead of being entered, and any directory
    that `realpath` places outside the root is skipped too. `topdown=True` is required: pruning
    `dirnames` in place is what stops `os.walk` descending.
    """
    skipped: list[str] = []
    for current, dirnames, filenames in os.walk(resolved, topdown=True, followlinks=False):
        keep = []
        for name in dirnames:
            candidate = os.path.join(current, name)
            if _is_reparse(candidate):
                skipped.append(f"{name} (a junction or link — not followed)")
                continue
            keep.append(name)
        dirnames[:] = keep
        if not _is_within(current, resolved):
            skipped.append(f"{current} (outside the cache folder)")
            dirnames[:] = []
            continue
        yield current, filenames, skipped


def measure(category: Category) -> Measurement:
    """Count files and bytes in one category. Reads only; creates and deletes nothing.

    A missing root is `exists=False` with zero counts, not an error: the app creates these lazily,
    so "no poster cache yet" is the ordinary first-run state and must not look like a fault.
    """
    result = Measurement(key=category.key, label=category.label, expensive=category.expensive)
    resolved = _resolved_root(category.root)
    if resolved is None:
        result.error = "the folder path could not be resolved"
        return result
    if not os.path.isdir(resolved):
        return result
    result.exists = True
    try:
        for current, filenames, skipped in _walk_contained(category.root, resolved):
            result.skipped = skipped
            for name in filenames:
                path = os.path.join(current, name)
                if _is_reparse(path):
                    skipped.append(f"{name} (a junction or link — not counted)")
                    continue
                try:
                    result.files += 1
                    result.total_bytes += os.lstat(path).st_size
                except OSError:
                    result.files -= 1
                    skipped.append(f"{name} (could not be read)")
    except OSError as exc:
        result.error = str(getattr(exc, "strerror", None) or exc)
    return result


def clear(category: Category, *, remove=None, rmdir=None) -> ClearOutcome:
    """Delete the files in one category, then prune the directories that became empty.

    `remove` and `rmdir` are injected so every branch — a locked file, an unreadable directory,
    a junction — is testable without a disk. They default to `os.remove` and `os.rmdir`.

    `os.rmdir` is used rather than a recursive tree removal, and that is the safety property
    rather than a stylistic choice: `rmdir` refuses a directory that is not empty, so anything
    this function declined to delete (a junction, a file in use, a path it could not prove
    contained) keeps its parent directory alive instead of being swept away inside it. The root
    itself is never removed.

    A file another process holds open — a render still writing into `processing/`, a poster being
    served to the browser — fails with a reported reason and is left alone. That is the "do not
    clear files being used by active jobs" rule, enforced by the filesystem rather than by asking.
    """
    unlink = remove or os.remove
    drop = rmdir or os.rmdir
    outcome = ClearOutcome(key=category.key, label=category.label)

    resolved = _resolved_root(category.root)
    if resolved is None:
        outcome.failures.append("the folder path could not be resolved; nothing was deleted")
        return outcome
    if not os.path.isdir(resolved):
        return outcome                                 # nothing to do, and that is not a failure

    directories: list[str] = []
    try:
        for current, filenames, skipped in _walk_contained(category.root, resolved):
            outcome.skipped = skipped
            directories.append(current)
            for name in filenames:
                path = os.path.join(current, name)
                if _is_reparse(path):
                    skipped.append(f"{name} (a junction or link — not deleted)")
                    continue
                if not _is_within(path, resolved):
                    skipped.append(f"{name} (outside the cache folder — not deleted)")
                    continue
                try:
                    size = os.lstat(path).st_size
                except OSError:
                    size = 0
                try:
                    unlink(path)
                except OSError as exc:
                    outcome.failures.append(
                        f"{name}: {getattr(exc, 'strerror', None) or exc}")
                    continue
                outcome.files_removed += 1
                outcome.bytes_removed += size
    except OSError as exc:
        outcome.failures.append(str(getattr(exc, "strerror", None) or exc))

    # Deepest first, and never the root.
    for current in sorted(directories, key=len, reverse=True):
        if _norm(current) == _norm(resolved):
            continue
        try:
            drop(current)
        except OSError:
            continue                                   # not empty, or held: leave it
        outcome.directories_removed += 1
    return outcome


def report_lines(measurements) -> list[str]:
    """The inspection read-out. States totals, and names the expensive category as expensive."""
    rows = list(measurements)
    if not rows:
        return ["No cache categories are configured."]
    lines = ["Cache contents:", ""]
    total_files = total_bytes = 0
    for row in rows:
        if row.error:
            lines.append(f"  {row.label}: could not be read — {row.error}")
            continue
        if not row.exists:
            lines.append(f"  {row.label}: nothing cached yet")
            continue
        total_files += row.files
        total_bytes += row.total_bytes
        note = "  ⚠ expensive to rebuild" if row.expensive else ""
        lines.append(f"  {row.label}: {row.files} file(s), {format_bytes(row.total_bytes)}{note}")
        for skip in row.skipped[:4]:
            lines.append(f"      skipped {skip}")
    lines.append("")
    lines.append(f"Total: {total_files} file(s), {format_bytes(total_bytes)}")
    return lines


def cleared_lines(outcomes) -> list[str]:
    """What was actually deleted — counts of real removals, never of intentions."""
    rows = list(outcomes)
    if not rows:
        return ["Nothing was selected, so nothing was cleared."]
    lines = ["Cleared:", ""]
    total_files = total_bytes = 0
    for row in rows:
        total_files += row.files_removed
        total_bytes += row.bytes_removed
        lines.append(f"  {row.label}: {row.files_removed} file(s), "
                     f"{format_bytes(row.bytes_removed)} freed"
                     + (f", {row.directories_removed} empty folder(s) removed"
                        if row.directories_removed else ""))
        for skip in row.skipped[:4]:
            lines.append(f"      skipped {skip}")
        for failure in row.failures[:6]:
            lines.append(f"      could not delete {failure}")
    lines.append("")
    lines.append(f"Total freed: {total_files} file(s), {format_bytes(total_bytes)}")
    if any(row.failures for row in rows):
        lines.append("")
        lines.append("Anything a running job still had open was left exactly where it was.")
    return lines


__all__ = ["POSTERS", "PROCESSING", "RETIRED_UPLOADS", "ANALYSIS",
           "Category", "Measurement", "ClearOutcome",
           "format_bytes", "measure", "clear", "report_lines", "cleared_lines"]
