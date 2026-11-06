"""R14-A stage A4: the read-only cache-location model.

Three things this suite is built to prove, and they are different claims:

1. **Nothing is created.** The configured folder, which ``analysis_cache.resolve_location``
   deliberately creates when its parent exists, must survive an A4 inspection absent. A negative
   control runs the *real* mutating resolver on an exclusively owned synthetic fixture, so the
   hazard being guarded against is demonstrated rather than asserted.
2. **A false ``available`` cannot become ACTIVE.** With a refusing ``make_dirs`` the resolver
   answers ``source='shared' available=True`` for a directory that does not exist. A4 must
   establish the directory state itself and report ``CONFIGURED_NOT_CREATED``.
3. **The four questions stay separate.** ``configured``, ``shared_requested``, ``observed`` and
   ``active`` are asserted independently, including the cases where they disagree -- an absent
   folder that is configured and requested but neither observed nor active, and an ACCESS_DENIED
   folder that is observed but still not active.

Every fixture is synthetic and `tmp_path`-based. No production path, no operator cache, no
`shared-config.json`, no media. The settings are always handed in as a plain dict.
"""

from __future__ import annotations

import ast
import builtins
import io
import os
import stat as stat_module
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from beatsync_fork import analysis_cache as ac                       # noqa: E402
from beatsync_fork import library_cache_location as lcl              # noqa: E402
from beatsync_fork import library_inventory as lib                   # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_A4 = os.path.join(_REPO_ROOT, "src", "beatsync_fork", "library_cache_location.py")

ENABLED = ac.CACHE_ENABLED
FOLDER = ac.CACHE_FOLDER


# =============================================================================================
# fixtures and helpers
# =============================================================================================

def _cfg(enabled=None, folder=None) -> dict:
    """A settings mapping. Deliberately built like the stored file, not like a tidy object."""
    out = dict(ac.DEFAULTS)
    if enabled is not None:
        out[ENABLED] = enabled
    if folder is not None:
        out[FOLDER] = folder
    return out


def _local(tmp_path, *, create=True) -> str:
    path = tmp_path / "checkout" / "input" / "video_analysis_cache"
    if create:
        path.mkdir(parents=True)
    return str(path)


def _manifest(root) -> tuple:
    """Every entry under ``root`` as ``(relpath, is_dir, size, mtime_ns)``, plus the dir set."""
    rows = []
    dirs = set()
    for base, subdirs, files in os.walk(str(root)):
        rel_base = os.path.relpath(base, str(root))
        dirs.add(os.path.normcase(rel_base))
        for name in sorted(subdirs):
            p = os.path.join(base, name)
            rows.append((os.path.normcase(os.path.relpath(p, str(root))), True, 0, 0))
        for name in sorted(files):
            p = os.path.join(base, name)
            info = os.lstat(p)
            rows.append((os.path.normcase(os.path.relpath(p, str(root))), False,
                         info.st_size, info.st_mtime_ns))
    return tuple(sorted(rows)), frozenset(dirs)


def _absent_drive_letter() -> str | None:
    """A drive letter that is genuinely absent, found by probing rather than assumed."""
    for letter in "QRSTUVWXYZ":
        root = f"{letter}:\\"
        if not os.path.exists(root) and not os.path.isdir(root):
            return letter
    return None


def _make_junction(link: str, target: str) -> bool:
    """A directory junction, preferred over a symlink: `mklink /J` needs no elevation."""
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", os.path.normpath(link),
                               os.path.normpath(target)], capture_output=True, text=True)
        if done.returncode == 0:
            return True
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False


class _CountingFS:
    """Counts the filesystem calls A4 makes, and never hides one."""

    def __init__(self, *, deny=(), vanish_after=None):
        self.listdir = []
        self.lstat = []
        self.isdir = []
        self.realpath = []
        self._deny = {os.path.normcase(os.path.abspath(p)) for p in deny}
        self._vanish_after = vanish_after
        self._isdir_seen = {}

    def _denied(self, path) -> bool:
        return os.path.normcase(os.path.abspath(str(path))) in self._deny

    def wrap_listdir(self, path):
        self.listdir.append(str(path))
        if self._denied(path):
            raise PermissionError(13, "Access is denied")
        return os.listdir(path)

    def wrap_lstat(self, path):
        self.lstat.append(str(path))
        return os.lstat(path)

    def wrap_isdir(self, path):
        self.isdir.append(str(path))
        key = os.path.normcase(os.path.abspath(str(path)))
        if self._vanish_after is not None:
            target, after = self._vanish_after
            if key == os.path.normcase(os.path.abspath(target)):
                self._isdir_seen[key] = self._isdir_seen.get(key, 0) + 1
                if self._isdir_seen[key] > after:
                    return False
        return os.path.isdir(path)

    def wrap_realpath(self, path):
        self.realpath.append(str(path))
        return os.path.realpath(path)

    def kwargs(self) -> dict:
        return {"listdir": self.wrap_listdir, "lstat": self.wrap_lstat,
                "isdir": self.wrap_isdir, "realpath": self.wrap_realpath}


class _OpenSpy:
    """Records every ``open`` while active. A4 must make none."""

    def __enter__(self):
        self.calls: list = []
        self._real = builtins.open

        def patched(path, *a, **k):
            self.calls.append(str(path))
            return self._real(path, *a, **k)

        builtins.open = patched
        return self

    def __exit__(self, *exc):
        builtins.open = self._real
        return False


def _mutant(*replacements):
    """A copy of the A4 module with one safety property weakened.

    Each anchor is asserted to have applied exactly once. An anchor that silently matches nothing
    produces a mutant identical to the original, and a 'surviving' mutant then proves nothing --
    a trap this repository has hit before.
    """
    with open(_A4, "r", encoding="utf-8") as handle:
        source = handle.read()
    for old, new in replacements:
        assert source.count(old) == 1, f"anchor matched {source.count(old)} times: {old!r}"
        source = source.replace(old, new)
    name = f"mutant_library_cache_location_{abs(hash(source)):x}"
    module = types.ModuleType(name)
    module.__dict__["__file__"] = _A4
    # Registered before the exec: `dataclasses` resolves a field's string annotation through
    # `sys.modules[cls.__module__]`, so an unregistered module makes every @dataclass raise.
    sys.modules[name] = module
    try:
        exec(compile(source, "<mutant>", "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    return module


# =============================================================================================
# A. the mandatory resolver hazard -- measured in both directions
# =============================================================================================

def test_a_the_no_op_resolver_does_not_create_the_configured_folder(tmp_path):
    """The central safety property. The folder must still be absent afterwards."""
    local = _local(tmp_path)
    shared = str(tmp_path / "shared-cache")          # parent (tmp_path) exists; this does not
    assert os.path.isdir(os.path.dirname(shared))
    assert not os.path.exists(shared)

    before = _manifest(tmp_path)
    report = lcl.inspect_location(local, _cfg(True, shared))
    after = _manifest(tmp_path)

    assert not os.path.exists(shared), "A4 created the configured cache folder"
    assert before == after, "the fixture tree changed under a read-only inspection"

    # the resolver WAS asked to create it, and was refused -- positive evidence, not silence
    assert report.creation_attempts == (shared,)


def test_a_the_resolver_really_does_create_it_without_the_no_op(tmp_path):
    """NEGATIVE CONTROL: the hazard is real, so the guard above is not guarding nothing.

    This runs the genuine mutating `resolve_location` against a directory this test exclusively
    owns inside its own `tmp_path`. It is never pointed at an operator or production path.
    """
    local = _local(tmp_path)
    shared = str(tmp_path / "created-by-the-real-resolver")
    assert not os.path.exists(shared)

    location = ac.resolve_location(local, _cfg(True, shared))     # default args == mutating

    assert os.path.isdir(shared), "the documented creation behaviour did not reproduce"
    assert location.source == ac.SOURCE_SHARED and location.available is True
    # ...and A4, on the same now-existing folder, reports it ACTIVE because it really is there
    report = lcl.inspect_location(local, _cfg(True, shared))
    assert report.active is True and report.status == lib.PRESENT


def test_a_a_false_available_cannot_become_active(tmp_path):
    """The exact false-available response, and A4's refusal to publish it as ACTIVE."""
    local = _local(tmp_path)
    shared = str(tmp_path / "absent-shared")

    refusal = lcl.RefuseDirectoryCreation()
    raw = ac.resolve_location(local, _cfg(True, shared), make_dirs=refusal)
    # the resolver's own answer, recorded verbatim: it claims availability for a missing directory
    assert raw.source == ac.SOURCE_SHARED
    assert raw.available is True
    assert not os.path.exists(shared)

    report = lcl.inspect_location(local, _cfg(True, shared))
    assert report.resolver_available is True, "the raw response must be recorded, not hidden"
    assert report.status == lib.CONFIGURED_NOT_CREATED
    assert report.active is False
    assert report.shared_in_use is False
    assert report.resolver_disagreed is True
    assert lcl.DISAGREEMENT_CAVEAT in report.caveats


# =============================================================================================
# B. the directory-state matrix, scenarios A-N
# =============================================================================================

def test_b_scenario_a_sharing_disabled_and_no_path(tmp_path):
    local = _local(tmp_path)
    report = lcl.inspect_location(local, _cfg(False, ""))
    assert report.sharing_enabled is False
    assert report.configured_shared == ""
    assert report.configured is False
    assert report.shared_requested is False
    assert report.selected_source == ac.SOURCE_LOCAL
    assert report.selected_path == local
    assert report.active is True and report.status == lib.PRESENT
    assert report.shared.inspected is False
    assert report.shared.verdict == lib.NOT_INSPECTED
    assert report.caveats == (), "no shared folder is configured, so no shared caveat applies"


def test_b_scenario_b_sharing_disabled_preserves_the_stored_path_uninspected(tmp_path):
    """Turning sharing off must not look like forgetting the path -- nor like inspecting it."""
    local = _local(tmp_path)
    shared = tmp_path / "retained-shared"
    shared.mkdir()
    fs = _CountingFS()

    report = lcl.inspect_location(local, _cfg(False, str(shared)), **fs.kwargs())

    assert report.configured_shared == str(shared), "the stored path was lost"
    assert report.configured is True
    assert report.sharing_enabled is False
    assert report.shared_requested is False
    assert report.shared.inspected is False, "a disabled shared folder must not be inspected"
    assert report.shared.verdict == lib.NOT_INSPECTED
    assert report.shared.path == str(shared), "the state still carries the stored path"
    # ...and nothing in the pass touched that directory, even though it exists
    touched = [p for p in fs.listdir + fs.lstat + fs.realpath
               if os.path.normcase(str(shared)) in os.path.normcase(p)]
    assert touched == [], f"the disabled shared folder was probed: {touched}"
    # the ownership caveat still travels, because the folder is configured
    assert lcl.SHARED_CAVEAT in report.caveats
    assert report.selected_source == ac.SOURCE_LOCAL
    assert report.active is True


def test_b_scenario_c_sharing_enabled_on_an_existing_readable_folder(tmp_path):
    local = _local(tmp_path)
    shared = tmp_path / "live-shared"
    shared.mkdir()
    report = lcl.inspect_location(local, _cfg(True, str(shared)))
    assert report.sharing_enabled is True
    assert report.shared_requested is True
    assert report.selected_source == ac.SOURCE_SHARED
    assert report.selected_path == str(shared)
    assert report.shared.inspected is True
    assert report.shared.observed is True
    assert report.shared.inspectable is True
    assert report.status == lib.PRESENT
    assert report.active is True
    assert report.shared_in_use is True
    assert report.fallback is False
    assert report.resolver_disagreed is False
    assert lcl.SHARED_CAVEAT in report.caveats


def test_b_scenario_d_absent_folder_with_a_reachable_parent(tmp_path):
    local = _local(tmp_path)
    shared = str(tmp_path / "not-created-yet")
    report = lcl.inspect_location(local, _cfg(True, shared))
    assert report.status == lib.CONFIGURED_NOT_CREATED
    assert report.configured is True and report.shared_requested is True
    assert report.shared.inspected is True
    assert report.shared.observed is False
    assert report.shared.inspectable is False
    assert report.shared.anchor_reachable is True, "the parent volume IS live"
    assert report.active is False
    assert not os.path.exists(shared)


def test_b_scenario_e_an_unavailable_drive_is_unreachable_not_absent(tmp_path):
    letter = _absent_drive_letter()
    if letter is None:
        pytest.skip("no absent drive letter available on this machine")
    local = _local(tmp_path)
    shared = f"{letter}:\\BeatSyncSharedCache"
    report = lcl.inspect_location(local, _cfg(True, shared))
    assert report.shared.verdict == lib.ROOT_UNREACHABLE
    assert report.shared.anchor_reachable is False
    assert report.shared.observed is False
    assert report.active is False
    # the resolver itself falls back here, and says so
    assert report.selected_source == ac.SOURCE_LOCAL
    assert report.fallback is True
    assert report.resolver_available is False
    assert report.resolver_notice != ""
    assert lcl.FALLBACK_CAVEAT in report.caveats


def test_b_scenario_f_an_unavailable_unc_location_is_unreachable(tmp_path):
    local = _local(tmp_path)
    shared = r"\\no-such-host-r14a\share\cache"
    report = lcl.inspect_location(local, _cfg(True, shared))
    assert report.shared.inspected is True
    assert report.shared.verdict in (lib.ROOT_UNREACHABLE, lib.UNKNOWN)
    assert report.shared.observed is False
    assert report.active is False
    assert report.shared_in_use is False


def test_b_scenario_g_permission_denied_is_observed_but_not_active(tmp_path):
    """ACCESS_DENIED is the sharpest case: the folder IS there, and still cannot be inspected."""
    local = _local(tmp_path)
    shared = tmp_path / "denied-shared"
    shared.mkdir()
    fs = _CountingFS(deny=[str(shared)])

    report = lcl.inspect_location(local, _cfg(True, str(shared)), **fs.kwargs())

    assert report.shared.verdict == lib.ACCESS_DENIED
    assert report.shared.observed is True, "the directory was positively seen to exist"
    assert report.shared.inspectable is False, "...and its contents cannot be examined"
    assert report.active is False, "ACCESS_DENIED must not be published as ACTIVE"
    assert report.shared_in_use is False
    assert report.status == lib.ACCESS_DENIED


def test_b_scenario_h_an_unresolvable_reparse_point_cannot_be_verified(tmp_path):
    """A broken junction must fail closed: its real location is unknown."""
    local = _local(tmp_path)
    link = tmp_path / "broken-link"
    target = tmp_path / "target-to-remove"
    target.mkdir()
    if not _make_junction(str(link), str(target)):
        pytest.skip("no directory-link primitive available in this environment")
    os.rmdir(str(target))        # the junction now dangles

    report = lcl.inspect_location(local, _cfg(True, str(link)))

    assert report.shared.verdict in (lib.UNKNOWN, lib.ROOT_UNREACHABLE,
                                     lib.CONFIGURED_NOT_CREATED)
    assert report.shared.observed is False, "a dangling link was treated as positive evidence"
    assert report.shared.inspectable is False
    assert report.active is False


def test_b_scenario_i_an_explicitly_selected_junction_resolves_to_its_target(tmp_path):
    """A junction root is a legitimate, measured setup and must be followed, not refused."""
    local = _local(tmp_path)
    target = tmp_path / "real-cache-location"
    target.mkdir()
    link = tmp_path / "cache-link"
    if not _make_junction(str(link), str(target)):
        pytest.skip("no directory-link primitive available in this environment")

    report = lcl.inspect_location(local, _cfg(True, str(link)))

    assert report.shared.verdict == lib.PRESENT
    assert report.shared.observed is True
    assert report.shared.inspectable is True
    assert report.shared.is_reparse is True, "the link was not recognised as one"
    assert os.path.normcase(report.shared.real) == os.path.normcase(str(target)), \
        "identity must be the RESOLVED location, not the typed path"
    assert report.active is True
    assert report.shared_in_use is True


@pytest.mark.skipif(not os.environ.get("BEATSYNC_TEST_XVOL_DIR"),
                    reason="set BEATSYNC_TEST_XVOL_DIR to a writable directory on a DIFFERENT "
                           "volume to exercise a genuinely cross-volume junction")
def test_b_scenario_i_cross_volume_junction_resolves_to_the_other_volume(tmp_path):
    """The F-1 lesson: a cross-volume junction's identity is the volume holding the data."""
    other = os.environ["BEATSYNC_TEST_XVOL_DIR"].strip()
    if os.path.splitdrive(other)[0].lower() == os.path.splitdrive(str(tmp_path))[0].lower():
        pytest.skip("BEATSYNC_TEST_XVOL_DIR is on the same volume as tmp_path")
    target = os.path.join(other, "r14a_a4_xvol_target")
    os.makedirs(target, exist_ok=True)
    link = tmp_path / "xvol-cache-link"
    if not _make_junction(str(link), target):
        pytest.skip("no directory-link primitive available in this environment")

    report = lcl.inspect_location(_local(tmp_path), _cfg(True, str(link)))

    assert report.shared.verdict == lib.PRESENT
    assert os.path.normcase(report.shared.real) == os.path.normcase(os.path.realpath(target))
    assert os.path.splitdrive(report.shared.anchor)[0].lower() == \
        os.path.splitdrive(target)[0].lower(), "the anchor must name the volume holding the data"
    assert report.active is True


def test_b_scenario_j_a_path_that_vanishes_between_observations_is_not_active(tmp_path):
    """The resolver's `isdir` and A4's own probe are two observations; the probe is authoritative."""
    local = _local(tmp_path)
    shared = tmp_path / "vanishing-shared"
    shared.mkdir()
    # the resolver's isdir sees it; every later isdir does not
    fs = _CountingFS(vanish_after=(str(shared), 1))

    report = lcl.inspect_location(local, _cfg(True, str(shared)), **fs.kwargs())

    assert report.resolver_available is True, "the resolver saw it and said so"
    assert report.selected_source == ac.SOURCE_SHARED
    assert report.active is False, "the later independent observation must win"
    assert report.shared.inspectable is False
    assert report.resolver_disagreed is True
    assert lcl.DISAGREEMENT_CAVEAT in report.caveats


def test_b_scenario_k_and_l_the_local_cache_exists_or_does_not_yet(tmp_path):
    present = lcl.inspect_location(_local(tmp_path), _cfg(False, ""))
    assert present.local.inspected is True
    assert present.local.verdict == lib.PRESENT
    assert present.local.observed is True
    assert present.active is True

    fresh = tmp_path / "second" / "input" / "video_analysis_cache"
    fresh.parent.mkdir(parents=True)
    absent = lcl.inspect_location(str(fresh), _cfg(False, ""))
    assert absent.local.inspected is True
    assert absent.local.verdict == lib.CONFIGURED_NOT_CREATED, \
        "a first run is 'not created yet', never a fault and never unreachable"
    assert absent.active is False
    assert not os.path.exists(str(fresh)), "the local cache folder was created"


@pytest.mark.parametrize("enabled,folder,why", [
    (None, None, "the stored defaults"),
    ("", "", "both blank"),
    ("   ", "   ", "whitespace only"),
    ("yes", "", "enabled with no path"),
    (True, "   ", "enabled with a whitespace path"),
    (1, "", "a non-bool truthy switch"),
    (None, 12345, "a non-string path"),
    ({"not": "a bool"}, ["not", "a path"], "both the wrong type"),
])
def test_b_scenario_m_blank_or_malformed_configuration_never_raises(tmp_path, enabled, folder, why):
    """A hand-editable file may hold anything. The existing parsers decide; A4 adds no second rule."""
    local = _local(tmp_path)
    report = lcl.inspect_location(local, _cfg(enabled, folder))
    assert isinstance(report.sharing_enabled, bool)
    assert isinstance(report.configured_shared, str)
    assert report.status in lcl.LOCATION_STATES
    # with no usable configured path there is nothing to request and nothing shared to inspect
    if not report.configured_shared:
        assert report.shared_requested is False
        assert report.selected_source == ac.SOURCE_LOCAL
        assert report.shared.inspected is False
    # and the answers agree with the ONE authoritative parser, not with a local guess
    assert report.sharing_enabled == ac._as_bool(_cfg(enabled, folder).get(ENABLED))
    assert report.configured_shared == ac._clean(_cfg(enabled, folder).get(FOLDER))


def test_b_scenario_n_a_later_configuration_change_does_not_move_the_answer(tmp_path):
    """The snapshot is taken once, so a Settings change cannot move the cache mid-inspection.

    This covers the **caller-dict** case: an operator's Settings change arriving while the pass is
    on the filesystem. It is a real case and worth keeping.

    It does **not** cover the copy at ``inspect_location``'s ``snapshot = ... dict(config)``, and
    must not be read as covering it. `inspect_location` reads ``CACHE_FOLDER``/``CACHE_ENABLED``
    into locals before its first filesystem touch, and ``resolve_location`` likewise reads its own
    settings before its first ``isdir()`` -- so a mutation delivered through an injected
    filesystem callable always fires *after every read* and these three assertions hold for a
    structural reason, whether or not the copy is there. Replacing ``dict(config)`` with ``config``
    leaves this test green. The window is reachable only by a mapping that mutates itself when
    **read**, which is
    `test_b_scenario_n2_a_mapping_that_rewrites_itself_when_read_cannot_move_the_answer` below.
    """
    local = _local(tmp_path)
    shared = tmp_path / "snapshot-shared"
    shared.mkdir()
    other = tmp_path / "switched-to"
    other.mkdir()

    live = _cfg(True, str(shared))

    class _MutatingProbe:
        """Rewrites the caller's dict the moment the pass starts touching the filesystem."""

        def __init__(self, cfg):
            self.cfg = cfg
            self.fired = False

        def __call__(self, path):
            if not self.fired:
                self.fired = True
                self.cfg[FOLDER] = str(other)
                self.cfg[ENABLED] = False
            return os.path.isdir(path)

    probe = _MutatingProbe(live)
    report = lcl.inspect_location(local, live, isdir=probe)

    assert probe.fired is True, "the fixture never mutated the configuration"
    assert live[FOLDER] == str(other) and live[ENABLED] is False, "the caller's dict did change"
    assert report.configured_shared == str(shared), "the invocation snapshot was not frozen"
    assert report.sharing_enabled is True
    assert report.selected_path == str(shared)


class _SelfMutatingConfig(dict):
    """A settings mapping that rewrites itself when it is **read**.

    The only fixture that can reach ``inspect_location``'s one-shot configuration copy. Scenario N
    above mutates from an injected ``isdir``, which always fires after both settings have already
    been read into locals; a mapping whose ``get`` rewrites the mapping lands the change *between*
    A4's two reads and ``resolve_location``'s own two reads, which is exactly the window the copy
    exists to close.

    ``dict(mapping)`` does not go through ``get`` -- it copies via the C path -- so a real snapshot
    is taken from the entry-time contents and never sees the rewrite. A live reference does.
    """

    def __init__(self, contents, *, switch_to, after=2):
        super().__init__(contents)
        self.get_calls: list = []
        self.mutated = False
        self._switch_to = switch_to
        self._after = after

    def get(self, key, default=None):
        value = super().get(key, default)
        self.get_calls.append(key)
        if len(self.get_calls) >= self._after and not self.mutated:
            super().__setitem__(FOLDER, self._switch_to)
            self.mutated = True
        return value


def test_b_scenario_n2_a_mapping_that_rewrites_itself_when_read_cannot_move_the_answer(tmp_path):
    """The configuration copy itself, with the one fixture that can actually reach it.

    Asserted in both halves, because either alone is weaker than the property: the report's
    *entry-time* settings, **and** that the folder the report names is the folder it probed. A
    report that names one folder while having probed another is precisely the misstatement this
    stage exists to prevent -- and with the copy removed, that is what comes out.
    """
    local = _local(tmp_path)
    original = tmp_path / "entry-time-shared"
    original.mkdir()
    switched = tmp_path / "switched-mid-pass"
    switched.mkdir()

    original_key = os.path.normcase(os.path.abspath(str(original)))
    switched_key = os.path.normcase(os.path.abspath(str(switched)))

    def _touched(fs) -> set:
        """Every path the pass put to the filesystem, listings and existence tests alike."""
        return {os.path.normcase(os.path.abspath(p)) for p in fs.listdir + fs.isdir}

    # (1) The fixture discriminates -- proved on a SEPARATE instance, with no module involved, so
    # that a fixture which quietly stopped working cannot make the arms below pass vacuously.
    control = _SelfMutatingConfig(_cfg(True, str(original)), switch_to=str(switched))
    assert control.get(FOLDER) == str(original), "the first read must see the entry-time path"
    assert control.get(ENABLED) is True, "the second read must still see the entry-time flag"
    assert control.mutated is True, "the fixture never rewrote itself"
    assert control.get(FOLDER) == str(switched), "a later read must see the switched path"
    assert control[FOLDER] == str(switched), "the mapping itself did not change"

    # (2) The property, on the real module.
    live = _SelfMutatingConfig(_cfg(True, str(original)), switch_to=str(switched))
    fs = _CountingFS()
    report = lcl.inspect_location(local, live, **fs.kwargs())

    assert report.configured_shared == str(original), "the invocation snapshot was not frozen"
    assert report.sharing_enabled is True
    assert report.selected_path == str(original), "the selection followed a mid-pass rewrite"
    assert report.selected_source == ac.SOURCE_SHARED
    assert (os.path.normcase(report.selected_path)
            == os.path.normcase(report.configured_shared)), \
        "the report names one folder and selected another"
    assert switched_key not in _touched(fs), "the post-rewrite folder was put to the filesystem"
    # ...and the entry-time folder WAS probed, so the absence above is a finding and not silence
    assert original_key in _touched(fs), "nothing probed the entry-time folder at all"

    # Deliberately NOT asserted above: that `live.get` was called, or that `live` stayed put.
    # `dict(config)` copies via the C path and never calls `get` at all, so a correct
    # implementation may leave this mapping completely unread. The fixture's own counters are
    # asserted only where they mean something -- on the control instance in (1), and on the
    # mutant in (3).

    # (3) Discrimination control: the same fixture against the module with the copy removed. This
    # arm is what makes (2) a constraint rather than a coincidence -- see `_mutant`'s unique-anchor
    # assertion for why a silently-unapplied mutation cannot masquerade as a survivor.
    weak = _mutant(("    snapshot = empty_config() if config is None else dict(config)",
                    "    snapshot = empty_config() if config is None else config"))
    mut_cfg = _SelfMutatingConfig(_cfg(True, str(original)), switch_to=str(switched))
    mut_fs = _CountingFS()
    mutated = weak.inspect_location(local, mut_cfg, **mut_fs.kwargs())

    assert mut_cfg.get_calls, "the mutant never read the mapping through its own `get`"
    assert mut_cfg.mutated is True, "the mapping did not actually change under the mutant"
    assert mut_cfg[FOLDER] == str(switched)
    assert mutated.configured_shared == str(original), "A4's own read is eager either way"
    assert mutated.selected_path == str(switched), "the mutation did not apply"
    assert (os.path.normcase(mutated.selected_path)
            != os.path.normcase(mutated.configured_shared)), "the mutant report is consistent"
    assert switched_key in _touched(mut_fs), "the mutant never reached the switched folder"


# =============================================================================================
# C. the four questions are separately answerable
# =============================================================================================

def test_c_configured_requested_observed_and_active_are_four_facts(tmp_path):
    """One fixture per combination that can actually occur, asserted field by field.

    Each fact is about a named location, which is the point: `configured` and `shared_requested`
    are about the SHARED folder, `shared.observed` is about its storage, and `active` is about
    whether the REQUESTED location is the one in effect and inspectable.
    """
    local = _local(tmp_path)
    live = tmp_path / "live"; live.mkdir()
    absent = str(tmp_path / "absent")
    denied = tmp_path / "denied"; denied.mkdir()

    rows = []

    off = lcl.inspect_location(local, _cfg(False, str(live)))
    # the shared folder is configured but nothing requested it, so the LOCAL cache is the
    # requested location -- and it is present, so `active` is True about THAT.
    rows.append(("configured, NOT requested, shared not observed, local active", off,
                 (True, False, False, True)))

    missing = lcl.inspect_location(local, _cfg(True, absent))
    rows.append(("configured + requested, NOT observed, NOT active", missing,
                 (True, True, False, False)))

    fs = _CountingFS(deny=[str(denied)])
    refused = lcl.inspect_location(local, _cfg(True, str(denied)), **fs.kwargs())
    rows.append(("configured + requested + OBSERVED, still NOT active", refused,
                 (True, True, True, False)))

    good = lcl.inspect_location(local, _cfg(True, str(live)))
    rows.append(("configured + requested + observed + ACTIVE", good,
                 (True, True, True, True)))

    for label, report, expected in rows:
        got = (report.configured, report.shared_requested, report.shared.observed, report.active)
        assert got == expected, f"{label}: expected {expected}, measured {got}"

    # the sharpest pair: observed and active differ, and neither implies the other
    assert refused.shared.observed is True and refused.active is False
    assert missing.shared_requested is True and missing.shared.observed is False
    # ...and `status` follows the REQUESTED location while `selected_status` follows what you get
    assert missing.status == lib.CONFIGURED_NOT_CREATED
    assert missing.selected_status == lib.CONFIGURED_NOT_CREATED
    assert off.status == lib.PRESENT and off.selected_status == lib.PRESENT


def test_c_active_and_selected_active_are_not_the_same_question(tmp_path):
    """The case that forces two fields: an unreachable shared folder with a healthy fallback.

    A single flag cannot be honest here. The operator did NOT get the folder they asked for, so
    `active` is False; a new invocation nevertheless lands somewhere perfectly usable, so
    `selected_active` is True. Collapsing them would report an unplugged drive as active.
    """
    letter = _absent_drive_letter()
    if letter is None:
        pytest.skip("no absent drive letter available on this machine")
    local = _local(tmp_path)
    report = lcl.inspect_location(local, _cfg(True, os.path.join(letter + ':', 'SharedCache')))

    assert report.fallback is True
    assert report.status == lib.ROOT_UNREACHABLE, "status follows the REQUESTED location"
    assert report.active is False, "the operator did not get the folder they asked for"
    assert report.selected_status == lib.PRESENT, "...but the fallback itself is fine"
    assert report.selected_active is True
    assert report.shared_in_use is False


def test_c_a_fallback_is_never_called_a_running_jobs_active_cache(tmp_path):
    """FALLBACK is what a NEW invocation would do. It is not evidence about a running one."""
    letter = _absent_drive_letter()
    if letter is None:
        pytest.skip("no absent drive letter available on this machine")
    local = _local(tmp_path)
    report = lcl.inspect_location(local, _cfg(True, f"{letter}:\\SharedCache"))

    assert report.fallback is True
    assert report.shared_requested is True
    assert report.shared_in_use is False
    assert lcl.FALLBACK_CAVEAT in report.caveats
    assert "not evidence that an analysis already running" in lcl.FALLBACK_CAVEAT
    # the local fallback is reported explicitly, by path, not merely implied
    assert report.selected_source == ac.SOURCE_LOCAL
    assert report.selected_path == local
    assert report.resolver_notice != "", "the resolver's own wording must travel with it"


def test_c_an_inaccessible_location_never_becomes_an_empty_cache(tmp_path):
    """The whole report must carry no count at all, so 'unreachable' cannot read as 'empty'."""
    letter = _absent_drive_letter()
    local = _local(tmp_path)
    cases = [lcl.inspect_location(local, _cfg(True, str(tmp_path / "absent")))]
    denied = tmp_path / "denied2"; denied.mkdir()
    cases.append(lcl.inspect_location(local, _cfg(True, str(denied)),
                                      **_CountingFS(deny=[str(denied)]).kwargs()))
    if letter is not None:
        cases.append(lcl.inspect_location(local, _cfg(True, f"{letter}:\\x")))

    import dataclasses
    for report in cases:
        assert report.active is False
        numeric = [f.name for f in dataclasses.fields(report)
                   if isinstance(getattr(report, f.name), int)
                   and not isinstance(getattr(report, f.name), bool)]
        assert numeric == [], f"A4 published a number that could read as a count: {numeric}"
        for state in (report.shared, report.local, report.selected):
            nums = [f.name for f in dataclasses.fields(state)
                    if isinstance(getattr(state, f.name), int)
                    and not isinstance(getattr(state, f.name), bool)]
            assert nums == [], f"a LocationState published a number: {nums}"


# =============================================================================================
# D. read-only guards -- the design's §10 safety list and guard 18
# =============================================================================================

def test_d_the_pass_opens_no_file_at_all(tmp_path):
    """No record JSON, no preference file, no shared configuration -- no `open` whatsoever."""
    local = _local(tmp_path)
    shared = tmp_path / "with-records"
    shared.mkdir()
    for name in ("00000001_000000000000000000000001.json", "qwen_batch_request_x.json", "a.txt"):
        (shared / name).write_text("{}", encoding="utf-8")

    with _OpenSpy() as spy:
        report = lcl.inspect_location(local, _cfg(True, str(shared)))

    assert spy.calls == [], f"A4 opened {len(spy.calls)} file(s): {spy.calls[:5]}"
    assert report.active is True
    # ...and no filename from the folder reaches the report
    blob = repr(report)
    for name in ("00000001_", "qwen_batch_request", "a.txt"):
        assert name not in blob, f"a folder entry name leaked into the report: {name}"


def test_d_no_filesystem_modification_under_any_scenario(tmp_path):
    """Before/after manifests AND the directory set, across every reachable branch."""
    local = _local(tmp_path)
    live = tmp_path / "live-shared"; live.mkdir()
    (live / "keep.json").write_text("{}", encoding="utf-8")
    denied = tmp_path / "denied-shared"; denied.mkdir()
    letter = _absent_drive_letter()

    configs = [
        _cfg(False, ""),
        _cfg(False, str(live)),
        _cfg(True, str(live)),
        _cfg(True, str(tmp_path / "never-created")),
        _cfg(True, r"\\no-such-host-r14a\share"),
        _cfg(True, ""),
        _cfg("nonsense", str(live)),
    ]
    if letter is not None:
        configs.append(_cfg(True, f"{letter}:\\SharedCache"))

    before = _manifest(tmp_path)
    for cfg in configs:
        lcl.inspect_location(local, cfg)
        lcl.inspect_location(local, cfg, **_CountingFS(deny=[str(denied)]).kwargs())
    after = _manifest(tmp_path)

    assert before == after, "the fixture tree changed under a read-only inspection"
    assert not os.path.exists(str(tmp_path / "never-created"))


def test_d_the_module_imports_the_standard_library_and_its_own_package_only():
    """Guard 16, asserted here as well as by `test_no_runtime_dependency`'s discovery."""
    with open(_A4, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top.add((node.module or "").split(".")[0])
    assert top <= {"__future__", "os", "dataclasses", "beatsync_fork"}, sorted(top)
    nested = [n for n in ast.walk(tree)
              if isinstance(n, (ast.Import, ast.ImportFrom)) and n not in tree.body]
    assert nested == [], "a nested or conditional import appeared"


def test_d_guard_17_no_destructive_or_writing_primitive_is_referenced():
    """AST, not substring: the module docstring deliberately NAMES what it refuses to call."""
    with open(_A4, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name)
                                                                else "")
            if name:
                called.add(name)
    banned = {"makedirs", "mkdir", "remove", "unlink", "rmdir", "rmtree", "rename", "replace",
              "open", "write_text", "write_bytes", "truncate", "chmod", "utime", "symlink",
              "mkstemp", "mkdtemp", "NamedTemporaryFile", "run", "Popen", "system",
              "save_preferences", "save_config", "write_config", "import_records", "copy_record",
              "clear", "measure", "read_records", "_cache_path", "resolve_cache_location",
              "load_config", "config_path", "default_config_path"}
    assert not (called & banned), f"A4 calls a forbidden primitive: {sorted(called & banned)}"


def test_d_guard_18_every_resolver_call_passes_make_dirs_explicitly():
    """The stage's load-bearing guard. A default-argument resolver call is a creating one."""
    with open(_A4, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name)
                                                                else "")
            if name in ("resolve_location", "resolve_cache_location"):
                calls.append(node)
    assert calls, "no resolver call found -- the guard would be vacuous"
    assert len(calls) == 1, f"expected exactly one resolver call, found {len(calls)}"
    for node in calls:
        keywords = {k.arg for k in node.keywords}
        assert "make_dirs" in keywords, \
            f"a resolver call at line {node.lineno} omits make_dirs"
        value = [k.value for k in node.keywords if k.arg == "make_dirs"][0]
        assert not (isinstance(value, ast.Constant) and value.value is None), \
            "make_dirs=None restores the real os.makedirs"
    # `resolve_cache_location` -- which reads live configuration AND reaches the mutating
    # resolver -- is not referenced at all. Checked by AST: the module docstring deliberately
    # NAMES it in its "what this never does" list, so a substring ban trips on the disclaimer.
    referenced = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "resolve_cache_location" not in referenced
    assert "load_config" not in referenced and "config_path" not in referenced


def test_d_the_refusal_records_what_it_refused(tmp_path):
    """A silent no-op cannot distinguish 'refused' from 'never reached'. This one can."""
    refusal = lcl.RefuseDirectoryCreation()
    assert refusal.attempts == []
    assert refusal("C:\\somewhere", exist_ok=True) is None
    assert refusal.attempts == ["C:\\somewhere"]
    assert not os.path.exists("C:\\somewhere")

    local = _local(tmp_path)
    reached = lcl.inspect_location(local, _cfg(True, str(tmp_path / "x")))
    not_reached = lcl.inspect_location(local, _cfg(False, ""))
    assert reached.creation_attempts == (str(tmp_path / "x"),)
    assert not_reached.creation_attempts == (), "nothing should have been asked for"


def test_d_no_live_configuration_is_read(tmp_path, monkeypatch):
    """A4 takes its settings as a parameter. Point the env override at a path that cannot exist."""
    monkeypatch.setenv(ac.ENV_CONFIG_PATH, str(tmp_path / "nope" / "shared-config.json"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "also-nope"))
    with _OpenSpy() as spy:
        report = lcl.inspect_location(_local(tmp_path), _cfg(True, str(tmp_path / "s")))
    assert spy.calls == []
    assert report.configured_shared == str(tmp_path / "s")
    assert not os.path.exists(str(tmp_path / "nope"))
    assert not os.path.exists(str(tmp_path / "also-nope"))


def test_d_a_blank_selection_is_not_resolved_to_the_working_directory(tmp_path):
    """`os.path.abspath("")` answers the CWD -- a resolution that already cost one defect."""
    report = lcl.inspect_location("", _cfg(False, ""))
    assert report.local.verdict == lib.NOT_INSPECTED
    assert report.local.path == ""
    assert report.active is False
    assert report.status == lib.NOT_INSPECTED
    assert os.path.normcase(os.getcwd()) not in os.path.normcase(repr(report))


# =============================================================================================
# E. negative controls -- each weakens one safety property and must change an answer
# =============================================================================================

def test_e_m1_omitting_the_no_op_lets_the_resolver_create_the_folder(tmp_path):
    """Mutant 1: the explicit `make_dirs=refusal` removed. The folder appears."""
    local = _local(tmp_path)
    shared = str(tmp_path / "m1-shared")

    weak = _mutant(("    location = resolve_location(local_clean, snapshot, make_dirs=refusal, "
                    "is_dir=isdir)",
                    "    location = resolve_location(local_clean, snapshot, is_dir=isdir)"))
    assert not os.path.exists(shared)
    weak.inspect_location(local, _cfg(True, shared))
    created_by_mutant = os.path.isdir(shared)

    real_target = str(tmp_path / "m1-real")
    lcl.inspect_location(local, _cfg(True, real_target))
    created_by_real = os.path.isdir(real_target)

    assert created_by_mutant is True, "the mutation did not reproduce the creation hazard"
    assert created_by_real is False, "the real module created the folder"


def test_e_m2_trusting_resolver_available_publishes_a_missing_folder_as_active(tmp_path):
    """Mutant 2: `active` derived from the resolver instead of the independent probe."""
    local = _local(tmp_path)
    shared = str(tmp_path / "m2-shared")

    weak = _mutant(("        return bool(self.selected.path) and self.selected.verdict == PRESENT",
                    "        return bool(self.resolver_available)"))
    mutated = weak.inspect_location(local, _cfg(True, shared))
    real = lcl.inspect_location(local, _cfg(True, shared))

    assert mutated.resolver_available is real.resolver_available is True
    assert mutated.active is True, "the mutation did not apply"
    assert real.active is False
    assert real.status == mutated.status == lib.CONFIGURED_NOT_CREATED
    assert not os.path.exists(shared)


def test_e_m3_an_absent_cache_must_not_be_reported_as_an_unreachable_disk(tmp_path):
    """Mutant 3: the probe's verdict replaced by ROOT_UNREACHABLE, collapsing the distinction.

    `CONFIGURED_NOT_CREATED` versus `ROOT_UNREACHABLE` is the asymmetry the whole resolver design
    rests on: a new or mistyped folder on a live disk is not an absent disk.
    """
    local = _local(tmp_path)
    absent = str(tmp_path / "m3-absent")

    weak = _mutant(("        verdict=state.verdict,", "        verdict=ROOT_UNREACHABLE,"))
    mutated = weak.inspect_location(local, _cfg(True, absent))
    real = lcl.inspect_location(local, _cfg(True, absent))

    assert real.status == lib.CONFIGURED_NOT_CREATED
    assert mutated.status == lib.ROOT_UNREACHABLE, "the mutation did not apply"
    assert real.shared.anchor_reachable is True, "the parent IS live -- that is the distinction"
    assert real.active is False and mutated.active is False, "neither is active; the REASON differs"


def test_e_m4_unavailable_storage_must_not_become_an_empty_cache(tmp_path):
    """Mutant 4: an unreachable anchor promoted to PRESENT, which an inventory reads as empty."""
    letter = _absent_drive_letter()
    if letter is None:
        pytest.skip("no absent drive letter available on this machine")
    local = _local(tmp_path)
    cfg = _cfg(True, f"{letter}:\\m4")
    weak = _mutant(("        inspectable=state.verdict == PRESENT,",
                    "        inspectable=True,"))
    mutated = weak.inspect_location(local, cfg)
    real = lcl.inspect_location(local, cfg)
    assert real.shared.inspectable is False and real.active is False
    assert mutated.shared.inspectable is True
    assert real.shared.verdict == lib.ROOT_UNREACHABLE


def test_e_m5_an_unknown_root_must_not_be_promoted_to_active(tmp_path):
    """Mutant 5: `active` widened from PRESENT to 'anything positively observed or not refused'."""
    local = _local(tmp_path)
    denied = tmp_path / "m5-denied"
    denied.mkdir()
    fs = _CountingFS(deny=[str(denied)])
    weak = _mutant(("        return bool(self.selected.path) and self.selected.verdict == PRESENT",
                    "        return bool(self.selected.path) and self.selected.observed"))
    mutated = weak.inspect_location(local, _cfg(True, str(denied)), **fs.kwargs())
    real = lcl.inspect_location(local, _cfg(True, str(denied)),
                                **_CountingFS(deny=[str(denied)]).kwargs())
    assert real.selected.observed is True, "ACCESS_DENIED IS positively observed"
    assert real.active is False, "...and must still not be ACTIVE"
    assert mutated.active is True, "the mutation did not apply"


def test_e_m6_disabling_sharing_must_not_lose_the_configured_path(tmp_path):
    """Mutant 6: the stored path dropped when sharing is off."""
    local = _local(tmp_path)
    shared = tmp_path / "m6-shared"
    shared.mkdir()
    weak = _mutant(('    configured = _clean(snapshot.get(CACHE_FOLDER))',
                    '    configured = _clean(snapshot.get(CACHE_FOLDER)) '
                    'if _as_bool(snapshot.get(CACHE_ENABLED)) else ""'))
    mutated = weak.inspect_location(local, _cfg(False, str(shared)))
    real = lcl.inspect_location(local, _cfg(False, str(shared)))
    assert real.configured_shared == str(shared) and real.configured is True
    assert mutated.configured_shared == "" and mutated.configured is False
    assert lcl.SHARED_CAVEAT in real.caveats
    assert lcl.SHARED_CAVEAT not in mutated.caveats, "the ownership caveat vanished with the path"


def test_e_m7_a_fallback_candidate_must_not_be_called_the_shared_cache_in_use(tmp_path):
    """Mutant 7: `shared_in_use` reduced to 'the shared folder was requested'."""
    letter = _absent_drive_letter()
    if letter is None:
        pytest.skip("no absent drive letter available on this machine")
    local = _local(tmp_path)
    cfg = _cfg(True, f"{letter}:\\m7")
    weak = _mutant(("        return self.shared_selected and self.selected_active",
                    "        return self.shared_requested"))
    mutated = weak.inspect_location(local, cfg)
    real = lcl.inspect_location(local, cfg)
    assert real.shared_requested is True and real.fallback is True
    assert real.shared_in_use is False, "a fallback is not the shared cache in use"
    assert mutated.shared_in_use is True, "the mutation did not apply"
