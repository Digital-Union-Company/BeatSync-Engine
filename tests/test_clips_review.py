"""R5 Clips Preview Selector: the secure core.

[FORK] Digital-Union. The feature's whole safety argument lives in
``beatsync_fork.clips_review``, which is stdlib-only, so all of it is ordinary importable code
here -- no Gradio, no browser, no FFmpeg. That is the point of putting it there: the thing that
decides whether a byte may be served is testable without the thing that serves it.

Four claims are kept apart deliberately, because proving one would not imply the others:

1. **A path never travels through the browser.** Ids are random and opaque, and nothing taken from
   a URL is ever joined to a filesystem path -- so traversal is impossible by construction rather
   than filtered. Asserted on the id alphabet and on the absence of any path-joining.
2. **Two browser sessions are isolated.** One operator's rescan revokes only their own pages. The
   R5-P0 prototype revoked everything process-wide, which would have let one tab destroy
   another's authorization; that behaviour is pinned here as a negative control.
3. **Identity is revalidated on the opened descriptor, not the pathname.** A name repointed
   between enumeration and the read is caught, which a ``stat`` of the path before ``open`` cannot
   promise.
4. **The decision record survives.** Process restart, a torn final journal line, a stale tab and a
   superseded scan all have defined outcomes, and a decision that could not be written durably is
   reported as a failure rather than shown as a tick.

The HTTP shape around it -- status codes, Range semantics, refusal bodies -- is in
``tests/test_clips_media_server.py``. Real-browser playback and the 25/50/100 timings are runtime
evidence and are NOT claimed here.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys

import pytest

from beatsync_fork import clips_review as cr

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


# =============================================================================================
# 1. Discovery -- the Raw Cutter's ordering, reused rather than re-invented
# =============================================================================================

def _make(path: str, payload: bytes = b"\x00" * 2048) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


@pytest.fixture
def clips_tree(tmp_path):
    """One folder shaped like real Raw Cutter output, plus every edge case that matters."""
    root = tmp_path / "review"
    for index in range(1, 7):
        _make(str(root / f"clip{index:04d}.mp4"))
    _make(str(root / "nested" / "clip0001.mp4"))                 # duplicate basename
    _make(str(root / "nested" / "deeper" / "clip0001.mp4"))      # and again, deeper
    _make(str(root / "clip_\u00fcn\u00efc\u00f8d\u00e9_\u65e5\u672c\u8a9e.mp4"))
    _make(str(root / "batch_manifest.json"), b'{"schema":"beatsync-raw-cutter-batch-1"}')
    _make(str(root / "notes.txt"), b"not a video")
    _make(str(root / "empty_zero.mp4"), b"")
    _make(str(root / "undecodable.mp4"), b"\x01\x02\x03\x04" * 8)
    return str(root)


def test_discovery_reuses_the_raw_cutter_suffix_set():
    """R5 reviews what the cutter produced, so the two must accept the same files. A second
    suffix list would be a second ordering policy in disguise.

    Read by AST rather than imported: `raw_cutter_export` pulls `ffmpeg_processing` and then
    `logger`, which imports librosa, so it is not importable on the bare interpreter this suite
    runs on. The constant is still the real one from the real file.
    """
    tree = ast.parse(open(os.path.join(_SRC, "raw_cutter_export.py"), encoding="utf-8").read())
    found = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and getattr(node.targets[0], "id", "") == "SUPPORTED_SOURCE_SUFFIXES":
            found = ast.literal_eval(node.value)
    assert found is not None, "SUPPORTED_SOURCE_SUFFIXES not found in raw_cutter_export.py"
    assert cr.SERVABLE_SUFFIXES == found


def test_discovery_calls_scan_folder_rather_than_walking_the_tree_itself():
    """Structural, and load-bearing: the deterministic order is `input_manager.order_key`'s, and
    re-deriving it here is how R5 and the Raw Cutter would come to disagree about 'the files, in
    order'."""
    source = ast.unparse(ast.parse(open(os.path.join(_SRC, "beatsync_fork", "clips_review.py"),
                                        encoding="utf-8").read()))
    body = source[source.index("def discover_clips"):source.index("def freeze_page")]
    assert "input_manager.scan_folder" in body
    for forbidden in ("os.walk", "os.scandir", "glob", "rglob", "sorted("):
        assert forbidden not in body, f"discovery re-implements traversal via {forbidden}"


def test_non_recursive_and_recursive_scans_differ_by_the_nested_clips(clips_tree):
    flat = cr.discover_clips(clips_tree, False)
    deep = cr.discover_clips(clips_tree, True)
    assert not flat.error and not deep.error
    assert len(deep.files) == len(flat.files) + 2


def test_enumeration_order_is_deterministic_and_matches_order_key(clips_tree):
    first = cr.discover_clips(clips_tree, True)
    second = cr.discover_clips(clips_tree, True)
    assert [f.path for f in first.files] == [f.path for f in second.files]
    expected = sorted((f.path for f in first.files),
                      key=lambda p: (os.path.normpath(p).replace("\\", "/").casefold(),
                                     os.path.normpath(p).replace("\\", "/")))
    assert [f.path for f in first.files] == expected


def test_the_batch_manifest_is_never_enumerated_as_a_clip(clips_tree):
    """The Raw Cutter leaves `batch_manifest.json` in its own output folder, which is the folder
    an operator will point this tab at. Serving it as a clip would hand the browser the real
    source paths the manifest exists to record."""
    names = [os.path.basename(f.path) for f in cr.discover_clips(clips_tree, True).files]
    assert "batch_manifest.json" not in names
    assert "notes.txt" not in names


def test_an_empty_file_is_rejected_but_an_undecodable_one_is_kept(clips_tree):
    """Different reasons, different outcomes. A zero-byte file cannot be reviewed; a file with
    real bytes that Chromium cannot decode still has to be classifiable."""
    names = [os.path.basename(f.path) for f in cr.discover_clips(clips_tree, True).files]
    assert "empty_zero.mp4" not in names
    assert "undecodable.mp4" in names


def test_unicode_and_duplicate_basenames_survive_enumeration(clips_tree):
    files = cr.discover_clips(clips_tree, True).files
    names = [os.path.basename(f.path) for f in files]
    assert any("\u65e5\u672c\u8a9e" in n for n in names)
    duplicates = [f.path for f in files if os.path.basename(f.path) == "clip0001.mp4"]
    assert len(duplicates) == 3 and len(set(duplicates)) == 3


def test_empty_and_missing_directories_are_handled_without_crashing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    found = cr.discover_clips(str(empty), True)
    assert not found.error and found.files == ()
    assert cr.discover_clips(str(tmp_path / "nope"), True).error
    assert cr.discover_clips("", True).error
    assert cr.discover_clips(None, True).error


def test_a_file_path_instead_of_a_folder_is_refused(clips_tree):
    assert cr.discover_clips(os.path.join(clips_tree, "clip0001.mp4"), True).error


# =============================================================================================
# 2. Opaque identity
# =============================================================================================

def _page(registry, owner, files, index=1):
    session = registry.new_session(owner, "scan-1")
    frozen = cr.freeze_page(files, 0, len(list(files)))
    token, entries = registry.publish_page(owner, session, index, frozen)
    return session, token, entries


def test_clip_ids_are_opaque_and_carry_no_path_fragment(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    assert entries
    for entry in entries:
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,}", entry.clip_id)
        for fragment in ("/", "\\", ":", ".mp4", "clip0", "nested", "review"):
            assert fragment not in entry.clip_id


def test_two_pages_of_the_same_files_mint_different_ids(clips_tree):
    """Ids are per-page random, not a function of the path, so one cannot be replayed into
    another page -- and knowing one tells you nothing about the next."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, first = _page(registry, "owner-a", files)
    _, _, second = _page(registry, "owner-a", files)
    assert {e.clip_id for e in first}.isdisjoint({e.clip_id for e in second})


def test_the_label_is_the_index_and_never_the_basename(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    assert entries[0].label == "Clip 0001"
    for entry in entries:
        assert os.path.basename(entry.path) not in entry.label


def test_no_url_text_is_ever_joined_to_a_filesystem_path():
    """The structural guarantee behind every traversal refusal. If the media layer ever joined a
    URL segment to a path, the 403s in the HTTP matrix would be filtering rather than impossibility."""
    server = ast.unparse(ast.parse(open(os.path.join(_SRC, "clips_media_server.py"),
                                        encoding="utf-8").read()))
    assert "os.path.join" not in server, "the HTTP layer must not build paths at all"
    assert "_tokens" in server


def test_a_non_media_file_can_never_enter_a_servable_page(tmp_path):
    """Defence in depth. Discovery already filters, but publish_page refuses again, so a future
    caller that enumerated differently still cannot publish a secret."""
    registry = cr.ClipRegistry()
    secret = _make(str(tmp_path / "secret.txt"), b"SENTINEL")
    info = os.stat(secret)
    session = registry.new_session("owner-a", "s")
    _, entries = registry.publish_page("owner-a", session, 1,
                                       [(1, secret, info.st_size, info.st_mtime_ns,
                                         info.st_dev, info.st_ino)])
    assert entries == []


def test_a_page_is_bounded(tmp_path):
    registry = cr.ClipRegistry()
    files = []
    for index in range(cr.MAX_CLIPS_PER_PAGE + 10):
        path = _make(str(tmp_path / f"c{index:05d}.mp4"))
        info = os.stat(path)
        files.append((index + 1, path, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
    session = registry.new_session("owner-a", "s")
    _, entries = registry.publish_page("owner-a", session, 1, files)
    assert len(entries) == cr.MAX_CLIPS_PER_PAGE


# =============================================================================================
# 3. Authorization gates
# =============================================================================================

def test_an_in_page_clip_resolves_and_everything_else_is_refused(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session, token, entries = _page(registry, "owner-a", files)
    assert registry.resolve(session, token, entries[0].clip_id).index == 1

    for args, reason in (
        ((session, token, "AAAAAAAAAAAAAAAAAAAAAA"), "clip_not_in_page"),
        ((session, token, entries[0].clip_id + "x"), "clip_not_in_page"),
        (("nosuchsession000000", token, entries[0].clip_id), "unknown_session"),
        ((session, "nosuchpagetoken0000", entries[0].clip_id), "unknown_page_token"),
        ((session, token, "../../../etc/passwd"), "malformed_token"),
        ((session, token, "C:\\Windows\\win.ini"), "malformed_token"),
        ((session, token, ".."), "malformed_token"),
        ((session, token, "x" * 80), "malformed_token"),
    ):
        with pytest.raises(cr.Refusal) as caught:
            registry.resolve(*args)
        assert caught.value.reason == reason, args
        assert caught.value.status == 403


def test_publishing_a_new_page_revokes_the_previous_one(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session = registry.new_session("owner-a", "scan-1")
    frozen = cr.freeze_page(files, 0, 3)
    first_token, first_entries = registry.publish_page("owner-a", session, 1, frozen)
    second_token, _ = registry.publish_page("owner-a", session, 2, frozen)
    with pytest.raises(cr.Refusal) as caught:
        registry.resolve(session, first_token, first_entries[0].clip_id)
    assert caught.value.reason == "expired_page_token"
    assert second_token != first_token


def test_a_revoked_page_retains_no_file_identities(clips_tree):
    """Not just unreachable -- gone. A revoked page must not keep a map of real paths alive for
    the lifetime of the process."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session = registry.new_session("owner-a", "scan-1")
    frozen = cr.freeze_page(files, 0, 3)
    token, _ = registry.publish_page("owner-a", session, 1, frozen)
    registry.publish_page("owner-a", session, 2, frozen)
    page = registry._owners["owner-a"][session].pages[token]      # noqa: SLF001 - white box
    assert page.revoked and page.clips == {}


def test_missing_session_identity_fails_closed_with_no_anonymous_fallback():
    """The R5-P0 gap. There is deliberately no shared registry to fall into, because that is how
    one operator's rescan would be able to revoke another's authorization."""
    registry = cr.ClipRegistry()
    for bad in (None, "", "   ", 7, b"abc", "has space", "semi;colon"):
        with pytest.raises(cr.Refusal) as caught:
            registry.new_session(bad, "scan")
        assert caught.value.reason in ("no_session_identity", "malformed_session_identity")
        assert caught.value.status == 403


# =============================================================================================
# 4. Cross-session isolation -- the P0 gap, closed
# =============================================================================================

def test_session_b_cannot_use_session_as_credentials(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    a_session, a_token, a_entries = _page(registry, "owner-a", files)
    b_session, b_token, b_entries = _page(registry, "owner-b", files)

    assert a_session != b_session and a_token != b_token
    # A's page token under B's session id, and vice versa.
    for session, token, clip in ((b_session, a_token, a_entries[0].clip_id),
                                 (a_session, b_token, b_entries[0].clip_id)):
        with pytest.raises(cr.Refusal) as caught:
            registry.resolve(session, token, clip)
        assert caught.value.reason == "unknown_page_token"
    # And A's own clip id is not in B's page.
    with pytest.raises(cr.Refusal) as caught:
        registry.resolve(b_session, b_token, a_entries[0].clip_id)
    assert caught.value.reason == "clip_not_in_page"


def test_a_rescan_in_a_revokes_a_but_leaves_b_working(clips_tree):
    """The behaviour R5-P0 got wrong. Its registry revoked every session on any new scan, so two
    tabs could not coexist; this asserts the correction in both directions at once."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    a_session, a_token, a_entries = _page(registry, "owner-a", files)
    b_session, b_token, b_entries = _page(registry, "owner-b", files)

    registry.new_session("owner-a", "scan-2")               # A rescans

    with pytest.raises(cr.Refusal) as caught:
        registry.resolve(a_session, a_token, a_entries[0].clip_id)
    assert caught.value.reason in ("unknown_session", "revoked_session")
    # B is untouched.
    assert registry.resolve(b_session, b_token, b_entries[0].clip_id).index == 1


def test_the_owner_of_a_session_is_recovered_server_side(clips_tree):
    """The HTTP layer authorizes on the server-minted session id and asks the registry whose it
    is. No request ever carries an owner, so a browser cannot claim to be another session."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    a_session, _, _ = _page(registry, "owner-a", files)
    b_session, _, _ = _page(registry, "owner-b", files)
    assert registry.owner_of(a_session) == "owner-a"
    assert registry.owner_of(b_session) == "owner-b"
    with pytest.raises(cr.Refusal):
        registry.owner_of("notasession00000000")


def test_revoking_one_owner_leaves_the_other(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    a_session, a_token, a_entries = _page(registry, "owner-a", files)
    b_session, b_token, b_entries = _page(registry, "owner-b", files)
    registry.revoke_owner("owner-a")
    with pytest.raises(cr.Refusal):
        registry.resolve(a_session, a_token, a_entries[0].clip_id)
    assert registry.resolve(b_session, b_token, b_entries[0].clip_id).index == 1


def test_tracked_owners_and_sessions_are_bounded(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    for index in range(cr.MAX_OWNERS + 8):
        _page(registry, f"owner-{index}", files[:1])
    assert registry.stats()["owners"] <= cr.MAX_OWNERS


# =============================================================================================
# 5. Descriptor identity -- the time-of-check/time-of-use gap
# =============================================================================================

def test_identity_is_revalidated_on_the_descriptor_not_the_pathname():
    """Structural. `os.stat(path)` followed by `open(path)` is two different objects; the check
    that decides must be the one made on the handle that will be read."""
    source = open(os.path.join(_SRC, "beatsync_fork", "clips_review.py"), encoding="utf-8").read()
    body = source[source.index("def open_entry"):source.index("def live_identity_ok")]
    assert "os.fstat(fd)" in body, "identity is not revalidated on the opened descriptor"
    assert "os.stat(" not in body, "a path stat must not be the deciding check"


def test_an_authorized_clip_opens_and_the_handle_matches_its_capture(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session, token, entries = _page(registry, "owner-a", files)
    fd, entry = registry.open_authorized(session, token, entries[0].clip_id)
    try:
        info = os.fstat(fd)
        assert info.st_size == entry.size and info.st_mtime_ns == entry.mtime_ns
    finally:
        os.close(fd)


def test_a_grown_or_truncated_clip_is_refused(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session, token, entries = _page(registry, "owner-a", files)
    target = entries[1]
    with open(target.path, "ab") as handle:
        handle.write(b"CHANGED-AFTER-ENUMERATION")
    with pytest.raises(cr.Refusal) as caught:
        registry.open_authorized(session, token, target.clip_id)
    assert caught.value.reason == "identity_changed" and caught.value.status == 409
    # Its neighbour is unaffected: the refusal is per file, not per page.
    fd, _ = registry.open_authorized(session, token, entries[0].clip_id)
    os.close(fd)


def test_a_deleted_clip_is_refused_as_unavailable(clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session, token, entries = _page(registry, "owner-a", files)
    os.unlink(entries[2].path)
    with pytest.raises(cr.Refusal) as caught:
        registry.open_authorized(session, token, entries[2].clip_id)
    assert caught.value.reason == "source_unavailable" and caught.value.status == 410


def test_a_path_replaced_by_a_different_file_of_the_same_size_is_refused(clips_tree):
    """The race a path check cannot close. Same name, same length, same mtime -- a different
    file. The inode recorded at enumeration is what catches it."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    session, token, entries = _page(registry, "owner-a", files)
    victim = entries[3]
    original = os.stat(victim.path)
    replacement = victim.path + ".other"
    with open(replacement, "wb") as handle:
        handle.write(b"\x09" * original.st_size)
    os.replace(replacement, victim.path)
    os.utime(victim.path, ns=(original.st_atime_ns, original.st_mtime_ns))
    swapped = os.stat(victim.path)
    if swapped.st_ino == victim.ino and swapped.st_size == victim.size \
            and swapped.st_mtime_ns == victim.mtime_ns:
        pytest.skip("this filesystem reused the inode and the metadata: nothing distinguishes "
                    "the two files, which is the documented residual case")
    with pytest.raises(cr.Refusal) as caught:
        registry.open_authorized(session, token, victim.clip_id)
    assert caught.value.reason == "identity_changed"


def test_a_directory_cannot_be_served_as_a_clip(tmp_path):
    registry = cr.ClipRegistry()
    folder = tmp_path / "a_folder.mp4"
    folder.mkdir()
    info = os.stat(folder)
    session = registry.new_session("owner-a", "s")
    _, entries = registry.publish_page("owner-a", session, 1,
                                       [(1, str(folder), 0, info.st_mtime_ns,
                                         info.st_dev, info.st_ino)])
    token = registry._owners["owner-a"][session].active_page_token    # noqa: SLF001
    with pytest.raises(cr.Refusal) as caught:
        registry.open_authorized(session, token, entries[0].clip_id)
    assert caught.value.reason in ("not_a_regular_file", "source_unavailable")


def test_the_identity_guarantee_is_documented_rather_than_overclaimed():
    """The core states exactly what it detects and what it does not. A path/size/mtime/inode
    identity is not a content hash, and the module must say so rather than imply otherwise."""
    doc = cr.__doc__ or ""
    assert "not** a content hash" in doc or "not a content hash" in doc
    assert "same-size" in doc


# =============================================================================================
# 6. Range parsing
# =============================================================================================

@pytest.mark.parametrize("header,expected", [
    (None, None),
    ("", None),
    ("bytes=0-99", (0, 99)),
    ("bytes=500-", (500, 999)),
    ("bytes=-100", (900, 999)),
    ("bytes=0-99999", (0, 999)),
    ("bytes=abc-def", None),
    ("items=0-5", None),
    ("bytes=-", None),
    ("bytes=0", None),
])
def test_range_parsing_accepts_or_ignores(header, expected):
    assert cr.parse_range(header, 1000) == expected


@pytest.mark.parametrize("header,reason", [
    ("bytes=5000-6000", "range_start_beyond_eof"),
    ("bytes=500-100", "range_end_before_start"),
    ("bytes=0-10,20-30", "multipart_range_unsupported"),
    ("bytes=-0", "unsatisfiable_suffix_range"),
    ("bytes=" + "9" * 400, "range_header_too_long"),
])
def test_unsatisfiable_ranges_raise_416(header, reason):
    with pytest.raises(cr.Refusal) as caught:
        cr.parse_range(header, 1000)
    assert caught.value.status == 416 and caught.value.reason == reason


def test_rfc_9110_distinguishes_unparsable_from_unsatisfiable():
    """Two different things: an unparsable Range is ignored and the full body is sent, while a
    parsable one that cannot be satisfied is a real 416."""
    assert cr.parse_range("bytes=nonsense", 100) is None
    with pytest.raises(cr.Refusal):
        cr.parse_range("bytes=900-", 100)


def test_content_types_cover_every_servable_suffix():
    for suffix in cr.SERVABLE_SUFFIXES:
        assert cr.content_type_for(suffix) != "application/octet-stream"
    assert cr.content_type_for(".txt") == "application/octet-stream"


# =============================================================================================
# 7. Path-relationship validation -- read-only
# =============================================================================================

@pytest.fixture
def roots(tmp_path):
    for name in ("input", "library", "rejected"):
        (tmp_path / name).mkdir()
    _make(str(tmp_path / "input" / "clip0001.mp4"))
    return tmp_path


def test_disjoint_roots_are_accepted(roots):
    verdict, ids = cr.validate_roots(str(roots / "input"), str(roots / "library"),
                                     str(roots / "rejected"))
    assert verdict.ok and set(ids) == {"input", "library", "rejected"}


@pytest.mark.parametrize("library,rejected,code", [
    ("input/inside", "rejected", "destination_inside_input"),
    ("input", "rejected", "library_is_input"),
    ("library", "input", "rejected_is_input"),
    ("library", "library", "destinations_identical"),
    ("library", "library/under", "destinations_nested"),
])
def test_overlapping_roots_are_refused(roots, library, rejected, code):
    verdict, _ = cr.validate_roots(str(roots / "input"), str(roots / library),
                                   str(roots / rejected))
    assert not verdict.ok and verdict.code == code


def test_input_inside_a_destination_is_refused(roots):
    verdict, _ = cr.validate_roots(str(roots / "input" / "inner"), str(roots / "input"),
                                   str(roots / "rejected"))
    assert not verdict.ok
    assert verdict.code in ("input_not_a_directory", "input_inside_destination")


@pytest.mark.parametrize("role", ["bin", "output", "input/video_analysis_cache",
                                  "input/gradio_uploads", "state"])
def test_protected_runtime_directories_are_refused_as_destinations(roots, role):
    app_root = str(roots / "app")
    target = os.path.join(app_root, *role.split("/"))
    verdict, _ = cr.validate_roots(str(roots / "input"), target, str(roots / "rejected"),
                                   app_root=app_root)
    assert not verdict.ok and verdict.code == "protected_runtime_destination"


def test_a_sibling_with_a_shared_prefix_is_not_inside(tmp_path):
    """`commonpath`, not a string prefix. `media-evil` is not inside `media`."""
    (tmp_path / "media").mkdir()
    (tmp_path / "media-evil").mkdir()
    (tmp_path / "other").mkdir()
    _make(str(tmp_path / "media" / "clip0001.mp4"))
    verdict, _ = cr.validate_roots(str(tmp_path / "media"), str(tmp_path / "media-evil"),
                                   str(tmp_path / "other"))
    assert verdict.ok


def test_empty_and_nonexistent_inputs_are_refused(roots):
    assert cr.validate_roots("", str(roots / "library"), str(roots / "rejected"))[0].code \
        == "empty_path"
    assert cr.validate_roots(str(roots / "input" / "clip0001.mp4"), str(roots / "library"),
                             str(roots / "rejected"))[0].code == "input_not_a_directory"


def test_validation_creates_no_destination_directory(roots):
    target = roots / "library" / "not-created-yet"
    cr.validate_roots(str(roots / "input"), str(target), str(roots / "rejected"))
    assert not target.exists()


def test_a_junction_alias_to_the_input_is_refused(roots):
    """Windows junctions are resolved, not banned on sight: a destination that merely *is* a
    link is fine, one that resolves into the input folder is not."""
    link = roots / "lib_link"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(roots / "input")],
                             capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip(f"could not create a junction: {created.stderr.strip()[:80]}")
    verdict, _ = cr.validate_roots(str(roots / "input"), str(link), str(roots / "rejected"))
    assert not verdict.ok and verdict.code == "library_is_input"


def test_a_junction_destination_that_resolves_elsewhere_is_accepted(roots):
    link = roots / "lib_link_ok"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(roots / "library")],
                             capture_output=True, text=True)
    if created.returncode != 0:
        pytest.skip(f"could not create a junction: {created.stderr.strip()[:80]}")
    verdict, _ = cr.validate_roots(str(roots / "input"), str(link), str(roots / "rejected"))
    assert verdict.ok, verdict.code


def test_a_directory_symlink_alias_to_the_input_is_refused(roots):
    """The case R5-P0 could not test: creating a directory symlink needs elevation or Developer
    Mode. Where it can be created, the same resolved-path comparison must catch it; where it
    cannot, this skips and the fail-closed rule below is what carries the boundary."""
    link = roots / "lib_symlink"
    try:
        os.symlink(str(roots / "input"), str(link), target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError) as exc:
        pytest.skip(f"directory symlinks unavailable without elevation: {exc}")
    verdict, _ = cr.validate_roots(str(roots / "input"), str(link), str(roots / "rejected"))
    assert not verdict.ok and verdict.code == "library_is_input"


def test_an_unresolvable_reparse_point_fails_closed(roots, monkeypatch):
    """The enforcement that does not depend on being able to create a symlink.

    If a root is a reparse point whose target `realpath` could not follow, its true location is
    unknown and every containment comparison would be meaningless -- so it is refused rather than
    assumed safe. Simulated here by making `realpath` a no-op and the path look like a link,
    because the privilege to build the real thing is not available in the suite.
    """
    link = str(roots / "library")
    monkeypatch.setattr(cr.os.path, "realpath", lambda p, strict=False: p)
    monkeypatch.setattr(cr, "_is_reparse", lambda p: os.path.normcase(p) == os.path.normcase(link))
    verdict, _ = cr.validate_roots(str(roots / "input"), link, str(roots / "rejected"))
    assert not verdict.ok and verdict.code == "unresolved_reparse_point"


# =============================================================================================
# 8. Durable decision record
# =============================================================================================

def test_every_clip_starts_undecided(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    assert store.get("a" * 64) == (cr.PENDING, 0)


def test_a_decision_is_recorded_and_revisions_advance(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    identity = "b" * 64
    assert store.decide("owner-a", identity, cr.SELECTED, "scan-1") == (cr.SELECTED, 1)
    assert store.decide("owner-a", identity, cr.REJECTED, "scan-1") == (cr.REJECTED, 2)


def test_an_illegal_decision_is_refused(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    for bad in ("DELETED", "deleted", "", None, 7):
        with pytest.raises(cr.StaleWrite) as caught:
            store.decide("owner-a", "c" * 64, bad, "scan-1")
        assert caught.value.code == "invalid_decision"


def test_a_stale_tab_cannot_silently_overwrite(tmp_path):
    """Optimistic concurrency. The second tab holds revision 0, the record is already at 1, so
    its write is refused *with the current value* rather than applied."""
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    identity = "d" * 64
    store.decide("owner-a", identity, cr.SELECTED, "scan-1")
    with pytest.raises(cr.StaleWrite) as caught:
        store.decide("owner-a", identity, cr.REJECTED, "scan-1", expected_revision=0)
    assert caught.value.code == "revision_conflict"
    assert caught.value.current == cr.SELECTED and caught.value.revision == 1
    assert store.get(identity) == (cr.SELECTED, 1)


def test_an_old_scan_cannot_overwrite_a_newer_one(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    store.begin_scan("owner-a", "scan-2")
    with pytest.raises(cr.StaleWrite) as caught:
        store.decide("owner-a", "e" * 64, cr.SELECTED, "scan-1")
    assert caught.value.code == "stale_scan"


def test_one_owners_scan_does_not_invalidate_anothers(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-a")
    store.begin_scan("owner-b", "scan-b")
    store.begin_scan("owner-a", "scan-a2")
    assert store.decide("owner-b", "f" * 64, cr.SELECTED, "scan-b") == (cr.SELECTED, 1)


def test_decisions_survive_a_process_restart(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    identity = "0" * 64
    store.decide("owner-a", identity, cr.SELECTED, "scan-1")
    reopened = cr.ReviewStore(directory)
    assert reopened.get(identity) == (cr.SELECTED, 1)
    assert reopened.current_scan("owner-a") == "scan-1"


def test_decisions_survive_compaction(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    identity = "1" * 64
    store.decide("owner-a", identity, cr.REJECTED, "scan-1")
    store.compact()
    assert os.path.getsize(store.journal_path) == 0
    assert cr.ReviewStore(directory).get(identity) == (cr.REJECTED, 1)


def test_a_torn_final_journal_record_is_discarded_and_the_rest_survives(tmp_path):
    """A crash during the last write can only damage the last line. It is discarded rather than
    guessed at, and everything before it still replays."""
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    identity = "2" * 64
    store.decide("owner-a", identity, cr.SELECTED, "scan-1")
    with open(store.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"k":"decide","identity":"3333","dec')
    recovered = cr.ReviewStore(directory)
    assert recovered.get(identity) == (cr.SELECTED, 1)
    assert recovered.corrupt_records_discarded >= 1


def test_an_unusable_snapshot_still_replays_the_journal(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    identity = "4" * 64
    store.decide("owner-a", identity, cr.SELECTED, "scan-1")
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    recovered = cr.ReviewStore(directory)
    assert recovered.get(identity) == (cr.SELECTED, 1)


def test_a_journal_that_cannot_be_written_is_reported_not_swallowed(tmp_path, monkeypatch):
    """A decision the operator is shown must be on disk. If the write fails the caller gets an
    exception, never a tick."""
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")

    def refuse(*args, **kwargs):
        raise OSError(13, "permission denied")

    monkeypatch.setattr(cr.os, "open", refuse)
    with pytest.raises(cr.PersistenceError):
        store.decide("owner-a", "5" * 64, cr.SELECTED, "scan-1")


def test_the_journal_is_fsynced_and_appended_one_record_at_a_time():
    source = open(os.path.join(_SRC, "beatsync_fork", "clips_review.py"), encoding="utf-8").read()
    body = source[source.index("def _append"):source.index("def compact")]
    assert "os.fsync" in body
    assert "O_APPEND" in body


def test_the_snapshot_is_replaced_atomically():
    source = open(os.path.join(_SRC, "beatsync_fork", "clips_review.py"), encoding="utf-8").read()
    body = source[source.index("def compact"):source.index("def maybe_compact")]
    assert "os.replace" in body
    assert "mkstemp" in body


def test_compaction_is_bounded_by_a_record_count(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    assert store.maybe_compact() is False
    store.journal_records = cr.COMPACT_AFTER_RECORDS
    assert store.maybe_compact() is True
    assert store.journal_records == 0


def test_a_changed_clip_inherits_no_decision(tmp_path):
    """Structural invalidation: identity folds in size and mtime, so changed bytes hash to a
    different key and the old decision simply does not apply."""
    clip = _make(str(tmp_path / "clip0001.mp4"))
    info = os.stat(clip)
    before = cr.decision_identity(clip, info.st_size, info.st_mtime_ns)
    after = cr.decision_identity(clip, info.st_size + 1, info.st_mtime_ns)
    assert before != after
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", before, cr.SELECTED, "scan-1")
    assert store.get(after) == (cr.PENDING, 0)


def test_counts_and_snapshots_agree(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    identities = [f"{index:064x}" for index in range(5)]
    store.decide("owner-a", identities[0], cr.SELECTED, "scan-1")
    store.decide("owner-a", identities[1], cr.REJECTED, "scan-1")
    counts = store.counts(identities)
    assert counts == {cr.SELECTED: 1, cr.REJECTED: 1, cr.PENDING: 3, "TOTAL": 5}
    snapshot = store.snapshot_of(identities)
    assert snapshot[identities[0]] == (cr.SELECTED, 1)
    assert snapshot[identities[4]] == (cr.PENDING, 0)


def test_no_decision_state_is_written_beside_the_source_media(clips_tree, tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "6" * 64, cr.SELECTED, "scan-1")
    for _, _, names in os.walk(clips_tree):
        for name in names:
            assert not name.startswith("review-"), name
    assert os.path.isfile(os.path.join(str(tmp_path / "state"), "review-journal.jsonl"))


def test_the_store_directory_is_not_under_gradio_uploads():
    """`cleanup_on_startup` deletes everything under `input/` except three protected folders, so a
    decision journal there would be destroyed by the next app start."""
    source = open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read()
    line = [l for l in source.splitlines() if l.startswith("CLIPS_STATE_DIR")][0]
    assert "'state'" in line and "clips-review" in line
    assert "gradio_uploads" not in line and "input" not in line


# =============================================================================================
# 9. Posters
# =============================================================================================

def test_the_poster_argv_is_built_in_one_place_and_seeks_before_decoding():
    argv = cr.build_poster_command("ffmpeg.exe", "in.mp4", "out.jpg", 1.0)
    assert argv[0] == "ffmpeg.exe"
    assert argv.index("-ss") < argv.index("-i"), "seeking after -i would decode the whole clip"
    assert "-frames:v" in argv and argv[-1] == "out.jpg"
    assert f"scale={cr.POSTER_WIDTH}:-2" in argv


def test_poster_keys_change_when_the_clip_changes(tmp_path):
    clip = _make(str(tmp_path / "clip0001.mp4"))
    info = os.stat(clip)
    first = cr.poster_key(clip, info.st_size, info.st_mtime_ns)
    assert first != cr.poster_key(clip, info.st_size + 1, info.st_mtime_ns)
    assert first != cr.poster_key(clip, info.st_size, info.st_mtime_ns + 1)


def test_posters_are_generated_once_then_cached(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        with open(argv[-1], "wb") as handle:
            handle.write(b"\xff\xd8\xff")
        return 0

    cache = str(tmp_path / "posters")
    first = cr.ensure_posters(entries, cache, "ffmpeg", runner, workers=1)
    assert first.generated == len(entries) and first.hits == 0
    second = cr.ensure_posters(entries, cache, "ffmpeg", runner, workers=1)
    assert second.hits == len(entries) and second.generated == 0
    assert len(calls) == len(entries), "a cached poster must not be regenerated"


def test_a_failing_poster_leaves_the_clip_classifiable(tmp_path, clips_tree):
    """A poster is optional. An undecodable clip must still be reviewable, so a failure is
    counted and the file removed rather than raised."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)

    def runner(argv, timeout):
        return 1

    outcome = cr.ensure_posters(entries, str(tmp_path / "posters"), "ffmpeg", runner, workers=1)
    assert outcome.failures == len(entries) and outcome.generated == 0
    assert not os.listdir(str(tmp_path / "posters"))


def test_a_poster_runner_that_raises_is_contained(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)

    def runner(argv, timeout):
        raise OSError("the binary vanished")

    outcome = cr.ensure_posters(entries, str(tmp_path / "posters"), "ffmpeg", runner, workers=1)
    assert outcome.failures == len(entries)


def test_poster_concurrency_is_bounded_to_four(tmp_path, clips_tree):
    """Four is a ceiling, not a suggestion: D0 traced clip-extraction timeouts to four concurrent
    FFmpeg-class readers on one mechanical drive, so this must never be raised casually."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)

    def runner(argv, timeout):
        with open(argv[-1], "wb") as handle:
            handle.write(b"\xff\xd8\xff")
        return 0

    outcome = cr.ensure_posters(entries, str(tmp_path / "posters"), "ffmpeg", runner, workers=99)
    assert outcome.workers == cr.POSTER_MAX_WORKERS == 4


def test_the_poster_cache_is_pruned_to_a_bound(tmp_path):
    cache = tmp_path / "posters"
    cache.mkdir()
    for index in range(12):
        _make(str(cache / f"{index:032x}.jpg"), b"\xff\xd8\xff")
    removed = cr.prune_poster_cache(str(cache), limit=5)
    assert removed == 7
    assert len(os.listdir(cache)) == 5


# =============================================================================================
# 10. Gallery markup -- escaped, and never the decision record
# =============================================================================================

def _entry(clip_id="abc123", index=1, path=r"C:\secret\private wedding.mp4"):
    return cr.ClipEntry(clip_id=clip_id, index=index, path=path, size=10, mtime_ns=1,
                        dev=1, ino=2, suffix=".mp4")


def test_a_tile_leaks_no_path_or_filename():
    html = cr.tile_html(_entry(), cr.PENDING, 0, "sess", "page", True)
    for fragment in ("secret", "private", "wedding", ".mp4", "C:", "\\"):
        assert fragment not in html, fragment
    assert "Clip 0001" in html


def test_a_tile_carries_a_non_colour_status_signal():
    """Colour must not be the only indicator -- the badge glyph and the aria-label both say it."""
    for decision, badge in ((cr.PENDING, "?"), (cr.SELECTED, "✓"), (cr.REJECTED, "✗")):
        html = cr.tile_html(_entry(), decision, 1, "s", "p", True)
        assert f'>{badge}<' in html
        assert decision.lower() in html
        assert 'role="button"' in html and 'tabindex="0"' in html


def test_markup_cannot_be_injected_through_an_id():
    html = cr.tile_html(_entry(clip_id="a&b<script>"), cr.PENDING, 0, "s", "p", False)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html or "&amp;" in html


def test_an_unplayable_clip_gets_a_fallback_that_still_classifies():
    html = cr.tile_html(_entry(), cr.PENDING, 0, "s", "p", has_poster=False)
    assert "preview unavailable" in html and "still classifiable" in html
    assert 'data-clip="abc123"' in html, "the tile is still classifiable"
    assert "<img" not in html


def test_the_grid_carries_a_nonce_so_an_identical_render_is_still_delivered():
    """Measured in R5-P0: Gradio 6.19.0 does not deliver a `gr.HTML` update whose markup equals
    the previous value, which made 're-read the server's record' look like a no-op."""
    entries = [_entry()]
    identities = {"abc123": "ident"}
    decisions = {"ident": (cr.PENDING, 0)}
    first = cr.gallery_html(entries, decisions, identities, "s", "p", lambda e: True)
    second = cr.gallery_html(entries, decisions, identities, "s", "p", lambda e: True)
    assert first != second
    assert 'data-nonce="' in first


def test_the_grid_exposes_only_opaque_ids_to_the_browser():
    entries = [_entry()]
    html = cr.gallery_html(entries, {"ident": (cr.SELECTED, 3)}, {"abc123": "ident"},
                           "sess", "page", lambda e: True)
    assert "/r5clip/sess/page/abc123" in html
    assert "/r5poster/sess/page/abc123" in html
    assert "secret" not in html and "wedding" not in html


def test_counts_text_reports_all_four_numbers():
    text = cr.counts_text({cr.SELECTED: 2, cr.REJECTED: 3, cr.PENDING: 4, "TOTAL": 9})
    assert "Kept 2" in text and "Discarded 3" in text
    assert "Undecided 4" in text and "Total 9" in text


# =============================================================================================
# 11. The core stays stdlib-only
# =============================================================================================

def test_the_core_imports_no_runtime_module():
    """The hard rule for `beatsync_fork`. It is what makes every assertion above runnable without
    Gradio, FFmpeg or a browser."""
    tree = ast.parse(open(os.path.join(_SRC, "beatsync_fork", "clips_review.py"),
                          encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("gradio", "numpy", "cv2", "librosa", "cupy", "logger", "paths",
                      "starlette", "fastapi", "ffmpeg_processing"):
        assert forbidden not in imported, f"the core imports {forbidden}"


def test_the_core_is_importable_on_a_bare_interpreter():
    probe = ("import sys; sys.path.insert(0, %r);"
             "import beatsync_fork.clips_review as c;"
             "leaked=[m for m in ('gradio','numpy','cv2','librosa','cupy','logger','paths')"
             " if m in sys.modules];"
             "print(leaked)") % _SRC
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout
