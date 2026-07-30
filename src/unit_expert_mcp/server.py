"""Minimal Streamable HTTP MCP server for PlayMCP."""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
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
SUPPORTED_PROTOCOL_VERSIONS = ("2024-03-26", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
LATEST_PROTOCOL_VERSION = "2026-07-28"

# 2026-07-28 stateless protocol constants.
PROTOCOL_VERSION_2026 = "2026-07-28"
# Per-request _meta envelope keys (spec: basic/index#meta).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
# MCP-defined JSON-RPC error codes introduced in 2026-07-28.
ERR_HEADER_MISMATCH = -32020
ERR_MISSING_CAPABILITY = -32021
ERR_UNSUPPORTED_VERSION = -32022
ERR_INVALID_PARAMS = -32602
# method -> params key mirrored into the Mcp-Name header.
NAME_BEARING_METHODS = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}
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
CONFIG_QUERY_PARAM = "cfg"

SESSIONS: set[str] = set()
SCENARIO_LOCK = Lock()
ACTIVE_SCENARIO = "ok"
ACTIVE_CONFIG: dict[str, Any] = {}

# In-memory ring buffer of recent /mcp exchanges, surfaced by the /inspect page
# so the 2026 wire (headers + _meta envelope + response) is visible live.
INSPECT_LOCK = Lock()
INSPECT_LOG: deque[dict[str, Any]] = deque(maxlen=50)
INSPECT_SEQ = 0
# Only these request headers are interesting for MCP wire inspection.
INSPECT_HEADER_PREFIXES = ("mcp-", "x-mcp-")
INSPECT_HEADER_NAMES = ("accept", "content-type", "authorization")


def record_exchange(
    method_http: str,
    path: str,
    request_headers: Any,
    request_body: Any,
    status: int,
    response_body: Any,
) -> None:
    """Append one /mcp exchange to the inspection ring buffer."""
    global INSPECT_SEQ
    headers = {}
    for name, value in (request_headers.items() if request_headers else []):
        lowered = name.lower()
        if lowered in INSPECT_HEADER_NAMES or lowered.startswith(INSPECT_HEADER_PREFIXES):
            headers[name] = value
    jsonrpc_method = request_body.get("method") if isinstance(request_body, dict) else None
    era = request_era(request_body, dict(request_headers) if request_headers else None)
    with INSPECT_LOCK:
        INSPECT_SEQ += 1
        INSPECT_LOG.appendleft(
            {
                "seq": INSPECT_SEQ,
                "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                "httpMethod": method_http,
                "path": path,
                "era": era,
                "method": jsonrpc_method,
                "requestHeaders": headers,
                "requestBody": request_body,
                "status": status,
                "responseBody": response_body,
            }
        )


def inspect_snapshot() -> list[dict[str, Any]]:
    with INSPECT_LOCK:
        return list(INSPECT_LOG)

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
            "duplicate-tool-name",
            "too-many-tools",
            "delayed-response",
        ),
    ),
    (
        "tool error",
        (
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
    (
        "tools/list 도구 구성",
        "정상 tools 반환일 때 선택 가능",
        (
            "duplicate-tool-name",
        ),
    ),
)

TOOL_ERROR_SCENARIOS = (
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

# Scenarios that only make sense in the 2025 (handshake) transport. The 2026
# transport has no initialize step, so unsupported-min-version has no meaning.
ERA_2025_ONLY_SCENARIOS = ("unsupported-min-version",)

TOOLS_LIST_MODES = {
    "normal": "정상 tools 반환",
    "json-rpc-error": "JSON-RPC error 반환",
    "null": "tools: null 반환",
    "empty": "tools: [] 반환",
    "too-many": "너무 많은 tools 반환",
}

DEFAULT_CONFIG: dict[str, Any] = {
    # "2025" -> initialize handshake + session id (legacy).
    # "2026" -> stateless per-request _meta, no handshake/session.
    "protocolEra": "2025",
    # When True, the server refuses any request that is NOT on the 2026 transport
    # (i.e. legacy initialize-handshake clients) with -32022. Used to verify that
    # an older client which doesn't speak 2026-07-28 actually errors out against a
    # 2026-only server, instead of silently succeeding via the hospitable legacy path.
    "rejectLegacy": False,
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
        "duplicateToolName": False,
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
    if scenario == "duplicate-tool-name":
        config["toolsList"]["duplicateToolName"] = True
        return config
    if scenario in TOOL_ERROR_SCENARIOS:
        config["toolErrors"] = [scenario]
        return config
    return config


def long_description(service_name: str = SERVICE_NAME) -> str:
    prefix = f"{service_name} "
    return prefix + ("a" * (1051 - len(prefix)))


def normalize_config(raw_config: Any) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return default_config()

    config = default_config()

    protocol_era = str(raw_config.get("protocolEra", config["protocolEra"])).strip()
    if protocol_era not in {"2025", "2026"}:
        raise ValueError("protocolEra must be 2025 or 2026")
    config["protocolEra"] = protocol_era

    config["rejectLegacy"] = bool(raw_config.get("rejectLegacy", config["rejectLegacy"]))

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

    target = str(server.get("target", config["server"]["target"]))
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
    config["toolsList"]["duplicateToolName"] = (
        mode in {"normal", "too-many"} and bool(tools_list.get("duplicateToolName", False))
    )

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


def encode_config_token(raw_config: Any) -> str:
    config = compact_config(normalize_config(raw_config))
    if not config:
        return ""
    body = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def compact_config(config: dict[str, Any]) -> dict[str, Any]:
    default = default_config()

    def diff(value: Any, default_value: Any) -> Any:
        if isinstance(value, dict) and isinstance(default_value, dict):
            result = {
                key: diff(child_value, default_value.get(key))
                for key, child_value in value.items()
                if diff(child_value, default_value.get(key)) is not None
            }
            return result or None
        if value == default_value:
            return None
        return value

    return diff(config, default) or {}


def decode_config_token(raw_token: str) -> dict[str, Any]:
    token = raw_token.strip()
    if not token:
        raise ValueError("empty config token")
    padding = "=" * (-len(token) % 4)
    try:
        body = base64.urlsafe_b64decode(f"{token}{padding}".encode("ascii"))
        payload = json.loads(body.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid config token") from error
    return normalize_config(payload)


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
            return {"tools": [valid_tool("search_place", long_description())]}
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
        return [
            valid_tool(
                "long_description_case",
                long_description(service_name),
                service_name=service_name,
            )
        ]
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
        count = tools_list["tooManyCount"]
        generated_tools = [
            valid_tool(f"tool_{index}", service_name=service_name)
            for index in range(count)
        ]
        if tools_list.get("duplicateToolName") and count >= 2:
            generated_tools[1] = valid_tool("tool_0", service_name=service_name)
        return {"tools": generated_tools}
    if mode != "normal":
        return None

    configured_tools: list[dict[str, Any]] = []
    if tools_list.get("duplicateToolName"):
        configured_tools.extend(mutated_tool_for_error("duplicate-tool-name", service_name))
    for tool_error in config["toolErrors"]:
        configured_tools.extend(mutated_tool_for_error(tool_error, service_name))
    if configured_tools:
        return {"tools": configured_tools}
    return None


def request_meta(payload: Any) -> dict[str, Any]:
    """Extract params._meta as a dict (empty if absent/malformed)."""
    if not isinstance(payload, dict):
        return {}
    params = payload.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    return meta if isinstance(meta, dict) else {}


def request_era(payload: Any, headers: dict[str, str] | None = None) -> str:
    """Decide whether a request uses the 2026 (stateless) transport.

    The 2026 transport is identified by its *markers*, not by the version
    value: the ``Mcp-Method`` routing header, or a ``params._meta`` carrying the
    protocolVersion key. This is deliberate — a 2026 client asking for a version
    the server does not support must still be routed into the 2026 path so it
    gets -32022, rather than silently falling back to legacy. Legacy 2025 never
    sends ``Mcp-Method`` nor the ``_meta`` envelope, so it stays on the old path.
    """
    lowered = {name.lower(): value for name, value in (headers or {}).items()}
    if "mcp-method" in lowered:
        return "2026"
    if str(lowered.get("mcp-protocol-version", "")).strip() == PROTOCOL_VERSION_2026:
        return "2026"
    if META_PROTOCOL_VERSION in request_meta(payload):
        return "2026"
    return "2025"


def validate_2026_request(
    payload: Any,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Validate a 2026-07-28 request's headers and _meta envelope.

    Returns a JSON-RPC error dict if the request is malformed, else None.
    The HTTP layer maps any returned error to a 400 response.
    """
    headers = headers or {}
    request_id = payload.get("id") if isinstance(payload, dict) else None
    method = payload.get("method") if isinstance(payload, dict) else None
    meta = request_meta(payload)

    # Case-insensitive header lookup.
    lowered = {name.lower(): value for name, value in headers.items()}

    # Protocol version: header must match body _meta.
    header_version = str(lowered.get("mcp-protocol-version", "")).strip()
    meta_version = str(meta.get(META_PROTOCOL_VERSION, "")).strip()
    if not meta_version:
        return json_rpc_error(
            request_id, ERR_INVALID_PARAMS, f"Missing _meta '{META_PROTOCOL_VERSION}'"
        )
    if header_version and header_version != meta_version:
        return json_rpc_error(
            request_id,
            ERR_HEADER_MISMATCH,
            f"Header mismatch: MCP-Protocol-Version '{header_version}' "
            f"does not match body value '{meta_version}'",
        )
    if meta_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return json_rpc_error(
            request_id,
            ERR_UNSUPPORTED_VERSION,
            f"Unsupported protocol version '{meta_version}'. "
            f"Supported: {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}",
        )

    # clientCapabilities is a required envelope field.
    if META_CLIENT_CAPABILITIES not in meta:
        return json_rpc_error(
            request_id, ERR_INVALID_PARAMS, f"Missing _meta '{META_CLIENT_CAPABILITIES}'"
        )

    # Mcp-Method header is required and must match body method.
    header_method = lowered.get("mcp-method")
    if header_method is None:
        return json_rpc_error(request_id, ERR_HEADER_MISMATCH, "Missing Mcp-Method header")
    if header_method != method:
        return json_rpc_error(
            request_id,
            ERR_HEADER_MISMATCH,
            f"Header mismatch: Mcp-Method '{header_method}' does not match body '{method}'",
        )

    # Mcp-Name header is required for name-bearing methods and must match the body.
    if method in NAME_BEARING_METHODS:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        body_name = params.get(NAME_BEARING_METHODS[method])
        header_name = lowered.get("mcp-name")
        if header_name is None:
            return json_rpc_error(
                request_id, ERR_HEADER_MISMATCH, f"Missing Mcp-Name header for {method}"
            )
        if header_name != body_name:
            return json_rpc_error(
                request_id,
                ERR_HEADER_MISMATCH,
                f"Header mismatch: Mcp-Name '{header_name}' does not match body '{body_name}'",
            )
    return None


def json_rpc_result_2026(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a result in the 2026 envelope: resultType + _meta.serverInfo.

    The handler-provided result may set its own resultType (e.g. an error
    scenario that omits it); only fill defaults when absent.
    """
    enriched = dict(result)
    enriched.setdefault("resultType", "complete")
    meta = dict(enriched.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, {"name": SERVER_NAME, "version": SERVER_VERSION})
    enriched["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": enriched}


def handle_json_rpc(
    payload: Any,
    scenario: str = "ok",
    config: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    scenario = normalize_scenario(scenario)
    config = normalize_config(config) if config is not None else scenario_to_config(scenario)
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return 400, {}, json_rpc_error(None, -32600, "Invalid Request")

    request_id = payload.get("id")
    method = payload.get("method")
    is_notification = "id" not in payload

    # 2026-07-28: stateless, per-request metadata. Detect via header/_meta and,
    # for actual requests, validate the envelope before dispatching.
    is_2026 = request_era(payload, headers) == "2026"

    # 2026-only mode: a legacy (handshake) client that does not speak the 2026
    # transport is refused with -32022 instead of being served via the old path.
    # This is how we prove an older client actually breaks against a 2026 server.
    if config.get("rejectLegacy") and not is_2026 and not is_notification:
        return 400, {}, json_rpc_error(
            request_id,
            ERR_UNSUPPORTED_VERSION,
            "This server only supports protocol version "
            f"{PROTOCOL_VERSION_2026}. Legacy initialize-handshake clients are "
            f"not accepted. Supported: {PROTOCOL_VERSION_2026}",
        )

    if is_2026 and not is_notification:
        error = validate_2026_request(payload, headers)
        if error is not None:
            return 400, {}, error
        return handle_json_rpc_2026(payload, config, request_id, method)

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


def handle_json_rpc_2026(
    payload: dict[str, Any],
    config: dict[str, Any],
    request_id: Any,
    method: Any,
) -> tuple[int, dict[str, str], dict[str, Any] | None]:
    """Dispatch a validated 2026-07-28 request.

    Stateless: no initialize handshake, no session id. Results carry the 2026
    envelope (resultType + _meta.serverInfo) via json_rpc_result_2026.
    """
    if method == "initialize":
        # 2026 has no handshake, but a client may still probe. Respond without a
        # session id, advertising the negotiated version.
        result = {
            "protocolVersion": PROTOCOL_VERSION_2026,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "server/discover":
        # Modern (2026) connect-time probe — the stateless replacement for the
        # initialize handshake. Advertise supported versions + capabilities so
        # the SDK client can adopt() and proceed to tools/list, tools/call.
        result = {
            "supportedVersions": [PROTOCOL_VERSION_2026],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": config["mcp"]["identifier"], "version": SERVER_VERSION},
        }
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "ping":
        return 200, {}, json_rpc_result_2026(request_id, {})

    if method == "tools/list":
        if config["toolsList"]["mode"] == "json-rpc-error":
            return 200, {}, json_rpc_error(request_id, -32603, "Injected tools/list failure")
        scenario_tools = tools_for_config(config)
        result = dict(scenario_tools or {"tools": tools(config["mcp"]["serviceName"])})
        # 2026 cacheable-list hints are required fields on ListToolsResult.
        result.setdefault("ttlMs", 0)
        result.setdefault("cacheScope", "private")
        return 200, {}, json_rpc_result_2026(request_id, result)

    if method == "tools/call":
        result = call_tool(payload.get("params"))
        return 200, {}, json_rpc_result_2026(request_id, result)

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


def render_inspect_page() -> str:
    """Live viewer for recent /mcp exchanges (request headers + body, response).

    Plain (non f-string) template so the embedded JS braces need no escaping.
    The page polls /inspect/log and re-renders on change.
    """
    tool_names = [tool["name"] for tool in tools()]
    return _INSPECT_PAGE.replace("__CONVERT_TOOLS__", json.dumps(tool_names))


_INSPECT_PAGE = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unit Expert MCP · Wire Inspector</title>
  <style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #0f1420;
      color: #e6e9ef;
      line-height: 1.45;
    }
    header {
      position: sticky; top: 0; z-index: 10;
      display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
      padding: 14px 20px;
      background: #161d2e;
      border-bottom: 1px solid #2a3346;
    }
    header h1 { font-size: 16px; margin: 0; font-weight: 700; }
    header .meta { color: #8b95a7; font-size: 12px; }
    header .spacer { flex: 1; }
    button, label.toggle {
      font: inherit; font-size: 12px;
      background: #223049; color: #cdd6e6; border: 1px solid #33415c;
      border-radius: 8px; padding: 6px 12px; cursor: pointer;
    }
    button:hover { background: #2b3b57; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; background: #3ddc84; display: inline-block; margin-right: 6px; }
    .status-dot.paused { background: #f5a623; }
    main { padding: 16px 20px 60px; display: flex; flex-direction: column; gap: 12px; }
    .empty { color: #6b7488; padding: 40px; text-align: center; }
    .row {
      border: 1px solid #2a3346; border-radius: 12px; overflow: hidden;
      background: #131a29;
    }
    .row-head {
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
      padding: 10px 14px; cursor: pointer; user-select: none;
    }
    .row-head:hover { background: #182135; }
    .badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 6px; }
    .badge.era-2026 { background: #0e766e; color: #eafffb; }
    .badge.era-2025 { background: #394a63; color: #cdd6e6; }
    .badge.ok { background: #14532d; color: #d5f5e0; }
    .badge.err { background: #5a1d1d; color: #ffd9d5; }
    .method { font-weight: 700; color: #9ecbff; }
    .time { color: #6b7488; font-size: 12px; }
    .rid { color: #6b7488; font-size: 12px; }
    .row-head .spacer { flex: 1; }
    .row-body { display: none; border-top: 1px solid #2a3346; padding: 12px 14px; gap: 14px; }
    .row.open .row-body { display: grid; grid-template-columns: 1fr 1fr; }
    @media (max-width: 820px) { .row.open .row-body { grid-template-columns: 1fr; } }
    .pane h3 { margin: 0 0 6px; font-size: 12px; color: #8b95a7; text-transform: uppercase; letter-spacing: 0.05em; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12px; background: #0c1220; border: 1px solid #212a3d; border-radius: 8px; padding: 10px; }
    .hdr-line { color: #b7c3d8; }
    .hdr-line b { color: #9ecbff; font-weight: 600; }
    .console {
      border: 1px solid #2a3346; border-radius: 12px; background: #131a29;
      padding: 14px 16px; display: flex; flex-direction: column; gap: 12px;
    }
    .console h2 { margin: 0; font-size: 14px; font-weight: 700; }
    .console .hint { color: #8b95a7; font-size: 12px; margin: -4px 0 0; }
    .console-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
    .field-c { display: flex; flex-direction: column; gap: 4px; }
    .field-c label { font-size: 11px; color: #8b95a7; text-transform: uppercase; letter-spacing: 0.04em; }
    .console select, .console input {
      font: inherit; font-size: 13px; background: #0c1220; color: #e6e9ef;
      border: 1px solid #33415c; border-radius: 8px; padding: 7px 10px; min-width: 90px;
    }
    .console input.val { width: 90px; }
    .send-btn { background: #0e766e; color: #eafffb; border-color: #0e766e; font-weight: 700; padding: 8px 20px; }
    .send-btn:hover { background: #109c91; }
    .send-btn:disabled { opacity: 0.5; cursor: default; }
    .console .arg-fields { display: contents; }
    .console .arg-fields.hidden { display: none; }
    .call-note { color: #6b7488; font-size: 12px; margin: 0; }
    .console code { background: #223049; padding: 1px 5px; border-radius: 4px; font-size: 11px; }
    .reset-btn { background: #223049; color: #cdd6e6; border: 1px solid #33415c; font-weight: 600; padding: 8px 14px; border-radius: 8px; cursor: pointer; }
    .reset-btn:hover { background: #2b3b57; }
    .editor-row { display: flex; gap: 12px; flex-wrap: wrap; }
    .editor-col { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 260px; }
    .editor-col label { font-size: 11px; color: #8b95a7; text-transform: uppercase; letter-spacing: 0.04em; }
    .console textarea {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
      background: #0c1120; color: #e6e9ef; border: 1px solid #33415c;
      border-radius: 8px; padding: 8px 10px; resize: vertical; width: 100%;
    }
    .console textarea:focus { outline: none; border-color: #0e766e; }
  </style>
</head>
<body>
  <header>
    <h1>🔌 MCP Wire Inspector</h1>
    <span class="meta" id="meta">최근 /mcp 요청·응답을 실시간으로 표시합니다.</span>
    <span class="spacer"></span>
    <label class="toggle"><span class="status-dot" id="dot"></span><span id="liveLabel">실시간</span> ·
      <input type="checkbox" id="liveToggle" checked style="vertical-align:middle"></label>
    <button id="clearBtn" type="button">화면 비우기</button>
  </header>
  <main>
    <div class="console">
      <h2>▶ 요청 보내기</h2>
      <p class="hint">이 페이지의 /mcp로 2026-07-28 요청을 직접 조립해서 보냅니다. (브라우저 fetch — SDK와 동일한 wire)</p>
      <div class="console-row">
        <div class="field-c">
          <label>메서드</label>
          <select id="cMethod">
            <option value="server/discover">server/discover</option>
            <option value="tools/list">tools/list</option>
            <option value="tools/call" selected>tools/call</option>
            <option value="ping">ping</option>
          </select>
        </div>
        <div class="field-c arg-fields" id="callFields">
          <div class="field-c">
            <label>도구 (tools/call)</label>
            <select id="cTool"></select>
          </div>
          <div class="field-c" id="fVal">
            <label>value</label>
            <input class="val" id="cValue" type="number" value="1">
          </div>
          <div class="field-c" id="fFrom">
            <label>from_unit</label>
            <input class="val" id="cFrom" type="text" value="m">
          </div>
          <div class="field-c" id="fTo">
            <label>to_unit</label>
            <input class="val" id="cTo" type="text" value="cm">
          </div>
        </div>
        <div class="field-c">
          <label>&nbsp;</label>
          <button class="reset-btn" id="fillBtn" type="button">↺ 컨트롤로 채우기</button>
        </div>
      </div>
      <p class="hint">아래 헤더·본문을 직접 수정해서 보낼 수 있습니다. 예: <code>_meta</code>를 지우거나 헤더를 틀리게 바꿔 위반 응답(-32602/-32020/-32022)을 확인하세요. <b>보내기는 아래 내용을 그대로 전송</b>합니다.</p>
      <div class="editor-row">
        <div class="editor-col">
          <label>헤더 (한 줄에 <code>Key: Value</code>)</label>
          <textarea id="hdrBox" spellcheck="false" rows="5"></textarea>
        </div>
        <div class="editor-col">
          <label>본문 (JSON — 그대로 전송, 깨진 JSON도 가능)</label>
          <textarea id="bodyBox" spellcheck="false" rows="10"></textarea>
        </div>
      </div>
      <div class="console-row">
        <button class="send-btn" id="sendBtn" type="button">보내기</button>
        <p class="call-note" id="callNote"></p>
      </div>
    </div>
    <div class="empty" id="empty">아직 기록된 요청이 없습니다. 위에서 요청을 보내거나, 외부 클라이언트가 이 서버의 /mcp로 요청하면 여기에 나타납니다.</div>
    <div id="list"></div>
  </main>
  <script>
    const listEl = document.getElementById("list");
    const emptyEl = document.getElementById("empty");
    const metaEl = document.getElementById("meta");
    const dotEl = document.getElementById("dot");
    const liveToggle = document.getElementById("liveToggle");
    const liveLabel = document.getElementById("liveLabel");
    const openRows = new Set();
    let hideBefore = 0;
    let lastSignature = "";

    // --- Request console -------------------------------------------------
    const CONVERT_TOOLS = __CONVERT_TOOLS__;
    const protocolVersion2026 = "2026-07-28";
    const cMethod = document.getElementById("cMethod");
    const cTool = document.getElementById("cTool");
    const callFields = document.getElementById("callFields");
    const sendBtn = document.getElementById("sendBtn");
    const fillBtn = document.getElementById("fillBtn");
    const callNote = document.getElementById("callNote");
    const cValue = document.getElementById("cValue");
    const cFrom = document.getElementById("cFrom");
    const cTo = document.getElementById("cTo");
    const hdrBox = document.getElementById("hdrBox");
    const bodyBox = document.getElementById("bodyBox");

    CONVERT_TOOLS.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      cTool.appendChild(opt);
    });

    function syncConsole() {
      const isCall = cMethod.value === "tools/call";
      callFields.classList.toggle("hidden", !isCall);
      // list_supported_units takes no args → hide the value/unit inputs.
      const needsArgs = isCall && cTool.value !== "list_supported_units";
      document.getElementById("fVal").style.display = needsArgs ? "" : "none";
      document.getElementById("fFrom").style.display = needsArgs ? "" : "none";
      document.getElementById("fTo").style.display = needsArgs ? "" : "none";
    }
    cMethod.addEventListener("change", syncConsole);
    cTool.addEventListener("change", syncConsole);

    // Build a *correct* draft from the controls. The user can then freely
    // edit the header/body boxes (delete _meta, break a header) before sending.
    function buildDraft() {
      const method = cMethod.value;
      const meta = {
        "io.modelcontextprotocol/protocolVersion": protocolVersion2026,
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": { name: "inspect-console", version: "1.0" }
      };
      const params = { _meta: meta };
      const headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "MCP-Protocol-Version": protocolVersion2026,
        "Mcp-Method": method
      };
      if (method === "tools/call") {
        const tool = cTool.value;
        params.name = tool;
        headers["Mcp-Name"] = tool;
        if (tool === "list_supported_units") {
          params.arguments = {};
        } else {
          params.arguments = {
            value: Number(cValue.value),
            from_unit: cFrom.value.trim(),
            to_unit: cTo.value.trim()
          };
        }
      }
      const body = { jsonrpc: "2.0", id: 1, method, params };
      return { headers, body };
    }

    function fillFromControls() {
      const { headers, body } = buildDraft();
      hdrBox.value = Object.keys(headers).map((k) => `${k}: ${headers[k]}`).join("\\n");
      bodyBox.value = JSON.stringify(body, null, 2);
      callNote.textContent = "컨트롤 값으로 채웠습니다. 이제 자유롭게 수정 후 보내기.";
    }
    fillBtn.addEventListener("click", fillFromControls);

    // Parse the header box verbatim: each non-empty line "Key: Value".
    function parseHeaders(text) {
      const headers = {};
      text.split("\\n").forEach((line) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        const idx = trimmed.indexOf(":");
        if (idx === -1) return;      // malformed line → skipped, on purpose
        const key = trimmed.slice(0, idx).trim();
        const value = trimmed.slice(idx + 1).trim();
        if (key) headers[key] = value;
      });
      return headers;
    }

    async function sendRequest() {
      // Send whatever is in the boxes, verbatim — no correction, no validation.
      const headers = parseHeaders(hdrBox.value);
      const rawBody = bodyBox.value;
      sendBtn.disabled = true;
      const prev = sendBtn.textContent;
      sendBtn.textContent = "전송 중…";
      callNote.textContent = "";
      try {
        const res = await fetch("/mcp", { method: "POST", headers, body: rawBody });
        callNote.textContent = `HTTP ${res.status} · 아래 로그 최상단에 기록됨`;
        lastSignature = "";        // force re-render on next poll
        poll();
      } catch (e) {
        callNote.textContent = "요청 실패: " + (e && e.message ? e.message : e);
      } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = prev;
      }
    }
    sendBtn.addEventListener("click", sendRequest);
    syncConsole();
    fillFromControls();   // seed the boxes with a valid request on load

    function esc(value) {
      return String(value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    function pretty(value) {
      if (typeof value === "string") return esc(value);
      try { return esc(JSON.stringify(value, null, 2)); } catch (e) { return esc(String(value)); }
    }
    function headerLines(headers) {
      const keys = Object.keys(headers || {});
      if (!keys.length) return "<span class=\\"hdr-line\\">(없음)</span>";
      return keys.map((k) => `<div class="hdr-line"><b>${esc(k)}</b>: ${esc(headers[k])}</div>`).join("");
    }
    function render(exchanges) {
      const visible = exchanges.filter((x) => x.seq > hideBefore);
      emptyEl.style.display = visible.length ? "none" : "block";
      metaEl.textContent = `표시 ${visible.length}건 · 버퍼 ${exchanges.length}건 (최대 50)`;
      listEl.innerHTML = visible.map((x) => {
        const ok = x.status < 400;
        const open = openRows.has(x.seq) ? " open" : "";
        const errCode = x.responseBody && x.responseBody.error ? x.responseBody.error.code : null;
        const statusText = errCode !== null && errCode !== undefined ? `${x.status} · ${errCode}` : x.status;
        return `
        <div class="row${open}" data-seq="${x.seq}">
          <div class="row-head">
            <span class="time">${esc(x.time)}</span>
            <span class="badge era-${x.era === "2026" ? "2026" : "2025"}">${esc(x.era)}</span>
            <span class="method">${esc(x.method || x.httpMethod)}</span>
            <span class="spacer"></span>
            <span class="badge ${ok ? "ok" : "err"}">${esc(statusText)}</span>
          </div>
          <div class="row-body">
            <div class="pane">
              <h3>요청 헤더</h3>
              <pre>${headerLines(x.requestHeaders)}</pre>
              <h3 style="margin-top:10px">요청 바디</h3>
              <pre>${pretty(x.requestBody)}</pre>
            </div>
            <div class="pane">
              <h3>응답 (HTTP ${esc(x.status)})</h3>
              <pre>${pretty(x.responseBody)}</pre>
            </div>
          </div>
        </div>`;
      }).join("");
    }
    listEl.addEventListener("click", (event) => {
      const row = event.target.closest(".row");
      if (!row) return;
      const seq = Number(row.dataset.seq);
      if (openRows.has(seq)) { openRows.delete(seq); row.classList.remove("open"); }
      else { openRows.add(seq); row.classList.add("open"); }
    });
    document.getElementById("clearBtn").addEventListener("click", () => {
      // Client-side clear: hide everything currently buffered without touching the server.
      const rows = listEl.querySelectorAll(".row");
      let maxSeq = hideBefore;
      rows.forEach((r) => { maxSeq = Math.max(maxSeq, Number(r.dataset.seq)); });
      hideBefore = maxSeq;
      openRows.clear();
      lastSignature = "";
      poll();
    });
    liveToggle.addEventListener("change", () => {
      dotEl.classList.toggle("paused", !liveToggle.checked);
      liveLabel.textContent = liveToggle.checked ? "실시간" : "일시정지";
    });
    async function poll() {
      try {
        const res = await fetch("/inspect/log", { cache: "no-store" });
        const data = await res.json();
        const exchanges = data.exchanges || [];
        const signature = exchanges.length ? `${exchanges[0].seq}:${exchanges.length}:${hideBefore}` : `0:0:${hideBefore}`;
        if (signature !== lastSignature) {
          lastSignature = signature;
          render(exchanges);
        }
      } catch (e) { /* keep polling */ }
    }
    setInterval(() => { if (liveToggle.checked) poll(); }, 1000);
    poll();
  </script>
</body>
</html>"""


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
            # initialize/handshake-only scenarios don't exist in the stateless
            # 2026 transport, so hide them when the 2026 toggle is active.
            row_class = "check-row"
            if scenario in ERA_2025_ONLY_SCENARIOS:
                row_class += " era-2025-only"
            controls.append(
                f"""
        <label class="{row_class}">
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
      transition: background-color 160ms ease, border-color 160ms ease, box-shadow 160ms ease;
    }}
    .endpoint.needs-apply {{
      background: #fff7ed;
      border-color: #fdba74;
    }}
    .endpoint.url-updated {{
      background: #ecfdf5;
      border-color: #86efac;
      box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.14);
      animation: urlPulse 900ms ease-out 1;
    }}
    @keyframes urlPulse {{
      0% {{ box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.36); }}
      100% {{ box-shadow: 0 0 0 8px rgba(34, 197, 94, 0); }}
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
    .endpoint-hint {{
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 12px;
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
    .usage-details {{
      padding: 0;
      overflow: hidden;
      background: #eef7f4;
      border-color: #b8d9d0;
    }}
    .usage-details .usage-body {{
      display: grid;
      gap: 14px;
      padding: 16px;
      border-top: 1px solid #c7e2da;
    }}
    .usage-details .usage-steps {{
      margin: 0;
      padding-left: 20px;
      color: var(--text);
      font-size: 14px;
    }}
    .usage-details .usage-steps li + li {{
      margin-top: 7px;
    }}
    .usage-details .usage-note {{
      margin: 0;
      padding: 10px 11px;
      border: 1px solid #bdded4;
      border-radius: 6px;
      background: #f8fffc;
      color: #245a4c;
      font-size: 13px;
      line-height: 1.55;
    }}
    .usage-details .usage-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .usage-details .usage-box {{
      display: grid;
      gap: 6px;
      min-width: 0;
      padding: 10px 11px;
      border: 1px solid #d7e6e2;
      border-radius: 6px;
      background: #fff;
    }}
    .usage-details .usage-box strong {{
      font-size: 13px;
    }}
    .usage-details .usage-box p {{
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .usage-toggle-label {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 28px;
      padding: 0 10px;
      border: 1px solid #aacfc5;
      border-radius: 999px;
      color: #245a4c;
      background: #fff;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .usage-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .usage-details[open] .usage-toggle-label::before {{
      content: "접기";
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
    .policy-toggle-label {{
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
    .policy-toggle-label::before {{
      content: "펼쳐보기";
    }}
    .policy-details[open] .policy-toggle-label::before {{
      content: "접기";
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
      .usage-details .usage-grid {{ grid-template-columns: 1fr; }}
      .inline-field {{ grid-template-columns: 1fr; }}
      .terminal {{ min-height: 360px; }}
      .terminal pre {{ min-height: 314px; }}
    }}
    .era-toggle {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 6px;
      flex-shrink: 0;
    }}
    .era-toggle-label {{
      font-size: 12px;
      font-weight: 600;
      color: var(--muted);
      letter-spacing: 0.02em;
    }}
    .era-toggle-buttons {{
      display: inline-flex;
      background: #e9eef7;
      border: 1px solid #c8d1df;
      border-radius: 10px;
      padding: 3px;
      gap: 3px;
    }}
    .era-button {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--muted);
      font-size: 14px;
      font-weight: 700;
      padding: 6px 18px;
      border-radius: 8px;
      cursor: pointer;
      transition: background-color 140ms ease, color 140ms ease, box-shadow 140ms ease;
    }}
    .era-button.is-active {{
      background: var(--accent);
      color: #ffffff;
      box-shadow: 0 1px 2px rgba(15, 118, 110, 0.35);
    }}
    .era-toggle-hint {{
      margin: 0;
      font-size: 12px;
      color: var(--muted);
      max-width: 260px;
      text-align: right;
    }}
    body[data-era="2026"] .era-2025-only {{
      display: none !important;
    }}
    body[data-era="2026"] .era-2026-badge {{
      display: inline-flex;
    }}
    .era-2026-badge {{
      display: none;
      margin-left: 8px;
      font-size: 11px;
      font-weight: 700;
      color: var(--accent-strong);
      background: #d7f2ee;
      border-radius: 6px;
      padding: 2px 8px;
      vertical-align: middle;
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
      <div class="era-toggle" role="group" aria-label="프로토콜 버전 선택">
        <span class="era-toggle-label">프로토콜 버전</span>
        <div class="era-toggle-buttons">
          <button type="button" class="era-button" data-era="2025">2025</button>
          <button type="button" class="era-button" data-era="2026">2026</button>
        </div>
        <p class="era-toggle-hint" id="eraHint"></p>
      </div>
    </header>
    <div class="workspace">
      <div class="left-column">
        {notice}
        <span id="activeTitle" hidden>{escape(active_title)}</span>
        <span id="activeGroup" hidden>{escape(active_group)}</span>
        <span id="activeScenario" hidden>{escape(active_scenario)}</span>
        <div class="control-grid">
          <details class="control-card full policy-details usage-details">
            <summary>
              <h2>사용 설명서</h2>
              <span class="usage-toggle-label" aria-hidden="true"></span>
            </summary>
            <div class="usage-body">
              <ol class="usage-steps">
                <li><strong>MCP 상태 제어 페이지</strong>에 접속합니다.</li>
                <li>테스트할 MCP의 <strong>MCP 식별자</strong>와 <strong>MCP 이름(서비스 이름)</strong>으로 변경합니다. 필요한 경우 <strong>커스텀 헤더 설정 여부</strong>도 설정합니다.</li>
                <li>원하는 시나리오를 선택한 뒤 <strong>적용</strong> 버튼을 누릅니다.</li>
                <li>좌측 상단의 <strong>테스트용 MCP URL</strong> 옆 <strong>복사</strong> 버튼을 누릅니다.</li>
                <li>복사된 URL을 MCP 등록/수정 요청의 <strong>endpoint URL</strong> 값에 넣고 검증합니다.</li>
              </ol>
              <p class="usage-note">
                적용 버튼은 서버 상태를 바꾸는 것이 아니라, 현재 설정이 포함된 테스트용 MCP URL을 생성합니다.
              </p>
            </div>
          </details>
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
              <span class="policy-toggle-label" aria-hidden="true"></span>
            </summary>
            <div class="policy-reference" id="scenarioRows">
              {policy_reference}
            </div>
          </details>
        </div>
      </div>
      <aside class="right-column">
        <section class="endpoint" aria-label="MCP URL">
          <div class="endpoint-label">테스트용 MCP URL</div>
          <div class="endpoint-row">
            <code id="mcpUrl">{escape(PUBLIC_MCP_URL)}</code>
            <button id="copyMcpUrl" class="copy-button" type="button">복사</button>
          </div>
          <p id="mcpUrlHint" class="endpoint-hint">설정을 바꾼 뒤 적용을 누르면 이 URL이 갱신됩니다.</p>
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
            <div class="sidebar-custom-header-row era-2026-only" id="rejectLegacyRow">
              <div class="sidebar-custom-header-label">
                <span class="field-title">
                  지원 프로토콜 버전 2026-07-28로 고정
                  <span class="field-hint">이 버전만 지원합니다. 구버전(2025 이하) initialize 핸드셰이크 요청은 -32022로 거부됩니다.</span>
                </span>
                <label class="custom-header-toggle">
                  <input type="checkbox" id="rejectLegacyEnabled">
                  <span class="toggle-track"><span class="toggle-thumb"></span></span>
                </label>
              </div>
              <div id="rejectLegacyHint" class="custom-header-hint" hidden>
                <span class="hint-label">동작</span>
                <code>-32022 UnsupportedProtocolVersion</code>
                <p class="hint-desc">2026 전송 마커(_meta·Mcp-Method)가 없는 요청은 모두 거부됩니다. 최신 버전을 아직 지원하지 않는 클라이언트가 실제로 실패하는지 확인할 때 사용합니다.</p>
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
    const endpointCard = document.querySelector(".endpoint");
    const mcpUrlHint = document.getElementById("mcpUrlHint");
    const applyConfig = document.getElementById("applyConfig");
    const resetConfig = document.getElementById("resetConfig");
    const toolErrorDisabledNote = document.getElementById("toolErrorDisabledNote");
    const toolErrorGrid = document.getElementById("toolErrorGrid");
    const summaryServerScenarios = document.getElementById("summaryServerScenarios");
    const summaryToolScenarios = document.getElementById("summaryToolScenarios");
    const mcpIdentifier = document.getElementById("mcpIdentifier");
    const mcpServiceName = document.getElementById("mcpServiceName");
    const publicMcpUrl = {json.dumps(PUBLIC_MCP_URL)};
    const configQueryParam = {json.dumps(CONFIG_QUERY_PARAM)};
    const protocolVersion2026 = {json.dumps(PROTOCOL_VERSION_2026)};
    const metaProtocolVersionKey = {json.dumps(META_PROTOCOL_VERSION)};
    const metaClientCapabilitiesKey = {json.dumps(META_CLIENT_CAPABILITIES)};
    const metaClientInfoKey = {json.dumps(META_CLIENT_INFO)};
    const eraButtons = Array.from(document.querySelectorAll(".era-button"));
    const eraHint = document.getElementById("eraHint");
    const eraHints = {{
      "2025": "initialize 핸드셰이크 + 세션ID를 쓰는 기존 방식입니다.",
      "2026": "핸드셰이크 없이 요청마다 _meta·헤더를 싣는 stateless 방식입니다."
    }};
    let appliedConfig = clone(initialConfig);
    let currentEra = appliedConfig.protocolEra === "2026" ? "2026" : "2025";

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

    function compactConfig(config) {{
      function diff(value, defaultValue) {{
        if (
          value &&
          defaultValue &&
          typeof value === "object" &&
          typeof defaultValue === "object" &&
          !Array.isArray(value) &&
          !Array.isArray(defaultValue)
        ) {{
          const result = {{}};
          Object.entries(value).forEach(([key, childValue]) => {{
            const childDiff = diff(childValue, defaultValue[key]);
            if (childDiff !== undefined) result[key] = childDiff;
          }});
          return Object.keys(result).length ? result : undefined;
        }}
        return JSON.stringify(value) === JSON.stringify(defaultValue) ? undefined : value;
      }}
      return diff(config, defaultConfig) || {{}};
    }}

    function encodeConfig(config) {{
      const compact = compactConfig(config);
      if (!Object.keys(compact).length) return "";
      const json = JSON.stringify(compact);
      const bytes = new TextEncoder().encode(json);
      let binary = "";
      bytes.forEach((byte) => {{
        binary += String.fromCharCode(byte);
      }});
      return btoa(binary)
        .replace(/\\+/g, "-")
        .replace(/\\//g, "_")
        .replace(/=+$/g, "");
    }}

    function buildMcpUrl(config) {{
      const url = new URL(publicMcpUrl);
      const token = encodeConfig(config);
      if (token) {{
        url.searchParams.set(configQueryParam, token);
      }} else {{
        url.searchParams.delete(configQueryParam);
      }}
      return url.toString();
    }}

    function buildLocalMcpPath(config) {{
      const token = encodeConfig(config);
      return token ? `/mcp?${{configQueryParam}}=${{encodeURIComponent(token)}}` : "/mcp";
    }}

    function markUrlNeedsApply() {{
      endpointCard.classList.remove("url-updated");
      endpointCard.classList.add("needs-apply");
      mcpUrlHint.textContent = "선택값이 바뀌었습니다. 적용을 누르면 테스트용 URL이 갱신됩니다.";
      copyMcpUrl.textContent = "복사";
    }}

    function setGeneratedUrl(config, highlight) {{
      mcpUrl.textContent = buildMcpUrl(config);
      endpointCard.classList.remove("needs-apply");
      if (highlight) {{
        endpointCard.classList.remove("url-updated");
        void endpointCard.offsetWidth;
        endpointCard.classList.add("url-updated");
        mcpUrlHint.textContent = "URL이 갱신됐습니다. 이 URL을 복사해서 FE/Swagger내 Endpoint URL 필드에 넣어주세요.";
        copyMcpUrl.textContent = "이 URL 복사";
      }} else {{
        endpointCard.classList.remove("url-updated");
        mcpUrlHint.textContent = "이 URL은 현재 설정을 포함합니다. 복사해서 FE/Swagger에 넣으면 같은 응답을 반환합니다.";
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
      if (config.toolsList.duplicateToolName) scenarios.push("duplicate-tool-name");
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
      const rejectLegacyEl = document.getElementById("rejectLegacyEnabled");
      if (rejectLegacyEl) {{
        rejectLegacyEl.checked = !!config.rejectLegacy;
        syncRejectLegacyHint();
      }}
      syncEnabledStates(config);
      updateSummary(config, false);
    }}

    function collectConfig() {{
      const serverErrors = selectedServerErrors();
      const mode = toolsListModeFromServerErrors(serverErrors);
      const duplicateToolName =
        ["normal", "too-many"].includes(mode) && serverErrors.includes("duplicate-tool-name");
      const toolErrors = mode === "normal" ? selectedToolErrors() : [];
      return {{
        protocolEra: currentEra,
        rejectLegacy: currentEra === "2026" && document.getElementById("rejectLegacyEnabled").checked,
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
          tooManyCount: 21,
          duplicateToolName
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
      handleControlChange();
    }});

    function syncRejectLegacyHint() {{
      const enabled = document.getElementById("rejectLegacyEnabled").checked;
      document.getElementById("rejectLegacyHint").hidden = !enabled;
    }}

    document.getElementById("rejectLegacyEnabled").addEventListener("change", () => {{
      syncRejectLegacyHint();
      handleControlChange();
    }});

    function syncEnabledStates(config) {{
      const toolErrorsEnabled = config.toolsList.mode === "normal";
      const duplicateToolEnabled = ["normal", "too-many"].includes(config.toolsList.mode);
      toolErrorDisabledNote.classList.toggle("visible", !toolErrorsEnabled);
      toolErrorGrid.classList.toggle("is-disabled", !toolErrorsEnabled);
      document.querySelectorAll('input[name="toolErrors"]').forEach((input) => {{
        input.disabled = !toolErrorsEnabled;
        if (!toolErrorsEnabled) {{
          input.checked = false;
        }}
      }});
      const duplicateToolInput = document.querySelector(
        'input[name="serverErrors"][value="duplicate-tool-name"]'
      );
      if (duplicateToolInput) {{
        duplicateToolInput.disabled = !duplicateToolEnabled;
        if (!duplicateToolEnabled) {{
          duplicateToolInput.checked = false;
        }}
      }}
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
      if (config.toolsList.duplicateToolName) {{
        parts.push(titleForToolError("duplicate-tool-name"));
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
        config.toolsList.mode !== "normal" ||
        config.toolsList.duplicateToolName;
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

    async function postMcp(payload, config, extraHeaders) {{
      const era = (config && config.protocolEra === "2026") ? "2026" : "2025";
      const headers = {{ "Content-Type": "application/json" }};
      if (era === "2026") {{
        // Stateless transport: single JSON response, per-request routing headers.
        headers["Accept"] = "application/json";
        headers["MCP-Protocol-Version"] = protocolVersion2026;
        headers["Mcp-Method"] = payload.method;
        if (payload.params && typeof payload.params.name === "string") {{
          headers["Mcp-Name"] = payload.params.name;
        }}
      }} else {{
        headers["Accept"] = "application/json, text/event-stream";
        headers["MCP-Protocol-Version"] = "2025-03-26";
      }}
      Object.assign(headers, extraHeaders || {{}});
      const response = await fetch(buildLocalMcpPath(config), {{
        method: "POST",
        headers,
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

    function meta2026() {{
      const meta = {{}};
      meta[metaProtocolVersionKey] = protocolVersion2026;
      meta[metaClientCapabilitiesKey] = {{}};
      meta[metaClientInfoKey] = {{ name: "scenario-ui", version: "1.0" }};
      return meta;
    }}

    async function refreshPreview(config) {{
      const title = activeTitle.textContent;
      const group = activeGroup.textContent;
      const era = (config && config.protocolEra === "2026") ? "2026" : "2025";
      preview.textContent = `$ 구분: ${{group}}\\n$ 설정: ${{title}}\\n$ POST /mcp (${{era}})\\n...`;
      try {{
        if (era === "2026") {{
          // No handshake: send tools/list directly with the _meta envelope.
          const toolsList = await postMcp({{
            jsonrpc: "2.0",
            id: 1,
            method: "tools/list",
            params: {{ _meta: meta2026() }}
          }}, config);
          const toolsCall = await postMcp({{
            jsonrpc: "2.0",
            id: 2,
            method: "tools/call",
            params: {{
              name: "convert_length",
              arguments: {{ value: 1, from_unit: "m", to_unit: "cm" }},
              _meta: meta2026()
            }}
          }}, config);
          preview.textContent = [
            `$ 구분: ${{group}}`,
            `$ 설정: ${{title}}`,
            `$ 프로토콜: ${{protocolVersion2026}} (stateless)`,
            `$ 적용 JSON`,
            pretty(config),
            "",
            formatHttp("POST /mcp tools/list", toolsList),
            "",
            formatHttp("POST /mcp tools/call convert_length", toolsCall)
          ].join("\\n");
          return;
        }}
        const initialize = await postMcp({{
          jsonrpc: "2.0",
          id: 1,
          method: "initialize",
          params: {{
            protocolVersion: "2025-03-26",
            capabilities: {{}},
            clientInfo: {{ name: "scenario-ui", version: "1.0" }}
          }}
        }}, config);
        const toolsList = await postMcp({{
          jsonrpc: "2.0",
          id: 2,
          method: "tools/list",
          params: {{}}
        }}, config);
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

    function setEra(era, options) {{
      const opts = options || {{}};
      currentEra = era === "2026" ? "2026" : "2025";
      document.body.setAttribute("data-era", currentEra);
      eraButtons.forEach((button) => {{
        button.classList.toggle("is-active", button.dataset.era === currentEra);
      }});
      if (eraHint) {{
        eraHint.textContent = eraHints[currentEra] || "";
      }}
      if (currentEra === "2026") {{
        // 2025-only scenarios are hidden in 2026; clear them so a stale check
        // doesn't leak into collectConfig via the :checked query.
        document.querySelectorAll(".era-2025-only input[type=checkbox]").forEach((input) => {{
          input.checked = false;
        }});
      }} else {{
        // Symmetrically, clear 2026-only toggles when returning to 2025.
        document.querySelectorAll(".era-2026-only input[type=checkbox]").forEach((input) => {{
          input.checked = false;
        }});
        syncRejectLegacyHint();
      }}
      if (opts.silent) return;
      // Switching era invalidates 2025-only selections; recollect and re-apply.
      const config = collectConfig();
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      refreshPreview(appliedConfig);
    }}

    eraButtons.forEach((button) => {{
      button.addEventListener("click", () => setEra(button.dataset.era));
    }});

    applyConfig.addEventListener("click", () => {{
      const config = collectConfig();
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      flashButton(applyConfig, "URL 생성됨", "적용");
      refreshPreview(appliedConfig);
    }});

    resetConfig.addEventListener("click", () => {{
      const config = clone(defaultConfig);
      appliedConfig = clone(config);
      setControls(appliedConfig);
      updateSummary(appliedConfig, true);
      setGeneratedUrl(appliedConfig, true);
      flashButton(resetConfig, "초기화됨", "초기화");
      refreshPreview(appliedConfig);
    }});

    function handleControlChange(event) {{
      if (event && event.target && event.target.name === "serverErrors") {{
        enforceExclusiveScenario(event.target);
      }}
      const config = collectConfig();
      syncEnabledStates(config);
      updateSummary(config, false);
      markUrlNeedsApply();
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

    setEra(currentEra, {{ silent: true }});
    setControls(appliedConfig);
    updateSummary(appliedConfig, true);
    setGeneratedUrl(appliedConfig, false);
    refreshPreview(appliedConfig);
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

        if path == "/inspect":
            self.send_html(200, render_inspect_page())
            return

        if path == "/inspect/log":
            self.send_json(200, {"exchanges": inspect_snapshot()})
            return

        if path != "/mcp":
            self.send_text(404, "Not found")
            return

        # 2026-07-28 removed the GET stream endpoint (stateless). A 2026 client
        # (detected via MCP-Protocol-Version) gets 405; legacy keeps the stream.
        if request_era(None, self.headers) == "2026":
            self.send_405()
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.send_text(400, str(error))
            return
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

    def do_DELETE(self) -> None:
        # 2026-07-28 is stateless: there is no session to terminate, so DELETE
        # /mcp is not allowed. Legacy also has no DELETE handling here.
        path = urlparse(self.path).path
        if path == "/mcp":
            self.send_405()
            return
        self.send_text(404, "Not found")

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

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            error = json_rpc_error(None, -32700, "Parse error")
            record_exchange("POST", path, self.headers, raw_body.decode("utf-8", "replace"), 400, error)
            self.send_json(400, error)
            return

        # 2026-07-28 is stateless and single-shot; a client may accept only
        # application/json. Legacy (2025) still requires both media types.
        accept = self.headers.get("Accept", "")
        if request_era(payload, self.headers) == "2026":
            if "application/json" not in accept and "*/*" not in accept:
                record_exchange("POST", path, self.headers, payload, 400,
                                {"error": "Invalid Accept header. Expected APPLICATION_JSON"})
                self.send_text(400, "Invalid Accept header. Expected APPLICATION_JSON")
                return
        elif "application/json" not in accept or "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON")
            return

        try:
            config = self.request_config()
        except ValueError as error:
            self.send_text(400, str(error))
            return
        if self.handle_pre_json_rpc_config(config, payload.get("method")):
            return

        status, extra_headers, response = handle_json_rpc(
            payload, config=config, headers=dict(self.headers)
        )
        record_exchange("POST", path, self.headers, payload, status, response)
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

    def send_405(self) -> None:
        # 2026-07-28 stateless transport: only POST /mcp is allowed.
        body = b"Method Not Allowed"
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_cors_headers()
        self.send_header("Allow", "POST, OPTIONS")
        self.send_header("Content-Type", "text/plain;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
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
        query = parse_qs(urlparse(self.path).query)
        raw_config = query.get(CONFIG_QUERY_PARAM, [""])[0]
        if raw_config:
            return decode_config_token(raw_config)
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
