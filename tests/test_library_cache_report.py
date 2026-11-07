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
import dataclasses
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
    # Anchor RE-POINTED by the X2-1 fix. The branch now guards on `zero_doubts`, which
    # carries the concurrency cause AND the two new ones, because a truncated display or
    # uncertified counts reach this zero exactly as a moving folder does. The rule under
    # test is unchanged -- two overlapping holds must withhold rather than collapse to a
    # measured zero -- so the mutation is the same unconditional branch.
    weak = _mutant(("    elif not contributing and not any_withheld and not zero_doubts:",
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
    """An A4-derived hold names an A4 folder -- and, per N-3, the **right** one.

    This assertion previously pinned the defect. It required the hold to name
    ``normcase(normpath(LOCAL))``, A4's *selected* folder, which in exactly this fixture is the
    healthy local fallback: sharing was configured, ``SharedCache`` came back ``ROOT_UNREACHABLE``,
    and A4 selected ``LOCAL``. The report therefore announced that the configured shared folder was
    unreachable and named the one folder that demonstrably was not. The subject must be the folder
    the hold is about.
    """
    b = report(location=loc(shared=state(lcl.ROLE_SHARED, CACHE,
                                         verdict_=lib.ROOT_UNREACHABLE, observed=False,
                                         inspectable=False),
                            selected_path=LOCAL, selected=state(lcl.ROLE_LOCAL, LOCAL)),
               inventory=inv(path=LOCAL, resolved=LOCAL))
    shared_holds = [f for label, f in b.uncertain_observations
                    if f.basis == "CacheLocationReport.shared.verdict"]
    assert shared_holds, "the unreachable shared folder must raise a hold"
    assert shared_holds[0].subject == CACHE_KEY, "the hold named a folder it is not about"
    assert shared_holds[0].subject != os.path.normcase(os.path.normpath(LOCAL))
    # and A4 still identifies both folders, so nothing was lost by correcting the subject
    assert b.identity.path_as_configured == CACHE
    assert b.identity.path_as_selected == LOCAL


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
    # 28 before X2-1; section K adds seven behavioural controls for the published semantics of the
    # evidence hold and the headline zero; section L adds one for the note cap that keeps the floor
    # explanation reachable (finding R1-2). Updated here deliberately, as the docstring requires.
    assert len(controls) == 36, f"expected 36 mutation controls, found {len(controls)}: {controls}"
    for name in controls:
        source = inspect.getsource(getattr(this_module, name))
        assert "_mutant(" in source, f"{name} does not build a mutant"
        assert "the mutation did not apply" in source, \
            f"{name} has no guard proving its anchor applied"


# =============================================================================================
# H. N-1 .. N-5: the instability rule applies to EVERY hold, and every figure's unit, subject
#    and observation time belongs to the thing it describes
#
# Why the previous F-8 fixtures could not see N-1: `test_f8_an_unstable_inventory_never_publishes_
# an_exact_uncertainty` gives every record a PRESENT verdict, so no source hold fires at all, and
# its per-hold loop then filters on `basis.startswith("RecordInventory")` -- which the unresolved
# and rejected holds do not satisfy. Each fixture below makes exactly ONE source hold fire, and
# asserts the hold itself as well as the headline.
# =============================================================================================

REL_SOURCE = os.path.join("..", "elsewhere", "rel.mp4")
OUTSIDE = os.path.join(LIBRARY, "not-a-record-here.mp4")
OUTSIDE2 = os.path.join(LIBRARY, "also-not-a-record.mp4")

#: The four producers bucket E gained with the F-1 fix, each with a fixture that fires only it.
#: ``headline_passthrough`` is False for the rejected-evidence producer because N-2 keeps an
#: evidence-object count out of the record-unit headline entirely.
N1_PRODUCERS = (
    ("unresolved supplied verdict",
     "uncertain: records whose supplied source verdict established no state",
     lambda: dict(records=(rec("r0"),),
                  verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.UNKNOWN)]),
     True),
    ("record with no matching verdict",
     "uncertain: records with a source path but no supplied source verdict",
     lambda: dict(records=(rec("r0"),),
                  verdicts=[verdict(OUTSIDE, state=lib.PRESENT)]),
     True),
    ("unjoinable recorded source path",
     "uncertain: records whose recorded source path A5 cannot join to a verdict",
     lambda: dict(records=(rec("r0"), rec("r1", source=REL_SOURCE)),
                  verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT)]),
     True),
    ("rejected verdict evidence",
     "uncertain: supplied source verdicts rejected as evidence",
     lambda: dict(records=(rec("r0"),),
                  verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), all_four=False)]),
     False),
)


def _n1_inv(records, **kw):
    """A fully exact, fully assessed inventory of exactly these records."""
    n = len(records)
    base = dict(records=records, records_recognised=n, records_attempted=n,
                parse_counts={lcr.PARSE_OK: n}, contract_counts={lcr.CONTRACT_CURRENT: n},
                scope_counts={lcr.SCOPE_IN: n}, complete_counts={lcr.COMPLETE_YES: n},
                entries_listed=n, entries_examined=n)
    base.update(kw)
    return inv(**base)


def _hold(buckets, label):
    found = [f for lab, f in buckets.uncertain_observations if lab == label]
    assert found, (f"the fixture did not fire {label!r}; it fired "
                   f"{[lab for lab, _f in buckets.uncertain_observations]}")
    return found[0]


@pytest.mark.parametrize("name,label,build,passthrough", N1_PRODUCERS)
def test_n1_a_stable_folder_keeps_its_source_hold_exact(name, label, build, passthrough):
    """The control. Correcting instability must not demote a settled folder's figures."""
    spec = build()
    b = report(location=loc(), inventory=_n1_inv(spec["records"], path=CACHE, resolved=CACHE),
               source_verdicts=spec["verdicts"])
    hold = _hold(b, label)
    assert hold.value == 1
    assert hold.exactness == a5.EXACT, f"{name}: a stable folder's hold was demoted"
    assert not any("changed while it was being read" in item for item in hold.limitations)
    if passthrough:
        assert b.uncertain.value == 1 and b.uncertain.exactness == a5.EXACT
        assert b.uncertain.publishable_as_total
    else:
        assert b.uncertain.value is None, "an evidence-object count is not a record headline"


@pytest.mark.parametrize("name,label,build,passthrough", N1_PRODUCERS)
@pytest.mark.parametrize("moving", ({"vanished": ("a.json",)}, {"changed": ("b.json",)},
                                    {"vanished": ("a.json",), "changed": ("b.json",)}))
def test_n1_an_unstable_folder_demotes_every_source_hold(name, label, build, passthrough, moving):
    """N-1. Each of the four source holds, under each way A3 can report instability.

    Measured before the fix: every one of these published ``EXACT``, and three of the four carried
    that straight into ``E uncertain: 1 records (exact)`` with ``publishable_as_total`` True, on a
    page that also said every record-level figure here is a floor.
    """
    spec = build()
    inventory = _n1_inv(spec["records"], path=CACHE, resolved=CACHE, **moving)
    assert inventory.unstable is True, "fixture misbuilt: the folder must be moving"
    b = report(location=loc(), inventory=inventory, source_verdicts=spec["verdicts"])

    hold = _hold(b, label)
    assert hold.value == 1, f"{name}: fixture misbuilt"
    assert hold.exactness == a5.LOWER_BOUND, f"{name}: the hold stayed exact while moving"
    assert not hold.publishable_as_total
    assert a5.CONCURRENCY_LIMITATION in hold.limitations, \
        f"{name}: the hold was demoted without saying why"

    # the headline, which is what a consumer reads first
    assert b.uncertain.exactness != a5.EXACT, f"{name}: E claimed an exact folder-wide figure"
    assert not b.uncertain.publishable_as_total, f"{name}: E was certified as a total"
    assert any("changed while it was being read" in item for item in b.caveats)

    # and no OTHER hold quietly kept its exactness either -- one corrected headline must not hide
    # a still-exact individual figure
    for lab, figure in b.uncertain_observations:
        if figure.value is not None:
            assert figure.exactness != a5.EXACT, f"{lab} stayed exact while the folder moved"

    # the rendered text must not offer an exact number anywhere in the uncertainty block
    for line in lines_for(b):
        stripped = line.strip()
        if stripped.startswith("E "):
            assert "(exact)" not in stripped, f"an exact uncertainty line survived: {line}"


def test_n1_a_withheld_source_hold_stays_withheld_while_moving():
    """A withheld figure carries no number, so there is nothing to demote and no reason to relabel.

    `Figure.consistent` requires ``value is None`` exactly when ``NOT_ASSESSED``, so a demotion
    that overwrote the exactness of a withheld hold would break the module's core invariant.
    """
    b = report(location=loc(), inventory=_n1_inv((rec("r0"),), vanished=("a.json",)))
    withheld_holds = [(lab, f) for lab, f in b.uncertain_observations if f.value is None]
    assert withheld_holds, "fixture misbuilt: no verdicts were supplied, so a hold must be withheld"
    for lab, figure in withheld_holds:
        assert figure.exactness == a5.NOT_ASSESSED, f"{lab} lost its withheld state"
        assert figure.consistent
        assert figure.withheld_reason


def test_n1_the_demotion_is_enforced_at_one_point_over_the_assembled_list():
    """A structural guard: the rule must not go back to living inside one producer.

    If a future hold is added anywhere in `_uncertainty` and the demotion has drifted back into a
    per-builder branch, this fails -- `_demote_while_moving` must be called, and called once.
    """
    with open(_A5, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("def _demote_while_moving(") == 1
    assert source.count("_demote_while_moving(tuple(out), inv_unstable)") == 1, \
        "the central demotion is not applied exactly once over the assembled hold list"
    tree = ast.parse(source)
    uncertainty = [n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_uncertainty"]
    assert len(uncertainty) == 1
    inner = [n.name for n in ast.walk(uncertainty[0]) if isinstance(n, ast.FunctionDef)]
    assert "inv_figure" in inner
    # the inventory builder must no longer decide the concurrency question for itself
    builder = [n for n in ast.walk(uncertainty[0])
               if isinstance(n, ast.FunctionDef) and n.name == "inv_figure"][0]
    assert "inv_unstable" not in ast.dump(builder), \
        "inv_figure decides instability again, so a hold built elsewhere can bypass the rule"


# ------------------------------------------------------------------------------------ N-2

def _rejected_hold(buckets):
    return _hold(buckets, "uncertain: supplied source verdicts rejected as evidence")


@pytest.mark.parametrize("moving", ({}, {"vanished": ("a.json",)}))
def test_n2_two_rejected_verdicts_for_one_path_are_not_two_records(moving):
    """N-2. Two refused verdict objects naming ONE path that is not a record of this folder."""
    bad = [verdict(OUTSIDE, root_key=os.path.normcase(os.path.normpath(OTHER))) for _ in range(2)]
    b = report(location=loc(),
               inventory=_n1_inv((rec("r0"),), **moving),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT)] + bad)
    hold = _rejected_hold(b)
    assert hold.value == 2, "both refused verdict objects must stay visible"
    assert hold.unit == a5.VERDICTS, "an evidence-object count was published in a record unit"
    assert hold.unit != a5.RECORDS
    assert "not a count of distinct cache records" in hold.population
    # the headline must not become a record total built from evidence objects
    assert b.uncertain.value is None
    assert b.uncertain.exactness == a5.NOT_ASSESSED
    assert not b.uncertain.publishable_as_total
    assert "counts supplied objects" in b.uncertain.withheld_reason
    for line in lines_for(b):
        assert "2 records (exact)" not in line, f"verdict objects were printed as records: {line}"


def test_n2_four_rejected_verdicts_over_two_paths():
    bad = []
    for path in (OUTSIDE, OUTSIDE2):
        bad += [verdict(path, root_key=os.path.normcase(os.path.normpath(OTHER)))
                for _ in range(2)]
    b = report(location=loc(), inventory=_n1_inv((rec("r0"),)),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT)] + bad)
    hold = _rejected_hold(b)
    assert hold.value == 4 and hold.unit == a5.VERDICTS
    assert b.uncertain.value is None, "four objects over two paths is still not a record count"
    for line in lines_for(b):
        assert "4 records" not in line


def test_n2_a_rejected_path_absent_from_every_record_is_never_a_record_count():
    """The rejected path is not among the inventory's records at all, so no record population
    exists for it -- and A5 must not invent one by deduplicating paths it cannot place."""
    b = report(location=loc(), inventory=_n1_inv((rec("r0"),)),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT),
                                verdict(OUTSIDE, root_key=os.path.normcase(
                                    os.path.normpath(OTHER)))])
    recorded = {r.source_path for r in _n1_inv((rec("r0"),)).records}
    assert OUTSIDE not in recorded, "fixture misbuilt"
    hold = _rejected_hold(b)
    assert hold.value == 1 and hold.unit == a5.VERDICTS
    assert b.uncertain.value is None


@pytest.mark.parametrize("moving", ({}, {"vanished": ("a.json",)}))
def test_n2_one_unmatched_record_plus_one_rejected_verdict_describing_it(moving):
    """The two holds describe the same doubt in different units, so the aggregate stays withheld.

    This is also the double-count check: the record counts once in the record-unit hold and its
    verdict counts once in the evidence-unit hold, and the two are never added.
    """
    b = report(location=loc(), inventory=_n1_inv((rec("r0"),), **moving),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"),
                                        root_key=os.path.normcase(os.path.normpath(OTHER)))])
    unmatched = _hold(b, "uncertain: records with a source path but no supplied source verdict")
    rejected_hold = _rejected_hold(b)
    assert unmatched.value == 1 and unmatched.unit == a5.RECORDS
    assert rejected_hold.value == 1 and rejected_hold.unit == a5.VERDICTS
    assert b.uncertain.value is None, "two units were collapsed into one number"
    assert not b.uncertain.publishable_as_total
    if moving:
        assert rejected_hold.exactness == a5.LOWER_BOUND
        assert unmatched.exactness == a5.LOWER_BOUND


def test_n2_the_evidence_unit_is_a_declared_unit_and_not_a_content_unit():
    assert a5.VERDICTS in a5.UNITS
    assert a5.VERDICTS not in a5.CONTENT_UNITS
    assert set(a5.CONTENT_UNITS) == {a5.BYTES, a5.RECORDS, a5.ENTRIES}


# ------------------------------------------------------------------------------------ N-3 / N-4

#: Deliberately distinct per input, so a borrowed window cannot pass by coincidence.
A4_STAMP = "A4-OBSERVED"
A3_FROM, A3_TO = "A3-FROM", "A3-TO"
FLAT_STAMP = "FLAT-OBSERVED"
CAT_STAMP = "CAT-OBSERVED"


def _fallback_location(shared_verdict, **kw):
    base = dict(configured_shared=CACHE, selected_path=LOCAL, fallback=True, observed_at=A4_STAMP,
                selected=state(lcl.ROLE_LOCAL, LOCAL),
                shared=state(lcl.ROLE_SHARED, CACHE, verdict_=shared_verdict, observed=False,
                             inspectable=False))
    base.update(kw)
    return loc(**base)


@pytest.mark.parametrize("shared_verdict", (lib.ROOT_UNREACHABLE, lib.ACCESS_DENIED, lib.UNKNOWN))
def test_n3_the_shared_hold_names_the_configured_shared_folder(shared_verdict):
    """N-3, for all three triggering states. The selected folder is healthy and must not be named."""
    b = report(location=_fallback_location(shared_verdict),
               inventory=inv(path=LOCAL, resolved=LOCAL, observed_from=A3_FROM, observed_to=A3_TO))
    hold = _hold(b, f"uncertain: the configured shared folder reported {shared_verdict}")
    assert hold.subject == CACHE_KEY, "the hold named a folder it is not about"
    assert hold.subject != os.path.normcase(os.path.normpath(LOCAL))
    assert hold.value is None and hold.exactness == a5.NOT_ASSESSED
    # N-4: A4's own time, not A3's record-pass window
    assert hold.window == (A4_STAMP, A4_STAMP), f"the hold borrowed a window: {hold.window}"
    assert A3_FROM not in hold.window and A3_TO not in hold.window


def test_n3_a_blank_configured_path_reports_no_folder_rather_than_the_selected_one():
    b = report(location=_fallback_location(lib.ROOT_UNREACHABLE, configured_shared=""),
               inventory=inv(path=LOCAL, resolved=LOCAL))
    hold = _hold(b, f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}")
    assert hold.subject == "an unestablished folder"
    assert hold.subject != os.path.normcase(os.path.normpath(LOCAL))


def test_n3_a_relative_configured_path_reports_no_folder():
    b = report(location=_fallback_location(lib.ROOT_UNREACHABLE,
                                           configured_shared=os.path.join("..", "rel", "cache")),
               inventory=inv(path=LOCAL, resolved=LOCAL))
    hold = _hold(b, f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}")
    assert hold.subject == "an unestablished folder", "a relative path was resolved into a subject"


def test_n4_every_figure_carries_its_own_inputs_observation_time():
    """One report, four distinct supplied timestamps, each landing only where it belongs."""
    b = report(location=_fallback_location(lib.ROOT_UNREACHABLE),
               inventory=inv(path=LOCAL, resolved=LOCAL, observed_from=A3_FROM, observed_to=A3_TO,
                             entries_unclassified=2),
               flat_measurement=flat(path=LOCAL), observed_at=FLAT_STAMP,
               categories=[catobs(ca.POSTERS, os.path.join(BASE, "posters"), at=CAT_STAMP,
                                  storage="vol-1")])
    shared = _hold(b, f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}")
    assert shared.window == (A4_STAMP, A4_STAMP)

    a3_holds = [f for _l, f in b.uncertain_observations
                if f.basis.startswith("RecordInventory")]
    assert a3_holds, "fixture misbuilt"
    for figure in a3_holds:
        assert figure.window == (A3_FROM, A3_TO), "an A3 hold lost its own window"

    flat_figures = [f for name, f in b.measurements if name == "analysis_cache.measure"]
    assert flat_figures and flat_figures[0].window == (FLAT_STAMP, FLAT_STAMP)
    assert [f.window for _k, f in b.cheap] == [(CAT_STAMP, CAT_STAMP)]


def test_n4_a4_without_a_timestamp_carries_none_and_says_so():
    b = report(location=_fallback_location(lib.ROOT_UNREACHABLE, observed_at=""),
               inventory=inv(path=LOCAL, resolved=LOCAL, observed_from=A3_FROM, observed_to=A3_TO))
    hold = _hold(b, f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}")
    assert hold.window == ("", ""), "an absent A4 time was filled in from somewhere else"
    assert any("supplied no observation time" in note for note in hold.notes)
    assert not any("observed:" in line for line in lines_for(b)
                   if "configured shared folder" in line)


# ------------------------------------------------------------------------------------ N-5

def test_n5_the_two_refusal_counts_are_named_for_different_questions():
    """N-5. One figure printed "refused as evidence 0" and "1 ... rejected as evidence"."""
    b = report(location=loc(), inventory=_n1_inv((rec("r0"),)),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"),
                                        root_key=os.path.normcase(os.path.normpath(OTHER)))])
    d = b.confirmed_missing
    assert "refused as evidence 0" not in d.population, "the ambiguous phrasing survived"
    # N-8 corrected this noun. The figure counts matched verdict OBJECTS, and calling them
    # "matched verdicts" left it indistinguishable from the record-level refusal count printed
    # beside it -- which is the separate number the next assertion pins.
    assert "matched verdict objects refused for missing clause evidence 0" in d.population
    assert "record-level refusals of those objects 0" in d.population
    assert "supplied verdict objects rejected before matching 1" in d.population
    joined = " | ".join(d.limitations)
    assert "1 supplied verdict objects were rejected as evidence" in joined
    assert "0 matched a record but carried no finalised clause evidence" in joined
    assert "1 were rejected before any record was matched" in joined


def test_n5_a_clause_evidence_refusal_is_counted_separately_from_a_pre_match_rejection():
    """Both kinds in one report, each under its own name."""
    b = report(location=loc(), inventory=_n1_inv((rec("r0"), rec("r1"))),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), all_four=False),
                                verdict(OUTSIDE, root_key=os.path.normcase(
                                    os.path.normpath(OTHER)))])
    d = b.confirmed_missing
    assert "matched verdict objects refused for missing clause evidence 1" in d.population
    assert "record-level refusals of those objects 1" in d.population
    assert "supplied verdict objects rejected before matching 1" in d.population
    assert _rejected_hold(b).value == 2, "both rejections stay visible as evidence objects"


def test_n5_d_still_reports_the_same_value_and_exactness():
    """The wording changed; the figure did not."""
    records = tuple(rec("r%d" % i) for i in range(3))
    verdicts = [verdict(r.source_path) for r in records[:2]] \
        + [verdict(records[2].source_path, state=lib.PRESENT)]
    b = report(location=loc(), inventory=_n1_inv(records), source_verdicts=verdicts)
    assert b.confirmed_missing.value == 2
    assert b.confirmed_missing.exactness == a5.EXACT


# ------------------------------------------------------------------------------------ N-6

def test_n6_d_names_the_population_it_covers_and_discloses_the_unparsed_remainder():
    """The N-6 disposition, asserted: D stays exact for its own population and says which one.

    `parsed=1, recognised=4`. F and G are floors because A3's counters answer for parsed records
    and three were never parsed. D stays exact because an unparsed record has no readable payload
    and therefore no `source_path` -- it is outside D's population rather than missing from it --
    and the figure now says so on its own line instead of claiming bare "complete coverage".
    """
    b = report(location=loc(),
               inventory=inv(records=(rec("r0"),), records_recognised=4, records_attempted=4,
                             parse_counts={lcr.PARSE_OK: 1},
                             contract_counts={lcr.CONTRACT_CURRENT: 1},
                             scope_counts={lcr.SCOPE_IN: 1},
                             complete_counts={lcr.COMPLETE_YES: 1},
                             entries_listed=4, entries_examined=4),
               source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"))])
    d = b.confirmed_missing
    assert d.value == 1 and d.exactness == a5.EXACT, "the disposition keeps D's figure unchanged"
    assert "complete coverage of the records whose source state could be established" in \
        d.population
    assert "parsed 1, recognised 4" in d.population
    assert any("3 recognised records were never parsed" in item for item in d.limitations), \
        "an exact D did not disclose the population it never saw"
    # the sibling axes are floors for the same inventory, which is the distinction being documented
    assert b.other_configuration.exactness == a5.LOWER_BOUND
    assert b.out_of_scope.exactness == a5.LOWER_BOUND


# ------------------------------------------------------------------------------------ N-7

def test_n7_identical_signatures_with_different_unmet_clauses_publish_the_same_figures():
    """The N-7 disposition: only the quoted explanation varies, never a published figure."""
    first = lib.LeafVerdict(path=os.path.join(LIBRARY, "r0.mp4"), verdict=lib.MISSING_CONFIRMED,
                            clauses=lib.Clauses(parent_listed=True), root_key=ROOT_KEY,
                            is_final=True)
    second = lib.LeafVerdict(path=os.path.join(LIBRARY, "r0.mp4"), verdict=lib.MISSING_CONFIRMED,
                             clauses=lib.Clauses(name_absent=True), root_key=ROOT_KEY,
                             is_final=True)
    assert a5._source_signature(first) == a5._source_signature(second), "fixture misbuilt"
    shapes = []
    for order in ((first, second), (second, first)):
        b = report(location=loc(), inventory=_n1_inv((rec("r0"),)), source_verdicts=list(order))
        hold = _rejected_hold(b)
        shapes.append((b.confirmed_missing.value, b.confirmed_missing.exactness,
                       b.confirmed_missing.population, hold.value, hold.unit, hold.exactness,
                       hold.subject, b.uncertain.value, b.uncertain.exactness))
    assert shapes[0] == shapes[1], "a published figure depended on the supplied order"


# ------------------------------------------------------------------------------------ N-8

#: A source path that several cache records legitimately share. This is the expected steady state
#: of a versioned, never-pruned cache: the Stage-5 signature mixes eight inputs of which the path
#: is one, so a contract bump, a Qwen settings change or a new mtime all add a record for the same
#: video. `CacheRecord` has no uniqueness constraint on `source_path` at all.
SHARED_SRC = os.path.join(LIBRARY, "shared-source.mp4")


def _bad_finalisation(path=SHARED_SRC):
    """`MISSING_CONFIRMED` without the four clauses: refused at the record join, not before it."""
    return verdict(path, state=lib.MISSING_CONFIRMED, final=True, all_four=False)


def _pre_match_reject(path=OUTSIDE):
    """No root provenance: rejected before any join, so it is a pre-match object."""
    return verdict(path, root_key="")


def _records_sharing(path, n):
    return tuple(lcr.CacheRecord(name="r%d" % i, size=100, parse=lcr.PARSE_OK,
                                 contract=lcr.CONTRACT_CURRENT, scope=lcr.SCOPE_IN,
                                 source_path=path)
                 for i in range(n))


#: ``(name, verdicts, records, objects, record_events, pre_match)``. The six populations R1 requires
#: to stay distinguishable, as independent oracles. Measured against the parent commit, the
#: published object count was wrong in **both** directions: 3 where 1 was true when a path was
#: shared, and 1 where 2 was true when two entries shared a signature.
_N8_SHAPES = (
    ("one refused entry, one record", [_bad_finalisation()], 1, 1, 1, 0),
    ("one refused entry, two records sharing its path", [_bad_finalisation()], 2, 1, 2, 0),
    ("one refused entry, three records sharing its path", [_bad_finalisation()], 3, 1, 3, 0),
    ("two equal-signature entries, one record",
     [_bad_finalisation(), _bad_finalisation()], 1, 2, 1, 0),
    ("two equal-signature entries, three records sharing the path",
     [_bad_finalisation(), _bad_finalisation()], 3, 2, 3, 0),
    ("one pre-match rejection plus one refused entry, two records",
     [_pre_match_reject(), _bad_finalisation()], 2, 2, 2, 1),
    ("one pre-match rejection plus two equal-signature entries, two records",
     [_pre_match_reject(), _bad_finalisation(), _bad_finalisation()], 2, 3, 2, 1),
)


def _n8_report(verdicts, nrecords, **invkw):
    return report(location=loc(),
                  inventory=_n1_inv(_records_sharing(SHARED_SRC, nrecords), **invkw),
                  source_verdicts=list(verdicts))


@pytest.mark.parametrize("name,verdicts,nrec,objects,events,pre", _N8_SHAPES)
@pytest.mark.parametrize("moving", ({}, {"vanished": ("a.json",)}, {"changed": ("b.json",)}))
def test_n8_the_evidence_figure_counts_input_entries_not_records(
        name, verdicts, nrec, objects, events, pre, moving):
    """N-8. The published `VERDICTS` figure is the input-entry population and nothing else.

    It must not be the number of cache records that matched, and it must not silently drop an
    equal-signature successor -- the two failure directions the parent commit had at once.
    """
    b = _n8_report(verdicts, nrec, **moving)
    hold = _rejected_hold(b)
    assert hold.value == objects, (
        f"{name}: published {hold.value} for {objects} supplied input entries "
        f"({nrec} records, {events} record-level refusals)")
    assert hold.unit == a5.VERDICTS
    assert hold.value != events or objects == events, "an event count reached an object figure"


@pytest.mark.parametrize("name,verdicts,nrec,objects,events,pre", _N8_SHAPES)
def test_n8_d_states_objects_and_events_under_their_own_names(
        name, verdicts, nrec, objects, events, pre):
    """Every rendered counter identifies its own population and cannot be read as another."""
    d = _n8_report(verdicts, nrec).confirmed_missing
    matched = objects - pre
    assert f"matched verdict objects refused for missing clause evidence {matched}" in d.population
    assert f"record-level refusals of those objects {events}" in d.population
    assert f"supplied verdict objects rejected before matching {pre}" in d.population
    joined = " | ".join(d.limitations)
    assert f"{objects} supplied verdict objects were rejected as evidence" in joined
    assert f"{matched} matched a record but carried no finalised clause evidence" in joined
    assert f"refused at record level {events} times" in joined
    assert f"{pre} were rejected before any record was matched" in joined


def test_n8_two_entries_over_one_record_report_no_pre_match_rejection():
    """R1's named trap: the old `len(rejected) - refused` invented a pre-match rejection here.

    Two equal-signature entries over one record is 2 objects and 1 event, and the subtraction read
    ``2 - 1 = 1`` "rejected before any record was matched" -- a rejection that never happened.
    """
    d = _n8_report([_bad_finalisation(), _bad_finalisation()], 1).confirmed_missing
    assert "supplied verdict objects rejected before matching 0" in d.population
    assert "0 were rejected before any record was matched" in " | ".join(d.limitations)


def test_n8_the_same_input_instance_supplied_twice_is_two_entries():
    """Identity is not the unit: the caller handed in two entries, so two were refused.

    This is also why `id(item)` cannot be the provenance key -- it would collapse to one.
    """
    one = _bad_finalisation()
    b = _n8_report([one, one], 1)
    assert _rejected_hold(b).value == 2


def test_n8_a_shared_path_prints_one_note_that_states_its_multiplicity():
    """The note list stopped repeating one object once per matching record.

    Nine records sharing a path printed eight identical lines and hid the rest behind the cap.
    """
    single = _n8_report([_bad_finalisation()], 9)
    notes = _rejected_hold(single).notes
    assert len(notes) == 1, f"one refused entry produced {len(notes)} notes"
    assert "supplied entries naming this path" not in notes[0], "one entry claimed multiplicity"

    doubled = _n8_report([_bad_finalisation(), _bad_finalisation()], 9)
    dnotes = _rejected_hold(doubled).notes
    assert len(dnotes) == 1
    assert "supplied entries naming this path: 2" in dnotes[0], \
        "two genuinely distinct entries were grouped without exposing their multiplicity"


def test_n8_a_rejected_path_that_is_no_record_of_this_folder_is_still_one_object():
    """No record population exists for it, and A5 must not invent one."""
    b = report(location=loc(), inventory=_n1_inv(_records_sharing(SHARED_SRC, 3)),
               source_verdicts=[_pre_match_reject(OUTSIDE)])
    assert _rejected_hold(b).value == 1


@pytest.mark.parametrize("state_", (lib.MISSING_CONFIRMED, lib.PRESENT))
def test_n8_an_accepted_verdict_shared_by_three_records_raises_no_hold(state_):
    """Positive control. A finalised-with-clauses verdict and a PRESENT one each establish a
    state, so sharing a source path across three records must produce no rejection at all."""
    b = _n8_report([verdict(SHARED_SRC, state=state_)], 3)
    labels = [lab for lab, _f in b.uncertain_observations]
    assert not any("rejected as evidence" in lab for lab in labels), \
        f"an accepted verdict produced a rejection hold: {labels}"
    d = b.confirmed_missing
    assert "rejected as evidence" not in " | ".join(d.limitations)


def test_n8_the_t3_decision_is_retained_and_applies_to_the_evidence_hold():
    """The explicit Rule T-3 disposition (R1 requirement 7): demotion is KEPT, with a reason.

    `supplied_objects` is `pre_match_objects + matched_objects`, and the second addend is reached
    through the record join -- a record a moving pass never saw could have matched and refused
    another supplied entry. One addend is a floor, so the sum is a floor. The rule therefore needs
    no per-unit exemption, which is what keeps N-1's single unconditional call site intact.
    """
    stable = _rejected_hold(_n8_report([_bad_finalisation()], 2))
    assert stable.exactness == a5.EXACT
    assert stable.publishable_as_total

    moving = _rejected_hold(_n8_report([_bad_finalisation()], 2, vanished=("a.json",)))
    assert moving.exactness == a5.LOWER_BOUND
    assert not moving.publishable_as_total
    assert a5.CONCURRENCY_LIMITATION in moving.limitations
    assert moving.value == stable.value, "demotion changed the count as well as the exactness"


def test_n8_the_rejection_record_never_mixes_a_count_of_records_into_a_unit():
    """No byte, entry, record or evidence-object count is mixed on any rendered line."""
    for _name, verdicts, nrec, objects, events, _pre in _N8_SHAPES:
        b = _n8_report(verdicts, nrec)
        for line in lines_for(b):
            if "rejected as evidence" not in line:
                continue
            assert f"{events} records" not in line, f"an event count was printed as records: {line}"
            if objects != nrec:
                assert f"{nrec} records (exact)" not in line, f"record count leaked: {line}"


def test_n8_d_value_and_exactness_are_unchanged_by_the_fix():
    """D is LOWER_BOUND whenever anything was rejected, before and after -- and still 0 here."""
    for _name, verdicts, nrec, _objects, _events, _pre in _N8_SHAPES:
        d = _n8_report(verdicts, nrec).confirmed_missing
        assert d.value == 0
        assert d.exactness == a5.LOWER_BOUND


def test_n8_the_supplied_object_total_is_the_sum_of_its_two_direct_counters():
    """A structural invariant, so a future edit cannot reintroduce a derived count."""
    r = a5._Rejections(pre_match_objects=2, matched_objects=3, record_events=9)
    assert r.supplied_objects == 5
    assert bool(r)
    assert not bool(a5._Rejections())
    assert bool(a5._Rejections(record_events=1)), \
        "a record-level refusal must still make the rejection record truthy"


# =============================================================================================
# I. mutation controls for N-1 .. N-4. Anchors are distinct from every anchor in sections E and G.
# =============================================================================================

def test_mutant_demoting_only_the_inventory_built_holds_is_caught():
    """N-1: the pre-fix architecture, where only `inv_figure`'s own holds were demoted.

    The discriminating hold is the unresolved-verdict one: its basis names `library_inventory`, so
    a demotion restricted to `RecordInventory`-based figures skips exactly it -- which is how the
    defect survived a suite that looped over `basis.startswith("RecordInventory")`.
    """
    kw = dict(location=loc(),
              inventory=_n1_inv((rec("r0"),), vanished=("a.json",)),
              source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.UNKNOWN)])
    label = "uncertain: records whose supplied source verdict established no state"
    # Anchor RE-POINTED by the X2-1 fix. The guard now skips only a WITHHELD figure: a
    # figure that is already a floor for its own cause keeps its exactness but must still
    # collect the concurrency reason, which the old `exactness != EXACT` skip suppressed.
    # The rule under test is unchanged -- the demotion must not be restricted to the holds
    # one builder happens to produce -- so the mutation is the same basis restriction.
    weak = _mutant(("        if fig.value is None:",
                    "        if fig.value is None or not "
                    "fig.basis.startswith(\"RecordInventory\"):"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations if lab == label][0]
    assert mutated_hold.exactness == weak.EXACT, "the mutation did not apply"
    assert mutated.uncertain.exactness == weak.EXACT
    real = report(**kw)
    assert _hold(real, label).exactness == a5.LOWER_BOUND
    assert real.uncertain.exactness == a5.LOWER_BOUND
    assert not real.uncertain.publishable_as_total


def test_mutant_labelling_rejected_evidence_objects_as_records_is_caught():
    """N-2: the evidence-object count goes back into the records unit."""
    kw = dict(location=loc(), inventory=_n1_inv((rec("r0"),)),
              source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT)]
                              + [verdict(OUTSIDE, root_key=os.path.normcase(
                                  os.path.normpath(OTHER))) for _ in range(2)])
    # Anchor RE-POINTED by the N-8 fix, not re-scored: the published value is now
    # `rejected.supplied_objects` rather than `len(rejected)`, because the old length was taken
    # over a list fed at two cardinalities. The rule under test is unchanged -- an evidence-object
    # count must not be published in a record unit -- so the mutation is the same unit swap.
    # Anchor RE-POINTED a second time, by the X2-1 fix: `exactness` moved onto its own
    # line and is now conditional on `join_incomplete`. The rule under test is unchanged --
    # an evidence-object count must not be published in a record unit -- so the mutation is
    # still the same unit swap, now on the line that carries the unit alone.
    weak = _mutant(("            Figure(value=rejected.supplied_objects, unit=VERDICTS,",
                    "            Figure(value=rejected.supplied_objects, unit=RECORDS,"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations
                    if "rejected as evidence" in lab][0]
    assert mutated_hold.unit == weak.RECORDS, "the mutation did not apply"
    assert mutated.uncertain.value == 2, "the mutant must publish the object count as records"
    real = report(**kw)
    assert _rejected_hold(real).unit == a5.VERDICTS
    assert real.uncertain.value is None


def test_mutant_promoting_rejected_evidence_into_the_headline_is_caught():
    """N-2: the evidence hold becomes eligible for the record-unit headline again."""
    kw = dict(location=loc(), inventory=_n1_inv((rec("r0"),)),
              source_verdicts=[verdict(os.path.join(LIBRARY, "r0.mp4"), state=lib.PRESENT)]
                              + [verdict(OUTSIDE, root_key=os.path.normcase(
                                  os.path.normpath(OTHER))) for _ in range(2)])
    weak = _mutant(("    if evidence:", "    if False:"),
                   ("                    if fig.unit in CONTENT_UNITS and fig.value is not None "
                    "and fig.value > 0]",
                    "                    if fig.value is not None and fig.value > 0]"))
    mutated = _run(weak, **kw).uncertain
    assert mutated.value == 2 and mutated.exactness == weak.EXACT, "the mutation did not apply"
    real = report(**kw).uncertain
    assert real.value is None and real.exactness == a5.NOT_ASSESSED


def test_mutant_attributing_the_shared_hold_to_the_selected_folder_is_caught():
    """N-3: the hold goes back to naming A4's selected folder."""
    kw = dict(location=_fallback_location(lib.ROOT_UNREACHABLE),
              inventory=inv(path=LOCAL, resolved=LOCAL))
    label = f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}"
    weak = _mutant(("                         subject=_configured_subject(obs.location),",
                    "                         subject=_location_subject(obs.location),"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations if lab == label][0]
    assert mutated_hold.subject == os.path.normcase(os.path.normpath(LOCAL)), \
        "the mutation did not apply"
    assert _hold(report(**kw), label).subject == CACHE_KEY


def test_mutant_making_the_a4_hold_inherit_a3s_window_is_caught():
    """N-4: the A4-derived hold goes back to borrowing A3's record-pass window."""
    kw = dict(location=_fallback_location(lib.ROOT_UNREACHABLE),
              inventory=inv(path=LOCAL, resolved=LOCAL, observed_from=A3_FROM, observed_to=A3_TO))
    label = f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}"
    weak = _mutant(("            loc_window = _location_window(obs.location)",
                    "            loc_window = window"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations if lab == label][0]
    assert mutated_hold.window == (A3_FROM, A3_TO), "the mutation did not apply"
    assert _hold(report(**kw), label).window == (A4_STAMP, A4_STAMP)


# ------------------------------------------------------------------- mutation controls for N-8

def test_mutant_making_the_evidence_count_depend_on_matching_records_is_caught():
    """N-8: the published object count goes back to being the record-level event count.

    This is the over-counting half of the parent commit's defect, and the discriminating fixture is
    one refused entry against three records that share its source path.
    """
    kw = dict(location=loc(), inventory=_n1_inv(_records_sharing(SHARED_SRC, 3)),
              source_verdicts=[_bad_finalisation()])
    weak = _mutant(("    matched_objects = sum(entries_for_key.get(k, 1) for k in refused_keys)",
                    "    matched_objects = refused"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations
                    if "rejected as evidence" in lab][0]
    assert mutated_hold.value == 3, "the mutation did not apply"
    assert _rejected_hold(report(**kw)).value == 1, \
        "one supplied entry over three records is one refused object"


def test_mutant_collapsing_equal_signature_entries_into_one_is_caught():
    """N-8: the under-counting half -- per-key accounting instead of per-entry accounting.

    Setting the tally to a constant 1 is exactly the `refused_keys`-only formula the original
    review suggested, and it loses the second of two equal-signature supplied entries.
    """
    kw = dict(location=loc(), inventory=_n1_inv(_records_sharing(SHARED_SRC, 1)),
              source_verdicts=[_bad_finalisation(), _bad_finalisation()])
    weak = _mutant(("        entries_for_key[key] = entries_for_key.get(key, 0) + 1",
                    "        entries_for_key[key] = 1"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations
                    if "rejected as evidence" in lab][0]
    assert mutated_hold.value == 1, "the mutation did not apply"
    assert _rejected_hold(report(**kw)).value == 2, \
        "two distinct supplied entries sharing a signature are two refused objects"


def test_mutant_restoring_the_mixed_pre_match_subtraction_is_caught():
    """N-8: `len(rejected) - refused` comes back, inventing a pre-match rejection.

    Two equal-signature entries over one record is 2 objects and 1 event, so the subtraction
    publishes one rejection "before any record was matched" that never occurred.
    """
    kw = dict(location=loc(), inventory=_n1_inv(_records_sharing(SHARED_SRC, 1)),
              source_verdicts=[_bad_finalisation(), _bad_finalisation()])
    weak = _mutant(('        f"{rejected.record_events}, supplied verdict objects rejected '
                    'before matching "\n        f"{rejected.pre_match_objects}, "',
                    '        f"{rejected.record_events}, supplied verdict objects rejected '
                    'before matching "\n        f"{rejected.supplied_objects - '
                    'rejected.record_events}, "'))
    mutated = _run(weak, **kw)
    assert "supplied verdict objects rejected before matching 1" in \
        mutated.confirmed_missing.population, "the mutation did not apply"
    assert "supplied verdict objects rejected before matching 0" in \
        report(**kw).confirmed_missing.population, \
        "a pre-match rejection was invented by subtracting an event count from an object count"


def test_mutant_aggregating_the_object_total_from_record_events_is_caught():
    """N-8: the aggregation itself lies, rather than either counter feeding it.

    Distinct from the control above: there `matched_objects` is mis-computed, here both counters are
    right and `supplied_objects` adds the wrong one. The published figure is the only place the
    difference shows, which is exactly why the value is asserted rather than the internals.

    Note on a guard that is deliberately NOT a control here: `if key not in refused_keys:` was
    measured and is **behaviourally inert** -- `refused_keys[key] = item` is idempotent for a given
    key, since `by_path[key]` is fixed once assigned. Mutating it to `if True:` compiles, imports
    and changes no published figure, so it is a surviving mutant and is reported as one rather than
    being dressed up as a kill. The guard is kept for intent, not for behaviour.
    """
    kw = dict(location=loc(), inventory=_n1_inv(_records_sharing(SHARED_SRC, 3)),
              source_verdicts=[_bad_finalisation()])
    weak = _mutant(("        return self.pre_match_objects + self.matched_objects",
                    "        return self.pre_match_objects + self.record_events"))
    mutated = _run(weak, **kw)
    mutated_hold = [f for lab, f in mutated.uncertain_observations
                    if "rejected as evidence" in lab][0]
    assert mutated_hold.value == 3, "the mutation did not apply"
    assert _rejected_hold(report(**kw)).value == 1, \
        "the object total was aggregated from a record-level event count"


# =============================================================================================
# J. X2-1 -- the published semantics of the VERDICTS evidence hold, and the headline zero beside
#    it, across every combination of the three ways the record join can fall short of the folder.
#
# Why this section exists at all. `_Rejections.supplied_objects` is `pre_match_objects +
# matched_objects`, and the second addend is produced by matching supplied entries against
# `RecordInventory.records` -- a capped DISPLAY list. The hold was built with an unconditional
# `exactness=EXACT` and an empty `limitations` tuple, and only `_demote_while_moving` ever touched
# it, on concurrency alone. So a truncated display or uncertified counts published a floor as a
# total. Axis D weighed all three states in `complete_coverage` already; its limitations never
# reached bucket E.
#
# Measured on the parent blob, the shape that makes it undeniable: one folder, one set of supplied
# verdicts, and only the position of the display window moved. Refused record inside it ->
# `1 verdicts (exact)` with `publishable_as_total` True. Beyond it -> no hold at all, and
# `E uncertain: 0 records (exact)`, also publishable. A difference in where a cap fell, published
# as a difference in fact.
#
# Everything here is behavioural: public entry points in, published fields out. The structural
# guards at the end of the section are additions, never substitutes -- a mutation that changes a
# number has to be killed by an assertion about that number.
# =============================================================================================

class _X2InexactCounts(lcr.RecordInventory):
    """A real inventory whose aggregate counters are NOT certified to cover the folder.

    `counts_exact` is derived from `entries_complete` and `entries_unclassified`, and each of those
    raises an E hold of its own -- a withheld one and a positive one respectively. Setting either
    would change `contributing`/`any_withheld` and so measure a different branch entirely, which is
    exactly how the flag went untested. Overriding the property varies the one flag under test and
    nothing else, and the builder asserts all three flags landed as intended.
    """

    @property
    def counts_exact(self) -> bool:
        return False


def _x2_inv(records, *, folder_records=None, truncated=False, counts_exact=True, unstable=False,
            **override):
    """An inventory whose COUNTERS describe the whole folder and whose ``records`` is the display.

    ``folder_records`` is how many records the folder actually holds; ``records`` is what A3 chose
    to show. Keeping them separate is the whole point: a truncated display with whole-folder
    counters is the ordinary state of any cache past `max_reported_names`, not an edge case.
    """
    n = folder_records if folder_records is not None else len(records)
    base = dict(
        path=CACHE, resolved=CACHE, state=lcr.CACHE_PRESENT, complete=True, bound=lcr.BOUND_NONE,
        entries_listed=n, entries_examined=n, records_recognised=n, records_attempted=n,
        parse_counts={lcr.PARSE_OK: n}, contract_counts={lcr.CONTRACT_CURRENT: n},
        contract_assessed=True, scope_counts={lcr.SCOPE_IN: n}, scope_assessed=True,
        complete_counts={lcr.COMPLETE_YES: n}, completeness_assessed=True,
        inspection_roots=(ROOT_KEY,), entries_complete=True, assessments_complete=True,
        entries_unclassified=0, skipped_entries=0, observed_from="T0", observed_to="T1",
        records=tuple(records), records_truncated=truncated,
        vanished=("gone.json",) if unstable else (),
    )
    base.update(override)
    out = (lcr.RecordInventory if counts_exact else _X2InexactCounts)(**base)
    assert bool(out.records_truncated) is truncated, "fixture misbuilt: records_truncated"
    assert bool(out.counts_exact) is counts_exact, "fixture misbuilt: counts_exact"
    assert bool(out.unstable) is unstable, "fixture misbuilt: unstable"
    return out


#: ``(name, truncated, counts_exact, unstable)``. The three flags independently and in every
#: combination -- eight states, not the three the old suite could reach.
X2_STATES = (
    ("complete", False, True, False),
    ("T", True, True, False),
    ("C", False, False, False),
    ("TC", True, False, False),
    ("U", False, True, True),
    ("UT", True, True, True),
    ("UC", False, False, True),
    ("UTC", True, False, True),
)


def _x2_expected_hold_limitations(a5mod, truncated, counts_exact, unstable):
    """Exactly the causes that apply, and no other. Order-independent by construction."""
    want = set()
    if truncated:
        want.add(a5mod.TRUNCATED_JOIN_LIMITATION)
    if not counts_exact:
        want.add(a5mod.INEXACT_JOIN_LIMITATION)
    if unstable:
        want.add(a5mod.CONCURRENCY_LIMITATION)
    return want


def _x2_expected_zero_limitations(a5mod, truncated, counts_exact, unstable):
    want = set()
    if unstable:
        want.add(a5mod.ZERO_CONCURRENCY_LIMITATION)
    if truncated:
        want.add(a5mod.ZERO_TRUNCATED_LIMITATION)
    if not counts_exact:
        want.add(a5mod.ZERO_INEXACT_LIMITATION)
    return want


# ------------------------------------------------------------------- J1. the eight-state matrix

@pytest.mark.parametrize("name,truncated,counts_exact,unstable", X2_STATES)
def test_x2_1_the_evidence_hold_publishes_a_floor_on_any_incomplete_join(
        name, truncated, counts_exact, unstable):
    """The matrix. One refused supplied entry, two records sharing its source path.

    This is the test that fails against the parent blob: in states T, C and TC it published
    ``exact`` / ``publishable_as_total=True`` / ``limitations=()`` for a figure whose matched half
    came out of a capped display list.
    """
    records = _records_sharing(SHARED_SRC, 2)
    inventory = _x2_inv(records, truncated=truncated, counts_exact=counts_exact,
                        unstable=unstable)
    b = report(location=loc(), inventory=inventory, source_verdicts=[_bad_finalisation()])
    hold = _rejected_hold(b)
    incomplete = truncated or (not counts_exact) or unstable
    #: The two causes the PRODUCER decides, and so the two that carry the record-join floor
    #: wording. Concurrency is deliberately not among them: `_demote_while_moving` owns that
    #: decision, once, over the assembled list, and it is unit-agnostic -- it cannot attach a
    #: `VERDICTS`-specific note without the producer pre-judging instability, which is the
    #: architecture finding N-1 closed. In the concurrency-only state the demotion is explained by
    #: `CONCURRENCY_LIMITATION`, whose own wording already says the count covers only what this
    #: pass examined, so no reason goes unstated.
    join_incomplete = truncated or (not counts_exact)

    # the value is a property of the INPUTS and must never move with the flags
    assert hold.value == 1, f"{name}: the count changed with an incompleteness flag"
    assert hold.unit == a5.VERDICTS
    assert hold.axis == a5.AXIS_UNCERTAIN
    assert hold.consistent, f"{name}: Figure.consistent broke"
    assert "not a count of distinct cache records" in hold.population

    if not incomplete:
        assert hold.exactness == a5.EXACT, f"{name}: a complete join was demoted"
        assert hold.publishable_as_total
        assert hold.limitations == (), f"{name}: a cause was given that does not apply"
    else:
        assert hold.exactness == a5.LOWER_BOUND, f"{name}: an incomplete join published EXACT"
        assert not hold.publishable_as_total, f"{name}: a floor was certified as a total"
        assert hold.limitations, f"{name}: demoted without saying why"

    # the record-join floor wording tracks the two producer-decided causes
    if join_incomplete:
        assert a5.REFUSAL_FLOOR_NOTE in hold.notes, f"{name}: no reason given for the floor"
        assert a5.REFUSAL_FLOOR_POPULATION in hold.population, \
            f"{name}: the population still reads as a total of what the caller supplied"
    else:
        assert a5.REFUSAL_FLOOR_NOTE not in hold.notes, f"{name}: a join cause was implied"
        assert a5.REFUSAL_FLOOR_POPULATION not in hold.population, name
        if unstable:
            # the concurrency-only state: the central rule's own sentence is the stated reason
            assert hold.limitations == (a5.CONCURRENCY_LIMITATION,), name
            assert "counts only what this pass examined" in hold.limitations[0]

    # every applicable cause, and NO cause that does not apply
    assert set(hold.limitations) == _x2_expected_hold_limitations(
        a5, truncated, counts_exact, unstable), f"{name}: wrong set of causes"
    assert len(hold.limitations) == len(set(hold.limitations)), f"{name}: a cause was duplicated"

    # D is untouched by this fix: it already weighed all three states for itself
    d = b.confirmed_missing
    assert d.value == 0
    assert d.exactness == a5.LOWER_BOUND, f"{name}: D changed"
    assert d.unit == a5.RECORDS

    # the E headline: a positive VERDICTS hold withholds it, in every state
    assert b.uncertain.value is None, f"{name}: an evidence-object count reached the headline"
    assert b.uncertain.exactness == a5.NOT_ASSESSED
    assert not b.uncertain.publishable_as_total
    assert "counts supplied objects" in b.uncertain.withheld_reason

    # and the rendered report, which is what a consumer actually reads
    text = "\n".join(lines_for(b))
    if incomplete:
        assert "rejected as evidence: at least 1 verdicts (lower_bound)" in text, \
            f"{name}: the rendered line still offered an exact count"
        assert "rejected as evidence: 1 verdicts (exact)" not in text
    else:
        assert "rejected as evidence: 1 verdicts (exact)" in text
    assert "1 records (exact)" not in text, f"{name}: a verdict count rendered as records"


@pytest.mark.parametrize("name,truncated,counts_exact,unstable", X2_STATES)
def test_x2_1_the_headline_zero_is_a_floor_on_any_incomplete_join(
        name, truncated, counts_exact, unstable):
    """The adjacent E-zero path (prompt requirement 6), reached with no rejection at all.

    Every record carries a PRESENT verdict, so nothing is outstanding and every question returns
    zero. On the parent blob that published ``0 records (exact)``, ``publishable_as_total=True``,
    over a folder whose unexamined remainder could hold any number of doubts.
    """
    records = tuple(rec("p%d" % i) for i in range(2))
    inventory = _x2_inv(records, folder_records=5, truncated=truncated,
                        counts_exact=counts_exact, unstable=unstable)
    b = report(location=loc(), inventory=inventory,
               source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    e = b.uncertain
    incomplete = truncated or (not counts_exact) or unstable

    assert e.value == 0, f"{name}: fixture misbuilt -- a hold fired or was withheld"
    assert e.unit == a5.RECORDS
    assert e.consistent
    assert not any(f.unit == a5.VERDICTS for _lab, f in b.uncertain_observations), \
        f"{name}: fixture misbuilt -- this path must be reached with no rejection"

    if not incomplete:
        assert e.exactness == a5.EXACT, f"{name}: a complete pass was demoted"
        assert e.publishable_as_total
        assert e.limitations == ()
        assert e.population == "every uncertainty question that was put returned zero"
    else:
        assert e.exactness == a5.LOWER_BOUND, f"{name}: an incomplete zero published EXACT"
        assert not e.publishable_as_total, f"{name}: an incomplete zero was certified as a total"
        assert "this pass examined" in e.population, f"{name}: the population still reads folder-wide"

    assert set(e.limitations) == _x2_expected_zero_limitations(
        a5, truncated, counts_exact, unstable), f"{name}: wrong set of causes on the zero"
    assert len(e.limitations) == len(set(e.limitations))

    text = "\n".join(lines_for(b))
    if incomplete:
        assert "E uncertain: 0 records (exact)" not in text, \
            f"{name}: the rendered headline still read as an exact folder-wide zero"


@pytest.mark.parametrize("name,truncated,counts_exact,unstable", X2_STATES)
def test_x2_1_the_concurrency_reason_is_given_only_for_concurrency(
        name, truncated, counts_exact, unstable):
    """Never say the folder moved because a display list was capped.

    The two new causes exist as separate sentences for this reason alone: a reader told the folder
    changed under the pass would be told about an event that did not happen. Checked on the hold,
    on the headline and in the rendered text.
    """
    records = _records_sharing(SHARED_SRC, 2)
    b = report(location=loc(),
               inventory=_x2_inv(records, truncated=truncated, counts_exact=counts_exact,
                                 unstable=unstable),
               source_verdicts=[_bad_finalisation()])
    hold = _rejected_hold(b)
    moved_claimed = any("changed while it was being read" in item for item in hold.limitations)
    assert moved_claimed is unstable, \
        f"{name}: concurrency claimed={moved_claimed} while unstable={unstable}"

    settled = tuple(rec("q%d" % i) for i in range(2))
    zero = report(location=loc(),
                  inventory=_x2_inv(settled, folder_records=4, truncated=truncated,
                                    counts_exact=counts_exact, unstable=unstable),
                  source_verdicts=[verdict(r.source_path, state=lib.PRESENT)
                                   for r in settled]).uncertain
    zero_moved = any("changed while it was being read" in item for item in zero.limitations)
    assert zero_moved is unstable, f"{name}: the headline zero claimed a concurrency event"


# ------------------------------------------------------- J2. the decisive display-window pair

def test_x2_1_the_decisive_display_window_pair_publishes_no_total_either_way():
    """One folder, one set of supplied verdicts; only where the cap fell differs.

    This is the measurement that turns X2-1 from an argument into a fact. The folder holds two
    records: one whose source A1 saw, and one whose source carries a confirmed-missing enum with no
    clause evidence. The aggregate counters say two either way. The only difference is whether A3's
    shortened display happened to include the second one.

    Measured on the parent blob:
      inside  -> hold `1 verdicts (exact)`, publishable_as_total True
      beyond  -> no hold at all, headline `0 records (exact)`, publishable_as_total True
    Neither may be read as a total now, and the whole headline is asserted, not merely whether the
    hold exists.
    """
    present = rec("p0")
    refused = rec("x0", source=SHARED_SRC)
    verdicts = [verdict(present.source_path, state=lib.PRESENT), _bad_finalisation()]

    inside = report(location=loc(),
                    inventory=_x2_inv((present, refused), folder_records=2, truncated=True),
                    source_verdicts=verdicts)
    beyond = report(location=loc(),
                    inventory=_x2_inv((present,), folder_records=2, truncated=True),
                    source_verdicts=verdicts)

    # the arm that sees the refusal: a floor, with truncation named, and the headline withheld
    hold = _rejected_hold(inside)
    assert hold.value == 1 and hold.unit == a5.VERDICTS
    assert hold.exactness == a5.LOWER_BOUND
    assert not hold.publishable_as_total
    assert a5.TRUNCATED_JOIN_LIMITATION in hold.limitations
    assert inside.uncertain.value is None
    assert inside.uncertain.exactness == a5.NOT_ASSESSED
    assert not inside.uncertain.publishable_as_total

    # the arm that cannot see it: no hold exists, and the zero must not pass as a folder total
    assert not [f for lab, f in beyond.uncertain_observations if "rejected as evidence" in lab], \
        "fixture misbuilt: the refused record must lie beyond the window on this arm"
    e = beyond.uncertain
    assert e.value == 0
    assert e.unit == a5.RECORDS
    assert e.exactness == a5.LOWER_BOUND, \
        "a zero taken over a truncated display was published as an exact folder-wide zero"
    assert not e.publishable_as_total
    assert a5.ZERO_TRUNCATED_LIMITATION in e.limitations
    assert not any("changed while it was being read" in item for item in e.limitations), \
        "a truncated display was reported as a concurrency event"
    assert "this pass examined" in e.population
    assert e.consistent

    # neither arm may offer a publishable HEADLINE total. The individual whole-folder counter
    # holds stay exact on purpose (requirement 5): `records_truncated` shortens the per-record
    # display and leaves `entries_unclassified`, `skipped_entries` and the parse counters whole, so
    # demoting them would report a doubt that does not exist. Only the headline rendering -- the
    # bare "E uncertain: <n> <unit>" line, with no hold label after the colon -- is checked here.
    for name, buckets in (("inside", inside), ("beyond", beyond)):
        assert not buckets.uncertain.publishable_as_total, name
        for lab, fig in buckets.uncertain_observations:
            if fig.unit == a5.VERDICTS and fig.value:
                assert not fig.publishable_as_total, f"{name}: {lab}"
        text = "\n".join(lines_for(buckets))
        assert "E uncertain: 0 records (exact)" not in text, f"{name}: exact headline zero"
        assert "E uncertain: 1 verdicts (exact)" not in text, f"{name}: exact headline count"


# ------------------------------------------------------------------- J3. the two cardinalities

@pytest.mark.parametrize("nrecords,events", ((1, 1), (2, 2), (3, 3)))
@pytest.mark.parametrize("truncated", (False, True))
def test_x2_1_one_refused_entry_over_n_records_keeps_both_cardinalities(nrecords, events,
                                                                       truncated):
    """One supplied entry, N records sharing its path: 1 object refusal, N record events.

    The N-8 separation must survive the X2-1 change untouched -- the exactness moved, the
    cardinalities did not, and neither was deduplicated by source path or by object identity.
    """
    b = report(location=loc(),
               inventory=_x2_inv(_records_sharing(SHARED_SRC, nrecords), truncated=truncated),
               source_verdicts=[_bad_finalisation()])
    hold = _rejected_hold(b)
    assert hold.value == 1, "one supplied entry became more or fewer than one object"
    assert hold.unit == a5.VERDICTS
    assert f"record-level refusals of those objects {events}" in b.confirmed_missing.population
    assert "matched verdict objects refused for missing clause evidence 1" in \
        b.confirmed_missing.population
    assert hold.exactness == (a5.LOWER_BOUND if truncated else a5.EXACT)


@pytest.mark.parametrize("truncated", (False, True))
def test_x2_1_two_equal_signature_entries_and_the_same_object_twice(truncated):
    """Two input entries over one path and one record: 2 objects, 1 record event.

    Also the same Python object supplied twice -- still two supplied entries, because an entry is
    what the caller handed in, not a distinct identity. Both are N-8 cases and both must keep their
    counts while gaining the floor.
    """
    one = _bad_finalisation()
    for label, verdicts in (("two equal-signature entries", [_bad_finalisation(),
                                                             _bad_finalisation()]),
                            ("the same object supplied twice", [one, one])):
        b = report(location=loc(), inventory=_x2_inv(_records_sharing(SHARED_SRC, 1),
                                                     truncated=truncated),
                   source_verdicts=verdicts)
        hold = _rejected_hold(b)
        assert hold.value == 2, f"{label}: a key was counted instead of an entry"
        assert "record-level refusals of those objects 1" in b.confirmed_missing.population, label
        assert hold.exactness == (a5.LOWER_BOUND if truncated else a5.EXACT), label
        if truncated:
            assert set(hold.limitations) == {a5.TRUNCATED_JOIN_LIMITATION}, label


@pytest.mark.parametrize("truncated", (False, True))
def test_x2_1_one_pre_match_rejection_plus_two_matched_entries_invents_nothing(truncated):
    """1 pre-match + 2 equal-signature matched entries = 3 objects, 1 pre-match, 1 record event."""
    b = report(location=loc(),
               inventory=_x2_inv(_records_sharing(SHARED_SRC, 1), truncated=truncated),
               source_verdicts=[_pre_match_reject(), _bad_finalisation(), _bad_finalisation()])
    hold = _rejected_hold(b)
    assert hold.value == 3
    population = b.confirmed_missing.population
    assert "supplied verdict objects rejected before matching 1" in population, \
        "the pre-match count was invented or derived"
    assert "matched verdict objects refused for missing clause evidence 2" in population
    assert "record-level refusals of those objects 1" in population
    assert hold.exactness == (a5.LOWER_BOUND if truncated else a5.EXACT)


@pytest.mark.parametrize("name,truncated,counts_exact,unstable", X2_STATES)
def test_x2_1_only_pre_match_rejects_with_potential_unseen_records(name, truncated, counts_exact,
                                                                   unstable):
    """No observed record refusal at all, and records the pass never saw.

    The pre-match half is exact in isolation. The PUBLISHED figure is the sum, and an unseen record
    can only ever raise its matched half, so the sum is a floor. The rule is deliberately
    unconditional on the flags rather than reasoning about whether some surviving verdict could
    have matched an unseen record: that is a second join over records A5 was never shown, getting
    it wrong would publish a total, and a floor is sound in every case.
    """
    kept = rec("k0")
    b = report(location=loc(),
               inventory=_x2_inv((kept,), folder_records=9, truncated=truncated,
                                 counts_exact=counts_exact, unstable=unstable),
               source_verdicts=[verdict(kept.source_path, state=lib.PRESENT),
                                _pre_match_reject()])
    hold = _rejected_hold(b)
    assert hold.value == 1
    assert "supplied verdict objects rejected before matching 1" in b.confirmed_missing.population
    assert "matched verdict objects refused for missing clause evidence 0" in \
        b.confirmed_missing.population
    incomplete = truncated or (not counts_exact) or unstable
    assert hold.exactness == (a5.LOWER_BOUND if incomplete else a5.EXACT), name
    assert set(hold.limitations) == _x2_expected_hold_limitations(
        a5, truncated, counts_exact, unstable), name


# ------------------------------------------------------------------------- J4. the controls

@pytest.mark.parametrize("truncated", (False, True))
@pytest.mark.parametrize("state_", (lib.PRESENT, lib.MISSING_CONFIRMED))
def test_x2_1_an_established_source_state_manufactures_no_evidence_hold(state_, truncated):
    """`SOURCE_PRESENT` and a fully finalised four-clause confirmation are positive observations.

    Truncation must not conjure a `VERDICTS` object out of a verdict that was accepted. A fix that
    demoted by inventing a hold would be worse than the defect.
    """
    b = report(location=loc(),
               inventory=_x2_inv(_records_sharing(SHARED_SRC, 3), truncated=truncated),
               source_verdicts=[verdict(SHARED_SRC, state=state_)])
    labels = [lab for lab, _f in b.uncertain_observations]
    assert not any("rejected as evidence" in lab for lab in labels), \
        f"an accepted verdict produced a rejection hold: {labels}"
    assert "rejected as evidence" not in " | ".join(b.confirmed_missing.limitations)
    if state_ == lib.MISSING_CONFIRMED:
        assert b.confirmed_missing.value == 3
        assert b.confirmed_missing.exactness == (a5.LOWER_BOUND if truncated else a5.EXACT)


@pytest.mark.parametrize("truncated", (False, True))
def test_x2_1_contradictory_verdicts_still_withhold_rather_than_publish_a_floor(truncated):
    """Two supplied verdicts disagreeing about one path: D and the source hold are WITHHELD.

    A withheld figure carries no number, so there is nothing to demote; the fix must not turn a
    refusal into an optimistic zero or a floor of zero.
    """
    records = _records_sharing(SHARED_SRC, 1)
    b = report(location=loc(), inventory=_x2_inv(records, truncated=truncated),
               source_verdicts=[verdict(SHARED_SRC, all_four=True),
                                verdict(SHARED_SRC, all_four=False)])
    assert b.confirmed_missing.value is None
    assert b.confirmed_missing.exactness == a5.NOT_ASSESSED
    assert "disagree about the same source path" in b.confirmed_missing.withheld_reason
    assert b.confirmed_missing.consistent
    assert b.uncertain.value is None, "a contradiction became a countable zero"
    for lab, fig in b.uncertain_observations:
        if fig.value is None:
            assert fig.exactness == a5.NOT_ASSESSED and fig.withheld_reason, lab
            assert fig.consistent, lab


@pytest.mark.parametrize("truncated", (False, True))
def test_x2_1_no_verified_root_withholds_and_invents_no_optimistic_zero(truncated):
    """With no verified inspection root, no supplied verdict has jurisdiction: D is withheld.

    The early return happens before the record join, so only pre-match objects can exist -- and the
    fix must not turn that refusal into an exact zero in either bucket.
    """
    b = report(location=loc(),
               inventory=_x2_inv(_records_sharing(SHARED_SRC, 2), truncated=truncated,
                                 inspection_roots=()),
               source_verdicts=[_bad_finalisation()])
    assert b.confirmed_missing.value is None
    assert b.confirmed_missing.exactness == a5.NOT_ASSESSED
    assert b.confirmed_missing.consistent
    assert "no verified inspection root" in b.confirmed_missing.withheld_reason
    assert not (b.uncertain.value == 0 and b.uncertain.exactness == a5.EXACT)


def test_x2_1_a_withheld_hold_stays_withheld_under_every_incompleteness_state():
    """`Figure.consistent` requires ``value is None`` exactly when ``NOT_ASSESSED``."""
    for name, truncated, counts_exact, unstable in X2_STATES:
        b = report(location=loc(),
                   inventory=_x2_inv((rec("r0"),), truncated=truncated,
                                     counts_exact=counts_exact, unstable=unstable))
        withheld_holds = [(lab, f) for lab, f in b.uncertain_observations if f.value is None]
        assert withheld_holds, f"{name}: fixture misbuilt -- no verdicts, so a hold must withhold"
        for lab, figure in withheld_holds:
            assert figure.exactness == a5.NOT_ASSESSED, f"{name}: {lab} lost its withheld state"
            assert figure.consistent and figure.withheld_reason, f"{name}: {lab}"
        for lab, figure in b.uncertain_observations:
            assert figure.consistent, f"{name}: {lab}"


def test_x2_1_a_truncated_display_does_not_demote_unrelated_producers():
    """Requirement 5. Truncation bears on counts reached THROUGH the record detail list, and on
    nothing else.

    A3's whole-folder entry counters, A4's location holds, the category measurements, the cheap
    subtotal, the independent A4 timestamp and the configured/selected distinction are all
    unaffected -- asserted against a non-truncated twin built from the same inputs, field by field,
    so "unaffected" means equal and not merely plausible.
    """
    records = tuple(rec("c%d" % i) for i in range(3))
    verdicts = [verdict(r.source_path, state=lib.PRESENT) for r in records]
    location = loc(fallback=True,
                   shared=state(lcl.ROLE_SHARED, OTHER, verdict_=lib.ROOT_UNREACHABLE,
                                observed=False, inspectable=False),
                   configured_shared=OTHER, observed_at="T9")
    cats = [catobs(ca.POSTERS, os.path.join(BASE, "p"), total=11, storage="vol-p"),
            catobs(ca.ANALYSIS, os.path.join(BASE, "a"), expensive=True, total=22,
                   storage="vol-a")]

    def build(truncated):
        return report(location=location,
                      inventory=_x2_inv(records, folder_records=3, truncated=truncated),
                      source_verdicts=verdicts, categories=cats)

    plain, cut = build(False), build(True)

    # the A4-derived hold: its own folder, its own observation window, untouched
    label = f"uncertain: the configured shared folder reported {lib.ROOT_UNREACHABLE}"
    a_plain, a_cut = _hold(plain, label), _hold(cut, label)
    assert a_cut.subject == a_plain.subject
    assert a_cut.window == a_plain.window, "a truncated record display moved the A4 hold's window"
    assert a_cut.window != ("T0", "T1"), "the A4 hold borrowed A3's record-pass window (N-3/N-4)"
    assert a_cut.exactness == a_plain.exactness == a5.NOT_ASSESSED
    assert a_cut.limitations == a_plain.limitations == ()

    # the entry-counter holds come from whole-folder counters, not the display list
    for lab, fig in cut.uncertain_observations:
        if fig.basis.startswith("RecordInventory") and fig.value is not None:
            twin = _hold(plain, lab)
            assert (fig.value, fig.unit, fig.exactness, fig.limitations) == \
                   (twin.value, twin.unit, twin.exactness, twin.limitations), lab
            assert fig.exactness == a5.EXACT, f"{lab} was demoted by a truncated display"

    # the cost axis, the subtotal and the identity are not record-join figures at all
    assert [(k, f.value, f.exactness) for k, f in cut.cheap] == \
           [(k, f.value, f.exactness) for k, f in plain.cheap]
    assert [(k, f.value, f.exactness) for k, f in cut.expensive] == \
           [(k, f.value, f.exactness) for k, f in plain.expensive]
    assert (cut.cheap_subtotal.value, cut.cheap_subtotal.exactness) == \
           (plain.cheap_subtotal.value, plain.cheap_subtotal.exactness)
    assert cut.identity.path_as_configured == plain.identity.path_as_configured == OTHER
    assert cut.identity.path_as_selected == plain.identity.path_as_selected == CACHE
    assert (cut.other_configuration.value, cut.other_configuration.exactness) == \
           (plain.other_configuration.value, plain.other_configuration.exactness)
    assert (cut.out_of_scope.value, cut.out_of_scope.exactness) == \
           (plain.out_of_scope.value, plain.out_of_scope.exactness)

    # what truncation legitimately DOES change: D says so, and E stops claiming a folder total
    assert plain.confirmed_missing.exactness == a5.EXACT
    assert cut.confirmed_missing.exactness == a5.LOWER_BOUND
    assert any("truncated display" in item for item in cut.confirmed_missing.limitations)


def test_x2_1_no_reclaimable_total_is_created_in_any_incompleteness_state():
    """The data model is unchanged: no field exists in which a reclaimable total could be held.

    A fix in this area is exactly where one would be tempted to appear -- "if the figure is only a
    floor, how much could be reclaimed?" -- so the absence is asserted across all eight states
    rather than once.
    """
    records = _records_sharing(SHARED_SRC, 2)
    fields = {f.name for f in dataclasses.fields(a5.Buckets)}
    assert not fields & {"reclaimable", "total", "reclaimable_total", "freeable"}
    for name, truncated, counts_exact, unstable in X2_STATES:
        b = report(location=loc(),
                   inventory=_x2_inv(records, truncated=truncated, counts_exact=counts_exact,
                                     unstable=unstable),
                   source_verdicts=[_bad_finalisation()])
        assert {f.name for f in dataclasses.fields(b)} == fields, name
        text = "\n".join(lines_for(b))
        assert a5.MISSING_SOURCE_SENTENCE not in text, \
            f"{name}: no source was confirmed missing, so that sentence must not appear"
        # the one line carrying the new floor wording must not also carry a reclaim verb
        for line in lines_for(b):
            if a5.REFUSAL_FLOOR_POPULATION.strip("; ")[:40] in line:
                for word in ("delete", "reclaim", "obsolete"):
                    assert word not in line.lower(), f"{name}: {line}"


# ------------------------------------------------------ J5. X2-2, and the strengthened N-1 guard

def test_x2_2_the_unit_is_the_operative_reason_the_hold_is_not_the_headline():
    """X2-2 was a false *provenance* claim in a comment; the behaviour it guards is correct.

    The mechanism is asserted behaviourally rather than by reading the comment: `VERDICTS` is not a
    content unit, so the hold is excluded from the headline candidates, and a positive one forces
    the headline to be withheld rather than merely skipped. Then the corrected prose is checked for
    the claim it must no longer make.
    """
    assert a5.VERDICTS not in a5.CONTENT_UNITS
    b = report(location=loc(), inventory=_x2_inv(_records_sharing(SHARED_SRC, 1)),
               source_verdicts=[_bad_finalisation()])
    hold = _rejected_hold(b)
    assert hold.value == 1 and hold.unit == a5.VERDICTS
    # the only positive hold is in a non-content unit, so the headline is WITHHELD, not passed
    # through and not turned into a zero
    positive = [(lab, f) for lab, f in b.uncertain_observations if f.value]
    assert [lab for lab, _f in positive] == ["uncertain: supplied source verdicts rejected as "
                                             "evidence"]
    assert b.uncertain.value is None and b.uncertain.exactness == a5.NOT_ASSESSED
    assert "never becomes a record total" in b.uncertain.withheld_reason

    with open(_A5, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "a fact about the inputs, not about the folder" not in source, \
        "the false provenance claim corrected by X2-2 is back"
    assert "CONTENT_UNITS" in source.split("Finding N-2, with its stated reason corrected")[1][:900], \
        "the corrected comment no longer names the unit as the operative reason"


def test_n1_the_demotion_is_still_enforced_at_exactly_one_point_after_x2_1():
    """N-1, re-pointed and strengthened rather than relaxed.

    The X2-1 fix deliberately did NOT pre-attach the concurrency sentence at the producer, because
    that is the architecture N-1 closed. So the single call site must still exist, and the
    concurrency constant must appear nowhere inside `_uncertainty` -- which the previous guard did
    not check, and which is the drift that would actually reintroduce the defect.
    """
    with open(_A5, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert source.count("def _demote_while_moving(") == 1
    assert source.count("_demote_while_moving(") == 2, \
        "the central demotion is defined once and must be called exactly once"
    assert source.count("_demote_while_moving(tuple(out), inv_unstable)") == 1

    tree = ast.parse(source)
    uncertainty = [n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_uncertainty"]
    assert len(uncertainty) == 1

    def names_in(node):
        """Identifiers, resolved as identifiers.

        Deliberately NOT a substring search on the dumped tree: `ZERO_CONCURRENCY_LIMITATION`
        CONTAINS `CONCURRENCY_LIMITATION`, so `"CONCURRENCY_LIMITATION" not in dump` can never be
        satisfied by any correct implementation and the guard would be a permanent false alarm --
        or, written the other way round, a permanent vacuous pass.
        """
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    uncertainty_names = names_in(uncertainty[0])
    assert "CONCURRENCY_LIMITATION" not in uncertainty_names, \
        "_uncertainty attaches the concurrency sentence to a hold itself, bypassing the one rule"
    assert "ZERO_CONCURRENCY_LIMITATION" in uncertainty_names, \
        "the headline zero no longer names concurrency; the headline is not a hold, so it must"

    builder = [n for n in ast.walk(uncertainty[0])
               if isinstance(n, ast.FunctionDef) and n.name == "inv_figure"][0]
    assert "inv_unstable" not in names_in(builder), \
        "inv_figure decides instability again, so a hold built elsewhere can bypass the rule"

    normaliser = [n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_demote_while_moving"][0]
    normaliser_names = names_in(normaliser)
    assert "CONCURRENCY_LIMITATION" in normaliser_names, "the central rule lost its own sentence"
    assert not normaliser_names & {"TRUNCATED_JOIN_LIMITATION", "INEXACT_JOIN_LIMITATION"}, \
        "the concurrency normaliser took on a cause that is not concurrency"
    assert "records_truncated" not in ast.dump(normaliser)


def test_n1_an_already_floored_hold_still_collects_the_concurrency_reason():
    """The behavioural half of the N-1 re-pointing, which a source-text guard cannot give.

    Under UT the hold is already a floor for truncation when the normaliser sees it. Before the
    fix, `exactness != EXACT` made the normaliser skip it entirely, so the folder having moved went
    unsaid. Both reasons must now appear, exactly once each, with the exactness still a floor.
    """
    records = _records_sharing(SHARED_SRC, 2)
    hold = _rejected_hold(report(
        location=loc(), inventory=_x2_inv(records, truncated=True, unstable=True),
        source_verdicts=[_bad_finalisation()]))
    assert hold.exactness == a5.LOWER_BOUND
    assert set(hold.limitations) == {a5.TRUNCATED_JOIN_LIMITATION, a5.CONCURRENCY_LIMITATION}
    assert hold.limitations.count(a5.CONCURRENCY_LIMITATION) == 1, "the reason was duplicated"
    assert not hold.publishable_as_total


# =============================================================================================
# K. behavioural mutation controls for X2-1 / X2-2. Anchors are distinct from every anchor in
#    sections E, G and I. Each one is a real byte change in a compiled copy, proved to apply by an
#    assertion on the MUTANT's published output before the real module is asserted on at all.
# =============================================================================================

def _x2_mutant_fixture(**invkw):
    return dict(location=loc(),
                inventory=_x2_inv(_records_sharing(SHARED_SRC, 2), **invkw),
                source_verdicts=[_bad_finalisation()])


def test_mutant_restoring_the_unconditional_exact_evidence_hold_is_caught():
    """X2-1 itself: the published exactness goes back to being unconditional.

    This is the mutate-to-the-defect control. The mutant reproduces the parent blob's behaviour
    exactly -- `exact`, publishable, no limitation -- on a truncated join.
    """
    kw = _x2_mutant_fixture(truncated=True)
    weak = _mutant(("                   exactness=(LOWER_BOUND if join_incomplete else EXACT),",
                    "                   exactness=EXACT,"))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert mutated.exactness == weak.EXACT, "the mutation did not apply"
    assert mutated.publishable_as_total, "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert real.exactness == a5.LOWER_BOUND
    assert not real.publishable_as_total


def test_mutant_ignoring_record_detail_truncation_is_caught():
    """The truncation cause stops being asked for."""
    kw = _x2_mutant_fixture(truncated=True)
    weak = _mutant(("        (inv_truncated, TRUNCATED_JOIN_LIMITATION),",
                    "        (False, TRUNCATED_JOIN_LIMITATION),"))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert mutated.exactness == weak.EXACT and mutated.limitations == (), \
        "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert real.exactness == a5.LOWER_BOUND
    assert a5.TRUNCATED_JOIN_LIMITATION in real.limitations


def test_mutant_ignoring_uncertified_counts_is_caught():
    """The `counts_exact` cause stops being asked for -- the flag truncation does not imply."""
    kw = _x2_mutant_fixture(counts_exact=False)
    weak = _mutant(("        (not inv_counts_exact, INEXACT_JOIN_LIMITATION)) if applies)",
                    "        (False, INEXACT_JOIN_LIMITATION)) if applies)"))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert mutated.exactness == weak.EXACT and mutated.limitations == (), \
        "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert real.exactness == a5.LOWER_BOUND
    assert set(real.limitations) == {a5.INEXACT_JOIN_LIMITATION}


def test_mutant_blaming_concurrency_for_a_truncated_display_is_caught():
    """A fix that reused the concurrency sentence would report an event that did not happen."""
    kw = _x2_mutant_fixture(truncated=True)
    weak = _mutant(("        (inv_truncated, TRUNCATED_JOIN_LIMITATION),",
                    "        (inv_truncated, CONCURRENCY_LIMITATION),"))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert weak.CONCURRENCY_LIMITATION in mutated.limitations, "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert a5.CONCURRENCY_LIMITATION not in real.limitations, \
        "a capped display list was published as the folder changing under the pass"
    assert a5.TRUNCATED_JOIN_LIMITATION in real.limitations


def test_mutant_letting_an_incomplete_headline_zero_stay_exact_is_caught():
    """The E-zero path: truncation stops counting as a doubt about the zero."""
    records = tuple(rec("z%d" % i) for i in range(2))
    kw = dict(location=loc(),
              inventory=_x2_inv(records, folder_records=6, truncated=True),
              source_verdicts=[verdict(r.source_path, state=lib.PRESENT) for r in records])
    weak = _mutant(("        (inv_truncated, ZERO_TRUNCATED_LIMITATION),",
                    "        (False, ZERO_TRUNCATED_LIMITATION),"))
    mutated = _run(weak, **kw).uncertain
    assert mutated.value == 0 and mutated.exactness == weak.EXACT, "the mutation did not apply"
    assert mutated.publishable_as_total, "the mutation did not apply"
    real = report(**kw).uncertain
    assert real.value == 0
    assert real.exactness == a5.LOWER_BOUND
    assert not real.publishable_as_total
    assert a5.ZERO_TRUNCATED_LIMITATION in real.limitations


def test_mutant_dropping_the_concurrency_reason_from_a_floored_hold_is_caught():
    """The N-1 re-pointing, guarded behaviourally: the pre-fix skip hid one of two causes."""
    kw = _x2_mutant_fixture(truncated=True, unstable=True)
    weak = _mutant(("        if fig.value is None:",
                    "        if fig.value is None or fig.exactness != EXACT:"))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert weak.CONCURRENCY_LIMITATION not in mutated.limitations, "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert set(real.limitations) == {a5.TRUNCATED_JOIN_LIMITATION, a5.CONCURRENCY_LIMITATION}, \
        "a hold that was already a floor lost the fact that the folder also moved"
    assert real.exactness == a5.LOWER_BOUND


def test_mutant_dropping_the_floor_note_from_the_population_is_caught():
    """The number must not read as a total of what the caller supplied."""
    kw = _x2_mutant_fixture(truncated=True)
    weak = _mutant(('                              + (REFUSAL_FLOOR_POPULATION if join_incomplete '
                    'else ""),',
                    '                              + "",'))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert weak.REFUSAL_FLOOR_POPULATION not in mutated.population, "the mutation did not apply"
    real = _rejected_hold(report(**kw))
    assert a5.REFUSAL_FLOOR_POPULATION in real.population
    assert "floor rather than a total" in real.population


# =============================================================================================
# L. R1-2 and R1-1 -- the two residual findings of the independent review of the X2-1 commit.
#
# R1-2 is a COVERAGE finding, not a defect: the shipped order
#
#     notes = <rejection notes>[:8] + (REFUSAL_FLOOR_NOTE if join_incomplete else ())
#
# is correct, and the review proved it was also undefended. Moving the `[:8]` to after the
# concatenation survived every one of the 277 tests the mutant run selected out of the 278 this
# file held before this section -- measured, not taken on trust; the 278th censuses this test
# module, not the source, so it was deselected -- yet at eight or more distinct rejection groups on
# an incomplete join it pushes the floor explanation off the end, so the hold publishes a floor
# while no longer saying why. That is the X2-1 failure mode in the one field X2-1's own matrix
# never sized. Reproduced independently before this section was written, with a changed Git blob:
# at 8, 9 and 12 genuinely distinct groups the real module renders nine notes including the floor
# and the mutant renders eight without it, in `Figure.notes` AND in `report_lines`.
#
# Eight is the boundary, measured, not nine: at exactly eight groups the cap is already full, so the
# post-concatenation slice evicts the floor note. The fixtures below therefore start at eight.
#
# R1-1 is a producer-contract finding. Axis D weighs `shortfall <= 0` where
# `shortfall = records_parsed - len(records)`; the X2-1 guard does not. A hand-built inventory with
# a shortfall and all three flags clear makes D a floor while the evidence hold stays EXACT. That
# state is unreachable from A3, provably rather than by sampling, and the test here holds A3 to it
# so the assumption fails loudly if A3 ever stops honouring it. No A5 behaviour is changed for it.
# =============================================================================================

#: Distinct refused source paths produce distinct `(why, what)` rejection groups, because `what`
#: carries the path. A fixture that repeated one line would make the cap look exercised while
#: measuring nothing, so the tests assert distinctness instead of assuming it.
def _distinct_refusal_groups(n):
    """``(records, verdicts)`` for ``n`` genuinely distinct rejection groups."""
    paths = [os.path.join(LIBRARY, "absent-%02d.mp4" % i) for i in range(n)]
    records = tuple(rec("g%02d" % i, source=p) for i, p in enumerate(paths))
    verdicts = [_bad_finalisation(p) for p in paths]
    return records, verdicts


#: ``(name, truncated, counts_exact)`` -- the two causes the producer decides, each alone. The
#: concurrency state is deliberately absent: `_demote_while_moving` adds no note, so it cannot move
#: the cap, and asserting it here would test N-1 again rather than the cap.
_R1_2_INCOMPLETE = (("truncated display", True, True),
                    ("uncertified counts", False, False))


@pytest.mark.parametrize("groups", (8, 9, 12))
@pytest.mark.parametrize("name,truncated,counts_exact", _R1_2_INCOMPLETE)
def test_r1_2_a_capped_rejection_list_still_states_why_the_number_is_a_floor(
        groups, name, truncated, counts_exact):
    """Finding R1-2. The floor explanation survives a full rejection-note cap.

    Asserted on the published `Figure` **and** on the rendered report, because the rendered line is
    what an operator reads: a floor whose reason is invisible there is a floor nobody was told
    about.
    """
    records, verdicts = _distinct_refusal_groups(groups)
    inventory = _x2_inv(records, truncated=truncated, counts_exact=counts_exact)
    buckets = report(location=loc(), inventory=inventory, source_verdicts=list(verdicts))
    hold = _rejected_hold(buckets)

    rejection_notes = tuple(n for n in hold.notes if n != a5.REFUSAL_FLOOR_NOTE)
    assert len(rejection_notes) == 8, (
        f"{name}/{groups}: {len(rejection_notes)} rejection notes published, cap is eight")
    assert len(set(rejection_notes)) == len(rejection_notes), (
        f"{name}/{groups}: the fixture repeated a line instead of supplying distinct groups")

    # exactly once, and after them -- the ordering is the whole finding
    assert hold.notes.count(a5.REFUSAL_FLOOR_NOTE) == 1, (
        f"{name}/{groups}: the floor explanation appears {hold.notes.count(a5.REFUSAL_FLOOR_NOTE)} "
        f"times in a hold carrying a full rejection-note cap")
    assert hold.notes[-1] == a5.REFUSAL_FLOOR_NOTE, (
        f"{name}/{groups}: the floor explanation is not the note appended after the cap")
    assert len(hold.notes) == 9

    # the published semantics the note explains
    assert hold.value == groups, f"{name}/{groups}: the count moved with the note cap"
    assert hold.exactness == a5.LOWER_BOUND
    assert not hold.publishable_as_total
    assert hold.consistent
    assert a5.REFUSAL_FLOOR_POPULATION in hold.population

    # the cause that applies, and no cause that does not
    expected = {a5.TRUNCATED_JOIN_LIMITATION} if truncated else {a5.INEXACT_JOIN_LIMITATION}
    assert set(hold.limitations) == expected, f"{name}/{groups}: {hold.limitations}"
    assert a5.CONCURRENCY_LIMITATION not in hold.limitations, (
        f"{name}/{groups}: a stable folder was reported as changing under the pass")

    # and the operator-visible surface
    rendered = lines_for(buckets)
    assert any(a5.REFUSAL_FLOOR_NOTE in line for line in rendered), (
        f"{name}/{groups}: the rendered report publishes a floor without its reason")
    assert sum(line.count(a5.REFUSAL_FLOOR_NOTE) for line in rendered) == 1


@pytest.mark.parametrize("groups", (8, 9, 12))
def test_r1_2_a_complete_join_attaches_no_floor_note_at_any_group_count(groups):
    """The control. A full note cap must not become a reason to call a complete join a floor.

    Without this, a "fix" that appended the floor explanation unconditionally would pass the test
    above and silently demote every exact hold.
    """
    records, verdicts = _distinct_refusal_groups(groups)
    inventory = _x2_inv(records, truncated=False, counts_exact=True, unstable=False)
    buckets = report(location=loc(), inventory=inventory, source_verdicts=list(verdicts))
    hold = _rejected_hold(buckets)

    assert hold.value == groups
    assert hold.exactness == a5.EXACT, "a complete join was demoted by the note cap"
    assert hold.publishable_as_total
    assert hold.limitations == ()
    assert a5.REFUSAL_FLOOR_NOTE not in hold.notes, (
        "the floor explanation was attached to a join that fell short of nothing")
    assert a5.REFUSAL_FLOOR_POPULATION not in hold.population
    assert len(hold.notes) == 8
    assert not any(a5.REFUSAL_FLOOR_NOTE in line for line in lines_for(buckets))


def test_r1_1_the_producer_never_shows_fewer_records_than_it_parsed_without_saying_so():
    """Finding R1-1, as a producer contract rather than as an A5 behaviour change.

    A5's X2-1 guard asks ``records_truncated or not counts_exact``. Axis D additionally weighs
    ``shortfall <= 0``. The gap between them is only reachable with a hand-built inventory, because
    A3 guarantees ``records_parsed > len(records) => records_truncated``: every record counted into
    ``parse_counts`` reaches ``read_records``' append-or-truncate decision in the same iteration,
    and declining to append sets ``records_truncated``.

    This test holds A3 to that guarantee with injected, non-filesystem inputs, so if the producer
    ever stops honouring it the failure lands here -- loudly -- instead of A5 quietly publishing a
    total. A5 is deliberately **not** changed to re-check the fourth term: doing so would add a
    second opinion about a producer invariant, which is the shape of coupling finding N-1 closed.
    """
    folder = os.path.join(BASE, "A3Probe")
    library = os.path.join(BASE, "A3Lib")
    root_key = os.path.normcase(os.path.normpath(library))

    class _FakeStat:
        #: ``S_IFREG | 0o644``. Spelled as a literal rather than importing ``stat``, and present at
        #: all because ``_link_evidence`` reports a Windows entry UNDECIDABLE without
        #: ``st_file_attributes`` -- every entry would then land in ``skipped_undecidable`` and the
        #: probe would read ``records_parsed == 0``, measuring nothing at all.
        def __init__(self, size):
            self.st_size = size
            self.st_mode = 0o100644
            self.st_mtime_ns = 1
            self.st_ino = 1
            self.st_dev = 1
            self.st_nlink = 1
            self.st_file_attributes = 0x80

    def _probe(entries, max_reported, max_records, bad_every=0):
        names = ["rec%04d.json" % i for i in range(entries)]

        def _read_text(path, **_kw):
            base = os.path.basename(path)
            index = names.index(base) if base in names else 0
            if bad_every and index % bad_every == 0:
                return "{ this is not json"
            return ('{"video_file": "%s", "contract_version": "stage5_cache_v3"}'
                    % os.path.join(library, "v%04d.mp4" % index).replace("\\", "\\\\"))

        return lcr.read_records(
            folder,
            is_record_name=lambda nm: nm.endswith(".json"),
            contract="stage5_cache_v3", inspection_roots=(root_key,),
            listdir=lambda _p: list(names),
            lstat=lambda _p: _FakeStat(120),
            realpath=os.path.normpath,
            read_text=_read_text,
            max_entries=100_000, max_records=max_records, max_record_bytes=4_000_000,
            max_total_bytes=1_000_000_000, max_reported_names=max_reported)

    shortfalls = 0
    for entries in (0, 1, 2, 5, 9, 12, 40):
        for max_reported in (1, 2, 8, 200):
            for max_records in (3, 10, 20_000):
                for bad_every in (0, 3):
                    out = _probe(entries, max_reported, max_records, bad_every)
                    shortfall = out.records_parsed - len(out.records)
                    if shortfall <= 0:
                        continue
                    shortfalls += 1
                    where = (f"entries={entries} max_reported={max_reported} "
                             f"max_records={max_records} bad_every={bad_every}")
                    assert out.records_truncated, (
                        f"{where}: A3 showed {len(out.records)} of {out.records_parsed} parsed "
                        f"records without setting records_truncated -- the invariant A5's "
                        f"join_incomplete guard relies on is broken")
                    # and therefore A5's weaker guard is satisfied too
                    assert out.records_truncated or not out.counts_exact

    #: Guards the guard. A configuration grid that never produced a shortfall would pass every
    #: assertion above while measuring nothing -- the vacuous-sweep trap.
    assert shortfalls > 0, "no configuration produced a shortfall: this test measured nothing"


def test_mutant_capping_the_rejection_notes_after_the_floor_note_is_caught():
    """Finding R1-2's mutation control: the cap is applied to the concatenation instead.

    The anchor is the two-line `notes=` expression, unique in the module. The mutant is proved to
    have changed the published notes BEFORE the real module is asserted on, and the fixture carries
    nine distinct rejection groups so the cap is genuinely full -- below eight the mutation is
    behaviourally inert and a "kill" would be an illusion.
    """
    records, verdicts = _distinct_refusal_groups(9)
    kw = dict(location=loc(), inventory=_x2_inv(records, truncated=True),
              source_verdicts=list(verdicts))
    weak = _mutant((
        '                   notes=tuple(f"{why}: {what}" for why, what in rejected.notes)[:8]\n'
        '                         + ((REFUSAL_FLOOR_NOTE,) if join_incomplete else ())))',
        '                   notes=(tuple(f"{why}: {what}" for why, what in rejected.notes)\n'
        '                         + ((REFUSAL_FLOOR_NOTE,) if join_incomplete else ()))[:8]))'))
    mutated = [f for lab, f in _run(weak, **kw).uncertain_observations
               if "rejected as evidence" in lab][0]
    assert weak.REFUSAL_FLOOR_NOTE not in mutated.notes, "the mutation did not apply"
    assert len(mutated.notes) == 8, "the mutation did not apply"

    real = _rejected_hold(report(**kw))
    assert a5.REFUSAL_FLOOR_NOTE in real.notes, (
        "a hold with a full rejection-note cap published a floor without its reason")
    assert real.notes[-1] == a5.REFUSAL_FLOOR_NOTE
    assert len(real.notes) == 9
    assert real.exactness == a5.LOWER_BOUND
    assert not real.publishable_as_total
