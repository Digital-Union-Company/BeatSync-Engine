"""The startup upload retirement, and the deletion it must never fall back to.

**The incident this suite exists for.** On 2026-11-11 a normal start printed

    Could not set aside the previous uploads, so they were left alone: [WinError 5] Access is
    denied: '...\\input\\gradio_uploads' -> '...\\input\\retired_uploads\\20261111_212009'

and then deleted them. `_retire_previous_uploads` caught the `OSError` and returned, exactly as its
docstring promises; `cleanup_on_startup` then swept `input/` with
`shutil.rmtree(..., ignore_errors=True)` over everything not in `protected_dirs` — and
`gradio_uploads` was not in that set. A 114,522,906-byte browser-uploaded MP3 went with it. The
operating-system refusal behind the `WinError 5` was never identified and this suite does not
pretend to know it: the defect is that a **failed** retirement was treated as permission to delete.

Two existing tests covered this area and both stayed green through the incident, which is the
lesson worth keeping:

* `test_open_output_folder.py::test_the_upload_retirement_is_a_move_and_never_a_deletion` asserts
  no delete call is reachable **inside `_retire_previous_uploads`**. True then and true now — the
  deletion was in the caller.
* the same test asserts `cleanup_on_startup` mentions `RETIRED_UPLOADS_DIR_NAME`, i.e. that the
  *destination* is protected. The **source** was not.

So the assertions below are behavioural: the real bodies of both functions are AST-extracted from
`gui.py` and executed against a synthetic `input/` tree with a shimmed `os`/`shutil`, so a failed
rename can be injected and the actual bytes on disk inspected afterwards. `gui.py` cannot be
imported on a bare interpreter, and a test that re-derived the sweep would have reproduced the bug.
"""

from __future__ import annotations

import ast
import builtins
import datetime as _datetime
import errno
import os
import shutil
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")

RETIRE_FN = "_retire_previous_uploads"
CLEANUP_FN = "cleanup_on_startup"

#: The folder whose contents the incident destroyed. A literal here on purpose: the whole point is
#: that the test knows the name independently of whatever `gui.py` calls its constant.
UPLOADS = "gradio_uploads"

#: Everything `cleanup_on_startup` protected before the fix, by name.
ALREADY_PROTECTED = ("audio", "video", "video_analysis_cache", "retired_uploads")


def _tree() -> ast.Module:
    with open(_GUI, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=_GUI)


def _func(name: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree())
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _module_assigns(wanted) -> list:
    """Module-level `NAME = ...` statements for any of ``wanted`` that `gui.py` actually has.

    Tolerant by design: this suite must run against the pre-fix source too, where the uploads
    constant does not exist yet.
    """
    out = []
    for node in _tree().body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) in wanted):
            out.append(node)
    return out


class _OsShim:
    """The real `os`, with an injectable `rename` failure and a record of every mutation."""

    def __init__(self, *, rename_error=None, listdir_error_for=None):
        self._rename_error = rename_error
        self._listdir_error_for = listdir_error_for
        self.calls: list = []

    def __getattr__(self, name):                 # everything not overridden is the real thing
        return getattr(os, name)

    # --- the one operation the incident turned on -------------------------------------------
    def rename(self, src, dst):
        self.calls.append(("rename", str(src), str(dst)))
        if self._rename_error is not None:
            raise self._rename_error
        return os.rename(src, dst)

    # --- mutations, recorded -----------------------------------------------------------------
    def remove(self, path):
        self.calls.append(("remove", str(path)))
        return os.remove(path)

    def unlink(self, path):
        self.calls.append(("unlink", str(path)))
        return os.unlink(path)

    def rmdir(self, path):
        self.calls.append(("rmdir", str(path)))
        return os.rmdir(path)

    def replace(self, src, dst):
        self.calls.append(("replace", str(src), str(dst)))
        return os.replace(src, dst)

    def makedirs(self, path, *a, **kw):
        self.calls.append(("makedirs", str(path)))
        return os.makedirs(path, *a, **kw)

    # --- reads --------------------------------------------------------------------------------
    def listdir(self, path="."):
        if (self._listdir_error_for is not None
                and os.path.normcase(os.path.abspath(str(path)))
                == os.path.normcase(os.path.abspath(self._listdir_error_for))):
            raise OSError(errno.EACCES, "Access is denied", str(path))
        return os.listdir(path)


class _ShutilShim:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        return getattr(shutil, name)

    def rmtree(self, path, *a, **kw):
        self.calls.append(("rmtree", str(path)))
        return shutil.rmtree(path, *a, **kw)

    def move(self, src, dst, *a, **kw):
        self.calls.append(("move", str(src), str(dst)))
        return shutil.move(src, dst, *a, **kw)


class _FrozenDatetime:
    """`datetime.datetime.now().strftime(...)` only — fixed, so the collision case is reachable."""

    class datetime:
        @staticmethod
        def now():
            return _datetime.datetime(2026, 11, 11, 21, 20, 9)


def _run_cleanup(root, *, rename_error=None, listdir_error_for=None, mutations=()):
    """Execute the REAL retirement + startup cleanup against ``root`` as the app root.

    Returns ``(namespace, os_shim, mutation_calls, printed)``.
    """
    input_base = os.path.join(root, "input")
    os_shim = _OsShim(rename_error=rename_error, listdir_error_for=listdir_error_for)
    mutation_calls = os_shim.calls
    printed: list = []

    namespace = {
        "os": os_shim,
        "shutil": _ShutilShim(mutation_calls),
        "datetime": _FrozenDatetime,
        "ROOT_DIR": root,
        "get_input_dir": (lambda: input_base),
        "get_audio_input_dir": (lambda: os.path.join(input_base, "audio")),
        "get_video_input_dir": (lambda: os.path.join(input_base, "video")),
        "print": (lambda *a, **kw: printed.append(" ".join(str(x) for x in a))),
        "__builtins__": builtins,
    }

    body = list(_module_assigns({
        "RETIRED_UPLOADS_DIR_NAME", "RETIRED_UPLOADS_DIR",
        "GRADIO_UPLOADS_DIR_NAME", "GRADIO_UPLOADS_DIR",
    }))
    for name in (RETIRE_FN, CLEANUP_FN):
        node = _func(name)
        for mutate in mutations:
            node = mutate(name, node)
        body.append(node)

    exec(compile(ast.Module(body=body, type_ignores=[]), "<gui-startup>", "exec"),  # noqa: S102
         namespace)
    namespace[CLEANUP_FN]()
    return namespace, os_shim, mutation_calls, printed


def _snapshot(root):
    """(path -> (is_dir, size, mtime_ns)) for everything under ``root``, relative paths."""
    out = {}
    for base, dirs, files in os.walk(root):
        for name in list(dirs) + list(files):
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root)
            try:
                st = os.stat(p)
                out[rel] = (os.path.isdir(p), st.st_size, st.st_mtime_ns)
            except OSError:
                out[rel] = None
    return out


ACCESS_DENIED = OSError(errno.EACCES, "Access is denied")
ACCESS_DENIED.winerror = 5                       # the incident's exact shape


@pytest.fixture
def app(tmp_path):
    """A synthetic app root: one uploaded file, the protected folders, and disposable scratch."""
    root = tmp_path / "app"
    inp = root / "input"
    uploads = inp / UPLOADS / "affa6a33deadbeef"
    uploads.mkdir(parents=True)
    (uploads / "New_Music_Synthetic.mp3").write_bytes(b"\x7f" * 4096)
    (inp / UPLOADS / "tmp_zero").write_bytes(b"")
    for name in ALREADY_PROTECTED:
        (inp / name).mkdir(parents=True, exist_ok=True)
    (inp / "audio" / "keep.wav").write_bytes(b"a" * 64)
    (inp / "video" / "keep.mp4").write_bytes(b"v" * 64)
    (inp / "video_analysis_cache" / "aabbccdd_0011223344556677889900aa.json").write_text("{}")
    (inp / "retired_uploads" / "20260101_000000").mkdir(parents=True)
    (inp / "retired_uploads" / "20260101_000000" / "older.mp3").write_bytes(b"o" * 32)
    scratch = inp / "processing"
    scratch.mkdir()
    (scratch / "temp_clip_0.mp4").write_bytes(b"s" * 128)
    (inp / "stray_file.tmp").write_bytes(b"t" * 16)
    return {"root": str(root), "input": str(inp),
            "uploads": str(inp / UPLOADS),
            "mp3": str(uploads / "New_Music_Synthetic.mp3")}


def _uploads_tree(app):
    return _snapshot(app["uploads"]) if os.path.isdir(app["uploads"]) else {}


# ===========================================================================================
# GROUP 1 — the incident, as a behavioural gate
# ===========================================================================================

def test_r1_a_failed_retirement_never_deletes_the_uploads(app):
    """THE regression. Inject the incident's own `OSError` and require the bytes to survive.

    This is the test that fails on `143191933f32`: the mp3 is gone, the folder is recreated
    empty, and the only thing on screen says the uploads were "left alone".
    """
    before = _uploads_tree(app)
    assert before, "the fixture did not create an upload to lose"

    _ns, _os_shim, _calls, printed = _run_cleanup(app["root"], rename_error=ACCESS_DENIED)

    assert os.path.isfile(app["mp3"]), (
        "the uploaded file was destroyed after the retirement failed:\n  "
        + "\n  ".join(printed))
    assert _uploads_tree(app) == before, "the uploads tree changed despite the failed retirement"
    with open(app["mp3"], "rb") as fh:
        assert fh.read() == b"\x7f" * 4096, "the uploaded bytes changed"


def test_r2_a_failed_retirement_issues_no_delete_call_against_the_uploads(app):
    """Stronger than "the bytes survived": the sweep must not even *attempt* a delete there.

    Bytes can survive an `ignore_errors=True` rmtree that merely failed, which would leave the
    defect in place behind a lucky filesystem.
    """
    _ns, _os_shim, calls, _printed = _run_cleanup(app["root"], rename_error=ACCESS_DENIED)

    key = os.path.normcase(app["uploads"])
    offenders = [c for c in calls
                 if c[0] in ("rmtree", "remove", "unlink", "rmdir", "move", "replace")
                 and os.path.normcase(c[1]).startswith(key)]
    assert not offenders, f"a delete was issued against the uploads folder: {offenders}"


def test_r3_the_failure_message_does_not_outlive_the_fix(app):
    """The message says the uploads were left alone. After the fix that is finally true, so the
    test asserts the *pairing* rather than the wording: if the message is printed, the files are
    still there."""
    _ns, _os_shim, _calls, printed = _run_cleanup(app["root"], rename_error=ACCESS_DENIED)
    said_left_alone = any("left alone" in line for line in printed)
    assert said_left_alone, printed
    assert os.path.isfile(app["mp3"]), "it said 'left alone' and then they were not"


# ===========================================================================================
# GROUP 2 — the successful path is unchanged
# ===========================================================================================

def test_r4_a_successful_retirement_still_moves_the_uploads_aside(app):
    _ns, _os_shim, _calls, printed = _run_cleanup(app["root"])

    retired = os.path.join(app["input"], "retired_uploads")
    stamped = [d for d in os.listdir(retired) if d.startswith("20261111_")]
    assert len(stamped) == 1, os.listdir(retired)
    moved = os.path.join(retired, stamped[0], "affa6a33deadbeef", "New_Music_Synthetic.mp3")
    assert os.path.isfile(moved), os.listdir(retired)
    with open(moved, "rb") as fh:
        assert fh.read() == b"\x7f" * 4096
    assert not os.path.exists(app["mp3"]), "the source copy should have been MOVED, not copied"
    assert any("Kept last session's uploads" in line for line in printed), printed


def test_r5_the_active_uploads_directory_is_recreated(app):
    """Both paths must leave a usable `gradio_uploads` behind, or the next upload has nowhere
    to go."""
    for rename_error in (None, ACCESS_DENIED):
        fresh = os.path.join(app["root"], "..", f"app_{'fail' if rename_error else 'ok'}")
        shutil.copytree(app["root"], fresh, dirs_exist_ok=True)
        _run_cleanup(fresh, rename_error=rename_error)
        assert os.path.isdir(os.path.join(fresh, "input", UPLOADS)), rename_error


def test_r6_a_name_collision_never_overwrites_an_existing_retirement(app):
    """The clock is frozen, so the second start lands on the first start's name and must advance
    past it rather than merge into or replace it."""
    existing = os.path.join(app["input"], "retired_uploads", "20261111_212009")
    os.makedirs(existing)
    with open(os.path.join(existing, "earlier.mp3"), "wb") as fh:
        fh.write(b"E" * 11)

    _run_cleanup(app["root"])

    with open(os.path.join(existing, "earlier.mp3"), "rb") as fh:
        assert fh.read() == b"E" * 11, "an existing retirement was overwritten"
    siblings = sorted(os.listdir(os.path.join(app["input"], "retired_uploads")))
    assert "20261111_212009_01" in siblings, siblings
    assert os.path.isfile(os.path.join(
        app["input"], "retired_uploads", "20261111_212009_01",
        "affa6a33deadbeef", "New_Music_Synthetic.mp3"))


# ===========================================================================================
# GROUP 3 — everything else about the sweep is unchanged
# ===========================================================================================

@pytest.mark.parametrize("rename_error", [None, ACCESS_DENIED], ids=["retired", "retirement-failed"])
def test_r7_the_other_protected_directories_survive(app, rename_error):
    _run_cleanup(app["root"], rename_error=rename_error)
    assert os.path.isfile(os.path.join(app["input"], "audio", "keep.wav"))
    assert os.path.isfile(os.path.join(app["input"], "video", "keep.mp4"))
    assert os.path.isfile(os.path.join(
        app["input"], "video_analysis_cache", "aabbccdd_0011223344556677889900aa.json"))
    assert os.path.isfile(os.path.join(
        app["input"], "retired_uploads", "20260101_000000", "older.mp3"))


@pytest.mark.parametrize("rename_error", [None, ACCESS_DENIED], ids=["retired", "retirement-failed"])
def test_r8_disposable_scratch_is_still_swept(app, rename_error):
    """The fix must not turn the startup into a no-op: `processing/` is a finished render's
    scratch and its removal is the reason this function exists."""
    _run_cleanup(app["root"], rename_error=rename_error)
    assert not os.path.exists(os.path.join(app["input"], "processing")), \
        "processing scratch was not cleaned, so the sweep stopped doing its job"
    assert not os.path.exists(os.path.join(app["input"], "stray_file.tmp")), \
        "a stray file at the top of input/ was not cleaned"


def test_r9_an_unreadable_uploads_directory_is_protected_too(app):
    """`_retire_previous_uploads` returns early when it cannot even list the folder. That early
    return is the same situation as a failed rename: nothing was retired, so nothing may be
    deleted."""
    _ns, _os_shim, calls, _printed = _run_cleanup(
        app["root"], listdir_error_for=app["uploads"])

    assert os.path.isfile(app["mp3"]), "an unreadable uploads folder was deleted"
    key = os.path.normcase(app["uploads"])
    assert not [c for c in calls if c[0] in ("rmtree", "remove", "unlink")
                and os.path.normcase(c[1]).startswith(key)]


def test_r10_repeated_startups_never_escalate_a_failure_into_a_deletion(app):
    """The docstring promises "the next start tries again". Three failed starts must therefore
    leave three failures and one intact upload — not a deletion on the second."""
    for _ in range(3):
        _run_cleanup(app["root"], rename_error=ACCESS_DENIED)
        assert os.path.isfile(app["mp3"])
    with open(app["mp3"], "rb") as fh:
        assert fh.read() == b"\x7f" * 4096
    assert os.listdir(os.path.join(app["input"], "retired_uploads")) == ["20260101_000000"], \
        "a failed retirement produced a retirement folder"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows reparse point")
def test_r11_a_reparse_point_in_place_of_the_uploads_folder_fails_closed(tmp_path):
    """Unsafe directory state: `gradio_uploads` is a junction onto storage outside `input/`.

    Following it would delete files the app never owned. The protection means it is never
    entered at all — neither the link nor its target may lose anything.
    """
    root = tmp_path / "app"
    inp = root / "input"
    inp.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "not-ours.mp3").write_bytes(b"N" * 77)
    link = inp / UPLOADS
    rc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                        capture_output=True, text=True)
    if rc.returncode != 0 or not os.path.isdir(link):
        pytest.skip(f"could not create a junction: {rc.stdout.strip()} {rc.stderr.strip()}")

    _ns, _os_shim, calls, _printed = _run_cleanup(str(root), rename_error=ACCESS_DENIED)

    assert os.path.isfile(target / "not-ours.mp3"), "the junction was followed and its target lost a file"
    with open(target / "not-ours.mp3", "rb") as fh:
        assert fh.read() == b"N" * 77
    key = os.path.normcase(str(link))
    assert not [c for c in calls if c[0] in ("rmtree", "remove", "unlink", "rmdir")
                and os.path.normcase(c[1]).startswith(key)]


# ===========================================================================================
# GROUP 4 — the protection is load-bearing (mutation control)
# ===========================================================================================

def _drop_uploads_protection(name, node):
    """DEFECT: the uploads name is removed from `protected_dirs`, restoring the incident."""
    if name != CLEANUP_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    for stmt in ast.walk(clone):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and getattr(stmt.targets[0], "id", None) == "protected_dirs"):
            kept = [e for e in stmt.value.elts
                    if UPLOADS not in ast.unparse(e) and "UPLOADS_DIR_NAME" not in ast.unparse(e)
                    or "RETIRED" in ast.unparse(e)]
            assert len(kept) < len(stmt.value.elts), (
                "no uploads entry found in protected_dirs to remove; the control proves nothing")
            stmt.value.elts = kept
            return ast.fix_missing_locations(clone)
    raise AssertionError("no `protected_dirs` assignment found in cleanup_on_startup")


def test_r12_removing_the_protection_restores_the_deletion(app):
    """Without this, every assertion above could be passing for an unrelated reason."""
    _run_cleanup(app["root"], rename_error=ACCESS_DENIED,
                 mutations=(_drop_uploads_protection,))
    assert not os.path.exists(app["mp3"]), (
        "the uploads survived even with the protection removed, so the tests above do not "
        "demonstrate that the protection is what saves them")


# ===========================================================================================
# GROUP 5 — structure, so the protection cannot be quietly undone
# ===========================================================================================

def test_r13_the_uploads_folder_is_named_once_and_protected_by_that_name():
    """The incident's root shape was that the retirement's SOURCE and the sweep's protected set
    named the same folder in two unrelated places. One constant, used by both."""
    source = ast.unparse(_tree())
    assert "GRADIO_UPLOADS_DIR_NAME" in source, \
        "the uploads folder still has no name the two functions can share"
    retire = ast.unparse(_func(RETIRE_FN))
    cleanup = ast.unparse(_func(CLEANUP_FN))
    assert "GRADIO_UPLOADS_DIR_NAME" in retire, \
        "the retirement still spells its own source folder as a literal"
    assert "GRADIO_UPLOADS_DIR_NAME" in cleanup, \
        "the sweep does not protect the folder the retirement reads"


def test_r14_no_new_coordinator_recovery_path_or_rename_was_added():
    """The fix is a protection, not a mechanism.

    Asserted over CALLS, not over the text — the same reason
    `test_open_output_folder.py::test_the_upload_retirement_is_a_move_and_never_a_deletion` gives:
    the docstrings here have to be able to say what this code does *not* do, and a substring scan
    cannot tell an explanation from a violation.
    """
    for fn in (CLEANUP_FN, RETIRE_FN):
        calls = {ast.unparse(n.func) for n in ast.walk(_func(fn)) if isinstance(n, ast.Call)}
        for banned in ("shutil.copy", "shutil.copy2", "shutil.copytree", "shutil.move",
                       "os.replace", "threading.Thread", "subprocess.run", "subprocess.Popen",
                       "atexit.register"):
            assert banned not in calls, (fn, banned)
    renames = [ast.unparse(n) for n in ast.walk(_tree())
               if isinstance(n, ast.Call) and ast.unparse(n.func) == "os.rename"]
    assert len(renames) == 3, renames


def test_r16_the_protected_name_is_the_folder_gradio_actually_uploads_into():
    """Cross-module pin, by AST because neither module imports on a bare interpreter.

    The protection is only worth anything if it names the folder uploads really land in. That
    folder is defined once, in `paths.py`, and `gui.py` publishes its basename to Gradio through
    `os.environ['GRADIO_TEMP_DIR']`. If someone renames it there, the protected set would silently
    stop matching — the same class of drift as the incident, one module further out.

    Deliberately a test rather than `GRADIO_UPLOADS_DIR_NAME = os.path.basename(GRADIO_TEMP_DIR)`:
    the constant stays a plain literal beside `RETIRED_UPLOADS_DIR_NAME`, which is how this file
    already spells protected directory names, and the agreement is asserted instead of imposed.
    """
    paths_tree = ast.parse(open(os.path.join(_SRC, "paths.py"), encoding="utf-8").read())
    temp_dir = next(n for n in paths_tree.body
                    if isinstance(n, ast.Assign) and len(n.targets) == 1
                    and getattr(n.targets[0], "id", None) == "GRADIO_TEMP_DIR")
    assert isinstance(temp_dir.value, ast.Call), ast.unparse(temp_dir.value)
    assert ast.unparse(temp_dir.value.func) == "os.path.join", ast.unparse(temp_dir.value)
    last = temp_dir.value.args[-1]
    assert isinstance(last, ast.Constant), ast.unparse(last)
    leaf = last.value
    assert leaf == UPLOADS, f"paths.GRADIO_TEMP_DIR now ends in {leaf!r}, not {UPLOADS!r}"

    gui_const = next(n for n in _tree().body
                     if isinstance(n, ast.Assign) and len(n.targets) == 1
                     and getattr(n.targets[0], "id", None) == "GRADIO_UPLOADS_DIR_NAME")
    assert isinstance(gui_const.value, ast.Constant)
    assert gui_const.value.value == leaf, (
        f"gui.py protects {gui_const.value.value!r} while uploads land in {leaf!r}")

    # and the env var that tells Gradio where to put them still points at that same folder
    assert "os.environ['GRADIO_TEMP_DIR'] = GRADIO_TEMP_DIR" in ast.unparse(_tree())


def test_r15_the_sweep_still_deletes_and_the_protection_is_not_a_blanket_skip():
    """A `continue` for every entry would pass every preservation test above. The sweep must
    still contain both disposal calls."""
    cleanup = ast.unparse(_func(CLEANUP_FN))
    assert "shutil.rmtree(" in cleanup
    assert "os.remove(" in cleanup
