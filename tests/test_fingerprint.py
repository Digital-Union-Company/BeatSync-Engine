"""Fingerprint unit tests: the duplicate-candidate primitive, tested in isolation."""

from __future__ import annotations

import os

import pytest
from conftest import write_file

from beatsync_fork.input_manager import (
    FINGERPRINT_STRATEGY_VERSION,
    MediaFile,
    find_duplicate_groups,
    fingerprint_file,
)


def _media(path: str) -> MediaFile:
    info = os.stat(path)
    return MediaFile(
        path=path,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        extension=os.path.splitext(path)[1].casefold(),
    )


def test_identical_content_fingerprints_equal(tmp_path):
    a = write_file(str(tmp_path / "a.mp4"), b"same bytes")
    b = write_file(str(tmp_path / "b.mp4"), b"same bytes")
    assert fingerprint_file(a).digest == fingerprint_file(b).digest


def test_different_content_same_size_differs(tmp_path):
    a = write_file(str(tmp_path / "a.mp4"), b"aaaa")
    b = write_file(str(tmp_path / "b.mp4"), b"bbbb")
    assert fingerprint_file(a).digest != fingerprint_file(b).digest


def test_size_is_mixed_into_the_digest(tmp_path):
    """Head and tail alone must not decide equality: length is part of the identity."""
    small = write_file(str(tmp_path / "small.mp4"), b"xy")
    large = write_file(str(tmp_path / "large.mp4"), b"xyxy")
    assert fingerprint_file(small).digest != fingerprint_file(large).digest


def test_small_files_are_hashed_whole(tmp_path):
    path = write_file(str(tmp_path / "small.mp4"), b"z" * 64)
    result = fingerprint_file(path, chunk_bytes=1024)
    assert result.strategy == "whole"
    assert result.size == 64


def test_large_files_use_head_and_tail(tmp_path):
    path = write_file(str(tmp_path / "big.mp4"), b"h" * 100 + b"m" * 500 + b"t" * 100)
    result = fingerprint_file(path, chunk_bytes=64)
    assert result.strategy == "head_tail"
    assert result.size == 700


def test_head_tail_ignores_the_middle(tmp_path):
    """Documents the deliberate trade-off: only head+tail+size are read, so two files differing
    solely in their middle bytes are reported as duplicate *candidates*, not confirmed identical."""
    a = write_file(str(tmp_path / "a.mp4"), b"h" * 64 + b"AAAA" + b"t" * 64)
    b = write_file(str(tmp_path / "b.mp4"), b"h" * 64 + b"BBBB" + b"t" * 64)
    assert fingerprint_file(a, chunk_bytes=64).digest == fingerprint_file(b, chunk_bytes=64).digest


def test_head_tail_boundary_does_not_double_count(tmp_path):
    """At exactly 2 * chunk_bytes the whole-file path is used, so no byte is hashed twice."""
    path = write_file(str(tmp_path / "boundary.mp4"), b"q" * 128)
    assert fingerprint_file(path, chunk_bytes=64).strategy == "whole"
    assert fingerprint_file(path, chunk_bytes=63).strategy == "head_tail"


def test_empty_file_is_fingerprintable(tmp_path):
    path = write_file(str(tmp_path / "empty.mp4"), b"")
    result = fingerprint_file(path)
    assert result.size == 0
    assert result.strategy == "whole"
    assert len(result.digest) == 64


def test_all_empty_files_share_a_digest(tmp_path):
    a = write_file(str(tmp_path / "a.mp4"), b"")
    b = write_file(str(tmp_path / "b.mp4"), b"")
    assert fingerprint_file(a).digest == fingerprint_file(b).digest


def test_strategy_version_changes_the_digest(tmp_path):
    """The version namespace is actually mixed in, so digests are never compared across strategies."""
    path = write_file(str(tmp_path / "a.mp4"), b"payload")
    import hashlib

    unnamespaced = hashlib.sha256(b"payload").hexdigest()
    assert fingerprint_file(path).digest != unnamespaced
    assert FINGERPRINT_STRATEGY_VERSION == "beatsync-fp-v1"


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        fingerprint_file(str(tmp_path / "nope.mp4"))


def test_invalid_chunk_size_rejected(tmp_path):
    path = write_file(str(tmp_path / "a.mp4"), b"x")
    with pytest.raises(ValueError):
        fingerprint_file(path, chunk_bytes=0)


# ---------------------------------------------------------------------------
# find_duplicate_groups
# ---------------------------------------------------------------------------


def test_duplicate_groups_found_and_sorted(tmp_path):
    same = b"identical payload"
    paths = [
        write_file(str(tmp_path / "z.mp4"), same),
        write_file(str(tmp_path / "a.mp4"), same),
        write_file(str(tmp_path / "m.mp4"), same),
    ]
    unique = write_file(str(tmp_path / "unique.mp4"), b"different payload!")

    groups, errors = find_duplicate_groups([_media(p) for p in paths + [unique]])

    assert errors == ()
    assert len(groups) == 1
    assert groups[0].paths == tuple(sorted(paths, key=lambda p: os.path.basename(p)))
    assert groups[0].extra_copies == 2


def test_unique_sizes_are_never_opened(tmp_path, monkeypatch):
    """Size is a free pre-filter: a file with a unique size must not be read at all."""
    files = [
        _media(write_file(str(tmp_path / f"f{i}.mp4"), b"x" * (10 + i)))
        for i in range(5)
    ]

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fingerprint_file was called for a unique-sized file")

    monkeypatch.setattr("beatsync_fork.input_manager.fingerprint_file", explode)
    groups, errors = find_duplicate_groups(files)
    assert groups == ()
    assert errors == ()


def test_same_size_different_content_is_not_a_duplicate(tmp_path):
    files = [
        _media(write_file(str(tmp_path / "a.mp4"), b"aaaa")),
        _media(write_file(str(tmp_path / "b.mp4"), b"bbbb")),
    ]
    groups, errors = find_duplicate_groups(files)
    assert groups == ()
    assert errors == ()


def test_fingerprint_failure_is_reported_not_treated_as_equal(tmp_path, monkeypatch):
    same = b"payload!!"
    good = _media(write_file(str(tmp_path / "good.mp4"), same))
    bad = _media(write_file(str(tmp_path / "bad.mp4"), same))

    real = fingerprint_file

    def selective(path, size=None, chunk_bytes=None, **kwargs):
        if os.path.basename(path) == "bad.mp4":
            raise PermissionError("locked by another process")
        if chunk_bytes is None:
            return real(path, size=size)
        return real(path, size=size, chunk_bytes=chunk_bytes)

    monkeypatch.setattr("beatsync_fork.input_manager.fingerprint_file", selective)
    groups, errors = find_duplicate_groups([good, bad])

    # One readable file left alone is not a group, and the failure is surfaced.
    assert groups == ()
    assert len(errors) == 1
    assert errors[0].path == bad.path
    assert "locked by another process" in errors[0].error


def test_empty_file_list_is_handled():
    assert find_duplicate_groups([]) == ((), ())
