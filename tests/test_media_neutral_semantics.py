"""P2 — media-neutral Stage-5 semantics.

The one architectural claim this suite protects:

    STAGE 5 RECORDS INTRINSIC MEDIA TRUTH.
    STAGE 6 (and any future creative/director layer) INTERPRETS IT FOR THE CURRENT PROJECT.

Concretely: a source needs one semantic analysis per compatible *source identity* + *Qwen backend
identity* + *result-affecting media-semantic Qwen configuration* — and **not** one per edit style,
track, tempo, section layout, cut count or variation seed.

Three things are checked, three ways:

* the production worker's prompt, structurally and by its literal text (``ast``; no model is loaded);
* the worker request payloads the parent actually writes, by executing the real request-building
  bodies against a stubbed streaming runner;
* cache identity and the v2 → v3 generation boundary, by lifting the real identity and completion
  primitives out of ``video_analysis.py`` and executing them (the module cannot be imported on a bare
  interpreter — see CLAUDE.md).

No Qwen model runs, no GPU is needed, no render happens, and every path lives inside pytest's
``tmp_path``. The real runtime cache is never touched.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Sequence

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO, "src", "video_analysis.py")
_WORKER = os.path.join(_REPO, "src", "auto_mode", "stage5_qwen_scene_worker.py")
_GUI = os.path.join(_REPO, "src", "gui.py")
_AUTO_MODE = os.path.join(_REPO, "src", "auto_mode", "__init__.py")

# The exact conditioning the P2 real-material A/B validated. Split as two adjacent literals in the
# worker, so it is compared against the concatenated prompt rather than against source text.
NEUTRAL_INSTRUCTION = (
    "Assess the moment only from what is visually present; do not adapt the tags "
    "to music, song energy, edit style, or desired pacing. "
)

# What P2 must NOT have redesigned: the experiment validated the *existing* schema.
SCHEMA_FRAGMENTS = (
    "You are tagging one source-video moment for professional AMV/GMV editing. ",
    "Return JSON only. Keys: action_intensity, beauty_score, combat, chase, explosion, ",
    "character_focus, camera_motion, visual_quality as numbers 0..1; ",
    "emotion as one of soft,tension,hype,sad,neutral; ",
    "recommended_use as one of drop,soft,build,transition,flow,filler; ",
    "description under 12 words. Do not include markdown.",
)

# Every preset `_build_audio_visual_profile` can emit, plus the old mirrored default.
PRESETS = ("rhythmic_hype_gmv_amv", "cinematic_soft_amv", "hybrid_drop_story",
           "rhythmic_flow_gmv_amv", "rhythmic_gmv_amv")

PROFILE_A = {
    "smart_preset": "hybrid_drop_story", "tempo": 174.0, "beat_count": 812,
    "section_types": ["intro", "build", "drop", "outro"], "average_wave": 0.83,
    "cut_count": 240, "energy_peak": 0.97,
}
PROFILE_B = {
    "smart_preset": "cinematic_soft_amv", "tempo": 78.5, "beat_count": 190,
    "section_types": ["intro", "verse", "breakdown"], "average_wave": 0.19,
    "cut_count": 54, "energy_peak": 0.31,
}

_SECOND = 1_700_000_000_000_000_000
_QWEN_ENV = ("BEATSYNC_QWEN_MAX_WINDOWS", "BEATSYNC_QWEN_FRAME_WIDTH",
             "BEATSYNC_QWEN_MAX_NEW_TOKENS")


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _strip_docstrings(node: ast.AST) -> ast.AST:
    """Drop every docstring, at every nesting level.

    A shallow strip is not enough here: P2's own docstrings legitimately name `smart_preset`,
    `audio_profile` and the variation seed in order to say that none of them may be an input, and a
    nested function's docstring survives `ast.unparse` of its parent. Works on a deep copy, so the
    caller's tree is never mutated.
    """
    node = copy.deepcopy(node)
    for inner in ast.walk(node):
        body = getattr(inner, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
    return ast.fix_missing_locations(node)


def _body_code(node: ast.FunctionDef) -> str:
    """Executable body only — docstrings stripped, and the signature line excluded, so neither prose
    nor a retained-but-unused parameter name can satisfy or fail a check."""
    return "\n".join(ast.unparse(item) for item in _strip_docstrings(node).body)


def _executable_source(path: str) -> str:
    """A whole module with every docstring removed, at every nesting level."""
    with open(path, "r", encoding="utf-8") as handle:
        return ast.unparse(_strip_docstrings(ast.parse(handle.read())))


def _prompt_text(tree: ast.AST) -> str:
    """The worker prompt's literal pieces, concatenated, with the docstring excluded.

    `ast.unparse` normalises quoting, so the string constants themselves are what gets compared. The
    docstring must not be part of it: it names the retired `smart_preset` in order to say the prompt
    no longer carries one.
    """
    fn = _strip_docstrings(_func(tree, "_build_prompt"))
    return "".join(node.value for node in ast.walk(fn)
                   if isinstance(node, ast.Constant) and isinstance(node.value, str))


def _write(path: str, byte: bytes, size: int, mtime_ns: int = _SECOND) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(byte * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return os.path.abspath(path)


_IDENTITY_FUNCS = (
    "_safe_name", "_hash_text", "_env_int", "_qwen_max_windows", "_path_signature_token",
    "_bounded_fingerprint", "_full_fingerprint", "_backend_component_token",
    "_llama_version_token", "_resolve_qwen_backend_paths", "_qwen_backend_signature_token",
    "_qwen_config_token", "_video_signature", "_cache_path", "_same_source", "_is_count",
    "_stored_ai_cache_is_consistent", "_deterministic_analysis_completed",
    "_cache_entry_is_complete", "_load_cache",
)
_IDENTITY_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_FINGERPRINT_CHUNK",
                    "_FINGERPRINT_WHOLE_FILE_LIMIT", "_FINGERPRINT_DIGEST_SIZE",
                    "_NO_AI_CONFIG_TOKEN", "_DETERMINISTIC_SCORING_KEY")


def _load_identity(root: str, cache_dir: str) -> Dict[str, Any]:
    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    models = os.path.join(root, "bin", "models")
    llama = os.path.join(root, "bin", "llama-bin-win-vulkan-x64")
    for directory in (models, llama, cache_dir):
        os.makedirs(directory, exist_ok=True)

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "hashlib": hashlib, "subprocess": subprocess,
        "Any": Any, "Dict": Dict, "List": List, "Sequence": Sequence,
        "__builtins__": __builtins__,
        "ROOT_DIR": root,
        "DEFAULT_QWEN_MODEL_DIR": models,
        "DEFAULT_QWEN_GGUF_MODEL": os.path.join(models, "Qwen3VL-2B-Instruct-Q8_0.gguf"),
        "DEFAULT_QWEN_MMPROJ_MODEL": os.path.join(models, "mmproj-Qwen3VL-2B-Instruct-F16.gguf"),
        "DEFAULT_LLAMA_CPP_DIR": llama,
        "VIDEO_ANALYSIS_CACHE_DIR": cache_dir,
        "_LLAMA_VERSION_TOKENS": {},
    }
    found: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _IDENTITY_FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _IDENTITY_CONSTS:
                    exec(compile(ast.Module(body=[node], type_ignores=[]), "<const>", "exec"),
                         namespace)
    missing = [name for name in _IDENTITY_FUNCS if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"
    for name in _IDENTITY_CONSTS:
        assert name in namespace, f"{name} missing from video_analysis.py"
    for name in _IDENTITY_FUNCS:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    namespace["_MODELS"] = models
    namespace["_LLAMA"] = llama
    namespace["_CACHE"] = cache_dir
    return namespace


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
    """Identity primitives with a complete, readable fake Qwen backend."""
    ns = _load_identity(str(tmp_path / "root"), str(tmp_path / "cache"))
    chunk = ns["_FINGERPRINT_CHUNK"]
    for name in ("Qwen3VL-2B-Instruct-Q8_0.gguf", "mmproj-Qwen3VL-2B-Instruct-F16.gguf"):
        _write(os.path.join(ns["_MODELS"], name), b"A", 4 * chunk)
    for name in ("llama-server.exe", "llama-mtmd-cli.exe"):
        _write(os.path.join(ns["_LLAMA"], name), b"B", 2048)
    return ns


@pytest.fixture
def worker_tree():
    return _tree(_WORKER)


# ===========================================================================
# A. THE WORKER PROMPT
# ===========================================================================


def test_a_build_prompt_takes_no_arguments(worker_tree):
    """It cannot be conditioned on anything, because it is handed nothing."""
    fn = _func(worker_tree, "_build_prompt")
    args = fn.args
    assert not args.args and not args.posonlyargs and not args.kwonlyargs, ast.unparse(args)
    assert args.vararg is None and args.kwarg is None


def test_a_the_prompt_carries_the_validated_media_neutral_instruction(worker_tree):
    prompt = _prompt_text(worker_tree)
    assert NEUTRAL_INSTRUCTION in prompt, prompt


def test_a_the_prompt_carries_no_edit_style_conditioning(worker_tree):
    fn = _func(worker_tree, "_build_prompt")
    prompt = _prompt_text(worker_tree)
    code = _body_code(fn)

    for gone in ("The music edit style is", "music edit style", "rhythmic_gmv_amv",
                 "smart_preset"):
        assert gone not in prompt, f"prompt still conditions on {gone!r}"
        assert gone not in code, f"_build_prompt still references {gone!r}"
    assert "style_hint" not in code
    # nothing is interpolated at all any more: a plain literal, no f-string
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.JoinedStr)], (
        "the prompt must be a constant; an f-string is how style context got in")


def test_a_the_semantic_schema_contract_is_untouched(worker_tree):
    """P2 changes semantic conditioning, not the schema. Redesigning `recommended_use` or the eight
    numeric keys at the same time would have invalidated the experiment's own evidence and widened
    the cold-rebuild boundary."""
    prompt = _prompt_text(worker_tree)
    for fragment in SCHEMA_FRAGMENTS:
        assert fragment in prompt, f"prompt drifted: missing {fragment!r}"

    source = open(_WORKER, "r", encoding="utf-8").read()
    assert '"recommended_use"' in source or "'recommended_use'" in source
    tree = ast.parse(source)
    numeric = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and getattr(n.targets[0], "id", None) == "NUMERIC_KEYS")
    keys = ast.literal_eval(numeric.value)
    assert tuple(keys) == (
        "action_intensity", "beauty_score", "combat", "chase", "explosion",
        "character_focus", "camera_motion", "visual_quality",
    ), keys


def test_a_the_worker_never_reads_an_audio_profile_from_the_request(worker_tree):
    assert "audio_profile" not in _body_code(_func(worker_tree, "main"))
    for node in ast.walk(worker_tree):
        if isinstance(node, (ast.Subscript, ast.Call)):
            rendered = ast.unparse(node)
            assert 'request.get("audio_profile")' not in rendered
            assert "request.get('audio_profile')" not in rendered
            assert 'request["audio_profile"]' not in rendered
    # and no executable statement anywhere in the worker mentions it
    executable = _executable_source(_WORKER)
    assert "audio_profile" not in executable
    assert "smart_preset" not in executable


def test_a_main_builds_the_prompt_with_no_context(worker_tree):
    calls = [n for n in ast.walk(_func(worker_tree, "main"))
             if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_build_prompt"]
    assert len(calls) == 1
    assert not calls[0].args and not calls[0].keywords, ast.unparse(calls[0])


# ===========================================================================
# B. THE WORKER REQUEST PAYLOADS
#
# The real request-building bodies, executed. A structural check would not catch a key added back
# via `request.update(...)`, and the request JSON is what the worker actually receives.
# ===========================================================================


_RUNNER_FUNCS = ("_run_qwen_worker", "_run_qwen_worker_batch", "_hash_text",
                 "_qwen_worker_stdout_printer", "_short_qwen_error")


class _Outcome:
    timed_out = False
    launch_error = ""
    returncode = 0
    stderr_tail = ""


class _Result:
    outcome = _Outcome()
    response_error = ""
    response: Dict[str, Any] = {}


def _runner_namespace(tmp_path, captured: List[Dict[str, Any]]) -> Dict[str, Any]:
    """`_run_qwen_worker`/`_run_qwen_worker_batch` with the streaming runner stubbed out."""
    import time as time_module
    import types

    with open(_VA, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)

    cache_dir = str(tmp_path / "cache")
    os.makedirs(cache_dir, exist_ok=True)

    fork_qwen = types.SimpleNamespace(
        run_qwen_worker=lambda *args, **kwargs: _Result())
    fork_progress = types.SimpleNamespace(
        emit=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)

    def qwen_worker_environment():
        return {}

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "hashlib": hashlib, "time": time_module,
        "subprocess": subprocess, "sys": types.SimpleNamespace(executable="python"),
        "Any": Any, "Dict": Dict, "List": List, "Sequence": Sequence,
        "__builtins__": __builtins__,
        "ROOT_DIR": str(tmp_path / "root"),
        "VIDEO_ANALYSIS_CACHE_DIR": cache_dir,
        "fork_qwen": fork_qwen,
        "fork_progress": fork_progress,
        "_qwen_worker_environment": qwen_worker_environment,
    }
    found: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _RUNNER_FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
    missing = [name for name in _RUNNER_FUNCS if name not in found]
    assert not missing, f"missing from video_analysis.py: {missing}"
    for name in _RUNNER_FUNCS:
        exec(compile("from __future__ import annotations\n" + found[name], f"<{name}>", "exec"),
             namespace)
    namespace["_CACHE"] = cache_dir
    assert captured is not None
    return namespace


def _written_requests(cache_dir: str, prefix: str) -> List[Dict[str, Any]]:
    requests = []
    for name in sorted(os.listdir(cache_dir)):
        if name.startswith(prefix) and name.endswith(".json"):
            with open(os.path.join(cache_dir, name), "r", encoding="utf-8") as handle:
                requests.append(json.load(handle))
    return requests


def test_b_the_single_worker_request_carries_no_audio_profile(tmp_path):
    ns = _runner_namespace(tmp_path, [])
    ns["_run_qwen_worker"](
        video_file="C:\\lib\\clip.mp4", fps=25.0,
        candidates=[{"id": "c0", "start": 1.0, "end": 2.0}],
        qwen_model_path="m.gguf", use_gpu=False)

    requests = _written_requests(ns["_CACHE"], "qwen_request_")
    assert len(requests) == 1
    request = requests[0]
    assert set(request) == {"video_file", "fps", "qwen_model_path", "use_gpu", "candidates"}
    assert "audio_profile" not in request
    assert "smart_preset" not in json.dumps(request)


def test_b_the_batch_worker_request_carries_no_audio_profile(tmp_path):
    ns = _runner_namespace(tmp_path, [])
    ns["_run_qwen_worker_batch"](
        jobs=[{"job_id": "1", "video_file": "C:\\lib\\a.mp4", "fps": 24.0,
               "candidates": [{"id": "c0", "start": 0.0, "end": 1.0}]},
              {"job_id": "2", "video_file": "C:\\lib\\b.mp4", "fps": 30.0,
               "candidates": [{"id": "c1", "start": 2.0, "end": 3.0}]}],
        qwen_model_path="m.gguf", use_gpu=False)

    requests = _written_requests(ns["_CACHE"], "qwen_batch_request_")
    assert len(requests) == 1
    request = requests[0]
    assert set(request) == {"jobs", "qwen_model_path", "use_gpu"}
    assert "audio_profile" not in request
    assert "smart_preset" not in json.dumps(request)


def test_b_neither_runner_accepts_an_audio_profile_parameter():
    tree = _tree(_VA)
    for name in ("_run_qwen_worker", "_run_qwen_worker_batch",
                 "_annotate_candidates_with_qwen", "_complete_deferred_qwen",
                 "_complete_deferred_qwen_batch", "_analyze_single_video",
                 "classify_library_sources"):
        fn = _func(tree, name)
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert "audio_profile" not in params, f"{name} still forwards audio_profile"


# ===========================================================================
# C. THE CENTRAL INVARIANT — AN AUDIO PROFILE HAS ZERO STAGE-5 AI EFFECT
# ===========================================================================


def test_c_the_retained_public_parameter_is_used_by_nothing():
    """`analyze_video_sources(..., audio_profile=None, ...)` survives deliberately, so existing
    callers (`auto_mode.analyze_beats_auto`) need no change. It must reach nothing."""
    tree = _tree(_VA)
    fn = _func(tree, "analyze_video_sources")
    params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
    assert "audio_profile" in params, "the compatibility signature is retained on purpose"
    assert "audio_profile" not in _body_code(fn), "and it must have zero effect"

    # the established caller still passes it, which is the whole point of keeping it
    caller = _body_code(_func(_tree(_AUTO_MODE), "analyze_beats_auto"))
    assert "audio_profile=audio_visual_profile" in caller


@pytest.mark.parametrize("preset", PRESETS)
def test_c_an_edit_style_cannot_be_handed_to_the_config_token(va, preset):
    """The strongest form of the invariant: the seam does not merely ignore a preset, it rejects one.

    Every value `_build_audio_visual_profile` can emit is tried, so this is the full product-level
    list rather than a sample.
    """
    baseline = va["_qwen_config_token"]()
    assert baseline.startswith("cfg_")

    for attempt in ({"smart_preset": preset}, preset):
        with pytest.raises(TypeError):
            va["_qwen_config_token"](attempt)
    assert va["_qwen_config_token"]() == baseline


def test_c_two_very_different_renders_share_one_ai_cache_path(va, tmp_path):
    """The central P2 invariant, at the production key seam.

    PROFILE_A and PROFILE_B differ in preset, tempo, beat count, sections, energy and cut count —
    every downstream edit fact. The Stage-5 AI key is identical, because none of it is an input.
    """
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    assert PROFILE_A != PROFILE_B
    assert PROFILE_A["smart_preset"] != PROFILE_B["smart_preset"]

    token = va["_qwen_backend_signature_token"](None)
    keys = set()
    for _profile in (PROFILE_A, PROFILE_B):
        keys.add(va["_cache_path"](clip, True, None, backend_token=token,
                                   config_token=va["_qwen_config_token"]()))
    assert len(keys) == 1 and None not in keys

    # and the unthreaded form agrees, so a caller cannot re-key by taking the slow path
    assert va["_cache_path"](clip, True, None) == keys.pop()


def test_c_a_warm_source_stays_prepared_whatever_the_render_wants(va, tmp_path):
    """End to end over the real loader: one record, reused under every profile."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    path = va["_cache_path"](clip, True, None)
    record = {
        "analysis_version": va["ANALYSIS_VERSION"],
        "cache_contract": va["CACHE_CONTRACT_VERSION"],
        "video_file": clip,
        "candidates": [{"id": "c1", "ai_analyzed": True}, {"id": "c2", "ai_analyzed": True}],
        "ai_enabled": True,
        "timings": {va["_DETERMINISTIC_SCORING_KEY"]: 1.0,
                    "qwen_frame_count": 2, "qwen_tag_count": 2},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle)

    for _profile in (PROFILE_A, PROFILE_B, {}, None):
        key = va["_cache_path"](clip, True, None)
        assert key == path
        assert va["_load_cache"](key, require_ai=True, expected_video_file=clip)


@pytest.mark.parametrize("env,expect_rekey", [
    ({"BEATSYNC_QWEN_MAX_WINDOWS": "60"}, True),
    ({"BEATSYNC_QWEN_FRAME_WIDTH": "384"}, True),
    ({"BEATSYNC_QWEN_MAX_NEW_TOKENS": "64"}, True),
    ({"BEATSYNC_QWEN_MAX_WINDOWS": "120"}, False),      # the effective default
    ({"BEATSYNC_QWEN_LLAMA_SLOTS": "8"}, False),        # runtime-only
    ({"BEATSYNC_QWEN_BATCH_VIDEOS": "0"}, False),       # runtime-only
])
def test_c_media_semantic_configuration_still_re_keys(va, tmp_path, env, expect_rekey):
    """Removing the edit style must not have loosened the three settings that genuinely change what
    the model sees or produces."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    baseline = va["_cache_path"](clip, True, None)
    os.environ.update(env)
    try:
        changed = va["_cache_path"](clip, True, None)
    finally:
        for name in env:
            os.environ.pop(name, None)

    assert (changed != baseline) is expect_rekey, (env, expect_rekey)


def test_c_backend_and_source_identity_are_unchanged_by_p2(va, tmp_path):
    """P2 removes exactly one identity input. Everything else about D2 identity still holds."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    ai_key = va["_cache_path"](clip, True, None)
    no_ai_key = va["_cache_path"](clip, False, None)
    assert ai_key != no_ai_key, "a no_ai run keeps its own distinct identity"

    # a swapped model still re-keys
    chunk = va["_FINGERPRINT_CHUNK"]
    _write(os.path.join(va["_MODELS"], "Qwen3VL-2B-Instruct-Q8_0.gguf"), b"C", 4 * chunk,
           _SECOND + 7)
    va["_LLAMA_VERSION_TOKENS"].clear()
    assert va["_cache_path"](clip, True, None) != ai_key

    # changed content behind identical metadata still re-keys
    _write(clip, b"w", 4096, _SECOND)
    assert va["_cache_path"](clip, False, None) != no_ai_key

    # an unprovable source still fails closed
    assert va["_cache_path"](str(tmp_path / "library" / "gone.mp4"), False, None) is None


# ===========================================================================
# D. THE v2 -> v3 GENERATION BOUNDARY
# ===========================================================================


def test_d_the_contract_constant_is_v3_and_the_analysis_version_is_not():
    source = open(_VA, "r", encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_d_a_newly_born_record_carries_the_v3_marker():
    """Stamped in `_analyze_single_video`, where records are born — never by the writer."""
    tree = _tree(_VA)
    born = _body_code(_func(tree, "_analyze_single_video"))
    assert "'cache_contract': CACHE_CONTRACT_VERSION" in born
    assert "cache_contract" not in _body_code(_func(tree, "_save_cache")), (
        "the writer stays a transport primitive")


def test_d_a_valid_v2_shaped_record_is_not_accepted_as_v3(va):
    """The only thing wrong with it is its generation, and that is enough."""
    record = {
        "analysis_version": va["ANALYSIS_VERSION"],
        "cache_contract": "stage5_cache_v2",
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "candidates": [{"id": "a", "ai_analyzed": True}, {"id": "b", "ai_analyzed": True}],
        "candidate_count": 2,
        "timings": {"total_seconds": 4.4, va["_DETERMINISTIC_SCORING_KEY"]: 0.4,
                    "qwen_frame_count": 2, "qwen_tag_count": 2},
        "ai_enabled": True, "ai_deferred": False,
    }
    # complete in every respect EXCEPT the generation
    assert va["_cache_entry_is_complete"](dict(record, cache_contract="stage5_cache_v3"),
                                         require_ai=True) is True
    for require_ai in (True, False):
        assert va["_cache_entry_is_complete"](record, require_ai=require_ai) is False


def test_d_a_v2_record_on_disk_is_never_even_looked_up(va, tmp_path):
    """Two independent barriers: the key differs, and the loader would reject the payload anyway."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    v3_key = va["_cache_path"](clip, True, None)

    # a v2-generation record, written under the filename a v2 build would have chosen
    v2_signature = va["_hash_text"]("|".join([
        "stage5_cache_v2", va["ANALYSIS_VERSION"], os.path.abspath(clip),
        str(os.path.getsize(clip)), str(os.stat(clip).st_mtime_ns),
        va["_bounded_fingerprint"](clip, os.path.getsize(clip)),
        va["_qwen_backend_signature_token"](None), va["_qwen_config_token"](),
    ]), length=24)
    v2_key = os.path.join(va["_CACHE"],
                          f"{va['_hash_text']('clip', 8)}_{v2_signature}.json")
    payload = {
        "analysis_version": va["ANALYSIS_VERSION"], "cache_contract": "stage5_cache_v2",
        "video_file": clip, "candidates": [{"id": "a", "ai_analyzed": True}],
        "ai_enabled": True,
        "timings": {va["_DETERMINISTIC_SCORING_KEY"]: 1.0,
                    "qwen_frame_count": 1, "qwen_tag_count": 1},
    }
    with open(v2_key, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)

    assert v2_key != v3_key, "the generation is the first signature component, so the key differs"
    assert not os.path.exists(v3_key), "a cold v3 rebuild is the intended cost"
    # and even if something handed the old file straight to the loader:
    assert va["_load_cache"](v2_key, require_ai=True, expected_video_file=clip) is None


def test_d_there_is_no_migration_and_no_automatic_deletion():
    """A v2 → v3 compatibility loader, a rewriter or a cache sweeper are all out of scope, and the
    old records stay on disk by the retention policy.

    Executable code only: the contract constant's own comment legitimately explains what v2 was.
    """
    executable = _executable_source(_VA)
    for forbidden in ("stage5_cache_v2", "_migrate", "migrate_cache", "LEGACY_CONTRACT",
                      "shutil.rmtree", "os.unlink", "glob.glob", "os.scandir"):
        assert forbidden not in executable, f"video_analysis.py must not contain {forbidden}"

    # the only record-removing call in the module is `_save_cache`'s own temp cleanup
    tree = _tree(_VA)
    removers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n).startswith("os.remove(")]
    assert len(removers) == 1, [ast.unparse(n) for n in removers]
    assert ast.unparse(removers[0]) == "os.remove(tmp_path)"
    assert "os.remove(tmp_path)" in _body_code(_func(tree, "_save_cache"))


def test_d_a_v2_record_is_orphaned_not_mutated(va, tmp_path):
    """Byte-for-byte: classification and lookup leave the old generation exactly as it was."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    stale = os.path.join(va["_CACHE"], "deadbeef_" + "0" * 24 + ".json")
    payload = {"analysis_version": va["ANALYSIS_VERSION"], "cache_contract": "stage5_cache_v2",
               "video_file": clip, "candidates": [], "ai_enabled": True, "timings": {}}
    with open(stale, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    with open(stale, "rb") as handle:
        before = handle.read()

    assert va["_load_cache"](va["_cache_path"](clip, True, None), require_ai=True,
                             expected_video_file=clip) is None
    with open(stale, "rb") as handle:
        assert handle.read() == before


# ===========================================================================
# E. CREATIVE STATE STAYS DOWNSTREAM
#
# The existing seed invariant is pinned by tests/test_creative_seed.py; what P2 adds is the broader
# boundary. No future parameter is invented here — the rule is asserted over today's real APIs.
# ===========================================================================


_CREATIVE_WORDS = ("seed", "creative", "variation", "master_seed", "freestyle", "director",
                   "smart_preset", "audio_profile", "tempo")


@pytest.mark.parametrize("name", [
    "_video_signature", "_cache_path", "_qwen_config_token", "_qwen_backend_signature_token",
    "_cache_entry_is_complete", "_stored_ai_cache_is_consistent", "_qwen_job_completed",
    "_checkpoint_cache", "_save_cache", "classify_library_sources",
])
def test_e_no_identity_or_completion_function_knows_about_creative_state(name):
    """Whole words, so `os.makedirs(directory)` is not read as a Director reference."""
    body = _body_code(_func(_tree(_VA), name)).lower()
    for word in _CREATIVE_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", body), f"{name} mentions {word!r}"


def test_e_the_worker_knows_nothing_about_creative_state():
    """Stage 5's process boundary is where the separation is easiest to lose: whatever the parent
    stops sending, the worker must also stop being able to ask for.

    "edit style" is deliberately absent from the word list: the media-neutral instruction itself
    names it, in order to tell the model *not* to consider it. Section A pins that the conditioning
    phrase ("The music edit style is ...") is gone, which is the thing that mattered.
    """
    executable = _executable_source(_WORKER).lower()
    for word in ("smart_preset", "audio_profile", "variation", "master_seed", "freestyle",
                 "tempo", "section_types", "beat_count"):
        assert not re.search(rf"\b{re.escape(word)}\b", executable), (
            f"the worker must not know about {word!r}")


def test_e_no_director_or_freestyle_machinery_was_added():
    """P2 preserves the architectural space for those modes; it does not build them, and it adds no
    speculative abstraction for them either.

    **Amended by Variant Lab V1 R1 (C2).** `master_seed` was speculative when P2 wrote this; C2
    implements it as a GUI generator input, and `_fresh_variant_master_seed` is named for exactly
    what it does. The guard was therefore split rather than evaded by renaming the function: the
    **Stage-5 side** — `video_analysis.py`, the Qwen worker and `library_prep.py` — must still know
    nothing about a master seed, which is the architectural property P2 actually cared about, and
    every other token stays forbidden everywhere including the GUI.

    **Amended again by AI Director V1, by exactly the same rule.** `director` moves from
    "speculative everywhere" to "speculative on the Stage-5 side", because that is the half P2
    actually cared about: a Director may exist, but **Stage 5 must never hear of it**. The GUI now
    legitimately defines `_on_generate_director_proposal` and `_on_apply_director_proposal`, and
    the names are kept rather than disguised. `freestyle`, `shortlist`, `second_pass` and
    `interpretation_mode` are untouched and still forbidden everywhere, including the GUI — the
    Director is a *Stage-6-and-above creative producer*, not a second interpretation pass over the
    persisted library, and nothing here licenses one.
    """
    speculative_everywhere = ("freestyle", "shortlist", "second_pass", "interpretation_mode")
    stage5_side = (_VA, _WORKER, os.path.join(_REPO, "src", "beatsync_fork", "library_prep.py"))

    for path in stage5_side + (_GUI,):
        source = open(path, "r", encoding="utf-8").read()
        tree = ast.parse(source)
        defined = {n.name.lower() for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        forbidden = speculative_everywhere
        if path in stage5_side:
            forbidden = forbidden + ("master_seed", "director", "proposal")
        for speculative in forbidden:
            assert not any(speculative in name for name in defined), (
                f"{path} defines speculative {speculative} machinery")

    # The positive half: the accepted Director surface really is in the GUI and only there.
    gui_defined = {n.name for n in ast.walk(ast.parse(open(_GUI, encoding="utf-8").read()))
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert {"_on_generate_director_proposal", "_on_apply_director_proposal"} <= gui_defined


def _identifier_parts(source: str) -> set:
    """Every identifier in `source`, plus its `snake_case` and `camelCase` parts, lowercased.

    **Whole-word matching is not enough here, and a mutation proved it.** `\\bdirector\\b` does not
    match inside `DIRECTOR_PROPOSAL_INSTRUCTION`, because `_` is a word character — so a leak in
    exactly the shape a real leak would take (a compound name) walked straight past the guard.
    Plain substring matching is not the fix either: `director` is a substring of `directory`, and
    `os.makedirs(directory)` must not read as a Director reference. Decomposing identifiers answers
    both: `directory` yields `{"directory"}` and never `"director"`, while
    `DIRECTOR_PROPOSAL_INSTRUCTION` and `DirectorProposal` both yield `{"director", "proposal", …}`.
    """
    parts = set()
    for identifier in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source):
        lowered = identifier.lower()
        parts.add(lowered)
        parts.update(lowered.split("_"))
        parts.update(piece.lower() for piece in re.findall(r"[A-Z]?[a-z0-9]+", identifier))
    return parts


def test_e_the_director_vocabulary_reaches_no_stage_5_surface():
    """**§34.** `DIRECTOR_CHANGES_PERSISTED_MEDIA_SEMANTICS = NO`.

    Stage 5 records intrinsic media truth; the Director is creative interpretation and lives
    entirely above it. So no Stage-5 file, no Qwen request builder, no prompt and no persisted
    record may so much as name it — not even as one part of a compound identifier, which is the
    shape a real leak actually takes.

    The six control names are deliberately **not** repeated here:
    `test_creative_controls_seam.py` already pins their absence from every identity and completion
    function, executably as well as structurally, and a second copy of that list is a second place
    for it to go stale.
    """
    director_words = ("director", "proposal", "instruction", "explanation",
                      "directorproposal", "creativerecipe")
    paths = (_VA, _WORKER, os.path.join(_REPO, "src", "beatsync_fork", "library_prep.py"),
             os.path.join(_REPO, "src", "auto_mode", "stage6_av_planner.py"))

    for path in paths:
        parts = _identifier_parts(_executable_source(path))
        for word in director_words:
            assert word not in parts, f"{os.path.basename(path)} mentions {word!r}"

    # the decomposition really does tell a leak from an innocent word
    assert "director" not in _identifier_parts("os.makedirs(directory)")
    assert "director" in _identifier_parts("DIRECTOR_PROPOSAL_INSTRUCTION = 'x'")
    assert "proposal" in _identifier_parts("class DirectorProposal: pass")


def test_e_the_qwen_request_builders_and_prompt_never_see_a_director():
    """The process boundary, where separation is easiest to lose: whatever the parent stops
    sending, the worker must also stop being able to ask for."""
    tree = _tree(_VA)
    for builder in ("_run_qwen_worker", "_run_qwen_worker_batch"):
        body = _body_code(_func(tree, builder)).lower()
        for word in ("director", "proposal", "instruction", "explanation", "creative"):
            assert not re.search(rf"\b{re.escape(word)}\b", body), f"{builder} mentions {word!r}"

    prompt = _body_code(_func(_tree(_WORKER), "_build_prompt")).lower()
    for word in ("director", "proposal", "instruction", "editing intention", "creative"):
        assert not re.search(rf"\b{re.escape(word)}\b", prompt), f"the prompt mentions {word!r}"


def test_e_the_director_does_not_move_the_cache_contract_or_analysis_version():
    """`DIRECTOR_CHANGES_STAGE5_CACHE_IDENTITY = NO`. The Director re-plans; it never re-analyses,
    so neither constant may move and no cold rebuild may be charged for it."""
    values = {}
    for node in ast.walk(_tree(_VA)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                        "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION"}:
                    values[target.id] = node.value.value

    assert values["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"
    assert values["ANALYSIS_VERSION"] == "auto_av_analysis_v8_llama_vulkan_batched"


def test_e_the_director_module_knows_nothing_about_stage_5_or_a_cache():
    """The other direction, and the reason the Director is media-blind in V1: there is nowhere in
    the pure module for a frame, a filename, a semantic record or a cache token to enter."""
    director = os.path.join(_REPO, "src", "beatsync_fork", "director.py")
    source = _executable_source(director).lower()
    for word in ("cache", "cache_contract_version", "analysis_version", "video_signature",
                 "audio_visual_profile", "beat_info", "semantics", "candidate", "frame",
                 "mmproj", "qwen", "stage5", "video_analysis", "smart_preset", "audio_profile",
                 "tempo", "sections", "source_file", "filename"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"director.py mentions {word!r}"


def test_e_stage_6_scoring_and_planning_are_untouched():
    """P2 changes what Stage 5 persists, not how Stage 6 reads it."""
    planner = os.path.join(_REPO, "src", "auto_mode", "stage6_av_planner.py")
    source = open(planner, "r", encoding="utf-8").read()
    for forbidden in ("CACHE_CONTRACT_VERSION", "media_neutral", "audio_profile"):
        assert forbidden not in source, f"the planner must not learn about {forbidden}"
    # the planner still receives the music-aware bus it always did
    assert "beat_info" in source and "sections" in source
