"""Qwen scalar/container trust boundary (T1).

Two independent boundaries carry values that came out of a subprocess's JSON or off disk:

1. **worker-response ingestion** - `_complete_deferred_qwen_batch` and `_annotate_candidates_with_qwen`
   read optional telemetry (durations, VRAM, batch size, model id) out of the parsed response;
2. **library aggregation** - the tail of `analyze_video_sources` reads the same kinds of field back out
   of every returned record, and on a warm run every one of those records is a **cache hit**.

Neither boundary may raise, change semantic completion, or let a non-finite value into an aggregate.
That is not a new rule: `_qwen_job_completed`, `_stored_ai_cache_is_consistent` and
`_cache_entry_is_complete` already decide completion from the envelope, real integer counts and the
requested/returned id sets alone, and the R2 accounting contract already says accounting "must stay
non-fatal". This suite pins the boundary those rules assume.

Two hazards here are demonstrated rather than asserted by fiat, because both were surprising:

* ``json`` accepts and emits ``NaN``/``Infinity``/``-Infinity``, so a non-finite telemetry value
  survives the worker response file *and* a cache round trip, and one of them makes every sum it
  enters non-finite for good (`test_json_really_does_admit_non_finite_numbers`,
  `test_a_non_finite_value_survives_a_real_cache_round_trip`);
* bare ``int()``/``float()`` raise on the other malformed shapes, and the shared-batch call site is
  unguarded, so a telemetry field could abort Stage 5 after real GPU minutes.

None of this says the shipping worker emits malformed telemetry - it cannot: it reports monotonic-clock
deltas, ``max(1, min(32, int(slots)))`` and a literal ``0.0``. The exposure is the retained
request/response and cache JSON under ``input/video_analysis_cache/``, a future worker change, or a
regression.

Like the other Stage-5 suites this runs on a bare CPython: `video_analysis.py` cannot be imported
(cv2/numpy/librosa, and it mutates PATH), so the real function bodies are lifted out by their ``ast``
source ranges and executed in a stdlib-only namespace, and the library aggregation - which lives inline
inside `analyze_video_sources` - is executed as the verbatim production assignment statements. The
bodies under test are therefore the production bodies, not a paraphrase of them. Every cache path is
inside pytest's ``tmp_path``; the runtime cache is never touched.
"""

from __future__ import annotations

import ast
import json
import math
import os
import tempfile
import time
from typing import Any, Dict

import pytest

_VA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "video_analysis.py")

# The T1 telemetry seam, plus the D1 count primitives it deliberately does not redefine.
_TELEMETRY_FUNCS = ("_as_mapping", "_is_real_number", "_optional_telemetry_number",
                    "_telemetry_seconds", "_telemetry_total", "_is_nonnegative_count",
                    "_bounded_count", "_telemetry_text", "_record_telemetry",
                    "_record_candidate_count")
_COUNT_FUNCS = ("_coerce_count", "_is_count", "_reported_count")
_CACHE_FUNCS = ("_safe_name", "_hash_text", "_same_source", "_stored_ai_cache_is_consistent",
                "_qwen_job_completed", "_deterministic_analysis_completed",
                "_cache_entry_is_complete", "_load_cache", "_save_cache", "_checkpoint_cache",
                "_fmt_seconds", "_select_ai_candidates", "_qwen_max_windows")
_PIPELINE_FUNCS = ("_annotate_candidates_with_qwen", "_complete_deferred_qwen_batch")
_ALL_FUNCS = _TELEMETRY_FUNCS + _COUNT_FUNCS + _CACHE_FUNCS + _PIPELINE_FUNCS
_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_QWEN_COMPLETED_KEY",
           "_QWEN_SINGLE_JOB_ID", "_DETERMINISTIC_SCORING_KEY")

# The library aggregation, in production evaluation order. `_vram_values` is a real intermediate.
_AGGREGATES = ("qwen_tag_count", "qwen_frame_count", "qwen_seconds", "qwen_inference_seconds",
               "qwen_model_id", "qwen_concurrency", "_vram_values", "qwen_peak_vram_gb")


def _source() -> str:
    with open(_VA, encoding="utf-8") as handle:
        return handle.read()


def _tree() -> ast.Module:
    return ast.parse(_source())


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} missing from video_analysis.py")


def _namespace(**stubs) -> Dict[str, Any]:
    """Exec the real production definitions verbatim in an isolated stdlib-only namespace."""
    tree = _tree()
    picked = [node for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in _ALL_FUNCS]
    missing = set(_ALL_FUNCS) - {node.name for node in picked}
    assert not missing, f"could not extract {missing}"
    consts = [node for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id in _CONSTS for t in node.targets)]
    module = ast.Module(body=consts + picked, type_ignores=[])
    ast.fix_missing_locations(module)
    merged: list = []
    namespace: Dict[str, Any] = {
        "os": os, "json": json, "tempfile": tempfile, "math": math, "time": time,
        "Any": Any, "Dict": Dict, "List": list, "Sequence": list, "Iterable": list,
        # faithful to the real `_merge_semantic`, which also sets `ai_analyzed`
        "_merge_semantic": lambda candidate, semantic: (
            merged.append(candidate["id"]),
            candidate.update({"ai_analyzed": True, "semantic": semantic}))[1],
    }
    namespace.update(stubs)
    exec(compile(module, _VA, "exec"), namespace)  # noqa: S102 - real source, isolated namespace
    for name in _CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"
    namespace["_merged"] = merged
    os.environ.pop("BEATSYNC_QWEN_MAX_WINDOWS", None)
    return namespace


@pytest.fixture()
def ns():
    return _namespace()


def _aggregate(videos):
    """Run the VERBATIM production aggregation assignments over `videos`."""
    source = _source()
    tree = ast.parse(source)
    body = _func(tree, "analyze_video_sources")
    found: Dict[str, str] = {}
    for node in ast.walk(body):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in _AGGREGATES and node.targets[0].id not in found:
            found[node.targets[0].id] = ast.get_source_segment(source, node)
    missing = [name for name in _AGGREGATES if name not in found]
    assert not missing, f"aggregation assignments missing: {missing}"
    scope = _namespace()
    scope["videos"] = videos
    for name in _AGGREGATES:
        exec(compile(found[name], f"<aggregate:{name}>", "exec"), scope)  # noqa: S102
    return {name: scope[name] for name in _AGGREGATES if not name.startswith("_")}


# --------------------------------------------------------------------------------- fixture builders
def _semantics(name, count):
    return {f"{name}-{i}": {"action": 0.9, "emotion": "calm"} for i in range(count)}


def _deferred_record(ns, name="a.mp4", n=3):
    """A parallel-pass record awaiting deferred Qwen, exactly as `_analyze_single_video` leaves it."""
    return {
        "analysis_version": ns["ANALYSIS_VERSION"], "cache_contract": ns["CACHE_CONTRACT_VERSION"],
        "video_file": rf"C:\src\{name}", "source_name": name, "fps": 25.0,
        "candidate_count": n,
        "candidates": [{"id": f"{name}-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                        "beauty_score": 0.4, "quality_score": 0.6, "editorial_score": 0.9 - i * 0.1}
                       for i in range(n)],
        "analysis_seconds": 12.0,
        "timings": {"total_seconds": 12.0, "candidate_scoring_seconds": 0.4},
        "ai_enabled": False, "ai_deferred": True,
    }


def _batch_response(timing=None, top=None, name="a.mp4", frames=3):
    """A fully successful single-job batch response: every requested candidate came back tagged."""
    job_timing = {"frame_count": frames, "tag_count": frames,
                  "inference_seconds": 5.0, "prefetch_seconds": 0.1}
    job_timing.update(timing or {})
    response = {
        "semantics_by_job": {"1": _semantics(name, frames)},
        "timings_by_job": {"1": job_timing},
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b",
        "batch_size": 4, "peak_vram_gb": 3.0,
    }
    response.update(top or {})
    return response


def _single_response(timing=None, top=None, frames=3):
    job_timing = {"frame_count": frames, "tag_count": frames,
                  "inference_seconds": 5.0, "prefetch_seconds": 0.1}
    job_timing.update(timing or {})
    response = {
        "timings_by_job": {"single": job_timing},
        "semantics": {f"c-{i}": {"action": 0.9} for i in range(frames)},
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b",
        "batch_size": 4, "peak_vram_gb": 3.0,
    }
    response.update(top or {})
    return response


def _run_batch(tmp_path, timing=None, top=None, response=None, frames=3):
    """Drive the real batch orchestration over one fully successful job."""
    payload = _batch_response(timing, top, frames=frames) if response is None else response
    scope = _namespace(_run_qwen_worker_batch=lambda **kwargs: payload,
                       _env_int=lambda name, default, lo=None, hi=None: default)
    record = _deferred_record(scope, n=frames)
    stats = _new_stats()
    items = [({"index": 1, "cache_file": str(tmp_path / "a.json")}, record)]
    scope["_complete_deferred_qwen_batch"](
        video_items=items, use_gpu=False, qwen_model_path="m.gguf",
        total_video_count=1, run_stats=stats)
    return record, stats, scope


def _run_single(timing=None, top=None, response=None, frames=3):
    payload = _single_response(timing, top, frames=frames) if response is None else response
    scope = _namespace(_run_qwen_worker=lambda **kwargs: payload)
    candidates = [{"id": f"c-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                   "beauty_score": 0.4, "quality_score": 0.6, "editorial_score": 0.9 - i * 0.1}
                  for i in range(frames)]
    stats = _new_stats()
    info = scope["_annotate_candidates_with_qwen"](
        video_file=r"C:\src\a.mp4", fps=25.0, candidates=candidates,
        qwen_model_path="m.gguf", use_gpu=False, run_stats=stats)
    return info, stats, candidates, scope


def _new_stats():
    return {"qwen_jobs": 0, "qwen_completed_jobs": 0, "qwen_incomplete_jobs": 0,
            "qwen_requested_count": 0, "qwen_frame_count": 0, "qwen_tag_count": 0,
            "qwen_seconds": 0.0, "qwen_inference_seconds": 0.0}


def _record(candidates=3, **telemetry):
    """A returned/cached record carrying library telemetry."""
    timings = {"qwen_tag_count": 3, "qwen_frame_count": 3, "qwen_seconds": 6.0,
               "qwen_inference_seconds": 5.0, "qwen_model_id": "qwen3vl-2b",
               "qwen_concurrency": 4, "qwen_peak_vram_gb": 3.0}
    timings.update(telemetry)
    return {"timings": timings,
            "candidates": [{"id": f"x-{i}"} for i in range(candidates)]}


# Every malformed shape the boundary has to survive, named so failures read clearly.
MALFORMED = [
    ("none", None),
    ("empty-string", ""),
    ("string", "bad"),
    ("numeric-string", "2.5"),
    ("true", True),
    ("false", False),
    ("list", [1, 2]),
    ("empty-list", []),
    ("dict", {"a": 1}),
    ("empty-dict", {}),
    ("nan", float("nan")),
    ("+inf", float("inf")),
    ("-inf", float("-inf")),
    ("negative", -5.0),
    ("negative-int", -5),
    ("huge-int", 10 ** 400),
]
MALFORMED_IDS = [name for name, _ in MALFORMED]
MALFORMED_VALUES = [value for _, value in MALFORMED]

# `-5.0` and `-5` really are real finite numbers; they are rejected by the *domain* rule, not by the
# type rule. Keeping the two apart is the difference between "this is not a number" and "this is a
# number that cannot mean what the field means".
_NEGATIVE_IDS = {"negative", "negative-int"}
NOT_A_NUMBER = [(name, value) for name, value in MALFORMED if name not in _NEGATIVE_IDS]
NOT_A_NUMBER_IDS = [name for name, _ in NOT_A_NUMBER]
NOT_A_NUMBER_VALUES = [value for _, value in NOT_A_NUMBER]

# Shapes that can never be a count. `huge-int` is excluded because it *is* a real non-negative
# integer - it is rejected by a natural bound where one exists, not by its type; see
# `test_an_absurdly_large_batch_size_is_reported_as_given_not_invented`.
NOT_A_COUNT = [(name, value) for name, value in MALFORMED if name != "huge-int"]
NOT_A_COUNT_IDS = [name for name, _ in NOT_A_COUNT]
NOT_A_COUNT_VALUES = [value for _, value in NOT_A_COUNT]


# ============================================================== 0. THE TWO DEMONSTRATED HAZARDS
def test_json_really_does_admit_non_finite_numbers():
    """The NaN/Infinity hazard is a property of this parser, not a hypothetical.

    ``json.loads`` accepts the bare ``NaN``/``Infinity``/``-Infinity`` tokens through its default
    ``parse_constant``, and ``json.dump`` emits them because ``allow_nan`` defaults to ``True``. So a
    non-finite telemetry value round-trips through the worker response file and through the cache, and
    ``float()`` will not reject it on the way back in.
    """
    parsed = json.loads('{"a": NaN, "b": Infinity, "c": -Infinity}')
    assert math.isnan(parsed["a"])
    assert math.isinf(parsed["b"]) and parsed["b"] > 0
    assert math.isinf(parsed["c"]) and parsed["c"] < 0
    assert "NaN" in json.dumps({"a": float("nan")})
    # and the conversion the old code used is perfectly happy with all of them
    assert math.isnan(float(float("nan")))
    assert math.isinf(float("inf"))


def test_a_non_finite_value_survives_a_real_cache_round_trip(ns, tmp_path):
    """Through the real `_save_cache`/`json.load`, so the poison-pill premise is proven not assumed."""
    path = str(tmp_path / "rt.json")
    ns["_save_cache"](path, {"timings": {"qwen_inference_seconds": float("nan"),
                                         "qwen_peak_vram_gb": float("inf")}})
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "NaN" in text and "Infinity" in text
    with open(path, encoding="utf-8") as handle:
        back = json.load(handle)
    assert math.isnan(back["timings"]["qwen_inference_seconds"])
    assert math.isinf(back["timings"]["qwen_peak_vram_gb"])


# ============================================================== 1. THE HELPER CONTRACT
@pytest.mark.parametrize("value", NOT_A_NUMBER_VALUES, ids=NOT_A_NUMBER_IDS)
def test_no_malformed_shape_is_a_real_number(ns, value):
    assert ns["_is_real_number"](value) is False


@pytest.mark.parametrize("value", [-5.0, -5, -0.001])
def test_negatives_are_real_numbers_rejected_by_their_domain(ns, value):
    """Two separate rules, deliberately: the type is fine, the value cannot mean a duration or a size."""
    assert ns["_is_real_number"](value) is True
    assert ns["_optional_telemetry_number"](value) is None


@pytest.mark.parametrize("value", [0, 0.0, 1, 2.5, 10 ** 18, 1e308])
def test_real_numbers_are_accepted(ns, value):
    assert ns["_is_real_number"](value) is True


def test_a_numeric_string_is_never_laundered_into_telemetry(ns):
    """`float("2.5")` succeeding is not a reason to believe a string in a numeric field."""
    assert ns["_is_real_number"]("2.5") is False
    assert ns["_optional_telemetry_number"]("2.5") is None
    assert ns["_telemetry_seconds"]({"x": "2.5"}, "x") == 0.0
    assert ns["_telemetry_total"](["2.5", "7"]) == 0.0
    assert ns["_bounded_count"]("3", 10) == 0


def test_bools_are_never_numbers_or_counts(ns):
    """``bool`` subclasses ``int``, which is how ``frame_count: true`` once compared equal to 1."""
    for value in (True, False):
        assert ns["_is_real_number"](value) is False
        assert ns["_optional_telemetry_number"](value) is None
        assert ns["_is_nonnegative_count"](value) is False
        assert ns["_bounded_count"](value, 10) == 0


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_malformed_telemetry_is_unknown_and_never_raises(ns, value):
    assert ns["_optional_telemetry_number"](value) is None
    assert ns["_telemetry_seconds"]({"x": value}, "x") == 0.0
    assert ns["_bounded_count"](value, 10) == 0
    # `_telemetry_text` is deliberately not asserted here: a non-empty string is a *valid* model id,
    # so "bad" is malformed for a duration and perfectly well formed for an identifier. Its own
    # contract is pinned by `test_telemetry_text_requires_a_real_non_empty_string`.


def test_a_huge_integer_does_not_raise_overflow(ns):
    """``math.isfinite(10 ** 400)`` raises ``OverflowError``; that is one of the shapes to survive."""
    assert ns["_is_real_number"](10 ** 400) is False
    assert ns["_optional_telemetry_number"](10 ** 400) is None
    # a huge *count* is a real non-negative int, so it is rejected by its bound rather than its type
    assert ns["_is_nonnegative_count"](10 ** 400) is True
    assert ns["_bounded_count"](10 ** 400, 10) == 0


def test_a_legitimate_zero_survives_every_telemetry_reader(ns):
    """Zero is a measurement. The whole point of not writing `value or default`."""
    assert ns["_optional_telemetry_number"](0.0) == 0.0
    assert ns["_optional_telemetry_number"](0) == 0.0
    assert ns["_telemetry_seconds"]({"x": 0.0}, "x") == 0.0
    assert ns["_is_nonnegative_count"](0) is True
    assert ns["_bounded_count"](0, 10) == 0
    # and absence is distinguishable from zero at the optional reader
    assert ns["_optional_telemetry_number"](None) is None
    assert ns["_telemetry_seconds"]({}, "x") == 0.0


def test_unknown_is_distinct_from_a_legitimate_zero(ns):
    """`peak_vram_gb` is literally `0.0` from the worker, so malformed must not also read as 0.0."""
    assert ns["_optional_telemetry_number"](0.0) == 0.0
    assert ns["_optional_telemetry_number"]("bad") is None
    assert ns["_optional_telemetry_number"](0.0) is not None


def test_a_negative_duration_is_not_evidence(ns):
    """`_fmt_seconds` already hides negatives on screen, so they must be rejected at ingestion."""
    assert ns["_optional_telemetry_number"](-0.001) is None
    assert ns["_telemetry_seconds"]({"x": -4.0}, "x") == 0.0
    assert ns["_fmt_seconds"](-4.0) == "0ms"  # the display clamp that made this invisible


def test_telemetry_text_requires_a_real_non_empty_string(ns):
    assert ns["_telemetry_text"]("qwen3vl-2b") == "qwen3vl-2b"
    assert ns["_telemetry_text"]("") == ""
    assert ns["_telemetry_text"](123) == ""
    assert ns["_telemetry_text"]({"a": 1}) == ""
    assert ns["_telemetry_text"](["m"]) == ""


def test_a_total_can_never_be_non_finite_even_from_finite_parts(ns):
    """Filtering individual values is not enough: finite values can overflow a running total."""
    total = ns["_telemetry_total"]([1e308, 1e308, 1e308])
    assert math.isfinite(total)
    assert total == 0.0, "an unmeasurable total degrades to neutral rather than reporting inf"
    assert ns["_telemetry_total"]([1.0, float("nan"), 2.0]) == pytest.approx(3.0)
    assert ns["_telemetry_total"]([1.0, float("inf"), 2.0]) == pytest.approx(3.0)
    assert ns["_telemetry_total"]([1.0, -99.0, 2.0]) == pytest.approx(3.0)


@pytest.mark.parametrize("value", [None, "", "nope", 0, 1, [1], ("a",), object()])
def test_a_non_dict_container_reads_as_empty(ns, value):
    assert ns["_as_mapping"](value) == {}


def test_as_mapping_passes_a_real_dict_through_unchanged(ns):
    payload = {"a": 1}
    assert ns["_as_mapping"](payload) is payload


def test_record_telemetry_survives_a_truthy_non_dict_timings(ns):
    """``(v.get("timings") or {})`` returned the string and then raised ``AttributeError`` on ``.get``."""
    assert ns["_record_telemetry"]({"timings": "nope"}) == {}
    assert ns["_record_telemetry"]({"timings": None}) == {}
    assert ns["_record_telemetry"]({}) == {}
    assert ns["_record_telemetry"](None) == {}
    assert ns["_record_telemetry"]({"timings": {"a": 1}}) == {"a": 1}


def test_record_candidate_count_is_the_natural_bound(ns):
    assert ns["_record_candidate_count"]({"candidates": [1, 2, 3]}) == 3
    assert ns["_record_candidate_count"]({"candidates": []}) == 0
    assert ns["_record_candidate_count"]({"candidates": "nope"}) == 0
    assert ns["_record_candidate_count"]({}) == 0


# ============================================================== 2. D1 COUNT SEMANTICS STAY FROZEN
def test_is_count_is_not_redefined(ns):
    """`_is_count` is load-bearing for completion. T1 adds a predicate; it does not change this one."""
    assert ns["_is_count"](3) is True
    assert ns["_is_count"](0) is True
    assert ns["_is_count"](-5) is True, "still admits negatives - that is why _bounded_count exists"
    assert ns["_is_count"](True) is False
    assert ns["_is_count"](2.9) is False
    assert ns["_is_count"]("3") is False


def test_reported_count_keeps_its_presence_semantics(ns):
    """A genuine 0 stays 0; the caller's default applies only when the field is absent."""
    assert ns["_reported_count"]({"frame_count": 0}, "frame_count", 10) == 0
    assert ns["_reported_count"]({}, "frame_count", 10) == 10
    assert ns["_reported_count"]({"frame_count": "bad"}, "frame_count", 10) == 0
    assert ns["_reported_count"]("not-a-dict", "frame_count", 10) == 10
    assert ns["_coerce_count"]("bad", 7) == 7


def test_the_new_count_predicate_is_separate_and_stricter(ns):
    assert ns["_is_nonnegative_count"](-5) is False
    assert ns["_is_count"](-5) is True
    assert ns["_bounded_count"](11, 10) == 0, "more decoded than submitted is not evidence"
    assert ns["_bounded_count"](10, 10) == 10
    assert ns["_bounded_count"](10, None) == 10


# ============================================================== 3. BATCH INGESTION (A-F)
@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_batch_malformed_inference_seconds(tmp_path, value):
    """The originally reported crash: `float(timing.get("inference_seconds") or 0.0)`."""
    record, stats, _ = _run_batch(tmp_path, timing={"inference_seconds": value})
    timings = record["timings"]
    assert timings["qwen_inference_seconds"] == 0.0
    assert math.isfinite(timings["qwen_seconds"])
    assert timings["qwen_seconds"] >= 0.0
    assert stats["qwen_inference_seconds"] == 0.0


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_batch_malformed_prefetch_seconds(tmp_path, value):
    record, _, _ = _run_batch(tmp_path, timing={"prefetch_seconds": value})
    assert record["timings"]["qwen_prefetch_seconds"] == 0.0
    assert math.isfinite(record["timings"]["qwen_seconds"])


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_batch_malformed_model_load_seconds(tmp_path, value):
    """This one raised *before* any job merged, so the whole batch lost its per-job checkpoints."""
    record, _, _ = _run_batch(tmp_path, top={"model_load_seconds": value})
    assert record["timings"]["qwen_model_load_seconds_amortized"] == 0.0
    assert math.isfinite(record["timings"]["qwen_seconds"])


@pytest.mark.parametrize("value", NOT_A_COUNT_VALUES, ids=NOT_A_COUNT_IDS)
def test_batch_malformed_batch_size_is_never_a_fake_concurrency(tmp_path, value):
    """Every shape that cannot be a count. `int(2.7)` was 2 and `int(True)` was 1 before."""
    record, _, _ = _run_batch(tmp_path, top={"batch_size": value})
    assert record["timings"]["qwen_concurrency"] == 0
    assert isinstance(record["timings"]["qwen_concurrency"], int)
    assert not isinstance(record["timings"]["qwen_concurrency"], bool)


def test_an_absurdly_large_batch_size_is_reported_as_given_not_invented(tmp_path):
    """Scoped decision, not an oversight.

    Concurrency has no natural bound available on this side: the producer's real ceiling is
    ``min(32, ...)`` inside `stage5_qwen_scene_worker.py`, and restating 32 here would be exactly the
    drifting magic number this seam is supposed to avoid. A huge integer is still a *real non-negative
    integer*, it cannot be NaN or Infinity, it cannot poison a sum, and it is only ever reported - so
    R1 requires non-negative-integer robustness and stops there. What must not happen is inventing a
    plausible-looking value out of a float, a bool or a string, which is what the tests above cover.
    """
    record, _, _ = _run_batch(tmp_path, top={"batch_size": 10 ** 40})
    assert record["timings"]["qwen_concurrency"] == 10 ** 40
    assert math.isfinite(record["timings"]["qwen_seconds"])


def test_batch_size_floats_and_bools_are_not_laundered(tmp_path):
    """`int(2.7)` was 2 and `int(True)` was 1 - a fabricated concurrency either way."""
    record, _, _ = _run_batch(tmp_path, top={"batch_size": 2.7})
    assert record["timings"]["qwen_concurrency"] == 0
    record, _, _ = _run_batch(tmp_path, top={"batch_size": True})
    assert record["timings"]["qwen_concurrency"] == 0
    record, _, _ = _run_batch(tmp_path, top={"batch_size": 4})
    assert record["timings"]["qwen_concurrency"] == 4


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_batch_malformed_peak_vram_is_unknown_not_zero(tmp_path, value):
    record, _, _ = _run_batch(tmp_path, top={"peak_vram_gb": value})
    assert record["timings"]["qwen_peak_vram_gb"] is None, "unknown, kept distinct from a real 0.0"


def test_a_genuine_zero_vram_is_stored_as_zero(tmp_path):
    """The shipping worker reports exactly this, so it must not read as unknown."""
    record, _, _ = _run_batch(tmp_path, top={"peak_vram_gb": 0.0})
    assert record["timings"]["qwen_peak_vram_gb"] == 0.0


@pytest.mark.parametrize("value", [123, 2.5, {"a": 1}, ["m"], True, "", None])
def test_batch_non_string_model_id_is_not_stringified(tmp_path, value):
    record, _, _ = _run_batch(tmp_path, top={"model_id": value})
    assert record["timings"]["qwen_model_id"] == ""


# ============================================================== 4. COMPLETION INDEPENDENCE (G)
@pytest.mark.parametrize("field,value", [
    ("inference_seconds", "not-a-number"),
    ("inference_seconds", float("nan")),
    ("prefetch_seconds", float("inf")),
    ("inference_seconds", -5.0),
])
def test_malformed_job_telemetry_leaves_the_semantic_job_complete(tmp_path, field, value):
    """3 requested, 3 decoded, 3 returned for exactly the requested ids: that is complete.

    None of the completion inputs is a duration, so a malformed duration cannot change the verdict -
    and must not, or a fully tagged source is re-analysed forever because of an observability field.
    """
    record, stats, _ = _run_batch(tmp_path, timing={field: value})
    assert record["ai_enabled"] is True
    assert record["ai_deferred"] is False
    assert stats["qwen_completed_jobs"] == 1
    assert stats["qwen_incomplete_jobs"] == 0
    assert stats["qwen_tag_count"] == 3
    assert os.path.exists(str(tmp_path / "a.json")), "a complete job must still be checkpointable"


@pytest.mark.parametrize("field,value", [
    ("batch_size", "bad"), ("batch_size", float("nan")),
    ("peak_vram_gb", "bad"), ("peak_vram_gb", [3]),
    ("model_load_seconds", "bad"), ("model_id", {"a": 1}),
])
def test_malformed_response_metadata_leaves_the_semantic_job_complete(tmp_path, field, value):
    record, stats, _ = _run_batch(tmp_path, top={field: value})
    assert record["ai_enabled"] is True
    assert stats["qwen_completed_jobs"] == 1
    assert os.path.exists(str(tmp_path / "a.json"))


def test_the_checkpointed_record_carries_no_malformed_telemetry(tmp_path):
    """Sanitising happens before the record is written, so nothing malformed reaches the cache."""
    _run_batch(tmp_path, timing={"inference_seconds": float("nan")},
               top={"peak_vram_gb": "bad", "batch_size": "bad", "model_id": 7})
    with open(str(tmp_path / "a.json"), encoding="utf-8") as handle:
        text = handle.read()
    assert "NaN" not in text and "Infinity" not in text
    stored = json.loads(text)["timings"]
    assert stored["qwen_inference_seconds"] == 0.0
    assert stored["qwen_peak_vram_gb"] is None
    assert stored["qwen_concurrency"] == 0
    assert stored["qwen_model_id"] == ""
    assert math.isfinite(stored["qwen_seconds"])


def test_malformed_counts_still_leave_a_job_incomplete(tmp_path):
    """Counts ARE load-bearing. T1 must not have made the completion rule more permissive."""
    for value in ("bad", True, 2.9, None):
        record, stats, _ = _run_batch(tmp_path / f"{value}", timing={"frame_count": value})
        assert record["ai_enabled"] is False
        assert stats["qwen_incomplete_jobs"] == 1
        assert stats["qwen_completed_jobs"] == 0


def test_a_negative_frame_count_is_incomplete_and_uncounted(tmp_path):
    record, stats, _ = _run_batch(tmp_path, timing={"frame_count": -5})
    assert record["ai_enabled"] is False
    assert stats["qwen_frame_count"] == 0


# ============================================================== 5. CURRENT-RUN TRUTH (H)
@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_current_run_inference_seconds_is_never_contaminated(tmp_path, value):
    _, stats, _ = _run_batch(tmp_path, timing={"inference_seconds": value})
    assert stats["qwen_inference_seconds"] == 0.0
    assert math.isfinite(stats["qwen_inference_seconds"])
    assert stats["qwen_inference_seconds"] >= 0.0


def test_a_valid_inference_time_still_counts(tmp_path):
    _, stats, _ = _run_batch(tmp_path, timing={"inference_seconds": 4.0})
    assert stats["qwen_inference_seconds"] == pytest.approx(4.0)


@pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (2, 2), (3, 3), (4, 0), (10 ** 40, 0)])
def test_current_run_frame_count_respects_its_natural_bound(tmp_path, value, expected):
    """0 <= decoded <= requested. 4 decoded frames from a 3-candidate job is not evidence."""
    _, stats, _ = _run_batch(tmp_path, timing={"frame_count": value})
    assert stats["qwen_frame_count"] == expected


def test_current_run_wall_time_stays_parent_measured(tmp_path):
    """Never worker supplied, so it cannot be poisoned at all - only counted once per invocation."""
    _, stats, _ = _run_batch(tmp_path, timing={"inference_seconds": float("inf")},
                             top={"model_load_seconds": float("nan")})
    assert stats["qwen_seconds"] > 0.0
    assert math.isfinite(stats["qwen_seconds"])
    assert stats["qwen_jobs"] == 1
    assert stats["qwen_requested_count"] == 3


def test_current_run_tag_count_is_parent_derived_not_worker_reported(tmp_path):
    """The worker claims 99 tags; only 3 semantics were actually merged."""
    _, stats, _ = _run_batch(tmp_path, timing={"tag_count": 99})
    assert stats["qwen_tag_count"] == 3


def test_all_current_run_floats_are_finite_and_non_negative(tmp_path):
    _, stats, _ = _run_batch(
        tmp_path,
        timing={"inference_seconds": float("-inf"), "prefetch_seconds": float("nan")},
        top={"model_load_seconds": float("inf"), "peak_vram_gb": float("nan")})
    for key in ("qwen_seconds", "qwen_inference_seconds"):
        assert math.isfinite(stats[key]), key
        assert stats[key] >= 0.0, key


# ============================================================== 6. SINGLE / INLINE PATH
@pytest.mark.parametrize("value", NOT_A_COUNT_VALUES, ids=NOT_A_COUNT_IDS)
def test_inline_malformed_batch_size_does_not_destroy_completion(value):
    """This raised *inside* the facade, and both callers wrap it in `except Exception` - so a
    malformed `batch_size` silently turned a fully tagged source into `ai_enabled=False`."""
    info, stats, candidates, scope = _run_single(top={"batch_size": value})
    assert info[scope["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_concurrency"] == 0
    assert all(c.get("ai_analyzed") for c in candidates)
    assert stats["qwen_completed_jobs"] == 1


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_inline_malformed_peak_vram_does_not_destroy_completion(value):
    info, _, _, scope = _run_single(top={"peak_vram_gb": value})
    assert info[scope["_QWEN_COMPLETED_KEY"]] is True
    assert info["qwen_peak_vram_gb"] is None


@pytest.mark.parametrize("value", [123, {"a": 1}, ["m"], 2.5, True])
def test_inline_non_string_model_id(value):
    info, _, _, scope = _run_single(top={"model_id": value})
    assert info["qwen_model_id"] == ""
    assert info[scope["_QWEN_COMPLETED_KEY"]] is True


@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_inline_current_run_inference_seconds(value):
    _, stats, _, _ = _run_single(timing={"inference_seconds": value})
    assert stats["qwen_inference_seconds"] == 0.0
    assert math.isfinite(stats["qwen_inference_seconds"])


@pytest.mark.parametrize("value,expected", [(-5, 0), (0, 0), (3, 3), (4, 0)])
def test_inline_current_run_frame_count_bound(value, expected):
    _, stats, _, _ = _run_single(timing={"frame_count": value})
    assert stats["qwen_frame_count"] == expected


def test_inline_counts_keep_their_stored_reporting_semantics():
    """`_reported_count` still governs the *stored* pair - D1 behaviour, untouched by T1."""
    info, _, _, _ = _run_single(timing={"frame_count": 0})
    assert info["qwen_frame_count"] == 0, "a reported zero stays zero"
    info, _, _, _ = _run_single(response={"timings_by_job": {}, "semantics": {}})
    assert info["qwen_frame_count"] == 3, "absent -> the caller's requested-count default"


# ============================================================== 7. NESTED CONTAINERS (K)
@pytest.mark.parametrize("container", ["a-string", [1, 2, 3], 7, 2.5, True, ("a",)])
def test_a_truthy_non_dict_semantics_container_degrades_to_no_semantics(tmp_path, container):
    """It slipped past the empty-response branch and then raised `AttributeError` on `.get`.

    The right outcome is the no-usable-semantics one: the job was attempted, it is incomplete,
    nothing is invented, and Stage 5 survives.
    """
    response = _batch_response()
    response["semantics_by_job"] = container
    record, stats, _ = _run_batch(tmp_path, response=response)
    assert record["ai_enabled"] is False
    assert record["ai_deferred"] is False
    assert stats["qwen_jobs"] == 1
    assert stats["qwen_requested_count"] == 3
    assert stats["qwen_incomplete_jobs"] == 1
    assert stats["qwen_completed_jobs"] == 0
    assert stats["qwen_tag_count"] == 0
    assert not os.path.exists(str(tmp_path / "a.json")), "an incomplete job is not checkpointed"


@pytest.mark.parametrize("container", ["a-string", [1, 2, 3], 7, None])
def test_a_truthy_non_dict_timings_container_does_not_raise(tmp_path, container):
    response = _batch_response()
    response["timings_by_job"] = container
    record, stats, _ = _run_batch(tmp_path, response=response)
    assert record["ai_enabled"] is False, "no timing evidence -> not complete"
    assert math.isfinite(record["timings"]["qwen_seconds"])
    assert stats["qwen_jobs"] == 1


@pytest.mark.parametrize("container", ["a-string", [1], 7])
def test_a_per_job_timing_that_is_not_a_dict_does_not_raise(tmp_path, container):
    response = _batch_response()
    response["timings_by_job"] = {"1": container}
    record, _, _ = _run_batch(tmp_path, response=response)
    assert record["ai_enabled"] is False
    assert math.isfinite(record["timings"]["qwen_seconds"])


@pytest.mark.parametrize("container", ["a-string", [1], 7, None])
def test_inline_nested_containers_are_normalised(container):
    info, stats, _, scope = _run_single(
        response={"timings_by_job": container, "semantics": container})
    assert info[scope["_QWEN_COMPLETED_KEY"]] is False
    assert stats["qwen_jobs"] == 1
    assert stats["qwen_tag_count"] == 0


# ============================================================== 8. LIBRARY AGGREGATION (I, J, 11, 12)
def test_the_healthy_aggregation_is_unchanged():
    result = _aggregate([_record(), _record()])
    assert result["qwen_tag_count"] == 6
    assert result["qwen_frame_count"] == 6
    assert result["qwen_seconds"] == pytest.approx(12.0)
    assert result["qwen_inference_seconds"] == pytest.approx(10.0)
    assert result["qwen_model_id"] == "qwen3vl-2b"
    assert result["qwen_concurrency"] == 4
    assert result["qwen_peak_vram_gb"] == pytest.approx(3.0)


@pytest.mark.parametrize("field", ["qwen_tag_count", "qwen_frame_count", "qwen_seconds",
                                   "qwen_inference_seconds", "qwen_concurrency",
                                   "qwen_peak_vram_gb", "qwen_model_id"])
@pytest.mark.parametrize("value", MALFORMED_VALUES, ids=MALFORMED_IDS)
def test_no_malformed_library_telemetry_can_raise(field, value):
    result = _aggregate([_record(), _record(**{field: value})])
    assert math.isfinite(result["qwen_seconds"])
    assert math.isfinite(result["qwen_inference_seconds"])
    assert math.isfinite(result["qwen_peak_vram_gb"])
    assert isinstance(result["qwen_tag_count"], int)
    assert isinstance(result["qwen_frame_count"], int)
    assert isinstance(result["qwen_concurrency"], int)
    assert isinstance(result["qwen_model_id"], str)


@pytest.mark.parametrize("field", ["qwen_tag_count", "qwen_frame_count", "qwen_seconds",
                                   "qwen_inference_seconds", "qwen_concurrency",
                                   "qwen_peak_vram_gb"])
@pytest.mark.parametrize("value", ["bad", float("nan"), float("inf"), [1], None, -4.0])
def test_malformed_record_order_does_not_change_the_outcome(field, value):
    """`next(...)` converted only up to the first truthy record, so whether Stage 5 crashed used to
    depend on where a malformed source happened to sort in `videos`."""
    bad_first = _aggregate([_record(**{field: value}), _record()])
    bad_last = _aggregate([_record(), _record(**{field: value})])
    assert bad_first == bad_last


@pytest.mark.parametrize("value", [123, {"a": 1}, ["m"], None, "", True, 2.5])
def test_an_invalid_model_id_is_order_independent(value):
    """Kept separate from the matrix above because a non-empty *string* is a legitimate model id.

    Which valid id wins is first-come and therefore order-sensitive - it always was, and that is not a
    defect. What must be order-independent is whether an *invalid* one is skipped.
    """
    assert _aggregate([_record(qwen_model_id=value), _record()]) == \
           _aggregate([_record(), _record(qwen_model_id=value)])


@pytest.mark.parametrize("container", ["nope", 7, [1], None, True])
def test_a_non_dict_timings_payload_does_not_raise_the_aggregation(container):
    result = _aggregate([_record(), {"timings": container, "candidates": []}])
    assert result["qwen_tag_count"] == 3
    assert math.isfinite(result["qwen_seconds"])


def test_records_with_no_timings_at_all_are_tolerated():
    result = _aggregate([_record(), {"candidates": []}, {}])
    assert result["qwen_tag_count"] == 3
    assert math.isfinite(result["qwen_seconds"])


def test_the_nan_poison_pill_cannot_reach_a_library_total():
    """One cached NaN used to make the whole library total NaN, silently, on every warm run."""
    result = _aggregate([_record(), _record(qwen_seconds=float("nan")),
                         _record(qwen_inference_seconds=float("nan"))])
    assert math.isfinite(result["qwen_seconds"])
    assert math.isfinite(result["qwen_inference_seconds"])
    assert result["qwen_seconds"] == pytest.approx(12.0), "the two healthy records still count"


def test_infinity_cannot_reach_a_library_total():
    result = _aggregate([_record(), _record(qwen_seconds=float("inf"),
                                           qwen_peak_vram_gb=float("inf"))])
    assert math.isfinite(result["qwen_seconds"])
    assert math.isfinite(result["qwen_peak_vram_gb"])
    assert result["qwen_peak_vram_gb"] == pytest.approx(3.0)


def test_a_negative_duration_is_never_silently_subtracted():
    result = _aggregate([_record(), _record(qwen_seconds=-1000.0)])
    assert result["qwen_seconds"] == pytest.approx(6.0)


def test_an_aggregate_of_finite_but_overflowing_durations_stays_finite():
    result = _aggregate([_record(qwen_seconds=1e308), _record(qwen_seconds=1e308),
                         _record(qwen_seconds=1e308)])
    assert math.isfinite(result["qwen_seconds"])


def test_invalid_selected_metadata_is_skipped_for_a_valid_sibling():
    result = _aggregate([_record(qwen_model_id=123, qwen_concurrency="bad",
                                qwen_peak_vram_gb="bad"), _record()])
    assert result["qwen_model_id"] == "qwen3vl-2b"
    assert result["qwen_concurrency"] == 4
    assert result["qwen_peak_vram_gb"] == pytest.approx(3.0)


def test_with_no_valid_sibling_the_neutral_representation_is_used():
    result = _aggregate([_record(qwen_model_id=123, qwen_concurrency="bad",
                                qwen_peak_vram_gb="bad")])
    assert result["qwen_model_id"] == ""
    assert result["qwen_concurrency"] == 0
    assert result["qwen_peak_vram_gb"] == 0.0


def test_an_unknown_vram_does_not_beat_a_known_one():
    """`None` is what a sanitised-unknown record stores, so it must not win or break `max`."""
    result = _aggregate([_record(qwen_peak_vram_gb=None), _record(qwen_peak_vram_gb=2.0)])
    assert result["qwen_peak_vram_gb"] == pytest.approx(2.0)
    result = _aggregate([_record(qwen_peak_vram_gb=None)])
    assert result["qwen_peak_vram_gb"] == 0.0


def test_library_counts_respect_the_records_own_candidate_bound():
    """A hand-edited huge count cannot inflate a library total past what the record holds."""
    result = _aggregate([_record(candidates=3, qwen_tag_count=10 ** 9,
                                qwen_frame_count=10 ** 9)])
    assert result["qwen_tag_count"] == 0
    assert result["qwen_frame_count"] == 0


def test_a_candidate_less_record_cannot_contribute_counts():
    result = _aggregate([_record(), {"candidates": [],
                                     "timings": {"qwen_tag_count": 5000,
                                                 "qwen_frame_count": 5000}}])
    assert result["qwen_tag_count"] == 3
    assert result["qwen_frame_count"] == 3


def test_a_legitimate_zero_library_total_stays_zero():
    result = _aggregate([_record(qwen_tag_count=0, qwen_frame_count=0, qwen_seconds=0.0,
                                qwen_inference_seconds=0.0, qwen_peak_vram_gb=0.0)])
    assert result["qwen_tag_count"] == 0
    assert result["qwen_seconds"] == 0.0
    assert result["qwen_peak_vram_gb"] == 0.0


# ============================================================== 9. CACHE REUSE POISON PILL (J)
def _consistent_record(ns, **telemetry):
    """A record that genuinely satisfies the completion contract, carrying optional telemetry."""
    timings = {"candidate_scoring_seconds": 1.0, "total_seconds": 10.0,
               "qwen_frame_count": 3, "qwen_tag_count": 3,
               "qwen_seconds": 6.0, "qwen_inference_seconds": 5.0,
               "qwen_peak_vram_gb": 3.0, "qwen_concurrency": 4,
               "qwen_model_id": "qwen3vl-2b"}
    timings.update(telemetry)
    return {
        "analysis_version": ns["ANALYSIS_VERSION"],
        "cache_contract": ns["CACHE_CONTRACT_VERSION"],
        "video_file": r"C:\src\a.mp4",
        "candidates": [{"id": f"a.mp4-{i}", "ai_analyzed": True} for i in range(3)],
        "ai_enabled": True, "ai_deferred": False,
        "timings": timings,
    }


@pytest.mark.parametrize("field,value", [
    ("qwen_seconds", "bad"),
    ("qwen_seconds", float("nan")),
    ("qwen_seconds", float("inf")),
    ("qwen_seconds", -4.0),
    ("qwen_inference_seconds", "bad"),
    ("qwen_inference_seconds", float("nan")),
    ("qwen_peak_vram_gb", "bad"),
    ("qwen_peak_vram_gb", float("nan")),
    ("qwen_concurrency", "bad"),
    ("qwen_concurrency", float("nan")),
    ("qwen_model_id", 123),
])
def test_a_reusable_record_with_malformed_telemetry_is_still_reusable_and_safe(ns, tmp_path,
                                                                              field, value):
    """The poison pill: such a record passes every completion rule, so nothing ever recomputes it.

    Before T1 it was written, reloaded as an AI-complete hit, and then crashed or silently poisoned
    the aggregation on **every** subsequent warm run. Old cache files are never rewritten, so the
    aggregation is where this has to be survivable.
    """
    data = _consistent_record(ns, **{field: value})
    path = str(tmp_path / "hit.json")
    assert ns["_cache_entry_is_complete"](data, True) is True
    assert ns["_stored_ai_cache_is_consistent"](data) is True
    assert ns["_checkpoint_cache"](path, data, require_ai=True) is True
    loaded = ns["_load_cache"](path, require_ai=True, expected_video_file=r"C:\src\a.mp4")
    assert loaded is not None, "still a reusable cache hit - T1 rejects nothing the loader accepted"
    result = _aggregate([loaded])
    assert math.isfinite(result["qwen_seconds"])
    assert math.isfinite(result["qwen_inference_seconds"])
    assert math.isfinite(result["qwen_peak_vram_gb"])
    assert result["qwen_seconds"] >= 0.0


def test_a_healthy_cache_entry_round_trips_byte_identically(ns, tmp_path):
    """No migration, no rewrite: a valid record is stored exactly as it was handed over."""
    data = _consistent_record(ns)
    path = str(tmp_path / "hit.json")
    assert ns["_checkpoint_cache"](path, data, require_ai=True) is True
    with open(path, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored == data
    result = _aggregate([stored])
    assert result["qwen_seconds"] == pytest.approx(6.0)
    assert result["qwen_tag_count"] == 3


def test_poison_pill_order_does_not_matter(ns, tmp_path):
    bad = _consistent_record(ns, qwen_seconds=float("nan"), qwen_concurrency="bad")
    good = _consistent_record(ns)
    assert _aggregate([bad, good]) == _aggregate([good, bad])
    assert math.isfinite(_aggregate([bad, good])["qwen_seconds"])


# ================================================ 9b. FINITE PARTS, OVERFLOWING SUMS (R2)
#
# R1 validated every telemetry scalar individually and made the *final library aggregate*
# overflow-safe, then claimed newly written records carry no non-finite telemetry. Review found two
# earlier sums still using raw floating-point addition, so that claim did not hold. Both are
# reproduced here as behaviour, against the real extracted bodies:
#
#   1e308 (valid) + 1e308 (valid) -> inf
#
# Measured on the R1 head before this fix: the per-job case wrote `"qwen_seconds": Infinity` into a
# checkpointed record whose semantics were *complete*, so the source stayed reusable while carrying
# precisely the value R1 existed to exclude; and the current-run totals reached `inf` in both the
# batch and the inline path. Validating the parts is necessary but not sufficient - every telemetry
# sum has to preserve finiteness too.
_OVERFLOW = 1e308


def test_per_job_sum_of_two_valid_durations_cannot_overflow_the_stored_record(tmp_path):
    """A. prefetch and inference are each individually valid; their sum is not representable."""
    record, stats, scope = _run_batch(
        tmp_path, timing={"prefetch_seconds": _OVERFLOW, "inference_seconds": _OVERFLOW})
    timings = record["timings"]
    # each part was accepted on its own merits - the inputs really were valid
    assert timings["qwen_prefetch_seconds"] == _OVERFLOW
    assert timings["qwen_inference_seconds"] == _OVERFLOW
    # ...and the sum is still a finite, non-negative number rather than `inf`
    assert math.isfinite(timings["qwen_seconds"])
    assert timings["qwen_seconds"] >= 0.0
    # completion is decided without consulting any duration, so it is untouched
    assert record["ai_enabled"] is True
    assert stats["qwen_completed_jobs"] == 1
    # and the record is still durable
    assert os.path.exists(str(tmp_path / "a.json"))


def test_the_checkpointed_json_from_an_overflowing_sum_has_no_infinity(tmp_path):
    """The R1-head failure was visible on disk as `"qwen_seconds": Infinity`."""
    _run_batch(tmp_path, timing={"prefetch_seconds": _OVERFLOW, "inference_seconds": _OVERFLOW},
               top={"model_load_seconds": _OVERFLOW})
    path = str(tmp_path / "a.json")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    assert "Infinity" not in text
    assert "NaN" not in text
    stored = json.loads(text)["timings"]
    for key in ("qwen_seconds", "qwen_prefetch_seconds", "qwen_inference_seconds",
                "qwen_model_load_seconds_amortized", "total_seconds"):
        assert math.isfinite(stored[key]), key
        assert stored[key] >= 0.0, key


def test_an_overflowing_per_job_sum_leaves_the_record_reusable(ns, tmp_path):
    """The whole point: a complete job stays cacheable, and what it stores is readable next run."""
    record, _, scope = _run_batch(
        tmp_path, timing={"prefetch_seconds": _OVERFLOW, "inference_seconds": _OVERFLOW})
    with open(str(tmp_path / "a.json"), encoding="utf-8") as handle:
        stored = json.load(handle)
    assert scope["_cache_entry_is_complete"](stored, True) is True
    result = _aggregate([stored])
    assert math.isfinite(result["qwen_seconds"])
    assert math.isfinite(result["qwen_inference_seconds"])


@pytest.mark.parametrize("model_load", [8.0, _OVERFLOW])
def test_an_overflowing_amortized_model_share_stays_finite(tmp_path, model_load):
    record, _, _ = _run_batch(tmp_path, timing={"inference_seconds": _OVERFLOW},
                              top={"model_load_seconds": model_load})
    assert math.isfinite(record["timings"]["qwen_seconds"])
    assert math.isfinite(record["timings"]["qwen_model_load_seconds_amortized"])


def test_current_run_batch_inference_total_cannot_overflow(tmp_path):
    """B. two submitted jobs, each reporting an individually valid duration."""
    frames = 3
    response = {
        "semantics_by_job": {"1": _semantics("a.mp4", frames), "2": _semantics("b.mp4", frames)},
        "timings_by_job": {
            job: {"frame_count": frames, "tag_count": frames,
                  "prefetch_seconds": 0.1, "inference_seconds": _OVERFLOW}
            for job in ("1", "2")},
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b",
        "batch_size": 4, "peak_vram_gb": 3.0,
    }
    scope = _namespace(_run_qwen_worker_batch=lambda **kwargs: response,
                       _env_int=lambda name, default, lo=None, hi=None: default)
    first, second = _deferred_record(scope, "a.mp4", n=frames), _deferred_record(scope, "b.mp4", n=frames)
    stats = _new_stats()
    scope["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": str(tmp_path / "a.json")}, first),
                     ({"index": 2, "cache_file": str(tmp_path / "b.json")}, second)],
        use_gpu=False, qwen_model_path="m.gguf",
        total_video_count=2, run_stats=stats)
    assert stats["qwen_jobs"] == 2
    assert stats["qwen_completed_jobs"] == 2
    assert math.isfinite(stats["qwen_inference_seconds"])
    assert stats["qwen_inference_seconds"] >= 0.0
    assert math.isfinite(stats["qwen_seconds"])


def test_current_run_inline_inference_total_cannot_overflow():
    """C. one `run_stats` shared across two real facade calls, as the serial path does."""
    response = _single_response(timing={"inference_seconds": _OVERFLOW})
    scope = _namespace(_run_qwen_worker=lambda **kwargs: response)
    stats = _new_stats()
    for _ in range(2):
        candidates = [{"id": f"c-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                       "beauty_score": 0.4, "quality_score": 0.6, "editorial_score": 0.9 - i * 0.1}
                      for i in range(3)]
        scope["_annotate_candidates_with_qwen"](
            video_file=r"C:\src\a.mp4", fps=25.0, candidates=candidates,
            qwen_model_path="m.gguf", use_gpu=False, run_stats=stats)
    assert stats["qwen_jobs"] == 2
    assert math.isfinite(stats["qwen_inference_seconds"])
    assert stats["qwen_inference_seconds"] >= 0.0


def test_current_run_wall_time_is_still_parent_measured_and_counted_once(tmp_path):
    """D. R2 routes this through `_telemetry_total`; the semantics must not move."""
    _, stats, _ = _run_batch(tmp_path, timing={"inference_seconds": _OVERFLOW,
                                               "prefetch_seconds": _OVERFLOW})
    assert stats["qwen_seconds"] > 0.0, "a real measured duration, not zeroed"
    assert math.isfinite(stats["qwen_seconds"])
    assert stats["qwen_seconds"] < 60.0, "one stubbed invocation, not a worker-supplied 1e308"
    assert stats["qwen_jobs"] == 1


def test_inline_wall_time_accumulates_once_per_invocation():
    response = _single_response()
    scope = _namespace(_run_qwen_worker=lambda **kwargs: response)
    stats = _new_stats()
    seen = []
    for _ in range(3):
        candidates = [{"id": f"c-{i}", "start": i, "end": i + 2, "action_score": 0.5,
                       "beauty_score": 0.4, "quality_score": 0.6, "editorial_score": 0.9 - i * 0.1}
                      for i in range(3)]
        scope["_annotate_candidates_with_qwen"](
            video_file=r"C:\src\a.mp4", fps=25.0, candidates=candidates,
            qwen_model_path="m.gguf", use_gpu=False, run_stats=stats)
        seen.append(stats["qwen_seconds"])
    assert stats["qwen_jobs"] == 3
    assert seen == sorted(seen), "monotonically accumulating, one measurement per invocation"
    assert math.isfinite(stats["qwen_seconds"]) and stats["qwen_seconds"] > 0.0


def test_healthy_telemetry_sums_are_unchanged_by_the_overflow_fix(tmp_path):
    """E. 1.0 prefetch + 4.0 inference + 2.0 model share is still exactly 7.0."""
    record, stats, _ = _run_batch(
        tmp_path, timing={"prefetch_seconds": 1.0, "inference_seconds": 4.0},
        top={"model_load_seconds": 2.0})
    timings = record["timings"]
    assert timings["qwen_prefetch_seconds"] == pytest.approx(1.0)
    assert timings["qwen_inference_seconds"] == pytest.approx(4.0)
    assert timings["qwen_model_load_seconds_amortized"] == pytest.approx(2.0)
    assert timings["qwen_seconds"] == pytest.approx(7.0)
    assert stats["qwen_inference_seconds"] == pytest.approx(4.0)


def test_healthy_multi_job_inference_totals_still_add_up(tmp_path):
    """Overflow safety must not have turned accumulation into something lossy."""
    frames = 3
    response = {
        "semantics_by_job": {"1": _semantics("a.mp4", frames), "2": _semantics("b.mp4", frames)},
        "timings_by_job": {
            "1": {"frame_count": frames, "tag_count": frames,
                  "prefetch_seconds": 0.1, "inference_seconds": 4.0},
            "2": {"frame_count": frames, "tag_count": frames,
                  "prefetch_seconds": 0.1, "inference_seconds": 6.0}},
        "model_load_seconds": 8.0, "model_id": "qwen3vl-2b",
        "batch_size": 4, "peak_vram_gb": 3.0,
    }
    scope = _namespace(_run_qwen_worker_batch=lambda **kwargs: response,
                       _env_int=lambda name, default, lo=None, hi=None: default)
    first, second = _deferred_record(scope, "a.mp4", n=frames), _deferred_record(scope, "b.mp4", n=frames)
    stats = _new_stats()
    scope["_complete_deferred_qwen_batch"](
        video_items=[({"index": 1, "cache_file": str(tmp_path / "a.json")}, first),
                     ({"index": 2, "cache_file": str(tmp_path / "b.json")}, second)],
        use_gpu=False, qwen_model_path="m.gguf",
        total_video_count=2, run_stats=stats)
    assert stats["qwen_inference_seconds"] == pytest.approx(10.0)
    assert first["timings"]["qwen_seconds"] == pytest.approx(0.1 + 4.0 + 4.0)
    assert second["timings"]["qwen_seconds"] == pytest.approx(0.1 + 6.0 + 4.0)


def test_a_legitimate_zero_sum_is_still_zero(tmp_path):
    record, stats, _ = _run_batch(
        tmp_path, timing={"prefetch_seconds": 0.0, "inference_seconds": 0.0},
        top={"model_load_seconds": 0.0})
    assert record["timings"]["qwen_seconds"] == 0.0
    assert stats["qwen_inference_seconds"] == 0.0


def test_every_telemetry_sum_in_orchestration_goes_through_the_safe_aggregator():
    """Structural guard: no raw `+`/`+=` may reappear on a telemetry quantity in these bodies.

    Behavioural tests catch today's sites; this catches a *new* one being added later, which is how
    the R1 gap survived its own review.
    """
    tree = _tree()
    guarded = {"qwen_seconds", "qwen_inference_seconds"}
    for name in ("_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        func = _func(tree, name)
        for node in ast.walk(func):
            # run_stats["<telemetry>"] += ...  is no longer allowed for float telemetry
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Subscript) \
                    and isinstance(node.target.slice, ast.Constant) \
                    and node.target.slice.value in guarded:
                raise AssertionError(
                    f"{name}: raw augmented assignment to {node.target.slice.value!r}")
        code = ast.unparse(func)
        assert "_telemetry_total" in code, f"{name} must use the safe aggregator"


def test_the_safe_aggregator_is_shared_not_duplicated():
    """R2 adds no new summation helper; it reuses the R1 one."""
    tree = _tree()
    summers = [n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and "total" in n.name and "telemetry" in n.name]
    assert summers == ["_telemetry_total"], f"expected exactly one telemetry summer, got {summers}"


# ============================================================== 10. CACHE ISOLATION (N)
def _names_used(func: ast.FunctionDef) -> set:
    return {node.id for node in ast.walk(func) if isinstance(node, ast.Name)}


@pytest.mark.parametrize("name", ["_video_signature", "_cache_path", "_qwen_backend_signature_token",
                                  "_qwen_config_token", "_path_signature_token"])
def test_no_telemetry_helper_enters_cache_identity(name):
    used = _names_used(_func(_tree(), name))
    leaked = used & set(_TELEMETRY_FUNCS)
    assert not leaked, f"{name} must not depend on telemetry sanitisation: {leaked}"


@pytest.mark.parametrize("name", ["_qwen_job_completed", "_stored_ai_cache_is_consistent",
                                  "_cache_entry_is_complete", "_deterministic_analysis_completed",
                                  "_checkpoint_cache", "_save_cache", "_load_cache"])
def test_the_completion_contract_does_not_consult_telemetry(name):
    """Completion is decided from the envelope, real integer counts and the id sets - nothing else."""
    used = _names_used(_func(_tree(), name))
    leaked = used & set(_TELEMETRY_FUNCS)
    assert not leaked, f"{name} must not read optional telemetry: {leaked}"


@pytest.mark.parametrize("name", ["_qwen_job_completed", "_stored_ai_cache_is_consistent",
                                  "_cache_entry_is_complete"])
def test_no_completion_rule_reads_a_duration_or_vram_field(name):
    """Textual on purpose: these field names must not appear in a completion body at all."""
    code = ast.unparse(_func(_tree(), name))
    for field in ("inference_seconds", "prefetch_seconds", "peak_vram_gb", "batch_size",
                  "model_load_seconds", "qwen_seconds", "qwen_model_id", "qwen_concurrency"):
        assert field not in code, f"{name} must not depend on {field}"


def test_no_current_run_accounting_reaches_a_cache_payload():
    """Re-pinned here because T1 touches both accounting sites."""
    tree = _tree()
    for name in ("_complete_deferred_qwen_batch", "_annotate_candidates_with_qwen"):
        func = _func(tree, name)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "_checkpoint_cache":
                rendered = " ".join(ast.unparse(a) for a in node.args) + " " + \
                           " ".join(ast.unparse(k.value) for k in node.keywords)
                assert "run_stats" not in rendered


def test_the_cache_generation_is_frozen():
    """T1 changes no cached meaning, so neither version constant may move."""
    tree = _tree()
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in (
                        "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION"):
                    found[target.id] = ast.literal_eval(node.value)
    assert found["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"
    assert found["ANALYSIS_VERSION"] == "auto_av_analysis_v8_llama_vulkan_batched"


def test_the_producer_side_worker_is_untouched_by_this_boundary():
    """T1 is consumer-side trust. The worker's own emitters are already well typed, so it is not
    edited - a test rather than a comment because that is the reason the fix is scoped this way."""
    worker = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "src", "auto_mode", "stage5_qwen_scene_worker.py")
    with open(worker, encoding="utf-8") as handle:
        code = handle.read()
    assert "max(1, min(32, int(slots)))" in code, "slots is clamped to a real positive int"
    assert '"peak_vram_gb": 0.0' in code, "vram is a literal 0.0, which is why 0.0 is legitimate"
    for helper in _TELEMETRY_FUNCS:
        assert helper not in code, "the parent's telemetry seam must not leak into the worker"
