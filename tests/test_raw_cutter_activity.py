"""[FORK] Digital-Union (Raw Cutter R12): the Activity & Performance read-out.

Two tiers, for the reason `.claude/rules/test-harness.md` describes.

`beatsync_fork.raw_cutter_activity` is stdlib-only and clock-injected, so tier 1 **imports and
executes it** — including every timer rule, which is testable precisely because no reading comes
from a real clock. Tier 2 asserts structurally against the real `gui.py`, where the panel is wired
into the three handlers and the one-second browser tick is installed.

The properties this file defends, each of which is a way the panel could have lied:

* **A number on screen was measured.** The read-out reports elapsed time and facts the handler
  knew. It computes no percentage, no rate and no prediction of when anything will finish.
* **An idle job shows no running timer.** A refusal performed no work, so it carries no panel.
* **A terminal panel is frozen by the server**, not left to the browser to guess.
* **The phase timer resets only on a real phase change** — never on a Gradio re-render, and never
  three times per source because probe, detection and planning are separate function calls.
* **Qwen is reported as not used rather than as offline**, and **GPU telemetry is reported as
  unavailable rather than invented.**
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from beatsync_fork import raw_cutter_activity as activity
from beatsync_fork import raw_cutter_batch as batch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_MODULE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "raw_cutter_activity.py")


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


def _attr(markup: str, name: str) -> str:
    found = re.search(rf'{name}="([^"]*)"', markup)
    assert found, f"{name} missing from {markup[:200]}"
    return found.group(1)


# --------------------------------------------------------------------------------------------
# Tier 1 -- the real module, imported and executed.
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ms,shown", [
    (0, "00:00"),
    (999, "00:00"),               # truncates: a second that has not finished has not elapsed
    (1000, "00:01"),
    (154_000, "02:34"),
    (3_599_000, "59:59"),
    (3_600_000, "1:00:00"),       # widens only once it has to
    (3_754_000, "1:02:34"),
    (-5_000, "00:00"),            # never below zero, whatever the caller passes
])
def test_elapsed_is_formatted_by_truncation_and_never_goes_negative(ms, shown):
    assert activity.format_elapsed(ms) == shown


def test_the_job_timer_measures_from_the_clock_start():
    clock = activity.ActivityClock(1000.0)
    assert clock.snapshot(1000.0).job_ms == 0
    assert clock.snapshot(1154.5).job_ms == 154_500


def test_a_reading_before_the_start_cannot_produce_a_negative_elapsed():
    """A monotonic source makes this impossible in the app; the record still refuses to carry it,
    because a negative duration would reach the browser as an anchor in the future."""
    clock = activity.ActivityClock(1000.0)
    assert clock.snapshot(900.0).job_ms == 0
    assert clock.snapshot(900.0).phase_ms == 0


def test_the_phase_timer_resets_only_when_the_phase_actually_changes():
    """The property that makes the phase figure worth reading. Re-declaring the same phase -- which
    is what every heartbeat frame does -- must not restart it."""
    clock = activity.ActivityClock(100.0)
    clock.enter(activity.PHASE_DETECTING, 100.0)
    assert clock.snapshot(130.0).phase_ms == 30_000
    clock.enter(activity.PHASE_DETECTING, 130.0)          # same phase, a later frame
    assert clock.snapshot(160.0).phase_ms == 60_000       # still counting from 100.0
    clock.enter(activity.PHASE_PLANNING, 160.0)           # a genuine change
    assert clock.snapshot(161.0).phase_ms == 1_000


def test_the_phase_token_changes_exactly_once_per_real_phase_change():
    """The browser anchors on the token, so a token that moved without the phase moving would
    make the display jump on every server frame."""
    clock = activity.ActivityClock(10.0)
    clock.enter(activity.PHASE_PREPARING, 10.0)
    first = clock.snapshot(11.0).phase_token
    clock.enter(activity.PHASE_PREPARING, 11.0)
    assert clock.snapshot(12.0).phase_token == first
    clock.enter(activity.PHASE_CUTTING, 12.0)
    assert clock.snapshot(13.0).phase_token != first


def test_the_batch_key_restarts_the_phase_timer_per_source():
    """`Cutting current source` recurs on every file. Keyed by (position, phase) it answers how
    long THIS source has been encoding; keyed by the label alone it would run across the batch."""
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_CUTTING_SOURCE, 0.0, "1:exporting")
    assert clock.snapshot(50.0).phase_ms == 50_000
    clock.enter(activity.PHASE_CUTTING_SOURCE, 50.0, "2:exporting")
    assert clock.snapshot(51.0).phase_ms == 1_000
    assert clock.snapshot(51.0).phase == activity.PHASE_CUTTING_SOURCE


def test_the_job_token_is_stable_for_a_job_and_different_between_jobs():
    """Derived from the start reading rather than minted from a clock or a random number, which is
    what keeps this module deterministic -- and two jobs cannot begin in the same millisecond."""
    clock = activity.ActivityClock(1234.5)
    assert clock.snapshot(1300.0).job_token == clock.snapshot(1400.0).job_token
    assert activity.ActivityClock(1234.6).token != clock.token


def test_a_display_phase_override_does_not_disturb_the_phase_timer():
    """A terminal frame relabels the phase `Completed` while reporting how long the last REAL
    phase took. Restarting the timer there would answer a question nobody asked."""
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_CUTTING, 0.0)
    final = clock.snapshot(90.0, status=activity.STATUS_DONE, running=False,
                           phase=activity.PHASE_COMPLETED)
    assert final.phase == activity.PHASE_COMPLETED
    assert final.phase_ms == 90_000
    assert clock.phase == activity.PHASE_CUTTING


def test_the_panel_publishes_the_whole_data_contract():
    """`gui.CUT_ACTIVITY_JS` and the browser tests read these; nothing has to parse presentation."""
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_DETECTING, 0.0)
    markup = clock.render(12.0, source="rec.mp4", ffmpeg=True)
    assert 'class="bs-cut-activity"' in markup
    assert _attr(markup, "data-running") == "1"
    assert _attr(markup, "data-job-ms") == "12000"
    assert _attr(markup, "data-phase-ms") == "12000"
    assert _attr(markup, "data-phase") == activity.PHASE_DETECTING
    assert _attr(markup, "data-status") == activity.STATUS_WORKING
    assert _attr(markup, "data-ffmpeg") == "active"
    assert _attr(markup, "data-gpu") == "unavailable"
    assert _attr(markup, "data-job-token") == clock.token
    assert _attr(markup, "data-phase-token").startswith(clock.token)


def test_the_elapsed_figures_are_also_written_as_text_server_side():
    """Progressive enhancement, deliberately: with the interval blocked or not yet installed the
    panel is still correct as of the moment it was emitted, rather than blank."""
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_CUTTING, 0.0)
    markup = clock.render(154.0)
    assert 'class="bs-cut-job-elapsed"' in markup
    assert 'class="bs-cut-phase-elapsed"' in markup
    assert markup.count("02:34") == 2


def test_a_terminal_panel_is_frozen_by_the_server():
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_CUTTING, 0.0)
    markup = clock.render(61.0, status=activity.STATUS_DONE, running=False,
                          phase=activity.PHASE_COMPLETED)
    assert _attr(markup, "data-running") == "0"
    assert _attr(markup, "data-status") == activity.STATUS_DONE
    assert "01:01" in markup


def test_the_panel_escapes_whatever_a_filename_contains():
    clock = activity.ActivityClock(0.0)
    markup = clock.render(1.0, source='<script>alert("x")</script>.mp4')
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_an_unknown_count_renders_no_row_rather_than_a_confident_zero():
    """`None` is not zero. `0 / 0` would be a measurement the handler never made."""
    clock = activity.ActivityClock(0.0)
    assert "Clip" not in clock.render(1.0)
    assert "Clip" in clock.render(1.0, clip_done=3, clip_total=12)
    assert "3 / 12" in clock.render(1.0, clip_done=3, clip_total=12)


def test_ffmpeg_is_reported_from_what_the_caller_knows_and_otherwise_unknown():
    clock = activity.ActivityClock(0.0)
    assert _attr(clock.render(1.0), "data-ffmpeg") == "unknown"
    assert _attr(clock.render(1.0, ffmpeg=True), "data-ffmpeg") == "active"
    assert _attr(clock.render(1.0, ffmpeg=False), "data-ffmpeg") == "idle"
    assert "Active" in clock.render(1.0, ffmpeg=True)
    assert "Idle" in clock.render(1.0, ffmpeg=False)


def test_qwen_is_not_used_rather_than_offline():
    """A red Offline indicator would report a component the Raw Cutter never asks for as a fault."""
    assert activity.QWEN_TEXT == "Not used in Raw Cutter"
    markup = activity.ActivityClock(0.0).render(1.0)
    assert activity.QWEN_TEXT in markup
    for misleading in ("Offline", "offline", "Unavailable GPU", "Error"):
        assert misleading not in markup.replace(activity.GPU_TEXT, "")


def test_gpu_usage_is_reported_unavailable_and_never_estimated():
    """Nothing lightweight in this process measures utilisation or VRAM, so the panel says so.
    The encoder is a separate, known fact and is still reported."""
    assert activity.GPU_TEXT == "Unavailable"
    markup = activity.ActivityClock(0.0).render(1.0, encoder=activity.encoder_label("h264_nvenc"))
    assert "GPU usage" in markup and activity.GPU_TEXT in markup
    # No percentage anywhere an operator can read one. Checked on the VISIBLE text rather than on
    # the markup, because `border-radius:50%` in the status dot is not a measurement.
    assert "%" not in re.sub(r"<[^>]*>", " ", markup)
    assert "NVENC H.264 (GPU)" in markup


def test_the_encoder_label_is_recognisable_and_an_unknown_one_is_passed_through():
    assert activity.encoder_label("h264_nvenc") == "NVENC H.264 (GPU)"
    assert activity.encoder_label("libx264") == "libx264 (CPU)"
    assert activity.encoder_label("hevc_qsv") == "hevc_qsv"
    assert activity.encoder_label(None) == ""
    assert activity.encoder_label("") == ""


def test_the_batch_phase_vocabulary_is_the_batch_modules_own():
    """Restating these strings here would create a second vocabulary able to drift from the one
    the batch actually publishes. So the table is keyed by the real constants, and every published
    phase has a label."""
    published = {batch.PHASE_PROBE, batch.PHASE_DETECT, batch.PHASE_PLAN, batch.PHASE_EXPORT,
                 batch.PHASE_MANIFEST}
    assert set(activity.BATCH_PHASE_LABELS) == published
    assert set(activity.BATCH_FFMPEG_ACTIVE) == published


def test_probe_detect_and_plan_collapse_into_one_analysis_phase():
    """They are one operation from the operator's side, the progress line beneath already names
    which of the three, and three resets per source would make the phase timer useless on exactly
    the files that are slow."""
    for phase in (batch.PHASE_PROBE, batch.PHASE_DETECT, batch.PHASE_PLAN):
        assert activity.batch_phase_label(phase) == activity.PHASE_ANALYZING_SOURCE
    assert activity.batch_phase_label(batch.PHASE_EXPORT) == activity.PHASE_CUTTING_SOURCE
    assert activity.batch_phase_label(batch.PHASE_MANIFEST) == activity.PHASE_WRITING_MANIFEST


def test_an_absent_batch_phase_is_the_discovery_frame_and_claims_no_ffmpeg():
    """Before the first source is opened nothing has been started. Claiming Active there, as a
    naive "a job is running" reading would, is the guess this panel exists to avoid."""
    assert activity.batch_phase_label("") == activity.PHASE_DISCOVERING
    assert activity.batch_phase_label(None) == activity.PHASE_DISCOVERING
    assert activity.batch_ffmpeg_active("") is False
    assert activity.batch_ffmpeg_active(None) is False
    assert activity.batch_ffmpeg_active("something else entirely") is False


def test_ffmpeg_activity_is_stated_from_what_each_batch_phase_really_runs():
    """Probe runs FFprobe, detection and export run FFmpeg, planning is arithmetic and the record
    is a JSON write."""
    assert activity.batch_ffmpeg_active(batch.PHASE_PROBE) is True
    assert activity.batch_ffmpeg_active(batch.PHASE_DETECT) is True
    assert activity.batch_ffmpeg_active(batch.PHASE_PLAN) is False
    assert activity.batch_ffmpeg_active(batch.PHASE_EXPORT) is True
    assert activity.batch_ffmpeg_active(batch.PHASE_MANIFEST) is False


def test_a_did_not_run_panel_is_not_spelled_as_a_cancellation():
    """A refused batch stopped nothing, so it must not report that the operator did."""
    assert activity.PHASE_NOT_RUN != activity.PHASE_CANCELLED
    assert "ancel" not in activity.PHASE_NOT_RUN


def test_the_snapshot_record_is_frozen():
    snapshot = activity.ActivityClock(0.0).snapshot(1.0)
    with pytest.raises(Exception):
        snapshot.phase = "something else"


def test_the_module_measures_nothing_and_predicts_nothing():
    """It reads no clock, starts no child and touches no disk -- which is what makes every rule
    above testable on a bare interpreter. And there is no prediction vocabulary anywhere in it:
    segment lengths vary and the encoder is not linear in wall-clock, so any such figure would be
    invented."""
    tree = _tree(_MODULE)
    imported = {alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {(node.module or "").split(".")[0]
                 for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"html", "dataclasses", "typing", "", "__future__"}, imported
    # The import allowlist already makes a clock read, a child process and a file open
    # unreachable. Asserted on the call targets as well, over the AST rather than over the text,
    # so prose describing what the module must not do cannot trip its own guard.
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for target in called:
        assert not target.startswith(("time.", "os.", "subprocess", "open")), target
        assert target not in ("open", "eval", "exec", "__import__"), target
    # And no prediction vocabulary in the executable code. Taken from the unparsed AST with
    # docstrings removed: the comments and docstrings above legitimately NAME these words in
    # order to forbid them, which is how a text-matching guard ends up failing on its own policy.
    code = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            continue
        code.append(ast.unparse(node))
    lowered = "\n".join(code).lower()
    for forbidden in ("eta", "estimated", "time_left", "remaining_seconds", "percent",
                      "finishes_at"):
        assert not re.search(rf"\b{forbidden}\b", lowered), forbidden


# --------------------------------------------------------------------------------------------
# Tier 2 -- the real gui.py wiring, asserted structurally.
# --------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def renderer():
    """The REAL `_cut_progress_html` plus the two R12 helpers, executed against a minimal parent.

    `_cut_progress_html` still depends only on `html` and `_CUT_TONES`, which is the whole reason
    `activity` crosses into it as an already-rendered fragment rather than as a record: the panel
    renderer stays dependency-free and this harness stays honest.
    """
    import html as html_lib
    import time as real_time

    gui = _tree(_GUI)
    namespace: dict = {"html_lib": html_lib, "time": real_time, "fork_activity": activity}
    for node in gui.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "_CUT_TONES":
            exec(compile(ast.Module(body=[node], type_ignores=[]), _GUI, "exec"), namespace)
    for name in ("_cut_progress_html", "_cut_activity", "_cut_activity_final"):
        fn = _func(gui, name)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), _GUI, "exec"), namespace)
    return namespace


def test_the_panel_without_an_activity_fragment_is_the_pre_r12_panel(renderer):
    """Every idle, refused and stale path omits it, so those surfaces must be untouched."""
    markup = renderer["_cut_progress_html"]("Cutting", "clip 3", done=3, total=12)
    assert "bs-cut-activity" not in markup
    assert markup.endswith("</div>")
    assert 'data-pct="25"' in markup


def test_the_activity_fragment_lands_inside_the_panel_below_the_bar(renderer):
    """"Immediately below the existing progress bar" is structural here: the fragment is the last
    child of the same container, after the bar, the detail and the note."""
    markup = renderer["_cut_progress_html"]("Cutting", "clip 3", done=3, total=12,
                                            note="note", activity="<i>ACT</i>")
    assert markup.index("cut-progress-bar") < markup.index("ACT")
    assert markup.index("cut-progress-detail") < markup.index("ACT")
    assert markup.index("cut-progress-note") < markup.index("ACT")
    assert markup.endswith("ACT</i></div>")


def test_the_gui_helper_declares_the_phase_before_rendering_it(renderer):
    """One reading serves the phase change and the render, so the two cannot disagree -- and the
    phase on screen is the one about to execute rather than the one that just finished."""
    clock = activity.ActivityClock(0.0)
    first = renderer["_cut_activity"](clock, activity.PHASE_DETECTING, source="a.mp4", ffmpeg=True)
    assert _attr(first, "data-phase") == activity.PHASE_DETECTING
    assert _attr(first, "data-running") == "1"
    again = renderer["_cut_activity"](clock, activity.PHASE_DETECTING)
    assert _attr(again, "data-phase-token") == _attr(first, "data-phase-token")
    moved = renderer["_cut_activity"](clock, activity.PHASE_PLANNING)
    assert _attr(moved, "data-phase-token") != _attr(first, "data-phase-token")


def test_the_gui_terminal_helper_freezes_without_entering_a_new_phase(renderer):
    clock = activity.ActivityClock(0.0)
    renderer["_cut_activity"](clock, activity.PHASE_CUTTING)
    before = clock.phase_index
    final = renderer["_cut_activity_final"](clock, activity.PHASE_COMPLETED,
                                            activity.STATUS_DONE, ffmpeg=False)
    assert _attr(final, "data-running") == "0"
    assert _attr(final, "data-phase") == activity.PHASE_COMPLETED
    assert clock.phase_index == before, "a terminal frame must not restart the phase timer"


def test_both_handlers_start_one_clock_and_pass_it_to_every_frame(gui_tree):
    for name in ("_on_cut_analyze", "_on_cut_run", "_on_cut_batch_run_ui"):
        body = _code_only(_func(gui_tree, name))
        assert body.count("fork_activity.ActivityClock(time.perf_counter())") == 1, name
        assert "activity=_cut_activity(" in body, name


def test_every_terminal_frame_freezes_the_timers(gui_tree):
    """A running timer left on a finished job is the failure this read-out exists to prevent."""
    for name, expected in (("_on_cut_analyze", 3), ("_on_cut_run", 3),
                           ("_on_cut_batch_run_ui", 1)):
        body = _code_only(_func(gui_tree, name))
        assert body.count("_cut_activity_final(") == expected, (name, body.count(
            "_cut_activity_final("))


def test_a_refusal_carries_no_activity_panel_at_all(gui_tree):
    """`refuse` performed no work: no mutex, no directory, no clip. A timer there would be
    counting a job that never began."""
    run = _func(gui_tree, "_on_cut_run")
    refuse = _func(run, "refuse")
    assert "activity" not in ast.unparse(refuse)
    # And the idle panel is still simply empty.
    assert _code_only(_func(gui_tree, "_cut_progress_clear")).strip() == "return ''"


def test_the_single_video_phases_are_the_real_cutter_operations(gui_tree):
    """Probe, detect, plan, cut, finalize -- each announced before its own blocking step."""
    analyze = _code_only(_func(gui_tree, "_on_cut_analyze"))
    for phase in ("PHASE_PREPARING", "PHASE_DETECTING", "PHASE_PLANNING"):
        assert f"fork_activity.{phase}" in analyze, phase
    run = _code_only(_func(gui_tree, "_on_cut_run"))
    for phase in ("PHASE_PREPARING", "PHASE_CUTTING"):
        assert f"fork_activity.{phase}" in run, phase


def test_there_is_no_single_video_finalizing_phase():
    """Measured, not assumed: an announced `Finalizing` frame took 1 ms in the browser, and it cost
    the terminal panel its only useful phase figure -- a finished job reported `Phase elapsed
    00:00` for that millisecond rather than how long the cutting had taken. A phase label that
    displaces a measured figure with an unmeasurable one is the same defect as a percentage for an
    opaque phase, so the label does not exist. The batch's equivalent step IS published, because
    the batch already emits a frame while it happens."""
    assert not hasattr(activity, "PHASE_FINALIZING")
    assert activity.PHASE_WRITING_MANIFEST in activity.BATCH_PHASE_LABELS.values()


def test_the_batch_phase_and_source_come_from_the_sink_it_already_publishes(gui_tree):
    """No second progress channel, no second bookkeeping, and the phase key carries the position
    so the phase timer is per source."""
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    assert "fork_activity.batch_phase_label(raw_phase)" in wrapper
    assert "fork_activity.batch_ffmpeg_active(raw_phase)" in wrapper
    assert "sink.get('phase', '')" in wrapper
    assert "key=f'{position}:{raw_phase}'" in wrapper


def test_the_batch_terminal_activity_class_is_structural(gui_tree, gui_source):
    """Keyed by the batch module's own `TERMINAL_*` vocabulary -- the same pure classifier the
    headline and the manifest use -- so the three cannot disagree. Never read off the status text.
    """
    table = gui_source[gui_source.index("_CUT_BATCH_ACTIVITY = {"):]
    table = table[:table.index("\n}\n") + 3]
    for terminal in ("TERMINAL_FAILED", "TERMINAL_CANCELLED", "TERMINAL_COMPLETED_WITH_FAILURES",
                     "TERMINAL_COMPLETED"):
        assert f"fork_cutter_batch.{terminal}" in table, terminal
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    assert "_CUT_BATCH_ACTIVITY.get(sink.get('terminal')" in wrapper
    assert "fork_activity.PHASE_NOT_RUN" in wrapper, "a run that never started is not a success"
    for forbidden in ("startswith", "endswith", "status =="):
        assert forbidden not in wrapper, forbidden


def test_a_lost_batch_record_is_not_reported_as_a_clean_completion(gui_tree):
    """The headline already says so; the activity panel must agree with it rather than show a
    green Completed over a run whose authoritative record never reached disk."""
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    marker = wrapper.index("manifest_error")
    tail = wrapper[marker:marker + 400]
    assert "fork_activity.PHASE_PARTIAL" in tail
    assert "fork_activity.STATUS_FAILED" in tail


def test_the_manifest_phase_is_published_where_the_batch_already_knows_it(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "phase=fork_cutter_batch.PHASE_MANIFEST" in body


# --------------------------------------------------------------------------------------------
# The browser tick. One interval per page, reading the panel's own data -- no backend poll.
# --------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def tick_js(gui_source: str) -> str:
    start = gui_source.index('CUT_ACTIVITY_JS = r"""')
    return gui_source[start:gui_source.index('"""', start + 24) + 3]


def test_the_tick_is_installed_exactly_once_and_registers_no_server_call(gui_source):
    registrations = [line for line in gui_source.splitlines()
                     if "js=CUT_ACTIVITY_JS" in line]
    assert len(registrations) == 1
    assert "fn=None" in registrations[0]
    assert "inputs=None" in registrations[0] and "outputs=None" in registrations[0]


def test_the_tick_polls_no_backend(tick_js):
    """"No unnecessary backend polling" is the requirement, and the way to keep it is to have no
    network call in here at all. There is also no new endpoint and no background service."""
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "axios",
                      "/queue/", "navigator.sendBeacon"):
        assert forbidden not in tick_js, forbidden


def test_the_tick_runs_about_once_a_second_and_only_once_per_page(tick_js):
    assert "setInterval(tick, 1000)" in tick_js
    assert tick_js.count("setInterval") == 1
    assert "if (window.__bsCutActivity) { return; }" in tick_js


def test_the_tick_selects_by_class_because_two_modes_render_the_same_markup(tick_js):
    """Single Video and Folder Batch write this panel into two components, so an id would be
    ambiguous by construction."""
    assert 'querySelectorAll(".bs-cut-activity")' in tick_js
    assert "getElementById" not in tick_js


def test_the_tick_reads_exactly_the_contract_the_renderer_publishes(tick_js):
    clock = activity.ActivityClock(0.0)
    clock.enter(activity.PHASE_CUTTING, 0.0)
    markup = clock.render(1.0)
    for name in ("data-running", "data-job-token", "data-job-ms", "data-phase-token",
                 "data-phase-ms"):
        assert name in tick_js, name
        assert f'{name}="' in markup, name
    assert ".bs-cut-job-elapsed" in tick_js and ".bs-cut-phase-elapsed" in tick_js


def test_the_tick_freezes_a_terminal_panel_and_never_advances_it(tick_js):
    assert 'getAttribute("data-running") === "1"' in tick_js
    assert "if (!running) { out.textContent = show(base); return; }" in tick_js


def test_the_tick_is_anchored_rather_than_incremented(tick_js):
    """A Gradio re-render replaces the element and loses per-element state, so an incrementing
    counter would restart on every server frame. The anchor is keyed by TOKEN and survives."""
    assert "anchors[token] = Date.now() - base" in tick_js
    assert "show(Date.now() - anchors[token])" in tick_js
    assert "if (!live[token]) { delete anchors[token]; }" in tick_js


def test_the_tick_contains_no_apostrophe(tick_js):
    """Measured hazard, R5-P3: this file is a Python string handed to Gradio as JS, and one stray
    apostrophe in the wrong quoting context takes the whole script with it."""
    assert "'" not in tick_js
