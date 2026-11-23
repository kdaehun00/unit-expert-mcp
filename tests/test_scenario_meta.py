"""GET /scenario/meta and the validate/decode actions of POST /scenario.

The metadata is the single definition the control pages render from, so these
tests tie it to DEFAULT_CONFIG, normalize_config, the docs and the README."""

from __future__ import annotations

import json
import re
import unittest
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

from mcp_test_support import HttpServerTestCase, legacy_request
from unit_expert_mcp.server import (
    AUTH_MODES,
    CLIENT_CAPABILITY_NAMES,
    CONFIG_OPTION_EXCLUDED,
    DEFAULT_CONFIG,
    DISCOVER_MODES,
    ERR_HEADER_MISMATCH,
    ERR_INTERNAL,
    ERR_INVALID_PARAMS,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_MISSING_CAPABILITY,
    ERR_UNSUPPORTED_VERSION,
    LEGACY_MODERN_PROBE_MODES,
    MODERN_RESULT_METHODS,
    MODERN_RESULT_TYPE_MODES,
    SCENARIO_DESCRIPTIONS,
    SERVER_ERA_SCENARIOS,
    SERVER_ERAS,
    SERVER_ERROR_GROUPS,
    SESSIONS,
    TOOL_CALL_MODES,
    TOOL_ERROR_SCENARIOS,
    TOOLS_LIST_MODES,
    compact_config,
    config_option_schema,
    config_token_from_text,
    decode_config_token,
    default_config,
    encode_config_token,
    get_active_config,
    get_active_scenario,
    header_scenario_patches,
    normalize_config,
    preset_config,
    scenario_meta,
    scenario_presets,
    scenario_to_config,
    set_active_config,
)

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
REFERENCE = (ROOT / "docs" / "CONFIG_REFERENCE.md").read_text(encoding="utf-8")

GROUP_IDS = [
    "basic", "protocol", "auth", "tools", "session",
    "errToolCall", "errToolsList", "errAll", "errDiscover", "errModernResponse", "errLegacyProbe", "errRaw",
]
BUILD_GROUPS = GROUP_IDS[:5]
KINDS = {"bool", "enum", "int", "float", "string", "list", "json"}

# Allowed values of every option that has choices. The constants are the ones
# normalize_config checks against; the literal sets mirror checks it does inline.
ALLOWED_VALUES: dict[str, set[Any]] = {
    "serverEra": set(SERVER_ERAS),
    "auth.mode": set(AUTH_MODES),
    "toolsList.mode": set(TOOLS_LIST_MODES),
    "toolCall.mode": set(TOOL_CALL_MODES),
    "discover.mode": set(DISCOVER_MODES),
    "modernResponse.resultType": set(MODERN_RESULT_TYPE_MODES),
    "legacyModernProbe.mode": set(LEGACY_MODERN_PROBE_MODES),
    "toolErrors": set(TOOL_ERROR_SCENARIOS),
    "modernResponse.methods": set(MODERN_RESULT_METHODS),
    "modern.requiredClientCapabilities": set(CLIENT_CAPABILITY_NAMES),
    "server.httpStatus": {200, 401, 403},
    "server.target": {"all", "initialize"},
    "toolsList.cacheScope": {"private", "public"},
    "discover.cacheScope": {"private", "public"},
    "modern.invalidParamsHttpStatus": {200, 400},
}


def leaf_paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, child in value.items():
            paths.extend(leaf_paths(child, f"{prefix}.{key}" if prefix else key))
        return paths
    return [prefix]


def at(config: dict, path: str) -> Any:
    value: Any = config
    for key in path.split("."):
        value = value[key]
    return value


def patch_for(path: str, value: Any) -> dict:
    patch: Any = value
    for key in reversed(path.split(".")):
        patch = {key: patch}
    return patch


def with_dependency(option: dict, patch: dict) -> dict:
    """The patch plus whatever the option depends on, so normalize keeps the value."""
    depends = option["dependsOn"]
    if depends is None:
        return patch
    value = depends["in"][0] if "in" in depends else depends["min"]
    merged = default_config()
    for part in (patch_for(depends["path"], value), patch):
        stack = [(merged, part)]
        while stack:
            target, source = stack.pop()
            for key, child in source.items():
                if isinstance(child, dict) and isinstance(target.get(key), dict):
                    stack.append((target[key], child))
                else:
                    target[key] = child
    return merged


class OptionSchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.options = config_option_schema()
        self.by_path = {option["path"]: option for option in self.options}

    def test_paths_are_unique_and_exist_with_the_right_default(self) -> None:
        self.assertEqual(len(self.by_path), len(self.options))
        for option in self.options:
            with self.subTest(path=option["path"]):
                self.assertEqual(option["default"], at(DEFAULT_CONFIG, option["path"]))

    def test_every_config_leaf_is_in_the_schema_or_explicitly_excluded(self) -> None:
        leaves = set(leaf_paths(DEFAULT_CONFIG))
        self.assertEqual(set(self.by_path) & set(CONFIG_OPTION_EXCLUDED), set())
        self.assertEqual(set(self.by_path) | set(CONFIG_OPTION_EXCLUDED), leaves)
        for path, reason in CONFIG_OPTION_EXCLUDED.items():
            self.assertTrue(reason.strip(), path)

    def test_excluded_leaves_really_cannot_be_set(self) -> None:
        config = normalize_config({"customHeader": {"enabled": True, "name": "X-Other", "value": "x"}})
        self.assertEqual(config["customHeader"], {"enabled": True, "name": "X-Mock-Auth", "value": "allow"})
        self.assertIs(normalize_config({"serverEra": "legacy", "rejectLegacy": True})["rejectLegacy"], False)

    def test_entries_have_the_documented_shape(self) -> None:
        required = {
            "path", "label", "description", "kind", "choices", "min", "max",
            "group", "section", "appliesTo", "dependsOn", "default",
        }
        for option in self.options:
            with self.subTest(path=option["path"]):
                self.assertLessEqual(required, set(option))
                self.assertIn(option["kind"], KINDS)
                self.assertIn(option["group"], GROUP_IDS)
                self.assertIn(option["appliesTo"], {"both", "legacy", "modern"})
                self.assertTrue(option["label"].strip())
                self.assertTrue(option["description"].strip())
                self.assertNotIn("\n", option["description"])
                if option["kind"] == "enum":
                    self.assertTrue(option["choices"])
                if option["choices"] is not None:
                    for choice in option["choices"]:
                        self.assertLessEqual({"value", "description"}, set(choice))
                        self.assertTrue(str(choice["description"]).strip())

    def test_defaults_match_their_kind(self) -> None:
        types = {"bool": bool, "int": int, "float": float, "string": str, "list": list, "json": list}
        for option in self.options:
            with self.subTest(path=option["path"]):
                if option["kind"] == "enum":
                    self.assertIn(option["default"], [choice["value"] for choice in option["choices"]])
                else:
                    self.assertIs(type(option["default"]), types[option["kind"]])

    def test_choices_equal_the_values_normalize_config_allows(self) -> None:
        with_choices = {path for path, option in self.by_path.items() if option["choices"] is not None}
        self.assertEqual(with_choices, set(ALLOWED_VALUES))
        for path in sorted(with_choices):
            with self.subTest(path=path):
                values = [choice["value"] for choice in self.by_path[path]["choices"]]
                self.assertEqual(len(values), len(set(values)))
                self.assertEqual(set(values), ALLOWED_VALUES[path])

    def test_every_choice_is_accepted_and_anything_else_is_rejected(self) -> None:
        for path, allowed in ALLOWED_VALUES.items():
            option = self.by_path[path]
            is_list = option["kind"] == "list"
            for value in allowed:
                with self.subTest(path=path, value=value):
                    raw = with_dependency(option, patch_for(path, [value] if is_list else value))
                    self.assertEqual(at(normalize_config(raw), path), [value] if is_list else value)
            bogus: Any = 999 if all(isinstance(value, int) for value in allowed) else "__nope__"
            with self.subTest(path=path, value=bogus):
                with self.assertRaises(ValueError):
                    normalize_config(patch_for(path, [bogus] if is_list else bogus))

    def test_ranges_equal_the_ranges_normalize_config_allows(self) -> None:
        ranged = [option for option in self.options if option["min"] is not None or option["max"] is not None]
        self.assertGreaterEqual(len(ranged), 14)
        for option in ranged:
            path = option["path"]
            self.assertIn(option["kind"], {"int", "float"}, path)
            self.assertIsNotNone(option["min"], path)
            self.assertIsNotNone(option["max"], path)
            cast = int if option["kind"] == "int" else float
            for value in (cast(option["min"]), cast(option["max"])):
                with self.subTest(path=path, value=value):
                    raw = with_dependency(option, patch_for(path, value))
                    self.assertEqual(at(normalize_config(raw), path), value)
            for value in (cast(option["min"]) - 1, cast(option["max"]) + 1):
                with self.subTest(path=path, rejected=value):
                    with self.assertRaises(ValueError):
                        normalize_config(patch_for(path, value))
        for option in self.options:
            if option["kind"] in {"int", "float"}:
                self.assertIsNotNone(option["min"], option["path"])

    def test_depends_on_points_at_another_option(self) -> None:
        for option in self.options:
            depends = option["dependsOn"]
            if depends is None:
                continue
            with self.subTest(path=option["path"]):
                self.assertIn(depends["path"], self.by_path)
                self.assertNotEqual(depends["path"], option["path"])
                self.assertEqual(len({"in", "min"} & set(depends)), 1)
                target = self.by_path[depends["path"]]
                if "in" in depends:
                    self.assertTrue(depends["in"])
                    if target["kind"] == "enum":
                        allowed = [choice["value"] for choice in target["choices"]]
                        self.assertLessEqual(set(depends["in"]), set(allowed))
                    else:
                        self.assertEqual(target["kind"], "bool")
                        self.assertLessEqual(set(depends["in"]), {True, False})
                else:
                    self.assertIn(target["kind"], {"int", "float"})

    def test_applies_to_matches_the_reference_doc(self) -> None:
        eras = {"둘 다": "both", "모던": "modern", "레거시": "legacy"}
        documented: dict[str, str] = {}
        for line in REFERENCE.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            match = re.fullmatch(r"`([A-Za-z0-9.]+)`", cells[0]) if len(cells) == 5 else None
            if match is None:
                continue
            era = re.sub(r"\(.*\)$", "", cells[-1]).strip()
            if era in eras:
                documented[match.group(1)] = eras[era]
        self.assertLessEqual(set(self.by_path), set(documented))
        for path, option in self.by_path.items():
            with self.subTest(path=path):
                self.assertEqual(option["appliesTo"], documented[path])

    def test_strict_only_marks_the_options_strict2026_gates(self) -> None:
        gated = {path for path, option in self.by_path.items() if option.get("strictOnly")}
        expected = {
            path for path in self.by_path if path.split(".")[0] in {"discover", "modernResponse", "modern"}
        }
        self.assertEqual(gated, expected)
        for path in gated:
            self.assertEqual(self.by_path[path]["appliesTo"], "modern")

    def test_groups_are_ordered_and_all_used(self) -> None:
        meta = scenario_meta()
        self.assertEqual([group["id"] for group in meta["groups"]], GROUP_IDS)
        for group in meta["groups"]:
            self.assertEqual(set(group), {"id", "section", "open", "label", "description"})
            self.assertEqual(group["section"], "build" if group["id"] in BUILD_GROUPS else "error")
            # A build group is never folded.
            self.assertTrue(group["open"] or group["section"] == "error")
            self.assertTrue(group["label"].strip() and group["description"].strip())
        used = [option["group"] for option in self.options]
        self.assertEqual(set(used), set(GROUP_IDS))
        # Options come grouped, in group order.
        self.assertEqual(sorted(used, key=GROUP_IDS.index), used)

    def test_group_membership_follows_the_era(self) -> None:
        for option in self.options:
            with self.subTest(path=option["path"]):
                if option["group"] in {"errDiscover", "errModernResponse"}:
                    self.assertEqual(option["appliesTo"], "modern")
                if option["group"] in {"session", "errLegacyProbe"}:
                    self.assertEqual(option["appliesTo"], "legacy")
        self.assertEqual(self.by_path["rawResponses"]["group"], "errRaw")
        self.assertEqual(self.by_path["allowedOrigins"]["group"], "auth")
        self.assertEqual(self.by_path["strict2026"]["group"], "protocol")
        self.assertEqual(self.by_path["serverEra"]["group"], "protocol")

    def test_every_option_is_build_or_error(self) -> None:
        """build: a setting a server that follows the spec may have. error:
        breaks the spec or imitates a failure. The section comes from the group."""
        meta = scenario_meta()
        self.assertEqual([section["id"] for section in meta["sections"]], ["build", "error"])
        sections = {group["id"]: group["section"] for group in meta["groups"]}
        for option in self.options:
            with self.subTest(path=option["path"]):
                self.assertEqual(option["section"], sections[option["group"]])
        build = {path for path, option in self.by_path.items() if option["section"] == "build"}
        self.assertEqual(
            build,
            {
                "mcp.identifier", "mcp.serviceName",
                "serverEra", "strict2026", "initialize.protocolVersionEnabled", "initialize.protocolVersion",
                "modern.requiredClientCapabilities", "modern.invalidParamsHttpStatus",
                "modern.rejectUnexpectedParamHeader",
                "auth.mode", "auth.protectedTools", "auth.enforceScope", "allowedOrigins",
                "modern.testTools", "toolsList.pageSize", "toolsList.cacheTtlMs", "toolsList.cacheScope",
                "discover.cacheTtlMs", "discover.cacheScope",
                "session.issueId", "session.validate",
            },
        )
        # Every option that fails or delays on purpose is an error option.
        for path in ("toolCall.mode", "toolsList.mode", "discover.mode", "server.httpStatus",
                     "server.delayEnabled", "customHeader.enabled", "modernResponse.resultType",
                     "legacyModernProbe.mode", "rawResponses", "toolErrors", "toolsList.failAtPage"):
            self.assertEqual(self.by_path[path]["section"], "error", path)

    def test_presets_take_the_section_of_what_they_set(self) -> None:
        presets = {preset["id"]: preset for preset in scenario_presets()}
        build = {name for name, preset in presets.items() if preset["category"] == "injection" and preset["section"] == "build"}
        self.assertEqual(build, {"auth-oauth-401", "tools-list-paged", "session-validate"})
        # A preset that sets one error option is an error preset, whatever else it sets.
        self.assertEqual(presets["legacy-probe-http-400"]["section"], "error")


class PresetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.presets = scenario_presets()

    def test_shape(self) -> None:
        ids = [preset["id"] for preset in self.presets]
        self.assertEqual(len(ids), len(set(ids)))
        for preset in self.presets:
            with self.subTest(preset=preset["id"]):
                self.assertLessEqual({"id", "label", "description", "category", "patch"}, set(preset))
                self.assertIn(preset["category"], {"matrix", "injection"})
                self.assertIsInstance(preset["patch"], dict)
                self.assertTrue(preset["patch"])
                self.assertTrue(preset["label"].strip() and preset["description"].strip())
        categories = [preset["category"] for preset in self.presets]
        self.assertEqual(categories.count("matrix"), 4)
        self.assertTrue(10 <= categories.count("injection") <= 16)

    def test_every_patch_normalizes_and_round_trips_through_a_token(self) -> None:
        for preset in self.presets:
            with self.subTest(preset=preset["id"]):
                config = preset_config(preset["patch"])
                self.assertEqual(normalize_config(preset["patch"]), config)
                self.assertEqual(normalize_config(config), config)
                self.assertNotEqual(config, default_config())
                token = encode_config_token(preset["patch"])
                self.assertTrue(token)
                self.assertEqual(decode_config_token(token), config)
                self.assertEqual(encode_config_token(decode_config_token(token)), token)
                # The patch says what it changes: every value in it is in effect.
                for path in leaf_paths(preset["patch"]):
                    self.assertEqual(at(config, path), at(preset["patch"], path))

    def test_patches_only_use_schema_options(self) -> None:
        paths = {option["path"] for option in config_option_schema()}
        for preset in self.presets:
            with self.subTest(preset=preset["id"]):
                self.assertLessEqual(set(leaf_paths(preset["patch"])), paths)

    def test_injection_presets_cover_the_common_cases(self) -> None:
        compact = [
            compact_config(preset_config(preset["patch"]))
            for preset in self.presets
            if preset["category"] == "injection"
        ]
        for expected in (
            {"auth": {"mode": "oauth"}},
            {"server": {"delayEnabled": True}},
            {"toolsList": {"mode": "json-rpc-error"}},
            {"toolsList": {"mode": "empty"}},
            {"toolsList": {"pageSize": 2}},
            {"toolCall": {"mode": "is-error"}},
            {"toolCall": {"mode": "json-rpc-error"}},
            {"toolCall": {"mode": "http-status"}},
            {"toolCall": {"mode": "disconnect"}},
            {"toolCall": {"mode": "sse"}},
            {"discover": {"mode": "no-modern-version"}},
            {"discover": {"mode": "http-5xx"}},
            {"serverEra": "legacy", "legacyModernProbe": {"mode": "http-400"}},
            {"session": {"validate": True}},
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, compact)


class MatrixPresetTest(unittest.TestCase):
    """The matrix presets are the README section "게이트웨이 테스트 매트릭스"."""

    def setUp(self) -> None:
        self.matrix = [preset for preset in scenario_presets() if preset["category"] == "matrix"]
        # | modern | `{"serverEra":"modern"}` | `<server>/mcp?cfg=<token>` | ...
        self.readme_urls = {
            era: (json.loads(cfg_json), token)
            for era, cfg_json, token in re.findall(
                r"^\| (modern|legacy) \| `(\{[^`]+\})` \| `[^`]*/mcp\?cfg=([A-Za-z0-9_-]+)` \|",
                README,
                re.MULTILINE,
            )
        }
        # | 2026 방식 요청 → 2026-07-28만 지원하는 서버 | modern URL | `run ...` | ...
        modes = {"2026 방식": "modern", "2025 방식": "legacy"}
        servers = {"2026-07-28만": "modern", "2025 방식만": "legacy"}
        self.readme_commands = {
            (modes[client], servers[server]): command
            for client, server, command in re.findall(
                r"^\| (2026 방식|2025 방식) 요청 → (2026-07-28만|2025 방식만) 지원하는 서버[^|]*\| [^|]+\| `(run [^`]+)` \|",
                README,
                re.MULTILINE,
            )
        }

    def test_readme_tables_were_found(self) -> None:
        self.assertEqual(set(self.readme_urls), {"modern", "legacy"})
        self.assertEqual(len(self.readme_commands), 4)

    def test_the_four_pairs(self) -> None:
        pairs = [(preset["clientMode"], preset["patch"]["serverEra"]) for preset in self.matrix]
        self.assertEqual(
            pairs,
            [("modern", "modern"), ("modern", "legacy"), ("legacy", "legacy"), ("legacy", "modern")],
        )
        for preset in self.matrix:
            client, server = preset["clientMode"], preset["patch"]["serverEra"]
            mode = {"modern": "2026 방식", "legacy": "2025 방식"}[client]
            supports = {"modern": "2026-07-28만", "legacy": "2025 방식만"}[server]
            self.assertEqual(preset["label"], f"{mode} 요청 → {supports} 지원하는 서버")
            # The README table names the pair the same way.
            self.assertIn(f"| {preset['label']}", README)
            self.assertTrue(preset["expect"].strip())
            self.assertNotIn("\n", preset["expect"])

    def test_tokens_decode_to_the_readme_tokens(self) -> None:
        for preset in self.matrix:
            with self.subTest(preset=preset["id"]):
                readme_patch, readme_token = self.readme_urls[preset["patch"]["serverEra"]]
                self.assertEqual(preset["patch"], readme_patch)
                self.assertEqual(
                    decode_config_token(encode_config_token(preset["patch"])),
                    decode_config_token(readme_token),
                )

    def test_cli_is_the_readme_command_with_a_url_slot(self) -> None:
        prefix = "PYTHONPATH=src python3 -m unit_expert_mcp.client "
        self.assertIn(prefix.strip(), README)
        for preset in self.matrix:
            with self.subTest(preset=preset["id"]):
                cli = preset["cli"]
                self.assertTrue(cli.startswith(prefix))
                self.assertEqual(cli.count("{url}"), 1)
                self.assertIn(" --url {url}", cli)
                command = cli[len(prefix):].replace(" --url {url}", "")
                self.assertEqual(
                    command, self.readme_commands[(preset["clientMode"], preset["patch"]["serverEra"])]
                )


class MetaContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.meta = scenario_meta()

    def test_top_level(self) -> None:
        self.assertLessEqual(
            {"defaults", "options", "groups", "presets", "scenarioHeaders", "errorCodes"}, set(self.meta)
        )
        self.assertEqual(self.meta["defaults"], default_config())
        self.assertEqual(self.meta["options"], config_option_schema())
        self.assertEqual(self.meta["presets"], scenario_presets())
        self.assertEqual(self.meta["excluded"], CONFIG_OPTION_EXCLUDED)
        self.assertEqual(self.meta["scenarioHeader"], "X-MCP-Test-Scenario")

    def test_it_is_json_and_a_fresh_copy(self) -> None:
        self.assertEqual(json.loads(json.dumps(self.meta, ensure_ascii=False)), self.meta)
        self.meta["defaults"]["toolsList"]["mode"] = "empty"
        self.meta["options"][0]["default"] = "changed"
        self.assertEqual(scenario_meta()["defaults"], default_config())
        self.assertEqual(DEFAULT_CONFIG["toolsList"]["mode"], "normal")
        self.assertNotEqual(scenario_meta()["options"][0]["default"], "changed")

    def test_scenario_headers_list_every_name(self) -> None:
        entries = {entry["name"]: entry for entry in self.meta["scenarioHeaders"]}
        self.assertEqual(len(entries), len(self.meta["scenarioHeaders"]))
        concrete = set(SCENARIO_DESCRIPTIONS) | set(SERVER_ERA_SCENARIOS) | set(header_scenario_patches())
        self.assertEqual({name for name, entry in entries.items() if entry["kind"] != "pattern"}, concrete)
        self.assertEqual(
            {name for name, entry in entries.items() if entry["kind"] == "pattern"},
            {"initialize-version-<버전>", "cfg:<토큰>"},
        )
        for name in concrete:
            with self.subTest(name=name):
                entry = entries[name]
                self.assertTrue(entry["description"].strip())
                self.assertEqual(entry["kind"], "scenario" if name in SCENARIO_DESCRIPTIONS else "header-only")
                # The patch is exactly what the header value does.
                self.assertEqual(preset_config(entry["patch"]), scenario_to_config(name))

    def test_the_nineteen_error_scenarios_map_to_config(self) -> None:
        entries = {entry["name"]: entry for entry in self.meta["scenarioHeaders"]}
        server_errors = [name for _, _, names in SERVER_ERROR_GROUPS for name in names]
        names = server_errors + list(TOOL_ERROR_SCENARIOS)
        self.assertEqual(len(names), 19)
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(entries[name]["title"])
                self.assertTrue(entries[name]["patch"])
        self.assertEqual(entries["auth-401"]["patch"], {"server": {"httpStatus": 401}})
        self.assertEqual(entries["delayed-response"]["patch"], {"server": {"delayEnabled": True}})
        self.assertEqual(entries["too-many-tools"]["patch"], {"toolsList": {"mode": "too-many"}})
        self.assertEqual(entries["duplicate-tool-name"]["patch"], {"toolsList": {"duplicateToolName": True}})
        self.assertEqual(
            entries["unsupported-min-version"]["patch"], {"initialize": {"protocolVersion": "2024-11-05"}}
        )
        for name in TOOL_ERROR_SCENARIOS:
            self.assertEqual(entries[name]["patch"], {"toolErrors": [name]})
        self.assertEqual(entries["ok"]["patch"], {})

    def test_error_codes(self) -> None:
        codes = {entry["code"]: entry["name"] for entry in self.meta["errorCodes"]}
        self.assertEqual(len(codes), len(self.meta["errorCodes"]))
        self.assertEqual(
            codes,
            {
                -32700: "ParseError",
                ERR_INVALID_REQUEST: "InvalidRequest",
                ERR_METHOD_NOT_FOUND: "MethodNotFound",
                ERR_INVALID_PARAMS: "InvalidParams",
                ERR_INTERNAL: "InternalError",
                ERR_HEADER_MISMATCH: "HeaderMismatch",
                ERR_MISSING_CAPABILITY: "MissingRequiredClientCapability",
                ERR_UNSUPPORTED_VERSION: "UnsupportedProtocolVersion",
            },
        )
        self.assertEqual(
            (ERR_INVALID_REQUEST, ERR_METHOD_NOT_FOUND, ERR_INVALID_PARAMS, ERR_INTERNAL),
            (-32600, -32601, -32602, -32603),
        )
        self.assertEqual((ERR_HEADER_MISMATCH, ERR_MISSING_CAPABILITY, ERR_UNSUPPORTED_VERSION), (-32020, -32021, -32022))
        for entry in self.meta["errorCodes"]:
            self.assertTrue(entry["description"].strip())


class TokenTextTest(unittest.TestCase):
    def test_forms(self) -> None:
        token = encode_config_token({"serverEra": "legacy"})
        for text in (
            token,
            f"  {token}\n",
            f"cfg={token}",
            f"cfg:{token}",
            f"/mcp?cfg={token}",
            f"http://localhost:8000/mcp?cfg={token}",
            f"https://mcp.example/mcp?x=1&cfg={token}#frag",
        ):
            with self.subTest(text=text):
                self.assertEqual(config_token_from_text(text).strip(), token)

    def test_rejections(self) -> None:
        for text in ("", "   ", None, 12, ["a"], "http://localhost:8000/mcp", "/mcp?other=1", "x=1"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    config_token_from_text(text)


class ScenarioApiTest(HttpServerTestCase):
    def get_json(self, path: str) -> tuple[int, dict[str, str], Any]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(raw)
        finally:
            conn.close()

    def scenario(self, payload: Any) -> tuple[int, Any]:
        status, headers, raw = self.request(
            "POST",
            "/scenario",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload).encode("utf-8"),
        )
        self.assertEqual(headers["content-type"], "application/json")
        return status, json.loads(raw)

    # -- GET /scenario/meta -----------------------------------------------------
    def test_meta_endpoint(self) -> None:
        status, headers, body = self.get_json("/scenario/meta")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(body, json.loads(json.dumps(scenario_meta())))

    def test_meta_does_not_depend_on_the_active_config(self) -> None:
        before = self.get_json("/scenario/meta")[2]
        set_active_config({"serverEra": "legacy", "toolsList": {"mode": "empty"}})
        self.assertEqual(self.get_json("/scenario/meta")[2], before)

    def test_meta_only_allows_get(self) -> None:
        status, headers, _ = self.request("PUT", "/scenario/meta")
        self.assertEqual((status, headers["allow"]), (405, "GET, OPTIONS"))
        status, _, _ = self.request("POST", "/scenario/meta")
        self.assertEqual(status, 404)

    # -- validate ---------------------------------------------------------------
    def test_validate_returns_the_config_its_compact_form_and_token(self) -> None:
        raw = {"toolCall": {"mode": "sse"}, "serverEra": "modern"}
        status, body = self.scenario({"action": "validate", "config": raw})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "action", "config", "compact", "token"})
        self.assertIs(body["ok"], True)
        self.assertEqual(body["config"], normalize_config(raw))
        self.assertEqual(body["compact"], compact_config(normalize_config(raw)))
        self.assertEqual(body["compact"]["rejectLegacy"], True)  # derived from serverEra
        self.assertEqual(body["token"], encode_config_token(raw))
        self.assertEqual(decode_config_token(body["token"]), body["config"])

    def test_validate_of_the_defaults_has_an_empty_token(self) -> None:
        status, body = self.scenario({"action": "validate", "config": {}})
        self.assertEqual((status, body["compact"], body["token"]), (200, {}, ""))
        self.assertEqual(body["config"], default_config())

    def test_validate_reports_the_normalize_config_message(self) -> None:
        for raw in (
            {"toolCall": {"mode": "nope"}},
            {"serverEra": "future"},
            {"discover": {"httpStatus": 200}},
            {"strict2026": "yes"},
            {"rawResponses": [{"method": ""}]},
            {"toolErrors": ["nope"]},
        ):
            with self.subTest(config=raw):
                with self.assertRaises(ValueError) as caught:
                    normalize_config(raw)
                status, body = self.scenario({"action": "validate", "config": raw})
                self.assertEqual(status, 400)
                self.assertEqual(body, {"ok": False, "error": str(caught.exception)})

    def test_validate_needs_a_config_object(self) -> None:
        for payload in ({"action": "validate"}, {"action": "validate", "config": "x"}, {"action": "validate", "config": []}):
            with self.subTest(payload=payload):
                status, body = self.scenario(payload)
                self.assertEqual((status, body), (400, {"ok": False, "error": "config must be an object"}))

    # -- decode -----------------------------------------------------------------
    def test_decode_accepts_a_token_or_a_url(self) -> None:
        raw = {"serverEra": "legacy", "legacyModernProbe": {"mode": "http-400"}}
        token = encode_config_token(raw)
        for text in (token, f"http://localhost:8000/mcp?cfg={token}", f"/mcp?cfg={token}", f"cfg={token}"):
            with self.subTest(text=text):
                status, body = self.scenario({"action": "decode", "token": text})
                self.assertEqual(status, 200)
                self.assertEqual(set(body), {"ok", "action", "config", "compact", "token"})
                self.assertEqual(body["config"], normalize_config(raw))
                self.assertEqual(body["compact"], raw)
                self.assertEqual(body["token"], token)

    def test_decode_returns_the_canonical_token(self) -> None:
        # An old token that only says rejectLegacy decodes to serverEra=modern.
        import base64

        old = base64.urlsafe_b64encode(b'{"rejectLegacy":true}').decode("ascii").rstrip("=")
        status, body = self.scenario({"action": "decode", "token": old})
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["serverEra"], "modern")
        self.assertEqual(body["token"], encode_config_token({"serverEra": "modern"}))

    def test_decode_errors(self) -> None:
        import base64

        bad_value = base64.urlsafe_b64encode(b'{"discover":{"mode":"nope"}}').decode("ascii").rstrip("=")
        cases = (
            ("!!!", "invalid config token"),
            ("http://localhost:8000/mcp", "no cfg parameter in the URL"),
            ("", "token must be a non-empty string"),
            (None, "token must be a non-empty string"),
            (bad_value, "discover.mode must be one of"),
        )
        for text, message in cases:
            with self.subTest(text=text):
                status, body = self.scenario({"action": "decode", "token": text})
                self.assertEqual(status, 400)
                self.assertIs(body["ok"], False)
                self.assertIn(message, body["error"])

    # -- no global state --------------------------------------------------------
    def test_validate_and_decode_do_not_change_server_state(self) -> None:
        set_active_config({"toolsList": {"mode": "empty"}, "mcp": {"identifier": "kept"}})
        status, headers, _, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(status, 200)
        config_before = get_active_config()
        scenario_before = get_active_scenario()
        sessions_before = set(SESSIONS)
        self.assertEqual(scenario_before, "custom")
        self.assertTrue(sessions_before)

        token = encode_config_token({"serverEra": "modern", "toolsList": {"mode": "null"}})
        for payload in (
            {"action": "validate", "config": {"serverEra": "modern", "toolsList": {"mode": "null"}}},
            {"action": "validate", "config": {}},
            {"action": "validate", "config": {"toolCall": {"mode": "nope"}}},
            {"action": "decode", "token": token},
            {"action": "decode", "token": "!!!"},
        ):
            self.scenario(payload)
            with self.subTest(payload=payload):
                self.assertEqual(get_active_config(), config_before)
                self.assertEqual(get_active_scenario(), scenario_before)
                self.assertEqual(set(SESSIONS), sessions_before)

        # The server still answers with the active config, not the validated one.
        status, _, body, _ = self.post(legacy_request("tools/list"))
        self.assertEqual((status, body["result"]["tools"]), (200, []))
        self.get_json("/scenario/meta")
        self.assertEqual(get_active_config(), config_before)

    # -- existing request forms -------------------------------------------------
    def test_existing_forms_are_unchanged(self) -> None:
        status, body = self.scenario({"action": "expireSessions"})
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"ok", "action", "expired"})
        self.assertEqual((body["ok"], body["action"]), (True, "expireSessions"))

        status, body = self.scenario({"action": "nope"})
        self.assertEqual((status, body), (400, {"ok": False, "error": "unknown action 'nope'"}))

        status, body = self.scenario({"config": {"toolsList": {"mode": "empty"}}})
        self.assertEqual((status, body["scenario"]), (200, "custom"))
        self.assertEqual(get_active_config()["toolsList"]["mode"], "empty")

        status, body = self.scenario({"scenario": "tools-list-null"})
        self.assertEqual((status, body["scenario"]), (200, "tools-list-null"))
        self.assertEqual(set(body), {"ok", "scenario", "config"})
        self.assertEqual(get_active_config()["toolsList"]["mode"], "null")


if __name__ == "__main__":
    unittest.main()
