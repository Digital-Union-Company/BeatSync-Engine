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
    NOT_INSPECTED,
    PRESENT,
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
#: Supplied **evidence objects** -- not folder contents. Finding N-2: the rejected-verdict hold
#: counts `LeafVerdict`s that A5 refused, and publishing that count in :data:`RECORDS` said "2
#: records (exact)" for two verdicts naming **one** path -- a path that need not be a record in
#: this folder at all. So it carries its own unit, and :func:`_uncertainty` never promotes it
#: into a record total -- because this unit is not in :data:`CONTENT_UNITS`, which is the
#: operative reason. It is **not** purely a fact about the *inputs* (finding X2-2): the
#: count's matched half is reached through the record join, which is why it is demoted to a
#: floor whenever that join could not cover the folder.
VERDICTS = "verdicts"
UNITS = (BYTES, RECORDS, ENTRIES, VERDICTS)

#: Units that count things **in the folder**. A figure in one of these answers for the subject; a
#: figure in :data:`VERDICTS` answers for what the caller handed in.
CONTENT_UNITS = (BYTES, RECORDS, ENTRIES)

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

#: Carried by every uncertainty figure once the folder has demonstrably changed under the pass.
#: Rule T-3: a count taken while its subject was moving describes the entries this pass examined,
#: never the folder. The wording is the one :func:`_demote_while_moving` applies to all of them, so
#: a reader cannot find two different accounts of the same concurrency on one page.
CONCURRENCY_LIMITATION = ("the folder changed while it was being read, so this counts only what "
                          "this pass examined")

#: Carried by an evidence-object hold whose record join could not see the whole folder, because
#: `RecordInventory.records` is a shortened *display* (finding X2-1). Deliberately a different
#: sentence from :data:`CONCURRENCY_LIMITATION`: a truncated display list is not a folder that
#: moved, and saying so would report a concurrency event that never happened.
TRUNCATED_JOIN_LIMITATION = (
    "the supplied record detail list is a truncated display, so a supplied verdict that only a "
    "record beyond it would have refused is not counted here")

#: Carried by the same hold when A3 did not certify that its aggregate counts cover the whole
#: folder. Separate from truncation, and not implied by it: `records_truncated` shortens only
#: the per-record display and leaves :attr:`counts_exact` true, so each has to be asked for
#: itself.
INEXACT_JOIN_LIMITATION = (
    "RecordInventory.counts_exact is False, so the record set this count was joined against is "
    "not certified to be the whole folder")

#: Why either of the two above makes the count a floor rather than a total.
#: :attr:`_Rejections.supplied_objects` is ``pre_match_objects + matched_objects``; only the
#: first addend is purely an input fact, and the second is reached through the record join, so
#: a record this pass never saw can only ever *raise* it. A floor is therefore never an
#: over-claim here, which is what makes the conservative rule safe.
REFUSAL_FLOOR_NOTE = (
    "a floor: a record this pass did not see could have matched a further supplied verdict and "
    "refused it, which would raise the matched half of this count")

#: Appended to that hold's population so the number is not read as a total of what the caller
#: supplied. The population sentence is what a consumer reads to find the denominator, so the
#: qualification belongs there and not only in a limitation.
REFUSAL_FLOOR_POPULATION = (
    "; and a floor rather than a total of them, because the matched half of this count was "
    "reached through a record detail list that does not cover the whole folder")

#: What a headline **zero** may still be hiding, one sentence per cause (finding X2-1). A zero
#: that only covers the part of the folder the questions could be put to is not a folder-wide
#: zero. The concurrency wording is preserved verbatim from before the fix: it is the one a
#: reader and this suite already know.
ZERO_CONCURRENCY_LIMITATION = ("the folder changed while it was being read, so entries it never "
                               "saw are not counted here")
ZERO_TRUNCATED_LIMITATION = ("the supplied record detail list is a truncated display, so records "
                             "beyond it were never put to these questions")
ZERO_INEXACT_LIMITATION = ("RecordInventory.counts_exact is False, so these questions were put "
                           "over counts that are not certified to cover the whole folder")

#: A1 leaf verdicts that **establish** a source state, as opposed to leaving it open. These are the
#: only two outcomes that answer "what is the state of this source file?", and the distinction is
#: load-bearing for bucket E: :data:`library_inventory.PRESENT` is a positive observation, not an
#: uncertainty, so a record whose source A1 saw must **not** become an uncertainty hold merely
#: because it is not missing. A cache whose every source is `PRESENT`, with every other question
#: answered, legitimately reports D = 0 and E = 0 as exact.
#:
#: ``MISSING_CONFIRMED`` is deliberately absent here: it establishes a state only when
#: :func:`_finalised_confirmed` also finds the four clauses, and that second gate is applied
#: separately so an enum carrying no clause evidence is *refused* rather than believed.
SOURCE_PRESENT = PRESENT

#: Published leaf verdicts that establish **nothing** about the source. ``MISSING_PROVISIONAL`` and
#: a non-final ``MISSING_CONFIRMED`` do not appear because `LeafVerdict.published` has already
#: mapped both to :data:`library_inventory.UNKNOWN` before A5 sees them -- which is exactly why
#: ``published`` is read and ``verdict`` never is.
SOURCE_UNRESOLVED = (UNKNOWN, ROOT_UNREACHABLE, ACCESS_DENIED, NOT_INSPECTED)


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
    def measurement_key(self) -> str:
        return str(getattr(self.measurement, "key", "") or "")

    @property
    def measurement_label(self) -> str:
        return str(getattr(self.measurement, "label", "") or "")

    @property
    def identity_conflict(self) -> str:
        """Does the supplied Measurement actually describe the supplied Category? ``""`` if so.

        `cache_admin.Measurement` carries its own ``key`` and ``expensive`` flag, and the caller
        pairs the two objects by hand. If they disagree, the pair is evidence about **something
        else**: a Measurement keyed ``analysis`` carries the Stage-5 folder's bytes, and filing it
        under a ``posters`` Category would publish those bytes under another category's label and
        root -- and, because only `Category.expensive` is consulted when splitting the cost axis,
        would classify the expensive analysis cache as cheaply regenerable poster art.

        A5 already refuses a bare Measurement and a Category with no absolute root. Refusing an
        incompatible *pair* is the same rule: a figure may only be published about a subject its
        evidence actually describes. A differing **label** is a presentational disagreement and is
        reported as a caveat rather than a refusal -- it cannot misattribute bytes on its own.
        """
        if self.category is None or self.measurement is None:
            return ""
        mkey, ckey = self.measurement_key, self.category_key
        if mkey and ckey and mkey != ckey:
            return (f"the measurement is keyed {mkey!r} but the category is {ckey!r}, so it is a "
                    f"measurement of a different category")
        if bool(getattr(self.measurement, "expensive", False)) != self.expensive:
            return (f"category {ckey or self.label!r} declares expensive="
                    f"{self.expensive} while its measurement declares "
                    f"expensive={bool(getattr(self.measurement, 'expensive', False))}, so the "
                    f"regeneration cost of these bytes is not established")
        return ""

    @property
    def label_disagreement(self) -> str:
        """A differing descriptive label: disclosed, never a refusal on its own."""
        if self.category is None or self.measurement is None:
            return ""
        clabel = str(getattr(self.category, "label", "") or "")
        mlabel = self.measurement_label
        if clabel and mlabel and clabel != mlabel:
            return (f"category {self.category_key!r} is labelled {clabel!r} but its measurement is "
                    f"labelled {mlabel!r}")
        return ""

    @property
    def usable(self) -> bool:
        return bool(self.category is not None and self.measurement is not None and self.key
                    and not self.identity_conflict)


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
        elif cat.identity_conflict:
            refusals.append(
                f"category observation {index} pairs a Category and a Measurement that do not "
                f"describe the same thing: {cat.identity_conflict}")
        elif cat.measurement is not None and not cat.observed_at:
            caveats.append(
                f"category {cat.label!r} was supplied without an observation time; "
                f"cache_admin.Measurement carries none of its own")
        if cat.label_disagreement:
            caveats.append(cat.label_disagreement)

    # A category key is the only name a consumer has for a category figure, so two different roots
    # sharing one key leave every such figure ambiguous even though each figure's own `subject` is
    # still truthful. The individual observations are kept; the group subtotal is not.
    seen_keys: dict = {}
    for cat in cats:
        if not cat.usable:
            continue
        previous = seen_keys.get(cat.category_key)
        if previous is not None and previous != cat.key:
            caveats.append(
                f"category key {cat.category_key!r} was supplied twice for different roots "
                f"({previous} and {cat.key}), so that key does not identify one category")
        seen_keys.setdefault(cat.category_key, cat.key)

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
    if not cat.observed_at:
        # Rule T-2. `cache_admin.Measurement` carries no timestamp of its own, so a figure built
        # from one has no observation window unless the caller supplies it -- and a byte count with
        # no "as at when" is not an observation of anything in particular. `_flat_figure` already
        # refuses the identical omission; the two measurement paths must not disagree about whether
        # the same missing fact is fatal. A4's own `observed_at` is NOT substituted: it timestamps
        # a location selection, not this walk.
        return withheld("no observation time was supplied for this measurement", unit=BYTES,
                        axis=axis, subject=subject, basis=basis,
                        notes=(f"category {cat.label!r}",
                               "cache_admin.Measurement carries no timestamp of its own, so the "
                               "caller must supply one"))
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


def _flat_subject(observation: Observation) -> str:
    """The folder an `analysis_cache.Measurement` is actually about -- its own ``path``.

    Not the report's primary subject. When the primary subject came from A4 and this measurement
    describes a different folder, the combined figures are withheld, but this figure survives as a
    statement about its own folder -- so it must carry *its own* key or it would publish one
    folder's bytes under another folder's name.
    """
    m = observation.flat_measurement
    return _key(str(getattr(m, "path", "") or "")) or "an unestablished folder"


def _inventory_subject(inv) -> str:
    """The folder an A3 `RecordInventory` is actually about -- its own resolved location."""
    if inv is None:
        return "an unestablished folder"
    return (_key(str(getattr(inv, "resolved", "") or ""))
            or _key(str(getattr(inv, "path", "") or ""))
            or "an unestablished folder")


def _location_subject(location) -> str:
    """The folder an A4 `CacheLocationReport` observation is about."""
    if location is None:
        return "an unestablished folder"
    chosen = getattr(location, "selected", None)
    return (_key(str(getattr(chosen, "real", "") or ""))
            or _key(str(getattr(location, "selected_path", "") or ""))
            or "an unestablished folder")


def _configured_subject(location) -> str:
    """The folder a *configured shared folder* observation is about -- A4's configured path.

    Finding N-3. This hold was given :func:`_location_subject`, A4's **selected** folder. In the
    case the hold exists to report, those are different folders and the selected one is the
    healthy one: sharing was configured, the shared root came back ``ROOT_UNREACHABLE``, and A4
    fell back to the local default. The report then said the configured shared folder was
    unreachable and named the one folder that demonstrably was not.

    ``configured_shared`` is the only path A4 carries for that folder, and it is already on
    :attr:`FolderIdentity.path_as_configured`, so nothing had to be resolved to fix this. A blank
    or relative configured path yields no subject rather than silently falling back to the
    selected folder -- naming the wrong folder is the defect, and naming none is the honest
    alternative.
    """
    if location is None:
        return "an unestablished folder"
    return (_key(str(getattr(location, "configured_shared", "") or ""))
            or "an unestablished folder")


def _location_window(location) -> tuple:
    """A4's **own** observation time, for a figure derived from A4.

    Finding N-4. :attr:`Observation.window` is A3's ``(observed_from, observed_to)`` whenever an
    inventory was supplied, and the A4-derived hold inherited it -- attributing A3's record pass
    over one folder to A4's observation of another. The two measurement figures already carry
    their own supplied stamps; this closes the last borrowed window. When A4 supplied no time,
    the hold carries none and says so rather than taking a neighbour's.
    """
    if location is None:
        return ("", "")
    stamp = str(getattr(location, "observed_at", "") or "")
    return (stamp, stamp) if stamp else ("", "")


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
    keys: dict = {}
    for c in cats:
        if c.category_key in keys and keys[c.category_key] != c.key:
            return False, (f"category key {c.category_key!r} was supplied for two different roots, "
                           f"so the group it names is not well defined")
        keys.setdefault(c.category_key, c.key)
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


def _cost_overlaps(cats: tuple) -> tuple:
    """``((reason, involves_cheap), ...)`` for every pair of category roots that is not separate.

    :func:`_disjoint` answers "may the **cheap** figures be added together?" and therefore only
    ever sees the cheap categories. That is not the whole question, because `cache_admin.measure`
    **walks**: if the expensive Stage-5 root lies *inside* a cheap root, the cheap root's byte count
    already contains the expensive bytes, and a "non-expensive categories, taken together" subtotal
    silently reports expensive storage as cheaply regenerable. No double-count occurs *within* the
    cheap group, so the within-group test cannot see it -- which is why this runs across **all**
    supplied categories regardless of cost tier.

    Containment and equality are pure lexical component work (`commonpath`, never `startswith`), so
    no provenance is needed for them and A5 still resolves nothing. A supplied ``storage_key``
    collision is also reported, because two paths the caller says are one storage are one storage
    whatever the strings look like. Both directions of containment are reported: whichever way the
    nesting runs, the cost split is not a partition of distinct bytes.
    """
    out: list = []
    for index, outer in enumerate(cats):
        for inner in cats[index + 1:]:
            involves_cheap = (not outer.expensive) or (not inner.expensive)
            pair = f"{outer.label!r} ({outer.key}) and {inner.label!r} ({inner.key})"
            if outer.key == inner.key:
                out.append((f"{pair} name the same folder, so their bytes are one quantity "
                            f"reported twice, not two quantities", involves_cheap))
            elif _contained(outer.key, inner.key):
                out.append((f"{inner.label!r} lies inside {outer.label!r}, so {outer.label!r}'s "
                            f"measured bytes already include {inner.label!r}'s", involves_cheap))
            elif _contained(inner.key, outer.key):
                out.append((f"{outer.label!r} lies inside {inner.label!r}, so {inner.label!r}'s "
                            f"measured bytes already include {outer.label!r}'s", involves_cheap))
            elif outer.storage_key and outer.storage_key == inner.storage_key:
                out.append((f"{pair} were supplied with the same storage provenance, so they are "
                            f"one storage under two paths", involves_cheap))
    return tuple(out)


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


def _source_signature(item) -> tuple:
    """Everything about a verdict that A5's acceptance of it actually depends on.

    Two verdicts for one source path are in conflict unless they agree on **all** of this.
    Comparing `LeafVerdict.published` alone is not sufficient, and that was finding F-2: a
    confirmation carrying its four clauses and one carrying none publish the *same* enum while
    being, to A5, opposite evidence -- the first establishes a missing source, the second is refused
    outright. With only ``published`` compared, no contradiction was detected and a plain
    last-write-wins assignment let **whichever verdict arrived last** decide whether D read
    ``1 (exact)`` or ``0 (lower bound)``.

    So the signature carries every field the two independent gates consult -- the published answer,
    finalisation, the clause evidence and the root provenance -- and a difference in any of them is
    a disagreement A5 refuses rather than resolves.

    **Disposition on N-7, accepted as-is.** Two verdicts can share this signature and still differ
    in *which* clauses they left unmet: ``all_four`` is a single boolean, so ``parent_listed``
    alone and ``name_absent`` alone both reduce to ``False``. Those two are not a contradiction --
    A5 refuses both for the same reason, and the refusal is identical -- but the first-seen one is
    the one kept, so the ``Clauses.unmet()`` list quoted in the rejection note depends on arrival
    order. Measured on both orders: D is ``0 / LOWER_BOUND`` either way, the rejected count is 1
    either way, and the unit, population, subject, exactness and every other published field agree.
    **No figure is order-dependent**; what varies is which of two equally true explanations of the
    same refusal is quoted. Pinning an order here would mean ranking unmet-clause sets against each
    other, which is a judgement about A1's evidence that A5 is in no position to make, so the note
    is left as the first-seen one and this paragraph is the disclosure.
    """
    clauses = getattr(item, "clauses", None)
    return (str(getattr(item, "published", "") or ""),
            bool(getattr(item, "is_final", False)),
            bool(getattr(clauses, "all_four", False)),
            str(getattr(item, "root_key", "") or ""))


@dataclass(frozen=True)
class _Rejections:
    """What A5 refused, counted in the cardinalities that are genuinely different (finding N-8).

    One flat list used to carry both, and ``len()`` of it was published as an exact count of
    *supplied objects*. It never was. Four of the five append sites fire once per supplied entry and
    the fifth fires once per **matching cache record**, so the published number drifted in both
    directions at once. Measured on the parent commit:

    ======================================== ============== ==============
    shape                                    true entries   published
    ======================================== ============== ==============
    1 entry, 3 records sharing its path      1              3  (over)
    2 equal-signature entries, 1 record      2              1  (**under**)
    2 equal-signature entries, 3 records     2              3  (over)
    the same entry object supplied twice     2              1  (**under**)
    ======================================== ============== ==============

    The under-count is the half the review did not see, and it has its own cause: the indexing loop
    assigns ``by_path[key]`` only when ``previous is None``, so a second entry with an *equal*
    signature takes neither that branch nor the contradiction branch. It remains a supplied entry
    with no representative in the map. That is also why counting ``len(refused_keys)`` -- the
    review's suggested minimal fix -- would still publish 1 for two equal-signature entries: a key
    is not an entry. So the entries naming each key are counted as they arrive.

    Why these three numbers and not one: ``pre_match_objects`` answers "how many things you handed
    me could not be used at all", ``matched_objects`` answers "how many of them were refused once
    joined to a record", and ``record_events`` answers "how many record-level refusals that caused".
    The last is a different unit of thing from the first two and is never added to them.
    """

    #: Supplied input entries rejected before any join was attempted. One per entry, always.
    pre_match_objects: int = 0
    #: Supplied input entries refused at the record join. Counted once per entry, **not** once per
    #: matching record: a single bad verdict joined onto three records sharing a source path is one
    #: refused object and three refusal events.
    matched_objects: int = 0
    #: Record-level refusal **events**. Real information, in its own right, in its own unit.
    record_events: int = 0
    #: ``(why, what)`` pairs, one per rejection *group*, with multiplicity stated in the text
    #: rather than by repeating the line once per matching record.
    notes: tuple = ()

    @property
    def supplied_objects(self) -> int:
        """The input-entry population: what a :data:`VERDICTS` figure is allowed to publish."""
        return self.pre_match_objects + self.matched_objects

    def __bool__(self) -> bool:
        """True when anything was refused, so ``not rejected`` keeps its original meaning."""
        return bool(self.pre_match_objects or self.matched_objects or self.record_events)


def _source_state_figure(obs: Observation, inv, subject, window) -> tuple:
    """``(Figure, rejected, holds)`` for axis D. Never generates a verdict of its own.

    ``holds`` is the source axis's contribution to bucket E, and populating it is not optional.
    A record whose source state was never established *is* an uncertainty, and reporting it nowhere
    was finding F-1: E then published ``0 records (exact)`` -- "every uncertainty question that was
    put returned zero" -- over a question nobody had answered, in the worst shape alongside a D
    reading ``not assessed``.

    Only two outcomes **establish** a source state: :data:`library_inventory.PRESENT`, and a
    finalised confirmation that also carries its four clauses. ``PRESENT`` therefore contributes no
    hold at all -- it is a positive observation, not a doubt, and a cache whose every source A1 saw,
    with every other question answered, legitimately reports D = 0 and E = 0 as exact. Everything
    else -- an unresolved answer, a record no verdict covered, a recorded path A5 cannot join -- is
    counted and labelled rather than absorbed.
    """
    basis = ("library_inventory.LeafVerdict.published == MISSING_CONFIRMED, joined onto "
             "RecordInventory.records[].source_path")
    verdicts = obs.source_verdicts
    #: One entry per supplied input entry refused before any join (finding N-8). Deliberately
    #: separate from the record-level refusals below, which are events and not objects.
    pre_match: list = []
    holds: list = []

    records = tuple(getattr(inv, "records", ()) or ()) if inv is not None else ()
    truncated = bool(getattr(inv, "records_truncated", False))
    counts_exact = bool(getattr(inv, "counts_exact", False))
    unstable = bool(getattr(inv, "unstable", False))
    parsed = int(getattr(inv, "records_parsed", 0) or 0)
    recognised = int(getattr(inv, "records_recognised", 0) or 0)

    # --- which records could carry a source state at all (finding F-9) --------------------------
    # A record with no recorded ``video_file`` has no source, so no source of its own can be
    # missing: excluding it is correct and it raises no doubt. A record that recorded a path A5
    # cannot form a key from is different -- A3 may well have resolved and scoped that path against
    # its own verified root, so the record is one whose source state is simply unknown *here*, and
    # it must be disclosed rather than silently dropped out of the denominator.
    eligible: list = []
    unjoinable = 0
    no_source = 0
    for rec in records:
        text = str(getattr(rec, "source_path", "") or "").strip()
        if not text:
            no_source += 1
            continue
        key = _key(text)
        if key:
            eligible.append((rec, key))
        else:
            unjoinable += 1

    # Note on phrasing throughout this function: a count is written *after* its noun
    # ("records in the supplied detail list: 10"), never as "10 records". A cross-cutting guard
    # asserts that no line in the report presents an F+G cross-axis total, and it does so by
    # looking for the literal sum as "<n> records" -- so a population string that happens to
    # mention the same number in that shape would trip it and read as the very double-count the
    # guard exists to forbid.
    if unjoinable:
        holds.append((
            "uncertain: records whose recorded source path A5 cannot join to a verdict",
            Figure(value=unjoinable, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject,
                   population=f"records in the supplied detail list: {len(records)}",
                   basis="RecordInventory.records[].source_path that is not an absolute path",
                   window=window,
                   notes=("A3 resolves a recorded path against its own verified root; A5 resolves "
                          "nothing, so without supplied absolute provenance it cannot join these "
                          "to a source verdict",))))

    def axis_withheld(reason):
        """Withhold D, and -- when there was something to answer for -- withhold an E hold too."""
        if eligible or unjoinable:
            holds.append((
                "uncertain: the source state of these records was not established",
                withheld(reason, unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject, basis=basis,
                         window=window,
                         notes=("records carrying a source path A5 could join: "
                                f"{len(eligible)}",))))
        # Every early return happens before the record join, so only pre-match entries can exist
        # here and there are no record-level events to report.
        return (withheld(reason, unit=RECORDS, axis=AXIS_SOURCE, subject=subject, basis=basis,
                         window=window),
                _Rejections(pre_match_objects=len(pre_match),
                            notes=tuple(sorted(pre_match))), tuple(holds))

    if not verdicts:
        return axis_withheld("no finalised source verdict was supplied, so nothing is known about "
                             "any source file")

    root_keys = _verified_root_keys(inv) if inv is not None else ()
    if not root_keys:
        return axis_withheld("no verified inspection root was supplied, so no source verdict has "
                             "jurisdiction over records in this folder")

    # --- index the supplied verdicts by path, refusing contradictions rather than picking one ----
    by_path: dict = {}
    signatures: dict = {}
    contradictions: list = []
    #: How many validated input **entries** named each key. Finding N-8: ``by_path`` cannot answer
    #: this, because an equal-signature successor is never stored in it, and the same object
    #: supplied twice is two entries either way. Incremented once per arriving entry, so a later
    #: refusal of that key can be charged to every entry that asked for it.
    entries_for_key: dict = {}
    for item in verdicts:
        if not isinstance(item, LeafVerdict):
            pre_match.append(("not a LeafVerdict", repr(type(item).__name__)))
            continue
        key = _key(getattr(item, "path", ""))
        if not key:
            pre_match.append(("no usable absolute path", str(getattr(item, "path", ""))))
            continue
        vroot = str(getattr(item, "root_key", "") or "")
        if vroot and vroot not in root_keys:
            pre_match.append(("root provenance not among the verified inspection roots",
                              f"{item.path} (root_key={vroot})"))
            continue
        if not vroot:
            pre_match.append(("no root provenance, so jurisdiction cannot be established",
                              str(item.path)))
            continue
        entries_for_key[key] = entries_for_key.get(key, 0) + 1
        signature = _source_signature(item)
        previous = signatures.get(key)
        if previous is None:
            signatures[key] = signature
            by_path[key] = item
        elif previous != signature:
            # Flagged, and the first-seen item is deliberately NOT replaced: the outcome is a
            # refusal either way, so no surviving assignment can depend on arrival order.
            contradictions.append(key)

    if contradictions:
        return axis_withheld("two supplied verdicts disagree about the same source path, and A5 "
                             "does not choose between them: "
                             + ", ".join(sorted(set(contradictions))))

    if not eligible:
        return axis_withheld("no record in the supplied inventory carries a usable source path, so "
                             "no source verdict can be joined to one")

    confirmed = 0
    present = 0
    unmatched = 0
    unresolved = 0
    refused = 0
    #: The distinct keys refused at the join, first-seen item kept for the note. Finding N-8: this
    #: is keyed so that three records sharing one source path produce three *events* but charge the
    #: refusal to that key's supplied entries exactly once.
    refused_keys: dict = {}
    for rec, key in eligible:
        item = by_path.get(key)
        if item is None:
            unmatched += 1
            continue
        if _finalised_confirmed(item):
            confirmed += 1
            continue
        published = str(getattr(item, "published", "") or "")
        if published == SOURCE_PRESENT:
            present += 1
        elif published == MISSING_CONFIRMED:
            refused += 1
            if key not in refused_keys:
                refused_keys[key] = item
        else:
            unresolved += 1

    # The two populations, each counted directly rather than by subtracting one cardinality from
    # the other (finding N-8, requirement 8). `len(rejected) - refused` mixed objects with events:
    # it read 1 pre-match rejection out of two equal-signature entries over one record, and could
    # in principle go negative.
    matched_objects = sum(entries_for_key.get(k, 1) for k in refused_keys)
    rejection_notes = list(pre_match)
    for key in sorted(refused_keys):
        item = refused_keys[key]
        entries = entries_for_key.get(key, 1)
        # Multiplicity is stated rather than shown by repeating the line, which is what printed the
        # same object once per matching record and, past eight, hid the rest behind the cap.
        shared = "" if entries == 1 else f" [supplied entries naming this path: {entries}]"
        rejection_notes.append(
            ("a confirmed-missing enum without its finalised clause evidence",
             f"{item.path} (unmet: "
             f"{','.join(item.clauses.unmet()) or 'none recorded'}){shared}"))
    rejected = _Rejections(pre_match_objects=len(pre_match), matched_objects=matched_objects,
                           record_events=refused, notes=tuple(sorted(rejection_notes)))

    if unresolved:
        holds.append((
            "uncertain: records whose supplied source verdict established no state",
            Figure(value=unresolved, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject,
                   population=f"records with a joinable source path: {len(eligible)}",
                   basis="library_inventory.LeafVerdict.published outside (PRESENT, "
                         "MISSING_CONFIRMED)",
                   window=window,
                   notes=("a provisional or non-final missing verdict publishes as UNKNOWN, and an "
                          "unreachable, access-denied or uninspected source establishes nothing "
                          "either",))))
    if unmatched:
        holds.append((
            "uncertain: records with a source path but no supplied source verdict",
            Figure(value=unmatched, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                   subject=subject,
                   population=f"records with a joinable source path: {len(eligible)}",
                   basis="RecordInventory.records[].source_path with no matching LeafVerdict",
                   window=window)))

    shortfall = parsed - len(records)
    limits: list = []
    if truncated:
        limits.append("RecordInventory.records is a truncated display, so records beyond it could "
                      "not be joined to a verdict")
    if not counts_exact:
        limits.append("RecordInventory.counts_exact is False")
    if unmatched:
        limits.append(f"{unmatched} eligible records had no supplied source verdict")
    if unresolved:
        limits.append(f"{unresolved} eligible records had a supplied verdict that established no "
                      f"source state")
    if unjoinable:
        limits.append(f"{unjoinable} records recorded a source path A5 could not join to a verdict")
    if no_source:
        limits.append(f"{no_source} records recorded no source path at all, so no source of theirs "
                      f"can be missing")
    if not truncated and shortfall > 0:
        limits.append(f"{shortfall} parsed records are absent from the supplied detail list")
    if unstable:
        limits.append("the folder changed while it was being read")
    if recognised > parsed:
        limits.append(f"{recognised - parsed} recognised records were never parsed, so they "
                      f"recorded no source path A5 could read and are outside this figure's "
                      f"population entirely")
    if rejected:
        # Finding N-5. Two different counts were both called "refused": one counts verdicts that
        # matched a record and lacked their clause evidence, the other also covers verdicts thrown
        # out before any matching happened. The figure printed "refused as evidence 0" and, on the
        # same page, "1 supplied verdicts were rejected as evidence". Both numbers were right; the
        # shared word made them read as a contradiction. Each is now named for the question it
        # answers.
        #
        # Finding N-8 then made the numbers themselves true. Every count here is tracked directly:
        # the object counts never subtract a record-event count, and the event count is reported as
        # an event count instead of being labelled "matched verdicts".
        limits.append(f"{rejected.supplied_objects} supplied verdict objects were rejected as "
                      f"evidence ({rejected.matched_objects} matched a record but carried no "
                      f"finalised clause evidence, refused at record level "
                      f"{rejected.record_events} times; {rejected.pre_match_objects} were "
                      f"rejected before any record was matched)")

    population = (
        f"source states established: {confirmed + present} of {len(eligible)} joinable "
        f"(confirmed missing {confirmed}, present {present}); unresolved {unresolved}, "
        f"unmatched {unmatched}, matched verdict objects refused for missing clause evidence "
        f"{rejected.matched_objects}, record-level refusals of those objects "
        f"{rejected.record_events}, supplied verdict objects rejected before matching "
        f"{rejected.pre_match_objects}, "
        f"unjoinable {unjoinable}, recording no source path {no_source}; in this folder parsed "
        f"{parsed}, recognised {recognised}")

    # EXACT means "this is the count for the whole source-state question in this folder". That
    # needs every record in the population to have been answered -- not merely every verdict that
    # happened to be supplied to have been consumed.
    #
    # Disposition on N-6, stated rather than left implicit. This test deliberately does **not**
    # carry F's and G's ``parsed == recognised`` term, and the difference is a real one rather than
    # an oversight. F and G read A3 counters that A3 bumps once per *parsed* record, so an unparsed
    # record is a record the question was put to and lost -- their population genuinely falls short
    # of the folder. D is reachable only by joining a verdict onto a ``source_path`` that A3 read
    # out of a record's payload, and an unparsed record has no readable payload at all: it could
    # not carry a source path even in principle, so it is not a shortfall in D's population but
    # outside it. D may therefore stay exact for the population it names, and what N-6 actually
    # required is that the *wording* stop implying a folder total. So the label now names the
    # population the count covers, and the unparsed remainder is disclosed as a limitation on the
    # exact figure too -- D's value and exactness are unchanged.
    complete_coverage = (counts_exact and not truncated and not unstable and not rejected
                         and unmatched == 0 and unresolved == 0 and unjoinable == 0
                         and shortfall <= 0)
    if complete_coverage:
        return (Figure(value=confirmed, unit=RECORDS, exactness=EXACT, axis=AXIS_SOURCE,
                       subject=subject,
                       population=population + " (complete coverage of the records whose source "
                                               "state could be established here, which is the "
                                               "population this figure answers for)",
                       basis=basis, window=window, limitations=tuple(limits)),
                rejected, tuple(holds))
    return (Figure(value=confirmed, unit=RECORDS, exactness=LOWER_BOUND, axis=AXIS_SOURCE,
                   subject=subject, population=population, basis=basis, window=window,
                   limitations=tuple(limits),
                   notes=("a floor: records whose source state was not established could also "
                          "qualify",)),
            rejected, tuple(holds))


# ---------------------------------------------------------------------------------------------
# axis E -- separately labelled holds
# ---------------------------------------------------------------------------------------------

def _demote_while_moving(holds: tuple, unstable: bool) -> tuple:
    """The **one** place concurrency demotes an uncertainty figure (Rule T-3).

    Finding N-1. This rule used to live inside :func:`_uncertainty`'s ``inv_figure`` builder, which
    meant it protected exactly the holds that happened to be built there. The four figures bucket E
    gained with the F-1 fix are built elsewhere -- an unjoinable recorded source path, an unresolved
    supplied verdict, a record with no matching verdict, and rejected verdict evidence -- so all
    four were constructed ``EXACT`` and reached the headline untouched. Measured on a folder with
    ``vanished=('x.json',)``: D, F, G and all five inventory-derived holds were correctly
    ``LOWER_BOUND``, while the source hold stayed ``EXACT`` and ``_uncertainty``'s
    single-contributor passthrough copied that into ``E uncertain: 1 records (exact)`` with
    :attr:`Figure.publishable_as_total` **True** -- three lines from the report's own "every
    record-level figure here is a floor" caveat.

    So the rule is applied to the **assembled list** rather than to each producer. That is the
    point: a hold added later cannot bypass a rule that runs over all of them, and there is one
    wording for the limitation instead of one per builder.

    Two things are deliberately left alone, and one was corrected by finding X2-1. A figure
    that is already a floor keeps its own **exactness** and its own reason -- it is not
    re-demoted and not relabelled. It does now also gain the concurrency *reason*, because
    the folder having moved is true of that figure whether or not something else had already
    made it a floor: with the reason suppressed, a hold that was a floor for its own cause
    published one of two applicable causes and read as though the other had not happened.
    Stating a reason and demoting an exactness are therefore two decisions, not one. And a
    **withheld** figure stays withheld: it carries no number, so there is nothing to demote,
    and overwriting its ``NOT_ASSESSED`` would break :attr:`Figure.consistent`.

    **The evidence-object hold stays in scope, decided rather than inherited (finding N-8).** The
    question was put directly: a count of caller-supplied objects is a fact about the *inputs*, and
    a folder moving under A3's pass cannot change how many verdicts the caller handed in, so why
    should it become a floor? Because the published figure is
    :attr:`_Rejections.supplied_objects`, which is ``pre_match_objects + matched_objects``, and only
    the first term is purely an input fact. ``matched_objects`` is reached through the record join:
    a record this pass never saw could have matched a supplied verdict and refused it, which would
    raise the term. One of the two addends is therefore a floor whenever the folder moved, and a sum
    containing a floor is a floor. So the hold is demoted, and the rule needs no exemption
    mechanism and no per-unit special case -- which is also what keeps finding N-1 closed: Rule T-3
    still runs over every hold, unconditionally, in exactly one place.

    Had the figure been split into two published numbers, the pre-match half would have a genuine
    claim to staying exact. It is deliberately not split: one number answering "how much of what
    you supplied was unusable" is the more useful report, and a floor is the honest exactness for
    it.

    **Finding X2-1 extended that same argument to two causes this function does not own.** A
    folder that moved is not the only way the record join can fall short of the folder: a
    truncated record *display* and uncertified aggregate counts do it too, without any
    concurrency event at all. Those are producer facts rather than concurrency, so the hold
    is built as a floor where it is built -- see :func:`_uncertainty` -- and this function is
    left owning exactly one rule. Pre-attaching the concurrency sentence at a producer is
    what finding N-1 closed, so it is not done, and this is why an already-floor figure must
    still collect the reason here.
    """
    if not unstable:
        return tuple(holds)
    out: list = []
    for label, fig in holds:
        if fig.value is None:
            out.append((label, fig))
            continue
        limits = (fig.limitations if CONCURRENCY_LIMITATION in fig.limitations
                  else fig.limitations + (CONCURRENCY_LIMITATION,))
        out.append((label, replace(fig, exactness=(LOWER_BOUND if fig.exactness == EXACT
                                                   else fig.exactness),
                                   limitations=limits)))
    return tuple(out)


def _uncertainty(obs: Observation, inv, subject, window, rejected, source_holds=()) -> tuple:
    """``(headline, observations)``. Separate labels, separate units, deliberately no sum.

    Clarification C-3: the uncertainty category stays visible, but a visible category does not imply
    a known number. These members can refer to the **same** underlying entries -- an unclassified
    directory entry may be the same thing as a skipped one -- and they are not even all in the same
    unit, so adding them would invent a population. The headline is therefore published only when
    exactly one member contributes, and withheld otherwise.

    **Every hold names the folder it is actually about** (finding F-3). The report's primary subject
    is established from A4 when A4 is supplied, and an A3 inventory of a *different* folder still
    contributes its own observations here -- correctly, because refusing them would destroy evidence
    rather than qualify it. Labelling them with the primary subject published one folder's numbers
    under another folder's name, which is the very thing the cross-folder refusal exists to prevent.
    So an inventory-derived hold carries the inventory's key and an A4-derived hold carries A4's.
    """
    out: list = []
    inv_subject = _inventory_subject(inv) if inv is not None else subject
    inv_unstable = bool(getattr(inv, "unstable", False)) if inv is not None else False

    # Finding X2-1. Three independent ways the record join this pass could perform fell short
    # of the folder, each named for itself. `records_truncated` shortens the per-record
    # *display* and leaves the aggregate counters whole; `counts_exact` answers for those
    # counters; `unstable` is a concurrency event. Telling a reader the folder changed under
    # the pass because a display list was capped would report an event that did not happen,
    # so the three never share one sentence. `counts_exact` defaults to False for the same
    # reason it does in `_source_state_figure`: an input that cannot answer the question is
    # not an input that answered it affirmatively.
    if inv is None:
        inv_truncated = False
        inv_counts_exact = True
    else:
        inv_truncated = bool(getattr(inv, "records_truncated", False))
        inv_counts_exact = bool(getattr(inv, "counts_exact", False))

    #: Why a count reached *through* the capped record detail list is a floor. Deliberately
    #: carries no concurrency sentence: :func:`_demote_while_moving` owns that, once, over the
    #: assembled hold list, and duplicating the decision in a producer is exactly what
    #: finding N-1 closed.
    join_incomplete = tuple(reason for applies, reason in (
        (inv_truncated, TRUNCATED_JOIN_LIMITATION),
        (not inv_counts_exact, INEXACT_JOIN_LIMITATION)) if applies)

    #: What a headline zero may still be hiding. The concurrency cause **is** named here,
    #: because the headline is not a hold: it is chosen after the demotion pass and
    #: `_demote_while_moving` never sees it, so N-1's single call site is unaffected.
    zero_doubts = tuple(reason for applies, reason in (
        (inv_unstable, ZERO_CONCURRENCY_LIMITATION),
        (inv_truncated, ZERO_TRUNCATED_LIMITATION),
        (not inv_counts_exact, ZERO_INEXACT_LIMITATION)) if applies)

    def add(label, figure):
        out.append((label, figure))

    def inv_figure(value, unit, basis, population, notes=()):
        """An inventory-derived count, carrying the inventory's own folder as its subject.

        It builds the figure at face value and does **not** decide the concurrency question:
        :func:`_demote_while_moving` does that for every hold at once, which is what finding N-1
        was about. Deciding it here protected only the holds built by this one helper.
        """
        return Figure(value=value, unit=unit, exactness=EXACT, axis=AXIS_UNCERTAIN,
                      subject=inv_subject, population=population, basis=basis, window=window,
                      notes=tuple(notes))

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
            inv_figure(unclassified, ENTRIES, "RecordInventory.entries_unclassified",
                       "entries examined by this pass"))
        add("uncertain: directory entries deliberately skipped",
            inv_figure(skipped, ENTRIES, "RecordInventory.skipped_entries",
                       "entries examined by this pass"))
        add("uncertain: recognised records that could not be parsed",
            inv_figure(max(recognised - parsed, 0), RECORDS,
                       "RecordInventory.records_recognised - records_parsed",
                       f"{recognised} recognised records"))
        add("uncertain: records whose completion authority would not answer",
            inv_figure(unavailable, RECORDS, "RecordInventory.completion_unavailable",
                       f"{parsed} parsed records"))
        if scope_assessed and isinstance(scope_counts, dict):
            add("uncertain: records whose recorded source path could not be resolved",
                inv_figure(int(scope_counts.get(SCOPE_UNKNOWN, 0) or 0), RECORDS,
                           f"RecordInventory.scope_counts[{SCOPE_UNKNOWN}]",
                           f"{_counter_total(scope_counts)} records scoped"))
        else:
            add("uncertain: source scope was not assessed",
                withheld("scope was not assessed for this folder, so the number of unscoped "
                         "records is unknown", unit=RECORDS, axis=AXIS_UNCERTAIN,
                         subject=inv_subject, basis="RecordInventory.scope_assessed",
                         window=window))
        if unverified:
            add("uncertain: roots supplied without a positive observation",
                inv_figure(len(unverified), ENTRIES, "RecordInventory.unverified_roots",
                           "roots supplied to the record pass",
                           notes=("these took no part in any scope verdict",)))
        if not entries_complete:
            add("uncertain: the enumeration did not reach the end of the folder",
                withheld("the entry loop stopped early, so the number of unseen entries is "
                         "unknown", unit=ENTRIES, axis=AXIS_UNCERTAIN, subject=inv_subject,
                         basis="RecordInventory.entries_complete", window=window))
    else:
        add("uncertain: no record inventory was supplied",
            withheld("no record inventory was supplied, so nothing about this folder's records is "
                     "known", unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject,
                     basis="Observation.inventory"))

    for label, figure in tuple(source_holds or ()):
        add(label, figure)

    if obs.location is not None:
        shared = getattr(obs.location, "shared", None)
        verdict = str(getattr(shared, "verdict", "") or "")
        if verdict in (ROOT_UNREACHABLE, ACCESS_DENIED, UNKNOWN):
            # Findings N-3 and N-4: this hold is about the **configured shared** folder, so it
            # names that folder and carries A4's own observation time -- not the selected folder,
            # which in the fallback case is the healthy one, and not A3's record-pass window.
            loc_window = _location_window(obs.location)
            add(f"uncertain: the configured shared folder reported {verdict}",
                withheld(f"A1's verdict for the configured shared folder was {verdict}, so its "
                         f"contents were not established", unit=RECORDS, axis=AXIS_UNCERTAIN,
                         subject=_configured_subject(obs.location),
                         basis="CacheLocationReport.shared.verdict", window=loc_window,
                         notes=(() if any(loc_window) else
                                ("A4 supplied no observation time, so this hold carries none "
                                 "rather than borrowing another input's",))))

    if rejected:
        # Finding N-2. This counts supplied verdict OBJECTS. Two rejected verdicts can name one
        # path, and a rejected path need not be a record in this folder at all, so this is not a
        # count of records and is not published as one. A5 cannot derive a unique record population
        # from it either -- deduplicating by path would still be counting paths it cannot place in
        # the inventory -- so the honest figure is the evidence-object count, in its own unit, on
        # its own line.
        #
        # Finding N-8 is why the value is `supplied_objects` and not a length. The old `len()` was
        # over one list fed at two cardinalities, so it published the number of record-level
        # refusal *events* whenever a source path was shared, and dropped equal-signature entries
        # entirely. `supplied_objects` is the input-entry population and nothing else.
        #
        # Finding X2-1 is why its exactness is now conditional. `supplied_objects` is
        # `pre_match_objects + matched_objects`, and `matched_objects` is reached by matching
        # supplied entries against `RecordInventory.records` -- a capped *display* list, not
        # necessarily every record in the folder. So whenever that list is truncated, or the
        # aggregate counts are not certified, a supplied verdict that only an unseen record
        # would have refused is simply absent from this number. Published `EXACT` with an
        # empty `limitations` tuple, it read as a complete count of what the caller handed in.
        # Measured on the parent commit, identical supplied verdicts over one folder:
        # the refused record inside the display window gave `1 verdicts (exact)` with
        # `publishable_as_total` True, and beyond it gave no hold at all -- a difference in
        # where a cap fell, published as a difference in fact. Axis D already weighed all
        # three states in `complete_coverage`; its limitations never reached here. The
        # pre-match half of the sum genuinely is exact in isolation, but the published figure
        # is the sum, and a sum with a floor in it is a floor.
        #
        # The rule is deliberately unconditional on the two flags rather than reasoning about
        # whether an unseen record *could* have matched a surviving refusable verdict. That
        # second join is a question about records A5 was never shown, and getting it wrong
        # would publish a total; a floor, by contrast, is sound in every case, because an
        # unseen match can only raise `matched_objects` and never lower it.
        add("uncertain: supplied source verdicts rejected as evidence",
            Figure(value=rejected.supplied_objects, unit=VERDICTS,
                   exactness=(LOWER_BOUND if join_incomplete else EXACT),
                   axis=AXIS_UNCERTAIN,
                   subject=inv_subject,
                   population="supplied verdict objects that A5 refused as evidence; not a count "
                              "of distinct cache records, not a count of record-level refusals, "
                              "and not necessarily records of this folder at all"
                              + (REFUSAL_FLOOR_POPULATION if join_incomplete else ""),
                   basis="A5 rejection of unusable source verdicts", window=window,
                   limitations=join_incomplete,
                   # Sliced BEFORE the floor note is appended, so a hold already carrying eight
                   # rejection notes cannot push the reason it is a floor off the end.
                   notes=tuple(f"{why}: {what}" for why, what in rejected.notes)[:8]
                         + ((REFUSAL_FLOOR_NOTE,) if join_incomplete else ())))

    # Rule T-3, enforced once over every hold rather than per producer (finding N-1). This runs
    # before the headline is chosen, so the single-contributor passthrough below cannot copy an
    # exactness the folder's own instability has already forfeited.
    out = list(_demote_while_moving(tuple(out), inv_unstable))

    # Finding N-2, with its stated reason corrected by finding X2-2. The behaviour below is
    # right and is unchanged; the justification was not. An evidence-object hold is **not** a
    # fact about the inputs and not about the folder -- `supplied_objects` includes
    # `matched_objects`, which exists only because of the record join, which is precisely why
    # `_demote_while_moving` demotes it and why finding X2-1 makes it a floor on a truncated
    # or uncertified join. The real reason it can never become the record-unit headline is
    # the **unit**: `VERDICTS` is not in `CONTENT_UNITS`, so it is excluded from
    # `contributing` below; and a positive `VERDICTS` hold additionally forces the headline to
    # be **withheld** rather than merely skipped, since E would otherwise publish a count of
    # supplied verdicts as a number of this folder's records. The hold stays on its own line
    # in its own unit, so refusing the aggregate costs no evidence.
    evidence = [(label, fig) for label, fig in out
                if fig.unit == VERDICTS and fig.value is not None and fig.value > 0]
    contributing = [(label, fig) for label, fig in out
                    if fig.unit in CONTENT_UNITS and fig.value is not None and fig.value > 0]
    any_withheld = any(fig.value is None for _label, fig in out)

    if evidence:
        headline = withheld(
            "uncertain, but not countable: rejected verdict evidence counts supplied objects, not "
            "records in this folder, so it is reported on its own line and never becomes a record "
            "total",
            unit=RECORDS, axis=AXIS_UNCERTAIN, subject=subject,
            basis="A5 uncertainty observations", window=window,
            notes=tuple(label for label, _f in out)[:12])
    elif len(contributing) == 1 and not any_withheld:
        label, fig = contributing[0]
        headline = replace(fig, notes=fig.notes + (f"the only uncertainty observed: {label}",))
    elif not contributing and not any_withheld and not zero_doubts:
        headline = Figure(value=0, unit=RECORDS, exactness=EXACT, axis=AXIS_UNCERTAIN,
                          subject=subject,
                          population="every uncertainty question that was put returned zero",
                          basis="all A5 uncertainty observations", window=window)
    elif not contributing and not any_withheld:
        # Every question returned zero, but not over the whole folder, so each of those zeros
        # describes only the part this pass could put the question to. Publishing it as an
        # exact folder-wide zero would contradict this report's own caveats.
        #
        # Finding X2-1: a moving folder was not the only way to arrive here. A truncated
        # record *display* reaches this branch identically, because the refused record that
        # would have raised a hold can simply lie beyond the display window -- measured, one
        # folder and one set of supplied verdicts published `E uncertain: 1 verdicts` or
        # `E uncertain: 0 records (exact)` depending only on where the cap fell, the second
        # of them `publishable_as_total`. The zero is kept rather than withheld: it is a real
        # observation and withholding it would destroy evidence. It is demoted to a floor
        # that names every cause that applies and no cause that does not.
        headline = Figure(value=0, unit=RECORDS, exactness=LOWER_BOUND, axis=AXIS_UNCERTAIN,
                          subject=subject,
                          population="every uncertainty question that was put returned zero for "
                                     "the entries and records this pass examined",
                          basis="all A5 uncertainty observations", window=window,
                          limitations=zero_doubts)
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

    all_cats = tuple(c for c in obs.categories if c.usable)
    cheap_cats = tuple(c for c in all_cats if not c.expensive)
    ok, why = _disjoint(cheap_cats)
    cost_overlaps = _cost_overlaps(all_cats)
    cheap_overlap = next((reason for reason, involves_cheap in cost_overlaps if involves_cheap), "")
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
    elif cheap_overlap:
        # Finding F-6. `_disjoint` only ever sees the cheap group, so a cheap root that *contains*
        # the expensive root passes it -- while `cache_admin.measure` walks, so that cheap root's
        # byte count already includes the expensive bytes. A "cheaply regenerable, taken together"
        # figure would then be reporting the Stage-5 analysis cache as cheap to rebuild.
        cheap_subtotal = withheld("the supplied category roots are not separate storage, so a "
                                  "non-expensive subtotal would include bytes that belong to "
                                  "another category: " + cheap_overlap,
                                  unit=BYTES, axis=AXIS_COST, subject=subject,
                                  basis="sum of cache_admin category measurements")
    elif len(values) != len(cheap):
        cheap_subtotal = withheld("at least one category was not measured, so a subtotal would "
                                  "understate the group without saying so", unit=BYTES,
                                  axis=AXIS_COST, subject=subject,
                                  basis="sum of cache_admin category measurements")
    else:
        # Finding F-7: `storage_key` is provenance the CALLER supplied. A5 resolves nothing, so it
        # is in no position to call anything "proven", and a single-member group has not been
        # compared against anything at all.
        if len(values) == 1:
            provenance = ("a single non-expensive category, so no disjointness question arises "
                          "between members")
        else:
            provenance = (f"{len(values)} categories, each supplied with a distinct storage "
                          f"provenance by the caller; A5 resolves nothing and so verifies none of "
                          f"it independently")
        cheap_subtotal = Figure(
            value=int(sum(values)), unit=BYTES, exactness=LOWER_BOUND, axis=AXIS_COST,
            subject="the non-expensive categories, taken together",
            population=provenance,
            basis="sum of cache_admin category measurements",
            limitations=("every member is itself a floor, so the sum is a floor",))

    for reason, _involves_cheap in cost_overlaps:
        overlaps.append("supplied category roots are not separate storage: " + reason)

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
        # Its own path, not the report's primary subject (finding F-3): when this measurement
        # describes a different folder from A4's selection, the combined figures are withheld but
        # this one survives as a statement about the folder it actually measured.
        measurements.append(("analysis_cache.measure", _flat_figure(obs, _flat_subject(obs))))

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
    # Every one of these is an observation of the A3 inventory's folder, so they carry the
    # inventory's own key as their subject rather than the report's primary subject (finding F-3).
    record_subject = _inventory_subject(inv) if inv is not None else subject
    if inv is None:
        other_configuration = withheld("no record inventory was supplied", unit=RECORDS,
                                       axis=AXIS_CONTRACT, subject=subject)
        out_of_scope = withheld("no record inventory was supplied", unit=RECORDS, axis=AXIS_SCOPE,
                                subject=subject)
        confirmed_missing = withheld("no record inventory was supplied", unit=RECORDS,
                                     axis=AXIS_SOURCE, subject=subject)
        rejected = ()
        source_holds = ()
    else:
        other_configuration = _record_axis_figure(
            inv, counts_attr="contract_counts", assessed_attr="contract_assessed",
            wanted=CONTRACT_OTHER, axis=AXIS_CONTRACT,
            label="the cache-contract generation", subject=record_subject, window=window,
            extra_limits=("only the recorded cache-contract generation was compared: A3 reads no "
                          "analysis version, backend identity or Qwen configuration token, so "
                          "those remain unassessed",))
        roots = _verified_root_keys(inv)
        out_of_scope = _record_axis_figure(
            inv, counts_attr="scope_counts", assessed_attr="scope_assessed",
            wanted=SCOPE_OUT, axis=AXIS_SCOPE, label="source scope", subject=record_subject,
            window=window,
            extra_limits=(() if roots else
                          ("no verified inspection root anchored the comparison",)))
        if not roots and out_of_scope.value is not None:
            out_of_scope = withheld(
                "no verified inspection root was supplied, so nothing lies demonstrably outside "
                "the library", unit=RECORDS, axis=AXIS_SCOPE, subject=record_subject,
                basis=f"RecordInventory.scope_counts[{SCOPE_OUT}]", window=window)
        confirmed_missing, rejected, source_holds = _source_state_figure(
            obs, inv, record_subject, window)

    uncertain, uncertain_observations = _uncertainty(obs, inv, subject, window, rejected,
                                                     source_holds)

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
    "BYTES", "CONTENT_UNITS", "ENTRIES", "RECORDS", "UNITS", "VERDICTS",
    "CONCURRENCY_LIMITATION",
    "EMPTY_OR_UNREADABLE", "EXACT", "EXACTNESS", "LOWER_BOUND", "NOT_ASSESSED",
    "OBSERVED_PARTIAL", "PUBLISHABLE",
    "IDENTITY_LEXICAL", "IDENTITY_NONE", "IDENTITY_PROVENANCE",
    "SOURCE_PRESENT", "SOURCE_UNRESOLVED",
    "EXPENSIVE_SENTENCE", "MISSING_SOURCE_SENTENCE", "OUT_OF_SCOPE_SENTENCE",
    "SELECTION_SENTENCE", "SHARED_SENTENCE",
    "Buckets", "CategoryObservation", "Figure", "FolderIdentity", "Observation",
    "bind_observation", "estimate_reclaimable", "report_lines", "withheld",
]
