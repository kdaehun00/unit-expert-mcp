"""The strict-2026 and injection options are reachable from every config surface: DEFAULT_CONFIG,
normalize_config, cfg tokens, X-MCP-Test-Scenario, POST /scenario and the
/scenario page, and docs/CONFIG_REFERENCE.md lists them."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from mcp_test_support import (
    ERR_METHOD_NOT_FOUND,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_headers,
    modern_request,
)
from unit_expert_mcp.server import (
    DEFAULT_CONFIG,
    DISCOVER_MODES,
    LEGACY_MODERN_PROBE_MODES,
    MODERN_RESULT_TYPE_MODES,
    TOOL_CALL_MODES,
    advanced_field_groups,
    compact_config,
    decode_config_token,
    default_config,
    encode_config_token,
    handle_json_rpc,
    header_scenario_patches,
    normalize_config,
    render_inspect_page,
    render_scenario_page,
    scenario_to_config,
)

REFERENCE = Path(__file__).resolve().parents[1] / "docs" / "CONFIG_REFERENCE.md"

NEW_DEFAULTS = {
    "strict2026": True,
    "allowedOrigins": [],
    "modern": {
        "testTools": True,
        "requiredClientCapabilities": [],
        "invalidParamsHttpStatus": 200,
        "rejectUnexpectedParamHeader": True,
    },
    "discover": {
        "mode": "normal",
        "errorCode": -32603,
        "httpStatus": 503,
        "delaySeconds": 0.0,
        "cacheTtlMs": 0,
        "cacheScope": "private",
    },
    "modernResponse": {
        "resultType": "normal",
        "omitServerInfo": False,
        "omitCacheFields": False,
        "issueSessionId": False,
        "methods": [],
    },
    "legacyModernProbe": {"mode": "method-not-found", "delaySeconds": 10.0},
    "toolCall": {
        "mode": "normal",
        "errorCode": -32603,
        "httpStatus": 500,
        "delaySeconds": 0.0,
        "targetTools": [],
        "sseNotificationCount": 2,
    },
    "session": {"validate": False, "issueId": True},
}

CUSTOM = {
    "strict2026": False,
    "allowedOrigins": ["https://a.example", "https://b.example"],
    "modern": {
        "testTools": False,
        "requiredClientCapabilities": ["elicitation"],
        "invalidParamsHttpStatus": 400,
        "rejectUnexpectedParamHeader": False,
    },
    "discover": {
        "mode": "http-5xx",
        "errorCode": -32000,
        "httpStatus": 502,
        "delaySeconds": 1.5,
        "cacheTtlMs": 1000,
        "cacheScope": "public",
    },
    "modernResponse": {
        "resultType": "unknown",
        "omitServerInfo": True,
        "omitCacheFields": True,
        "issueSessionId": True,
        "methods": ["tools/list"],
    },
    "legacyModernProbe": {"mode": "timeout", "delaySeconds": 3.0},
    "toolCall": {
        "mode": "sse",
        "errorCode": -32001,
        "httpStatus": 503,
        "delaySeconds": 2.0,
        "targetTools": ["convert_length"],
        "sseNotificationCount": 5,
    },
    "session": {"validate": True, "issueId": False},
}


def leaf_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(leaf_paths(child, f"{prefix}.{key}" if prefix else key))
        return paths
    return [prefix]


class DefaultsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = default_config()
        for key, expected in NEW_DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(config[key], expected)
        self.assertEqual(config["toolsList"]["pageSize"], 0)
        self.assertEqual(config["toolsList"]["failAtPage"], 0)
        self.assertIs(config["auth"]["enforceScope"], False)

    def test_normalize_fills_defaults_and_is_idempotent(self) -> None:
        self.assertEqual(normalize_config({}), default_config())
        self.assertEqual(normalize_config(default_config()), default_config())
        custom = normalize_config(CUSTOM)
        self.assertEqual(normalize_config(custom), custom)

    def test_partial_groups_keep_the_other_defaults(self) -> None:
        config = normalize_config({"toolCall": {"mode": "empty"}, "discover": {"delaySeconds": 2}})
        self.assertEqual(config["toolCall"], {**NEW_DEFAULTS["toolCall"], "mode": "empty"})
        self.assertEqual(config["discover"], {**NEW_DEFAULTS["discover"], "delaySeconds": 2.0})

    def test_custom_values_survive_normalization(self) -> None:
        config = normalize_config(CUSTOM)
        for key, expected in CUSTOM.items():
            with self.subTest(key=key):
                self.assertEqual(config[key], expected)

    def test_lists_are_deduplicated_and_trimmed(self) -> None:
        config = normalize_config({"toolCall": {"targetTools": [" a ", "a", "b"]}})
        self.assertEqual(config["toolCall"]["targetTools"], ["a", "b"])

    def test_wrong_types_are_rejected(self) -> None:
        for bad in (
            {"strict2026": "true"},
            {"strict2026": 1},
            {"modern": {"testTools": "no"}},
            {"modern": {"invalidParamsHttpStatus": 404}},
            {"modern": {"rejectUnexpectedParamHeader": "no"}},
            {"modern": []},
            {"auth": {"enforceScope": "yes"}},
        ):
            with self.subTest(config=bad):
                with self.assertRaises(ValueError):
                    normalize_config(bad)


class ConfigTokenTest(unittest.TestCase):
    def test_default_token_is_still_empty(self) -> None:
        self.assertEqual(encode_config_token({}), "")
        self.assertEqual(compact_config(default_config()), {})

    def test_round_trip(self) -> None:
        token = encode_config_token(CUSTOM)
        decoded = decode_config_token(token)
        for key, expected in CUSTOM.items():
            with self.subTest(key=key):
                self.assertEqual(decoded[key], expected)

    def test_token_only_carries_the_differences(self) -> None:
        compact = compact_config(normalize_config({"toolCall": {"mode": "empty"}}))
        self.assertEqual(compact, {"toolCall": {"mode": "empty"}})

    def test_invalid_value_in_a_token_is_rejected(self) -> None:
        import base64

        raw = base64.urlsafe_b64encode(json.dumps({"discover": {"mode": "nope"}}).encode()).decode().rstrip("=")
        with self.assertRaises(ValueError):
            decode_config_token(raw)


class HeaderScenarioTest(unittest.TestCase):
    def test_every_mode_has_a_header_scenario(self) -> None:
        patches = header_scenario_patches()
        for mode in DISCOVER_MODES:
            if mode != "normal":
                self.assertEqual(scenario_to_config(f"discover-{mode}")["discover"]["mode"], mode)
        for mode in TOOL_CALL_MODES:
            if mode != "normal":
                self.assertEqual(scenario_to_config(f"tool-call-{mode}")["toolCall"]["mode"], mode)
        for mode in LEGACY_MODERN_PROBE_MODES:
            config = scenario_to_config(f"legacy-probe-{mode}")
            self.assertEqual(config["serverEra"], "legacy")
            self.assertEqual(config["legacyModernProbe"]["mode"], mode)
        for mode in MODERN_RESULT_TYPE_MODES:
            if mode != "normal":
                name = f"modern-result-type-{mode.replace('_', '-')}"
                self.assertEqual(scenario_to_config(name)["modernResponse"]["resultType"], mode)
        for name in patches:
            # Every shortcut yields a config that passes validation.
            self.assertEqual(normalize_config(scenario_to_config(name)), scenario_to_config(name))

    def test_flag_scenarios(self) -> None:
        self.assertIs(scenario_to_config("lenient-2026")["strict2026"], False)
        self.assertIs(scenario_to_config("session-validate")["session"]["validate"], True)
        self.assertIs(scenario_to_config("session-no-id")["session"]["issueId"], False)
        self.assertIs(scenario_to_config("modern-omit-server-info")["modernResponse"]["omitServerInfo"], True)
        self.assertIs(scenario_to_config("modern-omit-cache-fields")["modernResponse"]["omitCacheFields"], True)
        self.assertIs(scenario_to_config("modern-issue-session-id")["modernResponse"]["issueSessionId"], True)
        self.assertEqual(scenario_to_config("tools-list-paged")["toolsList"]["pageSize"], 2)
        self.assertEqual(
            scenario_to_config("require-capability-sampling")["modern"]["requiredClientCapabilities"],
            ["sampling"],
        )
        self.assertEqual(
            scenario_to_config("initialize-version-2026-07-28")["initialize"]["protocolVersion"], "2026-07-28"
        )

    def test_scenarios_combine_with_commas(self) -> None:
        config = scenario_to_config("server-era-modern, discover-sse ,tool-call-empty,tools-list-empty")
        self.assertEqual(config["serverEra"], "modern")
        self.assertIs(config["rejectLegacy"], True)
        self.assertEqual(config["discover"]["mode"], "sse")
        self.assertEqual(config["toolCall"]["mode"], "empty")
        self.assertEqual(config["toolsList"]["mode"], "empty")

    def test_existing_single_scenarios_are_unchanged(self) -> None:
        self.assertEqual(scenario_to_config("ok"), default_config())
        self.assertEqual(scenario_to_config(None), default_config())
        self.assertEqual(scenario_to_config("no-such-scenario"), default_config())
        self.assertEqual(scenario_to_config("auth-401")["server"]["httpStatus"], 401)
        self.assertEqual(scenario_to_config("missing-name")["toolErrors"], ["missing-name"])
        self.assertEqual(scenario_to_config("server-era-legacy")["serverEra"], "legacy")

    def test_cfg_prefix_carries_a_full_config(self) -> None:
        token = encode_config_token(CUSTOM)
        config = scenario_to_config(f"cfg:{token}")
        self.assertEqual(config["toolCall"], CUSTOM["toolCall"])
        with self.assertRaises(ValueError):
            scenario_to_config("cfg:!!!")

    def test_scenario_argument_of_handle_json_rpc(self) -> None:
        status, _, payload = handle_json_rpc(
            modern_request("server/discover"),
            scenario="discover-json-rpc-error",
            headers=modern_headers("server/discover"),
        )
        self.assertEqual(status, 200)
        assert payload is not None
        self.assertIn("error", payload)


class ScenarioSurfaceHttpTest(HttpServerTestCase):
    def test_header_scenario_on_the_wire(self) -> None:
        status, _, payload, _ = self.post(
            modern_request("server/discover"),
            headers={**modern_headers("server/discover"), "X-MCP-Test-Scenario": "discover-empty-versions"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["supportedVersions"], [])

    def test_header_cfg_token_on_the_wire(self) -> None:
        token = encode_config_token({"toolsList": {"pageSize": 1}})
        status, _, payload, _ = self.post(
            legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": f"cfg:{token}"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["result"]["tools"]), 1)

    def test_invalid_header_cfg_token_is_400(self) -> None:
        status, _, _, raw = self.post(legacy_request("tools/list"), headers={"X-MCP-Test-Scenario": "cfg:!!!"})
        self.assertEqual(status, 400)
        self.assertIn(b"invalid config token", raw)

    def test_scenario_endpoint_applies_and_validates_the_options(self) -> None:
        def apply(config: dict) -> tuple[int, dict]:
            status, _, raw = self.request(
                "POST",
                "/scenario",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"config": config}).encode("utf-8"),
            )
            return status, json.loads(raw)

        status, body = apply({"serverEra": "legacy", "legacyModernProbe": {"mode": "invalid-params"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["legacyModernProbe"]["mode"], "invalid-params")
        status, _, payload, _ = self.modern_post("server/discover")
        self.assertEqual(payload["error"]["code"], -32602)

        status, body = apply({"toolCall": {"mode": "nope"}})
        self.assertEqual(status, 400)
        self.assertIn("toolCall.mode", body["error"])

    def test_default_server_still_serves_both_eras(self) -> None:
        status, headers, payload, _ = self.post(legacy_initialize())
        self.assertEqual(status, 200)
        self.assertTrue(headers["mcp-session-id"])
        status, _, payload, _ = self.modern_post("server/discover")
        self.assertEqual(status, 200)
        self.assertIn("supportedVersions", payload["result"])
        status, _, payload, _ = self.post(legacy_request("server/discover"))
        self.assertEqual(payload["error"]["code"], ERR_METHOD_NOT_FOUND)


class PageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.page = render_scenario_page()

    def field_paths(self) -> list[str]:
        return [field["path"] for _, _, fields in advanced_field_groups() for field in fields]

    def test_every_new_option_has_a_control(self) -> None:
        expected = set(leaf_paths(NEW_DEFAULTS)) | {
            "toolsList.pageSize",
            "toolsList.failAtPage",
            "auth.enforceScope",
            "initialize.protocolVersion",
        }
        self.assertEqual(set(self.field_paths()), expected)
        for path in expected:
            with self.subTest(path=path):
                self.assertIn(f'data-config-path="{path}"', self.page)

    def test_controls_are_grouped_with_a_2026_only_group(self) -> None:
        groups = {label: [field["path"] for field in fields] for label, _, fields in advanced_field_groups()}
        self.assertEqual(list(groups), ["2026-07-28 전용", "레거시 전용", "공용"])
        modern_only = groups["2026-07-28 전용"]
        self.assertIn("strict2026", modern_only)
        self.assertTrue(
            all(path.split(".")[0] in {"strict2026", "discover", "modernResponse", "modern"} for path in modern_only)
        )
        self.assertIn("toolCall.mode", groups["공용"])
        self.assertIn("legacyModernProbe.mode", groups["레거시 전용"])
        self.assertIn("2026-07-28 전용", self.page)

    def test_select_options_list_every_mode(self) -> None:
        for modes in (DISCOVER_MODES, TOOL_CALL_MODES, LEGACY_MODERN_PROBE_MODES, MODERN_RESULT_TYPE_MODES):
            for mode in modes:
                with self.subTest(mode=mode):
                    self.assertIn(f'<option value="{mode}">', self.page)
        for version in ("2026-07-28", "2025-11-25", "2024-03-26"):
            self.assertIn(f'<option value="{version}">', self.page)

    def test_field_paths_exist_in_the_default_config(self) -> None:
        for path in self.field_paths():
            with self.subTest(path=path):
                value: object = DEFAULT_CONFIG
                for key in path.split("."):
                    self.assertIsInstance(value, dict)
                    self.assertIn(key, value)  # type: ignore[arg-type]
                    value = value[key]  # type: ignore[index]

    def test_page_has_the_expire_sessions_button_and_generic_binding(self) -> None:
        self.assertIn('id="expireSessions"', self.page)
        self.assertIn("expireSessions", self.page)
        self.assertIn("function readAdvanced", self.page)
        self.assertIn("function writeAdvanced", self.page)

    def test_inspect_page_shows_injections(self) -> None:
        self.assertIn("x.injected", render_inspect_page())

    def test_inspect_page_does_not_show_a_dropped_connection_as_ok(self) -> None:
        page = render_inspect_page()
        # status 0 (disconnect) must not get the green badge.
        self.assertIn("x.status >= 200 && x.status < 400", page)
        self.assertNotIn("const ok = x.status < 400;", page)
        self.assertIn('if (!exchange.status) return "연결 종료";', page)


class ReferenceDocTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = REFERENCE.read_text(encoding="utf-8")

    def test_every_config_key_is_documented(self) -> None:
        for path in leaf_paths(DEFAULT_CONFIG):
            with self.subTest(path=path):
                self.assertIn(f"`{path}`", self.text)

    def test_every_mode_value_is_documented(self) -> None:
        for modes in (DISCOVER_MODES, TOOL_CALL_MODES, LEGACY_MODERN_PROBE_MODES, MODERN_RESULT_TYPE_MODES):
            for mode in modes:
                with self.subTest(mode=mode):
                    self.assertIn(f"`{mode}`", self.text)

    def test_every_header_scenario_is_documented(self) -> None:
        documented = set(re.findall(r"`([a-z0-9_\-]+)`", self.text))
        prefixes = ("discover-", "tool-call-", "legacy-probe-", "modern-result-type-", "require-capability-")
        for name in header_scenario_patches():
            with self.subTest(name=name):
                self.assertTrue(
                    name in documented or any(name.startswith(prefix) for prefix in prefixes),
                    name,
                )
        for prefix in prefixes:
            self.assertIn(prefix, self.text)


if __name__ == "__main__":
    unittest.main()
