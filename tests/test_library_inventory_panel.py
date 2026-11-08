"""R14-A stage F-3, Option 1 — the LOCAL-ONLY Media Library inventory panel.

F-3 was the finding that A2 (`library_inventory`) and A3 (`library_cache_records`) merged with no
user surface at all. The owner resolved it in favour of Option 1 — a small, read-only, local-only
exposure of the producers that already exist — and chose two operational details explicitly:

* **record reading is DEFAULT OFF**, and needs its own tick *and* a deliberate Build click.
  Ticking the box alone must read nothing; and
* **the Discarded folder is OMITTED** from this increment: not inspected, not offered, not
  selectable. `include_rejected` is always ``False``.

The approved R-3 restrictions on the live shared analysis-cache folder are carried over unchanged
and are what most of this file is about. Stated precisely, because it is easy to state backwards:

    this panel never reads, lists, counts, measures or resolves the shared folder. It uses the
    startup snapshot's folder TEXT for exactly one purpose — proving, lexically, that the folder
    it is about to read records from is NOT that folder — and where that cannot be proved, the
    record pass is REFUSED rather than attempted.

`gui.py` cannot be imported on a bare interpreter (`logger` pulls in librosa, `gpu_cpu_utils`
probes NVENC), so the REAL F-3 functions and the REAL F-3 constants are AST-extracted from
`gui.py` and executed against a synthesised namespace, exactly as
`tests/test_library_cache_panel.py` does for A6. The A1–A5 fork modules **are** imported normally
— they are stdlib-only by rule — so what runs here is the real scanner, the real record reader and
the real report renderer, not a mock of any of them.

Three measurement notes, each of which an earlier draft of a sibling suite got wrong:

1. **A1, A2 and A3 bind their filesystem callables as keyword defaults at import time.** Patching
   the `os` module, or patching `probe_root`, is therefore invisible to them. Every zero-contact
   assertion here is measured at `__kwdefaults__`, plus a seam-independent before/after manifest
   of the whole fixture tree, which no binding trick can evade.
2. **Digits are not banned; a fabricated figure is.** A path contains digits and so does a
   timestamp, and A5's own rendered block legitimately carries numbers — each with its unit, its
   exactness and the population it answers for. The ban is on this panel authoring a number of its
   own, and on anything unestablished being rendered as a zero.
3. **Every guard carries a control.** A zero-call assertion that cannot fail, and a ban list whose
   entries are unsatisfiable, are the two ways this repository has shipped a false green before.
"""

from __future__ import annotations

import ast
import builtins
import functools
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from beatsync_fork import analysis_cache as ac                      # noqa: E402
from beatsync_fork import clips_review as fork_clips                # noqa: E402
from beatsync_fork import input_manager as im                       # noqa: E402
from beatsync_fork import library_cache_records as a3               # noqa: E402
from beatsync_fork import library_cache_report as a5                # noqa: E402
from beatsync_fork import library_inventory as a1                   # noqa: E402

HANDLER = "_on_library_inventory_click"
LINES_FN = "_f3_inventory_lines"
MEDIA_FN = "_f3_media_inventory"
RECORD_FN = "_f3_record_lines"
IDENTITY_FN = "_f3_local_cache_identity"
SHARED_KEYS_FN = "_f3_shared_folder_keys"
LOCATION_FN = "_f3_location_lines"
TRUTH_FN = "_f3_record_truth_line"
CLOCK_FN = "_f3_observed_at"

BUTTON = "library_inventory_btn"
FOLDER_BOX = "library_inventory_folder"
RECORDS_BOX = "library_inventory_records"
REPORT_BOX = "library_inventory_box"

#: The whole F-3 call graph, in one place, because every structural ban below is only as wide as
#: this tuple. `test_s7` pins the tuple itself against `gui.py`, so a new `_f3_*` function cannot
#: be added to the panel without either entering these bans or failing that test.
F3_FUNCTIONS = (HANDLER, LINES_FN, MEDIA_FN, RECORD_FN, IDENTITY_FN, SHARED_KEYS_FN, LOCATION_FN,
                TRUTH_FN, CLOCK_FN)

#: The three PURE A6 helpers this panel is permitted to read, and the only three. All touch no
#: disk. Sharing them is the "one parser, not two that can disagree" argument A4 and A6 make; a
#: second copy of the path gate in particular would be two equivalence proofs to keep equal.
A6_SHARED_HELPERS = ("_a6_configured_path_is_unambiguous", "_a6_shared_cache_startup_snapshot",
                     "_a6_startup_snapshot_superseded")
#: Loaded so the supersession tests drive the latch through the SHIPPED transition rather than a
#: test-local imitation of it.
A6_MARK_FN = "_a6_mark_startup_snapshot_superseded"
SUPERSEDED_FLAG = "_A6_SNAPSHOT_SUPERSEDED"

#: A6 constants the shared helpers read. Executed from the real `gui.py`, in source order.
A6_CONSTANTS = ("A6_LOCAL_CACHE_DIRNAME", "A6_RESERVED_DEVICE_NAMES",
                "A6_FORBIDDEN_COMPONENT_CHARS")


# ---------------------------------------------------------------------------
# AST plumbing (the conventions of tests/test_library_cache_panel.py)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _source(path: str = _GUI) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@functools.lru_cache(maxsize=8)
def _tree(path: str = _GUI) -> ast.Module:
    return ast.parse(_source(path))


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in gui.py")


def _code(node: ast.FunctionDef) -> str:
    """A function body with its docstring removed, so prose cannot satisfy a code assertion."""
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _f3_bodies(names=F3_FUNCTIONS) -> str:
    return "\n".join(_code(_func(n)) for n in names)


def _click(widget: str) -> ast.Call:
    return next(n for n in ast.walk(_tree())
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "click" and getattr(n.func.value, "id", None) == widget)


def _call_sites(name: str, names=F3_FUNCTIONS) -> list:
    """Every call to `name` inside the F-3 bodies, by resolved final attribute name."""
    return [n for n in ast.walk(ast.parse(_f3_bodies(names)))
            if isinstance(n, ast.Call) and ast.unparse(n.func).rsplit(".", 1)[-1] == name]


def _kwargs(call: ast.Call) -> dict:
    return {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}


# ---------------------------------------------------------------------------
# recording shims
# ---------------------------------------------------------------------------

class _FsSpy:
    """Records the filesystem calls the loaded code makes through its OWN `os` name.

    This is the shallow seam: it sees what the F-3 handler does directly and nothing that A1, A2
    or A3 do, because those bind their callables as keyword defaults at import time. It is paired
    with `watch_fork_filesystem` below for exactly that reason.
    """

    RECORDED = ("listdir", "scandir", "stat", "lstat", "makedirs", "mkdir", "remove", "rmdir",
                "rename", "replace", "walk", "unlink", "open")
    PATH_RECORDED = ("isdir", "isfile", "exists", "realpath", "getsize", "lexists")

    def __init__(self):
        self.calls: list = []
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

    def touching(self, needle: str) -> list:
        key = os.path.normcase(os.path.abspath(needle))
        return [c for c in self.calls
                if c[1] and os.path.normcase(os.path.abspath(c[1])).startswith(key)]


class _OsProxy:
    """A recording stand-in for a module's own `os` global (the `scan_folder` seam)."""

    RECORDED = ("listdir", "scandir", "stat", "lstat", "walk", "makedirs", "mkdir", "remove",
                "rmdir", "rename", "replace", "unlink")

    def __init__(self, sink: list):
        self._sink = sink
        sink_ref = sink

        class _Path:
            def __getattr__(self, item):
                target = getattr(os.path, item)
                if item in ("realpath", "isdir", "isfile", "exists", "getsize"):
                    def recorded(*a, **k):
                        sink_ref.append((item, str(a[0]) if a else ""))
                        return target(*a, **k)
                    return recorded
                return target

        self.path = _Path()

    def __getattr__(self, item):
        target = getattr(os, item)
        if item in self.RECORDED:
            sink = self._sink

            def recorded(*a, **k):
                sink.append((item, str(a[0]) if a else ""))
                return target(*a, **k)
            return recorded
        return target


class watch_fork_filesystem:  # noqa: N801  (a context manager, named for what it does)
    """Record EVERY filesystem call A1/A2/A3 make, at the only seams that actually see them.

    `inspect_library` and `read_records` bind `listdir`, `lstat`, `isdir` and `realpath` as
    keyword defaults at import time and then pass them explicitly down to A1's `probe_root`, so
    neither patching the `os` module nor patching `probe_root` is observed. A3 additionally reads
    record bytes with the builtin `open` resolved through its own module globals, and
    `input_manager.scan_folder` reaches the disk through its module's `os` global.

    Without all four seams a "no shared-folder contact" assertion would be vacuous with respect
    to the components that do the actual work — which is what the negative controls in GROUP K
    demonstrate.
    """

    WATCHED = {"listdir": os.listdir, "lstat": os.lstat,
               "isdir": os.path.isdir, "realpath": os.path.realpath}
    #: The PRISTINE function objects, captured at import. Resolving `a3.read_records` at enter
    #: time instead would patch whatever wrapper a record-reader spy had just installed, leaving
    #: the real function's own defaults untouched and this instrument silently blind.
    TARGETS = (a1.inspect_library, a3.read_records)

    def __init__(self):
        self.calls: list = []
        self.opened: list = []
        self._saved: dict = {}
        self._saved_open: dict = {}
        self._saved_os = None
        self._entered = False

    def __enter__(self):
        try:
            for fn in self.TARGETS:
                defaults = fn.__kwdefaults__ or {}
                for name, real in self.WATCHED.items():
                    if name not in defaults:
                        continue
                    assert defaults[name] is real, (
                        f"{fn.__name__}'s {name} default is not the real one; this seam needs "
                        "re-measuring")
                    self._saved[(fn, name)] = defaults[name]
                    defaults[name] = self._record(name, real)
            # BOTH record readers, because they are different modules. A3 parses with its own
            # module-global `open`; the delegated transport check re-opens the same path from
            # inside `analysis_cache`. Watching only A3 would make "no record is read twice"
            # vacuous with respect to the one thing that would read it twice.
            for module in (a3, ac):
                self._saved_open[module] = module.__dict__.get("open", None)
                module.open = self._record_open
            self._saved_os = im.os
            im.os = _OsProxy(self.calls)
            self._entered = True
        except BaseException:
            # A half-installed instrument would leak patched defaults into every later test and
            # make them all fail on the assertion above. Unwind whatever was installed first.
            self._restore()
            raise
        return self

    def __exit__(self, *exc):
        self._restore()
        return False

    def _restore(self):
        for (fn, name), real in self._saved.items():
            fn.__kwdefaults__[name] = real
        self._saved.clear()
        for module, saved in self._saved_open.items():
            if saved is None:
                module.__dict__.pop("open", None)
            else:
                module.open = saved
        self._saved_open.clear()
        if self._saved_os is not None:
            im.os = self._saved_os
        self._saved_os = None

    def _record(self, name, real):
        def recorded(path, *a, **k):
            self.calls.append((name, str(path)))
            return real(path, *a, **k)
        return recorded

    def _record_open(self, file, *a, **k):
        self.opened.append(str(file))
        self.calls.append(("open", str(file)))
        return builtins.open(file, *a, **k)

    def touching(self, needle: str) -> list:
        key = os.path.normcase(os.path.abspath(needle))
        out = []
        for name, path in self.calls:
            if not path:
                continue
            full = os.path.normcase(os.path.abspath(path))
            if full == key or full.startswith(key + os.sep):
                out.append((name, path))
        return out


class _RecordReaderSpy:
    """A3 whose `read_records` is counted and whose directory argument is captured."""

    def __init__(self):
        self.calls: list = []
        self._real = a3.read_records

    def __enter__(self):
        spy = self

        def recorded(cache_dir, **kw):
            spy.calls.append((str(cache_dir), dict(kw)))
            return spy._real(cache_dir, **kw)
        a3.read_records = recorded
        return self

    def __exit__(self, *exc):
        a3.read_records = self._real
        return False


# ---------------------------------------------------------------------------
# the loader: REAL F-3 code over REAL A1-A5, in a synthesised namespace
# ---------------------------------------------------------------------------

def _wanted_constants() -> list:
    names = [t.id for n in _tree().body if isinstance(n, ast.Assign) and len(n.targets) == 1
             for t in [n.targets[0]] if isinstance(t, ast.Name)
             and (t.id.startswith("F3_") or t.id.startswith("LABEL_LIBRARY_INVENTORY")
                  or t.id == "INFO_LIBRARY_INVENTORY")]
    assert names, "no F-3 constant was found in gui.py at all; the loader would prove nothing"
    return names


def load_f3(*, shared_config=None, local_default="", fs=None, mutations=()) -> dict:
    """Execute the real F-3 constants and functions against a controlled namespace."""
    fs = fs if fs is not None else _FsSpy()
    namespace: dict = {
        "os": fs,
        "datetime": __import__("datetime"),
        "get_input_dir": (lambda: local_default),
        "_SHARED_CACHE_CONFIG": {} if shared_config is None else dict(shared_config),
        "fork_analysis_cache": ac,
        "fork_cache_report": a5,
        "fork_clips": fork_clips,
        "scan_library_folder": im.scan_folder,
        "InputScanError": im.InputScanError,
        "__builtins__": builtins,
    }

    tree = _tree()
    wanted = set(_wanted_constants()) | set(A6_CONSTANTS) | {SUPERSEDED_FLAG}
    body: list = [n for n in tree.body
                  if isinstance(n, ast.Assign) and len(n.targets) == 1
                  and getattr(n.targets[0], "id", None) in wanted]
    missing = wanted - {n.targets[0].id for n in body}
    assert not missing, f"constants absent from gui.py: {sorted(missing)}"

    for name in A6_SHARED_HELPERS + (A6_MARK_FN,) + F3_FUNCTIONS:
        node = _func(name)
        for mutate in mutations:
            node = mutate(name, node)
        body.append(node)

    exec(compile(ast.Module(body=body, type_ignores=[]), "<gui-f3>", "exec"),
         namespace)  # noqa: S102
    namespace["_fs"] = fs
    return namespace


# ---------------------------------------------------------------------------
# the mutators — one per headline claim, each used as its own negative control
# ---------------------------------------------------------------------------

def _find_if(node: ast.FunctionDef, needle: str) -> ast.If:
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.If) and needle in ast.unparse(stmt.test):
            return stmt
    raise AssertionError(f"no `if` whose test names {needle!r} in {node.name}")


def mutate_records_always_on(name, node):
    """DEFECT: the default-off gate stops gating, so an unticked click reads records."""
    if name != LINES_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    branch = _find_if(clone, "inspect_records")
    branch.test = ast.Constant(False)          # take the `else` (the record pass) every time
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control did not change the gate"
    return mutated


def mutate_shared_alias_allowed(name, node):
    """DEFECT: the local/shared identity proof is dropped, so an alias onto shared is read."""
    if name != IDENTITY_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    branch = _find_if(clone, "shared_keys")
    branch.test = ast.Constant(False)
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control did not change the gate"
    return mutated


def mutate_prefix_comparison(name, node):
    """DEFECT: the exact-equality identity proof becomes a PREFIX test.

    The specific trap: with a prefix test, any folder whose path merely STARTS WITH the configured
    shared one -- a sibling named `…VideoAnalysis2`, or anything under `…\\SharedCache` -- is
    called the shared folder and refused. The R1 independent review measured that the previous
    version of `test_e6` stayed GREEN under exactly this mutation, because it never used its own
    look-alike fixture as the local cache.
    """
    if name != IDENTITY_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    branch = _find_if(clone, "shared_keys")
    branch.test = ast.parse(
        "any(os.path.normcase(typed).startswith(key) "
        "or os.path.normcase(resolved).startswith(key) for key in shared_keys)").body[0].value
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control did not change the comparison"
    return mutated


def mutate_typed_arm_dropped(name, node):
    """DEFECT: only the RESOLVED path is compared, so the typed arm stops protecting anything.

    Also measured by the R1 review: deleting `normcase(typed)` left the shipped suite green,
    while deleting `normcase(resolved)` went red. One half of the comparison had no coverage.
    """
    if name != IDENTITY_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    branch = _find_if(clone, "shared_keys")
    branch.test = ast.parse("{os.path.normcase(resolved)} & shared_keys").body[0].value
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control did not change the comparison"
    return mutated


def mutate_root_gate_open(name, node):
    """DEFECT: every root text is treated as unambiguous, so a blank field scans something."""
    if name != LINES_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    branch = _find_if(clone, "_a6_configured_path_is_unambiguous")
    branch.test = ast.Constant(False)
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control did not change the gate"
    return mutated


def mutate_transport_enabled(name, node):
    """DEFECT: the delegated transport check is requested, so every record is read twice."""
    if name != RECORD_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    for call in ast.walk(clone):
        if isinstance(call, ast.Call) and ast.unparse(call.func).endswith("read_records"):
            for kw in call.keywords:
                if kw.arg == "looks_reusable":
                    kw.value = ast.parse("fork_analysis_cache.looks_reusable").body[0].value
            mutated = ast.fix_missing_locations(clone)
            assert ast.unparse(mutated) != ast.unparse(node), "the control changed nothing"
            return mutated
    raise AssertionError("no read_records call to mutate")


def mutate_bounds_widened(name, node):
    """DEFECT: the reduced bounds are replaced by A3's own much larger defaults."""
    if name != RECORD_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    swap = {"max_entries": "fork_cache_records.MAX_CACHE_ENTRIES",
            "max_records": "fork_cache_records.MAX_CACHE_RECORDS",
            "max_record_bytes": "fork_cache_records.MAX_RECORD_BYTES",
            "max_total_bytes": "fork_cache_records.MAX_TOTAL_RECORD_BYTES",
            "max_reported_names": "fork_cache_records.MAX_REPORTED_RECORD_NAMES"}
    for call in ast.walk(clone):
        if isinstance(call, ast.Call) and ast.unparse(call.func).endswith("read_records"):
            for kw in call.keywords:
                if kw.arg in swap:
                    kw.value = ast.parse(swap[kw.arg]).body[0].value
            mutated = ast.fix_missing_locations(clone)
            assert ast.unparse(mutated) != ast.unparse(node), "the control changed nothing"
            return mutated
    raise AssertionError("no read_records call to mutate")


def mutate_fabricated_zero(name, node):
    """DEFECT: the panel authors a figure of its own, and it is a zero."""
    if name != RECORD_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    planted = ast.parse("lines.append('  records: 0 obsolete records, safe to delete')").body[0]
    # Planted BEFORE A5's own rendered block, because that block is excluded from the figure scan
    # by design — A5 is the approved renderer. A zero hidden inside its lines would be testing the
    # exclusion rather than the guard.
    cut = next(i for i, stmt in enumerate(clone.body)
               if "report_lines" in ast.unparse(stmt))
    clone.body.insert(cut, planted)
    mutated = ast.fix_missing_locations(clone)
    assert ast.unparse(mutated) != ast.unparse(node), "the control changed nothing"
    return mutated


def mutate_point_at_shared(name, node):
    """DEFECT: the handler composes the SHARED folder as the folder to read records from."""
    if name != HANDLER:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    for stmt in clone.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and getattr(stmt.targets[0], "id", None) == "local_cache"):
            stmt.value = ast.parse("_a6_shared_cache_startup_snapshot()[0]").body[0].value
            mutated = ast.fix_missing_locations(clone)
            assert ast.unparse(mutated) != ast.unparse(node), "the control changed nothing"
            return mutated
    raise AssertionError("no local_cache assignment to mutate")


def mutate_discarded_included(name, node):
    """DEFECT: the Discarded root is offered to the scanner after all."""
    if name != MEDIA_FN:
        return node
    clone = ast.parse(ast.unparse(node)).body[0]
    for call in ast.walk(clone):
        if isinstance(call, ast.Call) and ast.unparse(call.func).endswith("inspect_library"):
            for kw in call.keywords:
                if kw.arg == "include_rejected":
                    kw.value = ast.Constant(True)
            mutated = ast.fix_missing_locations(clone)
            assert ast.unparse(mutated) != ast.unparse(node), "the control changed nothing"
            return mutated
    raise AssertionError("no inspect_library call to mutate")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tree(tmp_path):
    """A realistic library folder, a local cache, a shared cache, and the awkward neighbours."""
    media = tmp_path / "Library"
    # A SIBLING, which is what the application's own Discarded root is. Nesting it under the
    # library would make "the Discarded folder was not counted" untestable: the authoritative
    # scan is recursive, so its files would legitimately be inside the library's own total.
    rejected = tmp_path / "Discarded"
    local = tmp_path / "input" / "video_analysis_cache"
    shared = tmp_path / "SharedCache" / "VideoAnalysis"
    # A folder the configured shared path is a strict string PREFIX of, so a prefix test would
    # call it shared. It is a realistic NEIGHBOUR in this tree and is deliberately kept here --
    # but the discriminating prefix case needs the LOCAL CACHE to sit under it, which this
    # fixture's local cache does not, so `test_e6` builds its own tree (`_prefix_tree`). The
    # earlier version of that test used this entry and proved nothing; see R1 finding F-3.
    lookalike = tmp_path / "SharedCache" / "VideoAnalysis2"
    for p in (media, rejected, local, shared, lookalike):
        p.mkdir(parents=True)
    (media / "a.mp4").write_bytes(b"\0" * 2048)
    (media / "b.mkv").write_bytes(b"\0" * 4096)
    (media / "notes.txt").write_bytes(b"x" * 16)
    (rejected / "bad.mp4").write_bytes(b"\0" * 1024)
    # Two well-formed local records: one inside the library, one pointing elsewhere.
    (local / "aabbccdd_0123456789abcdef01234567.json").write_text(
        '{"cache_contract": "stage5_cache_v3", "source_path": '
        + _json_path(media / "a.mp4") + '}', encoding="utf-8")
    (local / "11223344_89abcdef0123456789abcdef.json").write_text(
        '{"cache_contract": "other_generation", "source_path": '
        + _json_path(tmp_path / "Elsewhere" / "z.mp4") + '}', encoding="utf-8")
    # Deliberately retained worker artifacts: recognised by SHAPE, so these are skipped.
    (local / "qwen_batch_request_000.json").write_text("{}", encoding="utf-8")
    (local / "qwen_batch_response_000.json").write_text("{}", encoding="utf-8")
    (shared / "deadbeef_fedcba9876543210fedcba98.json").write_text(
        '{"cache_contract": "stage5_cache_v3"}', encoding="utf-8")
    return {
        "root": str(tmp_path),
        "media": str(media),
        "rejected": str(rejected),
        "input": str(tmp_path / "input"),
        "local": str(local),
        "shared": str(shared),
        "lookalike": str(lookalike),
        "absent": str(tmp_path / "NoSuchFolder"),
    }


def _json_path(p) -> str:
    import json
    return json.dumps(str(p))


def _junction(link, target) -> None:
    """A real directory junction, or skip. `mklink /J` needs no elevation; a symlink does.

    The alias cases below are measured against a genuine reparse point rather than a stub,
    because the whole question is what `realpath` does with one.
    """
    import subprocess
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.normpath(str(link)),
                               os.path.normpath(str(target))], capture_output=True, text=True)
        if done.returncode == 0:
            return
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    pytest.skip("this platform would not create a directory junction or symlink")


def manifest(root: str) -> dict:
    """`(kind, size, mtime_ns)` for every entry, plus the directory set — the zero-write
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


def ns_for(tree, *, shared=None, enabled=True, fs=None, mutations=()) -> dict:
    return load_f3(shared_config=cfg(tree["shared"] if shared is None else shared, enabled),
                   local_default=tree["input"], fs=fs, mutations=mutations)


@pytest.fixture
def contract_module():
    """A stand-in `video_analysis` exposing the REAL `CACHE_CONTRACT_VERSION`.

    The constant is AST-extracted from the shipped `video_analysis.py`, so the contract generation
    this panel is tested against is the production one rather than a literal retyped here. The
    real module cannot be imported on a bare interpreter, which is precisely why the panel reads
    the constant through a deferred import and treats its absence as "that axis was not
    assessed".
    """
    path = os.path.join(_SRC, "video_analysis.py")
    with open(path, "r", encoding="utf-8") as fh:
        va = ast.parse(fh.read())
    value = None
    for node in va.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "CACHE_CONTRACT_VERSION"):
            value = ast.literal_eval(node.value)
    assert isinstance(value, str) and value, "CACHE_CONTRACT_VERSION was not found as a literal"
    module = types.ModuleType("video_analysis")
    module.CACHE_CONTRACT_VERSION = value
    saved = sys.modules.get("video_analysis")
    sys.modules["video_analysis"] = module
    try:
        yield value
    finally:
        if saved is None:
            sys.modules.pop("video_analysis", None)
        else:
            sys.modules["video_analysis"] = saved


# ===========================================================================
# GROUP A — the default is OFF, and OFF means nothing is read
# ===========================================================================

def test_a1_the_checkbox_component_defaults_to_false():
    panel = _panel_with()
    box = next(n for n in ast.walk(panel) if isinstance(n, ast.Call)
               and ast.unparse(n.func) == "gr.Checkbox")
    value = next(kw for kw in box.keywords if kw.arg == "value")
    assert isinstance(value.value, ast.Constant), (
        f"the record checkbox's value= is the expression {ast.unparse(value.value)!r}, not a "
        "literal. An evaluated expression cannot be read off the source, and a conditional here "
        "is how a box ships ticked while the file still reads 'value=False'.")
    assert value.value.value is False, f"it defaults to {value.value.value!r}"


def test_a2_the_handler_parameter_itself_defaults_to_false():
    node = _func(HANDLER)
    defaults = dict(zip([a.arg for a in node.args.args][-len(node.args.defaults):],
                        node.args.defaults))
    assert isinstance(defaults["inspect_records"], ast.Constant)
    assert defaults["inspect_records"].value is False
    assert isinstance(defaults["root_text"], ast.Constant)
    assert defaults["root_text"].value == "", (
        "a missing folder value must be the refusing one, not a path")


def test_a3_an_unticked_click_calls_the_record_reader_zero_times(tree, contract_module):
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        text = ns[HANDLER](tree["media"], False)
    assert text.strip()
    assert spy.calls == [], f"the record reader ran {len(spy.calls)} time(s) with the box unticked"
    assert ns["F3_RECORDS_OFF"] in text


def test_a4_an_unticked_click_opens_no_file_in_either_cache_folder(tree):
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree)
        ns[HANDLER](tree["media"], False)
    assert fw.touching(tree["local"]) == [], fw.touching(tree["local"])
    assert fw.touching(tree["shared"]) == [], fw.touching(tree["shared"])
    assert fw.opened == [], fw.opened


@pytest.mark.parametrize("value", [1, "true", "false", "on", [], None, 0])
def test_a5_a_non_boolean_value_never_enables_the_record_pass(tree, value):
    """`inspect_records is not True`, not a truthy test. A stray value from anywhere must not be
    able to start a filesystem pass the operator did not ask for."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        text = ns[HANDLER](tree["media"], value)
    assert spy.calls == [], f"{value!r} enabled the record pass"
    assert ns["F3_RECORDS_OFF"] in text


def test_a6_ticking_the_box_registers_no_change_listener_that_could_read(tree):
    """Neither new widget may register an event of its own: naming a folder or ticking a box must
    not itself reach a disk, so only a deliberate Build click inspects anything."""
    registrations = [ast.unparse(n.func) for n in ast.walk(_tree())
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and getattr(n.func.value, "id", None) in (FOLDER_BOX, RECORDS_BOX)]
    assert registrations == [], registrations


# ===========================================================================
# GROUP B — the root selection REFUSES rather than guessing
# ===========================================================================

_REFUSED_ROOTS = ("", "   ", "Library", ".", "..", "~/Library", '"C:\\Library"',
                  "%USERPROFILE%\\Library", "C:Library", "\\Library", "C:\\Library\\",
                  "C:\\Library\\.", "C:\\Library.", "C:\\NUL")


@pytest.mark.parametrize("text", _REFUSED_ROOTS)
def test_b1_an_unusable_root_is_refused_with_no_contact_at_all(tree, text):
    fs = _FsSpy()
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree, fs=fs)
        out = ns[HANDLER](text, False)
    assert ns["F3_ROOT_REFUSED"] in out, out
    assert fw.calls == [], fw.calls
    probing = [c for c in fs.calls if c[0] in ("listdir", "scandir", "walk", "realpath", "isdir",
                                               "stat", "lstat", "open")]
    assert probing == [], probing


def test_b2_the_working_directory_is_never_probed_by_a_blank_field(tree):
    """`os.path.abspath('')` is the process working directory. Inspecting the application's own
    folder because a box was empty is a defect this repository has shipped before."""
    cwd = os.getcwd()
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree)
        ns[HANDLER]("", True)
    assert fw.touching(cwd) == [], fw.touching(cwd)


def test_b3_a_non_string_root_is_refused_rather_than_coerced(tree):
    ns = ns_for(tree)
    for value in (None, 7, [], {}):
        out = ns[HANDLER](value, False)
        assert ns["F3_ROOT_REFUSED"] in out, value


def test_b4_a_plain_absolute_folder_is_still_inspected(tree, contract_module):
    ns = ns_for(tree)
    out = ns[HANDLER](tree["media"], False)
    assert ns["F3_ROOT_REFUSED"] not in out
    assert a1.LIBRARY_ROLE in out
    assert "2 media files" in out, out


def test_b5_an_absent_folder_publishes_no_count_at_all(tree):
    """Not a zero. A2's own read-out says the inventory is incomplete and attaches no figure, and
    the panel must not improve on that by printing one."""
    ns = ns_for(tree)
    out = ns[HANDLER](tree["absent"], False)
    # A2 renders the word in upper case; `INVENTORY_INCOMPLETE` the constant is the lower-case
    # token. Both are checked, so neither a renamed constant nor a reworded line slips through.
    assert a1.INVENTORY_INCOMPLETE.upper() in out, out
    assert a1.INVENTORY_INCOMPLETE == "inventory_incomplete", a1.INVENTORY_INCOMPLETE
    assert "media files" not in out, out
    assert "no count for this folder is a total" in out, out


def test_b6_the_refusal_gate_is_the_one_a6_already_proved_equivalent():
    """Reused by identity rather than reimplemented. A second copy of A1's expansion rules would
    be two equivalence proofs to keep equal forever; `tests/test_library_cache_panel.py` already
    pins this one against `normalize_root_text` + `abspath` over a measured table."""
    sites = _call_sites("_a6_configured_path_is_unambiguous")
    assert len(sites) == 2, (
        f"{len(sites)} call sites; expected exactly two (the Media Library root, and the shared "
        "folder's own comparability check)")
    bodies = _f3_bodies()
    for forbidden in ("expandvars", "expanduser", "normalize_root_text"):
        assert forbidden not in bodies, (
            f"{forbidden} appears in the F-3 bodies, which means the gate is being reimplemented")


# ===========================================================================
# GROUP C — Discarded is OMITTED, not merely defaulted off
# ===========================================================================

def test_c1_the_scanner_is_always_told_not_to_include_the_rejected_root():
    call = _call_sites("inspect_library")
    assert len(call) == 1, f"{len(call)} inspect_library call sites; expected exactly one"
    kw = _kwargs(call[0])
    assert kw["include_rejected"] == "False", kw["include_rejected"]


def test_c2_the_rejected_role_is_never_offered_as_a_root():
    bodies = _f3_bodies()
    for forbidden in ("REJECTED_ROLE", "include_rejected=True", "clips_rejected"):
        assert forbidden not in bodies, f"{forbidden} appears in the F-3 bodies"
    source = _source()
    panel = ast.unparse(_panel_with())
    for word in ("discard", "rejected"):
        assert word not in panel.lower(), (
            f"the panel offers a {word!r} control; this increment omits it entirely")
    assert "F3_DISCARDED_OMITTED" in source


def test_c3_the_sibling_discarded_folder_is_never_inspected(tree):
    """It is a sibling of the library, holding one media file. Nothing in the read-out mentions
    it, no figure covers it, and no filesystem call reaches it in either mode."""
    for records in (False, True):
        with watch_fork_filesystem() as fw:
            ns = ns_for(tree)
            out = ns[HANDLER](tree["media"], records)
        assert ns["F3_DISCARDED_OMITTED"] in out
        assert a1.REJECTED_ROLE not in out, out
        assert tree["rejected"] not in out, out
        assert fw.touching(tree["rejected"]) == [], fw.touching(tree["rejected"])


def test_c4_only_one_root_is_ever_supplied_and_it_is_the_library_role(tree):
    """The structural half of c3: no second root exists to be included, whatever
    `include_rejected` says. The mutation control below is what proves the keyword matters too."""
    roots = ast.unparse(_call_sites("inspect_library")[0].args[0])
    assert "LIBRARY_ROLE" in roots, roots
    assert "REJECTED" not in roots and "rejected" not in roots, roots
    assert roots.count("(") >= 1 and roots.count(",") <= 2, (
        f"more than one root is supplied: {roots}")


def test_c5_the_include_rejected_control_fires(tree):
    """Negative control: the mutated keyword IS observable at the call, so test_c1 is measuring a
    live argument rather than a comment."""
    ns = ns_for(tree, mutations=(mutate_discarded_included,))
    captured: list = []
    real = a1.inspect_library

    def recorded(roots, **kw):
        captured.append(dict(kw))
        return real(roots, **kw)
    a1.inspect_library = recorded
    try:
        ns[HANDLER](tree["media"], False)
    finally:
        a1.inspect_library = real
    assert captured and captured[0]["include_rejected"] is True, (
        "the mutation did not reach the scanner, so test_c1 proves nothing")


# ===========================================================================
# GROUP D — the opt-in: ONE bounded local pass, and the bounds are the chosen ones
# ===========================================================================

def test_d1_the_opt_in_calls_the_record_reader_exactly_once_on_the_local_folder(
        tree, contract_module):
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        out = ns[HANDLER](tree["media"], True)
    assert len(spy.calls) == 1, f"{len(spy.calls)} record passes"
    directory, _kw = spy.calls[0]
    assert os.path.normcase(os.path.abspath(directory)) == \
        os.path.normcase(os.path.abspath(tree["local"])), directory
    assert "Analysis cache report" in out


def test_d2_the_five_bounds_that_reach_a3_are_exactly_the_chosen_ones(tree, contract_module):
    """Measured at the call, not asserted about the constants: a constant nobody passes is not a
    bound."""
    expected = {"max_entries": 4_000, "max_records": 2_000, "max_record_bytes": 1_000_000,
                "max_total_bytes": 32_000_000, "max_reported_names": 100}
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        ns[HANDLER](tree["media"], True)
    _directory, kw = spy.calls[0]
    for name, value in expected.items():
        assert kw[name] == value, f"{name} reached A3 as {kw[name]!r}, not {value!r}"
    defaults = a3.read_records.__kwdefaults__ if hasattr(a3.read_records, "__kwdefaults__") else {}
    for name, value in expected.items():
        if name in defaults and defaults[name] is not None:
            assert value <= defaults[name], (
                f"{name}={value} is not below A3's own default {defaults[name]}")


def test_d3_the_bound_keywords_are_module_constants_not_inline_literals():
    """A ceiling expressed as a widget bound would put a resource limit in the operator's hands,
    and would drag an upper-bound keyword into a block other suites slice raw source around."""
    kw = _kwargs(_call_sites("read_records")[0])
    for name in ("max_entries", "max_records", "max_record_bytes", "max_total_bytes",
                 "max_reported_names"):
        assert kw[name].startswith("F3_CACHE_"), f"{name}={kw[name]!r} is not an F-3 constant"
    panel = ast.unparse(_panel_with())
    for forbidden in ("gr.Number", "gr.Slider", "maximum", "minimum", "precision"):
        assert forbidden not in panel, f"{forbidden} appears in the panel"


def test_d4_the_media_root_keeps_a2_s_own_existing_per_root_ceiling():
    kw = _kwargs(_call_sites("inspect_library")[0])
    assert kw["max_entries"].endswith("MAX_INVENTORY_ENTRIES_PER_ROOT"), kw["max_entries"]
    assert kw["diagnostics"] == "False", (
        "a diagnostic walk is a recursive lower-bound pass and is out of scope for this panel")


def test_d5_a_reached_bound_is_named_and_withholds_the_totals(tree, contract_module):
    """Planted: a ceiling of one entry. The pass must name the bound and must not publish its
    partial counts as folder totals."""
    def tiny(name, node):
        if name != RECORD_FN:
            return node
        clone = ast.parse(ast.unparse(node)).body[0]
        for call in ast.walk(clone):
            if isinstance(call, ast.Call) and ast.unparse(call.func).endswith("read_records"):
                for kw in call.keywords:
                    if kw.arg == "max_entries":
                        kw.value = ast.Constant(1)
        return ast.fix_missing_locations(clone)

    ns = ns_for(tree, mutations=(tiny,))
    out = ns[HANDLER](tree["media"], True)
    assert a3.BOUND_ENTRIES in out, out
    assert "counts cover the whole folder: NO" in out, out
    assert "may be published as folder totals: NO" in out, out


def test_d6_the_contract_generation_is_passed_in_and_never_restated(tree, contract_module):
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        ns[HANDLER](tree["media"], True)
    _directory, kw = spy.calls[0]
    assert kw["contract"] == contract_module, kw["contract"]
    bodies = _f3_bodies()
    assert "stage5_cache_v" not in bodies, (
        "the contract generation is spelled out in gui.py, so this panel can drift from "
        "video_analysis.py's own constant")


def test_d7_an_unreadable_contract_leaves_that_axis_unassessed_not_zero(tree):
    """No `contract_module` fixture here, so the deferred import genuinely fails on a bare
    interpreter — which is the production behaviour whenever the runtime is absent."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        out = ns[HANDLER](tree["media"], True)
    _directory, kw = spy.calls[0]
    assert kw["contract"] is None
    assert ns["F3_RECORDS_CONTRACT_UNAVAILABLE"] in out
    assert "F belongs to a different analysis configuration" in out
    line = next(ln for ln in out.splitlines() if "F belongs to a different" in ln)
    assert "not assessed" in line, line


# ===========================================================================
# GROUP E — the shared folder, and the identity proof that keeps A3 off it
# ===========================================================================

def test_e1_the_shared_folder_is_never_contacted_in_either_mode(tree, contract_module):
    for records in (False, True):
        before = manifest(tree["root"])
        with watch_fork_filesystem() as fw:
            ns = ns_for(tree)
            out = ns[HANDLER](tree["media"], records)
        assert out.strip()
        assert fw.touching(tree["shared"]) == [], (records, fw.touching(tree["shared"]))
        assert manifest(tree["root"]) == before, records


def test_e2_a_local_cache_aliased_onto_the_shared_folder_is_refused(tmp_path, contract_module):
    """The one resolution that must be refused. Measured on a real Windows directory junction."""
    shared = tmp_path / "SharedCache"
    shared.mkdir()
    (shared / "deadbeef_fedcba9876543210fedcba98.json").write_text("{}", encoding="utf-8")
    inp = tmp_path / "input"
    inp.mkdir()
    link = inp / "video_analysis_cache"
    _junction(link, shared)

    with _RecordReaderSpy() as spy, watch_fork_filesystem() as fw:
        ns = load_f3(shared_config=cfg(str(shared), True), local_default=str(inp))
        out = ns[HANDLER](str(tmp_path), True)
    assert ns["F3_LOCAL_IS_SHARED"] in out, out
    assert spy.calls == [], "the record reader ran against a folder proven to be the shared one"
    assert fw.opened == [], fw.opened


def test_e3_the_alias_control_fires(tmp_path, contract_module):
    """Negative control for e2: with the identity proof removed the reader DOES reach the shared
    folder, so the assertion above is measuring a live guard."""
    shared = tmp_path / "SharedCache"
    shared.mkdir()
    (shared / "deadbeef_fedcba9876543210fedcba98.json").write_text("{}", encoding="utf-8")
    inp = tmp_path / "input"
    inp.mkdir()
    _junction(inp / "video_analysis_cache", shared)

    with _RecordReaderSpy() as spy:
        ns = load_f3(shared_config=cfg(str(shared), True), local_default=str(inp),
                     mutations=(mutate_shared_alias_allowed,))
        out = ns[HANDLER](str(tmp_path), True)
    assert ns["F3_LOCAL_IS_SHARED"] not in out
    assert len(spy.calls) == 1, (
        "the mutation did not make the reader run, so test_e2 proves nothing")


def test_e4_a_shared_pointing_handler_is_still_refused_by_the_identity_proof(
        tree, contract_module):
    """Independent control: the handler itself is mutated to compose the SHARED folder as the one
    to read. The proof, not the composition, is what stops it."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, mutations=(mutate_point_at_shared,))
        out = ns[HANDLER](tree["media"], True)
    assert ns["F3_LOCAL_IS_SHARED"] in out, out
    assert spy.calls == []


def test_e5_both_mutations_together_do_reach_the_shared_folder(tree, contract_module):
    """And with the proof removed as well, it reaches it — which is what makes e4's guard real."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, mutations=(mutate_point_at_shared, mutate_shared_alias_allowed))
        ns[HANDLER](tree["media"], True)
    assert len(spy.calls) == 1
    directory, _kw = spy.calls[0]
    assert os.path.normcase(os.path.abspath(directory)) == \
        os.path.normcase(os.path.abspath(tree["shared"]))


def _prefix_tree(tmp_path):
    """A local cache whose path has the configured shared path as a strict STRING prefix.

    ``…\\SharedCache\\VideoAnalysis`` is a strict prefix of
    ``…\\SharedCache\\VideoAnalysis2\\video_analysis_cache``, and so is ``…\\SharedCache``
    itself -- the realistic spelling, since a shared folder's parent commonly holds other things.
    Neither is the local cache folder, so neither may be mistaken for it.
    """
    shared = tmp_path / "SharedCache" / "VideoAnalysis"
    shared.mkdir(parents=True)
    (shared / "deadbeef_fedcba9876543210fedcba98.json").write_text("{}", encoding="utf-8")
    parent = tmp_path / "SharedCache" / "VideoAnalysis2"
    parent.mkdir()
    local = parent / "video_analysis_cache"
    local.mkdir()
    (local / "aabbccdd_0123456789abcdef01234567.json").write_text(
        '{"cache_contract": "stage5_cache_v3"}', encoding="utf-8")
    media = tmp_path / "Library"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"\0" * 2048)
    return {"shared": str(shared), "parent": str(parent), "local": str(local),
            "media": str(media), "above": str(tmp_path / "SharedCache")}


def test_e6_a_shared_folder_that_is_a_strict_prefix_is_not_mistaken_for_the_local_one(
        tmp_path, contract_module):
    """The DISCRIMINATING prefix case: the record pass must PROCEED, on the local folder.

    The previous version of this test built a look-alike fixture, never used it as the local
    cache, put the local cache somewhere unrelated, and ended in `assert inp` -- a tautology,
    since `dirname()` of an absolute path is never empty. The R1 independent review measured that
    a prefix-test mutation left the whole suite green (98/98). So this version makes the local
    cache path actually carry the configured shared path as a strict prefix, both spellings, and
    asserts the pass runs once on the intended folder.
    """
    fixture = _prefix_tree(tmp_path)
    for configured in (fixture["shared"], fixture["above"]):
        assert os.path.normcase(fixture["local"]).startswith(os.path.normcase(configured)), \
            configured
        assert os.path.normcase(fixture["local"]) != os.path.normcase(configured), configured
        with _RecordReaderSpy() as spy, watch_fork_filesystem() as fw:
            ns = load_f3(shared_config=cfg(configured, True), local_default=fixture["parent"])
            out = ns[HANDLER](fixture["media"], True)
        assert ns["F3_LOCAL_IS_SHARED"] not in out, (configured, out)
        assert len(spy.calls) == 1, (configured, out)
        directory, _kw = spy.calls[0]
        assert os.path.normcase(os.path.abspath(directory)) == \
            os.path.normcase(fixture["local"]), (configured, directory)
        # The instrument was live in THIS run -- it saw the local folder -- and the shared leaf
        # was never touched. Both halves matter: the first stops the second being vacuous.
        assert fw.touching(fixture["local"]), (configured, fw.calls[:5])
        assert fw.touching(fixture["shared"]) == [], (configured, fw.touching(fixture["shared"]))


def test_e6a_the_prefix_control_fires(tmp_path, contract_module):
    """Negative control for e6: as a prefix test, both spellings DO refuse a legitimate local
    cache, so the assertions above are measuring a live comparison."""
    fixture = _prefix_tree(tmp_path)
    for configured in (fixture["shared"], fixture["above"]):
        with _RecordReaderSpy() as spy:
            ns = load_f3(shared_config=cfg(configured, True), local_default=fixture["parent"],
                         mutations=(mutate_prefix_comparison,))
            out = ns[HANDLER](fixture["media"], True)
        assert ns["F3_LOCAL_IS_SHARED"] in out, (
            configured, "the mutation did not turn the comparison into a prefix test, so test_e6 "
            "proves nothing")
        assert spy.calls == [], configured


#: The R1 independent review (finding F-1) measured the panel's strongest sentence to be false
#: two ways. These are the shapes that sentence took. They are checked against the CONSTANT
#: VALUES loaded out of `gui.py`, not against this file's own text, so naming them here cannot
#: trip the check -- the "naming a ban trips it" trap, which this repository has hit repeatedly.
_UNCONDITIONAL_ISOLATION_CLAIMS = (
    "the shared folder is never read, listed or counted",
    "never reads, lists, counts or resolves the shared",
    "cannot contact the shared",
    "never contacts the shared",
)


def test_e16_the_unconditional_isolation_claim_cannot_come_back(tree, contract_module):
    """The user-facing texts must scope the guarantee to what the comparison establishes.

    Two measured counterexamples to an unconditional version: the MEDIA half enumerates whatever
    folder the operator types, shared folder included, with no alias involved at all; and the
    record half's check compares folder NAMES, so a configured shared text reaching this folder's
    storage under a different name is not refused (`test_e13`'s mirror image, reproduced in R2).
    So the texts may promise a scoped thing and must not promise a physical one.
    """
    ns = ns_for(tree)
    faces = {name: ns[name] for name in
             ("LABEL_LIBRARY_INVENTORY_RECORDS", "INFO_LIBRARY_INVENTORY",
              "F3_MEDIA_NAMED_FOLDER_NOTE", "F3_RECORDS_ALIAS_LIMIT")}
    for name, text in faces.items():
        assert isinstance(text, str) and text.strip(), name
        lowered = text.lower()
        for claim in _UNCONDITIONAL_ISOLATION_CLAIMS:
            assert claim not in lowered, (
                f"{name} carries {claim!r}, which R1 measured to be false; scope it to the record "
                "half and to a comparison of names")

    # And the scoped half is actually said, in both places an operator looks.
    assert "names" in faces["LABEL_LIBRARY_INVENTORY_RECORDS"].lower(), (
        "the checkbox label no longer warns that the check is on names")
    limit = faces["F3_RECORDS_ALIAS_LIMIT"].lower()
    for required in ("junction", "not storage", "untick"):
        assert required in limit, (required, limit[:200])
    media = faces["F3_MEDIA_NAMED_FOLDER_NOTE"].lower()
    assert "whichever folder that is" in media, media[:200]

    # Control: the comparison really does see the claim it is a guard for.
    planted = (faces["INFO_LIBRARY_INVENTORY"]
               + " The shared folder is never read, listed or counted.")
    assert [c for c in _UNCONDITIONAL_ISOLATION_CLAIMS if c in planted.lower()], (
        "the instrument cannot see the sentence it exists to keep out")


def test_e17_the_alias_limit_is_shown_exactly_when_the_pass_ran(tree, contract_module):
    """The residual is disclosed where the records are reported, and NOT where nothing was read.

    Printing it beside a refusal would be noise; omitting it beside a result would be the
    overclaim again, one layer down.
    """
    ns = ns_for(tree)
    ran = ns[HANDLER](tree["media"], True)
    assert ns["F3_RECORDS_ALIAS_LIMIT"] in ran, ran
    off = ns[HANDLER](tree["media"], False)
    assert ns["F3_RECORDS_ALIAS_LIMIT"] not in off, off
    assert ns["F3_RECORDS_OFF"] in off
    refused = ns_for(tree, shared="%SHARED_CACHE%\\VideoAnalysis")
    out = refused[HANDLER](tree["media"], True)
    assert refused["F3_SHARED_UNKNOWN_AMBIGUOUS"] in out
    assert refused["F3_RECORDS_ALIAS_LIMIT"] not in out, out

    # The media half's note is shown when a folder is inspected and withheld when it is refused.
    assert ns["F3_MEDIA_NAMED_FOLDER_NOTE"] in ran, ran
    blank = ns[HANDLER]("", False)
    assert ns["F3_MEDIA_NAMED_FOLDER_NOTE"] not in blank, blank
    assert ns["F3_ROOT_REFUSED"] in blank


def test_e7_a_superseded_snapshot_refuses_the_record_pass(tree, contract_module):
    """Fail-closed: after a Settings save this session, which folder is the shared one is no
    longer known, so "this is not the shared folder" is unprovable and nothing is read."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree)
        ns[A6_MARK_FN]()
        out = ns[HANDLER](tree["media"], True)
    assert ns["F3_SHARED_UNKNOWN_SUPERSEDED"] in out, out
    assert spy.calls == []
    assert ns["F3_LOCATION_CONFIGURED_SUPERSEDED"] in out, (
        "the location block still states a configured folder it knows to be stale")


def test_e8_an_ambiguous_shared_folder_refuses_the_record_pass(tree, contract_module):
    """Also fail-closed: a configured folder with no single absolute form cannot be compared
    without resolving it, and resolving it would mean contacting it."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, shared="%SHARED_CACHE%\\VideoAnalysis")
        out = ns[HANDLER](tree["media"], True)
    assert ns["F3_SHARED_UNKNOWN_AMBIGUOUS"] in out, out
    assert spy.calls == []


def test_e9_no_shared_folder_configured_still_permits_the_local_pass(tree, contract_module):
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, shared="")
        out = ns[HANDLER](tree["media"], True)
    assert len(spy.calls) == 1, out
    assert ns["F3_LOCATION_CONFIGURED_NONE"] in out


def test_e10_a_benign_local_junction_is_allowed_and_disclosed(tmp_path, contract_module):
    """A local cache pointed at a larger disk is a supported setup, so it is followed — and the
    resolution is DISCLOSED, because the records came off the resolved storage."""
    elsewhere = tmp_path / "BigDisk" / "Cache"
    elsewhere.mkdir(parents=True)
    (elsewhere / "aabbccdd_0123456789abcdef01234567.json").write_text("{}", encoding="utf-8")
    inp = tmp_path / "input"
    inp.mkdir()
    _junction(inp / "video_analysis_cache", elsewhere)

    with _RecordReaderSpy() as spy:
        ns = load_f3(shared_config=cfg(str(tmp_path / "SharedCache"), True),
                     local_default=str(inp))
        out = ns[HANDLER](str(tmp_path), True)
    assert len(spy.calls) == 1, out
    assert ns["F3_LOCAL_RESOLVED_ELSEWHERE"] in out, out


def test_e11_no_shared_configuration_file_is_read_and_no_creating_resolver_is_reached():
    bodies = _f3_bodies()
    for forbidden in ("load_config", "save_config", "config_path", "resolve_location",
                      "resolve_cache_location", "_analysis_cache_location", "import_records",
                      "measure", "makedirs", "mkdir", "remove", "rmdir", "unlink", "rename",
                      "replace", "shutil", "walk", "scandir", "open("):
        assert forbidden not in bodies, f"{forbidden} is reachable from the F-3 call graph"


def test_e12_the_shared_reach_instrument_can_actually_fail(tree):
    """Control for the whole of GROUP E: a deliberate listing of the shared folder IS seen by the
    instrument, so a clean result means something."""
    with watch_fork_filesystem() as fw:
        a3.read_records(tree["shared"], is_record_name=ac.is_record_name)
    assert fw.touching(tree["shared"]), "the instrument saw nothing; every GROUP E test is vacuous"


def _typed_arm_tree(tmp_path):
    """A local cache whose TYPED path is the configured shared folder, resolving elsewhere.

    The reparse point is on the LOCAL side, so `realpath` leads away from the configured folder
    and only the typed comparison can see the collision. A real junction, not a stub: the whole
    question is what `realpath` does with one.
    """
    elsewhere = tmp_path / "BigDisk" / "Cache"
    elsewhere.mkdir(parents=True)
    (elsewhere / "aabbccdd_0123456789abcdef01234567.json").write_text(
        '{"cache_contract": "stage5_cache_v3"}', encoding="utf-8")
    inp = tmp_path / "input"
    inp.mkdir()
    link = inp / "video_analysis_cache"
    _junction(link, elsewhere)
    media = tmp_path / "Library"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"\0" * 2048)
    return {"input": str(inp), "typed": os.path.abspath(str(link)),
            "elsewhere": str(elsewhere), "media": str(media)}


def test_e13_a_typed_local_path_that_is_the_shared_folder_is_refused(tmp_path, contract_module):
    """The TYPED arm of the comparison, which nothing else in this suite reached.

    The local cache path AS TYPED is exactly the configured shared folder, while the folder it
    resolves to is somewhere else entirely. The resolved arm therefore sees nothing, and the R1
    independent review measured that deleting `normcase(typed)` left the shipped suite GREEN.
    """
    fixture = _typed_arm_tree(tmp_path)
    assert os.path.normcase(os.path.realpath(fixture["typed"])) != \
        os.path.normcase(fixture["typed"]), (
            "the junction did not redirect, so the RESOLVED arm could refuse this on its own and "
            "the test would not isolate the typed one")
    with _RecordReaderSpy() as spy, watch_fork_filesystem() as fw:
        ns = load_f3(shared_config=cfg(fixture["typed"], True), local_default=fixture["input"])
        out = ns[HANDLER](fixture["media"], True)
    assert ns["F3_LOCAL_IS_SHARED"] in out, out
    assert spy.calls == [], "the record reader ran against the configured shared folder's own path"
    assert fw.opened == [], fw.opened
    # Under BOTH names: the typed one (which is the configured folder) and the storage it leads
    # to. The instrument records the path as it was asked for, so both have to be stated.
    assert fw.touching(fixture["typed"]) == [], fw.touching(fixture["typed"])
    assert fw.touching(fixture["elsewhere"]) == [], fw.touching(fixture["elsewhere"])


def test_e14_the_typed_arm_control_fires(tmp_path, contract_module):
    """Negative control for e13: with the typed arm deleted the records ARE read, off the storage
    the junction leads to, so the refusal above is a live guard rather than an accident."""
    fixture = _typed_arm_tree(tmp_path)
    with _RecordReaderSpy() as spy, watch_fork_filesystem() as fw:
        ns = load_f3(shared_config=cfg(fixture["typed"], True), local_default=fixture["input"],
                     mutations=(mutate_typed_arm_dropped,))
        out = ns[HANDLER](fixture["media"], True)
    assert ns["F3_LOCAL_IS_SHARED"] not in out, out
    assert len(spy.calls) == 1, (
        "the mutation did not make the reader run, so test_e13 proves nothing")
    # The instrument fired in THIS run, which is what makes its silence in e13 meaningful. It
    # records the path as asked for -- the junction -- not the storage `realpath` leads to, so
    # that is the name to look for.
    assert fw.touching(fixture["typed"]), (
        "the instrument saw no contact at all, so its silence in test_e13 would mean nothing")


def test_e15_the_resolved_arm_is_covered_too(tmp_path, contract_module):
    """The other half of the same comparison, so neither arm rests on the other's coverage.

    `test_e2` already measures a local cache junctioned ONTO the shared folder, which is the
    resolved arm. This states the pairing explicitly: with the typed arm deleted, `test_e2`'s
    case must STILL be refused.
    """
    shared = tmp_path / "SharedCache"
    shared.mkdir()
    (shared / "deadbeef_fedcba9876543210fedcba98.json").write_text("{}", encoding="utf-8")
    inp = tmp_path / "input"
    inp.mkdir()
    _junction(inp / "video_analysis_cache", shared)
    with _RecordReaderSpy() as spy:
        ns = load_f3(shared_config=cfg(str(shared), True), local_default=str(inp),
                     mutations=(mutate_typed_arm_dropped,))
        out = ns[HANDLER](str(tmp_path), True)
    assert ns["F3_LOCAL_IS_SHARED"] in out, out
    assert spy.calls == []


# ===========================================================================
# GROUP F — the observations deliberately NOT requested, each stated rather than implied
# ===========================================================================

def test_f1_the_delegated_transport_re_read_is_disabled(tree, contract_module):
    kw = _kwargs(_call_sites("read_records")[0])
    assert kw["looks_reusable"] == "None", kw["looks_reusable"]
    assert kw["read_text"] == "None", kw["read_text"]
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree)
        out = ns[HANDLER](tree["media"], True)
    records = [p for p in fw.opened if p.lower().endswith(".json")]
    assert records, "no record was opened at all, so 'never twice' would be vacuous"
    assert len(records) == len(set(os.path.normcase(p) for p in records)), records
    assert ns["F3_RECORDS_TRANSPORT_NOTE"] in out


def test_f2_the_transport_control_fires(tree, contract_module):
    """Negative control: requesting the delegated check DOES open each record a second time."""
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree, mutations=(mutate_transport_enabled,))
        ns[HANDLER](tree["media"], True)
    records = [os.path.normcase(p) for p in fw.opened if p.lower().endswith(".json")]
    assert len(records) > len(set(records)), (
        "the mutation did not cause a second read, so test_f1 proves nothing")


def test_f3_record_completeness_is_unassessed_rather_than_counted(tree, contract_module):
    kw = _kwargs(_call_sites("read_records")[0])
    assert kw["is_complete"] == "None" and kw["require_ai"] == "None"
    ns = ns_for(tree)
    out = ns[HANDLER](tree["media"], True)
    assert ns["F3_RECORDS_MODE_NOTE"] in out
    assert "_cache_entry_is_complete" not in _f3_bodies()


def test_f4_scope_is_assessed_only_from_positively_observed_roots(tree, contract_module):
    """The roots A2 observed are handed to A3 so "is this record's source inside the library?" can
    be put at all. A3 refuses the question for a root nobody probed, and that refusal is the one
    this panel relies on rather than reimplementing."""
    kw = _kwargs(_call_sites("read_records")[0])
    assert kw["inspection_roots"] == "verified_roots", kw["inspection_roots"]
    good = ns_for(tree)[HANDLER](tree["media"], True)
    assert "G describes a source outside the folders inspected" in good
    bad = ns_for(tree)[HANDLER](tree["absent"], True)
    assert ns_for(tree)["F3_RECORDS_SCOPE_UNASSESSED"] in bad, bad


def test_f5_a3_s_four_truthfulness_questions_are_all_published(tree, contract_module):
    """Four separate questions, deliberately not collapsed: enumeration, assessment, detail
    exhaustiveness and whether the counts cover the folder."""
    ns = ns_for(tree)
    out = ns[HANDLER](tree["media"], True)
    for phrase in ("every entry classified:", "every requested question answered:",
                   "counts cover the whole folder:", "detail lists exhaustive:",
                   "may be published as folder totals:"):
        assert phrase in out, phrase


# ===========================================================================
# GROUP G — no figure is authored here, and nothing unknown becomes a zero
# ===========================================================================

#: The lines A5 renders are excluded from the figure scan: they are the approved renderer, every
#: one of its numbers carries unit, exactness and population, and A5 is QUALIFIED as it stands.
def _panel_authored(text: str) -> list:
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("Analysis cache report"):
            inside = True
            continue
        if inside:
            if line and not line.startswith(" "):
                inside = False
            else:
                continue
        out.append(line)
    return out


_FABRICATION_PATTERNS = ("records:", "bytes:", " records", " bytes", "reclaim", "obsolete",
                         "safe to delete", "can be deleted", "free up")


@pytest.mark.parametrize("records", [False, True])
def test_g1_the_panel_authors_no_figure_of_its_own(tree, records, contract_module):
    """A2's and A5's read-outs own every number. What this panel writes is labels, states,
    refusals and words — so a figure appearing in a panel-authored line is a fabrication."""
    ns = ns_for(tree)
    text = ns[HANDLER](tree["media"], records)
    authored = _panel_authored(text)
    # A2's own rendered lines are indented under the Media Library heading and are its read-out.
    authored = [ln for ln in authored
                if not ln.startswith("  " + a1.LIBRARY_ROLE)
                and "folder:" not in ln
                and "media files" not in ln]
    for line in authored:
        low = line.lower()
        for pattern in _FABRICATION_PATTERNS:
            if pattern in low:
                # the standing caveats legitimately use these words in prose, never with a number
                assert not any(ch.isdigit() for ch in line), (
                    f"a panel-authored line carries both a figure word and a digit: {line!r}")


def test_g2_the_fabrication_scan_is_not_vacuous():
    """Control: the scan must reject a planted figure."""
    planted = ["  records: 0 obsolete records, safe to delete"]
    hits = [p for p in _FABRICATION_PATTERNS if p in planted[0].lower()]
    assert len(hits) >= 3, hits
    assert any(ch.isdigit() for ch in planted[0])


def test_g3_the_fabricated_zero_control_fires(tree, contract_module):
    """Negative control: a planted zero IS caught by test_g1's scan."""
    ns = ns_for(tree, mutations=(mutate_fabricated_zero,))
    text = ns[HANDLER](tree["media"], True)
    authored = [ln for ln in _panel_authored(text)
                if not ln.startswith("  " + a1.LIBRARY_ROLE)
                and "folder:" not in ln and "media files" not in ln]
    offending = [ln for ln in authored
                 if any(p in ln.lower() for p in _FABRICATION_PATTERNS)
                 and any(ch.isdigit() for ch in ln)]
    assert offending, "the planted zero was not caught, so test_g1 proves nothing"


def test_g4_no_bucket_is_summed_and_no_reclaim_total_is_offered(tree, contract_module):
    ns = ns_for(tree)
    text = ns[HANDLER](tree["media"], True)
    assert a5.OUT_OF_SCOPE_SENTENCE in text
    assert "No reclaimable-space figure is offered" in text
    bodies = _f3_bodies()
    for forbidden in ("reclaimable_total", "safe_to_delete", "sum(", "total =", "+ buckets"):
        assert forbidden not in bodies, f"{forbidden} appears in the F-3 bodies"


def test_g5_a_refusal_never_renders_as_an_empty_or_clean_folder(tree, contract_module):
    """Denied, absent, unproven and not-requested are four different findings, and none is a
    zero. The word 'empty' must not describe a folder this panel did not read."""
    cases = [
        (tree["media"], False, ns_for(tree)["F3_RECORDS_OFF"]),
        (tree["media"], True, None),
    ]
    for root, records, expected in cases:
        ns = ns_for(tree)
        text = ns[HANDLER](root, records)
        if expected:
            assert expected in text
        for forbidden in ("no records", "nothing in it", "cache is empty", "cache is clean"):
            assert forbidden not in text.lower(), forbidden


def test_g6_the_three_location_facts_are_three_separately_labelled_lines(tree, contract_module):
    ns = ns_for(tree)
    text = ns[HANDLER](tree["media"], True)
    assert ns["F3_LOCATION_CONFIGURED"].format(folder=tree["shared"]) in text
    assert ns["F3_LOCATION_SELECTED_NOT_INSPECTED"] in text
    assert ns["F3_LOCATION_LAST_RUN_UNAVAILABLE"] in text
    for phrase in ("NOT INSPECTED", "NOT AVAILABLE"):
        assert phrase in text


def test_g7_no_a4_observation_is_bound_beside_a_local_record_inventory():
    """Binding A4's SHARED selection next to a LOCAL record inventory would put two populations
    in one report — which A5 would either refuse or render as one subject. Neither is wanted."""
    sites = _call_sites("bind_observation")
    assert len(sites) == 1, f"{len(sites)} bind_observation call sites"
    kw = _kwargs(sites[0])
    assert set(kw) == {"inventory"}, kw
    bodies = _f3_bodies()
    for forbidden in ("inspect_location", "fork_cache_location", "CacheLocationReport",
                      "flat_measurement", "categories="):
        assert forbidden not in bodies, f"{forbidden} is reachable from the F-3 call graph"


def test_g8_the_report_names_no_configured_or_selected_folder_inside_a5_s_block(
        tree, contract_module):
    """A consequence of g7 worth measuring: with no location bound, A5's own identity block
    carries neither a configured nor a selected shared path."""
    ns = ns_for(tree)
    text = ns[HANDLER](tree["media"], True)
    block = text[text.index("Analysis cache report"):]
    assert "configured shared folder:" not in block, block[:400]
    assert "location a new invocation would select:" not in block, block[:400]


# ===========================================================================
# GROUP H — the whole pass modifies nothing, in every reachable combination
# ===========================================================================

@pytest.mark.parametrize("records", [False, True])
@pytest.mark.parametrize("root", ["media", "absent", "blank", "relative"])
def test_h1_no_filesystem_modification_under_any_path(tree, records, root, contract_module):
    """Before/after `(kind, size, mtime_ns)` manifest plus a recursive directory-set comparison —
    seam-independent, so no keyword-default binding trick can evade it."""
    text_for = {"blank": "", "relative": "Library"}
    selection = text_for.get(root, tree.get(root, ""))
    before = manifest(tree["root"])
    dirs_before = {d for d, m in before.items() if m[0] == "dir"}

    ns = ns_for(tree)
    out = ns[HANDLER](selection, records)
    assert out.strip()

    after = manifest(tree["root"])
    assert after == before, {k: (before.get(k), after.get(k))
                             for k in set(before) ^ set(after) or set()}
    assert {d for d, m in after.items() if m[0] == "dir"} == dirs_before


def test_h2_the_manifest_instrument_detects_a_real_modification(tree):
    """Control: an instrument that cannot see a change proves nothing about one."""
    before = manifest(tree["root"])
    probe = os.path.join(tree["local"], "instrument-probe.json")
    with open(probe, "w", encoding="utf-8") as fh:
        fh.write("{}")
    try:
        assert manifest(tree["root"]) != before
    finally:
        os.remove(probe)
    assert manifest(tree["root"]) == before


def test_h3_no_temporary_file_is_written_anywhere_the_handler_can_reach(tree, contract_module):
    fs = _FsSpy()
    with watch_fork_filesystem() as fw:
        ns = ns_for(tree, fs=fs)
        ns[HANDLER](tree["media"], True)
    writes = [c for c in fs.calls + fw.calls
              if c[0] in ("makedirs", "mkdir", "remove", "rmdir", "rename", "replace", "unlink")]
    assert writes == [], writes
    # Every file this pass opened already existed, so none of them was created by opening it.
    assert fw.opened, "nothing was opened at all, so this assertion would be vacuous"
    for path in fw.opened:
        assert os.path.exists(path), path


# ===========================================================================
# GROUP I — render and source-input isolation, and the disjoint call graph
# ===========================================================================

def _panel_with() -> ast.With:
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            rendered = ast.unparse(item.context_expr)
            if rendered.startswith("gr.Accordion(") and "LABEL_LIBRARY_INVENTORY" in rendered:
                return node
    raise AssertionError("the F-3 accordion was not found in gui.py")


def test_i1_the_registration_is_two_inputs_and_exactly_one_output():
    call = _click(BUTTON)
    kw = {k.arg: k.value for k in call.keywords}
    assert [ast.unparse(e) for e in kw["inputs"].elts] == [FOLDER_BOX, RECORDS_BOX]
    assert [ast.unparse(e) for e in kw["outputs"].elts] == [REPORT_BOX]
    assert ast.unparse(kw["fn"]) == HANDLER
    assert ast.unparse(kw["concurrency_limit"]) == "None"
    assert "concurrency_id" not in kw, (
        "a lane of one leaves a duplicate press QUEUED rather than letting the server answer it")
    assert ast.unparse(kw["show_progress"]) == "'hidden'"


def test_i2_the_inputs_list_matches_the_handler_parameter_order():
    """Gradio passes handler arguments positionally, so a silent reordering is a real defect."""
    params = [a.arg for a in _func(HANDLER).args.args]
    inputs = [ast.unparse(e) for e in
              {k.arg: k.value for k in _click(BUTTON).keywords}["inputs"].elts]
    assert params == ["root_text", "inspect_records"], params
    assert inputs == [FOLDER_BOX, RECORDS_BOX], inputs


def test_i3_the_panel_writes_no_existing_widget_and_joins_no_gate_collection():
    source = _source()
    for collection in ("source_outputs = [", "prep_outputs = ["):
        block = source[source.index(collection):]
        block = block[:block.index("]")]
        for widget in (BUTTON, FOLDER_BOX, RECORDS_BOX, REPORT_BOX):
            assert widget not in block, f"{collection.strip(' =[')} names {widget}"
    # Compared ELEMENT-WISE: `library_inventory_box` CONTAINS `library_inventory`, so a substring
    # check would be unsatisfiable against a correct implementation.
    written = {ast.unparse(e)
               for e in {k.arg: k.value for k in _click(BUTTON).keywords}["outputs"].elts}
    assert written == {REPORT_BOX}, written
    for forbidden in ("status_output", "video_output", "process_btn", "source_state", "prep_state",
                      "session_state", "render_invocation_state", "variation_seed",
                      "creative_preset", "prep_status", "prep_report", "prep_folder",
                      "shared_cache_folder", "shared_cache_status", "cache_report",
                      "library_cache_report_box", "clips_gallery"):
        assert forbidden not in written


def test_i4_the_panel_is_not_a_render():
    code = _f3_bodies()
    for forbidden in ("_RENDER_LOCK", "RENDER_CONCURRENCY_ID", "RenderLifecycle",
                      "_active_render_slot", "gr.State", "process_video", "process_video_guarded",
                      "analyze_beats_auto", "create_music_video", "get_processing_dir",
                      "_promote_output_no_replace", "live_declaration", "resolve_for_render",
                      "ScanCoordinator", "_CLIPS_TRANSFERS", "threading", "Thread"):
        assert forbidden not in code, f"{forbidden} reachable from the F-3 handler"
    block = _source()[_source().index(f"{BUTTON}.click("):]
    block = block[:block.index(")\n") + 2]
    assert "gr.State" not in block


def test_i5_the_panel_adds_no_subprocess_and_no_javascript_installer():
    tree_ = _tree()
    runs = [n for n in ast.walk(tree_)
            if isinstance(n, ast.Call) and ast.unparse(n.func) == "subprocess.run"]
    assert len(runs) == 2, (
        f"gui.py has {len(runs)} subprocess.run sites; the audited count is 2 and F-3 adds none")
    loads = [n for n in ast.walk(tree_)
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "app.load"]
    js = [n for n in loads if any(kw.arg in {"js", "_js"} for kw in n.keywords)]
    assert len(js) == 3, (
        f"{len(js)} JavaScript app.load registrations; the allowlist holds 3 and F-3 adds none")
    code = _f3_bodies()
    for forbidden in ("subprocess", "os.startfile", "Popen", "requests", "urllib"):
        assert forbidden not in code


def test_i6_the_four_widgets_are_the_whole_panel():
    panel = _panel_with()
    calls = [ast.unparse(n.func) for n in ast.walk(panel) if isinstance(n, ast.Call)]
    assert calls.count("gr.Accordion") == 1, calls
    assert calls.count("gr.Checkbox") == 1, calls
    assert calls.count("gr.Button") == 1, calls
    assert calls.count("gr.Textbox") == 2, calls
    for forbidden in ("gr.Radio", "gr.Slider", "gr.Number", "gr.DownloadButton", "gr.File",
                      "gr.UploadButton", "gr.Gallery", "gr.Dropdown", "gr.State"):
        assert forbidden not in calls, f"{forbidden} appeared inside the F-3 panel"
    assigned = {t.id for n in ast.walk(panel) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert assigned == {FOLDER_BOX, RECORDS_BOX, BUTTON, REPORT_BOX}, assigned
    rendered = ast.unparse(panel)
    for word in ("delete", "clean", "migrate", "rename", "repair", "clear", "sweep"):
        assert word not in rendered.lower(), f"the F-3 panel names a mutating action: {word}"


def test_i7_the_accordion_starts_closed_and_sits_in_the_approved_window():
    """Between the A6 drawer and the preparation drawer: the one position that disturbs no pinned
    region. Both halves measured on LINE numbers, and `open=` read off the PARSED keyword — a
    conditional there is how an accordion ships open while the file still reads `open=False`."""
    source = _source()

    def line_of(needle):
        return source[:source.index(needle)].count("\n") + 1

    tab = line_of("with gr.Tab(LABEL_SECTION_PREPARE_VERIFY")
    a6 = line_of("gr.Accordion(LABEL_LIBRARY_CACHE_REPORT")
    prep = line_of("gr.Accordion(label=LABEL_PREP_SECTION")
    panel = _panel_with().lineno
    assert tab < a6 < panel < prep, (
        f"the F-3 accordion (line {panel}) is outside the approved window "
        f"({tab} < {a6} < panel < {prep})")

    call = _panel_with().items[0].context_expr
    kw = next(k for k in call.keywords if k.arg == "open")
    assert isinstance(kw.value, ast.Constant), ast.unparse(kw.value)
    assert kw.value.value is False


def test_i8_the_wiring_sits_outside_both_pinned_windows():
    sample = _click("sample_btn").lineno
    clips = _click("clips_check_btn").lineno
    a6 = _click("library_cache_report_btn").lineno
    wiring = _click(BUTTON).lineno
    assert sample < a6 < wiring < clips, (
        f"F-3 wiring at line {wiring} is outside the approved window "
        f"({sample} < {a6} < wiring < {clips})")


def test_i9_the_two_call_graphs_are_disjoint_and_the_arrow_is_one_way():
    """A6 must never be able to reach this handler: it would gain the record reader its own bans
    forbid. This handler may read three PURE A6 helpers, and exactly three."""
    a6_names = ("_on_library_cache_report_click", "_a6_cache_report_lines", "_a6_state_sentence",
                "_a6_uninspected_lines") + A6_SHARED_HELPERS + (A6_MARK_FN,)
    a6_bodies = "\n".join(_code(_func(n)) for n in a6_names)
    reached_from_a6 = {ast.unparse(n).rsplit(".", 1)[-1]
                       for n in ast.walk(ast.parse(a6_bodies))
                       if isinstance(n, (ast.Name, ast.Attribute))}
    for name in F3_FUNCTIONS:
        assert name not in reached_from_a6, f"the A6 call graph reaches {name}"

    called = {ast.unparse(n.func).rsplit(".", 1)[-1] for n in ast.walk(ast.parse(_f3_bodies()))
              if isinstance(n, ast.Call)}
    assert set(A6_SHARED_HELPERS) <= called, sorted(called)
    other_a6 = {n for n in called if n.startswith("_a6_")} - set(A6_SHARED_HELPERS)
    assert not other_a6, f"the F-3 call graph reaches further A6 functions: {sorted(other_a6)}"


def test_i10_no_f3_helper_carries_the_a6_prefix():
    """`tests/test_library_cache_panel.py::test_k6` pins every `_a6_`-prefixed function into that
    suite's tuple, whose bans forbid the record reader and the library scanner outright. A helper
    carrying that prefix would therefore either enter A6's bans or fail that test."""
    declared = {n.name for n in ast.walk(_tree()) if isinstance(n, ast.FunctionDef)}
    for name in F3_FUNCTIONS:
        assert name in declared, name
        assert not name.startswith("_a6_"), name
    assert {n for n in declared if n.startswith("_a6_")} == set(A6_SHARED_HELPERS) | {
        A6_MARK_FN, "_a6_cache_report_lines", "_a6_state_sentence", "_a6_uninspected_lines"}, (
        "the set of _a6_ functions changed; A6's own tuple pin needs re-reading")


# ===========================================================================
# GROUP J — this change's own prose, because prose is executable here
# ===========================================================================

#: Tokens other suites slice RAW SOURCE on with `str.index`, or ban inside such a slice. Naming
#: any of them in this block's comments relocates somebody else's region boundary onto it. The
#: first eighteen are A6's measured list; the rest are the further gui.py needles an audit of
#: every `.index("...")` in `tests/` turned up near these two insertion points.
_SPAN_POISON = (
    "LABEL_PREP_SECTION", "prep_status = gr.Markdown", "clips_check_btn.click(",
    "LABEL_SECTION_VIDEO_REVIEW", "gr.Radio", "maximum=", "gr.Slider", "status_output",
    "source_outputs", "prep_outputs", "session_state", "cut_run_btn", "cut_report",
    "render_invocation_state", "variation_seed", "creative_preset", "_RENDER_LOCK",
    "RENDER_CONCURRENCY_ID", "video_output", "process_btn", "source_state",
    "INFO_PREP_SAMPLE", "sample_btn.click(", "sample_btn = gr.Button",
    "sample_report = gr.Textbox", "LABEL_PREP_SCAN", "prep_batch_size", "prep_state",
    "cache_inspect_btn.click(", "cache_clear_btn = gr.Button(", "cache_clear_btn.click(",
    "outputs=[cache_report]", "app.load(", "clips_scan_btn.click(",
    "gr.Tab(LABEL_TAB_MEDIA_LIBRARY", "gr.Tab(LABEL_TAB_SETTINGS",
    "gr.Tab(LABEL_SECTION_PREPARE_VERIFY",
)


def _f3_regions(source: str) -> list:
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


def test_j1_the_f3_comments_name_no_region_boundary_or_banned_token():
    source = _source()
    for region in _f3_regions(source):
        for token in _SPAN_POISON:
            assert token not in region, (
                f"the F-3 region names {token!r}, which another suite slices raw source on or "
                "bans inside such a slice. Describe it; do not spell it.")


def test_j2_the_regions_are_the_regions_this_change_introduced():
    source = _source()
    regions = _f3_regions(source)
    assert len(regions) == 2, regions
    panel_region, wiring_region = regions
    assert "gr.Accordion(LABEL_LIBRARY_INVENTORY" in panel_region
    assert FOLDER_BOX in panel_region and RECORDS_BOX in panel_region
    assert REPORT_BOX in panel_region
    assert f"{BUTTON}.click(" not in panel_region, "the panel region swallowed the wiring"
    assert f"{BUTTON}.click(" in wiring_region
    assert "gr.Accordion(" not in wiring_region, "the wiring region swallowed the panel"
    for region in regions:
        assert region in source, "a region is not a verbatim slice of gui.py"
        assert region.strip()


def test_j3_the_poison_guard_fires_on_a_token_planted_in_a_real_region():
    """A control on the guard's own comparison, not merely on the tuple."""
    clean = _f3_regions(_source())
    for region in clean:
        assert not [t for t in _SPAN_POISON if t in region], "the shipped region is already red"
    for token in ("LABEL_PREP_SECTION", "maximum=", "_RENDER_LOCK", "INFO_PREP_SAMPLE"):
        poisoned = clean[0] + f"\n# a planted mention of {token}\n"
        assert token in [t for t in _SPAN_POISON if t in poisoned], token


def test_j4_every_poison_token_really_is_sliced_or_banned_by_another_suite():
    """The tuple is only useful if its entries are real. Each is searched for in `tests/`, so an
    entry nobody slices on is removed rather than left as decoration."""
    found = {}
    for name in sorted(os.listdir(_HERE)):
        if not name.endswith(".py") or name == os.path.basename(__file__):
            continue
        with open(os.path.join(_HERE, name), "r", encoding="utf-8") as fh:
            body = fh.read()
        for token in _SPAN_POISON:
            if token in body:
                found.setdefault(token, name)
    missing = [t for t in _SPAN_POISON if t not in found]
    assert not missing, f"no suite mentions {missing}; they are decoration, not a guard"


# ===========================================================================
# GROUP K — the mutation table: every headline claim, mutated to its defect
# ===========================================================================

def test_k1_the_default_off_control_fires(tree, contract_module):
    """DEFECT: the default-off gate stops gating. GROUP A's zero-call assertions must go red."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, mutations=(mutate_records_always_on,))
        ns[HANDLER](tree["media"], False)
    assert len(spy.calls) == 1, (
        "the mutation did not make an unticked click read records, so GROUP A proves nothing")


def test_k2_the_root_gate_control_fires(tree, contract_module):
    """DEFECT: every root text is accepted. GROUP B's refusal assertions must go red."""
    ns = ns_for(tree, mutations=(mutate_root_gate_open,))
    out = ns[HANDLER]("", False)
    assert ns["F3_ROOT_REFUSED"] not in out, (
        "the mutation did not open the gate, so GROUP B proves nothing")


def test_k3_the_bounds_control_fires(tree, contract_module):
    """DEFECT: A3's own much larger defaults are used. test_d2 must go red."""
    with _RecordReaderSpy() as spy:
        ns = ns_for(tree, mutations=(mutate_bounds_widened,))
        ns[HANDLER](tree["media"], True)
    _directory, kw = spy.calls[0]
    assert kw["max_records"] == a3.MAX_CACHE_RECORDS != 2_000, kw["max_records"]
    assert kw["max_total_bytes"] == a3.MAX_TOTAL_RECORD_BYTES != 32_000_000


def test_k4a_the_gradio_6_keyword_regression_cannot_come_back():
    """A measured regression fix, pinned here because the suite could not have caught it.

    `gr.Textbox` has **no** `show_copy_button` parameter on the pinned `gradio==6.19.0` — Gradio 6
    replaced it with `buttons` — so passing it raises

        TypeError: Textbox.__init__() got an unexpected keyword argument 'show_copy_button'

    while `create_ui()` is building the interface. Measured against pristine `main`
    `2a7dcb01e0f1b247a4b8bd91033b799592aa8e89` (`src/gui.py` blob
    `171ab573a9d6b06ecf9d47c1dff997583952b30b`) on the real library: the whole application failed
    to start. It went unnoticed because `gradio` cannot be imported on a bare interpreter, so
    every existing assertion about that widget is an AST assertion about source text the real
    library rejects.

    This test is the bare-CPython half of the fix and is deliberately a **source** assertion,
    which is all a bare interpreter can offer. The other half is the isolated
    build-the-real-`Blocks` smoke, which is the only thing that can catch the next one of these.
    """
    # Asserted over the AST, on KEYWORD NAMES, never over raw source: the fix's own comment and
    # this docstring both have to name the removed parameter in order to explain it, and a
    # substring scan cannot tell an explanation from a violation. That is the "naming a ban trips
    # it" trap, and it has bitten this repository repeatedly — including on the first draft of
    # this very test.
    removed = "show_" + "copy_button"
    offenders = [(ast.unparse(n.func), kw.arg) for n in ast.walk(_tree())
                 if isinstance(n, ast.Call) and ast.unparse(n.func).startswith("gr.")
                 for kw in n.keywords if kw.arg == removed]
    assert offenders == [], (
        f"{offenders} pass a parameter the pinned gradio removed; it raises TypeError while the "
        "interface is being built, so the application does not start at all")
    textboxes = [n for n in ast.walk(_tree())
                 if isinstance(n, ast.Call) and ast.unparse(n.func) == "gr.Textbox"]
    assert textboxes, "no gr.Textbox call was found, so this test would prove nothing"
    copying = [n for n in textboxes if any(kw.arg == "buttons" for kw in n.keywords)]
    assert len(copying) == 2, (
        f"{len(copying)} read-out boxes offer a copy button; the two R14-A ones are expected")
    for node in copying:
        kw = next(k for k in node.keywords if k.arg == "buttons")
        assert ast.unparse(kw.value) == "['copy']", ast.unparse(kw.value)

    # Control: the same comparison, over a planted call, must flag it.
    planted = ast.parse(f"gr.Textbox(label='x', {removed}=True)")
    hits = [kw.arg for n in ast.walk(planted) if isinstance(n, ast.Call)
            for kw in n.keywords if kw.arg == removed]
    assert hits == [removed], "the instrument cannot see the defect it is a guard for"


#: Every in-memory AST mutator in this file, in definition order. TEN, since the R1 independent
#: review added the prefix-comparison and dropped-typed-arm defects that the shipped suite was
#: measured to survive. They mutate EXTRACTED function bodies in memory: `gui.py` is never
#: written to and therefore never needs restoring, which is the safer arrangement and must not be
#: described as a file-mutating harness.
MUTATORS = (mutate_records_always_on, mutate_shared_alias_allowed, mutate_prefix_comparison,
            mutate_typed_arm_dropped, mutate_root_gate_open, mutate_transport_enabled,
            mutate_bounds_widened, mutate_fabricated_zero, mutate_point_at_shared,
            mutate_discarded_included)


def test_k4_every_mutator_really_changes_the_shipped_function():
    """A mutator that silently no-ops turns its own control green for the wrong reason."""
    mutators = MUTATORS
    assert len(set(mutators)) == len(mutators) == 10, len(mutators)
    changed = 0
    for mutate in mutators:
        for name in F3_FUNCTIONS:
            original = _func(name)
            after = mutate(name, original)
            if ast.unparse(after) != ast.unparse(original):
                changed += 1
                break
        else:
            raise AssertionError(f"{mutate.__name__} changed no F-3 function at all")
    assert changed == len(mutators)
