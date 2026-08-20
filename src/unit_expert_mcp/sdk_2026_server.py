"""Official MCP SDK-backed 2026 baseline server.

This module intentionally does not replace ``unit_expert_mcp.server``. The
existing server is a low-level fault-injection target; this one is a clean
baseline that lets the official MCP Python SDK own the 2026-07-28 transport and
JSON-RPC behavior.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from unit_expert_mcp.server import (
    AREA_FACTORS,
    LENGTH_FACTORS,
    SERVER_NAME,
    SERVER_VERSION,
    SERVICE_NAME,
    SUPPORTED_UNITS,
    VOLUME_FACTORS,
    WEIGHT_FACTORS,
    convert_temperature as convert_temperature_value,
    convert_with_factor,
    format_number,
)


def conversion_text(result: dict[str, Any]) -> str:
    return (
        f"{format_number(result['input_value'])} {result['input_unit']} = "
        f"{format_number(result['output_value'])} {result['output_unit']}"
    )


def security_settings() -> TransportSecuritySettings:
    raw_hosts = os.getenv(
        "MCP_ALLOWED_HOSTS",
        "127.0.0.1,127.0.0.1:*,localhost,localhost:*,"
        "unit-expert-mcp.onrender.com,unit-expert-mcp-2026.onrender.com",
    )
    raw_origins = os.getenv(
        "MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:*,http://localhost:*,"
        "https://unit-expert-mcp.onrender.com,https://unit-expert-mcp-2026.onrender.com",
    )
    return TransportSecuritySettings(
        allowed_hosts=[host.strip() for host in raw_hosts.split(",") if host.strip()],
        allowed_origins=[origin.strip() for origin in raw_origins.split(",") if origin.strip()],
    )


mcp = MCPServer(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Unit Expert baseline MCP server backed by the official Python SDK. "
        "Use this endpoint to verify normal 2026-07-28 protocol behavior; use "
        "unit_expert_mcp.server for intentional failure scenarios."
    ),
)


@mcp.tool()
def convert_length(value: float, from_unit: str, to_unit: str) -> str:
    """Convert length units. Supports mm, cm, m, km, in, ft, yd, and mi."""
    return conversion_text(
        convert_with_factor(
            {"value": value, "from_unit": from_unit, "to_unit": to_unit},
            "length",
            LENGTH_FACTORS,
        )
    )


@mcp.tool()
def convert_weight(value: float, from_unit: str, to_unit: str) -> str:
    """Convert weight units. Supports mg, g, kg, t, oz, and lb."""
    return conversion_text(
        convert_with_factor(
            {"value": value, "from_unit": from_unit, "to_unit": to_unit},
            "weight",
            WEIGHT_FACTORS,
        )
    )


@mcp.tool()
def convert_temperature(value: float, from_unit: str, to_unit: str) -> str:
    """Convert temperature units. Supports c, f, and k."""
    return conversion_text(
        convert_temperature_value(
            {"value": value, "from_unit": from_unit, "to_unit": to_unit}
        )
    )


@mcp.tool()
def convert_area(value: float, from_unit: str, to_unit: str) -> str:
    """Convert area units. Supports mm2, cm2, m2, km2, in2, ft2, yd2, and acre."""
    return conversion_text(
        convert_with_factor(
            {"value": value, "from_unit": from_unit, "to_unit": to_unit},
            "area",
            AREA_FACTORS,
        )
    )


@mcp.tool()
def convert_volume(value: float, from_unit: str, to_unit: str) -> str:
    """Convert volume units. Supports ml, l, m3, in3, ft3, cup, pt, qt, gal, and floz."""
    return conversion_text(
        convert_with_factor(
            {"value": value, "from_unit": from_unit, "to_unit": to_unit},
            "volume",
            VOLUME_FACTORS,
        )
    )


@mcp.tool()
def list_supported_units() -> str:
    """List supported canonical units by conversion category."""
    return "\n".join(
        f"{category}: {', '.join(units)}" for category, units in SUPPORTED_UNITS.items()
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> Response:
    return JSONResponse(
        {
            "ok": True,
            "service": SERVER_NAME,
            "version": SERVER_VERSION,
            "implementation": "official-mcp-python-sdk",
            "protocol": "2026-07-28 baseline",
        }
    )


app = mcp.streamable_http_app(
    json_response=True,
    stateless_http=True,
    transport_security=security_settings(),
)


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
