"""``discover.*`` and ``modernResponse.*`` injection on the modern path.

Each ``discover.mode`` maps to one branch of a dual-era client's (the gateway's)
server/discover classification: modern, not modern (with a reason), or auth
rejected.
"""

from __future__ import annotations

import time
import unittest
from http.client import RemoteDisconnected

from mcp_test_support import (
    CONVERT_ARGS,
    ERR_HEADER_MISMATCH,
    ERR_UNSUPPORTED_VERSION,
    META_SERVER,
    HttpServerTestCase,
    legacy_initialize,
    modern_call,
    modern_headers,
    modern_request,
    parse_sse,
)
from unit_expert_mcp.server import (
    DISCOVER_MODES,
    PROTOCOL_VERSION_2026,
    handle_json_rpc,
    inspect_snapshot,
    normalize_config,
)


def discover(config: dict | None = None, request_id: object = "discover-1"):
    return handle_json_rpc(
        modern_request("server/discover", request_id), config=config, headers=modern_headers("server/discover")
    )


def mode(name: str, **extra: object) -> dict:
    return {"discover": {"mode": name, **extra}}


class DiscoverModeTest(unittest.TestCase):
    def test_normal(self) -> None:
        status, _, payload = discover()
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])

    def test_no_modern_version(self) -> None:
        status, _, payload = discover(mode("no-modern-version"))
        self.assertEqual(status, 200)
        assert payload is not None
        versions = payload["result"]["supportedVersions"]
        self.assertTrue(versions)
        self.assertNotIn(PROTOCOL_VERSION_2026, versions)
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_empty_versions(self) -> None:
        _, _, payload = discover(mode("empty-versions"))
        assert payload is not None
        self.assertEqual(payload["result"]["supportedVersions"], [])

    def test_missing_versions(self) -> None:
        _, _, payload = discover(mode("missing-versions"))
        assert payload is not None
        self.assertNotIn("supportedVersions", payload["result"])
        self.assertIn("capabilities", payload["result"])

    def test_json_rpc_error(self) -> None:
        status, _, payload = discover(mode("json-rpc-error"))
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32603)
        self.assertEqual(payload["id"], "discover-1")
        _, _, payload = discover(mode("json-rpc-error", errorCode=-32601))
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32601)

    def test_header_mismatch(self) -> None:
        status, _, payload = discover(mode("header-mismatch"))
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_HEADER_MISMATCH)

    def test_unsupported_version(self) -> None:
        status, _, payload = discover(mode("unsupported-version"))
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_UNSUPPORTED_VERSION)
        data = payload["error"]["data"]
        self.assertEqual(data["requested"], PROTOCOL_VERSION_2026)
        self.assertNotIn(PROTOCOL_VERSION_2026, data["supported"])
        self.assertTrue(data["supported"])

    def test_id_mismatch(self) -> None:
        status, _, payload = discover(mode("id-mismatch"))
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertNotEqual(payload["id"], "discover-1")
        self.assertEqual(payload["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])
        _, _, payload = discover(mode("id-mismatch"), request_id=7)
        assert payload is not None
        self.assertIsInstance(payload["id"], int)
        self.assertNotEqual(payload["id"], 7)

    def test_injection_needs_a_valid_request(self) -> None:
        # An invalid discover still gets the protocol error, not the injected answer.
        status, _, payload = handle_json_rpc(
            modern_request("server/discover"),
            config=mode("no-modern-version"),
            headers={"Mcp-Method": "server/discover"},
        )
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_HEADER_MISMATCH)

    def test_other_methods_are_not_affected(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("tools/list"), config=mode("http-5xx"), headers=modern_headers("tools/list")
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("tools", payload["result"])

    def test_legacy_initialize_still_works_while_discover_fails(self) -> None:
        # This is the fallback a dual-era client takes after a failed probe.
        for name in DISCOVER_MODES:
            with self.subTest(mode=name):
                status, headers, payload = handle_json_rpc(legacy_initialize(), config=mode(name))
                self.assertEqual(status, 200)
                self.assertIn("Mcp-Session-Id", headers)
                assert payload is not None
                self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")

    def test_config_validation(self) -> None:
        for bad in (
            {"discover": {"mode": "nope"}},
            {"discover": {"httpStatus": 404}},
            {"discover": {"delaySeconds": -1}},
            {"discover": {"delaySeconds": 1000}},
            {"discover": {"errorCode": "x"}},
            {"discover": {"cacheScope": "shared"}},
            {"discover": "sse"},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class DiscoverHttpTest(HttpServerTestCase):
    def discover(self, config: dict | None = None, **kwargs):
        return self.modern_post("server/discover", config=config, **kwargs)

    def test_http_401_is_an_auth_challenge(self) -> None:
        status, headers, _, _ = self.discover(mode("http-401"))
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata", headers["www-authenticate"])

    def test_http_403(self) -> None:
        status, _, payload, raw = self.discover(mode("http-403"))
        self.assertEqual(status, 403)
        self.assertIsNone(payload)
        self.assertEqual(raw, b"Forbidden")

    def test_http_5xx(self) -> None:
        status, _, payload, _ = self.discover(mode("http-5xx"))
        self.assertEqual(status, 503)
        self.assertIsNone(payload)
        status, _, _, _ = self.discover(mode("http-5xx", httpStatus=500))
        self.assertEqual(status, 500)

    def test_header_mismatch_and_unsupported_version_bodies(self) -> None:
        status, _, payload, _ = self.discover(mode("header-mismatch"))
        self.assertEqual((status, payload["error"]["code"]), (400, ERR_HEADER_MISMATCH))
        status, _, payload, _ = self.discover(mode("unsupported-version"))
        self.assertEqual((status, payload["error"]["code"]), (400, ERR_UNSUPPORTED_VERSION))

    def test_sse_carries_a_notification_then_the_response(self) -> None:
        status, headers, _, raw = self.discover(mode("sse"))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        self.assertEqual(headers["x-accel-buffering"], "no")
        messages = parse_sse(raw)
        self.assertEqual(len(messages), 2)
        self.assertNotIn("id", messages[0])
        self.assertTrue(messages[0]["method"].startswith("notifications/"))
        self.assertEqual(messages[1]["id"], 1)
        self.assertEqual(messages[1]["result"]["supportedVersions"], [PROTOCOL_VERSION_2026])

    def test_sse_without_a_response(self) -> None:
        status, headers, _, raw = self.discover(mode("sse-no-response"))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers["content-type"])
        messages = parse_sse(raw)
        self.assertEqual(len(messages), 1)
        self.assertNotIn("id", messages[0])

    def test_invalid_json_body(self) -> None:
        status, headers, payload, raw = self.discover(mode("invalid-json"))
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        self.assertIsNone(payload)
        self.assertTrue(raw)

    def test_disconnect(self) -> None:
        with self.assertRaises((RemoteDisconnected, ConnectionError)):
            self.discover(mode("disconnect"))

    def test_delay(self) -> None:
        started = time.monotonic()
        status, _, payload, _ = self.discover({"discover": {"delaySeconds": 0.3}})
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(status, 200)
        self.assertIn("result", payload)

    def test_delay_can_outlast_a_client_timeout(self) -> None:
        with self.assertRaises(TimeoutError):
            self.discover({"discover": {"delaySeconds": 1.5}}, timeout=0.3)

    def test_inspect_log_names_the_injection(self) -> None:
        self.discover(mode("no-modern-version", delaySeconds=0.01))
        entry = inspect_snapshot()[0]
        self.assertEqual(entry["method"], "server/discover")
        self.assertEqual(entry["injected"], ["discover.mode=no-modern-version", "discover.delaySeconds=0.01"])

    def test_inspect_log_is_empty_for_a_normal_exchange(self) -> None:
        self.discover()
        self.assertEqual(inspect_snapshot()[0]["injected"], [])


class ModernResponseInjectionTest(unittest.TestCase):
    def results(self, injection: dict) -> dict[str, tuple]:
        config = {"modernResponse": injection}
        call_payload, call_headers = modern_call("convert_length", CONVERT_ARGS)
        return {
            "server/discover": discover(config),
            "tools/list": handle_json_rpc(
                modern_request("tools/list"), config=config, headers=modern_headers("tools/list")
            ),
            "tools/call": handle_json_rpc(call_payload, config=config, headers=call_headers),
        }

    def test_result_type_omitted(self) -> None:
        for method, (status, _, payload) in self.results({"resultType": "omit"}).items():
            with self.subTest(method=method):
                self.assertEqual(status, 200)
                self.assertNotIn("resultType", payload["result"])

    def test_result_type_input_required(self) -> None:
        for method, (_, _, payload) in self.results({"resultType": "input_required"}).items():
            with self.subTest(method=method):
                self.assertEqual(payload["result"]["resultType"], "input_required")
                self.assertIn("requestState", payload["result"])

    def test_result_type_unknown(self) -> None:
        for method, (_, _, payload) in self.results({"resultType": "unknown"}).items():
            with self.subTest(method=method):
                self.assertNotIn(payload["result"]["resultType"], {"complete", "input_required"})

    def test_server_info_omitted(self) -> None:
        for method, (_, _, payload) in self.results({"omitServerInfo": True}).items():
            with self.subTest(method=method):
                self.assertNotIn(META_SERVER, payload["result"].get("_meta", {}))
                self.assertEqual(payload["result"]["resultType"], "complete")

    def test_cache_fields_omitted(self) -> None:
        results = self.results({"omitCacheFields": True})
        for method in ("server/discover", "tools/list"):
            with self.subTest(method=method):
                result = results[method][2]["result"]
                self.assertNotIn("ttlMs", result)
                self.assertNotIn("cacheScope", result)

    def test_session_id_issued_on_modern_responses(self) -> None:
        for method, (_, headers, _) in self.results({"issueSessionId": True}).items():
            with self.subTest(method=method):
                self.assertTrue(headers["Mcp-Session-Id"])

    def test_methods_limits_the_scope(self) -> None:
        results = self.results({"resultType": "omit", "methods": ["tools/list"]})
        self.assertNotIn("resultType", results["tools/list"][2]["result"])
        self.assertEqual(results["server/discover"][2]["result"]["resultType"], "complete")
        self.assertEqual(results["tools/call"][2]["result"]["resultType"], "complete")

    def test_errors_and_legacy_responses_are_untouched(self) -> None:
        config = {"modernResponse": {"resultType": "omit", "issueSessionId": True, "omitServerInfo": True}}
        status, headers, payload = handle_json_rpc(
            modern_request("ping"), config=config, headers=modern_headers("ping")
        )
        self.assertEqual(status, 404)
        self.assertNotIn("Mcp-Session-Id", headers)
        status, headers, payload = handle_json_rpc(legacy_initialize(), config=config)
        assert payload is not None
        self.assertEqual(payload["result"]["serverInfo"]["name"], "unitExpert")

    def test_defaults_inject_nothing(self) -> None:
        for method, (_, headers, payload) in self.results({}).items():
            with self.subTest(method=method):
                self.assertNotIn("Mcp-Session-Id", headers)
                self.assertEqual(payload["result"]["resultType"], "complete")
                self.assertIn(META_SERVER, payload["result"]["_meta"])

    def test_config_validation(self) -> None:
        for bad in (
            {"modernResponse": {"resultType": "partial"}},
            {"modernResponse": {"omitServerInfo": "yes"}},
            {"modernResponse": {"methods": ["initialize"]}},
            {"modernResponse": {"methods": "tools/list"}},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class ModernResponseHttpTest(HttpServerTestCase):
    def test_session_id_header_on_the_wire(self) -> None:
        status, headers, _, _ = self.modern_post(
            "tools/list", config={"modernResponse": {"issueSessionId": True}}
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["mcp-session-id"])
        entry = inspect_snapshot()[0]
        self.assertEqual(entry["injected"], ["modernResponse.issueSessionId"])
        self.assertIn("Mcp-Session-Id", entry["responseHeaders"])

    def test_sse_discover_combined_with_result_type_violation(self) -> None:
        config = {"discover": {"mode": "sse"}, "modernResponse": {"resultType": "omit"}}
        _, _, _, raw = self.modern_post("server/discover", config=config)
        self.assertNotIn("resultType", parse_sse(raw)[-1]["result"])


if __name__ == "__main__":
    unittest.main()
