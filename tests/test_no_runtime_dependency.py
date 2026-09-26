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
        "hashlib",
        "json",
        "os",
        "stat",
        "subprocess",
        "threading",
        "time",
        "collections",
        "dataclasses",
        "enum",
        "types",
        "typing",
    }
    # beatsync_fork itself is allowed: fork modules may build on each other, just not on the runtime.
    allowed = allowed | {"beatsync_fork"}
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

        assert imported <= allowed, f"{module}: unexpected imports {sorted(imported - allowed)}"
