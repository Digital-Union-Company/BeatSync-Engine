"""The tab layout, and the properties each re-layout had to preserve.

Originally the three-tab GUI Refresh A; grown to seven top-level tabs one feature at a time, and
re-organised by **R8 UX Refresh (Commit A)** into seven workflow-named tabs with three of the
former tabs demoted to sections of a new Media Library tab. See `_TABS` for the current topology
and why it changed.

`gui.py` cannot be imported on a bare interpreter, so everything here is `ast` over real source —
the same technique as the other GUI seam suites.

Every one of these re-layouts was a **pure re-parenting**: every component definition and every
event registration moved verbatim inside new `with` blocks, which R8 proved mechanically by
comparing the complete set of `name = gr.X(` assignments before and after (233 names, unchanged).
So the interesting assertions are mostly about what did *not* change. The one genuinely new seam
is the Creative Profile read-out, which R8 Commit C rebuilt around a different question -- see the
section comment there. In particular this suite exists to catch the two ways a cosmetic re-layout
can quietly break the app:

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
#: R5 P1, R7 and R7.2 each added one more top-level tab (Clips Preview, the Raw Audio Cutter, and
#: Audio Preview), and each one was slotted to keep Advanced Tools at index 3 and Clips Preview
#: last, because the containment assertions below counted from the front.
#:
#: **Rewritten by R8 UX Refresh (Commit A).** Seven top-level tabs grew one at a time, each
#: justified on its own, and the result was a drawer named "Advanced Tools" holding three
#: unrelated tenants plus two separate review tabs plus a settings panel buried at the foot of a
#: third. The order is now the operator's workflow rather than the order features arrived:
#:
#:     Render -> Creative Settings -> Raw Cutter -> Audio Cutter -> Audio Mix
#:            -> Media Library -> Settings
#:
#: Two structural changes follow from it, and both are asserted below rather than assumed:
#:
#: * **Three former containers are now SECTIONS**, not top-level tabs: Video Review (was Clips
#:   Preview), Audio Review (was Audio Preview) and Prepare & Verify (was the Library Preparation
#:   accordion inside Advanced Tools). They are `gr.Tab`s nested in a second `gr.Tabs()` inside
#:   Media Library, so there are now 7 top-level tabs, 10 `gr.Tab` calls and 2 `gr.Tabs` calls.
#: * **Index-counting is gone.** `_tab_span` keys off the label it is given rather than a position,
#:   so the next re-order cannot silently point an assertion at the wrong tab. That is why these
#:   are two separate tuples: the order assertion needs a sequence, the containment assertions
#:   need a name.
_TABS = ("LABEL_TAB_RENDER", "LABEL_TAB_CREATIVE_SETTINGS", "LABEL_TAB_RAW_CUTTER",
         "LABEL_TAB_AUDIO_CUTTER", "LABEL_TAB_AUDIO_MIX", "LABEL_TAB_MEDIA_LIBRARY",
         "LABEL_TAB_SETTINGS")

#: The Media Library sections, in their declared order. Prepare & Verify is LAST, and that is
#: load-bearing for `test_the_sample_policy_span_still_holds` below.
_SECTIONS = ("LABEL_SECTION_VIDEO_REVIEW", "LABEL_SECTION_AUDIO_REVIEW",
             "LABEL_SECTION_PREPARE_VERIFY")


def test_the_tab_set_is_exactly_the_documented_one(tree: ast.Module):
    """Asserted by IDENTITY, not by a bare count.

    The count alone used to do double duty -- it kept the topology fixed *and* incidentally banned
    any further tab. R7 legitimately added one, and renumbering a count would let any mistaken tab
    satisfy it. So the sets themselves are the assertion: these labels, nothing else.

    R8 splits that into two claims, because "a tab" is now two different things: a top-level
    destination and a Media Library section. A section promoted to a top-level tab, or a tab
    demoted to a section, must fail here rather than quietly pass a combined count.
    """
    items = [i for i in _with_items(tree) if i.startswith("gr.Tab(")]
    expected = _TABS + _SECTIONS
    assert len(items) == len(expected), \
        f"expected {len(expected)} gr.Tab calls, found {len(items)}: {items}"
    for label in expected:
        assert any(i.startswith(f"gr.Tab({label}") for i in items), f"{label} is not a tab"

    # Exactly two tab containers: the top-level set, and Media Library's sections.
    containers = [i for i in _with_items(tree) if i.startswith("gr.Tabs(")]
    assert len(containers) == 2, f"expected 2 gr.Tabs containers, found {containers}"


def test_the_tabs_are_in_the_documented_order(source: str):
    positions = [source.index(f"gr.Tab({label}") for label in _TABS]
    assert positions == sorted(positions), (
        "Render, Creative Settings, Raw Cutter, Audio Cutter, Audio Mix, Media Library, Settings "
        "— the order is the operator's workflow and is load-bearing for the containment "
        "assertions below")


def test_the_media_library_sections_are_in_the_documented_order(source: str):
    positions = [source.index(f"gr.Tab({label}") for label in _SECTIONS]
    assert positions == sorted(positions), (
        "Video Review, then Audio Review, then Prepare & Verify — Prepare & Verify must stay "
        "last; see test_the_sample_policy_span_still_holds")


def test_the_sections_are_nested_inside_the_media_library_tab(source: str):
    """A section is only a section if it is actually inside the tab. Asserted on source position
    against the NEXT top-level tab, so a section that escaped into Settings fails.
    """
    library = source.index("gr.Tab(LABEL_TAB_MEDIA_LIBRARY")
    settings = source.index("gr.Tab(LABEL_TAB_SETTINGS")
    assert library < settings
    for label in _SECTIONS:
        where = source.index(f"gr.Tab({label}")
        assert library < where < settings, f"{label} is not inside the Media Library tab"
    assert "with gr.Tabs() as library_tabs:" in source, \
        "the sections need a named container so Open-results-in-Media-Library can select one"


def test_the_raw_cutter_is_a_top_level_tab_not_an_accordion(source: str, tree: ast.Module):
    """The whole placement decision, pinned. Burying the cutter inside another tab would
    reproduce the problem the tab refresh fixed, so it must be a `gr.Tab` of its own and its
    widgets must not end up inside any accordion.

    R8: the span's end used to be the hardcoded Advanced Tools tab, which is no longer the tab
    that follows. `_tab_span` finds the real next boundary, so this survives the next re-order.
    """
    assert "gr.Tab(LABEL_TAB_RAW_CUTTER" in source
    start, end = _tab_span(source, "LABEL_TAB_RAW_CUTTER")
    block = source[start:end]
    assert "gr.Accordion" not in block, "the Raw Cutter must not hide its controls in an accordion"
    for widget in ("cut_source", "cut_output", "cut_sensitivity", "cut_min_seconds",
                   "cut_max_seconds", "cut_remove_audio", "cut_rename_output",
                   "cut_analyze_btn", "cut_run_btn",
                   "cut_cancel_btn", "cut_summary", "cut_table", "cut_report",
                   "cut_open_library_btn"):
        assert f"{widget} = gr." in block, f"{widget} is not in the Raw Cutter tab"


def test_video_review_hides_none_of_its_controls(source: str, tree: ast.Module):
    """[FORK] R5 P1, amended by R8.

    The original claim was "it is a top-level tab, and it is LAST". R8 demotes it to the first
    SECTION of Media Library, so "last" is gone and "top-level" is deliberately false now. What
    the test was actually protecting survives unchanged and is what is asserted here: an operator
    reviewing clips must be able to see every control without opening anything, so no part of
    this section may be behind an accordion.
    """
    start, end = _section_span(source, "LABEL_SECTION_VIDEO_REVIEW")
    block = source[start:end]
    assert "gr.Accordion" not in block, "Video Review must not hide its controls"


def test_video_review_uses_no_slider(source: str):
    """Measured in R5-P0, and the reason page size is a `gr.Number`.

    `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui` forbids
    `gr.Slider` over a span that runs past the tab markup into the event wiring, so a slider
    anywhere in this region would fail a test protecting the approved sample resource policy. The
    test is right; the widget is a Number.

    R8 note: Video Review now sits *before* Prepare & Verify, so it is no longer inside that span
    at all. The Number is kept regardless — the control was chosen for this reason once, and
    re-introducing the other widget type here would be a latent trap for the next re-order rather
    than an immediate failure. A latent trap is worse.
    """
    start, end = _section_span(source, "LABEL_SECTION_VIDEO_REVIEW")
    block = source[start:end]
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
    # [FORK] R8 Final Polish: SIX, not four. Keep-only classification adds the sweep gate's
    # Proceed and Cancel, mirroring Audio Review. Still an exact count rather than a
    # minimum, so a seventh registration has to be reviewed deliberately.
    assert block.count(".click(") == 6, "expected exactly six Video Review registrations"


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
    for label in _TABS + _SECTIONS:
        assert f"{label} = " in content, f"{label} is not defined in ui_content.py"


def test_the_render_column_split_is_three_to_two(source: str):
    """The task asked for roughly 3:2. The controls need the width; the preview needs enough to be
    watchable. Asserted so a later tidy-up cannot silently collapse it back to 1:1.
    """
    start, end = _tab_span(source, "LABEL_TAB_RENDER")
    block = source[start:end]
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
                if "LABEL_TAB_RENDER" in ast.unparse(item.context_expr):
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
    "output_profile", "creative_preset", "output_filename", "creative_profile_summary",
    "process_btn", "cancel_render_btn", "status_output", "video_output",
    "open_output_folder_btn", "open_creative_settings_btn",
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

def _tab_span(source: str, label: str) -> tuple[int, int]:
    """The source span of one top-level tab, located BY LABEL rather than by position.

    R8 replaces the old `_tab_span(source, index)`. Index-based lookup meant that re-ordering the
    tabs left every containment assertion pointing at a different tab while still passing, which
    is precisely the failure mode a containment assertion exists to catch. The boundary is the
    next top-level tab in *source order*, computed from positions, so neither the declaration
    order nor `_TABS` order has to be maintained by hand.
    """
    start = source.index(f"gr.Tab({label}")
    later = [p for p in (source.index(f"gr.Tab({other}") for other in _TABS) if p > start]
    # The last tab runs to the start of the event wiring, which begins with `source_outputs`.
    return start, min(later) if later else source.index("source_outputs = [", start)


def _section_span(source: str, label: str) -> tuple[int, int]:
    """The same, for a Media Library section. Its boundary is the next section, or the end of the
    Media Library tab (= the next top-level tab) for the last one."""
    start = source.index(f"gr.Tab({label}")
    later = [p for p in (source.index(f"gr.Tab({other}") for other in _SECTIONS) if p > start]
    if later:
        return start, min(later)
    return start, _tab_span(source, "LABEL_TAB_MEDIA_LIBRARY")[1]


@pytest.mark.parametrize("widget", [
    "audio_input", "source_mode", "source_folder", "video_input", "output_profile",
    "processing_mode", "output_filename", "process_btn", "cancel_render_btn",
    "status_output", "video_output", "open_output_folder_btn", "creative_profile_summary",
    "open_creative_settings_btn",
])
def test_the_render_workflow_is_on_the_render_tab(source: str, widget: str):
    """[FORK] R8 Commit C: `creative_preset` left this list and joined the Creative Settings one,
    and `active_settings_summary` was replaced by `creative_profile_summary`. The render tab now
    carries a resolved SUMMARY of the creative state rather than one of its controls."""
    """Everything needed for one ordinary render, and nothing that needs another tab. This is the
    whole point of the refresh: Create Music Video used to sit below six collapsed accordions.
    """
    start, end = _tab_span(source, "LABEL_TAB_RENDER")
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not on Render"


@pytest.mark.parametrize("widget", [
    "variation_seed", "randomize_btn", "cut_density", "micro_cuts", "semantic_emphasis",
    "energy_response", "motion_bias", "source_diversity", "freestyle_enabled",
    "freestyle_summary", "surprise_btn",
    # [FORK] R8 Commit C: the preset selector moved here, beside the six sliders it writes.
    "creative_preset",
])
def test_the_creative_state_is_on_the_creative_settings_tab(source: str, widget: str):
    start, end = _tab_span(source, "LABEL_TAB_CREATIVE_SETTINGS")
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not in Creative Settings"


@pytest.mark.parametrize("widget", ["voice_files", "sfx_folder", "sfx_roles"])
def test_the_audio_extras_are_on_the_audio_mix_tab(source: str, widget: str):
    """R8 splits the old "occasional tools" assertion in two, because the tab it named held two
    unrelated jobs. Voice and SFX layering is audio mixing and stays here; library preparation is
    a Media Library job and moved there. Keeping one combined assertion would have let either
    tenant drift into the other's tab unnoticed.
    """
    start, end = _tab_span(source, "LABEL_TAB_AUDIO_MIX")
    assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not in Audio Mix"


@pytest.mark.parametrize("widget", [
    "prep_folder", "prep_recursive", "prep_verify_btn", "prep_verify_report",
    "sample_btn", "sample_report",
])
def test_library_preparation_is_in_the_media_library_tab(source: str, widget: str):
    start, end = _section_span(source, "LABEL_SECTION_PREPARE_VERIFY")
    assert start < source.index(f"{widget} = gr.") < end, \
        f"{widget} is not in Media Library -> Prepare & Verify"


@pytest.mark.parametrize("widget", [
    "clips_input", "clips_recursive", "clips_library", "clips_rejected", "clips_page_size",
    "clips_check_btn", "clips_scan_btn", "clips_refresh_btn", "clips_report", "clips_counts",
    "clips_gallery", "clips_confirm_btn", "clips_confirm_out",
])
def test_video_review_is_in_the_media_library_tab(source: str, widget: str):
    start, end = _section_span(source, "LABEL_SECTION_VIDEO_REVIEW")
    assert start < source.index(f"{widget} = gr.") < end, \
        f"{widget} is not in Media Library -> Video Review"


def test_the_sample_policy_span_still_holds(source: str):
    """Deliberate, and load-bearing for a different suite.

    `test_media_sample.py::test_no_frame_timeout_or_budget_control_exists_in_the_ui` slices from
    `INFO_PREP_SAMPLE` to the sample button's registration and asserts no `gr.Slider` appears in
    between — the approved sample resource policy is enforced by OMISSION, so a slider in that
    span would be the defect.

    R8 moved the preparation block out of the tab that used to guarantee this by being third of
    four. The guarantee is now bought differently, and both halves are asserted here:

    * Prepare & Verify is the LAST Media Library section, so Audio Mix's sliders (Smart Mix
      Amount and Level, the voice controls) all sit *earlier* in the source and fall outside.
    * Only the Settings tab follows it, which is why every numeric control in Settings is a
      Number widget rather than the banned type.
    """
    prep = source.index("gr.Tab(LABEL_SECTION_PREPARE_VERIFY")
    assert source.index("gr.Tab(LABEL_TAB_AUDIO_MIX") < prep, \
        "Audio Mix must come before Prepare & Verify or its sliders land in the guarded span"
    for label in _SECTIONS[:-1]:
        assert source.index(f"gr.Tab({label}") < prep, f"{label} must precede Prepare & Verify"

    span = source[source.index("INFO_PREP_SAMPLE"):source.index("sample_btn.click(")]
    assert "gr.Slider" not in span, (
        "a slider sits between the sample UI block and the event wiring, which defeats the "
        "omission-based sample resource policy")


def test_the_settings_tab_holds_the_remembered_folders(source: str):
    """The panel's whole reason for moving: preferences were at the foot of a review tab."""
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    for widget in ("pref_library", "pref_rejected", "pref_raw_output", "pref_audio_library",
                   "pref_audio_discarded", "pref_audio_output", "pref_audio_preview_library",
                   "pref_audio_preview_discarded", "pref_save_btn", "pref_reset_btn",
                   "pref_status"):
        assert start < source.index(f"{widget} = gr.") < end, f"{widget} is not in Settings"


# ---------------------------------------------------------------------------------------------
# [FORK] R8 Final Polish: the Settings layout.
#
# A pure re-layout again, and the assertions say so: the same nine fields, the same nine Browse
# buttons, the same preference keys, the same two save/reset event registrations. What changed is
# which Row each widget sits in and which heading precedes it. These tests pin the two properties
# the re-layout was performed to obtain, because both are the kind a later tidy-up undoes without
# noticing.
# ---------------------------------------------------------------------------------------------

#: Every folder field on the Settings tab, paired with the Browse button that fills it. Both
#: halves of each pair are named, so a renamed widget fails loudly rather than being skipped.
_SETTINGS_BROWSE_PAIRS = (
    ("pref_render_output", "pref_render_output_browse"),
    ("pref_raw_output", "pref_raw_output_browse"),
    ("pref_audio_output", "pref_audio_output_browse"),
    ("pref_library", "pref_library_browse"),
    ("pref_rejected", "pref_rejected_browse"),
    ("pref_audio_library", "pref_audio_library_browse"),
    ("pref_audio_discarded", "pref_audio_discarded_browse"),
    ("pref_audio_preview_library", "pref_audio_preview_library_browse"),
    ("pref_audio_preview_discarded", "pref_audio_preview_discarded_browse"),
)


@pytest.mark.parametrize("field,browse", _SETTINGS_BROWSE_PAIRS)
def test_each_browse_button_sits_in_its_own_fields_row(source: str, field: str, browse: str):
    """Measured on the shipped page: Browse buttons were collected into a Row of their own BELOW
    a Row holding two text fields, so a button landed under the gap between two boxes and read as
    belonging to neither.

    The fix is structural rather than cosmetic -- one field per Row, its own Browse beside it --
    so the assertion is structural too: between a field's definition and its Browse button there
    must be no second `gr.Textbox`, and no `with gr.Row(` may open between them. Either would mean
    the button had drifted away from the field it fills.
    """
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    opening = f"{field} = gr.Textbox("
    field_at = source.index(opening, start, end)
    browse_at = source.index(f"{browse} = gr.Button(", start, end)
    assert field_at < browse_at, f"{browse} comes before the field it fills"
    # Skip past the field's OWN opening call before looking for a second one.
    between = source[field_at + len(opening):browse_at]
    assert "gr.Textbox(" not in between, f"another field sits between {field} and {browse}"
    assert "with gr.Row(" not in between, f"{browse} is in a different Row from {field}"


def test_the_folder_defaults_are_grouped_by_purpose(source: str):
    """Nine paths as one undifferentiated list is the other half of why this panel was hard to
    scan. The headings are Markdown, so they carry no state and nothing can read them -- the only
    thing pinned is that each group precedes the fields it introduces."""
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    order = [source.index(token, start, end) for token in (
        "LABEL_PREF_GROUP_VIDEO",
        "pref_render_output = gr.Textbox(",
        "LABEL_PREF_GROUP_AUDIO",
        "pref_audio_output = gr.Textbox(",
        "LABEL_PREF_GROUP_REVIEW",
        "pref_library = gr.Textbox(")]
    assert order == sorted(order), (
        "expected: render/video heading, render field, audio heading, audio field, "
        "review heading, review field")


def test_cache_management_is_collapsed_on_settings(source: str):
    """It is occasional maintenance with a destructive button in it, not setup -- so it opens on
    request. The folder defaults and the collision policy stay visible, because an operator should
    be able to read their destinations without opening anything.
    """
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    block = source[start:end]
    assert "gr.Accordion(label=LABEL_CACHE_SECTION, open=False)" in block
    assert "gr.Accordion(label=LABEL_CACHE_SECTION, open=True)" not in block


def test_only_cache_and_the_audio_overrides_are_collapsible(source: str):
    """[FORK] R8 Owner Fixes (P1): the exact set, by label, rather than a count.

    A bare count said "Settings hides one block" and would be satisfied by hiding the wrong one.
    Two blocks now collapse and each earns it differently: Cache Management is occasional
    maintenance, and the Audio Review overrides are a setting most operators never touch -- four
    near-identically named audio folders is what the owner read as duplication.

    Everything else stays visible: the folder defaults and the collision policy are what an
    operator comes to this tab to read.
    """
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    block = source[start:end]
    collapsible = {line.split("label=")[1].split(",")[0].split(")")[0]
                   for line in block.splitlines() if "gr.Accordion(" in line}
    assert collapsible == {"LABEL_CACHE_SECTION", "LABEL_PREF_AUDIO_OVERRIDES"}, collapsible


def test_the_audio_overrides_panel_opens_when_an_override_is_actually_set(source: str):
    """A setting that is in force must never be hidden behind a closed drawer -- otherwise an
    operator whose Audio Review files somewhere unexpected has no way to see why."""
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    block = source[start:end]
    assert "gr.Accordion(label=LABEL_PREF_AUDIO_OVERRIDES,\n" in block
    assert "open=_audio_overrides_in_use()" in block, \
        "the panel's open state must follow whether an override exists"


def test_the_cache_deletion_wiring_was_not_touched_by_the_re_layout(source: str):
    """The brief that moved this block said not to change the deletion logic, and no defect was
    found in it. So the selection, the confirmation gate and the destructive button must still be
    the same three widgets, inside the span that moved."""
    start, end = _tab_span(source, "LABEL_TAB_SETTINGS")
    block = source[start:end]
    for widget in ("cache_inspect_btn = gr.Button(", "cache_select = gr.CheckboxGroup(",
                   "cache_confirm = gr.Checkbox(", "cache_clear_btn = gr.Button(",
                   "cache_report = gr.Textbox("):
        assert widget in block, widget
    assert "variant='stop'" in block[block.index("cache_clear_btn = gr.Button("):], \
        "the clear button lost its destructive styling"


# ---------------------------------------------------------------------------------------------
# Discoverability: which accordions open
# ---------------------------------------------------------------------------------------------

def test_all_three_creative_panels_are_collapsed(source: str):
    """[FORK] R8 Final Polish -- replaces `test_the_ai_director_and_freestyle_are_no_longer_collapsed`.

    GUI Refresh A opened the Director and Freestyle because they were easy to overlook, and that
    was the right call when they shared one long scrolling column with everything else. Measured
    afterwards, this tab was **3,516 px tall** and the controls an operator reaches for on every
    render -- the preset, the seed, the six sliders -- were below three open panels.

    Discoverability costs one tab visit; scrolling costs every visit. So the panels are collapsed
    and the frequently-used controls are first. The ORDER still carries the old explanation
    (describe the edit, then the controls, then the lab) -- it is now the order of the collapsed
    headings rather than of three open blocks.
    """
    for label in ("LABEL_AI_DIRECTOR", "LABEL_FREESTYLE", "LABEL_VARIANT_LAB"):
        assert f"gr.Accordion(label={label}, open=False)" in source, label
        assert f"gr.Accordion(label={label}, open=True)" not in source, label


def test_the_frequently_used_creative_controls_come_first(source: str):
    """The ordering the height fix rests on, pinned so a later edit cannot quietly undo it."""
    order = [source.index(token) for token in (
        "creative_preset = gr.Radio(",
        "variation_seed = gr.Number(",
        "cut_density = gr.Slider(",
        "gr.Accordion(label=LABEL_AI_DIRECTOR",
        "gr.Accordion(label=LABEL_FREESTYLE",
        "gr.Accordion(label=LABEL_VARIANT_LAB")]
    assert order == sorted(order), (
        "expected preset, seed, sliders, then Director, Freestyle, Variant Lab")


def test_the_bulky_blocks_stay_collapsed(source: str):
    """Opened accordions are a budget, not a default. These are long, occasional configuration
    rather than entry points someone loses a render by missing, and each one's collapsed state is
    independently pinned by its own subsystem suite.

    R8 UX Refresh (Commit A) removed `LABEL_PREP_SECTION` from this list, and the reason is the
    whole argument for the budget rather than an exception to it. It was collapsed because it
    shared the Advanced Tools tab with Audio Layers and Smart Mix, and three long open blocks in
    one column is unreadable. It is now the entire content of its own Media Library section, so
    collapsing it shows an operator who navigated deliberately to Prepare & Verify one closed
    drawer and nothing else. The budget exists to keep a *shared* column readable; it was never a
    reason to hide the only thing on a screen.
    """
    for label in ("LABEL_VARIANT_LAB", "LABEL_AUDIO_LAYERS", "LABEL_SMART_MIX"):
        assert f"gr.Accordion(label={label}, open=False)" in source, label


def test_library_preparation_is_open_now_that_it_owns_a_section(source: str):
    """The positive half of the change above, asserted so a later tidy-up cannot quietly shut it
    again and reintroduce the empty-looking section."""
    assert "gr.Accordion(label=LABEL_PREP_SECTION, open=True)" in source
    assert "gr.Accordion(label=LABEL_PREP_SECTION, open=False)" not in source


# ---------------------------------------------------------------------------------------------
# The Creative Profile read-out
#
# [FORK] R8 UX Refresh (Commit C). This section replaces the "active-settings read-out" block.
# The widget, the handler and the question all changed, so the tests did too -- but the two
# properties the old block existed to protect are carried forward verbatim and are still the
# majority of what is asserted here:
#
#   1. it is a READ-OUT, not an authority -- writes nothing, stores nothing, reaches no render
#      path, and cannot invalidate a confirmed source set;
#   2. it never claims a render will be changed when it will not -- the over-reporting direction,
#      which an earlier version got wrong twice.
#
# What is new is the under-reporting direction, which is the defect the owner actually reported:
# the Creative Preset selector could read `Balanced` while Freestyle, a Variation Seed and SFX
# were all armed. The composition rule now lives in `beatsync_fork.effective_profile` and has its
# own suite (`test_effective_profile.py`); what is tested HERE is the GUI seam -- that the handler
# delegates to it, is fed the right widgets, and writes only the one box.
# ---------------------------------------------------------------------------------------------

def test_the_summary_is_read_only(tree: ast.Module):
    assignment = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.Assign) and len(n.targets) == 1
                      and getattr(n.targets[0], "id", None) == "creative_profile_summary")
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in assignment.value.keywords}
    assert ast.unparse(assignment.value.func) == "gr.Textbox"
    assert kwargs["interactive"] == "False", "the read-out must not be editable"


def test_the_old_active_settings_box_is_gone(tree: ast.Module, source: str):
    """It is replaced, not kept alongside. Two boxes describing one render is how they start
    disagreeing, and the old one's silence about the six creative controls was half the defect."""
    assert "active_settings_summary" not in _code_only(tree), \
        "the superseded read-out is still wired"
    assert "LABEL_ACTIVE_SETTINGS" not in _code_only(tree)


def test_the_summary_has_exactly_one_writer_and_it_is_the_tab(tree: ast.Module):
    """A second writer is how a read-out starts disagreeing with itself. One registration writes
    it, and that registration is the tab's own `select`.
    """
    writers = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg == "outputs" and "creative_profile_summary" in ast.unparse(keyword.value):
                writers.append(ast.unparse(call.func))
    assert writers == ["tab_render.select"], f"unexpected writers: {writers}"


def test_the_tab_select_writes_nothing_else(tree: ast.Module):
    """It is a read-out, not a settings authority. Writing any other component — let alone an
    execution widget — would make looking at a tab change the next render.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_render.select")
    outputs = next(kw for kw in call.keywords if kw.arg == "outputs")
    assert ast.unparse(outputs.value) == "[creative_profile_summary]"


def test_the_tab_select_inputs_match_the_handler_signature(tree: ast.Module):
    """Gradio passes handler arguments POSITIONALLY.

    R8 shape: the six sliders LEAD (as the `creative_control_sliders` list, so the one contract
    ordering is reused rather than restated), a fixed middle section follows, and the ten
    Freestyle dropdowns ride LAST collected by `*styles`. A dropdown landing anywhere but the tail
    would be read as a named parameter.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_render.select")
    inputs = next(kw for kw in call.keywords if kw.arg == "inputs")
    rendered = ast.unparse(inputs.value)
    assert rendered.startswith("creative_control_sliders +"), (
        "the six sliders must be contributed as the existing ordered list, not re-listed: "
        + rendered)
    assert rendered.endswith("+ freestyle_section_dropdowns"), rendered

    handler = _func(tree, "_on_render_tab_select")
    parameters = [argument.arg for argument in handler.args.args]
    assert handler.args.vararg is not None and handler.args.vararg.arg == "styles", \
        "the section dropdowns must be collected by *styles"
    # six sliders + the six middle inputs
    assert len(parameters) == 12, parameters


def test_the_six_creative_sliders_ARE_inputs_of_the_read_out(tree: ast.Module):
    """R8 — the deliberate reversal of R1's removal, and the reason it is now safe.

    R1 removed the sliders because the Creative Preset radio sat on the render tab and could
    change all six while this box, refreshed only on tab selection, went on showing the old
    answer. That was the one staleness hole the read-out had.

    Commit C moved the preset to Creative Settings, so EVERY control this box describes now lives
    on another tab and the `.select()` that fires on the way back is sufficient for all of them.
    The hole is closed by the move, not by the omission -- and the omission was what made the
    screen able to claim a plain `Balanced` render while Freestyle was armed.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "tab_render.select")
    inputs = ast.unparse(next(kw for kw in call.keywords if kw.arg == "inputs").value)
    assert "creative_control_sliders" in inputs

    # and the preset radio is genuinely NOT on the render tab any more, which is what makes the
    # tab-selection refresh sufficient rather than merely convenient.
    source = open(_GUI, encoding="utf-8").read()
    render_start, render_end = _tab_span(source, "LABEL_TAB_RENDER")
    assert not (render_start < source.index("creative_preset = gr.") < render_end), \
        "the preset is back on the render tab, which reopens the staleness hole R1 closed"


def test_the_preset_still_registers_exactly_one_handler(tree: ast.Module):
    """Unchanged by R8: moving the selector to another tab is a re-parenting, not a rewiring."""
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and getattr(n.func.value, "id", None) == "creative_preset"
             and n.func.attr in {"click", "change", "input", "submit", "release"}]
    assert len(calls) == 1, f"creative_preset registers {len(calls)} handlers"
    assert ast.unparse(next(kw for kw in calls[0].keywords if kw.arg == "fn").value) \
        == "_on_preset_input"


def test_the_summary_handler_writes_nothing_and_reads_no_state(tree: ast.Module):
    handler = _func(tree, "_on_render_tab_select")
    rendered = _code_only(handler)
    for forbidden in ("gr.update", "session_state", "gr.State", "_RENDER_LOCK", "subprocess",
                      "os.", "open(", "LAST_OUTPUT_PATH_KEY", "RENDER_OUTCOME_KEY"):
        assert forbidden not in rendered, f"the read-out reaches {forbidden}"
    assert handler.returns is not None and ast.unparse(handler.returns) == "str"


def test_the_summary_builds_no_config_or_declaration_judgement_of_its_own(tree: ast.Module):
    """Every verdict is delegated to the subsystem that owns it. Two places collapsing widgets
    into a judgement is two chances to disagree about what the widgets mean, and both of this
    handler's historical defects were exactly that.
    """
    rendered = _code_only(_func(tree, "_on_render_tab_select"))
    assert "SmartMixConfig" not in rendered, "the read-out constructs its own Smart Mix config"
    assert "plans_anything_from" in rendered, "the SFX verdict must come from the fork module"
    assert "fork_effective.summary" in rendered, "the line must come from the fork module"
    assert "matching_preset" not in rendered, \
        "the preset label must come through effective_profile, not be recomputed here"


def test_the_summary_is_absent_from_the_gate_and_prep_output_lists(source: str):
    """Looking at a tab cannot invalidate a confirmed source set or move the preparation state
    machine.
    """
    for list_name in ("source_outputs = [", "prep_outputs = ["):
        start = source.index(list_name)
        end = source.index("]", start)
        assert "creative_profile_summary" not in source[start:end], list_name


def test_balanced_really_is_the_all_neutral_recipe():
    """Still asserted, for a different reason than before.

    R1 relied on this equivalence to DELETE a line. R8 relies on it for the summary's honesty: an
    untouched screen must read as the named default rather than as `Custom`, and that is only true
    while `Balanced` is six times `DEFAULT_CONTROL`.
    """
    from beatsync_fork import creative as fork_creative
    from beatsync_fork import presets as fork_presets

    balanced = fork_presets.preset_values(fork_presets.BALANCED_PRESET)
    assert balanced is not None
    assert set(balanced) == {fork_creative.DEFAULT_CONTROL}


# ---------------------------------------------------------------------------------------------
# The read-out's actual answers, executed
#
# `gui.py` cannot be imported here, so the real handler body is AST-extracted and executed against
# a namespace holding the real fork modules. These are behavioural assertions about the shipped
# source, not assertions that some widget exists.
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def read_out(tree: ast.Module):
    from beatsync_fork import effective_profile as fork_effective
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random
    from beatsync_fork import presets as fork_presets
    from beatsync_fork import smart_mix as fork_smart_mix
    import ui_content

    namespace = {
        "fork_freestyle_random": fork_freestyle_random,
        "fork_smart_mix": fork_smart_mix,
        "fork_effective": fork_effective,
        "_FREESTYLE_WIDGET_ORDER": fork_freestyle.SECTION_TYPES,
        "CREATIVE_PROFILE_PLAIN_HINT": ui_content.CREATIVE_PROFILE_PLAIN_HINT,
        "CREATIVE_PROFILE_MODIFIED_HINT": ui_content.CREATIVE_PROFILE_MODIFIED_HINT,
    }
    exec(ast.unparse(_func(tree, "_on_render_tab_select")), namespace)
    handler = namespace["_on_render_tab_select"]

    balanced = fork_presets.preset_values(fork_presets.BALANCED_PRESET)

    def call(controls=None, freestyle_enabled=False, seed=0, voice=None, sfx_folder="",
             sfx_roles=(), sfx_amount=50, styles=None):
        order = fork_freestyle.SECTION_TYPES
        resolved = styles or {}
        six = tuple(balanced if controls is None else controls)
        return handler(*six, freestyle_enabled, seed, voice, sfx_folder, sfx_roles, sfx_amount,
                       *[resolved.get(section) for section in order])

    return call


def _first_line(answer: str) -> str:
    return answer.splitlines()[0]


#: [FORK] R8 Owner Fixes (P1): the read-out is now labelled over up to two lines --
#:
#:     Base preset: High Energy
#:     Active variations: Freestyle (10 section rules), Variation Seed 367319
#:
#: instead of chaining the same clauses with " + ". The owner confirmed the one-line form was
#: CORRECT and asked only that it be easier to read, so these helpers re-point the existing
#: assertions at the same facts rather than weakening them: `_base` still answers "which recipe",
#: and the over-reporting tests still search the whole answer for an override's name.
_BASE_PREFIX = "Base preset: "
_ACTIVE_PREFIX = "Active variations: "


def _base(answer: str) -> str:
    """The base preset name, with the label stripped."""
    head = _first_line(answer)
    assert head.startswith(_BASE_PREFIX), f"the read-out lost its base label: {head!r}"
    return head[len(_BASE_PREFIX):]


def test_an_untouched_screen_reads_as_the_named_default(read_out):
    from beatsync_fork import presets as fork_presets

    assert _base(read_out()) == fork_presets.BALANCED_PRESET


def test_a_named_preset_with_freestyle_armed_is_not_presented_as_unmodified(read_out):
    """The defect the owner reported, asserted through the real shipped handler."""
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import presets as fork_presets

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    styles[fork_freestyle.SECTION_TYPES[0]] = "Cinematic"
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert _first_line(answer) != fork_presets.BALANCED_PRESET, answer
    assert _base(answer) == fork_presets.BALANCED_PRESET, answer
    assert _ACTIVE_PREFIX in answer and "Freestyle" in answer, answer


def test_moving_a_slider_off_a_recipe_reads_as_custom(read_out):
    from beatsync_fork import effective_profile as fork_effective

    assert _base(read_out(controls=(7, 7, 7, 7, 7, 7))) == fork_effective.CUSTOM_LABEL


def test_selecting_another_recipe_reads_as_that_recipe(read_out):
    from beatsync_fork import presets as fork_presets

    cinematic = fork_presets.preset_values("Cinematic")
    assert _base(read_out(controls=cinematic)) == "Cinematic"


# -- the over-reporting direction, preserved from the previous block ---------------------------

def test_freestyle_on_with_every_section_base_is_not_reported(read_out):
    """The checkbox is ON, but `Base` means "inherit the live global value", so the declaration
    has no overrides, `is_active()` is False, and the renderer takes the ordinary global path.
    """
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import presets as fork_presets

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert _base(answer) == fork_presets.BALANCED_PRESET, answer
    assert "Freestyle" not in answer


def test_freestyle_on_with_random_sections_is_reported(read_out):
    """`Random` is not a preset name, so V1's resolver answers `None` for it. The read-out must go
    through the V2 builder — reading ten Random sections as ten Base sections is the exact defect
    that made PR #66 need an R1 of its own.
    """
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    assert "Freestyle" in read_out(freestyle_enabled=True, styles=styles)


def test_freestyle_off_is_never_reported_whatever_the_sections_say(read_out):
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random
    from beatsync_fork import presets as fork_presets

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    assert _base(read_out(freestyle_enabled=False, styles=styles)) \
        == fork_presets.BALANCED_PRESET


def test_the_freestyle_verdict_does_not_depend_on_the_seed(read_out):
    """A `Random` section yields a rule for every seed, so activity must be seed-independent or
    the read-out would flicker as the Variation Seed changed."""
    from beatsync_fork import freestyle as fork_freestyle
    from beatsync_fork import freestyle_random as fork_freestyle_random

    styles = {section: fork_freestyle_random.RANDOM_STYLE
              for section in fork_freestyle.SECTION_TYPES}
    verdicts = {("Freestyle" in read_out(freestyle_enabled=True, seed=seed, styles=styles))
                for seed in (0, 1, 7, 4242, -1, 999999)}
    assert verdicts == {True}


def test_smart_mix_at_amount_zero_is_not_reported(read_out):
    """`plans_anything` is `amount > 0 and enabled_roles`, so Amount 0 is a hard off-branch
    however many roles are ticked."""
    from beatsync_fork import presets as fork_presets

    answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact", "transition"), sfx_amount=0)
    assert _base(answer) == fork_presets.BALANCED_PRESET, answer
    assert "Smart Mix" not in answer


def test_smart_mix_with_roles_and_amount_is_reported(read_out):
    assert "Smart Mix" in read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact",), sfx_amount=60)


def test_smart_mix_with_no_roles_is_not_reported(read_out):
    assert "Smart Mix" not in read_out(sfx_folder=r"C:\sfx", sfx_roles=(), sfx_amount=60)


def test_smart_mix_without_a_folder_is_not_reported(read_out):
    """`plans_anything` cannot know this — the config carries no folder — so the handler checks it
    separately. Without a library there is nothing to place whatever the three controls say."""
    assert "Smart Mix" not in read_out(sfx_folder="   ", sfx_roles=("impact",), sfx_amount=60)


def test_the_amount_verdict_follows_the_fork_record_not_a_local_rule(read_out):
    from beatsync_fork import smart_mix as fork_smart_mix

    for amount in (0, 1, 25, 50, 99, 100):
        expected = fork_smart_mix.SmartMixConfig(
            enabled_roles=("impact",), amount=amount).plans_anything
        answer = read_out(sfx_folder=r"C:\sfx", sfx_roles=("impact",), sfx_amount=amount)
        assert ("Smart Mix" in answer) is expected, f"amount={amount}: {answer}"


def test_the_seed_and_voice_clips_are_still_reported(read_out):
    assert "Variation Seed 7" in read_out(seed=7)
    assert "Variation Seed" not in read_out(seed=0)
    assert "Audio Layers" in read_out(voice=["a.wav", "b.wav"])


def test_several_active_settings_are_all_listed(read_out):
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: fork_freestyle.BASE_STYLE for section in fork_freestyle.SECTION_TYPES}
    styles[fork_freestyle.SECTION_TYPES[0]] = "Dynamic"
    answer = read_out(freestyle_enabled=True, seed=99, voice=["v.wav"],
                      sfx_folder=r"C:\sfx", sfx_roles=("impact",), sfx_amount=40, styles=styles)
    for fragment in ("Freestyle", "Variation Seed 99", "Audio Layers", "Smart Mix"):
        assert fragment in answer, f"{fragment} missing from {answer}"


def test_a_modified_profile_says_where_to_change_it(read_out):
    """Phase 2's "a clear way to open Creative Settings", in words as well as in the button."""
    import ui_content

    assert ui_content.CREATIVE_PROFILE_MODIFIED_HINT in read_out(seed=7)
    assert ui_content.CREATIVE_PROFILE_PLAIN_HINT in read_out()


@pytest.mark.parametrize("junk", [None, "", "abc", [], {}, object()])
def test_junk_widget_values_never_raise(read_out, junk):
    """Selecting a tab must never fail. Gradio can hand over `None` for an emptied Number, and the
    styles arrive straight from ten dropdowns.
    """
    answer = read_out(controls=(junk,) * 6, freestyle_enabled=junk, seed=junk, voice=junk,
                      sfx_folder=junk, sfx_roles=junk, sfx_amount=junk, styles=None)
    assert isinstance(answer, str) and answer


def test_junk_section_styles_never_raise(read_out):
    from beatsync_fork import freestyle as fork_freestyle

    styles = {section: object() for section in fork_freestyle.SECTION_TYPES}
    answer = read_out(freestyle_enabled=True, styles=styles)
    assert isinstance(answer, str) and answer


# ---------------------------------------------------------------------------------------------
# The Media Library video source (Phase 4)
# ---------------------------------------------------------------------------------------------

def test_the_media_library_choice_is_not_a_third_source_mode(tree: ast.Module, source: str):
    """It is a local folder whose path was configured once instead of typed every time, so it maps
    onto the EXISTING `LOCAL_FOLDER` mode and reuses the whole gate behind it. A third enum member
    would have been a second source concept for the snapshot, the session state and every test
    that enumerates modes.
    """
    assert "SOURCE_CHOICE_MEDIA_LIBRARY" in source
    mapping = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and getattr(n.targets[0], "id", None) == "SOURCE_MODE_VALUES")
    rendered = ast.unparse(mapping)
    assert "SOURCE_CHOICE_MEDIA_LIBRARY: SourceMode.LOCAL_FOLDER" in rendered, rendered
    # and no new SourceMode member was invented
    from beatsync_fork.input_confirmation import SourceMode
    assert {m.value for m in SourceMode} == {"local_folder", "browser_files"}


def test_choosing_the_library_fills_the_folder_box_so_the_existing_gate_invalidates(
        tree: ast.Module):
    """The required behaviour is "changing the selected library must invalidate the previous
    source confirmation exactly as changing the local folder already does". It is obtained by
    construction rather than by a new rule: the handler WRITES `source_folder`, and that textbox's
    own `.change` already invalidates.
    """
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "source_mode.change")
    outputs = ast.unparse(next(kw for kw in call.keywords if kw.arg == "outputs").value)
    assert outputs.startswith("[source_folder,"), outputs
    assert "source_outputs" in outputs

    handler = _func(tree, "_on_source_mode_change")
    assert "SOURCE_CHOICE_MEDIA_LIBRARY" in ast.unparse(handler)
    assert "fork_prefs.CLIPS_LIBRARY" in ast.unparse(handler), \
        "it must read the configured default video Media Library"


def test_an_unconfigured_library_changes_nothing_and_says_so(tree: ast.Module):
    """Clearing the box would silently destroy a path the operator had already typed; inventing
    one would point the render at a directory nobody chose."""
    handler = ast.unparse(_func(tree, "_on_source_mode_change"))
    assert "STATUS_LIBRARY_SOURCE_UNSET" in handler
    assert "gr.Warning" in handler


def test_the_source_mode_handler_creates_no_directory(tree: ast.Module):
    """A remembered path is a suggestion, not an action -- the same rule the whole preference
    store follows."""
    handler = _code_only(_func(tree, "_on_source_mode_change"))
    for forbidden in ("makedirs", "mkdir", "scan_folder_action", "discover"):
        assert forbidden not in handler, f"selecting a source mode reaches {forbidden}"


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
                and ast.unparse(n.func) == "tab_render.select")
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
