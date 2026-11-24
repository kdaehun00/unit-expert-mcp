"""Tests for the MCP test client (src/unit_expert_mcp/client).

Client behaviour is checked against ``SpecServer``, a small spec-following fake
defined here, so these tests do not depend on how far server.py has caught up
with the 2026-07-28 spec. A few smoke tests run against the real Handler and use
only long-standing behaviour (legacy handshake, serverEra, the mock OAuth AS).
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from unit_expert_mcp.client import protocol as p
from unit_expert_mcp.client import report
from unit_expert_mcp.client.__main__ import main
from unit_expert_mcp.client.clients import LegacyClient, ModernClient, Target, probe
from unit_expert_mcp.client.runner import FAIL, PASS, SKIP, WARN, Options, Runner, exit_code
from unit_expert_mcp.client.scenarios import SCENARIOS
from unit_expert_mcp.client.transport import (
    Exchange,
    encode_cfg_token,
    mask_form,
    mask_header_value,
    parse_sse,
    send,
)
from unit_expert_mcp.server import Handler

SERVER_INFO = {p.META_SERVER_INFO: {"name": "spec-fake", "version": "1"}}
LEGACY_SUPPORTED = ("2025-03-26", "2025-06-18", "2025-11-25")
TOOLS = [
    {
        "name": "convert_length",
        "description": "Converts length.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "value": {"type": "number"},
                "from_unit": {"type": "string"},
                "to_unit": {"type": "string"},
            },
            "required": ["value", "from_unit", "to_unit"],
        },
    },
    {
        "name": "echo_header_params",
        "description": "Echoes arguments mirrored into headers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"},
                "count": {"type": "integer", "x-mcp-header": "Count"},
                "verbose": {"type": "boolean", "x-mcp-header": "Verbose"},
            },
        },
    },
    {
        "name": "confirm_action",
        "description": "Asks for confirmation through input_required.",
        "inputSchema": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "convert_interactive",
        "description": "Asks for the target unit, then for the decimals.",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "number"}, "from_unit": {"type": "string"}},
            "required": ["value", "from_unit"],
        },
    },
]
# Round -> (inputRequests key, requestedSchema property, state the retry must carry).
INTERACTIVE_ROUNDS = {
    None: ("target_unit", "to_unit", {"type": "string", "enum": ["m", "cm"]}, "unit-state"),
    "unit-state": (
        "precision",
        "decimals",
        {"type": "integer", "minimum": 0, "maximum": 10},
        "precision-state",
    ),
}


class Recorded:
    def __init__(self, method: str, path: str, headers: list[tuple[str, str]], body: bytes) -> None:
        self.method, self.path, self.headers, self.body = method, path, headers, body

    def header(self, name: str) -> str | None:
        return next((value for key, value in self.headers if key.lower() == name.lower()), None)

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None


def rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def as_json(status: int, payload: Any, headers: list[tuple[str, str]] | None = None):
    return (
        status,
        [("Content-Type", "application/json"), *(headers or [])],
        json.dumps(payload).encode(),
    )


class SpecServer:
    """A responder that follows the checklist in docs/SPEC_2026-07-28_NOTES.md."""

    def __init__(
        self,
        era: str = "dual",
        *,
        sse: bool = False,
        paged: bool = False,
        auth: bool = False,
        broken: set[str] | None = None,
    ) -> None:
        self.era, self.sse, self.paged, self.auth = era, sse, paged, auth
        self.broken = broken or set()
        self.sessions: set[str] = set()

    def __call__(self, request: Recorded):
        if request.path.startswith("/.well-known/oauth-protected-resource"):
            host = request.header("Host")
            return as_json(
                200, {"resource": f"http://{host}/mcp", "authorization_servers": [f"http://{host}"]}
            )
        if self.auth:
            token = (request.header("Authorization") or "").removeprefix("Bearer ")
            metadata = f'resource_metadata="http://{request.header("Host")}/.well-known/oauth-protected-resource"'
            if token not in ("good-token", "weak-token"):
                return 401, [("WWW-Authenticate", f'Bearer {metadata}, scope="mcp"')], b""
            if token == "weak-token":
                return (
                    403,
                    [
                        (
                            "WWW-Authenticate",
                            f'Bearer error="insufficient_scope", scope="mcp", {metadata}',
                        )
                    ],
                    b"",
                )
        if request.method in ("GET", "DELETE"):
            session = request.header("Mcp-Session-Id")
            if self.era == "modern" or request.method == "GET":
                return 405, [("Allow", "POST")], b""
            if session in self.sessions:
                self.sessions.discard(session)
                return 200, [], b""
            return 404, [], b""
        payload = request.json()
        meta = (payload.get("params") or {}).get("_meta") or {}
        is_modern = request.header("Mcp-Method") is not None or p.META_PROTOCOL_VERSION in meta
        if is_modern and self.era != "legacy":
            return self.modern(request, payload, meta)
        if self.era == "modern":
            data = {"supported": [p.MODERN_VERSION], "requested": "legacy"}
            return as_json(
                400,
                rpc_error(payload.get("id"), -32022, f"Only {p.MODERN_VERSION} is supported", data),
            )
        return self.legacy(request, payload)

    # -- 2026-07-28 -----------------------------------------------------------
    def modern(self, request: Recorded, payload: dict, meta: dict):
        rid, method, params = payload.get("id"), payload.get("method"), payload.get("params") or {}

        def reject(code: int, message: str, status: int = 400, data: Any = None):
            return as_json(status, rpc_error(rid, code, message, data))

        version_header, method_header = (
            request.header("MCP-Protocol-Version"),
            request.header("Mcp-Method"),
        )
        if version_header is None or method_header is None:
            return reject(-32020, "Missing required header")
        if method in p.NAME_BEARING_METHODS and request.header("Mcp-Name") is None:
            return reject(-32020, "Missing Mcp-Name")
        if p.META_PROTOCOL_VERSION not in meta or not isinstance(
            meta.get(p.META_CLIENT_CAPABILITIES), dict
        ):
            return reject(-32602, "Missing required _meta")
        if version_header != meta[p.META_PROTOCOL_VERSION]:
            return reject(-32020, "Version header mismatch")
        if version_header != p.MODERN_VERSION:
            return reject(
                -32022,
                "Unsupported",
                data={"supported": [p.MODERN_VERSION], "requested": version_header},
            )
        if method_header != method:
            return reject(-32020, "Mcp-Method mismatch")
        if method in p.NAME_BEARING_METHODS and p.decode_header_value(
            request.header("Mcp-Name")
        ) != params.get("name"):
            return reject(-32020, "Mcp-Name mismatch")

        def complete(result: dict, cacheable: bool = False):
            body = {"resultType": "complete", **result}
            if "no-server-info" not in self.broken:
                body["_meta"] = dict(SERVER_INFO)
            if cacheable and "no-ttl" not in self.broken:
                body.update({"ttlMs": 0, "cacheScope": "private"})
            message = {"jsonrpc": "2.0", "id": rid, "result": body}
            if self.sse and method == "tools/call":
                progress = {
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {"progress": 1},
                }
                stream = "".join(
                    f"event: message\ndata: {json.dumps(item)}\n\n" for item in (progress, message)
                )
                return 200, [("Content-Type", "text/event-stream")], f": hello\n\n{stream}".encode()
            return as_json(200, message)

        if method == "server/discover":
            return complete(
                {"supportedVersions": [p.MODERN_VERSION], "capabilities": {"tools": {}}},
                cacheable=True,
            )
        if method == "tools/list":
            cursor = params.get("cursor")
            if cursor not in (None, "p2") or (cursor == "p2" and not self.paged):
                return reject(-32602, "Invalid cursor")
            if not self.paged:
                return complete({"tools": TOOLS}, cacheable=True)
            return complete(
                {"tools": TOOLS[:2], "nextCursor": "p2"}
                if cursor is None
                else {"tools": TOOLS[2:]},
                True,
            )
        if method == "tools/call":
            return self.call_tool(request, params, complete, reject)
        return reject(-32601, f"Method not found: {method}", status=404)

    def call_tool(self, request: Recorded, params: dict, complete, reject):
        name, arguments = params.get("name"), params.get("arguments") or {}
        tool = next((tool for tool in TOOLS if tool["name"] == name), None)
        if tool is None:
            if "unknown-tool-iserror" in self.broken:
                return complete(
                    {"content": [{"type": "text", "text": "Unknown tool"}], "isError": True}
                )
            return reject(-32602, f"Unknown tool: {name}")
        for key, schema in tool["inputSchema"]["properties"].items():
            if "x-mcp-header" not in schema:
                continue
            raw = request.header(f"Mcp-Param-{schema['x-mcp-header']}")
            value = arguments.get(key)
            if value is None:
                continue
            if raw is None or any(ord(char) > 0x7E for char in raw):
                return reject(-32020, f"Bad Mcp-Param for {key}")
            if p.decode_header_value(raw) != p.decode_header_value(p.param_header_value(value)):
                return reject(-32020, f"Mcp-Param mismatch for {key}")
        if name == "convert_length":
            if arguments.get("from_unit") not in ("km", "m"):
                return complete(
                    {"content": [{"type": "text", "text": "unsupported unit"}], "isError": True}
                )
            return complete(
                {"content": [{"type": "text", "text": "1 km = 1000 m"}], "isError": False}
            )
        if name == "confirm_action" and "inputResponses" not in params:
            schema = {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            }
            ask = {
                "method": "elicitation/create",
                "params": {"message": "Sure?", "requestedSchema": schema},
            }
            body = {
                "resultType": "input_required",
                "inputRequests": {"confirm": ask},
                "requestState": "state-1",
            }
            return as_json(200, {"jsonrpc": "2.0", "id": request.json()["id"], "result": body})
        if name == "convert_interactive":
            state = params.get("requestState")
            if state == "precision-state":
                answer = (params.get("inputResponses") or {}).get("precision") or {}
                decimals = (answer.get("content") or {}).get("decimals")
                if list(params["inputResponses"]) != ["precision"] or not 0 <= decimals <= 10:
                    return reject(-32602, "Bad inputResponses")
                text = f"3 km = {3000:.{decimals}f} m"
                return complete({"content": [{"type": "text", "text": text}], "isError": False})
            if state not in INTERACTIVE_ROUNDS:
                return reject(-32602, "Bad requestState")
            if state == "unit-state":
                answer = (params.get("inputResponses") or {}).get("target_unit") or {}
                if (answer.get("content") or {}).get("to_unit") != "m":
                    return reject(-32602, "Bad inputResponses")
            key, field, schema, next_state = INTERACTIVE_ROUNDS[state]
            ask = {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {field: schema},
                        "required": [field],
                    },
                },
            }
            body = {
                "resultType": "input_required",
                "inputRequests": {key: ask},
                "requestState": next_state,
            }
            return as_json(200, {"jsonrpc": "2.0", "id": request.json()["id"], "result": body})
        if name == "confirm_action":
            answer = params["inputResponses"].get("confirm") or {}
            if params.get("requestState") != "state-1" or answer.get("action") != "accept":
                return reject(-32602, "Bad inputResponses")
        return complete(
            {"content": [{"type": "text", "text": json.dumps(arguments)}], "isError": False}
        )

    # -- 2025-xx ----------------------------------------------------------------
    def legacy(self, request: Recorded, payload: dict):
        rid, method, params = payload.get("id"), payload.get("method"), payload.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            session = uuid.uuid4().hex
            self.sessions.add(session)
            result = {
                "protocolVersion": requested
                if requested in LEGACY_SUPPORTED
                else LEGACY_SUPPORTED[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "spec-fake", "version": "1"},
            }
            return as_json(
                200, {"jsonrpc": "2.0", "id": rid, "result": result}, [("Mcp-Session-Id", session)]
            )
        if "no-session-check" not in self.broken:
            session = request.header("Mcp-Session-Id")
            if session is None:
                return as_json(400, rpc_error(rid, -32600, "Mcp-Session-Id required"))
            if session not in self.sessions:
                return as_json(404, rpc_error(rid, -32600, "Session not found"))
        if "id" not in payload:
            return 202, [], b""
        if method == "tools/list":
            return as_json(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        if method == "tools/call":
            ok = {"content": [{"type": "text", "text": "1 km = 1000 m"}], "isError": False}
            return as_json(200, {"jsonrpc": "2.0", "id": rid, "result": ok})
        return as_json(200, rpc_error(rid, -32601, f"Method not found: {method}"))


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = Recorded(
            self.command,
            self.path,
            list(self.headers.items()),
            self.rfile.read(length) if length else b"",
        )
        self.server.requests.append(request)  # type: ignore[attr-defined]
        status, headers, body = self.server.responder(request)  # type: ignore[attr-defined]
        self.send_response(status)
        streaming = ("Content-Type", "text/event-stream") in headers
        for name, value in headers:
            self.send_header(name, value)
        if streaming:
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = _handle

    def log_message(self, format: str, *args: Any) -> None:
        pass


class QuietHandler(Handler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


class ServerCase(unittest.TestCase):
    """Starts HTTP servers on ephemeral ports and stops them after each test."""

    def serve(self, handler: type, responder: Any = None) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.requests, server.responder = [], responder  # type: ignore[attr-defined]
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def fake(self, responder: Any = None, **kwargs: Any) -> tuple[ThreadingHTTPServer, Target]:
        server = self.serve(FakeHandler, responder or SpecServer(**kwargs))
        return server, Target(url=f"http://127.0.0.1:{server.server_address[1]}/mcp", timeout=5)

    def real(self, cfg: dict | None = None) -> Target:
        server = self.serve(QuietHandler)
        return Target(url=f"http://127.0.0.1:{server.server_address[1]}/mcp", timeout=5, cfg=cfg)

    def cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(list(argv))
        return code, out.getvalue()


# -- protocol rules (no network) ---------------------------------------------------
class HeaderEncodingTest(unittest.TestCase):
    def test_spec_example_is_base64_wrapped(self) -> None:
        self.assertEqual(p.encode_header_value("Hello, 世界"), "=?base64?SGVsbG8sIOS4lueVjA==?=")
        self.assertEqual(p.decode_header_value("=?base64?SGVsbG8sIOS4lueVjA==?="), "Hello, 世界")

    def test_plain_ascii_is_sent_as_is(self) -> None:
        for value in ("us-west1", "a b", "convert_length", ""):
            self.assertEqual(p.encode_header_value(value), value)

    def test_values_that_must_be_encoded(self) -> None:
        for value in (" lead", "trail ", "tab\there", "line\nbreak", "=?base64?abc?=", "café"):
            encoded = p.encode_header_value(value)
            self.assertTrue(encoded.startswith("=?base64?") and encoded.endswith("?="), value)
            self.assertEqual(p.decode_header_value(encoded), value)

    def test_invalid_sentinel_payload_decodes_to_none(self) -> None:
        self.assertIsNone(p.decode_header_value("=?base64?***?="))

    def test_param_value_conversion(self) -> None:
        self.assertEqual(p.param_header_value("us-west1"), "us-west1")
        self.assertEqual(p.param_header_value(42), "42")
        self.assertEqual(p.param_header_value(42.0), "42")
        self.assertEqual(p.param_header_value(True), "true")
        self.assertEqual(p.param_header_value(False), "false")
        self.assertIsNone(p.param_header_value(None))


class HeaderSchemaTest(unittest.TestCase):
    def test_collects_root_and_nested_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"},
                "opts": {
                    "type": "object",
                    "properties": {"limit": {"type": ["integer", "null"], "x-mcp-header": "Limit"}},
                },
            },
        }
        params, problems = p.collect_header_params(schema)
        self.assertEqual(problems, [])
        self.assertEqual(
            [(param.path, param.header_name, param.type) for param in params],
            [
                (("region",), "Mcp-Param-Region", "string"),
                (("opts", "limit"), "Mcp-Param-Limit", "integer"),
            ],
        )
        self.assertEqual(params[1].lookup({"opts": {"limit": 5}}), 5)
        self.assertIsNone(params[1].lookup({}))

    def test_rule_violations_exclude_the_tool(self) -> None:
        bad_schemas = {
            "number type": {"properties": {"a": {"type": "number", "x-mcp-header": "A"}}},
            "empty name": {"properties": {"a": {"type": "string", "x-mcp-header": ""}}},
            "not a token": {"properties": {"a": {"type": "string", "x-mcp-header": "Bad Name"}}},
            "duplicate": {
                "properties": {
                    "a": {"type": "string", "x-mcp-header": "Dup"},
                    "b": {"type": "string", "x-mcp-header": "dup"},
                }
            },
            "under items": {
                "properties": {
                    "a": {"type": "array", "items": {"type": "string", "x-mcp-header": "A"}}
                }
            },
            "under anyOf": {
                "properties": {"a": {"anyOf": [{"type": "string", "x-mcp-header": "A"}]}}
            },
        }
        good = {
            "name": "good",
            "inputSchema": {"properties": {"a": {"type": "boolean", "x-mcp-header": "A"}}},
        }
        tools = [good] + [
            {"name": name, "inputSchema": schema} for name, schema in bad_schemas.items()
        ]
        usable, warnings = p.split_usable_tools(tools)
        self.assertEqual([tool["name"] for tool in usable], ["good"])
        self.assertEqual(len(warnings), len(bad_schemas))

    def test_param_headers_follow_presence_rules(self) -> None:
        tool = TOOLS[1]
        self.assertEqual(
            p.param_headers_for(tool, {"region": "서울", "count": 3, "verbose": False}),
            [
                ("Mcp-Param-Region", "=?base64?7ISc7Jq4?="),
                ("Mcp-Param-Count", "3"),
                ("Mcp-Param-Verbose", "false"),
            ],
        )
        self.assertEqual(p.param_headers_for(tool, {"region": None}), [])


class MiscParsingTest(unittest.TestCase):
    def test_www_authenticate(self) -> None:
        parsed = p.parse_www_authenticate(
            'Bearer error="insufficient_scope", scope="mcp read, write", resource_metadata="https://h/.well-known/x"'
        )
        self.assertEqual(parsed["scheme"], "Bearer")
        self.assertEqual(
            parsed["params"],
            {
                "error": "insufficient_scope",
                "scope": "mcp read, write",
                "resource_metadata": "https://h/.well-known/x",
            },
        )
        self.assertEqual(p.parse_www_authenticate(None), {"scheme": None, "params": {}})

    def test_sse_parsing(self) -> None:
        events = parse_sse(
            ': ping\n\nevent: message\ndata: {"a":\ndata: 1}\nid: 7\n\ndata: plain\r\n\r\n'
        )
        self.assertEqual(
            [(event.event, event.json, event.id) for event in events],
            [("message", {"a": 1}, "7"), (None, None, "7")],
        )
        self.assertEqual(events[1].data, "plain")

    def test_cfg_token_is_unpadded_base64url_json(self) -> None:
        token = encode_cfg_token({"serverEra": "legacy"})
        self.assertNotIn("=", token)
        self.assertEqual(
            json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))),
            {"serverEra": "legacy"},
        )

    def test_credentials_are_masked(self) -> None:
        self.assertEqual(mask_header_value("Authorization", "Bearer abc.def"), "Bearer ***")
        self.assertEqual(mask_header_value("X-Inhouse-Service-Token", "abc"), "***")
        self.assertEqual(
            mask_header_value("WWW-Authenticate", 'Bearer scope="mcp"'), 'Bearer scope="mcp"'
        )
        self.assertEqual(mask_header_value("Mcp-Session-Id", "s1"), "s1")

    def test_form_body_credentials_are_masked(self) -> None:
        # D6: OAuth token/authorize requests are form-encoded, not JSON.
        self.assertEqual(
            mask_form(
                "grant_type=authorization_code&code=SECRET1&redirect_uri=http%3A%2F%2Fl%2Fcb"
                "&client_id=c1&code_verifier=SECRET2&client_secret=SECRET3"
                "&refresh_token=SECRET4&access_token=SECRET5&state=keep&code_challenge=keep2"
            ),
            "grant_type=authorization_code&code=***&redirect_uri=http%3A%2F%2Fl%2Fcb"
            "&client_id=c1&code_verifier=***&client_secret=***"
            "&refresh_token=***&access_token=***&state=keep&code_challenge=keep2",
        )
        self.assertEqual(mask_form("code=&flag&refresh%5Ftoken=SECRET"), "code=&flag&refresh%5Ftoken=***")

    def form_exchange(self, body: bytes, content_type: str) -> Exchange:
        return Exchange(
            method="POST",
            url="http://127.0.0.1:1/token",
            request_headers=[("Content-Type", content_type)],
            request_body=body,
            status=200,
            reason="OK",
        )

    def test_exchange_output_masks_a_form_request_body(self) -> None:
        body = b"grant_type=refresh_token&refresh_token=SECRET-R&client_id=c1&code=SECRET-C"
        for content_type in (
            "application/x-www-form-urlencoded",
            "Application/X-WWW-Form-Urlencoded; charset=UTF-8",
        ):
            with self.subTest(content_type=content_type):
                exchange = self.form_exchange(body, content_type)
                expected = "grant_type=refresh_token&refresh_token=***&client_id=c1&code=***"
                self.assertEqual(exchange.detail()["requestBody"], expected)
                for text in (
                    report.format_exchange(exchange, True),
                    report.format_exchange(exchange, False),
                    report.to_json(exchange.detail()),
                ):
                    self.assertNotIn("SECRET", text)
                    self.assertIn("client_id=c1", text)

    def test_json_request_body_keeps_the_json_rpc_code_field(self) -> None:
        body = json.dumps({"error": {"code": -32600}, "refresh_token": "SECRET"}).encode()
        detail = self.form_exchange(body, "application/json").detail()
        self.assertEqual(detail["requestBody"], {"error": {"code": -32600}, "refresh_token": "***"})

    def test_redirect_location_hides_the_authorization_code(self) -> None:
        self.assertEqual(
            mask_header_value("Location", "http://localhost/callback?code=SECRET&state=s1"),
            "http://localhost/callback?code=***&state=s1",
        )
        self.assertEqual(mask_header_value("Location", "/next"), "/next")

    def test_control_characters_are_escaped(self) -> None:
        escape = report.escape_control
        self.assertEqual(escape("plain 한글 ✓\ttab"), "plain 한글 ✓\ttab")
        self.assertEqual(escape("\x1b[31mred\x1b[0m"), "\\x1b[31mred\\x1b[0m")
        self.assertEqual(escape("a\r\nb\x00\x07\x7f\x9b\u2028"), "a\\r\\nb\\x00\\x07\\x7f\\x9b\\u2028")
        self.assertEqual(escape("a\nb\rc", keep_newlines=True), "a\nb\\rc")


# -- request construction and response parsing ----------------------------------------
class ModernClientTest(ServerCase):
    def test_request_matches_the_spec_example(self) -> None:
        server, target = self.fake()
        exchange = ModernClient(target).list_tools()
        seen = server.requests[-1]
        self.assertEqual(exchange.status, 200)
        self.assertEqual(seen.header("Content-Type"), "application/json")
        self.assertEqual(seen.header("Accept"), "application/json, text/event-stream")
        self.assertEqual(seen.header("MCP-Protocol-Version"), "2026-07-28")
        self.assertEqual(seen.header("Mcp-Method"), "tools/list")
        self.assertIsNone(seen.header("Mcp-Name"))
        self.assertIsNone(seen.header("Mcp-Session-Id"))
        body = seen.json()
        self.assertEqual((body["jsonrpc"], body["method"]), ("2.0", "tools/list"))
        self.assertEqual(
            body["params"]["_meta"],
            {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "unit-expert-client",
                    "version": "0.1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        )

    def test_tools_call_attaches_name_and_param_headers(self) -> None:
        server, target = self.fake()
        client = ModernClient(target)
        client.load_tools()
        final = client.call_tool(
            "echo_header_params", {"region": "Hello, 世界", "count": 42, "verbose": True}
        )[-1]
        seen = server.requests[-1]
        self.assertEqual(final.status, 200, final.text)
        self.assertEqual(seen.header("Mcp-Name"), "echo_header_params")
        self.assertEqual(seen.header("Mcp-Param-Region"), "=?base64?SGVsbG8sIOS4lueVjA==?=")
        self.assertEqual(seen.header("Mcp-Param-Count"), "42")
        self.assertEqual(seen.header("Mcp-Param-Verbose"), "true")

    def test_absent_argument_sends_no_param_header(self) -> None:
        server, target = self.fake()
        client = ModernClient(target)
        client.load_tools()
        client.call_tool("echo_header_params", {"count": 1})
        self.assertIsNone(server.requests[-1].header("Mcp-Param-Region"))
        self.assertEqual(server.requests[-1].header("Mcp-Param-Count"), "1")

    def test_tweaks_break_the_request_on_purpose(self) -> None:
        server, target = self.fake()
        client = ModernClient(target)
        tweaks = p.Tweaks(
            omit_meta_version=True,
            omit_version_header=True,
            method_header="x/y",
            session_id="s-1",
            accept="application/json",
            param_headers=[("Mcp-Param-Region", "raw")],
        )
        client.request(
            "tools/call",
            {"name": "echo_header_params", "arguments": {"region": "a"}},
            tool=TOOLS[1],
            tweaks=tweaks,
            request_id="abc",
        )
        seen = server.requests[-1]
        self.assertIsNone(seen.header("MCP-Protocol-Version"))
        self.assertEqual(seen.header("Mcp-Method"), "x/y")
        self.assertEqual(seen.header("Mcp-Session-Id"), "s-1")
        self.assertEqual(seen.header("Accept"), "application/json")
        self.assertEqual(seen.header("Mcp-Param-Region"), "raw")
        self.assertEqual(seen.json()["id"], "abc")
        self.assertNotIn(p.META_PROTOCOL_VERSION, seen.json()["params"]["_meta"])

    def test_user_header_replaces_generated_one_and_bearer_is_sent(self) -> None:
        server, target = self.fake()
        target.headers, target.bearer = [("Accept", "text/plain"), ("X-Tenant-Id", "t1")], "tok"
        ModernClient(target).discover()
        seen = server.requests[-1]
        self.assertEqual(
            [value for key, value in seen.headers if key.lower() == "accept"], ["text/plain"]
        )
        self.assertEqual(seen.header("X-Tenant-Id"), "t1")
        self.assertEqual(seen.header("Authorization"), "Bearer tok")

    def test_pagination_follows_next_cursor(self) -> None:
        server, target = self.fake(paged=True)
        client = ModernClient(target)
        tools = client.load_tools()
        self.assertEqual([tool["name"] for tool in tools], [tool["name"] for tool in TOOLS])
        self.assertEqual(len(client.pages), 2)
        self.assertEqual(server.requests[-1].json()["params"]["cursor"], "p2")

    def test_sse_response_is_parsed(self) -> None:
        _, target = self.fake(sse=True)
        client = ModernClient(target)
        final = client.call_tool(
            "convert_length", {"value": 1, "from_unit": "km", "to_unit": "m"}, tool=TOOLS[0]
        )[-1]
        self.assertTrue(final.is_sse)
        self.assertEqual(final.result_type, "complete")
        self.assertEqual(final.result["content"][0]["text"], "1 km = 1000 m")
        self.assertEqual(
            [note["method"] for note in final.notifications], ["notifications/progress"]
        )
        self.assertEqual(len(final.events), 2)

    def test_input_required_roundtrip(self) -> None:
        server, target = self.fake()
        client = ModernClient(target, capabilities={"elicitation": {}})
        exchanges = client.call_tool(
            "confirm_action", {"action": "delete"}, tool=TOOLS[2], auto_input=True
        )
        self.assertEqual(
            [exchange.result_type for exchange in exchanges], ["input_required", "complete"]
        )
        first, retry = (request.json() for request in server.requests[-2:])
        self.assertNotEqual(first["id"], retry["id"])
        self.assertEqual(retry["params"]["requestState"], "state-1")
        self.assertEqual(
            retry["params"]["inputResponses"],
            {"confirm": {"action": "accept", "content": {"ok": True}}},
        )
        self.assertEqual(retry["params"]["arguments"], first["params"]["arguments"])
        self.assertEqual(retry["params"]["_meta"][p.META_CLIENT_CAPABILITIES], {"elicitation": {}})

    def test_input_required_over_several_rounds(self) -> None:
        server, target = self.fake()
        client = ModernClient(target, capabilities={"elicitation": {}})
        before = len(server.requests)
        exchanges = client.call_tool(
            "convert_interactive", {"value": 3, "from_unit": "km"}, tool=TOOLS[3], auto_input=True
        )
        self.assertEqual(
            [exchange.result_type for exchange in exchanges],
            ["input_required", "input_required", "complete"],
        )
        first, second, third = (request.json() for request in server.requests[before:])
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        self.assertNotIn("requestState", first["params"])
        # Each retry: the state of the response before it and that round's answers only.
        self.assertEqual(second["params"]["requestState"], "unit-state")
        self.assertEqual(
            second["params"]["inputResponses"],
            {"target_unit": {"action": "accept", "content": {"to_unit": "m"}}},
        )
        self.assertEqual(third["params"]["requestState"], "precision-state")
        self.assertEqual(
            third["params"]["inputResponses"],
            {"precision": {"action": "accept", "content": {"decimals": 10}}},
        )
        for retry in (second, third):
            self.assertEqual(retry["params"]["arguments"], first["params"]["arguments"])
        self.assertEqual(exchanges[-1].result["content"][0]["text"], "3 km = 3000.0000000000 m")

    def test_sample_values_follow_enum_and_bounds(self) -> None:
        cases = (
            ({"type": "string", "enum": ["m", "cm"]}, "m"),
            ({"type": "integer", "minimum": 0, "maximum": 10}, 10),
            ({"type": "integer", "minimum": 100}, 100),
            ({"type": "integer", "minimum": 0, "maximum": 100}, 42),
            ({"type": "integer"}, 42),
            ({"type": "integer", "maximum": True}, 42),
            ({"type": "number", "minimum": 5}, 5),
            ({"type": "number", "maximum": 0.5}, 0.5),
            ({"type": "number"}, 1),
        )
        for schema, expected in cases:
            with self.subTest(schema=schema):
                self.assertEqual(p.sample_value(schema), expected)

    def test_auto_input_stops_after_max_rounds(self) -> None:
        def responder(request: Recorded):
            body = {
                "resultType": "input_required",
                "inputRequests": {},
                "requestState": f"state-{request.json()['id']}",
            }
            return as_json(200, {"jsonrpc": "2.0", "id": request.json()["id"], "result": body})

        server, target = self.fake(responder=responder)
        exchanges = ModernClient(target).call_tool("loop", {}, tool=None, auto_input=True, max_rounds=3)
        self.assertEqual(len(exchanges), 4)
        states = [request.json()["params"].get("requestState") for request in server.requests[-4:]]
        self.assertEqual(states[0], None)
        self.assertEqual(states[1:], [exchange.result["requestState"] for exchange in exchanges[:3]])

    def test_without_auto_input_the_first_result_is_returned(self) -> None:
        _, target = self.fake()
        exchanges = ModernClient(target).call_tool("confirm_action", {"action": "x"}, tool=TOOLS[2])
        self.assertEqual([exchange.result_type for exchange in exchanges], ["input_required"])

    def test_response_with_another_id_is_flagged(self) -> None:
        def responder(request: Recorded):
            body = {
                "jsonrpc": "2.0",
                "id": 999,
                "result": {"resultType": "complete", "tools": TOOLS},
            }
            return as_json(200, body)

        _, target = self.fake(responder)
        client = ModernClient(target)
        exchange = client.list_tools()
        self.assertTrue(exchange.id_mismatch)
        self.assertEqual(client.load_tools(refresh=True), [])
        self.assertTrue(client.rejected)

    def test_open_sse_stream_keeps_the_events_it_delivered(self) -> None:
        ack = {"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged", "params": {}}

        class Holding(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(f"event: message\ndata: {json.dumps(ack)}\n\n".encode())
                self.wfile.flush()
                self.server.release.wait(5)  # type: ignore[attr-defined]
                self.close_connection = True

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = self.serve(Holding)
        server.release = threading.Event()  # type: ignore[attr-defined]
        self.addCleanup(server.release.set)  # type: ignore[attr-defined]
        target = Target(url=f"http://127.0.0.1:{server.server_address[1]}/mcp", timeout=0.3)
        exchange = ModernClient(target).request("subscriptions/listen")
        self.assertIsNone(exchange.error)
        self.assertTrue(exchange.stream_open)
        self.assertEqual(len(exchange.notifications), 1)
        self.assertIn("스트림이 열린 채", report.summary_line(exchange))

    def test_transport_error_is_reported_not_raised(self) -> None:
        exchange = send("POST", "http://127.0.0.1:1/mcp", [], b"{}", timeout=1)
        self.assertIsNone(exchange.status)
        self.assertTrue(exchange.error)


class LegacyClientTest(ServerCase):
    def test_handshake_then_session_and_version_headers(self) -> None:
        server, target = self.fake()
        client = LegacyClient(target, "2025-06-18")
        init = client.initialize()
        listed = client.request("tools/list")
        init_request, notified, list_request = server.requests[-3:]
        self.assertEqual(init.status, 200)
        self.assertEqual(init_request.json()["params"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(init_request.header("Mcp-Session-Id"))
        self.assertIsNone(init_request.header("Mcp-Method"))
        self.assertEqual(notified.json(), {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(client.initialized_exchange.status, 202)
        self.assertEqual(list_request.header("Mcp-Session-Id"), client.session_id)
        self.assertEqual(list_request.header("MCP-Protocol-Version"), "2025-06-18")
        self.assertNotIn("_meta", list_request.json().get("params", {}))
        self.assertEqual(len(listed.result["tools"]), len(TOOLS))

    def test_reinitializes_after_session_404(self) -> None:
        _, target = self.fake()
        client = LegacyClient(target, reinit_on_404=True)
        client.initialize()
        client.session_id = "expired"
        exchange = client.request("tools/list")
        self.assertEqual(exchange.status, 200)
        self.assertEqual(client.reinitialized, 1)
        self.assertNotEqual(client.session_id, "expired")

    def test_404_is_returned_without_the_option(self) -> None:
        _, target = self.fake()
        client = LegacyClient(target)
        client.initialize()
        client.session_id = "expired"
        self.assertEqual(client.request("tools/list").status, 404)


class ResultJudgementTest(unittest.TestCase):
    """basic/index "ResultType" and basic/patterns/mrtr, from the client's side."""

    def test_absent_result_type_is_complete(self) -> None:
        self.assertEqual(p.judge_result("tools/list", {"tools": []}), ("complete", None))

    def test_known_values(self) -> None:
        self.assertEqual(p.judge_result("tools/list", {"resultType": "complete"})[0], "complete")
        self.assertEqual(
            p.judge_result("tools/call", {"resultType": "input_required", "requestState": "s"}),
            ("input_required", None),
        )

    def test_unknown_value_is_invalid(self) -> None:
        for value in ("bogus_result_type", "Complete", 1, None):
            with self.subTest(value=value):
                self.assertEqual(
                    p.judge_result("tools/call", {"resultType": value})[0], p.RESULT_INVALID
                )

    def test_input_required_only_where_the_spec_allows_it(self) -> None:
        result = {"resultType": "input_required", "requestState": "s"}
        for method in ("tools/list", "server/discover"):
            self.assertEqual(p.judge_result(method, result)[0], p.RESULT_INVALID)
        for method in ("tools/call", "resources/read", "prompts/get"):
            self.assertEqual(p.judge_result(method, result)[0], "input_required")
        self.assertEqual(
            p.judge_result("tools/call", {"resultType": "input_required"})[0], p.RESULT_INVALID
        )

    def test_absent_or_negative_ttl_is_zero(self) -> None:
        self.assertEqual(p.effective_ttl_ms({}), 0)
        self.assertEqual(p.effective_ttl_ms({"ttlMs": -5}), 0)
        self.assertEqual(p.effective_ttl_ms({"ttlMs": True}), 0)
        self.assertEqual(p.effective_ttl_ms({"ttlMs": 1500}), 1500)


# -- probe ---------------------------------------------------------------------------
class ProbeTest(ServerCase):
    def verdict(self, **kwargs: Any) -> str:
        return probe(self.fake(**kwargs)[1]).verdict

    def test_fake_servers_of_each_era(self) -> None:
        self.assertEqual(self.verdict(era="dual"), "dual")
        self.assertEqual(self.verdict(era="legacy"), "legacy")
        self.assertEqual(self.verdict(era="modern"), "modern")

    def test_real_server_eras(self) -> None:
        for era, expected in (("both", "dual"), ("legacy", "legacy"), ("modern", "modern")):
            with self.subTest(era=era):
                self.assertEqual(probe(self.real({"serverEra": era})).verdict, expected)

    def test_dual_client_stops_at_modern_without_legacy_check(self) -> None:
        server, target = self.fake(era="dual")
        result = probe(target, check_legacy=False)
        self.assertEqual((result.verdict, result.dual_client_choice), ("modern", "modern"))
        self.assertEqual(
            [request.json()["method"] for request in server.requests], ["server/discover"]
        )

    def test_400_with_empty_body_falls_back_to_initialize(self) -> None:
        legacy = SpecServer(era="legacy")

        def responder(request: Recorded):
            if request.header("Mcp-Method"):
                return 400, [], b""
            return legacy(request)

        server, target = self.fake(responder)
        result = probe(target)
        self.assertEqual((result.verdict, result.dual_client_choice), ("legacy", "legacy"))
        self.assertEqual(
            [request.method for request in server.requests], ["POST", "POST", "DELETE"]
        )
        self.assertEqual(server.requests[1].json()["method"], "initialize")

    def test_modern_error_is_retried_instead_of_falling_back(self) -> None:
        server, target = self.fake(era="modern")
        result = probe(target, check_legacy=False, version="2027-01-01")
        self.assertEqual(result.verdict, "modern")
        methods = [
            (request.json()["method"], request.header("MCP-Protocol-Version"))
            for request in server.requests
        ]
        self.assertEqual(
            methods, [("server/discover", "2027-01-01"), ("server/discover", "2026-07-28")]
        )

    def test_401_means_auth_required(self) -> None:
        self.assertEqual(self.verdict(auth=True), "auth-required")

    def discover_answer(self, answer: Any):
        """A dual server whose server/discover answer is replaced by ``answer``."""
        spec = SpecServer(era="dual")

        def responder(request: Recorded):
            payload = request.json() if request.method == "POST" else None
            if isinstance(payload, dict) and payload.get("method") == "server/discover":
                return answer(payload)
            return spec(request)

        return self.fake(responder)

    def methods(self, server: ThreadingHTTPServer) -> list[str]:
        return [request.json()["method"] for request in server.requests if request.method == "POST"]

    def test_discover_listing_only_legacy_versions_picks_initialize(self) -> None:
        def answer(payload: dict):
            result = {"resultType": "complete", "supportedVersions": ["2025-06-18", "2025-03-26"]}
            return as_json(200, {"jsonrpc": "2.0", "id": payload["id"], "result": result})

        server, target = self.discover_answer(answer)
        result = probe(target, check_legacy=False)
        self.assertEqual((result.dual_client_choice, result.fell_back), ("legacy", True))
        self.assertEqual(self.methods(server), ["server/discover", "initialize"])
        # The mutually supported version is the one asked for.
        self.assertEqual(server.requests[1].json()["params"]["protocolVersion"], "2025-06-18")

    def test_no_compatible_version_is_an_error_not_a_fallback(self) -> None:
        def unsupported(payload: dict):
            data = {"supported": ["2099-01-01"], "requested": p.MODERN_VERSION}
            return as_json(400, rpc_error(payload["id"], -32022, "Unsupported", data))

        def empty(payload: dict):
            result = {"resultType": "complete", "supportedVersions": []}
            return as_json(200, {"jsonrpc": "2.0", "id": payload["id"], "result": result})

        for answer in (unsupported, empty):
            with self.subTest(answer=answer.__name__):
                server, target = self.discover_answer(answer)
                result = probe(target, check_legacy=False)
                self.assertIsNone(result.dual_client_choice)
                self.assertFalse(result.fell_back)
                self.assertEqual(self.methods(server), ["server/discover"])

    def test_persistent_modern_error_never_falls_back(self) -> None:
        server, target = self.discover_answer(
            lambda payload: as_json(400, rpc_error(payload["id"], -32020, "Header mismatch"))
        )
        result = probe(target, check_legacy=False)
        self.assertEqual((result.dual_client_choice, result.fell_back), ("modern", False))
        self.assertEqual(self.methods(server), ["server/discover", "server/discover"])

    def test_anything_else_falls_back_to_initialize(self) -> None:
        def wrong_id(payload: dict):
            result = {"resultType": "complete", "supportedVersions": [p.MODERN_VERSION]}
            return as_json(200, {"jsonrpc": "2.0", "id": payload["id"] + 99, "result": result})

        answers = {
            "200 + -32601": lambda payload: as_json(200, rpc_error(payload["id"], -32601, "no")),
            "200 + -32602": lambda payload: as_json(200, rpc_error(payload["id"], -32602, "no")),
            "400 + -32600": lambda payload: as_json(400, rpc_error("server-error", -32600, "no")),
            "404 text": lambda payload: (404, [("Content-Type", "text/plain")], b"Not Found"),
            "503 text": lambda payload: (503, [("Content-Type", "text/plain")], b"down"),
            "broken json": lambda payload: (200, [("Content-Type", "application/json")], b"{"),
            "unknown resultType": lambda payload: as_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"resultType": "bogus", "supportedVersions": [p.MODERN_VERSION]},
                },
            ),
            "wrong id": wrong_id,
        }
        for name, answer in answers.items():
            with self.subTest(answer=name):
                server, target = self.discover_answer(answer)
                result = probe(target, check_legacy=False)
                self.assertEqual((result.dual_client_choice, result.fell_back), ("legacy", True))
                self.assertEqual(self.methods(server), ["server/discover", "initialize"])

    def test_unreachable(self) -> None:
        self.assertEqual(
            probe(Target(url="http://127.0.0.1:1/mcp", timeout=1)).verdict, "unreachable"
        )


# -- scenario runner and output --------------------------------------------------------
PORTABLE = ["modern", "modern-violation", "legacy", "compat", "auth-no-token-401", "auth-bad-token"]


class RunnerTest(ServerCase):
    def run_all(
        self, names: list[str], options: Options | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        _, target = self.fake(**kwargs)
        if kwargs.get("auth"):
            target.bearer = "good-token"
        results = Runner(target, options or Options(), SCENARIOS).run(names)
        return {result.scenario.name: result for result in results}

    def describe(self, results: dict[str, Any]) -> str:
        return report.format_results(list(results.values()), "test")

    def test_compliant_dual_server_passes(self) -> None:
        results = self.run_all(PORTABLE, era="dual", auth=True, paged=True)
        bad = {
            name: result.status for name, result in results.items() if result.status in (FAIL, WARN)
        }
        self.assertEqual(bad, {}, self.describe(results))
        skipped = sorted(name for name, result in results.items() if result.status == SKIP)
        self.assertEqual(skipped, ["delete-405", "get-405", "subscriptions-listen"])
        self.assertEqual(results["input-required-roundtrip"].status, PASS)
        self.assertEqual(results["input-required-multi-round"].status, PASS)
        self.assertEqual(results["param-header-ok"].status, PASS)
        self.assertEqual(results["legacy-session-expire-reconnect"].status, PASS)

    def test_modern_only_server(self) -> None:
        results = self.run_all(["modern-violation", "legacy", "compat"], era="modern")
        self.assertEqual(
            {name for name, result in results.items() if result.status in (FAIL, WARN)},
            set(),
            self.describe(results),
        )
        self.assertEqual(results["get-405"].status, PASS)
        self.assertEqual(results["delete-405"].status, PASS)
        self.assertEqual(results["legacy-initialize"].status, SKIP)
        self.assertEqual(results["compat-legacy-client"].status, PASS)

    def test_legacy_only_server(self) -> None:
        results = self.run_all(["modern", "legacy", "compat"], era="legacy")
        self.assertEqual(
            {name for name, result in results.items() if result.status in (FAIL, WARN)},
            set(),
            self.describe(results),
        )
        self.assertEqual(results["discover"].status, SKIP)
        self.assertIn("legacy", results["discover"].reason)
        self.assertEqual(results["legacy-tools-call-ok"].status, PASS)
        self.assertEqual(results["compat-dual-client"].status, PASS)

    def test_spec_deviations_are_reported(self) -> None:
        broken = {"no-ttl", "no-server-info", "unknown-tool-iserror", "no-session-check"}
        results = self.run_all(
            ["discover", "unknown-tool", "legacy-no-session", "legacy-session-expire-reconnect"],
            broken=broken,
        )
        self.assertEqual(results["discover"].status, FAIL)
        labels = {(check.label, check.level) for check in results["discover"].failures()}
        self.assertEqual(
            labels,
            {
                ("server/discover: ttlMs", "must"),
                ("server/discover: cacheScope", "must"),
                ("_meta에 serverInfo", "should"),
            },
        )
        self.assertEqual(results["unknown-tool"].status, FAIL)
        self.assertEqual(results["legacy-no-session"].status, WARN)
        self.assertEqual(results["legacy-session-expire-reconnect"].status, SKIP)
        self.assertEqual(exit_code(list(results.values())), 1)
        self.assertEqual(exit_code([results["legacy-no-session"]]), 0)
        self.assertEqual(exit_code([results["legacy-no-session"]], strict=True), 1)

    def run_real(self, names: list[str], cfg: dict | None = None, **options: Any) -> dict[str, Any]:
        results = Runner(self.real(cfg), Options(**options), SCENARIOS).run(names)
        return {result.scenario.name: result for result in results}

    def test_real_server_in_every_era_has_no_failures(self) -> None:
        """Default server config: the scenarios switch on what they need via ?cfg=."""
        expected_warn = {"both": set(), "modern": set(), "legacy": {"auth-no-token-401"}}
        for era in ("both", "legacy", "modern"):
            with self.subTest(era=era):
                results = self.run_real(["all"], {"serverEra": era})
                by_status: dict[str, set[str]] = {FAIL: set(), WARN: set()}
                for name, result in results.items():
                    if result.status in by_status:
                        by_status[result.status].add(name)
                self.assertEqual(by_status[FAIL], set(), self.describe(results))
                self.assertEqual(by_status[WARN], expected_warn[era], self.describe(results))
        # era == "modern" from the last round: the session scenarios cannot run.
        self.assertEqual(results["legacy-no-session"].status, SKIP)

    def test_default_off_server_options_are_injected(self) -> None:
        names = [
            "tools-list-pagination",
            "legacy-no-session",
            "legacy-unknown-session-404",
            "legacy-session-expire-reconnect",
            "legacy-delete-session",
            "auth-insufficient-scope-403",
        ]
        results = self.run_real(names)
        self.assertEqual({r.status for r in results.values()}, {PASS}, self.describe(results))
        self.assertTrue(
            any('"pageSize":3' in note for note in results["tools-list-pagination"].notes)
        )
        self.assertGreater(len(results["tools-list-pagination"].exchanges), 2)
        # Without injection the default (lenient) server behaviour shows up again.
        plain = self.run_real(names, inject=False)
        self.assertEqual(plain["legacy-no-session"].status, WARN)
        self.assertEqual(plain["legacy-session-expire-reconnect"].status, SKIP)
        self.assertEqual(plain["auth-insufficient-scope-403"].status, WARN)

    def test_injection_group_runs_only_against_a_cfg_server(self) -> None:
        results = self.run_real(["injection"])
        self.assertEqual({r.status for r in results.values()}, {PASS}, self.describe(results))
        self.assertEqual(
            sorted(results),
            [
                "inject-discover-modes",
                "inject-legacy-probe-modes",
                "inject-missing-capability",
                "inject-modern-response",
                "inject-tool-call-modes",
            ],
        )
        # A server that ignores ?cfg= (the fake), --no-inject and the gateway profile skip it.
        for options in (Options(), None):
            fake = self.run_all(["injection"], options)
            self.assertEqual({r.status for r in fake.values()}, {SKIP})
        self.assertEqual(
            {r.status for r in self.run_real(["injection"], inject=False).values()}, {SKIP}
        )
        gateway = self.run_real(["injection"], profile="gateway", assume_era="dual")
        self.assertEqual({r.status for r in gateway.values()}, {SKIP})

    def test_injection_group_on_a_legacy_only_server(self) -> None:
        results = self.run_real(["injection"], {"serverEra": "legacy"})
        self.assertEqual(results["inject-discover-modes"].status, SKIP)
        self.assertEqual(results["inject-modern-response"].status, SKIP)
        self.assertEqual(results["inject-legacy-probe-modes"].status, PASS, self.describe(results))
        self.assertEqual(results["inject-tool-call-modes"].status, PASS, self.describe(results))

    def test_test_tools_are_the_default_but_any_tool_can_be_named(self) -> None:
        results = self.run_real(["param-header-ok", "input-required-roundtrip"])
        self.assertEqual({r.status for r in results.values()}, {PASS}, self.describe(results))
        bodies = [
            exchange.request_json() or {}
            for result in results.values()
            for exchange in result.exchanges
        ]
        called = {
            (body.get("params") or {}).get("name")
            for body in bodies
            if body.get("method") == "tools/call"
        }
        self.assertEqual(called, {"echo_header_params", "confirm_action"})
        missing = self.run_real(
            ["param-header-ok", "input-required-roundtrip"],
            header_tool="no_such_tool",
            input_tool="no_such_tool",
        )
        self.assertEqual({r.status for r in missing.values()}, {SKIP})
        self.assertIn("--header-tool", missing["param-header-ok"].reason)
        # Tools are still discovered when the default names are absent.
        hidden = self.run_real(
            ["param-header-ok", "input-required-roundtrip"], {"modern": {"testTools": False}}
        )
        self.assertEqual({r.status for r in hidden.values()}, {SKIP})

    def test_multi_round_scenario_on_the_real_server(self) -> None:
        result = self.run_real(["input-required-multi-round"])["input-required-multi-round"]
        self.assertEqual(result.status, PASS, self.describe({"multi": result}))
        self.assertEqual(
            [exchange.result_type for exchange in result.exchanges if exchange.label.startswith("tools/call")],
            ["input_required", "input_required", "complete"],
        )
        hidden = self.run_real(["input-required-multi-round"], {"modern": {"testTools": False}})
        self.assertEqual(hidden["input-required-multi-round"].status, SKIP)
        gateway = self.run_real(["input-required-multi-round"], profile="gateway", assume_era="dual")
        self.assertEqual(gateway["input-required-multi-round"].status, SKIP)
        self.assertIn("미확정", gateway["input-required-multi-round"].reason)

    def test_version_header_mismatch_is_a_header_mismatch_on_the_real_server(self) -> None:
        for era in ("both", "modern"):
            with self.subTest(era=era):
                result = self.run_real(["version-header-mismatch"], {"serverEra": era})[
                    "version-header-mismatch"
                ]
                self.assertEqual(result.status, PASS)
                exchange = result.exchanges[-1]
                self.assertEqual((exchange.status, exchange.rpc_code), (400, -32020))

    def test_dual_server_may_treat_a_headerless_request_as_legacy(self) -> None:
        def headerless_as_legacy(era: str):
            spec = SpecServer(era=era)

            def responder(request: Recorded):
                payload = request.json() if request.method == "POST" else None
                if (
                    isinstance(payload, dict)
                    and payload.get("method") == "tools/list"
                    and request.header("MCP-Protocol-Version") is None
                ):
                    return as_json(
                        200, {"jsonrpc": "2.0", "id": payload["id"], "result": {"tools": TOOLS}}
                    )
                return spec(request)

            return responder

        def status(era: str) -> str:
            _, target = self.fake(headerless_as_legacy(era))
            return Runner(target, Options(), SCENARIOS).run(["no-version-header"])[0].status

        self.assertEqual(status("dual"), PASS)
        # A modern-only server has no such leeway: it MUST reject.
        self.assertEqual(status("modern"), FAIL)

    def test_gateway_profile_skips_unconfirmed_expectations(self) -> None:
        options = Options(profile="gateway", assume_era="dual")
        results = self.run_all(["discover", "auth-oauth-flow", "no-meta-version"], options)
        self.assertEqual(results["discover"].status, SKIP)
        self.assertIn("미확정", results["discover"].reason)
        self.assertEqual(results["auth-oauth-flow"].status, SKIP)
        self.assertEqual(results["no-meta-version"].status, PASS)
        self.assertTrue(any("조건부" in note for note in results["no-meta-version"].notes))
        included = self.run_all(
            ["discover"], Options(profile="gateway", assume_era="dual", include_unconfirmed=True)
        )
        self.assertEqual(included["discover"].status, PASS)

    def test_every_scenario_states_expectation_and_basis(self) -> None:
        names = [scenario.name for scenario in SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        for scenario in SCENARIOS:
            self.assertTrue(
                scenario.expect and scenario.basis and scenario.gateway.basis, scenario.name
            )
            self.assertIn(scenario.gateway.kind, report.GATEWAY_KIND)

    def test_text_report_shows_expected_and_actual(self) -> None:
        results = self.run_all(["discover", "tools-list"], broken={"no-ttl"})
        text = self.describe(results)
        self.assertRegex(text, r"FAIL\s+modern\s+discover")
        self.assertIn("[MUST] server/discover: ttlMs: 기대 0 이상 정수 / 실제 None", text)
        self.assertIn("근거: discover", text)
        self.assertIn("합계: PASS 0, FAIL 2, WARN 0, SKIP 0", text)


class HostileOutputTest(ServerCase):
    """D7: bytes the target sends must not reach the terminal as control codes."""

    HOSTILE = "\x1b[2J\x1b]0;owned\x07\rPASS \x9b31m\x7f"

    def responder(self, kind: str) -> Any:
        hostile = self.HOSTILE

        def respond(request: Recorded) -> tuple[int, list[tuple[str, str]], bytes]:
            if kind == "text":
                body = f"oops {hostile}\nsecond line".encode("utf-8")
                return 500, [("Content-Type", "text/plain"), ("X-Note", "a\x1b[1mb")], body
            payload = request.json()
            rid = payload.get("id") if isinstance(payload, dict) else None
            return as_json(400, rpc_error(rid, -32602, f"bad {hostile}\nFAKE LINE", {"why": hostile}))

        return respond

    def assert_clean(self, out: str) -> None:
        for char in out:
            code = ord(char)
            self.assertFalse(
                (code < 0x20 and char not in "\n\t") or 0x7F <= code <= 0x9F,
                f"control character {char!r} in output: {out!r}",
            )

    def test_call_output(self) -> None:
        for kind in ("text", "json"):
            for flags in ((), ("-v",)):
                with self.subTest(kind=kind, flags=flags):
                    _, target = self.fake(self.responder(kind))
                    code, out = self.cli("call", "tools/list", "--url", target.url, *flags)
                    self.assertEqual(code, 0)
                    self.assert_clean(out)
                    # JSON bodies are re-serialised, so C0 controls show as \u001b;
                    # DEL and C1, which JSON leaves raw, show as \x7f / \x9b.
                    self.assertIn("\\u001b[2J" if kind == "json" else "\\x1b[2J", out)
                    self.assertIn("\\x9b31m\\x7f", out)
        # A one-line field cannot start a new output line.
        _, target = self.fake(self.responder("json"))
        _, out = self.cli("call", "tools/list", "--url", target.url)
        self.assertNotRegex(out, r"(?m)^FAKE LINE")
        # A plain-text body keeps its own line breaks.
        _, target = self.fake(self.responder("text"))
        _, out = self.cli("call", "tools/list", "--url", target.url)
        self.assertRegex(out, r"(?m)^second line$")

    def test_run_and_probe_output(self) -> None:
        _, target = self.fake(self.responder("json"))
        code, out = self.cli(
            "run", "discover", "tools-list", "--url", target.url, "--assume-era", "modern", "-v"
        )
        self.assertEqual(code, 1)
        self.assert_clean(out)
        self.assertIn("\\x1b[2J", out)
        self.assertNotRegex(out, r"(?m)^FAKE LINE")
        code, out = self.cli("probe", "--url", target.url, "-v")
        self.assert_clean(out)

    def test_json_output_is_still_valid_json(self) -> None:
        _, target = self.fake(self.responder("json"))
        _, out = self.cli("call", "tools/list", "--url", target.url, "--json")
        payload = json.loads(out)
        self.assertIn(self.HOSTILE, payload["exchanges"][-1]["responseBody"]["error"]["message"])


class StrictReportTest(ServerCase):
    """D8: with --strict the totals and the footer say that WARN counts as failure."""

    def warn_target(self) -> Target:
        # legacy-no-session is WARN (a SHOULD) against a server that does not
        # check the session.
        _, target = self.fake(era="dual", broken={"no-session-check"})
        return target

    def run_cli(self, *flags: str) -> tuple[int, str]:
        target = self.warn_target()
        return self.cli(
            "run", "legacy-no-session", "tools-list", "--url", target.url,
            "--assume-era", "dual", "--no-inject", *flags,
        )

    def test_without_strict(self) -> None:
        code, out = self.run_cli()
        self.assertRegex(out, r"(?m)^WARN\s+legacy\s+legacy-no-session")
        totals = next(line for line in out.splitlines() if line.startswith("합계:"))
        self.assertRegex(totals, r"^합계: PASS \d+, FAIL \d+, WARN 1, SKIP \d+$")
        self.assertIn("--strict를 주면 실패로 센다.", out)
        self.assertEqual(code, 1 if "FAIL 0" not in totals else 0)

    def test_with_strict(self) -> None:
        code, out = self.run_cli("--strict")
        self.assertEqual(code, 1)
        totals = next(line for line in out.splitlines() if line.startswith("합계:"))
        match = re.fullmatch(
            r"합계: PASS \d+, FAIL (\d+), WARN 1, SKIP \d+ \(--strict: WARN 1건 포함 실패 (\d+)건\)",
            totals,
        )
        assert match is not None, totals
        self.assertEqual(int(match.group(2)), int(match.group(1)) + 1)
        self.assertIn("--strict라서 실패로 센다(종료 코드 1).", out)
        self.assertNotIn("--strict를 주면", out)

    def test_json_reports_strict_and_failed(self) -> None:
        for flags, strict in (((), False), (("--strict",), True)):
            with self.subTest(strict=strict):
                code, out = self.run_cli("--json", *flags)
                payload = json.loads(out)
                summary = payload["summary"]
                self.assertEqual(set(summary), {"PASS", "FAIL", "WARN", "SKIP"})
                self.assertEqual(summary["WARN"], 1)
                self.assertIs(payload["strict"], strict)
                self.assertEqual(payload["failed"], summary["FAIL"] + (1 if strict else 0))
                self.assertEqual(code, 1 if payload["failed"] else 0)

    def test_strict_without_warnings_changes_nothing(self) -> None:
        results: list[Any] = []
        self.assertEqual(
            report.format_results(results, "h", strict=True), report.format_results(results, "h")
        )


class CliTest(ServerCase):
    def test_list_shows_every_group(self) -> None:
        code, out = self.cli("list", "--expect", "gateway")
        self.assertEqual(code, 0)
        for group in (
            "[modern]",
            "[modern-violation]",
            "[legacy]",
            "[auth]",
            "[compat]",
            "[injection]",
        ):
            self.assertIn(group, out)
        self.assertIn("기대:", out)
        self.assertIn("근거:", out)
        self.assertIn("게이트웨이(", out)
        code, out = self.cli("list", "--json")
        self.assertEqual(len(json.loads(out)), len(SCENARIOS))

    def test_run_exit_code_and_json_output_mask_the_token(self) -> None:
        _, target = self.fake(auth=True)
        base = ["--url", target.url, "--bearer", "good-token", "--assume-era", "dual"]
        code, out = self.cli("run", "tools-list", "unknown-tool", "--json", "-v", *base)
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(payload["summary"], {"PASS": 2, "FAIL": 0, "WARN": 0, "SKIP": 0})
        self.assertNotIn("good-token", out)
        self.assertIn("Bearer ***", out)
        code, out = self.cli(
            "run", "tools-list", *base[:2], "--bearer", "nope", "--assume-era", "dual"
        )
        self.assertEqual(code, 1)
        self.assertRegex(out, r"FAIL\s+modern\s+tools-list")
        self.assertEqual(self.cli("run", "no-such-scenario", *base)[0], 2)

    def test_call_violation_flags_reach_the_wire(self) -> None:
        server, target = self.fake()
        code, out = self.cli(
            "call",
            "tools/call",
            "--url",
            target.url,
            "--tool",
            "echo_header_params",
            "--args",
            '{"region":"서울"}',
            "--omit-capabilities",
            "--version-header",
            "2025-11-25",
            "--meta-version",
            "2026-07-28",
            "--name-header",
            "other",
            "--param-header",
            "Region=plain",
            "--id",
            "77",
            "--bearer",
            "secret-value",
        )
        seen = server.requests[-1]
        self.assertEqual(code, 0)
        self.assertEqual(server.requests[0].json()["method"], "tools/list")
        self.assertEqual(seen.header("MCP-Protocol-Version"), "2025-11-25")
        self.assertEqual(seen.header("Mcp-Name"), "other")
        self.assertEqual(seen.header("Mcp-Param-Region"), "plain")
        self.assertEqual(seen.json()["id"], 77)
        self.assertNotIn(p.META_CLIENT_CAPABILITIES, seen.json()["params"]["_meta"])
        self.assertIn("HTTP 400", out)
        self.assertIn("code -32602", out)
        self.assertNotIn("secret-value", out)

    def test_call_raw_body_and_auto_input(self) -> None:
        server, target = self.fake()
        code, _ = self.cli(
            "call",
            "tools/call",
            "--url",
            target.url,
            "--tool",
            "confirm_action",
            "--args",
            '{"action":"go"}',
            "--auto-input",
            "--json",
        )
        self.assertEqual(code, 0)
        self.assertIn("inputResponses", server.requests[-1].json()["params"])
        import tempfile

        with tempfile.NamedTemporaryFile("wb", suffix=".json") as handle:
            handle.write(b"{not json")
            handle.flush()
            self.cli("call", "tools/list", "--url", target.url, "--raw-body", handle.name)
        self.assertEqual(server.requests[-1].body, b"{not json")
        self.assertEqual(server.requests[-1].header("Mcp-Method"), "tools/list")

    def test_call_legacy_mode_against_real_server(self) -> None:
        target = self.real()
        code, out = self.cli("call", "tools/list", "--url", target.url, "--mode", "legacy", "-v")
        self.assertEqual(code, 0)
        self.assertIn("[initialize]", out)
        self.assertIn("Mcp-Session-Id", out)
        self.assertIn('"tools"', out)

    def test_no_inject_flag(self) -> None:
        target = self.real()
        base = ["run", "legacy-no-session", "inject-tool-call-modes", "--url", target.url, "--json"]
        code, out = self.cli(*base)
        statuses = {item["name"]: item["status"] for item in json.loads(out)["results"]}
        self.assertEqual(statuses, {"legacy-no-session": PASS, "inject-tool-call-modes": PASS}, out)
        code, out = self.cli(*base, "--no-inject")
        statuses = {item["name"]: item["status"] for item in json.loads(out)["results"]}
        self.assertEqual(statuses, {"legacy-no-session": WARN, "inject-tool-call-modes": SKIP})
        self.assertEqual(code, 0)

    def test_cfg_json_and_probe_against_real_server(self) -> None:
        target = self.real()
        code, out = self.cli("probe", "--url", target.url, "--cfg-json", '{"serverEra":"legacy"}')
        self.assertEqual(code, 0)
        self.assertIn("판정: legacy", out)
        code, out = self.cli(
            "probe", "--url", target.url, "--scenario-header", "server-era-modern", "--json"
        )
        self.assertEqual(json.loads(out)["verdict"], "modern")

    def test_oauth_flow_against_real_server(self) -> None:
        target = self.real()
        code, out = self.cli(
            "run",
            "auth-oauth-flow",
            "auth-no-token-401",
            "--url",
            target.url,
            "--assume-era",
            "legacy",
            "-v",
            "--json",
        )
        payload = json.loads(out)
        statuses = {result["name"]: result["status"] for result in payload["results"]}
        self.assertEqual(statuses["auth-oauth-flow"], PASS, out)
        self.assertNotEqual(statuses["auth-no-token-401"], FAIL, out)
        self.assertNotRegex(out, r'"access_token": "ey')
        self.assertNotRegex(out, r"Bearer ey")
        # D6: the form bodies of /authorize and /token, and the redirect.
        self.assertIn("code=***", out)
        self.assertIn("code_verifier=***", out)
        self.assertIn("refresh_token=***", out)
        self.assertNotRegex(out, r"(code|code_verifier|refresh_token|access_token)=(?!\*\*\*)[^&\"]")
        # No JWT (access or refresh token) anywhere in the output.
        self.assertNotRegex(out, r"eyJ[A-Za-z0-9_-]+\.eyJ")


if __name__ == "__main__":
    unittest.main()
