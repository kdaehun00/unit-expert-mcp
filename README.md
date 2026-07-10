# Unit Expert MCP

Minimal Streamable HTTP MCP server for PlayMCP.

## PlayMCP Settings

```text
MCP 이름: Unit Expert(단위전문가)
MCP 식별자: unit_expert
MCP URL: https://unit-expert-mcp.onrender.com/mcp
인증: 없음
```

## HTTP Endpoints

```text
GET  /healthz
POST /mcp
GET  /mcp
```

`POST /mcp` expects JSON-RPC with:

```text
Accept: application/json, text/event-stream
Content-Type: application/json
```

## Tools

- `convert_length`
- `convert_weight`
- `convert_temperature`
- `convert_area`
- `convert_volume`
- `list_supported_units`

## Header Scenarios

Use `X-MCP-Test-Scenario` to change behavior per request. Omit it for the normal
PlayMCP-compatible path.

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
| `invalid-tool-name-length` | Returns a 129-character tool name |
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

## Run Locally

```bash
PYTHONPATH=src PORT=8000 python -m unit_expert_mcp.server
```

Smoke test:

```bash
curl -i -X POST http://localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-03-26' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test*.py'
```
