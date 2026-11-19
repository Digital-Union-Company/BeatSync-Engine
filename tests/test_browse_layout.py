"""UX-BROWSE-LAYOUT: folder pickers visually belong to their inputs.

[FORK] Digital-Union. The owner's screenshots showed two failures with one cause -- a Browse button
that does not read as belonging to any particular field.

* **Media Library -> Video Review** kept the shape R8 Final Polish had already removed from
  Settings: a row of fields, then a row of buttons underneath. With two `scale=0` buttons bunched
  at the left of that lower row, the picker for the DISCARDED folder sat directly under the MEDIA
  LIBRARY field. Both pointed at the wrong field, which is the one sense in which a layout can be
  *incorrect* rather than merely ugly: the operator cannot tell what a control does.
* **Settings** was already one field per row (R8), so what remained there was vertical, and the
  cause is measured rather than assumed: `gr.Row` defaults to `equal_height=False`, rendering the
  class `unequal-height`, whose only rule in the pinned gradio 6.19.0 stylesheet is
  `align-items: flex-start`. A field's block is [label][input][info], so the button lands level
  with the **label** -- floating above the input it fills.

The split in this suite mirrors the split in the fix, and it matters for what may be claimed:

* the **structural** half is fully verifiable offline and is pinned here and in
  `test_gui_tabs.py` -- a button in the same row as exactly one field, directly after it, belongs
  to that field however the row is painted;
* the **stylesheet** half is asserted only as *what the page declares*. These tests prove the
  rules are present, scoped and registered. They do **not** prove the rendered result, and they
  are deliberately not written as if they did -- the roadmap requires real Gradio 6.19.0 at
  desktop and narrow widths for that, and this suite is not a substitute for it.
"""

from __future__ import annotations

import ast
import re

import pytest

_GUI = "src/gui.py"


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def css(gui_source: str) -> str:
    """The stylesheet's own text, read off the shipped assignment rather than restated here."""
    tree = ast.parse(gui_source)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "BROWSE_ROW_CSS":
                    return ast.literal_eval(node.value)
    raise AssertionError("BROWSE_ROW_CSS is not a module-level literal in gui.py")


# =============================================================================================
# 1 - the stylesheet reaches the page at all
# =============================================================================================

def test_the_stylesheet_is_registered_on_the_blocks_app(gui_source: str):
    """A rule nobody loads is not a fix. It joins the existing concatenation rather than becoming
    a second `css=` argument, because `gr.Blocks` takes one."""
    assert "BROWSE_ROW_CSS" in gui_source
    launch = gui_source[gui_source.index("css=STATUS_BOX_CSS"):]
    launch = launch[:launch.index(")")]
    assert "BROWSE_ROW_CSS" in launch, launch
    assert gui_source.count("css=STATUS_BOX_CSS") == 1, "exactly one stylesheet argument"


def test_the_existing_stylesheets_are_still_registered_and_in_order(gui_source: str):
    """Appended, not reordered or replaced. Another suite pins this prefix by substring, and the
    build-id rules in particular are load-bearing for a different feature."""
    launch = gui_source[gui_source.index("css=STATUS_BOX_CSS"):]
    launch = launch[:launch.index(")")]
    for existing in ("STATUS_BOX_CSS", "CUT_CANCEL_CSS", "CLIPS_GALLERY_CSS", "BUILD_ID_CSS"):
        assert existing in launch, existing
    assert launch.index("BUILD_ID_CSS") < launch.index("BROWSE_ROW_CSS"), \
        "the new rules are appended last"


# =============================================================================================
# 2 - what the rules say, and what they may not reach
# =============================================================================================

def test_every_rule_is_scoped_to_the_one_shared_class(css: str):
    """It must reach no other control on the page. Every selector -- including the ones inside the
    media query -- has to be qualified by the class; a bare `button { }` would restyle the app."""
    selectors = [part.strip() for part in re.findall(r"([^{}]+)\{", css)]
    selectors = [s for s in selectors if not s.startswith("@media")]
    assert selectors, css
    for selector in selectors:
        assert ".bs-browse-row" in selector, f"unscoped selector: {selector!r}"


def test_the_class_name_is_used_by_nothing_else(gui_source: str, css: str):
    """A shared class is only safe while it is this feature's alone. Every occurrence outside the
    stylesheet must be an `elem_classes` on a row that pairs a field with its picker."""
    outside = gui_source.replace(css, "")
    occurrences = outside.count("bs-browse-row")
    rows = outside.count("elem_classes='bs-browse-row'")
    assert occurrences == rows, (
        f"{occurrences - rows} occurrence(s) of the class are not a row's elem_classes")
    # Nine Settings fields plus the three on the review tab.
    assert rows == 12, f"expected 12 paired rows, found {rows}"


def test_the_field_may_shrink_so_a_long_path_cannot_overflow_the_row(css: str):
    """Requirement 4's real mechanism. A flex item will not shrink below its content width by
    default, so a long placeholder is what pushes a row wider than the viewport."""
    assert "min-width: 0" in css
    assert "flex-wrap: wrap" in css, "and if it still cannot fit, the row wraps rather than scrolls"


def test_the_button_keeps_its_natural_size_and_moves_to_the_bottom_of_the_field_block(css: str):
    assert "flex: 0 0 auto" in css, "the picker must not grow into the field's space"
    assert "align-items: flex-end" in css, \
        "the picker must move to the bottom of the field's block, not sit level with its label"
    assert "white-space: nowrap" in css, "the label must not wrap inside the button"


def test_the_gradio_default_this_corrects_is_still_what_the_pinned_build_ships():
    """The rationale, verified rather than asserted in prose.

    `align-items: flex-end` is only the right correction while Gradio's own default is
    `flex-start`. That default is why the button floats level with the field's LABEL, and it was
    read off the pinned build: `gr.Row(equal_height=False)` -- the default -- renders the class
    `unequal-height`, whose rule in gradio 6.19.0's stylesheet is `align-items: flex-start`.

    Skipped when the portable runtime is absent, exactly like the real-FFmpeg tier: the rest of
    this suite runs on bare CPython, and this one assertion needs the installed frontend. If a
    future Gradio flips that default, this fails and the correction above has to be re-derived
    instead of silently becoming a no-op or an over-correction.
    """
    import glob
    import os

    row = ("bin/python-3.13.14-embed-amd64/Lib/site-packages/gradio/layouts/row.py")
    assets = ("bin/python-3.13.14-embed-amd64/Lib/site-packages/gradio/"
              "templates/frontend/assets")
    if not os.path.isfile(row) or not os.path.isdir(assets):
        pytest.skip("the portable runtime is not installed in this checkout")

    with open(row, encoding="utf-8") as handle:
        assert "equal_height: bool = False" in handle.read(), \
            "gr.Row no longer defaults to unequal height; re-derive the alignment correction"

    rule = None
    for path in glob.glob(os.path.join(assets, "*.css")):
        with open(path, encoding="utf-8", errors="strict") as handle:
            text = handle.read()
        match = re.search(r"\.unequal-height[^{]*\{([^}]*)\}", text)
        if match:
            rule = match.group(1)
            break
    assert rule is not None, "no .unequal-height rule found in the pinned frontend"
    assert "align-items:flex-start" in rule.replace(" ", ""), rule


def test_a_narrow_viewport_gets_a_deliberate_stacked_layout(css: str):
    """"Intentional stacked layout on narrower viewports", not whatever wrapping happens to do."""
    assert "@media" in css, "there must be a narrow-viewport rule at all"
    query = css[css.index("@media"):]
    assert "max-width" in query
    assert "flex-direction: column" in query, "the field and its picker stack"
    assert "align-items: stretch" in query, "and both span the width"
    assert "width: 100%" in query, "a full-width picker under a full-width field"


def test_no_absolute_positioning_or_height_arithmetic(css: str):
    """The same reasoning the build-id rules record: a negative margin or a pinned height tuned
    against one Gradio version is what silently overlaps something in the next one. Every property
    here degrades to ordinary flex behaviour if the surrounding markup changes."""
    for forbidden in ("position: absolute", "position: fixed", "margin-top: -", "margin-bottom: -",
                      "margin-left: -", "margin-right: -", "transform:", "height: 4", "top: -",
                      "line-height: 0"):
        assert forbidden not in css, f"{forbidden!r} is layout arithmetic, not a flex rule"


def test_the_rules_touch_no_colour_so_nothing_depends_on_them_for_meaning(css: str):
    """A layout change must not become a status signal, and must not fight the theme."""
    for forbidden in ("color:", "background", "border-color", "opacity"):
        assert forbidden not in css, f"{forbidden!r} is not a layout property"


# =============================================================================================
# 3 - nothing about behaviour moved with the markup
# =============================================================================================

#: Every folder picker this item repositioned, with the field it fills.
_ALL_PAIRS = (
    ("clips-input-folder", "clips-input-browse"),
    ("clips-library-folder", "clips-library-browse"),
    ("clips-rejected-folder", "clips-rejected-browse"),
    ("pref-render-output", "pref-render-output-browse"),
    ("pref-raw-output", "pref-raw-output-browse"),
    ("pref-audio-output", "pref-audio-output-browse"),
    ("pref-library", "pref-library-browse"),
    ("pref-rejected", "pref-rejected-browse"),
    ("pref-audio-library", "pref-audio-library-browse"),
    ("pref-audio-discarded", "pref-audio-discarded-browse"),
)


@pytest.mark.parametrize("field_id,browse_id", _ALL_PAIRS)
def test_every_repositioned_control_keeps_its_element_id(gui_source: str, field_id: str,
                                                         browse_id: str):
    """`elem_id`s are the stable contract the browser tests and the stylesheets read. A re-layout
    that renamed one would break a surface this change never looked at."""
    assert f"elem_id='{field_id}'" in gui_source, field_id
    assert f"elem_id='{browse_id}'" in gui_source, browse_id


def test_the_pickers_are_still_wired_by_the_ONE_shared_table_and_handler(gui_source: str):
    """Requirement 6, and this is the shape that already satisfied it: every folder picker in the
    app is wired by one loop over `(button, field)` pairs with a single `.click` registration, so
    there is exactly one copy of the browse behaviour.

    Repositioning markup must not touch that. The assertion is therefore that each repositioned
    button still appears in that table paired with **its own** field -- a pair table is precisely
    where a re-layout could mis-pair a button without any test noticing, because the markup would
    look right and the binding would be wrong.
    """
    tree = ast.parse(gui_source)
    loops = [n for n in ast.walk(tree)
             if isinstance(n, ast.For) and "_browse_button" in ast.unparse(n.target)]
    assert len(loops) == 1, f"there must be exactly one browse-wiring loop, found {len(loops)}"
    loop = loops[0]

    clicks = [n for n in ast.walk(loop)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "click"]
    assert len(clicks) == 1, "one registration for every picker in the app"
    handler = next(ast.unparse(k.value) for k in clicks[0].keywords if k.arg == "fn")
    assert handler == "_browse_folder", handler

    pairs = {}
    for element in ast.walk(loop.iter):
        if isinstance(element, ast.Tuple) and len(element.elts) == 2:
            button, field = (ast.unparse(e) for e in element.elts)
            if button.endswith("_browse"):
                pairs[button] = field
    for field, button in (("clips_input", "clips_input_browse"),
                          ("clips_library", "clips_library_browse"),
                          ("clips_rejected", "clips_rejected_browse"),
                          ("pref_library", "pref_library_browse"),
                          ("pref_rejected", "pref_rejected_browse"),
                          ("pref_render_output", "pref_render_output_browse")):
        assert pairs.get(button) == field, (
            f"{button} fills {pairs.get(button)!r}, not {field!r}")


def test_a_picker_sends_and_receives_only_its_own_field(gui_source: str):
    """The self-contained property the helper was built for: Browse starts where the field already
    points and writes back to the same field, so it can reach nothing else -- and a cancelled
    dialog leaves the value exactly as it was."""
    tree = ast.parse(gui_source)
    loop = next(n for n in ast.walk(tree)
                if isinstance(n, ast.For) and "_browse_button" in ast.unparse(n.target))
    click = next(n for n in ast.walk(loop)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "click")
    keywords = {k.arg: ast.unparse(k.value) for k in click.keywords}
    assert keywords["inputs"] == "[_target]", keywords
    assert keywords["outputs"] == "[_target]", keywords


def test_the_subfolders_switch_is_still_on_the_review_tab_with_its_own_text(gui_source: str):
    """It moved out of the clips-folder row -- it is a scan option, not that field's picker -- and
    had to keep its value, its label and its helper text."""
    block = gui_source[gui_source.index("clips_recursive = gr.Checkbox("):]
    block = block[:block.index(")")]
    assert "value=True" in block
    assert "LABEL_CLIPS_RECURSIVE" in block
    assert "INFO_CLIPS_RECURSIVE" in block
    assert "elem_id='clips-recursive'" in block


@pytest.mark.parametrize("info", [
    "INFO_CLIPS_INPUT", "INFO_CLIPS_LIBRARY", "INFO_CLIPS_REJECTED",
])
def test_the_review_fields_keep_their_helper_text_on_the_field_itself(gui_source: str, info: str):
    """"Keep its label and helper text with that exact input." The helper text stays an `info=` on
    the field, so it cannot drift away from the box it describes."""
    assert f"info={info}" in gui_source, info


def test_loading_the_page_still_reads_no_directory(gui_source: str):
    """Requirement: loading Settings must never scan or create folders. This change is markup and a
    stylesheet, so the properties worth pinning are that the rows it touched introduced no
    filesystem call -- the saved values still arrive through the same `_pref` read."""
    tree = ast.parse(gui_source)
    builder = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "create_ui")
    # The paired rows are markup only: the field values come from `_pref`, which reads the
    # preferences file and never touches a media folder.
    for field in ("clips_input", "clips_library", "clips_rejected",
                  "pref_library", "pref_rejected"):
        assignment = next(
            n for n in ast.walk(builder)
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == field for t in n.targets))
        rendered = ast.unparse(assignment)
        assert "_pref(" in rendered, f"{field} no longer reads its saved value through _pref"
        for forbidden in ("os.makedirs", "os.mkdir", "os.listdir", "os.scandir", "os.walk",
                          "glob", "scan_library_folder", "scan_folder", "os.path.exists",
                          "os.stat"):
            assert forbidden not in rendered, f"{field}'s definition reaches {forbidden}"
