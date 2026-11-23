"""Modern (2026-07-28, stateless) and legacy (2025-xx, handshake) client roles,
plus the dual-client probe that follows the spec's HTTP fallback procedure."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from . import protocol as p
from .transport import Exchange, HeaderList, deep_merge, encode_cfg_token, send, with_query_param

TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"  # server.py: TEST_SCENARIO_HEADER
_DEFAULT = object()


@dataclass
class Target:
    """Where requests go (gateway or server) and what every request carries."""

    url: str
    headers: list[tuple[str, str]] = field(default_factory=list)  # user -H values
    bearer: str | None = None
    timeout: float = 30.0
    cfg: dict[str, Any] | None = None  # unit-expert server config, sent as ?cfg=
    scenario_header: str | None = None
    recorders: list[Callable[[Exchange], None]] = field(default_factory=list)

    def endpoint(self, extra_cfg: dict[str, Any] | None = None) -> str:
        config = self.cfg
        if extra_cfg:
            config = deep_merge(config or {}, extra_cfg)
        return with_query_param(self.url, "cfg", encode_cfg_token(config)) if config else self.url

    def derive(self, cfg: dict[str, Any] | None = None, timeout: float | None = None) -> "Target":
        """A copy that adds ``cfg`` to this target's server config (and/or uses another
        timeout). The copy shares the recorders, so its exchanges are still reported."""
        merged = deep_merge(self.cfg or {}, cfg) if cfg else self.cfg
        return replace(self, cfg=merged, timeout=self.timeout if timeout is None else timeout)

    def finish_headers(self, headers: HeaderList, bearer: Any = _DEFAULT) -> HeaderList:
        """Add auth, the scenario header and user -H values. A user header replaces
        a generated header of the same name, so anything can be overridden."""
        result: HeaderList = list(headers)
        token = self.bearer if bearer is _DEFAULT else bearer
        if token:
            result.append(("Authorization", f"Bearer {token}"))
        if self.scenario_header:
            result.append((TEST_SCENARIO_HEADER, self.scenario_header))
        # When a scenario sets credentials explicitly, a user Authorization header must not win.
        user_headers = [
            (name, value)
            for name, value in self.headers
            if not (bearer is not _DEFAULT and name.lower() == "authorization")
        ]
        overridden = {name.lower() for name, _ in user_headers}
        result = [(name, value) for name, value in result if name.lower() not in overridden]
        result.extend(user_headers)
        return result

    def send(
        self,
        http_method: str,
        headers: HeaderList,
        body: bytes | None = None,
        *,
        label: str = "",
        request_id: Any = None,
        cfg: dict[str, Any] | None = None,
        bearer: Any = _DEFAULT,
        url: str | None = None,
        max_sse_events: int | None = None,
    ) -> Exchange:
        # An explicit url (well-known, OAuth endpoints) is used as-is, without ?cfg=.
        target_url = url or self.endpoint(cfg)
        exchange = send(
            http_method,
            target_url,
            self.finish_headers(headers, bearer),
            body,
            timeout=self.timeout,
            label=label,
            request_id=request_id,
            **({"max_sse_events": max_sse_events} if max_sse_events else {}),
        )
        for recorder in self.recorders:
            recorder(exchange)
        return exchange


def dump(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ModernClient:
    """Stateless 2026-07-28 client: every POST carries the standard headers and
    the ``_meta`` envelope (streamable-http, basic/index#_meta)."""

    def __init__(
        self,
        target: Target,
        version: str = p.MODERN_VERSION,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.target = target
        self.version = version
        self.capabilities = capabilities if capabilities is not None else {}
        self.tools: list[dict[str, Any]] | None = None
        self.raw_tools: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.pages: list[Exchange] = []
        self.rejected: list[str] = []  # why a tools/list result was not used
        self._next_id = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        tweaks: p.Tweaks | None = None,
        tool: dict[str, Any] | None = None,
        request_id: Any = _DEFAULT,
        label: str = "",
        cfg: dict[str, Any] | None = None,
        bearer: Any = _DEFAULT,
        http_method: str = "POST",
        max_sse_events: int | None = None,
    ) -> Exchange:
        tweaks = tweaks or p.Tweaks()
        body_params = dict(params or {})
        meta = p.modern_meta(self.version, self.capabilities, tweaks)
        if meta is not None:
            body_params["_meta"] = {**meta, **(body_params.get("_meta") or {})}
        rpc_id = self.next_id() if request_id is _DEFAULT else request_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if body_params:
            payload["params"] = body_params
        body = tweaks.raw_body if tweaks.raw_body is not None else dump(payload)
        headers = p.modern_headers(method, body_params, self.version, tool, tweaks)
        return self.target.send(
            http_method,
            headers,
            body,
            label=label or method,
            request_id=rpc_id,
            cfg=cfg,
            bearer=bearer,
            max_sse_events=max_sse_events,
        )

    def discover(self, **kwargs: Any) -> Exchange:
        return self.request("server/discover", **kwargs)

    def list_tools(self, cursor: str | None = None, **kwargs: Any) -> Exchange:
        params = {"cursor": cursor} if cursor is not None else None
        return self.request("tools/list", params, **kwargs)

    def load_tools(
        self, *, max_pages: int = 20, refresh: bool = False, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Fetch every tools/list page and keep the tools whose x-mcp-header
        annotations are valid (invalid ones MUST be excluded)."""
        if self.tools is not None and not refresh:
            return self.tools
        self.pages, collected, cursor = [], [], None
        self.rejected = []
        for _ in range(max_pages):
            exchange = self.list_tools(cursor, **kwargs)
            self.pages.append(exchange)
            result = exchange.result or {}
            # A response for another id, or a result the client has to consider
            # invalid (unknown resultType), is not a tool list.
            _, problem = p.judge_result("tools/list", result)
            if exchange.id_mismatch:
                problem = "응답 id가 요청 id와 다름"
            if exchange.result is not None and problem:
                self.rejected.append(problem)
                break
            if isinstance(result.get("tools"), list):
                collected.extend(tool for tool in result["tools"] if isinstance(tool, dict))
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str):
                break
        self.raw_tools = collected
        self.tools, self.warnings = p.split_usable_tools(collected)
        return self.tools

    def find_tool(self, name: str) -> dict[str, Any] | None:
        return next((tool for tool in self.tools or [] if tool.get("name") == name), None)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        tool: Any = _DEFAULT,
        auto_input: bool = False,
        max_rounds: int = 5,
        tweaks: p.Tweaks | None = None,
        **kwargs: Any,
    ) -> list[Exchange]:
        """tools/call with Mcp-Param-* derived from the cached schema.

        With ``auto_input`` an ``input_required`` result is answered by re-sending
        the original params plus ``inputResponses`` and the unchanged
        ``requestState`` under a new JSON-RPC id (basic/patterns/mrtr).
        """
        schema_tool = self.find_tool(name) if tool is _DEFAULT else tool
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        exchanges = [self.request("tools/call", params, tool=schema_tool, tweaks=tweaks, **kwargs)]
        rounds = 0
        retry_kwargs = {
            key: value for key, value in kwargs.items() if key not in ("request_id", "label")
        }
        while auto_input and exchanges[-1].result_type == "input_required" and rounds < max_rounds:
            rounds += 1
            result = exchanges[-1].result or {}
            retry = dict(params)
            requests = result.get("inputRequests")
            if isinstance(requests, dict):
                retry["inputResponses"] = {
                    key: p.input_response_for(req) for key, req in requests.items()
                }
            if "requestState" in result:  # echo exactly; never invent one
                retry["requestState"] = result["requestState"]
            exchanges.append(
                self.request(
                    "tools/call",
                    retry,
                    tool=schema_tool,
                    tweaks=tweaks,
                    label=f"tools/call (inputResponses #{rounds})",
                    **retry_kwargs,
                )
            )
        return exchanges


class LegacyClient:
    """2025-xx client: initialize -> notifications/initialized, then keeps the
    session id and negotiated version on every request."""

    def __init__(
        self,
        target: Target,
        version: str = p.LATEST_LEGACY_VERSION,
        *,
        reinit_on_404: bool = False,
    ) -> None:
        self.target = target
        self.version = version
        self.reinit_on_404 = reinit_on_404
        self.session_id: str | None = None
        self.negotiated: str | None = None
        self.reinitialized = 0
        self.initialized_exchange: Exchange | None = None
        self._next_id = 0

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def headers(
        self, session: Any = _DEFAULT, version_header: Any = _DEFAULT, accept: str | None = None
    ) -> HeaderList:
        headers: HeaderList = [
            ("Content-Type", "application/json"),
            ("Accept", accept or p.ACCEPT_BOTH),
        ]
        version = self.negotiated if version_header is _DEFAULT else version_header
        if version:
            headers.append((p.H_VERSION, version))
        session_id = self.session_id if session is _DEFAULT else session
        if session_id:
            headers.append((p.H_SESSION, session_id))
        return headers

    def initialize(
        self,
        version: str | None = None,
        *,
        send_initialized: bool = True,
        request_id: Any = _DEFAULT,
        **kwargs: Any,
    ) -> Exchange:
        rpc_id = self.next_id() if request_id is _DEFAULT else request_id
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "initialize",
            "params": {
                "protocolVersion": version or self.version,
                "capabilities": {},
                "clientInfo": dict(p.CLIENT_INFO),
            },
        }
        exchange = self.target.send(
            "POST",
            self.headers(session=None, version_header=None),
            dump(payload),
            label="initialize",
            request_id=rpc_id,
            **kwargs,
        )
        result = exchange.result
        if exchange.status == 200 and result is not None:
            self.session_id = exchange.header(p.H_SESSION)
            negotiated = result.get("protocolVersion")
            self.negotiated = negotiated if isinstance(negotiated, str) else None
            if send_initialized:
                self.initialized_exchange = self.notify("notifications/initialized", **kwargs)
        return exchange

    def notify(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Exchange:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            payload["params"] = params
        return self.target.send("POST", self.headers(), dump(payload), label=method, **kwargs)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: Any = _DEFAULT,
        version_header: Any = _DEFAULT,
        request_id: Any = _DEFAULT,
        label: str = "",
        raw_body: bytes | None = None,
        accept: str | None = None,
        **kwargs: Any,
    ) -> Exchange:
        rpc_id = self.next_id() if request_id is _DEFAULT else request_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
        if params:
            payload["params"] = params
        body = raw_body if raw_body is not None else dump(payload)
        exchange = self.target.send(
            "POST",
            self.headers(session, version_header, accept),
            body,
            label=label or method,
            request_id=rpc_id,
            **kwargs,
        )
        # 2025-11-25 transports "Session Management": on 404 for a request carrying
        # a session id the client MUST start a new session with a fresh initialize.
        if (
            exchange.status == 404
            and self.reinit_on_404
            and session is _DEFAULT
            and self.session_id
        ):
            self.session_id, self.negotiated = None, None
            init = self.initialize(**kwargs)
            if init.status == 200 and init.result is not None:
                self.reinitialized += 1
                rpc_id = self.next_id()
                payload["id"] = rpc_id
                exchange = self.target.send(
                    "POST",
                    self.headers(accept=accept),
                    dump(payload),
                    label=f"{label or method} (after re-initialize)",
                    request_id=rpc_id,
                    **kwargs,
                )
        return exchange

    def delete_session(
        self, session: Any = _DEFAULT, *, label: str = "DELETE session", **kwargs: Any
    ) -> Exchange:
        headers = [(name, value) for name, value in self.headers(session) if name != "Content-Type"]
        return self.target.send("DELETE", headers, None, label=label, **kwargs)


@dataclass
class ProbeResult:
    verdict: str  # modern | legacy | dual | unknown | auth-required | unreachable
    modern: bool | None = None  # the target answered as a modern (2026-07-28) server
    legacy: bool | None = None
    # What a dual client ends up speaking: "modern", "legacy", or None when it has
    # to surface an error (no mutually supported version, or nothing worked).
    dual_client_choice: str | None = None
    supported_versions: list[str] = field(default_factory=list)
    legacy_version: str | None = None
    steps: list[str] = field(default_factory=list)
    exchanges: list[Exchange] = field(default_factory=list)
    # True when the fallback procedure itself sends initialize (as opposed to the
    # extra initialize that check_legacy adds to tell dual from modern-only).
    fell_back: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "modern": self.modern,
            "legacy": self.legacy,
            "dualClientChoice": self.dual_client_choice,
            "fellBack": self.fell_back,
            "supportedVersions": self.supported_versions,
            "legacyVersion": self.legacy_version,
            "steps": self.steps,
            "exchanges": [exchange.summary() for exchange in self.exchanges],
        }


def _known_legacy(versions: list[Any]) -> list[str]:
    return [v for v in versions if isinstance(v, str) and v in p.LEGACY_VERSIONS]


def probe(
    target: Target, *, check_legacy: bool = True, version: str = p.MODERN_VERSION
) -> ProbeResult:
    """Classify the target the way a dual client does.

    streamable-http "Backward Compatibility" + basic/versioning: send a modern
    request first. A recognised modern JSON-RPC error on 400 identifies a modern
    server: the request is corrected (or re-sent with a version from ``supported``)
    and retried, never ``initialize``. Anything else (a 4xx without such a body, an
    empty body, another JSON-RPC error, no usable response, a timeout) identifies a
    legacy server and the client falls back to ``initialize``. The fallback MUST NOT
    be keyed to one specific error code.

    When the server names its versions (DiscoverResult.supportedVersions or
    -32022 data.supported) the client picks a mutually supported one: the modern
    version if listed, else a listed legacy version through ``initialize``, else
    there is no compatible version and the client stops with an error.

    A real dual client stops as soon as modern works. With ``check_legacy`` the
    probe also sends ``initialize`` afterwards so it can tell dual from modern-only.
    """
    result = ProbeResult(verdict="unknown")
    modern = ModernClient(target, version)
    # modern | legacy-fallback | legacy-version | incompatible
    outcome = "legacy-fallback"
    legacy_pick: str | None = None
    rejected = False  # modern server, but it keeps refusing the request

    def run_discover(client: ModernClient, note: str) -> Exchange:
        exchange = client.discover(label=f"probe: server/discover ({note})")
        result.exchanges.append(exchange)
        return exchange

    def pick_from(versions: list[Any], source: str) -> str:
        """No modern version in a server-provided list: legacy version or nothing."""
        nonlocal legacy_pick
        legacy_versions = _known_legacy(versions)
        if legacy_versions:
            legacy_pick = max(legacy_versions)
            result.modern = False
            result.steps.append(
                f"{source}={versions}: 모던 버전이 없고 레거시 버전만 있음 -> "
                f"상호 지원 버전 {legacy_pick}로 initialize"
            )
            return "legacy-version"
        result.modern = True
        result.steps.append(
            f"{source}={versions}: 이 클라이언트가 아는 버전이 없음 -> 호환 버전 없음, "
            "initialize로 폴백하지 않고 에러로 처리"
        )
        return "incompatible"

    exchange = run_discover(modern, version)
    for attempt in range(2):
        if exchange.error:
            result.steps.append(f"모던 요청 전송 실패({exchange.error}) -> initialize로 폴백")
            break
        if exchange.status in (401, 403):
            result.steps.append(f"모던 요청에 HTTP {exchange.status}: 인증이 필요해 판정 불가")
            result.verdict = "auth-required"
            return result
        discover = exchange.result
        if exchange.status == 200 and discover is not None and exchange.id_mismatch:
            result.modern = False
            result.steps.append(
                "server/discover 200이지만 응답 id가 요청 id와 달라 응답으로 인정하지 않음 "
                "-> initialize로 폴백"
            )
            break
        result_type, problem = p.judge_result("server/discover", discover)
        if (
            exchange.status == 200
            and discover is not None
            and isinstance(discover.get("supportedVersions"), list)
        ):
            if problem:
                result.modern = False
                result.steps.append(
                    f"server/discover 200이지만 무효한 결과({problem}) -> initialize로 폴백"
                )
                break
            versions = discover["supportedVersions"]
            result.supported_versions = [v for v in versions if isinstance(v, str)]
            if modern.version in versions or p.MODERN_VERSION in versions:
                result.modern = True
                outcome = "modern"
                result.steps.append(
                    f"server/discover 200 + DiscoverResult(supportedVersions={result.supported_versions}) -> 모던 지원"
                )
            else:
                outcome = pick_from(versions, "DiscoverResult supportedVersions")
            break
        code = exchange.rpc_code
        if exchange.status == 400 and code in p.MODERN_ERROR_CODES:
            data = (exchange.rpc_error or {}).get("data")
            supported = data.get("supported") if isinstance(data, dict) else None
            if code == p.ERR_UNSUPPORTED_VERSION and isinstance(supported, list):
                result.supported_versions = [v for v in supported if isinstance(v, str)]
                if (
                    attempt == 0
                    and p.MODERN_VERSION in supported
                    and modern.version != p.MODERN_VERSION
                ):
                    result.steps.append(
                        f"400 -32022, supported={supported} -> {p.MODERN_VERSION}로 고쳐 재시도"
                    )
                    modern = ModernClient(target, p.MODERN_VERSION)
                    exchange = run_discover(modern, f"retry {p.MODERN_VERSION}")
                    continue
                outcome = pick_from(supported, "400 -32022 supported")
                break
            if attempt == 0:
                result.steps.append(
                    f"400 + 모던 에러 {code}: initialize로 가지 않고 요청을 다시 구성해 재시도"
                )
                exchange = run_discover(modern, "retry")
                continue
            result.modern = True
            outcome = "modern"
            rejected = True
            result.steps.append(
                f"재시도에도 400 + 모던 에러 {code}: 대상은 모던 서버지만 요청을 거부함(중간 경유지가 헤더를 "
                "바꿨을 수 있음). initialize로 폴백하지 않음"
            )
            break
        result.modern = False
        if exchange.status == 400:
            body = "빈 바디" if not exchange.body else f"모던 에러가 아닌 바디(code={code})"
            result.steps.append(f"모던 요청에 400 + {body} -> initialize로 폴백")
        else:
            result.steps.append(
                f"모던 요청에 HTTP {exchange.status}, JSON-RPC code={code}, DiscoverResult 아님 -> initialize로 폴백"
            )
        break

    result.fell_back = outcome in ("legacy-fallback", "legacy-version")
    if result.fell_back or check_legacy:
        legacy = LegacyClient(target, legacy_pick or p.LATEST_LEGACY_VERSION)
        init = _probe_initialize(legacy)
        result.exchanges.append(init)
        if init.error:
            result.steps.append(f"initialize 전송 실패: {init.error}")
        elif init.status in (401, 403) and result.modern is None:
            result.verdict = "auth-required"
            result.steps.append(f"initialize에 HTTP {init.status}: 인증이 필요해 판정 불가")
            return result
        elif (
            init.status == 200
            and init.result is not None
            and isinstance(init.result.get("protocolVersion"), str)
        ):
            result.legacy = True
            result.legacy_version = init.result["protocolVersion"]
            note = ""
            if result.legacy_version == p.MODERN_VERSION:
                note = " (경고: 핸드셰이크가 모던 버전을 돌려줌. 2026-07-28에는 initialize가 없다)"
            result.steps.append(
                f"initialize 200, protocolVersion={result.legacy_version} -> 레거시 지원{note}"
            )
            if legacy.session_id:
                result.exchanges.append(
                    legacy.delete_session(label="probe: DELETE session (cleanup)")
                )
        else:
            result.legacy = False
            result.steps.append(
                f"initialize 거부: HTTP {init.status}, JSON-RPC code={init.rpc_code} -> 레거시 미지원"
            )

    if all(exchange.error for exchange in result.exchanges):
        result.verdict = "unreachable"
    elif result.modern and result.legacy:
        result.verdict = "dual"
    elif result.modern:
        result.verdict = "modern"
    elif result.legacy:
        result.verdict = "legacy"
    if outcome == "modern":
        result.dual_client_choice = "modern"
    elif result.fell_back and result.legacy:
        result.dual_client_choice = "legacy"
    if outcome == "modern" and not check_legacy and not rejected:
        result.steps.append(
            "모던이 동작하므로 듀얼 클라이언트는 여기서 멈춘다(레거시 지원 여부는 확인하지 않음)"
        )
    return result


def _probe_initialize(legacy: LegacyClient) -> Exchange:
    exchange = legacy.initialize(send_initialized=False)
    exchange.label = "probe: initialize"
    return exchange
