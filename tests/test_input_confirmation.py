"""Source-set identity: a confirmation must represent the actual set, not just its size.

The whole point of these tests is the failure the feature exists to prevent: a user confirms N files,
the set changes underneath them, and a render proceeds anyway because "the count still matches".
"""

from __future__ import annotations

import os

import pytest
from conftest import write_file

from beatsync_fork.input_confirmation import (
    ConfirmationError,
    SourceMode,
    SourceSnapshot,
    SourceSnapshotError,
    confirm_snapshot,
    describe_change,
    entry_for_path,
    snapshot_from_media_files,
    snapshot_from_paths,
)
from beatsync_fork.input_manager import scan_folder


def _folder(tmp_path, name="lib", count=3):
    root = str(tmp_path / name)
    paths = [write_file(os.path.join(root, f"clip_{i}.mp4"), b"x" * (10 + i)) for i in range(count)]
    return root, paths


def _folder_snapshot(root: str, recursive: bool = True):
    result = scan_folder(root, recursive=recursive, detect_duplicates=False)
    return snapshot_from_media_files(result.files, scan_root=result.root, recursive=result.recursive)


# --- 1. identical set -> same digest ----------------------------------------


def test_identical_ordered_set_has_same_digest(tmp_path):
    root, _paths = _folder(tmp_path)
    assert _folder_snapshot(root).digest() == _folder_snapshot(root).digest()


def test_digest_is_stable_across_equivalent_snapshots(tmp_path):
    root, paths = _folder(tmp_path)
    a = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root, recursive=True)
    b = _folder_snapshot(root)
    assert a.digest() == b.digest()


# --- 2/3/4. added / removed / replaced --------------------------------------


def test_added_file_changes_digest(tmp_path):
    root, _paths = _folder(tmp_path)
    before = _folder_snapshot(root)
    write_file(os.path.join(root, "extra.mp4"), b"extra")
    assert _folder_snapshot(root).digest() != before.digest()


def test_removed_file_changes_digest(tmp_path):
    root, paths = _folder(tmp_path)
    before = _folder_snapshot(root)
    os.remove(paths[1])
    assert _folder_snapshot(root).digest() != before.digest()


def test_replaced_file_same_count_changes_digest(tmp_path):
    """The headline case: same count, different content set."""
    root, paths = _folder(tmp_path)
    before = _folder_snapshot(root)
    os.remove(paths[1])
    write_file(os.path.join(root, "different.mp4"), b"y" * 11)
    after = _folder_snapshot(root)

    assert after.count == before.count, "counts must match for this test to be meaningful"
    assert after.digest() != before.digest()


# --- 5/6/7. renamed / size / mtime -----------------------------------------


def test_renamed_path_changes_digest(tmp_path):
    root, paths = _folder(tmp_path)
    before = _folder_snapshot(root)
    os.rename(paths[0], os.path.join(root, "renamed.mp4"))
    after = _folder_snapshot(root)

    assert after.count == before.count
    assert after.digest() != before.digest()


def test_size_change_changes_digest(tmp_path):
    root, paths = _folder(tmp_path)
    before = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root)
    write_file(paths[0], b"z" * 9999)
    after = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root)
    assert after.digest() != before.digest()


def test_mtime_change_changes_digest(tmp_path):
    root, paths = _folder(tmp_path)
    before = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root)
    info = os.stat(paths[0])
    os.utime(paths[0], ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))
    after = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root)

    assert after.entries[0].size == before.entries[0].size, "only mtime should differ"
    assert after.digest() != before.digest()


# --- 8. order ---------------------------------------------------------------


def test_order_change_changes_digest(tmp_path):
    """Ordering is part of the contract: candidate order derives from input order."""
    _root, paths = _folder(tmp_path)
    forward = snapshot_from_paths(paths, SourceMode.BROWSER_FILES)
    reversed_ = snapshot_from_paths(list(reversed(paths)), SourceMode.BROWSER_FILES)

    assert forward.count == reversed_.count
    assert set(forward.paths) == set(reversed_.paths)
    assert forward.digest() != reversed_.digest()


# --- 9/10/11. root / recursive / mode --------------------------------------


def test_scan_root_change_changes_digest(tmp_path):
    _root, paths = _folder(tmp_path)
    a = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=str(tmp_path / "rootA"))
    b = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=str(tmp_path / "rootB"))
    assert a.digest() != b.digest()


def test_recursive_flag_change_changes_digest(tmp_path):
    root, paths = _folder(tmp_path)
    a = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root, recursive=True)
    b = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root=root, recursive=False)
    assert a.digest() != b.digest()


def test_mode_change_changes_digest(tmp_path):
    _root, paths = _folder(tmp_path)
    a = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER)
    b = snapshot_from_paths(paths, SourceMode.BROWSER_FILES)
    assert a.digest() != b.digest()


# --- 12. empty set ----------------------------------------------------------


def test_empty_snapshot_cannot_be_confirmed():
    empty = SourceSnapshot(mode=SourceMode.LOCAL_FOLDER, entries=())
    assert empty.is_empty()
    with pytest.raises(ConfirmationError):
        confirm_snapshot(empty)


def test_missing_snapshot_cannot_be_confirmed():
    with pytest.raises(ConfirmationError):
        confirm_snapshot(None)


def test_non_empty_snapshot_can_be_confirmed(tmp_path):
    root, _paths = _folder(tmp_path)
    snapshot = _folder_snapshot(root)
    assert confirm_snapshot(snapshot) is snapshot


# --- snapshot construction edge cases --------------------------------------


def test_missing_path_is_reported_not_skipped(tmp_path):
    root, paths = _folder(tmp_path)
    ghost = os.path.join(root, "ghost.mp4")
    with pytest.raises(SourceSnapshotError) as caught:
        snapshot_from_paths(paths + [ghost], SourceMode.BROWSER_FILES)
    assert ghost in caught.value.paths


def test_entry_for_path_reports_missing_file(tmp_path):
    with pytest.raises(SourceSnapshotError):
        entry_for_path(str(tmp_path / "nope.mp4"))


def test_snapshot_exposes_paths_count_and_size(tmp_path):
    root, paths = _folder(tmp_path, count=3)
    snapshot = _folder_snapshot(root)
    assert snapshot.count == 3
    assert set(snapshot.paths) == set(paths)
    assert snapshot.total_bytes == sum(os.path.getsize(p) for p in paths)
    assert len(snapshot.digest()) == 64
    assert len(snapshot.short_digest()) == 12


def test_matches_is_digest_equality(tmp_path):
    root, _paths = _folder(tmp_path)
    a, b = _folder_snapshot(root), _folder_snapshot(root)
    assert a.matches(b)
    assert not a.matches(None)


# --- change description ----------------------------------------------------


def test_describe_change_reports_added_removed_modified(tmp_path):
    root, paths = _folder(tmp_path, count=3)
    before = _folder_snapshot(root)

    os.remove(paths[0])
    write_file(os.path.join(root, "new.mp4"), b"new")
    write_file(paths[1], b"changed content")
    after = _folder_snapshot(root)

    delta = describe_change(before, after)
    assert delta.any_change()
    assert len(delta.added) == 1 and os.path.basename(delta.added[0]) == "new.mp4"
    assert len(delta.removed) == 1 and os.path.basename(delta.removed[0]) == "clip_0.mp4"
    assert len(delta.modified) == 1 and os.path.basename(delta.modified[0]) == "clip_1.mp4"
    summary = delta.summary()
    assert "1 added" in summary and "1 removed" in summary and "1 modified" in summary


def test_describe_change_detects_reorder_only(tmp_path):
    _root, paths = _folder(tmp_path)
    forward = snapshot_from_paths(paths, SourceMode.BROWSER_FILES)
    backward = snapshot_from_paths(list(reversed(paths)), SourceMode.BROWSER_FILES)

    delta = describe_change(forward, backward)
    assert delta.reordered is True
    assert delta.added == () and delta.removed == ()
    assert "order changed" in delta.summary()


def test_describe_change_flags_structural_differences(tmp_path):
    _root, paths = _folder(tmp_path)
    a = snapshot_from_paths(paths, SourceMode.LOCAL_FOLDER, scan_root="/rootA", recursive=True)
    b = snapshot_from_paths(paths, SourceMode.BROWSER_FILES, scan_root="/rootB", recursive=False)

    delta = describe_change(a, b)
    assert delta.mode_changed and delta.root_changed and delta.recursive_changed
    summary = delta.summary()
    assert "input mode changed" in summary
    assert "folder changed" in summary
    assert "subfolder setting changed" in summary


def test_describe_change_on_identical_snapshots_reports_nothing(tmp_path):
    root, _paths = _folder(tmp_path)
    delta = describe_change(_folder_snapshot(root), _folder_snapshot(root))
    assert not delta.any_change()
    assert delta.summary() == "no change"
