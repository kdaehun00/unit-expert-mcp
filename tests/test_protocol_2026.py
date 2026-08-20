from __future__ import annotations

import io
import json
import unittest

from unit_expert_mcp.server import (
    Handler,
    META_SUBSCRIPTION_ID,
    PROTOCOL_VERSION_2026,
    SERVER_NAME,
    SERVER_VERSION,
    SUBSCRIPTIONS_ACKNOWLEDGED_METHOD,
    handle_json_rpc,
    subscriptions_acknowledged_notification,
)

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"


def meta(version: str = PROTOCOL_VERSION_2026, *, caps: bool = True) -> dict:
    envelope = {META_VERSION: version}
    if caps:
        envelope[META_CAPS] = {}
    return envelope


def headers(method: str, *, version: str = PROTOCOL_VERSION_2026, **extra: str) -> dict:
    base = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    base.update(extra)
    return base


class Protocol2026Test(unittest.TestCase):
    def test_initialize_is_stateless_and_wraps_result(self) -> None:
        status, response_headers, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"_meta": meta()},
            },
            headers=headers("initialize"),
        )

        self.assertEqual(status, 200)
        self.assertNotIn("Mcp-Session-Id", response_headers)
        assert payload is not None
        result = payload["result"]
        self.assertEqual(result["protocolVersion"], PROTOCOL_VERSION_2026)
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(
            result["_meta"][META_SERVER],
            {"name": SERVER_NAME, "version": SERVER_VERSION},
        )

    def test_tools_list_carries_result_type_and_server_info(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": meta()},
            },
            headers=headers("tools/list"),
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["resultType"], "complete")
        self.assertEqual(len(payload["result"]["tools"]), 6)
        self.assertIn(META_SERVER, payload["result"]["_meta"])

    def test_tools_list_can_advertise_positive_cache_ttl(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/list",
                "params": {"_meta": meta()},
            },
            config={
                "protocolEra": "2026",
                "toolsList": {
                    "cacheTtlMs": 60000,
                    "cacheScope": "private",
                },
            },
            headers=headers("tools/list"),
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["ttlMs"], 60000)
        self.assertEqual(payload["result"]["cacheScope"], "private")

    def test_tools_call_executes_with_name_header(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "convert_length",
                    "arguments": {"value": 1, "from_unit": "m", "to_unit": "cm"},
                    "_meta": meta(),
                },
            },
            headers=headers("tools/call", **{"Mcp-Name": "convert_length"}),
        )

        self.assertEqual(status, 200)
        assert payload is not None
        self.assertEqual(payload["result"]["content"][0]["text"], "1 m = 100 cm")
        self.assertEqual(payload["result"]["resultType"], "complete")

    def test_missing_meta_protocol_version_is_invalid_params(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {"_meta": {META_CAPS: {}}},
            },
            headers=headers("tools/list"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32602)

    def test_missing_client_capabilities_field_is_invalid_params(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": {"_meta": meta(caps=False)},
            },
            headers=headers("tools/list"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32602)

    def test_method_header_body_mismatch_is_header_mismatch(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/list",
                "params": {"_meta": meta()},
            },
            headers=headers("tools/call"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32020)

    def test_missing_name_header_for_tools_call_is_header_mismatch(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "convert_length",
                    "arguments": {},
                    "_meta": meta(),
                },
            },
            headers=headers("tools/call"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32020)

    def test_unsupported_version_is_reported_with_supported_list(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
                "params": {"_meta": meta("2099-01-01")},
            },
            headers=headers("tools/list", version="2099-01-01"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32022)

    def test_legacy_request_is_untouched_by_2026_path(self) -> None:
        # No Mcp-Method header and no _meta envelope -> legacy 2025 path.
        status, response_headers, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", response_headers)
        assert payload is not None
        self.assertNotIn("resultType", payload["result"])

    def test_subscriptions_listen_requires_notifications_filter(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": "sub-1",
                "method": "subscriptions/listen",
                "params": {"_meta": meta()},
            },
            headers=headers("subscriptions/listen"),
        )

        self.assertEqual(status, 400)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32602)

    def test_subscriptions_listen_sends_acknowledged_sse_first(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": "sub-1",
            "method": "subscriptions/listen",
            "params": {
                "notifications": {
                    "toolsListChanged": True,
                    "promptsListChanged": True,
                },
                "_meta": meta(),
            },
        }
        status, _, final_payload = handle_json_rpc(
            payload,
            headers=headers("subscriptions/listen"),
        )
        self.assertEqual(status, 200)
        assert final_payload is not None

        class FakeHandler:
            headers: dict[str, str] = {}
            wfile = io.BytesIO()
            close_connection = False
            sent_headers: dict[str, str] = {}

            def send_response(self, status: int) -> None:
                self.status = status

            def send_cors_headers(self) -> None:
                pass

            def send_header(self, name: str, value: str) -> None:
                self.sent_headers[name] = value

            def end_headers(self) -> None:
                pass

        fake = FakeHandler()
        Handler.send_sse_json_rpc(
            fake,
            200,
            [
                subscriptions_acknowledged_notification(payload),
                final_payload,
            ],
        )
        body = fake.wfile.getvalue().decode("utf-8")

        self.assertEqual(fake.status, 200)
        self.assertEqual(fake.sent_headers["Content-Type"], "text/event-stream")
        self.assertIs(fake.close_connection, True)
        data_lines = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
        self.assertGreaterEqual(len(data_lines), 2)
        ack = json.loads(data_lines[0])
        final_result = json.loads(data_lines[1])

        self.assertEqual(ack["method"], SUBSCRIPTIONS_ACKNOWLEDGED_METHOD)
        self.assertEqual(ack["params"]["notifications"], {"toolsListChanged": True})
        self.assertEqual(ack["params"]["_meta"][META_SUBSCRIPTION_ID], "sub-1")
        self.assertEqual(final_result["id"], "sub-1")
        self.assertEqual(final_result["result"]["resultType"], "complete")
        self.assertEqual(final_result["result"]["_meta"][META_SUBSCRIPTION_ID], "sub-1")


if __name__ == "__main__":
    unittest.main()
