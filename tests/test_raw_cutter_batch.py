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
    """Order inside the outer `finally`, read in document order rather than by top-level index.

    The flat-index version of this test read `finalbody` one statement at a time, so wrapping the
    block in a `try/finally` -- which R2 did, so that a finalisation fault can never skip the
    release -- collapsed all three calls into statement 0 and compared `0 < 0`. The guarantee was
    untouched; only the measurement broke. Walking the body recursively measures the same
    ordering without caring how it is nested. `test_the_cleanup_order_survives_a_finalisation_
    fault` then proves it by execution.
    """
    finallies = [n for n in ast.walk(_func(gui_tree, "_on_cut_batch_run"))
                 if isinstance(n, ast.Try) and n.finalbody]
    assert finallies
    ordered = []
    for statement in finallies[0].finalbody:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                ordered.append((node.lineno, node.col_offset, ast.unparse(node)))
    ordered.sort()
    rendered = [text for _line, _col, text in ordered]
    release_at = next(i for i, s in enumerate(rendered) if "_RENDER_LOCK.release()" in s)
    for token in ("_mark_lock_holder('')", "_clear_cutter_job()"):
        at = next(i for i, s in enumerate(rendered) if token in s)
        assert at < release_at, (token, rendered)


def test_the_batch_names_cancellation_before_the_generic_handler(gui_tree):
    """`CutterCancelled` is an ordinary Exception, so a generic clause first would report the
    operator's own Stop as a crash."""
    handler = _func(gui_tree, "_on_cut_batch_run")
    tries = [n for n in ast.walk(handler) if isinstance(n, ast.Try) and n.handlers]
    outer = max(tries, key=lambda n: len(n.handlers))
    caught = [ast.unparse(h.type) if h.type else "bare" for h in outer.handlers]
    assert caught[0].endswith("CutterCancelled"), caught
    assert "Exception" in caught[-1], caught


def test_every_terminal_path_finalises_through_one_shared_helper(gui_tree):
    """Structure only -- the behaviour is proved by execution in section 9.

    This replaces a test that asserted `'write_batch_manifest' in <the finally body>`. That
    assertion passed against the implementation R1 measured as writing a manifest covering ONE of
    three discovered sources, and as writing none at all when the fault landed before the first
    record -- because the string was present either way. A substring cannot see a guard, and
    `if batch_directory and items:` was the guard that made the test's own name false.
    """
    handler = _func(gui_tree, "_on_cut_batch_run")
    finalise = next((n for n in ast.walk(handler)
                     if isinstance(n, ast.FunctionDef) and n.name == "finalise"), None)
    assert finalise is not None, "the single finalisation helper is gone"
    body = _code_only(finalise)
    assert "write_batch_manifest" in body
    assert "manifest_payload" in body
    assert "OUTCOME_NOT_ATTEMPTED" in body, "the backfill must live with the manifest write"

    # Every terminal path delegates to it, so no path can forget one half of finalisation.
    calls = [n for n in ast.walk(handler) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "finalise"]
    assert len(calls) >= 4, f"expected normal + cancelled + fatal + the net, got {len(calls)}"

    # And the manifest is no longer written from anywhere else in the handler.
    outer = [n for n in ast.walk(handler) if isinstance(n, ast.Call)
             and "write_batch_manifest" in ast.unparse(n)]
    assert all(call.lineno >= finalise.lineno and call.lineno <= finalise.end_lineno
               for call in outer), "write_batch_manifest escaped the one finalisation helper"


# =============================================================================================
# 8. One press, no confirmation, and the single-video workflow untouched
# =============================================================================================

def test_start_batch_is_one_click_with_no_confirmation_step(gui_tree, gui_source):
    assert gui_source.count("cut_batch_start_btn.click(") == 1
    kwargs = _kwargs(_click(gui_tree, "cut_batch_start_btn"))
    # [FORK] Raw Cutter progress P1: the click now enters the presentation wrapper, which adds the
    # progress panel and the button states and delegates to the reviewed `_on_cut_batch_run`
    # below. The guarantee this test exists for is untouched -- ONE click, no `.then(...)` chain,
    # no confirmation step -- and the wrapper is asserted to call the real batch exactly once.
    assert ast.unparse(kwargs["fn"]) == "_on_cut_batch_run_ui"
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    assert wrapper.count("_on_cut_batch_run(") == 1, "the wrapper must run the batch exactly once"
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
    run = _kwargs(_click(gui_tree, "cut_run_btn"))
    assert ast.unparse(run["fn"]) == "_on_cut_run"
    assert _names(run["inputs"]) == ["cut_plan_state", "cut_source", "cut_output",
                                     "cut_sensitivity", "cut_min_seconds", "cut_max_seconds",
                                     "cut_remove_audio"]
    # [FORK] Raw Cutter progress P1: this test's purpose is that BATCH work did not disturb the
    # single-video workflow, so it pins the original widgets as a PREFIX that is still written in
    # the original order -- nothing removed, nothing reordered. The progress panel and the three
    # button states are the deliberate P1 additions, and they are listed explicitly rather than
    # waved through, so an accidental fourth addition would still fail here.
    assert _names(analyze["outputs"]) == ["cut_summary", "cut_table", "cut_plan_state",
                                          "cut_source_info",
                                          "cut_progress", "cut_analyze_btn", "cut_run_btn",
                                          "cut_cancel_btn"]
    assert _names(run["outputs"]) == ["cut_report",
                                      "cut_progress", "cut_analyze_btn", "cut_run_btn",
                                      "cut_cancel_btn"]
    # And neither handler may reach outside the Raw Cutter: no render, source-confirmation,
    # Media Preparation or Qwen widget may appear in either list.
    forbidden = {"status_output", "video_output", "process_btn", "source_state", "source_report",
                 "confirm_btn", "confirm_status", "prep_status", "prep_report", "verify_report"}
    for kwargs in (analyze, run):
        assert not (set(_names(kwargs["outputs"])) & forbidden), _names(kwargs["outputs"])


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
    for widget, handler in (("cut_batch_start_btn", "_on_cut_batch_run_ui"),
                            ("cut_batch_count_btn", "_on_cut_batch_count"),
                            ("cut_batch_cancel_btn", "_on_cut_batch_cancel")):
        inputs = _kwargs(_click(gui_tree, widget))["inputs"]
        names = [ast.unparse(e) for e in inputs.elts]
        args = _func(gui_tree, handler).args
        # [FORK] Raw Cutter progress P1: Gradio supplies only the positional inputs, so compare
        # against the parameters that have NO default. `_on_cut_batch_run` gained an optional
        # output-only `sink=None`, which Gradio neither knows about nor supplies.
        required = [a.arg for a in args.args[:len(args.args) - len(args.defaults)]]
        assert len(names) == len(required), (widget, names, required)
    # The wrapper and the batch it delegates to must still agree on the positional arguments.
    ui = _func(gui_tree, "_on_cut_batch_run_ui").args
    core = _func(gui_tree, "_on_cut_batch_run").args
    ui_required = [a.arg for a in ui.args[:len(ui.args) - len(ui.defaults)]]
    core_required = [a.arg for a in core.args[:len(core.args) - len(core.defaults)]]
    assert ui_required == core_required, (ui_required, core_required)


def test_the_batch_generator_always_yields_both_of_its_two_outputs(gui_tree):
    """Every yield must be exactly as wide as the outputs it writes, or Gradio writes the wrong
    widget. This is the defect class that shipped a 3-tuple against 4 outputs in V1's analyze
    handler.

    [FORK] Raw Cutter progress P1: there are now TWO arities to pin, and both still matter.
    `_on_cut_batch_run` -- the reviewed batch -- keeps yielding exactly 2-tuples, which is what
    makes it safe to delegate to. The wrapper that Gradio actually calls writes 6 widgets, and
    every one of its yields must be 6 wide.
    """
    core = _func(gui_tree, "_on_cut_batch_run")
    core_yields = [n for n in ast.walk(core) if isinstance(n, ast.Yield)]
    assert core_yields, "the batch must stream progress"
    for node in core_yields:
        assert isinstance(node.value, ast.Tuple), ast.unparse(node)
        assert len(node.value.elts) == 2, ast.unparse(node)

    outputs = _kwargs(_click(gui_tree, "cut_batch_start_btn"))["outputs"]
    assert _names(outputs) == ["cut_batch_status", "cut_batch_summary", "cut_batch_progress",
                               "cut_batch_count_btn", "cut_batch_start_btn",
                               "cut_batch_cancel_btn"]
    wrapper = _func(gui_tree, "_on_cut_batch_run_ui")
    wrapper_yields = [n for n in ast.walk(wrapper) if isinstance(n, ast.Yield)]
    assert wrapper_yields, "the wrapper must stream progress"
    for node in wrapper_yields:
        # `(status, summary, panel) + <three button updates>` == 6.
        assert isinstance(node.value, ast.BinOp), ast.unparse(node)
        assert len(node.value.left.elts) == 3, ast.unparse(node)
        assert len(outputs.elts) == 6


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
    # [FORK] Raw Cutter progress P1: Cancel Batch also writes the progress panel so it can say
    # "cancellation requested". Its status box stays FIRST and it still writes nothing outside
    # the batch's own two read-only widgets.
    assert _names(kwargs["outputs"]) == ["cut_batch_status", "cut_batch_progress"]
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


def test_unattempted_sources_are_recorded_on_every_terminal_path(gui_tree):
    """Structure only -- the behaviour is proved by execution in section 9.

    The previous version of this test was `assert "OUTCOME_NOT_ATTEMPTED" in body`. The identifier
    was in the body the whole time R1 was measuring sources silently vanishing from the manifest,
    because the backfill sat inside the `try` and the fault jumped over it. Presence of a name is
    not execution of a loop. What is checkable here is that the backfill lives in the one helper
    every terminal path calls; `test_a_fatal_error_*` below measures the result.
    """
    handler = _func(gui_tree, "_on_cut_batch_run")
    finalise = next(n for n in ast.walk(handler)
                    if isinstance(n, ast.FunctionDef) and n.name == "finalise")
    assert "OUTCOME_NOT_ATTEMPTED" in _code_only(finalise)
    # Nowhere else: a second backfill loop would be a second place to forget.
    others = [n for n in ast.walk(handler)
              if isinstance(n, ast.Attribute) and n.attr == "OUTCOME_NOT_ATTEMPTED"
              and not (finalise.lineno <= n.lineno <= finalise.end_lineno)]
    assert not others, "the `not_attempted` backfill must exist in exactly one place"


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


# =============================================================================================
# 9. [R2] The terminal paths, EXECUTED
# =============================================================================================
# Everything above reaches `_on_cut_batch_run` with `ast`, which is the right tool for structure:
# "is cancellation named before the generic handler", "is the lock taken exactly once". It is the
# wrong tool for the question the R1 independent review actually asked -- what does the handler
# DO on its way out when something fails -- and answering that question with substring checks is
# how three reporting defects shipped under green tests.
#
# So this section runs the real body. The export adapter is synthesised rather than imported:
# `raw_cutter_export` pulls in `ffmpeg_processing` and `gpu_cpu_utils` at module scope, the second
# of which probes FFmpeg on import, and neither belongs in a bare-interpreter suite. The manifest
# writer here writes real JSON into a real `tmp_path`, so manifest completeness is measured from
# the file on disk rather than from the arguments the handler passed.

import datetime as _datetime
import json as _json
import sys as _sys
import threading as _threading
import time as _time
import types as _types


class _FakeCancelled(Exception):
    """Stands in for `raw_cutter_export.CutterCancelled` -- an ordinary Exception, as there."""


class _FakeJob:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Res:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _BatchRun:
    """What one executed batch did: on disk, on screen, and to the mutex."""

    def __init__(self, yields, batch_dir, manifest, clips, events, lock, released):
        self.yields, self.batch_dir, self.manifest = yields, batch_dir, manifest
        self.clips, self.events, self.lock, self.released = clips, events, lock, released

    @property
    def status(self):
        return self.yields[-1][0] if self.yields else ""

    @property
    def summary(self):
        return self.yields[-1][1] if self.yields else ""

    @property
    def headline(self):
        return self.summary.splitlines()[0] if self.summary else ""

    @property
    def outcomes(self):
        return [s["outcome"] for s in self.manifest["sources"]] if self.manifest else []

    @property
    def escaped(self):
        return [y for y in self.yields if y[0] == "__ESCAPED__"]


def _run_batch(tmp_path, monkeypatch, *, sources=("a.mp4", "b.mp4", "c.mp4"),
               probe_fault=None, export_hook=None, manifest_error="", manifest_raises=False,
               prelock=False):
    """Execute the real `_on_cut_batch_run` against a synthesised adapter and a real filesystem."""
    handler_node = _strip_docstrings(_func(_tree(_GUI), "_on_cut_batch_run"))
    module = ast.fix_missing_locations(ast.Module(body=[handler_node], type_ignores=[]))
    namespace: dict = {}
    exec(compile(module, _GUI, "exec"), namespace)          # noqa: S102 - the body under test

    tmp_path.mkdir(parents=True, exist_ok=True)
    src_root = tmp_path / "in"
    src_root.mkdir()
    for name in sources:
        (src_root / name).write_bytes(b"\0" * 2048)
    out_root = tmp_path / "out"
    events: list = []

    exporter = _types.ModuleType("raw_cutter_export")
    exporter.CutterCancelled = _FakeCancelled
    exporter.CutterJob = _FakeJob

    def discover_batch_sources(folder, recursive):
        found = sorted(str(src_root / n) for n in sources)
        return _Res(ok=True, root=str(src_root), paths=tuple(found), rejected_count=0, error="")

    def resolve_batch_roots(a, b):
        return str(src_root), str(out_root), ""

    def prepare_batch_directory(root, stamp):
        target = os.path.join(root, "RawCutter_Batch_" + str(stamp))
        os.makedirs(target, exist_ok=False)
        return target, ""

    def prepare_item_directory(batch_directory, parts):
        target = os.path.join(batch_directory, *parts)
        os.makedirs(target, exist_ok=False)
        return target, ""

    def probe_source(path):
        events.append("probe:" + os.path.basename(path))
        if probe_fault:
            probe_fault(path)
        return _Res(ok=True, error="", source=_Res(path=path, duration=60.0))

    def detect_scenes(path, threshold, job=None):
        events.append("detect:" + os.path.basename(path))
        return _Res(succeeded=True, times=[10.0, 20.0, 30.0, 40.0], error="")

    def export_segments(source=None, plan=None, run_directory=None, remove_audio=True,
                        encoder="", job=None, on_complete=None):
        events.append("export:" + os.path.basename(source.path))
        if export_hook:
            return export_hook(source, plan, run_directory, job, on_complete)
        for segment in plan.segments:
            target = os.path.join(run_directory, "clip_%03d.mp4" % segment.index)
            with open(target, "wb") as handle:
                handle.write(b"\0" * 1024)
            on_complete(_Res(index=segment.index, ok=True, size_bytes=1024, error=""))
        return None

    def write_batch_manifest(batch_directory, payload):
        events.append("manifest")
        if manifest_raises:
            raise RuntimeError("injected unexpected finalisation error")
        if manifest_error:
            return manifest_error
        with open(os.path.join(batch_directory, batch.MANIFEST_FILENAME), "w",
                  encoding="utf-8") as handle:
            _json.dump(payload, handle)
        return ""

    for _name, _value in dict(
            discover_batch_sources=discover_batch_sources, resolve_batch_roots=resolve_batch_roots,
            prepare_batch_directory=prepare_batch_directory,
            prepare_item_directory=prepare_item_directory, probe_source=probe_source,
            detect_scenes=detect_scenes, export_segments=export_segments,
            write_batch_manifest=write_batch_manifest).items():
        setattr(exporter, _name, _value)
    monkeypatch.setitem(_sys.modules, "raw_cutter_export", exporter)

    lock = _threading.Lock()
    if prelock:
        lock.acquire()
    released = []

    class _Lock:
        def acquire(self, blocking=False):
            return lock.acquire(blocking=blocking)

        def release(self):
            released.append(True)
            lock.release()

    namespace.update(
        os=os, time=_time, datetime=_datetime, List=list, Any=object,
        fork_cutter=cutter, fork_cutter_batch=batch, _RENDER_LOCK=_Lock(),
        _mark_lock_holder=lambda token: events.append("holder:" + (token or "cleared")),
        _LOCK_HOLDER_CUTTER="cutter",
        _install_cutter_job=lambda job: events.append("job:installed"),
        _clear_cutter_job=lambda: events.append("job:cleared"),
        _cut_busy_message=lambda: "BUSY", NVENC_AVAILABLE=False,
        CUT_BATCH_DONE_STATUS="DONE", CUT_BATCH_CANCELLED_STATUS="CANCELLED")

    yields = []
    try:
        for value in namespace["_on_cut_batch_run"](
                str(src_root), False, str(out_root), 50, 1.0, 8.0, True):
            yields.append(value)
    except BaseException as exc:                              # noqa: BLE001 - must be observable
        yields.append(("__ESCAPED__", type(exc).__name__ + ": " + str(exc)))

    batch_dir = ""
    if out_root.exists():
        batch_dir = next((str(p) for p in sorted(out_root.iterdir())
                          if p.name.startswith("RawCutter_Batch_")), "")
    manifest = None
    if batch_dir and os.path.exists(os.path.join(batch_dir, batch.MANIFEST_FILENAME)):
        with open(os.path.join(batch_dir, batch.MANIFEST_FILENAME), encoding="utf-8") as handle:
            manifest = _json.load(handle)
    clips = []
    if batch_dir:
        clips = [os.path.join(b, f) for b, _d, fs in os.walk(batch_dir)
                 for f in fs if f.endswith(".mp4")]
    if prelock and lock.locked():
        lock.release()
    return _BatchRun(yields, batch_dir, manifest, clips, events, lock, len(released))


def _assert_complete_manifest(run, discovered):
    """The invariant the whole manifest exists to carry."""
    assert run.manifest is not None, "no manifest was written at all"
    assert len(run.manifest["sources"]) == discovered
    assert run.manifest["totals"]["discovered"] == discovered
    assert [s["index"] for s in run.manifest["sources"]] == list(range(1, discovered + 1))


# --- the control arm: without this the fault cases below prove nothing -----------------------

def test_executed_normal_batch_is_complete_and_consistent(tmp_path, monkeypatch):
    run = _run_batch(tmp_path, monkeypatch)
    _assert_complete_manifest(run, 3)
    assert run.outcomes == [batch.OUTCOME_COMPLETED] * 3
    assert run.manifest["cancelled"] is False
    assert run.manifest["terminal_status"] == batch.TERMINAL_COMPLETED
    assert run.manifest["totals"]["clips_written"] == len(run.clips)
    assert run.status == "DONE"
    assert run.headline.endswith("Batch finished")
    assert run.released == 1


# --- F-1: the manifest survives a fault, complete --------------------------------------------

def test_a_fatal_error_before_the_first_record_still_writes_a_complete_manifest(
        tmp_path, monkeypatch):
    """R1 measured: no manifest at ALL, and three sources absent from every artifact."""
    def boom(path):
        raise RuntimeError("injected: first probe")

    run = _run_batch(tmp_path, monkeypatch, probe_fault=boom)
    _assert_complete_manifest(run, 3)
    assert run.outcomes == [batch.OUTCOME_UNEXPECTED_ERROR,
                            batch.OUTCOME_NOT_ATTEMPTED, batch.OUTCOME_NOT_ATTEMPTED]
    assert run.released == 1


def test_a_fatal_error_after_one_source_records_all_three(tmp_path, monkeypatch):
    """R1 measured `len(sources) == 1` while `totals.discovered == 3` -- invariant violated."""
    def boom(path):
        if os.path.basename(path) == "b.mp4":
            raise RuntimeError("injected: second source")

    run = _run_batch(tmp_path, monkeypatch, probe_fault=boom)
    _assert_complete_manifest(run, 3)
    assert run.outcomes == [batch.OUTCOME_COMPLETED, batch.OUTCOME_UNEXPECTED_ERROR,
                            batch.OUTCOME_NOT_ATTEMPTED]
    # The source that WAS opened is never laundered into "we never touched it".
    assert run.manifest["sources"][1]["outcome"] != batch.OUTCOME_NOT_ATTEMPTED
    assert "injected: second source" in run.manifest["sources"][1]["error"]


# --- F-2: a crash is never dressed up as the operator's own Stop ------------------------------

def test_an_unexpected_failure_is_never_reported_as_a_cancellation(tmp_path, monkeypatch):
    """R1 measured a three-way contradiction: the status said error, the headline said
    "Batch cancelled", and the manifest beside them said `cancelled: false`."""
    def boom(path):
        raise RuntimeError("injected: fatal")

    run = _run_batch(tmp_path, monkeypatch, probe_fault=boom)
    assert "cancel" not in run.headline.lower(), run.headline
    assert run.manifest["cancelled"] is False
    assert run.manifest["terminal_status"] == batch.TERMINAL_FAILED
    assert "injected: fatal" in run.manifest["fatal_error"]
    assert "injected: fatal" in run.summary


def test_the_four_terminal_classifications_are_distinguishable(tmp_path, monkeypatch):
    """Normal, cancellation and fatal must not collapse into each other."""
    def cancel_after_first(source, plan, run_directory, job, on_complete):
        for segment in plan.segments:
            with open(os.path.join(run_directory, "c%d.mp4" % segment.index), "wb") as handle:
                handle.write(b"\0")
            on_complete(_Res(index=segment.index, ok=True, size_bytes=1, error=""))
        if os.path.basename(source.path) == "a.mp4":
            job.cancel()
        return None

    def boom(path):
        raise RuntimeError("injected: fatal")

    normal = _run_batch(tmp_path / "n", monkeypatch)
    cancelled = _run_batch(tmp_path / "c", monkeypatch, export_hook=cancel_after_first)
    fatal = _run_batch(tmp_path / "f", monkeypatch, probe_fault=boom)
    assert normal.manifest["terminal_status"] == batch.TERMINAL_COMPLETED
    assert cancelled.manifest["terminal_status"] == batch.TERMINAL_CANCELLED
    assert fatal.manifest["terminal_status"] == batch.TERMINAL_FAILED
    assert len({normal.headline, cancelled.headline, fatal.headline}) == 3
    assert cancelled.manifest["cancelled"] is True
    assert fatal.manifest["cancelled"] is False


# --- F-3: published clips are never erased from the accounting --------------------------------

def test_clips_published_before_a_fatal_export_fault_stay_counted(tmp_path, monkeypatch):
    """R1 measured 2 real files on disk reported as `Clips exported 0`, with the source absent
    from every artifact."""
    def publish_two_then_raise(source, plan, run_directory, job, on_complete):
        for segment in plan.segments[:2]:
            with open(os.path.join(run_directory, "c%d.mp4" % segment.index), "wb") as handle:
                handle.write(b"\0" * 64)
            on_complete(_Res(index=segment.index, ok=True, size_bytes=64, error=""))
        raise OSError("injected: export fault after 2 published clips")

    run = _run_batch(tmp_path, monkeypatch, export_hook=publish_two_then_raise)
    _assert_complete_manifest(run, 3)
    first = run.manifest["sources"][0]
    assert first["clips_written"] == 2, "a published, validated clip was lost from the record"
    assert len(run.clips) == 2, "the clips must still be on disk"
    assert run.manifest["totals"]["clips_written"] == len(run.clips)
    assert first["outcome"] == batch.OUTCOME_PARTIAL
    assert run.manifest["terminal_status"] == batch.TERMINAL_FAILED
    assert run.outcomes[1:] == [batch.OUTCOME_NOT_ATTEMPTED] * 2


def test_a_fault_after_the_final_clip_loses_no_clip_accounting(tmp_path, monkeypatch):
    def publish_all_then_raise(source, plan, run_directory, job, on_complete):
        for segment in plan.segments:
            with open(os.path.join(run_directory, "c%d.mp4" % segment.index), "wb") as handle:
                handle.write(b"\0" * 64)
            on_complete(_Res(index=segment.index, ok=True, size_bytes=64, error=""))
        raise OSError("injected: fault after the final clip")

    run = _run_batch(tmp_path, monkeypatch, export_hook=publish_all_then_raise)
    _assert_complete_manifest(run, 3)
    assert run.manifest["totals"]["clips_written"] == len(run.clips)
    assert len(run.clips) > 0
    assert run.manifest["terminal_status"] == batch.TERMINAL_FAILED


# --- F-4: the manifest claim reports rather than predicts -------------------------------------

_SAVED = "A full per-source record was written"


def test_a_manifest_write_failure_is_warned_about_and_never_claimed_as_saved(
        tmp_path, monkeypatch):
    """R1 measured 33 clips written, no manifest file, and the report asserting it was saved."""
    run = _run_batch(tmp_path, monkeypatch, manifest_error="disk is full")
    assert run.manifest is None, "the harness should have suppressed the write"
    assert _SAVED not in run.summary, "the report claimed a manifest that does not exist"
    assert "could NOT be saved" in run.summary
    assert "disk is full" in run.summary
    # ...and the clips are still reported as the successes they are.
    assert run.status == "DONE"
    assert run.headline.endswith("Batch finished")
    assert len(run.clips) > 0


def test_a_successful_manifest_write_is_still_reported_as_saved(tmp_path, monkeypatch):
    """The negative control for the test above: the claim must remain when it is true."""
    run = _run_batch(tmp_path, monkeypatch)
    assert _SAVED in run.summary
    assert "could NOT be saved" not in run.summary


def test_the_manifest_is_persisted_before_the_terminal_result_is_emitted(tmp_path, monkeypatch):
    """Order of real operations, not the presence of a call: the operator must never be handed a
    final result that is still waiting on persistence."""
    run = _run_batch(tmp_path, monkeypatch)
    assert "manifest" in run.events
    assert run.events.index("manifest") < run.events.index("job:cleared")
    assert run.status == "DONE" and run.summary


# --- cleanup can never be skipped --------------------------------------------------------------

def test_the_cleanup_order_survives_a_finalisation_fault(tmp_path, monkeypatch):
    """A finalisation fault used to propagate out of `finally`, skipping the release and
    stranding the one process-global mutex for the lifetime of the app."""
    run = _run_batch(tmp_path, monkeypatch, manifest_raises=True)
    assert not run.escaped, "the fault escaped to Gradio: %s" % (run.escaped,)
    assert run.released == 1, "the mutex was not released"
    assert not run.lock.locked(), "the mutex is still held"
    assert "job:cleared" in run.events
    assert run.events.index("job:cleared") < len(run.events) - 1
    assert run.events[-1] == "holder:cleared"
    assert _SAVED not in run.summary


def test_a_refused_batch_releases_nothing_and_creates_nothing(tmp_path, monkeypatch):
    run = _run_batch(tmp_path, monkeypatch, prelock=True)
    assert run.status == "BUSY"
    assert run.batch_dir == ""
    assert run.released == 0, "a refused batch released a lock it never acquired"
    assert "probe:a.mp4" not in run.events
