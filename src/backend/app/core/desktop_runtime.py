"""Short-lived admission barrier used while Desktop replaces its Python runtime."""

import time
from contextvars import ContextVar

from starlette.responses import JSONResponse

from app.core.config import get_settings

active_requests = 0
inside_request: ContextVar[bool] = ContextVar("desktop_admitted_request", default=False)
_paused_until = 0.0


def paused() -> bool:
    return get_settings().is_desktop and time.monotonic() < _paused_until


def set_paused(value: bool) -> None:
    global _paused_until
    _paused_until = time.monotonic() + 30 if value else 0


class DesktopRuntimeBarrier:
    """Count streaming responses until completion, not merely response-header dispatch."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global active_requests
        path = scope.get("path", "")
        control = path.startswith("/api/desktop-runtime/") or path in {
            "/api/health",
            "/api/auth/local-session",
        }
        # Passive subscriptions perform no work and may stay open indefinitely.
        control = control or (scope.get("method") == "GET" and path.endswith("/events"))
        if not get_settings().is_desktop or control or scope["type"] != "http":
            return await self.app(scope, receive, send)
        if paused():
            return await JSONResponse(
                {"detail": "DESKTOP_RUNTIME_SWITCH_PENDING"},
                status_code=503,
                headers={"Retry-After": "2"},
            )(scope, receive, send)
        active_requests += 1
        token = inside_request.set(True)
        try:
            await self.app(scope, receive, send)
        finally:
            inside_request.reset(token)
            active_requests -= 1
