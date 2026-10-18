"""[FORK] Digital-Union (Raw Cutter progress P1): the live progress surface.

Two tiers, for the reason `.claude/rules/test-harness.md` describes. `_cut_progress_html` is pure
and stdlib-only, so it is AST-extracted and EXECUTED -- these are real assertions about the real
renderer, not mocks. Everything that depends on Gradio is asserted structurally against the real
`gui.py` tree.

The property this file exists to defend: **the Raw Cutter never shows a number it did not
measure.** The only determinate fraction it can honestly produce is published clips over planned
clips; everything else is a named phase with an indeterminate bar. A percentage that moved on a
timer, or an ETA of any kind, would be a lie told with a progress bar.
"""
from __future__ import annotations

import ast
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


@pytest.fixture(scope="module")
def gui_tree() -> ast.Module:
    return _tree(_GUI)


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _code_only(node) -> str:
    body = [n for n in node.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    return "\n".join(ast.unparse(n) for n in body)


def _click(tree: ast.Module, widget: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "click"
                and getattr(node.func.value, "id", None) == widget):
            return node
    raise AssertionError(f"{widget}.click(...) not found")


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node) -> list[str]:
    return [getattr(e, "id", ast.unparse(e)) for e in node.elts]


@pytest.fixture(scope="module")
def renderer():
    """The REAL `_cut_progress_html`, executed against a minimal synthesised parent.

    It depends only on `html` and the module-level `_CUT_TONES`, both of which are reproduced
    here from the real source rather than stubbed, so what runs is the shipped code.
    """
    import html as html_lib

    gui = _tree(_GUI)
    namespace: dict = {"html_lib": html_lib}
    for node in gui.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_CUT_TONES":
            exec(compile(ast.Module(body=[node], type_ignores=[]), _GUI, "exec"), namespace)
    for name in ("_cut_progress_html", "_cut_progress_clear"):
        fn = _func(gui, name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), _GUI, "exec"), namespace)
    assert "_cut_progress_html" in namespace
    return namespace


# --------------------------------------------------------------------------------------------
# Tier 1 -- the real renderer, executed.
# --------------------------------------------------------------------------------------------

def test_a_measured_fraction_renders_the_exact_percentage(renderer):
    """`done / total` and nothing else. 3 of 12 is 25%, not "about a quarter" and not a guess."""
    html = renderer["_cut_progress_html"]("Cutting", "clip 3", done=3, total=12)
    assert 'data-determinate="1"' in html
    assert 'data-pct="25"' in html
    assert 'data-done="3"' in html and 'data-total="12"' in html
    assert "width:25%" in html
    assert "3 / 12" in html


def test_an_unmeasurable_phase_never_invents_a_number(renderer):
    """Scene detection reports nothing usable, so the bar must say "unknown", not "0%".

    `data-pct="-1"` rather than 0 is deliberate: a zero would render as a stalled job, which is a
    different and equally false claim.
    """
    html = renderer["_cut_progress_html"]("Analyzing", "Detecting scene changes…")
    assert 'data-determinate="0"' in html
    assert 'data-pct="-1"' in html
    assert 'data-pct="0"' not in html
    assert 'data-done=""' in html and 'data-total=""' in html


def test_the_bar_is_clamped_and_never_reports_more_than_was_planned(renderer):
    """A miscount must not render 140%. The honest ceiling is the plan."""
    assert 'data-pct="100"' in renderer["_cut_progress_html"]("Cutting", "", done=14, total=10)
    assert 'data-pct="0"' in renderer["_cut_progress_html"]("Cutting", "", done=-3, total=10)


def test_a_zero_clip_plan_is_indeterminate_rather_than_a_division(renderer):
    html = renderer["_cut_progress_html"]("Cutting", "", done=0, total=0)
    assert 'data-determinate="0"' in html


def test_the_panel_escapes_whatever_a_filename_contains(renderer):
    """Clip names reach this panel. A source called `<img onerror=...>` must not become markup."""
    html = renderer["_cut_progress_html"]("Cutting", '<script>x</script>', done=1, total=2)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_idle_panel_is_empty(renderer):
    """Nothing on screen may imply work when none is running."""
    assert renderer["_cut_progress_clear"]() == ""


def test_every_tone_is_rendered_and_distinguishable(renderer):
    seen = {renderer["_cut_progress_html"]("t", "", tone=tone).split('data-state="')[1][:8]
            for tone in ("active", "done", "stopping", "failed")}
    assert len(seen) == 4


# --------------------------------------------------------------------------------------------
# Tier 2 -- structural assertions against the real gui.py.
# --------------------------------------------------------------------------------------------

def test_no_eta_is_ever_computed_or_shown(gui_source):
    """There is no honest ETA here: segment lengths vary and the encoder is not linear in
    wall-clock. The way to keep it honest is to never build one."""
    import re as _re
    cutter = gui_source[gui_source.index("def _cut_progress_html"):
                        gui_source.index("def _on_cut_mode_change")].lower()
    # Word boundaries, not substrings: a bare `in` match for "eta" also fires on "detail", which
    # is how a guard like this ends up asserting nothing useful or failing for the wrong reason.
    for forbidden in ("eta", "etas", "time_remaining", "remaining_seconds", "estimated",
                      "time_left", "finishes_at"):
        assert not _re.search(rf"\b{forbidden}\b", cutter), forbidden


def test_the_bar_only_advances_on_a_published_clip(gui_tree):
    """`on_progress` announces a clip STARTING; only `on_complete` means one was written.

    Counting starts would show 12/12 while the last clip was still encoding.
    """
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    # The count is recomputed from `completed`, which only `on_complete` appends to.
    assert "published = sum(" in body and "for r in completed if r.ok" in body
    # `on_progress` -> `started` must not touch the published count at all.
    started = body.index("def started")
    assert "published" not in body[started:body.index("def finished")]
    # And `on_complete` -> `finished` is what records the clip.
    finished = body[body.index("def finished"):body.index("def run_export")]
    assert "completed.append(result)" in finished


def test_analyze_disables_both_actions_and_never_arms_cancel(gui_tree):
    """Analyze installs no CutterJob and holds no mutex, so Cancel cannot stop it. Presenting
    Cancel as able to would be a lie, and pressing it would answer "no cut job is running"."""
    body = _code_only(_func(gui_tree, "_on_cut_analyze"))
    assert "_cut_buttons(analyze=False, run=False, cancel=False)" in body
    assert "cancel=True" not in body


def test_cut_arms_cancel_while_working_and_disarms_it_afterwards(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    assert "_cut_buttons(analyze=False, run=False, cancel=True)" in body
    assert body.count("cancel=False") >= 2, "every terminal path must return Cancel to idle"


def test_cut_video_is_only_offered_for_a_plan_that_exports_something(gui_tree):
    """The button and the server-side refusal must use the SAME predicate, or they disagree."""
    analyze = _code_only(_func(gui_tree, "_on_cut_analyze"))
    assert "if plan.has_work:" in analyze
    run = _code_only(_func(gui_tree, "_on_cut_run"))
    assert "if not plan.has_work:" in run


def test_cancel_reports_requested_and_never_claims_completion(gui_tree):
    """The job stops at the next cooperative checkpoint; only the running generator knows when
    it actually stopped."""
    body = _code_only(_func(gui_tree, "_on_cut_cancel"))
    assert "CUT_CANCEL_REQUESTED_TEXT" in body
    assert "CUT_CANCELLED_TEXT" not in body


def test_the_worker_is_reaped_before_the_mutex_is_handed_back(gui_tree):
    """The reviewed safety requirement. The export now runs on a worker, so releasing admission
    while that worker is still writing would let a second cut start on top of the first -- which
    is exactly what `_RENDER_LOCK` exists to prevent. An abandoned Gradio stream closes the
    generator under `GeneratorExit`, and this `finally` is the only thing standing there.
    """
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    releasing = [n for n in finallies
                 if any("_RENDER_LOCK.release()" in ast.unparse(s) for s in n.finalbody)]
    assert len(releasing) == 1
    final = [ast.unparse(s) for s in releasing[0].finalbody]
    joined = next(i for i, s in enumerate(final) if "worker.join()" in s)
    released = next(i for i, s in enumerate(final) if "_RENDER_LOCK.release()" in s)
    assert joined < released, final
    assert any("job.cancel()" in s for s in final), "the worker must be asked to stop, not killed"


def test_a_planning_change_disables_cut_without_destroying_the_plan(gui_tree):
    """Presentation only. The backend staleness gate is still the authority, and the plan is left
    alone so reverting the setting is cheap rather than a re-analysis."""
    body = _code_only(_func(gui_tree, "_on_cut_planning_input_change"))
    assert "interactive=False" in body
    assert "cut_plan_state" not in body
    run = _code_only(_func(gui_tree, "_on_cut_run"))
    assert "plan_signature" in run and "CUT_STALE_PLAN_TEXT" in run


def test_the_progress_components_exist_and_are_cutter_scoped(gui_source):
    assert "elem_id='cut-progress'" in gui_source
    assert "elem_id='cut-batch-progress'" in gui_source


def test_both_cancel_buttons_ship_inactive(gui_source):
    """A permanently red emergency control that does nothing teaches an operator to ignore it."""
    for marker in ("elem_id='cut-cancel-button'", "elem_id='cut-batch-cancel-button'"):
        start = gui_source.index(marker)
        assert "interactive=False" in gui_source[start - 240:start + 80], marker


def test_cut_video_ships_unavailable(gui_source):
    start = gui_source.index("elem_id='cut-run-button'")
    assert "interactive=False" in gui_source[start - 200:start + 60]


def test_the_batch_sink_is_written_but_never_read_by_the_batch(gui_tree):
    """`sink` is an output-only channel for presentation. If the batch ever branched on it, a UI
    concern would have become a behavioural one."""
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "sink.update(" in body
    assert "sink['terminal']" in body or 'sink["terminal"]' in body
    assert "sink.get(" not in body, "the batch must not read its own progress sink"


def test_the_batch_terminal_class_is_structural_not_parsed_from_prose(gui_tree):
    """Deriving an outcome class from a status string is forbidden project-wide, and for a good
    reason: prose is not a contract, so a reworded message would silently change the verdict."""
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    assert "sink.get('terminal')" in wrapper
    for forbidden in ("startswith", "endswith", "in status", "status =="):
        assert forbidden not in wrapper, forbidden


def test_the_progress_panel_never_writes_outside_the_raw_cutter(gui_tree):
    """Every P1 output must be a Raw Cutter widget. One stray render-state write here would make
    a progress update capable of disturbing a render."""
    allowed = {"cut_summary", "cut_table", "cut_plan_state", "cut_source_info", "cut_progress",
               "cut_report", "cut_analyze_btn", "cut_run_btn", "cut_cancel_btn",
               "cut_batch_status", "cut_batch_summary", "cut_batch_progress",
               "cut_batch_count_btn", "cut_batch_start_btn", "cut_batch_cancel_btn"}
    for widget in ("cut_analyze_btn", "cut_run_btn", "cut_cancel_btn",
                   "cut_batch_start_btn", "cut_batch_cancel_btn"):
        written = set(_names(_kwargs(_click(gui_tree, widget))["outputs"]))
        assert written <= allowed, (widget, written - allowed)


# --------------------------------------------------------------------------------------------
# [FORK] Digital-Union (P19-R4): the Cancel controls must actually be RED.
#
# The independent P19-R3 review measured `variant='stop'` rendering as a neutral grey gradient in
# the pinned Gradio 6.19.0 + Ocean theme, in BOTH states -- so the only difference between an
# armed emergency control and a dead one was opacity. These assertions pin the shipped stylesheet
# against the DOM that was actually measured; the computed-style proof is browser evidence, which
# a bare-interpreter suite cannot produce and must not pretend to.
# --------------------------------------------------------------------------------------------

def _cut_cancel_css(gui_source: str) -> str:
    start = gui_source.index("CUT_CANCEL_CSS = ")
    return gui_source[start:gui_source.index('"""', gui_source.index('"""', start) + 3) + 3]


def test_the_cancel_controls_are_styled_red_by_id(gui_source):
    """Both controls, selected by id. `variant='stop'` is not a promise of red and was measured
    not to be one."""
    css = _cut_cancel_css(gui_source)
    for marker in ("button#cut-cancel-button", "button#cut-batch-cancel-button"):
        assert marker in css, marker
    # The theme paints with a gradient over a transparent background-color, so overriding only
    # the colour would be invisible. This is the measured fact the rule depends on.
    assert "background-image:" in css
    assert css.count("background-image:") >= 2, "the enabled and disabled states both need one"


def test_the_cancel_styling_is_scoped_to_the_cutter_and_cannot_reach_quick_render(gui_source):
    """Quick Render's `Cancel Active Render` carries the SAME `stop` class and has no `elem_id`.
    A class-based selector would have restyled a render control this change must not touch.

    Checked by parsing the selectors rather than by banning substrings: `button {` occurs inside
    `button#cut-batch-cancel-button {`, so a substring ban here asserts the wrong thing and fails
    for the wrong reason.
    """
    css = _cut_cancel_css(gui_source)
    body = css[css.index('"""') + 3:css.rindex('"""')]
    selectors = []
    for block in body.split("}"):
        head = block.split("{")[0].strip()
        if head:
            selectors.extend(part.strip() for part in head.split(",") if part.strip())
    assert selectors, "no selectors found"
    for selector in selectors:
        assert selector.startswith("button#"), f"not id-scoped: {selector!r}"
        assert ("#cut-cancel-button" in selector
                or "#cut-batch-cancel-button" in selector), selector
    # Exactly the two cutter controls, and nothing else in the application.
    targeted = {s.split(":")[0] for s in selectors}
    assert targeted == {"button#cut-cancel-button", "button#cut-batch-cancel-button"}, targeted


def test_the_disabled_cancel_is_subdued_and_the_enabled_one_is_not(gui_source):
    """Idle must not look armed. The disabled rule drops the gradient and goes translucent, on
    top of the opacity and grayscale the theme already applies."""
    css = _cut_cancel_css(gui_source)
    assert ":disabled" in css
    disabled = css[css.index(":disabled"):]
    assert "background-image: none" in disabled
    assert "cursor: not-allowed" in disabled


def test_the_cancel_controls_keep_a_visible_hover_and_focus_affordance(gui_source):
    """Colour is never the only indicator, and the control must stay keyboard-reachable."""
    css = _cut_cancel_css(gui_source)
    assert ":not(:disabled):hover" in css, "hover must not light up a disabled control"
    assert ":focus-visible" in css
    assert "outline:" in css


def test_the_cancel_styling_is_actually_applied_to_the_app(gui_source):
    """A stylesheet nobody passes to `gr.Blocks` is decoration in a source file."""
    assert "css=STATUS_BOX_CSS + CUT_CANCEL_CSS" in gui_source
    # And the pre-existing status-box rules are still there rather than replaced.
    assert "STATUS_BOX_CSS = " in gui_source and "#status-output-box" in gui_source


# --------------------------------------------------------------------------------------------
# [FORK] Digital-Union (P19-R4): the batch terminal class is the batch module's own vocabulary.
# --------------------------------------------------------------------------------------------

def test_the_batch_terminal_class_comes_from_the_pure_classifier(gui_tree):
    """F-1/F-2 both came from the presentation inventing its own verdict. The wrapper now renders
    `terminal_status`, and the batch publishes it -- one classifier, two renderings."""
    core = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "fork_cutter_batch.terminal_status(" in core, \
        "the batch must publish the pure classifier's answer, not its own expression"
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    for member in ("TERMINAL_COMPLETED", "TERMINAL_COMPLETED_WITH_FAILURES",
                   "TERMINAL_CANCELLED", "TERMINAL_FAILED"):
        assert f"fork_cutter_batch.{member}" in wrapper, member
    # Still never parsed from prose.
    for forbidden in ("startswith", "endswith", "in status", "status =="):
        assert forbidden not in wrapper, forbidden


def test_the_batch_progress_count_is_folded_from_recorded_outcomes(gui_tree):
    """The F-1 root cause, pinned: the count must come from the same fold the manifest uses, and
    must no longer be read off the raw `items` length inside the progress callback."""
    core = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "fork_cutter_batch.summarise(items, total)" in core
    assert "finished=totals.attempted" in core
    assert "finished=len(items)" not in core, "the stale snapshot is back"


def test_the_batch_publishes_its_count_when_an_outcome_is_recorded(gui_tree):
    """Counting only at `progress(...)` is what dropped the final source. `record(...)` must
    publish too, because the last source has no further progress call."""
    core = _func(gui_tree, "_on_cut_batch_run")
    record = _func(core, "record")
    assert "publish()" in _code_only(record), \
        "the recording boundary must update the progress sink"


def test_a_batch_that_never_started_publishes_no_terminal_class(gui_tree):
    """F-2: absence of a class is the structural statement "there was no terminal state", and it
    is what routes both refusal shapes to the same honest panel."""
    core = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "started = bool(batch_directory)" in core
    assert "sink.pop('terminal', None)" in core
