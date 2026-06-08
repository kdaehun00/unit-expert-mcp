"""MCP tool registration for Unit Expert."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from unit_expert_mcp.converter import (
    convert_area as convert_area_value,
    convert_length as convert_length_value,
    convert_temperature as convert_temperature_value,
    convert_volume as convert_volume_value,
    convert_weight as convert_weight_value,
    list_supported_units as list_supported_units_value,
)

mcp = FastMCP("Unit Expert MCP")


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
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
