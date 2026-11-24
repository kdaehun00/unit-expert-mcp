"""Predefined scenarios and their expectations.

Expectations follow the official 2026-07-28 spec (docs/SPEC_2026-07-28_NOTES.md),
not the current behaviour of server.py. ``basis`` names the spec page; the
``Gateway`` entry says whether a gateway (an MCP proxy in front of the server)
is expected to show the same behaviour, a different one, or an unconfirmed one.

Spec pages (https://modelcontextprotocol.io/specification/2026-07-28/...):
  transport  basic/transports/streamable-http     meta      basic/index#_meta
  errors     basic/index#error-codes              discover  server/discover
  versioning basic/versioning                     tools     server/tools
  mrtr       basic/patterns/mrtr                  caching   server/utilities/caching
  pagination server/utilities/pagination          authz     basic/authorization
  legacy     specification/2025-11-25/basic/{lifecycle,transports}
"""

from __future__ import annotations

import base64
from typing import Any

from . import oauth
from . import protocol as p
from .clients import ProbeResult, probe
from .runner import (
    MULTI_ROUND_INPUT_ARGS,
    MULTI_ROUND_INPUT_TOOL,
    Context,
    Gateway,
    Scenario,
    Stop,
)
from .transport import Exchange, deep_merge, origin_of

_COND_NOTE = "게이트웨이 코드 조사로 확인한 동작이지만 실제 요청 경로에 적용되는지는 미확인"
UNKNOWN_SESSION = "unit-expert-client-unknown-session"
UNKNOWN_TOOL = "unit_expert_client_no_such_tool"
BOGUS_VERSION = "2099-01-01"
AUTH_CFG = {"auth": {"mode": "oauth"}}  # server.py: AUTH_MODES / enforce_auth
# Server options that are off by default (docs/CONFIG_REFERENCE.md). With
# --expect server the scenarios that need them switch them on through ?cfg=.
SCOPE_CFG = {"auth": {"enforceScope": True}}
SESSION_CFG = {"session": {"validate": True}}
PAGED_CFG = {"toolsList": {"pageSize": 3}}

SCENARIOS: list[Scenario] = []


def scenario(
    name: str,
    group: str,
    expect: str,
    basis: str,
    gateway: Gateway,
    needs: str | None = None,
    injects: bool = False,
):
    def register(fn):
        SCENARIOS.append(Scenario(name, group, expect, basis, fn, gateway, needs, injects))
        return fn

    return register


def cond(extra: str = "") -> Gateway:
    return Gateway("cond", f"{extra + ' / ' if extra else ''}{_COND_NOTE}")


# ============================================================================
# modern: compliant 2026-07-28 requests
# ============================================================================
@scenario(
    "discover",
    "modern",
    "200, resultType complete, supportedVersions에 2026-07-28, capabilities, ttlMs>=0, cacheScope; "
    "_meta serverInfo(SHOULD), 세션 ID 미발급(SHOULD)",
    "discover(서버 MUST 구현), caching(ttlMs·cacheScope MUST), meta(resultType MUST)",
    Gateway(
        "unknown",
        "게이트웨이 기대값 미확정: 게이트웨이가 클라이언트의 server/discover에 어떻게 답하는지 확인하지 못함",
    ),
    needs="modern",
)
def discover(c: Context) -> None:
    exchange = c.modern().discover()
    result = c.expect_result(exchange)
    versions = result.get("supportedVersions")
    c.check(
        "supportedVersions에 2026-07-28 포함",
        isinstance(versions, list) and p.MODERN_VERSION in versions,
        f"[..., {p.MODERN_VERSION!r}]",
        repr(versions),
    )
    c.check(
        "capabilities 객체",
        isinstance(result.get("capabilities"), dict),
        "object",
        repr(result.get("capabilities")),
    )
    c.expect_cache_fields(exchange)
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    c.check(
        "_meta에 serverInfo",
        isinstance(meta.get(p.META_SERVER_INFO), dict),
        f"_meta[{p.META_SERVER_INFO!r}]",
        repr(meta.get(p.META_SERVER_INFO)),
        level="should",
    )
    c.check(
        "Mcp-Session-Id 미발급",
        exchange.header(p.H_SESSION) is None,
        "헤더 없음",
        repr(exchange.header(p.H_SESSION)),
        level="should",
    )


@scenario(
    "tools-list",
    "modern",
    "200, resultType complete, tools 배열(각 툴에 name·inputSchema), ttlMs>=0, cacheScope",
    "tools, caching(tools/list 결과에 ttlMs·cacheScope MUST)",
    cond("모던 tools/list 결과에 resultType과 serverInfo를 채움"),
    needs="modern",
)
def tools_list(c: Context) -> None:
    client = c.modern()
    exchange = client.list_tools()
    result = c.expect_result(exchange)
    tools = result.get("tools")
    c.check("tools 배열", isinstance(tools, list), "array", type(tools).__name__)
    malformed = [
        tool.get("name") if isinstance(tool, dict) else tool
        for tool in tools or []
        if not (
            isinstance(tool, dict)
            and isinstance(tool.get("name"), str)
            and isinstance(tool.get("inputSchema"), dict)
        )
    ]
    c.check("모든 툴에 name과 inputSchema", not malformed, "없음", f"형식 오류 {malformed}")
    c.expect_cache_fields(exchange)
    _, warnings = p.split_usable_tools(tools)
    for warning in warnings:
        c.note(f"x-mcp-header 규칙 위반으로 제외: {warning}")


@scenario(
    "tools-list-pagination",
    "modern",
    "nextCursor를 끝까지 따라가면 종료, 페이지 간 cacheScope 동일(MUST), 두 번 조회해도 순서 동일(SHOULD)",
    "pagination, caching(페이지마다 cacheScope 동일 MUST), tools(순서 결정적 SHOULD)",
    cond("업스트림 쪽 페이징만 확인했고 클라이언트 쪽 커서 동작은 미확인"),
    needs="modern",
)
def tools_list_pagination(c: Context) -> None:
    client = c.modern(cfg=PAGED_CFG)
    client.load_tools()
    pages = list(client.pages)
    for page in pages:
        c.expect_result(page)
    if c.injectable():
        c.check(
            "toolsList.pageSize를 주입하면 여러 페이지로 나뉨",
            len(pages) > 1,
            "2페이지 이상",
            f"{len(pages)}페이지",
        )
    last = pages[-1].result or {}
    c.check(
        "마지막 페이지에 nextCursor 없음",
        not isinstance(last.get("nextCursor"), str),
        "nextCursor 없음",
        f"{len(pages)}페이지 후에도 nextCursor={last.get('nextCursor')!r}",
    )
    scopes = {(page.result or {}).get("cacheScope") for page in pages}
    c.check(
        "페이지 간 cacheScope 동일", len(scopes) == 1, "한 가지 값", repr(sorted(map(str, scopes)))
    )
    names = [tool.get("name") for tool in client.raw_tools]
    again = c.modern(cfg=PAGED_CFG)
    again.load_tools()
    c.check(
        "재조회 시 툴 순서 동일",
        names == [tool.get("name") for tool in again.raw_tools],
        "같은 순서",
        "순서 또는 내용이 다름",
        level="should",
    )
    c.note(
        f"{len(pages)}페이지, 툴 {len(names)}개"
        + ("" if len(pages) > 1 else " (nextCursor 없음: 단일 페이지)")
    )


@scenario(
    "tools-list-bad-cursor",
    "modern",
    "잘못된 cursor에 JSON-RPC -32602 (SHOULD)",
    "pagination(invalid cursor는 -32602 SHOULD)",
    Gateway("unknown", "다운스트림 cursor 검증 코드를 확인하지 못함"),
    needs="modern",
)
def tools_list_bad_cursor(c: Context) -> None:
    exchange = c.modern().list_tools("unit-expert-client-invalid-cursor")
    c.expect_code(exchange, p.ERR_INVALID_PARAMS, level="should")
    c.note(f"HTTP 상태(스펙 미명시): {exchange.status}")


@scenario(
    "tools-call-ok",
    "modern",
    "tools/list로 스키마를 얻은 뒤 호출: 200, resultType complete, isError 아님, content 배열",
    "tools, transport(Mcp-Name·Mcp-Param-* 자동 부착)",
    cond("모던 tools/call을 업스트림 호출로 바꿔 전달하고 결과를 모던 형태로 돌려줌"),
    needs="modern",
)
def tools_call_ok(c: Context) -> None:
    client = c.modern()
    tools = client.load_tools()
    if not tools:
        c.skip(f"tools/list에서 툴을 얻지 못함 ({c.describe(client.pages[0])})")
    tool, arguments = c.plain_tool(tools)
    exchange = client.call_tool(tool["name"], arguments)[-1]
    result = c.expect_result(exchange)
    c.check(
        "isError가 true가 아님",
        result.get("isError") is not True,
        "false 또는 없음",
        repr(result.get("isError")),
    )
    c.check(
        "content 배열",
        isinstance(result.get("content"), list),
        "array",
        type(result.get("content")).__name__,
    )
    c.note(f"호출한 툴: {tool['name']} {arguments}")


@scenario(
    "tools-call-direct",
    "modern",
    "같은 클라이언트에서 discover·tools/list를 먼저 보내지 않고 바로 tools/call: 200, resultType complete",
    "versioning·transport(무상태: 핸드셰이크 없이 매 요청이 자기 완결)",
    cond("모던 tools/call을 업스트림 호출로 바꿔 전달함"),
    needs="modern",
)
def tools_call_direct(c: Context) -> None:
    tool, arguments = c.plain_tool(c.tools().tools or [])
    exchange = c.modern().call_tool(tool["name"], arguments, tool=tool)[-1]
    result = c.expect_result(exchange)
    c.check(
        "isError가 true가 아님",
        result.get("isError") is not True,
        "false 또는 없음",
        repr(result.get("isError")),
    )


@scenario(
    "tool-error",
    "modern",
    "툴 실행 오류는 프로토콜 에러가 아니라 200 + result.isError true",
    "tools#error-handling(isError: 입력값 검증·비즈니스 오류)",
    cond("툴 실행 오류(isError 결과)를 200으로 돌려줌"),
    needs="modern",
)
def tool_error(c: Context) -> None:
    client = c.tools()
    tool = c.unit_tool(client.tools or [])
    if tool is None:
        c.skip("입력 오류를 유도할 단위 변환 툴(value/from_unit/to_unit)이 없음")
    arguments = {**c.arguments_for(tool), "from_unit": "not-a-unit"}
    exchange = c.modern().call_tool(tool["name"], arguments, tool=tool)[-1]
    result = c.expect_result(exchange)
    c.check("isError true", result.get("isError") is True, "true", repr(result.get("isError")))


@scenario(
    "unknown-tool",
    "modern",
    "알 수 없는 툴은 isError 결과가 아니라 JSON-RPC -32602 (HTTP 상태는 스펙 미명시라 기록만)",
    "tools#error-handling(Unknown tool 예시가 -32602), errors",
    Gateway(
        "cond",
        f"알 수 없는 툴은 -32602 / {_COND_NOTE}",
    ),
    needs="modern",
)
def unknown_tool(c: Context) -> None:
    exchange = c.modern().call_tool(UNKNOWN_TOOL, {}, tool=None)[-1]
    c.expect_code(exchange, p.ERR_INVALID_PARAMS)
    c.note(f"HTTP 상태(스펙 미명시): {exchange.status}")


def _sent_header(exchange: Exchange, name: str) -> str | None:
    for key, value in exchange.request_headers:
        if key.lower() == name.lower():
            return value.decode("latin-1") if isinstance(value, bytes) else value
    return None


@scenario(
    "param-header-ok",
    "modern",
    "x-mcp-header 인자를 Mcp-Param-*로 복제(문자열 그대로, 정수 10진, 불리언 true/false, 비ASCII는 "
    "=?base64?...?=)하면 서버가 받아들임. null/없는 인자는 헤더 생략",
    "transport(Custom headers from tool parameters, Value Encoding)",
    Gateway(
        "cond",
        f"Mcp-Param-*는 검증하지 않고 그대로 업스트림에 넘김 / {_COND_NOTE}",
    ),
    needs="modern",
)
def param_header_ok(c: Context) -> None:
    client = c.tools()
    header_tools = c.header_tools(client.tools or [])
    cases = [
        ("문자열", "string", "us-west1", "us-west1"),
        ("정수", "integer", 42, "42"),
        ("불리언", "boolean", True, "true"),
        ("비ASCII 문자열", "string", "Hello, 世界", "=?base64?SGVsbG8sIOS4lueVjA==?="),
    ]
    for label, kind, value, expected_header in cases:
        match = next(
            (
                (tool, param)
                for tool, params in header_tools
                for param in params
                if param.type == kind
            ),
            None,
        )
        if match is None:
            c.note(f"{label}: {kind} 타입 x-mcp-header 속성이 없어 생략")
            continue
        tool, param = match
        arguments = p.sample_arguments(tool.get("inputSchema"))
        p.set_path(arguments, param.path, value)
        exchange = c.modern().call_tool(
            tool["name"], arguments, tool=tool, label=f"tools/call ({label})"
        )[-1]
        sent = _sent_header(exchange, param.header_name)
        c.check(
            f"{label}: 클라이언트가 보낸 {param.header_name}",
            sent == expected_header,
            expected_header,
            repr(sent),
        )
        c.check(
            f"{label}: 서버가 프로토콜 에러 없이 처리",
            exchange.status == 200 and exchange.rpc_error is None,
            "HTTP 200, JSON-RPC 에러 없음",
            c.describe(exchange),
        )
        if (exchange.result or {}).get("isError") is True:
            c.note(f"{label}: 툴이 isError를 돌려줌(값 검증 실패로 보이며 헤더 검증과는 무관)")
    optional = next(
        (
            (tool, param)
            for tool, params in header_tools
            for param in params
            if len(param.path) == 1
            and param.path[0] not in (tool.get("inputSchema", {}).get("required") or [])
        ),
        None,
    )
    if optional is None:
        c.note("헤더 생략: 선택(optional) x-mcp-header 속성이 없어 생략")
        return
    tool, param = optional
    arguments = p.sample_arguments(tool.get("inputSchema"))
    arguments.pop(param.path[0], None)
    exchange = c.modern().call_tool(
        tool["name"], arguments, tool=tool, label="tools/call (인자 없음)"
    )[-1]
    c.check(
        f"인자가 없으면 {param.header_name} 생략",
        _sent_header(exchange, param.header_name) is None,
        "헤더 없음",
        repr(_sent_header(exchange, param.header_name)),
    )
    c.check(
        "인자 없음: 서버가 헤더를 기대하지 않음",
        exchange.rpc_code != p.ERR_HEADER_MISMATCH,
        "-32020 아님",
        c.describe(exchange),
    )


@scenario(
    "input-required-roundtrip",
    "modern",
    "resultType input_required(inputRequests 또는 requestState 중 하나 이상) -> 같은 params + inputResponses, "
    "requestState 그대로, 다른 JSON-RPC id로 재요청 -> complete",
    "mrtr(InputRequiredResult, 재요청 규칙)",
    Gateway(
        "unknown",
        "게이트웨이 기대값 미확정: input_required 재요청을 어떻게 중계하는지 확인하지 못함",
    ),
    needs="modern",
)
def input_required_roundtrip(c: Context) -> None:
    tool = c.input_tool(c.tools().tools or [])
    capabilities = {
        "elicitation": {},
        "sampling": {},
        "roots": {},
        **(c.options.capabilities or {}),
    }
    client = c.modern(capabilities=capabilities)
    arguments = c.arguments_for(tool)
    exchanges = client.call_tool(tool["name"], arguments, tool=tool, auto_input=True)
    first = exchanges[0]
    if first.status == 200 and first.result is not None and first.result_type in (None, "complete"):
        c.skip(
            f"툴 {tool['name']}이 input_required 없이 바로 complete를 돌려줌 (--input-tool/--args로 지정)"
        )
    result = c.expect_result(first, result_type="input_required")
    requests = result.get("inputRequests")
    c.check(
        "inputRequests 또는 requestState 중 하나 이상",
        isinstance(requests, dict) or "requestState" in result,
        "둘 중 하나 이상",
        f"키 {sorted(result)}",
    )
    allowed = {"elicitation/create", "sampling/createMessage", "roots/list"}
    methods = [req.get("method") for req in (requests or {}).values() if isinstance(req, dict)]
    c.check(
        "inputRequests의 method", set(methods) <= allowed, f"{sorted(allowed)} 중", repr(methods)
    )
    if len(exchanges) < 2:
        return
    for previous, retry in zip(exchanges, exchanges[1:]):
        prev_body, retry_body = previous.request_json() or {}, retry.request_json() or {}
        state = (previous.result or {}).get("requestState")
        c.check(
            "재요청의 JSON-RPC id가 다름",
            prev_body.get("id") != retry_body.get("id"),
            "다른 id",
            f"{prev_body.get('id')!r} -> {retry_body.get('id')!r}",
        )
        sent_state = (retry_body.get("params") or {}).get("requestState")
        c.check("requestState를 그대로 되돌림", sent_state == state, repr(state), repr(sent_state))
    c.expect_result(exchanges[-1])
    c.note(f"툴 {tool['name']}, 왕복 {len(exchanges) - 1}회")


@scenario(
    "input-required-multi-round",
    "modern",
    "1차·2차 응답이 input_required, 3차가 complete. 재요청마다 직전 응답의 requestState와 그 라운드의 "
    "inputResponses만 보내고 JSON-RPC id를 바꿈",
    "mrtr(InputRequiredResult, 재요청 규칙, 여러 라운드)",
    Gateway(
        "unknown",
        "게이트웨이 기대값 미확정: 여러 라운드의 input_required 재요청을 어떻게 중계하는지 확인하지 못함",
    ),
    needs="modern",
)
def input_required_multi_round(c: Context) -> None:
    tool = next(
        (tool for tool in c.tools().tools or [] if tool.get("name") == MULTI_ROUND_INPUT_TOOL), None
    )
    if tool is None:
        c.skip(f"tools/list에 두 라운드 툴 {MULTI_ROUND_INPUT_TOOL}이 없음")
    capabilities = {"elicitation": {}, **(c.options.capabilities or {})}
    client = c.modern(capabilities=capabilities)
    arguments = (
        dict(c.options.args)
        if c.options.args is not None and c.options.tool == MULTI_ROUND_INPUT_TOOL
        else dict(MULTI_ROUND_INPUT_ARGS)
    )
    exchanges = client.call_tool(tool["name"], arguments, tool=tool, auto_input=True)
    kinds = [exchange.result_type for exchange in exchanges]
    expected = ["input_required", "input_required", "complete"]
    c.check("라운드별 resultType", kinds == expected, repr(expected), repr(kinds))
    for index, exchange in enumerate(exchanges[:3]):
        c.expect_result(exchange, result_type=expected[index])
    for previous, retry in zip(exchanges, exchanges[1:]):
        asked = (previous.result or {}).get("inputRequests")
        asked_keys = sorted(asked) if isinstance(asked, dict) else []
        prev_body, retry_body = previous.request_json() or {}, retry.request_json() or {}
        retry_params = retry_body.get("params") or {}
        c.check(
            f"{retry.label}: JSON-RPC id가 다름",
            prev_body.get("id") != retry_body.get("id"),
            "다른 id",
            f"{prev_body.get('id')!r} -> {retry_body.get('id')!r}",
        )
        state = (previous.result or {}).get("requestState")
        c.check(
            f"{retry.label}: 직전 응답의 requestState를 그대로 되돌림",
            retry_params.get("requestState") == state,
            repr(state),
            repr(retry_params.get("requestState")),
        )
        sent_keys = sorted(retry_params.get("inputResponses") or {})
        c.check(
            f"{retry.label}: 그 라운드의 inputResponses만 보냄",
            sent_keys == asked_keys,
            repr(asked_keys),
            repr(sent_keys),
        )
        c.check(
            f"{retry.label}: 원래 arguments 유지",
            retry_params.get("arguments") == (prev_body.get("params") or {}).get("arguments"),
            repr((prev_body.get("params") or {}).get("arguments")),
            repr(retry_params.get("arguments")),
        )
    if len(exchanges) >= 2:
        first_keys = sorted((exchanges[0].result or {}).get("inputRequests") or {})
        second_keys = sorted((exchanges[1].result or {}).get("inputRequests") or {})
        c.check(
            "2차 라운드는 1차와 다른 입력을 물음",
            bool(second_keys) and first_keys != second_keys,
            f"{first_keys!r}와 다른 키",
            repr(second_keys),
        )
    final = exchanges[-1].result or {}
    c.check("최종 결과 isError", final.get("isError") is False, "False", repr(final.get("isError")))
    c.note(f"툴 {tool['name']}, 왕복 {len(exchanges) - 1}회")


@scenario(
    "subscriptions-listen",
    "modern",
    "200 text/event-stream, 첫 메시지가 notifications/subscriptions/acknowledged, _meta subscriptionId가 요청 id",
    "basic/patterns/subscriptions(ack가 스트림 첫 메시지 MUST, subscriptionId MUST)",
    cond("게이트웨이가 지원하는 모던 메서드가 아니어서 404 + -32601"),
    needs="modern",
)
def subscriptions_listen(c: Context) -> None:
    client = c.modern()
    exchange = client.request(
        "subscriptions/listen", {"notifications": {"toolsListChanged": True}}, max_sse_events=1
    )
    if c.gateway:
        c.expect_error(exchange, 404, p.ERR_METHOD_NOT_FOUND)
        return
    if exchange.rpc_code == p.ERR_METHOD_NOT_FOUND:
        c.skip("대상이 subscriptions/listen을 구현하지 않음(-32601)")
    c.expect_status(exchange, 200)
    c.check("응답이 text/event-stream", exchange.is_sse, "text/event-stream", exchange.content_type)
    first = exchange.events[0].json if exchange.events else None
    method = first.get("method") if isinstance(first, dict) else None
    c.check(
        "첫 메시지가 acknowledged",
        method == "notifications/subscriptions/acknowledged",
        "notifications/subscriptions/acknowledged",
        repr(method),
    )
    meta = ((first or {}).get("params") or {}).get("_meta") if isinstance(first, dict) else None
    request_id = (exchange.request_json() or {}).get("id")
    c.check(
        "_meta subscriptionId가 요청 id",
        isinstance(meta, dict) and meta.get(p.META_SUBSCRIPTION_ID) == request_id,
        repr(request_id),
        repr((meta or {}).get(p.META_SUBSCRIPTION_ID) if isinstance(meta, dict) else None),
    )


# ============================================================================
# modern-violation: requests the server MUST reject (or must tolerate)
# ============================================================================
def _reject(c: Context, exchange: Exchange, status: int, code: int) -> None:
    c.expect_error(exchange, status, code)


def _violation(
    name: str, expect: str, basis: str, gateway: Gateway, status: int, code: int, **tweak: Any
):
    @scenario(name, "modern-violation", expect, basis, gateway, needs="modern")
    def run(c: Context) -> None:
        _reject(c, c.modern().list_tools(tweaks=p.Tweaks(**tweak)), status, code)

    return run


_violation(
    "no-meta-version",
    "_meta에 protocolVersion 없음 -> 400 + -32602",
    "meta(필수 필드 누락은 -32602, HTTP 400 MUST)",
    cond("필수 _meta 누락 400 + -32602"),
    400,
    p.ERR_INVALID_PARAMS,
    omit_meta_version=True,
)
_violation(
    "no-capabilities",
    "_meta에 clientCapabilities 없음 -> 400 + -32602",
    "meta(필수 필드 누락은 -32602, HTTP 400 MUST)",
    cond("clientCapabilities가 객체가 아니면 400 + -32602"),
    400,
    p.ERR_INVALID_PARAMS,
    omit_capabilities=True,
)


@scenario(
    "no-version-header",
    "modern-violation",
    "MCP-Protocol-Version 헤더 없음 -> 400 + -32020. 레거시도 받는 서버는 2025-03-26 요청으로 보고 "
    "레거시 형태로 처리해도 됨(MAY)",
    "transport(모든 POST에 MUST, 필수 표준 헤더 누락은 400 + -32020. 2025-06-18 이전 클라이언트를 지원하는 서버는 "
    "헤더 없는 요청을 2025-03-26으로 간주 MAY, 지원하지 않는 서버는 거부 MUST)",
    cond("버전 헤더 누락 400 + -32020"),
    needs="modern",
)
def no_version_header(c: Context) -> None:
    exchange = c.modern().list_tools(tweaks=p.Tweaks(omit_version_header=True))
    legacy_shaped = (
        exchange.status == 200
        and exchange.result is not None
        and "resultType" not in exchange.result
    )
    if not c.gateway and c.era().verdict == "dual" and legacy_shaped:
        c.check(
            "헤더 없는 요청을 레거시 요청으로 처리(MAY)",
            True,
            "400 + -32020, 또는 레거시 형태 결과",
            c.describe(exchange),
        )
        c.note("대상이 헤더 없는 요청을 레거시(2025-03-26)로 간주해 resultType 없는 결과를 돌려줌")
        return
    _reject(c, exchange, 400, p.ERR_HEADER_MISMATCH)


_violation(
    "version-header-mismatch",
    "헤더는 2025-11-25, _meta는 2026-07-28 -> 400 + -32020",
    "transport·meta(헤더와 _meta 버전 불일치는 400 + -32020)",
    cond("헤더와 _meta 버전 불일치 400 + -32020"),
    400,
    p.ERR_HEADER_MISMATCH,
    version_header=p.LATEST_LEGACY_VERSION,
)
_violation(
    "version-header-unknown-value",
    f"헤더는 {BOGUS_VERSION}(모르는 값), _meta는 2026-07-28 -> 400 + -32020",
    "transport·meta(헤더와 _meta 버전 불일치는 400 + -32020)",
    cond("헤더와 _meta 버전 불일치 400 + -32020"),
    400,
    p.ERR_HEADER_MISMATCH,
    version_header=BOGUS_VERSION,
)
_violation(
    "no-method-header",
    "Mcp-Method 헤더 없음 -> 400 + -32020",
    "transport(Mcp-Method REQUIRED, 누락은 400 + -32020)",
    cond("Mcp-Method 누락 400 + -32020"),
    400,
    p.ERR_HEADER_MISMATCH,
    omit_method_header=True,
)
_violation(
    "method-header-mismatch",
    "Mcp-Method: server/discover인데 바디는 tools/list -> 400 + -32020",
    "transport(헤더와 바디 불일치는 400 + -32020)",
    cond("Mcp-Method 불일치 400 + -32020"),
    400,
    p.ERR_HEADER_MISMATCH,
    method_header="server/discover",
)


@scenario(
    "unsupported-version",
    "modern-violation",
    f"헤더와 _meta 모두 {BOGUS_VERSION} -> 400 + -32022, data.supported(문자열 배열)와 data.requested",
    "errors(-32022 data {supported, requested}), meta",
    cond("지원하지 않는 버전 400 + -32022, data.supported는 [2026-07-28, 2025-06-18]"),
    needs="modern",
)
def unsupported_version(c: Context) -> None:
    exchange = c.modern(version=BOGUS_VERSION).list_tools()
    c.expect_error(exchange, 400, p.ERR_UNSUPPORTED_VERSION)
    data = (exchange.rpc_error or {}).get("data")
    data = data if isinstance(data, dict) else {}
    supported = data.get("supported")
    c.check(
        "data.supported",
        isinstance(supported, list)
        and bool(supported)
        and all(isinstance(v, str) for v in supported),
        "비어 있지 않은 문자열 배열",
        repr(supported),
    )
    c.check(
        "data.requested",
        data.get("requested") == BOGUS_VERSION,
        repr(BOGUS_VERSION),
        repr(data.get("requested")),
    )
    if c.gateway:
        c.check(
            "게이트웨이 supported 목록",
            supported == [p.MODERN_VERSION, "2025-06-18"],
            repr([p.MODERN_VERSION, "2025-06-18"]),
            repr(supported),
        )


def _name_case(c: Context) -> tuple[dict[str, Any], dict[str, Any]]:
    return c.plain_tool(c.tools().tools or [])


@scenario(
    "name-header-missing",
    "modern-violation",
    "tools/call에 Mcp-Name 없음 -> 400 + -32020",
    "transport(tools/call에 Mcp-Name REQUIRED)",
    cond("Mcp-Name 누락 400 + -32020"),
    needs="modern",
)
def name_header_missing(c: Context) -> None:
    tool, arguments = _name_case(c)
    _reject(
        c,
        c.modern().call_tool(
            tool["name"], arguments, tool=tool, tweaks=p.Tweaks(omit_name_header=True)
        )[-1],
        400,
        p.ERR_HEADER_MISMATCH,
    )


@scenario(
    "name-header-mismatch",
    "modern-violation",
    "Mcp-Name이 params.name과 다름 -> 400 + -32020",
    "transport(헤더와 바디 불일치는 400 + -32020)",
    cond("Mcp-Name 불일치 400 + -32020"),
    needs="modern",
)
def name_header_mismatch(c: Context) -> None:
    tool, arguments = _name_case(c)
    tweaks = p.Tweaks(name_header=f"{tool['name']}_mismatch")
    _reject(
        c,
        c.modern().call_tool(tool["name"], arguments, tool=tool, tweaks=tweaks)[-1],
        400,
        p.ERR_HEADER_MISMATCH,
    )


def _sentinel(text: str) -> str:
    return f"{p.SENTINEL_PREFIX}{base64.b64encode(text.encode('utf-8')).decode('ascii')}{p.SENTINEL_SUFFIX}"


@scenario(
    "name-header-base64",
    "modern-violation",
    "Mcp-Name을 =?base64?...?=로 보내면 디코드 후 비교: 같은 이름은 통과, 다른 이름은 400 + -32020",
    "transport(Value Encoding: base64는 디코드 후 비교 MUST, Mcp-Name에도 적용)",
    cond("Mcp-Name을 base64 디코드한 뒤 비교, 불일치 400 + -32020"),
    needs="modern",
)
def name_header_base64(c: Context) -> None:
    tool, arguments = _name_case(c)
    accepted = c.modern().call_tool(
        tool["name"],
        arguments,
        tool=tool,
        tweaks=p.Tweaks(name_header=_sentinel(tool["name"])),
        label="tools/call (base64 Mcp-Name, 같은 이름)",
    )[-1]
    c.check(
        "디코드한 이름이 같으면 통과",
        accepted.status == 200 and accepted.rpc_error is None,
        "HTTP 200, JSON-RPC 에러 없음",
        c.describe(accepted),
    )
    rejected = c.modern().call_tool(
        tool["name"],
        arguments,
        tool=tool,
        tweaks=p.Tweaks(name_header=_sentinel(f"{tool['name']}_mismatch")),
        label="tools/call (base64 Mcp-Name, 다른 이름)",
    )[-1]
    _reject(c, rejected, 400, p.ERR_HEADER_MISMATCH)


def _param_case(
    c: Context, prefer: str | None = None
) -> tuple[dict[str, Any], p.HeaderParam, dict[str, Any]]:
    header_tools = c.header_tools(c.tools().tools or [])
    pairs = [(tool, param) for tool, params in header_tools for param in params]
    tool, param = next((pair for pair in pairs if pair[1].type == prefer), pairs[0])
    arguments = p.sample_arguments(tool.get("inputSchema"))
    value = {"string": "us-west1", "integer": 42, "boolean": True}[param.type]
    p.set_path(arguments, param.path, value)
    return tool, param, arguments


_PARAM_GW = Gateway(
    "unknown",
    "게이트웨이 기대값 미확정: 게이트웨이는 Mcp-Param-*를 검증하지 않고 업스트림이 검증함. 업스트림의 "
    "-32020이 클라이언트에 어떤 형태로 전달되는지 확인하지 못함",
)


@scenario(
    "param-header-missing",
    "modern-violation",
    "바디에 값이 있는데 Mcp-Param-* 없음 -> 400 + -32020",
    "transport(바디에 값이 있는데 헤더 없음: 서버는 거부 MUST)",
    _PARAM_GW,
    needs="modern",
)
def param_header_missing(c: Context) -> None:
    tool, _, arguments = _param_case(c)
    tweaks = p.Tweaks(omit_param_headers=True)
    _reject(
        c,
        c.modern().call_tool(tool["name"], arguments, tool=tool, tweaks=tweaks)[-1],
        400,
        p.ERR_HEADER_MISMATCH,
    )


@scenario(
    "param-header-mismatch",
    "modern-violation",
    "Mcp-Param-* 값이 바디 인자와 다름 -> 400 + -32020",
    "transport(헤더와 바디 값 불일치는 400 + -32020)",
    _PARAM_GW,
    needs="modern",
)
def param_header_mismatch(c: Context) -> None:
    tool, param, arguments = _param_case(c)
    wrong = {"string": "mismatch-value", "integer": "43", "boolean": "false"}[param.type]
    tweaks = p.Tweaks(param_headers=[(param.header_name, wrong)])
    _reject(
        c,
        c.modern().call_tool(tool["name"], arguments, tool=tool, tweaks=tweaks)[-1],
        400,
        p.ERR_HEADER_MISMATCH,
    )


@scenario(
    "param-header-invalid-chars",
    "modern-violation",
    "Mcp-Param-* 값에 인코딩하지 않은 비ASCII 바이트 -> 400 + -32020",
    "transport(헤더 값에 잘못된 문자는 400 + -32020)",
    _PARAM_GW,
    needs="modern",
)
def param_header_invalid_chars(c: Context) -> None:
    tool, param, arguments = _param_case(c, prefer="string")
    if param.type == "string":
        p.set_path(arguments, param.path, "café")
    tweaks = p.Tweaks(param_headers=[(param.header_name, "café".encode("utf-8"))])
    _reject(
        c,
        c.modern().call_tool(tool["name"], arguments, tool=tool, tweaks=tweaks)[-1],
        400,
        p.ERR_HEADER_MISMATCH,
    )


def _removed_method(
    name: str, method: str, params: dict[str, Any] | None, expect: str, gateway: Gateway
):
    @scenario(
        name,
        "modern-violation",
        expect,
        "errors(-32601은 HTTP 404), changelog(삭제된 메서드)",
        gateway,
        needs="modern",
    )
    def run(c: Context) -> None:
        _reject(c, c.modern().request(method, params), 404, p.ERR_METHOD_NOT_FOUND)

    return run


_removed_method(
    "removed-method-initialize",
    "initialize",
    {"protocolVersion": p.MODERN_VERSION, "capabilities": {}, "clientInfo": dict(p.CLIENT_INFO)},
    "모던 헤더·_meta를 갖춘 initialize(2026에서 삭제) -> 404 + -32601",
    Gateway(
        "unknown",
        "게이트웨이 기대값 미확정: 모던 헤더가 붙은 initialize를 레거시 initialize로 처리할 가능성이 있음",
    ),
)
_removed_method(
    "removed-method-ping",
    "ping",
    None,
    "모던 ping(2026에서 삭제) -> 404 + -32601",
    cond("게이트웨이가 지원하는 모던 메서드가 아니면 404 + -32601"),
)
_removed_method(
    "unknown-method",
    "unit-expert/unknown",
    None,
    "모르는 메서드 -> 404 + -32601",
    cond("게이트웨이가 지원하는 모던 메서드가 아니면 404 + -32601"),
)

_GET_GW = Gateway(
    "unknown",
    "게이트웨이 기대값 미확정: GET/DELETE는 레거시 세션 검증을 먼저 거칠 수 있고(세션 없으면 400), "
    "모던 클라이언트의 GET/DELETE에 대한 동작은 확인하지 못함",
)


@scenario(
    "get-405",
    "modern-violation",
    "모던 전용 서버의 MCP 엔드포인트에 GET -> 405 (SHOULD)",
    "transport#backward-compatibility(이 리비전만 지원하는 서버는 GET/DELETE에 405 SHOULD)",
    _GET_GW,
    needs="modern-only",
)
def get_405(c: Context) -> None:
    headers = [("Accept", "text/event-stream"), (p.H_VERSION, p.MODERN_VERSION)]
    c.expect_status(c.target.send("GET", headers, None, label="GET /mcp"), 405, level="should")


@scenario(
    "delete-405",
    "modern-violation",
    "모던 전용 서버의 MCP 엔드포인트에 DELETE -> 405 (SHOULD)",
    "transport#backward-compatibility(이 리비전만 지원하는 서버는 GET/DELETE에 405 SHOULD)",
    _GET_GW,
    needs="modern-only",
)
def delete_405(c: Context) -> None:
    headers = [(p.H_VERSION, p.MODERN_VERSION), (p.H_SESSION, UNKNOWN_SESSION)]
    c.expect_status(
        c.target.send("DELETE", headers, None, label="DELETE /mcp"), 405, level="should"
    )


@scenario(
    "session-id-ignored",
    "modern-violation",
    "모던 요청에 Mcp-Session-Id를 실어도 무시하고 정상 처리, 세션 ID를 되돌려주지 않음 (SHOULD)",
    "transport#backward-compatibility(Mcp-Session-Id 무시, 발급·반환 금지 SHOULD)",
    Gateway("unknown", "모던 요청에 실린 Mcp-Session-Id를 어떻게 다루는지 확인하지 못함"),
    needs="modern",
)
def session_id_ignored(c: Context) -> None:
    exchange = c.modern().list_tools(tweaks=p.Tweaks(session_id=UNKNOWN_SESSION))
    c.check(
        "세션 ID를 무시하고 처리",
        exchange.status == 200 and exchange.result is not None,
        "HTTP 200 + result",
        c.describe(exchange),
        level="should",
    )
    c.check(
        "응답에 Mcp-Session-Id 없음",
        exchange.header(p.H_SESSION) is None,
        "헤더 없음",
        repr(exchange.header(p.H_SESSION)),
        level="should",
    )


# ============================================================================
# legacy: 2025-xx initialize handshake + session
# ============================================================================
def _legacy_ready(c: Context, **kwargs: Any):
    client = c.legacy(**kwargs)
    init = client.initialize()
    if init.status != 200 or init.result is None:
        c.check("initialize", False, "HTTP 200 + result", c.describe(init))
        raise Stop
    return client, init


def _cleanup(client: Any) -> None:
    if client.session_id:
        client.delete_session(label="DELETE session (정리)")


@scenario(
    "legacy-initialize",
    "legacy",
    "initialize 200, protocolVersion이 레거시 버전(2026-07-28 아님), capabilities·serverInfo; "
    "notifications/initialized에 202",
    "legacy lifecycle(Initialization), transports(알림에는 202 Accepted)",
    Gateway(
        "same",
        "게이트웨이 코드 조사로 확인: initialize 결과의 기본 버전은 2025-06-18",
    ),
    needs="legacy",
)
def legacy_initialize(c: Context) -> None:
    client = c.legacy()
    init = client.initialize()
    result = c.expect_result(init, modern=False)
    version = result.get("protocolVersion")
    c.check(
        "protocolVersion이 레거시 버전",
        version in p.LEGACY_VERSIONS,
        f"{list(p.LEGACY_VERSIONS)} 중",
        repr(version),
    )
    c.check(
        "capabilities 객체",
        isinstance(result.get("capabilities"), dict),
        "object",
        repr(result.get("capabilities")),
    )
    info = result.get("serverInfo")
    c.check(
        "serverInfo.name",
        isinstance(info, dict) and isinstance(info.get("name"), str),
        "문자열",
        repr(info),
    )
    notified = client.initialized_exchange
    if notified is not None:
        c.expect_status(notified, 202)
    c.note(
        f"Mcp-Session-Id {'발급됨' if client.session_id else '발급 안 됨(선택 사항)'}, 협상 버전 {version}"
    )
    _cleanup(client)


@scenario(
    "legacy-version-negotiation",
    "legacy",
    "요청 버전(2025-03-26, 2025-06-18, 2025-11-25, 모르는 버전)마다 서버가 자신이 지원하는 레거시 버전으로 응답. "
    "게이트웨이는 항상 2025-06-18",
    "legacy lifecycle#version-negotiation(지원하면 같은 버전, 아니면 서버가 지원하는 다른 버전)",
    Gateway(
        "diff",
        "게이트웨이 코드 조사로 확인: 협상 버전은 항상 2025-06-18. 모르는 버전 문자열에 대한 응답 코드는 미확정",
    ),
    needs="legacy",
)
def legacy_version_negotiation(c: Context) -> None:
    table = []
    for requested in ("2025-03-26", "2025-06-18", "2025-11-25", BOGUS_VERSION):
        client = c.legacy()
        init = client.initialize(requested, send_initialized=False)
        init.label = f"initialize ({requested})"
        negotiated = (init.result or {}).get("protocolVersion")
        table.append(
            f"{requested} -> {negotiated if init.result is not None else c.describe(init)}"
        )
        if c.gateway and requested == BOGUS_VERSION:
            c.note(
                f"모르는 버전 {requested}: {c.describe(init)} (게이트웨이 기대값 미확정, 비교하지 않음)"
            )
        elif c.gateway:
            c.check(
                f"{requested}: 협상 버전",
                init.status == 200 and negotiated == "2025-06-18",
                "'2025-06-18'",
                repr(negotiated) if init.result is not None else c.describe(init),
            )
        else:
            c.check(
                f"{requested}: 서버가 지원하는 레거시 버전으로 응답",
                init.status == 200 and negotiated in p.LEGACY_VERSIONS,
                f"{list(p.LEGACY_VERSIONS)} 중",
                repr(negotiated) if init.result is not None else c.describe(init),
            )
        _cleanup(client)
    c.note("요청 -> 협상: " + "; ".join(table))


@scenario(
    "legacy-tools-list",
    "legacy",
    "세션 ID와 협상 버전 헤더를 실은 tools/list -> 200, tools 배열",
    "legacy transports(Session Management, Protocol Version Header)",
    Gateway(
        "same", "게이트웨이 코드 조사로 확인: 세션 검증 뒤 tools/list를 처리함"
    ),
    needs="legacy",
)
def legacy_tools_list(c: Context) -> None:
    client, _ = _legacy_ready(c)
    exchange = client.request("tools/list")
    result = c.expect_result(exchange, modern=False)
    c.check(
        "tools 배열",
        isinstance(result.get("tools"), list),
        "array",
        type(result.get("tools")).__name__,
    )
    _cleanup(client)


@scenario(
    "legacy-tools-call-ok",
    "legacy",
    "initialize -> tools/list -> tools/call: 200, isError 아님",
    "legacy server/tools",
    Gateway("same", "게이트웨이 코드 조사로 확인: tools/call을 업스트림으로 라우팅함"),
    needs="legacy",
)
def legacy_tools_call_ok(c: Context) -> None:
    client, _ = _legacy_ready(c)
    listed = client.request("tools/list")
    tools = [tool for tool in (listed.result or {}).get("tools") or [] if isinstance(tool, dict)]
    if not tools:
        c.skip(f"tools/list에서 툴을 얻지 못함 ({c.describe(listed)})")
    tool, arguments = c.plain_tool(tools)
    exchange = client.request("tools/call", {"name": tool["name"], "arguments": arguments})
    result = c.expect_result(exchange, modern=False)
    c.check(
        "isError가 true가 아님",
        result.get("isError") is not True,
        "false 또는 없음",
        repr(result.get("isError")),
    )
    c.note(f"호출한 툴: {tool['name']} {arguments}")
    _cleanup(client)


@scenario(
    "legacy-no-session",
    "legacy",
    "세션 ID를 발급한 서버에 Mcp-Session-Id 없이 요청 -> 400 (SHOULD). 게이트웨이는 400 + -32600",
    "legacy transports#session-management(세션이 필요한 서버는 400 SHOULD)",
    Gateway(
        "diff",
        "게이트웨이 코드 조사로 확인: 세션 ID 헤더 누락은 400 + -32600",
    ),
    needs="legacy",
)
def legacy_no_session(c: Context) -> None:
    client, _ = _legacy_ready(c, cfg=SESSION_CFG)
    if not client.session_id:
        c.skip("서버가 Mcp-Session-Id를 발급하지 않음")
    exchange = client.request("tools/list", session=None, label="tools/list (세션 ID 없음)")
    c.expect_status(exchange, 400, level=c.pick("should", "must"))
    if c.gateway:
        c.expect_code(exchange, -32600)
    _cleanup(client)


@scenario(
    "legacy-unknown-session-404",
    "legacy",
    "모르는(만료된) Mcp-Session-Id -> 404. 게이트웨이는 404 + -32600",
    "legacy transports#session-management(종료된 세션 ID에는 404 MUST. 발급한 적 없는 ID는 명시가 없어 SHOULD로 비교)",
    Gateway(
        "diff",
        "게이트웨이 코드 조사로 확인: 모르는 세션은 404 + -32600",
    ),
    needs="legacy",
)
def legacy_unknown_session(c: Context) -> None:
    client, _ = _legacy_ready(c, cfg=SESSION_CFG)
    real_session = client.session_id
    exchange = client.request(
        "tools/list", session=UNKNOWN_SESSION, label="tools/list (모르는 세션 ID)"
    )
    c.expect_status(exchange, 404, level=c.pick("should", "must"))
    if c.gateway:
        c.expect_code(exchange, -32600)
    client.session_id = real_session
    _cleanup(client)


@scenario(
    "legacy-session-expire-reconnect",
    "legacy",
    "세션 404를 받으면 클라이언트가 initialize를 다시 해 새 세션으로 같은 요청을 재시도 -> 200",
    "legacy transports#session-management(404를 받은 클라이언트는 새 InitializeRequest로 새 세션 시작 MUST)",
    Gateway(
        "same", "게이트웨이 코드 조사로 확인: 모르는 세션 404 뒤 initialize 재수행"
    ),
    needs="legacy",
)
def legacy_session_expire_reconnect(c: Context) -> None:
    client, _ = _legacy_ready(c, cfg=SESSION_CFG, reinit_on_404=True)
    stale = client.session_id
    client.session_id = UNKNOWN_SESSION  # stand-in for an expired session
    exchange = client.request("tools/list")
    if client.reinitialized == 0:
        client.session_id = stale
        _cleanup(client)
        c.skip(
            f"대상이 모르는 세션에 404를 주지 않아({c.describe(exchange)}) 재연결 경로를 검증할 수 없음"
        )
    c.expect_result(exchange, modern=False)
    c.check(
        "새 세션으로 교체",
        client.session_id != UNKNOWN_SESSION,
        "새 세션 ID",
        repr(client.session_id),
    )
    _cleanup(client)


@scenario(
    "legacy-delete-session",
    "legacy",
    "DELETE로 세션 종료: 2xx면 이후 그 세션 ID 요청은 404(MUST). 405면 서버가 클라이언트 종료를 허용하지 않는 것(MAY)",
    "legacy transports#session-management(DELETE, 405 MAY, 종료 후 404 MUST)",
    Gateway(
        "same",
        "게이트웨이 코드 조사로 확인: DELETE로 세션을 종료함",
    ),
    needs="legacy",
)
def legacy_delete_session(c: Context) -> None:
    client, _ = _legacy_ready(c, cfg=SESSION_CFG)
    if not client.session_id:
        c.skip("서버가 Mcp-Session-Id를 발급하지 않음")
    deleted = client.delete_session()
    allowed = (200, 202, 204) if c.gateway else (200, 202, 204, 405)
    if not c.expect_status(deleted, allowed):
        return
    if deleted.status == 405:
        c.note("DELETE에 405: 서버가 클라이언트의 세션 종료를 허용하지 않음(MAY)")
        return
    after = client.request("tools/list", label="tools/list (종료된 세션)")
    c.expect_status(after, 404)


# ============================================================================
# auth
# ============================================================================
def _auth_cfg(c: Context, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Against the unit-expert server the scenario switches OAuth on via ?cfg=."""
    if c.gateway:
        return None
    return deep_merge(AUTH_CFG, extra) if extra and c.options.inject else AUTH_CFG


def _any_request(
    c: Context, *, bearer: str | None, label: str, extra: dict[str, Any] | None = None
) -> Exchange:
    cfg = _auth_cfg(c, extra)
    if c.supports("modern"):
        return c.modern().list_tools(bearer=bearer, cfg=cfg, label=label)
    return c.legacy().initialize(send_initialized=False, bearer=bearer, cfg=cfg)


def _skip_if_open(c: Context, exchange: Exchange) -> None:
    """A target that is not the unit-expert server (no ?cfg=) and answers 200
    without a token simply has no authorization; there is nothing to compare."""
    if not c.gateway and exchange.status == 200 and c.options.inject and not c.injectable():
        c.skip("대상이 인증을 요구하지 않음(?cfg=로 OAuth를 켤 수 없는 서버)")


@scenario(
    "auth-no-token-401",
    "auth",
    '토큰 없이 요청 -> 401 + WWW-Authenticate: Bearer resource_metadata="...", scope="..."(SHOULD). '
    "resource_metadata 문서에 authorization_servers 1개 이상",
    "authz(401, WWW-Authenticate 또는 well-known 중 하나 MUST, scope SHOULD, RFC 9728 authorization_servers MUST)",
    Gateway(
        "diff",
        "게이트웨이 코드 조사로 확인: 401 + Bearer resource_metadata만 넣고 scope는 넣지 않음",
    ),
)
def auth_no_token(c: Context) -> None:
    exchange = _any_request(c, bearer=None, label="토큰 없는 요청")
    _skip_if_open(c, exchange)
    if not c.expect_status(exchange, 401):
        return
    challenge = p.parse_www_authenticate(exchange.header("WWW-Authenticate"))
    params = challenge["params"]
    origin = origin_of(c.target.url)
    metadata_url = params.get("resource_metadata")
    c.check(
        "WWW-Authenticate 스킴",
        (challenge["scheme"] or "").lower() == "bearer",
        "Bearer",
        repr(challenge["scheme"]),
        level="should",
    )
    if c.gateway:
        c.check("resource_metadata 파라미터", bool(metadata_url), "URL", repr(metadata_url))
        c.note(
            f"scope 파라미터: {params.get('scope')!r} (게이트웨이는 넣지 않는 것으로 확인됨, 비교하지 않음)"
        )
    else:
        c.check(
            "scope 파라미터",
            bool(params.get("scope")),
            'scope="..."',
            repr(params.get("scope")),
            level="should",
        )
        if not params.get("scope") and not c.supports("modern"):
            c.note(
                "레거시 경로의 401: 단위전문가 서버는 레거시 경로의 WWW-Authenticate를 이전 "
                "그대로 둔다(docs/CONFIG_REFERENCE.md). scope는 2025-11-25 인가 문서에서도 SHOULD다"
            )
    if not metadata_url:
        metadata_url = f"{origin}/.well-known/oauth-protected-resource"
        c.note("resource_metadata가 없어 well-known URI로 확인")
    if origin_of(metadata_url) != origin:
        c.note(f"resource_metadata가 다른 origin({origin_of(metadata_url)})이라 조회하지 않음")
        return
    document = c.target.send(
        "GET",
        [("Accept", "application/json")],
        None,
        label="protected resource metadata",
        url=metadata_url,
        bearer=None,
    )
    body = document.message if isinstance(document.message, dict) else {}
    servers = body.get("authorization_servers")
    c.check("resource metadata 조회", document.status == 200, "HTTP 200", c.describe(document))
    c.check(
        "authorization_servers 1개 이상",
        isinstance(servers, list) and bool(servers),
        "비어 있지 않은 배열",
        repr(servers),
    )


@scenario(
    "auth-bad-token",
    "auth",
    "유효하지 않은 토큰 -> 401 (MUST), WWW-Authenticate 포함(SHOULD)",
    "authz(무효·만료 토큰은 401 MUST)",
    Gateway(
        "same",
        "게이트웨이 코드 조사로 확인: 무효 토큰(invalid_token)은 401",
    ),
)
def auth_bad_token(c: Context) -> None:
    exchange = _any_request(c, bearer="unit-expert-client.invalid.token", label="무효 토큰 요청")
    _skip_if_open(c, exchange)
    c.expect_status(exchange, 401)
    c.check(
        "WWW-Authenticate 헤더",
        exchange.header("WWW-Authenticate") is not None,
        "헤더 있음",
        "없음",
        level="should",
    )


@scenario(
    "auth-insufficient-scope-403",
    "auth",
    '필요한 스코프가 없는 토큰 -> 403 + WWW-Authenticate: Bearer error="insufficient_scope", scope="..." (SHOULD)',
    "authz(스코프 부족은 403 + insufficient_scope SHOULD)",
    Gateway(
        "n/a", "스코프가 부족한 토큰을 게이트웨이용으로 만들 방법이 없음(로컬 서버의 mock AS 한정)"
    ),
)
def auth_insufficient_scope(c: Context) -> None:
    flow = oauth.run_flow(c.target, scope="unit-expert-client-unrelated")
    if not flow.access_token:
        c.skip(f"스코프가 다른 토큰을 발급받지 못함: {flow.error}")
    exchange = _any_request(
        c, bearer=flow.access_token, label="스코프 부족 토큰 요청", extra=SCOPE_CFG
    )
    c.expect_status(exchange, 403, level="should")
    params = p.parse_www_authenticate(exchange.header("WWW-Authenticate"))["params"]
    c.check(
        "error=insufficient_scope",
        params.get("error") == "insufficient_scope",
        "insufficient_scope",
        repr(params.get("error")),
        level="should",
    )
    c.check(
        "scope 파라미터",
        bool(params.get("scope")),
        'scope="..."',
        repr(params.get("scope")),
        level="should",
    )


@scenario(
    "auth-oauth-flow",
    "auth",
    "resource metadata -> AS metadata -> 등록 -> authorize -> token -> 토큰으로 호출 200 -> refresh -> 새 토큰으로 호출 200",
    "authz, authorization-server-discovery; server.py의 mock AS(/register, /authorize, /token)",
    Gateway("n/a", "로컬 서버의 자체 인증 서버 전용 흐름(authorize에 action=approve를 직접 POST)"),
)
def auth_oauth_flow(c: Context) -> None:
    flow = oauth.run_flow(c.target)
    metadata = flow.steps.get("resource-metadata")
    if metadata is not None and metadata.status == 404 and c.options.inject and not c.injectable():
        c.skip(
            "대상에 protected resource metadata가 없음(자체 인증 서버가 있는 단위전문가 서버 전용)"
        )
    for name, expected in (
        ("resource-metadata", 200),
        ("server-metadata", 200),
        ("register", (200, 201)),
        ("authorize", 302),
        ("token", 200),
    ):
        if name in flow.steps:
            c.expect_status(flow.steps[name], expected)
    if not flow.access_token:
        c.check("access_token 발급", False, "access_token", str(flow.error))
        return
    c.check(
        "token_type",
        str(flow.tokens.get("token_type", "")).lower() == "bearer",
        "Bearer",
        repr(flow.tokens.get("token_type")),
    )
    c.expect_status(_any_request(c, bearer=flow.access_token, label="발급 토큰으로 요청"), 200)
    refreshed = oauth.refresh(c.target, flow)
    if refreshed is None:
        c.check("refresh_token 발급", False, "refresh_token", "없음", level="should")
        return
    c.expect_status(refreshed, 200)
    new_token = flow.refreshed.get("access_token")
    c.check(
        "refresh로 새 access_token",
        isinstance(new_token, str) and bool(new_token),
        "access_token",
        "없음",
    )
    if isinstance(new_token, str):
        c.expect_status(_any_request(c, bearer=new_token, label="갱신 토큰으로 요청"), 200)
    refresh_as_access = flow.tokens.get("refresh_token")
    if isinstance(refresh_as_access, str):
        c.expect_status(
            _any_request(c, bearer=refresh_as_access, label="refresh 토큰을 access 토큰처럼 사용"),
            401,
        )


# ============================================================================
# compat: compatibility matrix (basic/versioning#compatibility-matrix)
# ============================================================================
_COMPAT_GW = Gateway("same", "기대값이 probe 판정에 따라 정해져 게이트웨이 전용 기대값이 없음")


@scenario(
    "compat-probe",
    "compat",
    "HTTP 폴백 절차로 대상이 modern/legacy/dual 중 무엇인지 판정됨(--expect-era를 주면 그 값과 일치)",
    "versioning(HTTP 폴백: 모던 먼저, 400이면 바디 확인, 모던 에러가 아니면 initialize)",
    _COMPAT_GW,
)
def compat_probe(c: Context) -> None:
    verdict = c.era()
    c.result.exchanges.extend(
        exchange for exchange in verdict.exchanges if exchange not in c.result.exchanges
    )
    for step in verdict.steps:
        c.note(step)
    c.check(
        "판정",
        verdict.verdict in ("modern", "legacy", "dual"),
        "modern | legacy | dual",
        verdict.verdict,
    )
    if c.options.expect_era:
        c.check(
            "--expect-era와 일치",
            verdict.verdict == c.options.expect_era,
            c.options.expect_era,
            verdict.verdict,
        )


@scenario(
    "compat-modern-client",
    "compat",
    "모던 클라이언트: 모던/듀얼 대상에서는 tools/list 성공, 레거시 대상에서는 DiscoverResult를 얻지 못함",
    "versioning#compatibility-matrix(모던->레거시는 실패)",
    _COMPAT_GW,
)
def compat_modern_client(c: Context) -> None:
    if c.supports("modern") and c.era().verdict != "legacy":
        c.expect_result(c.modern().list_tools())
        return
    exchange = c.modern().discover()
    is_discover = exchange.status == 200 and isinstance(
        (exchange.result or {}).get("supportedVersions"), list
    )
    c.check(
        "레거시 대상은 DiscoverResult를 주지 않음",
        not is_discover,
        "에러 또는 무응답",
        c.describe(exchange),
    )
    c.note(f"레거시 대상의 모던 요청 응답: {c.describe(exchange)}")


@scenario(
    "compat-legacy-client",
    "compat",
    "레거시 클라이언트: 레거시/듀얼 대상에서는 initialize + tools/list 성공, 모던 전용 대상에서는 initialize가 400으로 "
    "거부되고 에러에 지원 버전이 적힘(SHOULD)",
    "versioning#compatibility-matrix(레거시->모던은 필수 헤더 누락으로 400, 모던 전용 서버는 에러에 지원 버전 SHOULD)",
    _COMPAT_GW,
)
def compat_legacy_client(c: Context) -> None:
    client = c.legacy()
    init = client.initialize()
    if c.supports("legacy") and c.era().verdict != "modern":
        c.expect_result(init, modern=False)
        if init.result is not None:
            c.expect_result(client.request("tools/list"), modern=False)
            _cleanup(client)
        return
    c.expect_status(init, 400)
    c.check(
        "에러에 지원 버전 표기",
        p.MODERN_VERSION in init.text,
        f"본문에 {p.MODERN_VERSION}",
        init.text[:200],
        level="should",
    )


@scenario(
    "compat-dual-client",
    "compat",
    "듀얼 클라이언트: 모던이 되면 모던으로, 안 되면 initialize로 폴백해 tools/list까지 성공",
    "versioning(듀얼 클라이언트 폴백 절차)",
    _COMPAT_GW,
)
def compat_dual_client(c: Context) -> None:
    choice = c.era().dual_client_choice
    c.note(f"듀얼 클라이언트의 선택: {choice}")
    if choice == "modern":
        c.expect_result(c.modern().list_tools())
    elif choice == "legacy":
        client, _ = _legacy_ready(c)
        c.expect_result(client.request("tools/list"), modern=False)
        _cleanup(client)
    else:
        c.check("모던 또는 레거시로 연결", False, "modern | legacy", f"probe: {c.era().verdict}")


# ============================================================================
# injection: the unit-expert server's fault-injection options (?cfg=), and how a
# client has to judge what comes back. Direct to the server only.
# ============================================================================
_INJECT_GW = Gateway(
    "n/a",
    "단위전문가 서버의 ?cfg= 주입 옵션을 시나리오마다 바꿔 넣는다. 게이트웨이를 거치면 서버 설정은 "
    "게이트웨이에 등록한 URL로 정해지므로 대상이 아님",
)
_SHORT_TIMEOUT = 0.5  # client read timeout for the "server stays silent" cases
_SILENT_SECONDS = 2.0  # how long the server stays silent in those cases


def _sent_initialize(verdict: ProbeResult) -> bool:
    return any(
        (exchange.request_json() or {}).get("method") == "initialize"
        for exchange in verdict.exchanges
    )


def _check_dual_flow(c: Context, label: str, verdict: ProbeResult, expect: str | None) -> None:
    """Compare one dual-client probe with the fallback procedure.

    expect: "modern" (stays modern), "legacy" (falls back to initialize),
    "auth" (401/403: no verdict, no fallback), None (stops with an error).
    """
    last = verdict.steps[-1] if verdict.steps else ""
    sent = _sent_initialize(verdict)
    if expect == "auth":
        c.check(
            f"{label}: 인증 요구로 판정 중단",
            verdict.verdict == "auth-required" and not sent,
            "auth-required, initialize 보내지 않음",
            f"{verdict.verdict}, initialize {'보냄' if sent else '안 보냄'}",
        )
        return
    fallback = expect == "legacy"
    c.check(
        f"{label}: initialize 폴백 여부",
        sent == fallback,
        "initialize로 폴백" if fallback else "initialize를 보내지 않음",
        f"initialize {'보냄' if sent else '안 보냄'} ({last})",
    )
    want = expect
    if fallback and not c.supports("legacy"):
        want = None  # the target refuses initialize, so the fallback cannot succeed
    c.check(
        f"{label}: 듀얼 클라이언트의 선택",
        verdict.dual_client_choice == want,
        repr(want),
        f"{verdict.dual_client_choice!r} ({last})",
    )


# discover.mode -> what a dual client does (None: stops with an error).
_DISCOVER_FLOW: tuple[tuple[str, str | None, str], ...] = (
    ("normal", "modern", "DiscoverResult"),
    (
        "no-modern-version",
        "legacy",
        "supportedVersions에 레거시 버전만: 상호 지원 버전으로 initialize",
    ),
    ("empty-versions", None, "supportedVersions가 비어 호환 버전 없음: 폴백 없이 에러"),
    ("missing-versions", "legacy", "DiscoverResult가 아님"),
    ("json-rpc-error", "legacy", "200 + 모던 에러가 아닌 JSON-RPC 에러"),
    ("header-mismatch", "modern", "400 + -32020: 재시도, initialize 없음"),
    ("unsupported-version", None, "400 + -32022, supported에 아는 버전 없음: 폴백 없이 에러"),
    ("http-401", "auth", "401"),
    ("http-403", "auth", "403"),
    ("http-5xx", "legacy", "5xx, 모던 에러 바디 없음"),
    ("sse", "modern", "SSE로 온 DiscoverResult"),
    ("sse-no-response", "legacy", "SSE가 응답 없이 끝남"),
    ("id-mismatch", "legacy", "다른 id의 응답은 응답으로 인정하지 않음"),
    ("invalid-json", "legacy", "해석할 수 없는 바디"),
    ("disconnect", "legacy", "응답 없이 연결 종료"),
)


@scenario(
    "inject-discover-modes",
    "injection",
    "discover.mode 각 값에 대해 듀얼 클라이언트 폴백 절차의 결과: 모던 에러(400 + -32020/-32022)면 재시도하고 "
    "initialize로 가지 않음, DiscoverResult가 아니거나 응답이 없거나 타임아웃이면 initialize로 폴백, "
    "401/403이면 판정 중단",
    "transport#backward-compatibility, versioning(모던 에러면 재시도, 그 외는 레거시로 판단. 폴백을 특정 에러 코드에 "
    "묶지 않음. supported에서 상호 지원 버전을 고르고 없으면 에러)",
    _INJECT_GW,
    needs="modern",
    injects=True,
)
def inject_discover_modes(c: Context) -> None:
    for mode, expect, why in _DISCOVER_FLOW:
        verdict = probe(c.configured({"discover": {"mode": mode}}), check_legacy=False)
        _check_dual_flow(c, f"discover.mode={mode}", verdict, expect)
        c.note(f"discover.mode={mode}: {why} -> {verdict.steps[-1] if verdict.steps else ''}")
        if mode == "header-mismatch":
            discovers = [
                exchange
                for exchange in verdict.exchanges
                if (exchange.request_json() or {}).get("method") == "server/discover"
            ]
            c.check(
                "discover.mode=header-mismatch: 모던 요청을 다시 보냄",
                len(discovers) == 2,
                "server/discover 2회",
                f"{len(discovers)}회",
            )
    slow = c.configured({"discover": {"delaySeconds": _SILENT_SECONDS}}, timeout=_SHORT_TIMEOUT)
    verdict = probe(slow, check_legacy=False)
    _check_dual_flow(c, "discover.delaySeconds(타임아웃)", verdict, "legacy")


_LEGACY_PROBE_MODES = (
    "method-not-found",
    "invalid-params",
    "http-400",
    "http-404",
    "empty-400",
    "timeout",
)


@scenario(
    "inject-legacy-probe-modes",
    "injection",
    "serverEra=legacy + legacyModernProbe.mode 각 값: 모던 요청의 응답이 모던 에러가 아니므로 어느 모드에서든 "
    "initialize로 폴백해 legacy로 판정",
    "transport#backward-compatibility(빈 바디나 모던 에러가 아닌 바디면 initialize로 폴백), "
    "versioning#compatibility-matrix(듀얼 -> 레거시는 동작)",
    _INJECT_GW,
    injects=True,
)
def inject_legacy_probe_modes(c: Context) -> None:
    for mode in _LEGACY_PROBE_MODES:
        cfg: dict[str, Any] = {"serverEra": "legacy", "legacyModernProbe": {"mode": mode}}
        timeout = None
        if mode == "timeout":
            cfg["legacyModernProbe"]["delaySeconds"] = _SILENT_SECONDS
            timeout = _SHORT_TIMEOUT
        verdict = probe(c.configured(cfg, timeout), check_legacy=False)
        label = f"legacyModernProbe.mode={mode}"
        first = verdict.exchanges[0]
        c.check(
            f"{label}: 모던 요청의 응답이 모던 에러가 아님",
            not (first.status == 400 and first.rpc_code in p.MODERN_ERROR_CODES)
            and not isinstance((first.result or {}).get("supportedVersions"), list),
            "DiscoverResult도 모던 에러도 아님",
            c.describe(first),
        )
        sent = _sent_initialize(verdict)
        c.check(
            f"{label}: initialize로 폴백",
            sent and verdict.fell_back,
            "initialize 보냄",
            f"initialize {'보냄' if sent else '안 보냄'}",
        )
        c.check(
            f"{label}: 판정",
            (verdict.verdict, verdict.dual_client_choice) == ("legacy", "legacy"),
            "legacy",
            f"{verdict.verdict} (선택 {verdict.dual_client_choice!r})",
        )
        c.note(f"{label}: 모던 요청 응답 {c.describe(first)}")


def _injected_call(c: Context, cfg: dict[str, Any], label: str) -> Exchange:
    """One tools/call of a plain tool under ``cfg``: modern when the target speaks
    modern, else through a legacy session."""
    if c.supports("modern"):
        tool, arguments = c.plain_tool(c.tools().tools or [])
        return c.modern(cfg=cfg).call_tool(tool["name"], arguments, tool=tool, label=label)[-1]
    client = c.legacy(cfg=cfg)
    init = client.initialize()
    if init.status != 200 or init.result is None:
        c.check("initialize", False, "HTTP 200 + result", c.describe(init))
        raise Stop
    listed = client.request("tools/list")
    tools = [tool for tool in (listed.result or {}).get("tools") or [] if isinstance(tool, dict)]
    tool, arguments = c.plain_tool(tools)
    return client.request("tools/call", {"name": tool["name"], "arguments": arguments}, label=label)


@scenario(
    "inject-tool-call-modes",
    "injection",
    "toolCall.mode 각 값에서 클라이언트가 관찰한 결과: is-error는 isError 결과, json-rpc-error는 JSON-RPC 에러, "
    "http-status는 JSON-RPC 바디 없는 HTTP 오류, sse는 알림 뒤 최종 응답, id-mismatch는 응답으로 인정하지 않음, "
    "empty는 빈 바디, disconnect는 전송 실패",
    "tools#error-handling(isError와 프로토콜 에러 구분), transport(application/json 또는 text/event-stream 둘 다 지원 "
    "MUST), JSON-RPC 2.0(응답 id는 요청 id와 같아야 함)",
    _INJECT_GW,
    injects=True,
)
def inject_tool_call_modes(c: Context) -> None:
    def call(mode: str) -> Exchange:
        return _injected_call(c, {"toolCall": {"mode": mode}}, f"tools/call (toolCall.mode={mode})")

    exchange = call("normal")
    c.check(
        "normal: 결과",
        exchange.status == 200
        and exchange.result is not None
        and exchange.result.get("isError") is not True,
        "HTTP 200 + isError 아닌 result",
        c.describe(exchange),
    )
    exchange = call("is-error")
    c.check(
        "is-error: 프로토콜 에러가 아닌 isError 결과",
        exchange.status == 200 and (exchange.result or {}).get("isError") is True,
        "HTTP 200 + result.isError true",
        c.describe(exchange),
    )
    exchange = call("json-rpc-error")
    c.check(
        "json-rpc-error: JSON-RPC 에러(기본 -32603)",
        exchange.status == 200 and exchange.rpc_code == -32603,
        "HTTP 200 + code -32603",
        c.describe(exchange),
    )
    exchange = call("http-status")
    c.check(
        "http-status: JSON-RPC 메시지 없는 HTTP 오류(기본 500)",
        exchange.status == 500 and exchange.message is None,
        "HTTP 500, JSON-RPC 바디 없음",
        c.describe(exchange),
    )
    exchange = call("sse")
    c.check(
        "sse: text/event-stream에서 알림 뒤 최종 응답을 읽음",
        exchange.is_sse
        and len(exchange.notifications) == 2
        and exchange.result is not None
        and not exchange.id_mismatch,
        "SSE, 알림 2건(기본 sseNotificationCount) + 요청 id의 result",
        f"{c.describe(exchange)}, {exchange.content_type}, 알림 {len(exchange.notifications)}건",
    )
    exchange = call("id-mismatch")
    c.check(
        "id-mismatch: 다른 id의 응답을 이 요청의 응답으로 인정하지 않음",
        exchange.status == 200 and exchange.id_mismatch,
        "HTTP 200, 응답 id가 요청 id와 다름을 감지",
        f"{c.describe(exchange)}, 응답 id {(exchange.message or {}).get('id')!r}",
    )
    exchange = call("empty")
    c.check(
        "empty: 빈 바디라 JSON-RPC 응답 없음",
        exchange.status == 200 and not exchange.body and exchange.message is None,
        "HTTP 200, 바디 0바이트",
        f"{c.describe(exchange)}, {len(exchange.body)}바이트",
    )
    exchange = call("disconnect")
    c.check(
        "disconnect: 전송 실패로 보고",
        exchange.error is not None and exchange.status is None,
        "전송 실패",
        c.describe(exchange),
    )


@scenario(
    "inject-modern-response",
    "injection",
    "modernResponse.* 위반을 받은 클라이언트의 판정: resultType이 없으면 complete로 간주, 모르는 값은 무효, "
    "input_required는 tools/call 등에서만 유효하고 requestState를 그대로 되돌려 다른 id로 재요청, ttlMs가 없으면 0으로 "
    "간주, serverInfo 누락은 허용, 응답의 Mcp-Session-Id는 다음 요청에 싣지 않음",
    "basic/index#resulttype(absent는 complete MUST, 모르는 값은 invalid MUST), mrtr(허용 메서드, requestState 반환 "
    "MUST, 다른 id MUST), caching(ttlMs absent는 0 SHOULD), transport#backward-compatibility(세션 없음)",
    _INJECT_GW,
    needs="modern",
    injects=True,
)
def inject_modern_response(c: Context) -> None:
    tool, arguments = c.plain_tool(c.tools().tools or [])

    def response_cfg(**options: Any) -> dict[str, Any]:
        return {"modernResponse": options}

    # resultType absent -> "complete"
    client = c.modern(cfg=response_cfg(resultType="omit"))
    tools = client.load_tools()
    page = client.pages[0]
    c.check(
        "omit: 서버 응답에 resultType 없음(주입 확인)",
        page.result is not None and "resultType" not in page.result,
        "resultType 필드 없음",
        repr((page.result or {}).get("resultType")),
    )
    c.check(
        "omit: complete로 간주해 tools/list 결과를 사용",
        p.judge_result("tools/list", page.result) == ("complete", None) and bool(tools),
        "complete, 툴 1개 이상",
        f"{p.judge_result('tools/list', page.result)}, 툴 {len(tools)}개",
    )
    called = client.call_tool(tool["name"], arguments, tool=tool, label="tools/call (omit)")[-1]
    c.check(
        "omit: tools/call 결과도 complete로 간주",
        p.judge_result("tools/call", called.result) == ("complete", None),
        "complete",
        repr(p.judge_result("tools/call", called.result)),
    )

    # unknown value -> invalid
    client = c.modern(cfg=response_cfg(resultType="unknown"))
    tools = client.load_tools()
    page = client.pages[0]
    c.check(
        "unknown: 모르는 resultType은 무효, tools/list 결과를 쓰지 않음",
        p.judge_result("tools/list", page.result)[0] == p.RESULT_INVALID
        and not tools
        and bool(client.rejected),
        "invalid, 툴 0개",
        f"{p.judge_result('tools/list', page.result)}, 툴 {len(tools)}개",
    )
    called = client.call_tool(tool["name"], arguments, tool=tool, label="tools/call (unknown)")[-1]
    c.check(
        "unknown: tools/call 결과도 무효",
        p.judge_result("tools/call", called.result)[0] == p.RESULT_INVALID,
        "invalid",
        repr(p.judge_result("tools/call", called.result)),
    )

    # input_required where the spec does not allow it
    client = c.modern(cfg=response_cfg(resultType="input_required", methods=["tools/list"]))
    tools = client.load_tools()
    page = client.pages[0]
    c.check(
        "input_required(tools/list): 허용되지 않는 메서드라 무효",
        p.judge_result("tools/list", page.result)[0] == p.RESULT_INVALID and not tools,
        "invalid, 툴 0개",
        f"{p.judge_result('tools/list', page.result)}, 툴 {len(tools)}개",
    )

    # input_required on tools/call: echo requestState under a new id
    client = c.modern(cfg=response_cfg(resultType="input_required", methods=["tools/call"]))
    rounds = client.call_tool(
        tool["name"],
        arguments,
        tool=tool,
        auto_input=True,
        max_rounds=1,
        label="tools/call (input_required)",
    )
    first = rounds[0]
    state = (first.result or {}).get("requestState")
    c.check(
        "input_required(tools/call): 유효한 input_required로 인식",
        p.judge_result("tools/call", first.result) == ("input_required", None),
        "input_required",
        repr(p.judge_result("tools/call", first.result)),
    )
    if len(rounds) > 1:
        before, after = first.request_json() or {}, rounds[1].request_json() or {}
        sent_state = (after.get("params") or {}).get("requestState")
        c.check(
            "input_required(tools/call): requestState를 그대로 되돌리고 id를 바꿔 재요청",
            sent_state == state and state is not None and before.get("id") != after.get("id"),
            f"requestState {state!r}, 다른 id",
            f"requestState {sent_state!r}, id {before.get('id')!r} -> {after.get('id')!r}",
        )
    else:
        c.check("input_required(tools/call): 재요청", False, "재요청 1회", "재요청 없음")

    # serverInfo is SHOULD: its absence does not invalidate the result
    exchange = c.modern(cfg=response_cfg(omitServerInfo=True)).discover()
    meta = (exchange.result or {}).get("_meta")
    c.check(
        "omitServerInfo: serverInfo가 없어도 결과는 유효",
        exchange.result is not None
        and p.META_SERVER_INFO not in (meta if isinstance(meta, dict) else {})
        and p.judge_result("server/discover", exchange.result) == ("complete", None),
        "serverInfo 없음, complete",
        f"_meta {meta!r}, {p.judge_result('server/discover', exchange.result)}",
    )

    # cache fields absent -> ttl 0
    client = c.modern(cfg=response_cfg(omitCacheFields=True))
    tools = client.load_tools()
    page = client.pages[0]
    c.check(
        "omitCacheFields: ttlMs가 없으면 0(즉시 stale)으로 간주하고 결과는 사용",
        page.result is not None
        and "ttlMs" not in page.result
        and p.effective_ttl_ms(page.result) == 0
        and bool(tools),
        "ttlMs 없음 -> 0, 툴 1개 이상",
        f"ttlMs {(page.result or {}).get('ttlMs')!r}, 툴 {len(tools)}개",
    )

    # a session id on a modern response is not carried into the next request
    client = c.modern(cfg=response_cfg(issueSessionId=True))
    issued = client.list_tools(label="tools/list (issueSessionId)")
    follow = client.list_tools(label="tools/list (다음 요청)")
    c.check(
        "issueSessionId: 서버가 Mcp-Session-Id를 붙임(주입 확인)",
        issued.header(p.H_SESSION) is not None,
        "응답에 Mcp-Session-Id",
        repr(issued.header(p.H_SESSION)),
    )
    c.check(
        "issueSessionId: 다음 모던 요청에 Mcp-Session-Id를 싣지 않음",
        _sent_header(follow, p.H_SESSION) is None and follow.result is not None,
        "요청에 Mcp-Session-Id 없음, 정상 응답",
        f"{_sent_header(follow, p.H_SESSION)!r}, {c.describe(follow)}",
    )


@scenario(
    "inject-missing-capability",
    "injection",
    "modern.requiredClientCapabilities 주입: 선언하지 않으면 400 + -32021과 data.requiredCapabilities, "
    "그 capability를 선언해 다시 보내면 200. server/discover는 대상이 아니라 probe는 모던으로 판정",
    "meta·errors(-32021 MissingRequiredClientCapability, data.requiredCapabilities, HTTP 400 MUST), "
    "transport#backward-compatibility(모던 에러면 요청을 고쳐 재시도)",
    _INJECT_GW,
    needs="modern",
    injects=True,
)
def inject_missing_capability(c: Context) -> None:
    cfg = {"modern": {"requiredClientCapabilities": ["elicitation"]}}
    refused = c.modern(capabilities={}, cfg=cfg).list_tools(label="tools/list (capability 없음)")
    c.expect_error(refused, 400, p.ERR_MISSING_CAPABILITY)
    data = (refused.rpc_error or {}).get("data")
    required = data.get("requiredCapabilities") if isinstance(data, dict) else None
    c.check(
        "data.requiredCapabilities",
        isinstance(required, dict) and "elicitation" in required,
        "{'elicitation': ...}",
        repr(required),
    )
    declared = required if isinstance(required, dict) else {"elicitation": {}}
    retried = c.modern(capabilities=declared, cfg=cfg).list_tools(
        label="tools/list (capability 선언 후 재시도)"
    )
    c.expect_result(retried)
    verdict = probe(c.configured(cfg), check_legacy=False)
    c.check(
        "server/discover는 capability 없이도 응답",
        verdict.dual_client_choice == "modern",
        "modern",
        repr(verdict.dual_client_choice),
    )
