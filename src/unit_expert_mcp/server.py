"""MCP tool registration for Unit Expert."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable
from typing import Literal

import anyio
import mcp.shared.version as mcp_version
import mcp.types as mcp_types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from unit_expert_mcp.converter import (
    convert_area as convert_area_value,
    convert_length as convert_length_value,
    convert_temperature as convert_temperature_value,
    convert_volume as convert_volume_value,
    convert_weight as convert_weight_value,
    list_supported_units as list_supported_units_value,
)

Transport = Literal["stdio", "sse", "streamable-http"]
DEFAULT_MOCK_AUTH_HEADER = "X-MCP-Mock-Auth"
DEFAULT_MOCK_AUTH_HEADER_VALUE = "allow"
DEFAULT_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"
HEALTH_PATH = "/healthz"
DEFAULT_DELAY_SECONDS = 5.0
CORS_ALLOWED_ORIGINS = (
    "https://playmcp.kakao.com",
    "https://sandbox-playmcp.kakao.com",
    "https://developers.kakao.com",
)
CORS_ALLOWED_HOSTS = (
    "unit-expert-mcp.onrender.com",
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
)
SDK_SUPPORTED_PROTOCOL_VERSIONS = tuple(mcp_version.SUPPORTED_PROTOCOL_VERSIONS)
_list_tools_handler: object | None = None
_default_tools_list_scenario = "ok"
SERVICE_NAME = "Unit Expert MCP(유닛 익스퍼트 MCP)"


def _tool_annotations(title: str) -> mcp_types.ToolAnnotations:
    return mcp_types.ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _normalize_scenario(raw_scenario: str | None) -> str:
    if not raw_scenario:
        return "ok"
    return raw_scenario.strip().lower() or "ok"


def _scenario_from_scope(scope: Scope) -> str:
    header_name = TEST_SCENARIO_HEADER.lower().encode("latin-1")
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == header_name:
            return _normalize_scenario(raw_value.decode("latin-1"))
    return "ok"


def _scenario_from_request_context(default: str = "ok") -> str:
    try:
        request = mcp._mcp_server.request_context.request
    except LookupError:
        return default

    headers = getattr(request, "headers", None)
    if headers is None:
        return default

    return _normalize_scenario(headers.get(TEST_SCENARIO_HEADER, default))


class RawServerResult:
    """Test-only result wrapper for intentionally malformed JSON-RPC results."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(
        self,
        *,
        by_alias: bool = True,
        mode: str = "json",
        exclude_none: bool = True,
    ) -> dict[str, object]:
        return self.payload


class HealthCheckMiddleware:
    """Return a lightweight health response for HTTP deployment probes."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str = HEALTH_PATH,
    ) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._is_health_request(scope):
            response = JSONResponse({"ok": True, "service": "unit-expert-mcp"})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_health_request(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("path") == self.path
            and scope.get("method") == "GET"
        )


async def _null_tools_list_handler(_request: mcp_types.ListToolsRequest) -> RawServerResult:
    return RawServerResult({"tools": None})


async def _failing_tools_list_handler(_request: mcp_types.ListToolsRequest) -> mcp_types.ErrorData:
    return mcp_types.ErrorData(
        code=mcp_types.INTERNAL_ERROR,
        message="Injected tools/list failure",
    )


def _valid_tool(name: str, description: str | None = None) -> dict[str, object]:
    return {
        "name": name,
        "description": description or "Unit Expert MCP converts common measurement units.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
            },
            "required": ["value"],
        },
        "annotations": {
            "title": name.replace("_", " ").title(),
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


def _tools_for_scenario(scenario: str) -> RawServerResult | mcp_types.ErrorData | None:
    match scenario:
        case "tools-list-error":
            return mcp_types.ErrorData(
                code=mcp_types.INTERNAL_ERROR,
                message="Injected tools/list failure",
            )
        case "tools-list-null":
            return RawServerResult({"tools": None})
        case "tools-list-empty":
            return RawServerResult({"tools": []})
        case "valid-tools":
            return RawServerResult({"tools": [_valid_tool("convert_length")]})
        case "duplicate-tool-name":
            return RawServerResult(
                {"tools": [_valid_tool("search_place"), _valid_tool("search_place")]}
            )
        case "too-many-tools":
            return RawServerResult({"tools": [_valid_tool(f"tool_{index}") for index in range(21)]})
        case "invalid-tool-name-char":
            return RawServerResult({"tools": [_valid_tool("search place!")]})
        case "invalid-tool-name-length":
            return RawServerResult({"tools": [_valid_tool("a" * 111)]})
        case "missing-name":
            tool = _valid_tool("search_place")
            tool.pop("name")
            return RawServerResult({"tools": [tool]})
        case "missing-description":
            tool = _valid_tool("search_place")
            tool.pop("description")
            return RawServerResult({"tools": [tool]})
        case "missing-input-schema":
            tool = _valid_tool("search_place")
            tool.pop("inputSchema")
            return RawServerResult({"tools": [tool]})
        case "missing-annotations":
            tool = _valid_tool("search_place")
            tool.pop("annotations")
            return RawServerResult({"tools": [tool]})
        case "forbidden-kakao-name":
            return RawServerResult({"tools": [_valid_tool("kakao_search")]})
        case "mcp-identifier-name":
            return RawServerResult({"tools": [_valid_tool("kakaomap_search")]})
        case "long-description":
            return RawServerResult({"tools": [_valid_tool("search_place", "a" * 1051)]})
        case "missing-service-name-in-description":
            return RawServerResult({"tools": [_valid_tool("search_place", "Search places nearby.")]})
        case "incomplete-annotations":
            tool = _valid_tool("search_place")
            tool["annotations"] = {
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            return RawServerResult({"tools": [tool]})
        case _:
            return None


async def _scenario_tools_list_handler(_request: mcp_types.ListToolsRequest) -> RawServerResult | mcp_types.ErrorData:
    scenario = _scenario_from_request_context(_default_tools_list_scenario)
    scenario_result = _tools_for_scenario(scenario)
    if scenario_result is not None:
        return scenario_result

    tools = [
        tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        for tool in await mcp.list_tools()
    ]
    return RawServerResult({"tools": tools})


def _default_port() -> int:
    raw_port = os.getenv("PORT") or os.getenv("MCP_PORT") or "8000"
    return int(raw_port)


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float = 0.0) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    return float(raw_value)


def _parse_protocol_versions(raw_versions: str) -> tuple[str, ...]:
    protocol_versions = tuple(
        protocol_version.strip()
        for protocol_version in raw_versions.split(",")
        if protocol_version.strip()
    )
    if not protocol_versions:
        raise ValueError("at least one protocol version must be configured")

    return protocol_versions


def _supported_protocol_versions_for(protocol_versions: Iterable[str]) -> list[str]:
    requested_versions = tuple(protocol_versions)
    unknown_versions = [
        protocol_version
        for protocol_version in requested_versions
        if protocol_version not in SDK_SUPPORTED_PROTOCOL_VERSIONS
    ]
    if unknown_versions:
        supported_versions = ", ".join(SDK_SUPPORTED_PROTOCOL_VERSIONS)
        unknown = ", ".join(unknown_versions)
        raise ValueError(
            f"unsupported protocol version: {unknown}. "
            f"Known versions: {supported_versions}"
        )

    ordered_versions = [
        protocol_version
        for protocol_version in SDK_SUPPORTED_PROTOCOL_VERSIONS
        if protocol_version in requested_versions
    ]
    if not ordered_versions:
        raise ValueError("at least one protocol version must be configured")

    return ordered_versions


def configure_supported_protocol_versions(protocol_versions: Iterable[str]) -> tuple[str, ...]:
    """Limit MCP version negotiation to configured supported protocol versions."""
    supported_versions = _supported_protocol_versions_for(protocol_versions)

    mcp_version.SUPPORTED_PROTOCOL_VERSIONS[:] = supported_versions
    mcp_types.LATEST_PROTOCOL_VERSION = supported_versions[-1]

    return tuple(supported_versions)


def configure_tools_capability(enabled: bool) -> None:
    """Enable or hide the server tools capability for client behavior tests."""
    global _list_tools_handler

    request_handlers = mcp._mcp_server.request_handlers
    if enabled:
        if mcp_types.ListToolsRequest not in request_handlers and _list_tools_handler is not None:
            request_handlers[mcp_types.ListToolsRequest] = _list_tools_handler
        return

    handler = request_handlers.pop(mcp_types.ListToolsRequest, None)
    if handler is not None and handler not in {
        _null_tools_list_handler,
        _failing_tools_list_handler,
        _scenario_tools_list_handler,
    }:
        _list_tools_handler = handler


def configure_null_tools_list_result(enabled: bool) -> None:
    """Return {"tools": null} from tools/list for client deserialization tests."""
    global _list_tools_handler

    request_handlers = mcp._mcp_server.request_handlers
    if enabled:
        handler = request_handlers.get(mcp_types.ListToolsRequest)
        if handler is not None and handler not in {
            _null_tools_list_handler,
            _failing_tools_list_handler,
            _scenario_tools_list_handler,
        }:
            _list_tools_handler = handler
        request_handlers[mcp_types.ListToolsRequest] = _null_tools_list_handler
        return

    if request_handlers.get(mcp_types.ListToolsRequest) is _null_tools_list_handler:
        if _list_tools_handler is None:
            request_handlers.pop(mcp_types.ListToolsRequest, None)
            return
        request_handlers[mcp_types.ListToolsRequest] = _list_tools_handler


def configure_failing_tools_list_result(enabled: bool) -> None:
    """Return a JSON-RPC error from tools/list for client failure-path tests."""
    global _list_tools_handler

    request_handlers = mcp._mcp_server.request_handlers
    if enabled:
        handler = request_handlers.get(mcp_types.ListToolsRequest)
        if handler is not None and handler not in {
            _null_tools_list_handler,
            _failing_tools_list_handler,
            _scenario_tools_list_handler,
        }:
            _list_tools_handler = handler
        request_handlers[mcp_types.ListToolsRequest] = _failing_tools_list_handler
        return

    if request_handlers.get(mcp_types.ListToolsRequest) is _failing_tools_list_handler:
        if _list_tools_handler is None:
            request_handlers.pop(mcp_types.ListToolsRequest, None)
            return
        request_handlers[mcp_types.ListToolsRequest] = _list_tools_handler


def configure_scenario_tools_list_result(default_scenario: str = "ok") -> None:
    """Route tools/list responses by X-MCP-Test-Scenario header."""
    global _default_tools_list_scenario, _list_tools_handler

    _default_tools_list_scenario = _normalize_scenario(default_scenario)
    request_handlers = mcp._mcp_server.request_handlers
    handler = request_handlers.get(mcp_types.ListToolsRequest)
    if handler is not None and handler not in {
        _null_tools_list_handler,
        _failing_tools_list_handler,
        _scenario_tools_list_handler,
    }:
        _list_tools_handler = handler
    request_handlers[mcp_types.ListToolsRequest] = _scenario_tools_list_handler


class RequestDelayMiddleware:
    """Delay configured HTTP requests before they reach the MCP JSON-RPC app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        delay_seconds: float,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("request delay seconds must be greater than or equal to 0")

        self.app = app
        self.path = path
        self.delay_seconds = delay_seconds

    def _requires_delay(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("path") == self.path
            and scope.get("method") != "OPTIONS"
            and (
                self.delay_seconds > 0
                or _scenario_from_scope(scope) in {"delayed-response", "request-timeout"}
            )
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._requires_delay(scope):
            delay_seconds = (
                self.delay_seconds
                if self.delay_seconds > 0
                else DEFAULT_DELAY_SECONDS
            )
            await anyio.sleep(delay_seconds)

        await self.app(scope, receive, send)


class ScenarioResponseMiddleware:
    """Rewrite selected JSON-RPC responses for header-driven validation scenarios."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
    ) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scenario = _scenario_from_scope(scope)
        if not self._requires_rewrite(scope, scenario):
            await self.app(scope, receive, send)
            return

        response_start: dict[str, object] | None = None
        body_parts: list[bytes] = []

        async def capture_send(message: dict[str, object]) -> None:
            nonlocal response_start
            if message["type"] == "http.response.start":
                response_start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return

            body_parts.append(message.get("body", b""))
            if message.get("more_body", False):
                return

            body = self._rewrite_body(scenario, b"".join(body_parts))
            start = self._response_start_with_length(response_start, len(body))
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture_send)

    def _requires_rewrite(self, scope: Scope, scenario: str) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("path") == self.path
            and scope.get("method") == "POST"
            and scenario in {"no-tools-capability"}
        )

    def _rewrite_body(self, scenario: str, body: bytes) -> bytes:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return body

        if scenario == "no-tools-capability":
            capabilities = payload.get("result", {}).get("capabilities")
            if isinstance(capabilities, dict):
                capabilities.pop("tools", None)

        return json.dumps(payload, separators=(",", ":")).encode("utf-8")

    def _response_start_with_length(
        self,
        response_start: dict[str, object] | None,
        content_length: int,
    ) -> dict[str, object]:
        if response_start is None:
            return {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-length", str(content_length).encode("ascii"))],
            }

        headers = [
            (name, value)
            for name, value in response_start.get("headers", [])
            if name.lower() != b"content-length"
        ]
        headers.append((b"content-length", str(content_length).encode("ascii")))
        return {**response_start, "headers": headers}


class HeaderBypassAuthMiddleware:
    """Require a configured header before requests reach the MCP JSON-RPC app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        path: str,
        header_name: str,
        header_value: str | None,
    ) -> None:
        normalized_header_name = header_name.strip().lower()
        if not normalized_header_name:
            raise ValueError("mock auth header name must be a non-empty string")

        self.app = app
        self.path = path
        self.header_name = normalized_header_name.encode("latin-1")
        self.header_value = header_value

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._requires_auth(scope):
            scenario = _scenario_from_scope(scope)
            if scenario == "auth-401":
                await self._send_auth_error(send)
                return
            if scenario == "auth-403":
                await self._send_forbidden_error(send)
                return

            auth_result = self._check_bypass_header(scope)
            if auth_result == "missing":
                await self._send_auth_error(send)
                return
            if auth_result == "forbidden":
                await self._send_forbidden_error(send)
                return

        await self.app(scope, receive, send)

    def _requires_auth(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("path") == self.path
            and scope.get("method") != "OPTIONS"
        )

    def _check_bypass_header(self, scope: Scope) -> Literal["allowed", "forbidden", "missing"]:
        headers = scope.get("headers", [])
        for raw_name, raw_value in headers:
            if raw_name.lower() != self.header_name:
                continue

            value = raw_value.decode("latin-1").strip()
            if self.header_value is None:
                return "allowed" if value else "forbidden"
            return "allowed" if value == self.header_value else "forbidden"

        return "missing"

    async def _send_auth_error(self, send: Send) -> None:
        body = {
            "error": "invalid_token",
            "error_description": "Authentication required",
        }
        body_bytes = json.dumps(body).encode("utf-8")
        www_authenticate = (
            'Bearer error="invalid_token", error_description="Authentication required"'
        )

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode("ascii")),
                    (b"www-authenticate", www_authenticate.encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_bytes})

    async def _send_forbidden_error(self, send: Send) -> None:
        body = {
            "error": "forbidden",
            "error_description": "Mock auth header value is invalid",
        }
        body_bytes = json.dumps(body).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body_bytes)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body_bytes})


mcp = FastMCP(
    "Unit Expert MCP",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=_default_port(),
    stateless_http=False,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(CORS_ALLOWED_HOSTS),
        allowed_origins=list(CORS_ALLOWED_ORIGINS),
    ),
)


@mcp.tool(
    title="Convert Length",
    description=(
        f"{SERVICE_NAME} converts length values between mm, cm, m, km, in, ft, yd, and mi."
    ),
    annotations=_tool_annotations("Convert Length"),
)
def convert_length(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert length between mm, cm, m, km, in, ft, yd, and mi."""
    return convert_length_value(value, from_unit, to_unit)


@mcp.tool(
    title="Convert Weight",
    description=f"{SERVICE_NAME} converts weight values between mg, g, kg, t, oz, and lb.",
    annotations=_tool_annotations("Convert Weight"),
)
def convert_weight(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert weight between mg, g, kg, t, oz, and lb."""
    return convert_weight_value(value, from_unit, to_unit)


@mcp.tool(
    title="Convert Temperature",
    description=f"{SERVICE_NAME} converts temperature values between c, f, and k.",
    annotations=_tool_annotations("Convert Temperature"),
)
def convert_temperature(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert temperature between c, f, and k."""
    return convert_temperature_value(value, from_unit, to_unit)


@mcp.tool(
    title="Convert Area",
    description=(
        f"{SERVICE_NAME} converts area values between mm2, cm2, m2, km2, in2, ft2, yd2, "
        "and acre."
    ),
    annotations=_tool_annotations("Convert Area"),
)
def convert_area(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert area between mm2, cm2, m2, km2, in2, ft2, yd2, and acre."""
    return convert_area_value(value, from_unit, to_unit)


@mcp.tool(
    title="Convert Volume",
    description=(
        f"{SERVICE_NAME} converts volume values between ml, l, m3, in3, ft3, cup, pt, qt, "
        "gal, and floz."
    ),
    annotations=_tool_annotations("Convert Volume"),
)
def convert_volume(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert volume between ml, l, m3, in3, ft3, cup, pt, qt, gal, and floz."""
    return convert_volume_value(value, from_unit, to_unit)


@mcp.tool(
    title="List Supported Units",
    description=f"{SERVICE_NAME} lists supported canonical units by conversion category.",
    annotations=_tool_annotations("List Supported Units"),
)
def list_supported_units() -> dict[str, tuple[str, ...]]:
    """List supported canonical units by conversion category."""
    return list_supported_units_value()


def streamable_http_app_with_optional_mock_auth(
    *,
    mock_auth_required: bool,
    header_name: str = DEFAULT_MOCK_AUTH_HEADER,
    header_value: str | None = DEFAULT_MOCK_AUTH_HEADER_VALUE,
    request_delay_seconds: float = 0.0,
) -> ASGIApp:
    """Build the Streamable HTTP app, optionally protected by mock header auth."""
    app: ASGIApp = mcp.streamable_http_app()
    app = HealthCheckMiddleware(app)
    app = ScenarioResponseMiddleware(
        app,
        path=mcp.settings.streamable_http_path,
    )
    app = RequestDelayMiddleware(
        app,
        path=mcp.settings.streamable_http_path,
        delay_seconds=request_delay_seconds,
    )
    app = CORSMiddleware(
        app,
        allow_origins=list(CORS_ALLOWED_ORIGINS),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )
    if not mock_auth_required:
        return app

    return HeaderBypassAuthMiddleware(
        app,
        path=mcp.settings.streamable_http_path,
        header_name=header_name,
        header_value=header_value,
    )


def _run_streamable_http_app(app: ASGIApp) -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


def main() -> None:
    """Run the MCP server."""
    parser = argparse.ArgumentParser(description="Run the Unit Expert MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="MCP transport to use. Defaults to MCP_TRANSPORT or stdio.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST"),
        help="Host for HTTP transports. Defaults to MCP_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for HTTP transports. Defaults to PORT, MCP_PORT, or 8000.",
    )
    parser.add_argument(
        "--mock-auth",
        action="store_true",
        default=_env_flag("MCP_MOCK_AUTH"),
        help=(
            "Require a custom header before /mcp requests can reach JSON-RPC. "
            "Only supported with streamable-http."
        ),
    )
    parser.add_argument(
        "--mock-auth-header",
        default=os.getenv("MCP_MOCK_AUTH_HEADER", DEFAULT_MOCK_AUTH_HEADER),
        help=f"Header name used by --mock-auth. Defaults to {DEFAULT_MOCK_AUTH_HEADER}.",
    )
    parser.add_argument(
        "--mock-auth-header-value",
        default=os.getenv("MCP_MOCK_AUTH_HEADER_VALUE", DEFAULT_MOCK_AUTH_HEADER_VALUE),
        help=(
            "Header value used by --mock-auth. Defaults to "
            f"{DEFAULT_MOCK_AUTH_HEADER_VALUE}. Use an empty string to accept any "
            "non-empty value."
        ),
    )
    parser.add_argument(
        "--protocol-versions",
        default=os.getenv("MCP_PROTOCOL_VERSIONS", ",".join(DEFAULT_PROTOCOL_VERSIONS)),
        help=(
            "Comma-separated MCP protocol versions this server supports during initialize. "
            f"Defaults to {','.join(DEFAULT_PROTOCOL_VERSIONS)}."
        ),
    )
    parser.add_argument(
        "--protocol-version",
        action="append",
        default=None,
        help=(
            "MCP protocol version this server supports during initialize. "
            "Can be provided more than once. Overrides --protocol-versions."
        ),
    )
    parser.add_argument(
        "--disable-tools-capability",
        action="store_true",
        default=_env_flag("MCP_DISABLE_TOOLS_CAPABILITY"),
        help=(
            "Hide tools capability from initialize responses. Useful for testing "
            "clients that call listTools() without checking capabilities."
        ),
    )
    parser.add_argument(
        "--null-tools-list",
        action="store_true",
        default=_env_flag("MCP_NULL_TOOLS_LIST"),
        help=(
            "Advertise tools support, but return {'tools': null} from tools/list. "
            "Useful for client deserialization tests."
        ),
    )
    parser.add_argument(
        "--fail-tools-list",
        action="store_true",
        default=_env_flag("MCP_FAIL_TOOLS_LIST"),
        help=(
            "Advertise tools support, but return a JSON-RPC error from tools/list. "
            "Useful for client failure-path tests."
        ),
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=float,
        default=_env_float("MCP_REQUEST_DELAY_SECONDS"),
        help=(
            "Delay each non-OPTIONS /mcp request before JSON-RPC handling. "
            "Useful for request timeout tests. Defaults to MCP_REQUEST_DELAY_SECONDS or 0."
        ),
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        default=_env_flag("MCP_STATELESS_HTTP"),
        help=(
            "Do not issue or require mcp-session-id headers for Streamable HTTP. "
            "By default the HTTP transport is stateful and returns mcp-session-id."
        ),
    )
    args = parser.parse_args()

    if args.mock_auth and args.transport != "streamable-http":
        parser.error("--mock-auth is only supported with --transport streamable-http")
    if args.request_delay_seconds > 0 and args.transport != "streamable-http":
        parser.error("--request-delay-seconds is only supported with --transport streamable-http")
    if args.disable_tools_capability and args.null_tools_list:
        parser.error("--null-tools-list cannot be combined with --disable-tools-capability")
    if args.disable_tools_capability and args.fail_tools_list:
        parser.error("--fail-tools-list cannot be combined with --disable-tools-capability")
    if args.null_tools_list and args.fail_tools_list:
        parser.error("--null-tools-list cannot be combined with --fail-tools-list")
    if args.request_delay_seconds < 0:
        parser.error("--request-delay-seconds must be greater than or equal to 0")

    try:
        protocol_versions = (
            tuple(args.protocol_version)
            if args.protocol_version
            else _parse_protocol_versions(args.protocol_versions)
        )
        configure_supported_protocol_versions(protocol_versions)
    except ValueError as error:
        parser.error(str(error))

    configure_tools_capability(not args.disable_tools_capability)
    if not args.disable_tools_capability:
        default_tools_list_scenario = "ok"
        if args.null_tools_list:
            default_tools_list_scenario = "tools-list-null"
        if args.fail_tools_list:
            default_tools_list_scenario = "tools-list-error"
        configure_scenario_tools_list_result(default_tools_list_scenario)
    mcp.settings.stateless_http = args.stateless_http

    if args.host:
        mcp.settings.host = args.host
    if args.port is not None:
        mcp.settings.port = args.port

    if args.transport == "streamable-http":
        header_value = args.mock_auth_header_value or None
        app = streamable_http_app_with_optional_mock_auth(
            mock_auth_required=args.mock_auth,
            header_name=args.mock_auth_header,
            header_value=header_value,
            request_delay_seconds=args.request_delay_seconds,
        )
        _run_streamable_http_app(app)
        return

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
