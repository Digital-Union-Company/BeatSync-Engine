"""R7.2 Phase 2-4: the standalone Audio Preview tab's GUI seam.

`gui.py` cannot be imported on a bare interpreter, so this is `ast` over the real source like every
other seam suite here, plus a handful of cases that AST-extract a real handler body and execute it
against a synthesised namespace. Those are assertions about the shipped function, not about a
reimplementation of it.

What this file exists to pin:

* the tab is **independent of the Raw Audio Cutter** -- it works on a folder, after a restart, with
  no cut in this browser session;
* **nothing scans as a side effect**, including loading saved preferences, which is an explicit
  requirement and the easy thing to get wrong when a folder box has a remembered value;
* decisions are **stable across pages**, because the record is keyed by file identity;
* the tab is **isolated from the render pipeline** exactly as the Clips Preview and Audio Cutter
  tabs are.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import audio_review as review
from beatsync_fork import user_prefs as prefs

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def gui_tree(gui_source: str) -> ast.Module:
    return ast.parse(gui_source)


@pytest.fixture(scope="module")
def content() -> str:
    with open(_CONTENT, encoding="utf-8") as handle:
        return handle.read()


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def _registration(tree: ast.Module, widget: str, event: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == event
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == widget):
            return node
    raise AssertionError(f"no {widget}.{event}(...) registration")


def _wired(tree: ast.Module, widget: str, event: str, which: str) -> set:
    """The widget names an event reads or writes, resolving ONE level of list indirection.

    The Audio Preview registrations share their `outputs=` list on purpose -- all six write the
    same nine, assembled by one function -- so a plain substring check would see only the variable
    name and pass vacuously.
    """
    call = _registration(tree, widget, event)
    node = next(kw for kw in call.keywords if kw.arg == which).value
    if isinstance(node, ast.Name):
        for assign in ast.walk(tree):
            if (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                    and isinstance(assign.targets[0], ast.Name)
                    and assign.targets[0].id == node.id):
                node = assign.value
                break
        else:
            raise AssertionError(f"{widget}.{event} {which}={node.id} was never assigned")
    assert isinstance(node, (ast.List, ast.Tuple)), ast.unparse(node)
    return {n.id for n in node.elts if isinstance(n, ast.Name)}


def _tab_body(tree: ast.Module) -> str:
    """The `with gr.Tab(LABEL_SECTION_AUDIO_REVIEW ...)` node's own body.

    Taken from the AST node rather than a source slice: a slice would have to guess where the
    section ends and would silently include the next one.

    R8 UX Refresh (Commit A): this was `LABEL_TAB_AUDIO_PREVIEW`, a top-level tab. It is now the
    Audio Review section of Media Library. Only the container changed -- every widget, handler and
    registration this suite checks is the pre-existing one, which is why the rest of the file
    needed no behavioural amendment.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                text = ast.unparse(item.context_expr)
                if text.startswith("gr.Tab(LABEL_SECTION_AUDIO_REVIEW"):
                    return "\n".join(ast.unparse(statement) for statement in node.body)
    raise AssertionError("no Audio Review section")


_PREVIEW_HANDLERS = ("_on_audio_preview_scan", "_on_audio_preview_prev",
                     "_on_audio_preview_next", "_on_audio_preview_confirm",
                     "_on_audio_preview_proceed", "_on_audio_preview_cancel",
                     "_on_audio_preview_destinations_change")


# =============================================================================================
# The tab exists, in the right place, with the controls the owner asked for
# =============================================================================================

def test_audio_review_is_the_middle_media_library_section(gui_source: str):
    """R8 UX Refresh (Commit A) replaces "top-level, and before Clips Preview".

    The original claim was a position among seven top-level tabs, chosen to keep two *other*
    suites' index arithmetic valid. Both of those constraints are gone: this is now a section of
    Media Library, and nothing counts tab indices any more. What is asserted instead is the
    placement that is still load-bearing -- Audio Review sits between Video Review and Prepare &
    Verify, because Prepare & Verify must stay last for the sample-policy span
    (`test_gui_tabs.py::test_the_sample_policy_span_still_holds`).
    """
    assert "gr.Tab(LABEL_SECTION_AUDIO_REVIEW" in gui_source
    positions = {label: gui_source.index(f"gr.Tab({label}")
                 for label in ("LABEL_TAB_MEDIA_LIBRARY", "LABEL_SECTION_VIDEO_REVIEW",
                               "LABEL_SECTION_AUDIO_REVIEW", "LABEL_SECTION_PREPARE_VERIFY",
                               "LABEL_TAB_SETTINGS")}
    assert (positions["LABEL_TAB_MEDIA_LIBRARY"]
            < positions["LABEL_SECTION_VIDEO_REVIEW"]
            < positions["LABEL_SECTION_AUDIO_REVIEW"]
            < positions["LABEL_SECTION_PREPARE_VERIFY"]
            < positions["LABEL_TAB_SETTINGS"]), \
        "Audio Review is not between Video Review and Prepare & Verify inside Media Library"


def test_the_section_is_not_an_accordion_and_nests_no_further_tabs(gui_tree: ast.Module):
    """Amended by R8. "Not nested in another tab" is now deliberately false -- it is nested in
    Media Library on purpose. The property that mattered survives: an operator reviewing audio
    must see every control without opening anything, and this section must not grow a tab set of
    its own, which would bury controls two levels deep.
    """
    body = _tab_body(gui_tree)
    assert "gr.Accordion(" not in body
    assert "gr.Tab(" not in body


def test_the_section_label_is_defined_in_ui_content(content: str):
    assert "LABEL_SECTION_AUDIO_REVIEW" in content
    assert "Audio Review" in content


@pytest.mark.parametrize("widget", [
    "audio_preview_folder",             # the folder path
    "audio_preview_browse",             # the native Browse Folder button
    "audio_preview_recursive",          # include subfolders
    "audio_preview_scan_btn",           # Scan / Load Folder
    "audio_preview_page_size",
    "audio_preview_library",            # Media Library destination
    "audio_preview_discarded",          # Discarded destination
    "audio_preview_destination_status",
    "audio_preview_gallery",
    "audio_preview_counts",
    "audio_preview_decisions",
    "audio_preview_confirm_btn",
    "audio_preview_prev_btn",
    "audio_preview_next_btn",
    "audio_preview_page_label",
    "audio_preview_report",
])
def test_every_control_the_owner_asked_for_is_on_this_tab(gui_tree: ast.Module, widget: str):
    assert f"{widget} = gr." in _tab_body(gui_tree), f"{widget} is not declared in the tab"


def test_the_browse_button_is_the_native_folder_dialog(gui_tree: ast.Module, gui_source: str):
    """"Native Browse Folder button" -- the same self-contained helper every other folder field
    uses, which starts where the field points and can only ever ADD a choice."""
    assert "(audio_preview_browse, audio_preview_folder)" in gui_source
    helper = ast.unparse(_func(gui_tree, "_browse_folder"))
    assert "fork_folders.pick_folder" in helper or "pick_folder" in helper


def test_subfolders_are_optional_and_off_by_default(gui_tree: ast.Module):
    """Off by default, so one Audio Cutter run is one review batch."""
    body = _tab_body(gui_tree)
    start = body.index("audio_preview_recursive = gr.Checkbox(")
    declaration = body[start:body.index(")", body.index("elem_id", start))]
    assert "value=False" in declaration


def test_the_page_size_control_is_a_number(gui_tree: ast.Module):
    """Not cosmetic: a tab in this region of the file falls inside the source span that enforces
    the approved sample resource policy by omission, and that guard is a substring check."""
    body = _tab_body(gui_tree)
    assert "audio_preview_page_size = gr.Number(" in body
    assert "gr.Slider(" not in body


def test_the_section_declares_no_forbidden_widget_anywhere_in_its_span(gui_source: str):
    """The same guard, checked here directly so a failure names this section rather than the
    sample block 1,000 lines earlier."""
    start = gui_source.index("gr.Tab(LABEL_SECTION_AUDIO_REVIEW")
    end = gui_source.index("gr.Tab(LABEL_SECTION_PREPARE_VERIFY")
    span = gui_source[start:end]
    assert "gr.Slider(" not in span


# =============================================================================================
# Phase 2: independent of the Raw Audio Cutter
# =============================================================================================

def test_the_tab_reads_a_folder_and_needs_no_cut_in_this_session(gui_tree: ast.Module):
    """The owner's reason for the whole phase: cut today, classify next week. Scan must reach
    discovery, and must NOT read the cutter's review state, its source file or its plan."""
    scan = ast.unparse(_func(gui_tree, "_on_audio_preview_scan"))
    assert "fork_audio_review.discover_audio" in scan
    for cutter_state in ("audio_review_state", "cut_plan_state", "audio_source",
                         "_AUDIO_SURFACE_CUT", "_audio_probe", "_audio_plan"):
        assert cutter_state not in scan, f"Scan depends on the cutter via {cutter_state}"

    read = _wired(gui_tree, "audio_preview_scan_btn", "click", "inputs")
    assert read == {"audio_preview_folder", "audio_preview_recursive", "audio_preview_page_size",
                    "audio_preview_library", "audio_preview_discarded", "audio_preview_state"}


def test_nothing_scans_a_folder_just_because_preferences_loaded(gui_tree: ast.Module,
                                                                gui_source: str):
    """An explicit requirement, and the easy thing to get wrong once the folder box has a
    remembered value. The ONLY thing that may call discovery for this tab is the Scan button.
    """
    # `app.load` registrations must not reach the Audio Preview scan.
    for node in ast.walk(gui_tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"):
            text = ast.unparse(node)
            assert "_on_audio_preview_scan" not in text, "app.load scans the folder"
            assert "discover_audio" not in text

    # The folder box's own value is a remembered preference and triggers no event.
    body = _tab_body(gui_tree)
    assert "_pref(fork_prefs.AUDIO_PREVIEW_INPUT)" in body
    assert "audio_preview_folder.change(" not in gui_source
    assert "audio_preview_folder.input(" not in gui_source
    assert "audio_preview_folder.submit(" not in gui_source

    # And exactly one registration calls the scan handler.
    assert gui_source.count("fn=_on_audio_preview_scan") == 1


def test_the_two_destination_fields_scan_nothing_when_they_change(gui_tree: ast.Module):
    """Phase 4 wants immediate validation, not an immediate re-scan. The change handler writes one
    read-only textbox and must not walk a folder or touch the gallery."""
    handler = ast.unparse(_func(gui_tree, "_on_audio_preview_destinations_change"))
    assert "discover_audio" not in handler
    assert "_audio_publish" not in handler
    assert "_audio_render" not in handler
    assert "sort_clips" not in handler
    written = _wired(gui_tree, "audio_preview_library", "change", "outputs")
    assert written == {"audio_preview_destination_status"}


def test_the_saved_folder_paths_have_their_own_preference_keys():
    """Separate from the cutter's: the folder an operator REVIEWS and the folder the cutter WRITES
    INTO are different questions, and a review months later must not overwrite the cutter's
    output default."""
    assert prefs.AUDIO_PREVIEW_INPUT in prefs.LAST_USED_KEYS
    assert prefs.AUDIO_PREVIEW_LIBRARY in prefs.DESTINATION_KEYS
    assert prefs.AUDIO_PREVIEW_DISCARDED in prefs.DESTINATION_KEYS
    assert prefs.AUDIO_PREVIEW_LIBRARY != prefs.AUDIO_LIBRARY
    assert prefs.AUDIO_PREVIEW_DISCARDED != prefs.AUDIO_DISCARDED
    # Appended, never reordered: a preferences.json written before R7.2 must still load.
    assert prefs.KEYS.index(prefs.CLIPS_LIBRARY) == 0
    for key in (prefs.AUDIO_PREVIEW_LIBRARY, prefs.AUDIO_PREVIEW_DISCARDED):
        assert prefs.DESTINATION_KEYS.index(key) > prefs.DESTINATION_KEYS.index(
            prefs.AUDIO_CUTTER_OUTPUT)


def test_a_preferences_file_written_before_this_release_still_loads(tmp_path):
    """`load_preferences` fills a missing key with "", so the new keys are simply blank."""
    import json

    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"schema": prefs.SCHEMA,
                                "folders": {prefs.CLIPS_LIBRARY: r"J:\Lib"}}),
                    encoding="utf-8")
    loaded, notice = prefs.load_preferences(str(path))
    assert notice == "", "an older file is not a fault and must report nothing"
    assert loaded[prefs.CLIPS_LIBRARY] == r"J:\Lib"
    assert loaded[prefs.AUDIO_PREVIEW_INPUT] == ""
    assert loaded[prefs.AUDIO_PREVIEW_LIBRARY] == ""
    assert loaded[prefs.AUDIO_PREVIEW_DISCARDED] == ""


def test_the_preference_handlers_carry_the_three_new_fields(gui_tree: ast.Module):
    """Appended at the END of the signature, because Gradio passes arguments positionally and
    every pre-existing index must stay where the registration already put it.

    [FORK] R8 appended two more fields after these three, so `names[-3:]` is no longer this
    feature's tail. The assertion moves to the property that mattered and is unaffected by a
    later append: the three arrived as one CONTIGUOUS run, and nothing was inserted before them
    or between them. An insertion anywhere earlier would shift their indices and fail here, which
    is exactly what the original `[-3:]` check was for.
    """
    save = _func(gui_tree, "_on_save_folder_prefs")
    names = [arg.arg for arg in save.args.args]
    expected = ["audio_preview_input_folder", "audio_preview_library_folder",
                "audio_preview_discarded_folder"]
    assert all(name in names for name in expected), names
    first = names.index(expected[0])
    assert names[first:first + 3] == expected, (
        f"the three R7.2 fields are no longer one contiguous run: {names}")
    # They are still the R7.2 tail: everything after them was appended later, never inserted.
    assert first + 3 <= len(names), names
    read = _wired(gui_tree, "pref_save_btn", "click", "inputs")
    assert {"audio_preview_folder", "pref_audio_preview_library",
            "pref_audio_preview_discarded"} <= read

    body = ast.unparse(save)
    for key in ("AUDIO_PREVIEW_INPUT", "AUDIO_PREVIEW_LIBRARY", "AUDIO_PREVIEW_DISCARDED"):
        assert f"fork_prefs.{key}" in body, key


def test_reset_clears_the_two_new_destination_boxes_too(gui_tree: ast.Module):
    written = _wired(gui_tree, "pref_reset_btn", "click", "outputs")
    assert {"pref_audio_preview_library", "pref_audio_preview_discarded"} <= written
    reset = _func(gui_tree, "_on_reset_folder_prefs")
    returns = [node for node in ast.walk(reset) if isinstance(node, ast.Return)]
    for node in returns:
        assert isinstance(node.value, ast.Tuple)
        assert len(node.value.elts) == len(written), (
            "every return must have one value per output")


def test_open_results_in_media_library_fills_the_box_and_scans_nothing(gui_tree: ast.Module):
    """"Add a convenient Open results action if practical" -- and Phase 2's rule that nothing
    scans as a side effect still applies to it.

    R8 UX Refresh (Commit A): the destination is now a SECTION, so reaching it takes two
    selections rather than one. Both are asserted, because returning only one of them is a real
    and silent defect -- the inner selection alone leaves the operator on whatever top-level tab
    they were on, with Audio Review selected invisibly underneath.
    """
    handler = ast.unparse(_func(gui_tree, "_on_audio_open_in_library"))
    assert "discover_audio" not in handler
    assert "_audio_publish" not in handler
    assert "gr.Tabs(selected='tab-media-library')" in handler
    assert "gr.Tabs(selected='tab-audio-review')" in handler
    written = _wired(gui_tree, "audio_open_preview_btn", "click", "outputs")
    assert "audio_preview_folder" in written
    assert "main_tabs" in written, "the outer tab is not selected"
    assert "library_tabs" in written, "the inner section is not selected"
    # It reads the run directory the cut recorded, never a widget the operator could have edited.
    read = _wired(gui_tree, "audio_open_preview_btn", "click", "inputs")
    assert read == {"audio_run_dir_state"}


def test_open_results_says_so_plainly_when_there_is_nothing_to_open(gui_tree: ast.Module):
    namespace = {"gr": _FakeGradio()}
    exec(ast.unparse(_func(gui_tree, "_on_audio_open_in_library")), namespace)
    handler = namespace["_on_audio_open_in_library"]
    for empty in ("", None, "   "):
        returned = handler(empty)
        # One value per output, on the failure path too -- Gradio aligns them positionally, so a
        # short tuple here would write the message into the wrong component.
        assert len(returned) == 4, f"expected 4 values, got {len(returned)}"
        assert "Cut some clips first" in returned[-1]


class _FakeGradio:
    """The two `gr` members the open-in-preview handler touches."""

    class Tabs:
        def __init__(self, selected=None):
            self.selected = selected

    @staticmethod
    def update(**kwargs):
        return dict(kwargs) or "UNCHANGED"


# =============================================================================================
# Phase 2: paging, and decisions that survive it
# =============================================================================================

def test_turning_a_page_folds_the_current_decisions_in_before_it_moves(gui_tree: ast.Module):
    """The whole of "keep review decisions stable while changing pages". The record is keyed by
    file identity, so what was marked on page 1 is still marked on returning to it."""
    turn = ast.unparse(_func(gui_tree, "_audio_preview_turn"))
    assert "_audio_merge_payload" in turn
    merge = turn.index("_audio_merge_payload")
    load = turn.index("_audio_load_view")
    assert merge < load, "decisions must be folded in BEFORE the page changes"
    # And the carried-forward records are passed through by identity.
    assert "decisions=current.get('decisions')" in turn
    assert "names=current.get('names')" in turn


def test_turning_a_page_walks_no_folder(gui_tree: ast.Module):
    """Re-enumerating on every page turn would renumber the pages under the operator whenever a
    file appeared or vanished, and would charge a disk walk for a keystroke."""
    turn = ast.unparse(_func(gui_tree, "_audio_preview_turn"))
    assert "discover_audio" not in turn
    assert "current.get('paths')" in turn, "it pages over the list the scan already captured"


def test_both_page_buttons_read_the_hidden_field(gui_tree: ast.Module):
    for button in ("audio_preview_prev_btn", "audio_preview_next_btn"):
        read = _wired(gui_tree, button, "click", "inputs")
        assert "audio_preview_decisions" in read, button
        assert "audio_preview_state" in read, button


def test_the_decision_record_is_keyed_by_file_identity_not_by_page_position(gui_tree: ast.Module):
    """A per-page key would lose a decision the moment the operator turned a page; a path key would
    let a re-recorded clip inherit the old one's decision."""
    identities = ast.unparse(_func(gui_tree, "_audio_identities"))
    assert "fork_clips.decision_identity" in identities
    assert "entry.path" in identities and "entry.size" in identities
    assert "entry.mtime_ns" in identities


def test_the_page_label_tells_the_operator_where_they_are(gui_tree: ast.Module):
    label = ast.unparse(_func(gui_tree, "_audio_preview_page_label"))
    assert "page_count" in label
    assert "No clips loaded." in label


# =============================================================================================
# Phase 2: re-scan after sorting
# =============================================================================================

def test_confirm_re_enumerates_only_when_something_actually_moved(gui_tree: ast.Module):
    """"Re-scan after sorting and show remaining files." A refusal moves nothing, so it must keep
    its own view and its own message instead of triggering a pointless folder walk."""
    confirm = ast.unparse(_func(gui_tree, "_on_audio_preview_confirm"))
    assert "did_sort" in confirm
    assert "if not did_sort" in confirm
    assert "discover_audio" in confirm
    assert confirm.index("if not did_sort") < confirm.index("discover_audio")


def test_the_confirm_core_reports_whether_it_sorted_rather_than_leaving_it_inferred(
        gui_tree: ast.Module):
    """Deriving "did anything happen?" from an empty signature would conflate every refusal with
    the success, because they all return an empty one."""
    core_fn = _func(gui_tree, "_audio_confirm_core")
    returns = [node for node in ast.walk(core_fn) if isinstance(node, ast.Return)]
    assert returns, "the core must return explicitly"
    for node in returns:
        assert isinstance(node.value, ast.Tuple), ast.unparse(node)
        assert len(node.value.elts) == 7, (
            f"every return must carry the 7-value contract: {ast.unparse(node)}")
    # Exactly one return says it sorted.
    sorted_flags = [ast.unparse(node.value.elts[-1]) for node in returns]
    assert sorted_flags.count("True") == 1, sorted_flags


def test_a_failed_re_read_after_a_sort_does_not_discard_the_sort_report(gui_tree: ast.Module):
    """The files really did move. A folder that cannot be re-read afterwards is a reporting
    problem, not a reason to lose the record of what happened."""
    confirm = ast.unparse(_func(gui_tree, "_on_audio_preview_confirm"))
    assert "could not be re-read" in confirm
    assert "message +" in confirm, "the sort report must be preserved, not replaced"


# =============================================================================================
# Phase 3: the sweep gate on this tab
# =============================================================================================

def test_proceed_and_cancel_start_hidden(gui_tree: ast.Module):
    """A Proceed on screen that is not attached to a specific set of counts is exactly what the
    gate exists to prevent."""
    body = _tab_body(gui_tree)
    for button in ("audio_preview_proceed_btn", "audio_preview_sweep_cancel_btn"):
        start = body.index(f"{button} = gr.Button(")
        declaration = body[start:body.index(")", body.index("elem_classes", start))
                           if "elem_classes" in body[start:start + 400]
                           else body.index(")", body.index("elem_id", start))]
        assert "visible=False" in declaration, button


def test_all_six_registrations_write_the_same_nine_outputs(gui_tree: ast.Module):
    """One assembler, so the positional contract between six handlers and one `outputs=` list
    lives in a single function."""
    expected = {"audio_preview_report", "audio_preview_gallery", "audio_preview_counts",
                "audio_preview_state", "audio_preview_page_label", "audio_preview_armed_state",
                "audio_preview_proceed_btn", "audio_preview_sweep_cancel_btn",
                "audio_preview_destination_status"}
    for button in ("audio_preview_scan_btn", "audio_preview_prev_btn", "audio_preview_next_btn",
                   "audio_preview_confirm_btn", "audio_preview_proceed_btn",
                   "audio_preview_sweep_cancel_btn"):
        assert _wired(gui_tree, button, "click", "outputs") == expected, button


def test_every_preview_handler_returns_exactly_nine_values(gui_tree: ast.Module):
    """They all go through `_audio_preview_outputs`, which is the only way nine outputs and six
    handlers stay in step."""
    assembler = _func(gui_tree, "_audio_preview_outputs")
    returns = [node for node in ast.walk(assembler) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    assert len(returns[0].value.elts) == 9

    for name in ("_on_audio_preview_scan", "_on_audio_preview_prev", "_on_audio_preview_next",
                 "_on_audio_preview_confirm", "_on_audio_preview_cancel"):
        body = ast.unparse(_func(gui_tree, name))
        assert "_audio_preview_outputs(" in body, name


def test_proceed_is_confirm_with_the_signature_supplied(gui_tree: ast.Module):
    """There must be no path on which Proceed moves a file Confirm would not have moved."""
    body = ast.unparse(_func(gui_tree, "_on_audio_preview_proceed"))
    assert "_on_audio_preview_confirm(" in body
    assert "sort_clips" not in body
    assert "file_entry" not in body


def test_cancel_moves_nothing_and_clears_only_the_arming(gui_tree: ast.Module):
    body = ast.unparse(_func(gui_tree, "_on_audio_preview_cancel"))
    assert "sort_clips" not in body
    assert "file_entry" not in body
    assert "os.rename" not in body
    assert "updated['armed'] = ''" in body
    assert "AUDIO_SWEEP_CANCELLED_TEXT" in body


def test_cancel_repaints_the_gallery_so_the_update_is_delivered(gui_tree: ast.Module):
    """A `gr.HTML` update whose markup is byte-identical to the previous value is NOT delivered by
    the pinned Gradio, which is why the grid carries a nonce and why Cancel repaints."""
    body = ast.unparse(_func(gui_tree, "_on_audio_preview_cancel"))
    assert "_audio_preview_outputs(" in body


def test_the_gate_text_shows_the_counts_and_promises_no_deletion(content: str):
    gate = content[content.index("AUDIO_SWEEP_GATE_HEADING"):
                   content.index("LABEL_AUDIO_PROCEED")]
    assert "Nothing has moved yet" in gate
    assert "Proceed" in gate and "Cancel" in gate
    assert "Nothing is deleted" in gate
    assert "THIS batch only" in gate


def test_the_admission_text_explains_that_rejecting_everything_is_unnecessary(content: str):
    """The owner's actual complaint: clicking Reject on every unwanted clip is work the tool
    should not demand."""
    block = content[content.index("AUDIO_CONFIRM_DISABLED_TEXT"):
                    content.index("AUDIO_GALLERY_EMPTY_HTML")]
    assert "at least one clip to KEEP" in block
    assert "do not have to reject" in block


# =============================================================================================
# Phase 4: validation on this tab
# =============================================================================================

def test_confirm_revalidates_the_destinations_itself(gui_tree: ast.Module):
    """Not trusted from the change handler: the folder that was checked and the folder that is
    written must be the same one, and a folder can be renamed between the two events."""
    core_fn = ast.unparse(_func(gui_tree, "_audio_confirm_core"))
    assert "fork_audio_review.validate_destinations" in core_fn
    validate = core_fn.index("validate_destinations")
    sort = core_fn.index("_audio_sort")
    assert validate < sort, "validation must precede the sort"


def test_the_two_destination_check_is_reached_for_this_tab(gui_tree: ast.Module):
    """The missing check the owner found. The cutter's old single-destination helper could not
    express "these two are the same folder" at all."""
    core_fn = ast.unparse(_func(gui_tree, "_audio_confirm_core"))
    assert "need_library=plan.needs_library" in core_fn
    assert "need_discarded=plan.needs_discarded" in core_fn
    assert "fork_audio_library.destination_problem" not in core_fn, (
        "the single-destination helper cannot express the pair rule")


def test_a_refused_destination_pair_moves_nothing_and_keeps_the_gallery(gui_tree: ast.Module):
    core_fn = _func(gui_tree, "_audio_confirm_core")
    body = ast.unparse(core_fn)
    refusal = body[body.index("if not verdict.ok"):body.index("_audio_sort")]
    assert "Nothing was moved" in refusal
    assert "refusal_text" in refusal
    assert "gallery" in refusal, "the gallery stays usable while the folders are corrected"


# =============================================================================================
# Isolation from the render pipeline
# =============================================================================================

@pytest.mark.parametrize("handler", _PREVIEW_HANDLERS)
def test_no_preview_handler_touches_the_render_or_source_path(gui_tree: ast.Module, handler: str):
    """The same isolation the Clips Preview and Audio Cutter tabs carry: reviewing audio cannot
    clear a confirmed source set, change what a render would do, or disturb a cutter job."""
    body = ast.unparse(_func(gui_tree, handler))
    for forbidden in ("process_video", "analyze_beats_auto", "create_music_video",
                      "_RENDER_LOCK", "resolve_for_render", "live_declaration",
                      "_promote_output_no_replace", "RenderLifecycle",
                      "session_state", "source_state", "prep_state",
                      "status_output", "video_output", "process_btn"):
        assert forbidden not in body, f"{handler} reaches {forbidden}"


@pytest.mark.parametrize("button", ["audio_preview_scan_btn", "audio_preview_prev_btn",
                                    "audio_preview_next_btn", "audio_preview_confirm_btn",
                                    "audio_preview_proceed_btn",
                                    "audio_preview_sweep_cancel_btn"])
def test_no_preview_registration_writes_a_render_or_source_widget(gui_tree: ast.Module,
                                                                  button: str):
    written = _wired(gui_tree, button, "click", "outputs")
    for forbidden in ("status_output", "video_output", "process_btn", "source_state",
                      "prep_state", "variation_seed", "creative_preset", "cut_plan_state",
                      "audio_review_state", "clips_gallery", "clips_confirm_btn"):
        assert forbidden not in written, f"{button} writes {forbidden}"


@pytest.mark.parametrize("button", ["audio_preview_scan_btn", "audio_preview_prev_btn",
                                    "audio_preview_next_btn", "audio_preview_confirm_btn",
                                    "audio_preview_proceed_btn",
                                    "audio_preview_sweep_cancel_btn"])
def test_every_preview_registration_is_unqueued_and_takes_no_render_lane(gui_tree: ast.Module,
                                                                        button: str):
    """The measured S2-A R3 rule: on the pinned Gradio a full concurrency group leaves the event
    QUEUED rather than delivering it, so the handler is never entered."""
    call = _registration(gui_tree, button, "click")
    keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert keywords.get("concurrency_limit") == "None", button
    assert "concurrency_id" not in keywords, button


def test_the_preview_handlers_launch_no_process_of_their_own(gui_tree: ast.Module):
    """Two suites pin this module's process-launch site count. Durations come through the fork
    core's runner, like the poster pass, which is what keeps process launching out of the view.

    Matched by CALL SITE over the syntax tree, not as a substring: `_audio_durations`'s own
    docstring explains that the launch lives in the fork core, and a substring ban would fire on
    the sentence documenting the rule. That trap has caught this repository repeatedly.
    """
    forbidden = {"subprocess.run", "subprocess.Popen", "subprocess.call",
                 "subprocess.check_output", "os.startfile", "os.system", "os.popen",
                 "os.spawnl", "os.execv"}
    for handler in _PREVIEW_HANDLERS + ("_audio_durations", "_audio_load_view",
                                        "_audio_publish", "_audio_render", "_audio_tiles",
                                        "_audio_preview_turn", "_audio_sort"):
        node = _func(gui_tree, handler)
        calls = {ast.unparse(call.func) for call in ast.walk(node)
                 if isinstance(call, ast.Call)}
        overlap = calls & forbidden
        assert overlap == set(), f"{handler} launches a process: {sorted(overlap)}"


def test_the_two_audio_surfaces_do_not_share_a_registry(gui_source: str):
    """`ClipRegistry.new_session` revokes an owner's previous sessions, so a shared registry would
    let an Audio Preview scan silently revoke the cutter's just-cut gallery."""
    assert "_AUDIO_CUT_REGISTRY = fork_clips.ClipRegistry()" in gui_source
    assert "_AUDIO_PREVIEW_REGISTRY = fork_clips.ClipRegistry()" in gui_source
    assert "_CLIPS_REGISTRY = fork_clips.ClipRegistry()" in gui_source


def test_each_middleware_instance_is_bound_to_one_registry_and_one_prefix(gui_tree: ast.Module):
    """The prefix-to-registry pairing is a construction-time fact, so a token minted for one
    surface is simply not in the other's map."""
    launches = [node for node in ast.walk(gui_tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "launch"]
    assert launches, "no app.launch found"
    text = ast.unparse(launches[0])
    assert "AudioMediaMiddleware" in text
    assert "registry=_AUDIO_CUT_REGISTRY" in text
    assert "registry=_AUDIO_PREVIEW_REGISTRY" in text
    assert "prefix=fork_audio_review.MEDIA_PREFIX_CUTTER" in text
    assert "prefix=fork_audio_review.MEDIA_PREFIX_PREVIEW" in text
    # The R5 middleware is untouched and still first.
    assert text.index("ClipMediaMiddleware") < text.index("AudioMediaMiddleware")


def test_gradio_file_serving_permissions_were_not_widened(gui_tree: ast.Module):
    """Checked as a KEYWORD ARGUMENT, never as a substring: `gui.py` legitimately explains in prose
    why widening this would be wrong, and banning the word would ban the explanation."""
    for call in ast.walk(gui_tree):
        if isinstance(call, ast.Call):
            for forbidden in ("allowed_paths", "blocked_paths", "set_static_paths"):
                assert forbidden not in [kw.arg for kw in call.keywords], forbidden


def test_only_one_javascript_registration_was_added(gui_source: str):
    """One `app.load(..., js=AUDIO_GALLERY_JS)` serves both galleries. A second script would be a
    second client with its own idea of what a tile means."""
    assert gui_source.count("js=AUDIO_GALLERY_JS") == 1
    assert gui_source.count("js=CLIPS_GALLERY_JS") == 1
