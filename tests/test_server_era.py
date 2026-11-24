"""Tests for the ``serverEra`` config ("both" | "legacy" | "modern").

``both`` is the default and must behave exactly like the server did before the
option existed. ``legacy`` makes the server act like one that predates
2026-07-28 (2026 markers ignored, server/discover unknown). ``modern`` is the
2026-only server previously reachable through ``rejectLegacy``.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from unit_expert_mcp.server import (
    DEFAULT_CONFIG,
    PROTOCOL_VERSION_2026,
    Handler,
    config_server_era,
    decode_config_token,
    default_config,
    encode_config_token,
    handle_json_rpc,
    inspect_snapshot,
    normalize_config,
    render_scenario_page,
    scenario_to_config,
    set_active_scenario,
)

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"
LEGACY_ACCEPT = "application/json, text/event-stream"
ERR_METHOD_NOT_FOUND = -32601
ERR_UNSUPPORTED_VERSION = -32022


def modern_request(method: str, request_id: object = 1, **params: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": {META_VERSION: PROTOCOL_VERSION_2026, META_CAPS: {}}},
    }


def modern_headers(method: str, **extra: str) -> dict[str, str]:
    return {"MCP-Protocol-Version": PROTOCOL_VERSION_2026, "Mcp-Method": method, **extra}


def legacy_initialize(request_id: object = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }


def legacy_request(method: str, request_id: object = 2, **params: object) -> dict:
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        payload["params"] = params
    return payload


CALL_ARGS = {"name": "convert_length", "arguments": {"value": 1, "from_unit": "m", "to_unit": "cm"}}


class ServerEraConfigTest(unittest.TestCase):
    def test_default_is_both(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["serverEra"], "both")
        self.assertEqual(default_config()["serverEra"], "both")
        self.assertEqual(normalize_config({})["serverEra"], "both")
        self.assertIs(normalize_config({})["rejectLegacy"], False)

    def test_each_value_is_accepted(self) -> None:
        for era in ("both", "legacy", "modern"):
            with self.subTest(era=era):
                self.assertEqual(normalize_config({"serverEra": era})["serverEra"], era)

    def test_unknown_value_is_rejected(self) -> None:
        for value in ("2026", "Legacy", "", None, 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "serverEra"):
                    normalize_config({"serverEra": value})

    def test_modern_and_reject_legacy_are_kept_in_step(self) -> None:
        self.assertIs(normalize_config({"serverEra": "modern"})["rejectLegacy"], True)
        self.assertIs(normalize_config({"serverEra": "legacy"})["rejectLegacy"], False)
        self.assertIs(normalize_config({"serverEra": "both"})["rejectLegacy"], False)

    def test_reject_legacy_alone_is_an_alias_for_modern(self) -> None:
        config = normalize_config({"rejectLegacy": True})

        self.assertEqual(config["serverEra"], "modern")
        self.assertIs(config["rejectLegacy"], True)

    def test_conflict_explicit_server_era_wins_except_default_both(self) -> None:
        legacy = normalize_config({"serverEra": "legacy", "rejectLegacy": True})
        modern = normalize_config({"serverEra": "modern", "rejectLegacy": False})
        both = normalize_config({"serverEra": "both", "rejectLegacy": True})

        self.assertEqual((legacy["serverEra"], legacy["rejectLegacy"]), ("legacy", False))
        self.assertEqual((modern["serverEra"], modern["rejectLegacy"]), ("modern", True))
        # "both" is the default, so the alias still promotes it.
        self.assertEqual((both["serverEra"], both["rejectLegacy"]), ("modern", True))

    def test_config_server_era_helper(self) -> None:
        self.assertEqual(config_server_era(None), "both")
        self.assertEqual(config_server_era({}), "both")
        self.assertEqual(config_server_era({"rejectLegacy": True}), "modern")
        self.assertEqual(config_server_era({"serverEra": "legacy"}), "legacy")

    def test_cfg_token_round_trip(self) -> None:
        self.assertEqual(encode_config_token(default_config()), "")
        for era in ("legacy", "modern"):
            with self.subTest(era=era):
                decoded = decode_config_token(encode_config_token({"serverEra": era}))
                self.assertEqual(decoded["serverEra"], era)

    def test_old_reject_legacy_token_still_means_modern(self) -> None:
        token = base64.urlsafe_b64encode(b'{"protocolEra":"2026","rejectLegacy":true}')
        decoded = decode_config_token(token.decode("ascii").rstrip("="))

        self.assertEqual(decoded["serverEra"], "modern")
        self.assertIs(decoded["rejectLegacy"], True)

    def test_header_scenarios_select_the_era(self) -> None:
        self.assertEqual(scenario_to_config("server-era-legacy")["serverEra"], "legacy")
        modern = scenario_to_config("server-era-modern")
        self.assertEqual((modern["serverEra"], modern["rejectLegacy"]), ("modern", True))
        self.assertEqual(scenario_to_config("ok")["serverEra"], "both")

    def test_scenario_page_exposes_the_control(self) -> None:
        page = render_scenario_page()

        self.assertIn('<select id="serverEra">', page)
        for era in ("both", "legacy", "modern"):
            self.assertIn(f'<option value="{era}">', page)
        self.assertIn('serverEra: document.getElementById("serverEra").value', page)


class ServerEraDispatchTest(unittest.TestCase):
    """handle_json_rpc behaviour per mode (no HTTP layer)."""

    def call(self, era: str | None, payload: dict, headers: dict[str, str] | None = None):
        config = None if era is None else {"serverEra": era}
        return handle_json_rpc(payload, config=config, headers=headers)

    # -- both (default) ---------------------------------------------------------
    def test_both_matches_the_behaviour_without_the_option(self) -> None:
        cases = [
            (modern_request("server/discover"), modern_headers("server/discover")),
            (modern_request("tools/list"), modern_headers("tools/list")),
            (
                modern_request("tools/call", **CALL_ARGS),
                modern_headers("tools/call", **{"Mcp-Name": "convert_length"}),
            ),
            (legacy_request("tools/list"), None),
            (legacy_request("tools/call", **CALL_ARGS), None),
            (legacy_request("server/discover"), None),
        ]
        for payload, headers in cases:
            with self.subTest(method=payload["method"], modern=headers is not None):
                self.assertEqual(
                    self.call("both", payload, headers),
                    handle_json_rpc(payload, headers=headers),
                )

    def test_both_serves_server_discover(self) -> None:
        status, _, payload = self.call(
            "both", modern_request("server/discover"), modern_headers("server/discover")
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_both_serves_modern_tools_list(self) -> None:
        status, _, payload = self.call("both", modern_request("tools/list"), modern_headers("tools/list"))

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["resultType"], "complete")
        self.assertIn(META_SERVER, payload["result"]["_meta"])
        # 6 converters + the 3 modern-only test tools (echo_header_params, confirm_action,
        # convert_interactive).
        self.assertEqual(len(payload["result"]["tools"]), 9)

    def test_both_serves_legacy_initialize_and_tools_list(self) -> None:
        status, headers, payload = self.call("both", legacy_initialize())
        list_status, _, listed = self.call("both", legacy_request("tools/list"))

        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        assert payload is not None and listed is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(list_status, 200)
        self.assertEqual(len(listed["result"]["tools"]), 6)
        self.assertNotIn("resultType", listed["result"])

    # -- legacy -----------------------------------------------------------------
    def test_legacy_server_discover_is_method_not_found(self) -> None:
        status, headers, payload = self.call(
            "legacy", modern_request("server/discover", 9), modern_headers("server/discover")
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers, {})
        assert payload is not None
        self.assertEqual(payload["id"], 9)
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)
        self.assertNotIn("result", payload)

    def test_legacy_server_discover_without_markers_is_method_not_found(self) -> None:
        status, _, payload = self.call("legacy", legacy_request("server/discover"))

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)

    def test_legacy_answers_modern_tools_list_in_legacy_shape(self) -> None:
        status, _, payload = self.call(
            "legacy", modern_request("tools/list"), modern_headers("tools/list")
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(len(payload["result"]["tools"]), 6)
        for modern_only in ("resultType", "_meta", "ttlMs", "cacheScope"):
            self.assertNotIn(modern_only, payload["result"])

    def test_legacy_skips_2026_envelope_validation(self) -> None:
        # Mcp-Method that disagrees with the body is a 400 on the 2026 path; a
        # legacy server never looks at it.
        payload = modern_request("tools/list")
        mismatched = modern_headers("tools/call")

        both_status, _, _ = self.call("both", payload, mismatched)
        legacy_status, _, legacy_payload = self.call("legacy", payload, mismatched)

        self.assertEqual(both_status, 400)
        self.assertEqual(legacy_status, 200)
        assert legacy_payload is not None
        self.assertIn("tools", legacy_payload["result"])

    def test_legacy_handshake_flow_works(self) -> None:
        status, headers, initialized = self.call("legacy", legacy_initialize())
        notify_status, _, notify_body = self.call(
            "legacy", {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        list_status, _, listed = self.call("legacy", legacy_request("tools/list"))
        call_status, _, called = self.call("legacy", legacy_request("tools/call", 3, **CALL_ARGS))

        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        assert initialized is not None and listed is not None and called is not None
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertNotIn("resultType", initialized["result"])
        self.assertEqual((notify_status, notify_body), (202, None))
        self.assertEqual(list_status, 200)
        self.assertEqual(len(listed["result"]["tools"]), 6)
        self.assertEqual(call_status, 200)
        self.assertEqual(called["result"]["content"][0]["text"], "1 m = 100 cm")

    def test_legacy_initialize_with_2026_markers_starts_a_legacy_session(self) -> None:
        status, headers, payload = self.call(
            "legacy", modern_request("initialize"), modern_headers("initialize")
        )

        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        assert payload is not None
        self.assertNotEqual(payload["result"]["protocolVersion"], PROTOCOL_VERSION_2026)
        self.assertNotIn("resultType", payload["result"])

    def test_legacy_wins_over_reject_legacy(self) -> None:
        status, _, payload = handle_json_rpc(
            legacy_request("tools/list"), config={"serverEra": "legacy", "rejectLegacy": True}
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("tools", payload["result"])

    # -- modern -----------------------------------------------------------------
    def test_modern_serves_server_discover_and_tools_list(self) -> None:
        discover_status, _, discovered = self.call(
            "modern", modern_request("server/discover"), modern_headers("server/discover")
        )
        list_status, _, listed = self.call(
            "modern", modern_request("tools/list"), modern_headers("tools/list")
        )

        assert discovered is not None and listed is not None
        self.assertEqual(discover_status, 200)
        self.assertEqual(discovered["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["result"]["resultType"], "complete")

    def test_modern_rejects_legacy_initialize_and_tools_list(self) -> None:
        for payload in (legacy_initialize(), legacy_request("tools/list")):
            with self.subTest(method=payload["method"]):
                status, headers, response = self.call("modern", payload)

                self.assertEqual(status, 400)
                self.assertNotIn("Mcp-Session-Id", headers)
                assert response is not None
                self.assertEqual(response["error"]["code"], ERR_UNSUPPORTED_VERSION)

    def test_modern_equals_reject_legacy(self) -> None:
        cases = [
            (modern_request("server/discover"), modern_headers("server/discover")),
            (modern_request("tools/list"), modern_headers("tools/list")),
            (legacy_request("tools/list"), None),
            (legacy_request("server/discover"), None),
        ]
        for payload, headers in cases:
            with self.subTest(method=payload["method"], modern=headers is not None):
                self.assertEqual(
                    self.call("modern", payload, headers),
                    handle_json_rpc(payload, config={"rejectLegacy": True}, headers=headers),
                )


class ServerEraHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def tearDown(self) -> None:
        set_active_scenario("ok")

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        if method == "GET" and response.status == 200:
            # The legacy GET stream has no Content-Length and stays open.
            raw = response.read1(64)
        else:
            raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        conn.close()
        return response.status, response_headers, raw

    def path(self, era: str | None) -> str:
        if era is None:
            return "/mcp"
        return f"/mcp?cfg={encode_config_token({'serverEra': era})}"

    def post(
        self,
        era: str | None,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
        accept: str = LEGACY_ACCEPT,
    ) -> tuple[int, dict[str, str], dict | None, bytes]:
        request_headers = {"Content-Type": "application/json", "Accept": accept}
        request_headers.update(headers or {})
        status, response_headers, raw = self.request(
            "POST", self.path(era), headers=request_headers, body=json.dumps(payload).encode("utf-8")
        )
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return status, response_headers, parsed, raw

    def modern_post(self, era: str | None, method: str, **kwargs):
        return self.post(era, modern_request(method), headers=modern_headers(method), **kwargs)

    # -- default / both ---------------------------------------------------------
    def test_default_and_both_are_identical_on_the_wire(self) -> None:
        for era in (None, "both"):
            with self.subTest(era=era):
                discover_status, _, discovered, _ = self.modern_post(era, "server/discover")
                list_status, _, listed, _ = self.modern_post(era, "tools/list", accept="application/json")
                init_status, init_headers, initialized, _ = self.post(era, legacy_initialize())
                legacy_list_status, _, legacy_listed, _ = self.post(era, legacy_request("tools/list"))
                get_status, get_headers, _ = self.request(
                    "GET", self.path(era), headers=modern_headers("tools/list", Accept="text/event-stream")
                )

                assert discovered and listed and initialized and legacy_listed
                self.assertEqual(discover_status, 200)
                self.assertEqual(discovered["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])
                self.assertEqual(list_status, 200)
                self.assertEqual(listed["result"]["resultType"], "complete")
                self.assertEqual(init_status, 200)
                self.assertIn("mcp-session-id", init_headers)
                self.assertEqual(legacy_list_status, 200)
                self.assertNotIn("resultType", legacy_listed["result"])
                self.assertEqual(get_status, 405)
                self.assertEqual(get_headers["allow"], "POST, OPTIONS")

    # -- legacy -----------------------------------------------------------------
    def test_legacy_server_discover_is_method_not_found(self) -> None:
        status, headers, payload, _ = self.modern_post("legacy", "server/discover")

        self.assertEqual(status, 200)
        self.assertNotIn("mcp-session-id", headers)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)

    def test_legacy_modern_tools_list_takes_the_legacy_path(self) -> None:
        status, _, payload, _ = self.modern_post("legacy", "tools/list")

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(len(payload["result"]["tools"]), 6)
        self.assertNotIn("resultType", payload["result"])

    def test_legacy_applies_the_legacy_accept_rule_to_modern_requests(self) -> None:
        # A 2026 client may send Accept: application/json only; a legacy server
        # insists on both media types.
        status, _, payload, raw = self.modern_post("legacy", "server/discover", accept="application/json")

        self.assertEqual(status, 400)
        self.assertIsNone(payload)
        self.assertIn(b"Invalid Accept headers", raw)

    def test_legacy_handshake_flow_works(self) -> None:
        init_status, init_headers, initialized, _ = self.post("legacy", legacy_initialize())
        session = {"Mcp-Session-Id": init_headers.get("mcp-session-id", "")}
        notify_status, _, _, notify_raw = self.post(
            "legacy", {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session
        )
        list_status, _, listed, _ = self.post("legacy", legacy_request("tools/list"), headers=session)
        call_status, _, called, _ = self.post(
            "legacy", legacy_request("tools/call", 3, **CALL_ARGS), headers=session
        )

        assert initialized and listed and called
        self.assertEqual(init_status, 200)
        self.assertTrue(session["Mcp-Session-Id"])
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual((notify_status, notify_raw), (202, b""))
        self.assertEqual(list_status, 200)
        self.assertEqual(len(listed["result"]["tools"]), 6)
        self.assertEqual(call_status, 200)
        self.assertEqual(called["result"]["content"][0]["text"], "1 m = 100 cm")

    def test_legacy_get_with_2026_markers_is_not_405(self) -> None:
        status, headers, raw = self.request(
            "GET",
            self.path("legacy"),
            headers=modern_headers("tools/list", Accept="text/event-stream", **{"Mcp-Session-Id": "s"}),
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertTrue(raw.startswith(b": connected"))

    def test_legacy_get_keeps_legacy_validation(self) -> None:
        status, _, raw = self.request(
            "GET", self.path("legacy"), headers=modern_headers("tools/list", Accept="text/event-stream")
        )

        self.assertEqual(status, 400)
        self.assertIn(b"Session ID required", raw)

    def test_legacy_subscriptions_listen_is_method_not_found(self) -> None:
        status, headers, payload, _ = self.modern_post("legacy", "subscriptions/listen")

        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"].split(";")[0], "application/json")
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)

    def test_legacy_via_test_scenario_header(self) -> None:
        scenario = {"X-MCP-Test-Scenario": "server-era-legacy"}

        status, _, payload, _ = self.post(
            None,
            modern_request("server/discover"),
            headers={**modern_headers("server/discover"), **scenario},
        )
        init_status, init_headers, _, _ = self.post(None, legacy_initialize(), headers=scenario)

        assert payload is not None
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)
        self.assertEqual(init_status, 200)
        self.assertIn("mcp-session-id", init_headers)

    def test_legacy_via_scenario_endpoint_active_config(self) -> None:
        body = json.dumps({"config": {"serverEra": "legacy"}}).encode("utf-8")

        status, _, raw = self.request(
            "POST", "/scenario", headers={"Content-Type": "application/json"}, body=body
        )
        discover_status, _, discovered, _ = self.modern_post(None, "server/discover")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["config"]["serverEra"], "legacy")
        assert discovered is not None
        self.assertEqual(discover_status, 200)
        self.assertEqual(discovered["error"]["code"], ERR_METHOD_NOT_FOUND)

    def test_scenario_endpoint_rejects_unknown_era(self) -> None:
        body = json.dumps({"config": {"serverEra": "future"}}).encode("utf-8")

        status, _, raw = self.request(
            "POST", "/scenario", headers={"Content-Type": "application/json"}, body=body
        )

        self.assertEqual(status, 400)
        self.assertIn("serverEra", json.loads(raw)["error"])

    # -- modern -----------------------------------------------------------------
    def test_modern_serves_2026_and_rejects_legacy(self) -> None:
        discover_status, _, discovered, _ = self.modern_post("modern", "server/discover")
        list_status, _, listed, _ = self.modern_post("modern", "tools/list", accept="application/json")
        init_status, init_headers, initialized, _ = self.post("modern", legacy_initialize())
        legacy_list_status, _, legacy_listed, _ = self.post("modern", legacy_request("tools/list"))
        get_status, _, _ = self.request(
            "GET", self.path("modern"), headers=modern_headers("tools/list", Accept="text/event-stream")
        )

        assert discovered and listed and initialized and legacy_listed
        self.assertEqual(discover_status, 200)
        self.assertEqual(discovered["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])
        self.assertEqual(list_status, 200)
        self.assertEqual(listed["result"]["resultType"], "complete")
        self.assertEqual(init_status, 400)
        self.assertNotIn("mcp-session-id", init_headers)
        self.assertEqual(initialized["error"]["code"], ERR_UNSUPPORTED_VERSION)
        self.assertEqual(legacy_list_status, 400)
        self.assertEqual(legacy_listed["error"]["code"], ERR_UNSUPPORTED_VERSION)
        self.assertEqual(get_status, 405)

    def test_modern_via_test_scenario_header(self) -> None:
        status, _, payload, _ = self.post(
            None, legacy_initialize(), headers={"X-MCP-Test-Scenario": "server-era-modern"}
        )

        assert payload is not None
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], ERR_UNSUPPORTED_VERSION)

    # -- observability ----------------------------------------------------------
    def test_inspect_log_records_the_era_mode(self) -> None:
        marker = f"era-inspect-{time.monotonic_ns()}"

        self.post(
            "legacy", modern_request("server/discover", marker), headers=modern_headers("server/discover")
        )
        self.post(None, legacy_request("tools/list", marker + "-default"))
        entries = {
            item["requestBody"]["id"]: item
            for item in inspect_snapshot()
            if isinstance(item["requestBody"], dict) and "id" in item["requestBody"]
        }

        # "era" is what the request looked like; "serverEra" is how it was served.
        self.assertEqual(entries[marker]["era"], "2026")
        self.assertEqual(entries[marker]["serverEra"], "legacy")
        self.assertEqual(entries[marker + "-default"]["serverEra"], "both")


if __name__ == "__main__":
    unittest.main()
