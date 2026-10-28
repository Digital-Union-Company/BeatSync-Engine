"""What to do when the destination filename is already taken.

Both review workflows publish by moving a file into a folder the operator chose, and both have
always refused a name that was already there. Refusing is the right default and stays the default:
inventing a name produces a library the operator never approved, and the existing wording
(``a file of that name is already there; nothing was overwritten``) is a true and useful thing to
read.

But refusing is not always what the operator wants. Sorting forty takes of the same line into one
voice library means forty collisions on ``take.wav``, and resolving each by hand is the whole of
the work. So this module adds **one opt-in alternative** and nothing else:

```
SKIP    (default)  the pre-existing behaviour, byte for byte: report the collision, move nothing
SUFFIX  (opt-in)   clip.wav -> clip_01.wav -> clip_02.wav -> ...
```

Four properties make the opt-in safe, and each is a deliberate choice rather than an accident of
implementation:

* **The rename is still the authority, never an `exists()` check.** :func:`publish` calls the
  caller's no-replace rename and advances to the next candidate **when that call reports the name
  is taken**. A pre-check would leave a window in which another process could create the file
  between the check and the move; here there is no such window, because the check *is* the move.
  That is the same reasoning `_promote_output_no_replace` rests on in `gui.py`.
* **It retries.** A candidate name that is occupied by the time the rename runs is not a failure,
  it is the next iteration. This matters in practice: two review sessions filing into one library
  will race, and the loser of a race should take `_02`, not report an error.
* **The original is never lost.** If no free name is found within :data:`MAX_ATTEMPTS`, nothing has
  been moved and :class:`NoFreeName` is raised. The source file is exactly where it was.
* **Nothing here deletes, copies or overwrites.** There is no `unlink`, no `replace`, no fallback
  copy. The only mutation is the caller's rename, and it is a no-replace one.

Stdlib only (`os`), so the fork package's no-runtime-import rule still holds. It performs no I/O of
its own beyond the rename it is handed -- the caller supplies that, which is what makes every
branch here testable without touching a disk.
"""

from __future__ import annotations

import os

#: The two policies. Values are stored in `state/preferences.json`, so they are stable strings
#: rather than an enum -- a preferences file written by this version must still load in the next.
SKIP = "skip"
SUFFIX = "suffix"
POLICIES = (SKIP, SUFFIX)

#: The default is the pre-existing behaviour. A new setting must never change what the app does
#: for an operator who has not touched it.
DEFAULT_POLICY = SKIP

#: How many suffixed names to try before giving up. Two-digit names are the documented shape
#: (`_01` … `_99`); beyond that the numbering simply widens rather than wrapping, because a
#: hundredth collision is a real situation and silently failing at it would be worse than an
#: unpadded `_100`. The bound exists so a pathological folder cannot spin forever.
MAX_ATTEMPTS = 999


class NoFreeName(Exception):
    """Every candidate was taken. Nothing was moved and the source is untouched.

    `attempts` is how many names were tried, and callers **must** branch on it rather than on the
    policy they passed. Under :data:`SKIP` it is always `1`, which is the ordinary "that name is
    taken" case every caller already had wording for -- and reusing that wording is what keeps the
    default policy's operator-visible behaviour byte-identical to the pre-R8 behaviour. Only
    `attempts > 1` is the genuinely new situation of an exhausted suffix sequence.
    """

    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.attempts = int(attempts)


def resolve_policy(value) -> str:
    """Total: anything unrecognised is the safe default.

    Called with whatever a Gradio radio, a preferences file or an absent key produced, so `None`,
    a number, a mapping and a misspelling all have to answer something. They answer :data:`SKIP`,
    because a corrupt setting must not be read as permission to invent filenames.
    """
    if isinstance(value, str) and value.strip().lower() in POLICIES:
        return value.strip().lower()
    return DEFAULT_POLICY


def split_name(name: str) -> tuple[str, str]:
    """`("clip", ".wav")`. The extension is preserved exactly, including its case.

    `os.path.splitext` already does the right thing for the cases that matter here, including a
    dotfile (`.gitignore` -> `(".gitignore", "")`, not `("", ".gitignore")`) and a name with no
    extension at all. It is wrapped only so the suffix rule has one place to read it from.
    """
    stem, extension = os.path.splitext(str(name or ""))
    return stem, extension


def candidate_names(name: str, policy: str = DEFAULT_POLICY, limit: int = MAX_ATTEMPTS):
    """Yield the names to try, in order, starting with the operator's own.

    Under :data:`SKIP` that is a one-element sequence -- the requested name and nothing else --
    which is what makes the default provably identical to the old behaviour rather than merely
    similar to it.

    Under :data:`SUFFIX` the first candidate is still the unmodified name, so a file that does not
    collide keeps the name it was given. Suffixes only ever appear on an actual collision.
    """
    requested = str(name or "")
    yield requested
    if resolve_policy(policy) != SUFFIX:
        return
    stem, extension = split_name(requested)
    for index in range(1, int(limit) + 1):
        yield f"{stem}_{index:02d}{extension}"


def publish(source_path: str, folder: str, name: str, rename, *,
            policy: str = DEFAULT_POLICY, limit: int = MAX_ATTEMPTS,
            taken=None) -> tuple[str, int]:
    """Move `source_path` into `folder` under the first free name. Returns `(destination, tries)`.

    `rename(source, destination)` must be a **no-replace** move and must raise on an occupied
    destination -- `os.rename` on Windows does exactly that. It is injected rather than called
    directly so the retry loop, the exhaustion path and the suffix sequence are all testable
    without a filesystem, and so each caller keeps its own identity re-checks and its own error
    vocabulary.

    `taken(exc) -> bool` decides which exceptions mean "that name is in use" and therefore cause a
    retry; everything else propagates untouched to the caller, which already knows how to describe
    a cross-volume refusal or a sharing violation. The default treats `FileExistsError` as taken,
    which is what `os.rename` raises.

    `tries` is the number of rename attempts made, so `1` means the requested name was free. The
    caller reports the **returned destination**, never the requested name -- showing the name that
    was asked for when a different one was written is the one way this feature could mislead.
    """
    is_taken = taken or (lambda exc: isinstance(exc, FileExistsError))
    attempts = 0
    for candidate in candidate_names(name, policy, limit):
        destination = os.path.join(str(folder or ""), candidate)
        attempts += 1
        try:
            rename(source_path, destination)
        except Exception as exc:                      # noqa: BLE001 -- re-raised unless "taken"
            if is_taken(exc):
                continue
            raise
        return destination, attempts
    raise NoFreeName(
        f"{name}: the name is taken and so are the {attempts - 1} numbered alternatives tried "
        f"after it. Nothing was moved.", attempts)


def describe(destination: str, tries: int) -> str:
    """A short, honest note for the report, or `""` when nothing interesting happened.

    Silent on the ordinary case (`tries == 1`): a report that announced "kept its own name" on
    every line would bury the lines that matter. It speaks only when the written name differs from
    the requested one, which is exactly when the operator needs to be told.
    """
    if int(tries) <= 1:
        return ""
    return f"renamed to {os.path.basename(destination)} — the original name was taken"


__all__ = ["SKIP", "SUFFIX", "POLICIES", "DEFAULT_POLICY", "MAX_ATTEMPTS", "NoFreeName",
           "resolve_policy", "split_name", "candidate_names", "publish", "describe"]
