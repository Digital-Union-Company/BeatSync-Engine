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
from beatsync_fork import smart_mix as fork_smart_mix

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _executable_source(path: str) -> str:
    """Source with docstrings stripped, so prose *stating* a boundary is never read as crossing it.

    The same idiom the other seam suites use. Comments disappear too, because `ast.unparse` emits
    only executable structure — which is what makes an "X is never mentioned" assertion meaningful
    in a repository whose modules document their own contracts at length.
    """
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")

#: The five user-settable controls. These are render-request inputs and must NEVER be written back.
AUDIO_CONFIG_WIDGETS = ("voice_files", "voice_start_delay", "voice_min_gap",
                        "voice_avoid_drops", "music_under_voice")

#: The read-only placement read-out. It is an output of the render event and of nothing else.
AUDIO_REPORT_WIDGET = "audio_layers_report"

AUDIO_WIDGETS = AUDIO_CONFIG_WIDGETS + (AUDIO_REPORT_WIDGET,)

#: E2 V1 (§14) — the writer matrix, split rather than weakened. Exactly **one** Audio Layers
#: configuration widget became writable, and only by the two Variant Lab buttons; the other four
#: remain zero-writer values that nothing in the app may set programmatically.
AUDIO_VARIANT_WRITABLE = ("music_under_voice",)
AUDIO_NEVER_WRITTEN = tuple(w for w in AUDIO_CONFIG_WIDGETS
                            if w not in AUDIO_VARIANT_WRITABLE)

#: The only two events permitted to write any Variant Lab audio output.
VARIANT_LAB_WRITERS = ["generate_variant_btn.click", "new_variant_btn.click"]


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


def _list_assignment(root, name):
    """The value assigned to `name`, but only when it is a list or a concatenation of lists.

    A widget assignment (`music_under_voice = gr.Slider(...)`) is an `Assign` too, so without this
    guard the expander below would walk into a `gr.Slider` call and report its keyword names.
    """
    for node in ast.walk(root):
        if (isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, (ast.List, ast.BinOp))):
            return node.value
    return None


def expanded_names_in(root, node, _depth=0) -> list:
    """`names_in`, with locally-assigned *list* variables resolved into their own elements.

    This is load-bearing for E2's writer matrix rather than a convenience. `variant_lab_outputs` is
    built by concatenating named sub-lists, so a plain `names_in` sees `variant_lab_audio_bases` and
    never `music_under_voice`. Every "is this widget ever written?" guard in this file would then
    pass **vacuously** the moment a widget moved behind one level of indirection — which is exactly
    the evasion these guards exist to prevent, and it is how a split guard silently becomes no
    guard at all.
    """
    assert _depth < 6, "list indirection is deeper than expected"
    out = []
    for name in names_in(node):
        nested = _list_assignment(root, name)
        if nested is not None:
            out.extend(expanded_names_in(root, nested, _depth + 1))
        else:
            out.append(name)
    return out


def _writers_of(root, widget: str) -> list:
    """Every event registration whose (expanded) `outputs` contains `widget`."""
    writers = []
    for node in ast.walk(root):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release"}):
            outputs = next((kw.value for kw in node.keywords if kw.arg == "outputs"), None)
            if outputs is not None and widget in expanded_names_in(root, outputs):
                writers.append(ast.unparse(node.func))
    return writers


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
    """With no voice clips: no ordering, no probe, no voice planner, no temporary WAV.

    **Amended by Smart Mix V1 / E.** The mixdown guard widened from ``if prepared_voices:`` to
    ``if prepared_voices or sfx_placements:``, because an SFX-only render legitimately needs a
    master too. The property being protected is unchanged and is now asserted more precisely: the
    *voice* work stays behind a voice-only guard, and the mixdown runs only when there is actually
    something to mix.
    """
    source = body_source("_process_video_impl")
    for guarded in ("audio_mixdown.prepare_voice_inputs(", "audio_mixdown.build_mixed_master("):
        assert guarded in source
    assert "if voice_files:" in source
    assert "if prepared_voices or sfx_placements:" in source

    impl = func("_process_video_impl")
    voice_only_calls, mix_calls = [], []
    for node in ast.walk(impl):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        calls = [ast.unparse(inner.func) for inner in ast.walk(node)
                 if isinstance(inner, ast.Call)]
        if test in ("voice_files", "prepared_voices"):
            voice_only_calls.extend(calls)
        elif test == "prepared_voices or sfx_placements":
            mix_calls.extend(calls)
    assert any("prepare_voice_inputs" in c for c in voice_only_calls)
    assert any("build_mixed_master" in c for c in mix_calls)
    # and the mixdown is NOT reachable outside that guard
    assert source.count("audio_mixdown.build_mixed_master(") == 1


def test_prepared_voices_defaults_to_empty():
    assert "prepared_voices = ()" in body_source("_process_video_impl")


def test_the_success_panel_gains_nothing_without_voice():
    """**Amended by Smart Mix V1 / E.** An SFX-only render now also produces an ``audio_plan`` (with
    zero voice placements), so ``audio_plan is not None`` alone would print a voice summary for a
    render that had no voice. The guard gained ``and prepared_voices``, which is strictly stronger.
    """
    source = body_source("_process_video_impl")
    assert "if audio_plan is not None and prepared_voices:" in source
    assert "audio_plan = None" in source


# ===========================================================================
# 3. FAILURE BEFORE ANY VIDEO WORK
# ===========================================================================


def test_the_voice_preflight_runs_before_the_analysis():
    """A bad voice file must not cost a full Stage 1-5 run."""
    source = body_source("_process_video_impl")
    assert source.index("audio_mixdown.prepare_voice_inputs(") < \
        source.index("analyze_beats_auto(")


def test_every_audio_failure_returns_before_the_renderer():
    """**Amended by Smart Mix V1 / E**: three handlers now, not two — the voice preflight, the Smart
    Mix library preflight and the shared mixdown. The property is unchanged and still the point:
    every one of them sits before ``create_music_video`` and returns instead of continuing."""
    source = body_source("_process_video_impl")
    render_at = source.index("create_music_video(")
    handlers = [i for i in range(len(source))
                if source.startswith("except audio_mixdown.AudioMixError", i)]
    assert len(handlers) == 3, "expected two preflight handlers and a mixdown handler"
    for position in handlers:
        assert position < render_at
    # and each one returns rather than continuing (`ast.unparse` parenthesises the tuple)
    labels = []
    for position in handlers:
        block = source[position:position + 400]
        assert "return (None," in block
        assert ("Audio Layers" in block) or ("Smart Mix" in block)
        labels.append("Smart Mix" if "Smart Mix" in block else "Audio Layers")
    assert labels.count("Smart Mix") == 1, "the SFX preflight must name Smart Mix, not Audio Layers"


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
    """**Split by E2 V1.** Source and preparation wiring stays entirely audio-free; the Variant Lab
    lists are allowed exactly the three resolved levels and nothing else.

    Every lookup is `expanded_names_in`, so the named sub-lists E2 introduced cannot hide a widget
    from this assertion — without that this test would have kept passing while `variant_lab_outputs`
    quietly grew the whole Audio Layers block.
    """
    root = tree()
    for list_name in ("source_outputs", "prep_outputs", "creative_control_sliders"):
        assignment = next(n for n in ast.walk(root) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == list_name)
        for widget in AUDIO_WIDGETS:
            assert widget not in expanded_names_in(root, assignment.value), \
                f"{widget} in {list_name}"

    permitted = set(AUDIO_VARIANT_WRITABLE) | set(SMART_MIX_VARIANT_WRITABLE)
    for list_name in ("variant_lab_inputs", "variant_lab_outputs"):
        assignment = next(n for n in ast.walk(root) if isinstance(n, ast.Assign)
                          and getattr(n.targets[0], "id", None) == list_name)
        present = set(expanded_names_in(root, assignment.value))
        for widget in AUDIO_WIDGETS:
            if widget in permitted:
                continue
            assert widget not in present, f"{widget} in {list_name}"
        # the three permitted ones really are there — a guard that allowed them without
        # confirming them would not notice E2 silently losing its outputs
        assert permitted <= present, f"{list_name} lost {permitted - present}"

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
                            assert widget not in expanded_names_in(root, kw.value), \
                                f"{button}.{kw.arg}"


def test_no_audio_configuration_widget_is_ever_written():
    """Nothing programmatically changes the user's voice selection or settings.

    **Amended by R1-B.** This forbade *every* Audio Layers widget from being written, which made
    the read-only `audio_layers_report` dead — declared but with no writer, so the advertised
    placement report could never display anything. The invariant split rather than weakened: the
    five *configuration* widgets stay unwritable, and the report gets exactly one permitted writer
    (asserted separately below). Renaming around the guard would have been evasion.

    **Amended again by E2 V1**, and split the same way rather than loosened: `music_under_voice`
    gains exactly two permitted writers (the test below names them), while the remaining four stay
    at **zero** writers. Note `expanded_names_in` — the Variant Lab lists reach their widgets
    through a named sub-list, and a non-expanding check here would have passed vacuously.
    """
    root = tree()
    for widget in AUDIO_NEVER_WRITTEN:
        assert _writers_of(root, widget) == [], widget


def test_music_under_voice_is_written_only_by_the_two_variant_lab_buttons():
    """E2 V1's half of the split guard: one widget, exactly two writers, named explicitly.

    If a future change wires this slider into any other event — a preset handler, a source handler,
    a reset button — this fails. That is the whole point of pinning the writer *list* rather than
    merely asserting "it has a writer".
    """
    root = tree()
    assert _writers_of(root, "music_under_voice") == VARIANT_LAB_WRITERS

    # and the guard is not vacuous: the widget really is reached through the sub-list indirection
    outputs = next(kw.value for node in ast.walk(root)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "click"
                   and getattr(node.func.value, "id", None) == "generate_variant_btn"
                   for kw in node.keywords if kw.arg == "outputs")
    assert "music_under_voice" not in names_in(outputs)
    assert "music_under_voice" in expanded_names_in(root, outputs)


def test_the_report_has_exactly_one_writer_and_it_is_the_render_event():
    """R1-B: the report must have a real writer, and only one.

    **R1 (E2) note.** `_writers_of` rather than a hand-rolled `names_in` scan, so this cannot be
    evaded by routing the report through a named sub-list the way `variant_lab_outputs` reaches
    its widgets. The assertion is unchanged; only its blind spot is gone.
    """
    assert _writers_of(tree(), "audio_layers_report") == ["process_btn.click"]


def test_no_source_preparation_preset_or_variant_handler_writes_the_report():
    root = tree()
    for button in ("source_mode", "source_folder", "source_recursive", "scan_btn", "video_input",
                   "confirm_btn", "prep_folder", "prep_recursive", "prep_batch_size",
                   "prep_scan_btn", "prep_analyze_btn", "creative_preset", "randomize_btn",
                   "generate_variant_btn", "new_variant_btn"):
        for node in ast.walk(root):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and getattr(node.func.value, "id", None) == button):
                for kw in node.keywords:
                    if kw.arg in ("inputs", "outputs"):
                        # expanded: the two Variant Lab buttons really do pass their widgets
                        # through named sub-lists, so the plain form would assert nothing here
                        assert "audio_layers_report" not in expanded_names_in(root, kw.value), \
                            button


# ===========================================================================
# 7b. R1-A / R1-B: the two corrections, asserted on the real orchestration
# ===========================================================================


def test_the_voice_preflight_receives_the_unfiltered_selection():
    """**R1-A.** `_as_existing_source_paths` filters out paths that no longer exist — right for
    video sources, wrong for voice, where it turned a three-clip selection with a missing middle
    file into a silent two-clip render."""
    source = body_source("_process_video_impl")
    assert "audio_mixdown.prepare_voice_inputs(voice_files)" in source
    assert "_as_existing_source_paths(voice_files)" not in source

    # the call passes the raw widget value and nothing else
    impl = func("_process_video_impl")
    for node in ast.walk(impl):
        if isinstance(node, ast.Call) and "prepare_voice_inputs" in ast.unparse(node.func):
            assert [ast.unparse(a) for a in node.args] == ["voice_files"]
            break
    else:
        raise AssertionError("prepare_voice_inputs is not called")


def test_the_video_source_use_of_the_filter_helper_is_untouched():
    """The helper is still correct for video sources, which the confirmation gate has vouched for."""
    source = body_source("_process_video_impl")
    assert "_as_existing_source_paths(video_files)" in source


#: Statement selectors for the three real audio regions of `_process_video_impl`. The report
#: lifecycle is *behaviour* — cleared, then populated, and specifically NOT populated on either
#: failure path — which no structural assertion can show, so the real statements are extracted and
#: executed rather than mirrored by hand. Mirroring would let the test keep passing after the
#: production code stopped doing what the mirror says.
def _audio_region_statements() -> list:
    impl = func("_process_video_impl")
    picked = []
    for node in ast.walk(impl):
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript)
                and getattr(node.targets[0].slice, "id", None) == "AUDIO_LAYERS_REPORT_KEY"
                and getattr(node.value, "value", None) == ""):
            picked.append(node)
        elif isinstance(node, ast.Assign) and ast.unparse(node) in (
                "prepared_voices = ()", "render_audio_path = local_audio_path",
                "sfx_placements = ()"):
            picked.append(node)
        elif isinstance(node, ast.If) and ast.unparse(node.test) in (
                "voice_files", "prepared_voices or sfx_placements"):
            picked.append(node)
    picked.sort(key=lambda n: n.lineno)
    # report-clear, prepared_voices = (), if voice_files, render_audio_path = …,
    # sfx_placements = (), if prepared_voices or sfx_placements
    assert len(picked) == 6, [ast.unparse(p)[:60] for p in picked]
    return picked


_AUDIO_BLOCK_ARGS = ("voice_files", "session_state", "local_audio_path", "beat_info", "beat_times",
                     "audio_mix", "session_dir", "console_logger", "audio_mixdown",
                     "fork_audio_mix", "AUDIO_LAYERS_REPORT_KEY", "smart_mix")


def _compile_audio_block():
    """Wrap the extracted statements in a function so their real `return`s work."""
    statements = _audio_region_statements()
    prologue = ast.parse(
        "mixed_master_path = None\naudio_plan = None\n"
        "smart_mix_plan = None\nsmart_mix_active = False\nprepared_voices = ()").body
    epilogue = ast.parse(
        "return render_audio_path, session_state[AUDIO_LAYERS_REPORT_KEY], audio_plan").body
    fn = ast.FunctionDef(
        name="_audio_block",
        args=ast.arguments(posonlyargs=[], args=[ast.arg(arg=a) for a in _AUDIO_BLOCK_ARGS],
                           vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
        body=prologue + statements + epilogue,
        decorator_list=[], returns=None, type_params=[])
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    namespace = {}
    exec(compile(module, _GUI, "exec"), namespace)
    return namespace["_audio_block"]


def _run_report_lifecycle(voice_files, voice_result, plan=None, mix_error=None):
    """Execute the REAL extracted audio region; return (state, calls, error_message)."""
    calls = []

    class Err(Exception):
        pass

    class FakeMixdown:
        AudioMixError = Err

        @staticmethod
        def prepare_voice_inputs(selection):
            calls.append(("prepare", selection))
            if isinstance(voice_result, BaseException):
                raise Err(str(voice_result))
            return voice_result

        @staticmethod
        def probe_duration(path):
            calls.append(("probe", path))
            return 200.0

        @staticmethod
        def build_mixed_master(**kwargs):
            calls.append(("mix", kwargs))
            if mix_error is not None:
                raise Err(str(mix_error))
            return "C:/session/mix.wav", plan

    state = {"audio_layers_report": "STALE REPORT FROM A PREVIOUS RENDER"}
    result = _compile_audio_block()(
        voice_files=voice_files,
        session_state=state,
        local_audio_path="C:/music/track.mp3",
        beat_info={"audio_duration": 200.0, "sections": None},
        beat_times=[0.0, 1.0, 2.0],
        audio_mix=None,
        session_dir="C:/session",
        console_logger=None,
        audio_mixdown=FakeMixdown,
        fork_audio_mix=fork_audio_mix,
        AUDIO_LAYERS_REPORT_KEY="audio_layers_report",
        # A real normalised config, exactly as `_process_video_impl` resolves before this region.
        # Smart Mix is still inactive for these cases — no root is given, so `sfx_placements` stays
        # `()` and the mixdown guard behaves exactly as D's did. This harness proves the *voice*
        # report lifecycle.
        smart_mix=fork_smart_mix.SmartMixConfig(),
    )
    if result[0] is None:                       # a real refusal: (None, '❌ …', session_state)
        return state, calls, result[1]
    return state, calls, None


def _sample_plan():
    config = fork_audio_mix.AudioMixConfig()
    placements = (
        fork_audio_mix.VoicePlacement(0, "C:/v/01_intro.wav", 3.2, 12.4, 15.6, "verse",
                                      fork_audio_mix.ANCHOR_PREFERRED_SECTION, 0.0),
        fork_audio_mix.VoicePlacement(1, "C:/v/02_quote.wav", 4.1, 20.0, 24.1, "breakdown",
                                      fork_audio_mix.ANCHOR_BEAT, 1.5),
    )
    return fork_audio_mix.AudioMixPlan(
        200.0, placements,
        fork_audio_mix.build_duck_events(placements, 200.0, config), config)


def test_a_successful_plan_populates_the_report():
    plan = _sample_plan()
    state, _calls, error = _run_report_lifecycle(["a.wav", "b.wav"], ("v1", "v2"), plan=plan)
    report = state["audio_layers_report"]

    assert error is None
    assert report
    assert "01_intro.wav" in report and "02_quote.wav" in report
    assert report.index("01_intro.wav") < report.index("02_quote.wav")   # resolved order
    assert "12.4" in report and "15.6" in report                          # planned times
    assert "verse" in report and fork_audio_mix.ANCHOR_PREFERRED_SECTION in report
    assert "Music under voice: 35%" in report


def test_no_voice_leaves_a_blank_report():
    state, calls, error = _run_report_lifecycle([], ())
    assert error is None
    assert state["audio_layers_report"] == ''
    assert calls == [], "no voice must mean no probing and no mixdown"


def test_a_preflight_failure_leaves_no_stale_report():
    state, calls, error = _run_report_lifecycle(
        ["a.wav", "b.wav"], AssertionError("Voice clip is missing or unreadable: b.wav"))
    assert state["audio_layers_report"] == ''
    assert "missing or unreadable" in error
    assert [kind for kind, _ in calls] == ["prepare"], "the mixdown must not be reached"


def test_a_mix_failure_leaves_no_stale_report():
    # a previous successful render really did leave a report behind
    previous, _calls, _err = _run_report_lifecycle(["a.wav"], ("v1",), plan=_sample_plan())
    assert previous["audio_layers_report"]

    state, calls, error = _run_report_lifecycle(
        ["a.wav"], ("v1",), mix_error=AssertionError("Audio mixdown failed: boom"))
    assert state["audio_layers_report"] == ''
    assert "mixdown failed" in error
    assert "mix" in [kind for kind, _ in calls]


def test_the_report_is_cleared_before_the_gate_and_before_the_attempt():
    """Every path through the handlers starts from a blank report, including a refused render."""
    guarded = body_source("process_video_guarded")
    impl = body_source("_process_video_impl")
    assert "session_state[AUDIO_LAYERS_REPORT_KEY] = ''" in guarded
    assert guarded.index("AUDIO_LAYERS_REPORT_KEY] = ''") < guarded.index("resolve_for_render(")
    assert "session_state[AUDIO_LAYERS_REPORT_KEY] = ''" in impl
    assert impl.index("AUDIO_LAYERS_REPORT_KEY] = ''") < impl.index("try:")


def test_a_refused_render_yields_a_blank_report():
    """The gate's own refusal is a yield, so it has to carry the fourth value itself."""
    refusal = next(node for node in ast.walk(func("process_video_guarded"))
                   if isinstance(node, ast.If)
                   and ast.unparse(node.test) == "not decision.allowed")
    yielded = next(n for n in ast.walk(refusal) if isinstance(n, ast.Yield))
    assert isinstance(yielded.value, ast.Tuple)
    # 5 since Smart Mix V1 / E: video, status, state, Audio Layers report, Smart Mix report
    assert len(yielded.value.elts) == 5, ast.unparse(yielded)
    for index in (3, 4):
        assert isinstance(yielded.value.elts[index], ast.Constant)
        assert yielded.value.elts[index].value == ""


def test_the_guard_projects_the_three_value_stream_onto_five_outputs():
    """`process_video` keeps its 3-value contract; only the outermost handler projects.

    **Amended by Smart Mix V1 / E**: the projection gained the Smart Mix read-out. The invariant
    the test exists for is untouched and is the stronger half — the *inner* generator's streaming
    contract did not change, so the worker thread still carries nothing but `session_state`.
    """
    guarded = body_source("process_video_guarded")
    assert "for video, status, state in process_video(" in guarded
    assert ("yield (video, status, state, (state or {}).get(AUDIO_LAYERS_REPORT_KEY, ''), "
            "(state or {}).get(SMART_MIX_REPORT_KEY, ''))") in guarded
    # the inner generator's 3-value contract is unchanged; only the outer handler is 5-valued
    assert ast.unparse(func("process_video").returns) == "Iterator[StatusResult]"
    assert ast.unparse(func("process_video_guarded").returns) == "Iterator[GuardedResult]"


def test_the_report_is_populated_from_the_planner_and_never_recomputed():
    """The GUI quotes the plan it was given; it does not re-derive placement for display."""
    source = body_source("_process_video_impl")
    assert "'\\n'.join(audio_plan.report_lines())" in source
    for forbidden in ("plan_voice_placements", "build_duck_events", "duck_gain_at",
                      "build_duck_expression", "order_voice_paths"):
        assert forbidden not in source, forbidden


def test_the_report_key_is_a_single_named_constant():
    root = tree()
    assignment = next(n for n in ast.walk(root) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", None) == "AUDIO_LAYERS_REPORT_KEY")
    assert ast.literal_eval(assignment.value) == "audio_layers_report"


def test_the_report_never_reaches_the_pipeline():
    """Diagnostics only. No stage, no planner, no mixdown and no cache key may receive it."""
    for callee in ("analyze_beats_auto", "create_music_video", "build_mixed_master",
                   "prepare_voice_inputs", "project_sections"):
        for node in ast.walk(func("_process_video_impl")):
            if (isinstance(node, ast.Call)
                    and ast.unparse(node.func).endswith(callee)):
                rendered = ast.unparse(node)
                assert "AUDIO_LAYERS_REPORT_KEY" not in rendered, callee
                assert "audio_layers_report" not in rendered, callee


def test_the_report_is_an_output_only_and_never_a_render_input():
    click = next(node for node in ast.walk(tree())
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and ast.unparse(node.func) == "process_btn.click")
    kwargs = {kw.arg: kw.value for kw in click.keywords}
    assert "audio_layers_report" not in names_in(kwargs["inputs"])
    assert "smart_mix_report" not in names_in(kwargs["inputs"])
    outputs = names_in(kwargs["outputs"])
    assert outputs == ["video_output", "status_output", "session_state",
                       "audio_layers_report", "smart_mix_report"]


def test_the_report_is_absent_from_the_live_source_declaration():
    """It is render diagnostics, not a declaration of what is being rendered."""
    for node in ast.walk(tree()):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("live_declaration"):
            rendered = ast.unparse(node)
            for widget in AUDIO_WIDGETS:
                assert widget not in rendered, widget


def test_the_worker_thread_touches_no_gradio_component():
    """The report rides on `session_state`; the generator does the widget update."""
    worker = None
    for node in ast.walk(func("process_video")):
        if isinstance(node, ast.FunctionDef) and node.name == "worker":
            worker = node
            break
    assert worker is not None
    rendered = ast.unparse(worker)
    assert "audio_layers_report" not in rendered
    assert "gr." not in rendered


# ===========================================================================
# 8. STAGE 5, PLANNER AND CREATIVE ISOLATION
# ===========================================================================


@pytest.mark.parametrize("relative", [
    "src/video_processor.py", "src/ffmpeg_processing.py", "src/video_analysis.py",
    "src/auto_mode/__init__.py", "src/auto_mode/stage4_select.py",
    "src/auto_mode/stage6_av_planner.py", "src/auto_mode/stage5_qwen_scene_worker.py",
    "src/beatsync_fork/creative.py", "src/beatsync_fork/creative_recipe.py",
    "src/beatsync_fork/presets.py",
    "src/beatsync_fork/variation.py", "src/beatsync_fork/library_prep.py",
])
def test_no_pipeline_file_knows_about_audio_layers(relative: str):
    """`variant_lab.py` is deliberately **absent** from this list since E2 V1.

    It is the one module that legitimately knows three audio *values*, so a blanket "mentions no
    audio token" assertion would have to be deleted to let E2 land — exactly the weakening this
    suite exists to prevent. The guard is therefore split rather than dropped: every other file
    keeps the blanket rule at full strength, and `variant_lab.py` gets
    `test_variant_lab_knows_three_audio_values_and_nothing_else`, which enumerates the excluded
    controls by name and is strictly *stronger* than the blanket form for that file.
    """
    path = os.path.join(_REPO_ROOT, *relative.split("/"))
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read().lower()
    for word in ("audio_mix", "audio_mixdown", "voice_files", "music_under_voice",
                 "duckevent", "audiomixconfig", "voice_start_delay"):
        assert word not in source, f"{relative} mentions {word!r}"


def test_variant_lab_knows_three_audio_values_and_nothing_else():
    """E2 V1's exact knowledge boundary inside the resolver.

    What it may know: the three 0..100 levels, and the two normaliser modules it delegates to so
    each control keeps its own malformed-value default. What it must never acquire: a voice clip, a
    path, a role, an executor config, a plan object, or any FFmpeg concept.
    """
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_lab.py")
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()

    for permitted in ("music_under_voice_percent", "sfx_amount", "sfx_level_percent",
                      "AUDIO_CONTROL_FIELDS", "AudioRecipe", "resolve_audio"):
        assert permitted in source, f"variant_lab lost {permitted!r}"

    executable = _executable_source(path)
    for forbidden in ("voice_files", "voice_start_delay", "voice_min_gap", "avoid_drops",
                      "sfx_folder", "sfx_roles", "enabled_roles", "AudioMixConfig",
                      "SmartMixConfig", "AudioMixPlan", "SmartMixPlan", "DuckEvent",
                      "VoicePlacement", "SfxPlacement", "SfxAsset", "audio_mixdown",
                      "ffmpeg", "subprocess"):
        assert forbidden not in executable, f"variant_lab references {forbidden!r}"


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


def test_the_gui_delegates_audio_variation_and_implements_none_of_it():
    """Reconciled from D's `test_variant_lab_does_not_reach_audio_yet` — **not** deleted.

    E2 V1 landed, so "Variant Lab does not reach audio" is no longer true. The *useful* half of
    that guard is, though, and it is now the stronger statement: the GUI never derives a random
    audio value itself. It calls `fork_lab.resolve_audio(...)` and the pure resolver in
    `variant_lab.py` is the only place the `audio` RNG domain is consumed — so there is exactly one
    implementation of the audio draw, and it is the testable stdlib-only one.
    """
    with open(os.path.join(_REPO_ROOT, "src", "gui.py"), encoding="utf-8") as handle:
        source = handle.read()

    # the GUI owns no RNG derivation and no domain logic of its own. (A bare `'audio'` literal is
    # deliberately NOT forbidden: `cleanup_on_startup`'s preserved-directory set has contained one
    # since long before E2, and forbidding it would be a false coupling.)
    assert 'rng_for' not in source
    assert "DOMAIN_AUDIO" not in source

    # it delegates instead, and the resolver is the sole consumer of the domain
    assert "fork_lab.resolve_audio(" in source
    lab = _executable_source(
        os.path.join(_REPO_ROOT, "src", "beatsync_fork", "variant_lab.py"))
    assert "DOMAIN_AUDIO" in lab


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


# ===========================================================================
# SMART MIX V1 (E): the Smart Mix seam
# ===========================================================================

#: The four user-settable Smart Mix controls. Render-request inputs; never written back.
SMART_MIX_CONFIG_WIDGETS = ("sfx_folder", "sfx_roles", "sfx_amount", "sfx_level")

#: E2 V1 (§14): the two numeric levels became Variant Lab outputs; the folder and the role
#: selection are resource identity and structural intent, so they stay zero-writer.
SMART_MIX_VARIANT_WRITABLE = ("sfx_amount", "sfx_level")
SMART_MIX_NEVER_WRITTEN = tuple(w for w in SMART_MIX_CONFIG_WIDGETS
                                if w not in SMART_MIX_VARIANT_WRITABLE)

#: The read-only placement read-out. An output of the render event and of nothing else.
SMART_MIX_REPORT_WIDGET = "smart_mix_report"


def test_the_smart_mix_accordion_is_collapsed_and_declares_five_components():
    root = tree()
    accordion = None
    for node in ast.walk(root):
        if (isinstance(node, ast.withitem)
                and isinstance(node.context_expr, ast.Call)
                and ast.unparse(node.context_expr.func).endswith("Accordion")):
            kwargs = {kw.arg: kw.value for kw in node.context_expr.keywords}
            label = kwargs.get("label")
            if label is not None and "SMART_MIX" in ast.unparse(label):
                accordion = kwargs
    assert accordion is not None, "the Smart Mix accordion was not found"
    assert ast.literal_eval(accordion["open"]) is False, "it must start collapsed"


def test_the_five_smart_mix_components_and_their_defaults():
    root = tree()
    found = {}
    for node in ast.walk(root):
        if not isinstance(node, ast.Assign):
            continue
        name = getattr(node.targets[0], "id", None)
        if name in SMART_MIX_CONFIG_WIDGETS + (SMART_MIX_REPORT_WIDGET,):
            assert isinstance(node.value, ast.Call)
            found[name] = (ast.unparse(node.value.func),
                           {kw.arg: kw.value for kw in node.value.keywords})
    assert set(found) == set(SMART_MIX_CONFIG_WIDGETS) | {SMART_MIX_REPORT_WIDGET}

    assert found["sfx_folder"][0] == "gr.Textbox"
    assert found["sfx_roles"][0] == "gr.CheckboxGroup"
    assert found["sfx_amount"][0] == "gr.Slider"
    assert found["sfx_level"][0] == "gr.Slider"
    assert found["smart_mix_report"][0] == "gr.Textbox"

    for name in ("sfx_amount", "sfx_level"):
        kwargs = found[name][1]
        assert ast.literal_eval(kwargs["step"]) == 1
        assert ast.unparse(kwargs["minimum"]).endswith("CONTROL_MIN")
        assert ast.unparse(kwargs["maximum"]).endswith("CONTROL_MAX")
    assert ast.unparse(found["sfx_amount"][1]["value"]).endswith("DEFAULT_AMOUNT")
    assert ast.unparse(found["sfx_level"][1]["value"]).endswith("DEFAULT_SFX_LEVEL_PERCENT")

    # all five roles ticked by default, taken from the frozen vocabulary rather than retyped
    assert "ROLE_CHOICES" in ast.unparse(found["sfx_roles"][1]["choices"])
    assert "ROLE_CHOICES" in ast.unparse(found["sfx_roles"][1]["value"])

    report = found["smart_mix_report"][1]
    assert ast.literal_eval(report["value"]) == ""
    assert ast.literal_eval(report["interactive"]) is False


def test_there_is_no_scan_button():
    """V1 validates the library in the render preflight; a second scan path would drift from it."""
    source = ast.unparse(tree())
    for forbidden in ("sfx_scan_btn", "smart_mix_scan_btn", "sfx_scan_button"):
        assert forbidden not in source


def test_no_smart_mix_config_widget_is_ever_written():
    """**Split by E2 V1**, not weakened.

    `sfx_amount` and `sfx_level` are numeric levels and gain exactly two writers (below). The two
    *structural* controls — the library folder and the enabled roles — stay at **zero** writers,
    because a randomly retargeted folder or a randomly toggled role is a change to what the user
    asked for rather than a variation of it.
    """
    root = tree()
    for widget in SMART_MIX_NEVER_WRITTEN:
        assert _writers_of(root, widget) == [], widget


def test_the_two_smart_mix_levels_are_written_only_by_the_variant_lab_buttons():
    root = tree()
    for widget in SMART_MIX_VARIANT_WRITABLE:
        assert _writers_of(root, widget) == VARIANT_LAB_WRITERS, widget


def test_the_smart_mix_report_has_exactly_one_writer():
    assert _writers_of(tree(), SMART_MIX_REPORT_WIDGET) == ["process_btn.click"]


def test_no_smart_mix_widget_registers_a_handler_of_its_own():
    """Read at click time, exactly like every other audio control. No scan, no live validation."""
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release"}):
            owner = getattr(node.func.value, "id", None)
            assert owner not in SMART_MIX_CONFIG_WIDGETS + (SMART_MIX_REPORT_WIDGET,), owner


#: Events that have no legitimate business touching any Smart Mix widget: the source gate, the
#: preparation block, and the two creative-side handlers. **Not** the Variant Lab buttons — since
#: E2 V1 those genuinely carry two of these widgets, which is why they are asserted separately and
#: exactly rather than lumped in here.
SMART_MIX_FREE_EVENTS = ("source_mode", "source_folder", "source_recursive", "scan_btn",
                         "video_input", "confirm_btn", "prep_folder", "prep_recursive",
                         "prep_batch_size", "prep_scan_btn", "prep_analyze_btn",
                         "creative_preset", "randomize_btn")


def test_smart_mix_widgets_are_absent_from_source_and_preparation_wiring():
    """**Corrected in R1.** The isolation is kept and split truthfully; it was not removed.

    The R0 form looped over `generate_variant_btn` and `new_variant_btn` as well and asserted that
    *every* Smart Mix widget was absent from them. E2 V1 made that statement **false** for
    `sfx_amount` and `sfx_level`, which are deliberately inputs *and* outputs of both buttons — and
    it kept passing anyway, because `outputs=variant_lab_outputs` is a bare `ast.Name` and
    `names_in` saw only the list variable's own name. That is exactly the list-indirection vacuous
    pass this suite claims elsewhere to have closed, so the guard was asserting nothing about the
    two events it most needed to constrain.

    Split: the genuinely unrelated events keep the full absence rule — now under
    `expanded_names_in`, so no sub-list can hide a widget from them either — and the Variant Lab
    buttons get the exact positive/negative assertion below.
    """
    root = tree()
    everything = SMART_MIX_CONFIG_WIDGETS + (SMART_MIX_REPORT_WIDGET,)
    for assigned in ("source_outputs", "prep_outputs"):
        assignment = next((n for n in ast.walk(root) if isinstance(n, ast.Assign)
                           and getattr(n.targets[0], "id", None) == assigned), None)
        if assignment is None:
            continue
        for widget in everything:
            assert widget not in expanded_names_in(root, assignment.value), \
                f"{widget} in {assigned}"

    for button in SMART_MIX_FREE_EVENTS:
        for node in ast.walk(root):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and getattr(node.func.value, "id", None) == button):
                for kw in node.keywords:
                    if kw.arg in ("inputs", "outputs"):
                        for widget in everything:
                            assert widget not in expanded_names_in(root, kw.value), \
                                f"{button}.{kw.arg}"


def test_the_variant_lab_buttons_carry_exactly_the_two_smart_mix_levels():
    """R1's replacement for the half of the old guard that E2 made false — stated truthfully.

    `sfx_amount` and `sfx_level` must be **present** in both the `inputs` and the `outputs` of both
    Variant Lab buttons: they are live bases on the way in and resolved levels on the way out, and
    losing either direction would silently turn E2 into a no-op that still looked wired. The
    structural controls must stay **absent**: `sfx_folder` is resource identity, `sfx_roles` is
    structural intent, and `smart_mix_report` is render diagnostics — none is a value to vary.

    Everything is read through `expanded_names_in`, so both halves are measured against the widgets
    the event actually carries rather than against the name of the list it carries them in.
    """
    root = tree()
    for button in ("generate_variant_btn", "new_variant_btn"):
        for keyword in ("inputs", "outputs"):
            value = next((kw.value for node in ast.walk(root)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr == "click"
                          and getattr(node.func.value, "id", None) == button
                          for kw in node.keywords if kw.arg == keyword), None)
            assert value is not None, f"{button}.click has no {keyword}"
            present = set(expanded_names_in(root, value))

            for widget in SMART_MIX_VARIANT_WRITABLE:
                assert widget in present, f"{button}.{keyword} lost {widget}"
            for widget in SMART_MIX_NEVER_WRITTEN + (SMART_MIX_REPORT_WIDGET,):
                assert widget not in present, f"{widget} in {button}.{keyword}"

            # and the expansion is load-bearing rather than decorative: the widgets really are
            # reached through a named sub-list, so the non-expanding form sees none of them
            assert not set(SMART_MIX_VARIANT_WRITABLE) & set(names_in(value))


def test_smart_mix_is_absent_from_the_live_source_declaration():
    for node in ast.walk(tree()):
        if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("live_declaration"):
            rendered = ast.unparse(node)
            for widget in SMART_MIX_CONFIG_WIDGETS + (SMART_MIX_REPORT_WIDGET,):
                assert widget not in rendered, widget


def test_the_click_inputs_align_with_the_guard_parameters():
    """Gradio passes positionally, so a silent reordering would be invisible."""
    click = next(node for node in ast.walk(tree())
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and ast.unparse(node.func) == "process_btn.click")
    kwargs = {kw.arg: kw.value for kw in click.keywords}
    inputs = [getattr(n, "id", None) for n in kwargs["inputs"].elts]
    params = [a.arg for a in func("process_video_guarded").args.args]
    # audio_input -> audio_file is the one deliberate rename
    assert params[0] == "audio_file"
    assert inputs[1:] == params[1:]
    for widget in SMART_MIX_CONFIG_WIDGETS:
        assert widget in inputs
    assert SMART_MIX_REPORT_WIDGET not in inputs, "the report is an output only"


def test_the_guard_builds_one_normalised_config_after_the_gate():
    guarded = body_source("process_video_guarded")
    assert "fork_smart_mix.SmartMixConfig(" in guarded
    assert (guarded.index("resolve_for_render(")
            < guarded.index("fork_smart_mix.SmartMixConfig(")), (
        "the config must be built only after the gate has allowed the render")
    call = next(n for n in ast.walk(func("process_video_guarded"))
                if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "fork_smart_mix.SmartMixConfig")
    assert {kw.arg for kw in call.keywords} == {
        "enabled_roles", "amount", "sfx_level_percent"}


def test_the_sfx_root_stays_a_runtime_path_not_creative_state():
    source = ast.unparse(tree())
    for creative in ("CreativeProfile.from_widgets", "CreativeRecipe", "VariantLabConfig"):
        for node in ast.walk(tree()):
            if isinstance(node, ast.Call) and creative in ast.unparse(node.func):
                rendered = ast.unparse(node)
                for widget in SMART_MIX_CONFIG_WIDGETS:
                    assert widget not in rendered, f"{widget} reached {creative}"
    assert "sfx_root=sfx_folder" in source, "the root is threaded as a plain runtime argument"


def test_both_reports_are_cleared_before_the_gate_and_before_the_attempt():
    guarded = body_source("process_video_guarded")
    impl = body_source("_process_video_impl")
    for key in ("AUDIO_LAYERS_REPORT_KEY", "SMART_MIX_REPORT_KEY"):
        assert f"session_state[{key}] = ''" in guarded
        assert guarded.index(f"{key}] = ''") < guarded.index("resolve_for_render(")
        assert f"session_state[{key}] = ''" in impl
        assert impl.index(f"{key}] = ''") < impl.index("try:")


def test_the_smart_mix_preflight_runs_before_the_analysis():
    impl = body_source("_process_video_impl")
    assert impl.index("prepare_sfx_inputs(") < impl.index("analyze_beats_auto(")


def test_smart_mix_planning_runs_after_the_analysis():
    impl = body_source("_process_video_impl")
    assert impl.index("analyze_beats_auto(") < impl.index("fork_smart_mix.plan_sfx(")
    assert impl.index("fork_smart_mix.project_structure(") > impl.index("analyze_beats_auto(")


def test_the_analysis_never_sees_the_mixed_master_even_with_sfx():
    impl = func("_process_video_impl")
    call = next(n for n in ast.walk(impl) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "analyze_beats_auto")
    assert ast.unparse(call.args[0]) == "local_audio_path"
    rendered = ast.unparse(call)
    for forbidden in ("mixed_master_path", "render_audio_path", "sfx", "smart_mix"):
        assert forbidden not in rendered


def test_smart_mix_is_active_only_with_a_root_an_amount_and_a_role():
    impl = body_source("_process_video_impl")
    assert "smart_mix_active = bool(sfx_root and str(sfx_root).strip()) and smart_mix.plans_anything" in impl
    # and every SFX step is behind that flag
    for node in ast.walk(func("_process_video_impl")):
        if isinstance(node, ast.Call) and "prepare_sfx_inputs" in ast.unparse(node.func):
            break
    else:
        raise AssertionError("prepare_sfx_inputs is not called")
    guarded = [ast.unparse(n.test) for n in ast.walk(func("_process_video_impl"))
               if isinstance(n, ast.If)
               and "prepare_sfx_inputs" in ast.unparse(n)]
    assert "smart_mix_active" in guarded


def test_sfx_level_zero_does_not_disable_planning():
    """Level is a mute, not a switch — only the root, the Amount and the roles decide activity."""
    impl = body_source("_process_video_impl")
    activity = impl[impl.index("smart_mix_active ="):impl.index("smart_mix_active =") + 160]
    assert "sfx_level" not in activity
    assert "sfx_level_percent" not in activity


def test_the_success_panel_gains_a_smart_mix_line_only_when_sfx_were_used():
    impl = body_source("_process_video_impl")
    assert "if smart_mix_plan is not None and smart_mix_plan.total:" in impl
    assert "smart_mix_plan.summary_line()" in impl


def test_the_smart_mix_report_comes_from_the_plan_and_is_never_recomputed():
    impl = body_source("_process_video_impl")
    assert "'\\n'.join(smart_mix_plan.report_lines())" in impl
    for forbidden in ("_place_impacts", "_place_risers", "_Occupancy", "amount_params("):
        assert forbidden not in impl


def test_a_zero_placement_smart_mix_builds_no_pointless_master():
    impl = body_source("_process_video_impl")
    assert "if prepared_voices or sfx_placements:" in impl
    assert impl.count("audio_mixdown.build_mixed_master(") == 1


def test_the_smart_mix_report_never_reaches_the_pipeline():
    for callee in ("analyze_beats_auto", "create_music_video", "build_mixed_master",
                   "prepare_sfx_inputs", "plan_sfx"):
        for node in ast.walk(func("_process_video_impl")):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(callee):
                rendered = ast.unparse(node)
                assert "SMART_MIX_REPORT_KEY" not in rendered, callee
                assert "smart_mix_report" not in rendered, callee


def test_the_worker_thread_touches_no_smart_mix_component():
    worker = next(n for n in ast.walk(func("process_video"))
                  if isinstance(n, ast.FunctionDef) and n.name == "worker")
    rendered = ast.unparse(worker)
    assert "smart_mix_report" not in rendered
    assert "gr." not in rendered


def test_stage_five_and_creative_state_are_untouched_by_smart_mix():
    """Structural isolation: no SFX concept may reach Stage 5, the cache or the creative profile.

    `variant_lab.py` is deliberately absent since E2 V1 — it knows `sfx_amount` and
    `sfx_level_percent` on purpose. The narrower boundary for that one file is
    `test_variant_lab_knows_three_audio_values_and_nothing_else`, which still forbids every SFX
    *structural* concept (`sfx_folder`, `sfx_roles`, `SfxAsset`, `SfxPlacement`, `SmartMixConfig`).
    Stage 5, the cache, the worker and the creative objects keep the blanket rule unweakened.
    """
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    for relative in ("src/video_analysis.py", "src/auto_mode/stage5_qwen_scene_worker.py",
                     "src/beatsync_fork/library_prep.py", "src/beatsync_fork/creative.py",
                     "src/beatsync_fork/creative_recipe.py",
                     "src/beatsync_fork/presets.py", "src/beatsync_fork/variation.py"):
        with open(_os.path.join(repo, relative), "r", encoding="utf-8") as handle:
            text = handle.read()
        for token in ("smart_mix", "SmartMix", "sfx_", "SfxAsset", "SfxPlacement"):
            assert token not in text, f"{token} leaked into {relative}"


def test_the_cache_constants_are_unchanged():
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(repo, "src", "video_analysis.py"), "r", encoding="utf-8") as handle:
        text = handle.read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in text
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in text


def test_no_cli_flag_was_added():
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(repo, "src", "video_processor.py"), "r", encoding="utf-8") as handle:
        text = handle.read()
    for flag in ("--sfx", "--smart-mix", "--sfx-root", "--sfx-level", "--sfx-amount"):
        assert flag not in text


def test_the_gui_names_no_rng_domain_and_draws_nothing_itself():
    """**Renamed in R1**, because the old name — `…the_reserved_audio_rng_domain_stays_unused` —
    stopped being true when E2 V1 landed: the `audio` domain is now *used*, by `variant_lab.py`.

    The assertion was always the useful half and is kept verbatim: `gui.py` owns no RNG derivation
    and names no domain, so there is exactly one implementation of the audio draw and it is the
    stdlib-only testable one. Kept as an independent control beside
    `test_the_gui_delegates_audio_variation_and_implements_none_of_it`, which adds the positive
    half (the delegation call, and the resolver really consuming the domain).
    """
    import os as _os
    repo = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(repo, "src", "gui.py"), "r", encoding="utf-8") as handle:
        text = handle.read()
    assert 'rng_for(' not in text
    assert 'DOMAIN_AUDIO' not in text


# ===========================================================================
# SMART MIX V1 (E, R1): the GUI threads diagnostics, it never formats them
# ===========================================================================


def test_the_scan_diagnostics_are_threaded_into_the_planner():
    """R1: the scan's return value used to reach only `library_root`, so everything it had
    *ignored* was collected and then silently dropped."""
    impl = body_source("_process_video_impl")
    assert "sfx_assets, sfx_diagnostics = audio_mixdown.prepare_sfx_inputs(" in impl
    assert "library_diagnostics=sfx_diagnostics" in impl
    call = next(n for n in ast.walk(func("_process_video_impl"))
                if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "fork_smart_mix.plan_sfx")
    assert "library_diagnostics" in {kw.arg for kw in call.keywords}


def test_the_gui_invents_no_diagnostic_text_of_its_own():
    impl = body_source("_process_video_impl")
    for phrase in ("Ignored unknown role folders", "Ignored root-level files",
                   "Ignored unsupported files", "Files in disabled roles skipped",
                   "unknown_folders", "root_level_files", "unsupported_files",
                   "skipped_disabled_files"):
        assert phrase not in impl, f"gui.py formats {phrase!r} itself"
    assert "smart_mix_plan.report_lines()" in impl, "the plan is still the only source"


def test_the_report_still_has_exactly_one_writer_after_r1():
    """Smart Mix R1's own guard, kept as an independent control alongside the one above."""
    assert _writers_of(tree(), SMART_MIX_REPORT_WIDGET) == ["process_btn.click"]


def test_r1_added_no_gradio_event():
    """No scan button, no change handler, no upload handler, no preflight callback."""
    registrations = []
    for node in ast.walk(tree()):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"click", "change", "input", "submit", "release",
                                       "upload", "select"}):
            owner = getattr(node.func.value, "id", None)
            if owner:
                registrations.append(f"{owner}.{node.func.attr}")
    for widget in SMART_MIX_CONFIG_WIDGETS + (SMART_MIX_REPORT_WIDGET,):
        assert not any(r.startswith(widget + ".") for r in registrations), widget


def test_diagnostics_stay_out_of_every_other_data_model():
    """Report provenance only — never plan, creative or cache state."""
    impl = func("_process_video_impl")
    for callee in ("build_mixed_master", "analyze_beats_auto", "create_music_video",
                   "CreativeProfile.from_widgets"):
        for node in ast.walk(impl):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(callee):
                assert "sfx_diagnostics" not in ast.unparse(node), callee
    guarded = body_source("process_video_guarded")
    assert "sfx_diagnostics" not in guarded
