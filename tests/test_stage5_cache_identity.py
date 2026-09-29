"""Stage 5 cache identity + contract (D2).

`video_analysis.py` cannot be imported on a bare interpreter, but the identity primitives D2
introduces are stdlib-only. This suite lifts the real definitions out of the production file by their
`ast` source ranges and executes them, so the behaviour asserted here is the production behaviour
rather than a description of it.

Every path is inside pytest's ``tmp_path``. The real runtime cache is never touched.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
from typing import Any, Dict, List

import pytest

_VIDEO_ANALYSIS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "video_analysis.py")

_FUNCS = (
    "_safe_name", "_hash_text", "_env_int", "_qwen_max_windows", "_path_signature_token",
    "_bounded_fingerprint", "_full_fingerprint", "_backend_component_token",
    "_llama_version_token", "_resolve_qwen_backend_paths", "_qwen_backend_signature_token",
    "_qwen_config_token", "_video_signature", "_cache_path",
    "_is_count", "_stored_ai_cache_is_consistent", "_deterministic_analysis_completed",
    "_cache_entry_is_complete",
)
_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_FINGERPRINT_CHUNK",
           "_FINGERPRINT_WHOLE_FILE_LIMIT", "_FINGERPRINT_DIGEST_SIZE", "_NO_AI_CONFIG_TOKEN",
           "_DETERMINISTIC_SCORING_KEY")

_QWEN_ENV = ("BEATSYNC_QWEN_MAX_WINDOWS", "BEATSYNC_QWEN_FRAME_WIDTH",
             "BEATSYNC_QWEN_MAX_NEW_TOKENS")
_SECOND = 1_700_000_000_000_000_000


def _load(root: str, cache_dir: str) -> Dict[str, Any]:
    """Exec the production identity definitions in a stdlib-only namespace rooted at `root`."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    models = os.path.join(root, "bin", "models")
    llama = os.path.join(root, "bin", "llama-bin-win-vulkan-x64")
    for directory in (models, llama, cache_dir):
        os.makedirs(directory, exist_ok=True)

    namespace: Dict[str, Any] = {
        "os": os, "json": json, "hashlib": hashlib, "subprocess": subprocess,
        "Any": Any, "Dict": Dict, "List": List, "__builtins__": __builtins__,
        "ROOT_DIR": root,
        "DEFAULT_QWEN_MODEL_DIR": models,
        "DEFAULT_QWEN_GGUF_MODEL": os.path.join(models, "Qwen3VL-2B-Instruct-Q8_0.gguf"),
        "DEFAULT_QWEN_MMPROJ_MODEL": os.path.join(models, "mmproj-Qwen3VL-2B-Instruct-F16.gguf"),
        "DEFAULT_LLAMA_CPP_DIR": llama,
        "VIDEO_ANALYSIS_CACHE_DIR": cache_dir,
        "_LLAMA_VERSION_TOKENS": {},
    }
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCS:
            found[node.name] = ast.get_source_segment(source, node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _CONSTS:
                    # exec, not literal_eval: some constants are expressions (1 << 20)
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
    namespace["_LLAMA"] = llama
    return namespace


def _write(path: str, byte: bytes, size: int, mtime_ns: int = _SECOND) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(byte * size)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


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
    ns = _load(str(tmp_path / "root"), str(tmp_path / "cache"))
    chunk = ns["_FINGERPRINT_CHUNK"]
    for name, full in (("Qwen3VL-2B-Instruct-Q8_0.gguf", False),
                       ("mmproj-Qwen3VL-2B-Instruct-F16.gguf", False)):
        _write(os.path.join(ns["_MODELS"], name), b"A", 4 * chunk)
    for name in ("llama-server.exe", "llama-mtmd-cli.exe"):
        _write(os.path.join(ns["_LLAMA"], name), b"B", 2048)
    return ns


# ---------------------------------------------------------------------------
# the one generation constant
# ---------------------------------------------------------------------------


def test_one_constant_owns_identity_and_the_persisted_contract(va):
    """`CACHE_CONTRACT_VERSION` is both the first signature component and the stored marker, so a key
    and its payload can never disagree about which generation they belong to."""
    assert va["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"

    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    signature = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_video_signature")
    rule = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_cache_entry_is_complete")
    assert "CACHE_CONTRACT_VERSION" in ast.unparse(signature), "must key the signature"
    assert "CACHE_CONTRACT_VERSION" in ast.unparse(rule), "must gate reuse"
    # no second, drifting generation constant
    assert "cache_id_v2" not in source
    assert "cache_id_v3" not in source
    # P2 bumped the ONE constant; there is no parallel v2 generation left in the code
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert "stage5_cache_v2" not in source, (
        "there is no v2 compatibility loader and no migration - v2 keys are simply never produced "
        "or looked up again")


def test_analysis_version_is_untouched_by_d2_and_by_p2():
    """Its documented job is candidate scoring / window building / candidate schema, and neither D2
    nor P2 changes any of those. P2 only changes what a *semantic* record means, which is the cache
    contract's job."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


# ---------------------------------------------------------------------------
# SOURCE-1/2/3 — source identity
# ---------------------------------------------------------------------------


def test_source_1_same_size_and_restored_mtime_with_changed_content_re_keys(va, tmp_path):
    """The two collisions D1 reproduced, now closed.

    Pre-D2 identity was path + size + int(st_mtime), so a rewrite inside the same second — or one
    that restored the exact original timestamp — reused the previous analysis for different media.
    """
    clip = str(tmp_path / "clip.mp4")
    _write(clip, b"A", 4096, _SECOND)
    original = va["_video_signature"](clip, False, None)

    _write(clip, b"Z", 4096, _SECOND + 400_000_000)      # same integer second
    assert va["_video_signature"](clip, False, None) != original

    _write(clip, b"Q", 4096, _SECOND)                    # exact mtime restored
    assert va["_video_signature"](clip, False, None) != original, (
        "mtime_ns alone cannot catch this; the content fingerprint is what does")


def test_source_2_an_unchanged_file_keeps_a_stable_identity(va, tmp_path):
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    first = va["_video_signature"](clip, False, None)
    assert va["_video_signature"](clip, False, None) == first
    assert va["_cache_path"](clip, False, None) == va["_cache_path"](clip, False, None)


def test_source_3_a_small_file_is_fingerprinted_whole_and_deterministically(va, tmp_path):
    limit = va["_FINGERPRINT_WHOLE_FILE_LIMIT"]
    clip = _write(str(tmp_path / "small.mp4"), b"S", 1024)

    first = va["_bounded_fingerprint"](clip, 1024)
    assert first == va["_bounded_fingerprint"](clip, 1024)
    assert len(first) == va["_FINGERPRINT_DIGEST_SIZE"] * 2

    # a single changed byte anywhere in a small file must show up
    with open(clip, "r+b") as handle:
        handle.seek(512)
        handle.write(b"X")
    assert va["_bounded_fingerprint"](clip, 1024) != first
    assert 1024 <= limit


def test_the_three_sampled_windows_do_not_overlap_and_all_matter(va, tmp_path):
    """Exactly head / middle / tail, 1 MiB each, no byte hashed twice."""
    chunk = va["_FINGERPRINT_CHUNK"]
    size = 10 * chunk
    clip = _write(str(tmp_path / "big.mp4"), b"\x00", size)
    baseline = va["_bounded_fingerprint"](clip, size)
    middle = max(chunk, min(size // 2 - chunk // 2, size - 2 * chunk))
    assert middle >= chunk and middle + chunk <= size - chunk, "windows must not overlap"

    for offset in (0, middle, size - chunk):
        with open(clip, "r+b") as handle:
            handle.seek(offset)
            handle.write(b"\xff" * 64)
        assert va["_bounded_fingerprint"](clip, size) != baseline, f"window at {offset} ignored"
        with open(clip, "r+b") as handle:
            handle.seek(offset)
            handle.write(b"\x00" * 64)
        assert va["_bounded_fingerprint"](clip, size) == baseline


def test_size_participates_so_a_truncation_between_windows_is_caught(va, tmp_path):
    chunk = va["_FINGERPRINT_CHUNK"]
    clip = _write(str(tmp_path / "big.mp4"), b"\x00", 10 * chunk)
    before = va["_bounded_fingerprint"](clip, 10 * chunk)
    with open(clip, "r+b") as handle:
        handle.truncate(9 * chunk)
    assert va["_bounded_fingerprint"](clip, 9 * chunk) != before


# ---------------------------------------------------------------------------
# FAIL-1 / FAIL-2 — unprovable identity must mean "no cache", never a weak key
# ---------------------------------------------------------------------------


def test_fail_1_an_unreadable_source_yields_no_signature_and_no_cache_path(va, tmp_path):
    """No placeholder like "missing"/"unknown" may exist: a *stable* weak key is precisely what lets
    a stale entry be reused. `_cache_path` returning None is the no-lookup / no-write seam."""
    absent = str(tmp_path / "nope.mp4")
    assert va["_video_signature"](absent, False, None) is None
    assert va["_cache_path"](absent, False, None) is None
    assert va["_bounded_fingerprint"](absent, 10) is None
    assert va["_full_fingerprint"](absent) is None


def test_fail_2_an_unprovable_backend_yields_no_ai_signature(va, tmp_path):
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    assert va["_qwen_backend_signature_token"](None) is not None
    assert va["_video_signature"](clip, True, None) is not None

    os.remove(os.path.join(va["_LLAMA"], "llama-server.exe"))
    va["_LLAMA_VERSION_TOKENS"].clear()
    assert va["_qwen_backend_signature_token"](None) is None
    assert va["_video_signature"](clip, True, None) is None
    assert va["_cache_path"](clip, True, None) is None
    # the deterministic path is unaffected: it does not depend on the backend at all
    assert va["_cache_path"](clip, False, None) is not None


def test_no_weak_placeholder_token_survives_in_the_source():
    """`ai_missing` was the pre-D2 stable token for an unprovable backend.

    Executable statements only — the docstring legitimately names the old placeholder to explain why
    it is gone.
    """
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    for name in ("_qwen_backend_signature_token", "_backend_component_token", "_video_signature"):
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                        and n.name == name)
        code = "\n".join(ast.unparse(node) for node in function.body
                         if not (isinstance(node, ast.Expr)
                                 and isinstance(node.value, ast.Constant)))
        assert "ai_missing" not in code, name
        assert "missing" not in code, f"{name} must not fabricate a placeholder identity"


# ---------------------------------------------------------------------------
# BACKEND-1/2/3/4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("component", ["Qwen3VL-2B-Instruct-Q8_0.gguf",
                                       "mmproj-Qwen3VL-2B-Instruct-F16.gguf",
                                       "llama-server.exe", "llama-mtmd-cli.exe"])
def test_backend_1_changed_bytes_behind_identical_metadata_re_key(va, component):
    """All four components, not just the models: D1 reproduced the collision for every one."""
    chunk = va["_FINGERPRINT_CHUNK"]
    directory = va["_MODELS"] if component.endswith(".gguf") else va["_LLAMA"]
    path = os.path.join(directory, component)
    size = os.path.getsize(path)

    baseline = va["_qwen_backend_signature_token"](None)
    va["_LLAMA_VERSION_TOKENS"].clear()
    _write(path, b"Z", size // len(b"Z"), _SECOND)       # same size, same mtime_ns, new bytes
    assert os.path.getsize(path) == size
    assert va["_qwen_backend_signature_token"](None) != baseline
    assert chunk  # geometry is exercised elsewhere


def test_backend_2_identical_metadata_in_different_directories_re_keys(va, tmp_path):
    """Pre-D2 the token used the *basename*, so pointing an override at another copy did not re-key."""
    chunk = va["_FINGERPRINT_CHUNK"]
    original = os.path.join(va["_MODELS"], "Qwen3VL-2B-Instruct-Q8_0.gguf")
    elsewhere = _write(str(tmp_path / "other" / "Qwen3VL-2B-Instruct-Q8_0.gguf"), b"A", 4 * chunk)

    first = va["_backend_component_token"](original, full_hash=False)
    second = va["_backend_component_token"](elsewhere, full_hash=False)
    assert first != second, "absolute path must be part of the component token"
    # identical content, so only the location differs
    assert first.split(":")[-1] == second.split(":")[-1]


def test_backend_3_expensive_work_happens_once_for_many_sources(va, tmp_path):
    """The memoisation trap, with teeth.

    `_qwen_backend_signature_token` used to be reached from `_video_signature`, i.e. once per source.
    With content fingerprints that is 702 reads of a ~2.65 GB backend; the D2 study measured 61.7
    minutes. The orchestrator therefore computes the token once and threads it in.
    """
    calls = {"n": 0}
    real = va["_backend_component_token"]

    def counting(path, *, full_hash):
        calls["n"] += 1
        return real(path, full_hash=full_hash)

    va["_backend_component_token"] = counting
    sources = [_write(str(tmp_path / f"s{i}.mp4"), b"v", 2048 + i) for i in range(25)]

    calls["n"] = 0
    token = va["_qwen_backend_signature_token"](None)
    config = va["_qwen_config_token"]()
    paths = [va["_cache_path"](p, True, None, backend_token=token, config_token=config)
             for p in sources]

    assert calls["n"] == 4, (
        f"one fingerprint per component, once for the whole invocation; got {calls['n']}")
    assert len(set(paths)) == len(sources), "distinct sources must still key distinctly"

    calls["n"] = 0
    va["_cache_path"](sources[0], True, None)            # unthreaded: recomputes
    assert calls["n"] == 4, "an unthreaded call must recompute, which is why threading matters"


def test_backend_4_a_later_invocation_observes_a_changed_backend(va, tmp_path):
    """No module-level cache of the backend result, so a second Stage 5 call in the same process
    still sees a swapped model."""
    chunk = va["_FINGERPRINT_CHUNK"]
    model = os.path.join(va["_MODELS"], "Qwen3VL-2B-Instruct-Q8_0.gguf")

    first = va["_qwen_backend_signature_token"](None)
    _write(model, b"C", 4 * chunk, _SECOND + 5)
    va["_LLAMA_VERSION_TOKENS"].clear()
    assert va["_qwen_backend_signature_token"](None) != first


def test_the_llama_version_probe_is_not_the_only_strong_evidence(va):
    """A version-probe failure stays non-fatal, because the file fingerprints are strong on their own."""
    token = va["_qwen_backend_signature_token"](None)
    assert token is not None and token.startswith("ai_")
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    backend = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_qwen_backend_signature_token")
    rendered = ast.unparse(backend)
    assert "_llama_version_token" in rendered
    assert "_backend_component_token" in rendered


# ---------------------------------------------------------------------------
# CONFIG-1..5 — effective Qwen configuration identity
# ---------------------------------------------------------------------------


def _key(va, clip, **env):
    for name in _QWEN_ENV:
        os.environ.pop(name, None)
    os.environ.update(env)
    try:
        return va["_cache_path"](clip, True, None)
    finally:
        for name in env:
            os.environ.pop(name, None)


def test_config_1_max_windows_changes_the_ai_key(va, tmp_path):
    """D1 proved MAX_WINDOWS changes how many candidates receive semantics while being absent from
    identity, so a 60-window cache was silently reused by a run asking for 120."""
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    default = _key(va, clip)

    assert _key(va, clip, BEATSYNC_QWEN_MAX_WINDOWS="60") != default
    assert _key(va, clip, BEATSYNC_QWEN_MAX_WINDOWS="0") != default


@pytest.mark.parametrize("env", [
    {"BEATSYNC_QWEN_MAX_WINDOWS": "120"},
    {"BEATSYNC_QWEN_FRAME_WIDTH": "512"},
    {"BEATSYNC_QWEN_MAX_NEW_TOKENS": "128"},
    {"BEATSYNC_QWEN_FRAME_WIDTH": "abc"},          # malformed -> the worker's default, 512
    {"BEATSYNC_QWEN_MAX_NEW_TOKENS": "not-a-number"},
    {"BEATSYNC_QWEN_MAX_WINDOWS": "-5"},           # max(0, value) -> 0? no: runtime clamps to 0
])
def test_config_2_behaviourally_equivalent_configuration_keys_identically(va, tmp_path, env):
    """Identity is keyed on *effective* values, so unset and explicit-default agree, and a malformed
    value agrees with whatever default the runtime actually uses."""
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    default = _key(va, clip)
    if env == {"BEATSYNC_QWEN_MAX_WINDOWS": "-5"}:
        # runtime does max(0, value), so -5 behaves as 0, which is NOT the default of 120
        assert _key(va, clip, **env) != default
    else:
        assert _key(va, clip, **env) == default


def test_config_3_frame_width_changes_the_key_and_clamps_like_the_worker(va, tmp_path):
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    default = _key(va, clip)

    assert _key(va, clip, BEATSYNC_QWEN_FRAME_WIDTH="384") != default
    # the worker clamps 224..768, so an out-of-range value keys as the clamp bound
    assert (_key(va, clip, BEATSYNC_QWEN_FRAME_WIDTH="99999")
            == _key(va, clip, BEATSYNC_QWEN_FRAME_WIDTH="768"))
    assert (_key(va, clip, BEATSYNC_QWEN_FRAME_WIDTH="1")
            == _key(va, clip, BEATSYNC_QWEN_FRAME_WIDTH="224"))


def test_config_4_max_new_tokens_changes_the_key_and_clamps_like_the_worker(va, tmp_path):
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    default = _key(va, clip)

    assert _key(va, clip, BEATSYNC_QWEN_MAX_NEW_TOKENS="64") != default
    assert (_key(va, clip, BEATSYNC_QWEN_MAX_NEW_TOKENS="9999")
            == _key(va, clip, BEATSYNC_QWEN_MAX_NEW_TOKENS="256"))
    assert (_key(va, clip, BEATSYNC_QWEN_MAX_NEW_TOKENS="1")
            == _key(va, clip, BEATSYNC_QWEN_MAX_NEW_TOKENS="32"))


def test_config_5_a_no_ai_run_has_its_own_identity(va, tmp_path):
    """`BEATSYNC_DISABLE_QWEN` reaches here as ``enable_ai=False`` (auto_mode turns it into that),
    and a deterministic key must not carry Qwen configuration at all."""
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    deterministic = va["_cache_path"](clip, False, None)
    assert deterministic != _key(va, clip)

    # Qwen settings must not perturb a deterministic key
    for env in ({"BEATSYNC_QWEN_MAX_WINDOWS": "7"}, {"BEATSYNC_QWEN_FRAME_WIDTH": "300"},
                {"BEATSYNC_QWEN_MAX_NEW_TOKENS": "50"}):
        os.environ.update(env)
        try:
            assert va["_cache_path"](clip, False, None) == deterministic
        finally:
            for name in env:
                os.environ.pop(name, None)


# ---------------------------------------------------------------------------
# MEDIA-TRUTH-1..5 — the P2 invariant: no music/edit state may reach identity
#
# These deliberately REPLACE the D2 R2 "PROMPT-ID" tests, which asserted the opposite: that
# `smart_preset` re-keys. That expectation is intentionally obsolete. The P2 real-material A/B
# validated media-neutral semantics on the user's own content (115 identical moments, 115/115 tagged
# on both sides, post-merge score deltas <= ~0.023, no new planner fallback pattern), so Stage 5 now
# records intrinsic media evidence and Stage 6 does the music-specific interpretation.
# ---------------------------------------------------------------------------


_WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src", "auto_mode", "stage5_qwen_scene_worker.py")

_PRESETS = ("hybrid_drop_story", "cinematic_soft_amv", "rhythmic_hype_gmv_amv",
            "rhythmic_flow_gmv_amv", "rhythmic_gmv_amv")


def test_media_truth_1_the_config_token_takes_no_arguments(va):
    """`_qwen_config_token()` represents media-semantic configuration only, so it has nothing to be
    parameterised by. An argument here is how an edit style got into identity in the first place."""
    tree = ast.parse(open(_VIDEO_ANALYSIS, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_qwen_config_token")
    args = fn.args
    assert not args.args and not args.posonlyargs and not args.kwonlyargs, ast.unparse(args)
    assert args.vararg is None and args.kwarg is None
    assert va["_qwen_config_token"]() == va["_qwen_config_token"]()


def test_media_truth_2_the_identity_primitives_have_no_audio_profile_input():
    """Structural: the parameter is gone from the key formula, so a caller *cannot* re-key by style."""
    tree = ast.parse(open(_VIDEO_ANALYSIS, encoding="utf-8").read())
    for name in ("_video_signature", "_cache_path", "_qwen_config_token"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        assert "audio_profile" not in params, f"{name} still takes audio_profile"
        body = "\n".join(
            ast.unparse(node) for node in fn.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))
        for leaked in ("audio_profile", "smart_preset", "_qwen_prompt_style_hint",
                       "tempo", "section", "seed"):
            assert leaked not in body, f"{name} must not reference {leaked}"


def test_media_truth_3_the_retired_style_hint_helper_is_gone():
    """`_qwen_prompt_style_hint` existed only to mirror the worker's `smart_preset` interpolation.
    That concept no longer exists, and it must not be replaced by a fake constant style hint."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    assert "_qwen_prompt_style_hint" not in source
    assert "rhythmic_gmv_amv" not in source, "no fabricated default edit style may remain"


@pytest.mark.parametrize("preset", _PRESETS)
def test_media_truth_4_every_edit_style_targets_one_identity(va, tmp_path, preset):
    """The strong invariant. Same source, same backend, same media-semantic Qwen configuration =>
    one key, whatever edit style the *render* happens to want."""
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    token = va["_qwen_backend_signature_token"](None)
    config = va["_qwen_config_token"]()
    baseline = va["_cache_path"](clip, True, None, backend_token=token, config_token=config)

    # There is no seam through which a profile could even be supplied - which is the point. The
    # config token is computed from the environment alone, so every preset resolves here.
    assert preset  # named for the record; identity cannot vary with it
    assert va["_cache_path"](clip, True, None, backend_token=token,
                             config_token=va["_qwen_config_token"]()) == baseline


def test_media_truth_5_arbitrary_audio_and_creative_state_cannot_re_key(va, tmp_path):
    """Two wildly different renders — different preset, tempo, sections, energy, cut count, and a
    different variation seed — reach the same Stage-5 identity, because none of it is an input."""
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    token = va["_qwen_backend_signature_token"](None)

    profile_a = {"smart_preset": "hybrid_drop_story", "tempo": 174.0, "beat_count": 812,
                 "section_types": ["intro", "drop", "outro"], "average_wave": 0.81,
                 "cut_count": 240, "creative": {"seed": 101}}
    profile_b = {"smart_preset": "cinematic_soft_amv", "tempo": 82.0, "beat_count": 210,
                 "section_types": ["intro", "verse", "breakdown"], "average_wave": 0.22,
                 "cut_count": 61, "creative": {"seed": 202}}
    assert profile_a != profile_b

    # The only identity inputs are the source, the backend and the media-semantic config token.
    keys = {va["_cache_path"](clip, True, None, backend_token=token,
                              config_token=va["_qwen_config_token"]())
            for _ in (profile_a, profile_b)}
    assert len(keys) == 1
    assert next(iter(keys)) is not None


def test_no_audio_or_creative_state_appears_anywhere_in_the_key_formula():
    """A stricter version of the old "the whole profile is not hashed": nothing describing HOW to
    edit may appear in the key formula at all, in any form.

    Executable statements only — the docstring legitimately names the retired `smart_preset`
    component to explain why it is gone, exactly as the `ai_missing` test above does.
    """
    tree = ast.parse(open(_VIDEO_ANALYSIS, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
              and n.name == "_qwen_config_token")
    token = "\n".join(
        ast.unparse(node) for node in fn.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))
    for reckless in ("json.dumps(audio_profile", "str(audio_profile)", "sorted(audio_profile",
                     "smart_preset", "audio_profile", "tempo", "seed", "creative",
                     "master_seed", "freestyle", "director"):
        assert reckless not in token, f"must not enter cache identity: {reckless}"


# ---------------------------------------------------------------------------
# BACKEND memoisation / failure state at the orchestration seam (D2 R2)
# ---------------------------------------------------------------------------


def _orchestrator():
    tree = ast.parse(open(_VIDEO_ANALYSIS, encoding="utf-8").read())
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "analyze_video_sources")


def _calls_named(node, callee):
    found = []
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name == callee:
                found.append(inner)
    return found


def test_the_backend_token_is_computed_once_in_the_orchestrator(va):
    """Structural, because `analyze_video_sources` needs cv2/numpy/librosa and cannot be imported on a
    bare interpreter (see CLAUDE.md). The behavioural proof runs against the real orchestrator in the
    D2 scratch harness: 1 call for N=6 sources on success, 2 across two invocations, and 1 — not
    1 + N — when the token fails.
    """
    orchestrator = _orchestrator()
    calls = _calls_named(orchestrator, "_qwen_backend_signature_token")
    assert len(calls) == 1, (
        f"exactly one invocation-level computation; found {len(calls)}")

    assigned = [n for n in ast.walk(orchestrator) if isinstance(n, ast.Assign)
                and "invocation_backend_token" in ast.unparse(n.targets[0])]
    assert assigned, "the result must be held for the whole invocation"
    # and threaded, not recomputed per source
    for call in _calls_named(orchestrator, "_cache_path"):
        rendered = ast.unparse(call)
        assert "backend_token=" in rendered and "config_token=" in rendered, rendered


def test_a_failed_backend_token_disables_ai_caching_for_the_whole_invocation(va):
    """R2's defect: the failed `None` was passed on to `_cache_path`, where `None` means "not supplied,
    compute it now" — so every source retried the fingerprinting (measured 1 + N calls) and a transient
    later success re-enabled caching mid-run. An explicit state replaces the overloaded `None`."""
    orchestrator = _orchestrator()
    body = ast.unparse(orchestrator)
    assert "ai_cache_disabled" in body, "an explicit disabled state is required"

    # the cache path must be guarded by that state, not merely handed a None token
    guarded = [n for n in ast.walk(orchestrator)
               if isinstance(n, ast.IfExp) and "ai_cache_disabled" in ast.unparse(n.test)
               and "_cache_path" in ast.unparse(n)]
    assert guarded, "when AI caching is disabled, _cache_path must not be called at all"
    assert ast.unparse(guarded[0].body) == "None", ast.unparse(guarded[0])


def test_the_orchestrator_keeps_the_audio_profile_out_of_identity(va):
    """The P2 inverse of the D2 R2 test this replaces.

    `analyze_video_sources` still *accepts* `audio_profile` - a retained integration signature so
    `auto_mode.analyze_beats_auto` needs no change - but it may not thread it anywhere near the key.
    """
    orchestrator = _orchestrator()
    config_calls = _calls_named(orchestrator, "_qwen_config_token")
    assert len(config_calls) == 1, "the config token must be computed once per invocation"
    for call in config_calls + _calls_named(orchestrator, "_cache_path"):
        assert "audio_profile" not in ast.unparse(call), ast.unparse(call)

    # the parameter survives for compatibility, and is used by nothing
    params = [a.arg for a in orchestrator.args.args + orchestrator.args.kwonlyargs]
    assert "audio_profile" in params
    body = "\n".join(
        ast.unparse(node) for node in orchestrator.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)))
    assert "audio_profile" not in body, (
        "audio_profile must have ZERO effect: no cache key, no Qwen request, no prompt")


def test_runtime_only_knobs_are_absent_from_the_config_token():
    """Slots, device, timeouts and batching do not change the persisted semantic contract."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    token = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                             and n.name == "_qwen_config_token"))
    for name in ("SLOTS", "DEVICE", "TIMEOUT", "BATCH_VIDEOS", "DISABLE_SERVER",
                 "PREFETCH_FRAMES", "ORDERED_MAX_GAP", "LLAMA_CTX"):
        assert name not in token, f"{name} must not be part of cache identity"
    for name in ("MAX_WINDOWS", "FRAME_WIDTH", "MAX_NEW_TOKENS"):
        assert name in token, f"{name} must be part of cache identity"


# ---------------------------------------------------------------------------
# CONTRACT-1..4 and LEGACY-1
# ---------------------------------------------------------------------------


def _record(va, *, ai_enabled=True, candidates=None, contract=True, **over):
    scoring = va["_DETERMINISTIC_SCORING_KEY"]
    if candidates is None:
        candidates = [{"id": "c0", "ai_analyzed": True}]
    timings = {"total_seconds": 1.0, scoring: 0.3}
    if candidates and ai_enabled:
        timings["qwen_frame_count"] = len(candidates)
        timings["qwen_tag_count"] = len(candidates)
    record = {
        "analysis_version": va["ANALYSIS_VERSION"],
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "candidates": candidates, "candidate_count": len(candidates),
        "timings": timings, "ai_enabled": ai_enabled, "ai_deferred": False,
    }
    if contract:
        record["cache_contract"] = va["CACHE_CONTRACT_VERSION"]
    record.update(over)
    return record


def test_contract_1_a_new_ai_record_carries_the_marker_and_is_reusable(va):
    assert va["_cache_entry_is_complete"](_record(va), require_ai=True) is True


def test_contract_2_a_new_no_ai_record_carries_the_marker(va):
    record = _record(va, ai_enabled=False)
    assert record["cache_contract"] == va["CACHE_CONTRACT_VERSION"]
    assert va["_cache_entry_is_complete"](record, require_ai=False) is True


def test_contract_3_a_new_candidate_less_record_carries_the_marker(va):
    record = _record(va, ai_enabled=False, candidates=[])
    assert record["cache_contract"] == va["CACHE_CONTRACT_VERSION"]
    assert va["_cache_entry_is_complete"](record, require_ai=True) is True


@pytest.mark.parametrize("contract", [None, "", "stage5_cache_v1", "auto_av_analysis_v8", 7, {}])
def test_contract_4_a_missing_or_wrong_marker_is_rejected(va, contract):
    record = _record(va, contract=False)
    if contract is not None:
        record["cache_contract"] = contract
    for require_ai in (True, False):
        assert va["_cache_entry_is_complete"](record, require_ai=require_ai) is False


def test_legacy_1_a_d1_record_is_never_treated_as_d2_complete(va):
    """A real pre-D2 record: valid in every D1 respect, but carrying no contract marker. It is also
    unreachable because the D2 signature re-keys everything — this is the belt-and-braces half."""
    legacy = {
        "analysis_version": va["ANALYSIS_VERSION"],
        "video_file": r"C:\lib\clip.mp4", "source_name": "clip.mp4",
        "duration": 41.0, "fps": 25.0, "width": 1280, "height": 720,
        "scene_changes": [2.0], "candidate_count": 2,
        "candidates": [{"id": "a", "ai_analyzed": True}, {"id": "b", "ai_analyzed": True}],
        "analysis_seconds": 4.4,
        "timings": {"total_seconds": 4.4, "candidate_scoring_seconds": 0.4,
                    "qwen_frame_count": 2, "qwen_tag_count": 2},
        "ai_enabled": True, "ai_deferred": False,
    }
    assert "cache_contract" not in legacy
    assert va["_cache_entry_is_complete"](legacy, require_ai=True) is False
    assert va["_cache_entry_is_complete"](legacy, require_ai=False) is False


def test_the_record_builder_stamps_the_contract_not_the_writer():
    """`_save_cache` stays a transport primitive; semantic truth is stamped where records are built."""
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    analyse = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name == "_analyze_single_video")
    writer = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                  and n.name == "_save_cache")
    assert "cache_contract" in ast.unparse(analyse), "records must be born with the marker"
    assert "cache_contract" not in ast.unparse(writer), "the writer must not inject semantic truth"


# ---------------------------------------------------------------------------
# key layout
# ---------------------------------------------------------------------------


def test_the_cache_filename_layout_is_unchanged(va, tmp_path):
    clip = _write(str(tmp_path / "clip.mp4"), b"A", 4096)
    name = os.path.basename(va["_cache_path"](clip, False, None))
    stem, _, extension = name.rpartition(".")
    prefix, _, signature = stem.partition("_")

    assert extension == "json"
    assert len(prefix) == 8 and len(signature) == 24
    assert all(character in "0123456789abcdef" for character in prefix + signature)


def test_the_signature_inputs_are_the_documented_eight(va):
    source = open(_VIDEO_ANALYSIS, encoding="utf-8").read()
    tree = ast.parse(source)
    signature = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                                 and n.name == "_video_signature"))
    for expected in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "os.path.abspath(video_file)",
                     "st_size", "st_mtime_ns", "fingerprint", "backend_token", "config_token"):
        assert expected in signature, expected
    assert "int(stat.st_mtime)" not in signature, "integer-second truncation must be gone"
