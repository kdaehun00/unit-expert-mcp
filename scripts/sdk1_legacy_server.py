"""Minimal legacy (2025-xx) MCP server on the official Python SDK 1.x.

Used to observe how a real legacy server answers 2026-07-28 requests (the
modern envelope, ``server/discover``), so ``legacyModernProbe`` in
``unit_expert_mcp.server`` can mirror it. Not part of the package and not
deployed. Needs ``mcp>=1,<2`` (the project's dev ``.venv``), not the ``sdk2026``
extra.

    PORT=8001 .venv/bin/python scripts/sdk1_legacy_server.py

Environment:
    PORT           listen port (default 8001)
    MCP_STATELESS  "1" for stateless_http=True (no Mcp-Session-Id)
    MCP_JSON       "1" for json_response=True (application/json instead of SSE)
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "sdk1-legacy",
    host="127.0.0.1",
    port=int(os.getenv("PORT", "8001")),
    stateless_http=os.getenv("MCP_STATELESS") == "1",
    json_response=os.getenv("MCP_JSON") == "1",
)


@mcp.tool()
def convert_length(value: float, from_unit: str, to_unit: str) -> str:
    """Convert km to m. Enough to have one callable tool."""
    if (from_unit, to_unit) != ("km", "m"):
        raise ValueError(f"unsupported units: {from_unit} -> {to_unit}")
    return f"{value:g} km = {value * 1000:g} m"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
