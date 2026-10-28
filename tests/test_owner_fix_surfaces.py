"""[FORK] R8 Owner Fixes (P1/P2): the surfaces the owner's hands-on playtest asked for.

Four complaints, each with a property that must survive later edits:

* **Freestyle was easy to miss.** A small checkbox decides whether the six global Creative
  Controls apply at all, so missing its state means misreading the whole render. The panel now
  states the mode in words and only shows the ten section rules in the mode where they do
  anything -- *hidden, never cleared*, which is the part worth pinning.
* **Clear Creative Variations looked like a text link.** It is now a real button beside
  Randomize, and the two must stay behaviourally distinct.
* **Four audio destinations read as duplicated.** They are four real settings, but Audio Review
  now falls back to the Voice pair when its own override is blank. The fallback is READ-SIDE
  ONLY: nothing is ever consolidated, and a differing stored value always wins.
* **Stale or misleading text.** "where KEPT clips would go" described a plan that has moved
  files since UX V3; the Render tab printed "stays disabled until you confirm" while confirmed
  and rendering.

`gui.py` cannot be imported on a bare interpreter, so the GUI halves are `ast` and text over real
shipped source; the preference logic is ordinary stdlib-only fork code and is executed directly.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import user_prefs as fork_prefs

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


@pytest.fixture(scope="module")
def content() -> str:
    with open(_UI_CONTENT, encoding="utf-8") as handle:
        return handle.read()


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def _call(tree: ast.Module, dotted: str) -> ast.Call:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == dotted:
            return node
    raise AssertionError(f"no call {dotted}")


# =============================================================================================
# P1 — Freestyle says which mode it is in, and hiding is not clearing
# =============================================================================================

def test_the_switch_has_a_badge_beside_it(source: str):
    assert "freestyle_state_badge = gr.Markdown(" in source
    assert "elem_id='freestyle-state-badge'" in source


def test_both_badge_states_name_what_is_used(content: str):
    """"Freestyle is off" alone tells an operator nothing. The off state has to say what applies
    instead, because something always does."""
    assert "FREESTYLE_BADGE_OFF" in content and "FREESTYLE_BADGE_ON" in content
    off = content[content.index("FREESTYLE_BADGE_OFF = ("):]
    off = off[:off.index("\n)")]
    assert "Creative Controls" in off, "the off state must name what is used instead"
    on = content[content.index("FREESTYLE_BADGE_ON = ("):]
    on = on[:on.index("\n)")]
    assert "ON" in on and "OFF" in off, "each state must be readable at a glance"


def test_the_section_rules_are_hidden_rather_than_reset(source: str, tree: ast.Module):
    """THE property of this change. A Gradio component with `visible=False` keeps its value and
    still submits it, so switching Freestyle off and on returns the operator to the rules they
    built. Clearing them instead would make a visibility switch destructive."""
    assert "with gr.Group(visible=False) as freestyle_sections_group:" in source

    handler = _func(tree, "_on_freestyle_change")
    returned = ast.unparse(handler)
    assert "gr.update(visible=on)" in returned
    # it must not write a dropdown's value back
    for section in ("intro", "hook", "outro", "finale", "drop", "chorus", "bridge",
                    "breakdown", "verse", "body"):
        assert f"freestyle_{section} =" not in returned, f"it assigns freestyle_{section}"


def test_every_freestyle_change_writes_the_same_three_read_outs(source: str):
    """One list means the badge can never disagree with the switch that produced it."""
    assert "_freestyle_readout_outputs = [freestyle_summary, freestyle_state_badge," in source
    assert source.count("outputs=_freestyle_readout_outputs") == 11, (
        "all eleven Freestyle registrations share the one read-out list")


def test_the_badge_follows_the_checkbox_and_nothing_else(tree: ast.Module):
    """The checkbox is still the only gate. The badge is derived from it in one expression, so
    there is no second place a mode could be decided."""
    handler = _func(tree, "_on_freestyle_change")
    body = ast.unparse(handler)
    assert "on = bool(freestyle_enabled)" in body
    assert "FREESTYLE_BADGE_ON if on else FREESTYLE_BADGE_OFF" in body


# =============================================================================================
# P1 — Randomize and Clear are two real buttons, and stay distinct
# =============================================================================================

def test_the_two_seed_actions_share_one_row_at_the_same_size(source: str):
    """Clear was a `size='sm'` control under a full-width Randomize, which reads as a link."""
    row = source[source.index("randomize_btn = gr.Button("):]
    row = row[:row.index("gr.Markdown(INFO_CLEAR_VARIATIONS)")]
    assert "clear_variations_btn = gr.Button(" in row, "the two are not adjacent"
    assert "size='sm'" not in row, "one of them is still smaller than the other"
    assert "variant='stop'" in row, "Clear discards work and should say so"


def test_clear_is_still_not_randomize(tree: ast.Module):
    """Opposite operations on the same setting. Randomize writes a seed and only a seed; Clear
    zeroes it and turns Freestyle off. Neither may grow into the other."""
    randomize = _call(tree, "randomize_btn.click")
    clear = _call(tree, "clear_variations_btn.click")

    randomize_outputs = ast.unparse(
        next(kw for kw in randomize.keywords if kw.arg == "outputs").value)
    assert randomize_outputs == "[variation_seed]"

    clear_outputs = ast.unparse(next(kw for kw in clear.keywords if kw.arg == "outputs").value)
    assert "freestyle_enabled" in clear_outputs
    assert "variation_seed" in clear_outputs


def test_the_clear_label_says_what_it_resets(content: str):
    info = content[content.index("INFO_CLEAR_VARIATIONS = ("):]
    info = info[:info.index("\n)")]
    assert "Variation Seed" in info and "Freestyle" in info
    assert "sliders" in info and "preset" in info, "it must say what it leaves alone"
    assert "Audio Mix" in info


# =============================================================================================
# P1 — one voice pair, with an override that is never consolidated
# =============================================================================================

def test_audio_review_falls_back_to_the_shared_voice_pair(source: str):
    assert "def _audio_preview_default(" in source
    body = source[source.index("def _audio_preview_default("):]
    body = body[:body.index("\ndef ", 10)]
    assert "_pref(override_key) or _pref(shared_key)" in body, \
        "a set override must win; blank must fall back"


def test_the_fallback_is_read_side_only_and_never_persisted(tree: ast.Module):
    """THE safety property. If the save path wrote a fallback into the override key, two distinct
    settings would silently become one and the operator's Audio Review folder would be gone."""
    saver = _func(tree, "_on_save_folder_prefs")
    body = ast.unparse(saver)
    assert "_audio_preview_default" not in body, \
        "saving must write each key from its own field, never a fallback"
    for key in ("AUDIO_PREVIEW_LIBRARY: audio_preview_library_folder",
                "AUDIO_PREVIEW_DISCARDED: audio_preview_discarded_folder"):
        assert key in body, key


def test_a_differing_override_is_shown_rather_than_hidden(source: str):
    """A setting that is in force must never sit behind a closed drawer."""
    assert "def _audio_overrides_in_use(" in source
    assert "open=_audio_overrides_in_use()" in source


def test_the_optional_panel_says_blank_means_the_pair_above(content: str):
    info = content[content.index("INFO_PREF_AUDIO_OVERRIDES = ("):]
    info = info[:info.index("\n)")]
    assert "blank" in info.lower()
    assert "never changes the folders above" in info or "never overwrite" in info


# =============================================================================================
# P1 — a work folder that is also a finished library is reported, never relocated
# =============================================================================================

_LABELS = {
    fork_prefs.AUDIO_CUTTER_OUTPUT: "Default Audio Cutter output",
    fork_prefs.AUDIO_PREVIEW_LIBRARY: "Default Audio Preview Media Library",
    fork_prefs.AUDIO_LIBRARY: "Default voice library",
    fork_prefs.RAW_CUTTER_OUTPUT: "Default Raw Cutter output",
    fork_prefs.CLIPS_LIBRARY: "Media Library folder",
}


def test_the_owners_exact_configuration_is_reported():
    """Both pointing at `Raw_Cutts`, which is what the owner's screenshot showed."""
    notice = fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: r"J:\Raw_Cutts",
         fork_prefs.AUDIO_PREVIEW_LIBRARY: r"J:\Raw_Cutts"}, _LABELS)
    assert notice
    assert "Raw_Cutts" in notice
    assert "mixed" in notice.lower()


def test_distinct_folders_are_not_reported():
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: r"J:\Raw_Cutts",
         fork_prefs.AUDIO_PREVIEW_LIBRARY: r"J:\Voice Library"}, _LABELS) == ""


def test_blank_settings_are_not_an_overlap():
    """Two unset folders are not "the same folder" -- reporting them would train the operator to
    ignore the notice."""
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: "", fork_prefs.AUDIO_PREVIEW_LIBRARY: ""}, _LABELS) == ""
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: "   ", fork_prefs.AUDIO_PREVIEW_LIBRARY: None},
        _LABELS) == ""


@pytest.mark.parametrize("written,configured", [
    (r"J:\Raw_Cutts", r"j:\raw_cutts"),
    (r"J:\Raw_Cutts", "J:\\Raw_Cutts\\"),
    (r"J:\Raw_Cutts", "J:/Raw_Cutts"),
    (r"J:\Raw_Cutts", r"J:\Voice\..\Raw_Cutts"),
])
def test_the_same_folder_spelled_differently_is_still_the_same_folder(written, configured):
    """Case, separator and a trailing slash are spellings, not different directories. Missing
    one would make the warning fire on some screens and not others for the same setup."""
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: written,
         fork_prefs.AUDIO_PREVIEW_LIBRARY: configured}, _LABELS)


def test_the_raw_cutter_output_is_checked_too():
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.RAW_CUTTER_OUTPUT: r"J:\Parts",
         fork_prefs.CLIPS_LIBRARY: r"J:\Parts"}, _LABELS)


def test_the_notice_relocates_nothing_and_refuses_nothing():
    """It is a sentence. The save that produced it already succeeded."""
    notice = fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: r"J:\Raw_Cutts",
         fork_prefs.AUDIO_PREVIEW_LIBRARY: r"J:\Raw_Cutts"}, _LABELS)
    assert "Nothing was moved or changed" in notice
    assert "not saved" not in notice.lower(), "the save succeeded; this is advice, not a refusal"


def test_the_overlap_check_touches_no_filesystem(monkeypatch):
    """A read-out must not stat anything: it has to work for a folder that does not exist yet,
    and must not stall on a disconnected drive."""
    def explode(*args, **kwargs):                                # pragma: no cover - must not run
        raise AssertionError("the overlap check touched the filesystem")

    for name in ("exists", "isdir", "realpath", "stat"):
        monkeypatch.setattr(os.path, name, explode, raising=False)
    assert fork_prefs.overlapping_destinations(
        {fork_prefs.AUDIO_CUTTER_OUTPUT: r"Q:\nope", fork_prefs.AUDIO_LIBRARY: r"Q:\nope"},
        _LABELS)


# =============================================================================================
# P2 — text that describes what actually happens
# =============================================================================================

def test_the_media_library_label_no_longer_says_would_go(content: str):
    """Confirm has moved files since UX V3. "would go" described the plan-only version."""
    label = content[content.index("LABEL_CLIPS_LIBRARY = "):]
    label = label[:label.index("\n")]
    assert "would go" not in label
    assert "MOVED" in label


def test_the_discarded_label_still_says_never_deleted(content: str):
    label = content[content.index("LABEL_CLIPS_REJECTED = "):]
    label = label[:label.index("\n")]
    assert "never deleted" in label.lower() or "never deletion" in label.lower()


def test_the_confirm_label_does_not_claim_only_decided_clips_move(content: str):
    """Keep-only sorting also files the unmarked clips, after an explicit Proceed. A label
    promising "the decided clips" is now false for the common path."""
    label = content[content.index("LABEL_CLIPS_CONFIRM = "):]
    label = label[:label.index("\n")]
    assert "decided clips" not in label


def test_the_render_tab_no_longer_prints_a_contradiction(content: str):
    """It was printed unconditionally, including while the sources WERE confirmed and Stage 6 was
    rendering. The live half is `confirm_status`, which tracks the real state."""
    info = content[content.index("INFO_CONFIRMATION_GATE = ("):]
    info = info[:info.index("\n)")]
    assert "stays disabled" not in info
    # the fact it carried is still stated, as a property of the gate. Asserted on the joined
    # text, because the constant is wrapped across source lines and "re-checked before rendering"
    # straddles the break.
    joined = " ".join(info.split())
    assert "actual files" in joined
    assert "re-checked before " in joined and "rendering" in joined


def test_the_saved_folders_status_says_what_it_describes(content: str, source: str):
    """The owner saw it naming superseded paths beside newly typed ones. That is correct -- it
    reports SAVED settings -- and reads as a contradiction unless it says so."""
    assert "PREF_STATUS_SCOPE" in content
    scope = content[content.index("PREF_STATUS_SCOPE = ("):]
    scope = scope[:scope.index("\n)")]
    assert "saved settings" in scope
    assert "Save defaults" in scope
    assert "PREF_STATUS_SCOPE" in source, "the scope line must actually be shown"


def test_every_status_writer_goes_through_one_composer(tree: ast.Module, source: str):
    """A save, a reset and the startup notice must not describe the stored settings three
    different ways -- that is how a stale warning survives a successful save."""
    assert "def _folder_prefs_status(" in source
    for handler in ("_on_save_folder_prefs", "_on_reset_folder_prefs"):
        assert "_folder_prefs_status(" in ast.unparse(_func(tree, handler)), handler


# =============================================================================================
# P2 — Variant Lab presentation only
# =============================================================================================

def test_the_long_guidance_moved_behind_a_help_panel(source: str):
    assert "gr.Accordion(label=LABEL_VARIANT_HELP, open=False)" in source
    assert "gr.Markdown(INFO_VARIANT_LAB_LEAD)" in source


def test_no_guidance_was_deleted_only_moved(source: str):
    """Every block that used to be permanently visible is still shown, inside the panel. The
    point was to stop it occupying the screen, not to remove the explanation."""
    for name in ("INFO_VARIANT_LAB", "INFO_MASTER_SEED", "INFO_VARIANT_RANGES",
                 "INFO_VARIANT_AUDIO", "INFO_VARIANT_AUDIO_RANGES", "INFO_VARIANT_COMPARE",
                 "INFO_VARIANT_APPLY"):
        assert name in source, f"{name} is no longer shown anywhere"


def test_the_result_boxes_start_small_and_can_still_grow(source: str):
    """`lines` is the empty height and `max_lines` the grown one. Cutting the ceiling instead of
    the floor would clip a real report, which is the opposite of the fix."""
    report = source[source.index("variant_report = gr.Textbox("):]
    report = report[:report.index(")\n")]
    assert "lines=2," in report and "max_lines=12," in report

    table = source[source.index("variant_batch_table = gr.Textbox("):]
    table = table[:table.index(")\n")]
    assert "lines=3," in table
    assert "max_lines=16," in table, "16 holds twelve candidate rows plus the header"


def test_every_variant_lab_control_survived(source: str):
    """Presentation only: the controls, ranges, candidate selector and render operations are
    untouched."""
    for widget in ("variant_master_seed", "variation_spread", "variant_report",
                   "variant_batch_table", "variant_candidate_selector", "apply_variant_btn",
                   "variant_batch_status"):
        assert f"{widget} = gr." in source, widget
