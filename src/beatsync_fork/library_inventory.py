"""Whether a file is genuinely gone, or whether the storage holding it is simply unavailable.

[FORK] Digital-Union (R14-A stage A1). This module answers exactly one question and then stops:
**what is the state of a path right now, and what evidence says so?** It is the foundation the
later Media Library inspection stages read verdicts from. It enumerates nothing, totals nothing,
parses no cache record and estimates no storage.

The reason it exists at all is one measured fact:

    os.path.exists() == False is identical for "the file was deleted" and
    "the drive is not plugged in".

Measured on this platform, all three absent cases answer ``FileNotFoundError`` with ``errno == 2``.
Only ``winerror`` differs (``2`` for an absent name, ``3`` for an absent drive letter, ``53`` for an
absent UNC share) -- and a ``winerror`` allow-list is **not** a safe gate, because a
mounted-but-removed disk and a dropped SMB session produce codes this module has never heard of
(``ERROR_NOT_READY``, ``ERROR_UNEXP_NET_ERR``, ``ERROR_NETNAME_DELETED``, and more). A reader in
this repository has already failed by trusting such a list.

So the gate is **positive evidence about the container**, and ``winerror`` is only ever allowed to
*veto*:

    A leaf file may be called missing only when its containing directory was listed
    successfully in THIS pass, the name was not in that listing, a direct lstat failed
    in a way consistent with an absent NAME rather than an absent path/device/share, and
    the storage anchor was reachable both when that listing was taken and when the pass ended.

All four, or it is not missing. The fourth clause is the mid-pass-disconnect guard: a removable
disk pulled half way through a long inspection would otherwise convert a whole subtree into
"confirmed missing" one directory at a time. Because that clause can only be settled at the *end*
of a pass, a leaf that has satisfied the first three is held in an internal provisional state and
:func:`retract_subtree` is what finalises or retracts it. :attr:`LeafVerdict.published` refuses to
show a provisional claim as a confirmation, so a consumer that reads too early sees a hold rather
than an assertion.

**Clause (d) is only worth anything if it names the right storage, the right root and the right
directory.** Three independent review findings were all failures of identification rather than of
the model:

* **F-1** -- the anchor was derived from the operator's typed path *before* resolution, so a root
  selected as ``E:\\LibraryLink`` that resolves onto ``D:\\ActualLibrary`` watched ``E:``. Measured
  on a real cross-volume junction: ``E:`` stayed reachable, ``D:`` went away, and a whole subtree
  was confirmed missing while its disk was unavailable. The anchor is now taken from the
  **resolved** location, and a root whose target cannot be established yields no trustworthy
  anchor at all rather than falling back to the typed volume -- a lexical fallback is not proof
  that anything is reachable.
* **F-2** -- finalisation read one boolean, ``RootState.anchor_reachable``, and nothing else. Any
  reachable root could settle any leaf, and a root whose own verdict was a *hold* still counted as
  "the storage is still there". Evidence now carries the identity of the root it was taken under
  (:attr:`Listing.root_key` onto :attr:`LeafVerdict.root_key`), jurisdiction is decided before
  outcome, and the outcome is read from :attr:`RootState.positively_observed` -- an established
  anchor, reachable, on a root that was actually observed.
* **F-6** -- the same failure one level down, and the one the first two corrections did not reach.
  F-1 and F-2 both measure the *root*: the root's resolved anchor, the root's identity. Neither
  asks where the **evidence** came from. A nested junction ``E:\\Library\\Link`` resolving onto
  ``D:\\OtherMedia`` is lexically a child of the root's own typed text, so ``list_directory``
  accepted it, stamped it with the root's ``root_key``, and finalisation then settled a leaf
  beneath it against ``E:`` -- truthfully reporting ``E:`` as reachable while saying nothing about
  ``D:``. Measured on a real cross-volume junction, with and without an explicit ``root=``, and
  equally on a same-volume junction escaping to a sibling, where no anchor comparison could have
  noticed. Containment is now measured on the **resolved** directory in both places: the typed
  disjunct is gone from ``list_directory``, and the resolved listed directory travels onto
  :attr:`LeafVerdict.listing_real` so :func:`_listing_belongs_to_root` can re-check it at
  finalisation -- which is what also closes the no-``root=`` path, where there was no containment
  guard at all.

So a missing-file conclusion may only ever rest on a directory that was actually inspected **and**
whose resolved location belongs to the root being inspected. Anchor, provenance and containment all
have to agree, and each of the three is now a separate, named test.

What deliberately did **not** change: a root that is itself a junction is still followed, and a
child reached through such a root is still admissible, because the root is resolved first and the
child resolves *inside* that resolved location; a root that is present but merely refused
(:data:`ACCESS_DENIED`) can still settle a listing that already succeeded, because an ACL change
does not make a deleted file undeleted; and :data:`ROOT_UNREACHABLE` still retracts.

**Verdicts travel in one direction: they may only ever be weakened.** There is no code path from
``UNKNOWN`` to ``MISSING_CONFIRMED``, and retrying an ambiguous error cannot create one. Two
independent guards enforce that in :func:`retract_subtree` -- a closed transition table, and a
clause re-check that the table cannot override.

**Read-only by construction.** Nothing here creates, deletes, renames, replaces, truncates or
opens a file. The only filesystem operations are a directory listing, a link-level status read, a
directory test and a path resolution -- and every one of them is injectable, so each branch below
is testable without a disk.

Stdlib only (``os``, ``stat``, ``dataclasses``), so CLAUDE.md's hard rule for ``beatsync_fork``
still holds: no ``logger``, no ``paths``, no ``gradio``, no ``video_analysis``, nothing
third-party. No clock and no randomness, so every verdict is a pure function of the evidence
handed to it.
"""

from __future__ import annotations

import os
import stat as stat_module
from dataclasses import dataclass, field, replace

# NOTE on why stage A2 imports nothing new (`test_j_the_module_imports_the_standard_library_only`
# pins this module's imports to exactly `{__future__, os, stat, dataclasses}`):
#
# A2 needs the authoritative scanner, the destination-name candidates and a clock. All three are
# **injected** rather than imported -- `scan`/`scan_error`/`limit_error` are *required* keyword
# arguments of `inspect_library`, so there is no default for a caller to forget and no silent hole
# where a refusal could escape unhandled. That keeps A1's own import boundary exactly where its
# review left it, and it is the same discipline `media_verify` already uses for its FFprobe.

#: POSIX ``ENOENT``. Spelled as a constant rather than imported from ``errno``, deliberately: the
#: fork package's stdlib allow-list in ``tests/test_no_runtime_dependency.py`` is a curated list
#: where each entry carries the reason it was added, and widening it is a separate, considered
#: change rather than a side effect of this stage. The value is 2 on every platform CPython
#: supports, and ``FileNotFoundError`` -- which the interpreter already maps from both ``ENOENT``
#: and ``ERROR_FILE_NOT_FOUND`` -- is the primary test in any case; this constant only catches a
#: bare ``OSError`` carrying the same code.
_ENOENT = 2

# ---------------------------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------------------------

#: The file was observed: a link-level status read succeeded and it is a regular file.
PRESENT = "present"

#: The file is genuinely not there. Requires all four clauses; never published before the
#: end-of-pass anchor check.
MISSING_CONFIRMED = "missing_confirmed"

#: The anchor -- the drive, the UNC share, the mount -- could not be reached. Says nothing
#: whatsoever about any leaf beneath it.
ROOT_UNREACHABLE = "root_unreachable"

#: The path is there as far as can be told, but we may not look.
ACCESS_DENIED = "access_denied"

#: An error this model does not positively classify, including a file that vanished between a
#: listing and its own status read. A hold, not a finding.
UNKNOWN = "unknown"

#: Never looked at: out of scope, a bound was reached, no folder was named, or the pass stopped.
NOT_INSPECTED = "not_inspected"

#: Root state only: a configured folder whose parent is live but which does not exist yet. The
#: distinction that separates "a new or mistyped folder on a live disk" from "the disk is absent".
CONFIGURED_NOT_CREATED = "configured_not_created"

#: INTERNAL. The first three clauses hold and the fourth cannot be settled until the pass ends.
#: It is deliberately not in :data:`LEAF_VERDICTS`: it is a working state, not a result, and
#: :attr:`LeafVerdict.published` renders it as ``UNKNOWN`` until :func:`retract_subtree` has run.
MISSING_PROVISIONAL = "missing_provisional"

LEAF_VERDICTS = (PRESENT, MISSING_CONFIRMED, ROOT_UNREACHABLE, ACCESS_DENIED, UNKNOWN,
                 NOT_INSPECTED)
ROOT_VERDICTS = LEAF_VERDICTS + (CONFIGURED_NOT_CREATED,)

# ---------------------------------------------------------------------------------------------
# Windows error codes -- diagnostic enrichment and a VETO, never an admission rule
# ---------------------------------------------------------------------------------------------

#: The only code that corroborates "the NAME is not in its directory".
_ERROR_FILE_NOT_FOUND = 2

_ERROR_ACCESS_DENIED = 5

#: Codes that positively mean the path, device or share could not be reached. This set is used to
#: *reject* a missing claim and to raise :data:`ROOT_UNREACHABLE`; it is never used to accept one,
#: so a code absent from it costs a hold rather than a false finding.
_UNREACHABLE_WINERRORS = frozenset({
    3,      # ERROR_PATH_NOT_FOUND          -- a directory component, not the leaf
    15,     # ERROR_INVALID_DRIVE
    21,     # ERROR_NOT_READY               -- the removable disk is mounted but gone
    53,     # ERROR_BAD_NETPATH
    54,     # ERROR_NETWORK_BUSY
    55,     # ERROR_DEV_NOT_EXIST
    59,     # ERROR_UNEXP_NET_ERR           -- a dropped SMB session
    64,     # ERROR_NETNAME_DELETED
    67,     # ERROR_BAD_NET_NAME
    433,    # ERROR_NO_SUCH_DEVICE
    1167,   # ERROR_DEVICE_NOT_CONNECTED
})

#: How one failed observation is read. Four outcomes, and `_UNRECOGNISED` is the safe default.
_AS_LEAF_ABSENT = "leaf_absent"
_AS_ACCESS_DENIED = "access_denied"
_AS_UNREACHABLE = "unreachable"
_AS_UNRECOGNISED = "unrecognised"


def classify_os_error(exc) -> str:
    """How to read one failed filesystem observation. Pure; touches nothing.

    The ordering is the whole safety argument:

    1. a refusal is a refusal -- ``PermissionError`` (or ``ERROR_ACCESS_DENIED``) first, because an
       unreadable directory must never contribute to a missing claim;
    2. then the positively-recognised unreachable codes, which **veto** a missing claim;
    3. only then "the name is not there", and only for ``ENOENT`` whose ``winerror`` is either
       absent (a non-Windows or injected observation) or exactly ``ERROR_FILE_NOT_FOUND``;
    4. anything else is unrecognised, which becomes :data:`UNKNOWN`.

    Step 3 is **not** an admission on its own. It satisfies clause (c) only, and clauses (a), (b)
    and (d) are decided elsewhere from container and anchor evidence -- which is what keeps
    ``winerror`` enrichment rather than authority.
    """
    if isinstance(exc, PermissionError):
        return _AS_ACCESS_DENIED
    win = getattr(exc, "winerror", None)
    if win == _ERROR_ACCESS_DENIED:
        return _AS_ACCESS_DENIED
    if win in _UNREACHABLE_WINERRORS:
        return _AS_UNREACHABLE
    is_enoent = isinstance(exc, FileNotFoundError) or (
        isinstance(exc, OSError) and exc.errno == _ENOENT)
    if is_enoent and win in (None, _ERROR_FILE_NOT_FOUND):
        return _AS_LEAF_ABSENT
    return _AS_UNRECOGNISED


# ---------------------------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------------------------


def normalize_root_text(value) -> str:
    """The operator's typed folder text, or ``""`` when they did not actually name a folder.

    This is the same rule as ``clips_review.normalize_folder_text``, and the test suite pins the
    two to identical answers over a shared table rather than trusting the comment. It is restated
    here rather than imported so that stage A1 carries no dependency on a 3,000-line module;
    should they ever diverge, the equivalence test fails rather than a caller silently getting the
    weaker rule.

    The emptiness test happens **before** any absolute-path resolution, which is the whole point:
    ``os.path.abspath("")`` returns the current working directory, so an unnamed root would
    otherwise quietly mean "inspect everything under the application folder". Returns ``""`` for
    anything that does not name a folder -- not a string, blank, whitespace, bare quotation marks,
    a quoted run of spaces, or an environment reference expanding to nothing.

    ``str()`` is deliberately **not** applied to a non-string: ``str(None)`` is ``"None"``, which
    resolves to a real child of the working directory and produces a confusing refusal in place of
    an accurate one.
    """
    if not isinstance(value, str):
        return ""
    trimmed = value.strip().strip('"').strip()
    if not trimmed:
        return ""
    return os.path.expandvars(os.path.expanduser(trimmed)).strip()


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_within(child: str, parent: str) -> bool:
    """``child`` is ``parent`` or lies beneath it, by ``commonpath`` rather than string prefix.

    A prefix test treats ``C:\\media-evil`` as being inside ``C:\\media``, which is exactly how a
    containment check stops meaning anything. ``ValueError`` means different drives, which is a
    definite "not inside" rather than an error.
    """
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return _norm(os.path.commonpath([c, p])) == p
    except ValueError:
        return False


def anchor_of(path: str) -> str:
    """The reachability unit a path hangs from: a drive root, or a UNC share root.

    ``os.path.splitdrive`` returns ``"C:"`` for a local path and ``"\\\\host\\share"`` for a UNC
    path, so one call covers both cases this app meets. A relative or anchorless path yields
    ``""``, and an empty anchor is treated as not reachable -- there is nothing to probe.

    **Hand this the RESOLVED path, never the typed one.** A root the operator selected as
    ``E:\\LibraryLink`` may be a junction onto ``D:\\ActualLibrary``; the storage that holds every
    file reachable through it is ``D:``, and ``E:`` can stay perfectly reachable while ``D:`` is
    gone. Deriving the anchor before resolution was finding F-1: clause (d) watched a volume that
    was never involved in storing anything, so a whole subtree could be confirmed missing while
    its disk was unavailable. Callers in this module therefore resolve first and pass ``real``.
    """
    drive, _ = os.path.splitdrive(os.path.abspath(path) if path else "")
    if not drive:
        return ""
    return drive + os.sep


def is_reparse_point(path: str, *, lstat=os.lstat) -> bool:
    """Is this entry a junction, symbolic link or other reparse point?

    Link-level, never followed. Measured previously in this repository: ``os.walk`` really does
    descend an NTFS junction, so this is a live hazard rather than a theoretical one.
    """
    try:
        attributes = lstat(path).st_file_attributes        # Windows only
    except (OSError, AttributeError):
        return False
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


# ---------------------------------------------------------------------------------------------
# evidence records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clauses:
    """The four clauses of the missing rule, recorded individually so a report can show its work.

    Clause (d) is split in two because it spans the whole pass: the anchor must have been
    reachable when the directory listing was taken **and** still be reachable when the pass ends.
    Only :func:`retract_subtree` may set :attr:`anchor_reachable_at_end`.
    """

    parent_listed: bool = False
    name_absent: bool = False
    leaf_absent_error: bool = False
    anchor_reachable_at_listing: bool = False
    anchor_reachable_at_end: bool = False

    @property
    def first_three(self) -> bool:
        """Everything decidable before the pass ends."""
        return bool(self.parent_listed and self.name_absent and self.leaf_absent_error)

    @property
    def all_four(self) -> bool:
        return bool(self.first_three
                    and self.anchor_reachable_at_listing
                    and self.anchor_reachable_at_end)

    def unmet(self) -> tuple:
        """Which clauses are not satisfied, named -- so a refusal can say why."""
        names = (("parent_listed", self.parent_listed),
                 ("name_absent", self.name_absent),
                 ("leaf_absent_error", self.leaf_absent_error),
                 ("anchor_reachable_at_listing", self.anchor_reachable_at_listing),
                 ("anchor_reachable_at_end", self.anchor_reachable_at_end))
        return tuple(name for name, held in names if not held)


@dataclass(frozen=True)
class RootState:
    """One operator-selected root, as observed. Plain strings, ints and bools -- no handles."""

    typed: str = ""
    real: str = ""
    anchor: str = ""
    verdict: str = NOT_INSPECTED
    anchor_reachable: bool = False
    exists: bool = False
    listed: bool = False
    entry_count: int = 0
    is_reparse: bool = False
    dev: int = -1
    ino: int = -1
    detail: str = ""

    @property
    def reachable(self) -> bool:
        """Clause (d)'s subject, and deliberately the ANCHOR rather than the root directory.

        A root that is reachable but refused (:data:`ACCESS_DENIED`) has not gone away, so it must
        not retract a listing that already succeeded. A root whose anchor is gone has.

        This is one boolean about one volume. It is **not** on its own authority to finalise a
        missing claim -- see :attr:`positively_observed` and :attr:`root_key`.
        """
        return bool(self.anchor_reachable)

    @property
    def root_key(self) -> str:
        """This root's identity: its own resolved location, normalised. ``""`` if unestablished.

        Derived rather than stored so that it cannot disagree with :attr:`real`, and so a
        ``RootState`` built by hand gets it for free. It is the **resolved** location, which is
        what makes two roots that merely share a drive letter -- or a nested root and its parent --
        distinguishable from one another.
        """
        return _norm(self.real) if self.real else ""

    @property
    def positively_observed(self) -> bool:
        """Is this a positive, self-consistent observation of THIS root's own storage?

        Clause (d2) needs evidence, and ``anchor_reachable`` alone is not evidence: finding F-2 was
        that a root whose own verdict is a hold (:data:`UNKNOWN`) -- the link could not be
        followed, the path would not resolve -- still carried ``anchor_reachable=True`` and was
        accepted as "the storage is still there". So all three must agree:

        * the effective storage anchor was established at all, and
        * it was reachable, and
        * the root itself was positively observed -- it listed (:data:`PRESENT`), or it is there
          and merely refused us (:data:`ACCESS_DENIED`).

        :data:`ACCESS_DENIED` is deliberately included. An ACL change at the end of a pass does not
        make a deleted file undeleted, and the storage is demonstrably still present, so treating
        it as a disconnection would manufacture a false storage-loss finding out of an ordinary
        permission change. A hold, a root that no longer exists
        (:data:`CONFIGURED_NOT_CREATED`), :data:`ROOT_UNREACHABLE` and :data:`NOT_INSPECTED` are
        all *not* positive observations.
        """
        return bool(self.anchor_reachable and self.anchor and self.root_key
                    and self.verdict in (PRESENT, ACCESS_DENIED))

    @property
    def ok(self) -> bool:
        return self.verdict == PRESENT


@dataclass(frozen=True)
class Listing:
    """The result of listing ONE directory. This is the positive evidence clause (a) needs.

    It is a single ``listdir`` of a single directory -- not a walker. Recursive traversal,
    skipped-directory counting and inventory totals belong to a later stage.

    ``names`` holds normalised-case entry names so clause (b) is a case-insensitive membership
    test, which is what a Windows filesystem means by "that name is taken".

    ``root_key`` is the identity of the inspection root this listing was taken **under**, when the
    caller named one. It is stored rather than derived because a listing of a subdirectory belongs
    to a root whose path is not the subdirectory's, and it travels onto every leaf verdict this
    listing produces so that finalisation can tell whose evidence it is holding. ``""`` means the
    caller named no root, and finalisation then falls back to containment.
    """

    directory: str = ""
    real: str = ""
    anchor: str = ""
    root_key: str = ""
    ok: bool = False
    verdict: str = NOT_INSPECTED
    names: frozenset = field(default_factory=frozenset)
    anchor_reachable: bool = False
    detail: str = ""

    def covers(self, path: str) -> bool:
        """Is this listing actually the listing of ``path``'s own parent directory?

        Without this, a caller could hand over a successful listing of some *other* directory and
        clause (a) would pass on evidence about the wrong container. Compared against both the
        typed and the resolved directory, because either may be what the caller knows.
        """
        if not self.ok or not path:
            return False
        parent = _norm(os.path.dirname(os.path.abspath(path)))
        candidates = {_norm(p) for p in (self.directory, self.real) if p}
        return parent in candidates

    def has_name(self, path: str) -> bool:
        return os.path.normcase(os.path.basename(path)) in self.names


@dataclass(frozen=True)
class LeafVerdict:
    """One leaf path's state, the evidence behind it, and whether it is final.

    :attr:`published` is the accessor every consumer must read. It exists so that a provisional
    missing finding cannot escape as a confirmation before the end-of-pass anchor check has
    happened -- reading too early yields :data:`UNKNOWN`, which is a hold.

    ``root_key`` carries the identity of the inspection root whose listing produced this verdict,
    copied from :attr:`Listing.root_key`. It is what lets :func:`retract_subtree` refuse to settle
    one root's evidence with another root's end-of-pass state.

    ``listing_real`` carries the **resolved location of the directory that was actually listed**,
    copied from :attr:`Listing.real`. It is the correction for finding F-6: ``root_key`` records
    *which root claimed* this evidence, and that is not the same question as *where the evidence
    physically came from*. A nested junction's listing can legitimately carry a root's ``root_key``
    and still have been taken on another volume entirely, so finalisation needs both -- see
    :func:`_listing_belongs_to_root`. ``""`` means the provenance was never recorded, which
    :func:`retract_subtree` treats conservatively rather than permissively.
    """

    path: str = ""
    verdict: str = NOT_INSPECTED
    clauses: Clauses = field(default_factory=Clauses)
    root_key: str = ""
    listing_real: str = ""
    is_final: bool = False
    is_regular: bool = False
    dev: int = -1
    ino: int = -1
    detail: str = ""

    @property
    def published(self) -> str:
        """What this verdict is allowed to SAY, as opposed to what it internally holds."""
        if self.verdict == MISSING_PROVISIONAL:
            return UNKNOWN
        if self.verdict == MISSING_CONFIRMED and not self.is_final:
            return UNKNOWN
        return self.verdict

    @property
    def confirmed_missing(self) -> bool:
        return self.published == MISSING_CONFIRMED


#: What :func:`may_descend` decided about one child directory.
ENTER = "enter"
SKIP_REPARSE_CONTAINED = "skip_reparse_contained"
SKIP_REPARSE_ESCAPES = "skip_reparse_escapes"
SKIP_OUTSIDE_ROOT = "skip_outside_root"


@dataclass(frozen=True)
class DescendDecision:
    child: str = ""
    action: str = SKIP_OUTSIDE_ROOT
    real: str = ""
    is_reparse: bool = False
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ENTER


# ---------------------------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------------------------


def probe_root(path, *, listdir=os.listdir, lstat=os.lstat, isdir=os.path.isdir,
               realpath=os.path.realpath) -> RootState:
    """Establish one root's state. Reads only; creates nothing, not even the folder itself.

    ``realpath`` runs **first**, so a root that is itself a junction -- a legitimate, measured
    setup when someone points a library or a cache at a bigger drive -- is followed rather than
    refused, and every later containment comparison is measured on the resolved location. That is
    the same ordering ``cache_admin._resolved_root`` uses, and for the same reason. A *nested*
    junction is a different question entirely and is :func:`may_descend`'s.

    **The anchor is taken from the resolved location, not from the typed path** -- the correction
    for finding F-1. Measured on a real cross-volume junction: a root selected as ``E:\\…\\Link``
    resolving onto ``D:\\…`` previously recorded ``E:\\`` as its anchor, so clause (d) watched a
    volume that held none of the files and ``D:`` could vanish without retracting anything. The
    anchor now answers ``D:\\``, which is the storage whose loss actually matters. A root whose
    target cannot be established yields no trustworthy anchor at all and is reported unreachable
    rather than falling back to the typed volume.

    The verdicts, and why each is the honest one:

    * no folder named                       -> ``NOT_INSPECTED`` (there was never a path)
    * the path will not resolve              -> ``UNKNOWN`` with the anchor **not** reachable, so
      no lexical fallback can pose as positive evidence about the real target
    * the anchor cannot be reached           -> ``ROOT_UNREACHABLE`` (and no leaf verdict may form)
    * a reparse point that will not resolve  -> ``UNKNOWN`` (its true location is unknown, so
      containment would be meaningless -- fail closed rather than guess). The anchor bool may well
      be ``True`` here, because the *link* sits on a live volume; that is exactly why
      :attr:`RootState.positively_observed`, not :attr:`RootState.reachable`, is what authorises a
      confirmation
    * the directory exists and lists         -> ``PRESENT``
    * the directory exists and is refused    -> ``ACCESS_DENIED``
    * absent, but its parent is live         -> ``CONFIGURED_NOT_CREATED`` -- the case that must
      never be read as "the disk is gone", and never as a reason to create anything
    * absent, parent absent too              -> ``UNKNOWN``, naming the deepest ancestor that does
      exist, because that is a useful fact and a hold rather than a finding
    """
    typed = normalize_root_text(path)
    if not typed:
        return RootState(typed="", verdict=NOT_INSPECTED,
                         detail="no folder was named, so nothing was inspected")

    absolute = os.path.abspath(typed)
    reparse = is_reparse_point(absolute, lstat=lstat)

    # Resolution comes FIRST, and the anchor is taken from the result (F-1). The typed path's own
    # volume is not the storage: a junction root on E: can hold its files on D:.
    try:
        real = realpath(absolute)
    except (OSError, ValueError) as exc:
        # The effective target was never established. The typed path's volume is a lexical
        # fallback, and a lexical fallback is not proof that the target is reachable, so this
        # cannot be positive evidence for anything.
        return RootState(typed=typed, real="", anchor=anchor_of(absolute), verdict=UNKNOWN,
                         anchor_reachable=False, is_reparse=reparse,
                         detail=f"the path could not be resolved: {exc}")

    anchor = anchor_of(real)
    try:
        anchor_reachable = bool(anchor) and bool(isdir(anchor))
    except OSError:
        anchor_reachable = False

    if reparse and _norm(real) == _norm(absolute) and is_reparse_point(real, lstat=lstat):
        # The link did not resolve to a different real location, so its target is unknown and
        # every containment comparison below would be meaningless.
        return RootState(typed=typed, real=real, anchor=anchor, verdict=UNKNOWN,
                         anchor_reachable=anchor_reachable, is_reparse=True,
                         detail="the link could not be followed to a real location")

    if not anchor_reachable:
        return RootState(typed=typed, real=real, anchor=anchor, verdict=ROOT_UNREACHABLE,
                         anchor_reachable=False, is_reparse=reparse,
                         detail=f"the drive or share {anchor or '(none)'} could not be reached")

    try:
        exists = bool(isdir(real))
    except OSError as exc:
        exists = False
        _ = exc

    if not exists:
        parent = os.path.dirname(real) or real
        try:
            parent_live = bool(isdir(parent))
        except OSError:
            parent_live = False
        if parent_live:
            return RootState(typed=typed, real=real, anchor=anchor,
                             verdict=CONFIGURED_NOT_CREATED, anchor_reachable=True,
                             is_reparse=reparse,
                             detail="the folder does not exist yet, but its parent is there; "
                                    "nothing was created")
        return RootState(typed=typed, real=real, anchor=anchor, verdict=UNKNOWN,
                         anchor_reachable=True, is_reparse=reparse,
                         detail=f"neither the folder nor its parent {parent} is there")

    dev = ino = -1
    try:
        info = lstat(real)
        dev, ino = int(info.st_dev), int(info.st_ino)
    except OSError:
        dev = ino = -1

    try:
        entries = listdir(real)
    except OSError as exc:
        reading = classify_os_error(exc)
        if reading == _AS_ACCESS_DENIED:
            return RootState(typed=typed, real=real, anchor=anchor, verdict=ACCESS_DENIED,
                             anchor_reachable=True, exists=True, is_reparse=reparse,
                             dev=dev, ino=ino,
                             detail="the folder is there but could not be read")
        if reading == _AS_UNREACHABLE:
            return RootState(typed=typed, real=real, anchor=anchor, verdict=ROOT_UNREACHABLE,
                             anchor_reachable=False, exists=True, is_reparse=reparse,
                             dev=dev, ino=ino,
                             detail=f"the folder became unreachable while being read: {exc}")
        return RootState(typed=typed, real=real, anchor=anchor, verdict=UNKNOWN,
                         anchor_reachable=True, exists=True, is_reparse=reparse,
                         dev=dev, ino=ino, detail=f"the folder could not be read: {exc}")

    return RootState(typed=typed, real=real, anchor=anchor, verdict=PRESENT,
                     anchor_reachable=True, exists=True, listed=True,
                     entry_count=len(list(entries)), is_reparse=reparse, dev=dev, ino=ino,
                     detail="")


def list_directory(directory, *, root=None, listdir=os.listdir, isdir=os.path.isdir,
                   realpath=os.path.realpath) -> Listing:
    """List ONE directory and record the result as clause (a) evidence. Not a walker.

    An empty but readable directory is ``ok=True`` with an empty ``names`` set, and that is the
    whole point: "there is nothing in it" and "I could not look in it" are different facts, and a
    missing claim may only rest on the first.

    Like :func:`probe_root`, the anchor is taken from the **resolved** directory, so clause (d1)
    names the storage the entries actually came off rather than the volume the path text sits on.

    ``root`` is the :class:`RootState` this listing is being taken under, and naming it is what
    gives the resulting leaf verdicts a provenance a later :func:`retract_subtree` can check. It is
    optional so that a caller inspecting a single directory need not invent a root -- but a pass
    that walks several roots **should** pass it, because without it finalisation can only fall back
    to containment, and containment cannot tell a nested root from its parent. A directory that
    does not lie under the root it is offered for is refused outright rather than silently
    recorded as that root's evidence.

    **Containment is measured on the RESOLVED directory, never on the typed path** -- the
    correction for finding F-6. The refusal above previously accepted *either* resolved containment
    **or** typed-path containment, and the typed path is by definition unresolved: a nested
    junction ``<root>\\Link`` resolving onto another volume is lexically a child of the root's own
    text, so it passed, was stamped with the root's identity, and a leaf beneath it was then
    settled against the root's storage rather than its own. Measured on a real cross-volume
    junction: the listing correctly recorded the far volume as its anchor, that anchor was
    discarded, and the leaf published :data:`MISSING_CONFIRMED` saying "the storage holding it was
    still there" while that storage was gone. The same held for a same-volume junction escaping to
    a sibling, where no anchor comparison could have noticed. So the typed disjunct is gone, and
    nothing reintroduces it -- the legitimate selected-junction-root cases never needed it, because
    :func:`probe_root` resolves the root first and a child reached through the link resolves
    *inside* ``state.real``.
    """
    typed = normalize_root_text(directory)
    if not typed:
        return Listing(verdict=NOT_INSPECTED, detail="no folder was named")
    absolute = os.path.abspath(typed)
    try:
        real = realpath(absolute)
    except (OSError, ValueError) as exc:
        return Listing(directory=absolute, anchor=anchor_of(absolute), verdict=UNKNOWN,
                       anchor_reachable=False,
                       detail=f"the path could not be resolved: {exc}")
    anchor = anchor_of(real)
    try:
        anchor_reachable = bool(anchor) and bool(isdir(anchor))
    except OSError:
        anchor_reachable = False

    root_key = ""
    if root is not None:
        state = root if isinstance(root, RootState) else RootState()
        root_key = state.root_key
        if not root_key:
            return Listing(directory=absolute, real=real, anchor=anchor, verdict=NOT_INSPECTED,
                           anchor_reachable=anchor_reachable,
                           detail="the root has no established location, so this folder cannot "
                                  "be recorded as evidence for it")
        if not is_within(real, state.real):
            return Listing(directory=absolute, real=real, anchor=anchor, root_key=root_key,
                           verdict=NOT_INSPECTED, anchor_reachable=anchor_reachable,
                           detail="this folder's real location does not lie under the root it "
                                  "was offered as evidence for")

    try:
        entries = listdir(real)
    except OSError as exc:
        reading = classify_os_error(exc)
        verdict = {_AS_ACCESS_DENIED: ACCESS_DENIED,
                   _AS_UNREACHABLE: ROOT_UNREACHABLE}.get(reading, UNKNOWN)
        return Listing(directory=absolute, real=real, anchor=anchor, root_key=root_key,
                       ok=False, verdict=verdict,
                       anchor_reachable=anchor_reachable and verdict != ROOT_UNREACHABLE,
                       detail=f"the folder could not be listed: {exc}")
    names = frozenset(os.path.normcase(str(name)) for name in entries)
    return Listing(directory=absolute, real=real, anchor=anchor, root_key=root_key, ok=True,
                   verdict=PRESENT, names=names, anchor_reachable=anchor_reachable, detail="")


def classify_leaf(path, listing, *, lstat=os.lstat) -> LeafVerdict:
    """Classify ONE leaf path against a listing of its own parent directory.

    A successful status read is a **positive observation** and wins outright: the file is there,
    whatever a listing taken a moment earlier said. ``PRESENT`` additionally requires a regular
    file, per the design; something that exists but is of another kind is ``UNKNOWN`` -- it is
    certainly not missing, and "wrong kind of entry" is a later stage's vocabulary, not a verdict
    this model should invent.

    A failed read is where the care goes. Only the "absent name" reading can begin a missing
    claim, and even then the claim is :data:`MISSING_PROVISIONAL` and depends on:

    * clause (a) -- the listing succeeded **and** is genuinely this path's parent
      (:meth:`Listing.covers`), so evidence about some other directory cannot be borrowed;
    * clause (b) -- the name was not among the listed entries. This is the clause that catches the
      vanished-mid-pass race: if the listing *did* show the name and the read then failed, the
      file disappeared underneath us, the listing is stale, and the honest answer is ``UNKNOWN``;
    * clause (d1) -- the anchor was reachable when that listing was taken.

    Clause (d2) is unsettled here by construction, so the result is never final.
    """
    text = normalize_root_text(path)
    if not text:
        return LeafVerdict(path="", verdict=NOT_INSPECTED, is_final=True,
                           detail="no file was named, so nothing was inspected")
    absolute = os.path.abspath(text)
    evidence = listing if isinstance(listing, Listing) else Listing()

    # The root this evidence belongs to travels with the verdict, so that the end-of-pass check
    # can establish it is holding the right root's evidence rather than assuming it. The resolved
    # location the listing was actually taken on travels with it too (F-6): the root's claim and
    # the evidence's physical origin are two different facts, and clause (d2) needs both.
    origin = evidence.root_key
    origin_real = evidence.real

    try:
        info = lstat(absolute)
    except OSError as exc:
        reading = classify_os_error(exc)
        clauses = Clauses(
            parent_listed=evidence.covers(absolute),
            name_absent=evidence.covers(absolute) and not evidence.has_name(absolute),
            leaf_absent_error=(reading == _AS_LEAF_ABSENT),
            anchor_reachable_at_listing=bool(evidence.anchor_reachable),
        )
        if reading == _AS_ACCESS_DENIED:
            return LeafVerdict(path=absolute, verdict=ACCESS_DENIED, clauses=clauses,
                               root_key=origin, listing_real=origin_real, is_final=True,
                               detail="the file could not be read")
        if reading == _AS_UNREACHABLE:
            return LeafVerdict(path=absolute, verdict=ROOT_UNREACHABLE, clauses=clauses,
                               root_key=origin, listing_real=origin_real, is_final=True,
                               detail="the drive, share or device could not be reached")
        if reading == _AS_LEAF_ABSENT and clauses.first_three and \
                clauses.anchor_reachable_at_listing:
            return LeafVerdict(
                path=absolute, verdict=MISSING_PROVISIONAL, clauses=clauses, root_key=origin,
                listing_real=origin_real, is_final=False,
                detail="the parent folder was read and this name was not in it; awaiting the "
                       "end-of-pass check that the drive is still there")
        return LeafVerdict(
            path=absolute, verdict=UNKNOWN, clauses=clauses, root_key=origin,
            listing_real=origin_real, is_final=True,
            detail="not enough evidence to say the file is missing; unmet clause(s): "
                   + (", ".join(clauses.unmet()) or "none"))

    regular = bool(stat_module.S_ISREG(info.st_mode))
    dev = int(getattr(info, "st_dev", -1))
    ino = int(getattr(info, "st_ino", -1))
    observed = Clauses(parent_listed=evidence.covers(absolute),
                       anchor_reachable_at_listing=bool(evidence.anchor_reachable))
    if regular:
        return LeafVerdict(path=absolute, verdict=PRESENT, clauses=observed, root_key=origin,
                           listing_real=origin_real, is_final=True, is_regular=True, dev=dev,
                           ino=ino, detail="")
    return LeafVerdict(path=absolute, verdict=UNKNOWN, clauses=observed, root_key=origin,
                       listing_real=origin_real, is_final=True, is_regular=False, dev=dev,
                       ino=ino, detail="it is there, but it is not a regular file")


def may_descend(root_state, child, *, lstat=os.lstat, realpath=os.path.realpath
                ) -> DescendDecision:
    """May an inspection enter this child directory? Decides; it does not traverse.

    Two reparse cases are deliberately distinguished even though **both are skipped**:

    * the child resolves to somewhere still inside the root -> ``SKIP_REPARSE_CONTAINED``. Skipped
      because entering it would count the same files twice, not because it is dangerous.
    * the child resolves outside the root -> ``SKIP_REPARSE_ESCAPES``. This is the hazard: ``os.walk``
      descends an NTFS junction, which would turn "inspect this library" into "inspect whatever
      that points at".

    Neither is ever entered, which matches ``cache_admin._walk_contained``'s stricter behaviour --
    and a root that is *itself* a junction is a separate, legitimate case already resolved by
    :func:`probe_root`, so containment here is measured against the root's **real** location.
    """
    state = root_state if isinstance(root_state, RootState) else RootState()
    text = normalize_root_text(child)
    if not text:
        return DescendDecision(child="", action=SKIP_OUTSIDE_ROOT,
                               detail="no folder was named")
    absolute = os.path.abspath(text)
    base = state.real or state.typed
    if not base:
        return DescendDecision(child=absolute, action=SKIP_OUTSIDE_ROOT,
                               detail="the root has no established location")

    reparse = is_reparse_point(absolute, lstat=lstat)
    try:
        real = realpath(absolute)
    except (OSError, ValueError) as exc:
        return DescendDecision(child=absolute, action=SKIP_OUTSIDE_ROOT, is_reparse=reparse,
                               detail=f"the child could not be resolved: {exc}")

    if reparse:
        if is_within(real, base):
            return DescendDecision(
                child=absolute, action=SKIP_REPARSE_CONTAINED, real=real, is_reparse=True,
                detail="a junction or link inside the folder - reported, not followed")
        return DescendDecision(
            child=absolute, action=SKIP_REPARSE_ESCAPES, real=real, is_reparse=True,
            detail="a junction or link pointing outside the folder - reported, not followed")

    if not is_within(real, base):
        return DescendDecision(child=absolute, action=SKIP_OUTSIDE_ROOT, real=real,
                               detail="outside the folder being inspected")
    return DescendDecision(child=absolute, action=ENTER, real=real, detail="")


# ---------------------------------------------------------------------------------------------
# finalising: clause (d2), and the one direction of travel
# ---------------------------------------------------------------------------------------------

#: The ONLY transitions :func:`retract_subtree` may make, keyed by ``(verdict, anchor_reachable)``.
#: A pair absent from this table leaves its verdict exactly as it was. It is a module-level
#: mapping so a test can mutate it and prove the second guard below is load-bearing.
_FINALISATIONS = {
    (MISSING_PROVISIONAL, True): MISSING_CONFIRMED,
    (MISSING_PROVISIONAL, False): UNKNOWN,
    (MISSING_CONFIRMED, False): UNKNOWN,
}


def _same_inspection_root(state, item) -> bool:
    """Is this ``RootState`` about the very root whose listing produced this leaf's evidence?

    This is **jurisdiction**, and it is a separate question from whether the root was reachable.
    Finding F-2 was that the two were conflated: any reachable root could settle any leaf, so a
    library on a fixed disk could confirm deletions for a rejected folder on a removable one.

    It is the clause-(d) counterpart of :meth:`Listing.covers`, which already protects clause (a)
    for exactly the same reason -- evidence about one container must not be borrowed for another.

    Two levels, because A1 is consumed both ways:

    * **Explicit** -- the evidence names the root it was taken under
      (:attr:`Listing.root_key`, carried onto the verdict). The comparison is then exact, on
      *resolved* locations, which is what makes a nested root distinguishable from its parent and
      makes two roots that merely share a drive letter distinguishable from one another. The
      effective anchors are compared too, so a junction re-pointed onto different storage
      mid-pass is a mismatch rather than a silent substitution.
    * **Fallback** -- no root was named, so the strongest honest test is that the leaf lies under
      the root's own established location. Both the resolved and the typed location are tried,
      because a junction root legitimately has leaves addressed through the typed path while its
      resolved location is on another volume.

    This answers **whose** evidence it is, and nothing else. Whether the directory that produced it
    actually belongs to this root's storage is the separate question
    :func:`_listing_belongs_to_root` asks, and the two are deliberately not merged: a mismatch here
    means *another root's business*, which is returned untouched, while a mismatch there means
    *this root's label on a directory that is not under it*, which is a contradiction and is failed
    closed.
    """
    if item.root_key:
        if not state.root_key or item.root_key != state.root_key:
            return False
        return _norm(anchor_of(item.root_key)) == _norm(anchor_of(state.root_key))
    if not item.path:
        return False
    return bool((state.real and is_within(item.path, state.real))
                or (state.typed and is_within(item.path, state.typed)))


def _listing_belongs_to_root(state, item) -> bool:
    """Did the directory that produced this verdict actually lie under THIS root's real location?

    This is finding F-6, and it is a different question from
    :func:`_same_inspection_root`. ``root_key`` records which root *claimed* the evidence;
    :attr:`LeafVerdict.listing_real` records where the evidence was physically *taken*. A nested
    junction made those two disagree: ``<root>\\Link`` is lexically a child of the root, so it was
    stamped with the root's identity, while its listing had actually been taken on another volume.
    Finalisation then settled the leaf against the root's storage -- which was fine -- and said
    nothing at all about the storage that held the file.

    So the comparison is on the **resolved** listed directory against the root's **resolved**
    location. That one test is also the storage-anchor test, and deliberately not a second,
    separate one: :func:`is_within` compares through ``os.path.commonpath``, which raises
    ``ValueError`` across different drives or shares, so containment cannot succeed unless the
    effective anchors already agree. A drive-letter comparison on its own would **not** be
    sufficient -- a same-volume junction escaping to a sibling has an identical anchor and was
    measured to produce exactly the same false confirmation.

    **Missing provenance fails closed for a missing claim.** ``listing_real`` is empty only on a
    verdict that was built by hand rather than produced by :func:`classify_leaf` from a real
    :class:`Listing`, so for :data:`MISSING_PROVISIONAL` and :data:`MISSING_CONFIRMED` an absent
    origin is refused. Every other verdict is unaffected: :data:`PRESENT`, :data:`ACCESS_DENIED`,
    :data:`ROOT_UNREACHABLE`, :data:`UNKNOWN` and :data:`NOT_INSPECTED` assert nothing that this
    guard protects, and holding them hostage to a provenance field would break ordinary
    pass-through for no safety gain.

    A root with no established location of its own cannot answer the question at all, and
    :attr:`RootState.positively_observed` already refuses to confirm anything in that case, so this
    returns ``True`` and leaves the refusal where it belongs.
    """
    if not state.real:
        return True
    if item.listing_real:
        return is_within(item.listing_real, state.real)
    return item.verdict not in (MISSING_PROVISIONAL, MISSING_CONFIRMED)


def retract_subtree(verdicts, root_state):
    """Settle clause (d2) for a whole pass. Returns a new tuple; mutates nothing.

    This is the only place a missing finding becomes final, and the only place one is taken back:

    * the anchor is still reachable -> a provisional finding whose four clauses now all hold
      becomes :data:`MISSING_CONFIRMED`;
    * the anchor has gone -> every provisional finding **and** every already-confirmed one becomes
      :data:`UNKNOWN`. That is the mid-pass-disconnect guard: a disk pulled during a long
      inspection must not leave behind a subtree of confirmed deletions.

    **Jurisdiction is decided before outcome.** A verdict whose evidence came from a *different*
    inspection root is not this call's business at all: it is returned untouched, neither confirmed
    nor retracted, so that the pass over its own root can still settle it correctly. Destroying it
    here would be as wrong as confirming it. See :func:`_same_inspection_root`.

    **And containment is decided before outcome too** -- finding F-6. Jurisdiction establishes
    *whose* evidence this is; it does not establish that the directory it came from is under this
    root. A nested junction could satisfy the first and fail the second, and the leaf was then
    settled against storage that had never held it. :func:`_listing_belongs_to_root` is that second
    test, on the resolved listed directory. A failure there is **not** another root's business, so
    it is not returned untouched: it is a contradiction inside this root's own pass and is failed
    closed to :data:`UNKNOWN`.

    **Reachability is read from the root's whole observation, not one boolean.** Clause (d2) is
    settled from :attr:`RootState.positively_observed`, which requires an established anchor, that
    anchor reachable, *and* the root positively observed. ``anchor_reachable=True`` on a root whose
    own verdict is a hold is not evidence that the storage is there -- that was finding F-2's
    second half.

    **Two independent guards keep the direction of travel.** The closed table above is the first.
    The second is the clause re-check below, which refuses to emit a confirmation whose four
    clauses do not all hold -- so even a wrong table cannot manufacture one, and an ``UNKNOWN``
    cannot be promoted by retrying an ambiguous error. Verdicts this function does not recognise
    are returned untouched rather than guessed at.
    """
    state = root_state if isinstance(root_state, RootState) else RootState()
    observed = bool(state.positively_observed)
    settled = []
    for item in verdicts:
        if not isinstance(item, LeafVerdict):
            settled.append(item)
            continue
        if not _same_inspection_root(state, item):
            # Another root's evidence. Not ours to confirm, and not ours to retract either.
            settled.append(item)
            continue
        if not _listing_belongs_to_root(state, item):
            # F-6. This root's own label on a directory that does not lie under it -- a nested
            # junction's listing taken on other storage, or a missing claim with no recorded
            # origin at all. That is not another root's jurisdiction, it is a contradiction
            # inside this one, so it is failed closed rather than returned as-is. The end-of-pass
            # clause is recorded as unmet because the storage that actually held the file was
            # never the storage this pass checked.
            settled.append(replace(
                item, verdict=UNKNOWN,
                clauses=replace(item.clauses, anchor_reachable_at_end=False), is_final=True,
                detail="refused: the folder this was read from does not lie under the folder "
                       "being inspected, so its storage was never checked and nothing can be "
                       "said about this file"))
            continue
        clauses = replace(item.clauses, anchor_reachable_at_end=observed)
        outcome = _FINALISATIONS.get((item.verdict, observed), item.verdict)

        # The second, independent guard. A confirmation is only ever emitted on the evidence
        # itself, never on the strength of the lookup above.
        if outcome == MISSING_CONFIRMED and not clauses.all_four:
            outcome = UNKNOWN

        if outcome == item.verdict and item.verdict != MISSING_PROVISIONAL:
            settled.append(replace(item, clauses=clauses, is_final=True))
            continue
        if outcome == MISSING_CONFIRMED:
            detail = "the parent folder was read, this name was not in it, and the storage "\
                     "holding it was still there when the inspection finished"
        elif item.verdict in (MISSING_PROVISIONAL, MISSING_CONFIRMED):
            detail = ("retracted: the drive or share was not reachable when the inspection "
                      "finished, so nothing can be said about this file"
                      if not state.anchor_reachable else
                      "retracted: the folder's own state could not be positively confirmed when "
                      "the inspection finished, so nothing can be said about this file")
        else:
            detail = item.detail
        settled.append(replace(item, verdict=outcome, clauses=clauses, is_final=True,
                               detail=detail))
    return tuple(settled)


def confirmed_missing(verdicts) -> tuple:
    """The finalised, published, genuinely-missing paths -- and nothing provisional."""
    return tuple(item.path for item in verdicts
                 if isinstance(item, LeafVerdict) and item.confirmed_missing)


def summarise(verdicts) -> dict:
    """Counts per published verdict. A read-out helper; it decides nothing."""
    counts = {name: 0 for name in LEAF_VERDICTS}
    for item in verdicts:
        if isinstance(item, LeafVerdict):
            counts[item.published] = counts.get(item.published, 0) + 1
    return counts


# =============================================================================================
# stage A2: the bounded, read-only media inventory
# =============================================================================================
#
# A2 adds counting on top of A1's verdicts, and it adds **no second scanner**. Tier A is
# ``input_manager.scan_folder`` and nothing else; that function owns traversal, containment, the
# per-entry accounting and the all-or-nothing contract, and it is injected here rather than
# reimplemented. Tier B is a bounded *diagnostic* walk built out of A1's own ``list_directory`` and
# ``may_descend``, it runs only after Tier A has already failed, and it produces lower bounds that
# are never summed into a total.
#
# The one rule the whole stage exists to keep: **a figure is either a total or it is labelled as not
# one.** There is no third state and no way to read a short count as a complete one -- see
# ``RootInventory.totals``.

#: Entries one root's Tier A scan may classify before it refuses. The owner-approved value.
#:
#: This is a **classification-entry ceiling, not a wall-clock or peak-memory limit**, and no report
#: built on it may say otherwise. ``input_manager.scan_folder`` documents the measured floor the
#: ceiling cannot reach below (one directory is enumerated whole before its first entry is yielded;
#: one ``lstat`` runs per child directory before any of that directory's files appear).
#:
#: It is sized as a runaway guard rather than a responsiveness budget -- the failure it exists to
#: catch is a drive root or a system folder being handed in. A ceiling tuned for latency would
#: refuse a legitimately large library, and a refused root yields **no** total at all, which is the
#: worse outcome for a panel whose entire purpose is a trustworthy total.
MAX_INVENTORY_ENTRIES_PER_ROOT = 200_000

#: Tier B's own bounds. Diagnostics must not become the unbounded walk Tier A refused to be.
#:
#: What each one bounds is load-bearing, and both were corrected after review findings F-1 and F-2
#: measured them bounding something other than the work:
#:
#: * :data:`MAX_DIAGNOSTIC_DIRECTORIES` bounds directories **attempted**. A listing that fails costs
#:   the same ``listdir`` as one that succeeds, so charging only the successes bounded nothing --
#:   measured, 2000 unlistable children were every one of them attempted against a ceiling of 3.
#: * :data:`MAX_DIAGNOSTIC_ENTRIES` bounds directory entries **examined**, before the
#:   media-extension filter, exactly as Tier A's ceiling does. Charging only supported files left a
#:   directory of unsupported entries entirely unbounded -- measured, 3000 ``.txt`` files walked
#:   against a ceiling of 1 that could never fire.
#:
#: Neither is a wall-clock or peak-memory limit, and no report built on them may say otherwise:
#: ``listdir`` still materialises one directory whole before its first entry is examined, so the
#: irreducible floor is the size of the largest single directory. These bound the *walk*.
MAX_DIAGNOSTIC_DIRECTORIES = 2_000
MAX_DIAGNOSTIC_ENTRIES = 50_000

#: How many paths each diagnostic name list collects before it stops collecting. The two ceilings
#: above bound the walk; this bounds the **record** of it, which is a separate problem: 2000 named
#: folders is not a read-out an operator can act on, and the names are what Tier B exists to
#: produce. A list that stopped collecting is never presented as a complete list of folders to fix
#: -- see :attr:`Diagnostics.names_truncated`.
MAX_DIAGNOSTIC_NAMES = 200

#: How many alternative names a destination forecast will offer per conflict. Advisory only.
MAX_FORECAST_ALTERNATIVES = 3

#: Role names, matching ``user_prefs`` keys. Roles are **handed in**, never discovered: this module
#: reads no preference file, expands no wildcard and enumerates no drive.
LIBRARY_ROLE = "clips_library"
REJECTED_ROLE = "clips_rejected"

#: Inventory completeness. Deliberately a separate axis from A1's root verdicts: a root can be
#: perfectly :data:`PRESENT` and still yield no total, because the scan of it was refused.
INVENTORY_COMPLETE = "inventory_complete"
INVENTORY_INCOMPLETE = "inventory_incomplete"

#: Why an inventory is not complete. Reported, never inferred.
REASON_NONE = ""
REASON_ROOT_NOT_USABLE = "root_not_usable"
REASON_LIMIT_EXCEEDED = "limit_exceeded"
REASON_SCAN_REFUSED = "scan_refused"
REASON_ROOT_LOST = "root_lost_during_inspection"
REASON_STOPPED = "stopped_by_request"
REASON_NOT_REQUESTED = "not_requested"

INVENTORY_REASONS = (REASON_NONE, REASON_ROOT_NOT_USABLE, REASON_LIMIT_EXCEEDED,
                     REASON_SCAN_REFUSED, REASON_ROOT_LOST, REASON_STOPPED,
                     REASON_NOT_REQUESTED)


@dataclass(frozen=True)
class MediaCounts:
    """One root's media accounting, carried across from :class:`input_manager.InputSet` verbatim.

    Nothing is recomputed here and no figure is derived from a second traversal: every number comes
    off the one authoritative scan, and :attr:`reconciles` re-asserts that scanner's own counting
    invariant so a transcription slip in this module cannot pass unnoticed.

    ``ready_bytes`` is the sum of sizes the scanner already ``stat``-ed. **No file was opened** to
    produce any field here.
    """

    discovered: int = 0
    ready: int = 0
    ready_bytes: int = 0
    path_collisions: int = 0
    unsupported: int = 0
    empty: int = 0
    unreadable: int = 0
    not_a_file: int = 0

    @property
    def rejected(self) -> int:
        return self.unsupported + self.empty + self.unreadable + self.not_a_file

    @property
    def reconciles(self) -> bool:
        """``discovered == ready + rejected + path_collisions`` -- the scanner's own invariant."""
        return self.discovered == self.ready + self.rejected + self.path_collisions


@dataclass(frozen=True)
class BasenameGroup:
    """Several files whose **file names** collide, case-insensitively.

    This says nothing whatever about their contents. Two files called ``clip.mp4`` in different
    folders collide on *destination*, which is the operator's actual problem; whether their bytes
    match is a different question that would cost a read per file to answer, and the default
    inventory reads nothing.
    """

    name: str = ""
    """The collision key: the basename, normalised case."""

    display: str = ""
    """The first spelling seen, for a human-readable read-out."""

    paths: tuple = ()

    @property
    def count(self) -> int:
        return len(self.paths)

    @property
    def extra_copies(self) -> int:
        return max(0, len(self.paths) - 1)

    @property
    def contents_compared(self) -> bool:
        """Always ``False``. Present so a consumer must acknowledge it rather than assume."""
        return False


@dataclass(frozen=True)
class NameConflict:
    """One basename that a publish into the destination would have to resolve."""

    basename: str = ""
    occupied: bool = False
    """The destination already holds this name, as observed in the single listing taken."""

    sources: tuple = ()
    """Library paths competing for this name."""

    alternatives: tuple = ()
    """Names :func:`collision.candidate_names` would try next, filtered against that same single
    listing. Advisory: the destination may change before anything is ever published."""


@dataclass(frozen=True)
class DestinationForecast:
    """What a publish *would* collide with, as observed once, at one moment.

    A **nonbinding snapshot**. It predicts; it reserves nothing, creates nothing and moves nothing.
    When the destination could not be observed, :attr:`known` is ``False`` and :attr:`conflicts` is
    empty *because nothing was looked at* -- which is why a consumer must branch on :attr:`known`
    rather than reading an empty tuple as "no conflicts".
    """

    destination: str = ""
    state: str = NOT_INSPECTED
    observed: bool = False
    existing_entries: int = 0
    conflicts: tuple = ()
    detail: str = ""

    @property
    def known(self) -> bool:
        return bool(self.observed) and self.state == PRESENT

    @property
    def occupied_names(self) -> tuple:
        return tuple(item.basename for item in self.conflicts if item.occupied)

    @property
    def binding(self) -> bool:
        """Always ``False``. A forecast is never a reservation."""
        return False


@dataclass(frozen=True)
class Diagnostics:
    """Tier B. Diagnostics after a Tier A failure, and **never** an inventory.

    Every count here is a **lower bound**: the walk is bounded, it refuses whole subtrees it cannot
    prove safe, and it stops at its own ceilings. :attr:`complete` says whether it even finished.
    None of these numbers may be added to, substituted for, or presented as a total.
    """

    ran: bool = False
    complete: bool = False
    directories_listed: int = 0
    files_seen: int = 0
    bytes_seen: int = 0
    unreadable_directories: tuple = ()
    skipped_links: tuple = ()
    outside_root: tuple = ()
    bound_reached: str = ""
    detail: str = ""

    directories_attempted: int = 0
    """Directories a ``listdir`` was spent on, successful or not -- what the ceiling charges.

    :attr:`directories_listed` stays the count of *successful* listings, deliberately: one is the
    cost that had to be bounded, the other is the coverage achieved, and review finding F-1 was
    exactly the two being conflated."""

    entries_examined: int = 0
    """Directory entries considered, before any extension filter -- what the entry ceiling charges.

    :attr:`files_seen` and :attr:`bytes_seen` still mean supported media, which is the figure an
    operator wants; this is the figure a bound needs."""

    undecidable_links: tuple = ()
    """Child directories whose link status could not be established, and which were therefore not
    entered.

    A distinct category from :attr:`skipped_links` on purpose: that tuple names entries positively
    identified as a junction or link, and calling an entry a junction on the strength of a failed
    metadata read would be inventing evidence. These were refused because nothing was known, which
    is a different thing to report and a different thing to fix."""

    names_truncated: bool = False
    """A name list stopped collecting at :data:`MAX_DIAGNOSTIC_NAMES`, so it is not exhaustive."""

    @property
    def is_lower_bound(self) -> bool:
        """``True``: no figure here may be read as a total, an exact count or an upper bound.

        It is unconditional, and review finding F-3 is why it is now also *accurate*. Previously a
        contained junction whose link metadata could not be read was **entered**, so its subtree was
        walked twice and :attr:`files_seen` reported six files where three existed -- an
        over-count, which is the one thing a lower bound may not be. Descent now additionally
        requires link-level evidence that the child is an ordinary folder, so a child that cannot be
        proven is refused and named in :attr:`undecidable_links`. Refusing can only put these
        figures further below the truth, never above it, which is what makes the guarantee hold.
        """
        return True


@dataclass(frozen=True)
class RootInventory:
    """One root's result. Read :attr:`totals`, not :attr:`counts`, unless you mean it."""

    role: str = ""
    requested: str = ""
    root: RootState = field(default_factory=RootState)
    completeness: str = NOT_INSPECTED
    reason: str = REASON_NOT_REQUESTED
    counts: MediaCounts | None = None
    duplicate_basenames: tuple = ()
    destination: DestinationForecast | None = None
    diagnostics: Diagnostics | None = None
    limit: int | None = None
    counted_at_least: int = 0
    """A **lower bound** when the scan was refused for a limit. Never a total, and ``0`` otherwise."""

    detail: str = ""

    @property
    def authoritative(self) -> bool:
        return self.completeness == INVENTORY_COMPLETE and self.counts is not None

    @property
    def totals(self) -> MediaCounts | None:
        """The counts **only** when they are genuinely totals; ``None`` otherwise.

        This accessor is the structural half of "no partial result may be presented as a complete
        total". :attr:`counts` is kept so a diagnostic read-out can still say what was seen, but a
        consumer asking for totals cannot be handed a short count by accident.
        """
        return self.counts if self.authoritative else None


@dataclass(frozen=True)
class LibraryInventory:
    """The whole pass. One entry per root offered, in the order they were offered."""

    roots: tuple = ()
    stopped: bool = False

    observed_at: float = 0.0
    """When the pass **began**, from the injected clock; ``0.0`` if none was supplied.

    The start, not the end, and deliberately one timestamp for the whole pass rather than one per
    figure: the filesystem can change under a running inspection, so every finding here describes a
    window beginning at this instant and nothing is claimed to still be true afterwards."""

    suffixes: tuple = ()
    limit: int | None = None

    @property
    def complete(self) -> bool:
        """Every root inspected produced a total, and the pass was not stopped."""
        return (not self.stopped) and bool(self.roots) and all(
            item.authoritative for item in self.roots)

    @property
    def incomplete_roots(self) -> tuple:
        return tuple(item for item in self.roots if not item.authoritative)


def _basename_key(path: str) -> str:
    return os.path.normcase(os.path.basename(str(path or "")))


def duplicate_basenames(paths) -> tuple:
    """Group paths whose basenames collide case-insensitively. Reads nothing; opens nothing.

    Only groups of two or more are returned, ordered by key, and each group's paths keep the order
    they arrived in -- which is the scanner's deterministic ``order_key`` order when the caller
    passes ``InputSet.paths``.
    """
    buckets: dict = {}
    for path in paths or ():
        text = str(path or "")
        if not text:
            continue
        key = _basename_key(text)
        if not key:
            continue
        if key not in buckets:
            buckets[key] = [os.path.basename(text), []]
        buckets[key][1].append(text)
    groups = [
        BasenameGroup(name=key, display=display, paths=tuple(items))
        for key, (display, items) in buckets.items()
        if len(items) > 1
    ]
    groups.sort(key=lambda group: group.name)
    return tuple(groups)


def forecast_destination(destination, basenames, *, candidate_names=None, probe=None,
                         listdir=os.listdir, lstat=os.lstat, isdir=os.path.isdir,
                         realpath=os.path.realpath) -> DestinationForecast:
    """Predict what publishing ``basenames`` into ``destination`` would collide with.

    **One authoritative destination-name snapshot for the forecast, preceded by a root-state
    probe** -- and never once per candidate name, which is the cost this contract exists to rule
    out. It never creates the destination: :func:`probe_root` establishes its state without
    ``makedirs`` (that is one ``listdir``), and one :func:`list_directory` then supplies the names
    (that is the second, and the snapshot).

    The prediction derives **solely from the listing**. The probe's own ``entry_count`` is
    discarded, no figure here blends the two reads, and a destination that changes between them is
    reported exactly as the listing found it -- so a name that vanished after the probe is not
    claimed occupied, and a name that appeared is not missed. The two reads answer two different
    questions: the probe says whether the folder is observable at all, the listing says what is in
    it, and collapsing them would lose the "could not tell" branch below.

    The earlier wording here said "reads the destination exactly once" and was corrected after
    review finding F-4 measured two. The *behaviour* was correct and is unchanged.

    An unobservable destination yields ``known=False`` and **no** conflicts, because none were
    looked for. It is explicitly *not* reported as zero conflicts -- the distinction between
    "nothing collides" and "I could not tell" is the whole point.

    ``candidate_names`` is injected -- the caller passes ``collision.candidate_names``, so the
    alternatives offered here are the very names a real publish would try, without this module
    importing the publishing layer. Omitting it reports the conflicts with no suggestions rather
    than inventing a naming scheme of its own. ``collision.publish`` is never reached by any path:
    nothing is moved, renamed, created or overwritten by this function.
    """
    probe_fn = probe if probe is not None else probe_root
    wanted = []
    seen = set()
    for name in basenames or ():
        key = _basename_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        wanted.append(os.path.basename(str(name)))

    text = normalize_root_text(destination)
    if not text:
        return DestinationForecast(destination="", state=NOT_INSPECTED, observed=False,
                                   detail="no destination folder was named, so no prediction was "
                                          "made and none is implied")

    state = probe_fn(text, listdir=listdir, lstat=lstat, isdir=isdir, realpath=realpath)
    if state.verdict != PRESENT:
        return DestinationForecast(
            destination=state.real or text, state=state.verdict, observed=False,
            detail=f"the destination could not be read ({state.verdict}), so collisions are "
                   f"UNKNOWN rather than zero: {state.detail}")

    listing = list_directory(state.real or text, listdir=listdir, isdir=isdir, realpath=realpath)
    if not listing.ok:
        return DestinationForecast(
            destination=state.real or text, state=listing.verdict, observed=False,
            detail=f"the destination stopped being readable between probe and listing, so "
                   f"collisions are UNKNOWN rather than zero: {listing.detail}")

    taken = set(listing.names)
    conflicts = []
    for name in wanted:
        key = os.path.normcase(name)
        if key not in taken:
            continue
        alternatives = []
        if candidate_names is not None:
            for candidate in candidate_names(name):
                if os.path.normcase(candidate) in taken:
                    continue
                alternatives.append(candidate)
                if len(alternatives) >= MAX_FORECAST_ALTERNATIVES:
                    break
        conflicts.append(NameConflict(basename=name, occupied=True, sources=(),
                                      alternatives=tuple(alternatives)))
    conflicts.sort(key=lambda item: os.path.normcase(item.basename))
    return DestinationForecast(destination=state.real or text, state=PRESENT, observed=True,
                               existing_entries=len(taken), conflicts=tuple(conflicts),
                               detail="a snapshot taken once; the destination may change before "
                                      "anything is published, so this is not binding")


#: What link-level metadata actually established about a child directory. Three values, because
#: "I could not tell" is a real answer and must not be rounded to either of the other two.
_LINK_PLAIN = "plain"
_LINK_ALIAS = "alias"
_LINK_UNDECIDABLE = "undecidable"


def _link_evidence(path, *, lstat=os.lstat) -> str:
    """Is this child an ordinary folder, an alias, or undecidable? Link-level; never followed.

    A **gate**, and therefore deliberately shaped unlike :func:`is_reparse_point`, which is a
    *reporting* predicate and answers ``False`` when link metadata cannot be read. That is the right
    shape for a report and the wrong shape for a decision, and it is the same distinction
    ``input_manager._nested_link_kind`` already draws for Tier A.

    Review finding F-3 is what the missing gate cost: :func:`may_descend` combines that predicate
    with resolved-path containment, and containment alone cannot tell a *contained* alias from a
    real folder -- both resolve inside the root. So a contained junction whose ``lstat`` failed was
    entered, its subtree counted twice, and the result still described itself as a lower bound.

    Returns :data:`_LINK_UNDECIDABLE` rather than guessing. An unreadable attribute is not evidence
    of a plain directory, and it is not evidence of a junction either -- so this reports neither.
    """
    try:
        info = lstat(path)
    except OSError:
        return _LINK_UNDECIDABLE

    mode = getattr(info, "st_mode", None)
    if mode is not None and stat_module.S_ISLNK(mode):
        return _LINK_ALIAS

    attributes = getattr(info, "st_file_attributes", None)
    if attributes is None:
        # POSIX has no such field and needs none: ``S_ISLNK`` above is the only aliasing primitive
        # there, so an ordinary directory is still traversed normally. On Windows a junction cannot
        # be told from a folder without it, so the entry stays undecidable and is refused.
        return _LINK_UNDECIDABLE if os.name == "nt" else _LINK_PLAIN

    return (_LINK_ALIAS if attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT
            else _LINK_PLAIN)


def diagnose_root(root_state, *, suffixes=(), listdir=os.listdir, lstat=os.lstat,
                  isdir=os.path.isdir, realpath=os.path.realpath,
                  max_directories=MAX_DIAGNOSTIC_DIRECTORIES,
                  max_entries=MAX_DIAGNOSTIC_ENTRIES, should_stop=None) -> Diagnostics:
    """Tier B: name **every** directory that could not be read, as a bounded lower bound.

    Deliberately **not** ``os.walk``. ``os.walk`` decides whether to descend with
    ``os.path.islink()``, which is ``False`` for an NTFS junction, so it would follow one straight
    out of the root -- the defect the shared scanner was hardened against. This walk is built from
    A1's own :func:`list_directory` and :func:`may_descend` instead, so:

    * a nested reparse point is never entered, whether it escapes or stays inside;
    * descent requires **resolved-path containment** *and* link-level evidence that the child is an
      ordinary folder -- not merely "it did not look like a link". Both halves are necessary and
      review finding F-3 is why: :func:`is_reparse_point` is a *reporting* predicate and answers
      ``False`` when link metadata cannot be read, and resolved containment cannot tell a
      **contained** alias from a real folder because both resolve inside the root. So a contained
      junction with unreadable metadata satisfied :func:`may_descend` and was entered, and its
      subtree was counted twice. :func:`_link_evidence` is the missing half;
    * a directory whose safety cannot be established is refused rather than guessed at, and named
      in :attr:`Diagnostics.undecidable_links` rather than filed under a classification nobody
      actually observed.

    It exists to tell the operator *which* folders to fix, not how many files they have. Every
    count returned is a lower bound, it stops at its own ceilings, and reaching one is reported
    through :attr:`Diagnostics.bound_reached` rather than passed off as the end of the tree.

    What the two ceilings bound -- the correction for review findings F-1 and F-2:

    * ``max_directories`` charges a directory **attempted**, counted before the listing is tried,
      because a failed listing costs the same ``listdir`` as a successful one.
      :attr:`Diagnostics.directories_listed` still reports the successes.
    * ``max_entries`` charges a directory entry **examined**, counted before the extension filter,
      because an unsupported entry costs the walk an ``isdir`` either way.
      :attr:`Diagnostics.files_seen` still reports supported media.

    Exactly ``max_entries`` entries may be examined and the pass still finish; the next entry
    reports the bound. Neither ceiling is a wall-clock or memory bound -- see
    :data:`MAX_DIAGNOSTIC_ENTRIES`.
    """
    state = root_state if isinstance(root_state, RootState) else RootState()
    if not state.root_key:
        return Diagnostics(ran=False, complete=False,
                           detail="the root has no established location, so nothing could be "
                                  "diagnosed")

    accepted = frozenset(str(item).casefold() for item in (suffixes or ()) if str(item).strip())
    unreadable: list = []
    skipped: list = []
    outside: list = []
    undecidable: list = []
    truncated = False

    def _note(bucket: list, path: str) -> None:
        """Record a path, or record that the record ran out of room. Never grows without bound."""
        nonlocal truncated
        if len(bucket) < MAX_DIAGNOSTIC_NAMES:
            bucket.append(path)
        else:
            truncated = True

    attempted = 0
    directories = 0
    entries = 0
    files = 0
    total_bytes = 0
    bound = ""
    pending = [state.real]
    stopped = False

    while pending:
        if should_stop is not None and should_stop():
            stopped = True
            break
        if attempted >= int(max_directories):
            bound = "directories"
            break
        current = pending.pop(0)
        # Charged BEFORE the listing is tried. Charging only successful listings is finding F-1:
        # a root of unlistable children consumed no budget at all and was walked in full.
        attempted += 1
        listing = list_directory(current, root=state, listdir=listdir, isdir=isdir,
                                 realpath=realpath)
        if not listing.ok:
            _note(unreadable, current)
            continue
        directories += 1
        for name in sorted(listing.names):
            if entries >= int(max_entries):
                bound = "entries"
                break
            # Charged BEFORE the extension filter, so an unsupported entry costs the ceiling what
            # it costs the walk. Charging only supported media is finding F-2, and it is the very
            # semantics ``scan_folder``'s own docstring rules out for Tier A.
            entries += 1
            child = os.path.join(listing.real or current, name)
            try:
                child_is_dir = bool(isdir(child))
            except OSError:
                # Undecidable: not counted as a file and not descended. Named, never guessed.
                _note(unreadable, child)
                continue
            if child_is_dir:
                decision = may_descend(state, child, lstat=lstat, realpath=realpath)
                if not decision.allowed:
                    if decision.action in (SKIP_REPARSE_CONTAINED, SKIP_REPARSE_ESCAPES):
                        _note(skipped, child)
                    else:
                        _note(outside, child)
                    continue
                # `may_descend` approving is necessary but not sufficient: it rests on resolved
                # containment plus a reporting predicate that reads False when link metadata is
                # unreadable, and containment cannot separate a CONTAINED alias from a real folder.
                # Finding F-3. Descent needs positive link-level evidence as well.
                #
                # Cost: one extra `lstat` per child directory `may_descend` approved, because
                # `DescendDecision.is_reparse` reads False both for "readable, not a link" and for
                # "could not read", so the approved case cannot be told apart without asking again.
                # Measured at one per approved child, bounded by both ceilings, on a path that only
                # runs after Tier A has already failed.
                evidence = _link_evidence(child, lstat=lstat)
                if evidence == _LINK_PLAIN:
                    pending.append(child)
                elif evidence == _LINK_ALIAS:
                    _note(skipped, child)
                else:
                    _note(undecidable, child)
                continue
            extension = os.path.splitext(child)[1].casefold()
            if accepted and extension not in accepted:
                continue
            files += 1
            try:
                info = lstat(child)
                total_bytes += int(getattr(info, "st_size", 0) or 0)
            except OSError:
                pass
        if bound:
            break

    complete = ((not pending) and not bound and not unreadable and not undecidable
                and not truncated and not stopped)
    if stopped:
        bound = bound or "stopped"
    return Diagnostics(
        ran=True, complete=bool(complete), directories_attempted=attempted,
        directories_listed=directories, entries_examined=entries, files_seen=files,
        bytes_seen=total_bytes, unreadable_directories=tuple(unreadable),
        skipped_links=tuple(skipped), outside_root=tuple(outside),
        undecidable_links=tuple(undecidable), names_truncated=bool(truncated),
        bound_reached=bound,
        detail="a bounded diagnostic pass; every figure here is a LOWER BOUND and none of them is "
               "a total")


def _counts_from_input_set(input_set) -> MediaCounts:
    """Transcribe an ``InputSet`` into :class:`MediaCounts`. No traversal, no recomputation."""
    by_reason = input_set.rejected_by_reason()

    def _count(name: str) -> int:
        for reason, value in by_reason.items():
            if getattr(reason, "name", str(reason)) == name:
                return int(value)
        return 0

    return MediaCounts(
        discovered=int(input_set.discovered_count),
        ready=int(input_set.ready_count),
        ready_bytes=int(input_set.total_ready_bytes),
        path_collisions=int(input_set.path_collisions),
        unsupported=_count("UNSUPPORTED_EXTENSION"),
        empty=_count("EMPTY_FILE"),
        unreadable=_count("UNREADABLE"),
        not_a_file=_count("NOT_A_FILE"),
    )


def inspect_library(roots, *, suffixes, scan, scan_error, limit_error, destinations=None,
                    include_rejected=False, max_entries=MAX_INVENTORY_ENTRIES_PER_ROOT,
                    candidate_names=None, probe=None, should_stop=None, diagnostics=True,
                    listdir=os.listdir, lstat=os.lstat, isdir=os.path.isdir,
                    realpath=os.path.realpath, clock=None) -> LibraryInventory:
    """Inventory the roots handed in. Reads only; creates, moves and deletes nothing.

    Args:
        roots: ``(role, path)`` pairs. **Explicit and validated, never discovered** -- this
            function reads no preference file, expands no pattern, probes no drive letter and has
            no notion of "the other folders". A blank path is refused by
            :func:`normalize_root_text` before ``os.path.abspath`` could turn it into the
            application directory.
        suffixes: the accepted media extensions, **supplied by the caller** (the GUI passes
            ``clips_review.SERVABLE_SUFFIXES``) rather than imported, so this module stays free of
            the review layer.
        scan: **required.** The authoritative scanner -- ``input_manager.scan_folder``. Injected
            rather than imported, and deliberately given no default: a default would be one more
            thing a caller could get wrong, and this module would then need the import that A1's
            own guard pins shut.
        scan_error: **required.** The exception class (or tuple) a refused scan raises --
            ``input_manager.InputScanError``.
        limit_error: **required.** The subclass raised when the ceiling is reached --
            ``input_manager.InputScanLimitExceeded``. Passed separately because the two refusals
            read differently to an operator, not because they are handled differently in kind.
        destinations: optional ``{role: path}`` for collision forecasting. Each is read **once per
            pass** -- one state probe plus one name snapshot, never once per candidate name -- and
            never created. See :func:`forecast_destination` for which of the two the prediction
            comes from.
        include_rejected: a root whose role is :data:`REJECTED_ROLE` is skipped unless this is
            ``True``. Offering it is not the same as asking for it.
        max_entries: Tier A's per-root classification ceiling. See
            :data:`MAX_INVENTORY_ENTRIES_PER_ROOT` for what it is and is not.
        candidate_names: optional ``collision.candidate_names``-shaped callable for destination
            alternatives.
        should_stop: cooperative stop, checked **between roots and between phases only**. One
            ``scan`` call is atomic and cannot be interrupted; nothing here claims otherwise.
        diagnostics: run Tier B after a Tier A failure.
        clock: optional ``time.time``-shaped callable stamping :attr:`LibraryInventory.observed_at`,
            so every finding carries an "as observed at". ``0.0`` when not supplied.

    Returns:
        A :class:`LibraryInventory`. Read :attr:`RootInventory.totals` for figures that are totals.

    Tier A is the sole counting authority. A successful ``InputSet`` is
    :data:`INVENTORY_COMPLETE`; **any** ``InputScanError`` -- including
    ``InputScanLimitExceeded`` -- is :data:`INVENTORY_INCOMPLETE` with no counts attached, and Tier
    B then runs for diagnosis only. A Tier A refusal is never converted into a smaller successful
    inventory: in particular the shared scanner **refuses** a root containing a nested junction, and
    that refusal is reported as an incomplete inventory plus a named link, never as a total that
    quietly excluded it.

    Clause (d) is applied to the inventory as well as to leaf verdicts: every root that produced a
    total is **re-probed at the end of the pass**, and a root that is no longer positively observed
    is demoted to :data:`INVENTORY_INCOMPLETE`. Demotion only -- nothing is ever promoted.

    What that re-probe asks, precisely, and what it therefore does not (review finding F-5): the
    path re-probed is ``root.real``, the **resolved storage the files were actually counted off**,
    so the question is *"is the storage I counted still observable?"* and not *"does the selected
    path still lead here?"*. The two differ for exactly one shape -- a selected root that is itself
    a junction, repointed to another target mid-pass while the original target stays reachable.
    There the re-probe passes and the published total remains a truthful, timestamped count of the
    storage that was counted, while the selected path now leads somewhere else.

    This is deliberate rather than overlooked. A1's findings F-1 and F-6 established the *resolved*
    location as a root's identity precisely so that figures are attributed to the storage they came
    off, :attr:`LibraryInventory.observed_at` stamps the window every finding describes, and
    :class:`RootInventory` retains both :attr:`~RootInventory.requested` and ``root.real`` so the
    resolution is disclosed rather than hidden -- :func:`inventory_lines` prints both when they
    differ. Re-probing ``requested`` instead would answer the other question, and would change
    accepted A1 identity semantics; it is a separate decision, not a defect of this clause.
    """
    if scan is None or scan_error is None or limit_error is None:
        raise ValueError(
            "scan, scan_error and limit_error are required: pass input_manager.scan_folder, "
            "input_manager.InputScanError and input_manager.InputScanLimitExceeded")
    scanner = scan
    probe_fn = probe if probe is not None else probe_root
    accepted = tuple(str(item) for item in (suffixes or ()))
    folders = dict(destinations or {})
    observed_at = 0.0
    if clock is not None:
        try:
            observed_at = float(clock())
        except (TypeError, ValueError):
            observed_at = 0.0

    results: list = []
    stopped = False

    for role, path in list(roots or ()):
        role_name = str(role or "")
        requested = str(path or "")

        if stopped or (should_stop is not None and should_stop()):
            stopped = True
            results.append(RootInventory(
                role=role_name, requested=requested, completeness=NOT_INSPECTED,
                reason=REASON_STOPPED,
                detail="the pass was stopped before this folder was inspected"))
            continue

        if role_name == REJECTED_ROLE and not include_rejected:
            results.append(RootInventory(
                role=role_name, requested=requested, completeness=NOT_INSPECTED,
                reason=REASON_NOT_REQUESTED,
                detail="the Discarded folder was not included in this inspection"))
            continue

        state = probe_fn(requested, listdir=listdir, lstat=lstat, isdir=isdir, realpath=realpath)
        if state.verdict != PRESENT:
            # A root that is blank, absent, refused, unreachable or unresolvable is not scanned at
            # all. Tier A never ran, so there is nothing for Tier B to diagnose either: A1's root
            # verdict already says precisely what was wrong.
            results.append(RootInventory(
                role=role_name, requested=requested, root=state,
                completeness=(NOT_INSPECTED if state.verdict == NOT_INSPECTED
                              else INVENTORY_INCOMPLETE),
                reason=(REASON_NOT_REQUESTED if state.verdict == NOT_INSPECTED
                        else REASON_ROOT_NOT_USABLE),
                limit=int(max_entries) if max_entries is not None else None,
                detail=state.detail or f"the folder is {state.verdict}"))
            continue

        try:
            input_set = scanner(state.real, recursive=True, extensions=accepted,
                               detect_duplicates=False, max_entries=max_entries)
        except limit_error as exc:
            results.append(RootInventory(
                role=role_name, requested=requested, root=state,
                completeness=INVENTORY_INCOMPLETE, reason=REASON_LIMIT_EXCEEDED,
                diagnostics=(diagnose_root(state, suffixes=accepted, listdir=listdir, lstat=lstat,
                                           isdir=isdir, realpath=realpath,
                                           should_stop=should_stop)
                             if diagnostics else None),
                limit=int(getattr(exc, "limit", max_entries) or 0),
                counted_at_least=int(getattr(exc, "counted_at_least", 0) or 0),
                detail=str(exc)))
            continue
        except scan_error as exc:
            # Covers the nested-junction refusal and every traversal failure. Reported as an
            # incomplete inventory with a named cause -- never as a smaller complete one.
            results.append(RootInventory(
                role=role_name, requested=requested, root=state,
                completeness=INVENTORY_INCOMPLETE, reason=REASON_SCAN_REFUSED,
                diagnostics=(diagnose_root(state, suffixes=accepted, listdir=listdir, lstat=lstat,
                                           isdir=isdir, realpath=realpath,
                                           should_stop=should_stop)
                             if diagnostics else None),
                limit=int(max_entries) if max_entries is not None else None,
                detail=str(exc)))
            continue

        counts = _counts_from_input_set(input_set)
        groups = duplicate_basenames(input_set.paths)

        forecast = None
        if should_stop is not None and should_stop():
            stopped = True
            if role_name in folders:
                # Tier A finished, so its counts ARE a total and are kept. The forecast is the
                # phase that was dropped, and it must say so: `None` here would be
                # indistinguishable from "no destination was configured".
                forecast = DestinationForecast(
                    destination="", state=NOT_INSPECTED, observed=False,
                    detail="the pass was stopped before the destination was read, so collisions "
                           "are UNKNOWN rather than zero")
        elif role_name in folders:
            forecast = forecast_destination(
                folders[role_name], [os.path.basename(p) for p in input_set.paths],
                candidate_names=candidate_names, probe=probe_fn, listdir=listdir, lstat=lstat,
                isdir=isdir, realpath=realpath)

        results.append(RootInventory(
            role=role_name, requested=requested, root=state, completeness=INVENTORY_COMPLETE,
            reason=REASON_NONE, counts=counts, duplicate_basenames=groups, destination=forecast,
            limit=int(max_entries) if max_entries is not None else None, detail=""))

    # Clause (d) for totals: a root that stopped being observable during the pass loses its total.
    finalised: list = []
    for item in results:
        if item.completeness != INVENTORY_COMPLETE:
            finalised.append(item)
            continue
        again = probe_fn(item.root.real or item.requested, listdir=listdir, lstat=lstat,
                         isdir=isdir, realpath=realpath)
        if again.positively_observed and again.root_key == item.root.root_key:
            finalised.append(item)
            continue
        finalised.append(replace(
            item, completeness=INVENTORY_INCOMPLETE, reason=REASON_ROOT_LOST, counts=None,
            detail="the folder stopped being observable before the pass finished, so its counts "
                   "are not published as totals"))

    return LibraryInventory(roots=tuple(finalised), stopped=bool(stopped),
                            observed_at=observed_at, suffixes=accepted,
                            limit=int(max_entries) if max_entries is not None else None)


def inventory_lines(inventory) -> tuple:
    """A plain read-out. It decides nothing and it never prints a partial figure as a total.

    Every inspected root is named, and when the selected path resolved somewhere else both are
    printed: the figures belong to the **resolved** storage, so a read-out that showed only the
    role could not disclose that a selected junction was followed. Review finding F-5.

    The pass's :attr:`LibraryInventory.observed_at` is stated when there is one, because every
    figure below describes a window beginning at that instant rather than the present moment. It is
    **omitted entirely** when no clock was injected -- a fabricated timestamp would be worse than
    none, and this module cannot import ``time`` (its imports are pinned to the stdlib set A1
    fixed), so the raw value is printed as it was supplied.
    """
    lines: list = []
    observed_at = getattr(inventory, "observed_at", 0.0) or 0.0
    if observed_at:
        lines.append(f"as observed at {observed_at} - a point in time; the folders below may have "
                     f"changed since")
    for item in getattr(inventory, "roots", ()) or ():
        label = item.role or "(folder)"
        requested = item.requested or "(unnamed)"
        resolved = str(getattr(item.root, "real", "") or "")
        where = requested
        if resolved and _norm(resolved) != _norm(requested):
            where = f"{requested} -> {resolved} (the storage these figures came off)"
        if item.completeness == NOT_INSPECTED:
            lines.append(f"{label}: NOT INSPECTED - {item.detail or item.reason}")
            lines.append(f"    folder: {where}")
            continue
        if not item.authoritative:
            lines.append(f"{label}: INVENTORY_INCOMPLETE ({item.reason}) - no count for this "
                         f"folder is a total")
            lines.append(f"    folder: {where}")
            if item.reason == REASON_LIMIT_EXCEEDED:
                lines.append(f"    the limit of {item.limit} entries was reached; at least "
                             f"{item.counted_at_least} entries are present")
            diag = item.diagnostics
            if diag is not None and diag.ran:
                lines.append(f"    diagnostics (LOWER BOUND, not a total): {diag.files_seen} "
                             f"media files in {diag.directories_listed} folders")
                for path in diag.unreadable_directories:
                    lines.append(f"    could not read: {path}")
                for path in diag.skipped_links:
                    lines.append(f"    link not followed: {path}")
                for path in diag.undecidable_links:
                    lines.append(f"    could not tell whether this is a folder or a link, so it "
                                 f"was not entered: {path}")
                if diag.names_truncated:
                    lines.append(f"    more folders were affected than the {MAX_DIAGNOSTIC_NAMES} "
                                 f"listed above; this list is TRUNCATED, not complete")
                if diag.bound_reached:
                    lines.append(f"    diagnostics stopped at its own bound: {diag.bound_reached}")
            continue
        totals = item.totals
        lines.append(f"{label}: {totals.ready} media files, {totals.ready_bytes} bytes "
                     f"({totals.discovered} entries seen)")
        lines.append(f"    folder: {where}")
        if totals.rejected:
            lines.append(f"    not media: {totals.unsupported} unsupported, {totals.empty} empty, "
                         f"{totals.unreadable} unreadable, {totals.not_a_file} not a file")
        if item.duplicate_basenames:
            lines.append(f"    {len(item.duplicate_basenames)} duplicate file names - the NAMES "
                         f"collide; the contents were NOT compared")
        forecast = item.destination
        if forecast is not None:
            if forecast.known:
                lines.append(f"    destination: {len(forecast.conflicts)} name conflicts "
                             f"(a snapshot, not binding)")
            else:
                lines.append(f"    destination: UNKNOWN - {forecast.detail}")
    return tuple(lines)


__all__ = [
    "ACCESS_DENIED", "CONFIGURED_NOT_CREATED", "ENTER", "LEAF_VERDICTS", "MISSING_CONFIRMED",
    "MISSING_PROVISIONAL", "NOT_INSPECTED", "PRESENT", "ROOT_UNREACHABLE", "ROOT_VERDICTS",
    "SKIP_OUTSIDE_ROOT", "SKIP_REPARSE_CONTAINED", "SKIP_REPARSE_ESCAPES", "UNKNOWN",
    "Clauses", "DescendDecision", "LeafVerdict", "Listing", "RootState",
    "anchor_of", "classify_leaf", "classify_os_error", "confirmed_missing", "is_reparse_point",
    "is_within", "list_directory", "may_descend", "normalize_root_text", "probe_root",
    "retract_subtree", "summarise",
    # stage A2
    "INVENTORY_COMPLETE", "INVENTORY_INCOMPLETE", "INVENTORY_REASONS", "LIBRARY_ROLE",
    "MAX_DIAGNOSTIC_DIRECTORIES", "MAX_DIAGNOSTIC_ENTRIES", "MAX_DIAGNOSTIC_NAMES",
    "MAX_FORECAST_ALTERNATIVES",
    "MAX_INVENTORY_ENTRIES_PER_ROOT", "REASON_LIMIT_EXCEEDED", "REASON_NONE",
    "REASON_NOT_REQUESTED", "REASON_ROOT_LOST", "REASON_ROOT_NOT_USABLE", "REASON_SCAN_REFUSED",
    "REASON_STOPPED", "REJECTED_ROLE",
    "BasenameGroup", "DestinationForecast", "Diagnostics", "LibraryInventory", "MediaCounts",
    "NameConflict", "RootInventory",
    "diagnose_root", "duplicate_basenames", "forecast_destination", "inspect_library",
    "inventory_lines",
]
