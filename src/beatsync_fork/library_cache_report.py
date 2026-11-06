"""Where the analysis cache's figures come from, and what each one is allowed to claim.

[FORK] Digital-Union (R14-A stage A5). Unwired: nothing in the application imports this module.

A5 is the **reporting layer** and it is a **pure function**. It computes no new facts: it consumes
observations A1-A4 already produced and decides, for each number, whether that number may be
published as a total, published as a floor, or withheld. It makes **no filesystem call at all** --
not a listing, not a stat, not an open, not a `realpath` -- which is a stronger constraint than
A1-A4 carry and is what keeps A5 out of the limits conversation entirely: there is nothing here to
bound.

**The reconciled model: four independent axes, not one taxonomy.** The original design described
seven buckets A-G with "B-G are classifications of A". Measured against the implemented producers
that is not true, and building it would have manufactured a partition that does not exist:

* **cost** (B cheap / C expensive) is a **bytes** fact about *named folders* -- `cache_admin`'s four
  caller-supplied category roots, split by :attr:`cache_admin.Category.expensive`;
* **contract** (F) is a **records** fact inside *one* cache folder -- A3 rung 3;
* **scope** (G) is a **records** fact inside the same folder -- A3 rung 6;
* **source state** (D) is a **records** fact, and it has *no producer in A1-A4 at all*: A3's
  `_classify_scope` "resolves a path; **stats nothing**", so D is only reachable by joining an
  externally finalised A1 verdict onto A3's per-record `source_path`;
* **E** is the visible uncertainty category, and it is deliberately a *set of separately labelled
  observations* rather than a number, because its members can refer to the same underlying entries
  and are not even all in the same unit.

`CacheRecord.contract` and `CacheRecord.scope` are **separate fields with separate counters**, so one
record can be ``CONTRACT_OTHER`` *and* ``SCOPE_OUT``. F and G therefore overlap, cannot be summed,
and are not parts of a partition of A. Every cross-axis sum is refused by construction: there is no
field to put one in.

**There is no reclaimable total, in any spelling.** Not withheld, not conditional -- no such field
exists on :class:`Buckets`, so no caller can render one. A5 never recommends deleting anything.

**Why exactness is a field rather than a convention.** Each producer already distinguishes a total
from a floor, and A5's whole job is to not lose that:

* `RecordInventory.counts_exact` vs `authoritative` are deliberately different questions --
  `names_truncated` destroys unique evidence and raises a bound, while `records_truncated` only
  shortens a display, so the aggregate counters stay exact when the 200-row list does not;
* `RecordInventory.complete_records` returns ``None`` rather than ``0`` when the figure was never
  established, and rendering that as zero would invent "no records are complete";
* `LeafVerdict.published` demotes a provisional or non-final missing verdict to ``UNKNOWN``, which
  is why this module reads ``published`` and **never** ``verdict``;
* `RootState.positively_observed` is the only thing that may anchor a scope verdict, which is why an
  entry in `RecordInventory.unverified_roots` contributes nothing here either.

**Neither measurement may be published as a complete physical total** (clarification C-1).
`analysis_cache.measure` skips an unreadable or vanished entry and has no field in which to say so
-- measured, one unreadable entry took a fixture from 2 records / 51 B to 1 record / 28 B with
``error=''`` -- and it lists flat, so a nested subdirectory is invisible. `cache_admin.measure`
*does* name per-file skips, but it passes no ``onerror`` to `os.walk`, so an existing but unlistable
root returns ``exists=True, files=0, total_bytes=0, skipped=[], error=''``: a clean zero
indistinguishable from a genuinely empty folder. So a positive count is ``OBSERVED_PARTIAL``, a
clean zero is :data:`EMPTY_OR_UNREADABLE`, an absent folder yields **no** zero-byte occupancy claim,
and A5 **never calls either measurement function** -- it is handed their results.

**Population, not just count** (clarification C-2). A3's per-record counters are bumped only inside
``if payload is not None``, so they answer for *parsed* records. A figure that covers the parsed
population is not a figure about the whole folder, and every record-level figure here therefore
carries the population it answers for and the unknown remainder it does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from os.path import commonpath, isabs, normcase, normpath

from beatsync_fork.library_inventory import (
    ACCESS_DENIED,
    LeafVerdict,
    MISSING_CONFIRMED,
    ROOT_UNREACHABLE,
    UNKNOWN,
)
from beatsync_fork.library_cache_records import (
    CONTRACT_OTHER,
    PARSE_OK,
    SCOPE_OUT,
    SCOPE_UNKNOWN,
)

# ---------------------------------------------------------------------------------------------
# units, exactness and axes
# ---------------------------------------------------------------------------------------------

#: What a figure counts. A bytes figure and a records figure are never interchangeable, and
#: `report_lines` prints the unit on every numeric line so the two cannot be read as comparable.
BYTES = "bytes"
RECORDS = "records"
ENTRIES = "entries"
UNITS = (BYTES, RECORDS, ENTRIES)

#: Covers the whole stated subject; may be published as a total.
EXACT = "exact"
#: A real observation that is known not to cover everything. A **floor**, never a total.
OBSERVED_PARTIAL = "observed_partial"
#: Explicitly a floor because a cap was reached or a detail list was truncated.
LOWER_BOUND = "lower_bound"
#: A measurement returned a clean zero that cannot be distinguished from an unreadable folder.
#: The value is shown so the measurement is not hidden, but it is **not** a measured zero.
EMPTY_OR_UNREADABLE = "empty_or_unreadable"
#: The question was not put, or the evidence cannot support an answer. ``value`` is ``None`` and
#: must never be rendered as ``0``.
NOT_ASSESSED = "not_assessed"
EXACTNESS = (EXACT, OBSERVED_PARTIAL, LOWER_BOUND, EMPTY_OR_UNREADABLE, NOT_ASSESSED)

#: A figure that may be read as a total for its stated subject.
PUBLISHABLE = (EXACT,)

AXIS_TOTAL = "measured_total"
AXIS_COST = "cost"
AXIS_CONTRACT = "contract"
AXIS_SCOPE = "scope"
AXIS_SOURCE = "source_state"
AXIS_UNCERTAIN = "uncertain"
AXES = (AXIS_TOTAL, AXIS_COST, AXIS_CONTRACT, AXIS_SCOPE, AXIS_SOURCE, AXIS_UNCERTAIN)

#: How a folder identity was established. Lexical agreement of path strings is **not** proof of
#: physical identity -- an NTFS junction makes two different strings name one storage -- so the two
#: are distinguished and the weaker one carries a caveat rather than silently passing as the
#: stronger. A5 performs no `realpath`, so it can never upgrade this on its own.
IDENTITY_NONE = "identity_none"
IDENTITY_LEXICAL = "identity_lexical"
IDENTITY_PROVENANCE = "identity_provenance"

#: The mandated neutral wording for a confirmed-missing source. Emitted verbatim, on its own line,
#: and never on a line that also carries D's number. It is the one place the word "delete" appears,
#: because its whole purpose is to deny the recommendation.
MISSING_SOURCE_SENTENCE = (
    "The source file was confirmed missing at the inspected location. This is not a recommendation "
    "to delete the cache record.")

#: The multi-installation rule, carried wherever scope or a shared folder is reported.
OUT_OF_SCOPE_SENTENCE = "A record outside this library is not evidence of an obsolete record."

#: What A4 does and does not establish. A new invocation's selection is not the cache a running
#: Stage-5 job is writing into, and saying so is not optional.
SELECTION_SENTENCE = (
    "This reports the location a new invocation would select; it is not evidence about the cache a "
    "running analysis is currently writing into.")

#: Why a shared folder's figures are a snapshot and nothing more.
SHARED_SENTENCE = (
    "More than one installation may use this folder, so another installation may have changed it "
    "since it was observed.")

#: Rebuilding the expensive category is the thing the cache exists to avoid.
EXPENSIVE_SENTENCE = (
    "Rebuilding this is the GPU analysis pass the cache exists to avoid.")

#: Words that must never appear on a line carrying D's number.
_RECLAIM_WORDS = ("delete", "reclaim", "free", "obsolete", "safe")


# ---------------------------------------------------------------------------------------------
# pure path helpers -- lexical only, and deliberately CWD-independent
# ---------------------------------------------------------------------------------------------

def _key(path) -> str:
    """A comparable lexical key, or ``""`` when the path cannot be one.

    `abspath` is **not** used, and that is the point: it resolves a relative path against the
    process working directory, so a blank or relative input would silently become a real location
    (``abspath("") == os.getcwd()`` -- the trap A3 refuses a blank selection over). A path that is
    not already absolute is not comparable here and is reported as such.
    """
    if not isinstance(path, str):
        return ""
    text = path.strip()
    if not text or not isabs(text):
        return ""
    return normcase(normpath(text))


def _contained(outer: str, inner: str) -> bool:
    """Is ``inner`` lexically at or under ``outer``? ``commonpath``, never ``startswith``.

    ``startswith`` reports ``C:\\lib2`` as inside ``C:\\lib``; `commonpath` compares whole path
    components and does not. It raises on mixed drives or a mix of absolute and relative paths, and
    both of those mean "not comparable" rather than "not contained".
    """
    if not outer or not inner:
        return False
    try:
        return commonpath([outer, inner]) == outer
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------
# the figure
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Figure:
    """One number A5 is prepared to print, with everything needed to not misread it.

    The invariant the whole module rests on: ``value is None`` **exactly when**
    ``exactness == NOT_ASSESSED``, and then ``withheld_reason`` says why. A withheld figure has no
    numeric form at all, so it cannot be rendered as a measured zero.
    """

    value: int | None = None
    unit: str = RECORDS
    exactness: str = NOT_ASSESSED
    axis: str = AXIS_TOTAL
    #: The physical folder, or the record population, this figure answers for.
    subject: str = ""
    #: The denominator. For a record figure: which records were eligible, and how many were not.
    population: str = ""
    #: The producer field this came from, named exactly.
    basis: str = ""
    #: ``(observed_from, observed_to)`` as supplied. Never invented.
    window: tuple = ("", "")
    withheld_reason: str = ""
    #: Upstream caps, bounds and truncation flags that bear on this figure.
    limitations: tuple = ()
    notes: tuple = ()

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def publishable_as_total(self) -> bool:
        """May this be read as a total for :attr:`subject`? Only an exact figure may."""
        return self.exactness in PUBLISHABLE and self.value is not None

    @property
    def consistent(self) -> bool:
        """The structural invariant, asserted rather than assumed."""
        if self.exactness == NOT_ASSESSED:
            return self.value is None and bool(self.withheld_reason)
        return self.value is not None and not self.withheld_reason


def withheld(reason: str, *, unit: str = RECORDS, axis: str = AXIS_TOTAL, subject: str = "",
             population: str = "", basis: str = "", window: tuple = ("", ""),
             limitations: tuple = (), notes: tuple = ()) -> Figure:
    """A figure that is not available, and says why. The only way to build one."""
    return Figure(value=None, unit=unit, exactness=NOT_ASSESSED, axis=axis, subject=subject,
                  population=population, basis=basis, window=window,
                  withheld_reason=reason or "no evidence was supplied",
                  limitations=tuple(limitations), notes=tuple(notes))


# ---------------------------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class FolderIdentity:
    """Which physical folder a set of figures is about, and how firmly that was established."""

    key: str = ""
    proof: str = IDENTITY_NONE
    path_as_configured: str = ""
    path_as_selected: str = ""
    role: str = ""
    #: ``(input_label, key)`` for every input that agreed.
    established_from: tuple = ()
    #: ``(input_label, key)`` for every input that did not. Non-empty means refuse.
    disagreements: tuple = ()
    detail: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.key) and not self.disagreements


@dataclass(frozen=True)
class CategoryObservation:
    """One `cache_admin` category measurement, with the provenance the Measurement itself lacks.

    `cache_admin.Measurement` carries neither the root path nor a timestamp -- its only identity is
    ``key``/``label`` -- so a bare Measurement cannot be bound to a folder and is refused. The
    `Category` is the only place the root is named, and ``observed_at`` has to come from the caller.

    ``storage_key`` is optional *supplied* provenance for the storage behind ``Category.root``
    (whatever the caller can honestly establish -- a resolved path, a volume identity). Without it,
    two lexically distinct roots cannot be shown to be different storage, and the cheap subtotal is
    refused rather than guessed.
    """

    category: object | None = None
    measurement: object | None = None
    observed_at: str = ""
    storage_key: str = ""

    @property
    def root(self) -> str:
        return str(getattr(self.category, "root", "") or "")

    @property
    def key(self) -> str:
        return _key(self.root)

    @property
    def category_key(self) -> str:
        return str(getattr(self.category, "key", "") or "")

    @property
    def label(self) -> str:
        return str(getattr(self.category, "label", "") or self.category_key)

    @property
    def expensive(self) -> bool:
        return bool(getattr(self.category, "expensive", False))

    @property
    def usable(self) -> bool:
        return bool(self.category is not None and self.measurement is not None and self.key)


@dataclass(frozen=True)
class Observation:
    """The validated input envelope. Built by :func:`bind_observation`, never constructed ad hoc."""

    identity: FolderIdentity = field(default_factory=FolderIdentity)
    location: object | None = None
    inventory: object | None = None
    categories: tuple = ()
    flat_measurement: object | None = None
    flat_observed_at: str = ""
    source_verdicts: tuple = ()
    library_roots: tuple = ()
    window: tuple = ("", "")
    refusals: tuple = ()
    caveats: tuple = ()

    @property
    def combinable(self) -> bool:
        return self.identity.usable and not self.refusals


@dataclass(frozen=True)
class Buckets:
    """The A-G labels as **views** onto four axes. Deliberately no total of any kind.

    There is no ``reclaimable_total`` and no ``safe_to_delete_total`` field, in any spelling. Their
    absence is the design, not an omission: a field that does not exist cannot be rendered.
    """

    identity: FolderIdentity = field(default_factory=FolderIdentity)
    #: A -- the headline measured figure, when exactly one method was supplied for this folder.
    measured: Figure = field(default_factory=Figure)
    #: Every measurement supplied for this folder, one Figure per method. Never added together.
    measurements: tuple = ()
    #: B -- ``(category_key, Figure)`` for every non-expensive category.
    cheap: tuple = ()
    #: C -- ``(category_key, Figure)`` for every expensive category.
    expensive: tuple = ()
    #: B's subtotal. Refused unless the roots were *proven* disjoint from supplied provenance.
    cheap_subtotal: Figure = field(default_factory=Figure)
    #: D -- confirmed-missing sources, from externally finalised A1 verdicts only.
    confirmed_missing: Figure = field(default_factory=Figure)
    #: E -- the headline uncertainty figure, withheld whenever membership cannot be proved unique.
    uncertain: Figure = field(default_factory=Figure)
    #: E -- the separately labelled holds. ``(label, Figure)``; never summed.
    uncertain_observations: tuple = ()
    #: F -- records recording a different cache-contract generation.
    other_configuration: Figure = field(default_factory=Figure)
    #: G -- records whose source resolves outside every verified inspection root.
    out_of_scope: Figure = field(default_factory=Figure)
    #: Where one record or one byte may legitimately appear in two figures.
    overlaps: tuple = ()
    caveats: tuple = ()
    refusals: tuple = ()


# ---------------------------------------------------------------------------------------------
# binding: establish the subject, or refuse
# ---------------------------------------------------------------------------------------------

def _as_category_observation(item) -> CategoryObservation:
    """Accept a :class:`CategoryObservation`, or a ``(category, measurement, observed_at[, key])``
    tuple. A bare measurement is not accepted -- it has no root."""
    if isinstance(item, CategoryObservation):
        return item
    if isinstance(item, (tuple, list)):
        parts = list(item) + ["", ""]
        return CategoryObservation(category=parts[0], measurement=parts[1],
                                   observed_at=str(parts[2] or ""), storage_key=str(parts[3] or ""))
    return CategoryObservation()


def bind_observation(*, location=None, inventory=None, categories=(), flat_measurement=None,
                     source_verdicts=(), library_roots=(), observed_at="") -> Observation:
    """Establish the folder this evidence is about, recording disagreement instead of guessing.

    Pure. The only thing this function decides is **whether** the supplied observations may be read
    together, and it answers conservatively: a key that cannot be formed, or two keys that differ,
    produce a refusal that :func:`estimate_reclaimable` then honours by withholding the combined
    figures rather than blending them.

    What "the same folder" can and cannot mean here is stated rather than assumed. A5 performs no
    `realpath`, so agreement between two path *strings* is lexical agreement and nothing more --
    an NTFS junction can point two different strings at one storage, and can point one string at
    different storage over time. Where an input also carries a *resolved* location (A3's
    ``resolved``, A4's ``LocationState.real``) those are compared too, and a resolved mismatch under
    matching typed paths is treated as a disagreement: that is exactly what a re-pointed junction
    looks like.
    """
    refusals: list = []
    caveats: list = []
    agreed: list = []
    disagreed: list = []

    cats = tuple(_as_category_observation(item) for item in (categories or ()))
    for index, cat in enumerate(cats):
        if cat.category is None and cat.measurement is not None:
            refusals.append(
                f"category observation {index} supplied a measurement with no Category, so the "
                f"folder it describes cannot be established")
        elif cat.measurement is not None and not cat.key:
            refusals.append(
                f"category {cat.category_key or index!r} has no usable absolute root, so its "
                f"measurement has no verifiable subject")
        elif cat.measurement is not None and not cat.observed_at:
            caveats.append(
                f"category {cat.label!r} was supplied without an observation time; "
                f"cache_admin.Measurement carries none of its own")

    # --- the primary subject: the cache folder A3 and A4 are talking about -----------------------
    typed: list = []
    resolved: list = []
    role = ""
    configured = ""
    selected = ""

    if location is not None:
        selected = str(getattr(location, "selected_path", "") or "")
        configured = str(getattr(location, "configured_shared", "") or "")
        chosen = getattr(location, "selected", None)
        role = str(getattr(chosen, "role", "") or "")
        if _key(selected):
            typed.append(("A4.selected_path", _key(selected)))
        real = str(getattr(chosen, "real", "") or "")
        if _key(real):
            resolved.append(("A4.selected.real", _key(real)))

    if inventory is not None:
        path = str(getattr(inventory, "path", "") or "")
        res = str(getattr(inventory, "resolved", "") or "")
        if _key(path):
            typed.append(("A3.path", _key(path)))
        if _key(res):
            resolved.append(("A3.resolved", _key(res)))

    if flat_measurement is not None:
        path = str(getattr(flat_measurement, "path", "") or "")
        if _key(path):
            typed.append(("analysis_cache.Measurement.path", _key(path)))
        else:
            refusals.append("the flat measurement has no usable absolute path, so it has no "
                            "verifiable subject")

    keys = [k for _label, k in typed]
    primary = keys[0] if keys else ""
    for label, k in typed:
        (agreed if k == primary else disagreed).append((label, k))

    # A resolved mismatch is a disagreement even when the typed paths agree: that is what a
    # junction re-pointed onto different storage looks like, and it must not pass silently.
    resolved_keys = [k for _label, k in resolved]
    proof = IDENTITY_NONE
    if primary:
        proof = IDENTITY_LEXICAL
        if resolved_keys:
            if len(set(resolved_keys)) == 1:
                proof = IDENTITY_PROVENANCE
                agreed.extend(resolved)
            else:
                disagreed.extend(resolved)
                refusals.append(
                    "the supplied resolved locations disagree, so these observations may describe "
                    "different storage behind similar paths")

    if disagreed:
        refusals.append(
            "the supplied observations name different folders, so they were not combined: "
            + "; ".join(f"{label}={k}" for label, k in agreed + disagreed))

    if not primary:
        refusals.append("no usable absolute cache-folder path was supplied, so no subject could be "
                        "established")

    if proof == IDENTITY_LEXICAL:
        caveats.append("folder identity rests on path strings only: no resolved location was "
                       "supplied and A5 performs no filesystem lookup, so two different strings "
                       "could still name one storage")

    identity = FolderIdentity(
        key=primary, proof=proof, path_as_configured=configured, path_as_selected=selected,
        role=role, established_from=tuple(agreed), disagreements=tuple(disagreed),
        detail=("established from " + ", ".join(label for label, _k in agreed)) if agreed else "")

    verdicts = tuple(v for v in (source_verdicts or ()) if v is not None)
    roots = tuple(r for r in (library_roots or ()) if r is not None)

    window = ("", "")
    if inventory is not None:
        window = (str(getattr(inventory, "observed_from", "") or ""),
                  str(getattr(inventory, "observed_to", "") or ""))
    elif location is not None:
        stamp = str(getattr(location, "observed_at", "") or "")
        window = (stamp, stamp)
    elif observed_at:
        window = (str(observed_at), str(observed_at))

    return Observation(identity=identity, location=location, inventory=inventory, categories=cats,
                       flat_measurement=flat_measurement, flat_observed_at=str(observed_at or ""),
                       source_verdicts=verdicts, library_roots=roots, window=window,
                       refusals=tuple(refusals), caveats=tuple(caveats))


# ---------------------------------------------------------------------------------------------
# axis A / B / C -- measured bytes
# ---------------------------------------------------------------------------------------------

def _admin_figure(cat: CategoryObservation, *, axis: str, subject: str) -> Figure:
    """Classify one `cache_admin.Measurement`. Never ``EXACT`` -- clarification C-1.

    `cache_admin.measure` passes no ``onerror`` to `os.walk`, and `os.walk` with ``onerror=None``
    discards a listing failure, so a nested directory that cannot be read is skipped **silently**
    even while other readable files keep ``files > 0``. ``error == '' and skipped == []`` therefore
    does not prove complete coverage of the folder, and no combination of the fields on the
    Measurement can prove it. A5 produces no coverage certificate and must not manufacture one, so
    the best available label for a positive count is a floor.
    """
    m = cat.measurement
    window = (cat.observed_at, cat.observed_at)
    limits: list = []
    skipped = tuple(getattr(m, "skipped", ()) or ())
    error = str(getattr(m, "error", "") or "")
    exists = bool(getattr(m, "exists", False))
    files = int(getattr(m, "files", 0) or 0)
    total = int(getattr(m, "total_bytes", 0) or 0)
    basis = f"cache_admin.measure({cat.category_key}).total_bytes"

    if error:
        return withheld(f"the category could not be measured: {error}", unit=BYTES, axis=axis,
                        subject=subject, basis=basis, window=window,
                        notes=(f"category {cat.label!r}",))
    if not exists:
        return withheld("the folder does not exist, so no occupancy was measured", unit=BYTES,
                        axis=axis, subject=subject, basis=basis, window=window,
                        notes=(f"category {cat.label!r}",))
    if skipped:
        limits.append(f"{len(skipped)} entries were skipped and named by cache_admin")
    limits.append("cache_admin.measure passes no onerror to os.walk, so an unreadable nested "
                  "directory can be omitted without being reported")

    if files == 0 and total == 0:
        return Figure(value=0, unit=BYTES, exactness=EMPTY_OR_UNREADABLE, axis=axis,
                      subject=subject, population="the whole category root",
                      basis=basis, window=window, limitations=tuple(limits),
                      notes=(f"category {cat.label!r}",
                             "either empty or unreadable: a clean zero from this measurement "
                             "cannot be distinguished from a folder that could not be listed"))
    return Figure(value=total, unit=BYTES, exactness=OBSERVED_PARTIAL, axis=axis, subject=subject,
                  population=f"at least {files} files seen under the category root",
                  basis=basis, window=window, limitations=tuple(limits),
                  notes=(f"category {cat.label!r}",
                         "a floor: complete physical coverage of the folder was not certified"))


def _flat_figure(observation: Observation, subject: str) -> Figure:
    """Classify one `analysis_cache.Measurement`. ``OBSERVED_PARTIAL`` at best, by measurement.

    The dataclass has no ``skipped`` field and no completeness field, and the function skips an
    unreadable or vanished entry deliberately (a shared folder legitimately has other processes
    writing into it). It also lists **flat** rather than walking, because `os.walk` would descend an
    NTFS junction -- correct, and it means nested bytes are neither counted nor reported as
    uncounted.
    """
    m = observation.flat_measurement
    stamp = observation.flat_observed_at
    window = (stamp, stamp)
    basis = "analysis_cache.measure(path).total_bytes"
    error = str(getattr(m, "error", "") or "")
    exists = bool(getattr(m, "exists", False))
    records = int(getattr(m, "records", 0) or 0)
    others = int(getattr(m, "other_files", 0) or 0)
    total = int(getattr(m, "total_bytes", 0) or 0)
    limits = ("analysis_cache.Measurement has no skipped or completeness field, so an unreadable "
              "or vanished entry leaves no trace",
              "a flat listing, so any nested subdirectory is not counted and not reported")

    if error:
        return withheld(f"the folder could not be read: {error}", unit=BYTES, axis=AXIS_TOTAL,
                        subject=subject, basis=basis, window=window, limitations=limits,
                        notes=("unreadable, which is not the same as absent",))
    if not exists:
        return withheld("the folder does not exist, so no occupancy was measured", unit=BYTES,
                        axis=AXIS_TOTAL, subject=subject, basis=basis, window=window,
                        limitations=limits)
    if not stamp:
        return withheld("no observation time was supplied for this measurement", unit=BYTES,
                        axis=AXIS_TOTAL, subject=subject, basis=basis, limitations=limits)
    if records == 0 and others == 0 and total == 0:
        return Figure(value=0, unit=BYTES, exactness=EMPTY_OR_UNREADABLE, axis=AXIS_TOTAL,
                      subject=subject, population="the flat listing of the folder", basis=basis,
                      window=window, limitations=limits,
                      notes=("either empty or every entry was unreadable: this measurement cannot "
                             "distinguish the two",))
    return Figure(value=total, unit=BYTES, exactness=OBSERVED_PARTIAL, axis=AXIS_TOTAL,
                  subject=subject,
                  population=f"at least {records + others} entries seen in the flat listing",
                  basis=basis, window=window, limitations=limits,
                  notes=("a floor: entries that could not be read are silently absent from it",))


def _disjoint(cats: tuple) -> tuple:
    """Can these category roots be *proven* to be different storage? ``(ok, reason)``.

    Default is refusal. Lexical distinctness is not enough -- two different strings can name one
    storage through a junction -- so every category must carry supplied ``storage_key`` provenance,
    all of them must differ, and no root may lexically contain another. A5 resolves nothing itself,
    so when the provenance is absent the honest answer is that disjointness is unknown.
    """
    if len(cats) < 2:
        return True, ""
    missing = [c.label for c in cats if not c.storage_key]
    if missing:
        return False, ("no storage provenance was supplied for " + ", ".join(repr(m) for m in missing)
                       + ", so these roots cannot be shown to be different storage")
    seen: dict = {}
    for c in cats:
        if c.storage_key in seen:
            return False, (f"{c.label!r} and {seen[c.storage_key]!r} were supplied with the same "
                           f"storage provenance, so they name one storage under two paths")
        seen[c.storage_key] = c.label
    for outer in cats:
        for inner in cats:
            if outer is inner:
                continue
            if _contained(outer.key, inner.key):
                return False, (f"{inner.label!r} lies inside {outer.label!r}, so their bytes would "
                               f"be counted twice")
    return True, ""


# ---------------------------------------------------------------------------------------------
# axis F / G -- per-record contract and scope
# ---------------------------------------------------------------------------------------------

def _counter_total(counts) -> int:
    if not isinstance(counts, dict):
        return 0
    return int(sum(int(v or 0) for v in counts.values()))


def _record_axis_figure(inv, *, counts_attr, assessed_attr, wanted, axis, label, subject,
                        window, extra_limits=()) -> Figure:
    """One per-record axis figure, with its population and unknown remainder stated.

    A3 bumps these counters only inside ``if payload is not None``, so they answer for the
    **parsed** population -- ``parse_counts[PARSE_OK]`` -- and not for the folder. An exact count
    across every recognised record additionally needs the assessed population to equal the parsed
    population *and* the parsed population to equal the recognised one. Each shortfall is named
    rather than absorbed.
    """
    counts = getattr(inv, counts_attr, None)
    assessed = bool(getattr(inv, assessed_attr, False))
    basis = f"RecordInventory.{counts_attr}[{wanted}]"
    if not assessed or not isinstance(counts, dict):
        return withheld(f"{label} was not assessed for this folder", unit=RECORDS, axis=axis,
                        subject=subject, basis=basis, window=window,
                        limitations=tuple(extra_limits))

    value = int(counts.get(wanted, 0) or 0)
    answered = _counter_total(counts)
    parsed = int(getattr(inv, "records_parsed", 0) or 0)
    recognised = int(getattr(inv, "records_recognised", 0) or 0)
    counts_exact = bool(getattr(inv, "counts_exact", False))
    bound = str(getattr(inv, "bound", "") or "")
    unstable = bool(getattr(inv, "unstable", False))

    limits = list(extra_limits)
    if bound:
        limits.append(f"the pass stopped on {bound}")
    if not counts_exact:
        limits.append("RecordInventory.counts_exact is False, so the counters do not cover the "
                      "whole folder")
    if answered < parsed:
        limits.append(f"{parsed - answered} parsed records did not reach this question")
    if parsed < recognised:
        limits.append(f"{recognised - parsed} recognised records could not be parsed, so this "
                      f"question was never put to them")
    if unstable:
        limits.append("the folder changed while it was being read")

    population = (f"{answered} of {parsed} parsed records answered this question; "
                  f"{recognised} records were recognised in total")

    covers_everything = (counts_exact and answered == parsed and parsed == recognised
                         and not unstable)
    if covers_everything:
        return Figure(value=value, unit=RECORDS, exactness=EXACT, axis=axis, subject=subject,
                      population=population + " (complete coverage)", basis=basis, window=window,
                      limitations=tuple(limits))
    if counts_exact and not unstable:
        # Exact among the records that were actually assessed -- publishable only with the
        # population said out loud, which is what `population` is for.
        return Figure(value=value, unit=RECORDS, exactness=LOWER_BOUND, axis=axis, subject=subject,
                      population=population + " (exact among the assessed records only)",
                      basis=basis, window=window, limitations=tuple(limits),
                      notes=("a floor for the whole folder: records that were not assessed could "
                             "also qualify",))
    return Figure(value=value, unit=RECORDS, exactness=LOWER_BOUND, axis=axis, subject=subject,
                  population=population, basis=basis, window=window, limitations=tuple(limits),
                  notes=("a floor: the record inventory itself is not exact",))


# ---------------------------------------------------------------------------------------------
# axis D -- confirmed-missing sources, from externally finalised A1 verdicts only
# ---------------------------------------------------------------------------------------------

def _verified_root_keys(inv) -> tuple:
    return tuple(str(k) for k in (getattr(inv, "inspection_roots", ()) or ()) if str(k))


def _finalised_confirmed(item) -> bool:
    """Is this verdict genuinely a finalised confirmation, on its own evidence?

    Two independent gates, because either alone has been shown insufficient one layer down:

    * ``published`` rather than ``verdict`` -- A1 demotes ``MISSING_PROVISIONAL``, and
      ``MISSING_CONFIRMED`` that is not ``is_final``, to ``UNKNOWN``;
    * ``clauses.all_four`` -- a hand-built verdict can carry the enum **and** ``is_final=True`` with
      no clauses met at all, and that must not become proof of anything. A1's own `retract_subtree`
      applies the same second guard for the same reason.
    """
    if not isinstance(item, LeafVerdict):
        return False
    if item.published != MISSING_CONFIRMED:
        return False
    return bool(getattr(item.clauses, "all_four", False))


def _source_state_figure(obs: Observation, inv, subject, window) -> tuple:
    """``(Figure, rejected, holds)`` for axis D. Never generates a verdict of its own."""
    basis = ("library_inventory.LeafVerdict.published == MISSING_CONFIRMED, joined onto "
             "RecordInventory.records[].source_path")
    verdicts = obs.source_verdicts
    rejected: list = []
    holds: list = []

    if not verdicts:
        return (withheld("no finalised source verdict was supplied, so nothing is known about any "
                         "source file", unit=RECORDS, axis=AXIS_SOURCE, subject=subject,
                         basis=basis, window=window),
                tuple(rejected), tuple(holds))

    root_keys = _verified_root_keys(inv) if inv is not None else ()
    if not root_keys:
        return (withheld("no verified inspection root was supplied, so no source verdict has "
                         "jurisdiction over records in this folder", unit=RECORDS,
                         axis=AXIS_SOURCE, subject=subject, basis=basis, window=window),
                tuple(rejected), tuple(holds))

    # --- index the supplied verdicts by path, refusing contradictions rather than picking one ----
    by_path: dict = {}
    contradictions: list = []
    for item in verdicts:
        if not isinstance(item, LeafVerdict):
            rejected.append(("not a LeafVerdict", repr(type(item).__name__)))
            continue
        key = _key(getattr(item, "path", ""))
        if not key:
            rejected.append(("no usable absolute path", str(getattr(item, "path", ""))))
            continue
        vroot = str(getattr(item, "root_key", "") or "")
        if vroot and vroot not in root_keys:
            rejected.append(("root provenance not among the verified inspection roots",
                             f"{item.path} (root_key={vroot})"))
            continue
        if not vroot:
            rejected.append(("no root provenance, so jurisdiction cannot be established",
                             str(item.path)))
            continue
        previous = by_path.get(key)
        if previous is not None and previous.published != item.published:
            contradictions.append(key)
        by_path[key] = item

    if contradictions:
        return (withheld("two supplied verdicts disagree about the same source path, and A5 does "
                         "not choose between them: " + ", ".join(sorted(set(contradictions))),
                         unit=RECORDS, axis=AXIS_SOURCE, subject=subject, basis=basis,
                         window=window),
                tuple(rejected), tuple(holds))

    records = tuple(getattr(inv, "records", ()) or ())
    truncated = bool(getattr(inv, "records_truncated", False))
    counts_exact = bool(getattr(inv, "counts_exact", False))
    unstable = bool(getattr(inv, "unstable", False))

    eligible = []
    for rec in records:
        key = _key(getattr(rec, "source_path", ""))
        if key:
            eligible.append((rec, key))

    if not eligible:
        return (withheld("no record in the supplied inventory carries a usable source path, so no "
                         "source verdict can be joined to one", unit=RECORDS, axis=AXIS_SOURCE,
                         subject=subject, basis=basis, window=window),
                tuple(rejected), tuple(holds))

    confirmed = 0
    unmatched = 0
    not_confirmed = 0
    for rec, key in eligible:
        item = by_path.get(key)
        if item is None:
            unmatched += 1
            continue
        if _finalised_confirmed(item):
            confirmed += 1
        else:
            not_confirmed += 1
            if isinstance(item, LeafVerdict) and item.verdict == MISSING_CONFIRMED \
                    and not getattr(item.clauses, "all_four", False):
                rejected.append(("a confirmed-missing enum without its finalised clause evidence",
                                 f"{item.path} (unmet: "
                                 f"{','.join(item.clauses.unmet()) or 'none recorded'})"))

    limits: list = []
    if truncated:
        limits.append("RecordInventory.records is a truncated display, so records beyond it could "
                      "not be joined to a verdict")
    if not counts_exact:
        limits.append("RecordInventory.counts_exact is False")
    if unmatched:
        limits.append(f"{unmatched} eligible records had no supplied source verdict")
    if unstable:
        limits.append("the folder changed while it was being read")
    if rejected:
        limits.append(f"{len(rejected)} supplied verdicts were rejected as evidence")

    population = (f"{len(eligible) - unmatched} of {len(eligible)} records with a source path were "
                  f"matched to a finalised verdict; {not_confirmed} of those were not a "
                  f"confirmation")

    complete_coverage = (counts_exact and not truncated and unmatched == 0 and not unstable
                         and not rejected)
    if complete_coverage:
        return (Figure(value=confirmed, unit=RECORDS, exactness=EXACT, axis=AXIS_SOURCE,
                       subject=subject, population=population + " (complete coverage)",
                       basis=basis, window=window),
                tuple(rejected), tuple(holds))
    return (Figure(value=confirmed, unit=RECORDS, exactness=LOWER_BOUND, axis=AXIS_SOURCE,
                   subject=subject, population=population, basis=basis, window=window,
                   limitations=tuple(limits),
                   notes=("a floor: records with no supplied verdict were not assessed",)),
            tuple(rejected), tuple(holds))


# ---------------------------------------------------------------------------------------------
# axis E -- separately labelled holds
# ---------------------------------------------------------------------------------------------

def _uncertainty(obs: Observation, inv, subject, window, rejected) -> tuple:
    """``(headline, observations)``. Separate labels, separate units, deliberately no sum.

    Clarification C-3: the uncertainty category stays visible, but a visible category does not imply
    a known number. These members can refer to the **same** underlying entries -- an unclassified
    directory entry may be the same thing as a skipped one -- and they are not even all in the same
    unit, so adding them would invent a population. The headline is therefore published only when
    exactly one member contributes, and withheld otherwise.
    """
    out: list = []

    def add(label, figure):
        out.append((label, figure))

    if inv is not None:
        unclassified = int(getattr(inv, "entries_unclassified", 0) or 0)
        skipped = int(getattr(inv, "skipped_entries", 0) or 0)
        recognised = int(getattr(inv, "records_recognised", 0) or 0)
        parsed = int(getattr(inv, "records_parsed", 0) or 0)
        unavailable = int(getattr(inv, "completion_unavailable", 0) or 0)
        scope_counts = getattr(inv, "scope_counts", None)
        scope_assessed = bool(getattr(inv, "scope_assessed", False))
        unverified = tuple(getattr(inv, "unverified_roots", ()) or ())
        entries_complete = bool(getattr(inv, "entries_complete", False))

        add("uncertain: directory entries that could not be classified",
            Figure(value=unclassified, unit=ENTRIES, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject, population="entries examined by this pass",
                   basis="RecordInventory.entries_unclassified", window=window))
        add("uncertain: directory entries deliberately skipped",
            Figure(value=skipped, unit=ENTRIES, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject, population="entries examined by this pass",
                   basis="RecordInventory.skipped_entries", window=window))
        add("uncertain: recognised records that could not be parsed",
            Figure(value=max(recognised - parsed, 0), unit=RECORDS, exactness=EXACT,
                   axis=AXIS_UNCERTAIN, subject=subject,
                   population=f"{recognised} recognised records",
                   basis="RecordInventory.records_recognised - records_parsed", window=window))
        add("uncertain: records whose completion authority would not answer",
            Figure(value=unavailable, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject, population=f"{parsed} parsed records",
                   basis="RecordInventory.completion_unavailable", window=window))
        if scope_assessed and isinstance(scope_counts, dict):
            add("uncertain: records whose recorded source path could not be resolved",
                Figure(value=int(scope_counts.get(SCOPE_UNKNOWN, 0) or 0), unit=RECORDS,
                       exactness=EXACT, axis=AXIS_UNCERTAIN, subject=subject,
                       population=f"{_counter_total(scope_counts)} records scoped",
                       basis=f"RecordInventory.scope_counts[{SCOPE_UNKNOWN}]", window=window))
        else:
            add("uncertain: source scope was not assessed",
                withheld("scope was not assessed for this folder, so the number of unscoped "
                         "records is unknown", unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject,
                         basis="RecordInventory.scope_assessed", window=window))
        if unverified:
            add("uncertain: roots supplied without a positive observation",
                Figure(value=len(unverified), unit=ENTRIES, exactness=EXACT, axis=AXIS_UNCERTAIN,
                       subject=subject, population="roots supplied to the record pass",
                       basis="RecordInventory.unverified_roots", window=window,
                       notes=("these took no part in any scope verdict",)))
        if not entries_complete:
            add("uncertain: the enumeration did not reach the end of the folder",
                withheld("the entry loop stopped early, so the number of unseen entries is "
                         "unknown", unit=ENTRIES, axis=AXIS_UNCERTAIN, subject=subject,
                         basis="RecordInventory.entries_complete", window=window))
    else:
        add("uncertain: no record inventory was supplied",
            withheld("no record inventory was supplied, so nothing about this folder's records is "
                     "known", unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject,
                     basis="Observation.inventory"))

    if obs.location is not None:
        shared = getattr(obs.location, "shared", None)
        verdict = str(getattr(shared, "verdict", "") or "")
        if verdict in (ROOT_UNREACHABLE, ACCESS_DENIED, UNKNOWN):
            add(f"uncertain: the configured shared folder reported {verdict}",
                withheld(f"A1's verdict for the configured shared folder was {verdict}, so its "
                         f"contents were not established", unit=RECORDS, axis=AXIS_UNCERTAIN,
                         subject=subject, basis="CacheLocationReport.shared.verdict",
                         window=window))

    if rejected:
        add("uncertain: supplied source verdicts rejected as evidence",
            Figure(value=len(rejected), unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject, population="verdicts supplied to this report",
                   basis="A5 rejection of unusable source verdicts", window=window,
                   notes=tuple(f"{why}: {what}" for why, what in rejected)[:8]))

    contributing = [(label, fig) for label, fig in out
                    if fig.value is not None and fig.value > 0]
    any_withheld = any(fig.value is None for _label, fig in out)

    if len(contributing) == 1 and not any_withheld:
        label, fig = contributing[0]
        headline = replace(fig, notes=fig.notes + (f"the only uncertainty observed: {label}",))
    elif not contributing and not any_withheld:
        headline = Figure(value=0, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                          subject=subject,
                          population="every uncertainty question that was put returned zero",
                          basis="all A5 uncertainty observations", window=window)
    else:
        headline = withheld(
            "uncertain, but not countable: these holds can refer to the same entries and are not "
            "all in the same unit, so they are reported separately and never summed",
            unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject,
            basis="A5 uncertainty observations", window=window,
            notes=tuple(label for label, _f in out)[:12])
    return headline, tuple(out)


# ---------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------

def estimate_reclaimable(observation) -> Buckets:
    """Turn bound observations into figures. Pure: no filesystem call, no resolver, no measurement.

    The name is the design document's and is kept for continuity, but note what it does **not**
    produce: there is no reclaimable total on :class:`Buckets`, and no figure here is a
    recommendation. An estimate is not permission, and this function offers nothing to act on.
    """
    if not isinstance(observation, Observation):
        observation = bind_observation()

    obs = observation
    identity = obs.identity
    subject = identity.key or "an unestablished folder"
    window = obs.window
    inv = obs.inventory
    refusals = list(obs.refusals)
    caveats = list(obs.caveats)
    overlaps: list = []

    # ------------------------------------------------------------------ cost axis (B and C)
    cheap: list = []
    expensive: list = []
    for cat in obs.categories:
        if not cat.usable:
            continue
        axis = AXIS_COST
        fig = _admin_figure(cat, axis=axis, subject=cat.key)
        (expensive if cat.expensive else cheap).append((cat.category_key, fig))

    cheap_cats = tuple(c for c in obs.categories if c.usable and not c.expensive)
    ok, why = _disjoint(cheap_cats)
    values = [fig.value for _k, fig in cheap if fig.value is not None
              and fig.exactness != EMPTY_OR_UNREADABLE]
    if not cheap:
        cheap_subtotal = withheld("no non-expensive category was supplied", unit=BYTES,
                                  axis=AXIS_COST, subject=subject)
    elif not ok:
        cheap_subtotal = withheld("the category roots could not be shown to be different storage, "
                                  "so adding them could count the same bytes twice: " + why,
                                  unit=BYTES, axis=AXIS_COST, subject=subject,
                                  basis="sum of cache_admin category measurements")
    elif len(values) != len(cheap):
        cheap_subtotal = withheld("at least one category was not measured, so a subtotal would "
                                  "understate the group without saying so", unit=BYTES,
                                  axis=AXIS_COST, subject=subject,
                                  basis="sum of cache_admin category measurements")
    else:
        cheap_subtotal = Figure(
            value=int(sum(values)), unit=BYTES, exactness=LOWER_BOUND, axis=AXIS_COST,
            subject="the non-expensive categories, taken together",
            population=f"{len(values)} categories, each proven to be different storage",
            basis="sum of cache_admin category measurements",
            limitations=("every member is itself a floor, so the sum is a floor",))

    if expensive and any(cat.expensive and cat.key == identity.key for cat in obs.categories):
        overlaps.append("the expensive category and the measured total describe the same folder: "
                        "they are the same bytes on two axes, not two quantities")

    # ------------------------------------------------------------------ measured total (A)
    measurements: list = []
    for cat in obs.categories:
        if cat.usable and identity.key and cat.key == identity.key:
            measurements.append((f"cache_admin.{cat.category_key}",
                                 _admin_figure(cat, axis=AXIS_TOTAL, subject=cat.key)))
    if obs.flat_measurement is not None:
        measurements.append(("analysis_cache.measure", _flat_figure(obs, subject)))

    if not measurements:
        measured = withheld("the cache folder was not measured", unit=BYTES, axis=AXIS_TOTAL,
                            subject=subject)
    elif len(measurements) == 1:
        measured = measurements[0][1]
    else:
        measured = withheld(
            "more than one measurement method was supplied for this folder and they must not be "
            "combined: " + ", ".join(name for name, _f in measurements),
            unit=BYTES, axis=AXIS_TOTAL, subject=subject,
            basis="; ".join(f.basis for _n, f in measurements))
        overlaps.append("two measurement methods describe the same folder by different rules "
                        "(a contained walk versus a flat listing); they disagree by construction "
                        "and are never added")

    # ------------------------------------------------------------------ record axes (D, E, F, G)
    if inv is None:
        other_configuration = withheld("no record inventory was supplied", unit=RECORDS,
                                       axis=AXIS_CONTRACT, subject=subject)
        out_of_scope = withheld("no record inventory was supplied", unit=RECORDS, axis=AXIS_SCOPE,
                                subject=subject)
        confirmed_missing = withheld("no record inventory was supplied", unit=RECORDS,
                                     axis=AXIS_SOURCE, subject=subject)
        rejected = ()
    else:
        other_configuration = _record_axis_figure(
            inv, counts_attr="contract_counts", assessed_attr="contract_assessed",
            wanted=CONTRACT_OTHER, axis=AXIS_CONTRACT,
            label="the cache-contract generation", subject=subject, window=window,
            extra_limits=("only the recorded cache-contract generation was compared: A3 reads no "
                          "analysis version, backend identity or Qwen configuration token, so "
                          "those remain unassessed",))
        roots = _verified_root_keys(inv)
        out_of_scope = _record_axis_figure(
            inv, counts_attr="scope_counts", assessed_attr="scope_assessed",
            wanted=SCOPE_OUT, axis=AXIS_SCOPE, label="source scope", subject=subject,
            window=window,
            extra_limits=(() if roots else
                          ("no verified inspection root anchored the comparison",)))
        if not roots and out_of_scope.value is not None:
            out_of_scope = withheld(
                "no verified inspection root was supplied, so nothing lies demonstrably outside "
                "the library", unit=RECORDS, axis=AXIS_SCOPE, subject=subject,
                basis=f"RecordInventory.scope_counts[{SCOPE_OUT}]", window=window)
        confirmed_missing, rejected, _holds = _source_state_figure(obs, inv, subject, window)

    uncertain, uncertain_observations = _uncertainty(obs, inv, subject, window, rejected)

    if (other_configuration.value or 0) > 0 and (out_of_scope.value or 0) > 0:
        overlaps.append("a record can record a different cache-contract generation AND resolve "
                        "outside the library: the two figures may count the same records and are "
                        "never added")
    if (confirmed_missing.value or 0) > 0 and (out_of_scope.value or 0) > 0:
        overlaps.append("a confirmed-missing source and an out-of-scope source are separate "
                        "questions about the same records; the two figures are never added")

    # ------------------------------------------------------------------ caveats
    if obs.location is not None:
        caveats.append(SELECTION_SENTENCE)
        if bool(getattr(obs.location, "shared_in_use", False)) or \
                bool(getattr(obs.location, "configured", False)):
            caveats.append(SHARED_SENTENCE)
            caveats.append(OUT_OF_SCOPE_SENTENCE)
        if bool(getattr(obs.location, "resolver_disagreed", False)):
            caveats.append("the resolver's own availability answer disagreed with the independent "
                           "observation of the same folder")
        for text in tuple(getattr(obs.location, "caveats", ()) or ()):
            caveats.append(str(text))
    if inv is not None and bool(getattr(inv, "unstable", False)):
        caveats.append("the folder demonstrably changed while it was being read, so every "
                       "record-level figure here is a floor")
    if expensive:
        caveats.append(EXPENSIVE_SENTENCE)

    # ------------------------------------------------------------------ the refusal gate
    # An unestablished or disputed subject means the per-folder figures were never about one
    # folder, so they are withheld rather than blended. The per-category and per-method figures
    # survive, because each of those already carries its own subject and is a statement about
    # that subject alone -- refusing them too would destroy evidence instead of qualifying it.
    if not obs.combinable:
        why = ("the observations could not be shown to describe one folder, so no combined figure "
               "was produced: " + "; ".join(refusals) if refusals else
               "no folder subject could be established")
        refusals.append("no combined figure was produced for this folder")
        measured = withheld(why, unit=BYTES, axis=AXIS_TOTAL, subject=subject)
        cheap_subtotal = withheld(why, unit=BYTES, axis=AXIS_COST, subject=subject)
        confirmed_missing = withheld(why, unit=RECORDS, axis=AXIS_SOURCE, subject=subject)
        other_configuration = withheld(why, unit=RECORDS, axis=AXIS_CONTRACT, subject=subject)
        out_of_scope = withheld(why, unit=RECORDS, axis=AXIS_SCOPE, subject=subject)
        uncertain = withheld(why, unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject)

    return Buckets(
        identity=identity, measured=measured, measurements=tuple(measurements),
        cheap=tuple(cheap), expensive=tuple(expensive), cheap_subtotal=cheap_subtotal,
        confirmed_missing=confirmed_missing, uncertain=uncertain,
        uncertain_observations=uncertain_observations,
        other_configuration=other_configuration, out_of_scope=out_of_scope,
        overlaps=tuple(dict.fromkeys(overlaps)), caveats=tuple(dict.fromkeys(caveats)),
        refusals=tuple(dict.fromkeys(refusals)))


def _figure_line(label: str, fig: Figure) -> list:
    """One figure, rendered so none of its qualifications can be lost on the way to a screen."""
    lines: list = []
    if fig.value is None:
        lines.append(f"{label}: not assessed -- {fig.withheld_reason}")
    elif fig.exactness == EMPTY_OR_UNREADABLE:
        lines.append(f"{label}: {fig.value} {fig.unit} reported, but either empty or unreadable -- "
                     f"this is not a measured zero")
    elif fig.exactness == EXACT:
        lines.append(f"{label}: {fig.value} {fig.unit} (exact)")
    else:
        lines.append(f"{label}: at least {fig.value} {fig.unit} ({fig.exactness})")
    if fig.subject:
        lines.append(f"    subject: {fig.subject}")
    if fig.population:
        lines.append(f"    population: {fig.population}")
    if fig.basis:
        lines.append(f"    evidence: {fig.basis}")
    if any(fig.window):
        lines.append(f"    observed: {fig.window[0] or '?'} .. {fig.window[1] or '?'}")
    for item in fig.limitations:
        lines.append(f"    limitation: {item}")
    for item in fig.notes:
        lines.append(f"    note: {item}")
    return lines


def report_lines(buckets) -> list[str]:
    """The report. Every number carries its unit, its exactness and the population it answers for.

    Two placement rules are load-bearing rather than cosmetic. The mandated missing-source sentence
    is emitted on its **own** line, never on the line carrying D's number, so the number can never
    be read as an offer. And no line carrying D's number contains a reclaim verb at all.
    """
    if not isinstance(buckets, Buckets):
        return ["Analysis cache report: no observation was supplied."]

    out: list = ["Analysis cache report"]
    ident = buckets.identity
    if ident.key:
        out.append(f"  folder: {ident.key}")
        out.append(f"  identity: {ident.proof}"
                   + (f" -- {ident.detail}" if ident.detail else ""))
    else:
        out.append("  folder: not established")
    if ident.path_as_configured:
        out.append(f"  configured shared folder: {ident.path_as_configured}")
    if ident.path_as_selected:
        out.append(f"  location a new invocation would select: {ident.path_as_selected}")

    out.append("  measured storage")
    out.extend("  " + line for line in _figure_line("    A measured", buckets.measured))
    for name, fig in buckets.measurements:
        out.extend("  " + line for line in _figure_line(f"    by {name}", fig))

    out.append("  regeneration cost")
    for key, fig in buckets.cheap:
        out.extend("  " + line for line in _figure_line(f"    B {key} could be rebuilt cheaply",
                                                        fig))
    for key, fig in buckets.expensive:
        out.extend("  " + line for line in _figure_line(f"    C {key}", fig))
    out.extend("  " + line for line in _figure_line("    B subtotal", buckets.cheap_subtotal))

    out.append("  source state")
    out.extend("  " + line for line in _figure_line("    D source confirmed missing",
                                                    buckets.confirmed_missing))
    if (buckets.confirmed_missing.value or 0) > 0:
        out.append(f"    {MISSING_SOURCE_SENTENCE}")

    out.append("  configuration")
    out.extend("  " + line for line in
               _figure_line("    F belongs to a different analysis configuration, not obsolete",
                            buckets.other_configuration))
    out.append("    Backend, analysis-version and Qwen configuration identity were not assessed.")

    out.append("  scope")
    out.extend("  " + line for line in
               _figure_line("    G describes a source outside the folders inspected",
                            buckets.out_of_scope))
    out.append(f"    {OUT_OF_SCOPE_SENTENCE}")

    out.append("  uncertainty")
    out.extend("  " + line for line in _figure_line("    E uncertain", buckets.uncertain))
    for label, fig in buckets.uncertain_observations:
        out.extend("  " + line for line in _figure_line(f"    E {label}", fig))

    for item in buckets.overlaps:
        out.append(f"  overlap: {item}")
    for item in buckets.caveats:
        out.append(f"  note: {item}")
    for item in buckets.refusals:
        out.append(f"  refused: {item}")
    out.append("  No reclaimable-space figure is offered, and nothing here is a recommendation to "
               "remove any file.")
    return out


__all__ = [
    "AXES", "AXIS_CONTRACT", "AXIS_COST", "AXIS_SCOPE", "AXIS_SOURCE", "AXIS_TOTAL",
    "AXIS_UNCERTAIN",
    "BYTES", "ENTRIES", "RECORDS", "UNITS",
    "EMPTY_OR_UNREADABLE", "EXACT", "EXACTNESS", "LOWER_BOUND", "NOT_ASSESSED",
    "OBSERVED_PARTIAL", "PUBLISHABLE",
    "IDENTITY_LEXICAL", "IDENTITY_NONE", "IDENTITY_PROVENANCE",
    "EXPENSIVE_SENTENCE", "MISSING_SOURCE_SENTENCE", "OUT_OF_SCOPE_SENTENCE",
    "SELECTION_SENTENCE", "SHARED_SENTENCE",
    "Buckets", "CategoryObservation", "Figure", "FolderIdentity", "Observation",
    "bind_observation", "estimate_reclaimable", "report_lines", "withheld",
]
