"""Traversal completeness: an incomplete scan must fail, never return a quietly smaller set.

``os.walk`` ignores ``scandir`` failures unless an ``onerror`` callback is supplied. Without one, an
unreadable, vanished or disconnected nested directory is skipped in silence and the scan still returns
an ``InputSet`` that can report INPUT READY — the exact class of silent truncation this module exists
to prevent, just moved from the browser to the filesystem.

The contract these tests pin::

    complete traversal   -> InputSet
    incomplete traversal -> InputScanError

Failures are injected by patching ``os.scandir`` for one specific directory name, so the tests are
deterministic and platform-independent. They deliberately do **not** manipulate ACLs or file modes,
which behave differently across Windows/POSIX and across privilege levels.
"""

from __future__ import annotations

import os

import pytest
from conftest import write_file

from beatsync_fork.input_manager import InputScanError, RejectReason, scan_folder

BLOCKED_DIRNAME = "blocked_subdir"


def _tree_with_nested_dir(tmp_path) -> tuple[str, str]:
    """A scannable root whose nested ``blocked_subdir`` holds a supported file."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"top")
    write_file(os.path.join(root, "ok_subdir", "nested.mkv"), b"nested")
    blocked = os.path.join(root, BLOCKED_DIRNAME)
    write_file(os.path.join(blocked, "hidden.mp4"), b"hidden")
    return root, blocked


def _patch_scandir_failure(monkeypatch, blocked_basename: str, exc_factory) -> None:
    """Make ``os.scandir`` raise for one directory basename, and behave normally elsewhere.

    ``os.walk`` resolves ``scandir`` from the ``os`` module namespace, so patching it here is what a
    real permission/IO failure looks like from ``os.walk``'s point of view.
    """
    real_scandir = os.scandir

    def failing_scandir(path=".", *args, **kwargs):
        if os.path.basename(os.path.normpath(os.fspath(path))) == blocked_basename:
            raise exc_factory()
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing_scandir)


def test_tree_would_otherwise_be_ready(tmp_path):
    """Baseline: without an injected failure this tree scans clean and reports ready.

    Without this, the error tests below could pass for the wrong reason - an empty tree.
    """
    root, _blocked = _tree_with_nested_dir(tmp_path)

    result = scan_folder(root, recursive=True)

    assert result.ready_count == 3
    assert result.is_ready() is True


def test_nested_traversal_error_is_surfaced(tmp_path, monkeypatch):
    root, blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: PermissionError(13, "Access is denied", blocked),
    )

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, recursive=True)

    assert BLOCKED_DIRNAME in str(caught.value)


def test_nested_traversal_error_does_not_return_a_partial_ready_set(tmp_path, monkeypatch):
    """The scan must not succeed with the two reachable files and claim INPUT READY."""
    root, blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: PermissionError(13, "Access is denied", blocked),
    )

    result = None
    try:
        result = scan_folder(root, recursive=True)
    except InputScanError:
        pass

    assert result is None, (
        "scan_folder returned an InputSet despite an unscannable subdirectory; "
        f"it reported ready={getattr(result, 'ready_count', None)} "
        f"is_ready={result.is_ready() if result is not None else None}"
    )


def test_vanished_directory_is_surfaced(tmp_path, monkeypatch):
    """Not just permissions: a directory that disappears mid-walk must fail the scan too."""
    root, blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: FileNotFoundError(2, "The system cannot find the path specified", blocked),
    )

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, recursive=True)

    assert BLOCKED_DIRNAME in str(caught.value)


def test_error_preserves_path_and_original_os_error(tmp_path, monkeypatch):
    root, blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: OSError(64, "The specified network name is no longer available", blocked),
    )

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, recursive=True)

    message = str(caught.value)
    assert blocked in message, "the failing directory path must be in the diagnostic"
    assert "network name is no longer available" in message, "the OS error text must be preserved"

    cause = caught.value.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno == 64
    assert cause.filename == blocked


def test_error_context_falls_back_to_the_root(tmp_path, monkeypatch):
    """``os.walk`` hands ``onerror`` only the error, so an error with no filename still needs context."""
    root, _blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: OSError("device not ready"),  # no errno, no filename
    )

    with pytest.raises(InputScanError) as caught:
        scan_folder(root, recursive=True)

    message = str(caught.value)
    assert os.path.abspath(root) in message
    assert "device not ready" in message


def test_root_scandir_failure_is_explicit_in_recursive_mode(tmp_path, monkeypatch):
    """A root that exists but cannot be listed must raise, not yield an empty InputSet."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"top")
    _patch_scandir_failure(
        monkeypatch,
        "library",
        lambda: PermissionError(13, "Access is denied", os.path.abspath(root)),
    )

    with pytest.raises(InputScanError):
        scan_folder(root, recursive=True)


def test_non_recursive_top_level_failure_remains_explicit(tmp_path, monkeypatch):
    """The pre-existing non-recursive guard is unchanged by this correction."""
    root = str(tmp_path / "library")
    write_file(os.path.join(root, "top.mp4"), b"top")
    _patch_scandir_failure(
        monkeypatch,
        "library",
        lambda: PermissionError(13, "Access is denied", os.path.abspath(root)),
    )

    with pytest.raises(InputScanError, match="Cannot list folder"):
        scan_folder(root, recursive=False)


def test_non_recursive_mode_ignores_unscannable_subdirectories(tmp_path, monkeypatch):
    """Non-recursive scans never descend, so a broken subdirectory is irrelevant, not fatal."""
    root, blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        BLOCKED_DIRNAME,
        lambda: PermissionError(13, "Access is denied", blocked),
    )

    result = scan_folder(root, recursive=False)

    assert [os.path.basename(p) for p in result.paths] == ["top.mp4"]
    assert result.is_ready() is True


def test_ordinary_recursive_scan_is_unaffected_by_the_guard(tmp_path, monkeypatch):
    """With the injection armed but targeting a name that does not exist, nothing changes."""
    root, _blocked = _tree_with_nested_dir(tmp_path)
    _patch_scandir_failure(
        monkeypatch,
        "a_directory_that_is_not_in_this_tree",
        lambda: PermissionError(13, "Access is denied", "nowhere"),
    )

    result = scan_folder(root, recursive=True)

    assert result.ready_count == 3
    assert [os.path.basename(p) for p in result.paths] == [
        "hidden.mp4",
        "nested.mkv",
        "top.mp4",
    ]
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )


def test_unreadable_file_is_still_a_rejection_not_a_scan_failure(tmp_path, monkeypatch):
    """Scope guard: the all-or-nothing rule covers *directory traversal*, not individual files.

    A file that cannot be stat'ed is already accounted for as an ``unreadable`` rejection, which is
    visible in the report. Only an incomplete traversal - where entries may be missing without anyone
    knowing - escalates to a hard failure.
    """
    root = str(tmp_path / "library")
    good = write_file(os.path.join(root, "good.mp4"), b"good")
    bad = write_file(os.path.join(root, "bad.mp4"), b"bad")

    _patch_unreadable_path(monkeypatch, bad)

    result = scan_folder(root, recursive=True, detect_duplicates=False)

    assert result.paths == (good,)
    assert result.rejected_count == 1
    assert result.rejected[0].path == bad
    assert result.rejected[0].reason.value == "unreadable"


# ---------------------------------------------------------------------------
# Non-recursive entry probing
#
# The recursive path is guarded above. The non-recursive path had the same class of hole one level
# down: it decided what to yield with os.path.isfile(), which *suppresses* stat/access errors and
# returns False. A top-level source file whose metadata could not be read therefore vanished before
# the classifier ever saw it - absent from `ready`, absent from `rejected`, and missing from
# `discovered_count`, so even the counting invariant could not detect the loss.
# ---------------------------------------------------------------------------


def _patch_unreadable_path(monkeypatch, target_path: str, exc_factory=None) -> None:
    """Simulate one path whose metadata cannot be read, for every probe API.

    A locked, offline or permission-denied file fails *every* metadata probe, so all of them are
    patched for exactly that path and delegated for everything else.

    Patching ``os.stat`` alone is **not** sufficient, and that matters: since Python 3.13 on Windows
    ``os.path.isfile`` is ``nt._path_isfile``, a C builtin that never calls ``os.stat``. A stat-only
    patch therefore leaves the ``isfile`` probe reporting success, and the defect under test does not
    reproduce at all.

    No ACL or file-mode changes, so behaviour is identical on Windows and POSIX at any privilege level.
    """
    real_stat = os.stat
    real_isfile = os.path.isfile
    real_isdir = os.path.isdir

    if exc_factory is None:

        def exc_factory():
            return PermissionError(13, "Access is denied", target_path)

    def is_target(path) -> bool:
        return isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == target_path

    def failing_stat(path, *args, **kwargs):
        if is_target(path):
            raise exc_factory()
        return real_stat(path, *args, **kwargs)

    def failing_isfile(path, *args, **kwargs):
        # What os.path.isfile() does when its probe errors: swallow and report False.
        if is_target(path):
            return False
        return real_isfile(path, *args, **kwargs)

    def failing_isdir(path, *args, **kwargs):
        if is_target(path):
            return False
        return real_isdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)
    monkeypatch.setattr(os.path, "isfile", failing_isfile)
    monkeypatch.setattr(os.path, "isdir", failing_isdir)


def _nonrecursive_tree(tmp_path) -> tuple[str, str, str, str]:
    """``good1.mp4``, ``good2.mp4`` and ``locked.mp4`` all directly in the scan root."""
    root = str(tmp_path / "library")
    good1 = write_file(os.path.join(root, "good1.mp4"), b"one")
    good2 = write_file(os.path.join(root, "good2.mp4"), b"two")
    locked = write_file(os.path.join(root, "locked.mp4"), b"three")
    return root, good1, good2, locked


def test_nonrecursive_tree_would_otherwise_report_three(tmp_path):
    """Baseline, so the tests below cannot pass because the tree was small to begin with."""
    root, good1, good2, locked = _nonrecursive_tree(tmp_path)

    result = scan_folder(root, recursive=False)

    assert set(result.paths) == {good1, good2, locked}
    assert result.discovered_count == 3


def test_nonrecursive_unreadable_file_is_explicitly_rejected(tmp_path, monkeypatch):
    """A. The locked entry must be represented as an UNREADABLE rejection, not dropped."""
    root, good1, good2, locked = _nonrecursive_tree(tmp_path)
    _patch_unreadable_path(monkeypatch, locked)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert set(result.paths) == {good1, good2}
    assert [item.path for item in result.rejected] == [locked]
    assert result.rejected[0].reason is RejectReason.UNREADABLE
    assert "Access is denied" in result.rejected[0].detail


def test_nonrecursive_unreadable_file_cannot_vanish_from_the_accounting(tmp_path, monkeypatch):
    """B. The entry stays inside discovered == ready + rejected + path_collisions."""
    root, _good1, _good2, locked = _nonrecursive_tree(tmp_path)
    _patch_unreadable_path(monkeypatch, locked)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert result.discovered_count == 3, "the locked entry silently disappeared from discovered"
    assert result.ready_count == 2
    assert result.rejected_count == 1
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )
    assert result.supported_count == 3, "it has a supported extension and must still count as one"


def test_nonrecursive_vanished_file_is_also_accounted_for(tmp_path, monkeypatch):
    """Not only permissions: a file removed between listing and stat must not vanish silently."""
    root, _good1, _good2, locked = _nonrecursive_tree(tmp_path)
    _patch_unreadable_path(
        monkeypatch,
        locked,
        lambda: FileNotFoundError(2, "The system cannot find the file specified", locked),
    )

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert result.discovered_count == 3
    assert result.rejected[0].path == locked
    assert result.rejected[0].reason is RejectReason.UNREADABLE


def test_nonrecursive_directories_are_still_excluded(tmp_path, monkeypatch):
    """C. An ordinary directory is not a source video just because it exists."""
    root, good1, good2, locked = _nonrecursive_tree(tmp_path)
    os.makedirs(os.path.join(root, "subdir"), exist_ok=True)
    os.makedirs(os.path.join(root, "another.mp4"), exist_ok=True)  # directory named like a video
    _patch_unreadable_path(monkeypatch, locked)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert set(result.paths) == {good1, good2}
    assert result.discovered_count == 3
    assert [item.path for item in result.rejected] == [locked]


def test_nonrecursive_supported_files_still_work(tmp_path):
    """D. No regression for the ordinary case."""
    root = str(tmp_path / "library")
    expected = {
        write_file(os.path.join(root, "a.mp4"), b"a"),
        write_file(os.path.join(root, "b.MKV"), b"bb"),
    }

    result = scan_folder(root, recursive=False)

    assert set(result.paths) == expected
    assert result.is_ready() is True
    assert result.total_ready_bytes == 3


def test_nonrecursive_unsupported_files_keep_their_reason(tmp_path, monkeypatch):
    """E. Extension classification is unchanged, including for an unreadable unsupported file."""
    root = str(tmp_path / "library")
    good = write_file(os.path.join(root, "a.mp4"), b"a")
    write_file(os.path.join(root, "notes.txt"), b"t")
    locked_txt = write_file(os.path.join(root, "locked.txt"), b"t")
    _patch_unreadable_path(monkeypatch, locked_txt)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    assert result.paths == (good,)
    reasons = result.rejected_by_reason()
    # Extension is checked before stat, so an unreadable .txt is still an extension rejection.
    assert reasons[RejectReason.UNSUPPORTED_EXTENSION] == 2
    assert reasons[RejectReason.UNREADABLE] == 0
    assert result.discovered_count == 3


def test_nonrecursive_entry_with_failing_is_dir_probe_is_not_dropped(tmp_path, monkeypatch):
    """The defensive branch: if the directory-or-not probe itself fails, still account for the entry.

    ``DirEntry.is_dir()`` is a C-level call that does not route through ``os.stat``, so it cannot be
    made to fail by patching ``os.stat``. A fake scandir result is used instead, which is the only
    deterministic way to exercise this branch without privileged filesystem manipulation.
    """
    root = str(tmp_path / "library")
    target = write_file(os.path.join(root, "probe_fails.mp4"), b"payload")

    class FakeEntry:
        def __init__(self, path):
            self.name = os.path.basename(path)
            self.path = path

        def is_dir(self, *args, **kwargs):
            raise PermissionError(13, "Access is denied", self.path)

    class FakeScandir:
        def __init__(self, entries):
            self._entries = entries

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def __iter__(self):
            return iter(self._entries)

    real_scandir = os.scandir

    def fake_scandir(path=".", *args, **kwargs):
        if os.path.abspath(os.fspath(path)) == os.path.abspath(root):
            return FakeScandir([FakeEntry(target)])
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", fake_scandir)

    result = scan_folder(root, recursive=False, detect_duplicates=False)

    # The entry survived the probe failure and was classified normally (it is a real readable file).
    assert result.discovered_count == 1
    assert result.paths == (target,)


def test_recursive_behaviour_is_unchanged_by_the_nonrecursive_fix(tmp_path, monkeypatch):
    """G. Recursive mode still reports an unreadable nested file as a rejection, not a failure."""
    root = str(tmp_path / "library")
    good = write_file(os.path.join(root, "top.mp4"), b"top")
    locked = write_file(os.path.join(root, "nested", "locked.mp4"), b"locked")
    _patch_unreadable_path(monkeypatch, locked)

    result = scan_folder(root, recursive=True, detect_duplicates=False)

    assert result.paths == (good,)
    assert result.discovered_count == 2
    assert result.rejected[0].path == locked
    assert result.rejected[0].reason is RejectReason.UNREADABLE
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )
