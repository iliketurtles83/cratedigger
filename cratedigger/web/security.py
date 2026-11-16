"""Local web UI host and cross-site request protections."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_COOKIE = "cratedigger_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\.[0-9a-f]{64}")
_HOST_RE = re.compile(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", re.IGNORECASE)


def _authority(value: str) -> tuple[str, int | None] | None:
    if not _HOST_RE.fullmatch(value):
        return None
    try:
        parsed = urlsplit("//" + value)
        if (
            parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or value.endswith(":")
        ):
            return None
        return parsed.hostname, parsed.port
    except ValueError:
        return None


def _same_origin(origin: str, host: str, scheme: str) -> bool:
    try:
        parsed = urlsplit(origin)
        if parsed.scheme != scheme or parsed.path or parsed.query or parsed.fragment:
            return False
        origin_authority = _authority(parsed.netloc)
        host_authority = _authority(host)
        if origin_authority is None or host_authority is None:
            return False
        default_port = 443 if scheme == "https" else 80
        return (
            origin_authority[0] == host_authority[0]
            and (origin_authority[1] or default_port) == (host_authority[1] or default_port)
        )
    except ValueError:
        return False


class LocalSecurityMiddleware(BaseHTTPMiddleware):
    """Reject non-local hosts and require a signed session token for mutations."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._secret = secrets.token_bytes(32)

    def _new_token(self) -> str:
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{nonce}.{signature}"

    def _valid_token(self, token: str) -> bool:
        if not _TOKEN_RE.fullmatch(token):
            return False
        nonce, signature = token.split(".", 1)
        expected = hmac.new(self._secret, nonce.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if len(request.headers.getlist("host")) != 1:
            return PlainTextResponse("Invalid Host header", status_code=400)
        host = request.headers.get("host", "")
        if _authority(host) is None:
            return PlainTextResponse("Invalid Host header", status_code=400)

        token = request.cookies.get(_COOKIE, "")
        valid_session = self._valid_token(token)
        if request.method not in _SAFE_METHODS:
            origins = request.headers.getlist("origin")
            if len(origins) > 1:
                return PlainTextResponse("Invalid Origin", status_code=403)
            origin = origins[0] if origins else None
            if origin is not None and not _same_origin(origin, host, request.url.scheme):
                return PlainTextResponse("Invalid Origin", status_code=403)
            supplied = request.headers.get("x-csrf-token")
            if supplied is None:
                await request.body()
                form = await request.form()
                supplied = form.get("csrf_token")
            if (
                not valid_session
                or not isinstance(supplied, str)
                or not hmac.compare_digest(supplied.encode("utf-8"), token.encode("ascii"))
            ):
                return PlainTextResponse("Invalid CSRF token", status_code=403)
        elif not valid_session:
            token = self._new_token()

        request.state.csrf_token = token
        response = await call_next(request)
        if not valid_session:
            response.set_cookie(
                _COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https"
            )
        return response
