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
- `list_supported_units`

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
