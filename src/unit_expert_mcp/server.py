"""Minimal Streamable HTTP MCP server for PlayMCP."""

from __future__ import annotations

import json
import os
import time
import uuid
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

SERVER_NAME = "unitExpert"
SERVER_VERSION = "1.0.0"
SERVICE_NAME = "Unit Expert(단위전문가)"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION = "2025-11-25"
TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"
DEFAULT_DELAY_SECONDS = 5.0
PUBLIC_MCP_URL = "https://unit-expert-mcp.onrender.com/mcp"

SESSIONS: set[str] = set()
SCENARIO_LOCK = Lock()
ACTIVE_SCENARIO = "ok"

SCENARIO_TITLES = {
    "ok": "정상 응답",
    "auth-401": "인증 조건 - 401",
    "auth-403": "인증 조건 - 403",
    "unsupported-min-version": "MCP 버전 조건 - 최소 지원 버전",
    "tools-list-error": "툴 목록 조건 - JSON-RPC 에러",
    "tools-list-null": "툴 목록 조건 - null 반환",
    "tools-list-empty": "툴 목록 조건 - 빈 배열 반환",
    "duplicate-tool-name": "툴 이름 조건 - 중복",
    "too-many-tools": "툴 개수 조건 - 최대 개수",
    "invalid-tool-name-char": "툴 이름 조건 - 허용 문자",
    "invalid-tool-name-length": "툴 이름 조건 - 길이",
    "missing-name": "툴 필수 속성 - name",
    "missing-description": "툴 필수 속성 - description",
    "missing-input-schema": "툴 필수 속성 - inputSchema",
    "missing-annotations": "툴 필수 속성 - annotations",
    "forbidden-kakao-name": "툴 이름 조건 - 금지어",
    "long-description": "툴 설명 조건 - 길이",
    "missing-service-name-in-description": "툴 설명 조건 - 서비스명",
    "incomplete-annotations": "툴 annotations 조건 - 필수 힌트",
    "delayed-response": "응답속도 조건 - 지연",
}

SCENARIO_DESCRIPTIONS = {
    "ok": "정상 Unit Expert 도구 목록을 반환합니다.",
    "auth-401": "JSON-RPC 처리 전에 401 Unauthorized를 반환합니다.",
    "auth-403": "JSON-RPC 처리 전에 403 Forbidden을 반환합니다.",
    "unsupported-min-version": "최소 지원 버전보다 낮은 protocolVersion 2024-11-05를 반환합니다.",
    "tools-list-error": "tools/list에서 JSON-RPC error를 반환합니다.",
    "tools-list-null": "tools/list에서 tools: null을 반환합니다.",
    "tools-list-empty": "tools/list에서 tools: []를 반환합니다.",
    "duplicate-tool-name": "동일한 name을 가진 중복 tool을 반환합니다.",
    "too-many-tools": "도구 21개를 반환합니다.",
    "invalid-tool-name-char": "허용되지 않는 문자가 포함된 tool name을 반환합니다.",
    "invalid-tool-name-length": "129자 길이의 tool name을 반환합니다.",
    "missing-name": "name이 없는 tool을 반환합니다.",
    "missing-description": "description이 없는 tool을 반환합니다.",
    "missing-input-schema": "inputSchema가 없는 tool을 반환합니다.",
    "missing-annotations": "annotations가 없는 tool을 반환합니다.",
    "forbidden-kakao-name": "금지어가 포함된 tool name을 반환합니다.",
    "long-description": "1,051자 description을 반환합니다.",
    "missing-service-name-in-description": "서비스명이 빠진 description을 반환합니다.",
    "incomplete-annotations": "필수 필드가 빠진 annotations를 반환합니다.",
    "delayed-response": "OPTIONS가 아닌 /mcp 요청을 5초 지연시킵니다.",
}

SCENARIO_GROUPS = (
    ("기본", ("ok",)),
    (
        "서버 error",
        (
            "auth-401",
            "auth-403",
            "unsupported-min-version",
            "tools-list-error",
            "tools-list-null",
            "tools-list-empty",
            "too-many-tools",
            "delayed-response",
        ),
    ),
    (
        "tool error",
        (
            "duplicate-tool-name",
            "invalid-tool-name-char",
            "invalid-tool-name-length",
            "missing-name",
            "missing-description",
            "missing-input-schema",
            "missing-annotations",
            "forbidden-kakao-name",
            "long-description",
            "missing-service-name-in-description",
            "incomplete-annotations",
        ),
    ),
)

LENGTH_FACTORS = {
    "mm": 0.001,
    "cm": 0.01,
    "m": 1.0,
    "km": 1000.0,
    "in": 0.0254,
    "ft": 0.3048,
    "yd": 0.9144,
    "mi": 1609.344,
}

WEIGHT_FACTORS = {
    "mg": 0.000001,
    "g": 0.001,
    "kg": 1.0,
    "t": 1000.0,
    "oz": 0.028349523125,
    "lb": 0.45359237,
}

AREA_FACTORS = {
    "mm2": 0.000001,
    "cm2": 0.0001,
    "m2": 1.0,
    "km2": 1_000_000.0,
    "in2": 0.00064516,
    "ft2": 0.09290304,
    "yd2": 0.83612736,
    "acre": 4046.8564224,
}

VOLUME_FACTORS = {
    "ml": 0.001,
    "l": 1.0,
    "m3": 1000.0,
    "in3": 0.016387064,
    "ft3": 28.316846592,
    "cup": 0.2365882365,
    "pt": 0.473176473,
    "qt": 0.946352946,
    "gal": 3.785411784,
    "floz": 0.0295735295625,
}

TEMPERATURE_UNITS = ("c", "f", "k")

ALIASES = {
    "millimeter": "mm",
    "millimeters": "mm",
    "centimeter": "cm",
    "centimeters": "cm",
    "meter": "m",
    "meters": "m",
    "metre": "m",
    "metres": "m",
    "kilometer": "km",
    "kilometers": "km",
    "kilometre": "km",
    "kilometres": "km",
    "inch": "in",
    "inches": "in",
    "foot": "ft",
    "feet": "ft",
    "yard": "yd",
    "yards": "yd",
    "mile": "mi",
    "miles": "mi",
    "milligram": "mg",
    "milligrams": "mg",
    "gram": "g",
    "grams": "g",
    "kilogram": "kg",
    "kilograms": "kg",
    "ton": "t",
    "tons": "t",
    "tonne": "t",
    "tonnes": "t",
    "ounce": "oz",
    "ounces": "oz",
    "pound": "lb",
    "pounds": "lb",
    "celsius": "c",
    "centigrade": "c",
    "fahrenheit": "f",
    "kelvin": "k",
    "sqmm": "mm2",
    "squaremillimeter": "mm2",
    "squaremillimeters": "mm2",
    "sqcm": "cm2",
    "squarecentimeter": "cm2",
    "squarecentimeters": "cm2",
    "sqm": "m2",
    "squaremeter": "m2",
    "squaremeters": "m2",
    "squaremetre": "m2",
    "squaremetres": "m2",
    "sqkm": "km2",
    "squarekilometer": "km2",
    "squarekilometers": "km2",
    "sqin": "in2",
    "squareinch": "in2",
    "squareinches": "in2",
    "sqft": "ft2",
    "squarefoot": "ft2",
    "squarefeet": "ft2",
    "sqyd": "yd2",
    "squareyard": "yd2",
    "squareyards": "yd2",
    "acres": "acre",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "cubicmeter": "m3",
    "cubicmeters": "m3",
    "cubicmetre": "m3",
    "cubicmetres": "m3",
    "cubicinch": "in3",
    "cubicinches": "in3",
    "cubicfoot": "ft3",
    "cubicfeet": "ft3",
    "cups": "cup",
    "pint": "pt",
    "pints": "pt",
    "quart": "qt",
    "quarts": "qt",
    "gallon": "gal",
    "gallons": "gal",
    "fluidounce": "floz",
    "fluidounces": "floz",
}

SUPPORTED_UNITS = {
    "length": tuple(LENGTH_FACTORS),
    "weight": tuple(WEIGHT_FACTORS),
    "temperature": TEMPERATURE_UNITS,
    "area": tuple(AREA_FACTORS),
    "volume": tuple(VOLUME_FACTORS),
}


def tools() -> list[dict[str, Any]]:
    value_unit_schema = {
        "type": "object",
        "properties": {
            "value": {"type": "number", "description": "Numeric value to convert."},
            "from_unit": {"type": "string", "description": "Source unit."},
            "to_unit": {"type": "string", "description": "Target unit."},
        },
        "required": ["value", "from_unit", "to_unit"],
        "additionalProperties": False,
    }
    return [
        {
            "name": "convert_length",
            "description": (
                f"{SERVICE_NAME} converts length values between mm, cm, m, km, in, ft, yd, "
                "and mi."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Length"),
        },
        {
            "name": "convert_weight",
            "description": f"{SERVICE_NAME} converts weight values between mg, g, kg, t, oz, and lb.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Weight"),
        },
        {
            "name": "convert_temperature",
            "description": f"{SERVICE_NAME} converts temperature values between c, f, and k.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Temperature"),
        },
        {
            "name": "convert_area",
            "description": (
                f"{SERVICE_NAME} converts area values between mm2, cm2, m2, km2, in2, ft2, "
                "yd2, and acre."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Area"),
        },
        {
            "name": "convert_volume",
            "description": (
                f"{SERVICE_NAME} converts volume values between ml, l, m3, in3, ft3, cup, "
                "pt, qt, gal, and floz."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Volume"),
        },
        {
            "name": "list_supported_units",
            "description": f"{SERVICE_NAME} lists supported canonical units by conversion category.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "annotations": annotations("List Supported Units"),
        },
    ]


def annotations(title: str) -> dict[str, bool | str]:
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
        "idempotentHint": True,
    }


def normalize_scenario(raw_scenario: str | None) -> str:
    if not raw_scenario:
        return "ok"
    return raw_scenario.strip().lower() or "ok"


def get_active_scenario() -> str:
    with SCENARIO_LOCK:
        return ACTIVE_SCENARIO


def set_active_scenario(raw_scenario: str | None) -> str:
    scenario = normalize_scenario(raw_scenario)
    if scenario not in SCENARIO_DESCRIPTIONS:
        raise ValueError(f"unknown scenario '{scenario}'")

    global ACTIVE_SCENARIO
    with SCENARIO_LOCK:
        ACTIVE_SCENARIO = scenario
    return scenario


def resolve_scenario(raw_header_scenario: str | None) -> str:
    if raw_header_scenario is not None and raw_header_scenario.strip():
        return normalize_scenario(raw_header_scenario)
    return get_active_scenario()


def valid_tool(name: str, description: str | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"{SERVICE_NAME} converts common measurement units.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        "annotations": annotations(name.replace("_", " ").title()),
    }


def tools_for_scenario(scenario: str) -> dict[str, Any] | None:
    match scenario:
        case "tools-list-null":
            return {"tools": None}
        case "tools-list-empty":
            return {"tools": []}
        case "duplicate-tool-name":
            return {"tools": [valid_tool("search_place"), valid_tool("search_place")]}
        case "too-many-tools":
            return {"tools": [valid_tool(f"tool_{index}") for index in range(21)]}
        case "invalid-tool-name-char":
            return {"tools": [valid_tool("search place!")]}
        case "invalid-tool-name-length":
            return {"tools": [valid_tool("a" * 129)]}
        case "missing-name":
            tool = valid_tool("search_place")
            tool.pop("name")
            return {"tools": [tool]}
        case "missing-description":
            tool = valid_tool("search_place")
            tool.pop("description")
            return {"tools": [tool]}
        case "missing-input-schema":
            tool = valid_tool("search_place")
            tool.pop("inputSchema")
            return {"tools": [tool]}
        case "missing-annotations":
            tool = valid_tool("search_place")
            tool.pop("annotations")
            return {"tools": [tool]}
        case "forbidden-kakao-name":
            return {"tools": [valid_tool("kakao_search")]}
        case "long-description":
            return {"tools": [valid_tool("search_place", "a" * 1051)]}
        case "missing-service-name-in-description":
            return {"tools": [valid_tool("search_place", "Search places nearby.")]}
        case "incomplete-annotations":
            tool = valid_tool("search_place")
            tool["annotations"] = {
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            }
            return {"tools": [tool]}
        case _:
            return None


def handle_json_rpc(
    payload: Any,
    scenario: str = "ok",
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    scenario = normalize_scenario(scenario)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return 400, {}, json_rpc_error(None, -32600, "Invalid Request")

    request_id = payload.get("id")
    method = payload.get("method")
    is_notification = "id" not in payload

    if method == "initialize":
        session_id = str(uuid.uuid4())
        SESSIONS.add(session_id)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        if scenario == "unsupported-min-version":
            protocol_version = "2024-11-05"
        capabilities: dict[str, Any] = {"tools": {"listChanged": True}}
        result = {
            "protocolVersion": protocol_version,
            "capabilities": capabilities,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
        return 200, {"Mcp-Session-Id": session_id}, json_rpc_result(request_id, result)

    if method == "notifications/initialized":
        return 202, {}, None

    if method == "ping":
        return 202 if is_notification else 200, {}, None if is_notification else json_rpc_result(request_id, {})

    if method == "tools/list":
        if scenario == "tools-list-error":
            return 200, {}, json_rpc_error(request_id, -32603, "Injected tools/list failure")
        scenario_tools = tools_for_scenario(scenario)
        return 200, {}, json_rpc_result(request_id, scenario_tools or {"tools": tools()})

    if method == "tools/call":
        result = call_tool(payload.get("params"))
        return 200, {}, json_rpc_result(request_id, result)

    if is_notification:
        return 202, {}, None
    return 200, {}, json_rpc_error(request_id, -32601, f"Method not found: {method}")


def call_tool(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
        return tool_error("tools/call requires a tool name.")

    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    try:
        if params["name"] == "convert_length":
            return tool_success(convert_with_factor(arguments, "length", LENGTH_FACTORS))
        if params["name"] == "convert_weight":
            return tool_success(convert_with_factor(arguments, "weight", WEIGHT_FACTORS))
        if params["name"] == "convert_temperature":
            return tool_success(convert_temperature(arguments))
        if params["name"] == "convert_area":
            return tool_success(convert_with_factor(arguments, "area", AREA_FACTORS))
        if params["name"] == "convert_volume":
            return tool_success(convert_with_factor(arguments, "volume", VOLUME_FACTORS))
        if params["name"] == "list_supported_units":
            text = "\n".join(
                f"{category}: {', '.join(units)}"
                for category, units in SUPPORTED_UNITS.items()
            )
            return {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }
        return tool_error(f"Unknown tool: {params['name']}")
    except ValueError as error:
        return tool_error(str(error))


def convert_with_factor(
    arguments: dict[str, Any],
    category: str,
    factors: dict[str, float],
) -> dict[str, Any]:
    value = validate_value(arguments.get("value"))
    from_unit = normalize_unit(arguments.get("from_unit"))
    to_unit = normalize_unit(arguments.get("to_unit"))
    ensure_supported(from_unit, factors, category)
    ensure_supported(to_unit, factors, category)

    output_value = value * factors[from_unit] / factors[to_unit]
    return {
        "input_value": value,
        "input_unit": from_unit,
        "output_value": output_value,
        "output_unit": to_unit,
        "category": category,
    }


def convert_temperature(arguments: dict[str, Any]) -> dict[str, Any]:
    value = validate_value(arguments.get("value"))
    from_unit = normalize_unit(arguments.get("from_unit"))
    to_unit = normalize_unit(arguments.get("to_unit"))
    ensure_supported_temperature(from_unit)
    ensure_supported_temperature(to_unit)

    celsius = temperature_to_celsius(value, from_unit)
    output_value = celsius_to_temperature(celsius, to_unit)
    return {
        "input_value": value,
        "input_unit": from_unit,
        "output_value": output_value,
        "output_unit": to_unit,
        "category": "temperature",
    }


def validate_value(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be a finite number.") from error
    if not isfinite(number):
        raise ValueError("value must be a finite number.")
    return number


def normalize_unit(unit: Any) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise ValueError("unit must be a non-empty string.")
    normalized = unit.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    return ALIASES.get(normalized, normalized)


def ensure_supported(unit: str, factors: dict[str, float], category: str) -> None:
    if unit not in factors:
        raise ValueError(
            f"unsupported {category} unit '{unit}'. Supported units: {', '.join(factors)}."
        )


def ensure_supported_temperature(unit: str) -> None:
    if unit not in TEMPERATURE_UNITS:
        raise ValueError(
            f"unsupported temperature unit '{unit}'. Supported units: {', '.join(TEMPERATURE_UNITS)}."
        )


def temperature_to_celsius(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0
    if unit == "k":
        return value - 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'.")


def celsius_to_temperature(value: float, unit: str) -> float:
    if unit == "c":
        return value
    if unit == "f":
        return (value * 9.0 / 5.0) + 32.0
    if unit == "k":
        return value + 273.15
    raise ValueError(f"unsupported temperature unit '{unit}'.")


def tool_success(result: dict[str, Any]) -> dict[str, Any]:
    text = (
        f"{format_number(result['input_value'])} {result['input_unit']} = "
        f"{format_number(result['output_value'])} {result['output_unit']}"
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return str(float(f"{value:.12g}"))


def json_rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def json_rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def scenario_group_label(scenario: str) -> str:
    for label, scenarios in SCENARIO_GROUPS:
        if scenario in scenarios:
            return label
    return "기타"


def render_scenario_page(message: str | None = None, error: str | None = None) -> str:
    active_scenario = get_active_scenario()
    active_title = SCENARIO_TITLES[active_scenario]
    active_group = scenario_group_label(active_scenario)
    row_sections: list[str] = []
    option_sections: list[str] = []
    for group_label, scenarios in SCENARIO_GROUPS:
        row_sections.append(
            f'<tr class="section-row"><td colspan="2">[{escape(group_label)}]</td></tr>'
        )
        option_items: list[str] = []
        for scenario in scenarios:
            row_sections.append(
                f"""
        <tr
          data-scenario="{escape(scenario)}"
          data-title="{escape(SCENARIO_TITLES[scenario])}"
          data-group="{escape(group_label)}"
          class="{'active' if scenario == active_scenario else ''}"
        >
          <td>
            <strong>{escape(SCENARIO_TITLES[scenario])}</strong>
            <code>{escape(scenario)}</code>
          </td>
          <td>{escape(SCENARIO_DESCRIPTIONS[scenario])}</td>
        </tr>
        """
            )
            option_items.append(
                f'<option value="{escape(scenario)}" '
                f'{"selected" if scenario == active_scenario else ""}>'
                f"{escape(SCENARIO_TITLES[scenario])}</option>"
            )
        option_sections.append(
            f'<optgroup label="{escape(group_label)}">{"".join(option_items)}</optgroup>'
        )
    rows = "\n".join(row_sections)
    options = "\n".join(option_sections)
    notice = ""
    if message:
        notice = f'<div class="notice success">{escape(message)}</div>'
    if error:
        notice = f'<div class="notice error">{escape(error)}</div>'

    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP 시나리오 제어</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f8fa;
      --surface: #ffffff;
      --text: #151922;
      --muted: #5f6875;
      --line: #dfe3ea;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --danger: #b42318;
      --success: #146c43;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
    }}
    main {{
      width: min(1280px, calc(100vw - 32px));
      margin: 32px auto;
    }}
    header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .endpoint {{
      color: var(--muted);
      font-size: 13px;
      min-width: min(520px, 100%);
    }}
    .endpoint-label {{
      margin-bottom: 6px;
      color: var(--muted);
      font-weight: 600;
    }}
    .endpoint-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      align-items: center;
    }}
    .endpoint-row code {{
      width: 100%;
      overflow-wrap: anywhere;
    }}
    .panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 16px;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(360px, 520px);
      gap: 16px;
      align-items: start;
    }}
    .status {{
      display: grid;
      grid-template-columns: 160px 1fr;
      gap: 8px 16px;
      margin-bottom: 16px;
      font-size: 14px;
    }}
    .status span:nth-child(odd) {{ color: var(--muted); }}
    code {{
      display: inline-block;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      background: #f1f3f6;
      border: 1px solid #e4e7ec;
      border-radius: 4px;
      padding: 2px 5px;
    }}
    td strong {{
      display: block;
      margin-bottom: 5px;
      font-size: 14px;
    }}
    form {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) auto;
      gap: 10px;
      align-items: center;
    }}
    select, button {{
      height: 40px;
      font: inherit;
      border-radius: 6px;
    }}
    select {{
      width: 100%;
      border: 1px solid #cfd5df;
      background: #fff;
      color: var(--text);
      padding: 0 10px;
    }}
    button {{
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      padding: 0 14px;
      cursor: pointer;
      font-weight: 600;
    }}
    .copy-button {{
      height: 30px;
      padding: 0 10px;
      font-size: 13px;
      white-space: nowrap;
    }}
    button.secondary {{
      background: #fff;
      color: var(--accent-strong);
    }}
    .notice {{
      border-radius: 6px;
      padding: 10px 12px;
      margin-bottom: 14px;
      font-size: 14px;
      border: 1px solid;
    }}
    .notice.success {{
      color: var(--success);
      background: #edf8f2;
      border-color: #b7e2c8;
    }}
    .notice.error {{
      color: var(--danger);
      background: #fff0ed;
      border-color: #f2b8b0;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      text-align: left;
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      background: #f5f6f8;
      font-weight: 600;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    tr.active td {{ background: #ecfdf9; }}
    tr.section-row td {{
      background: #111827;
      color: #d1fae5;
      border-bottom-color: #111827;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .terminal {{
      background: #111827;
      color: #d1fae5;
      border: 1px solid #0f172a;
      border-radius: 8px;
      min-height: 520px;
      overflow: hidden;
    }}
    .terminal-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 10px 12px;
      color: #d1d5db;
      background: #0f172a;
      border-bottom: 1px solid #1f2937;
      font-size: 13px;
    }}
    .terminal pre {{
      margin: 0;
      padding: 14px;
      min-height: 474px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
    }}
    @media (max-width: 680px) {{
      header {{ display: block; }}
      .endpoint {{ margin-top: 8px; }}
      .endpoint-row {{ grid-template-columns: 1fr; }}
      .status {{ grid-template-columns: 1fr; }}
      form {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
      .workspace {{ grid-template-columns: 1fr; }}
      .terminal {{ min-height: 360px; }}
      .terminal pre {{ min-height: 314px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Unit Expert MCP 시나리오 제어</h1>
      <div class="endpoint">
        <div class="endpoint-label">MCP URL</div>
        <div class="endpoint-row">
          <code id="mcpUrl">{escape(PUBLIC_MCP_URL)}</code>
          <button id="copyMcpUrl" class="copy-button" type="button">복사</button>
        </div>
      </div>
    </header>
    <section class="panel">
      {notice}
      <div class="status">
        <span>현재 시나리오</span>
        <span>
          <strong id="activeTitle">{escape(active_title)}</strong>
          <code id="activeGroup">{escape(active_group)}</code>
          <code id="activeScenario">{escape(active_scenario)}</code>
        </span>
      </div>
      <form id="scenarioForm" method="post" action="/scenario">
        <select id="scenarioSelect" name="scenario" aria-label="시나리오">{options}</select>
        <button type="submit">적용</button>
      </form>
    </section>
    <div class="workspace">
      <table>
        <thead>
          <tr>
            <th>시나리오</th>
            <th>효과</th>
          </tr>
        </thead>
        <tbody id="scenarioRows">
          {rows}
        </tbody>
      </table>
      <aside class="terminal" aria-label="MCP 응답 미리보기">
        <div class="terminal-header">
          <span>실제 응답 미리보기</span>
          <span><span id="terminalGroup">{escape(active_group)}</span> · <span id="terminalScenario">{escape(active_title)}</span></span>
        </div>
        <pre id="responsePreview">Loading...</pre>
      </aside>
    </div>
  </main>
  <script>
    const form = document.getElementById("scenarioForm");
    const select = document.getElementById("scenarioSelect");
    const activeScenario = document.getElementById("activeScenario");
    const activeGroup = document.getElementById("activeGroup");
    const activeTitle = document.getElementById("activeTitle");
    const terminalGroup = document.getElementById("terminalGroup");
    const terminalScenario = document.getElementById("terminalScenario");
    const preview = document.getElementById("responsePreview");
    const copyMcpUrl = document.getElementById("copyMcpUrl");
    const mcpUrl = document.getElementById("mcpUrl");
    const rows = Array.from(document.querySelectorAll("[data-scenario]"));

    function pretty(value) {{
      try {{
        return JSON.stringify(value, null, 2);
      }} catch (error) {{
        return String(value);
      }}
    }}

    function formatHttp(label, response) {{
      const body = typeof response.body === "string" ? response.body : pretty(response.body);
      return [
        `$ ${{label}}`,
        `HTTP ${{response.status}}`,
        `content-type: ${{response.contentType || "-"}}`,
        "",
        body
      ].join("\\n");
    }}

    function markActive(scenario) {{
      const row = rows.find((candidate) => candidate.dataset.scenario === scenario);
      const title = row ? row.dataset.title : scenario;
      const group = row ? row.dataset.group : "기타";
      activeScenario.textContent = scenario;
      activeGroup.textContent = group;
      activeTitle.textContent = title;
      terminalGroup.textContent = group;
      terminalScenario.textContent = title;
      rows.forEach((row) => {{
        row.classList.toggle("active", row.dataset.scenario === scenario);
      }});
    }}

    async function postMcp(payload) {{
      const response = await fetch("/mcp", {{
        method: "POST",
        headers: {{
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          "MCP-Protocol-Version": "2025-03-26"
        }},
        body: JSON.stringify(payload)
      }});
      const text = await response.text();
      let body = text;
      try {{
        body = JSON.parse(text);
      }} catch (error) {{}}
      return {{
        status: response.status,
        contentType: response.headers.get("content-type"),
        body
      }};
    }}

    async function refreshPreview() {{
      const scenario = activeScenario.textContent;
      const title = activeTitle.textContent;
      const group = activeGroup.textContent;
      preview.textContent = `$ 구분: ${{group}}\\n$ 시나리오: ${{title}} (${{scenario}})\\n$ POST /mcp initialize\\n...`;
      try {{
        const initialize = await postMcp({{
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {{
            protocolVersion: "2025-03-26",
            capabilities: {{}},
            clientInfo: {{ name: "scenario-ui", version: "1.0" }}
          }}
        }});
        const toolsList = await postMcp({{
          jsonrpc: "2.0",
          id: 2,
          method: "tools/list",
          params: {{}}
        }});
        preview.textContent = [
          `$ 구분: ${{group}}`,
          `$ 시나리오: ${{title}} (${{scenario}})`,
          formatHttp("POST /mcp initialize", initialize),
          "",
          formatHttp("POST /mcp tools/list", toolsList)
        ].join("\\n");
      }} catch (error) {{
        preview.textContent = `$ 시나리오: ${{title}} (${{scenario}})\\n${{error && error.stack ? error.stack : error}}`;
      }}
    }}

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      const scenario = select.value;
      const title = select.options[select.selectedIndex].text;
      const group = select.options[select.selectedIndex].parentElement.label;
      preview.textContent = `$ 구분: ${{group}}\\n$ 시나리오: ${{title}} (${{scenario}})\\n$ POST /scenario\\n...`;
      const response = await fetch("/scenario", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ scenario }})
      }});
      const payload = await response.json();
      if (!response.ok || !payload.ok) {{
        preview.textContent = pretty({{ status: response.status, body: payload }});
        return;
      }}
      markActive(payload.scenario);
      refreshPreview();
    }});

    copyMcpUrl.addEventListener("click", async () => {{
      const text = mcpUrl.textContent.trim();
      try {{
        await navigator.clipboard.writeText(text);
        copyMcpUrl.textContent = "복사됨";
        setTimeout(() => {{
          copyMcpUrl.textContent = "복사";
        }}, 1200);
      }} catch (error) {{
        copyMcpUrl.textContent = "실패";
        setTimeout(() => {{
          copyMcpUrl.textContent = "복사";
        }}, 1200);
      }}
    }});

    refreshPreview();
  </script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UnitExpertMCP/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json(200, {"ok": True, "service": SERVER_NAME})
            return

        if path in {"/", "/scenario"}:
            self.send_html(200, render_scenario_page())
            return

        if path != "/mcp":
            self.send_text(404, "Not found")
            return

        scenario = self.request_scenario()
        if self.handle_pre_json_rpc_scenario(scenario):
            return

        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept header. Expected TEXT_EVENT_STREAM")
            return

        session_id = self.headers.get("mcp-session-id")
        if not session_id:
            self.send_text(400, "Session ID required in mcp-session-id header")
            return

        self.send_response(HTTPStatus.OK)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": connected\n\n")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/scenario":
            self.handle_scenario_update()
            return

        if path != "/mcp":
            self.send_text(404, "Not found")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)

        scenario = self.request_scenario()
        if self.handle_pre_json_rpc_scenario(scenario):
            return

        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON")
            return

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_json(400, json_rpc_error(None, -32700, "Parse error"))
            return

        status, extra_headers, response = handle_json_rpc(payload, scenario)
        if response is None:
            self.send_response(status)
            self.send_cors_headers()
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_json(status, response, extra_headers)

    def handle_scenario_update(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        content_type = self.headers.get("Content-Type", "")

        try:
            if "application/json" in content_type:
                payload = json.loads(raw_body or b"{}")
                raw_scenario = payload.get("scenario") if isinstance(payload, dict) else None
            else:
                form = parse_qs(raw_body.decode("utf-8"))
                raw_scenario = form.get("scenario", ["ok"])[0]

            scenario = set_active_scenario(raw_scenario)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            if "application/json" in content_type:
                self.send_json(400, {"ok": False, "error": str(error)})
                return
            self.send_html(400, render_scenario_page(error=str(error)))
            return

        if "application/json" in content_type:
            self.send_json(200, {"ok": True, "scenario": scenario})
            return

        self.send_html(200, render_scenario_page(message=f"Applied: {scenario}"))

    def send_json(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/plain;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "text/html;charset=UTF-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin") or "*"
        requested_headers = self.headers.get("Access-Control-Request-Headers")
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, DELETE")
        self.send_header(
            "Access-Control-Allow-Headers",
            requested_headers
            or "authorization,content-type,accept,mcp-protocol-version,mcp-session-id,x-mcp-test-scenario,*",
        )
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id,mcp-session-id,*")
        self.send_header("Vary", "Origin")

    def request_scenario(self) -> str:
        return resolve_scenario(self.headers.get(TEST_SCENARIO_HEADER))

    def handle_pre_json_rpc_scenario(self, scenario: str) -> bool:
        if scenario == "delayed-response":
            time.sleep(DEFAULT_DELAY_SECONDS)
            return False
        if scenario == "auth-401":
            self.send_text(401, "Unauthorized")
            return True
        if scenario == "auth-403":
            self.send_text(403, "Forbidden")
            return True
        return False

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{SERVER_NAME} listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
