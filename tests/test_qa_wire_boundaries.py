"""QA: wire-level boundary cases that the option tests do not cover.

Malformed bodies, the client/server compatibility matrix as one table, and a
null ``requestState``. Everything goes through a real Handler on a loopback
port.
"""

from __future__ import annotations

import json
from http.client import HTTPConnection

from mcp_test_support import (
    BOTH_ACCEPT,
    ERR_UNSUPPORTED_VERSION,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
)

ERR_PARSE = -32700
MODERN = {"serverEra": "modern"}
LEGACY = {"serverEra": "legacy"}


class MalformedBodyTest(HttpServerTestCase):
    def post_raw(self, body: bytes, *, headers: dict[str, str] | None = None, config: dict | None = None):
        request_headers = {"Content-Type": "application/json", "Accept": BOTH_ACCEPT}
        request_headers.update(headers or {})
        status, _, raw = self.request("POST", self.path(config), headers=request_headers, body=body)
        return status, json.loads(raw)

    def test_empty_and_truncated_bodies_are_parse_errors(self) -> None:
        for label, headers, config in (
            ("legacy", {}, None),
            ("modern", modern_headers("server/discover"), None),
            ("modern-only", modern_headers("server/discover"), MODERN),
        ):
            for body in (b"", b'{"jsonrpc":"2.0","id":1,"method":'):
                with self.subTest(client=label, body=body):
                    status, payload = self.post_raw(body, headers=headers, config=config)
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error"]["code"], ERR_PARSE)
                    self.assertIsNone(payload["id"])

    def test_large_body_is_served(self) -> None:
        payload = modern_request("tools/list", pad="x" * (1024 * 1024))
        status, _, body, _ = self.post(payload, headers=modern_headers("tools/list"), config=MODERN)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["resultType"], "complete")


class CompatibilityMatrixTest(HttpServerTestCase):
    """The four client/server cells of the spec's compatibility matrix."""

    def test_modern_client_modern_server(self) -> None:
        status, _, body, _ = self.modern_post("server/discover", config=MODERN)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["supportedVersions"], ["2026-07-28"])
        payload, headers = modern_call("convert_length", {"value": 1, "from_unit": "km", "to_unit": "m"})
        status, _, body, _ = self.post(payload, headers=headers, config=MODERN)
        self.assertEqual((status, body["result"]["isError"]), (200, False))

    def test_modern_client_legacy_server_falls_back_to_initialize(self) -> None:
        status, _, body, _ = self.modern_post("server/discover", config=LEGACY)
        # Not a DiscoverResult and not a modern error code: the client falls back.
        self.assertEqual(status, 200)
        self.assertNotIn("result", body)
        self.assertNotIn(body["error"]["code"], (-32020, -32021, -32022))
        status, headers, body, _ = self.post(legacy_initialize(), config=LEGACY)
        self.assertEqual(status, 200)
        self.assertIn("protocolVersion", body["result"])
        session = {"Mcp-Session-Id": headers["mcp-session-id"]}
        status, _, body, _ = self.post(legacy_request("tools/list"), headers=session, config=LEGACY)
        self.assertEqual(status, 200)
        self.assertNotIn("resultType", body["result"])

    def test_legacy_client_legacy_server(self) -> None:
        status, _, body, _ = self.post(legacy_initialize(), config=LEGACY)
        self.assertEqual(status, 200)
        call = legacy_request(
            "tools/call", name="convert_length", arguments={"value": 1, "from_unit": "km", "to_unit": "m"}
        )
        status, _, body, _ = self.post(call, config=LEGACY)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["content"][0]["text"], "1 km = 1000 m")

    def test_legacy_client_modern_server_is_rejected_at_every_step(self) -> None:
        for payload in (
            legacy_initialize(version="2025-11-25"),
            legacy_request("tools/list"),
            legacy_request("tools/call", name="convert_length", arguments={}),
        ):
            with self.subTest(method=payload["method"]):
                status, headers, body, _ = self.post(payload, config=MODERN)
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], ERR_UNSUPPORTED_VERSION)
                self.assertEqual(body["error"]["data"]["supported"], ["2026-07-28"])
                self.assertNotIn("mcp-session-id", headers)
        for method in ("GET", "DELETE"):
            with self.subTest(http=method):
                status, _, _ = self.request(method, self.path(MODERN), headers={"Accept": "text/event-stream"})
                self.assertEqual(status, 405)


class NullRequestStateTest(HttpServerTestCase):
    def test_null_request_state_starts_over(self) -> None:
        payload, headers = modern_call(
            "confirm_action",
            {"action": "deploy"},
            capabilities={"elicitation": {}},
            inputResponses={"confirm": {"action": "accept", "content": {"confirmed": True}}},
            requestState=None,
        )
        status, _, body, _ = self.post(payload, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["resultType"], "input_required")
        self.assertIsInstance(body["result"]["requestState"], str)


class UndecodableBodyTest(HttpServerTestCase):
    """D1: a body json.loads cannot even decode must not kill the connection."""

    post_raw = MalformedBodyTest.post_raw

    def test_non_utf8_body_is_a_parse_error(self) -> None:
        for label, headers, config in (
            ("legacy", {}, None),
            ("modern", modern_headers("server/discover"), None),
            ("modern-only", modern_headers("server/discover"), MODERN),
            ("legacy-only", {}, LEGACY),
        ):
            for body in (b"\xff\xfe\xfd", b'{"jsonrpc":"2.0","id":1,"method":"\xe9"}', b"\x80"):
                with self.subTest(client=label, body=body):
                    status, payload = self.post_raw(body, headers=headers, config=config)
                    self.assertEqual(status, 400)
                    self.assertEqual(
                        payload,
                        {"jsonrpc": "2.0", "id": None, "error": {"code": ERR_PARSE, "message": "Parse error"}},
                    )

    def test_deeply_nested_and_oversized_number_bodies_are_parse_errors(self) -> None:
        for body in (b"[" * 200_000, b'{"jsonrpc":"2.0","id":' + b"9" * 5000 + b"}"):
            with self.subTest(prefix=body[:24]):
                status, payload = self.post_raw(body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], ERR_PARSE)
                self.assertIsNone(payload["id"])

    def test_non_utf8_bodies_on_the_other_json_endpoints(self) -> None:
        for path in ("/register", "/scenario"):
            with self.subTest(path=path):
                status, _, raw = self.request(
                    "POST", path, headers={"Content-Type": "application/json"}, body=b"\xff\xfe"
                )
                self.assertEqual(status, 400)
                self.assertIsInstance(json.loads(raw), dict)
        status, _, raw = self.request(
            "POST",
            "/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=b"grant_type=\xff\xfe",
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw), {"error": "unsupported_grant_type"})


class DuplicateHeaderTest(HttpServerTestCase):
    """D2: a routing header sent twice is -32020 on the modern path (as mcp 2.3.0)."""

    def post_pairs(self, payload: dict, pairs: list[tuple[str, str]], config: dict | None = None):
        body = json.dumps(payload).encode("utf-8")
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest("POST", self.path(config))
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Accept", BOTH_ACCEPT)
            conn.putheader("Content-Length", str(len(body)))
            for name, value in pairs:
                conn.putheader(name, value)
            conn.endheaders(body)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def call_pairs(self, arguments: dict | None = None) -> tuple[dict, list[tuple[str, str]]]:
        name = "echo_header_params" if arguments is not None else "convert_length"
        payload, headers = modern_call(
            name, arguments if arguments is not None else {"value": 1, "from_unit": "km", "to_unit": "m"}
        )
        return payload, list(headers.items())

    def assert_duplicate(self, result: tuple[int, dict], header: str) -> None:
        status, body = result
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["code"], -32020)
        self.assertEqual(body["error"]["message"], f"{header} header appears more than once")
        self.assertEqual(body["id"], 1)

    def test_baseline_without_duplicates(self) -> None:
        payload, pairs = self.call_pairs()
        status, body = self.post_pairs(payload, pairs)
        self.assertEqual((status, body["result"]["isError"]), (200, False))

    def test_conflicting_routing_headers_are_rejected(self) -> None:
        for extra, display in (
            (("Mcp-Method", "tools/list"), "Mcp-Method"),
            (("Mcp-Name", "convert_weight"), "Mcp-Name"),
            (("MCP-Protocol-Version", "2099-01-01"), "MCP-Protocol-Version"),
        ):
            for order in ("after", "before"):
                with self.subTest(header=display, order=order):
                    payload, pairs = self.call_pairs()
                    pairs = pairs + [extra] if order == "after" else [extra] + pairs
                    self.assert_duplicate(self.post_pairs(payload, pairs), display)
                    self.assert_duplicate(self.post_pairs(payload, pairs, MODERN), display)

    def test_same_value_duplicates_are_rejected_too(self) -> None:
        # mcp 2.3.0 find_duplicated_routing_header does not look at the values.
        for name, display in (
            ("mcp-method", "Mcp-Method"),
            ("MCP-NAME", "Mcp-Name"),
            ("Mcp-Protocol-Version", "MCP-Protocol-Version"),
        ):
            with self.subTest(header=display):
                payload, pairs = self.call_pairs()
                value = next(v for k, v in pairs if k.lower() == name.lower())
                self.assert_duplicate(self.post_pairs(payload, pairs + [(name, value)]), display)

    def test_duplicated_mcp_param_header(self) -> None:
        payload, pairs = self.call_pairs({"region": "us-west1", "count": 3})
        good = pairs + [("Mcp-Param-Region", "us-west1"), ("Mcp-Param-Count", "3")]
        status, body = self.post_pairs(payload, good)
        self.assertEqual((status, body["result"]["isError"]), (200, False))
        for extra in (("Mcp-Param-Region", "us-east1"), ("mcp-param-region", "us-west1")):
            with self.subTest(extra=extra):
                self.assert_duplicate(self.post_pairs(payload, good + [extra]), "Mcp-Param-Region")
        self.assert_duplicate(self.post_pairs(payload, good + [("Mcp-Param-Count", "4")]), "Mcp-Param-Count")

    def test_duplicated_unrecognized_param_header_is_ignored(self) -> None:
        # Only headers the tool schema declares are validated (as mcp 2.3.0).
        payload, pairs = self.call_pairs()
        pairs += [("Mcp-Param-Region", "a"), ("Mcp-Param-Region", "b")]
        status, body = self.post_pairs(payload, pairs)
        self.assertEqual((status, body["result"]["isError"]), (200, False))

    def test_trailing_whitespace_on_the_wire(self) -> None:
        # D3: http.server keeps trailing OWS in the parsed value.
        payload, _ = self.call_pairs({"region": "us-west1", "count": 3})
        pairs = [
            ("MCP-Protocol-Version", "2026-07-28 "),
            ("Mcp-Method", "  tools/call \t"),
            ("Mcp-Name", "echo_header_params\t "),
            ("Mcp-Param-Region", "\tus-west1  "),
            ("Mcp-Param-Count", " 3 "),
        ]
        status, body = self.post_pairs(payload, pairs)
        self.assertEqual((status, body["result"]["isError"]), (200, False), body)

    def test_legacy_and_lenient_paths_do_not_look_at_duplicates(self) -> None:
        legacy = [("MCP-Protocol-Version", "2025-06-18"), ("MCP-Protocol-Version", "2025-06-18")]
        status, body = self.post_pairs(legacy_request("tools/list"), legacy)
        self.assertEqual(status, 200)
        self.assertIn("tools", body["result"])
        payload, pairs = self.call_pairs()
        status, body = self.post_pairs(
            payload, pairs + [("Mcp-Method", "tools/call")], {"strict2026": False}
        )
        self.assertEqual(status, 200)


class UnsupportedMethodTest(HttpServerTestCase):
    """D5: methods without a handler are 405 + Allow, not 501."""

    def test_mcp_on_every_era(self) -> None:
        for config, allow in (
            (None, "GET, POST, OPTIONS"),
            (LEGACY, "GET, POST, OPTIONS"),
            (MODERN, "POST, OPTIONS"),
            ({"serverEra": "modern", "strict2026": False}, "GET, POST, OPTIONS"),
            ({"session": {"validate": True}}, "GET, POST, OPTIONS, DELETE"),
        ):
            for method in ("PUT", "PATCH", "HEAD", "TRACE", "PROPFIND"):
                with self.subTest(config=config, method=method):
                    status, headers, raw = self.request(
                        method, self.path(config), body=b"{}" if method in ("PUT", "PATCH") else None
                    )
                    self.assertEqual(status, 405)
                    self.assertEqual(headers["allow"], allow)
                    self.assertEqual(raw, b"" if method == "HEAD" else b"Method Not Allowed")

    def test_modern_request_headers_do_not_change_the_answer(self) -> None:
        status, headers, _ = self.request("PUT", "/mcp", headers=modern_headers("tools/list"), body=b"{}")
        self.assertEqual(status, 405)
        self.assertIn("POST", headers["allow"])

    def test_existing_405s_keep_their_allow_header(self) -> None:
        status, headers, _ = self.request("DELETE", "/mcp")
        self.assertEqual((status, headers["allow"]), (405, "POST, OPTIONS"))
        status, headers, _ = self.request("GET", self.path(MODERN))
        self.assertEqual((status, headers["allow"]), (405, "POST, OPTIONS"))

    def test_other_routes(self) -> None:
        status, headers, _ = self.request("PUT", "/healthz")
        self.assertEqual((status, headers["allow"]), (405, "GET, OPTIONS"))
        status, headers, _ = self.request("PUT", "/token", body=b"x=1")
        self.assertEqual((status, headers["allow"]), (405, "POST, OPTIONS"))
        status, _, _ = self.request("PUT", "/nope")
        self.assertEqual(status, 404)

    def test_server_keeps_serving_after_an_unsupported_method(self) -> None:
        self.request("PUT", "/mcp", body=b'{"jsonrpc":"2.0"}')
        status, _, raw = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertIs(json.loads(raw)["ok"], True)
