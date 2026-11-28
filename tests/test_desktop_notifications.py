"""R17-A: desktop notifications for a finished long job.

Four tiers, deliberately kept apart because each proves a different kind of thing:

1. **The pure policy**, `beatsync_fork.desktop_notify`, imported and executed as ordinary code.
   Everything about what a notification may *say* lives here, including the content-safety rule,
   so it is tested exhaustively rather than by example.
2. **The stored preference**, through the real `user_prefs` store on a real temporary file --
   including the hazard a second option key introduced, which is that `save_preferences` fills any
   option the caller omits with its built-in default.
3. **The server half of the GUI seam**, by AST-extracting the shipped ledger, publisher, drain and
   the six outcome translators out of `src/gui.py` and executing them against a synthesised
   namespace. These are real assertions about shipped code: the session isolation, the
   consume-once rule, the delivery gate and every outcome mapping are *run*, not inspected.
4. **The client half, structurally.** The browser is the only authority on
   `Notification.permission`, so what can be asserted here is the shape of the scripts: `fn=None`
   on every registration, exactly one route to `requestPermission()`, no network call of any kind,
   no service worker and no timer. **Whether a notification actually appears on Windows is not
   asserted anywhere in this suite** -- that is a live smoke test, and the task report records it
   separately.
"""

from __future__ import annotations

import ast
import itertools
import json
import os
import re
import threading
import types
import uuid
from collections import OrderedDict

import pytest

from beatsync_fork import collision as fork_collision
from beatsync_fork import desktop_notify as dn
from beatsync_fork import library_prep as fork_prep
from beatsync_fork import prep_activity as fork_prep_activity
from beatsync_fork import raw_cutter_batch as fork_cutter_batch
from beatsync_fork import user_prefs as prefs
from beatsync_fork.render_worker import RenderOutcomeKind

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")
_MODULE = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "desktop_notify.py")

_ID = "a" * 32


def _source(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _tree(path: str) -> ast.Module:
    return ast.parse(_source(path), filename=path)


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


# =============================================================================================
# 1. The pure policy
# =============================================================================================

def test_every_job_kind_has_a_label_and_every_outcome_a_verb():
    """The two vocabularies and their wording tables are one thing, not two that can drift."""
    assert set(dn.JOB_KINDS) == set(dn.JOB_LABELS)
    assert set(dn.OUTCOMES) == set(dn.OUTCOME_VERBS) == set(dn.OUTCOME_BODIES)
    assert len(dn.OUTCOMES) == 4, "the accepted scope names exactly four terminal outcomes"
    assert set(dn.COUNT_FIELDS) == set(dn.COUNT_WORDS)


def test_the_four_outcomes_are_worded_distinctly():
    """Partial success is a first-class answer, not a decorated success.

    If two outcomes shared a verb, a batch that lost a source would read exactly like one that
    did not -- which is the specific dishonesty the four-way split exists to prevent.
    """
    verbs = [dn.OUTCOME_VERBS[outcome] for outcome in dn.OUTCOMES]
    assert len(set(verbs)) == 4, verbs
    assert len({dn.OUTCOME_BODIES[outcome] for outcome in dn.OUTCOMES}) == 4


@pytest.mark.parametrize("job", dn.JOB_KINDS)
@pytest.mark.parametrize("outcome", dn.OUTCOMES)
def test_build_produces_a_titled_bodied_tagged_record(job, outcome):
    record = dn.build(job, outcome, notification_id=_ID)
    assert record is not None
    assert record["id"] == _ID
    assert record["job"] == job
    assert record["outcome"] == outcome
    assert dn.JOB_LABELS[job] in record["title"]
    assert dn.OUTCOME_VERBS[outcome] in record["title"]
    assert record["title"].startswith(dn.APP_NAME)
    assert record["tag"] == dn.TAG_PREFIX + _ID
    # With no counts the body is the outcome's own sentence rather than a truncated headline.
    assert record["body"] == dn.OUTCOME_BODIES[outcome]


def test_counts_are_spoken_in_a_fixed_order_so_one_job_reads_the_same_way_twice():
    record = dn.build(dn.JOB_RAW_CUTTER_BATCH, dn.OUTCOME_PARTIAL, notification_id=_ID,
                      counts={"failed": 1, "completed": 27})
    assert record["body"].endswith("27 completed, 1 failed")
    again = dn.build(dn.JOB_RAW_CUTTER_BATCH, dn.OUTCOME_PARTIAL, notification_id=_ID,
                     counts={"completed": 27, "failed": 1})
    assert again["body"] == record["body"]


def test_a_supplied_zero_is_reported_and_an_absent_count_is_not():
    """`0 completed` after a batch of forty is information; suppressing it would make a total
    failure look like a job with nothing to say."""
    assert dn.detail_text({"completed": 0, "failed": 40}) == "0 completed, 40 failed"
    assert dn.detail_text({"completed": 3}) == "3 completed"
    assert dn.detail_text({}) == ""
    assert dn.detail_text(None) == ""


@pytest.mark.parametrize("value", [1.5, 2.0, True, False, -1, "3", None, float("nan")])
def test_a_count_that_is_not_a_whole_non_negative_int_is_dropped_not_guessed(value):
    """An operator reading `3.0 completed` would rightly distrust the whole line, and a clamped
    negative would report a number nobody measured."""
    assert dn.detail_text({"completed": value}) == ""


def test_an_unknown_job_kind_or_outcome_announces_nothing():
    """"Do not announce success while the result is unknown" is a refusal, not a fallback."""
    assert dn.build("not_a_job", dn.OUTCOME_SUCCESS, notification_id=_ID) is None
    assert dn.build(dn.JOB_RENDER, "nearly_finished", notification_id=_ID) is None
    assert dn.build(None, None, notification_id=_ID) is None
    assert dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id="") is None
    assert dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id="short") is None
    assert dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id="a b" * 4) is None
    assert dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id=None) is None


def test_no_composed_notification_can_contain_anything_that_reads_as_a_location():
    """Exhaustive over every job, every outcome and a count matrix, because this is the one
    property the Windows lock screen makes expensive to get wrong.

    The real guarantee is structural -- no caller may pass text in at all -- and this is the
    independent check on top of it.
    """
    matrices = [
        {},
        {"completed": 0},
        {"completed": 999999, "failed": 999999},
        {field: 7 for field in dn.COUNT_FIELDS},
    ]
    for job, outcome, counts in itertools.product(dn.JOB_KINDS, dn.OUTCOMES, matrices):
        record = dn.build(job, outcome, notification_id=_ID, counts=counts)
        assert record is not None, (job, outcome, counts)
        for field in ("title", "body"):
            assert dn.is_safe_text(record[field]), record
            for bad in dn.FORBIDDEN_CHARACTERS:
                assert bad not in record[field], (field, bad, record[field])


def test_build_takes_no_text_argument_at_all():
    """The structural half of the content guarantee, asserted on the signature.

    A source path, a media filename or an FFmpeg stderr line has no parameter to arrive through,
    which is stronger than sanitising a string a caller handed in.
    """
    node = _func(_tree(_MODULE), "build")
    positional = [arg.arg for arg in node.args.args]
    keyword_only = [arg.arg for arg in node.args.kwonlyargs]
    assert positional == ["job_kind", "outcome"]
    assert sorted(keyword_only) == ["counts", "notification_id"]
    assert node.args.vararg is None and node.args.kwarg is None


@pytest.mark.parametrize("text", [
    r"C:\Users\me\Videos\clip.mp4",
    "/home/me/clip.mp4",
    "file:///C:/out",
    "http://127.0.0.1:7860",
    "ffmpeg exited 1\nstderr: boom",
    "",
    "   ",
    None,
    123,
    "x" * 161,
])
def test_is_safe_text_refuses_anything_path_like_log_like_or_oversized(text):
    assert not dn.is_safe_text(text)


def test_is_safe_text_accepts_the_wording_the_module_itself_ships():
    for outcome in dn.OUTCOMES:
        assert dn.is_safe_text(dn.OUTCOME_BODIES[outcome])
    assert dn.is_safe_text(dn.TEST_TITLE)
    assert dn.is_safe_text(dn.TEST_BODY)


def test_the_pure_module_reads_no_clock_no_randomness_and_no_filesystem():
    """It mints no identity and performs no side effect, which is what makes every branch above
    testable without stubbing anything."""
    tree = _tree(_MODULE)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported == {"__future__", "json"}, imported
    rendered = ast.unparse(tree)
    for forbidden in ("uuid", "random", "time.", "datetime", "os.", "open(", "hashlib",
                      "subprocess", "threading"):
        assert forbidden not in rendered, forbidden


# =============================================================================================
# 1b. The envelope
# =============================================================================================

def test_an_empty_list_produces_the_empty_value_the_dispatcher_ignores():
    assert dn.envelope([]) == dn.ENVELOPE_EMPTY == ""
    assert dn.envelope(None) == dn.ENVELOPE_EMPTY
    assert dn.decode_envelope(dn.ENVELOPE_EMPTY) == []


def test_the_envelope_round_trips_every_record_in_order():
    records = [dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id="b" * 32),
               dn.build(dn.JOB_RAW_CUTTER, dn.OUTCOME_PARTIAL, notification_id="c" * 32,
                        counts={"completed": 2, "failed": 1})]
    text = dn.envelope(records)
    payload = json.loads(text)
    assert payload["v"] == dn.ENVELOPE_VERSION
    assert dn.decode_envelope(text) == records
    assert [item["id"] for item in payload["items"]] == ["b" * 32, "c" * 32]


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    json.dumps({"v": dn.ENVELOPE_VERSION + 1, "items": [{"id": "x"}]}),
    json.dumps({"items": [{"id": "x"}]}),
    json.dumps({"v": dn.ENVELOPE_VERSION, "items": "nope"}),
    None,
    7,
])
def test_an_unrecognised_envelope_decodes_to_nothing_rather_than_raising(text):
    """A page served from a cache must not misread a payload it does not understand."""
    assert dn.decode_envelope(text) == []


# =============================================================================================
# 1c. The stored toggle, the permission vocabulary and the outcome fold
# =============================================================================================

def test_the_default_is_off_because_the_feature_is_opt_in():
    assert dn.ENABLED_DEFAULT == dn.ENABLED_OFF
    assert prefs.OPTION_DEFAULTS[prefs.DESKTOP_NOTIFICATIONS] == dn.ENABLED_OFF
    assert not dn.is_enabled(prefs.OPTION_DEFAULTS[prefs.DESKTOP_NOTIFICATIONS])


@pytest.mark.parametrize("value,expected", [
    ("on", True), ("ON", True), ("  On  ", True),
    ("off", False), ("", False), ("   ", False),
    ("true", False), ("yes", False), ("1", False),
    (True, False), (1, False), (None, False), ([], False), ({}, False), (object(), False),
])
def test_only_the_exact_token_reads_as_on(value, expected):
    """A corrupt, hand-edited or half-written value must read as OFF. A notification setting that
    failed open would announce an operator's job titles to a desktop they never agreed to."""
    assert dn.is_enabled(value) is expected


def test_option_value_round_trips_through_is_enabled():
    assert dn.is_enabled(dn.option_value(True)) is True
    assert dn.is_enabled(dn.option_value(False)) is False
    assert dn.option_value(1) == dn.ENABLED_ON
    assert dn.option_value(0) == dn.ENABLED_OFF


def test_the_browser_default_is_reported_as_not_requested_and_never_as_denied():
    """They are very different things to tell an operator, and only one of them is a decision."""
    assert dn.BROWSER_PERMISSION_STATES["default"] == dn.PERMISSION_NOT_REQUESTED
    assert dn.BROWSER_PERMISSION_STATES["granted"] == dn.PERMISSION_GRANTED
    assert dn.BROWSER_PERMISSION_STATES["denied"] == dn.PERMISSION_DENIED
    assert set(dn.BROWSER_PERMISSION_STATES.values()) <= set(dn.PERMISSION_STATES)
    assert dn.PERMISSION_UNSUPPORTED not in dn.BROWSER_PERMISSION_STATES.values()


@pytest.mark.parametrize("state", list(dn.PERMISSION_STATES) + ["", None, "prompt", 7])
def test_permission_sentence_is_total_and_fails_towards_unsupported(state):
    sentence = dn.permission_sentence(state)
    assert sentence in dn.PERMISSION_SENTENCES.values()
    if state not in dn.PERMISSION_STATES:
        assert sentence == dn.PERMISSION_SENTENCES[dn.PERMISSION_UNSUPPORTED]


def test_the_denied_sentence_never_claims_beatsync_changed_a_chrome_permission():
    """BeatSync cannot revoke or restore a per-origin permission and must not imply that it did;
    changing it is a Chrome Site Settings action, so that is what the sentence says."""
    denied = dn.PERMISSION_SENTENCES[dn.PERMISSION_DENIED]
    assert "Site Settings" in denied
    assert "has not changed it" in denied


def test_the_granted_sentence_promises_no_guaranteed_alert():
    granted = dn.PERMISSION_SENTENCES[dn.PERMISSION_GRANTED]
    assert "not a guarantee" in granted
    assert "Focus Assist" in granted


def test_the_app_setting_sentence_distinguishes_the_two_states():
    assert dn.app_setting_sentence(True) != dn.app_setting_sentence(False)
    assert "OFF" in dn.app_setting_sentence(False)
    assert "whatever Chrome allows" in dn.app_setting_sentence(False)


@pytest.mark.parametrize("completed,failed,cancelled,expected", [
    (3, 0, False, dn.OUTCOME_SUCCESS),
    (0, 0, False, dn.OUTCOME_SUCCESS),
    (3, 1, False, dn.OUTCOME_PARTIAL),
    (0, 4, False, dn.OUTCOME_FAILED),
    (0, 1, False, dn.OUTCOME_FAILED),
    (3, 1, True, dn.OUTCOME_CANCELLED),
    (0, 0, True, dn.OUTCOME_CANCELLED),
    (None, None, False, dn.OUTCOME_SUCCESS),
])
def test_the_count_fold_calls_a_total_loss_a_failure_and_lets_cancellation_win(
        completed, failed, cancelled, expected):
    """A batch where every single item failed is a FAILURE; calling it partial would be the
    cheerful reading of a total loss. A stopped job's counts describe how far it got."""
    assert dn.outcome_from_counts(completed, failed, cancelled=cancelled) == expected


# =============================================================================================
# 2. The stored preference, through the real store
# =============================================================================================

def test_a_preferences_file_written_before_r17a_loads_with_notifications_off(tmp_path):
    """APPENDED, never inserted: `load_options` fills a missing key from `OPTION_DEFAULTS`, so an
    existing file needs no migration and nothing an operator already saved changes meaning."""
    path = str(tmp_path / "preferences.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schema": prefs.SCHEMA,
                   "folders": {key: "" for key in prefs.KEYS},
                   "options": {prefs.COLLISION_POLICY: "suffix"}}, handle)
    options = prefs.load_options(path)
    assert options[prefs.COLLISION_POLICY] == "suffix"
    assert options[prefs.DESKTOP_NOTIFICATIONS] == dn.ENABLED_OFF
    assert not dn.is_enabled(options[prefs.DESKTOP_NOTIFICATIONS])


def test_the_toggle_persists_and_reloads(tmp_path):
    path = str(tmp_path / "preferences.json")
    assert prefs.save_preferences(path, prefs.empty_preferences(),
                                  {prefs.COLLISION_POLICY: "skip",
                                   prefs.DESKTOP_NOTIFICATIONS: dn.ENABLED_ON}) == ""
    assert dn.is_enabled(prefs.load_options(path)[prefs.DESKTOP_NOTIFICATIONS])


def test_a_partial_options_dict_defaults_every_key_it_omits(tmp_path):
    """The hazard a second option key introduced, pinned as the real behaviour of the store.

    `save_preferences` replaces the whole file, so a caller that names one option resets the
    others. That is why every GUI option writer must pass the COMPLETE set -- asserted
    structurally in section 3 below.
    """
    path = str(tmp_path / "preferences.json")
    prefs.save_preferences(path, prefs.empty_preferences(),
                           {prefs.COLLISION_POLICY: "skip",
                            prefs.DESKTOP_NOTIFICATIONS: dn.ENABLED_ON})
    assert dn.is_enabled(prefs.load_options(path)[prefs.DESKTOP_NOTIFICATIONS])

    prefs.save_preferences(path, prefs.empty_preferences(),
                           {prefs.COLLISION_POLICY: "suffix"})
    reloaded = prefs.load_options(path)
    assert reloaded[prefs.COLLISION_POLICY] == "suffix"
    assert reloaded[prefs.DESKTOP_NOTIFICATIONS] == dn.ENABLED_OFF, \
        "a partial options dict defaults the rest -- this is the behaviour the GUI must work with"


def test_options_none_still_means_leave_every_option_alone(tmp_path):
    """The pre-existing contract, re-pinned now that there are two options to lose."""
    path = str(tmp_path / "preferences.json")
    prefs.save_preferences(path, prefs.empty_preferences(),
                           {prefs.COLLISION_POLICY: "suffix",
                            prefs.DESKTOP_NOTIFICATIONS: dn.ENABLED_ON})
    prefs.save_preferences(path, prefs.empty_preferences())
    reloaded = prefs.load_options(path)
    assert reloaded[prefs.COLLISION_POLICY] == "suffix"
    assert dn.is_enabled(reloaded[prefs.DESKTOP_NOTIFICATIONS])


def test_the_notification_option_is_not_a_folder_and_never_reaches_the_folder_store():
    assert prefs.DESKTOP_NOTIFICATIONS not in prefs.KEYS
    assert prefs.DESKTOP_NOTIFICATIONS not in prefs.DESTINATION_KEYS
    assert prefs.DESKTOP_NOTIFICATIONS not in prefs.LAST_USED_KEYS
    assert prefs.DESKTOP_NOTIFICATIONS in prefs.OPTION_KEYS
    # The store must not acquire a dependency on the policy that reads it. Checked STRUCTURALLY,
    # over the import statements, because `user_prefs.py` legitimately names `desktop_notify` in
    # prose to explain that it deliberately does not import it -- and a substring ban would ban
    # the explanation. (This repository has been bitten by exactly that, repeatedly.)
    tree = _tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "user_prefs.py"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not any("desktop_notify" in name for name in imported), imported


# =============================================================================================
# 3. The server half of the GUI seam, EXECUTED
# =============================================================================================

#: Every module-level name the extracted helpers need resolved from `gui.py` itself.
#: [FORK] R17-A R2 (F1/F3): `_CUT_BATCH_NOTIFY_OUTCOMES` is gone. The cutter batch's four terminal
#: classes now split two ways -- the two that describe the RUN are translated, the two that mean
#: "ran to the end" are folded from the measured counts -- and the analysis pass gained its own
#: verdict table.
_WANTED_ASSIGNS = ("_NOTIFY_LOCK", "_NOTIFY_PENDING", "NOTIFY_SESSION_CAP", "NOTIFY_QUEUE_CAP",
                   "_NOTIFY_RENDER_OUTCOMES", "_CUT_BATCH_RUN_OUTCOMES", "_CUT_BATCH_RAN_TO_END",
                   "_PREP_NOTIFY_JOBS", "_PREP_ANALYSIS_NOTIFY_OUTCOMES",
                   "NOTIFY_STATUS_ELEMENT_ID",
                   # [FORK] R17-A R3: the diagnostic acknowledgement line's element id, which
                   # `_notify_status_panel_html` now renders a second, empty div for.
                   "NOTIFY_FEEDBACK_ELEMENT_ID")

_WANTED_FUNCTIONS = ("_notify_session_key", "_notifications_enabled", "_notify_publish",
                     "_notify_drain", "_notify_render", "_notify_render_batch", "_notify_cut",
                     "_notify_cut_batch", "_notify_audio_cut", "_notify_library",
                     "_notify_settings_on_load", "_notify_status_panel_html",
                     "_on_notifications_enabled_change",
                     # [FORK] R17-A R2 (F2): Reset defaults is now a writer of the notification
                     # checkbox and the sentence beside it, so the suite executes the real body.
                     "_on_reset_folder_prefs",
                     # [FORK] Digital-Union (Settings Preference Consistency R1): the page-load
                     # reader is now settings-wide and composes `_notify_settings_on_load`, and
                     # Reset's failure path reads the policy that is still in force through
                     # `_collision_policy`. Both are extracted so the shipped bodies still run
                     # here rather than being stubbed.
                     "_settings_on_load", "_collision_policy")


class _Request:
    """What Gradio injects: an object whose only interesting attribute is the session hash."""

    def __init__(self, session_hash):
        self.session_hash = session_hash


class _HostileRequest:
    """A request-like object whose attribute access raises.

    Not a hypothetical: `_notify_publish` runs on a finished render's terminal path, and the one
    thing it may never do is take that render's final frame down with it.
    """

    @property
    def session_hash(self):
        raise RuntimeError("no")


def _notify_namespace(tmp_path, *, enabled=True):
    """Execute the SHIPPED notification bridge against a synthesised namespace."""
    tree = _tree(_GUI)
    body = [node for node in tree.body
            if (isinstance(node, (ast.Assign, ast.AnnAssign))
                and getattr(getattr(node, "target", None), "id",
                            getattr(node.targets[0], "id", "") if isinstance(node, ast.Assign)
                            else "") in _WANTED_ASSIGNS)]
    found = {getattr(getattr(node, "target", None), "id", None)
             or getattr(node.targets[0], "id", None) for node in body}
    assert found == set(_WANTED_ASSIGNS), f"missing from gui.py: {set(_WANTED_ASSIGNS) - found}"
    body += [_func(tree, name) for name in _WANTED_FUNCTIONS]

    options = {prefs.COLLISION_POLICY: "skip",
               prefs.DESKTOP_NOTIFICATIONS: dn.option_value(enabled)}
    # [FORK] Digital-Union (Settings Preference Consistency R1, F2): a real folder dictionary
    # rather than a literal, because Reset's failure path now reads it back through `_pref` and a
    # test that cannot tell a preserved value from a blank one would pass against the defect.
    folders = prefs.empty_preferences()
    toasts: list = []
    namespace: dict = {
        "threading": threading,
        "OrderedDict": OrderedDict,
        "uuid": uuid,
        "html_lib": __import__("html"),
        "fork_notify": dn,
        "fork_prefs": prefs,
        # [FORK] Digital-Union (Settings Preference Consistency R1): the real policy module, so
        # `_collision_policy` resolves exactly as it does in the app.
        "fork_collision": fork_collision,
        "fork_cutter_batch": fork_cutter_batch,
        "fork_prep": fork_prep,
        "fork_prep_activity": fork_prep_activity,
        "RenderOutcomeKind": RenderOutcomeKind,
        "INFO_NOTIFY_STATUS_PENDING": "Checking this browser…",
        "STATUS_NOTIFY_SAVED_ON": "saved on",
        "STATUS_NOTIFY_SAVED_OFF": "saved off",
        "PREFS_PATH": str(tmp_path / "preferences.json"),
        "_FOLDER_PREFS": folders,
        "_FOLDER_OPTIONS": options,
        "_option": lambda key: options.get(key, prefs.OPTION_DEFAULTS.get(key, "")),
        # [FORK] Digital-Union (Settings Preference Consistency R1, F2): the same reader the nine
        # Settings boxes are CONSTRUCTED from, so what a failed reset repaints can be compared
        # against what the panel would have shown.
        "_pref": lambda key: folders.get(key, ""),
        # [FORK] R17-A R2 (F2): `_on_reset_folder_prefs` composes its status line through this.
        # Stubbed to the identity, because what the status *says* is R8's contract and this suite
        # asserts only about the two notification outputs beside it.
        "_folder_prefs_status": lambda text: text,
        "gr": types.SimpleNamespace(Info=lambda text: toasts.append(("info", text)),
                                    Warning=lambda text: toasts.append(("warning", text)),
                                    # The same shape the settings-on-load test installs, so a
                                    # programmatic component write is inspectable as plain data.
                                    update=lambda **kwargs: ("gr.update", kwargs)),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 _GUI, "exec"), namespace)      # noqa: S102 - the body under test
    namespace["_toasts"] = toasts
    namespace["_options"] = options
    namespace["_folders"] = folders
    return namespace


def _drained(namespace, session):
    return dn.decode_envelope(namespace["_notify_drain"](_Request(session)))


# --- the ledger -------------------------------------------------------------------------------

def test_one_publish_produces_exactly_one_notification_and_the_drain_consumes_it(tmp_path):
    ns = _notify_namespace(tmp_path)
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["job"] == dn.JOB_RENDER
    assert records[0]["outcome"] == dn.OUTCOME_SUCCESS
    # Consume-once: a reconnect, a repeated frame or a later refused click cannot re-deliver it.
    assert _drained(ns, "S1") == []
    assert ns["_notify_drain"](_Request("S1")) == dn.ENVELOPE_EMPTY


def test_every_notification_identity_is_unique_per_terminal_event(tmp_path):
    ns = _notify_namespace(tmp_path)
    for _ in range(25):
        ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    identities = [record["id"] for record in _drained(ns, "S1")]
    assert len(set(identities)) == len(identities)
    assert all(dn.is_valid_id(identity) for identity in identities)


def test_a_session_can_never_read_back_another_sessions_notification(tmp_path):
    """"Do not broadcast one operator's jobs to all tabs", as an executable property."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_publish"](_Request("OWNER"), dn.JOB_RAW_CUTTER, dn.OUTCOME_SUCCESS)
    assert _drained(ns, "OTHER") == []
    assert ns["_notify_drain"](_Request("")) == dn.ENVELOPE_EMPTY
    assert ns["_notify_drain"](None) == dn.ENVELOPE_EMPTY
    # ...and the owner's record is still there, untouched by the other session's drain.
    assert len(_drained(ns, "OWNER")) == 1


def test_an_unusable_session_publishes_nothing_rather_than_sharing_a_bucket(tmp_path):
    """`''` is a refusal, not a process-global slot -- which would be the cross-session leak the
    ledger exists to prevent."""
    ns = _notify_namespace(tmp_path)
    for request in (None, _Request(""), _Request(None), _HostileRequest()):
        ns["_notify_publish"](request, dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert ns["_NOTIFY_PENDING"] == {}


def test_the_publisher_cannot_raise_into_a_finished_jobs_terminal_path(tmp_path):
    """A notification is a courtesy; a job's result is not."""
    ns = _notify_namespace(tmp_path)
    assert ns["_notify_publish"](_HostileRequest(), dn.JOB_RENDER, dn.OUTCOME_SUCCESS) is None
    assert ns["_notify_publish"](_Request("S1"), "not_a_job", "not_an_outcome") is None
    assert ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS,
                                 completed=object()) is None
    # The last call carried an unusable count, which `detail_text` drops -- so a record still
    # exists and it simply reports no figures.
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["body"] == dn.OUTCOME_BODIES[dn.OUTCOME_SUCCESS]


def test_both_ledger_bounds_hold(tmp_path):
    ns = _notify_namespace(tmp_path)
    cap = ns["NOTIFY_QUEUE_CAP"]
    for _ in range(cap + 15):
        ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert len(ns["_NOTIFY_PENDING"]["S1"]) == cap

    sessions = ns["NOTIFY_SESSION_CAP"]
    for index in range(sessions + 20):
        ns["_notify_publish"](_Request(f"T{index}"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert len(ns["_NOTIFY_PENDING"]) <= sessions
    # Oldest-first eviction: an undelivered record whose session never came back is the one to go.
    assert "S1" not in ns["_NOTIFY_PENDING"]
    assert f"T{sessions + 19}" in ns["_NOTIFY_PENDING"]


def test_two_jobs_of_one_session_both_arrive(tmp_path):
    """A render and a Raw Cutter run are serialised by different mutexes, so one session really
    can have two terminal events pending. Dropping one would be the "no notification arrived"
    complaint this feature exists to fix."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    ns["_notify_publish"](_Request("S1"), dn.JOB_RAW_CUTTER, dn.OUTCOME_PARTIAL,
                          completed=2, failed=1)
    jobs = [record["job"] for record in _drained(ns, "S1")]
    assert jobs == [dn.JOB_RENDER, dn.JOB_RAW_CUTTER]


# --- the delivery gate ------------------------------------------------------------------------

def test_the_app_toggle_gates_delivery_and_discards_rather_than_hoarding(tmp_path):
    """"The BeatSync toggle disables delivery even if Chrome permission remains granted" is a
    statement about DELIVERY, so the gate sits at the moment of delivery -- and a record produced
    while it was on is dropped rather than held back for a later click to deliver out of context.
    """
    ns = _notify_namespace(tmp_path, enabled=False)
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert ns["_NOTIFY_PENDING"]["S1"], "the record is still made; only delivery is gated"
    assert ns["_notify_drain"](_Request("S1")) == dn.ENVELOPE_EMPTY
    assert ns["_NOTIFY_PENDING"] == {}, "and it is discarded, not queued for later"


def test_turning_the_toggle_on_persists_it_and_reports_the_new_state(tmp_path):
    ns = _notify_namespace(tmp_path, enabled=False)
    sentence = ns["_on_notifications_enabled_change"](True)
    assert sentence == dn.app_setting_sentence(True)
    assert ns["_options"][prefs.DESKTOP_NOTIFICATIONS] == dn.ENABLED_ON
    assert ns["_toasts"] == [("info", "saved on")]
    # ...and it reached disk through the real store.
    stored = prefs.load_options(ns["PREFS_PATH"])
    assert dn.is_enabled(stored[prefs.DESKTOP_NOTIFICATIONS])
    # ...and the drain honours it immediately, with no second button press.
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert len(_drained(ns, "S1")) == 1


def test_switching_the_toggle_preserves_the_other_stored_option(tmp_path):
    """The regression a second option key introduced: `save_preferences` defaults any option the
    caller omits, so picking a collision policy must not switch notifications back off and vice
    versa."""
    ns = _notify_namespace(tmp_path, enabled=False)
    ns["_options"][prefs.COLLISION_POLICY] = "suffix"
    ns["_on_notifications_enabled_change"](True)
    stored = prefs.load_options(ns["PREFS_PATH"])
    assert stored[prefs.COLLISION_POLICY] == "suffix"
    assert dn.is_enabled(stored[prefs.DESKTOP_NOTIFICATIONS])


def test_a_failed_save_says_so_and_still_applies_to_this_session(tmp_path):
    ns = _notify_namespace(tmp_path, enabled=False)
    # A directory that cannot be created, because a FILE already occupies its path.
    blocker = tmp_path / "blocked"
    blocker.write_bytes(b"not a directory")
    ns["PREFS_PATH"] = os.path.join(str(blocker), "preferences.json")
    sentence = ns["_on_notifications_enabled_change"](True)
    assert sentence == dn.app_setting_sentence(True)
    assert ns["_toasts"] and ns["_toasts"][-1][0] == "warning"
    assert "this session only" in ns["_toasts"][-1][1]


# --- the programmatic-write guard, and Reset defaults  (R17-A R2, finding F2) -----------------

def test_a_programmatic_echo_of_the_stored_setting_writes_nothing_and_says_nothing(tmp_path):
    """Gradio's `.change` fires for a FUNCTION UPDATE as well as a user gesture, and this component
    has two legitimate programmatic writers: `_notify_settings_on_load` (every new browser session)
    and Reset defaults. Both re-entered the handler, so opening a tab rewrote `preferences.json`
    and raised an "ON" toast for a page the operator had only just loaded."""
    ns = _notify_namespace(tmp_path, enabled=True)
    ns["_on_notifications_enabled_change"](True)                   # the real user gesture, once
    assert ns["_toasts"] == [], "the stored value already said on; nothing changed"
    assert not os.path.exists(ns["PREFS_PATH"]), "and nothing was written at all"

    # ...and from the other direction: a programmatic OFF echo over a stored OFF.
    off = _notify_namespace(tmp_path / "off", enabled=False)
    off["_on_notifications_enabled_change"](False)
    assert off["_toasts"] == []
    assert not os.path.exists(off["PREFS_PATH"])


def test_a_corrupt_stored_value_is_not_rewritten_by_a_programmatic_echo(tmp_path):
    """`is_enabled` reads a hand-edited value as OFF -- the direction this setting must fail in --
    so the comparison is on the resolved booleans and an echo of the off state it already means
    changes nothing."""
    ns = _notify_namespace(tmp_path, enabled=False)
    ns["_options"][prefs.DESKTOP_NOTIFICATIONS] = "YES PLEASE"
    assert ns["_on_notifications_enabled_change"](False) == dn.app_setting_sentence(False)
    assert ns["_toasts"] == [] and not os.path.exists(ns["PREFS_PATH"])
    # ...while a genuine request to turn it ON is still a change, and still persists.
    assert ns["_on_notifications_enabled_change"](True) == dn.app_setting_sentence(True)
    assert dn.is_enabled(prefs.load_options(ns["PREFS_PATH"])[prefs.DESKTOP_NOTIFICATIONS])


def test_reset_defaults_leaves_the_setting_the_checkbox_and_the_sentence_in_agreement(tmp_path):
    """Reset returns the option to `off` in the file AND in `_FOLDER_OPTIONS`, which is what the
    delivery gate reads -- so a panel still saying "Desktop notifications are ON in BeatSync" was
    the one thing in the room that was wrong about what the application would do."""
    ns = _notify_namespace(tmp_path, enabled=True)
    ns["_options"][prefs.COLLISION_POLICY] = "suffix"
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert len(_drained(ns, "S1")) == 1, "delivering before the reset"

    returned = ns["_on_reset_folder_prefs"]()

    assert returned[10] == ("gr.update", {"value": False}), returned[10]
    assert returned[11] == dn.app_setting_sentence(False)
    assert ns["_options"][prefs.DESKTOP_NOTIFICATIONS] == dn.ENABLED_OFF
    assert not dn.is_enabled(
        prefs.load_options(ns["PREFS_PATH"])[prefs.DESKTOP_NOTIFICATIONS])
    assert ns["_notifications_enabled"]() is False
    # ...and the gate agrees with the sentence: nothing is delivered any more.
    ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    assert _drained(ns, "S1") == []


def test_a_reset_that_could_not_be_saved_reports_the_setting_that_is_still_in_force(tmp_path):
    """A failed save leaves `_FOLDER_OPTIONS` untouched, so the pair must describe what is still
    true rather than the cleared state the operator was told could not be saved."""
    ns = _notify_namespace(tmp_path, enabled=True)
    blocker = tmp_path / "blocked"
    blocker.write_bytes(b"not a directory")
    ns["PREFS_PATH"] = os.path.join(str(blocker), "preferences.json")

    returned = ns["_on_reset_folder_prefs"]()

    assert returned[-1].startswith("❌")
    assert returned[10] == ("gr.update", {"value": True}), returned[10]
    assert returned[11] == dn.app_setting_sentence(True)
    assert ns["_notifications_enabled"]() is True, "nothing was cleared, so nothing changed"


def test_reset_defaults_returns_exactly_as_many_values_as_its_outputs_list(tmp_path):
    """Gradio matches `outputs` by position, so a tuple that grew without its registration -- or
    the other way round -- silently writes the status line into the checkbox."""
    ns = _notify_namespace(tmp_path, enabled=True)
    call = next(node for node in ast.walk(_tree(_GUI))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) == "pref_reset_btn.click")
    listed = next(keyword.value for keyword in call.keywords if keyword.arg == "outputs")
    outputs = [ast.unparse(element) for element in listed.elts]

    assert outputs[10:12] == ["notify_enabled", "notify_app_state"], outputs
    assert outputs[-1] == "pref_status", "the status box stays last"
    assert "notify_status" not in outputs, \
        "the live browser-permission line has no server-side writer"
    assert len(ns["_on_reset_folder_prefs"]()) == len(outputs)


def test_the_browser_status_placeholder_states_no_permission_state(tmp_path):
    """Only the browser knows its own `Notification.permission`, so a server-rendered `granted`
    would be a guess that goes stale the moment Chrome Site Settings changes."""
    ns = _notify_namespace(tmp_path)
    html = ns["_notify_status_panel_html"]()
    assert ns["NOTIFY_STATUS_ELEMENT_ID"] in html
    assert 'data-state="checking"' in html
    for state in dn.PERMISSION_STATES:
        assert state not in html, state


def test_the_diagnostic_feedback_line_ships_empty_and_the_server_never_fills_it(tmp_path):
    """[FORK] R17-A R3. The acknowledgement states what the operator's LAST CLICK did, so a page
    that has just loaded must show nothing at all -- a server-rendered "Test notification
    requested successfully" would be a claim about a browser action that never happened, and a
    *stale* one from a previous session at that.

    It is a second div inside the one component with no server-side writer, so the whole of the
    "the page owns this line" contract carries over unchanged.
    """
    ns = _notify_namespace(tmp_path)
    html = ns["_notify_status_panel_html"]()
    feedback_id = ns["NOTIFY_FEEDBACK_ELEMENT_ID"]
    assert feedback_id != ns["NOTIFY_STATUS_ELEMENT_ID"], "two lines, two ids"
    assert f'<div id="{feedback_id}" data-feedback="none"></div>' in html, html
    # Not one word of the acknowledgement vocabulary is server-rendered.
    for sentence in dn.TEST_FEEDBACK_SENTENCES.values():
        assert sentence not in html
    assert "Re-checked" not in html


# --- the outcome translators ------------------------------------------------------------------

@pytest.mark.parametrize("kind,expected", [
    (RenderOutcomeKind.SUCCESS, dn.OUTCOME_SUCCESS),
    (RenderOutcomeKind.CANCELLED, dn.OUTCOME_CANCELLED),
    (RenderOutcomeKind.SHARED_FATAL, dn.OUTCOME_FAILED),
    (RenderOutcomeKind.UNKNOWN_FATAL, dn.OUTCOME_FAILED),
    (RenderOutcomeKind.CANDIDATE_LOCAL, dn.OUTCOME_FAILED),
    (None, dn.OUTCOME_FAILED),
])
def test_a_single_renders_typed_outcome_is_translated_and_never_derived(tmp_path, kind, expected):
    """Everything that is not explicitly SUCCESS or CANCELLED is a failure, including an
    unclassified `None` -- the same conservative reading the wrapper applies to the lifecycle."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_render"](_Request("S1"), kind, True)
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["job"] == dn.JOB_RENDER
    assert records[0]["outcome"] == expected


@pytest.mark.parametrize("started", [False, None, ""])
def test_a_render_the_gate_refused_announces_nothing(tmp_path, started):
    """A gate refusal records `SHARED_FATAL` exactly as a real pipeline failure does -- correct for
    batch continuation, wrong for a notification. The click performed no work and the refusal is
    already on screen, so the render BODY having been entered is what decides."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_render"](_Request("S1"), RenderOutcomeKind.SHARED_FATAL, started)
    assert _drained(ns, "S1") == []


def test_the_render_started_marker_is_written_only_by_the_gate_core():
    """Three writes, all in `_process_video_guarded_unlocked`: cleared before the gate and set
    immediately before the render stream. One reader, the wrapper's terminal path. Nothing inside
    the pipeline reads it back and nothing branches on it."""
    tree = _tree(_GUI)
    writers, readers = set(), set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Assign) and inner.targets
                    and ast.unparse(inner.targets[0])
                    == "session_state[RENDER_STARTED_KEY]"):
                writers.add(node.name)
            if (isinstance(inner, ast.Call)
                    and "RENDER_STARTED_KEY" in ast.unparse(inner)):
                readers.add(node.name)
    assert writers == {"_process_video_guarded_unlocked"}, writers
    assert readers == {"process_video_guarded"}, readers

    core = ast.unparse(_func(tree, "_process_video_guarded_unlocked"))
    assert core.count("session_state[RENDER_STARTED_KEY] = False") == 1
    assert core.count("session_state[RENDER_STARTED_KEY] = True") == 1
    # The marker is set BEFORE the stream is constructed, so a fault inside `process_video` is
    # still a started render.
    assert (core.index("session_state[RENDER_STARTED_KEY] = True")
            < core.index("render_stream = process_video("))


class _Candidate:
    def __init__(self, success):
        self.success = success


class _BatchOutcome:
    def __init__(self, requested_count, successes, failures, outcome_kind=None):
        self.requested_count = requested_count
        self.outcomes = tuple([_Candidate(True)] * successes + [_Candidate(False)] * failures)
        self.outcome_kind = outcome_kind


@pytest.mark.parametrize("outcome,expected,detail", [
    (_BatchOutcome(4, 4, 0), dn.OUTCOME_SUCCESS, "4 completed, 0 failed"),
    (_BatchOutcome(4, 3, 1), dn.OUTCOME_PARTIAL, "3 completed, 1 failed"),
    (_BatchOutcome(4, 0, 4), dn.OUTCOME_FAILED, "0 completed, 4 failed"),
    (_BatchOutcome(4, 1, 1, RenderOutcomeKind.CANCELLED), dn.OUTCOME_CANCELLED,
     "1 completed, 1 failed, 2 not attempted"),
    (_BatchOutcome(4, 1, 1, RenderOutcomeKind.SHARED_FATAL), dn.OUTCOME_PARTIAL,
     "1 completed, 1 failed, 2 not attempted"),
])
def test_the_batch_reports_counts_and_takes_only_cancellation_from_its_typed_cause(
        tmp_path, outcome, expected, detail):
    """A completed batch's `outcome_kind` is deliberately `None` even carrying local failures, so
    reading it would report one success and one failure as an unqualified success."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_render_batch"](_Request("S1"), outcome)
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["job"] == dn.JOB_VARIANT_BATCH
    assert records[0]["outcome"] == expected
    assert records[0]["body"].endswith(detail), records[0]["body"]


def test_a_batch_that_attempted_everything_reports_no_not_attempted_figure(tmp_path):
    ns = _notify_namespace(tmp_path)
    ns["_notify_render_batch"](_Request("S1"), _BatchOutcome(2, 2, 0))
    assert "not attempted" not in _drained(ns, "S1")[0]["body"]


@pytest.mark.parametrize("kwargs,expected,detail", [
    ({"written": 27, "failed": 0}, dn.OUTCOME_SUCCESS, "27 completed, 0 failed"),
    ({"written": 27, "failed": 1}, dn.OUTCOME_PARTIAL, "27 completed, 1 failed"),
    ({"written": 0, "failed": 3}, dn.OUTCOME_FAILED, "0 completed, 3 failed"),
    ({"written": 4, "failed": 0, "cancelled": True}, dn.OUTCOME_CANCELLED,
     "4 completed, 0 failed"),
    ({"written": 2, "crashed": True}, dn.OUTCOME_PARTIAL, "2 completed"),
    ({"written": 0, "crashed": True}, dn.OUTCOME_FAILED, "0 completed"),
])
def test_the_single_cuts_outcome_matches_what_its_handoff_record_says(
        tmp_path, kwargs, expected, detail):
    """A crash that nevertheless left clips on disk is PARTIAL, exactly as the handoff record
    classifies it -- both are derived from the same `completed` list."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_cut"](_Request("S1"), **kwargs)
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["job"] == dn.JOB_RAW_CUTTER
    assert records[0]["outcome"] == expected
    assert records[0]["body"].endswith(detail), records[0]["body"]


@pytest.mark.parametrize("terminal,expected", [
    (fork_cutter_batch.TERMINAL_CANCELLED, dn.OUTCOME_CANCELLED),
    (fork_cutter_batch.TERMINAL_FAILED, dn.OUTCOME_FAILED),
])
def test_the_two_run_level_batch_classes_are_translated_not_folded(tmp_path, terminal, expected):
    """Cancellation and a crash are statements about the RUN that counts cannot express: a stopped
    batch's counts say how far it got, and a crashed one's say what had been done when it stopped.
    So these two come straight from the batch module's own classifier, counts notwithstanding."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_cut_batch"](_Request("S1"), terminal, completed=27, failed=1,
                            cancelled=0, not_attempted=0, no_clips=0, partial=0)
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["job"] == dn.JOB_RAW_CUTTER_BATCH
    assert records[0]["outcome"] == expected
    assert records[0]["body"].endswith("27 completed, 1 failed")


@pytest.mark.parametrize("terminal", [None, "", "completed_ish", "not_run"])
def test_a_folder_batch_that_never_started_announces_nothing(tmp_path, terminal):
    """A batch refused before admission, or before it obtained an output directory, has no
    outcome to announce -- and that is deliberately not a cancellation."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_cut_batch"](_Request("S1"), terminal, completed=0, failed=0)
    assert _drained(ns, "S1") == []


def test_zero_valued_batch_counts_are_omitted_rather_than_printed_as_noise(tmp_path):
    ns = _notify_namespace(tmp_path)
    ns["_notify_cut_batch"](_Request("S1"), fork_cutter_batch.TERMINAL_COMPLETED,
                            completed=5, failed=0, cancelled=0, not_attempted=0)
    body = _drained(ns, "S1")[0]["body"]
    assert "cancelled" not in body and "not attempted" not in body
    assert body.endswith("5 completed, 0 failed")


# =============================================================================================
# 3a-R2. The folder batch's outcome, driven by the REAL classifier  (R17-A R2, finding F1)
#
# `terminal_status` answers `completed_with_failures` for ANY failed source, which is right for a
# headline and wrong for a notification outcome: a batch whose every source failed arrives there
# too. These cases are built as real `BatchItem` lists, folded by the real `summarise`, classified
# by the real `terminal_status`, and only then handed to the translator -- so the expectations are
# measured against the shipped cutter rather than asserted about a table.
# =============================================================================================

def _batch_item(index, outcome, *, written=0, planned=0):
    return fork_cutter_batch.BatchItem(index=index, source_path=f"S{index}.mp4", outcome=outcome,
                                       clips_written=written, clips_planned=planned)


_B = fork_cutter_batch

#: (label, items, discovered, cancelled, fatal_error, manifest_error, expected outcome)
_F1_BATCHES = [
    # The defect. Three unreadable sources, no clips anywhere: a total loss, and `partial` would
    # have been the cheerful reading of one.
    ("every source failed", [_batch_item(1, _B.OUTCOME_UNREADABLE),
                             _batch_item(2, _B.OUTCOME_EXPORT_FAILED),
                             _batch_item(3, _B.OUTCOME_DETECTION_FAILED)],
     3, False, "", "", dn.OUTCOME_FAILED),
    ("some worked, some failed", [_batch_item(1, _B.OUTCOME_COMPLETED, written=9, planned=9),
                                  _batch_item(2, _B.OUTCOME_UNREADABLE)],
     2, False, "", "", dn.OUTCOME_PARTIAL),
    # A source that exported 7 of 10 clips is counted in `failed` and in NEITHER `completed` nor
    # `no_clips`. Folding on `completed` alone would have called this a failure with the operator's
    # clips sitting in the output folder -- the same reading `_notify_cut` refuses for a crash.
    ("one partial source, clips on disk",
     [_batch_item(1, _B.OUTCOME_PARTIAL, written=7, planned=10)],
     1, False, "", "", dn.OUTCOME_PARTIAL),
    # Read and planned correctly, genuinely nothing worth exporting. A real answer about the
    # footage, which the panel calls `Batch finished`.
    ("nothing worth exporting anywhere", [_batch_item(1, _B.OUTCOME_NO_CLIPS),
                                          _batch_item(2, _B.OUTCOME_NO_CLIPS)],
     2, False, "", "", dn.OUTCOME_SUCCESS),
    # ...and the pair that proves `no_clips` has to be in the fold: without it this run, which
    # mostly worked, would be announced as a FAILURE.
    ("nothing worth exporting plus one failure", [_batch_item(1, _B.OUTCOME_NO_CLIPS),
                                                  _batch_item(2, _B.OUTCOME_NO_CLIPS),
                                                  _batch_item(3, _B.OUTCOME_UNREADABLE)],
     3, False, "", "", dn.OUTCOME_PARTIAL),
    ("fatal error outranks everything", [_batch_item(1, _B.OUTCOME_COMPLETED, written=4, planned=4),
                                         _batch_item(2, _B.OUTCOME_UNEXPECTED_ERROR)],
     3, False, "boom", "", dn.OUTCOME_FAILED),
    ("cancelled with work done and work not attempted",
     [_batch_item(1, _B.OUTCOME_COMPLETED, written=5, planned=5),
      _batch_item(2, _B.OUTCOME_CANCELLED, written=2, planned=6),
      _batch_item(3, _B.OUTCOME_NOT_ATTEMPTED)],
     3, True, "", "", dn.OUTCOME_CANCELLED),
    # The fifth rendering agreeing with the other four: the headline says the record was lost and
    # the activity panel says PARTIAL/FAILED, so `finished` was the one reading out of step.
    ("clean run, authoritative record lost",
     [_batch_item(1, _B.OUTCOME_COMPLETED, written=9, planned=9),
      _batch_item(2, _B.OUTCOME_COMPLETED, written=3, planned=3)],
     2, False, "", "could not write", dn.OUTCOME_PARTIAL),
    # ...and a lost record never makes an already-worse outcome look better.
    ("every source failed AND the record was lost",
     [_batch_item(1, _B.OUTCOME_UNREADABLE), _batch_item(2, _B.OUTCOME_EXPORT_FAILED)],
     2, False, "", "could not write", dn.OUTCOME_FAILED),
]


@pytest.mark.parametrize(
    "label,items,discovered,cancelled,fatal,manifest_error,expected",
    _F1_BATCHES, ids=[case[0] for case in _F1_BATCHES])
def test_the_folder_batch_outcome_is_measured_from_the_real_classifier(
        tmp_path, label, items, discovered, cancelled, fatal, manifest_error, expected):
    ns = _notify_namespace(tmp_path)
    totals = fork_cutter_batch.summarise(items, discovered)
    terminal = fork_cutter_batch.terminal_status(items, discovered, cancelled, fatal_error=fatal)

    ns["_notify_cut_batch"](
        _Request("S1"), terminal,
        completed=totals.completed, failed=totals.failed, cancelled=totals.cancelled,
        no_clips=totals.no_clips, partial=totals.partial,
        record_lost=bool(manifest_error),
        not_attempted=max(0, discovered - totals.attempted))

    records = _drained(ns, "S1")
    assert len(records) == 1, label
    assert records[0]["job"] == dn.JOB_RAW_CUTTER_BATCH
    assert records[0]["outcome"] == expected, (label, terminal, totals, records[0]["body"])


def test_a_lost_record_qualifies_a_success_and_nothing_else(tmp_path):
    """It mirrors the panel's own override, which fires only when the tone was otherwise `done`."""
    ns = _notify_namespace(tmp_path)
    for terminal, counts in (
            (fork_cutter_batch.TERMINAL_CANCELLED, dict(completed=1, failed=0)),
            (fork_cutter_batch.TERMINAL_FAILED, dict(completed=1, failed=1)),
            (fork_cutter_batch.TERMINAL_COMPLETED_WITH_FAILURES,
             dict(completed=0, failed=2, no_clips=0, partial=0))):
        ns["_notify_cut_batch"](_Request("S1"), terminal, record_lost=True, **counts)
    outcomes = [record["outcome"] for record in _drained(ns, "S1")]
    assert outcomes == [dn.OUTCOME_CANCELLED, dn.OUTCOME_FAILED, dn.OUTCOME_FAILED]


def test_the_two_batch_tables_cover_the_whole_terminal_vocabulary_exactly_once(tmp_path):
    """Structural: a new `TERMINAL_*` class must not be able to fall silently into neither rule.

    The `else: return` branch is the right home for an UNRECOGNISED status -- a run that never
    started -- and the wrong home for a real one, so the two sets are asserted to partition the
    batch module's own vocabulary rather than merely to be subsets of it.
    """
    ns = _notify_namespace(tmp_path)
    translated = set(ns["_CUT_BATCH_RUN_OUTCOMES"])
    folded = set(ns["_CUT_BATCH_RAN_TO_END"])
    # [FORK] R4-D: a THIRD rule, and it is deliberately not a table. `TERMINAL_STORAGE_FULL` is
    # neither a flat translation (only the counts can tell a stop after twelve good sources from
    # one before the first) nor a run that reached the end of its list (that fold can answer
    # SUCCESS, which a run stopped part-way must never be announced as), so it is an explicit
    # branch in `_notify_cut_batch`. It is named here rather than inferred, and the partition
    # assertion below still covers it, so a *fourth* class with no rule at all still fails.
    storage = {fork_cutter_batch.TERMINAL_STORAGE_FULL}
    assert f"fork_cutter_batch.{'TERMINAL_STORAGE_FULL'}" in ast.unparse(
        _func(_tree(_GUI), "_notify_cut_batch")), "the storage branch is gone"
    vocabulary = {getattr(fork_cutter_batch, name) for name in dir(fork_cutter_batch)
                  if name.startswith("TERMINAL_")
                  # `TERMINAL_IN_PROGRESS` is the checkpoint-only marker and is never a terminal
                  # answer -- `terminal_status` cannot return it, so `sink['terminal']` cannot
                  # hold it and no notification rule may claim it. Asserted, not assumed.
                  and name != "TERMINAL_IN_PROGRESS"}

    assert vocabulary, "the batch module declares no TERMINAL_* constants"
    assert fork_cutter_batch.TERMINAL_IN_PROGRESS not in {
        fork_cutter_batch.terminal_status([], 0, False),
        fork_cutter_batch.terminal_status([], 0, True),
        fork_cutter_batch.terminal_status([], 0, False, "boom"),
        fork_cutter_batch.terminal_status([], 0, False, storage_full=True)}
    assert translated | folded | storage == vocabulary, \
        vocabulary - (translated | folded | storage)
    assert not (translated & folded) and not (storage & (translated | folded))
    assert set(ns["_CUT_BATCH_RUN_OUTCOMES"].values()) <= set(dn.OUTCOMES)


@pytest.mark.parametrize("kwargs,expected,detail", [
    ({"written": 12, "failed": 0}, dn.OUTCOME_SUCCESS, "12 completed, 0 failed"),
    ({"written": 12, "failed": 2}, dn.OUTCOME_PARTIAL, "12 completed, 2 failed"),
    ({"written": 0, "failed": 2}, dn.OUTCOME_FAILED, "0 completed, 2 failed"),
])
def test_the_audio_cutter_reports_published_clips(tmp_path, kwargs, expected, detail):
    ns = _notify_namespace(tmp_path)
    ns["_notify_audio_cut"](_Request("S1"), **kwargs)
    records = _drained(ns, "S1")
    assert records[0]["job"] == dn.JOB_AUDIO_CUTTER
    assert records[0]["outcome"] == expected
    assert records[0]["body"].endswith(detail)


def test_an_audio_cutter_failure_before_any_clip_reports_no_fabricated_counts(tmp_path):
    ns = _notify_namespace(tmp_path)
    ns["_notify_audio_cut"](_Request("S1"), failed_outright=True)
    record = _drained(ns, "S1")[0]
    assert record["outcome"] == dn.OUTCOME_FAILED
    assert record["body"] == dn.OUTCOME_BODIES[dn.OUTCOME_FAILED]


@pytest.mark.parametrize("operation,job", [
    (fork_prep_activity.OPERATION_SCAN, dn.JOB_LIBRARY_SCAN),
    (fork_prep_activity.OPERATION_ANALYZE, dn.JOB_LIBRARY_ANALYSIS),
])
def test_the_two_preparation_passes_are_named_apart(tmp_path, operation, job):
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), operation)
    assert _drained(ns, "S1")[0]["job"] == job


def test_a_failed_preparation_pass_is_reported_as_a_failure(tmp_path):
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE, failed=True)
    assert _drained(ns, "S1")[0]["outcome"] == dn.OUTCOME_FAILED


def test_a_refused_preparation_pass_announces_nothing(tmp_path):
    """A declined pass never started, so it has no outcome; reporting it as a failure would send
    an operator looking for a fault in their library."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_SCAN, refused=True)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE,
                          failed=True, refused=True)
    assert _drained(ns, "S1") == []


@pytest.mark.parametrize("operation", [fork_prep_activity.OPERATION_VERIFY,
                                       fork_prep_activity.OPERATION_SAMPLE, "", None])
def test_verification_and_sample_decode_are_not_notification_sources(tmp_path, operation):
    """Both are bounded read-only diagnostics reporting in seconds, and neither is in the
    accepted R17-A event list."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), operation)
    assert _drained(ns, "S1") == []


# =============================================================================================
# 3a-R2. The library analysis pass's outcome  (R17-A R2, finding F3)
#
# `is_failure_report` recognises only the marker `record_failure` writes, so a pass that returned
# normally while NOTHING it attempted completed produced no marker and was announced as
# `Library analysis finished`. The verdict comes from the analyzer's own `*_this_run` counts.
# =============================================================================================

def _analysis_result(**overrides):
    """A result payload shaped like `analyze_video_sources`', complete and consistent by default."""
    payload = dict(sources_analyzed_this_run=4, qwen_jobs_this_run=4,
                   qwen_completed_jobs_this_run=4, qwen_incomplete_jobs_this_run=0,
                   qwen_requested_count_this_run=40, qwen_tag_count_this_run=40,
                   cache_hits=0, source_count=4, analysis_seconds=12.0)
    payload.update(overrides)
    return payload


_F3_RESULTS = [
    ("every submitted source analysed, every Qwen job complete",
     _analysis_result(), 4, fork_prep.ANALYSIS_COMPLETE),
    # A fully warm batch legitimately runs no job at all, and that is a success, not a shortfall.
    ("fully warm: nothing needed doing",
     _analysis_result(sources_analyzed_this_run=0, qwen_jobs_this_run=0,
                      qwen_completed_jobs_this_run=0, qwen_requested_count_this_run=0,
                      qwen_tag_count_this_run=0, cache_hits=4), 4, fork_prep.ANALYSIS_COMPLETE),
    # The defect: a dead shared worker. Real GPU minutes, nothing cached, no failure marker.
    ("the shared Qwen worker died: 0 of 4 jobs completed",
     _analysis_result(qwen_completed_jobs_this_run=0, qwen_incomplete_jobs_this_run=4,
                      qwen_tag_count_this_run=0, analysis_seconds=301.0),
     4, fork_prep.ANALYSIS_INCOMPLETE),
    ("3 of 4 Qwen jobs completed",
     _analysis_result(qwen_completed_jobs_this_run=3, qwen_incomplete_jobs_this_run=1,
                      qwen_tag_count_this_run=30), 4, fork_prep.ANALYSIS_INCOMPLETE),
    # `source_count` is `len(existing)`, so a source that vanished between Scan and Analyze is a
    # measured shortfall rather than something to be silently dropped.
    ("two submitted sources no longer existed",
     _analysis_result(sources_analyzed_this_run=2, qwen_jobs_this_run=2,
                      qwen_completed_jobs_this_run=2, qwen_requested_count_this_run=20,
                      qwen_tag_count_this_run=20, source_count=2),
     4, fork_prep.ANALYSIS_INCOMPLETE),
    ("the result is not a mapping at all", "not-a-dict", 4, fork_prep.ANALYSIS_UNKNOWN),
    ("a required count is missing",
     {key: value for key, value in _analysis_result().items()
      if key != "qwen_incomplete_jobs_this_run"}, 4, fork_prep.ANALYSIS_UNKNOWN),
    # `bool` subclasses `int`, the trap the Stage-5 count readers already record.
    ("a count arrived as a bool", _analysis_result(qwen_incomplete_jobs_this_run=False),
     4, fork_prep.ANALYSIS_UNKNOWN),
    ("a count arrived as a float", _analysis_result(source_count=4.0),
     4, fork_prep.ANALYSIS_UNKNOWN),
    ("a count arrived as a numeric string", _analysis_result(qwen_jobs_this_run="4"),
     4, fork_prep.ANALYSIS_UNKNOWN),
    # Every submitted job starts incomplete and is promoted only by its own returned evidence, so
    # a tally that does not add up is a payload nothing should be concluded from.
    ("the job tally does not add up",
     _analysis_result(qwen_jobs_this_run=4, qwen_completed_jobs_this_run=2,
                      qwen_incomplete_jobs_this_run=1), 4, fork_prep.ANALYSIS_UNKNOWN),
    ("the submitted count is not a count", _analysis_result(), None,
     fork_prep.ANALYSIS_UNKNOWN),
]


@pytest.mark.parametrize("label,result,submitted,expected", _F3_RESULTS,
                         ids=[case[0] for case in _F3_RESULTS])
def test_the_analysis_verdict_is_read_from_the_structured_counts(
        label, result, submitted, expected):
    assert fork_prep.analysis_run_outcome(result, submitted) == expected, label


def test_the_analysis_verdict_never_raises_on_anything_a_subprocess_could_produce():
    """It runs on a finished pass's terminal path, so it is total by contract."""
    hostile = [None, "", 0, [], (), object(), {"source_count": object()},
               {"qwen_jobs_this_run": float("nan")},
               _analysis_result(source_count=-1), _analysis_result(qwen_jobs_this_run=-2)]
    for result in hostile:
        assert fork_prep.analysis_run_outcome(result, 4) in fork_prep.ANALYSIS_OUTCOMES
        assert fork_prep.analysis_run_outcome(result, None) in fork_prep.ANALYSIS_OUTCOMES


@pytest.mark.parametrize("verdict,expected", [
    (fork_prep.ANALYSIS_COMPLETE, dn.OUTCOME_SUCCESS),
    (fork_prep.ANALYSIS_INCOMPLETE, dn.OUTCOME_PARTIAL),
    (fork_prep.ANALYSIS_UNKNOWN, dn.OUTCOME_PARTIAL),
    # An absent verdict, and one from a future vocabulary this build does not know.
    (None, dn.OUTCOME_PARTIAL),
    ("something_new", dn.OUTCOME_PARTIAL),
])
def test_an_analysis_pass_is_announced_from_its_verdict_and_never_cheerfully(
        tmp_path, verdict, expected):
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE, analysis=verdict)
    record = _drained(ns, "S1")[0]
    assert record["job"] == dn.JOB_LIBRARY_ANALYSIS
    assert record["outcome"] == expected


def test_a_scan_needs_no_analysis_verdict_and_is_still_a_plain_success(tmp_path):
    """There is no Stage-5 batch for a Scan's verdict to be about; its faults arrive through
    `failed`, exactly as before. So the two operations are asked different questions."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_SCAN)
    assert _drained(ns, "S1")[0]["outcome"] == dn.OUTCOME_SUCCESS


def test_a_failed_analysis_pass_stays_a_failure_whatever_verdict_arrives(tmp_path):
    """`record_failure`'s marker is the stronger statement: the pass did not finish at all."""
    ns = _notify_namespace(tmp_path)
    for verdict in (None, fork_prep.ANALYSIS_COMPLETE, fork_prep.ANALYSIS_UNKNOWN):
        ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE,
                              failed=True, analysis=verdict)
    assert {record["outcome"] for record in _drained(ns, "S1")} == {dn.OUTCOME_FAILED}


def test_the_analysis_verdict_reaches_no_cache_identity_or_persisted_record():
    """The P2/D2 boundary: a notification verdict is ephemeral invocation metadata and must not
    appear anywhere on the Stage-5 side."""
    for relative in ("src/video_analysis.py",
                     "src/auto_mode/stage5_qwen_scene_worker.py"):
        with open(os.path.join(_REPO_ROOT, relative), "r", encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("analysis_run_outcome", "ANALYSIS_COMPLETE", "ANALYSIS_INCOMPLETE",
                          "ANALYSIS_UNKNOWN", "desktop_notify", "outcome_sink"):
            assert forbidden not in source, (relative, forbidden)


def test_the_verdict_classifier_names_no_cache_or_completion_rule():
    """It reads the ephemeral `*_this_run` channel and nothing durable: no record is opened, no
    completion rule is re-answered, and no cache constant is mentioned."""
    tree = _tree(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_prep.py"))
    body = ast.unparse(_func(tree, "analysis_run_outcome"))
    for forbidden in ("_load_cache", "_cache_path", "_cache_entry_is_complete",
                      "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "open(", "json"):
        assert forbidden not in body, forbidden


def test_no_translator_can_produce_an_unsafe_envelope(tmp_path):
    """The content-safety rule, applied to what the SHIPPED translators actually emit."""
    ns = _notify_namespace(tmp_path)
    ns["_notify_render"](_Request("S1"), RenderOutcomeKind.SUCCESS, True)
    ns["_notify_render_batch"](_Request("S1"), _BatchOutcome(4, 2, 2))
    ns["_notify_cut"](_Request("S1"), written=10 ** 9, failed=10 ** 9)
    ns["_notify_cut_batch"](_Request("S1"), fork_cutter_batch.TERMINAL_CANCELLED,
                            completed=1, failed=2, cancelled=3, not_attempted=4)
    ns["_notify_audio_cut"](_Request("S1"), failed_outright=True)
    ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE)
    records = _drained(ns, "S1")
    assert len(records) == ns["NOTIFY_QUEUE_CAP"] or len(records) == 6
    for record in records:
        for field in ("title", "body"):
            assert dn.is_safe_text(record[field]), record


# =============================================================================================
# 3b. The server seam, STRUCTURALLY
# =============================================================================================

def _calls(tree, attr):
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr]


def _kwargs(call):
    return {keyword.arg: ast.unparse(keyword.value) for keyword in call.keywords}


#: Every click whose terminal outcome the accepted scope names as an event source. The `.then()`
#: drain is chained onto each one, which is what keeps every pinned `inputs`/`outputs` list and
#: emitted arity byte-identical.
_PRODUCING_CLICKS = ("process_btn.click", "render_selected_variants_btn.click",
                     "cut_run_btn.click", "cut_batch_start_btn.click", "audio_cut_btn.click",
                     "prep_scan_btn.click", "prep_analyze_btn.click")


def test_every_authorised_event_source_chains_exactly_one_drain():
    tree = _tree(_GUI)
    drains = [call for call in _calls(tree, "then")
              if _kwargs(call).get("fn") == "_notify_drain"]
    assert len(drains) == len(_PRODUCING_CLICKS), \
        f"{len(drains)} drains for {len(_PRODUCING_CLICKS)} authorised event sources"
    chained = set()
    for call in drains:
        kwargs = _kwargs(call)
        # `inputs=[]` is exact: nothing on screen decides what is delivered.
        assert kwargs["inputs"] == "[]", kwargs
        # `outputs` is exactly the one hidden envelope -- no report panel, no state, no widget.
        assert kwargs["outputs"] == "[notify_envelope]", kwargs
        assert kwargs.get("show_progress") == "'hidden'", kwargs
        # No concurrency group, for the measured S2-A R3 reason: a full group leaves the event
        # QUEUED, and a delivery arriving after the next job would be worse than none.
        assert "concurrency_id" not in kwargs, kwargs
        assert kwargs.get("concurrency_limit") == "None", kwargs
        chained.add(ast.unparse(call.func.value.func))
    assert chained == set(_PRODUCING_CLICKS), chained


def test_the_envelope_component_has_exactly_one_writer():
    """`_notify_drain` and nothing else. If a job handler wrote it directly, the notification
    channel would be an element of a pinned `outputs` list and an input `change` handler could
    deliver one."""
    tree = _tree(_GUI)
    writers = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        kwargs = {keyword.arg: keyword.value for keyword in node.keywords}
        outputs = kwargs.get("outputs")
        if outputs is None or not isinstance(outputs, (ast.List, ast.Tuple)):
            continue
        if "notify_envelope" in [ast.unparse(element) for element in outputs.elts]:
            writers.add(ast.unparse(kwargs.get("fn")) if "fn" in kwargs else "<no fn>")
    assert writers == {"_notify_drain"}, writers


def test_the_envelope_starts_empty_and_is_never_visible():
    """"No notification on page load" is structural: the dispatcher ignores an empty value, and
    nothing writes this component until a job that actually ran reaches a terminal state."""
    tree = _tree(_GUI)
    assign = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", "") == "notify_envelope")
    kwargs = _kwargs(assign.value)
    assert kwargs["value"] == "fork_notify.ENVELOPE_EMPTY"
    assert kwargs["visible"] == "False"
    assert kwargs["interactive"] == "False"


def test_the_notification_panel_writes_only_its_own_app_state_line():
    """The toggle is a server call; the browser-status line is written by the page alone. Keeping
    them apart is what lets the server rewrite its half without erasing the live answer.

    [FORK] Digital-Union (Settings Preference Consistency R2, L2): the listener is `.input`, not
    `.change` -- see `test_both_option_controls_are_user_only` in
    `tests/test_settings_pref_consistency.py` for why, and for the assertion that neither may
    silently go back to `.change`."""
    tree = _tree(_GUI)
    change = next(call for call in _calls(tree, "input")
                  if ast.unparse(call.func) == "notify_enabled.input")
    kwargs = _kwargs(change)
    assert kwargs["fn"] == "_on_notifications_enabled_change"
    assert kwargs["inputs"] == "[notify_enabled]"
    assert kwargs["outputs"] == "[notify_app_state]"

    # `notify_status` is written by no server handler at all.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "outputs" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                    assert "notify_status" not in [ast.unparse(e) for e in keyword.value.elts], \
                        "the browser-status line must have no server-side writer"


def test_the_notification_panel_writes_no_render_source_or_preparation_widget():
    """Changing a notification setting cannot clear a confirmed source set, disable Create Video,
    trigger a scan or move a render."""
    tree = _tree(_GUI)
    forbidden = {"status_output", "video_output", "process_btn", "source_state", "prep_state",
                 "session_state", "render_invocation_state", "pref_status", "prep_status",
                 "variant_batch_state", "director_proposal_state", "cut_handoff_state"}
    # [FORK] Digital-Union (R2, L2): `notify_enabled.input` -- the toggle is user-only now.
    # `notify_envelope.change` is unchanged: it is a hidden relay the server writes deliberately,
    # and a programmatic write is exactly how a notification is delivered.
    for name in ("notify_enabled.input", "notify_permission_btn.click",
                 "notify_test_btn.click", "notify_refresh_btn.click",
                 "notify_envelope.change"):
        call = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and ast.unparse(node.func) == name)
        outputs = {keyword.value for keyword in call.keywords if keyword.arg == "outputs"}
        for node in outputs:
            written = {ast.unparse(e) for e in node.elts} if isinstance(
                node, (ast.List, ast.Tuple)) else set()
            assert not (written & forbidden), (name, written & forbidden)


def test_every_option_writer_passes_the_complete_option_set():
    """The regression a second option key introduced. `save_preferences` replaces the file and
    defaults any option the caller omits, so a single-key write silently resets the other."""
    tree = _tree(_GUI)
    saves = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and ast.unparse(node.func) == "fork_prefs.save_preferences"]
    assert saves, "no save_preferences call found in gui.py"
    for call in saves:
        if len(call.args) < 3:
            continue                        # `options=None` means "leave them exactly as they are"
        rendered = ast.unparse(call.args[2])
        assert rendered in ("dict(_FOLDER_OPTIONS)", "options",
                            "fork_prefs.empty_options()"), rendered

    # ...and the one place a partial dict is built spreads the current options first.
    body = ast.unparse(_func(tree, "_on_save_folder_prefs"))
    assert "**_FOLDER_OPTIONS" in body, \
        "saving folders must preserve every option it does not own"


def test_the_seven_producers_take_an_injected_session_and_no_new_input():
    """`gr.Request` is injected at the annotated parameter's own index, so it is absent from every
    `inputs` list -- the browser gains no new way to name a session."""
    from conftest import INJECTED_ANNOTATIONS

    tree = _tree(_GUI)
    producers = ("process_video_guarded", "render_selected_variants_guarded", "_on_cut_run",
                 "_on_cut_batch_run_ui", "_on_audio_cut", "_on_prep_scan_click",
                 "_on_prep_analyze_click")
    for name in producers:
        arguments = _func(tree, name).args.args
        assert arguments[-1].arg == "request", name
        assert ast.unparse(arguments[-1].annotation) in INJECTED_ANNOTATIONS, name
        assert [argument.arg for argument in arguments].count("request") == 1, name
    for click in _PRODUCING_CLICKS:
        call = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and ast.unparse(node.func) == click)
        inputs = next(keyword.value for keyword in call.keywords if keyword.arg == "inputs")
        assert "request" not in [ast.unparse(e) for e in inputs.elts], click


#: How many notification calls each producing handler's body is allowed to make, and which
#: translator it may call. Exact counts, because "at most one notification per authorized job
#: terminal event" is a property of where the calls are, and a seventh call appearing in a handler
#: would be a second notification for one job.
_PRODUCER_CALLS = {
    # The one terminal path, beside the one place the lifecycle is marked. The refusal above it
    # performed no work and is deliberately not a notification site.
    "process_video_guarded": ("_notify_render", 1),
    # One for the whole 2-4-candidate batch, after its outcome object is built.
    "render_selected_variants_guarded": ("_notify_render_batch", 1),
    # Four: finished, cancelled, crashed, and -- since R4-D -- the output drive filling up. Every
    # `refuse(...)` path performed no work and is deliberately not a notification site, which
    # includes R4-D's own capacity preflight: a refused job created nothing and encoded nothing.
    "_on_cut_run": ("_notify_cut", 4),
    "_on_cut_batch_run_ui": ("_notify_cut_batch", 1),
    # Four: the export terminal, plus the three failures that follow the analysis.
    "_on_audio_cut": ("_notify_audio_cut", 4),
    # One, shared by Scan and Analyze, which `operation` tells apart.
    "_run_prep_in_worker": ("_notify_library", 1),
}


@pytest.mark.parametrize("handler,expected", sorted(_PRODUCER_CALLS.items()))
def test_each_producer_notifies_exactly_where_it_is_authorised_to(handler, expected):
    name, count = expected
    body = ast.unparse(_func(_tree(_GUI), handler))
    assert body.count(f"{name}(") == count, f"{handler} calls {name} {body.count(name)} times"
    # ...and no handler reaches the publisher or the pure module directly, so every notification
    # goes through the one translator that owns its vocabulary.
    assert "_notify_publish(" not in body, handler
    assert "fork_notify." not in body, handler


def test_the_publisher_has_exactly_the_six_translators_as_callers():
    """Everything else must go through one of them, so no call site can invent a vocabulary."""
    tree = _tree(_GUI)
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_notify_publish"):
                callers.add(node.name)
    assert callers == {"_notify_render", "_notify_render_batch", "_notify_cut",
                       "_notify_cut_batch", "_notify_audio_cut", "_notify_library"}, callers


def test_no_translator_can_raise_into_a_finished_jobs_terminal_path(tmp_path):
    """`_notify_publish`'s broad guard protects the PUBLISHER. The six translators run *outside*
    it, on a finished job's terminal path, so arithmetic or attribute access at that layer is what
    could still cost a render its final frame -- which is why R17-A R2 put the folder batch's new
    fold in `desktop_notify.total_count` rather than in `gui.py`.

    Every translator is called here with values no shipped call site produces, because the point is
    the property and not the reachability: a notification is a courtesy, and a job's result is not.
    """
    ns = _notify_namespace(tmp_path)
    # Everything a count-bearing field could arrive as, including an unhashable value: these four
    # translators do arithmetic, which is the layer R17-A R2 moved into `total_count`.
    hostile = ("12", None, object(), float("nan"), -3, True, [], {})
    for value in hostile:
        ns["_notify_cut"](_Request("S1"), written=value, failed=value)
        ns["_notify_cut"](_Request("S1"), written=value, crashed=True)
        ns["_notify_cut"](_Request("S1"), written=value, cancelled=True)
        ns["_notify_cut_batch"](_Request("S1"), fork_cutter_batch.TERMINAL_COMPLETED,
                                completed=value, failed=value, no_clips=value, partial=value,
                                cancelled=value, not_attempted=value, record_lost=value)
        ns["_notify_cut_batch"](_Request("S1"),
                                fork_cutter_batch.TERMINAL_COMPLETED_WITH_FAILURES,
                                completed=value, failed=value, no_clips=value, partial=value)
        ns["_notify_audio_cut"](_Request("S1"), written=value, failed=value)
    # The two translators whose argument is a TYPED CLASS rather than a count are given the value
    # space their producer can actually hand over: a `RenderOutcomeKind`, `None`, or -- if the
    # producer were broken -- some other hashable token. An unhashable one is deliberately out of
    # scope: both read it through a dict lookup, `session_state[RENDER_OUTCOME_KEY]` and
    # `RenderBatchOutcome.outcome_kind` are the only sources, and guarding a lookup would add a
    # name to `process_video_guarded`'s body, which three other suites freeze.
    for value in ("12", None, object(), float("nan"), -3, True):
        ns["_notify_render"](_Request("S1"), value, True)
        ns["_notify_render_batch"](_Request("S1"), value)
    for value in hostile:
        ns["_notify_library"](_Request("S1"), fork_prep_activity.OPERATION_ANALYZE,
                              analysis=value if isinstance(value, (str, type(None))) else None)
        ns["_notify_library"](_Request("S1"), value if isinstance(value, str) else "",
                              analysis=fork_prep.ANALYSIS_COMPLETE)
    # ...and a request object whose attribute access itself raises.
    ns["_notify_cut_batch"](_HostileRequest(), fork_cutter_batch.TERMINAL_COMPLETED,
                            completed=1, failed=0, no_clips=0, partial=0)
    ns["_notify_library"](_HostileRequest(), fork_prep_activity.OPERATION_ANALYZE,
                          analysis=fork_prep.ANALYSIS_COMPLETE)
    # Whatever survived is still safe text; nothing above was allowed to escape.
    for record in _drained(ns, "S1"):
        for field in ("title", "body"):
            assert dn.is_safe_text(record[field]), record


def test_the_analysis_verdict_is_published_structurally_and_read_after_the_join():
    """The sink is written by the worker thread and read by the generator, so the `thread.join()`
    is the happens-before. Reading it earlier would be a data race on a dict."""
    tree = _tree(_GUI)

    analyse = ast.unparse(_func(tree, "_prep_analyze_impl"))
    assert "outcome_sink['analysis'] = fork_prep.analysis_run_outcome(result, len(subset))" \
        in analyse, analyse
    assert analyse.count("analysis_run_outcome") == 1
    # It is published from the SUCCESS path only. A refusal and a fault return earlier, and those
    # are already carried by `refused` and `failed`.
    assert analyse.index("analysis_run_outcome") > analyse.index("analyze_video_sources(")

    worker = _func(tree, "_run_prep_in_worker")
    rendered = ast.unparse(worker)
    assert "outcome_sink" in rendered
    assert rendered.index("thread.join()") < rendered.index("outcome_sink or {}"), \
        "the verdict must be read only after the worker has been joined"
    # The sink is keyword-only with a plain `None` default on both, so every existing caller --
    # including the suites that execute these bodies against a synthesised namespace -- is
    # unchanged, and an absent sink reads as an absent verdict rather than as a success.
    for name in ("_prep_analyze_impl", "_run_prep_in_worker"):
        signature = _func(tree, name).args
        sink = next((argument for argument in signature.args + signature.kwonlyargs
                     if argument.arg == "outcome_sink"), None)
        assert sink is not None, name

    # And the dict belongs to the CLICK, not to the module: Scan and Analyze take no mutex, so two
    # browser sessions genuinely overlap and a shared slot would let one describe the other's run.
    click = ast.unparse(_func(tree, "_on_prep_analyze_click"))
    assert "analysis_outcome: dict = {}" in click
    assert click.count("outcome_sink=analysis_outcome") == 2, click
    assert not any(isinstance(node, ast.Assign)
                   and getattr(node.targets[0], "id", "").endswith("_OUTCOME_SINK")
                   for node in ast.walk(tree)), "no module-level verdict slot may be introduced"


def test_no_notification_is_published_from_a_finally_block():
    """An abandoned Gradio stream unwinds through `finally`, and abandonment is not a terminal
    outcome -- `.then()` does not run for an abandoned event either, so a record published there
    would simply sit in the ledger until some later click delivered it out of context."""
    tree = _tree(_GUI)
    for handler in _PRODUCER_CALLS:
        for node in ast.walk(_func(tree, handler)):
            if not isinstance(node, ast.Try):
                continue
            rendered = "\n".join(ast.unparse(statement) for statement in node.finalbody)
            assert "_notify_" not in rendered, (handler, rendered)


def test_the_ledger_holds_nothing_live_and_is_not_a_persistence_layer():
    """Plain data only: no thread, handle, lifecycle, component, path or `Popen` may enter it."""
    tree = _tree(_GUI)
    rendered = ast.unparse(_func(tree, "_notify_publish"))
    for forbidden in ("Thread", "Popen", "Lifecycle", "open(", "os.path", "json.dump",
                      "gr.", "session_state"):
        assert forbidden not in rendered, forbidden
    drain = ast.unparse(_func(tree, "_notify_drain"))
    for forbidden in ("Thread", "Popen", "Lifecycle", "open(", "os.path", "json.dump"):
        assert forbidden not in drain, forbidden


# =============================================================================================
# 4. The client half, structurally
# =============================================================================================

_SCRIPT_NAMES = ("NOTIFY_JS", "NOTIFY_DISPATCH_JS", "NOTIFY_REQUEST_JS", "NOTIFY_TEST_JS",
                 "NOTIFY_REFRESH_JS")


def _script(name: str) -> str:
    """The shipped script, read out of the running module's own globals.

    Built by evaluating only the assignment, so the substituted wording is the real wording: the
    sentences come from `desktop_notify`, and reading the raw source text would miss that.
    """
    source = _source(_GUI)
    start = source.index(f"{name} = r\"\"\"")
    end = source.index('"""', start + len(name) + 7)
    rendered = source[start:end + 3]
    # Carry the `.replace(...)` tail that substitutes the wording in, if there is one.
    tail_end = source.index("\n\n", end)
    tail = source[end + 3:tail_end]
    namespace = {"json": json, "fork_notify": dn, **_element_ids()}
    exec(rendered + tail, namespace)        # noqa: S102 - the shipped assignment under test
    return namespace[name]


#: [FORK] R17-A R3: the two element ids the installer is given. Taken from `gui.py`'s own
#: assignments rather than written out here: the whole point of substituting them in is that the
#: script and the panel address the same element, and a literal copy in this file is exactly the
#: second definition that would let them drift apart silently.
_ELEMENT_ID_ASSIGNS = ("NOTIFY_STATUS_ELEMENT_ID", "NOTIFY_FEEDBACK_ELEMENT_ID")


def _element_ids() -> dict:
    tree = _tree(_GUI)
    nodes = [node for node in tree.body
             if isinstance(node, ast.Assign)
             and getattr(node.targets[0], "id", "") in _ELEMENT_ID_ASSIGNS]
    namespace: dict = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]),      # noqa: S102 - two string literals
                 _GUI, "exec"), namespace)
    missing = set(_ELEMENT_ID_ASSIGNS) - set(namespace)
    assert not missing, f"missing from gui.py: {missing}"
    return {name: namespace[name] for name in _ELEMENT_ID_ASSIGNS}


def _entry_point_body(name: str) -> str:
    """One property of the installer's `window.__bsNotify` object, comment tails stripped.

    **Brace-balanced rather than line-based**, and that is the point: R3 made `test` and `request`
    multi-statement, and the `[:index("\\n")]` slice this replaced would have silently shrunk to
    the key's first line and gone on passing while asserting about almost nothing.
    """
    installer = _executable(_script("NOTIFY_JS"))
    start = installer.index(f"\n    {name}: ")
    depth, opened = 0, False
    for offset in range(start, len(installer)):
        character = installer[offset]
        if character == "{":
            depth, opened = depth + 1, True
        elif character == "}":
            depth -= 1
            if depth == 0:
                return installer[start:offset + 1]
        elif character == "," and depth == 0 and not opened:
            return installer[start:offset]
    raise AssertionError(f"{name} is not a balanced entry point of window.__bsNotify")


def _executable(script: str) -> str:
    """The script with its `//` comment tails removed.

    Load-bearing, not tidiness: these scripts document what they deliberately do NOT do, so they
    legitimately name `sessionStorage`, `setInterval` and the Push API in prose -- and a substring
    ban run over the whole text would trip on the very explanation of the ban. This repository has
    been bitten by that repeatedly; the fix is always to ban the usage structurally and never to
    stop explaining it. No string literal in any of these scripts contains `//`.
    """
    assert "//" not in json.dumps(dn.TEST_BODY), "a literal gained a `//`; revisit this stripper"
    lines = []
    for line in script.splitlines():
        head, _, _tail = line.partition("//")
        lines.append(head)
    return "\n".join(lines)


@pytest.mark.parametrize("name", _SCRIPT_NAMES)
def test_no_notification_script_makes_a_network_call_or_installs_a_background_worker(name):
    """The accepted scope forbids a service worker, Push infrastructure, a daemon, background
    polling, a browser extension and an external notification service. The way to keep that is to
    have none of them anywhere in here."""
    script = _executable(_script(name))
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "axios",
                      "/queue/", "navigator.sendBeacon", "serviceWorker", "ServiceWorker",
                      "PushManager", "pushManager", "importScripts", "new Worker",
                      "setInterval", "setTimeout", "requestIdleCallback", "chrome.runtime"):
        assert forbidden not in script, f"{name} contains {forbidden}"


def test_exactly_one_script_can_ask_chrome_for_permission():
    """"A user gesture must initiate `Notification.requestPermission()`; never on page load,
    during a render, or because the saved toggle changed."

    One IMPLEMENTATION, in the installer, and one route to it -- the button's delegate. The
    installer holding the only call site is what makes "a page load cannot prompt" checkable:
    `request()` is a method on `window.__bsNotify` and the installer never invokes it.
    """
    scripts = {name: _executable(_script(name)) for name in _SCRIPT_NAMES}
    callers = [name for name, script in scripts.items() if "requestPermission()" in script]
    assert callers == ["NOTIFY_JS"], callers
    assert scripts["NOTIFY_JS"].count("requestPermission()") == 1

    # The only script that reaches that implementation is the one button's delegate.
    invokers = [name for name, script in scripts.items()
                if "__bsNotify.request()" in script]
    assert invokers == ["NOTIFY_REQUEST_JS"], invokers

    # ...and the installer's own last act on load is to paint, never to request.
    installer = scripts["NOTIFY_JS"].rstrip()
    assert installer.endswith("paint();\n}"), installer[-120:]
    assert "request();" not in installer, "the installer must not ask for permission on load"


def test_the_installer_measures_the_browsing_context_rather_than_assuming_it():
    """`http://127.0.0.1:7860` is normally a potentially trustworthy loopback origin, but an
    operator reaching the app by another address is exactly the case where a promise of alerts
    would be false."""
    installer = _script("NOTIFY_JS")
    assert "window.isSecureContext === true" in installer
    assert 'typeof window.Notification === "function"' in installer
    assert 'typeof window.Notification.requestPermission === "function"' in installer


def test_the_dispatcher_ignores_an_empty_or_unrecognised_envelope():
    installer = _script("NOTIFY_JS")
    assert 'typeof text !== "string" || !text.trim()' in installer
    assert "payload.v !== ENVELOPE_VERSION" in installer
    assert "Array.isArray(payload.items)" in installer


def test_the_dispatcher_records_an_identity_before_it_sends_it():
    """Chrome, Windows or Focus Assist may refuse to display it and there is no retry path, so
    recording first makes a second delivery attempt for one terminal event impossible."""
    installer = _script("NOTIFY_JS")
    remember = installer.index("remember(item.id)")
    fire = installer.index("fire(String(item.title", remember)
    assert remember < fire, "the ledger write must precede the send"


def test_the_dispatcher_also_guards_duplicates_WITHIN_one_envelope(tmp_path):
    """R17-A R2. `remember` re-reads `localStorage` each time, so the durable ledger was always
    right -- but the local snapshot taken one line above the loop was not, so two items carrying
    the SAME id inside one envelope each passed the check and called the constructor.

    The server cannot produce that: `_notify_publish` mints a fresh `uuid4` per terminal event, and
    the half of this a Python test can execute is exactly that half. Asserted structurally for the
    client, because there is no JavaScript engine in this suite -- live behaviour is the smoke's
    job, and the rule records which of the two this is.
    """
    installer = _executable(_script("NOTIFY_JS"))
    remember = installer.index("remember(item.id)")
    push = installer.index("delivered.push(item.id)", remember)
    fire = installer.index("fire(String(item.title", remember)
    assert remember < push < fire, "the in-loop snapshot must be updated before the send"

    # The server side of the same guarantee, executed: six terminal events, six distinct ids.
    ns = _notify_namespace(tmp_path)
    for _ in range(6):
        ns["_notify_publish"](_Request("S1"), dn.JOB_RENDER, dn.OUTCOME_SUCCESS)
    identities = [record["id"] for record in _drained(ns, "S1")]
    assert len(identities) == len(set(identities)) == 6


def test_the_test_notification_is_reachable_with_the_app_toggle_off(tmp_path):
    """It is the operator's way of checking CHROME, so gating it on the BeatSync toggle would make
    the one diagnostic in the panel useless in exactly the state they are trying to get out of.

    It reads no setting and calls no server function, so the toggle cannot reach it: the whole
    route is `NOTIFY_TEST_JS` -> `window.__bsNotify.test()` -> `fire(...)`, whose only gate is the
    browser's own `granted`. Pinned so a later change cannot quietly make it conditional.
    """
    delegate = _executable(_script("NOTIFY_TEST_JS"))
    assert "__bsNotify.test()" in delegate
    test_body = _entry_point_body("test")
    assert "fire(TEST_TITLE, TEST_BODY, tag)" in test_body
    for forbidden in ("desktop_notifications", "ENABLED_ON", "enabled", "_option"):
        assert forbidden not in test_body, forbidden

    # ...and the registration carries no server call, so nothing server-side can gate it either.
    call = next(node for node in ast.walk(_tree(_GUI))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and ast.unparse(node.func) == "notify_test_btn.click")
    kwargs = _kwargs(call)
    assert kwargs["fn"] == "None" and kwargs["inputs"] == "[]" and kwargs["outputs"] == "[]"

    # The server-side gate really is OFF in this fixture, and the ledger really is empty -- so a
    # test notification cannot be mistaken for a delivered one.
    ns = _notify_namespace(tmp_path, enabled=False)
    assert ns["_notifications_enabled"]() is False
    assert ns["_NOTIFY_PENDING"] == {}


def test_the_duplicate_ledger_is_shared_across_tabs_and_bounded():
    installer = _executable(_script("NOTIFY_JS"))
    assert "window.localStorage" in installer
    assert "sessionStorage" not in installer, \
        "two tabs of one origin must share the ledger, which sessionStorage would not"
    assert json.dumps(dn.SEEN_STORAGE_KEY) in installer
    assert str(dn.SEEN_LIMIT) in installer
    assert "list.shift()" in installer


def test_clicking_a_notification_focuses_the_page_and_does_nothing_else():
    """"No automatic click, move, render, cleanup, restart or retry." """
    installer = _executable(_script("NOTIFY_JS"))
    assert "window.focus()" in installer
    for forbidden in ("window.location", "window.open", "href", "document.forms",
                      ".submit()", ".click()"):
        assert forbidden not in installer, forbidden


def test_every_word_the_page_shows_comes_from_the_pure_module():
    """One definition of each sentence, so the panel in Settings and the policy cannot drift."""
    installer = _script("NOTIFY_JS")
    for state, sentence in dn.PERMISSION_SENTENCES.items():
        assert json.dumps(sentence) in installer, state
    assert json.dumps(dn.TEST_TITLE) in installer
    assert json.dumps(dn.TEST_BODY) in installer
    for state in dn.BROWSER_PERMISSION_STATES:
        assert json.dumps(state) in installer
    # [FORK] R17-A R3: the acknowledgement vocabulary is substituted in whole, as one JSON object,
    # so a sentence cannot be edited in `gui.py` without the pure module agreeing.
    assert json.dumps(dn.TEST_FEEDBACK_SENTENCES) in installer
    assert json.dumps(dn.CHECK_SENTENCE_TEMPLATE) in installer
    assert json.dumps(dn.FEEDBACK_TIME_TEMPLATE) in installer


def test_the_three_delegates_are_one_line_each_and_hold_no_policy():
    """All the rules live in the installer, so no registration can grow a second interpretation."""
    for name in ("NOTIFY_DISPATCH_JS", "NOTIFY_REQUEST_JS", "NOTIFY_TEST_JS",
                 "NOTIFY_REFRESH_JS"):
        script = _executable(_script(name)).strip()
        assert script.count("\n") == 0, f"{name} is no longer a one-line delegate"
        assert "window.__bsNotify" in script
        assert "new Notification" not in script
        assert "localStorage" not in script


def test_all_four_client_registrations_declare_no_server_function():
    tree = _tree(_GUI)
    seen = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        kwargs = _kwargs(node)
        script = kwargs.get("js")
        if script in _SCRIPT_NAMES:
            seen[script] = (ast.unparse(node.func), kwargs)
    assert set(seen) == set(_SCRIPT_NAMES), seen
    for script, (callee, kwargs) in seen.items():
        assert kwargs["fn"] == "None", (script, kwargs)
        if script == "NOTIFY_JS":
            assert callee == "app.load", script
            assert kwargs["inputs"] == "None" and kwargs["outputs"] == "None"
        else:
            assert kwargs["outputs"] == "[]", (script, kwargs)
    assert seen["NOTIFY_DISPATCH_JS"][1]["inputs"] == "[notify_envelope]"
    assert seen["NOTIFY_REQUEST_JS"][0] == "notify_permission_btn.click"
    assert seen["NOTIFY_TEST_JS"][0] == "notify_test_btn.click"
    assert seen["NOTIFY_REFRESH_JS"][0] == "notify_refresh_btn.click"


def test_the_only_non_javascript_app_load_is_the_settings_reread():
    """`create_ui()` runs once per process, so a value baked at construction time is served to
    every later browser session. One server-side load corrects the saved settings per session,
    reads only in-memory dictionaries, writes only this panel's own components, and carries
    `show_progress='hidden'` -- which is load-bearing: the callable-`value=` form tried first
    leaves a progress overlay on the pinned gradio that never clears.

    [FORK] Digital-Union (Settings Preference Consistency R1, F1): the registered function is now
    `_settings_on_load`, which is settings-wide rather than notification-specific, and the
    collision radio is in its `outputs`. **The count stays one** -- that is the part of this
    contract that matters, and the reason F1 was fixed by widening this registration rather than
    adding a second one.
    """
    tree = _tree(_GUI)
    loads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "app.load"]
    server_side = [node for node in loads
                   if not any(keyword.arg == "js" for keyword in node.keywords)]
    assert len(server_side) == 1, f"{len(server_side)} server-side app.load registrations"
    kwargs = _kwargs(server_side[0])
    assert kwargs["fn"] == "_settings_on_load"
    assert kwargs["inputs"] == "None"
    assert kwargs["outputs"] == "[notify_enabled, notify_app_state, pref_collision]"
    assert kwargs["show_progress"] == "'hidden'"

    # Both the composed reader and the notification reader it delegates to stay inert.
    for name in ("_settings_on_load", "_notify_settings_on_load"):
        body = ast.unparse(_func(tree, name))
        for forbidden in ("open(", "os.", "json", "save_preferences", "subprocess", "requests"):
            assert forbidden not in body, (name, forbidden)

    # It composes the existing readers rather than interpreting a setting a second time.
    composed = ast.unparse(_func(tree, "_settings_on_load"))
    assert "_notify_settings_on_load()" in composed
    assert "_collision_policy()" in composed


def test_the_settings_panel_reread_reports_the_live_setting(tmp_path):
    ns = _notify_namespace(tmp_path, enabled=False)
    ns["gr"].update = lambda **kw: ("gr.update", kw)
    toggle, sentence = ns["_notify_settings_on_load"]()
    assert toggle == ("gr.update", {"value": False})
    assert sentence == dn.app_setting_sentence(False)
    ns["_options"][prefs.DESKTOP_NOTIFICATIONS] = dn.ENABLED_ON
    toggle, sentence = ns["_notify_settings_on_load"]()
    assert toggle == ("gr.update", {"value": True})
    assert sentence == dn.app_setting_sentence(True)


def test_the_settings_panel_warns_that_the_page_has_to_stay_open():
    """A page `Notification()` is not a promise that an alert will fire after the tab is closed,
    and the panel must say so rather than implying persistence."""
    from ui_content import (INFO_NOTIFY_ENABLED, INFO_NOTIFY_SECTION,
                            INFO_NOTIFY_STATUS_PENDING)

    assert "has to stay open" in INFO_NOTIFY_SECTION
    assert "close Chrome" in INFO_NOTIFY_SECTION
    assert "Focus Assist" in INFO_NOTIFY_SECTION
    assert "not as a guarantee" in INFO_NOTIFY_SECTION
    assert "Off by default" in INFO_NOTIFY_ENABLED
    # The placeholder states no permission state; it says the check has not completed.
    assert "Checking" in INFO_NOTIFY_STATUS_PENDING
    for state in dn.PERMISSION_STATES:
        assert state not in INFO_NOTIFY_STATUS_PENDING


def test_the_settings_panel_lists_every_authorised_event_source():
    from ui_content import INFO_NOTIFY_SECTION

    for phrase in ("render", "Variant Lab", "Raw Cutter", "Audio Cutter", "Library"):
        assert phrase in INFO_NOTIFY_SECTION, phrase


# =============================================================================================
# 6. [FORK] R17-A R3: the two diagnostic controls
#
# The owner granted Chrome's permission, received a real Windows notification from the first
# "Send test notification", and then saw nothing from any click afterwards. Two separate defects,
# both in the diagnostics and neither anywhere near a job:
#
#   * the test notification carried ONE FIXED TAG, and a second notification sharing a tag
#     REPLACES the first in place -- silently, with no banner and no sound;
#   * neither diagnostic button acknowledged a click at all. `paint()` returns early when the
#     permission state has not changed, which is the common case, so "Re-check browser status" was
#     indistinguishable from a dead button.
#
# Nothing here touches a job's terminal path, its outcome, its identity or the client ledger.
# =============================================================================================

# --- the tag --------------------------------------------------------------------------------

def test_a_test_notification_carries_a_PREFIX_and_a_job_notification_a_STABLE_tag():
    """The fix is deliberately narrow: it changes the one notification whose entire purpose is to
    be requested *again*, and leaves the per-event tag that guards a genuine duplicate alone."""
    assert dn.TEST_TAG_PREFIX.startswith(dn.TAG_PREFIX), "both stay recognisably ours"
    assert dn.TEST_TAG_PREFIX != dn.TAG_PREFIX
    assert dn.TEST_TAG_PREFIX.endswith("-"), "a prefix, so the per-click token appends cleanly"

    # A real job's tag is untouched: the per-event identity, and nothing minted per click.
    record = dn.build(dn.JOB_RENDER, dn.OUTCOME_SUCCESS, notification_id=_ID)
    assert record["tag"] == dn.TAG_PREFIX + _ID
    assert not record["tag"].startswith(dn.TEST_TAG_PREFIX)
    assert "test" not in record["tag"]


def test_the_old_single_fixed_test_tag_is_gone_from_both_halves():
    """The reproduction: `beatsync-test` as a complete tag is what made every click after the
    first one silently replace its predecessor instead of alerting."""
    assert not hasattr(dn, "TEST_TAG"), "a complete fixed test tag must not exist again"
    installer = _executable(_script("NOTIFY_JS"))
    assert "const TEST_TAG =" not in installer
    assert "TEST_TAG," not in installer, "no constant tag may be passed to the constructor"


def test_every_deliberate_test_click_mints_a_fresh_tag():
    """Unique across reloads (the clock) and within one millisecond (the counter)."""
    body = _entry_point_body("test")
    assert "TEST_TAG_PREFIX + String(Date.now())" in body
    assert "String(++tests)" in body
    # ...and the freshly minted tag is the one that reaches the constructor.
    assert "fire(TEST_TITLE, TEST_BODY, tag)" in body

    installer = _executable(_script("NOTIFY_JS"))
    assert installer.count("let tests = 0;") == 1, "one counter, minted in the installer"


def test_the_job_dispatch_path_keeps_the_per_event_tag_and_mints_nothing():
    """R3's tag change must not reach a job. A job's notification is identified by the terminal
    event it describes, which is what makes the browser-level collapse a real duplicate guard."""
    body = _entry_point_body("dispatch")
    assert "String(item.tag || item.id)" in body
    for forbidden in ("TEST_TAG_PREFIX", "Date.now", "++tests", "say("):
        assert forbidden not in body, forbidden


def test_renotify_was_not_reached_for_instead():
    """It is specified for `ServiceWorkerRegistration.showNotification`, this feature has no
    service worker by accepted scope, and with a unique tag there is nothing to re-alert."""
    for name in _SCRIPT_NAMES:
        assert "renotify" not in _script(name), name


# --- what the panel says back ----------------------------------------------------------------

def test_every_test_result_has_exactly_one_sentence():
    assert set(dn.TEST_FEEDBACK_SENTENCES) == set(dn.TEST_RESULTS)
    assert len(set(dn.TEST_FEEDBACK_SENTENCES.values())) == len(dn.TEST_RESULTS), \
        "two results sharing a sentence would be two obstacles with one fix"
    # A granted permission always resolves to `requested` or `error`, so reporting the state
    # itself means the construction was skipped -- which must not read as a success.
    assert dn.PERMISSION_GRANTED not in dn.TEST_FEEDBACK_SENTENCES


@pytest.mark.parametrize("junk", [None, "", "  ", "granted", "GRANTED", 7, 0, True, False,
                                  object(), [], {}, b"requested", "requested "])
def test_an_unrecognised_test_result_is_never_a_success(junk):
    """The same direction of failure every other refusal in this module takes."""
    assert dn.test_feedback_sentence(junk) == dn.TEST_FEEDBACK_SENTENCES[dn.TEST_UNKNOWN]
    assert "requested successfully" not in dn.test_feedback_sentence(junk)


def test_the_success_wording_describes_the_REQUEST_and_never_the_delivery():
    """A constructor that returned proves the browser accepted the call. Whether Windows drew
    anything is Windows' decision, and the one thing this sentence may not do is claim it did."""
    sentence = dn.test_feedback_sentence(dn.TEST_REQUESTED)
    assert sentence.startswith("Test notification requested successfully."), sentence
    for claim in ("was shown", "was displayed", "has been shown", "was delivered", "you will see",
                  "guaranteed", "Windows showed"):
        assert claim not in sentence, claim
    # ...and it names at least one of the things that can still suppress it.
    assert "Focus Assist" in sentence and "Windows" in sentence


@pytest.mark.parametrize("result,phrase", [
    (dn.PERMISSION_NOT_REQUESTED, "Allow notifications in Chrome"),
    (dn.PERMISSION_DENIED, "Chrome Site Settings"),
    (dn.PERMISSION_UNSUPPORTED, "no notification API"),
])
def test_each_refusal_names_the_obstacle_and_its_actual_fix(result, phrase):
    """Three different obstacles with three different fixes; one shared sentence would send the
    operator to the wrong place in two cases out of three."""
    sentence = dn.test_feedback_sentence(result)
    assert phrase in sentence, sentence
    assert "Nothing was sent" in sentence, sentence


def test_a_refusal_never_claims_beatsync_changed_a_chrome_permission():
    for result in (dn.PERMISSION_DENIED, dn.PERMISSION_UNSUPPORTED, dn.TEST_ERROR):
        sentence = dn.test_feedback_sentence(result)
        for claim in ("BeatSync has changed", "we changed", "reset for you", "unblocked"):
            assert claim not in sentence
    assert "cannot change that" in dn.test_feedback_sentence(dn.PERMISSION_DENIED)


@pytest.mark.parametrize("state", dn.PERMISSION_STATES)
def test_the_recheck_sentence_names_the_measured_state(state):
    sentence = dn.check_sentence(state)
    assert state in sentence
    assert sentence.startswith("Re-checked"), sentence
    assert dn.FEEDBACK_STATE_FIELD not in sentence, "the placeholder must be substituted"


@pytest.mark.parametrize("junk", [None, "", "yes", "default", 7, True, object(), []])
def test_an_unrecognised_permission_state_rechecks_as_unsupported(junk):
    """Exactly how `permission_sentence` resolves it, so the two halves of the panel cannot name
    one state differently."""
    assert dn.check_sentence(junk) == dn.check_sentence(dn.PERMISSION_UNSUPPORTED)
    assert dn.PERMISSION_UNSUPPORTED in dn.check_sentence(junk)


def test_a_feedback_line_is_stamped_so_a_repeated_answer_is_visibly_a_new_answer():
    """The acknowledgement is shown for an UNCHANGED state too, which is the whole fix -- so the
    only moving part on an identical line is the time."""
    first = dn.feedback_line("Re-checked this browser.", "20:14:06")
    second = dn.feedback_line("Re-checked this browser.", "20:14:09")
    assert first != second
    assert "20:14:06" in first and "Re-checked this browser." in first
    assert dn.FEEDBACK_TIME_FIELD not in first and dn.FEEDBACK_MESSAGE_FIELD not in first


@pytest.mark.parametrize("clock", ["", "   ", None, 7, object(), []])
def test_a_clock_that_cannot_be_read_still_leaves_the_measurement(clock):
    """A diagnostic that lost its timestamp must still say what it measured, rather than going
    blank or showing a half-substituted template."""
    assert dn.feedback_line("Re-checked this browser.", clock) == "Re-checked this browser."


@pytest.mark.parametrize("message", ["", "   ", None, 7, object(), [], {}])
def test_an_empty_feedback_message_composes_to_nothing_rather_than_a_bare_timestamp(message):
    assert dn.feedback_line(message, "20:14:06") == ""


def test_no_feedback_wording_can_disturb_the_substitution_that_carries_it():
    """Three real hazards, each measured against this repository's own machinery rather than
    assumed away:

    * a `$` in a JavaScript `String.replace` REPLACEMENT is a substitution pattern (`$&`, `$'`),
      so a sentence containing one would be silently mangled on its way to the panel;
    * a `//` inside a string literal breaks the comment stripper every structural scan in this
      file runs the scripts through -- stated as an invariant in `_executable`'s own docstring;
    * a sentence containing a placeholder token would be rewritten by the next replacement.
    """
    wording = (list(dn.TEST_FEEDBACK_SENTENCES.values())
               + [dn.CHECK_SENTENCE_TEMPLATE, dn.FEEDBACK_TIME_TEMPLATE, dn.TEST_TITLE,
                  dn.TEST_BODY]
               + list(dn.PERMISSION_SENTENCES.values()))
    for text in wording:
        assert "$" not in text, text
        assert "//" not in json.dumps(text), text
    for text in dn.TEST_FEEDBACK_SENTENCES.values():
        assert dn.FEEDBACK_TIME_FIELD not in text
        assert dn.FEEDBACK_MESSAGE_FIELD not in text
    assert dn.FEEDBACK_STATE_FIELD in dn.CHECK_SENTENCE_TEMPLATE
    assert dn.FEEDBACK_TIME_FIELD in dn.FEEDBACK_TIME_TEMPLATE
    assert dn.FEEDBACK_MESSAGE_FIELD in dn.FEEDBACK_TIME_TEMPLATE


# --- the client half, structurally ------------------------------------------------------------

def test_every_diagnostic_click_writes_the_acknowledgement_line():
    """"Visible feedback after every diagnostic button click" -- all three of them, including the
    permission gesture, which produces no Chrome prompt at all once permission is granted."""
    assert "say(sentence(result))" in _entry_point_body("test")
    assert "checked()" in _entry_point_body("recheck")
    assert "checked()" in _entry_point_body("request")


def test_the_mutation_observer_does_NOT_write_the_acknowledgement_line():
    """It fires on every DOM mutation, and a render emitting frames must not scroll a timestamped
    diagnostic underneath the operator. `paint` is left exactly as it was."""
    installer = _executable(_script("NOTIFY_JS"))
    paint = installer[installer.index("const paint = () =>"):installer.index("const seen = ()")]
    for forbidden in ("say(", "checked(", "Date.now", "toLocaleTimeString", "FEEDBACK_ID"):
        assert forbidden not in paint, forbidden
    assert "new MutationObserver(paint)" in installer, "still the painter, not the reporter"


def test_the_recheck_button_no_longer_delegates_to_the_early_returning_painter():
    """`paint()` returns when `data-state` is unchanged, which is why pressing this button on a
    settled permission used to do nothing observable whatsoever."""
    delegate = _executable(_script("NOTIFY_REFRESH_JS")).strip()
    assert "__bsNotify.recheck()" in delegate
    assert "paint()" not in delegate
    # The early return is still there, because the observer depends on it.
    installer = _executable(_script("NOTIFY_JS"))
    assert 'if (host.getAttribute("data-state") === now) { return; }' in installer


def test_the_constructor_outcome_is_REPORTED_rather_than_reduced_to_a_boolean():
    """"Nothing was sent because Chrome has not been asked" and "the constructor threw" have
    different fixes and must not collapse into one sentence."""
    installer = _executable(_script("NOTIFY_JS"))
    fire = installer[installer.index("const fire = ("):installer.index("window.__bsNotify = {")]
    assert "if (now !== GRANTED) { return now; }" in fire, "the obstacle is named"
    assert "return TEST_REQUESTED;" in fire
    assert "return TEST_ERROR;" in fire
    assert "return false" not in fire and "return true" not in fire


def test_the_client_composes_a_feedback_line_the_way_the_pure_module_does():
    """`feedback_line` is the reference implementation of a composition the browser performs,
    exactly as `decode_envelope` is for the dispatcher. These are the two substitutions it makes,
    pinned so the two halves cannot drift -- there is no JavaScript engine in this suite."""
    installer = _executable(_script("NOTIFY_JS"))
    assert "LINE_TEMPLATE.replace(TIME_FIELD, at).replace(MESSAGE_FIELD, message)" in installer
    assert "CHECK_TEMPLATE.replace(STATE_FIELD, state())" in installer


def test_the_acknowledgement_writer_cannot_throw_on_the_page():
    """The Settings panel is rendered lazily, so the host element legitimately may not exist --
    and a diagnostic must never be the thing that takes a page down."""
    installer = _executable(_script("NOTIFY_JS"))
    say = installer[installer.index("const say = ("):installer.index("const sentence = (")]
    assert "if (!host) { return; }" in say
    clock = installer[installer.index("const clockText = ("):installer.index("const say = (")]
    assert "try {" in clock and "catch (e) { return \"\"; }" in clock
    # An unrecognised result falls back rather than reading `undefined` onto the panel.
    assert "TEST_FEEDBACK[result] || TEST_FEEDBACK[TEST_UNKNOWN]" in installer


def test_the_diagnostics_still_call_no_server_function_and_write_no_setting():
    """R3 adds words to a panel, not an endpoint. The three diagnostic registrations stay
    `fn=None` with empty `inputs` and `outputs`, so nothing server-side can gate or observe one."""
    tree = _tree(_GUI)
    for registration in ("notify_permission_btn.click", "notify_test_btn.click",
                         "notify_refresh_btn.click"):
        call = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and ast.unparse(node.func) == registration)
        kwargs = _kwargs(call)
        assert kwargs["fn"] == "None", registration
        assert kwargs["inputs"] == "[]" and kwargs["outputs"] == "[]", registration
    for name in _SCRIPT_NAMES:
        script = _executable(_script(name))
        for forbidden in ("preferences", "localStorage.setItem(\"beatsyncNotifyEnabled",
                          "ENABLED_ON", "ENABLED_OFF"):
            assert forbidden not in script, (name, forbidden)


@pytest.mark.parametrize("name", _SCRIPT_NAMES)
def test_no_substitution_placeholder_survives_into_a_shipped_script(name):
    """A `__TOKEN__` left behind is a JavaScript syntax error that no structural assertion in this
    file would otherwise notice -- it would simply stop the whole installer compiling in the
    browser, which is how `NOTIFY_JS` fails silently and completely.

    `__bsNotify` is the deliberate exception: it is the installed object's own name.
    """
    leftovers = [token for token in re.findall(r"__[A-Z][A-Z0-9_]*__", _script(name))]
    assert leftovers == [], leftovers


# =============================================================================================
# 7. R4: a producer must hand the publisher the BROWSER session, not its own request record
# =============================================================================================
#
# The defect this section exists for was invisible to every assertion above it. The ledger is
# keyed by `gr.Request.session_hash`, `_notify_session_key` answers `''` for an object that has
# none, and `_notify_publish` then returns at its `if not key` guard -- no exception, no record,
# no toast. So a producer that passes the WRONG object is indistinguishable, from the inside,
# from a job that had nothing to announce.
#
# `render_selected_variants_guarded` did exactly that: `request, refusal =
# fork_render_batch.build_request(...)` rebound the injected parameter, and the terminal
# `_notify_render_batch(request, outcome)` handed the publisher a `RenderBatchRequest`. Measured
# live: two real 2/2-SUCCESS Variant Lab batches produced zero notifications, while a Raw Cutter
# batch in the same browser session delivered normally.
#
# Two kinds of test, because either one alone would have missed it:
#
#   * the STRUCTURAL one below generalises the defect to all seven producers, and would have
#     caught this before it shipped;
#   * the BEHAVIOURAL ones execute the real wrapper against the real translator, publisher,
#     ledger and drain. Nothing on the notification path is stubbed -- which is the gap that let
#     it through, since the only suite that ran this body replaced `_notify_render_batch` with a
#     no-op, and the only suite that ran the translator never ran this body.

_INJECTED_PRODUCERS = ("process_video_guarded", "render_selected_variants_guarded",
                       "_on_cut_run", "_on_cut_batch_run_ui", "_on_audio_cut",
                       "_on_prep_scan_click", "_on_prep_analyze_click")


@pytest.mark.parametrize("handler", _INJECTED_PRODUCERS)
def test_no_producer_rebinds_the_injected_request(handler):
    """The injected `gr.Request` must still be the `gr.Request` when the terminal path reads it.

    A producer's body is long, and "a request" is a natural name for more than one thing in it --
    the batch's own `RenderBatchRequest` being the obvious case. Rebinding the parameter is
    therefore an easy, silent mistake, and its consequence is a notification that is never made
    rather than an error anybody sees.
    """
    node = _func(_tree(_GUI), handler)
    assert node.args.args[-1].arg == "request", handler
    rebinds = [child for child in ast.walk(node)
               if isinstance(child, ast.Name) and child.id == "request"
               and isinstance(child.ctx, (ast.Store, ast.Del))]
    assert rebinds == [], (
        f"{handler} rebinds its injected `request` at line(s) "
        f"{[child.lineno for child in rebinds]} -- the session is then unreachable from the "
        f"terminal notification path, and the publisher fails silently")


def test_the_batch_names_its_own_request_record_separately():
    """The positive half of the rule above, pinned at the one call site that needs the
    distinction. Asserting only the absence of a rebind would also be satisfied by deleting the
    request entirely.
    """
    body = ast.unparse(_func(_tree(_GUI), "render_selected_variants_guarded"))
    assert "batch_request, refusal = fork_render_batch.build_request(" in body
    assert "_notify_render_batch(request, outcome)" in body
    assert "requested_count=batch_request.count" in body


# --- the real wrapper, the real ledger ---------------------------------------------------------

class _NoSkip:
    def __repr__(self):                                 # pragma: no cover - debugging aid only
        return "<skip>"


def _batch_namespace(tmp_path, *, enabled=True):
    """`_notify_namespace` PLUS the shipped batch wrapper, executed into the same namespace.

    Sharing one namespace is the point: the `_notify_render_batch`, `_notify_publish`, ledger and
    `_notify_drain` the wrapper reaches are the real ones already defined there, so a record has
    to travel the whole way from the terminal path to a decoded envelope.
    """
    import threading as _threading
    import uuid as _uuid

    import beatsync_fork.render_batch as fork_render_batch
    import beatsync_fork.render_worker as fork_render_worker

    namespace = _notify_namespace(tmp_path, enabled=enabled)
    namespace["gr"].skip = _NoSkip
    lifecycles: list = []
    started: list = []
    plan: dict = {}

    def tracking_lifecycle(invocation_id):
        lifecycle = fork_render_worker.RenderLifecycle(invocation_id=invocation_id)
        lifecycles.append(lifecycle)
        return lifecycle

    def guarded_unlocked(*args, **kwargs):
        index = len(started)
        started.append(index)
        session_state = args[24]

        def stream():
            step = plan.get(index, {})
            if step.get("cancel"):
                lifecycles[-1].request_cancel()
            session_state["render_outcome_kind"] = step.get("kind")
            session_state["last_output_path"] = step.get("durable", "")
            session_state["audio_layers_report"] = ""
            session_state["smart_mix_report"] = ""
            yield (None, "inner status", session_state, "", "", "inv")

        return stream()

    namespace.update({
        "uuid": _uuid,
        "threading": _threading,
        "fork_render_batch": fork_render_batch,
        "RenderLifecycle": tracking_lifecycle,
        "RenderLifecycleState": fork_render_worker.RenderLifecycleState,
        "RenderOutcomeKind": fork_render_worker.RenderOutcomeKind,
        "RenderCancelled": fork_render_worker.RenderCancelled,
        "RENDER_OUTCOME_KEY": "render_outcome_kind",
        "LAST_OUTPUT_PATH_KEY": "last_output_path",
        "AUDIO_LAYERS_REPORT_KEY": "audio_layers_report",
        "SMART_MIX_REPORT_KEY": "smart_mix_report",
        "RENDER_BUSY_MESSAGE": "busy",
        "_RENDER_LOCK": _threading.Lock(),
        "_busy_message": lambda: "busy",
        "_mark_lock_holder": lambda token=None: None,
        "_LOCK_HOLDER_RENDER": "render",
        "_install_active_render": lambda lifecycle: None,
        "_clear_active_render": lambda invocation_id: None,
        "_render_batch_request_tag": lambda: "T",
        "_process_video_guarded_unlocked": guarded_unlocked,
        "Iterator": None, "Tuple": None, "VideoFilesInput": None,
    })
    exec(compile(ast.Module(body=[_func(_tree(_GUI), "render_selected_variants_guarded")],
                            type_ignores=[]), _GUI, "exec"), namespace)      # noqa: S102
    namespace["_plan"] = plan
    namespace["_started"] = started
    namespace["_lifecycles"] = lifecycles
    return namespace


def _drive_batch(namespace, session, *, selection=(0, 1), count=2):
    """Run the real wrapper to exhaustion for one browser session."""
    import test_render_failure_classification as rfc

    stream = namespace["render_selected_variants_guarded"](
        rfc._root_92_batch(count=count), list(selection), "track.mp3", None, 2.0, 1.0, True, "",
        rfc.fork_smart_mix.ROLE_ORDER,
        "folder", "C:/src", False, None, "music_video", "h264", 30.0, {}, None,
        request=_Request(session))
    for _ in stream:
        pass


_SUCCESS = {"kind": RenderOutcomeKind.SUCCESS, "durable": "C:/out/c.mp4"}


def test_a_real_successful_batch_delivers_one_notification_to_its_own_session(tmp_path):
    """The regression. Against `bdb342fd` this drains NOTHING, because the publisher never saw a
    session: the injected request had been overwritten by the batch's own request record."""
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: _SUCCESS, 1: _SUCCESS})

    _drive_batch(ns, "S1")

    assert ns["_started"] == [0, 1], "both candidates must have run"
    records = _drained(ns, "S1")
    assert len(records) == 1, f"expected one batch notification, got {len(records)}"
    assert records[0]["job"] == dn.JOB_VARIANT_BATCH
    assert records[0]["outcome"] == dn.OUTCOME_SUCCESS
    assert "2 completed" in records[0]["body"]
    assert records[0]["tag"] == dn.TAG_PREFIX + records[0]["id"]


def test_the_batch_notification_reaches_no_other_session(tmp_path):
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: _SUCCESS, 1: _SUCCESS})

    _drive_batch(ns, "S1")

    assert _drained(ns, "S2") == [], "another browser session must not read this batch's record"
    assert len(_drained(ns, "S1")) == 1


def test_a_second_drain_does_not_redeliver_the_batch(tmp_path):
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: _SUCCESS, 1: _SUCCESS})

    _drive_batch(ns, "S1")

    assert len(_drained(ns, "S1")) == 1
    assert _drained(ns, "S1") == [], "consume-once still holds for the batch"


def test_a_real_partly_failed_batch_keeps_its_PARTIAL_classification(tmp_path):
    """One candidate durable, one locally failed. The run attempted its whole selection, so the
    batch has no terminal cause of its own and the counts decide -- exactly as before R4."""
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: _SUCCESS,
                        1: {"kind": RenderOutcomeKind.CANDIDATE_LOCAL, "durable": ""}})

    _drive_batch(ns, "S1")

    assert ns["_started"] == [0, 1], "a CANDIDATE_LOCAL failure must not stop the batch"
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["outcome"] == dn.OUTCOME_PARTIAL
    assert "1 completed" in records[0]["body"] and "1 failed" in records[0]["body"]


def test_a_real_cancelled_batch_keeps_its_CANCELLED_classification(tmp_path):
    """Cancellation wins over the counts, and the untried candidate is reported as not attempted."""
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: {"kind": RenderOutcomeKind.CANCELLED, "durable": "", "cancel": True}})

    _drive_batch(ns, "S1")

    assert ns["_started"] == [0], "a cancelled batch must not start the second candidate"
    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["outcome"] == dn.OUTCOME_CANCELLED
    assert "1 not attempted" in records[0]["body"]


def test_a_real_wholly_failed_batch_is_a_FAILURE_not_a_PARTIAL(tmp_path):
    ns = _batch_namespace(tmp_path)
    ns["_plan"].update({0: {"kind": RenderOutcomeKind.SHARED_FATAL, "durable": ""}})

    _drive_batch(ns, "S1")

    records = _drained(ns, "S1")
    assert len(records) == 1
    assert records[0]["outcome"] == dn.OUTCOME_FAILED


def test_a_refused_batch_announces_nothing(tmp_path):
    """`build_request` refuses a selection that is not 2-4 distinct candidates. Nothing was
    rendered, the refusal is already on screen, and a toast would describe a job that never ran."""
    ns = _batch_namespace(tmp_path)

    _drive_batch(ns, "S1", selection=(0,), count=2)

    assert ns["_started"] == [], "a refused batch must render nothing"
    assert _drained(ns, "S1") == []


def test_the_batch_still_respects_the_application_toggle(tmp_path):
    """The record is made; only DELIVERY is gated, and a gated record is discarded rather than
    held back for a later click to deliver out of context."""
    ns = _batch_namespace(tmp_path, enabled=False)
    ns["_plan"].update({0: _SUCCESS, 1: _SUCCESS})

    _drive_batch(ns, "S1")

    assert ns["_NOTIFY_PENDING"]["S1"], "the terminal path still recorded the batch"
    assert _drained(ns, "S1") == []
    assert ns["_NOTIFY_PENDING"] == {}
