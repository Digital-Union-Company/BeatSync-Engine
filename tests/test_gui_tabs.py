"""The three-tab layout (GUI Refresh A), and the properties the re-layout had to preserve.

`gui.py` cannot be imported on a bare interpreter, so everything here is `ast` over real source —
the same technique as the other GUI seam suites.

The refresh was a **pure re-parenting**: every component definition and every event registration
moved verbatim inside new `with` blocks. So the interesting assertions are mostly about what did
*not* change, and the one genuinely new seam is the active-settings read-out. In particular this
suite exists to catch the two ways a cosmetic re-layout can quietly break the app:

* a widget **duplicated** instead of moved, giving one execution setting two sources of truth;
* a widget moved into a tab while its event registration still names the old object, or a handler's
  positional `inputs` list silently reordered.

The second class is already covered in depth by `test_gui_guard_seam.py`,
`test_creative_controls_seam.py`, `test_audio_layers_seam.py` and `test_variant_lab.py`, which pin
the positional alignments name-for-name. This file adds the layout-level invariants those suites
have no reason to know about.
"""

from __future__ import annotations

import ast
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")


@pytest.fixture(scope="module")
def source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def tree(source: str) -> ast.Module:
    return ast.parse(source)


def _strip_docstrings(node):
    """A deep copy with every docstring removed, so prose can neither satisfy nor fail a check."""
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


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only(node) -> str:
    """Executable source with docstrings removed and string literals blanked.

    The same helper, and the same reasoning, as `test_freestyle.py` and `test_freestyle_random.py`
    carry: the bans below are about *machinery*. The read-out's own docstring legitimately contains
    `DEFAULT_CONTROL` and `creative_preset`, in the sentences explaining why neither is used any
    more — and a ban that the explanation of the ban cannot survive forces documentation to be
    written around a test. Scanning the raw source instead is a mistake this repository has now
    made five times.
    """
    return ast.unparse(_BlankStrings().visit(_strip_docstrings(node)))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def _with_items(tree: ast.Module) -> list[str]:
    rendered = []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                rendered.append(ast.unparse(item.context_expr))
    return rendered


def _registrations(tree: ast.Module, suffix: str) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func).endswith(suffix)]


# ---------------------------------------------------------------------------------------------
# The containers themselves
# ---------------------------------------------------------------------------------------------

#: Amended by Raw Cutter V1: a FOURTH top-level tab, placed third. It is a tool the operator
#: opens the app specifically to use -- curating footage before any of the render workflow applies
#: -- so it sits beside the other workflows rather than inside Advanced Tools, which is exactly
#: the discoverability problem the three-tab refresh existed to fix.
#:
#: Amended by R5 P1: a FIFTH top-level tab, the Clips Preview Selector, placed LAST. Last is
#: deliberate and keeps two existing contracts intact: Advanced Tools stays at index 3, so every
#: containment assertion below needs no index change, and the new tab's markup stays OUT of the
#: `source[cutter:advanced]` block that
#: `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans for `gr.Accordion`.
_TABS = ("LABEL_TAB_QUICK_RENDER", "LABEL_TAB_CREATIVE_STUDIO", "LABEL_TAB_RAW_CUTTER",
         "LABEL_TAB_ADVANCED", "LABEL_TAB_CLIPS_PREVIEW")


def test_there_are_exactly_five_tabs(tree: ast.Module):
    items = [i for i in _with_items(tree) if i.startswith("gr.Tab(")]
    assert len(items) == 5, f"expected five tabs, found {len(items)}: {items}"
    assert len([i for i in _with_items(tree) if i.startswith("gr.Tabs(")]) == 1


def test_the_tabs_are_the_documented_five_in_order(source: str):
    positions = [source.index(f"gr.Tab({label}") for label in _TABS]
    assert positions == sorted(positions), (
        "Quick Render, then Creative Studio, then Raw Cutter, then Advanced Tools, then Clips "
        "Preview — the order is load-bearing for several containment assertions in other suites")


def test_the_raw_cutter_is_a_top_level_tab_not_an_accordion(source: str, tree: ast.Module):
    """The whole placement decision, pinned. Burying the cutter inside Advanced Tools would
    reproduce the problem the tab refresh fixed, so it must be a `gr.Tab` of its own and its
    widgets must not end up inside any accordion."""
    assert "gr.Tab(LABEL_TAB_RAW_CUTTER" in source
    cutter = source.index("gr.Tab(LABEL_TAB_RAW_CUTTER")
    advanced = source.index("gr.Tab(LABEL_TAB_ADVANCED")
    assert cutter < advanced
    block = source[cutter:advanced]
    assert "gr.Accordion" not in block, "the Raw Cutter must not hide its controls in an accordion"
    for widget in ("cut_source", "cut_output", "cut_sensitivity", "cut_min_seconds",
                   "cut_max_seconds", "cut_remove_audio", "cut_rename_output",
                   "cut_analyze_btn", "cut_run_btn",
                   "cut_cancel_btn", "cut_summary", "cut_table", "cut_report"):
        assert f"{widget} = gr." in block, f"{widget} is not in the Raw Cutter tab"


def test_the_clips_preview_tab_is_last_and_top_level(source: str, tree: ast.Module):
    """[FORK] R5 P1. Two separate claims: it is a `gr.Tab` of its own rather than something
    buried in an accordion, and it is the LAST one.

    Last is not cosmetic. Inserting it between Raw Cutter and Advanced Tools would move Advanced
    to index 4 -- breaking `test_the_occasional_tools_are_on_the_advanced_tab` and
    `test_library_prep_is_the_last_block_in_the_advanced_tab` -- and would pull this tab's markup
    into the span `test_the_raw_cutter_is_a_top_level_tab_not_an_accordion` scans for
    `gr.Accordion`.
    """
    assert "gr.Tab(LABEL_TAB_CLIPS_PREVIEW" in source
    clips = source.index("gr.Tab(LABEL_TAB_CLIPS_PREVIEW")
    for earlier in _TABS[:-1]:
        assert source.index(f"gr.Tab({earlier}") < clips, f"{earlier} must come before it"
    block = source[clips:source.index("source_outputs = [", clips)]
    assert "gr.Accordion" not in block, "the Clips Preview tab must not hide its controls"
    for widget in ("clips_input", "clips_recursive", "clips_library", "clips_rejected",
                   "clips_page_size", "clips_check_btn", "clips_scan_btn", "clips_refresh_btn",
                   "clips_report", "clips_counts", "clips_gallery", "clips_confirm_btn",
                   "clips_confirm_out"):
        assert f"{widget} = gr." in block, f"{widget} is not in the Clips Preview tab"


def test_the_clips_preview_tab_uses_no_slider(source: str):
    """Measured in R5-P0, and the reason page size is a `gr.Number`.

    `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui` slices
    `source[INFO_PREP_SAMPLE : sample_btn.click(]` and forbids `gr.Slider` in it. That span runs
    past `source_outputs = [` into the event wiring, so a tab placed last falls inside it -- a
    slider here would fail a test protecting the approved sample resource policy. The test is
    right; the widget is a Number.
    """
    clips = source.index("gr.Tab(LABEL_TAB_CLIPS_PREVIEW")
    block = source[clips:source.index("source_outputs = [", clips)]
    assert "gr.Slider" not in block
    assert "clips_page_size = gr.Number(" in block


def test_the_clips_tab_writes_no_render_or_source_widget(source: str):
    """Isolation, asserted on the event wiring: reviewing clips must not be able to clear a
    confirmed source set, change what a render would do, or disturb a cutter job."""
    start = source.index("clips_check_btn.click(")
    end = source.index("app.load(", start)
    block = source[start:end]
    for forbidden in ("status_output", "video_output", "process_btn", "source_state",
                      "source_outputs", "prep_outputs", "session_state", "cut_run_btn",
                      "cut_report", "render_invocation_state", "variation_seed",
                      "creative_preset", "_RENDER_LOCK", "RENDER_CONCURRENCY_ID"):
        assert forbidden not in block, f"a Clips Preview event writes or reads {forbidden}"
    assert block.count(".click(") == 4, "expected exactly four Clips Preview registrations"


def test_the_clips_handlers_take_an_injected_request_not_a_widget(tree: ast.Module, source: str):
    """The owner of a review session is the server-side session hash. It must arrive through
    `gr.Request` injection -- a real parameter with a default so the handler stays callable --
    and must never be listed in a click's `inputs`, because a browser-supplied owner would let
    one session name another's pages.

    Asserted against the parsed signature rather than a formatted string: the earlier version of
    this test matched source text and broke on a line wrap, which proved nothing about the code.
    """
    for name in ("_on_clips_scan", "_on_clips_refresh", "_on_clips_confirm"):
        func = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == name), None)
        assert func is not None, f"{name} is missing"
        args = [a.arg for a in func.args.args]
        assert args[-1] == "request", f"{name} does not take an injected request last"
        annotation = ast.unparse(func.args.args[-1].annotation or ast.Constant(None))
        assert "gr.Request" in annotation, f"{name}'s request is not a gr.Request"
        assert func.args.defaults, f"{name}'s request has no default, so it is not optional"

    # And no click passes it in.
    start = source.index("clips_scan_btn.click(")
    block = source[start:source.index("outputs=", start)]
    assert "request" not in block, "gr.Request must not be listed in inputs"


def test_every_tab_label_is_defined_in_ui_content(source: str):
    with open(_UI_CONTENT, encoding="utf-8") as handle:
        content = handle.read()
    for label in _TABS:
        assert f"{label} = " in content, f"{label} is not defined in ui_content.py"


def test_the_render_column_split_is_three_to_two(source: str):
    """The task asked for roughly 3:2. The controls need the width; the preview needs enough to be
    watchable. Asserted so a later tidy-up cannot silently collapse it back to 1:1.
    """
    quick = source.index("gr.Tab(LABEL_TAB_QUICK_RENDER")
    studio = source.index("gr.Tab(LABEL_TAB_CREATIVE_STUDIO")
    block = source[quick:studio]
    assert "gr.Column(scale=3)" in block
    assert "gr.Column(scale=2)" in block


def test_the_quick_render_controls_are_two_side_by_side_sub_columns(tree: ast.Module):
    """Measured, not cosmetic. Stacked in one column this content ran to 1980 px and put Create
    Music Video at y=1714 — still below the fold, which defeats the point of a Quick Render tab.
    The audio picker and the Video Source group alone are 877 px of that, so no amount of
    tightening the settings could have recovered it; declaring inputs and choosing settings had to
    sit beside each other. Measured again afterwards: 1481 px, button at y=1032.

    A later "simplification" that flattens this back into one column would silently undo that, so
    the nesting is pinned: the scale=3 control column must contain a `gr.Row` holding two more
    columns, and the audio picker and the settings must be in different ones.
    """
    tab = None
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if "LABEL_TAB_QUICK_RENDER" in ast.unparse(item.context_expr):
                    tab = node
    assert tab is not None

    control_columns = []
    for node in ast.walk(tab):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            if ast.unparse(item.context_expr) != "gr.Column(scale=3)":
                continue
            rendered = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if "audio_input" in rendered:
                control_columns.append(node)

    assert control_columns, "no scale=3 column holding the render controls"
    outer = max(control_columns, key=lambda n: len(list(ast.walk(n))))
    body = ast.unparse(ast.Module(body=outer.body, type_ignores=[]))

    rows = [n for n in ast.walk(outer) if isinstance(n, ast.With)
            and any(ast.unparse(i.context_expr) == "gr.Row()" for i in n.items)]
    assert rows, "the control column no longer splits into a row of sub-columns"

    split = max(rows, key=lambda n: len(list(ast.walk(n))))
    sub = [n for n in split.body if isinstance(n, ast.With)
           and any(ast.unparse(i.context_expr).startswith("gr.Column(") for i in n.items)]
    assert len(sub) == 2, f"expected two sub-columns, found {len(sub)}"

    left = ast.unparse(ast.Module(body=sub[0].body, type_ignores=[]))
    right = ast.unparse(ast.Module(body=sub[1].body, type_ignores=[]))
    assert "audio_input" in left and "source_mode" in left, "the inputs are not in the first column"
    assert "process_btn" in right and "processing_mode" in right, \
        "the settings and the render button are not in the second column"
    assert "audio_input" not in right, "the audio picker is duplicated across sub-columns"


# ---------------------------------------------------------------------------------------------
# Nothing was duplicated
# ---------------------------------------------------------------------------------------------

#: One execution setting, one widget. A re-layout that copies a control instead of moving it gives
#: the render two sources of truth for the same value, and whichever one the handler does not read
#: becomes a lie on screen. This is the single most likely way the refresh could have broken the
#: app, so it is asserted over every widget the three tabs moved.
_MOVED_WIDGETS = (
    "audio_input", "source_mode", "source_folder", "source_recursive", "video_input",
    "scan_btn", "source_report", "confirm_btn", "confirm_status",
    "custom_fps", "creative_preset", "output_filename",
    "process_btn", "cancel_render_btn", "status_output", "video_output",
    "open_output_folder_btn",
    "variation_seed", "randomize_btn", "surprise_btn",
    "cut_density", "micro_cuts", "semantic_emphasis", "energy_response", "motion_bias",
    "source_diversity",
    "freestyle_enabled", "freestyle_summary",
    "voice_files", "sfx_folder", "sfx_roles",
    "prep_folder", "prep_recursive", "prep_verify_btn", "prep_verify_report",
    "sample_btn", "sample_report",
)


@pytest.mark.parametrize("widget", _MOVED_WIDGETS)
def test_each_moved_widget_is_assigned_exactly_once(tree: ast.Module, widget: str):
    assignments = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and getattr(n.targets[0], "id", None) == widget
                   and isinstance(n.value, ast.Call)
                   and ast.unparse(n.value.func).startswith("gr.")]
    assert len(assignments) == 1, (
        f"{widget} has {len(assignments)} widget assignments — moving a control must never "
        f"duplicate it")


def test_processing_mode_is_still_one_widget_built_by_two_exclusive_branches(tree: ast.Module):
    """`processing_mode` is the one widget assigned twice, and it is not a duplicate: the two
    `gr.Radio` calls are the NVENC-available and CPU-only choice lists, in the two arms of one
    `if`. Asserted explicitly so it cannot be mistaken for the duplication the test above forbids,
    and so a third unconditional assignment would fail.
    """
    assignments = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and getattr(n.targets[0], "id", None) == "processing_mode"
                   and isinstance(n.value, ast.Call)]
    assert len(assignments) == 2, f"expected two exclusive branches, found {len(assignments)}"

    branching = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                 and any(a in ast.walk(n) for a in assignments)]
    assert branching, "the two assignments are not inside a conditional"
    owner = min(branching, key=lambda n: len(list(ast.walk(n))))
    assert all(a in ast.walk(owner) for a in assignments), \
        "the two assignments are not arms of the same conditional"
    assert owner.orelse, "the second assignment is not in an else branch"


def test_the_ten_freestyle_dropdowns_are_still_ten(tree: ast.Module):
    create_ui = _func(tree, "create_ui")
    rendered = ast.unparse(create_ui)
    assert rendered.count("gr.Dropdown(") >= 10
    for name in ("freestyle_intro", "freestyle_hook", "freestyle_outro", "freestyle_finale",
                 "freestyle_drop", "freestyle_chorus", "freestyle_bridge", "freestyle_breakdown",
                 "freestyle_verse", "freestyle_body"):
        assert rendered.count(f"{name} = gr.Dropdown(") == 1, name


# ---------------------------------------------------------------------------------------------
# Which tab each thing landed in
# ---------------------------------------------------------------------------------------------

def _tab_span(source: str, index: int) -> tuple[int, int]:
    start = source.index(f"gr.Tab({_TABS[index]}")
    if index + 1 < len(_TABS):
        return start, source.index(f"gr.Tab({_TABS[index + 1]}")
    # The last tab runs to the start of the event wiring, which begins with `source_outputs`.
    return start, source.index("source_outputs = [", start)


@pytest.mark.parametrize("widget", [
    "audio_input", "source_mode", "source_folder", "video_input", "custom_fps",
    "creative_preset", "processing_mode", "output_filename", "process_btn", "cancel_render_btn",
    "status_output", "video_output", "open_output_folder_btn", "active_settings_summary",
])
def test_the_render_workflow_is_on_the_quick_render_tab(source: str, widget: str):
    """Everything needed for one ordinary render, and nothing that needs another tab. This is the
    whole point of the refresh: Create Music Video used to sit below six collapsed accordions.
    """
    start, end = _tab_span(source, 0)
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not on Quick Render"


@pytest.mark.parametrize("widget", [
    "variation_seed", "randomize_btn", "cut_density", "micro_cuts", "semantic_emphasis",
    "energy_response", "motion_bias", "source_diversity", "freestyle_enabled",
    "freestyle_summary", "surprise_btn",
])
def test_the_creative_state_is_on_the_creative_studio_tab(source: str, widget: str):
    start, end = _tab_span(source, 1)
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not in Creative Studio"


@pytest.mark.parametrize("widget", [
    "voice_files", "sfx_folder", "sfx_roles", "prep_folder", "prep_recursive",
    "prep_verify_btn", "prep_verify_report", "sample_btn", "sample_report",
])
def test_the_occasional_tools_are_on_the_advanced_tab(source: str, widget: str):
    # Index 3 since Raw Cutter V1 inserted a tab at position 2.
    start, end = _tab_span(source, 3)
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not in Advanced Tools"


def test_library_prep_is_the_last_block_in_the_advanced_tab(source: str):
    """Deliberate, and load-bearing for a different suite.

    `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui` slices from
    `INFO_PREP_SAMPLE` to `sample_btn.click(` and asserts no `gr.Slider` appears in between — the
    approved sample resource policy is enforced by omission, so a slider in that span would be the
    defect. Audio Layers and Smart Mix both contain sliders, so they must not sit between the
    preparation block and the event wiring.
    """
    start, end = _tab_span(source, 3)
    block = source[start:end]
    assert block.index("LABEL_AUDIO_LAYERS") < block.index("LABEL_PREP_SECTION")
    assert block.index("LABEL_SMART_MIX") < block.index("LABEL_PREP_SECTION")

    span = source[source.index("INFO_PREP_SAMPLE"):source.index("sample_btn.click(")]
    assert "gr.Slider" not in span, (
        "a slider sits between the sample UI block and the event wiring, which defeats the "
        "omission-based sample resource policy")


# ---------------------------------------------------------------------------------------------
# Discoverability: which accordions open
# ---------------------------------------------------------------------------------------------

def test_the_ai_director_and_freestyle_are_no_longer_collapsed(source: str):
    """The task's actual complaint: these two were easy to overlook. Both are now open on arrival
    in Creative Studio.
    """
    assert "gr.Accordion(label=LABEL_AI_DIRECTOR, open=True)" in source
    assert "gr.Accordion(label=LABEL_FREESTYLE, open=True)" in source
    assert "gr.Accordion(label=LABEL_AI_DIRECTOR, open=False)" not in source
    assert "gr.Accordion(label=LABEL_FREESTYLE, open=False)" not in source


def test_the_three_bulky_blocks_stay_collapsed(source: str):
    """Opened accordions are a budget, not a default. These three are long, occasional
    configuration rather than entry points someone loses a render by missing, and each one's
    collapsed state is independently pinned by its own subsystem suite.
    """
    for label in ("LABEL_VARIANT_LAB", "LABEL_AUDIO_LAYERS", "LABEL_SMART_MIX",
                  "LABEL_PREP_SECTION"):
        assert f"gr.Accordion(label={label}, open=False)" in source, label


# ---------------------------------------------------------------------------------------------
# The one new seam: the active-settings read-out
# ---------------------------------------------------------------------------------------------

def test_the_summary_is_read_only(tree: ast.Module):
    assignment = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.Assign) and len(n.targets) == 1
                      and getattr(n.targets[0], "id", None) == "active_settings_summary")
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in assignment.value.keywords}
    assert ast.unparse(assignment.value.func) == "gr.Textbox"
    assert kwargs["interactive"] == "False", "the read-out must not be editable"


def test_the_summary_has_exactly_one_writer_and_it_is_the_tab(tree: ast.Module):
    """A second writer is how a read-out starts disagreeing with itself. One registration writes
    it, and that registration is the tab's own `select`.
    """
    writers = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "outputs" and "active_settings_summary" in ast.unparse(keyword.value):
                writers.append(ast.unparse(call.func))
    assert writers == ["tab_quick_render.select"], f"unexpected writers: {writers}"


def test_the_tab_select_writes_nothing_else(tree: ast.Module):
    """It is a read-out, not a settings authority. Writing any other component — let alone an
    execution widget — would make looking at a tab change the next render.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_quick_render.select")
    outputs = next(kw for kw in call.keywords if kw.arg == "outputs")
    assert ast.unparse(outputs.value) == "[active_settings_summary]"


def test_the_tab_select_inputs_match_the_handler_signature(tree: ast.Module):
    """Gradio passes handler arguments POSITIONALLY. This is the alignment every other GUI seam
    suite pins for its own handler, asserted here for the new one.

    Since R1 the ten Freestyle section dropdowns ride last and are collected by `*styles`, so the
    named parameters must match the leading inputs exactly and the dropdowns must make up the
    whole tail — a dropdown landing anywhere but the tail would be read as a named parameter.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_quick_render.select")
    inputs = next(kw for kw in call.keywords if kw.arg == "inputs")
    # `[...] + freestyle_section_dropdowns`
    assert isinstance(inputs.value, ast.BinOp), "the dropdowns must be appended as a whole list"
    assert ast.unparse(inputs.value.right) == "freestyle_section_dropdowns"
    names = [ast.unparse(element) for element in inputs.value.left.elts]

    handler = _func(tree, "_on_quick_render_tab_select")
    parameters = [argument.arg for argument in handler.args.args]
    assert handler.args.vararg is not None and handler.args.vararg.arg == "styles", \
        "the section dropdowns must be collected by *styles"
    assert len(names) == len(parameters), (
        f"{len(names)} named inputs for {len(parameters)} parameters: {names} vs {parameters}")

    # Each parameter is named after its widget plus a suffix, except the checkbox which Gradio
    # hands over under the same name.
    for widget, parameter in zip(names, parameters):
        assert parameter.startswith(widget), f"{parameter!r} is not fed by {widget!r}"


def test_the_six_creative_sliders_are_not_inputs_of_the_read_out(tree: ast.Module):
    """R1. The sliders were removed from this registration, and that removal IS the fix for the
    one staleness hole the read-out had.

    The Creative Preset writes all six sliders and sits on the Quick Render tab, so it could change
    what the read-out reported while the read-out was already on screen and had no reason to
    refresh. Everything still listed here lives in another tab, which means the `.select()` that
    fires on the way back is sufficient to keep every one of them current — the read-out cannot be
    stale about anything it still reports.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_quick_render.select")
    inputs = next(kw for kw in call.keywords if kw.arg == "inputs")
    rendered = ast.unparse(inputs.value)
    for slider in ("cut_density", "micro_cuts", "semantic_emphasis", "energy_response",
                   "motion_bias", "source_diversity"):
        assert slider not in rendered, f"{slider} is back in the read-out's inputs"
    assert "creative_preset" not in rendered


def test_the_preset_still_registers_exactly_one_handler(tree: ast.Module):
    """R1 considered refreshing the read-out from `creative_preset.input` and rejected it.

    Rebuilding the whole string needs `sfx_folder`, `sfx_roles` and `sfx_amount`, which
    `test_audio_layers_seam.py::test_smart_mix_widgets_are_absent_from_source_and_preparation_wiring`
    forbids on a `creative_preset` event outright, and it would have taken the selector to two
    registrations against a pin that exists to keep it at one. Deleting the redundant line instead
    needed neither guard relaxed. This asserts the guard stayed un-relaxed.
    """
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and getattr(n.func.value, "id", None) == "creative_preset"
             and n.func.attr in {"click", "change", "input", "submit", "release"}]
    assert len(calls) == 1, f"creative_preset registers {len(calls)} handlers"
    assert ast.unparse(next(kw for kw in calls[0].keywords if kw.arg == "fn").value) \
        == "_on_preset_input"


def test_the_summary_handler_writes_nothing_and_reads_no_state(tree: ast.Module):
    handler = _func(tree, "_on_quick_render_tab_select")
    # `_code_only` for the same reason as below: these bans are about machinery, and the docstring
    # is free to name any of them while explaining their absence.
    rendered = _code_only(handler)
    for forbidden in ("gr.update", "session_state", "gr.State", "_RENDER_LOCK", "subprocess",
                      "os.", "open(", "LAST_OUTPUT_PATH_KEY", "RENDER_OUTCOME_KEY"):
        assert forbidden not in rendered, f"the read-out reaches {forbidden}"
    assert handler.returns is not None and ast.unparse(handler.returns) == "str"


def test_the_summary_handler_is_total(tree: ast.Module):
    """Selecting a tab must never raise. Every numeric read is guarded, because a Gradio Number can
    legitimately arrive as None or as an empty string.
    """
    handler = _func(tree, "_on_quick_render_tab_select")
    handlers = [n for n in ast.walk(handler) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "no exception handling at all in a function that parses widget values"
    for node in handlers:
        caught = ast.unparse(node.type)
        assert "BaseException" not in caught, "BaseException must never be swallowed"
        assert "TypeError" in caught and "ValueError" in caught, caught


def test_the_summary_reports_no_creative_preset(tree: ast.Module):
    """The preset radio is on the Quick Render tab itself. Echoing it in the read-out would be the
    one line that duplicates something already on screen — and a duplicated read-out is exactly
    the stale-preview problem the Freestyle summary already had to be fixed for.
    """
    rendered = _code_only(_func(tree, "_on_quick_render_tab_select"))
    assert "creative_preset" not in rendered
    assert "BALANCED_PRESET" not in rendered


def test_the_summary_no_longer_judges_the_creative_controls(tree: ast.Module):
    """R1 — replaces `test_the_summary_compares_against_the_fork_neutral_constant`.

    That test pinned the "away from neutral" comparison to `fork_creative.DEFAULT_CONTROL` rather
    than a restated `50`, which was right for as long as the line existed. The line is gone,
    because the Creative Preset selector on the same tab already states the identical fact and
    cannot go stale — `PRESETS["Balanced"]` is six 50s, and `matching_preset` recomputes the
    selector from the live sliders — whereas this box refreshes only on tab selection.

    So the handler must not re-acquire a creative-control opinion by any route: not the constant,
    not a literal 50, and not the slider names.
    """
    rendered = _code_only(_func(tree, "_on_quick_render_tab_select"))
    assert "DEFAULT_CONTROL" not in rendered
    assert "50" not in rendered, "the neutral value must not be restated as a literal either"
    for slider in ("cut_density", "micro_cuts", "semantic_emphasis", "energy_response",
                   "motion_bias", "source_diversity"):
        assert slider not in rendered, f"{slider} is back in the read-out"


def test_balanced_really_is_the_all_neutral_recipe():
    """The equivalence the removal above rests on, asserted rather than assumed.

    If a future retune moved `Balanced` off neutral, "the selector reads Balanced" would stop
    meaning "every control is at its default" and dropping the line would start hiding something.
    This fails loudly if that ever happens.
    """
    from beatsync_fork import creative as fork_creative
    from beatsync_fork import presets as fork_presets

    balanced = fork_presets.preset_values(fork_presets.BALANCED_PRESET)
    assert balanced is not None
    assert set(balanced) == {fork_creative.DEFAULT_CONTROL}, (
        "Balanced is no longer the all-neutral recipe, so the Creative Preset selector no longer "
        "tells the user whether the six controls are at their defaults — the read-out line that "
        "said so was removed on exactly that equivalence")


# ---------------------------------------------------------------------------------------------
# R1: the read-out's actual answers, executed
#
# `gui.py` cannot be imported here, so the real handler body is AST-extracted and executed against
# a namespace holding the real fork modules. These are behavioural assertions about the shipped
# source, not assertions that some widget exists — the three R1 scenarios were all cases where the
# structure was perfectly fine and the *answer* was wrong.
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def read_out(tree: ast.Module):
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random
    from beatsync_fork import smart_mix as fork_smart_mix

    namespace = {
        "fork_freestyle_random": fork_freestyle_random,
        "fork_smart_mix": fork_smart_mix,
        "_FREESTYLE_WIDGET_ORDER": fork_freestyle.SECTION_TYPES,
    }
    exec(ast.unparse(_func(tree, "_on_quick_render_tab_select")), namespace)
    handler = namespace["_on_quick_render_tab_select"]

    def call(freestyle_enabled=False, seed=0, voice=None, sfx_folder="", sfx_roles=(),
             sfx_amount=50, styles=None):
        order = fork_freestyle.SECTION_TYPES
        resolved = styles or {}
        return handler(freestyle_enabled, seed, voice, sfx_folder, sfx_roles, sfx_amount,
                       *[resolved.get(section) for section in order])

    return call


_NOTHING = "Nothing optional is active"


def test_a_default_screen_reports_nothing_active(read_out):
    assert read_out().startswith(_NOTHING)


# -- scenario 1: Freestyle ON but every section on Base ----------------------------------------

def test_freestyle_on_with_every_section_base_is_not_reported(read_out):
    """R1 scenario 1. The checkbox is ON, but `Base` means "inherit the live global value", so the
    declaration has no overrides, `is_active()` is False, and the renderer takes the ordinary
    global path. Claiming the sections are edited differently would be false.
    """
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert answer.startswith(_NOTHING), answer
    assert "Freestyle" not in answer


def test_freestyle_on_with_one_real_rule_is_reported(read_out):
    """The positive control for the above — the correction must not simply silence Freestyle."""
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    styles[fork_freestyle.SECTION_TYPES[0]] = "Cinematic"
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert "Freestyle: 1 section type(s)" in answer, answer


def test_freestyle_on_with_random_sections_is_reported(read_out):
    """`Random` is not a preset name, so V1's resolver answers `None` for it. The read-out must go
    through the V2 builder — reading ten Random sections as ten Base sections is the exact defect
    that made PR #66 need an R1 of its own, and this is the same trap one layer up.
    """
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert f"Freestyle: {len(fork_freestyle.SECTION_TYPES)} section type(s)" in answer, answer


def test_freestyle_off_is_never_reported_whatever_the_sections_say(read_out):
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=False, styles=styles)
    assert answer.startswith(_NOTHING), answer


def test_the_freestyle_verdict_does_not_depend_on_the_seed(read_out):
    """The seed is passed because it is in scope, but a `Random` section yields a rule for every
    seed, so activity must be seed-independent. If this ever stopped being true the read-out would
    start flickering as the Variation Seed changed.
    """
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    verdicts = {("Freestyle" in read_out(freestyle_enabled=True, seed=seed, styles=styles))
                for seed in (0, 1, 7, 4242, -1, 999999)}
    assert verdicts == {True}


# -- scenario 2: Smart Mix with Amount 0 --------------------------------------------------------

def test_smart_mix_at_amount_zero_is_not_reported(read_out):
    """R1 scenario 2. `SmartMixConfig.plans_anything` is `amount > 0 and enabled_roles`, so Amount
    0 is a hard off-branch however many roles are ticked. The first version ignored Amount and
    claimed SFX would be used.
    """
    answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact", "transition"), sfx_amount=0)
    assert answer.startswith(_NOTHING), answer
    assert "Smart Mix" not in answer


def test_smart_mix_with_roles_and_amount_is_reported(read_out):
    answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact", "transition"), sfx_amount=60)
    assert "Smart Mix: 2 SFX role(s) at Amount 60" in answer, answer


def test_smart_mix_with_no_roles_is_not_reported(read_out):
    """The other documented way to mean off, and it was already handled — pinned so the Amount fix
    cannot regress it.
    """
    answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=(), sfx_amount=60)
    assert answer.startswith(_NOTHING), answer


def test_smart_mix_without_a_folder_is_not_reported(read_out):
    """`plans_anything` cannot know this — the config carries no folder — so the handler checks it
    separately. Without a library there is nothing to place whatever the three controls say.
    """
    answer = read_out(sfx_folder="   ", sfx_roles=("impact",), sfx_amount=60)
    assert answer.startswith(_NOTHING), answer


def test_the_read_out_builds_no_smart_mix_config_of_its_own(tree: ast.Module):
    """`gui.py` must contain exactly one `SmartMixConfig` construction from widget values — the
    render boundary's — because two places collapsing widgets into a config is two chances to
    disagree about what the widgets mean. `test_audio_layers_seam.py` pins the count; this pins
    the reason the read-out is not the second one, next to the code that would be tempted.
    """
    handler = _func(tree, "_on_quick_render_tab_select")
    rendered = _code_only(handler)
    assert "SmartMixConfig" not in rendered, "the read-out constructs its own Smart Mix config"
    assert "plans_anything_from" in rendered, "the verdict must come from the fork module"


def test_the_amount_verdict_follows_the_fork_record_not_a_local_rule(read_out):
    """The authority check: for every amount, the read-out's answer must equal
    `SmartMixConfig.plans_anything`. A local `> 0` that happened to agree today would drift the
    moment the record changed its mind.
    """
    from beatsync_fork import smart_mix as fork_smart_mix

    for amount in (0, 1, 25, 50, 99, 100):
        expected = fork_smart_mix.SmartMixConfig(
            enabled_roles=("impact",), amount=amount).plans_anything
        answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact",), sfx_amount=amount)
        assert ("Smart Mix" in answer) is expected, f"amount={amount}: {answer}"


# -- the remaining reports, and totality --------------------------------------------------------

def test_the_seed_and_voice_clips_are_still_reported(read_out):
    assert "Variation Seed: 7" in read_out(seed=7)
    assert "Variation Seed" not in read_out(seed=0)
    assert "Audio Layers: 2 voice clip(s)" in read_out(voice=["a.wav", "b.wav"])


def test_several_active_settings_are_all_listed(read_out):
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    styles[fork_freestyle.SECTION_TYPES[0]] = "Dynamic"
    answer = read_out(freestyle_enabled=True, seed=99, voice=["v.wav"],
                      sfx_folder=r"C:\sfx", sfx_roles=("impact",), sfx_amount=40, styles=styles)
    assert answer.startswith("This render will also use:")
    for fragment in ("Freestyle: 1", "Variation Seed: 99", "Audio Layers: 1", "Smart Mix: 1"):
        assert fragment in answer, f"{fragment} missing from {answer}"


@pytest.mark.parametrize("junk", [None, "", "abc", [], {}, object()])
def test_junk_widget_values_never_raise(read_out, junk):
    """Selecting a tab must never fail. Gradio can hand over `None` for an emptied Number, and the
    styles arrive straight from ten dropdowns.
    """
    answer = read_out(freestyle_enabled=junk, seed=junk, voice=junk, sfx_folder=junk,
                      sfx_roles=junk, sfx_amount=junk,
                      styles=None)
    assert isinstance(answer, str) and answer


def test_junk_section_styles_never_raise(read_out):
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: object() for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert isinstance(answer, str) and answer


# ---------------------------------------------------------------------------------------------
# What the refresh must not have touched
# ---------------------------------------------------------------------------------------------

def test_the_refresh_added_no_render_event(tree: ast.Module):
    """A layout change must not create a new way to start a render."""
    for suffix in (".click", ".select", ".change", ".input"):
        for call in _registrations(tree, suffix):
            fn = next((kw for kw in call.keywords if kw.arg == "fn"), None)
            if fn is None:
                continue
            target = ast.unparse(fn.value)
            if target in ("process_video_guarded", "render_selected_variants_guarded"):
                assert ast.unparse(call.func) in (
                    "process_btn.click", "render_selected_variants_btn.click"), \
                    f"{ast.unparse(call.func)} starts a render"


def test_the_tab_select_is_not_in_the_render_or_cancel_lane(tree: ast.Module):
    """It is not a render, so it must not queue behind one — and it must not be given a lane of 1
    either, for the measured reason recorded in `source-verification.md`: on the pinned
    gradio 6.19.0 a full concurrency group leaves the event queued rather than refused.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_quick_render.select")
    keywords = {kw.arg for kw in call.keywords}
    assert keywords == {"fn", "inputs", "outputs"}, f"unexpected keywords: {keywords}"


def test_the_summary_is_absent_from_the_gate_and_prep_output_lists(source: str):
    """Looking at a tab cannot invalidate a confirmed source set or move the preparation state
    machine.
    """
    for list_name in ("source_outputs = [", "prep_outputs = ["):
        start = source.index(list_name)
        end = source.index("]", start)
        assert "active_settings_summary" not in source[start:end], list_name
