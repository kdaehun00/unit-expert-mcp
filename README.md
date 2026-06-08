# Unit Expert MCP

A small Model Context Protocol server that converts common units for length,
weight, temperature, area, and volume.

## Tools

- `convert_length(value, from_unit, to_unit)`
- `convert_weight(value, from_unit, to_unit)`
- `convert_temperature(value, from_unit, to_unit)`
- `convert_area(value, from_unit, to_unit)`
- `convert_volume(value, from_unit, to_unit)`
- `list_supported_units()`

## Supported Units

Length:

- `mm`, `cm`, `m`, `km`, `in`, `ft`, `yd`, `mi`

Weight:

- `mg`, `g`, `kg`, `t`, `oz`, `lb`

Temperature:

- `c`, `f`, `k`

Area:

- `mm2`, `cm2`, `m2`, `km2`, `in2`, `ft2`, `yd2`, `acre`

Volume:

- `ml`, `l`, `m3`, `in3`, `ft3`, `cup`, `pt`, `qt`, `gal`, `floz`

## Run Locally

Install dependencies:

```bash
uv sync
```

Run the server directly:

```bash
uv run unit-expert-mcp
```

Or run with the MCP CLI:

```bash
uv run mcp run src/unit_expert_mcp/server.py
```

Test with MCP Inspector:

```bash
uv run mcp dev src/unit_expert_mcp/server.py
```

## Client Config Example

For clients that launch servers over stdio, use:

```json
{
  "mcpServers": {
    "unit-expert": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/unit-expert-mcp",
        "run",
        "unit-expert-mcp"
      ]
    }
  }
}
```

## Development

```bash
uv run pytest
uv run ruff check .
```
