"""Minimal Streamable HTTP MCP server for PlayMCP."""

from __future__ import annotations

import json
import os
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import isfinite
from typing import Any

SERVER_NAME = "unitExpert"
SERVER_VERSION = "1.0.0"
SERVICE_NAME = "Unit Expert(단위전문가)"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL_VERSION = "2025-11-25"
TEST_SCENARIO_HEADER = "X-MCP-Test-Scenario"
DEFAULT_DELAY_SECONDS = 5.0

SESSIONS: set[str] = set()

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
        case "valid-tools":
            return {"tools": [valid_tool("convert_length")]}
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
        case "mcp-identifier-name":
            return {"tools": [valid_tool("kakaomap_search")]}
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
        capabilities: dict[str, Any] = {}
        if scenario != "no-tools-capability":
            capabilities["tools"] = {"listChanged": True}
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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "UnitExpertMCP/1.0"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self.send_json(200, {"ok": True, "service": SERVER_NAME})
            return

        if self.path != "/mcp":
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
        if self.path != "/mcp":
            self.send_text(404, "Not found")
            return

        scenario = self.request_scenario()
        if self.handle_pre_json_rpc_scenario(scenario):
            return

        accept = self.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            self.send_text(400, "Invalid Accept headers. Expected TEXT_EVENT_STREAM and APPLICATION_JSON")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
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
        return normalize_scenario(self.headers.get(TEST_SCENARIO_HEADER))

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
