"""UI_CORE_JS, the DOM-free screen logic both control pages inline, run under node.

Skipped when node is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from unit_expert_mcp.server import (
    CLIENT_COMMAND_PREFIX,
    PREVIEW_SOURCE,
    PROTOCOL_VERSION_2026,
    TEST_SCENARIO_HEADER,
    UI_CORE_JS,
    _merge_config_patch,
    compact_config,
    decode_config_token,
    decode_mcp_header_value,
    default_config,
    encode_config_token,
    normalize_config,
    render_inspect_page,
    render_scenario_page,
    scenario_meta,
)

NODE = shutil.which("node")

DRIVER = """
const UI = require(process.argv[2]);
const calls = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const results = calls.map(([name, ...args]) => {
  try {
    return { value: UI[name](...args) };
  } catch (error) {
    return { error: error.message };
  }
});
process.stdout.write(JSON.stringify(results));
"""

RAW_RULE = {
    "enabled": True,
    "method": "tools/list",
    "status": 200,
    "headers": {"X-Test": "값"},
    "contentType": "json",
    "body": '{"jsonrpc":"2.0","id":"{{id}}","result":{"tools":[]}}',
}


def merged(patch: dict[str, Any]) -> dict[str, Any]:
    config = default_config()
    _merge_config_patch(config, patch)
    return config


@unittest.skipUnless(NODE, "node is not installed")
class UiCoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workdir = tempfile.TemporaryDirectory()
        root = Path(cls.workdir.name)
        cls.core_path = root / "ui_core.js"
        cls.core_path.write_text(UI_CORE_JS, encoding="utf-8")
        cls.driver_path = root / "driver.js"
        cls.driver_path.write_text(DRIVER, encoding="utf-8")
        cls.meta = scenario_meta()
        cls.defaults = cls.meta["defaults"]
        cls.schema = cls.meta["options"]
        cls.options = {option["path"]: option for option in cls.schema}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()

    def run_calls(self, calls: list[list[Any]]) -> list[dict[str, Any]]:
        calls_path = Path(self.workdir.name) / "calls.json"
        calls_path.write_text(json.dumps(calls, ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [NODE, str(self.driver_path), str(self.core_path), str(calls_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**os.environ, "TZ": "Asia/Seoul"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def call(self, name: str, *args: Any) -> Any:
        result = self.run_calls([[name, *args]])[0]
        self.assertNotIn("error", result, result)
        return result.get("value")


class ModuleShapeTest(UiCoreTestCase):
    def test_both_pages_inline_the_module(self) -> None:
        self.assertIn(UI_CORE_JS, render_scenario_page())
        self.assertIn(UI_CORE_JS, render_inspect_page())

    def test_it_exports_for_node_and_has_no_open_slot(self) -> None:
        self.assertIn('if (typeof module !== "undefined") module.exports', UI_CORE_JS)
        self.assertNotRegex(UI_CORE_JS, r"__[A-Z0-9_]+__")
        for word in ("document.", "window.", "location."):
            self.assertNotIn(word, UI_CORE_JS)


class TokenTest(UiCoreTestCase):
    def patches(self) -> dict[str, dict[str, Any]]:
        cases = {preset["id"]: preset["patch"] for preset in self.meta["presets"]}
        cases.update(
            {
                "korean-service-name": {"mcp": {"serviceName": "단위 전문가 (테스트) ✓", "identifier": "유닛"}},
                "raw-responses": {"rawResponses": [RAW_RULE]},
                "raw-and-korean": {
                    "mcp": {"serviceName": '따옴표 " 역슬래시 \\ 줄\n바꿈'},
                    "rawResponses": [RAW_RULE, {**RAW_RULE, "method": "*", "contentType": "sse", "status": 503}],
                },
                "floats": {
                    "toolCall": {"delaySeconds": 2},
                    "discover": {"delaySeconds": 0.5},
                    "legacyModernProbe": {"delaySeconds": 0.00001},
                    "server": {"delayEnabled": True, "delaySeconds": 7},
                },
                "lists": {
                    "toolErrors": ["invalid-tool-name-length", "long-description"],
                    "allowedOrigins": ["https://a.example"],
                    "auth": {"mode": "mixed-oauth", "protectedTools": ["convert_length"]},
                    "modern": {"requiredClientCapabilities": ["sampling"]},
                },
                "hidden-fields": {"protocolEra": "2026", "toolsList": {"tooManyCount": 30, "mode": "too-many"}},
            }
        )
        return cases

    def test_js_token_has_the_bytes_of_the_python_token(self) -> None:
        cases = self.patches()
        normalized = {name: normalize_config(merged(patch)) for name, patch in cases.items()}
        calls = [["encodeToken", normalized[name], self.defaults, self.schema] for name in cases]
        # The page holds configs the server has not normalized yet.
        calls += [["encodeToken", merged(cases[name]), self.defaults, self.schema] for name in cases]
        results = self.run_calls(calls)
        for index, name in enumerate(list(cases) * 2):
            with self.subTest(name=name, normalized=index < len(cases)):
                expected = encode_config_token(normalized[name])
                self.assertTrue(expected)
                self.assertEqual(results[index].get("value"), expected)

    def test_default_config_has_an_empty_token(self) -> None:
        self.assertEqual(self.call("encodeToken", default_config(), self.defaults, self.schema), "")

    def test_python_token_decodes_in_js_and_back(self) -> None:
        cases = self.patches()
        normalized = {name: normalize_config(merged(patch)) for name, patch in cases.items()}
        tokens = {name: encode_config_token(config) for name, config in normalized.items()}
        results = self.run_calls(
            [["decodeToken", tokens[name], self.defaults] for name in cases]
            + [["decodeToken", tokens[name]] for name in cases]
        )
        for index, name in enumerate(cases):
            with self.subTest(name=name):
                self.assertEqual(results[index]["value"], normalized[name])
                self.assertEqual(results[index + len(cases)]["value"], compact_config(normalized[name]))

    def test_js_token_decodes_in_python(self) -> None:
        patch = {"mcp": {"serviceName": "한글 서비스"}, "rawResponses": [RAW_RULE], "serverEra": "modern"}
        token = self.call("encodeToken", merged(patch), self.defaults, self.schema)
        decoded = decode_config_token(token)
        self.assertEqual(decoded["mcp"]["serviceName"], "한글 서비스")
        self.assertEqual(decoded["rawResponses"], [RAW_RULE])
        self.assertEqual((decoded["serverEra"], decoded["rejectLegacy"]), ("modern", True))

    def test_raw_response_rule_gets_the_server_defaults(self) -> None:
        patch = {"rawResponses": [{"method": " tools/call ", "body": "{}"}]}
        token = self.call("encodeToken", merged(patch), self.defaults, self.schema)
        self.assertEqual(token, encode_config_token(merged(patch)))

    def test_derived_fields_follow_the_server(self) -> None:
        # duplicateToolName is dropped unless the list mode can show it;
        # rejectLegacy alone promotes the default era to modern.
        for patch in (
            {"toolsList": {"mode": "empty", "duplicateToolName": True}},
            {"rejectLegacy": True},
            {"serverEra": "legacy", "rejectLegacy": True},
        ):
            with self.subTest(patch=patch):
                token = self.call("encodeToken", merged(patch), self.defaults, self.schema)
                self.assertEqual(token, encode_config_token(merged(patch)))

    def test_bad_tokens_raise(self) -> None:
        results = self.run_calls([["decodeToken", "%%%"], ["decodeToken", "e30x"], ["decodeToken", "  "]])
        self.assertEqual(results[0]["error"], "invalid config token")
        self.assertEqual(results[1]["error"], "invalid config token")
        self.assertEqual(results[2]["error"], "empty config token")

    def test_compact_config_equals_the_server(self) -> None:
        for name, patch in self.patches().items():
            with self.subTest(name=name):
                config = normalize_config(merged(patch))
                self.assertEqual(self.call("compactConfig", config, self.defaults), compact_config(config))


class PatchTest(UiCoreTestCase):
    def test_every_preset_patch_merges_like_the_server(self) -> None:
        presets = self.meta["presets"]
        results = self.run_calls(
            [["applyPatch", self.defaults, preset["patch"]] for preset in presets]
            + [["patchToOverrides", preset["patch"], self.schema] for preset in presets]
        )
        for index, preset in enumerate(presets):
            with self.subTest(preset=preset["id"]):
                self.assertEqual(results[index]["value"], merged(preset["patch"]))
                overrides = results[index + len(presets)]["value"]
                self.assertTrue(overrides)
                self.assertLessEqual(set(overrides), set(self.options))
                via_form = self.call("mergeFormOverrides", self.defaults, overrides)
                token = self.call("encodeToken", via_form, self.defaults, self.schema)
                self.assertEqual(token, encode_config_token(merged(preset["patch"])))

    def test_apply_patch_does_not_change_its_input(self) -> None:
        base = default_config()
        self.call("applyPatch", base, {"toolCall": {"mode": "sse"}})
        self.assertEqual(base, default_config())

    def test_get_and_set_path(self) -> None:
        results = self.run_calls(
            [
                ["getPath", {"a": {"b": 3}}, "a.b"],
                ["getPath", {"a": {"b": 3}}, "a.x.y"],
                ["setPath", {"a": {"b": 3}}, "a.c.d", [1]],
            ]
        )
        self.assertEqual(results[0]["value"], 3)
        self.assertNotIn("value", results[1])
        self.assertEqual(results[2]["value"], {"a": {"b": 3, "c": {"d": [1]}}})

    def test_overrides_keep_fields_the_form_has_no_control_for(self) -> None:
        loaded = normalize_config(
            merged(
                {
                    "protocolEra": "2026",
                    "rawResponses": [RAW_RULE],
                    "server": {"target": "initialize", "delaySeconds": 7, "httpStatus": 401},
                    "toolsList": {"tooManyCount": 30},
                    "auth": {"protectedTools": ["convert_area"]},
                }
            )
        )
        result = self.call("mergeFormOverrides", loaded, {"toolCall.mode": "sse", "toolCall.delaySeconds": 1.5})
        expected = json.loads(json.dumps(loaded))
        expected["toolCall"]["mode"] = "sse"
        expected["toolCall"]["delaySeconds"] = 1.5
        self.assertEqual(result, expected)
        token = self.call("encodeToken", result, self.defaults, self.schema)
        decoded = decode_config_token(token)
        self.assertEqual(decoded["protocolEra"], "2026")
        self.assertEqual(decoded["rawResponses"], [RAW_RULE])
        self.assertEqual(decoded["server"]["target"], "initialize")
        self.assertEqual(decoded["server"]["delaySeconds"], 7)
        self.assertEqual(decoded["toolsList"]["tooManyCount"], 30)
        self.assertEqual(decoded["auth"]["protectedTools"], ["convert_area"])
        self.assertEqual(decoded["toolCall"]["mode"], "sse")

    def test_changing_the_era_moves_reject_legacy_with_it(self) -> None:
        loaded = normalize_config({"serverEra": "modern"})
        self.assertIs(loaded["rejectLegacy"], True)
        result = self.call("mergeFormOverrides", loaded, {"serverEra": "both"})
        self.assertEqual((result["serverEra"], result["rejectLegacy"]), ("both", False))
        self.assertEqual(self.call("encodeToken", result, self.defaults, self.schema), "")


class OptionStateTest(UiCoreTestCase):
    def state(self, path: str, patch: dict[str, Any]) -> dict[str, Any]:
        return self.call("optionState", self.options[path], merged(patch), self.schema)

    def test_defaults(self) -> None:
        results = self.run_calls([["optionState", option, self.defaults, self.schema] for option in self.schema])
        for option, result in zip(self.schema, results):
            with self.subTest(path=option["path"]):
                state = result["value"]
                self.assertEqual(state["effective"], option["dependsOn"] is None or state["reason"] == "")
                self.assertEqual(bool(state["reason"]), not state["effective"])

    def test_modern_only_option_on_a_legacy_server(self) -> None:
        state = self.state("discover.mode", {"serverEra": "legacy"})
        self.assertEqual(state, {"effective": False, "reason": "지원 스펙이 '2025 방식만'이라 적용되지 않습니다"})
        self.assertTrue(self.state("discover.mode", {"serverEra": "modern"})["effective"])
        self.assertTrue(self.state("discover.mode", {})["effective"])

    def test_legacy_only_option_on_a_modern_server(self) -> None:
        state = self.state("session.issueId", {"serverEra": "modern"})
        self.assertEqual(state, {"effective": False, "reason": "지원 스펙이 '2026-07-28만'이라 적용되지 않습니다"})
        self.assertTrue(self.state("session.issueId", {"serverEra": "legacy"})["effective"])

    def test_depends_on_values(self) -> None:
        state = self.state("toolCall.errorCode", {})
        self.assertEqual(
            state, {"effective": False, "reason": "'tools/call 응답' 값이 json-rpc-error일 때만 적용됩니다"}
        )
        self.assertTrue(self.state("toolCall.errorCode", {"toolCall": {"mode": "json-rpc-error"}})["effective"])
        state = self.state("server.target", {})
        self.assertEqual(state["reason"], "'요청 막기' 값이 401 또는 403일 때만 적용됩니다")

    def test_depends_on_minimum(self) -> None:
        state = self.state("toolsList.failAtPage", {})
        self.assertEqual(state, {"effective": False, "reason": "'한 페이지의 툴 수' 값이 1 이상일 때만 적용됩니다"})
        self.assertTrue(self.state("toolsList.failAtPage", {"toolsList": {"pageSize": 2}})["effective"])

    def test_strict_only(self) -> None:
        state = self.state("modern.testTools", {"strict2026": False})
        self.assertEqual(state, {"effective": False, "reason": "엄격 검증이 꺼져 있어 적용되지 않습니다"})
        self.assertTrue(self.state("strict2026", {"strict2026": False})["effective"])

    def test_condition_of_the_option_it_depends_on(self) -> None:
        # timeout is set, but the probe mode itself only matters on a legacy server.
        patch = {"legacyModernProbe": {"mode": "timeout"}}
        state = self.state("legacyModernProbe.delaySeconds", patch)
        self.assertEqual(state, {"effective": False, "reason": "'지원 스펙' 값이 legacy일 때만 적용됩니다"})
        self.assertTrue(self.state("legacyModernProbe.delaySeconds", {**patch, "serverEra": "legacy"})["effective"])


class DiffSummaryTest(UiCoreTestCase):
    def test_defaults_have_no_chip(self) -> None:
        self.assertEqual(self.call("diffSummary", default_config(), self.defaults, self.schema), [])

    def test_chips_carry_label_and_display_value(self) -> None:
        config = normalize_config(
            merged(
                {
                    "serverEra": "modern",
                    "toolCall": {"mode": "http-status", "errorCode": -32000},
                    "session": {"validate": True},
                    "toolErrors": ["invalid-tool-name-length"],
                    "rawResponses": [RAW_RULE],
                    "protocolEra": "2026",
                }
            )
        )
        chips = self.call("diffSummary", config, self.defaults, self.schema)
        by_path = {chip["path"]: chip for chip in chips}
        self.assertEqual(
            list(by_path),
            [
                "serverEra",
                "session.validate",
                "toolCall.mode",
                "toolCall.errorCode",
                "toolErrors",
                "rawResponses",
                "protocolEra",
            ],
        )
        self.assertEqual((by_path["serverEra"]["label"], by_path["serverEra"]["value"]), ("지원 스펙", "modern"))
        self.assertEqual(by_path["toolErrors"]["value"], "invalid-tool-name-length")
        self.assertEqual(by_path["rawResponses"]["value"], "규칙 1개")
        self.assertEqual(by_path["session.validate"]["value"], "켬")
        # Without effect here: wrong mode, and a legacy option on a modern server.
        self.assertIs(by_path["toolCall.errorCode"]["effective"], False)
        self.assertIs(by_path["session.validate"]["effective"], False)
        self.assertIs(by_path["toolCall.mode"]["effective"], True)
        # A field without a control is shown by its key; rejectLegacy is not repeated.
        self.assertEqual(by_path["protocolEra"], {**by_path["protocolEra"], "label": "protocolEra", "hidden": True})
        self.assertNotIn("rejectLegacy", by_path)

    def test_format_value(self) -> None:
        results = self.run_calls(
            [
                ["formatValue", False, None],
                ["formatValue", [], None],
                ["formatValue", ["a", "b"], None],
                ["formatValue", "x" * 80, None],
                ["formatValue", [], {"kind": "json"}],
            ]
        )
        self.assertEqual([result["value"] for result in results[:3]], ["끔", "비움", "a, b"])
        self.assertEqual(len(results[3]["value"]), 60)
        self.assertEqual(results[4]["value"], "비움")

    def test_local_errors(self) -> None:
        config = merged({"toolCall": {"httpStatus": 200, "sseNotificationCount": 1.5}})
        errors = self.call("localErrors", config, self.schema)
        self.assertEqual(
            errors,
            [
                {"path": "toolCall.httpStatus", "message": "toolCall.httpStatus는 400~599 사이여야 합니다"},
                {
                    "path": "toolCall.sseNotificationCount",
                    "message": "toolCall.sseNotificationCount는 정수여야 합니다",
                },
            ],
        )
        self.assertEqual(self.call("localErrors", default_config(), self.schema), [])


class CommandTest(UiCoreTestCase):
    URL = "https://mcp.example/mcp?cfg=eyJhIjoxfQ"

    def test_mcp_url(self) -> None:
        results = self.run_calls(
            [
                ["buildMcpUrl", "https://mcp.example", "eyJhIjoxfQ"],
                ["buildMcpUrl", "http://localhost:8000/", ""],
                ["scenarioHeaderLine", "eyJhIjoxfQ"],
                ["scenarioHeaderLine", ""],
            ]
        )
        self.assertEqual(results[0]["value"], self.URL)
        self.assertEqual(results[1]["value"], "http://localhost:8000/mcp")
        self.assertEqual(results[2]["value"], f"{TEST_SCENARIO_HEADER}: cfg:eyJhIjoxfQ")
        self.assertEqual(results[3]["value"], "")

    def test_legacy_initialize_curl(self) -> None:
        command = self.call("buildCurl", "legacy-initialize", self.URL)
        lines = command.split(" \\\n  ")
        self.assertEqual(lines[0], f"curl -i -sS -X POST '{self.URL}'")
        self.assertIn("-H 'Accept: application/json, text/event-stream'", lines)
        self.assertIn("-H 'Content-Type: application/json'", lines)
        self.assertTrue(lines[-1].startswith("-d '"))
        body = json.loads(lines[-1][4:-1])
        self.assertEqual(body["method"], "initialize")
        self.assertEqual(body["params"]["capabilities"], {})
        self.assertNotIn("Mcp-Method", command)

    def test_modern_tools_list_curl(self) -> None:
        command = self.call("buildCurl", "modern-tools-list", self.URL)
        self.assertIn(f"-H 'MCP-Protocol-Version: {PROTOCOL_VERSION_2026}'", command)
        self.assertIn("-H 'Mcp-Method: tools/list'", command)
        body = json.loads(command.split("-d '")[1][:-1])
        self.assertEqual(body["method"], "tools/list")
        meta = body["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], PROTOCOL_VERSION_2026)
        self.assertEqual(meta["io.modelcontextprotocol/clientCapabilities"], {})

    def test_curl_quotes_a_single_quote_and_rejects_unknown_kinds(self) -> None:
        results = self.run_calls(
            [["buildCurl", "legacy-tools-list", "http://h/mcp?cfg=a'b"], ["buildCurl", "nope", "http://h/mcp"]]
        )
        self.assertIn("'http://h/mcp?cfg=a'\\''b'", results[0]["value"])
        self.assertIn("unknown request kind", results[1]["error"])

    def test_cli_commands(self) -> None:
        results = self.run_calls(
            [
                ["buildCliCommand", "probe", self.URL],
                ["buildCliCommand", "run all", self.URL, {"expect": "server"}],
                ["buildCliCommand", "run legacy", self.URL, {"expect": "gateway", "prefix": CLIENT_COMMAND_PREFIX}],
            ]
        )
        self.assertEqual(results[0]["value"], f"python3 -m unit_expert_mcp.client probe --url '{self.URL}'")
        self.assertEqual(
            results[1]["value"], f"python3 -m unit_expert_mcp.client run all --expect server --url '{self.URL}'"
        )
        self.assertEqual(
            results[2]["value"], f"{CLIENT_COMMAND_PREFIX} run legacy --expect gateway --url '{self.URL}'"
        )

    def test_option_search(self) -> None:
        option = self.options["toolCall.mode"]
        results = self.run_calls(
            [
                ["matchOptionSearch", option, ""],
                ["matchOptionSearch", option, "TOOLCALL.mode"],
                ["matchOptionSearch", option, "tools/call 응답"],
                ["matchOptionSearch", option, "disconnect"],
                ["matchOptionSearch", option, "tools/call 세션"],
                ["matchOptionSearch", self.options["initialize.protocolVersion"], "2024-03-26"],
            ]
        )
        self.assertEqual([result["value"] for result in results], [True, True, True, True, False, True])


class ExchangeTest(UiCoreTestCase):
    def exchange(self, **fields: Any) -> dict[str, Any]:
        entry = {
            "seq": 1,
            "httpMethod": "POST",
            "path": "/mcp",
            "query": "",
            "cfg": None,
            "configSource": "global",
            "source": None,
            "era": "2025",
            "method": "tools/list",
            "requestBody": {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            "status": 200,
            "responseBody": {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}},
            "rawResponse": False,
            "injected": [],
        }
        entry.update(fields)
        return entry

    def samples(self) -> dict[str, dict[str, Any]]:
        rejected = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32022, "message": "Unsupported protocol version"}}
        return {
            "ok": self.exchange(),
            "injected": self.exchange(seq=2, injected=["toolCall.mode=is-error"], method="tools/call"),
            "rejected": self.exchange(seq=3, status=400, responseBody=rejected, era="2026", cfg="abc"),
            "plain-400": self.exchange(seq=4, status=400, responseBody={"error": "invalid config token"}),
            "raw": self.exchange(seq=5, rawResponse=True, injected=["rawResponses"], status=500),
            "closed": self.exchange(seq=6, status=0, responseBody={"disconnect": True}, injected=["toolCall.mode=disconnect"]),
            "preview": self.exchange(seq=7, source=PREVIEW_SOURCE, cfg="abc", configSource="url"),
        }

    def test_classify(self) -> None:
        samples = self.samples()
        results = self.run_calls(
            [["classifyExchange", entry, self.meta["errorCodes"]] for entry in samples.values()]
        )
        kinds = {name: result["value"] for name, result in zip(samples, results)}
        self.assertEqual(
            {name: (value["kind"], value["label"]) for name, value in kinds.items()},
            {
                "ok": ("ok", "정상"),
                "injected": ("injected", "넣은 에러"),
                "rejected": ("rejected", "서버가 거절"),
                "plain-400": ("rejected", "서버가 거절"),
                "raw": ("raw", "직접 지정"),
                "closed": ("closed", "연결 종료"),
                "preview": ("ok", "정상"),
            },
        )
        self.assertEqual(kinds["rejected"]["code"], -32022)
        self.assertEqual(kinds["rejected"]["codeName"], "UnsupportedProtocolVersion")
        self.assertIn("-32022 UnsupportedProtocolVersion", kinds["rejected"]["detail"])
        self.assertEqual(kinds["plain-400"]["detail"], "HTTP 400: invalid config token")
        self.assertEqual(kinds["injected"]["detail"], "toolCall.mode=is-error")

    def test_filter(self) -> None:
        entries = list(self.samples().values())
        filters = [
            {},
            {"cfg": "abc"},
            {"cfg": ""},
            {"hidePreview": True},
            {"kind": "rejected"},
            {"kind": ["raw", "closed"]},
            {"errorsOnly": True},
            {"status": "4xx"},
            {"status": "0"},
            {"era": "2026"},
            {"method": "tools/call"},
            {"configSource": "url"},
            {"text": "unsupported PROTOCOL"},
            {"cfg": "abc", "hidePreview": True},
        ]
        results = self.run_calls([["filterExchanges", entries, item] for item in filters])
        seqs = [[entry["seq"] for entry in result["value"]] for result in results]
        self.assertEqual(
            seqs,
            [
                [1, 2, 3, 4, 5, 6, 7],
                [3, 7],
                [1, 2, 4, 5, 6],
                [1, 2, 3, 4, 5, 6],
                [3, 4],
                [5, 6],
                [2, 3, 4, 5, 6],
                [3, 4],
                [6],
                [3],
                [2],
                [7],
                [3],
                [3],
            ],
        )

    def test_mask_secret_headers(self) -> None:
        headers = {
            "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
            "authorization": "Bearer short",
            "Cookie": "sid=1234567890abcdef",
            "X-Api-Key": "k",
            "Accept": "application/json",
            "Mcp-Method": "tools/list",
            TEST_SCENARIO_HEADER: "cfg:eyJhIjoxfQ",
        }
        masked = self.call("maskSecretHeaders", headers)
        self.assertEqual(
            masked,
            {
                "Authorization": "Bearer ***wxyz (len 26)",
                "authorization": "Bearer ***",
                "Cookie": "***cdef (len 20)",
                "X-Api-Key": "***",
                "Accept": "application/json",
                "Mcp-Method": "tools/list",
                TEST_SCENARIO_HEADER: "cfg:eyJhIjoxfQ",
            },
        )
        self.assertEqual(headers["Authorization"], "Bearer abcdefghijklmnopqrstuvwxyz")

    def test_mask_secret_headers_keeps_what_holds_no_credential(self) -> None:
        headers = {
            "Authorization": "Bearer",
            "Proxy-Authorization": "my-secret-word abcdefghijkl",
            "Cookie": "",
            "Mcp-Param-Token": "abcdefghijklmnop",
            "WWW-Authenticate": 'Bearer resource_metadata="http://h/x"',
        }
        self.assertEqual(
            self.call("maskSecretHeaders", headers),
            dict(headers, **{"Proxy-Authorization": "***ijkl (len 27)"}),
        )

    def test_is_secret_header_matches_the_server(self) -> None:
        from unit_expert_mcp.server import is_secret_header

        names = [
            "Authorization", "proxy-authorization", "COOKIE", "Set-Cookie", "X-Api-Key", "x-apikey",
            "X-Api_Key", "X-Auth-Token", "X-MCP-Token", "X-Client-Secret", "X-Password",
            "Accept", "Content-Type", "WWW-Authenticate", "MCP-Protocol-Version", "Mcp-Param-Token",
            "Mcp-Session-Id", TEST_SCENARIO_HEADER, "X-Unit-Expert-Source",
        ]
        results = self.run_calls([["isSecretHeader", name] for name in names])
        self.assertEqual([result["value"] for result in results], [is_secret_header(name) for name in names])
        self.assertEqual(sum(result["value"] for result in results), 11)

    def test_format_local_time(self) -> None:
        # The node process runs with TZ=Asia/Seoul (UTC+9).
        results = self.run_calls(
            [
                ["formatLocalTime", "2026-01-02T03:04:05.678Z"],
                ["formatLocalTime", "2026-01-02T15:00:00.007Z"],
                ["formatLocalTime", "not a time"],
                ["formatLocalTime", None],
            ]
        )
        self.assertEqual(
            [result["value"] for result in results], ["12:04:05.678", "00:00:00.007", "", ""]
        )


class InspectHelperTest(UiCoreTestCase):
    """Helpers the /inspect page uses on top of classify/filter."""

    def exchange(self, **fields: Any) -> dict[str, Any]:
        entry = {
            "seq": 1,
            "httpMethod": "POST",
            "path": "/mcp",
            "query": "",
            "cfg": None,
            "source": None,
            "era": "2026",
            "method": "tools/call",
            "requestHeaders": {},
            "requestBody": None,
            "status": 200,
            "responseBody": None,
            "rawResponse": False,
            "injected": [],
        }
        entry.update(fields)
        return entry

    def test_screen_and_options_filters(self) -> None:
        entries = [
            self.exchange(seq=1),
            self.exchange(seq=2, source=PREVIEW_SOURCE),
            self.exchange(seq=3, source="inspect-console"),
            self.exchange(seq=4, source="scenario-page"),
            self.exchange(seq=5, httpMethod="OPTIONS", method=None),
            self.exchange(seq=6, requestHeaders={"Mcp-Name": "convert_mass"}, cfg="tokenXYZ"),
        ]
        filters = [
            {"hideScreen": True},
            {"hideOptions": True},
            {"hideScreen": True, "hideOptions": True},
            {"text": "convert_MASS"},
            {"text": "tokenxyz"},
            {"cfg": None, "method": "", "era": "", "status": "", "kind": "", "text": ""},
        ]
        results = self.run_calls([["filterExchanges", entries, item] for item in filters])
        self.assertEqual(
            [[entry["seq"] for entry in result["value"]] for result in results],
            [[1, 5, 6], [1, 2, 3, 4, 6], [1, 6], [6], [6], [1, 2, 3, 4, 5, 6]],
        )

    def test_tool_name(self) -> None:
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "convert_length"}}
        results = self.run_calls(
            [
                ["toolName", self.exchange(requestBody=call)],
                ["toolName", self.exchange(method="tools/list", requestBody={**call, "method": "tools/list"})],
                ["toolName", self.exchange(requestBody="not json")],
                ["toolName", self.exchange(requestBody={"method": "tools/call", "params": {"name": 3}})],
            ]
        )
        self.assertEqual([result["value"] for result in results], ["convert_length", "", "", ""])

    def test_format_local_stamp(self) -> None:
        # TZ=Asia/Seoul. 2026-01-02T15:00Z is 2026-01-03 00:00 local.
        now = "2026-01-03T01:00:00.000Z"
        results = self.run_calls(
            [
                ["formatLocalStamp", "2026-01-02T15:00:00.007Z", now],
                ["formatLocalStamp", "2026-01-02T14:59:59.999Z", now],
                ["formatLocalStamp", "2025-12-31T03:04:05.678Z", now],
                ["formatLocalStamp", "not a time", now],
            ]
        )
        self.assertEqual(
            [result["value"] for result in results],
            ["00:00:00.007", "01-02 23:59:59.999", "2025-12-31 12:04:05.678", ""],
        )

    def test_token_summary(self) -> None:
        token = encode_config_token(merged({"serverEra": "legacy", "toolCall": {"mode": "sse"}}))
        results = self.run_calls(
            [
                ["tokenSummary", token, self.defaults, self.schema],
                ["tokenSummary", "e30", self.defaults, self.schema],
                ["tokenSummary", "%%%", self.defaults, self.schema],
            ]
        )
        summary, same, bad = (result["value"] for result in results)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["text"], "지원 스펙 = legacy, tools/call 응답 = sse")
        self.assertEqual([chip["path"] for chip in summary["chips"]], ["serverEra", "toolCall.mode"])
        self.assertEqual((same["ok"], same["text"]), (True, "기본 설정과 같음"))
        self.assertEqual((bad["ok"], bad["text"], bad["chips"]), (False, "읽을 수 없는 설정 코드", []))

    def test_token_from_text(self) -> None:
        cases = {
            "eyJhIjoxfQ": "eyJhIjoxfQ",
            "  eyJhIjoxfQ== ": "eyJhIjoxfQ==",
            "cfg=eyJhIjoxfQ": "eyJhIjoxfQ",
            "cfg:eyJhIjoxfQ": "eyJhIjoxfQ",
            "http://localhost:8000/mcp?cfg=eyJhIjoxfQ&x=1": "eyJhIjoxfQ",
            "/inspect?kind=ok&cfg=a%2Db": "a-b",
            "/scenario#cfg=eyJhIjoxfQ": "eyJhIjoxfQ",
            "http://localhost:8000/mcp": "",
            "two words": "",
            "": "",
        }
        results = self.run_calls([["tokenFromText", text] for text in cases])
        self.assertEqual([result["value"] for result in results], list(cases.values()))

    def test_filters_and_query_round_trip(self) -> None:
        defaults = self.call("defaultFilters")
        self.assertEqual(
            defaults,
            {
                "text": "", "kind": "", "status": "", "era": "", "method": "", "cfg": "", "plain": False,
                "hideScreen": True, "hideOptions": True, "errorsOnly": False,
            },
        )
        chosen = {
            **defaults,
            "text": "a b&c",
            "kind": "rejected",
            "status": "4xx",
            "era": "2026",
            "method": "tools/call",
            "cfg": "eyJhIjoxfQ",
            "hideScreen": False,
            "hideOptions": False,
            "errorsOnly": True,
        }
        queries = self.run_calls(
            [["filtersToQuery", defaults], ["filtersToQuery", chosen], ["filtersToQuery", {**defaults, "cfg": "abc", "kind": "rejected"}]]
        )
        self.assertEqual(queries[0]["value"], "")
        self.assertEqual(
            queries[1]["value"],
            "?cfg=eyJhIjoxfQ&q=a%20b%26c&kind=rejected&status=4xx&era=2026&method=tools%2Fcall&screen=1&options=1&errors=1",
        )
        self.assertEqual(queries[2]["value"], "?cfg=abc&kind=rejected")
        parsed = self.run_calls(
            [
                ["filtersFromQuery", ""],
                ["filtersFromQuery", queries[1]["value"]],
                ["filtersFromQuery", "?cfg=abc"],
                ["filtersFromQuery", "?kind=nope&status=9xx&era=2024&screen=0&errors=yes"],
            ]
        )
        self.assertEqual(parsed[0]["value"], defaults)
        self.assertEqual(parsed[1]["value"], chosen)
        self.assertEqual(parsed[2]["value"], {**defaults, "cfg": "abc"})
        self.assertEqual(parsed[3]["value"], defaults)

    def test_decode_header_value(self) -> None:
        results = self.run_calls(
            [
                ["decodeHeaderValue", "convert_length"],
                ["decodeHeaderValue", "=?base64?7ZWc6riA?="],
                ["decodeHeaderValue", "=?base64?YQ==?="],
                ["decodeHeaderValue", "=?base64?!!!?="],
                ["decodeHeaderValue", "=?base64?/w==?="],
                ["decodeHeaderValue", "=?base64??="],
            ]
        )
        self.assertEqual(
            [result["value"] for result in results],
            [
                {"value": "convert_length", "encoded": False, "valid": True},
                {"value": "한글", "encoded": True, "valid": True},
                {"value": "a", "encoded": True, "valid": True},
                {"value": "=?base64?!!!?=", "encoded": True, "valid": False},
                {"value": "=?base64?/w==?=", "encoded": True, "valid": False},
                {"value": "", "encoded": True, "valid": True},
            ],
        )

    def test_header_body_rows(self) -> None:
        version_key = "io.modelcontextprotocol/protocolVersion"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "한글",
                "arguments": {"Region": "kr", "count": 3, "verbose": True},
                "_meta": {version_key: PROTOCOL_VERSION_2026},
            },
        }
        headers = {
            "mcp-protocol-version": PROTOCOL_VERSION_2026,
            "Mcp-Method": "tools/list",
            "Mcp-Name": "=?base64?7ZWc6riA?=",
            "Mcp-Param-Region": "kr",
            "Mcp-Param-Count": "4",
            "Mcp-Param-Verbose": "true",
            "Mcp-Param-Extra": "x",
        }
        results = self.run_calls(
            [
                ["headerBodyRows", self.exchange(requestHeaders=headers, requestBody=body)],
                ["headerBodyRows", self.exchange(era="2025", requestHeaders=headers, requestBody=body)],
                ["headerBodyRows", self.exchange(method="tools/list", requestBody={"method": "tools/list", "params": {}})],
                ["headerBodyRows", self.exchange(requestBody="broken", requestHeaders={"Mcp-Name": "=?base64?!!!?="})],
            ]
        )
        rows = results[0]["value"]
        self.assertEqual(
            [(row["header"], row["headerValue"], row["bodyLabel"], row["bodyValue"], row["state"]) for row in rows],
            [
                ("mcp-protocol-version", PROTOCOL_VERSION_2026, "_meta protocolVersion", PROTOCOL_VERSION_2026, "match"),
                ("Mcp-Method", "tools/list", "method", "tools/call", "differ"),
                ("Mcp-Name", "=?base64?7ZWc6riA?=", "params.name", "한글", "match"),
                ("Mcp-Param-Count", "4", "params.arguments.count", "3", "differ"),
                ("Mcp-Param-Extra", "x", "params.arguments.extra", None, "body-missing"),
                ("Mcp-Param-Region", "kr", "params.arguments.Region", "kr", "match"),
                ("Mcp-Param-Verbose", "true", "params.arguments.verbose", "true", "match"),
            ],
        )
        self.assertEqual([row["decodedValue"] for row in rows][:3], [None, None, "한글"])
        self.assertEqual({row["stateLabel"] for row in rows}, {"일치", "다름", "바디에 값 없음"})
        self.assertEqual(results[1]["value"], [])
        self.assertEqual(
            [(row["header"], row["state"], row["stateLabel"]) for row in results[2]["value"]],
            [("MCP-Protocol-Version", "none", "둘 다 없음"), ("Mcp-Method", "header-missing", "헤더 없음")],
        )
        self.assertEqual(
            [(row["header"], row["state"]) for row in results[3]["value"]],
            [("MCP-Protocol-Version", "none"), ("Mcp-Method", "none"), ("Mcp-Name", "invalid")],
        )

    def test_build_exchange_curl(self) -> None:
        entry = self.exchange(
            query="cfg=eyJhIjoxfQ",
            requestHeaders={
                "Content-Type": "application/json",
                "Authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
                "Mcp-Method": "tools/list",
            },
            requestBody={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"note": "it's"}},
        )
        results = self.run_calls(
            [
                ["buildExchangeCurl", entry, "http://localhost:8000/"],
                ["buildExchangeCurl", entry, "http://localhost:8000", True],
                ["buildExchangeCurl", self.exchange(httpMethod="GET", requestHeaders={"Accept": "text/event-stream"}), "http://h"],
                ["buildExchangeCurl", self.exchange(httpMethod="x; rm", requestBody="raw text"), "http://h"],
            ]
        )
        masked, revealed, get, odd = (result["value"] for result in results)
        self.assertEqual(masked["placeholders"], ["Authorization"])
        self.assertEqual(
            masked["command"],
            "curl -i -sS -X POST 'http://localhost:8000/mcp?cfg=eyJhIjoxfQ' \\\n"
            "  -H 'Content-Type: application/json' \\\n"
            "  -H 'Authorization: <Authorization 값>' \\\n"
            "  -H 'Mcp-Method: tools/list' \\\n"
            "  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{\"note\":\"it'\\''s\"}}'",
        )
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", masked["command"])
        self.assertEqual(revealed["placeholders"], [])
        self.assertIn("-H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz'", revealed["command"])
        self.assertEqual(get["command"], "curl -i -sS -X GET 'http://h/mcp' \\\n  -H 'Accept: text/event-stream'")
        self.assertEqual(odd["command"], "curl -i -sS -X POST 'http://h/mcp' \\\n  -d 'raw text'")


META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CAPS = "io.modelcontextprotocol/clientCapabilities"


class RequestPasteTest(UiCoreTestCase):
    """What the send screen reads from pasted text: a JSON-RPC body, a request
    bundle or a curl command."""

    def parse(self, text: str) -> dict[str, Any]:
        return self.call("parseRequestPaste", text)

    def test_a_json_rpc_body_of_each_mode(self) -> None:
        modern = {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "convert_length", "arguments": {"value": 3}, "_meta": {META_VERSION: "2026-07-28"}},
        }
        parsed = self.parse(json.dumps(modern))
        self.assertEqual((parsed["ok"], parsed["kind"]), (True, "jsonrpc"))
        request = parsed["request"]
        self.assertEqual(
            (request["mode"], request["method"], request["tool"], request["args"], request["id"]),
            ("2026", "tools/call", "convert_length", {"value": 3}, 7),
        )
        # The body is sent as it was pasted.
        self.assertEqual(request["body"], modern)
        self.assertEqual(request["url"], "")
        cases = {
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}': "2025",
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}': "2025",
            '{"jsonrpc":"2.0","id":1,"method":"server/discover"}': "2026",
            '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"%s":{}}}}' % META_CAPS: "2026",
        }
        for text, mode in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.parse(text)["request"]["mode"], mode)

    def test_a_request_bundle(self) -> None:
        bundle = {
            "url": "/mcp?cfg=abc", "mode": "2025", "method": "tools/call", "tool": "convert_weight",
            "args": {"value": 1, "from_unit": "kg", "to_unit": "g"}, "id": "x-1",
            "headers": {"Authorization": "Bearer t", "X-N": 3}, "omit": [],
        }
        parsed = self.parse(json.dumps(bundle))
        self.assertEqual((parsed["ok"], parsed["kind"]), (True, "bundle"))
        request = parsed["request"]
        self.assertEqual(
            {key: request[key] for key in ("url", "mode", "method", "tool", "args", "id", "omit", "exactHeaders")},
            {"url": "/mcp?cfg=abc", "mode": "2025", "method": "tools/call", "tool": "convert_weight",
             "args": bundle["args"], "id": "x-1", "omit": [], "exactHeaders": False},
        )
        self.assertEqual(request["headers"], {"Authorization": "Bearer t", "X-N": "3"})
        # The mode has the client's names too, "arguments" is read as args, and
        # only method is needed.
        for text, expected in (
            ('{"mode":"modern","method":"ping"}', "2026"),
            ('{"mode":"legacy","method":"ping"}', "2025"),
            ('{"mode":"2026-07-28","method":"ping"}', "2026"),
            ('{"method":"ping"}', "2026"),
        ):
            self.assertEqual(self.parse(text)["request"]["mode"], expected, text)
        self.assertEqual(
            self.parse('{"method":"tools/call","tool":"t","arguments":{"a":1}}')["request"]["args"], {"a": 1}
        )
        # A body alone gives the form its method, tool and mode.
        body_only = self.parse(json.dumps({"body": {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "t"}}}))
        self.assertEqual(
            (body_only["request"]["method"], body_only["request"]["tool"], body_only["request"]["mode"], body_only["request"]["id"]),
            ("tools/call", "t", "2025", 4),
        )

    def test_a_curl_command_made_by_the_screens_reads_back(self) -> None:
        url = "http://localhost:8000/mcp?cfg=eyJhIjoxfQ"
        commands = self.run_calls([["buildCurl", "modern-tools-list", url], ["buildCurl", "legacy-initialize", url]])
        modern, legacy = (self.parse(result["value"]) for result in commands)
        self.assertEqual((modern["kind"], modern["request"]["url"]), ("curl", url))
        self.assertEqual((modern["request"]["mode"], modern["request"]["method"]), ("2026", "tools/list"))
        self.assertEqual(
            modern["request"]["headers"],
            {"Content-Type": "application/json", "Accept": "application/json",
             "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"},
        )
        self.assertTrue(modern["request"]["exactHeaders"])
        self.assertEqual(json.loads(modern["request"]["bodyText"])["method"], "tools/list")
        self.assertEqual((legacy["request"]["mode"], legacy["request"]["method"]), ("2025", "initialize"))
        # The curl of a recorded exchange (with a quote in the body) too.
        entry = {
            "httpMethod": "POST", "path": "/mcp", "query": "cfg=x",
            "requestHeaders": {"Mcp-Method": "tools/call", "Mcp-Name": "it's"},
            "requestBody": {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "it's", "arguments": {"q": "a b"}}},
        }
        command = self.call("buildExchangeCurl", entry, "http://h", True)["command"]
        again = self.parse(command)["request"]
        self.assertEqual((again["url"], again["tool"], again["args"]), ("http://h/mcp?cfg=x", "it's", {"q": "a b"}))
        self.assertEqual(again["headers"]["Mcp-Name"], "it's")

    def test_curl_forms(self) -> None:
        results = self.run_calls(
            [
                ["parseCurl", 'curl --request POST --url "http://h/mcp" --header "A: b \\"c\\"" --data-raw \'{"x":1}\' -sS -o out.txt'],
                ["parseCurl", "curl http://h/mcp"],
                ["parseCurl", "curl --json '{}' http://h/mcp"],
                ["parseCurl", "curl -X POST"],
                ["parseCurl", "wget http://h"],
                ["parseCurl", "curl 'http://h"],
            ]
        )
        self.assertEqual(
            results[0]["value"], {"url": "http://h/mcp", "httpMethod": "POST", "headers": {"A": 'b "c"'}, "body": '{"x":1}'}
        )
        self.assertEqual((results[1]["value"]["httpMethod"], results[1]["value"]["body"]), ("GET", None))
        self.assertEqual(results[2]["value"]["headers"]["Content-Type"], "application/json")
        self.assertIn("주소를 찾지 못했습니다", results[3]["error"])
        self.assertIn("curl로 시작하는 명령이 아닙니다", results[4]["error"])
        self.assertIn("작은따옴표", results[5]["error"])

    def test_what_is_wrong_is_said_in_one_line(self) -> None:
        cases = {
            "": "붙여 넣은 내용이 없습니다",
            '{"method": "tools/list",}': "JSON을 읽지 못했습니다: 1번째 줄 25번째 글자 근처",
            '{\n  "method": "tools/list"\n  "mode": "2026"\n}': "JSON을 읽지 못했습니다: 3번째 줄 3번째 글자 근처",
            '{"method": "tools/list"': "JSON을 읽지 못했습니다: 1번째 줄 24번째 글자 근처(끝이 잘렸습니다)",
            "[1]": "JSON 객체({ … })여야 합니다",
            '{"jsonrpc":"2.0"}': "JSON-RPC 요청에 method가 없습니다",
            '{"methd":"x","method":"y"}': "모르는 필드: methd. 쓸 수 있는 필드: url, mode, method, tool, args, params, id, headers, omit, body",
            '{"url":"/mcp"}': "method(문자열)나 body가 필요합니다",
            '{"method":"ping","mode":"2024"}': 'mode는 "2026" 또는 "2025"여야 합니다',
            '{"method":"tools/call","args":[1]}': "args는 JSON 객체여야 합니다",
            '{"method":"ping","headers":{"A":{}}}': 'headers는 {"이름": "값"} 객체여야 합니다',
            '{"method":"ping","omit":["version"]}': "omit에는 다음만 넣을 수 있습니다: meta-version, capabilities, meta, version-header, method-header, name-header",
            "curl -X POST": "curl 명령을 읽지 못했습니다: curl 명령에서 주소를 찾지 못했습니다",
        }
        results = self.run_calls([["parseRequestPaste", text] for text in cases])
        for (text, expected), result in zip(cases.items(), results):
            with self.subTest(text=text):
                value = result["value"]
                self.assertFalse(value["ok"])
                self.assertTrue(value["error"].startswith(expected), value["error"])
                self.assertNotIn("\n", value["error"])

    def test_the_error_position_does_not_depend_on_the_engine(self) -> None:
        valid = ['{}', '[]', ' {"a": [1, -2.5e3, true, null, "x\\n\\u00e9"], "b": {}} ', '"s"', '0', json.dumps(RAW_RULE, ensure_ascii=False)]
        broken = {'{"a":}': 5, '{"a" 1}': 5, '[1,]': 3, '{"a":1,}': 7, '{a:1}': 1, '[1 2]': 3, '{"a":"x': 7, '': 0,
                  '{"a":1}}': 7, '[01]': 2, '{"a":"\\x"}': 7, "{'a':1}": 1}
        results = self.run_calls([["jsonErrorPosition", text] for text in valid + list(broken)])
        positions = [result["value"] for result in results]
        self.assertEqual(positions[: len(valid)], [-1] * len(valid))
        self.assertEqual(positions[len(valid):], list(broken.values()))
        for text in valid:
            json.loads(text)
        for text in broken:
            with self.assertRaises(ValueError):
                json.loads(text)

    def test_the_retry_of_an_input_required_result(self) -> None:
        meta = {"io.modelcontextprotocol/clientCapabilities": {"elicitation": {}}}
        sent = {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {
                "name": "convert_interactive", "arguments": {"value": 3, "from_unit": "km"}, "_meta": meta,
                "inputResponses": {"target_unit": {"action": "accept", "content": {"to_unit": "m"}}},
                "requestState": "old",
            },
        }

        def ask(properties: dict[str, Any]) -> dict[str, Any]:
            schema = {"type": "object", "properties": properties}
            return {"method": "elicitation/create", "params": {"mode": "form", "requestedSchema": schema}}

        result = {
            "resultType": "input_required",
            "requestState": "new",
            "inputRequests": {
                "precision": ask({"decimals": {"type": "integer", "minimum": 0, "maximum": 10}}),
                "unit": ask({"to_unit": {"type": "string", "enum": ["cm", "m"]}}),
                "mixed": ask({
                    "ok": {"type": "boolean"}, "name": {"type": "string"}, "low": {"type": "integer", "minimum": 5},
                    "high": {"type": "number", "maximum": 0.5}, "preset": {"type": "integer", "default": 7},
                }),
                "roots": {"method": "roots/list"},
                "other": {"method": "x/unknown"},
            },
        }
        bundle = self.call("continueRequest", "/mcp?cfg=abc", sent, result)
        self.assertEqual(
            {key: bundle[key] for key in ("url", "mode", "method", "tool", "args", "id")},
            {
                "url": "/mcp?cfg=abc", "mode": "2026", "method": "tools/call", "tool": "convert_interactive",
                "args": {"value": 3, "from_unit": "km"}, "id": 5,
            },
        )
        # The state as received, answers for this result's keys only, the rest of params kept.
        self.assertEqual(bundle["params"]["requestState"], "new")
        self.assertEqual(bundle["params"]["_meta"], meta)
        self.assertEqual(
            bundle["params"]["inputResponses"],
            {
                "precision": {"action": "accept", "content": {"decimals": 1}},
                "unit": {"action": "accept", "content": {"to_unit": "cm"}},
                "mixed": {
                    "action": "accept",
                    "content": {"ok": True, "name": "", "low": 5, "high": 0.5, "preset": 7},
                },
                "roots": {"roots": []},
                "other": {},
            },
        )
        # It is a bundle the paste box reads, and the draft carries the capability on.
        draft = self.draft(bundle)
        self.assertEqual(draft["body"]["id"], 5)
        self.assertEqual(draft["body"]["params"]["requestState"], "new")
        self.assertEqual(draft["body"]["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"], {"elicitation": {}})
        self.assertEqual(draft["headers"]["Mcp-Name"], "convert_interactive")

        only_state = self.call("continueRequest", "", {**sent, "id": "call-1"}, {"resultType": "input_required", "requestState": "s"})
        self.assertEqual(only_state["id"], "call-1-next")
        self.assertNotIn("url", only_state)
        self.assertNotIn("inputResponses", only_state["params"])
        for body, answer in (
            (sent, {"resultType": "complete", "content": []}),
            (sent, None),
            ({**sent, "method": "tools/list"}, result),
            (None, result),
            ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}, result),
        ):
            self.assertIsNone(self.call("continueRequest", "/mcp", body, answer))

    def draft(self, bundle: dict[str, Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
        request = self.parse(json.dumps(bundle))["request"]
        return self.call("buildRequestDraft", request, options or {"clientName": "t"})

    def test_the_draft_of_a_modern_request(self) -> None:
        draft = self.draft({"method": "tools/call", "tool": "convert_length", "args": {"value": 1}})
        self.assertEqual(
            draft["headers"],
            {"Content-Type": "application/json", "Accept": "application/json",
             "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "convert_length"},
        )
        self.assertEqual(
            draft["body"],
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "convert_length", "arguments": {"value": 1},
                "_meta": {META_VERSION: "2026-07-28", META_CAPS: {}, META_CLIENT: {"name": "t", "version": "1.0"}},
            }},
        )
        self.assertEqual(json.loads(draft["bodyText"]), draft["body"])
        listen = self.draft({"method": "subscriptions/listen"})
        self.assertEqual(listen["headers"]["Accept"], "text/event-stream")
        self.assertEqual(listen["body"]["params"]["notifications"], {"toolsListChanged": True})

    def test_omit_and_headers_break_the_request_on_purpose(self) -> None:
        draft = self.draft({
            "method": "tools/call", "tool": "t",
            "omit": ["meta-version", "capabilities", "version-header", "method-header", "name-header"],
            "headers": {"accept": "text/plain", "X-Extra": "1"},
        })
        self.assertEqual(draft["headers"], {"Content-Type": "application/json", "accept": "text/plain", "X-Extra": "1"})
        self.assertEqual(list(draft["body"]["params"]["_meta"]), [META_CLIENT])
        self.assertNotIn("_meta", self.draft({"method": "tools/list", "omit": ["meta"]})["body"]["params"])
        # params are kept, and their _meta wins over the automatic one.
        custom = self.draft({"method": "tools/list", "params": {"cursor": "c", "_meta": {META_VERSION: "1999-01-01"}}})
        self.assertEqual(custom["body"]["params"]["cursor"], "c")
        self.assertEqual(custom["body"]["params"]["_meta"][META_VERSION], "1999-01-01")
        self.assertIn(META_CAPS, custom["body"]["params"]["_meta"])

    def test_the_draft_of_a_legacy_request(self) -> None:
        draft = self.draft({"mode": "2025", "method": "tools/call", "tool": "t", "id": 9})
        self.assertEqual(draft["headers"], {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        self.assertEqual(draft["body"], {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "t", "arguments": {}}})
        init = self.draft({"mode": "2025", "method": "initialize"})["body"]["params"]
        self.assertEqual((init["protocolVersion"], init["capabilities"], init["clientInfo"]["name"]), ("2025-11-25", {}, "t"))
        self.assertNotIn("id", self.draft({"mode": "2025", "method": "notifications/initialized"})["body"])

    def test_param_headers_follow_the_tool_and_the_server_accepts_them(self) -> None:
        header_params = {"echo_header_params": [{"header": "Region", "argument": "region"}, {"header": "Greeting", "argument": "greeting"}, {"header": "Count", "argument": "count"}]}
        draft = self.draft(
            {"method": "tools/call", "tool": "echo_header_params", "args": {"region": "kr", "greeting": "안녕 하세요", "count": 3}},
            {"headerParams": header_params},
        )
        self.assertEqual(draft["headers"]["Mcp-Param-Region"], "kr")
        self.assertEqual(draft["headers"]["Mcp-Param-Count"], "3")
        greeting = draft["headers"]["Mcp-Param-Greeting"]
        self.assertTrue(greeting.startswith("=?base64?") and greeting.endswith("?="))
        self.assertEqual(decode_mcp_header_value(greeting), "안녕 하세요")
        for value in ("a b", " lead", "=?base64?x?=", "x\ty"):
            encoded = self.call("paramHeaderValue", value)
            self.assertEqual(decode_mcp_header_value(encoded), value, value)

    def test_an_exact_body_and_exact_headers_are_kept(self) -> None:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"odd": True}}
        self.assertEqual(self.draft({"body": body})["body"], body)
        curl = self.parse("curl -X POST 'http://h/mcp' -H 'Only: one' -d 'not json'")["request"]
        draft = self.call("buildRequestDraft", curl, {})
        self.assertEqual((draft["headers"], draft["bodyText"]), ({"Only": "one"}, "not json"))

    def test_the_bundle_of_a_form_reads_back_as_the_same_request(self) -> None:
        form = {"url": "/mcp?cfg=abc", "mode": "2026", "method": "tools/call", "tool": "convert_length", "args": {"value": 2}}
        plain = self.call("requestBundle", form, None, None, {"clientName": "t"})
        self.assertEqual(plain, form)
        legacy = self.call("requestBundle", {"url": "", "mode": "2025", "method": "tools/list", "tool": "ignored"}, None, None, {})
        self.assertEqual(legacy, {"mode": "2025", "method": "tools/list"})
        # Edited headers and an edited body come out as headers / omit / body.
        draft = self.call("buildRequestDraft", self.parse(json.dumps(form))["request"], {"clientName": "t"})
        headers = dict(draft["headers"], **{"Mcp-Method": "tools/list", "Authorization": "Bearer x"})
        del headers["MCP-Protocol-Version"]
        body = json.loads(draft["bodyText"])
        del body["params"]["_meta"]
        edited = self.call("requestBundle", form, headers, json.dumps(body), {"clientName": "t"})
        self.assertEqual(edited["headers"], {"Mcp-Method": "tools/list", "Authorization": "Bearer x"})
        self.assertEqual(edited["omit"], ["version-header"])
        self.assertEqual(edited["body"], body)
        again = self.call("buildRequestDraft", self.parse(json.dumps(edited))["request"], {"clientName": "t"})
        self.assertEqual(again["headers"], headers)
        self.assertEqual(again["body"], body)

    def test_the_format_text_names_every_field_and_its_examples_parse(self) -> None:
        text = self.call("requestFormatText")
        for field in ("url", "mode", "method", "tool", "args", "params", "id", "headers", "omit", "body"):
            self.assertIn(f"- {field}: ", text)
        for name in self.call("parseRequestPaste", '{"method":"x","omit":["?"]}')["error"].split(": ")[1].split(", "):
            self.assertIn(name, text)
        examples = [line for line in text.splitlines() if line.startswith("{")]
        self.assertEqual(len(examples), 2)
        for example in examples:
            parsed = self.parse(example)
            self.assertEqual((parsed["ok"], parsed["kind"]), (True, "bundle"), example)
        # No name of a company or of a deployed host in what is handed to an AI.
        self.assertNotRegex(text, r"https?://")


class ConfigPasteTest(UiCoreTestCase):
    """"주소로 불러오기": an address, a config code or a config JSON."""

    def test_text_that_is_not_json_is_an_address_or_a_code(self) -> None:
        for text in ("eyJhIjoxfQ", "http://h/mcp?cfg=eyJhIjoxfQ", "X-MCP-Test-Scenario: cfg:eyJhIjoxfQ"):
            parsed = self.call("parseConfigPaste", f"  {text} ", self.defaults)
            self.assertEqual((parsed["ok"], parsed["kind"], parsed["text"]), (True, "token", text))

    def test_a_config_json_is_the_part_that_differs(self) -> None:
        patch = {"toolCall": {"mode": "is-error"}, "auth": {"mode": "oauth"}}
        parsed = self.call("parseConfigPaste", json.dumps(patch, indent=2), self.defaults)
        self.assertEqual((parsed["ok"], parsed["kind"], parsed["patch"]), (True, "json", patch))
        wrapped = self.call("parseConfigPaste", json.dumps({"config": patch}), self.defaults)
        self.assertEqual(wrapped["patch"], patch)
        self.assertEqual(self.call("parseConfigPaste", "{}", self.defaults)["patch"], {})
        # What the screen copies as "설정 JSON" reads back, and is what the token holds.
        config = normalize_config(merged(patch))
        text = self.call("configJsonText", config, self.defaults)
        self.assertEqual(json.loads(text), compact_config(config))
        self.assertEqual(self.call("parseConfigPaste", text, self.defaults)["patch"], compact_config(config))
        self.assertEqual(normalize_config(json.loads(text)), config)
        self.assertEqual(self.call("configJsonText", default_config(), self.defaults), "{}")

    def test_what_is_wrong_is_said_in_one_line(self) -> None:
        cases = {
            "": "붙여 넣은 내용이 없습니다",
            '{"toolCall": {"mode": }}': "JSON을 읽지 못했습니다: 1번째 줄 23번째 글자 근처",
            '{"toolCal": {"mode": "sse"}, "toolsList": {"pageSiz": 2, "pageSize": 1}}': "모르는 설정: toolCal, toolsList.pageSiz",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                parsed = self.call("parseConfigPaste", text, self.defaults)
                self.assertFalse(parsed["ok"])
                self.assertTrue(parsed["error"].startswith(expected), parsed["error"])
        self.assertEqual(self.call("unknownConfigPaths", {"rawResponses": [{"anything": 1}], "mcp": {"identifier": "x"}}, self.defaults), [])

    def test_an_mcp_address_without_a_config(self) -> None:
        cases = (
            ("https://mcp.example.com/mcp", True),
            (" http://localhost:8000/mcp/ ", True),
            ("/mcp", True),
            ("https://h.example/prefix/mcp?x=1#top", True),
            ("https://mcp.example.com/mcp?cfg=abc", False),
            ("/mcp?x=1&cfg=abc", False),
            ("https://www.example.com/docs", False),
            ("https://www.example.com", False),
            ("https://www.example.com/mcp-docs", False),
            ("eyJ0b29sQ2FsbCI6e319", False),
            ("cfg:eyJ0b29sQ2FsbCI6e319", False),
            ('{"toolCall": {}}', False),
            ("", False),
        )
        results = self.run_calls([["isPlainMcpAddress", text] for text, _ in cases])
        self.assertEqual([result["value"] for result in results], [expected for _, expected in cases])

    def test_option_tiers_and_changed_counts(self) -> None:
        tiers = {path: self.call("optionTier", option) for path, option in self.options.items()}
        self.assertEqual(
            [path for path, tier in tiers.items() if tier == "basic"],
            ["mcp.identifier", "mcp.serviceName", "serverEra", "auth.mode"],
        )
        for path, option in self.options.items():
            if tiers[path] != "basic":
                self.assertEqual(tiers[path], "advanced" if option["section"] == "build" else "error", path)
        zero = {"basic": 0, "advanced": 0, "error": 0}
        self.assertEqual(self.call("changedCounts", self.defaults, self.schema), zero)
        cases = (
            ({"serverEra": "legacy", "mcp": {"identifier": "x"}}, {**zero, "basic": 2}),
            ({"strict2026": False, "allowedOrigins": ["https://a.example"], "session": {"validate": True}},
             {**zero, "advanced": 3}),
            ({"toolCall": {"mode": "sse"}, "rawResponses": [{"method": "ping", "status": 200}]}, {**zero, "error": 2}),
            ({"auth": {"mode": "oauth", "enforceScope": True}, "server": {"httpStatus": 401}},
             {"basic": 1, "advanced": 1, "error": 1}),
        )
        for patch, expected in cases:
            with self.subTest(patch=patch):
                self.assertEqual(self.call("changedCounts", merged(patch), self.schema), expected)
        # A config field the form has no control for is not counted.
        self.assertEqual(self.call("changedCounts", merged({"protocolEra": "2026"}), self.schema), zero)

    def test_the_format_text_lists_every_option_by_section(self) -> None:
        text = self.call("configFormatText", self.schema, self.meta["sections"], "http://h:1/")
        self.assertIn("GET http://h:1/scenario/meta", text)
        lines = text.splitlines()
        build, error = lines.index("[서버 만들기]"), lines.index("[에러 넣기]")
        self.assertLess(build, error)
        for option in self.schema:
            with self.subTest(path=option["path"]):
                line = next(item for item in lines if item.startswith(option["path"] + " ("))
                self.assertIn(option["label"], line)
                self.assertEqual(build < lines.index(line) < error, option["section"] == "build")
                if option["kind"] == "enum":
                    for choice in option["choices"]:
                        self.assertIn(json.dumps(choice["value"]), line)
        self.assertIn('toolCall.mode (enum, "normal")', text)
        self.assertIn("toolsList.pageSize (int, 0) 한 페이지의 툴 수 — 0~100", text)
        # The two examples are configs the server accepts.
        examples = [json.loads(line) for line in lines if line.startswith("{")]
        self.assertEqual(len(examples), 2)
        for example in examples:
            self.assertEqual(self.call("unknownConfigPaths", example, self.defaults), [])
            normalize_config(example)


class AddressGroupsTest(UiCoreTestCase):
    def test_groups_become_rows_newest_first(self) -> None:
        token = encode_config_token(normalize_config({"toolCall": {"mode": "is-error"}, "serverEra": "legacy"}))
        groups = [
            {"cfg": None, "count": 4, "screen": 1, "lastSeq": 9, "lastTimestamp": "2026-01-02T03:04:05.000Z"},
            {"cfg": token, "count": 2, "screen": 0, "lastSeq": 12, "lastTimestamp": "2026-01-02T03:04:09.000Z"},
            {"cfg": "%%%", "count": 1, "screen": 0, "lastSeq": 3, "lastTimestamp": "2026-01-02T03:04:01.000Z"},
        ]
        rows = self.call("addressGroups", groups, self.defaults, self.schema)
        self.assertEqual([row["lastSeq"] for row in rows], [12, 9, 3])
        self.assertEqual(
            [(row["cfg"], row["plain"], row["title"], row["count"], row["screen"], row["readable"]) for row in rows],
            [
                (token, False, "지원 스펙 = legacy, tools/call 응답 = is-error", 2, 0, True),
                ("", True, "기본 주소 (설정 없음)", 4, 1, True),
                ("%%%", False, "읽을 수 없는 설정 코드", 1, 0, False),
            ],
        )
        self.assertEqual([chip["path"] for chip in rows[0]["chips"]], ["serverEra", "toolCall.mode"])
        self.assertEqual(self.call("addressGroups", None, self.defaults, self.schema), [])


if __name__ == "__main__":
    unittest.main()
