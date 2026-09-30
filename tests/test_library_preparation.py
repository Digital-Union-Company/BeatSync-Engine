"""Media Library Preparation (P V1 + P2 media-neutral / trackless).

Two halves, tested two ways:

* ``beatsync_fork.library_prep`` is stdlib-only, so it is imported and exercised directly.
* ``video_analysis.classify_library_sources`` cannot be imported on a bare interpreter (the module
  needs cv2/numpy/ffmpeg), so — exactly like ``tests/test_stage5_cache_identity.py`` — this suite
  lifts the real definition and every helper it calls out of the production file by their ``ast``
  source ranges and executes them. What is asserted is therefore the production behaviour, not a
  description of it.

The GUI seam is checked structurally with ``ast`` for the same reason ``test_gui_guard_seam.py``
does it: ``gui.py`` needs Gradio and the whole runtime.

No Qwen model is loaded, no GPU is required, no audio is analysed, no video is rendered, and every
path lives inside pytest's ``tmp_path``. The real runtime cache is never touched.

P2 deliberately retires this suite's track expectations. Preparation used to require an audio file so
it could resolve ``smart_preset`` — the one ``audio_profile`` field that reached a cache key — and to
guard against that file changing between the two clicks. Persisted Stage-5 semantics are media-neutral
now, so there is no track control, no Stage 1–4 pass during a scan, no ``TrackIdentity`` and no edit
style anywhere in the workflow.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from typing import Any, Dict, Iterable, List, Sequence

import pytest

from beatsync_fork import library_prep as lp
from beatsync_fork import progress as fork_progress

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO, "src", "video_analysis.py")
_GUI = os.path.join(_REPO, "src", "gui.py")
_PREP = os.path.join(_REPO, "src", "beatsync_fork", "library_prep.py")

# Everything `classify_library_sources` reaches, transitively. Listed explicitly so a future edit
# that makes it call something new fails here with a clear NameError rather than silently drifting.
_FUNCS = (
    "_safe_name", "_hash_text", "_env_int", "_fmt_seconds", "_qwen_max_windows",
    "_path_signature_token", "_bounded_fingerprint", "_full_fingerprint",
    "_backend_component_token", "_llama_version_token", "_resolve_qwen_backend_paths",
    "_qwen_backend_available", "_qwen_backend_model_path", "_qwen_backend_signature_token",
    "_qwen_config_token", "_video_signature", "_cache_path",
    "_same_source", "_is_count", "_stored_ai_cache_is_consistent",
    "_deterministic_analysis_completed", "_cache_entry_is_complete", "_load_cache",
    "classify_library_sources",
)
_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_FINGERPRINT_CHUNK",
           "_FINGERPRINT_WHOLE_FILE_LIMIT", "_FINGERPRINT_DIGEST_SIZE", "_NO_AI_CONFIG_TOKEN",
           "_DETERMINISTIC_SCORING_KEY", "_PREP_PHASE")

_QWEN_ENV = ("BEATSYNC_QWEN_MAX_WINDOWS", "BEATSYNC_QWEN_FRAME_WIDTH",
             "BEATSYNC_QWEN_MAX_NEW_TOKENS")
_SECOND = 1_700_000_000_000_000_000


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _body_code(node: ast.FunctionDef) -> str:
    """Executable body only — docstrings stripped, so prose cannot satisfy an assertion."""
    return "\n".join(
        ast.unparse(item) for item in node.body
        if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str))
    )


def _calls_named(node: ast.AST, callee: str) -> list[ast.Call]:
    found = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == callee:
                found.append(inner)
    return found


def _load(root: str, cache_dir: str) -> Dict[str, Any]:
    """Exec the production classifier and its helpers in a stdlib-only namespace rooted at `root`."""
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    models = os.path.join(root, "bin", "models")
    llama = os.path.join(root, "bin", "llama-bin-win-vulkan-x64")
    for directory in (models, llama, cache_dir):
        os.makedirs(directory, exist_ok=True)

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "hashlib": hashlib, "subprocess": subprocess, "time": __import__("time"),
        "Any": Any, "Dict": Dict, "Iterable": Iterable, "List": List, "Sequence": Sequence,
        "__builtins__": __builtins__,
        "ROOT_DIR": root,
        "DEFAULT_QWEN_MODEL_DIR": models,
        "DEFAULT_QWEN_GGUF_MODEL": os.path.join(models, "Qwen3VL-2B-Instruct-Q8_0.gguf"),
        "DEFAULT_QWEN_MMPROJ_MODEL": os.path.join(models, "mmproj-Qwen3VL-2B-Instruct-F16.gguf"),
        "DEFAULT_LLAMA_CPP_DIR": llama,
        "VIDEO_ANALYSIS_CACHE_DIR": cache_dir,
        "_LLAMA_VERSION_TOKENS": {},
        # The two fork modules are stdlib-only, so the REAL ones are injected rather than stubbed:
        # the status strings the classifier writes must be the ones the state module reads.
        "fork_progress": fork_progress,
        "fork_prep": lp,
    }

    found: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _CONSTS:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<const>", "exec"),
                         namespace)
    missing = [name for name in _FUNCS if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"
    for name in _CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"
    for name in _FUNCS:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    namespace["_MODELS"] = models
    namespace["_CACHE"] = cache_dir
    return namespace


def _write(path: str, byte: bytes, size: int, mtime_ns: int = _SECOND) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(byte * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return os.path.abspath(path)


@pytest.fixture(autouse=True)
def _clean_qwen_env():
    saved = {name: os.environ.get(name) for name in _QWEN_ENV}
    for name in _QWEN_ENV:
        os.environ.pop(name, None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def va(tmp_path):
    """A namespace with a complete, readable fake Qwen backend, so `ai_available` is True."""
    ns = _load(str(tmp_path / "root"), str(tmp_path / "cache"))
    chunk = ns["_FINGERPRINT_CHUNK"]
    for name in ("Qwen3VL-2B-Instruct-Q8_0.gguf", "mmproj-Qwen3VL-2B-Instruct-F16.gguf"):
        _write(os.path.join(ns["_MODELS"], name), b"A", 4 * chunk)
    llama = os.path.join(os.path.dirname(ns["_MODELS"]), "llama-bin-win-vulkan-x64")
    for name in ("llama-server.exe", "llama-mtmd-cli.exe"):
        _write(os.path.join(llama, name), b"B", 2048)
    return ns


def _source(tmp_path, name: str, byte: bytes = b"v", size: int = 4096,
            mtime_ns: int = _SECOND) -> str:
    return _write(os.path.join(str(tmp_path), "library", name), byte, size, mtime_ns)


def _complete_record(ns, video_file: str) -> Dict[str, Any]:
    """A payload that satisfies `_cache_entry_is_complete(require_ai=True)` in full."""
    return {
        "analysis_version": ns["ANALYSIS_VERSION"],
        "cache_contract": ns["CACHE_CONTRACT_VERSION"],
        "video_file": video_file,
        "candidates": [
            {"id": "c1", "ai_analyzed": True},
            {"id": "c2", "ai_analyzed": True},
        ],
        "ai_enabled": True,
        "timings": {
            ns["_DETERMINISTIC_SCORING_KEY"]: 1.0,
            "qwen_frame_count": 2,
            "qwen_tag_count": 2,
        },
    }


def _put_record(ns, video_file: str, payload: Dict[str, Any]) -> str:
    """Write a payload at the source's CURRENT production cache key."""
    path = ns["_cache_path"](video_file, True, ns["DEFAULT_QWEN_MODEL_DIR"])
    assert path is not None
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def _classify(ns, sources, enable_ai=True, event_callback=None):
    return ns["classify_library_sources"](
        sources, enable_ai=enable_ai,
        qwen_model_path=ns["DEFAULT_QWEN_MODEL_DIR"], event_callback=event_callback)


def _verdicts(result) -> Dict[str, tuple]:
    return {item["path"]: (item["status"], item["reason"])
            for item in result["classifications"]}


PREPARED = (lp.PrepStatus.PREPARED.value, "")
NEW_OR_CHANGED = (lp.PrepStatus.NEEDS_ANALYSIS.value, lp.NeedReason.NEW_OR_CHANGED.value)
INCOMPLETE = (lp.PrepStatus.NEEDS_ANALYSIS.value, lp.NeedReason.INCOMPLETE_OR_INVALID.value)
UNAVAILABLE = (lp.PrepStatus.SOURCE_IDENTITY_UNAVAILABLE.value, "")


# ===========================================================================
# A. CLASSIFICATION TRUTH
# ===========================================================================


def test_a_complete_same_path_record_is_prepared(va, tmp_path):
    source = _source(tmp_path, "warm.mp4")
    _put_record(va, source, _complete_record(va, source))

    assert _verdicts(_classify(va, [source]))[source] == PREPARED


def test_a_source_with_no_record_is_new_or_changed(va, tmp_path):
    source = _source(tmp_path, "cold.mp4")
    assert _verdicts(_classify(va, [source]))[source] == NEW_OR_CHANGED


def test_a_changed_same_path_file_re_keys_and_misses(va, tmp_path):
    """The whole point of D2 identity: same path, different content => a different key.

    Proven by construction rather than asserted — the record written for the original bytes is left
    in place, and the classifier is asked again after the file is rewritten.
    """
    source = _source(tmp_path, "edited.mp4")
    old_key = _put_record(va, source, _complete_record(va, source))
    assert _verdicts(_classify(va, [source]))[source] == PREPARED

    _write(source, b"w", 4096, mtime_ns=_SECOND + 5_000_000_000)
    new_key = va["_cache_path"](source, True, va["DEFAULT_QWEN_MODEL_DIR"])

    assert new_key != old_key, "a changed file must re-key"
    assert os.path.exists(old_key), "the old record is orphaned, never deleted"
    assert _verdicts(_classify(va, [source]))[source] == NEW_OR_CHANGED


def test_a_deferred_ai_record_is_incomplete_not_new(va, tmp_path):
    source = _source(tmp_path, "deferred.mp4")
    payload = _complete_record(va, source)
    payload["ai_deferred"] = True
    _put_record(va, source, payload)

    assert _verdicts(_classify(va, [source]))[source] == INCOMPLETE


def test_a_self_contradictory_ai_record_is_incomplete(va, tmp_path):
    """The real-cache shape D1 R6 found: 2 frames decoded, 1 tagged, claimed AI-complete."""
    source = _source(tmp_path, "inconsistent.mp4")
    payload = _complete_record(va, source)
    payload["timings"]["qwen_tag_count"] = 1
    _put_record(va, source, payload)

    assert _verdicts(_classify(va, [source]))[source] == INCOMPLETE


def test_a_record_for_a_different_source_is_incomplete(va, tmp_path):
    """`_load_cache`'s `expected_video_file` check must be reached by the classifier too."""
    source = _source(tmp_path, "mismatched.mp4")
    payload = _complete_record(va, source)
    payload["video_file"] = os.path.join(str(tmp_path), "library", "somebody_else.mp4")
    _put_record(va, source, payload)

    assert _verdicts(_classify(va, [source]))[source] == INCOMPLETE


def test_a_source_without_provable_identity_is_unavailable(va, tmp_path):
    """Stage 5 fails closed on an unstat-able source; preparation must report it, not hide it.

    Such a source is neither prepared nor analysable: `_cache_path` returns None, so
    `analyze_video_sources` would neither read nor write cache for it.
    """
    missing = os.path.join(str(tmp_path), "library", "vanished.mp4")
    result = _classify(va, [missing])

    assert _verdicts(result)[os.path.abspath(missing)] == UNAVAILABLE
    assert result["unavailable_count"] == 1
    assert missing not in lp.PrepScanResult(
        folder="f", recursive=True,
        runtime=lp.RuntimeIdentity(True, True, False),
        classifications=tuple(lp.SourceClassification.from_mapping(i)
                              for i in result["classifications"]),
    ).subset_for_analysis()


def test_a_mixed_library_counts_add_up(va, tmp_path):
    warm = [_source(tmp_path, f"warm_{i}.mp4", size=4096 + i) for i in range(8)]
    for path in warm:
        _put_record(va, path, _complete_record(va, path))
    fresh = _source(tmp_path, "fresh.mp4", size=9000)
    broken = _source(tmp_path, "broken.mp4", size=9100)
    payload = _complete_record(va, broken)
    payload["ai_deferred"] = True
    _put_record(va, broken, payload)

    result = _classify(va, warm + [fresh, broken])

    assert (result["prepared_count"], result["needs_analysis_count"],
            result["unavailable_count"]) == (8, 2, 0)
    assert result["source_count"] == 10


def test_a_no_ai_runtime_uses_the_existing_no_ai_identity(va, tmp_path):
    """A legitimately AI-disabled run is not a failure state — it classifies normally.

    Its key is the existing `no_ai` identity, so an AI-keyed record must not satisfy it and vice
    versa. No fake AI token is invented anywhere.
    """
    source = _source(tmp_path, "deterministic.mp4")
    _put_record(va, source, _complete_record(va, source))

    result = _classify(va, [source], enable_ai=False)

    assert result["ai_available"] is False
    assert result["ai_cache_disabled"] is False
    assert result["backend_token"] == "" and result["config_token"] == ""
    assert _verdicts(result)[source] == NEW_OR_CHANGED

    deterministic = {
        "analysis_version": va["ANALYSIS_VERSION"],
        "cache_contract": va["CACHE_CONTRACT_VERSION"],
        "video_file": source,
        "candidates": [{"id": "c1"}],
        "ai_enabled": False,
        "timings": {va["_DETERMINISTIC_SCORING_KEY"]: 1.0},
    }
    no_ai_key = va["_cache_path"](source, False, va["DEFAULT_QWEN_MODEL_DIR"])
    with open(no_ai_key, "w", encoding="utf-8") as handle:
        json.dump(deterministic, handle)

    assert _verdicts(_classify(va, [source], enable_ai=False))[source] == PREPARED


# ===========================================================================
# BACKEND IDENTITY FAILURE
# ===========================================================================


def test_unprovable_backend_identity_blocks_preparation(va, tmp_path, monkeypatch):
    """AI is available but its strong identity cannot be proven: Stage 5 would cache nothing.

    Preparation must refuse rather than burn GPU time on work that cannot persist. Per-source
    classification is skipped entirely, because no verdict could exist.
    """
    source = _source(tmp_path, "any.mp4")
    va["_qwen_backend_signature_token"] = lambda *_args, **_kwargs: None

    result = _classify(va, [source])

    assert result["ai_available"] is True
    assert result["ai_cache_disabled"] is True
    assert result["classifications"] == []
    assert result["cache_lookups"] == 0, "no per-source work may be attempted"

    runtime = lp.runtime_identity_from_classification(result, qwen_enabled=True)
    assert runtime.is_usable() is False
    scan = lp.build_scan_result(
        folder="F", recursive=True, runtime=runtime,
        classification_items=result["classifications"], supported_count=1)
    assert scan.can_analyze() is False
    assert lp.BACKEND_UNVERIFIED_TEXT in scan.render_text()
    assert lp.record_scan(lp.initial_state(), scan).can_analyze() is False


def test_a_no_ai_run_is_not_treated_as_a_backend_failure(va, tmp_path):
    """The two states must stay distinct: `no_ai` caches fine, unprovable-AI does not."""
    source = _source(tmp_path, "any.mp4")
    result = _classify(va, [source], enable_ai=False)
    runtime = lp.runtime_identity_from_classification(result, qwen_enabled=False)

    assert runtime.ai_available is False
    assert runtime.ai_cache_disabled is False
    assert runtime.is_usable() is True


# ===========================================================================
# B. READ-ONLY SCAN
# ===========================================================================


def _record_snapshot(cache_dir: str) -> Dict[str, tuple]:
    """Every cache RECORD, by name, with its size, mtime_ns and exact bytes."""
    snapshot = {}
    for name in sorted(os.listdir(cache_dir)):
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        with open(path, "rb") as handle:
            snapshot[name] = (stat.st_size, stat.st_mtime_ns, handle.read())
    return snapshot


def test_b_classification_mutates_no_cache_record(va, tmp_path):
    warm = _source(tmp_path, "warm.mp4")
    _put_record(va, warm, _complete_record(va, warm))
    cold = _source(tmp_path, "cold.mp4", size=5000)
    broken = _source(tmp_path, "broken.mp4", size=5100)
    payload = _complete_record(va, broken)
    payload["ai_deferred"] = True
    _put_record(va, broken, payload)
    missing = os.path.join(str(tmp_path), "library", "gone.mp4")

    before = _record_snapshot(va["_CACHE"])
    _classify(va, [warm, cold, broken, missing])
    after = _record_snapshot(va["_CACHE"])

    assert before == after, "no record may be created, updated or removed by a scan"
    assert set(before) == {os.path.basename(p) for p in (
        va["_cache_path"](warm, True, va["DEFAULT_QWEN_MODEL_DIR"]),
        va["_cache_path"](broken, True, va["DEFAULT_QWEN_MODEL_DIR"]),
    )}


def test_b_classification_survives_a_missing_cache_directory(va, tmp_path):
    """`_cache_path`'s existing `os.makedirs` is the one permitted side effect, and it is required:
    the classifier must work on a machine that has never rendered."""
    import shutil

    shutil.rmtree(va["_CACHE"])
    assert not os.path.exists(va["_CACHE"])

    source = _source(tmp_path, "first_ever.mp4")
    assert _verdicts(_classify(va, [source]))[source] == NEW_OR_CHANGED
    assert os.path.isdir(va["_CACHE"])
    assert os.listdir(va["_CACHE"]) == [], "a directory, but never a record"


@pytest.mark.parametrize("forbidden", [
    "_save_cache", "_checkpoint_cache", "_analyze_single_video", "_measure_windows",
    "_complete_deferred_qwen", "_complete_deferred_qwen_batch", "_run_qwen_worker",
    "_run_qwen_worker_batch", "_build_candidate", "_annotate_candidates_with_qwen",
    "create_music_video", "os.replace", "json.dump",
])
def test_b_classifier_body_performs_no_write_and_no_analysis(forbidden):
    body = _body_code(_func(_tree(_VA), "classify_library_sources"))
    assert forbidden not in body, f"classify_library_sources must not reference {forbidden}"


def test_b_classifier_reuses_the_production_reuse_rule():
    """No second answer to "is this reusable?" — the loader, and therefore the one completion rule."""
    fn = _func(_tree(_VA), "classify_library_sources")
    body = _body_code(fn)

    assert _calls_named(fn, "_load_cache"), "reuse must be decided by _load_cache"
    assert "require_ai=ai_available" in body
    assert "expected_video_file=video_file" in body
    assert "_cache_entry_is_complete" not in body, "must not re-answer completion itself"
    assert "_video_signature" not in body, "identity comes through _cache_path"


# ===========================================================================
# C. INVOCATION-SCOPED IDENTITY
# ===========================================================================


def test_c_backend_and_config_identity_are_computed_once_per_scan(va, tmp_path):
    """Behavioural: N sources, one backend computation. Per source this was measured at 61.7
    minutes for a 702-source library once content fingerprints entered identity."""
    calls = {"backend": 0, "config": 0}
    real_backend = va["_qwen_backend_signature_token"]
    real_config = va["_qwen_config_token"]

    def counting_backend(*args, **kwargs):
        calls["backend"] += 1
        return real_backend(*args, **kwargs)

    def counting_config(*args, **kwargs):
        calls["config"] += 1
        return real_config(*args, **kwargs)

    va["_qwen_backend_signature_token"] = counting_backend
    va["_qwen_config_token"] = counting_config

    sources = [_source(tmp_path, f"s{i}.mp4", size=4096 + i) for i in range(6)]
    result = _classify(va, sources)

    assert result["source_count"] == 6
    assert calls == {"backend": 1, "config": 1}


def test_c_a_second_scan_recomputes_identity(va, tmp_path):
    """Invocation-scoped, never module-cached: a swapped model must be observable in-process."""
    calls = {"n": 0}
    real = va["_qwen_backend_signature_token"]

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    va["_qwen_backend_signature_token"] = counting
    source = _source(tmp_path, "s.mp4")
    _classify(va, [source])
    _classify(va, [source])

    assert calls["n"] == 2


def test_c_every_cache_path_call_receives_the_invocation_tokens():
    fn = _func(_tree(_VA), "classify_library_sources")
    calls = _calls_named(fn, "_cache_path")
    assert calls, "the classifier must use _cache_path"
    for call in calls:
        rendered = ast.unparse(call)
        assert "backend_token=invocation_backend_token" in rendered, rendered
        assert "config_token=invocation_config_token" in rendered, rendered
        assert "audio_profile" not in rendered, (
            "P2: nothing about the music may reach the key formula")

    assert len(_calls_named(fn, "_qwen_backend_signature_token")) == 1
    assert len(_calls_named(fn, "_qwen_config_token")) == 1


def test_c_the_orchestrator_identity_structure_is_untouched():
    """This PR must not refactor `analyze_video_sources`' own once-per-invocation block, which its
    existing suites assert in place."""
    orchestrator = _func(_tree(_VA), "analyze_video_sources")
    assert len(_calls_named(orchestrator, "_qwen_backend_signature_token")) == 1
    assert len(_calls_named(orchestrator, "_qwen_config_token")) == 1
    assert "ai_cache_disabled" in _body_code(orchestrator)


def test_c_the_classifier_has_no_edit_style_input_at_all(va, tmp_path):
    """The P2 inverse of the P V1 expectation this replaces.

    Preparation used to re-key the whole library per ``smart_preset``, so a scan had to bind the
    resolved edit style. A prepared source is now prepared for *every* track and every preset,
    because the classifier has no way to be told about one: there is no parameter, and repeated scans
    return the identical verdict.
    """
    source = _source(tmp_path, "styled.mp4")
    _put_record(va, source, _complete_record(va, source))

    fn = _func(_tree(_VA), "classify_library_sources")
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    assert "audio_profile" not in params, params
    for leaked in ("audio_profile", "smart_preset", "_qwen_prompt_style_hint"):
        assert leaked not in _body_code(fn), leaked

    assert _verdicts(_classify(va, [source]))[source] == PREPARED
    assert _verdicts(_classify(va, [source]))[source] == PREPARED


# ===========================================================================
# D. P.1 SUBSET
# ===========================================================================


def _scan_with(items, runtime=None) -> lp.PrepScanResult:
    return lp.build_scan_result(
        folder="F", recursive=True,
        runtime=runtime or lp.RuntimeIdentity(True, True, False, "b", "c", "m"),
        classification_items=items, supported_count=len(items))


def test_d_only_the_needs_analysis_subset_is_selected():
    items = [{"path": f"p{i}", "status": PREPARED[0], "reason": ""} for i in range(8)]
    items.append({"path": "new", "status": NEW_OR_CHANGED[0], "reason": NEW_OR_CHANGED[1]})
    items.append({"path": "bad", "status": INCOMPLETE[0], "reason": INCOMPLETE[1]})

    scan = _scan_with(items)

    assert scan.supported_count == 10
    assert scan.prepared_count == 8
    assert scan.needs_analysis_count == 2
    assert scan.subset_for_analysis() == ("new", "bad")
    assert scan.can_analyze() is True


def test_d_a_fully_prepared_library_offers_no_work():
    scan = _scan_with([{"path": f"p{i}", "status": PREPARED[0], "reason": ""} for i in range(902)])

    assert scan.subset_for_analysis() == ()
    assert scan.can_analyze() is False
    assert "Everything in this library is prepared." in scan.render_text()
    assert "edit style" not in scan.render_text(), "P2: no style dependence may be implied"
    assert lp.record_scan(lp.initial_state(), scan).can_analyze() is False


def test_d_the_analyze_handler_passes_the_subset_and_nothing_else():
    """Structural: the one call must take a batch of the classified subset, never the library."""
    body = _body_code(_func(_tree(_GUI), "_prep_analyze_impl"))

    assert "subset = list(scan.subset_for_analysis_batch(batch_size))" in body
    call = _calls_named(_func(_tree(_GUI), "_prep_analyze_impl"), "analyze_video_sources")
    assert len(call) == 1
    rendered = ast.unparse(call[0])
    assert "video_files=subset" in rendered, rendered
    for wrong in ("ready_paths", "input_set", "scan.classifications", "scan_folder"):
        assert wrong not in body, f"Analyze must not rediscover the library ({wrong})"


def test_d_the_report_never_lists_individual_files():
    scan = _scan_with([{"path": f"/lib/clip_{i}.mp4",
                        "status": NEW_OR_CHANGED[0], "reason": NEW_OR_CHANGED[1]}
                       for i in range(300)])
    text = scan.render_text()

    assert "clip_0.mp4" not in text
    assert "Ready to analyze 300 video(s)." in text
    assert len(text.splitlines()) < 25


# ===========================================================================
# E. ANALYSIS OWNERSHIP
# ===========================================================================


@pytest.mark.parametrize("forbidden", [
    "_analyze_single_video", "_checkpoint_cache", "_save_cache", "_complete_deferred_qwen",
    "_complete_deferred_qwen_batch", "_run_qwen_worker", "_cache_path", "_load_cache",
    "_video_signature", "_qwen_backend_signature_token",
])
@pytest.mark.parametrize("function", ["_prep_scan_impl", "_prep_analyze_impl"])
def test_e_preparation_never_owns_analysis_or_persistence(function, forbidden):
    """Everything durable is the existing analyzer's job; the GUI only chooses the inputs."""
    assert forbidden not in _body_code(_func(_tree(_GUI), function))


def test_e_analysis_goes_through_the_existing_stage_5_entry_point():
    body = _body_code(_func(_tree(_GUI), "_prep_analyze_impl"))
    assert "analyze_video_sources" in body
    assert "classify_library_sources" in body, "the identity re-check reuses the one classifier seam"


def test_e_the_scan_handler_classifies_and_does_not_analyse():
    body = _body_code(_func(_tree(_GUI), "_prep_scan_impl"))
    assert "classify_library_sources" in body
    assert "analyze_video_sources" not in body


# ===========================================================================
# F. INVALIDATION
# ===========================================================================


def _prepared_state() -> lp.PrepSessionState:
    state = lp.set_folder(lp.initial_state(), "F")
    items = [{"path": "new", "status": NEW_OR_CHANGED[0], "reason": NEW_OR_CHANGED[1]}]
    return lp.record_scan(state, _scan_with(items))


@pytest.mark.parametrize("change", [
    lambda s: lp.set_folder(s, "OTHER"),
    lambda s: lp.set_recursive(s, False),
])
def test_f_every_classification_input_clears_the_scan(change):
    state = _prepared_state()
    assert state.can_analyze() is True

    changed = change(state)

    assert changed.scan is None
    assert changed.can_analyze() is False
    assert changed.subset_for_analysis() == ()
    assert "Scan Library again" in changed.notice


def test_f_the_live_declaration_guard_is_practical_equality():
    """Unit level: what counts as "the user changed the inputs".

    After P2 the declaration surface is folder + recursive, which is the whole of preparation's
    input. The guard is unchanged in strength - it simply has one fewer field to compare.
    """
    scan = lp.build_scan_result(
        folder=os.path.join("C:", os.sep, "lib"), recursive=True,
        runtime=lp.RuntimeIdentity(True, True, False, "b", "c", "m"),
        classification_items=[{"path": "new", "status": NEW_OR_CHANGED[0],
                               "reason": NEW_OR_CHANGED[1]}],
        supported_count=1)
    state = lp.record_scan(lp.initial_state(), scan)

    same = lp.LivePrepDeclaration.from_widgets(scan.folder, True)
    assert lp.declaration_refusal(state, same) is None

    for changed in (
        lp.LivePrepDeclaration.from_widgets(os.path.join("C:", os.sep, "other"), True),
        lp.LivePrepDeclaration.from_widgets(scan.folder, False),
        lp.LivePrepDeclaration.from_widgets("", True),
    ):
        assert lp.declaration_refusal(state, changed) == lp.STALE_DECLARATION_TEXT

    # cosmetic differences are not changes
    assert lp.declaration_refusal(state, lp.LivePrepDeclaration.from_widgets(
        scan.folder + os.sep, True)) is None
    assert lp.declaration_refusal(state, lp.LivePrepDeclaration.from_widgets(
        scan.folder.upper(), 1)) is None

    # nothing to compare: no scan, or a caller with no widgets (tests, scripts)
    assert lp.declaration_refusal(lp.initial_state(), same) is None
    assert lp.declaration_refusal(state, None) is None


def test_f_the_live_guard_stays_cheap_and_stats_nothing(tmp_path):
    """It runs before anything is stat'ed, probed or analysed, and must stay that way.

    P2 removed the later ``TrackIdentity.still_matches()`` content check along with the track itself,
    so the live declaration guard plus the runtime-identity recheck are now the whole of the Analyze
    gate. Neither may quietly start touching the filesystem per source.
    """
    body = _body_code(_func(_tree(_PREP), "declaration_refusal"))
    assert "os.stat" not in body and "open(" not in body

    prep_source = open(_PREP, "r", encoding="utf-8").read()
    for retired in ("TrackIdentity", "still_matches", "track_path", "smart_preset",
                    "audio_profile", "profile_snapshot", "profile_seconds", "set_track"):
        assert retired not in prep_source, f"{retired} must be gone from preparation state"

    describes = _body_code(_func(_tree(_PREP), "describes"))
    for forbidden in ("size", "mtime_ns", "blake2b", "open(", "fingerprint"):
        assert forbidden not in describes, f"the live guard must not fingerprint ({forbidden})"


def test_f_a_changed_runtime_identity_refuses_the_run(tmp_path):
    runtime = lp.RuntimeIdentity(True, True, False, "backend-A", "config-A", "m")
    scan = lp.build_scan_result(
        folder="F", recursive=True, runtime=runtime,
        classification_items=[{"path": "new", "status": NEW_OR_CHANGED[0],
                               "reason": NEW_OR_CHANGED[1]}],
        supported_count=1)
    state = lp.record_scan(lp.initial_state(), scan)

    assert lp.analyze_refusal(state, runtime) is None

    for drifted in (
        lp.RuntimeIdentity(True, True, False, "backend-B", "config-A", "m"),
        lp.RuntimeIdentity(True, True, False, "backend-A", "config-B", "m"),
        lp.RuntimeIdentity(True, False, False, "", "", ""),
        lp.RuntimeIdentity(True, True, True, "backend-A", "config-A", "m"),
    ):
        refusal = lp.analyze_refusal(state, drifted)
        assert refusal is not None and "Scan Library again" in refusal


def test_f_the_analyze_gate_has_no_track_refusal_left(tmp_path):
    """Retired by P2, and this asserts the retirement rather than leaving a hole.

    A track could invalidate a P V1 scan because it resolved the edit style the whole
    classification was keyed under. Nothing about the music reaches a key now, so there is no track
    to change - and the refusal list must not have acquired a substitute for it either.
    """
    runtime = lp.RuntimeIdentity(True, True, False, "b", "c", "m")
    state = lp.record_scan(lp.initial_state(), _scan_with(
        [{"path": "new", "status": NEW_OR_CHANGED[0], "reason": NEW_OR_CHANGED[1]}],
        runtime=runtime))

    assert lp.analyze_refusal(state, runtime) is None

    body = _body_code(_func(_tree(_PREP), "analyze_refusal"))
    for retired in ("track", "still_matches", "smart_preset", "audio", "preset"):
        assert retired not in body, f"analyze_refusal must not mention {retired}"
    # the refusals that DO remain
    assert "scan is None" in body and "is_usable" in body
    assert "subset_for_analysis" in body and "matches(runtime)" in body


def test_f_a_refused_or_finished_run_drops_the_scan():
    state = _prepared_state()

    refused = lp.record_failure(state, "nope", notice="Preparation run refused.")
    assert refused.scan is None and refused.can_analyze() is False

    finished = lp.record_analysis_complete(state, "PREPARATION RUN COMPLETE")
    assert finished.scan is None and finished.can_analyze() is False
    assert finished.report_text == "PREPARATION RUN COMPLETE"


def test_f_the_analyze_handler_rechecks_identity_before_running():
    """All three refusal checks must sit ahead of the analysis call, in source order."""
    fn = _func(_tree(_GUI), "_prep_analyze_impl")
    body = _body_code(fn)

    assert body.count("analyze_refusal") == 2, "cheap checks, then the recomputed-identity check"
    assert body.index("analyze_refusal") < body.index("analyze_video_sources(")
    assert body.rindex("analyze_refusal") < body.index("analyze_video_sources(")
    assert "runtime_identity_from_classification" in body

    # the live-declaration guard is FIRST: before the identity probe, before anything is analysed
    assert body.count("declaration_refusal") == 1
    assert body.index("declaration_refusal") < body.index("analyze_refusal")
    assert body.index("declaration_refusal") < body.index("classify_library_sources(")
    assert body.index("declaration_refusal") < body.index("analyze_video_sources(")


def test_f_the_analyze_click_submits_the_live_preparation_controls():
    """The fix for the queued-`change` race: state alone is not the authority at click time."""
    kwargs = _click_kwargs(_tree(_GUI), "prep_analyze_btn")
    names = [getattr(node, "id", None) for node in kwargs["inputs"].elts]

    assert names == ["prep_folder", "prep_recursive", "prep_batch_size", "prep_state"]
    assert getattr(kwargs["fn"], "id", None) == "_on_prep_analyze_click"


def test_f_the_analyze_handler_signature_matches_its_click_inputs():
    """Gradio supplies inputs positionally, so a silent reordering must fail the suite.

    Same guarantee `test_gui_guard_seam.py` pins for `process_video_guarded`.
    """
    kwargs = _click_kwargs(_tree(_GUI), "prep_analyze_btn")
    widgets = [getattr(node, "id", None) for node in kwargs["inputs"].elts]
    params = [a.arg for a in _func(_tree(_GUI), "_on_prep_analyze_click").args.args]

    assert params == ["folder_path", "recursive", "batch_size", "state"]
    assert len(params) == len(widgets)
    for widget, param in zip(widgets, params):
        assert widget.removeprefix("prep_").rstrip("_") in param.replace("_path", ""), \
            f"{widget} vs {param}"

    body = _body_code(_func(_tree(_GUI), "_on_prep_analyze_click"))
    assert "LivePrepDeclaration.from_widgets(folder_path, recursive)" in body


def test_f_the_scan_click_takes_the_folder_controls_only():
    kwargs = _click_kwargs(_tree(_GUI), "prep_scan_btn")
    names = [getattr(node, "id", None) for node in kwargs["inputs"].elts]
    assert names == ["prep_folder", "prep_recursive", "prep_state"]
    assert getattr(kwargs["fn"], "id", None) == "_on_prep_scan_click"
    params = [a.arg for a in _func(_tree(_GUI), "_on_prep_scan_click").args.args]
    assert params == ["folder_path", "recursive", "state"]


# ===========================================================================
# G. TRACKLESS, MEDIA-NEUTRAL PREPARATION (P2)
#
# Replaces P V1's "PROFILE PARITY" section wholesale. There is no profile to keep parity with: the
# scan derives no edit style, so it runs no audio analysis and needs no audio file.
# ===========================================================================


def test_g_the_scan_runs_no_audio_analysis_at_all():
    """No `analyze_beats_auto`, and therefore no Stages 1-4 during a preparation scan.

    This is where the measured ~15-20 s per-scan track-profile component went.
    """
    fn = _func(_tree(_GUI), "_prep_scan_impl")
    assert _calls_named(fn, "analyze_beats_auto") == []

    body = _body_code(fn)
    for forbidden in ("analyze_beats_auto", "audio_visual_profile", "_build_audio_visual_profile",
                      "smart_preset", "audio_profile", "TrackIdentity", "_as_existing_source_path",
                      "profile_seconds", "set_track"):
        assert forbidden not in body, f"a trackless scan must not reference {forbidden}"


def test_g_the_profile_formula_still_lives_only_in_auto_mode():
    """Unchanged: normal Create Video still builds it for Stage 6. It just never reaches Stage 5."""
    for path in (_GUI, _VA, _PREP):
        with open(path, "r", encoding="utf-8") as handle:
            assert "def _build_audio_visual_profile" not in handle.read(), path


def test_g_no_preparation_call_forwards_an_audio_profile():
    """Both runtime calls are media-neutral: the classifier has no such parameter, and Stage 5's
    retained one is simply not passed."""
    analyze = _func(_tree(_GUI), "_prep_analyze_impl")
    for call in _calls_named(analyze, "analyze_video_sources"):
        assert "audio_profile" not in ast.unparse(call), ast.unparse(call)
    for fn_name in ("_prep_scan_impl", "_prep_analyze_impl"):
        for call in _calls_named(_func(_tree(_GUI), fn_name), "classify_library_sources"):
            assert "audio_profile" not in ast.unparse(call), ast.unparse(call)


def test_g_preparation_state_carries_no_audio_or_style_field():
    """The state machine's whole surface, checked against the dataclasses themselves."""
    assert set(lp.PrepScanResult.__dataclass_fields__) == {
        "folder", "recursive", "runtime", "classifications", "supported_count",
        "folder_scan_seconds", "classify_seconds", "cache_identity_seconds",
        "cache_lookup_seconds",
    }
    assert set(lp.PrepSessionState.__dataclass_fields__) == {
        "folder", "recursive", "scan", "report_text", "notice", "batch_size",
    }
    assert set(lp.LivePrepDeclaration.__dataclass_fields__) == {"folder", "recursive"}
    for retired in ("TrackIdentity", "profile_snapshot", "set_track"):
        assert not hasattr(lp, retired), f"{retired} must be gone"
        assert retired not in lp.__all__


def test_g_the_report_states_the_semantic_mode_and_implies_no_track():
    scan = _scan_with([{"path": f"/lib/c{i}.mp4", "status": PREPARED[0], "reason": ""}
                       for i in range(903)])
    text = scan.render_text()

    assert "MEDIA LIBRARY PREPARATION" in text
    assert f"Semantic mode:     {lp.SEMANTIC_MODE_TEXT}" in text
    assert lp.SEMANTIC_MODE_TEXT == "media-neutral"
    assert "Supported videos:  903" in text
    assert "Prepared:          903" in text
    assert "New / changed:     0" in text
    assert "Scan time:         folder " in text and "identity " in text and "records " in text
    for forbidden in ("Track:", "Edit style:", "edit style", "smart_preset", "track"):
        assert forbidden not in text, f"the report must not imply track/style dependence: {forbidden}"


def test_g_no_preparation_ui_text_implies_a_track_or_an_edit_style():
    ui = open(os.path.join(_REPO, "src", "ui_content.py"), "r", encoding="utf-8").read()
    block = ui[ui.index("LABEL_PREP_SECTION"):ui.index("LABEL_PREP_SCAN")]
    for forbidden in ("LABEL_PREP_TRACK", "INFO_PREP_TRACK", "edit style the chosen track",
                      "MP3", "WAV", "FLAC"):
        assert forbidden not in block, forbidden
    assert "LABEL_PREP_TRACK" not in ui and "INFO_PREP_TRACK" not in ui

    prep_source = open(_PREP, "r", encoding="utf-8").read()
    assert "prepared for this edit style" not in prep_source
    assert "Everything in this library is prepared." in prep_source


def test_g_qwen_runtime_resolution_mirrors_auto_mode():
    body = _body_code(_func(_tree(_GUI), "_resolve_prep_qwen_runtime"))
    assert "AUTO_MODE_CONFIG.enable_qwen_semantics" in body
    assert "BEATSYNC_DISABLE_QWEN" in body
    assert "AUTO_MODE_CONFIG.qwen_model_path or DEFAULT_QWEN_MODEL_DIR" in body
    assert "_qwen_backend_available" not in body, "availability stays Stage 5's own rule"


# ===========================================================================
# H. NO RENDER
# ===========================================================================


_PREP_FUNCTIONS = (
    "_prep_ui_updates", "_prep_busy_updates", "_on_prep_folder_change",
    "_on_prep_recursive_change", "_on_prep_batch_size_change", "_resolve_prep_qwen_runtime",
    "_prep_scan_impl", "_prep_analyze_impl", "_run_prep_in_worker",
    "_on_prep_scan_click", "_on_prep_analyze_click",
)


@pytest.mark.parametrize("function", _PREP_FUNCTIONS)
@pytest.mark.parametrize("forbidden", [
    "create_music_video", "build_planned_clip_sequence", "extract_clip_segment_ffmpeg",
    "create_clip_parallel", "shutil.move", "get_output_dir", "FFMPEG_PATH", "_stage6_summary",
    "subprocess.run",
])
def test_h_no_preparation_path_can_render(function, forbidden):
    assert forbidden not in _body_code(_func(_tree(_GUI), function))


def test_h_preparation_functions_all_exist():
    tree = _tree(_GUI)
    for name in _PREP_FUNCTIONS:
        _func(tree, name)


# ===========================================================================
# I. CREATE VIDEO ISOLATION
# ===========================================================================


def _click_kwargs(tree: ast.Module, button: str) -> Dict[str, ast.AST]:
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "click"
                and getattr(n.func.value, "id", None) == button)
    return {kw.arg: kw.value for kw in call.keywords}


#: The render request, in order. Creative Controls Core added the three creative sliders alongside
#: the variation seed; they are render-request creative state, not preparation state, which is what
#: the assertions below actually care about.
_RENDER_CLICK_INPUTS = [
    "audio_input", "source_mode", "source_folder", "source_recursive", "video_input",
    "output_filename", "processing_mode", "custom_fps", "variation_seed",
    "cut_density", "energy_response", "motion_bias", "source_diversity", "micro_cuts",
    "session_state", "source_state",
]


def test_i_the_render_click_inputs_carry_no_preparation_state():
    kwargs = _click_kwargs(_tree(_GUI), "process_btn")
    names = [getattr(node, "id", None) for node in kwargs["inputs"].elts]

    assert names == _RENDER_CLICK_INPUTS
    assert getattr(kwargs["fn"], "id", None) == "process_video_guarded"
    assert not any(name and name.startswith("prep") for name in names)


def test_i_the_guard_signature_carries_no_preparation_state():
    args = [a.arg for a in _func(_tree(_GUI), "process_video_guarded").args.args]
    # audio_input -> audio_file is the one deliberate rename between widget and parameter.
    assert args == ["audio_file"] + _RENDER_CLICK_INPUTS[1:]
    assert not any(name.startswith("prep") for name in args)


def test_i_preparation_widgets_are_absent_from_source_outputs():
    tree = _tree(_GUI)
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", None) == "source_outputs")
    names = [getattr(node, "id", None) for node in assignment.value.elts]

    assert names == ["source_report", "confirm_btn", "confirm_status", "process_btn", "source_state"]
    assert not any(name and name.startswith("prep") for name in names)


def test_i_preparation_outputs_touch_no_render_widget():
    tree = _tree(_GUI)
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", None) == "prep_outputs")
    names = [getattr(node, "id", None) for node in assignment.value.elts]

    assert names == ["prep_report", "prep_status", "prep_analyze_btn", "prep_state"]
    for render_widget in ("process_btn", "source_state", "source_report", "confirm_btn",
                          "confirm_status", "video_input", "source_folder", "session_state"):
        assert render_widget not in names


def test_i_preparation_handlers_never_touch_the_source_gate():
    tree = _tree(_GUI)
    for name in _PREP_FUNCTIONS:
        body = _body_code(_func(tree, name))
        for forbidden in ("source_state", "resolve_for_render", "confirm_action",
                          "live_declaration", "scan_folder_action", "set_browser_files",
                          "_source_ui_updates", "process_video"):
            assert forbidden not in body, f"{name} must not reach {forbidden}"


def test_i_the_source_handlers_never_touch_preparation_state():
    tree = _tree(_GUI)
    for name in ("_on_source_mode_change", "_on_folder_path_change", "_on_recursive_change",
                 "_on_scan_click", "_on_browser_files_change", "_on_confirm_click",
                 "process_video_guarded", "process_video", "_process_video_impl"):
        body = _body_code(_func(tree, name))
        assert "prep_state" not in body and "fork_prep" not in body, name


def test_i_preparation_offers_no_browser_upload_mode():
    """Browser uploads land in a temp folder cleared on restart, so a path-keyed preparation of
    them would be worthless. Folder + recursive only."""
    source = open(_GUI, "r", encoding="utf-8").read()
    block = source[source.index("LABEL_PREP_SECTION"):source.index("prep_status = gr.Markdown")]
    assert "file_count='multiple'" not in block
    assert "gr.Radio" not in block, "no source-mode choice in preparation"


# ===========================================================================
# J. CONSTANTS AND CONTRACT
# ===========================================================================


def test_j_the_cache_contract_is_v3_and_the_analysis_version_is_unchanged():
    """P2 makes exactly one deliberate bump, because a v3 semantic record means something different
    from a v2 one. Deterministic candidate scoring / windows / schema are untouched."""
    source = open(_VA, "r", encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_j_preparation_adds_nothing_to_cache_identity_or_the_completion_rule():
    tree = _tree(_VA)
    for name in ("_video_signature", "_cache_path", "_qwen_config_token",
                 "_cache_entry_is_complete", "_checkpoint_cache", "_save_cache"):
        body = _body_code(_func(tree, name))
        for leaked in ("prep", "library_prep", "fork_prep", "classify_library_sources",
                       "_PREP_PHASE"):
            assert leaked not in body, f"{name} must not know about preparation ({leaked})"


def test_j_the_analyzer_return_payload_is_unchanged():
    """P V1 reads the existing R1 current-run fields; it adds no field to Stage 5's result."""
    code = _body_code(_func(_tree(_VA), "analyze_video_sources"))
    for field in ("'sources_analyzed_this_run': len(jobs)", "'source_count': len(existing)",
                  "'cache_hits': cache_hits"):
        assert field in code


# ===========================================================================
# PROGRESS
# ===========================================================================


def test_progress_uses_stage_zero_with_a_preparation_phase(va, tmp_path):
    events = []
    sources = [_source(tmp_path, f"p{i}.mp4", size=4096 + i) for i in range(3)]
    _classify(va, sources, event_callback=events.append)

    assert events, "the classifier must report progress"
    assert {event.stage for event in events} == {0}
    assert {event.data.get("phase") for event in events} == {"library_classify"}

    counted = [e for e in events if e.is_counted()]
    assert [e.current for e in counted] == sorted(e.current for e in counted), "monotonic"
    assert counted[-1].current == counted[-1].total == 3
    assert all(e.total == 3 for e in counted)


def test_progress_never_invents_a_rate_basis_or_an_eta(va, tmp_path):
    events = []
    _classify(va, [_source(tmp_path, "p.mp4")], event_callback=events.append)
    for event in events:
        assert not hasattr(event, "eta")
        assert "eta" not in event.data


def test_progress_emission_cannot_break_a_scan(va, tmp_path):
    """`emit` swallows callback failures; a broken status widget must not lose a library scan."""
    def broken(_event):
        raise RuntimeError("widget gone")

    source = _source(tmp_path, "p.mp4")
    assert _verdicts(_classify(va, [source], event_callback=broken))[source] == NEW_OR_CHANGED


def test_progress_reuses_the_existing_framework_only():
    body = _body_code(_func(_tree(_VA), "classify_library_sources"))
    assert "fork_progress.emit" in body
    assert "fork_progress.StageCounter" in body
    for name in _PREP_FUNCTIONS:
        gui_body = _body_code(_func(_tree(_GUI), name))
        assert "class " not in gui_body, f"{name} must not define a second progress type"

    worker = _body_code(_func(_tree(_GUI), "_run_prep_in_worker"))
    assert "ProgressView()" in worker and "StageConsoleLogger" in worker
    assert "status_queue.put(event)" in worker, "worker threads may only touch the queue"


def test_progress_the_worker_callback_only_touches_the_queue():
    """Same rule as `process_video`: no Gradio component may be reached from a worker thread."""
    worker = _func(_tree(_GUI), "_run_prep_in_worker")
    callback = _func(worker, "event_callback")
    attributes = {node.func.attr for node in ast.walk(callback)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert attributes == {"put"}


# ===========================================================================
# THE REAL GUI HANDLER BODIES, EXECUTED
#
# The structural checks above prove intent; these prove behaviour. `gui.py` needs Gradio and the
# whole runtime, so its two preparation impl functions are lifted out by their `ast` source ranges
# and executed against stubs — a fake ``video_analysis`` module registered in ``sys.modules`` for
# their local imports, plus a recording `scan_folder`. Nothing real runs: no Qwen, no OpenCV, no
# FFmpeg, no render.
#
# P2: `analyze_beats_auto` is still registered in the namespace, but as a stub that FAILS the test if
# it is ever called. Absence would only produce a NameError; a tripwire says why.
# ===========================================================================


_GUI_FUNCS = ("_prep_ui_updates", "_prep_busy_updates", "_resolve_prep_qwen_runtime",
              "_prep_scan_impl", "_prep_analyze_impl")


class _Recorder:
    """Records every call so the assertions are about what was actually invoked."""

    def __init__(self):
        self.classify_calls: list[dict] = []
        self.analyze_calls: list[dict] = []
        self.beats_calls: list[dict] = []
        self.scan_calls: list[dict] = []
        self.classify_result: dict = {}
        self.probe_result: dict | None = None
        self.ready_paths: list[str] = []


def _gui_namespace(recorder: _Recorder, tmp_path) -> Dict[str, Any]:
    import sys
    import time as time_module
    import types

    from beatsync_fork.input_manager import InputScanError as RealScanError

    with open(_GUI, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    def classify_library_sources(video_files, enable_ai=True,
                                 qwen_model_path=None, event_callback=None):
        recorder.classify_calls.append({
            "video_files": list(video_files),
            "enable_ai": enable_ai, "qwen_model_path": qwen_model_path,
        })
        if not video_files and recorder.probe_result is not None:
            return recorder.probe_result
        return recorder.classify_result

    def analyze_video_sources(video_files=None, audio_profile=None, use_gpu=False,
                              enable_ai=True, qwen_model_path=None, event_callback=None):
        recorder.analyze_calls.append({
            "video_files": list(video_files or []), "audio_profile": audio_profile,
            "enable_ai": enable_ai, "qwen_model_path": qwen_model_path, "use_gpu": use_gpu,
        })
        return {"sources_analyzed_this_run": len(video_files or []),
                "qwen_jobs_this_run": 0, "analysis_seconds": 2.5}

    fake_va = types.ModuleType("video_analysis")
    fake_va.classify_library_sources = classify_library_sources
    fake_va.analyze_video_sources = analyze_video_sources
    fake_va.DEFAULT_QWEN_MODEL_DIR = str(tmp_path / "models")
    sys.modules["video_analysis"] = fake_va

    def analyze_beats_auto(*_args, **_kwargs):
        """P2 tripwire: a trackless preparation scan must never run audio analysis."""
        recorder.beats_calls.append({})
        raise AssertionError(
            "preparation called analyze_beats_auto; a P2 scan runs no audio Stages 1-4")

    class _Media:
        def __init__(self, path):
            self.path = path

    class _InputSet:
        def __init__(self, paths):
            self.files = tuple(_Media(path) for path in paths)

    def scan_library_folder(root, recursive=True, detect_duplicates=True):
        recorder.scan_calls.append({"root": root, "recursive": recursive,
                                    "detect_duplicates": detect_duplicates})
        return _InputSet(recorder.ready_paths)

    class _Config:
        enable_qwen_semantics = True
        qwen_model_path = ""

    namespace: Dict[str, Any] = {
        "os": os, "time": time_module, "gr": None, "Tuple": tuple,
        "__builtins__": __builtins__,
        "fork_prep": lp,
        "AUTO_MODE_CONFIG": _Config(),
        "GPU_AVAILABLE": False,
        "InputScanError": RealScanError,
        "scan_library_folder": scan_library_folder,
        "analyze_beats_auto": analyze_beats_auto,
        "StageConsoleLogger": object,
        "_stage5_summary": lambda *_args, **_kwargs: None,
        "_as_existing_source_path": lambda p: (os.path.abspath(p)
                                               if p and os.path.isfile(p) else None),
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _GUI_FUNCS:
            exec(compile("from __future__ import annotations\n"
                         + ast.get_source_segment(source, node), f"<{node.name}>", "exec"),
                 namespace)
    missing = [name for name in _GUI_FUNCS if name not in namespace]
    assert not missing, f"missing from gui.py: {missing}"
    return namespace


@pytest.fixture
def gui(tmp_path):
    """The two preparation handlers, executed against stubs.

    An audio file is still written, and a third tuple element still carries it, purely so a
    regression that reintroduces a track input has something plausible to be handed — the handlers
    themselves take (folder, recursive, state) and must never look at it.
    """
    recorder = _Recorder()
    namespace = _gui_namespace(recorder, tmp_path)
    unused_track = str(tmp_path / "track.mp3")
    with open(unused_track, "wb") as handle:
        handle.write(b"audio bytes")
    yield namespace, recorder, unused_track
    import sys
    sys.modules.pop("video_analysis", None)


def _live_for(scan: lp.PrepScanResult) -> lp.LivePrepDeclaration:
    """The declaration a user who has changed nothing since the scan would submit."""
    return lp.LivePrepDeclaration.from_widgets(scan.folder, scan.recursive)


def _classification_payload(prepared: int, new: int, incomplete: int) -> dict:
    items = [{"path": f"/lib/warm_{i}.mp4", "status": PREPARED[0], "reason": ""}
             for i in range(prepared)]
    items += [{"path": f"/lib/new_{i}.mp4", "status": NEW_OR_CHANGED[0],
               "reason": NEW_OR_CHANGED[1]} for i in range(new)]
    items += [{"path": f"/lib/bad_{i}.mp4", "status": INCOMPLETE[0],
               "reason": INCOMPLETE[1]} for i in range(incomplete)]
    return {
        "classifications": items, "ai_available": True, "ai_cache_disabled": False,
        "backend_token": "backend-A", "config_token": "config-A",
        "qwen_model_path": "M",
        "cache_identity_seconds": 7.4, "cache_lookup_seconds": 3.9, "classify_seconds": 11.7,
    }


def test_real_scan_handler_classifies_the_whole_library_once(gui, tmp_path):
    namespace, recorder, _track = gui
    recorder.ready_paths = [f"/lib/f{i}.mp4" for i in range(10)]
    recorder.classify_result = _classification_payload(8, 1, 1)

    state = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())

    assert len(recorder.scan_calls) == 1
    assert recorder.scan_calls[0]["detect_duplicates"] is False
    assert recorder.beats_calls == [], "a trackless scan runs no audio analysis"
    assert len(recorder.classify_calls) == 1
    assert recorder.classify_calls[0]["video_files"] == recorder.ready_paths
    assert "audio_profile" not in recorder.classify_calls[0]
    assert not recorder.analyze_calls, "a scan must not analyse anything"

    assert state.scan is not None
    assert (state.scan.prepared_count, state.scan.needs_analysis_count) == (8, 2)
    assert state.can_analyze() is True
    assert "Prepared:          8" in state.report_text
    assert "media-neutral" in state.report_text


def test_real_analyze_handler_sends_only_the_subset(gui, tmp_path):
    namespace, recorder, _track = gui
    recorder.ready_paths = [f"/lib/f{i}.mp4" for i in range(10)]
    recorder.classify_result = _classification_payload(8, 1, 1)
    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())

    recorder.probe_result = dict(recorder.classify_result, classifications=[])
    finished = namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan))

    assert len(recorder.analyze_calls) == 1
    call = recorder.analyze_calls[0]
    assert call["video_files"] == ["/lib/new_0.mp4", "/lib/bad_0.mp4"]
    assert len(call["video_files"]) == 2 and len(recorder.ready_paths) == 10
    assert call["audio_profile"] is None, "P2: Stage 5 is called media-neutrally"
    assert recorder.beats_calls == []
    assert call["enable_ai"] is True

    # the identity re-check ran through the same classifier seam, with no sources
    assert recorder.classify_calls[-1]["video_files"] == []

    assert finished.scan is None, "the stale counts must not survive the run"
    assert finished.can_analyze() is False
    assert "Sources analyzed this batch:  2" in finished.report_text
    assert lp.RESCAN_HINT in finished.report_text


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda folder, rec: ("/other/library", rec), id="folder"),
    pytest.param(lambda folder, rec: (folder, not rec), id="recursive"),
])
def test_real_analyze_refuses_when_a_live_control_changed(gui, tmp_path, mutate):
    """The queued-`change` race: the widgets declare B while the state still holds A's scan.

    Refused before anything is probed or analysed - so neither the runtime identity probe nor the
    analyzer is reached, and the stale scan is dropped rather than silently re-targeted. Folder and
    recursive are the whole live surface after P2, so this covers all of it.
    """
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = _classification_payload(0, 1, 0)
    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())
    assert scanned.can_analyze() is True
    calls_after_scan = len(recorder.classify_calls)

    folder, recursive = mutate("/lib", True)
    live = lp.LivePrepDeclaration.from_widgets(folder, recursive)
    refused = namespace["_prep_analyze_impl"](scanned, live)

    assert not recorder.analyze_calls, "the analyzer must not be reached"
    assert len(recorder.classify_calls) == calls_after_scan, "no runtime identity probe either"
    assert refused.scan is None, "the stale scan is dropped, never re-targeted"
    assert refused.can_analyze() is False
    assert lp.STALE_DECLARATION_TEXT in refused.report_text
    assert not recorder.scan_calls[1:], "and no automatic rescan"


def test_real_analyze_proceeds_when_every_live_control_matches(gui, tmp_path):
    """The guard must not become a false refusal: unchanged controls analyse exactly as before."""
    namespace, recorder, _track = gui
    recorder.ready_paths = [f"/lib/f{i}.mp4" for i in range(10)]
    recorder.classify_result = _classification_payload(8, 1, 1)
    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())

    recorder.probe_result = dict(recorder.classify_result, classifications=[])
    finished = namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan))

    assert recorder.classify_calls[-1]["video_files"] == [], "the identity probe still runs"
    assert len(recorder.analyze_calls) == 1
    assert recorder.analyze_calls[0]["video_files"] == ["/lib/new_0.mp4", "/lib/bad_0.mp4"]
    assert finished.scan is None and "Sources analyzed this batch:  2" in finished.report_text


def test_real_analyze_tolerates_cosmetic_path_differences(gui, tmp_path):
    """A trailing separator or a case difference is not the user changing their mind."""
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = _classification_payload(0, 1, 0)
    folder = str(tmp_path / "lib")
    os.makedirs(folder, exist_ok=True)
    scanned = namespace["_prep_scan_impl"](folder, True, lp.initial_state())

    recorder.probe_result = dict(recorder.classify_result, classifications=[])
    cosmetic = lp.LivePrepDeclaration.from_widgets(folder + os.sep, True)
    finished = namespace["_prep_analyze_impl"](scanned, cosmetic)

    assert len(recorder.analyze_calls) == 1, "a trailing separator must not refuse"
    assert finished.scan is None and "PREPARATION BATCH COMPLETE" in finished.report_text


def test_real_analyze_handler_refuses_when_identity_drifted(gui, tmp_path):
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = _classification_payload(0, 1, 0)
    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())

    # a swapped GGUF between the two clicks
    recorder.probe_result = dict(recorder.classify_result, classifications=[],
                                 backend_token="backend-B")
    refused = namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan))

    assert not recorder.analyze_calls, "nothing may be analysed under a drifted identity"
    assert refused.scan is None
    assert "Scan Library again" in refused.report_text


def test_real_scan_handler_refuses_an_unverifiable_backend(gui, tmp_path):
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = {
        "classifications": [], "ai_available": True, "ai_cache_disabled": True,
        "backend_token": "", "config_token": "", "qwen_model_path": "",
    }

    state = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())

    assert state.scan is not None, "the scan is recorded, but unusable"
    assert state.can_analyze() is False
    assert lp.BACKEND_UNVERIFIED_TEXT in state.report_text
    assert namespace["_prep_analyze_impl"](state, _live_for(state.scan)).scan is None
    assert not recorder.analyze_calls


@pytest.mark.parametrize("folder", ["", "   ", None])
def test_real_scan_handler_requires_a_folder_and_nothing_else(gui, tmp_path, folder):
    """The folder is now the only mandatory input - there is no track to be missing."""
    namespace, recorder, _track = gui
    state = namespace["_prep_scan_impl"](folder, True, lp.initial_state())

    assert state.scan is None
    assert "No library folder selected" in state.report_text
    assert not recorder.scan_calls and not recorder.beats_calls


def test_real_scan_handler_applies_the_live_widget_values(gui, tmp_path):
    """A Textbox `change` may not have fired when the user types a path and clicks Scan at once."""
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = _classification_payload(1, 0, 0)

    stale = lp.set_folder(lp.initial_state(), "OLD")
    state = namespace["_prep_scan_impl"]("/live/folder", False, stale)

    assert recorder.scan_calls[0]["root"] == "/live/folder"
    assert recorder.scan_calls[0]["recursive"] is False
    assert state.scan.folder == "/live/folder" and state.scan.recursive is False


def test_real_scan_handler_survives_a_failed_folder_scan(gui, tmp_path):
    namespace, recorder, _track = gui

    def exploding(root, recursive=True, detect_duplicates=True):
        raise namespace["InputScanError"]("no such directory")

    namespace["scan_library_folder"] = exploding
    state = namespace["_prep_scan_impl"]("/missing", True, lp.initial_state())

    assert state.scan is None
    assert "LIBRARY SCAN FAILED" in state.report_text
    assert not recorder.classify_calls, "a failed enumeration must not classify anything"


def test_real_runtime_resolution_honours_the_disable_switch(gui, monkeypatch):
    namespace, _recorder, _track = gui

    monkeypatch.delenv("BEATSYNC_DISABLE_QWEN", raising=False)
    enabled, model = namespace["_resolve_prep_qwen_runtime"]()
    assert enabled is True and model.endswith("models")

    monkeypatch.setenv("BEATSYNC_DISABLE_QWEN", "1")
    assert namespace["_resolve_prep_qwen_runtime"]()[0] is False


def test_real_handlers_forward_the_resolved_runtime_to_both_calls(gui, tmp_path):
    namespace, recorder, _track = gui
    recorder.ready_paths = ["/lib/f0.mp4"]
    recorder.classify_result = _classification_payload(0, 1, 0)

    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())
    recorder.probe_result = dict(recorder.classify_result, classifications=[])
    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan))

    scan_call, probe_call = recorder.classify_calls[0], recorder.classify_calls[-1]
    analyze_call = recorder.analyze_calls[0]
    assert scan_call["enable_ai"] == probe_call["enable_ai"] == analyze_call["enable_ai"]
    assert (scan_call["qwen_model_path"] == probe_call["qwen_model_path"]
            == analyze_call["qwen_model_path"])
    assert analyze_call["audio_profile"] is None, "neither call carries an audio profile"
    assert recorder.beats_calls == []


# ===========================================================================
# K. DEPENDENCY RULE
# ===========================================================================


def test_k_library_prep_imports_only_stdlib_and_fork():
    allowed = {"__future__", "dataclasses", "enum", "os", "typing", "beatsync_fork"}
    imported: set[str] = set()
    for node in ast.walk(_tree(_PREP)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    assert imported <= allowed, sorted(imported - allowed)


def test_k_the_dependency_runs_one_way_only():
    """`video_analysis` may import the fork package; the fork package may never import back."""
    with open(_PREP, "r", encoding="utf-8") as handle:
        prep_source = handle.read()
    for forbidden in ("video_analysis", "gradio", "librosa", "cv2", "numpy", "logger", "paths"):
        assert f"import {forbidden}" not in prep_source

    with open(_VA, "r", encoding="utf-8") as handle:
        assert "from beatsync_fork import library_prep as fork_prep" in handle.read()


# ===========================================================================
# RUN SUMMARY
# ===========================================================================


def test_run_summary_uses_current_run_truth_only():
    """R1: the unsuffixed `qwen_*` aggregates include every cache hit, so a 4-source preparation
    run must never quote them as its own work."""
    result = {
        "sources_analyzed_this_run": 4,
        "qwen_jobs_this_run": 4,
        "qwen_tag_count_this_run": 38,
        "qwen_requested_count_this_run": 40,
        "qwen_incomplete_jobs_this_run": 1,
        "analysis_seconds": 61.5,
        # historical library aggregates that must not appear
        "qwen_tag_count": 8704,
        "qwen_frame_count": 8704,
        "qwen_seconds": 3031.9,
    }
    text = lp.summarize_analysis_run(result)

    assert "Sources analyzed this batch:  4" in text
    assert "38/40 tags" in text
    assert "1 incomplete" in text
    assert "61.5s" in text
    for historical in ("8704", "3031.9"):
        assert historical not in text
    assert lp.RESCAN_HINT in text


def test_run_summary_is_total_on_a_malformed_payload():
    for payload in (None, {}, {"sources_analyzed_this_run": "many"}, []):
        text = lp.summarize_analysis_run(payload)
        assert "PREPARATION BATCH COMPLETE" in text
        assert "Sources analyzed this batch:  0" in text


def test_summary_source_is_the_run_not_the_library():
    body = _body_code(_func(_tree(_PREP), "summarize_analysis_run"))
    assert "_this_run" in body
    for historical in ('"qwen_tag_count"', "'qwen_tag_count'", "'qwen_seconds'"):
        assert historical not in body


# ===========================================================================
# K. BOUNDED ANALYSIS BATCHES
#
# Scan still classifies the whole library; one Analyze click submits a bounded prefix of what that
# classification found outstanding. The bound exists because Stage 5's shared Qwen worker writes its
# response - and therefore releases every checkpoint in it - only after its whole job loop finishes,
# so a 1107-source submission is one all-or-nothing worker invocation.
#
# Batch size is execution policy, never classification identity. The invalidation tests below are
# the load-bearing half: folder and recursive must still drop a scan, and batch size must not.
# ===========================================================================


def test_k_the_default_batch_size_is_the_preparation_constant():
    assert lp.DEFAULT_ANALYZE_BATCH_SIZE == 100
    assert lp.initial_state().batch_size == 100
    assert lp.normalize_batch_size(None) == lp.DEFAULT_ANALYZE_BATCH_SIZE


@pytest.mark.parametrize("value,expected", [
    (1, 1), (2, 2), (50, 50), (100, 100), (250, 250), (5000, 5000),
    (1.0, 1), (100.0, 100), (5000.0, 5000),          # Gradio's number box yields floats
    ("1", 1), ("100", 100), ("5000", 5000),          # a hand-typed decimal string
])
def test_k_positive_whole_batch_sizes_are_accepted(value, expected):
    result = lp.normalize_batch_size(value)
    assert result == expected
    assert type(result) is int, "a batch size must be a plain int, never a float or bool"


@pytest.mark.parametrize("value", [
    None, "", "  ", "abc", "1e3", "100.0", "-5", "0",   # "100.0" is not a decimal integer string
    0, -1, -100, 0.0, -2.5,
    0.5, 7.9, 99.999, 100.5,                            # fractional: never silently truncated
    float("nan"), float("inf"), float("-inf"),
    True, False,                                        # bool subclasses int and is rejected first
    [], {}, (1,),
])
def test_k_malformed_or_non_positive_batch_sizes_fall_back_to_the_default(value):
    """Deliberately the default, never a guess.

    Mirrors `variation.normalize_seed`: truncating `7.9` to 7 would silently submit a batch the user
    never chose, and `True` is not a request for a batch of 1. Nothing here may raise mid-run.
    """
    assert lp.normalize_batch_size(value) == lp.DEFAULT_ANALYZE_BATCH_SIZE


def test_k_a_fractional_value_is_not_floored():
    """Pinned separately because flooring is the tempting wrong answer.

    Every value here has a floor that differs from the default, so the assertion can actually tell
    the two behaviours apart — `100.5` could not, since flooring it and falling back both give 100.
    """
    for value, floor in ((7.9, 7), (250.5, 250), (0.5, 0), (49.9, 49), (1.5, 1)):
        assert lp.normalize_batch_size(value) != floor
        assert lp.normalize_batch_size(value) == lp.DEFAULT_ANALYZE_BATCH_SIZE


def _outstanding_scan(count: int, prepared: int = 0) -> lp.PrepScanResult:
    items = [{"path": f"/lib/warm_{i}.mp4", "status": PREPARED[0], "reason": ""}
             for i in range(prepared)]
    items += [{"path": f"/lib/need_{i}.mp4", "status": NEW_OR_CHANGED[0],
               "reason": NEW_OR_CHANGED[1]} for i in range(count)]
    return _scan_with(items)


def test_k_the_full_outstanding_subset_is_unchanged_by_batching():
    """`subset_for_analysis()` keeps its meaning: the WHOLE outstanding set."""
    scan = _outstanding_scan(1107, prepared=93)

    assert scan.supported_count == 1200
    assert scan.prepared_count == 93
    assert scan.needs_analysis_count == 1107
    assert len(scan.subset_for_analysis()) == 1107
    assert scan.subset_for_analysis()[0] == "/lib/need_0.mp4"
    assert scan.subset_for_analysis()[-1] == "/lib/need_1106.mp4"


def test_k_a_batch_is_the_first_n_of_the_outstanding_ordering():
    scan = _outstanding_scan(1107)
    batch = scan.subset_for_analysis_batch(100)

    assert len(batch) == 100
    assert batch == scan.subset_for_analysis()[:100]
    assert batch[0] == "/lib/need_0.mp4" and batch[-1] == "/lib/need_99.mp4"


def test_k_a_smaller_outstanding_set_returns_all_of_it():
    for outstanding in (0, 1, 37, 57, 99, 100):
        scan = _outstanding_scan(outstanding)
        batch = scan.subset_for_analysis_batch(100)
        assert batch == scan.subset_for_analysis()
        assert len(batch) == outstanding


def test_k_batching_is_deterministic_and_preserves_classification_order():
    """No shuffling, no re-ranking, no deduplication - a plain prefix, repeatable."""
    scan = _outstanding_scan(250)

    first = scan.subset_for_analysis_batch(100)
    assert first == scan.subset_for_analysis_batch(100), "repeated calls must agree"
    assert first == tuple(f"/lib/need_{i}.mp4" for i in range(100))

    # Successive widths nest, which is what makes "the next batch" meaningful after a re-scan.
    assert scan.subset_for_analysis_batch(50) == first[:50]
    assert scan.subset_for_analysis_batch(250)[:100] == first
    assert len(set(scan.subset_for_analysis_batch(250))) == 250, "no duplicates introduced"


def test_k_a_malformed_batch_limit_still_yields_the_default_sized_batch():
    scan = _outstanding_scan(250)
    for bad in (None, 0, -1, 7.9, True, "abc"):
        assert len(scan.subset_for_analysis_batch(bad)) == lp.DEFAULT_ANALYZE_BATCH_SIZE


def test_k_changing_the_batch_size_does_not_invalidate_the_scan():
    """The whole point of the feature's UX: retuning the bound must not cost a re-classification."""
    state = _prepared_state()
    assert state.scan is not None and state.can_analyze() is True

    retuned = lp.set_batch_size(state, 50)

    assert retuned.scan is state.scan, "the SAME scan object must survive"
    assert retuned.can_analyze() is True
    assert retuned.batch_size == 50
    assert retuned.notice == state.notice, "no invalidation notice"


@pytest.mark.parametrize("value,expected", [(50, 50), (1, 1), (5000, 5000),
                                            (0, 100), (None, 100), (7.9, 100), (True, 100)])
def test_k_set_batch_size_normalizes_and_never_drops_the_scan(value, expected):
    retuned = lp.set_batch_size(_prepared_state(), value)
    assert retuned.batch_size == expected
    assert retuned.scan is not None


@pytest.mark.parametrize("change,label", [
    (lambda s: lp.set_folder(s, "OTHER"), "folder"),
    (lambda s: lp.set_recursive(s, False), "recursive"),
])
def test_k_classification_inputs_still_invalidate(change, label):
    """The contrast that makes the batch-size exemption safe rather than a loophole."""
    state = lp.set_batch_size(_prepared_state(), 50)
    assert state.scan is not None

    changed = change(state)

    assert changed.scan is None, f"{label} must still drop the scan"
    assert changed.can_analyze() is False
    assert changed.batch_size == 50, "the chosen batch size survives an invalidation"


def test_k_batch_size_is_absent_from_the_live_declaration():
    """`LivePrepDeclaration` describes the classification, so batch size must not appear in it."""
    assert set(lp.LivePrepDeclaration.__dataclass_fields__) == {"folder", "recursive"}

    scan = _outstanding_scan(250)
    live = lp.LivePrepDeclaration.from_widgets(scan.folder, scan.recursive)
    assert live.describes(scan) is True

    body = _body_code(_func(_tree(_PREP), "set_batch_size"))
    for invalidator in ("_invalidated", "scan=None", "INTRO_TEXT"):
        assert invalidator not in body, "set_batch_size must not invalidate anything"


def test_k_batch_size_reaches_no_cache_identity_or_completion_rule():
    """The P2/D2 boundary: execution policy may not become cache identity."""
    va_source = open(_VA, "r", encoding="utf-8").read()
    for name in ("DEFAULT_ANALYZE_BATCH_SIZE", "normalize_batch_size",
                 "subset_for_analysis_batch", "analyze_batch_size"):
        assert name not in va_source, f"{name} must not reach video_analysis.py"

    tree = _tree(_VA)
    for func in ("_video_signature", "_cache_path", "_qwen_config_token",
                 "_cache_entry_is_complete", "_checkpoint_cache", "classify_library_sources"):
        body = _body_code(_func(tree, func))
        assert "batch_size" not in body, f"{func} must not mention batch_size"


# --- button and report wording -------------------------------------------------


def test_k_the_button_names_the_bounded_work():
    state = lp.record_scan(lp.initial_state(), _outstanding_scan(1107))

    assert state.analyze_button_label() == "⚙️ Analyze next 100"
    assert state.analyze_button_label(50) == "⚙️ Analyze next 50"
    assert lp.set_batch_size(state, 250).analyze_button_label() == "⚙️ Analyze next 250"


def test_k_the_button_says_remaining_when_the_batch_covers_everything():
    for outstanding in (1, 37, 100):
        label = lp.record_scan(lp.initial_state(),
                               _outstanding_scan(outstanding)).analyze_button_label()
        assert label == f"⚙️ Analyze {outstanding} remaining"


def test_k_the_button_promises_no_duration():
    for outstanding in (1, 37, 100, 1107):
        label = lp.record_scan(lp.initial_state(),
                               _outstanding_scan(outstanding)).analyze_button_label()
        for promise in ("minute", "hour", "second", "eta", "~", "estimat"):
            assert promise not in label.lower()


def test_k_the_report_retains_both_the_outstanding_total_and_the_batch():
    text = lp.record_scan(lp.initial_state(), _outstanding_scan(1107)).report_text

    assert "New / changed:     1107" in text
    assert "Analyze batch:     100 per run" in text
    assert "Ready to analyze 1107 video(s)." in text
    assert "This run submits the next 100" in text
    for promise in ("minute", "hour", "eta", "estimat"):
        assert promise not in text.lower()


def test_k_the_report_does_not_mention_a_batch_when_everything_fits():
    """No redundant second name for the same number: 37 outstanding under a batch of 100 is just
    37 to analyse, and the closing line already says the run submits all of them."""
    text = lp.record_scan(lp.initial_state(), _outstanding_scan(37)).report_text

    assert "New / changed:     37" in text
    assert "Ready to analyze 37 video(s). This run submits all of them." in text
    assert "Analyze batch:" not in text
    assert "per run" not in text


def test_k_the_summary_reports_the_batch_and_demands_a_rescan():
    text = lp.summarize_analysis_run(
        {"sources_analyzed_this_run": 100, "qwen_jobs_this_run": 100,
         "qwen_tag_count_this_run": 1240, "qwen_requested_count_this_run": 1240,
         "analysis_seconds": 1144.0},
        submitted=100,
    )

    assert "PREPARATION BATCH COMPLETE" in text
    assert "Submitted this batch:         100" in text
    assert "Sources analyzed this batch:  100" in text
    assert lp.BATCH_FINISHED_TEXT in text
    assert lp.RESCAN_HINT in text


def test_k_the_summary_never_states_a_remaining_count():
    """A remaining figure would be provisional at best: the scan that knew the old total has just
    been dropped, and the library can change on disk between clicks."""
    text = lp.summarize_analysis_run(
        {"sources_analyzed_this_run": 100, "analysis_seconds": 1.0}, submitted=100)

    for word in ("remaining:", "left:", "1007", "outstanding:"):
        assert word not in text.lower()
    assert lp.RESCAN_HINT in text

    # No arithmetic on an outstanding count: a subtraction here would be exactly the provisional
    # "old total minus this batch" figure the contract forbids presenting as fact.
    fn = _func(_tree(_PREP), "summarize_analysis_run")
    assert not [n for n in ast.walk(fn)
                if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)]


def test_k_the_summary_submitted_line_is_optional():
    text = lp.summarize_analysis_run({"sources_analyzed_this_run": 4, "analysis_seconds": 1.0})
    assert "Submitted this batch" not in text
    assert "Sources analyzed this batch:  4" in text


# --- GUI seam ------------------------------------------------------------------


def test_k_the_batch_widget_exists_in_preparation_with_the_constant_default():
    source = open(_GUI, "r", encoding="utf-8").read()
    block = source[source.index("LABEL_PREP_SECTION"):source.index("prep_status = gr.Markdown")]

    assert "prep_batch_size = gr.Number(" in block
    assert "value=fork_prep.DEFAULT_ANALYZE_BATCH_SIZE" in block, \
        "the default must be the preparation constant, not a literal buried in the GUI"
    assert "LABEL_PREP_BATCH_SIZE" in block
    assert "minimum=1" in block
    assert "maximum=" not in block, "a batch size is not a library cap"


def test_k_the_analyze_click_receives_the_live_batch_size():
    kwargs = _click_kwargs(_tree(_GUI), "prep_analyze_btn")
    names = [getattr(node, "id", None) for node in kwargs["inputs"].elts]

    assert names == ["prep_folder", "prep_recursive", "prep_batch_size", "prep_state"]
    assert names.index("prep_batch_size") < names.index("prep_state"), \
        "the live widget, not just gr.State"


def test_k_the_scan_click_does_not_take_the_batch_size():
    """Classification cannot depend on how much of its result one click later consumes."""
    kwargs = _click_kwargs(_tree(_GUI), "prep_scan_btn")
    names = [getattr(node, "id", None) for node in kwargs["inputs"].elts]

    assert names == ["prep_folder", "prep_recursive", "prep_state"]
    assert "prep_batch_size" not in names
    assert "batch_size" not in _body_code(_func(_tree(_GUI), "_prep_scan_impl"))


def _change_kwargs(tree: ast.Module, widget: str) -> Dict[str, ast.AST]:
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "change"
                and getattr(n.func.value, "id", None) == widget)
    return {kw.arg: kw.value for kw in call.keywords}


def test_k_the_batch_size_change_handler_does_not_invalidate():
    tree = _tree(_GUI)
    kwargs = _change_kwargs(tree, "prep_batch_size")

    assert getattr(kwargs["fn"], "id", None) == "_on_prep_batch_size_change"
    assert [getattr(n, "id", None) for n in kwargs["inputs"].elts] == \
        ["prep_batch_size", "prep_state"]

    body = _body_code(_func(tree, "_on_prep_batch_size_change"))
    assert "set_batch_size" in body
    for invalidator in ("set_folder", "set_recursive", "record_failure", "record_scan"):
        assert invalidator not in body, f"batch size must not route through {invalidator}"


def test_k_the_classification_change_handlers_still_invalidate():
    tree = _tree(_GUI)
    for widget, handler, transition in (
        ("prep_folder", "_on_prep_folder_change", "set_folder"),
        ("prep_recursive", "_on_prep_recursive_change", "set_recursive"),
    ):
        kwargs = _change_kwargs(tree, widget)
        assert getattr(kwargs["fn"], "id", None) == handler
        assert transition in _body_code(_func(tree, handler))


def test_k_the_batch_widget_never_touches_the_create_video_gate():
    tree = _tree(_GUI)

    source_outputs = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == "source_outputs")
    assert "prep_batch_size" not in [getattr(n, "id", None) for n in source_outputs.value.elts]

    render_inputs = [getattr(n, "id", None)
                     for n in _click_kwargs(tree, "process_btn")["inputs"].elts]
    assert "prep_batch_size" not in render_inputs

    body = _body_code(_func(tree, "_on_prep_batch_size_change"))
    for forbidden in ("source_state", "process_btn", "confirm_action", "resolve_for_render"):
        assert forbidden not in body


# --- the real handler, executed --------------------------------------------------


def _scan_with_outstanding(namespace, recorder, outstanding: int, prepared: int = 0):
    recorder.ready_paths = [f"/lib/f{i}.mp4" for i in range(outstanding + prepared)]
    recorder.classify_result = _classification_payload(prepared, outstanding, 0)
    scanned = namespace["_prep_scan_impl"]("/lib", True, lp.initial_state())
    recorder.probe_result = dict(recorder.classify_result, classifications=[])
    return scanned


def test_k_real_analyze_submits_exactly_one_batch_of_the_outstanding_set(gui):
    """250 outstanding, batch 100 -> the analyzer receives entries 1..100. Not 250."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 250)

    assert scanned.scan.needs_analysis_count == 250, "Scan still classifies the whole library"

    finished = namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 100)

    assert len(recorder.analyze_calls) == 1, "one analyzer call for the batch, no more"
    submitted = recorder.analyze_calls[0]["video_files"]
    assert len(submitted) == 100
    assert submitted == [f"/lib/new_{i}.mp4" for i in range(100)]
    assert "/lib/new_100.mp4" not in submitted

    assert finished.scan is None, "the now-stale counts must not survive the batch"
    assert "Submitted this batch:         100" in finished.report_text
    assert lp.RESCAN_HINT in finished.report_text


def test_k_real_analyze_submits_everything_when_fewer_remain_than_the_batch(gui):
    """37 remaining, batch 100 -> exactly 37, never padded and never truncated."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 37, prepared=1070)

    assert scanned.scan.needs_analysis_count == 37
    assert scanned.scan.prepared_count == 1070

    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 100)

    assert len(recorder.analyze_calls) == 1
    submitted = recorder.analyze_calls[0]["video_files"]
    assert len(submitted) == 37
    assert submitted == [f"/lib/new_{i}.mp4" for i in range(37)]


@pytest.mark.parametrize("batch,expected", [
    (1, 1), (50, 50), (100, 100), (250, 250),
    (None, 100), (0, 100), (7.9, 100), (True, 100),   # malformed -> the default, never a crash
])
def test_k_real_analyze_honours_the_live_batch_size(gui, batch, expected):
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 250)

    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), batch)

    assert len(recorder.analyze_calls[0]["video_files"]) == expected


def test_k_a_live_batch_size_differing_from_the_state_is_not_a_stale_scan(gui):
    """The guard covers folder and recursive only; a retuned batch is legitimate, not a refusal."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 250)
    assert scanned.batch_size == lp.DEFAULT_ANALYZE_BATCH_SIZE

    finished = namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 50)

    assert len(recorder.analyze_calls) == 1, "the run proceeded rather than being refused"
    assert len(recorder.analyze_calls[0]["video_files"]) == 50
    assert "refused" not in finished.notice.lower()
    assert not finished.report_text.startswith("❌")


def test_k_the_batch_path_adds_no_alternate_persistence(gui):
    """Everything durable stays the existing analyzer's. Bounding the input adds no write path."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 250)

    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 100)

    assert len(recorder.analyze_calls) == 1
    assert recorder.analyze_calls[0]["audio_profile"] is None, "P2: still media-neutral"
    assert recorder.beats_calls == []

    body = _body_code(_func(_tree(_GUI), "_prep_analyze_impl"))
    for forbidden in ("_checkpoint_cache", "_save_cache", "_analyze_single_video",
                      "_cache_path", "_load_cache", "json.dump"):
        assert forbidden not in body


def test_k_a_huge_library_is_classified_whole_and_submitted_bounded(gui):
    """NO_HARD_LIBRARY_CAP: 5000 sources may legitimately all need analysis."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 5000)

    assert scanned.scan.supported_count == 5000
    assert scanned.scan.needs_analysis_count == 5000, "the batch size is not a library cap"
    assert len(scanned.scan.subset_for_analysis()) == 5000
    assert "New / changed:     5000" in scanned.report_text

    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 100)

    assert len(recorder.analyze_calls[0]["video_files"]) == 100


def test_k_a_batch_larger_than_the_library_is_not_clamped_to_a_cap(gui):
    """A user may legitimately ask for one enormous batch; nothing may silently cap it."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 5000)

    namespace["_prep_analyze_impl"](scanned, _live_for(scanned.scan), 10_000)

    assert len(recorder.analyze_calls[0]["video_files"]) == 5000


def test_k_no_incremental_worker_response_was_implemented():
    """This task bounds the submission; it does NOT make an in-flight worker resumable.

    Pinned so a future reader does not infer a durability guarantee that does not exist.
    """
    worker = os.path.join(_REPO, "src", "auto_mode", "stage5_qwen_scene_worker.py")
    source = open(worker, "r", encoding="utf-8").read()
    for invented in ("incremental_response", "partial_response", "write_partial",
                     "checkpoint_response", "flush_response"):
        assert invented not in source

    prep_source = open(_PREP, "r", encoding="utf-8").read()
    assert "does not make an in-flight worker resumable" in prep_source


# ---------------------------------------------------------------------------
# K (R1). LIVE REPORT CONSISTENCY
#
# Keeping the scan is not the same as keeping its rendered text. The report quotes the batch size,
# so a retune that left it alone made the screen contradict itself: widget 50, button "Analyze next
# 50", report still claiming 100. Execution was always correct - the handler reads the live value -
# so this is reporting consistency, and the fix is presentation only.
# ---------------------------------------------------------------------------


def test_k_r1_retuning_the_batch_size_re_renders_the_report_from_the_same_scan():
    """The exact reported defect: 1107 outstanding, 100 -> 50, no re-scan."""
    state = lp.record_scan(lp.initial_state(), _outstanding_scan(1107))

    assert "Analyze batch:     100 per run" in state.report_text
    assert "This run submits the next 100" in state.report_text
    assert state.analyze_button_label() == "⚙️ Analyze next 100"

    retuned = lp.set_batch_size(state, 50)

    # the invariant that must NOT change
    assert retuned.scan is state.scan, "the SAME scan object must survive"
    assert retuned.can_analyze() is True
    assert retuned.batch_size == 50

    # the presentation that must now agree with it
    assert "Analyze batch:     50 per run" in retuned.report_text
    assert "This run submits the next 50" in retuned.report_text
    assert retuned.analyze_button_label() == "⚙️ Analyze next 50"

    # and the stale claim is gone
    assert "Analyze batch:     100 per run" not in retuned.report_text
    assert "This run submits the next 100" not in retuned.report_text

    # the classification itself is untouched: same counts, same outstanding set
    assert "New / changed:     1107" in retuned.report_text
    assert retuned.scan.needs_analysis_count == 1107
    assert retuned.subset_for_analysis() == state.subset_for_analysis()


def test_k_r1_a_second_retune_re_renders_again_and_still_keeps_the_scan():
    """50 -> 250: presentation only, every time."""
    state = lp.set_batch_size(lp.record_scan(lp.initial_state(), _outstanding_scan(1107)), 50)
    original_scan = state.scan

    retuned = lp.set_batch_size(state, 250)

    assert retuned.scan is original_scan
    assert retuned.batch_size == 250
    assert "Analyze batch:     250 per run" in retuned.report_text
    assert "This run submits the next 250" in retuned.report_text
    # the full stale line, not the bare "50" - which is a substring of "250"
    assert "Analyze batch:     50 per run" not in retuned.report_text
    assert "This run submits the next 50;" not in retuned.report_text
    assert retuned.analyze_button_label() == "⚙️ Analyze next 250"


def test_k_r1_the_rendered_report_always_matches_the_button():
    """The consistency property itself, swept across bounds and library sizes."""
    for outstanding in (1, 37, 100, 250, 1107, 5000):
        state = lp.record_scan(lp.initial_state(), _outstanding_scan(outstanding))
        for size in (1, 7, 50, 100, 250, 5000):
            retuned = lp.set_batch_size(state, size)
            submitted = min(size, outstanding)
            assert retuned.scan is state.scan

            if outstanding > size:
                assert f"Analyze batch:     {size} per run" in retuned.report_text
                assert f"This run submits the next {size}" in retuned.report_text
                assert retuned.analyze_button_label() == f"⚙️ Analyze next {size}"
            else:
                assert "Analyze batch:" not in retuned.report_text
                assert "This run submits all of them." in retuned.report_text
                assert retuned.analyze_button_label() == f"⚙️ Analyze {outstanding} remaining"

            assert len(retuned.subset_for_analysis_batch(size)) == submitted


def test_k_r1_a_retune_re_renders_but_never_re_classifies():
    """Presentation only: the rendered text is exactly what the recorded scan already says."""
    scan = _outstanding_scan(1107)
    state = lp.record_scan(lp.initial_state(), scan)

    retuned = lp.set_batch_size(state, 50)

    # identical to rendering the untouched scan directly at the new size
    assert retuned.report_text == scan.render_text(batch_size=50)

    # nothing about the classification moved
    assert retuned.scan is scan
    assert retuned.scan.classifications is scan.classifications
    assert retuned.scan.runtime == scan.runtime
    assert (retuned.scan.supported_count, retuned.scan.prepared_count,
            retuned.scan.unavailable_count) == (1107, 0, 0)
    assert retuned.scan.cache_identity_seconds == scan.cache_identity_seconds


@pytest.mark.parametrize("preserved", [
    lp.INTRO_TEXT,
    "❌ LIBRARY SCAN FAILED\nWinError 3: path not found",
    "❌ Preparation inputs changed since the scan. Press Scan Library again.",
    "PREPARATION BATCH COMPLETE\n\nSubmitted this batch:         100\n"
    "Sources analyzed this batch:  100\n\nPreparation batch finished.\n" + lp.RESCAN_HINT,
])
def test_k_r1_with_no_scan_the_existing_report_text_is_preserved(preserved):
    """A batch-size change may not overwrite the intro, a failure, or a finished batch's summary."""
    state = lp.PrepSessionState(report_text=preserved)
    assert state.scan is None

    retuned = lp.set_batch_size(state, 50)

    assert retuned.report_text == preserved, "no scan means nothing to re-render"
    assert retuned.batch_size == 50
    assert retuned.scan is None
    assert retuned.can_analyze() is False


def test_k_r1_the_real_no_scan_states_preserve_their_text():
    """The same property reached through the actual transitions rather than a hand-built state."""
    prepared = lp.record_scan(lp.initial_state(), _outstanding_scan(1107))

    for state, label in (
        (lp.initial_state(), "startup"),
        (lp.record_failure(prepared, "LIBRARY SCAN FAILED\nboom"), "failure"),
        (lp.record_analysis_complete(
            prepared, lp.summarize_analysis_run(
                {"sources_analyzed_this_run": 100, "analysis_seconds": 1.0}, submitted=100)),
         "batch complete"),
        (lp.set_folder(prepared, "OTHER"), "folder invalidation"),
        (lp.set_recursive(prepared, False), "recursive invalidation"),
    ):
        assert state.scan is None, label
        retuned = lp.set_batch_size(state, 50)
        assert retuned.report_text == state.report_text, label
        assert retuned.notice == state.notice, label
        assert retuned.batch_size == 50, label
        assert retuned.analyze_button_label() == "⚙️ Analyze New / Changed", label


def test_k_r1_a_batch_completion_summary_survives_a_retune():
    """Concretely: the rescan hint must still be on screen after the user fiddles with the box."""
    prepared = lp.record_scan(lp.initial_state(), _outstanding_scan(1107))
    finished = lp.record_analysis_complete(
        prepared, lp.summarize_analysis_run(
            {"sources_analyzed_this_run": 100, "qwen_jobs_this_run": 100,
             "analysis_seconds": 1144.0}, submitted=100))

    retuned = lp.set_batch_size(finished, 250)

    assert "PREPARATION BATCH COMPLETE" in retuned.report_text
    assert "Sources analyzed this batch:  100" in retuned.report_text
    assert lp.RESCAN_HINT in retuned.report_text
    assert lp.BATCH_FINISHED_TEXT in retuned.report_text
    assert "Analyze batch:" not in retuned.report_text, "no scan to render a batch line from"


def test_k_r1_set_batch_size_reaches_no_runtime_work_at_all():
    """Structural: re-rendering must not have smuggled in a probe, a scan or a classification."""
    body = _body_code(_func(_tree(_PREP), "set_batch_size"))

    for forbidden in (
        "classify_library_sources", "scan_library_folder", "analyze_video_sources",
        "_cache_path", "_load_cache", "_video_signature", "_save_cache", "_checkpoint_cache",
        "_qwen_backend_signature_token", "os.stat", "os.walk", "open(", "build_scan_result",
        "runtime_identity_from_classification", "RuntimeIdentity(", "_invalidated",
        "scan=None", "INTRO_TEXT", "LivePrepDeclaration",
    ):
        assert forbidden not in body, f"set_batch_size must not reach {forbidden}"

    # The only thing it may render with is the scan it already holds.
    assert "render_text" in body
    assert "state.scan.render_text" in body
    assert "normalize_batch_size" in body


def test_k_r1_the_handler_is_still_the_only_gui_seam_and_still_not_an_invalidation(gui):
    """The GUI wiring is unchanged by R1: same handler, same non-invalidating transition."""
    tree = _tree(_GUI)
    kwargs = _change_kwargs(tree, "prep_batch_size")

    assert getattr(kwargs["fn"], "id", None) == "_on_prep_batch_size_change"
    assert [getattr(n, "id", None) for n in kwargs["inputs"].elts] == \
        ["prep_batch_size", "prep_state"]

    body = _body_code(_func(tree, "_on_prep_batch_size_change"))
    assert body.strip() == "return _prep_ui_updates(fork_prep.set_batch_size(state, batch_size))"

    # and no runtime call was reached: the stub recorder saw nothing
    _namespace, recorder, _track = gui
    assert recorder.scan_calls == [] and recorder.classify_calls == []
    assert recorder.analyze_calls == [] and recorder.beats_calls == []


def test_k_r1_analyze_still_uses_the_live_widget_not_the_re_rendered_state(gui):
    """The queued-event design is unchanged: the live value wins even over a retuned state."""
    namespace, recorder, _track = gui
    scanned = _scan_with_outstanding(namespace, recorder, 250)

    # the user retuned to 50 (state re-rendered), but the widget now reads 25 at click time
    retuned = lp.set_batch_size(scanned, 50)
    assert retuned.batch_size == 50
    assert "This run submits the next 50" in retuned.report_text

    namespace["_prep_analyze_impl"](retuned, _live_for(retuned.scan), 25)

    assert len(recorder.analyze_calls) == 1
    assert len(recorder.analyze_calls[0]["video_files"]) == 25, "the LIVE widget, not the state"


def test_k_r1_folder_and_recursive_still_invalidate_after_a_retune():
    """The asymmetry survives R1: presentation re-render for batch size, real drop for the rest."""
    state = lp.set_batch_size(lp.record_scan(lp.initial_state(), _outstanding_scan(1107)), 50)
    assert state.scan is not None and "next 50" in state.report_text

    for change, label in ((lambda s: lp.set_folder(s, "OTHER"), "folder"),
                          (lambda s: lp.set_recursive(s, False), "recursive")):
        changed = change(state)
        assert changed.scan is None, label
        assert changed.can_analyze() is False, label
        assert changed.report_text == lp.INTRO_TEXT, label
        assert changed.batch_size == 50, "the chosen batch size still survives an invalidation"
