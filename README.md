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
  -H 'MCP-Protocol-Version: 2024-11-05' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

## Mock Protected HTTP Server

For MCP clients that need to verify auth failures without issuing real user
tokens, run the Streamable HTTP server with mock auth enabled:

```bash
uv run unit-expert-mcp --transport streamable-http --port 8000 --mock-auth
```

Requests to `/mcp`, including `initialize`, are checked before JSON-RPC runs:

- Missing or differently named header: `401 Unauthorized`
- Matching header name with a wrong value: `403 Forbidden`
- Matching header name and value: request is forwarded to MCP JSON-RPC

The default bypass header is:

```text
X-MCP-Mock-Auth: allow
```

The successful JSON-RPC endpoint is still:

```text
http://localhost:8000/mcp
```

Streamable HTTP is stateful by default. The `initialize` response includes an
`mcp-session-id` header, and clients should send that same header on subsequent
`GET`, `POST`, and `DELETE` requests for the session.

The server supports only MCP protocol version `2024-11-05` by default. MCP
initialization does not return a list of all supported versions. The client sends
one `initialize.params.protocolVersion`, and the server responds with the single
protocol version it will use.

During `initialize`:

- Client requests `2024-11-05`: server responds `2024-11-05`
- Client requests any other version: server responds `2024-11-05`

Override this for experiments with:

```bash
uv run unit-expert-mcp \
  --transport streamable-http \
  --port 8000 \
  --mock-auth \
  --protocol-versions 2024-11-05
```

You can override the required header:

```bash
uv run unit-expert-mcp \
  --transport streamable-http \
  --port 8000 \
  --mock-auth \
  --mock-auth-header X-Local-MCP-Auth \
  --mock-auth-header-value local-dev
```

Equivalent environment variables are `MCP_MOCK_AUTH=true`,
`MCP_MOCK_AUTH_HEADER`, `MCP_MOCK_AUTH_HEADER_VALUE`, and
`MCP_PROTOCOL_VERSIONS`.

## Validation Scenarios

Run one server and select validation behavior per request with
`X-MCP-Test-Scenario`:

```bash
uv run unit-expert-mcp --transport streamable-http --port 8000 --mock-auth
```

Example request header:

```text
X-MCP-Test-Scenario: duplicate-tool-name
```

Supported scenarios:

| Scenario | Effect |
| --- | --- |
| `ok` | Normal Unit Expert tools |
| `valid-tools` | Returns one validation-friendly tool |
| `auth-401` | Returns `401 Unauthorized` before JSON-RPC |
| `auth-403` | Returns `403 Forbidden` before JSON-RPC |
| `no-tools-capability` | Removes `initialize.result.capabilities.tools` |
| `tools-list-error` | Returns a JSON-RPC error from `tools/list` |
| `tools-list-null` | Returns `tools: null` from `tools/list` |
| `tools-list-empty` | Returns `tools: []` from `tools/list` |
| `duplicate-tool-name` | Returns duplicate tool names |
| `too-many-tools` | Returns 21 tools |
| `invalid-tool-name-char` | Returns a tool name with disallowed characters |
| `invalid-tool-name-length` | Returns a 111-character tool name |
| `missing-name` | Returns a tool without `name` |
| `missing-description` | Returns a tool without `description` |
| `missing-input-schema` | Returns a tool without `inputSchema` |
| `missing-annotations` | Returns a tool without `annotations` |
| `forbidden-kakao-name` | Returns a tool name containing `kakao` |
| `mcp-identifier-name` | Returns `kakaomap_search` |
| `long-description` | Returns a 1051-character description |
| `missing-service-name-in-description` | Returns a description without the server name |
| `incomplete-annotations` | Returns annotations missing required fields |
| `delayed-response` | Delays each non-OPTIONS `/mcp` request by 5 seconds |

The older CLI flags (`--disable-tools-capability`, `--null-tools-list`,
`--fail-tools-list`, `--request-delay-seconds`) remain available, but the header
scenarios avoid restarting the server for each validation case.

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
