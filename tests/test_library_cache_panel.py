"""R14-A stage A6 — the Library cache report panel, under owner decision R-3 Option 2 + W-a.

The owner decided two things, and this suite exists to prove the implementation obeys both:

* **reach** — the panel may name the configured shared analysis-cache folder *without contacting
  it*. Only behind an explicit, **default-off** per-use opt-in may it take **one** bounded
  directory-state observation through A4, which opens nothing. No record read, no recursion, no
  count, no byte total, no reclaim figure, no write, no deletion — opt-in or not.
* **wording** — A5's two shipped sentences are approved exactly as they stand, so A6 must reference
  them **by identity** rather than copying their English into new code.

`gui.py` cannot be imported on a bare interpreter (`logger` pulls in librosa, `gpu_cpu_utils`
probes NVENC), so the REAL A6 functions and the REAL A6 constants are AST-extracted from `gui.py`
and executed against a synthesised namespace. The A1–A5 fork modules **are** imported normally —
they are stdlib-only by rule — so what runs here is the real A4 observer and the real A5 renderer,
not a mock of either.

Three test-design corrections are deliberate and are called out where they apply:

1. **`inspect_location` has no `make_dirs=` parameter.** It constructs
   `RefuseDirectoryCreation()` internally and hands it to `analysis_cache.resolve_location`. So the
   opt-in's no-creation guarantee is measured at that *internal resolver seam*, on
   `creation_attempts`, and on a real folder that stays absent — never by asserting a keyword that
   does not exist, which would pass without testing anything.
2. **Digits are not banned; metrics are.** A configured path legitimately contains digits, and so
   does an observation timestamp. The ban is on record counts, byte totals and reclaim figures, and
   on any false claim of a clean zero.
3. **Zero-I/O assertions are scoped to the SHARED folder.** Default-off forbids all shared-cache
   filesystem contact and specifically any A4 call. The default handler here is name-only
   *overall* — it touches neither folder — and that is proved as its own property rather than
   presented as an A1–A5 local report, which it is not.

Group A carries an executable **negative control**: the handler's own gate is AST-mutated so the
default path probes, and the suite proves the Group A assertions then go red. A zero-call assertion
that cannot fail proves nothing.
"""

from __future__ import annotations

import ast
import builtins
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from beatsync_fork import analysis_cache as ac                      # noqa: E402
from beatsync_fork import library_cache_location as a4              # noqa: E402
from beatsync_fork import library_cache_report as a5                # noqa: E402
from beatsync_fork import library_inventory as a1                   # noqa: E402

HANDLER = "_on_library_cache_report_click"
LINES_FN = "_a6_cache_report_lines"
STATE_FN = "_a6_state_sentence"
SNAPSHOT_FN = "_a6_shared_cache_startup_snapshot"
BUTTON = "library_cache_report_btn"
CHECKBOX = "library_cache_probe_shared"
REPORT_BOX = "library_cache_report_box"

#: The A6 module-level constants the functions read. Executed from the REAL `gui.py` source, in
#: source order, so every assertion below is made against the shipped text rather than a copy.
A6_CONSTANTS = (
    "LABEL_LIBRARY_CACHE_REPORT",
    "LABEL_LIBRARY_CACHE_PROBE_SHARED",
    "LABEL_LIBRARY_CACHE_BUILD",
    "INFO_LIBRARY_CACHE_REPORT",
    "A6_LOCAL_CACHE_DIRNAME",
    "A6_SHARED_STATE_PREFIX",
    "A6_LOCAL_STATE_PREFIX",
    "A6_STATE_WORDING",
    "A6_STATE_UNRECOGNISED",
    "A6_STARTUP_SNAPSHOT_NOTE",
    "A6_NAME_ONLY_NOTE",
    "A6_NO_SHARED_FOLDER_LINE",
    "A6_OPT_IN_HEADLINE",
    "A6_OPT_IN_LOCAL_DISCLOSURE",
    "A6_CREATION_REFUSED_PREFIX",
    "A6_CREATION_NONE_REQUESTED",
    "A6_SHARING_ON_LINE",
    "A6_SHARING_OFF_LINE",
)


# ---------------------------------------------------------------------------
# AST plumbing (the conventions of tests/test_open_output_folder.py)
# ---------------------------------------------------------------------------

def _source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _tree(path: str = _GUI) -> ast.Module:
    return ast.parse(_source(path), filename=path)


def _func(name: str, path: str = _GUI) -> ast.FunctionDef:
    return next(n for n in ast.walk(_tree(path))
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _code(node: ast.FunctionDef) -> str:
    """The function's EXECUTABLE body, docstring removed.

    Load-bearing rather than tidiness: these functions document in prose what they deliberately do
    NOT call, so they legitimately name `read_records`, `load_config` and `makedirs` in their own
    docstrings. A substring ban over raw source would trip on the explanation of the ban — which
    has happened repeatedly in this repository. Usage is banned structurally instead.
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


def _assign(name: str) -> ast.Assign:
    """The single assignment to `name`, found by WALKING the tree.

    `_tree().body` would only reach module level, and every widget in this file is declared inside
    nested `with gr.Blocks()/gr.Tab()/gr.Accordion()` blocks.
    """
    found = [n for n in ast.walk(_tree())
             if isinstance(n, ast.Assign) and len(n.targets) == 1
             and getattr(n.targets[0], "id", None) == name]
    assert len(found) == 1, f"expected exactly one assignment to {name}, found {len(found)}"
    return found[0]


def _click(widget: str) -> ast.Call:
    return next(n for n in ast.walk(_tree())
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "click" and getattr(n.func.value, "id", None) == widget)


# ---------------------------------------------------------------------------
# recording shims
# ---------------------------------------------------------------------------

class _FsSpy:
    """Records every filesystem-touching call the loaded code makes through its own namespace.

    `os.path` is delegated with overrides so pure-path helpers (`join`, `basename`) keep working;
    only the functions that actually reach a disk are recorded.
    """

    RECORDED = ("listdir", "scandir", "stat", "lstat", "makedirs", "mkdir", "remove", "rmdir",
                "rename", "replace", "walk", "unlink")
    PATH_RECORDED = ("isdir", "isfile", "exists", "realpath", "getsize", "lexists")

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        spy = self

        class _Path:
            def __getattr__(self, item):
                target = getattr(os.path, item)
                if item in _FsSpy.PATH_RECORDED:
                    def recorded(*a, **k):
                        spy.calls.append((item, str(a[0]) if a else ""))
                        return target(*a, **k)
                    return recorded
                return target

        self.path = _Path()

    def __getattr__(self, item):
        target = getattr(os, item)
        if item in self.RECORDED:
            spy = self

            def recorded(*a, **k):
                spy.calls.append((item, str(a[0]) if a else ""))
                return target(*a, **k)
            return recorded
        return target

    def touching(self, needle: str) -> list[tuple[str, str]]:
        key = os.path.normcase(needle)
        return [c for c in self.calls if key in os.path.normcase(c[1])]


class _ModuleSpy:
    """Wraps a real module, counting calls to named functions and delegating everything else.

    The wrapped module is the genuine article, so what runs underneath is real A4 / real A5.
    """

    def __init__(self, module, *watched):
        self._module = module
        self._watched = set(watched)
        self.calls: dict[str, list] = {name: [] for name in watched}

    def __getattr__(self, item):
        target = getattr(self._module, item)
        if item in self._watched:
            log = self.calls[item]

            def recorded(*a, **k):
                log.append((a, dict(k)))
                return target(*a, **k)
            return recorded
        return target

    def count(self, name: str) -> int:
        return len(self.calls[name])


class _OpenSpy:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, file, *a, **k):
        self.calls.append(str(file))
        return builtins.open(file, *a, **k)


class watch_a4_filesystem:  # noqa: N801  (a context manager, named for what it does)
    """Record EVERY filesystem call A4 makes, at the only seam that actually sees them.

    This exists because of a measured hole in the obvious approach. `_FsSpy` replaces the `os`
    name in the A6 code's own namespace, so it sees what the A6 handler does directly — and
    NOTHING that A4 does, because `inspect_location` binds `listdir=os.listdir`,
    `lstat=os.lstat`, `isdir=os.path.isdir` and `realpath=os.path.realpath` as keyword defaults at
    A4's import time and then passes them explicitly down to A1's `probe_root`. Neither patching
    the `os` module nor patching `probe_root` is seen; the defaults themselves are what production
    resolves.

    Without this, Group A's "no filesystem call" assertion would be vacuous with respect to the
    one component that does the actual probing — which the Group A negative control demonstrated
    before this class existed.
    """

    WATCHED = {"listdir": os.listdir, "lstat": os.lstat,
               "isdir": os.path.isdir, "realpath": os.path.realpath}

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._saved: dict = {}

    def __enter__(self):
        defaults = a4.inspect_location.__kwdefaults__
        for name, real in self.WATCHED.items():
            assert defaults[name] is real, (
                f"A4's {name} default is not the real one; this seam needs re-measuring")
            self._saved[name] = defaults[name]
            defaults[name] = self._record(name, real)
        return self

    def __exit__(self, *exc):
        defaults = a4.inspect_location.__kwdefaults__
        for name, real in self._saved.items():
            defaults[name] = real
        return False

    def _record(self, name, real):
        def recorded(path, *a, **k):
            self.calls.append((name, os.path.normcase(os.path.abspath(str(path)))))
            return real(path, *a, **k)
        return recorded

    def touching(self, needle: str) -> list[tuple[str, str]]:
        key = os.path.normcase(os.path.abspath(needle))
        return [c for c in self.calls if c[1] == key]


# ---------------------------------------------------------------------------
# the loader: REAL A6 code over REAL A1-A5, in a synthesised namespace
# ---------------------------------------------------------------------------

def load_a6(*, shared_config=None, local_default="", fs=None, open_fn=None,
            cache_location=None, cache_report=None, mutate_gate=False) -> dict:
    """Execute the real A6 constants and functions against a controlled namespace."""
    fs = fs if fs is not None else _FsSpy()
    namespace: dict = {
        "os": fs,
        "datetime": __import__("datetime"),
        "get_input_dir": (lambda: local_default),
        "_SHARED_CACHE_CONFIG": {} if shared_config is None else dict(shared_config),
        "fork_analysis_cache": ac,
        "fork_cache_location": cache_location if cache_location is not None else a4,
        "fork_cache_report": cache_report if cache_report is not None else a5,
        "open": open_fn if open_fn is not None else _OpenSpy(),
        "__builtins__": builtins,
    }

    tree = _tree()
    wanted = set(A6_CONSTANTS)
    body: list = [n for n in tree.body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and getattr(n.targets[0], "id", None) in wanted]
    missing = wanted - {n.targets[0].id for n in body}
    assert not missing, f"A6 constants absent from gui.py: {sorted(missing)}"

    for name in (SNAPSHOT_FN, STATE_FN, LINES_FN, HANDLER):
        node = _func(name)
        if mutate_gate and name == LINES_FN:
            node = _mutate_gate_closed(node)
        body.append(node)

    exec(compile(ast.Module(body=body, type_ignores=[]), "<gui-a6>", "exec"),
         namespace)  # noqa: S102
    namespace["_fs"] = fs
    namespace["_open"] = namespace["open"]
    return namespace


def _mutate_gate_closed(node: ast.FunctionDef) -> ast.FunctionDef:
    """The NEGATIVE CONTROL: force the opt-in gate to take the PROBE branch unconditionally.

    The gate lives in `_a6_cache_report_lines` as `if probe_shared_folder is not True: <return the
    name-only report>`. Inverting that one test to a constant `False` makes execution fall through
    to the probe branch for every caller, including the default one — which is precisely the
    defect Group A must detect. Nothing else is altered.

    Located structurally on the parameter name, and the located count is asserted: a control that
    silently failed to find the gate would make Group A look verified when it is not.
    """
    param = node.args.args[0].arg
    clone = ast.parse(ast.unparse(node)).body[0]
    hits = 0
    for sub in ast.walk(clone):
        if isinstance(sub, ast.If) and param in {
                n.id for n in ast.walk(sub.test) if isinstance(n, ast.Name)}:
            sub.test = ast.Constant(False)
            hits += 1
    assert hits == 1, (
        f"expected exactly one gate on {param!r} in {node.name}; found {hits}. The negative "
        "control cannot be trusted against a gate it did not locate.")
    return ast.fix_missing_locations(clone)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    """A realistic pair of folders, plus a manifest helper for matrix 15."""
    local = tmp_path / "input" / "video_analysis_cache"
    shared = tmp_path / "SharedCache" / "VideoAnalysis"
    local.mkdir(parents=True)
    shared.mkdir(parents=True)
    (shared / "a_1234567890abcdef.json").write_text('{"v": 1}', encoding="utf-8")
    (local / "b_fedcba0987654321.json").write_text('{"v": 2}', encoding="utf-8")
    return {
        "root": str(tmp_path),
        "local_parent": str(tmp_path / "input"),
        "local": str(local),
        "shared": str(shared),
        "absent_live_parent": str(tmp_path / "SharedCache" / "NotCreated"),
        "absent_dead_parent": str(tmp_path / "NoSuchDisk" / "Nested" / "Shared"),
    }


def manifest(root: str) -> dict:
    """`(relpath, size, mtime_ns)` for every entry, plus the directory set — matrix 15's own
    instrument, so a created folder or a left-behind temporary cannot hide."""
    seen: dict = {}
    for base, dirs, files in os.walk(root):
        for name in dirs:
            p = os.path.join(base, name)
            seen[os.path.relpath(p, root)] = ("dir", None, None)
        for name in files:
            p = os.path.join(base, name)
            st = os.stat(p)
            seen[os.path.relpath(p, root)] = ("file", st.st_size, st.st_mtime_ns)
    return seen


def cfg(folder: str, enabled: bool) -> dict:
    return {ac.CACHE_FOLDER: folder, ac.CACHE_ENABLED: enabled}


def shared_lines(text: str, ns: dict) -> list[str]:
    """Only the lines A6 itself authored ABOUT THE SHARED FOLDER.

    Scoped deliberately (correction 3): A5's own rendered block legitimately contains the word
    'availability' and the local folder's own lines legitimately say 'present', so a whole-report
    substring assertion would be both false-positive and false-negative prone.
    """
    prefix = ns["A6_SHARED_STATE_PREFIX"].strip()
    return [ln for ln in text.splitlines() if ln.strip().startswith(prefix)]


def reported_verdicts(lines: list[str], ns: dict) -> set[str]:
    """The verdict TOKEN each state line reports, parsed rather than substring-matched.

    The line shape is `<prefix><verdict> -- <sentence>`, so the verdict is recoverable exactly.
    Matching `'present' in line` instead would be satisfied by the word inside
    'no folder is present at the configured location', which reports the opposite.
    """
    prefix = ns["A6_SHARED_STATE_PREFIX"].strip()
    found = set()
    for ln in lines:
        body = ln.strip()[len(prefix):].strip()
        found.add(body.split(" -- ")[0].strip())
    return found


# ===========================================================================
# GROUP A — the default mode makes NO filesystem contact at all
# ===========================================================================

def test_a1_default_mode_makes_no_filesystem_call_at_either_seam(tree):
    """Both seams, because either alone is incomplete.

    `_fs` covers what the A6 handler does through its own `os`; `watch_a4_filesystem` covers what
    A4 would do through its import-time-bound defaults. Only together do they mean "nothing
    reached a disk".
    """
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        text = ns[HANDLER](False)
    assert isinstance(text, str) and text.strip()
    assert ns["_fs"].calls == [], f"default mode touched the filesystem: {ns['_fs'].calls}"
    assert ns["_open"].calls == [], f"default mode opened files: {ns['_open'].calls}"
    assert a4fs.calls == [], f"default mode reached a disk through A4: {a4fs.calls}"
    assert a4fs.touching(tree["shared"]) == []
    assert a4fs.touching(tree["local"]) == []


def test_a2_default_mode_does_not_call_a4_at_all(tree):
    """The load-bearing one. Not merely 'no syscall happened' — A4 itself is never entered.

    A syscall-only assertion would be satisfied by a future refactor that reached the disk through
    a new route, and A4 has no depth-0 mode: `inspect_location` probes the local folder
    unconditionally, so entering it at all is already contact.
    """
    spy = _ModuleSpy(a4, "inspect_location")
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                 cache_location=spy)
    ns[HANDLER](False)
    assert spy.count("inspect_location") == 0, "default mode entered A4"


def test_a3_default_mode_modifies_nothing_on_disk(tree):
    """Matrix 15, scoped to the default path: before/after manifest plus the directory set."""
    before = manifest(tree["root"])
    ns = load_a6(shared_config=cfg(tree["absent_live_parent"], True),
                 local_default=tree["local_parent"])
    ns[HANDLER](False)
    assert manifest(tree["root"]) == before
    assert not os.path.isdir(tree["absent_live_parent"]), \
        "the configured folder was created by a read-only report"


def test_a4_the_negative_control_proves_group_a_can_fail(tree):
    """Force the gate open and show the Group A assertions go red.

    Without this, `calls == 0` could be passing because the handler does nothing at all.
    """
    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                     cache_location=spy, mutate_gate=True)
        ns[HANDLER](False)
    assert spy.count("inspect_location") == 1, \
        "the mutant did not probe, so test_a2's zero-call assertion proves nothing"
    assert a4fs.calls != [], \
        "the mutant reached no disk, so test_a1's A4 filesystem watcher proves nothing"
    assert a4fs.touching(tree["shared"]), \
        "the mutant never touched the SHARED folder, which is the contact R-3 gates"


def test_a5_a_non_boolean_truthy_value_does_not_enable_probing(tree):
    """Fail closed. Only a real `True` opts in; `1`, `'true'` and `'yes'` must not."""
    for sneaky in (1, "true", "yes", "on", [1], 2.0):
        spy = _ModuleSpy(a4, "inspect_location")
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                     cache_location=spy)
        ns[HANDLER](sneaky)
        assert spy.count("inspect_location") == 0, f"{sneaky!r} enabled shared-folder probing"


def test_a6_the_handler_parameter_itself_defaults_to_false():
    node = _func(HANDLER)
    assert len(node.args.args) == 1, "the handler takes exactly the opt-in flag"
    assert len(node.args.defaults) == 1, "the opt-in parameter carries no default"
    default = node.args.defaults[0]
    assert isinstance(default, ast.Constant) and default.value is False, \
        f"the opt-in default is {ast.unparse(default)}, not False"


def test_a7_default_mode_does_not_present_itself_as_a_library_report(tree):
    """Correction 3: the default read-out is name-only OVERALL, and says so.

    It must not imply an A1-A5 local measurement happened, and it must not report the local folder
    as inspected either.
    """
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](False)
    assert ns["A6_NAME_ONLY_NOTE"] in text
    for line in shared_lines(text, ns):
        assert a1.NOT_INSPECTED in line, line
    local_prefix = ns["A6_LOCAL_STATE_PREFIX"].strip()
    local_rendered = [ln for ln in text.splitlines() if ln.strip().startswith(local_prefix)]
    assert local_rendered, "the default read-out is silent about the local folder"
    for line in local_rendered:
        assert a1.NOT_INSPECTED in line, line


# ===========================================================================
# GROUP B — the opt-in is bounded to depth 1
# ===========================================================================

def test_b1_opt_in_calls_a4_exactly_once(tree):
    spy = _ModuleSpy(a4, "inspect_location")
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                 cache_location=spy)
    ns[HANDLER](True)
    assert spy.count("inspect_location") == 1


def test_b1b_the_opt_in_really_does_reach_the_shared_folder(tree):
    """The companion every "default makes no contact" claim needs.

    Without it, Group A would be equally satisfied by a panel whose opt-in never worked at all —
    and a gate that is always shut is indistinguishable from a gate that is always open to a
    suite that only ever checks one side.
    """
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        ns[HANDLER](True)
    assert a4fs.touching(tree["shared"]), "the opt-in never reached the shared folder"
    assert a4fs.touching(tree["local"]), \
        "A4 is documented as probing the local folder unconditionally; it did not"


def test_b2_no_make_dirs_keyword_is_invented_and_the_real_refusal_is_used(tree):
    """CORRECTION 1, in full.

    `inspect_location`'s public signature has no `make_dirs`; it builds
    `RefuseDirectoryCreation()` itself. So this measures the INTERNAL resolver seam: every
    `resolve_location` call made during the opt-in receives a `RefuseDirectoryCreation` as
    `make_dirs`. Asserting a keyword on `inspect_location` would be a fake pass.
    """
    assert "make_dirs" not in {a.arg for a in _func("inspect_location",
                                                    a4.__file__).args.kwonlyargs}, \
        "A4 grew a public make_dirs parameter; this test's premise needs re-measuring"

    seen: list = []
    real_resolve = a4.resolve_location

    def watching_resolve(*a, **k):
        seen.append(k.get("make_dirs", "ABSENT"))
        return real_resolve(*a, **k)

    spy = _ModuleSpy(a4, "inspect_location")
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                 cache_location=spy)
    a4.resolve_location = watching_resolve
    try:
        ns[HANDLER](True)
    finally:
        a4.resolve_location = real_resolve

    assert seen, "no resolver call was observed during the opt-in"
    for got in seen:
        assert isinstance(got, a4.RefuseDirectoryCreation), \
            f"a resolver call received make_dirs={got!r} instead of the refusal"


def test_b3_the_opt_in_creates_nothing_and_says_the_creation_was_refused(tree):
    """A configured-but-absent folder whose parent is live is the measured hazard case: the
    resolver asks to create it. Nothing may appear, and the refusal is reported as positive
    evidence rather than as silence."""
    before = manifest(tree["root"])
    ns = load_a6(shared_config=cfg(tree["absent_live_parent"], True),
                 local_default=tree["local_parent"])
    text = ns[HANDLER](True)
    assert not os.path.isdir(tree["absent_live_parent"]), "the opt-in created the folder"
    assert manifest(tree["root"]) == before
    assert ns["A6_CREATION_REFUSED_PREFIX"].strip() in text
    assert tree["absent_live_parent"] in text


def test_b4_the_opt_in_opens_no_file_at_all(tree):
    """Depth 1 means directory state. A record read is depth 2 and was REJECTED by the owner."""
    opened = _OpenSpy()
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                 open_fn=opened)
    ns[HANDLER](True)
    assert opened.calls == [], f"the opt-in opened files: {opened.calls}"


def test_b5_no_a3_record_reader_is_reachable_from_the_a6_call_graph():
    """Structural, so a rename cannot evade it: the A6 bodies name no record-reading symbol."""
    bodies = "\n".join(_code(_func(n)) for n in (HANDLER, LINES_FN, STATE_FN, SNAPSHOT_FN))
    walked = ast.parse(bodies)
    used = {ast.unparse(n) for n in ast.walk(walked)
            if isinstance(n, (ast.Name, ast.Attribute))}
    for forbidden in ("read_records", "library_cache_records", "fork_cache_records",
                      "estimate_reclaimable_records", "measure", "fork_cache.measure",
                      "analysis_cache.measure", "is_record_name", "read_record",
                      "inventory_root", "fork_prep", "scan_library_folder", "os.walk",
                      "os.scandir", "os.makedirs", "os.remove", "os.rmdir", "os.rename",
                      "shutil"):
        assert forbidden not in used, f"{forbidden} is reachable from the A6 call graph"


def test_b6_the_opt_in_lists_neither_folders_subdirectories(tree):
    """No recursion: the configured folder and the local folder are each listed once, and nothing
    below either is listed. Measured on the real A4/A1 pair, not asserted about it."""
    deep = os.path.join(tree["shared"], "nested")
    os.makedirs(deep)
    (open(os.path.join(deep, "c_0011223344556677.json"), "w", encoding="utf-8")).close()

    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        ns[HANDLER](True)

    listed = {path for name, path in a4fs.calls if name == "listdir"}
    allowed = {os.path.normcase(os.path.abspath(tree["shared"])),
               os.path.normcase(os.path.abspath(tree["local"]))}
    assert listed, "nothing was listed, so this test proves nothing about recursion"
    assert listed <= allowed, f"listed outside the two named folders: {listed - allowed}"
    assert os.path.normcase(os.path.abspath(deep)) not in listed, "the opt-in recursed"
    # one listing per folder, not a repeated walk
    listdir_calls = [c for c in a4fs.calls if c[0] == "listdir"]
    assert len(listdir_calls) == len(listed), f"a folder was listed twice: {listdir_calls}"


def test_b7_the_checkbox_component_defaults_to_false():
    rendered = ast.unparse(_assign(CHECKBOX).value)
    assert rendered.startswith("gr.Checkbox("), rendered
    assert "value=False" in rendered, f"the opt-in checkbox is not default-off: {rendered}"


def test_b8_the_checkbox_registers_no_change_listener_that_could_probe():
    """Only a deliberate button click may probe. A `.change()` on the opt-in would make ticking
    the box itself reach the disk.

    The existence assertion comes FIRST and is load-bearing: "no listener was found" is also true
    of a checkbox that does not exist, so without it this test passed before the panel was
    written — measured, not hypothetical.
    """
    assert _assign(CHECKBOX) is not None
    changes = [n for n in ast.walk(_tree())
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr in {"change", "input", "select"}
               and getattr(n.func.value, "id", None) == CHECKBOX]
    assert changes == [], "the opt-in checkbox registered an event of its own"


def test_b9_the_opt_in_discloses_that_the_local_folder_was_probed_too(tree):
    """A4 cannot probe the shared folder without also probing the local one. The opt-in gates
    SHARED contact, which is what R-3 is about — but the panel must not imply the local folder
    went untouched."""
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](True)
    assert ns["A6_OPT_IN_LOCAL_DISCLOSURE"] in text


# ===========================================================================
# GROUP C — four states that are NOT a clean zero
# ===========================================================================

#: Every state A4 can report, and the tokens that would misrepresent it. `available` is listed
#: with a word boundary in mind — see the assertion, which must not trip on `unavailable`.
_FORBIDDEN_TOKENS = ("empty", "clean", "obsolete", "no records", "nothing in it")


def test_c1_every_a4_state_has_wording_and_the_set_matches_a4_exactly():
    """The wording table is keyed by literals; this pins those literals to A4's own authority, so
    a new or renamed state cannot slip through unworded."""
    ns = load_a6()
    assert set(ns["A6_STATE_WORDING"]) == set(a4.LOCATION_STATES), (
        f"wording covers {sorted(ns['A6_STATE_WORDING'])} but A4 reports "
        f"{sorted(a4.LOCATION_STATES)}")
    assert len(a4.LOCATION_STATES) == 6


@pytest.mark.parametrize("verdict", list(a4.LOCATION_STATES))
def test_c2_no_state_renders_as_a_clean_zero(verdict):
    ns = load_a6()
    sentence = ns[STATE_FN](verdict, "2026-11-08T09:00:00")
    assert sentence.strip(), verdict
    low = sentence.lower()
    for token in _FORBIDDEN_TOKENS:
        assert token not in low, f"{verdict} rendered with {token!r}: {sentence}"
    # CORRECTION 2: `unavailable` is legitimate; a bare `available` claim is not.
    assert "unavailable" in low or "available" not in low, \
        f"{verdict} claims availability: {sentence}"


def test_c3_access_denied_is_positively_observed_not_missing():
    ns = load_a6()
    sentence = ns[STATE_FN](a1.ACCESS_DENIED, "2026-11-08T09:00:00").lower()
    assert "refused" in sentence or "denied" in sentence, sentence
    for token in ("missing", "absent", "not there", "does not exist"):
        assert token not in sentence, f"ACCESS_DENIED rendered as absence: {sentence}"


def test_c4_present_is_an_observation_at_a_moment_not_continuing_availability():
    ns = load_a6()
    stamp = "2026-11-08T09:00:00"
    sentence = ns[STATE_FN](a1.PRESENT, stamp)
    assert stamp in sentence, f"PRESENT does not carry its observation time: {sentence}"
    assert "as observed" in sentence.lower(), sentence
    assert "currently" not in sentence.lower(), sentence


def test_c5_an_unrecognised_verdict_fails_closed(tree):
    ns = load_a6()
    sentence = ns[STATE_FN]("a_state_that_does_not_exist", "")
    assert sentence == ns["A6_STATE_UNRECOGNISED"]
    low = sentence.lower()
    for token in _FORBIDDEN_TOKENS:
        assert token not in low, sentence


def test_c6_a_denied_shared_folder_reaches_the_report_as_denied(tree):
    """End-to-end through the real handler, with A4's own probe seam driven to ACCESS_DENIED.

    `probe_root` is replaced on the A4 module, which is the name A4's `_probe` actually calls.
    That is a harness device for REACHING a state that cannot be produced on an ordinary test
    disk; nothing injected here exists in the production path.
    """
    real_probe = a4.probe_root
    target = os.path.normcase(os.path.abspath(tree["shared"]))

    def denied(path, **k):
        if os.path.normcase(os.path.abspath(str(path))) == target:
            return a1.RootState(typed=str(path), real=str(path), anchor=str(path),
                                verdict=a1.ACCESS_DENIED, anchor_reachable=True, exists=True,
                                detail="refused")
        return real_probe(path, **k)

    a4.probe_root = denied
    try:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        text = ns[HANDLER](True)
    finally:
        a4.probe_root = real_probe

    lines = shared_lines(text, ns)
    assert lines, "the report says nothing about the shared folder"
    assert any(a1.ACCESS_DENIED in ln for ln in lines), lines
    for ln in lines:
        low = ln.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in low, ln


def test_c7_a_fallback_does_not_let_a_healthy_local_cache_read_as_success(tree):
    """Under `fallback`, `active` is False however healthy the local cache is. The panel must
    surface the REQUESTED folder's state, not the stand-in's."""
    ns = load_a6(shared_config=cfg(tree["absent_dead_parent"], True),
                 local_default=tree["local_parent"])
    text = ns[HANDLER](True)
    lines = shared_lines(text, ns)
    assert lines
    assert reported_verdicts(lines, ns) and a1.PRESENT not in reported_verdicts(lines, ns), \
        f"an unreachable shared folder reported as present: {lines}"
    assert tree["absent_dead_parent"] in text
    # the healthy local cache is still reported, as itself, on its own line
    local_prefix = ns["A6_LOCAL_STATE_PREFIX"].strip()
    assert any(ln.strip().startswith(local_prefix) for ln in text.splitlines())


def test_c8_sharing_switched_off_leaves_the_stored_path_reported_but_not_inspected(tree):
    ns = load_a6(shared_config=cfg(tree["shared"], False), local_default=tree["local_parent"])
    text = ns[HANDLER](True)
    assert tree["shared"] in text, "turning sharing off hid the stored folder"
    assert any(a1.NOT_INSPECTED in ln for ln in shared_lines(text, ns))
    assert ns["A6_SHARING_OFF_LINE"] in text


# ===========================================================================
# GROUP D — no METRIC about the shared folder, in either mode
# ===========================================================================

#: CORRECTION 2. Digits are allowed (paths and timestamps carry them); these patterns are not.
_METRIC_PATTERNS = (
    r"\b\d+\s*(record|records|entr|entries|file|files|byte|bytes|item|items)\b",
    r"\b\d+(\.\d+)?\s*(B|KB|KiB|MB|MiB|GB|GiB|TB|TiB)\b",
    r"\breclaim\w*\s*:?\s*\d",
    r"\btotal\w*\s*:?\s*\d",
    r"\bcount\w*\s*:?\s*\d",
)


@pytest.mark.parametrize("probe", [False, True])
def test_d1_no_metric_is_published_about_the_shared_folder(tree, probe):
    import re
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](probe)
    for line in shared_lines(text, ns):
        for pattern in _METRIC_PATTERNS:
            assert not re.search(pattern, line, re.I), \
                f"metric published about the shared folder (probe={probe}): {line}"


@pytest.mark.parametrize("probe", [False, True])
def test_d2_the_whole_report_publishes_no_byte_or_record_metric(tree, probe):
    """Wider than D1 and still metric-scoped: with no inventory supplied, A5 withholds every
    figure, so the complete read-out should carry no count or byte total about either folder."""
    import re
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](probe)
    for line in text.splitlines():
        for pattern in _METRIC_PATTERNS[:2]:
            assert not re.search(pattern, line, re.I), f"metric published: {line}"


def test_d3_the_metric_patterns_actually_match_a_metric():
    """A negative control for D1/D2: a regex set that matched nothing would pass vacuously."""
    import re
    for sample in ("4821 records", "12.5 GiB", "reclaimable: 400", "total: 17", "count: 3"):
        assert any(re.search(p, sample, re.I) for p in _METRIC_PATTERNS), sample
    for benign in ("C:\\Users\\a6probe_4q\\SharedCache\\VideoAnalysis",
                   "as observed at 2026-11-08T09:00:00"):
        assert not any(re.search(p, benign, re.I) for p in _METRIC_PATTERNS), benign


# ===========================================================================
# GROUP E — W-a, by identity and never by a copied literal
# ===========================================================================

@pytest.mark.parametrize("probe", [False, True])
def test_e1_both_approved_sentences_appear_when_a_folder_is_configured(tree, probe):
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](probe)
    assert a5.SHARED_SENTENCE in text, "the approved SHARED_SENTENCE is missing"
    assert a5.OUT_OF_SCOPE_SENTENCE in text, "the approved OUT_OF_SCOPE_SENTENCE is missing"


def test_e2_the_sentences_are_referenced_by_identity_not_copied(tree):
    """A copied string is how approved wording silently diverges from what was approved."""
    bodies = "\n".join(_code(_func(n)) for n in (HANDLER, LINES_FN, STATE_FN, SNAPSHOT_FN))
    for name in ("SHARED_SENTENCE", "OUT_OF_SCOPE_SENTENCE"):
        assert f"fork_cache_report.{name}" in bodies, f"{name} is not referenced by identity"
    literals = {n.value for n in ast.walk(ast.parse(bodies))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    constants = _tree()
    declared = {ast.unparse(n.value) for n in constants.body
                if isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], "id", None) in set(A6_CONSTANTS)}
    for approved in (a5.SHARED_SENTENCE, a5.OUT_OF_SCOPE_SENTENCE):
        assert approved not in literals, "an approved sentence was copied into A6 code"
        for rendered in declared:
            assert approved not in rendered, "an approved sentence was copied into an A6 constant"


def test_e3_the_sentences_are_absent_when_no_folder_is_configured(tree):
    """A5's gate is `shared_in_use OR configured`; with nothing configured there is no shared
    folder to caveat, and inventing the caveat would describe a folder that does not exist."""
    ns = load_a6(shared_config=cfg("", False), local_default=tree["local_parent"])
    text = ns[HANDLER](False)
    assert ns["A6_NO_SHARED_FOLDER_LINE"] in text
    assert a5.SHARED_SENTENCE not in text


# ===========================================================================
# GROUP F — the startup snapshot, labelled honestly
# ===========================================================================

def test_f1_the_report_labels_the_configuration_as_loaded_at_startup(tree):
    """`_on_save_shared_cache` does not refresh `_SHARED_CACHE_CONFIG`, so after an in-session
    Save the dict is stale relative to the file. The panel must never claim it is current."""
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    for probe in (False, True):
        text = ns[HANDLER](probe)
        assert ns["A6_STARTUP_SNAPSHOT_NOTE"] in text
        low = text.lower()
        for claim in ("currently configured", "configuration now in force", "now in force"):
            assert claim not in low, f"the panel claimed currency: {claim}"


def test_f2_no_live_configuration_read_is_reachable_from_the_a6_call_graph():
    """The owner forbade a new direct read of the live `shared-config.json`, and
    `_analysis_cache_location` is forbidden for a second reason: `resolve_location` defaults
    `make_dirs` to the real `os.makedirs`, so it CREATES the configured folder."""
    bodies = "\n".join(_code(_func(n)) for n in (HANDLER, LINES_FN, STATE_FN, SNAPSHOT_FN))
    used = {ast.unparse(n) for n in ast.walk(ast.parse(bodies))
            if isinstance(n, (ast.Name, ast.Attribute))}
    for forbidden in ("load_config", "config_path", "default_config_path", "save_config",
                      "fork_analysis_cache.load_config", "fork_analysis_cache.config_path",
                      "fork_analysis_cache.save_config", "_analysis_cache_location",
                      "resolve_cache_location", "video_analysis.resolve_cache_location",
                      "resolve_location", "fork_analysis_cache.resolve_location",
                      "_on_save_shared_cache", "_shared_cache_report"):
        assert forbidden not in used, f"{forbidden} is reachable from the A6 call graph"


def test_f3_the_snapshot_reader_reads_the_existing_in_memory_dict(tree):
    ns = load_a6(shared_config=cfg(tree["shared"], True))
    folder, enabled = ns[SNAPSHOT_FN]()
    assert folder == tree["shared"]
    assert enabled is True
    assert ns["_fs"].calls == [], "reading the startup snapshot touched the filesystem"


def test_f4_the_snapshot_reader_tolerates_a_missing_or_junk_setting():
    for junk in ({}, {ac.CACHE_FOLDER: None}, {ac.CACHE_ENABLED: "yes"},
                 {ac.CACHE_FOLDER: "   ", ac.CACHE_ENABLED: "1"}):
        ns = load_a6(shared_config=junk)
        folder, enabled = ns[SNAPSHOT_FN]()
        assert isinstance(folder, str)
        assert isinstance(enabled, bool)


def test_f5_the_handler_never_mutates_the_startup_snapshot(tree):
    original = cfg(tree["shared"], True)
    ns = load_a6(shared_config=original, local_default=tree["local_parent"])
    ns[HANDLER](True)
    assert ns["_SHARED_CACHE_CONFIG"] == original, "the handler wrote to the startup snapshot"


# ===========================================================================
# GROUP G — the GUI seam
# ===========================================================================

def test_g1_the_registration_is_exactly_one_input_and_exactly_one_output():
    kwargs = {kw.arg: ast.unparse(kw.value) for kw in _click(BUTTON).keywords}
    assert kwargs["fn"] == HANDLER
    assert kwargs["inputs"] == f"[{CHECKBOX}]", kwargs["inputs"]
    assert kwargs["outputs"] == f"[{REPORT_BOX}]", kwargs["outputs"]
    assert "cancels" not in kwargs
    assert "concurrency_id" not in kwargs, "a read-out joined a concurrency lane"
    assert kwargs.get("concurrency_limit") == "None", \
        "a full concurrency group leaves the event QUEUED on the pinned gradio (S2-A R3)"


def test_g2_the_inputs_list_matches_the_handler_parameter_order():
    """Gradio passes handler arguments POSITIONALLY."""
    kwargs = {kw.arg: kw.value for kw in _click(BUTTON).keywords}
    supplied = [ast.unparse(e) for e in kwargs["inputs"].elts]
    params = [a.arg for a in _func(HANDLER).args.args]
    assert len(supplied) == len(params), f"{supplied} vs {params}"
    assert supplied == [CHECKBOX]


def test_g3_the_report_box_is_read_only():
    rendered = ast.unparse(_assign(REPORT_BOX).value)
    assert rendered.startswith("gr.Textbox("), rendered
    assert "interactive=False" in rendered, f"the report box is editable: {rendered}"


def test_g4_the_accordion_starts_closed_and_sits_in_the_approved_window():
    """Design 9.2 Option A: inside Prepare & Verify, after the section tab opens and BEFORE the
    `LABEL_PREP_SECTION` accordion, so no pinned span changes."""
    source = _source(_GUI)
    tab = source.index("with gr.Tab(LABEL_SECTION_PREPARE_VERIFY")
    prep = source.index("gr.Accordion(label=LABEL_PREP_SECTION")
    panel = source.index("LABEL_LIBRARY_CACHE_REPORT, open=False")
    assert tab < panel < prep, "the A6 accordion is outside the approved placement window"
    assert "open=False" in source[panel - 80:panel + 80]


def test_g5_the_wiring_sits_outside_both_pinned_spans():
    """Design 9.2: after `sample_btn.click(` (the sample-policy guard's end) and before
    `clips_check_btn.click(` (the start of the span pinned at exactly 6 `.click(` calls).

    Measured on the real call nodes' line numbers, NOT on `source.index`. Both of those tokens
    also occur inside explanatory comments — including the comment this very change added, which
    names `clips_check_btn.click(` while explaining the boundary — so a text search finds the
    prose before the code. That is the "naming a ban trips it" trap, and it has bitten this
    repository repeatedly.
    """
    sample = _click("sample_btn").lineno
    clips = _click("clips_check_btn").lineno
    wiring = _click(BUTTON).lineno
    assert sample < wiring < clips, (
        f"A6 wiring at line {wiring} is outside the approved window "
        f"({sample} < wiring < {clips})")


def test_g6_the_panel_writes_no_existing_widget_and_joins_no_gate_collection():
    source = _source(_GUI)
    for collection in ("source_outputs", "prep_outputs"):
        block = source[source.index(f"{collection} = ["):]
        block = block[:block.index("]")]
        for widget in (BUTTON, CHECKBOX, REPORT_BOX):
            assert widget not in block, f"{collection} names {widget}"
    # Compared ELEMENT-WISE, not by substring. `library_cache_report_box` CONTAINS the substring
    # `cache_report`, so a `forbidden not in outputs_text` check fails against a correct
    # implementation -- measured. A constant containing another is exactly the trap that has made
    # a `not in` guard unsatisfiable in this repository before.
    written = {ast.unparse(e)
               for e in {kw.arg: kw.value for kw in _click(BUTTON).keywords}["outputs"].elts}
    assert written == {REPORT_BOX}, written
    for forbidden in ("status_output", "video_output", "process_btn", "source_state",
                      "prep_state", "session_state", "render_invocation_state", "variation_seed",
                      "creative_preset", "cut_report", "sample_report", "variant_report",
                      "director_status", "director_proposal", "music_under_voice", "sfx_amount",
                      "sfx_level", "prep_status", "shared_cache_folder", "cache_report"):
        assert forbidden not in written, f"the A6 panel writes {forbidden}"


def test_g7_the_panel_is_not_a_render():
    code = _code(_func(HANDLER)) + _code(_func(LINES_FN))
    for forbidden in ("_RENDER_LOCK", "RENDER_CONCURRENCY_ID", "RenderLifecycle",
                      "_active_render_slot", "gr.State", "process_video", "process_video_guarded",
                      "analyze_beats_auto", "create_music_video", "get_processing_dir",
                      "_promote_output_no_replace", "live_declaration", "resolve_for_render"):
        assert forbidden not in code, f"{forbidden} reachable from the A6 handler"
    source = _source(_GUI)
    block = source[source.index(f"{BUTTON}.click("):]
    block = block[:block.index(")\n") + 2]
    assert "gr.State" not in block


def test_g8_the_panel_adds_no_subprocess_and_no_javascript_installer():
    tree_ = _tree()
    runs = [n for n in ast.walk(tree_)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "subprocess.run"]
    assert len(runs) == 2, (
        f"gui.py has {len(runs)} subprocess.run sites; the audited count is 2 and A6 adds none")
    loads = [n for n in ast.walk(tree_)
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "app.load"]
    js = [n for n in loads if any(kw.arg in {"js", "_js"} for kw in n.keywords)]
    assert len(js) == 3, (
        f"{len(js)} JavaScript app.load registrations; the allowlist holds 3 and A6 adds none")
    code = _code(_func(HANDLER)) + _code(_func(LINES_FN))
    for forbidden in ("subprocess", "os.startfile", "Popen", "requests", "urllib"):
        assert forbidden not in code, f"{forbidden} reachable from the A6 handler"


def _panel_with() -> ast.With:
    """The A6 accordion's own `with` node.

    Containment is read off this subtree rather than off a text span, following
    `test_the_sample_controls_sit_inside_the_preparation_accordion`'s precedent. A text span from
    this accordion to `LABEL_PREP_SECTION` would also swallow the NEIGHBOUR's 22-line comment —
    which legitimately mentions `gr.Slider` while explaining why one may not sit there — so a span
    ban would be measuring the wrong block's prose.
    """
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            rendered = ast.unparse(item.context_expr)
            if rendered.startswith("gr.Accordion(") and "LABEL_LIBRARY_CACHE_REPORT" in rendered:
                return node
    raise AssertionError("the A6 accordion was not found in gui.py")


def test_g9_the_three_new_widgets_are_the_whole_panel():
    """One accordion, one checkbox, one button, one textbox — and no rejected control.

    `gr.Number` with a `maximum=` and `gr.Radio` are named here because a future re-order could
    slide this accordion inside the `LABEL_PREP_SECTION` span, where both are banned. Expressing a
    cap as a constant rather than a widget bound is what keeps that legal.
    """
    panel = _panel_with()
    calls = [ast.unparse(n.func) for n in ast.walk(panel) if isinstance(n, ast.Call)]
    assert calls.count("gr.Checkbox") == 1, calls
    assert calls.count("gr.Button") == 1, calls
    assert calls.count("gr.Textbox") == 1, calls
    assert calls.count("gr.Accordion") == 1, calls
    for forbidden in ("gr.Radio", "gr.Slider", "gr.Number", "gr.DownloadButton", "gr.File",
                      "gr.UploadButton", "gr.Gallery", "gr.Dropdown", "gr.State"):
        assert forbidden not in calls, f"{forbidden} appeared inside the A6 panel"
    rendered = ast.unparse(panel)
    for forbidden in ("maximum=", "file_count=", "interactive=True"):
        assert forbidden not in rendered, f"{forbidden} appeared inside the A6 panel"
    # there is nothing to delete, clean, migrate or repair, so no such control exists
    for word in ("delete", "clean", "migrate", "rename", "repair", "clear", "sweep"):
        assert word not in rendered.lower(), f"the A6 panel names a mutating action: {word}"


def test_g10_the_panel_declares_exactly_the_four_widgets_and_no_state():
    """The assignment targets inside the panel are exactly the three named widgets."""
    panel = _panel_with()
    assigned = {t.id for n in ast.walk(panel) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert assigned == {CHECKBOX, BUTTON, REPORT_BOX}, assigned


#: Tokens that other suites slice RAW SOURCE on with `str.index`, or ban inside such a slice.
#: Naming any of them in A6's own comments relocates somebody else's span boundary onto this
#: block. Measured, not hypothetical: the first draft of the A6 comments spelled four of these
#: out and broke three §9.1 structural tests that merged main passes —
#: `test_i_preparation_offers_no_browser_upload_mode`,
#: `test_k_the_batch_widget_exists_in_preparation_with_the_constant_default` and
#: `test_the_clips_tab_writes_no_render_or_source_widget`.
_SPAN_POISON = (
    "LABEL_PREP_SECTION",          # span START for two preparation tests
    "prep_status = gr.Markdown",   # span END for the same two
    "clips_check_btn.click(",      # span START for the Video Review isolation test
    "LABEL_SECTION_VIDEO_REVIEW",  # span START for the Video Review widget test
    "gr.Radio",                    # banned inside the preparation span
    "maximum=",                    # banned inside the preparation span
    "gr.Slider",                   # banned inside the Video Review span
    "status_output",               # banned inside the Video Review isolation span
    "source_outputs", "prep_outputs", "session_state", "cut_run_btn", "cut_report",
    "render_invocation_state", "variation_seed", "creative_preset", "_RENDER_LOCK",
    "RENDER_CONCURRENCY_ID", "video_output", "process_btn", "source_state",
)


def _a6_regions(source: str) -> list[str]:
    """The two raw-source regions this change introduced into `gui.py`: the panel markup with its
    leading comment, and the event registration with its leading comment."""
    panel = _panel_with()
    lines = source.splitlines(keepends=True)

    def with_leading_comments(first_lineno: int, last_lineno: int) -> str:
        start = first_lineno - 1
        while start > 0 and lines[start - 1].strip().startswith("#"):
            start -= 1
        return "".join(lines[start:last_lineno])

    wiring = _click(BUTTON)
    return [with_leading_comments(panel.lineno, panel.end_lineno),
            with_leading_comments(wiring.lineno, wiring.end_lineno)]


def test_g11_the_a6_comments_name_no_span_boundary_or_banned_token():
    """A guard on this change's own prose, because prose is executable here.

    Three §9.1 structural tests slice raw `gui.py` with `str.index`, so a comment that *mentions*
    a boundary token moves that boundary. This test fails at the point of writing instead of
    surfacing as somebody else's unrelated red test.
    """
    source = _source(_GUI)
    for region in _a6_regions(source):
        for token in _SPAN_POISON:
            assert token not in region, (
                f"the A6 region names {token!r}, which another suite slices raw source on or "
                "bans inside such a slice. Describe it; do not spell it.")


def test_g12_the_span_poison_guard_is_not_vacuous():
    """Negative control: the guard must actually reject a planted token."""
    planted = "# this comment mentions LABEL_PREP_SECTION and maximum= deliberately\n"
    hits = [t for t in _SPAN_POISON if t in planted]
    assert len(hits) == 2, hits


# ===========================================================================
# GROUP H — matrix 15: the whole pass modifies nothing
# ===========================================================================

@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("case", ["present", "absent_live_parent", "absent_dead_parent", "off"])
def test_h1_matrix_15_no_filesystem_modification_under_any_read_only_path(tree, probe, case):
    """Design acceptance row 15, run over the fixture tree in every reachable combination:
    before/after `(relpath, size, mtime_ns)` manifest plus a recursive directory-set comparison."""
    folder = tree["shared"] if case in {"present", "off"} else tree[case]
    enabled = case != "off"
    before = manifest(tree["root"])
    dirs_before = {d for d, m in before.items() if m[0] == "dir"}

    ns = load_a6(shared_config=cfg(folder, enabled), local_default=tree["local_parent"])
    text = ns[HANDLER](probe)
    assert text.strip()

    after = manifest(tree["root"])
    assert after == before, f"{case}/probe={probe} modified the tree"
    assert {d for d, m in after.items() if m[0] == "dir"} == dirs_before
    assert not os.path.isdir(tree["absent_live_parent"])
    assert not os.path.isdir(tree["absent_dead_parent"])


def test_h2_the_manifest_instrument_detects_a_real_modification(tree):
    """Negative control for H1: a manifest comparison that could not fail would prove nothing."""
    before = manifest(tree["root"])
    os.makedirs(tree["absent_live_parent"])
    try:
        assert manifest(tree["root"]) != before, "the manifest missed a created directory"
    finally:
        os.rmdir(tree["absent_live_parent"])
    assert manifest(tree["root"]) == before

    before = manifest(tree["root"])
    extra = os.path.join(tree["shared"], "planted.json")
    with open(extra, "w", encoding="utf-8") as fh:
        fh.write("{}")
    try:
        assert manifest(tree["root"]) != before, "the manifest missed a created file"
    finally:
        os.remove(extra)
