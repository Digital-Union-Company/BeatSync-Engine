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
import stat

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
        idents = list(view["identity"].values())
        # [R5-P3 / F2] Mirrors the real `gui._clips_decide`: the expected revision is MANDATORY
        # and strictly typed, validated BEFORE anything is written. This double used to replicate
        # the `int(...)` coercion that turned every malformed revision into "no concurrency check
        # requested" -- keeping that here would have left the endpoint's decision tests blind to
        # the defect.
        try:
            expected = cr.parse_expected_revision(payload)
        except cr.RevisionError as bad:
            current, revision = store.get(ident)
            return {"ok": False, "code": bad.code, "clip": entry.clip_id,
                    "decision": current, "rev": revision, "counts": store.counts(idents)}
        if not registry.live_identity_ok(entry):
            current, revision = store.get(ident)
            return {"ok": False, "code": "identity_changed", "clip": entry.clip_id,
                    "decision": current, "rev": revision, "counts": store.counts(idents)}
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
                               body=b'{"to":"SELECTED","rev":0}')
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
                                 body=b'{"to":"SELECTED","rev":0}')
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] and payload["decision"] == "SELECTED" and payload["rev"] == 1
    assert payload["counts"]["SELECTED"] == 1
    assert headers["content-type"] == "application/json"


@pytest.mark.parametrize("to", ["SELECTED", "REJECTED", "PENDING"])
def test_every_legal_transition_is_accepted(world, to):
    # [R5-P3 / F2] `rev` is now mandatory, so the correct current revision (0, nothing recorded
    # yet) travels with the transition. The assertion itself is unchanged.
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=json.dumps({"to": to, "rev": 0}).encode())
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
                           body=json.dumps({"to": "SELECTED", "rev": 0,
                                            "path": r"C:\Windows\win.ini"}).encode())
    assert status == 200 and json.loads(body)["ok"]


def test_a_forged_revision_cannot_force_a_write(world):
    call(world["app"], decide_url(world), method="POST",
         headers={"Content-Type": "application/json"}, body=b'{"to":"SELECTED","rev":0}')
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
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 409 and json.loads(body)["code"] == "identity_changed"


def test_a_decision_on_a_guessed_clip_is_refused(world):
    status, _, body = call(world["app"],
                           f"/r5decide/{world['session']}/{world['page']}/AAAAAAAAAAAAAAAAAAAAAA",
                           method="POST", headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
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
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 503 and json.loads(body)["code"] == "not_durably_recorded"


def test_an_unverifiable_journal_tail_returns_503_and_records_nothing(world, monkeypatch):
    """[R5-P5 / N2] The same truthful 503, for the condition the independent review found.

    The journal tail probe used to answer "already terminated" on an `OSError`, so the append
    went ahead against an unverified -- possibly damaged -- tail and this endpoint answered
    **200** for a decision that recovered as PENDING. It must answer 503.

    The injection is scoped to one file. Patching `os.path.getsize` for everything would make the
    descriptor-identity probe fail first and the endpoint would refuse 409 before the journal was
    ever reached -- the same trap the test above records about `os.open`.
    """
    store = world["store"]
    journal = store.journal_path
    real_getsize = os.path.getsize

    def blind(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(journal)):
            raise OSError(5, "simulated I/O error reading the journal size")
        return real_getsize(path)

    with open(journal, "rb") as handle:
        before = handle.read()

    monkeypatch.setattr(cr.os.path, "getsize", blind)
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
    monkeypatch.undo()

    assert status == 503, "never 200 for a decision that is not on disk"
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    with open(journal, "rb") as handle:
        assert handle.read() == before, "a refused decision appends nothing"
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    assert store.get(identity) == (cr.PENDING, 0), "no revision moved"

    # The 503 body is operator-facing. It must not disclose media or journal paths.
    text = body.decode("utf-8", "replace")
    assert world["review"] not in text and world["secret"] not in text
    assert ".mp4" not in text and journal not in text
    assert SENTINEL_TEXT not in body

    # And the endpoint recovers: with the fault cleared the same decision is accepted.
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 200 and json.loads(body)["rev"] == 1


#: [R5-P7 / N6] The Windows codes CPython maps onto `FileNotFoundError`. Five of them mean "the
#: pathname did not resolve", not "the file is absent", and the R5-P5 carve-out read all six as
#: proof of a terminated tail -- so this endpoint answered **200** for a decision that recovered
#: as PENDING. The store-level matrix lives in `test_clips_review.py`; this is the boundary.
_FILE_NOT_FOUND_WINERRORS = (2, 3, 15, 53, 67, 161)


def _decide_once(world, revision=0):
    return call(world["app"], decide_url(world), method="POST",
                headers={"Content-Type": "application/json"},
                body=b'{"to":"SELECTED","rev":%d}' % revision)


def _mentions(haystack, needle):
    """Backslash-insensitive containment, for proving a path is NOT in a response body.

    `str(OSError)` reprs `filename`/`filename2`, doubling every backslash, and JSON encoding
    doubles them again -- so the obvious `path in text` test passes against a body that really
    does carry the path. Stripping the separators is immune to the layers of escaping.
    """
    return needle.replace("\\", "").replace("/", "") in \
        haystack.replace("\\", "").replace("/", "")


def _no_paths_in(body, world, journal):
    """Nothing in a 503 body may name the journal, the state directory or any source media."""
    text = body.decode("utf-8", "replace")
    assert not _mentions(text, journal), f"the journal path reached the client: {text}"
    assert not _mentions(text, os.path.dirname(journal))
    assert "review-journal" not in text and ".jsonl" not in text
    assert not _mentions(text, world["review"]) and not _mentions(text, world["secret"])
    assert ".mp4" not in text
    assert SENTINEL_TEXT not in body
    return text


@pytest.mark.parametrize("winerror", _FILE_NOT_FOUND_WINERRORS)
def test_a_file_not_found_journal_tail_returns_503_not_200(world, monkeypatch, winerror):
    """[R5-P7 / N6] Each code, through the shipped middleware, with a genuinely damaged tail.

    The injection delegates by path for the same reason as the test above: patching
    `os.path.getsize` for everything makes the descriptor-identity probe fail first and the
    handler refuses 409 before the journal is reached, which would make this pass vacuously.
    """
    store = world["store"]
    journal = store.journal_path
    with open(journal, "ab") as handle:
        handle.write(b'{"k":"decide","identity":"torn","decision":"SELE')
    with open(journal, "rb") as handle:
        before = handle.read()
    assert not before.endswith(b"\n"), "the tail really is unterminated"

    real_getsize = os.path.getsize
    target = os.path.normcase(os.path.abspath(journal))

    def blind(path):
        if os.path.normcase(os.path.abspath(path)) == target:
            raise OSError(0, "simulated path-resolution failure", None, winerror)
        return real_getsize(path)

    monkeypatch.setattr(cr.os.path, "getsize", blind)
    status, _, body = _decide_once(world)
    monkeypatch.undo()

    assert status == 503, f"winerror {winerror} answered {status}, never 200"
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    assert "errno" in payload["detail"], "the condition stays diagnosable"
    _no_paths_in(body, world, journal)

    with open(journal, "rb") as handle:
        assert handle.read() == before, "a refused decision appends nothing"
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    assert store.get(identity) == (cr.PENDING, 0), "no revision moved"
    assert store.journal_tails_repaired == 0

    # The refusal consumed nothing, so the operator's original expected revision still applies.
    status, _, body = _decide_once(world)
    assert status == 200 and json.loads(body)["rev"] == 1


def test_the_503_body_never_discloses_the_journal_path_when_it_cannot_be_opened(world):
    """[R5-P7] The pre-existing disclosure R5-P6 confirmed, pinned at the boundary it reaches.

    No fault injection: a read-only ACL on the journal is enough to make `os.open` fail with the
    filename attached, which is how the absolute path used to land in this body. An ACL change or
    a backup tool can do exactly this, so the condition is ordinary rather than contrived.
    """
    journal = world["store"].journal_path
    os.chmod(journal, stat.S_IREAD)
    try:
        status, _, body = _decide_once(world)
    finally:
        os.chmod(journal, stat.S_IREAD | stat.S_IWRITE)

    assert status == 503
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    text = _no_paths_in(body, world, journal)
    assert "Permission denied" not in text, "no raw OSError text"
    assert "errno 13" in text, "the condition stays diagnosable"

    # Writable again: the same decision is accepted, so the refusal left nothing wedged.
    status, _, body = _decide_once(world)
    assert status == 200 and json.loads(body)["rev"] == 1


# =============================================================================================
# 8. State
# =============================================================================================

def test_the_state_endpoint_reports_the_servers_record(world):
    call(world["app"], decide_url(world), method="POST",
         headers={"Content-Type": "application/json"}, body=b'{"to":"REJECTED","rev":0}')
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
         headers={"Content-Type": "application/json"}, body=b'{"to":"SELECTED","rev":0}')
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


# =============================================================================================
# [R5-P3] Independent-review remediation at the HTTP boundary: F5 and F2
# =============================================================================================

#: A COMPLETE minimal JPEG. The poster route does not parse JPEG structure -- publication is
#: atomic, so a published poster is always whole -- but using a realistic value keeps these
#: fixtures honest about what the cache holds.
_JPEG = b"\xff\xd8\xff\xdb" + b"\x00" * 48 + b"\xff\xd9"


def _poster_url(world, index=0):
    return f"/r5poster/{world['session']}/{world['page']}/{world['entries'][index].clip_id}"


def _clip_url(world, index=0):
    return f"/r5clip/{world['session']}/{world['page']}/{world['entries'][index].clip_id}"


# ---- F5: the poster route validates the live source ----------------------------------------

def test_a_valid_clip_and_poster_are_served(world):
    status, headers, body = call(world["app"], _poster_url(world))
    assert status == 200
    assert headers["content-type"] == "image/jpeg"
    assert body.startswith(b"\xff\xd8\xff")


def test_a_poster_is_refused_once_the_source_inode_changes(world, tmp_path):
    """F5. The poster route used to authorize page membership and then open the cached JPEG with
    no live-source check, so this request kept returning a thumbnail of a file that no longer
    existed in that form -- while /r5clip correctly answered 409. The operator was shown a frame
    they could neither play nor classify."""
    import time as _time
    entry = world["entries"][0]
    before = call(world["app"], _poster_url(world))
    assert before[0] == 200, "the poster is there to begin with"

    _time.sleep(0.02)
    replacement = _write(str(tmp_path / "replacement.bin"), b"B" * entry.size)
    os.replace(replacement, entry.path)           # same size, different inode

    clip_status, _, clip_body = call(world["app"], _clip_url(world))
    poster_status, _, poster_body = call(world["app"], _poster_url(world))
    assert clip_status == 409 and clip_body == b"identity_changed"
    assert poster_status == 409 and poster_body == b"identity_changed", (
        "both media routes must reach the same verdict about the same source")
    assert before[2] not in poster_body, "the stale poster bytes must not be served"


def test_a_poster_is_refused_once_the_source_size_changes(world):
    entry = world["entries"][0]
    with open(entry.path, "ab") as handle:
        handle.write(b"X" * 32)
    assert call(world["app"], _poster_url(world))[:2][0] == 409
    assert call(world["app"], _clip_url(world))[0] == 409


def test_a_poster_is_refused_once_the_source_mtime_changes(world):
    entry = world["entries"][0]
    info = os.stat(entry.path)
    os.utime(entry.path, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    status, _, body = call(world["app"], _poster_url(world))
    assert status == 409 and body == b"identity_changed"


def test_a_poster_is_refused_once_the_source_is_deleted(world):
    os.unlink(world["entries"][0].path)
    status, _, body = call(world["app"], _poster_url(world))
    assert status == 410 and body == b"source_unavailable"
    assert call(world["app"], _clip_url(world))[0] == 410


def test_an_unchanged_source_with_no_poster_falls_back_normally(world):
    """The second clip deliberately has no poster. An unplayable or un-postered clip must stay
    classifiable, so this is a plain 404 rather than an identity refusal."""
    status, _, body = call(world["app"], _poster_url(world, 1))
    assert status == 404 and body == b"poster_unavailable"


def test_poster_authorization_still_precedes_identity(world):
    """A foreign credential must be refused without the source ever being opened."""
    entry = world["entries"][0]
    os.unlink(entry.path)                          # would be 410 if identity were checked first
    bad_session = f"/r5poster/nosuchsession00000/{world['page']}/{entry.clip_id}"
    status, _, body = call(world["app"], bad_session)
    assert status == 403 and body == b"unknown_session"


def test_a_poster_refusal_discloses_no_path_or_filename(world, tmp_path):
    entry = world["entries"][0]
    os.unlink(entry.path)
    bodies = [call(world["app"], _poster_url(world))[2],
              call(world["app"], _poster_url(world, 1))[2]]
    for body in bodies:
        assert str(tmp_path).encode() not in body
        assert b"clip0001" not in body and b".mp4" not in body and b".jpg" not in body


def test_the_poster_route_uses_the_same_open_entry_as_the_clip_route():
    """Structural: one implementation, so the two routes cannot drift into checking different
    things. A pathname stat would reintroduce the time-of-check/time-of-use gap."""
    import ast
    source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "src", "clips_media_server.py"), encoding="utf-8").read()
    serve = source[source.index("async def _serve"):source.index("async def _decide")]
    poster_branch = serve[serve.index("if prefix == POSTER_PREFIX:"):serve.index("else:")]
    assert "open_entry" in poster_branch, "the poster branch must revalidate the live source"
    assert "os.stat(" not in poster_branch, "a pathname stat is not a descriptor check"
    tree = ast.parse(source)
    assert "os.path.join" not in source, "the HTTP layer still builds no paths"
    assert tree is not None


# ---- F2: the mandatory expected revision, through the real endpoint ------------------------

@pytest.mark.parametrize("body,code", [
    (b'{"to":"REJECTED"}', "revision_required"),
    (b'{"to":"REJECTED","rev":null}', "revision_required"),
    (b'{"to":"REJECTED","rev":"1"}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":"abc"}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":"1abc"}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":true}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":false}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":0.9}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":1.0}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":-1}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":[]}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":{}}', "malformed_revision"),
    (b'{"to":"REJECTED","rev":0}', "revision_conflict"),
    (b'{"to":"REJECTED","rev":99}', "revision_conflict"),
])
def test_no_malformed_or_stale_revision_can_overwrite_a_decision(world, body, code):
    """F2, at the boundary that matters. Every one of these used to be accepted except the two
    well-formed integers, because a malformed revision was coerced to None and None means
    "no concurrency check requested"."""
    first = call(world["app"], decide_url(world), method="POST",
                 headers={"Content-Type": "application/json"},
                 body=b'{"to":"SELECTED","rev":0}')
    assert first[0] == 200 and json.loads(first[2])["rev"] == 1

    journal = os.path.join(world["store"].dir, "review-journal.jsonl")
    with open(journal, "rb") as handle:
        before = handle.read()

    status, _, refused = call(world["app"], decide_url(world), method="POST",
                              headers={"Content-Type": "application/json"}, body=body)
    payload = json.loads(refused)
    assert status == 409, payload
    assert payload["code"] == code and payload["ok"] is False
    assert payload["decision"] == "SELECTED" and payload["rev"] == 1, (
        "a refusal reports the server's current record so a stale tab can resync")

    with open(journal, "rb") as handle:
        assert handle.read() == before, "a refused decision must not reach the journal"
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    assert world["store"].get(identity) == ("SELECTED", 1), "and must not move the record"


def test_only_the_exact_current_revision_is_accepted(world):
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 200 and json.loads(body)["rev"] == 1
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"REJECTED","rev":1}')
    assert status == 200 and json.loads(body)["decision"] == "REJECTED"
    assert json.loads(body)["rev"] == 2


def test_an_illegal_transition_is_refused_before_the_revision_is_considered(world):
    """Ordering matters for the refusal codes: a body that names no legal decision at all is
    not a concurrency problem."""
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"DELETE_FOREVER","rev":0}')
    assert status == 409 and json.loads(body)["code"] == "illegal_transition"
    status, _, body = call(world["app"], decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"}, body=b'{}')
    assert status == 409 and json.loads(body)["code"] == "illegal_transition"


# =============================================================================================
# [R5-P11 / N7] An unreadable durable record answers 503, never a successful classification
#
# The fixture above captures an already-constructed `ReviewStore` in its handler closures, so it
# cannot reach the condition this section is about: `gui._clips_store()` creates the store on
# **first use**, which means the constructor's refusal is raised inside the request. These tests
# therefore build a middleware whose handlers obtain the store lazily, the way the shipped ones
# do, and then make the recovery fail.
#
# The answer must be the existing persistence contract -- 503 `not_durably_recorded` with a
# path-free body -- rather than a 200 reporting every clip as `PENDING`, or a 409 that would make
# an unreadable record look like an ordinary refusal. Nothing in the middleware changes for this:
# it already maps the persistence failure to that response, and raising the right class from the
# store is the whole of the fix at this boundary.

def _deny_reads(monkeypatch, path):
    """`open` that refuses READ opens of exactly one file and delegates for every other.

    Narrow by the convention `test_clips_review.py` records: a global patch breaks the
    descriptor-identity probe, and the endpoint then refuses 409 before the record is reached --
    which is the trap `test_a_decision_that_cannot_be_persisted_returns_503_not_200` exists to
    remember.
    """
    real = open
    target = os.path.normcase(os.path.abspath(path))

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            raise PermissionError(13, "access denied", str(file), 32)
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)


def _lazily_served(world, state_dir):
    """A middleware whose decision and state handlers create the store on first use."""
    cache = {}
    view = world["view"]
    identities = list(view["identity"].values())

    def store():
        if "store" not in cache:
            cache["store"] = cr.ReviewStore(state_dir)   # may raise PersistenceError
        return cache["store"]

    def decide(owner_in, session_in, page_in, entry, payload):
        if session_in != view["session"] or page_in != view["page"]:
            return {"ok": False, "code": "not_the_active_page"}
        current = store()
        identity = view["identity"][entry.clip_id]
        expected = cr.parse_expected_revision(payload)
        decision, revision = current.decide(owner_in, identity, payload["to"],
                                            current.current_scan(owner_in), expected)
        return {"ok": True, "clip": entry.clip_id, "decision": decision, "rev": revision,
                "counts": current.counts(identities)}

    def state(owner_in, session_in, page_in):
        if session_in != view["session"] or page_in != view["page"]:
            return {"ok": False, "code": "not_the_active_page"}
        current = store()
        snapshot = current.snapshot_of(identities)
        return {"ok": True, "counts": current.counts(identities),
                "clips": {e.clip_id: {"decision": snapshot[view["identity"][e.clip_id]][0],
                                      "rev": snapshot[view["identity"][e.clip_id]][1]}
                          for e in view["entries"]}}

    app = server.ClipMediaMiddleware(
        Downstream(), registry=world["registry"],
        poster_resolver=lambda entry: cr.poster_path_for(world["posters"], entry),
        decide_handler=decide, state_handler=state)
    return app, cache


def _seed(state_dir, owner):
    store = cr.ReviewStore(state_dir)
    store.begin_scan(owner, "scan-1")
    return store


def _durable_bytes(store):
    out = {}
    for name, path in (("snapshot", store.snapshot_path), ("journal", store.journal_path)):
        try:
            with open(path, "rb") as handle:
                out[name] = handle.read()
        except FileNotFoundError:
            out[name] = None
    return out


def _leaks(text, secret):
    """Separator-stripped containment: `str(OSError)` reprs its filenames and JSON escapes them
    again, so a plain `in` passes against a body that really is disclosing the path (R5-P7)."""
    strip = lambda value: value.replace("\\", "").replace("/", "")       # noqa: E731
    return strip(secret) in strip(text)


def test_n7_case18_a_decision_whose_record_cannot_be_read_answers_503(world, tmp_path,
                                                                      monkeypatch):
    """Case 18, the write half. A classification against an unreadable record is not a refusal
    the client can act on by resyncing -- it is a durability failure, and 503 is the one answer
    that does not claim anything about the decision."""
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    app, cache = _lazily_served(world, state_dir)

    # Control: the same request, with the record readable. The baseline is taken AFTER it, so the
    # comparison below is against a journal that already holds a committed decision.
    status, _, body = call(app, decide_url(world), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 200 and json.loads(body)["ok"] is True, body
    cache.clear()                               # the lazy store has not been created yet
    before = _durable_bytes(seeded)

    _deny_reads(monkeypatch, seeded.journal_path)
    status, headers, body = call(app, decide_url(world, 1), method="POST",
                                 headers={"Content-Type": "application/json"},
                                 body=b'{"to":"REJECTED","rev":0}')
    monkeypatch.undo()

    assert status == 503, "never 200, and never a 409 the client would try to resync away"
    payload = json.loads(body)
    assert payload == {"ok": False, "code": "not_durably_recorded",
                       "detail": payload["detail"]}, payload
    assert "could not read the review journal" in payload["detail"]
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert _durable_bytes(seeded) == before, "a refused decision writes nothing"
    assert "store" not in cache, "a refusal must not be cached"

    # And the retry succeeds, on the same already-authorized page.
    status, _, body = call(app, decide_url(world, 1), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"REJECTED","rev":0}')
    assert status == 200 and json.loads(body)["decision"] == "REJECTED", body


def test_n7_case18_the_state_route_also_answers_503_rather_than_an_empty_record(world, tmp_path,
                                                                                monkeypatch):
    """Case 18, the read half -- and the more dangerous one.

    `/r5state` exists to be believed: the client repaints the whole page from it. A 200 whose
    `clips` map reported every decision as `PENDING` would make the browser *show* a reviewed
    folder as untouched, with `ok: true` attached.
    """
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    seeded.decide(world["owner"], identity, cr.SELECTED, "scan-1")
    app, cache = _lazily_served(world, state_dir)

    url = f"/r5state/{world['session']}/{world['page']}"
    status, _, body = call(app, url)
    assert status == 200
    assert json.loads(body)["clips"][world["entries"][0].clip_id]["decision"] == "SELECTED"
    cache.clear()

    _deny_reads(monkeypatch, seeded.journal_path)
    status, _, body = call(app, url)
    monkeypatch.undo()

    assert status == 503, body
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    assert "clips" not in payload and "counts" not in payload, (
        "no partial record may be reported alongside the failure")

    status, _, body = call(app, url)
    assert status == 200
    assert json.loads(body)["clips"][world["entries"][0].clip_id]["decision"] == "SELECTED", (
        "the record was never in danger; only the reading of it failed")


@pytest.mark.parametrize("route", ["decide", "state"])
def test_n7_the_503_body_never_discloses_a_filesystem_path(world, tmp_path, monkeypatch, route):
    """Both routes' bodies reach an authorized but untrusted client. Neither may carry a path.

    The failure is injected with the filenames attached, which is how a real pathname call raises
    one -- `str(OSError)` would otherwise put the journal's absolute path in the JSON body.
    """
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    app, _ = _lazily_served(world, state_dir)
    journal = seeded.journal_path

    real = open
    target = os.path.normcase(os.path.abspath(journal))

    def opener(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if (os.path.normcase(os.path.abspath(os.fspath(file))) == target
                and "r" in str(mode) and "+" not in str(mode)):
            raise OSError(13, "simulated failure", journal, 5, journal + ".second")
        return real(file, *args, **kwargs)

    monkeypatch.setattr(cr, "open", opener, raising=False)
    if route == "decide":
        status, _, body = call(app, decide_url(world), method="POST",
                               headers={"Content-Type": "application/json"},
                               body=b'{"to":"SELECTED","rev":0}')
    else:
        status, _, body = call(app, f"/r5state/{world['session']}/{world['page']}")
    monkeypatch.undo()

    assert status == 503
    text = body.decode("utf-8")
    for secret in (journal, journal + ".second", os.path.dirname(journal),
                   os.path.basename(os.path.dirname(journal))):
        assert not _leaks(text, secret), f"a path reached the client: {text}"
    assert "review-journal" not in text and ".jsonl" not in text
    assert "simulated failure" not in text, "nor the raw OSError text"
    assert "errno 13, winerror 5" in text, "the condition stays diagnosable"


# ---- R5-P13 / N7-R1 + N7-R2: the 503 boundary, for state that is READABLE but unaccountable ---
#
# Case 18 above was measured with a denied read. R5-P12 found that a readable-but-unaccountable
# record took a different route entirely: the store reported every clip as `PENDING` with
# `ok: true`, and two of the conditions (a non-numeric revision, non-UTF-8 bytes) escaped the store
# as a bare `ValueError`/`UnicodeDecodeError`, which the middleware's typed branch does not catch
# at all. Nothing in the middleware changes for this -- raising the right class is the whole fix --
# and these tests are what makes that claim checkable. No fault is injected: the bytes on disk are
# genuinely corrupt.


def _damage_a_committed_record(store):
    """Damage one historical committed decision, leaving later valid records after it."""
    with open(store.journal_path, "rb") as handle:
        lines = handle.read().splitlines(keepends=True)
    assert len(lines) >= 3, lines
    lines[1] = b"{ damaged committed record\n"
    with open(store.journal_path, "wb") as handle:
        handle.write(b"".join(lines))


@pytest.mark.parametrize("damage", ["malformed_history", "bad_schema", "not_utf8"])
def test_n7r2_a_decision_against_an_unaccountable_record_answers_503(world, tmp_path, damage):
    """The write half, for each shape R5-P12 found outside the typed contract."""
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    seeded.decide(world["owner"], identity, cr.SELECTED, "scan-1")
    app, cache = _lazily_served(world, state_dir)

    status, _, body = call(app, decide_url(world, 1), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"SELECTED","rev":0}')
    assert status == 200 and json.loads(body)["ok"] is True, body
    cache.clear()

    if damage == "malformed_history":
        _damage_a_committed_record(seeded)
    else:
        extra = (b'{"k":"decide","identity":"x","decision":"SELECTED","revision":"x"}\n'
                 if damage == "bad_schema" else b"\xff\xfe not utf-8\n")
        with open(seeded.journal_path, "rb") as handle:
            lines = handle.read().splitlines(keepends=True)
        lines.insert(1, extra)
        with open(seeded.journal_path, "wb") as handle:
            handle.write(b"".join(lines))
    before = _durable_bytes(seeded)

    status, headers, body = call(app, decide_url(world, 2), method="POST",
                                 headers={"Content-Type": "application/json"},
                                 body=b'{"to":"REJECTED","rev":0}')

    assert status == 503, f"{damage}: never 200, and never a 409 the client would resync away"
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    assert headers["cache-control"] == "no-store"
    assert _durable_bytes(seeded) == before, "a refused decision writes nothing"
    assert "store" not in cache, "a refusal must not be cached"
    text = body.decode("utf-8")
    for secret in (seeded.journal_path, os.path.dirname(seeded.journal_path)):
        assert not _leaks(text, secret), f"a path reached the client: {text}"
    assert "review-journal" not in text and ".jsonl" not in text


def test_n7r2_the_state_route_answers_503_for_an_unaccountable_record(world, tmp_path):
    """The read half, which is the dangerous one: `/r5state` repaints the whole page.

    Pre-fix this answered 200 with every decision reported `PENDING`, so a reviewed folder would
    have been *shown* as untouched with `ok: true` attached.
    """
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    seeded.decide(world["owner"], identity, cr.SELECTED, "scan-1")
    seeded.decide(world["owner"], identity, cr.REJECTED, "scan-1")
    app, cache = _lazily_served(world, state_dir)

    url = f"/r5state/{world['session']}/{world['page']}"
    status, _, body = call(app, url)
    assert status == 200
    assert json.loads(body)["clips"][world["entries"][0].clip_id]["decision"] == "REJECTED"
    cache.clear()

    _damage_a_committed_record(seeded)
    before = _durable_bytes(seeded)
    status, _, body = call(app, url)

    assert status == 503, body
    payload = json.loads(body)
    assert payload["ok"] is False and payload["code"] == "not_durably_recorded"
    assert "clips" not in payload and "counts" not in payload, (
        "no partial record may be reported alongside the failure")
    assert _durable_bytes(seeded) == before


def test_n7r1_an_unaccountable_snapshot_answers_503_on_both_routes(world, tmp_path):
    """The N7-R1 shape at this boundary: a compacted store whose snapshot cannot be accounted for.

    The journal is empty after compaction, so the snapshot is the only copy. Both routes must
    refuse, and the retained bytes must still be there afterwards.
    """
    state_dir = str(tmp_path / "lazy-state")
    seeded = _seed(state_dir, world["owner"])
    identity = world["view"]["identity"][world["entries"][0].clip_id]
    seeded.decide(world["owner"], identity, cr.SELECTED, "scan-1")
    seeded.compact()
    assert os.path.getsize(seeded.journal_path) == 0
    snapshot = json.loads(open(seeded.snapshot_path, encoding="utf-8").read())
    snapshot["records"]["damaged-entry"] = {"revision": 1}
    with open(seeded.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    before = _durable_bytes(seeded)
    app, cache = _lazily_served(world, state_dir)

    status, _, body = call(app, f"/r5state/{world['session']}/{world['page']}")
    assert status == 503, body
    assert json.loads(body)["code"] == "not_durably_recorded"
    cache.clear()
    status, _, body = call(app, decide_url(world, 1), method="POST",
                           headers={"Content-Type": "application/json"},
                           body=b'{"to":"REJECTED","rev":0}')
    assert status == 503, body
    assert json.loads(body)["code"] == "not_durably_recorded"
    assert _durable_bytes(seeded) == before, "the only copy of the record is untouched"

    snapshot["records"].pop("damaged-entry")
    with open(seeded.snapshot_path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle)
    cache.clear()
    status, _, body = call(app, f"/r5state/{world['session']}/{world['page']}")
    assert status == 200
    assert json.loads(body)["clips"][world["entries"][0].clip_id]["decision"] == "SELECTED", (
        "the record was never in danger; only this build's ability to account for it was")
