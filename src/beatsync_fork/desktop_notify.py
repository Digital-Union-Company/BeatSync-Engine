"""R17-A: what a desktop notification for a finished long job is allowed to say.

This module is the whole policy layer of the feature and it deliberately does **nothing**. It
mints no identity, reads no clock, touches no browser, holds no ledger and sends nothing: it
turns *a job kind plus a typed terminal outcome plus a few integers* into one short line of text,
and refuses anything it cannot vouch for. `gui.py` performs every side effect — it keeps the
per-session ledger, it decides when a job is terminal, and it hands the envelope to the one
client-side dispatcher.

Four rules shape it, and each one is a thing the accepted scope says must not happen:

* **Content cannot leak a location, because no caller may supply text.** :func:`build` takes a
  job-kind token, an outcome token and integers. The words come from the tables in this file. A
  source path, a media filename, an output folder or an FFmpeg stderr line has no parameter to
  arrive through, which is a stronger guarantee than sanitising a string a caller passed in. As a
  second, independent check :func:`is_safe_text` is applied to the composed result and
  :func:`build` answers ``None`` rather than emit something that looks like a path.

* **An unknown outcome is not a success.** :func:`build` answers ``None`` for a job kind or an
  outcome it does not recognise, so a future caller that forgets to classify itself announces
  nothing instead of announcing the cheerful default. "Do not announce success while the result
  is unknown" is implemented as a refusal, not as a fallback.

* **The app setting is a plain stored string.** ``user_prefs`` stores options as strings and
  :func:`load_options` keeps only non-empty strings, so the toggle persists as ``"on"`` / ``"off"``
  through the existing mechanism with no schema bump and no new file. :func:`is_enabled` is total
  and only the exact token ``"on"`` means on — a corrupt or hand-edited value reads as **off**,
  which is the direction a notification setting must fail in.

* **The browser's own answer is the authority on permission, and this module only explains it.**
  :data:`BROWSER_PERMISSION_STATES` maps what ``Notification.permission`` actually returns onto the
  four states the accepted scope names, and :func:`permission_sentence` is the one place the
  wording for each lives. The sentences are injected into the client script rather than written
  twice, so the panel in Settings and this file can never disagree.

Stdlib only (``json``), so the fork package's no-runtime-import rule still holds.
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------------------------
# Job kinds. One token per authorised event source in the accepted R17-A scope, and nothing else:
# a notification exists only where the server already has a reliable terminal result, never for a
# timer, a guessed duration or "the FFmpeg process disappeared".
# ---------------------------------------------------------------------------------------------

JOB_RENDER = "render"
JOB_VARIANT_BATCH = "variant_batch"
JOB_RAW_CUTTER = "raw_cutter"
JOB_RAW_CUTTER_BATCH = "raw_cutter_batch"
JOB_AUDIO_CUTTER = "audio_cutter"
JOB_LIBRARY_ANALYSIS = "library_analysis"
JOB_LIBRARY_SCAN = "library_scan"

#: What the operator is told the job WAS. Deliberately the workflow's own name as it appears on
#: screen, and deliberately free of every character :func:`is_safe_text` rejects, so no label can
#: ever read as a path fragment.
JOB_LABELS = {
    JOB_RENDER: "Music video render",
    JOB_VARIANT_BATCH: "Variant Lab batch",
    JOB_RAW_CUTTER: "Raw Cutter",
    JOB_RAW_CUTTER_BATCH: "Raw Cutter batch",
    JOB_AUDIO_CUTTER: "Audio Cutter",
    JOB_LIBRARY_ANALYSIS: "Library analysis",
    JOB_LIBRARY_SCAN: "Library scan",
}

JOB_KINDS = tuple(JOB_LABELS)

# ---------------------------------------------------------------------------------------------
# Terminal outcomes. Four, exactly as the accepted scope requires, and PARTIAL is a first-class
# answer rather than a decorated success: "27 completed, 1 failed" is neither a success nor a
# failure and must not be reported as either.
# ---------------------------------------------------------------------------------------------

OUTCOME_SUCCESS = "success"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_CANCELLED = "cancelled"

#: How each outcome is spoken. The wording is the whole difference between a truthful toast and a
#: misleading one, so it lives here and nowhere else.
OUTCOME_VERBS = {
    OUTCOME_SUCCESS: "finished",
    OUTCOME_PARTIAL: "finished with problems",
    OUTCOME_FAILED: "failed",
    OUTCOME_CANCELLED: "cancelled",
}

OUTCOMES = tuple(OUTCOME_VERBS)

#: The lock-screen-safe fallback body for an outcome that carries no counts at all. One sentence
#: per outcome, because "finished" with nothing after it reads as truncated.
OUTCOME_BODIES = {
    OUTCOME_SUCCESS: "The job finished. Open BeatSync for the full result.",
    OUTCOME_PARTIAL: "The job finished, but not everything succeeded. Open BeatSync for details.",
    OUTCOME_FAILED: "The job did not finish. Open BeatSync for the reported reason.",
    OUTCOME_CANCELLED: "The job was stopped. Open BeatSync for what was kept.",
}

#: Count fields a caller may supply, in the fixed order they are spoken. Fixed so the same job
#: always produces the same sentence — an operator comparing two notifications must not see a
#: difference that is not there.
COUNT_FIELDS = ("completed", "failed", "skipped", "cancelled", "not_attempted")

COUNT_WORDS = {
    "completed": "completed",
    "failed": "failed",
    "skipped": "skipped",
    "cancelled": "cancelled",
    # The Raw Cutter batch and the Variant Lab batch both distinguish a source they STOPPED
    # before reaching from one they reached and skipped. Reporting both as "skipped" would make a
    # cancelled batch look like it had examined every file and declined most of them.
    "not_attempted": "not attempted",
}

#: The product name, used as the notification's title prefix. One constant, so the Windows Action
#: Center groups every BeatSync toast together.
APP_NAME = "BeatSync"

#: The separator between the headline and the counts. A middle dot rather than a colon, because a
#: colon is one of the characters :func:`is_safe_text` rejects.
DETAIL_SEPARATOR = " · "

#: What a notification's `tag` is prefixed with. Chrome collapses two notifications sharing a tag,
#: so a tag built from the per-event id is a second, browser-level duplicate guard underneath the
#: client ledger.
TAG_PREFIX = "beatsync-"

# ---------------------------------------------------------------------------------------------
# The stored application preference.
# ---------------------------------------------------------------------------------------------

#: Stored in `preferences.json` under the existing `options` section. Strings, because
#: `user_prefs.load_options` keeps only non-empty strings and a boolean would silently vanish.
ENABLED_ON = "on"
ENABLED_OFF = "off"

#: OFF by default, which the accepted scope requires. An operator who never opens Settings must
#: see no change whatsoever.
ENABLED_DEFAULT = ENABLED_OFF


def is_enabled(value) -> bool:
    """Is the app-level toggle on? Total, and **only** the exact token means on.

    Deliberately not `bool(value)` and deliberately not a truthy-string test: a corrupt,
    hand-edited, half-written or unexpected value must read as OFF. A notification setting that
    failed open would announce an operator's job titles to a desktop they never agreed to.
    """
    return isinstance(value, str) and value.strip().lower() == ENABLED_ON


def option_value(enabled) -> str:
    """The string to store for a checkbox state. The inverse of :func:`is_enabled`."""
    return ENABLED_ON if bool(enabled) else ENABLED_OFF


# ---------------------------------------------------------------------------------------------
# Browser permission, as four states with one explanation each.
# ---------------------------------------------------------------------------------------------

PERMISSION_NOT_REQUESTED = "not requested"
PERMISSION_GRANTED = "granted"
PERMISSION_DENIED = "denied"
PERMISSION_UNSUPPORTED = "unsupported"

PERMISSION_STATES = (PERMISSION_NOT_REQUESTED, PERMISSION_GRANTED,
                     PERMISSION_DENIED, PERMISSION_UNSUPPORTED)

#: What `Notification.permission` actually returns, mapped onto the four states above. Defined
#: here rather than in the client script so there is one table, and so a test can assert that the
#: browser's `"default"` is reported as *not requested* rather than as a denial — they are very
#: different things to tell an operator.
BROWSER_PERMISSION_STATES = {
    "default": PERMISSION_NOT_REQUESTED,
    "granted": PERMISSION_GRANTED,
    "denied": PERMISSION_DENIED,
}

#: One plain explanation per state. These are injected into the client script, so the panel in
#: Settings cannot drift from what this module says.
#:
#: The denied sentence is the one that has to be exactly right: BeatSync cannot revoke or restore
#: a Chrome per-origin permission, and must never imply that it did. Changing it is a Chrome Site
#: Settings action, so that is what the sentence says.
PERMISSION_SENTENCES = {
    PERMISSION_NOT_REQUESTED: (
        "Chrome has not been asked yet. Use “Allow notifications in Chrome” below; "
        "BeatSync never asks on its own."),
    PERMISSION_GRANTED: (
        "Chrome will show BeatSync notifications. Windows Focus Assist, Do Not Disturb or a "
        "notification setting can still suppress one, so this is not a guarantee of an alert."),
    PERMISSION_DENIED: (
        "Chrome is blocking notifications for this address. BeatSync cannot change that and has "
        "not changed it — reset it in Chrome Site Settings for this page if you want them."),
    PERMISSION_UNSUPPORTED: (
        "This browser does not offer the notification API in this page’s context, so no "
        "desktop alert can be shown. Everything in BeatSync works exactly as before."),
}


def permission_sentence(state) -> str:
    """The explanation for a permission state. Total: an unknown state reads as unsupported.

    Failing towards *unsupported* is the honest direction. The alternative — reporting an
    unrecognised state as granted — would promise an alert that may never arrive.
    """
    if isinstance(state, str) and state in PERMISSION_SENTENCES:
        return PERMISSION_SENTENCES[state]
    return PERMISSION_SENTENCES[PERMISSION_UNSUPPORTED]


#: The app-setting half of the Settings read-out. Separate from the permission half on purpose:
#: the accepted scope requires both to be shown as DISTINCT states, because "BeatSync is set not
#: to notify" and "Chrome is blocking notifications" have different fixes.
APP_SETTING_SENTENCES = {
    True: "Desktop notifications are ON in BeatSync.",
    False: "Desktop notifications are OFF in BeatSync. Nothing is sent, whatever Chrome allows.",
}


def app_setting_sentence(enabled) -> str:
    return APP_SETTING_SENTENCES[bool(enabled)]


# ---------------------------------------------------------------------------------------------
# The test notification, worded here so the client script carries no text of its own.
# ---------------------------------------------------------------------------------------------

TEST_TITLE = APP_NAME + " — test notification"
TEST_BODY = ("This is what a finished job will look like. No job was started and nothing was "
             "changed.")

# ---------------------------------------------------------------------------------------------
# The client-side duplicate ledger, declared here so the numbers are reviewable in Python.
# ---------------------------------------------------------------------------------------------

#: `localStorage` key the dispatcher records delivered notification ids under. `localStorage` is
#: shared by every tab of the origin, which is exactly why it is used: two BeatSync tabs that
#: somehow both received one terminal event must still produce one notification.
SEEN_STORAGE_KEY = "beatsyncNotifySeen"

#: How many delivered ids the ledger remembers. Bounded, because this is a duplicate guard and not
#: a history: it has to outlive a reconnect, not a reinstall.
SEEN_LIMIT = 64

#: The envelope's schema tag. A dispatcher that does not recognise it does nothing, so an older
#: page served from a cache cannot misread a newer payload.
ENVELOPE_VERSION = 1

#: What the envelope component holds when there is nothing to deliver. The dispatcher ignores it,
#: which is what makes "no notification on page load" structural rather than a check.
ENVELOPE_EMPTY = ""

# ---------------------------------------------------------------------------------------------
# Content safety.
# ---------------------------------------------------------------------------------------------

#: Characters that make a string look like a filesystem location or a URL. Checked on the
#: COMPOSED text as an independent backstop: the real guarantee is that no caller may pass text in
#: at all, and this exists so that a future label or an accidental interpolation cannot quietly
#: become the first leak.
FORBIDDEN_CHARACTERS = ("\\", "/", ":", "\n", "\r", "\t", "\0", "~", "%", "<", ">", "\"", "|")


def is_safe_text(text) -> bool:
    """Is this short enough, and free of anything that reads as a path, URL or log line?

    Deliberately crude and deliberately a *refusal* rather than a sanitiser. A sanitiser that
    stripped a drive letter would still have put the rest of somebody's folder name on a Windows
    lock screen.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if len(text) > 160:
        return False
    return not any(bad in text for bad in FORBIDDEN_CHARACTERS)


# ---------------------------------------------------------------------------------------------
# Identity. This module VALIDATES one; it never mints one, because that would need randomness or
# a clock and would make every branch here untestable without stubbing.
# ---------------------------------------------------------------------------------------------

#: An id's permitted alphabet. `uuid4().hex` satisfies it; so does a hyphenated or underscored
#: token. Anything else — a path, a space, a quote — is refused, because the id reaches the client
#: inside a JSON envelope and is used as a storage key and a notification tag.
_ID_CHARACTERS = frozenset("0123456789abcdefghijklmnopqrstuvwxyz"
                           "ABCDEFGHIJKLMNOPQRSTUVWXYZ-_")

ID_MIN_LENGTH = 8
ID_MAX_LENGTH = 64


def is_valid_id(value) -> bool:
    """Is this a usable per-event notification id?"""
    if not isinstance(value, str):
        return False
    if not ID_MIN_LENGTH <= len(value) <= ID_MAX_LENGTH:
        return False
    return all(character in _ID_CHARACTERS for character in value)


# ---------------------------------------------------------------------------------------------
# The record, and the one builder.
# ---------------------------------------------------------------------------------------------

def _count(value):
    """An integer count, or `None` for "not supplied".

    A count is *reported*, so a value that is not a plain whole number is dropped rather than
    guessed at: an operator reading "3.0 completed" would rightly distrust the whole line. A
    negative count is nonsense and is dropped for the same reason — clamping it to zero would
    report a number nobody measured.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def detail_text(counts) -> str:
    """`"27 completed, 1 failed"`, or `""` when nothing countable was supplied.

    Only fields that were actually supplied appear, and a supplied zero DOES appear: "0 completed"
    after a batch of forty is information, and suppressing it would make a total failure look like
    a job with nothing to say.
    """
    parts = []
    for field in COUNT_FIELDS:
        amount = _count((counts or {}).get(field))
        if amount is None:
            continue
        parts.append(f"{amount} {COUNT_WORDS[field]}")
    return ", ".join(parts)


def build(job_kind, outcome, *, notification_id, counts=None):
    """One notification, or ``None`` when it cannot be vouched for.

    ``None`` is returned — never an approximation — when the job kind is unknown, the outcome is
    unknown, the id is unusable, or the composed text fails :func:`is_safe_text`. Every one of
    those is a programming error at the call site, and the right behaviour for a programming error
    in a *notification* layer is silence: the job's own in-app report is unaffected, and the
    operator is not told something untrue.

    The returned mapping is plain JSON-serialisable data, deliberately not a dataclass: it is
    handed straight to :func:`envelope` and from there to the browser, and a record that cannot
    hold anything but strings and integers is a record that cannot carry a surprise.
    """
    label = JOB_LABELS.get(job_kind) if isinstance(job_kind, str) else None
    verb = OUTCOME_VERBS.get(outcome) if isinstance(outcome, str) else None
    if label is None or verb is None or not is_valid_id(notification_id):
        return None

    title = f"{APP_NAME} — {label} {verb}"
    detail = detail_text(counts)
    body = f"{label} {verb}{DETAIL_SEPARATOR}{detail}" if detail else OUTCOME_BODIES[outcome]
    if not is_safe_text(title) or not is_safe_text(body):
        return None
    return {
        "id": notification_id,
        "job": job_kind,
        "outcome": outcome,
        "title": title,
        "body": body,
        "tag": TAG_PREFIX + notification_id,
    }


def envelope(notifications) -> str:
    """The value the hidden envelope component carries, as compact JSON.

    A LIST rather than a single record, because two genuinely different jobs of one session can
    finish while only one of them has a delivery event pending — a render and a Raw Cutter run are
    serialised by different mutexes. Dropping one silently would be the "no notification arrived"
    complaint this feature exists to fix.

    Returns :data:`ENVELOPE_EMPTY` for an empty list, so "nothing to deliver" is a value the
    dispatcher ignores rather than a well-formed payload with no items.
    """
    items = [record for record in (notifications or []) if isinstance(record, dict)]
    if not items:
        return ENVELOPE_EMPTY
    return json.dumps({"v": ENVELOPE_VERSION, "items": items},
                      ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_envelope(text):
    """The records in an envelope, or `[]`. Total — used by tests and by nothing in the app.

    The browser has its own copy of this logic in the dispatcher; this exists so the Python side
    can assert what the browser will see without a browser.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [record for record in items if isinstance(record, dict)]


# ---------------------------------------------------------------------------------------------
# Outcome translation helpers.
#
# Each one turns a subsystem's OWN typed terminal vocabulary into one of the four outcomes above.
# They live here, next to the four tokens, rather than in `gui.py`, so the mapping is executable
# on a bare interpreter and so no handler has to decide what "partial" means for itself. They take
# tokens and integers; none of them imports the subsystem it translates for, because a fork module
# may not depend on `render_worker`'s enum values being importable from here either -- the caller
# passes the token's VALUE.
# ---------------------------------------------------------------------------------------------

def outcome_from_counts(completed, failed, *, cancelled=False):
    """The four-way outcome for a job that counted things.

    Cancellation wins over everything, because a stopped job's counts describe how far it got and
    not what it achieved. After that it is the ordinary three-way split, and the partial case is
    "some of each" rather than "any failure at all": a batch where every single item failed is a
    FAILURE, and calling it partial would be the cheerful reading of a total loss.
    """
    if cancelled:
        return OUTCOME_CANCELLED
    done = _count(completed) or 0
    bad = _count(failed) or 0
    if bad and done:
        return OUTCOME_PARTIAL
    if bad:
        return OUTCOME_FAILED
    return OUTCOME_SUCCESS


__all__ = [
    "JOB_RENDER", "JOB_VARIANT_BATCH", "JOB_RAW_CUTTER", "JOB_RAW_CUTTER_BATCH",
    "JOB_AUDIO_CUTTER", "JOB_LIBRARY_ANALYSIS", "JOB_LIBRARY_SCAN",
    "JOB_LABELS", "JOB_KINDS",
    "OUTCOME_SUCCESS", "OUTCOME_PARTIAL", "OUTCOME_FAILED", "OUTCOME_CANCELLED",
    "OUTCOMES", "OUTCOME_VERBS", "OUTCOME_BODIES",
    "COUNT_FIELDS", "COUNT_WORDS", "APP_NAME", "DETAIL_SEPARATOR", "TAG_PREFIX",
    "ENABLED_ON", "ENABLED_OFF", "ENABLED_DEFAULT", "is_enabled", "option_value",
    "PERMISSION_NOT_REQUESTED", "PERMISSION_GRANTED", "PERMISSION_DENIED",
    "PERMISSION_UNSUPPORTED", "PERMISSION_STATES", "BROWSER_PERMISSION_STATES",
    "PERMISSION_SENTENCES", "permission_sentence",
    "APP_SETTING_SENTENCES", "app_setting_sentence",
    "TEST_TITLE", "TEST_BODY",
    "SEEN_STORAGE_KEY", "SEEN_LIMIT", "ENVELOPE_VERSION", "ENVELOPE_EMPTY",
    "FORBIDDEN_CHARACTERS", "is_safe_text",
    "ID_MIN_LENGTH", "ID_MAX_LENGTH", "is_valid_id",
    "detail_text", "build", "envelope", "decode_envelope", "outcome_from_counts",
]
