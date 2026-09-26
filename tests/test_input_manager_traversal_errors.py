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

from beatsync_fork.input_manager import InputScanError, scan_folder

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

    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == bad:
            raise PermissionError(13, "Access is denied", bad)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)

    result = scan_folder(root, recursive=True, detect_duplicates=False)

    assert result.paths == (good,)
    assert result.rejected_count == 1
    assert result.rejected[0].path == bad
    assert result.rejected[0].reason.value == "unreadable"
