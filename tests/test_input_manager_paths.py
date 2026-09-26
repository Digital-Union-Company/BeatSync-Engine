"""Path, recursion and failure-mode handling.

Windows is the target platform, so these cover the path shapes a real media library actually has.
Anything that depends on a filesystem feature that may be unavailable is skipped rather than failed.
"""

from __future__ import annotations

import os

import pytest
from conftest import write_file

from beatsync_fork.input_manager import InputScanError, RejectReason, scan_folder


def test_spaces_in_folder_and_file_names(tmp_path):
    root = str(tmp_path / "My Source Videos 2026")
    expected = {
        write_file(os.path.join(root, "clip one.mp4"), b"x"),
        write_file(os.path.join(root, "Season 1 - Episode 2", "final cut .mkv"), b"xx"),
    }

    result = scan_folder(root)

    assert set(result.paths) == expected
    assert result.ready_count == 2


def test_unicode_filenames(tmp_path):
    root = str(tmp_path / "unicode")
    names = ["日本語クリップ.mp4", "café_montage.mkv", "Ελληνικά.mp4", "видео.mkv"]

    created = set()
    for name in names:
        try:
            created.add(write_file(os.path.join(root, name), b"x"))
        except (OSError, UnicodeError) as exc:  # pragma: no cover - filesystem dependent
            pytest.skip(f"filesystem rejected {name!r}: {exc}")

    result = scan_folder(root)

    assert set(result.paths) == created
    assert result.ready_count == len(names)


def test_mixed_extension_case_is_accepted(tmp_path):
    root = str(tmp_path / "case")
    expected = set()
    for name in ["a.MP4", "b.Mp4", "c.mKv", "d.MKV", "e.mp4", "f.mkv"]:
        expected.add(write_file(os.path.join(root, name), b"x"))

    result = scan_folder(root)

    assert set(result.paths) == expected
    assert result.ready_count == 6
    assert result.rejected_count == 0
    # The recorded extension is normalised even though the filename keeps its original case.
    assert {item.extension for item in result.files} == {".mp4", ".mkv"}


def test_recursive_finds_nested_files(tmp_path):
    root = str(tmp_path / "src")
    top = write_file(os.path.join(root, "top.mp4"), b"x")
    deep = write_file(os.path.join(root, "a", "b", "c", "deep.mkv"), b"x")

    result = scan_folder(root, recursive=True)

    assert set(result.paths) == {top, deep}


def test_non_recursive_ignores_subdirectories(tmp_path):
    root = str(tmp_path / "src")
    top = write_file(os.path.join(root, "top.mp4"), b"x")
    write_file(os.path.join(root, "a", "b", "deep.mkv"), b"x")

    result = scan_folder(root, recursive=False)

    assert result.paths == (top,)
    assert result.discovered_count == 1
    assert result.recursive is False


def test_non_recursive_does_not_count_directories_as_entries(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "top.mp4"), b"x")
    os.makedirs(os.path.join(root, "subdir"), exist_ok=True)

    result = scan_folder(root, recursive=False)

    assert result.discovered_count == 1
    assert result.rejected_count == 0


def test_missing_folder_raises(tmp_path):
    with pytest.raises(InputScanError, match="does not exist"):
        scan_folder(str(tmp_path / "nope"))


def test_file_passed_as_root_raises(tmp_path):
    path = write_file(str(tmp_path / "a.mp4"), b"x")
    with pytest.raises(InputScanError, match="Not a folder"):
        scan_folder(path)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_root_raises(value):
    with pytest.raises(InputScanError, match="No folder"):
        scan_folder(value)


def test_empty_folder_is_not_an_error(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root, exist_ok=True)

    result = scan_folder(root)

    assert result.ready_count == 0
    assert result.discovered_count == 0
    assert result.rejected_count == 0
    assert result.is_ready() is False


def test_folder_with_only_unsupported_files_is_not_ready(tmp_path):
    root = str(tmp_path / "docs")
    write_file(os.path.join(root, "readme.txt"), b"x")
    write_file(os.path.join(root, "cover.png"), b"x")

    result = scan_folder(root)

    assert result.is_ready() is False
    assert result.discovered_count == 2
    assert result.rejected_by_reason()[RejectReason.UNSUPPORTED_EXTENSION] == 2


def test_no_extension_is_rejected(tmp_path):
    root = str(tmp_path / "src")
    write_file(os.path.join(root, "README"), b"x")

    result = scan_folder(root)

    assert result.ready_count == 0
    assert result.rejected[0].reason is RejectReason.UNSUPPORTED_EXTENSION
    assert result.rejected[0].detail == "(none)"


def test_trailing_separator_and_relative_root_resolve(tmp_path, monkeypatch):
    root = tmp_path / "src"
    expected = write_file(str(root / "a.mp4"), b"x")

    monkeypatch.chdir(tmp_path)
    absolute = scan_folder(str(root) + os.sep)
    relative = scan_folder("src")

    assert absolute.paths == (expected,)
    assert relative.paths == (expected,)
    assert absolute.root == relative.root


def test_symlinked_directory_is_not_traversed(tmp_path):
    """Directory links are not followed, so a self-referential tree cannot inflate the count."""
    root = tmp_path / "src"
    real = write_file(str(root / "real" / "clip.mp4"), b"x")
    try:
        os.symlink(str(root / "real"), str(root / "alias"), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover - platform
        pytest.skip(f"directory symlinks unavailable: {exc}")

    result = scan_folder(str(root))

    assert result.paths == (real,)
    assert result.discovered_count == 1


def test_file_reachable_twice_is_counted_once(tmp_path):
    """Two directory entries resolving to one file: one ready entry, one recorded collision."""
    root = tmp_path / "src"
    real = write_file(str(root / "clip.mp4"), b"payload")
    try:
        os.symlink(str(root / "clip.mp4"), str(root / "alias.mp4"))
    except (OSError, NotImplementedError, AttributeError) as exc:  # pragma: no cover - platform
        pytest.skip(f"file symlinks unavailable: {exc}")

    result = scan_folder(str(root), recursive=False)

    assert result.discovered_count == 2
    assert result.ready_count == 1
    assert result.path_collisions == 1
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )

    kept = result.paths[0]
    assert os.path.realpath(kept) == os.path.realpath(real)
    # The first entry in deterministic walk order wins, so the choice is reproducible.
    assert os.path.basename(kept) == "alias.mp4"
