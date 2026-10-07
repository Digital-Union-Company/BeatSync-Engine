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
import errno
import os
import sys

import pytest
from conftest import write_file

import beatsync_fork.render_worker as fork_render_worker
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


def test_the_ai_director_added_no_render_event():
    """**AI Director V1.** Two new button events, and neither is a render.

    Asserted positively rather than by omission: the Director's registrations exist, their `fn=`
    is a Director handler, and neither carries a `concurrency_id` — because joining the render
    group would claim it competes for the renderer, which it must not.
    """
    tree = _gui_tree()
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"):
            owner = getattr(node.func.value, "id", None)
            if owner in ("generate_director_btn", "apply_director_btn"):
                found[owner] = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}

    assert set(found) == {"generate_director_btn", "apply_director_btn"}, found
    assert found["generate_director_btn"]["fn"] == "_on_generate_director_proposal"
    assert found["apply_director_btn"]["fn"] == "_on_apply_director_proposal"
    for owner, kwargs in found.items():
        assert "concurrency_id" not in kwargs, owner
        assert "every" not in kwargs, owner


def test_no_director_handler_can_reach_the_render_chain_or_the_gate():
    """`DIRECTOR_AUTO_RENDER = NO`, walked from both buttons rather than banned by token.

    The forbidden set is deliberately wider than the renderer: the shared gate core,
    `resolve_for_render`, `live_declaration` and the durable promotion are all in it, because a
    Director that could reach the *gate* would be able to invalidate or approve a source set.

    **References, not just calls**, and that is a correction rather than a flourish: a mutation
    proof showed `_ = create_music_video` reaching the renderer while appearing in no `ast.Call`
    node, so a call-only walker reported a clean boundary on a leaking handler. Attribute leaves
    count too, so a module alias is no hiding place.
    """
    tree = _gui_tree()
    defined = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    forbidden = {"process_video_guarded", GATE_CORE, "process_video", "_process_video_impl",
                 "analyze_beats_auto", "create_music_video",
                 "render_selected_variants_guarded", PROMOTION_HELPER,
                 "resolve_for_render", "live_declaration", "confirm_action",
                 "scan_folder_action", "build_mixed_master"}

    for entry in ("_on_generate_director_proposal", "_on_apply_director_proposal"):
        assert entry in defined, entry
        referenced, seen, pending = set(), set(), [entry]
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            node = defined.get(name)
            if node is None:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name):
                    leaf = inner.id
                elif isinstance(inner, ast.Attribute):
                    leaf = inner.attr
                elif isinstance(inner, ast.Call):
                    leaf = ast.unparse(inner.func).rsplit(".", 1)[-1]
                else:
                    continue
                referenced.add(leaf)
                if leaf in defined:
                    pending.append(leaf)
        leaked = sorted(referenced & forbidden)
        assert not leaked, f"{entry} reaches {leaked}"
        assert len(seen) > 1, f"{entry} resolved no call graph at all"


def test_the_director_never_touches_the_render_mutex_or_the_processing_dir():
    """One render at a time is a property of the two render wrappers. A proposal is not a render,
    so it must neither take the mutex nor clear the process-global processing directory."""
    for name in ("_on_generate_director_proposal", "_on_apply_director_proposal",
                 "_run_director_model", "_director_apply_outputs", "_director_apply_skips"):
        body = _gui_body(name)
        for forbidden in ("_RENDER_LOCK", "RENDER_CONCURRENCY_ID", "RENDER_BUSY_MESSAGE",
                          "get_processing_dir", "LAST_OUTPUT_PATH_KEY", "session_state",
                          "source_state", "prep_state"):
            assert forbidden not in body, f"{name} references {forbidden}"


def test_the_render_wrappers_never_learned_about_the_director():
    """The other direction: no render or pipeline handler writes a Director surface or reads one.

    Including the two report panels — the Director owns its own read-outs, so no render handler may
    describe a proposal and no proposal may describe a render.
    """
    for name in RENDER_CHAIN:
        node = _gui_func(name)
        params = [a.arg for a in node.args.args]
        body = _gui_body(name)
        for word in ("director_proposal_state", "director_proposal", "director_status",
                     "director_instruction", "fork_director", "DirectorProposal",
                     "DIRECTOR_LLAMA_EXE", "DIRECTOR_LLAMA_DIR", "DIRECTOR_MODEL"):
            assert word not in params, f"{name} takes {word}"
            assert word not in body, f"{name} references {word}"


def test_the_director_runner_is_the_only_new_subprocess_in_the_gui():
    """A bounded one-shot, and the only one the Director owns. Pinned so a second invocation —
    a warm-up call, a version probe, a retry — cannot arrive unreviewed."""
    tree = _gui_tree()
    runs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "subprocess.run":
            kwargs = {kw.arg for kw in node.keywords}
            runs.append(kwargs)
    # the pre-existing ProRes preview call plus the Director's one invocation
    assert len(runs) == 2, f"{len(runs)} subprocess.run call sites in gui.py"

    director = _gui_body("_run_director_model")
    assert director.count("subprocess.run(") == 1
    assert "timeout=fork_director.TIMEOUT_SECONDS" in director
    assert "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)" in director
    assert "capture_output=True" in director
    for forbidden in ("Popen", "shell=True", "check=True"):
        assert forbidden not in director, f"the Director runner uses {forbidden}"


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


def test_the_durable_output_key_is_cleared_before_the_gate_and_set_after_promotion():
    """The batch reads this, so a refused render must not leave the previous path behind.

    **Re-pointed by H1**: the authority is no longer `shutil.move` but the atomic no-replace
    promotion helper. The lifecycle property is unchanged and now covers the single render too.
    """
    core = _gui_body(GATE_CORE)
    impl = _gui_body("_process_video_impl")
    assert "session_state[LAST_OUTPUT_PATH_KEY] = ''" in core
    assert core.index("LAST_OUTPUT_PATH_KEY] = ''") < core.index("resolve_for_render(")

    # H1: the pipeline function clears it for itself too, so "empty unless a promotion succeeded"
    # is a local property rather than one inherited from whoever called it. The core's clear still
    # has to exist: a gate refusal never reaches `_process_video_impl` at all.
    assert "session_state[LAST_OUTPUT_PATH_KEY] = ''" in impl
    assert impl.index("LAST_OUTPUT_PATH_KEY] = ''") < impl.index("os.path.exists(output_path)")

    assert "session_state[LAST_OUTPUT_PATH_KEY] = output_path" in impl
    assert impl.index(f"{PROMOTION_HELPER}(result_path, output_path)") < \
        impl.index("session_state[LAST_OUTPUT_PATH_KEY] = output_path")
    # and it is never the preview
    assert "LAST_OUTPUT_PATH_KEY] = preview_path" not in impl

    # exactly one assignment of a real path, and it is after the promotion
    assigns = [ast.unparse(n) for n in ast.walk(_gui_func("_process_video_impl"))
               if isinstance(n, ast.Assign)
               and ast.unparse(n.targets[0]) == "session_state[LAST_OUTPUT_PATH_KEY]"]
    assert assigns == ["session_state[LAST_OUTPUT_PATH_KEY] = ''",
                       "session_state[LAST_OUTPUT_PATH_KEY] = output_path"], assigns


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


def test_the_batch_renders_nothing_and_implements_no_cancellation_of_its_own():
    """**Amended by C3-R1A**, and widened rather than relaxed.

    The batch still renders nothing itself — it reaches the pipeline only through the shared gate
    core. R1A makes it *cancellable*, which it expresses solely by holding one `RenderLifecycle` and
    reading it; it must still implement no cancellation mechanism. So `cancels` (Gradio's built-in,
    which would kill the event and orphan the daemon worker), a raw `threading.Event`, a stop flag
    and `terminate(`/`kill(` all stay banned, and `Popen`/`signal` are added — the batch wrapper is
    not where a process gets stopped.
    """
    body = _gui_body("render_selected_variants_guarded")
    for forbidden in ("create_music_video", "analyze_beats_auto", "_process_video_impl(",
                      "cancels", "threading.Event", "threading.Lock", "stop_flag",
                      "terminate(", "kill(", "Popen", "signal.", "os.kill"):
        assert forbidden not in body, f"the batch references {forbidden}"
    # It holds and reads a lifecycle; it never builds a second one per candidate.
    assert body.count("RenderLifecycle(") == 1, "exactly one lifecycle for the whole batch"


def test_the_batch_stops_on_the_first_failed_or_cancelled_candidate():
    """**§46-F, extended by C3-R1A.** Fail fast — and stop fast.

    C3-R0's reasoning stands unchanged for *failures*: the render boundary exposes no typed
    classification that could tell a shared-input failure (which would simply repeat) from a
    candidate-local one, so a failure stops the batch and nothing already produced is deleted.
    Continue-after-failure is still C3-R1B.

    C3-R1A adds exactly one more way to stop, and the guard is widened by precisely that one:
    a candidate that was CANCELLED also stops the batch. It is pinned as a **typed** cause read
    from `RENDER_OUTCOME_KEY`, never inferred from `durable` or from the status prose — that
    distinction is the whole point, because a cancelled candidate is *also* not durable, and
    reporting it as an ordinary failure would tell a user who pressed Stop that their render broke.
    """
    node = _gui_func("render_selected_variants_guarded")
    loop = next(n for n in ast.walk(node) if isinstance(n, ast.For))

    failure = next(n for n in ast.walk(loop) if isinstance(n, ast.If)
                   and ast.unparse(n.test) == "candidate_cancelled or not durable")
    rendered = ast.unparse(failure)
    assert "break" in rendered, "a failed or cancelled candidate must stop the batch"
    assert "stopped = True" in rendered, "and the outcome must say so"

    # success is decided by the durable file, never by the preview or the status prose
    body = _gui_body("render_selected_variants_guarded")
    assert "durable = (session_state or {}).get(LAST_OUTPUT_PATH_KEY, '') or ''" in body
    assert "success=bool(durable)" in body
    for forbidden in ("success=bool(last_video)", "success=bool(last_status)",
                      "'error' in last_status", "status.startswith"):
        assert forbidden not in body, f"success inferred from {forbidden}"

    # [C3-R1A] the cancelled cause is typed, and read from the one key that carries it.
    # [C3-R1B-a] and the loop now preserves the FULL class rather than only CANCELLED: the class is
    # read once off RENDER_OUTCOME_KEY and threaded into the outcome, so CANDIDATE_LOCAL,
    # SHARED_FATAL and UNKNOWN_FATAL survive instead of collapsing to None.
    flat = body.replace("\n", "").replace("    ", "")
    assert "candidate_kind = (session_state or {}).get(RENDER_OUTCOME_KEY)" in flat, body
    assert "candidate_cancelled = candidate_kind is RenderOutcomeKind.CANCELLED" in flat, body
    # [C3-R1B-a / R2] the explicit class must reach the model UNCONDITIONALLY. Exactly this form --
    # not "outcome_kind=candidate_kind if ...", which is how R1 quietly substituted `None` on a
    # success/kind disagreement and let the conservative derivation publish a different class than
    # the producer named. `RenderCandidateOutcome.__post_init__` owns agreement validation and must
    # be allowed to raise.
    assert "outcome_kind=candidate_kind)" in flat, body
    # the R1A shape must be GONE -- it is what discarded every class except CANCELLED
    assert "outcome_kind=RenderOutcomeKind.CANCELLED if candidate_cancelled else None" not in flat, \
        "the batch reverted to preserving only CANCELLED and discarding the other typed classes"
    # and no filter may conditionally discard an explicit class on its way to the model
    for laundering in ("outcome_kind=candidate_kind if", "outcome_kind=(candidate_kind if",
                       "agrees", "if candidate_kind is not None else",
                       "candidate_kind or None"):
        assert laundering not in flat, \
            f"an explicit outcome class is conditionally discarded via {laundering!r} -- " \
            f"that bypasses RenderCandidateOutcome.__post_init__"
    for inferred in ("'Cancelled' in last_status", "'⏹' in last_status",
                     "last_status.startswith", "cancelled = not durable"):
        assert inferred not in body, f"cancellation inferred from {inferred}"

    # nothing deletes an earlier candidate's output
    for destructive in ("os.remove(", "shutil.rmtree(", "os.unlink("):
        assert destructive not in body, f"the batch {destructive}"


# ===========================================================================
# H1: one universal GUI output policy — atomic no-replace durable promotion
# ===========================================================================
#
# OLD   ordinary Create Music Video  ->  `shutil.move`, destructive, measured
#       C3-R0 batch                  ->  two `os.path.exists` checks around the same move
# NEW   every GUI render             ->  one atomic no-replace `os.rename`
#
# The two `exists()` checks were a TOCTOU pair: real protection against this app's own second
# candidate, best-effort only against another process. H1 does not extend them — it replaces the
# promotion primitive, so the check and the act become one operation, and it removes the opt-in
# flag because no GUI caller ever wanted destructive replacement.

#: The single GUI-owned durable promotion. Nothing else may move a render into `output/`.
PROMOTION_HELPER = "_promote_output_no_replace"

#: Every function in the GUI render chain. No overwrite-policy parameter may appear in any of them.
RENDER_CHAIN = ("_process_video_impl", "process_video", GATE_CORE,
                "process_video_guarded", "render_selected_variants_guarded")


def test_no_overwrite_policy_flag_exists_anywhere_in_the_gui_render_chain():
    """**H1 load-bearing.** Safety is not opt-in, so there is nothing to opt into.

    C3-R0's `refuse_existing_output` defaulted to `False`, which is precisely how the ordinary
    single render kept a destructive promotion while the batch was safe. With an atomic no-replace
    primitive, overwriting is *impossible* rather than *disabled* — a boolean that re-enabled it
    would have no implementation to switch to, and a flag defaulting to the unsafe value is a trap
    for the next caller. Pin the absence, not a default.
    """
    for name in RENDER_CHAIN:
        node = _gui_func(name)
        assert not node.args.kwonlyargs, \
            f"{name} grew a keyword-only argument; H1 removed the only one that existed"
        params = [a.arg for a in node.args.args]
        for suspicious in ("refuse_existing_output", "overwrite", "allow_overwrite",
                           "force", "replace_existing", "no_overwrite"):
            assert suspicious not in params, f"{name} took an overwrite-policy parameter"
        # Docstring-stripped: the core's prose names the retired flag deliberately, to say it is
        # gone and must not come back. Prose must not read as implementation.
        assert "refuse_existing_output" not in _gui_body(name), \
            f"{name} still threads the retired policy flag"

    # and no call anywhere in the module passes it on
    for call in ast.walk(_gui_tree()):
        if isinstance(call, ast.Call):
            assert "refuse_existing_output" not in [kw.arg for kw in call.keywords], \
                f"{ast.unparse(call.func)} still passes the retired policy flag"


def test_the_exact_output_path_is_checked_before_any_analysis():
    """Refusing before Stage 1 costs nothing; refusing after Stage 5 wastes the whole run.

    The check is unconditional — both wrappers reach it through the one shared core — and it must
    sit after the final path is computed but before `analyze_beats_auto`.
    """
    impl = _gui_body("_process_video_impl")
    guard = "if os.path.exists(output_path):"
    assert guard in impl, "the universal early collision check is missing"

    naming = impl.index("output_path = os.path.join(output_folder, filename)")
    early = impl.index(guard)
    analysis = impl.index("analyze_beats_auto(")
    render = impl.index("create_music_video(")
    assert naming < early < analysis < render, \
        "check the exact final path, before Stage 1, before the renderer"

    node = next(n for n in ast.walk(_gui_func("_process_video_impl"))
                if isinstance(n, ast.If) and ast.unparse(n.test) == "os.path.exists(output_path)")
    branch = chr(10).join(ast.unparse(stmt) for stmt in node.body)
    assert "return (None," in branch, "a collision must refuse, not continue"
    assert not node.orelse, "no silent fallback path"
    for destructive in ("os.remove(", "shutil.rmtree(", "os.unlink(", "shutil.move(",
                        "os.replace(", "os.rename("):
        assert destructive not in branch, f"the early refusal {destructive}"


def test_the_final_promotion_is_one_owned_no_replace_helper():
    """One helper owns durable promotion, and the destructive primitives are gone from gui.py."""
    impl = _gui_body("_process_video_impl")
    # [FORK] Digital-Union (C3-R1B-a): the helper returns (message, outcome_kind), so the call site
    # unpacks a pair. Still exactly ONE call site and ONE definition, asserted below.
    assert (f"promotion_error, promotion_kind = {PROMOTION_HELPER}"
            f"(result_path, output_path)") in impl

    # exactly one promotion call site, and exactly one helper definition
    source = _gui_source()
    assert source.count(f"def {PROMOTION_HELPER}(") == 1
    assert sum(1 for n in ast.walk(_gui_tree())
               if isinstance(n, ast.Call) and getattr(n.func, "id", None) == PROMOTION_HELPER) == 1

    # the old destructive promotion is gone from the module entirely
    assert "shutil.move(" not in source, "shutil.move silently replaces; it must not return"
    assert "os.replace(" not in source, "os.replace silently replaces"

    # nothing else renames into the output folder
    renames = [ast.unparse(n) for n in ast.walk(_gui_tree())
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.rename"]
    assert renames == ["os.rename(temp_output, output_path)"], renames

    # a promotion failure returns a failure; it never falls through into the success path
    failure = next(n for n in ast.walk(_gui_func("_process_video_impl"))
                   if isinstance(n, ast.If) and ast.unparse(n.test) == "promotion_error")
    assert "return (None, promotion_error, session_state)" in ast.unparse(failure)
    assert not failure.orelse


def test_the_promotion_helper_removes_nothing_and_has_no_copy_fallback():
    """**Fail closed.** No destination is ever deleted to make room, and a cross-volume
    destination is refused rather than copied: a copy is not atomic, and an interrupted one would
    leave a partial video at the final path, which reads as a finished render."""
    body = _gui_body(PROMOTION_HELPER)
    for forbidden in ("os.remove(", "os.unlink(", "shutil.rmtree(", "shutil.move(",
                      "shutil.copy", "shutil.copyfile", "os.replace(", "open(",
                      "subprocess"):
        assert forbidden not in body, f"the promotion helper uses {forbidden}"
    calls = [ast.unparse(n.func) for n in ast.walk(_gui_func(PROMOTION_HELPER))
             if isinstance(n, ast.Call)]
    assert "os.rename" in calls, "the helper must promote with the no-replace primitive"
    assert "os.path.exists" not in calls, \
        "a pre-check inside the helper would re-open the race the rename closes"


def test_the_promotion_is_not_preceded_by_an_exists_check():
    """The rename IS the authority. `exists()` then `rename()` would be the old TOCTOU pair with
    a new primitive — correct-looking and still racy."""
    body = _gui_body("_process_video_impl")
    between = body[body.index("create_music_video("):body.index("promotion_error, promotion_kind")]
    assert "os.path.exists(output_path)" not in between, \
        "a second exists() check reappeared immediately before the promotion"
    # exactly one exists() check against the final path survives: the early one
    assert body.count("os.path.exists(output_path)") == 1


def test_prores_preview_is_generated_only_after_a_successful_promotion():
    """A ProRes render's durable artifact is the `.mov`; the `_preview.mp4` is a session temp.

    Order is the contract: promote, record the durable path, *then* build the preview. A refused
    or failed promotion must produce no preview at all — there is no durable file to preview, and
    the one already on disk belongs to a render the user owns.
    """
    impl = _gui_body("_process_video_impl")
    promotion = impl.index(f"{PROMOTION_HELPER}(result_path, output_path)")
    refusal = impl.index("return (None, promotion_error, session_state)")
    durable = impl.index("session_state[LAST_OUTPUT_PATH_KEY] = output_path")
    preview_branch = impl.index("if is_prores:")
    preview_run = impl.index("subprocess.run(preview_cmd")
    assert promotion < refusal < durable < preview_branch < preview_run

    # and the preview filename semantics are untouched
    assert "preview_filename = f'{name}_{timestamp}_preview.mp4'" in impl
    assert "preview_path = os.path.join(session_dir, preview_filename)" in impl


def test_the_ordinary_filename_contract_is_unchanged():
    """H1 changes *where a file may land*, never *what it is called*. No auto-rename, no counter.

    Scoped to the statements that actually build the name — a substring sweep over the whole
    function matches innocent things like `time.perf_counter()`.
    """
    impl = _gui_body("_process_video_impl")
    assert "filename = f'{name}_{timestamp}{creative.filename_suffix()}{ext}'" in impl
    assert "ext = '.mov' if is_prores else '.mp4'" in impl

    node = _gui_func("_process_video_impl")
    naming = [ast.unparse(n) for n in ast.walk(node)
              if isinstance(n, ast.Assign)
              and ast.unparse(n.targets[0]) in ("filename", "ext", "output_path", "timestamp")]
    assert naming == [
        "ext = '.mov' if is_prores else '.mp4'",
        "timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')",
        "filename = f'{name}_{timestamp}{creative.filename_suffix()}{ext}'",
        "output_path = os.path.join(output_folder, filename)",
    ], naming
    for invented in ("uuid", "itertools.count", "counter", "random", "_2", "_3"):
        assert not any(invented in stmt for stmt in naming), f"the output name gained {invented}"

    # and nothing searches for a free name
    assert not [n for n in ast.walk(node) if isinstance(n, ast.While)], \
        "a retry loop around the output name is auto-rename by another name"


# ---------------------------------------------------------------------------
# The REAL promotion helper, executed against controlled OS behaviour
# ---------------------------------------------------------------------------
#
# Portability (CLAUDE.md's hard rule, and §20 of the H1 authorization): Windows `os.rename` refuses
# an existing destination, POSIX `rename(2)` replaces it silently. Asserting the real platform's
# semantics unconditionally would make this suite Windows-only. So these cases stub `os.rename` and
# prove the helper's *handling* of each outcome; the real Windows primitive is measured separately
# at the end, behind a skip.


class _OsShim:
    """The real `os`, with `rename` replaced. Everything else passes straight through."""

    def __init__(self, rename):
        self.rename = rename

    def __getattr__(self, name):
        return getattr(os, name)


def _load_promotion_helper(rename):
    """Execute the REAL `_promote_output_no_replace` body from `gui.py` over a controlled `os`.

    [FORK] Digital-Union (C3-R1B-a): the helper now names `RenderOutcomeKind`, so the synthesised
    namespace supplies the REAL enum from the fork module -- never a stub, so these cases pin the
    actual members the GUI will store.
    """
    node = _gui_func(PROMOTION_HELPER)
    namespace = {"os": _OsShim(rename), "errno": errno,
                 "RenderOutcomeKind": fork_render_worker.RenderOutcomeKind}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<gui>", "exec"), namespace)
    return namespace[PROMOTION_HELPER]


def _no_replace_rename(src, dst):
    """A portable stand-in for the Windows no-replace rename the app depends on.

    Itself a check-then-act, which is exactly why it is only ever a *test* stand-in: what these
    cases verify is the helper's handling of each outcome, not the primitive's atomicity. The real
    primitive is measured on Windows below, and in task scratch during acceptance.
    """
    if os.path.exists(dst):
        raise FileExistsError(errno.EEXIST, "File exists")
    os.rename(src, dst)


def _exdev_rename(src, dst):
    raise OSError(errno.EXDEV, "Invalid cross-device link")


def test_the_helper_promotes_and_reports_success_when_the_destination_is_free(tmp_path):
    promote = _load_promotion_helper(_no_replace_rename)
    temp = write_file(str(tmp_path / "session" / "clip.mp4"), b"NEW")
    dest = str(tmp_path / "out" / "clip.mp4")
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    # [FORK] Digital-Union (C3-R1B-a): success is ('', None) -- no prose AND no failure class, so a
    # successful promotion can never contribute an outcome kind of its own. SUCCESS is still
    # written by the caller, after this returns.
    assert promote(temp, dest) == ("", None), "success is empty message + no kind, never prose"
    assert open(dest, "rb").read() == b"NEW"
    assert not os.path.exists(temp), "a successful promotion consumes the temp"


def test_the_helper_refuses_a_collision_and_preserves_both_files(tmp_path):
    promote = _load_promotion_helper(_no_replace_rename)
    temp = write_file(str(tmp_path / "session" / "clip.mp4"), b"NEW")
    dest = write_file(str(tmp_path / "out" / "clip.mp4"), b"OLD")

    message, kind = promote(temp, dest)
    assert message, "a collision must report failure"
    assert open(dest, "rb").read() == b"OLD", "the existing durable output was replaced"
    assert open(temp, "rb").read() == b"NEW", "the new render was discarded"
    assert dest in message and temp in message, \
        "the message must name both the occupied destination and the retained render"
    assert "preserved" in message.lower()
    # [FORK] Digital-Union (C3-R1B-a): the destination name carries the candidate index and master,
    # and `RenderBatchRequest.__post_init__` asserts stems are distinct, so no other selected
    # candidate can compute this path -- the collision is candidate-local.
    assert kind is fork_render_worker.RenderOutcomeKind.CANDIDATE_LOCAL


def test_the_helper_fails_closed_on_a_cross_volume_destination(tmp_path):
    """**No copy fallback.** EXDEV is a refusal, not a slower route to the same place."""
    promote = _load_promotion_helper(_exdev_rename)
    temp = write_file(str(tmp_path / "session" / "clip.mp4"), b"NEW")
    dest = write_file(str(tmp_path / "out" / "clip.mp4"), b"OLD")

    message, kind = promote(temp, dest)
    assert message
    assert open(dest, "rb").read() == b"OLD", "the destination was touched"
    assert open(temp, "rb").read() == b"NEW", "the temp render must be retained"
    assert temp in message and dest in message
    assert "volume" in message.lower()
    # [FORK] Digital-Union (C3-R1B-a): `session_dir` and `get_output_dir()` are process-global, so
    # every remaining candidate promotes between the identical volume pair and fails identically.
    # This is one of SHARED_FATAL's real producers.
    assert kind is fork_render_worker.RenderOutcomeKind.SHARED_FATAL


def test_the_helper_fails_closed_on_any_other_os_error(tmp_path):
    """A permission error is still a failure, never an apparent success."""
    def denied(src, dst):
        raise PermissionError(errno.EACCES, "Access is denied")

    promote = _load_promotion_helper(denied)
    temp = write_file(str(tmp_path / "session" / "clip.mp4"), b"NEW")
    dest = write_file(str(tmp_path / "out" / "clip.mp4"), b"OLD")

    message, kind = promote(temp, dest)
    assert message
    assert open(dest, "rb").read() == b"OLD"
    assert open(temp, "rb").read() == b"NEW"
    assert temp in message
    # [FORK] Digital-Union (C3-R1B-a): a permission or I/O error proves nothing about the other
    # candidates, so it fails closed rather than claiming either specific class.
    assert kind is fork_render_worker.RenderOutcomeKind.UNKNOWN_FATAL


@pytest.mark.skipif(sys.platform != "win32",
                    reason="no-replace rename is a Windows guarantee; POSIX rename(2) replaces")
def test_the_real_windows_rename_refuses_an_existing_destination(tmp_path):
    """The platform primitive the whole guarantee rests on, measured rather than assumed.

    This is the one case that cannot be portable: it asserts that *this* OS refuses. It skips
    cleanly elsewhere, so the bare-CPython suite contract survives.
    """
    promote = _load_promotion_helper(os.rename)

    free_temp = write_file(str(tmp_path / "s" / "a.mp4"), b"NEW")
    free_dest = str(tmp_path / "o" / "a.mp4")
    os.makedirs(os.path.dirname(free_dest), exist_ok=True)
    assert promote(free_temp, free_dest) == ("", None)
    assert open(free_dest, "rb").read() == b"NEW"
    assert not os.path.exists(free_temp)

    occupied_temp = write_file(str(tmp_path / "s" / "b.mp4"), b"NEW")
    occupied_dest = write_file(str(tmp_path / "o" / "b.mp4"), b"OLD")
    message, kind = promote(occupied_temp, occupied_dest)
    assert message, "real Windows os.rename did not refuse an existing destination"
    assert open(occupied_dest, "rb").read() == b"OLD"
    assert open(occupied_temp, "rb").read() == b"NEW"
    # [FORK] Digital-Union (C3-R1B-a): the REAL platform FileExistsError, not a stand-in, classified
    # CANDIDATE_LOCAL -- so the one Windows-only case pins the typed cause too.
    assert kind is fork_render_worker.RenderOutcomeKind.CANDIDATE_LOCAL


# ---------------------------------------------------------------------------
# The REAL `_process_video_impl`, executed end to end over a scratch filesystem
# ---------------------------------------------------------------------------
#
# The structural tests above pin where the check and the promotion sit. These prove what actually
# happens to real bytes on disk — collision before the render, collision that appears *during* the
# render, a clean promotion, and a cross-volume failure — by AST-extracting the real function and
# executing it against a synthesised module namespace. Nothing about the collision policy is
# reimplemented here; only the 30-odd runtime globals `gui.py` would have supplied are stubbed, and
# `analyze_beats_auto` / `create_music_video` are instrumented so "no expensive work began" is a
# measured call count rather than an inference.


class _StubAudioMixdown:
    AudioMixError = type("AudioMixError", (Exception,), {})
    # [FORK] Digital-Union (C3-R1B-a): the extracted body names these in `except` clauses. None of
    # the cases below reaches them, so Python would never resolve the names -- which is exactly how
    # the C3-R1A harness nearly shipped a latent NameError. Completed deliberately rather than left
    # to luck, mirroring `test_audio_mixdown.py`'s own note on the same hazard.
    AudioProbeError = type("AudioProbeError", (AudioMixError,), {})
    AudioMixInputError = type("AudioMixInputError", (AudioMixError,), {})
    AudioMixPlanError = type("AudioMixPlanError", (AudioMixError,), {})
    AudioMixExecutionError = type("AudioMixExecutionError", (AudioMixError,), {})

    @staticmethod
    def discard_master(path):
        return None


def _impl_namespace(tmp_path, calls, *, rename, dest_appears_during_render):
    """The real module globals `_process_video_impl` closes over, stubbed at the runtime edge."""
    import beatsync_fork.audio_mix as fork_audio_mix
    import beatsync_fork.creative as fork_creative
    import beatsync_fork.render_worker as fork_render_worker
    import beatsync_fork.smart_mix as fork_smart_mix

    out_dir = str(tmp_path / "output")
    os.makedirs(out_dir, exist_ok=True)

    def analyze_beats_auto(*args, **kwargs):
        calls.append("analyze_beats_auto")
        return [0.0, 1.0, 2.0], {
            "times": [0.0, 1.0, 2.0], "audio_duration": 2.0, "tempo": 120.0,
            "selection_info": [], "video_analysis": None, "sections": None,
        }

    def create_music_video(*args, output_file, **kwargs):
        calls.append("create_music_video")
        with open(output_file, "wb") as handle:
            handle.write(b"NEW")
        if dest_appears_during_render:
            # Another process (or a second candidate of this batch) takes the destination while
            # this render is in flight — the whole reason the promotion must decide atomically.
            with open(os.path.join(out_dir, os.path.basename(output_file)), "wb") as handle:
                handle.write(b"OLD")
        return output_file

    return {
        "os": _OsShim(rename), "errno": errno,
        "datetime": __import__("datetime"), "time": __import__("time"),
        "tempfile": __import__("tempfile"), "subprocess": __import__("subprocess"),
        "fork_creative": fork_creative, "fork_audio_mix": fork_audio_mix,
        "fork_smart_mix": fork_smart_mix, "audio_mixdown": _StubAudioMixdown,
        "analyze_beats_auto": analyze_beats_auto, "create_music_video": create_music_video,
        "get_output_dir": lambda: out_dir,
        "get_video_fps": lambda path: 30.0,
        "get_success_message_auto": lambda *a, **k: "✅ rendered",
        "_scale_diagnostics_block": lambda *a, **k: "",
        "_stage5_summary": lambda *a, **k: None,
        "_stage6_summary": lambda *a, **k: None,
        "_stage_status": lambda stage: f"Stage {stage}",
        "set_gpu_mode": lambda enabled: None,
        "GRADIO_TEMP_DIR": str(tmp_path / "gradio_uploads"),
        "AUDIO_LAYERS_REPORT_KEY": "audio_layers_report",
        "SMART_MIX_REPORT_KEY": "smart_mix_report",
        "LAST_OUTPUT_PATH_KEY": "last_output_path",
        # [C3-R1A] The REAL cancellation contract, not a stub: `render_worker` is a stdlib-only fork
        # module and imports cleanly on a bare interpreter, so the extracted body gets the genuine
        # enum and the genuine exception type. A stub here would let `RENDER_OUTCOME_KEY` drift.
        "RENDER_OUTCOME_KEY": "render_outcome_kind",
        "RenderOutcomeKind": fork_render_worker.RenderOutcomeKind,
        "RenderCancelled": fork_render_worker.RenderCancelled,
        "PARALLEL_WORKERS": 1, "CPU_COUNT": 1, "MAX_THREADS": 1,
        "GPU_AVAILABLE": False, "NVENC_AVAILABLE": False, "GPU_INFO": {"name": "cpu"},
        "USING_PORTABLE_PYTHON": False, "USING_PORTABLE_CUDA": False, "USING_CUPY_CTK": False,
        "FFMPEG_PATH": "ffmpeg",
    }


def _run_real_impl(tmp_path, *, existing_dest=None, rename=None,
                   dest_appears_during_render=False):
    """Run the REAL `_process_video_impl` over scratch files; return its result plus a call log."""
    calls = []
    namespace = _impl_namespace(tmp_path, calls, rename=rename or _no_replace_rename,
                                dest_appears_during_render=dest_appears_during_render)
    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    assert len(nodes) == 4, [n.name for n in nodes]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)

    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    out_dir = str(tmp_path / "output")
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")

    if existing_dest is not None:
        # The exact name the impl will compute, so this is a real collision rather than a guess.
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        write_file(os.path.join(out_dir, f"music_video_{stamp}.mp4"), existing_dest)

    state = {"session_dir": session_dir}
    result = namespace["_process_video_impl"](
        audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
        processing_mode="cpu", custom_fps=30.0, creative=None, session_state=state,
    )
    produced = sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []
    return result, calls, produced, session_dir, out_dir


def test_an_existing_output_is_refused_before_any_expensive_work(tmp_path):
    """**§23.** The destination is occupied, so Stage 1 must never start and OLD must survive."""
    (preview, status, state), calls, produced, _session, out_dir = _run_real_impl(
        tmp_path, existing_dest=b"OLD")

    assert calls == [], f"expensive work began despite an occupied destination: {calls}"
    assert preview is None
    assert "already exists" in status and "preserved" in status
    assert len(produced) == 1, produced
    assert open(os.path.join(out_dir, produced[0]), "rb").read() == b"OLD", \
        "the user's existing output was replaced"
    assert state["last_output_path"] == "", "a refusal must not record a durable output"
    # [C3-R1A] and it must not record a SUCCESS either.
    # [C3-R1B-a] R1A left this `None`, which derived to UNKNOWN_FATAL downstream. The cause IS
    # provable: `output_path` is built from this candidate's own stem (request tag + `_cNN_m<master>`)
    # and `RenderBatchRequest.__post_init__` asserts the stems in a batch are distinct, so no other
    # selected candidate can compute this name. CANDIDATE_LOCAL is stricter than `None`.
    assert state["render_outcome_kind"] is fork_render_worker.RenderOutcomeKind.CANDIDATE_LOCAL
    assert state["render_outcome_kind"] is not fork_render_worker.RenderOutcomeKind.SUCCESS


def test_a_destination_that_appears_during_the_render_is_not_overwritten(tmp_path):
    """**§23 / §13.** Free before Stage 1, occupied by promotion time: preserve OLD, keep NEW."""
    (preview, status, state), calls, produced, session_dir, out_dir = _run_real_impl(
        tmp_path, dest_appears_during_render=True)

    assert calls == ["analyze_beats_auto", "create_music_video"], calls
    assert preview is None
    assert len(produced) == 1, produced
    destination = os.path.join(out_dir, produced[0])
    assert open(destination, "rb").read() == b"OLD", "the file that appeared was overwritten"

    retained = [f for f in os.listdir(session_dir) if f.endswith(".mp4")]
    assert len(retained) == 1, f"the new render was not retained: {os.listdir(session_dir)}"
    assert open(os.path.join(session_dir, retained[0]), "rb").read() == b"NEW"

    assert destination in status and os.path.join(session_dir, retained[0]) in status, \
        "the failure must name both the occupied destination and the retained render"
    assert state["last_output_path"] == ""
    # [C3-R1A] a promotion failure is a proven fatal — never SUCCESS.
    # [C3-R1B-a] and the three promotion branches no longer share one conservative class. A
    # destination that appeared DURING the render is a `FileExistsError` on a candidate-unique
    # name, so it is CANDIDATE_LOCAL. Stricter than the previous UNKNOWN_FATAL.
    import beatsync_fork.render_worker as _rw
    assert state["render_outcome_kind"] is _rw.RenderOutcomeKind.CANDIDATE_LOCAL
    assert state["render_outcome_kind"] is not _rw.RenderOutcomeKind.SUCCESS


def test_a_clean_promotion_moves_the_render_and_records_it(tmp_path):
    """The ordinary success path still works, and the temp is consumed rather than duplicated."""
    (preview, status, state), calls, produced, session_dir, out_dir = _run_real_impl(tmp_path)

    assert calls == ["analyze_beats_auto", "create_music_video"]
    assert len(produced) == 1, produced
    destination = os.path.join(out_dir, produced[0])
    assert open(destination, "rb").read() == b"NEW"
    assert not [f for f in os.listdir(session_dir) if f.endswith(".mp4")], \
        "a successful promotion must consume the temp, not copy it"
    assert state["last_output_path"] == destination
    # [C3-R1A] the durable promotion IS the success commit point, and it is the only producer of
    # SUCCESS anywhere on the render path.
    import beatsync_fork.render_worker as _rw
    assert state["render_outcome_kind"] is _rw.RenderOutcomeKind.SUCCESS
    assert preview == destination
    assert status.startswith("✅")
    # the shipped filename contract, unchanged
    assert produced[0].startswith("music_video_") and produced[0].endswith(".mp4")
    assert "_seed" not in produced[0], "seed 0 keeps today's name exactly"


def test_a_cross_volume_destination_fails_closed_without_copying(tmp_path):
    """**§11.** No copy fallback: an interrupted copy would leave a partial video at the final
    path, which looks like a finished render. Refuse, retain, report."""
    (preview, status, state), calls, produced, session_dir, _out = _run_real_impl(
        tmp_path, rename=_exdev_rename)

    assert calls == ["analyze_beats_auto", "create_music_video"]
    assert preview is None
    assert produced == [], f"a copy fallback wrote to the output folder: {produced}"
    retained = [f for f in os.listdir(session_dir) if f.endswith(".mp4")]
    assert len(retained) == 1 and \
        open(os.path.join(session_dir, retained[0]), "rb").read() == b"NEW"
    assert "volume" in status.lower()
    assert state["last_output_path"] == ""
    # [C3-R1B-a] EXDEV is SHARED_FATAL: `session_dir` and `get_output_dir()` are process-global, so
    # every remaining candidate promotes between the identical volume pair. Stricter than the
    # previous UNKNOWN_FATAL, and one of SHARED_FATAL's real producers.
    import beatsync_fork.render_worker as _rw
    assert state["render_outcome_kind"] is _rw.RenderOutcomeKind.SHARED_FATAL
    assert state["render_outcome_kind"] is not _rw.RenderOutcomeKind.SUCCESS


def test_a_variation_seed_still_names_its_own_file(tmp_path):
    """Sanity that the collision work did not disturb the Phase A suffix contract."""
    import beatsync_fork.creative as fork_creative

    calls = []
    namespace = _impl_namespace(tmp_path, calls, rename=_no_replace_rename,
                                dest_appears_during_render=False)
    nodes = [n for n in _gui_tree().body
             if isinstance(n, ast.FunctionDef)
             and n.name in {"_as_existing_source_path", "_as_existing_source_paths",
                            PROMOTION_HELPER, "_process_video_impl"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<gui>", "exec"), namespace)

    session_dir = str(tmp_path / "session")
    os.makedirs(session_dir, exist_ok=True)
    audio = write_file(str(tmp_path / "src" / "track.wav"), b"audio")
    clip = write_file(str(tmp_path / "src" / "a.mp4"), b"clip")
    profile = fork_creative.CreativeProfile.from_widgets(
        seed=381944, cut_density=50, energy_response=50, motion_bias=50,
        source_diversity=50, micro_cuts=50, semantic_emphasis=50)

    namespace["_process_video_impl"](
        audio_file=audio, video_files=[clip], output_filename="music_video.mp4",
        processing_mode="cpu", custom_fps=30.0, creative=profile,
        session_state={"session_dir": session_dir})

    produced = os.listdir(str(tmp_path / "output"))
    assert len(produced) == 1 and produced[0].endswith("_seed381944.mp4"), produced
