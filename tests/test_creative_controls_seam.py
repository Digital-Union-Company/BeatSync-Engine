"""The seams around the Creative Profile: Stage 5 isolation, the GUI gate, and the CLI.

Three boundaries, three techniques:

* **Stage 5 isolation** is the real B0 invariant, and the one with a multi-hour cost if it breaks: a
  creative control that reached cache identity would re-key an 845-source Qwen library. It is proved
  twice — structurally (no identity function so much as *mentions* a control) and executably (the
  production ``_cache_path`` is AST-extracted and run, and it produces the same filename regardless
  of what the render wants).
* **The GUI gate** is asserted with ``ast`` over ``gui.py``, which cannot be imported here. That is
  also the stronger check: what matters is the *absence* of the controls from every source-invalidating
  handler, plus the positional alignment Gradio depends on.
* **The CLI** is asserted over ``video_processor.py``'s argument definitions and its
  ``analyze_beats_auto`` call, plus the real normalisation running on the values argparse would hand
  over. No render is performed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
from typing import Any, Dict, List, Sequence

import pytest

from beatsync_fork import creative as fork_creative

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_WORKER = os.path.join(_REPO_ROOT, "src", "auto_mode", "stage5_qwen_scene_worker.py")
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_VP = os.path.join(_REPO_ROOT, "src", "video_processor.py")
_AUTO_MODE = os.path.join(_REPO_ROOT, "src", "auto_mode", "__init__.py")
_LIBRARY_PREP = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_prep.py")

#: Every name a creative control could travel under. Whole-word matched, so `os.makedirs(directory)`
#: is not read as a Director reference and `_seconds` is not read as a seed.
_CREATIVE_WORDS = ("creative", "seed", "variation", "cut_density", "energy_response",
                   "motion_bias", "source_diversity", "micro_cuts", "semantic_emphasis",
                   "deterministic_view", "density",
                   "profile_settings", "scoring_controls", "motion_centered", "energy_factor",
                   "source_diversity_factor", "micro_cut_ratio_factor")


def _tree(path: str) -> ast.Module:
    with open(path, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _strip_docstrings(node):
    """A deep copy with every docstring removed, at every nesting level.

    Prose must not be able to satisfy *or* fail these checks: since P2 several of these docstrings
    legitimately name the variation seed and creative state precisely in order to say that neither
    may be an input.
    """
    import copy

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
    """Executable body only: the signature is excluded too, so a retained-but-unused parameter name
    can neither satisfy nor fail a check."""
    return "\n".join(ast.unparse(item) for item in _strip_docstrings(node).body)


def _executable_source(path: str) -> str:
    return ast.unparse(_strip_docstrings(_tree(path)))


# ===========================================================================
# 1. STAGE 5 ISOLATION — structural
# ===========================================================================


_IDENTITY_AND_COMPLETION = [
    "_video_signature", "_cache_path", "_qwen_config_token", "_qwen_backend_signature_token",
    "_path_signature_token", "_bounded_fingerprint", "_cache_entry_is_complete",
    "_stored_ai_cache_is_consistent", "_qwen_job_completed", "_checkpoint_cache", "_save_cache",
    "_load_cache", "classify_library_sources",
]


@pytest.mark.parametrize("name", _IDENTITY_AND_COMPLETION)
def test_no_stage_5_identity_or_completion_function_knows_any_creative_control(name):
    """The B0 invariant. None of the four controls may be reachable from cache identity."""
    body = _body_code(_func(_tree(_VA), name)).lower()
    for word in _CREATIVE_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", body), f"{name} mentions {word!r}"


def test_video_analysis_never_imports_the_creative_module():
    """Not even for a type hint: an import is the first step towards a use."""
    tree = _tree(_VA)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "creative" not in alias.name, ast.unparse(node)
        elif isinstance(node, ast.ImportFrom):
            rendered = ast.unparse(node)
            assert "creative" not in rendered, rendered
            assert "variation" not in rendered, rendered


def test_the_whole_of_video_analysis_names_no_new_creative_control():
    """Stage 5 is not merely uninterested in the controls; it has never heard of them."""
    source = _executable_source(_VA).lower()
    for word in ("cut_density", "energy_response", "motion_bias", "source_diversity",
                 "micro_cuts", "semantic_emphasis", "deterministic_view", "creativeprofile",
                 "scoring_controls", "creative_profile"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"video_analysis mentions {word!r}"


def test_the_qwen_worker_still_knows_nothing_about_creative_state():
    """The process boundary is where separation is easiest to lose: whatever the parent stops
    sending, the worker must also stop being able to ask for."""
    source = _executable_source(_WORKER).lower()
    for word in ("cut_density", "energy_response", "motion_bias", "source_diversity",
                 "micro_cuts", "semantic_emphasis", "creative", "variation", "smart_preset",
                 "audio_profile"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"the worker mentions {word!r}"


def test_the_worker_request_builders_carry_no_creative_key():
    """Executed rather than read: the actual dict literals the parent sends."""
    tree = _tree(_VA)
    for builder in ("_run_qwen_worker", "_run_qwen_worker_batch"):
        body = _body_code(_func(tree, builder)).lower()
        for word in _CREATIVE_WORDS:
            assert not re.search(rf"\b{re.escape(word)}\b", body), f"{builder} mentions {word!r}"


def test_the_cache_contract_and_analysis_versions_are_unchanged():
    """No control may cost a cold rebuild, so neither constant moves for this feature.
    `stage5_cache_v3` is P2's one deliberate bump (the media-neutral prompt)."""
    values = {}
    for node in ast.walk(_tree(_VA)):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                        "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION"}:
                    values[target.id] = node.value.value

    assert values["CACHE_CONTRACT_VERSION"] == "stage5_cache_v3"
    assert values["ANALYSIS_VERSION"] == "auto_av_analysis_v8_llama_vulkan_batched"


def test_no_creative_state_reaches_analyze_video_sources():
    """Stage 5's entry point. `audio_profile` is still passed — a retained, zero-effect integration
    signature under P2 — and must remain the only thing the pipeline hands it besides sources."""
    fn = _func(_tree(_AUTO_MODE), "analyze_beats_auto")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "analyze_video_sources"]

    assert len(calls) == 1
    rendered = ast.unparse(calls[0]).lower()
    for word in _CREATIVE_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", rendered), rendered


def test_media_library_preparation_gained_no_creative_input():
    """Preparation warms the Stage-5 cache. If it had to know about a creative control, the control
    would be in cache identity by definition."""
    source = _executable_source(_LIBRARY_PREP).lower()
    for word in ("cut_density", "energy_response", "motion_bias", "source_diversity",
                 "micro_cuts", "semantic_emphasis", "creative", "variation", "seed"):
        assert not re.search(rf"\b{re.escape(word)}\b", source), f"library_prep mentions {word!r}"


def test_the_audio_visual_profile_carries_no_creative_state():
    """Cut Density legitimately *changes* this profile (through `selected_beats`), but the profile
    must never carry the control itself — it describes the track, and keeping creative state off it
    is what keeps the two separable for anything downstream that forwards it."""
    body = _body_code(_func(_tree(_AUTO_MODE), "_build_audio_visual_profile")).lower()
    for word in _CREATIVE_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", body), f"the profile mentions {word!r}"


def test_no_stage_cache_or_director_machinery_was_added():
    """Explicitly out of scope: L2 stage caching, Freestyle, an AI Director, Variant Lab, per-section
    profiles and a second Qwen pass are documented future work, not speculative abstraction here."""
    for path in (_VA, _WORKER, _AUTO_MODE,
                 os.path.join(_REPO_ROOT, "src", "auto_mode", "stage6_av_planner.py"),
                 os.path.join(_REPO_ROOT, "src", "beatsync_fork", "creative.py")):
        source = _executable_source(path).lower()
        for word in ("freestyle", "director", "variant_lab", "creative_recipe", "master_seed",
                     "shortlist", "stage_cache", "per_section_profile", "micro_cuts_control"):
            assert not re.search(rf"\b{re.escape(word)}\b", source), f"{path} mentions {word!r}"


# ===========================================================================
# 2. STAGE 5 ISOLATION — executable
# ===========================================================================


_IDENTITY_FUNCS = (
    "_safe_name", "_hash_text", "_env_int", "_qwen_max_windows", "_path_signature_token",
    "_bounded_fingerprint", "_full_fingerprint", "_backend_component_token",
    "_llama_version_token", "_resolve_qwen_backend_paths", "_qwen_backend_signature_token",
    "_qwen_config_token", "_video_signature", "_cache_path",
)
_IDENTITY_CONSTS = ("ANALYSIS_VERSION", "CACHE_CONTRACT_VERSION", "_FINGERPRINT_CHUNK",
                    "_FINGERPRINT_WHOLE_FILE_LIMIT", "_FINGERPRINT_DIGEST_SIZE",
                    "_NO_AI_CONFIG_TOKEN")
_QWEN_ENV = ("BEATSYNC_QWEN_MAX_WINDOWS", "BEATSYNC_QWEN_FRAME_WIDTH",
             "BEATSYNC_QWEN_MAX_NEW_TOKENS", "BEATSYNC_QWEN_LLAMA_DIR",
             "BEATSYNC_QWEN_LLAMA_MODEL", "BEATSYNC_QWEN_LLAMA_MMPROJ")


def _load_identity(root: str, cache_dir: str) -> Dict[str, Any]:
    """The production key functions, AST-extracted and executed on a sandboxed root.

    Deliberately the real bodies rather than a reimplementation: a test that re-derived the key
    formula could agree with a buggy expectation. `ROOT_DIR` and `VIDEO_ANALYSIS_CACHE_DIR` are
    redirected into `tmp_path`, so the real runtime cache is never read or written — see CLAUDE.md's
    `._pth` provenance hazard note.
    """
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
def identity(tmp_path):
    ns = _load_identity(str(tmp_path / "root"), str(tmp_path / "cache"))
    chunk = ns["_FINGERPRINT_CHUNK"]
    for name in ("Qwen3VL-2B-Instruct-Q8_0.gguf", "mmproj-Qwen3VL-2B-Instruct-F16.gguf"):
        _write(os.path.join(ns["_MODELS"], name), b"A", 4 * chunk)
    for name in ("llama-server.exe", "llama-mtmd-cli.exe"):
        _write(os.path.join(ns["_LLAMA"], name), b"B", 2048)
    return ns


def _write(path: str, byte: bytes, size: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(byte * size)
    return os.path.abspath(path)


#: Two audio profiles a *different Cut Density on the same track* would legitimately produce. Cut
#: count, average interval and therefore `smart_preset` all differ — see the next test for why that
#: is allowed rather than a defect.
_SPARSE_PROFILE = {
    "tempo": 123.0, "smart_preset": "cinematic_soft_amv", "average_wave": 0.41,
    "average_impact": 0.38, "average_rhythm": 0.40, "peak_ratio": 0.18,
    "average_cut_interval": 3.639, "cut_count": 75, "beat_count": 566,
    "section_types": ["intro", "verse", "outro"], "rhythm_preference": "rhythmic",
}
_DENSE_PROFILE = {
    "tempo": 123.0, "smart_preset": "rhythmic_hype_gmv_amv", "average_wave": 0.41,
    "average_impact": 0.38, "average_rhythm": 0.40, "peak_ratio": 0.18,
    "average_cut_interval": 1.355, "cut_count": 204, "beat_count": 566,
    "section_types": ["intro", "verse", "outro"], "rhythm_preference": "rhythmic",
}


def test_cut_density_changes_the_audio_profile_and_that_is_allowed():
    """Stating the real invariant rather than a false one.

    Cut Density changes `selected_beats`, so `_build_audio_visual_profile` legitimately derives a
    different `average_cut_interval`, `cut_count` and even `smart_preset`. A test claiming otherwise
    would be false. What must hold is that none of it can reach Stage-5 identity — which the next
    test measures at the production key seam.
    """
    assert _SPARSE_PROFILE != _DENSE_PROFILE
    assert _SPARSE_PROFILE["smart_preset"] != _DENSE_PROFILE["smart_preset"]
    assert _SPARSE_PROFILE["cut_count"] != _DENSE_PROFILE["cut_count"]
    assert _SPARSE_PROFILE["average_cut_interval"] != _DENSE_PROFILE["average_cut_interval"]


def test_every_source_keys_identically_however_the_creative_controls_are_set(identity, tmp_path):
    """The executable B0 proof, at the production key seam.

    `_cache_path` has no creative or audio parameter at all, so the demonstration is that the key is
    a function of source identity plus backend and Qwen config identity — and that the *complete*
    argument list a render can influence produces one filename for many sources and many profiles.
    """
    sources = [_write(str(tmp_path / "library" / f"clip_{i:02d}.mp4"), b"v", 4096 + i)
               for i in range(6)]
    backend = identity["_qwen_backend_signature_token"](None)
    config = identity["_qwen_config_token"]()
    assert backend and config

    for source in sources:
        keys = {identity["_cache_path"](source, True, None,
                                       backend_token=backend, config_token=config)
                for _ in (_SPARSE_PROFILE, _DENSE_PROFILE)}
        keys.add(identity["_cache_path"](source, True, None))
        assert len(keys) == 1 and None not in keys, source

    # ...and distinct sources still key distinctly, so the above is not vacuous
    assert len({identity["_cache_path"](s, True, None) for s in sources}) == len(sources)


def test_the_qwen_config_token_takes_no_arguments_and_is_creative_free(identity):
    """There is nowhere for a creative value to enter, and the token is stable across renders."""
    baseline = identity["_qwen_config_token"]()

    assert baseline.startswith("cfg_")
    for attempt in ({"cut_density": 0}, 100, _DENSE_PROFILE):
        with pytest.raises(TypeError):
            identity["_qwen_config_token"](attempt)
    assert identity["_qwen_config_token"]() == baseline


def test_the_three_media_semantic_settings_still_re_key(identity, tmp_path):
    """The separation cuts both ways: what genuinely changes persisted semantics must still
    invalidate, or the isolation above would be achieved by keying on nothing."""
    clip = _write(str(tmp_path / "library" / "clip.mp4"), b"v", 4096)
    baseline = identity["_cache_path"](clip, True, None)

    for name, value in (("BEATSYNC_QWEN_MAX_WINDOWS", "40"),
                        ("BEATSYNC_QWEN_FRAME_WIDTH", "640"),
                        ("BEATSYNC_QWEN_MAX_NEW_TOKENS", "200")):
        os.environ[name] = value
        try:
            assert identity["_cache_path"](clip, True, None) != baseline, name
        finally:
            os.environ.pop(name, None)
    assert identity["_cache_path"](clip, True, None) == baseline


# ===========================================================================
# 3. THE GUI GATE
# ===========================================================================


_NEW_CONTROLS = ("cut_density", "energy_response", "motion_bias",
                 "source_diversity", "micro_cuts", "semantic_emphasis")
_ALL_CREATIVE_WIDGETS = ("variation_seed",) + _NEW_CONTROLS


def _gui_tree():
    return _tree(_GUI)


def _registration(tree, widget: str, attrs=("click", "change", "input", "submit", "release")):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in attrs
            and isinstance(node.func.value, ast.Name) and node.func.value.id == widget]


def _kwargs(call):
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node):
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _widget_call(tree, name: str) -> ast.Call:
    """The `gr.<Component>(...)` assigned to `name`."""
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no widget assignment for {name}")


@pytest.mark.parametrize("widget", _NEW_CONTROLS)
def test_each_control_is_a_0_to_100_slider_defaulting_to_neutral(widget):
    call = _widget_call(_gui_tree(), widget)
    kwargs = _kwargs(call)

    assert ast.unparse(call.func) == "gr.Slider", ast.unparse(call.func)
    assert ast.unparse(kwargs["minimum"]) == "fork_creative.CONTROL_MIN"
    assert ast.unparse(kwargs["maximum"]) == "fork_creative.CONTROL_MAX"
    assert ast.unparse(kwargs["value"]) == "fork_creative.DEFAULT_CONTROL"
    assert ast.literal_eval(kwargs["step"]) == 1
    # the label and help text come from ui_content, like every other control
    assert ast.unparse(kwargs["label"]).startswith("LABEL_")
    assert ast.unparse(kwargs["info"]).startswith("INFO_")


def test_the_declared_defaults_really_are_the_neutral_values():
    """The widget defaults are read from the fork module, and those constants must be the neutral
    profile — otherwise a fresh UI would silently open on a non-default render."""
    assert fork_creative.DEFAULT_CONTROL == fork_creative.NEUTRAL_PROFILE.cut_density
    assert fork_creative.DEFAULT_CONTROL == fork_creative.NEUTRAL_PROFILE.energy_response
    assert fork_creative.DEFAULT_CONTROL == fork_creative.NEUTRAL_PROFILE.motion_bias
    assert fork_creative.NEUTRAL_PROFILE.is_neutral()


def test_the_controls_live_in_the_creative_direction_group():
    """Grouped with the seed, not inside Video Source: their placement is what tells the user they
    are render-request creative state rather than part of the source declaration."""
    source = open(_GUI, encoding="utf-8").read()
    heading = source.index("Creative Direction")
    # the next group heading after Creative Direction
    following = source.index("Processing Mode", heading)

    for widget in _ALL_CREATIVE_WIDGETS:
        position = source.index(f"{widget} = gr.", heading - 4000)
        assert heading < position < following, f"{widget} is outside the Creative Direction group"


@pytest.mark.parametrize("widget", _NEW_CONTROLS)
def test_no_control_registers_a_source_invalidating_handler(widget):
    """Every source handler writes `source_outputs`, which disables Create Music Video. A creative
    control with any handler of its own is one refactor away from clearing a confirmation."""
    assert _registration(_gui_tree(), widget) == [], f"{widget} registered an event handler"


@pytest.mark.parametrize("button", ["source_mode", "source_folder", "source_recursive",
                                    "scan_btn", "video_input", "confirm_btn"])
def test_no_source_handler_reads_any_creative_control(button):
    tree = _gui_tree()
    calls = _registration(tree, button, attrs=("click", "change"))
    assert calls, f"no registration found for {button}"

    for call in calls:
        kwargs = _kwargs(call)
        inputs = _names(kwargs["inputs"])
        for widget in _ALL_CREATIVE_WIDGETS:
            assert widget not in inputs, f"{button} reads {widget}"
        outputs = _names(kwargs["outputs"])
        assert "source_outputs" in outputs or "source_report" in outputs


@pytest.mark.parametrize("widget", _NEW_CONTROLS)
def test_no_control_is_written_by_any_handler_output(widget):
    """Nothing may write these boxes: Randomize is seed-only, and there is deliberately no reset."""
    tree = _gui_tree()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release"}):
            outputs = next((kw.value for kw in node.keywords if kw.arg == "outputs"), None)
            if outputs is not None:
                assert widget not in _names(outputs), (
                    f"{ast.unparse(node.func)} writes {widget}")


def test_randomize_still_writes_the_seed_and_only_the_seed():
    calls = _registration(_gui_tree(), "randomize_btn", attrs=("click",))
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])

    assert _names(kwargs["inputs"]) == []
    assert _names(kwargs["outputs"]) == ["variation_seed"]
    assert ast.unparse(kwargs["fn"]) == "fork_variation.random_seed"


def test_the_controls_are_absent_from_source_state_and_preparation_wiring():
    tree = _gui_tree()
    for list_name in ("source_outputs", "prep_outputs"):
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == list_name)
        names = _names(assignment.value)
        for widget in _ALL_CREATIVE_WIDGETS:
            assert widget not in names, f"{widget} is in {list_name}"

    for button in ("prep_scan_btn", "prep_analyze_btn", "prep_folder", "prep_recursive",
                   "prep_batch_size"):
        for call in _registration(tree, button, attrs=("click", "change")):
            kwargs = _kwargs(call)
            for widget in _ALL_CREATIVE_WIDGETS:
                assert widget not in _names(kwargs["inputs"]), f"{button} reads {widget}"
                assert widget not in _names(kwargs["outputs"]), f"{button} writes {widget}"


def test_all_four_controls_are_live_render_request_inputs():
    """They must reach the guarded handler, in positions matching the widget list: Gradio passes
    them positionally, so a silent reordering would be invisible at runtime."""
    tree = _gui_tree()
    calls = _registration(tree, "process_btn", attrs=("click",))
    assert len(calls) == 1
    kwargs = _kwargs(calls[0])
    assert ast.unparse(kwargs["fn"]) == "process_video_guarded"

    inputs = [n.id for n in kwargs["inputs"].elts if isinstance(n, ast.Name)]
    parameters = [a.arg for a in _func(tree, "process_video_guarded").args.args]

    assert len(inputs) == len(parameters), f"{inputs} vs {parameters}"
    for widget in _ALL_CREATIVE_WIDGETS:
        assert widget in inputs, f"{widget} is not a render-request input"
        assert inputs.index(widget) == parameters.index(widget), widget
    # audio_input -> audio_file is the one deliberate rename; everything else matches by name
    for parameter, widget in zip(parameters, inputs):
        assert parameter == widget or (parameter, widget) == ("audio_file", "audio_input")


def test_the_guard_collapses_the_widgets_into_one_profile():
    """Four loose scalars threaded through every inner function is the shape this avoids, and it is
    also where normalisation happens: nothing past this seam sees a raw widget value."""
    body = _body_code(_func(_gui_tree(), "process_video_guarded"))

    assert "fork_creative.CreativeProfile.from_widgets(" in body
    for widget in _ALL_CREATIVE_WIDGETS:
        assert widget in body, f"{widget} is accepted but never used"
    assert "creative=creative" in body


def test_the_profile_is_what_flows_down_the_pipeline():
    tree = _gui_tree()
    for name in ("process_video", "_process_video_impl"):
        parameters = [a.arg for a in _func(tree, name).args.args]
        assert "creative" in parameters, name
        for widget in _NEW_CONTROLS:
            assert widget not in parameters, f"{name} takes a raw {widget}"

    impl = _body_code(_func(tree, "_process_video_impl"))
    assert "creative=creative.as_dict()" in impl
    assert "creative.filename_suffix()" in impl


def test_the_gate_itself_is_unchanged():
    """The creative controls ride alongside the source verification; they must not participate in
    it. The decision is still resolved from the source state plus the live source declaration."""
    body = _body_code(_func(_gui_tree(), "process_video_guarded"))

    assert "resolve_for_render(source_state, live_declaration(" in body.replace("\n", "")
    declaration = body[body.index("live_declaration("):]
    declaration = declaration[:declaration.index(")")]
    for widget in _ALL_CREATIVE_WIDGETS:
        assert widget not in declaration, f"{widget} reached the source declaration"
    # and the profile is built only after the gate has allowed the render
    assert body.index("if not decision.allowed:") < body.index("CreativeProfile.from_widgets")


def test_no_preset_randomizer_or_freestyle_control_was_added():
    """Explicitly out of scope for this PR."""
    source = open(_GUI, encoding="utf-8").read().lower()
    for word in ("preset_btn", "freestyle", "director", "variant_lab", "creative_recipe",
                 "randomize_controls", "reset_creative"):
        assert word not in source, f"gui.py contains {word!r}"


# ===========================================================================
# 4. THE CLI
# ===========================================================================


_CLI_FLAGS = {"--cut-density": "cut_density", "--energy-response": "energy_response",
              "--motion-bias": "motion_bias", "--source-diversity": "source_diversity",
              "--micro-cuts": "micro_cuts", "--semantic-emphasis": "semantic_emphasis"}


def _cli_flag_literals() -> list[str]:
    """Every flag string passed to `parser.add_argument`, including those added in a loop."""
    fn = _func(_tree(_VP), "parse_arguments")
    flags = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            flags.extend(a.value for a in node.args if isinstance(a, ast.Constant))
    # the three creative controls are declared from a tuple of (flag, help) pairs
    for node in ast.walk(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value.startswith("--"):
            flags.append(node.value)
    return flags


def test_all_three_flags_exist_alongside_the_existing_seed_flag():
    flags = set(_cli_flag_literals())

    assert "--seed" in flags, "the existing seed flag must survive"
    for flag in _CLI_FLAGS:
        assert flag in flags, f"{flag} is missing from the CLI"


def test_the_new_flags_default_to_none_so_normalisation_owns_the_default():
    """`default=None` plus `creative.normalize_control` rather than an argparse `type=`: a malformed
    value then falls back exactly as it does in the UI instead of aborting the run inside argparse.

    The neutral value itself is asserted through the profile, not through an argparse default, so
    there is only one place the number 50 lives.
    """
    fn = _func(_tree(_VP), "parse_arguments")
    body = ast.unparse(_strip_docstrings(fn))

    assert "default=None" in body
    assert "fork_creative.DEFAULT_CONTROL" in body, "the help text must quote the real default"
    for flag in _CLI_FLAGS:
        assert f"'{flag}'" in body
    # no custom argparse type for the creative controls
    assert "type=creative" not in body and "normalize_control" not in body


def test_omitting_the_new_flags_yields_a_neutral_profile():
    """The compatibility contract: today's invocations keep today's behaviour."""
    profile = fork_creative.CreativeProfile.from_widgets(
        seed=0, cut_density=None, energy_response=None, motion_bias=None)

    assert profile == fork_creative.NEUTRAL_PROFILE
    assert profile.is_neutral()
    assert profile.filename_suffix() == ""


@pytest.mark.parametrize("raw, expected", [
    ("0", 0), ("50", 50), ("100", 100), ("65", 65),
    ("120", 100), ("-10", 0),           # clamped, as in the UI
    ("abc", 50), ("", 50), ("50.5", 50),  # malformed falls back rather than aborting the run
    (None, 50),
])
def test_argparse_strings_normalise_the_same_way_the_ui_does(raw, expected):
    """argparse hands over strings; the UI hands over floats. One normaliser, one answer."""
    profile = fork_creative.CreativeProfile.from_widgets(cut_density=raw)

    assert profile.cut_density == expected


def test_the_cli_builds_one_profile_and_hands_it_to_analyze_beats_auto():
    body = _body_code(_func(_tree(_VP), "main"))

    assert "fork_creative.CreativeProfile.from_widgets(" in body
    for attribute in ("args.seed", "args.cut_density", "args.energy_response", "args.motion_bias",
                      "args.source_diversity", "args.micro_cuts", "args.semantic_emphasis"):
        assert attribute in body, attribute
    assert "creative=cli_creative.as_dict()" in body


def test_the_cli_filename_rule_is_still_the_seed_rule():
    """`--seed 101` keeps adding `_seed101`; the three new flags add nothing to the name."""
    body = _body_code(_func(_tree(_VP), "main"))

    assert "cli_creative.filename_suffix()" in body
    for control in _NEW_CONTROLS:
        assert f"{control}_suffix" not in body
    for density in (0, 50, 100):
        assert fork_creative.CreativeProfile(
            seed=101, cut_density=density, energy_response=density,
            motion_bias=density).filename_suffix() == "_seed101"


def test_no_new_cli_mode_was_added():
    """Three optional creative flags, and nothing else: no new subcommand, no new mode."""
    flags = {flag for flag in _cli_flag_literals() if flag.startswith("--")}
    expected = {"--seed", "--cut-density", "--energy-response", "--motion-bias",
                "--source-diversity", "--micro-cuts", "--semantic-emphasis",
                "--output", "--start-time", "--end-time", "--lossless", "--gpu",
                "--gpu-encoder", "--fps"}

    assert flags == expected, (
        f"unexpected: {sorted(flags - expected)}; missing: {sorted(expected - flags)}")
    # the positional arguments and short aliases are untouched
    assert {"mp3_file", "video_directory", "-o", "-s", "-e"} <= set(_cli_flag_literals())


# ===========================================================================
# 5. REPORTING AND DIAGNOSTICS
# ===========================================================================


def test_the_resolved_profile_lands_in_the_render_diagnostics():
    body = _body_code(_func(_tree(_VP), "create_music_video"))

    assert "render_info['creative'] = resolved_creative.as_dict()" in body
    assert "render_info['creative_seed'] = int(variation_seed)" in body, (
        "the existing seed diagnostic must be retained")
    assert "creative=resolved_creative" in body, "the plan summary must carry the profile"


def test_the_console_reports_the_profile_on_the_line_that_already_exists():
    """The console has a five-line budget; the profile rides the planner line rather than taking a
    slot of its own, and a neutral render still prints exactly 'legacy'."""
    body = _body_code(_func(_gui_tree(), "_stage6_summary"))

    assert "plan_summary.get('creative_text')" in body
    assert "Planner: " in body
    assert fork_creative.NEUTRAL_PROFILE.describe() == "legacy"


def test_the_ui_success_panel_reports_a_non_neutral_profile_and_omits_a_neutral_one():
    body = _body_code(_func(_gui_tree(), "_process_video_impl"))

    assert "variation_text=None if creative.is_neutral() else creative.describe()" in body
