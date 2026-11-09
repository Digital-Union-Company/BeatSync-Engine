#!/usr/bin/env python3
"""Which source revision this installation is actually running.

`FORK_VERSION` says *what* this is; this module answers *which build* — the commit of the checkout
the running code was loaded from. The two are deliberately separate and must stay separate: a
version is a release decision a human makes, a build identity is a fact about this installation's
own disk. A header that shows only the version cannot distinguish two machines running the same
release from two different commits, which is the whole reason this exists.

**It describes the LOCAL checkout and nothing else.** No network request is made, no remote is
consulted, no `fetch` or `ls-remote` is run, and `UPSTREAM_BASELINE_COMMIT` is never substituted.
Each of those would answer a *different* question — what is published on GitHub, what upstream was
forked from — while reading on screen like an answer to this one. An installation copied out of a
checkout genuinely has no commit, and the honest answer there is `Build unknown`; a plausible SHA
is never invented to fill the space.

Three further rules the branches below exist to enforce:

* **A git worktree is only believed when it IS the application root.** A portable install dropped
  inside some unrelated repository would otherwise report that repository's commit as BeatSync's
  own. `--show-toplevel` is therefore compared, resolved and normalised, against the root handed
  in — never a prefix test, and never merely "a `.git` was found somewhere above us".
* **Tracked modifications are disclosed, not hidden.** A checkout with edited source is not the
  commit it names, so it is labelled. And when the modification state cannot be *measured* it is
  reported as unverified rather than silently as clean — the one direction that would turn a
  modified build into a confident-looking clean one.
* **No branch name is read.** Nothing here calls `symbolic-ref` or `--abbrev-ref`, which is what
  makes a detached HEAD work by construction rather than by a special case.

Nothing reads the operator's media, input/output directories, shared analysis cache or
configuration: the only paths touched are the application root the caller hands in and whatever
`git` reports about it. Nothing is written, created or deleted.

Per CLAUDE.md's hard rule this module imports no upstream runtime, so every branch below is
testable on a bare interpreter — the `git` calls go through an injectable runner, and the three
operator-facing tokens live here rather than in `ui_content.py` precisely so the module stays
importable with nothing but the standard library. The *templates* that place these tokens on the
page are in `ui_content.py` with every other string in the app.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

#: How much of the commit is shown. The full value is always kept on the record for diagnostics —
#: eight characters is a display choice, never the identity itself.
SHORT_LENGTH = 8

#: Bounded, like every other child process in this repository. A `git` call against a local
#: checkout is milliseconds; this only exists so a pathological filesystem cannot hang startup.
GIT_TIMEOUT_SECONDS = 10

#: Shown when no commit could be established. Deliberately not a guess, a placeholder SHA, a
#: version number or an empty space.
UNKNOWN_LABEL = "Build unknown"

#: Appended when tracked files differ from the named commit — the running code is then *not* that
#: commit, and saying so is the point.
MODIFIED_NOTE = "modified"

#: Appended when the commit is known but its modification state could not be measured. A third
#: answer on purpose: reporting it as clean would be a confident claim nothing verified.
UNVERIFIED_NOTE = "unverified"

REASON_NO_ROOT = "the application root is not a directory"
REASON_NO_GIT = "git is not available"
REASON_NOT_A_REPOSITORY = "the application root is not a git worktree"
REASON_FOREIGN_WORKTREE = "the git worktree found does not belong to the application root"
REASON_NO_COMMIT = "git reported no usable commit"

#: Windows-only; absent on the bare interpreters the test suite also runs on.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Every inherited variable whose name begins with this is dropped from the `git` child's
#: environment, compared case-insensitively.
#:
#: `git` reads its own behaviour out of the environment, so inheriting it hands an outsider the
#: answer. An inherited `GIT_DIR` was measured making `rev-parse HEAD` reply out of a *different*
#: repository while `--show-toplevel` still named this one — so the ownership check below passed
#: and the header stated a foreign commit with no disclosure, which is the exact lie this module
#: exists to prevent. `GIT_WORK_TREE` moves the toplevel instead (a legitimate install then reads
#: `Build unknown`), and `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY` and `GIT_INDEX_FILE` break
#: `status` so a clean tree reads `(unverified)`.
#:
#: It is a **prefix, not a list of the variables that happen to bite today**. The `GIT_CONFIG_*`
#: family is inert for these three commands in git 2.53.0 and is swept anyway: enumerating known
#: offenders leaves the next one to be discovered in production, and `git`'s environment surface
#: grows with every release. Only `-C <root>` and the checkout on disk may decide which repository
#: answers.
#:
#: The sweep is deliberately narrow in the other direction — it removes `GIT_*` and nothing else,
#: so `PATH`, `SystemRoot` and `USERPROFILE` still arrive and `git` stays findable. A nonstandard
#: install that depends on an inherited `GIT_EXEC_PATH` will answer `Build unknown` rather than a
#: wrong commit, which is the correct direction to fail in.
#:
#: The sweep removes the two settings this module sets for itself as well, so `_run_git` puts those
#: back **by name, inline where the child is spawned** rather than through a second constant: a
#: reader asking whether an unattended startup can block on a credential prompt should find the
#: answer at the call, and the pre-existing structural guard asserts it there.
GIT_ENVIRONMENT_PREFIX = "GIT_"

#: ``(ok, stdout)``. A runner never raises: a failure is ``(False, '')``.
GitRunner = Callable[[str, Tuple[str, ...]], Tuple[bool, str]]


@dataclass(frozen=True)
class BuildIdentity:
    """What this installation can honestly say about its own source revision.

    ``commit`` is the full 40-character hex id or ``''``. ``modified`` is a genuine three-state:
    ``True`` (tracked changes measured), ``False`` (measured clean) and ``None`` (not measured),
    and the third is never collapsed into the second.
    """

    commit: str = ""
    modified: Optional[bool] = None
    reason: str = ""

    @property
    def known(self) -> bool:
        return bool(self.commit)

    @property
    def short(self) -> str:
        """The abbreviation shown on screen. Empty when nothing is known."""
        return self.commit[:SHORT_LENGTH]

    def label(self) -> str:
        """The one string the header displays."""
        if not self.known:
            return UNKNOWN_LABEL
        if self.modified is True:
            return f"{self.short} ({MODIFIED_NOTE})"
        if self.modified is None:
            return f"{self.short} ({UNVERIFIED_NOTE})"
        return self.short


def _is_commit(value: str) -> bool:
    """A full object id, and nothing looser. `git` answering with prose is not a commit."""
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _same_location(left: str, right: str) -> bool:
    """Equality of two paths as LOCATIONS, resolved and case-folded.

    `--show-toplevel` answers with forward slashes on Windows, and the root arrives with
    backslashes; `realpath` normalises the separators and follows a junction or symlink, so a
    checkout reached through either spelling still compares equal. Exact equality, never a prefix:
    `C:\\apps\\BeatSync` must not be accepted as the worktree `C:\\apps`.
    """
    try:
        return (os.path.normcase(os.path.realpath(left))
                == os.path.normcase(os.path.realpath(right)))
    except (OSError, ValueError):
        return False


def _run_git(root: str, args: Tuple[str, ...]) -> Tuple[bool, str]:
    """One bounded, windowless, read-only `git` call. Never raises, never prompts.

    `-C` scopes the call to the application root rather than inheriting whatever working directory
    the process happens to have, and the child's environment inherits everything *except* `git`'s
    own variables (`GIT_ENVIRONMENT_PREFIX`), so nothing an outsider exported can change which
    repository answers. The sweep takes this module's own two settings with it, so they are put
    back immediately below — an unattended UI startup must fail cleanly instead of blocking on a
    credential prompt, and `status` must not take the index lock, which is what keeps this call
    read-only.
    """
    env = {name: value for name, value in os.environ.items()
           if not name.upper().startswith(GIT_ENVIRONMENT_PREFIX)}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(
            ("git", "-C", root) + args,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
            env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        # A missing `git` executable (`FileNotFoundError`), a timeout, or anything else the
        # platform raises. None of them is worth a traceback at startup: the build is unknown.
        return (False, "")
    return (completed.returncode == 0, (completed.stdout or "").strip())


def resolve(root: str, runner: Optional[GitRunner] = None) -> BuildIdentity:
    """Establish the running build identity for the checkout at `root`.

    Read-only and total: every failure answers with an unknown identity carrying a reason, so no
    caller has to handle an exception at startup.
    """
    run = runner or _run_git

    if not root or not os.path.isdir(root):
        return BuildIdentity(reason=REASON_NO_ROOT)

    ok, toplevel = run(root, ("rev-parse", "--show-toplevel"))
    if not ok:
        # Both "git is not installed" and "this is not a worktree" land here, and the two are
        # worth distinguishing in the reason even though the operator sees the same label.
        probe_ok, _ = run(root, ("--version",))
        return BuildIdentity(reason=REASON_NOT_A_REPOSITORY if probe_ok else REASON_NO_GIT)
    if not toplevel or not _same_location(toplevel, root):
        return BuildIdentity(reason=REASON_FOREIGN_WORKTREE)

    ok, head = run(root, ("rev-parse", "HEAD"))
    if not ok or not _is_commit(head):
        return BuildIdentity(reason=REASON_NO_COMMIT)

    # Tracked files only. Untracked content is normal here -- `bin/`, `input/` and `output/` are
    # runtime directories -- and must never be read as a modified build.
    ok, status = run(root, ("status", "--porcelain", "--untracked-files=no"))
    return BuildIdentity(commit=head.lower(), modified=bool(status) if ok else None)


#: Resolved once per application root. The identity is a property of the installation on disk, so
#: it cannot change while the process runs -- and resolving it per browser event or per render
#: would spawn a child process for a label that is already known.
_CACHE: dict = {}


def cached_identity(root: str) -> BuildIdentity:
    """`resolve`, memoised per root. The call the GUI makes once at startup."""
    key = os.path.normcase(os.path.abspath(root or ""))
    if key not in _CACHE:
        _CACHE[key] = resolve(root)
    return _CACHE[key]
