"""Audio Layers V1: the orchestration seam in `gui.py`.

Three properties matter more than everything else in D, and all three are about *which audio goes
where*:

1. **The original music is the only audio ever analysed.** Even with voice clips present,
   `analyze_beats_auto` receives `local_audio_path`, never the mixed master. If that ever inverted,
   adding a voice clip would change the beat grid, the sections and therefore the whole video edit —
   silently, and in a way no output inspection would obviously reveal.
2. **No voice means the exact original path.** Not a mixed-but-equivalent WAV: the same object that
   was passed before this feature existed.
3. **An audio failure stops the render before any clip is extracted.** The user asked for voice; a
   silent fallback to the music alone would produce a plausible, wrong video.

`gui.py` cannot be imported here (logger pulls in librosa and mutates PATH/CUDA_PATH), so the first
two are asserted with `ast` over the real source and the third by executing the orchestration body
against stubs.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import audio_mix as fork_audio_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")

AUDIO_WIDGETS = ("voice_files", "voice_start_delay", "voice_min_gap",
                 "voice_avoid_drops", "music_under_voice", "audio_layers_report")


def tree() -> ast.Module:
    with open(_GUI, "r", encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=_GUI)


def func(name: str) -> ast.FunctionDef:
    for node in ast.walk(tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def body_source(name: str) -> str:
    node = func(name)
    return "\n".join(ast.unparse(item) for item in node.body
                     if not (isinstance(item, ast.Expr)
                             and isinstance(item.value, ast.Constant)
                             and isinstance(item.value.value, str)))


def call_kwargs(name: str, fn_name: str) -> dict:
    for node in ast.walk(func(name)):
        if isinstance(node, ast.Call):
            rendered = ast.unparse(node.func)
            if rendered == fn_name or rendered.endswith("." + fn_name):
                return {kw.arg: ast.unparse(kw.value) for kw in node.keywords}, node
    raise AssertionError(f"{fn_name} not called in {name}")


def names_in(node) -> list:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


# ===========================================================================
# 1. THE MOST IMPORTANT TEST IN D
# ===========================================================================


def test_analyze_beats_auto_always_receives_the_original_music():
    """Never the mixed master, voice or no voice. The edit is decided before audio layering."""
    impl = func("_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "analyze_beats_auto":
            first_positional = ast.unparse(node.args[0])
            assert first_positional == "local_audio_path", first_positional
            rendered = ast.unparse(node)
            for forbidden in ("mixed_master", "render_audio_path", "audio_mixdown",
                              "prepared_voices"):
                assert forbidden not in rendered, rendered
            break
    else:
        raise AssertionError("analyze_beats_auto is not called in _process_video_impl")


def test_create_music_video_receives_the_substituted_path():
    impl = func("_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "create_music_video":
            assert ast.unparse(node.args[0]) == "render_audio_path"
            break
    else:
        raise AssertionError("create_music_video is not called in _process_video_impl")


def test_the_mixdown_happens_after_the_analysis_and_before_the_render():
    """Ordering is the architecture: analyse the music, then layer, then render."""
    source = body_source("_process_video_impl")
    analyse = source.index("analyze_beats_auto(")
    mix = source.index("audio_mixdown.build_mixed_master(")
    render = source.index("create_music_video(")
    assert analyse < mix < render


def test_the_mixed_master_is_never_fed_back_into_analysis():
    source = body_source("_process_video_impl")
    after_mix = source[source.index("audio_mixdown.build_mixed_master("):]
    assert "analyze_beats_auto" not in after_mix


def test_the_same_beats_and_beat_info_are_handed_to_the_renderer():
    """Voice must not re-derive or perturb anything the analysis produced."""
    impl = func("_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "create_music_video":
            rendered = ast.unparse(node)
            assert "selected_beats" in rendered
            assert "beat_info=beat_info" in rendered
            break


# ===========================================================================
# 2. NO VOICE = EXACT LEGACY PATH
# ===========================================================================


def test_render_audio_path_starts_as_the_original_and_only_voice_changes_it():
    source = body_source("_process_video_impl")
    assert "render_audio_path = local_audio_path" in source
    # the only reassignment is inside the voice branch
    assignments = [line.strip() for line in source.splitlines()
                   if line.strip().startswith("render_audio_path =")]
    assert assignments == ["render_audio_path = local_audio_path",
                           "render_audio_path = mixed_master_path"]


def test_every_audio_layer_step_is_behind_a_voice_guard():
    """With no voice clips: no ordering, no probe, no planner, no mixdown, no temporary WAV."""
    source = body_source("_process_video_impl")
    for guarded in ("audio_mixdown.prepare_voice_inputs(", "audio_mixdown.build_mixed_master("):
        assert guarded in source
    assert "if voice_files:" in source
    assert "if prepared_voices:" in source

    # the mixdown call must sit inside the `if prepared_voices:` block
    impl = func("_process_video_impl")
    guarded_calls = []
    for node in ast.walk(impl):
        if isinstance(node, ast.If) and ast.unparse(node.test) in ("voice_files",
                                                                   "prepared_voices"):
            guarded_calls.extend(
                ast.unparse(inner.func) for inner in ast.walk(node)
                if isinstance(inner, ast.Call))
    assert any("build_mixed_master" in c for c in guarded_calls)
    assert any("prepare_voice_inputs" in c for c in guarded_calls)


def test_prepared_voices_defaults_to_empty():
    assert "prepared_voices = ()" in body_source("_process_video_impl")


def test_the_success_panel_gains_nothing_without_voice():
    source = body_source("_process_video_impl")
    assert "if audio_plan is not None:" in source
    assert "audio_plan = None" in source


# ===========================================================================
# 3. FAILURE BEFORE ANY VIDEO WORK
# ===========================================================================


def test_the_voice_preflight_runs_before_the_analysis():
    """A bad voice file must not cost a full Stage 1-5 run."""
    source = body_source("_process_video_impl")
    assert source.index("audio_mixdown.prepare_voice_inputs(") < \
        source.index("analyze_beats_auto(")


def test_both_audio_failures_return_before_the_renderer():
    source = body_source("_process_video_impl")
    render_at = source.index("create_music_video(")
    handlers = [i for i in range(len(source))
                if source.startswith("except audio_mixdown.AudioMixError", i)]
    assert len(handlers) == 2, "expected a preflight handler and a mixdown handler"
    for position in handlers:
        assert position < render_at
    # and each one returns rather than continuing (`ast.unparse` parenthesises the tuple)
    for position in handlers:
        block = source[position:position + 400]
        assert "return (None," in block
        assert "Audio Layers" in block


def test_no_silent_fallback_to_the_original_music_on_failure():
    """The user asked for voice; producing a music-only video would look deliberate and be wrong."""
    source = body_source("_process_video_impl")
    for position in [i for i in range(len(source))
                     if source.startswith("except audio_mixdown.AudioMixError", i)]:
        block = source[position:position + 400]
        assert "render_audio_path = local_audio_path" not in block
        assert "pass" not in block


# ===========================================================================
# 4. TEMP OWNERSHIP AND CLEANUP
# ===========================================================================


def test_the_master_is_written_into_the_session_dir():
    """Never `get_processing_dir()`, which `create_music_video` clears at startup."""
    impl = func("_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and "build_mixed_master" in ast.unparse(node.func):
            kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
            assert kwargs["session_dir"] == "session_dir"
            assert kwargs["music_path"] == "local_audio_path"
            break
    else:
        raise AssertionError("build_mixed_master is not called")


def test_the_master_is_discarded_in_a_finally():
    node = func("_process_video_impl")
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "no try/finally in _process_video_impl"
    finally_source = "\n".join(ast.unparse(s) for t in tries for s in t.finalbody)
    assert "audio_mixdown.discard_master(mixed_master_path)" in finally_source


def test_mixed_master_path_is_initialised_before_the_try():
    """So the `finally` is safe on every path, including the early returns."""
    source = body_source("_process_video_impl")
    assert source.index("mixed_master_path = None") < source.index("try:")


# ===========================================================================
# 5. CONFIG NORMALISATION AT THE BOUNDARY
# ===========================================================================


def test_the_guard_collapses_the_widgets_into_one_normalised_config():
    source = body_source("process_video_guarded")
    assert "fork_audio_mix.AudioMixConfig(" in source
    for widget in ("voice_start_delay", "voice_min_gap", "voice_avoid_drops",
                   "music_under_voice"):
        assert widget in source
    assert "voice_files=voice_files" in source
    assert "audio_mix=audio_mix" in source


def test_the_config_is_built_only_after_the_gate_allows_the_render():
    source = body_source("process_video_guarded")
    assert source.index("if not decision.allowed:") < source.index("AudioMixConfig(")


def test_audio_widgets_never_reach_the_source_declaration():
    source = body_source("process_video_guarded")
    declaration = source[source.index("live_declaration("):]
    declaration = declaration[:declaration.index(")")]
    for widget in AUDIO_WIDGETS:
        assert widget not in declaration


# ===========================================================================
# 6. PROCESS WIRING
# ===========================================================================


def test_the_audio_controls_are_live_render_request_inputs_in_order():
    """Gradio passes `inputs` positionally, so the click list and the signature are one contract."""
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"
                and getattr(node.func.value, "id", None) == "process_btn"):
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            break
    else:
        raise AssertionError("process_btn.click not found")

    inputs = [n.id for n in kwargs["inputs"].elts if isinstance(n, ast.Name)]
    parameters = [a.arg for a in func("process_video_guarded").args.args]

    assert len(inputs) == len(parameters)
    for widget in ("voice_files", "voice_start_delay", "voice_min_gap",
                   "voice_avoid_drops", "music_under_voice"):
        assert widget in inputs
        assert inputs.index(widget) == parameters.index(widget), widget
    for parameter, widget in zip(parameters, inputs):
        assert parameter == widget or (parameter, widget) == ("audio_file", "audio_input")


def test_the_report_widget_is_not_a_render_input():
    """It is a read-out, not a request value."""
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"
                and getattr(node.func.value, "id", None) == "process_btn"):
            inputs = [n.id for n in node.keywords[1].value.elts if isinstance(n, ast.Name)]
            assert "audio_layers_report" not in inputs
            break


# ===========================================================================
# 7. SOURCE-GATE AND PREPARATION ISOLATION
# ===========================================================================


def test_audio_widgets_register_no_handlers():
    """Read at click time only; a handler could otherwise reach the gate."""
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release", "upload"}
                and isinstance(node.func.value, ast.Name)):
            assert node.func.value.id not in AUDIO_WIDGETS, ast.unparse(node.func)


def test_audio_widgets_are_absent_from_source_and_preparation_wiring():
    root = tree()
    for list_name in ("source_outputs", "prep_outputs", "creative_control_sliders",
                      "variant_lab_inputs", "variant_lab_outputs"):
        assignment = next(n for n in ast.walk(root) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == list_name)
        for widget in AUDIO_WIDGETS:
            assert widget not in names_in(assignment.value), f"{widget} in {list_name}"

    for button in ("source_mode", "source_folder", "source_recursive", "scan_btn", "video_input",
                   "confirm_btn", "prep_folder", "prep_recursive", "prep_batch_size",
                   "prep_scan_btn", "prep_analyze_btn"):
        for node in ast.walk(root):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"click", "change"}
                    and getattr(node.func.value, "id", None) == button):
                for kw in node.keywords:
                    if kw.arg in ("inputs", "outputs"):
                        for widget in AUDIO_WIDGETS:
                            assert widget not in names_in(kw.value), f"{button}.{kw.arg}"


def test_no_audio_widget_is_written_by_any_handler():
    """Nothing programmatically changes the user's voice selection or settings."""
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release"}):
            outputs = next((kw.value for kw in node.keywords if kw.arg == "outputs"), None)
            if outputs is None:
                continue
            for widget in AUDIO_WIDGETS:
                assert widget not in names_in(outputs), (
                    f"{ast.unparse(node.func)} writes {widget}")


# ===========================================================================
# 8. STAGE 5, PLANNER AND CREATIVE ISOLATION
# ===========================================================================


@pytest.mark.parametrize("relative", [
    "src/video_processor.py", "src/ffmpeg_processing.py", "src/video_analysis.py",
    "src/auto_mode/__init__.py", "src/auto_mode/stage4_select.py",
    "src/auto_mode/stage6_av_planner.py", "src/auto_mode/stage5_qwen_scene_worker.py",
    "src/beatsync_fork/creative.py", "src/beatsync_fork/creative_recipe.py",
    "src/beatsync_fork/variant_lab.py", "src/beatsync_fork/presets.py",
    "src/beatsync_fork/variation.py", "src/beatsync_fork/library_prep.py",
])
def test_no_pipeline_file_knows_about_audio_layers(relative: str):
    path = os.path.join(_REPO_ROOT, *relative.split("/"))
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read().lower()
    for word in ("audio_mix", "audio_mixdown", "voice_files", "music_under_voice",
                 "duckevent", "audiomixconfig", "voice_start_delay"):
        assert word not in source, f"{relative} mentions {word!r}"


def test_stage_five_constants_are_untouched():
    with open(os.path.join(_REPO_ROOT, "src", "video_analysis.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_no_audio_field_was_added_to_the_creative_objects():
    from beatsync_fork import creative, creative_recipe, presets, variant_lab

    profile = creative.CreativeProfile()
    for field in ("voice_files", "audio_mix", "music_under_voice", "voice_start_delay"):
        assert not hasattr(profile, field)
        assert field not in profile.as_dict()

    recipe = creative_recipe.CreativeRecipe(
        seed=1, **dict(zip(presets.CREATIVE_CONTROL_FIELDS, (50,) * 6)))
    for field in ("voice_files", "audio_mix", "music_under_voice"):
        assert not hasattr(recipe, field)
        assert field not in recipe.as_mapping()

    config = variant_lab.VariantLabConfig(master_seed=1)
    for field in ("voice_files", "audio_mix", "music_under_voice"):
        assert not hasattr(config, field)


def test_variant_lab_does_not_reach_audio_yet():
    """E2 is where that happens; D must not pre-empt it."""
    with open(os.path.join(_REPO_ROOT, "src", "gui.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert 'rng_for' not in source
    assert "DOMAIN_AUDIO" not in source


def test_no_cli_audio_layer_flag_was_added():
    with open(os.path.join(_REPO_ROOT, "src", "video_processor.py"), encoding="utf-8") as handle:
        source = handle.read()
    for flag in ("--voice", "--voice-gap", "--duck", "--music-under-voice", "--audio-layers",
                 "--start-delay"):
        assert flag not in source


# ===========================================================================
# 9. THE WIDGETS
# ===========================================================================


def widget_call(name: str) -> ast.Call:
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no widget assignment for {name}")


def test_the_accordion_is_collapsed_and_sits_under_the_audio_input():
    with open(_GUI, encoding="utf-8") as handle:
        source = handle.read()
    assert "gr.Accordion(label=LABEL_AUDIO_LAYERS, open=False)" in source
    assert source.index("audio_input = gr.File") < source.index("LABEL_AUDIO_LAYERS")
    assert source.index("LABEL_AUDIO_LAYERS") < source.index("### 🎬 Video Source")


def test_the_voice_picker_accepts_exactly_the_music_formats():
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in widget_call("voice_files").keywords}
    assert ast.unparse(widget_call("voice_files").func) == "gr.File"
    assert kwargs["file_count"] == "'multiple'"
    assert kwargs["type"] == "'filepath'"
    assert kwargs["file_types"] == "['.mp3', '.wav', '.flac']"
    assert ".m4a" not in kwargs["file_types"]


def test_the_defaults_are_the_shipped_ones():
    delay = {kw.arg: ast.unparse(kw.value) for kw in widget_call("voice_start_delay").keywords}
    assert delay["value"] == "fork_audio_mix.DEFAULT_START_DELAY_SECONDS"
    gap = {kw.arg: ast.unparse(kw.value) for kw in widget_call("voice_min_gap").keywords}
    assert gap["value"] == "fork_audio_mix.DEFAULT_MIN_GAP_SECONDS"
    avoid = {kw.arg: ast.unparse(kw.value) for kw in widget_call("voice_avoid_drops").keywords}
    assert avoid["value"] == "True"
    floor = {kw.arg: ast.unparse(kw.value) for kw in widget_call("music_under_voice").keywords}
    assert floor["value"] == "fork_audio_mix.DEFAULT_MUSIC_UNDER_VOICE_PERCENT"
    assert floor["step"] == "1"
    assert fork_audio_mix.DEFAULT_START_DELAY_SECONDS == 2.0
    assert fork_audio_mix.DEFAULT_MIN_GAP_SECONDS == 1.0
    assert fork_audio_mix.DEFAULT_MUSIC_UNDER_VOICE_PERCENT == 35


def test_no_hidden_limiter_or_envelope_widget():
    with open(_GUI, encoding="utf-8") as handle:
        source = handle.read().lower()
    for forbidden in ("limiter", "duck_attack", "duck_release", "voice_gain", "voice_volume",
                      "lookahead"):
        assert f"{forbidden} = gr." not in source


def test_the_help_text_states_the_order_and_the_percent_contract():
    with open(os.path.join(_REPO_ROOT, "src", "ui_content.py"), encoding="utf-8") as handle:
        source = handle.read()
    layers = source[source.index("INFO_AUDIO_LAYERS"):]
    layers = layers[:layers.index("\n)")]
    assert "filename order" in layers
    assert "01_intro" in layers
    assert "final audio only" in layers

    floor = source[source.index("INFO_MUSIC_UNDER_VOICE"):]
    floor = floor[:floor.index("\n)")]
    assert "Percent of the normal music level" in floor
    assert "dB" not in floor
