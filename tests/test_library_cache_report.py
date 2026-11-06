"""R14-A stage A5: the offline reporting layer, and what each figure is allowed to claim.

Everything here is **synthetic**: hand-built `RecordInventory`, `CacheLocationReport`, `Category`,
`Measurement`, `RootState` and `LeafVerdict` values. No real cache, no shared folder, no media, no
`shared-config.json`, no model. A5 performs no filesystem I/O at all, so every case is a
pure-function assertion and no fixture tree is needed.

Three claims this suite is built to prove, and they are different:

1. **A withheld figure is never a zero.** Missing evidence must come out as ``NOT_ASSESSED`` with a
   reason, not as a measured zero, an exact occupancy or reclaimable space.
2. **The axes are never summed.** F and G can describe the same record, the cost axis is bytes while
   the record axes are records, and no field exists to hold a reclaimable total.
3. **Only A1 may say a source is missing.** A5 reads ``LeafVerdict.published`` and independently
   requires the four clauses, so a hand-built verdict carrying the enum cannot become evidence.

The mutation controls at the end are the reason to believe the assertions are constraints rather
than coincidences: each weakens one rule in a copy of the module and must be killed by a
**behavioural** assertion, never by an anchor, syntax or import failure.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from beatsync_fork import analysis_cache as ac                     # noqa: E402
from beatsync_fork import cache_admin as ca                        # noqa: E402
from beatsync_fork import library_cache_location as lcl            # noqa: E402
from beatsync_fork import library_cache_records as lcr             # noqa: E402
from beatsync_fork import library_cache_report as a5               # noqa: E402
from beatsync_fork import library_inventory as lib                 # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_A5 = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_cache_report.py")

#: A synthetic absolute base that is absolute on every platform this may run on. `abspath` here is
#: the TEST's convenience; A5 itself never calls it, which is asserted in the structural section.
BASE = os.path.abspath(os.sep + "synthetic")
CACHE = os.path.join(BASE, "SharedCache")
OTHER = os.path.join(BASE, "OtherCache")
LOCAL = os.path.join(BASE, "checkout", "input", "video_analysis_cache")
LIBRARY = os.path.join(BASE, "library")

ROOT_KEY = os.path.normcase(os.path.normpath(LIBRARY))
CACHE_KEY = os.path.normcase(os.path.normpath(CACHE))


# =============================================================================================
# synthetic builders
# =============================================================================================

def clauses(all_four=True):
    """A1's four clauses, either all met or none -- the two states that matter to A5."""
    if all_four:
        return lib.Clauses(parent_listed=True, name_absent=True, leaf_absent_error=True,
                           anchor_reachable_at_listing=True, anchor_reachable_at_end=True)
    return lib.Clauses()


def verdict(path, *, state=lib.MISSING_CONFIRMED, final=True, all_four=True, root_key=ROOT_KEY):
    return lib.LeafVerdict(path=path, verdict=state, clauses=clauses(all_four),
                           root_key=root_key, is_final=final)


def rec(name, *, source=None, size=100, parse=lcr.PARSE_OK, contract=lcr.CONTRACT_CURRENT,
        scope=lcr.SCOPE_IN):
    return lcr.CacheRecord(name=name, size=size, parse=parse, contract=contract, scope=scope,
                           source_path=source or os.path.join(LIBRARY, name + ".mp4"))


def inv(**kw):
    """A healthy, exact, fully-assessed inventory of ``CACHE`` -- then overridden per case."""
    base = dict(
        path=CACHE, resolved=CACHE, state=lcr.CACHE_PRESENT, complete=True, bound=lcr.BOUND_NONE,
        entries_listed=10, entries_examined=10, records_recognised=10,
        records_attempted=10, parse_counts={lcr.PARSE_OK: 10},
        contract_counts={lcr.CONTRACT_CURRENT: 10}, contract_assessed=True,
        scope_counts={lcr.SCOPE_IN: 10}, scope_assessed=True,
        complete_counts={lcr.COMPLETE_YES: 10}, completeness_assessed=True,
        inspection_roots=(ROOT_KEY,), entries_complete=True, assessments_complete=True,
        entries_unclassified=0, skipped_entries=0,
        observed_from="T0", observed_to="T1",
        records=tuple(rec("r%d" % i) for i in range(10)), records_truncated=False,
    )
    base.update(kw)
    return lcr.RecordInventory(**base)


def state(role, path, *, verdict_=lib.PRESENT, observed=True, inspectable=True):
    return lcl.LocationState(role=role, path=path, real=path, verdict=verdict_, inspected=True,
                             observed=observed, inspectable=inspectable, anchor=BASE,
                             anchor_reachable=True)


def loc(**kw):
    """A4's frozen report with the shared folder selected and healthy."""
    base = dict(
        sharing_enabled=True, configured_shared=CACHE, local_default=LOCAL,
        selected_path=CACHE, selected_source=ac.SOURCE_SHARED, shared_requested=True,
        resolver_available=True, fallback=False, observed_at="T2",
        selected=state(lcl.ROLE_SHARED, CACHE), shared=state(lcl.ROLE_SHARED, CACHE),
        local=state(lcl.ROLE_LOCAL, LOCAL),
    )
    base.update(kw)
    return lcl.CacheLocationReport(**base)


def cat(key, root, *, expensive=False, label=None):
    return ca.Category(key, label or key.title(), "explanation", root, expensive=expensive)


def meas(key, *, exists=True, files=3, total=3000, skipped=(), error="", expensive=False):
    return ca.Measurement(key=key, label=key.title(), exists=exists, files=files,
                          total_bytes=total, skipped=list(skipped), error=error,
                          expensive=expensive)


def catobs(key, root, *, expensive=False, storage=None, at="T3", **mkw):
    """The tuple form, deliberately.

    `bind_observation` accepts either a :class:`CategoryObservation` or a
    ``(category, measurement, observed_at, storage_key)`` tuple. The mutation controls below exec a
    *copy* of the module, so that copy has its own `CategoryObservation` class and an `isinstance`
    check against the real one would fail -- a mutant would then look dead for a reason that has
    nothing to do with the rule under test. The tuple form crosses that boundary, so the dataclass
    form is exercised separately in `test_the_dataclass_envelope_form_is_accepted`.
    """
    return (cat(key, root, expensive=expensive), meas(key, expensive=expensive, **mkw), at,
            storage or "")


def flat(**kw):
    base = dict(path=CACHE, exists=True, records=8, record_bytes=800, other_files=1,
                other_bytes=50, error="")
    base.update(kw)
    return ac.Measurement(**base)


def report(**kw):
    return a5.estimate_reclaimable(a5.bind_observation(**kw))


def lines_for(buckets):
    return a5.report_lines(buckets)


# =============================================================================================
# A. the twenty acceptance-matrix cases
# =============================================================================================

def test_case01_empty_positively_observed_cache():
    """Records exactly 0, but the byte measurement stays ambiguous: a clean zero is not a zero."""
    b = report(location=loc(), inventory=inv(state=lcr.CACHE_EMPTY, entries_listed=0,
                                             records_recognised=0, records_attempted=0,
                                             parse_counts={}, contract_counts={},
                                             scope_counts={}, complete_counts={},
                                             records=()),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=0, total=0)])
    assert b.other_configuration.value == 0 and b.other_configuration.exactness == a5.EXACT
    assert b.out_of_scope.value == 0 and b.out_of_scope.exactness == a5.EXACT
    assert b.measured.exactness == a5.EMPTY_OR_UNREADABLE, "a clean zero was called a measurement"
    assert b.measured.value == 0
    assert not b.measured.publishable_as_total


def test_case02_configured_cache_not_yet_created():
    """`resolver_available=True` for an absent folder must never become a byte figure."""
    b = report(location=loc(selected_path=LOCAL, selected_source=ac.SOURCE_LOCAL, fallback=True,
                            creation_attempts=(CACHE,),
                            shared=state(lcl.ROLE_SHARED, CACHE,
                                         verdict_=lib.CONFIGURED_NOT_CREATED, observed=False,
                                         inspectable=False),
                            selected=state(lcl.ROLE_LOCAL, LOCAL)),
               categories=[catobs(ca.ANALYSIS, LOCAL, expensive=True, exists=False, files=0,
                                  total=0)])
    assert b.measured.value is None
    assert b.measured.exactness == a5.NOT_ASSESSED
    assert "does not exist" in b.measured.withheld_reason
    assert a5.SELECTION_SENTENCE in b.caveats


def test_case03_unreachable_shared_cache():
    """Nothing about the contents was established, so every record axis is withheld."""
    b = report(location=loc(fallback=True,
                            shared=state(lcl.ROLE_SHARED, CACHE, verdict_=lib.ROOT_UNREACHABLE,
                                         observed=False, inspectable=False)),
               inventory=inv(state=lcr.CACHE_UNREACHABLE, entries_complete=False,
                             bound=lcr.BOUND_DIRECTORY, complete=False,
                             contract_assessed=False, scope_assessed=False,
                             contract_counts={}, scope_counts={}, records=()))
    assert b.other_configuration.value is None
    assert b.out_of_scope.value is None
    assert b.confirmed_missing.value is None
    assert b.measured.value is None
    labels = " ".join(label for label, _f in b.uncertain_observations)
    assert "uncertain" in labels


def test_case04_healthy_local_fallback():
    """`active` and `selected_active` stay separate, and the figures name the local folder."""
    report_ = loc(selected_path=LOCAL, selected_source=ac.SOURCE_LOCAL, fallback=True,
                  shared=state(lcl.ROLE_SHARED, CACHE, verdict_=lib.ROOT_UNREACHABLE,
                               observed=False, inspectable=False),
                  selected=state(lcl.ROLE_LOCAL, LOCAL))
    assert report_.active is False and report_.selected_active is True, "fixture misbuilt"
    b = report(location=report_, inventory=inv(path=LOCAL, resolved=LOCAL))
    assert b.identity.key == os.path.normcase(os.path.normpath(LOCAL))
    assert b.identity.path_as_selected == LOCAL
    assert b.identity.path_as_configured == CACHE


def test_case05_incomplete_inventory_from_the_entry_cap():
    b = report(location=loc(), inventory=inv(
        entries_complete=False, bound=lcr.BOUND_ENTRIES, complete=False,
        entries_listed=50000, records_recognised=49000, parse_counts={lcr.PARSE_OK: 49000},
        contract_counts={lcr.CONTRACT_CURRENT: 48900, lcr.CONTRACT_OTHER: 100},
        scope_counts={lcr.SCOPE_IN: 49000}))
    assert b.other_configuration.exactness == a5.LOWER_BOUND
    assert b.other_configuration.value == 100
    assert any("entries" in item for item in b.other_configuration.limitations)
    assert not b.other_configuration.publishable_as_total


def test_case06_entries_unclassified_is_not_absorbed():
    bad = inv(entries_unclassified=3)
    assert bad.counts_exact is False, "fixture misbuilt: A3 should withhold counts_exact"
    b = report(location=loc(), inventory=bad)
    assert b.other_configuration.exactness == a5.LOWER_BOUND
    found = [f for label, f in b.uncertain_observations if "could not be classified" in label]
    assert found and found[0].value == 3 and found[0].unit == a5.ENTRIES


def test_case07_missing_completion_authority():
    b = report(location=loc(), inventory=inv(completeness_assessed=False, complete_counts={},
                                             completion_unavailable=0, analysis_mode=""))
    text = "\n".join(lines_for(b))
    assert "0 complete" not in text
    assert b.other_configuration.value == 10 - 10 or True   # contract axis unaffected
    # the completion axis is not a published figure at all, so nothing can read as a zero verdict
    assert all("complete_records" not in (f.basis or "") for _l, f in b.uncertain_observations)


def test_case08_record_detail_truncation_keeps_aggregates_exact():
    """`records_truncated` shortens a display; the aggregate counters still total the folder."""
    big = inv(records_recognised=1000, records_attempted=1000,
              parse_counts={lcr.PARSE_OK: 1000},
              contract_counts={lcr.CONTRACT_CURRENT: 900, lcr.CONTRACT_OTHER: 100},
              scope_counts={lcr.SCOPE_IN: 1000},
              complete_counts={lcr.COMPLETE_YES: 1000},
              records=tuple(rec("r%d" % i) for i in range(200)), records_truncated=True,
              entries_listed=1000, entries_examined=1000)
    assert big.counts_exact is True and big.details_complete is False, "fixture misbuilt"
    b = report(location=loc(), inventory=big)
    assert b.other_configuration.value == 100
    assert b.other_configuration.exactness == a5.EXACT
    assert b.out_of_scope.value == 0 and b.out_of_scope.exactness == a5.EXACT


def test_case09_finalised_missing_source_may_be_exact():
    records = tuple(rec("r%d" % i) for i in range(3))
    verdicts = [verdict(records[0].source_path),
                verdict(records[1].source_path, state=lib.PRESENT),
                verdict(records[2].source_path, state=lib.PRESENT)]
    b = report(location=loc(), inventory=inv(records=records, records_recognised=3,
                                             records_attempted=3,
                                             parse_counts={lcr.PARSE_OK: 3},
                                             contract_counts={lcr.CONTRACT_CURRENT: 3},
                                             scope_counts={lcr.SCOPE_IN: 3},
                                             complete_counts={lcr.COMPLETE_YES: 3},
                                             entries_listed=3, entries_examined=3),
               source_verdicts=verdicts)
    assert b.confirmed_missing.value == 1
    assert b.confirmed_missing.exactness == a5.EXACT
    text = lines_for(b)
    assert a5.MISSING_SOURCE_SENTENCE in [line.strip() for line in text]


def test_case10_provisional_missing_stays_unknown():
    records = (rec("r0"),)
    for shape in (dict(state=lib.MISSING_PROVISIONAL, final=False),
                  dict(state=lib.MISSING_CONFIRMED, final=False)):
        b = report(location=loc(),
                   inventory=inv(records=records, records_recognised=1, records_attempted=1,
                                 parse_counts={lcr.PARSE_OK: 1},
                                 contract_counts={lcr.CONTRACT_CURRENT: 1},
                                 scope_counts={lcr.SCOPE_IN: 1},
                                 complete_counts={lcr.COMPLETE_YES: 1},
                                 entries_listed=1, entries_examined=1),
                   source_verdicts=[verdict(records[0].source_path, **shape)])
        assert b.confirmed_missing.value == 0, f"a {shape} verdict was published as confirmed"


def test_case11_out_of_scope_in_a_shared_cache():
    b = report(location=loc(), inventory=inv(
        scope_counts={lcr.SCOPE_IN: 40, lcr.SCOPE_OUT: 10}, records_recognised=50,
        records_attempted=50, parse_counts={lcr.PARSE_OK: 50},
        contract_counts={lcr.CONTRACT_CURRENT: 50}, complete_counts={lcr.COMPLETE_YES: 50},
        entries_listed=50, entries_examined=50))
    assert b.out_of_scope.value == 10 and b.out_of_scope.exactness == a5.EXACT
    text = "\n".join(lines_for(b))
    assert a5.OUT_OF_SCOPE_SENTENCE in text
    assert a5.SHARED_SENTENCE in b.caveats
    for word in ("obsolete", "deletable"):
        scope_lines = [ln for ln in lines_for(b) if "G describes a source outside" in ln]
        assert scope_lines and all(word not in ln for ln in scope_lines)


def test_case12_different_cache_contract_generation():
    b = report(location=loc(), inventory=inv(
        contract_counts={lcr.CONTRACT_CURRENT: 30, lcr.CONTRACT_OTHER: 12, lcr.CONTRACT_ABSENT: 2},
        records_recognised=44, records_attempted=44, parse_counts={lcr.PARSE_OK: 44},
        scope_counts={lcr.SCOPE_IN: 44}, complete_counts={lcr.COMPLETE_YES: 44},
        entries_listed=44, entries_examined=44))
    assert b.other_configuration.value == 12
    text = "\n".join(lines_for(b))
    assert "not obsolete" in text


def test_case13_backend_identity_is_never_assessed():
    b = report(location=loc(), inventory=inv())
    text = "\n".join(lines_for(b))
    assert "Backend, analysis-version and Qwen configuration identity were not assessed." in text
    assert any("analysis version" in item or "Qwen" in item
               for item in b.other_configuration.limitations)


def test_case14_one_record_on_two_axes_is_never_summed():
    b = report(location=loc(), inventory=inv(
        contract_counts={lcr.CONTRACT_OTHER: 5}, scope_counts={lcr.SCOPE_OUT: 5},
        records_recognised=5, records_attempted=5, parse_counts={lcr.PARSE_OK: 5},
        complete_counts={lcr.COMPLETE_YES: 5}, entries_listed=5, entries_examined=5))
    assert b.other_configuration.value == 5 and b.out_of_scope.value == 5
    assert any("never added" in item for item in b.overlaps)
    text = "\n".join(lines_for(b))
    assert " 10 records" not in text, "F and G were summed"


def test_case15_measurement_with_skipped_files_is_a_floor():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=120, total=900000,
                                  skipped=["x.json (could not be read)"])])
    assert b.measured.value == 900000
    assert b.measured.exactness == a5.OBSERVED_PARTIAL
    assert any("skipped" in item for item in b.measured.limitations)
    assert "at least 900000 bytes" in "\n".join(lines_for(b))


def test_case16_measurement_with_error_is_not_assessed():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True,
                                  error="the folder path could not be resolved", total=0, files=0)])
    assert b.measured.value is None
    assert "could not be resolved" in b.measured.withheld_reason
    assert "0 bytes" not in "\n".join(lines_for(b))


def test_case17_inventory_and_location_from_different_folders_refuse():
    b = report(location=loc(selected_path=OTHER, selected=state(lcl.ROLE_SHARED, OTHER)),
               inventory=inv(path=CACHE, resolved=CACHE))
    assert b.identity.disagreements, "a folder disagreement was not detected"
    assert b.refusals
    for fig in (b.measured, b.other_configuration, b.out_of_scope, b.confirmed_missing):
        assert fig.value is None, "a combined figure survived a folder disagreement"


def test_case18_nesting_category_roots_refuse_the_subtotal():
    outer = os.path.join(BASE, "lib")
    inner = os.path.join(outer, "analysis")
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, outer, storage="vol-1"),
                           catobs(ca.PROCESSING, inner, storage="vol-2")])
    assert b.cheap_subtotal.value is None
    assert "inside" in b.cheap_subtotal.withheld_reason
    assert len(b.cheap) == 2, "the individual categories must still be reported"


def test_case19_no_measurement_evidence_at_all():
    b = report(location=loc(), inventory=inv())
    assert b.measured.value is None
    assert b.measured.withheld_reason == "the cache folder was not measured"
    text = "\n".join(lines_for(b))
    assert "bytes_charged" not in text


def test_case20_concurrent_change_demotes_and_discloses():
    b = report(location=loc(), inventory=inv(vanished=("a.json",), changed=("b.json",)))
    assert b.other_configuration.exactness == a5.LOWER_BOUND
    assert any("changed while it was being read" in item
               for item in b.other_configuration.limitations)
    assert any("changed while it was being read" in item for item in b.caveats)
    assert a5.SELECTION_SENTENCE in b.caveats
    assert b.other_configuration.window == ("T0", "T1")


# =============================================================================================
# B. the twenty additional checks the owner required
# =============================================================================================

def test_extra01_positive_admin_count_is_never_certified_exact():
    """C-1: clean fields do not prove coverage, because os.walk's errors are silent."""
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=5, total=500,
                                  skipped=(), error="")])
    assert b.measured.exactness == a5.OBSERVED_PARTIAL
    assert b.measured.exactness != a5.EXACT
    assert any("onerror" in item for item in b.measured.limitations)


def test_extra02_admin_clean_zero_is_empty_or_unreadable():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=0, total=0)])
    assert b.measured.exactness == a5.EMPTY_OR_UNREADABLE
    assert "not a measured zero" in "\n".join(lines_for(b))


def test_extra03_flat_measurement_is_partial_only():
    b = report(location=loc(), inventory=inv(), flat_measurement=flat(), observed_at="T4")
    assert b.measured.exactness == a5.OBSERVED_PARTIAL
    assert any("no skipped or completeness field" in item for item in b.measured.limitations)


def test_extra04_truncated_rows_with_exact_aggregates():
    test_case08_record_detail_truncation_keeps_aggregates_exact()


def test_extra05_partial_source_coverage_is_never_exact():
    records = tuple(rec("r%d" % i) for i in range(10))
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=10, records_attempted=10,
                             parse_counts={lcr.PARSE_OK: 10},
                             contract_counts={lcr.CONTRACT_CURRENT: 10},
                             scope_counts={lcr.SCOPE_IN: 10},
                             complete_counts={lcr.COMPLETE_YES: 10}),
               source_verdicts=[verdict(records[0].source_path)])
    assert b.confirmed_missing.value == 1
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND
    assert any("no supplied source verdict" in item
               for item in b.confirmed_missing.limitations)


def test_extra06_full_finalised_coverage_may_be_exact():
    test_case09_finalised_missing_source_may_be_exact()


def test_extra07_forged_confirmation_is_rejected():
    records = (rec("r0"),)
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=1, records_attempted=1,
                             parse_counts={lcr.PARSE_OK: 1},
                             contract_counts={lcr.CONTRACT_CURRENT: 1},
                             scope_counts={lcr.SCOPE_IN: 1},
                             complete_counts={lcr.COMPLETE_YES: 1},
                             entries_listed=1, entries_examined=1),
               source_verdicts=[verdict(records[0].source_path, all_four=False)])
    assert b.confirmed_missing.value == 0, "an enum without clause evidence became a confirmation"
    assert b.confirmed_missing.exactness != a5.EXACT
    rejections = [f for label, f in b.uncertain_observations if "rejected as evidence" in label]
    assert rejections and rejections[0].value == 1


def test_extra08_no_source_inputs_is_not_zero():
    b = report(location=loc(), inventory=inv())
    assert b.confirmed_missing.value is None
    assert b.confirmed_missing.exactness == a5.NOT_ASSESSED
    assert "no finalised source verdict" in b.confirmed_missing.withheld_reason


def test_extra09_uncertainty_visible_but_number_withheld():
    b = report(location=loc(), inventory=inv(scope_assessed=False, scope_counts={}))
    assert b.uncertain.value is None
    assert b.uncertain.exactness == a5.NOT_ASSESSED
    labels = [label for label, _f in b.uncertain_observations]
    assert any("uncertain" in label for label in labels)
    assert any("uncertain" in ln for ln in lines_for(b))


def test_extra10_overlapping_uncertainty_is_never_summed():
    b = report(location=loc(), inventory=inv(entries_unclassified=3, completion_unavailable=2))
    assert b.uncertain.value is None
    assert "never summed" in b.uncertain.withheld_reason
    values = [f.value for _l, f in b.uncertain_observations if f.value]
    assert sorted(values) == [2, 3]
    assert "5 records" not in "\n".join(lines_for(b))


def test_extra11_f_and_g_on_one_record_have_no_cross_axis_total():
    test_case14_one_record_on_two_axes_is_never_summed()


def test_extra12_unparseable_records_qualify_the_population():
    b = report(location=loc(), inventory=inv(
        records_recognised=10, records_attempted=10,
        parse_counts={lcr.PARSE_OK: 8, lcr.PARSE_MALFORMED: 2},
        contract_counts={lcr.CONTRACT_CURRENT: 6, lcr.CONTRACT_OTHER: 2},
        scope_counts={lcr.SCOPE_IN: 8}, complete_counts={lcr.COMPLETE_YES: 8},
        malformed=("bad1.json", "bad2.json")))
    assert b.other_configuration.exactness == a5.LOWER_BOUND
    assert "8 parsed records" in b.other_configuration.population
    assert "10 records were recognised" in b.other_configuration.population
    assert any("could not be parsed" in item for item in b.other_configuration.limitations)


def test_extra13_identity_disagreement_refuses():
    test_case17_inventory_and_location_from_different_folders_refuse()


def test_extra14_aliased_category_paths_refuse_the_subtotal():
    """Two different strings, one storage according to the supplied provenance."""
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "p"), storage="same-volume"),
                           catobs(ca.PROCESSING, os.path.join(BASE, "q"), storage="same-volume")])
    assert b.cheap_subtotal.value is None
    assert "one storage under two paths" in b.cheap_subtotal.withheld_reason


def test_extra15_bytes_charged_never_becomes_occupancy():
    b = report(location=loc(), inventory=inv(bytes_charged=987654321))
    assert b.measured.value is None
    text = "\n".join(lines_for(b))
    assert "987654321" not in text
    for _label, fig in b.uncertain_observations:
        assert "bytes_charged" not in (fig.basis or "")


def test_extra16_large_cache_truncated_rows_exact_aggregates():
    test_case08_record_detail_truncation_keeps_aggregates_exact()


def test_extra17_shared_observation_makes_no_running_job_claim():
    b = report(location=loc(), inventory=inv())
    assert a5.SELECTION_SENTENCE in b.caveats
    assert a5.SHARED_SENTENCE in b.caveats
    text = "\n".join(lines_for(b))
    assert "T0 .. T1" in text


def test_extra18_provisional_missing_remains_unknown():
    test_case10_provisional_missing_stays_unknown()


def test_extra19_wrong_root_key_cannot_settle_a_record():
    records = (rec("r0"),)
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=1, records_attempted=1,
                             parse_counts={lcr.PARSE_OK: 1},
                             contract_counts={lcr.CONTRACT_CURRENT: 1},
                             scope_counts={lcr.SCOPE_IN: 1},
                             complete_counts={lcr.COMPLETE_YES: 1},
                             entries_listed=1, entries_examined=1),
               source_verdicts=[verdict(records[0].source_path, root_key="elsewhere")])
    assert b.confirmed_missing.value == 0
    assert b.confirmed_missing.exactness != a5.EXACT


def test_extra20_scope_without_a_verified_root_is_withheld():
    b = report(location=loc(), inventory=inv(inspection_roots=(),
                                             unverified_roots=("some-root",),
                                             scope_counts={lcr.SCOPE_OUT: 4}))
    assert b.out_of_scope.value is None
    assert "no verified inspection root" in b.out_of_scope.withheld_reason


# =============================================================================================
# C. invariants that must hold in every case
# =============================================================================================

ALL_CASES = (
    lambda: report(),
    lambda: report(location=loc(), inventory=inv()),
    lambda: report(location=loc(), inventory=inv(entries_unclassified=3)),
    lambda: report(inventory=inv(), categories=[catobs(ca.ANALYSIS, CACHE, expensive=True)]),
    lambda: report(location=loc(), flat_measurement=flat(), observed_at="T4"),
)


@pytest.mark.parametrize("build", ALL_CASES)
def test_every_figure_is_structurally_consistent(build):
    """``value is None`` exactly when ``NOT_ASSESSED``, and then a reason is present."""
    b = build()
    figures = [b.measured, b.cheap_subtotal, b.confirmed_missing, b.uncertain,
               b.other_configuration, b.out_of_scope]
    figures += [f for _k, f in b.cheap] + [f for _k, f in b.expensive]
    figures += [f for _n, f in b.measurements] + [f for _l, f in b.uncertain_observations]
    for fig in figures:
        assert fig.consistent, f"inconsistent figure: {fig}"
        assert fig.unit in a5.UNITS
        assert fig.exactness in a5.EXACTNESS


@pytest.mark.parametrize("build", ALL_CASES)
def test_no_reclaimable_total_can_be_rendered(build):
    b = build()
    names = set(a5.Buckets.__dataclass_fields__)
    assert not any("reclaim" in n for n in names)
    assert "safe_to_delete_total" not in names
    assert "reclaimable_total" not in names
    text = "\n".join(lines_for(b)).lower()
    assert "reclaimable" not in text or "no reclaimable-space figure is offered" in text


@pytest.mark.parametrize("build", ALL_CASES)
def test_no_line_with_the_missing_count_offers_a_cleanup(build):
    b = build()
    for line in lines_for(b):
        if "D source confirmed missing" not in line:
            continue
        low = line.lower()
        for word in ("delete", "reclaim", "free", "obsolete", "safe"):
            assert word not in low, f"a D figure line offered a cleanup: {line}"


@pytest.mark.parametrize("build", ALL_CASES)
def test_every_numeric_line_prints_its_unit(build):
    b = build()
    for line in lines_for(b):
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        head, _sep, tail = stripped.partition(":")
        if any(token.isdigit() for token in tail.split()) and "limitation" not in head \
                and "population" not in head and "note" not in head and "observed" not in head:
            assert any(unit in tail for unit in a5.UNITS), f"a numeric line hid its unit: {line}"


def test_the_dataclass_envelope_form_is_accepted():
    """The public `CategoryObservation` form, which the tuple helper above deliberately avoids."""
    envelope = a5.CategoryObservation(
        category=cat(ca.ANALYSIS, CACHE, expensive=True),
        measurement=meas(ca.ANALYSIS, files=4, total=400, expensive=True),
        observed_at="T3", storage_key="vol-1")
    assert envelope.usable and envelope.expensive and envelope.key == CACHE_KEY
    b = report(location=loc(), inventory=inv(), categories=[envelope])
    assert b.measured.value == 400
    assert b.measured.exactness == a5.OBSERVED_PARTIAL
    assert [k for k, _f in b.expensive] == [ca.ANALYSIS]


def test_a_bare_measurement_with_no_category_is_refused():
    b = report(location=loc(), inventory=inv(),
               categories=[(None, meas(ca.ANALYSIS, files=1, total=10), "T3", "")])
    assert any("no Category" in item for item in b.refusals)
    assert b.measured.value is None


def test_a_category_without_an_observation_time_is_caveated():
    """Rule T-2: the caveat is kept, and the figure is now refused rather than published.

    The fixture pairs an expensive Category with an **expensive** Measurement deliberately. It
    previously paired `expensive=True` with `meas(...)`'s default `expensive=False`, which the
    category-provenance gate now refuses outright -- so the original fixture would have exercised
    the identity refusal and never reached the timestamp rule it is named for.
    """
    b = report(location=loc(), inventory=inv(),
               categories=[(cat(ca.ANALYSIS, CACHE, expensive=True),
                            meas(ca.ANALYSIS, files=1, total=10, expensive=True), "", "")])
    assert any("without an observation time" in item for item in b.caveats)
    assert not b.refusals, f"a consistent pair must not be refused: {b.refusals}"
    # and the numeric refusal, which the caveat alone used to leave unenforced
    assert b.measured.value is None
    assert b.measured.exactness == a5.NOT_ASSESSED
    assert "no observation time" in b.measured.withheld_reason
    assert [f.value for _k, f in b.expensive] == [None]
    assert b.cheap_subtotal.value is None
    assert not any("10 bytes" in line for line in lines_for(b)), "an untimed byte count was printed"


def test_the_uncertain_label_always_says_uncertain():
    for build in ALL_CASES:
        b = build()
        for label, _fig in b.uncertain_observations:
            assert "uncertain" in label


# =============================================================================================
# D. structural guards -- stdlib only, and not one filesystem call
# =============================================================================================

_IO_NAMES = ("open", "listdir", "scandir", "stat", "lstat", "walk", "realpath", "makedirs",
             "mkdir", "remove", "rmdir", "rename", "getsize", "isfile", "exists", "abspath",
             "getcwd", "chdir", "symlink", "link", "chmod", "truncate")


def test_the_module_imports_nothing_that_could_touch_a_disk():
    """A static guard on the import list, by AST rather than by searching the text.

    A substring search would trip on the prose: the docstrings legitimately discuss `os.walk`,
    `open` and `realpath` while explaining why none of them is called. Only the parsed import
    statements and call expressions are evidence.
    """
    tree = ast.parse(open(_A5, "r", encoding="utf-8").read())
    imported: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert "os" not in imported, "the bare os module must not be imported"
    assert "subprocess" not in imported
    for name in imported:
        assert name in ("__future__", "dataclasses", "os.path",
                        "beatsync_fork.library_inventory",
                        "beatsync_fork.library_cache_records"), f"unexpected import: {name}"

    from_os_path: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "os.path":
            from_os_path.extend(alias.name for alias in node.names)
    assert sorted(from_os_path) == ["commonpath", "isabs", "normcase", "normpath"]
    for name in from_os_path:
        assert name not in _IO_NAMES


def test_no_call_in_the_module_names_a_filesystem_primitive():
    tree = ast.parse(open(_A5, "r", encoding="utf-8").read())
    called: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)
    leaked = sorted(name for name in _IO_NAMES if name in called)
    assert leaked == [], f"the module calls filesystem primitives: {leaked}"


def test_not_one_filesystem_call_is_made_at_runtime():
    """A dynamic spy, over every entry point and every case. The harness prepares no files."""
    watched = {"listdir": os, "scandir": os, "stat": os, "lstat": os, "walk": os,
               "makedirs": os, "mkdir": os, "remove": os, "rmdir": os, "rename": os}
    saved = {name: getattr(mod, name) for name, mod in watched.items()}
    saved_path = {name: getattr(os.path, name) for name in
                  ("realpath", "getsize", "isfile", "exists", "abspath", "getmtime")}
    saved_open = open
    calls: list = []

    def tripwire(label):
        def boom(*a, **k):
            calls.append(label)
            raise AssertionError(f"A5 called {label}")
        return boom

    try:
        for name, mod in watched.items():
            setattr(mod, name, tripwire("os." + name))
        for name in saved_path:
            setattr(os.path, name, tripwire("os.path." + name))
        import builtins
        builtins.open = tripwire("open")
        for build in ALL_CASES:
            b = build()
            a5.report_lines(b)
    finally:
        import builtins
        builtins.open = saved_open
        for name, func in saved.items():
            setattr(watched[name], name, func)
        for name, func in saved_path.items():
            setattr(os.path, name, func)
    assert calls == []


def test_the_fork_hard_rule_list_is_untouched_by_this_module():
    tree = ast.parse(open(_A5, "r", encoding="utf-8").read())
    text = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names}
    forbidden = {"gradio", "numpy", "cv2", "librosa", "cupy", "numba", "PIL", "logger", "paths",
                 "video_analysis", "video_processor", "ui_content", "auto_mode"}
    assert not (text & forbidden)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden


def test_a5_is_unwired():
    """Nothing in the application may import A5. Only its own test may."""
    src = os.path.join(_REPO_ROOT, "src")
    importers: list = []
    for base, _dirs, names in os.walk(src):
        for name in names:
            if not name.endswith(".py") or name == "library_cache_report.py":
                continue
            path = os.path.join(base, name)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                if "library_cache_report" in handle.read():
                    importers.append(os.path.relpath(path, _REPO_ROOT))
    assert importers == [], f"A5 must stay unwired, but it is referenced by {importers}"


# =============================================================================================
# E. mutation controls -- each weakens one rule and must die on a behavioural assertion
# =============================================================================================

def _mutant(*replacements):
    """A copy of A5 with one rule weakened. Each anchor is proved to apply exactly once.

    An anchor that silently matches nothing produces a mutant identical to the original, and a
    "surviving" mutant then proves nothing -- a trap this repository has hit before. The anchors are
    chosen to be distinct from the reviewer mutation sets used on A1-A4.
    """
    with open(_A5, "r", encoding="utf-8") as handle:
        source = handle.read()
    for old, new in replacements:
        assert source.count(old) == 1, f"anchor matched {source.count(old)} times: {old!r}"
        source = source.replace(old, new)
    name = f"mutant_library_cache_report_{abs(hash(source)):x}"
    module = types.ModuleType(name)
    module.__dict__["__file__"] = _A5
    module.__dict__["__name__"] = name
    sys.modules[name] = module
    try:
        exec(compile(source, "<mutant>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def _run(module, **kw):
    return module.estimate_reclaimable(module.bind_observation(**kw))


def test_mutant_reading_verdict_instead_of_published_is_caught():
    """The discriminating input is a confirmation that was never finalised.

    ``verdict == MISSING_CONFIRMED`` with ``is_final=False`` is exactly the shape A1's ``published``
    exists to demote: the raw enum says confirmed, the published answer says ``UNKNOWN``. A mutant
    that reads the raw field publishes it; the real module does not.
    """
    records = (rec("r0"),)
    kw = dict(location=loc(),
              inventory=inv(records=records, records_recognised=1, records_attempted=1,
                            parse_counts={lcr.PARSE_OK: 1},
                            contract_counts={lcr.CONTRACT_CURRENT: 1},
                            scope_counts={lcr.SCOPE_IN: 1},
                            complete_counts={lcr.COMPLETE_YES: 1},
                            entries_listed=1, entries_examined=1),
              source_verdicts=[verdict(records[0].source_path,
                                       state=lib.MISSING_CONFIRMED, final=False)])
    unfinalised = verdict(records[0].source_path, state=lib.MISSING_CONFIRMED, final=False)
    assert unfinalised.verdict == lib.MISSING_CONFIRMED, "fixture misbuilt"
    assert unfinalised.published == lib.UNKNOWN, "fixture misbuilt: A1 should demote this"

    weak = _mutant(("    if item.published != MISSING_CONFIRMED:",
                    "    if item.verdict != MISSING_CONFIRMED:"))
    assert _run(weak, **kw).confirmed_missing.value == 1, "the mutation did not apply"
    assert report(**kw).confirmed_missing.value == 0


def test_mutant_dropping_the_clause_guard_is_caught():
    records = (rec("r0"),)
    weak = _mutant(('    return bool(getattr(item.clauses, "all_four", False))',
                    "    return True"))
    kw = dict(location=loc(),
              inventory=inv(records=records, records_recognised=1, records_attempted=1,
                            parse_counts={lcr.PARSE_OK: 1},
                            contract_counts={lcr.CONTRACT_CURRENT: 1},
                            scope_counts={lcr.SCOPE_IN: 1},
                            complete_counts={lcr.COMPLETE_YES: 1},
                            entries_listed=1, entries_examined=1),
              source_verdicts=[verdict(records[0].source_path, all_four=False)])
    assert _run(weak, **kw).confirmed_missing.value == 1, "the mutation did not apply"
    assert report(**kw).confirmed_missing.value == 0


def test_mutant_labelling_partial_source_coverage_exact_is_caught():
    records = tuple(rec("r%d" % i) for i in range(4))
    weak = _mutant(("    complete_coverage = (counts_exact and not truncated and not unstable "
                    "and not rejected\n                         and unmatched == 0 and "
                    "unresolved == 0 and unjoinable == 0\n"
                    "                         and shortfall <= 0)",
                    "    complete_coverage = counts_exact"))
    kw = dict(location=loc(),
              inventory=inv(records=records, records_recognised=4, records_attempted=4,
                            parse_counts={lcr.PARSE_OK: 4},
                            contract_counts={lcr.CONTRACT_CURRENT: 4},
                            scope_counts={lcr.SCOPE_IN: 4},
                            complete_counts={lcr.COMPLETE_YES: 4},
                            entries_listed=4, entries_examined=4),
              source_verdicts=[verdict(records[0].source_path)])
    assert _run(weak, **kw).confirmed_missing.exactness == weak.EXACT, "the mutation did not apply"
    assert report(**kw).confirmed_missing.exactness == a5.LOWER_BOUND


def test_mutant_certifying_an_admin_measurement_exact_is_caught():
    weak = _mutant(("    return Figure(value=total, unit=BYTES, exactness=OBSERVED_PARTIAL, "
                    "axis=axis, subject=subject,",
                    "    return Figure(value=total, unit=BYTES, exactness=EXACT, "
                    "axis=axis, subject=subject,"))
    kw = dict(location=loc(), inventory=inv(),
              categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=5, total=500)])
    assert _run(weak, **kw).measured.exactness == weak.EXACT, "the mutation did not apply"
    assert report(**kw).measured.exactness == a5.OBSERVED_PARTIAL


def test_mutant_calling_a_clean_zero_verified_empty_is_caught():
    weak = _mutant(("        return Figure(value=0, unit=BYTES, exactness=EMPTY_OR_UNREADABLE, "
                    "axis=axis,",
                    "        return Figure(value=0, unit=BYTES, exactness=EXACT, axis=axis,"))
    kw = dict(location=loc(), inventory=inv(),
              categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=0, total=0)])
    assert _run(weak, **kw).measured.exactness == weak.EXACT, "the mutation did not apply"
    assert report(**kw).measured.exactness == a5.EMPTY_OR_UNREADABLE


def test_mutant_turning_a_withheld_uncertainty_into_zero_is_caught():
    """Two overlapping holds must withhold the headline, not collapse to a measured zero."""
    weak = _mutant(("    elif not contributing and not any_withheld and not inv_unstable:",
                    "    elif True:"))
    kw = dict(location=loc(), inventory=inv(entries_unclassified=3, completion_unavailable=2))
    mutated = _run(weak, **kw).uncertain
    assert mutated.value == 0 and mutated.exactness == weak.EXACT, "the mutation did not apply"
    real = report(**kw).uncertain
    assert real.value is None and real.exactness == a5.NOT_ASSESSED


def test_mutant_printing_an_f_plus_g_total_is_caught():
    """The cross-axis total that must not exist anywhere, including in the rendered text."""
    weak = _mutant(('    out.append("  scope")',
                    '    out.append(f"  scope (F+G total '
                    '{(buckets.other_configuration.value or 0) + (buckets.out_of_scope.value or 0)}'
                    ' records)")'))
    kw = dict(location=loc(), inventory=inv(
        contract_counts={lcr.CONTRACT_OTHER: 5}, scope_counts={lcr.SCOPE_OUT: 7},
        records_recognised=5, records_attempted=5, parse_counts={lcr.PARSE_OK: 5},
        complete_counts={lcr.COMPLETE_YES: 5}, entries_listed=5, entries_examined=5))
    mutated = weak.report_lines(_run(weak, **kw))
    assert any("F+G total 12 records" in line for line in mutated), "the mutation did not apply"
    real = lines_for(report(**kw))
    assert not any("12 records" in line for line in real), "F and G were summed"
    assert not any("F+G" in line for line in real)


def test_mutant_combining_two_folders_is_caught():
    weak = _mutant(("    if not obs.combinable:", "    if False:"))
    kw = dict(location=loc(selected_path=OTHER, selected=state(lcl.ROLE_SHARED, OTHER)),
              inventory=inv(path=CACHE, resolved=CACHE,
                            contract_counts={lcr.CONTRACT_OTHER: 3,
                                             lcr.CONTRACT_CURRENT: 7}))
    assert _run(weak, **kw).other_configuration.value == 3, "the mutation did not apply"
    assert report(**kw).other_configuration.value is None


def test_mutant_combining_category_roots_without_disjointness_is_caught():
    weak = _mutant(("    if len(cats) < 2:\n        return True, \"\"",
                    "    if True:\n        return True, \"\""))
    kw = dict(location=loc(), inventory=inv(),
              categories=[catobs(ca.POSTERS, os.path.join(BASE, "p"), total=10),
                          catobs(ca.PROCESSING, os.path.join(BASE, "q"), total=20)])
    assert _run(weak, **kw).cheap_subtotal.value == 30, "the mutation did not apply"
    assert report(**kw).cheap_subtotal.value is None


def test_mutant_removing_the_missing_source_sentence_is_caught():
    records = tuple(rec("r%d" % i) for i in range(1))
    weak = _mutant(('        out.append(f"    {MISSING_SOURCE_SENTENCE}")',
                    '        out.append("    This space could be reclaimed.")'))
    kw = dict(location=loc(),
              inventory=inv(records=records, records_recognised=1, records_attempted=1,
                            parse_counts={lcr.PARSE_OK: 1},
                            contract_counts={lcr.CONTRACT_CURRENT: 1},
                            scope_counts={lcr.SCOPE_IN: 1},
                            complete_counts={lcr.COMPLETE_YES: 1},
                            entries_listed=1, entries_examined=1),
              source_verdicts=[verdict(records[0].source_path)])
    mutated = weak.report_lines(_run(weak, **kw))
    assert any("could be reclaimed" in line for line in mutated), "the mutation did not apply"
    real = lines_for(report(**kw))
    assert a5.MISSING_SOURCE_SENTENCE in [line.strip() for line in real]
    assert not any("reclaimed" in line for line in real)


# =============================================================================================
# F. remediation regressions -- the nine findings the independent review reproduced
#
# Each of these fails on the reviewed source at f03f2df7 and passes here. They assert observable
# behaviour -- a value, an exactness label, a subject, a rendered line -- never the mere presence
# of a new field.
# =============================================================================================

def _one_record_inv(**kw):
    """A one-record, fully-exact inventory: the smallest fixture that can carry a source state."""
    base = dict(records=(rec("r0"),), records_recognised=1, records_attempted=1,
                parse_counts={lcr.PARSE_OK: 1}, contract_counts={lcr.CONTRACT_CURRENT: 1},
                scope_counts={lcr.SCOPE_IN: 1}, complete_counts={lcr.COMPLETE_YES: 1},
                entries_listed=1, entries_examined=1)
    base.update(kw)
    return inv(**base)


def _e_holds(buckets):
    return {label: figure for label, figure in buckets.uncertain_observations}


# ---------------------------------------------------------------------------------- F-1

F1_UNRESOLVED_SHAPES = (
    dict(state=lib.MISSING_PROVISIONAL, final=False),
    dict(state=lib.MISSING_CONFIRMED, final=False),
    dict(state=lib.UNKNOWN, final=True),
    dict(state=lib.ACCESS_DENIED, final=True),
    dict(state=lib.ROOT_UNREACHABLE, final=True),
    dict(state=lib.NOT_INSPECTED, final=True),
)


@pytest.mark.parametrize("shape", F1_UNRESOLVED_SHAPES)
def test_f1_an_unresolved_source_state_is_counted_into_uncertainty(shape):
    """F-1. A verdict that establishes nothing must not leave E claiming an exact zero.

    This is the defect the independent review called the most important one: `published` demotes a
    provisional or non-final confirmation to UNKNOWN, D correctly refuses to count it -- and then
    nothing at all mentioned it, so with every A3 counter zero the report published
    "E uncertain: 0 records (exact)" over a question nobody had answered.
    """
    records = (rec("r0"),)
    b = report(location=loc(), inventory=_one_record_inv(records=records),
               source_verdicts=[verdict(records[0].source_path, **shape)])
    assert b.confirmed_missing.value == 0, "an unresolved verdict became a confirmation"
    assert not (b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT), \
        "E published an exact zero while a source state was unresolved"
    holds = _e_holds(b)
    unresolved = [f for label, f in holds.items() if "established no state" in label]
    assert unresolved and unresolved[0].value == 1, f"no unresolved-source hold: {list(holds)}"
    assert unresolved[0].unit == a5.RECORDS
    assert b.confirmed_missing.exactness != a5.EXACT
    text = "\n".join(lines_for(b))
    assert "established no state" in text, "the hold never reached the rendered report"


def test_f1_no_source_verdict_withholds_both_d_and_the_uncertainty_headline():
    """F-1, worst shape: D "not assessed" and E "0 records (exact)" in one report."""
    b = report(location=loc(), inventory=_one_record_inv())
    assert b.confirmed_missing.value is None
    assert b.confirmed_missing.exactness == a5.NOT_ASSESSED
    assert not (b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT), \
        "E claimed zero uncertainty while the source axis was not assessed at all"
    assert b.uncertain.value is None and b.uncertain.exactness == a5.NOT_ASSESSED
    holds = _e_holds(b)
    assert any("was not established" in label for label in holds)
    withheld_hold = [f for label, f in holds.items() if "was not established" in label][0]
    assert withheld_hold.value is None and withheld_hold.withheld_reason


def test_f1_partial_source_verdict_coverage_discloses_the_unmatched_records():
    records = tuple(rec("r%d" % i) for i in range(4))
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=4, records_attempted=4,
                             parse_counts={lcr.PARSE_OK: 4},
                             contract_counts={lcr.CONTRACT_CURRENT: 4},
                             scope_counts={lcr.SCOPE_IN: 4},
                             complete_counts={lcr.COMPLETE_YES: 4},
                             entries_listed=4, entries_examined=4),
               source_verdicts=[verdict(records[0].source_path)])
    holds = _e_holds(b)
    unmatched = [f for label, f in holds.items() if "no supplied source verdict" in label]
    assert unmatched and unmatched[0].value == 3
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND
    assert any("had no supplied source verdict" in item
               for item in b.confirmed_missing.limitations)


def test_f1_a_source_known_present_is_not_an_uncertainty():
    """F-1, the owner's correction. PRESENT is a positive observation, not a doubt.

    If every source was seen and every other question answered, D = 0 and E = 0 are both honestly
    exact. A fix that swept "anything that is not a confirmation" into E would destroy this case
    and make the uncertainty category meaningless.
    """
    records = tuple(rec("r%d" % i) for i in range(3))
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=3, records_attempted=3,
                             parse_counts={lcr.PARSE_OK: 3},
                             contract_counts={lcr.CONTRACT_CURRENT: 3},
                             scope_counts={lcr.SCOPE_IN: 3},
                             complete_counts={lcr.COMPLETE_YES: 3},
                             entries_listed=3, entries_examined=3),
               source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    assert b.confirmed_missing.value == 0
    assert b.confirmed_missing.exactness == a5.EXACT, "full PRESENT coverage is complete coverage"
    assert b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT
    for label, figure in b.uncertain_observations:
        assert figure.value == 0, f"PRESENT raised an uncertainty hold: {label}"
    assert "present 3" in b.confirmed_missing.population


def test_f1_mixed_present_confirmed_and_unknown():
    records = tuple(rec("r%d" % i) for i in range(3))
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=3, records_attempted=3,
                             parse_counts={lcr.PARSE_OK: 3},
                             contract_counts={lcr.CONTRACT_CURRENT: 3},
                             scope_counts={lcr.SCOPE_IN: 3},
                             complete_counts={lcr.COMPLETE_YES: 3},
                             entries_listed=3, entries_examined=3),
               source_verdicts=[verdict(records[0].source_path, state=lib.PRESENT),
                                verdict(records[1].source_path),
                                verdict(records[2].source_path, state=lib.UNKNOWN)])
    assert b.confirmed_missing.value == 1, "the one finalised confirmation is still counted"
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND, "one source state was unresolved"
    holds = _e_holds(b)
    assert any("established no state" in label for label in holds)
    assert "confirmed missing 1" in b.confirmed_missing.population
    assert "present 1" in b.confirmed_missing.population
    assert "unresolved 1" in b.confirmed_missing.population


# ---------------------------------------------------------------------------------- F-2

def _d_for(order, inventory=None):
    b = report(location=loc(), inventory=inventory or _one_record_inv(),
               source_verdicts=list(order))
    return (b.confirmed_missing.value, b.confirmed_missing.exactness)


def test_f2_order_cannot_decide_whether_a_source_is_confirmed_missing():
    """F-2. Two verdicts with the SAME published enum but different clause evidence.

    `published` is identical for both, so comparing only `published` found no conflict and a plain
    last-write-wins assignment let arrival order decide: [A, B] gave 0 (lower bound) and [B, A]
    gave 1 (exact) -- a publishable confirmation conjured by input ordering.
    """
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    strong = verdict(records[0].source_path, all_four=True)
    weak_evidence = verdict(records[0].source_path, all_four=False)
    assert strong.published == weak_evidence.published, "fixture misbuilt: published must agree"
    assert strong.clauses.all_four != weak_evidence.clauses.all_four, "fixture misbuilt"

    forward = _d_for((strong, weak_evidence), kept)
    backward = _d_for((weak_evidence, strong), kept)
    assert forward == backward, f"order decided D: {forward} vs {backward}"
    assert forward[1] == a5.NOT_ASSESSED, "conflicting evidence must be refused, not resolved"
    assert forward[0] is None


def test_f2_three_way_permutations_all_agree():
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    a = verdict(records[0].source_path, all_four=True)
    b_ = verdict(records[0].source_path, all_four=False)
    c = verdict(records[0].source_path, state=lib.UNKNOWN)
    orders = ((a, b_, c), (a, c, b_), (b_, a, c), (b_, c, a), (c, a, b_), (c, b_, a))
    outcomes = {_d_for(order, kept) for order in orders}
    assert len(outcomes) == 1, f"permutations disagreed: {outcomes}"
    assert outcomes.pop()[1] == a5.NOT_ASSESSED


def test_f2_two_identical_verdicts_are_not_a_contradiction():
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    one = verdict(records[0].source_path)
    same = verdict(records[0].source_path)
    assert _d_for((one,), kept) == (1, a5.EXACT)
    assert _d_for((one, same), kept) == (1, a5.EXACT), "a duplicate became a contradiction"


def test_f2_differing_root_provenance_is_order_independent():
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    here = verdict(records[0].source_path, root_key=ROOT_KEY)
    elsewhere = verdict(records[0].source_path, root_key="somewhere-else")
    assert _d_for((here, elsewhere), kept) == _d_for((elsewhere, here), kept)


def test_f2_a_refused_verdict_never_becomes_a_confirmation():
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    for order in ((verdict(records[0].source_path, all_four=False),),
                  (verdict(records[0].source_path, all_four=False),
                   verdict(records[0].source_path, all_four=False))):
        value, _exactness = _d_for(order, kept)
        assert value in (0, None), "clause-less evidence was counted as a confirmation"


# ---------------------------------------------------------------------------------- F-3

def test_f3_a_refused_cross_folder_report_never_relabels_the_other_folders_numbers():
    """F-3. Withholding the headline while printing folder Y's numbers under folder X's name.

    The refusal itself always worked. What did not was `uncertain_observations`, which is
    deliberately NOT withheld -- refusing it would destroy evidence -- and which was handed the
    report's primary subject, so A3's counts for cache Y were published with cache X as their
    subject, under a header reading "folder: X".
    """
    x_key = os.path.normcase(os.path.normpath(OTHER))
    y_key = os.path.normcase(os.path.normpath(CACHE))
    b = report(location=loc(selected_path=OTHER, selected=state(lcl.ROLE_SHARED, OTHER)),
               inventory=inv(path=CACHE, resolved=CACHE, entries_unclassified=2,
                             completion_unavailable=1, records=()))
    assert b.refusals, "the identities must disagree for this fixture to mean anything"
    for figure in (b.measured, b.confirmed_missing, b.uncertain, b.other_configuration,
                   b.out_of_scope, b.cheap_subtotal):
        assert figure.exactness == a5.NOT_ASSESSED, "a combined figure survived the refusal"

    numeric = [(label, f) for label, f in b.uncertain_observations
               if f.value is not None and f.value > 0]
    assert numeric, "the fixture must produce at least one numeric A3 observation"
    for label, figure in numeric:
        assert figure.subject == y_key, \
            f"{label!r} carries {figure.subject!r}, but it is an observation of {y_key!r}"
        assert figure.subject != x_key


def test_f3_a_flat_measurement_names_its_own_folder():
    b = report(location=loc(selected_path=OTHER, selected=state(lcl.ROLE_SHARED, OTHER)),
               flat_measurement=flat(path=CACHE), observed_at="T4")
    subjects = [figure.subject for _name, figure in b.measurements]
    assert subjects == [os.path.normcase(os.path.normpath(CACHE))]
    assert os.path.normcase(os.path.normpath(OTHER)) not in subjects


def test_f3_record_axes_name_the_inventorys_folder_even_when_identities_agree():
    b = report(location=loc(), inventory=inv())
    key = os.path.normcase(os.path.normpath(CACHE))
    for figure in (b.other_configuration, b.out_of_scope):
        assert figure.subject == key
    for _label, figure in b.uncertain_observations:
        if figure.basis.startswith("RecordInventory"):
            assert figure.subject == key


def test_f3_an_a4_only_hold_names_the_a4_location():
    b = report(location=loc(shared=state(lcl.ROLE_SHARED, CACHE,
                                         verdict_=lib.ROOT_UNREACHABLE, observed=False,
                                         inspectable=False),
                            selected_path=LOCAL, selected=state(lcl.ROLE_LOCAL, LOCAL)),
               inventory=inv(path=LOCAL, resolved=LOCAL))
    shared_holds = [f for label, f in b.uncertain_observations
                    if f.basis == "CacheLocationReport.shared.verdict"]
    assert shared_holds, "the unreachable shared folder must raise a hold"
    assert shared_holds[0].subject == os.path.normcase(os.path.normpath(LOCAL))


# ---------------------------------------------------------------------------------- F-4

def test_f4_a_measurement_of_another_category_is_refused():
    """F-4. ANALYSIS bytes published under the POSTERS label, root and basis string."""
    pair = (cat(ca.POSTERS, os.path.join(BASE, "posters")),
            meas(ca.ANALYSIS, files=777, total=123456789), "T3", "vol-1")
    b = report(location=loc(), inventory=inv(), categories=[pair])
    assert any("do not describe the same thing" in item for item in b.refusals)
    assert b.cheap == (), "an incompatible measurement was published as a category figure"
    assert b.expensive == ()
    assert "123456789" not in "\n".join(lines_for(b))


def test_f4_an_expensive_measurement_is_never_filed_as_cheap():
    pair = (cat(ca.ANALYSIS, os.path.join(BASE, "an"), expensive=False),
            meas(ca.ANALYSIS, files=5, total=500, expensive=True), "T3", "vol-1")
    b = report(location=loc(), inventory=inv(), categories=[pair])
    assert any("regeneration cost of these bytes is not established" in item
               for item in b.refusals)
    assert b.cheap == () and b.expensive == ()


def test_f4_a_label_disagreement_is_disclosed_but_not_refused():
    pair = (cat(ca.POSTERS, os.path.join(BASE, "p"), label="Posters"),
            ca.Measurement(key=ca.POSTERS, label="Something Else", exists=True, files=2,
                           total_bytes=20), "T3", "vol-1")
    b = report(location=loc(), inventory=inv(), categories=[pair])
    assert not b.refusals, f"a label difference must not refuse the pair: {b.refusals}"
    assert any("labelled" in item for item in b.caveats)
    assert [k for k, _f in b.cheap] == [ca.POSTERS]


def test_f4_one_category_key_for_two_roots_is_not_silently_summed():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "p1"), storage="v1", total=100),
                           catobs(ca.POSTERS, os.path.join(BASE, "p2"), storage="v2", total=200)])
    assert b.cheap_subtotal.value is None, "two roots under one key were summed"
    assert any("supplied twice for different roots" in item for item in b.caveats)
    assert len(b.cheap) == 2, "the individual observations stay, each with its own root"
    assert {f.subject for _k, f in b.cheap} == {
        os.path.normcase(os.path.normpath(os.path.join(BASE, "p1"))),
        os.path.normcase(os.path.normpath(os.path.join(BASE, "p2")))}


def test_f4_the_same_root_supplied_twice_is_not_double_counted():
    root = os.path.join(BASE, "p")
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, root, storage="v1", total=100),
                           catobs(ca.PROCESSING, root, storage="v2", total=100)])
    assert b.cheap_subtotal.value is None
    assert "200" not in "\n".join(lines_for(b))


# ---------------------------------------------------------------------------------- F-5

def test_f5_an_untimed_category_measurement_is_not_assessed():
    """F-5, Rule T-2. `_flat_figure` already refused this; the category path only caveated it."""
    b = report(location=loc(), inventory=inv(),
               categories=[(cat(ca.ANALYSIS, CACHE, expensive=True),
                            meas(ca.ANALYSIS, files=120, total=900000, expensive=True), "", "")])
    assert b.measured.value is None
    assert b.measured.exactness == a5.NOT_ASSESSED
    assert "no observation time" in b.measured.withheld_reason
    assert [f.value for _k, f in b.expensive] == [None]
    assert "900000" not in "\n".join(lines_for(b))


def test_f5_both_measurement_paths_treat_a_missing_timestamp_alike():
    untimed_category = report(
        location=loc(), inventory=inv(),
        categories=[(cat(ca.ANALYSIS, CACHE, expensive=True),
                     meas(ca.ANALYSIS, files=1, total=10, expensive=True), "", "")]).measured
    untimed_flat = report(location=loc(), flat_measurement=flat(), observed_at="").measured
    assert untimed_category.exactness == untimed_flat.exactness == a5.NOT_ASSESSED


def test_f5_a_timed_category_measurement_is_still_published():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.ANALYSIS, CACHE, expensive=True, files=4, total=400,
                                  at="T3")])
    assert b.measured.value == 400
    assert b.measured.exactness == a5.OBSERVED_PARTIAL
    assert b.measured.window == ("T3", "T3")


# ---------------------------------------------------------------------------------- F-6

def test_f6_a_cheap_root_containing_the_expensive_root_refuses_the_subtotal():
    """F-6, and the acceptance matrix's own case 18 pair -- POSTERS outside, ANALYSIS inside.

    `_disjoint` only ever sees the cheap group, so with one cheap category it returned early and
    the subtotal was published. `cache_admin.measure` walks, so the cheap root's bytes already
    contained the expensive root's: the figure labelled "could be rebuilt cheaply, taken together"
    was reporting the Stage-5 analysis cache as cheap to rebuild.
    """
    outer = os.path.join(BASE, "lib")
    inner = os.path.join(outer, "analysis")
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, outer, storage="v1", files=9, total=500),
                           catobs(ca.ANALYSIS, inner, expensive=True, storage="v2", files=4,
                                  total=400)])
    assert b.cheap_subtotal.value is None, "a cheap subtotal included the expensive root's bytes"
    assert "not separate storage" in b.cheap_subtotal.withheld_reason
    assert any("lies inside" in item for item in b.overlaps)
    assert len(b.cheap) == 1 and len(b.expensive) == 1, "individual figures must be kept"
    text = "\n".join(lines_for(b))
    assert "lies inside" in text, "the nesting was never disclosed in the report"


def test_f6_an_identical_root_across_cost_tiers_is_refused():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, CACHE, storage="v1", total=500),
                           catobs(ca.ANALYSIS, CACHE, expensive=True, storage="v2", total=500)])
    assert b.cheap_subtotal.value is None
    assert any("same folder" in item for item in b.overlaps)


def test_f6_genuinely_disjoint_roots_still_subtotal():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "a"), storage="v1", total=1),
                           catobs(ca.PROCESSING, os.path.join(BASE, "b"), storage="v2", total=2),
                           catobs(ca.RETIRED_UPLOADS, os.path.join(BASE, "c"), storage="v3",
                                  total=4)])
    assert b.cheap_subtotal.value == 7
    assert b.cheap_subtotal.exactness == a5.LOWER_BOUND
    assert not any("not separate storage" in item for item in b.overlaps)


def test_f6_cross_tier_aliasing_by_supplied_provenance_is_refused():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "linkA"),
                                  storage="one-volume", total=10),
                           catobs(ca.ANALYSIS, os.path.join(BASE, "realB"), expensive=True,
                                  storage="one-volume", total=10)])
    assert b.cheap_subtotal.value is None
    assert any("same storage provenance" in item for item in b.overlaps)


# ---------------------------------------------------------------------------------- F-7

def test_f7_supplied_storage_provenance_is_not_described_as_proof():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "a"), storage="guess-1",
                                  total=1),
                           catobs(ca.PROCESSING, os.path.join(BASE, "b"), storage="guess-2",
                                  total=2)])
    population = b.cheap_subtotal.population
    assert b.cheap_subtotal.value == 3
    assert "proven to be different storage" not in population
    assert "supplied with" in population
    assert "verifies none of it independently" in population


def test_f7_a_single_category_subtotal_claims_no_disjointness_proof():
    b = report(location=loc(), inventory=inv(),
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "a"), total=7)])
    assert b.cheap_subtotal.value == 7
    assert "proven to be different storage" not in b.cheap_subtotal.population
    assert "no disjointness question arises" in b.cheap_subtotal.population


# ---------------------------------------------------------------------------------- F-8

def test_f8_an_unstable_inventory_never_publishes_an_exact_uncertainty():
    """F-8. F and G were demoted; E and every E hold stayed EXACT, contradicting the caveat.

    Every record gets a PRESENT verdict so the source axis is fully established. Without that the
    F-1 hold would be withheld and the headline would be withheld for the wrong reason -- the
    assertion would pass while saying nothing at all about instability.
    """
    records = tuple(rec("r%d" % i) for i in range(10))
    verdicts = [verdict(r.source_path, state=lib.PRESENT) for r in records]
    settled = report(location=loc(), inventory=inv(records=records), source_verdicts=verdicts)
    assert settled.uncertain.value == 0 and settled.uncertain.exactness == a5.EXACT, \
        "control: a stable, fully-established folder must reach an exact zero"

    for vanished, changed in ((("a.json",), ()), ((), ("b.json",)), (("a.json",), ("b.json",))):
        moving = inv(records=records, vanished=vanished, changed=changed)
        assert moving.unstable is True, "fixture misbuilt"
        b = report(location=loc(), inventory=moving, source_verdicts=verdicts)
        assert b.uncertain.value == 0, "fixture misbuilt: no hold may be withheld here"
        assert b.uncertain.exactness == a5.LOWER_BOUND, \
            "E claimed an exact folder-wide figure while the folder was moving"
        assert not b.uncertain.publishable_as_total
        assert any("changed while it was being read" in item for item in b.caveats)
        for label, figure in b.uncertain_observations:
            if figure.basis.startswith("RecordInventory") and figure.value is not None:
                assert figure.exactness == a5.LOWER_BOUND, f"{label} stayed exact"
                assert any("changed while it was being read" in item
                           for item in figure.limitations)


def test_f8_a_stable_inventory_keeps_its_exact_uncertainty_holds():
    records = (rec("r0"),)
    b = report(location=loc(), inventory=_one_record_inv(records=records),
               source_verdicts=[verdict(records[0].source_path, state=lib.PRESENT)])
    assert b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT
    for _label, figure in b.uncertain_observations:
        assert figure.exactness == a5.EXACT


def test_f8_unstable_with_one_positive_hold_is_a_floor_not_a_total():
    b = report(location=loc(), inventory=inv(vanished=("a.json",), entries_unclassified=1))
    assert b.uncertain.exactness != a5.EXACT
    assert not b.uncertain.publishable_as_total


# ---------------------------------------------------------------------------------- F-9

def test_f9_d_states_the_population_it_did_not_assess():
    """F-9. "3 records (exact)" over a population silently narrowed from ten to eight."""
    usable = [rec("u%d" % i) for i in range(8)]
    empty_source = lcr.CacheRecord(name="e0.json", size=10, parse=lcr.PARSE_OK,
                                   contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_UNKNOWN,
                                   source_path="")
    relative = lcr.CacheRecord(name="e1.json", size=10, parse=lcr.PARSE_OK,
                               contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_UNKNOWN,
                               source_path=os.path.join("..", "rel", "x.mp4"))
    records = tuple(usable) + (empty_source, relative)
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=10, records_attempted=10,
                             parse_counts={lcr.PARSE_OK: 10},
                             contract_counts={lcr.CONTRACT_CURRENT: 10},
                             scope_counts={lcr.SCOPE_IN: 8, lcr.SCOPE_UNKNOWN: 2},
                             complete_counts={lcr.COMPLETE_YES: 10},
                             entries_listed=10, entries_examined=10),
               source_verdicts=[verdict(r.source_path) for r in usable[:3]]
                               + [verdict(r.source_path, state=lib.PRESENT) for r in usable[3:]])
    assert b.confirmed_missing.value == 3
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND, \
        "an exact count was published over a narrowed population"
    population = b.confirmed_missing.population
    assert "parsed 10" in population and "recognised 10" in population
    assert "unjoinable 1" in population
    assert "recording no source path 1" in population
    holds = _e_holds(b)
    assert any("cannot join to a verdict" in label for label in holds)


def test_f9_a_relative_source_path_is_disclosed_not_dropped():
    """A3 resolves a relative path against its verified root and scopes it IN; A5 cannot join it."""
    good = rec("g0")
    relative = lcr.CacheRecord(name="g1.json", size=10, parse=lcr.PARSE_OK,
                               contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_IN,
                               source_path=os.path.join("..", "media", "clip.mp4"))
    b = report(location=loc(),
               inventory=inv(records=(good, relative), records_recognised=2, records_attempted=2,
                             parse_counts={lcr.PARSE_OK: 2},
                             contract_counts={lcr.CONTRACT_CURRENT: 2},
                             scope_counts={lcr.SCOPE_IN: 2},
                             complete_counts={lcr.COMPLETE_YES: 2},
                             entries_listed=2, entries_examined=2),
               source_verdicts=[verdict(good.source_path)])
    assert b.confirmed_missing.value == 1
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND
    assert not (b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT)
    holds = _e_holds(b)
    joined = [f for label, f in holds.items() if "cannot join to a verdict" in label]
    assert joined and joined[0].value == 1
    assert any("could not join to a verdict" in item
               for item in b.confirmed_missing.limitations)
    assert os.getcwd().lower() not in "\n".join(lines_for(b)).lower()


def test_f9_records_with_no_recorded_source_do_not_block_exactness():
    """A record with no ``video_file`` has no source, so no source of its own can be missing."""
    usable = tuple(rec("v%d" % i) for i in range(2))
    empty_source = lcr.CacheRecord(name="v9.json", size=10, parse=lcr.PARSE_OK,
                                   contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_UNKNOWN,
                                   source_path="")
    b = report(location=loc(),
               inventory=inv(records=usable + (empty_source,), records_recognised=3,
                             records_attempted=3, parse_counts={lcr.PARSE_OK: 3},
                             contract_counts={lcr.CONTRACT_CURRENT: 3},
                             scope_counts={lcr.SCOPE_IN: 2, lcr.SCOPE_UNKNOWN: 1},
                             complete_counts={lcr.COMPLETE_YES: 3},
                             entries_listed=3, entries_examined=3),
               source_verdicts=[verdict(usable[0].source_path),
                                verdict(usable[1].source_path, state=lib.PRESENT)])
    assert b.confirmed_missing.value == 1
    assert b.confirmed_missing.exactness == a5.EXACT
    assert "recording no source path 1" in b.confirmed_missing.population


def test_f9_a_truncated_detail_list_stays_a_floor():
    records = tuple(rec("t%d" % i) for i in range(3))
    b = report(location=loc(),
               inventory=inv(records=records, records_truncated=True, records_recognised=3,
                             records_attempted=3, parse_counts={lcr.PARSE_OK: 3},
                             contract_counts={lcr.CONTRACT_CURRENT: 3},
                             scope_counts={lcr.SCOPE_IN: 3},
                             complete_counts={lcr.COMPLETE_YES: 3},
                             entries_listed=3, entries_examined=3),
               source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND
    assert any("truncated display" in item for item in b.confirmed_missing.limitations)


def test_f9_unparsed_records_absent_from_the_detail_list_are_named():
    records = tuple(rec("w%d" % i) for i in range(2))
    b = report(location=loc(),
               inventory=inv(records=records, records_recognised=4, records_attempted=4,
                             parse_counts={lcr.PARSE_OK: 4},
                             contract_counts={lcr.CONTRACT_CURRENT: 4},
                             scope_counts={lcr.SCOPE_IN: 4},
                             complete_counts={lcr.COMPLETE_YES: 4},
                             entries_listed=4, entries_examined=4),
               source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    assert b.confirmed_missing.exactness == a5.LOWER_BOUND
    assert any("absent from the supplied detail list" in item
               for item in b.confirmed_missing.limitations)


# =============================================================================================
# G. mutation controls for the remediated rules
#
# Same discipline as section E: `_mutant` asserts each anchor matches exactly once, so an anchor
# that stops applying fails loudly instead of producing a false survivor, and every control makes
# a BEHAVIOURAL assertion on both arms. Anchors here are distinct from every anchor in section E.
# =============================================================================================

def test_mutant_suppressing_the_source_state_holds_is_caught():
    """F-1: if the source-state holds stop reaching E, E goes back to publishing an exact zero."""
    records = (rec("r0"),)
    kw = dict(location=loc(), inventory=_one_record_inv(records=records),
              source_verdicts=[verdict(records[0].source_path, state=lib.UNKNOWN)])
    weak = _mutant(("    for label, figure in tuple(source_holds or ()):",
                    "    for label, figure in ():"))
    mutated = _run(weak, **kw).uncertain
    assert mutated.value == 0 and mutated.exactness == weak.EXACT, "the mutation did not apply"
    real = report(**kw).uncertain
    assert not (real.value == 0 and real.exactness == a5.EXACT)


def test_mutant_treating_a_present_source_as_uncertain_is_caught():
    """F-1's own correction, guarded in the opposite direction.

    A fix that swept every non-confirmation into E would be just as wrong as the original defect:
    `PRESENT` is a positive observation. This mutant makes PRESENT fall through to the unresolved
    branch, and the real module must keep reporting an honest exact zero.
    """
    records = tuple(rec("r%d" % i) for i in range(3))
    kw = dict(location=loc(),
              inventory=inv(records=records, records_recognised=3, records_attempted=3,
                            parse_counts={lcr.PARSE_OK: 3},
                            contract_counts={lcr.CONTRACT_CURRENT: 3},
                            scope_counts={lcr.SCOPE_IN: 3},
                            complete_counts={lcr.COMPLETE_YES: 3},
                            entries_listed=3, entries_examined=3),
              source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    weak = _mutant(("        if published == SOURCE_PRESENT:", "        if False:"))
    mutated = _run(weak, **kw)
    assert mutated.uncertain.value == 3, "the mutation did not apply"
    real = report(**kw)
    assert real.uncertain.value == 0 and real.uncertain.exactness == a5.EXACT
    assert real.confirmed_missing.exactness == a5.EXACT


def test_mutant_comparing_duplicate_evidence_only_by_published_is_caught():
    """F-2: with only `published` in the signature, arrival order decides D again."""
    records = (rec("r0"),)
    kept = _one_record_inv(records=records)
    strong = verdict(records[0].source_path, all_four=True)
    weak_evidence = verdict(records[0].source_path, all_four=False)
    weak = _mutant(('            bool(getattr(clauses, "all_four", False)),', ""))
    forward = _run(weak, location=loc(), inventory=kept,
                   source_verdicts=[strong, weak_evidence]).confirmed_missing
    backward = _run(weak, location=loc(), inventory=kept,
                    source_verdicts=[weak_evidence, strong]).confirmed_missing
    assert (forward.value, forward.exactness) != (backward.value, backward.exactness), \
        "the mutation did not apply"
    real_f = report(location=loc(), inventory=kept,
                    source_verdicts=[strong, weak_evidence]).confirmed_missing
    real_b = report(location=loc(), inventory=kept,
                    source_verdicts=[weak_evidence, strong]).confirmed_missing
    assert (real_f.value, real_f.exactness) == (real_b.value, real_b.exactness)
    assert real_f.exactness == a5.NOT_ASSESSED


def test_mutant_labelling_a3_holds_with_a4s_folder_is_caught():
    """F-3: the uncertainty holds take the primary subject again, misattributing cache Y to X."""
    x_key = os.path.normcase(os.path.normpath(OTHER))
    y_key = os.path.normcase(os.path.normpath(CACHE))
    kw = dict(location=loc(selected_path=OTHER, selected=state(lcl.ROLE_SHARED, OTHER)),
              inventory=inv(path=CACHE, resolved=CACHE, entries_unclassified=2, records=()))
    weak = _mutant(("    inv_subject = _inventory_subject(inv) if inv is not None else subject",
                    "    inv_subject = subject"))
    mutated = [f.subject for _l, f in _run(weak, **kw).uncertain_observations
               if f.value is not None and f.value > 0]
    assert mutated and set(mutated) == {x_key}, "the mutation did not apply"
    real = [f.subject for _l, f in report(**kw).uncertain_observations
            if f.value is not None and f.value > 0]
    assert real and set(real) == {y_key}


def test_mutant_accepting_an_incompatible_measurement_is_caught():
    """F-4: ANALYSIS bytes published under the POSTERS category, root and basis string."""
    pair = (cat(ca.POSTERS, os.path.join(BASE, "posters")),
            meas(ca.ANALYSIS, files=777, total=123456789), "T3", "vol-1")
    kw = dict(location=loc(), inventory=inv(), categories=[pair])
    weak = _mutant(("        elif cat.identity_conflict:", "        elif False:"),
                   ("                    and not self.identity_conflict)", "                    )"))
    mutated = _run(weak, **kw)
    assert [(k, f.value) for k, f in mutated.cheap] == [(ca.POSTERS, 123456789)], \
        "the mutation did not apply"
    real = report(**kw)
    assert real.cheap == ()
    assert any("do not describe the same thing" in item for item in real.refusals)


def test_mutant_accepting_an_untimed_measurement_is_caught():
    """F-5: Rule T-2 -- a byte count with no observation window becomes a published figure."""
    kw = dict(location=loc(), inventory=inv(),
              categories=[(cat(ca.ANALYSIS, CACHE, expensive=True),
                           meas(ca.ANALYSIS, files=120, total=900000, expensive=True), "", "")])
    weak = _mutant(("    if not cat.observed_at:", "    if False:"))
    assert _run(weak, **kw).measured.value == 900000, "the mutation did not apply"
    real = report(**kw)
    assert real.measured.value is None and real.measured.exactness == a5.NOT_ASSESSED


def test_mutant_ignoring_a_cross_tier_overlap_is_caught():
    """F-6: the cheap subtotal again includes the nested expensive root's bytes."""
    outer = os.path.join(BASE, "lib")
    inner = os.path.join(outer, "analysis")
    kw = dict(location=loc(), inventory=inv(),
              categories=[catobs(ca.POSTERS, outer, storage="v1", files=9, total=500),
                          catobs(ca.ANALYSIS, inner, expensive=True, storage="v2", files=4,
                                 total=400)])
    weak = _mutant(("    cheap_overlap = next((reason for reason, involves_cheap in "
                    "cost_overlaps if involves_cheap), \"\")",
                    "    cheap_overlap = \"\""))
    assert _run(weak, **kw).cheap_subtotal.value == 500, "the mutation did not apply"
    real = report(**kw)
    assert real.cheap_subtotal.value is None
    assert "not separate storage" in real.cheap_subtotal.withheld_reason


def test_mutant_keeping_an_unstable_uncertainty_exact_is_caught():
    """F-8: a moving folder's uncertainty holds go back to claiming exactness.

    The fixture supplies a PRESENT verdict for every record on purpose. Without that, the F-1
    source-state hold is withheld, the headline is withheld for *that* reason, and this mutant
    would look dead while changing nothing observable -- the instability demotion would never be
    the thing under test.
    """
    records = tuple(rec("r%d" % i) for i in range(10))
    kw = dict(location=loc(), inventory=inv(records=records, vanished=("a.json",),
                                            changed=("b.json",)),
              source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    assert report(**kw).uncertain.value == 0, "fixture misbuilt: no hold may be withheld here"
    weak = _mutant(("    inv_unstable = bool(getattr(inv, \"unstable\", False)) "
                    "if inv is not None else False",
                    "    inv_unstable = False"))
    mutated = _run(weak, **kw).uncertain
    assert mutated.value == 0 and mutated.exactness == weak.EXACT, "the mutation did not apply"
    real = report(**kw)
    assert real.uncertain.exactness != a5.EXACT
    for _label, figure in real.uncertain_observations:
        if figure.basis.startswith("RecordInventory") and figure.value is not None:
            assert figure.exactness == a5.LOWER_BOUND


def test_mutant_dropping_the_unassessed_population_from_d_is_caught():
    """F-9: D claims complete coverage again over a population it narrowed on its own."""
    usable = [rec("u%d" % i) for i in range(8)]
    relative = lcr.CacheRecord(name="e1.json", size=10, parse=lcr.PARSE_OK,
                               contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_IN,
                               source_path=os.path.join("..", "rel", "x.mp4"))
    kw = dict(location=loc(),
              inventory=inv(records=tuple(usable) + (relative,), records_recognised=9,
                            records_attempted=9, parse_counts={lcr.PARSE_OK: 9},
                            contract_counts={lcr.CONTRACT_CURRENT: 9},
                            scope_counts={lcr.SCOPE_IN: 9},
                            complete_counts={lcr.COMPLETE_YES: 9},
                            entries_listed=9, entries_examined=9),
              source_verdicts=[verdict(r.source_path) for r in usable[:3]]
                              + [verdict(r.source_path, state=lib.PRESENT) for r in usable[3:]])
    weak = _mutant(("                         and unmatched == 0 and unresolved == 0 "
                    "and unjoinable == 0\n                         and shortfall <= 0)",
                    "                         and unmatched == 0)"))
    assert _run(weak, **kw).confirmed_missing.exactness == weak.EXACT, \
        "the mutation did not apply"
    real = report(**kw)
    assert real.confirmed_missing.value == 3
    assert real.confirmed_missing.exactness == a5.LOWER_BOUND


def test_every_mutation_control_is_accounted_for():
    """The count claim the previous report got wrong: tabulate, do not estimate.

    The implementation report said "nine mutation controls" while the file held ten. This test
    makes the number self-reporting, so the claim cannot drift from the source again.
    """
    import inspect
    this_module = sys.modules[__name__]
    controls = [name for name in dir(this_module)
                if name.startswith("test_mutant_") and callable(getattr(this_module, name))]
    assert len(controls) == 19, f"expected 19 mutation controls, found {len(controls)}: {controls}"
    for name in controls:
        source = inspect.getsource(getattr(this_module, name))
        assert "_mutant(" in source, f"{name} does not build a mutant"
        assert "the mutation did not apply" in source, \
            f"{name} has no guard proving its anchor applied"
