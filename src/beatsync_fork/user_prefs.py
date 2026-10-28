"""Remembered folder choices, kept in runtime state rather than in source.

The operator picks the same Media Library and Discarded folder every session, so the app should
remember them. Three rules shape this file:

* **Nothing personal is ever written to source.** The store lives under `state/`, which is
  gitignored exactly like the decision journal and the poster cache, so no operator's directory
  layout can end up in a commit.
* **A remembered path is a suggestion, not an action.** Loading preferences never creates a
  directory and never scans one. A Media Library that has gone away is reported, not recreated --
  recreating it would silently hand the operator an empty folder where their library used to be.
* **An unreadable store is not an empty one.** A corrupt or partially written file yields the
  built-in defaults *and* says so, so a lost preference is visible instead of looking like a
  first run.

Stdlib only (`json`, `os`, `tempfile`), so the fork package's no-runtime-import rule still holds.
The write is `mkstemp` + `os.replace` in the same directory -- the same durable-publish pattern
`ReviewStore`'s snapshot and `qwen_partials` already use, so a crash mid-save leaves the previous
preferences intact rather than a truncated file.
"""

from __future__ import annotations

import json
import os
import tempfile

#: The remembered fields. Every one may legitimately be empty, and all but one are folder paths —
#: `AUDIO_CUTTER_INPUT` holds the last source *file*, because the Raw Audio Cutter's input is one
#: recording rather than a directory. It is used only to reopen the Browse dialog where the
#: operator last was, so a stale or deleted path costs nothing.
CLIPS_LIBRARY = "clips_library"
CLIPS_REJECTED = "clips_rejected"
CLIPS_INPUT = "clips_input"
RAW_CUTTER_OUTPUT = "raw_cutter_output"
RAW_CUTTER_INPUT = "raw_cutter_input"
QUICK_RENDER_SOURCE = "quick_render_source"
# [FORK] R7 Raw Audio Cutter. APPENDED, never reordered: `load_preferences` fills a missing key
# with "", so a `preferences.json` written before R7 still loads and simply has these blank.
AUDIO_LIBRARY = "audio_library"
AUDIO_DISCARDED = "audio_discarded"
AUDIO_CUTTER_OUTPUT = "audio_cutter_output"
AUDIO_CUTTER_INPUT = "audio_cutter_input"
# [FORK] R7.2 Audio Preview. APPENDED for the same reason, and with its own keys rather than
# reusing the cutter's: the folder an operator reviews and the folder the cutter writes into are
# different questions, and a review session months later should not overwrite the cutter's output
# default just because the operator browsed somewhere else. The two destinations are separate for
# the same reason -- an operator may well sort reviewed clips somewhere other than where a fresh
# cut files them.
AUDIO_PREVIEW_INPUT = "audio_preview_input"
AUDIO_PREVIEW_LIBRARY = "audio_preview_library"
AUDIO_PREVIEW_DISCARDED = "audio_preview_discarded"

#: Destinations the operator deliberately configures, versus the merely-convenient last-used
#: inputs. Both are remembered; only the first group is presented as "defaults" in the UI.
DESTINATION_KEYS = (CLIPS_LIBRARY, CLIPS_REJECTED, RAW_CUTTER_OUTPUT,
                    AUDIO_LIBRARY, AUDIO_DISCARDED, AUDIO_CUTTER_OUTPUT,
                    AUDIO_PREVIEW_LIBRARY, AUDIO_PREVIEW_DISCARDED)
LAST_USED_KEYS = (CLIPS_INPUT, RAW_CUTTER_INPUT, QUICK_RENDER_SOURCE, AUDIO_CUTTER_INPUT,
                  AUDIO_PREVIEW_INPUT)
KEYS = DESTINATION_KEYS + LAST_USED_KEYS

SCHEMA = "beatsync-folder-preferences-1"


def empty_preferences() -> dict:
    """The built-in default: every field blank. Deliberately not the author's own folders."""
    return {key: "" for key in KEYS}


def preferences_path(state_dir: str) -> str:
    return os.path.join(state_dir, "preferences.json")


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def load_preferences(path: str) -> tuple[dict, str]:
    """`(preferences, notice)`. `notice` is `""` when everything was in order.

    A missing file is a first run, not a fault, and reports nothing. Anything present but
    unreadable reports, because silently starting from blank would look identical to a first run
    and the operator would never learn their settings had been lost.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return empty_preferences(), ""
    except (OSError, ValueError):
        return empty_preferences(), (
            "Saved folder defaults could not be read, so none were applied. "
            "Set them again and press Save defaults.")
    if not isinstance(stored, dict):
        return empty_preferences(), (
            "Saved folder defaults were not in the expected format, so none were applied.")
    values = stored.get("folders")
    if not isinstance(values, dict):
        values = stored
    preferences = empty_preferences()
    for key in KEYS:
        preferences[key] = _clean(values.get(key))
    return preferences, ""


def save_preferences(path: str, preferences: dict) -> str:
    """Write atomically. Returns `""` on success or an operator-readable reason.

    Only the known keys are stored, so a stray value in the caller's dictionary cannot be
    persisted, and the file stays something a human can read and edit.
    """
    payload = {"schema": SCHEMA,
               "folders": {key: _clean(preferences.get(key)) for key in KEYS}}
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="prefs-", suffix=".json", dir=directory)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = ""
        finally:
            if temporary:
                try:
                    os.unlink(temporary)       # only ever this call's own temporary
                except OSError:
                    pass
    except OSError as exc:
        return f"Could not save folder defaults: {exc.strerror or exc}"
    return ""


def missing_folders(preferences: dict, keys=DESTINATION_KEYS) -> list[str]:
    """Which remembered folders are no longer there. Reads only; creates nothing."""
    absent = []
    for key in keys:
        value = _clean(preferences.get(key))
        if value and not os.path.isdir(value):
            absent.append(key)
    return absent


def unavailable_notice(preferences: dict, labels: dict) -> str:
    """One sentence naming the remembered folders that have gone away, or `""`.

    The folder is **not** recreated: an operator whose Media Library moved needs to be told, not
    handed a new empty directory with the old name.
    """
    absent = missing_folders(preferences)
    if not absent:
        return ""
    named = "; ".join(f"{labels.get(key, key)}: {preferences.get(key, '')}" for key in absent)
    return ("Saved folder(s) not found, so they were left as-is rather than created — "
            f"{named}. Pick them again or edit the path.")


__all__ = ["CLIPS_LIBRARY", "CLIPS_REJECTED", "CLIPS_INPUT", "RAW_CUTTER_OUTPUT",
           "RAW_CUTTER_INPUT", "QUICK_RENDER_SOURCE",
           "AUDIO_LIBRARY", "AUDIO_DISCARDED", "AUDIO_CUTTER_OUTPUT", "AUDIO_CUTTER_INPUT",
           "AUDIO_PREVIEW_INPUT", "AUDIO_PREVIEW_LIBRARY", "AUDIO_PREVIEW_DISCARDED",
           "DESTINATION_KEYS", "LAST_USED_KEYS",
           "KEYS", "SCHEMA", "empty_preferences", "preferences_path", "load_preferences",
           "save_preferences", "missing_folders", "unavailable_notice"]
