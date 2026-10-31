"""
FRIDAY API authentication (Phase 13D): a shared secret on every state-changing request.

Why this exists, beyond "the API is open". The service is meant to listen on 127.0.0.1 only, and
that is good advice, but it does not make these routes safe. A browser will happily send a request
to http://127.0.0.1:8000 from ANY page the owner has open: a page on the open internet can POST to
`/api/hermes/kill`, `/api/risk/resume` or `/api/directives/resume` — which restarts a platform that
TERMINATOR deliberately shut down — and the request arrives from the loopback address looking
perfectly local. There is therefore no "it came from localhost, so it is trusted" exemption here.

What is protected: every POST, PUT, PATCH and DELETE. Reads stay open so the dashboard keeps working
without holding a secret in browser code.

How to authenticate: either header.
    Authorization: Bearer <token>
    X-JARVIS-Token: <token>

If JARVIS_API_TOKEN is unset the API behaves exactly as before and every reply to a write carries
`X-JARVIS-Auth: disabled`, with the reason reported by /health. That default keeps an existing
install running; it is not a recommendation.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Request
from fastapi.responses import JSONResponse

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MIN_TOKEN_LENGTH = 16


def configured_token() -> str | None:
    token = os.getenv("JARVIS_API_TOKEN", "").strip()
    return token or None


def auth_status() -> dict[str, object]:
    token = configured_token()
    if token is None:
        return {"enabled": False, "protects": "nothing",
                "reason": ("JARVIS_API_TOKEN is not set: any page in the owner's browser can POST to this "
                           "API, including the kill switch and the directive resume")}
    if len(token) < MIN_TOKEN_LENGTH:
        return {"enabled": True, "protects": "writes", "weak": True,
                "reason": f"JARVIS_API_TOKEN is shorter than {MIN_TOKEN_LENGTH} characters"}
    return {"enabled": True, "protects": "writes"}


def presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return (request.headers.get("x-jarvis-token") or "").strip() or None


async def require_token(request: Request, call_next):
    """Middleware: a write needs the shared secret. Reads are untouched."""
    if request.method not in WRITE_METHODS:
        return await call_next(request)
    expected = configured_token()
    if expected is None:
        response = await call_next(request)
        response.headers["X-JARVIS-Auth"] = "disabled"
        return response
    given = presented_token(request)
    if given is None or not hmac.compare_digest(given, expected):     # constant time: no length leak
        return JSONResponse(
            status_code=401,
            content={"detail": "this endpoint changes state and needs the API token",
                     "how": "send Authorization: Bearer <JARVIS_API_TOKEN> or X-JARVIS-Token: <token>"},
            headers={"WWW-Authenticate": "Bearer", "X-JARVIS-Auth": "required"})
    response = await call_next(request)
    response.headers["X-JARVIS-Auth"] = "ok"
    return response
