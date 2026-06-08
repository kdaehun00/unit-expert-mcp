"""MCP tool registration for Unit Expert."""

from __future__ import annotations

import argparse
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP

from unit_expert_mcp.converter import (
    convert_area as convert_area_value,
    convert_length as convert_length_value,
    convert_temperature as convert_temperature_value,
    convert_volume as convert_volume_value,
    convert_weight as convert_weight_value,
    list_supported_units as list_supported_units_value,
)

Transport = Literal["stdio", "sse", "streamable-http"]


def _default_port() -> int:
    raw_port = os.getenv("PORT") or os.getenv("MCP_PORT") or "8000"
    return int(raw_port)


mcp = FastMCP(
    "Unit Expert MCP",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=_default_port(),
    stateless_http=True,
    json_response=True,
)


@mcp.tool()
def convert_length(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert length between mm, cm, m, km, in, ft, yd, and mi."""
    return convert_length_value(value, from_unit, to_unit)


@mcp.tool()
def convert_weight(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert weight between mg, g, kg, t, oz, and lb."""
    return convert_weight_value(value, from_unit, to_unit)


@mcp.tool()
def convert_temperature(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert temperature between c, f, and k."""
    return convert_temperature_value(value, from_unit, to_unit)


@mcp.tool()
def convert_area(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert area between mm2, cm2, m2, km2, in2, ft2, yd2, and acre."""
    return convert_area_value(value, from_unit, to_unit)


@mcp.tool()
def convert_volume(value: float, from_unit: str, to_unit: str) -> dict[str, float | str]:
    """Convert volume between ml, l, m3, in3, ft3, cup, pt, qt, gal, and floz."""
    return convert_volume_value(value, from_unit, to_unit)


@mcp.tool()
def list_supported_units() -> dict[str, tuple[str, ...]]:
    """List supported canonical units by conversion category."""
    return list_supported_units_value()


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
    args = parser.parse_args()

    if args.host:
        mcp.settings.host = args.host
    if args.port is not None:
        mcp.settings.port = args.port

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
