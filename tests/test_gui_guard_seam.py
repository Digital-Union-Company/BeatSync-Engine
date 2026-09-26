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


def test_gui_guarded_builds_a_live_declaration():
    """The handler must actually use the widgets it now receives."""
    source = _gui_source()
    start = source.index("def process_video_guarded")
    body = source[start : source.index("\ndef ", start + 1)]

    assert "live_declaration(" in body, "the handler still resolves the gate from state alone"
    for widget in ("source_mode", "source_folder", "source_recursive", "video_input"):
        assert widget in body, f"{widget} is accepted but never used"


@pytest.mark.parametrize("forbidden", ["def process_video(", "def _process_video_impl("])
def test_render_pipeline_entry_points_are_untouched(forbidden):
    """Sanity: this task must not have changed the pipeline functions themselves."""
    assert _gui_source().count(forbidden) == 1
