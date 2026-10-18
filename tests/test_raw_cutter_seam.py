"""Raw Cutter V1: the FFmpeg adapter and the GUI seam.

`raw_cutter_export.py` imports `ffmpeg_processing`, and `gui.py` cannot be imported on a bare
interpreter at all, so both are reached the way every other GUI seam suite reaches them — by `ast`
over the real source, and by executing AST-extracted bodies against a synthesised namespace.

What this file is for: the Raw Cutter must be a *tool beside* the render pipeline, not a new
branch of it. Most of these assertions are therefore about what it cannot reach.
"""

from __future__ import annotations

import ast
import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_EXPORT = os.path.join(_REPO_ROOT, "src", "raw_cutter_export.py")


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


@pytest.fixture(scope="module")
def gui_tree() -> ast.Module:
    return _tree(_GUI)


@pytest.fixture(scope="module")
def export_tree() -> ast.Module:
    return _tree(_EXPORT)


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _strip_docstrings(node):
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


def _code_only(node) -> str:
    """Executable source only. The bans below are about machinery; prose may discuss them."""
    class Blank(ast.NodeTransformer):
        def visit_Constant(self, inner):
            if isinstance(inner.value, str):
                return ast.copy_location(ast.Constant(value=""), inner)
            return inner

    return ast.unparse(Blank().visit(_strip_docstrings(node)))


def _click(tree: ast.Module, widget: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == widget
                and node.func.attr == "click"):
            return node
    raise AssertionError(f"no {widget}.click registration")


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


# =============================================================================================
# 1. The cutter cannot reach the render pipeline
# =============================================================================================

#: Everything the Raw Cutter must never touch. Rendering, the source-confirmation gate, creative
#: state, Stage-5 identity and output promotion are each a separate contract with its own rule
#: file; a footage-prep tool that reached any of them would be a second way into all of them.
_FORBIDDEN = (
    "process_video_guarded", "_process_video_guarded_unlocked", "process_video",
    "_process_video_impl", "render_selected_variants_guarded", "analyze_beats_auto",
    "create_music_video", "_promote_output_no_replace", "_publish_share_no_replace",
    "resolve_for_render", "live_declaration", "RenderLifecycle", "_install_active_render",
    "_clear_active_render", "RENDER_OUTCOME_KEY", "LAST_OUTPUT_PATH_KEY",
    "classify_library_sources", "_video_signature", "_cache_path", "_load_cache",
    "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "fork_creative", "fork_presets",
    "fork_freestyle", "fork_director", "fork_lab", "fork_batch",
)


@pytest.mark.parametrize("handler", ["_on_cut_analyze", "_on_cut_run", "_on_cut_cancel",
                                     "_on_cut_mode_change", "_on_cut_batch_count",
                                     "_on_cut_batch_run", "_on_cut_batch_cancel"])
def test_no_cutter_handler_can_reach_the_render_pipeline(gui_tree, handler):
    rendered = _code_only(_func(gui_tree, handler))
    for forbidden in _FORBIDDEN:
        assert forbidden not in rendered, f"{handler} reaches {forbidden}"


def test_the_cutter_writes_no_gate_preparation_or_render_widget(gui_tree):
    """The isolation that matters in practice: using the cutter cannot clear a source
    confirmation, enable or disable Create Music Video, move the preparation state machine or
    change what a render would produce."""
    forbidden = ("source_outputs", "source_report", "confirm_btn", "confirm_status", "process_btn",
                 "source_state", "prep_outputs", "prep_report", "prep_status", "prep_analyze_btn",
                 "prep_state", "session_state", "video_output", "status_output",
                 "render_invocation_state", "variation_seed", "creative_preset")
    for widget in ("cut_analyze_btn", "cut_run_btn", "cut_cancel_btn"):
        outputs = _names(_kwargs(_click(gui_tree, widget))["outputs"])
        for name in forbidden:
            assert name not in outputs, f"{widget} writes {name}"


def test_the_cutter_widgets_are_absent_from_the_gate_and_prep_output_lists(gui_source):
    for list_name in ("source_outputs = [", "prep_outputs = ["):
        start = gui_source.index(list_name)
        end = gui_source.index("]", start)
        block = gui_source[start:end]
        assert "cut_" not in block, list_name


#: The Raw Cutter's widgets, by exact name. Deliberately NOT a `cut_` prefix match: `cut_density`
#: is one of the six CREATIVE sliders and is a legitimate render input, so a prefix test would
#: fail on it and teach the next reader that the rule is wrong.
_CUTTER_WIDGETS = (
    "cut_source", "cut_source_info", "cut_output", "cut_sensitivity", "cut_min_seconds",
    "cut_max_seconds", "cut_remove_audio", "cut_analyze_btn", "cut_run_btn", "cut_cancel_btn",
    "cut_summary", "cut_table", "cut_report", "cut_plan_state",
    # Batch V1 additions. Same contract: a cutter widget wired into a render handler would make
    # footage curation part of the render request.
    "cut_mode", "cut_batch_folder", "cut_batch_recursive", "cut_batch_count_btn",
    "cut_batch_discovery", "cut_batch_start_btn", "cut_batch_cancel_btn", "cut_batch_status",
    "cut_batch_summary",
)


def test_the_cutter_is_absent_from_every_render_input_list(gui_tree):
    """A cutter widget wired into a render handler would make it part of the render request."""
    for button in ("process_btn", "render_selected_variants_btn"):
        inputs = set(_names(_kwargs(_click(gui_tree, button))["inputs"]))
        assert not inputs & set(_CUTTER_WIDGETS), button


# =============================================================================================
# 2. The mutex and the concurrency policy
# =============================================================================================

def test_the_cut_takes_the_shared_mutex_exactly_once(gui_tree):
    """Reusing `_RENDER_LOCK` rather than inventing a second lock is the whole concurrency design:
    one atomic test-and-set gives BOTH directions (a cut refuses during a render, and the render
    path already refuses while the lock is held) with no change to the render code."""
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    assert body.count("_RENDER_LOCK.acquire(blocking=False)") == 1
    assert body.count("_RENDER_LOCK.release()") == 1
    assert "_RENDER_LOCK.locked()" not in body, \
        "check-then-act is the race the non-blocking acquire exists to avoid"
    assert "_RENDER_LOCK.acquire()" not in body


def test_the_cut_clears_its_holder_token_before_releasing(gui_tree):
    """Same ordering every other holder obeys: if the mutex is free, the token is empty.

    [FORK] Raw Cutter progress P1: select the `finally` that actually RELEASES the mutex rather
    than whichever happens to be last. `_on_cut_run` now also contains the export worker's own
    `try/finally`, and `finallies[-1]` silently picked that one -- the assertion then tested a
    block it was never about. Selecting by content makes the test say what it means, and makes it
    strictly harder to satisfy: a handler that released the mutex somewhere with no holder clear
    would now fail here instead of being skipped.
    """
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    assert finallies
    releasing = [n for n in finallies
                 if any("_RENDER_LOCK.release()" in ast.unparse(s) for s in n.finalbody)]
    assert len(releasing) == 1, "exactly one finally may release the cutter's mutex"
    final = [ast.unparse(s) for s in releasing[0].finalbody]
    clear_at = next(i for i, s in enumerate(final) if "_mark_lock_holder('')" in s)
    release_at = next(i for i, s in enumerate(final) if "_RENDER_LOCK.release()" in s)
    assert clear_at < release_at, final
    drop_at = next(i for i, s in enumerate(final) if "_clear_cutter_job()" in s)
    assert drop_at < release_at, "the job slot must be dropped before the mutex is released"


def test_a_refused_cut_does_no_work_at_all(gui_tree):
    """A refusal must not create a directory or start FFmpeg -- it yields a message and returns.

    Asserted over the `if not ...acquire(...)` branch itself rather than a window of characters
    after it: the branch body IS the refusal path, so reading it directly cannot be fooled by how
    the rest of the function happens to be laid out.
    """
    guard = next(
        node for node in ast.walk(_func(gui_tree, "_on_cut_run"))
        if isinstance(node, ast.If)
        and "_RENDER_LOCK.acquire(blocking=False)" in ast.unparse(node.test)
        and isinstance(node.test, ast.UnaryOp))
    branch = ast.unparse(ast.Module(body=guard.body, type_ignores=[]))
    assert "_cut_busy_message()" in branch
    assert branch.strip().endswith("return"), branch
    for forbidden in ("prepare_run_directory", "export_segments", "makedirs", "CutterJob",
                      "_install_cutter_job", "_mark_lock_holder"):
        assert forbidden not in branch, f"a refused cut reaches {forbidden}"


def test_neither_cutter_event_joins_the_render_concurrency_lane(gui_tree):
    """Measured S2-A R3 rule: on the pinned gradio 6.19.0 a full concurrency group leaves the
    event QUEUED rather than delivering it, so the handler is never entered and the mutex that was
    meant to refuse it is never reached. A lane of 1 recreates the defect one step in."""
    for widget in ("cut_analyze_btn", "cut_run_btn"):
        kwargs = _kwargs(_click(gui_tree, widget))
        assert "concurrency_id" not in kwargs, f"{widget} must not join a shared lane"
        assert ast.unparse(kwargs["concurrency_limit"]) == "None", widget


def test_the_cutter_cancel_has_its_own_lane(gui_tree):
    """A stop queued behind the job it is meant to stop would only run once that job finished."""
    kwargs = _kwargs(_click(gui_tree, "cut_cancel_btn"))
    assert ast.unparse(kwargs["concurrency_id"]) == "CUTTER_CANCEL_CONCURRENCY_ID"
    assert ast.unparse(kwargs["concurrency_limit"]) == "None"
    assert ast.unparse(kwargs["inputs"]) == "[]"
    # [FORK] Raw Cutter progress P1: Cancel now also writes the live progress panel so it can say
    # "cancellation requested". The guarantee this test exists for is unchanged and still exact:
    # the cutter's report box stays FIRST, and Cancel writes nothing beyond the cutter's own two
    # read-only widgets -- no render, source-confirmation or Media Preparation widget.
    assert _names(kwargs["outputs"]) == ["cut_report", "cut_progress"]


def test_the_cancel_lane_is_not_the_render_cancel_lane(gui_source):
    assert "CUTTER_CANCEL_CONCURRENCY_ID = 'beatsync-cutter-cancel'" in gui_source
    assert "CANCEL_CONCURRENCY_ID = 'beatsync-cancel'" in gui_source


def test_nothing_live_enters_gradio_state(gui_tree):
    """`cut_plan_state` carries a plain dict of primitives. A lock, thread, handle or job object
    in `gr.State` is the hazard this repository bans outright."""
    rendered = _code_only(_func(gui_tree, "_on_cut_analyze"))
    for forbidden in ("CutterJob", "threading", "Lock(", "Popen", "RenderLifecycle"):
        assert forbidden not in rendered, forbidden


# =============================================================================================
# 3. Staleness
# =============================================================================================

def test_the_cut_revalidates_the_live_source_and_settings_before_working(gui_tree):
    """The plan says where THIS file's cuts fall. A re-saved source or a changed sensitivity makes
    it fiction, so the signature is recomputed from the live widgets and compared."""
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    assert "plan_signature" in body
    assert "CUT_STALE_PLAN_TEXT" in body
    assert "probe_source" in body, "the live file must be re-probed, not trusted from state"
    stale_at = body.index("CUT_STALE_PLAN_TEXT")
    acquire_at = body.index("_RENDER_LOCK.acquire(blocking=False)")
    assert stale_at < acquire_at, "a stale plan is refused before the mutex is even taken"


def test_remove_audio_is_deliberately_not_part_of_the_plan_signature():
    """It changes how a segment is exported, never where it begins or ends. Including it would
    refuse a still-valid plan and force a needless re-analysis of a long video."""
    from beatsync_fork import raw_cutter as rc

    source = rc.SourceInfo(path=r"C:\a.mkv", duration=10.0, size_bytes=1, mtime_ns=2)
    assert rc.plan_signature(source, 50, 5, 300) == rc.plan_signature(source, 50, 5, 300)
    signature = _code_only(_func(_tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork",
                                                    "raw_cutter.py")), "plan_signature"))
    assert "remove_audio" not in signature
    assert "encoder" not in signature


def test_an_absent_plan_is_refused_rather_than_invented(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_run"))
    assert "CUT_NO_PLAN_TEXT" in body


# =============================================================================================
# 4. The export adapter
# =============================================================================================

def test_every_ffmpeg_invocation_is_an_argument_list_never_a_shell_string(export_tree):
    """`shell=True` or a concatenated command is how a path with a quote in it becomes an
    injection. Every command in this module is built as a list."""
    rendered = _code_only(export_tree)
    assert "shell=True" not in rendered
    assert "os.system" not in rendered
    assert "subprocess.call" not in rendered
    command = _func(export_tree, "build_segment_command")
    returns = [n for n in ast.walk(command) if isinstance(n, ast.Return)]
    assert returns and all(isinstance(r.value, ast.Name) for r in returns)


def test_the_export_never_passes_an_overwrite_flag(export_tree):
    """No implicit `-y`. A collision is a refusal, so a second run cannot destroy the first's
    output."""
    rendered = ast.unparse(_func(export_tree, "build_segment_command"))
    assert "'-y'" not in rendered and '"-y"' not in rendered
    export = _code_only(_func(export_tree, "export_segments"))
    assert "os.path.exists(destination)" in export, "a collision must be detected and refused"


def test_remove_audio_drops_the_stream_rather_than_silencing_it(export_tree):
    """`-an` removes the audio stream from the container. A volume filter would leave an audible
    silence in a stream that is still there, which is not what the control promises."""
    rendered = ast.unparse(_func(export_tree, "build_segment_command"))
    assert "'-an'" in rendered
    assert "volume=" not in rendered
    assert "anullsrc" not in rendered


def test_keeping_audio_encodes_a_compatible_mp4_stream(export_tree):
    rendered = ast.unparse(_func(export_tree, "build_segment_command"))
    assert "'aac'" in rendered
    assert "'0:a:0?'" in rendered, "the optional-stream suffix keeps a silent source usable"


def test_a_source_without_audio_is_exported_without_audio_either_way(export_tree):
    """`has_audio=False` must take the `-an` branch even when Remove Audio is OFF, or FFmpeg would
    be asked to map a stream that does not exist and the whole segment would fail.

    Executed, not read: the command builder is pure argument assembly, so it can be AST-extracted
    and run against a namespace with nothing in it but the two constants it names.
    """
    namespace = {"FFMPEG_PATH": "ffmpeg.exe"}
    exec(ast.unparse(_func(export_tree, "build_segment_command")), namespace)
    build = namespace["build_segment_command"]

    silent = build("in.mkv", "out.mp4", 0.0, 5.0, remove_audio=False, encoder="libx264",
                   has_audio=False)
    assert "-an" in silent
    assert "aac" not in silent

    with_audio = build("in.mkv", "out.mp4", 0.0, 5.0, remove_audio=False, encoder="libx264",
                       has_audio=True)
    assert "-an" not in with_audio
    assert "aac" in with_audio

    stripped = build("in.mkv", "out.mp4", 0.0, 5.0, remove_audio=True, encoder="libx264",
                     has_audio=True)
    assert "-an" in stripped and "aac" not in stripped

    # The source is only ever an input, never a destination.
    assert stripped.index("in.mkv") < stripped.index("out.mp4")
    assert stripped[-1] == "out.mp4"
    assert "-y" not in stripped


# ---------------------------------------------------------------------------------------------
# [FORK] Digital-Union (frame-boundary P5-S1): the export's geometry guard.
#
# The original form of the guard below searched the RENDERED SOURCE of `build_segment_command`
# for six forbidden substrings. That shape has two defects, and P5 hit both:
#
#   1. It could not tell a banned operation from prose naming the ban. P5's docstring explains
#      "`trim`/`setpts` change no geometry: no `-s`, no `-r`, no `scale`, no `setsar`/`setdar`"
#      -- so FOUR of the six tokens now appear in the function purely as rationale. Dropping only
#      "'-vf'" from the tuple, which is what the P5 report proposed, would still have failed on
#      `setsar`. This file already carries `_code_only` for exactly that hazard; this test did not
#      use it.
#   2. A substring ban over source text is bypassable by spelling. `-filter:v scale=...` applies
#      a scaler while containing neither "'-vf'" nor... well, it contains "scale=", but
#      `-filter:v $(cat chain)` or a renamed local would not. A ban you can evade without changing
#      behaviour is not a guard.
#
# So the guard is now SEMANTIC: it executes the real command builder and inspects the argv it
# produces, across both encoders and both audio states, and it is supplemented by an AST pass over
# EXECUTABLE CODE ONLY so that a conditionally-added option cannot hide behind an untested branch.
#
# What is permitted is narrow and exact: the one `trim`/`setpts` chain P5 measured, bounded at the
# planned duration. Resolution, aspect ratio and frame rate are still preserved by saying nothing
# about them, which is what this guard has always been for.
# ---------------------------------------------------------------------------------------------

#: The ONLY video filter chain this export may carry. `trim` selects frames by their own
#: presentation timestamp and `setpts` rebases the clip's timeline to zero; neither resizes,
#: re-rates, re-samples nor touches the sample aspect ratio.
_ALLOWED_VIDEO_FILTER = re.compile(r"^trim=end=(\d+(?:\.\d+)?),setpts=PTS-STARTPTS$")

#: The audio bound has to describe the same interval, or the two streams disagree.
_ALLOWED_AUDIO_FILTER = re.compile(r"^atrim=end=(\d+(?:\.\d+)?),asetpts=PTS-STARTPTS$")

#: Every spelling FFmpeg accepts for "apply a filter graph". Only `-vf` (and `-af` for the audio
#: bound) may appear, exactly once each -- so a second chain, a replacement chain, or the same
#: chain under another name is a failure rather than a silent addition.
_FILTER_OPTION = re.compile(
    r"^-(?:vf|af|lavfi|filter|filter_complex|filter_script|filter_complex_script)(?::.*)?$")

#: Options that change output geometry without a filter at all.
_GEOMETRY_OPTIONS = (
    "-s", "-s:v", "-video_size", "-aspect", "-sws_flags", "-vf:v",
)

#: Every spelling that would IMPOSE an output frame rate. Matched by PATTERN rather than kept as an
#: exact list, because `-r:v` is the same instruction as `-r` and a ban you can evade by adding a
#: stream specifier is not a ban -- the same lesson as the substring ban this guard replaced.
#:
#: [FORK] Digital-Union (frame-boundary P8): `-fps_mode` is deliberately NOT in here any more, and
#: that is a TIGHTENING rather than a relaxation. It belonged here while the export said nothing
#: about timing at all -- but "say nothing" was measured to mean "let `-fps_mode auto` re-time a
#: variable-rate source to its nominal `r_frame_rate` and discard 38 of the 78 frames in the
#: requested interval". So the option is now required, in exactly one form with exactly one value,
#: which `_SYNC_CONTRACT` pins: an unreviewed value for it fails, and so does its absence.
#: `-vsync` stays banned -- it is the deprecated alias of `-fps_mode`, so admitting it would give
#: the same instruction a second, unpinned spelling.
_RATE_OPTION = re.compile(r"^-(?:r|fpsmax|framerate|vsync)(?::.*)?$")

#: [FORK] P8. The ONLY synchronisation options this export may carry, and their only permitted
#: values. Measured on the pinned FFmpeg 8.1.2 against a 270-frame source authored at 30, then 15,
#: then 60 fps, exporting the 78-frame 60 fps passage:
#:
#:   * neither option            -> 40 frames, 38 lost, output re-timed to 1/30
#:   * `passthrough` alone       -> 78 frames, but 38 of their timestamps collapse onto the
#:                                  preceding frame's, and video runs 1.333 s against audio 1.300 s
#:   * `demux` alone             -> correct HERE, but only because `-fps_mode auto` happened to
#:                                  stop converting once the time base was pinned; that is a
#:                                  heuristic's current behaviour, not a contract
#:   * both, as written below    -> the exact 78 frames, exact 1/60 spacing, no duplicate
#:                                  timestamp, video and audio both 1.300 s
#:
#: `demux` means "the source's own time base", which is why it is right in principle and not just
#: on this fixture: a source's timestamps are by definition exactly representable in its own time
#: base, so passthrough cannot collapse two of them. The older `-1` spelling measured identical on
#: every field, but FFmpeg 8.1.2 answers it with "-enc_time_base -1 is deprecated, use
#: -enc_time_base demux", so the non-deprecated name is the one pinned here.
_SYNC_CONTRACT = {
    "-fps_mode": "passthrough",
    "-enc_time_base": "demux",
}

#: Matches either synchronisation option in ANY stream-specifier spelling, so the value check
#: cannot be bypassed by writing `-fps_mode:v:0` or by dropping the specifier.
_SYNC_OPTION = re.compile(r"^-(fps_mode|enc_time_base)(?::.*)?$")

#: Filter names that would resize, re-rate or re-interpret the picture. Checked as written
#: tokens in executable code, so a chain assembled in pieces is still caught.
_GEOMETRY_FILTERS = (
    "scale=", "scale2ref", "scale_cuda", "scale_npp", "crop=", "pad=", "zoompan",
    "setsar", "setdar", "fps=", "framerate=", "minterpolate", "tinterlace", "yadif",
)

_EXPORT_PATHS = (
    ("libx264, audio removed", True, "libx264", False),
    ("libx264, audio removed, source has audio", True, "libx264", True),
    ("libx264, audio kept", False, "libx264", True),
    ("libx264, source has no audio", False, "libx264", False),
    ("h264_nvenc, audio removed", True, "h264_nvenc", True),
    ("h264_nvenc, audio kept", False, "h264_nvenc", True),
)


def _export_builder(export_tree):
    """`build_segment_command` AST-extracted and executed against a namespace holding nothing but
    the single constant it is allowed to name."""
    namespace = {"FFMPEG_PATH": "ffmpeg.exe"}
    exec(ast.unparse(_func(export_tree, "build_segment_command")), namespace)
    return namespace["build_segment_command"]


def _string_constants(node):
    """Every string literal in EXECUTABLE code, including the literal parts of f-strings.

    Docstrings are removed first: prose is allowed to name the operations the machinery bans.
    """
    out = []
    for inner in ast.walk(_strip_docstrings(node)):
        if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
            out.append(inner.value)
    return out


def test_no_scaling_or_frame_rate_argument_is_ever_added(export_tree):
    """Aspect ratio is preserved by saying nothing about it; the frame rate is never IMPOSED.

    Measured on the produced argv rather than on source text, so neither rationale prose nor an
    alternate option spelling can change the answer.

    [FORK] P8: the distinction this test now draws is between *imposing* a rate and *preserving*
    one. `-r`, `-fpsmax` and `-vsync` tell the output what its timing should be and stay banned in
    every specifier spelling; the reviewed `-fps_mode`/`-enc_time_base` pair tells FFmpeg to keep
    the timing the source already has, and is pinned separately by
    `test_the_only_synchronisation_options_are_the_reviewed_pair`.
    """
    build = _export_builder(export_tree)
    for label, remove_audio, encoder, has_audio in _EXPORT_PATHS:
        argv = build("in.mkv", "out.mp4", 10.022, 5.011, remove_audio=remove_audio,
                     encoder=encoder, has_audio=has_audio)
        for option in _GEOMETRY_OPTIONS:
            assert option not in argv, (label, option)
        imposed = [a for a in argv if _RATE_OPTION.match(a)]
        assert imposed == [], (label, "an output frame rate was imposed", imposed, argv)
        chain = argv[argv.index("-vf") + 1] if "-vf" in argv else ""
        for name in _GEOMETRY_FILTERS:
            assert name not in chain, (label, name, chain)

    # And the same bans over executable code, so a branch these six paths do not reach cannot
    # introduce one unseen. `_strip_docstrings` is what makes this immune to the rationale text.
    literals = _string_constants(_func(export_tree, "build_segment_command"))
    for option in _GEOMETRY_OPTIONS:
        assert option not in literals, option
    assert [text for text in literals if _RATE_OPTION.match(text)] == [], literals
    for name in _GEOMETRY_FILTERS:
        assert not any(name in text for text in literals), name


def test_the_only_video_filter_is_the_validated_trim_chain(export_tree):
    """[FORK] P5-S1. Exactly one video filter option, exactly one approved chain, right order.

    The chain is what makes the clip's frame membership the half-open interval the planner means;
    anything else in it, or a second chain anywhere, is a change this guard exists to notice.
    """
    build = _export_builder(export_tree)
    for label, remove_audio, encoder, has_audio in _EXPORT_PATHS:
        duration = 5.011
        argv = build("in.mkv", "out.mp4", 10.022, duration, remove_audio=remove_audio,
                     encoder=encoder, has_audio=has_audio)

        video_filters = [a for a in argv if a == "-vf"]
        assert len(video_filters) == 1, (label, "exactly one -vf expected", argv)

        other = [a for a in argv
                 if _FILTER_OPTION.match(a) and a not in ("-vf", "-af")]
        assert other == [], (
            label, "a filter graph was applied under another option spelling", other)

        chain = argv[argv.index("-vf") + 1]
        match = _ALLOWED_VIDEO_FILTER.match(chain)
        assert match, (label, "unapproved video filter chain", chain)
        assert abs(float(match.group(1)) - duration) < 5e-7, (
            label, "the trim bound is not the planned duration", chain, duration)

        elements = chain.split(",")
        assert len(elements) == 2, (label, "extra filter in the chain", elements)
        assert elements[0].startswith("trim=end="), (label, elements)
        assert elements[1] == "setpts=PTS-STARTPTS", (label, elements)
        assert chain.index("trim=") < chain.index("setpts="), (
            label, "setpts must follow trim, or the bound is measured on rebased timestamps")


def test_the_audio_bound_matches_the_video_bound_exactly(export_tree):
    """[FORK] P5-S1. Audio is bounded by `atrim` on the same interval, or not at all."""
    build = _export_builder(export_tree)
    for label, remove_audio, encoder, has_audio in _EXPORT_PATHS:
        duration = 7.967
        argv = build("in.mkv", "out.mp4", 6.066, duration, remove_audio=remove_audio,
                     encoder=encoder, has_audio=has_audio)
        keeps_audio = not remove_audio and has_audio
        if not keeps_audio:
            assert "-af" not in argv, (label, "audio was filtered on a path with no audio")
            assert "-an" in argv, (label, "audio must be dropped, not merely silenced")
            continue
        assert len([a for a in argv if a == "-af"]) == 1, (label, argv)
        chain = argv[argv.index("-af") + 1]
        match = _ALLOWED_AUDIO_FILTER.match(chain)
        assert match, (label, "unapproved audio filter chain", chain)
        assert abs(float(match.group(1)) - duration) < 5e-7, (label, chain, duration)
        video = _ALLOWED_VIDEO_FILTER.match(argv[argv.index("-vf") + 1])
        assert video and match.group(1) == video.group(1), (
            label, "the audio and video bounds describe different intervals")


def test_the_only_synchronisation_options_are_the_reviewed_pair(export_tree):
    """[FORK] P8. Exactly one `-fps_mode` and one `-enc_time_base`, each with its reviewed value.

    This is what replaced the blanket `-fps_mode` ban, and it is strictly tighter than the ban was:
    the ban matched one exact token, so `-fps_mode:v` walked straight past it, and it could say
    nothing at all about the VALUE -- `-fps_mode cfr` would have satisfied it while reintroducing
    the frame loss. This matches either option in any specifier spelling, requires both to be
    present exactly once, and admits exactly one value for each.

    Both options must also sit on the OUTPUT side. Before `-i` they are input options with a
    different meaning entirely, and an export that quietly became an input-side instruction would
    read as correct here while doing nothing.
    """
    build = _export_builder(export_tree)
    for label, remove_audio, encoder, has_audio in _EXPORT_PATHS:
        argv = build("in.mkv", "out.mp4", 10.022, 5.011, remove_audio=remove_audio,
                     encoder=encoder, has_audio=has_audio)
        input_at = argv.index("-i")
        seen = {}
        for position, token in enumerate(argv):
            match = _SYNC_OPTION.match(token)
            if match is None:
                continue
            option = "-" + match.group(1)
            assert option not in seen, (label, f"{option} appears more than once", argv)
            assert position + 1 < len(argv), (label, f"{token} carries no value", argv)
            assert position > input_at, (
                label, f"{token} is an input option here, not an output one", argv)
            seen[option] = (token, argv[position + 1])

        assert set(seen) == set(_SYNC_CONTRACT), (
            label, "the reviewed synchronisation pair is not exactly what gets passed", seen)
        for option, (token, value) in sorted(seen.items()):
            assert value == _SYNC_CONTRACT[option], (
                label, f"{token} carries an unreviewed value", value)
            assert token in (option, f"{option}:v"), (
                label, "unexpected option spelling", token)


# ---------------------------------------------------------------------------------------------
# [FORK] Digital-Union (frame-boundary P8): negative controls for the export guards.
#
# A guard nobody has watched fail is a guard nobody knows works. Each control below mutates the
# REAL adapter source in memory, re-parses it, and hands the mutated tree to the SHIPPED assertion
# that is supposed to notice -- so what is proved is that this file's own guard catches the defect,
# not that some test somewhere fails. The shipped file is never written to.
#
# `_mutated_tree` insists the target text is uniquely present. A mutation that silently matched
# nothing would make its control pass for the wrong reason, which is precisely the failure mode
# these exist to exclude.
# ---------------------------------------------------------------------------------------------

_FPS_MODE_LINE = "        '-fps_mode:v', 'passthrough',\n"
_ENC_TB_LINE = "        '-enc_time_base:v', 'demux',\n"
_VIDEO_CHAIN = 'f"trim=end={duration:.6f},setpts=PTS-STARTPTS"'
_AUDIO_CHAIN = 'f"atrim=end={duration:.6f},asetpts=PTS-STARTPTS"'


def _mutated_tree(*replacements: tuple[str, str]) -> ast.Module:
    with open(_EXPORT, encoding="utf-8") as handle:
        source = handle.read()
    mutated = source
    for old, new in replacements:
        assert mutated.count(old) == 1, f"mutation target is not uniquely present: {old!r}"
        mutated = mutated.replace(old, new)
    assert mutated != source, "mutation did not change the source"
    return ast.parse(mutated, filename=_EXPORT)


def test_negative_control_removing_passthrough_mode_is_caught():
    """Mutation: drop `-fps_mode:v passthrough`, which is how the frame loss was shipped."""
    tree = _mutated_tree((_FPS_MODE_LINE, ""))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


@pytest.mark.parametrize("mode", ["cfr", "vfr", "auto", "drop"])
def test_negative_control_a_frame_dropping_sync_mode_is_caught(mode):
    """Mutation: keep the option, change the value to one that may drop or duplicate frames.

    The old blanket ban could not have caught any of these -- it banned the option, so it had
    nothing to say once the option was legitimately present. `auto` is included because it is the
    default that caused the defect, and `vfr` because it silently DROPS a frame whose timestamp
    collides instead of keeping it.
    """
    tree = _mutated_tree((_FPS_MODE_LINE, f"        '-fps_mode:v', '{mode}',\n"))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


def test_negative_control_removing_the_encoder_time_base_is_caught():
    """Mutation: keep passthrough, drop the time base -- the measured collapsed-timestamp case.

    This is the control for "78 frames, 38 of them sharing a timestamp with their predecessor":
    a frame count alone would have called that a fix.
    """
    tree = _mutated_tree((_ENC_TB_LINE, ""))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


@pytest.mark.parametrize("value", ["0", "1:90000", "1:1000", "-1", "filter"])
def test_negative_control_changing_the_encoder_time_base_is_caught(value):
    """Mutation: a different time base. `0` is the nominal-rate base that collapses timestamps;
    the fixed ratios impose a grid unrelated to the source; `-1` is the deprecated spelling and
    `filter` the sibling named value -- both of which happen to work on the measured fixture, which
    is exactly why the guard pins the reviewed one rather than accepting anything that passes."""
    tree = _mutated_tree((_ENC_TB_LINE, f"        '-enc_time_base:v', '{value}',\n"))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


@pytest.mark.parametrize("option,value", [("-r", "30"), ("-r:v", "25"), ("-fpsmax", "30"),
                                          ("-vsync", "1"), ("-framerate", "30")])
def test_negative_control_an_imposed_output_frame_rate_is_caught(option, value):
    """Mutation: add an option that tells the output what its rate must be, in each spelling.

    `-vsync` is the one that matters most here: it is the deprecated alias of `-fps_mode`, so
    admitting it would give the reviewed instruction a second unpinned spelling.
    """
    tree = _mutated_tree((_FPS_MODE_LINE,
                          _FPS_MODE_LINE + f"        '{option}', '{value}',\n"))
    with pytest.raises(AssertionError):
        test_no_scaling_or_frame_rate_argument_is_ever_added(tree)


def test_negative_control_a_second_sync_option_spelling_is_caught():
    """Mutation: pass the reviewed value twice, once bare and once specified.

    Two spellings of one instruction is how a later edit changes behaviour while leaving the
    reviewed line visibly intact.
    """
    tree = _mutated_tree((_FPS_MODE_LINE,
                          _FPS_MODE_LINE + "        '-fps_mode', 'passthrough',\n"))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


def test_negative_control_an_input_side_sync_option_is_caught():
    """Mutation: move the pair before `-i`, where they are input options and preserve nothing.

    Both replacements are required for the control to mean anything: removing the output-side pair
    without re-adding it anywhere would be caught by the "both present" clause instead, and would
    prove nothing about position.
    """
    tree = _mutated_tree(
        (_FPS_MODE_LINE + _ENC_TB_LINE, ""),
        ("        '-i', source_path,\n",
         _FPS_MODE_LINE + _ENC_TB_LINE + "        '-i', source_path,\n"))
    with pytest.raises(AssertionError):
        test_the_only_synchronisation_options_are_the_reviewed_pair(tree)


@pytest.mark.parametrize("filter_name", ["fps=25", "framerate=30", "minterpolate"])
def test_negative_control_a_rate_conversion_filter_is_caught(filter_name):
    """Mutation: do the conversion in the filter graph instead, where no option ban would see it."""
    tree = _mutated_tree(
        (_VIDEO_CHAIN,
         'f"trim=end={duration:.6f},setpts=PTS-STARTPTS,%s"' % filter_name))
    with pytest.raises(AssertionError):
        test_no_scaling_or_frame_rate_argument_is_ever_added(tree)
    with pytest.raises(AssertionError):
        test_the_only_video_filter_is_the_validated_trim_chain(tree)


def test_negative_control_breaking_the_audio_video_alignment_is_caught():
    """Mutation: trim the audio to a different interval than the video.

    The A/V skew the P8 measurement exposed (1.333 s of video against 1.300 s of audio) came from
    the video side, but the reverse mistake is equally available and must not pass.
    """
    tree = _mutated_tree(
        (_AUDIO_CHAIN,
         'f"atrim=end={duration - 0.05:.6f},asetpts=PTS-STARTPTS"'))
    with pytest.raises(AssertionError):
        test_the_audio_bound_matches_the_video_bound_exactly(tree)


def test_the_negative_controls_are_measuring_a_guard_that_currently_passes(export_tree):
    """The control on the controls: unmutated, every assertion the mutants trip must hold.

    Without this, a guard that was broken outright would make every `pytest.raises` above succeed
    and the whole block would read as green.
    """
    test_the_only_synchronisation_options_are_the_reviewed_pair(export_tree)
    test_no_scaling_or_frame_rate_argument_is_ever_added(export_tree)
    test_the_only_video_filter_is_the_validated_trim_chain(export_tree)
    test_the_audio_bound_matches_the_video_bound_exactly(export_tree)


def test_the_video_stream_is_pinned_explicitly(export_tree):
    """A multi-stream container must not silently export a different stream than was probed."""
    rendered = ast.unparse(_func(export_tree, "build_segment_command"))
    assert "'0:v:0'" in rendered


def test_a_completed_file_is_validated_before_being_called_a_success(export_tree):
    body = _code_only(_func(export_tree, "export_segments"))
    assert "_validate_output" in body
    validator = _code_only(_func(export_tree, "_validate_output"))
    assert "getsize" in validator, "an empty file is not a success"
    assert "FFPROBE_PATH" in validator, "a file that is not readable video is not a success"


def test_only_this_operations_own_staging_file_is_ever_removed(export_tree):
    """R1 finding B. The earlier version deleted the DESTINATION path on failure, which could
    remove a file another process created in the window after the existence check — a file this
    run never owned. Cleanup is now scoped to our own staging artifact, and the destination is
    never removed on any path."""
    discard = _code_only(_func(export_tree, "_discard_staging"))
    assert "os.remove(path)" in discard
    for forbidden in ("rmtree", "os.rmdir", "glob", "listdir", "os.walk"):
        assert forbidden not in discard, forbidden
    rendered = _code_only(export_tree)
    assert "shutil" not in rendered
    assert "_discard_partial" not in rendered, "the destination-deleting helper must be gone"

    export = _code_only(_func(export_tree, "export_segments"))
    assert "_discard_staging(staging)" in export
    assert "_discard_staging(destination)" not in export
    assert "os.remove(destination)" not in export


def test_the_clip_is_published_by_an_atomic_no_replace_rename(export_tree):
    """A single same-directory `os.rename`, which on Windows raises `FileExistsError` rather than
    replacing. The check and the move are one indivisible step, so a collision cannot be missed in
    a window — and staging lives inside the run directory, so `EXDEV` is structurally impossible
    rather than merely unlikely."""
    export = _code_only(_func(export_tree, "export_segments"))
    assert "os.rename(staging, destination)" in export
    assert "FileExistsError" in export
    for forbidden in ("os.replace", "shutil.move", "copyfile"):
        assert forbidden not in export, f"publication uses {forbidden}"


def test_staging_names_carry_a_per_attempt_token(export_tree):
    """A predictable `.part` is a latent collision in which a retry truncates the first attempt's
    in-flight write — the same trap the delivery share file had to be fixed for."""
    export = _code_only(_func(export_tree, "export_segments"))
    assert "uuid.uuid4()" in export
    assert "staging_filename" in export

    from beatsync_fork import raw_cutter as rc
    first = rc.staging_filename("clip.mkv", 1, 3, "aaaa")
    second = rc.staging_filename("clip.mkv", 1, 3, "bbbb")
    assert first != second
    # `.part` marks it, but the real extension stays LAST: FFmpeg infers its muxer from the
    # extension when no `-f` is given, so a name ending in `.part` fails before it writes a frame.
    assert ".part." in first and first.endswith(".mp4")
    assert ".part." in second and second.endswith(".mp4")
    assert first.startswith("."), "staging artifacts stay out of the way in a listing"
    assert ".." not in first.lstrip(".") and "/" not in first and "\\" not in first


def test_completion_is_reported_through_a_callback(export_tree):
    """R1 finding A. `export_segments` raises on a stop, and an exception discards its return —
    so the caller reported "0 clip(s) completed" after a cancellation that had written several."""
    signature = [a.arg for a in _func(export_tree, "export_segments").args.args]
    assert "on_complete" in signature
    export = _code_only(_func(export_tree, "export_segments"))
    assert "on_complete(result)" in export


def test_the_cut_handler_reports_the_real_completed_count_on_cancellation(gui_tree):
    """The other half of finding A: the handler must read the callback's list, not the discarded
    return value."""
    handler = _func(gui_tree, "_on_cut_run")
    # Search every Try in the handler, not the first one `ast.walk` happens to reach: the
    # cancellation clause is on the outer try, and an inner one would be found first.
    cancelled = next(
        clause
        for node in ast.walk(handler) if isinstance(node, ast.Try)
        for clause in node.handlers
        if clause.type is not None and ast.unparse(clause.type).endswith("CutterCancelled"))
    branch = _code_only(ast.Module(body=cancelled.body, type_ignores=[]))
    assert "completed" in branch, "the cancellation report ignores the completed list"
    assert "for r in results" not in branch, "`results` is discarded when the exception propagates"
    assert "on_complete=finished" in _code_only(handler)


def test_the_run_directory_refuses_to_reuse_an_existing_folder(export_tree):
    body = _code_only(_func(export_tree, "prepare_run_directory"))
    assert "exist_ok=False" in body, "the creation must be the test, not a separate check"


def test_destinations_are_proven_to_stay_inside_the_run_directory(export_tree):
    body = _code_only(_func(export_tree, "export_segments"))
    assert "commonpath" in body, "containment must be measured, not assumed from a prefix"


def test_scene_detection_distinguishes_no_cuts_from_a_failed_detector(export_tree):
    """The whole reason this wrapper exists. `detect_video_scene_changes` answers `[]` for both,
    which would present a failed detector as a successful zero-cut analysis."""
    result = next(n for n in ast.walk(export_tree)
                  if isinstance(n, ast.ClassDef) and n.name == "SceneResult")
    fields = {t.target.id for t in result.body if isinstance(t, ast.AnnAssign)}
    assert {"times", "succeeded", "error"} <= fields
    body = _code_only(_func(export_tree, "detect_scenes"))
    assert "succeeded=False" in body and "succeeded=True" in body


def test_the_analyze_handler_refuses_on_a_failed_detector(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_analyze"))
    assert "scenes.succeeded" in body
    assert "detect_video_scene_changes" not in body, \
        "the conflating detector must not be used here"


def test_the_adapter_does_not_import_the_conflating_detector(export_tree):
    for node in ast.walk(export_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ffmpeg_processing":
            names = {alias.name for alias in node.names}
            assert "detect_video_scene_changes" not in names


def test_the_cutter_job_is_a_flag_not_a_render_lifecycle(export_tree):
    rendered = _code_only(export_tree)
    assert "RenderLifecycle" not in rendered
    assert "RenderOutcomeKind" not in rendered
    job = next(n for n in ast.walk(export_tree)
               if isinstance(n, ast.ClassDef) and n.name == "CutterJob")
    methods = {n.name for n in job.body if isinstance(n, ast.FunctionDef)}
    assert methods <= {"__init__", "cancel", "cancelled", "raise_if_cancelled"}


def test_cancellation_is_an_ordinary_exception(export_tree):
    """`BaseException` would make a cancelled cut indistinguishable from a KeyboardInterrupt and
    would slip past every `except Exception` on the path."""
    node = next(n for n in ast.walk(export_tree)
                if isinstance(n, ast.ClassDef) and n.name == "CutterCancelled")
    assert [ast.unparse(b) for b in node.bases] == ["Exception"]


def test_the_cut_handler_names_cancellation_before_the_generic_handler(gui_tree):
    """`CutterCancelled` is an ordinary Exception, so a generic handler listed first would report
    the operator's own Stop as a failure."""
    handler = _func(gui_tree, "_on_cut_run")
    tries = [n for n in ast.walk(handler) if isinstance(n, ast.Try) and n.handlers]
    outer = max(tries, key=lambda n: len(n.handlers))
    caught = [ast.unparse(h.type) if h.type else "bare" for h in outer.handlers]
    assert caught[0].endswith("CutterCancelled"), caught
    assert "Exception" in caught[-1], caught


# =============================================================================================
# 5. The GUI controls and their defaults
# =============================================================================================

def _widget(tree: ast.Module, name: str) -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name
                and isinstance(node.value, ast.Call)):
            return node.value
    raise AssertionError(f"no widget {name}")


def test_the_documented_defaults_are_the_shipped_defaults(gui_tree):
    sensitivity = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree,
                                                                   "cut_sensitivity").keywords}
    assert sensitivity["value"] == "fork_cutter.SENSITIVITY_DEFAULT"
    assert sensitivity["minimum"] == "fork_cutter.SENSITIVITY_MIN"
    assert sensitivity["maximum"] == "fork_cutter.SENSITIVITY_MAX"

    low = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree, "cut_min_seconds").keywords}
    assert low["value"] == "fork_cutter.MIN_CLIP_SECONDS_DEFAULT"
    high = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree, "cut_max_seconds").keywords}
    assert high["value"] == "fork_cutter.MAX_CLIP_SECONDS_DEFAULT"

    audio = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree,
                                                             "cut_remove_audio").keywords}
    assert audio["value"] == "True", "Remove Audio defaults ON"

    from beatsync_fork import raw_cutter as rc
    assert rc.SENSITIVITY_DEFAULT == 50
    assert rc.MIN_CLIP_SECONDS_DEFAULT == 5.0
    assert rc.MAX_CLIP_SECONDS_DEFAULT == 300.0


def test_the_read_out_widgets_are_not_editable(gui_tree):
    for name in ("cut_source_info", "cut_summary", "cut_report"):
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in _widget(gui_tree, name).keywords}
        assert kwargs["interactive"] == "False", name


def test_analyze_and_cut_are_two_separate_buttons(gui_tree, gui_source):
    """Cutting must never start automatically after an analysis."""
    assert gui_source.count("cut_analyze_btn.click(") == 1
    assert gui_source.count("cut_run_btn.click(") == 1
    analyze = _kwargs(_click(gui_tree, "cut_analyze_btn"))
    assert ast.unparse(analyze["fn"]) == "_on_cut_analyze"
    assert "then" not in gui_source[gui_source.index("cut_analyze_btn.click("):
                                    gui_source.index("cut_run_btn.click(")]


def test_analyze_writes_no_file_and_starts_no_export(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_analyze"))
    for forbidden in ("export_segments", "prepare_run_directory", "makedirs", "open("):
        assert forbidden not in body, f"Analyze reaches {forbidden}"


def test_the_cut_inputs_match_the_handler_signature(gui_tree):
    """Gradio passes handler arguments positionally."""
    for widget, handler in (("cut_analyze_btn", "_on_cut_analyze"),
                            ("cut_run_btn", "_on_cut_run")):
        inputs = _kwargs(_click(gui_tree, widget))["inputs"]
        names = [ast.unparse(e) for e in inputs.elts]
        parameters = [a.arg for a in _func(gui_tree, handler).args.args]
        assert len(names) == len(parameters), (widget, names, parameters)


#: The three button updates `_cut_buttons` / `_cut_batch_buttons` return as one set.
_BUTTON_SET_WIDTH = {"_cut_buttons": 3, "_cut_batch_buttons": 3}


def _emitted_arity(node, locals_=None):
    """How many Gradio outputs one `yield`/`return` expression supplies.

    [FORK] Raw Cutter progress P1. A handler emits `(payload values) + _cut_buttons(...)`, and
    some paths emit through a small local helper (`fail(...)`, `refuse(...)`). Counting only the
    literal tuple would under-count and let a genuinely wrong arity pass, so this resolves both
    the button-set helpers and any helper defined INSIDE the handler, by following that helper's
    own return. Anything it cannot resolve returns 0 and fails loudly rather than silently.
    """
    locals_ = locals_ or {}
    if isinstance(node, ast.Tuple):
        return len(node.elts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _emitted_arity(node.left, locals_) + _emitted_arity(node.right, locals_)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if name in _BUTTON_SET_WIDTH:
            return _BUTTON_SET_WIDTH[name]
        return locals_.get(name, 0)
    if isinstance(node, ast.IfExp):
        # `idle_plan if keep_plan else idle_no_plan` -- both branches must be the same width, or
        # one code path would write a different number of widgets than the other.
        left, right = _emitted_arity(node.body, locals_), _emitted_arity(node.orelse, locals_)
        assert left == right, f"branches of {ast.unparse(node)} emit {left} vs {right}"
        return left
    if isinstance(node, ast.Name):
        # `busy` / `idle_plan` / `idle_no_plan` are bound to a `_cut_buttons(...)` set.
        return 3
    return 0


def _handler_arities(handler):
    """Arity of every helper defined inside `handler`, resolved from its own return."""
    widths = {}
    for node in ast.walk(handler):
        if isinstance(node, ast.FunctionDef) and node is not handler:
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and ret.value is not None:
                    width = _emitted_arity(ret.value, widths)
                    if width:
                        widths[node.name] = width
    return widths


def test_the_analyze_outputs_match_what_the_handler_returns(gui_tree):
    """Every emitted tuple must be exactly as wide as the declared outputs.

    This is the defect class that shipped a 3-tuple against 4 outputs in V1: Gradio writes the
    wrong widget rather than failing, so the arity has to be pinned somewhere. P1 widened both
    sides together -- four payload widgets plus the progress panel, plus the three button states.
    """
    outputs = _kwargs(_click(gui_tree, "cut_analyze_btn"))["outputs"]
    assert _names(outputs) == ["cut_summary", "cut_table", "cut_plan_state", "cut_source_info",
                               "cut_progress", "cut_analyze_btn", "cut_run_btn", "cut_cancel_btn"]
    declared = len(outputs.elts)
    assert declared == 8
    handler = _func(gui_tree, "_on_cut_analyze")
    helpers = _handler_arities(handler)
    emitted = [n for n in ast.walk(handler)
               if isinstance(n, ast.Yield) and n.value is not None]
    assert emitted, "Analyze must emit something"
    for node in emitted:
        assert _emitted_arity(node.value, helpers) == declared, ast.unparse(node)


def test_the_cut_outputs_match_what_the_handler_yields(gui_tree):
    """[FORK] Raw Cutter progress P1: the same arity guarantee for Cut Video.

    Cut Video gained the progress panel and the three button states, and it emits from several
    terminal paths (success, cancellation, failure, and four separate refusals). Every one of
    them must be exactly as wide as the declared outputs.
    """
    outputs = _kwargs(_click(gui_tree, "cut_run_btn"))["outputs"]
    assert _names(outputs) == ["cut_report", "cut_progress",
                               "cut_analyze_btn", "cut_run_btn", "cut_cancel_btn"]
    declared = len(outputs.elts)
    handler = _func(gui_tree, "_on_cut_run")
    helpers = _handler_arities(handler)
    emitted = [n for n in ast.walk(handler) if isinstance(n, ast.Yield) and n.value is not None]
    assert emitted, "Cut Video must emit something"
    for node in emitted:
        assert _emitted_arity(node.value, helpers) == declared, ast.unparse(node)


def test_the_four_way_busy_table_names_every_holder(gui_tree):
    """The cutter's caller-side refusal, the fourth face of the shared table. Nothing is decided
    by elimination: an unpublished holder gets its own honest answer."""
    body = _code_only(_func(gui_tree, "_cut_busy_message"))
    for token in ("_LOCK_HOLDER_CUTTER", "_LOCK_HOLDER_RENDER", "_LOCK_HOLDER_VERIFY",
                  "_LOCK_HOLDER_SAMPLE", "MEDIA_BUSY_UNKNOWN_TEXT"):
        assert token in body, token


def test_the_adapter_is_imported_inside_the_handlers_not_at_module_scope(gui_tree):
    """A module-level import would add an FFmpeg-touching dependency to every GUI start for a tool
    most sessions never open."""
    top_level = {alias.name for node in gui_tree.body if isinstance(node, ast.Import)
                 for alias in node.names}
    assert "raw_cutter_export" not in top_level
    for handler in ("_on_cut_analyze", "_on_cut_run"):
        assert "import raw_cutter_export" in ast.unparse(_func(gui_tree, handler))
