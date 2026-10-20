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


def test_an_unusable_snapshot_refuses_the_store_and_changes_nothing(tmp_path):
    """[R5-P13 / N7-R1] **Replaces** an R5-P3 assertion that an unusable snapshot replays anyway.

    The rationale for the change, stated where the old expectation lived: `compact()` truncates
    the journal, so a snapshot's existence *means* the journal is only the delta written after it.
    Continuing from a journal that cannot be shown to hold the rest published a partial history as
    authoritative and let the next compaction write it over the real snapshot. The positive
    recovery paths this used to cover are covered by `test_n7_case1`/`2`/`3` and by
    `test_decisions_survive_compaction`, which exercise exactly the same journal-replay code.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    identity = "4" * 64
    store.decide("owner-a", identity, cr.SELECTED, "scan-1")
    journal = open(store.journal_path, "rb").read()
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")

    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    assert open(store.snapshot_path, encoding="utf-8").read() == "{ this is not json", (
        "a refused snapshot is never rewritten")
    assert open(store.journal_path, "rb").read() == journal, "nor is the journal touched"

    os.unlink(store.snapshot_path)
    assert cr.ReviewStore(directory).get(identity) == (cr.SELECTED, 1), (
        "the journal still holds the decision, so the refusal lost nothing")


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

#: [R5-P3 / F6] A COMPLETE minimal JPEG: start-of-image, a little payload, end-of-image.
#:
#: The fixtures below used to write the three SOI bytes only. `ensure_posters` now distinguishes a
#: finished poster from a truncated one by the end-of-image marker -- because a size check cannot,
#: which is how a poster truncated by a killed FFmpeg used to count as a permanent cache hit. So a
#: fixture writing SOI alone was standing in for exactly the corruption the check must reject.
#: Every assertion in this section is unchanged; only the fixture became realistic.
_JPEG = b"\xff\xd8\xff\xdb" + b"\x00" * 48 + b"\xff\xd9"


def test_the_poster_argv_is_built_in_one_place_and_seeks_before_decoding():
    argv = cr.build_poster_command("ffmpeg.exe", "in.mp4", "out.jpg", 1.0)
    assert argv[0] == "ffmpeg.exe"
    assert argv.index("-ss") < argv.index("-i"), "seeking after -i would decode the whole clip"
    assert "-frames:v" in argv and argv[-1] == "out.jpg"
    assert f"scale={cr.POSTER_WIDTH}:-2" in argv
    # [R5-P3 / F6] The destination is an attempt file now, so the format must be stated rather
    # than inferred from the output filename's extension.
    assert argv[argv.index("-f") + 1] == "image2"
    assert argv[argv.index("-c:v") + 1] == "mjpeg"


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
            handle.write(_JPEG)
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
            handle.write(_JPEG)
        return 0

    outcome = cr.ensure_posters(entries, str(tmp_path / "posters"), "ffmpeg", runner, workers=99)
    assert outcome.workers == cr.POSTER_MAX_WORKERS == 4


def test_the_poster_cache_is_pruned_to_a_bound(tmp_path):
    cache = tmp_path / "posters"
    cache.mkdir()
    for index in range(12):
        _make(str(cache / f"{index:032x}.jpg"), _JPEG)
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


# =============================================================================================
# 13. [R5-P3] Independent-review remediation: F1, F2, F4, F6
#
# Every test below pins a defect that was independently reproduced on the reviewed commit
# 2829800b and corrected in this one. They are regression tests in the strict sense: each one
# fails on the pre-remediation source.
# =============================================================================================

import threading                                                            # noqa: E402
import time                                                                 # noqa: E402


# ---- F1: the same-owner scan generation protocol -------------------------------------------

def test_scan_generations_are_monotonic_and_per_owner():
    coordinator = cr.ScanCoordinator()
    a1 = coordinator.begin("owner-a")
    b1 = coordinator.begin("owner-b")
    a2 = coordinator.begin("owner-a")
    assert a2 > a1 and b1 > a1, "generations must increase"
    assert coordinator.newest("owner-a") == a2
    assert coordinator.newest("owner-b") == b1, "one owner's scan must not move another's"
    assert coordinator.is_current("owner-a", a2) and not coordinator.is_current("owner-a", a1)


def test_an_older_generation_cannot_enter_the_authority_section():
    """The whole point of F1: the generation check and the authority-changing calls must happen
    together. A check followed by an unprotected new_session is what the defect was."""
    coordinator = cr.ScanCoordinator()
    old = coordinator.begin("owner-a")
    new = coordinator.begin("owner-a")
    with pytest.raises(cr.SupersededScan) as refused:
        with coordinator.authority("owner-a", old):
            pytest.fail("the superseded scan entered its authority section")
    assert refused.value.generation == old and refused.value.newest == new
    with coordinator.authority("owner-a", new):
        pass                                        # the newest one enters


def test_an_older_generation_cannot_publish_a_view():
    coordinator = cr.ScanCoordinator()
    old = coordinator.begin("owner-a")
    new = coordinator.begin("owner-a")
    assert coordinator.publish("owner-a", new, {"session": "new", "entries": []}) is True
    assert coordinator.publish("owner-a", old, {"session": "old", "entries": []}) is False
    assert coordinator.view("owner-a")["session"] == "new"
    assert coordinator.stats()["superseded_scans"] == 1


def test_the_authority_section_serialises_concurrent_same_owner_scans():
    """Two threads racing the authority section: exactly one must get through, and it must be
    the newer generation."""
    coordinator = cr.ScanCoordinator()
    started = threading.Barrier(2)
    results = []

    def attempt(generation):
        started.wait(10)
        try:
            with coordinator.authority("owner-a", generation):
                time.sleep(0.01)
                results.append(("entered", generation))
        except cr.SupersededScan:
            results.append(("refused", generation))

    old = coordinator.begin("owner-a")
    new = coordinator.begin("owner-a")
    threads = [threading.Thread(target=attempt, args=(g,)) for g in (old, new)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert ("entered", new) in results
    assert ("refused", old) in results
    assert [r for r in results if r[0] == "entered"] == [("entered", new)]


def test_a_failed_newest_scan_discards_rather_than_resurrecting_an_older_page():
    coordinator = cr.ScanCoordinator()
    first = coordinator.begin("owner-a")
    coordinator.publish("owner-a", first, {"session": "first", "entries": [1]})
    second = coordinator.begin("owner-a")
    assert coordinator.discard("owner-a", second) is True
    assert coordinator.view("owner-a") == {}, (
        "a failed newest scan must leave an honest empty state, not the previous page")


def test_an_older_generation_cannot_discard_the_newest_view():
    coordinator = cr.ScanCoordinator()
    old = coordinator.begin("owner-a")
    new = coordinator.begin("owner-a")
    coordinator.publish("owner-a", new, {"session": "new", "entries": [1]})
    assert coordinator.discard("owner-a", old) is False
    assert coordinator.view("owner-a")["session"] == "new"


def test_the_coordinator_is_bounded_and_forgets_owners():
    coordinator = cr.ScanCoordinator(max_owners=3)
    for index in range(6):
        owner = f"owner-{index}"
        coordinator.publish(owner, coordinator.begin(owner), {"session": owner, "entries": []})
    assert coordinator.stats()["owners"] <= 3
    assert coordinator.view("owner-0") == {}, "the oldest owner was evicted"
    assert coordinator.view("owner-5")["session"] == "owner-5"
    coordinator.forget("owner-5")
    assert coordinator.view("owner-5") == {}


def test_coordinator_identity_fails_closed():
    coordinator = cr.ScanCoordinator()
    for bad in (None, "", "   ", 7, "a" * 200, "has space", "has/slash"):
        with pytest.raises(cr.Refusal):
            coordinator.begin(bad)
    assert coordinator.view(None) == {}, "view must not raise for an unusable owner"


def test_a_non_integer_generation_can_never_publish():
    coordinator = cr.ScanCoordinator()
    generation = coordinator.begin("owner-a")
    for bad in (None, "1", "abc", 1.5, [], {}):
        assert coordinator.publish("owner-a", bad, {"session": "x", "entries": []}) is False
    assert coordinator.publish("owner-a", generation, {"session": "ok", "entries": []}) is True


# ---- [R5-P9 / N4] an active view must never describe a revoked session ---------------------
#
# `begin` makes a new scan the newest *before* that scan revokes the previous session and
# publishes its own page. For the whole interval in between -- which contains the poster pass, so
# seconds, not microseconds -- the stored view named a session the registry had already revoked.
# Every reader of `view()` (the handler, Refresh, and the `/r5state` read) therefore described a
# dead page as current.

def _published(coordinator, owner, session):
    generation = coordinator.begin(owner)
    assert coordinator.publish(owner, generation, {"session": session, "entries": []})
    return generation


def test_a_view_stops_being_current_the_moment_a_newer_scan_begins():
    coordinator = cr.ScanCoordinator()
    _published(coordinator, "owner-a", "session-1")
    assert coordinator.view("owner-a")["session"] == "session-1"

    coordinator.begin("owner-a")             # the newer scan has started, not yet published
    assert coordinator.view("owner-a") == {}, (
        "between `begin` and `publish` the previous session is already revoked, so the stored "
        "view must not be served as the current page")


def test_the_newer_scans_page_becomes_current_once_it_publishes():
    coordinator = cr.ScanCoordinator()
    _published(coordinator, "owner-a", "session-1")
    second = coordinator.begin("owner-a")
    assert coordinator.view("owner-a") == {}
    assert coordinator.publish("owner-a", second, {"session": "session-2", "entries": []})
    assert coordinator.view("owner-a")["session"] == "session-2"


def test_a_scan_that_never_took_a_generation_cannot_blank_a_valid_view():
    """A refused folder or an empty enumeration returns before `begin`, so it changes nothing.

    This is the property that keeps the fail-closed read above from being destructive: only a
    scan that is really going to change authoritative state supersedes the current page.
    """
    coordinator = cr.ScanCoordinator()
    _published(coordinator, "owner-a", "session-1")
    for _ in range(3):
        assert coordinator.view("owner-a")["session"] == "session-1"
    assert coordinator.newest("owner-a") == 1


def test_a_failed_newest_scan_never_resurrects_the_older_page():
    """Both with and without the handler's `discard`, which must not be the only line of defence."""
    coordinator = cr.ScanCoordinator()
    _published(coordinator, "owner-a", "session-1")
    failed = coordinator.begin("owner-a")
    coordinator.discard("owner-a", failed)
    assert coordinator.view("owner-a") == {}

    other = cr.ScanCoordinator()
    _published(other, "owner-a", "session-1")
    other.begin("owner-a")                   # fails without ever calling discard
    assert other.view("owner-a") == {}, "the older page must not reappear as current"


def test_one_owners_transition_does_not_disturb_another_owner():
    coordinator = cr.ScanCoordinator()
    _published(coordinator, "owner-a", "a-session")
    _published(coordinator, "owner-b", "b-session")
    coordinator.begin("owner-b")             # B is mid-scan
    assert coordinator.view("owner-b") == {}
    assert coordinator.view("owner-a")["session"] == "a-session", "owners are isolated"


# ---- [R5-P9 / N5] a failed compaction must not cost a committed decision --------------------
#
# Compaction is an optimisation over the journal, never the record. These pin the two failure
# points separately, because they fail at opposite ends of the operation.

def _store_with_decisions(tmp_path, count=4):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    for index in range(count):
        store.decide("owner-a", f"clip-{index}", cr.SELECTED, "scan-1")
    return store


def test_a_snapshot_publication_failure_loses_no_committed_decision(tmp_path, monkeypatch):
    """The snapshot never lands. The journal must still hold everything."""
    store = _store_with_decisions(tmp_path)
    real_replace = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst) == store.snapshot_path:
            raise OSError(13, "snapshot refused", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cr.os, "replace", refuse)
    with pytest.raises(OSError):
        store.compact()
    monkeypatch.undo()

    assert os.path.getsize(store.journal_path) > 0, "the journal must not have been truncated"
    reloaded = cr.ReviewStore(str(tmp_path / "state"))
    for index in range(4):
        assert reloaded.get(f"clip-{index}") == (cr.SELECTED, 1)


def test_a_journal_truncation_failure_after_the_snapshot_landed_loses_no_decision(
        tmp_path, monkeypatch):
    """The snapshot *has* landed and then the truncation fails -- the crash-safe direction.

    Both copies briefly hold the same records, which is why the replay is idempotent per identity.
    Nothing may be lost, and nothing may be double-counted.
    """
    store = _store_with_decisions(tmp_path)
    real_open = open

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(file) == store.journal_path and "w" in str(mode):
            raise OSError(13, "truncation refused", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)
    with pytest.raises(OSError):
        store.compact()
    monkeypatch.undo()

    assert os.path.exists(store.snapshot_path), "the snapshot did land"
    reloaded = cr.ReviewStore(str(tmp_path / "state"))
    for index in range(4):
        assert reloaded.get(f"clip-{index}") == (cr.SELECTED, 1), "no record lost or doubled"
    assert reloaded.get("clip-0")[1] == 1, "a replayed record must not advance a revision"


def test_the_public_io_detail_is_the_same_numbers_only_formatter():
    """[R5-P9] `gui.py` formats its operator-visible I/O failures with this, so it is public.

    One rule, one implementation: a second copy in the view layer is how the two drift apart.
    """
    failure = OSError(13, "denied", r"C:\state\review-journal.jsonl", 5,
                      r"C:\state\review-snapshot.json")
    detail = cr.io_detail(failure)
    assert detail == "errno 13, winerror 5"
    assert "io_detail" in cr.__all__
    assert cr.io_detail(OSError(28, "no space")) == "errno 28"
    assert cr.io_detail(Exception("not an OSError")) == "errno unknown"
    stripped = lambda text: text.replace("\\", "").replace("/", "")      # noqa: E731
    assert stripped("C:statereview-journal.jsonl") not in stripped(detail)


# ---- F2: the strict mandatory expected revision --------------------------------------------

@pytest.mark.parametrize("payload,code", [
    ({}, cr.REVISION_REQUIRED),
    ({"to": "SELECTED"}, cr.REVISION_REQUIRED),
    ({"rev": None}, cr.REVISION_REQUIRED),
    ({"rev": "0"}, cr.MALFORMED_REVISION),
    ({"rev": "1"}, cr.MALFORMED_REVISION),
    ({"rev": "1abc"}, cr.MALFORMED_REVISION),
    ({"rev": "abc"}, cr.MALFORMED_REVISION),
    ({"rev": True}, cr.MALFORMED_REVISION),
    ({"rev": False}, cr.MALFORMED_REVISION),
    ({"rev": 0.9}, cr.MALFORMED_REVISION),
    ({"rev": 1.0}, cr.MALFORMED_REVISION),
    ({"rev": -1}, cr.MALFORMED_REVISION),
    ({"rev": []}, cr.MALFORMED_REVISION),
    ({"rev": {}}, cr.MALFORMED_REVISION),
    ("not a dict", cr.REVISION_REQUIRED),
    (None, cr.REVISION_REQUIRED),
])
def test_only_a_genuine_nonnegative_json_integer_is_an_expected_revision(payload, code):
    """F2. Coercing with int() under except (TypeError, ValueError) -> expected = None turned
    every one of these into "skip the concurrency check" -- a fail-open, and reachable from the
    shipped client because JSON.stringify of NaN is null."""
    with pytest.raises(cr.RevisionError) as refused:
        cr.parse_expected_revision(payload)
    assert refused.value.code == code


@pytest.mark.parametrize("value", [0, 1, 2, 99, 10 ** 9])
def test_a_valid_revision_parses_to_itself(value):
    assert cr.parse_expected_revision({"to": "SELECTED", "rev": value}) == value


def test_bool_is_rejected_even_though_it_is_an_int_subclass():
    """isinstance(True, int) is True in Python, so without an explicit guard a JSON true would
    be read as revision 1."""
    assert isinstance(True, int)
    with pytest.raises(cr.RevisionError):
        cr.parse_expected_revision({"rev": True})


def test_the_store_refuses_a_non_integer_expected_revision_rather_than_coercing(tmp_path):
    """Defence in depth. int(0.9) is 0 and int(True) is 1, so coercion here would quietly
    compare against the wrong revision."""
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "ident", cr.SELECTED, "scan-1", 0)
    for bad in (0.9, 1.0, True, "0", [0]):
        with pytest.raises(cr.StaleWrite) as refused:
            store.decide("owner-a", "ident", cr.REJECTED, "scan-1", bad)
        assert refused.value.code == cr.MALFORMED_REVISION
    assert store.get("ident") == (cr.SELECTED, 1), "none of them may touch the record"


def test_expected_revision_none_is_still_available_to_internal_callers(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    assert store.decide("owner-a", "ident", cr.SELECTED, "scan-1", None) == (cr.SELECTED, 1)


def test_a_refused_revision_appends_nothing_to_the_journal(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "ident", cr.SELECTED, "scan-1", 0)
    journal = os.path.join(store.dir, "review-journal.jsonl")
    with open(journal, "rb") as handle:
        before = handle.read()
    for bad in (0, 2, 7):
        with pytest.raises(cr.StaleWrite):
            store.decide("owner-a", "ident", cr.REJECTED, "scan-1", bad)
    with open(journal, "rb") as handle:
        assert handle.read() == before, "a refused decision must not reach the journal"
    assert store.get("ident") == (cr.SELECTED, 1)


# ---- F4: journal write durability ----------------------------------------------------------

def test_a_positive_short_write_is_completed_rather_than_reported_as_done(tmp_path, monkeypatch):
    """F4. os.write may accept fewer bytes than offered and say so in its return value with no
    exception. The original code discarded that value, so the API reported a decision recorded
    while a truncated record sat on disk."""
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    real_write = os.write
    calls = []

    def dribble(handle, data):
        calls.append(len(data))
        return real_write(handle, data[:9])

    monkeypatch.setattr(os, "write", dribble)
    assert store.decide("owner-a", "ident", cr.SELECTED, "scan-1", 0) == (cr.SELECTED, 1)
    monkeypatch.undo()
    assert len(calls) > 3, "the write really was short"
    assert cr.ReviewStore(str(tmp_path / "state")).get("ident") == (cr.SELECTED, 1), (
        "a reported success must mean a complete, recoverable record")


def test_a_short_write_then_an_oserror_is_a_persistence_error(tmp_path, monkeypatch):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    real_write = os.write
    state = {"n": 0}

    def short_then_fail(handle, data):
        state["n"] += 1
        if state["n"] == 1:
            return real_write(handle, data[:20])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "write", short_then_fail)
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "ident", cr.SELECTED, "scan-1", 0)
    monkeypatch.undo()
    assert "after 20 byte(s)" in str(failure.value), "the byte count belongs in the message"
    assert store.get("ident") == (cr.PENDING, 0), "no visible decision"
    assert cr.ReviewStore(str(tmp_path / "state")).get("ident") == (cr.PENDING, 0)


def test_a_zero_byte_write_raises_instead_of_looping_forever(tmp_path, monkeypatch):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    monkeypatch.setattr(os, "write", lambda handle, data: 0)
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "ident", cr.SELECTED, "scan-1", 0)
    monkeypatch.undo()
    assert "accepted no bytes" in str(failure.value)
    assert cr.ReviewStore(str(tmp_path / "state")).get("ident") == (cr.PENDING, 0)


def test_an_unterminated_tail_cannot_swallow_the_next_record(tmp_path):
    """F4, the sharper half. The journal is O_APPEND and a record is one line, so an append
    landing after a newline-less tail used to merge with it into a single unparsable line --
    destroying a *later, fully successful* decision. The docstring claimed a torn write could
    only damage the last line; it could damage the next one too."""
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "committed", cr.SELECTED, "scan-1", 0)
    journal = os.path.join(store.dir, "review-journal.jsonl")

    # what a power loss mid-write leaves behind
    with open(journal, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')

    restarted = cr.ReviewStore(directory)
    restarted.begin_scan("owner-a", "scan-1")
    assert restarted.decide("owner-a", "later", cr.REJECTED, "scan-1", 0) == (cr.REJECTED, 1)
    assert restarted.journal_tails_repaired >= 1

    recovered = cr.ReviewStore(directory)
    assert recovered.get("later") == (cr.REJECTED, 1), (
        "the later decision reported success, so it must survive")
    assert recovered.get("committed") == (cr.SELECTED, 1), "committed records are preserved"
    assert recovered.get("torn") == (cr.PENDING, 0), "the torn record never reported success"
    assert recovered.corrupt_records_discarded == 1, "exactly one line was damaged"


def test_every_journal_line_stays_independently_parseable_after_a_repair(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    with open(os.path.join(store.dir, "review-journal.jsonl"), "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn"')
    store.decide("owner-a", "a", cr.SELECTED, "scan-1", 0)
    store.decide("owner-a", "b", cr.REJECTED, "scan-1", 0)
    with open(os.path.join(store.dir, "review-journal.jsonl"), "rb") as handle:
        lines = [line for line in handle.read().splitlines() if line.strip()]
    parsed = 0
    for line in lines:
        try:
            json.loads(line)
            parsed += 1
        except ValueError:
            pass
    assert parsed == len(lines) - 1, "only the one damaged line is unparseable"


def test_compaction_recovers_after_a_damaged_tail(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "kept", cr.SELECTED, "scan-1", 0)
    with open(os.path.join(store.dir, "review-journal.jsonl"), "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn"')
    store.decide("owner-a", "also", cr.REJECTED, "scan-1", 0)
    store.compact()
    with open(os.path.join(store.dir, "review-journal.jsonl"), "rb") as handle:
        assert handle.read() == b"", "compaction truncates the journal"
    after = cr.ReviewStore(directory)
    assert after.get("kept") == (cr.SELECTED, 1)
    assert after.get("also") == (cr.REJECTED, 1)
    assert after.get("torn") == (cr.PENDING, 0)
    after.begin_scan("owner-a", "scan-2")
    assert after.decide("owner-a", "fresh", cr.SELECTED, "scan-2", 0) == (cr.SELECTED, 1)


# ---- N2: the journal tail probe fails CLOSED ------------------------------------------------
#
# The independent R5-P4 review found that `_journal_tail_is_terminated` answered `True` on an
# `OSError` -- "already terminated" -- so `_terminate_damaged_tail` skipped the repair and the
# append landed against a possibly damaged tail. Measured: `decide()` returned ('REJECTED', 1),
# 112 bytes were appended into the damaged line, and the decision recovered as PENDING after a
# restart. That is the F4 defect reinstated whenever the probe hits an I/O fault.
#
# Fault injection below is deliberately NARROW. It is scoped to the module under test -- its own
# `os.path.getsize` and its own `open` name, which Python resolves from module globals before
# builtins -- and every injected callable DELEGATES for any path that is not this test's own
# disposable journal. A global patch would break the descriptor-identity probe and make the
# handler refuse with 409 before the journal was ever reached, which is the trap recorded in
# `test_clips_media_server.py::test_a_decision_that_cannot_be_persisted_returns_503_not_200`.

def _journal_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _blind_getsize(journal, errno_=5):
    """`os.path.getsize` that raises for exactly one file and delegates for every other."""
    real = os.path.getsize

    def getsize(path):
        if _same_file(path, journal):
            raise OSError(errno_, "simulated I/O error reading the journal size")
        return real(path)
    return getsize


def _blind_tail_read(journal, errno_=5):
    """`open` that returns a reader whose `read` raises, for exactly one file."""
    real = open

    class _Unreadable:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

        def seek(self, *args, **kwargs):
            return self._handle.seek(*args, **kwargs)

        def read(self, *args, **kwargs):
            raise OSError(errno_, "simulated I/O error reading the journal tail")

    def opener(file, *args, **kwargs):
        if _same_file(file, journal):
            return _Unreadable(real(file, *args, **kwargs))
        return real(file, *args, **kwargs)
    return opener


def _store_with_a_committed_record(tmp_path):
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "committed", cr.SELECTED, "scan-1", 0)
    return directory, store


def test_a_journal_tail_that_cannot_be_sized_refuses_the_decision(tmp_path, monkeypatch):
    """N2, case A. An `OSError` from `os.path.getsize` is not an answer about the tail."""
    directory, store = _store_with_a_committed_record(tmp_path)
    before = _journal_bytes(store.journal_path)

    monkeypatch.setattr(cr.os.path, "getsize", _blind_getsize(store.journal_path))
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert "could not verify the review journal tail" in str(failure.value)
    assert _journal_bytes(store.journal_path) == before, "not one byte may be appended"
    assert store.get("later") == (cr.PENDING, 0), "no in-memory decision or revision"
    reloaded = cr.ReviewStore(directory)
    assert reloaded.get("later") == (cr.PENDING, 0), "nothing was reported, nothing recovered"
    assert reloaded.get("committed") == (cr.SELECTED, 1), "committed records are preserved"


def test_a_journal_tail_that_cannot_be_read_refuses_the_decision(tmp_path, monkeypatch):
    """N2, case B. The same fail-closed outcome when the failure is in the tail READ rather than
    in the size check -- the probe has two I/O steps and both must fail closed."""
    directory, store = _store_with_a_committed_record(tmp_path)
    before = _journal_bytes(store.journal_path)

    monkeypatch.setattr(cr, "open", _blind_tail_read(store.journal_path), raising=False)
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert "could not verify the review journal tail" in str(failure.value)
    assert _journal_bytes(store.journal_path) == before
    assert store.get("later") == (cr.PENDING, 0)
    assert cr.ReviewStore(directory).get("later") == (cr.PENDING, 0)


def test_the_refusal_message_does_not_disclose_the_journal_path(tmp_path, monkeypatch):
    """This message reaches the HTTP client in the 503 body, so it carries the errno rather than
    the exception text -- an `OSError` from `getsize` stringifies with the filename in it."""
    directory, store = _store_with_a_committed_record(tmp_path)
    monkeypatch.setattr(cr.os.path, "getsize", _blind_getsize(store.journal_path))
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()
    message = str(failure.value)
    # [R5-P7] `_mentions` rather than `in`: `str(OSError)` reprs its filenames, so a literal
    # substring test passes against a message that really is disclosing the path.
    assert not _mentions(message, store.journal_path)
    assert "review-journal" not in message and ".jsonl" not in message
    assert os.path.basename(directory) not in message
    assert "errno 5" in message, "the condition stays diagnosable without the path"


def test_a_damaged_tail_plus_an_inspection_error_is_refused_not_committed(tmp_path, monkeypatch):
    """N2, case C -- the exact R5-P4 reproduction.

    A genuine unterminated record, then the inspection error. Before the fix this returned
    ('REJECTED', 1) and recovered as PENDING: a success reported for a record that does not exist.
    """
    directory, store = _store_with_a_committed_record(tmp_path)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')
    before = _journal_bytes(store.journal_path)
    assert not before.endswith(b"\n"), "the tail really is unterminated"

    monkeypatch.setattr(cr.os.path, "getsize", _blind_getsize(store.journal_path))
    with pytest.raises(cr.PersistenceError):
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert _journal_bytes(store.journal_path) == before, (
        "the append must not land against the damaged tail")
    assert store.journal_tails_repaired == 0, "no repair may be written to an unverifiable file"
    recovered = cr.ReviewStore(directory)
    assert recovered.get("later") == (cr.PENDING, 0)
    assert recovered.get("committed") == (cr.SELECTED, 1)


def test_a_healthy_retry_after_the_refusal_repairs_the_tail_and_persists(tmp_path, monkeypatch):
    """N2, case D. Failing closed must not leave the store wedged: once the I/O fault clears, the
    same decision succeeds, the genuinely damaged tail is repaired, and the record survives a
    restart. The revision is 1, not 2 -- the refused attempt consumed nothing."""
    directory, store = _store_with_a_committed_record(tmp_path)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')

    monkeypatch.setattr(cr.os.path, "getsize", _blind_getsize(store.journal_path))
    with pytest.raises(cr.PersistenceError):
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0) == (cr.REJECTED, 1), (
        "the refused attempt advanced no revision, so 0 is still the expected one")
    assert store.journal_tails_repaired == 1, "the genuine damage is repaired by the retry"

    recovered = cr.ReviewStore(directory)
    assert recovered.get("later") == (cr.REJECTED, 1), "a reported success must be recoverable"
    assert recovered.get("committed") == (cr.SELECTED, 1)
    assert recovered.get("torn") == (cr.PENDING, 0), "the torn record never reported success"
    assert recovered.corrupt_records_discarded == 1, "exactly one line was damaged"


def test_the_tail_probe_still_answers_normally_when_the_file_is_readable(tmp_path):
    """N2, case F. Failing closed must not make the probe pessimistic: an empty journal and a
    newline-terminated one are both 'terminated', and only a genuine unterminated tail repairs."""
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    assert not os.path.exists(store.journal_path), "nothing has been journalled yet"
    assert store._journal_tail_is_terminated() is True, (
        "a journal that does not exist has no tail to terminate -- that is a definite answer, "
        "not a failure to read one, and it is the ONLY OSError carved out of the refusal")

    store.begin_scan("owner-a", "scan-1")
    assert store._journal_tail_is_terminated() is True, "a normal append leaves a newline"
    assert store.decide("owner-a", "a", cr.SELECTED, "scan-1", 0) == (cr.SELECTED, 1)
    assert store._journal_tail_is_terminated() is True
    assert store.journal_tails_repaired == 0, "nothing to repair on the happy path"

    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn"')
    assert store._journal_tail_is_terminated() is False
    assert store.decide("owner-a", "b", cr.REJECTED, "scan-1", 0) == (cr.REJECTED, 1)
    assert store.journal_tails_repaired == 1
    assert cr.ReviewStore(directory).get("b") == (cr.REJECTED, 1)


def test_an_unverifiable_tail_also_refuses_begin_scan_rather_than_half_recording_it(tmp_path,
                                                                                    monkeypatch):
    """`begin_scan` journals too, so it must fail closed the same way. The GUI already turns a
    `PersistenceError` from the scan into an honest empty state rather than a half-live page."""
    directory, store = _store_with_a_committed_record(tmp_path)
    before = _journal_bytes(store.journal_path)
    monkeypatch.setattr(cr.os.path, "getsize", _blind_getsize(store.journal_path))
    with pytest.raises(cr.PersistenceError):
        store.begin_scan("owner-a", "scan-2")
    monkeypatch.undo()
    assert _journal_bytes(store.journal_path) == before
    assert store.current_scan("owner-a") == "scan-1", "the scan pointer did not move"


# ---- N6: `FileNotFoundError` is not proof that the journal is empty -------------------------
#
# R5-P5 carved `FileNotFoundError` out of the refusal above and justified it with "`_append`
# opens with O_CREAT before calling this, so the production path never sees it". The independent
# R5-P6 review measured both halves of that and both were wrong:
#
#   * `FileNotFoundError` is the class CPython maps SIX Windows codes onto, and five of them say
#     "the pathname did not resolve", not "the file is not there". A share that stopped answering
#     or a volume that went away raises them while the journal is still on disk with a torn tail,
#     so the carve-out reinstated the original false success for every one of them -- through all
#     three of the probe's I/O steps, not just the size check.
#   * The one code it was designed for, ERROR_FILE_NOT_FOUND, is the one that cannot occur there:
#     with the append descriptor open Windows refuses to unlink or rename the file at all, and if
#     the name had already gone, O_CREAT would recreate it and `getsize` would answer 0 honestly.
#
# So the carve-out is conditioned on the open descriptor, and these tests pin that. `os.path.
# exists`/`lexists` are deliberately NOT the discriminator: they answer "no" for an unresolvable
# path just as readily, which is the confusion being removed.

#: Asserted by construction in `_winerror`, so this table cannot drift from the interpreter.
_FILE_NOT_FOUND_WINERRORS = (
    (2, "ERROR_FILE_NOT_FOUND"),
    (3, "ERROR_PATH_NOT_FOUND"),
    (15, "ERROR_INVALID_DRIVE"),
    (53, "ERROR_BAD_NETPATH"),
    (67, "ERROR_BAD_NET_NAME"),
    (161, "ERROR_BAD_PATHNAME"),
)


def _winerror(code, name):
    """An `OSError` carrying a Windows native code, classified as the interpreter classifies it."""
    failure = OSError(0, f"simulated {name}", None, code)
    assert isinstance(failure, FileNotFoundError), (
        f"winerror {code} ({name}) is expected to map to FileNotFoundError; if the interpreter "
        f"stops doing that, this table -- not the implementation -- is what needs revisiting")
    return failure


def _blind_tail(monkeypatch, journal, step, failure):
    """Fail exactly one of the tail probe's three I/O steps, for exactly one file.

    Narrow by DELEGATION, not by scope. `cr.os.path` **is** `os.path`, so patching `getsize`
    through it is a process-global patch; what keeps everything else working is that each
    replacement delegates for any path that is not this test's own disposable journal. Without
    that, the descriptor-identity probe fails first and the endpoint refuses with 409 before the
    journal is ever reached -- the trap already recorded in `test_clips_media_server.py`.
    """
    real_getsize = os.path.getsize
    real_open = open

    class _Blinded:
        """A reader whose `read` fails; `seek` still works, so the failure is in the last step."""

        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

        def seek(self, *args, **kwargs):
            return self._handle.seek(*args, **kwargs)

        def read(self, *args, **kwargs):
            raise failure

    def getsize(path):
        if step == "size" and _same_file(path, journal):
            raise failure
        return real_getsize(path)

    def opener(file, *args, **kwargs):
        if _same_file(file, journal):
            if step == "open":
                raise failure
            if step == "read":
                return _Blinded(real_open(file, *args, **kwargs))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(cr.os.path, "getsize", getsize)
    monkeypatch.setattr(cr, "open", opener, raising=False)


@pytest.mark.parametrize("step", ["size", "open", "read"])
@pytest.mark.parametrize("code,name", _FILE_NOT_FOUND_WINERRORS)
def test_a_file_not_found_during_tail_inspection_refuses_the_decision(tmp_path, monkeypatch,
                                                                      step, code, name):
    """N6. Every `FileNotFoundError`-classified code, in every I/O step, must fail closed.

    Against the original implementation each of these eighteen cases returned ('REJECTED', 1),
    merged ~112 bytes into the damaged line and recovered as PENDING after a restart.
    """
    directory, store = _store_with_a_committed_record(tmp_path)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')
    before = _journal_bytes(store.journal_path)
    assert not before.endswith(b"\n"), "the tail really is unterminated"

    _blind_tail(monkeypatch, store.journal_path, step, _winerror(code, name))
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert "could not verify the review journal tail" in str(failure.value)
    assert _journal_bytes(store.journal_path) == before, "not one byte may be appended"
    assert store.journal_tails_repaired == 0, "no repair against an unverifiable file"
    assert store.get("later") == (cr.PENDING, 0), "no in-memory decision or revision"

    reloaded = cr.ReviewStore(directory)
    assert reloaded.get("later") == (cr.PENDING, 0), "nothing reported, nothing recovered"
    assert reloaded.get("committed") == (cr.SELECTED, 1), "committed records are preserved"
    assert reloaded.get("torn") == (cr.PENDING, 0), "the torn record never claimed success"


def test_the_open_append_descriptor_is_what_forbids_the_missing_journal_answer(tmp_path,
                                                                              monkeypatch):
    """The discriminator is the descriptor, not the error class and not an existence check.

    Same store, same absent journal, same `FileNotFoundError`: without a handle it is a definite
    answer, with one it is a refusal. This is the whole of the N6 fix, stated once.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    assert not os.path.exists(store.journal_path)

    assert store._journal_tail_is_terminated() is True, (        # noqa: SLF001 - white box
        "a direct read-only probe on an absent journal has no tail to terminate")

    handle = os.open(str(tmp_path / "stand-in"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        with pytest.raises(cr.PersistenceError) as failure:
            store._journal_tail_is_terminated(handle)           # noqa: SLF001 - white box
    finally:
        os.close(handle)
    assert "could not verify the review journal tail" in str(failure.value)


def test_a_healthy_retry_after_a_file_not_found_refusal_keeps_the_original_revision(tmp_path,
                                                                                    monkeypatch):
    """Failing closed must not wedge the store or consume the operator's expected revision."""
    directory, store = _store_with_a_committed_record(tmp_path)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')

    _blind_tail(monkeypatch, store.journal_path, "size", _winerror(53, "ERROR_BAD_NETPATH"))
    with pytest.raises(cr.PersistenceError):
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0) == (cr.REJECTED, 1), (
        "the refused attempt advanced no revision, so 0 is still the expected one")
    assert store.journal_tails_repaired == 1, "the genuine damage is repaired by the retry"

    reloaded = cr.ReviewStore(directory)
    assert reloaded.get("later") == (cr.REJECTED, 1), "a reported success must be recoverable"
    assert reloaded.get("committed") == (cr.SELECTED, 1)
    assert reloaded.corrupt_records_discarded == 1, "exactly one line was damaged"


# ---- The 503 body carries numbers, never a filesystem path ----------------------------------
#
# Independently confirmed by R5-P6 and pre-existing: `_append`'s open-failure message
# interpolated `{exc}`, and `str(OSError)` appends `filename` (and `filename2`) whenever the
# failing call was made by pathname -- so an authorized operator's decision request could receive
# the journal's absolute path in the 503 body. Reproduced there with no fault injection at all,
# via a read-only ACL and via another process holding the file with no sharing.

def _path_bearing(errno_, winerror, journal):
    """An `OSError` shaped the way a real pathname call raises one: filenames attached."""
    return OSError(errno_, "simulated failure", journal, winerror, journal + ".second")


def _mentions(haystack, needle):
    """Backslash-insensitive containment, for proving a path is NOT being disclosed.

    `str(OSError)` runs `repr()` over `filename` and `filename2`, which doubles every backslash,
    and a JSON body doubles them again. So the obvious `path in message` test passes against a
    message that really does carry the path -- measured, and it is why this helper exists rather
    than a plain `in`. Comparing with the separators stripped is immune to however many layers of
    escaping sit in between.
    """
    return needle.replace("\\", "").replace("/", "") in \
        haystack.replace("\\", "").replace("/", "")


def test_the_io_detail_helper_reports_numbers_and_never_the_exception_text():
    """The single formatter every client-visible persistence message goes through."""
    journal = r"C:\app\state\clips-review\review-journal.jsonl"
    detail = cr._io_detail(_path_bearing(13, 5, journal))       # noqa: SLF001 - white box
    assert detail == "errno 13, winerror 5"
    assert not _mentions(detail, journal)
    assert "review-journal" not in detail and ".jsonl" not in detail
    assert "simulated failure" not in detail, "not the exception's own text either"

    # errno alone when there is no native code, and an honest word when there is no errno.
    assert cr._io_detail(OSError(28, "no space")) == "errno 28"  # noqa: SLF001
    assert cr._io_detail(Exception("not an OSError")) == "errno unknown"  # noqa: SLF001


@pytest.mark.parametrize("failing", ["open", "write", "fsync", "tail"])
def test_no_persistence_failure_discloses_the_journal_path(tmp_path, monkeypatch, failing):
    """Every `PersistenceError` `_append` can raise reaches the browser. None may carry a path.

    `winerror 53` is used for the tail case so this also covers the N6 refusal, whose message is
    new in this change.
    """
    directory, store = _store_with_a_committed_record(tmp_path)
    journal = store.journal_path
    state_directory = os.path.dirname(journal)

    if failing == "tail":
        _blind_tail(monkeypatch, journal, "size", _winerror(53, "ERROR_BAD_NETPATH"))
    else:
        real_open, real_write, real_fsync = os.open, os.write, os.fsync

        def opener(path, flags, mode=0o777):
            if failing == "open" and _same_file(path, journal):
                raise _path_bearing(13, 5, journal)
            return real_open(path, flags, mode)

        def writer(descriptor, payload):
            if failing == "write":
                raise _path_bearing(28, 39, journal)
            return real_write(descriptor, payload)

        def flusher(descriptor):
            if failing == "fsync":
                raise _path_bearing(5, 1117, journal)
            return real_fsync(descriptor)

        monkeypatch.setattr(cr.os, "open", opener)
        monkeypatch.setattr(cr.os, "write", writer)
        monkeypatch.setattr(cr.os, "fsync", flusher)

    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    message = str(failure.value)
    assert not _mentions(message, journal), f"the journal path reached the client: {message}"
    assert not _mentions(message, journal + ".second"), "nor may filename2"
    assert not _mentions(message, state_directory)
    assert "review-journal" not in message and ".jsonl" not in message
    assert os.path.basename(directory) not in message
    assert "simulated failure" not in message, "no raw OSError text"
    assert "errno " in message, "the condition stays diagnosable"


def test_the_byte_count_survives_the_redaction(tmp_path, monkeypatch):
    """Redacting the path must not cost the one diagnostic that identifies a torn write."""
    directory, store = _store_with_a_committed_record(tmp_path)
    real_write = os.write
    calls = {"n": 0}

    def stuttering(descriptor, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(descriptor, payload[:20])
        raise _path_bearing(28, 39, store.journal_path)

    monkeypatch.setattr(cr.os, "write", stuttering)
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()

    assert "after 20 byte(s)" in str(failure.value), "the byte count belongs in the message"
    assert not _mentions(str(failure.value), store.journal_path)
    assert ".jsonl" not in str(failure.value)


def test_the_refusal_message_names_the_native_code_when_errno_is_ambiguous(tmp_path,
                                                                           monkeypatch):
    """Windows collapses distinct conditions onto one errno, so the native code is the useful one.

    `ERROR_BAD_NETPATH` and a genuinely missing file are both `errno 2`. Without the `winerror`
    the operator cannot tell "the share went away" from "there is no journal".
    """
    directory, store = _store_with_a_committed_record(tmp_path)
    _blind_tail(monkeypatch, store.journal_path, "size", _winerror(53, "ERROR_BAD_NETPATH"))
    with pytest.raises(cr.PersistenceError) as failure:
        store.decide("owner-a", "later", cr.REJECTED, "scan-1", 0)
    monkeypatch.undo()
    assert "errno 2" in str(failure.value)
    assert "winerror 53" in str(failure.value)


def test_concurrent_decisions_keep_revisions_unique_and_contiguous(tmp_path):
    store = cr.ReviewStore(str(tmp_path / "state"))
    store.begin_scan("owner-a", "scan-1")
    accepted, failures = [], []

    def hammer():
        for _ in range(10):
            while True:
                _, revision = store.get("shared")
                try:
                    accepted.append(
                        store.decide("owner-a", "shared", cr.SELECTED, "scan-1", revision)[1])
                    break
                except cr.StaleWrite:
                    continue                    # lost the race, re-read -- the designed flow
                except Exception as error:      # noqa: BLE001
                    failures.append(error)
                    return

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not failures, failures[:2]
    assert sorted(accepted) == list(range(1, 41)), "revisions are unique and contiguous"
    assert store.get("shared")[1] == 40
    assert cr.ReviewStore(str(tmp_path / "state")).get("shared") == (cr.SELECTED, 40)


# ---- F6: atomic poster publication ---------------------------------------------------------

@pytest.mark.parametrize("payload,complete", [
    (b"", False),
    (b"\xff", False),
    (b"\xff\xd8", False),
    (b"\xff\xd8\xff", False),                      # start-of-image only: a truncated poster
    (b"hello world" * 8, False),
    (b"\xff\xd9", False),                          # end marker without a start
    # The REALISTIC truncation: a killed FFmpeg has written several KB of a valid JPEG and then
    # died, so the header is right, the file is far past any length threshold, and only the
    # end-of-image marker is missing. A mutation control found that the cases above all failed
    # for other reasons (too short, or a bad header), so none of them actually exercised the EOI
    # check -- this one does, and it is the shape the defect took in production.
    (b"\xff\xd8\xff\xdb" + b"\x7f" * 4096, False),
    (b"\xff\xd8\xff\xdb" + b"\x7f" * 4096 + b"\xff\xd9", True),
    # A bad header WITH a valid end marker. Nothing above exercised the header clause on its own,
    # so removing it went undetected until a mutation control said so.
    (b"this is not a jpeg at all" + b"\xff\xd9", False),
    # Four bytes in which the start and end markers OVERLAP. Without the length floor this reads
    # as a complete JPEG, because `seek(-2, END)` lands inside the header.
    (b"\xff\xd8\xff\xd9", False),
    (_JPEG, True),
])
def test_is_complete_jpeg_distinguishes_finished_from_truncated(tmp_path, payload, complete):
    """F6. The old cache test was getsize(destination) > 0, which cannot tell a finished poster
    from one a killed FFmpeg stopped half way -- so a truncated poster counted as a permanent
    hit and that clip showed a broken image forever."""
    path = _make(str(tmp_path / "candidate.jpg"), payload)
    assert cr.is_complete_jpeg(path) is complete


def test_is_complete_jpeg_is_false_for_a_missing_file(tmp_path):
    assert cr.is_complete_jpeg(str(tmp_path / "nope.jpg")) is False


def test_a_truncated_published_poster_is_a_miss_and_is_regenerated(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    cache = str(tmp_path / "posters")
    os.makedirs(cache, exist_ok=True)
    entry = entries[0]
    destination = cr.poster_path_for(cache, entry)
    _make(destination, b"\xff\xd8\xff")            # what a killed FFmpeg leaves behind

    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        with open(argv[-1], "wb") as handle:
            handle.write(_JPEG)
        return 0

    outcome = cr.ensure_posters([entry], cache, "ffmpeg", runner, workers=1)
    assert (outcome.hits, outcome.generated) == (0, 1), "a truncated poster is not a cache hit"
    assert cr.is_complete_jpeg(destination), "it healed"
    assert cr.ensure_posters([entry], cache, "ffmpeg", runner, workers=1).hits == 1


def test_ffmpeg_never_writes_the_published_poster_filename(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    cache = str(tmp_path / "posters")
    entry = entries[0]
    destinations = []

    def runner(argv, timeout):
        destinations.append(argv[-1])
        with open(argv[-1], "wb") as handle:
            handle.write(_JPEG)
        return 0

    cr.ensure_posters([entry], cache, "ffmpeg", runner, workers=1)
    published = cr.poster_path_for(cache, entry)
    assert destinations and destinations[0] != published, (
        "a killed process must not be able to leave a partial file at the cache path")
    assert destinations[0].endswith(".part.jpg")
    assert cr.is_complete_jpeg(published)


def test_a_truncated_attempt_is_never_published(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    cache = str(tmp_path / "posters")
    entry = entries[0]

    def killed(argv, timeout):
        with open(argv[-1], "wb") as handle:
            handle.write(b"\xff\xd8\xff")          # half a JPEG
        return 0                                   # and claims success anyway

    outcome = cr.ensure_posters([entry], cache, "ffmpeg", killed, workers=1)
    assert (outcome.generated, outcome.failures) == (0, 1)
    assert not os.path.exists(cr.poster_path_for(cache, entry))
    assert not [n for n in os.listdir(cache) if ".part." in n], "no attempt file is left behind"


def test_a_failing_attempt_cannot_delete_a_valid_published_poster(tmp_path, clips_tree):
    """The previous implementation ran os.unlink(destination) on failure, which could destroy a
    perfectly good published poster."""
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    cache = str(tmp_path / "posters")
    os.makedirs(cache, exist_ok=True)
    entry = entries[0]
    destination = cr.poster_path_for(cache, entry)
    _make(destination, b"\xff\xd8\xff")            # incomplete, so the pass will attempt it

    cr.ensure_posters([entry], cache, "ffmpeg", lambda argv, timeout: 1, workers=1)
    assert os.path.exists(destination), "a failing attempt must not unlink the cache path"
    with open(destination, "rb") as handle:
        assert handle.read() == b"\xff\xd8\xff", "and must not alter it either"


def test_concurrent_attempts_for_one_key_each_own_a_unique_temporary(tmp_path, clips_tree):
    registry = cr.ClipRegistry()
    files = cr.discover_clips(clips_tree, True).files
    _, _, entries = _page(registry, "owner-a", files)
    cache = str(tmp_path / "posters")
    entry = entries[0]
    seen, lock = [], threading.Lock()

    def slow(argv, timeout):
        with lock:
            seen.append(argv[-1])
        time.sleep(0.03)
        with open(argv[-1], "wb") as handle:
            handle.write(_JPEG)
        return 0

    threads = [threading.Thread(target=cr.ensure_posters,
                                args=([entry], cache, "ffmpeg", slow), kwargs={"workers": 1})
               for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert len(seen) >= 2 and len(set(seen)) == len(seen), "every attempt had its own name"
    assert cr.is_complete_jpeg(cr.poster_path_for(cache, entry))
    assert not [n for n in os.listdir(cache) if ".part." in n]


def test_poster_attempt_paths_are_exclusively_created_and_distinct(tmp_path):
    cache = str(tmp_path / "posters")
    os.makedirs(cache)
    destination = os.path.join(cache, "a" * 32 + ".jpg")
    first = cr.poster_attempt_path(cache, destination)
    second = cr.poster_attempt_path(cache, destination)
    assert first != second
    assert os.path.exists(first) and os.path.exists(second)
    assert os.path.dirname(first) == cache, "same directory, so os.replace is atomic"


def test_pruning_leaves_in_flight_attempt_files_alone(tmp_path):
    cache = tmp_path / "posters"
    cache.mkdir()
    for index in range(6):
        _make(str(cache / f"{index:032x}.jpg"), _JPEG)
    attempt = cr.poster_attempt_path(str(cache), str(cache / ("b" * 32 + ".jpg")))
    removed = cr.prune_poster_cache(str(cache), limit=0)
    assert removed == 6, "all six published posters were candidates"
    assert os.path.exists(attempt), "an attempt belongs to the attempt that created it"


# =============================================================================================
# [R5-P3] The composed gallery script must be VALID JavaScript
#
# This section exists because of a defect R5-P3 introduced and the real browser caught: the
# Confirm hint is operator text from `ui_content.py`, it contains an apostrophe ("the server's
# record"), and it was substituted *bare* into a single-quoted JS literal. The apostrophe closed
# the literal, Gradio's `new AsyncFunction` threw `SyntaxError: Unexpected identifier 's'`, and
# the ENTIRE gallery script failed to compile -- no hover, no clicks, no decisions, and no visible
# error anywhere in the Python layer.
#
# Asserting "the placeholder is present" was not enough, and neither was the full suite. What was
# missing is a check on the *composed* result, which is what these tests add.
# =============================================================================================

def _compose_gallery_js():
    """Build CLIPS_GALLERY_JS exactly as `gui.py` does, without importing gui.py."""
    import json as _json

    gui_source = open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read()
    ui_source = open(os.path.join(_SRC, "ui_content.py"), encoding="utf-8").read()

    namespace = {"json": _json}
    for node in ast.parse(ui_source).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.startswith("CLIPS_"):
            try:
                namespace[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass

    assign = next(node for node in ast.parse(gui_source).body
                  if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], "id", "") == "CLIPS_GALLERY_JS")
    return eval(                                        # noqa: S307 - the expression under test
        compile(ast.Expression(assign.value), "<gallery-js>", "eval"), namespace)


def test_operator_text_enters_the_gallery_script_as_a_json_literal():
    """The substitution must emit a complete, escaped literal -- not raw text inside quotes."""
    gui_source = open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read()
    assert ".replace('__REFRESH_HINT__', json.dumps(CLIPS_REFRESH_TO_ENABLE_HINT))" in gui_source
    assert "'__REFRESH_HINT__'" not in gui_source.split("CLIPS_GALLERY_JS = r\"\"\"")[1] \
        .split('""".replace(')[0], (
        "the placeholder must NOT be wrapped in JS quotes: json.dumps supplies them, and quoting "
        "it again is what let an apostrophe in the wording close the literal")


def test_the_composed_gallery_script_is_syntactically_valid_javascript():
    """Parsed by a real JavaScript engine when one is available, and structurally otherwise.

    Node is not a dependency of this project, so its absence is a skip rather than a failure --
    but the structural half below runs everywhere and would have caught the actual defect.
    """
    import shutil
    import subprocess

    composed = _compose_gallery_js()
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; the structural assertions below still apply")
    probe = f"const f = {composed};\nif (typeof f !== 'function') throw new Error('not a fn');\n"
    result = subprocess.run([node, "--input-type=module", "-e", probe],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"the composed gallery script is not valid JavaScript:\n{result.stderr[:800]}")


def test_no_substituted_operator_text_can_close_a_javascript_literal():
    """Structural, engine-free: every interpolated constant must survive quote characters.

    Checks the composed script rather than the template, because the template is correct by
    construction and the composed result is where the damage appeared.
    """
    composed = _compose_gallery_js()
    ui_source = open(os.path.join(_SRC, "ui_content.py"), encoding="utf-8").read()
    hint = next(ast.literal_eval(node.value) for node in ast.parse(ui_source).body
                if isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "CLIPS_REFRESH_TO_ENABLE_HINT")
    assert "'" in hint, (
        "this test is only meaningful while the wording contains an apostrophe; if the wording "
        "changed, keep a quote in it or this guard silently stops guarding anything")
    assert hint not in composed, (
        "the raw text appears verbatim in the script, so it was interpolated rather than encoded")
    import json as _json
    assert _json.dumps(hint) in composed, "the encoded form must be what lands in the script"


def test_the_gallery_script_is_installed_through_app_load_unchanged():
    """The composed value is what Gradio compiles, so the registration must pass the constant
    itself rather than rebuild it at the call site."""
    gui_source = open(os.path.join(_SRC, "gui.py"), encoding="utf-8").read()
    assert "app.load(fn=None, inputs=None, outputs=None, js=CLIPS_GALLERY_JS)" in gui_source


# =============================================================================================
# [R5-P11 / N7] Start-up recovery is all-or-nothing, and an incomplete one is NEVER compacted
#
# The defect these pin was confirmed twice independently and is a **permanent loss of committed
# decisions**, not the transient under-reporting the earlier reports and rule called it.
#
# `_load` treated every read failure as corruption: an unreadable snapshot was caught in the same
# clause as a syntactically invalid one, and an `OSError` part way through the journal replay was
# swallowed with whatever had been applied so far kept. Either way the constructor returned a
# store that could not be told from a healthy one, with an EMPTY or truncated record. That store
# was fully writable and fully compactable, and `compact()` -- including the automatic one at
# COMPACT_AFTER_RECORDS -- then wrote that incomplete memory over the real snapshot and truncated
# the journal. After that the decisions were gone: the bytes that still held them had been
# replaced by a snapshot assembled from a record that was never authoritative.
#
# Measured on the pre-fix code, in the task evidence, four independent ways: a real Windows
# exclusive-sharing lock and controlled fault injection, each against the snapshot and against the
# journal, plus the real 2,000-record automatic threshold. In the sharpest of them no new decision
# was needed at all -- the append was correctly refused by the N2/N6 fix, and `compact()` alone
# destroyed three committed decisions. The retained state was mutated every time (a 369-byte
# snapshot replaced by a 146-byte one; a 419-byte journal truncated to 0). After the fix all four
# leave the snapshot and journal byte-for-byte identical.
#
# Fault injection below follows the N2/N6 convention in this file: scoped to the module under
# test, delegating for every path that is not this test's own disposable state.

def _deny_reads(monkeypatch, path, make_error):
    """`open` that raises for READ opens of exactly one file, and delegates for every other."""
    real = open
    target = os.path.normcase(os.path.abspath(path))

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            raise make_error()
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)


def _fail_part_way_through(monkeypatch, path, after_lines, errno_=5):
    """`open` whose returned handle yields `after_lines` real lines and then raises.

    The point of injecting at *iteration* rather than at `open` is that it is the only shape that
    produces a genuinely partial replay: the first records are real, applied records, and the
    failure arrives with the store half rebuilt.
    """
    real = open
    target = os.path.normcase(os.path.abspath(path))

    class _Partial:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self._handle.close()
            return False

        def __iter__(self):
            for index, line in enumerate(self._handle):
                if index >= after_lines:
                    raise OSError(errno_, "simulated I/O error part way through the replay")
                yield line

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            return _Partial(real(file, *args, **kwargs))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)


class _RealExclusiveLock:
    """A genuine Windows handle opened with **no sharing**: any other open is refused by the OS.

    Not a simulation. This is the condition the defect was reported for -- a backup, an indexer or
    an antivirus holding the file for a moment -- and it needs no injection at all, which is why
    one of these is kept in the suite rather than only in the task evidence.
    """

    def __init__(self, path):
        import ctypes

        self._ctypes = ctypes
        self.path = path
        self._handle = None
        self._kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        self._kernel.CreateFileW.restype = ctypes.c_void_p
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]

    def __enter__(self):
        handle = self._kernel.CreateFileW(self.path, 0x80000000, 0, None, 3, 0, None)
        if handle == self._ctypes.c_void_p(-1).value:
            raise OSError(self._ctypes.get_last_error(), "CreateFileW refused")
        self._handle = handle
        return self

    def __exit__(self, *exc_info):
        if self._handle is not None:
            self._kernel.CloseHandle(self._handle)
            self._handle = None
        return False


def _state_bytes(store):
    """The whole durable footprint, so "not one byte changed" is a single comparison."""
    out = {}
    for name, path in (("snapshot", store.snapshot_path), ("journal", store.journal_path)):
        try:
            with open(path, "rb") as handle:
                out[name] = handle.read()
        except FileNotFoundError:
            out[name] = None
    return out


def _committed(directory, count=3, compact=False):
    """`count` distinct decisions, each committed at revision 1. Optionally compacted."""
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    expected = {}
    for index in range(count):
        identity = f"clip-{index}"
        decision = cr.SELECTED if index % 2 else cr.REJECTED
        expected[identity] = store.decide("owner-a", identity, decision, "scan-1")
    if compact:
        store.compact()
    return store, expected


def _assert_fully_recovered(directory, expected, scan="scan-1"):
    """Every committed identity, decision and revision is back, and so is the scan pointer."""
    store = cr.ReviewStore(directory)
    for identity, value in expected.items():
        assert store.get(identity) == value, f"{identity} did not survive"
    assert store.current_scan("owner-a") == scan, "the scan state must be restored too"
    return store


# ---- 1-3: the cases that must keep working exactly as they did ------------------------------

def test_n7_case1_a_genuinely_fresh_store_still_works(tmp_path):
    """Case 1. Both files genuinely absent is the ordinary first run, not a failure.

    This is the property that makes the fix non-trivial: "absent" has to stay a usable answer
    while every *unresolved* condition fails closed.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    assert not os.path.exists(store.snapshot_path) and not os.path.exists(store.journal_path)
    assert store._recovered is True                              # noqa: SLF001 - white box
    store.begin_scan("owner-a", "scan-1")
    assert store.decide("owner-a", "clip-0", cr.SELECTED, "scan-1") == (cr.SELECTED, 1)
    store.compact()
    assert cr.ReviewStore(directory).get("clip-0") == (cr.SELECTED, 1)


def test_n7_case2_a_valid_snapshot_with_an_empty_journal_recovers(tmp_path):
    """Case 2. The post-compaction steady state: the snapshot IS the record."""
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    assert os.path.getsize(store.journal_path) == 0, "compaction emptied the journal"
    recovered = _assert_fully_recovered(directory, expected)
    assert recovered.journal_records == 0
    assert recovered.corrupt_records_discarded == 0, "a valid snapshot is not corruption"


def test_n7_case3_a_valid_snapshot_plus_later_journal_events_recovers(tmp_path):
    """Case 3. Snapshot and journal together, with the journal winning where they overlap."""
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    store.begin_scan("owner-a", "scan-2")
    expected["clip-0"] = store.decide("owner-a", "clip-0", cr.SELECTED, "scan-2")
    expected["clip-new"] = store.decide("owner-a", "clip-new", cr.REJECTED, "scan-2")
    assert os.path.getsize(store.journal_path) > 0

    recovered = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert recovered.get("clip-0") == (cr.SELECTED, 2), (
        "the journal event supersedes the snapshot and keeps its revision")
    assert recovered.journal_records == 3, "one scan plus two decisions replayed"


# ---- 4-8: every unreadable condition fails closed, and changes nothing ----------------------

@pytest.mark.parametrize("which", ["snapshot", "journal"])
def test_n7_cases4_5_a_temporarily_unreadable_state_file_refuses_the_store(tmp_path, monkeypatch,
                                                                           which):
    """Cases 4 and 5. The core reproduction, for each file in turn.

    Pre-fix this returned a store reporting every decision as `PENDING, 0`, with no exception and
    nothing said to the operator. It must raise instead -- and raise the one class the GUI and the
    HTTP boundary already handle.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=(which == "snapshot"))
    target = store.snapshot_path if which == "snapshot" else store.journal_path
    before = _state_bytes(store)

    _deny_reads(monkeypatch, target, lambda: PermissionError(13, "access denied", target, 5))
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert f"could not read the review {which}" in str(failure.value)
    assert _state_bytes(store) == before, "a refused load may not touch one byte"
    _assert_fully_recovered(directory, expected)


@pytest.mark.parametrize("which", ["snapshot", "journal"])
def test_n7_cases6_7_one_readable_file_does_not_excuse_the_other(tmp_path, monkeypatch, which):
    """Cases 6 and 7. A snapshot and a journal that both hold records, one of them unreadable.

    This is the asymmetric pair: the readable half alone looks like a plausible record, which is
    exactly why a partial recovery must not be published. The decisions that live only in the
    unreadable half would be silently absent, and the next compaction would make that permanent.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)        # three decisions in the snapshot
    store.begin_scan("owner-a", "scan-2")
    expected["clip-journal-only"] = store.decide("owner-a", "clip-journal-only",
                                                 cr.SELECTED, "scan-2")
    assert os.path.getsize(store.snapshot_path) > 0 and os.path.getsize(store.journal_path) > 0

    target = store.snapshot_path if which == "snapshot" else store.journal_path
    before = _state_bytes(store)
    _deny_reads(monkeypatch, target, lambda: PermissionError(13, "access denied", target, 5))
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert _state_bytes(store) == before
    _assert_fully_recovered(directory, expected, scan="scan-2")


def test_n7_case8_an_io_failure_part_way_through_the_replay_refuses_the_store(tmp_path,
                                                                              monkeypatch):
    """Case 8. The subtlest half of the defect: a replay that genuinely half succeeded.

    The original code's `except OSError` wrapped the whole loop and kept every record applied
    before the failure, so the store published a *prefix* of the journal as the authoritative
    record. A prefix is more dangerous than an empty set, because it looks right.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, count=4)
    before = _state_bytes(store)

    # Two lines in: the scan record and the first decision have been applied for real.
    _fail_part_way_through(monkeypatch, store.journal_path, after_lines=2)
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert "could not replay the review journal" in str(failure.value)
    assert _state_bytes(store) == before
    _assert_fully_recovered(directory, expected)


@pytest.mark.parametrize("code,name", _FILE_NOT_FOUND_WINERRORS)
def test_n7_a_file_not_found_that_is_not_absence_still_refuses(tmp_path, monkeypatch, code, name):
    """`FileNotFoundError` is not proof of absence on the READ path either.

    The same six Windows codes the N6 fix separated on the append path reach `_load`, and five of
    them mean *the pathname did not resolve* while the journal sits on disk holding every
    committed decision. The one code that does mean an absent leaf is the one that cannot be true
    here, because the directory listing still shows the file -- so all six must refuse, and the
    assertion is about the *filesystem evidence*, not about the error class.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    before = _state_bytes(store)

    def error():
        failure = FileNotFoundError(2, f"simulated {name}", store.journal_path)
        failure.winerror = code
        return failure

    _deny_reads(monkeypatch, store.journal_path, error)
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert _state_bytes(store) == before
    _assert_fully_recovered(directory, expected)


@pytest.mark.parametrize("which", ["snapshot", "journal"])
def test_n7_a_path_that_stops_resolving_is_not_absence(tmp_path, monkeypatch, which):
    """**A bare existence check is not evidence**, and this is the case that proves it.

    `os.path.exists` answers `False` for a path that stopped *resolving* -- a share that went
    away, a volume that was removed, an ACL that denies traversal -- exactly as readily as for a
    name that was never there, while the file is still on disk holding every committed decision.
    Measured directly: `os.path.exists(r'\\\\no-such-host\\share\\file.txt')` is `False`.

    Modelled here by making `exists` and `lexists` answer `False` for this one file while the
    directory listing still shows it -- which is the evidence the fix reads instead. A disposable
    mutation control that replaces the classification with `not os.path.exists(path)` passes every
    other test in this suite and fails only this one, which is why it is a separate case rather
    than a variant of the one below.

    `winerror` is deliberately not what carries this: measured on this interpreter,
    `builtins.open` sets `errno` but **no** `winerror` (only `os.path.getsize` supplies one), so
    on the real read path the decision rests entirely on the listing.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=(which == "snapshot"))
    target = store.snapshot_path if which == "snapshot" else store.journal_path
    before = _state_bytes(store)
    real_exists, real_lexists = os.path.exists, os.path.lexists

    def vanished(path, real=None):
        if _same_file(path, target):
            return False                    # the pathname no longer resolves
        return real(path)

    monkeypatch.setattr(cr.os.path, "exists", lambda path: vanished(path, real_exists))
    monkeypatch.setattr(cr.os.path, "lexists", lambda path: vanished(path, real_lexists))
    _deny_reads(monkeypatch, target,
                lambda: FileNotFoundError(2, "simulated ERROR_BAD_NETPATH", target))

    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert f"could not read the review {which}" in str(failure.value)
    assert _state_bytes(store) == before
    assert os.path.basename(target) in os.listdir(directory), (
        "the file never went anywhere -- only the pathname stopped resolving")
    _assert_fully_recovered(directory, expected)


def test_n7_absence_is_established_from_the_directory_listing_not_from_the_error(tmp_path,
                                                                                 monkeypatch):
    """The discriminator, stated once. Same error class, same code, opposite answers.

    With the file genuinely gone from the directory, `ERROR_FILE_NOT_FOUND` is a definite answer.
    With the file still listed, the identical exception is an unresolved condition. And if the
    directory itself cannot be enumerated there is no evidence either way, so it fails closed --
    which is what `os.path.exists` could never express, because it answers "no" for all three.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)

    def absent():
        failure = FileNotFoundError(2, "simulated ERROR_FILE_NOT_FOUND", store.journal_path)
        failure.winerror = 2
        return failure

    # (a) the journal IS in the listing: not absence, refuse.
    _deny_reads(monkeypatch, store.journal_path, absent)
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    monkeypatch.undo()

    # (b) the directory cannot be enumerated: no evidence, refuse.
    _deny_reads(monkeypatch, store.journal_path, absent)
    monkeypatch.setattr(cr.os, "listdir",
                        lambda path: (_ for _ in ()).throw(OSError(5, "cannot enumerate")))
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    monkeypatch.undo()

    # (c) genuinely removed, and the listing agrees: an answer, so the snapshot alone recovers.
    store.compact()
    os.unlink(store.journal_path)
    recovered = cr.ReviewStore(directory)
    for identity, value in expected.items():
        assert recovered.get(identity) == value, "the snapshot alone is a complete recovery"


# ---- 9-10: corruption tolerance is PRESERVED, and kept distinct from a failed read ----------

def test_n7_case9_a_torn_final_record_is_still_a_complete_recovery(tmp_path):
    """Case 9. A torn tail is a statement about one record's content, not a failed read.

    The distinction is the whole fix: this must stay tolerated and the store must stay writable
    and compactable, because the damaged bytes were never a committed decision in the first place.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"k":"decide","identity":"torn","dec')

    recovered = _assert_fully_recovered(directory, expected)
    assert recovered.corrupt_records_discarded == 1, "exactly the one damaged line"
    assert recovered.get("torn") == (cr.PENDING, 0), "a torn record never reported success"
    assert recovered._recovered is True                          # noqa: SLF001 - white box

    recovered.begin_scan("owner-a", "scan-2")
    assert recovered.decide("owner-a", "after", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    recovered.compact()
    _assert_fully_recovered(directory, expected, scan="scan-2")


def test_n7_case10_a_syntactically_invalid_snapshot_refuses_rather_than_guessing(tmp_path):
    """Case 10, **reversed by R5-P13 / N7-R1**, with the before/after rationale kept here.

    R5-P11 asserted that unusable snapshot *content* stays recoverable from the journal, on the
    grounds that unparsable bytes are "a definite finding about the snapshot". The finding is
    definite; the conclusion drawn from it was not. An independent review measured the sequence it
    licenses: the damaged snapshot is discarded, whatever the journal happens to hold is published
    as authoritative, and the next `compact()` writes that over the retained bytes. Every
    committed decision the journal did not happen to contain is then gone for good.

    So the tolerance is withdrawn, and this test now asserts the refusal and the preservation.
    The journal-replay coverage it used to carry has not been dropped -- see `test_n7_case1`,
    `test_n7_case2`, `test_n7_case3` and the torn-tail group, which replay real journals through
    the same code.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    assert _state_bytes(store)["snapshot"] is None, "no compaction, so there is no snapshot yet"
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        handle.write("{ this is not json")
    damaged = _state_bytes(store)

    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    assert "could not be read as a complete record" in str(failure.value)
    assert _state_bytes(store) == damaged, "a refused snapshot is not rewritten"

    # And the refusal is not a wedge: the decisions were never actually unrecoverable.
    os.unlink(store.snapshot_path)
    _assert_fully_recovered(directory, expected)


@pytest.mark.parametrize("payload", [
    '{"records": "not a mapping"}',
    '{"records": {"a": "not a record"}}',
    '{"records": {"a": {"revision": 1}}}',                       # no decision key
    '{"records": {"a": {"decision": "SELECTED", "revision": "x"}}}',
    '{"records": {"a": {"decision": "NOT_A_DECISION", "revision": 1, "scan_id": ""}}}',
    '{"records": {"a": {"decision": "SELECTED", "revision": -1, "scan_id": ""}}}',
    '{"records": {"a": {"decision": "SELECTED", "revision": true, "scan_id": ""}}}',
    '{"records": {"a": {"decision": "SELECTED", "revision": 1}}}',   # no scan_id key
    '["not", "an", "object"]',
    '{"scans": 7, "records": {}}',
    '{"scans": {"owner-a": 7}, "records": {}}',
    '',
])
def test_n7_a_structurally_invalid_snapshot_refuses_rather_than_contributing_nothing(tmp_path,
                                                                                     payload):
    """**Replaces** "contributes nothing rather than a prefix" -- which was the wrong remedy.

    Materialising the records in one expression did fix a *partial* publication, and that part is
    kept: a half-built record set is still never installed. What it did not fix is that
    contributing nothing is itself a publication -- of an empty history -- and `compact()` turns
    it into the snapshot. Each payload here is a snapshot this build cannot account for, so each
    one must refuse and leave both copies alone.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    damaged = _state_bytes(store)

    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    assert _state_bytes(store) == damaged, "not one byte of either copy may change"

    os.unlink(store.snapshot_path)
    _assert_fully_recovered(directory, expected)


# ---- 11-13: the retry, and normal service afterwards ----------------------------------------

def test_n7_cases11_12_13_a_denial_that_clears_recovers_everything_and_resumes(tmp_path,
                                                                               monkeypatch):
    """Cases 11, 12 and 13 in one sequence, because they are one operator story.

    A denial, a refusal, the denial clearing, then a healthy retry that recovers every decision at
    its original revision, restores the scan state, accepts a new decision and compacts normally.
    No journal was hand-edited and nothing had to be repaired -- which is the test that the fix is
    a refusal rather than a wedge.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    before = _state_bytes(store)

    _deny_reads(monkeypatch, store.journal_path,
                lambda: PermissionError(13, "access denied", store.journal_path, 32))
    for _ in range(3):                      # a repeated failure must stay a failure
        with pytest.raises(cr.PersistenceError):
            cr.ReviewStore(directory)
    monkeypatch.undo()

    assert _state_bytes(store) == before, "three refused attempts changed nothing"

    retried = _assert_fully_recovered(directory, expected)
    retried.begin_scan("owner-a", "scan-2")
    assert retried.decide("owner-a", "clip-fresh", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    retried.compact()
    assert os.path.getsize(retried.journal_path) == 0

    final = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert final.get("clip-fresh") == (cr.SELECTED, 1)


# ---- The destructive sequence: compaction after a failed load ------------------------------

@pytest.mark.parametrize("which", ["snapshot", "journal"])
def test_n7_the_destructive_sequence_cannot_happen(tmp_path, monkeypatch, which):
    """**The mandatory reproduction.** A failed load, then a real `compact()`.

    Pre-fix, with the snapshot denied: the store loaded empty, accepted a new decision and
    compacted, and all three originals were gone for good. With the journal denied the append was
    correctly refused by the N2/N6 fix and `compact()` destroyed them anyway -- which is why the
    guard is on `compact()` and not only on the write path.

    The corrected behaviour must *prevent* the sequence, not delay it: there is no store to
    compact, and if one is somehow held, `compact()` itself refuses.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=(which == "snapshot"))
    target = store.snapshot_path if which == "snapshot" else store.journal_path
    before = _state_bytes(store)

    _deny_reads(monkeypatch, target, lambda: PermissionError(13, "access denied", target, 5))
    try:
        incomplete = cr.ReviewStore(directory)
    except cr.PersistenceError:
        incomplete = None
    if incomplete is not None:
        # Deliberately NOT a `pytest.raises` around the construction. If the constructor ever
        # stops refusing, this branch runs the real destructive sequence so the test fails on the
        # measured loss below -- rather than passing because some other exception arrived first.
        try:
            incomplete.begin_scan("owner-a", "scan-2")
            incomplete.decide("owner-a", "clip-new", cr.SELECTED, "scan-2")
        except cr.PersistenceError:
            pass                            # the write path may refuse; compaction is the danger
        try:
            incomplete.compact()
        except (cr.PersistenceError, OSError):
            pass
    monkeypatch.undo()

    assert incomplete is None, "an incompletely recovered store must not be constructible"
    assert _state_bytes(store) == before, "not one byte of either copy may have changed"
    _assert_fully_recovered(directory, expected)


def test_n7_a_store_that_did_not_fully_recover_is_read_only(tmp_path):
    """The guard behind the refusal, exercised directly.

    `compact()` neither reads nor needs the files it replaces, so "raise from `_load`" is not by
    itself sufficient -- anything holding a store whose recovery was incomplete could still turn
    memory into the snapshot. Both mutating entry points therefore check the recovery flag.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    before = _state_bytes(store)
    store._recovered = False                                     # noqa: SLF001 - white box

    with pytest.raises(cr.PersistenceError) as compaction:
        store.compact()
    assert "not completely recovered" in str(compaction.value)
    with pytest.raises(cr.PersistenceError):
        store.begin_scan("owner-a", "scan-2")
    with pytest.raises(cr.PersistenceError):
        store.decide("owner-a", "clip-new", cr.SELECTED, "scan-1")
    store.journal_records = cr.COMPACT_AFTER_RECORDS
    with pytest.raises(cr.PersistenceError):
        store.maybe_compact()

    assert _state_bytes(store) == before, "a read-only store writes nothing at all"
    store._recovered = True                                      # noqa: SLF001 - white box
    _assert_fully_recovered(directory, expected)


def test_n7_the_real_automatic_compaction_threshold_is_preserved_and_gated(tmp_path):
    """Case: the REAL `maybe_compact()` threshold, from a real journal of that many records.

    Two halves, because both matter. A completely recovered store must still compact at
    COMPACT_AFTER_RECORDS -- the fix must not disable the housekeeping. And a store whose recovery
    was incomplete must refuse at exactly the same point, which is the automatic route by which
    the loss happened with no operator action at all.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    # Real records in the real format, written in one call rather than 2,000 fsync'd appends.
    # The counter under test is `journal_records`, and it is derived from the replay.
    with open(store.journal_path, "a", encoding="utf-8") as handle:
        for index in range(cr.COMPACT_AFTER_RECORDS):
            handle.write(json.dumps({"k": "scan", "owner": "owner-a",
                                     "scan_id": "scan-1", "at": 0.0},
                                    separators=(",", ":")) + "\n")

    loaded = cr.ReviewStore(directory)
    assert loaded.journal_records >= cr.COMPACT_AFTER_RECORDS, "the threshold is really reached"

    gated = cr.ReviewStore(directory)
    gated._recovered = False                                     # noqa: SLF001 - white box
    before = _state_bytes(gated)
    with pytest.raises(cr.PersistenceError):
        gated.maybe_compact()
    assert _state_bytes(gated) == before, "the automatic route destroys nothing either"

    assert loaded.maybe_compact() is True, "a recovered store still compacts automatically"
    assert loaded.journal_records == 0
    _assert_fully_recovered(directory, expected)


# ---- A real OS lock, with no injection at all ----------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows exclusive sharing modes only")
@pytest.mark.parametrize("which", ["snapshot", "journal"])
def test_n7_a_real_windows_exclusive_lock_refuses_the_store_and_loses_nothing(tmp_path, which):
    """The reported condition itself: another process holding the file with no sharing.

    No monkeypatching anywhere. A backup, an indexer or an antivirus is enough to produce this,
    and pre-fix it was sufficient on its own to lose every committed decision once anything
    compacted afterwards.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=(which == "snapshot"))
    target = store.snapshot_path if which == "snapshot" else store.journal_path
    before = _state_bytes(store)

    with _RealExclusiveLock(target):
        with pytest.raises(cr.PersistenceError) as failure:
            cr.ReviewStore(directory)
        assert f"could not read the review {which}" in str(failure.value)

    assert _state_bytes(store) == before, "the retained bytes are untouched"
    _assert_fully_recovered(directory, expected)


# ---- The refusal is reportable: a typed class, and no path in the message -------------------

def test_n7_the_refusal_is_a_persistence_error_and_discloses_no_path(tmp_path, monkeypatch):
    """Every one of these messages can reach the browser in the 503 body and the status box.

    `PersistenceError` is the class both boundaries already handle, and `_io_detail` is the one
    formatter -- so the numbers survive and the journal's absolute path does not. Compared with
    the separators stripped, because `str(OSError)` reprs its filenames (R5-P7).
    """
    directory = str(tmp_path / "state")
    store, _ = _committed(directory, compact=True)

    for which, target in (("snapshot", store.snapshot_path), ("journal", store.journal_path)):
        _deny_reads(monkeypatch, target,
                    lambda target=target: _path_bearing(13, 5, target))
        with pytest.raises(cr.PersistenceError) as failure:
            cr.ReviewStore(directory)
        monkeypatch.undo()
        message = str(failure.value)
        assert not _mentions(message, target), f"a path reached the client: {message}"
        assert not _mentions(message, directory)
        assert "review-journal" not in message and "review-snapshot" not in message
        assert ".jsonl" not in message and ".json" not in message
        assert "simulated failure" not in message, "nor the raw OSError text"
        assert "errno 13, winerror 5" in message, "and it stays diagnosable"
        assert f"could not read the review {which}" in message


def test_n7_an_unusable_state_directory_is_also_a_persistence_error(tmp_path, monkeypatch):
    """The constructor's other I/O step. One typed class for every durability failure means the
    GUI and the 503 boundary need exactly one `except` between them."""
    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "cannot create", path, 5)

    monkeypatch.setattr(cr.os, "makedirs", refuse)
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(str(tmp_path / "state"))
    monkeypatch.undo()
    assert "could not open the review state directory" in str(failure.value)
    assert not _mentions(str(failure.value), str(tmp_path))


def test_n7_concurrent_decisions_after_a_recovery_keep_revision_order(tmp_path):
    """Case 15, after a real recovery rather than on a fresh store.

    The staged replay installs a new `_records` dict on the instance, so this pins that the lock
    and the revision sequence still hold over the record that `_load` published.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    reopened = cr.ReviewStore(directory)
    reopened.begin_scan("owner-a", "scan-2")
    accepted, failures = [], []

    def hammer():
        for _ in range(8):
            while True:
                _, revision = reopened.get("clip-0")
                try:
                    accepted.append(reopened.decide("owner-a", "clip-0", cr.SELECTED,
                                                    "scan-2", revision)[1])
                    break
                except cr.StaleWrite:
                    continue
                except Exception as error:                       # noqa: BLE001
                    failures.append(error)
                    return

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert not failures, failures[:2]
    start = expected["clip-0"][1]
    assert sorted(accepted) == list(range(start + 1, start + 33)), (
        "revisions stay unique and contiguous from the recovered revision")
    assert cr.ReviewStore(directory).get("clip-0") == (cr.SELECTED, start + 32)


def test_n7_a_compaction_failure_still_leaves_a_completely_recovered_store(tmp_path,
                                                                           monkeypatch):
    """Case 14, from the N7 angle. The two compaction failure points are already covered for
    loss; what matters here is that neither leaves a store that must be treated as incomplete.

    A failed compaction is a failed *optimisation*. The record in memory is still the one that was
    recovered, so the store stays writable -- otherwise containing the failure (N5) would have
    traded one outage for another.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    real_replace = os.replace

    def refuse(src, dst, *args, **kwargs):
        if str(dst) == store.snapshot_path:
            raise OSError(13, "snapshot refused", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(cr.os, "replace", refuse)
    with pytest.raises(OSError):
        store.compact()
    monkeypatch.undo()

    assert store._recovered is True                              # noqa: SLF001 - white box
    store.begin_scan("owner-a", "scan-2")
    assert store.decide("owner-a", "clip-after", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    store.compact()
    final = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert final.get("clip-after") == (cr.SELECTED, 1)


# =============================================================================================
# 13. R5-P13 / N7-R1 + N7-R2: a readable file is not necessarily a RECOVERABLE file
# =============================================================================================
#
# R5-P11 made recovery all-or-nothing for files it could not *read*. An independent R5-P12 review
# measured what was left: for files it could read but not account for, the store still declared
# itself recovered, accepted a decision and compacted -- so the loss route survived intact behind
# a different door. Both halves were reproduced executably on the frozen candidate.
#
# N7-R1, the decisive case. Three decisions committed and compacted; the 381-byte snapshot was
# valid JSON and still held all three complete records, plus one entry missing its `decision`. The
# snapshot was discarded whole, `_recovered` was set, a new decision was accepted, and `compact()`
# wrote a 144-byte replacement holding none of the three. Restart read all three as PENDING.
# SHA-256 efdac66e... -> 929bbca7.... The automatic 2,000-record `maybe_compact()` did the same
# with no operator action at all.
#
# N7-R2. One committed decision's journal line was replaced by malformed content while later valid
# decisions remained; the line was skipped, recovery was declared complete, and compaction
# truncated the 313 bytes that still held it. A JSON-decodable `decide` event with no `identity`
# did the same while leaving `corrupt_records_discarded` at 0. A non-numeric revision escaped as a
# bare `ValueError` and non-UTF-8 bytes as a `UnicodeDecodeError` -- outside the typed refusal the
# GUI and the 503 boundary are built on.
#
# The invariant these tests pin: a store becomes writable and compactable only when it can
# JUSTIFY that the whole persisted record has been accounted for. Reaching end of file is not that
# justification, and neither is a corruption counter.


def _journal_lines(store):
    with open(store.journal_path, "rb") as handle:
        return handle.read().splitlines(keepends=True)


def _rewrite_journal(store, lines):
    with open(store.journal_path, "wb") as handle:
        handle.write(b"".join(lines))


def _destructive_sequence(directory, store):
    """The real failure sequence from R5-P12, run in full. Returns (store_or_None, bytes_after).

    Deliberately NOT a bare `pytest.raises` around the construction: if the constructor ever stops
    refusing, this runs the genuine destructive steps -- a new decision, a real `compact()` and a
    real threshold `maybe_compact()` -- so the caller's assertions fail on measured loss rather
    than passing because some unrelated exception happened to arrive first.
    """
    try:
        incomplete = cr.ReviewStore(directory)
    except cr.PersistenceError:
        incomplete = None
    if incomplete is not None:
        for attempt in (lambda: incomplete.begin_scan("owner-a", "scan-9"),
                        lambda: incomplete.decide("owner-a", "clip-new", cr.SELECTED, "scan-9"),
                        incomplete.compact):
            try:
                attempt()
            except (cr.PersistenceError, cr.StaleWrite, OSError):
                pass
        incomplete.journal_records = cr.COMPACT_AFTER_RECORDS
        try:
            incomplete.maybe_compact()
        except (cr.PersistenceError, OSError):
            pass
    return incomplete, _state_bytes(store)


# ---- N7-R1: the authoritative snapshot ------------------------------------------------------

def test_n7r1_three_valid_entries_plus_one_invalid_entry_destroys_nothing(tmp_path):
    """The reproduction that made this merge-blocking, as a test.

    Every byte of the three committed records is still present and readable in the snapshot. The
    old code threw them away because a *fourth* entry was malformed, then let compaction make that
    permanent. The corrected store refuses, and the originals are recoverable the moment the one
    damaged entry is removed -- which is the proof that the refusal preserved something real.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    before = _state_bytes(store)

    snapshot = json.loads(before["snapshot"].decode("utf-8"))
    assert set(expected) <= set(snapshot["records"]), "all three are in the snapshot to begin with"
    snapshot["records"]["invalid-tail"] = {"revision": 1}
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None, "a snapshot that cannot be accounted for must not be constructible"
    assert after == damaged, "neither copy may be rewritten or truncated"

    snapshot["records"].pop("invalid-tail")
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    _assert_fully_recovered(directory, expected)


def test_n7r1_a_syntactically_invalid_snapshot_with_an_empty_journal_refuses(tmp_path):
    """The sharpest shape: after a normal compaction the journal is EMPTY, so the snapshot is the
    only copy of every decision. Discarding it is discarding the record."""
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    assert os.path.getsize(store.journal_path) == 0, "compaction emptied the journal"
    with open(store.snapshot_path, "wb") as handle:
        handle.write(b"{ broken snapshot still holding clip-0 clip-1 clip-2")
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None
    assert after == damaged, "the retained damaged bytes are the only forensic copy left"


def test_n7r1_an_invalid_snapshot_with_a_partial_delta_journal_refuses(tmp_path):
    """The journal is readable, parses, and holds real decisions -- just not these ones.

    This is why "the journal replayed cleanly" cannot be the completeness proof. After a
    compaction the journal holds only what was written since, so a readable delta looks exactly
    like a readable whole history.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    later = cr.ReviewStore(directory)
    later.begin_scan("owner-a", "scan-2")
    later.decide("owner-a", "clip-late", cr.SELECTED, "scan-2")
    assert os.path.getsize(store.journal_path) > 0, "there is a real delta to replay"
    with open(store.snapshot_path, "wb") as handle:
        handle.write(b"{ broken")
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None, "a readable delta is not proof of a complete history"
    assert after == damaged


def test_n7r1_an_invalid_snapshot_with_a_complete_journal_also_refuses(tmp_path):
    """The deliberate, documented limit of this correction.

    Here the journal really does hold the whole history -- but **nothing on disk says so**. The
    format carries no genesis marker and no provenance, and this case is byte-for-byte
    indistinguishable from the delta case above except by knowledge the store does not have.
    R5-P13 refuses rather than inventing a completeness check that would be wrong exactly when it
    mattered. The decisions are not lost: both copies are preserved, and removing the damaged
    snapshot recovers all three from the journal alone.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)                     # no compaction: journal is whole
    with open(store.snapshot_path, "wb") as handle:
        handle.write(b"{ broken")
    damaged = _state_bytes(store)

    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    assert _state_bytes(store) == damaged

    os.unlink(store.snapshot_path)
    _assert_fully_recovered(directory, expected)


def test_n7r1_the_real_automatic_threshold_cannot_compact_an_unaccounted_snapshot(tmp_path):
    """The automatic route, with a real journal of `COMPACT_AFTER_RECORDS` real events.

    The measured N7-R1 loss needed no operator action: housekeeping alone was enough. There must
    be no store to reach the threshold with -- and the threshold must still work for a store that
    did recover, which the second half asserts.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    with open(store.journal_path, "a", encoding="utf-8") as handle:
        for _ in range(cr.COMPACT_AFTER_RECORDS):
            handle.write(json.dumps({"k": "scan", "owner": "owner-a", "scan_id": "scan-1",
                                     "at": 0.0}, separators=(",", ":")) + "\n")
    healthy = cr.ReviewStore(directory)
    assert healthy.journal_records >= cr.COMPACT_AFTER_RECORDS, "the threshold is really reached"

    snapshot = json.loads(open(store.snapshot_path, encoding="utf-8").read())
    snapshot["records"]["invalid-tail"] = {"revision": 1}
    with open(store.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    damaged = _state_bytes(store)

    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    assert _state_bytes(store) == damaged, "the automatic route destroys nothing either"

    # The housekeeping itself is not disabled: the already recovered store still compacts.
    assert healthy.maybe_compact() is True
    assert healthy.journal_records == 0


# ---- N7-R2: damaged and unaccountable journal records ---------------------------------------

def test_n7r2_a_malformed_historical_line_followed_by_valid_events_refuses(tmp_path):
    """Damage in the MIDDLE is not the torn tail, and the difference is the whole finding.

    R5-P3's justification was "a damaged line can only ever be a single line, because `_append`
    terminates an unterminated tail before writing after it". That is a statement about damage
    this process caused. Here the damage sits before later committed events, so the record it held
    cannot be established -- and the old code skipped it, declared recovery complete and let
    compaction truncate the only bytes that still held it.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    lines = _journal_lines(store)
    assert b"clip-1" in lines[2], "the line being damaged is a committed decision"
    lines[2] = b"{ invalid committed line\n"
    _rewrite_journal(store, lines)
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None, "a damaged historical record must refuse the store"
    assert after == damaged, "the retained bytes are the only evidence of what was damaged"


@pytest.mark.parametrize("line,why", [
    (b'{"k":"decide","revision":1,"decision":"SELECTED","scan_id":"scan-1"}\n', "no identity"),
    (b'{"k":"decide","identity":7,"decision":"SELECTED","revision":1,"scan_id":"s"}\n',
     "identity is not a string"),
    (b'{"k":"decide","identity":"clip-1","revision":1,"scan_id":"scan-1"}\n', "no decision"),
    (b'{"k":"decide","identity":"clip-1","decision":"MAYBE","revision":1,"scan_id":"s"}\n',
     "unknown decision"),
    (b'{"k":"decide","identity":"clip-1","decision":"SELECTED","scan_id":"scan-1"}\n',
     "no revision"),
    (b'{"k":"decide","identity":"c","decision":"SELECTED","revision":"x","scan_id":"s"}\n',
     "revision is a string"),
    (b'{"k":"decide","identity":"c","decision":"SELECTED","revision":true,"scan_id":"s"}\n',
     "revision is a bool"),
    (b'{"k":"decide","identity":"c","decision":"SELECTED","revision":-1,"scan_id":"s"}\n',
     "negative revision"),
    (b'{"k":"decide","identity":"c","decision":"SELECTED","revision":1}\n', "no scan_id"),
    (b'{"k":"decide","identity":"c","decision":"SELECTED","revision":1,"scan_id":"s","at":"x"}\n',
     "at is not a number"),
    (b'{"k":"scan","scan_id":"scan-1","at":0.0}\n', "a scan with no owner"),
    (b'{"k":"scan","owner":"","scan_id":"scan-1","at":0.0}\n', "a scan with an empty owner"),
    (b'{"k":"scan","owner":"owner-a","at":0.0}\n', "a scan with no scan_id"),
    (b'{"k":"transfer","identity":"clip-1","to":"library"}\n', "an unsupported event kind"),
    (b'{"k":7,"identity":"clip-1"}\n', "a non-string kind"),
    (b'{"identity":"clip-1","decision":"SELECTED","revision":1}\n', "no kind at all"),
    (b'["not","an","object"]\n', "a JSON array instead of an event"),
    (b'"a bare string"\n', "a JSON string instead of an event"),
    (b'{"k":"tail-repaired"}\n', "a repair marker with no timestamp"),
], ids=lambda value: value if isinstance(value, str) else "line")
def test_n7r2_a_semantically_invalid_event_refuses_rather_than_no_opping(tmp_path, line, why):
    """A parsable event this build cannot account for is corruption, wherever it sits.

    Torn writes cannot produce these: truncating one of this writer's records always leaves JSON
    that will not parse, so a *parsable* event was written whole and its content is a real
    statement -- one that may well be standing in for a committed decision. The old `_apply`
    returned early on a non-string identity, coerced a missing decision to PENDING and a missing
    revision to 0, and ignored an unknown kind entirely, every one of which is a silent no-op on
    a record that may have held something.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    lines = _journal_lines(store)
    lines.insert(2, line)
    _rewrite_journal(store, lines)
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None, f"{why} must refuse the store"
    assert after == damaged, f"{why} must leave both copies byte-identical"

    lines.pop(2)
    _rewrite_journal(store, lines)
    _assert_fully_recovered(directory, expected)


def test_n7r2_a_semantic_failure_is_a_typed_path_free_persistence_error(tmp_path):
    """R5-P12 found `int("x")` escaping as a bare `ValueError`, outside the 503 contract."""
    directory = str(tmp_path / "state")
    store, _ = _committed(directory)
    lines = _journal_lines(store)
    lines.insert(2, b'{"k":"decide","identity":"lost","revision":"x","decision":"SELECTED"}\n')
    _rewrite_journal(store, lines)

    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    message = str(failure.value)
    assert "revision" in message, "the condition stays diagnosable"
    assert not _mentions(message, store.journal_path)
    assert not _mentions(message, store.dir)
    assert "review-journal" not in message and ".jsonl" not in message


def test_n7r2_a_journal_that_is_not_utf8_is_a_typed_refusal(tmp_path):
    """R5-P12 found this escaping as a bare `UnicodeDecodeError` from the replay loop.

    It is raised by the text layer during *iteration*, not inside the `json.loads` guard, so
    catching `ValueError` around the parse never saw it -- even though `UnicodeDecodeError` is a
    `ValueError` subclass.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    lines = _journal_lines(store)
    lines.insert(2, b"\xff\xfe not utf-8 at all\n")
    _rewrite_journal(store, lines)
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None
    assert after == damaged
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    message = str(failure.value)
    assert "UTF-8" in message and "byte offset" in message
    assert not _mentions(message, store.journal_path)


def test_n7r2_an_invalid_snapshot_that_is_not_utf8_is_a_typed_refusal(tmp_path):
    """The same decoding failure on the other file. Pre-fix it was the same bare exception."""
    directory = str(tmp_path / "state")
    store, expected = _committed(directory, compact=True)
    with open(store.snapshot_path, "wb") as handle:
        handle.write(b'{"records": {"\xff": {}}}')
    damaged = _state_bytes(store)

    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    assert "UTF-8" in str(failure.value)
    assert not _mentions(str(failure.value), store.snapshot_path)
    assert _state_bytes(store) == damaged


# ---- The torn-tail guarantee, preserved and now provable ------------------------------------

def test_n7r2_the_accepted_f4_durable_write_sequence_still_holds_end_to_end(tmp_path):
    """**The mandatory compatibility gate.** The full F4/N2 sequence, across two restarts.

    This is the sequence a naive "only the final line may be damaged" rule would have broken, and
    it is why the repair writes durable evidence rather than a bare newline. After
    `_terminate_damaged_tail` closes the fragment off, the fragment is no longer the last line --
    so on the *second* restart a positional rule would refuse a store whose later decision had
    already been reported successful, which is exactly the false-failure mirror of the N7 defect.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')

    first = _assert_fully_recovered(directory, expected)
    assert first.corrupt_records_discarded == 1, "exactly the one unfinished record"
    assert first.get("torn") == (cr.PENDING, 0), "an unfinished record never reported success"
    first.begin_scan("owner-a", "scan-2")
    assert first.decide("owner-a", "later", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    assert first.journal_tails_repaired == 1

    lines = _journal_lines(first)
    damaged_at = next(index for index, line in enumerate(lines) if b'"torn"' in line)
    assert damaged_at != len(lines) - 1, (
        "the precondition of this test: the damaged line is no longer the final line")
    assert lines[damaged_at].rstrip().endswith(b'}'), "the damaged line was terminated"
    assert b'{"k":"tail-repaired"' in lines[damaged_at], (
        "and it carries the durable evidence of its own repair")
    assert cr.ReviewStore._damage_is_accounted_for(                 # noqa: SLF001 - white box
        lines[damaged_at].decode("utf-8").strip())
    with pytest.raises(ValueError):
        json.loads(lines[damaged_at])                               # still unreadable as a record
    assert sum(1 for line in lines if b'{"k":"tail-repaired"' in line) == 1, "one repair, once"

    second = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert second.get("later") == (cr.SELECTED, 1), "a reported success must survive a restart"
    assert second.corrupt_records_discarded == 1
    second.compact()

    third = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert third.get("later") == (cr.SELECTED, 1), "and must survive compaction too"
    assert third.get("torn") == (cr.PENDING, 0)
    assert third.corrupt_records_discarded == 0, "compaction removed the accounted-for damage"
    third.begin_scan("owner-a", "scan-3")
    assert third.decide("owner-a", "newest", cr.SELECTED, "scan-3") == (cr.SELECTED, 1)
    _assert_fully_recovered(directory, expected, scan="scan-3")


def test_n7r2_an_interrupted_repair_still_recovers_every_reported_success(tmp_path):
    """The repair itself interrupted, at every measurable prefix of its single write.

    `_write_all` guarantees all-the-bytes-or-a-raise per call, not atomicity across an
    interruption, so any prefix of the marker can reach disk. Each one is checked for the property
    that matters: every decision that was ever reported successful is still recoverable
    afterwards, and the store is not wedged.

    This test is the reason the marker is appended ONTO the fragment. The first version of the fix
    wrote `"\\n" + marker`, and this test failed at `cut=1`: the lone newline terminated the
    fragment without documenting it, the next append therefore found a terminated tail and wrote
    no marker of its own, and the restart after that refused a store whose `after` decision had
    already been reported successful -- the N7 defect's false-failure mirror image.
    """
    full = b'{"k":"tail-repaired","at":0.0}\n'
    for cut in range(len(full) + 1):
        directory = str(tmp_path / f"state-{cut}")
        store, expected = _committed(directory)
        with open(store.journal_path, "ab") as handle:
            handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')
            handle.write(full[:cut])                 # the interrupted repair write

        recovered = _assert_fully_recovered(directory, expected)
        assert recovered.get("torn") == (cr.PENDING, 0)
        recovered.begin_scan("owner-a", "scan-2")
        assert recovered.decide("owner-a", "after", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)

        again = _assert_fully_recovered(directory, expected, scan="scan-2")
        assert again.get("after") == (cr.SELECTED, 1), (
            f"a reported success must survive a repair interrupted after {cut} byte(s)")
        again.compact()
        final = _assert_fully_recovered(directory, expected, scan="scan-2")
        assert final.get("after") == (cr.SELECTED, 1)


@pytest.mark.parametrize("line,why", [
    (b'{"k":"tail-repaired","at":0.0}\n{ damaged on its own line\n',
     "a marker on a line of its own excuses no other line"),
    (b'{ damaged }{"k":"tail-repaired"\n', "a truncated marker is not a marker"),
    (b'{ damaged }{"k":"tail-repaired","at":"later"}\n', "nor is one with a bad timestamp"),
    (b'{ damaged }{"k":"tail-repaired","at":0.0} trailing\n',
     "nor one that is not the end of the line"),
    (b'{"k":"tail-repaired","at":0.0} { damaged }\n',
     "nor one that precedes the damage instead of closing it"),
])
def test_n7r2_only_a_genuine_attached_marker_accounts_for_damage(tmp_path, line, why):
    """The marker is evidence, not an amnesty.

    Without these bounds it would be a loophole: anything resembling a repair anywhere near a
    malformed line would license skipping it, which is the N7-R2 defect with an extra step. A
    marker only ever reaches disk as the terminating suffix of the fragment it repaired.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    lines = _journal_lines(store)
    lines.insert(2, line)
    _rewrite_journal(store, lines)
    damaged = _state_bytes(store)

    incomplete, after = _destructive_sequence(directory, store)
    assert incomplete is None, why
    assert after == damaged

    lines.pop(2)
    _rewrite_journal(store, lines)
    _assert_fully_recovered(directory, expected)


def test_n7r2_a_terminated_unreadable_line_is_corruption_even_at_the_end_of_the_file(tmp_path):
    """Tolerating the *last* unreadable line is not enough of a bound, and this is why.

    An earlier draft of this fix tolerated the whole trailing run of unreadable lines, reasoning
    that nothing committed can follow a record that was never finished. True of the unfinished
    record; false of a **terminated** one sitting in front of it. `_append` writes a record and its
    newline in one `_write_all` call, so a line this process terminated is a line it wrote in full
    -- and a full record parses. Terminated damage is therefore external, may well have stood for a
    committed decision, and under the run rule it was laundered: tolerated on the first load,
    marked discarded, and then truncated away by the first compaction after a repair.

    Both arrangements are checked, because the run rule tolerated both.
    """
    for name, tail in (("damage-then-tear", b'{ terminated damage\n{"k":"decide","identity":"t'),
                       ("damage-alone", b"{ terminated damage\n")):
        directory = str(tmp_path / name)
        store, expected = _committed(directory)
        with open(store.journal_path, "ab") as handle:
            handle.write(tail)
        damaged = _state_bytes(store)

        incomplete, after = _destructive_sequence(directory, store)
        assert incomplete is None, f"{name}: terminated damage must refuse the store"
        assert after == damaged, f"{name}: and must not be truncated away"
        with pytest.raises(cr.PersistenceError) as failure:
            cr.ReviewStore(directory)
        assert "written in full" in str(failure.value)
        assert not _mentions(str(failure.value), store.journal_path)


# ---- The writer and the validator must agree -------------------------------------------------

def test_n7_every_event_this_module_writes_satisfies_its_own_replay_validator(tmp_path):
    """The consistency gate for a strict validator: the strictness must not reject the writer.

    Each of the three event shapes is produced by the real write path -- a scan, a decision and a
    tail repair -- and every one is fed through `_apply` directly. A validator that rejected one of
    these would turn the first restart after a repair into an outage, which is why this is pinned
    rather than assumed.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn"')
    store.decide("owner-a", "clip-0", cr.SELECTED, "scan-1")
    assert store.journal_tails_repaired == 1, "all three shapes are now on disk"

    kinds = set()
    for line in _journal_lines(store):
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except ValueError:
            # The repaired fragment. Its marker suffix is the third shape, so pull it out and
            # replay that, which is what `_replay_journal` relies on being valid.
            assert cr.ReviewStore._damage_is_accounted_for(text)     # noqa: SLF001 - white box
            event = json.loads(text[text.rindex('{"k":"tail-repaired"'):])
        store._apply(event, {}, {})                   # noqa: SLF001 - white box: must not raise
        kinds.add(event["k"])
    assert kinds == {"scan", "decide", "tail-repaired"}, kinds


def test_n7_recovery_is_not_declared_complete_from_a_partial_replay(tmp_path, monkeypatch):
    """`_recovered` must mean "all of it", not "the read returned".

    The guard is checked from the other side too: a store that did not finish replaying must not
    exist, so there is nothing holding a prefix to compact from.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    before = _state_bytes(store)

    _fail_part_way_through(monkeypatch, store.journal_path, after_lines=2)
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert _state_bytes(store) == before
    _assert_fully_recovered(directory, expected)


def test_n7r2_an_unfinished_record_may_not_be_followed_by_anything(tmp_path, monkeypatch):
    """The guard that a real file cannot reach, exercised with a handle that can.

    An unterminated line ends iteration over a real file, so "damage this process caused, with
    committed events behind it" is not a state the filesystem can present. It is still asserted,
    because the alternative to the raise is precisely the N7-R2 defect -- and because a guard no
    test exercises is a guard that can be deleted by accident. A minimal handle supplies the one
    line sequence the filesystem will not.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    before = _state_bytes(store)
    target = os.path.normcase(os.path.abspath(store.journal_path))
    real = open

    class _Handle:
        """Yields an unterminated record and then keeps going."""

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def __iter__(self):
            yield '{"k":"decide","identity":"torn"'          # no newline: an unfinished record
            yield ('{"k":"decide","identity":"after","decision":"SELECTED","revision":1,'
                   '"scan_id":"scan-1","at":0.0}\n')

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            return _Handle()
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    monkeypatch.undo()

    assert "continues past an unfinished record" in str(failure.value)
    assert not _mentions(str(failure.value), store.journal_path)
    assert _state_bytes(store) == before
    _assert_fully_recovered(directory, expected)


def test_n7_the_replay_validator_is_never_stricter_than_the_write_path(tmp_path):
    """The symmetry gate. A validator stricter than the writer is a false-failure generator.

    `decide` and `begin_scan` accept these; the replay must therefore accept them too, or a
    decision this module reported successful would be refused on the next restart -- the exact
    false-failure class the torn-tail repair exists to avoid. An empty identity is the real case:
    production identities are 64-character SHA-256 digests, but `decide` does not forbid an empty
    one, so the replay must not either. The fix for a stricter reader is never a new write-time
    refusal invented to justify it.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "")                      # an empty scan id is accepted
    assert store.decide("owner-a", "", cr.SELECTED, "") == (cr.SELECTED, 1)
    assert store.decide("owner-a", "x" * 64, cr.PENDING, "") == (cr.PENDING, 1)

    recovered = cr.ReviewStore(directory)
    assert recovered.get("") == (cr.SELECTED, 1), "an empty identity round-trips"
    assert recovered.get("x" * 64) == (cr.PENDING, 1)
    assert recovered.current_scan("owner-a") == ""
    assert recovered.corrupt_records_discarded == 0, "nothing here is corruption"
    recovered.compact()
    again = cr.ReviewStore(directory)
    assert again.get("") == (cr.SELECTED, 1), "and survives the snapshot too"
    assert again.get("x" * 64) == (cr.PENDING, 1)


# =============================================================================================
# N7-R3: a newline-less tail is not necessarily an unfinished record
# =============================================================================================
#
# R5-P13 stamped a `tail-repaired` marker onto EVERY newline-less final line. An independent
# review (R5-P14) measured the consequence: a decision that had been reported successful, whose
# line lost only its terminating newline, was recovered correctly on the next start, turned into
# "damage this process accounted for" by the marker, and then erased by the following compaction.
# The parent commit -- which wrote a bare newline -- preserved it, so the marker mechanism was a
# strict regression for this one shape.
#
# The distinction these tests pin: truncating one of this writer's records always leaves JSON
# that will not parse, so a newline-less line that parses WHOLE was written whole and is
# completed, not relabelled. Everything about the genuine torn fragment is unchanged, which the
# frozen interrupted-repair tests above continue to assert.


def _drop_final_newline(store):
    """External loss of exactly one byte: the journal's terminating newline. Nothing else."""
    with open(store.journal_path, "rb") as handle:
        data = handle.read()
    assert data.endswith(b"\n"), "precondition: the journal was properly terminated"
    with open(store.journal_path, "wb") as handle:
        handle.write(data[:-1])
    return data[:-1]


def _parsed_journal_events(store):
    """Every journal line parsed INDEPENDENTLY. Raises if any line is not one whole event."""
    events = []
    for line in _journal_lines(store):
        text = line.decode("utf-8").strip()
        if text:
            events.append(json.loads(text))          # no partial parse, no trailing bytes
    return events


def test_n7r3_a_valid_record_missing_only_its_newline_survives_repair_and_compaction(tmp_path):
    """The mandatory N7-R3 durable-write sequence, end to end.

    This is the regression the whole fix exists for. It fails on the parent commit of this
    correction -- the decision comes back ``PENDING, 0`` at step 7 -- and the failure is permanent
    once ``compact()`` has run.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    last = "clip-2"
    assert expected[last] == (cr.REJECTED, 1), "the last decision was REPORTED SUCCESSFUL"

    # 2. its terminating newline is lost, and nothing else
    truncated = _drop_final_newline(store)
    assert truncated.endswith(b"}"), "the record itself is complete"
    final_line = truncated.rsplit(b"\n", 1)[-1]
    assert json.loads(final_line.decode("utf-8"))["identity"] == last, (
        "the final line is still one complete, parseable, valid event")

    # 3. the remaining valid event is recovered after a restart
    first = _assert_fully_recovered(directory, expected)
    assert first.corrupt_records_discarded == 0, "a whole record is not corruption"

    # 4. another operation completes the missing newline WITHOUT marking it as damage
    first.begin_scan("owner-a", "scan-2")
    assert first.journal_tails_completed == 1, "the newline was supplied"
    assert first.journal_tails_repaired == 0, "and no repair marker was written"
    with open(first.journal_path, encoding="utf-8") as handle:
        assert cr._JOURNAL_TAIL_REPAIRED not in handle.read(), (   # noqa: SLF001 - white box
            "no marker reached the journal at all")

    # the record keeps its exact original bytes, now simply terminated
    with open(first.journal_path, "rb") as handle:
        assert handle.read().startswith(truncated + b"\n")

    # 5. a subsequent valid decision is appended and reported successful
    assert first.decide("owner-a", "clip-new", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    expected["clip-new"] = (cr.SELECTED, 1)

    # the journal is a sequence of independently parseable events, not a concatenation
    events = _parsed_journal_events(first)
    assert [e["identity"] for e in events if e["k"] == "decide"] == [
        "clip-0", "clip-1", "clip-2", "clip-new"]
    assert all(e["k"] in cr._JOURNAL_KINDS for e in events)        # noqa: SLF001 - white box

    # 6-7. restart: both decisions present, at their correct revisions
    second = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert second.corrupt_records_discarded == 0

    # 8-10. compact, restart, everything remains
    second.compact()
    third = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert third.corrupt_records_discarded == 0

    # 11-12. a second AUTOMATIC compaction cycle, through the real threshold
    for _ in range(cr.COMPACT_AFTER_RECORDS):
        third.begin_scan("owner-a", "scan-2")
        if third.maybe_compact():
            break
    else:                                                          # pragma: no cover
        pytest.fail("the real automatic threshold never fired")
    fourth = _assert_fully_recovered(directory, expected, scan="scan-2")
    assert fourth.get(last) == (cr.REJECTED, 1), (
        "no previously accepted decision may become PENDING or disappear")


def test_n7r3_a_short_write_that_lands_the_record_but_not_its_newline_is_not_destroyed(tmp_path):
    """The writer's OWN near-miss, with reported-FAILURE semantics stated explicitly.

    A positive short write lands the complete JSON object and then fails before the newline, so
    ``_write_all`` raises and the caller is correctly told the decision was **not** recorded. The
    bytes are nevertheless on disk and parseable, and the next start reports the decision as
    present -- so completing the line is the self-consistent answer, and relabelling it as damage
    (which is what R5-P13 did) makes the store contradict itself across two restarts.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    real_write = os.write
    calls = {"n": 0}

    def short_then_fail(fd, payload):
        calls["n"] += 1
        if calls["n"] == 1 and payload.endswith(b"\n") and len(payload) > 1:
            real_write(fd, payload[:-1])             # everything except the newline
            raise OSError(28, "synthetic no space left")
        return real_write(fd, payload)

    os.write = short_then_fail
    try:
        with pytest.raises(cr.PersistenceError):
            store.decide("owner-a", "clip-short", cr.SELECTED, "scan-1")
    finally:
        os.write = real_write
    assert store.get("clip-short") == (cr.PENDING, 0), "the caller was told it failed"

    # it is nevertheless a complete, parseable record on disk
    with open(store.journal_path, "rb") as handle:
        tail = handle.read().rsplit(b"\n", 1)[-1]
    assert json.loads(tail.decode("utf-8"))["identity"] == "clip-short"

    reloaded = _assert_fully_recovered(directory, expected)
    assert reloaded.get("clip-short") == (cr.SELECTED, 1), (
        "a whole record on disk replays; this is the state the repair must not destroy")
    reloaded.begin_scan("owner-a", "scan-1")
    assert reloaded.journal_tails_repaired == 0, "no marker onto a complete record"
    assert reloaded.journal_tails_completed == 1

    final = _assert_fully_recovered(directory, expected)
    assert final.get("clip-short") == (cr.SELECTED, 1)
    final.compact()
    assert _assert_fully_recovered(directory, expected).get("clip-short") == (cr.SELECTED, 1)


def test_n7r3_a_genuinely_torn_fragment_still_gets_the_marker_first_repair(tmp_path):
    """The other half of the distinction: nothing about the F4 torn tail changed.

    Guards the fix against over-reach -- a fragment that does not parse must still be closed off
    with the marker, before its newline, and must still be discarded on the next start.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')

    first = _assert_fully_recovered(directory, expected)
    assert first.get("torn") == (cr.PENDING, 0), "an unfinished record never reported success"
    first.begin_scan("owner-a", "scan-2")
    assert first.journal_tails_repaired == 1, "genuine damage is still marker-repaired"
    assert first.journal_tails_completed == 0, "and is NOT treated as a complete record"

    damaged = next(line for line in _journal_lines(first) if b'"torn"' in line)
    assert damaged.rstrip().endswith(b"}"), "the marker terminates the fragment"
    assert cr._TAIL_REPAIR_PREFIX.encode() in damaged              # noqa: SLF001 - white box
    with pytest.raises(ValueError):
        json.loads(damaged.decode("utf-8").strip())                # still one unparsable line
    assert cr.ReviewStore._damage_is_accounted_for(                # noqa: SLF001 - white box
        damaged.decode("utf-8").strip()) is True

    assert first.decide("owner-a", "after", cr.SELECTED, "scan-2") == (cr.SELECTED, 1)
    expected["after"] = (cr.SELECTED, 1)
    second = _assert_fully_recovered(directory, expected, scan="scan-2")
    second.compact()
    _assert_fully_recovered(directory, expected, scan="scan-2")


def test_n7r3_a_complete_but_unaccountable_final_record_fails_closed(tmp_path):
    """Case C. Parses whole, describes no record this build understands: write nothing.

    Reachable because the store is already constructed and in memory when the file changes
    underneath it. Completing it would publish an unaccountable record; marking it repaired would
    destroy the evidence of one. So neither happens.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.journal_path, "ab") as handle:
        handle.write(json.dumps({"k": "decide", "identity": "x", "decision": "SELECTED",
                                 "revision": "not-a-number", "scan_id": "scan-1"}).encode())
    before = _state_bytes(store)

    with pytest.raises(cr.PersistenceError) as failure:
        store.begin_scan("owner-a", "scan-2")
    assert "nothing has been changed" in str(failure.value)
    assert not _mentions(str(failure.value), store.journal_path), "no path in a client message"
    assert _state_bytes(store) == before, "not one byte was written"
    assert store.journal_tails_repaired == 0 and store.journal_tails_completed == 0

    # removing the one bad line recovers everything, at the original revisions
    _rewrite_journal(store, _journal_lines(store)[:-1])
    _assert_fully_recovered(directory, expected)


def test_n7r3_a_complete_record_behind_a_partial_marker_is_refused_not_discarded(tmp_path):
    """The adversarial combined shape: a complete valid event plus a PARTIAL repair marker.

    Only an EARLIER build could have produced this -- the corrected repair writes a lone newline
    after a record that parses, and a one-byte write has no partial state. It is covered anyway
    because the two unsafe answers are both reachable from here and both destroy data:

    * stamping a marker over it is N7-R3 with an extra step;
    * discarding it as marker-accounted damage is N7-R3 reached through the READ path -- and a
      short write really can land a whole marker and still miss its newline.

    Choosing between "a committed decision" and "a record mid-write" needs provenance the format
    does not carry, so both paths refuse and every byte is kept. The decision stays recoverable:
    this proves it by supplying the newline the earlier build should have written.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    truncated = _drop_final_newline(store)
    partial = json.dumps({"k": cr._JOURNAL_TAIL_REPAIRED,          # noqa: SLF001 - white box
                          "at": 0.0}, separators=(",", ":"))[:12]
    with open(store.journal_path, "ab") as handle:
        handle.write(partial.encode("utf-8"))
    before = _state_bytes(store)

    # the READ path refuses rather than silently dropping the record in front of the marker
    with pytest.raises(cr.PersistenceError) as failure:
        cr.ReviewStore(directory)
    assert "unidentifiable partial write" in str(failure.value)
    assert not _mentions(str(failure.value), store.journal_path)
    assert _state_bytes(store) == before, "a refused load writes nothing"

    # and so does the APPEND path, reached from a store that was already constructed
    with open(store.journal_path, "wb") as handle:
        handle.write(truncated)
    live = cr.ReviewStore(directory)
    with open(live.journal_path, "ab") as handle:
        handle.write(partial.encode("utf-8"))
    guarded = _state_bytes(live)
    with pytest.raises(cr.PersistenceError) as second:
        live.begin_scan("owner-a", "scan-2")
    assert "unidentifiable partial write" in str(second.value)
    assert _state_bytes(live) == guarded
    assert live.journal_tails_repaired == 0 and live.journal_tails_completed == 0

    # Nothing was destroyed. Supplying the newline is NOT the repair -- the partial marker is
    # still sitting behind the record, so the refusal correctly stands:
    with open(live.journal_path, "ab") as handle:
        handle.write(b"\n")
    with pytest.raises(cr.PersistenceError):
        cr.ReviewStore(directory)

    # Removing the partial marker -- the bytes the earlier build should never have written -- is
    # what recovers every decision, at its original revision.
    with open(live.journal_path, "wb") as handle:
        handle.write(truncated + b"\n")
    recovered = cr.ReviewStore(directory)
    for identity, value in expected.items():
        assert recovered.get(identity) == value
    assert recovered.corrupt_records_discarded == 0


@pytest.mark.parametrize("shape,expected_kind", [
    ('{"k":"decide","identity":"a","decision":"SELECTED","revision":1,"scan_id":"s","at":1.0}',
     "complete_event"),
    ('{"k":"scan","owner":"owner-a","scan_id":"s","at":1.0}', "complete_event"),
    ('{"k":"tail-repaired","at":1.0}', "complete_event"),
    ('{"k":"decide","identity":"a","decision":"SELE', "torn_fragment"),
    ('{"k":"decide","identity":"torn"', "torn_fragment"),
    ('', "torn_fragment"),
    ('   ', "torn_fragment"),
    ('{"k":"decide","identity":"a","decision":"SELECTED","revision":"x","scan_id":"s"}',
     "invalid_event"),
    ('{"k":"transfer","identity":"a"}', "invalid_event"),
    ('{"k":"decide","identity":"a","decision":"SELECTED","revision":1,"scan_id":"s","at":1.0}'
     '{"k":"tail-rep', "trailing_bytes"),
    ('{"k":"tail-repaired","at":1.0} { damaged }', "trailing_bytes"),
])
def test_n7r3_the_tail_classifier_tells_the_four_shapes_apart(tmp_path, shape, expected_kind):
    """The classification table, stated as a test so it cannot drift.

    ``_apply`` is handed throwaway dicts, so classifying a tail can never publish it; the
    assertion on the live record below is what pins that.
    """
    store = cr.ReviewStore(str(tmp_path / "state"))
    before = dict(store._records)                                  # noqa: SLF001 - white box
    assert store._classify_unterminated_tail(                      # noqa: SLF001 - white box
        shape.encode("utf-8")) == expected_kind
    assert store._records == before, (                             # noqa: SLF001 - white box
        "validating a tail must not mutate the live record")


def test_n7r3_a_tail_that_is_not_utf8_is_genuine_damage(tmp_path):
    """A torn write can stop mid multi-byte sequence. Not text, so not a whole record."""
    store = cr.ReviewStore(str(tmp_path / "state"))
    assert store._classify_unterminated_tail(                      # noqa: SLF001 - white box
        b'{"k":"decide","identity":"\xff\xfe') == "torn_fragment"


def test_n7r3_every_event_the_writer_emits_classifies_as_a_complete_record(tmp_path):
    """Writer/reader symmetry for the new classifier, from the REAL write path.

    A newline-less tail that this writer produced whole must never be read as damage. The events
    are taken off disk rather than hand-written, so the agreement is measured and not assumed.
    """
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    store.decide("owner-a", "c" * 64, cr.SELECTED, "scan-1")
    store.decide("owner-a", "", cr.REJECTED, "scan-1")             # the writer permits this
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn"')
    store.decide("owner-a", "after", cr.PENDING, "scan-1")         # forces a real repair
    assert store.journal_tails_repaired == 1

    kinds = set()
    for line in _journal_lines(store):
        text = line.decode("utf-8").strip()
        if not text:
            continue
        try:
            kinds.add(json.loads(text)["k"])
        except ValueError:
            # The repaired fragment. The marker never exists as a line of its own -- it is always
            # a suffix of the damage it accounts for -- so the writer's real marker bytes are
            # taken from where the repair actually put them.
            cut = text.rfind(cr._TAIL_REPAIR_PREFIX)               # noqa: SLF001 - white box
            assert cut > 0, "the only unparsable line here is the repaired fragment"
            marker = text[cut:]
            kinds.add(json.loads(marker)["k"])
            assert store._classify_unterminated_tail(              # noqa: SLF001 - white box
                marker.encode("utf-8")) == "complete_event", (
                "the writer's own tail-repair event must classify as a whole record")
            continue
        assert store._classify_unterminated_tail(                  # noqa: SLF001 - white box
            text.encode("utf-8")) == "complete_event", (
            f"the writer emitted {text!r}, which the tail classifier must accept whole")
    assert kinds == set(cr._JOURNAL_KINDS), (                      # noqa: SLF001 - white box
        "all three shapes were exercised")


def test_n7r3_an_oversized_final_line_is_refused_rather_than_guessed_at(tmp_path):
    """The bound on tail inspection. An unclassifiable line is a refusal, never a guess.

    Also the proof that classification is bounded at all: without a ceiling, identifying one
    final record could become a read of a journal holding ``COMPACT_AFTER_RECORDS`` records.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    with open(store.journal_path, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"' + b"x" * (cr.MAX_TAIL_INSPECT_BYTES + 1))
    before = _state_bytes(store)

    live = cr.ReviewStore(directory)                               # the tail is a torn fragment
    with pytest.raises(cr.PersistenceError) as failure:
        live.begin_scan("owner-a", "scan-2")
    assert "too large to identify" in str(failure.value)
    assert str(cr.MAX_TAIL_INSPECT_BYTES) in str(failure.value), "the bound is diagnosable"
    assert not _mentions(str(failure.value), store.journal_path)
    assert _state_bytes(store) == before, "nothing was written to an unclassifiable journal"

    _rewrite_journal(store, _journal_lines(store)[:-1])
    _assert_fully_recovered(directory, expected)


def test_n7r3_the_tail_reader_is_bounded_and_finds_the_last_line_across_blocks(tmp_path):
    """The backwards walk returns exactly the bytes after the last newline, over many blocks."""
    directory = str(tmp_path / "state")
    store = cr.ReviewStore(directory)
    store.begin_scan("owner-a", "scan-1")
    filler = b'{"k":"scan","owner":"owner-a","scan_id":"' + b"y" * 400 + b'","at":1.0}\n'
    with open(store.journal_path, "ab") as handle:
        for _ in range(int(cr._TAIL_CHUNK_BYTES / 100) + 40):      # noqa: SLF001 - white box
            handle.write(filler)
        handle.write(b'{"k":"decide","identity":"tail-marker"')
    assert os.path.getsize(store.journal_path) > cr._TAIL_CHUNK_BYTES * 2    # noqa: SLF001
    assert store._journal_tail_bytes() == (                        # noqa: SLF001 - white box
        b'{"k":"decide","identity":"tail-marker"')

    # a journal that is one single unterminated line has no newline to find at all
    lone = cr.ReviewStore(str(tmp_path / "other"))
    with open(lone.journal_path, "wb") as handle:
        handle.write(b'{"k":"decide","identity":"only"')
    assert lone._journal_tail_bytes() == (                         # noqa: SLF001 - white box
        b'{"k":"decide","identity":"only"')


def test_n7r3_an_uninspectable_tail_writes_nothing(tmp_path, monkeypatch):
    """Case D. If the tail's CONTENT cannot be read, that is not a licence to append.

    The N2/N6 rule, extended from termination to content. Getting this test to exercise the right
    guard took a correction worth recording: failing *every* binary open of the journal makes
    `_journal_tail_is_terminated` raise first, so the assertion passed without
    `_journal_tail_bytes` ever running -- and the mutation that swallows ITS failure survived. So
    the termination probe is allowed to succeed and only the content read is refused, which is
    the one ordering that actually reaches the new guard.
    """
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    _drop_final_newline(store)
    live = cr.ReviewStore(directory)
    before = _state_bytes(live)

    real_open = open
    opens = {"n": 0}

    def refuse_the_content_read(path, *args, **kwargs):
        if os.path.abspath(str(path)) == os.path.abspath(live.journal_path) and (
                "b" in "".join(str(a) for a in args)):
            opens["n"] += 1
            if opens["n"] > 1:            # 1st = the termination probe, which must succeed
                raise OSError(5, "synthetic device error")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refuse_the_content_read)
    with pytest.raises(cr.PersistenceError) as failure:
        live.begin_scan("owner-a", "scan-2")
    monkeypatch.undo()
    assert opens["n"] >= 2, "the termination probe ran and the CONTENT read was the one refused"
    assert not _mentions(str(failure.value), live.journal_path)
    assert _state_bytes(live) == before, "an unreadable tail is never written after"
    assert live.journal_tails_repaired == 0 and live.journal_tails_completed == 0
    _assert_fully_recovered(directory, expected)


def test_n7r3_a_short_read_of_the_tail_writes_nothing(tmp_path, monkeypatch):
    """A read that returns fewer bytes than asked for is not an answer about the tail either."""
    directory = str(tmp_path / "state")
    store, expected = _committed(directory)
    _drop_final_newline(store)
    live = cr.ReviewStore(directory)
    before = _state_bytes(live)

    real_open = open

    class ShortReader:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

        def seek(self, *a):
            return self.inner.seek(*a)

        def tell(self):
            return self.inner.tell()

        def read(self, size=-1):
            data = self.inner.read(size)
            return data[:-1] if size and size > 1 and data else data

    opens = {"n": 0}

    def short(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        if os.path.abspath(str(path)) == os.path.abspath(live.journal_path) and (
                "b" in "".join(str(a) for a in args)):
            opens["n"] += 1
            if opens["n"] > 1:
                return ShortReader(handle)
        return handle

    monkeypatch.setattr("builtins.open", short)
    with pytest.raises(cr.PersistenceError) as failure:
        live.begin_scan("owner-a", "scan-2")
    monkeypatch.undo()
    assert "could not read the end of the review journal" in str(failure.value)
    assert not _mentions(str(failure.value), live.journal_path)
    assert _state_bytes(live) == before
    _assert_fully_recovered(directory, expected)
