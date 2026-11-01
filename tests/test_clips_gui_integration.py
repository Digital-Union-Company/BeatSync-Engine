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
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import OrderedDict

import pytest

from beatsync_fork import clips_review as cr
from beatsync_fork import collision
from beatsync_fork import transfer_guard as tg

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
_GUI = os.path.join(_SRC, "gui.py")
_UI_CONTENT = os.path.join(_SRC, "ui_content.py")

#: The handlers this suite drives, in dependency order within one namespace.
_HANDLERS = ("_clips_store", "_clips_state_unavailable_text", "_clips_unavailable_outputs",
             "_clips_owner", "_clips_remember", "_clips_view",
             "_clips_failure_text", "_clips_poster_exists", "_clips_poster_resolver",
             "_clips_identities", "_clips_durations",
             "_on_clips_check", "_clips_render_current", "_on_clips_scan",
             # [R13] The transfer's exclusion key, then the single-flight generator and the
             # collected form every pre-R13 assertion in this file drives.
             "_clips_transfer_key",
             "_clips_decide", "_clips_state", "_on_clips_refresh",
             "_clips_confirm_only", "_clips_working_frame", "_clips_sorted_text",
             "_clips_confirm_frames", "_on_clips_confirm",
             # [FORK] R8 Final Polish: the sweep gate's Cancel half.
             "_on_clips_sweep_cancel")


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
        # [R13] The real single-flight authority, a fresh one per built namespace so two tests
        # never inherit each other's active transfers. Not a stub: the admission rule is the
        # thing under test in `test_clips_transfer_overlay.py`, and every assertion here about a
        # press being refused or allowed has to meet the shipped object.
        "fork_transfer": tg,
        "_CLIPS_TRANSFERS": tg.TransferGuard(),
        # [FORK] R8 UX Refresh: Confirm reads the operator's saved filename-collision policy, so
        # the handler names `gui._collision_policy`. It is stubbed to the DEFAULT here on purpose:
        # every assertion in this suite is about the pre-R8 behaviour -- one clip per name, an
        # occupied destination refused, nothing overwritten -- and the default is exactly that
        # behaviour. The opt-in `suffix` policy has its own suite, `test_collision.py`, which
        # exercises both policies against a real filesystem.
        "_collision_policy": lambda: collision.DEFAULT_POLICY,
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


def test_putting_every_clip_back_to_pending_disables_confirm_again(world):
    """[FORK] R8 Final Polish: EVERY clip, not one.

    The original put a single clip back to PENDING and required Confirm to go dark, because the
    admission rule was "nothing may be undecided". Under keep-only that is no longer true and
    should not be — one undecided clip among nine kept ones is exactly the case the gate exists
    for. What must still hold, and is asserted here, is the fail-closed direction: when the record
    says **nothing is kept**, Confirm goes dark again and a press refuses.
    """
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    for entry in view["entries"]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "SELECTED", "rev": 0})
    assert namespace["_on_clips_refresh"](world["request"](owner))[2] == {"interactive": True}

    # One clip back to PENDING must NOT disable it any more: the others are still kept.
    reset = namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                                       {"to": "PENDING", "rev": 1})
    assert reset["ok"] and reset["decision"] == "PENDING"
    _, counts, confirm = namespace["_on_clips_refresh"](world["request"](owner))
    assert "Undecided 1" in counts
    if len(view["entries"]) > 1:
        assert confirm == {"interactive": True}, (
            "one undecided clip among kept ones is the gate's case, not a refusal")

    # Now clear the rest: with nothing kept, the fail-closed direction must hold.
    for index, entry in enumerate(view["entries"][1:], start=1):
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "PENDING", "rev": 1})
    _, counts, confirm = namespace["_on_clips_refresh"](world["request"](owner))
    assert confirm == {"interactive": False}, "nothing kept must leave Confirm unusable"
    assert world["ui"]["CLIPS_CONFIRM_DISABLED_TEXT"].split(".")[0] in \
        namespace["_on_clips_confirm"]("", world["request"](owner))[4]


def test_the_counters_tell_the_operator_that_refresh_enables_confirm(world):
    """A decision posts straight to /r5decide, so it cannot enable the Gradio button. Rather
    than leave 'Undecided 0' beside a greyed-out Confirm unexplained, the client says what is
    needed -- as display text only."""
    source = open(_GUI, encoding="utf-8").read()
    start = source.index("CLIPS_GALLERY_JS = r\"\"\"")
    javascript = source[start:source.index('""".replace(', start)]
    assert "counts.SELECTED > 0" in javascript, (
        "[R8] readiness is now at least one KEPT clip, matching the server")
    assert "__REFRESH_HINT__" in javascript
    assert "CLIPS_REFRESH_TO_ENABLE_HINT" in source, "the wording lives in ui_content.py"
    for forbidden in ("interactive", "disabled = false", "removeAttribute('disabled')",
                      'removeAttribute("disabled")'):
        assert forbidden not in javascript, (
            f"the client must never enable Confirm itself ({forbidden})")


def test_only_the_record_can_ever_ENABLE_the_confirm_button():
    """Four handlers touch the button, and only two of them may ever enable it.

    `_on_clips_scan` and `_on_clips_refresh` additionally *disable* it on a refusal or a failure,
    and [R5-P11 / N7] `_clips_unavailable_outputs` disables it when the durable record could not
    be read. That is the fail-closed direction and must stay. The asymmetry is the point: enabling
    is derived from `store.counts()` and nothing else.

    The expected list is deliberately an identity set rather than a count. A new handler that
    merely *disables* the button is a safe addition and belongs here; one that enables it is the
    defect this test exists to catch, and it would have to appear in `enablers` -- which is pinned
    separately, with the derivation of each enabler checked below.
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
    # [R13] `_clips_working_frame` is the fifth mover, and it is exactly the safe addition this
    # test's docstring describes: it takes Confirm away while a transfer is running and has no
    # route to giving it back. The enablers list is unchanged, which is the assertion that
    # matters -- R13 added a transfer, not a second authority over the button.
    assert sorted(movers) == ["_clips_render_current", "_clips_unavailable_outputs",
                              "_clips_working_frame",
                              "_on_clips_refresh", "_on_clips_scan"], movers
    assert sorted(enablers) == ["_clips_render_current", "_on_clips_scan"], (
        f"only these may enable Confirm, and both from the record: {enablers}")
    for disable_only in ("_clips_unavailable_outputs", "_clips_working_frame"):
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == disable_only)
        body = ast.unparse(node)
        assert "interactive=False" in body and "interactive=True" not in body, (
            f"{disable_only} may only ever DISABLE Confirm")
    for name in enablers:
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "counts[fork_clips.SELECTED] > 0" in ast.unparse(node), (
            f"{name} must derive the enabled state from the durable record")
    refresh = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "_on_clips_refresh")
    assert "interactive=False" in ast.unparse(refresh), (
        "an unusable session must still fail closed")


def test_confirm_sorts_the_page_into_the_two_destinations(world):
    """[UX V3] Confirm now does what its label says. Supersedes the P1-era
    `test_confirm_reports_and_moves_nothing`, which asserted the opposite because no sorter
    existed yet -- the decisions still come only from the durable record, which is the part that
    must not change."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    assert len(entries) >= 2, "this fixture needs at least two clips"
    keep, bin_ = entries[0], entries[1]
    payload = {entry.clip_id: open(entry.path, "rb").read() for entry in (keep, bin_)}

    namespace["_clips_decide"](owner, view["session"], view["page"], keep,
                               {"to": "SELECTED", "rev": 0})
    namespace["_clips_decide"](owner, view["session"], view["page"], bin_,
                               {"to": "REJECTED", "rev": 0})
    for entry in entries[2:]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "REJECTED", "rev": 0})

    message = namespace["_on_clips_confirm"]("", world["request"](owner))[4]

    kept = os.path.join(world["library"], os.path.basename(keep.path))
    binned = os.path.join(world["rejected"], os.path.basename(bin_.path))
    assert os.path.isfile(kept), message
    assert os.path.isfile(binned), message
    assert not os.path.exists(keep.path), "a sorted clip leaves its old location"
    assert not os.path.exists(bin_.path)
    assert open(kept, "rb").read() == payload[keep.clip_id], "content must survive the move"
    assert open(binned, "rb").read() == payload[bin_.clip_id]
    assert "Sorted" in message, message


def test_confirm_never_overwrites_an_occupied_destination_name(world):
    """A taken name leaves the clip exactly where it is, with the occupant untouched."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    victim = view["entries"][0]
    os.makedirs(world["library"], exist_ok=True)
    occupant = os.path.join(world["library"], os.path.basename(victim.path))
    with open(occupant, "wb") as handle:
        handle.write(b"i was here first")
    original = open(victim.path, "rb").read()

    for entry in view["entries"]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "SELECTED", "rev": 0})
    message = namespace["_on_clips_confirm"]("", world["request"](owner))[4]

    assert open(occupant, "rb").read() == b"i was here first", "the occupant was overwritten"
    assert os.path.isfile(victim.path), "the clip must stay put when its name is taken"
    assert open(victim.path, "rb").read() == original
    assert "already taken" in message, message
    # and no invented name was used to dodge the collision
    stem, ext = os.path.splitext(os.path.basename(victim.path))
    assert [n for n in os.listdir(world["library"]) if n.startswith(stem)] == \
        [os.path.basename(victim.path)], "a collision must never be renamed around"


def test_confirm_skips_a_clip_that_changed_after_the_scan(world):
    """A file edited during review is not sorted on a decision that may no longer describe it."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    moved_on = view["entries"][0]
    for entry in view["entries"]:
        namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                   {"to": "SELECTED", "rev": 0})
    with open(moved_on.path, "ab") as handle:        # same name, different bytes
        handle.write(b"appended after the scan")

    message = namespace["_on_clips_confirm"]("", world["request"](owner))[4]

    assert os.path.isfile(moved_on.path), "the changed clip must stay where it is"
    assert "changed after it was scanned" in message, message
    assert not os.path.exists(os.path.join(world["library"],
                                           os.path.basename(moved_on.path)))


def test_confirm_with_undecided_clips_shows_the_gate_and_moves_nothing(world):
    """[FORK] R8 Final Polish -- replaces `test_confirm_refuses_while_any_clip_is_undecided`.

    Undecided clips no longer REFUSE a Confirm; they open a gate. The property the old test
    protected is unchanged and is what is asserted here: on that first press **nothing moves**.
    What changed is that the operator is now shown the consequence and offered Proceed/Cancel,
    instead of being told to go and click 194 more clips.
    """
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": "SELECTED", "rev": 0})

    result = namespace["_on_clips_confirm"]("", world["request"](owner))
    message, proceed, cancel, signature = result[4], result[5], result[6], result[7]

    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in message, message
    assert proceed == {"visible": True} and cancel == {"visible": True}, (proceed, cancel)
    assert signature, "the gate must arm the exact batch it showed"

    for entry in view["entries"]:
        assert os.path.isfile(entry.path), "the gate must move nothing at all"
    assert not os.path.exists(world["library"])
    assert not os.path.exists(world["rejected"])


def test_proceed_sweeps_the_unmarked_clips_to_discarded_and_deletes_nothing(world):
    """[FORK] R8 Final Polish: the second half of the gate, end to end on a real filesystem.

    One clip is kept and the rest are left undecided. The first Confirm shows the gate and moves
    nothing; re-pressing with the signature it armed sweeps the unmarked clips into Discarded.
    Every file is still on disk afterwards, just in a different folder -- "never permanently
    delete" is the promise the gate is asking the operator to rely on.
    """
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    assert len(entries) >= 2, "this fixture needs at least two clips"
    keep = entries[0]
    unmarked = entries[1:]
    payload = {entry.clip_id: open(entry.path, "rb").read() for entry in entries}

    namespace["_clips_decide"](owner, view["session"], view["page"], keep,
                               {"to": "SELECTED", "rev": 0})

    # First press: the gate. Nothing moves.
    gated = namespace["_on_clips_confirm"]("", world["request"](owner))
    signature = gated[7]
    assert signature and gated[5] == {"visible": True}
    for entry in entries:
        assert os.path.isfile(entry.path), "the gate moved a file"

    # Second press, carrying the signature the gate armed: the sweep proceeds.
    swept = namespace["_on_clips_confirm"](signature, world["request"](owner))
    message = swept[4]

    kept_path = os.path.join(world["library"], os.path.basename(keep.path))
    assert os.path.isfile(kept_path), message
    assert open(kept_path, "rb").read() == payload[keep.clip_id]
    for entry in unmarked:
        binned = os.path.join(world["rejected"], os.path.basename(entry.path))
        assert os.path.isfile(binned), f"{entry.path} was not swept: {message}"
        assert open(binned, "rb").read() == payload[entry.clip_id], "content survived the move"
        assert not os.path.exists(entry.path), "a swept clip leaves its old location"
    assert "Nothing was deleted" in message, message
    # the gate closes and the arming is spent
    assert swept[5] == {"visible": False} and swept[7] == ""


def test_a_signature_from_a_different_batch_cannot_authorise_a_sweep(world):
    """The signature is a change detector, not a credential. Presenting a stale one must simply
    fail to match a freshly derived plan, which shows the gate again rather than sweeping."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    namespace["_clips_decide"](owner, view["session"], view["page"], entries[0],
                               {"to": "SELECTED", "rev": 0})

    result = namespace["_on_clips_confirm"]("not-the-signature-for-this-batch",
                                            world["request"](owner))
    assert world["ui"]["CLIPS_SWEEP_GATE_HEADING"] in result[4], result[4]
    for entry in entries:
        assert os.path.isfile(entry.path), "a forged signature moved a file"
    assert not os.path.exists(world["library"])
    assert not os.path.exists(world["rejected"])


def test_cancelling_the_sweep_changes_absolutely_nothing(world):
    """Cancel closes the gate, clears the arming, and touches neither a decision nor a file."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    namespace["_clips_decide"](owner, view["session"], view["page"], entries[0],
                               {"to": "SELECTED", "rev": 0})
    before = {entry.path: open(entry.path, "rb").read() for entry in entries}

    namespace["_on_clips_confirm"]("", world["request"](owner))
    cancelled = namespace["_on_clips_sweep_cancel"](world["request"](owner))

    assert world["ui"]["CLIPS_SWEEP_CANCELLED_TEXT"] in cancelled[4]
    assert cancelled[5] == {"visible": False} and cancelled[6] == {"visible": False}
    assert cancelled[7] == "", "Cancel must clear the arming"
    for path, payload in before.items():
        assert os.path.isfile(path), "Cancel moved a file"
        assert open(path, "rb").read() == payload
    assert not os.path.exists(world["library"])
    assert not os.path.exists(world["rejected"])
    # and the decision the operator had already made is untouched
    snapshot = namespace["_clips_store"]().snapshot_of(namespace["_clips_identities"](view))
    assert snapshot[view["identity"][entries[0].clip_id]][0] == "SELECTED"


def test_the_sweep_never_writes_a_decision_to_the_durable_record(world):
    """"For this batch only" is the contract. A swept clip must stay PENDING in the store, so a
    re-scan shows it undecided rather than as a rejection the operator never made."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    entries = list(view["entries"])
    namespace["_clips_decide"](owner, view["session"], view["page"], entries[0],
                               {"to": "SELECTED", "rev": 0})
    identities = namespace["_clips_identities"](view)
    unmarked_identity = view["identity"][entries[1].clip_id]

    signature = namespace["_on_clips_confirm"]("", world["request"](owner))[7]
    namespace["_on_clips_confirm"](signature, world["request"](owner))

    snapshot = namespace["_clips_store"]().snapshot_of(identities)
    assert snapshot.get(unmarked_identity, ("PENDING", 0))[0] == "PENDING", (
        "the sweep wrote a REJECTED decision the operator never made")


def test_confirm_refuses_when_no_clip_is_kept(world):
    """The admission rule that replaced "everything decided": at least one clip must be KEPT."""
    namespace = world["ns"]
    owner = "browser-session-1"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    namespace["_clips_decide"](owner, view["session"], view["page"], view["entries"][0],
                               {"to": "REJECTED", "rev": 0})

    result = namespace["_on_clips_confirm"]("", world["request"](owner))
    assert world["ui"]["CLIPS_CONFIRM_DISABLED_TEXT"].split(".")[0] in result[4], result[4]
    assert result[5] == {"visible": False}, "a refusal must not open the gate"
    assert result[7] == "", "a refusal must arm nothing"
    for entry in view["entries"]:
        assert os.path.isfile(entry.path), "a refused Confirm moves nothing at all"
    assert not os.path.exists(world["library"])
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


# =============================================================================================
# [R5-P9 / N5] A failed optional compaction must not destroy a scan that already succeeded
# =============================================================================================

class _StoreGate:
    """The real store, with `maybe_compact` made to pause or misbehave on demand."""

    def __init__(self, real, error=None, gate=None):
        self._real = real
        self._error = error
        self._gate = gate
        self.calls = 0

    def __getattr__(self, item):
        return getattr(self._real, item)

    def maybe_compact(self):
        self.calls += 1
        if self._gate is not None:
            self._gate()
        if self._error is not None:
            raise self._error
        return self._real.maybe_compact()


def _prime_for_real_compaction(namespace):
    """Make the next `maybe_compact()` actually compact, so a real failure point is reached."""
    store = namespace["_clips_store"]()
    store.journal_records = cr.COMPACT_AFTER_RECORDS
    return store


def _assert_served(result, world, owner):
    """The scan was served: a real grid, live credentials, and Confirm derived from the record."""
    report, grid, counts, confirm = result
    session, page = _session_of(grid)
    assert session and page, f"no grid was served: {report[:160]!r}"
    view = world["ns"]["_clips_view"](owner)
    assert view.get("session") == session, "the served grid is not the authoritative page"
    entries = world["ns"]["_CLIPS_REGISTRY"].page_entries(session, page)
    assert entries, "the served page's credentials must be live"
    return report, grid, counts, confirm


@pytest.mark.parametrize("where", ["snapshot", "truncation"])
def test_a_failed_compaction_still_serves_the_scan_that_already_succeeded(
        world, monkeypatch, where):
    """Both compaction failure points, through the real handler.

    Compaction runs *after* the scan has published a page and minted live credentials, and it was
    outside the handler's `try`. An `OSError` from it therefore threw away a scan that had
    entirely succeeded -- the operator lost a gallery they were already entitled to, for a failure
    that costs nothing. `snapshot` fails before the snapshot lands; `truncation` fails after it
    has landed, which is the opposite end of the same operation.
    """
    owner = "browser-compaction"
    store = _prime_for_real_compaction(world["ns"])
    real_replace, real_open = os.replace, open

    def refuse_replace(src, dst, *args, **kwargs):
        if str(dst) == store.snapshot_path:
            raise OSError(13, "snapshot refused", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    def refuse_truncate(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(file) == store.journal_path and "w" in str(mode):
            raise OSError(13, "truncation refused", str(file))
        return real_open(file, *args, **kwargs)

    if where == "snapshot":
        monkeypatch.setattr(cr.os, "replace", refuse_replace)
    else:
        monkeypatch.setattr(cr, "open", refuse_truncate, raising=False)

    result = _scan(world, owner)
    monkeypatch.undo()

    report, grid, _, _ = _assert_served(result, world, owner)

    # The operator is told, honestly, and without a path.
    assert "compaction" in report.lower(), f"the failure must be reported: {report!r}"
    assert "errno 13" in report, "the diagnosable numbers must survive"
    stripped = lambda text: text.replace("\\", "").replace("/", "")      # noqa: E731
    for secret in (store.journal_path, store.snapshot_path, os.path.dirname(store.journal_path)):
        assert stripped(secret) not in stripped(report), \
            f"a filesystem path reached the operator: {report!r}"


def test_a_failed_compaction_leaves_every_committed_decision_recoverable(world, monkeypatch):
    """The journal stays authoritative, which is the whole reason the failure is survivable."""
    owner = "browser-compaction-2"
    first = _scan(world, owner)
    session, page = _session_of(first[1])
    entries = world["ns"]["_CLIPS_REGISTRY"].page_entries(session, page)
    store = world["ns"]["_clips_store"]()
    decided = world["ns"]["_clips_decide"](
        owner, session, page, entries[0], {"to": cr.SELECTED, "rev": 0})
    assert decided["ok"], decided

    _prime_for_real_compaction(world["ns"])
    real_replace = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst) == store.snapshot_path:
            raise OSError(13, "snapshot refused", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cr.os, "replace", refuse)
    second = _scan(world, owner)
    monkeypatch.undo()
    _assert_served(second, world, owner)

    identity = cr.decision_identity(entries[0].path, entries[0].size, entries[0].mtime_ns)
    reloaded = cr.ReviewStore(store.dir)
    assert reloaded.get(identity) == (cr.SELECTED, 1), \
        "a committed decision must survive the failed compaction and a restart"


def test_a_compaction_failure_before_the_scan_transaction_is_still_fail_closed(world):
    """Containment is scoped to the *optional* compaction, not to the scan's own durability.

    `begin_scan` appends to the journal inside the authority block. If that cannot be recorded the
    scan has not happened, and the handler must still report an honest empty state.
    """
    owner = "browser-fail-closed"
    store = world["ns"]["_clips_store"]()
    original = store.begin_scan

    def refuse(*args, **kwargs):
        raise cr.PersistenceError("could not verify the review journal tail (errno 13)")

    store.begin_scan = refuse
    try:
        report, grid, counts, confirm = _scan(world, owner)
    finally:
        store.begin_scan = original

    assert grid == world["ui"]["CLIPS_GALLERY_INITIAL_HTML"], "nothing may be loaded"
    assert "failed" in report.lower()
    assert confirm == {"interactive": False}
    assert world["ns"]["_clips_view"](owner) == {}, "no page may be left looking current"


def test_a_programming_error_in_compaction_is_not_swallowed(world):
    """Only the two I/O classes are contained. A `TypeError` is a bug and must still surface."""
    owner = "browser-bug"
    real = world["ns"]["_clips_store"]()
    world["ns"]["_CLIPS_STORE"] = _StoreGate(real, error=TypeError("a real bug"))
    with pytest.raises(TypeError):
        _scan(world, owner)


# =============================================================================================
# [R5-P9 / N4] No reader may describe a revoked session as current, mid-transition
# =============================================================================================

def test_no_reader_describes_a_revoked_session_as_current_during_a_scan(tmp_path, ui):
    """The mandatory deterministic test.

    Scan B is paused *after* the authority block -- so `new_session` has already revoked A's
    session -- and *before* it publishes its own view. While it is paused, a different thread
    drives the real Refresh and `/r5state` handlers. Neither may describe A's dead page as
    current. Then B is released and its page must be served and classifiable.
    """
    media = _tree_of_clips(tmp_path)
    reached, release = threading.Event(), threading.Event()

    def hook(entries):
        if threading.current_thread().name == "scan-B":
            reached.set()
            assert release.wait(30), "the test's own gate timed out"

    namespace = _build(str(tmp_path / "state"), ui, poster_hook=hook)
    owner = "browser-session-N4"
    request = _Gr.Request(session_hash=owner)

    first = namespace["_on_clips_scan"](
        media, True, str(tmp_path / "lib"), str(tmp_path / "rej"), 100, request)
    a_session, a_page = _session_of(first[1])
    assert a_session, "the first scan must have produced a real page"

    box = {}

    def run_b():
        box["B"] = namespace["_on_clips_scan"](
            media, True, str(tmp_path / "lib"), str(tmp_path / "rej"), 100, request)

    thread = threading.Thread(target=run_b, name="scan-B")
    thread.start()
    try:
        assert reached.wait(30), "scan B never reached the poster pass"

        # A's credentials really are dead by now -- that is what makes serving them a defect.
        with pytest.raises(cr.Refusal):
            namespace["_CLIPS_REGISTRY"].page_entries(a_session, a_page)

        assert namespace["_clips_view"](owner) == {}, \
            "the authoritative view still named A's revoked session"

        grid, counts, confirm = namespace["_on_clips_refresh"](request)
        assert _session_of(grid) == (None, None), "Refresh repainted a revoked page"
        assert grid == ui["CLIPS_GALLERY_INITIAL_HTML"]
        assert confirm == {"interactive": False}

        state = namespace["_clips_state"](owner, a_session, a_page)
        assert state == {"ok": False, "code": "not_the_active_page"}, \
            "/r5state answered for a revoked page"
    finally:
        release.set()
        thread.join(30)

    # Released: B's page is now the current one, and it is usable.
    b_session, b_page = _session_of(box["B"][1])
    assert b_session and b_session != a_session
    assert namespace["_clips_view"](owner)["session"] == b_session
    entries = namespace["_CLIPS_REGISTRY"].page_entries(b_session, b_page)
    decided = namespace["_clips_decide"](
        owner, b_session, b_page, entries[0], {"to": cr.SELECTED, "rev": 0})
    assert decided["ok"], f"the newest page must be classifiable: {decided}"


def test_a_failed_newest_scan_leaves_nothing_looking_current(world):
    """A scan that takes a generation and then fails must not leave the older page on screen."""
    owner = "browser-N4-failed"
    first = _scan(world, owner)
    assert _session_of(first[1])[0]

    store = world["ns"]["_clips_store"]()
    original = store.begin_scan
    store.begin_scan = lambda *a, **k: (_ for _ in ()).throw(
        cr.PersistenceError("could not open the review journal (errno 13)"))
    try:
        failed = _scan(world, owner)
    finally:
        store.begin_scan = original

    assert failed[1] == world["ui"]["CLIPS_GALLERY_INITIAL_HTML"]
    assert world["ns"]["_clips_view"](owner) == {}
    assert _session_of(world["ns"]["_on_clips_refresh"](world["request"](
        session_hash=owner))[0]) == (None, None)

    # And the operator recovers simply by scanning again.
    again = _scan(world, owner)
    assert _session_of(again[1])[0], "a later healthy scan must restore a usable gallery"


def test_a_refused_folder_does_not_invalidate_a_valid_current_page(world):
    """A validation refusal returns before `begin`, so it must change nothing at all."""
    owner = "browser-N4-refused"
    first = _scan(world, owner)
    session, page = _session_of(first[1])

    refused = _scan(world, owner, folder=str(world["tmp"] / "does-not-exist"))
    assert "❌" in refused[0]

    assert world["ns"]["_clips_view"](owner).get("session") == session, \
        "a refused scan blanked a page it never superseded"
    assert world["ns"]["_CLIPS_REGISTRY"].page_entries(session, page)
    assert _session_of(world["ns"]["_on_clips_refresh"](
        world["request"](session_hash=owner))[0]) == (session, page)


# =============================================================================================
# [R5-P9 / N1] A late scan response must render the authoritative page, not its own locals
# =============================================================================================

def _paused_after_publication(world, owner):
    """Start a scan and stop it between `publish` and the response it renders."""
    paused, release, armed = threading.Event(), threading.Event(), {"on": True}

    def gate():
        if armed["on"] and threading.current_thread().name == "scan-A":
            armed["on"] = False
            paused.set()
            assert release.wait(30), "the test's own gate timed out"

    world["ns"]["_CLIPS_STORE"] = _StoreGate(world["ns"]["_clips_store"](), gate=gate)
    box = {}

    def run():
        box["A"] = _scan(world, owner)

    thread = threading.Thread(target=run, name="scan-A")
    thread.start()
    assert paused.wait(30), "scan A never reached the post-publication pause"
    return thread, release, box


def test_a_scan_superseded_after_publishing_returns_the_newest_page(world):
    """Interleaving 1 and 2: A publishes, B publishes, then A assembles its response.

    A's own `entries`/`session_id`/`page_token` are revoked by now. Rendering from them is the F1
    symptom returning by a later route, so the response is rendered from the authoritative view.
    """
    owner = "browser-N1"
    thread, release, box = _paused_after_publication(world, owner)
    a_session = world["ns"]["_clips_view"](owner)["session"]

    b_report, b_grid, _, _ = _scan(world, owner)         # B overtakes and completes
    b_session, b_page = _session_of(b_grid)
    assert b_session and b_session != a_session

    release.set()
    thread.join(30)
    a_report, a_grid, a_counts, a_confirm = box["A"]

    assert _session_of(a_grid) == (b_session, b_page), \
        "the superseded response handed the browser its own revoked credentials"
    assert world["ns"]["_CLIPS_REGISTRY"].page_entries(b_session, b_page), \
        "whatever A returned must carry live credentials"
    assert a_report == world["ui"]["CLIPS_SCAN_SUPERSEDED_TEXT"], \
        "the report must describe the page actually being shown"
    assert world["ns"]["_clips_view"](owner)["session"] == b_session


def test_the_superseded_response_counts_and_confirm_describe_the_page_it_shows(world):
    """Interleaving 3: A's response arrives last. Everything in it must describe B's page."""
    owner = "browser-N1-consistent"
    thread, release, box = _paused_after_publication(world, owner)
    b = _scan(world, owner)
    b_session, b_page = _session_of(b[1])
    release.set()
    thread.join(30)
    a_report, a_grid, a_counts, a_confirm = box["A"]

    entries = world["ns"]["_CLIPS_REGISTRY"].page_entries(b_session, b_page)
    store = world["ns"]["_clips_store"]()
    identities = [cr.decision_identity(e.path, e.size, e.mtime_ns) for e in entries]
    expected = cr.counts_text(store.counts(identities))
    assert a_counts == expected, "the counters describe a different page from the grid"
    assert a_confirm == {"interactive": store.counts(identities)[cr.PENDING] == 0}
    # Arriving last is therefore safe: the grid, the counters and Confirm all describe B's page.
    assert _session_of(a_grid) == (b_session, b_page) == _session_of(b[1])


def test_a_scan_superseded_by_one_that_then_fails_shows_an_honest_empty_state(world):
    """Interleaving 4: B supersedes A and then fails. Neither page may look current."""
    owner = "browser-N1-bfails"
    thread, release, box = _paused_after_publication(world, owner)
    a_session = world["ns"]["_clips_view"](owner)["session"]

    store = world["ns"]["_CLIPS_STORE"]
    original = store._real.begin_scan
    store._real.begin_scan = lambda *a, **k: (_ for _ in ()).throw(
        cr.PersistenceError("could not open the review journal (errno 13)"))
    try:
        failed = _scan(world, owner)
    finally:
        store._real.begin_scan = original
    assert failed[1] == world["ui"]["CLIPS_GALLERY_INITIAL_HTML"]

    release.set()
    thread.join(30)
    a_report, a_grid, _, a_confirm = box["A"]

    assert _session_of(a_grid) == (None, None), \
        "A resurrected its own page after a newer scan had already revoked it"
    assert a_grid == world["ui"]["CLIPS_GALLERY_INITIAL_HTML"]
    assert a_confirm == {"interactive": False}
    assert world["ns"]["_clips_view"](owner) == {}
    assert a_session, "A really had published before being superseded"

    # Not a permanently dead gallery: the next scan restores one.
    assert _session_of(_scan(world, owner)[1])[0]


def test_two_owners_do_not_supersede_each_other_after_publication(world):
    """Interleaving 5: A and B belong to different browsers and must not interact."""
    owner_a, owner_b = "browser-N1-a", "browser-N1-b"
    thread, release, box = _paused_after_publication(world, owner_a)
    a_session = world["ns"]["_clips_view"](owner_a)["session"]

    other = _scan(world, owner_b)
    b_session, _ = _session_of(other[1])

    release.set()
    thread.join(30)
    a_grid = box["A"][1]

    assert _session_of(a_grid)[0] == a_session, \
        "another owner's scan superseded this one"
    assert b_session != a_session
    assert world["ns"]["_clips_view"](owner_a)["session"] == a_session
    assert world["ns"]["_clips_view"](owner_b)["session"] == b_session


def test_the_scan_handler_renders_from_the_authoritative_view(world):
    """Structural: the grid is built from the published view, not from the scan's own locals.

    A behavioural test can only catch the interleavings it schedules; this catches a future edit
    that quietly goes back to the locals, whatever the timing.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    handler = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == "_on_clips_scan")

    assignments = [ast.unparse(node) for node in ast.walk(handler)
                   if isinstance(node, ast.Assign)]
    assert "current = _clips_view(owner)" in assignments, \
        "the handler must re-read the authoritative view before rendering"

    calls = [node for node in ast.walk(handler) if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "fork_clips.gallery_html"]
    assert len(calls) == 1, f"expected exactly one gallery_html call, found {len(calls)}"
    args = [ast.unparse(argument) for argument in calls[0].args]
    assert args[0] == "current['entries']"
    assert args[2] == "current['identity']"
    assert args[3] == "current['session']"
    assert args[4] == "current['page']"
    for stale in ("entries", "identity", "session_id", "page_token"):
        assert stale not in args, (
            f"the grid is still built from the scan's local {stale!r}; a scan superseded after "
            "publishing would hand the browser revoked credentials")


# =============================================================================================
# [R5-P11 / N7] An unreadable durable record must refuse honestly, not look like a clean folder
#
# `ReviewStore` now raises `PersistenceError` rather than returning a store that recovered only
# part of the record. Three things follow for this layer, and each was wrong before:
#
# * **Scan** obtains the store *before* `ScanCoordinator.begin()`, so a refused recovery takes no
#   generation, opens no review session, revokes nothing and never starts the poster pass.
# * **The refusal is not cached.** `_CLIPS_STORE` is assigned from a local that only exists once
#   the constructor returned, so a passing exclusive share does not become a process-lifetime
#   outage -- and a half-loaded store never becomes the one every later caller writes through.
# * **Nothing reports success.** A grid of PENDING tiles, "Undecided 0", or a readiness report
#   derived from a record that could not be read are all false statements with the authority of
#   true ones. Refresh has no status box of its own, so its gallery carries the message.
#
# The underlying defect was permanent loss of committed decisions, reproduced on the frozen
# baseline with a real Windows exclusive-sharing lock and with controlled injection; see
# `test_clips_review.py`'s N7 section for the storage-level contract.

def _seed_decisions(state_dir, count=3):
    """Commit `count` decisions into the GUI's own state directory, before any handler runs."""
    store = cr.ReviewStore(state_dir)
    store.begin_scan("seed-owner", "seed-scan")
    expected = {}
    for index in range(count):
        identity = f"seeded-{index}"
        expected[identity] = store.decide("seed-owner", identity, cr.SELECTED, "seed-scan")
    return store, expected


def _deny_journal_reads(monkeypatch, journal):
    """`open` that refuses READ opens of exactly this journal and delegates for every other.

    Narrow by the convention this suite and `test_clips_review.py` both follow: a global patch
    would break the poster cache and the descriptor-identity probe, and the handler would then
    fail for a reason that is not the one under test.
    """
    real = open
    target = os.path.normcase(os.path.abspath(journal))

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            raise PermissionError(13, "access denied", str(file), 32)
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)


def _durable_bytes(store):
    out = {}
    for name, path in (("snapshot", store.snapshot_path), ("journal", store.journal_path)):
        try:
            with open(path, "rb") as handle:
                out[name] = handle.read()
        except FileNotFoundError:
            out[name] = None
    return out


def _leaks_path(text, secret):
    """Separator-stripped containment (R5-P7): `str(OSError)` reprs its filenames, so a plain
    `in` test passes against text that really is disclosing the path."""
    strip = lambda value: value.replace("\\", "").replace("/", "")       # noqa: E731
    return strip(secret) in strip(text)


def test_n7_case16_scan_refuses_and_mints_nothing_when_the_record_cannot_be_read(
        world, monkeypatch):
    """Case 16. Scan must refuse before it changes any authoritative state.

    The ordering is the containment: `_clips_store()` is called before `begin()`, so a refusal
    cannot supersede a running scan, cannot blank an existing page, cannot open a review session
    against a record the server could not read, and cannot start the poster pass.
    """
    state_dir = str(world["tmp"] / "state")
    seed, expected = _seed_decisions(state_dir)
    before = _durable_bytes(seed)
    namespace = world["ns"]
    assert namespace["_CLIPS_STORE"] is None, "the store is created lazily"

    _deny_journal_reads(monkeypatch, seed.journal_path)
    report, grid, counts, confirm = _scan(world, "browser-n7-scan")
    monkeypatch.undo()

    assert "could NOT be read" in report, report
    assert "try again" in report.lower(), "the operator is told the retry is the next step"
    assert confirm == {"interactive": False}
    assert "r5-tile" not in grid, "not one clip may be painted as undecided"
    assert "Nothing scanned yet" not in grid, (
        "the placeholder would read as a folder nobody has reviewed")
    assert "could NOT be read" in grid, "Refresh has no status box, so the gallery must say it"
    assert "unavailable" in counts.lower(), f"the counters must not read as decided: {counts!r}"

    # Nothing authoritative moved, and nothing was cached.
    assert namespace["_CLIPS_STORE"] is None, "a refusal must not be cached"
    assert namespace["_clips_view"]("browser-n7-scan") == {}
    assert namespace["_CLIPS_SCANS"].stats()["generations_issued"] == 0, (
        "no generation may be taken, or a refusal could supersede a healthy running scan")
    assert namespace["_CLIPS_REGISTRY"].stats()["sessions"] == 0, "no session was opened"
    assert _durable_bytes(seed) == before, "a refused scan writes nothing"

    # And the healthy retry works, with every seeded decision intact.
    retried = namespace["_clips_store"]()
    for identity, value in expected.items():
        assert retried.get(identity) == value, "the seeded decisions survived the refusal"
    served = _scan(world, "browser-n7-scan")
    assert _session_of(served[1])[0], f"the retry must serve a real grid: {served[0][:200]!r}"


def test_n7_case17_refresh_and_confirm_refuse_rather_than_report_a_clean_page(world,
                                                                              monkeypatch):
    """Case 17. The two read-only surfaces, after a page is already on screen.

    `_CLIPS_STORE` is set back to `None` to model the lazy store not yet existing -- which is the
    only state in which `_clips_store()` reads the disk, and therefore the only state in which an
    unreadable record can reach these handlers. The view is untouched, so the page still has
    entries: without the refusal, Refresh would repaint every decided clip as PENDING and Confirm
    would report "0 kept, 0 discarded" about a record it could not read.
    """
    namespace = world["ns"]
    owner = "browser-n7-refresh"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    assert view.get("entries"), "the page must be live before the record becomes unreadable"
    store = namespace["_clips_store"]()
    for entry in view["entries"]:
        decided = namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                             {"to": cr.SELECTED, "rev": 0})
        assert decided["ok"], decided

    # Control: with the record readable, these two surfaces report the decided page.
    healthy_grid, healthy_counts, healthy_confirm = namespace["_on_clips_refresh"](
        world["request"](session_hash=owner))
    assert healthy_confirm == {"interactive": True}, "the control arm must really be positive"
    assert "Kept 3" in healthy_counts, healthy_counts
    # [UX V3] The healthy arm deliberately no longer calls Confirm: Confirm now SORTS the page,
    # so using it as a read-only control would move the very clips whose record this test is
    # about to make unreadable. Refresh above is the positive control.

    before = _durable_bytes(store)
    namespace["_CLIPS_STORE"] = None
    _deny_journal_reads(monkeypatch, store.journal_path)

    grid, counts, confirm = namespace["_on_clips_refresh"](world["request"](session_hash=owner))
    confirmation = namespace["_on_clips_confirm"]("", world["request"](session_hash=owner))[4]
    monkeypatch.undo()

    assert confirm == {"interactive": False}, "Confirm must fail closed"
    assert "r5-tile" not in grid, "a decided clip may never be repainted as undecided"
    assert "could NOT be read" in grid, "Refresh's only surface for this is the gallery"
    assert "unavailable" in counts.lower(), counts
    assert "could NOT be read" in confirmation, confirmation
    for forbidden in ("NOT enabled", "Recorded:", "are still undecided"):
        assert forbidden not in confirmation, (
            f"an unreadable record must not produce a readiness claim ({forbidden})")
    assert namespace["_CLIPS_STORE"] is None, "neither surface cached the refusal"
    assert _durable_bytes(store) == before

    # The record is still there, and the next Refresh paints the real decisions again.
    grid, counts, confirm = namespace["_on_clips_refresh"](world["request"](session_hash=owner))
    assert confirm == {"interactive": True}, "recovery is automatic once the file is readable"
    assert "Kept 3" in counts, counts


def test_n7_the_store_factory_never_caches_a_refusal(world, monkeypatch):
    """The one site that could turn a passing lock into a process-lifetime outage.

    Caching the exception would wedge the tab until the app restarted; caching a half-loaded
    store would re-arm the loss path, because every later caller would be appending to a record
    the disk does not hold.
    """
    namespace = world["ns"]
    state_dir = str(world["tmp"] / "state")
    seed, expected = _seed_decisions(state_dir)

    _deny_journal_reads(monkeypatch, seed.journal_path)
    for _ in range(3):
        with pytest.raises(cr.PersistenceError):
            namespace["_clips_store"]()
        assert namespace["_CLIPS_STORE"] is None, "nothing may be cached by a failed attempt"
    monkeypatch.undo()

    store = namespace["_clips_store"]()
    assert namespace["_CLIPS_STORE"] is store, "a complete recovery IS cached"
    assert namespace["_clips_store"]() is store, "and reused"
    for identity, value in expected.items():
        assert store.get(identity) == value


def test_n7_no_operator_visible_refusal_discloses_a_filesystem_path(world, monkeypatch):
    """Every surface the refusal reaches: the report, the gallery, the counters and Confirm.

    `_clips_failure_text` reduces a bare `OSError` to `errno`/`winerror`, and `PersistenceError`
    is path-free by the R5-P7 contract -- but the message is assembled here, so it is checked
    here, separator-blind.
    """
    namespace = world["ns"]
    state_dir = str(world["tmp"] / "state")
    seed, _ = _seed_decisions(state_dir)
    secrets_to_keep = (seed.journal_path, seed.snapshot_path, state_dir,
                       os.path.basename(state_dir))

    owner = "browser-n7-redaction"
    _scan(world, owner)
    namespace["_CLIPS_STORE"] = None
    _deny_journal_reads(monkeypatch, seed.journal_path)
    report, grid, counts, confirm = _scan(world, owner)
    refresh = namespace["_on_clips_refresh"](world["request"](session_hash=owner))
    confirmation = namespace["_on_clips_confirm"]("", world["request"](session_hash=owner))[4]
    monkeypatch.undo()

    surfaces = [report, grid, counts, confirmation, refresh[0], refresh[1]]
    for text in surfaces:
        for secret in secrets_to_keep:
            assert not _leaks_path(text, secret), f"a path reached the operator: {text[:200]!r}"
        assert "review-journal" not in text and "review-snapshot" not in text
        assert ".jsonl" not in text
    assert "errno 13" in report, "the condition must stay diagnosable without the path"
    assert confirm == {"interactive": False}


def test_n7_the_compaction_notice_no_longer_claims_the_journal_holds_everything(world,
                                                                                monkeypatch):
    """[R5-P11] The R5-P9 reassurance was inaccurate and an independent review said so.

    It told the operator every decision already made "is still in the journal". After a
    successful compaction the journal is empty and the snapshot is the authoritative copy, so that
    is not universally true -- and the confirmed N7 path could compact incomplete memory into a
    replacement snapshot, which made it false in the one case where it mattered most. The
    corrected wording promises recoverability from both copies, which is what the storage contract
    actually guarantees.
    """
    owner = "browser-n7-wording"
    store = _prime_for_real_compaction(world["ns"])
    real_replace = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst) == store.snapshot_path:
            raise OSError(13, "snapshot refused", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cr.os, "replace", refuse)
    report, grid, _, _ = _scan(world, owner)
    monkeypatch.undo()

    assert "compaction" in report.lower(), report
    assert "still in it" not in report, "the inaccurate reassurance must be gone"
    assert "journal is the" not in report, (
        "and so must the claim that the journal alone is the record")
    assert "recoverable from the snapshot plus the journal" in report, report
    assert _session_of(grid)[0], "the scan that succeeded is still served"


def test_n7_the_scan_handler_reads_the_store_before_it_takes_a_generation():
    """Structural, because the ordering is what makes the refusal harmless.

    A behavioural test can only catch the interleavings it schedules. If `_clips_store()` were
    moved after `begin()`, a refusal would mint a generation -- which, by the N4 rule, makes any
    existing page stop being current -- and would then never publish one, so an operator with a
    live gallery would lose it to someone else's unreadable disk.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    scan = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "_on_clips_scan")
    body = ast.unparse(scan)
    assert body.index("_clips_store()") < body.index("_CLIPS_SCANS.begin("), (
        "the durable record must be obtained BEFORE a scan generation is taken")
    assert body.index("_clips_store()") < body.index("ensure_posters"), (
        "and before the poster pass")
    assert "_clips_unavailable_outputs" in body, "the refusal must be reported, not raised"


def test_n7_the_decision_and_state_routes_let_the_503_contract_answer():
    """Structural. These two answer HTTP, and the middleware already maps the persistence failure
    to a path-free 503 `not_durably_recorded`. Catching it here to return a JSON body would make
    an unreadable record indistinguishable from an ordinary refusal -- or, worse, report success.

    Asserted over the **exception handlers in the AST**, not over the unparsed text. The two
    functions legitimately *explain* in prose why they do not catch it, and a substring test
    would be tripped by the explanation -- which has now happened six times in this repository.
    """
    tree = ast.parse(open(_GUI, encoding="utf-8").read())
    for name in ("_clips_decide", "_clips_state"):
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == name)
        caught = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.ExceptHandler) and inner.type is not None:
                targets = (inner.type.elts if isinstance(inner.type, ast.Tuple)
                           else [inner.type])
                caught.update(ast.unparse(target).rsplit(".", 1)[-1] for target in targets)
        assert "_clips_store()" in ast.unparse(node), f"{name} must use the lazy store"
        assert "PersistenceError" not in caught, (
            f"{name} caught the persistence failure instead of letting 503 answer: {caught}")
        assert "OSError" not in caught, f"{name} must not contain a bare I/O failure either"
        assert "_clips_unavailable_outputs" not in ast.unparse(node), (
            f"{name} returns JSON, not GUI outputs")


# ---- R5-P13 / N7-R1 + N7-R2: the same refusal, for state that is READABLE but unaccountable ---
#
# The R5-P11 handler contract above was measured against files that could not be *read*. R5-P12
# found the other half still open: a readable snapshot or journal that this build cannot account
# for used to produce a store that looked healthy, reported every decided clip as undecided, and
# then compacted the loss into the snapshot. The storage layer now refuses those too, with the same
# `PersistenceError`, so these handlers need no new code -- which is the claim these tests make.
#
# Note what is NOT injected here: nothing. The state on disk is genuinely corrupt, so this is the
# strongest form of this evidence available without a browser.


def _corrupt_a_committed_journal_line(store):
    """Damage one *historical* committed decision, leaving the later ones valid and readable.

    Terminated damage, deliberately: that is the shape R5-P12 reproduced, and the shape that used
    to be skipped silently rather than refused.
    """
    with open(store.journal_path, "rb") as handle:
        lines = handle.read().splitlines(keepends=True)
    assert len(lines) >= 4, lines
    lines[2] = b"{ damaged committed record\n"
    with open(store.journal_path, "wb") as handle:
        handle.write(b"".join(lines))
    return lines


def test_n7r2_scan_refuses_for_a_readable_but_unaccountable_record(world):
    """Scan, against real corruption rather than a denied read.

    Every assertion of case 16 has to hold for this condition too, because the operator-visible
    consequence is identical: a grid of undecided tiles over a folder that was reviewed.
    """
    state_dir = str(world["tmp"] / "state")
    seed, expected = _seed_decisions(state_dir)
    namespace = world["ns"]
    _corrupt_a_committed_journal_line(seed)
    before = _durable_bytes(seed)
    assert namespace["_CLIPS_STORE"] is None

    report, grid, counts, confirm = _scan(world, "browser-n7r2-scan")

    assert "could NOT be read" in report, report
    assert confirm == {"interactive": False}
    assert "r5-tile" not in grid, "not one clip may be painted as undecided"
    assert "Nothing scanned yet" not in grid
    assert "unavailable" in counts.lower(), counts
    assert namespace["_CLIPS_STORE"] is None, "a refusal must not be cached"
    assert namespace["_CLIPS_SCANS"].stats()["generations_issued"] == 0, (
        "no authoritative generation may be minted from an unaccountable record")
    assert namespace["_CLIPS_REGISTRY"].stats()["sessions"] == 0
    assert _durable_bytes(seed) == before, "a refused scan writes nothing, so nothing is lost"

    for secret in (seed.journal_path, seed.snapshot_path, state_dir):
        for text in (report, grid, counts):
            assert not _leaks_path(text, secret), f"a path reached the operator: {text[:200]!r}"
    assert "review-journal" not in report and ".jsonl" not in report


def test_n7r2_refresh_and_confirm_refuse_for_a_readable_but_unaccountable_record(world):
    """Refresh and Confirm, with a live page on screen and real corruption underneath it.

    The control arm runs first, so the refusal is measured against a page that genuinely did
    report "Kept 3" a moment earlier.
    """
    namespace = world["ns"]
    owner = "browser-n7r2-refresh"
    _scan(world, owner)
    view = namespace["_clips_view"](owner)
    assert view.get("entries")
    store = namespace["_clips_store"]()
    for entry in view["entries"]:
        assert namespace["_clips_decide"](owner, view["session"], view["page"], entry,
                                          {"to": cr.SELECTED, "rev": 0})["ok"]

    healthy_grid, healthy_counts, healthy_confirm = namespace["_on_clips_refresh"](
        world["request"](session_hash=owner))
    assert healthy_confirm == {"interactive": True}, "the control arm must really be positive"
    assert "Kept 3" in healthy_counts, healthy_counts

    _corrupt_a_committed_journal_line(store)
    before = _durable_bytes(store)
    namespace["_CLIPS_STORE"] = None

    grid, counts, confirm = namespace["_on_clips_refresh"](world["request"](session_hash=owner))
    confirmation = namespace["_on_clips_confirm"]("", world["request"](session_hash=owner))[4]

    assert confirm == {"interactive": False}, "Confirm must fail closed"
    assert "r5-tile" not in grid, "a decided clip may never be repainted as undecided"
    assert "could NOT be read" in grid
    assert "unavailable" in counts.lower(), counts
    assert "could NOT be read" in confirmation, confirmation
    for forbidden in ("NOT enabled", "Recorded:", "are still undecided"):
        assert forbidden not in confirmation, f"no readiness claim may be made ({forbidden})"
    assert namespace["_CLIPS_STORE"] is None, "neither surface cached the refusal"
    assert _durable_bytes(store) == before


def test_n7r2_an_unaccountable_snapshot_also_refuses_every_surface(world):
    """The N7-R1 shape at the handler boundary: a compacted store with a damaged snapshot.

    After compaction the journal is empty, so this is the case where discarding the snapshot
    discards the whole record -- and the GUI would have shown it as a folder nobody had reviewed.
    """
    state_dir = str(world["tmp"] / "state")
    seed, expected = _seed_decisions(state_dir)
    seed.compact()
    assert os.path.getsize(seed.journal_path) == 0
    snapshot = json.loads(open(seed.snapshot_path, encoding="utf-8").read())
    snapshot["records"]["damaged-entry"] = {"revision": 1}
    with open(seed.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    before = _durable_bytes(seed)
    namespace = world["ns"]

    report, grid, counts, confirm = _scan(world, "browser-n7r1-scan")
    assert "could NOT be read" in report, report
    assert confirm == {"interactive": False}
    assert "r5-tile" not in grid
    assert namespace["_CLIPS_STORE"] is None
    assert _durable_bytes(seed) == before, "the only copy of the record is left untouched"

    # Remove the one damaged entry and every seeded decision comes back, from the same bytes.
    snapshot["records"].pop("damaged-entry")
    with open(seed.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    recovered = namespace["_clips_store"]()
    for identity, value in expected.items():
        assert recovered.get(identity) == value, "the refusal preserved a real record"
