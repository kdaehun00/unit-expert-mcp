"""MCP tool registration for Unit Expert."""

from __future__ import annotations

import argparse
import os
from collections.abc import Iterable
from typing import Literal

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

Transport = Literal["stdio", "streamable-http"]

SERVICE_NAME = "Unit Expert MCP(유닛 익스퍼트 MCP)"
SERVER_NAME = "unit-expert-mcp"
HEALTH_PATH = "/healthz"
DEFAULT_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
SDK_SUPPORTED_PROTOCOL_VERSIONS = tuple(mcp_version.SUPPORTED_PROTOCOL_VERSIONS)

PLAYMCP_ORIGINS = (
    "https://playmcp.kakao.com",
    "https://sandbox-playmcp.kakao.com",
    "https://developers.kakao.com",
)
ALLOWED_HOSTS = (
    "unit-expert-mcp.onrender.com",
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
)


def _default_port() -> int:
    raw_port = os.getenv("PORT") or os.getenv("MCP_PORT") or "8000"
    return int(raw_port)


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
            f"unsupported protocol version: {unknown}. Known versions: {supported_versions}"
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


def _tool_annotations(title: str) -> mcp_types.ToolAnnotations:
    return mcp_types.ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


class HealthCheckMiddleware:
    """Return a lightweight health response for deployment probes."""

    def __init__(self, app: ASGIApp, *, path: str = HEALTH_PATH) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._is_health_request(scope):
            response = JSONResponse({"ok": True, "service": SERVER_NAME})
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_health_request(self, scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("path") == self.path
            and scope.get("method") == "GET"
        )


mcp = FastMCP(
    SERVER_NAME,
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=_default_port(),
    stateless_http=False,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(ALLOWED_HOSTS),
        allowed_origins=list(PLAYMCP_ORIGINS),
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


def streamable_http_app() -> ASGIApp:
    """Build the public Streamable HTTP app."""
    app: ASGIApp = mcp.streamable_http_app()
    app = HealthCheckMiddleware(app)
    return CORSMiddleware(
        app,
        allow_origins=list(PLAYMCP_ORIGINS),
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
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
        choices=("stdio", "streamable-http"),
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
        help="Port for HTTP transport. Defaults to PORT, MCP_PORT, or 8000.",
    )
    parser.add_argument(
        "--protocol-versions",
        default=os.getenv("MCP_PROTOCOL_VERSIONS", ",".join(DEFAULT_PROTOCOL_VERSIONS)),
        help=(
            "Comma-separated MCP protocol versions this server supports during initialize. "
            f"Defaults to {','.join(DEFAULT_PROTOCOL_VERSIONS)}."
        ),
    )
    args = parser.parse_args()

    try:
        protocol_versions = _parse_protocol_versions(args.protocol_versions)
        configure_supported_protocol_versions(protocol_versions)
    except ValueError as error:
        parser.error(str(error))

    if args.host:
        mcp.settings.host = args.host
    if args.port is not None:
        mcp.settings.port = args.port

    if args.transport == "streamable-http":
        _run_streamable_http_app(streamable_http_app())
        return

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
