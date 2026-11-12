"""R15-C: one review surface for exported audio.

The increment, in one sentence: the Raw Audio Cutter's duplicated Keep/Discard gallery is removed,
and **Media Library -> Audio Review** becomes the single canonical place where exported clips are
reviewed. The Cutter exports and hands off; Audio Review reviews.

`gui.py` cannot be imported on a bare interpreter, so this suite works the way every other seam
suite here does -- `ast` over the real source, plus cases that AST-extract a real handler body and
execute it against a synthesised namespace. Those are assertions about the shipped functions, not
about reimplementations of them.

What this file exists to pin, grouped as the task that commissioned it listed them:

* a **successful** cut hands over its own exact run directory, and a failed or empty one hands over
  nothing -- including the case the old code got wrong, where every clip failed but the directory
  had been created;
* **repeated** handoff presses are idempotent, and a handoff **refusal** retires nothing;
* a handoff **retires Audio Review's stale page authority**, so a delayed Confirm, Proceed or
  decision event cannot act on the folder the operator just navigated away from;
* **switching between two different run directories** lands on the second one with no residue of
  the first;
* Audio Review remains **independently usable**, including after a restart with no cut in the
  session;
* the handoff performs **no scan, no classification, no sort and no file movement**;
* the **audio output and manifest contracts are unchanged** by the removal;
* the GUI's components, event inputs/outputs and callback registrations stay **consistent**, with no
  orphaned Cutter-side gallery state;
* the **R14 inventory panels and the startup upload protection are untouched.**

Companion suites, deliberately not duplicated here: `test_audio_cutter.py` holds the absence
assertions for each removed widget and handler, `test_audio_preview_tab.py` holds Audio Review's own
behaviour, `test_cut_handoff.py` holds the record type, and `test_audio_review.py` holds the shared
review core.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import audio_cutter as cutter
from beatsync_fork import audio_review as review
from beatsync_fork import cut_handoff as handoff

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


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


def _registration(tree: ast.Module, widget: str, event: str = "click") -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == event
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == widget):
            return node
    raise AssertionError(f"no {widget}.{event}(...) registration")


def _resolve(tree: ast.Module, node: ast.AST) -> ast.AST:
    if not isinstance(node, ast.Name):
        return node
    for assign in ast.walk(tree):
        if (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                and isinstance(assign.targets[0], ast.Name)
                and assign.targets[0].id == node.id):
            return assign.value
    raise AssertionError(f"{node.id} was never assigned")


def _names(tree: ast.Module, node: ast.AST) -> list:
    """Widget names in an `inputs=`/`outputs=` expression, IN ORDER, through variables and `+`.

    Order matters here and a set would discard it: Gradio aligns a handler's returned tuple against
    this list positionally, so "the fifth output is Audio Review's report" is the claim that stops a
    refusal message from being written into a gallery.
    """
    node = _resolve(tree, node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _names(tree, node.left) + _names(tree, node.right)
    assert isinstance(node, (ast.List, ast.Tuple)), ast.unparse(node)
    return [n.id for n in node.elts if isinstance(n, ast.Name)]


def _wired(tree: ast.Module, widget: str, which: str, event: str = "click") -> list:
    call = _registration(tree, widget, event)
    return _names(tree, next(kw for kw in call.keywords if kw.arg == which).value)


# =============================================================================================
# The handoff chain, executed for real
# =============================================================================================

class _FakeGradio:
    """The two `gr` members the handoff chain touches."""

    class Tabs:
        def __init__(self, selected=None):
            self.selected = selected

    @staticmethod
    def update(**kwargs):
        return dict(kwargs) or "UNCHANGED"


#: Four of the handoff's own widgets, then the nine Audio Review outputs it repaints.
_HANDOFF_OUTPUTS = 4 + 9
#: Index of Audio Review's report inside the returned tuple. Named rather than spelled 4 at every
#: call site, because the whole hazard this suite guards is a message landing in the wrong slot.
_REPORT_AT = 4
#: Index of Audio Review's view (`audio_preview_state`) -- the page authority itself.
_VIEW_AT = 4 + 3


@pytest.fixture
def chain(gui_tree: ast.Module) -> dict:
    """The real handoff chain against a synthesised namespace.

    Five real functions are executed rather than reimplemented: `_on_audio_open_in_library`,
    `_audio_handoff_refused`, `_audio_handoff_retire_review`, the nine-output assembler
    `_audio_preview_outputs`, and `_audio_preview_page_label`. `cut_handoff`, `audio_review`, `os`
    and both refusal strings are the genuine articles.

    Only `_audio_render` and `_AUDIO_SURFACE_PREVIEW` are stubbed, and both for the same reason --
    they need a live registry, which cannot exist on a bare interpreter. The stub RECORDS every view
    it is handed, which is what lets the retirement tests assert that an empty one arrives.
    """
    import ui_content

    rendered = []

    def _audio_render(surface, view):
        rendered.append(view)
        return ("GALLERY", "COUNTS")

    namespace = {
        "gr": _FakeGradio(),
        "os": os,
        "fork_handoff": handoff,
        "fork_audio_review": review,
        "_AUDIO_SURFACE_PREVIEW": object(),
        "_audio_render": _audio_render,
        "AUDIO_HANDOFF_NONE_TEXT": ui_content.AUDIO_HANDOFF_NONE_TEXT,
        "AUDIO_HANDOFF_MISSING_TEXT": ui_content.AUDIO_HANDOFF_MISSING_TEXT,
    }
    for node in gui_tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "_AUDIO_PREVIEW_OUTPUT_ORDER"):
            exec(ast.unparse(node), namespace)
            break
    else:
        raise AssertionError("_AUDIO_PREVIEW_OUTPUT_ORDER is not module-level in gui.py")
    for name in ("_audio_preview_page_label", "_audio_preview_outputs",
                 "_audio_handoff_refused", "_audio_handoff_retire_review",
                 "_on_audio_open_in_library"):
        exec(ast.unparse(_func(gui_tree, name)), namespace)
    namespace["_views_rendered"] = rendered
    return namespace


def _record(directory, clips=3, outcome=handoff.OUTCOME_COMPLETED):
    return handoff.build(directory, mode=handoff.MODE_SINGLE, clips=clips, outcome=outcome)


# =============================================================================================
# A successful cut hands over its own exact run directory
# =============================================================================================

def test_a_successful_cut_hands_over_that_run_directory_and_not_its_parent(chain, tmp_path):
    """The exact finished-run folder, which is the whole accuracy claim.

    The parent output folder accumulates one timestamped run per cut, so handing *it* over would
    show an operator every cut they have ever made when they asked about one. The record carries
    the run directory and the handler fills that in verbatim.
    """
    run = tmp_path / "take_20261112_181755"
    run.mkdir()
    parent = str(tmp_path)

    out = chain["_on_audio_open_in_library"](_record(str(run)))
    assert len(out) == _HANDOFF_OUTPUTS
    assert out[0] == {"value": str(run)}
    assert out[0]["value"] != parent, "the parent output folder was handed over"
    # Subfolders excluded, from the job's own shape rather than an operator preference: a single
    # audio cut writes its clips directly into one folder.
    assert out[1] == {"value": False}
    assert out[2].selected == "tab-media-library", "the outer tab is not selected"
    assert out[3].selected == "tab-audio-review", "the inner section is not selected"
    assert str(run) in out[_REPORT_AT]
    assert "Press Scan Folder" in out[_REPORT_AT]


def test_the_report_says_audio_review_and_never_video_review(chain, tmp_path):
    """One record type serves both cutters, so the sentence has to name the right destination.

    `cut_handoff.loaded_text` defaults to Video Review for R11's sake; the audio caller passes its
    own. A default left unpassed would send the operator a confidently wrong instruction.
    """
    run = tmp_path / "run"
    run.mkdir()
    message = chain["_on_audio_open_in_library"](_record(str(run)))[_REPORT_AT]
    assert "Audio Review" in message
    assert "Video Review" not in message


@pytest.mark.parametrize("outcome,fragment", [
    (handoff.OUTCOME_COMPLETED, "clip(s) written"),
    (handoff.OUTCOME_PARTIAL, "some did not finish"),
])
def test_a_partial_cut_is_not_described_as_a_clean_one(chain, tmp_path, outcome, fragment):
    """A cut with failures is still worth reviewing, and must not be reported as complete."""
    run = tmp_path / "run"
    run.mkdir()
    message = chain["_on_audio_open_in_library"](_record(str(run), outcome=outcome))[_REPORT_AT]
    assert fragment in message


# =============================================================================================
# A failed or empty cut hands over nothing
# =============================================================================================

@pytest.mark.parametrize("bad", [
    None, "", "   ", 0, [], "C:/not/a/record",
    {}, {"directory": "C:/x"},
    {"directory": "", "mode": "single", "clips": 3, "outcome": "completed"},
    {"directory": "C:/x", "mode": "single", "clips": 0, "outcome": "completed"},
    {"directory": "C:/x", "mode": "single", "clips": -1, "outcome": "completed"},
    {"directory": "C:/x", "mode": "single", "clips": "three", "outcome": "completed"},
    {"directory": "C:/x", "mode": "nonsense", "clips": 3, "outcome": "completed"},
    {"directory": "C:/x", "mode": "single", "clips": 3, "outcome": "nonsense"},
])
def test_nothing_reviewable_is_refused_in_words_and_navigates_nowhere(chain, bad):
    """Every shape that is not a finished cut reads as "no finished cut", never as a directory.

    The zero-clip case is the one the old code got wrong. `audio_run_dir_state` was a bare path
    written whenever the run directory had been CREATED, so an export in which every single clip
    failed still offered its empty folder for review -- and the operator pressed Scan Folder on
    nothing. `cut_handoff.build` answers None below one published clip, so the offer and the gate
    are the same object.
    """
    out = chain["_on_audio_open_in_library"](bad)
    assert len(out) == _HANDOFF_OUTPUTS, "a short tuple would misalign every later output"
    assert out[0] == "UNCHANGED", "a refused handoff must not fill the folder box"
    assert out[1] == "UNCHANGED", "a refused handoff must not change the recursion tick"
    assert out[2] == "UNCHANGED" and out[3] == "UNCHANGED", "it must not navigate anywhere"
    assert "no finished cut to review yet" in out[_REPORT_AT]


def test_a_cut_whose_folder_has_since_gone_is_reported_not_repaired(chain, tmp_path):
    """A record describes a job that finished, not a disk that has stayed still since.

    Reported, never repaired, and never silently replaced by a parent that still exists -- which is
    exactly the substitution that would hand over three cuts instead of the missing one.
    """
    missing = str(tmp_path / "deleted_after_the_cut")
    out = chain["_on_audio_open_in_library"](_record(missing))
    assert len(out) == _HANDOFF_OUTPUTS
    assert out[0] == "UNCHANGED"
    assert missing in out[_REPORT_AT]
    assert "no longer there" in out[_REPORT_AT]
    assert "Nothing was changed." in out[_REPORT_AT]


def test_a_file_where_the_run_directory_should_be_is_not_accepted(chain, tmp_path):
    """`isdir`, not `exists`: a file with the run directory's name is not a folder of clips."""
    impostor = tmp_path / "run"
    impostor.write_bytes(b"not a directory")
    out = chain["_on_audio_open_in_library"](_record(str(impostor)))
    assert out[0] == "UNCHANGED"
    assert "no longer there" in out[_REPORT_AT]


# =============================================================================================
# Retiring stale page authority -- the R11 F-1 hazard, in audio
# =============================================================================================

def test_a_successful_handoff_retires_whatever_audio_review_was_holding(chain, tmp_path):
    """The hazard, and it is a real one rather than cosmetic.

    Filling the folder box with a new folder while the gallery, the counts and an armed sweep all
    still described the PREVIOUS one meant that pressing Confirm sorted the previous folder -- and
    an armed sweep moved its unmarked clips into Discarded -- while every visible label named the
    new one.

    The empty view is the mechanism; the widget updates are consequences of it. `_audio_sort` reads
    `view.get('entries')` and finds none, so Confirm, Proceed and the sort all refuse because the
    page is gone, not because something in the browser went grey.
    """
    run = tmp_path / "run"
    run.mkdir()
    out = chain["_on_audio_open_in_library"](_record(str(run)))

    assert out[_VIEW_AT] == {}, "the page authority was not retired"
    assert chain["_views_rendered"][-1] == {}, "the gallery was repainted from a surviving page"
    # The armed sweep signature is cleared, so a confirmation minted for the previous batch cannot
    # authorise a sweep of the next one.
    assert out[4 + 5] == "", "an armed sweep survived the handoff"
    # And the gate's own two buttons are closed, both halves of them.
    assert out[4 + 6] == {"visible": False}
    assert out[4 + 7] == {"visible": False}


def test_a_refused_handoff_retires_nothing_at_all(chain, tmp_path):
    """A mis-press must not cost an operator the page they were working on.

    Both refusal paths change the report and nothing else. This is the asymmetry that makes the
    retirement safe to have: it fires only when the operator is actually being moved.
    """
    for bad in (None, _record(str(tmp_path / "gone"))):
        out = chain["_on_audio_open_in_library"](bad)
        for index in range(_HANDOFF_OUTPUTS):
            if index == _REPORT_AT:
                continue
            assert out[index] == "UNCHANGED", (
                f"a refused handoff wrote output {index}; it must leave the page alone")


def test_pressing_the_handoff_twice_is_idempotent(chain, tmp_path):
    """Repeated presses, which an operator will make because nothing on screen says it worked yet.

    The second press must land on the same folder with the same retired page -- not accumulate
    state, not re-enumerate, and not report something different the second time.
    """
    run = tmp_path / "run"
    run.mkdir()
    record = _record(str(run))
    first = chain["_on_audio_open_in_library"](record)
    second = chain["_on_audio_open_in_library"](record)

    assert first[0] == second[0] == {"value": str(run)}
    assert first[_VIEW_AT] == second[_VIEW_AT] == {}
    assert first[_REPORT_AT] == second[_REPORT_AT]
    # The record itself is not consumed or mutated by being handed over: a handoff is a statement
    # about a finished job, so reading it must not change it.
    assert record == _record(str(run))


def test_switching_between_two_run_directories_leaves_no_residue_of_the_first(chain, tmp_path):
    """Two cuts in one session, reviewed one after the other.

    The second handoff must land wholly on the second folder: its own path, its own report, and a
    page retired again rather than the first folder's page still standing underneath.
    """
    first_run = tmp_path / "take_a_20261112_100000"
    second_run = tmp_path / "take_b_20261112_110000"
    first_run.mkdir()
    second_run.mkdir()

    first = chain["_on_audio_open_in_library"](_record(str(first_run), clips=4))
    second = chain["_on_audio_open_in_library"](_record(str(second_run), clips=9))

    assert first[0] == {"value": str(first_run)}
    assert second[0] == {"value": str(second_run)}
    assert str(first_run) not in second[_REPORT_AT], "the first folder leaked into the second report"
    assert "9 clip(s) written" in second[_REPORT_AT]
    # Retired both times, not just the first.
    assert second[_VIEW_AT] == {}
    assert chain["_views_rendered"] == [{}, {}]


def test_the_real_renderer_survives_the_empty_view_the_retirement_hands_it(gui_tree: ast.Module):
    """The one thing the stubbed `chain` fixture cannot tell us.

    `_audio_render` is stubbed there because it needs a live registry, so the retirement tests prove
    that `{}` *arrives* but not that the renderer survives it. If the real one raised on an empty
    view, the handoff would 500 in the operator's browser and every test above would still pass.

    So the genuine `_audio_tiles`, `_audio_page_decisions` and `_audio_render` are executed here
    against the real `audio_review` module, with only the surface described by a stand-in carrying
    the four ids it reads.
    """
    namespace = {"fork_audio_review": review}
    for name in ("_audio_tiles", "_audio_page_decisions", "_audio_render"):
        exec(ast.unparse(_func(gui_tree, name)), namespace)

    class _Surface:
        grid_id = review.GRID_PREVIEW
        store_id = "audio-preview-decisions"
        counts_id = "audio-preview-counts"
        media_prefix = review.MEDIA_PREFIX_PREVIEW
        empty_message = "No audio clips loaded. Choose a folder and press Scan Folder."

    grid, counts = namespace["_audio_render"](_Surface(), {})
    assert _Surface.empty_message in grid
    assert "r7-tile" not in grid, "an empty view produced tile markup"
    assert "Total 0" in counts


def test_the_retired_empty_state_is_still_deliverable_to_the_browser():
    """The grid nonce, and why the retirement depends on it.

    A `gr.HTML` update whose markup is byte-identical to the previous value is **not delivered** by
    the pinned Gradio. An operator who is already looking at the empty state and then presses the
    handoff would otherwise get a retirement the browser never applied — the server's page gone, the
    screen unchanged. Two identical calls must therefore differ.
    """
    first = review.gallery_html([], {}, "", "", grid_id=review.GRID_PREVIEW,
                                empty_message="EMPTY")
    second = review.gallery_html([], {}, "", "", grid_id=review.GRID_PREVIEW,
                                 empty_message="EMPTY")
    assert "data-nonce=" in first, "the empty state carries no nonce"
    assert first != second, "two empty-state repaints are byte-identical and would not be delivered"


def test_the_retirement_reaches_no_disk_and_mints_no_session(gui_tree: ast.Module):
    """It repaints an empty page. Walking a folder, publishing a page or minting a registry session
    would all be side effects of navigation, which is the one thing this button must not have."""
    body = ast.unparse(_func(gui_tree, "_audio_handoff_retire_review"))
    for forbidden in ("discover_audio", "_audio_publish", "_audio_load_view", "new_session",
                      "publish_page", "os.walk", "os.scandir", "os.listdir", "os.rename",
                      "os.remove", "os.makedirs", "sort_clips", "file_clip"):
        assert forbidden not in body, f"the retirement reaches {forbidden}"


# =============================================================================================
# The handoff scans nothing, classifies nothing and moves nothing
# =============================================================================================

def test_the_handoff_performs_no_scan_classification_sort_or_move(gui_tree: ast.Module):
    """Navigation is not an action. The operator still presses Scan Folder, and still confirms any
    sorting there -- which is the requirement that survives unchanged from R7.2 Phase 2."""
    body = ast.unparse(_func(gui_tree, "_on_audio_open_in_library"))
    for forbidden in ("discover_audio", "_audio_publish", "_audio_load_view", "_audio_sort",
                      "sort_clips", "file_clip", "os.rename", "os.replace", "os.remove",
                      "os.unlink", "shutil", "os.makedirs", "_on_audio_preview_scan",
                      "_audio_confirm_core", "SELECTED", "REJECTED"):
        assert forbidden not in body, f"the handoff reaches {forbidden}"
    # The one filesystem question it DOES ask is a single `isdir` -- a stat, not a walk.
    assert body.count("os.path.isdir") == 1
    for walk in ("os.walk", "os.scandir", "os.listdir", "glob"):
        assert walk not in body, f"the handoff enumerates with {walk}"


def test_the_handoff_is_not_wired_to_any_scan_or_decision_handler(gui_tree: ast.Module):
    """Asserted on the registration, because a `.then()` chain would be a scan the handler itself
    does not contain."""
    call = _registration(gui_tree, "audio_open_preview_btn")
    assert ast.unparse(next(kw for kw in call.keywords if kw.arg == "fn").value) == \
        "_on_audio_open_in_library"
    # No chained follow-up event: the registration is the whole of it.
    assert not isinstance(call.func.value, ast.Call), "the handoff click is chained onto something"
    rendered = ast.unparse(call)
    for forbidden in ("_on_audio_preview_scan", "_on_audio_preview_confirm", ".then(", ".success("):
        assert forbidden not in rendered, f"the handoff registration reaches {forbidden}"


# =============================================================================================
# Audio Review stays independently usable
# =============================================================================================

def test_audio_review_still_owns_a_folder_box_the_operator_can_type_into(gui_tree: ast.Module):
    """The handoff is a convenience, never the only way in. Reviewing clips this app did not just
    make -- after a restart, or from another machine's export -- is the surface's original reason
    for existing, and R15-C must not quietly make it depend on a cut."""
    folder = next(node for node in ast.walk(gui_tree)
                  if isinstance(node, ast.Assign) and len(node.targets) == 1
                  and isinstance(node.targets[0], ast.Name)
                  and node.targets[0].id == "audio_preview_folder")
    keywords = {kw.arg: ast.unparse(kw.value) for kw in folder.value.keywords}
    assert "interactive=False" not in ast.unparse(folder.value), (
        "the folder box must stay editable")
    assert keywords.get("value", "").startswith("_pref("), (
        "it should still come up with the saved folder")

    # Scan reads the box and the tab's own state, and nothing belonging to the cutter.
    read = _wired(gui_tree, "audio_preview_scan_btn", "inputs")
    assert "audio_preview_folder" in read
    assert "audio_handoff_state" not in read, "Scan now depends on a cut having happened"


def test_every_audio_review_registration_survived_the_removal(gui_tree: ast.Module):
    """Eight events: Scan, Prev, Next, Confirm, Proceed, sweep Cancel, and both destination
    `.change()` validators. Removing the cutter's half must not have taken any of them."""
    for widget in ("audio_preview_scan_btn", "audio_preview_prev_btn", "audio_preview_next_btn",
                   "audio_preview_confirm_btn", "audio_preview_proceed_btn",
                   "audio_preview_sweep_cancel_btn"):
        _registration(gui_tree, widget, "click")
    for widget in ("audio_preview_library", "audio_preview_discarded"):
        _registration(gui_tree, widget, "change")


def test_the_saved_voice_pair_preferences_were_not_orphaned(gui_tree: ast.Module, gui_source: str):
    """The removed boxes were CONSUMERS of `AUDIO_LIBRARY`/`AUDIO_DISCARDED`, never their owners.

    Settings still writes both keys, and Audio Review's own destinations still fall back to them,
    so an operator's saved voice-library pair keeps working. Worth pinning: deleting a widget that
    displays a preference is exactly how a preference becomes unreachable without anything failing.
    """
    saved = _wired(gui_tree, "pref_save_btn", "inputs")
    assert "pref_audio_library" in saved, "Settings no longer saves the voice library folder"
    assert "pref_audio_discarded" in saved, "Settings no longer saves the discarded folder"

    # And the fallback is still in place on Audio Review's two destinations: its own key first,
    # then the voice-pair key the removed boxes used to show. Asserted on the real `_pref(...)`
    # call arguments rather than as a substring, so a comment mentioning the keys cannot pass it.
    fallbacks = []
    for widget in ("audio_preview_library", "audio_preview_discarded"):
        node = next(n for n in ast.walk(gui_tree)
                    if isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == widget)
        value = next(kw.value for kw in node.value.keywords if kw.arg == "value")
        fallbacks.append(ast.unparse(value))
    assert "fork_prefs.AUDIO_PREVIEW_LIBRARY" in fallbacks[0]
    assert "fork_prefs.AUDIO_LIBRARY" in fallbacks[0], (
        "Audio Review no longer falls back to the saved voice library folder")
    assert "fork_prefs.AUDIO_PREVIEW_DISCARDED" in fallbacks[1]
    assert "fork_prefs.AUDIO_DISCARDED" in fallbacks[1], (
        "Audio Review no longer falls back to the saved discarded folder")


# =============================================================================================
# The export contracts are unchanged
# =============================================================================================

def test_the_cut_still_writes_the_same_files_the_same_way(gui_tree: ast.Module):
    """R15-C is a review-surface change. Cutting, silence detection, formats, output naming and the
    run directory's exclusive creation are all untouched, and the suite says so here rather than
    leaving it implied by the absence of a failure elsewhere."""
    cut = ast.unparse(_func(gui_tree, "_on_audio_cut"))
    for preserved in ("_audio_settings", "_audio_mp3_problem", "_audio_probe", "_audio_plan",
                      "fork_audio.safe_stem", "fork_audio.segment_rows",
                      "fork_audio.run_directory_name", "fork_audio.clip_filename",
                      "fork_audio.build_extract_command", "fork_audio.run_media_command",
                      "fork_audio.encoding_record", "fork_audio.manifest_payload",
                      "fork_audio.MANIFEST_NAME", "_audio_format_line", "_audio_fade_line"):
        assert preserved in cut, f"the cut no longer reaches {preserved}"
    # The run directory is still created exclusively: a fresh folder per cut is what makes
    # overwriting impossible without an existence check a second press could race.
    node = _func(gui_tree, "_on_audio_cut")
    makedirs = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "os.makedirs"]
    assert len(makedirs) == 1
    assert {kw.arg: ast.unparse(kw.value) for kw in makedirs[0].keywords}.get("exist_ok") == "False"


def test_the_manifest_contract_is_byte_for_byte_the_old_one():
    """Asserted against the fork core, which the removal did not touch at all."""
    assert cutter.MANIFEST_NAME
    payload = cutter.manifest_payload.__doc__ or ""
    assert payload, "the manifest builder lost its contract documentation"
    assert hasattr(cutter, "encoding_record")
    assert list(cutter.FORMATS), "the format list is empty"


def test_removing_the_gallery_did_not_remove_the_review_core():
    """The engine Audio Review runs on. R15-C removes a surface, never the thing underneath it."""
    for name in ("discover_audio", "gallery_html", "media_url", "sort_clips",
                 "validate_destinations", "destination_feedback", "page_count",
                 "PENDING", "SELECTED", "REJECTED", "SERVABLE_SUFFIXES",
                 "MEDIA_PREFIX_CUTTER", "MEDIA_PREFIX_PREVIEW", "GRID_PREVIEW"):
        assert hasattr(review, name), f"audio_review lost {name}"


def test_the_cut_no_longer_measures_every_clip_just_to_label_a_tile(gui_tree: ast.Module):
    """A real cost removed, not merely hidden.

    Publishing the cutter's page ran an FFprobe duration pass over every exported clip so each tile
    could show a length. With no tile to label, that work is gone -- and so is the per-clip `stat`
    the span map needed. The timings an operator actually needs are still on screen in the plan
    table and still recorded in the manifest, so nothing truthful was lost with it.
    """
    cut = ast.unparse(_func(gui_tree, "_on_audio_cut"))
    for gone in ("_audio_durations", "_audio_load_view", "_audio_render", "_audio_publish",
                 "decision_identity", "os.path.getsize", "os.stat"):
        assert gone not in cut, f"the cut still reaches {gone}"


# =============================================================================================
# Wiring consistency
# =============================================================================================

def test_the_handoff_writes_one_value_per_output_on_every_path(gui_tree: ast.Module):
    """Gradio aligns a returned tuple against `outputs=` positionally, so an arity that differs
    between paths writes a message into a gallery. Asserted on the registration and on the handler
    together, because either one alone can be right while the pair is wrong."""
    outputs = _wired(gui_tree, "audio_open_preview_btn", "outputs")
    assert len(outputs) == _HANDOFF_OUTPUTS, outputs
    assert outputs[:4] == ["audio_preview_folder", "audio_preview_recursive",
                           "main_tabs", "library_tabs"]
    # The nine that follow are Audio Review's own, in the order the assembler emits them.
    assert outputs[4:] == _wired(gui_tree, "audio_preview_scan_btn", "outputs")


def test_the_handoff_shares_audio_reviews_output_list_rather_than_copying_it(gui_tree: ast.Module):
    """Structural, so the two cannot drift.

    If Audio Review grows a tenth output, the shared list grows and this registration grows with
    it. A hand-copied second set of nine is how one gets forgotten -- and a forgotten output is a
    silent positional misalignment, not an error anybody sees.
    """
    call = _registration(gui_tree, "audio_open_preview_btn")
    expression = next(kw for kw in call.keywords if kw.arg == "outputs").value
    assert isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add), \
        ast.unparse(expression)
    assert "_audio_preview_outputs_list" in ast.unparse(expression.right)


def test_the_handoff_joins_no_concurrency_group_and_takes_no_render_lock(gui_tree: ast.Module):
    """The measured S2-A R3 rule: on the pinned Gradio a full concurrency group leaves an event
    QUEUED rather than delivering it, so a second press would wait instead of running."""
    keywords = {kw.arg: ast.unparse(kw.value)
                for kw in _registration(gui_tree, "audio_open_preview_btn").keywords}
    assert keywords.get("concurrency_limit") == "None"
    assert "concurrency_id" not in keywords
    body = ast.unparse(_func(gui_tree, "_on_audio_open_in_library"))
    for forbidden in ("_RENDER_LOCK", "RENDER_CONCURRENCY_ID", "RenderLifecycle",
                      "process_video", "analyze_beats_auto", "create_music_video"):
        assert forbidden not in body, f"the handoff reaches {forbidden}"


def test_the_handoff_takes_no_injected_request(gui_tree: ast.Module):
    """It owns no per-owner page: the view it retires arrives as a `gr.State` value and goes back
    as one. An injected request here would imply a registry operation that does not happen."""
    func = _func(gui_tree, "_on_audio_open_in_library")
    assert [a.arg for a in func.args.args] == ["handoff"], [a.arg for a in func.args.args]


def test_no_registration_anywhere_still_points_at_a_removed_handler(gui_tree: ast.Module):
    """The failure mode this catches is a NameError in the operator's browser rather than in a
    test: `create_ui` is never executed by the suite, so an orphaned binding is invisible here
    unless something looks for it on purpose."""
    removed = {"_on_audio_confirm", "_on_audio_proceed", "_on_audio_cancel",
               "_on_audio_destinations_change"}
    for node in ast.walk(gui_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("click", "change", "select", "submit"):
            for kw in node.keywords:
                assert ast.unparse(kw.value) not in removed, \
                    f"{ast.unparse(node.func)} is still wired to {ast.unparse(kw.value)}"


# =============================================================================================
# Untouched neighbours
# =============================================================================================

def test_the_r14_inventory_panels_are_untouched(gui_tree: ast.Module, gui_source: str):
    """R14-A/R14-B shipped the Media Library inventory and its two default-off ticks. R15-C edits a
    different section of the same tab, so this is the regression control for that adjacency."""
    for widget in ("library_inventory_records", "library_inventory_sources"):
        node = next(n for n in ast.walk(gui_tree)
                    if isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name) and n.targets[0].id == widget)
        keywords = {kw.arg: ast.unparse(kw.value) for kw in node.value.keywords}
        assert keywords.get("value") == "False", f"{widget} no longer defaults to OFF"
    assert "F3_SAMPLE_MAX_PATHS" in gui_source, "the confirmed-missing sample constant is gone"


def test_the_startup_upload_protection_is_untouched(gui_tree: ast.Module, gui_source: str):
    """PR #100's protection: one pair of constants feeds both the retirement and the startup
    sweep's protected set, so `input/gradio_uploads` survives a restart. R15-C must not disturb it,
    and an audio-cutter change touching the startup sweep would be a real surprise.

    Read off the real `protected_dirs` ASSIGNMENT, not a source slice. The first attempt sliced from
    `gui_source.index("protected_dirs")` and landed inside the comment that *explains* the
    protection -- which is the prose-trips-a-substring-check trap this repository has paid for
    repeatedly, reproduced here by writing the guard the lazy way.
    """
    assert "RETIRED_UPLOADS_DIR_NAME" in gui_source
    assert "GRADIO_UPLOADS_DIR_NAME" in gui_source
    assignment = next(node for node in ast.walk(gui_tree)
                      if isinstance(node, ast.Assign) and len(node.targets) == 1
                      and isinstance(node.targets[0], ast.Name)
                      and node.targets[0].id == "protected_dirs")
    rendered = ast.unparse(assignment.value)
    for protected in ("RETIRED_UPLOADS_DIR_NAME", "GRADIO_UPLOADS_DIR_NAME",
                      "video_analysis_cache", "audio", "video"):
        assert protected in rendered, f"the startup sweep no longer protects {protected}"


#: The eleven UI strings that went with the removed surface. Eight were orphaned by R15-C itself;
#: the last three (`LABEL_AUDIO_PLAYER`, `LABEL_AUDIO_KEEP`, `LABEL_AUDIO_DISCARD`) were **already
#: dead before it** -- R7.1's single-clip player and its two buttons, superseded by the gallery in
#: R7.2 and never removed. They are listed here because they belong to the surface this increment
#: deletes, so leaving them would make them the only residue of it.
_REMOVED_UI_STRINGS = (
    "LABEL_AUDIO_REVIEW", "INFO_AUDIO_REVIEW", "LABEL_AUDIO_CLIP_NAME", "INFO_AUDIO_CLIP_NAME",
    "LABEL_AUDIO_FILE_REPORT", "AUDIO_FILE_REPORT_INITIAL_TEXT", "LABEL_AUDIO_LIBRARY",
    "LABEL_AUDIO_DISCARDED", "LABEL_AUDIO_PLAYER", "LABEL_AUDIO_KEEP", "LABEL_AUDIO_DISCARD",
)


@pytest.mark.parametrize("name", _REMOVED_UI_STRINGS)
def test_no_unrendered_ui_string_was_left_behind(name: str):
    """An unrendered UI string reads as a control that exists, and is how one concept ends up with
    two vocabularies the next time a review surface is added."""
    import ui_content

    assert not hasattr(ui_content, name), f"ui_content still defines the unrendered {name}"


def test_the_strings_audio_review_actually_reuses_were_kept(gui_source: str):
    """The positive half, and the reason the two destination LABELs went while their INFOs stayed.

    Audio Review has its own `LABEL_AUDIO_PREVIEW_*` labels but reuses the cutter's `INFO_` text for
    both destinations, so deleting those would have blanked two live descriptions.
    """
    import ui_content

    for kept in ("INFO_AUDIO_LIBRARY", "INFO_AUDIO_DISCARDED", "AUDIO_GALLERY_EMPTY_HTML",
                 "AUDIO_COUNTS_INITIAL_TEXT", "AUDIO_CONFIRM_DISABLED_TEXT",
                 "AUDIO_SWEEP_GATE_PROMPT", "AUDIO_SWEEP_CANCELLED_TEXT", "INFO_AUDIO_GALLERY",
                 "AUDIO_GALLERY_CSS", "LABEL_AUDIO_CONFIRM", "LABEL_AUDIO_PROCEED",
                 "LABEL_AUDIO_SWEEP_CANCEL", "LABEL_AUDIO_COUNTS", "LABEL_AUDIO_ENABLE_HOVER",
                 "LABEL_AUDIO_DESTINATION_STATUS", "AUDIO_DESTINATION_STATUS_INITIAL"):
        assert hasattr(ui_content, kept), f"ui_content lost {kept}, which Audio Review still uses"
        assert kept in gui_source, f"{kept} is defined but no longer rendered"


def test_the_handoff_texts_exist_and_name_the_right_surface():
    """R15-C's own two refusal strings, and they must not drift into Video Review's wording."""
    import ui_content

    assert "no finished cut to review yet" in ui_content.AUDIO_HANDOFF_NONE_TEXT
    assert "Audio Review" in ui_content.AUDIO_HANDOFF_NONE_TEXT
    assert "{folder}" in ui_content.AUDIO_HANDOFF_MISSING_TEXT
    assert "Nothing was changed." in ui_content.AUDIO_HANDOFF_MISSING_TEXT
    # Distinct from the Raw Cutter's pair, which says "job" where these say "cut".
    assert ui_content.AUDIO_HANDOFF_NONE_TEXT != ui_content.CUT_HANDOFF_NONE_TEXT
    assert ui_content.AUDIO_HANDOFF_MISSING_TEXT != ui_content.CUT_HANDOFF_MISSING_TEXT


def test_video_reviews_own_handoff_is_unchanged(gui_tree: ast.Module):
    """The Raw Cutter's `Review these clips` shares the record type R15-C reuses, so the shared
    change has to leave it exactly as it was -- including its default destination wording."""
    body = ast.unparse(_func(gui_tree, "_on_cut_open_in_library"))
    assert "fork_handoff.loaded_text(record)" in body, (
        "the video handoff must keep calling loaded_text with no destination, so R11's wording and "
        "its pinned tests are preserved byte for byte")
    assert "_cut_handoff_retire_review" in body
    assert handoff.loaded_text(_record("C:/x")).count("Video Review") == 1
