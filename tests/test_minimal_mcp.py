from __future__ import annotations

import unittest

from unit_expert_mcp.server import (
    SCENARIO_DESCRIPTIONS,
    SCENARIO_GROUPS,
    SERVER_NAME,
    decode_config_token,
    encode_config_token,
    get_active_scenario,
    handle_json_rpc,
    resolve_config,
    resolve_scenario,
    set_active_config,
    set_active_scenario,
    tools,
)


class MinimalMcpTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_active_scenario("ok")

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

        self.assertEqual(len(listed_tools), 6)
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

    def test_convert_temperature_tool_returns_minimal_text_result(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "convert_temperature",
                    "arguments": {"value": 32, "from_unit": "f", "to_unit": "c"},
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(
            payload["result"],
            {
                "content": [{"type": "text", "text": "32 f = 0 c"}],
                "isError": False,
            },
        )

    def test_unsupported_min_version_scenario_returns_old_protocol_version(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            "unsupported-min-version",
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2024-11-05")

    def test_duplicate_tool_name_scenario_changes_tools_list(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            "duplicate-tool-name",
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        names = [tool["name"] for tool in payload["result"]["tools"]]
        self.assertEqual(names, ["search_place", "search_place"])

    def test_tools_list_error_scenario_returns_json_rpc_error(self) -> None:
        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            "tools-list-error",
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["error"]["code"], -32603)

    def test_custom_config_can_combine_initialize_and_tool_errors(self) -> None:
        config = {
            "server": {"httpStatus": 200, "target": "initialize"},
            "initialize": {
                "protocolVersionEnabled": True,
                "protocolVersion": "2024-11-05",
            },
            "toolsList": {"mode": "normal"},
            "toolErrors": ["duplicate-tool-name", "missing-description"],
        }

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            config=config,
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"]["protocolVersion"], "2024-11-05")

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            config=config,
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        listed_tools = payload["result"]["tools"]
        self.assertEqual([tool.get("name") for tool in listed_tools[:2]], ["search_place", "search_place"])
        self.assertNotIn("description", listed_tools[2])

    def test_active_custom_config_can_return_many_tools(self) -> None:
        set_active_config(
            {
                "toolsList": {
                    "mode": "too-many",
                    "tooManyCount": 25,
                }
            }
        )

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            config=resolve_config(None),
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(len(payload["result"]["tools"]), 25)

    def test_custom_config_can_override_mcp_identifier_and_service_name(self) -> None:
        config = {
            "mcp": {
                "identifier": "unitExpertLocal",
                "serviceName": "Unit Expert Local(단위전문가 로컬)",
            }
        }

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            },
            config=config,
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"]["serverInfo"]["name"], "unitExpertLocal")

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            config=config,
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIn(
            "Unit Expert Local(단위전문가 로컬)",
            payload["result"]["tools"][0]["description"],
        )

    def test_config_token_round_trip_normalizes_custom_url_config(self) -> None:
        token = encode_config_token(
            {
                "mcp": {
                    "identifier": "unitExpertUrl",
                    "serviceName": "Unit Expert URL(단위전문가 URL)",
                },
                "server": {"httpStatus": 401},
                "customHeader": {"enabled": True},
                "toolsList": {"mode": "normal"},
                "toolErrors": ["invalid-tool-name-char"],
            }
        )

        config = decode_config_token(token)

        self.assertEqual(config["mcp"]["identifier"], "unitExpertUrl")
        self.assertEqual(config["mcp"]["serviceName"], "Unit Expert URL(단위전문가 URL)")
        self.assertEqual(config["server"]["httpStatus"], 401)
        self.assertIs(config["customHeader"]["enabled"], True)
        self.assertEqual(config["toolErrors"], ["invalid-tool-name-char"])

    def test_default_config_token_is_empty_for_short_default_url(self) -> None:
        self.assertEqual(encode_config_token({}), "")

    def test_scenario_groups_cover_every_supported_scenario(self) -> None:
        grouped_scenarios = [
            scenario
            for _, scenarios in SCENARIO_GROUPS
            for scenario in scenarios
        ]

        self.assertEqual(set(grouped_scenarios), set(SCENARIO_DESCRIPTIONS))
        self.assertEqual(len(grouped_scenarios), len(set(grouped_scenarios)))
        self.assertNotIn("mcp-identifier-name", grouped_scenarios)

    def test_active_scenario_can_be_changed_without_header(self) -> None:
        set_active_scenario("tools-list-empty")

        self.assertEqual(get_active_scenario(), "tools-list-empty")
        self.assertEqual(resolve_scenario(None), "tools-list-empty")
        self.assertEqual(resolve_scenario("duplicate-tool-name"), "duplicate-tool-name")

        status, _, payload = handle_json_rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            resolve_scenario(None),
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["result"]["tools"], [])


if __name__ == "__main__":
    unittest.main()
