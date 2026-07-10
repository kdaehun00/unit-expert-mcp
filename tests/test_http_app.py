from __future__ import annotations

import json
from typing import Any

import anyio
from starlette.types import Receive, Scope, Send

from unit_expert_mcp.server import HealthCheckMiddleware, streamable_http_app


def test_health_check_returns_ok_without_reaching_inner_app() -> None:
    async def failing_app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("inner app should not be called")

    async def run() -> tuple[int, dict[str, Any]]:
        app = HealthCheckMiddleware(failing_app)
        messages: list[dict[str, Any]] = []
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/healthz",
            "raw_path": b"/healthz",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)

        response_start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        response_body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        return response_start["status"], json.loads(response_body)

    status, payload = anyio.run(run)

    assert status == 200
    assert payload == {"ok": True, "service": "unit-expert-mcp"}


def test_streamable_http_app_allows_playmcp_cors_preflight() -> None:
    async def run(origin: str) -> tuple[int, dict[str, str]]:
        app = streamable_http_app()
        messages: list[dict[str, Any]] = []
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "OPTIONS",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (b"origin", origin.encode("latin-1")),
                (b"access-control-request-method", b"POST"),
                (
                    b"access-control-request-headers",
                    b"authorization,content-type,mcp-protocol-version,mcp-session-id",
                ),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            messages.append(message)

        await app(scope, receive, send)

        response_start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        response_headers = {
            name.decode("latin-1"): value.decode("latin-1")
            for name, value in response_start["headers"]
        }
        return response_start["status"], response_headers

    for origin in ("https://playmcp.kakao.com", "https://sandbox-playmcp.kakao.com"):
        status, headers = anyio.run(run, origin)

        assert status == 200
        assert headers["access-control-allow-origin"] == origin
        assert "POST" in headers["access-control-allow-methods"]
