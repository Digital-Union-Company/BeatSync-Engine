#!/usr/bin/env python3
"""[FORK] Digital-Union (R5 Clips Preview Selector): the authorized media endpoint.

Why this exists at all. Gradio serves files only from its own `created_paths`
(`[upload_dir, cache_folder]`), so a path under `GRADIO_TEMP_DIR` is served `206` while an
arbitrary operator media path is served `403` -- measured, and already relied on by the delivery
preview. The two obvious ways out are both wrong: widening `allowed_paths` to a media root exposes
that whole tree to every request the browser can make, and copying a page of clips into Gradio's
cache duplicates gigabytes to show thumbnails.

So R5 serves its own bytes, through its own route, under its own authorization -- attached to the
**existing** app with one public parameter:

    app.launch(..., app_kwargs={"middleware": [Middleware(ClipMediaMiddleware, ...)]})

`Blocks.launch(app_kwargs=...)` forwards to the FastAPI constructor, which accepts
`middleware=[Middleware(...)]`. That keeps the operator's startup workflow exactly as it was: no
second Uvicorn, no `mount_gradio_app`, no launcher replacement, no new dependency (Starlette is
already a Gradio dependency), and `allowed_paths` / `set_static_paths` / `blocked_paths` untouched.

This is **pure ASGI** rather than Starlette's `BaseHTTPMiddleware` on purpose: the response body
is streamed in bounded chunks, so peak memory does not scale with clip size.

Four routes are claimed. Everything else is passed through to Gradio unchanged:

    GET|HEAD  /r5clip/{session}/{page}/{clip}     video bytes, byte-range capable
    GET|HEAD  /r5poster/{session}/{page}/{clip}   poster JPEG, same authorization *and* the same
                                                  live-source descriptor identity gate
    POST      /r5decide/{session}/{page}/{clip}   one classification, mandatory expected revision
    GET       /r5state/{session}/{page}           the server's authoritative record
"""

from __future__ import annotations

import json
import os

from beatsync_fork import clips_review as core

URL_PREFIX = "/r5clip"
POSTER_PREFIX = "/r5poster"
DECIDE_PREFIX = "/r5decide"
STATE_PREFIX = "/r5state"
PREFIXES = (URL_PREFIX, POSTER_PREFIX, DECIDE_PREFIX, STATE_PREFIX)

#: Bounded streaming. Peak memory is one chunk, whatever the clip weighs.
CHUNK_BYTES = 64 * 1024
#: A classification is a few dozen bytes. Anything larger is not one.
MAX_BODY_BYTES = 4096


class ClipMediaMiddleware:
    """Serve authorized clip bytes, posters and decisions; pass everything else through.

    The middleware owns no policy of its own. Authorization, identity revalidation and the
    decision record all live in ``beatsync_fork.clips_review``, which is stdlib-only and therefore
    testable without Gradio, Starlette or a browser. This layer is the HTTP shape around it.
    """

    def __init__(self, app, registry=None, poster_resolver=None,
                 decide_handler=None, state_handler=None):
        self.app = app
        self.registry = registry
        self.poster_resolver = poster_resolver
        self.decide_handler = decide_handler
        self.state_handler = state_handler

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        owned = None
        for prefix in PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                owned = prefix
                break
        if owned is None:
            await self.app(scope, receive, send)
            return
        try:
            # A cross-origin page must not be able to drive a review session. The app binds
            # 127.0.0.1 and adds no permissive CORS of its own; this check makes the media and
            # decision routes refuse a foreign Origin explicitly rather than relying on that.
            self._check_origin(scope)
            if owned == DECIDE_PREFIX:
                await self._decide(scope, receive, send)
            elif owned == STATE_PREFIX:
                await self._state(scope, send)
            else:
                await self._serve(scope, send, owned)
        except core.Refusal as refusal:
            if self.registry is not None:
                self.registry.note_refusal(refusal.reason, path)
            await _send_text(send, refusal.status, refusal.reason)
        except core.PersistenceError as exc:
            # Never report a decision as recorded when it is not on disk.
            await _send_json(send, 503, {"ok": False, "code": "not_durably_recorded",
                                         "detail": str(exc)[:200]})

    # ---- helpers ---------------------------------------------------------------------

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])}

    def _check_origin(self, scope) -> None:
        """Refuse an Origin that is not this machine.

        The HOST is compared exactly, never by prefix. `startswith("http://127.0.0.1")` would
        happily admit `http://127.0.0.1.evil.example`, whose host is `evil.example` -- a real
        bypass, caught by test rather than by inspection.
        """
        origin = self._headers(scope).get("origin")
        if not origin:
            return                                     # same-origin navigations send no Origin
        from urllib.parse import urlsplit
        try:
            parts = urlsplit(origin)
        except ValueError:
            raise core.Refusal(403, "foreign_origin_refused") from None
        if parts.scheme not in ("http", "https") or parts.hostname not in ("127.0.0.1",
                                                                           "localhost", "::1"):
            raise core.Refusal(403, "foreign_origin_refused")

    @staticmethod
    def _tokens(scope, prefix: str, count: int) -> list[str]:
        """Split the path into exactly ``count`` opaque segments.

        Nothing taken from here is ever joined to a filesystem path, so a ``..``, a drive letter
        or a percent-escape is simply a token that will not be found.
        """
        parts = scope["path"][len(prefix):].strip("/").split("/")
        if len(parts) != count or not all(parts):
            raise core.Refusal(404, "malformed_clip_url")
        return parts

    # ---- media -----------------------------------------------------------------------

    async def _serve(self, scope, send, prefix: str) -> None:
        method = scope.get("method", "GET").upper()
        if method not in ("GET", "HEAD"):
            raise core.Refusal(405, "method_not_allowed")
        session_id, page_token, clip_id = self._tokens(scope, prefix, 3)

        if prefix == POSTER_PREFIX:
            entry = self.registry.resolve(session_id, page_token, clip_id)
            # [R5-P3 / F5] The poster route revalidates the LIVE SOURCE on its opened descriptor,
            # exactly as the clip route does. Authorizing page membership and then serving a
            # cached JPEG was an asymmetry, not a design: a replaced or truncated source made the
            # clip route answer 409 while the poster route kept serving a thumbnail of a file
            # that no longer exists in that form, so the operator was shown a frame they could
            # not play and could not classify. `open_entry` is the same one implementation, so
            # the two routes cannot drift into checking different things, and it raises the same
            # 409/410/403 refusals. The fd is closed immediately: it is a gate, not the body.
            source_fd, entry = self.registry.open_entry(entry)
            os.close(source_fd)
            poster = self.poster_resolver(entry) if self.poster_resolver else None
            if not poster or not os.path.isfile(poster):
                raise core.Refusal(404, "poster_unavailable")
            fd = os.open(poster, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            size = os.fstat(fd).st_size
            content_type = "image/jpeg"
        else:
            fd, entry = self.registry.open_authorized(session_id, page_token, clip_id)
            size = entry.size
            content_type = core.content_type_for(entry.suffix)

        try:
            span = core.parse_range(self._headers(scope).get("range"), size)
            if span is None:
                start, end, status = 0, max(0, size - 1), 200
            else:
                start, end = span
                status = 206
            length = max(0, end - start + 1) if size else 0

            headers = [
                (b"content-type", content_type.encode()),
                (b"content-length", str(length).encode()),
                (b"accept-ranges", b"bytes"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                # No filename is disclosed: the browser must never learn the real name.
                (b"content-disposition", b"inline"),
            ]
            if status == 206:
                headers.append((b"content-range", f"bytes {start}-{end}/{size}".encode()))

            await send({"type": "http.response.start", "status": status, "headers": headers})
            if method == "HEAD" or length == 0:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                os.lseek(fd, start, os.SEEK_SET)
                remaining = length
                while remaining > 0:
                    chunk = os.read(fd, min(CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    await send({"type": "http.response.body", "body": chunk,
                                "more_body": remaining > 0})
                if remaining > 0:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            os.close(fd)
        if self.registry is not None:
            self.registry.note_serve()

    # ---- decisions -------------------------------------------------------------------

    async def _decide(self, scope, receive, send) -> None:
        if scope.get("method", "").upper() != "POST":
            raise core.Refusal(405, "method_not_allowed")
        if self.decide_handler is None:
            raise core.Refusal(404, "no_decide_handler")
        session_id, page_token, clip_id = self._tokens(scope, DECIDE_PREFIX, 3)

        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                raise core.Refusal(413, "body_too_large")
            if not message.get("more_body"):
                break
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise core.Refusal(400, "unparsable_body") from None
        if not isinstance(payload, dict):
            raise core.Refusal(400, "body_not_an_object")

        # The server resolves the opaque triple itself. The browser named no file, and any `path`
        # it chose to include in the body is ignored.
        entry = self.registry.resolve(session_id, page_token, clip_id)
        owner = self.registry.owner_of(session_id)
        result = self.decide_handler(owner, session_id, page_token, entry, payload)
        await _send_json(send, 200 if result.get("ok") else 409, result)

    async def _state(self, scope, send) -> None:
        if scope.get("method", "GET").upper() not in ("GET", "HEAD"):
            raise core.Refusal(405, "method_not_allowed")
        if self.state_handler is None:
            raise core.Refusal(404, "no_state_handler")
        session_id, page_token = self._tokens(scope, STATE_PREFIX, 2)
        self.registry.page_entries(session_id, page_token)             # authorization gate
        owner = self.registry.owner_of(session_id)
        await _send_json(send, 200, self.state_handler(owner, session_id, page_token))


async def _send_json(send, status: int, obj) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    await send({"type": "http.response.start", "status": int(status),
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_text(send, status: int, reason: str) -> None:
    """Refusals return only a stable machine token -- never a path, a filename or a stack."""
    body = str(reason).encode("utf-8")
    await send({"type": "http.response.start", "status": int(status),
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = ["CHUNK_BYTES", "DECIDE_PREFIX", "MAX_BODY_BYTES", "POSTER_PREFIX", "PREFIXES",
           "STATE_PREFIX", "URL_PREFIX", "ClipMediaMiddleware"]
