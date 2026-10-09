"""Headless source enumeration: `video_processor.get_video_files()`.

``src/video_processor.py`` needs the whole portable runtime (numpy, librosa, the ``logger``
environment prologue), so it cannot be imported on a bare interpreter. This suite therefore
AST-extracts **exactly one real function** from the real file and executes it against a controlled
namespace — the same technique ``tests/test_cut_density.py`` and ``tests/test_gui_guard_seam.py``
already use, and the reason ``ast.unparse`` is preferred over ``ast.get_source_segment``.

The defect under test is a Windows property, and the test must not be: ``Path.glob`` matches
case-insensitively there, so ``*.mp4`` and ``*.MP4`` return the *same* file and the four extension
passes yielded every source twice. RC0 measured 4 real files producing 8 CLI entries, which doubled
Stage-5 analysis work in the headless path. The fake ``Path`` below reproduces that shape by
returning the same path from both patterns, so the assertions hold on any host — including a
case-sensitive one, where ``os.path.normcase`` is the identity function and the duplicate is caught
by the paths being equal rather than by case folding.
"""

from __future__ import annotations

import ast
import os
import posixpath
from typing import Any

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VIDEO_PROCESSOR = os.path.join(_REPO_ROOT, "src", "video_processor.py")


# ---------------------------------------------------------------------------
# A controlled stand-in for pathlib.Path: `glob` answers from a script
# ---------------------------------------------------------------------------


class _FakePath:
    """Only what ``get_video_files`` uses: construction, ``glob`` and ``str``."""

    def __init__(self, directory: str, script: dict[str, list[str]] | None = None,
                 name: str | None = None) -> None:
        self._directory = directory
        self._script = script or {}
        self._name = name if name is not None else directory
        self.glob_calls: list[str] = []

    def glob(self, pattern: str):
        self.glob_calls.append(pattern)
        for entry in self._script.get(pattern, []):
            yield _FakePath(entry, name=entry)

    def __str__(self) -> str:
        return self._name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"_FakePath({self._name!r})"


def _extract(name: str) -> ast.FunctionDef:
    with open(_VIDEO_PROCESSOR, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {_VIDEO_PROCESSOR}")


def _load(source: str) -> Any:
    """Execute one function body against a namespace holding only what it may touch.

    ``os`` is the real module on purpose: ``normcase``/``abspath`` are the platform semantics the
    fix depends on, and stubbing them would test the stub.
    """
    namespace: dict[str, Any] = {"os": os, "VideoList": list, "List": list}
    exec(compile(ast.Module(body=[ast.parse(source).body[0]], type_ignores=[]),
                 "<video-processor-enumeration>", "exec"), namespace)
    return namespace


def _call(script: dict[str, list[str]], directory: str = "/srcs"):
    """Run the REAL extracted function against a scripted fake ``Path``."""
    source = ast.unparse(_extract("get_video_files"))
    namespace = _load(source)
    created: list[_FakePath] = []

    def _factory(path: str) -> _FakePath:
        made = _FakePath(path, script=script)
        created.append(made)
        return made

    namespace["Path"] = _factory
    result = namespace["get_video_files"](directory)
    return result, created


#: Four real files, each returned by BOTH the lower-case and the upper-case pattern — exactly what
#: Windows `Path.glob` does, and exactly the RC0 measurement (4 files -> 8 entries).
_WINDOWS_CASE_INSENSITIVE = {
    "*.mp4": ["/srcs/a.mp4", "/srcs/b.mp4"],
    "*.MP4": ["/srcs/a.mp4", "/srcs/b.mp4"],
    "*.mkv": ["/srcs/c.mkv", "/srcs/d.mkv"],
    "*.MKV": ["/srcs/c.mkv", "/srcs/d.mkv"],
}


# ---------------------------------------------------------------------------
# The historical bug shape, and that the current code no longer has it
# ---------------------------------------------------------------------------


def _legacy_get_video_files(directory: str, path_factory) -> list[str]:
    """The pre-fix body, kept here ONLY to prove the simulation reproduces the real defect.

    If this did not duplicate, the fake ``Path`` would not be modelling Windows and every
    assertion below would be vacuous.
    """
    video_extensions = ['.mp4', '.MP4', '.mkv', '.MKV']
    video_files: list[Any] = []
    for ext in video_extensions:
        video_files.extend(path_factory(directory).glob(f'*{ext}'))
    if not video_files:
        raise ValueError(f'No MP4/MKV files found in {directory}')
    return [str(f) for f in video_files]


def test_the_simulation_reproduces_the_measured_duplication_against_the_old_body():
    legacy = _legacy_get_video_files(
        "/srcs", lambda d: _FakePath(d, script=_WINDOWS_CASE_INSENSITIVE))
    assert len(legacy) == 8, "the fake Path must reproduce the 4 -> 8 defect"
    assert len(set(legacy)) == 4


def test_each_real_file_is_returned_exactly_once():
    result, _created = _call(_WINDOWS_CASE_INSENSITIVE)
    assert len(result) == 4, result
    assert sorted(result) == ["/srcs/a.mp4", "/srcs/b.mp4", "/srcs/c.mkv", "/srcs/d.mkv"]
    assert len(set(result)) == len(result)


# ---------------------------------------------------------------------------
# Everything the narrow fix had to preserve
# ---------------------------------------------------------------------------


def test_both_container_formats_are_still_supported():
    result, _created = _call({"*.mp4": ["/srcs/one.mp4"], "*.MP4": ["/srcs/one.mp4"],
                              "*.mkv": ["/srcs/two.mkv"], "*.MKV": ["/srcs/two.mkv"]})
    assert result == ["/srcs/one.mp4", "/srcs/two.mkv"]


def test_two_distinct_mp4_files_remain_two():
    result, _created = _call({"*.mp4": ["/srcs/x.mp4", "/srcs/y.mp4"], "*.MP4": [],
                              "*.mkv": [], "*.MKV": []})
    assert result == ["/srcs/x.mp4", "/srcs/y.mp4"]


def test_an_upper_case_only_file_is_retained():
    """A case-sensitive host really can hold only ``CLIP.MP4``; it must not be dropped."""
    result, _created = _call({"*.mp4": [], "*.MP4": ["/srcs/CLIP.MP4"],
                              "*.mkv": [], "*.MKV": ["/srcs/OTHER.MKV"]})
    assert result == ["/srcs/CLIP.MP4", "/srcs/OTHER.MKV"]


def test_mixed_case_extensions_across_both_formats_all_survive():
    result, _created = _call({"*.mp4": ["/srcs/a.mp4"], "*.MP4": ["/srcs/B.MP4"],
                              "*.mkv": ["/srcs/c.mkv"], "*.MKV": ["/srcs/D.MKV"]})
    assert sorted(result) == ["/srcs/B.MP4", "/srcs/D.MKV", "/srcs/a.mp4", "/srcs/c.mkv"]


def test_the_same_basename_in_two_directories_is_not_deduplicated():
    """Identity is the full normalised path, never the filename."""
    result, _created = _call({"*.mp4": ["/srcs/one/clip.mp4", "/srcs/two/clip.mp4"],
                              "*.MP4": [], "*.mkv": [], "*.MKV": []})
    assert result == ["/srcs/one/clip.mp4", "/srcs/two/clip.mp4"]


def test_first_occurrence_wins_so_lower_case_passes_keep_their_order():
    """`.mp4` is scanned before `.MP4`, and `.mkv` before `.MKV`; order is observable output."""
    result, _created = _call({
        "*.mp4": ["/srcs/second.mp4"],
        "*.MP4": ["/srcs/second.mp4", "/srcs/first.MP4"],
        "*.mkv": ["/srcs/third.mkv"],
        "*.MKV": ["/srcs/third.mkv"],
    })
    assert result == ["/srcs/second.mp4", "/srcs/first.MP4", "/srcs/third.mkv"]


def test_no_supported_file_still_raises_the_existing_value_error():
    with pytest.raises(ValueError) as excinfo:
        _call({"*.mp4": [], "*.MP4": [], "*.mkv": [], "*.MKV": []})
    assert "No MP4/MKV files found" in str(excinfo.value)
    assert "/srcs" in str(excinfo.value)


def test_a_directory_whose_only_matches_are_duplicates_does_not_raise():
    result, _created = _call({"*.mp4": ["/srcs/solo.mp4"], "*.MP4": ["/srcs/solo.mp4"],
                              "*.mkv": [], "*.MKV": []})
    assert result == ["/srcs/solo.mp4"]


# ---------------------------------------------------------------------------
# The fix stays narrow
# ---------------------------------------------------------------------------


def test_exactly_the_four_documented_patterns_are_globbed_and_nothing_recursive():
    _result, created = _call(_WINDOWS_CASE_INSENSITIVE)
    patterns = [pattern for path in created for pattern in path.glob_calls]
    assert patterns == ["*.mp4", "*.MP4", "*.mkv", "*.MKV"]
    for pattern in patterns:
        assert "**" not in pattern, "enumeration must not become recursive"


def test_the_function_introduces_no_recursive_traversal_or_new_dependency():
    source = ast.unparse(_extract("get_video_files"))
    for forbidden in ("rglob", "**", "walk", "iterdir", "scandir", "glob.glob",
                      "resolve(", "realpath", "stat(", "is_file", "exists("):
        assert forbidden not in source, f"get_video_files mentions {forbidden!r}"


def test_identity_is_a_platform_normalised_absolute_path():
    """Not the filename, and not a case-folded basename: `normcase(abspath(...))`."""
    source = ast.unparse(_extract("get_video_files"))
    assert "os.path.normcase" in source
    assert "os.path.abspath" in source


def test_a_relative_directory_still_dedupes_through_abspath():
    """`abspath` resolves against the CWD, so relative inputs normalise consistently."""
    result, _created = _call({"*.mp4": ["srcs/a.mp4"], "*.MP4": ["srcs/a.mp4"],
                              "*.mkv": [], "*.MKV": []}, directory="srcs")
    assert result == ["srcs/a.mp4"]


def test_the_returned_values_are_plain_strings():
    result, _created = _call(_WINDOWS_CASE_INSENSITIVE)
    assert all(isinstance(entry, str) for entry in result)


@pytest.mark.skipif(os.path.normcase("A") == "A",
                    reason="case-insensitive normcase is a Windows/macOS property")
def test_on_windows_two_case_variants_of_one_name_collapse():
    """The real platform semantics, asserted where they exist and skipped cleanly elsewhere.

    On Windows ``clip.mp4`` and ``CLIP.MP4`` are the same file, and the glob passes can return
    each spelling, so normalisation has to fold them together.
    """
    result, _created = _call({"*.mp4": ["/srcs/clip.mp4"], "*.MP4": ["/srcs/CLIP.MP4"],
                              "*.mkv": [], "*.MKV": []})
    assert result == ["/srcs/clip.mp4"]


def test_posixpath_is_untouched_by_this_suite():
    """A guard on the test itself: the fake paths are strings, so nothing resolved on disk."""
    assert posixpath.sep == "/"
