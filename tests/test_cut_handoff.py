"""R11: Raw Cutter → "Review these clips", the finished job's exact-directory handoff.

Three tiers, in the order this repository already uses them:

1. **The pure record**, `beatsync_fork.cut_handoff`, imported and tested as ordinary code. It owns
   the one gate that matters -- a handoff exists only when clips really reached disk -- so that
   gate is tested directly rather than through the UI.
2. **The GUI seam**, by `ast` over the real `src/gui.py`: which widget the button reads, which
   outputs each handler declares, and the structural absence of a scan, a probe, a file move or a
   process-global handoff path.
3. **The real `_on_cut_run`, executed**, against a synthesised exporter and a real temporary
   filesystem. There was no executable harness for the single-video handler before R11; the
   handoff's terminal-path behaviour (success, partial, cancelled, crashed, refused) is exactly
   the kind of property that an AST assertion can only approximate, so one is built here.
"""

from __future__ import annotations

import ast
import json as _json
import os
import queue as _queue
import sys as _sys
import threading as _threading
import time as _time
import types as _types

import pytest

from beatsync_fork import clips_review as fork_clips
from beatsync_fork import cut_handoff as handoff
from beatsync_fork import raw_cutter as cutter
from beatsync_fork import raw_cutter_activity as activity
from beatsync_fork import raw_cutter_batch as fork_batch
from beatsync_fork import raw_cutter_profiles as profiles

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as hand:
        return ast.parse(hand.read(), filename=path)


@pytest.fixture(scope="module")
def gui_tree() -> ast.Module:
    return _tree(_GUI)


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as hand:
        return hand.read()


def _func(tree, name: str) -> ast.FunctionDef:
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
    """Executable source only; the bans below are about machinery, and prose may discuss them."""
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
# 1. The pure record: the published-clip gate is the whole safety property
# =============================================================================================

def test_a_job_that_published_no_clips_offers_nothing():
    """The single most important case, and the reason the gate lives in `build` rather than in
    five terminal paths that each have to remember it.

    A successful-looking job with zero published clips must not offer review of an empty folder.
    Every count at or below zero refuses, including a negative one, which could only arrive from
    a caller that had already got its arithmetic wrong.
    """
    for count in (0, -1, -99):
        assert handoff.build(r"J:\out\run_1", mode=handoff.MODE_SINGLE, clips=count,
                             outcome=handoff.OUTCOME_COMPLETED) is None, count


def test_a_job_with_no_directory_offers_nothing():
    """A run refused before it obtained an output directory has no folder to name, and there is
    deliberately no fallback to the parent -- the parent is what R11 removed."""
    for directory in ("", "   ", None, 0, [], {}):
        assert handoff.build(directory, mode=handoff.MODE_SINGLE, clips=5,
                             outcome=handoff.OUTCOME_COMPLETED) is None, repr(directory)


def test_a_partial_or_cancelled_job_may_offer_review_when_clips_reached_disk():
    """The requirement in its exact form: the outcome does not decide, the clip count does."""
    for outcome in (handoff.OUTCOME_COMPLETED, handoff.OUTCOME_PARTIAL,
                    handoff.OUTCOME_CANCELLED):
        record = handoff.build(r"J:\out\run_1", mode=handoff.MODE_SINGLE, clips=4,
                               outcome=outcome)
        assert record is not None, outcome
        assert record["outcome"] == outcome
        assert record["clips"] == 4


def test_an_unknown_mode_or_outcome_fails_closed():
    """A programming error must not produce a record with an unprintable verdict."""
    assert handoff.build(r"J:\o", mode="sideways", clips=1,
                         outcome=handoff.OUTCOME_COMPLETED) is None
    assert handoff.build(r"J:\o", mode=handoff.MODE_BATCH, clips=1, outcome="finished") is None
    assert handoff.build(r"J:\o", mode=handoff.MODE_BATCH, clips=1, outcome="") is None
    assert handoff.build(r"J:\o", mode=None, clips=1, outcome=None) is None


def test_recursion_follows_the_shape_of_the_job_not_a_preference():
    """Single Video writes its clips into one folder; a batch writes one subfolder per source."""
    assert handoff.recursive_for(handoff.MODE_SINGLE) is False
    assert handoff.recursive_for(handoff.MODE_BATCH) is True
    single = handoff.build(r"J:\out\run", mode=handoff.MODE_SINGLE, clips=2,
                           outcome=handoff.OUTCOME_COMPLETED)
    batch = handoff.build(r"J:\out\batch", mode=handoff.MODE_BATCH, clips=2,
                          outcome=handoff.OUTCOME_COMPLETED)
    assert single["recursive"] is False
    assert batch["recursive"] is True
    # An unknown shape reviews exactly the named folder rather than walking a tree nobody asked
    # about -- the conservative direction for a control whose cost is a library scan.
    assert handoff.recursive_for("sideways") is False


def test_the_record_holds_only_plain_serializable_values():
    """It rides in session state, so nothing live may enter it -- no handle, job, lock or path
    object. Proven by round-tripping through JSON, which accepts none of those."""
    record = handoff.build(r"J:\out\run", mode=handoff.MODE_BATCH, clips=7,
                           outcome=handoff.OUTCOME_PARTIAL)
    assert _json.loads(_json.dumps(record)) == record
    assert set(record) == {"directory", "recursive", "clips", "mode", "outcome"}
    for value in record.values():
        assert isinstance(value, (str, bool, int)), value


def test_a_record_coming_back_from_session_state_is_revalidated_not_trusted():
    """It has been round-tripped through the UI layer, so on the way back in it is untrusted."""
    assert handoff.read(None) is None
    assert handoff.read("J:\\out\\run") is None, "a bare string is not a record"
    assert handoff.read({}) is None
    assert handoff.read({"directory": r"J:\out", "mode": handoff.MODE_SINGLE, "clips": 0,
                         "outcome": handoff.OUTCOME_COMPLETED}) is None, \
        "the published-clip gate must apply to a stored record too"
    good = handoff.build(r"J:\out\run", mode=handoff.MODE_SINGLE, clips=1,
                         outcome=handoff.OUTCOME_COMPLETED)
    assert handoff.read(good) == good


def test_a_tampered_recursive_flag_is_corrected_rather_than_honoured():
    """`read` rebuilds through `build`, so recursion is always the value the MODE implies. A
    client that flipped the flag cannot turn a single-video review into a tree walk."""
    forged = dict(handoff.build(r"J:\out\run", mode=handoff.MODE_SINGLE, clips=3,
                                outcome=handoff.OUTCOME_COMPLETED))
    forged["recursive"] = True
    assert handoff.read(forged)["recursive"] is False


def test_is_offer_is_exactly_the_readability_of_the_record():
    assert handoff.is_offer(None) is False
    assert handoff.is_offer({"directory": r"J:\o", "mode": handoff.MODE_SINGLE, "clips": 0,
                             "outcome": handoff.OUTCOME_COMPLETED}) is False
    assert handoff.is_offer(handoff.build(r"J:\o", mode=handoff.MODE_SINGLE, clips=1,
                                          outcome=handoff.OUTCOME_COMPLETED)) is True


def test_the_wording_tells_the_truth_about_each_outcome():
    """A stopped job must not be described as finished, and a job with failures must not be
    described as clean. The three sentences are deliberately different sentences."""
    def text(outcome):
        return handoff.summary(handoff.build(r"J:\o", mode=handoff.MODE_SINGLE, clips=3,
                                             outcome=outcome))

    completed, partial, cancelled = (text(handoff.OUTCOME_COMPLETED),
                                     text(handoff.OUTCOME_PARTIAL),
                                     text(handoff.OUTCOME_CANCELLED))
    assert len({completed, partial, cancelled}) == 3
    assert "3" in completed and "3" in partial and "3" in cancelled
    assert "did not finish" in partial
    assert "stopped" in cancelled
    assert "stopped" not in completed and "did not finish" not in completed
    # Nothing to describe produces nothing, rather than an empty-looking claim.
    assert handoff.summary(None) == ""
    assert handoff.offer_text(None, "Review these clips") == ""


def test_the_offer_names_the_button_it_is_telling_you_to_press():
    """The label is passed in rather than imported, so the prompt cannot drift from the control
    and the fork module stays free of UI content."""
    record = handoff.build(r"J:\o", mode=handoff.MODE_SINGLE, clips=2,
                           outcome=handoff.OUTCOME_COMPLETED)
    assert "Review these clips" in handoff.offer_text(record, "Review these clips")
    assert "Hand to Review" in handoff.offer_text(record, "Hand to Review")


def test_the_loaded_message_describes_recursion_and_never_claims_a_scan():
    single = handoff.build(r"J:\out\run", mode=handoff.MODE_SINGLE, clips=2,
                           outcome=handoff.OUTCOME_COMPLETED)
    batch = handoff.build(r"J:\out\batch", mode=handoff.MODE_BATCH, clips=9,
                          outcome=handoff.OUTCOME_COMPLETED)
    single_text, batch_text = handoff.loaded_text(single), handoff.loaded_text(batch)
    assert r"J:\out\run" in single_text and "Scan Folder" in single_text
    assert "excluded" in single_text and "included" in batch_text
    for text in (single_text, batch_text):
        for forbidden in ("Enumerated", "Found ", "moved", "renamed", "deleted"):
            assert forbidden not in text, (forbidden, text)
    assert handoff.loaded_text(None) == ""


def test_the_pure_module_imports_nothing_from_the_runtime():
    """The fork package's hard rule, asserted here as well because this module is new."""
    source = _tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "cut_handoff.py"))
    imported = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("logger", "paths", "gradio", "gr", "cupy", "cv2", "librosa", "numpy",
                      "os", "subprocess", "shutil"):
        assert forbidden not in imported, forbidden


# =============================================================================================
# 2. The GUI seam
# =============================================================================================

def test_the_review_button_reads_the_handoff_record_and_not_the_output_widget(gui_tree):
    """The substitution that IS the feature. `cut_output` is the parent folder; reading it is
    what made the button hand over three jobs when the operator asked about one.

    It also closes a hazard: with the output-folder widget as the input, editing that box after a
    job finished silently redirected the finished job's review.
    """
    kwargs = _kwargs(_click(gui_tree, "cut_open_library_btn"))
    assert _names(kwargs["inputs"]) == ["cut_handoff_state"]
    assert ast.unparse(kwargs["fn"]) == "_on_cut_open_in_library"
    handler = _code_only(_func(gui_tree, "_on_cut_open_in_library"))
    assert "cut_output" not in handler
    assert "fork_handoff.read" in handler, "the record must be revalidated, not trusted"


def test_the_handoff_never_falls_back_to_a_folder_it_was_not_given(gui_tree):
    """Requirement F, structurally: no `getcwd`, no `abspath('')`, no parent-of, no output root.

    `abspath` is the exact spelling that caused the defect family PR #83 and PR #84 closed --
    `os.path.abspath('')` is the application's own install folder -- so it is banned here by name
    rather than left to be noticed later.
    """
    handler = _code_only(_func(gui_tree, "_on_cut_open_in_library"))
    for forbidden in ("getcwd", "abspath", "dirname", "ROOT_DIR", "get_output_dir",
                      "expanduser", "expandvars", "_pref("):
        assert forbidden not in handler, f"the handoff reaches {forbidden}"


def test_pressing_review_scans_nothing_and_moves_nothing(gui_tree):
    """The requirement that keeps this cheap. Filling in a folder box must not walk a library,
    probe media, start FFmpeg, transfer a clip or touch a review decision.

    `os.path.isdir` is the one filesystem call and it is deliberately permitted: a single `stat`
    answering "is that folder still there" is what makes the honest report possible, and it is
    not a scan.
    """
    handler = _code_only(_func(gui_tree, "_on_cut_open_in_library"))
    for forbidden in ("listdir", "os.walk", "scandir", "glob", "discover_clips",
                      "discover_batch_sources", "ensure_durations", "subprocess", "Popen",
                      "FFPROBE_PATH", "FFMPEG_PATH", "probe_source", "os.rename", "os.replace",
                      "shutil", "os.remove", "makedirs", "open("):
        assert forbidden not in handler, f"the review handoff reaches {forbidden}"
    assert "os.path.isdir" in handler, "a missing folder must be detected, not assumed"


def test_the_handoff_is_session_state_and_never_a_process_global(gui_tree, gui_source):
    """Two browser sessions must each review their own last job. A module-level path would let
    one operator's cut redirect another's review."""
    assert "cut_handoff_state = gr.State(None)" in gui_source
    # No module-level binding of a handoff anywhere: the record exists only in session state and
    # in the local frames that build it.
    for node in _tree(_GUI).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            assert "handoff" not in node.targets[0].id.lower() or \
                node.targets[0].id == "_CUT_BATCH_HANDOFF", node.targets[0].id
    # And the one module-level name that does mention it is a frozen lookup table of strings,
    # not a mutable slot for a path.
    table = next(n for n in _tree(_GUI).body
                 if isinstance(n, ast.Assign)
                 and getattr(n.targets[0], "id", "") == "_CUT_BATCH_HANDOFF")
    assert isinstance(table.value, ast.Dict)
    for value in table.value.values:
        assert ast.unparse(value).startswith("fork_handoff.OUTCOME_"), ast.unparse(value)


def test_nothing_live_enters_the_handoff_state(gui_tree):
    """The repository-wide ban, applied to the new state."""
    for name in ("_cut_handoff", "_on_cut_open_in_library"):
        rendered = _code_only(_func(gui_tree, name))
        for forbidden in ("CutterJob", "threading", "Lock(", "Popen", "RenderLifecycle",
                          "queue.Queue", "_RENDER_LOCK"):
            assert forbidden not in rendered, (name, forbidden)


def test_the_review_button_starts_unavailable(gui_source):
    """Before a job has written clips there is no particular job to review, so an enabled button
    could only earn a refusal. Video Review's own folder box stays editable, so reviewing still
    never requires having run the cutter."""
    start = gui_source.index("cut_open_library_btn = gr.Button(")
    block = gui_source[start:gui_source.index(")", start)]
    assert "interactive=False" in block


def test_the_batch_terminal_outcome_table_is_keyed_by_the_pure_classifier(gui_source):
    """The review prompt must be a rendering of the SAME `TERMINAL_*` classification the
    headline, the manifest and the activity panel use -- never read off the status text."""
    start = gui_source.index("_CUT_BATCH_HANDOFF = {")
    block = gui_source[start:gui_source.index("}", start)]
    for terminal in ("TERMINAL_FAILED", "TERMINAL_CANCELLED",
                     "TERMINAL_COMPLETED_WITH_FAILURES", "TERMINAL_COMPLETED"):
        assert f"fork_cutter_batch.{terminal}" in block, terminal
    # Every class the pure classifier can produce has a row, so a new one cannot be silently
    # absorbed into a neutral default that happens to look reasonable. The set is read off the
    # fork module itself rather than listed here, so adding a fifth terminal class there fails
    # this test instead of quietly inheriting the default.
    produced = {name for name in dir(fork_batch) if name.startswith("TERMINAL_")}
    assert produced, "the batch module must publish its terminal vocabulary"
    assert len(produced) == block.count("fork_cutter_batch.TERMINAL_"), produced


def test_the_handoff_count_is_never_a_planned_count(gui_tree):
    """`plan.count` is the number of clips the planner INTENDED. Deriving the offer from it is
    how a job whose every clip failed would invite the operator to review an empty folder."""
    run = _func(gui_tree, "_on_cut_run")
    for node in ast.walk(run):
        if (isinstance(node, ast.Call) and ast.unparse(node.func) == "fork_handoff.build"):
            rendered = ast.unparse(node)
            assert "plan.count" not in rendered, rendered
            assert "clips_planned" not in rendered, rendered
    wrapper = _code_only(_func(gui_tree, "_on_cut_batch_run_ui"))
    built = wrapper[wrapper.index("fork_handoff.build"):]
    built = built[:built.index(")")]
    assert "clips_planned" not in built and "plan" not in built, built


def test_the_batch_publishes_its_own_directory_rather_than_the_wrapper_rebuilding_one(gui_tree):
    """The exact directory has to come from the handler that created it. A wrapper that rebuilt
    it from the output-root text plus a timestamp would be guessing, and would be wrong the first
    time the two disagreed.

    Asserted over `ast.unparse` rather than `_code_only` deliberately: `_code_only` blanks string
    literals, which is right for a ban over machinery and useless for a key name -- every
    `sink['...']` would read as `sink['']`.
    """
    core = ast.unparse(_strip_docstrings(_func(gui_tree, "_on_cut_batch_run")))
    assert "sink['directory'] = batch_directory" in core
    assert "sink.pop('directory', None)" in core, \
        "a run that never got a directory must publish none"
    wrapper = ast.unparse(_strip_docstrings(_func(gui_tree, "_on_cut_batch_run_ui")))
    assert "sink.get('directory'" in wrapper
    # Scoped to the BUILD CALL, not to the whole wrapper: the wrapper legitimately receives
    # `output_folder` and forwards it to the core, so banning the name outright would be testing
    # the wrong thing. What must hold is that none of it reaches the record.
    build = next(node for node in ast.walk(_func(gui_tree, "_on_cut_batch_run_ui"))
                 if isinstance(node, ast.Call)
                 and ast.unparse(node.func) == "fork_handoff.build")
    rendered = ast.unparse(build)
    for forbidden in ("output_folder", "output_root", "prepare_batch_directory", "strftime",
                      "os.path.join", "getcwd", "abspath", "dirname"):
        assert forbidden not in rendered, (
            f"the wrapper reconstructs the directory via {forbidden}: {rendered}")


def test_every_running_frame_clears_the_handoff(gui_tree):
    """"Do not present an old job as the result of a new operation", structurally: a frame that
    is not terminal emits the cleared form, so the Review button cannot keep offering the
    previous run's folder while this one is working.

    Counted rather than merely sampled: every `_cut_handoff(...)` call in both handlers is either
    `None` or a built record, and the refusal/progress paths outnumber the terminal ones.
    """
    for name in ("_on_cut_run", "_on_cut_batch_run_ui"):
        body = _code_only(_func(gui_tree, name))
        calls = [n for n in ast.walk(_func(gui_tree, name))
                 if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_cut_handoff"]
        assert calls, name
        arguments = [ast.unparse(c.args[0]) for c in calls]
        assert set(arguments) <= {"None", "handoff"}, (name, arguments)
        assert "None" in arguments, f"{name} never clears the handoff"
        assert body.count("_cut_handoff(None)") >= 1


def test_a_refused_cut_clears_the_previous_jobs_handoff(gui_tree):
    """A refused attempt is still a new operation."""
    refuse = _func(_func(gui_tree, "_on_cut_run"), "refuse")
    assert "_cut_handoff(None)" in ast.unparse(refuse)


# =============================================================================================
# 3. The real `_on_cut_run`, executed
# =============================================================================================

class _FakeCancelled(Exception):
    pass


class _FakeJob:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def raise_if_cancelled(self):
        if self.cancelled:
            raise _FakeCancelled("stopped")


class _Res:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _ui_strings() -> dict:
    """Every `CUT_*` / `LABEL_CUT*` constant, read from the real `src/ui_content.py`, so the
    handler under test renders the wording the application actually ships."""
    path = os.path.join(_REPO_ROOT, "src", "ui_content.py")
    with open(path, encoding="utf-8") as hand:
        source = ast.parse(hand.read(), filename=path)
    out: dict = {}
    for node in source.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if not name.startswith(("CUT_", "LABEL_CUT", "PLACEHOLDER_CUT")):
            continue
        try:
            out[name] = ast.literal_eval(node.value)
        except ValueError:
            continue
    return out


_UI = _ui_strings()


class _CutRun:
    """What one executed single-video cut did, on screen and to the handoff."""

    def __init__(self, yields, run_dir, clips):
        self.yields, self.run_dir, self.clips = yields, run_dir, clips

    # `(report, panel) + _cut_handoff(record) + <three button updates>`: indices 2 and 3.
    @property
    def report(self):
        return self.yields[-1][0] if self.yields else ""

    @property
    def handoff(self):
        return self.yields[-1][2] if self.yields else None

    @property
    def handoffs(self):
        return [y[2] for y in self.yields]

    @property
    def review_enabled(self):
        return self.yields[-1][3].get("interactive") if self.yields else None

    @property
    def widths(self):
        return {len(y) for y in self.yields}


def _run_cut(tmp_path, monkeypatch, *, clip_results=None, raise_after=None, fault=None,
             plan_state=..., prelock=False, run_dir_error="", cancel_at=None):
    """Execute the REAL `_on_cut_run` against a synthesised adapter and a real filesystem.

    There was no executable harness for the single-video handler before R11. It is built the way
    `test_raw_cutter_batch.py` builds the batch's: the shipped function bodies are AST-extracted
    and executed in one namespace against a stub `raw_cutter_export`, so what these cases assert
    about the handoff is the shipped code's own behaviour on each terminal path.
    """
    gui = _tree(_GUI)
    wanted = ["_restore_plan", "_cut_progress_html", "_cut_activity", "_cut_activity_final",
              "_cut_buttons", "_cut_handoff", "_on_cut_run"]
    body = [node for node in gui.body
            if isinstance(node, ast.Assign)
            and getattr(node.targets[0], "id", "") == "_CUT_TONES"]
    body += [_strip_docstrings(_func(gui, name)) for name in wanted]
    namespace: dict = dict(fork_activity=activity, fork_handoff=handoff, fork_cutter=cutter,
                           fork_profiles=profiles)
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    exec(compile(module, _GUI, "exec"), namespace)          # noqa: S102 - the body under test

    tmp_path.mkdir(parents=True, exist_ok=True)
    source_file = tmp_path / "recording.mp4"
    source_file.write_bytes(b"\0" * 4096)
    out_root = tmp_path / "out"
    out_root.mkdir()
    created: dict = {}

    exporter = _types.ModuleType("raw_cutter_export")
    exporter.CutterCancelled = _FakeCancelled
    exporter.CutterJob = _FakeJob

    stat = os.stat(source_file)
    source_info = cutter.SourceInfo(path=str(source_file), duration=60.0, width=1280, height=720,
                                    fps=25.0, has_audio=True, size_bytes=stat.st_size,
                                    mtime_ns=stat.st_mtime_ns)

    def probe_source(path):
        return _Res(ok=True, error="", source=source_info)

    def prepare_run_directory(output_folder, source_path, timestamp, naming=None):
        if run_dir_error:
            return "", run_dir_error
        target = os.path.join(str(out_root), "RawCutter_" + str(timestamp))
        os.makedirs(target, exist_ok=False)
        created["run"] = target
        return target, ""

    def export_segments(source=None, plan=None, run_directory=None, remove_audio=True,
                        encoder="", job=None, on_progress=None, on_complete=None, naming=None,
                        output_profile=None):
        results = []
        for position, segment in enumerate(plan.segments, start=1):
            if cancel_at is not None and position > cancel_at:
                raise _FakeCancelled("stopped")
            if raise_after is not None and position > raise_after:
                raise RuntimeError("injected export fault")
            ok = True if clip_results is None else bool(clip_results[position - 1])
            if ok:
                target = os.path.join(run_directory, f"clip_{segment.index:03d}.mp4")
                with open(target, "wb") as hand:
                    hand.write(b"\0" * 512)
            result = _Res(index=segment.index, ok=ok, size_bytes=512 if ok else 0,
                          path=os.path.join(run_directory, f"clip_{segment.index:03d}.mp4"),
                          error="" if ok else "injected clip failure")
            if on_progress:
                on_progress(position, plan.count, "clip")
            if on_complete:
                on_complete(result)
            results.append(result)
        if fault is not None:
            raise fault
        return results

    for _name, _value in dict(probe_source=probe_source,
                              prepare_run_directory=prepare_run_directory,
                              export_segments=export_segments).items():
        setattr(exporter, _name, _value)
    monkeypatch.setitem(_sys.modules, "raw_cutter_export", exporter)

    lock = _threading.Lock()
    if prelock:
        lock.acquire()

    class _Lock:
        def acquire(self, blocking=False):
            return lock.acquire(blocking=blocking)

        def release(self):
            lock.release()

    namespace.update(
        os=os, time=_time, datetime=__import__("datetime"), threading=_threading, queue=_queue,
        List=list, Any=object, Dict=dict,
        _RENDER_LOCK=_Lock(), _mark_lock_holder=lambda token: None,
        _LOCK_HOLDER_CUTTER="cutter",
        _install_cutter_job=lambda job: None, _clear_cutter_job=lambda: None,
        _cut_busy_message=lambda: "BUSY", NVENC_AVAILABLE=False,
        gr=_types.SimpleNamespace(update=lambda **kw: dict(kw)),
        html_lib=__import__("html"))
    namespace.update(_UI)

    if plan_state is ...:
        plan = cutter.plan_segments(60.0, [10.0, 20.0, 30.0], 1.0, 30.0, 50)
        plan_state = {
            "source": {"path": str(source_file), "duration": 60.0, "width": 1280, "height": 720,
                       "fps": 25.0, "has_audio": True, "size_bytes": stat.st_size,
                       "mtime_ns": stat.st_mtime_ns},
            "segments": [(s.index, s.start, s.end, s.shot_index, s.boundary_kind)
                         for s in plan.segments],
            "shots": [(s.index, s.start, s.end, s.exported, s.skip_reason, s.piece_count)
                      for s in plan.shots],
            "sensitivity": 50, "threshold": plan.threshold, "min_seconds": 1.0,
            "max_seconds": 30.0, "detected": 3,
            "signature": cutter.plan_signature(source_info, 50, 1.0, 30.0),
        }

    yields = []
    for value in namespace["_on_cut_run"](plan_state, str(source_file), str(out_root), 50,
                                          1.0, 30.0, True, False, profiles.SAME_AS_SOURCE):
        yields.append(value)

    run_dir = created.get("run", "")
    clips = sorted(os.listdir(run_dir)) if run_dir and os.path.isdir(run_dir) else []
    if prelock and lock.locked():
        lock.release()
    return _CutRun(yields, run_dir, clips)


def test_a_successful_cut_hands_over_its_own_run_directory(tmp_path, monkeypatch):
    """The feature's main case, measured end to end on the shipped handler: the directory in the
    handoff is the one `prepare_run_directory` actually created, not the parent it sits in."""
    run = _run_cut(tmp_path, monkeypatch)
    assert run.clips, "the harness must really have written clips"
    assert run.handoff is not None
    assert run.handoff["directory"] == run.run_dir
    assert run.handoff["directory"] != str(tmp_path / "out"), "the PARENT must not be handed over"
    assert run.handoff["clips"] == len(run.clips)
    assert run.handoff["outcome"] == handoff.OUTCOME_COMPLETED
    assert run.handoff["recursive"] is False, "a single run writes into one folder"
    assert run.review_enabled is True
    assert _UI["LABEL_CUT_OPEN_LIBRARY"] in run.report


def test_every_frame_writes_exactly_as_many_values_as_the_handler_declares(tmp_path,
                                                                           monkeypatch):
    """Gradio writes the wrong widget rather than failing on a width mismatch, so the width is
    measured on real emitted frames as well as pinned by `ast`."""
    run = _run_cut(tmp_path, monkeypatch)
    assert run.widths == {7}, run.widths


def test_the_handoff_is_cleared_while_the_cut_is_still_working(tmp_path, monkeypatch):
    """Not just at the end: a job in flight must not leave the previous job's folder on offer."""
    run = _run_cut(tmp_path, monkeypatch)
    assert len(run.handoffs) > 1, "the run must have streamed progress"
    assert all(value is None for value in run.handoffs[:-1]), run.handoffs
    assert run.handoffs[-1] is not None


def test_a_cut_whose_every_clip_failed_offers_nothing(tmp_path, monkeypatch):
    """The empty-folder case, on the real handler: the run directory exists, and review is still
    not offered because nothing reached disk."""
    run = _run_cut(tmp_path, monkeypatch, clip_results=[False, False, False, False, False,
                                                        False, False, False])
    assert run.run_dir, "the directory was created"
    assert run.clips == [], "but no clip was written"
    assert run.handoff is None
    assert run.review_enabled is False
    assert _UI["LABEL_CUT_OPEN_LIBRARY"] not in run.report


def test_a_partly_failed_cut_offers_review_and_says_so(tmp_path, monkeypatch):
    run = _run_cut(tmp_path, monkeypatch, clip_results=[True, False, True, True, True,
                                                        True, True, True])
    assert run.handoff is not None
    assert run.handoff["outcome"] == handoff.OUTCOME_PARTIAL
    assert run.handoff["clips"] == len(run.clips)
    assert "did not finish" in run.report
    assert run.review_enabled is True


def test_a_cancelled_cut_offers_only_what_reached_disk(tmp_path, monkeypatch):
    """Cancellation after some clips were written: offered, with cancelled wording and the real
    count -- which on this path comes from the completion callback, because the exception
    discarded `export_segments`' return value."""
    run = _run_cut(tmp_path, monkeypatch, cancel_at=2)
    assert len(run.clips) == 2
    assert run.handoff is not None
    assert run.handoff["outcome"] == handoff.OUTCOME_CANCELLED
    assert run.handoff["clips"] == 2
    assert run.handoff["directory"] == run.run_dir
    assert run.review_enabled is True


def test_a_cut_cancelled_before_any_clip_was_written_offers_nothing(tmp_path, monkeypatch):
    run = _run_cut(tmp_path, monkeypatch, cancel_at=0)
    assert run.clips == []
    assert run.handoff is None
    assert run.review_enabled is False


def test_a_crash_after_some_clips_were_written_still_offers_them(tmp_path, monkeypatch):
    """A fault does not un-write the clips already on disk. `completed` and `run_directory` are
    both bound before the handler's `try`, which is what makes this path safe to report from."""
    run = _run_cut(tmp_path, monkeypatch, raise_after=3)
    assert len(run.clips) == 3
    assert run.handoff is not None
    assert run.handoff["clips"] == 3
    assert run.handoff["outcome"] == handoff.OUTCOME_PARTIAL
    assert run.handoff["directory"] == run.run_dir
    assert "Cutting failed" in run.report


def test_a_crash_before_any_clip_was_written_offers_nothing(tmp_path, monkeypatch):
    run = _run_cut(tmp_path, monkeypatch, raise_after=0)
    assert run.handoff is None
    assert run.review_enabled is False


@pytest.mark.parametrize("kind,kwargs", [
    ("no plan", {"plan_state": None}),
    ("busy", {"prelock": True}),
    ("no output directory", {"run_dir_error": "the output folder is read-only"}),
])
def test_every_refusal_clears_the_handoff_and_disables_review(tmp_path, monkeypatch, kind,
                                                              kwargs):
    """A refused attempt is a new operation, so it must invalidate the previous job's record --
    otherwise the Review button would present an older cut as this one's result."""
    run = _run_cut(tmp_path, monkeypatch, **kwargs)
    assert run.handoffs, kind
    assert all(value is None for value in run.handoffs), (kind, run.handoffs)
    assert run.review_enabled is False, kind


def test_a_second_cut_replaces_the_first_jobs_handoff(tmp_path, monkeypatch):
    """Consecutive jobs: the second run's record must name the second run's directory, and the
    first must be gone from the very first frame of the second."""
    first = _run_cut(tmp_path / "one", monkeypatch)
    second = _run_cut(tmp_path / "two", monkeypatch)
    assert first.handoff["directory"] != second.handoff["directory"]
    assert second.handoffs[0] is None, "the new job clears before it works"
    assert first.handoff["directory"] not in str(second.handoffs)


def test_nothing_in_the_source_tree_was_moved_or_renamed(tmp_path, monkeypatch):
    """The handoff fills in a folder box. The source recording and the written clips must be
    exactly where the cut left them."""
    run = _run_cut(tmp_path, monkeypatch)
    assert (tmp_path / "recording.mp4").exists(), "the source must be untouched"
    assert run.clips == sorted(os.listdir(run.run_dir))
    assert all(name.endswith(".mp4") for name in run.clips)


# =============================================================================================
# 4. F-1: a handoff retires the review page it is navigating away from
#
# The independent R11 review measured this, on the candidate and with real media: scan folder A,
# keep its clips, finish a job into B, press "Review these clips", press Confirm -- and **A** was
# sorted into the Media Library while every visible label named B. Pressing Proceed on a sweep
# that had been armed on A was worse: one of A's clips went to the Library and its two unmarked
# clips went to Discarded.
#
# The cause is not the gallery. Everything actionable about a review page -- the grid, the
# counters, Confirm's admission, an armed sweep's signature, and the decision endpoint -- is
# derived from the coordinator's per-owner view, and the handoff changed the folder box without
# touching it. So the correction retires that view, and the widget updates are consequences.
#
# Two tiers, matching the rest of this file: the coordinator primitive is imported and tested as
# ordinary code, and the seam is pinned by `ast` over the real `src/gui.py`.
# =============================================================================================

_CLIPS_REVIEW_PY = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "clips_review.py")


def test_invalidate_retires_the_owners_page_without_scanning_anything():
    """The primitive F-1's correction is built on. It is a pure state transition: one locked step
    that leaves the owner with no current page at all."""
    coordinator = fork_clips.ScanCoordinator()
    generation = coordinator.begin("owner-one")
    assert coordinator.publish("owner-one", generation, {"entries": ["a"], "root": "A"}) is True
    assert coordinator.view("owner-one").get("entries") == ["a"]

    retired = coordinator.invalidate("owner-one")

    assert coordinator.view("owner-one") == {}, "the page must be gone, not merely stale"
    assert isinstance(retired, int) and not isinstance(retired, bool)
    assert retired > generation, "retiring must take a NEWER generation, not reuse one"
    assert coordinator.newest("owner-one") == retired


def test_an_older_scan_still_in_flight_cannot_restore_the_retired_page():
    """The ordering that makes this safe rather than cosmetic. A scan's poster pass takes seconds,
    so a scan of A can still be running when the handoff lands -- and before the fix its
    `publish` succeeded and put A back as the current page."""
    coordinator = fork_clips.ScanCoordinator()
    inflight = coordinator.begin("owner-one")
    coordinator.invalidate("owner-one")

    assert coordinator.publish("owner-one", inflight, {"entries": ["a"]}) is False
    assert coordinator.view("owner-one") == {}
    # ... and its own failure path cannot blank a page it no longer owns either.
    assert coordinator.discard("owner-one", inflight) is False


def test_a_scan_started_after_the_handoff_behaves_entirely_normally():
    """Retiring a page must not wedge the surface: pressing Scan Folder is still the one
    deliberate way to get a review session, and it must work the first time."""
    coordinator = fork_clips.ScanCoordinator()
    coordinator.publish("owner-one", coordinator.begin("owner-one"), {"entries": ["a"]})
    coordinator.invalidate("owner-one")

    generation = coordinator.begin("owner-one")
    assert coordinator.publish("owner-one", generation, {"entries": ["b"], "root": "B"}) is True
    assert coordinator.view("owner-one")["root"] == "B"


def test_invalidate_touches_exactly_one_owner():
    """Two browser sessions. One operator pressing Review must not blank the other's gallery."""
    coordinator = fork_clips.ScanCoordinator()
    for owner in ("one", "two"):
        coordinator.publish(owner, coordinator.begin(owner), {"entries": [owner]})
    coordinator.invalidate("one")

    assert coordinator.view("one") == {}
    assert coordinator.view("two").get("entries") == ["two"]


def test_retiring_a_session_that_never_scanned_registers_nothing():
    """Found by the focused review of this correction, not by the original finding.

    Unlike `begin`, this is reached by a UI action an operator can take in a browser session that
    has never opened Video Review -- and both of the coordinator's maps are capped, evicting the
    least recently used. Registering a brand-new key would therefore let pressing Review in a
    fresh session drop a *different* session's live page.
    """
    coordinator = fork_clips.ScanCoordinator(max_owners=2)
    for owner in ("one", "two"):
        coordinator.publish(owner, coordinator.begin(owner), {"entries": [owner]})

    assert coordinator.invalidate("a-session-that-never-scanned") == 0

    assert coordinator.view("one").get("entries") == ["one"], "evicted a live page"
    assert coordinator.view("two").get("entries") == ["two"], "evicted a live page"
    assert coordinator.newest("a-session-that-never-scanned") == 0


def test_retiring_still_blocks_a_scan_that_has_begun_but_not_published():
    """The complement of the test above, and the reason the shortcut is keyed on the generation
    map rather than on the stored view: `begin` records the generation *before* a scan changes
    anything, so an owner with a scan in flight has a key even though it has no page yet. That
    owner must still be retired, or the in-flight scan would publish A after the handoff."""
    coordinator = fork_clips.ScanCoordinator()
    inflight = coordinator.begin("owner-one")
    assert coordinator.view("owner-one") == {}, "no page published yet"

    assert coordinator.invalidate("owner-one") > inflight
    assert coordinator.publish("owner-one", inflight, {"entries": ["a"]}) is False


def test_invalidate_refuses_an_unusable_owner_rather_than_inventing_one():
    """Fails closed, like every other owner-keyed call here: there is deliberately no shared
    anonymous slot for one session's retirement to reach into."""
    coordinator = fork_clips.ScanCoordinator()
    for bad in (None, "", 0):
        with pytest.raises(fork_clips.Refusal):
            coordinator.invalidate(bad)


def test_invalidate_is_not_a_decision_and_writes_no_record():
    """It holds pages, not decisions. The durable record lives in the store and must be
    unreachable from here -- an operator's kept/discarded marks survive a handoff."""
    body = _code_only(_func(_tree(_CLIPS_REVIEW_PY), "invalidate"))
    for forbidden in ("store", "journal", "snapshot", "DECISIONS", "SELECTED", "REJECTED",
                      "open(", "os.", "commit", "write"):
        assert forbidden not in body, forbidden


# ---- the seam -------------------------------------------------------------------------------

#: The Review button's declared outputs, in order. The five original positions are unchanged and
#: the seven review surfaces are appended, so a frame cannot silently reorder into them.
_REVIEW_OUTPUTS = ["clips_input", "clips_recursive", "main_tabs", "library_tabs", "clips_report",
                   "clips_gallery", "clips_counts", "clips_confirm_btn",
                   "clips_proceed_btn", "clips_sweep_cancel_btn", "clips_armed_state",
                   "clips_confirm_out"]

#: What each review-surface helper must return. Both are seven wide, because both answer the same
#: seven appended outputs -- one by retiring the page, the other by changing nothing.
_HANDOFF_HELPER_WIDTHS = {"_cut_handoff_retire_review": 7,
                          "_cut_handoff_review_unchanged": 7}


def test_the_review_button_writes_the_surfaces_it_invalidates(gui_tree):
    """Exact equality, not a subset: this is the positional contract the handler's three return
    paths are measured against, and an output appended in the wrong place would misalign it."""
    outputs = _kwargs(_click(gui_tree, "cut_open_library_btn"))["outputs"]
    assert _names(outputs) == _REVIEW_OUTPUTS


def test_each_review_surface_helper_is_seven_wide(gui_tree):
    for name, expected in _HANDOFF_HELPER_WIDTHS.items():
        returns = [n for n in ast.walk(_func(gui_tree, name))
                   if isinstance(n, ast.Return) and n.value is not None]
        assert returns, name
        for node in returns:
            if isinstance(node.value, ast.Tuple):
                assert len(node.value.elts) == expected, ast.unparse(node)
            else:                                  # the generated `tuple(... range(N))` form
                assert str(expected) in ast.unparse(node.value), ast.unparse(node)


def test_the_retirement_writes_the_right_value_into_each_surface(gui_tree):
    """Width is not enough, and a mutation run proved it: swapping the armed-sweep clear for a
    bare `gr.update()` left every other assertion in this file passing. The seven returned
    elements are therefore matched to the seven appended outputs **by position**, and each one's
    shape is pinned.

    Note this reads the raw tree rather than `_code_only`, which blanks string constants -- under
    that transform an empty string and a sentence are the same node.
    """
    appended = _REVIEW_OUTPUTS[5:]
    returns = [n for n in ast.walk(_func(gui_tree, "_cut_handoff_retire_review"))
               if isinstance(n, ast.Return) and n.value is not None]
    assert len(returns) == 1
    elements = returns[0].value.elts
    assert len(elements) == len(appended) == 7
    rendered = dict(zip(appended, (ast.unparse(e) for e in elements)))

    # The grid, the counters and Confirm come straight from the repaint, by name.
    assert rendered["clips_gallery"] == "grid"
    assert rendered["clips_counts"] == "counts"
    assert rendered["clips_confirm_btn"] == "confirm"
    # Both halves of the sweep gate close.
    assert rendered["clips_proceed_btn"] == "gr.update(visible=False)"
    assert rendered["clips_sweep_cancel_btn"] == "gr.update(visible=False)"
    # The arming is VOIDED, not merely left alone. A surviving signature authorises nothing today
    # -- a freshly computed one for another folder could not match it -- but "it would not happen
    # to collide" is not the property the operator was promised.
    assert rendered["clips_armed_state"] == "''"
    # And the gate's prompt goes with the page it was describing.
    assert rendered["clips_confirm_out"] == "CLIPS_CONFIRM_DISABLED_TEXT"


def test_every_handoff_path_is_exactly_as_wide_as_the_declared_outputs(gui_tree):
    """Gradio aligns positionally, so a path returning eleven values where twelve are declared
    writes the wrong widget. Each `return` is flattened with the helper widths resolved."""
    declared = len(_kwargs(_click(gui_tree, "cut_open_library_btn"))["outputs"].elts)
    assert declared == len(_REVIEW_OUTPUTS)
    returns = [n for n in ast.walk(_func(gui_tree, "_on_cut_open_in_library"))
               if isinstance(n, ast.Return) and n.value is not None]
    assert len(returns) == 3, "a record refusal, a vanished folder, and the handoff itself"
    for node in returns:
        width = 0
        stack = [node.value]
        while stack:                               # flatten the `(...) + helper()` spine
            item = stack.pop()
            if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                stack.extend((item.left, item.right))
            elif isinstance(item, ast.Tuple):
                width += len(item.elts)
            elif isinstance(item, ast.Call):
                width += _HANDOFF_HELPER_WIDTHS[ast.unparse(item.func)]
            else:                                  # pragma: no cover - a shape nobody declared
                raise AssertionError(ast.unparse(item))
        assert width == declared, ast.unparse(node)


def test_a_refused_handoff_leaves_the_review_page_exactly_as_it_was(gui_tree):
    """A mis-press must not cost the operator the page they were working on. Both refusals use
    the no-change helper, and that helper writes nothing but a bare update."""
    handler = _code_only(_func(gui_tree, "_on_cut_open_in_library"))
    assert handler.count("_cut_handoff_review_unchanged()") == 2
    assert handler.count("_cut_handoff_retire_review(request)") == 1
    helper = _code_only(_func(gui_tree, "_cut_handoff_review_unchanged"))
    assert "gr.update()" in helper
    for forbidden in ("invalidate", "_on_clips_refresh", "value=", "interactive=", "visible="):
        assert forbidden not in helper, forbidden


def test_the_retirement_goes_through_the_review_coordinator(gui_tree):
    """Reuse, not a second state machine. The authority for what is actionable is the
    coordinator's per-owner view, so that is what is retired -- a flag beside it would leave two
    notions of "the current page" to be kept in step by hand."""
    body = _code_only(_func(gui_tree, "_cut_handoff_retire_review"))
    assert "_CLIPS_SCANS.invalidate(_clips_owner(request))" in body
    assert "_on_clips_refresh(request)" in body, \
        "the empty state must be the one Refresh renders, not a second set of constants"
    for forbidden in ("_CLIPS_SCANS.begin", "_CLIPS_SCANS.discard", "_CLIPS_SCANS.publish",
                      "new_session", "revoke", "_CLIPS_REGISTRY", "global "):
        assert forbidden not in body, forbidden


def test_the_retirement_scans_nothing_moves_nothing_and_decides_nothing(gui_tree):
    """The same ban list the handler itself carries, applied to the helper -- otherwise the
    cheapest route to a side effect would be the function the handler delegates to."""
    body = _code_only(_func(gui_tree, "_cut_handoff_retire_review"))
    for forbidden in ("listdir", "os.walk", "scandir", "glob", "discover_clips", "subprocess",
                      "Popen", "FFPROBE_PATH", "FFMPEG_PATH", "os.rename", "os.replace",
                      "shutil", "os.remove", "makedirs", "open(", "sort_decided_clips",
                      "plan_sort", "_clips_store", "getcwd", "abspath", "ROOT_DIR"):
        assert forbidden not in body, f"the retirement reaches {forbidden}"


def test_a_session_without_identity_does_not_break_the_handoff(gui_tree):
    """`_clips_owner` fails closed, and that refusal must not take the handoff down with it: a
    session with no identity has no page to retire, and filling a folder box never needed one."""
    handlers = [n for n in ast.walk(_func(gui_tree, "_cut_handoff_retire_review"))
                if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 1
    assert ast.unparse(handlers[0].type) == "fork_clips.Refusal", \
        "contain the identity refusal specifically, never a bare except"
    assert all(isinstance(stmt, ast.Pass) for stmt in handlers[0].body)


def test_the_handoff_takes_the_injected_request_and_no_new_widget(gui_tree):
    """The request is INJECTED, never listed in `inputs` -- so the browser gains no new way to
    name an owner, and the button still reads exactly one widget."""
    handler = _func(gui_tree, "_on_cut_open_in_library")
    assert [a.arg for a in handler.args.args] == ["handoff", "request"]
    assert ast.unparse(handler.args.args[1].annotation) == "gr.Request"
    assert _names(_kwargs(_click(gui_tree, "cut_open_library_btn"))["inputs"]) \
        == ["cut_handoff_state"]
