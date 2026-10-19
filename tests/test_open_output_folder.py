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
        # the one seam: a callable standing in for the real `paths.get_output_dir`
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


def test_the_handler_reads_get_output_dir_and_nothing_else():
    """The folder is a fixed property of the installation, never derived from render state."""
    node = _func(_GUI, HANDLER)
    code = _code(node)

    calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
    assert calls.count("get_output_dir") == 1, calls

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


def test_the_folder_is_the_running_installations_own_output_dir():
    """It must be the SAME name `_process_video_impl` resolves `output/` with — not a literal, not
    a drive letter, not a second definition.

    `paths.py` cannot be imported here (it imports `logger`, which imports librosa), so the chain
    is verified on the real source instead: `gui.py` imports `get_output_dir` from `paths`, that
    function returns `OUTPUT_DIR`, and `OUTPUT_DIR` is built from `logger.ROOT_DIR` — i.e. the
    directory of the installation actually running, discovered at import rather than configured.
    """
    gui = _source(_GUI)
    assert "    get_output_dir,\n" in gui, "get_output_dir is not imported from paths"

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
    assert "output_dir = get_output_dir()" in assigns, assigns


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
_EXPECTED_STATE_COUNT = 7


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
                     "director_proposal_state", "render_invocation_state"):
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


def test_the_two_renames_are_still_exactly_the_promotion_and_the_publication():
    renames = [ast.unparse(n) for n in ast.walk(_tree(_GUI))
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.rename"]
    assert renames == [
        "os.rename(temp_output, output_path)",
        "os.rename(staging_path, share_path)",
    ], renames


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
