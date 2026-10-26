r"""Adversarial security tests for `beatsync_fork.win32_transfer`.

These test **the implemented module**, not a research wrapper. Every protection case is paired with
a control that demonstrates the attack genuinely works without the protection: a test whose
adversarial operation fails even unprotected proves nothing, and is INVALID rather than PASS.

Fixtures are synthetic, disposable and created fresh per test. Set
`BEATSYNC_P69_FIXTURE_ROOT` to place them under a claimed workspace boundary (R5-P69 ran with it
pointed at its own `Scratch` boundary, as its evidence package records); otherwise pytest's own
tmp_path is used so the suite stays portable. No operator media is touched and no production
transfer path is exercised -- `win32_transfer` is imported by nothing in the product.
"""
from __future__ import annotations

import ast
import ctypes
import hashlib
import os
import struct
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from beatsync_fork import win32_transfer as w32  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="the Win32 primitive is Windows-only by construction")

_MODULE_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src", "beatsync_fork", "win32_transfer.py")

#: The eight entry points the owner authorized. Frozen here INDEPENDENTLY of the module, so a
#: change to the module's own tuple cannot silently redefine what "authorized" means.
EXPECTED_SURFACE = {
    "CreateFileW",
    "GetFileInformationByHandle",
    "GetFileInformationByHandleEx",
    "GetFinalPathNameByHandleW",
    "GetVolumeInformationByHandleW",
    "ReadFile",
    "SetFileInformationByHandle",
    "CloseHandle",
}

PAYLOAD = (b"R5P69-SYNTHETIC-FIXTURE" * 3000)[:65536]


@pytest.fixture()
def area(tmp_path, request):
    """A fresh synthetic area, preferring a claimed workspace boundary when one is configured."""
    root = os.environ.get("BEATSYNC_P69_FIXTURE_ROOT")
    if not root:
        return os.path.normpath(str(tmp_path))
    target = os.path.join(root, request.node.name.replace("[", "_").replace("]", ""))
    if os.path.isdir(target):
        import shutil
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    # normpath is load-bearing, not tidiness: `mklink` silently REFUSES forward slashes, so a
    # fixture root configured as "E:/..." would make every junction-based test skip itself with
    # INVALID_CONTROL -- quietly dropping the three most important cases in this file.
    return os.path.normpath(target)


def _write(path, payload=PAYLOAD):
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _file_id(path):
    """Identity of whatever is at this pathname right now, via a throwaway permissive handle."""
    handle = w32.open_destination_directory  # not used; kept explicit that we go through the module
    del handle
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    h = k32.CreateFileW(path, 0x80, 7, None, 3, 0, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        return None
    try:
        return w32._Handle(h).identity().file_id_128
    finally:
        pass  # _Handle.close() below releases it
    # unreachable


def _identity_at(path):
    """Identity at a pathname, using the module's own reader on a throwaway handle."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    h = k32.CreateFileW(path, 0x80, 7, None, 3, 0, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        return None
    wrapped = w32._Handle(h)
    try:
        return wrapped.identity()
    finally:
        wrapped.close()


def _stage_decoy(area):
    """Approved A in-root; byte-identical decoy B out-of-root; a stash for the adversary.

    B is byte-identical to A on purpose: a digest comparison then CANNOT be what distinguishes
    them, so only object identity can.
    """
    root = os.path.join(area, "input_root")
    outside = os.path.join(area, "OUTSIDE")
    stash = os.path.join(area, "STASH")
    library = os.path.join(area, "LIBRARY")
    for path in (root, outside, stash, library):
        os.makedirs(path, exist_ok=True)
    approved = _write(os.path.join(root, "clip0001.mp4"))
    decoy = _write(os.path.join(outside, "decoy.mp4"))
    a_id = _identity_at(approved).file_id_128
    b_id = _identity_at(decoy).file_id_128
    assert a_id != b_id, "fixture failure: A and B are the same object"
    with open(approved, "rb") as fh_a, open(decoy, "rb") as fh_b:
        assert fh_a.read() == fh_b.read(), "fixture failure: A and B are not byte-identical"
    return {"root": root, "outside": outside, "stash": stash, "library": library,
            "approved": approved, "decoy": decoy, "a_id": a_id, "b_id": b_id}


def _swap(scenario):
    """Adversary: move A aside and put B at the approved pathname. Verifies it actually staged."""
    moved = os.path.join(scenario["stash"], "A.mp4")
    os.rename(scenario["approved"], moved)
    os.rename(scenario["decoy"], scenario["approved"])
    scenario["a_now"] = moved
    return _identity_at(scenario["approved"]).file_id_128 == scenario["b_id"]


def _stage_junction_decoy(area):
    r"""D2-C staged through an ancestor JUNCTION rather than the leaf.

    This matters, and it is not a convenience. The module opens the source with protective sharing,
    which refuses a leaf rename outright (winerror 32 -- see test_05), so the classic
    "move A aside, drop B in its place" staging is *prevented* rather than survived. To exercise
    pure object binding under the module's real sharing mode, the pathname must be redirected by a
    route the pin does not cover: R5-P67 C1 measured that an ancestor junction inside the root
    stays re-pointable while a descendant handle is held, because the junction is a directory entry
    of its parent and not a real ancestor of the open object.

    Layout:  input_root\junc -> REAL  (A lives in REAL);  ELSEWHERE holds byte-identical B.
    """
    root = os.path.join(area, "input_root")
    real = os.path.join(area, "REAL")
    elsewhere = os.path.join(area, "ELSEWHERE")
    library = os.path.join(area, "LIBRARY")
    for path in (root, real, elsewhere, library):
        os.makedirs(path, exist_ok=True)
    approved_object = _write(os.path.join(real, "clip0001.mp4"))
    decoy_object = _write(os.path.join(elsewhere, "clip0001.mp4"))
    junction = os.path.join(root, "junc")
    if not _mklink_j(junction, real):
        pytest.skip("INVALID_CONTROL: a directory junction could not be created here")
    approved_path = os.path.join(junction, "clip0001.mp4")
    a_id = _identity_at(approved_object).file_id_128
    b_id = _identity_at(decoy_object).file_id_128
    assert a_id != b_id, "fixture failure: A and B are the same object"
    with open(approved_object, "rb") as fh_a, open(decoy_object, "rb") as fh_b:
        assert fh_a.read() == fh_b.read(), "fixture failure: A and B are not byte-identical"
    assert _identity_at(approved_path).file_id_128 == a_id
    return {"root": root, "real": real, "elsewhere": elsewhere, "library": library,
            "junction": junction, "approved_path": approved_path,
            "approved_object": approved_object, "decoy_object": decoy_object,
            "a_id": a_id, "b_id": b_id}


def _repoint(scenario):
    """Adversary: re-point the ancestor junction so the approved PATHNAME now names B."""
    os.rmdir(scenario["junction"])
    if not _mklink_j(scenario["junction"], scenario["elsewhere"]):
        return False
    return _identity_at(scenario["approved_path"]).file_id_128 == scenario["b_id"]


# =================================================================================================
# 1 + 2 -- the D2-C pair. Neither half means anything without the other.
# =================================================================================================

def test_01_pathname_rename_publishes_the_decoy_NEGATIVE_CONTROL(area):
    """The attack genuinely works against a pathname-addressed rename."""
    scenario = _stage_junction_decoy(area)
    assert _repoint(scenario), "INVALID_CONTROL: the junction re-point did not stage"
    published = os.path.join(scenario["library"], "published.mp4")
    os.rename(scenario["approved_path"], published)     # re-resolves the pathname right here
    assert _identity_at(published).file_id_128 == scenario["b_id"], (
        "control ineffective: the pathname rename did not publish the decoy")
    assert os.path.exists(scenario["approved_object"]), (
        "the approved object should have been left behind, never published")


def test_02_handle_bound_rename_publishes_the_approved_object_and_spares_the_decoy(area):
    """The same staged attack, against the module, under its real protective sharing."""
    scenario = _stage_junction_decoy(area)
    source = w32.open_source(scenario["approved_path"])
    try:
        assert source.identity().file_id_128 == scenario["a_id"]
        assert _repoint(scenario), "INVALID_CONTROL: the junction re-point did not stage"
        # the approved PATHNAME and the handle now name different objects
        assert _identity_at(scenario["approved_path"]).file_id_128 == scenario["b_id"]
        assert source.identity().file_id_128 == scenario["a_id"]
        with w32.open_destination_directory(scenario["library"]) as destination:
            source.rename_no_replace(destination.final_path() + "\\published.mp4")
        published = os.path.join(scenario["library"], "published.mp4")
        assert _identity_at(published).file_id_128 == scenario["a_id"], "published the wrong object"
    finally:
        source.close()
    # the decoy is untouched: same object, same bytes, still where the adversary put it
    assert _identity_at(scenario["decoy_object"]).file_id_128 == scenario["b_id"]
    with open(scenario["decoy_object"], "rb") as handle:
        assert handle.read() == PAYLOAD


# =================================================================================================
# 3 + 11 -- identity verification
# =================================================================================================

def test_03_source_identity_matches_the_approved_object(area):
    path = _write(os.path.join(area, "clip.mp4"))
    expected = _identity_at(path)
    with w32.open_source(path) as source:
        verified = source.verify_identity(expected)
        assert verified.matches(expected)
        assert verified.file_id_128 == expected.file_id_128
        assert verified.volume_serial == expected.volume_serial


def test_11_a_stale_or_incorrect_source_identity_refuses(area):
    scenario = _stage_decoy(area)
    approved_identity = _identity_at(scenario["approved"])
    assert _swap(scenario), "INVALID_CONTROL: the swap did not stage"
    # the transfer acquires its own handle AFTER the swap and revalidates against the stale record
    with w32.open_source(scenario["approved"]) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.verify_identity(approved_identity)
    assert caught.value.code == "identity_mismatch"
    assert caught.value.code in w32.REFUSAL_CODES


# =================================================================================================
# 4 + 5 -- the ordering pair. This is the security property.
# =================================================================================================

def test_04_inverted_ordering_binds_the_decoy_NEGATIVE_CONTROL(area):
    """Validate first, acquire after: the handle binds the wrong object and binds it perfectly."""
    scenario = _stage_decoy(area)
    # "containment validated" with no handle held -- the adversary is free
    assert os.path.realpath(scenario["approved"]).startswith(os.path.realpath(scenario["root"]))
    assert _swap(scenario), "INVALID_CONTROL: the swap did not stage"
    with w32.open_source(scenario["approved"]) as source:      # acquired AFTER the check
        bound = source.identity()
    assert bound.file_id_128 == scenario["b_id"], (
        "control ineffective: the late-acquired handle did not bind the decoy")


def test_05_correct_ordering_survives_the_same_adversarial_setup(area):
    """Acquire first, and the same adversary cannot even stage the swap."""
    scenario = _stage_decoy(area)
    with w32.open_source(scenario["approved"]) as source:
        bound = source.identity()
        assert bound.file_id_128 == scenario["a_id"]
        own_name = source.final_path()
        with pytest.raises(OSError) as caught:
            _swap(scenario)
        assert getattr(caught.value, "winerror", None) == 32, (
            "the protective share mode should refuse the leaf rename with a sharing violation")
        # identity and own name are unchanged by the failed attack
        assert source.identity().file_id_128 == scenario["a_id"]
        assert source.final_path() == own_name


# =================================================================================================
# 6 -- hashing from the handle
# =================================================================================================

def test_06_content_hash_comes_from_the_opened_handle(area):
    path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(path) as source:
        digest, total = source.sha256_stream()
        assert digest == hashlib.sha256(PAYLOAD).hexdigest()
        assert total == len(PAYLOAD)
        # no SetFilePointerEx in the authorized surface, so a second pass cannot rewind:
        # refused rather than silently returning the digest of an empty stream
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream()
        assert caught.value.code == "already_streamed"


# =================================================================================================
# 7 -- collision
# =================================================================================================

def test_07_existing_destination_is_refused_with_no_overwrite(area):
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    occupant = _write(os.path.join(library, "published.mp4"), b"OCCUPANT" * 100)
    occupant_identity = _identity_at(occupant)
    with open(occupant, "rb") as handle:
        occupant_bytes = handle.read()

    with w32.open_source(source_path) as source:
        with w32.open_destination_directory(library) as destination:
            with pytest.raises(w32.Win32Refused) as caught:
                source.rename_no_replace(destination.final_path() + "\\published.mp4")
    assert caught.value.code == "destination_exists"
    assert caught.value.winerror == 183
    # the occupant kept its identity AND its bytes; the source is still where it was
    assert _identity_at(occupant).file_id_128 == occupant_identity.file_id_128
    with open(occupant, "rb") as handle:
        assert handle.read() == occupant_bytes
    assert os.path.exists(source_path)


def test_07b_replace_if_exists_is_not_reachable_through_the_api():
    """`ReplaceIfExists` is hard-wired, not a parameter."""
    import inspect

    signature = inspect.signature(w32.SourceHandle.rename_no_replace)
    assert set(signature.parameters) == {"self", "destination_path"}, (
        "rename_no_replace must expose no flag that could enable replacement")
    source = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    for node in ast.walk(source):
        if isinstance(node, ast.FunctionDef) and node.name == "rename_no_replace":
            assigned = [n for n in ast.walk(node)
                        if isinstance(n, ast.Constant) and n.value == b"\x01"]
            assert assigned == [], "a TRUE ReplaceIfExists byte must never appear"


# =================================================================================================
# 8 -- protective sharing
# =================================================================================================

def test_08_protective_sharing_prevents_the_external_writer(area):
    """A cross-process writer is refused on the pinned object, and succeeds on an unpinned twin."""
    pinned = _write(os.path.join(area, "pinned.mp4"))
    unpinned = _write(os.path.join(area, "unpinned.mp4"))
    writer = (
        "import sys, json\n"
        "out = {}\n"
        "for tag, path in (('pinned', sys.argv[1]), ('unpinned', sys.argv[2])):\n"
        "    try:\n"
        "        fh = open(path, 'r+b'); fh.seek(0); fh.write(b'TAMPERED'); fh.close()\n"
        "        out[tag] = 'SUCCEEDED'\n"
        "    except OSError as exc:\n"
        "        out[tag] = 'refused errno=%s' % exc.errno\n"
        "print(json.dumps(out))\n")
    with w32.open_source(pinned) as source:
        digest_before, _ = source.sha256_stream()
        completed = subprocess.run([sys.executable, "-c", writer, pinned, unpinned],
                                   capture_output=True, text=True, timeout=120)
    import json as _json
    result = _json.loads(completed.stdout.strip())
    assert result["unpinned"] == "SUCCEEDED", (
        "INVALID_CONTROL: the writer failed even unpinned, so the refusal proves nothing")
    assert result["pinned"].startswith("refused")
    with w32.open_source(pinned) as source:
        assert source.sha256_stream()[0] == digest_before


# =================================================================================================
# 9 -- destination pinning
# =================================================================================================

def _mklink_j(link, target):
    """Create an NTFS directory junction. Paths are normalised because mklink refuses '/'."""
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.path.normpath(link), os.path.normpath(target)],
        capture_output=True, text=True)
    return completed.returncode == 0 and os.path.isdir(link)


def test_09_destination_pinning_blocks_the_measured_substitution(area):
    """Committing to the destination handle's RESOLVED name defeats junction substitution."""
    dest_root = os.path.join(area, "DEST_ROOT")
    real = os.path.join(area, "REAL_LIB")
    rogue = os.path.join(area, "ROGUE_LIB")
    for path in (dest_root, real, rogue):
        os.makedirs(path, exist_ok=True)
    junction = os.path.join(dest_root, "clips")
    if not _mklink_j(junction, real):
        pytest.skip("INVALID_CONTROL: a directory junction could not be created here")
    source_path = _write(os.path.join(area, "clip.mp4"))

    with w32.open_source(source_path) as source:
        with w32.open_destination_directory(junction) as destination:
            resolved = destination.final_path()
            os.rmdir(junction)
            assert _mklink_j(junction, rogue), "INVALID_CONTROL: the junction was not re-pointed"
            source.rename_no_replace(resolved + "\\published.mp4")

    assert os.path.exists(os.path.join(real, "published.mp4")), "did not land in the verified dir"
    assert not os.path.exists(os.path.join(rogue, "published.mp4")), "landed in the rogue target"


def test_09b_a_pinned_destination_cannot_be_relocated(area):
    dest_root = os.path.join(area, "DEST_ROOT")
    library = os.path.join(dest_root, "clips")
    outside = os.path.join(area, "OUTSIDE")
    for path in (library, outside):
        os.makedirs(path, exist_ok=True)
    with w32.open_destination_directory(library) as destination:
        with pytest.raises(OSError) as caught:
            os.rename(library, os.path.join(outside, "clips"))
        assert getattr(caught.value, "winerror", None) == 32
        assert destination.final_path().endswith("clips")


# =================================================================================================
# 10 + 13 -- handle information across a rename, and link-count observability
# =================================================================================================

def test_10_handle_information_is_consistent_across_a_successful_rename(area):
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(source_path) as source:
        before = source.identity()
        name_before = source.final_path()
        with w32.open_destination_directory(library) as destination:
            target = destination.final_path() + "\\published.mp4"
            source.rename_no_replace(target)
        after = source.identity()
        name_after = source.final_path()
    assert after.matches(before), "the handle must still name the same object"
    assert after.file_id_128 == before.file_id_128
    assert after.size == before.size
    assert name_after != name_before, "the object's own name must have changed"
    assert name_after.endswith("published.mp4")
    assert not os.path.exists(source_path)


def test_13_link_count_changes_are_observable_but_not_enforced(area):
    """The count is reported live. The module must NOT make an H1/H2 policy decision."""
    import ctypes
    source_path = _write(os.path.join(area, "clip.mp4"))
    alias = os.path.join(area, "alias.mp4")
    with w32.open_source(source_path) as source:
        assert source.identity().link_count == 1
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateHardLinkW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
        assert k32.CreateHardLinkW(alias, source_path, None), (
            "INVALID_CONTROL: the extra hard link was not created, so nothing is being observed")
        # live on the already-open handle
        assert source.identity().link_count == 2
        # and the module still permits every operation: policy is deferred, not decided here
        source.verify_identity(source.identity())
        digest, _ = source.sha256_stream()
        assert len(digest) == 64


# =================================================================================================
# 12 -- filesystem qualification
# =================================================================================================

def test_12a_a_local_ntfs_source_qualifies(area):
    source_path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(source_path) as source:
        qualification = source.qualify_local_ntfs()
    assert qualification.filesystem.upper() == "NTFS"
    assert qualification.dos_path.upper().startswith("\\\\?\\")
    assert "\\\\?\\UNC\\" not in qualification.dos_path.upper()
    assert qualification.guid_path.upper().startswith("\\\\?\\VOLUME{")
    assert qualification.nt_path.upper().startswith("\\DEVICE\\HARDDISKVOLUME")


def test_12b_a_remote_smb_handle_is_refused(area):
    """An SMB handle reports fs=NTFS and the IDENTICAL volume serial, and must still be refused."""
    source_path = _write(os.path.join(area, "clip.mp4"))
    drive, tail = os.path.splitdrive(os.path.abspath(source_path))
    unc = "\\\\127.0.0.1\\%s$%s" % (drive[0], tail)
    try:
        handle = w32.open_destination_directory  # keep the module in the call graph
        del handle
        remote = _open_raw_readonly(unc)
    except OSError:
        remote = None
    if remote is None:
        pytest.skip("no SMB loopback target is reachable read-only on this machine")
    try:
        # the deceptive part, asserted so the test documents WHY the name is insufficient
        filesystem, serial, _flags = remote._volume_information()
        assert filesystem.upper() == "NTFS"
        with w32.open_source(source_path) as local:
            assert local._volume_information()[1] == serial, (
                "INVALID_CONTROL: the SMB target is a different volume, so this proves nothing")
        with pytest.raises(w32.Win32Refused) as caught:
            remote.qualify_local_ntfs()
        # [R5-P71 / F70-7] The EXACT code, not a disjunction. R5-P70's point was that a four-code
        # disjunction pins code identity rather than the security property: removing a condition
        # changed which code was raised while the handle stayed refused, so the test still passed.
        # Condition 5 is evaluated first, so a genuinely remote handle must refuse as volume_remote.
        assert caught.value.code == "volume_remote", (
            f"expected the remote veto to fire first, got {caught.value.code}")

        # And the other conditions are shown to refuse it INDEPENDENTLY, by asserting the handle's
        # own measured characteristics rather than by relying on evaluation order. The decisive
        # per-condition controls are the injected ones in test_19b, which cannot skip.
        assert remote.final_path(w32.VOLUME_NAME_DOS).upper().startswith("\\\\?\\UNC\\"), (
            "condition 1 would not independently refuse this handle")
        with pytest.raises(w32.Win32Refused):
            remote.final_path(w32.VOLUME_NAME_GUID)          # condition 2: no local volume GUID
        assert not w32._is_local_device_path(remote.final_path(w32.VOLUME_NAME_NT)), (
            "condition 3 would not independently refuse this handle")
        assert (_flags & w32.REQUIRED_VOLUME_FLAGS) != w32.REQUIRED_VOLUME_FLAGS, (
            "condition 4b would not independently refuse this handle")
    finally:
        remote.close()


def _open_raw_readonly(path):
    """A read-only handle for comparison targets. Never writes."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    handle = k32.CreateFileW(path, 0x80, 7, None, 3, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None
    return w32._Handle(handle)


def test_12c_a_non_ntfs_volume_is_refused():
    """Scan for any mounted non-NTFS volume and prove the gate refuses it."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetLogicalDriveStringsW.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
    k32.GetLogicalDriveStringsW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(1024)
    length = k32.GetLogicalDriveStringsW(1024, buffer)
    candidates = [d for d in buffer[:length].split("\x00") if d]
    refused = []
    for drive in candidates:
        handle = _open_raw_readonly_dir(drive)
        if handle is None:
            continue
        try:
            filesystem = handle._volume_information()[0]
            if filesystem.upper() == "NTFS":
                continue
            with pytest.raises(w32.Win32Refused) as caught:
                handle.qualify_local_ntfs()
            refused.append((drive, filesystem, caught.value.code))
        except w32.Win32Refused:
            continue
        finally:
            handle.close()
    if not refused:
        pytest.skip("no non-NTFS volume is mounted on this machine to refuse")
    for _drive, _filesystem, code in refused:
        assert code == "volume_not_ntfs", f"expected volume_not_ntfs, got {code}"


def _open_raw_readonly_dir(path):
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = ctypes.c_void_p
    k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                ctypes.c_void_p]
    handle = k32.CreateFileW(path, 0x80, 7, None, 3, 0x02000000, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None
    return w32._Handle(handle)


def test_12d_cross_volume_is_refused_without_using_the_serial_as_proof_of_locality(area):
    """Same-volume is required, and serial equality is a same-volume test only."""
    source_path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(source_path) as source:
        qualification = source.qualify_local_ntfs()
    forged = w32.VolumeQualification(
        filesystem="NTFS", volume_serial=qualification.volume_serial ^ 0xFFFF,
        volume_flags=qualification.volume_flags, dos_path=qualification.dos_path,
        guid_path=qualification.guid_path, nt_path=qualification.nt_path)
    with pytest.raises(w32.Win32Refused) as caught:
        w32.require_same_volume(qualification, forged)
    assert caught.value.code == "cross_volume_not_supported"
    w32.require_same_volume(qualification, qualification)      # the matching case is permitted


# =================================================================================================
# 14 -- an unexpected Win32 failure cannot look like success
# =================================================================================================

def test_14_an_unexpected_win32_failure_cannot_produce_false_success(area, monkeypatch):
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    real_bind = w32._bind

    def failing_bind(name):
        if name == "SetFileInformationByHandle":
            def always_fails(*_args, **_kwargs):
                import ctypes
                ctypes.set_last_error(1460)        # ERROR_TIMEOUT: not a code the module maps
                return 0
            always_fails.restype = None
            return always_fails
        return real_bind(name)

    monkeypatch.setattr(w32, "_bind", failing_bind)
    with w32.open_source(source_path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.rename_no_replace(os.path.join(library, "published.mp4"))
    assert caught.value.code == "rename_failed"
    assert caught.value.winerror == 1460
    assert caught.value.code in w32.REFUSAL_CODES
    assert os.path.exists(source_path), "nothing may be published when the commit failed"
    assert not os.path.exists(os.path.join(library, "published.mp4"))


def test_14b_every_refusal_code_the_module_raises_is_declared():
    """A refusal code absent from REFUSAL_CODES would break a caller's exhaustive branching."""
    source = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    raised = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Win32Refused":
            if node.args and isinstance(node.args[0], ast.Constant):
                raised.add(node.args[0].value)
    assert raised, "the AST scan found no refusals, so this test would be vacuous"
    assert raised <= w32.REFUSAL_CODES, f"undeclared refusal codes: {sorted(raised - w32.REFUSAL_CODES)}"


# =================================================================================================
# 15 -- handle release on every path
# =================================================================================================

def test_15_every_handle_is_released_on_all_failure_paths(area):
    """After any refusal, an ordinary delete of the source must succeed.

    A leaked handle holds a pin that makes the file unreadable to the preview and undeletable to the
    operator, so this is a user-visible defect rather than a resource nicety.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)

    # (a) refusal inside a with-block
    path = _write(os.path.join(area, "a.mp4"))
    _write(os.path.join(library, "taken.mp4"), b"X" * 10)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused):
            source.rename_no_replace(os.path.join(library, "taken.mp4"))
    os.remove(path)

    # (b) an exception escaping the with-block
    path = _write(os.path.join(area, "b.mp4"))
    with pytest.raises(RuntimeError):
        with w32.open_source(path) as source:
            source.identity()
            raise RuntimeError("boom")
    os.remove(path)

    # (c) explicit close, twice, plus use-after-close
    path = _write(os.path.join(area, "c.mp4"))
    source = w32.open_source(path)
    source.close()
    source.close()
    assert source.closed
    with pytest.raises(w32.Win32Refused) as caught:
        source.identity()
    assert caught.value.code == "handle_closed"
    os.remove(path)

    # (d) destination handle released after a refusal
    path = _write(os.path.join(area, "d.mp4"))
    destination = w32.open_destination_directory(library)
    destination.close()
    os.remove(path)
    import shutil
    shutil.rmtree(library)      # would raise if a directory handle leaked


def test_15b_a_failed_acquisition_leaks_nothing_and_refuses(area):
    missing = os.path.join(area, "does_not_exist.mp4")
    with pytest.raises(w32.Win32Refused) as caught:
        w32.open_source(missing)
    assert caught.value.code == "source_unavailable"
    assert caught.value.winerror in (2, 3)

    # a sharing conflict is a refusal, never a retry by another route
    path = _write(os.path.join(area, "held.mp4"))
    with w32.open_source(path):
        with pytest.raises(w32.Win32Refused) as caught:
            w32.open_source(path)
    assert caught.value.code == "source_shared_violation"
    assert caught.value.winerror == 32
    os.remove(path)


# =================================================================================================
# 16 -- the executable guard over the FFI surface
# =================================================================================================

#: [R5-P71 / F70-3] Names that evaluate to the live kernel32 `WinDLL`.
#:
#: `_library` was R5-P69's module-level accessor and R5-P71 removed it; it stays listed so that
#: reintroducing it is caught rather than silently permitted. `_k32` is the module global the loader
#: caches into, and is the only remaining reference to the library anywhere in the module.
_RAW_LIBRARY_ACCESSORS = frozenset({"_library"})
_RAW_LIBRARY_GLOBALS = frozenset({"_k32"})


def _raw_library_aliases(tree):
    """Local names bound to the raw library, e.g. `lib = _library()` or `lib = _k32`.

    Without this, `_library()` could be stashed in a local and the attribute access would be on an
    innocuous-looking name. Required by the task's "aliased library references" clause.

    Iterated to a fixed point so a chain (`a = _k32` then `b = a`) is caught regardless of the
    order `ast.walk` happens to visit the two assignments in.
    """
    aliases = set()
    while True:
        discovered = set(aliases)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and _names_the_raw_library(node.value, discovered):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        discovered.add(target.id)
        if discovered == aliases:
            return aliases
        aliases = discovered


def _names_the_raw_library(node, aliases=frozenset()):
    """True if this expression evaluates to the live kernel32 library object."""
    if isinstance(node, ast.Call) and getattr(node.func, "id", None) in _RAW_LIBRARY_ACCESSORS:
        return True
    if isinstance(node, ast.Name):
        return node.id in _RAW_LIBRARY_GLOBALS or node.id in aliases
    return False


def _kernel32_attribute_names(tree, chokepoint="_bind"):
    """Every literal entry point resolved through the module's `_bind`, plus any bypass of it.

    `_bind`'s own body is exempt: it is the single gated chokepoint and it validates its argument
    against `AUTHORIZED_ENTRY_POINTS` *before* resolving, so its `getattr(library, name)` is the
    guard rather than a bypass of it. Every OTHER route to the library is a bypass.

    [R5-P71 / F70-3] R5-P70 measured that the R5-P69 form of this scan flagged a `getattr` only
    when the *name* was computed, so `getattr(_library(), "DeleteFileW")` (M12) passed, and direct
    attribute access `_library().DeleteFileW` (M12b) was not examined at all. Both planted
    mutations produced a fully green suite. Four routes are now flagged:

      1. `_bind` called with a non-literal argument (as before);
      2. `getattr` with a computed name outside `_bind` (as before);
      3. `getattr` whose FIRST argument is the raw library, **whatever the name's literalness** —
         this is M12;
      4. direct attribute access on the raw library — this is M12b.

    Routes 3 and 4 both honour `_raw_library_aliases`, so stashing the library in a local does not
    evade them.
    """
    chokepoint_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == chokepoint:
            chokepoint_nodes.update(id(inner) for inner in ast.walk(node))

    aliases = _raw_library_aliases(tree)
    literal, dynamic = set(), []
    for node in ast.walk(tree):
        if id(node) in chokepoint_nodes:
            continue
        if isinstance(node, ast.Attribute):
            # route 4 -- M12b
            if _names_the_raw_library(node.value, aliases):
                dynamic.append(f"direct attribute access on the raw library: .{node.attr}")
            continue
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None)
        if name == chokepoint:
            if node.args and isinstance(node.args[0], ast.Constant):
                literal.add(node.args[0].value)
            else:
                dynamic.append("non-literal _bind argument")
        elif name == "getattr":
            # route 3 -- M12. Checked BEFORE the literalness rule, because a literal name is
            # exactly what made M12 invisible.
            if node.args and _names_the_raw_library(node.args[0], aliases):
                requested = (node.args[1].value
                             if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                             else "<computed>")
                dynamic.append(f"getattr on the raw library outside _bind: {requested!r}")
            elif len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                # route 2
                dynamic.append("getattr with a computed name")
    return literal, dynamic


def test_16a_the_module_binds_only_the_authorized_surface():
    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    literal, dynamic = _kernel32_attribute_names(tree)
    assert literal, "the scan found no bindings, so this test would be vacuous"
    assert set(w32.AUTHORIZED_ENTRY_POINTS) == EXPECTED_SURFACE, (
        "the module's declared surface drifted from the owner-authorized eight")
    assert len(w32.AUTHORIZED_ENTRY_POINTS) == 8
    assert literal <= EXPECTED_SURFACE, f"unauthorized bindings: {sorted(literal - EXPECTED_SURFACE)}"
    assert dynamic == [], f"dynamic API lookup would bypass the guard: {dynamic}"


def test_16b_an_unlisted_kernel32_function_is_refused_at_runtime():
    for forbidden in ("DeleteFileW", "RemoveDirectoryW", "MoveFileExW", "CreateHardLinkW",
                      "SetFilePointerEx", "NtSetInformationFile"):
        with pytest.raises(AssertionError) as caught:
            w32._bind(forbidden)
        assert forbidden in str(caught.value)


def test_16c_the_surface_check_is_not_vacuous_PLANTED_CONTROL():
    """A planted forbidden binding must be caught by the same scan used on the real module."""
    planted = ast.parse("def f():\n    return _bind('DeleteFileW')\n")
    literal, _ = _kernel32_attribute_names(planted)
    assert literal == {"DeleteFileW"}
    assert not literal <= EXPECTED_SURFACE, "the scan failed to flag a planted forbidden binding"

    planted_dynamic = ast.parse("def f(name):\n    return _bind(name)\n")
    _literal, dynamic = _kernel32_attribute_names(planted_dynamic)
    assert dynamic, "the scan failed to flag a dynamic lookup"

    planted_getattr = ast.parse("def f(dll, name):\n    return getattr(dll, name)\n")
    _literal2, dynamic2 = _kernel32_attribute_names(planted_getattr)
    assert dynamic2, "the scan failed to flag getattr with a computed name"


#: [R5-P71 / F70-5] FILE_INFO_BY_HANDLE_CLASS values this module must never declare.
#:
#:   4  FileDispositionInfo     -- deletion
#:  21  FileDispositionInfoEx   -- deletion, with POSIX semantics
#:  22  FileRenameInfoEx        -- rename with a `Flags` union carrying
#:                                 FILE_RENAME_FLAG_REPLACE_IF_EXISTS (0x1), i.e. a SECOND route to
#:                                 replacement that sidesteps the hard-wired ReplaceIfExists byte
#:                                 test_07b guards.
#:
#: R5-P70 measured mutation M14 (`_FileRenameInfoEx = 22`) passing the entire suite: the R5-P69
#: form of this test banned 4 and 21 numerically and the two disposition classes by name, and 22 was
#: covered by neither.
_BANNED_INFORMATION_CLASSES = {
    4: "FileDispositionInfo",
    21: "FileDispositionInfoEx",
    22: "FileRenameInfoEx",
}


def _information_classes_passed_to(tree, entry_point):
    """Every information-class argument the module passes to one kernel32 entry point.

    Resolved by dataflow rather than by reading: find the local names bound from
    `_bind("<entry_point>")` inside each function, then collect argument 1 of every call to those
    names. Returns a list of `ast.expr` so the caller can require a specific identifier.
    """
    passed = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        bound = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Call):
                call = inner.value
                if (getattr(call.func, "id", None) == "_bind"
                        and call.args and isinstance(call.args[0], ast.Constant)
                        and call.args[0].value == entry_point):
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            bound.add(target.id)
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "id", None) in bound
                    and len(inner.args) >= 2):
                passed.append(inner.args[1])
    return passed


def test_16d_no_disposition_or_replacement_information_class_is_used():
    """Deletion and alternate-rename information classes must never appear. Checked numerically."""
    source_text = open(_MODULE_SOURCE_PATH, encoding="utf-8").read()
    tree = ast.parse(source_text)
    classes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "_bind":
                continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_File"):
                    if isinstance(node.value, ast.Constant):
                        classes.add((target.id, node.value.value))
    assert ("_FileRenameInfo", 3) in classes
    numbers = {value for _name, value in classes}
    for banned_value, banned_name in _BANNED_INFORMATION_CLASSES.items():
        assert banned_value not in numbers, (
            f"{banned_name} ({banned_value}) must never appear as an information class")
    for banned in ("FileDispositionInfo", "FileDispositionInfoEx", "FileRenameInfoEx"):
        assert f"_{banned} =" not in source_text


def test_16d2_the_only_class_passed_to_SetFileInformationByHandle_is_FileRenameInfo():
    """[R5-P71 / F70-5] Banning the constants is not enough; pin what is actually PASSED.

    A banned constant can be reintroduced under a different identifier, or an integer literal can be
    passed inline. This asserts the stronger property: every call to the bound
    `SetFileInformationByHandle` passes the identifier `_FileRenameInfo`, and that identifier is 3.
    """
    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    passed = _information_classes_passed_to(tree, "SetFileInformationByHandle")
    assert passed, "no SetFileInformationByHandle call site was found, so this test would be vacuous"
    for argument in passed:
        assert isinstance(argument, ast.Name), (
            "the information class must be the named constant, not an inline literal or expression")
        assert argument.id == "_FileRenameInfo", (
            f"SetFileInformationByHandle was passed {argument.id!r}; only _FileRenameInfo is allowed")
    assert w32._FileRenameInfo == 3

    # The same dataflow check over GetFileInformationByHandleEx, whose two documented classes are
    # the only other information classes this module uses.
    queried = _information_classes_passed_to(tree, "GetFileInformationByHandleEx")
    assert queried, "no GetFileInformationByHandleEx call site was found"
    for argument in queried:
        assert isinstance(argument, ast.Name) and argument.id in {
            "_FileIdInfo", "_FileRemoteProtocolInfo"}, (
            "only FileIdInfo and FileRemoteProtocolInfo may be queried")


def test_16d3_the_information_class_guard_catches_M14_PLANTED_CONTROL():
    """[R5-P71 / F70-5] M14 and an inline-literal variant must both be flagged."""
    # M14, verbatim from R5-P70's mutation matrix: the constant is declared.
    planted = ast.parse("_FileRenameInfo = 3\n_FileRenameInfoEx = 22\n")
    declared = set()
    for node in ast.walk(planted):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith("_File"):
                    if isinstance(node.value, ast.Constant):
                        declared.add(node.value.value)
    assert 22 in declared, "the planted M14 constant was not seen at all"
    assert any(value in _BANNED_INFORMATION_CLASSES for value in declared), (
        "the numeric ban failed to cover the planted FileRenameInfoEx class")

    # And the stronger dataflow check: a call site passing something other than _FileRenameInfo.
    planted_call = ast.parse(
        "def f(self, buf, size):\n"
        '    fn = _bind("SetFileInformationByHandle")\n'
        "    return fn(self._raw(), _FileRenameInfoEx, buf, size)\n")
    passed = _information_classes_passed_to(planted_call, "SetFileInformationByHandle")
    assert passed, "the dataflow scan found no planted call site"
    assert not all(isinstance(a, ast.Name) and a.id == "_FileRenameInfo" for a in passed), (
        "the dataflow scan failed to notice a non-FileRenameInfo information class")

    planted_literal = ast.parse(
        "def f(self, buf, size):\n"
        '    fn = _bind("SetFileInformationByHandle")\n'
        "    return fn(self._raw(), 22, buf, size)\n")
    passed_literal = _information_classes_passed_to(planted_literal, "SetFileInformationByHandle")
    assert passed_literal and not isinstance(passed_literal[0], ast.Name), (
        "the dataflow scan failed to notice an inline integer information class")


def test_16e_the_module_declares_no_second_ffi_holder():
    """Only one fork module may import ctypes, and this suite names the same one the guard does.

    Loaded by path rather than by `from tests import ...`: an unrelated installed `tests` package
    shadows the repository's directory on this interpreter, and importing the wrong one would make
    the assertion meaningless rather than failing loudly.
    """
    import importlib.util

    guard_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "test_no_runtime_dependency.py")
    spec = importlib.util.spec_from_file_location("_p69_guard_probe", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    assert guard.FFI_EXCEPTION_MODULE == "win32_transfer"
    assert guard.FFI_EXCEPTION_IMPORT == "ctypes"
    # and the exception is genuinely scoped: ctypes must not be in the shared allowlist
    source = ast.parse(open(guard_path, encoding="utf-8").read())
    shared_allowlist = set()
    for node in ast.walk(source):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "allowed":
                    if isinstance(node.value, ast.Set):
                        shared_allowlist = {e.value for e in node.value.elts
                                            if isinstance(e, ast.Constant)}
    assert shared_allowlist, "the shared allowlist was not found, so this test would be vacuous"
    assert "ctypes" not in shared_allowlist, (
        "ctypes leaked into the SHARED allowlist; the exception must stay module-scoped")


def test_16c2_the_bind_bypass_routes_are_caught_PLANTED_CONTROL():
    """[R5-P71 / F70-3] M12 and M12b must each be flagged by the same scan used on the real module.

    These are the two mutations R5-P70 planted and measured passing with 6/28 green. They are
    reproduced verbatim from its mutation matrix, then widened to the aliased spellings.
    """
    # M12 -- literal name, bypassing _bind entirely.
    m12 = ast.parse(
        "def _delete(path):\n"
        '    return getattr(_library(), "DeleteFileW")(path)\n')
    _literal, dynamic = _kernel32_attribute_names(m12)
    assert dynamic, "the scan failed to flag M12: getattr on the raw library with a literal name"
    assert any("raw library" in item for item in dynamic), dynamic

    # M12b -- direct attribute access on the library.
    m12b = ast.parse(
        "def _delete(path):\n"
        "    return _library().DeleteFileW(path)\n")
    _literal, dynamic = _kernel32_attribute_names(m12b)
    assert dynamic, "the scan failed to flag M12b: direct attribute access on the raw library"
    assert any("direct attribute access" in item for item in dynamic), dynamic

    # The same two routes reached through the surviving module global rather than the removed
    # accessor, since `_k32` is what actually holds the library now.
    for label, source in {
        "getattr_on_k32": 'def f():\n    return getattr(_k32, "DeleteFileW")\n',
        "attribute_on_k32": "def f():\n    return _k32.DeleteFileW\n",
        "aliased_accessor": 'def f():\n    lib = _library()\n    return lib.DeleteFileW\n',
        "aliased_global": 'def f():\n    lib = _k32\n    return getattr(lib, "DeleteFileW")\n',
        "alias_chain": "def f():\n    a = _k32\n    b = a\n    return b.DeleteFileW\n",
    }.items():
        _literal, dynamic = _kernel32_attribute_names(ast.parse(source))
        assert dynamic, f"the scan failed to flag {label}"

    # And the real module must be clean by this stricter scan -- asserted here as well as in
    # test_16a so the planted control and the live assertion cannot drift apart.
    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    _literal, dynamic = _kernel32_attribute_names(tree)
    assert dynamic == [], f"the module itself bypasses _bind: {dynamic}"


def test_16g_the_module_exposes_no_accessor_to_the_raw_library():
    """[R5-P71 / F70-4] The capability must not be importable at all.

    R5-P69 exposed `_library()`, a module-level accessor returning the live `WinDLL`. R5-P70
    measured a planted fork module reaching the whole of kernel32 through it with no `ctypes`
    import and no `_bind`. The accessor is gone; this pins its absence, because re-adding it is a
    one-line change that would restore the gap silently.
    """
    assert not hasattr(w32, "_library"), (
        "a module-level accessor to the live kernel32 library must not exist; the loader belongs "
        "inside _bind, after the authorization check")

    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    loader_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in {"WinDLL", "OleDLL", "CDLL", "PyDLL", "LibraryLoader"}:
                loader_sites.append(name)
    # The module loads the library through a `loader` local obtained by getattr(ctypes, "WinDLL"),
    # so a direct `ctypes.WinDLL(...)` call site should not exist either.
    assert loader_sites == [], f"unexpected direct library-loader call sites: {loader_sites}"

    # Exactly one function may resolve the library, and it is the chokepoint.
    resolving = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Global) and "_k32" in inner.names:
                    resolving.append(node.name)
    assert resolving == ["_bind"], (
        f"only _bind may bind the library global; found {resolving}")


def test_16h_the_public_api_declaration_matches_what_the_module_exports():
    """[R5-P71 / F70-4] PUBLIC_API must name real, public attributes.

    The package-level guard in `test_no_runtime_dependency.py` is only as good as this tuple: a
    typo would silently forbid a legitimate import, and a private name listed here would re-open
    F70-4. Both directions are checked.
    """
    assert w32.PUBLIC_API, "PUBLIC_API must not be empty"
    assert len(set(w32.PUBLIC_API)) == len(w32.PUBLIC_API), "PUBLIC_API has duplicates"
    for name in w32.PUBLIC_API:
        assert hasattr(w32, name), f"PUBLIC_API names {name!r}, which the module does not define"
        assert not name.startswith("_"), (
            f"PUBLIC_API must not carry a private name: {name!r}")
    for private in ("_library", "_k32", "_bind", "_SIGNATURES", "_Handle",
                    "_encoded_destination_name", "_create_file"):
        assert private not in w32.PUBLIC_API, (
            f"{private!r} is internal and must not be part of the approved interface")


# =================================================================================================
# 17 -- [R5-P71 / F70-1] the destination name is serialized at its true UTF-16 length
# =================================================================================================

class _FILE_RENAME_INFO_REFERENCE(ctypes.Structure):
    """An INDEPENDENT reference layout, declared from the documented Windows SDK structure.

    Deliberately not derived from the module's own buffer arithmetic. R5-P70 found F70-1 precisely
    because it built this separately and diffed the bytes: a rename that *works* proves nothing
    about buffer shape, since the kernel happily accepts a short `FileNameLength` and publishes the
    truncated name. Letting ctypes compute the offsets is the point -- the expectation must not be
    hand-arithmetic that happens to agree with the implementation's hand-arithmetic.

    winbase.h, x64:

        typedef struct _FILE_RENAME_INFO {
          union { BOOLEAN ReplaceIfExists; DWORD Flags; } DUMMYUNIONNAME;
          HANDLE RootDirectory;
          DWORD  FileNameLength;      // BYTES, excluding any terminator
          WCHAR  FileName[1];
        } FILE_RENAME_INFO;
    """

    class _U(ctypes.Union):
        _fields_ = [("ReplaceIfExists", ctypes.c_ubyte), ("Flags", ctypes.c_uint32)]

    _anonymous_ = ("u",)
    _fields_ = [
        ("u", _U),
        ("RootDirectory", ctypes.c_void_p),
        ("FileNameLength", ctypes.c_uint32),
        ("FileName", ctypes.c_wchar * 1),
    ]


def _reference_rename_buffer(destination_path):
    """Serialize FILE_RENAME_INFO independently of the module under test."""
    encoded = destination_path.encode("utf-16-le")
    name_offset = _FILE_RENAME_INFO_REFERENCE.FileName.offset
    length_offset = _FILE_RENAME_INFO_REFERENCE.FileNameLength.offset
    root_offset = _FILE_RENAME_INFO_REFERENCE.RootDirectory.offset
    buffer = bytearray(name_offset + len(encoded) + 2)        # + the WCHAR NUL terminator
    # ReplaceIfExists = FALSE and RootDirectory = NULL are both all-zero, which the zero-filled
    # bytearray already provides; written explicitly so the expectation is visible.
    struct.pack_into("<B", buffer, _FILE_RENAME_INFO_REFERENCE.ReplaceIfExists.offset, 0)
    struct.pack_into("<Q", buffer, root_offset, 0)
    struct.pack_into("<I", buffer, length_offset, len(encoded))
    buffer[name_offset:name_offset + len(encoded)] = encoded
    return bytes(buffer)


def _capture_rename_buffer(monkeypatch):
    """Record the exact bytes the module hands SetFileInformationByHandle, and still commit.

    The real entry point is invoked with the same arguments, so the rename genuinely happens and
    the module's own post-commit verification runs. A capture that returned a fake success instead
    would test the buffer while skipping everything downstream of it.
    """
    captured = []
    real_bind = w32._bind

    def capturing_bind(name):
        function = real_bind(name)
        if name != "SetFileInformationByHandle":
            return function

        def wrapper(handle, information_class, buffer, size):
            captured.append({
                "information_class": information_class,
                "size": size,
                "bytes": bytes(bytearray(buffer)[:size]),
            })
            return function(handle, information_class, buffer, size)

        return wrapper

    monkeypatch.setattr(w32, "_bind", capturing_bind)
    return captured


#: Destination leaves covering every Unicode class the task enumerates. `expect` is the committed
#: leaf the module must produce -- always identical to the request, which is the whole point.
_UNICODE_NAME_CASES = [
    ("ascii", "published.mp4"),
    ("slovak_accents", "nahravka_lubos_zilina_áäéíóôúýčďľňŕšťž.mp4"),
    ("cyrillic", "видео_клип.mp4"),
    ("cjk_bmp", "音楽動画作品.mp4"),
    ("one_supplementary", "clip_\U0001F600.mp4"),
    ("many_supplementary", "\U0001F600\U0001F601\U0001F602.mp4"),
    ("emoji_in_basename", "a\U0001F600b.mp4"),
    ("emoji_final_character", "tail_\U0001F600"),
    ("musical_symbol_supplementary", "score_\U0001D11E.mp4"),
    ("cjk_extension_b_supplementary", "ext_\U00020000.mp4"),
    ("mixed_bmp_and_supplementary", "mix_á_я_音_\U0001F600_\U0001D11E.mp4"),
    ("long_unicode", ("áľ\U0001F600" * 30) + ".mp4"),
]


@pytest.mark.parametrize("label,leaf", _UNICODE_NAME_CASES,
                         ids=[case[0] for case in _UNICODE_NAME_CASES])
def test_17_the_committed_destination_name_equals_the_requested_name(area, label, leaf):
    """[R5-P71 / F70-1] The REAL committed leaf must equal the requested leaf, exactly.

    This is the assertion R5-P69's suite never made: it checked that the API returned success, and
    `clip_<emoji>.mp4` returning success while landing at `clip_<emoji>.mp` is exactly what success
    failed to exclude. Asserted three independent ways -- the handle's own post-rename name, the
    directory listing, and the absence of any OTHER entry (which is what catches a truncation that
    happens to produce a name that also exists).
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))

    with w32.open_source(source_path) as source:
        identity_before = source.identity()
        with w32.open_destination_directory(library) as destination:
            source.rename_no_replace(destination.final_path() + "\\" + leaf)
        published_name = source.final_path()
        assert source.identity().matches(identity_before), "the object identity changed"

    # 1. the handle's own name
    assert published_name.endswith("\\" + leaf), (
        f"the handle reports {published_name!r}, which does not end with the requested leaf {leaf!r}")

    # 2. the directory holds exactly the requested name, and nothing else
    entries = os.listdir(library)
    assert entries == [leaf], (
        "the committed name differs from the requested name: "
        f"requested {leaf.encode('unicode_escape').decode('ascii')!r}, "
        f"found {[e.encode('unicode_escape').decode('ascii') for e in entries]!r}")

    # 3. the bytes survived, and the source leaf is gone
    with open(os.path.join(library, leaf), "rb") as handle:
        assert handle.read() == PAYLOAD
    assert not os.path.exists(source_path)


@pytest.mark.parametrize("label,leaf", _UNICODE_NAME_CASES,
                         ids=[case[0] for case in _UNICODE_NAME_CASES])
def test_17b_the_serialized_buffer_matches_an_independent_reference(area, monkeypatch, label, leaf):
    """[R5-P71 / F70-1] Byte-exact buffer comparison against an independently derived structure.

    Catches the defect at its source rather than through its symptom: `FileNameLength` must equal
    `len(dest.encode("utf-16-le"))`, which for a supplementary-plane character is 2 bytes more than
    `len(dest) * 2` per character.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    captured = _capture_rename_buffer(monkeypatch)

    with w32.open_source(source_path) as source:
        with w32.open_destination_directory(library) as destination:
            target = destination.final_path() + "\\" + leaf
            source.rename_no_replace(target)

    assert len(captured) == 1, f"expected exactly one commit, captured {len(captured)}"
    record = captured[0]
    expected = _reference_rename_buffer(target)

    assert record["information_class"] == 3, "the information class must be FileRenameInfo (3)"
    assert record["size"] == len(expected), (
        f"buffer size {record['size']} != independently derived {len(expected)}")
    assert record["bytes"] == expected, (
        "the serialized FILE_RENAME_INFO does not match the independent reference\n"
        f"  declared FileNameLength = {struct.unpack_from('<I', record['bytes'], 16)[0]}\n"
        f"  required FileNameLength = {len(target.encode('utf-16-le'))}")

    # The two fields that must never change, read back out of the captured bytes.
    assert struct.unpack_from("<B", record["bytes"], 0)[0] == 0, "ReplaceIfExists must be FALSE"
    assert struct.unpack_from("<Q", record["bytes"], 8)[0] == 0, "RootDirectory must be NULL"
    assert struct.unpack_from("<I", record["bytes"], 16)[0] == len(target.encode("utf-16-le"))


def test_17c_the_reference_layout_is_the_documented_x64_layout():
    """The independent reference must itself be right, or test_17b proves nothing."""
    cls = _FILE_RENAME_INFO_REFERENCE
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "this layout assertion assumes x64"
    assert cls.ReplaceIfExists.offset == 0
    assert cls.Flags.offset == 0, "ReplaceIfExists and Flags are a union and must share offset 0"
    assert cls.RootDirectory.offset == 8
    assert cls.FileNameLength.offset == 16
    assert cls.FileName.offset == 20
    assert ctypes.sizeof(cls) == 24
    assert ctypes.sizeof(ctypes.c_wchar) == 2, "UTF-16 code units are 2 bytes"


def test_17d_len_times_two_is_not_a_utf16_byte_count():
    """The arithmetic behind F70-1, pinned so the correct derivation cannot regress silently.

    `create_unicode_buffer` sizes correctly for surrogate pairs, so R5-P69 copied a *correct*
    encoding and truncated it by declaring a wrong length. That is why this is asserted as a
    property of the lengths rather than of the copy.
    """
    for _label, leaf in _UNICODE_NAME_CASES:
        encoded = leaf.encode("utf-16-le")
        assert len(encoded) == w32._encoded_destination_name(leaf).__len__()
        supplementary = sum(1 for character in leaf if ord(character) > 0xFFFF)
        assert len(encoded) == len(leaf) * 2 + supplementary * 2
        if supplementary:
            assert len(encoded) != len(leaf) * 2, (
                f"{_label}: this case must actually exercise the defect")


# =================================================================================================
# 18 -- [R5-P71 / F70-2] malformed destinations are refused BEFORE anything is committed
# =================================================================================================

#: Malformed *leaves*, appended to a real destination directory. The empty destination is
#: deliberately NOT in this list: `dir + "\\" + ""` is the directory's own path, which exists and is
#: correctly refused as `destination_exists` — a different property. It gets its own test below,
#: passing the empty string as the whole destination, which is the case F70-2 actually names.
_MALFORMED_DESTINATIONS = [
    ("embedded_nul", "cl\x00ip.bin"),
    ("embedded_nul_leading", "\x00clip.bin"),
    ("embedded_nul_trailing", "clip.bin\x00"),
    ("nul_only", "\x00"),
    ("lone_high_surrogate", "clip_\ud83d.mp4"),
    ("lone_low_surrogate", "clip_\ude00.mp4"),
    ("lone_surrogate_trailing", "tail_\ud83d"),
    ("reversed_surrogate_pair", "clip_\ude00\ud83d.mp4"),
]


@pytest.mark.parametrize("label,leaf", _MALFORMED_DESTINATIONS,
                         ids=[case[0] for case in _MALFORMED_DESTINATIONS])
def test_18_a_malformed_destination_refuses_with_nothing_committed(area, label, leaf):
    """[R5-P71 / F70-2] Refused pre-commit, and the source keeps its identity, name and bytes.

    R5-P70 measured `cl\\x00ip.bin` returning SUCCESS with the object published as `cl` and the
    source gone from its origin. The serialized buffer was itself byte-correct -- the **kernel**
    truncates at the NUL -- so this can only be fixed by refusing the input.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    identity_before = _identity_at(source_path)

    with w32.open_source(source_path) as source:
        own_name_before = source.final_path()
        with w32.open_destination_directory(library) as destination:
            with pytest.raises(w32.Win32Refused) as caught:
                source.rename_no_replace(destination.final_path() + "\\" + leaf)
        # the handle still names the same object, under the same name
        assert source.final_path() == own_name_before
        assert source.identity().matches(identity_before)

    assert caught.value.code == "destination_malformed"
    assert caught.value.code in w32.REFUSAL_CODES
    # a refusal means NOTHING happened -- not a truncated name, not anything
    assert os.listdir(library) == [], (
        f"a refused rename created {os.listdir(library)!r}")
    assert os.path.exists(source_path), "the source must still be at its original pathname"
    assert _identity_at(source_path).matches(identity_before)
    with open(source_path, "rb") as handle:
        assert handle.read() == PAYLOAD


def test_18a2_an_empty_destination_refuses_pre_commit(area):
    """[R5-P71 / F70-2] The empty destination, as a whole destination rather than an empty leaf.

    R5-P69 left this to the kernel, which refused it with winerror 24 (`rename_failed`). That was
    already fail-closed, so this is a deliberate behaviour CHANGE rather than a defect fix: the
    refusal now happens pre-commit, with the whole class of malformed input on one code path and
    one declared code. Recorded explicitly because a reviewer comparing against R5-P70's report
    will find `rename_failed`/24 described there as the current behaviour.
    """
    source_path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(source_path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.rename_no_replace("")
    assert caught.value.code == "destination_malformed"
    assert caught.value.winerror is None, (
        "a pre-commit refusal carries no winerror, because no kernel call was made")
    assert os.path.exists(source_path)


def test_18b_a_non_string_destination_is_refused_and_never_coerced(area):
    """[R5-P71 / F70-2] `str(obj)` would invent a pathname from a repr and publish to it."""
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))

    class _Sneaky:
        def __str__(self):
            return os.path.join(library, "coerced.mp4")
        def __fspath__(self):
            return os.path.join(library, "coerced.mp4")

    for bad in (None, 7, 3.5, b"published.mp4", bytearray(b"x"), ["published.mp4"],
                {"name": "published.mp4"}, _Sneaky(), object()):
        with w32.open_source(source_path) as source:
            with pytest.raises(w32.Win32Refused) as caught:
                source.rename_no_replace(bad)
        assert caught.value.code == "destination_malformed", (
            f"{type(bad).__name__} was not refused with destination_malformed")
    assert os.listdir(library) == [], "a non-string destination was coerced and published"
    assert os.path.exists(source_path)


def test_18c_a_str_subclass_cannot_pass_validation_as_a_different_string(area):
    """[R5-P71 / F70-2] Validation and serialization must see the SAME bytes.

    A `str` subclass overriding `encode` or `__contains__` would otherwise validate as one name and
    serialize as another, reintroducing the defect through the front door. The module calls
    `str.encode` unbound and checks the resulting code units, so the override is simply not
    consulted.
    """
    class _Liar(str):
        def encode(self, *args, **kwargs):          # noqa: A003 - deliberately hostile
            return "published.mp4".encode("utf-16-le")

        def __contains__(self, item):
            return False

    hostile = _Liar("cl\x00ip.bin")
    assert hostile.encode("utf-16-le") == "published.mp4".encode("utf-16-le"), (
        "INVALID_CONTROL: the hostile subclass does not actually lie")
    assert "\x00" not in hostile, "INVALID_CONTROL: the hostile __contains__ is not in effect"

    with pytest.raises(w32.Win32Refused) as caught:
        w32._encoded_destination_name(hostile)
    assert caught.value.code == "destination_malformed"

    # and a truthful subclass of a VALID name still encodes to its real bytes
    truthful = str.__new__(str, "clip_\U0001F600.mp4")
    assert w32._encoded_destination_name(truthful) == "clip_\U0001F600.mp4".encode("utf-16-le")


def test_18d_an_occupied_destination_is_untouched_by_a_malformed_sibling(area):
    """A refusal must not disturb an existing occupant of the destination directory."""
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    occupant = _write(os.path.join(library, "existing.mp4"), b"OCCUPANT" * 64)
    occupant_identity = _identity_at(occupant)
    source_path = _write(os.path.join(area, "clip.mp4"))

    with w32.open_source(source_path) as source:
        with pytest.raises(w32.Win32Refused):
            source.rename_no_replace(os.path.join(library, "bad\x00name.mp4"))

    assert sorted(os.listdir(library)) == ["existing.mp4"]
    assert _identity_at(occupant).matches(occupant_identity)
    with open(occupant, "rb") as handle:
        assert handle.read() == b"OCCUPANT" * 64


def test_18e_validation_happens_before_the_kernel_is_reached(area, monkeypatch):
    """[R5-P71 / F70-2] Nothing may be handed to SetFileInformationByHandle for a bad name.

    Asserted by counting commits rather than by inspecting the outcome: a refusal raised *after* a
    commit would leave the filesystem correct-looking in a test that only checked the directory.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    captured = _capture_rename_buffer(monkeypatch)

    with w32.open_source(source_path) as source:
        for _label, leaf in _MALFORMED_DESTINATIONS:
            with pytest.raises(w32.Win32Refused):
                source.rename_no_replace(os.path.join(library, leaf))
    assert captured == [], (
        f"a malformed destination reached SetFileInformationByHandle {len(captured)} time(s)")


def test_18f_refusal_and_commit_unverified_are_disjoint_and_distinguishable():
    """[R5-P71 / F70-2] A caller must never read a publication as 'nothing happened'."""
    assert w32.REFUSAL_CODES.isdisjoint(w32.COMMIT_UNVERIFIED_CODES)
    assert not issubclass(w32.Win32CommitUnverified, w32.Win32Refused), (
        "Win32CommitUnverified must not be caught by `except Win32Refused`, which means "
        "'nothing was committed'")
    assert not issubclass(w32.Win32Refused, w32.Win32CommitUnverified)
    unverified = w32.Win32CommitUnverified("published_name_mismatch")
    assert unverified.published is True
    assert "PUBLICATION MAY HAVE OCCURRED" in str(unverified)
    assert unverified.code in w32.COMMIT_UNVERIFIED_CODES


def test_18g_commit_unverified_codes_raised_by_the_module_are_all_declared():
    """The companion of test_14b, for the second exception type."""
    source = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    raised = set()
    for node in ast.walk(source):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "Win32CommitUnverified"):
            if node.args and isinstance(node.args[0], ast.Constant):
                raised.add(node.args[0].value)
    assert raised, "the AST scan found no commit-unverified raises, so this test would be vacuous"
    assert raised <= w32.COMMIT_UNVERIFIED_CODES, (
        f"undeclared commit-unverified codes: {sorted(raised - w32.COMMIT_UNVERIFIED_CODES)}")


def test_18h_a_commit_at_an_unrequested_name_is_reported_as_published_not_refused(area, monkeypatch):
    """[R5-P71 / F70-2] The post-commit guard converts a silent mis-publication into a loud one.

    Injected rather than measured against a real mangling route, because the two real routes are
    now closed: the point is that an UNKNOWN future route cannot return unqualified success. The
    commit is made to land at a different leaf than the one requested, exactly as F70-1 and F70-2
    did, and the module must say so.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    real_bind = w32._bind

    def diverting_bind(name):
        function = real_bind(name)
        if name != "SetFileInformationByHandle":
            return function

        def wrapper(handle, information_class, buffer, size):
            # commit to a DIFFERENT leaf than the caller asked for
            substitute = w32._encoded_destination_name(
                os.path.join(library, "not_what_you_asked_for.mp4"))
            forged = ctypes.create_string_buffer(20 + len(substitute) + 2)
            ctypes.memmove(ctypes.addressof(forged) + 16,
                           ctypes.byref(ctypes.c_uint32(len(substitute))), 4)
            ctypes.memmove(ctypes.addressof(forged) + 20, substitute, len(substitute))
            return function(handle, information_class, forged, len(forged))

        return wrapper

    monkeypatch.setattr(w32, "_bind", diverting_bind)
    with w32.open_source(source_path) as source:
        with pytest.raises(w32.Win32CommitUnverified) as caught:
            source.rename_no_replace(os.path.join(library, "requested.mp4"))
        assert caught.value.published is True
        assert caught.value.code == "published_name_mismatch"

    # The publication really did happen -- which is exactly why this is not a refusal.
    assert os.listdir(library) == ["not_what_you_asked_for.mp4"]
    assert not os.path.exists(source_path)


# =================================================================================================
# 19 -- [R5-P71 / F70-7 + F70-10] every gate condition, driven by injected API results
# =================================================================================================

class _InjectedHandle(w32._Handle):
    """A handle whose three volume-query primitives return crafted values.

    This exists because R5-P70 measured that four of the five gate conditions could be deleted
    individually with the whole suite green -- not because they are dead, but because the only
    non-local targets available on this machine are refused by all five at once. Redundancy that
    strong makes every real-volume negative control non-decisive.

    Driving the predicate directly fixes that: for each condition, every OTHER condition is
    satisfied and the one under test is the sole reason for refusal. Removing that one condition
    from the module then makes exactly one test fail.

    These tests **cannot skip**, which is also the F70-10 correction: the real SMB and FAT32
    controls remain as additional platform measurements, but the essential rejection properties no
    longer vanish on a machine without a loopback share or a non-NTFS volume.

    No real handle is held and no filesystem object is touched -- `qualify_local_ntfs` reaches the
    kernel only through the three methods overridden here.
    """

    __slots__ = ("_remote", "_filesystem", "_serial", "_flags", "_paths", "_calls")

    def __init__(self, *, remote=False, filesystem="NTFS", flags=None, paths=None,
                 serial=0x1A2B3C4D):
        self._handle = None
        self._closed = False
        self._remote = remote
        self._filesystem = filesystem
        self._flags = w32.REQUIRED_VOLUME_FLAGS if flags is None else flags
        self._paths = dict(_QUALIFYING_PATHS if paths is None else paths)
        self._serial = serial
        self._calls = []

    def _is_remote(self):
        self._calls.append("_is_remote")
        return self._remote

    def _volume_information(self):
        self._calls.append("_volume_information")
        return self._filesystem, self._serial, self._flags

    def final_path(self, flags=w32.VOLUME_NAME_DOS):
        self._calls.append(f"final_path({flags})")
        value = self._paths.get(flags)
        if value is None:
            # the real module raises this when GetFinalPathNameByHandleW returns 0
            raise w32.Win32Refused("final_path_unavailable", 3)
        return value


#: A fully qualifying local-NTFS handle, as measured on this machine's own fixed NTFS volume.
_QUALIFYING_PATHS = {
    w32.VOLUME_NAME_DOS: "\\\\?\\E:\\PROJECT_WORKSPACES\\clip.mp4",
    w32.VOLUME_NAME_GUID:
        "\\\\?\\Volume{b5f2a1c3-0000-0000-0000-100000000000}\\PROJECT_WORKSPACES\\clip.mp4",
    w32.VOLUME_NAME_NT: "\\Device\\HarddiskVolume8\\PROJECT_WORKSPACES\\clip.mp4",
}


def test_19_the_injected_baseline_qualifies_POSITIVE_CONTROL():
    """Without this, every condition test below could pass by refusing everything."""
    qualification = _InjectedHandle().qualify_local_ntfs()
    assert qualification.filesystem == "NTFS"
    assert qualification.volume_serial == 0x1A2B3C4D
    assert qualification.volume_flags == w32.REQUIRED_VOLUME_FLAGS
    assert qualification.dos_path == _QUALIFYING_PATHS[w32.VOLUME_NAME_DOS]
    assert qualification.guid_path == _QUALIFYING_PATHS[w32.VOLUME_NAME_GUID]
    assert qualification.nt_path == _QUALIFYING_PATHS[w32.VOLUME_NAME_NT]


#: One entry per independently required condition. `kwargs` breaks exactly that condition and
#: nothing else, so the refusal is attributable to it alone.
_GATE_CONDITIONS = [
    ("c1_dos_drive_shape", "volume_path_not_local_dos",
     {"paths": {**_QUALIFYING_PATHS,
                w32.VOLUME_NAME_DOS: "\\\\?\\UNC\\127.0.0.1\\E$\\PROJECT_WORKSPACES\\clip.mp4"}}),
    ("c1_dos_not_a_drive_letter", "volume_path_not_local_dos",
     {"paths": {**_QUALIFYING_PATHS, w32.VOLUME_NAME_DOS: "\\\\?\\Volume{x}\\clip.mp4"}}),
    ("c2_volume_guid_unresolvable", "volume_guid_unavailable",
     {"paths": {**_QUALIFYING_PATHS, w32.VOLUME_NAME_GUID: None}}),
    ("c2_volume_guid_wrong_shape", "volume_guid_unavailable",
     {"paths": {**_QUALIFYING_PATHS, w32.VOLUME_NAME_GUID: "\\\\?\\E:\\clip.mp4"}}),
    ("c3_local_device_namespace_mup", "volume_device_not_local",
     {"paths": {**_QUALIFYING_PATHS,
                w32.VOLUME_NAME_NT: "\\Device\\Mup\\127.0.0.1\\E$\\clip.mp4"}}),
    ("c3_local_device_namespace_unknown", "volume_device_not_local",
     {"paths": {**_QUALIFYING_PATHS,
                w32.VOLUME_NAME_NT: "\\Device\\WebDavRedirector\\clip.mp4"}}),
    ("c4a_filesystem_name", "volume_not_ntfs", {"filesystem": "FAT32"}),
    ("c4a_filesystem_name_refs", "volume_not_ntfs", {"filesystem": "ReFS"}),
    ("c4b_capability_flags", "volume_missing_required_capability",
     {"flags": w32.REQUIRED_VOLUME_FLAGS & ~w32.FILE_SUPPORTS_HARD_LINKS}),
    ("c5_remote_veto", "volume_remote", {"remote": True}),
]


@pytest.mark.parametrize("label,expected_code,kwargs", _GATE_CONDITIONS,
                         ids=[case[0] for case in _GATE_CONDITIONS])
def test_19b_each_gate_condition_is_decisive_REFUSED_VERSUS_QUALIFIED(label, expected_code, kwargs):
    """REFUSED versus QUALIFIED, not a change in refusal-code wording.

    R5-P70's point was that `test_12b` asserted a four-code disjunction, so removing a condition
    changed which code was raised while the handle stayed refused -- pinning code identity rather
    than the security property. Here the comparison is against the SAME injected handle with the
    condition restored, which genuinely qualifies. A removed condition therefore turns a refusal
    into a qualification, and that is what fails.
    """
    # the control: identical handle, condition satisfied -> QUALIFIED
    assert _InjectedHandle().qualify_local_ntfs() is not None

    with pytest.raises(w32.Win32Refused) as caught:
        _InjectedHandle(**kwargs).qualify_local_ntfs()
    assert caught.value.code == expected_code, (
        f"{label}: expected {expected_code}, got {caught.value.code}")
    assert caught.value.code in w32.REFUSAL_CODES


@pytest.mark.parametrize("flag_name", [
    "FILE_SUPPORTS_HARD_LINKS",
    "FILE_SUPPORTS_OPEN_BY_FILE_ID",
    "FILE_PERSISTENT_ACLS",
    "FILE_SUPPORTS_REPARSE_POINTS",
])
def test_19c_every_required_volume_flag_is_individually_required(flag_name):
    """Each flag in REQUIRED_VOLUME_FLAGS must be decisive on its own.

    Also pins the constant R5-P69 self-caught: `FILE_SUPPORTS_REPARSE_POINTS` is 0x80, and the
    0x100 R5-P68 recorded is `FILE_SUPPORTS_REMOTE_STORAGE`. With 0x100 here a correct local NTFS
    volume fails the gate, so this parametrisation would fail on the qualifying baseline.
    """
    bit = getattr(w32, flag_name)
    assert w32.REQUIRED_VOLUME_FLAGS & bit == bit, f"{flag_name} is not actually required"
    with pytest.raises(w32.Win32Refused) as caught:
        _InjectedHandle(flags=w32.REQUIRED_VOLUME_FLAGS & ~bit).qualify_local_ntfs()
    assert caught.value.code == "volume_missing_required_capability"


def test_19d_the_reparse_point_flag_is_0x80_not_0x100():
    """The specific constant whose mislabelling cost R5-P69 a debugging cycle."""
    assert w32.FILE_SUPPORTS_REPARSE_POINTS == 0x80
    # 0x100 is FILE_SUPPORTS_REMOTE_STORAGE and must not be what the gate requires.
    assert w32.REQUIRED_VOLUME_FLAGS & 0x100 == 0
    # the real local NTFS flag word measured on this machine has 0x80 set and 0x100 clear
    measured_local_ntfs_flags = 0x03E72EFF
    assert measured_local_ntfs_flags & 0x80 == 0x80
    assert measured_local_ntfs_flags & 0x100 == 0
    assert measured_local_ntfs_flags & w32.REQUIRED_VOLUME_FLAGS == w32.REQUIRED_VOLUME_FLAGS


def test_19e_a_failed_remote_query_is_never_positive_proof_of_locality():
    """Condition 5 is a veto, never the proof -- the D4 mutation R5-P70 describes.

    `_is_remote()` returning False means only "this class did not apply to this handle". A gate
    that treated it as proof of locality would admit a handle failing all four affirmative
    conditions, so this asserts that a non-remote handle with otherwise remote characteristics is
    still refused.
    """
    handle = _InjectedHandle(
        remote=False,                                   # the veto does not fire
        filesystem="NTFS",                              # and the name is deceptive, as measured
        paths={**_QUALIFYING_PATHS,
               w32.VOLUME_NAME_DOS: "\\\\?\\UNC\\127.0.0.1\\E$\\clip.mp4",
               w32.VOLUME_NAME_GUID: None,
               w32.VOLUME_NAME_NT: "\\Device\\Mup\\127.0.0.1\\E$\\clip.mp4"})
    with pytest.raises(w32.Win32Refused) as caught:
        handle.qualify_local_ntfs()
    assert caught.value.code in {"volume_path_not_local_dos", "volume_guid_unavailable",
                                 "volume_device_not_local"}
    assert "_is_remote" in handle._calls, "the veto must still be consulted"


def test_19f_an_smb_shaped_handle_is_refused_by_every_condition_independently():
    """The deceptive case, asserted condition by condition rather than as a disjunction.

    R5-P68 measured that an SMB loopback reports filesystem name `NTFS` **and the identical volume
    serial** as the volume it re-exports, differing only in the flag word and the path shapes. This
    drives each affirmative condition in isolation against those measured values, so the test
    documents five independent refusals instead of one.
    """
    smb_flags = 0x00C700FE              # measured for the SMB handle (vs 0x03e72eff local)
    assert smb_flags & w32.REQUIRED_VOLUME_FLAGS != w32.REQUIRED_VOLUME_FLAGS

    independently_refused = {}
    for label, kwargs in {
        "c1_dos_is_unc": {"paths": {**_QUALIFYING_PATHS,
                                    w32.VOLUME_NAME_DOS: "\\\\?\\UNC\\127.0.0.1\\E$\\clip.mp4"}},
        "c2_no_volume_guid": {"paths": {**_QUALIFYING_PATHS, w32.VOLUME_NAME_GUID: None}},
        "c3_device_is_mup": {"paths": {**_QUALIFYING_PATHS,
                                       w32.VOLUME_NAME_NT: "\\Device\\Mup\\127.0.0.1\\E$\\x"}},
        "c4b_flag_word_differs": {"flags": smb_flags},
        "c5_remote_query_succeeds": {"remote": True},
    }.items():
        with pytest.raises(w32.Win32Refused) as caught:
            _InjectedHandle(**kwargs).qualify_local_ntfs()
        independently_refused[label] = caught.value.code
    assert len(independently_refused) == 5, independently_refused
    # the identical serial is deliberately NOT what distinguishes it
    assert _InjectedHandle(serial=0x1A2B3C4D).qualify_local_ntfs().volume_serial == 0x1A2B3C4D


# =================================================================================================
# 20 -- [R5-P71 / F70-11] the handle-bound rename with two links
# =================================================================================================

def _create_hard_link(alias, target):
    """CreateHardLinkW through the test's own ctypes, never through the module under test."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateHardLinkW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
    k32.CreateHardLinkW.restype = ctypes.c_int
    return bool(k32.CreateHardLinkW(alias, target, None))


def test_20_a_multiply_linked_object_renames_and_the_outside_alias_survives(area):
    """[R5-P71 / F70-11] The H2 consequence, pinned as a measured and deliberately open residual.

    R5-P69's `test_13` pinned observability (1 -> 2 live on an open handle) and non-enforcement,
    then stopped before the commit. These are the facts an owner actually needs in order to decide
    H1 vs H2, and all of them are consequences of the rename rather than of the observation:

      * identity is preserved across the commit;
      * the outside alias SURVIVES publication and still names the same object;
      * the link count is unchanged by the rename;
      * reading through the alias yields the published object's bytes.

    **This decides nothing.** Decision B is unapproved: the module neither admits nor refuses a
    multiply-linked object, and this test asserts exactly that non-enforcement rather than
    endorsing it. A product layer that requires H1 must enforce it itself, and the residual race
    (a link created between observation and commit) is untouched by this test and remains open.
    """
    library = os.path.join(area, "LIBRARY")
    os.makedirs(library, exist_ok=True)
    source_path = _write(os.path.join(area, "clip.mp4"))
    alias = os.path.join(area, "OUTSIDE_ALIAS.mp4")

    if not _create_hard_link(alias, source_path):
        pytest.skip("INVALID_CONTROL: a second hard link could not be created here")
    assert os.path.exists(alias), "INVALID_CONTROL: the alias was not created"

    with w32.open_source(source_path) as source:
        before = source.identity()
        assert before.link_count == 2, (
            "INVALID_CONTROL: the object is not multiply linked, so nothing is being pinned")

        # the module does NOT refuse a multiply-linked object -- policy is deferred, not decided
        with w32.open_destination_directory(library) as destination:
            source.rename_no_replace(destination.final_path() + "\\published.mp4")

        after = source.identity()
        published_name = source.final_path()

    assert after.matches(before), "identity must survive the rename"
    assert after.file_id_128 == before.file_id_128
    assert after.link_count == 2, "the rename must not change the link count"
    assert published_name.endswith("\\published.mp4")

    # the outside alias survives publication and still refers to the SAME object
    assert os.path.exists(alias), "the outside alias did not survive the commit"
    alias_identity = _identity_at(alias)
    assert alias_identity.matches(after), "the alias no longer names the published object"
    assert alias_identity.file_id_128 == before.file_id_128
    with open(alias, "rb") as handle:
        assert handle.read() == PAYLOAD
    # the source leaf is gone; the object is reachable under BOTH its new name and the alias
    assert not os.path.exists(source_path)
    assert os.path.exists(os.path.join(library, "published.mp4"))
    assert _identity_at(os.path.join(library, "published.mp4")).file_id_128 == before.file_id_128


def test_20b_link_count_observation_does_not_enforce_a_policy():
    """The module must contain no admit/refuse decision keyed on `link_count`.

    Decision B is unapproved, so an H1 enforcement appearing here would be an unauthorized product
    policy rather than a fix.
    """
    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for inner in ast.walk(node.test):
                if isinstance(inner, ast.Attribute) and inner.attr == "link_count":
                    raise AssertionError(
                        "link_count is branched on; the module must observe and report it, never "
                        "make an H1/H2 decision (decision B is unapproved)")
    assert "link_count" in {
        field for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        for field in [node.target.id]
    }, "link_count must still be observed and reported"


# =================================================================================================
# 21 -- [R5-P71 / F70-8 + F70-9] handle ownership and the ABI
# =================================================================================================

def test_21_the_handle_ownership_contract_is_documented_and_the_deferral_is_explicit():
    """[R5-P71 / F70-8] Retained OPEN, with the obligation written down rather than assumed.

    No finalizer is added: `weakref` is not on the fork package's stdlib allowlist, and adding it
    would widen exactly the import scope this module exists to keep narrow. So the correction here
    is documentary, and this test pins the documentation so the deferral cannot quietly become an
    undocumented gap.
    """
    module_doc = w32.__doc__ or ""
    for obligation in ("Handle ownership", "must close it", "no finalizer", "F70-8"):
        assert obligation in module_doc, (
            f"the module docstring must state the handle contract; missing {obligation!r}")
    for function in (w32.open_source, w32.open_destination_directory):
        doc = function.__doc__ or ""
        assert "Ownership transfers to the caller" in doc, (
            f"{function.__name__} must document who owns the returned handle")
        assert "with" in doc

    # the deferral is real: no finalizer, and no new import to provide one
    assert not hasattr(w32._Handle, "__del__"), (
        "a finalizer appeared; F70-8 is recorded as deferred and `weakref` is not on the allowlist")
    source_text = open(_MODULE_SOURCE_PATH, encoding="utf-8").read()
    tree = ast.parse(source_text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert "weakref" not in imported, (
        "weakref is not on the fork allowlist; adding it widens the import scope and needs its own "
        "authorization")
    # close() must stay idempotent and never raise -- the contract the documentation relies on
    assert "Safe to call repeatedly; never raises" in (w32._Handle.close.__doc__ or "")


def test_21b_every_authorized_entry_point_has_a_declared_restype_and_argtypes():
    """[R5-P71 / F70-9] Signatures declared once, at the single binding point.

    R5-P69 had 8 `restype` assignments and 0 `argtypes`, each applied at a call site. ctypes caches
    one function pointer per name, so those assignments mutated shared state, and without
    `argtypes` a bare Python `int` passed where a pointer-width value is required would marshal as
    a 32-bit C `int` and truncate silently.
    """
    assert set(w32._SIGNATURES) == set(w32.AUTHORIZED_ENTRY_POINTS), (
        "every authorized entry point must have a declared signature, and no others")
    for name, (restype, argtypes) in w32._SIGNATURES.items():
        assert restype is not None, f"{name} has no restype"
        assert argtypes, f"{name} has no argtypes"
        for index, argument in enumerate(argtypes):
            assert hasattr(argument, "_type_") or issubclass(argument, ctypes._SimpleCData), (
                f"{name} argtype {index} is not a ctypes type")

    # No call site may assign restype or argtypes: that is what made them shared-state mutation.
    tree = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    binding_point = {node.name for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name == "_bind"}
    assert binding_point == {"_bind"}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name == "_bind":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                for target in inner.targets:
                    if isinstance(target, ast.Attribute) and target.attr in {"restype", "argtypes"}:
                        offenders.append(f"{node.name}:{target.attr}")
    assert offenders == [], (
        f"restype/argtypes must be set only inside _bind; found {offenders}")


def test_21c_no_call_relies_on_implicit_integer_conversion_for_a_pointer(area):
    """[R5-P71 / F70-9] Handle- and pointer-width arguments are declared pointer-width.

    Checked against the live signatures rather than by reading the call sites: every entry point's
    first parameter is the HANDLE, and it must be `c_void_p` (8 bytes here), never `c_int`.
    """
    assert ctypes.sizeof(ctypes.c_void_p) == 8
    for name, (_restype, argtypes) in w32._SIGNATURES.items():
        if name == "CreateFileW":
            assert argtypes[0] is ctypes.c_wchar_p, "CreateFileW takes a name, not a handle"
            assert argtypes[-1] is ctypes.c_void_p, "hTemplateFile is a HANDLE"
            assert argtypes[3] is ctypes.c_void_p, "lpSecurityAttributes is a pointer"
        else:
            assert argtypes[0] is ctypes.c_void_p, (
                f"{name}'s HANDLE parameter must be pointer-width, not {argtypes[0]}")
        for index, argument in enumerate(argtypes):
            assert argument is not ctypes.c_int or name in {
                "GetFileInformationByHandleEx", "SetFileInformationByHandle"}, (
                f"{name} argtype {index} is c_int; only the FILE_INFO_BY_HANDLE_CLASS enum may be")

    # CreateFileW's return is pointer-width, or a 64-bit handle would be truncated to 32 bits.
    assert w32._SIGNATURES["CreateFileW"][0] is ctypes.c_void_p

    # And a real call still works with the declared signatures.
    path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(path) as source:
        assert source.identity().size == len(PAYLOAD)
        assert source.final_path().endswith("clip.mp4")
        assert source.qualify_local_ntfs().filesystem.upper() == "NTFS"
        assert source.sha256_stream()[1] == len(PAYLOAD)


def test_21d_bind_applies_the_signature_on_every_resolution(area):
    """ctypes caches one function pointer per name, so the signature must be (re)applied at bind.

    Pins the shared-cached-pointer behaviour R5-P70 measured: `getattr(lib, "CloseHandle")` returns
    the same object every time, so a mutation through one reference is visible through the other.
    `_bind` therefore re-declares the signature each call, and the identity of the cached pointer is
    asserted here so the premise cannot silently change.
    """
    path = _write(os.path.join(area, "clip.mp4"))
    with w32.open_source(path):                              # force the library to load
        pass
    first = w32._bind("CloseHandle")
    second = w32._bind("CloseHandle")
    assert first is second, "ctypes no longer caches one pointer per name; re-check F70-9"
    assert first.restype is ctypes.c_int
    assert list(first.argtypes) == [ctypes.c_void_p]

    # a hostile mutation through the shared pointer is repaired by the next bind
    first.restype = None
    first.argtypes = []
    rebound = w32._bind("CloseHandle")
    assert rebound.restype is ctypes.c_int, "_bind must re-apply restype on every resolution"
    assert list(rebound.argtypes) == [ctypes.c_void_p], (
        "_bind must re-apply argtypes on every resolution")


def test_16f_importing_the_module_has_no_side_effects(area):
    """Import must not load a DLL, open a handle, probe or create a directory."""
    probe = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "before = set(os.listdir(sys.argv[2]))\n"
        "import beatsync_fork.win32_transfer as m\n"
        "assert m._k32 is None, 'kernel32 was bound at import time'\n"
        "assert set(os.listdir(sys.argv[2])) == before, 'import created filesystem entries'\n"
        "print('CLEAN')\n")
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    completed = subprocess.run([sys.executable, "-c", probe, src, area],
                               capture_output=True, text=True, timeout=120)
    assert completed.stdout.strip().endswith("CLEAN"), completed.stdout + completed.stderr


# =================================================================================================
# 22 -- [R5-P73 / F72-1] sha256_stream: chunk_size validation and full-stream completeness
#
# R5-P72 measured `sha256_stream(chunk_size=0)` returning the SHA-256 of the EMPTY STRING with
# size 0, as an unqualified success, for a real non-empty 55,500-byte file on real local NTFS.
# The mechanism is that `ReadFile(h, buf, 0, &got, NULL)` returns rv=1 with got=0, which is
# indistinguishable from EOF, so the loop's `read.value == 0` terminator fired immediately.
#
# The whole suite previously called `sha256_stream` six times and NEVER with an explicit
# chunk_size, which is why none of this was covered. These tests exist so that gap cannot reopen.
#
# Every invalid-input case proves three things, not one: the call is refused with a DECLARED code,
# NO source byte was read, and NO oversized buffer was allocated. A test that only checked the
# refusal would stay green against an implementation that validated after allocating.
# =================================================================================================

#: The chunk-size ceiling the owner-authorized correction is expected to enforce, frozen here
#: INDEPENDENTLY of the module -- exactly as EXPECTED_SURFACE is -- so a change to the module's own
#: constant cannot silently redefine what "bounded" means, and so these tests fail with a precise
#: assertion rather than an AttributeError when run against a build that has no such constant.
_EXPECTED_CHUNK_CEILING = 1 << 20

#: Just over the declared cap -- a boundary that must be refused while the cap itself is accepted.
_OVER_CAP = _EXPECTED_CHUNK_CEILING + 1


class _ComparisonLiar(int):
    """An `int` subclass whose bounds comparisons lie while its real value is unchanged.

    This is the reason the module checks `type(x) is int` rather than `isinstance(x, int)`. In
    `1 <= x <= MAX` Python gives this subclass's reflected operations priority, so `__ge__`/`__le__`
    are consulted and both return True regardless of the true value. MEASURED: ctypes does NOT
    consult `__index__`, so the divergence is in the comparison, not the marshalling -- a value can
    pass an isinstance-based bounds check and then be marshalled as its real (unchecked) number.
    """

    def __ge__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return False

    def __lt__(self, other):
        return False


class _StreamSpy:
    """Counts ReadFile invocations and every create_string_buffer request.

    An allocation larger than the module's own declared cap is turned into an immediate
    AssertionError rather than being allowed to proceed, so a regression that allocates before
    validating fails the test instead of raising MemoryError (or succeeding on a large machine).
    """

    def __init__(self, monkeypatch):
        self.readfile_calls = 0
        self.bytes_returned = 0
        self.allocations = []
        real_bind = w32._bind
        real_buffer = ctypes.create_string_buffer
        spy = self

        def counting_bind(name):
            fn = real_bind(name)
            if name != "ReadFile":
                return fn

            def read_file(handle, buffer, requested, got_ptr, overlapped):
                rv = fn(handle, buffer, requested, got_ptr, overlapped)
                spy.readfile_calls += 1
                spy.bytes_returned += ctypes.cast(
                    got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
                return rv

            return read_file

        def counting_buffer(init, size=None):
            if isinstance(init, int) and size is None:
                spy.allocations.append(int(init))
                assert int(init) <= _EXPECTED_CHUNK_CEILING, (
                    f"an allocation of {int(init)} bytes was attempted, above the independently "
                    f"frozen ceiling of {_EXPECTED_CHUNK_CEILING}")
            return real_buffer(init) if size is None else real_buffer(init, size)

        monkeypatch.setattr(w32, "_bind", counting_bind)
        monkeypatch.setattr(ctypes, "create_string_buffer", counting_buffer)


def test_22a_the_declared_cap_is_the_default_and_cannot_truncate_a_dword():
    """The bound must be the default (one literal, no drift) and below the DWORD wrap point."""
    import inspect
    default = inspect.signature(w32.SourceHandle.sha256_stream).parameters["chunk_size"].default
    assert default == w32._MAX_STREAM_CHUNK, (
        "the default chunk_size and the declared cap must be the same value, or a caller using the "
        "default could exceed the bound the cap exists to enforce")
    assert w32._MAX_STREAM_CHUNK == _EXPECTED_CHUNK_CEILING, (
        "the module's cap must match the ceiling this suite froze independently of it")
    assert w32._MAX_STREAM_CHUNK > 0
    assert w32._MAX_STREAM_CHUNK < (1 << 32), (
        "a cap at or above 2**32 would permit the very truncation it exists to prevent")
    # the truncation this bound makes unreachable, measured rather than assumed
    assert ctypes.c_uint32(1 << 32).value == 0


def test_22b_the_two_new_refusal_codes_are_declared_and_not_commit_codes():
    """A refusal means nothing happened; confusing it with a commit code would invert the report."""
    for code in ("chunk_size_invalid", "stream_incomplete"):
        assert code in w32.REFUSAL_CODES, f"{code!r} must be declared for exhaustive branching"
        assert code not in w32.COMMIT_UNVERIFIED_CODES, (
            f"{code!r} is pre-commit; declaring it as a commit code would report a publication")


@pytest.mark.parametrize("chunk_size", [None, 1, 3, 7, 4096, 65536, 1 << 20])
def test_22c_every_valid_chunk_size_produces_the_whole_content_digest(area, chunk_size):
    """`None` here means "call with no argument", i.e. the default. All must agree exactly."""
    path = _write(os.path.join(area, "clip.mp4"))
    kwargs = {} if chunk_size is None else {"chunk_size": chunk_size}
    with w32.open_source(path) as source:
        digest, total = source.sha256_stream(**kwargs)
    assert digest == hashlib.sha256(PAYLOAD).hexdigest(), (
        "a valid chunk size must not change the digest")
    assert total == len(PAYLOAD) == os.path.getsize(path)


@pytest.mark.parametrize("label,chunk_size", [
    ("zero", 0),                                   # F72-1 route one
    ("false", False),                              # F72-1 route two: bool is an int subclass
    ("true", True),                                # a misrouted flag is not a read size either
    ("negative_one", -1),
    ("negative_large", -(1 << 20)),
    ("none", None),
    ("float", 4096.0),
    ("string", "4096"),
    ("bytes", b"4096"),
    ("two_pow_32", 1 << 32),                       # F72-1 route three: marshals to 0
    ("two_pow_32_plus", (1 << 32) + 4096),
    ("one_tebibyte", 1 << 40),                     # the unbounded allocation F72-2 measured
    ("just_over_cap", _OVER_CAP),
    ("comparison_liar_zero", _ComparisonLiar(0)),
    ("comparison_liar_huge", _ComparisonLiar(1 << 40)),
])
def test_22d_an_invalid_chunk_size_reads_nothing_allocates_nothing_and_spends_nothing(
        area, monkeypatch, label, chunk_size):
    """The F72-1 regression. Three properties per case, because one alone is not enough.

    Checking only the refusal would pass against code that validated after allocating a terabyte
    or after burning the handle's one shot -- which is precisely what R5-P72 measured.
    """
    path = _write(os.path.join(area, "clip.mp4"))
    spy = _StreamSpy(monkeypatch)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=chunk_size)
        assert caught.value.code == "chunk_size_invalid", (
            f"{label}: expected a contracted refusal, got {caught.value.code!r}")
        assert caught.value.code in w32.REFUSAL_CODES

        # (1) not one source byte was read
        assert spy.readfile_calls == 0, f"{label}: ReadFile was called {spy.readfile_calls} times"
        assert spy.bytes_returned == 0
        # (2) nothing was allocated at all -- not merely nothing oversized
        assert spy.allocations == [], f"{label}: allocations attempted: {spy.allocations}"
        # (3) the handle's one shot survives, so the caller is not forced to re-acquire and
        #     re-verify because of an argument mistake
        assert source._streamed is False, f"{label}: the one-shot flag was burned by a refusal"
        digest, total = source.sha256_stream()
        assert digest == hashlib.sha256(PAYLOAD).hexdigest(), (
            f"{label}: after a pre-read refusal the handle must still hash correctly")
        assert total == len(PAYLOAD)


def test_22e_the_comparison_liar_control_is_not_vacuous():
    """INVALID_CONTROL guard: prove the liar really would defeat an isinstance-based check.

    Without this, `test_22d`'s two liar cases could be passing for the trivial reason that the
    subclass is harmless, and would prove nothing about why `type(x) is int` is the right check.
    """
    cap = _EXPECTED_CHUNK_CEILING
    for true_value in (0, 1 << 40):
        liar = _ComparisonLiar(true_value)
        isinstance_form_admits = (isinstance(liar, int) and not isinstance(liar, bool)
                                  and 1 <= liar <= cap)
        assert isinstance_form_admits, (
            "INVALID_CONTROL: the liar no longer defeats an isinstance-based bounds check, so the "
            "exact-type check is not being shown to be load-bearing")
        assert type(liar) is not int
        assert int(liar) == true_value, "the real value must be unchanged by the lying comparisons"
    # and the real value is what ctypes would have used -- 0 reproduces F72-1 exactly
    assert ctypes.c_uint32(_ComparisonLiar(0)).value == 0


def test_22f_a_premature_zero_byte_read_is_not_mistaken_for_end_of_file(area, monkeypatch):
    """A short read that reports success must fail closed, not return a digest of the prefix.

    `read.value == 0` ends the loop whether it is EOF or a premature success, so EOF alone is not
    evidence the whole object was covered. MEASURED against the R5-P71 candidate: this injection
    made it return a digest of only the first chunk, with that chunk's length, as a success.
    """
    path = _write(os.path.join(area, "clip.mp4"))

    # control: unfaulted, the same handle and chunk size produce the true digest
    with w32.open_source(path) as source:
        control_digest, control_total = source.sha256_stream(chunk_size=4096)
    assert control_digest == hashlib.sha256(PAYLOAD).hexdigest() and control_total == len(PAYLOAD), (
        "INVALID_CONTROL: the unfaulted path does not work, so the faulted refusal proves nothing")

    real_bind = w32._bind
    state = {"calls": 0, "delivered": 0}

    def faulting_bind(name):
        fn = real_bind(name)
        if name != "ReadFile":
            return fn

        def read_file(handle, buffer, requested, got_ptr, overlapped):
            if state["calls"] >= 1:
                # success, zero bytes, while the object still has content left
                ctypes.cast(got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = 0
                state["calls"] += 1
                return 1
            rv = fn(handle, buffer, requested, got_ptr, overlapped)
            state["calls"] += 1
            state["delivered"] += ctypes.cast(
                got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
            return rv

        return read_file

    monkeypatch.setattr(w32, "_bind", faulting_bind)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "stream_incomplete"
    assert caught.value.code in w32.REFUSAL_CODES
    assert 0 < state["delivered"] < len(PAYLOAD), (
        "INVALID_CONTROL: the injection must deliver a real partial prefix, otherwise an "
        "empty-stream bug and a truncation bug are not being distinguished")


def test_22g_a_mid_stream_read_failure_never_returns_a_digest(area, monkeypatch):
    """A failed ReadFile after partial data must refuse, carrying the winerror."""
    path = _write(os.path.join(area, "clip.mp4"))
    real_bind = w32._bind
    state = {"calls": 0, "delivered": 0}

    def faulting_bind(name):
        fn = real_bind(name)
        if name != "ReadFile":
            return fn

        def read_file(handle, buffer, requested, got_ptr, overlapped):
            if state["calls"] >= 2:
                ctypes.set_last_error(1117)          # ERROR_IO_DEVICE
                state["calls"] += 1
                return 0
            rv = fn(handle, buffer, requested, got_ptr, overlapped)
            state["calls"] += 1
            state["delivered"] += ctypes.cast(
                got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
            return rv

        return read_file

    monkeypatch.setattr(w32, "_bind", faulting_bind)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "stream_failed"
    assert caught.value.winerror == 1117
    assert state["delivered"] > 0, (
        "INVALID_CONTROL: the failure must land after real data, or this is not a mid-stream case")


@pytest.mark.parametrize("size", [0, 1, 4095, 4096, 4097, 55500, 65536])
def test_22h_the_reported_byte_count_equals_the_real_source_length(area, size):
    """The returned count must equal both the on-disk length and the handle's own identity size.

    The zero case matters in both directions: an empty file genuinely hashes to the empty digest,
    so the completeness check must not turn a correct empty result into a refusal.
    """
    blob = (b"R5P73-LEN" * ((size // 9) + 1))[:size]
    path = _write(os.path.join(area, "clip.mp4"), blob)
    with w32.open_source(path) as source:
        identity_size = source.identity().size
        digest, total = source.sha256_stream(chunk_size=4096)
    assert total == size == os.path.getsize(path) == identity_size
    assert digest == hashlib.sha256(blob).hexdigest()


@pytest.mark.parametrize("delta", [1, -1, 4096])
def test_22i_a_length_disagreement_refuses_instead_of_returning_a_plausible_digest(
        area, monkeypatch, delta):
    """If the expected length and the bytes read disagree, nothing may be returned.

    `identity` is patched on the CLASS, not the instance: `_Handle`/`SourceHandle` declare
    `__slots__`, so per-instance assignment raises AttributeError. The code under test is still
    the real product code; only its collaborator is faulted.
    """
    path = _write(os.path.join(area, "clip.mp4"))
    real_identity = w32._Handle.identity

    def lying_identity(handle_self):
        true = real_identity(handle_self)
        return w32.HandleIdentity(
            volume_serial=true.volume_serial, file_index=true.file_index,
            file_id_128=true.file_id_128, link_count=true.link_count,
            size=true.size + delta, attributes=true.attributes)

    monkeypatch.setattr(w32._Handle, "identity", lying_identity)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "stream_incomplete"
    assert caught.value.code in w32.REFUSAL_CODES


def test_22j_repeated_hashing_after_success_and_after_failure(area, monkeypatch):
    """The one-shot contract: spent by a read, NOT spent by a pre-read refusal."""
    # (a) after a success, a second pass is refused rather than hashing nothing
    path = _write(os.path.join(area, "a.mp4"))
    with w32.open_source(path) as source:
        assert source.sha256_stream()[1] == len(PAYLOAD)
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream()
        assert caught.value.code == "already_streamed"

    # (b) after a FAILED read -- the stream really was consumed, so the handle stays spent
    path = _write(os.path.join(area, "b.mp4"))
    real_bind = w32._bind
    state = {"calls": 0}

    def faulting_bind(name):
        fn = real_bind(name)
        if name != "ReadFile":
            return fn

        def read_file(handle, buffer, requested, got_ptr, overlapped):
            if state["calls"] >= 1:
                ctypes.set_last_error(1117)
                state["calls"] += 1
                return 0
            rv = fn(handle, buffer, requested, got_ptr, overlapped)
            state["calls"] += 1
            return rv

        return read_file

    monkeypatch.setattr(w32, "_bind", faulting_bind)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as first:
            source.sha256_stream(chunk_size=4096)
        assert first.value.code == "stream_failed"
        assert source._streamed is True, "a consumed stream must leave the handle spent"
        with pytest.raises(w32.Win32Refused) as second:
            source.sha256_stream(chunk_size=4096)
        assert second.value.code == "already_streamed", (
            "after a real read failure the handle must not offer a second pass, which would hash "
            "from wherever the file pointer stopped")

    # (c) after a PRE-READ argument refusal the handle is untouched and still usable
    monkeypatch.undo()
    path = _write(os.path.join(area, "c.mp4"))
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused):
            source.sha256_stream(chunk_size=0)
        assert source._streamed is False
        assert source.sha256_stream()[0] == hashlib.sha256(PAYLOAD).hexdigest()


def test_22k_a_closed_handle_refuses_before_the_one_shot_is_spent(area):
    """`handle_closed` must win over spending the handle, so the refusal is not self-destructive."""
    path = _write(os.path.join(area, "clip.mp4"))
    source = w32.open_source(path)
    source.close()
    with pytest.raises(w32.Win32Refused) as caught:
        source.sha256_stream()
    assert caught.value.code == "handle_closed"
    assert source._streamed is False


def test_22l_validation_precedes_allocation_and_the_one_shot_in_the_source():
    """Structural pin: the ORDER is the property, and a reordering is the defect.

    R5-P72's finding was an ordering defect -- the flag was burned and the buffer allocated before
    anything about `chunk_size` was known. A behavioural test can show the current order is right;
    this makes a future reordering visible as a failure even if some other check masks it.
    """
    module = ast.parse(open(_MODULE_SOURCE_PATH, encoding="utf-8").read())
    function = None
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "sha256_stream":
            function = node
    assert function is not None, "sha256_stream was not found, so this test would be vacuous"

    def line_of(predicate):
        hits = [n.lineno for n in ast.walk(function) if predicate(n)]
        return min(hits) if hits else None

    validation = line_of(
        lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", None) == "Win32Refused"
        and n.args and isinstance(n.args[0], ast.Constant)
        and n.args[0].value == "chunk_size_invalid")
    allocation = line_of(
        lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_string_buffer")
    one_shot = line_of(
        lambda n: isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_streamed" for t in n.targets))
    read_call = line_of(
        lambda n: isinstance(n, ast.Call) and getattr(n.func, "id", None) == "fn")

    assert validation is not None, "the chunk_size refusal is not raised inside sha256_stream"
    for name, line in (("allocation", allocation), ("one-shot assignment", one_shot),
                       ("read call", read_call)):
        assert line is not None, f"{name} was not found in sha256_stream"
        assert validation < line, (
            f"chunk_size validation (line {validation}) must precede the {name} (line {line})")
    assert one_shot < read_call, "the one-shot flag must be set before the first read"
    assert allocation < one_shot, (
        "the buffer must be allocated while the handle's one shot is still intact, so a failed "
        "allocation does not spend it")


def test_22m_a_truncated_read_reported_as_eof_is_refused(area, monkeypatch):
    """Negative-control anchor: removing the completeness check must break something real.

    This pairs with the mutation campaign. It asserts the refusal is reachable from a short read
    rather than merely declared, so a mutation that deletes the check cannot leave the suite green.
    """
    path = _write(os.path.join(area, "clip.mp4"))
    real_bind = w32._bind
    state = {"calls": 0}

    def truncating_bind(name):
        fn = real_bind(name)
        if name != "ReadFile":
            return fn

        def read_file(handle, buffer, requested, got_ptr, overlapped):
            rv = fn(handle, buffer, requested, got_ptr, overlapped)
            state["calls"] += 1
            if state["calls"] == 1:
                # truncate the very first chunk, then report EOF on the next call
                ctypes.cast(got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = 16
            elif state["calls"] == 2:
                ctypes.cast(got_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = 0
            return rv

        return read_file

    monkeypatch.setattr(w32, "_bind", truncating_bind)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "stream_incomplete", (
        "a 16-byte digest of a 65,536-byte object must never be returned as a success")


# ---- section 23: ReadFile byte-count integrity (R5-P74 F74-1) -------------------------------
#
# R5-P74 measured four wrong-digest successes in the R5-P73 candidate: `digest.update` receives
# `buffer.raw[:read.value]`, which Python CLAMPS to the buffer length, while `total` accumulated
# the unclamped `read.value`. An over-reported count therefore inflated `total` by more than the
# content it represented and could be made to land EXACTLY on `expected_size` while only a prefix
# had been hashed -- which the final `total != expected_size` comparison then accepted.
#
# Two independent properties now prevent that, and each is pinned separately below:
#   (1) a count greater than the requested read is refused `stream_failed` inside the loop;
#   (2) progress is `len(chunk)` -- the bytes actually handed to the digest.
#
# EVERY fabricated ReadFile result in this section is INJECTED BY THESE TESTS. No actual Windows
# kernel or filter-driver defect was observed, and none is claimed: a conforming `ReadFile` never
# reports more than it was asked for. These are defence-in-depth controls against an impossible
# count being trusted, not evidence of a live kernel fault.

#: 55,500 bytes: deliberately NOT a multiple of 4096, so the final chunk is short and the
#: short-positive-read path is exercised by the ordinary cases too.
_P23 = (b"R5P75-READ-COUNT-INTEGRITY-" * 3000)[:55500]
_P23_SHA = hashlib.sha256(_P23).hexdigest()

#: Frozen here INDEPENDENTLY of the module: the code that must reject an impossible count.
_EXPECTED_OVER_COUNT_REFUSAL = "stream_failed"
#: And the code for an honest count that simply does not add up to the whole object.
_EXPECTED_UNDER_COUNT_REFUSAL = "stream_incomplete"


class _HashSpy:
    """Record every byte sequence actually passed to ``digest.update``.

    This is what makes "bytes really supplied to SHA-256" a measurement rather than an
    inference from the returned total -- which is exactly the number F74-1 corrupted.
    """

    def __init__(self):
        self.chunks = []

    @property
    def total(self):
        return sum(len(c) for c in self.chunks)

    @property
    def content(self):
        return b"".join(self.chunks)

    def install(self, monkeypatch):
        real = w32.hashlib
        spy = self

        class _Hashlib:
            def __getattr__(self, name):
                return getattr(real, name)

            def sha256(self, *args):
                inner = real.sha256(*args)

                class _Recording:
                    def update(self, data):
                        spy.chunks.append(bytes(data))
                        return inner.update(data)

                    def hexdigest(self):
                        return inner.hexdigest()

                return _Recording()

        monkeypatch.setattr(w32, "hashlib", _Hashlib())
        return self


def _counting_bind(monkeypatch, *, fabricate=None, rewrite=None, clamp=None):
    """Interpose on ReadFile.

    ``fabricate(idx, requested)`` returns ``(rv, reported)`` WITHOUT a real read, so the buffer
    keeps whatever it already held -- zero-filled on the first call, stale content later.
    ``rewrite(idx, requested, real_got)`` performs the REAL read first, so genuine content enters
    the buffer, and only the REPORTED count differs.
    """
    real_bind = w32._bind
    state = {"calls": 0, "rows": [], "real_delivered": 0}

    def binder(name):
        fn = real_bind(name)
        if name != "ReadFile":
            return fn

        def read_file(handle, buffer, requested, got_ptr, overlapped):
            state["calls"] += 1
            index = state["calls"]
            cell = ctypes.cast(got_ptr, ctypes.POINTER(ctypes.c_uint32))
            forced = fabricate(index, requested) if fabricate else None
            if forced is not None:
                rv, reported = forced
                cell.contents.value = reported
                state["rows"].append({"call": index, "requested": requested,
                                      "real_got": None, "reported": reported, "rv": rv,
                                      "source": "FABRICATED"})
                return rv
            ask = min(requested, clamp) if clamp else requested
            rv = fn(handle, buffer, ask, got_ptr, overlapped)
            real_got = cell.contents.value
            state["real_delivered"] += real_got
            if rewrite is not None:
                rewritten = rewrite(index, requested, real_got)
                if rewritten is not None:
                    rv, reported = rewritten
                    cell.contents.value = reported
                    state["rows"].append({"call": index, "requested": requested,
                                          "real_got": real_got, "reported": reported,
                                          "rv": rv, "source": "REAL_COUNT_REWRITTEN"})
                    return rv
            state["rows"].append({"call": index, "requested": requested,
                                  "real_got": real_got, "reported": real_got, "rv": rv,
                                  "source": "REAL"})
            return rv

        return read_file

    monkeypatch.setattr(w32, "_bind", binder)
    return state


def _refuse_over_count(area, monkeypatch, leaf, chunk, *, fabricate=None, rewrite=None):
    """Assert an over-reported count is refused, and check every property the contract owes.

    Checks: the actual requested size, the reported size, the bytes really supplied to SHA-256,
    the independently computed whole-file digest, the returned exception, source identity and
    content, and the one-shot state.
    """
    path = _write(os.path.join(area, leaf), _P23)
    spy = _HashSpy().install(monkeypatch)
    state = _counting_bind(monkeypatch, fabricate=fabricate, rewrite=rewrite)

    with w32.open_source(path) as source:
        identity_before = source.identity()
        assert identity_before.size == len(_P23), "INVALID_CONTROL: fixture size disagrees"
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=chunk)
        # the one shot IS spent: the stream was entered before the fault
        with pytest.raises(w32.Win32Refused) as retry:
            source.sha256_stream(chunk_size=chunk)
        identity_after = source.identity()

    assert caught.value.code == _EXPECTED_OVER_COUNT_REFUSAL, (
        f"an impossible ReadFile count must be refused {_EXPECTED_OVER_COUNT_REFUSAL!r}, "
        f"not {caught.value.code!r}; a count above the request can never be legitimate")
    assert caught.value.code in w32.REFUSAL_CODES, "the refusal code must be declared"
    assert retry.value.code == "already_streamed", (
        "a fault after the stream was entered must leave the one shot spent")

    # The injection must really have been an OVER-count, or this control proves nothing.
    over = [r for r in state["rows"] if r["reported"] > r["requested"]]
    assert over, ("INVALID_CONTROL: no row reported more bytes than were requested, so the "
                  "over-count path was never exercised")

    # Nothing that was hashed may amount to the whole object, and it must never be
    # mistaken for it.
    assert spy.total < len(_P23), (
        f"INVALID_CONTROL: {spy.total} bytes were hashed out of {len(_P23)}; the fault must "
        f"leave a strict prefix for the refusal to be meaningful")
    assert hashlib.sha256(spy.content).hexdigest() != _P23_SHA, (
        "the bytes actually hashed must not equal the whole file")

    # Source untouched.
    assert identity_after.file_index == identity_before.file_index
    assert identity_after.volume_serial == identity_before.volume_serial
    assert identity_after.size == len(_P23)
    with open(path, "rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() == _P23_SHA, (
            "hashing must never modify the source")
    return state, spy


# -- honest paths: these must keep working, or every refusal above proves nothing --------------

@pytest.mark.parametrize("chunk", [1, 4096, 55500, 1 << 20])
def test_23a_an_honest_full_read_returns_the_whole_file_digest(area, chunk):
    """Ordinary full-file hashing, including an exactly-one-chunk and an oversized chunk."""
    path = _write(os.path.join(area, "clip.mp4"), _P23)
    with w32.open_source(path) as source:
        digest, total = source.sha256_stream(chunk_size=chunk)
    assert digest == _P23_SHA
    assert total == len(_P23)


def test_23b_an_honest_short_positive_read_is_not_mistaken_for_a_fault(area, monkeypatch):
    """A short POSITIVE read is legal and routine; the correction must not reject it.

    This is the case a naive "any count that is not the request is a fault" check would break,
    which is why the guard is `>` and not `!=`.
    """
    spy = _HashSpy().install(monkeypatch)
    state = _counting_bind(monkeypatch, clamp=777)
    path = _write(os.path.join(area, "clip.mp4"), _P23)
    with w32.open_source(path) as source:
        digest, total = source.sha256_stream(chunk_size=4096)
    assert digest == _P23_SHA, "short positive reads must still produce the whole-file digest"
    assert total == len(_P23)
    assert spy.total == len(_P23), "every byte must reach the digest exactly once"
    shorts = [r for r in state["rows"] if 0 < r["reported"] < r["requested"]]
    assert len(shorts) > 1, (
        "INVALID_CONTROL: the clamp did not produce repeated short positive reads")


def test_23c_an_exact_chunk_multiple_and_an_empty_source_are_both_correct(area):
    """Exact chunk boundary, and the legitimate empty-source digest."""
    exact = _write(os.path.join(area, "exact.bin"), _P23[:4096 * 13])
    with w32.open_source(exact) as source:
        digest, total = source.sha256_stream(chunk_size=4096)
    assert digest == hashlib.sha256(_P23[:4096 * 13]).hexdigest()
    assert total == 4096 * 13

    empty = _write(os.path.join(area, "empty.bin"), b"")
    with w32.open_source(empty) as source:
        digest, total = source.sha256_stream(chunk_size=4096)
    assert digest == hashlib.sha256(b"").hexdigest(), (
        "a genuinely empty source must still produce SHA-256 of the empty string")
    assert total == 0


def test_23d_an_honest_under_report_still_refuses_stream_incomplete(area, monkeypatch):
    """The contract distinguishes the two failure modes, and this pins the distinction.

    An honest count that simply does not add up is `stream_incomplete`; an impossible count is
    `stream_failed`. A correction that collapsed both into one code would lose information the
    caller is invited to branch on.
    """
    spy = _HashSpy().install(monkeypatch)
    _counting_bind(monkeypatch,
                   fabricate=lambda i, rq: (1, 0) if i == 3 else None)
    path = _write(os.path.join(area, "clip.mp4"), _P23)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == _EXPECTED_UNDER_COUNT_REFUSAL, (
        f"an honest premature terminator must stay {_EXPECTED_UNDER_COUNT_REFUSAL!r}")
    assert caught.value.code != _EXPECTED_OVER_COUNT_REFUSAL, (
        "the under-report and over-count modes must remain distinguishable")
    assert 0 < spy.total < len(_P23), (
        "INVALID_CONTROL: the injection must leave a real partial prefix")


# -- the F74-1 attacks: every one must refuse, and none may return a digest ---------------------

def test_23e_a_count_one_byte_over_the_request_is_refused(area, monkeypatch):
    """`requested + 1` is impossible and must be refused on its own merits.

    MEASURED against the R5-P73 candidate: this case was refused only INCIDENTALLY, as
    `stream_incomplete`, because the inflated total failed to match the expected size. It was
    never detected as an impossible count, which is precisely why it could be tuned into a
    wrong-digest success (see the tests below). Asserting `stream_failed` here proves the new
    guard fires rather than the completeness check catching it by luck.
    """
    state, spy = _refuse_over_count(
        area, monkeypatch, "clip.mp4", 4096,
        fabricate=lambda i, rq: (1, rq + 1) if i == 1 else ((1, 0) if i == 2 else None))
    assert state["rows"][0]["reported"] == 4097
    assert state["rows"][0]["requested"] == 4096
    assert spy.total == 0, "nothing may be hashed from an impossible count"


def test_23f_a_count_claiming_the_whole_file_after_one_chunk_is_refused(area, monkeypatch):
    """The sharpest form: one real chunk read, then a count equal to the whole expected size.

    MEASURED against the R5-P73 candidate: SUCCESS, reported size 55,500 -- equal to the true
    file size -- carrying the digest of only the first 4,096 bytes.
    """
    state, spy = _refuse_over_count(
        area, monkeypatch, "clip.mp4", 4096,
        rewrite=lambda i, rq, got: (1, len(_P23)) if i == 1 else ((1, 0) if i == 2 else None))
    assert state["rows"][0]["reported"] == len(_P23)
    assert state["rows"][0]["real_got"] == 4096, "the real read must have delivered one chunk"
    assert spy.total == 0, "the over-reported chunk must not be hashed at all"


def test_23g_an_inflated_count_after_several_legitimate_reads_is_refused(area, monkeypatch):
    """Three honest reads, then one inflated count tuned to land total exactly on expected.

    MEASURED against the R5-P73 candidate: SUCCESS, reported 55,500, digest covering 16,384 B.
    This is the case the final `total != expected_size` comparison cannot catch, because the
    attacker chose the inflation so that the comparison passes.
    """
    state, spy = _refuse_over_count(
        area, monkeypatch, "clip.mp4", 4096,
        rewrite=lambda i, rq, got: (1, len(_P23) - 3 * 4096) if i == 4
        else ((1, 0) if i == 5 else None))
    assert spy.total == 3 * 4096, (
        "the three honest reads must be hashed and the inflated fourth must not")
    inflated = state["rows"][3]
    assert inflated["reported"] == len(_P23) - 3 * 4096
    assert inflated["reported"] > inflated["requested"]
    assert 3 * 4096 + inflated["reported"] == len(_P23), (
        "INVALID_CONTROL: the inflation must be tuned to satisfy the completeness check, or "
        "this test would pass for the wrong reason")


def test_23h_an_inflated_count_over_a_stale_buffer_is_refused(area, monkeypatch):
    """Two honest reads, then a fabricated inflated count over the STALE buffer.

    No read occurs on the faulted call, so the buffer still holds chunk two. MEASURED against
    the R5-P73 candidate: SUCCESS, reported 55,500, digest covering 12,288 B of which the last
    4,096 were a duplicate of the previous chunk -- a digest of content that never existed in
    the file in that order.
    """
    state, spy = _refuse_over_count(
        area, monkeypatch, "clip.mp4", 4096,
        fabricate=lambda i, rq: (1, len(_P23) - 2 * 4096) if i == 3
        else ((1, 0) if i == 4 else None))
    assert spy.total == 2 * 4096
    assert state["rows"][2]["source"] == "FABRICATED"


def test_23i_an_inflated_count_over_a_never_filled_buffer_is_refused(area, monkeypatch):
    """Fabricated from the very first call, so the buffer is still zero-filled.

    MEASURED against the R5-P73 candidate: SUCCESS, reported 55,500, carrying the digest of
    4,096 ZERO bytes for a file containing none -- the content was never read at all.
    """
    state, spy = _refuse_over_count(
        area, monkeypatch, "clip.mp4", 4096,
        fabricate=lambda i, rq: (1, len(_P23)) if i == 1 else ((1, 0) if i == 2 else None))
    assert spy.total == 0, "nothing was read, so nothing may be hashed"
    assert state["rows"][0]["real_got"] is None, "no real read may have occurred"


def test_23j_a_failed_read_after_a_prefix_never_returns_a_digest(area, monkeypatch):
    """A genuine failure mid-stream, and the one shot stays spent on retry."""
    spy = _HashSpy().install(monkeypatch)
    _counting_bind(monkeypatch, fabricate=lambda i, rq: (0, 0) if i == 5 else None)
    path = _write(os.path.join(area, "clip.mp4"), _P23)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=4096)
        with pytest.raises(w32.Win32Refused) as retry:
            source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "stream_failed"
    assert retry.value.code == "already_streamed", (
        "retry after a partial failure must not restart the stream")
    assert 0 < spy.total < len(_P23)


@pytest.mark.parametrize("bad", [0, False, 1 << 32, 1 << 33, True, -1, 4096.0, "4096", None])
def test_23k_invalid_chunk_sizes_are_still_refused_before_any_read(area, monkeypatch, bad):
    """The F72-1 routes must stay closed, and the correction must not have moved validation."""
    state = _counting_bind(monkeypatch)
    path = _write(os.path.join(area, "clip.mp4"), _P23)
    with w32.open_source(path) as source:
        with pytest.raises(w32.Win32Refused) as caught:
            source.sha256_stream(chunk_size=bad)
        # the one shot survives a PRE-READ refusal, so a valid retry still works
        digest, total = source.sha256_stream(chunk_size=4096)
    assert caught.value.code == "chunk_size_invalid"
    assert state["calls"] == 0 or all(
        r["source"] == "REAL" for r in state["rows"]), (
        "no ReadFile may be issued for the refused call")
    assert digest == _P23_SHA and total == len(_P23), (
        "a pre-read refusal must leave the handle able to hash correctly")


def test_23l_progress_is_derived_from_the_hashed_bytes_not_the_reported_count(area, monkeypatch):
    """Structural pin, in the module's own source, for both halves of the correction.

    These are two independent protections, and either alone closes F74-1. A mutation campaign
    that removes only one will therefore find the suite still green at runtime -- which is the
    intended defence in depth, not a gap in coverage. This test is what makes removing either
    half visible in a diff, so the redundancy cannot be quietly optimised away.
    """
    text = open(_MODULE_SOURCE_PATH, encoding="utf-8").read()
    module = ast.parse(text)
    function = None
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "sha256_stream":
            function = node
    assert function is not None, "sha256_stream was not found, so this test would be vacuous"
    lines = text.splitlines()
    body = "\n".join(lines[function.lineno - 1:function.end_lineno])

    assert "total += read.value" not in body, (
        "progress must not be accumulated from the kernel's self-reported count: "
        "`buffer.raw[:read.value]` is clamped to the buffer while `read.value` is not, so the "
        "two can diverge and `total` can reach `expected_size` over a strict prefix (F74-1)")
    assert "total += len(chunk)" in body, (
        "progress must be the length of the byte sequence actually passed to digest.update")
    assert "chunk = buffer.raw[:read.value]" in body, (
        "the hashed bytes and the counted bytes must be the same object")
    assert "if read.value > chunk_size:" in body, (
        "a count greater than the requested read is impossible and must be refused")

    guard = body.index("if read.value > chunk_size:")
    hashed = body.index("digest.update(chunk)")
    eof = body.index("if read.value == 0:")
    success = body.index("raise Win32Refused(\"stream_failed\", _last_error())")
    assert success < guard, (
        "the reported count is only meaningful after ReadFile reports success")
    assert guard < eof < hashed, (
        "the impossible count must be rejected before the EOF test and before hashing")
