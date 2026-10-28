#!/usr/bin/env python3
"""[FORK] Digital-Union (R7.2 Audio Preview): the authorized **audio** media endpoint.

Why this exists at all is the R5 argument repeated for a second media type, so it is stated once
and briefly: Gradio serves files only from its own ``created_paths`` (``[upload_dir,
cache_folder]``), an operator's exported clip lives wherever they chose, and the two obvious ways
out are both wrong -- widening ``allowed_paths`` to a media root exposes that whole tree to every
request the browser can make, and copying a review page into Gradio's cache duplicates gigabytes to
play a few seconds of speech. R7.1's third way out was to inline the bytes as base64 data URIs
under a cumulative budget, and that is the defect this module replaces: a valid clip became
unplayable because earlier clips had spent the page's allowance.

**Everything about authorization is reused, not reimplemented.** The registry, the six gates, the
opaque ``(session, page, clip)`` triple and the descriptor-identity revalidation all live in
``beatsync_fork.clips_review`` and are shared byte for byte with the Clips Preview. What is new
here is one route and an audio ``Content-Type``:

    GET|HEAD  /r7audio/{session}/{page}/{clip}    audio bytes, byte-range capable

**Why a separate middleware rather than a fifth prefix on ``ClipMediaMiddleware``.** That class
claims exactly four prefixes and a test pins both halves of that -- the four it owns and the
everything-else it passes through. Audio needs no poster route, no decision route and no state
route (the audio gallery's decisions reach the server through its existing hidden field), so adding
a prefix there would widen a reviewed security boundary to carry a feature that uses none of it.
Two small classes, each claiming what it actually serves, is the cheaper thing to review.

Like its sibling this is **pure ASGI** rather than Starlette's ``BaseHTTPMiddleware``, so the body
streams in bounded chunks and peak memory does not scale with clip size. That is the property the
whole phase exists to obtain: an hour-long WAV is now streamed on demand instead of being refused.
"""

from __future__ import annotations

import os

from beatsync_fork import audio_review as audio
from beatsync_fork import clips_review as core

URL_PREFIX = audio.MEDIA_PREFIX_CUTTER
PREFIXES = audio.MEDIA_PREFIXES

#: Bounded streaming. Peak memory is one chunk, whatever the clip weighs.
CHUNK_BYTES = 64 * 1024


class AudioMediaMiddleware:
    """Serve authorized audio clip bytes; pass everything else through untouched.

    The middleware owns no policy of its own. Authorization and identity revalidation live in
    ``beatsync_fork.clips_review``; the audio media type lives in ``beatsync_fork.audio_review``.
    This layer is the HTTP shape around them.

    **One instance serves exactly one prefix and one registry**, and the pairing is the whole
    reason ``prefix`` is a parameter. The two audio surfaces hold separate registries because
    ``ClipRegistry.new_session`` revokes an owner's previous sessions, so a shared registry would
    let one surface's scan silently kill the other's live page. Two instances, each bound to its
    own registry, keep the prefix→registry mapping a construction-time fact rather than a lookup
    that could resolve a token against the wrong map.
    """

    def __init__(self, app, registry=None, prefix: str = URL_PREFIX):
        self.app = app
        self.registry = registry
        self.prefix = str(prefix)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not (path == self.prefix or path.startswith(self.prefix + "/")):
            await self.app(scope, receive, send)
            return
        try:
            # A cross-origin page must not be able to read a review session's audio. The app binds
            # 127.0.0.1 and adds no permissive CORS of its own; this makes the refusal explicit
            # rather than resting on that.
            self._check_origin(scope)
            await self._serve(scope, send)
        except core.Refusal as refusal:
            if self.registry is not None:
                self.registry.note_refusal(refusal.reason, path)
            await _send_text(send, refusal.status, refusal.reason)

    # ---- helpers ---------------------------------------------------------------------

    @staticmethod
    def _headers(scope) -> dict[str, str]:
        return {key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])}

    def _check_origin(self, scope) -> None:
        """Refuse an Origin that is not this machine.

        The HOST is compared exactly, never by prefix: ``startswith("http://127.0.0.1")`` would
        admit ``http://127.0.0.1.evil.example``, whose host is ``evil.example``. Same check, same
        reason and same test shape as the R5 route -- a real bypass, caught by test rather than by
        inspection.
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

    def _tokens(self, scope) -> list[str]:
        """Split the path into exactly three opaque segments.

        Nothing taken from here is ever joined to a filesystem path -- this module contains no
        ``os.path.join`` at all -- so a ``..``, a drive letter, a UNC prefix or a percent-escape is
        simply a token that will not be found in any page.
        """
        parts = scope["path"][len(self.prefix):].strip("/").split("/")
        if len(parts) != 3 or not all(parts):
            raise core.Refusal(404, "malformed_clip_url")
        return parts

    # ---- media -----------------------------------------------------------------------

    async def _serve(self, scope, send) -> None:
        method = scope.get("method", "GET").upper()
        if method not in ("GET", "HEAD"):
            raise core.Refusal(405, "method_not_allowed")
        session_id, page_token, clip_id = self._tokens(scope)

        # Authorize, open, and revalidate the OPENED DESCRIPTOR -- never the pathname. One shared
        # implementation with the video route, so the two cannot drift into checking different
        # things.
        fd, entry = self.registry.open_authorized(session_id, page_token, clip_id)
        size = entry.size
        content_type = audio.content_type_for(entry.suffix)

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
                # Range support is what makes Shift+wheel seeking work without the whole clip:
                # the browser asks for the bytes around the new position and nothing else.
                (b"accept-ranges", b"bytes"),
                (b"cache-control", b"no-store"),
                (b"x-content-type-options", b"nosniff"),
                # No filename is disclosed. The gallery shows the operator's chosen name from the
                # server-rendered markup; the HTTP layer never confirms what is on disk.
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


async def _send_text(send, status: int, reason: str) -> None:
    """Refusals return only a stable machine token -- never a path, a filename or a stack."""
    body = str(reason).encode("utf-8")
    await send({"type": "http.response.start", "status": int(status),
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store"),
                            (b"x-content-type-options", b"nosniff")]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


__all__ = ["CHUNK_BYTES", "PREFIXES", "URL_PREFIX", "AudioMediaMiddleware"]
