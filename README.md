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

Run the server with Streamable HTTP:

```bash
uv run unit-expert-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

Or run with the MCP CLI:

```bash
uv run mcp run src/unit_expert_mcp/server.py
```

Test with MCP Inspector:

```bash
uv run mcp dev src/unit_expert_mcp/server.py
```

## Docker

Build and run the server:

```bash
docker build -t unit-expert-mcp .
docker run --rm -p 8000:8000 unit-expert-mcp
```

The Streamable HTTP endpoint is:

```text
http://localhost:8000/mcp
```

Health check endpoint:

```text
http://localhost:8000/healthz
```

## Public HTTP Deployment

To expose this server like `https://mcp.example.com/mcp`, deploy the Docker image
to a host that can keep a long-running HTTP service online, then attach an HTTPS
domain to it.

Required runtime settings:

```text
MCP_TRANSPORT=streamable-http
MCP_HOST=0.0.0.0
PORT=8000
```

The Dockerfile already sets these defaults. If your platform injects a different
`PORT`, the server reads it automatically.

Deployment checklist:

- Build and run the container from this repository.
- Expose the container's HTTP port through the platform load balancer.
- Point your DNS record, such as `mcp.example.com`, to that load balancer.
- Enable HTTPS/TLS on the domain.
- Configure the platform health check to `GET /healthz`.
- Share the MCP endpoint as `https://mcp.example.com/mcp`.

If you run it on your own VM behind Nginx, the reverse proxy only needs to pass
`/mcp` and `/healthz` to the app:

```nginx
server {
    server_name mcp.example.com;

    location /mcp {
        proxy_pass http://127.0.0.1:8000/mcp;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /healthz {
        proxy_pass http://127.0.0.1:8000/healthz;
    }
}
```

Smoke test the deployed endpoint:

```bash
curl -i -X POST https://mcp.example.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

## PlayMCP Registration

Use the public Streamable HTTP endpoint without authentication:

```text
https://unit-expert-mcp.onrender.com/mcp
```

The server uses stateful Streamable HTTP and returns an `mcp-session-id` header
from `initialize`. Clients should send that header on subsequent requests in the
same session.

The server supports MCP protocol versions `2025-03-26`, `2025-06-18`, and
`2025-11-25` by default. MCP initialization does not return a list of all
supported versions. The client sends one `initialize.params.protocolVersion`,
and the server responds with the single protocol version it will use.

During `initialize`:

- Client requests `2025-03-26`: server responds `2025-03-26`
- Client requests `2025-11-25`: server responds `2025-11-25`

Override this for experiments with:

```bash
uv run unit-expert-mcp \
  --transport streamable-http \
  --port 8000 \
  --protocol-versions 2025-03-26,2025-06-18,2025-11-25
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
