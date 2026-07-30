from __future__ import annotations

import unittest

from unit_expert_mcp.server import (
    PROTOCOL_VERSION_2026,
    SERVER_NAME,
    SERVER_VERSION,
    handle_json_rpc,
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

    def test_missing_client_capabilities_is_invalid_params(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
