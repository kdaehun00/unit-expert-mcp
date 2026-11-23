"""The /scenario presets, end to end.

A preset is a button on /scenario. These tests take each preset's patch the way
the page does (patch -> cfg token -> ``/mcp?cfg=<token>``), send one real
request and check that

* the server does what the preset's description says, and
* the entry the request leaves in the inspection log is read by the /inspect
  screen logic (UI_CORE_JS, run under node) the way a user would expect.

The second half needs node and is skipped without it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from mcp_test_support import (
    CONVERT_ARGS,
    ERR_HEADER_MISMATCH,
    ERR_INTERNAL,
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_UNSUPPORTED_VERSION,
    HttpServerTestCase,
    legacy_initialize,
    legacy_request,
    modern_call,
    modern_headers,
    modern_request,
    parse_sse,
    sentinel,
)
from unit_expert_mcp.server import (
    DEFAULT_DELAY_SECONDS,
    PROTOCOL_VERSION_2026,
    UI_CORE_JS,
    compact_config,
    decode_config_token,
    encode_config_token,
    inspect_snapshot,
    preset_config,
    scenario_meta,
    scenario_presets,
)

NODE = shutil.which("node")

PRESETS = {preset["id"]: preset for preset in scenario_presets()}
INJECTION_IDS = [preset["id"] for preset in scenario_presets() if preset["category"] == "injection"]
MATRIX_IDS = [preset["id"] for preset in scenario_presets() if preset["category"] == "matrix"]

# The option each injection preset leaves in entry["injected"] of its
# representative request. Every preset names one, so /inspect never says "이
# 응답에 기록된 주입 옵션은 없습니다" about a response a preset made.
PRESET_INJECTED_LABELS = {
    "auth-oauth-401": ["auth.mode=oauth"],
    "response-delay": ["server.delaySeconds=0.3"],
    "tools-list-error": ["toolsList.mode=json-rpc-error"],
    "tools-list-empty": ["toolsList.mode=empty"],
    "tools-list-paged": ["toolsList.pageSize=2"],
    "tool-call-is-error": ["toolCall.mode=is-error"],
    "tool-call-json-rpc-error": ["toolCall.mode=json-rpc-error"],
    "tool-call-http-500": ["toolCall.mode=http-status"],
    "tool-call-disconnect": ["toolCall.mode=disconnect"],
    "tool-call-sse": ["toolCall.mode=sse"],
    "discover-no-modern-version": ["discover.mode=no-modern-version"],
    "discover-http-5xx": ["discover.mode=http-5xx"],
    "legacy-probe-http-400": ["legacyModernProbe.mode=http-400"],
    "session-validate": ["session.validate"],
}
# What the screen calls each of them. A response that was only delayed keeps
# the kind it would have had, and a dropped connection is "연결 종료".
PRESET_KINDS = {
    **{preset_id: "injected" for preset_id in PRESET_INJECTED_LABELS},
    "response-delay": "ok",
    "tool-call-disconnect": "closed",
}

DRIVER = """
const UI = require(process.argv[2]);
const calls = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(calls.map(([name, ...args]) => UI[name](...args))));
"""


def preset_token(preset_id: str, extra: dict[str, Any] | None = None) -> str:
    """The cfg token /scenario makes for a preset pressed on the default form."""
    config = preset_config(PRESETS[preset_id]["patch"])
    for section, values in (extra or {}).items():
        config[section].update(values)
    return encode_config_token(config)


class PresetCase(HttpServerTestCase):
    def send(
        self,
        token: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], Any, bytes, dict[str, Any] | None]:
        """POST to /mcp?cfg=<token>. Returns the response and its log entry."""
        before = max((entry["seq"] for entry in inspect_snapshot()), default=0)
        request_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        request_headers.update(headers or {})
        path = f"/mcp?cfg={token}" if token else "/mcp"
        try:
            status, response_headers, raw = self.request(
                "POST", path, headers=request_headers, body=json.dumps(payload).encode("utf-8")
            )
        except (ConnectionError, OSError):
            status, response_headers, raw = 0, {}, b""
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        entry = None
        deadline = time.monotonic() + 2
        while entry is None and time.monotonic() < deadline:
            fresh = [item for item in inspect_snapshot() if item["seq"] > before]
            entry = fresh[-1] if fresh else None
            if entry is None:
                time.sleep(0.01)
        return status, response_headers, parsed, raw, entry

    def legacy_call(self, token: str):
        return self.send(token, legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS))

    def modern(self, token: str, method: str):
        return self.send(token, modern_request(method), modern_headers(method))

    def representative(self, preset_id: str):
        """One request that shows what the preset does: (status, headers, json, raw, entry)."""
        if preset_id == "response-delay":
            # The preset waits 5 s; the same switch with a short delay keeps the suite fast.
            return self.send(preset_token(preset_id, {"server": {"delaySeconds": 0.3}}), legacy_request("tools/list"))
        token = preset_token(preset_id)
        if preset_id in {"auth-oauth-401", "tools-list-error", "tools-list-empty", "tools-list-paged", "session-validate"}:
            return self.send(token, legacy_request("tools/list"))
        if preset_id.startswith("tool-call-"):
            return self.legacy_call(token)
        if preset_id.startswith("discover-") or preset_id == "legacy-probe-http-400":
            return self.modern(token, "server/discover")
        raise AssertionError(f"no representative request for {preset_id}")


class PresetBehaviourTest(PresetCase):
    def test_there_are_fourteen_injection_and_four_matrix_presets(self) -> None:
        self.assertEqual(len(INJECTION_IDS), 14)
        self.assertEqual(len(MATRIX_IDS), 4)

    def test_every_preset_has_a_behaviour_test(self) -> None:
        tested = {
            name[len("test_preset_"):].replace("_", "-")
            for name in dir(self)
            if name.startswith("test_preset_")
        }
        self.assertEqual(tested, set(INJECTION_IDS))

    def test_preset_auth_oauth_401(self) -> None:
        status, headers, _, _, _ = self.representative("auth-oauth-401")
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata=", headers["www-authenticate"])

    def test_preset_response_delay(self) -> None:
        self.assertEqual(
            compact_config(preset_config(PRESETS["response-delay"]["patch"])), {"server": {"delayEnabled": True}}
        )
        self.assertEqual(PRESETS["response-delay"]["patch"]["server"]["delaySeconds"], DEFAULT_DELAY_SECONDS)
        started = time.monotonic()
        status, _, body, _, entry = self.representative("response-delay")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(len(body["result"]["tools"]), 6)
        self.assertGreaterEqual(entry["durationMs"], 300)

    def test_preset_tools_list_error(self) -> None:
        status, _, body, _, _ = self.representative("tools-list-error")
        self.assertEqual((status, body["error"]["code"]), (200, ERR_INTERNAL))

    def test_preset_tools_list_empty(self) -> None:
        status, _, body, _, _ = self.representative("tools-list-empty")
        self.assertEqual((status, body["result"]["tools"]), (200, []))

    def test_preset_tools_list_paged(self) -> None:
        token = preset_token("tools-list-paged")
        names: list[str] = []
        cursor = None
        for _ in range(10):
            params = {"cursor": cursor} if cursor else {}
            _, _, body, _, _ = self.send(token, legacy_request("tools/list", **params))
            page = body["result"]["tools"]
            self.assertLessEqual(len(page), 2)
            names.extend(tool["name"] for tool in page)
            cursor = body["result"].get("nextCursor")
            if not cursor:
                break
        self.assertEqual(len(names), 6)
        self.assertEqual(len(set(names)), 6)

    def test_preset_tool_call_is_error(self) -> None:
        status, _, body, _, _ = self.representative("tool-call-is-error")
        self.assertEqual((status, body["result"]["isError"]), (200, True))
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        _, _, modern_body, _, _ = self.send(preset_token("tool-call-is-error"), payload, headers)
        self.assertIs(modern_body["result"]["isError"], True)

    def test_preset_tool_call_json_rpc_error(self) -> None:
        status, _, body, _, _ = self.representative("tool-call-json-rpc-error")
        self.assertEqual((status, body["error"]["code"]), (200, ERR_INTERNAL))

    def test_preset_tool_call_http_500(self) -> None:
        status, headers, body, raw, _ = self.representative("tool-call-http-500")
        self.assertEqual(status, 500)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertIsNone(body)
        self.assertTrue(raw)

    def test_preset_tool_call_disconnect(self) -> None:
        status, _, _, raw, entry = self.representative("tool-call-disconnect")
        self.assertEqual((status, raw), (0, b""))
        self.assertEqual(entry["status"], 0)

    def test_preset_tool_call_sse(self) -> None:
        status, headers, _, raw, _ = self.representative("tool-call-sse")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        messages = parse_sse(raw)
        self.assertEqual(len(messages), 3)
        self.assertIn("result", messages[-1])
        self.assertTrue(all("method" in message for message in messages[:-1]))

    def test_preset_discover_no_modern_version(self) -> None:
        status, _, body, _, _ = self.representative("discover-no-modern-version")
        self.assertEqual(status, 200)
        versions = body["result"]["supportedVersions"]
        self.assertTrue(versions)
        self.assertNotIn(PROTOCOL_VERSION_2026, versions)

    def test_preset_discover_http_5xx(self) -> None:
        status, _, body, raw, _ = self.representative("discover-http-5xx")
        self.assertEqual(status, 503)
        self.assertIsNone(body)
        self.assertTrue(raw)

    def test_preset_legacy_probe_http_400(self) -> None:
        status, _, body, _, entry = self.representative("legacy-probe-http-400")
        self.assertEqual((status, body["error"]["code"]), (400, ERR_INVALID_REQUEST))
        self.assertEqual(entry["serverEra"], "legacy")
        # A legacy client is still served.
        ok_status, _, ok_body, _, _ = self.send(preset_token("legacy-probe-http-400"), legacy_initialize())
        self.assertEqual(ok_status, 200)
        self.assertIn("protocolVersion", ok_body["result"])

    def test_preset_session_validate(self) -> None:
        token = preset_token("session-validate")
        self.assertEqual(self.representative("session-validate")[0], 400)
        status, _, _, _, _ = self.send(token, legacy_request("tools/list"), {"Mcp-Session-Id": "unknown"})
        self.assertEqual(status, 404)
        _, headers, _, _, _ = self.send(token, legacy_initialize())
        session_id = headers["mcp-session-id"]
        status, _, body, _, _ = self.send(token, legacy_request("tools/list"), {"Mcp-Session-Id": session_id})
        self.assertEqual((status, len(body["result"]["tools"])), (200, 6))
        deleted, _, _ = self.request("DELETE", f"/mcp?cfg={token}", headers={"Mcp-Session-Id": session_id})
        self.assertIn(deleted, (200, 204))
        status, _, _, _, _ = self.send(token, legacy_request("tools/list"), {"Mcp-Session-Id": session_id})
        self.assertEqual(status, 404)

    def test_presets_pressed_together_merge(self) -> None:
        """Four presets pressed one after another: every one of them still acts."""
        config = preset_config({})
        for preset_id in ("tools-list-paged", "tool-call-is-error", "session-validate", "legacy-probe-http-400"):
            patch = PRESETS[preset_id]["patch"]
            for key, value in patch.items():
                if isinstance(value, dict):
                    config[key].update(value)
                else:
                    config[key] = value
        token = encode_config_token(config)
        self.assertEqual(
            compact_config(decode_config_token(token)),
            {
                "serverEra": "legacy",
                "toolsList": {"pageSize": 2},
                "toolCall": {"mode": "is-error"},
                "session": {"validate": True},
                "legacyModernProbe": {"mode": "http-400"},
            },
        )
        self.assertEqual(self.modern(token, "server/discover")[0], 400)
        self.assertEqual(self.send(token, legacy_request("tools/list"))[0], 400)  # no session id
        _, headers, _, _, _ = self.send(token, legacy_initialize())
        session = {"Mcp-Session-Id": headers["mcp-session-id"]}
        _, _, listed, _, _ = self.send(token, legacy_request("tools/list"), session)
        self.assertEqual(len(listed["result"]["tools"]), 2)
        self.assertIn("nextCursor", listed["result"])
        _, _, called, _, _ = self.send(
            token, legacy_request("tools/call", name="convert_length", arguments=CONVERT_ARGS), session
        )
        self.assertIs(called["result"]["isError"], True)


class MatrixPresetBehaviourTest(PresetCase):
    """What the four matrix cells say a client sees when it talks to the server directly."""

    def test_modern_client_on_a_modern_server_works(self) -> None:
        token = preset_token("matrix-modern-modern")
        status, _, body, _, entry = self.modern(token, "server/discover")
        self.assertEqual(status, 200)
        self.assertIn(PROTOCOL_VERSION_2026, body["result"]["supportedVersions"])
        self.assertEqual(entry["serverEra"], "modern")

    def test_modern_client_on_a_legacy_server_gets_no_discover_result(self) -> None:
        status, _, body, _, entry = self.modern(preset_token("matrix-modern-legacy"), "server/discover")
        self.assertEqual((status, body["error"]["code"]), (200, ERR_METHOD_NOT_FOUND))
        self.assertEqual((entry["era"], entry["serverEra"]), ("2026", "legacy"))

    def test_legacy_client_on_a_legacy_server_works(self) -> None:
        status, headers, body, _, _ = self.send(preset_token("matrix-legacy-legacy"), legacy_initialize())
        self.assertEqual(status, 200)
        self.assertIn("protocolVersion", body["result"])
        self.assertIn("mcp-session-id", headers)

    def test_legacy_client_on_a_modern_server_is_refused(self) -> None:
        """The cell's own wording: initialize gets 400 + -32022."""
        self.assertIn("400(-32022)", PRESETS["matrix-legacy-modern"]["expect"])
        status, _, body, _, _ = self.send(preset_token("matrix-legacy-modern"), legacy_initialize())
        self.assertEqual((status, body["error"]["code"]), (400, ERR_UNSUPPORTED_VERSION))


@unittest.skipUnless(NODE, "node is not installed")
class RecordedExchangeOnScreenTest(PresetCase):
    """Entries of real requests, read by the /inspect screen logic."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.workdir = tempfile.TemporaryDirectory()
        root = Path(cls.workdir.name)
        (root / "ui_core.js").write_text(UI_CORE_JS, encoding="utf-8")
        (root / "driver.js").write_text(DRIVER, encoding="utf-8")
        cls.root = root
        cls.error_codes = scenario_meta()["errorCodes"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workdir.cleanup()
        super().tearDownClass()

    def ui(self, name: str, *args: Any) -> Any:
        calls = self.root / "calls.json"
        calls.write_text(json.dumps([[name, *args]], ensure_ascii=False), encoding="utf-8")
        done = subprocess.run(
            [NODE, str(self.root / "driver.js"), str(self.root / "ui_core.js"), str(calls)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**os.environ, "TZ": "Asia/Seoul"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)[0]

    def kind(self, entry: dict[str, Any]) -> str:
        return self.ui("classifyExchange", entry, self.error_codes)["kind"]

    def test_every_injection_preset_names_its_option_in_the_log(self) -> None:
        self.assertEqual(set(PRESET_INJECTED_LABELS), set(INJECTION_IDS))
        for preset_id in INJECTION_IDS:
            with self.subTest(preset=preset_id):
                entry = self.representative(preset_id)[4]
                self.assertEqual(entry["injected"], PRESET_INJECTED_LABELS[preset_id])

    def test_every_injection_preset_shows_as_injected(self) -> None:
        """All fourteen read as "주입" on the screen, apart from the two whose
        response itself is not a made-up one: the delay (a normal answer with
        the delay badge) and the dropped connection ("연결 종료")."""
        kinds = {preset_id: self.kind(self.representative(preset_id)[4]) for preset_id in INJECTION_IDS}
        self.assertEqual(kinds, PRESET_KINDS)

    def test_the_disconnect_preset_shows_as_closed_with_its_option(self) -> None:
        entry = self.representative("tool-call-disconnect")[4]
        info = self.ui("classifyExchange", entry, self.error_codes)
        self.assertEqual((info["kind"], info["detail"]), ("closed", "toolCall.mode=disconnect"))

    def test_the_delay_preset_shows_as_a_normal_answer_with_the_delay(self) -> None:
        entry = self.representative("response-delay")[4]
        info = self.ui("classifyExchange", entry, self.error_codes)
        self.assertEqual((info["kind"], info["label"]), ("ok", "정상"))
        self.assertEqual(info["delays"], ["server.delaySeconds=0.3"])
        self.assertEqual(info["detail"], "늦게 답함: server.delaySeconds=0.3")

    def test_a_refusal_without_any_injection_is_a_rejection(self) -> None:
        """Only a refusal that no option caused reads as "검증 거부"."""
        entry = self.send("", legacy_request("tools/list"), {"Accept": "application/json"})[4]
        self.assertEqual(entry["injected"], [])
        self.assertEqual(self.kind(entry), "rejected")

    def test_a_real_refusal_shows_its_code_name(self) -> None:
        entry = self.send("", modern_request("tools/list"), {"MCP-Protocol-Version": PROTOCOL_VERSION_2026})[4]
        info = self.ui("classifyExchange", entry, self.error_codes)
        self.assertEqual((info["kind"], info["code"], info["codeName"]), ("rejected", ERR_HEADER_MISMATCH, "HeaderMismatch"))

    def test_header_body_rows_of_a_real_echo_header_params_call(self) -> None:
        arguments = {"region": "kr-central", "greeting": "안녕하세요", "count": 3, "verbose": True, "note": "n"}
        payload, headers = modern_call(
            "echo_header_params",
            arguments,
            param_headers={
                "Mcp-Param-Region": "kr-central",
                "Mcp-Param-Greeting": sentinel("안녕하세요"),
                "Mcp-Param-Count": "3",
                "Mcp-Param-Verbose": "true",
            },
        )
        status, _, _, _, entry = self.send("", payload, headers)
        self.assertEqual(status, 200)
        rows = {row["header"]: row for row in self.ui("headerBodyRows", entry)}
        self.assertEqual(
            sorted(rows),
            [
                "MCP-Protocol-Version", "Mcp-Method", "Mcp-Name",
                "Mcp-Param-Count", "Mcp-Param-Greeting", "Mcp-Param-Region", "Mcp-Param-Verbose",
            ],
        )
        self.assertEqual({row["state"] for row in rows.values()}, {"match"})
        self.assertEqual(rows["Mcp-Param-Greeting"]["decodedValue"], "안녕하세요")
        self.assertEqual(rows["Mcp-Param-Count"]["bodyLabel"], "params.arguments.count")

    def test_header_body_rows_of_a_real_base64_tool_name(self) -> None:
        payload, headers = modern_call("convert_length", CONVERT_ARGS)
        headers["Mcp-Name"] = sentinel("convert_length")
        status, _, _, _, entry = self.send("", payload, headers)
        self.assertEqual(status, 200)
        row = next(row for row in self.ui("headerBodyRows", entry) if row["header"] == "Mcp-Name")
        self.assertEqual((row["decodedValue"], row["state"]), ("convert_length", "match"))

    def test_a_missing_param_header_has_a_row_in_the_table(self) -> None:
        """The server refuses the call (-32020). The table shows the header
        that did not arrive, from the tool's schema or, without it, from the
        header the refusal names."""
        payload, headers = modern_call("echo_header_params", {"region": "kr-central"})
        status, _, body, _, entry = self.send("", payload, headers)
        self.assertEqual((status, body["error"]["code"]), (400, ERR_HEADER_MISMATCH))
        self.assertIn("Mcp-Param-Region", body["error"]["message"])
        header_params = {"echo_header_params": [{"header": "Region", "argument": "region"}]}
        for args in ((entry, header_params), (entry,)):
            with self.subTest(with_schema=len(args) == 2):
                rows = self.ui("headerBodyRows", *args)
                self.assertEqual(
                    [row["header"] for row in rows],
                    ["MCP-Protocol-Version", "Mcp-Method", "Mcp-Name", "Mcp-Param-Region"],
                )
                missing = rows[-1]
                self.assertEqual(
                    (missing["headerValue"], missing["bodyLabel"], missing["bodyValue"], missing["stateLabel"]),
                    (None, "params.arguments.region", "kr-central", "헤더 없음"),
                )

    def test_the_default_filters_hide_the_scenario_preview(self) -> None:
        """/scenario preview -> "이 URL의 요청 기록 보기": the preview's own requests
        are hidden by the default filters (documented in the README)."""
        token = preset_token("tools-list-paged")
        entry = self.send(token, legacy_request("tools/list"), {"X-Unit-Expert-Source": "scenario-preview"})[4]
        self.assertEqual((entry["source"], entry["cfg"]), ("scenario-preview", token))
        filters = self.ui("filtersFromQuery", f"?cfg={token}")
        self.assertIs(filters["hideScreen"], True)
        self.assertEqual(self.ui("filterExchanges", [entry], {"cfg": token, "hideScreen": True}), [])
        self.assertEqual(len(self.ui("filterExchanges", [entry], {"cfg": token, "hideScreen": False})), 1)
        self.assertIs(self.ui("defaultFilters")["hideScreen"], True)


if __name__ == "__main__":
    unittest.main()
