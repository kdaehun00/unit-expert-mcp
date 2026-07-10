# Unit Expert MCP

Minimal Streamable HTTP MCP server for PlayMCP.

## PlayMCP Settings

```text
MCP 이름: Unit Expert(단위전문가)
MCP 식별자: unitExpert
MCP URL: https://unit-expert-mcp.onrender.com/mcp
인증: 없음
```

## HTTP Endpoints

```text
GET  /
GET  /healthz
POST /mcp
GET  /mcp
GET  /scenario
POST /scenario
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

Open `/scenario` in a browser to set the server-wide default scenario. After
applying a scenario there, `/mcp` returns that scenario response even when the
client sends no custom header.

Use `X-MCP-Test-Scenario` to override the server-wide state for one request.
Omit it for the currently selected scenario.

The selected scenario is stored in process memory, so it resets to `ok` after a
server restart or redeploy.

```text
X-MCP-Test-Scenario: duplicate-tool-name
```

Supported scenarios:

| Scenario | 효과 |
| --- | --- |
| `ok` | 정상 Unit Expert 도구 목록을 반환합니다. |
| `valid-tools` | 검증용 정상 도구 1개만 반환합니다. |
| `auth-401` | JSON-RPC 처리 전에 `401 Unauthorized`를 반환합니다. |
| `auth-403` | JSON-RPC 처리 전에 `403 Forbidden`을 반환합니다. |
| `no-tools-capability` | `initialize.result.capabilities.tools`를 제거합니다. |
| `tools-list-error` | `tools/list`에서 JSON-RPC error를 반환합니다. |
| `tools-list-null` | `tools/list`에서 `tools: null`을 반환합니다. |
| `tools-list-empty` | `tools/list`에서 `tools: []`를 반환합니다. |
| `duplicate-tool-name` | 중복된 tool name을 반환합니다. |
| `too-many-tools` | 도구 21개를 반환합니다. |
| `invalid-tool-name-char` | 허용되지 않는 문자가 포함된 tool name을 반환합니다. |
| `invalid-tool-name-length` | 129자 길이의 tool name을 반환합니다. |
| `missing-name` | `name`이 없는 tool을 반환합니다. |
| `missing-description` | `description`이 없는 tool을 반환합니다. |
| `missing-input-schema` | `inputSchema`가 없는 tool을 반환합니다. |
| `missing-annotations` | `annotations`가 없는 tool을 반환합니다. |
| `forbidden-kakao-name` | `kakao`가 포함된 tool name을 반환합니다. |
| `mcp-identifier-name` | `kakaomap_search` tool name을 반환합니다. |
| `long-description` | 1,051자 `description`을 반환합니다. |
| `missing-service-name-in-description` | 서비스명이 빠진 `description`을 반환합니다. |
| `incomplete-annotations` | 필수 필드가 빠진 `annotations`를 반환합니다. |
| `delayed-response` | OPTIONS가 아닌 `/mcp` 요청을 5초 지연시킵니다. |

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
