"""Cache inspection and clearing (R8 UX Refresh, Phase 8).

The feature is a delete button, so this suite is mostly about what it does **not** touch. Two
independent mechanisms keep it safe and both are asserted here:

1. **`cache_admin` never discovers a path.** Every root is handed to it, and every path it acts on
   is proven inside that root by `realpath` + `commonpath`. So the "never delete" list is enforced
   by construction rather than by a blocklist.
2. **`gui.py` only ever hands it four roots**, none of which is the operator's media, a finished
   render, a Media Library, the review journals or the saved preferences. That half is asserted in
   `test_gui_cache_management.py`-style fashion at the bottom, by AST over the real `gui.py`.

The most important single test here is
`test_a_file_another_process_holds_open_is_reported_and_kept`: the "do not clear files being used
by active jobs" rule is enforced by the filesystem refusing the delete, not by asking politely,
and a held file must also keep its parent directories alive rather than being swept away inside
one.
"""

from __future__ import annotations

import ast
import os

import pytest

from beatsync_fork import cache_admin


def _seed(root, relative, size):
    path = os.path.join(root, *relative.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    return path


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "posters"
    root.mkdir()
    _seed(str(root), "a.jpg", 1000)
    _seed(str(root), "sub/b.jpg", 2000)
    _seed(str(root), "sub/deeper/c.jpg", 3000)
    return cache_admin.Category(cache_admin.POSTERS, "Poster cache", "thumbnails", str(root))


# ---------------------------------------------------------------------------------------------
# format_bytes
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (0, "0 B"),
    (1, "1 B"),
    (999, "999 B"),
    (812_000, "812.0 KB"),
    (1_400_000_000, "1.4 GB"),
])
def test_sizes_are_readable(value, expected):
    assert cache_admin.format_bytes(value) == expected


@pytest.mark.parametrize("junk", [None, "", "abc", -5, [], {}])
def test_a_junk_size_never_raises(junk):
    """It renders a read-out inside a live Gradio event, so it has to answer something."""
    assert cache_admin.format_bytes(junk) == "0 B"


# ---------------------------------------------------------------------------------------------
# measure reads and nothing else
# ---------------------------------------------------------------------------------------------

def test_measuring_counts_every_file_recursively(cache):
    measured = cache_admin.measure(cache)
    assert measured.exists is True
    assert measured.files == 3
    assert measured.total_bytes == 6000
    assert measured.error == ""


def test_a_missing_cache_is_a_first_run_not_a_fault(tmp_path):
    """The app creates these lazily, so "no poster cache yet" must not look like an error."""
    absent = cache_admin.Category("x", "Absent", "", str(tmp_path / "never-created"))
    measured = cache_admin.measure(absent)
    assert measured.exists is False
    assert measured.files == 0
    assert measured.error == ""


def test_measuring_creates_nothing(tmp_path):
    absent = cache_admin.Category("x", "Absent", "", str(tmp_path / "never-created"))
    cache_admin.measure(absent)
    assert not (tmp_path / "never-created").exists(), "measuring created the folder"


def test_measuring_deletes_nothing(cache):
    cache_admin.measure(cache)
    remaining = sum(len(files) for _, _, files in os.walk(cache.root))
    assert remaining == 3


def test_the_expensive_category_is_marked_in_the_report(tmp_path):
    root = tmp_path / "analysis"
    root.mkdir()
    _seed(str(root), "a.json", 10)
    expensive = cache_admin.Category(cache_admin.ANALYSIS, "Analysis", "", str(root),
                                     expensive=True)
    text = "\n".join(cache_admin.report_lines([cache_admin.measure(expensive)]))
    assert "expensive to rebuild" in text


def test_the_report_totals_every_category(cache, tmp_path):
    other = tmp_path / "processing"
    other.mkdir()
    _seed(str(other), "clip.mp4", 4000)
    rows = [cache_admin.measure(cache),
            cache_admin.measure(cache_admin.Category("p", "Processing", "", str(other)))]
    text = "\n".join(cache_admin.report_lines(rows))
    assert "10.0 KB" in text, text        # 6000 + 4000
    assert "4 file(s)" in text, text


# ---------------------------------------------------------------------------------------------
# clear deletes inside the root, and only there
# ---------------------------------------------------------------------------------------------

def test_clearing_removes_the_files_and_prunes_the_empty_folders(cache):
    outcome = cache_admin.clear(cache)
    assert outcome.ok, outcome.failures
    assert outcome.files_removed == 3
    assert outcome.bytes_removed == 6000
    assert outcome.directories_removed == 2
    assert os.listdir(cache.root) == []


def test_the_root_itself_is_never_removed(cache):
    """The app expects it to exist; recreating it lazily everywhere would be a second rule."""
    cache_admin.clear(cache)
    assert os.path.isdir(cache.root)


def test_clearing_a_missing_cache_is_a_no_op_not_a_failure(tmp_path):
    absent = cache_admin.Category("x", "Absent", "", str(tmp_path / "never-created"))
    outcome = cache_admin.clear(absent)
    assert outcome.ok
    assert outcome.files_removed == 0
    assert not (tmp_path / "never-created").exists()


def test_a_file_another_process_holds_open_is_reported_and_kept(cache):
    """The "do not clear files being used by active jobs" rule, enforced by the filesystem.

    Two separate guarantees are checked, and the second is the one that is easy to lose: the held
    file survives, AND so do its parent directories. `os.rmdir` refuses a non-empty directory, so
    anything this function declined to delete keeps its parents alive instead of being swept away
    inside a recursive tree removal.
    """
    held_path = os.path.join(cache.root, "sub", "deeper", "c.jpg")
    with open(held_path, "rb"):
        outcome = cache_admin.clear(cache)

    assert outcome.files_removed == 2
    assert outcome.bytes_removed == 3000
    assert not outcome.ok, "a refused delete must be reported, not swallowed"
    assert any("c.jpg" in failure for failure in outcome.failures), outcome.failures
    assert os.path.isfile(held_path), "a file in use was deleted"
    assert os.path.isdir(os.path.dirname(held_path)), "its parent folder was removed under it"

    text = "\n".join(cache_admin.cleared_lines([outcome]))
    assert "could not delete" in text
    assert "left exactly where it was" in text


def test_the_clear_report_counts_real_removals_not_intentions(cache):
    outcome = cache_admin.clear(cache)
    text = "\n".join(cache_admin.cleared_lines([outcome]))
    assert "3 file(s)" in text
    assert "6.0 KB freed" in text


def test_nothing_selected_clears_nothing():
    assert "nothing was cleared" in "\n".join(cache_admin.cleared_lines([])).lower()


def test_clearing_never_follows_a_path_outside_the_root(tmp_path):
    """Containment is measured with `commonpath`, never a string prefix -- so a sibling folder
    whose name merely shares a prefix is a different folder."""
    inside = tmp_path / "cache"
    outside = tmp_path / "cache-evil"
    inside.mkdir()
    outside.mkdir()
    _seed(str(inside), "mine.jpg", 10)
    precious = _seed(str(outside), "not-mine.jpg", 10)

    category = cache_admin.Category("c", "Cache", "", str(inside))
    cache_admin.clear(category)
    assert os.path.isfile(precious), "a same-prefix sibling folder was cleared"


def test_the_module_uses_no_recursive_tree_removal():
    """`shutil.rmtree` is what made `cleanup_on_startup` dangerous. Files are removed one at a
    time and only empty directories are pruned, so a refusal cannot be swept over.

    Asserted over CALLS so the docstrings may name what they do not do.
    """
    import inspect

    tree = ast.parse(inspect.getsource(cache_admin))
    calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("shutil.rmtree", "os.replace", "shutil.move", "shutil.copytree"):
        assert forbidden not in calls, f"{forbidden} is reachable from the cache admin"


def test_the_walk_cannot_descend_a_link():
    """Measured previously in this repository: `os.walk` DOES descend an NTFS junction. A junction
    inside the poster cache would otherwise turn "clear some thumbnails" into "delete whatever it
    points at", so `followlinks` must be off and reparse-point entries must be pruned.
    """
    import inspect

    source = inspect.getsource(cache_admin)
    tree = ast.parse(source)
    walks = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func) == "os.walk"]
    assert walks, "no walk to check"
    for call in walks:
        keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
        assert keywords.get("followlinks") == "False", keywords
        assert keywords.get("topdown") == "True", \
            "pruning dirnames in place only works topdown"
    assert "_is_reparse" in source, "reparse-point entries are not detected"


# ---------------------------------------------------------------------------------------------
# The other half of the safety argument: which roots gui.py is willing to name
# ---------------------------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


def _gui_tree() -> ast.Module:
    with open(_GUI, encoding="utf-8") as handle:
        return ast.parse(handle.read())


class _BlankStrings(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            return ast.copy_location(ast.Constant(value=""), node)
        return node


def _code_only(node) -> str:
    """Executable source with every string literal blanked, docstrings included.

    The same helper, and the same reasoning, that `test_gui_tabs.py` and the Freestyle suites
    carry: the bans below are about *machinery*, and `_cache_categories`' own docstring
    legitimately names `CLIPS_STATE_DIR` in the sentence explaining why it is NOT used. A ban that
    the explanation of the ban cannot survive forces documentation to be written around a test.
    This file's first draft made exactly that mistake and failed on its own prose.
    """
    import copy

    return ast.unparse(_BlankStrings().visit(copy.deepcopy(node)))


def test_the_gui_offers_exactly_the_four_documented_categories():
    builder = next(n for n in ast.walk(_gui_tree())
                   if isinstance(n, ast.FunctionDef) and n.name == "_cache_categories")
    rendered = ast.unparse(builder)
    for key in ("fork_cache.POSTERS", "fork_cache.PROCESSING", "fork_cache.RETIRED_UPLOADS",
                "fork_cache.ANALYSIS"):
        assert key in rendered, f"{key} is not offered"
    assert rendered.count("fork_cache.Category(") == 4, \
        "the category set changed; a new clearable root must be reviewed deliberately"


def test_the_gui_never_names_a_protected_root():
    """The list that must never be cleared, asserted by its ABSENCE from the only function that
    chooses what may be cleared. `get_output_dir` and `get_render_output_dir` are finished videos;
    the audio/video input dirs are the operator's own media; `CLIPS_STATE_DIR` is the parent of
    the poster cache and holds the review decision journals; `PREFS_PATH` is the saved settings.
    """
    builder = _code_only(next(n for n in ast.walk(_gui_tree())
                              if isinstance(n, ast.FunctionDef)
                              and n.name == "_cache_categories"))
    for forbidden in ("get_output_dir", "get_render_output_dir", "get_audio_input_dir",
                      "get_video_input_dir", "PREFS_PATH", "PREFS_DIR",
                      "GRADIO_TEMP_DIR", "ROOT_DIR"):
        assert forbidden not in builder, f"{forbidden} was offered as a clearable cache"
    # The poster SUBDIRECTORY, never its parent -- the journals live in the parent.
    assert "CLIPS_POSTER_DIR" in builder
    assert "CLIPS_STATE_DIR" not in builder, \
        "pointing a category at the review state folder would delete the decision journals"


def test_clearing_requires_both_a_selection_and_an_explicit_confirmation():
    handler = next(n for n in ast.walk(_gui_tree())
                   if isinstance(n, ast.FunctionDef) and n.name == "_on_clear_cache")
    parameters = [a.arg for a in handler.args.args]
    assert parameters == ["selected", "confirmed"], parameters
    rendered = ast.unparse(handler)
    # Both guards return BEFORE anything is cleared.
    assert rendered.index("if not chosen") < rendered.index("fork_cache.clear")
    assert rendered.index("if not confirmed") < rendered.index("fork_cache.clear")


def test_an_unrecognised_category_name_clears_nothing():
    """Selection values arrive from the browser. They are MATCHED against the built list rather
    than used to construct a root, so an invented value names no directory.
    """
    handler = ast.unparse(next(n for n in ast.walk(_gui_tree())
                               if isinstance(n, ast.FunctionDef) and n.name == "_on_clear_cache"))
    assert "category.key in chosen" in handler, \
        "selection must be matched against the built categories, never trusted as a path"
    assert "not recognised" in handler


def test_neither_cache_event_writes_a_render_or_source_widget():
    """Inspecting or clearing a cache must not invalidate a confirmed source set or change what
    the next render would do."""
    source = open(_GUI, encoding="utf-8").read()
    start = source.index("cache_inspect_btn.click(")
    end = source.index(")", source.index("outputs=[cache_report]", start)) + 1
    second = source.index("cache_clear_btn.click(")
    block = source[start:end] + source[second:source.index(
        ")", source.index("outputs=[cache_report]", second)) + 1]
    for forbidden in ("source_outputs", "prep_outputs", "source_state", "process_btn",
                      "status_output", "video_output", "session_state", "creative_preset",
                      "_RENDER_LOCK", "RENDER_CONCURRENCY_ID"):
        assert forbidden not in block, f"a cache event reaches {forbidden}"
