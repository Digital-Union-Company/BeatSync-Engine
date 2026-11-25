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
import threading
import types
import uuid
from collections import OrderedDict

import pytest

from beatsync_fork import desktop_notify as dn
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
_WANTED_ASSIGNS = ("_NOTIFY_LOCK", "_NOTIFY_PENDING", "NOTIFY_SESSION_CAP", "NOTIFY_QUEUE_CAP",
                   "_NOTIFY_RENDER_OUTCOMES", "_CUT_BATCH_NOTIFY_OUTCOMES", "_PREP_NOTIFY_JOBS",
                   "NOTIFY_STATUS_ELEMENT_ID")

_WANTED_FUNCTIONS = ("_notify_session_key", "_notifications_enabled", "_notify_publish",
                     "_notify_drain", "_notify_render", "_notify_render_batch", "_notify_cut",
                     "_notify_cut_batch", "_notify_audio_cut", "_notify_library",
                     "_notify_settings_on_load", "_notify_status_panel_html",
                     "_on_notifications_enabled_change")


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
    toasts: list = []
    namespace: dict = {
        "threading": threading,
        "OrderedDict": OrderedDict,
        "uuid": uuid,
        "html_lib": __import__("html"),
        "fork_notify": dn,
        "fork_prefs": prefs,
        "fork_cutter_batch": fork_cutter_batch,
        "fork_prep_activity": fork_prep_activity,
        "RenderOutcomeKind": RenderOutcomeKind,
        "INFO_NOTIFY_STATUS_PENDING": "Checking this browser…",
        "STATUS_NOTIFY_SAVED_ON": "saved on",
        "STATUS_NOTIFY_SAVED_OFF": "saved off",
        "PREFS_PATH": str(tmp_path / "preferences.json"),
        "_FOLDER_PREFS": prefs.empty_preferences(),
        "_FOLDER_OPTIONS": options,
        "_option": lambda key: options.get(key, prefs.OPTION_DEFAULTS.get(key, "")),
        "gr": types.SimpleNamespace(Info=lambda text: toasts.append(("info", text)),
                                    Warning=lambda text: toasts.append(("warning", text))),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])),
                 _GUI, "exec"), namespace)      # noqa: S102 - the body under test
    namespace["_toasts"] = toasts
    namespace["_options"] = options
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


def test_the_browser_status_placeholder_states_no_permission_state(tmp_path):
    """Only the browser knows its own `Notification.permission`, so a server-rendered `granted`
    would be a guess that goes stale the moment Chrome Site Settings changes."""
    ns = _notify_namespace(tmp_path)
    html = ns["_notify_status_panel_html"]()
    assert ns["NOTIFY_STATUS_ELEMENT_ID"] in html
    assert 'data-state="checking"' in html
    for state in dn.PERMISSION_STATES:
        assert state not in html, state


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
    (fork_cutter_batch.TERMINAL_COMPLETED, dn.OUTCOME_SUCCESS),
    (fork_cutter_batch.TERMINAL_COMPLETED_WITH_FAILURES, dn.OUTCOME_PARTIAL),
    (fork_cutter_batch.TERMINAL_CANCELLED, dn.OUTCOME_CANCELLED),
    (fork_cutter_batch.TERMINAL_FAILED, dn.OUTCOME_FAILED),
])
def test_the_folder_batch_renders_the_same_terminal_class_the_panel_does(
        tmp_path, terminal, expected):
    ns = _notify_namespace(tmp_path)
    ns["_notify_cut_batch"](_Request("S1"), terminal, completed=27, failed=1,
                            cancelled=0, not_attempted=0)
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
    them apart is what lets the server rewrite its half without erasing the live answer."""
    tree = _tree(_GUI)
    change = next(call for call in _calls(tree, "change")
                  if ast.unparse(call.func) == "notify_enabled.change")
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
    for name in ("notify_enabled.change", "notify_permission_btn.click",
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
    # Three: finished, cancelled, crashed. Every `refuse(...)` path performed no work.
    "_on_cut_run": ("_notify_cut", 3),
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
    namespace = {"json": json, "fork_notify": dn,
                 "NOTIFY_STATUS_ELEMENT_ID": "bs-notify-permission"}
    exec(rendered + tail, namespace)        # noqa: S102 - the shipped assignment under test
    return namespace[name]


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
    every later browser session. One server-side load corrects the toggle per session, reads one
    in-memory dictionary, writes only this panel's own two components, and carries
    `show_progress='hidden'` -- which is load-bearing: the callable-`value=` form tried first
    leaves a progress overlay on the pinned gradio that never clears."""
    tree = _tree(_GUI)
    loads = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and ast.unparse(node.func) == "app.load"]
    server_side = [node for node in loads
                   if not any(keyword.arg == "js" for keyword in node.keywords)]
    assert len(server_side) == 1, f"{len(server_side)} server-side app.load registrations"
    kwargs = _kwargs(server_side[0])
    assert kwargs["fn"] == "_notify_settings_on_load"
    assert kwargs["inputs"] == "None"
    assert kwargs["outputs"] == "[notify_enabled, notify_app_state]"
    assert kwargs["show_progress"] == "'hidden'"

    body = ast.unparse(_func(tree, "_notify_settings_on_load"))
    for forbidden in ("open(", "os.", "json", "save_preferences", "subprocess", "requests"):
        assert forbidden not in body, forbidden


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
