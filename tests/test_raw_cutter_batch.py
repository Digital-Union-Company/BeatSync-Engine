"""Raw Cutter Batch V1: the pure bookkeeping and the GUI seam.

`beatsync_fork.raw_cutter_batch` is stdlib-only, so it is imported and tested as ordinary code.
`gui.py` and `raw_cutter_export.py` cannot be imported on a bare interpreter, so they are reached
the way every other seam suite reaches them -- by `ast` over the real source, and by executing
AST-extracted bodies against a synthesised namespace.

The product contract under test: ONE Start Batch press processes every eligible source without
further interaction, one broken source never costs the rest of the batch, nothing is ever
overwritten, and the report is honest after a failure or a cancellation.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import raw_cutter as cutter
from beatsync_fork import raw_cutter_batch as batch

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


def _click(tree: ast.Module, widget: str, event: str = "click") -> ast.Call:
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and getattr(node.func.value, "id", None) == widget
                and node.func.attr == event):
            return node
    raise AssertionError(f"no {widget}.{event} registration")


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords}


def _names(node) -> list[str]:
    return [n.id for n in ast.walk(node) if isinstance(n, ast.Name)]


def _item(index: int, outcome: str, **extra) -> batch.BatchItem:
    return batch.BatchItem(index=index, source_path=rf"J:\in\v{index}.mp4",
                           relative_parts=(f"v{index}",), outcome=outcome, **extra)


# =============================================================================================
# 1. The output layout: nothing may ever collide
# =============================================================================================

def test_the_same_basename_in_two_folders_gets_two_distinct_output_folders():
    """The headline collision from the requirement. `cameraA/video01.mp4` and
    `cameraB/video01.mp4` must never write into the same place."""
    root = r"J:\footage"
    layout = batch.plan_item_directories(
        [rf"{root}\cameraA\video01.mp4", rf"{root}\cameraB\video01.mp4"], root)
    assert layout == (("cameraA", "video01"), ("cameraB", "video01"))
    assert len(set(layout)) == 2


def test_the_input_directory_structure_is_preserved():
    root = r"J:\footage"
    layout = batch.plan_item_directories(
        [rf"{root}\a\b\c\deep.mp4", rf"{root}\top.mp4"], root)
    assert layout == (("a", "b", "c", "deep"), ("top",))


def test_two_names_that_sanitise_to_the_same_stem_are_still_separated():
    """`safe_stem` replaces unsafe characters, so two visibly different names can reduce to one.
    Uniqueness is decided across the WHOLE frozen list, which is the only way to catch this: a
    per-file check cannot know a later file is about to collide with it."""
    root = r"J:\footage"
    layout = batch.plan_item_directories(
        [rf"{root}\clip#1.mp4", rf"{root}\clip@1.mp4"], root)
    assert cutter.safe_stem("clip#1.mp4") == cutter.safe_stem("clip@1.mp4"), "premise"
    assert len(set(layout)) == 2, layout
    assert layout[0] != layout[1]


def test_collision_disambiguation_is_deterministic_not_ordinal():
    """A counter alone would depend on enumeration order, so the same tree scanned twice could
    produce two different layouts. The tag comes from the colliding file's own full path."""
    root = r"J:\footage"
    first = batch.plan_item_directories([rf"{root}\clip#1.mp4", rf"{root}\clip@1.mp4"], root)
    again = batch.plan_item_directories([rf"{root}\clip#1.mp4", rf"{root}\clip@1.mp4"], root)
    assert first == again
    assert "__" in first[1][-1], first


def test_case_insensitive_collisions_are_caught():
    """The destination filesystem is case-insensitive, so `Video01` and `video01` are one folder."""
    root = r"J:\footage"
    layout = batch.plan_item_directories([rf"{root}\Video01.mp4", rf"{root}\video01.mkv"], root)
    assert len({tuple(p.casefold() for p in parts) for parts in layout}) == 2, layout


def test_no_layout_component_can_escape_the_batch_directory():
    root = r"J:\footage"
    layout = batch.plan_item_directories(
        [rf"{root}\..\outside.mp4", rf"{root}\sub\ok.mp4"], root)
    for parts in layout:
        for piece in parts:
            assert piece not in ("", ".", "..")
            assert "/" not in piece and "\\" not in piece
            assert not piece.startswith("..")


def test_safe_component_keeps_dots_but_refuses_reserved_and_trailing_dots():
    # A folder legitimately named `camera.A` must not collapse the way a file stem does.
    assert batch.safe_component("camera.A") == "camera.A"
    assert cutter.safe_stem("camera.A") == "camera", "contrast: a STEM does split the extension"
    # Windows silently drops trailing dots and spaces, which would merge two distinct names.
    assert batch.safe_component("trailing...") == "trailing"
    assert batch.safe_component("spaced  ") == "spaced"
    # Reserved device names are reserved in every directory.
    assert batch.safe_component("CON") != "CON"
    assert batch.safe_component("nul.mp4").startswith("_")
    # Never empty, never a dot-run.
    assert batch.safe_component("") == "folder"
    assert batch.safe_component("..") == "folder"
    assert batch.safe_component(".") == "folder"
    assert batch.safe_component(None) == "folder"
    assert len(batch.safe_component("x" * 500)) <= 60


def test_the_batch_directory_name_carries_the_caller_s_timestamp():
    """Pure: the clock is the caller's, exactly as in `raw_cutter.run_directory_name`."""
    assert batch.batch_directory_name("20261017_120000") == "RawCutter_Batch_20261017_120000"
    assert batch.batch_directory_name("") == "RawCutter_Batch_run"
    source = _code_only(_func(_tree(os.path.join(
        _REPO_ROOT, "src", "beatsync_fork", "raw_cutter_batch.py")), "batch_directory_name"))
    for forbidden in ("now()", "time.time", "datetime", "random"):
        assert forbidden not in source, forbidden


# =============================================================================================
# 2. The output root may not sit inside the source tree
# =============================================================================================

@pytest.mark.parametrize("source,target", [
    (r"J:\footage", r"J:\footage"),
    (r"J:\footage", r"J:\footage\parts"),
    (r"J:\footage", r"J:\footage\a\b\parts"),
])
def test_an_output_root_inside_the_source_tree_is_refused(source, target):
    """Otherwise the next scan ingests this run's own clips and cuts them again: the batch writes
    MP4 and the enumeration accepts MP4."""
    assert batch.output_root_conflict(source, target)


def test_a_source_tree_inside_the_output_root_is_refused():
    """The same loop seen from the other end."""
    assert batch.output_root_conflict(r"J:\out\footage", r"J:\out")


@pytest.mark.parametrize("source,target", [
    (r"J:\footage", r"J:\parts"),
    (r"J:\footage", r"K:\footage\parts"),
    (r"J:\footage", r"J:\footage-parts"),
])
def test_a_genuinely_separate_output_root_is_allowed(source, target):
    """`footage-parts` must not be refused for `footage`: containment is measured with
    `commonpath`, never tested as a string prefix."""
    assert batch.output_root_conflict(source, target) == ""


def test_the_containment_decision_touches_no_filesystem():
    """Resolution belongs in the adapter (it needs `realpath` to see through a junction); the
    decision belongs in the pure module."""
    source = _code_only(_func(_tree(os.path.join(
        _REPO_ROOT, "src", "beatsync_fork", "raw_cutter_batch.py")), "output_root_conflict"))
    for forbidden in ("realpath", "exists", "isdir", "listdir", "stat", "open("):
        assert forbidden not in source, forbidden


def test_the_adapter_resolves_both_roots_before_judging_them(export_tree):
    """A junction is resolved and then judged on where it actually lands -- never refused on
    sight, and never compared unresolved. `J:\\out` may really BE `J:\\footage\\out`."""
    body = _code_only(_func(export_tree, "resolve_batch_roots"))
    assert body.count("os.path.realpath") >= 2, "both roots must be resolved"
    assert "output_root_conflict" in body
    resolve_at = body.index("os.path.realpath")
    judge_at = body.index("output_root_conflict")
    assert resolve_at < judge_at, "resolution must precede the containment decision"


# =============================================================================================
# 3. Outcome classification and honest totals
# =============================================================================================

def test_a_partial_export_is_never_counted_as_completed():
    """The requirement states it outright: do not report partially exported videos as complete."""
    totals = batch.summarise([_item(1, batch.OUTCOME_PARTIAL, clips_planned=4, clips_written=2)], 1)
    assert totals.completed == 0
    assert totals.partial == 1
    assert totals.failed == 1, "a partial source produced clips AND lost some; both halves count"
    assert totals.clips_written == 2


def test_no_usable_shots_is_not_a_failure():
    """A truthful answer about footage that has nothing long enough at these settings."""
    totals = batch.summarise([_item(1, batch.OUTCOME_NO_CLIPS)], 1)
    assert totals.no_clips == 1
    assert totals.failed == 0
    assert totals.completed == 0
    assert totals.attempted == 1


def test_unattempted_sources_are_never_counted_as_attempted():
    """A cancelled batch must not claim to have attempted files it never opened."""
    items = [_item(1, batch.OUTCOME_COMPLETED, clips_planned=2, clips_written=2),
             _item(2, batch.OUTCOME_NOT_ATTEMPTED),
             _item(3, batch.OUTCOME_NOT_ATTEMPTED)]
    totals = batch.summarise(items, 3)
    assert totals.discovered == 3
    assert totals.attempted == 1
    assert totals.not_attempted == 2
    assert totals.completed == 1


def test_every_outcome_has_operator_facing_wording():
    for name, value in vars(batch).items():
        if name.startswith("OUTCOME_") and isinstance(value, str) and name != "OUTCOME_LABELS":
            assert value in batch.OUTCOME_LABELS, name


def test_the_failure_family_covers_every_real_failure():
    failures = {batch.OUTCOME_UNREADABLE, batch.OUTCOME_DETECTION_FAILED,
                batch.OUTCOME_PLAN_FAILED, batch.OUTCOME_PARTIAL, batch.OUTCOME_EXPORT_FAILED}
    for outcome in failures:
        assert batch.summarise([_item(1, outcome)], 1).failed == 1, outcome
    for outcome in (batch.OUTCOME_COMPLETED, batch.OUTCOME_NO_CLIPS,
                    batch.OUTCOME_NOT_ATTEMPTED, batch.OUTCOME_CANCELLED):
        assert batch.summarise([_item(1, outcome)], 1).failed == 0, outcome


# =============================================================================================
# 4. The report is honest after failure and cancellation
# =============================================================================================

def _settings() -> batch.BatchSettings:
    return batch.BatchSettings(sensitivity=50, threshold=0.27, min_seconds=5.0, max_seconds=300.0,
                               remove_audio=True, encoder="h264_nvenc", recursive=True)


def test_the_summary_after_cancellation_says_so_and_keeps_the_written_count():
    items = [_item(1, batch.OUTCOME_COMPLETED, clips_planned=3, clips_written=3,
                   exported_seconds=30.0, bytes_written=3_000_000),
             _item(2, batch.OUTCOME_CANCELLED, clips_planned=5, clips_written=2,
                   exported_seconds=20.0, bytes_written=2_000_000),
             _item(3, batch.OUTCOME_NOT_ATTEMPTED)]
    text = batch.summary_text(items, 3, _settings(), r"J:\out\RawCutter_Batch_x", 61.5, True)
    assert "Batch cancelled" in text
    assert "Attempted        2" in text
    assert "Not attempted    1" in text
    assert "Clips exported   5" in text
    assert "kept" in text
    assert "not modified" in text


def test_the_summary_after_a_failure_does_not_claim_success():
    items = [_item(1, batch.OUTCOME_UNREADABLE, error="FFprobe could not read this file"),
             _item(2, batch.OUTCOME_COMPLETED, clips_planned=1, clips_written=1)]
    text = batch.summary_text(items, 2, _settings(), r"J:\out\b", 10.0, False)
    assert "Batch finished with failures" in text
    assert "⚠️" in text
    assert "unreadable source" in text
    assert "FFprobe could not read this file" in text


def test_a_clean_batch_reports_success_without_a_warning():
    items = [_item(i, batch.OUTCOME_COMPLETED, clips_planned=2, clips_written=2) for i in (1, 2)]
    text = batch.summary_text(items, 2, _settings(), r"J:\out\b", 5.0, False)
    assert "✅ Batch finished" in text
    assert "with failures" not in text
    assert "Sources needing attention" not in text


def test_the_summary_never_claims_the_source_folder_was_preserved():
    text = batch.summary_text([_item(1, batch.OUTCOME_COMPLETED, clips_planned=1, clips_written=1)],
                              1, _settings(), r"J:\out\b", 1.0, False)
    assert "Skipped footage is intentional" in text
    for forbidden in ("every frame", "complete coverage", "fully preserved", "lossless"):
        assert forbidden not in text.casefold(), forbidden


def test_a_long_failure_list_is_truncated_but_says_it_was():
    items = [_item(i, batch.OUTCOME_UNREADABLE, error="bad") for i in range(1, 60)]
    text = batch.summary_text(items, 59, _settings(), r"J:\out\b", 1.0, False, max_listed=5)
    assert "and 54 more" in text
    assert batch.MANIFEST_FILENAME in text


# =============================================================================================
# 5. The manifest
# =============================================================================================

def test_the_manifest_records_every_discovered_source_including_unattempted_ones():
    """A manifest that silently omitted them would read as a complete run."""
    items = [_item(1, batch.OUTCOME_COMPLETED, clips_planned=2, clips_written=2),
             _item(2, batch.OUTCOME_NOT_ATTEMPTED)]
    payload = batch.manifest_payload(items, 2, _settings(), r"J:\out\b", r"J:\in",
                                     "2026-10-17T12:00:00", "2026-10-17T12:05:00", 300.0, True)
    assert payload["schema"] == batch.MANIFEST_SCHEMA
    assert payload["cancelled"] is True
    assert len(payload["sources"]) == 2
    assert [s["outcome"] for s in payload["sources"]] == [batch.OUTCOME_COMPLETED,
                                                          batch.OUTCOME_NOT_ATTEMPTED]
    assert payload["totals"]["discovered"] == 2
    assert payload["totals"]["attempted"] == 1


def test_the_manifest_is_json_serialisable_primitives_only():
    import json

    payload = batch.manifest_payload(
        [_item(1, batch.OUTCOME_PARTIAL, clips_planned=3, clips_written=1, error="x")],
        1, _settings(), r"J:\out\b", r"J:\in", "a", "b", 1.5, False)
    restored = json.loads(json.dumps(payload))
    assert restored == payload, "the payload must survive a JSON round trip unchanged"


def test_the_pure_module_does_not_serialise_or_write_the_manifest():
    """It returns a dict; the adapter owns the write. So the module needs no `json` import and
    touches no file."""
    source = open(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "raw_cutter_batch.py"),
                  encoding="utf-8").read()
    tree = ast.parse(source)
    imported = {alias.name.split(".")[0]
                for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert "json" not in imported
    rendered = _code_only(tree)
    for forbidden in ("open(", "makedirs", "os.rename", "os.remove", "subprocess", "Popen"):
        assert forbidden not in rendered, forbidden


def test_the_manifest_write_is_a_no_replace_rename_and_never_raises(export_tree):
    body = _code_only(_func(export_tree, "write_batch_manifest"))
    assert "os.rename(staging, destination)" in body
    for forbidden in ("os.replace", "shutil"):
        assert forbidden not in body, forbidden
    assert "uuid.uuid4()" in body, "a predictable temp name is a latent collision"
    assert "_discard_staging(staging)" in body
    # Total: it returns an error string, it does not propagate.
    handlers = [h for node in ast.walk(_func(export_tree, "write_batch_manifest"))
                if isinstance(node, ast.Try) for h in node.handlers]
    assert handlers, "the write must be guarded"
    caught = " ".join(ast.unparse(h.type) for h in handlers if h.type)
    assert "OSError" in caught


def test_the_fork_module_suffix_list_matches_the_adapter_s_real_one(export_tree):
    """The pure module restates the suffixes so it can render its own count message. The adapter
    owns the real list; pin them together rather than hoping they stay in step."""
    node = next(n for n in ast.walk(export_tree)
                if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", None) == "SUPPORTED_SOURCE_SUFFIXES")
    real = ast.literal_eval(node.value)
    assert tuple(real) == batch.cutter_supported_suffixes()


# =============================================================================================
# 6. Discovery: frozen, deterministic, and reusing the existing enumeration
# =============================================================================================

def test_discovery_reuses_the_existing_scanner_and_skips_fingerprinting(export_tree):
    """`scan_folder` already owns the two properties a batch needs and would be wrong to
    re-derive: all-or-nothing traversal, and a deterministic total order.

    `detect_duplicates=False` is the one deviation -- duplicate grouping reads the first and last
    1 MiB of every file, and a folder of multi-gigabyte footage would pay that before a clip was
    cut, for a feature batch mode does not have."""
    body = _code_only(_func(export_tree, "discover_batch_sources"))
    assert "input_manager.scan_folder" in body
    assert "detect_duplicates=False" in body
    assert "extensions=SUPPORTED_SOURCE_SUFFIXES" in body, \
        "a batch must accept exactly what Single Video mode accepts"
    assert "os.walk" not in body, "no second enumeration"
    assert "InputScanError" in body, "an incomplete traversal must be reported, never truncated"


def test_discovery_returns_a_frozen_tuple_not_a_mutable_list(export_tree):
    result = next(n for n in ast.walk(export_tree)
                  if isinstance(n, ast.ClassDef) and n.name == "DiscoveryResult")
    fields = {t.target.id: ast.unparse(t.annotation) for t in result.body
              if isinstance(t, ast.AnnAssign)}
    assert fields["paths"].startswith("tuple")
    assert "error" in fields


def test_the_discovery_count_text_reads_nothing_and_says_so():
    text = batch.discovery_text(12, r"J:\in", True, rejected=3)
    assert "12 video file(s)" in text
    assert "only a file count" in text
    assert "3 other file(s)" in text
    empty = batch.discovery_text(0, r"J:\in", False)
    assert "No supported video files" in empty
    assert "Include subfolders" in empty, "the likely cause is named when recursion is off"


# =============================================================================================
# 7. The GUI seam: one mutex, one job, no per-file acquire
# =============================================================================================

def test_the_batch_takes_the_shared_mutex_exactly_once_for_its_whole_lifetime(gui_tree):
    """The requirement is explicit, and the reason is structural: `_RENDER_LOCK` is a plain
    non-reentrant `Lock`, so a per-source acquire would deadlock on the second file."""
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert body.count("_RENDER_LOCK.acquire(blocking=False)") == 1
    assert body.count("_RENDER_LOCK.release()") == 1
    assert "_RENDER_LOCK.locked()" not in body
    assert "_RENDER_LOCK.acquire()" not in body


def test_the_batch_acquires_outside_its_own_loop(gui_tree):
    """A census of 1 is not enough on its own: one acquire INSIDE the per-source loop would still
    count as one call site while deadlocking on the second file."""
    handler = _func(gui_tree, "_on_cut_batch_run")
    loops = [n for n in ast.walk(handler) if isinstance(n, (ast.For, ast.While))]
    for loop in loops:
        rendered = _code_only(loop)
        assert "_RENDER_LOCK" not in rendered, \
            "the mutex must not be touched anywhere inside a loop body"
    assert loops, "the batch must actually loop over sources"


def test_the_batch_creates_exactly_one_cutter_job(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert body.count("CutterJob()") == 1
    assert body.count("_install_cutter_job(job)") == 1
    handler = _func(gui_tree, "_on_cut_batch_run")
    for loop in [n for n in ast.walk(handler) if isinstance(n, (ast.For, ast.While))]:
        assert "CutterJob" not in _code_only(loop), "one job for the whole batch"


def test_a_refused_batch_does_no_work_at_all(gui_tree):
    guard = next(
        node for node in ast.walk(_func(gui_tree, "_on_cut_batch_run"))
        if isinstance(node, ast.If)
        and "_RENDER_LOCK.acquire(blocking=False)" in ast.unparse(node.test)
        and isinstance(node.test, ast.UnaryOp))
    branch = ast.unparse(ast.Module(body=guard.body, type_ignores=[]))
    assert "_cut_busy_message()" in branch, "the refusal must name the real holder"
    assert branch.strip().endswith("return"), branch
    for forbidden in ("prepare_batch_directory", "prepare_item_directory", "export_segments",
                      "makedirs", "CutterJob", "_install_cutter_job", "_mark_lock_holder"):
        assert forbidden not in branch, f"a refused batch reaches {forbidden}"


def test_the_batch_clears_its_holder_token_and_job_before_releasing(gui_tree):
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_batch_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    assert finallies
    final = [ast.unparse(s) for s in finallies[0].finalbody]
    release_at = next(i for i, s in enumerate(final) if "_RENDER_LOCK.release()" in s)
    for token in ("_mark_lock_holder('')", "_clear_cutter_job()"):
        at = next(i for i, s in enumerate(final) if token in s)
        assert at < release_at, (token, final)


def test_the_batch_names_cancellation_before_the_generic_handler(gui_tree):
    """`CutterCancelled` is an ordinary Exception, so a generic clause first would report the
    operator's own Stop as a crash."""
    handler = _func(gui_tree, "_on_cut_batch_run")
    tries = [n for n in ast.walk(handler) if isinstance(n, ast.Try) and n.handlers]
    outer = max(tries, key=lambda n: len(n.handlers))
    caught = [ast.unparse(h.type) if h.type else "bare" for h in outer.handlers]
    assert caught[0].endswith("CutterCancelled"), caught
    assert "Exception" in caught[-1], caught


def test_the_manifest_is_written_from_the_finally_so_it_survives_a_failure(gui_tree):
    """A manifest that only appeared on the happy path would be worthless exactly when it is
    needed."""
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_batch_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    final = _code_only(ast.Module(body=finallies[0].finalbody, type_ignores=[]))
    assert "write_batch_manifest" in final
    assert "manifest_payload" in final


# =============================================================================================
# 8. One press, no confirmation, and the single-video workflow untouched
# =============================================================================================

def test_start_batch_is_one_click_with_no_confirmation_step(gui_tree, gui_source):
    assert gui_source.count("cut_batch_start_btn.click(") == 1
    kwargs = _kwargs(_click(gui_tree, "cut_batch_start_btn"))
    assert ast.unparse(kwargs["fn"]) == "_on_cut_batch_run"
    # No `.then(...)` chain: one press does the whole folder.
    start = gui_source.index("cut_batch_start_btn.click(")
    end = gui_source.index("cut_batch_cancel_btn.click(")
    assert ".then(" not in gui_source[start:end]


def test_the_batch_handler_requires_no_plan_state_and_no_staleness_gate(gui_tree):
    """Batch mode deliberately has no Analyze step, so there is no plan to go stale. Importing the
    single-video staleness machinery here would reintroduce the per-file interaction the product
    decision removed."""
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    for forbidden in ("plan_signature", "CUT_STALE_PLAN_TEXT", "_restore_plan", "cut_plan_state"):
        assert forbidden not in body, forbidden


def test_the_single_video_workflow_is_unchanged(gui_tree, gui_source):
    """Batch mode must not have altered Analyze / Cut Video."""
    assert gui_source.count("cut_analyze_btn.click(") == 1
    assert gui_source.count("cut_run_btn.click(") == 1
    analyze = _kwargs(_click(gui_tree, "cut_analyze_btn"))
    assert ast.unparse(analyze["fn"]) == "_on_cut_analyze"
    assert _names(analyze["outputs"]) == ["cut_summary", "cut_table", "cut_plan_state",
                                          "cut_source_info"]
    run = _kwargs(_click(gui_tree, "cut_run_btn"))
    assert ast.unparse(run["fn"]) == "_on_cut_run"
    assert _names(run["inputs"]) == ["cut_plan_state", "cut_source", "cut_output",
                                     "cut_sensitivity", "cut_min_seconds", "cut_max_seconds",
                                     "cut_remove_audio"]
    assert _names(run["outputs"]) == ["cut_report"]


def test_the_batch_reuses_the_shared_execution_settings_rather_than_duplicating_them(gui_tree):
    """The requirement: avoid maintaining duplicated values for the same execution setting. The
    sensitivity, minimum, maximum, Remove Audio and output-folder widgets feed BOTH modes."""
    shared = {"cut_sensitivity", "cut_min_seconds", "cut_max_seconds", "cut_remove_audio",
              "cut_output"}
    batch_inputs = set(_names(_kwargs(_click(gui_tree, "cut_batch_start_btn"))["inputs"]))
    single_inputs = set(_names(_kwargs(_click(gui_tree, "cut_run_btn"))["inputs"]))
    assert shared <= batch_inputs, shared - batch_inputs
    assert shared <= single_inputs, shared - single_inputs
    # And there is no second copy of any of them on the screen.
    for name in shared:
        assigns = [n for n in ast.walk(gui_tree)
                   if isinstance(n, ast.Assign) and len(n.targets) == 1
                   and getattr(n.targets[0], "id", None) == name]
        assert len(assigns) == 1, f"{name} is declared {len(assigns)} times"


def test_batch_mode_is_not_a_fifth_top_level_tab(gui_source):
    """The requirement says so explicitly: extend the existing Raw Cutter tab."""
    assert gui_source.count("gr.Tab(") == 4
    assert "LABEL_TAB_RAW_CUTTER" in gui_source
    assert gui_source.count("LABEL_CUT_MODE") >= 1, "the mode selector lives inside that tab"


def test_the_mode_switch_writes_only_visibilities(gui_tree):
    """Switching mode must not move an execution setting or clear any state, or the two workflows
    could silently change each other."""
    handler = _func(gui_tree, "_on_cut_mode_change")
    body = _code_only(handler)
    for forbidden in ("cut_sensitivity", "cut_min_seconds", "cut_max_seconds", "cut_remove_audio",
                      "cut_output", "cut_plan_state", "export_segments", "_RENDER_LOCK"):
        assert forbidden not in body, forbidden
    updates = [n for n in ast.walk(handler)
               if isinstance(n, ast.Call) and ast.unparse(n).startswith("gr.update(")]
    assert len(updates) == 6
    for call in updates:
        assert ast.unparse(call).startswith("gr.update(visible="), ast.unparse(call)
    outputs = _names(_kwargs(_click(gui_tree, "cut_mode", event="change"))["outputs"])
    assert outputs == ["cut_single_inputs", "cut_batch_inputs", "cut_single_actions",
                       "cut_batch_actions", "cut_single_readouts", "cut_batch_readouts"]


def test_every_batch_click_input_matches_its_handler_signature(gui_tree):
    """Gradio passes handler arguments positionally."""
    for widget, handler in (("cut_batch_start_btn", "_on_cut_batch_run"),
                            ("cut_batch_count_btn", "_on_cut_batch_count"),
                            ("cut_batch_cancel_btn", "_on_cut_batch_cancel")):
        inputs = _kwargs(_click(gui_tree, widget))["inputs"]
        names = [ast.unparse(e) for e in inputs.elts]
        parameters = [a.arg for a in _func(gui_tree, handler).args.args]
        assert len(names) == len(parameters), (widget, names, parameters)


def test_the_batch_generator_always_yields_both_of_its_two_outputs(gui_tree):
    """`outputs=[cut_batch_status, cut_batch_summary]`, so every yield must be a 2-tuple or Gradio
    writes the wrong widget. This is the defect class that shipped a 3-tuple against 4 outputs in
    V1's analyze handler."""
    outputs = _kwargs(_click(gui_tree, "cut_batch_start_btn"))["outputs"]
    assert _names(outputs) == ["cut_batch_status", "cut_batch_summary"]
    handler = _func(gui_tree, "_on_cut_batch_run")
    yields = [n for n in ast.walk(handler) if isinstance(n, ast.Yield)]
    assert yields, "the batch must stream progress"
    for node in yields:
        assert isinstance(node.value, ast.Tuple), ast.unparse(node)
        assert len(node.value.elts) == 2, ast.unparse(node)


def test_the_count_button_is_read_only_and_takes_no_mutex(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_count"))
    assert "discover_batch_sources" in body
    for forbidden in ("_RENDER_LOCK", "export_segments", "prepare_batch_directory", "makedirs",
                      "detect_scenes", "probe_source", "open("):
        assert forbidden not in body, forbidden
    assert _names(_kwargs(_click(gui_tree, "cut_batch_count_btn"))["outputs"]) == \
        ["cut_batch_discovery"]


def test_neither_batch_worker_event_joins_a_shared_concurrency_lane(gui_tree):
    """Measured S2-A R3 rule: a full concurrency group leaves the event QUEUED rather than
    delivering it, so the handler is never entered and the mutex never reached."""
    for widget in ("cut_batch_start_btn", "cut_batch_count_btn"):
        kwargs = _kwargs(_click(gui_tree, widget))
        assert "concurrency_id" not in kwargs, widget
        assert ast.unparse(kwargs["concurrency_limit"]) == "None", widget


def test_cancel_batch_shares_the_cutter_cancel_lane_and_the_one_job_slot(gui_tree):
    """Correct rather than convenient: both cancels target the SAME one-slot `_cutter_job`, because
    admission is the one shared mutex and only one cutter job can exist at a time."""
    kwargs = _kwargs(_click(gui_tree, "cut_batch_cancel_btn"))
    assert ast.unparse(kwargs["concurrency_id"]) == "CUTTER_CANCEL_CONCURRENCY_ID"
    assert ast.unparse(kwargs["concurrency_limit"]) == "None"
    assert ast.unparse(kwargs["inputs"]) == "[]"
    assert ast.unparse(kwargs["outputs"]) == "[cut_batch_status]"
    body = _code_only(_func(gui_tree, "_on_cut_batch_cancel"))
    assert "_CUTTER_JOB_LOCK" in body and "_cutter_job" in body
    assert "job.cancel()" in body
    for forbidden in ("terminate", "kill", "os.remove", "rmtree", "_RENDER_LOCK"):
        assert forbidden not in body, f"a cancel must not {forbidden}"


# =============================================================================================
# 9. One broken source may not cost the rest of the batch
# =============================================================================================

def test_each_per_source_failure_continues_the_loop(gui_tree):
    """The requirement: a batch must not stop because one video is broken or has no usable shots.

    Checked structurally -- every per-source failure classification must be followed by `continue`,
    not by `return` or `break`. `break` is reserved for cancellation.

    Both kinds of block are inspected, which matters: an unreadable source is classified in an
    `if`, while a planner refusal is classified in an `except fork_cutter.RawCutterError` handler.
    An earlier draft of this test walked only `ast.If` and therefore silently failed to check the
    `except` path at all -- so the set of constants it reached is asserted to be the COMPLETE set,
    rather than trusting that every one was visited.
    """
    handler = _func(gui_tree, "_on_cut_batch_run")
    loop = next(n for n in ast.walk(handler) if isinstance(n, ast.For)
                and "sources" in ast.unparse(n.iter))

    constant_names = {
        "OUTCOME_UNREADABLE", "OUTCOME_DETECTION_FAILED",
        "OUTCOME_PLAN_FAILED", "OUTCOME_NO_CLIPS",
    }
    found = set()
    for node in ast.walk(loop):
        if not isinstance(node, (ast.If, ast.ExceptHandler)):
            continue
        rendered = ast.unparse(node)
        for name in constant_names:
            if f"fork_cutter_batch.{name}" in rendered:
                statements = [ast.unparse(s) for s in node.body]
                assert any(s == "continue" for s in statements), (name, statements)
                assert not any(s.startswith("return") or s == "break" for s in statements), \
                    (name, statements)
                found.add(name)
    assert found == constant_names, f"unchecked recoverable outcomes: {constant_names - found}"

    # Every recoverable constant the module defines is covered above; a new one must be added here
    # deliberately rather than slipping in unchecked.
    assert constant_names == {
        name for name, value in vars(batch).items()
        if name.startswith("OUTCOME_") and isinstance(value, str)
        and value in {batch.OUTCOME_UNREADABLE, batch.OUTCOME_DETECTION_FAILED,
                      batch.OUTCOME_PLAN_FAILED, batch.OUTCOME_NO_CLIPS}}


def test_cancellation_is_checked_at_the_top_of_every_iteration(gui_tree):
    """So a Cancel can never be outrun by a `continue`: the remaining sources become
    NOT_ATTEMPTED rather than being started anyway."""
    handler = _func(gui_tree, "_on_cut_batch_run")
    loop = next(n for n in ast.walk(handler) if isinstance(n, ast.For)
                and "sources" in ast.unparse(n.iter))
    first = loop.body[0]
    assert isinstance(first, ast.If), ast.unparse(first)
    assert "job.cancelled" in ast.unparse(first.test), ast.unparse(first)
    assert any("break" == ast.unparse(s) for s in first.body), ast.unparse(first)


def test_unattempted_sources_are_recorded_after_the_loop(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "OUTCOME_NOT_ATTEMPTED" in body


def test_the_batch_never_deletes_an_output_directory_or_a_source(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    for forbidden in ("rmtree", "os.rmdir", "os.remove", "os.unlink", "os.replace", "shutil"):
        assert forbidden not in body, forbidden


def test_the_batch_reads_completed_clips_from_the_callback_not_the_return_value(gui_tree):
    """`export_segments` raises on a stop and an exception discards its return, so the callback's
    list is the only record of what reached disk -- the V1 R1 finding, which applies per source
    here."""
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "on_complete=written.append" in body
    assert "results =" not in body, "the discarded return value must not be the source of truth"


def test_the_item_directory_is_only_created_once_there_is_something_to_export(gui_tree):
    """A source with no usable shots must not leave an empty folder behind."""
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    no_clips_at = body.index("OUTCOME_NO_CLIPS")
    prepare_at = body.index("prepare_item_directory")
    assert no_clips_at < prepare_at, "the zero-clip refusal must come first"


def test_the_item_directory_refuses_to_reuse_an_existing_folder(export_tree):
    body = _code_only(_func(export_tree, "prepare_item_directory"))
    assert "exist_ok=False" in body, "the leaf creation must be the test, not a separate check"
    assert "commonpath" in body, "containment must be measured, not assumed from a prefix"


def test_the_batch_directory_refuses_to_reuse_an_existing_folder(export_tree):
    body = _code_only(_func(export_tree, "prepare_batch_directory"))
    assert "exist_ok=False" in body


def test_the_batch_adds_no_transition_detection(gui_tree, export_tree):
    """The product decision: stop pursuing automatic dissolve detection. A missed transition is an
    accepted V1 limitation, not something batch mode quietly tries to fix."""
    for tree, label in ((gui_tree, "gui"), (export_tree, "export")):
        rendered = _code_only(tree)
        for forbidden in ("dissolve", "blend_linearity", "contrast_dip", "resid"):
            assert forbidden not in rendered, f"{label} reaches {forbidden}"


def test_the_batch_reuses_the_v1_planner_and_detector_unchanged(gui_tree):
    body = _code_only(_func(gui_tree, "_on_cut_batch_run"))
    assert "fork_cutter.plan_segments" in body
    assert "cutter_export.detect_scenes" in body
    assert "cutter_export.probe_source" in body
    assert "cutter_export.export_segments" in body
    assert "scenes.succeeded" in body, "a failed detector is never a successful zero-cut analysis"
    assert "detect_video_scene_changes" not in body, "the conflating detector must not be used"


def test_no_two_fork_imports_share_an_alias(gui_tree):
    """A real bug this caught during development, worth pinning permanently.

    `raw_cutter_batch` was first imported as `fork_batch` -- which `variant_batch` already owns.
    The later binding silently SHADOWED Variant Lab's module, so every C3 multi-candidate path
    would have failed at runtime on an attribute that no longer existed. Nothing about the Raw
    Cutter would have looked wrong.

    `gui.py` aliases fourteen-odd fork modules, so the next collision is a rename away. This
    asserts the aliases are distinct as a set rather than checking the one pair that broke.
    """
    aliases: dict[str, list[str]] = {}
    for node in gui_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "beatsync_fork":
            for alias in node.names:
                aliases.setdefault(alias.asname or alias.name, []).append(alias.name)
    duplicated = {name: sources for name, sources in aliases.items() if len(sources) > 1}
    assert not duplicated, f"these aliases are bound twice, shadowing: {duplicated}"
    assert aliases.get("fork_cutter_batch") == ["raw_cutter_batch"]
    assert aliases.get("fork_batch") == ["variant_batch"]
    assert aliases.get("fork_cutter") == ["raw_cutter"]


def test_the_adapter_is_still_imported_inside_the_handlers(gui_tree):
    top_level = {alias.name for node in gui_tree.body if isinstance(node, ast.Import)
                 for alias in node.names}
    assert "raw_cutter_export" not in top_level
    for handler in ("_on_cut_batch_run", "_on_cut_batch_count"):
        assert "import raw_cutter_export" in ast.unparse(_func(gui_tree, handler))
