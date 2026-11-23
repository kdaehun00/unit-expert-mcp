"""Text and JSON output. Credentials are masked by transport.Exchange.summary/detail.

Text for people passes through escape_control, so bytes the target sent cannot
drive the terminal (colours, cursor moves, overwritten lines).
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from .clients import ProbeResult
from .runner import FAIL, PASS, SKIP, WARN, Scenario, ScenarioResult
from .transport import Exchange, mask_header_value, mask_json, mask_url

BODY_LIMIT = 2000


def width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def pad(text: str, size: int) -> str:
    return text + " " * max(0, size - width(text))


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def escape_control(text: str, keep_newlines: bool = False) -> str:
    """Make control characters (C0, DEL, C1) visible instead of sending them out.

    Tab is kept. Line feed is kept only with ``keep_newlines`` (multi-line
    bodies); in a one-line field it becomes ``\\n`` so the target cannot forge
    extra output lines. The Unicode line/paragraph separators are escaped too.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char == "\t" or (char == "\n" and keep_newlines):
            out.append(char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        elif code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def summary_line(exchange: Exchange) -> str:
    info = exchange.summary()
    if exchange.error:
        return escape_control(
            f"{info['request']} -> 전송 실패: {exchange.error} ({info['elapsedMs']}ms)"
        )
    parts = [f"HTTP {info['status']}"]
    if info["contentType"]:
        parts.append(info["contentType"])
    if info["rpcCode"] is not None:
        parts.append(f"code {info['rpcCode']} ({info['rpcMessage']})")
    if info["resultType"]:
        parts.append(f"resultType {info['resultType']}")
    if info.get("sseEvents"):
        parts.append(f"SSE {info['sseEvents']}건")
    if info.get("streamOpen"):
        parts.append("스트림이 열린 채 타임아웃")
    parts.append(f"{info['elapsedMs']}ms")
    label = f"[{exchange.label}] " if exchange.label else ""
    return escape_control(f"{label}{info['request']} -> " + ", ".join(parts))


def format_exchange(exchange: Exchange, verbose: bool = False) -> str:
    """Request/response of one call. Without -v the body is truncated and only
    protocol headers are shown."""
    lines = [f"[{exchange.label}]"] if exchange.label else []
    lines.append(f"> {exchange.method} {mask_url(exchange.url)}")
    for name, value in exchange.request_headers:
        lines.append(f"> {name}: {mask_header_value(name, value)}")
    detail = exchange.detail()
    if detail["requestBody"] is not None:
        body = detail["requestBody"]
        lines.append(
            "> " + (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
        )
    if exchange.error:
        lines.append(f"< 전송 실패: {exchange.error}")
        return "\n".join(escape_control(line) for line in lines)
    lines.append(f"< HTTP {exchange.status} {exchange.reason} ({exchange.elapsed_ms:.1f}ms)")
    shown = (
        exchange.headers
        if verbose
        else [
            (name, value)
            for name, value in exchange.headers
            if name.lower()
            in {
                "content-type",
                "mcp-session-id",
                "mcp-protocol-version",
                "www-authenticate",
                "allow",
                "location",
            }
        ]
    )
    for name, value in shown:
        lines.append(f"< {name}: {mask_header_value(name, value)}")
    # Everything so far is one-line fields; only the body may span lines.
    lines = [escape_control(line) for line in lines]
    body = detail["responseBody"]
    if body is not None:
        text = body if isinstance(body, str) else to_json(body)
        if not verbose and len(text) > BODY_LIMIT:
            text = text[:BODY_LIMIT] + f"\n... ({len(text) - BODY_LIMIT}자 생략, -v로 전체 보기)"
        lines.append(escape_control(text, keep_newlines=True))
    info = exchange.summary()
    lines.append(
        escape_control(
            f"요약: HTTP {info['status']}, JSON-RPC code {info['rpcCode'] if info['rpcCode'] is not None else '-'}, "
            f"resultType {info['resultType'] or '-'}, {info['elapsedMs']}ms"
        )
    )
    return "\n".join(lines)


def count_statuses(results: list[ScenarioResult]) -> dict[str, int]:
    return {
        status: sum(1 for result in results if result.status == status)
        for status in (PASS, FAIL, WARN, SKIP)
    }


def failed_count(counts: dict[str, int], strict: bool) -> int:
    """Scenarios that make the run fail: FAIL, plus WARN under --strict."""
    return counts[FAIL] + (counts[WARN] if strict else 0)


def format_results(
    results: list[ScenarioResult], header: str, verbose: bool = False, strict: bool = False
) -> str:
    name_width = max([width(result.scenario.name) for result in results] + [8])
    lines = [
        header,
        "",
        f"{pad('결과', 5)} {pad('그룹', 17)} {pad('시나리오', name_width)} {'ms':>7}  비고",
    ]
    for result in results:
        remark = result.reason if result.status == SKIP else ""
        if result.status in (FAIL, WARN):
            remark = f"{len(result.failures())}건 불일치"
        lines.append(
            f"{pad(result.status, 5)} {pad(result.scenario.group, 17)} {pad(result.scenario.name, name_width)} "
            f"{result.elapsed_ms:7.0f}  {remark}"
        )
        for check in result.failures():
            level = "MUST" if check.level == "must" else "SHOULD"
            lines.append(
                f"        - [{level}] {check.label}: 기대 {check.expected} / 실제 {check.actual}"
            )
        if result.failures():
            lines.append(f"        근거: {result.scenario.basis}")
        if verbose or result.status in (FAIL, WARN):
            for note in result.notes:
                lines.append(f"        참고: {note}")
        if verbose:
            for check in result.checks:
                if check.ok:
                    lines.append(f"        + {check.label}: {check.expected}")
            for exchange in result.exchanges:
                lines.append(f"        {summary_line(exchange)}")
    counts = count_statuses(results)
    totals = "합계: " + ", ".join(f"{status} {count}" for status, count in counts.items())
    if strict and counts[WARN]:
        totals += f" (--strict: WARN {counts[WARN]}건 포함 실패 {failed_count(counts, strict)}건)"
    lines.append("")
    lines.append(totals)
    if counts[WARN] and strict:
        lines.append("WARN은 SHOULD 수준 불일치다. --strict라서 실패로 센다(종료 코드 1).")
    elif counts[WARN]:
        lines.append("WARN은 SHOULD 수준 불일치다. --strict를 주면 실패로 센다.")
    return "\n".join(escape_control(line) for line in lines)


def results_json(
    results: list[ScenarioResult],
    meta: dict[str, Any],
    verbose: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    counts = count_statuses(results)
    return {
        **meta,
        "strict": strict,
        "summary": counts,
        "failed": failed_count(counts, strict),
        "results": [result.to_json(verbose) for result in results],
    }


def format_scenarios(scenarios: list[Scenario], profile: str) -> str:
    lines: list[str] = []
    group = None
    for item in scenarios:
        if item.group != group:
            group = item.group
            lines.extend(["", f"[{group}]"])
        lines.append(f"  {item.name}")
        lines.append(f"      기대: {item.expect}")
        lines.append(f"      근거: {item.basis}")
        if profile == "gateway":
            lines.append(
                f"      게이트웨이({GATEWAY_KIND[item.gateway.kind]}): {item.gateway.basis}"
            )
    return "\n".join(lines).lstrip("\n")


GATEWAY_KIND = {
    "same": "확정, 스펙과 같음",
    "diff": "확정, 게이트웨이 전용 기대값",
    "cond": "조건부",
    "unknown": "미확정, SKIP",
    "n/a": "대상 아님, SKIP",
}


def scenarios_json(scenarios: list[Scenario]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.name,
            "group": item.group,
            "expect": item.expect,
            "basis": item.basis,
            "needs": item.needs,
            "gateway": {"kind": item.gateway.kind, "basis": item.gateway.basis},
        }
        for item in scenarios
    ]


def format_probe(result: ProbeResult, verbose: bool = False) -> str:
    labels = {
        "modern": "modern (2026-07-28 전용)",
        "legacy": "legacy (initialize 핸드셰이크 전용)",
        "dual": "dual (모던과 레거시 모두)",
        "auth-required": "판정 불가 (인증 필요: --bearer로 토큰을 주고 다시 실행)",
        "unreachable": "판정 불가 (연결 실패)",
        "unknown": "판정 불가 (모던도 레거시도 아님)",
    }
    lines = [f"판정: {labels.get(result.verdict, result.verdict)}"]
    if result.dual_client_choice:
        lines.append(f"듀얼 클라이언트의 선택: {result.dual_client_choice}")
    if result.supported_versions:
        lines.append(f"서버가 알린 지원 버전: {', '.join(result.supported_versions)}")
    if result.legacy_version:
        lines.append(f"initialize 협상 버전: {result.legacy_version}")
    lines.append("절차:")
    lines.extend(f"  {index}. {step}" for index, step in enumerate(result.steps, 1))
    lines.append("요청:")
    lines = [escape_control(line) for line in lines]
    for exchange in result.exchanges:
        lines.append(f"  {summary_line(exchange)}")
        if verbose:
            lines.extend("    " + line for line in format_exchange(exchange, True).splitlines())
    return "\n".join(lines)


__all__ = [
    "escape_control",
    "format_exchange",
    "format_probe",
    "format_results",
    "format_scenarios",
    "mask_json",
    "results_json",
    "scenarios_json",
    "summary_line",
    "to_json",
]
