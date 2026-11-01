"""Deliverable Export V1-B-S2 — the Open Output Folder button.

The product owner rejected a browser download in favour of one button that opens this
installation's `output` folder in Windows File Explorer. So the two things worth testing are the
handler's behaviour under every reachable condition, and the absence of everything that was
rejected.

`gui.py` needs the whole portable runtime (`logger` pulls in librosa, `gpu_cpu_utils` probes
NVENC), so it cannot be imported on a bare interpreter — but `_on_open_output_folder_click` depends
on exactly two names, `os` and `gr`, plus module constants from `ui_content`. The REAL body is
therefore AST-extracted from `gui.py` and executed against a synthesised namespace, with `os` and
`gr` shimmed to record what the handler did. Nothing here restates the handler's logic: a test that
re-derived "check platform, check directory, call startfile" would pass against an implementation
that made the same mistake.

What the suite pins:

    * the folder comes from the REAL get_output_dir(), never from render state   -> PATH SOURCE
    * exactly one os.startfile call, on the directory, never on a video         -> THE CALL
    * missing dir / unsupported platform / OS failure are reported truthfully   -> FAILURE MATRIX
    * nothing is created, moved, deleted or overwritten                         -> NO SIDE EFFECTS
    * zero inputs, zero outputs, no new state, no second status writer          -> WIRING
    * nothing that was rejected came back                                       -> REJECTED SURFACE
"""

from __future__ import annotations

import ast
import os
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
_UI = os.path.join(_SRC, "ui_content.py")
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

HANDLER = "_on_open_output_folder_click"
BUTTON = "open_output_folder_btn"


def _source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _tree(path: str) -> ast.Module:
    return ast.parse(_source(path), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree(path))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _code(node: ast.FunctionDef) -> str:
    """The function's EXECUTABLE body, with the docstring removed.

    Load-bearing, not tidiness. These handlers document what they deliberately do *not* touch, so
    they legitimately name `LAST_OUTPUT_PATH_KEY`, `subprocess` and `allowed_paths` in prose — and
    a substring ban run over the whole source would trip on the very explanation of the ban. That
    has now happened repeatedly in this repository; the fix is to ban usage structurally and never
    to stop explaining it.
    """
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    stripped = ast.FunctionDef(
        name=node.name, args=node.args, body=body or [ast.Pass()],
        decorator_list=[], returns=None, type_comment=None, type_params=[],
    )
    return ast.unparse(ast.fix_missing_locations(stripped))


def _comment_free_source(path: str) -> str:
    """The module's source with every comment and docstring removed, for the few checks that
    genuinely need text rather than structure. Same reason as `_code`."""
    return ast.unparse(ast.parse(_source(path)))


# ---------------------------------------------------------------------------
# the real handler, over recording shims
# ---------------------------------------------------------------------------


class _Toasts:
    """Stands in for the `gr` module, recording only the two calls the handler may make."""

    def __init__(self):
        self.info: list[str] = []
        self.warning: list[str] = []

    def Info(self, message, *a, **k):          # noqa: N802  (mirrors gradio's own casing)
        self.info.append(str(message))

    def Warning(self, message, *a, **k):       # noqa: N802
        self.warning.append(str(message))

    @property
    def all(self):
        return self.info + self.warning


class _OsShim:
    """The real `os`, with named attributes replaced. `os.path` is delegated with overrides, so
    the handler's own `isdir` call runs rather than a reimplementation of it.

    `startfile` is NOT delegated: it is absent unless a test supplies it, which is exactly how the
    unsupported-platform branch becomes reachable on any interpreter.
    """

    def __init__(self, startfile=None, **overrides):
        self._path_overrides = {k: v for k, v in overrides.items() if k in {"isdir", "exists"}}
        self._overrides = {k: v for k, v in overrides.items()
                           if k not in self._path_overrides}
        if startfile is not None:
            self._overrides["startfile"] = startfile
        self.path = _PathShim(self._path_overrides)

    def __getattr__(self, item):
        if item in self._overrides:
            return self._overrides[item]
        if item == "startfile":
            # deliberately absent -> `getattr(os, 'startfile', None)` is None
            raise AttributeError("startfile")
        return getattr(os, item)


class _PathShim:
    def __init__(self, overrides):
        self._overrides = overrides

    def __getattr__(self, item):
        if item in self._overrides:
            return self._overrides[item]
        return getattr(os.path, item)


def load_handler(output_dir, *, os_module=None, gr_module=None) -> dict:
    """The REAL handler plus the REAL `ui_content` message constants."""
    node = _func(_GUI, HANDLER)
    import ui_content

    namespace = {
        "os": os_module if os_module is not None else _OsShim(),
        "gr": gr_module if gr_module is not None else _Toasts(),
        # The one seam: a callable standing in for the real resolver.
        #
        # [FORK] R8 UX Refresh: that resolver is now `gui.get_render_output_dir`, not
        # `paths.get_output_dir`. The Render output folder became configurable, and this button
        # must open the folder the render ACTUALLY published into -- opening `output/` while the
        # render wrote somewhere else would be a confidently wrong answer. `get_output_dir` stays
        # in the namespace because `get_render_output_dir` falls back to it.
        "get_render_output_dir": (output_dir if callable(output_dir)
                                  else (lambda: output_dir)),
        "get_output_dir": (output_dir if callable(output_dir) else (lambda: output_dir)),
        "STATUS_OPEN_FOLDER_REQUESTED": ui_content.STATUS_OPEN_FOLDER_REQUESTED,
        "STATUS_OPEN_FOLDER_MISSING": ui_content.STATUS_OPEN_FOLDER_MISSING,
        "STATUS_OPEN_FOLDER_UNSUPPORTED": ui_content.STATUS_OPEN_FOLDER_UNSUPPORTED,
        "STATUS_OPEN_FOLDER_FAILED": ui_content.STATUS_OPEN_FOLDER_FAILED,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<gui>", "exec"),
         namespace)  # noqa: S102
    return namespace


@pytest.fixture
def folder(tmp_path):
    """A realistic output folder holding exactly what a finished render leaves behind."""
    out = tmp_path / "output"
    os.makedirs(out, exist_ok=True)
    master = out / "music_video_20260101_000000.mp4"
    share = out / "music_video_20260101_000000_share.mp4"
    master.write_bytes(b"MASTER-BYTES")
    share.write_bytes(b"SHARE-BYTES")
    os.makedirs(out / ".beatsync-staging", exist_ok=True)
    return {"dir": str(out), "master": str(master), "share": str(share)}


def _snapshot(root):
    """Every path under `root` with its bytes, so a side effect cannot hide."""
    seen = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            seen[os.path.join(base, name)] = None
        for name in files:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                seen[path] = fh.read()
    return seen


# ===========================================================================
# PATH SOURCE
# ===========================================================================


def test_the_handler_reads_the_render_output_resolver_and_nothing_else():
    """The folder is a property of the configuration, never derived from render state.

    [FORK] R8: "a fixed property of the installation" is no longer true -- the Render output
    folder is configurable. What still matters, and is what this test was really protecting, is
    that the folder comes from ONE resolver call and from nothing on screen: not the last output
    path, not the render outcome, not the status text, not session state.
    """
    node = _func(_GUI, HANDLER)
    code = _code(node)

    calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
    assert calls.count("get_render_output_dir") == 1, calls
    assert calls.count("get_output_dir") == 0, \
        "the handler must go through the resolver, not around it"

    for forbidden in ("LAST_OUTPUT_PATH_KEY", "RENDER_OUTCOME_KEY", "session_state",
                      "status_output", "video_output", "output_filename", "preview_path",
                      "share_path", "output_path", "gr.State"):
        assert forbidden not in code, f"{forbidden} reached the folder handler"


def test_the_handler_takes_no_arguments_at_all():
    """Zero inputs is the contract: nothing on the screen decides which folder to open, so no
    user-controlled text can reach the OS call."""
    node = _func(_GUI, HANDLER)
    args = node.args
    assert args.args == [] and args.posonlyargs == [] and args.kwonlyargs == []
    assert args.vararg is None and args.kwarg is None


def test_the_folder_is_the_same_one_the_render_publishes_into():
    """It must be the SAME name `_process_video_impl` resolves the output folder with — not a
    literal, not a drive letter, not a second definition.

    [FORK] R8: that name is `get_render_output_dir`, declared once in `gui.py`. The two call
    sites — the render's publication and this button — are asserted to be the only ones, because
    a second resolver is exactly how the button could start opening a folder the render no longer
    uses.

    Its FALLBACK is still the installation's own `output/`, and that chain is verified on the real
    source as before: `paths.py` cannot be imported here (it imports `logger`, which imports
    librosa), so `gui.py` is checked to import `get_output_dir` from `paths`, that function to
    return `OUTPUT_DIR`, and `OUTPUT_DIR` to be built from `logger.ROOT_DIR`.
    """
    gui = _source(_GUI)
    assert "    get_output_dir,\n" in gui, "get_output_dir is not imported from paths"

    gui_tree = _tree(_GUI)
    resolvers = [n for n in ast.walk(gui_tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "get_render_output_dir"]
    assert len(resolvers) == 1, "there must be exactly one render-output resolver"
    # It falls back to the installation's own folder rather than inventing a path.
    body = ast.unparse(resolvers[0])
    assert "get_output_dir()" in body, "the resolver does not fall back to the installation"
    # No hardcoded location: the configured value comes from the preference store and the fallback
    # from `paths`. Matched as a drive-letter pattern rather than by banning ":" outright, because
    # `ast.unparse` of any function contains the colon of its own `def` line.
    assert not re.search(r"['\"][A-Za-z]:[\\/]", body), \
        "the resolver hardcodes a drive letter"
    # And it NEVER creates the folder: a remembered path is a suggestion, not an action.
    for forbidden in ("makedirs", "mkdir"):
        assert forbidden not in body, f"the resolver calls {forbidden}"

    callers = [ast.unparse(n.func) for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "get_render_output_dir"]
    assert len(callers) == 2, (
        f"expected exactly two call sites (the render's publication and the folder button), "
        f"found {len(callers)}")

    paths_tree = _tree(os.path.join(_SRC, "paths.py"))
    getter = next(n for n in ast.walk(paths_tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "get_output_dir")
    returns = [n for n in ast.walk(getter) if isinstance(n, ast.Return)]
    assert len(returns) == 1
    assert ast.unparse(returns[0].value) == "OUTPUT_DIR", ast.unparse(returns[0].value)

    output_dir = next(n for n in ast.walk(paths_tree)
                      if isinstance(n, ast.Assign) and len(n.targets) == 1
                      and getattr(n.targets[0], "id", None) == "OUTPUT_DIR")
    rendered = ast.unparse(output_dir.value)
    assert "ROOT_DIR" in rendered, rendered
    assert "'output'" in rendered or '"output"' in rendered, rendered
    # no absolute path or drive letter is hardcoded anywhere in the resolution
    assert ":" not in rendered and "\\\\" not in rendered, rendered

    paths_source = _source(os.path.join(_SRC, "paths.py"))
    assert "from logger import ROOT_DIR" in paths_source

    # and the handler calls exactly that name, not a path expression of its own
    handler = _func(_GUI, HANDLER)
    assigns = [ast.unparse(n) for n in ast.walk(handler) if isinstance(n, ast.Assign)]
    assert "output_dir = get_render_output_dir()" in assigns, assigns


def test_the_handler_never_opens_a_file_only_the_folder(folder):
    opened = []
    ns = load_handler(folder["dir"], os_module=_OsShim(startfile=opened.append))

    ns[HANDLER]()

    assert opened == [folder["dir"]]
    assert folder["master"] not in opened and folder["share"] not in opened
    assert os.path.isdir(opened[0]), "a non-directory was handed to the shell"


# ===========================================================================
# THE CALL
# ===========================================================================


def test_a_valid_directory_triggers_exactly_one_startfile_call(folder):
    opened = []
    toasts = _Toasts()
    ns = load_handler(folder["dir"],
                      os_module=_OsShim(startfile=opened.append), gr_module=toasts)

    result = ns[HANDLER]()

    assert len(opened) == 1, f"{len(opened)} startfile calls"
    assert opened[0] == folder["dir"]
    assert result is None, "the handler returned a value into zero outputs"
    assert toasts.warning == [], toasts.warning
    assert len(toasts.info) == 1
    assert folder["dir"] in toasts.info[0]


def test_the_success_message_does_not_claim_explorer_appeared(folder):
    """`os.startfile` returns when the shell ACCEPTS the path. Only a human can confirm a window
    appeared, so the copy must promise the request and nothing more."""
    toasts = _Toasts()
    ns = load_handler(folder["dir"],
                      os_module=_OsShim(startfile=lambda p: None), gr_module=toasts)

    ns[HANDLER]()

    message = toasts.info[0].lower()
    assert "asked windows to open" in message, toasts.info[0]
    for overclaim in ("explorer opened", "explorer is open", "opened explorer",
                      "folder is now open", "window opened"):
        assert overclaim not in message, f"the message claims {overclaim!r}"


def test_no_subprocess_or_shell_is_used_to_open_the_folder():
    """`os.startfile` is the stdlib shell verb, so there is no command line to assemble and no
    new subprocess site in a module whose count of two is pinned elsewhere."""
    code = _code(_func(_GUI, HANDLER))
    for forbidden in ("subprocess", "Popen", "os.system", "os.popen", "shell=True",
                      "explorer.exe", "cmd.exe", "powershell", "shlex", "ShellExecute"):
        assert forbidden not in code, f"{forbidden} in the folder handler"

    # and the module-wide counts are unchanged by S2
    tree = _tree(_GUI)
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]
    popens = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and ast.unparse(n.func) in {"subprocess.Popen", "Popen"}]
    assert len(runs) == 2, f"{len(runs)} subprocess.run sites; expected preview + Director"
    assert popens == [], "a Popen site appeared"


def test_the_only_os_mutation_primitive_absent_from_the_handler():
    """A read-only user action. Not one creating, moving or deleting call may appear."""
    node = _func(_GUI, HANDLER)
    calls = {ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)}
    for forbidden in ("os.makedirs", "os.mkdir", "os.remove", "os.unlink", "os.rename",
                      "os.replace", "os.rmdir", "shutil.rmtree", "shutil.move", "shutil.copy",
                      "shutil.copy2", "open"):
        assert forbidden not in calls, f"{forbidden} is called by the folder handler"


# ===========================================================================
# FAILURE MATRIX
# ===========================================================================


def test_a_missing_directory_produces_a_truthful_warning(tmp_path):
    missing = str(tmp_path / "output")
    assert not os.path.exists(missing)
    opened = []
    toasts = _Toasts()
    ns = load_handler(missing, os_module=_OsShim(startfile=opened.append), gr_module=toasts)

    result = ns[HANDLER]()

    assert result is None
    assert opened == [], "the shell was asked to open a path that does not exist"
    assert toasts.info == []
    assert len(toasts.warning) == 1
    assert missing in toasts.warning[0]
    assert "does not exist" in toasts.warning[0]
    assert "nothing was created or changed" in toasts.warning[0].lower()
    # and it really was not created
    assert not os.path.exists(missing), "the handler created the missing directory"


def test_an_unsupported_platform_is_handled_safely(folder):
    """`os.startfile` exists only on Windows. A missing attribute is a fact to report, never an
    AttributeError escaping into a live Gradio event."""
    toasts = _Toasts()
    ns = load_handler(folder["dir"], os_module=_OsShim(), gr_module=toasts)   # no startfile

    result = ns[HANDLER]()

    assert result is None
    assert toasts.info == []
    assert len(toasts.warning) == 1
    assert "Windows-only" in toasts.warning[0]
    assert folder["dir"] in toasts.warning[0]


def test_the_platform_check_uses_getattr_not_a_bare_call():
    node = _func(_GUI, HANDLER)
    body = ast.unparse(node)
    assert "getattr(os, 'startfile', None)" in body, body
    assert "sys.platform" not in body, "a platform string test is weaker than the attribute itself"


@pytest.mark.parametrize("exc", [
    OSError(5, "shell association failed"),
    OSError("no application is associated"),
    RuntimeError("something entirely unexpected"),
])
def test_a_startfile_failure_is_reported_not_raised(folder, exc):
    def boom(path):
        raise exc

    toasts = _Toasts()
    ns = load_handler(folder["dir"], os_module=_OsShim(startfile=boom), gr_module=toasts)

    result = ns[HANDLER]()          # must not raise

    assert result is None
    assert toasts.info == [], "a failed open was reported as a success"
    assert len(toasts.warning) == 1
    assert "could not open" in toasts.warning[0]
    assert folder["dir"] in toasts.warning[0]
    assert "still saved there" in toasts.warning[0]


def test_the_handler_contains_a_broad_guard_so_nothing_escapes():
    """An unhandled exception inside a Gradio event surfaces as a raw server error. No
    shell-association failure justifies that for a button that changes nothing."""
    node = _func(_GUI, HANDLER)
    assert not [n for n in ast.walk(node) if isinstance(n, ast.Raise)]
    handlers = [h for n in ast.walk(node) if isinstance(n, ast.Try) for h in n.handlers]
    named = [ast.unparse(h.type) for h in handlers if h.type is not None]
    assert "OSError" in named, named
    assert "Exception" in named, "no broad containment handler"
    assert named.index("OSError") < named.index("Exception"), \
        "the specific handler must come first or it is unreachable"


# ===========================================================================
# NO SIDE EFFECTS
# ===========================================================================


@pytest.mark.parametrize("scenario", ["ok", "missing", "unsupported", "failure"])
def test_no_file_is_created_moved_deleted_or_overwritten(folder, tmp_path, scenario):
    before = _snapshot(str(tmp_path))

    if scenario == "ok":
        shim = _OsShim(startfile=lambda p: None)
        target = folder["dir"]
    elif scenario == "missing":
        shim = _OsShim(startfile=lambda p: None)
        target = str(tmp_path / "absent")
    elif scenario == "unsupported":
        shim = _OsShim()
        target = folder["dir"]
    else:
        def boom(path):
            raise OSError("denied")
        shim = _OsShim(startfile=boom)
        target = folder["dir"]

    load_handler(target, os_module=shim)[HANDLER]()

    assert _snapshot(str(tmp_path)) == before, f"{scenario}: the tree changed"


def test_the_master_and_share_files_are_untouched(folder, tmp_path):
    import hashlib

    def digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()

    before = (digest(folder["master"]), digest(folder["share"]))

    load_handler(folder["dir"], os_module=_OsShim(startfile=lambda p: None))[HANDLER]()

    assert (digest(folder["master"]), digest(folder["share"])) == before


def test_no_render_or_conversion_can_begin_from_this_button():
    """Walked structurally, so a rename cannot evade it: the handler reaches no render entry
    point, no analysis, no encoder and no lock."""
    code = _code(_func(_GUI, HANDLER))
    for forbidden in ("process_video", "_process_video_impl", "_process_video_guarded_unlocked",
                      "process_video_guarded", "render_selected_variants_guarded",
                      "analyze_beats_auto", "create_music_video", "_promote_output_no_replace",
                      "_publish_share_no_replace", "build_delivery_preview_command",
                      "_RENDER_LOCK", "RenderLifecycle", "resolve_for_render",
                      "live_declaration", "ffmpeg", "_run_director_model"):
        assert forbidden not in code, f"{forbidden} reachable from the folder handler"


# ===========================================================================
# WIRING
# ===========================================================================


def _click(button: str) -> ast.Call:
    return next(n for n in ast.walk(_tree(_GUI))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "click" and getattr(n.func.value, "id", None) == button)


def test_the_button_registration_has_zero_inputs_and_zero_outputs():
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in _click(BUTTON).keywords}
    assert kwargs["fn"] == HANDLER
    assert kwargs["inputs"] == "[]", kwargs["inputs"]
    assert kwargs["outputs"] == "[]", kwargs["outputs"]
    assert "cancels" not in kwargs
    # not a render: it must never share the render concurrency lane
    assert "concurrency_id" not in kwargs, \
        "the folder button joined a concurrency lane; it is not a render"


def test_the_button_is_a_secondary_always_interactive_button():
    source = _source(_GUI)
    call = next(n for n in ast.walk(_tree(_GUI))
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], "id", None) == BUTTON)
    rendered = ast.unparse(call.value)
    assert rendered.startswith("gr.Button("), rendered
    assert "LABEL_OPEN_OUTPUT_FOLDER" in rendered, "the label is inlined instead of ui_content"
    assert "variant='secondary'" in rendered
    assert "interactive=False" not in rendered, "the folder button must never start disabled"
    assert "open-output-folder-button" in rendered
    # it sits with the video output, which is the question it answers
    assert source.index("video_output = gr.Video(") < source.index(f"{BUTTON} = gr.Button(")


def test_the_button_is_absent_from_every_gate_collection():
    """No source or preparation event may disable it: the folder is a fixed property of the
    installation, so there is no state in which pointing at it would be wrong."""
    source = _source(_GUI)
    for collection in ("source_outputs", "prep_outputs"):
        block = source[source.index(f"{collection} = ["):]
        block = block[:block.index("]")]
        assert BUTTON not in block, f"{collection} writes the folder button"


def test_no_existing_output_tuple_or_writer_matrix_changed():
    """S2 adds a reader-only event. Every pre-existing writer list must be byte-identical."""
    source = _source(_GUI)
    # the two render events keep their exact outputs, ending in the invocation state
    for button in ("process_btn", "render_selected_variants_btn"):
        outputs = next(kw.value for kw in _click(button).keywords if kw.arg == "outputs")
        names = [n.id for n in outputs.elts if isinstance(n, ast.Name)]
        assert names[-1] == "render_invocation_state", button
        assert BUTTON not in names, f"{button} writes the folder button"
        assert "video_output" in names and "status_output" in names, button
    # Cancel still owns exactly the status line
    cancel = {kw.arg: ast.unparse(kw.value) for kw in _click("cancel_render_btn").keywords}
    assert cancel["outputs"] == "[status_output]"
    # status_output keeps ONE writer: the folder button must not have become a second
    writers = [ast.unparse(n.func.value) for n in ast.walk(_tree(_GUI))
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in {"click", "change", "input", "select"}
               for kw in n.keywords
               if kw.arg == "outputs" and "status_output" in ast.unparse(kw.value)]
    assert sorted(writers) == ["cancel_render_btn", "process_btn",
                               "render_selected_variants_btn"], writers
    assert f"{BUTTON}.click" not in [f"{w}.click" for w in writers if w == BUTTON]
    # and no new gr.State was introduced
    states = [n for n in ast.walk(_tree(_GUI)) if isinstance(n, ast.Call)
              and ast.unparse(n.func) == "gr.State"]
    assert len(states) == _EXPECTED_STATE_COUNT, \
        f"{len(states)} gr.State objects; the count is pinned - add one only under review"
    assert "open_output_folder_state" not in source


#: The number of `gr.State` objects in `gui.py`. S2 itself adds none; the count is pinned so that
#: any new state has to be a reviewed decision rather than a side effect.
#:
#: 6 before Raw Cutter V1; 7 since, for `cut_plan_state`, which carries the Raw Cutter's segment
#: plan between Analyze and Cut as a plain dict of primitives. Nothing live enters it.
#:
#: 8 since R7, for `audio_review_state`. It carries the Raw Audio Cutter's exported-clip list
#: between Cut and the Keep/Discard buttons, as a list of plain dicts -- a path, a name, an index
#: and two floats. It is a `gr.State` rather than a module-level record precisely so one browser
#: session's review list is not reachable from another, which is the same reasoning that kept the
#: Clips Preview registry per-session.
#:
#: 12 since R7.2, which added four for the same per-session reason and nothing else:
#:
#:   `audio_armed_state`          the sweep signature of the batch the operator confirmed -- a
#:                                plain sha256 string, never a live object. It exists so that
#:                                agreeing to discard unmarked clips authorises ONE specific set of
#:                                counts: change a decision or a folder and the signature no longer
#:                                matches, so the operator is asked again.
#:   `audio_run_dir_state`        the last cut's output folder, so "Open results in Audio Preview"
#:                                can fill the other tab's box. A plain path string.
#:   `audio_preview_state`        the Audio Preview tab's whole view -- session and page tokens,
#:                                the frozen entries, and the decision/name records keyed by file
#:                                identity, which is what makes paging lossless.
#:   `audio_preview_armed_state`  that tab's own sweep signature, separate from the cutter's
#:                                because the two review different clips.
#:
#: `audio_review_state` also changed shape, from a list of dicts to the same view dict. Nothing
#: live -- no lock, thread, handle, descriptor or lifecycle -- enters any of them.
#:
#: 13 since R8 Final Polish, for `clips_armed_state` -- the thirteenth is the reviewed decision
#: this pin exists to force, and it is the exact sibling of `audio_armed_state` rather than a new
#: kind of state. Video Review gained the same keep-only confirmation Audio Review already had, so
#: it needs the same thing: the sha256 sweep signature of the batch the operator actually agreed
#: to discard unmarked clips from. A plain string, never a live object, and per-session for the
#: same reason every state above is. Separate from `audio_armed_state` because the two tabs review
#: different clips, and a signature minted for one batch must not authorise a sweep of another --
#: `test_a_signature_from_a_different_batch_cannot_authorise_a_sweep` pins that directly.
_EXPECTED_STATE_COUNT = 13


def test_the_expected_state_count_matches_the_named_states():
    """Self-check, so the constant above cannot silently become whatever the code says."""
    source = _source(_GUI)
    names = sorted({
        ast.unparse(n.targets[0]) for n in ast.walk(_tree(_GUI))
        if isinstance(n, ast.Assign) and len(n.targets) == 1
        and isinstance(n.value, ast.Call) and ast.unparse(n.value.func) == "gr.State"
    })
    assert len(names) == _EXPECTED_STATE_COUNT, names
    for expected in ("session_state", "source_state", "prep_state", "variant_batch_state",
                     "director_proposal_state", "render_invocation_state",
                     "audio_review_state",
                     # [R7.2] Named individually rather than counted, so a future state cannot
                     # satisfy the count by displacing one of these.
                     "audio_armed_state", "audio_run_dir_state",
                     "audio_preview_state", "audio_preview_armed_state",
                     # [R8 Final Polish] Video Review's own sweep signature, named for the same
                     # reason: a later state must not satisfy the count by displacing it.
                     "clips_armed_state"):
        assert expected in names, f"{expected} missing from {names}"
    assert "gr.State" in source


# ===========================================================================
# REJECTED SURFACE
# ===========================================================================


def test_nothing_the_product_owner_rejected_was_implemented():
    """A download button, endpoint, cache, duplicate video or state machine. None of it."""
    source = _source(_GUI)
    tree = _tree(_GUI)
    components = [ast.unparse(n.func) for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("gr.")]

    assert components.count("gr.DownloadButton") == 0
    for forbidden in ("share_download", "download_btn", "download_button", "SHARE_OUTCOME_KEY",
                      "FileResponse", "send_file", "mount_gradio_app", "StaticFiles",
                      "@app.get", "add_api_route"):
        assert forbidden not in source, f"{forbidden} was added"


def test_gradio_file_serving_permissions_were_not_widened():
    """A download control would have needed this; opening the folder does not.

    Checked as a KEYWORD ARGUMENT, never as a substring: `gui.py` legitimately explains in prose
    why widening `allowed_paths` would be wrong, and banning the word would ban the explanation.
    """
    tree = _tree(_GUI)
    for call in ast.walk(tree):
        if isinstance(call, ast.Call):
            assert "allowed_paths" not in [kw.arg for kw in call.keywords], \
                f"allowed_paths passed to {ast.unparse(call.func)}"

    launches = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "launch"]
    assert launches, "no launch call found"
    for call in launches:
        kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert kwargs.get("share", "False") == "False", kwargs
        assert kwargs.get("server_name", "'127.0.0.1'") in ("'127.0.0.1'", '"127.0.0.1"'), kwargs


#: Browser-side mechanisms Open Output Folder must never acquire. The approved behaviour is a
#: Windows shell verb on the machine running the backend; anything here would move the action into
#: the browser.
#:
#: Every entry is a precise API name. A bare `"download"` was tried and removed: the handler's own
#: docstring explains *why* a download control was rejected, so the word appears in prose and the
#: substring ban fired on the explanation rather than on any mechanism. Download mechanisms are
#: named exactly below, and `test_nothing_the_product_owner_rejected_was_implemented` keeps
#: guarding `gr.DownloadButton`, `FileResponse` and `send_file` across the whole module.
BROWSER_SIDE_FORBIDDEN = ("file://", "window.open", "navigator.", "document.querySelector",
                          "document.location", "window.location", "showDirectoryPicker",
                          "showSaveFilePicker", "createObjectURL", "gr.DownloadButton",
                          "FileResponse", "send_file")


def test_no_browser_side_file_navigation_or_javascript_was_added():
    """Section 4: the folder opens on the machine running the backend, by design.

    Scoped to the Open Output Folder feature -- its handler and its one event registration -- and
    that scope is the correction R5-P1 required.

    The original version walked the WHOLE of `gui.py`: no `js`/`_js` keyword on any call, and no
    browser-API substring in any literal anywhere in the module. That was wider than the claim it
    was written to defend (this test's own docstring names one button), and it made the file an
    application-wide ban on client-side code. R5's Clips Preview gallery legitimately needs a
    JavaScript interaction layer -- hover preview, keyboard navigation, one active player at a
    time -- which the accepted specification mandates, so the global form had to go.

    Nothing this test actually protected is given up. The handler still may not reach a browser
    API, its registration still may not carry `js`/`_js`, and it still may not acquire a download
    path. `test_nothing_the_product_owner_rejected_was_implemented` continues to guard the
    rejected-mechanism list module-wide, and a negative control in the R5-P1 R2 closeout proves a
    deliberate `window.open` planted in this handler still fails here.
    """
    # 1. The handler itself: no browser-side mechanism, in code or in any string it builds.
    #    Docstrings stripped via this module's existing `_code` helper -- this handler's own prose
    #    explains which mechanisms were rejected and why, so an un-stripped scan fires on the
    #    explanation rather than on code. `_code`'s own docstring records that this has happened
    #    repeatedly here.
    handler = _code(_func(_GUI, HANDLER))
    for forbidden in BROWSER_SIDE_FORBIDDEN:
        assert forbidden not in handler, f"{forbidden} reached {HANDLER}"

    # 2. Its one registration: no JavaScript may be attached to this event.
    registration = _click(BUTTON)
    for banned_kw in ("js", "_js"):
        assert banned_kw not in [kw.arg for kw in registration.keywords], \
            f"{banned_kw}= passed to {BUTTON}.click"

    # 3. Nothing the handler calls may smuggle one in either. Walked by name through the module,
    #    so a rename cannot evade it.
    tree = _tree(_GUI)
    called = {node.func.id for node in ast.walk(_func(_GUI, HANDLER))
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    for name in sorted(called):
        helper = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef) and n.name == name), None)
        if helper is None:
            continue
        rendered = _code(helper)
        for forbidden in BROWSER_SIDE_FORBIDDEN:
            assert forbidden not in rendered, f"{forbidden} reached {name}, called by {HANDLER}"

    # 4. `file://` is refused module-wide, independently of scope: the app never navigates a
    #    browser to a local path, and no feature has a reason to build such a URL.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "file://" not in node.value, \
                f"a file:// URL appears in a string literal: {node.value[:80]!r}"


#: [R5-P3 / F9] The ONLY event registrations in `gui.py` permitted to carry client-side
#: JavaScript, as (callee, argument) pairs read from the AST.
#:
#: Why an allowlist and not the old blanket ban. Before R5-P1 this file forbade `js=`/`_js=` on
#: *any* call in `gui.py`, which was wider than the one-button claim its own docstring makes --
#: and it had to go when the R5 gallery legitimately needed an interaction layer the accepted
#: specification mandates. The independent review (F9) recorded the cost of that narrowing: with
#: the global form gone, any future feature could attach JavaScript anywhere with no test
#: objecting. This restores the blast radius without re-banning the one legitimate case.
#:
#: Structural rather than a substring scan: the pair comes from `ast.unparse` of the call's `func`
#: and of the keyword's value, so a renamed variable holding the same script, or the same keyword
#: on a different event, both show up as a new pair rather than slipping through a text match.
AUTHORIZED_JAVASCRIPT_REGISTRATIONS = (
    ("app.load", "CLIPS_GALLERY_JS"),
    # [R7.1] The Audio Cutter gallery's own installer. A SECOND authorised entry, not a relaxation:
    # the allowlist is still asserted by identity, so any other JavaScript registration -- or
    # either of these two growing a server call -- still fails.
    ("app.load", "AUDIO_GALLERY_JS"),
    # [FORK] Raw Cutter R12: the Activity & Performance panel's one-second tick. A THIRD authorised
    # entry on the same terms -- `fn=None`, so it registers no server call, and it reads nothing
    # but the `data-*` figures the panel it ticks already published. Authorised deliberately, with
    # the identity claim unchanged: a fourth registration, or a different script in any slot,
    # still fails. `tests/test_raw_cutter_activity.py` pins what this one is allowed to contain --
    # notably that it performs no network call of any kind.
    ("app.load", "CUT_ACTIVITY_JS"),
)


#: Gradio's event-registration methods. Calling one of these is how a component attaches
#: behaviour -- including client-side script -- to the app. Any *other* call is ordinary Python
#: and is none of this guard's business, which is what keeps the fail-closed rule below from
#: rejecting unrelated code.
GRADIO_EVENT_METHODS = frozenset({
    "blur", "change", "clear", "click", "collapse", "download", "edit", "end", "expand",
    "focus", "input", "key_up", "like", "load", "pause", "play", "release", "retry",
    "select", "stop", "stream", "submit", "success", "then", "tick", "undo", "upload",
})

#: What :func:`_javascript_registrations` reports for an event registration whose keywords cannot
#: be read statically. Deliberately not a script name, so it can never match the allowlist.
OPAQUE_REGISTRATION = "**<keywords not statically readable>"


def _javascript_registrations(tree):
    """Every call in `gui.py` that can carry client-side JavaScript, as (callee, argument) pairs.

    [R5-P9 / N3] **Fails closed on keyword unpacking.** `ast` represents `**mapping` as a
    `keyword` whose `arg` is `None`, so the old loop -- which only ever compared `keyword.arg`
    against the two names -- skipped it silently. An event registered through a dynamic keyword
    dictionary therefore produced *no* pair at all, the exact-equality check below compared the
    allowlist against a set that simply did not contain it, and the guard passed something it had
    never looked at. That is the worst failure mode a guard can have: not a wrong answer, but a
    confident answer about code it did not read.

    The rule is structural, not textual: if a **Gradio event method** is called with keywords that
    cannot be resolved statically, the registration is reported as :data:`OPAQUE_REGISTRATION`,
    which is on no allowlist and so fails the comparison. The guard does not try to prove what is
    inside the mapping -- it cannot, in general -- it refuses to certify what it cannot read.

    Scoping to :data:`GRADIO_EVENT_METHODS` is what keeps this honest in the other direction.
    `gui.py` legitimately calls `sink.update(**payload)` and `fork_cutter_batch.BatchItem(**fields)`;
    neither can attach script to the app, and neither is rejected. A positive control for the one
    approved registration, and negative controls for both evasion shapes, are below.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if (getattr(node.func, "attr", None) in GRADIO_EVENT_METHODS
                and any(keyword.arg is None for keyword in node.keywords)):
            found.append((ast.unparse(node.func), OPAQUE_REGISTRATION))
        for keyword in node.keywords:
            if keyword.arg in ("js", "_js"):
                found.append((ast.unparse(node.func), ast.unparse(keyword.value)))
    return sorted(found)


def test_only_the_authorized_registration_carries_client_side_javascript():
    """Application-wide, and deliberately exact.

    Adding a `js=` anywhere in `gui.py` fails this test until the pair is added to the allowlist
    above -- which is a decision someone has to make on purpose, with a reviewer looking at it.
    """
    registrations = _javascript_registrations(_tree(_GUI))
    assert registrations == sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS), (
        "an unauthorized client-side JavaScript registration appeared in gui.py: "
        f"{registrations!r} != {sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS)!r}")


#: Every JavaScript installer `gui.py` is allowed to register, by the NAME of the script it
#: carries. Amended by R7.1, which added the Audio Cutter gallery's listeners; the claim is
#: unchanged and is still identity-based, so a third registration, or a different script in either
#: slot, still fails.
#: amended again by Raw Cutter R12, which added the Activity & Performance tick.
_AUTHORIZED_JS_SCRIPTS = ("CLIPS_GALLERY_JS", "AUDIO_GALLERY_JS", "CUT_ACTIVITY_JS")


def test_the_authorized_registration_is_the_gallery_installer_and_nothing_else():
    """Each permitted entry is pinned to what it actually is: a listener installer that registers
    no server call (`fn=None`), not a hidden channel for decisions."""
    tree = _tree(_GUI)
    loads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "app.load"
             and any(keyword.arg == "js" for keyword in node.keywords)]
    assert len(loads) == len(_AUTHORIZED_JS_SCRIPTS), (
        f"expected {len(_AUTHORIZED_JS_SCRIPTS)} JavaScript app.load registrations, "
        f"found {len(loads)}")
    seen = []
    for load in loads:
        keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in load.keywords}
        seen.append(keywords.get("js"))
        assert keywords.get("fn") == "None", "an installer must register no server call"
        assert keywords.get("inputs") == "None" and keywords.get("outputs") == "None"
    assert sorted(seen) == sorted(_AUTHORIZED_JS_SCRIPTS), seen


def test_the_allowlist_itself_rejects_a_planted_registration():
    """An executable negative control for the allowlist's own comparison.

    The mutation controls in the task report plant a real `js=` in `gui.py` and show this suite
    failing. This checks the comparison that makes that happen, without editing the source: a
    pair that is not on the list must not compare equal.
    """
    planted = sorted(list(AUTHORIZED_JAVASCRIPT_REGISTRATIONS)
                     + [("process_btn.click", "SOME_OTHER_SCRIPT")])
    assert planted != sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS)
    assert ("open_output_folder_btn.click", "ANY_SCRIPT") not in \
        AUTHORIZED_JAVASCRIPT_REGISTRATIONS, (
        "the protected button must never be allowlisted for JavaScript")


# ---------------------------------------------------------------------------------------------
# [R5-P9 / N3] The allowlist must not be evadable by keyword unpacking.
#
# The fixtures below are parsed from source built at run time, and the keyword name is assembled
# rather than written out. Two reasons, in order of importance. First, these tests must fail
# because of the *shape* of the registration in the AST, never because some literal happens to
# appear in this file -- a guard that trips on its own explanatory text proves nothing about the
# behaviour it claims to pin. Second, this repository carries raw-source substring guards, and
# planting a forbidden literal where one of them might read it has produced false failures before.
# ---------------------------------------------------------------------------------------------

_JS_KEYWORD = "j" + "s"


def _registration_fixture(source):
    """Parse a one-line fixture and return what the guard makes of it."""
    return _javascript_registrations(ast.parse(source))


def test_a_registration_hidden_behind_keyword_unpacking_cannot_evade_the_allowlist():
    """The N3 bypass, as an assertion.

    `ast` reports `keyword.arg is None` for `**mapping`, so before this fix the guard returned an
    empty list for the registration below and the exact-equality check passed it without ever
    reading it. Now the call is reported as opaque, which is on no allowlist.
    """
    hidden = "{'" + _JS_KEYWORD + "': EVIL_SCRIPT}"
    source = f"protected_btn.click(fn=None, inputs=None, outputs=None, **{hidden})"

    # The fixture really is the shape under test: an event call with an unreadable keyword.
    call = ast.parse(source).body[0].value
    assert isinstance(call, ast.Call) and call.func.attr in GRADIO_EVENT_METHODS
    assert any(keyword.arg is None for keyword in call.keywords), \
        "the fixture must use keyword unpacking, or it is not testing N3"

    found = _registration_fixture(source)
    assert found == [("protected_btn.click", OPAQUE_REGISTRATION)], (
        "an event registration whose keywords cannot be read statically must be reported, "
        f"not silently skipped: {found!r}")
    assert found != sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS), \
        "the opaque registration must not compare equal to the allowlist"


def test_an_unauthorized_named_javascript_keyword_on_another_event_is_still_reported():
    """The ordinary, non-unpacked evasion. This never regressed; it must not start to."""
    source = f"process_btn.click(fn=None, {_JS_KEYWORD}=EVIL_SCRIPT)"
    found = _registration_fixture(source)
    assert found == [("process_btn.click", "EVIL_SCRIPT")]
    assert found != sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS)


def test_the_approved_installer_still_reads_as_the_only_registration():
    """The positive control: the fix must not reject the one legitimate case.

    Read from the shipped `gui.py`, so this fails if the real registration ever stops matching.
    """
    assert _javascript_registrations(_tree(_GUI)) == sorted(AUTHORIZED_JAVASCRIPT_REGISTRATIONS)


def test_ordinary_keyword_unpacking_is_not_mistaken_for_an_event_registration():
    """Fail-closed on event registrations, not on `**kwargs` as such.

    Grounded in the real source rather than a hypothetical: `gui.py` genuinely contains calls that
    unpack a mapping, and none of them is a Gradio event method. If a future change made the guard
    reject plain `**kwargs`, the suite would start failing on unrelated, legitimate code -- so this
    asserts both that such calls exist and that they are ignored.
    """
    for source in ("sink.update(**payload)",
                   "fork_cutter_batch.BatchItem(**fields)",
                   "helper(**options)"):
        assert _registration_fixture(source) == [], f"{source} must not be reported"

    unpacking_calls = [
        node for node in ast.walk(_tree(_GUI))
        if isinstance(node, ast.Call) and any(k.arg is None for k in node.keywords)]
    assert unpacking_calls, "expected gui.py to contain at least one **kwargs call"
    assert all(getattr(node.func, "attr", None) not in GRADIO_EVENT_METHODS
               for node in unpacking_calls), (
        "gui.py now unpacks keywords into a Gradio event registration; that is exactly what the "
        "N3 guard refuses to certify, and it needs a deliberate decision rather than a test edit")


def test_no_progress_stage_was_invented_for_the_folder_button():
    """M4 stays DEFERRED_BY_PRODUCT_OWNER, and this button is not a reason to reopen it."""
    body = ast.unparse(_func(_GUI, HANDLER))
    for token in ("fork_progress", "ProgressEvent", "EventKind", "event_callback", "stage=7",
                  "Stage.DELIVERY", "ProgressView"):
        assert token not in body, f"{token} in the folder handler"


# ===========================================================================
# THE EXPORT PIPELINE IS UNTOUCHED
# ===========================================================================


def test_the_export_pipeline_helpers_are_unreferenced_by_s2():
    """Opening a folder is an independent user action. It must not appear anywhere in the
    publication path, and the publication path must not appear in it."""
    for helper in ("_promote_output_no_replace", "_publish_share_no_replace",
                   "_publish_share_attempt", "_process_video_impl"):
        body = ast.unparse(_func(_GUI, helper))
        assert HANDLER not in body, f"{helper} references the folder handler"
        assert BUTTON not in body, f"{helper} references the folder button"


def test_the_master_success_commit_point_is_still_single_and_unmoved():
    impl = ast.unparse(_func(_GUI, "_process_video_impl"))
    assert impl.count(
        "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS") == 1
    assert impl.index("_promote_output_no_replace(") < impl.index(
        "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS")
    assert impl.index("session_state[LAST_OUTPUT_PATH_KEY] = output_path") < impl.index(
        "session_state[RENDER_OUTCOME_KEY] = RenderOutcomeKind.SUCCESS")


def test_every_rename_in_the_gui_is_one_of_the_three_audited_ones():
    """`gui.py`'s complete set of `os.rename` call sites, by exact identity.

    A rename is how this application moves a file that matters, so the set is pinned rather than
    counted: a NEW one must be reviewed deliberately, which is what this assertion forces.

    [FORK] R8 UX Refresh adds the THIRD, and it is a different kind of operation from the first
    two. Those two are durable publications of a finished render; this one moves the previous
    session's browser uploads aside at startup instead of deleting them, and it exists precisely
    so that a `shutil.rmtree` no longer runs over files an operator may have no other path to.

    It cannot be mistaken for a publication, and the distinctions are worth stating because they
    are what make a third rename acceptable here:

      * it runs in `_retire_previous_uploads`, during startup, before any render exists;
      * its source is `input/gradio_uploads` and its destination is under
        `input/retired_uploads/`, so it never touches `output/` or a staging folder;
      * it is no-replace by construction -- the destination name is advanced until it is free, so
        two starts in the same second cannot merge or overwrite;
      * it writes neither `RENDER_OUTCOME_KEY` nor `LAST_OUTPUT_PATH_KEY`, so it is invisible to
        the render outcome model and to batch continuation.
    """
    renames = [ast.unparse(n) for n in ast.walk(_tree(_GUI))
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.rename"]
    assert renames == [
        "os.rename(temp_output, output_path)",        # H1: the authoritative master promotion
        "os.rename(staging_path, share_path)",        # H1-b: the subordinate share publication
        "os.rename(uploads, candidate)",              # R8: retire, rather than delete, uploads
    ], renames


def test_the_upload_retirement_is_a_move_and_never_a_deletion():
    """The whole point of the third rename: nothing on this path deletes an uploaded file.

    Asserted over CALLS so the docstring may explain what it does not do.
    """
    retire = _func(_GUI, "_retire_previous_uploads")
    calls = {ast.unparse(n.func) for n in ast.walk(retire) if isinstance(n, ast.Call)}
    for forbidden in ("shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.replace",
                      "shutil.move"):
        assert forbidden not in calls, f"{forbidden} is reachable while retiring uploads"
    assert "os.rename" in calls, "retirement must actually move the folder"

    # And the startup sweep must not delete the retirement folder on the NEXT start, which would
    # reproduce the old behaviour one run later.
    cleanup = ast.unparse(_func(_GUI, "cleanup_on_startup"))
    assert "RETIRED_UPLOADS_DIR_NAME" in cleanup, \
        "the retirement folder is not protected from the startup sweep"


def test_the_protected_contract_constants_are_unchanged():
    analysis = _source(os.path.join(_SRC, "video_analysis.py"))
    partials = _source(os.path.join(_SRC, "beatsync_fork", "qwen_partials.py"))
    prep = _source(os.path.join(_SRC, "beatsync_fork", "library_prep.py"))

    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in analysis
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in analysis
    assert "_CACHE_IDENTITY_WORKER_CAP = 16" in analysis
    assert 'PARTIAL_CONTRACT_VERSION = "stage5_job_partial_v1"' in partials
    assert "DEFAULT_ANALYZE_BATCH_SIZE = 100" in prep


# ===========================================================================
# COPY
# ===========================================================================


def test_every_message_the_handler_can_emit_comes_from_ui_content():
    """No inlined prose, so the copy is reviewable in one place."""
    node = _func(_GUI, HANDLER)
    literals = [n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # the docstring is the only string literal permitted in the body
    assert len(literals) == 2, literals          # docstring + the getattr attribute name
    assert "startfile" in literals

    body = ast.unparse(node)
    for constant in ("STATUS_OPEN_FOLDER_REQUESTED", "STATUS_OPEN_FOLDER_MISSING",
                     "STATUS_OPEN_FOLDER_UNSUPPORTED", "STATUS_OPEN_FOLDER_FAILED"):
        assert constant in body, f"{constant} unused"


def test_the_label_and_help_text_exist_and_describe_the_local_machine():
    import ui_content

    assert ui_content.LABEL_OPEN_OUTPUT_FOLDER == "📂 Open Output Folder"
    info = ui_content.INFO_OPEN_OUTPUT_FOLDER
    assert "running BeatSync" in info, "the help text does not say whose machine opens"
    assert "Explorer" in info
    # it must not promise a download
    for overclaim in ("download", "Download"):
        assert overclaim not in info, info
