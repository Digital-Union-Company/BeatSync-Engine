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


# =============================================================================================
# N. the independently reproduced findings F-1 .. F-7
#
# Every test in this section was run against the PRE-REMEDIATION module and shown to fail there,
# except where noted: F-6's behaviour is identical before and after (the fix is additive
# disclosure), so its evidence is the negative control N-M6 rather than an old/new difference.
# That distinction is stated per test rather than glossed, because a test that cannot fail is not
# evidence of an invariant.
# =============================================================================================


def _exact_bytes(directory, name: str, payload, *, pad_to: int) -> str:
    """Write a record whose encoded length is EXACTLY ``pad_to`` bytes.

    ``newline=""`` matters: without it the platform translates the JSON's own characters on the way
    out and the byte count stops being the one the test computed.
    """
    for pad in range(0, 8192):
        candidate = dict(payload)
        candidate["_pad"] = "x" * pad
        text = json.dumps(candidate)
        if len(text.encode("utf-8")) == pad_to:
            path = os.path.join(str(directory), name)
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
            return text
    raise AssertionError(f"no padding reaches exactly {pad_to} bytes")


# ---------------------------------------------------------------------------------------------
# F-1  a resource ceiling that stops a requested observation must be announced
# ---------------------------------------------------------------------------------------------

def test_n_f1_a_budget_refused_transport_reread_is_announced_as_a_bound(tmp_path):
    """The pass may not call itself a complete, authoritative total when a ceiling stopped rung 4.

    Pre-remediation this reported ``bound=BOUND_NONE``, ``complete=True`` and
    ``authoritative=True`` for every budget in ``[S, 2S-1]`` while the delegated re-read had been
    refused -- the one thing a bounded reader must never do, and the same standard the module
    already applied to a truncated name list.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.path.getsize(os.path.join(str(cache), _NAME_A))

    for budget in (size, size + 1, 2 * size - 1):
        inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable,
                    max_total_bytes=budget)
        assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}, budget
        assert inv.bound == lcr.BOUND_TOTAL_BYTES, budget
        assert inv.complete is False, budget
        assert inv.authoritative is False, budget
        assert inv.assessments_complete is False, budget
        # The ceiling stopped an ASSESSMENT, not the enumeration: the counts still cover the folder.
        assert inv.entries_complete is True, budget
        assert inv.counts_exact is True, budget
        assert "budget" in inv.records[0].detail


def test_n_f1_at_twice_the_record_size_the_reread_happens_and_raises_no_bound(tmp_path):
    """The exact upper threshold: 2S is enough for the parse AND the delegated re-read."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.path.getsize(os.path.join(str(cache), _NAME_A))

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable,
                max_total_bytes=2 * size)
    assert inv.transport_counts == {lcr.TRANSPORT_OK: 1}
    assert inv.bound == lcr.BOUND_NONE
    assert inv.complete is True and inv.authoritative is True
    assert inv.bytes_charged == 2 * size, "the parse and the delegated re-read are both charged"
    # Asserted last, deliberately: everything above is unchanged by the remediation, so this test
    # is a THRESHOLD control that must keep passing, not a discriminator for the fix.
    assert inv.assessments_complete is True


def test_n_f1_a_trailing_non_record_entry_does_not_hide_the_ceiling(tmp_path):
    """The reachable shape: the budget-exhausted record is the last thing that charges bytes.

    A non-record entry costs no bytes, so the loop runs to the end and nothing else trips the
    parse-side check -- which is exactly how the omission stayed invisible.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    _write_record(cache, "zz_qwen_batch_request_00001.json", {"retained": "for debugging"})
    size = os.path.getsize(os.path.join(str(cache), _NAME_A))

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable, max_total_bytes=size)
    assert inv.other_files == 1, "the loop really did reach the trailing entry"
    assert inv.bound == lcr.BOUND_TOTAL_BYTES
    assert inv.complete is False and inv.authoritative is False


def test_n_f1_the_first_bound_reached_is_the_one_reported(tmp_path):
    """A later limit must not overwrite the bound that actually stopped the first observation."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(4):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))
    size = os.path.getsize(os.path.join(str(cache), _record_name(0)))

    # Enough budget for the first record's parse but not its delegated re-read, and an entry
    # ceiling that trips on the very next entry. Pre-remediation the byte ceiling set no bound at
    # all and the later entry ceiling was reported instead, which named the wrong limit.
    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable,
                max_total_bytes=size, max_entries=1)
    assert inv.bound == lcr.BOUND_TOTAL_BYTES, "the byte ceiling came first and must survive"
    assert inv.complete is False
    assert inv.entries_complete is False, "...and the enumeration is separately reported as partial"


# ---------------------------------------------------------------------------------------------
# F-2  only a root A1 positively observed may anchor rung 6
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state_factory, label", [
    (lambda p: lib.probe_root(str(p / "never-created")), "configured_not_created"),
    (lambda p: lib.probe_root("Z:\\NoSuchVolume\\Library"), "root_unreachable"),
    (lambda p: lib.RootState(typed="E:\\t", real="D:\\ClaimedNeverProbed",
                             verdict=lib.NOT_INSPECTED), "not_inspected"),
    (lambda p: lib.RootState(typed="E:\\u", real="D:\\Unknown", verdict=lib.UNKNOWN,
                             anchor_reachable=True), "unknown"),
])
def test_n_f2_a_root_that_was_not_positively_observed_cannot_answer_scope(tmp_path, state_factory,
                                                                         label):
    """``root_key`` is a DERIVED property and is populated for every one of these verdicts.

    Pre-remediation each of them produced a definite ``SCOPE_IN``/``SCOPE_OUT`` -- a claim about a
    library that was unreachable, never created, or never looked at. A1 publishes
    `RootState.positively_observed` for precisely this, and it was not consulted.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    state = state_factory(tmp_path)
    assert state.root_key, f"{label}: the fixture must carry a derived root_key"
    assert state.positively_observed is False, label
    _write_record(cache, _NAME_A, _complete_payload(os.path.join(state.real, "clip.mp4")))

    inv = _read_full(cache, roots=[state])
    assert inv.scope_assessed is False, label
    assert inv.scope_counts == {}, label
    assert inv.records[0].scope == lcr.NOT_ASSESSED, label
    assert inv.inspection_roots == (), label
    assert inv.unverified_roots == (state.root_key,), label


def test_n_f2_a_positively_observed_root_still_answers_scope(tmp_path):
    """The positive control: the fix must not disarm rung 6 for a root A1 really did see."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    _write_record(cache, _NAME_A, _complete_payload(str(library / "clip.mp4")))
    _write_record(cache, _NAME_B, _complete_payload("D:\\Elsewhere\\clip.mp4"))
    state = lib.probe_root(str(library))
    assert state.verdict == lib.PRESENT and state.positively_observed is True

    inv = _read_full(cache, roots=[state])
    assert inv.scope_assessed is True
    assert inv.scope_counts == {lcr.SCOPE_IN: 1, lcr.SCOPE_OUT: 1}
    assert inv.inspection_roots == (state.root_key,)
    # Last, because every claim above is unchanged by the remediation: this is the control proving
    # the fix did not disarm rung 6 for a root A1 really did observe.
    assert inv.unverified_roots == ()


def test_n_f2_a_bare_path_string_is_an_unverified_input_not_a_verified_root(tmp_path):
    """Nobody probed it, so it may be reported but must never anchor a verdict."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("Q:\\PhantomLibrary\\clip.mp4"))

    inv = _read_full(cache, roots=["Q:\\PhantomLibrary"])
    assert inv.scope_assessed is False
    assert inv.scope_counts == {}
    assert inv.records[0].scope == lcr.NOT_ASSESSED
    assert inv.inspection_roots == ()
    assert inv.unverified_roots and "phantomlibrary" in inv.unverified_roots[0].lower()


def test_n_f2_scope_still_stats_no_source_video(tmp_path):
    """Rung 6 resolves a path and compares it. It must not look at the source at all."""
    cache = tmp_path / "cache"
    cache.mkdir()
    library = tmp_path / "library"
    library.mkdir()
    absent = str(library / "never-existed.mp4")
    _write_record(cache, _NAME_A, _complete_payload(absent))
    state = lib.probe_root(str(library))

    seen = []

    def spy_lstat(path):
        seen.append(os.path.normcase(os.path.abspath(path)))
        return os.lstat(path)

    inv = _read_full(cache, roots=[state], lstat=spy_lstat)
    assert inv.scope_counts == {lcr.SCOPE_IN: 1}, "a containment claim, on a file that is not there"
    assert not os.path.exists(absent)
    assert os.path.normcase(os.path.abspath(absent)) not in seen


# ---------------------------------------------------------------------------------------------
# F-3  the transport change-discriminator, on BOTH branches
# ---------------------------------------------------------------------------------------------

def _swap_reader(path: str, replacement: str, *, atomic: bool):
    """Hand back the ORIGINAL bytes, then put ``replacement`` on disk before rung 4 delegates."""
    def read_text(target):
        with open(target, "r", encoding="utf-8") as handle:
            original = handle.read()
        if atomic:
            temporary = target + ".newgen"
            with open(temporary, "w", encoding="utf-8", newline="") as handle:
                handle.write(replacement)
            os.replace(temporary, target)
        else:
            with open(target, "r+", encoding="utf-8", newline="") as handle:
                handle.seek(0)
                handle.write(replacement)
        return original
    return read_text


def test_n_f3_a_same_size_atomic_replacement_is_not_a_stable_transport_verdict(tmp_path):
    """The exact concurrency the product can produce, and the reason mtime is not the test.

    `video_analysis._save_cache` publishes every record through a unique temp file and
    ``os.replace``, so a replacement always changes the inode -- measured 40/40, against 5/40 for
    ``st_mtime_ns``, whose resolution lets a fast write land inside one tick. Size alone moves on
    neither. Pre-remediation this record was published as ``TRANSPORT_OK`` with ``unstable=False``:
    a verdict about bytes the pass never parsed, presented as a stable observation.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    deferred = dict(_complete_payload("C:/x/a.mp4", candidates=[_candidate(0, tagged=False)]))
    deferred["ai_deferred"] = True
    usable = _complete_payload("C:/x/a.mp4", candidates=[_candidate(0, tagged=False)])
    width = max(len(json.dumps(deferred).encode("utf-8")),
                len(json.dumps(usable).encode("utf-8"))) + 32
    original = _exact_bytes(cache, _NAME_A, deferred, pad_to=width)
    replacement = _exact_bytes(tmp_path, "staged.json", usable, pad_to=width)
    assert len(original.encode("utf-8")) == len(replacement.encode("utf-8"))
    assert original != replacement, "the two payloads must actually differ"
    _exact_bytes(cache, _NAME_A, deferred, pad_to=width)

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable,
                read_text=_swap_reader(os.path.join(str(cache), _NAME_A), replacement,
                                       atomic=True))
    assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}
    assert inv.changed == (_NAME_A,)
    assert inv.unstable is True
    assert inv.assessments_complete is False


def test_n_f3_a_replacement_of_a_different_size_is_detected_too(tmp_path):
    """The easy case, kept as a control so the identity widening cannot regress it."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    bigger = json.dumps(_complete_payload("C:/x/a.mp4")) + (" " * 512)

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable,
                read_text=_swap_reader(os.path.join(str(cache), _NAME_A), bigger, atomic=True))
    assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}
    assert inv.changed == (_NAME_A,)


def test_n_f3_growth_during_an_ACCEPTED_transport_read_is_detected(tmp_path):
    """The general gap the same-size case is a special case of.

    The post-read re-observation used to sit inside the refusal branch only, so an ACCEPTED verdict
    was never checked at all -- a plain size change during the helper's own read was published as a
    stable ``TRANSPORT_OK``.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def growing(path, contract):
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(" " * 2048)
        return ac.looks_reusable(path, contract)

    inv = _read(cache, contract=CONTRACT, looks_reusable=growing)
    assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}
    assert inv.changed == (_NAME_A,)
    assert inv.unstable is True


def test_n_f3_growth_during_a_REFUSED_transport_read_is_still_detected(tmp_path):
    """The branch that already verified must keep verifying."""
    cache = tmp_path / "cache"
    cache.mkdir()
    deferred = dict(_complete_payload("C:/x/a.mp4"))
    deferred["ai_deferred"] = True
    _write_record(cache, _NAME_A, deferred)

    def growing(path, contract):
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(" " * 2048)
        return ac.looks_reusable(path, contract)

    inv = _read(cache, contract=CONTRACT, looks_reusable=growing)
    assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}
    assert inv.changed == (_NAME_A,)


def test_n_f3_a_stable_record_still_reaches_a_transport_verdict(tmp_path):
    """The positive control. An identity check that refused everything would be useless."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    deferred = dict(_complete_payload("C:/x/b.mp4"))
    deferred["ai_deferred"] = True
    _write_record(cache, _NAME_B, deferred)

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable)
    assert inv.transport_counts == {lcr.TRANSPORT_OK: 1, lcr.TRANSPORT_REFUSED: 1}
    assert inv.changed == () and inv.unstable is False
    assert inv.complete is True
    assert inv.assessments_complete is True


def test_n_f3_an_unobservable_identity_refuses_a_stable_verdict(tmp_path):
    """"We could not look" is not evidence that nothing moved, so it must fail closed."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    calls = {"n": 0}

    def flaky_lstat(path):
        calls["n"] += 1
        if calls["n"] > 2 and os.path.basename(path) == _NAME_A:
            raise OSError(5, "device error")
        return os.lstat(path)

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable, lstat=flaky_lstat)
    assert inv.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}


# ---------------------------------------------------------------------------------------------
# F-4  the per-record ceiling governs the bytes this pass consumed
# ---------------------------------------------------------------------------------------------

def _grow_after_the_size_gate(target: str, text: str):
    """Grow ``target`` after its SECOND lstat -- the size gate -- and before the read.

    The reader legitimately stats each entry twice (link evidence, then size), so a hook keyed on
    the first call never opens the stat-then-read window at all: the size gate then simply observes
    the grown file and refuses it, which looks like the ceiling working.
    """
    seen = {"n": 0}

    def wrapped(path):
        info = os.lstat(path)
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(target)):
            seen["n"] += 1
            if seen["n"] == 2:
                with open(target, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
        return info

    return wrapped


def test_n_f4_a_record_that_grows_after_its_size_gate_is_refused_not_parsed(tmp_path):
    """Pre-remediation the read was unbounded: measured parsing 5 000 190 bytes under a 1 000-byte
    ceiling, charging 177, naming nothing in ``oversize`` and reporting no instability.

    The ceiling now applies to the bytes consumed, the refusal is ``PARSE_TOO_LARGE`` rather than a
    malformed prefix, and the growth is reported.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    small = os.path.getsize(path)
    grown = json.dumps({**_complete_payload("C:/x/a.mp4"), "_bulk": "y" * 200_000})
    assert small < 1_000 < len(grown.encode("utf-8"))

    inv = _read(cache, contract=CONTRACT, max_record_bytes=1_000,
                lstat=_grow_after_the_size_gate(path, grown))
    assert inv.parse_counts == {lcr.PARSE_TOO_LARGE: 1}
    assert lcr.PARSE_OK not in inv.parse_counts, "the oversized payload must NOT have been parsed"
    assert inv.oversize == (_NAME_A,)
    assert inv.changed == (_NAME_A,), "it demonstrably moved after it was measured"
    assert inv.bytes_charged > small, "the charge must reflect what was really consumed"
    assert inv.records[0].parse == lcr.PARSE_TOO_LARGE
    assert inv.assessments_complete is False


def test_n_f4_an_oversized_read_is_never_reported_as_malformed(tmp_path):
    """A truncated prefix of a JSON object is either invalid or a DIFFERENT, smaller object.

    Either way it must not be parsed, and the verdict must say "too large" rather than "malformed":
    they are different facts about the record and the operator acts on them differently.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    grown = json.dumps({**_complete_payload("C:/x/a.mp4"), "_bulk": "y" * 50_000})

    inv = _read(cache, contract=CONTRACT, max_record_bytes=2_000,
                lstat=_grow_after_the_size_gate(path, grown))
    assert lcr.PARSE_MALFORMED not in inv.parse_counts
    assert inv.parse_counts == {lcr.PARSE_TOO_LARGE: 1}
    assert inv.malformed == ()


def test_n_f4_a_multibyte_record_is_judged_on_bytes_and_not_characters(tmp_path):
    """The read is bounded in CHARACTERS; the ceiling is a BYTE ceiling. Both must hold.

    ``json.dumps`` escapes non-ASCII by default, which would quietly make this fixture
    single-byte and the assertion vacuous -- hence ``ensure_ascii=False``.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    text = json.dumps({**_complete_payload("C:/x/a.mp4"), "_bulk": "é" * 800},
                      ensure_ascii=False)
    path = os.path.join(str(cache), _NAME_A)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    characters = len(text)
    octets = len(text.encode("utf-8"))
    assert octets > characters, "the fixture must really be multi-byte"

    ceiling = (characters + octets) // 2            # above the char count, below the byte count
    inv = _read(cache, contract=CONTRACT, max_record_bytes=ceiling)
    assert inv.parse_counts == {lcr.PARSE_TOO_LARGE: 1}, \
        "a character-count ceiling would have accepted this"


def test_n_f4_the_record_byte_ceiling_is_exact_at_n_and_n_plus_one(tmp_path):
    """A stable file at exactly the ceiling is accepted; one byte over is refused unread."""
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.path.getsize(path)

    reads = []

    def counting(target):
        reads.append(target)
        with open(target, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    at_cap = _read(cache, contract=CONTRACT, max_record_bytes=size, read_text=counting)
    assert at_cap.parse_counts == {lcr.PARSE_OK: 1}
    assert at_cap.bytes_charged == size

    reads.clear()
    over = _read(cache, contract=CONTRACT, max_record_bytes=size - 1, read_text=counting)
    assert over.parse_counts == {lcr.PARSE_TOO_LARGE: 1}
    assert reads == [], "an already-oversize record is refused by the size gate, still unread"
    assert over.bytes_charged == 0


def test_n_f4_ordinary_malformed_json_is_still_malformed(tmp_path):
    """The bounded reader must not reclassify the ordinary cases it already got right."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, "{broken")
    _write_record(cache, _NAME_B, "[1, 2, 3]")
    _write_record(cache, _NAME_C, _complete_payload("C:/x/c.mp4"))

    inv = _read(cache, contract=CONTRACT)
    assert inv.parse_counts == {lcr.PARSE_MALFORMED: 1, lcr.PARSE_NOT_OBJECT: 1, lcr.PARSE_OK: 1}
    assert inv.oversize == ()


def test_n_f4_the_delegated_reread_is_charged_as_an_estimate_and_says_so(tmp_path):
    """A3 cannot bound `looks_reusable`'s own read, and the module must not pretend otherwise."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.path.getsize(os.path.join(str(cache), _NAME_A))

    inv = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable)
    assert inv.bytes_charged == 2 * size, "the estimate is the stat size, charged twice"

    with open(_A3, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "stat-time estimate" in source, \
        "the limitation must be stated where a reader of the contract will find it"


# ---------------------------------------------------------------------------------------------
# F-5  completion accounting
# ---------------------------------------------------------------------------------------------

def test_n_f5_a_raising_authority_withholds_the_total_instead_of_reading_as_zero(tmp_path):
    """Pre-remediation this answered ``complete_records == 0``: "no records are complete", which is
    a measurement, rather than "the authority would not answer", which is the truth."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(5):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    def explode(data, require_ai):
        raise RuntimeError("authority unavailable")

    inv = _read(cache, contract=CONTRACT, is_complete=explode, require_ai=False)
    assert inv.complete_records is None, "withheld, never a measured zero"
    assert inv.completion_unavailable == 5
    assert inv.completion_accounted == inv.records_parsed == 5
    assert inv.assessments_complete is False
    assert all(record.complete == lcr.NOT_ASSESSED for record in inv.records)


@pytest.mark.parametrize("builder, expect_yes, expect_no", [
    (lambda index: _complete_payload(f"C:/x/{index}.mp4"), 4, 0),
    (lambda index: {"cache_contract": CONTRACT, "analysis_version": VERSION,
                    "video_file": f"C:/x/{index}.mp4", "candidates": [], "timings": {}}, 0, 4),
])
def test_n_f5_every_parsed_record_is_accounted_for(tmp_path, builder, expect_yes, expect_no):
    """``sum(complete_counts) + completion_unavailable == records_parsed``, both ways round."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(4):
        _write_record(cache, _record_name(index), builder(index))

    inv = _read(cache, contract=CONTRACT, is_complete=IS_COMPLETE, require_ai=False)
    assert inv.records_parsed == 4
    assert inv.completion_accounted == 4
    assert inv.complete_counts.get(lcr.COMPLETE_YES, 0) == expect_yes
    assert inv.complete_counts.get(lcr.COMPLETE_NO, 0) == expect_no
    assert inv.complete_records == expect_yes
    assert inv.completion_unavailable == 0
    assert inv.assessments_complete is True


def test_n_f5_one_refusal_past_the_detail_cap_still_accounts_and_withholds(tmp_path):
    """The worst shape: the refusal is beyond ``max_reported_names``, so no per-record detail
    carries it and the aggregate is the only place it can show up."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(6):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    def mixed(data, require_ai):
        if data.get("video_file", "").endswith("5.mp4"):
            raise RuntimeError("declines")
        return IS_COMPLETE(data, require_ai)

    inv = _read(cache, contract=CONTRACT, is_complete=mixed, require_ai=False,
                max_reported_names=2)
    assert inv.records_truncated is True and len(inv.records) == 2
    assert inv.complete_records is None, "one unanswered record makes the total not a total"
    assert inv.complete_counts.get(lcr.COMPLETE_YES) == 5, "the verdicts given remain available"
    assert inv.completion_unavailable == 1
    assert inv.completion_accounted == inv.records_parsed == 6


def test_n_f5_a_partial_pass_still_reports_the_records_it_did_judge(tmp_path):
    """A ceiling is NOT an unanswered authority: every record reached was genuinely judged, so the
    figure is reported and `authoritative` is what says it is not a folder total."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(6):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    inv = _read(cache, contract=CONTRACT, is_complete=IS_COMPLETE, require_ai=False,
                max_records=3)
    assert inv.bound == lcr.BOUND_RECORDS and inv.authoritative is False
    assert inv.complete_records == 3, "a partial figure, explicitly not a total"
    assert inv.records_parsed == 3
    assert inv.completion_accounted == 3


# ---------------------------------------------------------------------------------------------
# F-6  aggregate completeness is a different question from detail completeness
#
# NOTE ON EVIDENCE: the pre-remediation module behaves IDENTICALLY here -- it already kept the
# counts exact and already set `records_truncated`. The fix is the explicit, separately testable
# contract (`counts_exact`, `details_complete`, `entries_complete`, `assessments_complete`) plus
# the documented reason the two truncation indicators differ. The behavioural evidence that
# `records_truncated` cannot silently disappear is therefore the negative control
# `test_n_mf6_...` below, which the pre-remediation suite did NOT kill.
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("count", [199, 200, 201])
def test_n_f6_a_large_cache_keeps_exact_counts_with_a_shortened_detail_list(tmp_path, count):
    """A production cache holds thousands of records against a 200-entry detail cap.

    Two misreadings must both be impossible: that the displayed records are the whole cache, and
    that a withheld or shortened figure means zero.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(count):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    inv = _read(cache, contract=CONTRACT, is_complete=IS_COMPLETE, require_ai=False)

    # the counts are exact totals, whatever the detail list shows
    assert inv.entries_listed == count
    assert inv.records_recognised == inv.records_attempted == count
    assert inv.parse_counts.get(lcr.PARSE_OK) == count
    assert inv.complete_records == count
    assert sum(inv.parse_counts.values()) == count
    assert inv.entries_complete is True
    assert inv.counts_exact is True
    assert inv.assessments_complete is True

    truncated = count > lcr.MAX_REPORTED_RECORD_NAMES
    assert inv.records_truncated is truncated
    assert inv.details_complete is not truncated
    assert len(inv.records) == min(count, lcr.MAX_REPORTED_RECORD_NAMES)
    if truncated:
        assert len(inv.records) < inv.records_attempted, \
            "the display is shorter than the cache, and says so"


def test_n_f6_problem_name_truncation_still_makes_the_pass_incomplete(tmp_path):
    """The asymmetry, asserted rather than left implicit.

    A problem-name list is the ONLY place an alias, an escape, undecidable link metadata, a vanished
    entry or a changed one is ever named -- those entries never reach ``records`` at all -- so
    truncating it destroys evidence held nowhere else and the pass must not call itself complete.
    A shortened per-record display loses nothing the counters do not still hold.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(5):
        _write_record(cache, _record_name(index), "{broken")
    for index in range(5, 10):
        _write_record(cache, _record_name(index), "[1, 2]")

    inv = _read(cache, contract=CONTRACT, max_reported_names=3)
    assert inv.names_truncated is True
    assert inv.bound == lcr.BOUND_NAMES
    assert inv.complete is False and inv.authoritative is False
    assert inv.details_complete is False
    # ...and even then the counts remain exact totals for the folder
    assert inv.counts_exact is True and inv.entries_complete is True
    assert sum(inv.parse_counts.values()) == 10


def test_n_f6_the_four_truthfulness_questions_are_separately_answerable(tmp_path):
    """Enumeration, assessment, detail and count-exactness are four facts, not one."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(4):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    clean = _read_full(cache)
    assert (clean.entries_complete, clean.assessments_complete,
            clean.details_complete, clean.counts_exact) == (True, True, True, True)

    stopped = _read_full(cache, max_records=2)
    assert stopped.entries_complete is False and stopped.counts_exact is False
    assert stopped.assessments_complete is True, "what it did assess, it assessed fully"

    shortened = _read_full(cache, max_reported_names=1)
    assert shortened.details_complete is False
    assert shortened.entries_complete is True


# ---------------------------------------------------------------------------------------------
# F-7  one effective require_ai mode, and no dead weight
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("supplied", [True, False, 0, 1, "", "no", [], {}, 0.0, 2.5, [1]])
def test_n_f7_the_authority_and_the_label_always_answer_the_same_mode(tmp_path, supplied):
    """Pre-remediation the raw value reached the authority while the label ran its own truth test.

    They agreed only by coincidence; the two could disagree for any authority that compared
    identity rather than truthiness. One normalisation, used by both, makes that impossible.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    seen = {}

    def spy(data, require_ai):
        seen["received"] = require_ai
        return IS_COMPLETE(data, require_ai)

    inv = _read(cache, contract=CONTRACT, is_complete=spy, require_ai=supplied)
    assert isinstance(seen["received"], bool), "the authority must receive a real bool"
    assert seen["received"] is bool(supplied)
    assert inv.analysis_mode == ("require_ai" if seen["received"] else "no_ai")
    assert inv.completeness_assessed is True


def test_n_f7_none_remains_distinct_from_false(tmp_path):
    """Normalising must not collapse "no mode was established" into "judge the no-AI mode"."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    unasked = _read(cache, contract=CONTRACT, is_complete=IS_COMPLETE, require_ai=None)
    assert unasked.completeness_assessed is False
    assert unasked.complete_records is None
    assert unasked.analysis_mode == ""
    assert unasked.records[0].complete == lcr.NOT_ASSESSED

    asked = _read(cache, contract=CONTRACT, is_complete=IS_COMPLETE, require_ai=False)
    assert asked.completeness_assessed is True
    assert asked.analysis_mode == "no_ai"
    assert asked.complete_records == 1


def test_n_f7_every_name_imported_from_a1_is_actually_used():
    """A dead import is small, but it is also the shape of a dependency nobody checked."""
    with open(_A3, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "beatsync_fork.library_inventory":
            imported.update(alias.name for alias in node.names)
    used = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert imported, "the fixture must have found the import at all"
    assert not (imported - used), f"dead imports: {sorted(imported - used)}"


def test_n_f7_the_a1_import_guard_is_described_as_the_subset_assertion_it_is():
    """It asserts ``imported <= {...}``. Calling that an exact pin misdescribes the guard a reader
    would go and check, and the conclusion drawn from it does not need the overstatement."""
    with open(_A3, "r", encoding="utf-8") as handle:
        source = handle.read()
    assert "pinned by an existing A1 test to exactly" not in source
    assert "subset" in source
    # and the guard really is a subset assertion that excludes json
    guard = os.path.join(_REPO_ROOT, "tests", "test_library_inventory.py")
    with open(guard, "r", encoding="utf-8") as handle:
        guard_tree = ast.parse(handle.read())
    allowed = None
    for node in ast.walk(guard_tree):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == "imported" and isinstance(node.comparators[0], ast.Set)):
            allowed = {element.value for element in node.comparators[0].elts
                       if isinstance(element, ast.Constant)}
            assert isinstance(node.ops[0], ast.LtE), "the guard is <=, not =="
    assert allowed and "json" not in allowed


# =============================================================================================
# N-M. negative controls for the remediation -- each weakens one fix and must change an answer
# =============================================================================================

def test_n_mf1_not_announcing_the_byte_ceiling_lets_the_pass_call_itself_authoritative(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    size = os.path.getsize(os.path.join(str(cache), _NAME_A))

    weak = _mutant(("                    bound = bound or BOUND_TOTAL_BYTES\n"
                    "                    assessments_complete = False\n",
                    "                    pass\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                looks_reusable=ac.looks_reusable, max_total_bytes=size)
    real = _read(cache, contract=CONTRACT, looks_reusable=ac.looks_reusable, max_total_bytes=size)
    assert mutated.transport_counts == real.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}
    assert mutated.bound == lcr.BOUND_NONE and mutated.authoritative is True
    assert real.bound == lcr.BOUND_TOTAL_BYTES and real.authoritative is False


def test_n_mf2_accepting_any_root_key_lets_an_unverified_root_answer_scope(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    ghost = lib.probe_root(str(tmp_path / "never-created"))
    assert ghost.root_key and ghost.positively_observed is False
    _write_record(cache, _NAME_A, _complete_payload(os.path.join(ghost.real, "clip.mp4")))

    weak = _mutant(("    if getattr(item, \"positively_observed\", False):\n        return True\n"
                    "    return getattr(item, \"verdict\", \"\") in (PRESENT, ACCESS_DENIED)",
                    "    return True"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                inspection_roots=[ghost])
    real = _read(cache, contract=CONTRACT, inspection_roots=[ghost])
    assert mutated.scope_counts == {"scope_in": 1}, "the mutant judged against a folder never seen"
    assert real.scope_assessed is False and real.scope_counts == {}


def test_n_mf3a_comparing_size_alone_cannot_see_a_same_size_replacement(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    deferred = dict(_complete_payload("C:/x/a.mp4"))
    deferred["ai_deferred"] = True
    usable = _complete_payload("C:/x/a.mp4")
    width = max(len(json.dumps(deferred).encode("utf-8")),
                len(json.dumps(usable).encode("utf-8"))) + 32
    replacement = _exact_bytes(tmp_path, "staged.json", usable, pad_to=width)

    weak = _mutant(("    return (int(size),\n"
                    "            int(getattr(info, \"st_ino\", -1)),\n"
                    "            int(getattr(info, \"st_dev\", -1)))",
                    "    return (int(size),)"))

    def run(module):
        _exact_bytes(cache, _NAME_A, deferred, pad_to=width)
        return module.read_records(
            str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
            looks_reusable=ac.looks_reusable,
            read_text=_swap_reader(os.path.join(str(cache), _NAME_A), replacement, atomic=True))

    mutated = run(weak)
    real = run(lcr)
    assert mutated.transport_counts == {lcr.TRANSPORT_OK: 1} and mutated.changed == ()
    assert real.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1} and real.changed == (_NAME_A,)


def test_n_mf3b_verifying_only_the_refusal_branch_leaves_an_accepted_verdict_unchecked(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    def growing(path, contract):
        with open(path, "a", encoding="utf-8", newline="") as handle:
            handle.write(" " * 2048)
        return ac.looks_reusable(path, contract)

    weak = _mutant(("                            after = _observe_identity(full, lstat)",
                    "                            after = gate_identity if ok "
                    "else _observe_identity(full, lstat)"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                looks_reusable=growing)
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    real = _read(cache, contract=CONTRACT, looks_reusable=growing)
    assert mutated.transport_counts == {lcr.TRANSPORT_OK: 1} and mutated.changed == ()
    assert real.transport_counts == {lcr.TRANSPORT_UNAVAILABLE: 1}


def test_n_mf4_an_unbounded_read_parses_a_record_past_the_ceiling(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    path = _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    grown = json.dumps({**_complete_payload("C:/x/a.mp4"), "_bulk": "y" * 200_000})

    weak = _mutant(("            return handle.read(max_record_bytes + 1)",
                    "            return handle.read()"),
                   ("            if consumed > max_record_bytes:",
                    "            if False:"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                max_record_bytes=1_000,
                                lstat=_grow_after_the_size_gate(path, grown))
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    real = _read(cache, contract=CONTRACT, max_record_bytes=1_000,
                 lstat=_grow_after_the_size_gate(path, grown))
    assert mutated.parse_counts == {lcr.PARSE_OK: 1}, "the mutant parsed the oversized payload"
    assert real.parse_counts == {lcr.PARSE_TOO_LARGE: 1}


def test_n_mf5_not_counting_a_refusal_turns_it_back_into_a_measured_zero(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(3):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    def explode(data, require_ai):
        raise RuntimeError("declines")

    weak = _mutant(("                    completion_unavailable += 1\n"
                    "                    assessments_complete = False\n",
                    "                    pass\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                is_complete=explode, require_ai=False)
    real = _read(cache, contract=CONTRACT, is_complete=explode, require_ai=False)
    assert mutated.complete_records == 0, "the mutant published a measured zero"
    assert real.complete_records is None
    assert real.completion_accounted == real.records_parsed == 3


def test_n_mf6_records_truncated_cannot_silently_disappear(tmp_path):
    """The previously SURVIVING mutant: a shortened detail display that denies being shortened."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(9):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    weak = _mutant(("            records_truncated = True\n            continue",
                    "            records_truncated = False\n            continue"),
                   ("        else:\n            records_truncated = True\n",
                    "        else:\n            records_truncated = False\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                max_reported_names=4)
    real = _read(cache, contract=CONTRACT, max_reported_names=4)
    assert len(mutated.records) == len(real.records) == 4
    assert mutated.records_attempted == real.records_attempted == 9
    assert mutated.records_truncated is False, "the mutant claims an exhaustive 4-of-9 list"
    assert real.records_truncated is True
    # The flag is what the previously surviving mutant erased, so it is asserted on its own above
    # before the derived property is brought in.
    assert mutated.details_complete is True and real.details_complete is False


def test_n_mf7_deriving_the_mode_label_separately_can_mislabel_it(tmp_path):
    """The other previously SURVIVING mutant: label and authority answering different modes."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))

    weak = _mutant(("    if assess_complete:\n        require_ai = bool(require_ai)",
                    "    if False:\n        require_ai = bool(require_ai)"),
                   ('    mode = ("require_ai" if require_ai else "no_ai") if assess_complete '
                    'else ""',
                    '    mode = ("require_ai" if require_ai is True else "no_ai") '
                    'if assess_complete else ""'))
    seen = {}

    def spy(data, require_ai):
        seen["received"] = require_ai
        return IS_COMPLETE(data, require_ai)

    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                is_complete=spy, require_ai="no")
    mutated_received = seen["received"]
    real = _read(cache, contract=CONTRACT, is_complete=spy, require_ai="no")
    assert mutated.analysis_mode == "no_ai" and mutated_received == "no", \
        "the mutant labels no_ai while the authority was handed a truthy value"
    assert real.analysis_mode == "require_ai" and seen["received"] is True


def test_n_mf8_an_alias_refusal_must_keep_charging_the_skipped_entry(tmp_path):
    """Skipped entries are still work done, and must stay visible in the aggregate."""
    cache = tmp_path / "cache"
    cache.mkdir()
    _write_record(cache, _NAME_A, _complete_payload("C:/x/a.mp4"))
    target = tmp_path / "elsewhere"
    target.mkdir()
    # A junction first: `mklink /J` needs no elevation, whereas `os.symlink` does on Windows
    # unless Developer Mode is on. Preferring the junction is what keeps this control RUNNING
    # rather than skipping on an ordinary unprivileged session -- and a junction is the alias
    # kind this product actually meets.
    alias = os.path.normpath(os.path.join(str(cache), "aliasdir"))
    made = False
    if os.name == "nt":
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", alias, os.path.normpath(str(target))],
                              capture_output=True, text=True).returncode == 0
    if not made and _can_symlink(tmp_path):
        os.symlink(str(target), alias, target_is_directory=True)
        made = True
    if not made:
        pytest.skip("no directory-link primitive available in this environment")

    weak = _mutant(("        if evidence == _LINK_ALIAS:\n            note(aliases, name)\n"
                    "            skipped_entries += 1\n            continue",
                    "        if evidence == _LINK_ALIAS:\n            note(aliases, name)\n"
                    "            continue"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT)
    real = _read(cache, contract=CONTRACT)
    assert mutated.skipped_aliases == real.skipped_aliases == ("aliasdir",)
    assert mutated.skipped_entries == 0 and real.skipped_entries == 1


# =============================================================================================
# N-F8. an entry that could not be CLASSIFIED must not be published as an exact total
#
# F-8, from the final independent delta review of this PR. `_link_evidence` is consulted before
# the size gate and before `is_record_name`, so an entry whose `lstat` fails is refused there and
# is never tested for recognition. It was therefore absent from `records_recognised` while the pass
# still published whole-folder totals: four real Stage-5 records in one folder, one failing
# `lstat`, and `records_recognised=3` alongside `counts_exact=True`, `authoritative=True`,
# `assessments_complete=True` and `bound=''`. An exact count of the records this pass could *see*
# was being presented as an exact count of the records the folder *holds*.
#
# Two disciplines apply to every fixture below:
#
# * **the input is established, never assumed.** Each name is asserted against the genuine
#   `ac.is_record_name` and each file is asserted `os.path.isfile` BEFORE any failure is injected,
#   so "one of these really is a Stage-5 record the pass failed to count" is measured rather than
#   claimed. Not every skipped entry is a record; this one provably is;
# * **every assertion is behavioural.** An `AttributeError` on a missing field would prove nothing
#   about the misclassification, so the tests assert the answers, not the schema.
# =============================================================================================

_F8_NAMES = (_record_name(1), _record_name(2), _record_name(3), _record_name(900))


def _f8_cache(tmp_path):
    """Four real ordinary files, all four recognised record names. Returns ``(cache, victim)``."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for name in _F8_NAMES:
        _write_record(cache, name, _complete_payload(f"C:/x/{name}.mp4"))
    for name in _F8_NAMES:
        assert ac.is_record_name(name), f"fixture broken: {name!r} is not a recognised record name"
        assert os.path.isfile(os.path.join(str(cache), name)), f"fixture broken: {name!r}"
    return cache, _F8_NAMES[-1]


def _failing_lstat(victim: str, exc: OSError, *, at: int):
    """Fail the victim's ``at``-th ``lstat``. ``1`` = ``_link_evidence``; ``2`` = the size gate.

    The call index is load-bearing. ``read_records`` stats every entry **twice** -- once inside
    ``_link_evidence``, then again for the size gate -- so a hook that fires on the first call can
    never exercise the size-gate branches at all. Firing on the wrong call is how a first draft of
    this fixture measured nothing while appearing to pass.
    """
    real_lstat = os.lstat
    seen: dict = {}

    def hostile(path):
        if os.path.basename(str(path)) == victim:
            seen[victim] = seen.get(victim, 0) + 1
            if seen[victim] == at:
                raise exc
        return real_lstat(path)

    return hostile


# (exception, which lstat call fails, which diagnostic bucket names it)
_F8_CASES = [
    pytest.param(PermissionError(13, "Access is denied"), 1, "undecidable",
                 id="permission-denied-at-link-evidence"),
    pytest.param(FileNotFoundError(2, "No such file or directory"), 1, "undecidable",
                 id="gone-at-link-evidence"),
    pytest.param(OSError(1, "attributes unavailable"), 1, "undecidable",
                 id="undecidable-link-metadata"),
    pytest.param(PermissionError(13, "Access is denied"), 2, "unreadable",
                 id="permission-denied-at-size-gate"),
    pytest.param(FileNotFoundError(2, "No such file or directory"), 2, "vanished",
                 id="vanished-at-size-gate"),
    pytest.param(OSError(1, "device error"), 2, "unreadable",
                 id="device-error-at-size-gate"),
]


@pytest.mark.parametrize("exc,at,bucket", _F8_CASES)
def test_n_f8_an_unclassifiable_entry_is_not_an_exact_total(tmp_path, exc, at, bucket):
    """F-8. Four real records, one the inspector could not classify, and NO ceiling reached.

    All six variants were measured against the pre-remediation module and all six reported
    ``counts_exact=True`` and ``authoritative=True``. Note that where the failure is consistent the
    entry lands in ``skipped_undecidable`` -- ``_link_evidence`` refuses first -- which is why the
    size-gate branches need the ``at=2`` variants to be reached at all.
    """
    cache, victim = _f8_cache(tmp_path)
    opened: list = []

    def spy(path):
        opened.append(os.path.basename(str(path)))
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    inv = _read_full(cache, lstat=_failing_lstat(victim, exc, at=at), read_text=spy)

    # the misclassification itself: one real record is missing from rung 1's count
    assert inv.entries_listed == 4
    assert inv.entries_examined == 4
    assert inv.records_recognised == 3, "the fixture must lose exactly one real record"
    assert inv.records_attempted == 3
    assert inv.parse_counts == {lcr.PARSE_OK: 3}

    # ...so no count here may be published as a folder total.
    #
    # These three come FIRST, deliberately. On the pre-remediation module they are the assertions
    # that fail, and they fail because the answer is wrong -- not because a field is missing. An
    # `AttributeError` from reading a field a defective build never had would be a schema kill and
    # would prove nothing about the misclassification, so the behavioural claim is asserted before
    # the new counter is read at all.
    assert inv.counts_exact is False, "a count short by an unknown amount is not exact"
    assert inv.authoritative is False, "these may not be published as this folder's totals"
    assert inv.assessments_complete is False, "rung 1 was never put to that entry"
    assert inv.entries_unclassified == 1, "and the result must say HOW MANY it could not classify"

    # ...while the two facts that ARE established stay true, and no ceiling is invented
    assert inv.entries_complete is True, "enumeration genuinely did reach every name"
    assert inv.complete is True, "nothing stopped the loop, so nothing may claim to have"
    assert inv.bound == lcr.BOUND_NONE, "no resource ceiling was reached; none may be reported"
    assert inv.state == lcr.CACHE_PRESENT

    # ...the entry is named rather than silently dropped
    named = {"undecidable": inv.skipped_undecidable,
             "unreadable": inv.unreadable,
             "vanished": inv.vanished}[bucket]
    assert any(str(entry).startswith(victim) for entry in named), f"{victim} missing from {bucket}"
    assert inv.skipped_entries == 1

    # ...and it was never read: refusing to classify it must not become a reason to open it
    assert victim not in opened, f"the unclassifiable entry was read: {opened}"


def test_n_f8_a_vanished_entry_is_charged_too_and_the_rule_is_deliberate(tmp_path):
    """The conservative half of the F-8 rule, asserted so it cannot drift silently.

    A size-gate ``FileNotFoundError`` means the entry was positively an ordinary file microseconds
    earlier -- ``_link_evidence`` had already answered ``_LINK_PLAIN`` for it -- and then was gone
    before rung 1 was put to it. It is charged as unclassifiable anyway, for two reasons:

    * a folder that changed under the pass has no single state for which the counters could be a
      total, so "exact" is not assertable for either state; and
    * :attr:`unstable` is an *observation*, not a withdrawal of an exactness claim. Leaving a
      consumer to cross-read it before trusting ``counts_exact`` is the same shape of mistake as
      F-8 itself.

    The alternative rule -- treat a vanished entry as no longer part of the folder and keep the
    totals exact -- was considered and rejected on those grounds.
    """
    cache, victim = _f8_cache(tmp_path)
    inv = _read_full(cache, lstat=_failing_lstat(
        victim, FileNotFoundError(2, "No such file or directory"), at=2))

    assert victim in inv.vanished
    assert inv.unstable is True, "the instability is still reported in its own right"
    # behavioural first, for the same reason as the test above
    assert inv.counts_exact is False and inv.authoritative is False
    assert inv.entries_unclassified == 1, "and it is ALSO charged as unclassifiable"
    # the parse-time vanish is a different thing and is NOT charged here: that entry WAS
    # classified -- it reached rung 1, was recognised, and its read then failed.
    gone_at_parse = _read_full(cache, read_text=lambda p: (_ for _ in ()).throw(
        FileNotFoundError(2, "No such file")))
    assert gone_at_parse.parse_counts.get(lcr.PARSE_VANISHED) == 4
    assert gone_at_parse.records_recognised == 4, "every entry was classified"
    assert gone_at_parse.counts_exact is True, "the counts DO total the folder"
    assert gone_at_parse.entries_unclassified == 0, "a classified record is not an unknown entry"


def test_n_f8_a_subdirectory_is_a_positive_refusal_not_an_unknown_entry(tmp_path):
    """``lstat`` answered and ``S_ISREG`` said no. That is knowing what it is."""
    cache, _ = _f8_cache(tmp_path)
    (cache / "a-subdirectory").mkdir()

    inv = _read_full(cache)
    assert inv.records_recognised == 4, "all four records are still counted"
    assert inv.skipped_entries == 1, "the directory is charged as work"
    assert inv.counts_exact is True and inv.authoritative is True
    assert inv.entries_unclassified == 0, "but it is not an unclassifiable entry"


def test_n_f8_a_confirmed_alias_is_a_positive_refusal_not_an_unknown_entry(tmp_path):
    """A2's ``_LINK_ALIAS`` gate refuses an entry it has positively identified.

    This is the discriminator that proves the fix did not simply charge every skip: an alias is
    *known* not to be an ordinary file in this folder, so excluding it from the record counters is
    correct by construction and the totals stay exact.
    """
    cache, _ = _f8_cache(tmp_path)
    alias_name = _record_name(7)
    alias_path = os.path.join(str(cache), alias_name)
    _write_record(cache, alias_name, _complete_payload("C:/x/alias.mp4"))
    real_lstat = os.lstat

    class _Alias:
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

    inv = _read_full(cache, lstat=aliasing_lstat)
    assert inv.skipped_aliases == (alias_name,)
    assert inv.skipped_entries == 1
    assert inv.counts_exact is True and inv.authoritative is True
    assert inv.records_recognised == 4
    assert inv.entries_unclassified == 0, "a confirmed alias is not an unknown entry"


def test_n_f8_a_proven_escape_is_a_positive_refusal_not_an_unknown_entry(tmp_path):
    """The other deliberate exclusion: an entry that resolves outside the selected folder."""
    cache, _ = _f8_cache(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    escapee = _record_name(8)
    _write_record(cache, escapee, _complete_payload("C:/x/escape.mp4"))

    def escaping_realpath(path):
        if os.path.basename(str(path)) == escapee:
            return str(outside / escapee)
        return os.path.realpath(path)

    inv = _read_full(cache, realpath=escaping_realpath)
    assert inv.skipped_escapes == (escapee,)
    assert inv.skipped_entries == 1
    assert inv.counts_exact is True and inv.authoritative is True
    assert inv.records_recognised == 4
    assert inv.entries_unclassified == 0, "a proven escape is not an unknown entry"


@pytest.mark.parametrize("count", [201, 500])
def test_n_f8_a_clean_large_cache_keeps_exact_totals_with_a_shortened_display(tmp_path, count):
    """The regression guard for the accepted J-4 decision: a short display is not a short inventory.

    If the F-8 fix had been written as ``entries_unclassified = skipped_entries``, or had been
    folded into ``records_truncated``, this is what would have broken -- every production-sized
    cache would have stopped reporting exact totals.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(count):
        _write_record(cache, _record_name(index), _complete_payload(f"C:/x/{index}.mp4"))

    inv = _read_full(cache)

    assert inv.counts_exact is True
    assert inv.authoritative is True
    assert inv.records_truncated is True
    assert inv.details_complete is False
    assert len(inv.records) == lcr.MAX_REPORTED_RECORD_NAMES
    # every applicable aggregate still equals the full record count
    assert inv.entries_listed == count
    assert inv.records_recognised == count
    assert inv.records_attempted == count
    assert sum(inv.parse_counts.values()) == count
    assert sum(inv.contract_counts.values()) == count
    assert sum(inv.complete_counts.values()) == count
    assert inv.complete_records == count
    assert inv.completion_accounted == inv.records_parsed == count
    assert inv.entries_unclassified == 0, "nothing here was unclassifiable"


def test_n_mf9_suppressing_the_unclassified_count_republishes_a_false_total(tmp_path):
    """Negative control A: the accounting itself. Removing it restores the F-8 behaviour exactly."""
    cache, victim = _f8_cache(tmp_path)
    hostile = _failing_lstat(victim, PermissionError(13, "Access is denied"), at=1)

    weak = _mutant(("            note(undecidable, name)\n            skipped_entries += 1\n"
                    "            entries_unclassified += 1\n"
                    "            assessments_complete = False\n",
                    "            note(undecidable, name)\n            skipped_entries += 1\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                is_complete=IS_COMPLETE, require_ai=False, lstat=hostile)
    real = _read_full(cache, lstat=_failing_lstat(
        victim, PermissionError(13, "Access is denied"), at=1))

    # both lose the same record -- the mutation changes only what is CLAIMED about it
    assert mutated.records_recognised == real.records_recognised == 3
    assert mutated.skipped_undecidable == real.skipped_undecidable == (victim,)
    assert mutated.counts_exact is True and real.counts_exact is False
    assert mutated.authoritative is True and real.authoritative is False
    assert mutated.assessments_complete is True and real.assessments_complete is False
    assert mutated.entries_unclassified == 0 and real.entries_unclassified == 1


def test_n_mf10_dropping_the_term_from_the_two_honesty_properties_also_republishes_it(tmp_path):
    """Negative control B: the properties. The count survives; only its consequence is removed.

    Deliberately a *different* mutation from control A. Here ``entries_unclassified`` is still
    counted and still reported -- so nothing fails with an ``AttributeError`` and nothing is a
    schema kill -- and the mutant nevertheless publishes the counts as folder totals.
    """
    cache, victim = _f8_cache(tmp_path)
    exc = OSError(1, "device error")

    weak = _mutant(
        ("        return bool(self.entries_complete\n"
         "                    and not self.entries_unclassified\n",
         "        return bool(self.entries_complete\n"),
        ("        return bool(self.complete\n"
         "                    and not self.entries_unclassified\n",
         "        return bool(self.complete\n"))
    mutated = weak.read_records(str(cache), is_record_name=ac.is_record_name, contract=CONTRACT,
                                is_complete=IS_COMPLETE, require_ai=False,
                                lstat=_failing_lstat(victim, exc, at=2))
    real = _read_full(cache, lstat=_failing_lstat(victim, exc, at=2))

    assert mutated.entries_unclassified == real.entries_unclassified == 1, \
        "the count must survive the mutation, or this is a schema kill rather than a behavioural one"
    assert mutated.counts_exact is True and real.counts_exact is False
    assert mutated.authoritative is True and real.authoritative is False
