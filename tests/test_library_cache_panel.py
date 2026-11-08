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
import functools
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
UNINSPECTED_FN = "_a6_uninspected_lines"
PATH_OK_FN = "_a6_configured_path_is_unambiguous"
SUPERSEDED_FN = "_a6_startup_snapshot_superseded"
MARK_FN = "_a6_mark_startup_snapshot_superseded"
SAVE_HANDLER = "_on_save_shared_cache"
SUPERSEDED_FLAG = "_A6_SNAPSHOT_SUPERSEDED"
BUTTON = "library_cache_report_btn"
CHECKBOX = "library_cache_probe_shared"
REPORT_BOX = "library_cache_report_box"

#: **The whole of the A6 call graph in `gui.py`**, in one place, because the structural bans below
#: are only as wide as this tuple. Several tests (`test_b5`, `test_e2`, `test_f2`, `test_g7`,
#: `test_g8`, `test_k4`) join these bodies and assert over the result; before this was a single
#: tuple each of them carried its own hand-written list, so a function added to the panel escaped
#: every one of them silently. `test_k5` pins the tuple itself against `gui.py`, so a new `_a6_*`
#: function cannot be added to the panel without either entering these bans or failing that test.
A6_FUNCTIONS = (HANDLER, LINES_FN, STATE_FN, SNAPSHOT_FN, UNINSPECTED_FN, PATH_OK_FN,
                SUPERSEDED_FN)

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
    # R-A / R-B and the honest-timestamp wording
    "A6_WHEN_UNAVAILABLE",
    "A6_NO_TIMESTAMP_LINE",
    "A6_NAME_ONLY_SENTENCE_CONTEXT",
    "A6_OPT_IN_HEADLINE_SHARED_NOT_OBSERVED",
    "A6_AMBIGUOUS_PATH_WARNING",
    "A6_SUPERSEDED_WARNING",
    "A6_REFUSED_NO_CONTACT_NOTE",
    "A6_REFUSED_CONTEXT_LINE",
    "A6_SNAPSHOT_SCOPE_NOTE",
    # the two tables the strengthened R-A gate reads (F1): the Windows device names a full-path
    # resolution can redirect, and the characters a path component cannot hold
    "A6_RESERVED_DEVICE_NAMES",
    "A6_FORBIDDEN_COMPONENT_CHARS",
)

#: Module-level A6 state, kept separate from the constants above because it is NOT a constant: it
#: is the one-way "the startup snapshot is stale" latch R-B installs. Seeded into the synthesised
#: namespace so the real `global` rebinding in `_a6_mark_startup_snapshot_superseded` has somewhere
#: to land, and so each `load_a6` starts from the shipped initial value rather than a value some
#: earlier test left behind.
A6_MODULE_STATE = (SUPERSEDED_FLAG,)


# ---------------------------------------------------------------------------
# AST plumbing (the conventions of tests/test_open_output_folder.py)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@functools.lru_cache(maxsize=8)
def _tree(path: str = _GUI) -> ast.Module:
    """`gui.py`'s parse tree, CACHED FOR THE PROCESS.

    `gui.py` is ~14,000 lines, `load_a6` resolves eight functions through `_func`, and this suite
    calls `load_a6` a few hundred times — so without the cache every load re-parsed the whole file
    eight times and the suite took 5m19s. It is safe to cache because nothing in this process ever
    rewrites `gui.py`: the mutation harness mutates the file BETWEEN pytest processes, so each
    process still sees exactly one version of the source. Tests that need the raw text read it
    through `_source`, which is cached from the same single read.
    """
    return ast.parse(_source(path), filename=path)


@functools.lru_cache(maxsize=64)
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
            cache_location=None, cache_report=None, analysis_cache=None,
            mutate_gate=False, mutate_path_gate=False,
            mutate_path_gate_pre_canonicalisation=False) -> dict:
    """Execute the real A6 constants and functions against a controlled namespace."""
    fs = fs if fs is not None else _FsSpy()
    namespace: dict = {
        "os": fs,
        "datetime": __import__("datetime"),
        "get_input_dir": (lambda: local_default),
        "_SHARED_CACHE_CONFIG": {} if shared_config is None else dict(shared_config),
        "fork_analysis_cache": analysis_cache if analysis_cache is not None else ac,
        "fork_cache_location": cache_location if cache_location is not None else a4,
        "fork_cache_report": cache_report if cache_report is not None else a5,
        "open": open_fn if open_fn is not None else _OpenSpy(),
        "__builtins__": builtins,
    }

    tree = _tree()
    wanted = set(A6_CONSTANTS) | set(A6_MODULE_STATE)
    body: list = [n for n in tree.body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and getattr(n.targets[0], "id", None) in wanted]
    missing = wanted - {n.targets[0].id for n in body}
    assert not missing, f"A6 constants absent from gui.py: {sorted(missing)}"

    # MARK_FN is loaded even though no report path calls it: the R-B tests drive the latch through
    # the REAL marker rather than by writing the flag themselves, so what they exercise is the
    # shipped transition and not a test-local imitation of it.
    for name in (PATH_OK_FN, SUPERSEDED_FN, MARK_FN, SNAPSHOT_FN, STATE_FN, UNINSPECTED_FN,
                 LINES_FN, HANDLER):
        node = _func(name)
        if mutate_gate and name == LINES_FN:
            node = _mutate_gate_closed(node)
        if mutate_path_gate and name == PATH_OK_FN:
            node = _mutate_path_gate_open(node)
        if mutate_path_gate_pre_canonicalisation and name == PATH_OK_FN:
            node = _mutate_path_gate_pre_canonicalisation(node)
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


def _mutate_path_gate_open(node: ast.FunctionDef) -> ast.FunctionDef:
    """The R-A NEGATIVE CONTROL: make the ambiguity test accept everything.

    `_a6_configured_path_is_unambiguous` is replaced by `return True`, so an ambiguous configured
    path reaches A4 exactly as it did before the fix. Group I's zero-contact assertions must then
    go red; if they do not, they are not measuring the refusal.

    The replacement is verified to differ from the original, because a control that silently failed
    to rewrite anything would make Group I look verified when it is not.
    """
    original = ast.unparse(node)
    clone = ast.parse(ast.unparse(node)).body[0]
    clone.body = [ast.Return(value=ast.Constant(True))]
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != original, "the R-A control did not change the gate"
    return mutated


def _mutate_path_gate_pre_canonicalisation(node: ast.FunctionDef) -> ast.FunctionDef:
    """The F1 NEGATIVE CONTROL: truncate the gate back to the state the review measured.

    The strengthened gate is the original text tests followed by an enumeration of the Win32
    canonicalisations `os.path.normpath` does not perform. This control cuts everything after the
    `normpath` check and returns `True` there, which is *exactly* the shipped gate of PR #94 head
    `add507e2` — so the F1 tests reproduce the seven measured counterexamples rather than merely
    asserting that today's gate refuses some strings.

    Anchored on the `normpath` test and the anchor count is asserted: a control that silently
    located nothing would make the F1 group look verified when it is not. It also asserts there
    IS something after the anchor, so the control cannot pass by cutting an empty tail.
    """
    clone = ast.parse(ast.unparse(node)).body[0]
    anchors = [i for i, stmt in enumerate(clone.body)
               if isinstance(stmt, ast.If) and "normpath" in ast.unparse(stmt.test)]
    assert len(anchors) == 1, (
        f"expected exactly one normpath test in {node.name}; found {len(anchors)}. The F1 control "
        "cannot be trusted against an anchor it did not locate.")
    cut = anchors[0] + 1
    assert cut < len(clone.body), (
        "there is nothing after the normpath check to remove, so this control would be a no-op "
        "and the F1 assertions would prove nothing")
    clone.body = clone.body[:cut] + [ast.Return(value=ast.Constant(True))]
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the F1 control did not change the gate"
    return mutated


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
    rendered = shared_lines(text, ns)
    # The guard is load-bearing. Without it this loop iterates zero times when the shared-state
    # line is absent, so DELETING that line left the whole suite green -- measured. `test_c6` and
    # `test_c7` carried this guard already; `test_a7` and `test_d1` did not.
    assert rendered, "the default read-out is silent about the shared folder"
    for line in rendered:
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


def _a6_bodies(names=A6_FUNCTIONS) -> str:
    """The executable bodies of the whole A6 call graph, docstrings removed."""
    return "\n".join(_code(_func(n)) for n in names)


def _a6_call_names(names=A6_FUNCTIONS) -> set:
    """The RESOLVED final name of every call site in the A6 call graph.

    `ast.unparse(call.func).rsplit('.', 1)[-1]` — so `fork_analysis_cache.measure(...)`,
    `ac.measure(...)` and a bare `measure(...)` all reduce to `measure`, whatever the alias. That
    is the correction R-C item 4 forces: a ban list that compares whole dotted strings only bans
    the spellings somebody happened to think of.
    """
    found = set()
    for node in ast.walk(ast.parse(_a6_bodies(names))):
        if isinstance(node, ast.Call):
            found.add(ast.unparse(node.func).rsplit(".", 1)[-1])
    return found


#: Every call the A6 call graph is PERMITTED to make, by resolved final name. An allowlist, and the
#: direction is the whole point.
#:
#: `test_b5` used to be a ban list of exact `ast.unparse` strings, and seven of its eighteen
#: entries were unsatisfiable: it banned the bare names `measure` and `read_records` and the alias
#: `analysis_cache.measure`, while `gui.py` spells the module `fork_analysis_cache`. Set membership
#: is equality, not substring, so adding `fork_analysis_cache.measure(configured_folder)` — a
#: recursive walk and a byte total, the exact pair R-3 forbids — to the opt-in handler left all 65
#: tests green. Measured, not hypothetical.
#:
#: Every name here is either an A6 helper, the single A4 observer, an A5 renderer entry point, the
#: one shared settings parser, or a pure text / list operation that cannot reach a disk.
_ALLOWED_CALL_NAMES = frozenset({
    # A6's own helpers
    "_a6_state_sentence", "_a6_shared_cache_startup_snapshot", "_a6_cache_report_lines",
    "_a6_uninspected_lines", "_a6_configured_path_is_unambiguous",
    "_a6_startup_snapshot_superseded",
    # A4 — the one observer. There is no second route from this panel to a filesystem.
    "inspect_location",
    # A5 — the renderer, reached by identity
    "report_lines", "estimate_reclaimable", "bind_observation",
    # the ONE settings parser, shared with A4 rather than reimplemented
    "_clean", "_as_bool",
    # pure text, pure string and list building; none of these reaches a disk
    "join", "splitdrive", "normpath", "get_input_dir", "isinstance", "bool",
    "append", "extend", "strip", "startswith", "format", "get",
    # F1: the strengthened R-A gate splits the path into components and upper-cases a stem to
    # compare it against the device-name table. Both are pure string operations.
    "split", "upper",
    # the injected observation clock
    "now", "astimezone", "isoformat",
})

#: Names that must never be admitted to the allowlist above, so that a future red test cannot be
#: "fixed" by widening the instrument. Record readers, measurers, writers, recursive walkers,
#: live-configuration readers, and the three text expansions whose absence is what makes the R-A
#: refusal sound (`expandvars` reads the environment; `expanduser` and `abspath` resolve against
#: process state) — plus `getcwd`, which is the working directory this panel must never consult.
_NEVER_ALLOWED = (
    "measure", "read_records", "read_record", "is_record_name", "inspect_library",
    "scan_library_folder", "inventory_root", "import_records", "estimate_reclaimable_records",
    "walk", "scandir", "listdir", "lstat", "stat", "makedirs", "mkdir", "remove", "rmdir",
    "unlink", "rename", "replace", "copy", "copytree", "open",
    "load_config", "save_config", "config_path", "default_config_path", "resolve_location",
    "resolve_cache_location", "probe_root", "expandvars", "expanduser", "abspath", "realpath",
    "getcwd", "isdir", "isfile", "exists", "getsize",
)


def test_b5_the_a6_call_graph_makes_only_allowlisted_calls():
    """RESOLVED call-target analysis, replacing a ban list that was provably unsatisfiable.

    A new call in the A6 panel fails this test until it is deliberately admitted, which is the
    opposite failure mode from a ban list: the allowlist's blind spot is a false RED, the ban
    list's was a false GREEN.
    """
    for forbidden in _NEVER_ALLOWED:
        assert forbidden not in _ALLOWED_CALL_NAMES, (
            f"{forbidden!r} was admitted to the allowlist. Widening the instrument is not a way "
            "to make this test pass.")

    used = _a6_call_names()
    assert used, "no call site was found at all; this test would then prove nothing"
    unexpected = used - _ALLOWED_CALL_NAMES
    assert not unexpected, (
        f"the A6 call graph calls {sorted(unexpected)}, which is not on the allowlist. If the "
        "call is legitimate, add it deliberately — and it may not be one of _NEVER_ALLOWED.")


def test_b5b_no_record_reader_or_measurer_is_named_anywhere_in_the_a6_bodies():
    """The secondary check, kept and repaired: the bodies name no such symbol under ANY alias.

    Aliases are resolved the same way as in `test_b5`, so `fork_analysis_cache.measure`,
    `fork_cache.measure` and a bare `measure` are all caught. This is belt to `test_b5`'s braces —
    it catches a forbidden symbol that is *referenced* without being called, for instance passed
    somewhere as a value.
    """
    referenced = {ast.unparse(n).rsplit(".", 1)[-1]
                  for n in ast.walk(ast.parse(_a6_bodies()))
                  if isinstance(n, (ast.Name, ast.Attribute))}
    for forbidden in ("read_records", "measure", "is_record_name", "read_record",
                      "inspect_library", "scan_library_folder", "inventory_root",
                      "estimate_reclaimable_records", "walk", "scandir", "makedirs", "remove",
                      "rmdir", "rename", "shutil", "expandvars", "expanduser", "abspath",
                      "getcwd"):
        assert forbidden not in referenced, (
            f"{forbidden} is referenced from the A6 call graph under some alias")


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
    rendered = shared_lines(text, ns)
    assert rendered, f"probe={probe}: no shared-folder line to examine, so this proves nothing"
    for line in rendered:
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
    bodies = _a6_bodies()
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
    bodies = _a6_bodies()
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


def _accordion_open_keyword() -> ast.keyword:
    """The A6 accordion's `open=` keyword NODE, located structurally."""
    call = _panel_with().items[0].context_expr
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "gr.Accordion", \
        ast.unparse(call)
    for kw in call.keywords:
        if kw.arg == "open":
            return kw
    raise AssertionError("the A6 accordion declares no open= keyword at all")


def _line_of(source: str, needle: str) -> int:
    return source[:source.index(needle)].count("\n") + 1


def test_g4_the_accordion_starts_closed_and_sits_in_the_approved_window():
    """Design 9.2 Option A: inside Prepare & Verify, after the section tab opens and BEFORE the
    `LABEL_PREP_SECTION` accordion, so no pinned span changes.

    The `open=` half is read off the PARSED keyword value. It used to be

        panel = source.index("LABEL_LIBRARY_CACHE_REPORT, open=False")
        assert "open=False" in source[panel - 80:panel + 80]

    which is a **tautology**: the index was derived from a needle that already contains the
    asserted substring, so the window always contains it and the assertion could not fail.
    Measured, not argued — rewriting the keyword as `open=False if False else True` ships an
    accordion that opens by default and left all 65 tests green, because the source still contains
    the characters `open=False`.

    Both halves of the placement check now use LINE numbers rather than character offsets mixed
    with a parsed node, so the two measurements are comparable.
    """
    source = _source(_GUI)
    tab = _line_of(source, "with gr.Tab(LABEL_SECTION_PREPARE_VERIFY")
    prep = _line_of(source, "gr.Accordion(label=LABEL_PREP_SECTION")
    panel = _panel_with().lineno
    assert tab < panel < prep, (
        f"the A6 accordion (line {panel}) is outside the approved placement window "
        f"({tab} < panel < {prep})")

    kw = _accordion_open_keyword()
    assert isinstance(kw.value, ast.Constant), (
        f"the accordion's open= is the expression {ast.unparse(kw.value)!r}, not a literal. An "
        "evaluated expression cannot be read off the source at all, and a conditional here is "
        "exactly how an accordion ships open while the file still reads 'open=False'.")
    assert kw.value.value is False, \
        f"the A6 accordion does not start closed: open={kw.value.value!r}"


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
    code = _a6_bodies()
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
    code = _a6_bodies()
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


# ===========================================================================
# GROUP I — R-A: the folder NAMED is the folder PROBED, or nothing is probed
# ===========================================================================
#
# The defect: this panel displays the configured folder as `analysis_cache._clean` leaves it
# (stripped, nothing more), while A4's probe sends it through A1's `normalize_root_text` — which
# strips quotation marks and expands `~` and environment references — and then through
# `os.path.abspath`, which resolves a relative path against the PROCESS WORKING DIRECTORY. So a
# stored `%SOMETHING%` was displayed verbatim, reported `present and inspectable`, and a different
# directory was the one enumerated; and a stored relative path made the application's own working
# directory the subject of the observation.

#: `(text, is_unambiguous)`. The second column is PROVED against the real A1 + `abspath` pair by
#: `test_i1` rather than asserted by hand, so this table cannot drift away from what A4 probes.
_PATH_CASES = (
    (r"C:\SharedCache\VideoAnalysis", True),
    (r"C:\Cache9\video_analysis_cache2", True),          # digits are legitimate in a path
    (r"\\nas\beatsync\cache", True),                     # UNC
    (r"c:\lowercase\drive", True),                       # a mixed-case drive is stable
    (r"C:\PROGRA~1\BeatSync", True),                     # a tilde INSIDE a component is ordinary
    (r"C:/forward/slashes", False),
    ("C:\\trailing\\\\", False),
    (r"C:\dot\.\here", False),
    (r"C:\up\..\there", False),
    (r"%BEATSYNC_R14A_NOT_A_REAL_VARIABLE%\sub", False),  # unset is the WORST case: see test_i1
    (r"%TEMP%\BeatSyncShared", False),
    (r"$BEATSYNC_R14A_NOT_A_REAL_VARIABLE", False),
    (r"~\BeatSyncCache", False),
    ("~", False),
    (r"relative\cache", False),
    ("cache", False),
    ("C:relative", False),
    (r"\driverelative\cache", False),
    ('"C:\\Quoted\\Path"', False),
    (None, False),
    (123, False),
    ("", False),
    # --- F1: the seven counterexamples the independent review measured, each of which the
    #     pre-R3 gate ACCEPTED while the resolver moved it. `test_i10` proves separately that
    #     each one really does move, so none of these rows is an invented refusal.
    (r"C:\cache.", False),                       # a trailing dot is stripped per component
    (r"C:\cache...", False),
    (r"C:\a.\b", False),                         # ... including an intermediate component
    (r"C:\a\b.", False),
    ("\\\\server\\share\\a.", False),            # ... and across a UNC root
    (r"C:\NUL", False),                          # redirected into the device namespace
    ("\\" * 4, False),                           # a separator run normpath leaves alone
    # --- F1: the same canonicalisations in the shapes the seven did not cover
    (r"C:\a \b", False),                         # a trailing SPACE is stripped too
    (r"C:\a. \b", False),                        # ... and a dot-space pair, in either order
    ("\\\\server.\\share\\a", False),            # the UNC server name canonicalises as well
    ("\\\\server\\share.\\a", False),
    (r"C:\nul.txt", False),                      # a device name keeps its meaning with a suffix
    (r"C:\Nul", False),                          # ... and is case-insensitive
    (r"C:\a\CON", False),                        # ... in any component
    (r"C:\cache\COM1", False),
    (r"C:\NUL .txt", False),                     # ... and the stem is compared with its trailing
                                                 #     space stripped, because whether THAT
                                                 #     spelling redirects is a property of the
                                                 #     platform call, not of this application
    ("C:\\a\x00b", False),                       # control characters, which no real path holds
    ("C:\\a\x01b", False),
    ("C:\\a\tb", False),                         # interior whitespace `_clean` does not strip
    (r"C:\a|b", False),                          # characters a component cannot hold
    (r"C:\a<b", False),
    # --- F1: and the shapes that must SURVIVE the new rules, so this is a tightening and not a
    #     refusal of everything. `test_i12` asserts these are still inspected end to end.
    (r"C:\NULL", True),                           # a device name is a component, not a prefix
    (r"C:\COM10", True),
    (r"C:\a.b\c.d", True),                        # dots INSIDE a component are ordinary
    (r"C:\a b\c d", True),                        # so are spaces
    ("C:\\日本語\\кэш", True),                     # and non-ASCII
    ("C:\\", True),                               # the bare drive root is stable
    (r"\\?\C:\cache", True),                      # an extended-length prefix is left alone
)


def test_i1_every_accepted_path_provably_resolves_to_itself():
    """The soundness proof, and the reason this is a refusal rather than a second resolver.

    For every text the gate ACCEPTS, the folder A4 would actually reach —
    `abspath(normalize_root_text(_clean(text)))`, the exact composition A4's `_probe` performs —
    must be the identical string this panel displays. The gate is permitted to be stricter than
    necessary; it is not permitted to be looser, and this is the direction that matters.
    """
    ns = load_a6()
    gate = ns[PATH_OK_FN]
    accepted = [t for t, _ in _PATH_CASES if gate(t)]
    assert accepted, "the gate accepted nothing; this proof would then be vacuous"
    for text in accepted:
        probed = os.path.abspath(a1.normalize_root_text(ac._clean(text)))
        assert probed == text, (
            f"the gate accepted {text!r} but A4 would probe {probed!r}. A verdict would be "
            "attributed to the wrong folder.")


def test_i1b_an_unset_environment_reference_resolves_against_the_working_directory():
    """Why presence of `%` is refused rather than `expandvars(text) != text`.

    An UNSET reference survives expansion unchanged — so an equality test would call it stable —
    and `abspath` then resolves it against the process working directory. That is the worst of
    both outcomes, and it is the case a cleverer gate would have missed.
    """
    text = r"%BEATSYNC_R14A_NOT_A_REAL_VARIABLE%\sub"
    assert text not in os.environ, "this test needs a genuinely unset variable"
    assert os.path.expandvars(text) == text, "the premise changed; re-measure this"
    probed = os.path.abspath(a1.normalize_root_text(ac._clean(text)))
    assert probed != text, "the premise changed: the reference no longer moves under abspath"
    assert os.path.isabs(probed)
    assert os.path.normcase(probed).startswith(os.path.normcase(os.getcwd())), (
        f"{text!r} was expected to resolve against the working directory; it resolved to "
        f"{probed!r}")
    ns = load_a6()
    assert ns[PATH_OK_FN](text) is False, "an unset environment reference was accepted"


@pytest.mark.parametrize("text,expected", _PATH_CASES)
def test_i2_the_gate_classifies_each_case_as_the_table_says(text, expected):
    ns = load_a6()
    assert ns[PATH_OK_FN](text) is expected, f"{text!r} was classified wrongly"


def test_i3_an_ambiguous_path_is_refused_with_no_contact_at_either_seam(tree):
    """The headline R-A property: refused means refused — A4 is never entered, and neither seam
    records a single filesystem call."""
    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(r"%BEATSYNC_R14A_NOT_A_REAL_VARIABLE%\sub", True),
                     local_default=tree["local_parent"], cache_location=spy)
        text = ns[HANDLER](True)

    assert spy.count("inspect_location") == 0, "an ambiguous path reached A4"
    assert a4fs.calls == [], f"an ambiguous path reached a disk through A4: {a4fs.calls}"
    assert ns["_fs"].calls == [], f"the refusal touched the filesystem: {ns['_fs'].calls}"
    assert ns["_open"].calls == []
    assert ns["A6_AMBIGUOUS_PATH_WARNING"] in text, "the refusal is not explained"
    assert ns["A6_REFUSED_NO_CONTACT_NOTE"] in text

    lines = shared_lines(text, ns)
    assert lines, "the refusal says nothing about the shared folder"
    assert reported_verdicts(lines, ns) == {a1.NOT_INSPECTED}, reported_verdicts(lines, ns)
    assert a1.PRESENT not in reported_verdicts(lines, ns)


@pytest.mark.parametrize("text", [t for t, ok in _PATH_CASES if not ok and isinstance(t, str) and t])
def test_i4_no_ambiguous_path_ever_earns_a_positive_verdict(tree, text):
    """Every refused shape, not just the convenient one: no `present` anywhere in the read-out,
    in either mode, and no filesystem contact."""
    for probe in (False, True):
        with watch_a4_filesystem() as a4fs:
            ns = load_a6(shared_config=cfg(text, True), local_default=tree["local_parent"])
            rendered = ns[HANDLER](probe)
        verdicts = reported_verdicts(shared_lines(rendered, ns), ns)
        assert verdicts == {a1.NOT_INSPECTED}, f"{text!r}/probe={probe}: {verdicts}"
        assert a4fs.calls == [], f"{text!r}/probe={probe} reached a disk: {a4fs.calls}"


def test_i5_the_working_directory_is_never_probed(tree):
    """A relative configured path used to make the application's own working directory the subject
    of the observation. Measured against the real CWD, both seams, both modes."""
    cwd = os.path.normcase(os.path.abspath(os.getcwd()))
    for text in (r"relative\cache", "cache", r"\driverelative\cache", "."):
        for probe in (False, True):
            with watch_a4_filesystem() as a4fs:
                ns = load_a6(shared_config=cfg(text, True), local_default=tree["local_parent"])
                ns[HANDLER](probe)
            for name, path in a4fs.calls:
                assert not path.startswith(cwd), \
                    f"{text!r}/probe={probe} probed the working directory via {name}: {path}"
            assert a4fs.calls == [], f"{text!r} reached a disk at all: {a4fs.calls}"


def test_i6_a_plain_absolute_path_is_still_inspected(tree):
    """The companion test_i3 needs. Without it, Group I would be equally satisfied by a gate that
    refuses everything — and a panel that never inspects anything is not the fix."""
    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                     cache_location=spy)
        text = ns[HANDLER](True)
    assert ns[PATH_OK_FN](tree["shared"]) is True, \
        f"the fixture's own folder is classified ambiguous: {tree['shared']!r}"
    assert spy.count("inspect_location") == 1, "a plain absolute path was refused"
    assert a4fs.touching(tree["shared"]), "a plain absolute path was never reached"
    assert ns["A6_AMBIGUOUS_PATH_WARNING"] not in text
    assert a1.PRESENT in reported_verdicts(shared_lines(text, ns), ns)


def test_i7_the_r_a_negative_control_proves_group_i_can_fail(tree):
    """Force the ambiguity gate to accept everything and show the refusal assertions go red."""
    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(r"%TEMP%\BeatSyncShared", True),
                     local_default=tree["local_parent"], cache_location=spy,
                     mutate_path_gate=True)
        text = ns[HANDLER](True)
    assert spy.count("inspect_location") == 1, \
        "the mutant did not reach A4, so test_i3's zero-call assertion proves nothing"
    assert a4fs.calls != [], \
        "the mutant reached no disk, so test_i3's filesystem watcher proves nothing"
    assert ns["A6_AMBIGUOUS_PATH_WARNING"] not in text, \
        "the mutant still refused, so the gate is not where this control thinks it is"


def test_i9_a_refusal_does_not_claim_it_named_the_folder_and_stopped(tree):
    """`A6_NAME_ONLY_NOTE` says this read-out named the configured folder and stopped there. That
    is true of the name-only mode and false of both refusals — under supersession nothing was
    named, and under an ambiguous path what was named is exactly what could not be trusted. The
    two contexts are therefore different constants, and this pins which appears where."""
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    plain = ns[HANDLER](False)
    assert ns["A6_NAME_ONLY_NOTE"] in plain
    assert ns["A6_REFUSED_CONTEXT_LINE"] not in plain

    refused = load_a6(shared_config=cfg(r"%TEMP%\Shared", True),
                      local_default=tree["local_parent"])
    text = refused[HANDLER](False)
    assert refused["A6_REFUSED_CONTEXT_LINE"] in text
    assert refused["A6_NAME_ONLY_NOTE"] not in text, \
        "a refusal claimed it had named the configured folder and stopped there"


def test_i8_the_gate_reads_no_environment_and_touches_no_disk(tree):
    """A purely syntactic test is the point: validating text with a probe would be contacting a
    folder in order to decide whether it may be contacted. Proved structurally, because an
    environment read leaves no filesystem trace for a spy to catch."""
    used = _a6_call_names((PATH_OK_FN,))
    for forbidden in ("expandvars", "expanduser", "abspath", "realpath", "getcwd", "isdir",
                      "exists", "listdir", "environ", "get"):
        assert forbidden not in used, f"the ambiguity gate calls {forbidden}"
    referenced = {ast.unparse(n) for n in ast.walk(ast.parse(_a6_bodies((PATH_OK_FN,))))
                  if isinstance(n, (ast.Name, ast.Attribute))}
    assert "os.environ" not in referenced, "the ambiguity gate reads the environment"


# ---------------------------------------------------------------------------
# F1 — the Win32 canonicalisations `normpath` does not perform
# ---------------------------------------------------------------------------
#
# `os.path.normpath` is pure string work. The resolver A4 uses finishes with `os.path.abspath`,
# which on Windows goes through `GetFullPathName` and ALSO strips trailing dots and spaces from
# every component, collapses a degenerate separator run, and redirects a reserved device name into
# the device namespace. A gate that models the second with the first therefore accepted text the
# resolver moved, and an independent review measured seven such texts out of 84 — identically on
# Python 3.13.12 and 3.14.3. `test_i1`'s table could not see any of them: it proves the property
# over the rows it contains, and none of those rows had a trailing dot, a device name or a
# degenerate separator run.

#: The seven measured counterexamples, as the review recorded them.
_CANONICALISATION_COUNTEREXAMPLES = (
    r"C:\cache.",
    r"C:\cache...",
    r"C:\a.\b",
    r"C:\a\b.",
    "\\\\server\\share\\a.",
    r"C:\NUL",
    "\\" * 4,
)

#: A wider corpus than the table, for the property test below. Deliberately includes shapes that
#: must be ACCEPTED as well as shapes that must be refused: a property test over refusals only is
#: satisfied by a gate that refuses everything.
_CANONICALISATION_CORPUS = _CANONICALISATION_COUNTEREXAMPLES + (
    # device names: every stem, every position, with and without a suffix, in mixed case
    r"C:\nul", r"C:\Nul", r"C:\NUL.txt", r"C:\nul.json", r"C:\CON.log", r"C:\aux",
    r"C:\PRN", r"C:\COM1", r"C:\com9", r"C:\LPT1", r"C:\lpt9.dat", "C:\\COM\xb9",
    r"C:\NUL\sub", r"C:\a\NUL\b", r"C:\a\CON", "C:\\CONIN$", "C:\\CONOUT$.x",
    r"C:\NUL .txt", r"C:\NUL.a.b",
    # NOT device names: the stem is a whole component, never a prefix or a substring
    r"C:\NULL", r"C:\CONFIG", r"C:\communications", r"C:\nulsoft\cache", r"C:\a\prnt",
    r"C:\COM10", r"C:\COM0", r"C:\LPT10",
    # trailing dot / space in every position, including the UNC head
    r"C:\a \b", r"C:\a .\b", r"C:\a. \b", r"C:\a\b. ",
    "\\\\server.\\share\\a", "\\\\server\\share.\\a", "\\\\server \\share\\a",
    "\\\\?\\C:\\a.", "\\\\?\\UNC\\server\\share\\a.",
    # degenerate separator runs
    "\\" * 3, "\\" * 5, "C:\\\\", "C:\\\\\\a",
    # control characters and characters a component cannot hold
    "C:\\a\x00b", "C:\\a\x01b", "C:\\a\x1fb", "C:\\a\tb",
    r"C:\a|b", r"C:\a<b", r"C:\a>b", r"C:\a?b", r"C:\a*b", 'C:\\a"b', r"C:\a:b",
    # legitimate and stable: these must survive
    r"C:\cache", r"C:\a.b\c.d", r"C:\a b\c d", "C:\\日本語\\кэш", "C:\\", "Z:\\",
    r"\\?\C:\cache", r"\\?\UNC\server\share\cache", r"\\nas\beatsync\cache",
    r"C:\PROGRA~1\BeatSync", r"C:\a-b_c.d\e (1)",
)

#: Shapes the strengthened gate must still ACCEPT, so F1 is a tightening and not a capitulation.
_STILL_ACCEPTED = (
    r"C:\cache", r"C:\BeatSync\video_analysis_cache", r"D:\a\b\c", r"C:\caché-ünïcode",
    r"C:\cache with spaces\deeper", r"\\server\share\cache", r"C:\~notleading", r"C:\a~b",
    "C:\\", "Z:\\", r"\\?\C:\cache", r"\\?\UNC\server\share\cache",
    r"C:\PROGRA~1\BeatSync", r"C:\NULL", r"C:\COM10", r"C:\a.b\c.d", r"C:\a b\c d",
    "C:\\日本語\\кэш",
)


class _TripwireOs:
    """An `os` whose ONLY permitted members are the two pure path helpers the gate may use.

    A structural ban (`test_i8`) proves the gate's source names nothing forbidden. This proves the
    stronger dynamic property over a whole corpus: every other `os` member is unreachable, so a
    future clause that reads the environment or resolves against the working directory raises here
    rather than passing quietly. An environment read leaves no filesystem trace for a spy, which
    is why this is a tripwire and not a recorder.
    """

    class _Path:
        def __init__(self, log):
            self._log = log

        def splitdrive(self, text):
            self._log.append("splitdrive")
            return os.path.splitdrive(text)

        def normpath(self, text):
            self._log.append("normpath")
            return os.path.normpath(text)

        def __getattr__(self, name):
            raise AssertionError(f"the textual gate reached os.path.{name}")

    def __init__(self):
        self.log: list[str] = []
        self.path = self._Path(self.log)

    def __getattr__(self, name):
        raise AssertionError(f"the textual gate reached os.{name}")


def _probed_target(text):
    """The folder A4 would actually reach for `text` — A4's own composition, nothing re-derived."""
    return os.path.abspath(a1.normalize_root_text(ac._clean(text)))


@pytest.mark.parametrize("text", _CANONICALISATION_COUNTEREXAMPLES)
def test_i10_every_measured_counterexample_really_moved_and_is_now_refused(tree, text):
    """Three assertions in one place, because each is worthless without the others.

    1. the resolver REALLY moves this text — so the row is a measured counterexample and not an
       invented refusal;
    2. the PRE-R3 gate accepted it — the control reproduces the defect on this machine rather than
       citing the review; and
    3. the shipped gate refuses it, and the refusal is a refusal: no A4 call, no filesystem
       contact at either seam, and `not_inspected` in the read-out.
    """
    probed = _probed_target(text)
    assert probed != text, (
        f"{text!r} no longer moves under the resolver on this platform; re-measure this row "
        "rather than trusting it")

    pre = load_a6(mutate_path_gate_pre_canonicalisation=True)
    assert pre[PATH_OK_FN](text) is True, (
        f"the pre-R3 control refused {text!r}, so it does not reproduce the gate the review "
        "measured and these assertions prove nothing about the fix")

    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(text, True), local_default=tree["local_parent"],
                     cache_location=spy)
        rendered = ns[HANDLER](True)
    assert ns[PATH_OK_FN](text) is False, f"{text!r} is still accepted"
    assert spy.count("inspect_location") == 0, f"{text!r} reached A4"
    assert a4fs.calls == [], f"{text!r} reached a disk: {a4fs.calls}"
    assert ns["_fs"].calls == [], f"{text!r} touched the filesystem: {ns['_fs'].calls}"
    assert reported_verdicts(shared_lines(rendered, ns), ns) == {a1.NOT_INSPECTED}
    assert ns["A6_AMBIGUOUS_PATH_WARNING"] in rendered, "the refusal is not explained"


def test_i11_no_text_the_gate_accepts_is_moved_by_the_resolver():
    """The property, over a corpus rather than over the table — which is the lesson of F1.

    `test_i1` proves `accepted => resolves to itself` for the rows of `_PATH_CASES`. That is a
    proof about a table, and a docstring that generalises it to every possible string is claiming
    more than the test delivers. This widens the evidence to the adversarial corpus above, and the
    accepted count is asserted so a gate that refused everything could not pass.
    """
    ns = load_a6()
    gate = ns[PATH_OK_FN]
    corpus = list(_CANONICALISATION_CORPUS) + [t for t, _ in _PATH_CASES]
    accepted = [t for t in corpus if isinstance(t, str) and gate(t)]
    assert len(accepted) >= len(_STILL_ACCEPTED), (
        f"only {len(accepted)} of {len(corpus)} texts were accepted; a gate that refuses "
        "everything satisfies no property worth having")
    for text in accepted:
        assert _probed_target(text) == text, (
            f"the gate accepted {text!r} but A4 would probe {_probed_target(text)!r}. A verdict "
            "would be attributed to the wrong folder.")


@pytest.mark.parametrize("text", _STILL_ACCEPTED)
def test_i12_the_tightening_refuses_no_ordinary_absolute_path(text):
    """The companion `test_i10` needs. The gate is permitted to be stricter than necessary, but a
    panel that refuses every real folder is not the fix either — so the ordinary drive, UNC,
    extended-length, unicode, spaced and dotted-component shapes are pinned as ACCEPTED."""
    ns = load_a6()
    assert ns[PATH_OK_FN](text) is True, f"an ordinary absolute path was refused: {text!r}"
    assert _probed_target(text) == text, f"the premise changed: {text!r} now moves"


def test_i13_a_real_folder_with_a_dot_and_a_space_in_its_name_is_still_inspected(tmp_path):
    """End to end, on a folder that actually exists: the new per-component rules reject a trailing
    dot or space, NOT a dot or a space inside a component. Measured through the real A4, because
    this is the case a careless implementation of F1 would break."""
    shared = tmp_path / "Shared cache v1.2"
    shared.mkdir()
    (shared / "a_1234567890abcdef.json").write_text('{"v": 1}', encoding="utf-8")
    local_parent = tmp_path / "input"
    (local_parent / "video_analysis_cache").mkdir(parents=True)

    spy = _ModuleSpy(a4, "inspect_location")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(str(shared), True), local_default=str(local_parent),
                     cache_location=spy)
        rendered = ns[HANDLER](True)
    assert ns[PATH_OK_FN](str(shared)) is True, f"a real folder was refused: {str(shared)!r}"
    assert spy.count("inspect_location") == 1
    assert a4fs.touching(str(shared)), "the folder was never reached"
    assert a1.PRESENT in reported_verdicts(shared_lines(rendered, ns), ns)
    assert ns["A6_AMBIGUOUS_PATH_WARNING"] not in rendered


def test_i14_the_gate_reaches_no_os_member_but_the_two_pure_path_helpers():
    """The dynamic half of `test_i8`, over the whole adversarial corpus.

    The gate is executed against an `os` that raises on every attribute except `path.splitdrive`
    and `path.normpath`, so an environment read or a working-directory resolution is structurally
    unreachable rather than merely unobserved. The members actually used are asserted exactly, so
    a new pure helper has to be admitted deliberately.
    """
    node = _func(PATH_OK_FN)
    tripwire = _TripwireOs()
    namespace = {"os": tripwire, "__builtins__": builtins}
    consts = [n for n in _tree().body
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and getattr(n.targets[0], "id", None) in {"A6_RESERVED_DEVICE_NAMES",
                                                        "A6_FORBIDDEN_COMPONENT_CHARS"}]
    assert len(consts) == 2, "the gate's two tables are not module-level assignments any more"
    exec(compile(ast.Module(body=consts + [node], type_ignores=[]), "<gate>", "exec"),
         namespace)  # noqa: S102
    gate = namespace[PATH_OK_FN]
    for text in _CANONICALISATION_CORPUS + tuple(t for t, _ in _PATH_CASES):
        gate(text)
    assert sorted(set(tripwire.log)) == ["normpath", "splitdrive"], sorted(set(tripwire.log))


# ===========================================================================
# GROUP J — R-B: a successful Settings save supersedes the startup snapshot
# ===========================================================================
#
# `_SHARED_CACHE_CONFIG` is bound once at import and never refreshed, while `_on_save_shared_cache`
# persists through `save_config`. Labelling the snapshot honestly was not enough: after an
# in-session save the panel still published present-tense falsehoods — "none was configured" when
# one had just been configured, a path that was no longer the path, and via A5 an unqualified
# claim about where a new invocation would be pointed — and the opt-in probed the OLD folder.


class _SaveStub:
    """`fork_analysis_cache` with ONLY `save_config` replaced, so nothing writes a real file.

    Everything else — `_clean`, `_as_bool`, `CACHE_FOLDER`, `CACHE_ENABLED` — is the genuine
    module, which is what keeps the code under test real while the writer is contained. The live
    per-user `shared-config.json` is never opened by this suite.
    """

    def __init__(self, failure=None):
        self.failure = failure
        self.saved: list = []

    def save_config(self, path, mapping):
        self.saved.append((path, dict(mapping)))
        return self.failure

    def __getattr__(self, item):
        return getattr(ac, item)


class _FakeLocation:
    def __init__(self, path, shared):
        self.path = path
        self.shared = shared
        self.notice = ""


def load_a6_with_save(*, shared_config=None, local_default="", save_failure=None,
                      cache_location=None, cache_report=None):
    """The A6 panel and the REAL `_on_save_shared_cache`, in ONE shared namespace.

    One namespace is the whole point: the latch the save handler sets is then the same
    module-level name the report handler reads, which is the property R-B is about. Two namespaces
    would have proved only that two unrelated dictionaries can each hold a boolean.

    The four `STATUS_SHARED_CACHE_*` strings are imported from the real `ui_content` (which has no
    imports of its own), so the handler's return values are the shipped ones rather than stand-ins.
    """
    import ui_content

    stub = _SaveStub(save_failure)
    ns = load_a6(shared_config=shared_config, local_default=local_default,
                 analysis_cache=stub, cache_location=cache_location, cache_report=cache_report)
    ns.update({
        "STATUS_SHARED_CACHE_SAVE_NEEDS_FOLDER": ui_content.STATUS_SHARED_CACHE_SAVE_NEEDS_FOLDER,
        "STATUS_SHARED_CACHE_SAVE_FAILED": ui_content.STATUS_SHARED_CACHE_SAVE_FAILED,
        "STATUS_SHARED_CACHE_SAVED_ON": ui_content.STATUS_SHARED_CACHE_SAVED_ON,
        "STATUS_SHARED_CACHE_SAVED_OFF": ui_content.STATUS_SHARED_CACHE_SAVED_OFF,
        "_shared_cache_config_path": (lambda: os.path.join(local_default or ".", "nowhere.json")),
        "_analysis_cache_location": (lambda: _FakeLocation(r"C:\Saved\Cache", True)),
        "_shared_cache_report": (lambda location: "(report stub)"),
    })
    node = _func(SAVE_HANDLER)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<gui-save>", "exec"),
         ns)  # noqa: S102
    ns["_save_stub"] = stub
    return ns


def test_j1_a_fresh_process_starts_with_the_snapshot_not_superseded(tree):
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    assert ns[SUPERSEDED_FLAG] is False, "the shipped initial value of the latch is not False"
    assert ns[SUPERSEDED_FN]() is False
    assert ns["A6_SUPERSEDED_WARNING"] not in ns[HANDLER](False)


def test_j2_a_successful_save_supersedes_and_the_panel_then_withholds_everything(tree):
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"])
    assert ns[SUPERSEDED_FN]() is False
    returned = ns[SAVE_HANDLER](r"C:\Somewhere\Else", True)
    assert isinstance(returned, str) and returned.strip(), "the save handler's shape changed"
    assert ns["_save_stub"].saved, "the real writer was not reached"
    assert ns[SUPERSEDED_FN]() is True, "a successful save did not supersede the snapshot"

    text = ns[HANDLER](False)
    assert ns["A6_SUPERSEDED_WARNING"] in text
    assert tree["shared"] not in text, "the stale folder was still displayed"
    assert ns["A6_SHARING_ON_LINE"] not in text, "a stale sharing claim survived"
    assert ns["A6_SHARING_OFF_LINE"] not in text
    assert ns["A6_NO_SHARED_FOLDER_LINE"] not in text, \
        "the panel claimed nothing was configured, which it cannot know"
    assert ns["A6_STARTUP_SNAPSHOT_NOTE"] not in text, \
        "a note about 'the folder above' survived with no folder above it"


def test_j2b_a_superseded_read_out_does_not_reveal_that_a_folder_was_configured(tree):
    """The hole `test_j2` left open, measured: mutation M10 of the independent review.

    `_a6_cache_report_lines` passes `''` into `_a6_uninspected_lines` under supersession, so the
    tail cannot disclose the one bit the rest of the refusal withholds — whether a folder was
    configured at startup at all. Changing that single argument to `configured_folder` left ALL
    137 tests of this suite green while adding four lines to the read-out: the name-only sentence
    context and A5's two approved sentences, which `_a6_uninspected_lines` emits only when a folder
    is named. `test_j2` asserts five absences, and none of them is that one.

    So the property is pinned as an IDENTITY rather than as a sixth absence, because an enumerated
    list of absences is exactly what was incomplete: under supersession the read-out must be the
    same text whether or not a folder was configured, in BOTH modes. Any leak breaks the equality,
    through whichever line it leaks.

    The second half is the positive control. An identity test alone is satisfied by deleting the
    three lines everywhere, so the unsuperseded name-only read-out is asserted to still carry all
    three — and the two approved ones by identity against A5's own constants, never as copied text.
    """
    configured = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                                   local_default=tree["local_parent"])
    assert configured[SAVE_HANDLER](r"C:\Somewhere\Else", True)
    assert configured[SUPERSEDED_FN]() is True, "the premise failed: no supersession"

    bare = load_a6_with_save(shared_config=cfg("", False), local_default=tree["local_parent"])
    assert bare[SAVE_HANDLER](r"C:\Somewhere\Else", True)
    assert bare[SUPERSEDED_FN]() is True, "the premise failed: no supersession"

    for probe in (False, True):
        with watch_a4_filesystem() as a4fs:
            with_folder = configured[HANDLER](probe)
            without_folder = bare[HANDLER](probe)
        assert with_folder == without_folder, (
            "a superseded read-out discloses whether a folder was configured at startup. The "
            "startup snapshot is stale in its entirety, and that bit is part of it.\n"
            f"--- configured at startup (probe={probe}) ---\n{with_folder}\n"
            f"--- nothing configured at startup ---\n{without_folder}")
        assert a4fs.calls == [], f"a superseded read-out reached a disk: {a4fs.calls}"

    text = configured[HANDLER](False)
    for leak, label in ((tree["shared"], "the stale folder"),
                        (configured["A6_SHARING_ON_LINE"], "a stale sharing claim"),
                        (configured["A6_SHARING_OFF_LINE"], "a stale sharing claim"),
                        (configured["A6_NO_SHARED_FOLDER_LINE"], "a claim nothing was configured"),
                        (configured["A6_STARTUP_SNAPSHOT_NOTE"], "a note about 'the folder above'"),
                        (configured["A6_NAME_ONLY_SENTENCE_CONTEXT"], "the name-only context"),
                        (a5.SHARED_SENTENCE, "A5's shared-folder sentence"),
                        (a5.OUT_OF_SCOPE_SENTENCE, "A5's out-of-scope sentence")):
        assert leak not in text, f"{label} survived supersession"

    named = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    name_only = named[HANDLER](False)
    assert named["A6_NAME_ONLY_SENTENCE_CONTEXT"] in name_only, (
        "the name-only context is simply gone, so the absences above prove nothing")
    assert a5.SHARED_SENTENCE in name_only, "the approved SHARED_SENTENCE is gone entirely"
    assert a5.OUT_OF_SCOPE_SENTENCE in name_only, "the approved OUT_OF_SCOPE_SENTENCE is gone"


def test_j3_a_refused_save_does_not_supersede(tree):
    """Sharing on with no folder is refused before `save_config` is reached. The snapshot is still
    accurate, so it must keep being usable."""
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"])
    ns[SAVE_HANDLER]("   ", True)
    assert ns["_save_stub"].saved == [], "a refused save still wrote"
    assert ns[SUPERSEDED_FN]() is False, "a refused save superseded the snapshot"
    assert tree["shared"] in ns[HANDLER](False)


def test_j4_a_failed_save_does_not_supersede(tree):
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"],
                           save_failure="the disk is read-only")
    ns[SAVE_HANDLER](r"C:\Somewhere\Else", True)
    assert ns["_save_stub"].saved, "the writer was not even attempted"
    assert ns[SUPERSEDED_FN]() is False, "a FAILED save superseded the snapshot"
    assert tree["shared"] in ns[HANDLER](False)


def test_j5_an_unchanged_value_save_does_supersede_and_that_is_deliberate(tree):
    """Documented rather than clever: establishing that the saved value equals the loaded one
    would need the file read this panel is not permitted to make, so a successful save supersedes
    whatever it wrote. Conservative in the safe direction — it withholds, it never invents."""
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"])
    ns[SAVE_HANDLER](tree["shared"], True)
    assert ns[SUPERSEDED_FN]() is True
    assert ns["A6_SUPERSEDED_WARNING"] in ns[HANDLER](False)


def test_j6_the_latch_is_one_way_and_survives_every_later_callback(tree):
    """Monotonic, which is why no lock is needed: the only transition is False -> True, so no
    interleaving of a save and a report can produce a stale value presented as a fresh one."""
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"])
    ns[HANDLER](False)                       # a report BEFORE any save
    assert ns[SUPERSEDED_FN]() is False, "merely building a report superseded the snapshot"
    ns[SAVE_HANDLER](r"C:\First", True)
    assert ns[SUPERSEDED_FN]() is True
    ns[HANDLER](True)                        # a report BETWEEN two saves
    assert ns[SUPERSEDED_FN]() is True, "a report cleared the latch"
    ns[SAVE_HANDLER]("", False)              # a second, different, successful save
    assert ns[SUPERSEDED_FN]() is True
    ns[SAVE_HANDLER]("   ", True)            # a refused save afterwards
    assert ns[SUPERSEDED_FN]() is True, "a later refusal un-latched a superseded snapshot"


def test_j7_a_superseded_snapshot_refuses_the_opt_in_entirely(tree):
    spy = _ModuleSpy(a4, "inspect_location")
    report_spy = _ModuleSpy(a5, "report_lines")
    with watch_a4_filesystem() as a4fs:
        ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                               local_default=tree["local_parent"],
                               cache_location=spy, cache_report=report_spy)
        ns[SAVE_HANDLER](r"C:\Somewhere\Else", True)
        text = ns[HANDLER](True)
    assert spy.count("inspect_location") == 0, "a superseded snapshot was passed to A4"
    assert a4fs.calls == [], f"a superseded snapshot reached a disk: {a4fs.calls}"
    assert ns["_fs"].calls == []
    assert report_spy.count("report_lines") == 0, "A5 rendered from a superseded snapshot"
    assert a5.SELECTION_SENTENCE not in text, \
        "the panel still claimed where a new invocation would be pointed"
    assert reported_verdicts(shared_lines(text, ns), ns) == {a1.NOT_INSPECTED}


def test_j8_the_latch_reader_fails_closed_on_a_junk_value(tree):
    """The flag's job is to SUPPRESS claims, so anything other than an explicit `False` must
    suppress. That is the mirror image of the opt-in gate's `is not True`."""
    for junk in (True, 1, "yes", object(), None, [], 0):
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        ns[SUPERSEDED_FLAG] = junk
        assert ns[SUPERSEDED_FN]() is True, f"{junk!r} was read as 'not superseded'"
        assert ns["A6_SUPERSEDED_WARNING"] in ns[HANDLER](False)


def test_j9_the_marker_is_called_once_from_the_save_handler_and_nowhere_else():
    """Structural, and positioned: after the `save_config` call and after the failure guard, so a
    refused or failed save cannot reach it."""
    calls = [n for n in ast.walk(_tree())
             if isinstance(n, ast.Call) and ast.unparse(n.func) == MARK_FN]
    assert len(calls) == 1, f"{MARK_FN} is called {len(calls)} times; exactly one is correct"

    save = _func(SAVE_HANDLER)
    inside = [n for n in ast.walk(save)
              if isinstance(n, ast.Call) and ast.unparse(n.func) == MARK_FN]
    assert len(inside) == 1, f"{MARK_FN} is not inside {SAVE_HANDLER}"

    saver = [n for n in ast.walk(save)
             if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("save_config")]
    assert len(saver) == 1, "the save handler no longer has exactly one writer call"
    assert saver[0].lineno < inside[0].lineno, \
        "the snapshot is superseded BEFORE the save is attempted"

    # the marker is a direct statement of the handler body, not nested in a conditional — so no
    # branch can route around it once the failure guard has been passed
    statements = [s for s in save.body
                  if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                  and ast.unparse(s.value.func) == MARK_FN]
    assert len(statements) == 1, (
        f"{MARK_FN} is nested inside a branch of {SAVE_HANDLER}; it must be an unconditional "
        "statement reached only by falling through the failure guard")

    guards = [s for s in save.body
              if isinstance(s, ast.If) and s.lineno < statements[0].lineno
              and any(isinstance(b, ast.Return) for b in s.body)]
    assert len(guards) >= 2, (
        "the refused-save and failed-save early returns are no longer both ahead of the marker")


def test_j10_the_save_handler_reads_no_configuration_file_to_feed_the_latch():
    """The owner's boundary: no new file read. The honest response to a save is to stop making
    present-tense claims, not to go and read the file this panel may not read."""
    marker = _code(_func(MARK_FN))
    for forbidden in ("load_config", "open", "read", "_SHARED_CACHE_CONFIG", "config_path",
                      "json", "_analysis_cache_location"):
        assert forbidden not in marker, f"the marker touches {forbidden}: {marker}"
    assert any(isinstance(n, ast.Global) for n in ast.walk(_func(MARK_FN))), \
        "the marker does not rebind the module-level latch"
    assigned = [ast.unparse(t) for n in ast.walk(_func(MARK_FN))
                if isinstance(n, ast.Assign) for t in n.targets]
    assert assigned == [SUPERSEDED_FLAG], (
        f"the marker assigns {assigned}; it must set exactly the one latch and nothing else")


def test_j11_the_out_of_process_limit_is_disclosed_in_every_mode(tree):
    """A settings file changed by another program after startup is invisible here. Stated in the
    read-out rather than left to be discovered."""
    ns = load_a6_with_save(shared_config=cfg(tree["shared"], True),
                           local_default=tree["local_parent"])
    for probe in (False, True):
        assert ns["A6_SNAPSHOT_SCOPE_NOTE"] in ns[HANDLER](probe)
    ns[SAVE_HANDLER](r"C:\Somewhere\Else", True)
    assert ns["A6_SNAPSHOT_SCOPE_NOTE"] in ns[HANDLER](False), \
        "the limit stopped being disclosed exactly when it started mattering most"


def test_j12_a_superseded_read_out_still_modifies_nothing(tree):
    before = manifest(tree["root"])
    ns = load_a6_with_save(shared_config=cfg(tree["absent_live_parent"], True),
                           local_default=tree["local_parent"])
    ns[SAVE_HANDLER](tree["absent_live_parent"], True)
    for probe in (False, True):
        assert ns[HANDLER](probe).strip()
    assert manifest(tree["root"]) == before
    assert not os.path.isdir(tree["absent_live_parent"])


# ===========================================================================
# GROUP K — the five measured coverage holes, each with its own positive test
# ===========================================================================
#
# Every test in this group was written against a mutation that the shipped 65-test suite left
# GREEN. Each names the mutation it kills, so a future reader can tell a real guard from a
# comfortable one.


def test_k1_the_default_mode_names_the_exact_configured_folder(tree):
    """KILLS: replacing the default-mode folder line's value with a placeholder.

    A6's entire reason to exist is "name the configured folder without contacting it", and the
    shipped suite asserted only that *some* line began with the shared-folder prefix. Substituting
    `'withheld'` for the folder left all 65 tests green.

    The exact name is asserted, with digits in it (digits are legitimate — see correction 2), and
    the zero-contact half is asserted at both seams in the same breath, because "names the folder"
    and "names it without looking" are one property.
    """
    folder = os.path.join(tree["root"], "Shared9", "video_analysis_cache2")
    assert not os.path.isdir(folder), "this test needs a folder nobody has created"
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(folder, True), local_default=tree["local_parent"])
        text = ns[HANDLER](False)

    expected = "  shared analysis-cache folder: " + folder
    assert expected in text.splitlines(), (
        f"the default mode does not name the configured folder exactly.\nexpected: {expected!r}")
    assert a4fs.calls == [] and ns["_fs"].calls == [] and ns["_open"].calls == [], \
        "the folder was named by looking at it"
    assert not os.path.isdir(folder), "naming a folder created it"


def test_k2_the_default_mode_reports_both_folder_states_exactly_once(tree):
    """KILLS: deleting the default-mode shared-state line.

    `test_a7` and `test_d1[False]` both iterate a FILTERED list with no non-emptiness guard, so
    with the line deleted they iterate zero times and pass. `test_c6`/`test_c7` do guard — the
    inconsistency is what made this hole survivable.
    """
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    body = ns[HANDLER](False).splitlines()
    for prefix in (ns["A6_SHARED_STATE_PREFIX"], ns["A6_LOCAL_STATE_PREFIX"]):
        matching = [ln for ln in body if ln.startswith(prefix)]
        assert len(matching) == 1, (
            f"expected exactly one line under {prefix.strip()!r}, found {len(matching)}")
        verdict = matching[0][len(prefix):].split(" -- ")[0].strip()
        assert verdict == a1.NOT_INSPECTED, verdict
    assert ns["A6_NAME_ONLY_NOTE"] in ns[HANDLER](False)


class _MeasureTripwire:
    """`fork_analysis_cache` whose measuring and record-reading entry points EXPLODE.

    The runtime half of R-C item 4: a structural allowlist proves nothing was *written*, and this
    proves nothing is *reached*. `_clean` and `_as_bool` stay real, so the panel under it is real.
    """

    EXPLODING = ("measure", "import_records", "read_records", "scan_library_folder",
                 "inspect_library")

    def __init__(self):
        self.reached: list = []

    def __getattr__(self, item):
        if item in self.EXPLODING:
            def tripped(*a, **k):
                self.reached.append(item)
                raise AssertionError(
                    f"the A6 panel reached {item}, which recursively walks a cache folder and "
                    "totals its bytes -- the exact pair R-3 forbids")
            return tripped
        return getattr(ac, item)


def test_k3_no_measuring_entry_point_is_reached_at_runtime_in_either_mode(tree):
    """KILLS: adding `fork_analysis_cache.measure(configured_folder)` to the opt-in handler.

    The structural allowlist in `test_b5` is the primary guard; this is the independent runtime
    one, and it also covers a measurer reached through an alias the allowlist's name reduction
    might one day fail to resolve.
    """
    for probe in (False, True):
        tripwire = _MeasureTripwire()
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"],
                     analysis_cache=tripwire)
        text = ns[HANDLER](probe)
        assert text.strip()
        assert tripwire.reached == [], f"probe={probe} reached {tripwire.reached}"


def test_k3b_the_tripwire_actually_fires(tree):
    """A negative control for k3: a tripwire that could not fire would prove nothing."""
    tripwire = _MeasureTripwire()
    with pytest.raises(AssertionError, match="recursively walks"):
        tripwire.measure(tree["shared"])
    assert tripwire.reached == ["measure"]
    assert tripwire._clean("  x  ") == "x", "the tripwire broke the real parser it delegates to"


def test_k4_a_successful_observation_carries_an_offset_aware_timestamp(tree):
    """KILLS: making the injected observation clock raise.

    A4 swallows a raising clock and returns `observed_at=''`; the panel then substituted 'the
    moment this report was built', which reads like a time and is not one. So the suite could not
    tell a stamped observation from an unstamped one, and all 65 tests stayed green with the clock
    broken.

    Both arms are needed. This one proves a REAL stamp is present and offset-aware; `test_k5`
    proves a missing one is declared missing rather than papered over.
    """
    import datetime as dt

    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    text = ns[HANDLER](True)
    assert ns["A6_NO_TIMESTAMP_LINE"] not in text, "a working clock reported no time"
    assert ns["A6_WHEN_UNAVAILABLE"] not in text

    prefix = ns["A6_SHARED_STATE_PREFIX"]
    line = next(ln for ln in text.splitlines() if ln.startswith(prefix))
    stamp = line.split("as observed at ", 1)[1].strip()
    parsed = dt.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None, (
        f"the observation stamp {stamp!r} is naive. A naive local time is ambiguous twice a year "
        "and meaningless on another machine, and this is the record of when a folder was seen.")
    assert parsed.utcoffset() is not None
    assert abs((parsed - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 3600, \
        f"the stamp {stamp!r} is not of this moment"


def test_k5_a_clock_that_fails_is_declared_rather_than_papered_over(tree):
    """The other arm: A4 hands back `''`, and the read-out says the time is unknown instead of
    substituting a phrase that reads like one. No ISO-looking stamp may appear anywhere."""
    import re

    real_inspect = a4.inspect_location

    def clock_broken(local_default, config=None, **kwargs):
        kwargs["clock"] = None                     # exactly what A4 produces for a failing clock
        return real_inspect(local_default, config, **kwargs)

    a4.inspect_location = clock_broken
    try:
        ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
        text = ns[HANDLER](True)
    finally:
        a4.inspect_location = real_inspect

    assert ns["A6_NO_TIMESTAMP_LINE"] in text, "a missing observation time was not declared"
    assert ns["A6_WHEN_UNAVAILABLE"] in text
    assert "the moment this report was built" not in text, \
        "a placeholder phrase stood in for a time that was never established"
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text), \
        "a timestamp appeared although none was established"


def test_k5b_a_raising_clock_really_does_reach_the_panel_as_an_empty_stamp(tree):
    """The premise k5 stands on, measured on the real A4 rather than assumed: A4 swallows the
    exception in a broad `except` and reports an empty stamp."""
    def boom():
        raise RuntimeError("no clock here")

    observation = a4.inspect_location(tree["local_parent"],
                                      cfg(tree["shared"], True), clock=boom)
    assert observation.observed_at == "", \
        "A4 no longer swallows a failing clock; test_k5's premise needs re-measuring"
    ns = load_a6()
    assert ns["A6_WHEN_UNAVAILABLE"] in ns[STATE_FN](a1.PRESENT, "")


def test_k6_the_function_tuple_covers_every_a6_function_in_gui_py():
    """The structural bans are only as wide as `A6_FUNCTIONS`. A new `_a6_*` function that nobody
    added to that tuple would escape every one of them silently, so the tuple itself is pinned."""
    declared = {n.name for n in ast.walk(_tree())
                if isinstance(n, ast.FunctionDef) and n.name.startswith("_a6_")}
    covered = set(A6_FUNCTIONS) | {MARK_FN}
    assert declared <= covered, (
        f"gui.py declares A6 functions outside A6_FUNCTIONS: {sorted(declared - covered)}. "
        "Add them deliberately — every structural ban in this file widens with that tuple.")
    assert HANDLER in A6_FUNCTIONS and LINES_FN in A6_FUNCTIONS


def test_k7_the_a6_regions_are_the_regions_this_change_introduced():
    """`test_g12` never exercised `_a6_regions`, so a mis-aligned slice would have gone unnoticed
    and `test_g11` would have been guarding the wrong lines. This exercises the real slicer and
    checks each region actually contains its own marker and not the other's."""
    source = _source(_GUI)
    regions = _a6_regions(source)
    assert len(regions) == 2, regions
    panel_region, wiring_region = regions

    assert "gr.Accordion(LABEL_LIBRARY_CACHE_REPORT" in panel_region
    assert CHECKBOX in panel_region and REPORT_BOX in panel_region
    assert f"{BUTTON}.click(" not in panel_region, "the panel region swallowed the wiring"

    assert f"{BUTTON}.click(" in wiring_region
    assert "gr.Accordion(" not in wiring_region, "the wiring region swallowed the panel"

    # each region really is a contiguous slice of the shipped file, not a reconstruction
    for region in regions:
        assert region in source, "a region is not a verbatim slice of gui.py"
        assert region.strip(), "a region came back empty"


# ===========================================================================
# GROUP L — the bounded editorial corrections, each pinned by a real assertion
# ===========================================================================


def test_l1_both_creation_lines_are_reachable_and_neither_implies_a_modification(tree):
    """`resolve_location` asks for the configured folder whenever sharing is on and the folder or
    its parent exists — PRESENT included — and does not ask at all when sharing is off or the
    parent is dead. Both lines are therefore live, and both must be worded so that a reader cannot
    take either as evidence that something happened on disk.

    The old `'  directory creation requested and refused: '` left that ambiguous and additionally
    fires for a folder that already exists, where "creation" reads as a claim the folder was
    missing.
    """
    requested = []
    not_requested = []
    for case, enabled in (("shared", True), ("absent_live_parent", True),
                          ("absent_dead_parent", True), ("shared", False)):
        before = manifest(tree["root"])
        ns = load_a6(shared_config=cfg(tree[case], enabled),
                     local_default=tree["local_parent"])
        text = ns[HANDLER](True)
        (requested if ns["A6_CREATION_REFUSED_PREFIX"].strip() in text
         else not_requested).append((case, enabled))
        assert manifest(tree["root"]) == before, f"{case}/{enabled} changed the tree"

    assert requested, "the creation-refused line is unreachable, so its wording is untested"
    assert not_requested, "the nothing-asked line is unreachable, so its wording is untested"

    ns = load_a6()
    required = "nothing was created or changed on disk"
    for line in (ns["A6_CREATION_REFUSED_PREFIX"], ns["A6_CREATION_NONE_REQUESTED"]):
        low = line.lower()
        assert required in low, f"the line does not state that nothing happened: {line}"
        # The required phrase is REMOVED before the bans are applied, because it CONTAINS two of
        # them ("was created", "created the"). A `not in` guard over a string that contains the
        # phrase the same test requires is unsatisfiable — this repository's most-repeated trap,
        # and it caught this very test on its first run. Ban the residue, not the whole line.
        residue = low.replace(required, "")
        for misleading in ("was created", "has been created", "created the", "then removed",
                           "rolled back", "deleted", "undone"):
            assert misleading not in residue, (
                f"{misleading!r} implies a modification happened: {line}")


def test_l2_the_opt_in_headline_never_claims_an_unobserved_shared_folder(tree):
    """Sharing off, or no folder named, means nobody looked at the shared folder — so the
    headline must not say its state was observed. A4 still probed the LOCAL folder, and the
    replacement headline says exactly that and no more."""
    ns = load_a6(shared_config=cfg(tree["shared"], True), local_default=tree["local_parent"])
    observed = ns[HANDLER](True)
    assert ns["A6_OPT_IN_HEADLINE"] in observed
    assert ns["A6_OPT_IN_HEADLINE_SHARED_NOT_OBSERVED"] not in observed

    for folder, enabled in ((tree["shared"], False), ("", False)):
        ns = load_a6(shared_config=cfg(folder, enabled), local_default=tree["local_parent"])
        text = ns[HANDLER](True)
        assert ns["A6_OPT_IN_HEADLINE_SHARED_NOT_OBSERVED"] in text, (
            f"folder={folder!r} enabled={enabled}: the headline claimed the shared folder was "
            "observed when A4 never probed it")
        assert ns["A6_OPT_IN_HEADLINE"] not in text
        assert reported_verdicts(shared_lines(text, ns), ns) == {a1.NOT_INSPECTED}


def test_l3_the_opt_in_label_does_not_promise_a_single_os_operation():
    """One A4 invocation is one bounded observation, not one syscall: A1 probes the folder and may
    probe its ancestors to tell "not created yet" apart from "not reachable". The previous label
    said "one directory check", which a careful reader can only take as one operation."""
    ns = load_a6()
    for text in (ns["LABEL_LIBRARY_CACHE_PROBE_SHARED"], ns["INFO_LIBRARY_CACHE_REPORT"]):
        low = text.lower()
        assert "one directory check" not in low, f"still promises one operation: {text}"
    label = ns["LABEL_LIBRARY_CACHE_PROBE_SHARED"].lower()
    assert "directory-state" in label, label
    assert "above" in label or "parent" in label, \
        f"the label does not disclose that folders above the named one may be looked at: {label}"
    assert "off by default" in label, label
    # That the disclosed ancestor probing REALLY happens is measured in `test_l4`. A label is only
    # allowed to disclose what is true, so the two tests are a pair.


def test_l4_the_disclosed_ancestor_probing_actually_happens(tree):
    """The companion to l3: a label may only disclose what is true. A configured-but-absent folder
    is where A1 must look above the folder itself, and this measures that it does."""
    with watch_a4_filesystem() as a4fs:
        ns = load_a6(shared_config=cfg(tree["absent_live_parent"], True),
                     local_default=tree["local_parent"])
        ns[HANDLER](True)
    touched = {path for _name, path in a4fs.calls}
    parent = os.path.normcase(os.path.abspath(os.path.dirname(tree["absent_live_parent"])))
    assert parent in touched, (
        "the label discloses that folders above the named one may be looked at, but no ancestor "
        f"was probed. Looked at: {sorted(touched)}")
    assert not os.path.isdir(tree["absent_live_parent"])


def test_l5_the_a3_import_comment_states_the_property_that_actually_holds():
    """A5 imports constants from A3 at module load, so A3's module object IS in `sys.modules`
    before this panel runs. "A3 is deliberately NOT imported" was false as written; the property
    that holds is that no A3 FUNCTION is reachable from the panel."""
    import importlib

    importlib.import_module("beatsync_fork.library_cache_report")
    assert "beatsync_fork.library_cache_records" in sys.modules, (
        "A5 no longer loads A3's module; the comment this test guards needs re-measuring")

    source = _source(_GUI)
    assert "A3 (`library_cache_records`) is deliberately NOT imported." not in source, \
        "the false claim is still in gui.py"
    marker = "A3 (`library_cache_records`) is deliberately not imported HERE"
    assert marker in source, "the corrected comment is not where this test expects it"
    block = source[source.index(marker):source.index(marker) + 700]
    low = block.lower()
    assert "no a3 function is reachable" in low, block
    assert "sys.modules" in block, "the comment does not say that A3's module does get loaded"

    # and the substantive property is still true, measured rather than asserted
    a3 = importlib.import_module("beatsync_fork.library_cache_records")
    reachable = {ast.unparse(n).rsplit(".", 1)[-1]
                 for n in ast.walk(ast.parse(_a6_bodies()))
                 if isinstance(n, (ast.Name, ast.Attribute))}
    public = {n for n in dir(a3) if not n.startswith("_") and callable(getattr(a3, n))}
    assert not (public & reachable), f"an A3 function is reachable: {sorted(public & reachable)}"


def test_k8_the_span_poison_guard_fires_on_a_token_planted_in_a_real_region():
    """`test_g12` checked that two tokens are findable in a hand-written string — which tests the
    tuple, not the guard. This plants a token into an actual region and asserts the guard's own
    comparison rejects it, so the non-vacuity control exercises the thing it is a control for."""
    source = _source(_GUI)
    clean = _a6_regions(source)
    for region in clean:
        assert not [t for t in _SPAN_POISON if t in region], "the shipped region is already red"

    for token in ("LABEL_PREP_SECTION", "maximum=", "_RENDER_LOCK", "clips_check_btn.click("):
        poisoned = clean[0] + f"\n# a planted mention of {token}\n"
        hits = [t for t in _SPAN_POISON if t in poisoned]
        assert token in hits, (
            f"planting {token!r} in a real A6 region did not trip the guard, so test_g11 cannot "
            "detect it either")
