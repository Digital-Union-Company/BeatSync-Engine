"""The native Windows folder picker, as one helper every folder field shares.

BeatSync is a local desktop application that happens to draw its interface in a browser, so the
operator expects the Explorer folder dialog rather than a web upload control. A browser cannot
give us one: a file input returns file *contents* and a name, never a directory path, and no
amount of styling turns it into a path selector. So the dialog is opened by the backend, on the
same desktop, through `System.Windows.Forms.FolderBrowserDialog`.

Three things make that reliable rather than fragile, and each is here on purpose:

* **`-STA`.** `FolderBrowserDialog` is a COM/OLE control and simply does not open on an MTA
  thread. Windows PowerShell 5.1 defaults to MTA, so the switch is mandatory, not decorative.
* **The start path travels in the environment, never on the command line.** A folder path can
  contain quotes, `$`, backticks and semicolons, all of which are live characters to PowerShell.
  Reading `$env:BEATSYNC_PICK_START` sidesteps the whole quoting problem and means a hostile
  pathname cannot become a command.
* **A bounded `timeout`.** A modal dialog the operator wanders away from would otherwise hold a
  Gradio worker forever. The wait is finite and a timeout is reported as an honest outcome.

Why this module lives in the fork package rather than in `gui.py`: two suites pin that module's
`subprocess.run` call sites at exactly **two** (the delivery preview and the Director), to keep
process launching out of the view layer. `run_poster_command` and `run_duration_command` already
set the precedent, and this follows it -- the GUI hands over a path and gets a path back.

Stdlib only (`os`, `subprocess`), so the fork package's no-runtime-import rule still holds.
"""

from __future__ import annotations

import os
import subprocess

#: How long to wait for the operator to answer the dialog before giving up on it. Generous: this
#: is a human reading a folder tree, not a machine. The point is only that the wait is finite.
PICKER_TIMEOUT_SECONDS = 300

#: Outcomes. `CANCELLED` and `PICKED` are both ordinary answers; the other two are failures that
#: must leave the operator's existing path untouched.
PICKED = "picked"
CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"
TIMED_OUT = "timed_out"

#: Marker-prefixed output, so a path is never confused with a PowerShell warning on stdout.
_PICKED_PREFIX = "PICKED:"
_CANCELLED = "CANCELLED"

_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
    "$owner = New-Object System.Windows.Forms.Form; "
    "$owner.TopMost = $true; "
    "$owner.ShowInTaskbar = $false; "
    "$owner.StartPosition = 'CenterScreen'; "
    "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
    "$dlg.Description = 'Choose a folder for BeatSync'; "
    "$dlg.ShowNewFolderButton = $true; "
    "$start = $env:BEATSYNC_PICK_START; "
    "if ($start -and (Test-Path -LiteralPath $start -PathType Container)) "
    "{ $dlg.SelectedPath = $start }; "
    "$answer = $dlg.ShowDialog($owner); "
    "$owner.Dispose(); "
    "if ($answer -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ Write-Output ('" + _PICKED_PREFIX + "' + $dlg.SelectedPath) } "
    "else { Write-Output '" + _CANCELLED + "' }"
)


# [FORK] R7: the same dialog machinery for a single FILE, which the Raw Audio Cutter needs because
# its input is one recording rather than a folder. `OpenFileDialog` is the sibling control of
# `FolderBrowserDialog` and carries every one of the three properties above for the same reasons:
# it is equally a COM control (so `-STA` is equally mandatory), its start directory equally travels
# in the environment, and its wait is equally bounded.
#
# The filter string travels in the environment too, and for the same reason as the path: it is
# assembled from extension lists and a stray quote in one would otherwise be live PowerShell.
_FILE_SCRIPT = (
    "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
    "$owner = New-Object System.Windows.Forms.Form; "
    "$owner.TopMost = $true; "
    "$owner.ShowInTaskbar = $false; "
    "$owner.StartPosition = 'CenterScreen'; "
    "$dlg = New-Object System.Windows.Forms.OpenFileDialog; "
    "$dlg.Title = 'Choose a file for BeatSync'; "
    "$dlg.Multiselect = $false; "
    "$dlg.CheckFileExists = $true; "
    "$filter = $env:BEATSYNC_PICK_FILTER; "
    "if ($filter) { $dlg.Filter = $filter }; "
    "$start = $env:BEATSYNC_PICK_START; "
    "if ($start -and (Test-Path -LiteralPath $start -PathType Container)) "
    "{ $dlg.InitialDirectory = $start }; "
    "$answer = $dlg.ShowDialog($owner); "
    "$owner.Dispose(); "
    "if ($answer -eq [System.Windows.Forms.DialogResult]::OK) "
    "{ Write-Output ('" + _PICKED_PREFIX + "' + $dlg.FileName) } "
    "else { Write-Output '" + _CANCELLED + "' }"
)


def build_picker_command(powershell: str = "powershell.exe") -> list[str]:
    """The one picker argv. `-STA` is required; `-NoProfile` keeps a slow profile out of the way."""
    return [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _SCRIPT]


def build_file_picker_command(powershell: str = "powershell.exe") -> list[str]:
    """The file-dialog argv. Same switches, same reasons, different control."""
    return [powershell, "-NoProfile", "-NonInteractive", "-STA", "-Command", _FILE_SCRIPT]


def file_filter(label: str, suffixes) -> str:
    """A Windows dialog filter such as ``Audio and video|*.wav;*.mp4|All files|*.*``.

    Always ends with an all-files entry: the suffix list is a convenience, never a gate. The real
    check is whether FFprobe finds an audio stream, so an operator with an unusual container must
    still be able to select it.
    """
    patterns = ";".join(f"*{str(s).lower()}" for s in suffixes if str(s).startswith("."))
    if not patterns:
        return "All files|*.*"
    return f"{label}|{patterns}|All files|*.*"


def file_picker_environment(start: str | None, filter_text: str = "",
                            base: dict | None = None) -> dict:
    """The file dialog's environment: the start DIRECTORY and the filter, never a command line.

    `start` may be a file or a folder — a file's directory is used, which is what makes "Browse"
    reopen where the operator last was rather than at some default.
    """
    environment = dict(os.environ if base is None else base)
    environment.pop("BEATSYNC_PICK_START", None)
    environment.pop("BEATSYNC_PICK_FILTER", None)
    directory = ""
    if start:
        candidate = os.path.abspath(start)
        if os.path.isdir(candidate):
            directory = candidate
        elif os.path.isdir(os.path.dirname(candidate)):
            directory = os.path.dirname(candidate)
    if directory:
        environment["BEATSYNC_PICK_START"] = directory
    if filter_text:
        environment["BEATSYNC_PICK_FILTER"] = str(filter_text)
    return environment


def pick_file(start: str | None = None, *, filter_text: str = "", runner=None,
              timeout: int = PICKER_TIMEOUT_SECONDS,
              powershell: str = "powershell.exe") -> tuple[str, str]:
    """Open the native file dialog and return `(status, path)`.

    `path` is absolute only when `status` is :data:`PICKED`; every other status returns `""` and
    the caller must leave the existing value alone — identical to :func:`pick_folder`, so Browse
    can only ever add a choice.
    """
    run = runner or run_picker_command
    status, output = run(build_file_picker_command(powershell),
                         file_picker_environment(start, filter_text), timeout)
    if status in (TIMED_OUT, UNAVAILABLE):
        return status, ""
    status, chosen = parse_picker_output(output)
    if status != PICKED:
        return CANCELLED, ""
    return PICKED, os.path.abspath(chosen)


def picker_environment(start: str | None, base: dict | None = None) -> dict:
    """The child's environment, carrying the start folder out of the command line.

    An empty or non-existent start folder is simply not set, which leaves the dialog on its own
    default rather than opening somewhere misleading.
    """
    environment = dict(os.environ if base is None else base)
    environment.pop("BEATSYNC_PICK_START", None)
    if start and os.path.isdir(start):
        environment["BEATSYNC_PICK_START"] = os.path.abspath(start)
    return environment


def parse_picker_output(text: str) -> tuple[str, str]:
    """`(status, path)` from the child's stdout. Anything unrecognised is a cancellation.

    Reading an unknown answer as "keep what you had" is the safe direction: the alternative is
    pasting a diagnostic line into the operator's folder field.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith(_PICKED_PREFIX):
            chosen = line[len(_PICKED_PREFIX):].strip()
            return (PICKED, chosen) if chosen else (CANCELLED, "")
        if line == _CANCELLED:
            return CANCELLED, ""
    return CANCELLED, ""


def run_picker_command(argv, environment, timeout: int = PICKER_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Execute the picker and return `(status, stdout)`. Never raises.

    `stdin=DEVNULL` for the same reason the FFprobe runner uses it: a child that inherits a
    captured stdin can block on it. No `CREATE_NO_WINDOW` here -- this child is *supposed* to put
    something on screen, and suppressing its window is how the dialog ends up invisible.
    """
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=environment)
    except subprocess.TimeoutExpired:
        return TIMED_OUT, ""
    except (OSError, ValueError, subprocess.SubprocessError):
        return UNAVAILABLE, ""
    if int(completed.returncode) != 0:
        return UNAVAILABLE, completed.stdout or ""
    return PICKED, completed.stdout or ""


def pick_folder(start: str | None = None, *, runner=None,
                timeout: int = PICKER_TIMEOUT_SECONDS,
                powershell: str = "powershell.exe") -> tuple[str, str]:
    """Open the native dialog and return `(status, folder)`.

    `folder` is an absolute path only when `status` is :data:`PICKED`; every other status returns
    `""` and the caller must leave the existing value alone. `runner` is injected so the whole
    path is testable without opening a window.
    """
    run = runner or run_picker_command
    status, output = run(build_picker_command(powershell), picker_environment(start), timeout)
    if status in (TIMED_OUT, UNAVAILABLE):
        return status, ""
    status, chosen = parse_picker_output(output)
    if status != PICKED:
        return CANCELLED, ""
    return PICKED, os.path.abspath(chosen)


__all__ = ["PICKER_TIMEOUT_SECONDS", "PICKED", "CANCELLED", "UNAVAILABLE", "TIMED_OUT",
           "build_picker_command", "picker_environment", "parse_picker_output",
           "run_picker_command", "pick_folder",
           "build_file_picker_command", "file_filter", "file_picker_environment", "pick_file"]
