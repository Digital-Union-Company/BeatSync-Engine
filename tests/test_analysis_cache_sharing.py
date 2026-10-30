"""FUN Shared Analysis Cache: the per-user config, the resolution, and the additive import.

`beatsync_fork/analysis_cache.py` is stdlib-only, so this suite imports it normally and tests it as
ordinary code. The Stage-5 side — that the resolved directory is threaded in and that it reaches no
cache signature — is asserted against the real `video_analysis.py` source, which cannot be imported
on a bare interpreter.

The session-wide `isolated_shared_config` fixture in `conftest.py` has already pointed
``BEATSYNC_SHARED_CONFIG`` at a throwaway path, so nothing here can read or write the operator's
real configuration. Every test that saves also pins its own path on top of that, so two tests
cannot see each other's writes.

Sections:

1.  The configuration file: round trip, tolerance, and the test override.
2.  Resolution: the four outcomes, and why an unreachable folder is reported rather than created.
3.  Record recognition: records in, worker artifacts out.
4.  Measurement.
5.  Import: additive, non-destructive, and never overwriting.
6.  The Stage-5 seam: one resolver, threaded, and absent from identity.
"""

from __future__ import annotations

import ast
import json
import os

import pytest

from beatsync_fork import analysis_cache as ac

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VA = os.path.join(_REPO_ROOT, "src", "video_analysis.py")
_GUI = os.path.join(_REPO_ROOT, "src", "gui.py")


def _tree(path: str) -> ast.Module:
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename=path)


def _func(path: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {path}")


def _body(path: str, name: str) -> str:
    """Executable body only — docstrings stripped, so prose cannot satisfy an assertion."""
    node = _func(path, name)
    return "\n".join(
        ast.unparse(item) for item in node.body
        if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)))


def _executable_source(path: str) -> str:
    """A whole module with every docstring stripped, recursively, and comments dropped.

    Load-bearing for the absence assertions below, and for a reason this repository has been
    bitten by repeatedly: `analysis_cache.py`'s own docstring *names* the things it must never do
    ("no `rmtree`", "cannot drift from `_video_signature`"), because that is the clearest way to
    record why. A substring scan over the raw characters cannot tell an explanation from a
    violation, and the only way to satisfy such a test is to delete the explanation — which makes
    the code worse to protect a test. Scanning the unparsed AST keeps both.
    """
    tree = _tree(path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A per-test configuration path, layered on the session-wide isolation."""
    path = str(tmp_path / "cfg" / "shared-config.json")
    monkeypatch.setenv(ac.ENV_CONFIG_PATH, path)
    return path


def _record_name(prefix="0026476a", signature="1e5b6af7eb7140a65ae4d623"):
    return f"{prefix}_{signature}.json"


def _write_record(directory, name, *, contract="stage5_cache_v3", video="C:/media/a.mp4",
                  **extra):
    os.makedirs(directory, exist_ok=True)
    payload = {"cache_contract": contract, "video_file": video,
               "analysis_version": "v8", "candidates": [], "ai_enabled": False}
    payload.update(extra)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


# ===========================================================================
# 1. the configuration file
# ===========================================================================


def test_a_first_run_is_not_a_fault(config_file):
    """A missing file is the overwhelmingly common case and must report nothing at all.

    The distinction matters because the *unreadable* case does report: silently falling back to the
    defaults there would look identical to a first run, and the operator would never learn that
    their shared cache had stopped being used.
    """
    config, notice = ac.load_config(config_file)
    assert config == ac.DEFAULTS
    assert notice == ""


def test_a_round_trip_preserves_both_settings(config_file):
    assert ac.save_config(config_file, {ac.CACHE_FOLDER: r"J:\Shared",
                                        ac.CACHE_ENABLED: True}) == ""
    config, notice = ac.load_config(config_file)
    assert config[ac.CACHE_FOLDER] == r"J:\Shared"
    assert config[ac.CACHE_ENABLED] is True
    assert notice == ""


def test_a_the_folder_and_the_switch_are_independent(config_file):
    """Turning sharing off must not make the operator retype the path.

    They are separate keys for exactly this reason: an empty path must not look like "off" when it
    is really "not set up yet", and "off" must not erase a path that took a Browse dialog to pick.
    """
    ac.save_config(config_file, {ac.CACHE_FOLDER: r"J:\Shared", ac.CACHE_ENABLED: False})
    config, _ = ac.load_config(config_file)
    assert config[ac.CACHE_FOLDER] == r"J:\Shared"
    assert config[ac.CACHE_ENABLED] is False


def test_a_an_unreadable_file_reports_and_defaults(config_file):
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    config, notice = ac.load_config(config_file)
    assert config == ac.DEFAULTS
    assert notice, "an unreadable file must be reported, not silently defaulted"
    assert "local" in notice.lower()


def test_a_a_non_object_payload_reports_and_defaults(config_file):
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as handle:
        json.dump(["not", "an", "object"], handle)
    config, notice = ac.load_config(config_file)
    assert config == ac.DEFAULTS
    assert notice


@pytest.mark.parametrize("stored,expected", [
    (True, True), (False, False),
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("no", False), ("", False), ("nonsense", False),
    (1, True), (0, False), (None, False), ([], False), ({}, False),
])
def test_a_the_switch_is_read_tolerantly(config_file, stored, expected):
    """The file is human-editable, so a hand-written switch must not raise on startup.

    Anything unrecognised means **off**, which is the pre-feature behaviour: the failure mode of a
    misread switch should be "my shared cache was not used", never "BeatSync would not start".
    """
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as handle:
        json.dump({ac.CACHE_FOLDER: "x", ac.CACHE_ENABLED: stored}, handle)
    config, _ = ac.load_config(config_file)
    assert config[ac.CACHE_ENABLED] is expected


def test_a_only_known_keys_are_persisted(config_file):
    """A stray value in the caller's dictionary must not reach the file."""
    ac.save_config(config_file, {ac.CACHE_FOLDER: "x", ac.CACHE_ENABLED: True,
                                 "secret_token": "leak", "video_files": ["/a.mp4"]})
    with open(config_file, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert set(stored) == {"schema", ac.CACHE_FOLDER, ac.CACHE_ENABLED}
    assert "leak" not in json.dumps(stored)


def test_a_the_write_is_atomic_and_leaves_no_temporary(config_file):
    ac.save_config(config_file, {ac.CACHE_FOLDER: "x", ac.CACHE_ENABLED: True})
    leftovers = [name for name in os.listdir(os.path.dirname(config_file))
                 if name != os.path.basename(config_file)]
    assert leftovers == [], f"a temporary survived: {leftovers}"


def test_a_the_override_is_the_only_way_in(monkeypatch, tmp_path):
    """**The test-isolation guarantee, asserted rather than assumed.**

    Every reader and writer goes through `config_path`, so while the override is set there is no
    path by which a suite can reach the operator's real file — and with it unset, no path by which
    production can be redirected.
    """
    monkeypatch.setenv(ac.ENV_CONFIG_PATH, str(tmp_path / "pinned.json"))
    assert ac.config_path() == str(tmp_path / "pinned.json")

    monkeypatch.delenv(ac.ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert ac.config_path() == os.path.join(
        str(tmp_path / "AppData"), ac.APP_DIR_NAME, ac.FILE_NAME)


def test_a_the_default_path_lives_outside_every_checkout(monkeypatch, tmp_path):
    """The whole point of the feature: a path a NEW playtest tree can find without being told.

    `LOCALAPPDATA` rather than a roaming location, because a cache folder is a property of this
    machine's disks — roaming it would propagate a path that may not exist elsewhere.
    """
    monkeypatch.delenv(ac.ENV_CONFIG_PATH, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    path = ac.default_config_path()
    assert ac.APP_DIR_NAME in path and path.endswith(ac.FILE_NAME)
    assert "input" not in path and "state" not in path, (
        "the shared setting must not live inside a checkout's own directories")


def test_a_no_per_user_location_is_reported_not_crashed(monkeypatch):
    monkeypatch.delenv(ac.ENV_CONFIG_PATH, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert ac.default_config_path() == ""
    assert ac.save_config("", {ac.CACHE_FOLDER: "x", ac.CACHE_ENABLED: True}) != ""
    assert ac.load_config("") == (ac.empty_config(), "")


# ===========================================================================
# 2. resolution
# ===========================================================================


def test_b_no_configuration_means_the_local_cache_and_no_notice(tmp_path):
    """The unchanged path. Sharing being off is a setting, not a problem, so nothing is reported."""
    local = str(tmp_path / "local")
    location = ac.resolve_location(local, ac.empty_config())
    assert location.path == local
    assert location.source == ac.SOURCE_LOCAL
    assert location.shared is False
    assert location.requested is False
    assert location.available is True
    assert location.notice == ""


def test_b_a_folder_without_the_switch_is_still_local(tmp_path):
    shared = str(tmp_path / "shared")
    os.makedirs(shared)
    location = ac.resolve_location(str(tmp_path / "local"),
                                   {ac.CACHE_FOLDER: shared, ac.CACHE_ENABLED: False})
    assert location.source == ac.SOURCE_LOCAL
    assert location.notice == ""


def test_b_an_enabled_folder_on_a_live_disk_is_created_and_used(tmp_path):
    """A new or mistyped folder whose PARENT exists is created — like every other cache here."""
    shared = str(tmp_path / "live" / "BeatSyncCache")
    os.makedirs(os.path.dirname(shared))
    assert not os.path.isdir(shared)
    location = ac.resolve_location(str(tmp_path / "local"),
                                   {ac.CACHE_FOLDER: shared, ac.CACHE_ENABLED: True})
    assert location.path == shared
    assert location.shared is True
    assert location.available is True
    assert location.notice == ""
    assert os.path.isdir(shared), "a folder on a live disk is created"


def test_b_an_unreachable_folder_is_reported_and_nothing_is_created(tmp_path):
    """**The failure the owner asked to be TOLD about rather than subjected to.**

    An offline drive or a downed share means the parent does not exist. Creating the folder anyway
    would turn that into a silent cold start against an unexpected empty location — an hour of
    re-analysis with nothing on screen to explain it.

    So: local cache, `available=False`, a notice naming the configured path, and **no directory
    created anywhere**.
    """
    local = str(tmp_path / "local")
    missing = str(tmp_path / "absent-drive" / "deeper" / "Cache")
    location = ac.resolve_location(local, {ac.CACHE_FOLDER: missing, ac.CACHE_ENABLED: True})
    assert location.path == local
    assert location.source == ac.SOURCE_LOCAL
    assert location.requested is True, "the operator DID ask for a shared cache"
    assert location.available is False
    assert missing in location.notice
    assert "local" in location.notice.lower()
    assert "analysed again" in location.notice, (
        "the notice must say what the fallback actually costs")
    assert not os.path.exists(os.path.dirname(missing)), "nothing may be created"


def test_b_the_parent_rule_is_what_separates_the_two_cases(tmp_path):
    """Stated as one assertion, because the asymmetry IS the safety argument.

    Same shape of input, same switch, opposite outcome — decided only by whether the parent folder
    exists. A mistyped leaf on a live disk is recoverable; a missing ancestor means the disk is not
    there.
    """
    created = []
    assert ac.resolve_location(
        "L", {ac.CACHE_FOLDER: r"X:\live\Cache", ac.CACHE_ENABLED: True},
        make_dirs=lambda p, exist_ok=False: created.append(p),
        is_dir=lambda p: p == r"X:\live").shared is True
    assert created == [r"X:\live\Cache"]

    created.clear()
    assert ac.resolve_location(
        "L", {ac.CACHE_FOLDER: r"X:\gone\Cache", ac.CACHE_ENABLED: True},
        make_dirs=lambda p, exist_ok=False: created.append(p),
        is_dir=lambda p: False).shared is False
    assert created == [], "an unreachable folder must not be created"


def test_b_a_folder_that_cannot_be_opened_is_reported(tmp_path):
    """A permission failure is reported with its reason, not swallowed into a bare fallback."""
    def explode(path, exist_ok=False):
        raise PermissionError(13, "Access is denied")

    location = ac.resolve_location("L", {ac.CACHE_FOLDER: r"X:\live\Cache",
                                         ac.CACHE_ENABLED: True},
                                   make_dirs=explode, is_dir=lambda p: p == r"X:\live")
    assert location.source == ac.SOURCE_LOCAL
    assert location.available is False
    assert "PermissionError" in location.notice


def test_b_an_existing_folder_is_used_without_being_recreated(tmp_path):
    shared = str(tmp_path / "shared")
    os.makedirs(shared)
    _write_record(shared, _record_name())
    location = ac.resolve_location(str(tmp_path / "local"),
                                   {ac.CACHE_FOLDER: shared, ac.CACHE_ENABLED: True})
    assert location.shared is True
    assert os.path.isfile(os.path.join(shared, _record_name())), "existing records survive"


def test_b_the_description_says_what_is_true_not_what_was_wanted(tmp_path):
    local = str(tmp_path / "local")
    refused = ac.resolve_location(local, {ac.CACHE_FOLDER: str(tmp_path / "gone" / "x"),
                                          ac.CACHE_ENABLED: True})
    assert "Local" in refused.describe()
    assert "unavailable" in refused.describe()

    shared = str(tmp_path / "shared")
    os.makedirs(shared)
    ok = ac.resolve_location(local, {ac.CACHE_FOLDER: shared, ac.CACHE_ENABLED: True})
    assert ok.describe() == f"Shared analysis cache: {shared}"


def test_b_the_location_record_is_frozen_and_plain(tmp_path):
    """It rides on an invocation's own state, so it must carry no handle to anything."""
    location = ac.resolve_location(str(tmp_path / "local"), ac.empty_config())
    with pytest.raises(Exception):
        location.path = "elsewhere"
    for value in vars(location).values():
        assert isinstance(value, (str, bool)), f"{value!r} is not a plain value"


# ===========================================================================
# 3. record recognition
# ===========================================================================


@pytest.mark.parametrize("name", [
    "0026476a_1e5b6af7eb7140a65ae4d623.json",
    "ffffffff_000000000000000000000000.json",
])
def test_c_a_record_filename_is_recognised(name):
    assert ac.is_record_name(name) is True


@pytest.mark.parametrize("name", [
    # the retained worker artifacts, which are deliberately kept for debugging and must never be
    # counted as records or copied into a shared folder
    "qwen_batch_request_abc123def456.json",
    "qwen_batch_response_abc123def456.json",
    "qwen_request_abc123def456.json",
    "qwen_response_abc123def456.json",
    "qwen_batch_response_abc_job_00001_1.partial.json",
    # near misses
    "0026476A_1e5b6af7eb7140a65ae4d623.json",     # uppercase is not a sha1 hexdigest
    "0026476g_1e5b6af7eb7140a65ae4d623.json",     # not hex
    "0026476_1e5b6af7eb7140a65ae4d623.json",      # prefix too short
    "0026476a_1e5b6af7eb7140a65ae4d62.json",      # signature too short
    "0026476a_1e5b6af7eb7140a65ae4d623.txt",      # not json
    "0026476a_1e5b_6af7eb7140a65ae4d6.json",      # two separators
    "0026476a1e5b6af7eb7140a65ae4d623.json",      # no separator
    # not a filename at all
    "", None, 42, [],
])
def test_c_everything_else_is_not_a_record(name):
    """**Shape in, not exclusions out**, and this is the test that buys it.

    An exclusion list would need updating every time the Qwen worker gained an artifact, and a
    missed one would be copied into a shared folder or counted as an analysed video. Recognising
    the record shape means a file type this module has never heard of is skipped by default.
    """
    assert ac.is_record_name(name) is False


def test_c_recognition_touches_no_disk():
    """It is pure string work, so it can be applied to a listing without a stat per entry."""
    body = "\n".join(
        ast.unparse(item) for item in
        _func(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "analysis_cache.py"),
              "is_record_name").body
        if not (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)))
    for forbidden in ("open(", "os.stat", "os.path.isfile", "os.listdir", "getsize"):
        assert forbidden not in body


# ===========================================================================
# 4. measurement
# ===========================================================================


def test_d_a_missing_folder_is_a_first_run_not_an_error(tmp_path):
    measurement = ac.measure(str(tmp_path / "never-created"))
    assert measurement.exists is False
    assert measurement.records == 0
    assert measurement.error == ""


def test_d_records_and_other_files_are_counted_separately(tmp_path):
    folder = str(tmp_path / "cache")
    _write_record(folder, "0026476a_1e5b6af7eb7140a65ae4d623.json")
    _write_record(folder, "11111111_222222222222222222222222.json")
    with open(os.path.join(folder, "qwen_batch_request_abc.json"), "w") as handle:
        handle.write("x" * 100)
    os.makedirs(os.path.join(folder, "a-subdirectory"))

    measurement = ac.measure(folder)
    assert measurement.exists is True
    assert measurement.records == 2
    assert measurement.other_files == 1
    assert measurement.other_bytes == 100
    assert measurement.record_bytes > 0
    assert measurement.total_bytes == measurement.record_bytes + 100


def test_d_the_listing_is_flat_and_never_walks(tmp_path):
    """A Stage-5 cache has no subdirectories, and `os.walk` descends an NTFS junction.

    That is the measured hazard `cache_admin` has to prune for. A counter has no business
    following a reparse point at all, so it does not recurse.
    """
    body = _body(os.path.join(_REPO_ROOT, "src", "beatsync_fork", "analysis_cache.py"), "measure")
    assert "os.walk" not in body
    assert "os.listdir" in body


@pytest.mark.parametrize("count,expected", [
    (0, "0 B"), (999, "999 B"), (1000, "1.0 KB"), (1_500_000, "1.5 MB"),
    (2_000_000_000, "2.0 GB"),
    # total, never raising: a read-out must not be able to fail a UI callback
    (-5, "0 B"), (None, "0 B"), ("nonsense", "0 B"), (float("nan"), "0 B"),
])
def test_d_byte_formatting_is_total(count, expected):
    assert ac.format_bytes(count) == expected


# ===========================================================================
# 5. import
# ===========================================================================


def test_e_a_reusable_record_is_copied(tmp_path):
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(dest)
    _write_record(source, _record_name())
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.copied == 1
    assert outcome.already_present == outcome.incompatible == outcome.failed == 0
    assert os.path.isfile(os.path.join(dest, _record_name()))


def test_e_the_source_folder_is_never_modified(tmp_path):
    """The production cache must come out of an import byte-identical. It is the owner's data."""
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(dest)
    path = _write_record(source, _record_name())
    before = (open(path, "rb").read(), sorted(os.listdir(source)))
    ac.import_records(source, dest, "stage5_cache_v3")
    assert (open(path, "rb").read(), sorted(os.listdir(source))) == before


def test_e_an_existing_destination_record_is_kept_byte_for_byte(tmp_path):
    """Import is additive. The destination always wins, and is not even opened for writing.

    The guarantee is the exclusive create itself, not a preceding `exists()` check, so there is no
    window between asking and acting.
    """
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    _write_record(source, _record_name(), video="C:/media/NEW.mp4")
    existing = _write_record(dest, _record_name(), video="C:/media/ORIGINAL.mp4")
    original = open(existing, "rb").read()

    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.copied == 0
    assert outcome.already_present == 1
    assert open(existing, "rb").read() == original, "the destination record was overwritten"


def test_e_a_different_cache_generation_is_not_copied(tmp_path):
    """Its filename could never be looked up again, so copying it is moving dead weight."""
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(dest)
    _write_record(source, _record_name(), contract="stage5_cache_v2")
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.incompatible == 1
    assert outcome.copied == 0
    assert os.listdir(dest) == []
    assert any("stage5_cache_v2" in line for line in outcome.first_errors)


@pytest.mark.parametrize("payload,why", [
    ({"video_file": "C:/a.mp4"}, "no contract recorded"),
    ({"cache_contract": "stage5_cache_v3"}, "no source path"),
    ({"cache_contract": "stage5_cache_v3", "video_file": "C:/a.mp4",
      "ai_deferred": True}, "still deferred"),
])
def test_e_transport_criteria_reject_what_a_copier_must_reject(tmp_path, payload, why):
    path = str(tmp_path / _record_name())
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    reusable, reason = ac.looks_reusable(path, "stage5_cache_v3")
    assert reusable is False, why
    assert reason


def test_e_an_unreadable_record_is_reported_not_copied(tmp_path):
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(source)
    os.makedirs(dest)
    with open(os.path.join(source, _record_name()), "w", encoding="utf-8") as handle:
        handle.write("{ truncated")
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.incompatible == 1
    assert os.listdir(dest) == []


def test_e_non_record_files_are_skipped_and_counted(tmp_path):
    """Media, renders, models and the retained worker JSON must never be copied."""
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(dest)
    _write_record(source, _record_name())
    for name in ("qwen_batch_request_a.json", "qwen_batch_response_a.json",
                 "a-big-movie.mp4", "model.gguf", "notes.txt"):
        with open(os.path.join(source, name), "w") as handle:
            handle.write("x")
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.copied == 1
    assert outcome.skipped_other_files == 5
    assert sorted(os.listdir(dest)) == [_record_name()]


def test_e_importing_a_folder_onto_itself_is_refused(tmp_path):
    """A no-op that reports "all already present" looks like a success and teaches nothing."""
    folder = str(tmp_path / "cache")
    _write_record(folder, _record_name())
    outcome = ac.import_records(folder, folder, "stage5_cache_v3")
    assert outcome.refusal
    assert outcome.copied == 0
    # and it is compared by resolved path, not by string: trailing separators and drive case differ
    other = ac.import_records(folder + os.sep, folder, "stage5_cache_v3")
    assert other.refusal


def test_e_a_missing_source_is_refused(tmp_path):
    outcome = ac.import_records(str(tmp_path / "nope"), str(tmp_path / "dest"),
                                "stage5_cache_v3")
    assert outcome.refusal
    assert outcome.copied == 0


def test_e_a_blank_destination_is_refused(tmp_path):
    outcome = ac.import_records(str(tmp_path), "", "stage5_cache_v3")
    assert outcome.refusal


def test_e_a_source_with_no_records_is_reported_clearly(tmp_path):
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(source)
    os.makedirs(dest)
    with open(os.path.join(source, "readme.txt"), "w") as handle:
        handle.write("x")
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.refusal
    assert "No analysis records" in outcome.refusal
    assert outcome.skipped_other_files == 1


def test_e_a_failed_write_removes_only_its_own_attempt(tmp_path, monkeypatch):
    """A half-written record must not survive at the final name — and the removal must be of the
    file this call exclusively created a moment earlier, never of the operator's data.
    """
    source, dest = str(tmp_path / "prod"), str(tmp_path / "shared")
    os.makedirs(dest)
    _write_record(source, _record_name())

    real_fdopen = os.fdopen

    def failing_fdopen(fd, *args, **kwargs):
        stream = real_fdopen(fd, *args, **kwargs)

        class Failing:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                stream.close()
                return False

            def write(self, data):
                raise OSError(28, "No space left on device")

        return Failing()

    monkeypatch.setattr(os, "fdopen", failing_fdopen)
    outcome = ac.import_records(source, dest, "stage5_cache_v3")
    assert outcome.failed == 1
    assert outcome.copied == 0
    assert os.listdir(dest) == [], "a failed attempt left a file behind"


def test_e_the_module_deletes_nothing_except_its_own_temporaries():
    """**The single most dangerous thing this module could grow, asserted by absence.**

    `input/video_analysis_cache/` is preserved by policy and a shared cache is read by several
    installations, so there is no cleanup path here at all. The only `os.unlink` calls permitted
    are the two that remove a file this very call created: the config writer's `mkstemp`
    temporary, and a record copy whose write failed.
    """
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "analysis_cache.py")
    source = _executable_source(path)
    for forbidden in ("shutil", "rmtree", "os.rmdir", "os.removedirs", "os.remove("):
        assert forbidden not in source, f"analysis_cache references {forbidden}"
    assert source.count("os.unlink(") == 2, (
        "exactly two self-cleanup removals: the config temp and a failed copy")


def test_e_the_module_computes_no_cache_identity():
    """Identity stays entirely in `video_analysis.py`, so the two cannot drift.

    The record *filename* is opaque here: it is recognised by shape and otherwise copied verbatim.
    A second signature formula would be a second answer to "which record is this source's?".
    """
    path = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "analysis_cache.py")
    source = _executable_source(path)
    for forbidden in ("hashlib", "sha1", "blake2", "_video_signature", "st_mtime_ns",
                      "CACHE_CONTRACT_VERSION", "ANALYSIS_VERSION", "fingerprint"):
        assert forbidden not in source, f"analysis_cache references {forbidden}"
    # the contract arrives as a PARAMETER, which is what keeps it from being restated
    assert "contract" in [a.arg for a in _func(path, "looks_reusable").args.args]
    assert "contract" in [a.arg for a in _func(path, "import_records").args.args]


# ===========================================================================
# 6. the Stage-5 seam
# ===========================================================================


def test_f_there_is_exactly_one_resolver_and_both_entry_points_use_it():
    """Scan and Analyze must resolve the same folder, or Scan would classify against one cache
    while Analyze wrote into another — a scan reporting every source prepared and an analysis
    re-doing all of them.
    """
    source = open(_VA, encoding="utf-8").read()
    assert source.count("def resolve_cache_location") == 1
    for name in ("analyze_video_sources", "classify_library_sources"):
        body = _body(_VA, name)
        assert "resolve_cache_location()" in body, f"{name} must use the one resolver"
        assert body.count("resolve_cache_location()") == 1, (
            f"{name} must resolve ONCE per invocation, not per source")


def test_f_the_resolved_directory_reaches_no_signature():
    """**The property that makes sharing work rather than being a migration.**

    Identity is `contract | version | abspath | size | mtime_ns | fingerprint | backend | config`.
    The directory is not in it, so the same source produces the same filename in every
    installation. If the folder were a signature component, changing it would re-key every record
    — the exact opposite of the feature.
    """
    for name in ("_video_signature", "_qwen_config_token", "_qwen_backend_signature_token",
                 "_bounded_fingerprint", "_cache_entry_is_complete",
                 "_stored_ai_cache_is_consistent", "_checkpoint_cache", "_save_cache"):
        body = _body(_VA, name)
        for forbidden in ("cache_dir", "resolve_cache_location", "fork_analysis_cache",
                          "CacheLocation", "shared"):
            assert forbidden not in body, f"{name} must not know about {forbidden}"
    # `_cache_path` is the one identity-adjacent function that DOES take it -- it builds the
    # filename, which is a directory plus a signature -- and it must not pass it onward.
    signature_calls = [n for n in ast.walk(_func(_VA, "_cache_path"))
                       if isinstance(n, ast.Call)
                       and getattr(n.func, "id", None) == "_video_signature"]
    assert len(signature_calls) == 1
    assert "cache_dir" not in ast.unparse(signature_calls[0]), (
        "the directory must not reach the signature")


def test_f_the_cache_contract_constant_is_untouched():
    """No record was invalidated by this feature, so there is no migration to perform."""
    source = open(_VA, encoding="utf-8").read()
    assert 'CACHE_CONTRACT_VERSION = "stage5_cache_v3"' in source
    assert 'ANALYSIS_VERSION = "auto_av_analysis_v8_llama_vulkan_batched"' in source


def test_f_the_worker_artifacts_stay_in_the_local_folder():
    """The retained Qwen request/response JSON is this installation's debugging material.

    It is anchored to one invocation's response path, so pooling several installations'
    invocations into one shared folder would make the A2-S1 same-invocation recovery anchor
    meaningless. Records are portable; worker scratch is not.
    """
    for name in ("_run_qwen_worker", "_run_qwen_worker_batch"):
        body = _body(_VA, name)
        assert "VIDEO_ANALYSIS_CACHE_DIR" in body, f"{name} must use the local folder"
        assert "cache_dir" not in body, f"{name} must not follow the shared setting"


def test_f_the_gui_reaches_the_same_resolver():
    """A Settings panel that computed the location independently could show one folder while the
    renderer used another — the most misleading thing this feature could do."""
    body = _body(_GUI, "_analysis_cache_location")
    assert "resolve_cache_location" in body
    # and the clearable-cache category now points at the resolved folder, so Inspect and Clear
    # act on what is really in use
    categories = _body(_GUI, "_cache_categories")
    assert "_analysis_cache_location()" in categories
    assert "location.path" in categories


def test_f_clearing_a_shared_cache_warns_that_it_is_shared():
    """Clearing a local cache costs one installation; clearing a shared one costs every
    installation on the machine, including the production install the operator is not looking at.
    """
    categories = _body(_GUI, "_cache_categories")
    assert "INFO_CACHE_ANALYSIS_SHARED" in categories
    assert "location.shared" in categories
    from ui_content import INFO_CACHE_ANALYSIS_SHARED
    rendered = INFO_CACHE_ANALYSIS_SHARED.format(path=r"J:\Shared")
    assert "SHARED" in rendered
    assert r"J:\Shared" in rendered
    assert "every one of them" in rendered


def test_f_the_shared_warning_actually_reaches_the_screen():
    """**Found by a real browser smoke of the live panel, not by inspection.**

    Setting the warning on the category was not enough: `cache_admin.report_lines` and
    `cleared_lines` print each category's **label** and sizes and never its `info`, so the Inspect
    read-out said only `Video analysis cache (Stage 5): 8 file(s), 152.9 KB` and the warning was
    unreachable. A test that only checked `_cache_categories` passed throughout.

    So both read-outs are pinned instead, by the helper they share.
    """
    notes = _body(_GUI, "_analysis_cache_notes")
    assert "INFO_CACHE_ANALYSIS_SHARED" in notes
    assert "location.shared" in notes
    assert "location.notice" in notes, "an unreachable shared folder must be stated here too"
    for handler in ("_on_inspect_cache", "_on_clear_cache"):
        assert "_analysis_cache_notes()" in _body(_GUI, handler), handler
    # And on the REFUSAL path, so it is read BEFORE anything is deleted rather than only after.
    clear = _body(_GUI, "_on_clear_cache")
    assert clear.index("_analysis_cache_notes()") < clear.index("fork_cache.clear(")


def test_f_the_panel_description_names_the_configured_folder_on_load():
    """A `gr.Markdown` value is static, so the on-load description is built from the resolved
    category rather than from the constant -- otherwise the screen would describe a local cache
    while the renderer used a shared one."""
    with open(_GUI, encoding="utf-8") as handle:
        source = handle.read()
    block = source[source.index("LABEL_CACHE_SELECT"):]
    block = block[:block.index("cache_confirm = ")]
    assert "_analysis_category_explanation()" in block, (
        "the analysis description must come from the built category, not the constant")
    body = _body(_GUI, "_analysis_category_explanation")
    assert "_cache_categories()" in body
    assert "fork_cache.ANALYSIS" in body


def test_f_the_category_description_field_is_the_real_one():
    """**The field name is read off the real `Category` record, not asserted from memory.**

    A first version of the on-load description reached for `category.info`. `Category` has no such
    field — it is `explanation` — and because the line runs at UI-CONSTRUCTION time the whole app
    failed to start with an `AttributeError`. Every source-inspecting test passed, and only a live
    launch found it.

    So this test constructs the real frozen record and reads the attribute the GUI reads. It is
    deliberately a behavioural check on a real object rather than a string search, which is the
    only kind of test that could have caught the original mistake.
    """
    from beatsync_fork import cache_admin

    category = cache_admin.Category(cache_admin.ANALYSIS, "label", "the description",
                                    r"J:\Cache", expensive=True)
    assert category.explanation == "the description"
    assert not hasattr(category, "info"), (
        "if Category ever gains an `info` field, the GUI helper must be revisited")
    body = _body(_GUI, "_analysis_category_explanation")
    assert "category.explanation" in body
    assert ".info" not in body
    # ...and it must be total: a read-out for a cache may never stop the app being built.
    assert "except Exception" in body
    assert "INFO_CACHE_ANALYSIS" in body, "there must be a constant fallback"


def test_f_the_cache_panel_handlers_write_only_their_own_status_box():
    """Changing where the cache lives must not be able to invalidate a confirmed source set,
    disable Create Video, trigger a scan or alter a render request."""
    source = open(_GUI, encoding="utf-8").read()
    for handler in ("_on_inspect_shared_cache", "_on_save_shared_cache",
                    "_on_import_analysis_cache"):
        body = _body(_GUI, handler)
        for forbidden in ("source_state", "prep_state", "session_state", "_RENDER_LOCK",
                          "RenderLifecycle", "process_video", "create_music_video",
                          "resolve_for_render", "analyze_beats_auto", "gr.State"):
            assert forbidden not in body, f"{handler} reaches {forbidden}"
    for registration in ("shared_cache_inspect_btn.click", "shared_cache_save_btn.click",
                         "cache_import_btn.click"):
        start = source.index(registration)
        block = source[start:source.index(")", source.index("concurrency_limit", start))]
        assert "outputs=[shared_cache_status]" in block, registration
        assert "concurrency_id" not in block, (
            f"{registration} must not join a render lane")


def test_f_switching_sharing_on_with_no_folder_is_refused():
    """`enabled=True` with a blank path resolves straight back to the local cache, so the operator
    would believe sharing was on. Refusing is the only honest answer."""
    body = _body(_GUI, "_on_save_shared_cache")
    assert "STATUS_SHARED_CACHE_SAVE_NEEDS_FOLDER" in body
    assert "save_config" in body
    # the refusal must come BEFORE the write
    assert body.index("STATUS_SHARED_CACHE_SAVE_NEEDS_FOLDER") < body.index("save_config")
