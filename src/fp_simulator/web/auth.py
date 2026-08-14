"""Web and MCP authentication helpers."""

from __future__ import annotations

import os

from starlette.types import ASGIApp, Receive, Scope, Send


def iap_auth_required() -> bool:
    """Return whether the web app must require an IAP identity."""
    return os.environ.get("FP_REQUIRE_IAP_AUTH", "").lower() in {"1", "true", "yes"}


def authenticated_email(headers: dict[str, str]) -> str | None:
    """Extract the email from the IAP authenticated-user header."""
    value = headers.get("x-goog-authenticated-user-email")
    if not value:
        return None
    return value.split(":", 1)[-1].strip().lower() or None


def mcp_api_key() -> str | None:
    """Return the configured MCP API key, if any."""
    value = os.environ.get("FP_MCP_API_KEY", "").strip()
    return value or None


class McpAuthMiddleware:
    """Protect the mounted MCP ASGI application with an API key."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        configured_key = mcp_api_key()
        authorization = headers.get("authorization", "")
        supplied_key = headers.get("x-api-key")
        if authorization.lower().startswith("bearer "):
            supplied_key = authorization[7:].strip()

        if configured_key is None:
            await self._respond(send, 503, b"MCP authentication is not configured")
            return
        if supplied_key != configured_key:
            await self._respond(send, 401, b"Unauthorized")
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(send: Send, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
