"""The visible version and the running build id.

Two questions, deliberately answered by two different things: `FORK_VERSION` says *what* this is,
`beatsync_fork.build_identity` says *which local revision* it was loaded from. This suite is
organised around the ways that pairing can go wrong, and the ordering is by how badly each
failure would mislead an operator:

1. **the resolver's branches**, driven by an injected runner so every one of them is reachable on
   a bare interpreter with no `git` and no repository;
2. **real `git`**, in temporary repositories, because the interesting cases (a detached HEAD, a
   dirty tree, an app root nested inside somebody else's repository) are properties of `git`'s
   actual answers rather than of a mock's;
3. **the GUI seam**, by `ast` over real `src/gui.py` source — it cannot be imported on a bare
   interpreter;
4. **the pinned Gradio signatures**, read from an installed package without importing it, which
   is the one check that would have caught the `show_copy_button` class of defect at authoring
   time rather than at startup.

The single worst outcome for this feature is a header that states a commit confidently while the
running code is something else, so the assertions lean hard on that direction: a build that cannot
be established says `Build unknown`, a modified tree says so, and a modification state that was
not *measured* is reported as unverified rather than as clean.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_UI_CONTENT = os.path.join(_REPO_ROOT, "src", "ui_content.py")
_MODULE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "build_identity.py")

import beatsync_fork
from beatsync_fork import build_identity as fork_build

_COMMIT = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_OTHER = "0f1e2d3c4b5a69788796a5b4c3d2e1f098765432"


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

def _runner(responses, log=None):
    """A fake `git`. Anything not named answers `(False, '')` — the shape a real failure takes.

    Keyed by the exact argument tuple, so a test that stops exercising a command stops answering
    it too; a runner that returned success for everything would make most of these tests vacuous.
    """
    def run(root, args):
        if log is not None:
            log.append(tuple(args))
        return responses.get(tuple(args), (False, ""))
    return run


def _healthy(root, head=_COMMIT, status="", toplevel=None, status_ok=True):
    return {
        ("rev-parse", "--show-toplevel"): (True, toplevel if toplevel is not None else root),
        ("rev-parse", "HEAD"): (True, head),
        ("status", "--porcelain", "--untracked-files=no"): (status_ok, status),
        ("--version",): (True, "git version 2.0.0"),
    }


@pytest.fixture(scope="module")
def gui_source() -> str:
    with open(_GUI, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def gui_tree(gui_source: str) -> ast.Module:
    return ast.parse(gui_source)


@pytest.fixture(scope="module")
def module_tree() -> ast.Module:
    with open(_MODULE, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"no function {name}")


# ---------------------------------------------------------------------------------------------
# 1. The resolver's branches
# ---------------------------------------------------------------------------------------------

def test_a_clean_checkout_reports_its_own_commit(tmp_path):
    root = str(tmp_path)
    identity = fork_build.resolve(root, _runner(_healthy(root)))
    assert identity.known
    assert identity.commit == _COMMIT
    assert identity.modified is False
    assert identity.reason == ""
    # Eight characters is the display choice; the full id stays on the record for diagnostics.
    assert identity.short == _COMMIT[:8]
    assert len(identity.short) == fork_build.SHORT_LENGTH == 8
    assert identity.label() == _COMMIT[:8]


def test_a_modified_checkout_is_disclosed_rather_than_presented_as_clean(tmp_path):
    """The running code is not the commit it names, and the header has to say so."""
    root = str(tmp_path)
    identity = fork_build.resolve(root, _runner(_healthy(root, status=" M src/gui.py")))
    assert identity.modified is True
    assert identity.label() == f"{_COMMIT[:8]} ({fork_build.MODIFIED_NOTE})"
    assert "modified" in identity.label()


def test_an_unmeasured_modification_state_is_unverified_and_never_clean(tmp_path):
    """The third answer, and the reason it exists.

    If `status` cannot be run, the tree is not known to be clean — it is simply unmeasured.
    Collapsing that into `False` is the one direction that turns a modified build into a
    confident-looking clean one, which is exactly the lie this feature exists to prevent.
    """
    root = str(tmp_path)
    identity = fork_build.resolve(root, _runner(_healthy(root, status_ok=False)))
    assert identity.known, "a measurable commit must not be thrown away over the dirty check"
    assert identity.modified is None
    assert identity.modified is not False
    assert identity.label() == f"{_COMMIT[:8]} ({fork_build.UNVERIFIED_NOTE})"
    assert identity.label() != identity.short, "an unverified build must not render as clean"


def test_a_missing_git_executable_yields_build_unknown(tmp_path):
    identity = fork_build.resolve(str(tmp_path), _runner({}))
    assert not identity.known
    assert identity.commit == ""
    assert identity.reason == fork_build.REASON_NO_GIT
    assert identity.label() == fork_build.UNKNOWN_LABEL == "Build unknown"


def test_a_directory_with_no_git_metadata_is_distinguished_from_a_missing_git(tmp_path):
    """Both show `Build unknown`; the recorded reason still tells them apart, which is the
    difference between "this install is not a checkout" and "this machine has no git"."""
    identity = fork_build.resolve(str(tmp_path), _runner({("--version",): (True, "git 2")}))
    assert identity.reason == fork_build.REASON_NOT_A_REPOSITORY
    assert identity.label() == fork_build.UNKNOWN_LABEL


def test_an_unrelated_worktree_is_refused_rather_than_borrowed(tmp_path):
    """The defect this check exists for: a portable install dropped inside somebody else's
    repository would otherwise report that repository's commit as BeatSync's own."""
    root = str(tmp_path / "app")
    os.makedirs(root)
    identity = fork_build.resolve(root, _runner(_healthy(root, toplevel=str(tmp_path))))
    assert not identity.known
    assert identity.reason == fork_build.REASON_FOREIGN_WORKTREE
    assert _COMMIT not in identity.label()


def test_containment_is_not_accepted_in_either_direction(tmp_path):
    """Exact equality, never a prefix test — in both directions, because a prefix comparison
    written the other way round is just as wrong."""
    root = str(tmp_path / "BeatSync")
    os.makedirs(root)
    os.makedirs(tmp_path / "BeatSync-Engine-Other")
    for foreign in (str(tmp_path), str(tmp_path / "BeatSync-Engine-Other"),
                    os.path.join(root, "src")):
        identity = fork_build.resolve(root, _runner(_healthy(root, toplevel=foreign)))
        assert identity.reason == fork_build.REASON_FOREIGN_WORKTREE, foreign


def test_a_worktree_named_in_another_spelling_is_still_ours(tmp_path):
    """`--show-toplevel` answers with forward slashes on Windows while the root arrives with
    backslashes, so a textual comparison would reject every real checkout. Resolved and
    case-folded equality is what makes the check correct rather than merely strict.
    """
    root = str(tmp_path / "App")
    os.makedirs(root)
    spellings = [root.replace("\\", "/"), root + os.sep, os.path.join(root, ".", "")]
    if os.name == "nt":
        spellings.append(root.upper())
    for spelling in spellings:
        identity = fork_build.resolve(root, _runner(_healthy(root, toplevel=spelling)))
        assert identity.commit == _COMMIT, f"{spelling!r} was not recognised as the app root"


def test_a_root_that_is_not_a_directory_is_refused(tmp_path):
    missing = str(tmp_path / "absent")
    assert fork_build.resolve(missing, _runner(_healthy(missing))).reason \
        == fork_build.REASON_NO_ROOT
    for empty in ("", None):
        assert fork_build.resolve(empty, _runner({})).reason == fork_build.REASON_NO_ROOT


@pytest.mark.parametrize("answer", [
    "", "HEAD", "not a commit", _COMMIT[:8], _COMMIT + "0", _COMMIT[:-1] + "z",
    "fatal: ambiguous argument 'HEAD'", _COMMIT + "\nsecond line",
])
def test_an_answer_that_is_not_a_commit_is_refused(tmp_path, answer):
    """`git` answering with prose, an abbreviation or two lines is not an identity. A header is
    allowed to say nothing; it is not allowed to display something that is not a commit.
    """
    root = str(tmp_path)
    identity = fork_build.resolve(root, _runner(_healthy(root, head=answer)))
    assert not identity.known, f"{answer!r} was accepted as a commit"
    assert identity.reason == fork_build.REASON_NO_COMMIT
    assert identity.label() == fork_build.UNKNOWN_LABEL


def test_no_identity_is_ever_invented(tmp_path):
    """Across every failure mode: no commit, no abbreviation, and in particular never the
    upstream baseline — which is a real 40-character hex id sitting in the same package and would
    look entirely plausible in the header while answering a completely different question.
    """
    root = str(tmp_path)
    failures = [
        _runner({}),
        _runner({("--version",): (True, "git 2")}),
        _runner(_healthy(root, head="nonsense")),
        _runner(_healthy(root, toplevel=str(tmp_path.parent))),
    ]
    for runner in failures:
        identity = fork_build.resolve(root, runner)
        label = identity.label()
        assert identity.commit == ""
        assert identity.short == ""
        assert label == fork_build.UNKNOWN_LABEL
        assert beatsync_fork.UPSTREAM_BASELINE_COMMIT not in label
        assert beatsync_fork.UPSTREAM_BASELINE_COMMIT[:8] not in label
        assert beatsync_fork.FORK_VERSION not in label, \
            "a version is not a build id and must not stand in for one"
        assert not any(len(word) >= fork_build.SHORT_LENGTH
                       and all(c in "0123456789abcdef" for c in word.lower())
                       for word in label.split()), \
            f"something hex-shaped leaked into the unknown label: {label!r}"


def test_the_unknown_answer_is_deterministic(tmp_path):
    """Called twice with the same inputs it answers the same thing — no clock, no randomness, no
    counter. The record is a frozen value and compares by value."""
    root = str(tmp_path)
    first = fork_build.resolve(root, _runner({}))
    second = fork_build.resolve(root, _runner({}))
    assert first == second
    assert fork_build.resolve(root, _runner(_healthy(root))) \
        == fork_build.resolve(root, _runner(_healthy(root)))


def test_no_branch_name_is_ever_requested(tmp_path):
    """What makes a detached HEAD work by construction rather than by a special case. A
    `symbolic-ref` or `--abbrev-ref` here would answer the literal string `HEAD` when detached,
    and whatever was written next would be built on it.
    """
    root = str(tmp_path)
    log: list = []
    fork_build.resolve(root, _runner(_healthy(root), log))
    assert log, "no git command was issued at all"
    flat = " ".join(" ".join(args) for args in log)
    for forbidden in ("symbolic-ref", "abbrev-ref", "branch", "describe", "name-rev"):
        assert forbidden not in flat, f"the resolver asks git for {forbidden}"


def test_no_remote_is_ever_contacted(tmp_path):
    """The whole point of "running build": it is the LOCAL revision. A remote lookup would answer
    what is published, make startup depend on a network, and be wrong in exactly the case the
    feature exists for — a machine running something that was never pushed.
    """
    root = str(tmp_path)
    log: list = []
    fork_build.resolve(root, _runner(_healthy(root), log))
    flat = " ".join(" ".join(args) for args in log)
    for forbidden in ("fetch", "ls-remote", "remote", "pull", "push", "clone", "http"):
        assert forbidden not in flat, f"the resolver reaches the network via {forbidden}"


def test_the_module_imports_nothing_that_could_reach_a_network(module_tree):
    """Asserted over the module's imports as well as over its commands, because the next edit
    could add a lookup that no current test drives."""
    imported = set()
    for node in ast.walk(module_tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("urllib", "http", "socket", "ssl", "requests", "httpx", "ftplib",
                      "telnetlib", "xmlrpc", "webbrowser"):
        assert forbidden not in imported, f"build_identity imports {forbidden}"
    # And the fork package's hard rule, asserted here too rather than only package-wide.
    for runtime in ("logger", "paths", "gradio", "cupy", "cv2", "librosa", "numpy", "ui_content"):
        assert runtime not in imported, f"build_identity imports the upstream runtime: {runtime}"


def test_untracked_files_are_not_a_modified_build(tmp_path):
    """`bin/`, `input/` and `output/` are runtime directories; a fresh install is full of
    untracked content and is not a modified checkout. The flag is what buys that."""
    root = str(tmp_path)
    log: list = []
    fork_build.resolve(root, _runner(_healthy(root), log))
    status = [args for args in log if args and args[0] == "status"]
    assert status == [("status", "--porcelain", "--untracked-files=no")], status


def test_every_git_call_is_bounded_windowless_and_read_only(module_tree):
    """The real runner is not exercised by the injected-runner tests above, so its argv and its
    bounds are asserted structurally instead — and `_run_git` is the ONLY `subprocess` site.
    """
    runner = _func(module_tree, "_run_git")
    rendered = ast.unparse(runner)
    assert "timeout=GIT_TIMEOUT_SECONDS" in rendered, "an unbounded git call can hang startup"
    assert "creationflags=_NO_WINDOW" in rendered, "a console window must not flash at startup"
    assert "capture_output=True" in rendered
    assert "GIT_TERMINAL_PROMPT" in rendered, \
        "an unattended startup must fail cleanly rather than block on a credential prompt"

    calls = [n for n in ast.walk(module_tree) if isinstance(n, ast.Call)
             and ast.unparse(n.func).startswith("subprocess.")]
    assert [ast.unparse(c.func) for c in calls] == ["subprocess.run"], \
        "exactly one subprocess site, and it is `run` so a timeout kills and reaps the child"
    assert all(c in ast.walk(runner) for c in calls), "the subprocess site escaped _run_git"


def test_the_module_writes_nothing(module_tree):
    """A read-only identity probe. It must not create, delete, move or write anything — a label
    on a screen is not a reason to touch the operator's disk.
    """
    rendered = ast.unparse(module_tree)
    for forbidden in ("os.makedirs", "os.mkdir", "os.remove", "os.unlink", "os.rename",
                      "os.replace", "os.rmdir", "shutil", "open("):
        assert forbidden not in rendered, f"build_identity reaches {forbidden}"


def test_the_identity_is_resolved_once_per_root(monkeypatch, tmp_path):
    """`cached_identity` is what the GUI calls, and it must not spawn a `git` child per caller."""
    fork_build._CACHE.clear()
    calls: list = []

    def counted(root, runner=None):
        calls.append(root)
        return fork_build.BuildIdentity(commit=_COMMIT, modified=False)

    monkeypatch.setattr(fork_build, "resolve", counted)
    root = str(tmp_path)
    first = fork_build.cached_identity(root)
    second = fork_build.cached_identity(root)
    third = fork_build.cached_identity(root.replace("\\", "/"))
    assert first == second == third
    assert len(calls) == 1, f"resolved {len(calls)} times for one root"
    fork_build._CACHE.clear()


# ---------------------------------------------------------------------------------------------
# 2. Real `git`
#
# The branches above are driven by a fake runner, which proves the policy and nothing about
# `git`'s actual answers. These use real repositories, and skip where `git` is absent so the
# bare-interpreter promise still holds.
# ---------------------------------------------------------------------------------------------

_GIT = shutil.which("git")
_needs_git = pytest.mark.skipif(_GIT is None, reason="git is not on PATH")


def _git(root, *args):
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    done = subprocess.run([_GIT, "-C", str(root)] + list(args),
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, f"git {args} failed: {done.stderr}"
    return done.stdout.strip()


@pytest.fixture
def real_repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-q", "-m", "first")
    return root


@_needs_git
def test_a_real_clean_checkout_matches_git(real_repo):
    identity = fork_build.resolve(str(real_repo))
    assert identity.commit == _git(real_repo, "rev-parse", "HEAD")
    assert identity.modified is False
    assert identity.label() == identity.commit[:8]


@_needs_git
def test_a_real_untracked_file_is_not_a_modified_build(real_repo):
    (real_repo / "scratch.log").write_text("noise\n", encoding="utf-8")
    assert fork_build.resolve(str(real_repo)).modified is False


@_needs_git
def test_a_real_tracked_edit_is_a_modified_build(real_repo):
    (real_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    identity = fork_build.resolve(str(real_repo))
    assert identity.modified is True
    assert identity.label().endswith(f"({fork_build.MODIFIED_NOTE})")


@_needs_git
def test_a_real_detached_head_still_resolves(real_repo):
    """The case a branch-name lookup would have broken. Measured, not reasoned about."""
    first = _git(real_repo, "rev-parse", "HEAD")
    (real_repo / "tracked.txt").write_text("second\n", encoding="utf-8")
    _git(real_repo, "commit", "-q", "-am", "second")
    _git(real_repo, "checkout", "-q", "--detach", first)
    assert _git(real_repo, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", \
        "the fixture is not actually detached, so this test would prove nothing"
    identity = fork_build.resolve(str(real_repo))
    assert identity.commit == first
    assert identity.modified is False
    assert identity.label() == first[:8]


@_needs_git
def test_a_real_app_root_nested_in_a_foreign_repository_is_refused(real_repo):
    """An installed copy sitting inside an unrelated checkout. `git` happily answers for the
    OUTER repository, and reporting that commit would be confidently wrong.
    """
    nested = real_repo / "BeatSyncInstall"
    nested.mkdir()
    assert _git(nested, "rev-parse", "--show-toplevel"), "git does answer for the nested path"
    identity = fork_build.resolve(str(nested))
    assert not identity.known
    assert identity.reason == fork_build.REASON_FOREIGN_WORKTREE
    assert identity.label() == fork_build.UNKNOWN_LABEL


@_needs_git
def test_a_real_directory_that_is_not_a_checkout_is_build_unknown(tmp_path):
    plain = tmp_path / "installed-copy"
    plain.mkdir()
    (plain / "src").mkdir()
    identity = fork_build.resolve(str(plain))
    assert not identity.known
    assert identity.reason == fork_build.REASON_NOT_A_REPOSITORY
    assert identity.label() == fork_build.UNKNOWN_LABEL


@_needs_git
def test_this_repository_resolves_to_its_own_head():
    """The end-to-end answer, against the checkout the suite is running from."""
    identity = fork_build.resolve(_REPO_ROOT)
    assert identity.commit == _git(_REPO_ROOT, "rev-parse", "HEAD")
    assert identity.short == identity.commit[:8]
    assert identity.label().startswith(identity.short)


@_needs_git
def test_a_real_linked_worktree_reports_its_own_checkout(real_repo, tmp_path):
    """A linked worktree IS its own root, and must report the revision checked out there rather
    than the main checkout's. Pinned because the containment check could plausibly have been
    written in a way that refused one."""
    first = _git(real_repo, "rev-parse", "HEAD")
    linked = tmp_path / "linked"
    _git(real_repo, "worktree", "add", "-q", "--detach", str(linked), first)
    try:
        identity = fork_build.resolve(str(linked))
        assert identity.commit == first
        assert identity.known
    finally:
        _git(real_repo, "worktree", "remove", "--force", str(linked))


# ---------------------------------------------------------------------------------------------
# 3. The GUI seam
#
# `gui.py` cannot be imported on a bare interpreter, so this is `ast` and source position over
# the real file — the same technique as the other GUI seam suites.
# ---------------------------------------------------------------------------------------------

def test_the_version_is_the_fork_packages_and_there_is_no_second_one(gui_source: str):
    """One product version, stated once. Two constants is how the screen and the §5(a) record
    start disagreeing, and the task explicitly asked for the existing one to be reused."""
    assert "from beatsync_fork import FORK_VERSION" in gui_source
    with open(_UI_CONTENT, encoding="utf-8") as handle:
        content = handle.read()
    for name in ("FORK_VERSION = ", "APP_VERSION", "UI_VERSION = ", "VERSION_STRING",
                 "__version__"):
        assert name not in gui_source, f"gui.py defines its own {name}"
        assert name not in content, f"ui_content.py defines its own {name}"
    # and the literal itself is nowhere near the UI layer
    assert beatsync_fork.FORK_VERSION not in gui_source, \
        "the version is hardcoded in gui.py instead of imported"
    assert beatsync_fork.FORK_VERSION not in content, \
        "the version is hardcoded in ui_content.py instead of substituted"


def test_the_version_is_still_a_three_part_release_number():
    """Guards the format the task named, so a future edit cannot quietly make it `1.01`."""
    import re

    assert re.fullmatch(r"\d+\.\d+\.\d+", beatsync_fork.FORK_VERSION), \
        beatsync_fork.FORK_VERSION


def test_the_heading_shows_the_title_and_the_version(gui_source: str):
    assert 'gr.Markdown(f"# {UI_TITLE_VERSIONED.format(title=UI_TITLE, ' \
           'version=FORK_VERSION)}")' in gui_source
    import ui_content
    rendered = ui_content.UI_TITLE_VERSIONED.format(title=ui_content.UI_TITLE,
                                                    version=beatsync_fork.FORK_VERSION)
    assert rendered == "🎵 BeatSync Engine v" + beatsync_fork.FORK_VERSION
    assert rendered.startswith(ui_content.UI_TITLE), "the title text itself must be unchanged"


def test_the_build_line_shows_the_resolved_label_and_the_fork(gui_source: str):
    assert "gr.Markdown(UI_BUILD_LINE.format(build=build_label), " \
           "elem_classes=['bs-build-id'])" in gui_source
    import ui_content
    rendered = ui_content.UI_BUILD_LINE.format(build="e6a7c0cd")
    assert rendered == "Running build: `e6a7c0cd` · Digital Union"
    # An unknown build renders the module's own sentence, not an empty gap or a fake id.
    unknown = ui_content.UI_BUILD_LINE.format(build=fork_build.UNKNOWN_LABEL)
    assert "Build unknown" in unknown


def test_the_build_line_sits_between_the_heading_and_the_description(gui_source: str):
    order = [gui_source.index(token) for token in (
        "UI_TITLE_VERSIONED.format", "UI_BUILD_LINE.format", "gr.Markdown(UI_MAIN_DESCRIPTION)")]
    assert order == sorted(order), "the build line is not directly under the heading"


def test_the_browser_title_shows_the_version_and_the_build(gui_source: str):
    assert "title=UI_BROWSER_TITLE.format(version=FORK_VERSION, build=build_label)" in gui_source
    assert "title='BeatSync Engine'" not in gui_source, "the bare title is still being passed"
    import ui_content
    rendered = ui_content.UI_BROWSER_TITLE.format(version="0.1.0", build="e6a7c0cd")
    assert rendered == "BeatSync Engine v0.1.0 (e6a7c0cd)"
    assert "`" not in ui_content.UI_BROWSER_TITLE, "a browser tab renders no Markdown"


def test_the_blocks_call_gained_no_new_keyword(gui_tree: ast.Module):
    """Only `title`'s VALUE changed. `gr.Blocks` takes `**kwargs`, so an invented keyword here
    would not be caught by reading its `__init__` — it would surface at startup, which is the
    failure mode this repository has already paid for once.
    """
    call = next(n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
                and ast.unparse(n.func) == "gr.Blocks")
    assert {kw.arg for kw in call.keywords} == {"title", "theme", "css"}
    assert call.args == [], "gr.Blocks must keep taking its arguments by keyword"


def test_the_identity_is_resolved_exactly_once_and_inside_create_ui(gui_source: str,
                                                                    gui_tree: ast.Module):
    """"Once at startup" asserted on the source rather than hoped for: one call, through the
    memoised accessor, in the function that runs once per process.
    """
    assert gui_source.count("fork_build.cached_identity(") == 1
    assert "fork_build.resolve(" not in gui_source, \
        "the GUI must go through the memoised accessor, not re-resolve"
    create_ui = ast.unparse(_func(gui_tree, "create_ui"))
    assert create_ui.count("fork_build.cached_identity(ROOT_DIR)") == 1
    assert create_ui.count("build_label = ") == 1
    # and it is the application root, never a guess or a relative path
    assert "cached_identity(os.getcwd())" not in gui_source
    assert "cached_identity('.')" not in gui_source


def test_no_handler_resolves_a_build_identity(gui_tree: ast.Module):
    """A browser event, a tab selection or a render must never reach this. Walked over every
    function in the module rather than the one we know about.
    """
    for node in ast.walk(gui_tree):
        if not isinstance(node, ast.FunctionDef) or node.name == "create_ui":
            continue
        rendered = ast.unparse(node)
        assert "fork_build" not in rendered, f"{node.name} resolves a build identity"
        assert "build_label" not in rendered, f"{node.name} reads the build label"


def test_the_header_adds_no_subprocess_site_to_gui(gui_tree: ast.Module):
    """The pinned count, which is the whole reason the resolver lives in the fork package."""
    runs = [n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
            and ast.unparse(n.func) == "subprocess.run"]
    popens = [n for n in ast.walk(gui_tree) if isinstance(n, ast.Call)
              and ast.unparse(n.func).endswith("Popen")]
    assert len(runs) == 2, f"gui.py has {len(runs)} subprocess.run sites, expected 2"
    assert popens == [], "gui.py must spawn nothing through Popen"


def test_the_header_adds_no_event_registration(gui_source: str, gui_tree: ast.Module):
    """It is static text written once at build time. No `app.load`, no component identity, and
    nothing that could put it in an `inputs` or `outputs` list.
    """
    assert gui_source.count("app.load(") == 3, "the authorised app.load installers are three"
    for call in ast.walk(gui_tree):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if keyword.arg in {"inputs", "outputs"}:
                rendered = ast.unparse(keyword.value)
                assert "build_label" not in rendered
                assert "bs-build-id" not in rendered


def test_the_two_header_markdowns_are_not_components_anything_can_write(gui_tree: ast.Module):
    """Unnamed on purpose: a name is what makes a widget addressable by a handler."""
    for node in ast.walk(gui_tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == "gr.Markdown"):
            rendered = ast.unparse(node.value)
            assert "UI_BUILD_LINE" not in rendered, "the build line was given a name"
            assert "UI_TITLE_VERSIONED" not in rendered, "the heading was given a name"


def test_the_build_line_styling_is_applied_and_scoped(gui_source: str):
    """A stylesheet nobody passes to `gr.Blocks` is decoration in a source file — and one that
    is not scoped to its own class can reach the rest of the page."""
    assert "BUILD_ID_CSS = " in gui_source
    assert "css=STATUS_BOX_CSS + CUT_CANCEL_CSS + CLIPS_GALLERY_CSS + BUILD_ID_CSS" in gui_source
    # The pre-existing pin in test_raw_cutter_progress.py must still hold.
    assert "css=STATUS_BOX_CSS + CUT_CANCEL_CSS" in gui_source
    start = gui_source.index("BUILD_ID_CSS = ")
    block = gui_source[start:gui_source.index('"""', gui_source.index('"""', start) + 3) + 3]
    assert ".bs-build-id" in block
    for selector in ("body", "#status-output-box", "#clips-gallery", "button", "h1 {"):
        assert selector not in block, f"the build-id rule reaches {selector}"


def test_the_render_pipeline_and_the_cache_identity_are_untouched(gui_source: str, module_tree):
    """A label on a screen must not be able to change what is rendered or re-key an analysis
    cache. Asserted from both ends: the module computes no identity, and the GUI's build label
    reaches no render or cache call site.
    """
    rendered = ast.unparse(module_tree)
    for forbidden in ("hashlib", "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "beat_info",
                      "creative", "_video_signature", "qwen", "Qwen"):
        assert forbidden not in rendered, f"build_identity reaches {forbidden}"
    # `build_label` is a local of `create_ui` and appears only where the header is built.
    occurrences = gui_source.count("build_label")
    assert occurrences == 3, f"build_label appears {occurrences} times, expected 3"
    for sink in ("analyze_beats_auto(", "create_music_video(", "process_video_guarded(",
                 "_promote_output_no_replace(", "resolve_for_render("):
        index = 0
        while True:
            index = gui_source.find(sink, index)
            if index < 0:
                break
            window = gui_source[index:index + 600]
            assert "build_label" not in window, f"the build label reaches {sink}"
            index += 1


def test_the_templates_live_in_ui_content_like_every_other_string():
    import ui_content

    for name in ("UI_TITLE_VERSIONED", "UI_BUILD_LINE", "UI_BROWSER_TITLE"):
        assert hasattr(ui_content, name), f"{name} is not in ui_content.py"
    # and they are templates, not finished sentences with a build baked in
    assert "{version}" in ui_content.UI_TITLE_VERSIONED
    assert "{build}" in ui_content.UI_BUILD_LINE
    assert "{version}" in ui_content.UI_BROWSER_TITLE and "{build}" in ui_content.UI_BROWSER_TITLE


def test_the_templates_do_not_re_decide_what_an_unknown_build_looks_like():
    """`build_identity` owns that vocabulary. A second opinion in a template is how the header
    and the record start saying different things about the same install."""
    import ui_content

    for template in (ui_content.UI_TITLE_VERSIONED, ui_content.UI_BUILD_LINE,
                     ui_content.UI_BROWSER_TITLE):
        for token in ("unknown", "modified", "unverified", "dirty"):
            assert token not in template.lower(), template


# ---------------------------------------------------------------------------------------------
# 4. The pinned Gradio signatures
#
# Read from an installed package WITHOUT importing it. This is the check that would have caught
# the `show_copy_button` defect at authoring time: a keyword that a component's `__init__` does
# not declare, on a class with no `**kwargs`, is a startup failure rather than a warning.
#
# It is a SIGNATURE check and nothing more. It does not construct a component, does not build the
# real `create_ui()`, and is not a live GUI smoke test -- see the construction test below for the
# strongest claim this suite is entitled to make.
# ---------------------------------------------------------------------------------------------

def _gradio_package() -> str:
    """The installed gradio package to inspect. `BEATSYNC_GRADIO_PACKAGE` overrides, so a machine
    whose portable runtime lives elsewhere can still run this check."""
    override = os.environ.get("BEATSYNC_GRADIO_PACKAGE", "").strip()
    if override:
        return override
    return os.path.join(_REPO_ROOT, "bin", "python-3.13.14-embed-amd64", "Lib",
                        "site-packages", "gradio")


def _declared_parameters(path: str, class_name: str):
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    names = {a.arg for a in item.args.args} | {a.arg for a in item.args.kwonlyargs}
                    return names, item.args.kwarg is not None
    raise AssertionError(f"no {class_name}.__init__ in {path}")


#: Only what THIS change introduces, and the distinction is load-bearing rather than convenient.
#:
#: `gr.Markdown` declares no `**kwargs`, so a name absent from its `__init__` is a startup
#: `TypeError` — that is the `show_copy_button` failure mode exactly, and `elem_classes` is the one
#: component keyword this feature adds.
#:
#: `gr.Blocks` is the opposite shape: measured on the pinned 6.19.0 it declares `title` but reaches
#: `theme` and `css` through `**kwargs` and a parent `__init__`, so a declared-name check says
#: nothing true about those two. They are also **not** this task's to verify: both are passed
#: unchanged on the base revision and the running application demonstrates them. This feature only
#: supplies a different *value* for the already-declared `title`.
@pytest.mark.parametrize("module,class_name,keywords", [
    (os.path.join("components", "markdown.py"), "Markdown", ("value", "elem_classes")),
    ("blocks.py", "Blocks", ("title",)),
])
def test_every_keyword_the_header_passes_is_declared_by_the_pinned_gradio(module, class_name,
                                                                          keywords):
    package = _gradio_package()
    path = os.path.join(package, module)
    if not os.path.isfile(path):
        pytest.skip(f"no installed gradio to inspect at {path}; "
                    f"set BEATSYNC_GRADIO_PACKAGE to run this check")
    declared, takes_kwargs = _declared_parameters(path, class_name)
    for keyword in keywords:
        assert keyword in declared, (
            f"gr.{class_name} does not declare {keyword!r} in the installed gradio "
            f"(takes **kwargs: {takes_kwargs}) — passing it would fail at startup")


def test_the_build_line_class_argument_matches_the_declared_type():
    """`elem_classes` is annotated `list[str] | str | None`, and every other call site in
    `gui.py` passes a list. The header passes a list too, so the form is the one the running
    application already demonstrates rather than the one merely permitted on paper.
    """
    with open(_GUI, encoding="utf-8") as handle:
        source = handle.read()
    assert "elem_classes=['bs-build-id']" in source
    assert "elem_classes='bs-build-id'" not in source


@pytest.mark.skipif(importlib.util.find_spec("gradio") is None,
                    reason="gradio is not importable by the test interpreter")
def test_the_header_components_really_construct_under_real_gradio(tmp_path, monkeypatch):
    """An ISOLATED construction, and the limits of what it proves are the point.

    It builds the three real header expressions — the `gr.Blocks` title and css, the versioned
    heading, the styled build line — under real Gradio, with temporary paths redirected so
    nothing touches the operator's configuration, cache or media. Nothing is launched, no port is
    bound, no network is used.

    It is **not** a live GUI acceptance test and does not build `create_ui()`: on this base
    revision the full build fails for an unrelated, separately documented reason (a
    `show_copy_button` keyword that gradio 6.19.0 removed), which this task deliberately does not
    fix. So a pass here means "these keywords and values are accepted by real Gradio", never
    "the application starts".
    """
    import gradio as gr

    import ui_content

    monkeypatch.setenv("GRADIO_TEMP_DIR", str(tmp_path / "gradio"))
    monkeypatch.setenv("GRADIO_ANALYTICS_ENABLED", "False")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BEATSYNC_SHARED_CONFIG", str(tmp_path / "shared-config.json"))

    label = fork_build.resolve(_REPO_ROOT).label()
    title = ui_content.UI_BROWSER_TITLE.format(version=beatsync_fork.FORK_VERSION, build=label)

    app = gr.Blocks(title=title, theme="ocean", css=".bs-build-id p { font-size: 0.85em; }")
    with app:
        gr.Markdown(f"# {ui_content.UI_TITLE_VERSIONED.format(title=ui_content.UI_TITLE, version=beatsync_fork.FORK_VERSION)}")
        gr.Markdown(ui_content.UI_BUILD_LINE.format(build=label), elem_classes=["bs-build-id"])
    assert app.title == title


@pytest.mark.skipif(importlib.util.find_spec("gradio") is None,
                    reason="gradio is not importable by the test interpreter")
def test_an_invented_component_keyword_would_be_rejected():
    """The control for the test above. Without it, a construction that silently swallowed unknown
    keywords would make the whole check vacuous — which is precisely how `show_copy_button`
    survived review once already.
    """
    import gradio as gr

    with pytest.raises(TypeError):
        gr.Markdown("x", definitely_not_a_gradio_keyword=True)
