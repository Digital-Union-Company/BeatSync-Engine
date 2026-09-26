"""Shared fixtures for the fork test suite.

All tests here run on a bare CPython with pytest and nothing else: the modules under test import only
the standard library. Fixture trees are built inside pytest's own ``tmp_path``, which the runner owns
and cleans up, so no repository-local scratch is created (see CLAUDE.md, PCBUS-HK-v1).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

import pytest

# pytest.ini sets `pythonpath = src`; this keeps the suite runnable when invoked in ways that bypass
# that (e.g. `python -m pytest tests/test_x.py` from a different rootdir).
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def write_file(path: str, content: bytes) -> str:
    """Create a file (and its parents) with exact byte content. Returns the absolute path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)
    return os.path.abspath(path)


@dataclass
class FixtureTree:
    """A generated source tree plus the exact truth about what was written.

    Tests assert against these recorded sets rather than against re-derived guesses, so a scanner bug
    cannot accidentally agree with a buggy expectation.
    """

    root: str
    supported: set[str] = field(default_factory=set)
    unsupported: set[str] = field(default_factory=set)
    duplicate_groups: list[list[str]] = field(default_factory=list)
    top_level_supported: set[str] = field(default_factory=set)
    """Supported files written directly in ``root``, so non-recursive scans have a known answer."""

    @property
    def expected_discovered(self) -> int:
        return len(self.supported) + len(self.unsupported)

    @property
    def expected_duplicate_extra(self) -> int:
        return sum(len(group) - 1 for group in self.duplicate_groups)


@pytest.fixture
def large_tree(tmp_path) -> FixtureTree:
    """A tree with 1000 unique supported files, unsupported files, nesting and duplicates.

    Shape:

    * 1000 unique-content supported files (``.mp4``/``.mkv``, mixed extension case), each with a
      unique size so the duplicate pre-filter has no false hits. Two of them sit directly in ``root``
      and the other 998 are spread over 20 nested subdirectories, so a non-recursive scan has an
      exact expected answer.
    * 37 unsupported files interleaved through the same directories.
    * 2 duplicate groups: 3 byte-identical files and 2 byte-identical files, in *different*
      directories, with sizes outside the unique-file size range.

    Totals: 1005 supported (2 at top level), 37 unsupported, 1042 discovered.
    """
    root = str(tmp_path / "library")
    tree = FixtureTree(root=root)

    for index in range(1000):
        # Alternate extension and case to prove case-insensitive matching at scale.
        extension = [".mp4", ".mkv", ".MP4", ".mKv"][index % 4]
        name = f"clip_{index:04d}{extension}"
        if index < 2:
            path = os.path.join(root, name)
        else:
            bucket = index % 20
            path = os.path.join(root, f"set_{bucket:02d}", f"part_{index % 7}", name)
        # Unique size per file (16..1015 bytes) => every size is unique => no fingerprint reads.
        created = write_file(path, b"v" * (16 + index))
        tree.supported.add(created)
        if index < 2:
            tree.top_level_supported.add(created)

    unsupported_extensions = [".txt", ".jpg", ".srt", ".nfo", ".mov", ".json", ".db"]
    for index in range(37):
        bucket = index % 20
        path = os.path.join(root, f"set_{bucket:02d}", f"note_{index:02d}{unsupported_extensions[index % 7]}")
        tree.unsupported.add(write_file(path, b"not a source video"))

    group_a_content = b"A" * 5000
    group_a = [
        write_file(os.path.join(root, "set_00", "dupe_a1.mp4"), group_a_content),
        write_file(os.path.join(root, "set_05", "nested", "dupe_a2.mp4"), group_a_content),
        write_file(os.path.join(root, "set_11", "dupe_a3.MP4"), group_a_content),
    ]
    group_b_content = b"B" * 6000
    group_b = [
        write_file(os.path.join(root, "set_03", "dupe_b1.mkv"), group_b_content),
        write_file(os.path.join(root, "set_19", "deep", "deeper", "dupe_b2.mkv"), group_b_content),
    ]
    for group in (group_a, group_b):
        tree.supported.update(group)
        tree.duplicate_groups.append(group)

    return tree
