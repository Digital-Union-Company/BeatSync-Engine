"""R14-A stage A3: the bounded, read-only, record-level Stage-5 cache inventory.

Three things this suite is built to prove, and they are different claims:

1. **Each of the six rungs is a separate observation.** A record with a different contract is not
   "obsolete"; a record whose source is outside the inspected library is not "missing"; a record
   nobody gave us the authority to judge is ``NOT_ASSESSED`` and not ``False``.
2. **Every authoritative decision is the real one, delegated.** Rung 5 here runs the genuine
   `video_analysis._cache_entry_is_complete`, AST-extracted from the real source and executed
   against a synthesised namespace (the precedent is `test_media_neutral_semantics.py`), because
   `video_analysis.py` cannot be imported on a bare interpreter. Rungs 1 and 4 are the real
   `analysis_cache.is_record_name` / `looks_reusable`, imported normally. A test that passed against
   a hand-written stand-in would prove nothing about drift, which is the whole risk.
3. **The bounds charge work attempted and never truncate silently.** Section I asserts the exact
   N / N+1 boundaries, that failures and non-record entries are charged too, and that a truncated
   name list makes the pass ``complete=False`` -- the invariant whose absence let A2's mutant C6
   survive.

Section K is the negative-control battery: each mutant weakens one specific safety property in a
copy of the module and asserts the fixture's answer **changes**. A test suite that cannot tell the
real module from a weakened one is not evidence, so the anchors are asserted to have applied before
the mutant is built (a replacement that silently matches nothing is a vacuous mutant -- a trap this
repository has hit before).
"""

from __future__ import annotations

import ast
import json
import os
import stat as stat_module
import sys
import types
from typing import Any, Dict, List

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from beatsync_fork import analysis_cache as ac                           # noqa: E402
from beatsync_fork import library_cache_records as lcr                   # noqa: E402
from beatsync_fork import library_inventory as lib                       # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_A3 = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_cache_records.py")


# =============================================================================================
# the real completion authority, extracted rather than imitated
# =============================================================================================

_COMPLETION_FUNCS = ("_is_count", "_stored_ai_cache_is_consistent",
                     "_deterministic_analysis_completed", "_cache_entry_is_complete")
_COMPLETION_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_DETERMINISTIC_SCORING_KEY")


def _load_completion_authority() -> Dict[str, Any]:
    """The genuine completion chain out of `video_analysis.py`, executable on bare CPython.

    Note what the extraction itself proves: this chain needs **no** filesystem, no ``ROOT_DIR`` and
    no cache directory to run. Completeness is a pure function of the payload -- which is exactly
    why A3 can assess it for free, and why no source file is opened to answer it.
    """
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    namespace: Dict[str, Any] = {
        "os": os, "json": json, "Any": Any, "Dict": Dict, "List": List,
        "__builtins__": __builtins__,
    }
    found: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _COMPLETION_FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _COMPLETION_CONSTS:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<const>", "exec"),
                         namespace)
    missing = [name for name in _COMPLETION_FUNCS if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"
    for name in _COMPLETION_CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"
    for name in _COMPLETION_FUNCS:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    return namespace


_VA_NS = _load_completion_authority()
IS_COMPLETE = _VA_NS["_cache_entry_is_complete"]
CONTRACT = _VA_NS["CACHE_CONTRACT_VERSION"]
VERSION = _VA_NS["ANALYSIS_VERSION"]
SCORING_KEY = _VA_NS["_DETERMINISTIC_SCORING_KEY"]


def test_a_the_extracted_authority_is_the_real_one_and_the_constants_are_untouched():
    """Sanity: the chain behaves as `video_analysis.py` documents, and A3 changed no constant."""
    assert CONTRACT == "stage5_cache_v3"
    assert VERSION == "auto_av_analysis_v8_llama_vulkan_batched"
    assert SCORING_KEY == "candidate_scoring_seconds"
    # A3 must not define, shadow or restate either constant anywhere.
    with open(_A3, "r", encoding="utf-8") as handle:
        a3_source = handle.read()
    for banned in ("stage5_cache_v3", "auto_av_analysis_v8", "CACHE_CONTRACT_VERSION =",
                   "ANALYSIS_VERSION ="):
        assert banned not in a3_source, f"A3 must not restate {banned!r}"


# =============================================================================================
# fixtures
# =============================================================================================

_NAME_A = "0026476a_1e5b6af7eb7140a65ae4d623.json"
_NAME_B = "11aa22bb_3c4d5e6f708192a3b4c5d6e7.json"
_NAME_C = "deadbeef_0123456789abcdef01234567.json"


def _record_name(index: int) -> str:
    """A filename of the real recognised shape: ``{8 hex}_{24 hex}.json``."""
    prefix = f"{index:08x}"
    signature = f"{index:024x}"
    name = f"{prefix}_{signature}.json"
    assert ac.is_record_name(name), name
    return name


def _complete_payload(video: str, *, candidates=None, ai=False) -> dict:
    """A payload the REAL authority accepts. Shape taken from the authority's own rules."""
    if candidates is None:
        # No candidates, with the deterministic-pass evidence present: reusable in both modes.
        return {
            "cache_contract": CONTRACT,
            "analysis_version": VERSION,
            "video_file": video,
            "candidates": [],
            "timings": {SCORING_KEY: 1.25},
        }
    payload = {
        "cache_contract": CONTRACT,
        "analysis_version": VERSION,
        "video_file": video,
        "candidates": candidates,
        "timings": {SCORING_KEY: 1.25},
    }
    if ai:
        tagged = [c for c in candidates if c.get("ai_analyzed") is True]
        payload["ai_enabled"] = True
        payload["timings"]["qwen_frame_count"] = len(tagged)
        payload["timings"]["qwen_tag_count"] = len(tagged)
    return payload


def _candidate(index: int, *, tagged: bool) -> dict:
    item = {"id": f"cand_{index:05d}", "start": float(index), "end": float(index) + 2.0}
    if tagged:
        item["ai_analyzed"] = True
    return item


def _write_record(directory, name: str, payload) -> str:
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)
    return path


def _read(directory, **kwargs):
    """Call the reader with the real rung-1 authority, and nothing else unless asked.

    A non-``str`` selection is passed through **unchanged**: ``str(None)`` is the relative path
    ``"None"``, which would quietly turn a "was a blank selection refused?" test into a test about
    a missing folder. The first draft of this helper did exactly that.
    """
    kwargs.setdefault("is_record_name", ac.is_record_name)
    target = str(directory) if isinstance(directory, (str, os.PathLike)) else directory
    return lcr.read_records(target, **kwargs)


def _read_full(directory, roots=(), **kwargs):
    """Every authority wired: contract, transport, completion in no-AI mode, and scope."""
    kwargs.setdefault("contract", CONTRACT)
    kwargs.setdefault("is_complete", IS_COMPLETE)
    kwargs.setdefault("require_ai", False)
    kwargs.setdefault("looks_reusable", ac.looks_reusable)
    kwargs.setdefault("inspection_roots", roots)
    return _read(directory, **kwargs)


# =============================================================================================
# A. directory-level states -- six distinguishable answers (required cases 1, 2, 3)
# =============================================================================================

def test_b_an_empty_but_reachable_cache_is_a_complete_observation_of_nothing(tmp_path):
    """Case 1. Zero records AND a total: a first run is not an error and not 'missing'."""
    cache = tmp_path / "cache"
    cache.mkdir()
    inv = _read(cache)
    assert inv.state == lcr.CACHE_EMPTY
    assert inv.complete is True
    assert inv.bound == lcr.BOUND_NONE
    assert inv.authoritative is True
    assert (inv.entries_listed, inv.records_recognised, inv.records_attempted) == (0, 0, 0)


def test_b_a_missing_directory_is_absent_and_is_not_the_same_answer_as_empty(tmp_path):
    """Case 2. The two must never collapse: one means 'no records yet', the other 'wrong folder'."""
    inv = _read(tmp_path / "not-there")
    assert inv.state == lcr.CACHE_ABSENT
    assert inv.complete is False
    assert inv.bound == lcr.BOUND_DIRECTORY
    assert inv.records_recognised == 0
    empty = tmp_path / "cache"
    empty.mkdir()
    assert _read(empty).state != inv.state


def test_b_a_blank_selection_is_refused_and_is_never_the_working_directory(tmp_path, monkeypatch):
    """Case 2, and the specific defect behind it: `os.path.abspath("")` is the CWD.

    This repository has already shipped an empty-folder field that resolved to the working
    directory and cut a whole install. A blank selection here must be refused outright, and the
    proof is that nothing was listed even though the CWD is full of records.
    """
    busy = tmp_path / "cwd-with-records"
    busy.mkdir()
    _write_record(busy, _NAME_A, _complete_payload("C:/x/a.mp4"))
    monkeypatch.chdir(busy)

    seen: list = []

    def spy_listdir(path):
        seen.append(path)
        return os.listdir(path)

    for blank in ("", "   ", None, 17):
        inv = _read(blank, listdir=spy_listdir)
        assert inv.state == lcr.CACHE_UNRESOLVED, blank
        assert inv.complete is False
        assert inv.records_recognised == 0
    assert seen == [], f"a blank selection listed something: {seen}"


def test_b_an_access_denied_listing_is_its_own_state_and_publishes_no_counts(tmp_path):
    """Case 3. Refused is not absent, and it is not zero records either."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def refuse(path):
        raise PermissionError(13, "Access is denied")

    inv = _read(cache, listdir=refuse)
    assert inv.state == lcr.CACHE_ACCESS_DENIED
    assert inv.complete is False
    assert inv.authoritative is False
    assert inv.records_recognised == 0
    assert "PermissionError" in inv.detail


def test_b_an_unclassifiable_listing_error_is_unreachable_rather_than_absent(tmp_path):
    """`errno` is 2 for every absent case, so classification is by TYPE, never by errno.

    Anything the model cannot positively classify is a hold -- :data:`CACHE_UNREACHABLE` -- and
    emphatically not 'the folder is empty'.
    """
    cache = tmp_path / "cache"
    cache.mkdir()

    def device_error(path):
        raise OSError(1167, "The device is not connected")

    inv = _read(cache, listdir=device_error)
    assert inv.state == lcr.CACHE_UNREACHABLE
    assert inv.complete is False
    assert inv.records_recognised == 0


# =============================================================================================
# B. rung 1 -- the real shape rule, and what is deliberately not a record (cases 4, 5, 6)
# =============================================================================================

def test_c_a_recognised_record_is_counted_read_and_classified(tmp_path):
    """Case 4: the ordinary good case, with every rung answered."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    video = str(library / "clip.mp4")
    _write_record(cache, _NAME_A, _complete_payload(video))

    root = lib.probe_root(str(library))
    inv = _read_full(cache, roots=[root])

    assert inv.state == lcr.CACHE_PRESENT
    assert inv.complete is True and inv.bound == lcr.BOUND_NONE
    assert (inv.records_recognised, inv.records_attempted, inv.records_parsed) == (1, 1, 1)
    record = inv.records[0]
    assert record.parse == lcr.PARSE_OK
    assert record.contract == lcr.CONTRACT_CURRENT
    assert record.transport == lcr.TRANSPORT_OK
    assert record.complete == lcr.COMPLETE_YES
    assert record.scope == lcr.SCOPE_IN
    assert inv.complete_records == 1


def test_c_an_uppercase_or_misshapen_filename_is_not_a_record(tmp_path):
    """Case 5. The shape rule is the real one, lower-case hex only -- not re-implemented here."""
    cache = tmp_path / "cache"
    cache.mkdir()
    rejected = (
        "0026476A_1E5B6AF7EB7140A65AE4D623.json",      # upper case
        "0026476a_1e5b6af7eb7140a65ae4d62.json",       # 23 hex
        "0026476a_1e5b6af7eb7140a65ae4d6234.json",     # 25 hex
        "0026476a1e5b6af7eb7140a65ae4d623.json",       # no separator
        "0026476a_1e5b_6af7eb7140a65ae4d623.json",     # two separators
        "0026476a_1e5b6af7eb7140a65ae4d623.txt",       # wrong suffix
        "0026476a_1e5b6af7eb7140a65ae4d623.json.bak",  # suffixed
        "zz26476a_1e5b6af7eb7140a65ae4d623.json",      # not hex
    )
    for name in rejected:
        assert ac.is_record_name(name) is False, name
        _write_record(cache, name, {"cache_contract": CONTRACT})

    inv = _read_full(cache)
    assert inv.records_recognised == 0
    assert inv.records_attempted == 0
    assert inv.other_files == len(rejected)
    assert inv.other_bytes > 0
    assert inv.parse_counts == {}
    assert inv.complete is True, "classifying non-records is still a complete observation"


def test_c_worker_artifacts_are_other_files_and_are_never_opened_or_judged(tmp_path):
    """Case 6. The retained Qwen JSON and the per-job partials are kept DELIBERATELY.

    They are counted by metadata only. Two things are asserted: they are not records, and they are
    never opened -- so no wording anywhere can call them orphaned or obsolete on the strength of a
    filename, because this pass never looked inside one.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    artifacts = (
        "qwen_batch_request_20261105_120000.json",
        "qwen_batch_response_20261105_120000.json",
        "qwen_batch_response_20261105_120000_job_00001_abc.partial.json",
    )
    for name in artifacts:
        _write_record(cache, name, {"anything": True})
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    opened: list = []

    def spy_read(path):
        opened.append(os.path.basename(path))
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    inv = _read_full(cache, read_text=spy_read)
    assert inv.records_recognised == 1
    assert inv.other_files == len(artifacts)
    assert opened == [_NAME_A], f"a worker artifact was opened: {opened}"
    # and nothing in the result mentions them as records at all
    assert [r.name for r in inv.records] == [_NAME_A]


# =============================================================================================
# C. rung 2 -- the bounded read (cases 7, 8)
# =============================================================================================

def test_d_malformed_json_is_reported_explicitly_and_not_silently_discarded(tmp_path):
    """Case 7."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, "{not json at all")
    inv = _read_full(cache)
    assert inv.parse_counts.get(lcr.PARSE_MALFORMED) == 1
    assert inv.records_attempted == 1
    assert inv.records_parsed == 0
    assert _NAME_A in inv.malformed
    record = inv.records[0]
    assert record.parse == lcr.PARSE_MALFORMED
    # No rung beyond 2 may claim an answer about a payload that does not exist.
    assert record.contract == lcr.NOT_ASSESSED
    assert record.complete == lcr.NOT_ASSESSED
    assert record.scope == lcr.NOT_ASSESSED


def test_d_valid_json_that_is_not_an_object_is_its_own_status(tmp_path):
    """Case 8. An array parses perfectly and is still not a record -- two different facts."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, [1, 2, 3])
    _write_record(cache, _NAME_B, "42")
    inv = _read_full(cache)
    assert inv.parse_counts.get(lcr.PARSE_NOT_OBJECT) == 2
    assert inv.parse_counts.get(lcr.PARSE_MALFORMED) is None
    assert all(r.parse == lcr.PARSE_NOT_OBJECT for r in inv.records)
    assert all(r.complete == lcr.NOT_ASSESSED for r in inv.records)


def test_d_an_unreadable_record_is_reported_rather_than_dropped(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def refuse(path):
        raise PermissionError(13, "Access is denied")

    inv = _read_full(cache, read_text=refuse)
    assert inv.parse_counts.get(lcr.PARSE_UNREADABLE) == 1
    assert inv.records_attempted == 1
    assert _NAME_A in inv.unreadable


# =============================================================================================
# D. rung 3 -- contract generation (case 9)
# =============================================================================================

def test_e_a_different_contract_is_another_generation_and_never_called_obsolete(tmp_path):
    """Case 9. Three distinguishable answers, and no 'obsolete' among them."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    older = _complete_payload("C:/x/b.mp4")
    older["cache_contract"] = "stage5_cache_v2"
    _write_record(cache, _NAME_B, older)
    absent = _complete_payload("C:/x/c.mp4")
    del absent["cache_contract"]
    _write_record(cache, _NAME_C, absent)

    inv = _read_full(cache)
    assert inv.contract_assessed is True
    assert inv.contract_counts == {lcr.CONTRACT_CURRENT: 1, lcr.CONTRACT_OTHER: 1,
                                   lcr.CONTRACT_ABSENT: 1}
    by_name = {r.name: r for r in inv.records}
    assert by_name[_NAME_B].recorded_contract == "stage5_cache_v2"
    # The vocabulary itself must not contain a disposal verdict.
    for value in (lcr.CONTRACT_CURRENT, lcr.CONTRACT_OTHER, lcr.CONTRACT_ABSENT):
        for banned in ("obsolete", "dead", "stale", "delete", "reclaim"):
            assert banned not in value


def test_e_without_a_contract_string_rung_three_is_not_assessed(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    inv = _read(cache)
    assert inv.contract_assessed is False
    assert inv.contract_counts == {}
    assert inv.records[0].contract == lcr.NOT_ASSESSED
    assert inv.records[0].recorded_contract == CONTRACT, \
        "the recorded value is still reported; only the VERDICT is withheld"


# =============================================================================================
# E. rung 5 -- the one completion authority, delegated (cases 10-15)
# =============================================================================================

def test_f_the_completion_verdict_comes_from_the_injected_authority_and_nowhere_else(tmp_path):
    """The anti-drift proof: A3 contains no completion rule, so a stub authority wins outright.

    An absurd authority that accepts everything -- including a payload the real rule rejects -- must
    produce COMPLETE for every record. If A3 held any local opinion, this could not happen.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    rubbish = {"cache_contract": "nonsense", "candidates": "not even a list"}
    _write_record(cache, _NAME_A, rubbish)
    assert IS_COMPLETE(rubbish, False) is False, "the real rule rejects this payload"

    calls: list = []

    def always_yes(data, require_ai):
        calls.append((dict(data), require_ai))
        return True

    inv = _read(cache, is_complete=always_yes, require_ai=False)
    assert inv.records[0].complete == lcr.COMPLETE_YES
    assert inv.complete_records == 1
    assert len(calls) == 1 and calls[0][1] is False
    assert calls[0][0] == rubbish, "the authority is handed the parsed payload verbatim"


@pytest.mark.parametrize("require_ai", [False, True])
def test_f_both_explicit_modes_are_answered_and_the_mode_is_named(tmp_path, require_ai):
    """Case 14. The report must say which question it answered."""
    cache = tmp_path / "cache"
    cache.mkdir()
    candidates = [_candidate(0, tagged=True), _candidate(1, tagged=False)]
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4", candidates=candidates, ai=True))

    inv = _read(cache, is_complete=IS_COMPLETE, require_ai=require_ai)
    assert inv.completeness_assessed is True
    assert inv.analysis_mode == ("require_ai" if require_ai else "no_ai")
    assert inv.complete_records == 1


def test_f_a_deferred_record_is_incomplete_in_both_modes(tmp_path):
    """Case 10. `ai_deferred` is never reusable, whatever else the payload says."""
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = _complete_payload("C:/x/a.mp4")
    payload["ai_deferred"] = True
    _write_record(cache, _NAME_A, payload)
    for require_ai in (False, True):
        inv = _read(cache, is_complete=IS_COMPLETE, require_ai=require_ai)
        assert inv.records[0].complete == lcr.COMPLETE_NO, require_ai
        assert inv.complete_records == 0


def test_f_an_incorrect_analysis_version_is_incomplete(tmp_path):
    """Case 11."""
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = _complete_payload("C:/x/a.mp4")
    payload["analysis_version"] = "auto_av_analysis_v7_whatever"
    _write_record(cache, _NAME_A, payload)
    inv = _read_full(cache)
    assert inv.records[0].complete == lcr.COMPLETE_NO
    # ...and yet its contract is CURRENT: the two rungs genuinely differ.
    assert inv.records[0].contract == lcr.CONTRACT_CURRENT


def test_f_empty_candidates_need_real_deterministic_evidence(tmp_path):
    """Case 12. The exact distinction the authority exists to draw.

    An empty candidate list is produced BOTH by a finished analysis that found no usable moments
    and by an OpenCV open failure. Only the first carries ``candidate_scoring_seconds``.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    with_evidence = _complete_payload("C:/x/a.mp4")
    assert with_evidence["candidates"] == [] and SCORING_KEY in with_evidence["timings"]
    _write_record(cache, _NAME_A, with_evidence)

    without = _complete_payload("C:/x/b.mp4")
    without["timings"] = {}
    _write_record(cache, _NAME_B, without)

    inv = _read(cache, is_complete=IS_COMPLETE, require_ai=False)
    verdicts = {r.name: r.complete for r in inv.records}
    assert verdicts[_NAME_A] == lcr.COMPLETE_YES
    assert verdicts[_NAME_B] == lcr.COMPLETE_NO
    assert inv.complete_records == 1


def test_f_inconsistent_stored_ai_metadata_is_incomplete_under_require_ai(tmp_path):
    """Case 13. `ai_enabled` alone is not trusted; the stored counts must agree."""
    cache = tmp_path / "cache"
    cache.mkdir()
    candidates = [_candidate(0, tagged=True), _candidate(1, tagged=False)]
    payload = _complete_payload("C:/x/a.mp4", candidates=candidates, ai=True)
    payload["timings"]["qwen_tag_count"] = 7        # claims 7 tagged; only 1 candidate says so
    payload["timings"]["qwen_frame_count"] = 7
    _write_record(cache, _NAME_A, payload)

    strict = _read(cache, is_complete=IS_COMPLETE, require_ai=True)
    assert strict.records[0].complete == lcr.COMPLETE_NO
    relaxed = _read(cache, is_complete=IS_COMPLETE, require_ai=False)
    assert relaxed.records[0].complete == lcr.COMPLETE_YES, \
        "the inconsistency is an AI-mode claim; no_ai does not depend on it"


def test_g_with_no_authority_or_no_mode_completeness_is_not_assessed_and_is_not_zero(tmp_path):
    """Case 15, and the figure that must never be published for an unestablished mode.

    Three ways to be unable to answer, all of which must look alike and none of which may read as
    'no records are complete'.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    for kwargs in ({},
                   {"is_complete": IS_COMPLETE},              # authority, but no mode
                   {"require_ai": False},                     # mode, but no authority
                   {"is_complete": IS_COMPLETE, "require_ai": None}):
        inv = _read(cache, **kwargs)
        assert inv.completeness_assessed is False, kwargs
        assert inv.complete_records is None, "None, never 0 -- that is the whole point"
        assert inv.complete_counts == {}
        assert inv.records[0].complete == lcr.NOT_ASSESSED
        assert inv.analysis_mode == ""


def test_g_an_authority_that_raises_yields_no_verdict_rather_than_incomplete(tmp_path):
    """An exception is the ABSENCE of a decision; recording it as 'incomplete' would invent one."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def explode(data, require_ai):
        raise RuntimeError("authority unavailable")

    inv = _read(cache, is_complete=explode, require_ai=False)
    assert inv.records[0].complete == lcr.NOT_ASSESSED
    assert inv.complete_counts == {}
    assert inv.complete_records == 0 or inv.complete_records is None
    assert "RuntimeError" in inv.records[0].detail


# =============================================================================================
# F. rung 6 -- source scope, by A1 containment (cases 16-19)
# =============================================================================================

def test_h_a_source_inside_a_verified_root_is_in_scope_and_names_the_root(tmp_path):
    """Case 16."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    (library / "sets").mkdir(parents=True)
    video = str(library / "sets" / "clip.mp4")
    _write_record(cache, _NAME_A, _complete_payload(video))

    root = lib.probe_root(str(library))
    assert root.verdict == lib.PRESENT
    inv = _read_full(cache, roots=[root])
    assert inv.scope_assessed is True
    assert inv.scope_counts == {lcr.SCOPE_IN: 1}
    assert inv.records[0].scope_root == root.root_key


def test_h_a_source_outside_every_root_is_out_of_scope_and_is_not_missing(tmp_path):
    """Case 17. The correctness requirement for a shared cache, not a conservatism.

    A record whose source is invisible from this library is very probably in active use by another
    installation. The verdict is about this inspection's REACH.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_record(cache, _NAME_A, _complete_payload(str(elsewhere / "other.mp4")))

    inv = _read_full(cache, roots=[lib.probe_root(str(library))])
    assert inv.scope_counts == {lcr.SCOPE_OUT: 1}
    record = inv.records[0]
    assert record.scope == lcr.SCOPE_OUT
    assert record.scope_root == ""
    # It is still a perfectly good, complete, current record.
    assert record.complete == lcr.COMPLETE_YES
    assert record.transport == lcr.TRANSPORT_OK
    # And the vocabulary offers no way to call it missing or disposable.
    assert lcr.SCOPE_OUT == "scope_out"
    for banned in ("missing", "obsolete", "delete", "orphan"):
        assert banned not in lcr.SCOPE_OUT


def test_h_containment_is_commonpath_not_a_string_prefix(tmp_path):
    """The classic failure: ``library-evil`` is not inside ``library``."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    evil = tmp_path / "library-evil"
    evil.mkdir()
    _write_record(cache, _NAME_A, _complete_payload(str(evil / "clip.mp4")))

    inv = _read_full(cache, roots=[lib.probe_root(str(library))])
    assert inv.scope_counts == {lcr.SCOPE_OUT: 1}


def test_h_an_unresolvable_or_absent_source_path_is_unknown_not_a_guess(tmp_path):
    """Case 18. A hold, in both of its forms."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    no_path = _complete_payload("C:/x/a.mp4")
    del no_path["video_file"]
    _write_record(cache, _NAME_A, no_path)
    blank = _complete_payload("   ")
    _write_record(cache, _NAME_B, blank)
    _write_record(cache, _NAME_C, _complete_payload("C:/x/c.mp4"))

    def hostile_realpath(path):
        if str(path).replace("\\", "/").endswith("/x/c.mp4"):
            raise OSError(1234, "cannot resolve")
        return os.path.realpath(path)

    inv = _read_full(cache, roots=[lib.probe_root(str(library))], realpath=hostile_realpath)
    assert inv.scope_counts.get(lcr.SCOPE_UNKNOWN) == 3
    assert lcr.SCOPE_OUT not in inv.scope_counts


def test_h_an_unreachable_source_resolves_lexically_and_is_never_stated_or_opened(tmp_path):
    """Case 18, the unreachable-drive form -- and the proof A3 does not probe sources.

    A path on an absent drive letter is a containment question, answered without touching the
    drive. Any ``lstat`` or ``open`` of the recorded source would be out of scope for A3.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    absent = next((f"{letter}:" for letter in "ZYXWVU"
                   if not os.path.exists(f"{letter}:\\")), None) or "Z:"
    _write_record(cache, _NAME_A, _complete_payload(f"{absent}\\lib\\clip.mp4"))

    touched: list = []
    real_lstat = os.lstat

    def spy_lstat(path):
        touched.append(str(path))
        return real_lstat(path)

    inv = _read_full(cache, roots=[lib.probe_root(str(library))], lstat=spy_lstat)
    assert inv.scope_counts == {lcr.SCOPE_OUT: 1}
    assert not any(absent.lower() in item.lower() for item in touched), \
        f"the recorded source's storage was probed: {touched}"


def test_h_with_no_roots_supplied_scope_is_not_assessed_rather_than_out_of_scope(tmp_path):
    """Case 19. 'Outside the library' is not a fact when nobody named a library."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/anywhere/clip.mp4"))
    missing_path = _complete_payload("x")
    del missing_path["video_file"]
    _write_record(cache, _NAME_B, missing_path)

    for roots in ((), None, []):
        inv = _read_full(cache, roots=roots)
        assert inv.scope_assessed is False, roots
        assert inv.scope_counts == {}
        assert {r.scope for r in inv.records} == {lcr.NOT_ASSESSED}
        assert inv.inspection_roots == ()


def test_h_a_root_with_no_resolvable_identity_does_not_participate(tmp_path):
    """A root that cannot contain anything provable must not manufacture a scope verdict."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/anywhere/clip.mp4"))
    hollow = lib.RootState(typed="E:\\nothing", real="", verdict=lib.ROOT_UNREACHABLE)
    assert hollow.root_key == ""
    inv = _read_full(cache, roots=[hollow])
    assert inv.scope_assessed is False
    assert inv.records[0].scope == lcr.NOT_ASSESSED


def test_h_scope_uses_the_resolved_root_identity_not_the_typed_one(tmp_path):
    """A1's F-1 lesson: a root typed as a link must be judged on the storage it resolves to."""
    cache = tmp_path / "cache"
    cache.mkdir()
    actual = tmp_path / "actual-library"
    actual.mkdir()
    video = str(actual / "clip.mp4")
    _write_record(cache, _NAME_A, _complete_payload(video))

    typed = str(tmp_path / "typed-link")
    state = lib.RootState(typed=typed, real=str(actual), verdict=lib.PRESENT,
                          anchor_reachable=True, exists=True, listed=True)
    inv = _read_full(cache, roots=[state])
    assert inv.scope_counts == {lcr.SCOPE_IN: 1}
    assert inv.inspection_roots == (state.root_key,)


# =============================================================================================
# G. containment of the CACHE folder itself (cases 20, 21)
# =============================================================================================

def _can_symlink(tmp_path) -> bool:
    try:
        target = tmp_path / "_sl_target"
        target.write_bytes(b"x")
        link = tmp_path / "_sl_link"
        os.symlink(str(target), str(link))
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        for leftover in ("_sl_link", "_sl_target"):
            path = tmp_path / leftover
            if os.path.lexists(str(path)):
                try:
                    os.remove(str(path))
                except OSError:
                    pass
    return True


def test_i_a_file_level_alias_in_the_cache_folder_is_refused_unread(tmp_path):
    """Case 20. A record-shaped symlink pointing outside the folder must not be read.

    Driven through the injected ``lstat`` so the property is asserted on every platform, and then
    again against a real symlink where the OS allows one.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _write_record(outside, "payload.json", _complete_payload("C:/x/secret.mp4"))
    alias_name = _record_name(1)
    alias_path = os.path.join(str(cache), alias_name)
    _write_record(cache, alias_name, _complete_payload("C:/x/a.mp4"))

    real_lstat = os.lstat

    class _Alias:
        """An lstat result that reports the reparse attribute, as a junction/symlink would."""

        def __init__(self, info):
            self.st_size = info.st_size
            self.st_mode = info.st_mode
            self.st_file_attributes = (getattr(info, "st_file_attributes", 0)
                                       | stat_module.FILE_ATTRIBUTE_REPARSE_POINT)

    def aliasing_lstat(path):
        info = real_lstat(path)
        if os.path.normcase(str(path)) == os.path.normcase(alias_path):
            return _Alias(info)
        return info

    opened: list = []

    def spy_read(path):
        opened.append(path)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    inv = _read_full(cache, lstat=aliasing_lstat, read_text=spy_read)
    assert inv.skipped_aliases == (alias_name,)
    assert inv.records_recognised == 0, "an alias is not counted as a record"
    assert inv.records_attempted == 0
    assert opened == [], f"an alias was read: {opened}"
    assert inv.skipped_entries == 1
    assert secret  # the file outside the folder exists and was never reached

    if _can_symlink(tmp_path):
        real_cache = tmp_path / "cache2"
        real_cache.mkdir()
        os.symlink(secret, os.path.join(str(real_cache), _record_name(2)))
        live = _read_full(real_cache)
        assert live.records_attempted == 0
        assert len(live.skipped_aliases) == 1


def test_i_undecidable_link_metadata_fails_closed(tmp_path):
    """Unreadable attributes are not evidence of an ordinary file. Refuse, do not read."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def blind_lstat(path):
        raise OSError(1, "attributes unavailable")

    opened: list = []
    inv = _read_full(cache, lstat=blind_lstat,
                     read_text=lambda p: opened.append(p) or "{}")
    assert inv.skipped_undecidable == (_NAME_A,)
    assert inv.records_attempted == 0
    assert opened == []
    assert inv.entries_examined == 1, "the work of classifying it is still charged"


def test_i_an_entry_resolving_outside_the_selected_folder_is_refused(tmp_path):
    """Belt and braces: even with innocent link metadata, containment is re-checked."""
    cache = tmp_path / "cache"
    cache.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def escaping_realpath(path):
        if os.path.basename(str(path)) == _NAME_A:
            return str(outside / _NAME_A)
        return os.path.realpath(path)

    opened: list = []
    inv = _read_full(cache, realpath=escaping_realpath,
                     read_text=lambda p: opened.append(p) or "{}")
    assert inv.skipped_escapes == (_NAME_A,)
    assert inv.records_attempted == 0
    assert opened == []


def test_i_an_explicitly_selected_junction_root_is_inspected_normally(tmp_path):
    """Case 21. The selected root MAY be an alias; its own entries are still inside it.

    The containment reference is the folder's RESOLVED path, so a cache selected through a junction
    reads its records normally -- refusing it would make the feature useless on a machine where the
    shared cache is reached through a link.
    """
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    _write_record(real_cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    link = tmp_path / "cache-link"

    made = False
    if os.name == "nt":
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real_cache)],
                              capture_output=True, text=True).returncode == 0
    if not made and _can_symlink(tmp_path):
        os.symlink(str(real_cache), str(link), target_is_directory=True)
        made = True
    if not made:
        pytest.skip("no directory-link primitive available in this environment")

    assert lib.is_reparse_point(str(link)) or os.path.islink(str(link))
    inv = _read_full(link)
    assert inv.state == lcr.CACHE_PRESENT
    assert inv.records_attempted == 1
    assert inv.records_parsed == 1
    assert inv.skipped_aliases == () and inv.skipped_escapes == ()
    assert inv.complete is True
    assert os.path.normcase(inv.resolved) == os.path.normcase(os.path.realpath(str(real_cache)))


def test_i_a_cross_volume_junction_cache_root_is_inspected_normally(tmp_path):
    """Case 21 on a REAL cross-volume junction -- the realistic shared-cache arrangement.

    Opt-in through the same ``BEATSYNC_TEST_XVOL_DIR`` convention A1's suite already uses, because
    a second volume cannot be synthesised. This is the shape that matters in production: the shared
    cache lives on one disk and is reached through a link from another, so refusing a resolved
    junction root would make the feature useless exactly where it is needed.
    """
    other_volume = os.environ.get("BEATSYNC_TEST_XVOL_DIR", "").strip()
    if not other_volume or not os.path.isdir(other_volume):
        pytest.skip("set BEATSYNC_TEST_XVOL_DIR to a writable directory on a DIFFERENT volume "
                    "to exercise a real cross-volume junction cache root")
    if os.name != "nt":
        pytest.skip("junctions are a Windows primitive; the product is Windows-only")

    import subprocess
    # Backslashes, deliberately: `cmd` parses a forward slash as the start of a switch, so
    # `E:/PROJECT_WORKSPACES/...` reaches mklink as the invalid switch `/PROJECT_WORKSPACES`. An
    # env var is just as likely to be given with forward slashes as with backslashes.
    real_cache = os.path.normpath(os.path.join(other_volume, f"a3-xvol-cache-{os.getpid()}"))
    os.makedirs(real_cache, exist_ok=False)
    link = os.path.normpath(str(tmp_path / "cache-link"))
    try:
        _write_record(real_cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, real_cache],
                              capture_output=True, text=True)
        if made.returncode != 0:
            pytest.skip(f"mklink refused: {made.stdout} {made.stderr}")

        assert lib.is_reparse_point(link)
        assert os.path.splitdrive(os.path.realpath(link))[0].lower() \
            != os.path.splitdrive(str(tmp_path))[0].lower(), "fixture is not cross-volume"

        inv = _read_full(link)
        assert inv.state == lcr.CACHE_PRESENT
        assert inv.records_attempted == 1 and inv.records_parsed == 1
        assert inv.skipped_aliases == () and inv.skipped_escapes == ()
        assert inv.complete is True
        assert inv.records[0].complete == lcr.COMPLETE_YES
    finally:
        if os.path.isdir(link):
            try:
                os.rmdir(link)                 # removes the junction, never its target
            except OSError:
                pass
        for name in os.listdir(real_cache):
            os.remove(os.path.join(real_cache, name))
        os.rmdir(real_cache)


def test_i_a_subdirectory_is_counted_and_never_entered(tmp_path):
    """A Stage-5 cache is flat. There is no recursion in this module at all."""
    cache = tmp_path / "cache"
    (cache / "nested").mkdir(parents=True)
    _write_record(cache / "nested", _NAME_B, _complete_payload("C:/x/b.mp4"))
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    listed: list = []

    def spy_listdir(path):
        listed.append(os.path.normcase(str(path)))
        return os.listdir(path)

    inv = _read_full(cache, listdir=spy_listdir)
    assert listed == [os.path.normcase(str(cache))], f"descended: {listed}"
    assert inv.records_attempted == 1
    assert [r.name for r in inv.records] == [_NAME_A]


# =============================================================================================
# H. concurrency -- a shared cache moves underneath the pass (cases 22, 23)
# =============================================================================================

def test_j_a_record_that_disappears_during_inspection_is_instability_not_a_bad_record(tmp_path):
    """Case 22. Another installation legitimately writes into a shared folder."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    _write_record(cache, _NAME_B, _complete_payload("C:/x/b.mp4"))

    def vanishing_read(path):
        if os.path.basename(path) == _NAME_A:
            raise FileNotFoundError(2, "No such file or directory")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    inv = _read_full(cache, read_text=vanishing_read)
    assert inv.parse_counts.get(lcr.PARSE_VANISHED) == 1
    assert _NAME_A in inv.vanished
    assert inv.unstable is True
    assert inv.records_attempted == 2, "the attempt is charged even though it vanished"
    assert inv.parse_counts.get(lcr.PARSE_MALFORMED) is None
    gone = {r.name: r for r in inv.records}[_NAME_A]
    assert gone.complete == lcr.NOT_ASSESSED, "no verdict is invented for bytes we never saw"


def test_j_a_record_that_changes_between_the_parse_and_the_transport_check(tmp_path):
    """Case 23. The delegated helper re-reads the PATH, so it may see different bytes.

    That is reported as TRANSPORT_UNAVAILABLE -- an observation about the folder -- and explicitly
    not as a refusal of the record, which would be a verdict about content this pass never read.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    original = os.lstat(path).st_size

    parsed = {"done": False}
    real_lstat = os.lstat

    class _Grown:
        def __init__(self, info):
            self.st_size = info.st_size + 4096
            self.st_mode = info.st_mode
            self.st_file_attributes = getattr(info, "st_file_attributes", 0)

    def shifting_lstat(p):
        """Report a larger file only AFTER this pass has read it -- the exact window at issue.

        Keyed on the parse having happened rather than on a call count, because the call count
        depends on how many times the reader legitimately stats an entry (link evidence, then
        size), and a count-based spy silently stops modelling the case when that changes.
        """
        info = real_lstat(p)
        if parsed["done"] and os.path.basename(str(p)) == _NAME_A:
            return _Grown(info)
        return info

    def read_then_change(p):
        with open(p, "r", encoding="utf-8") as handle:
            content = handle.read()
        parsed["done"] = True
        return content

    inv = _read_full(cache, lstat=shifting_lstat, read_text=read_then_change)
    assert inv.records[0].transport == lcr.TRANSPORT_UNAVAILABLE
    assert _NAME_A in inv.changed
    assert inv.unstable is True
    assert inv.transport_counts.get(lcr.TRANSPORT_REFUSED) is None
    # The parse-based rungs still hold: they were answered from bytes we did read.
    assert inv.records[0].parse == lcr.PARSE_OK
    assert inv.records[0].complete == lcr.COMPLETE_YES
    assert original > 0


def test_j_a_genuine_transport_refusal_is_reported_as_a_refusal_with_its_own_reason(tmp_path):
    """The other side of the same coin -- and the reason is the HELPER's, carried verbatim."""
    cache = tmp_path / "cache"
    cache.mkdir()
    deferred = _complete_payload("C:/x/a.mp4")
    deferred["ai_deferred"] = True
    _write_record(cache, _NAME_A, deferred)

    expected_ok, expected_reason = ac.looks_reusable(os.path.join(str(cache), _NAME_A), CONTRACT)
    assert expected_ok is False

    inv = _read_full(cache)
    record = inv.records[0]
    assert record.transport == lcr.TRANSPORT_REFUSED
    assert record.detail == expected_reason
    assert inv.changed == (), "a stable file must not be reported as unstable"


def test_j_the_transport_rung_is_delegated_and_is_charged_to_the_byte_budget(tmp_path):
    """The helper's own read is real work and is accounted for, not treated as free."""
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.lstat(path).st_size

    without = _read(cache, contract=CONTRACT)
    assert without.transport_assessed is False
    assert without.bytes_charged == size

    calls: list = []

    def spy_transport(p, contract):
        calls.append((p, contract))
        return ac.looks_reusable(p, contract)

    with_transport = _read(cache, contract=CONTRACT, looks_reusable=spy_transport)
    assert with_transport.transport_assessed is True
    assert [os.path.basename(p) for p, _ in calls] == [_NAME_A]
    assert calls[0][1] == CONTRACT, "the contract is passed through, never restated"
    assert with_transport.bytes_charged == 2 * size, "the delegated read is charged"


def test_j_the_transport_rung_needs_a_contract_to_delegate(tmp_path):
    """`looks_reusable` takes a contract; without one there is nothing to delegate."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    inv = _read(cache, looks_reusable=ac.looks_reusable)
    assert inv.transport_assessed is False
    assert inv.records[0].transport == lcr.NOT_ASSESSED


# =============================================================================================
# I. bounds -- charged on work attempted, never silently truncating (cases 24, 25, 26)
# =============================================================================================

def test_k_the_record_ceiling_is_exact_at_n_and_at_n_plus_one(tmp_path):
    """Case 24. N is a complete pass; N+1 is incomplete and says which limit it hit."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(5):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    exact = _read_full(cache, max_records=5)
    assert exact.records_attempted == 5
    assert exact.complete is True
    assert exact.bound == lcr.BOUND_NONE
    assert exact.authoritative is True

    one_too_many = _read_full(cache, max_records=4)
    assert one_too_many.records_attempted == 4
    assert one_too_many.complete is False
    assert one_too_many.bound == lcr.BOUND_RECORDS
    assert one_too_many.authoritative is False
    assert one_too_many.complete_records is not None
    assert one_too_many.complete_records <= 4


def test_k_the_entry_ceiling_charges_every_entry_classified_including_non_records(tmp_path):
    """Case 25. A non-record costs what a record costs: the bound is on WORK, not on results."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(30):
        _write_record(cache, f"qwen_batch_request_{index:05d}.json", {"x": index})
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    inv = _read_full(cache, max_entries=10)
    assert inv.entries_examined == 10
    assert inv.complete is False
    assert inv.bound == lcr.BOUND_ENTRIES
    assert inv.entries_listed == 31, "the listing size is still reported honestly"
    assert inv.other_files <= 10


def test_k_the_byte_budget_charges_failed_and_refused_reads_too(tmp_path):
    """Case 25 again, on the byte budget: a malformed file costs its bytes like any other."""
    cache = tmp_path / "cache"
    cache.mkdir()
    good = _write_record(cache, _record_name(1), _complete_payload("C:/x/a.mp4"))
    bad = _write_record(cache, _record_name(2), "{broken" + " " * 200)
    sizes = {os.path.basename(good): os.lstat(good).st_size,
             os.path.basename(bad): os.lstat(bad).st_size}

    inv = _read(cache, contract=CONTRACT)
    assert inv.parse_counts.get(lcr.PARSE_MALFORMED) == 1
    assert inv.bytes_charged == sum(sizes.values()), "the malformed file's bytes were still read"

    budget = _read(cache, contract=CONTRACT, max_total_bytes=sizes[os.path.basename(good)])
    assert budget.complete is False
    assert budget.bound == lcr.BOUND_TOTAL_BYTES
    assert budget.records_attempted == 1

    # A read that FAILS is charged too: it is charged before the attempt, so a folder of
    # unreadable files cannot consume unlimited I/O while reporting a zero spend.
    def refuse(path):
        raise PermissionError(13, "Access is denied")

    denied = _read(cache, contract=CONTRACT, read_text=refuse)
    assert denied.parse_counts.get(lcr.PARSE_UNREADABLE) == 2
    assert denied.bytes_charged == sum(sizes.values()), \
        "an attempted-but-failed read must still be charged"

    # ...and a vanished file likewise, for the same reason.
    def vanish(path):
        raise FileNotFoundError(2, "No such file or directory")

    gone = _read(cache, contract=CONTRACT, read_text=vanish)
    assert gone.parse_counts.get(lcr.PARSE_VANISHED) == 2
    assert gone.bytes_charged == sum(sizes.values())


def test_k_an_oversize_record_is_refused_unread_rather_than_spending_the_budget(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, {"cache_contract": CONTRACT, "pad": "y" * 5000})

    opened: list = []
    inv = _read(cache, contract=CONTRACT, max_record_bytes=1000,
                read_text=lambda p: opened.append(p) or "{}")
    assert inv.parse_counts.get(lcr.PARSE_TOO_LARGE) == 1
    assert inv.records_attempted == 1, "the attempt is charged"
    assert inv.bytes_charged == 0, "but its bytes were not read"
    assert opened == []
    assert _NAME_A in inv.oversize
    assert inv.complete is True, "a refusal is a classification, not a truncation"


def test_k_truncating_the_reported_names_makes_the_pass_incomplete(tmp_path):
    """The invariant whose absence let A2's mutant C6 survive. Asserted directly, both ways.

    Reachable in ordinary use: a folder with more than ``max_reported_names`` malformed records can
    no longer show its work in full, so it must not describe itself as a complete observation.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(7):
        _write_record(cache, _record_name(index), "{broken")

    at_cap = _read(cache, contract=CONTRACT, max_reported_names=7)
    assert len(at_cap.malformed) == 7
    assert at_cap.names_truncated is False
    assert at_cap.complete is True
    assert at_cap.bound == lcr.BOUND_NONE

    over_cap = _read(cache, contract=CONTRACT, max_reported_names=6)
    assert len(over_cap.malformed) == 6
    assert over_cap.names_truncated is True
    assert over_cap.complete is False, "a truncated list is not a complete observation"
    assert over_cap.bound == lcr.BOUND_NAMES
    assert over_cap.authoritative is False
    # ...and the counts stay authoritative-looking only in the sense of being honest:
    assert over_cap.parse_counts.get(lcr.PARSE_MALFORMED) == 7


def test_k_a_cooperative_stop_reports_truthful_partial_results(tmp_path):
    """Case 26."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(10):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    state = {"n": 0}

    def should_stop():
        state["n"] += 1
        return state["n"] > 3

    inv = _read_full(cache, should_stop=should_stop)
    assert inv.complete is False
    assert inv.bound == lcr.BOUND_STOPPED
    assert inv.authoritative is False
    assert inv.records_attempted == 3
    assert inv.complete_records == 3, "what it did see is reported, as a partial figure"


def test_k_the_ceilings_are_validated_before_any_filesystem_work(tmp_path):
    """A bad ceiling must be refused up front, not discovered half way through a folder."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    touched: list = []

    def spy_listdir(path):
        touched.append(path)
        return os.listdir(path)

    for field_name in ("max_entries", "max_records", "max_record_bytes", "max_total_bytes",
                       "max_reported_names"):
        for bad, expected in ((0, ValueError), (-1, ValueError), (True, TypeError),
                              (False, TypeError), (1.5, TypeError), ("5", TypeError),
                              (None, TypeError)):
            with pytest.raises(expected):
                _read(cache, listdir=spy_listdir, **{field_name: bad})
    assert touched == [], "a ceiling was validated after the folder was listed"


def test_k_a_bool_ceiling_is_refused_rather_than_silently_meaning_one(tmp_path):
    """``bool`` subclasses ``int``: ``max_records=True`` would mean 'one record' and then report a
    bound. The guard must REJECT it, and the discriminator is that the ceiling was never used."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(3):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    with pytest.raises(TypeError) as excinfo:
        _read(cache, max_records=True)
    assert "max_records" in str(excinfo.value)
    assert "bool" in str(excinfo.value)
    # The honest comparison: an integer 1 IS accepted and does bound the pass.
    bounded = _read_full(cache, max_records=1)
    assert bounded.records_attempted == 1 and bounded.bound == lcr.BOUND_RECORDS


def test_k_the_default_ceilings_are_the_documented_ones(tmp_path):
    """The limits are a published contract, so they are pinned rather than merely present."""
    assert lcr.MAX_CACHE_ENTRIES == 50_000 == lib.MAX_DIAGNOSTIC_ENTRIES
    assert lcr.MAX_REPORTED_RECORD_NAMES == 200 == lib.MAX_DIAGNOSTIC_NAMES
    assert lcr.MAX_CACHE_RECORDS == 20_000
    assert lcr.MAX_RECORD_BYTES == 4_000_000
    assert lcr.MAX_TOTAL_RECORD_BYTES == 1_000_000_000
    assert lcr.MAX_RECORD_BYTES < lcr.MAX_TOTAL_RECORD_BYTES


# =============================================================================================
# J. read-only, stdlib-only, no media (cases 27, 28)
# =============================================================================================

def test_l_no_media_or_model_file_is_ever_opened(tmp_path):
    """Case 27. Real media and a fake GGUF sit next to the cache; neither is touched."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    video = library / "clip.mp4"
    video.write_bytes(b"\x00" * 2048)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"\x00" * 1024)
    _write_record(cache, _NAME_A, _complete_payload(str(video)))

    opened: list = []
    real_open = open

    def spy_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    import builtins
    builtins.open = spy_open
    try:
        inv = _read_full(cache, roots=[lib.probe_root(str(library))])
    finally:
        builtins.open = real_open

    assert inv.records_parsed == 1
    assert [os.path.basename(p) for p in opened] == [_NAME_A, _NAME_A], \
        f"unexpected opens: {opened}"
    assert all(not p.endswith((".mp4", ".gguf")) for p in opened)


def test_l_a_whole_pass_changes_nothing_on_disk(tmp_path):
    """Case 28. Manifest of every entry before and after, plus the directory set.

    This is what would catch a resolver that quietly created the folder it was asked about -- the
    measured hazard behind this whole feature.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    (library / "clip.mp4").write_bytes(b"v" * 64)
    _write_record(cache, _NAME_A, _complete_payload(str(library / "clip.mp4")))
    _write_record(cache, _NAME_B, "{broken")
    _write_record(cache, "qwen_batch_request_1.json", {"x": 1})

    def manifest():
        rows = []
        for current, directories, files in os.walk(str(tmp_path)):
            rows.append((current, tuple(sorted(directories)), tuple(sorted(files))))
            for name in sorted(files):
                info = os.lstat(os.path.join(current, name))
                rows.append((os.path.join(current, name), info.st_size, info.st_mtime_ns))
        return rows

    before = manifest()
    # include the absent-folder and blank-selection paths: the ones most likely to create something
    _read_full(cache, roots=[lib.probe_root(str(library))])
    _read_full(tmp_path / "does-not-exist")
    _read_full("")
    _read_full(cache, roots=[lib.probe_root(str(library))], max_records=1, max_reported_names=1)
    after = manifest()

    assert before == after
    assert not os.path.exists(str(tmp_path / "does-not-exist"))


def test_l_the_module_imports_only_the_stdlib_and_its_own_package():
    """Static, and the dynamic half is `test_no_runtime_dependency.py`'s discovered-module probe."""
    with open(_A3, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "json", "os", "stat", "dataclasses", "beatsync_fork"}, \
        imported


def test_l_the_module_references_no_mutating_or_identity_operation():
    """AST over CALLS and ATTRIBUTES, so the prose above may name what the code must not do."""
    with open(_A3, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    attributes = set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)

    for banned in ("remove", "unlink", "rmdir", "rmtree", "rename", "replace", "makedirs",
                   "mkdir", "write", "writelines", "truncate", "fsync", "mkstemp", "walk",
                   "save_config", "copy_record", "import_records", "save_preferences",
                   "resolve_location", "resolve_cache_location", "_cache_path",
                   "_video_signature", "_bounded_fingerprint", "measure"):
        assert banned not in attributes, f"A3 must not call .{banned}()"
        assert banned not in names, f"A3 must not reference {banned}"

    for banned in ("hashlib", "subprocess", "shutil", "tempfile"):
        assert banned not in names, f"A3 must not reference {banned}"

    # The one `open` it does make must be read-mode text.
    modes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            literal = [a for a in node.args[1:] if isinstance(a, ast.Constant)]
            modes.append(literal[0].value if literal else "r")
    assert modes == ["r"], f"unexpected open modes: {modes}"


def test_l_no_record_filename_is_ever_decoded_or_reconstructed():
    """A3 answers questions about files that ARE there, never about which records ought to exist."""
    with open(_A3, "r", encoding="utf-8") as handle:
        source = handle.read()

    def _decoders(text: str) -> list:
        """Every call that takes a string apart -- the beginning of decoding a cache key."""
        found = []
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Attribute) and node.attr in ("partition", "rpartition",
                                                                 "split", "rsplit", "removesuffix",
                                                                 "removeprefix"):
                found.append(node.attr)
            elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
                found.append("slice")
        return found

    # The detector must actually detect: proven on a probe, so an empty result below is evidence.
    assert sorted(set(_decoders("stem = name.partition('_')\nhead = name[:8]\n"))) == \
        ["partition", "slice"]

    assert _decoders(source) == [], f"A3 must not take a filename apart: {_decoders(source)}"
    assert "is_record_name" in source
    # the shape constants live in analysis_cache and must not be duplicated here
    for banned in ("0123456789abcdef", "_RECORD_PREFIX_LENGTH", "_RECORD_SIGNATURE_LENGTH"):
        assert banned not in source, f"A3 must not restate {banned}"


def test_l_the_reader_refuses_to_run_without_the_real_shape_authority(tmp_path):
    """There is deliberately no local fallback for rung 1, so it has no default."""
    cache = tmp_path / "cache"
    cache.mkdir()
    with pytest.raises(TypeError):
        lcr.read_records(str(cache))                         # no is_record_name at all
    with pytest.raises(TypeError):
        lcr.read_records(str(cache), is_record_name="not callable")


def test_l_the_result_is_frozen_and_carries_no_handles(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    inv = _read_full(cache)
    with pytest.raises(Exception):
        inv.state = "tampered"
    with pytest.raises(Exception):
        inv.records[0].parse = "tampered"
    import copy
    assert copy.deepcopy(inv).records[0].name == _NAME_A


def test_l_the_observation_window_is_reported_only_when_a_clock_is_supplied(tmp_path):
    """No clock is imported: the module cannot claim a snapshot it did not take."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    silent = _read_full(cache)
    assert (silent.observed_from, silent.observed_to) == ("", "")

    ticks = iter(["T1", "T2"])
    timed = _read_full(cache, clock=lambda: next(ticks))
    assert (timed.observed_from, timed.observed_to) == ("T1", "T2")


# =============================================================================================
# K. negative controls -- each mutant must change the fixture's answer
# =============================================================================================

def _mutant(*replacements):
    """A copy of the A3 module with one safety property weakened.

    Each anchor is asserted to have applied. A replacement that silently matches nothing produces a
    mutant identical to the original, and a 'surviving' mutant then proves nothing at all -- a trap
    this repository has hit before (a CRLF-anchored patch voided 11 of 24 mutants).
    """
    with open(_A3, "r", encoding="utf-8") as handle:
        source = handle.read()
    for old, new in replacements:
        assert source.count(old) == 1, f"anchor matched {source.count(old)} times: {old!r}"
        source = source.replace(old, new)
    name = f"mutant_library_cache_records_{abs(hash(source)):x}"
    module = types.ModuleType(name)
    module.__dict__["__file__"] = _A3
    # Registered in `sys.modules` before the exec: `dataclasses` resolves a field's string
    # annotation through `sys.modules[cls.__module__]`, so an unregistered module makes every
    # @dataclass in the mutant raise AttributeError instead of being built.
    sys.modules[name] = module
    try:
        exec(compile(source, "<mutant>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


def test_m_c1_dropping_the_truncation_term_lets_an_incomplete_pass_call_itself_complete(tmp_path):
    """Kills the A2-style C6 survivor: the mutant reports complete=True on a truncating fixture."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(7):
        _write_record(cache, _record_name(index), "{broken")

    weak = _mutant(("complete_pass = bound == BOUND_NONE and not names_truncated",
                    "complete_pass = bound == BOUND_NONE"),
                   ("if bound == BOUND_NONE and names_truncated:\n        bound = BOUND_NAMES",
                    "if False:\n        bound = BOUND_NAMES"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name,
                                contract=CONTRACT, max_reported_names=3)
    real = _read(cache, contract=CONTRACT, max_reported_names=3)
    assert mutated.names_truncated is True and real.names_truncated is True
    assert mutated.complete is True, "the mutant must be the weakened one"
    assert real.complete is False
    assert real.authoritative is False and mutated.authoritative is True


@pytest.mark.skipif(os.name != "nt",
                    reason="undecidable link metadata is a Windows condition: without "
                           "st_file_attributes a junction cannot be told from a folder, which is "
                           "why _link_evidence refuses only on nt. The product is Windows-only.")
def test_m_c2_accepting_undecidable_link_metadata_reads_an_entry_it_should_refuse(tmp_path):
    """The fixture must have READABLE size metadata and UNDECIDABLE link metadata.

    A first draft made ``lstat`` raise outright, and the mutant then still refused the entry --
    because the later size ``lstat`` failed too and the independent unreadable-entry branch caught
    it. That mutant was masked by a second defence, so it demonstrated nothing about the gate under
    test. Reporting the fact rather than the convenient version of it: the fail-closed gate IS
    backstopped for a raising ``lstat``, and this control isolates the case where it is not.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    real_lstat = os.lstat

    class _NoAttributes:
        """Size and mode available; the Windows attribute word is not. Undecidable, so refuse."""

        def __init__(self, info):
            self.st_size = info.st_size
            self.st_mode = info.st_mode
            self.st_file_attributes = None

    def blind_lstat(path):
        return _NoAttributes(real_lstat(path))

    assert lib._link_evidence(os.path.join(str(cache), _NAME_A), lstat=blind_lstat) \
        == lib._LINK_UNDECIDABLE

    weak = _mutant(("if evidence != _LINK_PLAIN:", "if evidence == '\\0never':"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, lstat=blind_lstat)
    real = _read(cache, lstat=blind_lstat)
    assert real.skipped_undecidable == (_NAME_A,) and real.records_attempted == 0
    assert mutated.skipped_undecidable == ()
    assert mutated.records_attempted == 1, "the mutant read an undecidable entry"


def test_m_c3_prefix_containment_puts_a_sibling_directory_in_scope(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    evil = tmp_path / "library-evil"
    evil.mkdir()
    _write_record(cache, _NAME_A, _complete_payload(str(evil / "clip.mp4")))
    root = lib.probe_root(str(library))

    weak = _mutant(("            inside = is_within(resolved, key)",
                    "            inside = os.path.normcase(resolved).startswith("
                    "os.path.normcase(key))"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name,
                                inspection_roots=[root])
    real = _read(cache, inspection_roots=[root])
    assert real.scope_counts == {lcr.SCOPE_OUT: 1}
    assert mutated.scope_counts == {"scope_in": 1}, "the mutant accepted a prefix match"


def test_m_c4_charging_only_recognised_records_lets_non_records_run_free(tmp_path):
    """A2's F-2 lesson as a control: an unsupported entry must consume the budget too.

    The mutant charges only recognised records, so a folder of worker artifacts costs it nothing
    and it classifies all 31 entries under a 10-entry ceiling -- then calls the result complete.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _record_name(1), _complete_payload("C:/x/a.mp4"))
    for index in range(30):
        _write_record(cache, f"qwen_batch_request_{index:05d}.json", {"x": index})

    weak = _mutant(("        entries_examined += 1\n\n        full = os.path.join(target, name)",
                    "        full = os.path.join(target, name)"),
                   ("        records_recognised += 1\n",
                    "        records_recognised += 1\n        entries_examined += 1\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name,
                                contract=CONTRACT, max_entries=10)
    real = _read(cache, contract=CONTRACT, max_entries=10)

    assert real.entries_examined == 10
    assert real.bound == lcr.BOUND_ENTRIES
    assert real.complete is False
    assert real.other_files == 9, "the ceiling was spent on worker artifacts, as it must be"

    assert mutated.other_files == 30, "the mutant classified every entry for free"
    assert mutated.entries_examined == 1
    assert mutated.bound == ""
    assert mutated.complete is True, "and called an over-budget pass a total"


def test_m_c5_treating_an_authority_exception_as_incomplete_invents_a_verdict(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def explode(data, require_ai):
        raise RuntimeError("authority unavailable")

    weak = _mutant(("                    record_complete = NOT_ASSESSED\n"
                    "                    detail = detail or f\"completion authority raised "
                    "{type(exc).__name__}\"",
                    "                    record_complete = COMPLETE_NO\n"
                    "                    _bump(complete_counts, record_complete)\n"
                    "                    detail = detail or f\"completion authority raised "
                    "{type(exc).__name__}\""))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name,
                                is_complete=explode, require_ai=False)
    real = _read(cache, is_complete=explode, require_ai=False)
    assert real.records[0].complete == lcr.NOT_ASSESSED and real.complete_counts == {}
    assert mutated.records[0].complete == "incomplete"
    assert mutated.complete_counts == {"incomplete": 1}, "the mutant published a figure"


def test_m_c6_defaulting_the_mode_publishes_completeness_nobody_asked_for(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    candidates = [_candidate(0, tagged=True)]
    payload = _complete_payload("C:/x/a.mp4", candidates=candidates, ai=True)
    payload["ai_enabled"] = False            # complete under no_ai, incomplete under require_ai
    _write_record(cache, _NAME_A, payload)
    assert IS_COMPLETE(payload, False) is True
    assert IS_COMPLETE(payload, True) is False

    weak = _mutant(("    assess_complete = callable(is_complete) and require_ai is not None",
                    "    assess_complete = callable(is_complete)"),
                   ("                    verdict = bool(is_complete(payload, require_ai))",
                    "                    verdict = bool(is_complete(payload, bool(require_ai)))"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name,
                                is_complete=IS_COMPLETE)
    real = _read(cache, is_complete=IS_COMPLETE)
    assert real.completeness_assessed is False and real.complete_records is None
    assert mutated.completeness_assessed is True
    assert mutated.complete_records == 1, \
        "the mutant answered for no_ai without being told the mode"
    assert mutated.analysis_mode == "no_ai", \
        "and labelled the figure with a mode the caller never established"
    assert real.analysis_mode == "", "the real module names no mode when it answers nothing"


def test_m_c7_not_charging_the_delegated_read_understates_the_bytes(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.lstat(path).st_size

    weak = _mutant(("                        bytes_charged += size\n"
                    "                        try:\n"
                    "                            ok, reason = looks_reusable(full, contract)",
                    "                        try:\n"
                    "                            ok, reason = looks_reusable(full, contract)"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                looks_reusable=ac.looks_reusable)
    real = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable)
    assert real.bytes_charged == 2 * size
    assert mutated.bytes_charged == size, "the mutant treated the helper's read as free"


def test_m_c8_resolving_a_blank_selection_inspects_the_working_directory(tmp_path, monkeypatch):
    busy = tmp_path / "cwd-with-records"
    busy.mkdir()
    _write_record(busy, _NAME_A, _complete_payload("C:/x/a.mp4"))
    monkeypatch.chdir(busy)

    weak = _mutant(('    target = _clean(cache_dir)',
                    '    target = _clean(cache_dir) or os.path.abspath("")'))
    mutated = weak.read_records("", is_record_name=ac.is_record_name, contract=CONTRACT)
    real = _read("", contract=CONTRACT)
    assert real.state == lcr.CACHE_UNRESOLVED and real.records_recognised == 0
    assert mutated.records_recognised == 1, "the mutant inspected the working directory"


def test_m_the_mutant_harness_itself_refuses_a_vacuous_anchor():
    """The control on the controls: an anchor that matches nothing must raise, not pass quietly."""
    with pytest.raises(AssertionError):
        _mutant(("this text is definitely not in the module", "x"))
    with pytest.raises(AssertionError):
        _mutant(("os", "x"))          # matches many times -- also not a usable anchor
