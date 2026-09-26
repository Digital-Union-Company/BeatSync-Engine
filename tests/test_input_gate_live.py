"""The gate must trust the LIVE source controls, not only the stored session state.

Two review findings are pinned here.

**A — stale state.** Gradio delivers widget changes as separate queued events, so when Create is
clicked the stored state can lag behind the widgets: a file finishes uploading, or the folder textbox
changes, without its `change` handler having run. Every test below deliberately does **not** run the
normal transition first, so the state is stale on purpose.

**B — scope, not just the ready subset.** A live library gains `.mp4` files that are momentarily 0
bytes or unreadable. Those are rejected, so the ready list is unchanged; a ready-only identity would
report "no change" while a new source entry had appeared. Unsupported files (`.mp3`, `.txt`) must still
be ignored, because they are not video sources.
"""

from __future__ import annotations

import os

from conftest import write_file

from beatsync_fork.input_confirmation import GateReason, LiveSourceDeclaration, SourceMode
from beatsync_fork.input_session import (
    confirm_action,
    current_snapshot_for_render,
    declaration_from_state,
    initial_state,
    live_declaration,
    resolve_for_render,
    scan_folder_action,
    set_browser_files,
    set_folder_path,
    set_mode,
)


def _files(tmp_path, name, count=2, start=0):
    root = str(tmp_path / name)
    return root, [
        write_file(os.path.join(root, f"clip_{i}.mp4"), b"x" * (10 + i))
        for i in range(start, start + count)
    ]


def _confirmed_browser(tmp_path, count=2):
    _root, paths = _files(tmp_path, "browser", count)
    state = confirm_action(
        set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), paths)
    )
    assert state.is_confirmed()
    return state, paths


def _confirmed_folder(tmp_path, name="lib", count=2):
    root, paths = _files(tmp_path, name, count)
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))
    assert state.is_confirmed()
    return state, root, paths


def _folder_live(root, recursive=True):
    return live_declaration(SourceMode.LOCAL_FOLDER.value, root, recursive, None)


def _browser_live(paths):
    return live_declaration(SourceMode.BROWSER_FILES.value, "", False, paths)


# ---------------------------------------------------------------------------
# A — browser: the live gr.File list is the authority
# ---------------------------------------------------------------------------


def test_live_browser_list_with_extra_file_denies(tmp_path):
    """A1. Confirmed [a,b]; a third upload landed; its change event has not run."""
    state, paths = _confirmed_browser(tmp_path)
    late = write_file(str(tmp_path / "browser" / "late.mp4"), b"late arrival")

    # State is deliberately stale: it still believes the confirmed two files are current.
    assert state.current.count == 2

    decision = resolve_for_render(state, _browser_live(paths + [late]))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED
    assert "1 added" in decision.message
    assert decision.paths == ()


def test_live_browser_list_same_count_replacement_denies(tmp_path):
    """A2. Same length, different member — the case a count-only check cannot see."""
    state, paths = _confirmed_browser(tmp_path)
    other = write_file(str(tmp_path / "browser" / "other.mp4"), b"z" * 11)
    live = _browser_live([paths[0], other])

    assert len(live.browser_paths) == state.confirmed.count
    decision = resolve_for_render(state, live)
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_live_browser_list_with_removed_file_denies(tmp_path):
    state, paths = _confirmed_browser(tmp_path, count=3)
    decision = resolve_for_render(state, _browser_live(paths[:2]))
    assert decision.allowed is False
    assert "1 removed" in decision.message


def test_live_browser_reorder_denies(tmp_path):
    state, paths = _confirmed_browser(tmp_path, count=3)
    decision = resolve_for_render(state, _browser_live(list(reversed(paths))))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_live_browser_empty_list_denies(tmp_path):
    state, _paths = _confirmed_browser(tmp_path)
    decision = resolve_for_render(state, _browser_live([]))
    assert decision.allowed is False
    assert decision.reason is GateReason.NO_SOURCES


def test_live_browser_missing_file_denies(tmp_path):
    state, paths = _confirmed_browser(tmp_path)
    os.remove(paths[0])
    decision = resolve_for_render(state, _browser_live(paths))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_UNAVAILABLE


def test_live_browser_unchanged_list_allows(tmp_path):
    """Control: the guard must not produce false denials."""
    state, paths = _confirmed_browser(tmp_path)
    decision = resolve_for_render(state, _browser_live(paths))
    assert decision.allowed is True
    assert decision.paths == tuple(os.path.abspath(p) for p in paths)


def test_browser_snapshot_uses_live_paths_not_confirmed_paths(tmp_path):
    """The mechanism itself: the fresh snapshot must be built from the live list."""
    state, paths = _confirmed_browser(tmp_path)
    late = write_file(str(tmp_path / "browser" / "late.mp4"), b"late")

    snapshot = current_snapshot_for_render(state, _browser_live(paths + [late]))
    assert snapshot.count == 3, "snapshot ignored the live list and used confirmed.paths"


# ---------------------------------------------------------------------------
# A — folder: live mode / folder / recursive controls
# ---------------------------------------------------------------------------


def test_live_folder_change_denies(tmp_path):
    """A3. The textbox points elsewhere; the state transition has not run yet."""
    state, root, _paths = _confirmed_folder(tmp_path, "libA")
    other_root, _other = _files(tmp_path, "libB")

    assert state.folder_path == root, "state is deliberately stale"
    decision = resolve_for_render(state, _folder_live(other_root))
    assert decision.allowed is False
    assert "folder changed" in decision.message


def test_live_recursive_change_denies(tmp_path):
    """A4."""
    state, root, _paths = _confirmed_folder(tmp_path)
    assert state.recursive is True

    decision = resolve_for_render(state, _folder_live(root, recursive=False))
    assert decision.allowed is False
    assert "subfolder setting changed" in decision.message


def test_live_mode_change_denies(tmp_path):
    """A5."""
    state, _root, _paths = _confirmed_folder(tmp_path)
    decision = resolve_for_render(state, _browser_live([]))
    assert decision.allowed is False
    assert "input mode changed" in decision.message


def test_live_blank_folder_denies(tmp_path):
    state, _root, _paths = _confirmed_folder(tmp_path)
    decision = resolve_for_render(state, _folder_live("   "))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_changed_folder_is_never_scanned(tmp_path, monkeypatch):
    """The declaration check runs first, so a folder the user left is not scanned."""
    state, _root, _paths = _confirmed_folder(tmp_path)
    other_root, _other = _files(tmp_path, "elsewhere")

    calls = []
    import beatsync_fork.input_session as session

    real_scan = session.scan_folder
    monkeypatch.setattr(
        session, "scan_folder", lambda *a, **k: calls.append(a[0]) or real_scan(*a, **k)
    )

    assert resolve_for_render(state, _folder_live(other_root)).allowed is False
    assert calls == [], "a folder outside the confirmed declaration must not be scanned"


def test_live_folder_unchanged_allows(tmp_path):
    state, root, paths = _confirmed_folder(tmp_path)
    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is True
    assert set(decision.paths) == {os.path.abspath(p) for p in paths}


def test_declaration_from_state_matches_a_live_declaration(tmp_path):
    """Omitting `live` falls back to the state's own declaration, which must agree."""
    state, root, _paths = _confirmed_folder(tmp_path)
    implied = declaration_from_state(state)

    assert implied == LiveSourceDeclaration(
        mode=SourceMode.LOCAL_FOLDER,
        folder_path=root,
        recursive=True,
        browser_paths=state.current.paths,
    )
    assert resolve_for_render(state).allowed is True


def test_live_declaration_tolerates_gradio_shapes():
    single = live_declaration("browser_files", None, None, "one.mp4")
    assert single.browser_paths == ("one.mp4",)
    assert single.folder_path == ""
    assert single.recursive is False

    nones = live_declaration("browser_files", "", False, [None, "a.mp4", ""])
    assert nones.browser_paths == ("a.mp4",)

    unknown = live_declaration("not_a_mode", "", False, None)
    assert unknown.mode is SourceMode.LOCAL_FOLDER, "unknown mode falls back to the default"


# ---------------------------------------------------------------------------
# B — supported-but-rejected files are part of folder scope
# ---------------------------------------------------------------------------


def test_new_empty_supported_file_denies(tmp_path):
    """B1. A new 0-byte .mp4 leaves the ready list unchanged but changes the scope."""
    state, root, _paths = _confirmed_folder(tmp_path)
    assert state.confirmed.excluded_count == 0

    open(os.path.join(root, "new.mp4"), "wb").close()

    fresh = current_snapshot_for_render(state, _folder_live(root))
    assert fresh.count == state.confirmed.count, "ready list unchanged, so only scope can catch this"
    assert fresh.excluded_count == 1

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is False
    assert "unusable source file(s) appeared" in decision.message


def test_new_unreadable_supported_file_denies(tmp_path, monkeypatch):
    """B2. Deterministic injection rather than ACL manipulation."""
    state, root, _paths = _confirmed_folder(tmp_path)
    locked = write_file(os.path.join(root, "locked.mp4"), b"locked")

    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and os.fspath(path) == locked:
            raise PermissionError(13, "Access is denied", locked)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", failing_stat)

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_new_unsupported_files_do_not_deny(tmp_path):
    """B3. A new .mp3 or .txt is not a video source and must not invalidate."""
    state, root, _paths = _confirmed_folder(tmp_path)

    write_file(os.path.join(root, "song.mp3"), b"id3")
    write_file(os.path.join(root, "notes.txt"), b"hello")
    write_file(os.path.join(root, "cover.jpg"), b"jpeg")

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is True, decision.message


def test_stable_rejected_file_allows(tmp_path):
    """B4. An unusable file that was already there at confirmation time is not a change."""
    root, paths = _files(tmp_path, "lib")
    open(os.path.join(root, "stub.mp4"), "wb").close()
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    assert state.confirmed.excluded_count == 1
    assert state.confirmed.count == len(paths)

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is True
    assert set(decision.paths) == {os.path.abspath(p) for p in paths}, "only ready files render"


def test_rejected_file_becoming_ready_denies(tmp_path):
    """B5. The stub gained content and is now a render candidate — re-confirm required."""
    root, _paths = _files(tmp_path, "lib")
    stub = os.path.join(root, "stub.mp4")
    open(stub, "wb").close()
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    write_file(stub, b"now usable")

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_ready_file_becoming_rejected_denies(tmp_path):
    """B-extra. A confirmed render candidate was truncated to 0 bytes."""
    state, root, paths = _confirmed_folder(tmp_path, count=2)
    open(paths[0], "wb").close()

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is False
    assert decision.reason is GateReason.SOURCE_CHANGED


def test_rejected_file_removed_denies(tmp_path):
    root, _paths = _files(tmp_path, "lib")
    stub = os.path.join(root, "stub.mp4")
    open(stub, "wb").close()
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))
    assert state.confirmed.excluded_count == 1

    os.remove(stub)

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is False
    assert "unusable source file(s) disappeared" in decision.message


def test_allowed_render_list_excludes_unusable_files(tmp_path):
    """Scope is wider than the render list; the render list stays ready-only."""
    root, paths = _files(tmp_path, "lib", count=3)
    open(os.path.join(root, "stub.mp4"), "wb").close()
    write_file(os.path.join(root, "song.mp3"), b"id3")
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    decision = resolve_for_render(state, _folder_live(root))
    assert decision.allowed is True
    assert len(decision.paths) == 3
    assert all(p.lower().endswith(".mp4") for p in decision.paths)
    assert not any("stub" in p or "song" in p for p in decision.paths)
