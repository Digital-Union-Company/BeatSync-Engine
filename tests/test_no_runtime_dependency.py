"""The fork package must stay importable without the portable runtime.

Acceptance criterion for this slice: ``input_manager.py`` has no Gradio/browser dependency. More
broadly, the whole ``beatsync_fork`` package must import on a bare CPython — no gradio, numpy, cv2,
librosa or cupy, and none of the upstream modules that pull them in (``logger`` imports librosa at
module level and mutates ``PATH``/``CUDA_PATH`` as an import side effect; importing ``paths`` creates
directories on disk).

Checked in a subprocess so the assertion sees a clean module table rather than whatever the rest of
the suite has already imported.
"""

from __future__ import annotations

import os
import subprocess
import sys

FORBIDDEN = (
    # Heavy third-party runtime.
    "gradio",
    "numpy",
    "cv2",
    "librosa",
    "cupy",
    "numba",
    "PIL",
    # Upstream modules with import-time side effects.
    "logger",
    "paths",
    "gpu_cpu_utils",
    "ffmpeg_processing",
    "video_analysis",
    "video_processor",
    "ui_content",
    "auto_mode",
)

#: [R5-P69] The ONE module-scoped exception to the stdlib allowlist below.
#:
#: `ctypes` is deliberately NOT added to the shared `allowed` set: that would permit FFI in all 34
#: fork modules and discard the whole property a narrow grant buys. Instead exactly one named module
#: may import it, and `test_every_other_fork_module_still_rejects_ctypes` proves the exception did
#: not generalise.
#:
#: R5-P68 established by planted violation that the allowlist below is a real, already-effective
#: guard -- adding `import ctypes` to a new fork module failed this test before any exception
#: existed. So this is a carve-out from a working guard, not the introduction of one.
FFI_EXCEPTION_MODULE = "win32_transfer"
FFI_EXCEPTION_IMPORT = "ctypes"

#: [R5-P71 / F70-4] The dotted name other fork modules would import the FFI module by.
FFI_EXCEPTION_DOTTED = f"beatsync_fork.{FFI_EXCEPTION_MODULE}"

#: [R5-P71 / F70-4] Private names that are specifically the unrestricted-DLL surface. Listed
#: explicitly, in addition to the general "must be in PUBLIC_API" rule, so that the three names
#: that actually confer kernel32 capability are named in the failure message and in review.
FFI_RAW_LIBRARY_NAMES = ("_library", "_k32", "_bind", "_SIGNATURES")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO_ROOT, "src")

_PROBE = """
import importlib
import sys
import beatsync_fork
for name in {modules!r}:
    importlib.import_module("beatsync_fork." + name)
forbidden = {forbidden!r}
leaked = sorted(name for name in forbidden if name in sys.modules)
print("LEAKED:" + ",".join(leaked))
"""


def _fork_modules() -> list[str]:
    """Every module in the package, discovered — so a new fork module cannot escape this guard."""
    package = os.path.join(_SRC, "beatsync_fork")
    names = sorted(
        os.path.splitext(entry)[0]
        for entry in os.listdir(package)
        if entry.endswith(".py") and entry != "__init__.py"
    )
    assert names, "no fork modules discovered"
    return names


def _run_probe() -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = _SRC
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", _PROBE.format(forbidden=FORBIDDEN, modules=_fork_modules())],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        timeout=120,
        check=False,
    )


def test_fork_package_imports_without_the_portable_runtime():
    result = _run_probe()
    assert result.returncode == 0, f"import failed:\n{result.stderr}"


def test_fork_package_pulls_in_no_runtime_dependency():
    result = _run_probe()
    assert result.returncode == 0, f"import failed:\n{result.stderr}"

    line = next(
        (item for item in result.stdout.splitlines() if item.startswith("LEAKED:")),
        None,
    )
    assert line is not None, f"probe produced no verdict:\n{result.stdout}\n{result.stderr}"

    leaked = [name for name in line[len("LEAKED:"):].split(",") if name]
    assert leaked == [], f"beatsync_fork imported forbidden modules: {leaked}"


def test_fork_modules_import_only_stdlib():
    """Static check, so the guarantee holds even where a dependency happens not to be installed."""
    import ast

    allowed = {
        "__future__",
        # `copy` arrived with L2 V1: `stage_cache.py`'s defensive deep copying on both PUT and GET is
        # what stops two renders sharing one mutable bundle, so it is a contract rather than a
        # convenience (`.claude/rules/l2-stage-cache.md`). Still stdlib, still no runtime dependency.
        "copy",
        "hashlib",
        "json",
        "os",
        "random",
        "re",
        "stat",
        # `statistics` arrived with Director V2: `library_prep.media_summary_from_source_counts`
        # takes the median of the per-source candidate counts. Still stdlib, still no runtime
        # dependency — and deliberately not numpy, which remains forbidden for the fork package.
        "statistics",
        "subprocess",
        # `tempfile` arrived with Stage-5 A1: `qwen_partials.write_partial` publishes a per-job
        # partial through a UNIQUE same-directory `mkstemp` temp plus `fsync` and `os.replace`,
        # exactly as `video_analysis._save_cache` does. A shared `path + ".tmp"` is the measured D1
        # collision - one writer's `os.replace` published the other's payload while reporting
        # success - so using the stdlib primitive is a contract here rather than a convenience.
        # Still stdlib, still no runtime dependency.
        "tempfile",
        "threading",
        "time",
        "collections",
        # `concurrent` arrived with MEDIA-VERIFY-S2-A: `media_verify` runs its bounded FFprobe
        # probes through `concurrent.futures.ThreadPoolExecutor`, with the probe itself injected so
        # the module needs no runtime import to be tested. Still stdlib, still no runtime
        # dependency -- the same standard every other name on this list meets.
        "concurrent",
        "dataclasses",
        "enum",
        # `secrets` arrived with R5-P1: `clips_review` mints the opaque clip, page and session
        # identifiers the Clips Preview Selector hands the browser. It must be
        # `secrets.token_urlsafe`, not `random` -- `random` is already on this list and is NOT
        # cryptographically secure, so using it would make a clip id predictable and the whole
        # per-file authorization guessable. This is a security requirement rather than a
        # convenience. Still stdlib, still no runtime dependency.
        "secrets",
        # `html` arrived with R5-P1 for the same module: every value interpolated into a gallery
        # tile goes through `html.escape`, so no filename or label can carry markup or script into
        # the page. A hand-rolled escaper would be strictly worse than the stdlib one.
        "html",
        "math",
        "types",
        "typing",
    }
    # beatsync_fork itself is allowed: fork modules may build on each other, just not on the runtime.
    allowed = allowed | {"beatsync_fork"}
    # `ctypes` is NOT added here. See FFI_EXCEPTION_MODULE: it is granted per-module, below.
    assert FFI_EXCEPTION_IMPORT not in allowed, (
        "the FFI exception must stay module-scoped; adding it to the shared allowlist would permit "
        "ctypes in every fork module"
    )
    for module in _fork_modules():
        source_path = os.path.join(_SRC, "beatsync_fork", f"{module}.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source_path)

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])

        permitted = allowed
        if module == FFI_EXCEPTION_MODULE:
            permitted = allowed | {FFI_EXCEPTION_IMPORT}

        assert imported <= permitted, f"{module}: unexpected imports {sorted(imported - permitted)}"


def test_the_ffi_exception_is_exactly_one_named_module_that_exists():
    """The exception cannot quietly grow, and cannot outlive the module it was granted for."""
    granted = {FFI_EXCEPTION_MODULE}
    assert len(granted) == 1, "exactly one module may hold the FFI exception"
    assert FFI_EXCEPTION_MODULE in _fork_modules(), (
        "the FFI exception names a module that does not exist; remove the exception or restore "
        "the module"
    )


def test_every_other_fork_module_still_rejects_ctypes():
    """The carve-out must not generalise.

    Asserted over every DISCOVERED module rather than an enumerated list, so a module added later
    cannot escape it. This is the test §4 requires: it fails if `ctypes`, `ctypes.wintypes` or
    `from ctypes import ...` appears in any module but the one approved one.
    """
    import ast

    offenders = []
    for module in _fork_modules():
        if module == FFI_EXCEPTION_MODULE:
            continue
        source_path = os.path.join(_SRC, "beatsync_fork", f"{module}.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(a.name.split(".")[0] == FFI_EXCEPTION_IMPORT for a in node.names):
                    offenders.append(module)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                if node.module.split(".")[0] == FFI_EXCEPTION_IMPORT:
                    offenders.append(module)
    assert offenders == [], f"ctypes is only permitted in {FFI_EXCEPTION_MODULE}: {sorted(set(offenders))}"


#: [R5-P71 / F70-6] Dynamic *import* routes. Matched on either a bare name or an attribute, because
#: `importlib.import_module` is normally reached as an attribute. Unchanged from R5-P69.
_DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module"})

#: [R5-P71 / F70-6] Dynamic *execution* routes, which defeat every static check in this file by
#: moving the import into a string. R5-P70 measured `exec("import ctypes", ns)` passing the whole
#: suite (mutation M18).
#:
#: Matched as a **bare builtin name only**, never as an attribute. That is a correctness
#: requirement, not a loosening: `re.compile` is an attribute call appearing at 8 legitimate sites
#: in this package, and banning the attribute form would fail the suite on code that executes
#: nothing. `exec`/`eval`/`compile` as builtins are the dynamic-execution route; `<module>.compile`
#: is not.
_DYNAMIC_EXEC_BUILTINS = frozenset({"exec", "eval", "compile"})


def _dynamic_execution_offenders(tree, module: str) -> list[str]:
    """Every dynamic import or dynamic execution site in one module's AST."""
    import ast

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        # Dynamic import: bare name or attribute, as in R5-P69.
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name in _DYNAMIC_IMPORT_NAMES:
            offenders.append(f"{module}:{name}")
        # Dynamic execution: BARE BUILTIN ONLY. See _DYNAMIC_EXEC_BUILTINS.
        if isinstance(target, ast.Name) and target.id in _DYNAMIC_EXEC_BUILTINS:
            offenders.append(f"{module}:{target.id}")
    return offenders


def test_no_fork_module_reaches_an_import_dynamically():
    """Close the static blind spot R5-P68 measured, and the execution blind spot R5-P70 measured.

    `__import__('ctypes')` and `importlib.import_module('ctypes')` are not `ast.Import` nodes, so
    the allowlist above cannot see them, and the FORBIDDEN denylist misses `ctypes` too because it
    targets heavy runtime. With the allowlist now carved out for one module, that blind spot becomes
    load-bearing, so dynamic import is refused outright across the package.

    [R5-P71 / F70-6] `exec`, `eval` and `compile` are refused for the same reason: they relocate the
    import into a string where no static check in this file can see it.
    """
    import ast

    offenders = []
    for module in _fork_modules():
        source_path = os.path.join(_SRC, "beatsync_fork", f"{module}.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source_path)
        offenders.extend(_dynamic_execution_offenders(tree, module))
    assert offenders == [], f"dynamic imports are not permitted in the fork package: {sorted(offenders)}"


def test_the_dynamic_execution_guard_is_not_vacuous_PLANTED_CONTROL():
    """[R5-P71 / F70-6] M18 and friends must be caught, and `re.compile` must NOT be.

    The second half matters as much as the first: a guard that also flags `re.compile` would be
    caught instantly by the suite, but a guard written to avoid that by weakening the match could
    silently stop catching `exec`. Both directions are pinned here.
    """
    import ast

    caught = {
        # M18, verbatim from R5-P70's mutation matrix.
        "M18_exec": 'def probe():\n    ns = {}\n    exec("import ctypes", ns)\n    return ns["ctypes"]\n',
        "eval": 'def probe():\n    return eval("__import__(\'ctypes\')")\n',
        "compile": 'def probe():\n    return compile("import ctypes", "<s>", "exec")\n',
        "dunder_import": 'def probe():\n    return __import__("ctypes")\n',
        "import_module": 'import importlib\ndef probe():\n    return importlib.import_module("ctypes")\n',
    }
    for label, source in caught.items():
        offenders = _dynamic_execution_offenders(ast.parse(source), "planted")
        assert offenders, f"the dynamic-execution guard failed to flag {label}"

    permitted = {
        "re_compile": 'import re\n_RE = re.compile(r"\\A[a-z]+\\Z")\n',
        "method_named_eval": "def probe(engine):\n    return engine.eval(1)\n",
    }
    for label, source in permitted.items():
        offenders = _dynamic_execution_offenders(ast.parse(source), "planted")
        assert offenders == [], (
            f"the guard over-matched on {label}: {offenders}; an attribute call executes no string"
        )


# =================================================================================================
# [R5-P71 / F70-4] The FFI carve-out must not re-export unrestricted kernel32 package-wide.
# =================================================================================================

def _public_api_of_the_ffi_module() -> set:
    """Read `PUBLIC_API` out of the FFI module's source by AST.

    Read statically rather than imported so this guard behaves identically on a non-Windows
    interpreter and needs no `ctypes` of its own.
    """
    import ast

    source_path = os.path.join(_SRC, "beatsync_fork", f"{FFI_EXCEPTION_MODULE}.py")
    with open(source_path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=source_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PUBLIC_API":
                    if isinstance(node.value, (ast.Tuple, ast.List)):
                        return {element.value for element in node.value.elts
                                if isinstance(element, ast.Constant)}
    raise AssertionError(
        f"{FFI_EXCEPTION_MODULE} declares no PUBLIC_API tuple; the approved primitive interface "
        "must be declared for this guard to distinguish it from raw DLL access"
    )


def _refers_to_the_ffi_module(node, aliases: set) -> bool:
    """True if an AST expression names the FFI module itself.

    Covers both spellings: a local alias bound by an import, and the fully dotted
    `beatsync_fork.win32_transfer` form that `import beatsync_fork.win32_transfer` leaves behind.
    """
    import ast

    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute) and node.attr == FFI_EXCEPTION_MODULE:
        return isinstance(node.value, ast.Name) and node.value.id == "beatsync_fork"
    return False


def _private_surface_offenders(tree, module: str, public: set) -> list[str]:
    """Every route by which one module reaches the FFI module's non-public surface.

    Four routes, because R5-P70's M16 is only the most obvious one:

      1. `from beatsync_fork.win32_transfer import <name>` where `<name>` is not in `PUBLIC_API`
         (this is M16 itself, and the `as` form is the same node).
      2. attribute access to a non-public name through an imported module alias.
      3. the fully dotted `beatsync_fork.win32_transfer.<name>` form.
      4. `getattr(<module>, ...)`, which hides the name from routes 2 and 3 entirely.
    """
    import ast

    offenders = []
    aliases: set = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FFI_EXCEPTION_DOTTED:
                    # `import a.b` binds `a`; `import a.b as c` binds `c`.
                    aliases.add(alias.asname if alias.asname else "beatsync_fork")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == FFI_EXCEPTION_DOTTED:
                for alias in node.names:
                    if alias.name not in public:
                        raw = " (RAW LIBRARY SURFACE)" if alias.name in FFI_RAW_LIBRARY_NAMES else ""
                        offenders.append(
                            f"{module}: imports non-public {alias.name!r} from "
                            f"{FFI_EXCEPTION_DOTTED}{raw}"
                        )
            elif node.module == "beatsync_fork":
                for alias in node.names:
                    if alias.name == FFI_EXCEPTION_MODULE:
                        aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _refers_to_the_ffi_module(node.value, aliases):
            if node.attr not in public:
                raw = " (RAW LIBRARY SURFACE)" if node.attr in FFI_RAW_LIBRARY_NAMES else ""
                offenders.append(
                    f"{module}: reaches non-public {node.attr!r} on {FFI_EXCEPTION_DOTTED}{raw}"
                )
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
            if node.args and _refers_to_the_ffi_module(node.args[0], aliases):
                offenders.append(
                    f"{module}: getattr on {FFI_EXCEPTION_DOTTED} hides the name from static review"
                )
    return offenders


def test_no_fork_module_reaches_the_private_win32_binding_surface():
    """The `ctypes` carve-out must not become a package-wide FFI re-export.

    R5-P70 F70-4 measured the gap: a planted fork module containing
    `from beatsync_fork.win32_transfer import _library` reached the whole of kernel32 **without
    importing ctypes and without touching `_bind`**, and the suite stayed green. The carve-out
    confined the literal `import ctypes` to one module while exporting the *capability* to all of
    them.

    R5-P71 closes it from both ends. The module no longer has a `_library` accessor at all — the
    loader lives inside `_bind` and nothing returns the live `WinDLL` — and this guard rejects
    reaching any non-public name by any of four routes.

    **What this is.** A narrowly auditable repository regression contract: it makes an unauthorized
    widening of the FFI surface fail a test and show up in a diff. **What it is not.** An
    access-control boundary. Python has none — a leading underscore prevents nothing at runtime,
    `getattr` with a computed string defeats any static scan, and this test is not a sandbox. It
    buys review visibility, which is what a regression guard is for.

    Note what is deliberately *permitted*: importing the approved primitive interface. A future
    authorized integration phase will need `open_source` and `SourceHandle`, so the rule is
    PUBLIC_API-based rather than a blanket ban on importing the module.
    """
    import ast

    public = _public_api_of_the_ffi_module()
    assert public, "PUBLIC_API is empty, so this guard would permit everything"
    for raw in FFI_RAW_LIBRARY_NAMES:
        assert raw not in public, (
            f"{raw!r} is the raw kernel32 surface and must never appear in PUBLIC_API"
        )

    offenders = []
    for module in _fork_modules():
        if module == FFI_EXCEPTION_MODULE:
            continue
        source_path = os.path.join(_SRC, "beatsync_fork", f"{module}.py")
        with open(source_path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=source_path)
        offenders.extend(_private_surface_offenders(tree, module, public))
    assert offenders == [], (
        "only the approved primitive interface may be imported from the FFI module: "
        f"{sorted(offenders)}"
    )


def test_the_private_surface_guard_catches_M16_and_its_variants_PLANTED_CONTROL():
    """[R5-P71 / F70-4] Each reach-the-library route must be flagged, and the public API must not be.

    M16 is reproduced verbatim from R5-P70's mutation matrix, then widened to the alias and
    indirect-import spellings the task requires. The permitted cases are as load-bearing as the
    rejected ones: a guard that also blocked `open_source` would make the authorized integration
    phase impossible, which is explicitly not the intent.
    """
    import ast

    public = _public_api_of_the_ffi_module()

    rejected = {
        # M16, verbatim.
        "M16": (
            "from beatsync_fork.win32_transfer import _library\n\n\n"
            "def unauthorized_delete(path):\n"
            '    return getattr(_library(), "DeleteFileW")\n'
        ),
        "M16_aliased_import": (
            "from beatsync_fork.win32_transfer import _library as _lib\n"
            "def probe():\n    return _lib()\n"
        ),
        "import_bind": "from beatsync_fork.win32_transfer import _bind\ndef probe():\n    return _bind\n",
        "import_k32": "from beatsync_fork.win32_transfer import _k32\n",
        "import_signatures": "from beatsync_fork.win32_transfer import _SIGNATURES\n",
        "dotted_attribute": (
            "import beatsync_fork.win32_transfer\n"
            "def probe():\n    return beatsync_fork.win32_transfer._k32\n"
        ),
        "module_alias_attribute": (
            "import beatsync_fork.win32_transfer as w32\n"
            "def probe():\n    return w32._bind\n"
        ),
        "from_package_attribute": (
            "from beatsync_fork import win32_transfer\n"
            "def probe():\n    return win32_transfer._k32\n"
        ),
        "getattr_on_module": (
            "import beatsync_fork.win32_transfer as w32\n"
            'def probe():\n    return getattr(w32, "_library")\n'
        ),
        "getattr_computed_on_module": (
            "from beatsync_fork import win32_transfer\n"
            "def probe(name):\n    return getattr(win32_transfer, name)\n"
        ),
        "private_handle_class": (
            "from beatsync_fork.win32_transfer import _Handle\n"
        ),
    }
    for label, source in rejected.items():
        offenders = _private_surface_offenders(ast.parse(source), "planted", public)
        assert offenders, f"the private-surface guard failed to flag {label}"

    permitted = {
        "approved_api": (
            "from beatsync_fork.win32_transfer import open_source, SourceHandle, Win32Refused\n"
            "def probe(path):\n"
            "    with open_source(path) as source:\n        return source.identity()\n"
        ),
        "approved_api_via_alias": (
            "import beatsync_fork.win32_transfer as w32\n"
            "def probe(path):\n    return w32.open_source(path)\n"
        ),
        "unrelated_module": (
            "from beatsync_fork import raw_cutter\n"
            "def probe():\n    return raw_cutter._UNSAFE\n"
        ),
    }
    for label, source in permitted.items():
        offenders = _private_surface_offenders(ast.parse(source), "planted", public)
        assert offenders == [], (
            f"the guard rejected {label}, which must stay available to an authorized integration: "
            f"{offenders}"
        )
