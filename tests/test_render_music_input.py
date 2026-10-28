"""[FORK] R8 Final Polish: the Render tab's music input -- compacted, and why it was not *also*
given a native Browse button.

The owner asked for two things here: make the music upload zone less tall, and check whether the
native file picker used by every path field elsewhere in the app could be offered here too,
"without requiring typed paths or duplicating large local files".

The first was done: the drop zone is bounded rather than claiming the default height for a
single-file control. The second was **built, measured and reverted**, and this file is where the
measurement lives so nobody rebuilds it.

MEASURED, 2026-10-28, Gradio 6.19.0, real running app:

    A `gr.Button` was wired to a `_browse_render_audio` helper -- the same self-contained contract
    as `_browse_folder` and `_browse_audio_source` -- returning the chosen path into the SAME
    `gr.File` component, so there would be no second source of truth for the track. The native
    dialog opened correctly and the operator's choice came back correctly. Gradio then refused to
    render it:

        gradio.exceptions.InvalidPathError: Cannot move <chosen path> to the gradio cache dir
        because it was not created by the application or it is not located in either the current
        working directory or your system's temp directory.

    (`gradio/processing_utils.py::_check_allowed`, reached from `postprocess_data`.) A `gr.File`
    will only *display* a path under its allowed roots. The component shows "Error" and the track
    is not set.

Three ways out, all rejected, and the reasons are not stylistic:

  1. Add the operator's music folder to `launch(allowed_paths=...)`. That makes Gradio serve that
     whole tree to any request the browser can make. The app deliberately does not do this -- see
     the long comment above `app.launch` in `gui.py`, which explains that R5 serves clip bytes
     through its own authorized middleware precisely to avoid widening `allowed_paths`.
  2. Copy the chosen file into a directory Gradio accepts. That is exactly the duplication of
     large local files the owner asked to avoid; it is what the browser upload already does.
  3. Replace `gr.File` with a path textbox. That is rewriting the render source pipeline, which
     the brief explicitly ruled out, and it would lose drag-and-drop.

So the Render tab keeps one way in -- the drop zone -- and that zone is now compact. The finding
is recorded rather than silently dropped, because "we tried the obvious thing and here is the
exact error" is the part that is expensive to rediscover.
"""

from __future__ import annotations

import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


def _declaration(source: str) -> str:
    start = source.index("audio_input = gr.File(")
    return source[start:source.index(")\n", start)]


def test_the_drop_zone_is_bounded(source: str):
    """The compaction itself. Measured on the running page: 113 px tall against a default of
    roughly 190, which is what moved Create Music Video back above the fold at 1440x900."""
    assert "height=" in _declaration(source)


def test_the_track_is_still_a_drop_zone(source: str):
    """Bounding the zone must not become replacing it. A `gr.File` IS the drop target, so a
    textbox here would silently remove drag-and-drop -- the only way to choose a track."""
    assert "audio_input = gr.File(" in source
    assert "audio_input = gr.Textbox(" not in source


def test_the_component_is_otherwise_untouched(source: str):
    """Everything a handler depends on is unchanged, so the compaction cannot have moved a value."""
    declaration = _declaration(source)
    assert "type='filepath'" in declaration
    assert "elem_id='audio-file-input'" in declaration
    assert "file_types=['.mp3', '.wav', '.flac']" in declaration


def test_no_native_picker_was_wired_to_the_track(source: str):
    """The reverted experiment, pinned. Rebuilding this does not fail at review time -- it fails
    at runtime, in the one component an operator must use before anything else works, with an
    error that only appears in the server log. See this module's docstring for the measurement.
    """
    for token in ("_browse_render_audio", "audio_input_browse"):
        assert token not in source, (
            f"{token} is back; a native path cannot be displayed by this gr.File -- "
            "read the measurement in tests/test_render_music_input.py before trying again")


def test_the_launch_call_still_widens_nothing(source: str):
    """The reason option (1) above was rejected, asserted directly so a later convenience fix
    cannot quietly take it. R5's authorized middleware exists because these parameters stay shut.

    Asserted over the parsed `launch` call rather than over raw text: `gui.py` *discusses* these
    parameter names in the comment explaining why it does not use them, and a substring check
    would be satisfied by that prose while a real keyword argument slipped past unnoticed.
    """
    import ast

    launches = [node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "launch"]
    assert launches, "no launch call found"
    for call in launches:
        for keyword in call.keywords:
            assert keyword.arg not in ("allowed_paths", "set_static_paths", "blocked_paths"), \
                f"launch() now passes {keyword.arg}"
