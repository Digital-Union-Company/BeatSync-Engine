"""UX-PREP-VISUAL-PROGRESS R1: Prepare & Verify looks alive without lying about progress.

[FORK] Digital-Union. UX-PREP-ACTIVITY made Scan and Analyze *speak* through a silent phase; this
item makes them **visible**, and fixes the specific thing the owner reported:

    a Scan of 6,363 sources showed `0 / 6363 (0.0%)` for several minutes, with nothing but a small
    line of text below a ten-line report box to say anything was happening.

Both halves of that are real and they have different causes, which is why this suite is organised
around the distinction rather than around the widgets:

* The count is **true**. `classify_library_sources` emits `start(current=0, total=len(sources))`
  and then runs its parallel identity pass, which emits nothing at all until the per-source
  verdict loop begins. A determinate figure of `0 / 6363` is therefore a measurement.
* The **silence** is the defect. So the panel answers "is this running?" from facts that cost no
  measurement (a state chip, a CSS animation) and "how far has it got?" **only** from measured
  counts, leaving the figure absent when nothing measured one.

Three tiers, as `CLAUDE.md` describes them, and they are not interchangeable:

1. the pure renderer (`beatsync_fork.prep_activity`) and the new read-only `ProgressView`
   accessor, imported and executed as ordinary code on a bare interpreter;
2. the **shipped** `_run_prep_in_worker`, AST-extracted into the same synthesised namespace
   `tests/test_prep_activity.py` already defines, driven by real `ProgressEvent`s and a real
   `ProgressView`, with only the disk work stubbed;
3. GUI runtime behaviour -- how `gradio==6.19.0` renders the markup -- which is **not** asserted
   here and is reported separately as a served smoke.
"""

from __future__ import annotations

import ast
import html
import re
import threading
import time

import pytest

from beatsync_fork import library_prep as lp
from beatsync_fork import prep_activity as pa
from beatsync_fork import progress as fork_progress
from beatsync_fork.progress_view import ProgressView
from test_media_verify import _func, _tree
from test_prep_activity import _code_only, _load_prep_worker_seam

_GUI = "src/gui.py"

#: The phase key `classify_library_sources` really emits, checked against the shipped source below
#: so a rename cannot leave this suite asserting about a phase nobody publishes.
_PREP_PHASE_KEY = "library_classify"


def test_the_phase_key_this_suite_drives_is_the_one_the_scanner_really_emits():
    source = open("src/video_analysis.py", encoding="utf-8").read()
    assert f'_PREP_PHASE = "{_PREP_PHASE_KEY}"' in source, \
        "the scan's phase key moved; this suite is now driving a phase nobody publishes"


def _attr(markup: str, name: str) -> str:
    """One `data-bs-prep-*` value out of a panel. The tests read the CONTRACT, not presentation."""
    match = re.search(r'data-bs-prep-%s="([^"]*)"' % re.escape(name), markup)
    assert match, f"no data-bs-prep-{name} in {markup!r}"
    return match.group(1)


def _spoken_text(markup: str) -> str:
    """Every word an operator can actually read or hear: the element text, plus the `aria-label`.

    A substring ban has to be applied to this rather than to the markup. `</span>` contains `/s`,
    and "nothing has been changed on disk yet" contains `eta` -- this repository has shipped the
    same class of mistake five times, a ban tripped by something that is not the thing banned, so
    the ban is scoped to the text it is about. The `aria-label` is deliberately INCLUDED, because
    a screen reader speaks it and an invented estimate hidden there would still be an invented
    estimate.
    """
    labels = " ".join(re.findall(r'aria-label="([^"]*)"', markup))
    return html.unescape(re.sub(r"<[^>]*>", " ", markup) + " " + labels)


# =============================================================================================
# 1 - the pure renderer: what a panel may and may not claim
# =============================================================================================

def test_a_running_panel_is_distinctly_running_and_names_itself_immediately():
    """Requirements 1 and 2. The first frame already carries the state, the operation and a clock."""
    state = pa.ActivityState(operation=pa.OPERATION_SCAN, phase=pa.PHASE_STARTING, elapsed_ms=0)
    markup = pa.activity_panel_html(state)

    assert _attr(markup, "status") == pa.STATUS_RUNNING
    assert _attr(markup, "running") == "1"
    assert _attr(markup, "operation") == pa.OPERATION_SCAN
    assert pa.STATUS_LABELS[pa.STATUS_RUNNING] in markup, "the state must be in words, not colour"
    assert "bs-prep-running" in markup, "and in a class, so the stylesheet can animate it"
    assert "(00:00 elapsed)" not in markup, "the panel lays the clock out; it is not a sentence"
    assert ">00:00<" in markup, "a measured reading of zero is still a measured reading"


def test_the_animation_is_alive_for_a_measured_zero_which_is_the_reported_defect():
    """Requirement 4, and the whole reason this item exists.

    `0 / 6363` is a true count. It is NOT evidence that anything is idle, so the element carrying
    the motion must not be the one whose width is the measurement -- a zero-width fill has nowhere
    to animate. The sweep is therefore a sibling spanning the whole track.
    """
    state = pa.ActivityState(operation=pa.OPERATION_SCAN, phase="Classifying 6363 library source(s)",
                             elapsed_ms=207_000, done=0, total=6363)
    markup = pa.activity_panel_html(state)

    assert _attr(markup, "determinate") == "1", "the count was measured, so it is shown"
    assert _attr(markup, "percent") == "0.0"
    assert 'class="bs-prep-fill" style="width:0.0%;"' in markup, markup
    # The sweep is NOT inside the fill, and is not sized by the measurement.
    assert '<div class="bs-prep-sweep"></div>' in markup, markup
    assert "bs-prep-fill bs-prep-sweep" not in markup, \
        "the motion must not ride the measured width, or a 0% frame stops moving"
    assert markup.index('class="bs-prep-fill"') < markup.index('class="bs-prep-sweep"')


@pytest.mark.parametrize("done,total", [(0, 6363), (1500, 6363), (6363, 6363)])
def test_a_determinate_frame_animates_at_every_measured_value(done, total):
    """Not only at zero: the sweep is present for every count, because the count never answers
    "is it still running" -- a scan sitting at 6363 / 6363 through its report phase is still
    working."""
    markup = pa.activity_panel_html(pa.ActivityState(
        operation=pa.OPERATION_SCAN, phase=pa.PHASE_WORKING, elapsed_ms=1_000,
        done=done, total=total))
    assert '<div class="bs-prep-sweep"></div>' in markup, (done, total)


def test_an_indeterminate_panel_shows_no_figure_and_omits_aria_valuenow():
    """Requirement 5. With nothing measured, nothing numeric may be implied -- and ARIA's own rule
    for an indeterminate progressbar is to omit `aria-valuenow`, not to publish a zero, because a
    screen reader announces a zero as a measurement."""
    markup = pa.activity_panel_html(pa.ActivityState(
        operation=pa.OPERATION_ANALYZE, phase=pa.PHASE_WORKING, elapsed_ms=73_000))

    assert _attr(markup, "determinate") == "0"
    assert _attr(markup, "done") == "" and _attr(markup, "total") == ""
    assert _attr(markup, "percent") == ""
    assert 'role="progressbar"' in markup
    assert "aria-valuenow" not in markup, markup
    assert "%<" not in markup and "%)" not in markup, markup
    assert '<div class="bs-prep-sweep"></div>' in markup, "still visibly alive"


# ---------------------------------------------------------------------------------------------
# PR #111 finding F2 -- an unmeasured phase must not paint a completed-looking bar
# ---------------------------------------------------------------------------------------------

def test_an_indeterminate_track_carries_no_fill_element_at_all():
    """R1 rendered the indeterminate fill at `width:100%`, which paints the same solid bar a
    genuinely complete `N / N` paints -- so a phase nobody counted looked finished.

    No fill element is strictly better than a dimmed one: there is then no element whose width
    could be read as a quantity at all.
    """
    markup = pa.activity_panel_html(pa.ActivityState(
        operation=pa.OPERATION_ANALYZE, phase=pa.PHASE_WORKING, elapsed_ms=4_000))

    assert "bs-prep-fill" not in markup, markup
    assert "width:100%" not in markup, markup
    assert "width:" not in markup, "no width may be asserted for something nobody measured"
    # ...and the track, the sweep and the running state all survive, so it still reads as alive.
    assert "bs-prep-track" in markup and "bs-prep-indeterminate" in markup, markup
    assert '<div class="bs-prep-sweep"></div>' in markup, markup
    assert _attr(markup, "status") == pa.STATUS_RUNNING
    assert pa.STATUS_LABELS[pa.STATUS_RUNNING] in markup


@pytest.mark.parametrize("done,total,width", [
    (0, 6363, "0.0%"),          # the owner's frame -- still exactly zero, never nudged up
    (1, 8000, "0.0%"),
    (1500, 6363, "23.6%"),
    (50, 100, "50.0%"),
    (6363, 6363, "100.0%"),     # and a real 100% is still painted as 100%
])
def test_a_determinate_track_keeps_the_exact_measured_fill(done, total, width):
    """F2 must not cost the determinate case anything. The fill is the measurement, including at
    both ends: 0% stays empty and 100% stays full, because both are things something counted."""
    markup = pa.activity_panel_html(pa.ActivityState(
        operation=pa.OPERATION_SCAN, phase=pa.PHASE_WORKING, elapsed_ms=1_000,
        done=done, total=total))

    assert f'<div class="bs-prep-fill" style="width:{width};"></div>' in markup, markup
    assert "bs-prep-indeterminate" not in markup, "this one IS a measurement"
    assert _attr(markup, "percent") == width.rstrip("%")
    assert f'aria-valuenow="{width.rstrip("%")}"' in markup
    assert '<div class="bs-prep-sweep"></div>' in markup, "and it is still visibly alive"


def test_only_a_determinate_track_can_ever_paint_a_full_bar():
    """The two states must not be confusable in either direction: a measured `N / N` is the ONLY
    thing that may look complete, and it is still not a terminal panel."""
    full = pa.activity_panel_html(pa.ActivityState(
        pa.OPERATION_SCAN, pa.PHASE_WORKING, 1_000, done=7, total=7))
    unknown = pa.activity_panel_html(pa.ActivityState(
        pa.OPERATION_SCAN, pa.PHASE_WORKING, 1_000))

    assert 'style="width:100.0%;"' in full and _attr(full, "determinate") == "1"
    assert "width:" not in unknown and _attr(unknown, "determinate") == "0"
    # Neither is terminal: both still animate and both still say Running.
    for markup in (full, unknown):
        assert '<div class="bs-prep-sweep"></div>' in markup
        assert _attr(markup, "running") == "1"


# ---------------------------------------------------------------------------------------------
# PR #111 finding F1 -- the panel may not claim nothing has been written
# ---------------------------------------------------------------------------------------------

#: Words that would amount to a claim about what this operation has or has not written. The ban is
#: applied to the SPOKEN text, for the reason `_spoken_text` records.
_DISK_CLAIM_WORDS = ("nothing has been changed", "nothing has been written", "on disk",
                     "no changes", "unchanged on disk", "read-only", "read only",
                     "nothing is written", "nothing was written")


def test_the_running_panel_makes_no_claim_about_what_has_been_written():
    """PR #111 finding F1, at the renderer.

    R1 defaulted the footer to "Nothing has been changed on disk yet." on the stated belief that
    it held for all four operations. It does **not** hold for Analyze:
    `analyze_video_sources` calls `_checkpoint_cache` -> `_save_cache` -> `json.dump` inside its
    `as_completed` loop, so each source's record lands on disk the moment that source finishes --
    while this panel is on screen saying the opposite.
    """
    for operation in (pa.OPERATION_SCAN, pa.OPERATION_ANALYZE):
        for state in (pa.ActivityState(operation, pa.PHASE_WORKING, 9_000),
                      pa.ActivityState(operation, "analysed", 9_000, done=4, total=100)):
            spoken = _spoken_text(pa.activity_panel_html(
                state, summary=pa.activity_line(state))).lower()
            for claim in _DISK_CLAIM_WORDS:
                assert claim not in spoken, f"{operation}: {claim!r} in {spoken!r}"
            assert "bs-prep-foot" not in pa.activity_panel_html(state), "no footer is rendered"


def test_the_module_defines_no_blanket_readonly_footer_for_this_panel():
    """The constant is gone, not merely unused: a defaulted reassurance is a claim that the next
    operation to gain a write path silently falsifies, and this renderer cannot see which backend
    it is describing."""
    assert not hasattr(pa, "PANEL_READONLY_FOOTER")
    assert "PANEL_READONLY_FOOTER" not in pa.__all__
    source = open("src/beatsync_fork/prep_activity.py", encoding="utf-8").read()
    tree = ast.parse(source)
    panel = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "activity_panel_html")
    footer_default = dict(zip(
        [a.arg for a in panel.args.kwonlyargs],
        [ast.literal_eval(d) if d is not None else None for d in panel.args.kw_defaults],
    ))["footer"]
    assert footer_default == "", f"activity_panel_html defaults footer to {footer_default!r}"


def test_the_shipped_scan_and_analyze_call_site_supplies_no_footer():
    """Structural companion, so the fix cannot be half-reverted by changing only the default."""
    worker = _code_only(_func(_tree(_GUI), "_run_prep_in_worker"))
    assert "activity_panel_html" in worker, worker
    assert "footer" not in worker, "Scan/Analyze must not supply a reassurance footer"
    assert "PANEL_READONLY_FOOTER" not in worker
    gui_source = open(_GUI, encoding="utf-8").read()
    assert "Nothing has been changed on disk yet." not in gui_source, gui_source[:0]


def test_the_verify_and_sample_one_line_footer_is_untouched():
    """`WORKING_FOOTER` stays, and stays correct: its only consumers are Verify and Sample, which
    write nothing at all. F1 is about the panel's default, not about that text."""
    assert pa.WORKING_FOOTER == "Working — nothing has been changed on disk yet."
    assert pa.WORKING_FOOTER in pa.working_text(
        pa.ActivityState(pa.OPERATION_VERIFY, pa.PHASE_MEDIA_HEADERS, 1_000))
    assert pa.WORKING_FOOTER not in pa.working_text(
        pa.ActivityState(pa.OPERATION_VERIFY, pa.PHASE_MEDIA_HEADERS, 1_000), footer=False)


def test_a_caller_that_supplies_a_footer_still_gets_one_escaped_and_collapsed():
    """Removing the default must not remove the capability -- a caller that can prove its own
    claim may still make it, and it is escaped like everything else."""
    markup = pa.activity_panel_html(
        pa.ActivityState(pa.OPERATION_VERIFY, pa.PHASE_MEDIA_HEADERS, 1_000),
        footer="This reads your files.\n<b>Nothing</b> is written.")
    assert "bs-prep-foot" in markup
    assert "<b>" not in markup and "\n" not in markup
    assert "This reads your files. &lt;b&gt;Nothing&lt;/b&gt; is written." in markup


@pytest.mark.parametrize("done,total", [
    (True, 5),            # bool subclasses int and must be rejected first
    (3, True),
    (None, 10),
    (3, None),
    (3.0, 10),            # a float is not a measured integer count
    (3, 10.0),
    ("3", 10),
    (3, 0),               # a zero denominator is not a measurement
    (-1, 10),
    (11, 10),             # more done than total is not a measurement either
])
def test_the_panel_inherits_the_counts_fail_closed_rule_unchanged(done, total):
    """Requirement 3 from the other side: the panel must not be a second, laxer gate on what
    counts as a measurement. It reuses `ActivityState.measured`, so anything short of two real
    integers renders the indeterminate form."""
    markup = pa.activity_panel_html(pa.ActivityState(
        operation="X", phase=pa.PHASE_WORKING, elapsed_ms=0, done=done, total=total))
    assert _attr(markup, "determinate") == "0", (done, total)
    assert "aria-valuenow" not in markup, (done, total)


def test_no_panel_may_carry_an_estimate_a_rate_or_a_remaining_time():
    """The ban `progress-events.md` places on invented ETAs, asserted on the rendered surface
    where an operator would most want one and where the data to compute one does not exist."""
    indeterminate = pa.ActivityState(pa.OPERATION_SCAN, pa.PHASE_WORKING, 90_000)
    determinate = pa.ActivityState(pa.OPERATION_SCAN, pa.PHASE_WORKING, 90_000,
                                   done=1500, total=6363)
    panels = [
        pa.activity_panel_html(indeterminate, summary=pa.activity_line(indeterminate)),
        pa.activity_panel_html(determinate, summary=pa.activity_line(determinate)),
        pa.status_panel_html(status=pa.OUTCOME_COMPLETED, operation=pa.OPERATION_SCAN,
                             message="27 video(s) need analysis.", elapsed_ms=90_000),
    ]
    for markup in panels:
        spoken = _spoken_text(markup)
        for forbidden in ("ETA", " eta ", "remaining", "estimated", "estimate", "per second",
                          "/s ", "throughput", "approx", "~", "about "):
            assert forbidden not in spoken, f"{forbidden!r} in {spoken!r}"


def test_the_percentage_has_exactly_one_arithmetic():
    """A panel and a line that disagreed about the same two integers would be worse than either
    alone, so both derive from `measured_percent` and there is no other route to a percentage."""
    state = pa.ActivityState(pa.OPERATION_SCAN, pa.PHASE_WORKING, 1_000, done=7, total=20)
    assert pa.measured_percent(state) == 35.0
    assert "35.0%" in pa.activity_line(state)
    assert _attr(pa.activity_panel_html(state), "percent") == "35.0"
    assert pa.measured_percent(pa.ActivityState("X", pa.PHASE_WORKING, 0)) is None

    source = open("src/beatsync_fork/prep_activity.py", encoding="utf-8").read()
    assert source.count("* 100.0") == 1, "a second percentage formula appeared"


def test_consecutive_running_frames_differ_even_within_one_second():
    """Mechanical, not cosmetic. A Gradio update whose value is byte-identical to the previous one
    is NOT delivered (the hazard the R5 grid nonce exists for), so without a guaranteed per-frame
    difference the panel would freeze while work continued -- and an HTML panel cannot carry the
    text pulse the one-line form uses, so the pulse moved into a `data-` attribute."""
    frames = [pa.activity_panel_html(pa.ActivityState(
        pa.OPERATION_SCAN, pa.PHASE_WORKING, 1_000, done=0, total=6363, pulse=n))
        for n in range(4)]
    assert len(set(frames)) == 4, frames
    assert [_attr(f, "pulse") for f in frames] == ["0", "1", "2", "3"]


def test_the_panel_reads_no_clock_of_its_own():
    """Every reading is passed in, exactly as for the one-line form. A renderer with its own clock
    could not be tested without sleeping and would be measuring the wrong interval anyway."""
    tree = ast.parse(open("src/beatsync_fork/prep_activity.py", encoding="utf-8").read())
    calls = [ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    for forbidden in ("time.time", "time.perf_counter", "time.monotonic", "datetime.now"):
        assert forbidden not in calls, f"{forbidden} is called inside the pure module"


def test_the_pure_module_still_imports_no_upstream_runtime():
    """CLAUDE.md's hard rule for `beatsync_fork`, re-asserted because this item added an import."""
    tree = _tree("src/beatsync_fork/prep_activity.py")
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("logger", "paths", "gradio", "gr", "cupy", "cv2", "librosa", "numpy",
                      "video_analysis", "gui"):
        assert forbidden not in imported, f"{forbidden} is an upstream runtime import"
    assert "html" in imported, "the escaping is stdlib `html`, not a hand-rolled replace chain"


# =============================================================================================
# 2 - terminal states: distinct, honest, and never claimed by accident
# =============================================================================================

def test_the_three_terminal_states_are_visually_and_structurally_distinct():
    """Requirement 6. "Declined, nothing ran" and "ran and broke" lead an operator somewhere
    different, and so does a success."""
    panels = {
        status: pa.status_panel_html(status=status, operation=pa.OPERATION_SCAN,
                                     message="M", elapsed_ms=1_000)
        for status in (pa.OUTCOME_COMPLETED, pa.OUTCOME_FAILED, pa.OUTCOME_REFUSED)
    }
    assert len(set(panels.values())) == 3, panels
    for status, markup in panels.items():
        assert _attr(markup, "status") == status
        assert f"bs-prep-{status}" in markup
        assert pa.STATUS_LABELS[status] in markup, "named in words, never by colour alone"
    # Three different tones, so the colour agrees with the words rather than repeating one of them.
    tones = {re.search(r"border-left:3px solid (#[0-9a-f]{6})", m).group(1)
             for m in panels.values()}
    assert len(tones) == 3, tones


def test_a_terminal_panel_has_no_bar_and_no_animation():
    """Requirement 6's "stop the animation", and an honesty rule: a full bar on completion would
    be a measurement nobody took -- a finished Scan knows how many sources it classified, not that
    some progress figure reached 100."""
    for status in (pa.OUTCOME_COMPLETED, pa.OUTCOME_FAILED, pa.OUTCOME_REFUSED, pa.STATUS_IDLE):
        markup = pa.status_panel_html(status=status, operation=pa.OPERATION_SCAN, message="M")
        assert "bs-prep-track" not in markup, status
        assert "bs-prep-sweep" not in markup, status
        assert 'role="progressbar"' not in markup, status
        assert _attr(markup, "running") == "0", status
        assert _attr(markup, "determinate") == "0", status
        assert _attr(markup, "percent") == "", status


def test_a_terminal_panel_renders_its_notice_verbatim():
    """The four terminal strings are compared by equality in accepted suites, so this decorates
    none of them -- it escapes and lays out, and changes no wording."""
    for notice in ("27 video(s) need analysis.", "Library is fully prepared.", "Scan failed.",
                   lp.REFUSAL_NOTICE, lp.BATCH_FINISHED_TEXT,
                   "Preparation unavailable — Qwen backend identity could not be verified."):
        markup = pa.status_panel_html(status=pa.OUTCOME_COMPLETED, message=notice)
        assert html.escape(notice) in markup, notice


def test_an_unknown_status_fails_closed_to_neutral_and_never_to_success_or_running():
    """A status this renderer does not know must not become a claim. "Running" would keep an
    animation alive over a dead worker; "completed" is the one answer a caller must never receive
    by accident."""
    for junk in ("", None, "ok", "done", "COMPLETED", "running ", 7, object()):
        markup = pa.status_panel_html(status=junk, message="M")
        assert _attr(markup, "status") == pa.STATUS_IDLE, junk
        assert _attr(markup, "running") == "0", junk


def test_an_idle_panel_with_nothing_to_say_renders_nothing_at_all():
    """Requirement 7's blank-space half, at the source. An empty card under the Scan button is
    exactly the dead vertical space this item is meant to reduce."""
    assert pa.status_panel_html(status=pa.STATUS_IDLE, message="") == ""
    assert pa.status_panel_html(status=pa.STATUS_IDLE, message="   ") == ""
    assert pa.status_panel_html(status=pa.STATUS_IDLE, message="") == ""
    # ...but a real invalidation notice IS shown, neutrally: it is not a success and not a fault.
    invalidated = lp.set_folder(lp.initial_state(), "J:/other")
    markup = pa.status_panel_html(status=pa.STATUS_IDLE, message=invalidated.notice)
    assert html.escape(invalidated.notice) in markup
    assert _attr(markup, "status") == pa.STATUS_IDLE


def test_refusal_wins_over_failure_because_a_declined_run_never_started():
    assert pa.terminal_status(failed=True, refused=True) == pa.OUTCOME_REFUSED
    assert pa.terminal_status(failed=False, refused=True) == pa.OUTCOME_REFUSED
    assert pa.terminal_status(failed=True, refused=False) == pa.OUTCOME_FAILED
    assert pa.terminal_status() == pa.OUTCOME_COMPLETED


def test_the_panel_statuses_reuse_the_existing_outcome_vocabulary():
    """No second four-label vocabulary for the same four answers."""
    assert set(pa.PANEL_STATUSES) == {pa.STATUS_IDLE, pa.STATUS_RUNNING} | set(pa.OUTCOMES)
    assert len(pa.PANEL_STATUSES) == len(set(pa.PANEL_STATUSES))
    for status in pa.PANEL_STATUSES:
        assert status in pa.STATUS_LABELS, status


# =============================================================================================
# 3 - escaping: nothing in a panel trusts a string
# =============================================================================================

@pytest.mark.parametrize("hostile", [
    '<script>alert(1)</script>',
    '"><img src=x onerror=alert(1)>',
    'J:\\lib\\<b>&amp;</b>',
    '</div></div><div class="bs-prep-panel bs-prep-completed">',
    '" data-bs-prep-status="completed" x="',
])
def test_every_caller_supplied_string_is_escaped_in_both_entry_points(hostile):
    """A library folder, a phase message and an FFmpeg error are all operator- or
    filesystem-controlled, and all three reach this markup."""
    panels = [
        pa.activity_panel_html(
            pa.ActivityState(operation=hostile, phase=hostile, elapsed_ms=1_000),
            summary=hostile, footer=hostile),
        pa.status_panel_html(status=pa.OUTCOME_FAILED, operation=hostile, message=hostile),
    ]
    for markup in panels:
        assert hostile not in markup, "the raw string reached the document"
        # No tag and no attribute boundary the caller got to open. The escaped characters may
        # still appear as TEXT -- that is the point of escaping them, not a leak.
        assert "<script" not in markup and "<img" not in markup and "<b>" not in markup, markup
        # The panel is still exactly ONE element, and still reports its REAL status: an injected
        # closing tag cannot split it and an injected attribute cannot relabel it.
        assert markup.count('class="bs-prep-panel') == 1, markup
        assert markup.count('data-bs-prep-status="') == 1, markup
        assert _attr(markup, "status") in (pa.STATUS_RUNNING, pa.OUTCOME_FAILED), markup
        assert pa.OUTCOME_COMPLETED not in _attr(markup, "status")


@pytest.mark.parametrize("ragged", ["a\nb\n\nc", "a\r\nb", "  a \t b  ", "line\n\nline"])
def test_the_markup_is_one_line_because_it_lands_in_a_markdown_component(ragged):
    """A blank line inside a raw HTML block is how a Markdown renderer decides the block ended, so
    a fragment split there renders as visible angle brackets rather than as a panel -- and
    `html.escape` leaves a newline alone, so escaping alone does not prevent it. Every string that
    reaches this markup can contain one: an FFmpeg error, a Windows path, a typed folder name."""
    panels = [
        pa.activity_panel_html(
            pa.ActivityState(operation=ragged, phase=ragged, elapsed_ms=1_000, done=1, total=2),
            summary=ragged, footer=ragged),
        pa.status_panel_html(status=pa.OUTCOME_FAILED, operation=ragged, message=ragged),
    ]
    for markup in panels:
        assert "\n" not in markup and "\r" not in markup, repr(markup)
        # Whitespace is collapsed; no word, number or character other than whitespace is changed.
        assert " ".join(ragged.split()) in _spoken_text(markup), repr(markup)


def test_the_accessible_summary_is_the_callers_own_one_line_form():
    """One renderer for "what is this operation doing right now", so a screen reader and a sighted
    operator cannot be told two different things."""
    state = pa.ActivityState(pa.OPERATION_SCAN, pa.PHASE_WORKING, 5_000, done=7, total=20)
    summary = pa.activity_line(state)
    markup = pa.activity_panel_html(state, summary=summary)
    # Collapsed to one line (the one-line form pads with double spaces, and an attribute lives in
    # a single-line document) but otherwise the caller's own string, word for word.
    assert f'aria-label="{html.escape(" ".join(summary.split()), quote=True)}"' in markup, markup
    for fragment in ("Scan Library", pa.PHASE_WORKING, "7 / 20", "35.0%", "elapsed"):
        assert fragment in _spoken_text(markup), fragment
    # And an absent summary means no attribute at all -- an empty `aria-label` is worse than none.
    assert "aria-label" not in pa.activity_panel_html(state)


# =============================================================================================
# 4 - the ProgressView accessor: measured, read-only, and not prose parsing
# =============================================================================================

def test_the_accessor_reports_the_active_phase_measurement_the_view_already_folded():
    view = ProgressView()
    view.apply(fork_progress.start(0, "Classifying 6363 library source(s)",
                                   current=0, total=6363, unit="sources",
                                   phase=_PREP_PHASE_KEY))

    reading = view.active_measurement()
    assert reading is not None
    assert (reading.current, reading.total) == (0, 6363)
    assert reading.counted is True
    assert reading.phase == _PREP_PHASE_KEY
    assert reading.message == "Classifying 6363 library source(s)"
    assert reading.unit == "sources"
    assert reading.finished is False


def test_an_uncounted_active_phase_reports_none_and_never_zero():
    """A zero is a measurement; `None` is the absence of one. Only `counted` may license a
    determinate figure on screen."""
    view = ProgressView()
    view.apply(fork_progress.start(5, "Video Analysis"))
    view.apply(fork_progress.state(5, "Qwen is loading its model", phase="qwen"))

    reading = view.active_measurement()
    assert reading is not None
    assert reading.counted is False
    assert reading.current is None and reading.total is None


def test_the_accessor_carries_nothing_across_a_phase_boundary():
    """The per-`(stage, phase)` monotonicity rule, re-asserted through the new reader: it reports
    one `_PhaseView` and therefore cannot inherit a previous phase's denominator -- which is how
    `758 / 100 (758%)` happened."""
    view = ProgressView()
    view.apply(fork_progress.start(6, "extracting", current=0, total=100, phase="prores_extract"))
    view.apply(fork_progress.progress(6, 100, 100, "extracting", phase="prores_extract"))
    view.apply(fork_progress.state(6, "assembling", phase="assembly"))

    reading = view.active_measurement()
    assert reading.phase == "assembly"
    assert reading.counted is False, "assembly must not inherit the extraction counter"


def test_the_accessor_is_empty_before_any_event_and_mutates_nothing():
    view = ProgressView()
    assert view.active_measurement() is None

    event = fork_progress.start(0, "x", current=1, total=2, phase=_PREP_PHASE_KEY)
    view.apply(event)
    before = view.render()
    reading = view.active_measurement()
    assert view.render() == before, "reading the measurement must not fold anything"
    with pytest.raises(Exception):
        reading.current = 99            # frozen: a consumer cannot edit the view through it


def test_the_accessor_parses_nothing_out_of_rendered_text():
    """Phase 2A deleted prose parsing from the GUI. Recovering the numbers from `render()` would
    reintroduce it at the other end of the same pipe."""
    body = _code_only(_func(_tree("src/beatsync_fork/progress_view.py"), "active_measurement"))
    for forbidden in ("render(", "re.", "split(", "index(", "strip()", "find("):
        assert forbidden not in body, f"active_measurement reaches {forbidden}"


# =============================================================================================
# 5 - the SHIPPED Scan/Analyze stream
#
# Built on the namespace `tests/test_prep_activity.py` defines, deliberately rather than on a
# fresh one: the whole point is that this is the real `_run_prep_in_worker`, over a real
# `ProgressView` and the real `_prep_phase` slot, with only the disk work stubbed.
# =============================================================================================

#: What a stubbed Scan returns when the test is not about the terminal classification. A REAL
#: `PrepSessionState`, because the terminal frame goes through the real `_prep_ui_updates`, which
#: asks the state for its report, its notice and its Analyze label.
_PLAIN_FINAL = lp.initial_state()


def _drive(namespace, work, operation=pa.OPERATION_SCAN):
    """Every frame `_run_prep_in_worker` emits for `work`, drained to the terminal one."""
    return list(namespace["_run_prep_in_worker"](work, "PREP-STATE", operation=operation))


def _notices(frames):
    """`_prep_busy_updates` / `_prep_ui_updates` -> (report, notice, gr.update, state)."""
    return [frame[1] for frame in frames]


def test_the_very_first_frame_is_already_running(monkeypatch):
    """Requirement 1. A function cannot say "working" and then work, so the accepted path streams
    -- and the first thing it streams must already be the RUNNING panel."""
    namespace, _ = _load_prep_worker_seam()
    frames = _drive(namespace, lambda cb, log: _PLAIN_FINAL)

    first = _notices(frames)[0]
    assert _attr(first, "status") == pa.STATUS_RUNNING, first
    assert _attr(first, "operation") == pa.OPERATION_SCAN
    assert '<div class="bs-prep-sweep"></div>' in first, "visibly alive on frame one"


def test_a_silent_multi_second_phase_keeps_animating_with_a_measured_clock():
    """Requirement 4 against the shipped stream. The work blocks past several poll intervals and
    emits no `ProgressEvent` at all -- a cold library's identity pass -- so the stream has to keep
    speaking on its own, with a clock that was read rather than predicted."""
    namespace, _ = _load_prep_worker_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]
    release = threading.Event()

    def silent(event_callback, console_logger):
        release.wait(timeout=tick * 3.5)
        return _PLAIN_FINAL

    notices = _notices(_drive(namespace, silent))
    running = [n for n in notices if _attr(n, "status") == pa.STATUS_RUNNING]

    assert len(running) >= 3, f"a silent phase must keep reporting: {len(running)} frames"
    assert len(set(running)) == len(running), "every frame must differ or it is not delivered"
    assert all('<div class="bs-prep-sweep"></div>' in n for n in running)
    assert all(_attr(n, "determinate") == "0" for n in running), \
        "nothing measured a count, so none may appear"
    elapsed = [int(_attr(n, "elapsed-ms")) for n in running]
    assert elapsed == sorted(elapsed), f"the clock must never go backwards: {elapsed}"
    assert elapsed[-1] >= 1000, "a multi-second phase must show a measured figure above zero"


def test_the_panel_turns_determinate_only_when_real_events_supply_the_counts():
    """Requirement 3, end to end over the real `ProgressView`: before the first event the panel is
    honestly indeterminate; afterwards it carries exactly the numbers the event measured."""
    namespace, _ = _load_prep_worker_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]

    def counted(event_callback, console_logger):
        time.sleep(tick * 1.3)                      # one silent tick first
        event_callback(fork_progress.start(
            0, "Classifying 6363 library source(s)", current=0, total=6363,
            unit="sources", phase=_PREP_PHASE_KEY))
        time.sleep(tick * 1.3)
        event_callback(fork_progress.progress(
            0, 1500, 6363, "classified", unit="sources", phase=_PREP_PHASE_KEY))
        time.sleep(tick * 1.3)
        return _PLAIN_FINAL

    notices = _notices(_drive(namespace, counted))
    running = [n for n in notices if _attr(n, "status") == pa.STATUS_RUNNING]

    assert _attr(running[0], "determinate") == "0", "no event had arrived yet"
    determinate = [n for n in running if _attr(n, "determinate") == "1"]
    assert determinate, f"the measured counts never reached a panel: {running}"
    seen = {(_attr(n, "done"), _attr(n, "total")) for n in determinate}
    assert ("0", "6363") in seen, seen
    assert ("1500", "6363") in seen, seen
    assert {_attr(n, "percent") for n in determinate} >= {"0.0", "23.6"}
    # Requirement 4 again, now with real counts in hand: the measured zero still animates.
    zeroes = [n for n in determinate if _attr(n, "done") == "0"]
    assert zeroes and all('<div class="bs-prep-sweep"></div>' in n for n in zeroes)


def test_an_in_flight_analyze_panel_claims_nothing_about_what_it_has_written():
    """PR #111 finding F1 against the SHIPPED stream, and Analyze specifically.

    Analyze is the operation the removed footer was false for: `analyze_video_sources` checkpoints
    each source's record the moment that source completes, so a record is on disk long before the
    operation ends. Every frame it emits -- the immediate one, the silent ticks, and the ones
    carrying real counts -- must therefore make no claim about disk at all.

    Driven through the real `_run_prep_in_worker` with real `ProgressEvent`s rather than asserted
    on the renderer alone, because the defect was a DEFAULT: the renderer looked fine in isolation
    and the shipped call site is what picked the claim up.
    """
    namespace, _ = _load_prep_worker_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]

    def analyzing(event_callback, console_logger):
        event_callback(fork_progress.start(
            5, "Analysing 100 source(s)", current=0, total=100, unit="sources"))
        time.sleep(tick * 1.3)
        # ...and by here the real backend has already checkpointed finished records to disk.
        event_callback(fork_progress.progress(5, 4, 100, "analysed", unit="sources"))
        time.sleep(tick * 1.3)
        return _PLAIN_FINAL

    notices = _notices(_drive(namespace, analyzing, operation=pa.OPERATION_ANALYZE))
    running = [n for n in notices if _attr(n, "status") == pa.STATUS_RUNNING]

    assert len(running) >= 3, f"expected several in-flight frames, got {len(running)}"
    assert any(_attr(n, "determinate") == "1" for n in running), \
        "precondition: the measured frames this test is about must have been produced"
    for notice in notices:
        spoken = _spoken_text(notice).lower()
        for claim in _DISK_CLAIM_WORDS:
            assert claim not in spoken, f"{claim!r} in an Analyze frame: {spoken!r}"
        assert "bs-prep-foot" not in notice, "no reassurance footer may be rendered"


def test_the_phase_shown_is_this_operations_own_event_message():
    """Requirement 2. The phase is published by this operation's event stream and by nothing else
    -- which is also why it does not reintroduce the cross-operation attribution defect R2 fixed:
    `_prep_phase` belongs to the mutex holders and is still never read here."""
    namespace, _ = _load_prep_worker_seam()
    tick = namespace["_ACTIVITY_TICK_SECONDS"]

    # A Verify is mid-sweep and owns the one diagnostic slot. Scan holds no mutex, so it really is
    # running at the same time.
    namespace["_mark_prep_phase"](pa.PHASE_MEDIA_HEADERS)

    def counted(event_callback, console_logger):
        event_callback(fork_progress.start(
            0, "Classifying 6363 library source(s)", current=0, total=6363,
            unit="sources", phase=_PREP_PHASE_KEY))
        time.sleep(tick * 1.4)
        return _PLAIN_FINAL

    notices = _notices(_drive(namespace, counted))
    rendered = "".join(notices)

    assert html.escape("Classifying 6363 library source(s)") in rendered, rendered
    assert pa.PHASE_MEDIA_HEADERS not in rendered, \
        "another operation's phase was attributed to this one"
    assert namespace["_prep_phase_now"]() == pa.PHASE_MEDIA_HEADERS, \
        "and the running Verify's slot was neither read nor written"


@pytest.mark.parametrize("notice,report,expected", [
    ("27 video(s) need analysis.", "Supported videos:  27", pa.OUTCOME_COMPLETED),
    ("Library is fully prepared.", "Prepared:  903", pa.OUTCOME_COMPLETED),
    ("Scan failed.", lp.FAILURE_PREFIX + "LIBRARY SCAN FAILED\nboom", pa.OUTCOME_FAILED),
    ("Preparation run failed.", lp.FAILURE_PREFIX + "PREPARATION ANALYSIS FAILED\nboom",
     pa.OUTCOME_FAILED),
    (lp.REFUSAL_NOTICE, lp.FAILURE_PREFIX + "Preparation inputs changed since the scan.",
     pa.OUTCOME_REFUSED),
])
def test_the_terminal_frame_names_what_actually_happened(notice, report, expected):
    """Requirement 6 against the shipped stream, including the distinction a prose reading would
    get wrong: a refusal and a failure both carry the failure marker, and only the notice constant
    tells them apart."""
    namespace, _ = _load_prep_worker_seam()
    final = lp.replace(lp.initial_state(), report_text=report, notice=notice)

    notices = _notices(_drive(namespace, lambda cb, log: final))
    terminal = notices[-1]

    assert _attr(terminal, "status") == expected, terminal
    assert _attr(terminal, "running") == "0"
    assert "bs-prep-track" not in terminal, "the animation must stop"
    assert html.escape(notice) in terminal, "the notice is rendered verbatim"
    assert _attr(terminal, "operation") == pa.OPERATION_SCAN
    assert int(_attr(terminal, "elapsed-ms")) >= 0, "the total is measured, not predicted"


def test_an_abandoned_stream_is_never_classified_as_successful():
    """Requirement 6's last clause. Nothing is yielded from a `finally` anywhere in this
    generator, so a closed generator -- a dropped Gradio event -- emits NO terminal frame, and the
    last thing on screen stays the honest RUNNING panel rather than a green Completed chip."""
    namespace, _ = _load_prep_worker_seam()
    release = threading.Event()
    finished = threading.Event()

    def slow(event_callback, console_logger):
        try:
            release.wait(timeout=30)
            return _PLAIN_FINAL
        finally:
            finished.set()

    stream = namespace["_run_prep_in_worker"](slow, "PREP-STATE", operation=pa.OPERATION_SCAN)
    seen = []
    try:
        seen.append(next(stream)[1])
        seen.append(next(stream)[1])
    finally:
        release.set()
        stream.close()
    assert finished.wait(timeout=10), "the stubbed work must finish, restoring sys.stdout"

    assert seen, "frames were emitted"
    for status in (pa.OUTCOME_COMPLETED, pa.OUTCOME_FAILED, pa.OUTCOME_REFUSED):
        assert status not in "".join(seen), f"an abandoned stream claimed {status}"
    assert all(_attr(frame, "status") == pa.STATUS_RUNNING for frame in seen)


def test_a_new_operation_and_an_input_change_both_clear_a_stale_indicator():
    """"No stale indicator after a new operation or input invalidation."

    Two different mechanisms, deliberately: a new operation's FIRST frame is already RUNNING, and
    a widget `change` goes through `_prep_ui_updates` with no status at all, which is the neutral
    idle panel -- so a finished Scan's green chip cannot sit over a scan the operator has just
    invalidated by retyping the folder.
    """
    namespace, _ = _load_prep_worker_seam()
    prepared = lp.replace(lp.initial_state(), notice="Library is fully prepared.")

    finished = _notices(_drive(namespace, lambda cb, log: prepared))[-1]
    assert _attr(finished, "status") == pa.OUTCOME_COMPLETED, "precondition"

    # (a) a new operation replaces it immediately, before doing any work
    restarted = _notices(_drive(namespace, lambda cb, log: prepared))[0]
    assert _attr(restarted, "status") == pa.STATUS_RUNNING, restarted

    # (b) and an input change renders the neutral panel for the invalidated state
    invalidated = lp.set_folder(prepared, "J:/somewhere-else")
    idle = namespace["_prep_ui_updates"](invalidated)[1]
    assert _attr(idle, "status") == pa.STATUS_IDLE, idle
    assert pa.OUTCOME_COMPLETED not in idle
    assert html.escape(invalidated.notice) in idle


def test_the_preparation_outputs_contract_is_unchanged():
    """The whole item had to fit inside the existing wiring: same arity, same order, same widget,
    Analyze still held disabled in flight."""
    namespace, _ = _load_prep_worker_seam()
    frames = _drive(namespace, lambda cb, log: lp.initial_state())

    assert all(len(frame) == 4 for frame in frames), [len(f) for f in frames]
    for frame in frames[:-1]:
        assert frame[3] == "PREP-STATE", "an in-flight frame carries the state it was given"
        assert frame[2] == ("gr.update", {"interactive": False}), frame[2]
    assert isinstance(frames[-1][0], str), "the report text"
    assert frames[-1][2][0] == "gr.update"


# =============================================================================================
# 6 - the GUI seam, structurally: no second coordinator, no new wiring, nothing fabricated
# =============================================================================================

def test_the_event_wiring_is_byte_identical():
    """`inputs`, `outputs` and the four-tuple arity are untouched -- this item added no component
    and no listener, so every pinned isolation contract survives unchanged."""
    source = open(_GUI, encoding="utf-8").read()
    assert "prep_outputs = [prep_report, prep_status, prep_analyze_btn, prep_state]" in source
    tree = _tree(_GUI)
    assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                      and getattr(n.targets[0], "id", None) == "prep_outputs")
    assert [getattr(node, "id", None) for node in assignment.value.elts] == \
        ["prep_report", "prep_status", "prep_analyze_btn", "prep_state"]


def test_the_notice_widget_is_still_the_one_markdown_component_moved_not_replaced():
    """A `gr.HTML` here would have been the obvious change and the wrong one: four other suites
    slice `gui.py` on the literal `prep_status = gr.Markdown`, so swapping the type would relocate
    somebody else's span. The panel is rendered INTO the existing component instead."""
    source = open(_GUI, encoding="utf-8").read()
    assert source.count("prep_status = gr.Markdown") == 1, source.count("prep_status = gr.Markdown")
    # Moved above the report box -- which is the placement half of the complaint -- and still
    # below the control that starts the work.
    assert source.index("prep_scan_btn = gr.Button") \
        < source.index("prep_status = gr.Markdown") \
        < source.index("elem_id='prep-report-box'"), "the panel must sit where the press happened"


def test_the_preparation_span_every_other_suite_slices_is_intact():
    """The move is only safe inside one window, and this is the assertion that keeps it there."""
    source = open(_GUI, encoding="utf-8").read()
    block = source[source.index("LABEL_PREP_SECTION"):source.index("prep_status = gr.Markdown")]
    assert "prep_batch_size = gr.Number(" in block, \
        "moving the notice above the batch widget breaks test_library_preparation"
    assert "gr.Radio" not in block
    assert "maximum=" not in block
    assert "file_count='multiple'" not in block


def test_no_second_coordinator_timer_or_poller_was_introduced():
    """One thread per call, owned by the generator that started it. The motion lives in CSS, so
    there is no `setInterval`, no `app.load(js=…)` installer and no second progress protocol."""
    worker = _code_only(_func(_tree(_GUI), "_run_prep_in_worker"))
    for forbidden in ("setInterval", "requestAnimationFrame", "Timer(", "ThreadPoolExecutor",
                      "schedule", "asyncio", "every(", "app.load", "_activity_frames"):
        assert forbidden not in worker, forbidden
    assert worker.count("threading.Thread(") == 1, worker
    assert "status_queue.get(timeout=_ACTIVITY_TICK_SECONDS)" in worker
    assert "queue.Empty" in worker

    css = open(_GUI, encoding="utf-8").read()
    css = css[css.index("PREP_ACTIVITY_CSS = "):]
    css = css[:css.index('\n"""', css.index('"""') + 3)]
    assert "<script" not in css and "javascript" not in css.lower()


def test_the_worker_still_consults_no_phase_slot_it_does_not_own():
    """UX-PREP-ACTIVITY R2, re-asserted because this item gave the notice a phase to show. The
    phase now comes from this operation's own `ProgressView`; the shared slot is still off-limits."""
    worker = _code_only(_func(_tree(_GUI), "_run_prep_in_worker"))
    for forbidden in ("_prep_phase_now", "_mark_prep_phase", "_prep_phase"):
        assert forbidden not in worker, forbidden
    assert "view.active_measurement()" in worker, "the phase and counts come from its own events"
    assert "fork_prep_activity.PHASE_WORKING" in worker, "and that is still the pre-event label"


def test_the_worker_callback_still_only_touches_the_queue():
    """Never touch a Gradio component from a worker thread."""
    callback = _func(_func(_tree(_GUI), "_run_prep_in_worker"), "event_callback")
    attributes = {node.func.attr for node in ast.walk(callback)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert attributes == {"put"}


def test_the_stylesheet_is_scoped_respects_reduced_motion_and_pins_no_heights():
    """Requirement 8. Scoped to classes nothing else uses, so it can reach no other control; the
    reduced-motion branch stops the motion WITHOUT making the running state ambiguous."""
    source = open(_GUI, encoding="utf-8").read()
    css = source[source.index("PREP_ACTIVITY_CSS = \"\"\""):]
    css = css[:css.index('\n"""')]

    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation: none !important;" in reduced
    assert "repeating-linear-gradient" in reduced, \
        "the running state must stay visually distinct without motion"

    assert "@keyframes bs-prep-sweep" in css and "@keyframes bs-prep-pulse" in css
    # Every selector is one of this panel's own names.
    selectors = re.findall(r"^\s*([.#][\w\-. ,>]+?)\s*\{", css, re.MULTILINE)
    for selector in selectors:
        for part in selector.split(","):
            part = part.strip()
            assert part.startswith(".bs-prep") or part.startswith("#prep-status"), part
    assert "margin: 0 !important" in css, "the blank-space fix is a collapsed margin"
    # No negative margin and no layout arithmetic tuned against one Gradio version -- the shape
    # `BUILD_ID_CSS` records as the one that silently overlaps the next release's markup. The
    # track's own 8px and the dot's own 8px are decoration of elements this panel created, not
    # measurements of Gradio's.
    # (`background-position: -40%` in the keyframe is where the sweep starts, off the left edge of
    # its own track -- that is the animation, not a layout offset.)
    assert not re.search(r"\b(margin|margin-top|margin-bottom|margin-left|margin-right|top|right"
                         r"|bottom|left|inset)\s*:\s*-\d", css), "a negative offset appeared"
    assert "position: fixed" not in css and "z-index" not in css
    assert not re.search(r"\b(min-height|max-height)\b", css), \
        "no pinned block height: a card that cannot grow with its own text is a clipped card"
    # `!important` only where it has to beat Gradio's own paragraph margins, and nowhere else
    # except the reduced-motion override, which must win over the animation it is cancelling.
    importants = re.findall(r"^\s*([a-z-]+):[^;]*!important", css, re.MULTILINE)
    assert set(importants) == {"margin", "padding", "animation"}, importants

    assert "PREP_ACTIVITY_CSS" in source[source.index("app = gr.Blocks("):
                                         source.index("app = gr.Blocks(") + 500], \
        "the stylesheet must actually be installed on the Blocks"


def test_the_failure_marker_and_the_refusal_notice_each_have_one_definition():
    """The terminal classification reads structure, not prose -- so the structure needs exactly one
    source of truth, or the reader and the writer drift apart silently."""
    assert lp.is_failure_report(lp.record_failure(lp.initial_state(), "boom").report_text)
    assert lp.is_failure_report(lp.FAILURE_PREFIX + "x")
    for benign in ("", None, "Supported videos:  27", lp.INTRO_TEXT, lp.BATCH_FINISHED_TEXT,
                   "failed", "ERROR", "❌"):
        assert not lp.is_failure_report(benign), repr(benign)

    prep_source = open("src/beatsync_fork/library_prep.py", encoding="utf-8").read()
    assert prep_source.count('"❌ "') == 1, "the marker must be written in exactly one place"
    gui_source = open(_GUI, encoding="utf-8").read()
    assert "Preparation run refused." not in gui_source, \
        "the refusal notice is a constant now, so it cannot drift from the comparison"
    assert gui_source.count("notice=fork_prep.REFUSAL_NOTICE") == 2


def test_nothing_about_this_reaches_stage_5_identity_a_cache_or_the_render_gate():
    """It is a status panel. It must not have grown a route into anything that matters."""
    worker = _code_only(_func(_tree(_GUI), "_run_prep_in_worker"))
    updates = _code_only(_func(_tree(_GUI), "_prep_ui_updates"))
    for body in (worker, updates):
        for forbidden in ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "resolve_cache_location",
                          "source_state", "process_video", "_RENDER_LOCK", "confirm_action",
                          "resolve_for_render", "open(", "os.remove", "shutil"):
            assert forbidden not in body, forbidden

    panel_source = open("src/beatsync_fork/prep_activity.py", encoding="utf-8").read()
    panel_tree = ast.parse(panel_source)
    calls = [ast.unparse(n.func) for n in ast.walk(panel_tree) if isinstance(n, ast.Call)]
    for forbidden in ("open", "os.stat", "os.path.exists", "subprocess.run", "json.dump"):
        assert forbidden not in calls, f"the pure renderer reaches {forbidden}"
