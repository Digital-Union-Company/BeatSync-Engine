"""R5 Clips Preview Selector: the GUI handlers, executed for real.

[FORK] Digital-Union (R5-P3). The suites beside this one cover the stdlib-only core
(``test_clips_review.py``) and the HTTP boundary (``test_clips_media_server.py``). Neither
reaches ``gui.py``'s handlers, and that gap is exactly where the two MAJOR defects of the
independent R5-P2 review lived:

* **F1** — ``_on_clips_scan`` ran ``begin_scan -> new_session -> publish_page -> ensure_posters
  -> remember`` with no critical section, so a slow older scan republished its own revoked
  session and left every tile answering ``403 unknown_session``. The core's
  ``ScanCoordinator`` tests prove the mechanism; only this file proves the *handler* uses it.
* **F2** — the handler derived the expected revision by coercion, so a malformed one became
  "no concurrency check requested".
* **F8** — the Confirm button's enabled state must come from the durable record and nothing else.

``gui.py`` cannot be imported on a bare interpreter: it needs gradio, cv2, cupy and the portable
runtime. So the handlers are **extracted with ``ast`` and executed against a synthesised parent
namespace**, which is this repository's established tier-2 technique (see
``.claude/rules/test-harness.md`` and the ``_run_batch`` harness in ``test_raw_cutter_batch.py``).
The code under test is the shipped source, byte for byte -- not a reimplementation.
"""

from __future__ import annotations

import ast
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict

import pytest

from beatsync_fork import clips_review as cr

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
_UI_CONTENT = os.path.join(_SRC, "ui_content.py")

#: The handlers this suite drives, in dependency order within one namespace.
_HANDLERS = ("_clips_store", "_clips_owner", "_clips_remember", "_clips_view",
             "_clips_poster_exists", "_clips_poster_resolver", "_clips_identities",
             "_on_clips_check", "_clips_render_current", "_on_clips_scan", "_clips_decide",
             "_clips_state", "_on_clips_refresh", "_on_clips_confirm")


class _Update(dict):
    """Stands in for ``gr.update(...)``, which returns an opaque Gradio object."""


class _Gr:
    @staticmethod
    def update(**kwargs):
        return _Update(kwargs)

    class Request:
        """``gr.Request``: injected per request, never a widget in a click's ``inputs``."""

        def __init__(self, session_hash=None):
            self.session_hash = session_hash


class _FfmpegProcessing:
    FFMPEG_PATH = "ffmpeg.exe"


def _ui_constants() -> dict:
    """The real operator-facing strings, read from ``ui_content.py``.

    Read rather than stubbed on purpose: a stub here would let a test pass against wording the
    app does not actually ship.
    """
    tree = ast.parse(open(_UI_CONTENT, encoding="utf-8").read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.startswith("CLIPS_"):
                try:
                    out[name] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return out


@pytest.fixture(scope="module")
def ui():
    constants = _ui_constants()
    for required in ("CLIPS_GALLERY_INITIAL_HTML", "CLIPS_COUNTS_INITIAL_TEXT",
                     "CLIPS_CONFIRM_DISABLED_TEXT", "CLIPS_CONFIRM_READY_TEXT",
                     "CLIPS_SCAN_SUPERSEDED_TEXT", "CLIPS_REFRESH_TO_ENABLE_HINT"):
        assert required in constants, f"{required} is missing from ui_content.py"
    return constants


def _build(state_dir, ui_constants, poster_runner=None, poster_hook=None):
    """Execute the real handlers against a synthesised ``gui`` namespace."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    missing = [name for name in _HANDLERS if name not in functions]
    assert not missing, f"gui.py no longer defines {missing}"

    namespace: dict = {
        "os": os, "secrets": secrets, "threading": threading, "OrderedDict": OrderedDict,
        "gr": _Gr, "fork_clips": cr,
        "ROOT_DIR": os.path.join(state_dir, "approot"),
        "CLIPS_STATE_DIR": state_dir,
        "CLIPS_POSTER_DIR": os.path.join(state_dir, "posters"),
        "_CLIPS_REGISTRY": cr.ClipRegistry(),
        "_CLIPS_STORE_LOCK": threading.Lock(),
        "_CLIPS_STORE": None,
        "CLIPS_MAX_TRACKED_SESSIONS": 32,
        "_CLIPS_SCANS": cr.ScanCoordinator(max_owners=32),
    }
    namespace.update(ui_constants)
    sys.modules.setdefault("ffmpeg_processing", _FfmpegProcessing)

    module = ast.fix_missing_locations(
        ast.Module(body=[functions[name] for name in _HANDLERS], type_ignores=[]))
    exec(compile(module, _GUI, "exec"), namespace)          # noqa: S102 - the body under test

    if poster_hook is not None or poster_runner is not None:
        class _Shim:
            """Swaps only the poster entry point; everything else is the real core."""

            def __getattr__(self, item):
                return getattr(cr, item)

            def ensure_posters(self, entries, cache_dir, ffmpeg, runner, **kwargs):
                if poster_hook is not None:
                    poster_hook(list(entries))
                    return cr.PosterOutcome(workers=4)
                return cr.ensure_posters(entries, cache_dir, ffmpeg, poster_runner, **kwargs)

        namespace["fork_clips"] = _Shim()
    return namespace


def _tree_of_clips(root, count=3):
    media = os.path.join(str(root), "clips")
    os.makedirs(media, exist_ok=True)
    for index in range(count):
        with open(os.path.join(media, f"clip{index:04d}.mp4"), "wb") as handle:
            handle.write(b"m" * (512 + index))
    return media


def _session_of(grid):
    found = re.search(r'data-session="([^"]+)" data-page="([^"]+)"', grid)
    return (found.group(1), found.group(2)) if found else (None, None)


def _no_poster(argv, timeout):
    """A poster failure. The tile must stay classifiable, so this is the stricter default."""
    return 1


@pytest.fixture
def world(tmp_path, ui):
    media = _tree_of_clips(tmp_path)
    namespace = _build(str(tmp_path / "state"), ui, poster_runner=_no_poster)
    return {
        "ns": namespace, "media": media, "tmp": tmp_path, "ui": ui,
        "library": str(tmp_path / "library"), "rejected": str(tmp_path / "rejected"),
        "request": _Gr.Request,
    }


def _scan(world, owner, folder=None, page_size=100):
    return world["ns"]["_on_clips_scan"](
        folder or world["media"], True, world["library"], world["rejected"], page_size,
        world["request"](session_hash=owner))


# =============================================================================================
# F1 — the same-owner scan race, driven through the real handler
# =============================================================================================

def test_the_newest_scan_wins_when_an_older_one_finishes_last(tmp_path, ui):
    """The reproduction from the independent review, inverted into an assertion.

    A starts, blocks inside the poster pass, B overtakes and completes, then A finishes. Before
    the fix the authoritative view became A's -- whose session ``new_session`` had already
    revoked -- so every clip endpoint answered 403 and nothing could be classified.
    """
    media = _tree_of_clips(tmp_path)
    gate = threading.Event()
    order = []

    def hook(entries):
        name = threading.current_thread().name
        order.append(f"{name}-in")
        if name == "scan-A":
            assert gate.wait(30), "the test's own gate timed out"
        order.append(f"{name}-out")

    namespace = _build(str(tmp_path / "state"), ui, poster_hook=hook)
    owner = "browser-session-1"
    request = _Gr.Request(session_hash=owner)
    results = {}

    def run(tag):
        results[tag] = namespace["_on_clips_scan"](
            media, True, str(tmp_path / "lib"), str(tmp_path / "rej"), 100, request)

    first = threading.Thread(target=run, args=("A",), name="scan-A")
    second = threading.Thread(target=run, args=("B",), name="scan-B")
    first.start()
    while "scan-A-in" not in order:
        time.sleep(0.01)
    second.start()
    second.join(30)
    gate.set()
    first.join(30)
    assert order.index("scan-B-out") < order.index("scan-A-out"), "A really did finish last"

    registry = namespace["_CLIPS_REGISTRY"]
    view = namespace["_clips_view"](owner)
    b_session, b_page = _session_of(results["B"][1])

    # the authoritative view is B's
    assert (view["session"], view["page"]) == (b_session, b_page)

    # B's clip endpoint serves authorized bytes, and B's state is valid
    descriptor, _ = registry.open_authorized(b_session, b_page, view["entries"][0].clip_id)
    os.close(descriptor)
    assert namespace["_clips_state"](owner, b_session, b_page)["ok"] is True

    # classification through B succeeds
    decided = namespace["_clips_decide"](owner, b_session, b_page, view["entries"][0],
                                         {"to": "SELECTED", "rev": 0})
    assert decided["ok"] is True, decided

    # A's late return repaints the AUTHORITATIVE page, never its own revoked one
    a_session, a_page = _session_of(results["A"][1])
    assert (a_session, a_page) == (b_session, b_page), (
        "a superseded scan must not hand the browser markup for a revoked session")
    assert ui["CLIPS_SCAN_SUPERSEDED_TEXT"].splitlines()[0] in results["A"][0]
    assert "r5-empty" not in results["A"][1], "and must not blank the newest page either"


def test_the_newest_scan_wins_when_it_is_the_one_that_finishes_last(tmp_path, ui):
    """The reverse ordering: the older scan completes first, the newer one is slow."""
    media = _tree_of_clips(tmp_path)
    gate = threading.Event()

    def hook(entries):
        if threading.current_thread().name == "scan-second":
            assert gate.wait(30)

    namespace = _build(str(tmp_path / "state"), ui, poster_hook=hook)
    owner = "browser-session-1"
    request = _Gr.Request(session_hash=owner)
    results = {}

    def run(tag):
        results[tag] = namespace["_on_clips_scan"](
            media, True, str(tmp_path / "lib"), str(tmp_path / "rej"), 100, request)

    threading.Thread(target=run, args=("first",), name="scan-first").start()
    while "first" not in results:
        time.sleep(0.01)
    second = threading.Thread(target=run, args=("second",), name="scan-second")
    second.start()
    time.sleep(0.05)
    gate.set()
    second.join(30)

    session, page = _session_of(results["second"][1])
    view = namespace["_clips_view"](owner)
    assert (view["session"], view["page"]) == (session, page)
    assert ui["CLIPS_SCAN_SUPERSEDED_TEXT"].splitlines()[0] not in results["second"][0]
    assert namespace["_clips_state"](owner, session, page)["ok"] is True


def test_two_owners_scanning_at_once_stay_independent(tmp_path, ui):
    media = _tree_of_clips(tmp_path)
    namespace = _build(str(tmp_path / "state"), ui, poster_runner=_no_poster)
    outcomes = {}

    def run(owner):
        outcomes[owner] = namespace["_on_clips_scan"](
            media, True, str(tmp_path / f"lib-{owner}"), str(tmp_path / f"rej-{owner}"), 100,
            _Gr.Request(session_hash=owner))

    threads = [threading.Thread(target=run, args=(f"browser-{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    registry = namespace["_CLIPS_REGISTRY"]
    sessions = set()
    for owner, outcome in outcomes.items():
        session, page = _session_of(outcome[1])
        assert session, f"{owner} got no grid"
        sessions.add(session)
        view = namespace["_clips_view"](owner)
        assert view["session"] == session, f"{owner}'s view was overwritten"
        descriptor, _ = registry.open_authorized(session, page, view["entries"][0].clip_id)
        os.close(descriptor)
        assert namespace["_clips_state"](owner, session, page)["ok"] is True
    assert len(sessions) == len(outcomes), "every owner got its own session"


def test_a_failing_newest_scan_leaves_an_honest_empty_state(world):
    """A newest scan that fails must not resurrect the previous page as though it were current."""
    namespace = world["ns"]
    owner = "browser-session-1"
    good = _scan(world, owner)
    good_session, _ = _session_of(good[1])
    assert good_session

    real_registry = namespace["_CLIPS_REGISTRY"]

    class _Failing:
        def __getattr__(self, item):
            return getattr(real_registry, item)

        def new_session(self, *args, **kwargs):
            raise cr.Refusal(500, "synthetic_failure_inside_the_authority_section")

    namespace["_CLIPS_REGISTRY"] = _Failing()
    try:
        failed = _scan(world, owner)
    finally:
        namespace["_CLIPS_REGISTRY"] = real_registry

    assert "r5-empty" in failed[1], "the gallery must be emptied, not left showing the old page"
    assert good_session not in failed[1]
    assert failed[3] == {"interactive": False}, "and Confirm must be disabled"
    assert namespace["_clips_view"](owner) == {}, "the stale view was discarded"


def test_a_scan_refused_before_changing_anything_takes_no_generation(world):
    """Only a scan that is going to change state may supersede a running one."""
    namespace = world["ns"]
    owner = "browser-session-1"
    good = _scan(world, owner)
    session, page = _session_of(good[1])
    newest = namespace["_CLIPS_SCANS"].newest(owner)

    _scan(world, owner, folder=str(world["tmp"] / "does-not-exist"))         # folder not found
    world["library"] = world["media"]                                        # library == input
    _scan(world, owner)
    world["library"] = str(world["tmp"] / "library")

    assert namespace["_CLIPS_SCANS"].newest(owner) == newest, (
        "a refused scan must not take a generation")
    assert namespace["_clips_state"](owner, session, page)["ok"] is True, (
        "and the good page must still be authoritative")


def test_the_scan_handler_uses_the_coordinator_rather_than_a_bare_dict():
    """Structural, so the protocol cannot be quietly removed from the handler.

    The core's own tests prove ``ScanCoordinator`` works; this proves ``_on_clips_scan`` actually
    routes through it, in the right order, with the poster pass outside the critical section.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    handler = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_on_clips_scan")
    body = ast.unparse(handler)
    assert "_CLIPS_SCANS.begin(owner)" in body, "the scan must take a generation"
    assert "_CLIPS_SCANS.authority(owner, generation)" in body
    assert "_CLIPS_SCANS.discard(owner, generation)" in body

    authority = next(node for node in ast.walk(handler)
                     if isinstance(node, ast.With)
                     and any("authority" in ast.unparse(item.context_expr)
                             for item in node.items))
    critical = ast.unparse(ast.Module(body=authority.body, type_ignores=[]))
    for required in ("begin_scan", "new_session", "publish_page"):
        assert required in critical, f"{required} must be inside the authority section"
    assert "ensure_posters" not in critical, (
        "the slow poster pass must NOT hold the owner's authority lock")

    assert "_CLIPS_PAGES" not in ast.unparse(tree), (
        "the unconditional last-write-wins page dict must be gone")
    assert "_RENDER_LOCK" not in body and "RENDER_CONCURRENCY_ID" not in body, (
        "a read-only gallery must not join the render lock or its concurrency group")


def test_remember_is_conditional_on_the_generation():
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    remember = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_clips_remember")
    assert [a.arg for a in remember.args.args] == ["owner", "generation", "view"]
    assert "publish(owner, generation, view)" in ast.unparse(remember)


# =============================================================================================
# F2 — the mandatory expected revision, through the real handler
# =============================================================================================

@pytest.mark.parametrize("payload,code", [
    ({"to": "REJECTED"}, cr.REVISION_REQUIRED),
    ({"to": "REJECTED", "rev": None}, cr.REVISION_REQUIRED),
    ({"to": "REJECTED", "rev": "1"}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": "abc"}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": True}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": 0.9}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": 1.0}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": -1}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": []}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": {}}, cr.MALFORMED_REVISION),
    ({"to": "REJECTED", "rev": 0}, cr.REVISION_CONFLICT),
    ({"to": "REJECTED", "rev": 42}, cr.REVISION_CONFLICT),
])
def test_the_handler_refuses_every_malformed_or_stale_revision(world, payload, code):
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entry = view["entries"][0]

    accepted = namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                          {"to": "SELECTED", "rev": 0})
    assert accepted["ok"] and accepted["rev"] == 1

    store = namespace["_clips_store"]()
    journal = os.path.join(store.dir, "review-journal.jsonl")
    with open(journal, "rb") as handle:
        before = handle.read()

    refused = namespace["_clips_decide"](owner, view["session"], view["page"], entry, payload)
    assert refused["ok"] is False
    assert refused["code"] == code
    assert refused["decision"] == "SELECTED" and refused["rev"] == 1, (
        "a refusal reports the server's record so a stale tab resyncs rather than guessing")
    assert refused["counts"]["SELECTED"] == 1

    with open(journal, "rb") as handle:
        assert handle.read() == before, "a refused decision must append nothing"
    identity = view["identity"][entry.clip_id]
    assert store.get(identity) == ("SELECTED", 1), "and must not move the record"


def test_the_handler_accepts_only_the_exact_current_revision(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entry = view["entries"][0]
    decide = namespace["_clips_decide"]

    first = decide(owner, view["session"], view["page"], entry, {"to": "SELECTED", "rev": 0})
    assert (first["ok"], first["rev"]) == (True, 1)
    second = decide(owner, view["session"], view["page"], entry, {"to": "REJECTED", "rev": 1})
    assert (second["ok"], second["decision"], second["rev"]) == (True, "REJECTED", 2)
    third = decide(owner, view["session"], view["page"], entry, {"to": "PENDING", "rev": 2})
    assert (third["ok"], third["decision"], third["rev"]) == (True, "PENDING", 3)


def test_the_handler_no_longer_coerces_a_revision():
    """Structural. The defect was the coercion itself, so forbid its shape in the handler."""
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    handler = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_clips_decide")
    body = ast.unparse(handler)
    assert "parse_expected_revision(payload)" in body
    assert "int(payload.get('rev'))" not in body and 'int(payload["rev"])' not in body
    assert "expected = None" not in body, (
        "a malformed revision must never become 'no concurrency check requested'")


def test_the_gallery_client_validates_the_whole_revision_attribute():
    """F2's client half. ``parseInt`` accepts a numeric prefix and yields ``NaN`` otherwise, and
    ``JSON.stringify(NaN)`` is ``null`` -- which the server used to read as "no check". So one
    DOM edit disabled the guarantee it was meant to feed."""
    source = open(_GUI, encoding="utf-8").read()
    start = source.index("CLIPS_GALLERY_JS = r\"\"\"")
    javascript = source[start:source.index('""".replace(', start)]
    assert "parseInt(tile.dataset.rev" not in javascript, "parseInt must not build a revision"
    assert "/^(0|[1-9][0-9]*)$/" in javascript, "the whole attribute must match"
    assert "Number.isSafeInteger" in javascript
    assert "local_revision_invalid" in javascript, "an invalid revision is reported, not sent"
    assert "sayProblem(" in javascript, "a failed resync must be surfaced"
    resync = javascript[javascript.index("window.__r5Resync ="):]
    assert "catch (err)" in resync, "__r5Resync must not throw into decide()"


# =============================================================================================
# F8 — Confirm and the counters agree, and only the server decides
# =============================================================================================

def test_confirm_is_disabled_until_the_record_says_otherwise(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    report, grid, counts, confirm = _scan(world, owner)
    assert confirm == {"interactive": False}, "undecided clips keep Confirm disabled"

    view = namespace["_clips_view"](owner)
    for entry in view["entries"]:
        result = namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                            {"to": "SELECTED", "rev": 0})
        assert result["ok"], result

    _, counts, confirm = namespace["_on_clips_refresh"](world["request"](owner))
    assert confirm == {"interactive": True}, "Refresh derives the state from the record"
    assert "Undecided 0" in counts


def test_putting_a_clip_back_to_pending_disables_confirm_again(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    for entry in view["entries"]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "SELECTED", "rev": 0})
    assert namespace["_on_clips_refresh"](world["request"](owner))[2] == {"interactive": True}

    reset = namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                                       {"to": "PENDING", "rev": 1})
    assert reset["ok"] and reset["decision"] == "PENDING"
    _, counts, confirm = namespace["_on_clips_refresh"](world["request"](owner))
    assert confirm == {"interactive": False}, "Confirm must not stay usable"
    assert "Undecided 1" in counts
    assert world["ui"]["CLIPS_CONFIRM_DISABLED_TEXT"].split(".")[0] in \
        namespace["_on_clips_confirm"](world["request"](owner))


def test_the_counters_tell_the_operator_that_refresh_enables_confirm(world):
    """A decision posts straight to /r5decide, so it cannot enable the Gradio button. Rather
    than leave 'Undecided 0' beside a greyed-out Confirm unexplained, the client says what is
    needed -- as display text only."""
    source = open(_GUI, encoding="utf-8").read()
    start = source.index("CLIPS_GALLERY_JS = r\"\"\"")
    javascript = source[start:source.index('""".replace(', start)]
    assert "counts.PENDING === 0" in javascript
    assert "__REFRESH_HINT__" in javascript
    assert "CLIPS_REFRESH_TO_ENABLE_HINT" in source, "the wording lives in ui_content.py"
    for forbidden in ("interactive", "disabled = false", "removeAttribute('disabled')",
                      'removeAttribute("disabled")'):
        assert forbidden not in javascript, (
            f"the client must never enable Confirm itself ({forbidden})")


def test_only_the_record_can_ever_ENABLE_the_confirm_button():
    """Three handlers touch the button, and only one of them may ever enable it.

    `_on_clips_scan` and `_on_clips_refresh` additionally *disable* it on a refusal or a failure,
    which is the fail-closed direction and must stay. The asymmetry is the point: enabling is
    derived from `store.counts()` in exactly one place.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    movers, enablers = [], []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_clips") \
                and not node.name.startswith("_on_clips"):
            continue
        body = ast.unparse(node)
        if "interactive=" in body:
            movers.append(node.name)
        if "interactive=True" in body or "interactive=counts" in body:
            enablers.append(node.name)
    assert sorted(movers) == ["_clips_render_current", "_on_clips_refresh", "_on_clips_scan"], \
        movers
    assert sorted(enablers) == ["_clips_render_current", "_on_clips_scan"], (
        f"only these may enable Confirm, and both from the record: {enablers}")
    for name in enablers:
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "counts[fork_clips.PENDING] == 0" in ast.unparse(node), (
            f"{name} must derive the enabled state from the durable record")
    refresh = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_on_clips_refresh")
    assert "interactive=False" in ast.unparse(refresh), (
        "an unusable session must still fail closed")


def test_confirm_reports_and_moves_nothing(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    before = sorted(os.listdir(world["media"]))
    for entry in view["entries"]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "SELECTED", "rev": 0})
    message = namespace["_on_clips_confirm"](world["request"](owner))
    assert "NOT enabled" in message, message
    assert sorted(os.listdir(world["media"])) == before, "no clip may move"
    assert not os.path.exists(world["library"]), "no destination may even be created"
    assert not os.path.exists(world["rejected"])


# =============================================================================================
# Preserved behaviour: an unpostered clip stays classifiable, and no name reaches the browser
# =============================================================================================

def test_a_clip_with_no_poster_is_still_classifiable(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    _, grid, _, _ = _scan(world, owner)
    view = namespace["_clips_view"](owner)
    assert "still classifiable" in grid
    assert grid.count("r5-tile") == len(view["entries"])
    result = namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                                        {"to": "REJECTED", "rev": 0})
    assert result["ok"] is True


def test_no_source_filename_reaches_the_browser(world):
    namespace = world["ns"]
    owner = "browser-session-1"
    _, grid, _, _ = _scan(world, owner)
    view = namespace["_clips_view"](owner)
    for entry in view["entries"]:
        assert os.path.basename(entry.path) not in grid
        assert entry.path not in grid
    assert world["media"] not in grid
    for url in re.findall(r'(?:src|data-video)="([^"]+)"', grid):
        assert re.fullmatch(r"/r5(?:clip|poster)/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+",
                            url), url
    state = namespace["_clips_state"](owner, view["session"], view["page"])
    assert world["media"] not in repr(state)


def test_a_superseded_scan_report_names_no_path(world):
    assert "\\" not in world["ui"]["CLIPS_SCAN_SUPERSEDED_TEXT"]
    assert "superseded" in world["ui"]["CLIPS_SCAN_SUPERSEDED_TEXT"].lower()
