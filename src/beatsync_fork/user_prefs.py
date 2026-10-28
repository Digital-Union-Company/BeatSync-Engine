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
# [FORK] R8 UX Refresh. APPENDED for the third time and for the same reason: `load_preferences`
# fills a missing key with "", so every `preferences.json` written before R8 still loads and simply
# has this blank. Where finished renders go was the one destination the operator could not set --
# it was a fixed property of the installation -- so it is a *new* key rather than a reuse of one.
RENDER_OUTPUT = "render_output"

#: Destinations the operator deliberately configures, versus the merely-convenient last-used
#: inputs. Both are remembered; only the first group is presented as "defaults" in the UI.
DESTINATION_KEYS = (CLIPS_LIBRARY, CLIPS_REJECTED, RAW_CUTTER_OUTPUT,
                    AUDIO_LIBRARY, AUDIO_DISCARDED, AUDIO_CUTTER_OUTPUT,
                    AUDIO_PREVIEW_LIBRARY, AUDIO_PREVIEW_DISCARDED, RENDER_OUTPUT)
LAST_USED_KEYS = (CLIPS_INPUT, RAW_CUTTER_INPUT, QUICK_RENDER_SOURCE, AUDIO_CUTTER_INPUT,
                  AUDIO_PREVIEW_INPUT)
KEYS = DESTINATION_KEYS + LAST_USED_KEYS

SCHEMA = "beatsync-folder-preferences-1"

# ---------------------------------------------------------------------------------------------
# [FORK] R8 UX Refresh: non-folder options, in the same file and deliberately in a SEPARATE
# section.
#
# The schema string is unchanged, and that is a decision rather than an oversight. `folders` keeps
# exactly the shape and meaning it already had, so a `preferences.json` written by any previous
# version loads here with no migration step, and one written by this version loads in an older
# build with the options silently ignored -- which is the correct outcome, because an older build
# has no feature to apply them to. Bumping the schema would have bought nothing and would have
# meant teaching every reader a version rule.
#
# The practical consequence is a rule for writers: `save_preferences` must PRESERVE a section it
# does not own. Writing only `folders` would silently discard the options, and the operator would
# watch a setting reset itself every time they pressed Save defaults.
# ---------------------------------------------------------------------------------------------

#: What to do when a destination filename is already taken. The values are
#: `beatsync_fork.collision.SKIP` / `.SUFFIX`; this module deliberately does not import that one,
#: because preferences are inert storage and must not acquire a dependency on the policy that
#: happens to read them. `collision.resolve_policy` is total, so an unknown value read from here
#: resolves to the safe default rather than raising.
COLLISION_POLICY = "collision_policy"

OPTION_KEYS = (COLLISION_POLICY,)

#: The built-in defaults. Every one must reproduce the behaviour the app had before the option
#: existed, so that an operator who never opens Settings sees no change at all.
OPTION_DEFAULTS = {COLLISION_POLICY: "skip"}


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


def empty_options() -> dict:
    return dict(OPTION_DEFAULTS)


def load_options(path: str) -> dict:
    """The non-folder options, with every missing or unreadable value defaulted.

    Deliberately total and deliberately silent: unlike the folder store there is nothing here an
    operator loses by a default being applied, so an unreadable file reports through
    :func:`load_preferences` (which the caller always invokes too) rather than twice.
    """
    options = empty_options()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except (OSError, ValueError):
        return options
    if not isinstance(stored, dict):
        return options
    values = stored.get("options")
    if not isinstance(values, dict):
        return options
    for key in OPTION_KEYS:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            options[key] = value.strip()
    return options


def _existing_options(path: str) -> dict:
    """What is already on disk, so a writer cannot drop a section it does not own."""
    return load_options(path)


def save_preferences(path: str, preferences: dict, options: dict | None = None) -> str:
    """Write atomically. Returns `""` on success or an operator-readable reason.

    Only the known keys are stored, so a stray value in the caller's dictionary cannot be
    persisted, and the file stays something a human can read and edit.

    `options=None` means "leave the options exactly as they are", which is what every pre-R8
    caller wants and gets without being changed. The options are read back from disk and written
    out again rather than omitted, because omitting them would delete them -- this function
    replaces the whole file.
    """
    resolved = _existing_options(path) if options is None else {
        key: _clean(options.get(key)) or OPTION_DEFAULTS[key] for key in OPTION_KEYS}
    payload = {"schema": SCHEMA,
               "folders": {key: _clean(preferences.get(key)) for key in KEYS},
               "options": resolved}
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


# ---------------------------------------------------------------------------------------------
# [FORK] R8 Owner Fixes (P1): work folders that are also finished libraries.
#
# The owner's screenshot had the Audio Cutter's OUTPUT folder and the Audio Preview MEDIA LIBRARY
# both pointing at `Raw_Cutts`. Nothing is broken by that -- every move still validates, nothing
# is overwritten -- but it quietly defeats the point of reviewing: fresh unreviewed cuts land in
# the same directory that "keep" files into, so the two become indistinguishable on disk and the
# next scan re-offers clips that were already accepted.
#
# This reports; it never relocates, never rewrites a preference and never refuses a save. The
# operator may well mean it.
# ---------------------------------------------------------------------------------------------

#: Keys that receive freshly produced, not-yet-reviewed material.
WORK_OUTPUT_KEYS = (RAW_CUTTER_OUTPUT, AUDIO_CUTTER_OUTPUT)

#: Keys that receive material an operator has decided to KEEP.
FINISHED_LIBRARY_KEYS = (CLIPS_LIBRARY, AUDIO_LIBRARY, AUDIO_PREVIEW_LIBRARY)


def _comparable(path) -> str:
    """A path in a form two settings can be compared by, or `""` when there is nothing to compare.

    `normcase` so `J:\\Raw_Cutts` and `j:/raw_cutts` are recognised as the same place on Windows,
    and a trailing separator is dropped for the same reason. This is deliberately NOT `realpath`:
    a read-out must not touch the filesystem, and a folder that does not exist yet should still
    be comparable with one that does.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def overlapping_destinations(preferences: dict, labels: dict) -> str:
    """One sentence naming each work folder that is also configured as a finished library, or `""`.

    Pairs are reported in a fixed order so the same configuration always produces the same
    sentence -- an operator comparing two screens must not see a difference that is not there.
    """
    pairs = []
    for work_key in WORK_OUTPUT_KEYS:
        work = _comparable(preferences.get(work_key))
        if not work:
            continue
        for library_key in FINISHED_LIBRARY_KEYS:
            if _comparable(preferences.get(library_key)) == work:
                pairs.append((work_key, library_key))
    if not pairs:
        return ""
    named = "; ".join(
        f"{labels.get(work, work)} and {labels.get(library, library)} "
        f"are both {preferences.get(work, '')}"
        for work, library in pairs)
    return ("Heads up — a work folder is also set as a finished library: "
            f"{named}. Reviewed and unreviewed clips will be mixed together in it. "
            "Nothing was moved or changed; set different folders if that was not intended.")


__all__ = ["CLIPS_LIBRARY", "CLIPS_REJECTED", "CLIPS_INPUT", "RAW_CUTTER_OUTPUT",
           "RAW_CUTTER_INPUT", "QUICK_RENDER_SOURCE",
           "AUDIO_LIBRARY", "AUDIO_DISCARDED", "AUDIO_CUTTER_OUTPUT", "AUDIO_CUTTER_INPUT",
           "AUDIO_PREVIEW_INPUT", "AUDIO_PREVIEW_LIBRARY", "AUDIO_PREVIEW_DISCARDED",
           "RENDER_OUTPUT",
           "DESTINATION_KEYS", "LAST_USED_KEYS",
           "KEYS", "SCHEMA", "empty_preferences", "preferences_path", "load_preferences",
           "save_preferences", "missing_folders", "unavailable_notice",
           "WORK_OUTPUT_KEYS", "FINISHED_LIBRARY_KEYS", "overlapping_destinations",
           "COLLISION_POLICY", "OPTION_KEYS", "OPTION_DEFAULTS", "empty_options",
           "load_options"]
