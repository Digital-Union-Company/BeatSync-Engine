"""Filing an exported audio clip: KEEP into the voice library, DISCARD into quarantine.

The same safety design as the Clips Preview sorter, for the same reasons, stated once more because
this is a second caller and not a second policy:

* **`os.rename` is the mechanism, and that is a contract rather than an implementation detail.**
  On Windows it *refuses* an existing destination with `FileExistsError`, so "never overwrite" is
  enforced by the syscall instead of by an `exists()` check that a second press could race. And it
  *raises* across volumes instead of copying, so a cross-volume request is refused in words rather
  than silently degrading into copy-then-delete. :func:`os.replace` would overwrite — never use it
  here.
* **Discard is quarantine, not deletion.** Nothing in this module deletes anything. There is no
  retention expiry and no cleanup pass; a discarded clip is a file that moved.
* **A taken name is reported, never worked around — unless the operator asked otherwise.** That
  was unconditional until R8, and it is still the DEFAULT. Settings now offers an opt-in
  `collision.SUFFIX` policy, under which a taken name advances to `clip_01`, `clip_02`, … The two
  guarantees that made the original rule safe are unchanged by it: the rename is still no-replace,
  so an existing file is never overwritten under either policy, and the retry is driven by the
  rename *failing* rather than by an `exists()` check, so there is no window for a racing writer.
  See `beatsync_fork.collision`.

This is deliberately **not** the P2 transfer engine. There is no journal, no content fingerprint,
no staging and no recovery report — it is one rename inside one volume, which is all a V1 needs and
is the one shape that cannot half-succeed.

Stdlib only (`os`), so the fork package's no-runtime-import rule still holds.
"""

from __future__ import annotations

import os

from . import collision

KEPT = "kept"
DISCARDED = "discarded"

#: Outcomes of one filing attempt.
MOVED = "moved"
REFUSED = "refused"


class FilingError(Exception):
    """A clip could not be filed. Carries operator-readable prose, never a raw errno."""


def _describe(failure: Exception, destination_name: str) -> str:
    """Windows collapses several distinct conditions onto one errno, so name them."""
    winerror = getattr(failure, "winerror", None)
    if isinstance(failure, FileExistsError) or winerror == 183:
        return (f"a file called {destination_name} is already there. "
                f"Nothing was overwritten and the clip was left where it is.")
    if winerror == 17 or getattr(failure, "errno", None) == 18:
        return ("that folder is on a different drive. This V1 moves files within one drive only, "
                "so nothing was copied or deleted — choose a folder on the same drive as the "
                "clips.")
    if winerror == 32:
        return ("something still has the file open. Stop playing the clip and try again — "
                "nothing was changed.")
    if isinstance(failure, PermissionError):
        return "Windows refused permission. Nothing was changed."
    if isinstance(failure, FileNotFoundError):
        return "the clip or the destination folder is no longer there. Nothing was changed."
    return "it could not be moved. Nothing was changed."


def target_name(preferred: str, source_path: str) -> str:
    """The filename to file under: the operator's edit when they made one, else the clip's own.

    The extension always comes from the real file, so renaming a `.wav` to `voice` or to
    `voice.mp3` still produces a correctly-suffixed `.wav` — a wrong extension here would hand
    Audio Layers a file it could not open.
    """
    _, extension = os.path.splitext(os.path.basename(str(source_path or "")))
    chosen = os.path.basename(str(preferred or "").strip())
    if not chosen:
        return os.path.basename(str(source_path or ""))
    stem = os.path.splitext(chosen)[0]
    cleaned = "".join(ch for ch in stem if ch not in '<>:"/\\|?*').strip(" .")
    if not cleaned:
        return os.path.basename(str(source_path or ""))
    return f"{cleaned}{extension}"


def file_clip(source_path: str, destination_folder: str, preferred_name: str = "",
              policy: str = collision.DEFAULT_POLICY) -> str:
    """Move one clip into `destination_folder`. Returns the destination path.

    Raises :class:`FilingError` with operator-readable prose on every refusal. The source is never
    deleted on a failure, and never overwritten at the destination on success.

    [FORK] R8: `policy` selects what happens when the name is taken. The **return type is
    deliberately unchanged** -- still the destination path, not a `(path, tries)` pair. The caller
    needs to know whether a different name was written, and it can already see that by comparing
    the returned basename with the one it asked for; widening the contract would have broken every
    existing caller and assertion to convey something the return value already carries.

    Under the default :data:`collision.SKIP` the behaviour is unchanged down to the wording:
    exactly one no-replace rename, and `FileExistsError` becomes the same "a file called X is
    already there" sentence it always did.
    """
    source_path = str(source_path or "")
    folder = str(destination_folder or "").strip()
    if not folder:
        raise FilingError("No destination folder is set.")
    if not os.path.isfile(source_path):
        raise FilingError("That clip is no longer on disk. Nothing was changed.")
    if not os.path.isdir(folder):
        raise FilingError(f"{folder} is not a folder that exists. Nothing was created or moved.")

    name = target_name(preferred_name, source_path)
    destination = os.path.join(folder, name)

    if os.path.abspath(destination) == os.path.abspath(source_path):
        return destination                     # already exactly where it was asked to go

    try:
        written, _tries = collision.publish(source_path, folder, name, os.rename, policy=policy)
    except collision.NoFreeName as failure:
        # One attempt means the plain "that name is taken" case, which is the ONLY case the
        # default policy can produce -- so it keeps `_describe`'s exact pre-R8 sentence. Only an
        # exhausted suffix sequence gets the new wording.
        if failure.attempts <= 1:
            raise FilingError(
                f"{name}: {_describe(FileExistsError(), name)}") from failure
        raise FilingError(str(failure)) from failure
    except OSError as failure:
        raise FilingError(f"{name}: {_describe(failure, name)}") from failure
    return written


def destination_problem(folder: str, label: str, run_directory: str = "") -> str:
    """Read-only check of ONE destination. Returns `""` when it is usable.

    Creates nothing. Only the folder actually being written to is ever checked, so an operator who
    only keeps clips never has to name a quarantine folder to keep one.

    The one structural rule is that a destination may not be the folder of clips being reviewed,
    nor inside it: filing a clip into the folder it is being reviewed from would make the same clip
    reappear in the next batch.
    """
    text = str(folder or "").strip().strip('"')
    if not text:
        return f"Set the {label} first."
    if not os.path.isdir(text):
        return f"The {label} is not a folder that exists:\n{text}\nNothing was created or moved."

    if run_directory:
        run_real = os.path.abspath(run_directory)
        real = os.path.abspath(text)
        if real == run_real or real.startswith(run_real + os.sep):
            return (f"The {label} must not be inside the folder of clips being reviewed. "
                    f"Nothing was moved.")
    return ""


__all__ = ["KEPT", "DISCARDED", "MOVED", "REFUSED", "FilingError",
           "target_name", "file_clip", "destination_problem"]
