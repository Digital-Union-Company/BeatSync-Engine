"""The `gui.process_video_guarded` seam, without importing the runtime.

Why this is not an import of ``gui``
------------------------------------
``src/gui.py`` cannot be imported on a bare interpreter: its prologue runs
``logger.setup_environment()``, which imports librosa and mutates ``PATH``/``CUDA_PATH``, and then it
imports gradio, cupy and cv2. The fork suite must keep running anywhere (CLAUDE.md's hard rule), so
this file reconstructs the handler's *exact body* over the real gate and a stub ``process_video``.

What that buys, and what it does not
------------------------------------
It proves the property that matters — **no Stage 1 work can begin after a mismatch** — by making the
stand-in ``process_video`` fail loudly if it is ever reached on a denial. It does not prove that
``gui.py`` wires the right widgets into the click handler; that is asserted separately and cheaply by
reading the source in ``test_gui_click_inputs_include_live_source_controls``.
"""

from __future__ import annotations

import ast
import os

import pytest
from conftest import write_file

from beatsync_fork.input_confirmation import SourceMode
from beatsync_fork.input_session import (
    confirm_action,
    initial_state,
    live_declaration,
    resolve_for_render,
    scan_folder_action,
    set_browser_files,
    set_folder_path,
    set_mode,
)

_GUI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "gui.py"
)


class Stage1Started(AssertionError):
    """Raised by the stub pipeline. Reaching this on a denial is the bug the gate prevents."""


def _guarded(audio_file, source_mode, source_folder, source_recursive, video_input,
             output_filename, processing_mode, custom_fps, session_state, source_state,
             pipeline):
    """A faithful stand-in for ``gui.process_video_guarded``.

    Kept structurally identical to the handler: build the live declaration from the widget values,
    resolve the gate, and only then delegate. ``pipeline`` is the injected ``process_video``.
    """
    decision = resolve_for_render(
        source_state,
        live_declaration(source_mode, source_folder, source_recursive, video_input),
    )
    if not decision.allowed:
        yield None, f"❌ {decision.message}", session_state
        return

    yield from pipeline(
        audio_file=audio_file,
        video_files=list(decision.paths),
        output_filename=output_filename,
        processing_mode=processing_mode,
        custom_fps=custom_fps,
        session_state=session_state,
    )


def _exploding_pipeline(**kwargs):  # pragma: no cover - reaching this is the failure
    raise Stage1Started(
        "process_video() was called after the gate denied the render: "
        f"{len(kwargs.get('video_files') or [])} file(s) would have entered Stage 1"
    )
    yield  # noqa: W0101 - keeps this a generator function, matching process_video


def _recording_pipeline(seen):
    def pipeline(**kwargs):
        seen.append(kwargs)
        yield "preview.mp4", "done", kwargs["session_state"]

    return pipeline


def _drain(generator):
    return list(generator)


def _clips(tmp_path, folder, names):
    root = str(tmp_path / folder)
    return root, [write_file(os.path.join(root, n), b"clip-" + n.encode()) for n in names]


# ---------------------------------------------------------------------------
# Denials must not reach the pipeline
# ---------------------------------------------------------------------------


def test_stale_state_plus_changed_live_browser_list_cannot_call_process_video(tmp_path):
    """Stale ``gr.State`` + a browser list that grew => denial, and Stage 1 never starts."""
    _root, (a, b, c) = _clips(tmp_path, "seam_browser", ["a.mp4", "b.mp4", "c.mp4"])
    state = confirm_action(
        set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), [a, b])
    )
    assert state.is_confirmed()

    results = _drain(
        _guarded(
            audio_file="song.wav",
            source_mode=SourceMode.BROWSER_FILES.value,
            source_folder="",
            source_recursive=False,
            video_input=[a, b, c],  # the live widget, ahead of the state
            output_filename="out",
            processing_mode="fast",
            custom_fps=30.0,
            session_state={},
            source_state=state,
            pipeline=_exploding_pipeline,
        )
    )

    assert len(results) == 1
    preview, message, _session = results[0]
    assert preview is None
    assert "SOURCE INPUT CHANGED" in message


def test_stale_state_plus_changed_live_folder_controls_cannot_call_process_video(tmp_path):
    """Stale ``gr.State`` + a folder textbox pointing elsewhere => denial before any scan."""
    root_a, _ = _clips(tmp_path, "seam_folder_A", ["one.mp4"])
    root_b, _ = _clips(tmp_path, "seam_folder_B", ["two.mp4"])
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root_a)))
    assert state.is_confirmed()

    results = _drain(
        _guarded(
            audio_file="song.wav",
            source_mode=SourceMode.LOCAL_FOLDER.value,
            source_folder=root_b,  # the live widget, ahead of the state
            source_recursive=True,
            video_input=None,
            output_filename="out",
            processing_mode="fast",
            custom_fps=30.0,
            session_state={},
            source_state=state,
            pipeline=_exploding_pipeline,
        )
    )

    assert len(results) == 1
    assert "SOURCE INPUT CHANGED" in results[0][1]


def test_stale_state_plus_flipped_recursive_cannot_call_process_video(tmp_path):
    root, _ = _clips(tmp_path, "seam_recursive", ["top.mp4"])
    write_file(os.path.join(root, "sub", "nested.mp4"), b"nested")
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    results = _drain(
        _guarded("song.wav", SourceMode.LOCAL_FOLDER.value, root, False, None,
                 "out", "fast", 30.0, {}, state, _exploding_pipeline)
    )
    assert "SOURCE INPUT CHANGED" in results[0][1]


def test_new_unusable_supported_file_cannot_call_process_video(tmp_path):
    """Finding B through the seam: a 0-byte ``.mp4`` lands and Stage 1 must not start."""
    root, _ = _clips(tmp_path, "seam_scope", ["a.mp4"])
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))

    write_file(os.path.join(root, "arriving.mp4"), b"")

    results = _drain(
        _guarded("song.wav", SourceMode.LOCAL_FOLDER.value, root, True, None,
                 "out", "fast", 30.0, {}, state, _exploding_pipeline)
    )
    assert "SOURCE INPUT CHANGED" in results[0][1]


# ---------------------------------------------------------------------------
# The allowed path still delegates, with the freshly verified list
# ---------------------------------------------------------------------------


def test_allowed_render_passes_the_freshly_verified_list_to_process_video(tmp_path):
    root, paths = _clips(tmp_path, "seam_ok", ["a.mp4", "b.mp4"])
    state = confirm_action(scan_folder_action(set_folder_path(initial_state(), root)))
    seen: list[dict] = []

    results = _drain(
        _guarded("song.wav", SourceMode.LOCAL_FOLDER.value, root, True, None,
                 "out", "fast", 30.0, {"k": "v"}, state, _recording_pipeline(seen))
    )

    assert len(seen) == 1, "the pipeline must be called exactly once"
    assert seen[0]["video_files"] == list(state.confirmed.paths)
    assert set(seen[0]["video_files"]) == {os.path.abspath(p) for p in paths}
    assert isinstance(seen[0]["video_files"], list), "process_video's existing List[str] contract"
    assert seen[0]["session_state"] == {"k": "v"}
    assert results[0][0] == "preview.mp4"


def test_allowed_browser_render_uses_the_live_list(tmp_path):
    _root, (a, b) = _clips(tmp_path, "seam_ok_browser", ["a.mp4", "b.mp4"])
    state = confirm_action(
        set_browser_files(set_mode(initial_state(), SourceMode.BROWSER_FILES), [a, b])
    )
    seen: list[dict] = []

    _drain(
        _guarded("song.wav", SourceMode.BROWSER_FILES.value, "", False, [a, b],
                 "out", "fast", 30.0, {}, state, _recording_pipeline(seen))
    )

    assert seen[0]["video_files"] == [os.path.abspath(a), os.path.abspath(b)]


# ---------------------------------------------------------------------------
# gui.py wiring, asserted by reading the source (it cannot be imported here)
# ---------------------------------------------------------------------------


def _gui_source() -> str:
    with open(_GUI_PATH, "r", encoding="utf-8") as handle:
        return handle.read()


def test_gui_click_inputs_include_live_source_controls():
    """The click handler must receive every widget the gate needs to prove source identity.

    Without these in ``inputs``, the handler is back to trusting ``gr.State`` alone and the whole
    live-declaration check is unreachable in the real UI.
    """
    tree = ast.parse(_gui_source(), filename=_GUI_PATH)

    click_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "click"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "process_btn"
    ]
    assert len(click_calls) == 1, "expected exactly one process_btn.click registration"

    keywords = {kw.arg: kw.value for kw in click_calls[0].keywords}
    assert isinstance(keywords["fn"], ast.Name)
    assert keywords["fn"].id == "process_video_guarded"

    inputs = {node.id for node in ast.walk(keywords["inputs"]) if isinstance(node, ast.Name)}
    for required in (
        "audio_input",
        "source_mode",
        "source_folder",
        "source_recursive",
        "video_input",
        "output_filename",
        "processing_mode",
        "custom_fps",
        "session_state",
        "source_state",
    ):
        assert required in inputs, f"process_btn.click is missing the {required!r} input"


def test_gui_guarded_signature_matches_the_click_inputs():
    """Positional order must line up with ``inputs``; Gradio passes them positionally."""
    tree = ast.parse(_gui_source(), filename=_GUI_PATH)

    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "process_video_guarded"
    )
    parameters = [arg.arg for arg in handler.args.args]

    click_call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "click"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "process_btn"
    )
    inputs_node = next(kw.value for kw in click_call.keywords if kw.arg == "inputs")
    input_names = [node.id for node in inputs_node.elts if isinstance(node, ast.Name)]

    assert len(parameters) == len(input_names), (
        f"handler takes {parameters}, click supplies {input_names}"
    )
    # audio_input -> audio_file is the one deliberate rename; the rest must match by name.
    for parameter, widget in zip(parameters, input_names):
        if parameter == "audio_file" and widget == "audio_input":
            continue
        assert parameter == widget, f"positional mismatch: {parameter!r} vs {widget!r}"


def test_gui_guarded_accepts_the_live_source_widgets():
    """The public wrapper must still *receive* the live widgets it is handed.

    **Amended by C3-R0**: the wrapper now delegates the gate itself to one shared core, so the
    `live_declaration(...)` call moved. That half is asserted — more strongly, because it now
    covers both render paths — by `test_the_live_declaration_is_built_in_the_one_shared_gate_core`
    below. What stays here is the wrapper's own contract: it accepts the four live source widgets
    and forwards them, so `process_btn.click` cannot regress to state alone.
    """
    params = [a.arg for a in _gui_func("process_video_guarded").args.args]
    for widget in ("source_mode", "source_folder", "source_recursive", "video_input"):
        assert widget in params, f"{widget} is no longer accepted by the wrapper"
    body = _gui_body("process_video_guarded")
    for widget in ("source_mode", "source_folder", "source_recursive", "video_input"):
        assert widget in body, f"{widget} is accepted but never forwarded"


@pytest.mark.parametrize("forbidden", ["def process_video(", "def _process_video_impl("])
def test_render_pipeline_entry_points_are_untouched(forbidden):
    """Sanity: this task must not have changed the pipeline functions themselves."""
    assert _gui_source().count(forbidden) == 1


# ===========================================================================
# C3-R0: ONE gate core, TWO mutex-owning wrappers, one render at a time
# ===========================================================================

#: The shared live-source-gate + render core. C3-R0 moved the gate body out of
#: `process_video_guarded` so a second render entry point could reuse it rather than copy it;
#: both wrappers below call exactly this.
GATE_CORE = "_process_video_guarded_unlocked"


def _gui_tree() -> ast.Module:
    return ast.parse(_gui_source())


def _gui_func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_gui_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in gui.py")


def _gui_body(name: str) -> str:
    """One function's body with docstrings stripped — prose must not read as implementation."""
    node = _gui_func(name)
    return "\n".join(
        ast.unparse(stmt) for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str))
    )


def test_the_live_declaration_is_built_in_the_one_shared_gate_core():
    """**Re-pointed by C3-R0.** The property is unchanged; it moved into the shared core.

    This is strictly stronger than before: the assertion now covers *both* the single render and
    the two-candidate batch, because both reach the gate through this one function.
    """
    body = _gui_body(GATE_CORE)
    assert "live_declaration(" in body, "the core still resolves the gate from state alone"
    assert "resolve_for_render(" in body
    for widget in ("source_mode", "source_folder", "source_recursive", "video_input"):
        assert widget in body, f"{widget} is accepted but never used"


def test_there_is_exactly_one_source_gate_implementation():
    """The whole reason the core was extracted rather than the body copied."""
    whole = _gui_source()
    assert whole.count("resolve_for_render(") == 1, "a second source gate appeared"
    assert whole.count(f"def {GATE_CORE}(") == 1
    assert whole.count("def process_video_guarded(") == 1
    assert whole.count("def render_selected_variants_guarded(") == 1


def test_the_single_render_wrapper_takes_the_mutex_and_delegates():
    body = _gui_body("process_video_guarded")
    assert "_RENDER_LOCK.acquire(blocking=False)" in body
    assert "_RENDER_LOCK.release()" in body
    assert f"{GATE_CORE}(" in body
    # it is a wrapper, not a second implementation
    for duplicated in ("resolve_for_render(", "live_declaration(",
                       "fork_audio_mix.AudioMixConfig(", "fork_smart_mix.SmartMixConfig(",
                       "process_video("):
        assert duplicated not in body, f"the wrapper duplicates {duplicated}"


def test_the_batch_wrapper_takes_the_same_mutex_exactly_once_and_calls_the_core():
    """**Nested acquisition would be self-refusal**, not a deadlock — and that is worse.

    `_RENDER_LOCK` is a plain non-reentrant `Lock` acquired non-blockingly. If the batch called
    `process_video_guarded` while already holding it, the wrapper's own `acquire` would return
    `False` and every candidate would report "a render is already running" — a batch refusing
    itself. Hence: the batch calls the core.
    """
    body = _gui_body("render_selected_variants_guarded")
    assert body.count("_RENDER_LOCK.acquire(blocking=False)") == 1, "exactly one acquisition"
    assert body.count("_RENDER_LOCK.release()") == 1
    assert f"{GATE_CORE}(" in body
    assert "process_video_guarded(" not in body, "the batch must not re-enter the wrapper"


def test_the_batch_holds_the_lock_across_both_candidates():
    """Released in a `finally` after the candidate loop, never between candidates."""
    node = _gui_func("render_selected_variants_guarded")
    tries = [n for n in ast.walk(node) if isinstance(n, ast.Try) and n.finalbody]
    assert tries, "the batch must release its lock in a finally"
    holder = next(t for t in tries
                  if "_RENDER_LOCK.release()" in ast.unparse(t.finalbody))
    loops = [n for n in ast.walk(holder) if isinstance(n, ast.For)]
    assert loops, "the candidate loop must sit INSIDE the lock-holding try"
    assert f"{GATE_CORE}(" in ast.unparse(loops[0]), "candidates render inside the held lock"
    # and nothing releases it mid-loop
    assert "_RENDER_LOCK.release()" not in ast.unparse(loops[0])


def test_the_lock_is_a_plain_non_reentrant_lock():
    """An `RLock` would hide the nested-acquisition bug instead of refusing it."""
    source = _gui_source()
    assert "_RENDER_LOCK = threading.Lock()" in source
    for forbidden in ("threading.RLock(", "threading.Semaphore(", "threading.BoundedSemaphore("):
        assert forbidden not in source, forbidden


def test_only_the_two_wrappers_reach_the_core():
    callers = []
    for node in ast.walk(_gui_tree()):
        if isinstance(node, ast.FunctionDef) and node.name != GATE_CORE:
            body = "\n".join(ast.unparse(stmt) for stmt in node.body)
            if f"{GATE_CORE}(" in body:
                callers.append(node.name)
    assert sorted(callers) == ["process_video_guarded",
                               "render_selected_variants_guarded"], callers


def test_a_busy_renderer_refuses_without_touching_anything():
    """Both wrappers must refuse cleanly while the lock is held elsewhere."""
    import threading as _threading
    gui_lock = _threading.Lock()
    assert gui_lock.acquire(blocking=False)
    try:
        # the structural property: a non-blocking acquire returns False, and the refusal branch
        # yields `gr.skip()` for the video so the previous preview survives
        wrapper = _gui_body("process_video_guarded")
        assert "if not _RENDER_LOCK.acquire(blocking=False):" in wrapper
        assert "RENDER_BUSY_MESSAGE" in wrapper
        assert "gr.skip()" in wrapper
        batch = _gui_body("render_selected_variants_guarded")
        assert "if not _RENDER_LOCK.acquire(blocking=False):" in batch
        assert "RENDER_BUSY_MESSAGE" in batch
    finally:
        gui_lock.release()


def test_the_busy_refusal_writes_no_state_and_no_report():
    """A rejected click must cost the user nothing that was already on screen."""
    node = _gui_func("process_video_guarded")
    refusal = next(n for n in ast.walk(node) if isinstance(n, ast.If)
                   and "acquire" in ast.unparse(n.test))
    rendered = ast.unparse(refusal)
    for forbidden in ("AUDIO_LAYERS_REPORT_KEY", "SMART_MIX_REPORT_KEY",
                      "resolve_for_render", "source_state ="):
        assert forbidden not in rendered, f"the busy refusal touches {forbidden}"


def test_both_render_events_share_one_concurrency_group():
    """Separate Gradio listeners get separate lanes unless grouped — and two overlapping renders
    would each wipe the other's process-global processing directory."""
    tree = _gui_tree()
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"):
            owner = getattr(node.func.value, "id", None)
            if owner in ("process_btn", "render_selected_variants_btn"):
                kwargs = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
                found[owner] = kwargs
    assert set(found) == {"process_btn", "render_selected_variants_btn"}
    for owner, kwargs in found.items():
        assert kwargs.get("concurrency_id") == "RENDER_CONCURRENCY_ID", owner
        assert kwargs.get("concurrency_limit") == "1", owner
    # one shared constant, one value
    assert _gui_source().count("RENDER_CONCURRENCY_ID = ") == 1


def test_no_third_render_event_exists_outside_the_group():
    """Any future render entry point must join the group deliberately, not by accident."""
    tree = _gui_tree()
    renderers = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"):
            fn = next((kw.value for kw in node.keywords if kw.arg == "fn"), None)
            if fn is not None and ast.unparse(fn) in (
                    "process_video_guarded", "render_selected_variants_guarded", GATE_CORE,
                    "process_video", "_process_video_impl"):
                renderers.append((getattr(node.func.value, "id", "?"), ast.unparse(fn)))
    assert sorted(renderers) == [
        ("process_btn", "process_video_guarded"),
        ("render_selected_variants_btn", "render_selected_variants_guarded"),
    ], renderers


def test_the_batch_only_no_overwrite_flag_cannot_arrive_positionally():
    """`refuse_existing_output` is keyword-only on the core, so the ordinary widget list — which
    Gradio supplies positionally — can never set it. Single render keeps today's behaviour."""
    core = _gui_func(GATE_CORE)
    assert [a.arg for a in core.args.kwonlyargs] == ["refuse_existing_output"]
    default = core.args.kw_defaults[0]
    assert isinstance(default, ast.Constant) and default.value is False

    wrapper_params = [a.arg for a in _gui_func("process_video_guarded").args.args]
    assert "refuse_existing_output" not in wrapper_params
    assert "refuse_existing_output" not in _gui_body("process_video_guarded"), \
        "the single render must not opt in"
    assert "refuse_existing_output=True" in _gui_body("render_selected_variants_guarded")


def test_the_durable_output_key_is_cleared_before_the_gate_and_set_after_the_move():
    """The batch reads this, so a refused render must not leave the previous path behind."""
    core = _gui_body(GATE_CORE)
    impl = _gui_body("_process_video_impl")
    assert "session_state[LAST_OUTPUT_PATH_KEY] = ''" in core
    assert core.index("LAST_OUTPUT_PATH_KEY] = ''") < core.index("resolve_for_render(")
    assert "session_state[LAST_OUTPUT_PATH_KEY] = output_path" in impl
    assert impl.index("shutil.move(result_path, output_path)") < \
        impl.index("session_state[LAST_OUTPUT_PATH_KEY] = output_path")
    # and it is never the preview
    assert "LAST_OUTPUT_PATH_KEY] = preview_path" not in impl


def test_the_batch_reads_candidate_values_from_recipes_not_from_the_screen():
    """Two candidates are never both on screen, so widgets cannot be the batch's authority."""
    node = _gui_func("render_selected_variants_guarded")
    params = [a.arg for a in node.args.args]
    for widget in ("variation_seed", "cut_density", "micro_cuts", "semantic_emphasis",
                   "energy_response", "motion_bias", "source_diversity",
                   "music_under_voice", "sfx_amount", "sfx_level"):
        assert widget not in params, f"{widget} must come from the stored recipe, not the screen"
    body = _gui_body("render_selected_variants_guarded")
    for field in ("recipe.seed", "recipe.cut_density", "recipe.micro_cuts",
                  "recipe.semantic_emphasis", "recipe.energy_response", "recipe.motion_bias",
                  "recipe.source_diversity", "audio.music_under_voice_percent",
                  "audio.sfx_amount", "audio.sfx_level_percent"):
        assert field in body, f"{field} is not read from the candidate"


def test_the_batch_renders_nothing_of_its_own_and_adds_no_cancellation():
    body = _gui_body("render_selected_variants_guarded")
    for forbidden in ("create_music_video", "analyze_beats_auto", "_process_video_impl(",
                      "cancels", "threading.Event", "stop_flag", "terminate("):
        assert forbidden not in body, f"the batch references {forbidden}"


def test_the_batch_only_no_overwrite_is_checked_twice_and_never_overwrites():
    """**§38 load-bearing.** `shutil.move` silently overwrites an existing destination (measured),
    and a batch's two candidates can compute the same name — C3 guarantees unique candidate
    *masters* but not unique `CreativeRecipe.seed`, and the render path names its file
    `_seed<VariationSeed>`. So a batch must refuse rather than destroy what it just produced.

    Two checks, deliberately: one before Stage 1 so a doomed candidate costs nothing, and one
    immediately before the move because a render takes minutes and the file can appear meanwhile.
    """
    impl = _gui_body("_process_video_impl")
    guard = "if refuse_existing_output and os.path.exists(output_path):"
    assert impl.count(guard) == 2, "both collision checks must be present and gated by the flag"

    # the first is before any analysis; the second is immediately before the move
    first = impl.index(guard)
    second = impl.index(guard, first + 1)
    analysis = impl.index("analyze_beats_auto(")
    move = impl.index("shutil.move(result_path, output_path)")
    assert first < analysis, "refuse before Stage 1, not after paying for it"
    assert analysis < second < move, "re-check immediately before the move"

    # Neither branch deletes or replaces anything. Scoped to the `If` node rather than a text
    # window: the second check sits immediately before `shutil.move`, so any window wide enough
    # to hold the branch also catches the move that legitimately follows it.
    refusals = [n for n in ast.walk(_gui_func("_process_video_impl"))
                if isinstance(n, ast.If)
                and ast.unparse(n.test) == "refuse_existing_output and os.path.exists(output_path)"]
    assert len(refusals) == 2
    for node in refusals:
        branch = chr(10).join(ast.unparse(stmt) for stmt in node.body)
        assert "return (None," in branch, "a collision must refuse, not continue"
        for destructive in ("os.remove(", "shutil.rmtree(", "os.unlink(", "shutil.move("):
            assert destructive not in branch, f"the refusal {destructive}"
        assert not node.orelse, "no silent fallback path"

    # and the ordinary single render is NOT opted in
    core = _gui_func(GATE_CORE)
    assert core.args.kw_defaults[0].value is False
    assert "refuse_existing_output" not in _gui_body("process_video_guarded")


def test_the_batch_stops_on_the_first_failed_candidate():
    """**§46-F.** Fail fast: the render boundary exposes no typed failure classification, so a
    batch cannot tell a shared-input failure — which would simply repeat — from a candidate-local
    one. A failure therefore stops the batch, and nothing already produced is deleted.
    """
    node = _gui_func("render_selected_variants_guarded")
    loop = next(n for n in ast.walk(node) if isinstance(n, ast.For))

    failure = next(n for n in ast.walk(loop) if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "not durable")
    rendered = ast.unparse(failure)
    assert "break" in rendered, "a failed candidate must stop the batch"
    assert "stopped = True" in rendered, "and the outcome must say so"

    # success is decided by the durable file, never by the preview or the status prose
    body = _gui_body("render_selected_variants_guarded")
    assert "durable = (session_state or {}).get(LAST_OUTPUT_PATH_KEY, '') or ''" in body
    assert "success=bool(durable)" in body
    for forbidden in ("success=bool(last_video)", "success=bool(last_status)",
                      "'error' in last_status", "status.startswith"):
        assert forbidden not in body, f"success inferred from {forbidden}"

    # nothing deletes an earlier candidate's output
    for destructive in ("os.remove(", "shutil.rmtree(", "os.unlink("):
        assert destructive not in body, f"the batch {destructive}"
