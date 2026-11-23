"""``legacyModernProbe``: what a legacy-only server answers to a modern request.

Only read when ``serverEra`` is ``legacy``. Transport-level modes (http-400,
http-404, empty-400, timeout) answer before the legacy Accept rule; the
JSON-RPC-level modes (method-not-found, invalid-params) run after it, exactly
where a real legacy server would produce them.
"""

from __future__ import annotations

import time
import unittest
from http.client import RemoteDisconnected

from mcp_test_support import (
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_headers,
    modern_request,
)
from unit_expert_mcp.server import (
    LEGACY_MODERN_PROBE_MODES,
    PROTOCOL_VERSION_2026,
    handle_json_rpc,
    inspect_snapshot,
    normalize_config,
)


SDK1_UNKNOWN_METHOD_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "error": {"code": -32602, "message": "Invalid request parameters", "data": ""},
}


def legacy(mode: str | None = None, **extra: object) -> dict:
    config: dict = {"serverEra": "legacy"}
    if mode is not None:
        config["legacyModernProbe"] = {"mode": mode, **extra}
    return config


def probe(config: dict, method: str = "server/discover"):
    return handle_json_rpc(modern_request(method), config=config, headers=modern_headers(method))


class LegacyProbeDispatchTest(unittest.TestCase):
    def test_default_mode_is_method_not_found(self) -> None:
        self.assertEqual(normalize_config({})["legacyModernProbe"]["mode"], "method-not-found")
        for config in (legacy(), legacy("method-not-found")):
            status, _, payload = probe(config)
            self.assertEqual(status, 200)
            assert payload is not None
            self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)

    def test_default_mode_serves_a_modern_tools_list_in_legacy_shape(self) -> None:
        status, _, payload = probe(legacy(), "tools/list")
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertNotIn("resultType", payload["result"])

    def test_invalid_params(self) -> None:
        status, _, payload = probe(legacy("invalid-params"))
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_PARAMS)
        self.assertEqual(payload["id"], 1)
        # Known methods are still served.
        status, _, payload = probe(legacy("invalid-params"), "tools/list")
        assert payload is not None
        self.assertIn("tools", payload["result"])

    def test_http_400_keys_on_the_version_header(self) -> None:
        status, _, payload = probe(legacy("http-400"))
        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], ERR_INVALID_REQUEST)
        self.assertIn(PROTOCOL_VERSION_2026, payload["error"]["message"])
        # Not one of the modern error codes a dual-era client must recognise.
        self.assertNotIn(payload["error"]["code"], {-32020, -32021, -32022})
        # tools/list with the 2026 header is refused the same way.
        status, _, _ = probe(legacy("http-400"), "tools/list")
        self.assertEqual(status, 400)

    def test_http_400_without_the_version_header_falls_through(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("server/discover"),
            config=legacy("http-400"),
            headers={"Mcp-Method": "server/discover"},
        )
        # Python SDK 1.27.2 (stateless): no version header, unknown method.
        self.assertEqual(status, 200)
        self.assertEqual(payload, SDK1_UNKNOWN_METHOD_BODY)

    def test_bodies_equal_the_python_sdk_1x_responses(self) -> None:
        """Observed on mcp 1.27.2 (scripts/sdk1_legacy_server.py), 2026-11-23."""
        _, _, payload = probe(legacy("invalid-params"))
        self.assertEqual(payload, SDK1_UNKNOWN_METHOD_BODY)
        _, _, payload = probe(legacy("http-400"))
        self.assertEqual(
            payload,
            {
                "jsonrpc": "2.0",
                "id": "server-error",
                "error": {
                    "code": -32600,
                    "message": "Bad Request: Unsupported protocol version: 2026-07-28. "
                    "Supported versions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25",
                },
            },
        )

    def test_http_404_has_no_json_rpc_body(self) -> None:
        status, _, payload = probe(legacy("http-404"))
        self.assertEqual(status, 404)
        self.assertIsNone(payload)

    def test_empty_400_has_no_body(self) -> None:
        status, _, payload = probe(legacy("empty-400"))
        self.assertEqual(status, 400)
        self.assertIsNone(payload)

    def test_server_discover_without_markers_counts_as_a_probe(self) -> None:
        status, _, payload = handle_json_rpc(legacy_request("server/discover"), config=legacy("http-404"))
        self.assertEqual(status, 404)
        self.assertIsNone(payload)

    def test_legacy_handshake_is_never_affected(self) -> None:
        for mode in LEGACY_MODERN_PROBE_MODES:
            with self.subTest(mode=mode):
                status, headers, payload = handle_json_rpc(legacy_initialize(), config=legacy(mode))
                self.assertEqual(status, 200)
                self.assertIn("Mcp-Session-Id", headers)
                status, _, payload = handle_json_rpc(
                    legacy_request("tools/list"),
                    config=legacy(mode),
                    headers={"MCP-Protocol-Version": "2025-03-26"},
                )
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertEqual(len(payload["result"]["tools"]), 6)

    def test_option_is_ignored_unless_server_era_is_legacy(self) -> None:
        for era in ("both", "modern"):
            with self.subTest(era=era):
                config = {"serverEra": era, "legacyModernProbe": {"mode": "http-404"}}
                status, _, payload = probe(config)
                self.assertEqual(status, 200)
                assert payload is not None
                self.assertIn("supportedVersions", payload["result"])

    def test_config_validation(self) -> None:
        for bad in (
            {"legacyModernProbe": {"mode": "http-500"}},
            {"legacyModernProbe": "http-400"},
            {"legacyModernProbe": {"delaySeconds": 500}},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class LegacyProbeHttpTest(HttpServerTestCase):
    JSON_ONLY = "application/json"

    def probe(self, config: dict, method: str = "server/discover", **kwargs):
        return self.modern_post(method, config=config, **kwargs)

    def test_method_not_found_on_the_wire(self) -> None:
        status, headers, payload, _ = self.probe(legacy())
        self.assertEqual(status, 200)
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)
        self.assertNotIn("mcp-session-id", headers)

    def test_json_level_modes_keep_the_legacy_accept_rule(self) -> None:
        # A real legacy server checks Accept before it looks at the method.
        for mode in ("method-not-found", "invalid-params"):
            with self.subTest(mode=mode):
                status, _, _, raw = self.probe(legacy(mode), accept=self.JSON_ONLY)
                self.assertEqual(status, 400)
                self.assertIn(b"Invalid Accept headers", raw)

    def test_transport_level_modes_answer_regardless_of_accept(self) -> None:
        expected = {"http-400": 400, "http-404": 404, "empty-400": 400}
        for mode, status_code in expected.items():
            for accept in (self.JSON_ONLY, "application/json, text/event-stream"):
                with self.subTest(mode=mode, accept=accept):
                    status, _, _, raw = self.probe(legacy(mode), accept=accept)
                    self.assertEqual(status, status_code)
                    self.assertNotIn(b"Invalid Accept headers", raw)

    def test_http_400_body(self) -> None:
        status, headers, payload, _ = self.probe(legacy("http-400"))
        self.assertEqual(status, 400)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(payload["id"], "server-error")
        self.assertEqual(payload["error"]["code"], ERR_INVALID_REQUEST)

    def test_http_404_body_is_not_json_rpc(self) -> None:
        status, _, payload, raw = self.probe(legacy("http-404"))
        self.assertEqual(status, 404)
        self.assertIsNone(payload)
        self.assertEqual(raw, b"Not Found")

    def test_empty_400_has_zero_length_body(self) -> None:
        status, headers, _, raw = self.probe(legacy("empty-400"))
        self.assertEqual(status, 400)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["content-length"], "0")

    def test_timeout_stays_silent_then_drops_the_connection(self) -> None:
        started = time.monotonic()
        with self.assertRaises((RemoteDisconnected, ConnectionError)):
            self.probe(legacy("timeout", delaySeconds=0.3))
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_timeout_outlasts_a_client_read_timeout(self) -> None:
        with self.assertRaises(TimeoutError):
            self.probe(legacy("timeout", delaySeconds=2), timeout=0.3)

    def test_fallback_initialize_succeeds_after_a_failed_probe(self) -> None:
        for mode in ("http-400", "http-404", "empty-400", "invalid-params"):
            with self.subTest(mode=mode):
                # Gateway-style initialize: no version header, both media types.
                status, headers, payload, _ = self.post(legacy_initialize(), config=legacy(mode))
                self.assertEqual(status, 200)
                self.assertTrue(headers["mcp-session-id"])
                self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")

    def test_inspect_log_names_the_mode(self) -> None:
        self.probe(legacy("http-404"))
        entry = inspect_snapshot()[0]
        self.assertEqual(entry["injected"], ["legacyModernProbe.mode=http-404"])
        self.assertEqual(entry["serverEra"], "legacy")
        self.assertEqual(entry["status"], 404)

    def test_via_header_scenario(self) -> None:
        status, _, payload, _ = self.post(
            modern_request("server/discover"),
            headers={**modern_headers("server/discover"), "X-MCP-Test-Scenario": "legacy-probe-http-404"},
        )
        self.assertEqual(status, 404)
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
