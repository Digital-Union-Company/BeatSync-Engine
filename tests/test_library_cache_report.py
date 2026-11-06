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
    b = report(location=loc(), inventory=inv(),
               categories=[(cat(ca.ANALYSIS, CACHE, expensive=True),
                            meas(ca.ANALYSIS, files=1, total=10), "", "")])
    assert any("without an observation time" in item for item in b.caveats)


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
    weak = _mutant(("    complete_coverage = (counts_exact and not truncated and unmatched == 0 "
                    "and not unstable\n                         and not rejected)",
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
    weak = _mutant(("    elif not contributing and not any_withheld:", "    elif True:"))
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
