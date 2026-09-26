"""Large-input regression test.

This is the direct regression guard for the production incident that motivated folder mode: a run in
which only **329 of ~701** selected MP4 files reached Stage 5, silently, and rendered anyway.

The truncation happened in the browser upload layer, not here — but the backend accepted the short
list without complaint, which is what made it invisible. These tests pin the backend half of that
contract: given a folder, the input manager accounts for **every** entry, loses nothing, and reports
exact counts.

No video decoding, no FFmpeg, no Qwen. Fixture trees live in pytest's own ``tmp_path``, which the
runner owns and removes (CLAUDE.md / PCBUS-HK-v1: no repository-local scratch).
"""

from __future__ import annotations

import os

from beatsync_fork.input_manager import RejectReason, order_key, scan_folder
from beatsync_fork.input_report import InputReport

EXPECTED_UNIQUE_SUPPORTED = 1000
EXPECTED_DUPLICATE_SUPPORTED = 5  # one group of 3 + one group of 2
EXPECTED_SUPPORTED = EXPECTED_UNIQUE_SUPPORTED + EXPECTED_DUPLICATE_SUPPORTED  # 1005
EXPECTED_UNSUPPORTED = 37
EXPECTED_DISCOVERED = EXPECTED_SUPPORTED + EXPECTED_UNSUPPORTED  # 1042


def test_fixture_tree_is_the_expected_size(large_tree):
    """Guard the guard: if the fixture drifts, every assertion below becomes meaningless."""
    assert len(large_tree.supported) == EXPECTED_SUPPORTED
    assert len(large_tree.unsupported) == EXPECTED_UNSUPPORTED
    assert large_tree.expected_discovered == EXPECTED_DISCOVERED


def test_no_truncation_of_a_thousand_file_library(large_tree):
    """Every supported file is discovered. Not 20, not 329 — all of them."""
    result = scan_folder(large_tree.root, recursive=True)

    assert result.discovered_count == EXPECTED_DISCOVERED
    assert result.supported_count == EXPECTED_SUPPORTED
    assert result.ready_count == EXPECTED_SUPPORTED
    assert result.rejected_count == EXPECTED_UNSUPPORTED

    # The strongest form of the assertion: exact set equality against what was written to disk.
    assert set(result.paths) == large_tree.supported
    assert not (large_tree.supported - set(result.paths)), "files were silently dropped"
    assert not (set(result.paths) - large_tree.supported), "files were invented"


def test_counting_invariant_holds_at_scale(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    assert result.discovered_count == (
        result.ready_count + result.rejected_count + result.path_collisions
    )
    assert result.path_collisions == 0


def test_rejected_extension_count_is_exact(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    reasons = result.rejected_by_reason()

    assert reasons[RejectReason.UNSUPPORTED_EXTENSION] == EXPECTED_UNSUPPORTED
    assert reasons[RejectReason.EMPTY_FILE] == 0
    assert reasons[RejectReason.UNREADABLE] == 0
    assert reasons[RejectReason.NOT_A_FILE] == 0
    assert set(result.paths).isdisjoint(large_tree.unsupported)


def test_no_path_is_multiplied(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    assert len(result.paths) == len(set(result.paths)) == EXPECTED_SUPPORTED


def test_ordering_is_deterministic_and_stable_at_scale(large_tree):
    first = scan_folder(large_tree.root, recursive=True)
    second = scan_folder(large_tree.root, recursive=True)

    assert first.paths == second.paths
    assert first.paths == tuple(sorted(first.paths, key=order_key))
    # A total order: no two entries share a sort key, so ties can never fall back to walk order.
    assert len({order_key(p) for p in first.paths}) == EXPECTED_SUPPORTED


def test_duplicate_reporting_is_exact_at_scale(large_tree):
    result = scan_folder(large_tree.root, recursive=True)

    assert len(result.duplicate_groups) == len(large_tree.duplicate_groups) == 2
    assert result.duplicate_extra_files == large_tree.expected_duplicate_extra == 3
    assert result.fingerprint_errors == ()

    reported = {frozenset(group.paths) for group in result.duplicate_groups}
    expected = {frozenset(group) for group in large_tree.duplicate_groups}
    assert reported == expected

    # Duplicates are reported, never removed.
    for group in large_tree.duplicate_groups:
        for path in group:
            assert path in result.paths


def test_group_sizes_are_three_and_two(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    assert sorted(len(group.paths) for group in result.duplicate_groups) == [2, 3]


def test_recursion_disabled_sees_only_the_top_level(large_tree):
    """The same tree, non-recursive: nested files are correctly absent, not silently lost."""
    result = scan_folder(large_tree.root, recursive=False)

    assert set(result.paths) == large_tree.top_level_supported
    assert result.ready_count == 2
    assert result.discovered_count == 2

    # And recursion re-enabled on the same tree finds everything again.
    assert scan_folder(large_tree.root, recursive=True).ready_count == EXPECTED_SUPPORTED


def test_total_size_matches_the_filesystem(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    expected_bytes = sum(os.path.getsize(path) for path in large_tree.supported)
    assert result.total_ready_bytes == expected_bytes


def test_report_agrees_with_the_scan(large_tree):
    result = scan_folder(large_tree.root, recursive=True)
    report = InputReport.from_input_set(result)

    assert report.discovered == EXPECTED_DISCOVERED
    assert report.supported == EXPECTED_SUPPORTED
    assert report.ready == EXPECTED_SUPPORTED
    assert report.rejected == EXPECTED_UNSUPPORTED
    assert report.duplicate_groups == 2
    assert report.duplicate_extra_files == 3
    assert report.is_ready is True

    rendered = report.render_text()
    assert "Discovered:  1042" in rendered
    assert "Ready:       1005" in rendered
    assert "INPUT READY" in rendered
