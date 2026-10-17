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
    """Same ordering every other holder obeys: if the mutex is free, the token is empty."""
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    assert finallies
    final = [ast.unparse(s) for s in finallies[-1].finalbody]
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
    assert ast.unparse(kwargs["outputs"]) == "[cut_report]"


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


def test_no_scaling_or_frame_rate_argument_is_ever_added(export_tree):
    """Aspect ratio and frame rate are preserved by saying nothing about them."""
    rendered = ast.unparse(_func(export_tree, "build_segment_command"))
    for forbidden in ("'-vf'", "'-s'", "'-r'", "scale=", "setsar", "setdar"):
        assert forbidden not in rendered, forbidden


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


def test_the_analyze_outputs_match_what_the_handler_returns(gui_tree):
    outputs = _kwargs(_click(gui_tree, "cut_analyze_btn"))["outputs"]
    assert len(outputs.elts) == 4
    handler = _func(gui_tree, "_on_cut_analyze")
    for node in ast.walk(handler):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
            assert len(node.value.elts) == 4, ast.unparse(node)


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
