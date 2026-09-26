"""The render gate, tested without Gradio.

UI disablement is only a courtesy — a stale tab, a queued event or a direct API call can all reach the
render handler. These tests pin the backend check that actually decides, including the case the
feature exists for: a confirmation that no longer matches what is on disk.
"""

from __future__ import annotations

import os

from conftest import write_file

from beatsync_fork.input_confirmation import GateReason, SourceMode, snapshot_from_paths
from beatsync_fork.input_session import (
    confirm_action,
    current_snapshot_for_render,
    initial_state,
    resolve_for_render,
    scan_folder_action,
    set_browser_files,
    set_folder_path,
    set_mode,
    set_recursive,
)


def _library(tmp_path, name="lib", count=3):
    root = str(tmp_path / name)
    paths = [write_file(os.path.join(root, f"clip_{i}.mp4"), b"x" * (10 + i)) for i in range(count)]
    return root, paths


def _confirmed_folder_state(tmp_path, name="lib", count=3):
    root, paths = _library(tmp_path, name, count)
    state = set_folder_path(initial_state(), root)
    state = scan_folder_action(state)
    state = confirm_action(state)
    assert state.is_confirmed()
    return state, root, paths


# ---------------------------------------------------------------------------
# Unconfirmed -> denied
# ---------------------------------------------------------------------------


def test_initial_state_is_not_confirmed_and_render_denied():
    state = initial_state()

    assert state.mode is SourceMode.LOCAL_FOLDER, "local folder is the default mode"
    assert state.is_confirmed() is False
    assert state.can_confirm() is False

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.NOT_CONFIRMED
    assert decision.paths == ()


def test_scanned_but_unconfirmed_is_denied(tmp_path):
    root, _paths = _library(tmp_path)
    state = scan_folder_action(set_folder_path(initial_state(), root))

    assert state.can_confirm() is True
    assert state.is_confirmed() is False
    assert resolve_for_render(state).reason is GateReason.NOT_CONFIRMED


def test_empty_folder_cannot_be_confirmed(tmp_path):
    root = str(tmp_path / "empty")
    os.makedirs(root, exist_ok=True)
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    assert state.is_confirmed() is False
    assert resolve_for_render(state).allowed is False
    assert "CANNOT CONFIRM" in state.report_text


# ---------------------------------------------------------------------------
# Confirmed and matching -> allowed
# ---------------------------------------------------------------------------


def test_confirmed_matching_source_is_allowed(tmp_path):
    state, _root, paths = _confirmed_folder_state(tmp_path)

    decision = resolve_for_render(state)
    assert decision.allowed is True
    assert decision.reason is GateReason.OK
    assert set(decision.paths) == set(paths)
    assert len(decision.paths) == 3


def test_allowed_decision_hands_back_the_exact_ordered_list(tmp_path):
    state, root, _paths = _confirmed_folder_state(tmp_path, count=5)
    decision = resolve_for_render(state)

    assert decision.paths == state.confirmed.paths
    assert all(os.path.isabs(p) for p in decision.paths)
    assert all(p.lower().endswith((".mp4", ".mkv")) for p in decision.paths)


# ---------------------------------------------------------------------------
# Source changed after confirmation -> denied
# ---------------------------------------------------------------------------


def test_added_file_after_confirmation_denies_render(tmp_path):
    state, root, _paths = _confirmed_folder_state(tmp_path)
    write_file(os.path.join(root, "sneaky.mp4"), b"late arrival")

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED
    assert "SOURCE INPUT CHANGED" in decision.message
    assert "1 added" in decision.message


def test_removed_file_after_confirmation_denies_render(tmp_path):
    state, _root, paths = _confirmed_folder_state(tmp_path)
    os.remove(paths[0])

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED
    assert "1 removed" in decision.message


def test_same_count_different_file_denies_render(tmp_path):
    """The exact hole a count-only confirmation would leave open."""
    state, root, paths = _confirmed_folder_state(tmp_path)
    os.remove(paths[1])
    write_file(os.path.join(root, "swapped.mp4"), b"y" * 11)

    fresh = current_snapshot_for_render(state)
    assert fresh.count == state.confirmed.count, "counts match, so only identity can catch this"

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_renamed_file_denies_render(tmp_path):
    state, root, paths = _confirmed_folder_state(tmp_path)
    os.rename(paths[0], os.path.join(root, "renamed.mp4"))

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_modified_file_denies_render(tmp_path):
    state, _root, paths = _confirmed_folder_state(tmp_path)
    write_file(paths[0], b"different bytes entirely")

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert "1 modified" in decision.message


def test_vanished_folder_denies_render(tmp_path):
    state, root, paths = _confirmed_folder_state(tmp_path)
    for path in paths:
        os.remove(path)
    os.rmdir(root)

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_UNAVAILABLE


def test_unreadable_folder_denies_render(tmp_path, monkeypatch):
    """A traversal failure must deny, not silently render from a smaller set."""
    state, root, _paths = _confirmed_folder_state(tmp_path)
    write_file(os.path.join(root, "sub", "nested.mp4"), b"nested")
    state = confirm_action(scan_folder_action(state))
    assert state.is_confirmed()

    real_scandir = os.scandir

    def failing(path=".", *args, **kwargs):
        if os.path.basename(os.path.normpath(os.fspath(path))) == "sub":
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", failing)

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Re-confirming the new set -> allowed again
# ---------------------------------------------------------------------------


def test_reconfirming_the_changed_set_allows_render(tmp_path):
    state, root, _paths = _confirmed_folder_state(tmp_path)
    write_file(os.path.join(root, "extra.mp4"), b"extra")
    assert resolve_for_render(state).allowed is False

    state = confirm_action(scan_folder_action(state))

    decision = resolve_for_render(state)
    assert decision.allowed is True
    assert len(decision.paths) == 4


# ---------------------------------------------------------------------------
# Invalidation rules
# ---------------------------------------------------------------------------


def test_switching_mode_invalidates(tmp_path):
    state, _root, _paths = _confirmed_folder_state(tmp_path)

    switched = set_mode(state, SourceMode.BROWSER_FILES)
    assert switched.is_confirmed() is False
    assert switched.confirmed is None and switched.current is None
    assert resolve_for_render(switched).reason is GateReason.NOT_CONFIRMED

    back = set_mode(switched, SourceMode.LOCAL_FOLDER)
    assert back.is_confirmed() is False


def test_changing_folder_path_invalidates(tmp_path):
    state, _root, _paths = _confirmed_folder_state(tmp_path)
    changed = set_folder_path(state, str(tmp_path / "somewhere-else"))

    assert changed.is_confirmed() is False
    assert changed.confirmed is None
    assert resolve_for_render(changed).allowed is False


def test_toggling_recursive_invalidates(tmp_path):
    state, _root, _paths = _confirmed_folder_state(tmp_path)
    toggled = set_recursive(state, not state.recursive)

    assert toggled.is_confirmed() is False
    assert toggled.recursive != state.recursive
    assert resolve_for_render(toggled).allowed is False


def test_rescanning_invalidates_even_when_the_set_is_identical(tmp_path):
    """Pressing Scan again is an explicit re-declaration; it must be re-accepted."""
    state, _root, _paths = _confirmed_folder_state(tmp_path)
    rescanned = scan_folder_action(state)

    assert rescanned.current is not None
    assert rescanned.confirmed is None
    assert rescanned.is_confirmed() is False
    assert rescanned.current.digest() == state.confirmed.digest(), "same set, still not confirmed"


def test_browser_file_change_invalidates(tmp_path):
    _root, paths = _library(tmp_path, count=3)
    state = set_mode(initial_state(), SourceMode.BROWSER_FILES)
    state = confirm_action(set_browser_files(state, paths))
    assert state.is_confirmed()

    more = _library(tmp_path, name="lib2", count=1)[1]
    updated = set_browser_files(state, paths + more)
    assert updated.is_confirmed() is False
    assert resolve_for_render(updated).reason is GateReason.NOT_CONFIRMED


def test_browser_files_removed_invalidates(tmp_path):
    _root, paths = _library(tmp_path, count=3)
    state = confirm_action(
        set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), paths)
    )
    assert state.is_confirmed()

    cleared = set_browser_files(state, [])
    assert cleared.is_confirmed() is False
    assert cleared.current is None


def test_non_source_settings_do_not_invalidate(tmp_path):
    """FPS/encoder/filename/audio are not source identity and must not clear a confirmation.

    They are separate Gradio inputs that are never routed through these transitions, so the guarantee
    is structural: no transition exists that could clear confirmation for them.
    """
    state, _root, _paths = _confirmed_folder_state(tmp_path)
    before = state.confirmed.digest()

    # The only transitions that exist are source transitions; nothing else can touch the state.
    assert state.is_confirmed()
    assert state.confirmed.digest() == before
    assert resolve_for_render(state).allowed is True


# ---------------------------------------------------------------------------
# Browser mode specifics
# ---------------------------------------------------------------------------


def test_browser_mode_confirmed_then_file_deleted_denies_render(tmp_path):
    _root, paths = _library(tmp_path, count=3)
    state = confirm_action(
        set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), paths)
    )
    assert resolve_for_render(state).allowed is True

    os.remove(paths[0])

    decision = resolve_for_render(state)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_UNAVAILABLE


def test_browser_mode_report_never_claims_selected_or_pending(tmp_path):
    """Honesty check: the UI text must not invent a browser-side selected/pending count.

    The help text may *explain* that a selected count is unknowable; what it must never do is print a
    number for "selected" or "pending", because the backend cannot know either.
    """
    import re

    _root, paths = _library(tmp_path, count=2)
    state = set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), paths)

    assert "Backend ready: 2" in state.report_text, "the honest, backend-authoritative count"

    numeric_claim = re.search(
        r"(selected|pending|uploading|remaining)\D{0,20}\d", state.report_text, re.IGNORECASE
    )
    assert numeric_claim is None, f"fabricated browser-side count: {numeric_claim!r}"


def test_browser_mode_unreadable_file_is_reported(tmp_path):
    _root, paths = _library(tmp_path, count=2)
    state = set_browser_files(
        set_mode(initial_state(), SourceMode.BROWSER_FILES),
        paths + [os.path.join(str(tmp_path), "ghost.mp4")],
    )

    assert state.current is None
    assert "UNREADABLE" in state.report_text
    assert resolve_for_render(state).allowed is False


# ---------------------------------------------------------------------------
# UI projection helpers (pure)
# ---------------------------------------------------------------------------


def test_confirm_button_label_reflects_count(tmp_path):
    root, _paths = _library(tmp_path, count=7)
    state = scan_folder_action(set_folder_path(initial_state(), root))
    assert state.confirm_button_label() == "✅ Confirm 7 files"
    assert initial_state().confirm_button_label() == "✅ Confirm source files"


def test_status_text_distinguishes_never_confirmed_from_invalidated(tmp_path):
    fresh = initial_state()
    assert "NOT CONFIRMED" in fresh.confirmation_status_text()

    state, root, _paths = _confirmed_folder_state(tmp_path)
    assert "CONFIRMED" in state.confirmation_status_text()
    assert "NOT CONFIRMED" not in state.confirmation_status_text()

    write_file(os.path.join(root, "new.mp4"), b"new")
    invalidated = scan_folder_action(state)
    assert "NOT CONFIRMED" in invalidated.confirmation_status_text()


def test_scan_failure_is_surfaced_and_leaves_nothing_confirmable(tmp_path):
    state = scan_folder_action(set_folder_path(initial_state(), str(tmp_path / "missing")))

    assert "SCAN FAILED" in state.report_text
    assert state.can_confirm() is False
    assert resolve_for_render(state).allowed is False


def test_blank_folder_path_is_handled(tmp_path):
    state = scan_folder_action(set_folder_path(initial_state(), "   "))
    assert state.can_confirm() is False
    assert "No folder selected" in state.report_text
