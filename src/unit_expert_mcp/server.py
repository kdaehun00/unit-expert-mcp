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
SUPPORTED_PROTOCOL_VERSIONS = ("2024-03-26", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION_CHOICES = (
    ("2024-03-26", "2024-03-26 (스펙에 아예 없는 버전)"),
    ("2024-11-05", "2024-11-05 (최소 지원 미만)"),
    ("2025-03-26", "2025-03-26 (최소 지원)"),
    ("2025-06-18", "2025-06-18 (지원)"),
    ("2025-11-25", "2025-11-25 (최대 지원)"),
)
TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"
DEFAULT_DELAY_SECONDS = 5.0
PUBLIC_MCP_URL = "https://unit-expert-mcp.onrender.com/mcp"

SESSIONS: set[str] = set()
SCENARIO_LOCK = Lock()
ACTIVE_SCENARIO = "ok"
ACTIVE_CONFIG: dict[str, Any] = {}

SCENARIO_TITLES = {
    "ok": "정상 응답",
    "auth-401": "인증 조건 - 401 반환",
    "auth-403": "인증 조건 - 403 반환",
    "unsupported-min-version": "MCP 버전 조건 - 최소 지원 버전",
    "tools-list-error": "툴 목록 조건 - JSON-RPC 에러",
    "tools-list-null": "툴 목록 조건 - null 반환",
    "tools-list-empty": "툴 목록 조건 - 빈 배열 반환",
    "duplicate-tool-name": "툴 이름 조건 - 중복되는 툴",
    "too-many-tools": "툴 개수 조건 - 최대 개수",
    "invalid-tool-name-char": "툴 이름 조건 - 허용되지 않는 문자 사용",
    "invalid-tool-name-length": "툴 이름 조건 - 길이 초과",
    "missing-name": "툴 필수 속성 - name 없음",
    "missing-description": "툴 필수 속성 - description 없음",
    "missing-input-schema": "툴 필수 속성 - inputSchema 없음",
    "missing-annotations": "툴 필수 속성 - annotations 없음",
    "forbidden-kakao-name": "툴 이름 조건 - 금지어 사용",
    "long-description": "툴 설명 조건 - 길이 초과",
    "missing-service-name-in-description": "툴 설명 조건 - 서비스명 미포함",
    "incomplete-annotations": "툴 annotations 조건 - 필수 힌트",
    "delayed-response": "응답속도 조건 - 지연 / timeout 발생",
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

SERVER_ERROR_GROUPS = (
    (
        "인증/접근",
        "택 1",
        (
            "auth-401",
            "auth-403",
        ),
    ),
    (
        "MCP 스펙/응답",
        "복수 선택 가능",
        (
            "unsupported-min-version",
            "delayed-response",
        ),
    ),
    (
        "tools/list 응답",
        "택 1",
        (
            "tools-list-error",
            "tools-list-null",
            "tools-list-empty",
            "too-many-tools",
        ),
    ),
)

TOOL_ERROR_SCENARIOS = (
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
)

TOOL_ERROR_GROUPS = (
    (
        "툴 이름 조건",
        "복수 선택 가능",
        (
            "duplicate-tool-name",
            "invalid-tool-name-char",
            "invalid-tool-name-length",
            "forbidden-kakao-name",
        ),
    ),
    (
        "툴 필수 속성",
        "복수 선택 가능",
        (
            "missing-name",
            "missing-description",
            "missing-input-schema",
            "missing-annotations",
        ),
    ),
    (
        "툴 설명 조건",
        "복수 선택 가능",
        (
            "long-description",
            "missing-service-name-in-description",
        ),
    ),
    (
        "툴 annotations 조건",
        "복수 선택 가능",
        (
            "incomplete-annotations",
        ),
    ),
)

TOOLS_LIST_MODES = {
    "normal": "정상 tools 반환",
    "json-rpc-error": "JSON-RPC error 반환",
    "null": "tools: null 반환",
    "empty": "tools: [] 반환",
    "too-many": "너무 많은 tools 반환",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "mcp": {
        "identifier": SERVER_NAME,
        "serviceName": SERVICE_NAME,
    },
    "server": {
        "httpStatus": 200,
        "target": "all",
        "delayEnabled": False,
        "delaySeconds": DEFAULT_DELAY_SECONDS,
    },
    "customHeader": {
        "enabled": False,
        "name": "X-Mock-Auth",
        "value": "allow",
    },
    "initialize": {
        "protocolVersionEnabled": True,
        "protocolVersion": "2025-03-26",
    },
    "toolsList": {
        "mode": "normal",
        "tooManyCount": 21,
    },
    "toolErrors": [],
}

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


def tools(service_name: str = SERVICE_NAME) -> list[dict[str, Any]]:
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
                f"{service_name} converts length values between mm, cm, m, km, in, ft, yd, "
                "and mi."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Length"),
        },
        {
            "name": "convert_weight",
            "description": f"{service_name} converts weight values between mg, g, kg, t, oz, and lb.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Weight"),
        },
        {
            "name": "convert_temperature",
            "description": f"{service_name} converts temperature values between c, f, and k.",
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Temperature"),
        },
        {
            "name": "convert_area",
            "description": (
                f"{service_name} converts area values between mm2, cm2, m2, km2, in2, ft2, "
                "yd2, and acre."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Area"),
        },
        {
            "name": "convert_volume",
            "description": (
                f"{service_name} converts volume values between ml, l, m3, in3, ft3, cup, "
                "pt, qt, gal, and floz."
            ),
            "inputSchema": value_unit_schema,
            "annotations": annotations("Convert Volume"),
        },
        {
            "name": "list_supported_units",
            "description": f"{service_name} lists supported canonical units by conversion category.",
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


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_CONFIG))


def scenario_to_config(raw_scenario: str | None) -> dict[str, Any]:
    scenario = normalize_scenario(raw_scenario)
    config = default_config()
    if scenario == "auth-401":
        config["server"]["httpStatus"] = 401
        return config
    if scenario == "auth-403":
        config["server"]["httpStatus"] = 403
        return config
    if scenario == "delayed-response":
        config["server"]["delayEnabled"] = True
        return config
    if scenario == "unsupported-min-version":
        config["initialize"]["protocolVersionEnabled"] = True
        config["initialize"]["protocolVersion"] = "2024-11-05"
        return config
    if scenario == "tools-list-error":
        config["toolsList"]["mode"] = "json-rpc-error"
        return config
    if scenario == "tools-list-null":
        config["toolsList"]["mode"] = "null"
        return config
    if scenario == "tools-list-empty":
        config["toolsList"]["mode"] = "empty"
        return config
    if scenario == "too-many-tools":
        config["toolsList"]["mode"] = "too-many"
        return config
    if scenario in TOOL_ERROR_SCENARIOS:
        config["toolErrors"] = [scenario]
        return config
    return config


def normalize_config(raw_config: Any) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return default_config()

    config = default_config()
    mcp = raw_config.get("mcp") if isinstance(raw_config.get("mcp"), dict) else {}
    server = raw_config.get("server") if isinstance(raw_config.get("server"), dict) else {}
    initialize = raw_config.get("initialize") if isinstance(raw_config.get("initialize"), dict) else {}
    tools_list = raw_config.get("toolsList") if isinstance(raw_config.get("toolsList"), dict) else {}

    identifier = str(mcp.get("identifier", SERVER_NAME)).strip()
    if not identifier:
        raise ValueError("mcp.identifier must not be empty")
    if len(identifier) > 128:
        raise ValueError("mcp.identifier must be 128 characters or fewer")
    config["mcp"]["identifier"] = identifier

    service_name = str(mcp.get("serviceName", SERVICE_NAME)).strip()
    if not service_name:
        raise ValueError("mcp.serviceName must not be empty")
    if len(service_name) > 200:
        raise ValueError("mcp.serviceName must be 200 characters or fewer")
    config["mcp"]["serviceName"] = service_name

    http_status = int(server.get("httpStatus", 200))
    if http_status not in {200, 401, 403}:
        raise ValueError("server.httpStatus must be 200, 401, or 403")
    config["server"]["httpStatus"] = http_status

    target = str(server.get("target", "initialize"))
    if target not in {"initialize", "all"}:
        raise ValueError("server.target must be initialize or all")
    config["server"]["target"] = target

    config["server"]["delayEnabled"] = bool(server.get("delayEnabled", False))
    delay_seconds = float(server.get("delaySeconds", DEFAULT_DELAY_SECONDS))
    if delay_seconds < 0 or delay_seconds > 30:
        raise ValueError("server.delaySeconds must be between 0 and 30")
    config["server"]["delaySeconds"] = delay_seconds

    config["initialize"]["protocolVersionEnabled"] = bool(
        initialize.get(
            "protocolVersionEnabled",
            config["initialize"]["protocolVersionEnabled"],
        )
    )
    protocol_version = str(
        initialize.get("protocolVersion", config["initialize"]["protocolVersion"])
    ).strip()
    if not protocol_version:
        raise ValueError("initialize.protocolVersion must not be empty")
    config["initialize"]["protocolVersion"] = protocol_version

    mode = str(tools_list.get("mode", "normal"))
    if mode not in TOOLS_LIST_MODES:
        raise ValueError("toolsList.mode is invalid")
    config["toolsList"]["mode"] = mode

    too_many_count = int(tools_list.get("tooManyCount", 21))
    if too_many_count < 1 or too_many_count > 100:
        raise ValueError("toolsList.tooManyCount must be between 1 and 100")
    config["toolsList"]["tooManyCount"] = too_many_count

    raw_tool_errors = raw_config.get("toolErrors", [])
    if not isinstance(raw_tool_errors, list):
        raise ValueError("toolErrors must be a list")
    tool_errors = []
    for tool_error in raw_tool_errors:
        if tool_error not in TOOL_ERROR_SCENARIOS:
            raise ValueError(f"unknown tool error '{tool_error}'")
        if tool_error not in tool_errors:
            tool_errors.append(tool_error)
    config["toolErrors"] = tool_errors

    raw_custom_header = raw_config.get("customHeader")
    if isinstance(raw_custom_header, dict):
        config["customHeader"]["enabled"] = bool(raw_custom_header.get("enabled", False))

    return config


def get_active_config() -> dict[str, Any]:
    with SCENARIO_LOCK:
        return json.loads(json.dumps(ACTIVE_CONFIG or DEFAULT_CONFIG))


def set_active_config(raw_config: Any) -> dict[str, Any]:
    config = normalize_config(raw_config)
    global ACTIVE_CONFIG, ACTIVE_SCENARIO
    with SCENARIO_LOCK:
        ACTIVE_CONFIG = config
        ACTIVE_SCENARIO = "custom"
    return get_active_config()


def get_active_scenario() -> str:
    with SCENARIO_LOCK:
        return ACTIVE_SCENARIO


def set_active_scenario(raw_scenario: str | None) -> str:
    global ACTIVE_SCENARIO, ACTIVE_CONFIG

    scenario = normalize_scenario(raw_scenario)
    if scenario not in SCENARIO_DESCRIPTIONS:
        raise ValueError(f"unknown scenario '{scenario}'")

    with SCENARIO_LOCK:
        ACTIVE_SCENARIO = scenario
        ACTIVE_CONFIG = scenario_to_config(scenario)
    return scenario


def resolve_scenario(raw_header_scenario: str | None) -> str:
    if raw_header_scenario is not None and raw_header_scenario.strip():
        return normalize_scenario(raw_header_scenario)
    return get_active_scenario()


def resolve_config(raw_header_scenario: str | None) -> dict[str, Any]:
    if raw_header_scenario is not None and raw_header_scenario.strip():
        return scenario_to_config(raw_header_scenario)
    return get_active_config()


def valid_tool(
    name: str,
    description: str | None = None,
    service_name: str = SERVICE_NAME,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description or f"{service_name} converts common measurement units.",
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


def mutated_tool_for_error(tool_error: str, service_name: str = SERVICE_NAME) -> list[dict[str, Any]]:
    if tool_error == "duplicate-tool-name":
        return [valid_tool("search_place", service_name=service_name), valid_tool("search_place", service_name=service_name)]
    if tool_error == "invalid-tool-name-char":
        return [valid_tool("search place!", service_name=service_name)]
    if tool_error == "invalid-tool-name-length":
        return [valid_tool("a" * 129, service_name=service_name)]
    if tool_error == "missing-name":
        tool = valid_tool("missing_name_case", service_name=service_name)
        tool.pop("name")
        return [tool]
    if tool_error == "missing-description":
        tool = valid_tool("missing_description_case", service_name=service_name)
        tool.pop("description")
        return [tool]
    if tool_error == "missing-input-schema":
        tool = valid_tool("missing_input_schema_case", service_name=service_name)
        tool.pop("inputSchema")
        return [tool]
    if tool_error == "missing-annotations":
        tool = valid_tool("missing_annotations_case", service_name=service_name)
        tool.pop("annotations")
        return [tool]
    if tool_error == "forbidden-kakao-name":
        return [valid_tool("kakao_search", service_name=service_name)]
    if tool_error == "long-description":
        return [valid_tool("long_description_case", "a" * 1051, service_name=service_name)]
    if tool_error == "missing-service-name-in-description":
        return [valid_tool("missing_service_name_case", "Search places nearby.", service_name=service_name)]
    if tool_error == "incomplete-annotations":
        tool = valid_tool("incomplete_annotations_case", service_name=service_name)
        tool["annotations"] = {
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        return [tool]
    return []


def tools_for_config(config: dict[str, Any]) -> dict[str, Any] | None:
    tools_list = config["toolsList"]
    mode = tools_list["mode"]
    service_name = config["mcp"]["serviceName"]
    if mode == "null":
        return {"tools": None}
    if mode == "empty":
        return {"tools": []}
    if mode == "too-many":
        return {
            "tools": [
                valid_tool(f"tool_{index}", service_name=service_name)
                for index in range(tools_list["tooManyCount"])
            ]
        }
    if mode != "normal":
        return None

    configured_tools: list[dict[str, Any]] = []
    for tool_error in config["toolErrors"]:
        configured_tools.extend(mutated_tool_for_error(tool_error, service_name))
    if configured_tools:
        return {"tools": configured_tools}
    return None


def handle_json_rpc(
    payload: Any,
    scenario: str = "ok",
    config: dict[str, Any] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    scenario = normalize_scenario(scenario)
    config = normalize_config(config) if config is not None else scenario_to_config(scenario)
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
        if config["initialize"]["protocolVersionEnabled"]:
            protocol_version = config["initialize"]["protocolVersion"]
        capabilities: dict[str, Any] = {"tools": {"listChanged": True}}
        result = {
            "protocolVersion": protocol_version,
            "capabilities": capabilities,
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        return 200, {"Mcp-Session-Id": session_id}, json_rpc_result(request_id, result)

    if method == "notifications/initialized":
        return 202, {}, None

    if method == "ping":
        return 202 if is_notification else 200, {}, None if is_notification else json_rpc_result(request_id, {})

    if method == "tools/list":
        if config["toolsList"]["mode"] == "json-rpc-error":
            return 200, {}, json_rpc_error(request_id, -32603, "Injected tools/list failure")
        scenario_tools = tools_for_config(config)
        return 200, {}, json_rpc_result(
            request_id,
            scenario_tools or {"tools": tools(config["mcp"]["serviceName"])},
        )

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
    active_config = get_active_config()
    active_title = SCENARIO_TITLES.get(active_scenario, "커스텀 설정")
    active_group = scenario_group_label(active_scenario) if active_scenario != "custom" else "커스텀"
    config_json = json.dumps(active_config, ensure_ascii=False)
    server_error_sections: list[str] = []
    for group_label, selection_rule, scenarios in SERVER_ERROR_GROUPS:
        controls = []
        for scenario in scenarios:
            conflict_group = ""
            if scenario in {"auth-401", "auth-403"}:
                conflict_group = "auth"
            if scenario in {
                "tools-list-error",
                "tools-list-null",
                "tools-list-empty",
                "too-many-tools",
            }:
                conflict_group = "tools-list"
            conflict_attr = (
                f' data-conflict="{escape(conflict_group)}"' if conflict_group else ""
            )
            controls.append(
                f"""
        <label class="check-row">
          <input type="checkbox" name="serverErrors" value="{escape(scenario)}"{conflict_attr}>
          <span>{escape(SCENARIO_TITLES[scenario])}</span>
          <code>{escape(scenario)}</code>
        </label>
        """
            )
        server_error_sections.append(
            f"""
        <section class="tool-error-section">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <div class="tool-error-list">
            {"".join(controls)}
          </div>
        </section>
        """
        )
    server_error_controls = "\n".join(server_error_sections)
    tool_error_sections: list[str] = []
    for group_label, selection_rule, scenarios in TOOL_ERROR_GROUPS:
        controls = "\n".join(
            f"""
        <label class="check-row">
          <input type="checkbox" name="toolErrors" value="{escape(scenario)}">
          <span>{escape(SCENARIO_TITLES[scenario])}</span>
          <code>{escape(scenario)}</code>
        </label>
        """
            for scenario in scenarios
        )
        tool_error_sections.append(
            f"""
        <section class="tool-error-section">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <div class="tool-error-list">
            {controls}
          </div>
        </section>
        """
        )
    tool_error_controls = "\n".join(tool_error_sections)
    policy_reference_sections: list[str] = []
    for section_label, groups in (
        ("server 에러 시나리오", SERVER_ERROR_GROUPS),
        ("tools 에러 시나리오", TOOL_ERROR_GROUPS),
    ):
        group_cards = []
        for group_label, selection_rule, scenarios in groups:
            items = "\n".join(
                f"""
          <li>
            <div>
              <strong>{escape(SCENARIO_TITLES[scenario])}</strong>
              <code>{escape(scenario)}</code>
            </div>
            <p>{escape(SCENARIO_DESCRIPTIONS[scenario])}</p>
          </li>
          """
                for scenario in scenarios
            )
            group_cards.append(
                f"""
        <section class="policy-group">
          <h3>{escape(group_label)} <span>{escape(selection_rule)}</span></h3>
          <ul>
            {items}
          </ul>
        </section>
        """
            )
        policy_reference_sections.append(
            f"""
      <section class="policy-section">
        <h3>{escape(section_label)}</h3>
        <div class="policy-grid">
          {"".join(group_cards)}
        </div>
      </section>
      """
        )
    policy_reference = "\n".join(policy_reference_sections)
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
  <title>Unit Expert MCP 상태 제어</title>
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
    .header-title {{
      min-width: 0;
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
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      padding: 14px;
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
      background: #fff;
      border-color: #e1e6ef;
    }}
    .title-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
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
      grid-template-columns: minmax(340px, 460px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .workspace > * {{
      min-width: 0;
    }}
    .right-column {{
      display: grid;
      grid-column: 1;
      grid-row: 1;
      gap: 16px;
      min-width: 0;
      position: sticky;
      top: 16px;
      max-height: calc(100vh - 32px);
      overflow: auto;
      scrollbar-gutter: stable;
    }}
    .summary-sidebar {{
      display: grid;
      gap: 12px;
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      padding: 16px;
    }}
    .summary-sidebar h2 {{
      margin: 0;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .summary-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .summary-list {{
      display: grid;
      gap: 10px;
      margin: 0;
    }}
    .summary-item {{
      display: grid;
      gap: 4px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }}
    .summary-item:last-child {{
      padding-bottom: 0;
      border-bottom: 0;
    }}
    .summary-item dt {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .summary-item dd {{
      margin: 0;
      font-size: 14px;
      color: var(--text);
      overflow-wrap: anywhere;
    }}
    .summary-item code {{
      width: fit-content;
      max-width: 100%;
    }}
    .summary-chip {{
      display: block;
      width: fit-content;
      max-width: 100%;
      margin-bottom: 6px;
      padding: 3px 7px;
      border: 1px solid #d8dee9;
      border-radius: 999px;
      background: #fff;
      color: var(--text);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .summary-chip:last-child {{
      margin-bottom: 0;
    }}
    .summary-lines {{
      display: grid;
      gap: 6px;
    }}
    .summary-line {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }}
    .summary-line-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }}
    .left-column {{
      display: grid;
      grid-column: 2;
      grid-row: 1;
      gap: 16px;
      min-width: 0;
    }}
    .action-buttons {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
      min-width: 0;
    }}
    .spec-strip {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
    }}
    .spec-strip h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .server-settings-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    .setting-block {{
      display: grid;
      gap: 7px;
    }}
    .setting-heading {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: baseline;
    }}
    .setting-heading strong {{
      font-size: 14px;
      font-weight: 700;
    }}
    .setting-heading span {{
      color: var(--muted);
      font-size: 12px;
    }}
    .summary {{
      min-width: 220px;
      font-size: 13px;
      color: var(--muted);
      display: none;
    }}
    .control-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
    }}
    .control-card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .control-card.server-card {{
      background: #eef6ff;
    }}
    .server-card .tool-error-section {{
      background: #f8fbff;
      border-color: #cfe0f4;
    }}
    .control-card.tool-card {{
      background: #f6f2e9;
    }}
    .control-card.identity-card {{
      background: #f0f9f6;
      border-color: #b9ddd2;
    }}
    .tool-card .tool-error-section {{
      background: #fbf8ef;
      border-color: #ded6c3;
    }}
    .control-card.full {{
      grid-column: 1 / -1;
    }}
    .control-card h2 {{
      margin: 0 0 12px;
      font-size: 15px;
      letter-spacing: 0;
    }}
    .card-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .card-title h2 {{
      margin: 0;
    }}
    .step-badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 28px;
      height: 24px;
      border-radius: 999px;
      background: #e7f6f3;
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
    }}
    .control-stack {{
      display: grid;
      gap: 9px;
    }}
    .field-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .field-grid.three {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .delay-grid {{
      grid-template-columns: minmax(180px, 240px) minmax(0, 1fr);
      align-items: end;
    }}
    .delay-grid .check-row {{
      min-height: 34px;
    }}
    .field {{
      display: grid;
      gap: 6px;
      min-width: 0;
      font-size: 14px;
    }}
    .field > span:first-child {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    .field-title {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: baseline;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    .field-hint {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 400;
    }}
    .section-hint {{
      margin: -4px 0 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    .sidebar-mcp-fields {{
      display: grid;
      gap: 14px;
      padding: 14px 0;
      border-top: 1px solid #c8d1df;
      border-bottom: 1px solid #c8d1df;
      margin-bottom: 14px;
    }}
    .sidebar-custom-header-row {{
      display: grid;
      gap: 8px;
    }}
    .sidebar-custom-header-label {{
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .custom-header-hint .hint-desc {{
      margin: 6px 0 0;
      color: #335c85;
      font-size: 12px;
      line-height: 1.6;
      grid-column: 1 / -1;
    }}
    .custom-header-card .card-title {{
      justify-content: space-between;
    }}
    .custom-header-toggle {{
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
    }}
    .custom-header-toggle input[type="checkbox"] {{
      position: absolute;
      opacity: 0;
      width: 0;
      height: 0;
    }}
    .toggle-track {{
      display: inline-flex;
      align-items: center;
      width: 40px;
      height: 22px;
      border-radius: 999px;
      background: var(--line);
      transition: background 0.2s;
      cursor: pointer;
      flex-shrink: 0;
    }}
    .toggle-thumb {{
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: #fff;
      margin-left: 3px;
      transition: transform 0.2s;
      box-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}
    .custom-header-toggle input:checked + .toggle-track {{
      background: var(--accent);
    }}
    .custom-header-toggle input:checked + .toggle-track .toggle-thumb {{
      transform: translateX(18px);
    }}
    .custom-header-hint {{
      display: grid;
      grid-template-columns: auto 1fr;
      align-items: center;
      gap: 6px 10px;
      margin-top: 4px;
      padding: 10px 14px;
      border-radius: 8px;
      background: #edf6ff;
      border: 1px solid #c2ddf7;
    }}
    .custom-header-hint .hint-label {{
      color: #335c85;
      font-size: 12px;
      font-weight: 600;
      white-space: nowrap;
    }}
    .custom-header-hint code {{
      font-family: monospace;
      font-size: 13px;
      background: #d6eaff;
      color: #1a4a7a;
      padding: 3px 8px;
      border-radius: 5px;
      letter-spacing: 0.02em;
    }}
    .check-grid {{
      display: grid;
      gap: 14px;
    }}
    .check-grid.two-column {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
      align-items: start;
    }}
    .tool-error-section {{
      display: grid;
      gap: 8px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #f7f8fb;
    }}
    .tool-error-section h3 {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin: -4px -4px 2px;
      padding: 4px 6px;
      border-radius: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .server-card .tool-error-section h3 {{
      background: #edf6ff;
      color: #335c85;
    }}
    .tool-card .tool-error-section h3 {{
      background: #f3ecdc;
      color: #6d5b36;
    }}
    .tool-error-section h3 span {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f5f6f8;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .tool-error-list {{
      display: grid;
      gap: 6px;
    }}
    .policy-reference {{
      display: grid;
      gap: 18px;
    }}
    .policy-details {{
      padding: 0;
      overflow: hidden;
      background: #f7f8fb;
    }}
    .policy-details summary {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 16px;
      cursor: pointer;
      list-style: none;
    }}
    .policy-details summary::-webkit-details-marker {{
      display: none;
    }}
    .policy-details summary::before {{
      content: "▸";
      color: var(--muted);
      font-size: 12px;
    }}
    .policy-details[open] summary::before {{
      content: "▾";
    }}
    .policy-details[open] summary {{
      border-bottom: 1px solid var(--line);
    }}
    .policy-details h2 {{
      margin: 0;
    }}
    .policy-summary-context {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .policy-details .policy-reference {{
      padding: 16px;
    }}
    .policy-section {{
      display: grid;
      gap: 10px;
    }}
    .policy-section > h3 {{
      margin: 0;
      font-size: 14px;
      letter-spacing: 0;
    }}
    .policy-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: start;
    }}
    .policy-group {{
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .policy-group h3 {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .policy-group h3 span {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 7px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #f5f6f8;
      color: var(--muted);
      font-size: 11px;
      font-weight: 600;
    }}
    .policy-group ul {{
      display: grid;
      gap: 6px;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .policy-group li {{
      display: grid;
      gap: 5px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafbfc;
    }}
    .policy-group li > div {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
    }}
    .policy-group strong {{
      font-size: 13px;
      font-weight: 700;
    }}
    .policy-group p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .radio-row, .check-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 30px;
      font-size: 14px;
    }}
    .check-grid .check-row {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      grid-template-areas:
        "input title"
        "input code";
      align-items: start;
      gap: 2px 8px;
      min-height: 58px;
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    .check-grid .check-row input {{
      grid-area: input;
      margin-top: 3px;
    }}
    .check-grid .check-row span {{
      grid-area: title;
      min-width: 0;
      overflow-wrap: anywhere;
    }}
    .check-row code {{
      margin-left: auto;
      font-size: 11px;
      max-width: 48%;
      overflow-wrap: anywhere;
      white-space: normal;
    }}
    .check-grid .check-row code {{
      grid-area: code;
      width: fit-content;
      max-width: 100%;
      margin-left: 0;
    }}
    .check-grid.is-disabled .check-row {{
      opacity: 0.58;
      background: #f3f4f6;
    }}
    .inline-field {{
      display: grid;
      grid-template-columns: 160px minmax(0, 1fr);
      gap: 8px;
      align-items: center;
      font-size: 14px;
    }}
    input[type="number"], input[type="text"] {{
      width: 100%;
      height: 34px;
      border: 1px solid #cfd5df;
      border-radius: 6px;
      padding: 0 10px;
      font: inherit;
    }}
    select:disabled,
    input:disabled {{
      color: #98a2b3;
      background: #f3f4f6;
      cursor: not-allowed;
    }}
    .muted-note {{
      display: none;
      color: #8a4b0f;
      background: #fff7ed;
      border: 1px solid #fed7aa;
      border-radius: 6px;
      padding: 10px 11px;
      margin: -4px 0 12px;
      font-size: 13px;
    }}
    .muted-note.visible {{
      display: block;
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
      table-layout: fixed;
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
    .terminal-details {{
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 8px;
      overflow: hidden;
    }}
    .terminal-details summary {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 12px;
      padding: 12px;
      cursor: pointer;
      color: var(--text);
      font-size: 14px;
      font-weight: 700;
      background: #e9eef7;
    }}
    .terminal-details summary::-webkit-details-marker {{
      display: none;
    }}
    .terminal-details[open] summary {{
      border-bottom: 1px solid var(--line);
    }}
    .terminal-summary-label {{
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }}
    .terminal-summary-label::before {{
      content: "▸";
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 18px;
      height: 18px;
      border-radius: 999px;
      background: #e7ecf5;
      color: #475569;
      font-size: 11px;
      flex: 0 0 auto;
    }}
    .terminal-details[open] .terminal-summary-label::before {{
      content: "▾";
    }}
    .terminal-summary-context {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
      overflow-wrap: anywhere;
    }}
    .terminal-summary-text {{
      display: grid;
      gap: 3px;
      min-width: 0;
    }}
    .terminal-toggle-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border: 1px solid #c7d0dd;
      border-radius: 999px;
      color: #475569;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .terminal-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .terminal-details[open] .terminal-toggle-label::before {{
      content: "접기";
    }}
    .terminal {{
      background: #111827;
      color: #d1fae5;
      border: 0;
      border-radius: 0;
      min-height: 460px;
      overflow: hidden;
      min-width: 0;
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
      min-height: 414px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.55;
    }}
    @media (max-width: 1080px) {{
      header {{ display: block; }}
      .endpoint-row {{ grid-template-columns: 1fr; }}
      .action-buttons {{ justify-content: stretch; }}
      .status {{ grid-template-columns: 1fr; }}
      form {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
      .workspace {{ grid-template-columns: 1fr; }}
      .left-column, .right-column {{
        grid-column: auto;
        grid-row: auto;
        position: static;
        max-height: none;
        overflow: visible;
      }}
      .control-grid {{ grid-template-columns: 1fr; }}
      .server-settings-grid {{ grid-template-columns: 1fr; }}
      .field-grid, .field-grid.three {{ grid-template-columns: 1fr; }}
      .check-grid.two-column {{ grid-template-columns: 1fr; }}
      .policy-grid {{ grid-template-columns: 1fr; }}
      .inline-field {{ grid-template-columns: 1fr; }}
      .terminal {{ min-height: 360px; }}
      .terminal pre {{ min-height: 314px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="header-title">
        <h1>Unit Expert MCP 상태 제어</h1>
        <div class="title-note">
          MCP 상태는 서버 전역으로 공유됩니다. 여러 사용자가 동시에 적용하면 마지막 적용값으로 바뀔 수 있습니다.
        </div>
      </div>
    </header>
    <div class="workspace">
      <div class="left-column">
        {notice}
        <span id="activeTitle" hidden>{escape(active_title)}</span>
        <span id="activeGroup" hidden>{escape(active_group)}</span>
        <span id="activeScenario" hidden>{escape(active_scenario)}</span>
        <div class="control-grid">
          <section class="control-card full server-card">
            <div class="card-title">
              <h2>server 에러 시나리오</h2>
              <span class="step-badge">1</span>
            </div>
            <p class="section-hint">
              MCP 연결, initialize, tools/list 단계에서 발생시키고 싶은 server 응답을 선택합니다.
            </p>
            <div class="check-grid two-column" id="serverErrorGrid">
              {server_error_controls}
            </div>
          </section>
          <section class="control-card full tool-card">
            <div class="card-title">
              <h2>tools 에러 시나리오</h2>
              <span class="step-badge">2</span>
            </div>
            <div id="toolErrorDisabledNote" class="muted-note">
              tools/list 응답 시나리오가 먼저 적용됩니다. tools 에러 시나리오를 사용하려면 server 에러 시나리오의 tools/list 응답 선택을 해제해 주세요.
            </div>
            <div class="check-grid two-column" id="toolErrorGrid">
              {tool_error_controls}
            </div>
          </section>
          <details class="control-card full policy-details">
            <summary>
              <h2>시나리오별 설명</h2>
              <span class="policy-summary-context">각 시나리오가 어떤 응답을 만드는지 확인합니다.</span>
            </summary>
            <div class="policy-reference" id="scenarioRows">
              {policy_reference}
            </div>
          </details>
        </div>
      </div>
      <aside class="right-column">
        <section class="endpoint" aria-label="MCP URL">
          <div class="endpoint-label">MCP URL</div>
          <div class="endpoint-row">
            <code id="mcpUrl">{escape(PUBLIC_MCP_URL)}</code>
            <button id="copyMcpUrl" class="copy-button" type="button">복사</button>
          </div>
        </section>
        <section class="summary-sidebar" aria-label="현재 MCP 설정 요약">
          <div class="summary-header">
            <h2>MCP 설정</h2>
            <div class="action-buttons">
              <button id="applyConfig" type="button">적용</button>
              <button id="resetConfig" class="secondary" type="button">초기화</button>
            </div>
          </div>
          <div class="sidebar-mcp-fields">
            <div class="field-grid">
              <label class="field">
                <span class="field-title">
                  MCP 식별자
                  <span class="field-hint">initialize의 serverInfo.name에 반환됩니다.</span>
                </span>
                <input id="mcpIdentifier" type="text" maxlength="128" autocomplete="off">
              </label>
              <label class="field">
                <span class="field-title">
                  MCP 이름(서비스 이름)
                  <span class="field-hint">tool description에 포함됩니다.</span>
                </span>
                <input id="mcpServiceName" type="text" maxlength="200" autocomplete="off">
              </label>
            </div>
            <div class="sidebar-custom-header-row">
              <div class="sidebar-custom-header-label">
                <span class="field-title">커스텀 헤더 설정 여부</span>
                <label class="custom-header-toggle">
                  <input type="checkbox" id="customHeaderEnabled">
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>
              <div id="customHeaderHint" class="custom-header-hint" hidden>
                <span class="hint-label">요청 헤더</span>
                <code>X-Mock-Auth: allow</code>
                <p class="hint-desc">에러 시나리오와 별개로 동작합니다. 헤더가 없으면 401, 값이 틀리면 403을 반환합니다.</p>
              </div>
            </div>
          </div>
          <dl class="summary-list">
            <div class="summary-item">
              <dt>server 에러 시나리오</dt>
              <dd id="summaryServerScenarios">-</dd>
            </div>
            <div class="summary-item">
              <dt>tools 에러 시나리오</dt>
              <dd id="summaryToolScenarios">-</dd>
            </div>
          </dl>
        </section>
        <details class="terminal-details">
          <summary>
            <span class="terminal-summary-text">
              <span class="terminal-summary-label">실제 응답 미리보기</span>
              <span class="terminal-summary-context">MCP 요청과 반환 응답을 JSON으로 확인합니다.</span>
            </span>
            <span class="terminal-toggle-label" aria-hidden="true"></span>
          </summary>
          <section class="terminal" aria-label="MCP 응답 미리보기">
            <pre id="responsePreview">Loading...</pre>
          </section>
        </details>
      </aside>
    </div>
  </main>
  <script>
    const initialConfig = {config_json};
    const defaultConfig = {json.dumps(DEFAULT_CONFIG, ensure_ascii=False)};
    const scenarioTitles = {json.dumps(SCENARIO_TITLES, ensure_ascii=False)};
    const activeScenario = document.getElementById("activeScenario");
    const activeGroup = document.getElementById("activeGroup");
    const activeTitle = document.getElementById("activeTitle");
    const preview = document.getElementById("responsePreview");
    const copyMcpUrl = document.getElementById("copyMcpUrl");
    const mcpUrl = document.getElementById("mcpUrl");
    const applyConfig = document.getElementById("applyConfig");
    const resetConfig = document.getElementById("resetConfig");
    const toolErrorDisabledNote = document.getElementById("toolErrorDisabledNote");
    const toolErrorGrid = document.getElementById("toolErrorGrid");
    const summaryServerScenarios = document.getElementById("summaryServerScenarios");
    const summaryToolScenarios = document.getElementById("summaryToolScenarios");
    const mcpIdentifier = document.getElementById("mcpIdentifier");
    const mcpServiceName = document.getElementById("mcpServiceName");

    function clone(value) {{
      return JSON.parse(JSON.stringify(value));
    }}

    function setButtonBusy(button, label) {{
      button.textContent = label;
      button.disabled = true;
    }}

    function flashButton(button, label, resetLabel) {{
      button.textContent = label;
      button.disabled = false;
      window.setTimeout(() => {{
        button.textContent = resetLabel;
      }}, 1100);
    }}

    function pretty(value) {{
      try {{
        return JSON.stringify(value, null, 2);
      }} catch (error) {{
        return String(value);
      }}
    }}

    function escapeHtml(value) {{
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
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

    function selectedToolErrors() {{
      return Array.from(document.querySelectorAll('input[name="toolErrors"]:checked'))
        .map((input) => input.value);
    }}

    function selectedServerErrors() {{
      return Array.from(document.querySelectorAll('input[name="serverErrors"]:checked'))
        .map((input) => input.value);
    }}

    function toolsListModeFromServerErrors(serverErrors) {{
      if (serverErrors.includes("tools-list-error")) return "json-rpc-error";
      if (serverErrors.includes("tools-list-null")) return "null";
      if (serverErrors.includes("tools-list-empty")) return "empty";
      if (serverErrors.includes("too-many-tools")) return "too-many";
      return "normal";
    }}

    function serverErrorsFromConfig(config) {{
      const scenarios = [];
      if (config.server.httpStatus === 401) scenarios.push("auth-401");
      if (config.server.httpStatus === 403) scenarios.push("auth-403");
      if (config.initialize.protocolVersion !== "2025-03-26") {{
        scenarios.push("unsupported-min-version");
      }}
      if (config.server.delayEnabled) scenarios.push("delayed-response");
      const modeToScenario = {{
        "json-rpc-error": "tools-list-error",
        "null": "tools-list-null",
        "empty": "tools-list-empty",
        "too-many": "too-many-tools"
      }};
      if (modeToScenario[config.toolsList.mode]) {{
        scenarios.push(modeToScenario[config.toolsList.mode]);
      }}
      return scenarios;
    }}

    function setControls(config) {{
      mcpIdentifier.value = config.mcp.identifier;
      mcpServiceName.value = config.mcp.serviceName;
      const serverErrors = serverErrorsFromConfig(config);
      document.querySelectorAll('input[name="serverErrors"]').forEach((input) => {{
        input.checked = serverErrors.includes(input.value);
      }});
      document.querySelectorAll('input[name="toolErrors"]').forEach((input) => {{
        input.checked = config.toolErrors.includes(input.value);
      }});
      const customHeaderEl = document.getElementById("customHeaderEnabled");
      if (customHeaderEl) {{
        customHeaderEl.checked = !!(config.customHeader && config.customHeader.enabled);
        syncCustomHeaderHint();
      }}
      syncEnabledStates(config);
      updateSummary(config, false);
    }}

    function collectConfig() {{
      const serverErrors = selectedServerErrors();
      const mode = toolsListModeFromServerErrors(serverErrors);
      const toolErrors = mode === "normal" ? selectedToolErrors() : [];
      return {{
        mcp: {{
          identifier: mcpIdentifier.value.trim() || defaultConfig.mcp.identifier,
          serviceName: mcpServiceName.value.trim() || defaultConfig.mcp.serviceName
        }},
        server: {{
          httpStatus: serverErrors.includes("auth-403")
            ? 403
            : serverErrors.includes("auth-401")
              ? 401
              : 200,
          target: "all",
          delayEnabled: serverErrors.includes("delayed-response"),
          delaySeconds: 5
        }},
        initialize: {{
          protocolVersionEnabled: true,
          protocolVersion: serverErrors.includes("unsupported-min-version")
            ? "2024-11-05"
            : "2025-03-26"
        }},
        toolsList: {{
          mode,
          tooManyCount: 21
        }},
        customHeader: {{
          enabled: document.getElementById("customHeaderEnabled").checked,
          name: "X-Mock-Auth",
          value: "allow"
        }},
        toolErrors
      }};
    }}

    function syncCustomHeaderHint() {{
      const enabled = document.getElementById("customHeaderEnabled").checked;
      document.getElementById("customHeaderHint").hidden = !enabled;
    }}

    document.getElementById("customHeaderEnabled").addEventListener("change", () => {{
      syncCustomHeaderHint();
      onConfigChange();
    }});

    function syncEnabledStates(config) {{
      const toolErrorsEnabled = config.toolsList.mode === "normal";
      toolErrorDisabledNote.classList.toggle("visible", !toolErrorsEnabled);
      toolErrorGrid.classList.toggle("is-disabled", !toolErrorsEnabled);
      document.querySelectorAll('input[name="toolErrors"]').forEach((input) => {{
        input.disabled = !toolErrorsEnabled;
        if (!toolErrorsEnabled) {{
          input.checked = false;
        }}
      }});
    }}

    function enforceExclusiveScenario(input) {{
      if (!input.checked || !input.dataset.conflict) return;
      document
        .querySelectorAll(`input[name="serverErrors"][data-conflict="${{input.dataset.conflict}}"]`)
        .forEach((candidate) => {{
          if (candidate !== input) candidate.checked = false;
        }});
    }}

    function titleForToolError(error) {{
      return scenarioTitles[error] || error;
    }}

    function renderScenarioSummary(element, scenarios) {{
      if (!scenarios.length) {{
        element.textContent = "없음";
        return;
      }}
      element.innerHTML = scenarios
        .map((scenario) => `<span class="summary-chip">${{scenarioTitles[scenario] || scenario}}</span>`)
        .join("");
    }}

    function summarizeConfig(config) {{
      const parts = [];
      if (config.server.httpStatus !== 200) {{
        parts.push(`HTTP ${{config.server.httpStatus}}`);
      }}
      if (config.server.delayEnabled) {{
        parts.push(`응답 지연 ${{config.server.delaySeconds}}초`);
      }}
      if (config.initialize.protocolVersion !== "2025-03-26") {{
        parts.push(`protocolVersion ${{config.initialize.protocolVersion}}`);
      }}
      if (config.toolsList.mode !== "normal") {{
        const labels = {{
          "json-rpc-error": "tools/list JSON-RPC error",
          "null": "tools: null",
          "empty": "tools: []",
          "too-many": `tools ${{config.toolsList.tooManyCount}}개`
        }};
        parts.push(labels[config.toolsList.mode] || config.toolsList.mode);
      }}
      for (const error of config.toolErrors) {{
        parts.push(titleForToolError(error));
      }}
      return parts.length ? parts.join(" + ") : "정상 응답";
    }}

    function updateConfigSummary(config) {{
      renderScenarioSummary(summaryServerScenarios, serverErrorsFromConfig(config));
      renderScenarioSummary(
        summaryToolScenarios,
        config.toolsList.mode === "normal" ? config.toolErrors : []
      );
    }}

    function updateSummary(config, applied) {{
      const hasServerError =
        config.server.httpStatus !== 200 ||
        config.server.delayEnabled ||
        config.initialize.protocolVersion !== "2025-03-26" ||
        config.toolsList.mode !== "normal";
      const hasToolError = config.toolErrors.length > 0;
      const group = hasServerError && hasToolError
        ? "서버 error + tool error"
        : hasServerError
          ? "서버 error"
          : hasToolError
            ? "tool error"
            : "기본";
      const title = summarizeConfig(config);
      activeScenario.textContent = applied ? "custom" : "custom · 미적용";
      activeGroup.textContent = group;
      activeTitle.textContent = title;
      updateConfigSummary(config);
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

    async function postConfig(config) {{
      const response = await fetch("/scenario", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ config }})
      }});
      const payload = await response.json();
      if (!response.ok || !payload.ok) {{
        throw new Error(pretty({{ status: response.status, body: payload }}));
      }}
      return payload.config;
    }}

    async function refreshPreview(config) {{
      const title = activeTitle.textContent;
      const group = activeGroup.textContent;
      preview.textContent = `$ 구분: ${{group}}\\n$ 설정: ${{title}}\\n$ POST /mcp initialize\\n...`;
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
          `$ 설정: ${{title}}`,
          `$ 적용 JSON`,
          pretty(config),
          "",
          formatHttp("POST /mcp initialize", initialize),
          "",
          formatHttp("POST /mcp tools/list", toolsList)
        ].join("\\n");
      }} catch (error) {{
        preview.textContent = `$ 설정: ${{title}}\\n${{error && error.stack ? error.stack : error}}`;
      }}
    }}

    applyConfig.addEventListener("click", async () => {{
      const config = collectConfig();
      updateSummary(config, false);
      setButtonBusy(applyConfig, "적용 중");
      preview.textContent = "$ POST /scenario\\n...";
      try {{
        const appliedConfig = await postConfig(config);
        setControls(appliedConfig);
        updateSummary(appliedConfig, true);
        flashButton(applyConfig, "적용됨", "적용");
        await refreshPreview(appliedConfig);
      }} catch (error) {{
        flashButton(applyConfig, "실패", "적용");
        preview.textContent = error && error.stack ? error.stack : String(error);
      }}
    }});

    resetConfig.addEventListener("click", async () => {{
      const config = clone(defaultConfig);
      setControls(config);
      setButtonBusy(resetConfig, "초기화 중");
      preview.textContent = "$ POST /scenario\\n...";
      try {{
        const appliedConfig = await postConfig(config);
        setControls(appliedConfig);
        updateSummary(appliedConfig, true);
        flashButton(resetConfig, "초기화됨", "초기화");
        await refreshPreview(appliedConfig);
      }} catch (error) {{
        flashButton(resetConfig, "실패", "초기화");
        preview.textContent = error && error.stack ? error.stack : String(error);
      }}
    }});

    function handleControlChange(event) {{
      if (event && event.target && event.target.name === "serverErrors") {{
        enforceExclusiveScenario(event.target);
      }}
      const config = collectConfig();
      syncEnabledStates(config);
      updateSummary(config, false);
    }}

    document.querySelectorAll("input, select").forEach((input) => {{
      input.addEventListener("change", handleControlChange);
      input.addEventListener("input", handleControlChange);
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

    setControls(initialConfig);
    updateSummary(initialConfig, true);
    refreshPreview(initialConfig);
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

        config = self.request_config()
        if self.handle_pre_json_rpc_config(config):
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

        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON")
            return

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_json(400, json_rpc_error(None, -32700, "Parse error"))
            return

        config = self.request_config()
        if self.handle_pre_json_rpc_config(config, payload.get("method")):
            return

        status, extra_headers, response = handle_json_rpc(payload, config=config)
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
                if isinstance(payload, dict) and "config" in payload:
                    config = set_active_config(payload["config"])
                    scenario = "custom"
                else:
                    raw_scenario = payload.get("scenario") if isinstance(payload, dict) else None
                    scenario = set_active_scenario(raw_scenario)
                    config = get_active_config()
            else:
                form = parse_qs(raw_body.decode("utf-8"))
                raw_scenario = form.get("scenario", ["ok"])[0]
                scenario = set_active_scenario(raw_scenario)
                config = get_active_config()
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            if "application/json" in content_type:
                self.send_json(400, {"ok": False, "error": str(error)})
                return
            self.send_html(400, render_scenario_page(error=str(error)))
            return

        if "application/json" in content_type:
            self.send_json(200, {"ok": True, "scenario": scenario, "config": config})
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

    def request_config(self) -> dict[str, Any]:
        return resolve_config(self.headers.get(TEST_SCENARIO_HEADER))

    def handle_pre_json_rpc_config(
        self,
        config: dict[str, Any],
        method: str | None = None,
    ) -> bool:
        server = config["server"]
        if server["delayEnabled"]:
            time.sleep(server["delaySeconds"])

        custom_header = config.get("customHeader", {})
        if custom_header.get("enabled"):
            header_val = self.headers.get(custom_header.get("name", ""))
            if header_val is None:
                self.send_text(401, "Unauthorized")
                return True
            if header_val != custom_header.get("value", ""):
                self.send_text(403, "Forbidden")
                return True
            return False

        http_status = server["httpStatus"]
        target = server["target"]
        if http_status == 200:
            return False
        if target == "initialize" and method not in {None, "initialize"}:
            return False
        if http_status == 401:
            self.send_text(401, "Unauthorized")
            return True
        if http_status == 403:
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
