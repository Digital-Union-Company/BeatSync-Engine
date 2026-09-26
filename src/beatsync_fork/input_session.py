#!/usr/bin/env python3
"""The source-input state machine behind the UI's confirmation gate.

Every transition is a pure function returning a **new** :class:`SourceSessionState`. The UI layer owns
only the widget wiring; all the decisions — what invalidates a confirmation, what may be confirmed,
whether a render may start — live here so they can be tested without starting Gradio.

Backend state is the authority. Button appearance is a projection of this state, never the source of
truth: a disabled button is a courtesy, and :func:`resolve_for_render` is the actual gate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from beatsync_fork.input_confirmation import (
    ConfirmationError,
    GateDecision,
    GateReason,
    LiveSourceDeclaration,
    SourceMode,
    SourceSnapshot,
    SourceSnapshotError,
    check_declaration,
    confirm_snapshot,
    evaluate_gate,
    snapshot_from_media_files,
    snapshot_from_paths,
)
from beatsync_fork.input_manager import InputScanError, InputSet, RejectReason, scan_folder
from beatsync_fork.input_report import InputReport

DEFAULT_MODE = SourceMode.LOCAL_FOLDER
"""Local folder is the default: for a few-hundred-file library it is the only mode whose counts are
authoritative by construction, and it avoids duplicating the media into Gradio's upload directory."""

DEFAULT_RECURSIVE = True

BROWSER_HELP = (
    "Browser mode reports only what the backend has actually received. The number of files you "
    "selected in the file dialog is browser-side state that is never sent to the app, so it cannot "
    "be shown here. Wait until 'Backend ready' stops rising and matches what you expect, then "
    "confirm. For large libraries prefer Local folder."
)


@dataclass(frozen=True, slots=True)
class SourceSessionState:
    """Everything the UI needs to know about the source selection."""

    mode: SourceMode = DEFAULT_MODE
    folder_path: str = ""
    recursive: bool = DEFAULT_RECURSIVE
    current: SourceSnapshot | None = None
    """Snapshot of the latest scan / browser list. ``None`` means nothing usable is selected yet."""

    confirmed: SourceSnapshot | None = None
    """Snapshot the user explicitly accepted. Cleared by any source-identity change."""

    report_text: str = ""
    """Human-readable status for the UI, produced by the transition that set it."""

    @property
    def current_count(self) -> int:
        return 0 if self.current is None else self.current.count

    @property
    def confirmed_count(self) -> int:
        return 0 if self.confirmed is None else self.confirmed.count

    def can_confirm(self) -> bool:
        return self.current is not None and not self.current.is_empty()

    def is_confirmed(self) -> bool:
        """True only when a confirmation exists and still matches the current selection."""
        return (
            self.confirmed is not None
            and not self.confirmed.is_empty()
            and self.current is not None
            and self.current.matches(self.confirmed)
        )

    def confirm_button_label(self) -> str:
        if not self.can_confirm():
            return "✅ Confirm source files"
        count = self.current_count
        return f"✅ Confirm {count} file{'s' if count != 1 else ''}"

    def confirmation_status_text(self) -> str:
        if self.is_confirmed():
            count = self.confirmed_count
            return (
                f"✓ **CONFIRMED** — {count} source video{'s' if count != 1 else ''} "
                f"(`{self.confirmed.short_digest()}`)"
            )
        if self.confirmed is not None:
            return "⚠️ **NOT CONFIRMED** — the source selection changed. Confirm again."
        return "⚠️ **NOT CONFIRMED** — confirm the source videos to enable Create Music Video."


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def initial_state() -> SourceSessionState:
    return SourceSessionState(report_text="Choose a source folder and press Scan Folder.")


def snapshot_from_input_set(input_set: InputSet) -> SourceSnapshot:
    """Build a folder snapshot covering the whole **supported** scope, not just the ready subset.

    Identity has to include supported files the scan could not use. On a live library - the real
    ``Cuts`` folder was observed gaining about one MP4 per minute - a newly created ``.mp4`` is often
    0 bytes or briefly unreadable for its first moments. Such a file is rejected, so the ready list is
    unchanged, and a ready-only snapshot would report "no change" while a new source entry had in fact
    appeared. The render list stays the ready files only; this just makes the gate honest about scope.

    Unsupported extensions are excluded on purpose: a new ``.mp3`` or ``.txt`` is not a video source
    and must not invalidate a confirmation.
    """
    supported_rejections = [
        item for item in input_set.rejected if item.reason is not RejectReason.UNSUPPORTED_EXTENSION
    ]
    return snapshot_from_media_files(
        input_set.files,
        scan_root=input_set.root,
        recursive=input_set.recursive,
        excluded=supported_rejections,
    )


def declaration_from_state(state: SourceSessionState) -> LiveSourceDeclaration:
    """The declaration implied by the stored state, for callers that have no live widget values."""
    return LiveSourceDeclaration(
        mode=state.mode,
        folder_path=state.folder_path,
        recursive=state.recursive,
        browser_paths=() if state.current is None else state.current.paths,
    )


def live_declaration(mode_value, folder_path, recursive, browser_files) -> LiveSourceDeclaration:
    """Build a declaration from raw Gradio widget values.

    Tolerant of the shapes Gradio hands back (``None``, a single path, a list) so the UI layer stays a
    one-line call site.
    """
    try:
        mode = SourceMode(str(mode_value))
    except ValueError:
        mode = DEFAULT_MODE

    if browser_files is None:
        paths: tuple[str, ...] = ()
    elif isinstance(browser_files, (str, bytes)):
        paths = (str(browser_files),)
    else:
        paths = tuple(str(item) for item in browser_files if item)

    return LiveSourceDeclaration(
        mode=mode,
        folder_path="" if folder_path is None else str(folder_path),
        recursive=bool(recursive),
        browser_paths=paths,
    )


def _invalidated(state: SourceSessionState, report_text: str, **changes) -> SourceSessionState:
    """Apply changes and drop both the current selection and any confirmation."""
    return replace(state, current=None, confirmed=None, report_text=report_text, **changes)


def set_mode(state: SourceSessionState, mode: SourceMode | str) -> SourceSessionState:
    """Switch input mode. Always invalidates: a different mode is a different source set."""
    mode = SourceMode(mode)
    if mode is SourceMode.LOCAL_FOLDER:
        text = "Local folder mode. Choose a folder and press Scan Folder."
    else:
        text = "Browser mode. Select video files, then confirm.\n\n" + BROWSER_HELP
    return _invalidated(state, text, mode=mode)


def set_folder_path(state: SourceSessionState, folder_path: str) -> SourceSessionState:
    """Record a new folder path. Invalidates; the user must scan and confirm again."""
    return _invalidated(
        state,
        "Folder changed. Press Scan Folder.",
        folder_path="" if folder_path is None else str(folder_path),
    )


def set_recursive(state: SourceSessionState, recursive: bool) -> SourceSessionState:
    """Toggle subfolder inclusion. Invalidates; it changes which files are in scope."""
    return _invalidated(
        state, "Subfolder setting changed. Press Scan Folder.", recursive=bool(recursive)
    )


def scan_folder_action(state: SourceSessionState) -> SourceSessionState:
    """Scan the configured folder with the merged input manager.

    Always invalidates any prior confirmation, even if the resulting file set happens to be
    identical: the user asked to re-scan, so they must re-accept the result.

    Duplicate detection is left **on** here — this is the one place the user is shown a library
    overview, and the scanner only fingerprints files whose size is not already unique, so the cost
    on a real library is small.
    """
    if not state.folder_path.strip():
        return _invalidated(state, "No folder selected. Enter a folder path and press Scan Folder.")

    try:
        input_set: InputSet = scan_folder(
            state.folder_path, recursive=state.recursive, detect_duplicates=True
        )
    except InputScanError as exc:
        return _invalidated(state, f"❌ SCAN FAILED\n{exc}")
    except OSError as exc:  # pragma: no cover - defensive
        return _invalidated(state, f"❌ SCAN FAILED\n{exc}")

    report = InputReport.from_input_set(input_set).render_text()
    snapshot = snapshot_from_input_set(input_set)
    if snapshot.is_empty():
        return _invalidated(state, report + "\n\nNothing to confirm.")
    return replace(state, current=snapshot, confirmed=None, report_text=report)


def set_browser_files(
    state: SourceSessionState, file_paths: "list[str] | tuple[str, ...] | None"
) -> SourceSessionState:
    """Record the backend-visible browser file list.

    Deliberately reports only ``Backend ready`` — the count of files the server has actually
    received. The number the user selected in the browser dialog is frontend state that is never
    transmitted, so claiming to know it (or a "pending" figure derived from it) would be a fabricated
    number on the one screen whose entire purpose is trustworthy counts.
    """
    paths = [str(p) for p in (file_paths or []) if p]
    if not paths:
        return _invalidated(state, "No browser files received yet.\n\n" + BROWSER_HELP)

    try:
        snapshot = snapshot_from_paths(paths, SourceMode.BROWSER_FILES)
    except SourceSnapshotError as exc:
        return _invalidated(state, f"❌ BROWSER INPUT UNREADABLE\n{exc}")

    report = "\n".join(
        [
            "Mode:          Browser files",
            f"Backend ready: {snapshot.count}",
            f"Total size:    {_format_bytes(snapshot.total_bytes)}",
            "",
            BROWSER_HELP,
        ]
    )
    return replace(state, current=snapshot, confirmed=None, report_text=report)


def confirm_action(state: SourceSessionState) -> SourceSessionState:
    """Accept the current selection as confirmed."""
    try:
        confirmed = confirm_snapshot(state.current)
    except ConfirmationError as exc:
        return replace(state, confirmed=None, report_text=f"❌ CANNOT CONFIRM\n{exc}")
    return replace(state, confirmed=confirmed)


# ---------------------------------------------------------------------------
# The render gate
# ---------------------------------------------------------------------------


def current_snapshot_for_render(
    state: SourceSessionState, live: LiveSourceDeclaration | None = None
) -> SourceSnapshot | None:
    """Re-derive the source identity from the filesystem, right now, from the **live** declaration.

    ``live`` carries the widget values submitted with the render request. When omitted it is derived
    from the stored state, which is only safe for callers that have no widgets (tests, scripts) - the
    UI must always pass the real thing, because stored state can lag behind queued Gradio events.

    Folder mode re-scans the live folder with ``detect_duplicates=False``: duplicate grouping is a
    reporting feature, not identity, so its reads would be pure waste on every render. Browser mode
    snapshots the **live** ``gr.File`` list; using the confirmed paths instead would re-verify only the
    files already approved and would never notice a late upload.

    Returns ``None`` when the sources cannot be read at all, which the gate reports rather than
    treating as "no change".
    """
    declaration = live if live is not None else declaration_from_state(state)

    if declaration.mode is SourceMode.LOCAL_FOLDER:
        folder = declaration.folder_path
        if not folder.strip():
            return None
        try:
            fresh = scan_folder(folder, recursive=declaration.recursive, detect_duplicates=False)
        except (InputScanError, OSError):
            return None
        return snapshot_from_input_set(fresh)

    if not declaration.browser_paths:
        return SourceSnapshot(mode=SourceMode.BROWSER_FILES, entries=())
    try:
        return snapshot_from_paths(declaration.browser_paths, SourceMode.BROWSER_FILES)
    except SourceSnapshotError:
        return None


def resolve_for_render(
    state: SourceSessionState, live: LiveSourceDeclaration | None = None
) -> GateDecision:
    """The gate. No Stage 1 work may begin unless this returns ``allowed``.

    Two independent checks, cheapest first:

    1. :func:`~beatsync_fork.input_confirmation.check_declaration` compares the *declared intent*
       (mode, folder, recursive) against the confirmation, so a changed folder is caught before any
       filesystem work and a folder the user has navigated away from is never scanned.
    2. The identity comparison, against a snapshot re-derived from the live declaration.

    UI disablement is not sufficient on its own: a stale tab, a queued event or a direct API call can
    all reach the render handler.
    """
    if state.confirmed is None or state.confirmed.is_empty():
        return evaluate_gate(state.confirmed, None)

    declaration = live if live is not None else declaration_from_state(state)

    declaration_denial = check_declaration(state.confirmed, declaration)
    if declaration_denial is not None:
        return declaration_denial

    return evaluate_gate(state.confirmed, current_snapshot_for_render(state, declaration))


def _format_bytes(size: int) -> str:
    """Local copy of the byte formatter to keep this module independent of the report layer."""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024.0 or unit == "PB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"  # pragma: no cover


__all__ = [
    "BROWSER_HELP",
    "DEFAULT_MODE",
    "DEFAULT_RECURSIVE",
    "GateDecision",
    "GateReason",
    "LiveSourceDeclaration",
    "SourceMode",
    "SourceSessionState",
    "confirm_action",
    "current_snapshot_for_render",
    "declaration_from_state",
    "initial_state",
    "live_declaration",
    "resolve_for_render",
    "scan_folder_action",
    "set_browser_files",
    "set_folder_path",
    "set_mode",
    "set_recursive",
    "snapshot_from_input_set",
]
