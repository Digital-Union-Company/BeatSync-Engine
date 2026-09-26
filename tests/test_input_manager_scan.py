"""Core scanning behaviour: counting, classification, ordering, invariants."""

from __future__ import annotations

import os

import pytest
from conftest import write_file

from beatsync_fork.input_manager import (
    SUPPORTED_VIDEO_EXTENSIONS,
    InputScanError,
    RejectReason,
    order_key,
    scan_folder,
)


def test_supported_extensions_match_upstream_pipeline():
    """Folder mode must not accept anything the existing pipeline would reject.

    Upstream accepts exactly these two: `gr.File(file_types=['.mp4', '.mkv'])` in gui.py and
    `get_video_files()` in video_processor.py.
    """
    assert SUPPORTED_VIDEO_EXTENSIONS == frozenset({".mp4", ".mkv"})


def test_basic_classification(tmp_path):
    root = str(tmp_path / "src")
    keep = {
        write_file(os.path.join(root, "a.mp4"), b"aaa"),
        write_file(os.path.join(root, "b.mkv"), b"bbbb"),
    }
    write_file(os.path.join(root, "notes.txt"), b"text")
    write_file(os.path.join(root, "thumb.jpg"), b"jpeg")

    result = scan_folder(root)

    assert set(result.paths) == keep
    assert result.discovered_count == 4
    assert result.supported_count == 2
    assert result.ready_count == 2
    assert result.rejected_count == 2
    assert result.rejected_by_reason()[RejectReason.UNSUPPORTED_EXTENSION] == 2
    assert result.total_ready_bytes == 7
    assert result.is_ready() is True


def test_counting_invariant_always_holds(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "good.mp4"), b"x")
    write_file(os.path.join(root, "empty.mp4"), b"")
    write_file(os.path.join(root, "skip.txt"), b"x")
    write_file(os.path.join(root, "nested", "deep.mkv"), b"xx")

    result = scan_folder(root)

    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )


def test_empty_files_are_rejected_not_ready(tmp_path):
    """A zero-byte .mp4 is not a usable source; it must be reported, not silently rendered."""
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "real.mp4"), b"data")
    write_file(os.path.join(root, "zero.mp4"), b"")

    result = scan_folder(root)

    assert result.ready_count == 1
    assert result.supported_count == 2
    assert result.rejected_by_reason()[RejectReason.EMPTY_FILE] == 1
    assert os.path.basename(result.paths[0]) == "real.mp4"


def test_unsupported_extension_wins_over_emptiness(tmp_path):
    """Extension is checked first so the per-reason counts are unambiguous."""
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "empty.txt"), b"")

    result = scan_folder(root)

    reasons = result.rejected_by_reason()
    assert reasons[RejectReason.UNSUPPORTED_EXTENSION] == 1
    assert reasons[RejectReason.EMPTY_FILE] == 0


def test_deterministic_order_is_case_insensitive(tmp_path):
    root = str(tmp_path / "src")
    for name in ["Zebra.mp4", "apple.mp4", "Mango.mkv", "banana.MP4"]:
        write_file(os.path.join(root, name), b"x")

    result = scan_folder(root)
    names = [os.path.basename(p) for p in result.paths]

    assert names == ["apple.mp4", "banana.MP4", "Mango.mkv", "Zebra.mp4"]


def test_repeated_scans_are_byte_identical(tmp_path):
    root = str(tmp_path / "src")
    for index in range(40):
        write_file(os.path.join(root, f"dir_{index % 5}", f"CLIP_{index}.mp4"), b"x" * (index + 1))

    first = scan_folder(root)
    second = scan_folder(root)

    assert first.paths == second.paths
    assert first.paths == tuple(sorted(first.paths, key=order_key))


def test_paths_are_absolute_and_unique(tmp_path):
    root = str(tmp_path / "src")
    for index in range(10):
        write_file(os.path.join(root, f"c{index}.mp4"), b"x")

    result = scan_folder(root)

    assert all(os.path.isabs(p) for p in result.paths)
    assert len(set(result.paths)) == len(result.paths)


def test_no_media_is_copied_or_modified(tmp_path):
    """Scanning must be strictly read-only: same tree, same bytes, same mtimes, no new files."""
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mp4"), b"payload")
    write_file(os.path.join(root, "nested", "b.mkv"), b"payload")

    def snapshot() -> set[tuple[str, int, int]]:
        found = set()
        for dirpath, _dirnames, filenames in os.walk(tmp_path):
            for name in filenames:
                full = os.path.join(dirpath, name)
                info = os.stat(full)
                found.add((full, info.st_size, info.st_mtime_ns))
        return found

    before = snapshot()
    scan_folder(root)
    assert snapshot() == before


def test_duplicates_are_reported_and_kept(tmp_path):
    """Duplicate files stay in the ready set — reporting is not removal."""
    root = str(tmp_path / "src")
    same = b"identical!"
    a = write_file(os.path.join(root, "one.mp4"), same)
    b = write_file(os.path.join(root, "nested", "two.mp4"), same)
    write_file(os.path.join(root, "other.mp4"), b"different")

    result = scan_folder(root)

    assert result.ready_count == 3
    assert len(result.duplicate_groups) == 1
    assert set(result.duplicate_groups[0].paths) == {a, b}
    assert result.duplicate_extra_files == 1
    assert a in result.paths and b in result.paths


def test_duplicate_detection_can_be_skipped(tmp_path):
    root = str(tmp_path / "src")
    same = b"identical!"
    write_file(os.path.join(root, "one.mp4"), same)
    write_file(os.path.join(root, "two.mp4"), same)

    result = scan_folder(root, detect_duplicates=False)

    assert result.ready_count == 2
    assert result.duplicate_groups == ()
    assert result.duplicates_checked is False


def test_custom_extensions_accepted_with_or_without_dot(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "a.mov"), b"x")
    write_file(os.path.join(root, "b.mp4"), b"x")

    result = scan_folder(root, extensions=["mov"])

    assert [os.path.basename(p) for p in result.paths] == ["a.mov"]
    assert result.rejected_by_reason()[RejectReason.UNSUPPORTED_EXTENSION] == 1


def test_empty_extension_set_rejected(tmp_path):
    root = str(tmp_path / "src")
    os.makedirs(root, exist_ok=True)
    with pytest.raises(InputScanError):
        scan_folder(root, extensions=[])


def test_scan_records_extensions_used(tmp_path):
    root = str(tmp_path / "src")
    os.makedirs(root, exist_ok=True)
    result = scan_folder(root)
    assert result.extensions == SUPPORTED_VIDEO_EXTENSIONS
    assert result.root == os.path.abspath(root)
