from __future__ import annotations

import json
from functools import partial
from typing import Any

import anyio
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from unit_expert_mcp.server import (
    HealthCheckMiddleware,
    TEST_SCENARIO_HEADER,
    HeaderBypassAuthMiddleware,
    RequestDelayMiddleware,
)


async def ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


async def _request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    header_value: str | None = "allow",
) -> tuple[int, dict[str, str], dict[str, Any]]:
    app = HeaderBypassAuthMiddleware(
        ok_app,
        path="/mcp",
        header_name="X-MCP-Mock-Auth",
        header_value=header_value,
    )
    body = json.dumps(json_payload or {}).encode("utf-8")
    messages: list[dict[str, Any]] = []

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    response_start = next(message for message in messages if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in response_start["headers"]
    }
    return response_start["status"], response_headers, json.loads(response_body)


def request(
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_payload: dict[str, Any] | None = None,
    header_value: str | None = "allow",
) -> tuple[int, dict[str, str], dict[str, Any]]:
    return anyio.run(
        partial(
            _request,
            method,
            path,
            headers=headers,
            json_payload=json_payload,
            header_value=header_value,
        )
    )


def test_mock_auth_rejects_mcp_requests_without_bypass_header() -> None:
    status, headers, payload = request(
        "POST",
        "/mcp",
        json_payload={"jsonrpc": "2.0"},
    )

    assert status == 401
    assert payload == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert headers["www-authenticate"].startswith("Bearer")


def test_mock_auth_rejects_mcp_requests_with_wrong_header_name_as_unauthorized() -> None:
    status, headers, payload = request(
        "POST",
        "/mcp",
        headers={"X-Wrong-Mock-Auth": "allow"},
        json_payload={"jsonrpc": "2.0"},
    )

    assert status == 401
    assert payload == {
        "error": "invalid_token",
        "error_description": "Authentication required",
    }
    assert headers["www-authenticate"].startswith("Bearer")


def test_mock_auth_allows_mcp_requests_with_matching_bypass_header() -> None:
    status, _, payload = request(
        "POST",
        "/mcp",
        headers={"X-MCP-Mock-Auth": "allow"},
        json_payload={"jsonrpc": "2.0"},
    )

    assert status == 200
    assert payload == {"ok": True}


def test_mock_auth_rejects_mcp_requests_with_wrong_bypass_header_value_as_forbidden() -> None:
    status, _, payload = request(
        "POST",
        "/mcp",
        headers={"X-MCP-Mock-Auth": "deny"},
        json_payload={"jsonrpc": "2.0"},
    )

    assert status == 403
    assert payload == {
        "error": "forbidden",
        "error_description": "Mock auth header value is invalid",
    }


def test_mock_auth_can_accept_any_non_empty_header_value() -> None:
    status, _, _ = request(
        "POST",
        "/mcp",
        headers={"X-MCP-Mock-Auth": "local-dev"},
        json_payload={"jsonrpc": "2.0"},
        header_value=None,
    )

    assert status == 200


def test_mock_auth_does_not_block_unrelated_paths() -> None:
    status, _, payload = request("GET", "/health")

    assert status == 200
    assert payload == {"ok": True}


def test_mock_auth_can_inject_unauthorized_from_scenario_header() -> None:
    status, _, payload = request(
        "POST",
        "/mcp",
        headers={
            "X-MCP-Mock-Auth": "allow",
            TEST_SCENARIO_HEADER: "auth-401",
        },
    )

    assert status == 401
    assert payload["error"] == "invalid_token"


def test_mock_auth_can_inject_forbidden_from_scenario_header() -> None:
    status, _, payload = request(
        "POST",
        "/mcp",
        headers={
            "X-MCP-Mock-Auth": "allow",
            TEST_SCENARIO_HEADER: "auth-403",
        },
    )

    assert status == 403
    assert payload["error"] == "forbidden"


def test_request_delay_applies_to_mcp_requests_only() -> None:
    middleware = RequestDelayMiddleware(ok_app, path="/mcp", delay_seconds=5)

    assert middleware._requires_delay({"type": "http", "path": "/mcp", "method": "POST"})
    assert middleware._requires_delay({"type": "http", "path": "/mcp", "method": "GET"})
    assert middleware._requires_delay({"type": "http", "path": "/mcp", "method": "DELETE"})
    assert not middleware._requires_delay({"type": "http", "path": "/mcp", "method": "OPTIONS"})
    assert not middleware._requires_delay({"type": "http", "path": "/health", "method": "GET"})
    assert not middleware._requires_delay({"type": "websocket", "path": "/mcp"})


def test_request_delay_can_be_enabled_by_scenario_header() -> None:
    middleware = RequestDelayMiddleware(ok_app, path="/mcp", delay_seconds=0)

    assert middleware._requires_delay(
        {
            "type": "http",
            "path": "/mcp",
            "method": "POST",
            "headers": [(TEST_SCENARIO_HEADER.lower().encode("latin-1"), b"delayed-response")],
        }
    )


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
