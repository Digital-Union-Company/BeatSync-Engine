"""Where the Stage-5 analysis cache is configured to be, and what is actually there.

[FORK] Digital-Union (R14-A stage A4). A read-only model of **one invocation's** cache-location
question. It answers where the existing resolver *would* put the cache, and — separately, and from
its own independent observation — what is physically at that location.

**It creates nothing.** Not the configured folder, not a temporary, not a preference file. That is
the whole reason this module exists rather than the question being answered inline.

----

**The hazard this module is built around, measured rather than assumed.**
``analysis_cache.resolve_location`` creates the configured folder when its parent exists. That is
*intended* behaviour, and it is the asymmetry that separates "a new or mistyped folder on a live
disk" from "the disk is not plugged in". It is also reachable from handlers a UI presents as
read-only.

Injecting a no-op ``make_dirs`` stops the creation — and is **not sufficient on its own**, which is
the part that matters::

    resolve_location(local, cfg, make_dirs=<no-op>)   with the folder ABSENT and its parent live
        -> source='shared'  available=True            <-- for a directory that does not exist

So this module does two things the resolver cannot do for it:

* it passes a refusing ``make_dirs`` on **every** resolver call, with no default-argument path that
  could reach the real ``os.makedirs``; and
* it **re-establishes the directory state itself**, through A1's :func:`library_inventory.probe_root`,
  and reports :data:`library_inventory.CONFIGURED_NOT_CREATED` as a state distinct from both
  "active" and "unreachable". :attr:`CacheLocationReport.active` is derived **only** from that
  independent observation and **never** from ``resolve_location``'s ``available``.

The refusal also *remembers*: :attr:`CacheLocationReport.creation_attempts` names every directory
the resolver asked to create and did not get. A non-empty tuple is a positive record that the
hazard was reached and refused, not merely that nothing happened.

----

**Four questions that are not the same question**, kept in separate fields because collapsing them
is how a report starts claiming a missing folder is in use:

===================== =====================================================================
``configured``        a setting names the location. Says nothing about the disk.
``shared_requested``  the existing selection logic *would* try to use it for a NEW invocation.
``observed``          this pass positively established that location's own storage.
``active``            the selected location is both chosen **and** inspectable right now.
===================== =====================================================================

And two more that are routinely confused with them:

* ``fallback`` — the resolver reports a local fallback candidate because the configured shared
  folder was not usable. **This is not evidence that any running analysis switched folders.** It is
  what a new invocation would do, resolved at this instant.
* ``resolver_disagreed`` — the resolver called the selected location usable and the independent
  probe did not. Recorded rather than silently preferred either way; the probe is what ``active``
  follows.

**Nothing here is a statement about a running job.** A render freezes its own location once, at its
own start; this module resolves a fresh answer and cannot see that frozen one. ``selected_path`` is
"what a new invocation would choose", never "what the job currently writing to disk chose".

----

**What this module must never do, and does not:**

* it **counts nothing**. No record count, no byte total, no listing of a cache folder's contents.
  It calls neither ``analysis_cache.measure``, ``cache_admin.measure`` nor
  ``library_cache_records.read_records``, recognises no record filename, and opens no file at all;
* it **opens no media and no JSON**. There is no ``open`` anywhere in this file;
* it **writes nothing** — no ``makedirs``, ``remove``, ``rename``, ``replace``, ``rmdir``, no
  temporary, no preference and no shared configuration;
* it **reads no live configuration**. The settings arrive as a parameter. There is no call to
  ``analysis_cache.load_config``, ``config_path`` or ``default_config_path``, and none to
  ``video_analysis.resolve_cache_location`` — which would both read the real per-user file and
  reach the mutating resolver;
* it **judges no record and no folder obsolete**. A shared cache is read by installations that are
  not running, so "I cannot see it" is never evidence about its contents.

Stdlib only (``os``) plus A1/A2's package siblings, so CLAUDE.md's hard rule holds and every branch
is testable on a bare interpreter. No clock of its own: the observation stamp comes from an injected
``clock`` or is simply absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from beatsync_fork.analysis_cache import (
    CACHE_ENABLED,
    CACHE_FOLDER,
    SOURCE_LOCAL,
    SOURCE_SHARED,
    _as_bool,
    _clean,
    empty_config,
    resolve_location,
)
from beatsync_fork.library_inventory import (
    ACCESS_DENIED,
    CONFIGURED_NOT_CREATED,
    NOT_INSPECTED,
    PRESENT,
    ROOT_UNREACHABLE,
    UNKNOWN,
    probe_root,
)

# `_as_bool` and `_clean` are imported deliberately rather than re-implemented. They are the ONE
# parser for these two settings -- a tolerant bool for a hand-editable file, and a strip-or-empty
# for the path -- and a second copy here is exactly the "second configuration format" this stage is
# required not to introduce. A3 sets the same precedent importing `_link_evidence` from A1.

# ---------------------------------------------------------------------------------------------
# which of the two locations a state describes
# ---------------------------------------------------------------------------------------------

#: This installation's own cache folder, inside its checkout.
ROLE_LOCAL = "local"
#: The per-user configured folder, which several installations may share.
ROLE_SHARED = "shared"

ROLES = (ROLE_LOCAL, ROLE_SHARED)

#: The directory states this module reports. They are **A1's** verdicts, reused verbatim rather
#: than mapped onto a parallel vocabulary: a second set of names for the same six facts is a
#: translation layer that can drift. :data:`NOT_INSPECTED` carries its A1 meaning exactly -- the
#: question was not put -- which is also the honest answer for a shared path that sharing being
#: switched off means nobody looked at.
LOCATION_STATES = (PRESENT, CONFIGURED_NOT_CREATED, ROOT_UNREACHABLE, ACCESS_DENIED, UNKNOWN,
                   NOT_INSPECTED)

#: Said about any configured shared folder, present or not. It is a property of the *folder's role*
#: rather than of anything observed in it, so it is reported even when the folder was never looked
#: at -- a caveat that only appears once a count exists is a caveat that arrives too late.
SHARED_CAVEAT = (
    "This folder is the shared analysis cache and may be read and written by other BeatSync "
    "installations, including ones that are not running now. Records in it whose source is not "
    "part of the library inspected here are outside this inspection's reach -- that is a statement "
    "about the reach, never about the record, and nothing here may call such a record obsolete.")

#: Said whenever the resolver names a local fallback. Deliberately phrased about a *new* invocation.
FALLBACK_CAVEAT = (
    "The configured shared folder could not be used, so a new analysis invocation would fall back "
    "to the local cache. This is what the selection logic answers at this instant; it is not "
    "evidence that an analysis already running has changed folders.")

#: Said when the resolver called the selected location usable and the independent probe did not.
DISAGREEMENT_CAVEAT = (
    "The existing resolver reported this location as available while an independent check of the "
    "directory did not confirm it. The independent check is what this report follows.")


class RefuseDirectoryCreation:
    """A ``make_dirs`` that refuses to create anything, and **remembers being asked**.

    Injected into every :func:`analysis_cache.resolve_location` call this module makes. Two
    properties matter:

    * it never touches the filesystem, so the resolver's documented creation of a configured folder
      cannot happen on a read-only inspection path; and
    * it records the path, so :attr:`CacheLocationReport.creation_attempts` is positive evidence
      that the hazard was *reached and refused*, rather than the absence of evidence that it was
      not reached at all. Those are different findings and a silent no-op cannot tell them apart.

    The signature matches how the resolver calls it (``mkdir(configured, exist_ok=True)``).
    """

    __slots__ = ("attempts",)

    def __init__(self) -> None:
        self.attempts: list = []

    def __call__(self, path, exist_ok=False) -> None:
        self.attempts.append(str(path))
        return None


def _norm(value: str) -> str:
    """Normalised form for COMPARING two paths this module was handed. Resolves nothing."""
    return os.path.normcase(os.path.abspath(value)) if value else ""


@dataclass(frozen=True)
class LocationState:
    """One cache location, as independently observed. Plain strings and bools -- no handles.

    :attr:`verdict` is A1's, from :func:`library_inventory.probe_root`. The three booleans below it
    answer three different questions and are deliberately not one flag:

    * :attr:`inspected` -- was the question put at all? ``False`` for a shared path that sharing
      being off means nobody looked at, which is not the same as a path that was looked at and
      found wanting.
    * :attr:`observed` -- A1's ``positively_observed``: this location's own storage was positively
      seen. Note that :data:`ACCESS_DENIED` **is** positively observed -- the folder is there, we
      were merely refused.
    * :attr:`inspectable` -- could its contents actually be examined? Only :data:`PRESENT` can.
      This is the narrower claim, and it is the one :attr:`CacheLocationReport.active` follows,
      because a folder that exists but cannot be listed is not a folder an inventory can report on.
    """

    role: str = ""
    path: str = ""
    real: str = ""
    verdict: str = NOT_INSPECTED
    inspected: bool = False
    observed: bool = False
    inspectable: bool = False
    anchor: str = ""
    anchor_reachable: bool = False
    is_reparse: bool = False
    detail: str = ""

    @property
    def configured(self) -> bool:
        """Does a setting name this location at all? A pure statement about configuration."""
        return bool(_clean(self.path))


@dataclass(frozen=True)
class CacheLocationReport:
    """One invocation's frozen answer to "where is the analysis cache, and what is there?".

    The headline is :attr:`active`, and it is computed from the independent directory observation
    alone. :attr:`resolver_available` is recorded beside it precisely so the two can be compared --
    it is **never** an input to :attr:`active`.
    """

    # --- the configuration, as a snapshot taken once (see `inspect_location`) ------------------
    #: Was sharing switched on in the snapshot? Parsed by ``analysis_cache._as_bool``.
    sharing_enabled: bool = False
    #: The stored shared-cache path, cleaned. **Preserved even when sharing is off**, because
    #: turning sharing off must not look like forgetting the path -- that is the stored setting's
    #: own contract (``analysis_cache.CACHE_FOLDER``'s docstring), and A4 reports it unexamined.
    configured_shared: str = ""
    #: The local cache directory this installation would use. Handed in, never discovered.
    local_default: str = ""

    # --- what the EXISTING resolver would select. RECORDED, never trusted. ---------------------
    #: Where a **new** invocation would be pointed. Not a statement about any running job.
    selected_path: str = ""
    selected_source: str = SOURCE_LOCAL
    #: Would the selection logic try the shared folder? (``CacheLocation.requested``)
    shared_requested: bool = False
    #: ``CacheLocation.available`` verbatim. Kept for comparison; see :attr:`resolver_disagreed`.
    resolver_available: bool = False
    #: ``CacheLocation.notice`` verbatim -- the resolver's own fallback wording, unparaphrased.
    resolver_notice: str = ""
    #: The resolver wanted the shared folder and named a local fallback instead.
    fallback: bool = False
    #: Every directory the resolver asked :class:`RefuseDirectoryCreation` to create, and did not
    #: get. Positive evidence that the creation hazard was reached and refused.
    creation_attempts: tuple = ()

    # --- what was PHYSICALLY OBSERVED, independently of the resolver ---------------------------
    #: The state of the location the resolver selected. :attr:`active` follows this and only this.
    selected: LocationState = field(default_factory=LocationState)
    #: The configured shared folder's state. ``inspected=False`` when sharing is off.
    shared: LocationState = field(default_factory=LocationState)
    #: The local cache folder's state.
    local: LocationState = field(default_factory=LocationState)
    #: The resolver called the selected location usable; the independent probe did not confirm it.
    resolver_disagreed: bool = False

    #: From an injected ``clock``, or ``""``. This module owns no clock.
    observed_at: str = ""
    #: Ownership, cross-installation and fallback wording that must travel with any figure.
    caveats: tuple = ()
    detail: str = ""

    @property
    def configured(self) -> bool:
        """Is a shared folder named by configuration at all -- switched on or not?"""
        return bool(self.configured_shared)

    @property
    def shared_selected(self) -> bool:
        """Did the selection logic land on the shared folder? Selection only, not usability."""
        return self.selected_source == SOURCE_SHARED

    @property
    def selected_active(self) -> bool:
        """Is the location a NEW invocation would be given actually inspectable right now?

        Derived from :attr:`selected` -- the independent probe -- and never from
        :attr:`resolver_available`. That is the correction the measured hazard forces: with a
        refusing ``make_dirs``, the resolver answers ``available=True`` for a configured folder
        that does not exist, so trusting it would publish a missing directory as usable.

        :data:`ACCESS_DENIED` is deliberately **not** enough even though A1 counts it as positively
        observed. The folder is there; its contents cannot be examined; an inventory of it would be
        a claim this pass cannot support.
        """
        return bool(self.selected.path) and self.selected.verdict == PRESENT

    @property
    def active(self) -> bool:
        """Is the **requested** location both selected AND inspectable?

        Deliberately a different question from :attr:`selected_active`, and the split is forced by
        a case that otherwise reads backwards. When the configured shared folder sits on a drive
        that is not plugged in, the resolver falls back to a local cache that is perfectly fine --
        so the *selected* location is inspectable while the operator did **not** get the folder
        they asked for. Collapsing the two would make an unreachable shared cache report
        ``active=True``, which is precisely the misreport this stage exists to prevent.

        So: under :attr:`fallback` this is ``False`` however healthy the local cache is, and
        :attr:`selected_active` is what says the fallback itself is usable. With sharing switched
        off nothing else was asked for, so the local cache *is* the requested location and the two
        agree.
        """
        if self.fallback:
            return False
        return self.selected_active

    @property
    def status(self) -> str:
        """The **requested** location's directory state -- A1's verdict, not a second vocabulary.

        The requested location, not the selected one, because that is the question an operator is
        asking: under a fallback this answers :data:`ROOT_UNREACHABLE` for the shared folder rather
        than :data:`PRESENT` for the local cache that silently stood in for it.
        :attr:`selected_status` reports the other half.
        """
        if self.shared_requested:
            return self.shared.verdict
        return self.selected.verdict

    @property
    def selected_status(self) -> str:
        """The directory state of the location a new invocation would actually be given."""
        return self.selected.verdict

    @property
    def shared_in_use(self) -> bool:
        """Would a new invocation actually write into the shared folder?

        Both halves required: selected **and** inspectable. The name says *would*, not *is*: no
        running job is observable from here.
        """
        return self.shared_selected and self.selected_active


def inspect_location(local_default, config=None, *,
                     listdir=os.listdir, lstat=os.lstat, isdir=os.path.isdir,
                     realpath=os.path.realpath, clock=None) -> CacheLocationReport:
    """Report the cache location for ONE invocation. Reads only; creates nothing.

    ``config`` is the settings mapping, **handed in**. This module never reads the per-user shared
    configuration file: a read-only inspection has no business resolving the operator's live
    settings path, and a test that did would be reading the configuration their production install
    depends on.

    **The snapshot is taken once.** ``config`` is copied on entry, so a caller that mutates its own
    dictionary while this function runs cannot move the answer halfway through -- the same reason
    ``resolve_location`` freezes a location per invocation rather than per source.

    Three observations happen, in this order, and the later ones never overwrite the earlier:

    1. the existing resolver is asked what it *would* select, with a refusing ``make_dirs``;
    2. the local folder is probed;
    3. the configured shared folder is probed -- **only if sharing is on and a path is set**, so
       that switching sharing off leaves the stored path reported but untouched.

    ``listdir``/``lstat``/``isdir``/``realpath`` are threaded into A1's probe so every branch is
    reachable without a disk. ``isdir`` is also handed to the resolver, so its own availability
    test and this module's probes see one injected filesystem rather than two.
    """
    refusal = RefuseDirectoryCreation()

    # ONE immutable observation of the configuration. `dict()` rather than the mapping itself so a
    # later mutation by the caller cannot be read back mid-invocation.
    snapshot = empty_config() if config is None else dict(config)

    local_clean = _clean(local_default)
    configured = _clean(snapshot.get(CACHE_FOLDER))
    enabled = _as_bool(snapshot.get(CACHE_ENABLED))

    # (1) What would the existing selection logic do? `make_dirs` is supplied EXPLICITLY here --
    # there is no other resolver call in this module, and none without this keyword.
    location = resolve_location(local_clean, snapshot, make_dirs=refusal, is_dir=isdir)

    # (2) The local folder. Always probed: it is this installation's own, and "not created yet" is
    # an ordinary first-run state that must not read as a fault.
    local_state = _probe(ROLE_LOCAL, local_clean, listdir=listdir, lstat=lstat, isdir=isdir,
                         realpath=realpath)

    # (3) The shared folder -- only when sharing is actually on AND a path is set. With sharing off
    # the stored path is reported verbatim and NOT inspected, which is why `inspected` exists.
    if enabled and configured:
        shared_state = _probe(ROLE_SHARED, configured, listdir=listdir, lstat=lstat, isdir=isdir,
                              realpath=realpath)
    else:
        shared_state = LocationState(
            role=ROLE_SHARED, path=configured, verdict=NOT_INSPECTED,
            detail=("sharing is switched off, so the stored folder was reported but not inspected"
                    if configured else "no shared folder is configured"))

    # Which of the two did the resolver land on? Reuse the probe already taken rather than probing
    # the same directory twice -- a second probe could disagree with the first for no better reason
    # than the gap between them.
    if location.shared and _norm(location.path) == _norm(configured):
        selected = shared_state
    elif _norm(location.path) == _norm(local_clean):
        selected = local_state
    else:
        # The resolver named something that is neither folder this pass knows about. Probe it
        # rather than guessing, and never assume it inherits either state.
        selected = _probe(location.source, location.path, listdir=listdir, lstat=lstat,
                          isdir=isdir, realpath=realpath)

    fallback = bool(location.requested) and not location.shared

    # The resolver said the selected location was usable and the probe did not confirm it. Scoped
    # to a location that actually exists as a string, so a blank selection is not a disagreement.
    disagreed = (bool(location.available) and bool(location.path)
                 and not (selected.verdict == PRESENT))

    caveats: list = []
    if configured:
        caveats.append(SHARED_CAVEAT)
    if fallback:
        caveats.append(FALLBACK_CAVEAT)
    if disagreed:
        caveats.append(DISAGREEMENT_CAVEAT)

    stamp = ""
    if clock is not None:
        try:
            stamp = str(clock())
        except Exception:
            stamp = ""

    return CacheLocationReport(
        sharing_enabled=enabled,
        configured_shared=configured,
        local_default=local_clean,
        selected_path=location.path,
        selected_source=location.source,
        shared_requested=bool(location.requested),
        resolver_available=bool(location.available),
        resolver_notice=location.notice,
        fallback=fallback,
        creation_attempts=tuple(refusal.attempts),
        selected=selected,
        shared=shared_state,
        local=local_state,
        resolver_disagreed=disagreed,
        observed_at=stamp,
        caveats=tuple(caveats),
        detail=selected.detail,
    )


def _probe(role: str, path: str, *, listdir, lstat, isdir, realpath) -> LocationState:
    """One location's independent directory state, via A1. Creates nothing.

    A blank path is :data:`NOT_INSPECTED` rather than missing: there was never a directory to ask
    about, and ``os.path.abspath("")`` answers the working directory -- a resolution that has
    already cost this repository one defect, so a blank selection is refused here rather than
    resolved.
    """
    cleaned = _clean(path)
    if not cleaned:
        return LocationState(role=role, path="", verdict=NOT_INSPECTED,
                             detail="no folder was named, so nothing was inspected")

    state = probe_root(cleaned, listdir=listdir, lstat=lstat, isdir=isdir, realpath=realpath)
    return LocationState(
        role=role,
        path=cleaned,
        real=state.real,
        verdict=state.verdict,
        inspected=True,
        observed=bool(state.positively_observed),
        inspectable=state.verdict == PRESENT,
        anchor=state.anchor,
        anchor_reachable=bool(state.anchor_reachable),
        is_reparse=bool(state.is_reparse),
        detail=state.detail,
    )


__all__ = [
    "DISAGREEMENT_CAVEAT", "FALLBACK_CAVEAT", "SHARED_CAVEAT",
    "LOCATION_STATES", "ROLES", "ROLE_LOCAL", "ROLE_SHARED",
    "CacheLocationReport", "LocationState", "RefuseDirectoryCreation",
    "inspect_location",
]
