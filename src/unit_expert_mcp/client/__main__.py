"""CLI: ``python -m unit_expert_mcp.client`` / ``unit-expert-client``.

call <method>   one request, with flags that break the spec on purpose
run <names...>  predefined scenarios compared against expectations
list            scenarios, expectations and their basis
probe           classify the target as modern / legacy / dual
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import protocol as p
from . import report
from .clients import LegacyClient, ModernClient, Target, probe
from .runner import (
    DEFAULT_HEADER_TOOL,
    DEFAULT_INPUT_TOOL,
    GROUPS,
    Options,
    Runner,
    exit_code,
)
from .scenarios import SCENARIOS
from .transport import Exchange

ENV_URL = "UNIT_EXPERT_CLIENT_URL"
ENV_BEARER = "UNIT_EXPERT_CLIENT_BEARER"
EXIT_USAGE = 2


def parse_json_arg(text: str | None, option: str, expect: type | None = dict) -> Any:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError as error:
        raise SystemExit(f"{option}: JSON이 아님 ({error})") from error
    if expect is not None and not isinstance(value, expect):
        raise SystemExit(f"{option}: JSON {expect.__name__}이어야 함")
    return value


def parse_header(text: str) -> tuple[str, str]:
    name, separator, value = text.partition(":")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"'Name: value' 형식이어야 함: {text!r}")
    return name.strip(), value.strip()


def parse_param_header(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not name.strip():
        raise argparse.ArgumentTypeError(f"NAME=VALUE 형식이어야 함: {text!r}")
    name = name.strip()
    if not name.lower().startswith(p.H_PARAM_PREFIX.lower()):
        name = f"{p.H_PARAM_PREFIX}{name}"
    return name, value


def parse_id(text: str | None) -> Any:
    """--id 7 -> 7, --id null -> null, --id abc -> "abc"."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def common_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("공통 옵션")
    group.add_argument(
        "--url",
        default=os.getenv(ENV_URL),
        help=f"MCP 엔드포인트(게이트웨이 또는 서버). 환경변수 {ENV_URL}",
    )
    group.add_argument(
        "-H",
        "--header",
        action="append",
        type=parse_header,
        default=[],
        metavar="'Name: value'",
        help="모든 요청에 붙일 헤더(반복 가능). 같은 이름의 자동 헤더를 대체한다",
    )
    group.add_argument(
        "--bearer",
        default=os.getenv(ENV_BEARER),
        help=f"Authorization: Bearer 토큰. 환경변수 {ENV_BEARER}",
    )
    group.add_argument("--timeout", type=float, default=30.0, help="요청 타임아웃(초, 기본 30)")
    group.add_argument(
        "--mode",
        choices=("modern", "legacy"),
        default="modern",
        help="modern: 2026-07-28 무상태, legacy: initialize 핸드셰이크 (기본 modern)",
    )
    group.add_argument(
        "--protocol-version",
        help="모드의 프로토콜 버전 (기본 modern 2026-07-28, legacy 2025-11-25)",
    )
    group.add_argument(
        "--capabilities", metavar="JSON", help="모던 _meta의 clientCapabilities (기본 {})"
    )
    group.add_argument(
        "--cfg-json", metavar="JSON", help="단위전문가 서버 설정. base64url로 ?cfg=에 붙인다"
    )
    group.add_argument(
        "--scenario-header", metavar="NAME", help="X-MCP-Test-Scenario 헤더 값(단위전문가 서버용)"
    )
    group.add_argument("--json", action="store_true", help="기계 판독용 JSON 출력")
    group.add_argument("-v", "--verbose", action="store_true", help="전체 헤더와 바디 출력")
    return parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unit-expert-client",
        description="MCP 테스트 클라이언트: 게이트웨이나 서버에 modern(2026-07-28)/legacy(2025-xx) 클라이언트로 붙는다.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    parent = common_options()

    call = commands.add_parser(
        "call", parents=[parent], help="단발 요청", description="단발 JSON-RPC 요청을 보낸다."
    )
    call.add_argument(
        "method", help="JSON-RPC method (예: server/discover, tools/list, tools/call, initialize)"
    )
    call.add_argument("--tool", help="tools/call의 params.name")
    call.add_argument("--args", metavar="JSON", help="tools/call의 params.arguments")
    call.add_argument(
        "--params", metavar="JSON", help="params 전체를 직접 지정(_meta는 자동으로 합쳐진다)"
    )
    call.add_argument(
        "--id", dest="request_id", metavar="X", help="JSON-RPC id (JSON으로 해석, 실패하면 문자열)"
    )
    call.add_argument(
        "--auto-input",
        action="store_true",
        help="input_required를 받으면 inputResponses를 채워 재요청",
    )
    call.add_argument(
        "--no-list", action="store_true", help="tools/call 전에 tools/list로 스키마를 조회하지 않음"
    )
    call.add_argument(
        "--http-method",
        default="POST",
        help="HTTP 메서드 (기본 POST. GET/DELETE는 바디 없이 보낸다)",
    )
    call.add_argument(
        "--no-init", action="store_true", help="legacy: initialize 없이 요청만 보낸다"
    )
    call.add_argument(
        "--reinit-on-404",
        action="store_true",
        help="legacy: 세션 404를 받으면 initialize 후 재시도",
    )
    broken = call.add_argument_group("위반 플래그(일부러 규격을 어긴 요청)")
    broken.add_argument(
        "--omit-meta-version", action="store_true", help="_meta에서 protocolVersion 제거"
    )
    broken.add_argument(
        "--omit-capabilities", action="store_true", help="_meta에서 clientCapabilities 제거"
    )
    broken.add_argument("--omit-meta", action="store_true", help="_meta 전체 제거")
    broken.add_argument("--meta-version", metavar="X", help="_meta의 protocolVersion만 X로")
    broken.add_argument(
        "--omit-version-header", action="store_true", help="MCP-Protocol-Version 헤더 제거"
    )
    broken.add_argument("--version-header", metavar="X", help="MCP-Protocol-Version 헤더만 X로")
    broken.add_argument("--omit-method-header", action="store_true", help="Mcp-Method 헤더 제거")
    broken.add_argument("--method-header", metavar="X", help="Mcp-Method 헤더를 X로")
    broken.add_argument("--omit-name-header", action="store_true", help="Mcp-Name 헤더 제거")
    broken.add_argument(
        "--name-header", metavar="X", help="Mcp-Name 헤더를 X로(인코딩 없이 그대로)"
    )
    broken.add_argument(
        "--omit-param-headers", action="store_true", help="Mcp-Param-* 자동 부착 끄기"
    )
    broken.add_argument(
        "--param-header",
        action="append",
        type=parse_param_header,
        default=[],
        metavar="NAME=VALUE",
        help="Mcp-Param-NAME을 VALUE로(그대로 전송, 반복 가능)",
    )
    broken.add_argument(
        "--session-id",
        metavar="X",
        help="Mcp-Session-Id 헤더. legacy에서는 initialize 없이 이 세션을 쓴다",
    )
    broken.add_argument("--accept", metavar="X", help="Accept 헤더를 X로")
    broken.add_argument(
        "--raw-body", metavar="FILE", help="바디를 파일 내용 그대로 전송('-'는 표준 입력)"
    )

    run = commands.add_parser(
        "run",
        parents=[parent],
        help="시나리오 실행",
        description="시나리오를 실행하고 기대값과 비교한다.",
    )
    run.add_argument(
        "names",
        nargs="*",
        default=["all"],
        metavar="시나리오|그룹|all",
        help=f"시나리오 이름, 그룹({', '.join(GROUPS)}), 또는 all (기본 all)",
    )
    run.add_argument(
        "--expect",
        choices=("server", "gateway"),
        default="server",
        help="기대값 기준. server: 스펙대로 동작하는 단위전문가 서버, gateway: 게이트웨이(MCP 프록시)",
    )
    run.add_argument("--tool", help="tools/call 시나리오에 쓸 툴 이름")
    run.add_argument("--args", metavar="JSON", help="--tool에 넘길 인자")
    run.add_argument(
        "--input-tool",
        metavar="NAME",
        help=f"input-required-roundtrip에 쓸 툴 이름 (기본: {DEFAULT_INPUT_TOOL}이 있으면 그 툴, 없으면 탐색)",
    )
    run.add_argument(
        "--header-tool",
        metavar="NAME",
        help=f"param-header-* 시나리오에 쓸 툴 이름 (기본: {DEFAULT_HEADER_TOOL}가 있으면 그 툴, 없으면 탐색)",
    )
    run.add_argument(
        "--no-inject",
        action="store_true",
        help="--expect server에서 시나리오가 필요한 서버 설정(session.validate 등)을 ?cfg=로 넣지 않는다",
    )
    run.add_argument(
        "--assume-era", choices=("modern", "legacy", "dual"), help="probe 없이 대상의 세대를 지정"
    )
    run.add_argument(
        "--expect-era",
        choices=("modern", "legacy", "dual"),
        help="compat-probe 판정이 이 값이어야 함",
    )
    run.add_argument(
        "--include-unconfirmed",
        action="store_true",
        help="--expect gateway에서 미확정 시나리오도 스펙 기대값으로 실행",
    )
    run.add_argument("--strict", action="store_true", help="WARN(SHOULD 불일치)도 실패로 처리")

    listing = commands.add_parser(
        "list", help="시나리오 목록", description="시나리오와 기대값, 근거를 보여준다."
    )
    listing.add_argument(
        "--expect",
        choices=("server", "gateway"),
        default="server",
        help="gateway면 게이트웨이 근거도 표시",
    )
    listing.add_argument("--json", action="store_true", help="JSON 출력")

    probing = commands.add_parser(
        "probe",
        parents=[parent],
        help="modern/legacy/dual 판정",
        description="스펙의 HTTP 폴백 절차로 대상을 판정한다.",
    )
    probing.add_argument(
        "--no-legacy-check",
        action="store_true",
        help="모던이 동작하면 initialize를 보내지 않는다(실제 듀얼 클라이언트와 같은 동작)",
    )
    return parser


def build_target(args: argparse.Namespace) -> Target:
    if not args.url:
        raise SystemExit(f"--url 또는 환경변수 {ENV_URL}이 필요함")
    return Target(
        url=args.url,
        headers=list(args.header),
        bearer=args.bearer or None,
        timeout=args.timeout,
        cfg=parse_json_arg(args.cfg_json, "--cfg-json"),
        scenario_header=args.scenario_header,
    )


def emit(args: argparse.Namespace, text: str, payload: Any) -> None:
    print(report.to_json(payload) if args.json else text)


# -- call ---------------------------------------------------------------------------
def read_raw_body(path: str | None) -> bytes | None:
    if path is None:
        return None
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as handle:
        return handle.read()


def call_params(args: argparse.Namespace) -> dict[str, Any]:
    params: dict[str, Any] = parse_json_arg(args.params, "--params") or {}
    if args.tool:
        params["name"] = args.tool
    arguments = parse_json_arg(args.args, "--args")
    if arguments is not None:
        params["arguments"] = arguments
    elif args.method == "tools/call" and "arguments" not in params:
        params["arguments"] = {}
    return params


def tweaks_from(args: argparse.Namespace) -> p.Tweaks:
    return p.Tweaks(
        omit_meta_version=args.omit_meta_version,
        omit_capabilities=args.omit_capabilities,
        omit_meta=args.omit_meta,
        meta_version=args.meta_version,
        omit_version_header=args.omit_version_header,
        version_header=args.version_header,
        omit_method_header=args.omit_method_header,
        method_header=args.method_header,
        omit_name_header=args.omit_name_header,
        name_header=args.name_header,
        omit_param_headers=args.omit_param_headers,
        param_headers=list(args.param_header),
        session_id=args.session_id,
        accept=args.accept,
        raw_body=read_raw_body(args.raw_body),
    )


def call_modern(args: argparse.Namespace, target: Target) -> list[str]:
    client = ModernClient(
        target,
        args.protocol_version or p.MODERN_VERSION,
        parse_json_arg(args.capabilities, "--capabilities"),
    )
    params = call_params(args)
    tweaks = tweaks_from(args)
    extra: dict[str, Any] = (
        {} if args.request_id is None else {"request_id": parse_id(args.request_id)}
    )
    notes: list[str] = []
    http_method = args.http_method.upper()
    if http_method != "POST":
        headers = [
            (name, value)
            for name, value in p.modern_headers(args.method, params, client.version, None, tweaks)
            if name != "Content-Type"
        ]
        target.send(http_method, headers, None, label=f"{http_method} (바디 없음)")
        return notes
    if args.method != "tools/call":
        client.request(args.method, params, tweaks=tweaks, **extra)
        return notes
    tool = None
    if not args.no_list and not args.omit_param_headers:
        client.load_tools()
        notes.extend(client.warnings)
        tool = client.find_tool(str(params.get("name")))
        if tool is None:
            notes.append(
                f"tools/list에 {params.get('name')!r}이 없어 Mcp-Param-*를 자동으로 붙이지 않음"
            )
    if set(params) - {"name", "arguments"}:
        client.request("tools/call", params, tool=tool, tweaks=tweaks, **extra)
    else:
        client.call_tool(
            str(params.get("name")),
            params.get("arguments"),
            tool=tool,
            auto_input=args.auto_input,
            tweaks=tweaks,
            **extra,
        )
    return notes


def call_legacy(args: argparse.Namespace, target: Target) -> list[str]:
    version = args.protocol_version or p.LATEST_LEGACY_VERSION
    client = LegacyClient(target, version, reinit_on_404=args.reinit_on_404)
    notes: list[str] = []
    ignored = [
        flag
        for flag in (
            "omit_meta_version",
            "omit_capabilities",
            "omit_meta",
            "meta_version",
            "omit_method_header",
            "method_header",
            "omit_name_header",
            "name_header",
            "omit_param_headers",
            "param_header",
        )
        if getattr(args, flag)
    ]
    if ignored:
        notes.append(
            "legacy 모드에서 무시한 플래그: "
            + ", ".join("--" + flag.replace("_", "-") for flag in ignored)
        )
    extra: dict[str, Any] = (
        {} if args.request_id is None else {"request_id": parse_id(args.request_id)}
    )
    http_method = args.http_method.upper()
    if args.method == "initialize" and http_method == "POST":
        client.initialize(send_initialized=False, **extra)
        return notes
    if args.session_id:
        client.session_id, client.negotiated = args.session_id, version
    elif not args.no_init:
        init = client.initialize()
        if init.status != 200 or init.result is None:
            notes.append("initialize가 실패해 요청을 보내지 않음")
            return notes
    version_header: Any = client.negotiated
    if args.omit_version_header:
        version_header = None
    elif args.version_header:
        version_header = args.version_header
    if http_method != "POST":
        headers = [
            (name, value)
            for name, value in client.headers(version_header=version_header, accept=args.accept)
            if name != "Content-Type"
        ]
        target.send(http_method, headers, None, label=f"{http_method} (바디 없음)")
        return notes
    client.request(
        args.method,
        call_params(args) or None,
        version_header=version_header,
        accept=args.accept,
        raw_body=read_raw_body(args.raw_body),
        **extra,
    )
    if client.reinitialized:
        notes.append("세션 404를 받아 initialize를 다시 수행함")
    return notes


def command_call(args: argparse.Namespace) -> int:
    target = build_target(args)
    exchanges: list[Exchange] = []
    target.recorders.append(exchanges.append)
    notes = call_modern(args, target) if args.mode == "modern" else call_legacy(args, target)
    if args.json:
        print(
            report.to_json(
                {"notes": notes, "exchanges": [exchange.detail() for exchange in exchanges]}
            )
        )
    else:
        for index, exchange in enumerate(exchanges):
            last = index == len(exchanges) - 1
            # Preparatory requests are summarised; the last one is shown in full.
            print(
                report.format_exchange(exchange, args.verbose)
                if last or args.verbose
                else report.summary_line(exchange)
            )
            if not last and args.verbose:
                print()
        for note in notes:
            print(report.escape_control(f"참고: {note}"))
    if not exchanges or exchanges[-1].error:
        return EXIT_USAGE
    return 0


# -- run / list / probe -----------------------------------------------------------------
def command_run(args: argparse.Namespace) -> int:
    target = build_target(args)
    options = Options(
        profile=args.expect,
        tool=args.tool,
        args=parse_json_arg(args.args, "--args"),
        input_tool=args.input_tool,
        header_tool=args.header_tool,
        inject=not args.no_inject,
        assume_era=args.assume_era,
        expect_era=args.expect_era,
        include_unconfirmed=args.include_unconfirmed,
        protocol_version=args.protocol_version if args.mode == "modern" else None,
        legacy_version=args.protocol_version if args.mode == "legacy" else None,
        capabilities=parse_json_arg(args.capabilities, "--capabilities"),
    )
    runner = Runner(target, options, SCENARIOS)
    try:
        results = runner.run(args.names)
    except KeyError as error:
        print(f"모르는 시나리오 또는 그룹: {error.args[0]} (목록은 list 명령)", file=sys.stderr)
        return EXIT_USAGE
    verdict = runner._era.verdict if runner._era is not None else "판정 안 함"
    header = f"대상: {args.url}  (expect={args.expect}, probe={verdict})"
    meta = {"url": args.url, "expect": args.expect, "probe": verdict}
    emit(
        args,
        report.format_results(results, header, args.verbose, args.strict),
        report.results_json(results, meta, args.verbose, args.strict),
    )
    return exit_code(results, args.strict)


def command_list(args: argparse.Namespace) -> int:
    emit(args, report.format_scenarios(SCENARIOS, args.expect), report.scenarios_json(SCENARIOS))
    return 0


def command_probe(args: argparse.Namespace) -> int:
    target = build_target(args)
    result = probe(
        target,
        check_legacy=not args.no_legacy_check,
        version=args.protocol_version or p.MODERN_VERSION,
    )
    emit(args, report.format_probe(result, args.verbose), result.to_json())
    return 0 if result.verdict in ("modern", "legacy", "dual") else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "call": command_call,
        "run": command_run,
        "list": command_list,
        "probe": command_probe,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
