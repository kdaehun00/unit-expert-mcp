from __future__ import annotations

import unittest

from unit_expert_mcp.server import SERVER_NAME, handle_json_rpc, tools


class MinimalMcpTest(unittest.TestCase):
    def test_initialize_returns_playmcp_supported_protocol_and_session_header(self) -> None:
        status, headers, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertIn("Mcp-Session-Id", headers)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(payload["result"]["serverInfo"]["name"], SERVER_NAME)
        self.assertIs(payload["result"]["capabilities"]["tools"]["listChanged"], True)

    def test_tools_match_playmcp_required_metadata(self) -> None:
        listed_tools = tools()

        self.assertEqual(len(listed_tools), 3)
        for tool in listed_tools:
            self.assertGreaterEqual(len(tool["name"]), 1)
            self.assertLessEqual(len(tool["name"]), 128)
            self.assertTrue(
                all(character.isalnum() or character in {"_", "-"} for character in tool["name"])
            )
            self.assertNotIn("kakao", tool["name"].lower())
            self.assertIn("Unit Expert(단위전문가)", tool["description"])
            self.assertLessEqual(len(tool["description"]), 1024)
            self.assertTrue(tool["inputSchema"])
            self.assertEqual(
                tool["annotations"],
                {
                    "title": tool["annotations"]["title"],
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": False,
                    "idempotentHint": True,
                },
            )

    def test_convert_length_tool_returns_minimal_text_result(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "convert_length",
                    "arguments": {"value": 1, "from_unit": "m", "to_unit": "cm"},
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(
            payload["result"],
            {
                "content": [{"type": "text", "text": "1 m = 100 cm"}],
                "isError": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
