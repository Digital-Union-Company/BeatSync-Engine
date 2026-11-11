"""R14-R1: read-only GUI paths must not create the configured shared analysis-cache folder.

The defect. `analysis_cache.resolve_location` creates the configured folder when its parent exists
-- correct for an analysis, wrong for a read-out -- and `gui.create_ui()` builds the Cache
Management category list at construction time in order to NAME a shared folder in its description.
So merely launching the app materialised a configured shared cache folder before the operator had
started an analysis or pressed any Cache Management button. Measured on `2b414ae7` in a fresh
process with a synthetic configuration: imports alone created nothing, while
`_analysis_category_explanation()` and `create_ui()` each created the folder.

How this suite verifies it. `gui.py` and `video_analysis.py` cannot be imported on a bare
interpreter, so the **real** function bodies are AST-extracted and executed against a synthesised
namespace -- the technique `test_library_cache_panel.py` and `test_cut_density.py` already use. The
real `beatsync_fork` modules run underneath (`analysis_cache`, `cache_admin`,
`library_cache_location`), and the real `ui_content.py` constants are loaded by path, so the text
asserted here is the shipped text. The synthetic `video_analysis` module carries the **real**
extracted `resolve_cache_location`, which is what the GUI's local import resolves to.

What is asserted is *filesystem state*, not a mocked call: every case checks whether the directory
actually appeared. A mkdir spy is layered on top of that, because "the folder is absent" and "no
creation was even attempted" are different findings and the first alone would be satisfied by a
creation that failed for an unrelated reason.

Group F is the **negative control**: it rewrites the three fixed call sites back to the pre-fix
form and requires every read-only assertion to fail. A regression suite that cannot fail against
the original defect verifies nothing.

Sections:

  A. the construction seam -- the defect itself
  B. the other read-only read-outs (Inspect, Check, the notes)
  C. analysis-time creation is RETAINED
  D. truthfulness -- an absent folder is never reported present, empty or usable
  E. nothing else moved: local fallback, Stage-5 identity, upload retirement, create_ui's reach
  F. the negative control
"""

from __future__ import annotations

import ast
import builtins
import importlib.util
import json
import os
import sys
import types

import pytest

from beatsync_fork import analysis_cache as ac
from beatsync_fork import cache_admin as ca
from beatsync_fork import library_cache_location as a4

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")

#: The resolver, and the two GUI helpers that decide whether it may create anything.
RESOLVER = "resolve_cache_location"
LOCATION_FN = "_analysis_cache_location"
READONLY_FN = "_analysis_cache_location_readonly"

#: Every GUI function this suite executes. Order matters: later ones call earlier ones.
GUI_FUNCTIONS = (
    LOCATION_FN,
    READONLY_FN,
    "_cache_categories",
    "_analysis_cache_notes",
    "_analysis_category_explanation",
    "_on_inspect_cache",
    "_shared_cache_config_path",
    "_shared_cache_report",
    "_on_inspect_shared_cache",
)

#: The three read-only call sites the fix moved onto the refusing resolver. Group F rewrites
#: exactly these back, which is why they are named once here rather than per test.
FIXED_CALL_SITES = ("_cache_categories", "_analysis_cache_notes", "_on_inspect_shared_cache")


# ---------------------------------------------------------------------------
# source access
# ---------------------------------------------------------------------------

def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _body_src(path: str, name: str) -> str:
    """Executable body only -- docstrings stripped, so prose cannot satisfy an assertion."""
    node = _func(path, name)
    return "\n".join(
        ast.unparse(item) for item in node.body
        if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)))


def _ui_content() -> types.ModuleType:
    """The real shipped strings. `ui_content.py` has no imports at all, so this is a plain load."""
    spec = importlib.util.spec_from_file_location("_r1_ui_content", _UI_CONTENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UI = _ui_content()


# ---------------------------------------------------------------------------
# spies
# ---------------------------------------------------------------------------

class MkdirSpy:
    """Record every real directory-creation attempt, from any module, and still perform it.

    Patched onto the `os` module itself rather than into a namespace: `resolve_location` binds
    ``mkdir = os.makedirs if make_dirs is None else make_dirs`` **inside the call**, so this seam
    is what production actually resolves. A namespace-level `os` replacement would see only what
    the GUI body does directly and would miss the one call that matters.
    """

    def __init__(self):
        self.calls: list[str] = []
        self._saved: dict = {}

    def __enter__(self):
        for name in ("makedirs", "mkdir"):
            real = getattr(os, name)
            self._saved[name] = real
            setattr(os, name, self._record(name, real))
        return self

    def __exit__(self, *exc):
        for name, real in self._saved.items():
            setattr(os, name, real)
        return False

    def _record(self, name, real):
        def recorded(path, *a, **k):
            self.calls.append(os.path.normcase(os.path.abspath(str(path))))
            return real(path, *a, **k)
        return recorded

    def touching(self, needle: str) -> list[str]:
        key = os.path.normcase(os.path.abspath(needle))
        return [c for c in self.calls if c == key]


# ---------------------------------------------------------------------------
# the loader: REAL gui code over REAL fork modules and the REAL resolver
# ---------------------------------------------------------------------------

def _synthetic_video_analysis(local_default: str) -> types.ModuleType:
    """A `video_analysis` module holding the REAL extracted `resolve_cache_location`.

    The GUI reaches the resolver through a function-local `from video_analysis import ...`, which
    on a bare interpreter would raise and send `_analysis_cache_location` down its total fallback
    -- making every assertion here vacuous. So the genuine resolver body is executed into a module
    object and registered, and `test_e_gui_fallback_is_not_what_was_exercised` proves the real one
    was the one that ran.
    """
    module = types.ModuleType("video_analysis")
    module.__dict__.update({
        "os": os,
        "fork_analysis_cache": ac,
        "VIDEO_ANALYSIS_CACHE_DIR": local_default,
        "__builtins__": builtins,
    })
    exec(compile(ast.Module(body=[_func(_VA, RESOLVER)], type_ignores=[]),
                 "<video_analysis-r1>", "exec"), module.__dict__)  # noqa: S102
    return module


def _rewrite_to_pre_fix(node: ast.FunctionDef) -> ast.FunctionDef:
    """THE NEGATIVE CONTROL: put one function back on the creating resolver.

    `_analysis_cache_location_readonly()` -> `_analysis_cache_location()`, which is literally the
    pre-fix source. The rewrite is located structurally by callee name and the hit count is
    asserted, because a control that silently failed to find its call site would make the whole
    read-only group look verified when it is not.
    """
    clone = ast.parse(ast.unparse(node)).body[0]
    hits = 0
    for sub in ast.walk(clone):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                and sub.func.id == READONLY_FN):
            sub.func = ast.Name(id=LOCATION_FN, ctx=ast.Load())
            hits += 1
    assert hits == 1, (
        f"expected exactly one {READONLY_FN}() call in {node.name}; found {hits}. "
        "The negative control cannot be trusted against a call site it did not locate.")
    return ast.fix_missing_locations(clone)


def load_gui(*, local_default: str, processing_dir: str = "", poster_dir: str = "",
             retired_dir: str = "", pre_fix: bool = False) -> dict:
    """Execute the real GUI cache helpers against a controlled namespace."""
    namespace: dict = {
        "os": os,
        "get_input_dir": (lambda: local_default),
        "get_processing_dir": (lambda: processing_dir),
        "CLIPS_POSTER_DIR": poster_dir,
        "RETIRED_UPLOADS_DIR": retired_dir,
        "fork_cache": ca,
        "fork_analysis_cache": ac,
        "fork_cache_location": a4,
        "__builtins__": builtins,
    }
    for name in dir(UI):
        if name.isupper():
            namespace[name] = getattr(UI, name)

    body: list = []
    for name in GUI_FUNCTIONS:
        node = _func(_GUI, name)
        if pre_fix and name in FIXED_CALL_SITES:
            node = _rewrite_to_pre_fix(node)
        body.append(node)

    sys.modules["video_analysis"] = _synthetic_video_analysis(local_default)
    try:
        exec(compile(ast.Module(body=body, type_ignores=[]), "<gui-r1>", "exec"),
             namespace)  # noqa: S102
    finally:
        sys.modules.pop("video_analysis", None)
    # Re-register for the duration of the test: the GUI bodies import it lazily, per call.
    sys.modules["video_analysis"] = _synthetic_video_analysis(local_default)
    return namespace


@pytest.fixture(autouse=True)
def _drop_synthetic_module():
    yield
    sys.modules.pop("video_analysis", None)


# ---------------------------------------------------------------------------
# the four configuration shapes the gates name
# ---------------------------------------------------------------------------

class Tree:
    """One isolated layout: a local cache, and a configured shared folder in a chosen state."""

    def __init__(self, root, *, shared_state: str, enabled=True, config_text=None):
        self.root = str(root)
        self.local = os.path.join(self.root, "install", "input", "video_analysis_cache")
        self.local_parent = os.path.join(self.root, "install", "input")
        os.makedirs(self.local, exist_ok=True)
        self.parent = os.path.join(self.root, "shared_parent")
        self.shared = os.path.join(self.parent, "SharedAnalysisCache")
        if shared_state == "present":
            os.makedirs(self.shared, exist_ok=True)
        elif shared_state == "absent_live_parent":
            os.makedirs(self.parent, exist_ok=True)
        elif shared_state == "disconnected":
            self.parent = os.path.join(self.root, "NoSuchDrive", "nested")
            self.shared = os.path.join(self.parent, "SharedAnalysisCache")
        else:
            raise AssertionError(f"unknown shared_state {shared_state!r}")

        self.config = os.path.join(self.root, "cfg", "shared-config.json")
        os.makedirs(os.path.dirname(self.config), exist_ok=True)
        if config_text is None:
            with open(self.config, "w", encoding="utf-8") as fh:
                json.dump({"schema": ac.SCHEMA,
                           ac.CACHE_FOLDER: self.shared,
                           ac.CACHE_ENABLED: enabled}, fh)
        else:
            with open(self.config, "w", encoding="utf-8") as fh:
                fh.write(config_text)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    def build(**kwargs):
        built = Tree(tmp_path, **kwargs)
        monkeypatch.setenv(ac.ENV_CONFIG_PATH, built.config)
        return built
    return build


# ===========================================================================
# A. the construction seam -- the defect itself
# ===========================================================================

def test_a1_construction_seam_does_not_create_the_configured_folder(tree):
    """The reproduced defect, at the exact call `create_ui()` makes.

    `absent_live_parent` is the one shape that mattered: the resolver documents this case as
    "create it", which is right for an analysis and wrong for a description of one.
    """
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    assert not os.path.isdir(t.shared)
    with MkdirSpy() as spy:
        text = ns["_analysis_category_explanation"]()
    assert not os.path.isdir(t.shared), "building the category description created the folder"
    assert spy.touching(t.shared) == [], f"creation was attempted: {spy.calls}"
    assert t.shared in text, "the description must still NAME the configured shared folder"


def test_a2_construction_seam_still_warns_that_the_cache_is_shared(tree):
    """Removing the side effect must not remove the warning: the text is unchanged.

    This is the half a careless fix breaks -- resolving read-only but then reporting the local
    description would silently stop telling the operator that clearing costs every installation.
    """
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    text = ns["_analysis_category_explanation"]()
    assert text == UI.INFO_CACHE_ANALYSIS_SHARED.format(path=t.shared)
    assert "SHARED analysis cache" in text
    assert "not " + "just this one" in text


def test_a3_construction_seam_is_unchanged_when_the_folder_already_exists(tree):
    """Gate: GUI construction with an existing shared-cache folder. Nothing may differ."""
    t = tree(shared_state="present")
    ns = load_gui(local_default=t.local_parent)
    before = sorted(os.listdir(t.shared))
    with MkdirSpy() as spy:
        text = ns["_analysis_category_explanation"]()
    assert os.path.isdir(t.shared)
    assert sorted(os.listdir(t.shared)) == before
    assert spy.touching(t.shared) == []
    assert text == UI.INFO_CACHE_ANALYSIS_SHARED.format(path=t.shared)


def test_a4_repeated_construction_mutates_nothing(tree):
    """Gate: repeated GUI construction causes no unexpected filesystem mutation."""
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    before = _snapshot(t.root)
    with MkdirSpy() as spy:
        for _ in range(5):
            ns["_analysis_category_explanation"]()
            ns["_cache_categories"]()
            ns["_analysis_cache_notes"]()
    assert _snapshot(t.root) == before, "a read-only path mutated the tree"
    assert spy.calls == [], f"a read-only path attempted a mkdir: {spy.calls}"


def _snapshot(root: str) -> list[str]:
    out = []
    for current, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(current, root)
        out.append(rel)
        for name in sorted(filenames):
            out.append(f"{rel}/{name}:{os.path.getsize(os.path.join(current, name))}")
    return out


# ===========================================================================
# B. the other read-only read-outs
# ===========================================================================

def test_b1_cache_management_inspect_creates_nothing(tree):
    """`_on_inspect_cache`'s docstring has always said "creates nothing". Now it is true."""
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent, processing_dir=os.path.join(t.root, "proc"),
                  poster_dir=os.path.join(t.root, "poster"),
                  retired_dir=os.path.join(t.root, "retired"))
    with MkdirSpy() as spy:
        report = ns["_on_inspect_cache"]()
    assert not os.path.isdir(t.shared)
    assert spy.calls == [], f"Inspect attempted a mkdir: {spy.calls}"
    assert isinstance(report, str) and report.strip()


def test_b2_shared_cache_check_button_creates_nothing(tree):
    """The Settings "Check" button measures the resolved folder and must not conjure it."""
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    with MkdirSpy() as spy:
        report = ns["_on_inspect_shared_cache"]()
    assert not os.path.isdir(t.shared)
    assert spy.calls == [], f"Check attempted a mkdir: {spy.calls}"
    assert t.shared in report


def test_b3_inspect_and_clear_resolve_the_same_root(tree):
    """They share `_cache_categories`, so they cannot disagree about which folder is in play.

    If only one of the two had been moved onto the refusing resolver, the first click would have
    materialised the folder the other had just reported absent.
    """
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    roots = [c.root for c in ns["_cache_categories"]() if c.key == ca.ANALYSIS]
    assert roots == [t.shared]
    assert not os.path.isdir(t.shared)


# ===========================================================================
# C. analysis-time creation is RETAINED
# ===========================================================================

def test_c1_analysis_time_resolution_still_creates_the_folder(tree):
    """Gate: actual analysis-time cache creation remains possible.

    The real extracted resolver, called the way Stage 5 calls it -- no `make_dirs` -- against the
    same configuration the read-only tests above leave untouched.
    """
    t = tree(shared_state="absent_live_parent")
    va = _synthetic_video_analysis(t.local)
    assert not os.path.isdir(t.shared)
    location = va.resolve_cache_location(t.local)
    assert os.path.isdir(t.shared), "Stage 5 must still create the configured folder"
    assert location.path == t.shared
    assert location.source == ac.SOURCE_SHARED
    assert location.available is True


def test_c2_the_resolver_default_is_unchanged(tree):
    """`make_dirs=None` must mean "the real `os.makedirs`", not a second policy."""
    node = _func(_VA, RESOLVER)
    kwonly = {a.arg for a in node.args.kwonlyargs}
    assert kwonly == {"make_dirs"}, f"unexpected keyword-only arguments: {sorted(kwonly)}"
    assert all(d is not None and isinstance(d, ast.Constant) and d.value is None
               for d in node.args.kw_defaults), "make_dirs must default to None"
    assert "make_dirs=make_dirs" in _body_src(_VA, RESOLVER), \
        "the resolver must forward make_dirs verbatim rather than deciding for itself"


def test_c3_both_stage5_entry_points_still_ask_for_creation(tree):
    """Neither Stage-5 call site may acquire a `make_dirs`, or analysis would stop creating."""
    calls = [node for node in ast.walk(_tree(_VA))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == RESOLVER]
    assert len(calls) == 2, f"expected exactly two Stage-5 resolver calls, found {len(calls)}"
    for call in calls:
        assert call.args == [] and call.keywords == [], \
            "a Stage-5 resolver call passed an argument; it must take the creating default"


# ===========================================================================
# D. truthfulness -- absence is never reported as presence
# ===========================================================================

def test_d1_an_absent_folder_is_reported_absent_not_empty(tree):
    """Gate: never silently report a missing folder as present, empty or usable.

    This is the risk the fix introduces and must answer: with creation refused, the resolver still
    returns `available=True` for a folder that does not exist, so presence has to come from an
    independent measurement. It does -- and the shipped wording already says so.
    """
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent)
    report = ns["_on_inspect_shared_cache"]()
    assert "does not exist yet" in report
    assert "created the first time an analysis runs" in report
    assert "Records:" not in report, "an absent folder must not render a record count"


def test_d2_cache_management_calls_an_absent_folder_uncached(tree):
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent, processing_dir=os.path.join(t.root, "proc"),
                  poster_dir=os.path.join(t.root, "poster"),
                  retired_dir=os.path.join(t.root, "retired"))
    report = ns["_on_inspect_cache"]()
    analysis_line = [ln for ln in report.splitlines()
                     if ln.strip().startswith(UI.LABEL_CACHE_ANALYSIS)]
    assert analysis_line, f"no analysis line in:\n{report}"
    assert "nothing cached yet" in analysis_line[0]
    assert t.shared in report, "the read-out must still name the folder it is talking about"


def test_d3_disconnected_storage_reports_and_falls_back(tree):
    """Gate: missing or disconnected shared-cache storage. The parent rule is untouched."""
    t = tree(shared_state="disconnected")
    ns = load_gui(local_default=t.local_parent)
    with MkdirSpy() as spy:
        location = ns[READONLY_FN]()
        notes = ns["_analysis_cache_notes"]()
    assert location.path == t.local, "an unreachable folder must fall back to the local cache"
    assert location.source == ac.SOURCE_LOCAL
    assert location.available is False
    assert location.notice, "an unreachable configured folder must carry its reason"
    assert not os.path.isdir(t.shared)
    assert spy.calls == []
    assert any("not reachable" in line for line in notes)


def test_d4_invalid_configuration_falls_back_without_creating(tree):
    """Gate: invalid or unreadable cache configuration."""
    t = tree(shared_state="absent_live_parent", config_text="{ this is not json")
    ns = load_gui(local_default=t.local_parent)
    with MkdirSpy() as spy:
        location = ns[READONLY_FN]()
        notes = ns["_analysis_cache_notes"]()
    assert location.path == t.local
    assert location.source == ac.SOURCE_LOCAL
    assert not os.path.isdir(t.shared)
    assert spy.calls == []
    assert any("could not be read" in line for line in notes)


def test_d5_sharing_switched_off_is_not_a_problem_and_creates_nothing(tree):
    t = tree(shared_state="absent_live_parent", enabled=False)
    ns = load_gui(local_default=t.local_parent)
    with MkdirSpy() as spy:
        location = ns[READONLY_FN]()
    assert location.path == t.local
    assert location.requested is False
    assert location.notice == "", "sharing being off is a setting, not a fault"
    assert not os.path.isdir(t.shared)
    assert spy.calls == []


# ===========================================================================
# E. nothing else moved
# ===========================================================================

def test_e_gui_fallback_is_not_what_was_exercised(tree):
    """Guard against a vacuous suite: prove the REAL resolver ran, not the total fallback.

    `_analysis_cache_location` swallows any failure and returns a bare local `CacheLocation`. If
    the synthetic `video_analysis` module were missing, every assertion above would pass for the
    wrong reason -- so require a shared resolution that only the real resolver can produce.
    """
    t = tree(shared_state="present")
    ns = load_gui(local_default=t.local_parent)
    location = ns[READONLY_FN]()
    assert location.source == ac.SOURCE_SHARED
    assert location.configured == t.shared
    assert location.requested is True


def test_e_readonly_helper_uses_the_one_resolver_and_the_existing_refusal():
    """No second resolver, no second configuration store, no re-implemented refusal."""
    body = _body_src(_GUI, READONLY_FN)
    assert f"{LOCATION_FN}(make_dirs=" in body
    assert "fork_cache_location.RefuseDirectoryCreation()" in body
    assert "config_path" not in body and "load_config" not in body, \
        "the read-only helper must not read configuration for itself"
    assert "resolve_location" not in body, "it must go through the one GUI resolver"


def test_e_refusal_is_a_fresh_instance_per_call():
    """It REMEMBERS being asked, so a shared instance would accumulate across read-outs."""
    body = _body_src(_GUI, READONLY_FN)
    assert "RefuseDirectoryCreation()" in body, "the refusal must be constructed per call"
    assert len([n for n in ast.walk(_func(_GUI, READONLY_FN))
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "RefuseDirectoryCreation"]) == 1


def test_e_refusal_creates_nothing_but_records_the_request(tmp_path):
    """The injected refusal is positive evidence the hazard was reached and declined."""
    refusal = a4.RefuseDirectoryCreation()
    target = str(tmp_path / "never")
    refusal(target, exist_ok=True)
    assert refusal.attempts == [target]
    assert not os.path.isdir(target)


def test_e_local_fallback_path_is_unchanged():
    """Gate: no change in local fallback. The composed local default is the same expression."""
    body = _body_src(_GUI, LOCATION_FN)
    assert "os.path.join(get_input_dir(), 'video_analysis_cache')" in body
    assert "fork_analysis_cache.SOURCE_LOCAL" in body


def test_e_stage5_cache_identity_is_untouched():
    """Gate: no change in Stage-5 cache identity. `make_dirs` reaches no identity primitive."""
    for name in ("_video_signature", "_cache_path", "_qwen_config_token",
                 "_cache_entry_is_complete", "_checkpoint_cache", "_save_cache"):
        body = _body_src(_VA, name)
        assert "make_dirs" not in body, f"{name} mentions make_dirs"
    source = _tree(_VA)
    assigns = [n for n in source.body
               if isinstance(n, ast.Assign) and len(n.targets) == 1
               and getattr(n.targets[0], "id", "") in
               ("CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION")]
    found = {n.targets[0].id: n.value.value for n in assigns}
    assert found == {"CACHE_CONTRACT_VERSION": "stage5_cache_v3",
                     "ANALYSIS_VERSION": "auto_av_analysis_v8_llama_vulkan_batched"}, \
        f"a cache generation constant moved: {found}"


def test_e_upload_retirement_protection_is_unchanged():
    """Gate: no regression to the PR #100 `gradio_uploads` startup protection."""
    body = _body_src(_GUI, "cleanup_on_startup")
    assert "GRADIO_UPLOADS_DIR_NAME" in body
    assert "protected_dirs" in body
    protected = [line for line in body.splitlines() if "protected_dirs" in line and "=" in line]
    assert protected, "protected_dirs assignment not found"
    assert "GRADIO_UPLOADS_DIR_NAME" in "\n".join(protected), \
        "gradio_uploads must stay unconditionally protected"


def test_e_create_ui_reaches_the_cache_only_through_the_description():
    """`create_ui` must not acquire a second, unrefused route to the resolver."""
    node = _func(_GUI, "create_ui")
    names = [n.func.id for n in ast.walk(node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_analysis_category_explanation" in names
    assert LOCATION_FN not in names, "create_ui must not resolve the cache location directly"
    assert "_cache_categories" not in names, "create_ui must not build categories directly"
    assert names.count("_analysis_category_explanation") == 1


def test_e_save_and_import_keep_the_creating_resolver():
    """Deliberately NOT changed: these two establish and populate the folder on purpose.

    Import must be able to copy records into the destination, and Save is the operator explicitly
    configuring the folder. Pinned so the scope of this hotfix stays visible.
    """
    for name in ("_on_save_shared_cache", "_on_import_analysis_cache"):
        body = _body_src(_GUI, name)
        assert f"{LOCATION_FN}()" in body, f"{name} should keep the creating resolver"
        assert READONLY_FN not in body, f"{name} was changed by a hotfix that did not intend to"


# ===========================================================================
# F. the negative control
# ===========================================================================

@pytest.mark.parametrize("handler,kwargs", [
    ("_analysis_category_explanation", {}),
    ("_on_inspect_shared_cache", {}),
    ("_analysis_cache_notes", {}),
])
def test_f1_pre_fix_source_creates_the_folder(tree, handler, kwargs):
    """THE CONTROL. Put the call sites back and every read-only path creates the folder again.

    If this ever stops failing-as-the-defect, the assertions in groups A, B and D have stopped
    being able to detect the regression they exist for.
    """
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent, pre_fix=True)
    assert not os.path.isdir(t.shared)
    with MkdirSpy() as spy:
        ns[handler](**kwargs)
    assert os.path.isdir(t.shared), (
        f"the pre-fix {handler} did NOT create the folder, so the control proves nothing")
    assert spy.touching(t.shared), "the pre-fix path should have reached a real mkdir"


def test_f2_pre_fix_inspect_also_creates_the_folder(tree):
    """Inspect reaches the resolver through `_cache_categories`, which the control rewrote too."""
    t = tree(shared_state="absent_live_parent")
    ns = load_gui(local_default=t.local_parent, pre_fix=True,
                  processing_dir=os.path.join(t.root, "proc"),
                  poster_dir=os.path.join(t.root, "poster"),
                  retired_dir=os.path.join(t.root, "retired"))
    assert not os.path.isdir(t.shared)
    ns["_on_inspect_cache"]()
    assert os.path.isdir(t.shared)


def test_f3_the_control_is_a_real_rewrite_not_a_no_op():
    """The rewrite must actually change the source it claims to change."""
    for name in FIXED_CALL_SITES:
        original = _func(_GUI, name)
        assert f"{READONLY_FN}()" in ast.unparse(original), \
            f"{name} no longer calls {READONLY_FN}; the fix or the control has drifted"
        rewritten = ast.unparse(_rewrite_to_pre_fix(original))
        assert f"{READONLY_FN}()" not in rewritten
        assert f"{LOCATION_FN}()" in rewritten
