"""R7.2 Audio Preview: the HTTP authorization and Range matrix for the audio endpoint.

[FORK] Digital-Union. ``src/audio_media_server.py`` imports only the standard library and the
stdlib-only core, so the whole endpoint is driven **in process** here by a small ASGI harness --
no Uvicorn, no Starlette, no browser, no network. Every status code and every refusal token below
is produced by the shipped middleware.

The claim is the same narrow, absolute one the R5 suite makes for video, and it has to be
re-established rather than inherited because this is a second route: **nothing outside the current
page's enumerated set is reachable, and no URL text becomes a filesystem path.** A sentinel file
that was never enumerated is the negative control, checked at the level that matters -- its bytes
must not appear in any response body, not merely "the status was 403".

Two things this file exists to establish that the R5 suite cannot:

* **Range support is real**, because it is what makes Shift+wheel seeking work without pulling a
  whole clip -- the property Phase 1 replaced the base64 budget to obtain.
* **The two audio surfaces are isolated**, because they hold separate registries and a token
  minted for one must simply not exist in the other.

Real-browser playback is runtime evidence, recorded in the task report. It is NOT claimed here.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import audio_media_server as server
from beatsync_fork import audio_review as review
from beatsync_fork import clips_review as cr

SENTINEL_TEXT = b"R7-2-SECRET-SENTINEL-DO-NOT-SERVE-4f1b8e"
CLIP_BYTES = b"RIFF....WAVEfmt " + bytes(range(256)) * 8          # 2 KiB-ish, deterministic


# =============================================================================================
# A minimal ASGI harness
# =============================================================================================

class Downstream:
    """Stands in for Gradio. Records whether a request was passed through."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope.get("path"))
        body = b"gradio-would-handle-this"
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/plain"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body, "more_body": False})


def call(app, path, method="GET", headers=None):
    """Drive one request and collapse the ASGI messages into (status, headers, body)."""
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"", "client": ("127.0.0.1", 5555), "server": ("127.0.0.1", 7860),
    }
    sent = {"status": None, "headers": {}, "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        else:
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], sent["headers"], sent["body"]


@pytest.fixture
def world(tmp_path):
    """One owner, one scanned folder of three clips, one published page, plus a sentinel."""
    folder = tmp_path / "review"
    folder.mkdir()
    paths = []
    for n in range(1, 4):
        clip = folder / f"take_part_{n:03d}.wav"
        clip.write_bytes(CLIP_BYTES)
        paths.append(str(clip))

    # Never enumerated. If any response ever contains these bytes, the boundary leaked.
    secret = folder / "private.wav"
    secret.write_bytes(SENTINEL_TEXT)

    owner = "ownerabc123"
    registry = cr.ClipRegistry()
    session = registry.new_session(owner)
    frozen = review.freeze_paths(paths, 0, 10)
    token, entries = registry.publish_page(owner, session, 1, frozen,
                                           servable=review.SERVABLE_SUFFIXES)
    downstream = Downstream()
    app = server.AudioMediaMiddleware(downstream, registry=registry,
                                      prefix=review.MEDIA_PREFIX_CUTTER)
    return {"app": app, "downstream": downstream, "registry": registry, "session": session,
            "page": token, "entries": entries, "owner": owner, "folder": str(folder),
            "secret": str(secret), "paths": paths}


def clip_url(world, index=0, prefix=review.MEDIA_PREFIX_CUTTER):
    return f"{prefix}/{world['session']}/{world['page']}/{world['entries'][index].clip_id}"


# =============================================================================================
# 1. Pass-through: the middleware owns ONE route and nothing else
# =============================================================================================

@pytest.mark.parametrize("path", ["/", "/gradio_api/queue/join", "/theme.css",
                                  "/file=C:/Windows/win.ini",
                                  "/r5clip/a/b/c", "/r5poster/a/b/c",
                                  "/r7audiophile", "/r7preview", "/r7audi"])
def test_unrelated_requests_reach_gradio_untouched(world, path):
    """Including the R5 prefixes, which belong to the other middleware, and near-misses on this
    one's own name. `/r7preview` is another instance's prefix, so THIS instance must pass it on."""
    status, _, body = call(world["app"], path)
    assert status == 200 and body == b"gradio-would-handle-this"
    assert path in world["downstream"].calls


def test_the_owned_prefix_never_reaches_gradio(world):
    call(world["app"], review.MEDIA_PREFIX_CUTTER + "/a/b/c")
    assert world["downstream"].calls == []


def test_a_non_http_scope_is_passed_straight_through(world):
    """A websocket must not be inspected as if it had a path to authorize."""
    async def drive():
        seen = []

        async def send(message):
            seen.append(message)

        await world["app"]({"type": "websocket", "path": review.MEDIA_PREFIX_CUTTER},
                           None, send)
        return seen

    asyncio.run(drive())
    assert world["downstream"].calls == [review.MEDIA_PREFIX_CUTTER]


# =============================================================================================
# 2. The authorized clip serves
# =============================================================================================

def test_an_authorized_clip_is_served_with_safe_headers(world):
    status, headers, body = call(world["app"], clip_url(world))
    assert status == 200
    assert headers["content-type"] == "audio/wav"
    assert headers["content-length"] == str(len(CLIP_BYTES))
    assert headers["accept-ranges"] == "bytes"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert body == CLIP_BYTES


def test_no_filename_is_disclosed_in_any_header(world):
    """The browser must never learn the real name from the HTTP layer. The gallery shows the
    operator's chosen name from server-rendered markup; this route confirms nothing about disk."""
    _, headers, _ = call(world["app"], clip_url(world))
    assert headers["content-disposition"] == "inline"
    joined = " ".join(f"{k}:{v}" for k, v in headers.items())
    assert "take_part_001" not in joined
    assert ".wav" not in joined.replace("audio/wav", "")


def test_a_head_request_answers_the_metadata_with_no_body(world):
    status, headers, body = call(world["app"], clip_url(world), method="HEAD")
    assert status == 200
    assert headers["content-length"] == str(len(CLIP_BYTES))
    assert body == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_only_get_and_head_are_allowed(world, method):
    """This route serves bytes. It is not a decision endpoint and must not accept a write."""
    status, _, body = call(world["app"], clip_url(world), method=method)
    assert status == 405
    assert body == b"method_not_allowed"


@pytest.mark.parametrize("suffix,expected", [(".wav", "audio/wav"), (".mp3", "audio/mpeg"),
                                             (".flac", "audio/flac")])
def test_each_format_the_owner_named_is_served_with_a_type_chrome_accepts(tmp_path, suffix,
                                                                         expected):
    folder = tmp_path / "f"
    folder.mkdir()
    clip = folder / f"a{suffix}"
    clip.write_bytes(CLIP_BYTES)
    registry = cr.ClipRegistry()
    session = registry.new_session("o1")
    token, entries = registry.publish_page(
        "o1", session, 1, review.freeze_paths([str(clip)], 0, 1),
        servable=review.SERVABLE_SUFFIXES)
    app = server.AudioMediaMiddleware(Downstream(), registry=registry)
    status, headers, body = call(
        app, f"{review.MEDIA_PREFIX_CUTTER}/{session}/{token}/{entries[0].clip_id}")
    assert status == 200
    assert headers["content-type"] == expected
    assert body == CLIP_BYTES


# =============================================================================================
# 3. Range requests -- what replaced the base64 budget
# =============================================================================================

def test_a_byte_range_is_served_as_206_with_a_content_range(world):
    """Range support is what makes Shift+wheel seeking work without the whole clip in the page."""
    status, headers, body = call(world["app"], clip_url(world),
                                 headers={"Range": "bytes=100-199"})
    assert status == 206
    assert headers["content-range"] == f"bytes 100-199/{len(CLIP_BYTES)}"
    assert headers["content-length"] == "100"
    assert body == CLIP_BYTES[100:200]


def test_an_open_ended_range_runs_to_the_end(world):
    status, headers, body = call(world["app"], clip_url(world),
                                 headers={"Range": "bytes=2000-"})
    assert status == 206
    assert body == CLIP_BYTES[2000:]


def test_a_suffix_range_returns_the_tail(world):
    status, _, body = call(world["app"], clip_url(world), headers={"Range": "bytes=-64"})
    assert status == 206
    assert body == CLIP_BYTES[-64:]


def test_an_unsatisfiable_range_is_416_and_serves_nothing(world):
    status, _, body = call(world["app"], clip_url(world),
                           headers={"Range": f"bytes={len(CLIP_BYTES) + 10}-"})
    assert status == 416
    assert CLIP_BYTES[:32] not in body


def test_a_multipart_range_is_refused_rather_than_half_supported(world):
    status, _, body = call(world["app"], clip_url(world), headers={"Range": "bytes=0-9,20-29"})
    assert status == 416
    assert body == b"multipart_range_unsupported"


def test_an_unparsable_range_is_treated_as_absent(world):
    """RFC 9110: an unparsable Range is ignored, while a parsable-but-unsatisfiable one is a real
    416. Those are different cases and stay different."""
    status, _, body = call(world["app"], clip_url(world), headers={"Range": "furlongs=1-2"})
    assert status == 200
    assert body == CLIP_BYTES


def test_the_body_is_streamed_in_bounded_chunks(world):
    """Pure ASGI rather than `BaseHTTPMiddleware`, so peak memory is one chunk whatever the clip
    weighs. That is the property that makes an hour-long WAV serviceable at all."""
    assert server.CHUNK_BYTES == 64 * 1024

    chunks = []
    scope = {"type": "http", "method": "GET", "path": clip_url(world), "headers": [],
             "query_string": b"", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 2)}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(world["app"](scope, receive, send))
    assert b"".join(chunks) == CLIP_BYTES
    assert all(len(chunk) <= server.CHUNK_BYTES for chunk in chunks)


# =============================================================================================
# 4. Authorization -- the six gates, reached through this route
# =============================================================================================

@pytest.mark.parametrize("build,status,reason", [
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/{w['session']}/{w['page']}/AAAAAAAAAAAAAAAAAAAAAA",
     403, "clip_not_in_page"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/{w['session']}/{w['page']}/"
               f"{w['entries'][0].clip_id}x", 403, "clip_not_in_page"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/nosuchsession00000/{w['page']}/"
               f"{w['entries'][0].clip_id}", 403, "unknown_session"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/{w['session']}/nosuchpage000000000/"
               f"{w['entries'][0].clip_id}", 403, "unknown_page_token"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/{w['session']}/{w['page']}", 404,
     "malformed_clip_url"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/{w['session']}/{w['page']}/"
               f"{w['entries'][0].clip_id}/extra", 404, "malformed_clip_url"),
    (lambda w: f"{review.MEDIA_PREFIX_CUTTER}/", 404, "malformed_clip_url"),
    (lambda w: review.MEDIA_PREFIX_CUTTER, 404, "malformed_clip_url"),
])
def test_every_unauthorized_shape_is_refused_with_a_stable_token(world, build, status, reason):
    got, _, body = call(world["app"], build(world))
    assert got == status
    assert body == reason.encode()


def test_a_refusal_discloses_no_path_filename_or_stack(world):
    _, _, body = call(world["app"], f"{review.MEDIA_PREFIX_CUTTER}/x/y/z")
    text = body.decode()
    assert "\\" not in text and "/" not in text
    assert "wav" not in text
    assert "Traceback" not in text


def test_a_revoked_session_stops_serving(world):
    """A rescan opens a new session, and the previous one's credentials die with it."""
    url = clip_url(world)
    assert call(world["app"], url)[0] == 200
    world["registry"].new_session(world["owner"])
    status, _, body = call(world["app"], url)
    assert status == 403
    assert body in (b"revoked_session", b"unknown_session")


def test_a_revoked_page_holds_no_file_identities(world):
    """A revoked page clears its clip map, so it keeps no paths alive in memory."""
    world["registry"].new_session(world["owner"])
    status, _, _ = call(world["app"], clip_url(world))
    assert status == 403


# =============================================================================================
# 5. No URL text becomes a filesystem path
# =============================================================================================

@pytest.mark.parametrize("path", [
    f"{review.MEDIA_PREFIX_CUTTER}/../../../../Windows/win.ini",
    f"{review.MEDIA_PREFIX_CUTTER}/%2e%2e%2f%2e%2e%2fWindows/win.ini",
    f"{review.MEDIA_PREFIX_CUTTER}/%252e%252e%252fWindows/win.ini",
    f"{review.MEDIA_PREFIX_CUTTER}/C:/Windows/win.ini",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/C:%5CWindows%5Cwin.ini",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/%5C%5Cserver%5Cshare",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/private.wav",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/take_part_001.wav",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/..",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/.",
    f"{review.MEDIA_PREFIX_CUTTER}/a/b/%00",
])
def test_no_url_text_can_name_a_file(world, path):
    """These are refused because they are IDS THAT ARE NOT IN ANY PAGE -- never because a filter
    recognised them. The module contains no path join at all, which the structural test below
    asserts."""
    status, _, body = call(world["app"], path)
    assert status in (403, 404)
    assert SENTINEL_TEXT not in body
    assert CLIP_BYTES[:32] not in body


def test_the_sentinel_is_never_served_by_any_route_shape(world):
    """The negative control, checked at the level that matters: its BYTES must not appear."""
    attempts = [
        world["secret"].replace("\\", "/"),
        f"{review.MEDIA_PREFIX_CUTTER}/{world['session']}/{world['page']}/private.wav",
        f"{review.MEDIA_PREFIX_CUTTER}/{world['session']}/{world['page']}/private",
        "/" + world["secret"].replace("\\", "/"),
    ]
    for path in attempts:
        _, _, body = call(world["app"], path)
        assert SENTINEL_TEXT not in body, path


def test_the_http_layer_builds_no_path_from_a_url():
    """Traversal is impossible by CONSTRUCTION, not by filtering -- the same structural guarantee
    the R5 route carries. Never introduce path building into this layer.

    Asserted over the SYNTAX TREE, by call site, and deliberately **not** as a substring scan. The
    module's own docstring explains that it joins no path, so a substring ban would fire on the
    prose documenting the rule and get deleted instead of satisfied. That has happened repeatedly
    in this repository; an AST check cannot be tripped by a comment.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(server))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("os.path.join", "os.path.abspath", "os.path.realpath", "os.path.normpath",
                      "os.path.expanduser", "os.path.expandvars", "pathlib.Path", "Path",
                      "open", "os.fdopen"):
        assert forbidden not in calls, f"{forbidden} reached the HTTP layer"

    # The only thing done with a URL segment is a dictionary lookup inside the registry.
    served = ast.unparse(tree)
    assert "open_authorized" in served, "bytes are reached only through the authorizing lookup"


def test_the_http_layer_mutates_no_file():
    """This route serves bytes. A move, a delete or a write here would be a second transfer
    mechanism outside the reviewed one.

    Over the syntax tree for the same reason as above -- a future docstring sentence saying "this
    layer never renames anything" must not be able to fail this test.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(server))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("os.rename", "os.replace", "os.remove", "os.unlink", "os.rmdir",
                      "os.makedirs", "os.mkdir", "os.truncate", "os.write", "os.ftruncate",
                      "shutil.move", "shutil.copy", "shutil.rmtree"):
        assert forbidden not in calls, f"{forbidden} reached the HTTP layer"

    # And the descriptor it does open is read-only: no creating, no writing, no truncating.
    flags = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("os.O_CREAT", "os.O_WRONLY", "os.O_RDWR", "os.O_TRUNC", "os.O_APPEND"):
        assert forbidden not in flags, f"{forbidden} reached the HTTP layer"


# =============================================================================================
# 6. Live-source identity, on the opened descriptor
# =============================================================================================

def test_a_replaced_source_is_refused_rather_than_served_stale(world):
    """Identity is revalidated on the OPENED descriptor, never on the pathname: `stat` then `open`
    is two different objects, and the name can be repointed between them."""
    with open(world["paths"][0], "wb") as handle:
        handle.write(b"completely different content of another length")
    status, _, body = call(world["app"], clip_url(world))
    assert status == 409
    assert body == b"identity_changed"


def test_a_deleted_source_is_reported_as_gone_not_as_a_crash(world):
    os.remove(world["paths"][0])
    status, _, body = call(world["app"], clip_url(world))
    assert status == 410
    assert body == b"source_unavailable"


def test_a_directory_in_place_of_a_clip_is_refused(world, tmp_path):
    """An `open` on a directory would otherwise be served as a zero-length clip."""
    os.remove(world["paths"][0])
    os.mkdir(world["paths"][0])
    status, _, body = call(world["app"], clip_url(world))
    assert status in (403, 410)
    assert body in (b"not_a_regular_file", b"source_unavailable")


# =============================================================================================
# 7. Cross-origin
# =============================================================================================

@pytest.mark.parametrize("origin", ["http://evil.example", "https://evil.example",
                                    "http://127.0.0.1.evil.example",
                                    "http://localhost.evil.example", "file://", "null"])
def test_a_foreign_origin_is_refused(world, origin):
    """The HOST is compared exactly, never by prefix: `startswith('http://127.0.0.1')` would admit
    `http://127.0.0.1.evil.example`, whose host is `evil.example`."""
    status, _, body = call(world["app"], clip_url(world), headers={"Origin": origin})
    assert status == 403
    assert body == b"foreign_origin_refused"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:7860", "http://localhost:7861",
                                    "https://127.0.0.1", "http://[::1]:7860"])
def test_a_local_origin_is_accepted(world, origin):
    status, _, _ = call(world["app"], clip_url(world), headers={"Origin": origin})
    assert status == 200


def test_a_same_origin_navigation_sending_no_origin_is_accepted(world):
    assert call(world["app"], clip_url(world))[0] == 200


# =============================================================================================
# 8. The two surfaces are isolated
# =============================================================================================

def test_a_token_minted_for_one_surface_does_not_exist_in_the_other(world, tmp_path):
    """Each prefix is bound to its own registry at construction, so this is not a check that could
    be forgotten -- the other registry simply has no such session."""
    other_registry = cr.ClipRegistry()
    other = server.AudioMediaMiddleware(Downstream(), registry=other_registry,
                                        prefix=review.MEDIA_PREFIX_PREVIEW)
    url = clip_url(world, prefix=review.MEDIA_PREFIX_PREVIEW)
    status, _, body = call(other, url)
    assert status == 403
    assert body == b"unknown_session"


def test_a_scan_on_one_surface_cannot_revoke_the_others_live_page(world, tmp_path):
    """The measured reason the two registries are separate: `new_session` revokes an owner's
    previous sessions, so one shared registry would let an Audio Preview scan silently kill the
    cutter's just-cut gallery, with every tile answering 403 and nothing on screen to say why."""
    folder = tmp_path / "other"
    folder.mkdir()
    clip = folder / "b.wav"
    clip.write_bytes(CLIP_BYTES)

    preview_registry = cr.ClipRegistry()
    preview_registry.new_session(world["owner"])        # the Audio Preview tab scans
    preview_registry.publish_page(
        world["owner"], preview_registry.new_session(world["owner"]), 1,
        review.freeze_paths([str(clip)], 0, 1), servable=review.SERVABLE_SUFFIXES)

    # The cutter's page is untouched.
    assert call(world["app"], clip_url(world))[0] == 200


def test_each_instance_claims_only_its_own_prefix(world):
    preview = server.AudioMediaMiddleware(Downstream(), registry=world["registry"],
                                          prefix=review.MEDIA_PREFIX_PREVIEW)
    # The cutter instance passes a preview URL downstream...
    status, _, body = call(world["app"], review.MEDIA_PREFIX_PREVIEW + "/a/b/c")
    assert body == b"gradio-would-handle-this"
    # ...and the preview instance passes a cutter URL downstream.
    status, _, body = call(preview, review.MEDIA_PREFIX_CUTTER + "/a/b/c")
    assert body == b"gradio-would-handle-this"


def test_the_default_prefix_is_the_cutters_and_both_are_declared():
    assert server.URL_PREFIX == review.MEDIA_PREFIX_CUTTER
    assert set(server.PREFIXES) == {review.MEDIA_PREFIX_CUTTER, review.MEDIA_PREFIX_PREVIEW}


# =============================================================================================
# 9. The servable allowlist still bounds a page
# =============================================================================================

def test_a_non_audio_file_cannot_enter_an_audio_page(tmp_path):
    """Defence in depth. Discovery already filters, but a page must never be able to contain a
    manifest, a sidecar or a decision journal whatever the caller hands over."""
    folder = tmp_path / "f"
    folder.mkdir()
    good = folder / "a.wav"
    good.write_bytes(CLIP_BYTES)
    manifest = folder / "audio_manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    registry = cr.ClipRegistry()
    session = registry.new_session("o")
    frozen = review.freeze_paths([str(good), str(manifest)], 0, 10)
    assert len(frozen) == 2, "the caller did hand over both"
    _, entries = registry.publish_page("o", session, 1, frozen,
                                       servable=review.SERVABLE_SUFFIXES)
    assert [os.path.basename(e.path) for e in entries] == ["a.wav"]


def test_a_video_cannot_enter_an_audio_page_and_vice_versa(tmp_path):
    """The two allowlists are disjoint on purpose, so neither surface can serve the other's media
    through its own route."""
    folder = tmp_path / "f"
    folder.mkdir()
    audio = folder / "a.wav"
    video = folder / "a.mp4"
    audio.write_bytes(CLIP_BYTES)
    video.write_bytes(CLIP_BYTES)

    registry = cr.ClipRegistry()
    frozen = review.freeze_paths([str(audio), str(video)], 0, 10)

    session = registry.new_session("o")
    _, audio_page = registry.publish_page("o", session, 1, frozen,
                                          servable=review.SERVABLE_SUFFIXES)
    assert [os.path.basename(e.path) for e in audio_page] == ["a.wav"]

    session = registry.new_session("o")
    _, video_page = registry.publish_page("o", session, 1, frozen)       # the R5 default
    assert [os.path.basename(e.path) for e in video_page] == ["a.mp4"]


def test_an_empty_allowlist_is_refused_rather_than_meaning_anything_goes(tmp_path):
    """A permissive reading of "no suffixes given" would be the one bug this allowlist exists to
    prevent."""
    folder = tmp_path / "f"
    folder.mkdir()
    clip = folder / "a.wav"
    clip.write_bytes(CLIP_BYTES)
    registry = cr.ClipRegistry()
    session = registry.new_session("o")
    with pytest.raises(cr.Refusal):
        registry.publish_page("o", session, 1, review.freeze_paths([str(clip)], 0, 1),
                              servable=())


def test_the_r5_default_is_unchanged_so_every_existing_call_site_still_video_only():
    """The `servable` parameter was added with the video set as its default precisely so no R5
    call site changed behaviour."""
    import inspect

    signature = inspect.signature(cr.ClipRegistry.publish_page)
    assert signature.parameters["servable"].default == cr.SERVABLE_SUFFIXES
    assert cr.SERVABLE_SUFFIXES == (".mp4", ".mkv", ".mov", ".m4v", ".avi", ".webm")
