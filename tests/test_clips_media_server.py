"""R5 Clips Preview Selector: the HTTP authorization and Range matrix.

[FORK] Digital-Union. ``src/clips_media_server.py`` imports only the standard library and the
stdlib-only core, so the whole endpoint is driven **in process** here by a small ASGI harness --
no Uvicorn, no Starlette, no browser, no network. Every status code and every refusal token below
is produced by the shipped middleware.

The claim this file exists to establish is narrow and absolute: **nothing outside the current
page's enumerated set is reachable, and no URL text becomes a filesystem path.** A sentinel file
that was never enumerated is used as the negative control, and it is checked at the level that
matters -- its bytes must not appear in any response body, not merely "the status was 403".

Real-browser playback, the 25/50/100 timings and the live Gradio integration are runtime
evidence, recorded in the task report. They are NOT claimed here.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import clips_media_server as server
from beatsync_fork import clips_review as cr

SENTINEL_TEXT = b"R5-P1-SECRET-SENTINEL-DO-NOT-SERVE-91c4a7"


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


def call(app, path, method="GET", headers=None, body=b""):
    """Drive one request and collapse the ASGI messages into (status, headers, body)."""
    scope = {
        "type": "http", "method": method, "path": path, "raw_path": path.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "query_string": b"", "client": ("127.0.0.1", 5555), "server": ("127.0.0.1", 7860),
    }
    sent = {"status": None, "headers": {}, "body": b""}
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in message["headers"]}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], sent["headers"], sent["body"]


# =============================================================================================
# Fixtures
# =============================================================================================

def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


@pytest.fixture
def world(tmp_path):
    """A review session, its page, a poster, and a sentinel that is deliberately NOT in it."""
    review = tmp_path / "review"
    for index in range(1, 5):
        _write(str(review / f"clip{index:04d}.mp4"), bytes(range(256)) * 16)   # 4096 bytes
    secret = _write(str(tmp_path / "secret" / "sentinel.txt"), SENTINEL_TEXT)
    _write(str(tmp_path / "secret" / "private.mp4"), SENTINEL_TEXT)

    registry = cr.ClipRegistry()
    store = cr.ReviewStore(str(tmp_path / "state"))
    posters = str(tmp_path / "posters")
    os.makedirs(posters, exist_ok=True)

    found = cr.discover_clips(str(review), True)
    owner = "session-hash-a"
    scan_id = "scan-1"
    store.begin_scan(owner, scan_id)
    session = registry.new_session(owner, scan_id)
    token, entries = registry.publish_page(owner, session, 1,
                                           cr.freeze_page(found.files, 0, 10))
    identity = {e.clip_id: cr.decision_identity(e.path, e.size, e.mtime_ns) for e in entries}
    # A poster for the first clip only, so "poster missing" is also exercised.
    _write(cr.poster_path_for(posters, entries[0]), b"\xff\xd8\xff\xdb" + b"\x00" * 64)

    view = {"session": session, "page": token, "scan_id": scan_id,
            "entries": entries, "identity": identity}

    def decide(owner_in, session_in, page_in, entry, payload):
        if session_in != view["session"] or page_in != view["page"]:
            return {"ok": False, "code": "not_the_active_page"}
        target = payload.get("to")
        if not isinstance(target, str) or target not in cr.DECISIONS:
            return {"ok": False, "code": "illegal_transition"}
        ident = view["identity"].get(entry.clip_id)
        if ident is None:
            return {"ok": False, "code": "clip_not_on_page"}
        if not registry.live_identity_ok(entry):
            return {"ok": False, "code": "identity_changed"}
        try:
            expected = int(payload["rev"]) if payload.get("rev") is not None else None
        except (TypeError, ValueError):
            expected = None
        idents = list(view["identity"].values())
        try:
            decision, revision = store.decide(owner_in, ident, target, view["scan_id"], expected)
        except cr.StaleWrite as stale:
            return {"ok": False, "code": stale.code, "clip": entry.clip_id,
                    "decision": stale.current, "rev": stale.revision,
                    "counts": store.counts(idents)}
        return {"ok": True, "clip": entry.clip_id, "decision": decision, "rev": revision,
                "counts": store.counts(idents)}

    def state(owner_in, session_in, page_in):
        if session_in != view["session"] or page_in != view["page"]:
            return {"ok": False, "code": "not_the_active_page"}
        idents = list(view["identity"].values())
        snapshot = store.snapshot_of(idents)
        return {"ok": True, "counts": store.counts(idents),
                "clips": {e.clip_id: {"decision": snapshot[view["identity"][e.clip_id]][0],
                                      "rev": snapshot[view["identity"][e.clip_id]][1]}
                          for e in entries}}

    downstream = Downstream()
    app = server.ClipMediaMiddleware(
        downstream, registry=registry,
        poster_resolver=lambda entry: cr.poster_path_for(posters, entry),
        decide_handler=decide, state_handler=state)
    return {"app": app, "downstream": downstream, "registry": registry, "store": store,
            "session": session, "page": token, "entries": entries, "secret": secret,
            "review": str(review), "owner": owner, "view": view, "posters": posters}


def clip_url(world, index=0):
    return f"/r5clip/{world['session']}/{world['page']}/{world['entries'][index].clip_id}"


# =============================================================================================
# 1. Pass-through: the middleware owns four routes and nothing else
# =============================================================================================

@pytest.mark.parametrize("path", ["/", "/gradio_api/queue/join", "/theme.css",
                                  "/file=C:/Windows/win.ini", "/r5clipboard", "/r5stateful"])
def test_unrelated_requests_reach_gradio_untouched(world, path):
    status, _, body = call(world["app"], path)
    assert status == 200 and body == b"gradio-would-handle-this"
    assert path in world["downstream"].calls


@pytest.mark.parametrize("prefix", list(server.PREFIXES))
def test_the_four_owned_prefixes_never_reach_gradio(world, prefix):
    call(world["app"], prefix + "/a/b/c")
    assert world["downstream"].calls == []


# =============================================================================================
# 2. The authorized clip serves
# =============================================================================================

def test_an_authorized_clip_is_served_with_safe_headers(world):
    status, headers, body = call(world["app"], clip_url(world))
    assert status == 200
    assert headers["content-type"] == "video/mp4"
    assert headers["content-length"] == str(world["entries"][0].size)
    assert len(body) == world["entries"][0].size
    assert headers["accept-ranges"] == "bytes"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    # No filename is disclosed anywhere in the response.
    assert headers["content-disposition"] == "inline"
    assert "filename" not in headers["content-disposition"]


def test_head_returns_metadata_without_a_body(world):
    status, headers, body = call(world["app"], clip_url(world), method="HEAD")
    assert status == 200 and body == b""
    assert headers["content-length"] == str(world["entries"][0].size)


def test_a_poster_is_served_under_the_same_authorization(world):
    url = f"/r5poster/{world['session']}/{world['page']}/{world['entries'][0].clip_id}"
    status, headers, body = call(world["app"], url)
    assert status == 200 and headers["content-type"] == "image/jpeg" and body


def test_a_missing_poster_is_a_404_not_a_leak(world):
    url = f"/r5poster/{world['session']}/{world['page']}/{world['entries'][1].clip_id}"
    status, _, body = call(world["app"], url)
    assert status == 404 and body == b"poster_unavailable"


# =============================================================================================
# 3. Range semantics
# =============================================================================================

def test_a_byte_range_returns_206_with_a_correct_content_range(world):
    size = world["entries"][0].size
    status, headers, body = call(world["app"], clip_url(world), headers={"Range": "bytes=0-99"})
    assert status == 206
    assert headers["content-range"] == f"bytes 0-99/{size}"
    assert headers["content-length"] == "100"
    assert len(body) == 100


def test_an_open_ended_range_serves_the_tail(world):
    size = world["entries"][0].size
    status, headers, body = call(world["app"], clip_url(world),
                                 headers={"Range": f"bytes={size - 50}-"})
    assert status == 206 and len(body) == 50
    assert headers["content-range"] == f"bytes {size - 50}-{size - 1}/{size}"


def test_a_suffix_range_serves_the_last_bytes(world):
    size = world["entries"][0].size
    status, headers, body = call(world["app"], clip_url(world), headers={"Range": "bytes=-100"})
    assert status == 206 and len(body) == 100
    assert headers["content-range"] == f"bytes {size - 100}-{size - 1}/{size}"


def test_the_range_body_is_the_right_bytes(world):
    """Not just the right length: the right offset. An off-by-one here would corrupt seeking."""
    with open(world["entries"][0].path, "rb") as handle:
        handle.seek(1000)
        expected = handle.read(200)
    _, _, body = call(world["app"], clip_url(world), headers={"Range": "bytes=1000-1199"})
    assert body == expected


@pytest.mark.parametrize("header,reason", [
    ("bytes=99999-100000", "range_start_beyond_eof"),
    ("bytes=500-100", "range_end_before_start"),
    ("bytes=0-10,20-30", "multipart_range_unsupported"),
    ("bytes=-0", "unsatisfiable_suffix_range"),
    ("bytes=" + "9" * 400, "range_header_too_long"),
])
def test_unsatisfiable_ranges_return_416(world, header, reason):
    status, _, body = call(world["app"], clip_url(world), headers={"Range": header})
    assert status == 416 and body == reason.encode()


@pytest.mark.parametrize("header", ["bytes=abc-def", "items=0-5", "bytes=-"])
def test_an_unparsable_range_is_ignored_and_the_full_body_is_sent(world, header):
    status, _, body = call(world["app"], clip_url(world), headers={"Range": header})
    assert status == 200 and len(body) == world["entries"][0].size


def test_the_body_is_streamed_in_bounded_chunks():
    """Peak memory must not scale with clip size. 64 KiB is the ceiling."""
    assert server.CHUNK_BYTES == 64 * 1024
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "clips_media_server.py"), encoding="utf-8").read()
    body = source[source.index("async def _serve"):source.index("async def _decide")]
    assert "os.read(fd, min(CHUNK_BYTES" in body
    assert "handle.read()" not in body and ".read()" not in body.replace("os.read(", "")


# =============================================================================================
# 4. Authorization refusals
# =============================================================================================

@pytest.mark.parametrize("builder,status,reason", [
    (lambda w: f"/r5clip/{w['session']}/{w['page']}/AAAAAAAAAAAAAAAAAAAAAA", 403,
     "clip_not_in_page"),
    (lambda w: f"/r5clip/{w['session']}/{w['page']}/{w['entries'][0].clip_id}x", 403,
     "clip_not_in_page"),
    (lambda w: f"/r5clip/nosuchsession00000/{w['page']}/{w['entries'][0].clip_id}", 403,
     "unknown_session"),
    (lambda w: f"/r5clip/{w['session']}/nosuchpage000000000/{w['entries'][0].clip_id}", 403,
     "unknown_page_token"),
    (lambda w: f"/r5clip/{w['session']}/{w['page']}", 404, "malformed_clip_url"),
    (lambda w: f"/r5clip/{w['session']}/{w['page']}/{w['entries'][0].clip_id}/extra", 404,
     "malformed_clip_url"),
    (lambda w: "/r5clip/", 404, "malformed_clip_url"),
])
def test_unauthorized_clip_requests_are_refused(world, builder, status, reason):
    got, _, body = call(world["app"], builder(world))
    assert got == status and body == reason.encode()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_and_head_may_read_a_clip(world, method):
    status, _, body = call(world["app"], clip_url(world), method=method)
    assert status == 405 and body == b"method_not_allowed"


def test_a_superseded_page_token_is_refused(world):
    """Publishing the next page revokes the previous one, so a tab left open on it cannot keep
    pulling bytes."""
    old_page, old_clip = world["page"], world["entries"][0].clip_id
    world["registry"].publish_page(world["owner"], world["session"], 2,
                                   cr.freeze_page(cr.discover_clips(world["review"], True).files,
                                                  0, 2))
    status, _, body = call(world["app"], f"/r5clip/{world['session']}/{old_page}/{old_clip}")
    assert status == 403 and body == b"expired_page_token"


def test_a_revoked_session_is_refused(world):
    world["registry"].revoke_owner(world["owner"])
    status, _, body = call(world["app"], clip_url(world))
    assert status == 403 and body in (b"unknown_session", b"revoked_session")


def test_another_sessions_credentials_do_not_work(world):
    """Cross-session isolation at the HTTP boundary."""
    other = world["registry"].new_session("session-hash-b", "scan-b")
    found = cr.discover_clips(world["review"], True)
    other_page, other_entries = world["registry"].publish_page(
        "session-hash-b", other, 1, cr.freeze_page(found.files, 0, 2))
    # B's page token under A's session id.
    status, _, body = call(world["app"], f"/r5clip/{world['session']}/{other_page}/"
                                         f"{other_entries[0].clip_id}")
    assert status == 403 and body == b"unknown_page_token"
    # A's clip id inside B's page.
    status, _, body = call(world["app"],
                           f"/r5clip/{other}/{other_page}/{world['entries'][0].clip_id}")
    assert status == 403 and body == b"clip_not_in_page"


# =============================================================================================
# 5. Traversal, absolute paths and the sentinel
# =============================================================================================

@pytest.mark.parametrize("path", [
    "/r5clip/../../../../Windows/win.ini",
    "/r5clip/%2e%2e%2f%2e%2e%2fWindows/win.ini",
    "/r5clip/%252e%252e%252fWindows/win.ini",
    "/r5clip/C:/Windows/win.ini",
    "/r5clip/a/b/C:%5CWindows%5Cwin.ini",
    "/r5clip/a/b/%5C%5Cserver%5Cshare",
    "/r5clip/a/b/sentinel.txt",
    "/r5clip/a/b/..",
    "/r5clip/a/b/.",
    "/r5clip/a/b/clip0001.mp4",
])
def test_no_url_text_can_become_a_filesystem_path(world, path):
    """Each of these is refused because the segments are opaque ids that are not in any page --
    never because a filter spotted a dangerous character. Nothing is joined to a path at all."""
    status, _, _ = call(world["app"], path)
    assert status in (403, 404)


def test_the_sentinel_bytes_never_appear_in_any_response(world):
    """The content-level proof. A status code says the request was refused; this says the secret
    did not come back in the body of anything we could think to ask for."""
    attempts = [
        "/r5clip/" + world["secret"].replace("\\", "/"),
        f"/r5clip/{world['session']}/{world['page']}/sentinel.txt",
        f"/r5clip/{world['session']}/{world['page']}/private.mp4",
        f"/r5poster/{world['session']}/{world['page']}/sentinel.txt",
        "/r5clip/../secret/sentinel.txt",
        f"/r5state/{world['session']}/{world['page']}",
    ]
    for path in attempts:
        _, _, body = call(world["app"], path)
        assert SENTINEL_TEXT not in body, path
    # And the sentinel was never enumerated in the first place. Compared on the BASENAME:
    # pytest's temporary directory is named after the test, so the full path legitimately
    # contains the word "sentinel" and matching on it would be a false positive.
    for entry in world["entries"]:
        name = os.path.basename(entry.path)
        assert "sentinel" not in name and "private" not in name


def test_no_response_body_discloses_a_path_or_a_filename(world):
    """Refusals return a stable machine token only -- not a path, not a stack, not a name."""
    for path in ("/r5clip/a/b/c", f"/r5clip/{world['session']}/{world['page']}/nope",
                 "/r5clip/x/y", f"/r5poster/{world['session']}/{world['page']}/nope"):
        _, _, body = call(world["app"], path)
        text = body.decode("utf-8", "replace")
        assert "\\" not in text and "/" not in text and ".mp4" not in text
        assert len(text) < 64


# =============================================================================================
# 6. Foreign origin
# =============================================================================================

@pytest.mark.parametrize("origin", ["http://evil.example", "https://evil.example",
                                    "http://127.0.0.1.evil.example", "null"])
def test_a_foreign_origin_cannot_drive_a_review_session(world, origin):
    for path, method in ((clip_url(world), "GET"),
                         (f"/r5decide/{world['session']}/{world['page']}/"
                          f"{world['entries'][0].clip_id}", "POST")):
        status, _, body = call(world["app"], path, method=method,
                               headers={"Origin": origin, "Content-Type": "application/json"},
                               body=b'{"to":"SELECTED"}')
        assert status == 403 and body == b"foreign_origin_refused"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:7860", "http://localhost:7860"])
def test_a_localhost_origin_is_accepted(world, origin):
    status, _, _ = call(world["app"], clip_url(world), headers={"Origin": origin})
    assert status == 200


def test_no_permissive_cors_header_is_ever_emitted(world):
    for path in (clip_url(world), f"/r5state/{world['session']}/{world['page']}"):
        _, headers, _ = call(world["app"], path)
        assert "access-control-allow-origin" not in headers


# =============================================================================================
# 7. Decisions
# =============================================================================================

def decide_url(world, index=0):
    return f"/r5decide/{world['session']}/{world['page']}/{world['entries'][index].clip_id}"


def test_an_authorized_decision_is_recorded(world):
    status, headers, body = call(world["app"], decide_url(world), method="POST",
                                 headers={"Content-Type": "application/json"},
                                 body=b'{"to":"SELECTED"}')
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] and payload["decision"] == "SELECTED" and payload["rev"] == 1
    assert payload["counts"]["SELECTED"] == 1
    assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("to", ["SELECTED", "REJECTED", "PENDING"])
def test_every_legal_transition_is_accepted(world, to):
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=json.dumps({"to": to}).encode())
    assert status == 200 and json.loads(body)["decision"] == to


@pytest.mark.parametrize("payload,code", [
    (b'{"to":"DELETE_FOREVER"}', "illegal_transition"),
    (b'{"to":"deleted"}', "illegal_transition"),
    (b'{"to":null}', "illegal_transition"),
    (b'{}', "illegal_transition"),
])
def test_an_illegal_transition_is_refused(world, payload, code):
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"}, body=payload)
    assert status == 409 and json.loads(body)["code"] == code


@pytest.mark.parametrize("payload,status,token", [
    (b"not json", 400, b"unparsable_body"),
    (b'["SELECTED"]', 400, b"body_not_an_object"),
    (b'"SELECTED"', 400, b"body_not_an_object"),
])
def test_a_malformed_decision_body_is_refused(world, payload, status, token):
    got, _, body = call(world["app"], decide_url(world), method="POST",
                        headers={"Content-Type": "application/json"}, body=payload)
    assert got == status and body == token


def test_an_oversized_decision_body_is_refused(world):
    payload = b'{"to":"SELECTED","pad":"' + b"A" * (server.MAX_BODY_BYTES + 100) + b'"}'
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"}, body=payload)
    assert status == 413 and body == b"body_too_large"


def test_a_path_in_the_decision_body_is_ignored(world):
    """The browser sends an opaque id and a desired state. A `path` it chose to include is not
    authority, and must not become one."""
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=json.dumps({"to": "SELECTED",
                                            "path": r"C:\Windows\win.ini"}).encode())
    assert status == 200 and json.loads(body)["ok"]


def test_a_forged_revision_cannot_force_a_write(world):
    call(world["app"], decide_url(world), method="POST",
         headers={"Content-Type": "application/json"}, body=b'{"to":"SELECTED"}')
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"REJECTED","rev":999}')
    payload = json.loads(body)
    assert status == 409 and payload["code"] == "revision_conflict"
    assert payload["decision"] == "SELECTED"


def test_a_decision_on_a_changed_clip_is_refused(world):
    with open(world["entries"][1].path, "ab") as handle:
        handle.write(b"CHANGED")
    status, _, body = call(world["app"], decide_url(world, 1), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED"}')
    assert status == 409 and json.loads(body)["code"] == "identity_changed"


def test_a_decision_on_a_guessed_clip_is_refused(world):
    status, _, body = call(world["app"],
                           f"/r5decide/{world['session']}/{world['page']}/AAAAAAAAAAAAAAAAAAAAAA",
                           method="POST", headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED"}')
    assert status == 403 and body == b"clip_not_in_page"


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE"])
def test_only_post_may_decide(world, method):
    status, _, body = call(world["app"], decide_url(world), method=method)
    assert status == 405 and body == b"method_not_allowed"


def test_a_decision_that_cannot_be_persisted_returns_503_not_200(world, monkeypatch):
    """The operator must never see a tick for a decision that is not on disk."""
    def refuse(self, event):
        raise cr.PersistenceError("disk full")

    # Patch the journal append specifically. An earlier version of this test patched `os.open`
    # globally, which also broke the descriptor-identity probe, so the handler refused with 409
    # before it ever reached the journal -- the test was measuring the wrong refusal.
    monkeypatch.setattr(cr.ReviewStore, "_append", refuse)
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED"}')
    assert status == 503 and json.loads(body)["code"] == "not_durably_recorded"


# =============================================================================================
# 8. State
# =============================================================================================

def test_the_state_endpoint_reports_the_servers_record(world):
    call(world["app"], decide_url(world), method="POST",
         headers={"Content-Type": "application/json"}, body=b'{"to":"REJECTED"}')
    status, _, body = call(world["app"], f"/r5state/{world['session']}/{world['page']}")
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] and payload["counts"]["REJECTED"] == 1
    assert payload["clips"][world["entries"][0].clip_id]["decision"] == "REJECTED"


def test_the_state_endpoint_exposes_no_paths(world):
    _, _, body = call(world["app"], f"/r5state/{world['session']}/{world['page']}")
    text = body.decode()
    for entry in world["entries"]:
        assert os.path.basename(entry.path) not in text
    assert "clip0001.mp4" not in text and "\\\\" not in text


@pytest.mark.parametrize("builder,reason", [
    (lambda w: "/r5state/nosuchsession00000/" + w["page"], b"unknown_session"),
    (lambda w: f"/r5state/{w['session']}/nosuchpage000000000", b"unknown_or_expired_page"),
    (lambda w: f"/r5state/{w['session']}", b"malformed_clip_url"),
])
def test_unauthorized_state_reads_are_refused(world, builder, reason):
    _, _, body = call(world["app"], builder(world))
    assert body == reason


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_the_state_endpoint_is_read_only(world, method):
    status, _, body = call(world["app"], f"/r5state/{world['session']}/{world['page']}",
                           method=method)
    assert status == 405 and body == b"method_not_allowed"


# =============================================================================================
# 9. The endpoint mutates no media
# =============================================================================================

def test_serving_and_deciding_never_touches_the_reviewed_files(world):
    """The read-only guarantee, measured rather than asserted: every reviewed file's bytes, size
    and mtime are unchanged after the full exercise above."""
    import hashlib
    before = {}
    for entry in world["entries"]:
        with open(entry.path, "rb") as handle:
            before[entry.path] = (hashlib.sha256(handle.read()).hexdigest(),
                                  os.stat(entry.path).st_size,
                                  os.stat(entry.path).st_mtime_ns)
    call(world["app"], clip_url(world))
    call(world["app"], clip_url(world), headers={"Range": "bytes=0-99"})
    call(world["app"], f"/r5poster/{world['session']}/{world['page']}/"
                       f"{world['entries'][0].clip_id}")
    call(world["app"], decide_url(world), method="POST",
         headers={"Content-Type": "application/json"}, body=b'{"to":"SELECTED"}')
    call(world["app"], f"/r5state/{world['session']}/{world['page']}")
    for entry in world["entries"]:
        with open(entry.path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        assert (digest, os.stat(entry.path).st_size, os.stat(entry.path).st_mtime_ns) \
            == before[entry.path]


def test_the_media_layer_contains_no_mutating_filesystem_call():
    """No transfer code, not even disabled. A move cannot be switched on by a flag if it is not
    written."""
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "clips_media_server.py"), encoding="utf-8").read()
    for forbidden in ("os.rename", "os.replace", "os.remove", "os.unlink", "shutil.move",
                      "shutil.copy", "shutil.rmtree", "os.rmdir", "os.makedirs", "O_WRONLY",
                      "O_CREAT", "MoveFileEx"):
        assert forbidden not in source, f"the media layer can {forbidden}"
