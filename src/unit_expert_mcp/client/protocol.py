"""Protocol rules the client follows (MCP 2026-07-28 and the 2025-xx handshake).

Every rule here cites the spec page it comes from; see docs/SPEC_2026-07-28_NOTES.md.
Base URL: https://modelcontextprotocol.io/specification/2026-07-28/
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

MODERN_VERSION = "2026-07-28"
LEGACY_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_LEGACY_VERSION = "2025-11-25"

# basic/index#_meta — per-request envelope keys.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"

# basic/transports/streamable-http — standard request headers.
H_VERSION = "MCP-Protocol-Version"
H_METHOD = "Mcp-Method"
H_NAME = "Mcp-Name"
H_PARAM_PREFIX = "Mcp-Param-"
H_SESSION = "Mcp-Session-Id"
ACCEPT_BOTH = "application/json, text/event-stream"

# basic/index#error-codes
ERR_HEADER_MISMATCH = -32020
ERR_MISSING_CAPABILITY = -32021
ERR_UNSUPPORTED_VERSION = -32022
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
MODERN_ERROR_CODES = (ERR_HEADER_MISMATCH, ERR_MISSING_CAPABILITY, ERR_UNSUPPORTED_VERSION)

# Methods whose params.name / params.uri is mirrored into Mcp-Name.
NAME_BEARING_METHODS = {"tools/call": "name", "resources/read": "uri", "prompts/get": "name"}

# basic/index "ResultType": the values of the core protocol.
KNOWN_RESULT_TYPES = ("complete", "input_required")
RESULT_INVALID = "invalid"

CLIENT_INFO = {"name": "unit-expert-client", "version": "0.1.0"}

SENTINEL_PREFIX = "=?base64?"
SENTINEL_SUFFIX = "?="
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")  # RFC 9110 token (1*tchar)
MAX_SAFE_INTEGER = 2**53 - 1
HEADER_PARAM_TYPES = {"string", "integer", "boolean"}
# Keywords that break the "reachable through `properties` only" rule for x-mcp-header.
_NON_PROPERTY_CONTAINERS = (
    "items",
    "prefixItems",
    "additionalProperties",
    "patternProperties",
    "oneOf",
    "anyOf",
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "$defs",
    "definitions",
    "dependentSchemas",
    "contains",
)


# -- header value encoding ------------------------------------------------------
def is_sentinel(value: str) -> bool:
    return (
        len(value) >= len(SENTINEL_PREFIX) + len(SENTINEL_SUFFIX)
        and value.startswith(SENTINEL_PREFIX)
        and value.endswith(SENTINEL_SUFFIX)
    )


def needs_base64(value: str) -> bool:
    """streamable-http "Value Encoding": non-ASCII, control characters, or
    leading/trailing whitespace MUST be base64-wrapped; so MUST a plain value
    that happens to look like the sentinel itself."""
    if value == "":
        return False
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        return True
    if value[0] in " \t" or value[-1] in " \t":
        return True
    return is_sentinel(value)


def encode_header_value(value: str) -> str:
    """``"Hello, 世界"`` -> ``=?base64?SGVsbG8sIOS4lueVjA==?=`` (standard alphabet, padded)."""
    if not needs_base64(value):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"{SENTINEL_PREFIX}{encoded}{SENTINEL_SUFFIX}"


def decode_header_value(value: str) -> str | None:
    """Inverse of encode_header_value; None when the sentinel payload is not valid."""
    if not is_sentinel(value):
        return value
    payload = value[len(SENTINEL_PREFIX) : -len(SENTINEL_SUFFIX)]
    try:
        return base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return None


def param_header_value(value: Any) -> str | None:
    """Convert a tool argument to its Mcp-Param-* header text.

    string as-is, integer as decimal, boolean as lowercase true/false; null or
    absent arguments produce no header (client MUST omit it).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return encode_header_value(value)
    return None


# -- x-mcp-header schema handling ------------------------------------------------
@dataclass
class HeaderParam:
    path: tuple[str, ...]  # property path from the schema root
    header: str  # the x-mcp-header value, e.g. "Region"
    type: str

    @property
    def header_name(self) -> str:
        return f"{H_PARAM_PREFIX}{self.header}"

    def lookup(self, arguments: Any) -> Any:
        current = arguments
        for key in self.path:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current


def _schema_type(schema: dict[str, Any]) -> str | None:
    raw = schema.get("type")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        concrete = [item for item in raw if item != "null"]
        if len(concrete) == 1 and isinstance(concrete[0], str):
            return concrete[0]
    return None


def collect_header_params(input_schema: Any) -> tuple[list[HeaderParam], list[str]]:
    """Find x-mcp-header annotations and the reasons a tool violates the rules.

    streamable-http "Custom headers from tool parameters": the value MUST be a
    non-empty HTTP token, unique case-insensitively within the schema, placed
    only on integer/string/boolean properties reachable from the root through
    ``properties`` alone.
    """
    params: list[HeaderParam] = []
    problems: list[str] = []
    if not isinstance(input_schema, dict):
        return params, problems

    def walk(schema: Any, path: tuple[str, ...], reachable: bool) -> None:
        if isinstance(schema, list):
            for item in schema:
                walk(item, path, reachable)
            return
        if not isinstance(schema, dict):
            return
        if "x-mcp-header" in schema and path:
            header = schema["x-mcp-header"]
            where = ".".join(path)
            if not reachable:
                problems.append(f"{where}: x-mcp-header is not reachable through 'properties' only")
            elif not isinstance(header, str) or not header or not _TOKEN_RE.match(header):
                problems.append(f"{where}: x-mcp-header {header!r} is not a non-empty HTTP token")
            elif _schema_type(schema) not in HEADER_PARAM_TYPES:
                problems.append(
                    f"{where}: x-mcp-header on type {schema.get('type')!r} (string/integer/boolean only)"
                )
            else:
                params.append(
                    HeaderParam(path=path, header=header, type=_schema_type(schema) or "string")
                )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                walk(child, (*path, str(name)), reachable)
        for keyword in _NON_PROPERTY_CONTAINERS:
            if keyword in schema:
                child = schema[keyword]
                children = (
                    child.values()
                    if keyword in {"patternProperties", "$defs", "definitions", "dependentSchemas"}
                    and isinstance(child, dict)
                    else [child]
                )
                for item in children:
                    walk(item, (*path, f"<{keyword}>"), False)

    walk(input_schema, (), True)
    seen: dict[str, str] = {}
    for param in params:
        lowered = param.header.lower()
        if lowered in seen:
            problems.append(
                f"x-mcp-header {param.header!r} duplicates {seen[lowered]!r} (case-insensitive)"
            )
        seen.setdefault(lowered, param.header)
    return params, problems


def split_usable_tools(tools: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop tools whose x-mcp-header annotations are invalid (client MUST exclude
    them from tools/list results and SHOULD log a warning)."""
    usable: list[dict[str, Any]] = []
    warnings: list[str] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        _, problems = collect_header_params(tool.get("inputSchema"))
        if problems:
            warnings.append(f"tool {tool.get('name')!r} excluded: {'; '.join(problems)}")
        else:
            usable.append(tool)
    return usable, warnings


def param_headers_for(tool: dict[str, Any] | None, arguments: Any) -> list[tuple[str, str]]:
    """Mcp-Param-* headers for one tools/call, derived from the tool's inputSchema."""
    if not tool:
        return []
    params, problems = collect_header_params(tool.get("inputSchema"))
    if problems:
        return []
    headers = []
    for param in params:
        text = param_header_value(param.lookup(arguments))
        if text is not None:
            headers.append((param.header_name, text))
    return headers


# -- request construction ---------------------------------------------------------
@dataclass
class Tweaks:
    """Deliberate deviations from a compliant modern request (CLI violation flags)."""

    omit_meta_version: bool = False
    omit_capabilities: bool = False
    omit_meta: bool = False
    meta_version: str | None = None
    omit_version_header: bool = False
    version_header: str | None = None
    omit_method_header: bool = False
    method_header: str | None = None
    omit_name_header: bool = False
    name_header: str | None = None
    omit_param_headers: bool = False
    param_headers: list[tuple[str, "str | bytes"]] = field(default_factory=list)
    session_id: str | None = None
    accept: str | None = None
    omit_accept: bool = False
    raw_body: bytes | None = None
    extra_headers: list[tuple[str, "str | bytes"]] = field(default_factory=list)


def modern_meta(
    version: str, capabilities: dict[str, Any], tweaks: Tweaks | None = None
) -> dict[str, Any] | None:
    tweaks = tweaks or Tweaks()
    if tweaks.omit_meta:
        return None
    meta: dict[str, Any] = {}
    if not tweaks.omit_meta_version:
        meta[META_PROTOCOL_VERSION] = tweaks.meta_version or version
    meta[META_CLIENT_INFO] = dict(CLIENT_INFO)  # optional, client SHOULD send it
    if not tweaks.omit_capabilities:
        meta[META_CLIENT_CAPABILITIES] = capabilities
    return meta


def modern_headers(
    method: str,
    params: dict[str, Any],
    version: str,
    tool: dict[str, Any] | None = None,
    tweaks: Tweaks | None = None,
) -> list[tuple[str, "str | bytes"]]:
    """Protocol headers of a 2026-07-28 POST, in the order of the spec examples."""
    tweaks = tweaks or Tweaks()
    headers: list[tuple[str, str | bytes]] = [("Content-Type", "application/json")]
    if not tweaks.omit_accept:
        headers.append(("Accept", tweaks.accept or ACCEPT_BOTH))
    if not tweaks.omit_version_header:
        headers.append((H_VERSION, tweaks.version_header or version))
    if not tweaks.omit_method_header:
        headers.append((H_METHOD, tweaks.method_header or method))
    name_key = NAME_BEARING_METHODS.get(method)
    if tweaks.name_header is not None:
        headers.append((H_NAME, tweaks.name_header))
    elif name_key and not tweaks.omit_name_header and isinstance(params.get(name_key), str):
        headers.append((H_NAME, encode_header_value(params[name_key])))
    overridden = {name.lower() for name, _ in tweaks.param_headers}
    if method == "tools/call" and not tweaks.omit_param_headers:
        for name, value in param_headers_for(tool, params.get("arguments")):
            if name.lower() not in overridden:
                headers.append((name, value))
    headers.extend(tweaks.param_headers)
    if tweaks.session_id is not None:
        headers.append((H_SESSION, tweaks.session_id))
    headers.extend(tweaks.extra_headers)
    return headers


# -- result handling ------------------------------------------------------------------
def judge_result(method: str | None, result: Any) -> tuple[str, str | None]:
    """How a client has to read a modern result: (effective resultType, problem).

    basic/index "ResultType": an absent ``resultType`` MUST be treated as
    ``"complete"``; a value the client does not recognise MUST be considered
    invalid. basic/patterns/mrtr: ``input_required`` is only allowed on
    tools/call, resources/read and prompts/get and needs ``inputRequests`` or
    ``requestState``. An invalid result is reported as ``RESULT_INVALID`` and
    must not be used.
    """
    if not isinstance(result, dict):
        return RESULT_INVALID, "result가 객체가 아님"
    if "resultType" not in result:
        return "complete", None
    value = result["resultType"]
    if not isinstance(value, str) or value not in KNOWN_RESULT_TYPES:
        return RESULT_INVALID, f"모르는 resultType {value!r}"
    if value == "input_required":
        if method not in NAME_BEARING_METHODS:
            return RESULT_INVALID, f"{method}에는 input_required를 쓸 수 없음"
        if not isinstance(result.get("inputRequests"), dict) and "requestState" not in result:
            return RESULT_INVALID, "input_required에 inputRequests와 requestState가 모두 없음"
    return value, None


def effective_ttl_ms(result: Any) -> int:
    """server/utilities/caching: an absent or negative ``ttlMs`` is treated as 0."""
    ttl = result.get("ttlMs") if isinstance(result, dict) else None
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        return 0
    return ttl


# -- WWW-Authenticate ---------------------------------------------------------------
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z0-9_\-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]+))')


def parse_www_authenticate(value: str | None) -> dict[str, Any]:
    """Parse ``Bearer resource_metadata="...", scope="..."`` (RFC 6750 / RFC 9728)."""
    if not value:
        return {"scheme": None, "params": {}}
    scheme, _, rest = value.strip().partition(" ")
    if "=" in scheme:  # no scheme token, parameters only
        scheme, rest = "", value
    params = {
        match.group(1).lower(): (
            match.group(2).replace('\\"', '"') if match.group(2) is not None else match.group(3)
        )
        for match in _CHALLENGE_PARAM_RE.finditer(rest)
    }
    return {"scheme": scheme or None, "params": params}


# -- sample data ----------------------------------------------------------------------
def sample_value(schema: Any, hint: str = "") -> Any:
    """A plausible value for a JSON Schema property (used to call unknown test tools)."""
    if not isinstance(schema, dict):
        return "test"
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    kind = _schema_type(schema)
    if kind == "integer":
        return 42
    if kind == "number":
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return sample_arguments(schema)
    return hint or "test"


def sample_arguments(input_schema: Any, *, include_optional: bool = False) -> dict[str, Any]:
    if not isinstance(input_schema, dict):
        return {}
    properties = (
        input_schema.get("properties") if isinstance(input_schema.get("properties"), dict) else {}
    )
    required = (
        input_schema.get("required") if isinstance(input_schema.get("required"), list) else []
    )
    return {
        name: sample_value(schema)
        for name, schema in properties.items()
        if include_optional or name in required
    }


def set_path(arguments: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    current = arguments
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


def input_response_for(request: Any) -> Any:
    """Canned answer to one entry of ``inputRequests`` (basic/patterns/mrtr)."""
    method = request.get("method") if isinstance(request, dict) else None
    params = (
        request.get("params")
        if isinstance(request, dict) and isinstance(request.get("params"), dict)
        else {}
    )
    if method == "elicitation/create":
        schema = params.get("requestedSchema")
        return {"action": "accept", "content": sample_arguments(schema, include_optional=True)}
    if method == "sampling/createMessage":
        return {
            "role": "assistant",
            "content": {"type": "text", "text": "ok"},
            "model": CLIENT_INFO["name"],
            "stopReason": "endTurn",
        }
    if method == "roots/list":
        return {"roots": []}
    return {}
