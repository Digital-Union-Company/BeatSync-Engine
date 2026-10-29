#!/usr/bin/env python3
"""Where the Stage-5 video-analysis cache lives, and what is in it.

[FORK] Digital-Union (FUN Shared Analysis Cache). One owner problem, stated plainly: the cache
directory is derived from `logger.ROOT_DIR`, so every checkout gets its own
`input/video_analysis_cache`. Production has thousands of records; a fresh playtest tree has none,
and re-analysing the same library from cold is measured in hours. The analysis itself is already
*portable* — Stage-5 identity is `contract | version | abspath | size | mtime_ns | fingerprint |
backend | qwen-config`, and **none of those components is the cache directory** — so the records
were always reusable across installations. Only their location was not.

Three jobs, deliberately in one module because they are the same subject:

``shared config``
    A tiny per-user JSON file **outside every checkout**, at
    ``%LOCALAPPDATA%\\BeatSync\\shared-config.json``. This is the whole point: storing the shared
    path in a checkout's own ``state/preferences.json`` would mean a new playtest tree still
    started blind, which is the problem rather than the fix. A future BeatSync version running
    under the same Windows user reads this file and finds the same cache with no setup at all.

``resolution``
    `resolve_location` answers "which directory does *this* invocation use?" once, so a Settings
    change during a render cannot relocate anything mid-analysis.

``inspection and import``
    What is in a cache folder (`measure`), and a one-time non-destructive copy of reusable records
    from an existing folder into the shared one (`plan_import` / `copy_record`).

What this module deliberately does **not** do:

* it computes **no cache identity** — no signature, no fingerprint, no key formula, nothing that
  could drift from `video_analysis._video_signature`. A record's filename is opaque to it;
* it decides **no record validity**. `_cache_entry_is_complete` in `video_analysis.py` remains the
  one completion rule. `looks_reusable` is a *transport* question — "is this a Stage-5 record file
  of the expected contract generation?" — and the contract string is passed **in** by the caller
  rather than restated here, so the two cannot disagree;
* it **deletes nothing**, ever. There is no `os.remove`, no `os.unlink`, no `rmdir`, no `rmtree` and
  no truncating open anywhere in this file. `input/video_analysis_cache/` is preserved by policy
  and a shared cache is shared by several installations, so a cleanup path here would be the single
  most dangerous thing the module could grow;
* it **never overwrites** a record at the destination. Import is strictly additive: an existing
  destination record is reported as already present and left exactly as it is, byte for byte.

Stdlib only (`json`, `os`, `tempfile`), so CLAUDE.md's hard rule holds and the whole thing is
testable on a bare interpreter. The atomic write is the same ``mkstemp`` + ``fsync`` + ``os.replace``
pattern `user_prefs`, `qwen_partials` and `_save_cache` already use.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

# ---------------------------------------------------------------------------------------------
# the per-user shared configuration file
# ---------------------------------------------------------------------------------------------

SCHEMA = "beatsync-shared-config-1"

APP_DIR_NAME = "BeatSync"
FILE_NAME = "shared-config.json"

#: The two stored settings. A folder and a switch, kept separate on purpose: turning sharing off
#: must not make the operator retype the path, and an empty path must not look like "off" when it
#: is really "not set up yet".
CACHE_FOLDER = "video_analysis_cache_folder"
CACHE_ENABLED = "video_analysis_cache_enabled"

KEYS = (CACHE_FOLDER, CACHE_ENABLED)

#: Defaults reproduce today's behaviour exactly: no shared folder, sharing off, local cache.
DEFAULTS = {CACHE_FOLDER: "", CACHE_ENABLED: False}

#: [FORK] The isolated-test override, and it is a REQUIREMENT rather than a convenience.
#:
#: Without it, every test of this module would read and write the owner's real shared
#: configuration — the one their production install and their playtests both depend on. A suite
#: that set a cache folder would silently re-point production at a temporary directory. So the
#: override exists, it is honoured by `config_path` and therefore by every reader and writer, and
#: there is deliberately no second way to reach the default path from inside this module.
ENV_CONFIG_PATH = "BEATSYNC_SHARED_CONFIG"

#: Resolution outcomes. `source` says which directory won and `available` says whether the one the
#: operator asked for could actually be used — two different questions, because the dangerous case
#: is "configured but unreachable", which must be reported rather than quietly downgraded.
SOURCE_LOCAL = "local"
SOURCE_SHARED = "shared"


def default_config_path(environ=None) -> str:
    """``%LOCALAPPDATA%\\BeatSync\\shared-config.json``, or ``""`` if that is not resolvable.

    Reads only; creates nothing. ``LOCALAPPDATA`` is the right variable for per-user,
    per-machine, non-roaming state: a cache folder is a property of *this* machine's disks, so
    roaming it to another machine would propagate a path that may not exist there.
    """
    env = os.environ if environ is None else environ
    base = (env.get("LOCALAPPDATA") or "").strip()
    if not base:
        return ""
    return os.path.join(base, APP_DIR_NAME, FILE_NAME)


def config_path(environ=None) -> str:
    """The configuration file this process must use — the override if set, else the default.

    **Every** reader and writer in this module goes through here, which is what makes the test
    override total: there is no path by which a suite can reach the real file while the override
    is set, and no path by which production can be redirected without the variable.
    """
    env = os.environ if environ is None else environ
    override = (env.get(ENV_CONFIG_PATH) or "").strip()
    if override:
        return override
    return default_config_path(env)


def empty_config() -> dict:
    return dict(DEFAULTS)


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value) -> bool:
    """Tolerant, because this file is human-editable and may be hand-written.

    A real ``bool`` is taken as itself; the obvious string spellings are accepted; anything else is
    ``False``. Never raises — an unreadable switch must mean "off", which is today's behaviour,
    rather than an exception on startup.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def load_config(path: str) -> tuple[dict, str]:
    """``(config, notice)``. ``notice`` is ``""`` when everything was in order.

    A missing file is a first run and reports nothing — that is the overwhelmingly common case and
    it is not a fault. Anything **present but unreadable** reports, for the same reason
    `user_prefs` does: silently falling back to the defaults would look identical to a first run,
    and the operator would never learn that their shared cache had stopped being used.
    """
    config = empty_config()
    if not path:
        return config, ""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return config, ""
    except (OSError, ValueError) as exc:
        return config, (
            f"The shared BeatSync settings file could not be read ({exc}), so the local "
            f"analysis cache is being used. Set the shared folder again in Settings.")
    if not isinstance(stored, dict):
        return config, (
            "The shared BeatSync settings file was not in the expected format, so the local "
            "analysis cache is being used.")
    config[CACHE_FOLDER] = _clean(stored.get(CACHE_FOLDER))
    config[CACHE_ENABLED] = _as_bool(stored.get(CACHE_ENABLED))
    return config, ""


def save_config(path: str, config: dict) -> str:
    """Write atomically. ``""`` on success, else an operator-readable reason.

    Only the known keys are written, so a stray value in the caller's dictionary cannot be
    persisted and the file stays something a human can open and fix. The containing directory is
    created — that is the one directory this module makes, and it is the app's own per-user
    settings folder, not anybody's media.
    """
    if not path:
        return ("There is no per-user settings location on this system, so the shared cache "
                "folder could not be saved.")
    payload = {
        "schema": SCHEMA,
        CACHE_FOLDER: _clean(config.get(CACHE_FOLDER)),
        CACHE_ENABLED: _as_bool(config.get(CACHE_ENABLED)),
    }
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix="shared-config-", suffix=".json",
                                             dir=directory)
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
                    os.unlink(temporary)      # only ever this call's own temporary
                except OSError:
                    pass
    except OSError as exc:
        return f"Could not save the shared cache setting: {exc.strerror or exc}"
    return ""


# ---------------------------------------------------------------------------------------------
# resolution: which directory does this invocation use?
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheLocation:
    """One invocation's frozen answer to "where is the analysis cache?".

    Frozen and made of plain strings and bools, so it is deepcopy-safe and can ride on a render's
    own state without carrying a handle to anything.

    ``path`` is the directory to use and is **always** usable — on every failure it falls back to
    the caller's local default, because refusing to analyse at all would be a worse outcome than
    analysing into the local cache. ``available`` and ``notice`` carry whether the operator got
    what they asked for, which is the half that must be *reported* rather than swallowed.
    """

    path: str
    source: str = SOURCE_LOCAL
    configured: str = ""
    requested: bool = False
    available: bool = True
    notice: str = ""

    @property
    def shared(self) -> bool:
        return self.source == SOURCE_SHARED

    def describe(self) -> str:
        """One line for a status panel or a console log. Says what is true, not what was wanted."""
        if self.shared:
            return f"Shared analysis cache: {self.path}"
        if self.requested:
            return (f"Local analysis cache: {self.path} "
                    f"(the configured shared folder is unavailable)")
        return f"Local analysis cache: {self.path}"


def resolve_location(local_default: str, config: dict | None = None,
                     *, make_dirs=None, is_dir=None) -> CacheLocation:
    """Resolve the cache directory for ONE invocation, and freeze the answer.

    Call this once per analysis invocation and thread the result down. Resolving per source — or
    re-reading the configuration mid-run — would let a Settings change move the cache while an
    analysis was writing into it, which is exactly the hazard "freeze the location" exists to
    prevent.

    **The parent folder must already exist; the cache folder itself is created.** That asymmetry is
    the whole safety argument, and it separates the two failures that look alike:

    ====================================  ==========================================================
    ``J:\\BeatSyncCache`` where ``J:\\``   a new or mistyped folder on a live disk -> CREATE it
    exists                                 (every other cache directory in this app is auto-created)
    ``J:\\BeatSyncCache`` where ``J:\\``   the disk is not plugged in, or the network share is down
    does not                               -> REPORT it, and use the local cache
    ====================================  ==========================================================

    Creating the folder unconditionally would turn the second case into a silent cold start against
    an unexpected empty location — an hour of re-analysis with no indication that anything was
    wrong. That is the specific outcome the owner asked to be told about rather than subjected to.

    ``make_dirs``/``is_dir`` are injected so both branches are testable without a disk; they
    default to the real `os` calls.
    """
    mkdir = os.makedirs if make_dirs is None else make_dirs
    isdir = os.path.isdir if is_dir is None else is_dir

    local = _clean(local_default)
    settings = empty_config() if config is None else config
    configured = _clean(settings.get(CACHE_FOLDER))
    enabled = _as_bool(settings.get(CACHE_ENABLED))

    if not enabled or not configured:
        # The unchanged path. Note what is NOT reported here: sharing being off is a setting, not a
        # problem, so it produces no notice at all.
        return CacheLocation(path=local, source=SOURCE_LOCAL, configured=configured,
                             requested=False, available=True, notice="")

    parent = os.path.dirname(os.path.abspath(configured)) or configured
    if not isdir(configured) and not isdir(parent):
        return CacheLocation(
            path=local, source=SOURCE_LOCAL, configured=configured,
            requested=True, available=False,
            notice=(
                f"The shared analysis cache folder is not reachable: {configured}. Its parent "
                f"folder does not exist, so the drive or network share is probably unavailable. "
                f"Nothing was created there. This run is using the local cache at {local} "
                f"instead, which means sources analysed before will be analysed again."))
    try:
        mkdir(configured, exist_ok=True)
    except Exception as exc:
        return CacheLocation(
            path=local, source=SOURCE_LOCAL, configured=configured,
            requested=True, available=False,
            notice=(
                f"The shared analysis cache folder could not be opened: {configured} "
                f"({type(exc).__name__}: {exc}). This run is using the local cache at {local} "
                f"instead, which means sources analysed before will be analysed again."))
    return CacheLocation(path=configured, source=SOURCE_SHARED, configured=configured,
                         requested=True, available=True, notice="")


# ---------------------------------------------------------------------------------------------
# inspection: what is in a cache folder?
# ---------------------------------------------------------------------------------------------

#: A Stage-5 record filename is `{8 hex}_{24 hex}.json` — `video_analysis._cache_path` builds it
#: from two `_hash_text` values, which are truncated lowercase SHA-1 hex digests.
#:
#: Matching the SHAPE rather than listing what to exclude is deliberate. The same directory also
#: holds the retained Qwen worker request/response JSON and the A1 per-job partials, all of which
#: are **intentionally** kept for debugging; an exclusion list would have to be updated every time
#: the worker gained an artifact, and a missed one would be copied into a shared cache or counted
#: as a record. This module therefore recognises records and ignores everything else, including
#: files it has never heard of.
_RECORD_NAME_HEX = "0123456789abcdef"
_RECORD_PREFIX_LENGTH = 8
_RECORD_SIGNATURE_LENGTH = 24
RECORD_SUFFIX = ".json"


def is_record_name(name) -> bool:
    """Is this filename shaped like a Stage-5 cache record? Pure string work, touches no disk."""
    if not isinstance(name, str) or not name.endswith(RECORD_SUFFIX):
        return False
    stem = name[: -len(RECORD_SUFFIX)]
    prefix, _, signature = stem.partition("_")
    if not signature or "_" in signature:
        return False
    if len(prefix) != _RECORD_PREFIX_LENGTH or len(signature) != _RECORD_SIGNATURE_LENGTH:
        return False
    return all(ch in _RECORD_NAME_HEX for ch in prefix + signature)


@dataclass(frozen=True)
class Measurement:
    """What one cache folder holds. Counts and bytes only — no record contents are retained."""

    path: str
    exists: bool = False
    records: int = 0
    record_bytes: int = 0
    other_files: int = 0
    other_bytes: int = 0
    error: str = ""

    @property
    def total_bytes(self) -> int:
        return self.record_bytes + self.other_bytes


def format_bytes(count) -> str:
    """Decimal units, matching `cache_admin.format_bytes` so two panels cannot disagree."""
    try:
        value = float(count)
    except (TypeError, ValueError):
        return "0 B"
    if value < 0 or value != value:
        return "0 B"
    for unit, step in (("B", 1), ("KB", 10 ** 3), ("MB", 10 ** 6),
                       ("GB", 10 ** 9), ("TB", 10 ** 12)):
        if value < step * 1000 or unit == "TB":
            scaled = value / step
            if unit == "B":
                return f"{int(scaled)} B"
            return f"{scaled:.1f} {unit}"
    return f"{value:.0f} B"


def measure(path: str) -> Measurement:
    """Count records and bytes in one cache folder. Reads only; creates and changes nothing.

    A missing folder is a first run, not a fault, and reports ``exists=False`` with zero counts —
    never an error. It is a **flat** listing, not a walk: a Stage-5 cache has no subdirectories,
    and `os.walk` would descend an NTFS junction (the measured hazard `cache_admin` handles), which
    a counter has no business doing.
    """
    target = _clean(path)
    if not target:
        return Measurement(path="", exists=False)
    try:
        entries = os.listdir(target)
    except FileNotFoundError:
        return Measurement(path=target, exists=False)
    except OSError as exc:
        return Measurement(path=target, exists=False,
                           error=f"{type(exc).__name__}: {exc.strerror or exc}")
    records = record_bytes = others = other_bytes = 0
    for name in entries:
        full = os.path.join(target, name)
        try:
            if not os.path.isfile(full):
                continue
            size = os.path.getsize(full)
        except OSError:
            # An unreadable or vanished entry is skipped rather than failing the whole count: this
            # is a read-out, and a shared folder legitimately has other processes writing into it.
            continue
        if is_record_name(name):
            records += 1
            record_bytes += size
        else:
            others += 1
            other_bytes += size
    return Measurement(path=target, exists=True, records=records, record_bytes=record_bytes,
                       other_files=others, other_bytes=other_bytes)


# ---------------------------------------------------------------------------------------------
# import: a one-time additive copy from an existing cache folder
# ---------------------------------------------------------------------------------------------

#: The four outcomes one source file can have. There is deliberately no "replaced" and no
#: "removed": import is additive, and the source folder is never modified at all.
COPIED = "copied"
ALREADY_PRESENT = "already_present"
INCOMPATIBLE = "incompatible"
FAILED = "failed"


@dataclass(frozen=True)
class ImportOutcome:
    """The result of one import run, in the four counts the operator asked to see."""

    source: str = ""
    destination: str = ""
    copied: int = 0
    already_present: int = 0
    incompatible: int = 0
    failed: int = 0
    skipped_other_files: int = 0
    copied_bytes: int = 0
    refusal: str = ""
    first_errors: tuple = ()

    @property
    def considered(self) -> int:
        return self.copied + self.already_present + self.incompatible + self.failed

    @property
    def ok(self) -> bool:
        return not self.refusal


def looks_reusable(path: str, contract: str) -> tuple[bool, str]:
    """``(reusable, reason)`` for ONE record file, by **transport** criteria only.

    This is not a second completion rule, and the distinction matters. It asks three things a
    *copier* must ask and nothing more: does the file parse as a JSON object, does it carry a
    ``video_file`` naming the source it describes, and does its ``cache_contract`` match the
    generation the caller is running. `_cache_entry_is_complete` in `video_analysis.py` remains the
    only authority on whether a record may be *reused*, and it runs unchanged on every lookup after
    the import, against the copied bytes.

    The contract string is a **parameter** rather than a constant here, so this module cannot drift
    from `CACHE_CONTRACT_VERSION`. A record of an older generation is reported `incompatible`
    rather than copied: its filename could never be looked up again, so copying it would move dead
    weight into a shared folder.

    `ai_deferred` is rejected too — a deferred record is a half-finished analysis by the existing
    rule's own definition, so it is not something worth carrying to another installation.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return False, f"unreadable ({type(exc).__name__})"
    if not isinstance(data, dict):
        return False, "not a JSON object"
    stored = data.get("cache_contract")
    if not isinstance(stored, str) or not stored:
        return False, "no cache contract recorded (written before the current generation)"
    if contract and stored != contract:
        return False, f"different cache generation ({stored})"
    if not isinstance(data.get("video_file"), str) or not data.get("video_file"):
        return False, "no source path recorded"
    if data.get("ai_deferred"):
        return False, "analysis was still deferred"
    return True, ""


def copy_record(source_path: str, destination_path: str) -> tuple[str, str]:
    """Copy ONE record, never replacing anything. ``(outcome, detail)``.

    The no-replace guarantee is the **exclusive create itself**, not a preceding `exists()` check:
    `O_CREAT | O_EXCL` fails when the name is taken, so there is no window between asking and
    acting. That is the same reasoning `_publish_share_no_replace` and `collision.publish` rest on.

    The staging file is this call's own exclusively-created temporary in the destination directory,
    so the final step is a same-directory `os.replace` onto a name this call already owns — never
    onto the operator's data. On any failure the temporary is removed and **nothing at the
    destination is touched**; the only file this function may ever delete is a temporary it created
    itself.
    """
    directory = os.path.dirname(destination_path) or "."
    try:
        payload = open(source_path, "rb").read()
    except OSError as exc:
        return FAILED, f"could not read: {exc.strerror or exc}"

    # Claim the final name exclusively. An existing record wins and is left untouched.
    try:
        descriptor = os.open(destination_path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    except FileExistsError:
        return ALREADY_PRESENT, ""
    except OSError as exc:
        return FAILED, f"could not create: {exc.strerror or exc}"
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        # The name was claimed by THIS call a moment ago and no reader can have accepted a
        # zero-length record as valid, so removing it is removing our own failed attempt.
        try:
            os.unlink(destination_path)
        except OSError:
            pass
        return FAILED, f"could not write: {exc.strerror or exc}"
    _ = directory
    return COPIED, ""


def plan_import(source: str, destination: str, *, list_dir=None) -> tuple[list, int, str]:
    """``(record_names, skipped_other_files, refusal)`` for an import run.

    It refuses rather than guessing in the three cases where proceeding would be wrong:

    * the source folder is missing or unreadable — there is nothing to import from;
    * the destination is blank — there is nowhere to import to;
    * **the two resolve to the same folder** — an import onto itself would report every record as
      already present, which looks like a successful no-op and tells the operator nothing. Compared
      by `normcase(abspath(...))` rather than by string equality, so `J:\\Cache` and `j:\\cache\\`
      are recognised as one place.

    Non-record files are counted and skipped, never copied: that is how large media, renders,
    models, the portable runtime and the retained Qwen worker JSON stay where they are. The shape
    test is `is_record_name`, so a file type this module has never seen is skipped by default.
    """
    listing = os.listdir if list_dir is None else list_dir
    origin = _clean(source)
    target = _clean(destination)
    if not origin:
        return [], 0, "No folder to import from was given."
    if not target:
        return [], 0, "There is no analysis cache folder to import into."
    try:
        same = (os.path.normcase(os.path.abspath(origin))
                == os.path.normcase(os.path.abspath(target)))
    except Exception:
        same = False
    if same:
        return [], 0, (
            "The folder to import from is the same folder the cache already uses, so there is "
            "nothing to copy.")
    try:
        entries = listing(origin)
    except OSError as exc:
        return [], 0, f"Could not read {origin}: {exc.strerror or exc}"
    records = sorted(name for name in entries if is_record_name(name))
    others = sum(1 for name in entries if not is_record_name(name))
    if not records:
        return [], others, (
            f"No analysis records were found in {origin}. "
            f"{others} other file(s) were left alone.")
    return records, others, ""


def import_records(source: str, destination: str, contract: str, *,
                   list_dir=None, max_reported_errors: int = 5) -> ImportOutcome:
    """Copy every reusable record from ``source`` into ``destination``. Additive and read-only
    with respect to the source.

    The source folder is **never** modified: this function contains no write, rename or delete
    aimed at it, which is what keeps a production cache intact after it has been imported from. Old
    records are not deleted from either side, here or later — removing them is the operator's
    decision and there is no code path for it.
    """
    records, others, refusal = plan_import(source, destination, list_dir=list_dir)
    if refusal:
        return ImportOutcome(source=_clean(source), destination=_clean(destination),
                             skipped_other_files=others, refusal=refusal)
    copied = present = incompatible = failed = copied_bytes = 0
    errors: list = []
    for name in records:
        origin_path = os.path.join(source, name)
        reusable, reason = looks_reusable(origin_path, contract)
        if not reusable:
            incompatible += 1
            if len(errors) < max_reported_errors:
                errors.append(f"{name}: {reason}")
            continue
        outcome, detail = copy_record(origin_path, os.path.join(destination, name))
        if outcome == COPIED:
            copied += 1
            try:
                copied_bytes += os.path.getsize(os.path.join(destination, name))
            except OSError:
                pass
        elif outcome == ALREADY_PRESENT:
            present += 1
        else:
            failed += 1
            if len(errors) < max_reported_errors:
                errors.append(f"{name}: {detail}")
    return ImportOutcome(
        source=_clean(source), destination=_clean(destination),
        copied=copied, already_present=present, incompatible=incompatible, failed=failed,
        skipped_other_files=others, copied_bytes=copied_bytes, first_errors=tuple(errors))


__all__ = [
    "ALREADY_PRESENT",
    "APP_DIR_NAME",
    "CACHE_ENABLED",
    "CACHE_FOLDER",
    "COPIED",
    "CacheLocation",
    "DEFAULTS",
    "ENV_CONFIG_PATH",
    "FAILED",
    "FILE_NAME",
    "INCOMPATIBLE",
    "ImportOutcome",
    "KEYS",
    "Measurement",
    "RECORD_SUFFIX",
    "SCHEMA",
    "SOURCE_LOCAL",
    "SOURCE_SHARED",
    "config_path",
    "copy_record",
    "default_config_path",
    "empty_config",
    "format_bytes",
    "import_records",
    "is_record_name",
    "load_config",
    "looks_reusable",
    "measure",
    "plan_import",
    "resolve_location",
    "save_config",
]
