"""Scenario runner: executes scenarios, compares against expectations and
produces PASS / FAIL / WARN / SKIP results.

Levels: a failed MUST-level check makes the scenario FAIL; a failed SHOULD-level
check makes it WARN (exit code stays 0 unless --strict).
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import protocol as p
from .clients import LegacyClient, ModernClient, ProbeResult, Target, dump, probe
from .transport import Exchange

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
GROUPS = ("modern", "modern-violation", "legacy", "auth", "compat", "injection")
# Test tools of the unit-expert server (server.py: HEADER_PARAMS_TOOL, MRTR_TOOL).
# They are only preferred when tools/list has them; otherwise tools are discovered.
DEFAULT_HEADER_TOOL = "echo_header_params"
DEFAULT_INPUT_TOOL = "confirm_action"
CANARY_METHOD = "unit-expert-client/canary"


class Skip(Exception):
    """Raised by a scenario that cannot be evaluated against this target."""


class Stop(Exception):
    """Ends a scenario early; the checks recorded so far decide the status."""


@dataclass
class Gateway:
    """How a scenario's expectation maps to a gateway (an MCP proxy in front of the server).

    same     expectation equals the spec expectation; confirmed by reading gateway code
    diff     gateway-specific expectation, coded in the scenario; confirmed
    cond     the behaviour was seen in gateway code, but whether it applies on
             the actual request path is unverified
    unknown  not confirmed -> SKIP ("미확정") unless --include-unconfirmed
    n/a      only meaningful against the unit-expert server -> SKIP
    """

    kind: str
    basis: str


@dataclass
class Scenario:
    name: str
    group: str
    expect: str  # human-readable expectation
    basis: str  # spec section the expectation comes from
    fn: Callable[["Context"], None]
    gateway: Gateway
    needs: str | None = None  # "modern" | "legacy" | "modern-only"
    injects: bool = False  # only meaningful when the target takes ?cfg= (unit-expert server)


@dataclass
class Check:
    label: str
    ok: bool
    expected: str
    actual: str
    level: str = "must"

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ok": self.ok,
            "expected": self.expected,
            "actual": self.actual,
            "level": self.level,
        }


@dataclass
class ScenarioResult:
    scenario: Scenario
    status: str = PASS
    checks: list[Check] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reason: str = ""
    elapsed_ms: float = 0.0

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    def to_json(self, verbose: bool = False) -> dict[str, Any]:
        return {
            "name": self.scenario.name,
            "group": self.scenario.group,
            "status": self.status,
            "reason": self.reason,
            "expect": self.scenario.expect,
            "basis": self.scenario.basis,
            "gateway": {"kind": self.scenario.gateway.kind, "basis": self.scenario.gateway.basis},
            "checks": [check.to_json() for check in self.checks],
            "notes": self.notes,
            "elapsedMs": round(self.elapsed_ms, 1),
            "exchanges": [
                exchange.detail() if verbose else exchange.summary() for exchange in self.exchanges
            ],
        }


@dataclass
class Options:
    profile: str = "server"  # server | gateway
    tool: str | None = None
    args: dict[str, Any] | None = None
    input_tool: str | None = None
    header_tool: str | None = None
    inject: bool = True  # --expect server: add the server config a scenario needs via ?cfg=
    assume_era: str | None = None  # modern | legacy | dual
    expect_era: str | None = None
    include_unconfirmed: bool = False
    protocol_version: str | None = None
    legacy_version: str | None = None
    capabilities: dict[str, Any] | None = None


_UNIT_ARGS = {"value": 1, "from_unit": "km", "to_unit": "m"}
_INPUT_TOOL_RE = re.compile(
    r"input[_\- ]?required|elicit|mrtr|input_request|ask_|confirm", re.IGNORECASE
)


class Context:
    """What a scenario function sees: clients, assertions, shared discovery."""

    def __init__(self, runner: "Runner", result: ScenarioResult) -> None:
        self.runner = runner
        self.result = result
        self.target = runner.target
        self.options = runner.options
        self.gateway = runner.options.profile == "gateway"
        self.injected: list[dict[str, Any]] = []

    # -- clients -----------------------------------------------------------------
    def modern(
        self,
        version: str | None = None,
        capabilities: dict[str, Any] | None = None,
        cfg: dict[str, Any] | None = None,
    ) -> ModernClient:
        return ModernClient(
            self.configured(cfg),
            version or self.options.protocol_version or p.MODERN_VERSION,
            capabilities if capabilities is not None else self.options.capabilities,
        )

    def legacy(
        self, version: str | None = None, cfg: dict[str, Any] | None = None, **kwargs: Any
    ) -> LegacyClient:
        return LegacyClient(
            self.configured(cfg),
            version or self.options.legacy_version or p.LATEST_LEGACY_VERSION,
            **kwargs,
        )

    # -- server config injection ---------------------------------------------------
    def injectable(self) -> bool:
        """True when the target is a unit-expert server that honours ?cfg= and
        injection is on (--expect server, no --no-inject)."""
        return self.runner.injectable()

    def configured(self, cfg: dict[str, Any] | None, timeout: float | None = None) -> Target:
        """The target with ``cfg`` added to its server config. On a target that does
        not take ?cfg= (gateway, another server) the plain target is returned, so
        the scenario runs against whatever the target does by default."""
        if not (cfg and self.injectable()):
            return self.target if timeout is None else self.target.derive(timeout=timeout)
        self.injected.append(cfg)
        return self.target.derive(cfg, timeout)

    def era(self) -> ProbeResult:
        return self.runner.era()

    def supports(self, era: str) -> bool:
        return self.runner.supports(era)

    def tools(self) -> ModernClient:
        """A modern client with the tool list loaded (shared between scenarios)."""
        client = self.runner.shared_modern()
        if not client.tools:
            first = client.pages[0] if client.pages else None
            detail = f"HTTP {first.status}, code={first.rpc_code}" if first else "응답 없음"
            raise Skip(f"tools/list에서 쓸 수 있는 툴을 얻지 못함 ({detail})")
        return client

    # -- tool selection (no hard-coded server tool names) --------------------------
    def plain_tool(self, tools: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
        """A tool that can be called with generated arguments: --tool/--args, else a
        unit-conversion style tool (value/from_unit/to_unit), else the first tool
        without x-mcp-header parameters."""
        if self.options.tool:
            for tool in tools:
                if tool.get("name") == self.options.tool:
                    return tool, self.arguments_for(tool)
            raise Skip(f"--tool {self.options.tool!r}이 tools/list에 없음")
        unit = self.unit_tool(tools)
        if unit:
            return unit, self.arguments_for(unit)
        for tool in tools:
            if not p.collect_header_params(tool.get("inputSchema"))[0]:
                return tool, self.arguments_for(tool)
        if tools:
            return tools[0], self.arguments_for(tools[0])
        raise Skip("호출할 툴이 없음")

    @staticmethod
    def unit_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = []
        for tool in tools:
            schema = tool.get("inputSchema")
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if isinstance(properties, dict) and set(_UNIT_ARGS) <= set(properties):
                candidates.append(tool)
        preferred = [
            tool for tool in candidates if str(tool.get("name", "")).endswith("convert_length")
        ]
        return (preferred or candidates or [None])[0]

    def arguments_for(self, tool: dict[str, Any]) -> dict[str, Any]:
        if self.options.args is not None and (
            not self.options.tool or tool.get("name") == self.options.tool
        ):
            return dict(self.options.args)
        schema = tool.get("inputSchema")
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if isinstance(properties, dict) and set(_UNIT_ARGS) <= set(properties):
            return {**p.sample_arguments(schema), **_UNIT_ARGS}
        return p.sample_arguments(schema)

    def header_tools(
        self, tools: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], list[p.HeaderParam]]]:
        found = []
        for tool in tools:
            params, problems = p.collect_header_params(tool.get("inputSchema"))
            if params and not problems:
                found.append((tool, params))
        wanted = self.options.header_tool
        if wanted:
            found = [pair for pair in found if pair[0].get("name") == wanted]
            if not found:
                raise Skip(f"--header-tool {wanted!r}이 tools/list에 없거나 x-mcp-header가 없음")
        if not found:
            raise Skip("tools/list에 x-mcp-header가 달린 툴이 없음 (--header-tool NAME으로 지정)")
        # The server's own test tool first; any other annotated tool still counts.
        found.sort(key=lambda pair: pair[0].get("name") != DEFAULT_HEADER_TOOL)
        return found

    def input_tool(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        if self.options.input_tool:
            for tool in tools:
                if tool.get("name") == self.options.input_tool:
                    return tool
            raise Skip(f"--input-tool {self.options.input_tool!r}이 tools/list에 없음")
        for tool in tools:
            if tool.get("name") == DEFAULT_INPUT_TOOL:
                return tool
        for tool in tools:
            if _INPUT_TOOL_RE.search(f"{tool.get('name', '')} {tool.get('description', '')}"):
                return tool
        raise Skip("input_required를 돌려주는 툴을 찾지 못함 (--input-tool NAME으로 지정)")

    # -- assertions -------------------------------------------------------------------
    def check(
        self, label: str, ok: bool, expected: Any = "", actual: Any = "", level: str = "must"
    ) -> bool:
        self.result.checks.append(Check(label, bool(ok), str(expected), str(actual), level))
        return bool(ok)

    def note(self, text: str) -> None:
        self.result.notes.append(text)

    def skip(self, reason: str) -> None:
        raise Skip(reason)

    def pick(self, server: Any, gateway: Any) -> Any:
        return gateway if self.gateway else server

    @staticmethod
    def describe(exchange: Exchange) -> str:
        if exchange.error:
            return f"전송 실패({exchange.error})"
        parts = [f"HTTP {exchange.status}"]
        if exchange.rpc_code is not None:
            parts.append(f"code {exchange.rpc_code}")
        if exchange.result_type is not None:
            parts.append(f"resultType {exchange.result_type}")
        return ", ".join(parts)

    def expect_status(
        self,
        exchange: Exchange,
        status: int | tuple[int, ...],
        level: str = "must",
        label: str = "",
    ) -> bool:
        allowed = status if isinstance(status, tuple) else (status,)
        expected = " 또는 ".join(f"HTTP {code}" for code in allowed)
        return self.check(
            label or f"{exchange.label}: HTTP 상태",
            exchange.status in allowed,
            expected,
            self.describe(exchange),
            level,
        )

    def expect_code(
        self, exchange: Exchange, code: int | tuple[int, ...], level: str = "must"
    ) -> bool:
        allowed = code if isinstance(code, tuple) else (code,)
        expected = " 또는 ".join(f"code {item}" for item in allowed)
        return self.check(
            f"{exchange.label}: JSON-RPC 에러 코드",
            exchange.rpc_code in allowed,
            expected,
            self.describe(exchange),
            level,
        )

    def expect_error(
        self, exchange: Exchange, status: int | tuple[int, ...] | None, code: int | tuple[int, ...]
    ) -> bool:
        ok = True
        if status is not None:
            ok = self.expect_status(exchange, status) and ok
        return self.expect_code(exchange, code) and ok

    def expect_result(
        self, exchange: Exchange, *, modern: bool = True, result_type: str = "complete"
    ) -> dict[str, Any]:
        """HTTP 200 + a JSON-RPC result; modern results MUST carry resultType (basic/index)."""
        self.expect_status(exchange, 200)
        self.check(
            f"{exchange.label}: JSON-RPC result",
            exchange.result is not None,
            "result",
            self.describe(exchange),
        )
        result = exchange.result or {}
        if exchange.id_mismatch:
            self.check(
                f"{exchange.label}: 응답 id",
                False,
                f"요청 id {exchange.request_id!r}",
                f"{(exchange.message or {}).get('id')!r}",
            )
        if modern and exchange.result is not None:
            self.check(
                f"{exchange.label}: resultType",
                result.get("resultType") == result_type,
                f"resultType {result_type!r}",
                f"{result.get('resultType')!r}",
            )
        return result

    def expect_cache_fields(self, exchange: Exchange) -> None:
        """server/utilities/caching: ttlMs (>= 0) and cacheScope are MUST on complete results."""
        result = exchange.result or {}
        ttl = result.get("ttlMs")
        self.check(
            f"{exchange.label}: ttlMs",
            isinstance(ttl, int) and not isinstance(ttl, bool) and ttl >= 0,
            "0 이상 정수",
            repr(ttl),
        )
        self.check(
            f"{exchange.label}: cacheScope",
            result.get("cacheScope") in ("public", "private"),
            "'public' 또는 'private'",
            repr(result.get("cacheScope")),
        )


class Runner:
    def __init__(self, target: Target, options: Options, scenarios: list[Scenario]) -> None:
        self.target = target
        self.options = options
        self.scenarios = scenarios
        self._era: ProbeResult | None = None
        self._shared: ModernClient | None = None
        self._injectable: bool | None = None

    def era(self) -> ProbeResult:
        if self._era is None:
            assumed = self.options.assume_era
            if assumed:
                self._era = ProbeResult(
                    verdict=assumed,
                    modern=assumed in ("modern", "dual"),
                    legacy=assumed in ("legacy", "dual"),
                    steps=[f"--assume-era {assumed}"],
                )
            else:
                self._era = probe(self.target)
        return self._era

    def supports(self, era: str) -> bool:
        """False only when the probe positively says the target lacks that era."""
        verdict = self.era()
        if verdict.verdict not in ("modern", "legacy", "dual"):
            return True  # undetermined: run the scenario and let it report
        return bool(verdict.modern if era == "modern" else verdict.legacy)

    def injectable(self) -> bool:
        """Does the target apply a ?cfg= server config? Checked once with a
        rawResponses rule, which the unit-expert server answers before any other
        check (era, auth, Accept), so the canary works in every server mode."""
        if self._injectable is None:
            self._injectable = False
            if self.options.profile == "server" and self.options.inject:
                nonce = secrets.token_hex(8)
                body = '{"jsonrpc":"2.0","id":"{{id}}","result":{"canary":"' + nonce + '"}}'
                rule = {"method": CANARY_METHOD, "body": body}
                exchange = self.target.derive({"rawResponses": [rule]}).send(
                    "POST",
                    [("Content-Type", "application/json"), ("Accept", p.ACCEPT_BOTH)],
                    dump({"jsonrpc": "2.0", "id": 1, "method": CANARY_METHOD}),
                    label="cfg 주입 지원 확인",
                    request_id=1,
                )
                self._injectable = (exchange.result or {}).get("canary") == nonce
        return self._injectable

    def shared_modern(self) -> ModernClient:
        if self._shared is None:
            self._shared = ModernClient(
                self.target,
                self.options.protocol_version or p.MODERN_VERSION,
                self.options.capabilities,
            )
            self._shared.load_tools()
        return self._shared

    def select(self, names: list[str]) -> list[Scenario]:
        selected: list[Scenario] = []
        for name in names or ["all"]:
            if name == "all":
                matches = list(self.scenarios)
            elif name in GROUPS:
                matches = [scenario for scenario in self.scenarios if scenario.group == name]
            else:
                matches = [scenario for scenario in self.scenarios if scenario.name == name]
                if not matches:
                    raise KeyError(name)
            selected.extend(scenario for scenario in matches if scenario not in selected)
        return selected

    def run(self, names: list[str]) -> list[ScenarioResult]:
        return [self.run_one(scenario) for scenario in self.select(names)]

    def _gate(self, scenario: Scenario) -> str | None:
        """Reason to skip before running, or None."""
        if self.options.profile == "gateway":
            kind = scenario.gateway.kind
            if kind == "n/a":
                return f"게이트웨이 대상 아님: {scenario.gateway.basis}"
            if kind == "unknown" and not self.options.include_unconfirmed:
                return f"게이트웨이 기대값 미확정: {scenario.gateway.basis}"
        if scenario.injects:
            if self.options.profile == "server" and not self.options.inject:
                return "--no-inject: 서버 설정 주입이 필요한 시나리오"
            if not self.injectable():
                return "대상이 ?cfg= 설정 주입을 받지 않음(단위전문가 서버 전용 시나리오)"
        needs = scenario.needs
        if needs == "modern" and not self.supports("modern"):
            return f"대상이 모던(2026-07-28)을 지원하지 않는 것으로 판정됨(probe: {self.era().verdict})"
        if needs == "legacy" and not self.supports("legacy"):
            return f"대상이 레거시 핸드셰이크를 지원하지 않는 것으로 판정됨(probe: {self.era().verdict})"
        if needs == "modern-only" and self.era().verdict != "modern":
            return f"모던 전용 서버에만 적용되는 기대값(probe: {self.era().verdict})"
        return None

    def run_one(self, scenario: Scenario) -> ScenarioResult:
        result = ScenarioResult(scenario)
        started = time.perf_counter()
        # The era probe (run once, on demand) is not part of any single scenario.
        reason = self._gate(scenario)
        self.target.recorders.append(result.exchanges.append)
        try:
            if reason:
                raise Skip(reason)
            context = Context(self, result)
            if context.gateway and scenario.gateway.kind == "cond":
                result.notes.append(f"조건부 기대값: {scenario.gateway.basis}")
            if context.gateway and scenario.gateway.kind in ("same", "diff"):
                result.notes.append(f"게이트웨이 근거: {scenario.gateway.basis}")
            if context.gateway and scenario.gateway.kind == "unknown":
                result.notes.append("게이트웨이 기대값 미확정: 스펙 기대값으로 비교함")
            try:
                scenario.fn(context)
            finally:
                for cfg in context.injected:
                    note = f"주입한 서버 설정: {dump(cfg).decode('utf-8')}"
                    if note not in result.notes:
                        result.notes.append(note)
        except Skip as skip:
            result.status, result.reason = SKIP, str(skip)
        except Stop:
            pass
        except Exception as error:  # a scenario bug must not abort the whole run
            result.checks.append(
                Check("시나리오 실행", False, "예외 없음", f"{type(error).__name__}: {error}")
            )
        finally:
            self.target.recorders.remove(result.exchanges.append)
            result.elapsed_ms = (time.perf_counter() - started) * 1000
        if result.status != SKIP:
            failures = result.failures()
            if any(check.level == "must" for check in failures):
                result.status = FAIL
            elif failures:
                result.status = WARN
            elif not result.checks:
                result.status, result.reason = SKIP, "비교할 항목이 없음"
        return result


def exit_code(results: list[ScenarioResult], strict: bool = False) -> int:
    statuses = {result.status for result in results}
    if FAIL in statuses or (strict and WARN in statuses):
        return 1
    return 0
